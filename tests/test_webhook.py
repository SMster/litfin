"""Webhook receiver and docket-alert tests.

The receiver's contract is unusually strict and unusually easy to break:
CourtListener requires a 2xx within ONE SECOND, retries for ~54 hours, and
auto-disables the endpoint after 8 consecutive failures. Several tests here
exist specifically to stop a future refactor from moving work into the
request path.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from litfin.net import webhook
from litfin.store.db import Database


@pytest.fixture()
def server(tmp_path):
    db = Database(tmp_path / "wh.db")
    cfg = webhook.WebhookConfig(
        secret_path="test-secret-abc123",
        db_path=str(tmp_path / "wh.db"),
        host="127.0.0.1",
        port=0,                      # ephemeral
        enforce_ip_allowlist=False,  # loopback is not in the allowlist
    )
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), webhook._Handler)
    httpd.litfin_config = cfg
    httpd.litfin_db = db
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    port = httpd.server_address[1]
    yield f"http://127.0.0.1:{port}", cfg, db
    httpd.shutdown()
    httpd.server_close()
    db.close()


def _post(base: str, path: str, body: bytes, key: str | None = None):
    req = urllib.request.Request(f"{base}{path}", data=body, method="POST")
    if key:
        req.add_header("Idempotency-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


DOCKET_ALERT = json.dumps({
    "webhook": {"event_type": 1, "version": 2},
    "payload": {"docket": {"id": 12345}, "results": [{"description": "JUDGMENT"}]},
}).encode()


class TestAuthentication:
    """With NO HMAC available, the secret path and IP allowlist are all there is."""

    def test_rejections_are_delivered_not_reset(self, server):
        """BUG PINNED: the reject paths answered without reading the request
        body. Closing a socket with unread data in its receive buffer makes
        the OS send an RST instead of a FIN, so the caller sees a connection
        reset rather than the 403 the server actually sent.

        It surfaced as a flaky test and it is a real defect: CourtListener
        auto-disables an endpoint after 8 consecutive failures, and a reset
        counts as a failure while a clean 403 counts as an answer.

        Repeated because the race only shows up sometimes.
        """
        base, _, _ = server
        for _ in range(12):
            assert _post(base, "/webhook/wrong-secret", DOCKET_ALERT, "k") == 404

    def test_wrong_secret_is_404(self, server):
        base, _, _ = server
        assert _post(base, "/webhook/wrong-secret", DOCKET_ALERT) == 404

    def test_correct_secret_accepted(self, server):
        base, cfg, _ = server
        assert _post(base, f"/webhook/{cfg.secret_path}", DOCKET_ALERT, "k1") == 200

    def test_secret_compared_in_constant_time(self):
        """Guards against turning the comparison into a byte-by-byte oracle."""
        import inspect
        src = inspect.getsource(webhook._Handler.do_POST)
        assert "compare_digest" in src

    def test_ip_allowlist_enforced_when_on(self, tmp_path):
        db = Database(tmp_path / "ip.db")
        cfg = webhook.WebhookConfig(
            secret_path="s", db_path=str(tmp_path / "ip.db"),
            host="127.0.0.1", port=0, enforce_ip_allowlist=True,
        )
        httpd = ThreadingHTTPServer((cfg.host, 0), webhook._Handler)
        httpd.litfin_config = cfg
        httpd.litfin_db = db
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        try:
            # Loopback is not one of CourtListener's published source IPs.
            assert _post(f"http://127.0.0.1:{port}", "/webhook/s",
                         DOCKET_ALERT, "k") == 403
        finally:
            httpd.shutdown(); httpd.server_close(); db.close()

    def test_published_source_ips(self):
        assert webhook.ALLOWED_IPS == {"34.210.230.218", "54.189.59.91"}


class TestResponseContract:
    def test_responds_well_under_one_second(self, server):
        """The hard requirement. Exceeding it repeatedly disables the endpoint."""
        base, cfg, _ = server
        start = time.monotonic()
        assert _post(base, f"/webhook/{cfg.secret_path}", DOCKET_ALERT, "timed") == 200
        assert time.monotonic() - start < 0.5

    def test_malformed_body_still_returns_200(self, server):
        """A 5xx counts toward the 8 failures that auto-disable the endpoint.

        An unparsable payload from an allowlisted source is also worth SEEING
        -- discarding it would hide a real format change.
        """
        base, cfg, db = server
        assert _post(base, f"/webhook/{cfg.secret_path}", b"not json", "junk") == 200
        assert db.webhook_stats()["total"] >= 1

    def test_receiver_does_not_process_inline(self):
        """Pins the enqueue-only design.

        If a future change calls drain(), scoring, or extraction from the
        handler, one slow write becomes a self-inflicted outage.
        """
        import inspect
        src = inspect.getsource(webhook._Handler.do_POST)
        for forbidden in ("drain(", "rank_all", "extract", "classify_text"):
            assert forbidden not in src


class TestIdempotency:
    def test_duplicate_key_stored_once(self, server):
        """Retries re-deliver the same event for ~54 hours."""
        base, cfg, db = server
        url = f"/webhook/{cfg.secret_path}"
        assert _post(base, url, DOCKET_ALERT, "dupe") == 200
        assert _post(base, url, DOCKET_ALERT, "dupe") == 200
        assert db.webhook_stats()["total"] == 1

    def test_missing_key_falls_back_to_content_hash(self, server):
        """A missing header must not create unbounded duplicates."""
        base, cfg, db = server
        url = f"/webhook/{cfg.secret_path}"
        _post(base, url, DOCKET_ALERT)
        _post(base, url, DOCKET_ALERT)
        assert db.webhook_stats()["total"] == 1


class TestDrain:
    def test_docket_alert_touches_subscription(self, tmp_path):
        db = Database(tmp_path / "d.db")
        try:
            db.record_alert(docket_id=12345, status="active", case_name="Acme")
            db.enqueue_webhook(
                idempotency_key="e1",
                payload=DOCKET_ALERT.decode(),
                event_type=1, remote_addr="34.210.230.218",
            )
            stats = webhook.drain(db)
            assert stats["docket_alerts"] == 1
            assert db.alerts()[0]["last_event_at"] is not None
        finally:
            db.close()

    def test_unparsable_payload_recorded_as_error_not_dropped(self, tmp_path):
        db = Database(tmp_path / "d2.db")
        try:
            db.enqueue_webhook(idempotency_key="bad", payload="{{{",
                               event_type=None, remote_addr="x")
            stats = webhook.drain(db)
            assert stats["errors"] == 1
            assert db.webhook_stats()["pending"] == 0
        finally:
            db.close()

    def test_docket_id_extraction_is_defensive(self):
        """Payloads are unsigned and their shape may change; a miss is a skip."""
        assert webhook._docket_id_from(
            {"payload": {"docket": {"id": 7}}}) == 7
        assert webhook._docket_id_from({"payload": {"docket_id": 9}}) == 9
        assert webhook._docket_id_from(
            {"payload": {"results": [{"docket_id": 11}]}}) == 11
        assert webhook._docket_id_from({}) is None
        assert webhook._docket_id_from({"payload": {"docket": "nonsense"}}) is None


class TestCandidateDockets:
    def test_groups_by_docket_not_collapsed(self, tmp_path):
        """BUG PINNED: aliasing the extracted id as `docket_id` while LEFT
        JOINing docket_alert (which has a real `docket_id` column) made SQLite
        resolve GROUP BY to the joined column -- NULL for every row when no
        alerts exist -- collapsing 96 distinct dockets into one bucket.
        """
        db = Database(tmp_path / "c.db")
        try:
            from litfin.store.db import Item
            items = [
                Item(source_id="courtlistener", natural_key=f"recap-doc:{i}",
                     title=f"Case {i % 3}",
                     payload={"docket_id": 100 + (i % 3), "docket_url": "u"})
                for i in range(9)
            ]
            db.commit_task(
                run_id="r", task_id="t", source_id="courtlistener",
                task_key="t", items=items, watermark_value=None,
                seen_keys=[], rows_parsed=9, rows_new=9,
            )
            rows = db.candidate_dockets()
            assert len(rows) == 3, "distinct dockets collapsed into one group"
            assert {r["docket_id"] for r in rows} == {100, 101, 102}
            assert all(r["hits"] == 3 for r in rows)
        finally:
            db.close()

    def test_subscribed_dockets_are_excluded(self, tmp_path):
        db = Database(tmp_path / "c2.db")
        try:
            from litfin.store.db import Item
            db.commit_task(
                run_id="r", task_id="t", source_id="courtlistener", task_key="t",
                items=[Item(source_id="courtlistener", natural_key="d1",
                            payload={"docket_id": 500})],
                watermark_value=None, seen_keys=[], rows_parsed=1, rows_new=1,
            )
            assert len(db.candidate_dockets()) == 1
            db.record_alert(docket_id=500, status="active")
            assert len(db.candidate_dockets()) == 0
        finally:
            db.close()
