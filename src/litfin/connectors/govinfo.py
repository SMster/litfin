"""govinfo USCOURTS -- a federal court OPINION INDEX, not an event source.

Be clear about what this can and cannot do, because the distinction drives
the whole design.

WHAT IT GIVES (one request per 100 packages):
    packageId  e.g. "USCOURTS-caed-2_22-cv-00177"
    title      the case name, e.g. "Houston v. City of Fairfield et al."
    dateIssued, lastModified, docClass

WHAT IT DOES NOT GIVE at feasible request volume: any text describing what
happened. The rich metadata -- caseType, courtCircuit, parties, documentType
-- lives on `/packages/{id}/summary`, which is ONE REQUEST PER PACKAGE.
Measured 2026-08-15: ~4,100 packages in two days, so ~2,000/day. At
api.data.gov's 1,000/hour that is not affordable, and the opinion text itself
needs a further granule fetch.

CONSEQUENCE: package titles are bare case names with no event language, so
these will never trip the deal-thesis taxonomy on their own. Feeding them to
the LLM would burn the daily extraction budget on rows that say nothing.

SO THIS IS AN INDEX, deliberately. A package existing means a federal court
issued a WRITTEN OPINION in that case on that date -- which is a real signal
when cross-referenced against a case another source already surfaced
("CourtListener found a judgment entry here, and there is a published opinion
the same week"). Items are stored with record_kind="opinion_index" and carry
no synthetic event language, so the screen correctly drops them from
extraction while the dashboard can still join on them.

FREE FILTER: the case number embeds its own case type -- "-cv-" is civil,
"-cr-" is criminal. Filtering to civil at parse time removes roughly half the
volume at zero request cost, before anything else runs.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Sequence
from urllib.parse import urlencode

from ..canary.framework import ContentExpectation
from ..store.db import Item
from .base import FetchTask, ParseResult

API = "https://api.govinfo.gov"

# DEMO_KEY works for development but is globally throttled and shared. Register
# a free key at https://api.data.gov/signup/ and set GOVINFO_API_KEY.
DEFAULT_KEY = "DEMO_KEY"

# Court prefixes worth indexing: districts and bankruptcy courts in the
# commercially significant venues, plus the circuits that hear their appeals.
# Empty set = keep every court.
COMMERCIAL_COURTS: frozenset[str] = frozenset({
    # District
    "nysd", "nyed", "ded", "cand", "candce", "ilnd", "txsd", "txnd",
    "njd", "mad", "flsd", "gand", "ncwd", "paed", "wawd", "ohnd",
    # Bankruptcy
    "deb", "nysb", "njb", "txsb", "ilnb", "cacb",
    # Circuits (appeals -- relevant to judgment monetization)
    "ca1", "ca2", "ca3", "ca5", "ca7", "ca9", "ca11", "cafc",
})

_CASE_TYPE_RE = re.compile(r"-(\d+)[_:](\d{2})-([a-z]{2,3})-(\d+)", re.IGNORECASE)


class GovinfoConnector:
    source_id = "govinfo"
    schedule = "daily"

    def __init__(
        self,
        api_key: str = DEFAULT_KEY,
        lookback_days: int = 2,
        pages: int = 6,
        courts: frozenset[str] = COMMERCIAL_COURTS,
        civil_only: bool = True,
    ) -> None:
        self.api_key = api_key or DEFAULT_KEY
        self.lookback_days = max(1, lookback_days)
        # 6 pages x 100 = 600 packages/run. The collection is ordered by
        # lastModified, and a single busy court can fill several pages, so
        # this is a sample rather than exhaustive coverage -- which is
        # acceptable for an index whose job is cross-reference, not detection.
        self.pages = max(1, pages)
        self.courts = courts
        self.civil_only = civil_only

    def plan(self, watermark: str | None) -> Sequence[FetchTask]:
        since = (
            datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        ).strftime("%Y-%m-%dT00:00:00Z")
        tasks: list[FetchTask] = []
        for page in range(self.pages):
            qs = urlencode({
                "offset": page * 100,
                "pageSize": 100,
                "api_key": self.api_key,
            })
            tasks.append(
                FetchTask(
                    task_key=f"{self.source_id}:{since[:10]}:p{page}",
                    url=f"{API}/collections/USCOURTS/{since}?{qs}",
                    accept="application/json",
                    note="federal court opinion index (cross-reference source)",
                )
            )
        return tasks

    def expectation(self, task: FetchTask) -> ContentExpectation:
        return ContentExpectation(
            content_types=("application/json",),
            min_bytes=20,
            must_contain=('"packages"',),
        )

    def parse(self, raw: bytes, url: str) -> ParseResult:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return ParseResult()

        packages = data.get("packages") or []
        count = data.get("count")
        items: list[Item] = []
        skipped_criminal = 0
        skipped_court = 0

        for idx, pkg in enumerate(packages):
            pid = pkg.get("packageId") or ""
            if not pid:
                continue
            court, case_type, case_no = _parse_package_id(pid)

            # Free filters, applied before anything expensive.
            if self.civil_only and case_type and case_type.lower() != "cv":
                skipped_criminal += 1
                continue
            if self.courts and court and court not in self.courts:
                skipped_court += 1
                continue

            title = pkg.get("title") or pid
            items.append(
                Item(
                    source_id=self.source_id,
                    natural_key=pid,
                    title=title,
                    # No synthetic event language. These are index rows; adding
                    # words like "judgment" here would manufacture a signal the
                    # source does not actually carry.
                    body=(
                        f"Written opinion indexed by govinfo. Case {case_no or '?'} "
                        f"in court {court or '?'}. Issued {pkg.get('dateIssued')}."
                    ),
                    source_url=pkg.get("packageLink") or "",
                    published_at=_iso(pkg.get("dateIssued")),
                    extract_locator=f"packages[{idx}]",
                    payload={
                        "record_kind": "opinion_index",
                        "package_id": pid,
                        "court": court,
                        "case_number": case_no,
                        "case_type": case_type,
                        "doc_class": pkg.get("docClass"),
                        "date_issued": pkg.get("dateIssued"),
                        "last_modified": pkg.get("lastModified"),
                        # Fetching this would cost one request per package.
                        "summary_link": f"{API}/packages/{pid}/summary",
                    },
                )
            )

        note = (
            f"{len(items)} civil opinions indexed "
            f"(of {len(packages)} in page, {count} in window; "
            f"{skipped_criminal} criminal, {skipped_court} out-of-scope court)"
        )
        return ParseResult(
            items=items,
            note=note,
            # An empty page after filtering is NOT a broken parser -- the
            # collection is ordered by lastModified and one busy court can fill
            # an entire page with cases we deliberately skip.
            server_reported_empty=(not packages and count == 0) or bool(packages),
        )

    def watermark_for(self, items: Sequence[Item]) -> str | None:
        stamps = [
            i.payload.get("last_modified") for i in items
            if i.payload.get("last_modified")
        ]
        return max(stamps) if stamps else None


def _parse_package_id(pid: str) -> tuple[str, str, str]:
    """USCOURTS-<court>-<office>_<yy>-<type>-<serial> -> (court, type, caseno).

    The case type is embedded for free: 'cv' civil, 'cr' criminal, 'bk'
    bankruptcy, 'md' MDL. That saves a per-package metadata request.
    """
    parts = pid.split("-", 2)
    court = parts[1] if len(parts) > 2 else ""
    rest = parts[2] if len(parts) > 2 else ""
    m = _CASE_TYPE_RE.search("-" + rest)
    if m:
        return court, m.group(3), f"{m.group(1)}:{m.group(2)}-{m.group(3)}-{m.group(4)}"
    # Fall back to a bare substring check when the format differs.
    for t in ("cv", "cr", "bk", "md"):
        if f"-{t}-" in rest:
            return court, t, rest
    return court, "", rest


def _iso(day: str | None) -> str | None:
    if not day:
        return None
    try:
        return datetime.fromisoformat(day).replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def build(api_key: str = "", lookback_days: int = 2) -> GovinfoConnector:
    import os

    return GovinfoConnector(
        api_key=api_key or os.environ.get("GOVINFO_API_KEY", "") or DEFAULT_KEY,
        lookback_days=lookback_days,
    )
