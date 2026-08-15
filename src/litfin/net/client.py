"""PoliteClient -- the one and only outbound HTTP entry point.

Every fetch in the system goes through `get()`. The gate chain runs in a
deliberate order:

  1. Compliance gate      cheapest + most consequential -> before any socket
  2. URL allowlist        a parser bug cannot fetch an unvetted host
  3. Global daily budget  persisted, survives restarts
  4. Circuit breaker      fail fast on a host already known down
  5. robots.txt           cached 24h, honored for * and for our own token
  6. Per-host rate limit  token bucket, blocking
  7. Conditional request  If-None-Match / If-Modified-Since
  8. Send, with retry

Two behaviors worth not "simplifying" later:

  - On 429/503 we honor Retry-After AND permanently halve that host's rate for
    the remainder of the run. A host that says slow down once will say it
    again; adapting is more polite than rediscovering the limit.

  - A 403 on a Tier B source is NOT retried and escalates to a compliance
    alert. A WAF block against an honest, identified client is evidence of
    non-consent, not a transient error.

Never add a headless-browser fallback here. Defeating a bot check converts a
technical block into deliberate circumvention -- a materially worse posture.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

import httpx

from ..compliance.registry import get_policy
from ..compliance.status import (
    CompliancePermanentlyBlocked,
    ComplianceError,
    ComplianceUrlOutOfScope,
    Purpose,
    SourcePolicy,
    ToSStatus,
    assert_fetch_allowed,
)
from ..config import Config
from .breaker import BreakerRegistry, CircuitOpen
from .budget import BudgetExceeded, GlobalBudget
from .httpcache import HttpCache
from .ratelimit import (
    HostDailyCapExceeded,
    HostGovernor,
    HostHourlyCapExceeded,
    rate_key_for,
    set_courtlistener_member,
)
from .robots import RobotsCache

log = logging.getLogger("litfin.net")

RETRY_ON = frozenset({408, 425, 429, 500, 502, 503, 504})
NEVER_RETRY = frozenset({400, 401, 403, 404, 410, 451})


class FetchBlocked(RuntimeError):
    """Raised when a fetch is refused by policy, robots, budget, or breaker."""


class RobotsDisallowed(FetchBlocked):
    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"robots.txt disallows {url}: {reason}")
        self.url = url


class ConsentRefused(FetchBlocked):
    """A host actively refused an honest, identified client (403/401)."""

    def __init__(self, source_id: str, url: str, status: int) -> None:
        super().__init__(
            f"Host returned {status} to an identified client for source "
            f"{source_id!r} at {url}. This is treated as a refusal of consent, "
            f"not a transient error. Do NOT work around it with a headless "
            f"browser -- record the finding and mark the source PROHIBITED or "
            f"escalate for a human ToS read."
        )
        self.source_id = source_id
        self.url = url
        self.status = status


@dataclass(slots=True)
class Response:
    """A fetched (or revalidated) resource."""

    url: str
    status: int
    body: bytes
    headers: dict[str, str]
    from_cache: bool
    not_modified: bool
    sha256: str
    elapsed_s: float
    rate_key: str

    @property
    def text(self) -> str:
        # Prefer the declared charset; fall back to utf-8 with replacement so
        # a single bad byte never kills a run.
        ctype = self.headers.get("content-type", "")
        charset = "utf-8"
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()


@dataclass(slots=True)
class FetchStats:
    requests: int = 0
    cache_hits: int = 0
    not_modified: int = 0
    retries: int = 0
    blocked: int = 0
    ai_signals: dict[str, str] = field(default_factory=dict)


class PoliteClient:
    def __init__(
        self,
        cfg: Config,
        *,
        budget: GlobalBudget,
        cache: HttpCache | None = None,
        governor: HostGovernor | None = None,
        breakers: BreakerRegistry | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.cfg = cfg
        self.budget = budget
        self.cache = cache or HttpCache(cfg.cache_dir)
        self.governor = governor or HostGovernor()
        self.breakers = breakers or BreakerRegistry(
            failure_threshold=cfg.breaker_failure_threshold,
            open_seconds=cfg.breaker_open_seconds,
        )
        self.stats = FetchStats()

        self._ua = cfg.identity.user_agent

        # A CourtListener token both authenticates us and raises the published
        # rate ceiling. Apply the higher limits only when we actually have one.
        self._cl_token = (cfg.courtlistener_token or "").strip()
        if self._cl_token:
            set_courtlistener_member()

        self._client = httpx.Client(
            headers={
                "User-Agent": self._ua,
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=cfg.timeout_seconds,
            follow_redirects=True,
            transport=transport,
        )
        # robots fetching bypasses the gate chain (it IS the gate), but still
        # goes through the same UA and timeout.
        self.robots = RobotsCache(self._fetch_robots, ttl_seconds=cfg.cache_ttl_hours * 3600)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    def _fetch_robots(self, url: str) -> tuple[int, str]:
        try:
            r = self._client.get(url, timeout=10.0)
            return r.status_code, r.text
        except httpx.HTTPError:
            return 0, ""

    # -- the gate chain ----------------------------------------------------

    def get(
        self,
        url: str,
        *,
        source_id: str,
        accept: str | None = None,
        conditional: bool = True,
        timeout: float | None = None,
        extra_headers: dict[str, str] | None = None,
        reading_terms: bool = False,
    ) -> Response:
        policy = get_policy(source_id)

        # 1 + 2: compliance gate and URL scope.
        #
        # `reading_terms` is the ONE narrow exemption, and it exists because
        # without it the gate is circular: a source stays UNVERIFIED until
        # somebody reads its terms, and its terms cannot be fetched because it
        # is UNVERIFIED. Reading a public terms-of-use page in order to decide
        # whether you may use a site is a categorically different act from
        # harvesting that site's data.
        #
        # It is deliberately not a general escape hatch:
        #   * the URL must be one of the policy's OWN declared tos_urls -- a
        #     literal membership test, not a pattern, so it cannot be widened
        #     into a data fetch;
        #   * PROHIBITED still refuses, because a site that has already
        #     refused consent does not get re-litigated by re-reading it;
        #   * robots.txt, rate limits, the breaker and the budget all still
        #     apply below.
        if reading_terms:
            if policy.status is ToSStatus.PROHIBITED:
                self.stats.blocked += 1
                raise CompliancePermanentlyBlocked(
                    policy.source_id, policy.review_note
                )
            if url not in policy.tos_urls:
                self.stats.blocked += 1
                raise ComplianceUrlOutOfScope(
                    policy.source_id, url, policy.tos_urls
                )
        else:
            try:
                assert_fetch_allowed(
                    policy,
                    url,
                    purpose=self.cfg.purpose,
                    opt_in=self.cfg.unverified_opt_in,
                )
            except ComplianceError:
                self.stats.blocked += 1
                raise

        # 3: global daily budget.
        try:
            self.budget.consume(1)
        except BudgetExceeded:
            self.stats.blocked += 1
            raise
        if self.budget.should_warn():
            log.warning(
                "Global request budget at %.0f%% (%d/%d)",
                100 * self.budget.spent() / max(1, self.budget.cap),
                self.budget.spent(),
                self.budget.cap,
            )

        key = rate_key_for(url)

        # 4: circuit breaker.
        try:
            self.breakers.check(key)
        except CircuitOpen:
            self.stats.blocked += 1
            raise

        # 5: robots.txt. `robots_unavailable` is a per-source determination
        # recorded in the compliance registry -- see SourcePolicy.
        verdict = self.robots.check(
            url,
            self._ua,
            unavailable_allows=(policy.robots_unavailable == "allow"),
        )
        if verdict.ai_signal:
            prior = self.stats.ai_signals.get(source_id)
            if prior != verdict.ai_signal:
                self.stats.ai_signals[source_id] = verdict.ai_signal
                log.warning(
                    "Source %s robots.txt expresses an automated-access "
                    "preference (%s). The '*' group permits us, but a human "
                    "should see this exists.",
                    source_id,
                    verdict.ai_signal,
                )
        if not verdict.allowed:
            self.stats.blocked += 1
            raise RobotsDisallowed(url, verdict.reason)

        # 6: per-host rate limit (per-second bucket + hourly + daily caps).
        try:
            self.governor.acquire(url, timeout=300.0)
        except (HostDailyCapExceeded, HostHourlyCapExceeded):
            self.stats.blocked += 1
            raise

        try:
            return self._send_with_retry(
                url,
                policy=policy,
                key=key,
                accept=accept,
                conditional=conditional,
                timeout=timeout,
                extra_headers=extra_headers,
            )
        finally:
            self.governor.release(key)

    def _send_with_retry(
        self,
        url: str,
        *,
        policy: SourcePolicy,
        key: str,
        accept: str | None,
        conditional: bool,
        timeout: float | None,
        extra_headers: dict[str, str] | None,
    ) -> Response:
        headers: dict[str, str] = {}
        if accept:
            headers["Accept"] = accept
        # The literal word "Token" is required; omitting it is the classic
        # CourtListener auth mistake and yields a silent anonymous request
        # under much lower limits rather than an error.
        if self._cl_token and key == "courtlistener.com":
            headers["Authorization"] = f"Token {self._cl_token}"
        if extra_headers:
            headers.update(extra_headers)
        # 7: conditional request.
        if conditional:
            headers.update(self.cache.conditional_headers(url))

        last_exc: Exception | None = None

        for attempt in range(self.cfg.max_attempts):
            started = time.monotonic()
            try:
                r = self._client.get(
                    url,
                    headers=headers,
                    timeout=timeout or self.cfg.timeout_seconds,
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                self.breakers.record_failure(key)
                if attempt + 1 >= self.cfg.max_attempts:
                    break
                self.stats.retries += 1
                time.sleep(_backoff(attempt))
                continue

            elapsed = time.monotonic() - started
            self.stats.requests += 1
            status = r.status_code

            if status == 304:
                entry = self.cache.get(url)
                self.cache.touch(url)
                self.breakers.record_success(key)
                self.stats.not_modified += 1
                if entry is None:
                    # 304 without a cached body: treat as an empty success and
                    # let the canary decide. Should not happen in practice.
                    return Response(
                        url=url, status=304, body=b"", headers=dict(r.headers),
                        from_cache=True, not_modified=True, sha256="",
                        elapsed_s=elapsed, rate_key=key,
                    )
                return Response(
                    url=url,
                    status=304,
                    body=entry.read_body(),
                    headers=dict(r.headers),
                    from_cache=True,
                    not_modified=True,
                    sha256=entry.sha256,
                    elapsed_s=elapsed,
                    rate_key=key,
                )

            if status in (401, 403) and policy.tier in ("B", "C"):
                # Refusal of consent, not a transient error.
                self.breakers.record_failure(key)
                raise ConsentRefused(policy.source_id, url, status)

            if status in NEVER_RETRY:
                self.breakers.record_failure(key)
                r.raise_for_status()

            if status in RETRY_ON:
                self.breakers.record_failure(key)
                if status in (429, 503):
                    new_rps = self.governor.throttle(key, 0.5)
                    log.warning(
                        "Host %s returned %d; halving rate to %.3f req/s for "
                        "the rest of this run.", key, status, new_rps,
                    )
                    retry_after = _parse_retry_after(r.headers.get("retry-after"))
                else:
                    retry_after = None

                if attempt + 1 >= self.cfg.max_attempts:
                    r.raise_for_status()
                self.stats.retries += 1
                time.sleep(retry_after if retry_after is not None else _backoff(attempt))
                continue

            r.raise_for_status()

            body = r.content
            entry = self.cache.put(
                url,
                status=status,
                body=body,
                etag=r.headers.get("etag"),
                last_modified=r.headers.get("last-modified"),
                content_type=r.headers.get("content-type"),
            )
            self.breakers.record_success(key)
            return Response(
                url=url,
                status=status,
                body=body,
                headers=dict(r.headers),
                from_cache=False,
                not_modified=False,
                sha256=entry.sha256,
                elapsed_s=elapsed,
                rate_key=key,
            )

        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"Exhausted {self.cfg.max_attempts} attempts for {url}")


def _backoff(attempt: int) -> float:
    """Exponential backoff with full-ish jitter, capped at 60s."""
    base = min(2.0 * (2 ** attempt), 60.0)
    return base * random.uniform(0.5, 1.5)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
