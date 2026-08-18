"""Runtime configuration, loaded from litfin.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .compliance.status import Purpose

_CONFIG_NAME = "litfin.toml"
_ENV_NAME = ".env"


def load_dotenv(start: Path | None = None) -> int:
    """Load KEY=VALUE pairs from a gitignored .env into os.environ.

    Deliberately stdlib -- a 20-line parser is not worth a dependency, and
    secrets should not live in litfin.toml, which is meant to be committed.

    Existing environment variables WIN. That ordering matters: a value set in
    the shell (or by CI, or by a secrets manager) must not be silently
    overridden by a stale file on disk.
    """
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        candidate = d / _ENV_NAME
        if not candidate.is_file():
            continue
        loaded = 0
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value
                loaded += 1
        return loaded
    return 0


# Addresses that mean "nobody edited this". Running with one of these in the
# User-Agent is WORSE than declaring nothing: it looks like a real contact to
# a site operator, and reaches no one. Checked by `litfin preflight`.
PLACEHOLDER_EMAILS = frozenset({
    "you@example.com", "ops@example.invalid", "user@example.com",
    "your@email.com", "changeme@example.com", "",
})


def is_placeholder_contact(email: str) -> bool:
    e = (email or "").strip().lower()
    return e in PLACEHOLDER_EMAILS or e.endswith(("@example.com", "@example.invalid"))


@dataclass(frozen=True, slots=True)
class Identity:
    app_name: str = "LitFinDocketMonitor"
    app_version: str = "0.1.0"
    # Empty by DEFAULT and deliberately so -- see user_agent() below. A URL in
    # the UA triggers a 403 from both sec.gov and ftc.gov.
    contact_url: str = ""
    contact_email: str = "ops@example.invalid"

    @property
    def contact_is_placeholder(self) -> bool:
        return is_placeholder_contact(self.contact_email)

    @property
    def user_agent(self) -> str:
        """The UA sent on every request.

        This is functional, not cosmetic: SEC and FTC 403 undeclared agents,
        and a real monitored contact address is what gives an operator
        somewhere to complain instead of silently blocking us.

        MEASURED against sec.gov and ftc.gov on 2026-08-15. Both agencies'
        WAFs 403 on UA content, and two separate triggers were isolated:

          1. An HTTP library token anywhere in the string:
             "... (+https://example.com/c; contact: x@example.com) python-httpx"  -> 403/403
          2. A URL in the string, independent of domain or Accept-Encoding:
             "LitFin/0.1.0 (+https://github.com/org/repo; contact: x@y.com)"      -> 403/403
             "LitFin/0.1.0 (contact: x@y.com)"                                    -> 200/200

        So `contact_url` is included ONLY if explicitly configured, and the
        default omits it. Name plus a contact address is exactly the shape
        SEC's published fair-access guidance asks for, and it is what works.

        If you set contact_url, re-probe both hosts before running at volume.
        """
        base = f"{self.app_name}/{self.app_version}"
        if self.contact_url:
            return f"{base} (+{self.contact_url}; contact: {self.contact_email})"
        return f"{base} (contact: {self.contact_email})"


@dataclass(frozen=True, slots=True)
class Config:
    purpose: Purpose = Purpose.RESEARCH
    identity: Identity = field(default_factory=Identity)
    data_root: Path = Path(r"C:\LitFinData")
    max_requests_per_day: int = 10_000
    warn_at_fraction: float = 0.8
    timeout_seconds: float = 30.0
    max_attempts: int = 4
    cache_ttl_hours: int = 24
    breaker_failure_threshold: int = 5
    breaker_open_seconds: int = 1800
    unverified_opt_in: frozenset[str] = frozenset()
    # CourtListener API token. Read from the COURTLISTENER_TOKEN env var in
    # preference to litfin.toml, so a credential need not sit in a config file
    # that gets committed. Sent as "Authorization: Token <value>" -- a common
    # mistake is omitting the literal word "Token".
    courtlistener_token: str = ""
    extract_model: str = "claude-opus-5"
    max_candidates_per_day: int = 400
    send_enabled: bool = False
    recipient_allowlist: tuple[str, ...] = ()
    top_n_email: int = 20
    top_n_dashboard: int = 100
    # Banner-level repeats of permanent facts (partial venue coverage, the
    # imputed-damages share). True by default so a new operator meets them;
    # turn off once they are internalized. Nothing is lost -- the coverage
    # map still renders in full, every imputed row is still marked, and the
    # dollar filter still refuses to count an imputed figure.
    show_standing_caveats: bool = True

    # NY eTrack email ingestion (Phase 6). TWO fields, both required, because
    # they record two different decisions: `etrack_enabled` is the operational
    # switch, and `etrack_decision_recorded` is somebody's name against the
    # unresolved "may not be mined" question in the UCS terms. A source that
    # is RESTRICTED rather than permitted should not be reachable by flipping
    # one boolean.
    etrack_enabled: bool = False
    etrack_decision_recorded: str = ""

    # Scoring overrides from [score.weights] and [score.event_fit]. The
    # scoring docstring promised these were tunable in litfin.toml from the
    # start and nothing ever loaded them, so every "tune the weights" run had
    # to edit Python. Empty means "use the defaults in score/scoring.py".
    score_weights: tuple[tuple[str, float], ...] = ()
    score_event_fit: tuple[tuple[str, float], ...] = ()

    # Free text, deliberately. There is no boolean that could honestly mean
    # "we asked Free Law Project and they said yes", and a boolean is exactly
    # what somebody would flip without asking. `litfin preflight` refuses a
    # hosted deployment while this is empty and a RESEARCH_ONLY source is on.
    courtlistener_scope_resolved: str = ""

    @property
    def weights(self) -> dict[str, float]:
        return dict(self.score_weights)

    @property
    def event_fit(self) -> dict[str, float]:
        return dict(self.score_event_fit)

    # Filesystem layout, all derived from data_root.
    @property
    def db_path(self) -> Path:
        return self.data_root / "litfin.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_root / "raw"

    @property
    def cache_dir(self) -> Path:
        return self.data_root / "httpcache"

    @property
    def runs_dir(self) -> Path:
        return self.data_root / "runs"

    @property
    def manifest_dir(self) -> Path:
        return self.data_root / "manifest"

    @property
    def mail_dir(self) -> Path:
        return self.data_root / "mail"

    def ensure_dirs(self) -> None:
        for p in (
            self.data_root,
            self.raw_dir,
            self.cache_dir,
            self.runs_dir,
            self.manifest_dir,
            self.mail_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)


def find_config(start: Path | None = None) -> Path | None:
    """Walk up from `start` looking for litfin.toml."""
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        candidate = d / _CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def load_config(path: Path | None = None) -> Config:
    """Load configuration, falling back to defaults when absent."""
    # Secrets come from the environment (or a gitignored .env), never from
    # litfin.toml, which is meant to be committed.
    load_dotenv()
    if path is None:
        path = find_config()
    if path is None or not path.is_file():
        return Config()

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    ident_raw = raw.get("identity", {})
    paths_raw = raw.get("paths", {})
    budget_raw = raw.get("budget", {})
    http_raw = raw.get("http", {})
    breaker_raw = raw.get("breaker", {})
    compliance_raw = raw.get("compliance", {})
    extract_raw = raw.get("extract", {})
    deliver_raw = raw.get("deliver", {})
    etrack_raw = raw.get("etrack", {})
    score_raw = raw.get("score", {})
    deployment_raw = raw.get("deployment", {})

    purpose_str = str(raw.get("purpose", "research")).strip().lower()
    try:
        purpose = Purpose(purpose_str)
    except ValueError as exc:
        valid = ", ".join(repr(str(p)) for p in Purpose)
        raise ValueError(
            f"litfin.toml declares purpose = {purpose_str!r}, which is not a "
            f"recognized value. Must be one of: {valid}. This field gates "
            f"which data sources are lawfully available -- it is not a label."
        ) from exc

    # LITFIN_DATA_ROOT wins so one committed litfin.toml can serve both a
    # Windows laptop and a Linux container without either editing the file.
    data_root = Path(
        os.environ.get("LITFIN_DATA_ROOT", "").strip()
        or paths_raw.get("data_root", r"C:\LitFinData")
    )

    return Config(
        purpose=purpose,
        identity=Identity(
            app_name=ident_raw.get("app_name", "LitFinDocketMonitor"),
            app_version=ident_raw.get("app_version", "0.1.0"),
            contact_url=ident_raw.get("contact_url", ""),
            # The env var wins, so a public repo can carry a placeholder in
            # litfin.toml while a real deployment supplies a real mailbox
            # from a gitignored .env.
            contact_email=(
                os.environ.get("LITFIN_CONTACT_EMAIL", "").strip()
                or ident_raw.get("contact_email", "ops@example.invalid")
            ),
        ),
        data_root=data_root,
        max_requests_per_day=int(budget_raw.get("max_requests_per_day", 10_000)),
        warn_at_fraction=float(budget_raw.get("warn_at_fraction", 0.8)),
        timeout_seconds=float(http_raw.get("timeout_seconds", 30.0)),
        max_attempts=int(http_raw.get("max_attempts", 4)),
        cache_ttl_hours=int(http_raw.get("cache_ttl_hours", 24)),
        breaker_failure_threshold=int(breaker_raw.get("failure_threshold", 5)),
        breaker_open_seconds=int(breaker_raw.get("open_seconds", 1800)),
        unverified_opt_in=frozenset(compliance_raw.get("unverified_opt_in", [])),
        courtlistener_token=(
            os.environ.get("COURTLISTENER_TOKEN")
            or raw.get("courtlistener", {}).get("token", "")
        ),
        extract_model=extract_raw.get("model", "claude-opus-5"),
        max_candidates_per_day=int(extract_raw.get("max_candidates_per_day", 400)),
        send_enabled=bool(deliver_raw.get("send_enabled", False)),
        # LITFIN_RECIPIENTS (comma-separated) wins over the file, for the same
        # reason the contact address does: litfin.toml is committed, and a
        # public repo should not publish anybody's mailbox. The allowlist is
        # still an allowlist -- this changes WHERE it is configured, not
        # whether it is enforced.
        recipient_allowlist=(
            tuple(
                a.strip() for a in
                os.environ.get("LITFIN_RECIPIENTS", "").split(",")
                if a.strip()
            )
            or tuple(deliver_raw.get("recipient_allowlist", []))
        ),
        top_n_email=int(deliver_raw.get("top_n_email", 20)),
        top_n_dashboard=int(deliver_raw.get("top_n_dashboard", 100)),
        show_standing_caveats=bool(
            deliver_raw.get("show_standing_caveats", True)
        ),
        etrack_enabled=bool(etrack_raw.get("enabled", False)),
        etrack_decision_recorded=str(etrack_raw.get("decision_recorded", "")),
        score_weights=tuple(
            (str(k), float(v))
            for k, v in (score_raw.get("weights", {}) or {}).items()
        ),
        score_event_fit=tuple(
            (str(k), float(v))
            for k, v in (score_raw.get("event_fit", {}) or {}).items()
        ),
        courtlistener_scope_resolved=str(
            deployment_raw.get("courtlistener_scope_resolved", "")
        ),
    )
