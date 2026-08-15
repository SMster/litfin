"""EDGAR daily form index -- the complete-coverage path.

WHY THIS EXISTS ALONGSIDE sec_fts: full-text search saturates. Its ceiling is
10,000 results with `relation: "gte"`, and its page cap is 100. Day-and-form
slicing keeps us under both, but FTS can only ever find filings whose TEXT
matches a phrase we thought to search for. If a settlement is disclosed in
wording none of our eight phrases cover, FTS never sees it.

The daily index has no such gap: it is the authoritative list of EVERY filing
disseminated on a given day, by form type. Measured 2026-08-15:
form.20260814.idx is 2.1 MB, 11,151 rows, 283 of them 8-K.

The trade-off is the mirror image of FTS. The index has no document text at
all -- only form type, company, CIK, date, and path. So it cannot detect a
settlement by itself. Its job is COVERAGE ACCOUNTING: it tells us the full
denominator of 8-K/10-Q/10-K filings for a day, which makes it possible to
state honestly what fraction of them the phrase searches actually examined.

FORMAT, and why this does not use fixed-width offsets:

The header is WRAPPED ACROSS TWO PHYSICAL LINES --

    'Form Type   Company Name                                    CIK'
    '      Date Filed  File Name'

-- so column positions read from "the header row" only ever capture three of
the five fields, and the offsets on the second line are not comparable to the
first. An earlier version derived spans from the header and silently produced
zero rows.

Data rows have a far more reliable shape than the header does: form type,
company, a numeric CIK, an 8-digit date, and a path beginning "edgar/". Those
are matched directly, which is immune to both the wrapped header and the
column-width drift EDGAR has introduced over the years.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Sequence

from ..canary.framework import CanaryFailure, ContentExpectation
from ..store.db import Item
from .base import FetchTask, ParseResult

BASE = "https://www.sec.gov/Archives/edgar/daily-index"

# Only the forms that carry litigation outcomes. The index lists ~11k rows/day
# across every form type; these three are ~5% of that.
FORMS_OF_INTEREST: frozenset[str] = frozenset({
    "8-K", "8-K/A", "10-Q", "10-Q/A", "10-K", "10-K/A",
})

# Matches a data row by its SHAPE rather than by column offsets, which the
# wrapped header makes unreliable:
#   <form>  <company>  <numeric CIK>  <8-digit date>  edgar/<path>
# Anchored on the "edgar/" path and the 8-digit date, both of which are
# unambiguous, so header layout changes cannot silently break it.
_ROW_RE = re.compile(
    r"^(?P<form>\S[^ ]*(?: [^ ]+)*?)\s{2,}"
    r"(?P<company>.+?)\s{2,}"
    r"(?P<cik>\d{1,10})\s+"
    r"(?P<date>\d{8})\s+"
    r"(?P<path>edgar/\S+)\s*$"
)


class EdgarDailyIndexConnector:
    source_id = "sec_daily_index"
    schedule = "daily"

    def __init__(self, lookback_days: int = 2) -> None:
        self.lookback_days = max(1, lookback_days)

    def plan(self, watermark: str | None) -> Sequence[FetchTask]:
        today = datetime.now(timezone.utc).date()
        tasks: list[FetchTask] = []
        for offset in range(self.lookback_days):
            day = today - timedelta(days=offset)
            # EDGAR publishes nothing on weekends; skip rather than generate
            # tasks guaranteed to 404.
            if day.weekday() >= 5:
                continue
            qtr = (day.month - 1) // 3 + 1
            tasks.append(
                FetchTask(
                    task_key=f"{self.source_id}:{day.isoformat()}",
                    url=f"{BASE}/{day.year}/QTR{qtr}/form.{day:%Y%m%d}.idx",
                    accept="text/plain",
                    note="complete filing denominator for the day",
                )
            )
        return tasks

    def expectation(self, task: FetchTask) -> ContentExpectation:
        return ContentExpectation(
            # SEC serves .idx as text/html despite the content being plain
            # text, so do not assert a content type here.
            min_bytes=1000,
            must_contain=("Form Type",),
        )

    def parse(self, raw: bytes, url: str) -> ParseResult:
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()

        items: list[Item] = []
        total_rows = 0

        for raw_line in lines:
            m = _ROW_RE.match(raw_line)
            if not m:
                continue
            total_rows += 1
            form = m.group("form").strip()
            if form not in FORMS_OF_INTEREST:
                continue
            cik = m.group("cik")
            company = m.group("company").strip()
            filed = m.group("date")
            path = m.group("path").strip()

            items.append(
                Item(
                    source_id=self.source_id,
                    natural_key=path,          # unique per filing
                    title=f"{company} — {form}",
                    # No synthetic event language: the index carries none, and
                    # inventing some would manufacture a signal.
                    body=(
                        f"{company} filed {form} on {filed}. "
                        f"Indexed for coverage accounting; document text not "
                        f"included in the daily index."
                    ),
                    source_url=f"https://www.sec.gov/Archives/{path}",
                    published_at=_iso(filed),
                    extract_locator=path,
                    payload={
                        "record_kind": "filing_index",
                        "form": form,
                        "cik": cik,
                        "company": company,
                        "date_filed": filed,
                        "archive_path": path,
                    },
                )
            )

        return ParseResult(
            items=items,
            note=(
                f"{len(items)} filings of interest out of {total_rows} "
                f"total rows in the day's index"
            ),
        )

    def canary(self, raw: bytes, task: FetchTask) -> None:
        """A day's index that parses to zero rows is a format change.

        EDGAR disseminates thousands of filings every business day, so an
        empty parse on a 200 is never a quiet day -- it means the fixed-width
        layout moved and the column spans no longer line up.
        """
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if not _has_header(lines):
            raise CanaryFailure(
                self.source_id,
                "no 'Form Type' header found -- this is not an EDGAR form "
                f"index. Likely an error page. URL: {task.url}",
            )
        matched = sum(1 for line in lines if _ROW_RE.match(line))
        if matched < 100:
            raise CanaryFailure(
                self.source_id,
                f"only {matched} rows matched the data-row pattern out of "
                f"{len(lines)} lines; a business-day index carries thousands. "
                f"The row format has changed. URL: {task.url}",
            )

    def watermark_for(self, items: Sequence[Item]) -> str | None:
        stamps = [i.published_at for i in items if i.published_at]
        return max(stamps) if stamps else None


def _has_header(lines: list[str]) -> bool:
    return any(
        "Form Type" in line and "CIK" in line for line in lines[:60]
    )


def _iso(day: str | None) -> str | None:
    if not day:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(day, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def build(lookback_days: int = 2) -> EdgarDailyIndexConnector:
    return EdgarDailyIndexConnector(lookback_days=lookback_days)
