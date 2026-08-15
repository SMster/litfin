"""Storage-layer tests.

The atomicity test is the correctness backbone: items and watermark advance in
one transaction, so at-least-once delivery plus idempotent writes gives
exactly-once effect. It is the property most easily broken by a later
refactor, so it is pinned here.
"""

from __future__ import annotations

import sqlite3

import pytest

from litfin.net.ratelimit import rate_key_for
from litfin.store.db import Database, Item, make_item_uid


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def _item(key: str, source: str = "s1") -> Item:
    return Item(source_id=source, natural_key=key, title=f"t-{key}", body="b")


class TestIdempotency:
    def test_item_uid_is_deterministic(self):
        assert make_item_uid("s", "k") == make_item_uid("s", "k")
        assert make_item_uid("s", "k") != make_item_uid("s", "k2")

    def test_redoing_a_task_inserts_nothing_new(self, db):
        """At-least-once delivery + idempotent writes = exactly-once effect."""
        items = [_item("a"), _item("b")]
        first = db.commit_task(
            run_id="r1", task_id="t1", source_id="s1", task_key="t1",
            items=items, watermark_value="w1", seen_keys=["a", "b"],
            rows_parsed=2, rows_new=2,
        )
        assert first == 2

        second = db.commit_task(
            run_id="r1", task_id="t1", source_id="s1", task_key="t1",
            items=items, watermark_value="w1", seen_keys=["a", "b"],
            rows_parsed=2, rows_new=2,
        )
        assert second == 0, "redo must be a no-op"
        assert db.count_items("s1") == 2


class TestWatermarkInvariant:
    def test_watermark_advances_with_items(self, db):
        db.commit_task(
            run_id="r1", task_id="t1", source_id="s1", task_key="t1",
            items=[_item("a")], watermark_value="2026-01-01",
            seen_keys=["a"], rows_parsed=1, rows_new=1,
        )
        value, seen = db.get_watermark("s1", "t1")
        assert value == "2026-01-01"
        assert "a" in seen

    def test_broken_task_does_not_advance_watermark(self, db):
        """A BROKEN canary must NOT advance the watermark.

        Otherwise the data the parser failed to read is skipped forever once
        the parser is fixed.
        """
        db.commit_task(
            run_id="r1", task_id="t1", source_id="s1", task_key="t1",
            items=[_item("a")], watermark_value="2026-01-01",
            seen_keys=["a"], rows_parsed=1, rows_new=1,
        )
        db.commit_task(
            run_id="r2", task_id="t1", source_id="s1", task_key="t1",
            items=[], watermark_value="2026-06-06", seen_keys=["zzz"],
            rows_parsed=0, rows_new=0, status="BROKEN",
            error="selector stopped matching",
        )
        value, seen = db.get_watermark("s1", "t1")
        assert value == "2026-01-01", "watermark moved on a BROKEN task"
        assert "zzz" not in seen

    def test_rollback_leaves_no_partial_state(self, db):
        """A crash mid-commit must roll back items AND watermark together.

        This is the invariant the whole correctness argument rests on: the
        watermark can never advance past durably-stored items. Simulated by
        proxying the connection and failing the watermark write specifically,
        AFTER the item insert has already happened inside the transaction.
        """
        db.commit_task(
            run_id="r1", task_id="t1", source_id="s1", task_key="t1",
            items=[_item("a")], watermark_value="wm1", seen_keys=["a"],
            rows_parsed=1, rows_new=1,
        )
        before_count = db.count_items("s1")
        before_wm, _ = db.get_watermark("s1", "t1")

        class FailingWatermarkConn:
            """Forwards everything, but blows up on the watermark write."""

            def __init__(self, real):
                self._real = real

            def execute(self, sql, *a, **k):
                if sql.strip().upper().startswith("INSERT INTO WATERMARK"):
                    raise sqlite3.OperationalError("simulated crash mid-commit")
                return self._real.execute(sql, *a, **k)

            def __getattr__(self, name):
                return getattr(self._real, name)

        real = db._conn
        db._conn = FailingWatermarkConn(real)
        try:
            with pytest.raises(sqlite3.OperationalError):
                db.commit_task(
                    run_id="r2", task_id="t2", source_id="s1", task_key="t1",
                    items=[_item("b")], watermark_value="wm2",
                    seen_keys=["a", "b"], rows_parsed=1, rows_new=1,
                )
        finally:
            db._conn = real

        assert db.count_items("s1") == before_count, (
            "an item survived a rolled-back transaction -- the atomicity "
            "invariant in commit_task() is broken"
        )
        after_wm, _ = db.get_watermark("s1", "t1")
        assert after_wm == before_wm, "watermark advanced despite a rollback"


class TestRateKeyAggregation:
    def test_all_sec_hosts_share_one_bucket(self):
        """SEC's fair-access cap applies to the REQUESTER, not per-hostname.

        Bucketing per-hostname would let three connectors each run at the cap
        simultaneously and get the whole IP banned.
        """
        for host in ("www.sec.gov", "efts.sec.gov", "data.sec.gov"):
            assert rate_key_for(f"https://{host}/x") == "sec.gov"

    def test_all_claims_agents_share_one_bucket(self):
        for host in ("dm.epiq11.com", "cases.stretto.com", "veritaglobal.net"):
            assert rate_key_for(f"https://{host}/x") == "claims-agent"

    def test_uscourts_subdomains_map_to_one_key(self):
        for host in ("www.deb.uscourts.gov", "www.nysb.uscourts.gov",
                     "www.jpml.uscourts.gov"):
            assert rate_key_for(f"https://{host}/x") == "uscourts.gov"

    def test_unknown_host_gets_restrictive_default(self):
        """A new connector must not be able to be rude by accident."""
        from litfin.net.ratelimit import rate_for

        key = rate_key_for("https://brand-new-host.example.org/x")
        assert key == "_default"
        assert rate_for(key).rps <= 0.25
