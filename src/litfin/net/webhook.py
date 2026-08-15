"""CourtListener webhook receiver.

THE ONE JOB: return 2xx in under a second and write the payload down.
Nothing is parsed, scored, or extracted inline.

That is not fastidiousness, it is the documented contract. CourtListener
requires a 2xx within ONE SECOND, retries up to 7 times with exponential
backoff over roughly 54 hours, and AUTO-DISABLES the endpoint after 8
consecutive failures -- after which deliveries stop entirely until someone
re-enables it by hand. Doing real work in the handler converts one slow
database write into a silent, self-inflicted outage.

So: validate, insert one row, respond. `litfin webhook --drain` does the
actual processing out of band.

SECURITY -- read this before exposing the port.

CourtListener webhooks carry NO HMAC SIGNATURE. Their own documentation says
so plainly: there is no way to cryptographically verify that a POST came from
them. That leaves exactly two mitigations, and this module implements both:

  1. IP allowlist -- 34.210.230.218 and 54.189.59.91.
  2. A long random secret in the URL path, compared in constant time.

Treat every payload as untrusted input regardless. It is attacker-controllable
in principle, so the drain step must never eval, exec, or interpolate payload
content into a query. The `webhook_event` table stores it as opaque JSON text.
"""

from __future__ import annotations

import hmac
import json
import logging
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..store.db import Database

log = logging.getLogger("litfin.webhook")

# Published source IPs for CourtListener webhook deliveries.
ALLOWED_IPS: frozenset[str] = frozenset({"34.210.230.218", "54.189.59.91"})

# Refuse anything implausible before reading the body.
MAX_BODY_BYTES = 2_000_000

# Webhook event_type values (integers) from the CourtListener webhook API.
EVENT_TYPES = {
    1: "DOCKET_ALERT",
    2: "SEARCH_ALERT",
    3: "RECAP_FETCH",
    4: "OLD_DOCKET_ALERTS_REPORT",
    5: "PRAY_AND_PAY",
}


@dataclass(slots=True)
class WebhookConfig:
    secret_path: str
    db_path: str
    host: str = "0.0.0.0"
    port: int = 8787
    enforce_ip_allowlist: bool = True


class _Handler(BaseHTTPRequestHandler):
    server_version = "LitFinWebhook/1.0"
    # Suppress the default stderr access log; we log deliberately instead.
    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- helpers ----------------------------------------------------------

    @property
    def _cfg(self) -> WebhookConfig:
        return self.server.litfin_config  # type: ignore[attr-defined]

    @property
    def _db(self) -> Database:
        return self.server.litfin_db  # type: ignore[attr-defined]

    def _drain_body(self) -> None:
        """Read and discard the request body before an early rejection.

        BUG PINNED: the 404/403/400 paths used to answer without ever reading
        the body. Closing a socket that still has unread data in its receive
        buffer makes the OS send an RST rather than a FIN -- on Windows the
        caller then sees ConnectionAborted instead of the 403 the server
        actually sent, and the response is lost in transit.

        That matters here beyond tidiness: CourtListener auto-disables an
        endpoint after 8 consecutive failures, and a delivery that reads as a
        connection reset is a failure, while a clean 403 is an answer.

        Bounded by MAX_BODY_BYTES so a rejected caller cannot make us read an
        unbounded stream -- the point is to close politely, not to accept the
        payload.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        remaining = min(max(length, 0), MAX_BODY_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _respond(self, code: int, body: str = "") -> None:
        if code >= 400:
            self._drain_body()
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _client_ip(self) -> str:
        # Behind a reverse proxy the real source is in X-Forwarded-For. The
        # LEFTMOST entry is the original client; anything after it was added
        # by intermediaries. Only trust this if your proxy strips inbound
        # copies of the header -- otherwise a caller can forge it.
        fwd = self.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0]

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._respond(200, "ok")
            return
        self._respond(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        cfg = self._cfg

        # 1. Secret path, compared in constant time so the comparison cannot
        #    be used as an oracle to recover the secret byte by byte.
        expected = f"/webhook/{cfg.secret_path}"
        if not hmac.compare_digest(self.path, expected):
            log.warning("webhook: bad path from %s", self._client_ip())
            self._respond(404, "not found")
            return

        # 2. IP allowlist. With no HMAC available this is half the available
        #    authentication.
        ip = self._client_ip()
        if cfg.enforce_ip_allowlist and ip not in ALLOWED_IPS:
            log.warning("webhook: rejected source IP %s", ip)
            self._respond(403, "forbidden")
            return

        # 3. Bounded read.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._respond(400, "bad content-length")
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._respond(400, "bad content-length")
            return
        raw = self.rfile.read(length)

        # 4. Idempotency. Retries re-deliver the same event with the same key,
        #    so this is what stops a 54-hour retry window from writing the
        #    same delivery eight times.
        key = (self.headers.get("Idempotency-Key") or "").strip()
        if not key:
            # Fall back to a content hash so a missing header cannot create
            # unbounded duplicates.
            import hashlib
            key = "sha256:" + hashlib.sha256(raw).hexdigest()

        event_type = None
        try:
            body = json.loads(raw)
            event_type = (body.get("webhook") or {}).get("event_type")
        except (json.JSONDecodeError, AttributeError):
            # Store it anyway -- an unparsable payload from an allowlisted IP
            # is itself worth seeing, and discarding it would hide a real
            # format change.
            pass

        try:
            fresh = self._db.enqueue_webhook(
                idempotency_key=key,
                payload=raw.decode("utf-8", errors="replace"),
                event_type=event_type,
                remote_addr=ip,
            )
        except Exception:
            # Even on a storage failure, answer 2xx. CourtListener will retry,
            # and a 5xx here counts toward the 8 failures that auto-disable the
            # endpoint. Losing one delivery beats losing the endpoint.
            log.exception("webhook: enqueue failed for key %s", key[:24])
            self._respond(200, "accepted")
            return

        log.info(
            "webhook: %s event_type=%s from %s",
            "stored" if fresh else "duplicate",
            EVENT_TYPES.get(event_type or -1, event_type),
            ip,
        )
        self._respond(200, "ok")


def serve(cfg: WebhookConfig) -> None:
    """Run the receiver. Blocks."""
    db = Database(Path(cfg.db_path))
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), _Handler)
    httpd.litfin_config = cfg      # type: ignore[attr-defined]
    httpd.litfin_db = db           # type: ignore[attr-defined]
    httpd.daemon_threads = True

    log.info(
        "webhook receiver listening on %s:%s at /webhook/<secret> "
        "(IP allowlist %s)",
        cfg.host, cfg.port,
        "ON" if cfg.enforce_ip_allowlist else "OFF -- development only",
    )
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        db.close()


# ---------------------------------------------------------------------------
# Out-of-band processing
# ---------------------------------------------------------------------------

def drain(db: Database, *, limit: int = 200) -> dict[str, int]:
    """Process stored deliveries. Runs OUTSIDE the request path.

    Deliberately conservative: a DOCKET_ALERT payload tells us a subscribed
    docket moved. We record that fact and let the normal search/extract
    pipeline pick up the detail, rather than trusting an unsigned payload to
    carry authoritative case data.
    """
    stats = {"processed": 0, "docket_alerts": 0, "errors": 0, "skipped": 0}

    for row in db.unprocessed_webhooks(limit=limit):
        key = row["idempotency_key"]
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            db.mark_webhook_processed(key, error="unparsable JSON payload")
            stats["errors"] += 1
            continue

        event_type = (payload.get("webhook") or {}).get("event_type")
        if event_type == 1:  # DOCKET_ALERT
            docket_id = _docket_id_from(payload)
            if docket_id is not None:
                db.touch_alert(docket_id)
                stats["docket_alerts"] += 1
            else:
                stats["skipped"] += 1
        else:
            stats["skipped"] += 1

        db.mark_webhook_processed(key)
        stats["processed"] += 1

    return stats


def _docket_id_from(payload: dict) -> int | None:
    """Pull a docket id out of a DOCKET_ALERT payload, defensively.

    The payload is unsigned and its exact shape may change, so every access is
    guarded and a miss is a skip rather than a crash.
    """
    data = payload.get("payload") or {}
    for candidate in (
        data.get("docket"),
        (data.get("docket") or {}).get("id") if isinstance(data.get("docket"), dict) else None,
        data.get("docket_id"),
    ):
        if isinstance(candidate, int):
            return candidate
    results = data.get("results")
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            for k in ("docket_id", "docket"):
                v = first.get(k)
                if isinstance(v, int):
                    return v
    return None
