"""Hosting: authentication, the read-only boundary, and the preflight gate.

Hosting changes the security surface and the legal posture at the same time,
and both fail SILENTLY. An unauthenticated panel boots fine. A source used
outside its permitted purpose returns 200. Every test here exists because the
failure it describes would otherwise look like success.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from litfin.config import Config, Identity, is_placeholder_contact
from litfin.deliver import auth
from litfin.deploy import preflight
from litfin.deploy.preflight import Level


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

GOOD = auth.AuthConfig(username="op", password="p" * 20, secret="s" * 40)


class TestPublicBindGate:
    def test_loopback_needs_no_auth(self):
        """The local default. Unauthenticated is correct on your own machine
        and this must keep working."""
        auth.require_for_public_bind(auth.AuthConfig("", "", ""), "127.0.0.1")
        auth.require_for_public_bind(auth.AuthConfig("", "", ""), "localhost")

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5", "example.com"])
    def test_any_public_bind_without_auth_refuses(self, host):
        with pytest.raises(auth.AuthMisconfigured):
            auth.require_for_public_bind(auth.AuthConfig("", "", ""), host)

    def test_partial_config_still_refuses(self):
        """Two of three is not authentication."""
        for cfg in (
            auth.AuthConfig("op", "", "s" * 40),
            auth.AuthConfig("", "p" * 20, "s" * 40),
            auth.AuthConfig("op", "p" * 20, ""),
        ):
            with pytest.raises(auth.AuthMisconfigured):
                auth.require_for_public_bind(cfg, "0.0.0.0")

    def test_short_password_refuses(self):
        """A hosted panel that spends money should not be behind a password
        somebody typed in a hurry."""
        weak = auth.AuthConfig("op", "hunter2", "s" * 40)
        with pytest.raises(auth.AuthMisconfigured, match="minimum"):
            auth.require_for_public_bind(weak, "0.0.0.0")

    def test_short_session_secret_refuses(self):
        with pytest.raises(auth.AuthMisconfigured, match="SESSION_SECRET"):
            auth.require_for_public_bind(
                auth.AuthConfig("op", "p" * 20, "short"), "0.0.0.0"
            )

    def test_fully_configured_passes(self):
        auth.require_for_public_bind(GOOD, "0.0.0.0")

    def test_the_check_runs_before_the_socket(self):
        """A misconfigured hosted panel must fail to start, not come up open:
        one that boots successfully looks exactly like a working one."""
        src = Path(
            __import__("litfin.deliver.server", fromlist=["x"]).__file__
        ).read_text(encoding="utf-8")
        gate = src.index("require_for_public_bind")
        socket = src.index("ThreadingHTTPServer((host, port)")
        assert gate < socket


class TestSessions:
    def test_round_trip(self):
        assert auth.verify_session(GOOD, auth.issue_session(GOOD))

    def test_tampered_payload_rejected(self):
        token = auth.issue_session(GOOD)
        body, _, sig = token.rpartition(".")
        assert not auth.verify_session(GOOD, f"{body}x.{sig}")

    def test_signature_from_another_secret_rejected(self):
        token = auth.issue_session(GOOD)
        other = auth.AuthConfig("op", "p" * 20, "z" * 40)
        assert not auth.verify_session(other, token)

    def test_rotating_the_secret_invalidates_every_session(self):
        """That is the point of having a session secret."""
        token = auth.issue_session(GOOD)
        rotated = auth.AuthConfig(GOOD.username, GOOD.password, "n" * 40)
        assert not auth.verify_session(rotated, token)

    def test_expiry_is_checked_even_though_the_signature_is_valid(self):
        """A valid signature is not a valid session, or an old cookie replays
        forever."""
        token = auth.issue_session(GOOD)
        later = time.time() + auth.SESSION_TTL_SECONDS + 1
        assert not auth.verify_session(GOOD, token, now=later)

    def test_a_different_username_rejected(self):
        token = auth.issue_session(GOOD)
        renamed = auth.AuthConfig("someone_else", GOOD.password, GOOD.secret)
        assert not auth.verify_session(renamed, token)

    @pytest.mark.parametrize("junk", ["", "x", "....", "a.b", "notatoken"])
    def test_garbage_never_raises(self, junk):
        assert auth.verify_session(GOOD, junk) is False

    def test_signature_is_checked_before_the_payload_is_parsed(self):
        """Never parse a payload the server did not sign."""
        src = Path(auth.__file__).read_text(encoding="utf-8")
        verify = src.index("def verify_session")
        body = src[verify:]
        assert body.index("compare_digest") < body.index("json.loads")


class TestCredentials:
    def test_correct_credentials_accepted(self):
        assert auth.check_credentials(GOOD, "op", "p" * 20)

    def test_wrong_password_rejected(self):
        assert not auth.check_credentials(GOOD, "op", "wrong")

    def test_wrong_username_rejected(self):
        assert not auth.check_credentials(GOOD, "nope", "p" * 20)

    def test_both_fields_compared_in_constant_time(self):
        """Comparing the username with a plain equality check would leak which
        usernames exist, one character of timing at a time.

        Inspects the STATEMENTS, not the source text — the docstring names the
        wrong approach in order to warn against it, and a naive string search
        would flag that as the violation.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(auth.check_credentials))
        fn = tree.body[0]
        body = [n for n in fn.body if not isinstance(n, ast.Expr)]

        calls = [
            n for n in ast.walk(ast.Module(body=body, type_ignores=[]))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "compare_digest"
        ]
        assert len(calls) == 2, "both fields must be compared in constant time"

        compares = [
            n for n in ast.walk(ast.Module(body=body, type_ignores=[]))
            if isinstance(n, ast.Compare)
            and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in n.ops)
        ]
        assert not compares, "no plain equality comparison in the credential path"


class TestLoginThrottle:
    def test_lockout_after_repeated_failures(self):
        """A private URL and a password is not a defence against guessing at
        network speed."""
        t = auth.LoginThrottle()
        assert t.locked_out("1.2.3.4") == 0
        for _ in range(auth.MAX_ATTEMPTS):
            t.record_failure("1.2.3.4")
        assert t.locked_out("1.2.3.4") > 0

    def test_lockout_is_per_ip(self):
        t = auth.LoginThrottle()
        for _ in range(auth.MAX_ATTEMPTS):
            t.record_failure("1.2.3.4")
        assert t.locked_out("5.6.7.8") == 0

    def test_success_clears_the_counter(self):
        t = auth.LoginThrottle()
        for _ in range(auth.MAX_ATTEMPTS - 1):
            t.record_failure("1.2.3.4")
        t.clear("1.2.3.4")
        assert t.locked_out("1.2.3.4") == 0

    def test_lockout_expires(self):
        t = auth.LoginThrottle()
        now = time.time()
        for _ in range(auth.MAX_ATTEMPTS):
            t.record_failure("1.2.3.4", now=now)
        assert t.locked_out("1.2.3.4", now=now) > 0
        assert t.locked_out("1.2.3.4", now=now + auth.LOCKOUT_SECONDS + 1) == 0


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def _hosted_ready(**kw) -> Config:
    base = dict(
        identity=Identity(contact_email="ops@realdomain.example"),
        courtlistener_scope_resolved="emailed FLP 2026-08-20, confirmed",
    )
    base.update(kw)
    return Config(**base)


def _names(report) -> str:
    return " | ".join(c.name for c in report.checks)


class TestPreflightCourtListenerScope:
    def test_hosted_deploy_is_BLOCKED_while_the_scope_is_unresolved(self, monkeypatch):
        """The central check. Free Law Project bars building 'tools for
        for-profit or non-profit organizations' — a laptop clears that, an
        always-on host serving a team is arguable, and code cannot decide."""
        monkeypatch.setenv("LITFIN_WEB_USER", "op")
        monkeypatch.setenv("LITFIN_WEB_PASSWORD", "p" * 20)
        monkeypatch.setenv("LITFIN_SESSION_SECRET", "s" * 40)

        r = preflight.run(
            Config(identity=Identity(contact_email="ops@realdomain.example")),
            hosted=True,
        )
        assert not r.ok
        blocked = [c for c in r.failures if "CourtListener scope" in c.name]
        assert blocked, _names(r)
        assert "partnerships@free.law" in blocked[0].fix

    def test_recording_an_answer_unblocks_it(self, monkeypatch):
        monkeypatch.setenv("LITFIN_WEB_USER", "op")
        monkeypatch.setenv("LITFIN_WEB_PASSWORD", "p" * 20)
        monkeypatch.setenv("LITFIN_SESSION_SECRET", "s" * 40)

        r = preflight.run(_hosted_ready(), hosted=True)
        assert r.ok, _names(r) + " || " + "; ".join(c.detail for c in r.failures)

    def test_local_use_is_not_blocked_by_it(self):
        """A laptop clears the clause. Preflight must not cry wolf locally."""
        r = preflight.run(
            Config(identity=Identity(contact_email="ops@realdomain.example")),
            hosted=False,
        )
        assert r.ok

    def test_commercial_purpose_removes_the_question_by_disabling_the_source(self):
        """Flipping purpose is the honest alternative: the gate disables
        CourtListener loudly rather than using it under stale terms."""
        from litfin.compliance.status import Purpose

        r = preflight.run(
            Config(
                purpose=Purpose.COMMERCIAL,
                identity=Identity(contact_email="ops@realdomain.example"),
            ),
            hosted=False,
        )
        scope = [c for c in r.checks if "CourtListener scope" in c.name]
        assert not [c for c in scope if c.level is Level.FAIL]
        assert any("No RESEARCH_ONLY source is enabled" in c.name for c in r.checks)


class TestPreflightSafety:
    def test_placeholder_contact_blocks_a_deploy(self):
        """An unedited placeholder is worse than declaring nothing: it looks
        like a real contact and reaches no one."""
        r = preflight.run(Config(identity=Identity(contact_email="you@example.com")),
                          hosted=False)
        assert [c for c in r.failures if "placeholder" in c.name.lower()]

    @pytest.mark.parametrize("email", [
        "you@example.com", "ops@example.invalid", "", "  ", "x@example.com",
    ])
    def test_placeholder_detection(self, email):
        assert is_placeholder_contact(email)

    def test_a_real_address_passes(self):
        # A real-looking address on a real domain. Deliberately NOT the
        # operator's actual mailbox: this file is committed to a public repo,
        # which is the same reason the contact address lives in .env rather
        # than in litfin.toml.
        assert not is_placeholder_contact("ops@somefirm.com")
        assert not is_placeholder_contact("first.last@gmail.com")

    def test_missing_web_auth_blocks_a_hosted_deploy(self, monkeypatch):
        for k in ("LITFIN_WEB_USER", "LITFIN_WEB_PASSWORD", "LITFIN_SESSION_SECRET"):
            monkeypatch.delenv(k, raising=False)
        r = preflight.run(_hosted_ready(), hosted=True)
        assert [c for c in r.failures if "authentication" in c.name.lower()]

    def test_send_enabled_without_smtp_blocks(self, monkeypatch):
        """Otherwise a scheduled digest fails silently every week."""
        for k in ("LITFIN_SMTP_HOST", "LITFIN_SMTP_FROM"):
            monkeypatch.delenv(k, raising=False)
        r = preflight.run(
            _hosted_ready(send_enabled=True, recipient_allowlist=("a@b.invalid",)),
            hosted=False,
        )
        assert [c for c in r.failures if "SMTP" in c.name]

    def test_send_enabled_with_empty_allowlist_blocks(self, monkeypatch):
        monkeypatch.setenv("LITFIN_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("LITFIN_SMTP_FROM", "a@b.invalid")
        r = preflight.run(_hosted_ready(send_enabled=True), hosted=False)
        assert [c for c in r.failures if "allowlist" in c.name.lower()]

    def test_dry_run_send_is_a_warning_not_a_failure(self):
        r = preflight.run(_hosted_ready(), hosted=False)
        dry = [c for c in r.checks if "DRY RUN" in c.name]
        assert dry and dry[0].level is Level.WARN

    def test_machine_assisted_review_is_surfaced(self):
        """Stretto is enabled on a model's contract read. Hosting is exactly
        when somebody should confirm it."""
        r = preflight.run(_hosted_ready(), hosted=False)
        assert [c for c in r.checks if "machine-assisted" in c.name]

    def test_relative_data_root_blocks_a_hosted_deploy(self, monkeypatch):
        monkeypatch.setenv("LITFIN_WEB_USER", "op")
        monkeypatch.setenv("LITFIN_WEB_PASSWORD", "p" * 20)
        monkeypatch.setenv("LITFIN_SESSION_SECRET", "s" * 40)
        r = preflight.run(_hosted_ready(data_root=Path("data")), hosted=True)
        assert [c for c in r.failures if "data_root" in c.name]

    def test_report_exit_semantics(self):
        r = preflight.run(Config(identity=Identity(contact_email="you@example.com")),
                          hosted=False)
        assert not r.ok
        assert "Not safe to deploy" in r.to_text()


# ---------------------------------------------------------------------------
# Deployment assets
# ---------------------------------------------------------------------------

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


class TestDeploymentAssets:
    def test_the_tuesday_digest_is_scheduled(self):
        cron = (DEPLOY / "crontab").read_text(encoding="utf-8")
        line = [l for l in cron.splitlines()
                if l.strip() and not l.startswith("#") and "digest" in l]
        assert len(line) == 1
        # minute hour dom month dow=2 (Tuesday)
        fields = line[0].split()
        assert fields[4] == "2", f"not Tuesday: {line[0]}"
        assert "--send" in line[0]

    def test_weekly_sources_run_before_the_tuesday_digest(self):
        """Otherwise the weekly email omits the weekly sources."""
        cron = (DEPLOY / "crontab").read_text(encoding="utf-8")
        weekly = next(l for l in cron.splitlines()
                      if "run --weekly" in l and not l.startswith("#"))
        assert weekly.split()[4] == "1", "weekly sources should run Monday"

    def test_compose_runs_preflight_before_anything_else(self):
        compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
        assert "litfin\", \"preflight" in compose or "preflight" in compose
        assert compose.count("service_completed_successfully") >= 2

    def test_the_web_service_is_read_only(self):
        """Spending money and fetching third-party sites belong to the
        scheduler, which nothing can reach over the network."""
        compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
        web = compose[compose.index("  web:"):compose.index("  webhook:")]
        assert "--read-only" in web

    def test_ports_bind_to_host_loopback(self):
        """Session cookies are Secure; these must sit behind a TLS proxy."""
        compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
        for line in compose.splitlines():
            if line.strip().startswith("- \"") and ":" in line and "8" in line:
                assert "127.0.0.1:" in line, line

    def test_no_secret_values_are_committed_in_compose(self):
        compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
        for key in ("ANTHROPIC_API_KEY", "LITFIN_WEB_PASSWORD",
                    "LITFIN_SMTP_PASSWORD", "LITFIN_SESSION_SECRET"):
            assert f"{key}: ${{{key}" in compose, key

    def test_data_is_a_named_volume_not_an_image_layer(self):
        compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
        assert "litfin-data:/data" in compose
        dockerfile = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
        assert "LITFIN_DATA_ROOT=/data" in dockerfile

    def test_container_runs_unprivileged(self):
        dockerfile = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
        assert "useradd" in dockerfile

    def test_scheduler_exports_env_for_cron(self):
        """cron does not inherit the container environment. Without this every
        job runs with no API key and fails in a way that looks like a code
        problem rather than a config one."""
        sh = (DEPLOY / "entrypoint-scheduler.sh").read_text(encoding="utf-8")
        assert "printenv" in sh and "BASH_ENV" in sh

    def test_no_scheduled_job_discards_its_errors(self):
        """A job that fails silently looks exactly like a quiet week."""
        cron = (DEPLOY / "crontab").read_text(encoding="utf-8")
        for line in cron.splitlines():
            if line.strip().startswith(("#", "SHELL", "PATH")) or not line.strip():
                continue
            assert "2>&1" in line, line
            assert "2>/dev/null" not in line, line


# ---------------------------------------------------------------------------
# Publishing the dashboard bundle
# ---------------------------------------------------------------------------

class TestPublishGuard:
    """The bundle names real parties in real litigation, carries damages
    estimates, and describes how their cases might be monetized. Publishing it
    to an open host is the one irreversible mistake available here."""

    @pytest.mark.parametrize("target", [
        "smster.github.io",
        "https://user.github.io/litfin",
        "raw.githubusercontent.com/x/y",
        "my-bucket.s3.amazonaws.com",
        "storage.googleapis.com/bucket",
        "litfin.surge.sh",
    ])
    def test_public_by_default_hosts_are_refused(self, target):
        from litfin.deploy import publish

        with pytest.raises(publish.UnprotectedTarget):
            publish.assert_target_protected(target, protected_by="whatever")

    def test_refusal_says_WHY_that_host_is_public(self):
        """A generic 'unsafe' teaches nothing. Naming the mechanism is what
        stops the next person reaching for the same host."""
        from litfin.deploy import publish

        with pytest.raises(publish.UnprotectedTarget, match="GitHub Pages"):
            publish.assert_target_protected("x.github.io", protected_by="y")

    def test_protected_by_is_required(self):
        """The value of the check is that publishing requires SAYING what
        protects it -- which is the moment somebody notices nothing does."""
        from litfin.deploy import publish

        with pytest.raises(publish.UnprotectedTarget, match="protected-by"):
            publish.assert_target_protected("litfin.pages.dev", protected_by="")
        with pytest.raises(publish.UnprotectedTarget):
            publish.assert_target_protected("litfin.pages.dev", protected_by="   ")

    def test_a_named_protection_on_a_neutral_host_passes(self):
        from litfin.deploy import publish

        publish.assert_target_protected(
            "litfin.pages.dev", protected_by="Cloudflare Access, 2 emails"
        )


class TestPublishBundle:
    def _bundle(self, tmp_path):
        from litfin.config import Config
        from litfin.deploy import publish
        from litfin.store.db import Database

        cfg = Config(data_root=tmp_path / "data")
        cfg.ensure_dirs()
        db = Database(cfg.db_path)
        try:
            return publish.build(
                db, cfg, tmp_path / "out",
                protected_by="Cloudflare Access, 2 emails",
            )
        finally:
            db.close()

    def test_bundle_contains_a_self_contained_dashboard(self, tmp_path):
        b = self._bundle(tmp_path)
        index = b.directory / "index.html"
        assert index.is_file()
        html = index.read_text(encoding="utf-8")
        assert "<script src=" not in html, "must need no network to render"

    def test_bundle_ships_no_control_panel(self, tmp_path):
        """A static bundle must not show buttons that cannot work -- and must
        certainly not show ones that spend money."""
        html = (self._bundle(tmp_path).directory / "index.html").read_text(
            encoding="utf-8"
        )
        assert 'id="panel"' not in html
        assert "data-job=" not in html

    def test_bundle_asks_not_to_be_indexed(self, tmp_path):
        b = self._bundle(tmp_path)
        assert "Disallow: /" in (b.directory / "robots.txt").read_text()
        headers = (b.directory / "_headers").read_text()
        assert "noindex" in headers

    def test_manifest_records_what_protects_it(self, tmp_path):
        import json

        b = self._bundle(tmp_path)
        m = json.loads((b.directory / "manifest.json").read_text())
        assert m["protected_by"] == "Cloudflare Access, 2 emails"
        assert m["fetches_anything"] is False
        assert "Confidential" in m["contains"]

    def test_rebuild_clears_stale_rows(self, tmp_path):
        """A stale row from a previous publish is worse than no row: it looks
        current and is not."""
        b = self._bundle(tmp_path)
        stale = b.directory / "stale-from-last-week.html"
        stale.write_text("old", encoding="utf-8")
        b2 = self._bundle(tmp_path)
        assert not stale.exists()
        assert (b2.directory / "index.html").is_file()

    def test_the_hosted_artifact_fetches_nothing(self, tmp_path):
        """The whole reason this deployment shape sidesteps the source-terms
        question: nothing on the host talks to a third party."""
        import json

        b = self._bundle(tmp_path)
        assert json.loads((b.directory / "manifest.json").read_text())[
            "fetches_anything"
        ] is False


# ---------------------------------------------------------------------------
# Render blueprint
# ---------------------------------------------------------------------------

class TestRenderBlueprint:
    """Render attaches a persistent disk to exactly ONE service, so the
    compose layout (scheduler / web / webhook sharing a volume) does not map
    onto it. Everything touching /data has to be one process tree."""

    def _blueprint(self):
        import yaml

        return yaml.safe_load((DEPLOY / "render.yaml").read_text(encoding="utf-8"))

    def test_exactly_one_service_owns_the_disk(self):
        svcs = self._blueprint()["services"]
        with_disk = [s for s in svcs if "disk" in s]
        assert len(with_disk) == 1, "a Render disk attaches to one service only"
        assert with_disk[0]["disk"]["mountPath"] == "/data"

    def test_pinned_to_a_single_instance(self):
        """SQLite has one writer. Scaling past 1 would corrupt the invariant
        that items and watermarks advance in a single transaction."""
        svc = self._blueprint()["services"][0]
        assert svc.get("numInstances", 1) == 1

    def test_not_on_the_free_plan(self):
        """Free instances have no persistent disk and spin down. The service
        would come up, serve the dashboard, and silently lose the corpus on
        the first idle timeout."""
        assert self._blueprint()["services"][0]["plan"] != "free"

    def test_no_secret_has_a_literal_value(self):
        """render.yaml is committed to a public repo."""
        svc = self._blueprint()["services"][0]
        for env in svc["envVars"]:
            if "value" not in env:
                continue
            key = env["key"]
            assert not any(
                marker in key
                for marker in ("PASSWORD", "SECRET", "API_KEY", "TOKEN")
            ), f"{key} carries a literal value in a committed file"

    def test_credentials_are_prompted_or_generated(self):
        svc = self._blueprint()["services"][0]
        by_key = {e["key"]: e for e in svc["envVars"]}
        for key in ("ANTHROPIC_API_KEY", "LITFIN_SMTP_PASSWORD"):
            assert by_key[key].get("sync") is False, key
        for key in ("LITFIN_WEB_PASSWORD", "LITFIN_SESSION_SECRET"):
            assert by_key[key].get("generateValue") is True, key

    def test_panel_defaults_to_read_only(self):
        """A button on the public internet that spends money on the Anthropic
        API is a bad idea even behind a password."""
        svc = self._blueprint()["services"][0]
        by_key = {e["key"]: e for e in svc["envVars"]}
        assert by_key["LITFIN_WEB_READ_ONLY"]["value"] == "true"

    def test_healthcheck_points_at_the_unauthenticated_route(self):
        """Render's prober has no session. /healthz bypasses auth on purpose
        and reveals only liveness."""
        assert self._blueprint()["services"][0]["healthCheckPath"] == "/healthz"

    def test_entrypoint_gates_on_preflight(self):
        sh = (DEPLOY / "entrypoint-render.sh").read_text(encoding="utf-8")
        assert "litfin preflight" in sh
        assert "exit 1" in sh
        # The gate must precede the server, or it comes up misconfigured.
        assert sh.index("preflight") < sh.index("litfin serve")

    def test_entrypoint_honours_the_injected_port(self):
        """Render assigns $PORT; ignoring it fails the health check."""
        sh = (DEPLOY / "entrypoint-render.sh").read_text(encoding="utf-8")
        assert "${PORT:-8788}" in sh

    def test_entrypoint_exports_env_for_cron(self):
        sh = (DEPLOY / "entrypoint-render.sh").read_text(encoding="utf-8")
        assert "printenv" in sh and "BASH_ENV" in sh
