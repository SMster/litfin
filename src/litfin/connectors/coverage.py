"""Venue coverage matrix.

Deliberately NOT a Connector. Connectors model recurring event ingestion with
a pure bytes->items parse; this is a small, weekly metadata bootstrap that
needs a bounded cursor walk. Forcing it into the connector protocol would mean
breaking parse() purity for one caller, so it lives here instead and still
goes through PoliteClient -- and therefore through the compliance gate, the
rate limiter, robots, and the budget.

WHY IT EXISTS: roughly 173 of ~200 federal ECF instances publish a PACER RSS
feed. About 15 publish nothing, and several publish only orders and opinions.
Whether a court publishes, and what it publishes, is a local choice -- and it
is the root cause of RECAP's uneven freshness.

The consequence is the point: in a venue with no feed, ABSENCE OF SIGNAL IS
NOT ABSENCE OF ACTIVITY. Without this map, an empty venue on the dashboard
reads as "nothing happened there", which is exactly the false conclusion that
turns a sourcing tool into a false sense of completeness.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ..net.client import PoliteClient
from ..store.db import Database

log = logging.getLogger("litfin.coverage")

API = "https://www.courtlistener.com/api/rest/v4"

# The jurisdictions that actually carry RECAP docket text.
#   FD = federal district, FB = federal bankruptcy
JURISDICTIONS = ("FD", "FB")

# The endpoint returns 20 per page and ignores page_size, so ~105 district +
# ~94 bankruptcy courts is roughly 10 pages. Bounded so a runaway cursor
# cannot drain the daily quota.
MAX_PAGES_PER_JURISDICTION = 12


@dataclass(slots=True)
class CoverageStats:
    courts: int = 0
    full_feed: int = 0
    partial_feed: int = 0
    no_feed: int = 0
    pages_fetched: int = 0
    hit_page_limit: bool = False

    def to_markdown(self) -> str:
        lines = [
            "| venue coverage | count |", "|---|---:|",
            f"| courts mapped | {self.courts} |",
            f"| full RSS feed (high confidence) | {self.full_feed} |",
            f"| partial feed (orders/opinions only) | {self.partial_feed} |",
            f"| **no feed (absence of signal != absence of activity)** | "
            f"**{self.no_feed}** |",
        ]
        if self.hit_page_limit:
            lines.append("")
            lines.append(
                f"> Stopped at the {MAX_PAGES_PER_JURISDICTION}-page bound; "
                f"the map may be incomplete."
            )
        return "\n".join(lines)


def refresh(client: PoliteClient, db: Database) -> CoverageStats:
    """Walk the courts endpoint and store the coverage map."""
    stats = CoverageStats()

    for jurisdiction in JURISDICTIONS:
        url = f"{API}/courts/?jurisdiction={jurisdiction}&in_use=true"
        for _ in range(MAX_PAGES_PER_JURISDICTION):
            resp = client.get(url, source_id="courtlistener",
                              accept="application/json")
            stats.pages_fetched += 1
            try:
                data = json.loads(resp.body)
            except json.JSONDecodeError:
                log.warning("unparsable courts page: %s", url)
                break

            for court in data.get("results") or []:
                cid = court.get("id")
                if not cid:
                    continue
                has_rss = court.get("pacer_has_rss_feed")
                entry_types = (court.get("pacer_rss_entry_types") or "").strip()
                confidence = _confidence(has_rss, entry_types)

                db.store_court_coverage(
                    court_id=cid,
                    full_name=court.get("full_name") or cid,
                    jurisdiction=court.get("jurisdiction") or jurisdiction,
                    pacer_court_id=court.get("pacer_court_id"),
                    has_rss=has_rss,
                    entry_types=entry_types,
                    confidence=confidence,
                )
                stats.courts += 1
                if confidence == "high":
                    stats.full_feed += 1
                elif confidence == "partial":
                    stats.partial_feed += 1
                elif confidence == "low":
                    stats.no_feed += 1

            nxt = data.get("next")
            if not nxt:
                break
            url = nxt
        else:
            stats.hit_page_limit = True

    return stats


def _confidence(has_rss: object, entry_types: str) -> str:
    """How much to trust an EMPTY result from this venue.

    'all' in entry_types means the court publishes every docket entry type.
    Anything narrower (e.g. orders and opinions only) means routine docket
    activity never appears in the feed at all.
    """
    if has_rss is None:
        return "not_applicable"
    if has_rss is False:
        return "low"
    if entry_types and entry_types.lower() not in ("all", "*"):
        return "partial"
    return "high"
