"""Phase 1 connectors: SEC Litigation Releases, DOJ Antitrust, FTC.

One shared implementation (`FeedConnector`) plus three declarative configs.
Each config records WHY its natural key was chosen -- that choice is where
feed connectors usually go wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from ..canary.framework import ContentExpectation
from ..store.db import Item
from .base import FetchTask, ParseResult
from .rss import FeedEntry, parse_feed


@dataclass(frozen=True, slots=True)
class FeedSpec:
    source_id: str
    urls: tuple[str, ...]
    schedule: str
    key_field: str                 # "guid" | "link" | "creator"
    key_rationale: str
    expectation: ContentExpectation
    min_entries: int = 1


# -- SEC Litigation Releases -------------------------------------------------
# Confirmed live: official RSS, 25 items.
#
# Two gotchas found by probing this exact feed:
#   * guid isPermaLink="false" is an OPAQUE UUID -- unusable as a stable key.
#     dc:creator carries the release number (e.g. "LR-26610") and is
#     monotonic, so that is the natural key.
#   * <link> values carry a TRAILING NEWLINE. rss._clean() strips it; without
#     that every URL 404s.
SEC_LITREL = FeedSpec(
    source_id="sec_litrel",
    urls=(
        "https://www.sec.gov/enforcement-litigation/litigation-releases/rss",
    ),
    schedule="4x-daily",
    key_field="creator",
    key_rationale=(
        "dc:creator carries the monotonic LR-##### release number. The guid "
        "is isPermaLink='false' and opaque, so it is NOT stable."
    ),
    expectation=ContentExpectation(
        content_types=("application/rss+xml", "text/xml", "application/xml"),
        min_bytes=500,
        must_contain=("<item",),
    ),
)

# -- DOJ Antitrust Division --------------------------------------------------
# The "three separate feeds" premise was wrong. DOJ runs ONE parameterized
# Drupal view; field_component=376 selects the Antitrust Division.
#
# The feed window is a fixed 25 items. During a merger wave DOJ can exceed 25
# press releases between polls, so this is scheduled 4x/day and Phase 2 adds a
# daily reconcile against the HTML listing.
DOJ_ATR = FeedSpec(
    source_id="doj_atr",
    urls=(
        "https://www.justice.gov/news/rss?type[0]=press_release"
        "&field_component=376&search_api_language=en"
        "&show_public_archived=0&require_all=0",
    ),
    schedule="4x-daily",
    key_rationale=(
        "guid isPermaLink='true' and equals the article URL, which is stable. "
        "DOJ backdates items, so the seen-guid set is authoritative over "
        "max(pubDate)."
    ),
    key_field="guid",
    expectation=ContentExpectation(
        content_types=("application/rss+xml", "text/xml", "application/xml"),
        min_bytes=500,
        must_contain=("<item",),
    ),
)

# -- FTC ---------------------------------------------------------------------
# Confirmed live: https://www.ftc.gov/feeds/press-release.xml -> 200.
# Whether Cases & Proceedings has its own dedicated feed is UNCONFIRMED.
FTC = FeedSpec(
    source_id="ftc",
    urls=("https://www.ftc.gov/feeds/press-release.xml",),
    schedule="4x-daily",
    key_field="guid",
    key_rationale="Drupal guid is the article URL and is stable.",
    expectation=ContentExpectation(
        content_types=("application/rss+xml", "text/xml", "application/xml"),
        min_bytes=500,
        must_contain=("<item",),
    ),
)

SPECS: dict[str, FeedSpec] = {
    s.source_id: s for s in (SEC_LITREL, DOJ_ATR, FTC)
}


class FeedConnector:
    """Generic RSS connector driven by a FeedSpec."""

    def __init__(self, spec: FeedSpec) -> None:
        self.spec = spec
        self.source_id = spec.source_id
        self.schedule = spec.schedule

    def plan(self, watermark: str | None) -> Sequence[FetchTask]:
        tasks = []
        for i, url in enumerate(self.spec.urls):
            tasks.append(
                FetchTask(
                    task_key=f"{self.source_id}:feed:{i}",
                    url=url,
                    accept="application/rss+xml, application/xml;q=0.9, */*;q=0.8",
                    note=self.spec.key_rationale,
                )
            )
        return tasks

    def expectation(self, task: FetchTask) -> ContentExpectation:
        return self.spec.expectation

    def parse(self, raw: bytes, url: str) -> ParseResult:
        """PURE. Returns EVERYTHING in the feed -- the runner filters."""
        entries = parse_feed(raw)
        items: list[Item] = []
        for idx, e in enumerate(entries):
            key = e.best_key(self.spec.key_field)
            if not key:
                continue
            items.append(
                Item(
                    source_id=self.source_id,
                    natural_key=key,
                    title=e.title,
                    body=e.description,
                    source_url=e.link,
                    published_at=e.published,
                    extract_locator=f"item[{idx}]",
                    payload={
                        "guid": e.guid,
                        "guid_is_permalink": e.guid_is_permalink,
                        "creator": e.creator,
                        "categories": list(e.categories),
                        "published_raw": e.published_raw,
                        "feed_url": url,
                    },
                )
            )
        return ParseResult(
            items=items,
            note=f"{len(entries)} feed entries, {len(items)} keyed",
        )

    def watermark_for(self, items: Sequence[Item]) -> str | None:
        """Max published_at seen. The seen-key set is the real authority.

        Feeds backdate (DOJ demonstrably does), so a pure max(pubDate)
        watermark would silently skip late arrivals. The runner keeps a
        seen-key set alongside this value and uses BOTH.
        """
        stamps = [i.published_at for i in items if i.published_at]
        return max(stamps) if stamps else None


def build(source_id: str) -> FeedConnector:
    spec = SPECS.get(source_id)
    if spec is None:
        raise KeyError(f"No feed spec registered for {source_id!r}")
    return FeedConnector(spec)


def all_connectors() -> list[FeedConnector]:
    return [FeedConnector(s) for s in SPECS.values()]
