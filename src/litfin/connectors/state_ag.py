"""State Attorney General press-release feeds.

Multistate AG settlements -- opioids, pharma, consumer protection, antitrust --
are announced with dollar figures and are the largest publicly-disclosed
settlement category outside securities. That makes them worth having even at
low coverage.

COVERAGE IS GENUINELY POOR AND THE CONNECTOR SAYS SO. Seven of fifty states
publish a reachable feed (measured 2026-08-15 across 40 candidate URLs). New
York 403s an identified client and Texas 404s everywhere, so the two most
valuable states are dark. The feed list, including the probe record for every
state that failed, is data in `state_ag_feeds.toml` rather than code, so
adding a state is an edit not a deploy.

The design consequence: a dead feed must never fail the run. Each feed is its
own task, so one 404 degrades one task while the rest proceed, and the
connector reports live-vs-total so nobody mistakes 7 states for 50.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..canary.framework import ContentExpectation
from ..store.db import Item
from .base import FetchTask, ParseResult
from .rss import parse_feed

_FEEDS_FILE = Path(__file__).with_name("state_ag_feeds.toml")


@dataclass(frozen=True, slots=True)
class AgFeed:
    state: str
    name: str
    url: str


def load_feeds() -> tuple[list[AgFeed], list[dict]]:
    """Returns (verified feeds, blocked-state records)."""
    if not _FEEDS_FILE.is_file():
        return [], []
    with _FEEDS_FILE.open("rb") as fh:
        data = tomllib.load(fh)
    feeds = [
        AgFeed(state=f["state"], name=f.get("name", f["state"]), url=f["url"])
        for f in data.get("feed", [])
        if f.get("status") == "verified" and f.get("url")
    ]
    return feeds, list(data.get("blocked", []))


class StateAgConnector:
    """One task per verified feed. Reuses the Phase 1 RSS parser wholesale."""

    source_id = "state_ag"
    schedule = "daily"

    def __init__(self) -> None:
        self.feeds, self.blocked = load_feeds()
        self._by_url = {f.url: f for f in self.feeds}

    @property
    def coverage_note(self) -> str:
        return (
            f"{len(self.feeds)} of 50 state AG offices publish a reachable "
            f"feed ({', '.join(f.state for f in self.feeds)}). "
            f"{len(self.blocked)} probed and unavailable, including NY (403) "
            f"and TX (404) -- the two highest-value states."
        )

    def plan(self, watermark: str | None) -> Sequence[FetchTask]:
        return [
            FetchTask(
                task_key=f"{self.source_id}:{f.state}",
                url=f.url,
                accept="application/rss+xml, application/xml;q=0.9, */*;q=0.8",
                note=f"{f.name} press releases",
            )
            for f in self.feeds
        ]

    def expectation(self, task: FetchTask) -> ContentExpectation:
        # Deliberately loose. These are 50 different CMSes and several serve a
        # feed as text/html; a strict content-type assertion would fail live
        # feeds for a cosmetic reason.
        return ContentExpectation(min_bytes=200)

    def parse(self, raw: bytes, url: str) -> ParseResult:
        entries = parse_feed(raw)
        feed = self._by_url.get(url)
        state = feed.state if feed else _state_from_url(url)

        items: list[Item] = []
        for idx, e in enumerate(entries):
            key = e.best_key("guid")
            if not key:
                continue
            items.append(
                Item(
                    source_id=self.source_id,
                    # Namespaced by state: two AGs announcing the same
                    # multistate settlement are separate observations, and
                    # collapsing them would lose which states participated.
                    natural_key=f"{state}:{key}",
                    title=f"[{state}] {e.title}",
                    body=e.description or "",
                    source_url=e.link,
                    published_at=e.published,
                    extract_locator=f"item[{idx}]",
                    payload={
                        "record_kind": "ag_press_release",
                        "state": state,
                        "ag_office": feed.name if feed else "",
                        "guid": e.guid,
                        "categories": list(e.categories),
                        "feed_url": url,
                    },
                )
            )
        return ParseResult(items=items, note=f"{state}: {len(items)} releases")

    def watermark_for(self, items: Sequence[Item]) -> str | None:
        stamps = [i.published_at for i in items if i.published_at]
        return max(stamps) if stamps else None


def _state_from_url(url: str) -> str:
    feeds, _ = load_feeds()
    for f in feeds:
        if f.url == url:
            return f.state
    return "??"


def build() -> StateAgConnector:
    return StateAgConnector()
