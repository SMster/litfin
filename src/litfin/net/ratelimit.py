"""Per-host rate limiting.

The rate-limit key is deliberately NOT the hostname. SEC's fair-access cap
applies to the requester, aggregated across www.sec.gov, efts.sec.gov, and
data.sec.gov. Bucketing per-hostname would let three connectors each run at
the cap simultaneously and get the whole IP banned.

Observed Crawl-delay values (probed live 2026-08-14) are encoded here:
  justice.gov     Crawl-delay: 10  -> 0.10 req/s
  ftc.gov         Crawl-delay: 5   -> 0.20 req/s
  *.uscourts.gov  Crawl-delay: 10  -> 0.10 req/s
  sec.gov         no crawl-delay; the 10 req/s fair-access cap binds instead
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class HostRate:
    rps: float
    burst: int
    daily_cap: int | None = None
    concurrency: int = 1
    # Some hosts publish an HOURLY quota that a per-second rate alone cannot
    # honor. CourtListener is the motivating case: 5/min would permit 300/hour,
    # but the published anonymous limit is 50/hour. Without this, a burst-free
    # steady trickle still blows the quota by 6x.
    hourly_cap: int | None = None


# Deliberate choices worth not "optimizing" later:
#   - SEC at 6 rps, not the 10 the policy permits. A burst overshoot gets you
#     blocked and nothing here is latency-sensitive; 40% headroom is cheap.
#   - Claims agents at 1 request per 10 seconds regardless of what their
#     robots say. These are small vendors and the reputational downside of
#     hammering them dwarfs any throughput gain.
#   - Unknown hosts default RESTRICTIVE, so a newly added connector cannot
#     accidentally be rude before anyone tunes it.
RATES: dict[str, HostRate] = {
    "sec.gov":                 HostRate(6.00, 6, 8000, 4),
    "justice.gov":             HostRate(0.10, 2,  500, 1),
    "ftc.gov":                 HostRate(0.20, 2,  500, 1),
    "uscourts.gov":            HostRate(0.10, 2, 1000, 1),
    "api.govinfo.gov":         HostRate(0.20, 3,  800, 1),
    # CourtListener ANONYMOUS limits (as of May 2026): 5/min, 50/hour,
    # 125/day. These are the defaults because an unauthenticated run must not
    # exceed them. 0.06 rps = 3.6/min, comfortably under the per-minute cap;
    # the hourly and daily caps do the real work.
    #
    # A membership raises this substantially ($10/mo tier 1: 10/min, 75/hour,
    # 300/day). set_courtlistener_member() applies those when a token is
    # configured. Even then the ceiling is far too low to poll -- the
    # intended production path is webhooks, which do not consume read quota.
    "courtlistener.com":       HostRate(0.06, 2,  110, 1, hourly_cap=45),
    "securities.stanford.edu": HostRate(0.20, 2,  300, 1),
    "courts.delaware.gov":     HostRate(0.15, 1,  400, 1),
    "claims-agent":            HostRate(0.10, 1,  400, 1),
    "_default":                HostRate(0.20, 1,  200, 1),
}

# Hostname suffix -> rate key. Longest suffix wins, so a more specific entry
# such as api.govinfo.gov beats a broader one.
_HOST_TO_KEY: dict[str, str] = {
    "sec.gov": "sec.gov",
    "efts.sec.gov": "sec.gov",
    "data.sec.gov": "sec.gov",
    "www.sec.gov": "sec.gov",
    "justice.gov": "justice.gov",
    "www.justice.gov": "justice.gov",
    "ftc.gov": "ftc.gov",
    "www.ftc.gov": "ftc.gov",
    "uscourts.gov": "uscourts.gov",
    "api.govinfo.gov": "api.govinfo.gov",
    "govinfo.gov": "api.govinfo.gov",
    "courtlistener.com": "courtlistener.com",
    "www.courtlistener.com": "courtlistener.com",
    "securities.stanford.edu": "securities.stanford.edu",
    "courts.delaware.gov": "courts.delaware.gov",
    "courtconnect.courts.delaware.gov": "courts.delaware.gov",
    # All claims-agent vendors share ONE budget, deliberately.
    "dm.epiq11.com": "claims-agent",
    "document.epiq11.com": "claims-agent",
    "cases.stretto.com": "claims-agent",
    "veritaglobal.net": "claims-agent",
    "omniagentsolutions.com": "claims-agent",
    "bankruptcy.angeiongroup.com": "claims-agent",
    "bmcgroup.com": "claims-agent",
    "cases.ra.kroll.com": "claims-agent",
}


def rate_key_for(url: str) -> str:
    """Map a URL to its rate-limit key (NOT necessarily its hostname)."""
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return "_default"
    if host in _HOST_TO_KEY:
        return _HOST_TO_KEY[host]
    # Longest matching suffix wins.
    best: str | None = None
    for suffix, key in _HOST_TO_KEY.items():
        if host == suffix or host.endswith("." + suffix):
            if best is None or len(suffix) > len(best):
                best = suffix
    if best is not None:
        return _HOST_TO_KEY[best]
    return "_default"


def rate_for(key: str) -> HostRate:
    return RATES.get(key, RATES["_default"])


class TokenBucket:
    """Classic token bucket. Blocking acquire, thread-safe."""

    __slots__ = ("_rps", "_burst", "_tokens", "_last", "_lock")

    def __init__(self, rps: float, burst: int) -> None:
        self._rps = float(rps)
        self._burst = float(max(1, burst))
        self._tokens = float(max(1, burst))
        self._last = time.monotonic()
        self._lock = threading.Lock()

    @property
    def rps(self) -> float:
        return self._rps

    def throttle(self, factor: float = 0.5) -> float:
        """Permanently reduce this bucket's rate for the rest of the run.

        Called when a host answers 429/503. A host that says "slow down" once
        will say it again; permanently reducing the rate is more polite than
        retrying at the original rate and rediscovering the limit.
        """
        with self._lock:
            self._rps = max(0.01, self._rps * factor)
            return self._rps

    def acquire(self, timeout: float = 300.0) -> float:
        """Block until a token is available. Returns seconds waited."""
        deadline = time.monotonic() + timeout
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self._burst, self._tokens + elapsed * self._rps)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return waited
                deficit = 1.0 - self._tokens
                sleep_for = deficit / self._rps if self._rps > 0 else timeout

            if time.monotonic() + sleep_for > deadline:
                raise TimeoutError(
                    f"Rate limit wait exceeded {timeout}s (rps={self._rps})"
                )
            sleep_for = min(sleep_for, 5.0)
            time.sleep(sleep_for)
            waited += sleep_for


def set_courtlistener_member(tier_rps: float = 0.14, hourly: int = 70,
                             daily: int = 280) -> None:
    """Raise CourtListener limits once an API token is configured.

    Defaults correspond to the $10/mo tier (10/min, 75/hour, 300/day), held a
    little under the published ceiling for headroom. Called from the client
    when cfg.courtlistener_token is set.
    """
    RATES["courtlistener.com"] = HostRate(
        tier_rps, 2, daily, 1, hourly_cap=hourly
    )


class HostGovernor:
    """One bucket + concurrency semaphore + hour/day counters per rate key."""

    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._sems: dict[str, threading.Semaphore] = {}
        self._counts: dict[str, int] = {}
        self._hourly: dict[str, tuple[int, int]] = {}  # key -> (hour_epoch, n)
        self._lock = threading.Lock()

    def _ensure(self, key: str) -> tuple[TokenBucket, threading.Semaphore]:
        with self._lock:
            if key not in self._buckets:
                rate = rate_for(key)
                self._buckets[key] = TokenBucket(rate.rps, rate.burst)
                self._sems[key] = threading.Semaphore(max(1, rate.concurrency))
                self._counts.setdefault(key, 0)
            return self._buckets[key], self._sems[key]

    def bucket(self, key: str) -> TokenBucket:
        return self._ensure(key)[0]

    def count(self, key: str) -> int:
        with self._lock:
            return self._counts.get(key, 0)

    def acquire(self, url: str, *, timeout: float = 300.0) -> str:
        """Block until this URL may be fetched. Returns the rate key used."""
        key = rate_key_for(url)
        bucket, sem = self._ensure(key)

        rate = rate_for(key)
        if rate.daily_cap is not None and self.count(key) >= rate.daily_cap:
            raise HostDailyCapExceeded(key, rate.daily_cap)

        if rate.hourly_cap is not None:
            hour = int(time.time() // 3600)
            with self._lock:
                bucket_hour, n = self._hourly.get(key, (hour, 0))
                if bucket_hour != hour:
                    bucket_hour, n = hour, 0
                    self._hourly[key] = (hour, 0)
                if n >= rate.hourly_cap:
                    raise HostHourlyCapExceeded(key, rate.hourly_cap)

        sem.acquire()
        try:
            bucket.acquire(timeout=timeout)
        except BaseException:
            sem.release()
            raise
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1
            if rate.hourly_cap is not None:
                hour = int(time.time() // 3600)
                bucket_hour, n = self._hourly.get(key, (hour, 0))
                self._hourly[key] = (hour, (n + 1) if bucket_hour == hour else 1)
        return key

    def release(self, key: str) -> None:
        _, sem = self._ensure(key)
        sem.release()

    def throttle(self, key: str, factor: float = 0.5) -> float:
        return self.bucket(key).throttle(factor)


class HostDailyCapExceeded(RuntimeError):
    def __init__(self, key: str, cap: int) -> None:
        super().__init__(
            f"Per-host daily request cap reached for rate key {key!r} "
            f"({cap} requests). Remaining work for this host is deferred to "
            f"the next run rather than pushed through."
        )
        self.key = key
        self.cap = cap


class HostHourlyCapExceeded(RuntimeError):
    def __init__(self, key: str, cap: int) -> None:
        super().__init__(
            f"Per-host HOURLY request cap reached for rate key {key!r} "
            f"({cap} requests/hour). A per-second rate alone cannot honor a "
            f"published hourly quota -- this is the guard that does. Work is "
            f"deferred to the next hour."
        )
        self.key = key
        self.cap = cap
