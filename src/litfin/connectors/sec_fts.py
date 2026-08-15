"""SEC EDGAR full-text search.

Probed live 2026-08-15. Confirmed behavior:

  * Endpoint: https://efts.sec.gov/LATEST/search-index
    Params: q, forms, startdt, enddt. Returns Elasticsearch-shaped JSON.
  * `hits.total` is `{"value": N, "relation": "eq"}` for small result sets and
    `{"value": 10000, "relation": "gte"}` once saturated. A 19-month unfiltered
    range saturates; a single day with a form filter does not.
  * A page returns at most 100 hits.
  * `_id` is "<accession>:<filename>" -- unique and stable, so it is the
    natural key.
  * `_source` carries: adsh, ciks, display_names, file_date, form, root_forms,
    items (the 8-K item numbers!), file_type, file_description, period_ending,
    sics, biz_locations, biz_states, inc_states, file_num, film_num, sequence.

THE CAP IS WHY WE SLICE BY DAY. Deep pagination is capped, so any query whose
result set could exceed 10,000 silently truncates with no error. Slicing to a
single day per request keeps every slice far below the ceiling and makes
truncation structurally impossible rather than merely unlikely.

This endpoint is UNDOCUMENTED. SEC reserves the right to change it without
notice, which is why its canary is the strictest in the system: an anchor
query over a FIXED historical window must return a known accession. That
validates query semantics, not just reachability -- a changed filter parameter
would still return HTTP 200 with well-formed JSON while silently narrowing
results to nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Sequence
from urllib.parse import urlencode

from ..canary.framework import CanaryFailure, ContentExpectation
from ..store.db import Item
from .base import FetchTask, ParseResult

ENDPOINT = "https://efts.sec.gov/LATEST/search-index"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data"

# Phrase queries chosen to hit the three deal theses. Counts shown are what a
# two-week 8-K window returned when probed, as a sanity baseline.
QUERIES: tuple[str, ...] = (
    '"final judgment"',              # ~236 / 2wk -- judgment monetization
    '"settlement agreement"',        # ~115 / 2wk -- post-settlement
    '"memorandum of understanding"',  # ~55  / 2wk
    '"loss contingency"',            # ~13  / 2wk
    '"stipulation of settlement"',   # ~4   / 2wk
    '"class action settlement"',     # ~2   / 2wk
    '"consent judgment"',            # ~1   / 2wk
    '"jury verdict"',                # ~1   / 2wk
)

# 8-K items that actually carry litigation outcomes.
#   1.01 Entry into a Material Definitive Agreement -> settlement agreements
#   8.01 Other Events -> the most common home for litigation outcomes
#   2.05/2.06 exit costs / material impairments -> charges taken for settlements
RELEVANT_8K_ITEMS = {"1.01", "8.01", "2.05", "2.06"}

# Forms are queried SEPARATELY, not as a combined "8-K,10-Q,10-K" filter.
#
# MEASURED 2026-08-15: a page returns at most 100 hits, and a combined-form
# query for '"settlement agreement"' on 2026-08-14 reported total=104 while
# returning 100 -- four hits silently lost, with a perfectly valid 200.
# Splitting the same query by form gave 13 (8-K) + 89 (10-Q) + 2 (10-K) = 104,
# every slice comfortably under the cap.
#
# This is the same class of failure as the 10,000-result ceiling, one level
# down, and the same remedy applies: make truncation structurally impossible
# rather than merely unlikely. `_assert_not_truncated` is the backstop for the
# residual case (10-Q season can spike a single form past 100).
FORM_SLICES: tuple[str, ...] = ("8-K", "10-Q", "10-K")
FORMS = ",".join(FORM_SLICES)  # anchor only; daily slices go per-form


@dataclass(frozen=True, slots=True)
class FtsSlice:
    query: str
    day: date


class SecFtsConnector:
    """One task per (query, day). Small, cheap to redo, impossible to truncate."""

    source_id = "sec_fts"
    schedule = "daily"

    def __init__(self, lookback_days: int = 2, queries: tuple[str, ...] = QUERIES) -> None:
        # Default 2 days rather than 1: EDGAR back-fills late filings, and a
        # one-day window would miss anything indexed after our run.
        self.lookback_days = max(1, lookback_days)
        self.queries = queries

    # The anchor: a FIXED historical window that is known to contain results.
    # Because legitimately-empty slices are now HEALTHY (weekends return
    # zero), an empty daily slice can no longer signal breakage -- so this is
    # what actually guards the connector. It validates query SEMANTICS on an
    # undocumented endpoint: if SEC renames a parameter, the daily slices
    # would return well-formed zero-result JSON forever and look fine, while
    # this task fails loudly.
    #
    # Verified 2026-08-15: this window returns 54 hits.
    # Deliberately an OLD date, well outside any plausible lookback window, so
    # the anchor task can never be confused with an ordinary daily slice.
    # Verified 2026-08-15: 32 hits (8-K only).
    ANCHOR_QUERY = '"final judgment"'
    ANCHOR_FORM = "8-K"
    ANCHOR_START = "2026-06-15"
    ANCHOR_END = "2026-06-15"
    ANCHOR_MIN_HITS = 1
    ANCHOR_TASK_KEY = "sec_fts:anchor"

    def plan(self, watermark: str | None) -> Sequence[FetchTask]:
        today = datetime.now(timezone.utc).date()
        tasks: list[FetchTask] = []

        anchor_qs = urlencode({
            "q": self.ANCHOR_QUERY, "forms": self.ANCHOR_FORM,
            "startdt": self.ANCHOR_START, "enddt": self.ANCHOR_END,
        })
        tasks.append(
            FetchTask(
                task_key=self.ANCHOR_TASK_KEY,
                url=f"{ENDPOINT}?{anchor_qs}",
                accept="application/json",
                note="ANCHOR: fixed window, must always return hits",
            )
        )

        for offset in range(self.lookback_days):
            day = today - timedelta(days=offset)
            for q in self.queries:
                for form in FORM_SLICES:
                    qs = urlencode({
                        "q": q, "forms": form,
                        "startdt": day.isoformat(), "enddt": day.isoformat(),
                    })
                    tasks.append(
                        FetchTask(
                            task_key=f"{self.source_id}:{day.isoformat()}:{form}:{q}",
                            url=f"{ENDPOINT}?{qs}",
                            accept="application/json",
                            note=(
                                f"sliced by day AND form to stay under both the "
                                f"10k ceiling and the 100-per-page cap: "
                                f"{q} / {form} on {day}"
                            ),
                        )
                    )
        return tasks

    def expectation(self, task: FetchTask) -> ContentExpectation:
        return ContentExpectation(
            content_types=("application/json",),
            min_bytes=20,
            must_contain=('"hits"',),
        )

    def parse(self, raw: bytes, url: str) -> ParseResult:
        """PURE. Returns every hit in the slice; the runner filters."""
        if not raw.strip():
            return ParseResult()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return ParseResult()

        hits_root = data.get("hits") or {}
        total = hits_root.get("total") or {}
        hits = hits_root.get("hits") or []

        items: list[Item] = []
        for idx, hit in enumerate(hits):
            hid = hit.get("_id")
            src = hit.get("_source") or {}
            if not hid:
                continue

            form = src.get("form") or ""
            items_list = [str(i) for i in (src.get("items") or [])]
            # 8-K items are the cheapest available relevance filter, but only
            # 8-Ks carry them -- 10-Q/10-K legal-proceedings disclosures have
            # no item codes and must pass through to the LLM stage.
            relevant = (
                not form.startswith("8-K")
                or not items_list
                or bool(set(items_list) & RELEVANT_8K_ITEMS)
            )

            names = src.get("display_names") or []
            company = names[0] if names else ""
            ciks = src.get("ciks") or []
            file_date = src.get("file_date")

            items.append(
                Item(
                    source_id=self.source_id,
                    natural_key=hid,
                    title=f"{company} — {form} {src.get('file_type') or ''}".strip(),
                    body=(
                        f"{company} filed {form} on {file_date}. "
                        f"Document {src.get('file_description') or src.get('file_type') or ''}. "
                        f"8-K items: {', '.join(items_list) or 'n/a'}."
                    ),
                    source_url=_document_url(src, hid),
                    published_at=_iso(file_date),
                    extract_locator=f"hits.hits[{idx}]",
                    payload={
                        "accession": src.get("adsh"),
                        "ciks": ciks,
                        "company": company,
                        "form": form,
                        "root_forms": src.get("root_forms") or [],
                        "items_8k": items_list,
                        "file_type": src.get("file_type"),
                        "period_ending": src.get("period_ending"),
                        "sics": src.get("sics") or [],
                        "biz_locations": src.get("biz_locations") or [],
                        "relevant_item": relevant,
                        "matched_query": _query_from_url(url),
                        "slice_total": total.get("value"),
                        "slice_relation": total.get("relation"),
                    },
                )
            )

        note = f"{len(items)} hits (slice total {total.get('value')} {total.get('relation')})"
        # An explicit zero count from the server is positive evidence of no
        # news (a Saturday, say), not a failed parse. See ParseResult.
        reported_empty = (
            not items
            and total.get("relation") == "eq"
            and int(total.get("value") or 0) == 0
        )
        return ParseResult(
            items=items, note=note, server_reported_empty=reported_empty
        )

    def canary(self, raw: bytes, task: FetchTask) -> None:
        """Fail loudly if a slice reports saturation.

        `relation == "gte"` means the result set hit the 10,000 ceiling and is
        silently truncated. Day-slicing should make that impossible, so if it
        ever fires, the slicing logic has regressed -- and the consequence is
        missing data with no error, which is exactly what this system exists
        to prevent.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        hits_root = data.get("hits") or {}
        total = hits_root.get("total") or {}
        returned = len(hits_root.get("hits") or [])

        if total.get("relation") == "gte":
            raise CanaryFailure(
                self.source_id,
                f"slice saturated at {total.get('value')} results "
                f"(relation=gte) -- results are silently TRUNCATED. "
                f"Narrow the slice. URL: {task.url}",
            )

        # Page-level truncation: the server reported more results than it
        # returned. Slicing by day AND form should prevent this, so if it
        # fires the slicing has stopped being fine-grained enough -- and the
        # consequence is silently missing filings behind a valid 200.
        value = int(total.get("value") or 0)
        if value > returned:
            raise CanaryFailure(
                self.source_id,
                f"page truncation: server reported {value} results but "
                f"returned {returned} (100 is the per-page cap). "
                f"{value - returned} filings would be silently dropped. "
                f"Narrow the slice further. URL: {task.url}",
            )

        # The anchor must always produce hits. If it does not, the endpoint's
        # query semantics have changed and every daily slice is now silently
        # returning nothing while looking perfectly healthy.
        #
        # Keyed on task_key, NOT the URL. An earlier version sniffed the URL
        # for the anchor's date, which also matched every ordinary daily slice
        # filed on that date and failed all of them -- rare phrases legitimately
        # return zero for some form/day combinations.
        if task.task_key == self.ANCHOR_TASK_KEY:
            if value < self.ANCHOR_MIN_HITS:
                raise CanaryFailure(
                    self.source_id,
                    f"ANCHOR query returned {value} hits over the fixed "
                    f"window {self.ANCHOR_START}..{self.ANCHOR_END}, expected "
                    f">= {self.ANCHOR_MIN_HITS}. This window is known to "
                    f"contain results, so the endpoint's query semantics have "
                    f"changed -- daily slices are now silently returning "
                    f"nothing while appearing healthy. URL: {task.url}",
                )

    def watermark_for(self, items: Sequence[Item]) -> str | None:
        stamps = [i.published_at for i in items if i.published_at]
        return max(stamps) if stamps else None


def _document_url(src: dict, hid: str) -> str:
    """Build the canonical Archives URL for a hit.

    _id is "<accession>:<filename>"; the archive path wants the accession with
    dashes stripped and an un-zero-padded CIK.
    """
    adsh = src.get("adsh") or (hid.split(":", 1)[0] if ":" in hid else "")
    filename = hid.split(":", 1)[1] if ":" in hid else ""
    ciks = src.get("ciks") or []
    if not adsh or not ciks:
        return ""
    cik = str(ciks[0]).lstrip("0") or str(ciks[0])
    return f"{ARCHIVE}/{cik}/{adsh.replace('-', '')}/{filename}"


def _iso(day: str | None) -> str | None:
    if not day:
        return None
    try:
        return datetime.fromisoformat(day).replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def _query_from_url(url: str) -> str:
    from urllib.parse import parse_qs, urlsplit

    return (parse_qs(urlsplit(url).query).get("q") or [""])[0]


def build(lookback_days: int = 2) -> SecFtsConnector:
    return SecFtsConnector(lookback_days=lookback_days)
