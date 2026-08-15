"""DOJ Antitrust Division case filings -- the Tunney Act goldmine.

Why this source matters more than its size suggests: the Tunney Act (15 U.S.C.
s.16(b)-(h)) requires that civil antitrust settlements be filed publicly as a
*proposed* final judgment with a competitive impact statement and a 60-day
comment period BEFORE the court may enter it. So DOJ civil antitrust
settlements are structurally disclosed weeks ahead of entry -- the earliest
free settlement signal available anywhere.

Page structure (probed live 2026-08-15):
  * https://www.justice.gov/atr/antitrust-case-filings is a Drupal view.
    There are NO <table> elements -- an earlier assumption that it was a
    table was wrong.
  * 12 `div.views-row` per page, 206 pages (?page=0 .. ?page=205), newest
    first.
  * Each row carries `.case-title` (link to /atr/case/<slug>), `.node-date`
    ("Case Open Date: August 7, 2026"), and `.node-documents` -- a list of
    document-type labels plus links to /atr/case-document/<slug>.

Granularity decision: we emit ONE ITEM PER DOCUMENT, not per case. Documents
accumulate on a case over its life, so a case-level key would go stale the
moment a proposed final judgment was entered. A document href
(/atr/case-document/proposed-final-judgment-304) is unique and stable, so each
new filing surfaces as its own event -- which is exactly the granularity the
deal theses need.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from lxml import html as LH

from ..canary.framework import ContentExpectation
from ..store.db import Item
from .base import FetchTask, ParseResult

BASE = "https://www.justice.gov"
INDEX = f"{BASE}/atr/antitrust-case-filings"

# Document-type labels that carry deal signal, mapped to a coarse event class.
# Matched case-insensitively as substrings of the anchor text.
DOC_SIGNALS: dict[str, str] = {
    "proposed final judgment": "settlement_proposed",
    "final judgment": "judgment_entered",
    "competitive impact statement": "settlement_proposed",
    "consent decree": "settlement_proposed",
    "stipulation": "settlement_proposed",
    "asset preservation": "settlement_proposed",
    "hold separate": "settlement_proposed",
    "complaint": "case_filed",
    "response to public comment": "settlement_proposed",
}

_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2}),\s+(\d{4})",
    re.IGNORECASE,
)
_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], start=1
    )
}


@dataclass(slots=True)
class DojCaseRow:
    case_name: str
    case_url: str
    open_date: str | None
    documents: list[tuple[str, str]]   # (label, href)


class DojCaseFilingsConnector:
    """Polls the first N pages of the case-filings index.

    Only the first page or two matter for a daily monitor -- the view is
    newest-first. Deeper pages exist (206 of them) and are reachable by
    raising `pages`, but at justice.gov's Crawl-delay of 10s that is a
    backfill operation, not a daily one.
    """

    source_id = "doj_atr_case_filings"
    schedule = "daily"

    def __init__(self, pages: int = 1) -> None:
        # MEASURED 2026-08-15: page 0 returns 200, but `?page=1` returns 403
        # to an identified client -- justice.gov refuses paginated crawling of
        # this view. So the default is page 0 only, which is the right shape
        # for a daily monitor anyway (the view is newest-first, 12 cases and
        # ~59 documents per page). Raising `pages` will produce DEGRADED tasks,
        # not data.
        self.pages = max(1, pages)

    def plan(self, watermark: str | None) -> Sequence[FetchTask]:
        return [
            FetchTask(
                task_key=f"{self.source_id}:page:{p}",
                url=INDEX if p == 0 else f"{INDEX}?page={p}",
                accept="text/html",
                note="Tunney Act proposed/entered final judgments",
            )
            for p in range(self.pages)
        ]

    def expectation(self, task: FetchTask) -> ContentExpectation:
        return ContentExpectation(
            content_types=("text/html",),
            # The live page is ~327 KB. A short body here is an error page or a
            # redirect, not an empty result set.
            min_bytes=50_000,
            must_contain=("views-row",),
        )

    def parse(self, raw: bytes, url: str) -> ParseResult:
        """PURE. One item per (case, document) pair."""
        if not raw.strip():
            return ParseResult()
        try:
            doc = LH.fromstring(raw)
        except Exception:
            return ParseResult()

        items: list[Item] = []
        for r_idx, row in enumerate(
            doc.xpath('//div[contains(@class,"views-row")]')
        ):
            parsed_row = _parse_row(row)
            if parsed_row is None:
                continue
            for d_idx, (label, href) in enumerate(parsed_row.documents):
                event = _classify_document(label)
                items.append(
                    Item(
                        source_id=self.source_id,
                        natural_key=href,          # stable and unique per doc
                        title=f"{parsed_row.case_name} — {label}",
                        body=(
                            f"{parsed_row.case_name}. Document: {label}. "
                            f"Case open date: {parsed_row.open_date or 'unknown'}."
                        ),
                        source_url=_abs(href),
                        published_at=parsed_row.open_date,
                        extract_locator=f"views-row[{r_idx}]/document[{d_idx}]",
                        payload={
                            "case_name": parsed_row.case_name,
                            "case_url": _abs(parsed_row.case_url),
                            "document_label": label,
                            "event_class": event,
                            "case_open_date": parsed_row.open_date,
                            "practice_area_hint": "antitrust",
                            "index_url": url,
                        },
                    )
                )
        return ParseResult(items=items, note=f"{len(items)} case documents")

    def watermark_for(self, items: Sequence[Item]) -> str | None:
        stamps = [i.published_at for i in items if i.published_at]
        return max(stamps) if stamps else None


def _parse_row(row) -> DojCaseRow | None:
    title_links = row.xpath('.//*[contains(@class,"case-title")]//a[@href]')
    if not title_links:
        return None
    case_name = " ".join((title_links[0].text_content() or "").split())
    case_url = title_links[0].get("href") or ""
    if not case_name:
        return None

    open_date = None
    for el in row.xpath('.//*[contains(@class,"node-date")]'):
        open_date = _parse_date(el.text_content() or "")
        if open_date:
            break

    documents: list[tuple[str, str]] = []
    for el in row.xpath('.//*[contains(@class,"node-documents")]'):
        for a in el.xpath('.//a[@href]'):
            href = a.get("href") or ""
            label = " ".join((a.text_content() or "").split())
            if href and label:
                documents.append((label, href))

    return DojCaseRow(case_name, case_url, open_date, documents)


def _classify_document(label: str) -> str:
    """Map a document label to a coarse event class.

    Order matters: 'proposed final judgment' must be checked before
    'final judgment', or every proposed judgment is misread as an entered one.
    That distinction is the difference between a settlement that is still in
    its Tunney Act comment window and one the court has actually entered.
    """
    low = label.lower()
    if "proposed final judgment" in low:
        return "settlement_proposed"
    for needle, event in DOC_SIGNALS.items():
        if needle in low:
            return event
    return "other"


def _parse_date(text: str) -> str | None:
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    month = _MONTHS.get(m.group(1).lower())
    if not month:
        return None
    try:
        dt = datetime(int(m.group(3)), month, int(m.group(2)), tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.isoformat()


def _abs(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return BASE + href


def build() -> DojCaseFilingsConnector:
    return DojCaseFilingsConnector()
