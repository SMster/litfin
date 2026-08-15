"""The connector contract.

The load-bearing design decision here is that `parse()` is PURE over bytes:

    parse(raw: bytes, url: str) -> ParseResult

It takes no client, performs no I/O, and consults no watermark. Everything
good falls out of that:

  * fixture replay is a plain pytest over a saved file
  * canaries can run against stored artifacts
  * `litfin replay` can re-run today's parser over months of historical raw
    data and diff the output -- the single highest-value debugging tool here
  * a production regression becomes a test case by copying one file

Watermark filtering happens OUTSIDE parse(), in the runner, which is why
`rows_parsed` (everything the parser saw) and `rows_new` (what survived the
watermark) can be compared. That comparison is the whole canary system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable

from ..canary.framework import ContentExpectation
from ..store.db import Item


@dataclass(slots=True)
class FetchTask:
    """One unit of fetchable work.

    Tasks are enumerated BEFORE execution and journaled, so a killed run
    resumes exactly where it stopped. Keep them small enough that redoing one
    is cheap.
    """

    task_key: str
    url: str
    accept: str | None = None
    conditional: bool = True
    note: str = ""

    @property
    def task_id(self) -> str:
        return self.task_key


@dataclass(slots=True)
class ParseResult:
    """Output of a pure parse over one artifact.

    `items` is EVERYTHING the parser saw, pre-watermark. The runner does the
    filtering. A parser that filters internally breaks the canary.
    """

    items: list[Item] = field(default_factory=list)
    anchors_found: set[str] = field(default_factory=set)
    note: str = ""

    # Set when the response was well-formed AND the server affirmatively
    # reported zero results -- e.g. EDGAR returning
    # {"hits": {"total": {"value": 0, "relation": "eq"}}} for a Saturday.
    #
    # This is the difference between "the server told us there is nothing"
    # (positive evidence, HEALTHY) and "we found no rows" (absence of
    # evidence, BROKEN). Without it, every weekend would page a human.
    #
    # Only set this where the response format carries an explicit count. Do
    # NOT set it merely because a parse returned nothing -- that is precisely
    # the failure the canary exists to catch.
    server_reported_empty: bool = False

    # Set when the source returned FEWER rows than it says exist, and the
    # remainder cannot be retrieved affordably (a hard page cap plus a rate
    # ceiling that makes cursor-walking impractical).
    #
    # This is deliberately NOT the same as BROKEN. Truncation from a known
    # API limit is a coverage limitation, not a defect: the rows we did get
    # are good and must be kept. The distinction matters because marking it
    # BROKEN would discard real data AND freeze the watermark forever, since
    # the condition would recur on every run.
    #
    # It must still be LOUD -- it surfaces as a run warning and is recorded
    # per source, so nobody mistakes a truncated result for a complete one.
    partial_coverage: bool = False
    coverage_note: str = ""

    @property
    def rows_parsed(self) -> int:
        return len(self.items)


@runtime_checkable
class Connector(Protocol):
    source_id: str
    schedule: str

    def plan(self, watermark: str | None) -> Sequence[FetchTask]:
        """Enumerate the work for this run. No I/O."""
        ...

    def parse(self, raw: bytes, url: str) -> ParseResult:
        """PURE: bytes -> items. No I/O, no watermark, no clock."""
        ...

    def expectation(self, task: FetchTask) -> ContentExpectation:
        """Structural assertions for this task's response."""
        ...

    def canary(self, raw: bytes, task: FetchTask) -> None:
        """OPTIONAL semantic canary. Raise CanaryFailure to fail loudly.

        Takes the FetchTask, not just the URL, so a connector can branch on
        `task.task_key`. Sniffing the URL instead is a trap: sec_fts once
        detected its anchor task by looking for the anchor's date in the URL,
        which also matched every ordinary daily slice on that date and failed
        them all.
        """
        ...

    def watermark_for(self, items: Sequence[Item]) -> str | None:
        """Compute the new watermark value from the items just parsed."""
        ...
