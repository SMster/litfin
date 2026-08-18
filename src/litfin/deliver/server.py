"""A local-only control panel over the pipeline.

`litfin serve` gives you the dashboard with buttons: re-screen, re-rank with
different weights, regenerate the digest, collect a finished extraction batch.
It is the same rendered dashboard the static file produces, with a control
panel grafted on, so there is one renderer and no chance of the two disagreeing.

FOUR SAFETY PROPERTIES, none of them optional:

1. **Loopback by default, and leaving loopback is gated.** The default bind is
   127.0.0.1. `--host` exists for hosted deployments, and any non-loopback
   value REFUSES TO START without `LITFIN_WEB_USER`, `LITFIN_WEB_PASSWORD`
   (16+ chars) and `LITFIN_SESSION_SECRET` — checked before the socket is
   created, because a misconfigured hosted panel that boots successfully looks
   exactly like a working one. This process can rescore a prospect list, spend
   money on an API, and display case analysis about named parties in real
   litigation. `--read-only` removes every action that spends or fetches, and
   enforces it server-side rather than by hiding buttons.

2. **CSRF token on every mutating request.** Loopback is not a security
   boundary — any page in your browser can POST to http://127.0.0.1:8788. The
   server mints a random token per start, embeds it in the page it serves, and
   rejects a POST without it. A hostile page can issue the request but cannot
   read the token to include.

3. **Spending is opt-in per action, per click.** `screen`, `rank` and the
   dashboard/digest renderers are free and always available. `run` costs rate
   budget and `extract` costs real money, so both require the panel's typed
   confirmation and are labelled with what they cost.

4. **Jobs run in a background thread, one at a time.** A `run` takes minutes
   because the rate limits are conservative; holding the HTTP request open for
   that would hang the browser and invite a double-submit. The UI polls.
"""

from __future__ import annotations

import io
import json
import logging
import secrets
import threading
import traceback
import webbrowser
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from ..config import Config
from ..store.db import Database
from . import auth as auth_mod
from . import dashboard, dataset, digest as digest_mod, mailer

log = logging.getLogger("litfin.deliver.server")

LOOPBACK = "127.0.0.1"

# Jobs a READ-ONLY instance still permits. Everything else either spends money
# on the Anthropic API or reaches out to a third-party site under our declared
# purpose, and neither belongs on a box other people can reach.
_READ_ONLY_JOBS = frozenset({"rank", "dashboard", "digest", "screen"})


@dataclass
class Job:
    name: str
    status: str = "running"          # running | ok | failed
    started_at: str = ""
    finished_at: str = ""
    log: list[str] = field(default_factory=list)
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "log": self.log[-400:],
            "error": self.error,
        }


class JobRunner:
    """One job at a time, with its stdout captured for the UI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Job | None = None
        self._history: list[Job] = []

    @property
    def busy(self) -> bool:
        return self._current is not None and self._current.status == "running"

    def snapshot(self) -> dict[str, Any]:
        job = self._current
        return {
            "busy": self.busy,
            "current": job.to_json() if job else None,
            "history": [j.name for j in self._history[-8:]],
        }

    def start(self, name: str, fn: Callable[[Job], None]) -> tuple[bool, str]:
        with self._lock:
            if self.busy:
                return False, f"'{self._current.name}' is still running."
            job = Job(
                name=name,
                started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            self._current = job
            self._history.append(job)

        def target() -> None:
            buf = io.StringIO()
            try:
                # Capture whatever the pipeline prints so the panel shows the
                # same report text the CLI would have shown.
                with redirect_stdout(buf), redirect_stderr(buf):
                    fn(job)
                job.status = "ok"
            except Exception as exc:                      # noqa: BLE001
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.log.append(traceback.format_exc())
                log.exception("job %s failed", name)
            finally:
                text = buf.getvalue().strip()
                if text:
                    job.log = text.splitlines() + job.log
                job.finished_at = datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                )

        threading.Thread(target=target, daemon=True, name=f"litfin-{name}").start()
        return True, "started"


# ---------------------------------------------------------------------------
# The control panel markup, grafted onto the dashboard by render_page().
# ---------------------------------------------------------------------------

_PANEL_CSS = """
<style>
#panel { padding: 13px 22px; border-bottom: 1px solid var(--line);
         background: var(--panel); }
#panel h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em;
            color: var(--muted); margin: 0 0 9px; }
#panel .grp { display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
              margin-bottom: 9px; }
#panel .cost { font-size: 11px; color: var(--warn); }
#weights { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; }
#weights label { font-size: 11px; color: var(--muted); display: block; }
#weights input { width: 62px; }
#joblog { font: 11.5px ui-monospace, Consolas, monospace; white-space: pre-wrap;
          background: var(--bg); border: 1px solid var(--line); border-radius: 5px;
          padding: 9px; max-height: 220px; overflow: auto; margin-top: 8px; }
#jobstate { font-size: 12px; color: var(--muted); }
a.btnlink { display: inline-flex; align-items: center; height: 30px;
            font: inherit; padding: 0 11px; border: 1px solid var(--line);
            border-radius: 5px; background: var(--chip); color: var(--fg);
            text-decoration: none; cursor: pointer; }
a.btnlink:hover { background: var(--row-hover); }
</style>
"""


def _panel_html(cfg: Config, token: str, read_only: bool = False) -> str:
    weights_inputs = "".join(
        f'<div><label for="w-{k}">{k}</label>'
        f'<input type="number" id="w-{k}" data-w="{k}" step="0.05" min="0" '
        f'value="{v}"></div>'
        for k, v in _default_weights().items()
    )
    spend_group = "" if read_only else """
  <div class="grp">
    <button data-job="run" data-confirm="run">Fetch new data</button>
    <span class="cost">spends request budget; several minutes</span>
    <button data-job="extract" data-confirm="extract">Extract (Opus)</button>
    <span class="cost">COSTS MONEY — submits a Batches job over screened
      candidates</span>
  </div>"""
    if read_only:
        spend_group = ("""
  <div class="grp"><span class="cost">READ-ONLY instance — fetching and
    extraction are refused by the server. Run them where the pipeline
    lives.</span></div>""")
    heading = "Controls — read only" if read_only else "Controls — local only"
    return f"""{_PANEL_CSS}
<div id="panel" data-token="{token}">
  <h2>{heading}</h2>

  <div class="grp">
    <button data-job="screen">Screen</button>
    <button data-job="rank" class="primary">Re-rank</button>
    <button data-job="dashboard">Write static dashboard</button>
    <button data-job="digest">Render digest (dry run)</button>
    <button data-job="collect">Collect extraction batch</button>
    <a href="/export.xlsx" class="btnlink" download>Export to Excel</a>
    <span class="cost">these are free — no API calls, no fetches</span>
  </div>

  {spend_group}

  <details>
    <summary>Score weights — re-rank without re-extracting</summary>
    <div id="weights" style="margin-top:9px">
      {weights_inputs}
      <button id="apply-weights" class="primary">Apply &amp; re-rank</button>
      <button id="reset-weights">Defaults</button>
    </div>
    <div class="sub" style="margin-top:6px">
      Rescoring reads only stored extractions — seconds, and zero API calls.
    </div>
  </details>

  <div id="jobstate">idle</div>
  <div id="joblog" class="hide"></div>
  <div class="sub" style="margin-top:6px">
    Digest send gate: <b>{'OPEN' if cfg.send_enabled else 'CLOSED'}</b> ·
    allowlist {', '.join(cfg.recipient_allowlist) or '(empty)'} ·
    the button above always renders to disk and never transmits.
  </div>
</div>
"""


_PANEL_JS = r"""
(function () {
  const panel = document.getElementById('panel');
  if (!panel) return;
  const token = panel.dataset.token;
  const state = document.getElementById('jobstate');
  const logEl = document.getElementById('joblog');
  let poll = null;

  const CONFIRM = {
    run: 'Fetch new data from all sources? This spends request budget and '
       + 'takes several minutes. Type RUN to confirm.',
    extract: 'Submit screened candidates to Opus for extraction? THIS COSTS '
       + 'MONEY. Type EXTRACT to confirm.',
  };

  async function post(path, body) {
    const r = await fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-LitFin-Token': token},
      body: JSON.stringify(body || {}),
    });
    return r.json();
  }

  function showJob(j) {
    if (!j) { state.textContent = 'idle'; return; }
    state.textContent = j.name + ' — ' + j.status
      + (j.error ? ' — ' + j.error : '');
    logEl.classList.remove('hide');
    logEl.textContent = (j.log || []).join('\n');
    logEl.scrollTop = logEl.scrollHeight;
  }

  function startPolling() {
    if (poll) return;
    poll = setInterval(async () => {
      const s = await (await fetch('/api/jobs')).json();
      showJob(s.current);
      if (!s.busy) {
        clearInterval(poll); poll = null;
        // The dataset on the page is now stale; reload so the table, the
        // funnel counts and the banners all reflect what just happened.
        setTimeout(() => location.reload(), 700);
      }
    }, 900);
  }

  panel.addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-job]');
    if (!btn) return;
    const job = btn.dataset.job;
    const needs = btn.dataset.confirm;
    if (needs) {
      const want = needs.toUpperCase();
      if (prompt(CONFIRM[needs]) !== want) { state.textContent =
        'cancelled — confirmation did not match'; return; }
    }
    state.textContent = 'starting ' + job + '…';
    const res = await post('/api/job/' + job, {});
    if (!res.ok) { state.textContent = res.error || 'refused'; return; }
    startPolling();
  });

  document.getElementById('apply-weights').addEventListener('click', async () => {
    const weights = {};
    document.querySelectorAll('#weights input[data-w]').forEach(i => {
      const v = parseFloat(i.value);
      if (!isNaN(v)) weights[i.dataset.w] = v;
    });
    state.textContent = 'rescoring…';
    const res = await post('/api/job/rank', {weights});
    if (!res.ok) { state.textContent = res.error || 'refused'; return; }
    startPolling();
  });

  document.getElementById('reset-weights').addEventListener('click', async () => {
    const d = await (await fetch('/api/weights')).json();
    document.querySelectorAll('#weights input[data-w]').forEach(i => {
      i.value = d.defaults[i.dataset.w];
    });
  });
})();
"""


def _default_weights() -> dict[str, float]:
    from ..score.scoring import DEFAULT_WEIGHTS

    return dict(DEFAULT_WEIGHTS)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def _job_screen(cfg: Config, db: Database) -> Callable[[Job], None]:
    def fn(job: Job) -> None:
        from ..extract.runner import select_candidates

        candidates, report = select_candidates(db, cfg)
        print(report.to_markdown())
        print()
        print(f"Top {min(15, len(candidates))} by signal strength:")
        for c in candidates[:15]:
            print(f"  [{c.strength:.2f}] {c.thesis:22} {c.title[:70]}")
    return fn


def _job_rank(cfg: Config, db: Database, weights: dict | None) -> Callable[[Job], None]:
    def fn(job: Job) -> None:
        from ..score.scoring import rank_all

        report = rank_all(db, weights=weights or None,
                          limit=cfg.top_n_dashboard, cfg=cfg)
        if weights:
            print("weights: " + ", ".join(f"{k}={v}" for k, v in sorted(weights.items())))
        print(report.to_markdown())
        print("\nRescored from stored data. No API calls.")
    return fn


def _job_dashboard(cfg: Config, db: Database) -> Callable[[Job], None]:
    def fn(job: Job) -> None:
        path = dashboard.write(db, cfg)
        print(f"Wrote {path}")
    return fn


def _job_digest(cfg: Config, db: Database) -> Callable[[Job], None]:
    def fn(job: Job) -> None:
        data = dataset.load(db, cfg)
        d = digest_mod.render(
            data,
            top_n=cfg.top_n_email,
            dashboard_url=(cfg.data_root / "dashboard.html").as_uri(),
        )
        # dry_run is not parameterized here on purpose: there is no live-send
        # button, so the server cannot be the thing that mails a prospect list.
        result = mailer.send(d, cfg, dry_run=True)
        print(result.to_markdown())
    return fn


def _job_collect(cfg: Config, db: Database) -> Callable[[Job], None]:
    def fn(job: Job) -> None:
        from ..extract.runner import collect_batch

        open_batches = db.open_batches()
        if not open_batches:
            print("No open batches to collect.")
            return
        total = 0
        for bid in open_batches:
            n = collect_batch(bid, cfg, db, wait=False)
            print(f"batch {bid}: stored {n}")
            total += n
        print(f"Collected {total} extractions. Re-rank to see them.")
    return fn


def _job_extract(cfg: Config, db: Database) -> Callable[[Job], None]:
    def fn(job: Job) -> None:
        from ..extract.runner import select_candidates, submit_batch

        candidates, report = select_candidates(db, cfg)
        print(report.to_markdown())
        if not candidates:
            print("\nNothing to extract.")
            return
        bid = submit_batch(candidates, cfg, db)
        print(f"\nSubmitted batch {bid} with {len(candidates)} requests.")
        print("Collect it with the 'Collect extraction batch' button once it "
              "has finished processing.")
    return fn


def _job_run(cfg: Config, db: Database) -> Callable[[Job], None]:
    def fn(job: Job) -> None:
        from ..connectors import (
            courtlistener, doj_cases, edgar_index, feeds, govinfo, sec_fts,
            state_ag,
        )
        from ..net.budget import GlobalBudget
        from ..net.client import PoliteClient
        from ..runner.orchestrator import Orchestrator
        from ..store.artifacts import ArtifactStore

        budget = GlobalBudget(
            db.conn, max_per_day=cfg.max_requests_per_day,
            warn_at_fraction=cfg.warn_at_fraction,
        )
        client = PoliteClient(cfg, budget=budget)
        artifacts = ArtifactStore(cfg.raw_dir, cfg.manifest_dir)
        try:
            connectors = [
                *feeds.all_connectors(),
                doj_cases.build(),
                sec_fts.build(lookback_days=2),
                courtlistener.build_search(lookback_days=3),
                edgar_index.build(lookback_days=2),
                state_ag.build(),
                govinfo.build(lookback_days=2),
            ]
            report = Orchestrator(cfg, db, client, artifacts).run(connectors)
            print(report.to_markdown())
        finally:
            client.close()
    return fn


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "LitFin"
    sys_version = ""

    # injected by serve()
    cfg: Config
    db: Database
    runner: JobRunner
    token: str
    auth: auth_mod.AuthConfig
    throttle: auth_mod.LoginThrottle
    read_only: bool = False

    def log_message(self, fmt: str, *args: Any) -> None:      # quieter
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- authentication -----------------------------------------------------

    def _client_ip(self) -> str:
        fwd = self.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0]

    def _session_ok(self) -> bool:
        """True when the caller may see anything at all.

        With auth unconfigured this returns True -- that is the loopback
        default, and `require_for_public_bind` has already refused to start on
        a public interface in that state, so an unauthenticated server can
        only exist on 127.0.0.1.
        """
        if not self.auth.enabled:
            return True
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == auth_mod.SESSION_COOKIE:
                return auth_mod.verify_session(self.auth, value)
        return False

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _do_login(self) -> None:
        import urllib.parse

        ip = self._client_ip()
        wait = self.throttle.locked_out(ip)
        if wait:
            self._html(auth_mod.login_page(
                f"Too many attempts. Try again in {int(wait / 60) + 1} minute(s)."
            ), 429)
            return

        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, 4096)).decode("utf-8", errors="replace")
        form = urllib.parse.parse_qs(raw)
        user = (form.get("username") or [""])[0]
        password = (form.get("password") or [""])[0]

        if not auth_mod.check_credentials(self.auth, user, password):
            self.throttle.record_failure(ip)
            # Deliberately does not say WHICH field was wrong.
            log.warning("failed login from %s", ip)
            self._html(auth_mod.login_page("Incorrect user or password."), 401)
            return

        self.throttle.clear(ip)
        token = auth_mod.issue_session(self.auth)
        self.send_response(303)
        self.send_header("Location", "/")
        # Secure is set unconditionally: a hosted panel must be behind HTTPS,
        # and a cookie that would travel in cleartext should simply not be
        # accepted by the browser.
        self.send_header(
            "Set-Cookie",
            f"{auth_mod.SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; "
            f"Secure; Path=/; Max-Age={auth_mod.SESSION_TTL_SECONDS}",
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- helpers -----------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # This page embeds a CSRF token. Nothing should frame it or sniff it.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _html(self, text: str, code: int = 200) -> None:
        self._send(code, text.encode("utf-8"), "text/html; charset=utf-8")

    def _authorized(self) -> bool:
        """CSRF check. Loopback is not a security boundary."""
        if self.headers.get("X-LitFin-Token", "") != self.token:
            return False
        # A cross-origin form POST cannot set a custom header, but check the
        # Origin too where the browser sends one -- defense in depth is cheap.
        origin = self.headers.get("Origin")
        if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
            return False
        return True

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except (ValueError, json.JSONDecodeError):
            return {}

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:                                  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        # /healthz stays open so a load balancer can probe it without a
        # credential. It reveals nothing but liveness and the declared purpose.
        if path == "/healthz":
            self._json({"ok": True, "purpose": str(self.cfg.purpose)})
            return
        if path == "/login":
            self._html(auth_mod.login_page())
            return
        if not self._session_ok():
            self._redirect("/login")
            return

        try:
            if path == "/":
                data = dataset.load(self.db, self.cfg)
                self._html(dashboard.render(
                    data,
                    panel_html=_panel_html(self.cfg, self.token, self.read_only),
                    panel_js=_PANEL_JS,
                ))
            elif path == "/api/data":
                self._json(dataset.to_json(dataset.load(self.db, self.cfg)))
            elif path == "/api/jobs":
                self._json(self.runner.snapshot())
            elif path == "/api/weights":
                self._json({"defaults": _default_weights()})
            elif path == "/digest":
                data = dataset.load(self.db, self.cfg)
                d = digest_mod.render(
                    data, top_n=self.cfg.top_n_email,
                    dashboard_url="/",
                )
                self._html(d.html)
            elif path == "/digest.txt":
                data = dataset.load(self.db, self.cfg)
                d = digest_mod.render(data, top_n=self.cfg.top_n_email)
                self._send(200, d.text.encode("utf-8"), "text/plain; charset=utf-8")
            elif path == "/export.xlsx":
                import tempfile
                from pathlib import Path as _Path

                from . import excel

                data = dataset.load(self.db, self.cfg)
                with tempfile.TemporaryDirectory() as td:
                    out = excel.build(
                        data, _Path(td) / "litfin.xlsx"
                    )
                    body = out.read_bytes()
                stamp = data.generated_at[:10]
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet",
                )
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="litfin-prospects-{stamp}.xlsx"',
                )
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/healthz":
                self._json({"ok": True, "purpose": str(self.cfg.purpose)})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:                               # noqa: BLE001
            log.exception("GET %s failed", path)
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:                                 # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")

        if path == "/login":
            self._do_login()
            return
        if not self._session_ok():
            self._json({"ok": False, "error": "not signed in"}, 401)
            return
        if not self._authorized():
            self._json({"ok": False, "error": "bad or missing CSRF token"}, 403)
            return
        if not path.startswith("/api/job/"):
            self._json({"ok": False, "error": "not found"}, 404)
            return

        name = path.rsplit("/", 1)[-1]

        # A read-only instance serves the dashboard and the export and refuses
        # everything that spends money or reaches out to a third-party site.
        # Enforced HERE rather than by hiding the buttons: a hidden button is
        # a UI convenience, not a control.
        if self.read_only and name not in _READ_ONLY_JOBS:
            self._json({
                "ok": False,
                "error": f"'{name}' is disabled on a read-only instance. "
                         f"Run it where the pipeline lives.",
            }, 403)
            return
        body = self._body()
        builders: dict[str, Callable[[], Callable[[Job], None]]] = {
            "screen":    lambda: _job_screen(self.cfg, self.db),
            "rank":      lambda: _job_rank(self.cfg, self.db, body.get("weights")),
            "dashboard": lambda: _job_dashboard(self.cfg, self.db),
            "digest":    lambda: _job_digest(self.cfg, self.db),
            "collect":   lambda: _job_collect(self.cfg, self.db),
            "extract":   lambda: _job_extract(self.cfg, self.db),
            "run":       lambda: _job_run(self.cfg, self.db),
        }
        if name not in builders:
            self._json({"ok": False, "error": f"unknown job {name!r}"}, 404)
            return

        ok, msg = self.runner.start(name, builders[name]())
        self._json({"ok": ok, "error": "" if ok else msg}, 200 if ok else 409)


def serve(
    cfg: Config, db: Database, *, port: int = 8788, open_browser: bool = True,
    host: str = LOOPBACK, read_only: bool = False,
) -> None:
    """Run the control panel until interrupted.

    `host` defaults to loopback and a non-loopback value is refused unless
    authentication is fully configured -- see `auth.require_for_public_bind`.
    The check runs BEFORE the socket is created, so a misconfigured hosted
    panel fails to start rather than coming up open.
    """
    auth_cfg = auth_mod.from_environment()
    auth_mod.require_for_public_bind(auth_cfg, host)   # raises AuthMisconfigured

    token = secrets.token_urlsafe(24)
    runner = JobRunner()

    handler = type(
        "BoundHandler", (_Handler,),
        {
            "cfg": cfg, "db": db, "runner": runner, "token": token,
            "auth": auth_cfg, "throttle": auth_mod.LoginThrottle(),
            "read_only": read_only,
        },
    )

    # ThreadingHTTPServer so a polling /api/jobs request is never blocked
    # behind a page render.
    httpd = ThreadingHTTPServer((host, port), handler)
    shown = LOOPBACK if host in ("0.0.0.0", "::") else host
    url = f"http://{shown}:{port}/"
    print(f"LitFin control panel: {url}")
    if host == LOOPBACK:
        print("Bound to loopback only. Ctrl-C to stop.")
    else:
        print(f"Bound to {host} — authentication REQUIRED and configured.")
        print("Put HTTPS in front of this. Session cookies are set Secure, "
              "so sign-in will not work over plain HTTP.")
    if read_only:
        print("READ-ONLY: run/extract/collect are refused by the server, not "
              "merely hidden.")
    if open_browser and host == LOOPBACK:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
