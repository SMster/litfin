"""Compliance status model and the fetch gate.

This module is the reason the rest of the pipeline can be trusted to only talk
to hosts it is allowed to talk to. Every outbound request in the system passes
through `assert_fetch_allowed` before a socket is opened.

Design rules that are deliberate and should not be "simplified" later:

1. PROHIBITED has no config escape hatch. There is no flag, env var, or TOML
   key that enables it. Nobody turns on a prohibited source at 2am.
2. Opt-in for UNVERIFIED sources is per-source-id. There is no global
   "enable everything" switch, because that is how an unreviewed source ends
   up live by accident.
3. Reviews expire. A ToS read in 2026 does not authorize a crawl in 2029.
4. RESEARCH_ONLY sources are bound to the declared project purpose. If the
   purpose flips to commercial, they raise rather than silently continuing
   under terms that no longer apply.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class Purpose(StrEnum):
    """Declared purpose of the project. Read from litfin.toml at startup.

    This is load-bearing, not descriptive: RESEARCH_ONLY sources check it.
    """

    RESEARCH = "research"      # personal / educational / exploratory
    COMMERCIAL = "commercial"  # revenue-generating or firm-deployed


class ToSStatus(StrEnum):
    """How we are permitted to treat a source."""

    # US Government work, not subject to copyright (17 U.S.C. s.105).
    # robots.txt still honored.
    PUBLIC_DOMAIN_GOV = "public_domain_gov"

    # A human read the terms and recorded a verbatim quote in docs/tos/.
    VERIFIED_PERMITTED = "verified_permitted"

    # Terms permit research / educational / non-commercial use only.
    # Enabled when purpose == RESEARCH; raises otherwise.
    RESEARCH_ONLY = "research_only"

    # Default for anything unreviewed. DISABLED unless explicitly opted in
    # per-source-id in litfin.toml [compliance].unverified_opt_in.
    UNVERIFIED = "unverified"

    # Permitted, but only under stated conditions recorded in review_note.
    RESTRICTED = "restricted"

    # Hardcoded off. No configuration enables this.
    PROHIBITED = "prohibited"


class ComplianceError(RuntimeError):
    """Base for every refusal to fetch. Never caught broadly by connectors."""


class CompliancePermanentlyBlocked(ComplianceError):
    def __init__(self, source_id: str, note: str | None) -> None:
        super().__init__(
            f"Source {source_id!r} is PROHIBITED and cannot be enabled by "
            f"configuration. Reason on record: {note or '(none recorded)'}"
        )
        self.source_id = source_id


class ComplianceGateBlocked(ComplianceError):
    def __init__(self, source_id: str, tos_urls: tuple[str, ...]) -> None:
        urls = "\n  ".join(tos_urls) if tos_urls else "(none recorded)"
        super().__init__(
            f"Source {source_id!r} is UNVERIFIED and disabled.\n"
            f"To enable it, read the terms at:\n  {urls}\n"
            f"then run: litfin compliance review {source_id}\n"
            f"and add {source_id!r} to [compliance].unverified_opt_in."
        )
        self.source_id = source_id
        self.tos_urls = tos_urls


class CompliancePurposeMismatch(ComplianceError):
    def __init__(self, source_id: str, purpose: Purpose, note: str | None) -> None:
        super().__init__(
            f"Source {source_id!r} is permitted for research use only, but "
            f"litfin.toml declares purpose = {str(purpose)!r}. This source is "
            f"disabled until the declared purpose is 'research' or a broader "
            f"agreement is recorded.\nTerms on record: {note or '(none)'}"
        )
        self.source_id = source_id
        self.purpose = purpose


class ComplianceReviewStale(ComplianceError):
    def __init__(self, source_id: str, expired_at: date) -> None:
        super().__init__(
            f"Source {source_id!r} last had its terms reviewed on or before "
            f"{expired_at.isoformat()}, which has expired. Re-read the terms "
            f"and run: litfin compliance review {source_id}"
        )
        self.source_id = source_id


class ComplianceUrlOutOfScope(ComplianceError):
    def __init__(self, source_id: str, url: str, patterns: tuple[str, ...]) -> None:
        super().__init__(
            f"Source {source_id!r} attempted to fetch a URL outside its "
            f"declared scope:\n  {url}\nAllowed patterns:\n  "
            + "\n  ".join(patterns)
            + "\nThis usually means a parser produced an off-site link."
        )
        self.source_id = source_id
        self.url = url


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    """The compliance record for one source. Lives in version control.

    `review_note` should carry a VERBATIM quote of the operative clause, not a
    paraphrase. When someone revisits this in a year, the quote is what lets
    them re-evaluate without re-reading the whole document.
    """

    source_id: str
    tier: str                       # "A" | "B" | "C"
    status: ToSStatus
    display_name: str = ""
    rate_key: str = "_default"
    tos_urls: tuple[str, ...] = ()
    allowed_url_patterns: tuple[str, ...] = ()
    reviewed_by: str | None = None
    reviewed_at: date | None = None
    review_note: str | None = None
    expires_at: date | None = None
    robots_ai_signal: str | None = None

    # What an unavailable robots.txt (401/403) means for THIS source.
    #
    # Default "deny": a site that will not serve its own robots.txt to an
    # identified client is not inviting that client in. That is the correct
    # reading for a content site behind a WAF -- Kroll, for instance, 403s
    # both robots.txt AND its content.
    #
    # "allow" is for the genuinely different case where a host serves no
    # robots.txt but DOES serve its content to an identified client and the
    # operator publishes a separate written policy governing automated
    # access. efts.sec.gov is exactly that: 403 on robots.txt, 200 on the API,
    # and SEC's published fair-access policy expressly permits automated
    # EDGAR access with a declared UA under a rate cap.
    #
    # Setting this to "allow" is a determination a human makes and records in
    # review_note -- never a default, and never a way to route around a site
    # that is actually refusing.
    robots_unavailable: str = "deny"  # "deny" | "allow"
    # Base trust in this source's data, used as a scoring penalty later.
    base_confidence: float = 0.8
    notes: str = ""

    def is_enabled(self, purpose: Purpose, opt_in: frozenset[str]) -> bool:
        """Cheap boolean check for reporting. Does not raise."""
        try:
            self.assert_enabled(purpose, opt_in)
        except ComplianceError:
            return False
        return True

    def assert_enabled(self, purpose: Purpose, opt_in: frozenset[str]) -> None:
        """Raise if this source may not be fetched at all right now."""
        if self.status is ToSStatus.PROHIBITED:
            raise CompliancePermanentlyBlocked(self.source_id, self.review_note)

        if self.status is ToSStatus.RESEARCH_ONLY and purpose is not Purpose.RESEARCH:
            raise CompliancePurposeMismatch(self.source_id, purpose, self.review_note)

        if self.status is ToSStatus.UNVERIFIED and self.source_id not in opt_in:
            raise ComplianceGateBlocked(self.source_id, self.tos_urls)

        if self.expires_at is not None and date.today() > self.expires_at:
            raise ComplianceReviewStale(self.source_id, self.expires_at)

    def assert_url_in_scope(self, url: str) -> None:
        """Raise if `url` is outside this source's declared surface."""
        if not self.allowed_url_patterns:
            return
        if not any(fnmatch.fnmatch(url, p) for p in self.allowed_url_patterns):
            raise ComplianceUrlOutOfScope(
                self.source_id, url, self.allowed_url_patterns
            )


def assert_fetch_allowed(
    policy: SourcePolicy,
    url: str,
    *,
    purpose: Purpose,
    opt_in: frozenset[str],
) -> None:
    """The single gate. Called by PoliteClient before every request.

    Order matters: the cheapest and most consequential check (is this source
    allowed at all?) runs before the per-URL scope check, so a PROHIBITED
    source raises without us even parsing its URL.
    """
    policy.assert_enabled(purpose, opt_in)
    policy.assert_url_in_scope(url)
