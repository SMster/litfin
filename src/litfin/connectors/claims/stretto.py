"""Phase 7 -- Stretto's public chapter 11 case index.

THE ONLY TIER B SOURCE WHOSE ToS REVIEW CLEARED. Seven of the eight either
refuse an identified client, prohibit automated access in terms, publish no
terms at all, or gate access behind a click-through agreement. The verbatim
clauses are in `compliance/registry.py`; read them before extending this
package to another vendor.

What it adds: 369 active lead cases with 2,623 affiliated debtors, against the
102 assignments the four court-published lists carry. It is the same KIND of
record as Phase 5 -- a chapter 11 census with a docket pointer -- at roughly
four times the coverage, and it names the affiliated debtors, which the court
lists do not.

THE AI CARVE-OUT IS THE ONE HARD RULE HERE. Stretto's terms contain no general
prohibition on automated access; their only scraping clause sits in section 21
and is scoped to "the AI TOOLS" -- the "Stretto Conductor" assistant
advertised on the case list. So this connector reads the case index and
NOTHING ELSE. `ENABLE_CHAT_BOT` is stored as metadata because it is in the
payload, but no chatbot endpoint is ever constructed or called, and a test
pins that.

MEASURED 2026-08-15, and the first one is a trap:

1. **The endpoint returns `recordsTotal: 2992` with an EMPTY `data` array if
   the DataTables `columns[...]` parameters are omitted.** HTTP 200, valid
   JSON, a large plausible total, and no rows. That is indistinguishable from
   "no cases today" to anything that only checks the status code -- exactly
   the silent failure the canary system exists for. The column parameters are
   therefore not optional decoration, and the canary asserts rows against a
   non-zero total rather than trusting either number alone.

2. `length` is ignored. The endpoint returns all 369 lead debtors in one
   ~1.1 MB response regardless. That is fine at weekly cadence and means no
   pagination to get wrong.

3. `recordsTotal` counts EVERY debtor row (2,992); `data` carries only lead
   debtors (369) with affiliates nested under `sub_debtor`. Comparing the two
   as if they measured the same thing would report permanent partial
   coverage.

4. `DATE_FILED` is null on 8 of 369 rows, and the response `Content-Type` is
   `text/html` despite being JSON.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Sequence

from ...canary.framework import CanaryFailure, ContentExpectation
from ...store.db import Item
from ..base import FetchTask, ParseResult

BASE = "https://cases.stretto.com"
AJAX = f"{BASE}/wp-admin/admin-ajax.php"
LEGACY_BASE = "https://case.stretto.com"

TASK_CASE_LIST = "claims_stretto:case_list"

# DataTables server-side parameters. See trap 1 in the module docstring: drop
# the `columns[...]` entries and the endpoint answers 200 with a plausible
# recordsTotal and zero rows.
_COLUMNS = ("CASE_NAME", "DATE_FILED", "COURT_DISTRICT")


def case_list_url() -> str:
    params: list[tuple[str, str]] = [
        ("action", "case_list_data"),
        ("draw", "1"),
        ("start", "0"),
        ("length", "1000"),      # ignored by the server; harmless and honest
    ]
    for i, name in enumerate(_COLUMNS):
        params += [
            (f"columns[{i}][data]", name),
            (f"columns[{i}][searchable]", "true"),
            (f"columns[{i}][orderable]", "true"),
        ]
    params += [
        ("order[0][column]", "1"),
        ("order[0][dir]", "desc"),
        ("search[value]", ""),
    ]
    return f"{AJAX}?{urllib.parse.urlencode(params)}"


def _clean(v: object) -> str:
    return " ".join(str(v or "").split())


def _parse_date(raw: object) -> str:
    """'2026-03-23 12:00:00' -> '2026-03-23'. Null on ~2% of rows."""
    s = _clean(raw)
    if not s:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else ""


def _docket_url(row: dict[str, Any]) -> str:
    """The public docket page for a case, or empty when Stretto offers none.

    Deliberately NOT synthesized when PROVIDE_LINK is 0 -- 68 of 369 rows have
    no public page, and inventing a URL that 404s is worse than admitting the
    absence.
    """
    if _clean(row.get("PROVIDE_LINK")) != "1":
        return ""
    slug = _clean(row.get("VANITY_URL")) or _clean(row.get("URL"))
    if not slug:
        return ""
    base = LEGACY_BASE if _clean(row.get("IS_LEGACY")) == "1" else BASE
    return f"{base}/{slug}"


class StrettoCaseIndexConnector:
    source_id = "claims_stretto"
    # The list moves by a handful of cases a month, and the whole thing is one
    # 1.1 MB response. Daily polling would spend budget re-reading 369
    # unchanged rows.
    schedule = "weekly"

    def plan(self, watermark: str | None) -> Sequence[FetchTask]:
        return [
            FetchTask(
                task_key=TASK_CASE_LIST,
                url=case_list_url(),
                accept="application/json, text/javascript, */*;q=0.8",
                note="Stretto public chapter 11 case index",
            )
        ]

    def expectation(self, task: FetchTask) -> ContentExpectation:
        # NOT content_types=("application/json",): the endpoint answers
        # text/html while returning JSON. Asserting the declared type would
        # fail a perfectly good response.
        return ContentExpectation(min_bytes=500)

    # -- parse -------------------------------------------------------------

    def parse(self, raw: bytes, url: str) -> ParseResult:
        """PURE: JSON bytes -> one Item per lead debtor."""
        try:
            doc = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            return ParseResult(note=f"unparsable JSON: {exc}")
        if not isinstance(doc, dict):
            return ParseResult(note="unexpected JSON shape (not an object)")

        rows = doc.get("data")
        if not isinstance(rows, list):
            return ParseResult(note="response carries no 'data' array")

        total_debtors = _clean(doc.get("recordsTotal"))
        items: list[Item] = []

        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            case_no = _clean(row.get("CASE_NO"))
            case_id = _clean(row.get("CASE_ID"))
            if not (case_no or case_id):
                continue

            case_name = _clean(row.get("CASE_NAME"))
            court = _clean(row.get("NAME"))
            date_filed = _parse_date(row.get("DATE_FILED"))
            docket_url = _docket_url(row)

            subs = row.get("sub_debtor")
            affiliates = [
                _clean(s.get("DEBTOR_NAME"))
                for s in (subs if isinstance(subs, list) else [])
                if isinstance(s, dict) and _clean(s.get("DEBTOR_NAME"))
            ]

            items.append(
                Item(
                    source_id=self.source_id,
                    # CASE_ID is Stretto's own stable key; CASE_NO can repeat
                    # across districts.
                    natural_key=f"stretto:{case_id or case_no}",
                    title=f"[Stretto] {case_no} — {case_name}"[:300],
                    # Census language only. No "settlement", no "judgment",
                    # no "damages" -- this row records that a chapter 11 case
                    # exists and who administers its claims. Writing event
                    # language here would manufacture a signal the source
                    # does not carry and spend extraction budget on it, the
                    # same discipline as sec_daily_index and the Phase 5
                    # census. A test pins it.
                    body=(
                        f"Chapter 11 case {case_no} in "
                        f"{court or 'an unstated district'}. Debtor: "
                        f"{_clean(row.get('DEBTOR_NAME')) or case_name}. "
                        f"Claims agent: Stretto."
                        + (f" Affiliated debtors: {len(affiliates)}."
                           if affiliates else "")
                    ),
                    source_url=docket_url or BASE,
                    published_at=date_filed or None,
                    extract_locator=f"data[{idx}]",
                    payload={
                        "record_kind": "claims_assignment",
                        # The COURT, not the vendor. The census joins these
                        # rows with the court-published lists, where `court`
                        # means the district -- putting "stretto" here would
                        # make the merged table say the case was filed in a
                        # claims agent.
                        "court": court,
                        "vendor_id": "stretto",
                        "vendor_name": "Stretto",
                        "vendor_tos_source_id": "claims_stretto",
                        "case_number": case_no,
                        "stretto_case_id": case_id,
                        "debtor": _clean(row.get("DEBTOR_NAME")) or case_name,
                        "case_name": case_name,
                        "court_district": court,
                        "date_filed": date_filed,
                        "agent_case_url": docket_url,
                        "is_legacy": _clean(row.get("IS_LEGACY")) == "1",
                        "has_public_docket": bool(docket_url),
                        "affiliated_debtors": affiliates,
                        # Recorded because it is in the payload. NEVER acted
                        # on: the chatbot is the "AI TOOLS" that Stretto's
                        # section 21 protects, and it is the one thing in
                        # these terms we could actually violate.
                        "chatbot_enabled": _clean(row.get("ENABLE_CHAT_BOT")) == "1",
                    },
                )
            )

        note = (
            f"{len(items)} lead chapter 11 cases "
            f"({total_debtors} debtor rows incl. affiliates)"
        )
        return ParseResult(items=items, note=note)

    # -- canary ------------------------------------------------------------

    def canary(self, raw: bytes, task: FetchTask) -> None:
        try:
            doc = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise CanaryFailure(
                self.source_id,
                f"case list did not return JSON ({exc}). A WAF challenge page "
                f"would look like this -- cases.stretto.com fronts Incapsula "
                f"and AWS WAF, so check for an interstitial before assuming "
                f"the endpoint moved.",
            ) from exc

        rows = doc.get("data")
        total = doc.get("recordsTotal")

        if not isinstance(rows, list):
            raise CanaryFailure(
                self.source_id, "response has no 'data' array; the endpoint "
                "contract changed."
            )

        # THE TRAP, pinned. A non-zero recordsTotal with zero rows is what
        # this endpoint returns when the DataTables `columns[...]` parameters
        # are missing -- HTTP 200, valid JSON, plausible total, no data. Left
        # unchecked it reads as a quiet week, forever.
        if not rows:
            try:
                n = int(str(total))
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                raise CanaryFailure(
                    self.source_id,
                    f"endpoint reported recordsTotal={n} but returned ZERO "
                    f"rows. This is the documented failure mode when the "
                    f"DataTables columns[...] parameters are dropped from the "
                    f"query -- it is NOT a quiet week. Check case_list_url().",
                )
            raise CanaryFailure(
                self.source_id,
                "case list returned no rows and no total; Stretto administers "
                "hundreds of open cases, so an empty index is not credible.",
            )

        first = rows[0] if isinstance(rows[0], dict) else {}
        missing = [k for k in ("CASE_NO", "CASE_NAME", "DATE_FILED")
                   if k not in first]
        if missing:
            raise CanaryFailure(
                self.source_id,
                f"case rows are missing expected field(s) {missing}. The "
                f"endpoint's response shape changed.",
            )

    def watermark_for(self, items: Sequence[Item]) -> str | None:
        return datetime.now(timezone.utc).date().isoformat()


def build() -> StrettoCaseIndexConnector:
    return StrettoCaseIndexConnector()
