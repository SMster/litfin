"""SQLite access layer.

The one invariant this module exists to protect: items and the watermark
advance in a SINGLE transaction. A crash rolls back all three writes together,
so the watermark can never advance past durably-stored items (nothing lost),
and INSERT OR IGNORE on a deterministic item_uid makes redoing a task a no-op
(nothing duplicated). At-least-once delivery + idempotent writes = exactly-once
effect.

That property is easy to break with a well-meaning refactor. Don't split
commit_task().
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

_SCHEMA = Path(__file__).with_name("schema.sql")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_item_uid(source_id: str, natural_key: str) -> str:
    """Deterministic item identity. Same input -> same uid, forever."""
    return hashlib.sha256(f"{source_id}\x00{natural_key}".encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Item:
    """A normalized observation extracted from one artifact."""

    source_id: str
    natural_key: str
    title: str = ""
    body: str = ""
    source_url: str = ""
    published_at: str | None = None
    artifact_sha256: str | None = None
    extract_locator: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def item_uid(self) -> str:
        return make_item_uid(self.source_id, self.natural_key)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    # Columns added after the first release. `CREATE TABLE IF NOT EXISTS` is a
    # no-op on an existing database, so a new column in schema.sql never
    # reaches one -- the table silently keeps its old shape and the next query
    # fails with "no such column". Additive migrations are listed here and
    # applied idempotently.
    _MIGRATIONS: tuple[tuple[str, str, str], ...] = (
        ("prospect", "cluster_key", "TEXT NOT NULL DEFAULT ''"),
        ("prospect", "cluster_size", "INTEGER NOT NULL DEFAULT 1"),
        ("prospect", "is_primary", "INTEGER NOT NULL DEFAULT 1"),
        # Why a SEPARATE column from the model's own `excluded_reason`: this
        # one records an exclusion the pipeline applied AFTER extraction, and
        # conflating the two would overwrite the model's judgement with ours
        # and make the audit trail unreadable.
        ("extraction", "excluded_reason_late", "TEXT NOT NULL DEFAULT ''"),
        # Which extraction schema produced this row. Lets `extract --refresh`
        # re-run only the rows captured before a field was added, instead of
        # re-extracting the whole corpus for one new field.
        ("extraction", "schema_version", "INTEGER NOT NULL DEFAULT 0"),
    )

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
            self._migrate()

    def _migrate(self) -> None:
        for table, column, decl in self._MIGRATIONS:
            cols = {
                r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if not cols:
                continue                       # table not created yet
            if column not in cols:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                )
        # Indexes on migrated columns belong here, after the columns exist.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prospect_cluster "
            "ON prospect(cluster_key)"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- sources -----------------------------------------------------------

    def upsert_source(
        self,
        source_id: str,
        *,
        display_name: str,
        tier: str,
        status: str,
        base_confidence: float,
        enabled: bool,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO source (source_id, display_name, tier, status,
                                    base_confidence, enabled)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    display_name    = excluded.display_name,
                    tier            = excluded.tier,
                    status          = excluded.status,
                    base_confidence = excluded.base_confidence,
                    enabled         = excluded.enabled
                """,
                (source_id, display_name, tier, status, base_confidence, int(enabled)),
            )

    def set_health(
        self,
        source_id: str,
        *,
        run_id: str,
        verdict: str,
        rows_parsed: int,
        rows_new: int,
        byte_size: int = 0,
        note: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO source_health
                    (source_id, run_id, observed_at, verdict, rows_parsed,
                     rows_new, byte_size, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, run_id) DO UPDATE SET
                    verdict = excluded.verdict,
                    rows_parsed = excluded.rows_parsed,
                    rows_new = excluded.rows_new,
                    byte_size = excluded.byte_size,
                    note = excluded.note
                """,
                (source_id, run_id, utcnow(), verdict, rows_parsed, rows_new,
                 byte_size, note),
            )
            if verdict == "HEALTHY":
                self._conn.execute(
                    "UPDATE source SET health=?, health_note=?, "
                    "last_success_at=?, consecutive_failures=0 "
                    "WHERE source_id=?",
                    (verdict, note, utcnow(), source_id),
                )
            else:
                self._conn.execute(
                    "UPDATE source SET health=?, health_note=?, "
                    "consecutive_failures=consecutive_failures+1 "
                    "WHERE source_id=?",
                    (verdict, note, source_id),
                )

    def consecutive_failures(self, source_id: str) -> int:
        row = self._conn.execute(
            "SELECT consecutive_failures FROM source WHERE source_id=?",
            (source_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    # -- artifacts ---------------------------------------------------------

    def register_artifact(
        self,
        *,
        sha256: str,
        source_id: str,
        url: str,
        http_status: int | None,
        content_type: str | None,
        byte_size: int,
        etag: str | None = None,
        last_modified: str | None = None,
        run_id: str | None = None,
        ext: str = "bin",
        compressed: bool = True,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO artifact (sha256, source_id, url, http_status,
                    content_type, byte_size, etag, last_modified, fetched_at,
                    run_id, ext, compressed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sha256) DO NOTHING
                """,
                (sha256, source_id, url, http_status, content_type, byte_size,
                 etag, last_modified, utcnow(), run_id, ext, int(compressed)),
            )

    # -- watermarks --------------------------------------------------------

    def get_watermark(self, source_id: str, task_key: str) -> tuple[str | None, set[str]]:
        row = self._conn.execute(
            "SELECT value, seen_keys FROM watermark WHERE source_id=? AND task_key=?",
            (source_id, task_key),
        ).fetchone()
        if row is None:
            return None, set()
        try:
            seen = set(json.loads(row["seen_keys"] or "[]"))
        except json.JSONDecodeError:
            seen = set()
        return row["value"], seen

    # -- the atomic commit -------------------------------------------------

    def commit_task(
        self,
        *,
        run_id: str,
        task_id: str,
        source_id: str,
        task_key: str,
        items: Sequence[Item],
        watermark_value: str | None,
        seen_keys: Iterable[str],
        rows_parsed: int,
        rows_new: int,
        status: str = "OK",
        error: str | None = None,
        max_seen_keys: int = 5000,
    ) -> int:
        """Write items, advance the watermark, and mark the task -- atomically.

        Returns the number of genuinely new items inserted.

        DO NOT split this into separate transactions. The whole correctness
        argument rests on these three writes succeeding or failing together.
        """
        inserted = 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for it in items:
                    cur = self._conn.execute(
                        """
                        INSERT INTO item (item_uid, source_id, natural_key,
                            artifact_sha256, extract_locator, source_url,
                            title, body, published_at, observed_at, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(item_uid) DO NOTHING
                        """,
                        (
                            it.item_uid, it.source_id, it.natural_key,
                            it.artifact_sha256, it.extract_locator, it.source_url,
                            it.title, it.body, it.published_at, utcnow(),
                            json.dumps(it.payload, sort_keys=True),
                        ),
                    )
                    if cur.rowcount:
                        inserted += 1
                        rowid = self._conn.execute(
                            "SELECT rowid FROM item WHERE item_uid=?", (it.item_uid,)
                        ).fetchone()
                        if rowid is not None:
                            self._conn.execute(
                                "INSERT INTO item_fts (rowid, title, body) "
                                "VALUES (?, ?, ?)",
                                (rowid[0], it.title, it.body),
                            )

                # Only advance the watermark when the task actually succeeded.
                # A BROKEN canary must NOT advance it, or the data it failed to
                # parse is skipped forever once the parser is fixed.
                if status == "OK":
                    keys = list(dict.fromkeys(seen_keys))
                    if len(keys) > max_seen_keys:
                        keys = keys[-max_seen_keys:]
                    self._conn.execute(
                        """
                        INSERT INTO watermark (source_id, task_key, value,
                                               seen_keys, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(source_id, task_key) DO UPDATE SET
                            value = excluded.value,
                            seen_keys = excluded.seen_keys,
                            updated_at = excluded.updated_at
                        """,
                        (source_id, task_key, watermark_value,
                         json.dumps(keys), utcnow()),
                    )

                self._conn.execute(
                    """
                    INSERT INTO run_task (run_id, task_id, source_id, task_key,
                        status, attempts, rows_parsed, rows_new, error,
                        started_at, finished_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, task_id) DO UPDATE SET
                        status = excluded.status,
                        attempts = run_task.attempts + 1,
                        rows_parsed = excluded.rows_parsed,
                        rows_new = excluded.rows_new,
                        error = excluded.error,
                        finished_at = excluded.finished_at
                    """,
                    (run_id, task_id, source_id, task_key, status,
                     rows_parsed, rows_new, error, utcnow(), utcnow()),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return inserted

    # -- runs --------------------------------------------------------------

    def start_run(self, run_id: str, purpose: str, note: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO run (run_id, started_at, status, purpose, note) "
                "VALUES (?, ?, 'running', ?, ?) "
                "ON CONFLICT(run_id) DO NOTHING",
                (run_id, utcnow(), purpose, note),
            )

    def finish_run(self, run_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE run SET finished_at=?, status=? WHERE run_id=?",
                (utcnow(), status, run_id),
            )

    def pending_tasks(self, run_id: str) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM run_task WHERE run_id=? AND status IN "
            "('PENDING','RUNNING','FAILED')",
            (run_id,),
        ))

    def run_tasks(self, run_id: str) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM run_task WHERE run_id=? ORDER BY source_id, task_key",
            (run_id,),
        ))

    # -- docket alerts -----------------------------------------------------

    def candidate_dockets(self, limit: int = 50) -> list[sqlite3.Row]:
        """Discovered RECAP dockets not yet subscribed to.

        Ordered by how strong the deal signal on that docket was, so a capped
        subscribe run takes the best candidates rather than an arbitrary slice.
        """
        # The aggregation is done in a subquery BEFORE the anti-join.
        #
        # BUG PINNED: an earlier version aliased the extracted id as
        # `docket_id` while LEFT JOINing docket_alert, which also has a
        # `docket_id` column. SQLite resolved `GROUP BY docket_id` to the
        # JOINED table's column -- NULL for every row when no alerts exist --
        # so all 96 distinct dockets collapsed into a single bucket of 136.
        # Aggregating first, and naming the alias `cl_docket_id`, removes the
        # ambiguity entirely rather than relying on resolution order.
        return list(self._conn.execute(
            """
            WITH discovered AS (
                SELECT
                    json_extract(payload_json, '$.docket_id')      AS cl_docket_id,
                    MAX(title)                                     AS case_name,
                    MAX(json_extract(payload_json, '$.docket_url')) AS docket_url,
                    COUNT(*)                                       AS hits,
                    MAX(published_at)                              AS last_seen
                FROM item
                WHERE source_id = 'courtlistener'
                  AND json_extract(payload_json, '$.docket_id') IS NOT NULL
                GROUP BY cl_docket_id
            )
            SELECT d.cl_docket_id AS docket_id, d.case_name, d.docket_url,
                   d.hits, d.last_seen
            FROM discovered d
            LEFT JOIN docket_alert a ON a.docket_id = d.cl_docket_id
            WHERE a.docket_id IS NULL
            ORDER BY d.hits DESC, d.last_seen DESC
            LIMIT ?
            """,
            (limit,),
        ))

    def record_alert(
        self, *, docket_id: int, status: str, cl_alert_id: int | None = None,
        case_name: str = "", docket_url: str = "", reason: str = "",
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO docket_alert (docket_id, cl_alert_id, status,
                    case_name, docket_url, reason, created_at, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(docket_id) DO UPDATE SET
                    cl_alert_id=COALESCE(excluded.cl_alert_id, docket_alert.cl_alert_id),
                    status=excluded.status,
                    case_name=COALESCE(NULLIF(excluded.case_name,''), docket_alert.case_name),
                    docket_url=COALESCE(NULLIF(excluded.docket_url,''), docket_alert.docket_url),
                    reason=excluded.reason,
                    error=excluded.error
                """,
                (docket_id, cl_alert_id, status, case_name, docket_url, reason,
                 utcnow(), error),
            )

    def alerts(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return list(self._conn.execute(
                "SELECT * FROM docket_alert WHERE status=? ORDER BY created_at DESC",
                (status,),
            ))
        return list(self._conn.execute(
            "SELECT * FROM docket_alert ORDER BY created_at DESC"
        ))

    def touch_alert(self, docket_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE docket_alert SET last_event_at=? WHERE docket_id=?",
                (utcnow(), docket_id),
            )

    # -- webhooks ----------------------------------------------------------

    def enqueue_webhook(
        self, *, idempotency_key: str, payload: str,
        event_type: int | None, remote_addr: str,
    ) -> bool:
        """Store an inbound delivery. Returns False if already seen.

        Must stay cheap -- it runs inside the sub-second window the receiver
        has to answer in.
        """
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO webhook_event (idempotency_key, received_at, "
                "event_type, remote_addr, payload_json) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(idempotency_key) DO NOTHING",
                (idempotency_key, utcnow(), event_type, remote_addr, payload),
            )
            return bool(cur.rowcount)

    def unprocessed_webhooks(self, limit: int = 200) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM webhook_event WHERE processed_at IS NULL "
            "ORDER BY received_at LIMIT ?",
            (limit,),
        ))

    def mark_webhook_processed(self, key: str, error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE webhook_event SET processed_at=?, process_error=? "
                "WHERE idempotency_key=?",
                (utcnow(), error, key),
            )

    def webhook_stats(self) -> dict[str, int]:
        row = self._conn.execute(
            "SELECT COUNT(*) total, "
            "SUM(CASE WHEN processed_at IS NULL THEN 1 ELSE 0 END) pending, "
            "SUM(CASE WHEN process_error IS NOT NULL THEN 1 ELSE 0 END) errored "
            "FROM webhook_event"
        ).fetchone()
        return {
            "total": int(row[0] or 0),
            "pending": int(row[1] or 0),
            "errored": int(row[2] or 0),
        }

    # -- venue coverage ----------------------------------------------------

    def store_court_coverage(
        self, *, court_id: str, full_name: str, jurisdiction: str | None,
        pacer_court_id: str | None, has_rss: object, entry_types: str,
        confidence: str,
    ) -> None:
        rss_int = None if has_rss is None else int(bool(has_rss))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO court_coverage (court_id, full_name, jurisdiction,
                    pacer_court_id, has_rss, entry_types, confidence, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(court_id) DO UPDATE SET
                    full_name=excluded.full_name,
                    jurisdiction=excluded.jurisdiction,
                    pacer_court_id=excluded.pacer_court_id,
                    has_rss=excluded.has_rss,
                    entry_types=excluded.entry_types,
                    confidence=excluded.confidence,
                    updated_at=excluded.updated_at
                """,
                (court_id, full_name, jurisdiction, pacer_court_id, rss_int,
                 entry_types, confidence, utcnow()),
            )

    def coverage_summary(self) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT confidence, COUNT(*) n FROM court_coverage "
            "GROUP BY confidence ORDER BY n DESC"
        ))

    def low_coverage_courts(self, limit: int = 40) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT court_id, full_name, confidence, entry_types "
            "FROM court_coverage WHERE confidence IN ('low','partial') "
            "ORDER BY confidence, court_id LIMIT ?",
            (limit,),
        ))

    def all_court_coverage(self) -> list[sqlite3.Row]:
        """Every mapped court, worst confidence first.

        The dashboard renders this in full rather than a summary count. A
        summary answers "how many venues are dark"; only the list answers
        "is the venue I care about one of them", which is the question that
        actually stops an empty result being misread as a quiet one.
        """
        return list(self._conn.execute(
            """
            SELECT court_id, full_name, jurisdiction, has_rss, entry_types,
                   confidence
            FROM court_coverage
            ORDER BY CASE confidence
                       WHEN 'low' THEN 0
                       WHEN 'partial' THEN 1
                       WHEN 'high' THEN 2
                       ELSE 3
                     END,
                     court_id
            """
        ))

    # -- screening and extraction -----------------------------------------

    def record_screen(self, item_uid: str, verdict: str, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO screen_result (item_uid, verdict, reason, screened_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(item_uid) DO UPDATE SET "
                "verdict=excluded.verdict, reason=excluded.reason, "
                "screened_at=excluded.screened_at",
                (item_uid, verdict, reason, utcnow()),
            )

    def store_extraction(
        self, item_uid: str, payload: dict, *, model: str,
        schema_version: int = 0,
    ) -> None:
        dmg = payload.get("damages") or {}
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO extraction (item_uid, model, extracted_at,
                    payload_json, case_caption, court, venue, jurisdiction,
                    practice_area, deal_thesis, event_type, event_date,
                    is_excluded, damages_usd, damages_conf, summary,
                    schema_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_uid) DO UPDATE SET
                    model=excluded.model,
                    schema_version=excluded.schema_version,
                    -- A refresh supersedes an earlier post-hoc exclusion:
                    -- the row is being judged again from scratch.
                    is_excluded=excluded.is_excluded,
                    excluded_reason_late='',
                    extracted_at=excluded.extracted_at,
                    payload_json=excluded.payload_json,
                    case_caption=excluded.case_caption,
                    court=excluded.court,
                    venue=excluded.venue,
                    jurisdiction=excluded.jurisdiction,
                    practice_area=excluded.practice_area,
                    deal_thesis=excluded.deal_thesis,
                    event_type=excluded.event_type,
                    event_date=excluded.event_date,
                    is_excluded=excluded.is_excluded,
                    damages_usd=excluded.damages_usd,
                    damages_conf=excluded.damages_conf,
                    summary=excluded.summary
                """,
                (
                    item_uid, model, utcnow(), json.dumps(payload, sort_keys=True),
                    payload.get("case_caption"), payload.get("court"),
                    payload.get("venue"), payload.get("jurisdiction"),
                    payload.get("practice_area"), payload.get("deal_thesis"),
                    payload.get("event_type"), payload.get("event_date"),
                    int(bool(payload.get("is_excluded"))),
                    dmg.get("amount_usd"), dmg.get("confidence"),
                    payload.get("summary"), schema_version,
                ),
            )

    def record_batch(self, batch_id: str, n_requests: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO extract_batch (batch_id, submitted_at, n_requests) "
                "VALUES (?, ?, ?) ON CONFLICT(batch_id) DO NOTHING",
                (batch_id, utcnow(), n_requests),
            )

    def close_batch(self, batch_id: str, n_stored: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE extract_batch SET n_stored=?, status='collected' "
                "WHERE batch_id=?",
                (n_stored, batch_id),
            )

    def open_batches(self) -> list[str]:
        return [
            r[0] for r in self._conn.execute(
                "SELECT batch_id FROM extract_batch WHERE status='submitted'"
            )
        ]

    def batch_custom_id_map(self) -> dict[str, str]:
        """custom_id (first 60 chars of item_uid) -> full item_uid."""
        return {
            r[0][:60]: r[0]
            for r in self._conn.execute("SELECT item_uid FROM item")
        }

    def extractions(self, include_excluded: bool = False) -> list[sqlite3.Row]:
        sql = (
            "SELECT e.*, i.source_id, i.source_url, i.title, i.published_at "
            "FROM extraction e JOIN item i ON i.item_uid = e.item_uid"
        )
        if not include_excluded:
            sql += " WHERE e.is_excluded = 0"
        return list(self._conn.execute(sql))

    def store_prospect(
        self, item_uid: str, score: float, components: dict,
        rank: int | None = None, *, cluster_key: str = "",
        cluster_size: int = 1, is_primary: bool = True,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO prospect (item_uid, scored_at, score, rank_in_run, "
                "components_json, cluster_key, cluster_size, is_primary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(item_uid) DO UPDATE SET scored_at=excluded.scored_at, "
                "score=excluded.score, rank_in_run=excluded.rank_in_run, "
                "components_json=excluded.components_json, "
                "cluster_key=excluded.cluster_key, "
                "cluster_size=excluded.cluster_size, "
                "is_primary=excluded.is_primary",
                (item_uid, utcnow(), score, rank,
                 json.dumps(components, sort_keys=True),
                 cluster_key, cluster_size, int(is_primary)),
            )

    def top_prospects(
        self, limit: int = 100, *, include_duplicates: bool = False
    ) -> list[sqlite3.Row]:
        """The ranked list, one row per MATTER.

        Duplicates are filtered, not deleted -- every scored row is still in
        the table and `cluster_members` returns them. Pass
        include_duplicates=True to see the raw pre-clustering list.
        """
        dup_filter = "" if include_duplicates else "AND p.is_primary = 1"
        return list(self._conn.execute(
            f"""
            SELECT p.score, p.components_json, p.cluster_key, p.cluster_size,
                   e.*, i.source_id, i.source_url,
                   i.title AS item_title, i.published_at
            FROM prospect p
            JOIN extraction e ON e.item_uid = p.item_uid
            JOIN item i ON i.item_uid = p.item_uid
            WHERE e.is_excluded = 0 {dup_filter}
            ORDER BY p.score DESC
            LIMIT ?
            """,
            (limit,),
        ))

    def cluster_members(self, keys: Sequence[str]) -> list[sqlite3.Row]:
        """The non-primary rows for the given clusters.

        These are the other documents that reported the same matter. Shown in
        the expanded row so 'one matter' never means 'we threw evidence away'.
        """
        keys = [k for k in keys if k]
        if not keys:
            return []
        placeholders = ",".join("?" * len(keys))
        return list(self._conn.execute(
            f"""
            SELECT p.cluster_key, p.score, i.source_id, i.source_url,
                   i.title AS item_title, e.case_caption, e.event_date
            FROM prospect p
            JOIN extraction e ON e.item_uid = p.item_uid
            JOIN item i ON i.item_uid = p.item_uid
            WHERE p.cluster_key IN ({placeholders})
              AND p.is_primary = 0
              AND e.is_excluded = 0
            ORDER BY p.score DESC
            """,
            list(keys),
        ))

    def set_extraction_excluded(self, item_uid: str, reason: str) -> None:
        """Mark a stored extraction out of scope after a re-screen."""
        with self._lock:
            self._conn.execute(
                "UPDATE extraction SET is_excluded=1, "
                "excluded_reason_late=? WHERE item_uid=?",
                (reason, item_uid),
            )

    def recent_items(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM item ORDER BY observed_at DESC LIMIT ?", (limit,)
        ))

    # -- NY eTrack enrollment (Phase 6) ------------------------------------

    def upsert_enrollment(
        self, *, index_number: str, caption: str = "", court: str = "",
        county: str = "", reason: str = "", score_hint: float = 0.0,
    ) -> bool:
        """Add a candidate. Returns True only if it is genuinely new.

        Never downgrades an existing row's status: a case a human already
        enrolled must not be pushed back to 'candidate' because the ranker
        re-suggested it.
        """
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO etrack_enrollment (index_number, caption, court,
                    county, status, reason, score_hint, added_at)
                VALUES (?, ?, ?, ?, 'candidate', ?, ?, ?)
                ON CONFLICT(index_number) DO UPDATE SET
                    caption    = COALESCE(NULLIF(excluded.caption,''),
                                          etrack_enrollment.caption),
                    court      = COALESCE(NULLIF(excluded.court,''),
                                          etrack_enrollment.court),
                    county     = COALESCE(NULLIF(excluded.county,''),
                                          etrack_enrollment.county),
                    reason     = excluded.reason,
                    score_hint = MAX(excluded.score_hint,
                                     etrack_enrollment.score_hint)
                """,
                (index_number, caption, court, county, reason, score_hint,
                 utcnow()),
            )
            # rowcount is 1 for both INSERT and the DO UPDATE path, so ask the
            # table whether this row predates the call.
            row = self._conn.execute(
                "SELECT added_at FROM etrack_enrollment WHERE index_number=?",
                (index_number,),
            ).fetchone()
            return bool(cur.rowcount) and row is not None

    def mark_enrolled(self, index_number: str) -> None:
        """A human says they submitted the UCS form. Not proof -- see
        confirm_enrollment for that."""
        with self._lock:
            self._conn.execute(
                "UPDATE etrack_enrollment SET status='enrolled', enrolled_at=? "
                "WHERE index_number=? AND status='candidate'",
                (utcnow(), index_number),
            )

    def confirm_enrollment(self, index_number: str) -> bool:
        """An alert arrived. This is the ONLY proof enrollment worked.

        Returns True the first time a given index number is confirmed.
        """
        with self._lock:
            now = utcnow()
            cur = self._conn.execute(
                "UPDATE etrack_enrollment "
                "SET status='confirmed', confirmed_at=COALESCE(confirmed_at,?), "
                "    last_alert_at=?, alert_count=alert_count+1 "
                "WHERE index_number=? AND status!='confirmed'",
                (now, now, index_number),
            )
            if cur.rowcount:
                return True
            # Already confirmed: still record the alert.
            self._conn.execute(
                "UPDATE etrack_enrollment SET last_alert_at=?, "
                "alert_count=alert_count+1 WHERE index_number=?",
                (now, index_number),
            )
            return False

    def enrollments(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return list(self._conn.execute(
                "SELECT * FROM etrack_enrollment WHERE status=? "
                "ORDER BY score_hint DESC, added_at DESC",
                (status,),
            ))
        return list(self._conn.execute(
            "SELECT * FROM etrack_enrollment "
            "ORDER BY CASE status WHEN 'confirmed' THEN 0 WHEN 'enrolled' "
            "THEN 1 WHEN 'candidate' THEN 2 ELSE 3 END, score_hint DESC"
        ))

    # -- claims-agent routing (Phase 5) ------------------------------------

    def claims_assignments(self, limit: int = 500) -> list[sqlite3.Row]:
        """The chapter 11 census: which agent was retained in which case."""
        return list(self._conn.execute(
            """
            SELECT json_extract(payload_json, '$.court')          AS court,
                   json_extract(payload_json, '$.case_number')    AS case_number,
                   json_extract(payload_json, '$.debtor')         AS debtor,
                   json_extract(payload_json, '$.agent_raw')      AS agent_raw,
                   json_extract(payload_json, '$.vendor_id')      AS vendor_id,
                   json_extract(payload_json, '$.vendor_name')    AS vendor_name,
                   json_extract(payload_json, '$.agent_case_url') AS agent_case_url,
                   json_extract(payload_json, '$.date_filed')     AS date_filed,
                   source_url
            FROM item
            WHERE source_id IN ('claims_routing', 'claims_stretto')
              AND json_extract(payload_json, '$.record_kind') = 'claims_assignment'
            ORDER BY date_filed DESC NULLS LAST, case_number DESC
            LIMIT ?
            """,
            (limit,),
        ))

    def claims_vendor_counts(self) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            """
            SELECT json_extract(payload_json, '$.vendor_id')   AS vendor_id,
                   json_extract(payload_json, '$.vendor_name') AS vendor_name,
                   COUNT(*)                                    AS n
            FROM item
            WHERE source_id IN ('claims_routing', 'claims_stretto')
              AND json_extract(payload_json, '$.record_kind') = 'claims_assignment'
            GROUP BY vendor_id
            ORDER BY n DESC
            """
        ))

    def claims_unmapped(self) -> list[sqlite3.Row]:
        """Agent names no routing-table alias matched.

        These are kept, never dropped. An unrecognized vendor is either a new
        entrant or a rename, and both are things you want to be told about.
        """
        return list(self._conn.execute(
            """
            SELECT DISTINCT
                   json_extract(payload_json, '$.agent_raw') AS agent_raw,
                   json_extract(payload_json, '$.court')     AS court,
                   COUNT(*) OVER (
                       PARTITION BY json_extract(payload_json, '$.agent_raw')
                   ) AS n
            FROM item
            WHERE source_id IN ('claims_routing', 'claims_stretto')
              AND json_extract(payload_json, '$.vendor_id') = 'unmapped'
            ORDER BY n DESC
            """
        ))

    # -- delivery ----------------------------------------------------------

    def source_rows(self) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT source_id, display_name, tier, status, base_confidence, "
            "enabled, health, health_note, last_success_at, "
            "consecutive_failures FROM source ORDER BY source_id"
        ))

    def pipeline_counts(self) -> dict[str, int]:
        """Funnel counts, so an empty dashboard explains itself.

        Without these, "0 prospects" is ambiguous between "nothing was
        collected", "everything screened out", and "extraction has not run".
        Those need different responses, so the dashboard shows all four
        numbers rather than only the last.
        """
        def one(sql: str) -> int:
            row = self._conn.execute(sql).fetchone()
            return int(row[0] or 0)

        return {
            "items": one("SELECT COUNT(*) FROM item"),
            "screened_out": one("SELECT COUNT(*) FROM screen_result"),
            "excluded": one(
                "SELECT COUNT(*) FROM screen_result WHERE verdict='excluded'"
            ),
            "no_signal": one(
                "SELECT COUNT(*) FROM screen_result WHERE verdict='no_signal'"
            ),
            "extracted": one("SELECT COUNT(*) FROM extraction"),
            "extraction_excluded": one(
                "SELECT COUNT(*) FROM extraction WHERE is_excluded=1"
            ),
            "ranked": one("SELECT COUNT(*) FROM prospect"),
            "awaiting_extraction": one(
                "SELECT COUNT(*) FROM item i "
                "LEFT JOIN extraction e ON e.item_uid = i.item_uid "
                "LEFT JOIN screen_result s ON s.item_uid = i.item_uid "
                "WHERE e.item_uid IS NULL AND s.item_uid IS NULL"
            ),
        }

    def items_by_source(self) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT source_id, COUNT(*) n, MAX(published_at) newest "
            "FROM item GROUP BY source_id ORDER BY n DESC"
        ))

    def last_run(self) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT run_id, started_at, finished_at, status, purpose "
            "FROM run ORDER BY started_at DESC LIMIT 1"
        ).fetchone()

    def count_items(self, source_id: str | None = None) -> int:
        if source_id:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM item WHERE source_id=?", (source_id,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM item").fetchone()
        return int(row[0])
