"""Opus extraction: candidate selection, Batches submission, result storage.

Cost control has three independent layers, because a runaway crawl feeding an
LLM is the one part of this pipeline that can produce a surprise bill:

  1. The lexical exclusion screen drops out-of-scope documents before any
     API call.
  2. The taxonomy screen drops documents carrying no deal signal at all.
  3. A hard daily candidate cap (config: extract.max_candidates_per_day)
     truncates whatever survives, lowest-signal first.

The Batches API is used because latency is irrelevant here -- the deadline is
"before the user wakes up" -- and it halves the cost. Results come back in
ARBITRARY order and must be keyed by custom_id, never by position.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from ..config import Config
from ..score.exclude import ExclusionVerdict, screen
from ..score.taxonomy import Thesis, classify_text, practice_area_hints
from ..store.db import Database
from .prompts import build_system_blocks, build_user_content
from .schema import SCHEMA_VERSION, CaseExtraction, json_schema

log = logging.getLogger("litfin.extract")

MAX_TOKENS = 8000


@dataclass(slots=True)
class Candidate:
    item_uid: str
    source_id: str
    title: str
    body: str
    source_url: str
    thesis: Thesis
    strength: float
    practice_hints: list[str]

    @property
    def custom_id(self) -> str:
        # Batches results return in arbitrary order; this is how they are
        # matched back. Must be unique within a batch.
        return self.item_uid[:60]


@dataclass(slots=True)
class ScreenReport:
    total: int = 0
    excluded: int = 0
    no_signal: int = 0
    capped: int = 0
    selected: int = 0
    exclusions: list[tuple[str, ExclusionVerdict]] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            "| stage | count |", "|---|---:|",
            f"| candidates considered | {self.total} |",
            f"| dropped: out of scope | {self.excluded} |",
            f"| dropped: no deal signal | {self.no_signal} |",
            f"| dropped: over daily cap | {self.capped} |",
            f"| **sent to extraction** | **{self.selected}** |",
        ]
        if self.exclusions:
            lines += ["", "Exclusion audit (first 15):", ""]
            for uid, v in self.exclusions[:15]:
                lines.append(f"- `{uid[:12]}` {v.audit_line}")
        return "\n".join(lines)


def select_candidates(
    db: Database, cfg: Config, *, limit: int | None = None,
    refresh: bool = False,
) -> tuple[list[Candidate], ScreenReport]:
    """Pick which stored items are worth an Opus call.

    By default: items with no extraction at all.

    With `refresh=True`: ALSO items whose stored extraction predates the
    current schema version. Adding one field should not mean re-extracting
    the whole corpus, and it should not mean living with a column that is
    permanently blank for everything collected before today either --
    `schema_version` is what makes the middle option possible.
    """
    cap = limit if limit is not None else cfg.max_candidates_per_day
    report = ScreenReport()
    candidates: list[Candidate] = []

    if refresh:
        rows = db.conn.execute(
            """
            SELECT i.item_uid, i.source_id, i.title, i.body, i.source_url
            FROM item i
            LEFT JOIN extraction e ON e.item_uid = i.item_uid
            WHERE e.item_uid IS NULL
               OR COALESCE(e.schema_version, 0) < ?
            ORDER BY i.observed_at DESC
            """,
            (SCHEMA_VERSION,),
        ).fetchall()
    else:
        rows = db.conn.execute(
            """
            SELECT i.item_uid, i.source_id, i.title, i.body, i.source_url
            FROM item i
            LEFT JOIN extraction e ON e.item_uid = i.item_uid
            WHERE e.item_uid IS NULL
            ORDER BY i.observed_at DESC
            """
        ).fetchall()

    for r in rows:
        report.total += 1
        text = f"{r['title']}\n{r['body']}"

        verdict = screen(text)
        if verdict.excluded:
            report.excluded += 1
            report.exclusions.append((r["item_uid"], verdict))
            db.record_screen(r["item_uid"], "excluded", verdict.reason)
            continue

        hit = classify_text(text)
        if not hit.is_signal:
            report.no_signal += 1
            db.record_screen(r["item_uid"], "no_signal", "no deal-thesis pattern matched")
            continue

        candidates.append(
            Candidate(
                item_uid=r["item_uid"],
                source_id=r["source_id"],
                title=r["title"] or "",
                body=r["body"] or "",
                source_url=r["source_url"] or "",
                thesis=hit.thesis,
                strength=hit.strength,
                practice_hints=practice_area_hints(text),
            )
        )

    # Strongest signal first, so the cap truncates the tail rather than a
    # random slice.
    candidates.sort(key=lambda c: c.strength, reverse=True)
    if len(candidates) > cap:
        report.capped = len(candidates) - cap
        log.warning(
            "Daily extraction cap reached: %d candidates dropped (cap=%d). "
            "Raise extract.max_candidates_per_day to widen.",
            report.capped, cap,
        )
        candidates = candidates[:cap]

    report.selected = len(candidates)
    return candidates, report


def _request_params(c: Candidate, cfg: Config) -> dict[str, Any]:
    return {
        "model": cfg.extract_model,
        "max_tokens": MAX_TOKENS,
        "system": build_system_blocks(),
        "thinking": {"type": "adaptive"},
        "output_config": {
            "format": {"type": "json_schema", "schema": json_schema()}
        },
        "messages": [
            {
                "role": "user",
                "content": build_user_content(
                    title=c.title,
                    body=c.body,
                    source_url=c.source_url,
                    source_id=c.source_id,
                    hint_thesis=c.thesis,
                    hint_practice_areas=c.practice_hints,
                ),
            }
        ],
    }


def extract_sync(
    candidates: Sequence[Candidate], cfg: Config, db: Database, *, limit: int = 5
) -> int:
    """Synchronous extraction for a handful of items.

    Used for smoke-testing the prompt and confirming the cache is working
    before committing a whole day's volume to a batch.
    """
    import anthropic

    client = anthropic.Anthropic()
    stored = 0
    cache_reads = 0

    for c in candidates[:limit]:
        params = _request_params(c, cfg)
        resp = client.messages.create(**params)

        cache_reads += getattr(resp.usage, "cache_read_input_tokens", 0) or 0
        text = next((b.text for b in resp.content if b.type == "text"), "")
        if not text:
            log.warning("empty extraction for %s", c.item_uid[:12])
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            log.warning("unparsable extraction for %s", c.item_uid[:12])
            continue
        db.store_extraction(c.item_uid, payload, model=cfg.extract_model,
                            schema_version=SCHEMA_VERSION)
        stored += 1

    if stored and cache_reads == 0:
        # Not fatal, but it means something above the cache breakpoint is
        # varying between requests and the cost saving is being lost.
        log.warning(
            "cache_read_input_tokens was ZERO across %d extractions -- the "
            "cached prefix is being invalidated. Check that nothing volatile "
            "(timestamps, ids, counters) leaked into build_system_blocks().",
            stored,
        )
    else:
        log.info("cache reads: %d tokens across %d extractions", cache_reads, stored)
    return stored


def submit_batch(
    candidates: Sequence[Candidate], cfg: Config, db: Database
) -> str | None:
    """Submit the day's extraction as a Message Batch. Returns the batch id."""
    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    if not candidates:
        return None

    client = anthropic.Anthropic()
    requests = [
        Request(
            custom_id=c.custom_id,
            params=MessageCreateParamsNonStreaming(**_request_params(c, cfg)),
        )
        for c in candidates
    ]
    batch = client.messages.batches.create(requests=requests)
    db.record_batch(batch.id, len(requests))
    log.info("submitted batch %s with %d requests", batch.id, len(requests))
    return batch.id


def collect_batch(batch_id: str, cfg: Config, db: Database, *, wait: bool = False) -> int:
    """Fetch batch results and store them. Returns the number stored."""
    import anthropic

    client = anthropic.Anthropic()

    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            break
        if not wait:
            log.info("batch %s still %s", batch_id, batch.processing_status)
            return 0
        time.sleep(30)

    # Map custom_id -> item_uid. Results arrive in ARBITRARY order, so never
    # match them positionally.
    id_map = db.batch_custom_id_map()

    stored = 0
    for result in client.messages.batches.results(batch_id):
        if result.result.type != "succeeded":
            log.warning("batch item %s: %s", result.custom_id, result.result.type)
            continue
        msg = result.result.message
        text = next((b.text for b in msg.content if b.type == "text"), "")
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        item_uid = id_map.get(result.custom_id, result.custom_id)
        db.store_extraction(item_uid, payload, model=cfg.extract_model,
                            schema_version=SCHEMA_VERSION)
        stored += 1

    db.close_batch(batch_id, stored)
    return stored


def validate(payload: dict) -> CaseExtraction | None:
    try:
        return CaseExtraction.model_validate(payload)
    except Exception:
        return None
