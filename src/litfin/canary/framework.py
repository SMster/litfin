"""The canary system.

The requirement: a broken scraper must never look like a quiet day.

The mechanism is separating two numbers most pipelines conflate:

    rows_parsed -- items the parser found, IGNORING the watermark
    rows_new    -- items remaining AFTER watermark filtering

    rows_parsed | rows_new | verdict
    ------------+----------+---------------------------------------
        0       |    0     | BROKEN   -- DOM/schema changed
       >0       |    0     | HEALTHY  -- the normal quiet day
       >0       |   >0     | HEALTHY  -- new items
        0       |    -     | HEALTHY  -- iff HTTP 304 (byte-identical)

That single distinction eliminates the classic silent-failure mode: a parser
whose selector stopped matching returns zero rows, which without this looks
exactly like "nothing happened today."

Three further layers sit on top:

  * Structural assertions -- status, content-type, and body size within the
    learned p5-p95 band. A 3.3 KB response from a normally-327 KB page is an
    error page, not an empty table.
  * Anchor records -- an immutable historical item that must always be
    present. This is the strongest guard on an undocumented endpoint because
    it validates query SEMANTICS, not just reachability: a changed filter
    parameter that silently narrows results to nothing still returns HTTP 200
    with well-formed output.
  * Staleness alarm -- compares the gap since the last non-empty run against
    that source's own learned distribution. DOJ silent for 7 days is
    suspicious; JPML silent for 7 days is Tuesday.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


class Verdict(StrEnum):
    HEALTHY = "HEALTHY"
    BROKEN = "BROKEN"      # our parser is wrong / the site changed
    DEGRADED = "DEGRADED"  # the host is down; not our fault
    STALE = "STALE"        # structurally fine but suspiciously quiet


class CanaryFailure(RuntimeError):
    """Raised by a connector's canary() when a structural assertion fails."""

    def __init__(self, source_id: str, message: str) -> None:
        super().__init__(f"[{source_id}] canary failed: {message}")
        self.source_id = source_id


@dataclass(slots=True)
class ContentExpectation:
    """Structural assertions for one source's fetched resource."""

    content_types: tuple[str, ...] = ()
    min_bytes: int = 0
    max_bytes: int = 0            # 0 = no ceiling
    must_contain: tuple[str, ...] = ()
    anchors: tuple[str, ...] = () # substrings that must ALWAYS be present

    def assert_ok(self, source_id: str, *, body: bytes, content_type: str) -> None:
        if self.content_types and content_type not in self.content_types:
            raise CanaryFailure(
                source_id,
                f"content-type {content_type!r} not in expected "
                f"{self.content_types!r} -- likely an error page or a redirect",
            )
        size = len(body)
        if self.min_bytes and size < self.min_bytes:
            raise CanaryFailure(
                source_id,
                f"body is {size} bytes, below the expected floor of "
                f"{self.min_bytes}. A short body from a normally-large page "
                f"is an error page, not an empty result set.",
            )
        if self.max_bytes and size > self.max_bytes:
            raise CanaryFailure(
                source_id, f"body is {size} bytes, above the ceiling {self.max_bytes}"
            )
        text = body.decode("utf-8", errors="replace")
        for needle in self.must_contain:
            if needle not in text:
                raise CanaryFailure(
                    source_id, f"expected marker {needle!r} not found in body"
                )
        for anchor in self.anchors:
            if anchor not in text:
                raise CanaryFailure(
                    source_id,
                    f"anchor record {anchor!r} is missing. This validates "
                    f"query semantics, not just reachability -- its absence "
                    f"usually means a filter parameter changed meaning and is "
                    f"now silently returning a narrowed result set.",
                )


@dataclass(slots=True)
class CanaryResult:
    source_id: str
    verdict: Verdict
    rows_parsed: int
    rows_new: int
    byte_size: int = 0
    note: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def is_failure(self) -> bool:
        return self.verdict in (Verdict.BROKEN, Verdict.DEGRADED)


def classify(
    source_id: str,
    *,
    rows_parsed: int,
    rows_new: int,
    not_modified: bool,
    has_body: bool = True,
    server_reported_empty: bool = False,
    byte_size: int = 0,
) -> CanaryResult:
    """Apply the core rows_parsed/rows_new decision table.

    `not_modified` alone does NOT grant HEALTHY. On a 304 we serve the cached
    body and still parse it, so a broken parser produces zero rows from a body
    that demonstrably contains items. Treating that as "nothing changed" would
    let a parser regression hide behind the cache indefinitely -- exactly the
    silent failure this whole module exists to prevent.

    A 304 only short-circuits when there is genuinely nothing to parse.
    """
    if not_modified and not has_body:
        return CanaryResult(
            source_id, Verdict.HEALTHY, rows_parsed, rows_new, byte_size,
            note="HTTP 304 with no cached body -- nothing to parse",
        )
    if rows_parsed == 0 and server_reported_empty:
        # The server affirmatively reported a zero count. That is positive
        # evidence of no news, not absence of evidence -- e.g. a date-sliced
        # EDGAR query on a Saturday. Distinct from a parser that simply found
        # nothing, which is BROKEN below.
        return CanaryResult(
            source_id, Verdict.HEALTHY, 0, 0, byte_size,
            note="server affirmatively reported zero results for this slice",
        )
    if rows_parsed == 0:
        if not_modified:
            return CanaryResult(
                source_id, Verdict.BROKEN, 0, 0, byte_size,
                note=(
                    "parser found ZERO rows in a CACHED body (HTTP 304). The "
                    "body is known-good -- it parsed before -- so this is a "
                    "parser regression that the cache would otherwise hide."
                ),
            )
        return CanaryResult(
            source_id, Verdict.BROKEN, 0, 0, byte_size,
            note=(
                "parser found ZERO rows on a 200 response. This is a broken "
                "selector or a changed schema, NOT a quiet day -- a quiet day "
                "still parses rows and filters them out by watermark."
            ),
        )
    if rows_new == 0:
        return CanaryResult(
            source_id, Verdict.HEALTHY, rows_parsed, 0, byte_size,
            note=f"parsed {rows_parsed} rows, none new since the watermark",
        )
    return CanaryResult(
        source_id, Verdict.HEALTHY, rows_parsed, rows_new, byte_size,
        note=f"parsed {rows_parsed} rows, {rows_new} new",
    )


def check_staleness(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    min_history: int = 8,
    tolerance: float = 2.0,
) -> str | None:
    """Warn if this source has been quiet longer than it usually is.

    Learned per source rather than hardcoded, because the normal gap varies by
    two orders of magnitude across sources.

    Returns a warning string, or None if the silence is within normal range.
    """
    rows = list(conn.execute(
        "SELECT observed_at, rows_new FROM source_health "
        "WHERE source_id=? ORDER BY observed_at DESC LIMIT 200",
        (source_id,),
    ))
    if len(rows) < min_history:
        return None

    productive = [r["observed_at"] for r in rows if int(r["rows_new"]) > 0]
    if len(productive) < 3:
        return None

    stamps = sorted(_parse(t) for t in productive if _parse(t) is not None)
    if len(stamps) < 3:
        return None

    gaps = [
        (stamps[i + 1] - stamps[i]).total_seconds() / 3600.0
        for i in range(len(stamps) - 1)
    ]
    if not gaps:
        return None

    typical = statistics.median(gaps)
    worst = max(gaps)
    threshold = max(worst, typical * tolerance)

    since_h = (datetime.now(timezone.utc) - stamps[-1]).total_seconds() / 3600.0
    if since_h > threshold:
        return (
            f"{source_id} has produced nothing new for {since_h:.0f}h; its "
            f"typical gap is {typical:.0f}h and its worst observed gap is "
            f"{worst:.0f}h. Structural checks passed, so this is not a broken "
            f"parser -- but it is worth a look."
        )
    return None


def _parse(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
