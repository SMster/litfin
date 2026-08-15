"""CourtListener / RECAP -- federal docket ENTRY TEXT.

This is the only free source that searches the text of individual docket
lines, which is the thing PACER's own Case Locator API cannot do. It is
therefore the highest-value connector in the project.

Probed live 2026-08-15, unauthenticated:

  * /api/rest/v4/courts/ carries pacer_has_rss_feed and
    pacer_rss_entry_types. That lives in coverage.py, not here -- it is a
    weekly metadata bootstrap needing a cursor walk, not event ingestion.
  * /api/rest/v4/search/?type=rd&q=description:("notice of settlement")
    -> 253,255 results all-time; adding entry_date_filed_after=2026-08-01
    collapses it to 49. The date window is what makes this usable.
  * A type=rd result carries: description (the FULL docket entry text),
    entry_date_filed, entry_number, document_number, docket_id,
    docket_entry_id, pacer_doc_id, absolute_url, short_description,
    is_available, and meta.date_created.

TWO TRAPS, both designed around here:

1. meta.date_created is CourtListener's INGESTION time, not the court's.
   Content can arrive weeks or months after it was filed. Every event date in
   this module comes from `entry_date_filed`; date_created is used only as a
   changefeed watermark, never as "when this happened".

2. nature_of_suit is dirty free text -- the model field is CharField(1000) for
   a nominally 3-digit code, and real values include "410", "410 Antitrust",
   and "410 Anti-Trust". Match with startswith, never equality. Entries that
   reached CourtListener via RSS carry no NOS at all, so null rates are high
   and NOS must never be a hard filter.

RATE LIMITS make polling structurally unviable: 5/min, 50/hour, 125/day
anonymous; 10/min, 75/hour, 300/day on the $10 membership. The intended
production path is webhooks, which do not consume read quota. This connector
is the DISCOVERY half -- it finds cases worth watching. `alerts.py` then
subscribes to them so updates arrive by push.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence
from urllib.parse import urlencode

from ..canary.framework import CanaryFailure, ContentExpectation
from ..store.db import Item
from .base import FetchTask, ParseResult

BASE = "https://www.courtlistener.com"
API = f"{BASE}/api/rest/v4"

# Phrase queries against the docket entry text itself. These mirror
# score/taxonomy.py but are expressed in CourtListener's query syntax.
#
# Kept deliberately short: each query costs a request, and the anonymous
# ceiling is 50/hour. Breadth comes from the date window, not from query count.
DESCRIPTION_QUERIES: tuple[tuple[str, str], ...] = (
    ("judgment_entered", 'description:("entry of judgment" OR "judgment entered")'),
    ("final_judgment", 'description:("final judgment")'),
    ("verdict", 'description:("jury verdict" OR "verdict was returned")'),
    ("notice_of_appeal", 'description:("notice of appeal")'),
    ("settlement_notice", 'description:("notice of settlement")'),
    ("stipulation", 'description:("stipulation of dismissal" OR "stipulation of settlement")'),
    ("class_settlement", 'description:("preliminary approval" OR "final approval")'),
    ("bankruptcy_9019", 'description:("9019" OR "compromise and settlement")'),
)


# ---------------------------------------------------------------------------
# Docket-text search
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SearchSlice:
    label: str
    query: str


class RecapSearchConnector:
    """Discovery: find docket ENTRIES whose text signals a deal event."""

    source_id = "courtlistener"
    schedule = "daily"

    # Anchor: a fixed historical window that must always return results.
    # Same reasoning as sec_fts -- an empty daily window is legitimate, so
    # something else has to guard connector health.
    # A SINGLE fixed day, not a range. A multi-day anchor would exceed the
    # 20-result page cap every run and permanently report partial coverage,
    # burying the real partial-coverage warnings in noise. One day of this
    # phrase runs ~8 hits -- enough to prove the endpoint works, small enough
    # never to truncate.
    ANCHOR_TASK_KEY = "courtlistener:search:anchor"
    ANCHOR_QUERY = 'description:("notice of settlement")'
    ANCHOR_AFTER = "2026-08-14"
    ANCHOR_BEFORE = "2026-08-14"
    ANCHOR_MIN_HITS = 1

    def __init__(self, lookback_days: int = 3,
                 queries: Sequence[tuple[str, str]] = DESCRIPTION_QUERIES) -> None:
        # 3 days by default. RECAP ingests late -- content can appear weeks
        # after filing -- so a 1-day window would miss a great deal. This is a
        # different reason from EDGAR's, where the lag is indexing rather than
        # contribution.
        self.lookback_days = max(1, lookback_days)
        self.queries = tuple(queries)

    # MEASURED 2026-08-15: the search endpoint returns at most 20 results per
    # page and IGNORES page_size (page_size=100 still returned 20). Deep
    # pagination requires following the `next` cursor, which is sequential and
    # would break parse()'s purity -- and at a 50/hour ceiling, cursor-walking
    # every query is not affordable anyway.
    #
    # So slice per DAY, exactly as sec_fts slices by day and form. A single
    # day of one phrase runs ~4 hits against a 10-day window of 39, so every
    # slice sits comfortably under the cap and truncation becomes structurally
    # impossible rather than merely unlikely.
    PAGE_CAP = 20

    def plan(self, watermark: str | None) -> Sequence[FetchTask]:
        today = datetime.now(timezone.utc).date()

        tasks = [
            FetchTask(
                task_key=self.ANCHOR_TASK_KEY,
                url=f"{API}/search/?" + urlencode({
                    "type": "rd",
                    "q": self.ANCHOR_QUERY,
                    "entry_date_filed_after": self.ANCHOR_AFTER,
                    "entry_date_filed_before": self.ANCHOR_BEFORE,
                }),
                accept="application/json",
                note="ANCHOR: fixed window, must always return hits",
            )
        ]

        for offset in range(self.lookback_days):
            day = (today - timedelta(days=offset)).isoformat()
            for label, query in self.queries:
                tasks.append(
                    FetchTask(
                        task_key=f"courtlistener:search:{day}:{label}",
                        url=f"{API}/search/?" + urlencode({
                            "type": "rd",
                            "q": query,
                            # Inclusive on both ends -> exactly one day.
                            "entry_date_filed_after": day,
                            "entry_date_filed_before": day,
                            "order_by": "entry_date_filed desc",
                        }),
                        accept="application/json",
                        note=f"docket-entry text search, one day: {label} on {day}",
                    )
                )
        return tasks

    def expectation(self, task: FetchTask) -> ContentExpectation:
        return ContentExpectation(
            content_types=("application/json",),
            min_bytes=20,
            must_contain=('"results"',),
        )

    def parse(self, raw: bytes, url: str) -> ParseResult:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return ParseResult()

        results = data.get("results") or []
        count = data.get("count")
        items: list[Item] = []

        for idx, hit in enumerate(results):
            rd_id = hit.get("id")
            if rd_id is None:
                continue
            description = hit.get("description") or ""
            docket_id = hit.get("docket_id")
            abs_url = hit.get("absolute_url") or ""

            items.append(
                Item(
                    source_id=self.source_id,
                    natural_key=f"recap-doc:{rd_id}",
                    title=_case_name_from_url(abs_url) or f"RECAP document {rd_id}",
                    body=description,
                    source_url=f"{BASE}{abs_url}" if abs_url.startswith("/") else abs_url,
                    # entry_date_filed is the COURT's date. meta.date_created is
                    # CourtListener's ingestion time and must never be used as
                    # the event date -- it can lag by weeks or months.
                    published_at=_iso(hit.get("entry_date_filed")),
                    extract_locator=f"results[{idx}]",
                    payload={
                        "record_kind": "recap_document",
                        "recap_document_id": rd_id,
                        "docket_id": docket_id,
                        "docket_entry_id": hit.get("docket_entry_id"),
                        "entry_number": hit.get("entry_number"),
                        "document_number": hit.get("document_number"),
                        "pacer_doc_id": hit.get("pacer_doc_id"),
                        "short_description": hit.get("short_description"),
                        "is_available": hit.get("is_available"),
                        "entry_date_filed": hit.get("entry_date_filed"),
                        # Kept for changefeed/debug ONLY. Not an event date.
                        "cl_ingested_at": (hit.get("meta") or {}).get("date_created"),
                        "docket_url": (
                            f"{BASE}/docket/{docket_id}/" if docket_id else ""
                        ),
                        "query_url": url,
                    },
                )
            )

        truncated = isinstance(count, int) and count > len(items)
        coverage_note = ""
        if truncated:
            coverage_note = (
                f"PARTIAL: {count} matching entries exist, {len(items)} "
                f"retrieved ({self.PAGE_CAP}/page hard cap, page_size "
                f"ignored). Sorted newest-first, so the most recent were "
                f"kept. {count - len(items)} older entries in this slice were "
                f"not retrieved."
            )

        return ParseResult(
            items=items,
            note=f"{len(items)} of {count} matching docket entries",
            server_reported_empty=(not results and count == 0),
            partial_coverage=truncated,
            coverage_note=coverage_note,
        )

    def canary(self, raw: bytes, task: FetchTask) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        # NOTE: page truncation is deliberately NOT raised here.
        #
        # A busy court-day can exceed the 20-result page cap even after
        # per-day slicing, and with page_size ignored and a 50/hour ceiling,
        # cursor-walking every query is not affordable. Raising BROKEN would
        # discard the 20 good entries we did retrieve and freeze the watermark
        # permanently, since the condition recurs every run.
        #
        # Instead parse() flags partial_coverage, which stores the rows, marks
        # the slice as incomplete, and surfaces a run warning. See
        # ParseResult.partial_coverage.

        if task.task_key == self.ANCHOR_TASK_KEY:
            count = int(data.get("count") or 0)
            if count < self.ANCHOR_MIN_HITS:
                raise CanaryFailure(
                    self.source_id,
                    f"ANCHOR returned {count} hits for the fixed window "
                    f"{self.ANCHOR_AFTER}..{self.ANCHOR_BEFORE}, expected "
                    f">= {self.ANCHOR_MIN_HITS}. This window is known to "
                    f"contain results, so either the query syntax changed or "
                    f"the search index is not returning docket text -- daily "
                    f"searches would then return nothing while looking "
                    f"perfectly healthy.",
                )

    def watermark_for(self, items: Sequence[Item]) -> str | None:
        stamps = [i.published_at for i in items if i.published_at]
        return max(stamps) if stamps else None


def _case_name_from_url(abs_url: str) -> str:
    """Derive a readable case name from /docket/<id>/<n>/<slug>/.

    type=rd results carry no caseName field, and fetching the docket for each
    hit would cost one request apiece against a 50/hour ceiling. The slug is
    good enough for triage and free.
    """
    parts = [p for p in (abs_url or "").split("/") if p]
    if not parts:
        return ""
    slug = parts[-1]
    if slug.isdigit() or len(slug) < 3:
        return ""
    return slug.replace("-", " ").title()


def _iso(day: str | None) -> str | None:
    if not day:
        return None
    try:
        return datetime.fromisoformat(day).replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None




def build_search(lookback_days: int = 3) -> RecapSearchConnector:
    return RecapSearchConnector(lookback_days=lookback_days)
