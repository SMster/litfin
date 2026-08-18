"""The delivery layer: dashboard, digest, mailer send gate, local server.

The send-gate tests are the ones that matter. A dashboard that renders badly
is annoying; a pipeline that mails a litigation-finance prospect list to the
wrong address cannot be un-sent.

The second theme is the honesty requirements. An imputed damages figure that
renders like a stated one, or an empty venue that renders like a quiet one,
are the two ways this dashboard could actively mislead — so both are pinned in
both renderers.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from litfin.config import Config
from litfin.deliver import dashboard, dataset, digest, mailer, server
from litfin.deliver.dataset import (
    ClaimsRow, CourtRow, Dataset, Prospect, SourceRow,
)


def make_prospect(**kw) -> Prospect:
    base = dict(
        rank=1, item_uid="a" * 64, score=0.75,
        caption="Acme Corp v. Widget Inc.", summary="A dispute about widgets.",
        court="S.D.N.Y.", venue="S.D.N.Y.", jurisdiction="federal",
        practice_area="antitrust", deal_thesis="antitrust_followon",
        event_type="settlement_reached", event_date="2026-08-01",
        published_at="2026-08-02", source_id="doj_atr",
        source_url="https://example.invalid/doc",
        damages_usd=40_000_000.0, damages_conf="high", damages_imputed=False,
        damages_basis="settled for $40 million", docket_number="1:26-cv-1234",
        procedural_posture="Settlement reached, approval pending.",
        appeal_status="none", collectability_note="public company defendant",
        defendant_is_public=True, defendant_ticker="WDGT",
        parties_plaintiff=["Acme Corp"], parties_defendant=["Widget Inc."],
        caveats="", extraction_confidence="high", artifact_sha256="",
        components={"thesis_fit": 0.85, "damages": 0.7, "notes": []},
    )
    base.update(kw)
    return Prospect(**base)


def make_dataset(**kw) -> Dataset:
    base = dict(
        generated_at="2026-08-15T12:00:00+00:00",
        purpose="research",
        data_root=r"C:\LitFinData",
        prospects=[make_prospect()],
        courts=[
            CourtRow("nysd", "S.D.N.Y.", "FD", "all", "high"),
            CourtRow("nvd", "D. Nev.", "FD", "", "low"),
        ],
        sources=[
            SourceRow("doj_atr", "DOJ Antitrust", "A", "public_domain_gov",
                      "HEALTHY", "", "2026-08-15T10:00:00+00:00", 0, 120),
        ],
        counts={"items": 1695, "screened_out": 1502, "extracted": 187,
                "ranked": 163, "awaiting_extraction": 0},
        coverage_summary={"high": 118, "partial": 55, "low": 15},
        last_run_id="run_x",
    )
    base.update(kw)
    return Dataset(**base)


# ---------------------------------------------------------------------------
# The send gate
# ---------------------------------------------------------------------------

class TestSendGate:
    def test_dry_run_is_the_default(self):
        """Not a config value that could be absent, mistyped, or overwritten
        — a default on the function signature."""
        import inspect

        sig = inspect.signature(mailer.send)
        assert sig.parameters["dry_run"].default is True

    def test_send_enabled_false_refuses(self):
        cfg = Config(send_enabled=False, recipient_allowlist=("a@b.invalid",))
        with pytest.raises(mailer.SendRefused, match="send_enabled"):
            mailer.check_gate(cfg, ["a@b.invalid"])

    def test_empty_allowlist_refuses_even_when_enabled(self):
        """Two independent conditions: the first catches 'I did not mean to
        send at all', the second catches 'I meant to send, but not there.'"""
        cfg = Config(send_enabled=True, recipient_allowlist=())
        with pytest.raises(mailer.SendRefused, match="allowlist"):
            mailer.check_gate(cfg, ["a@b.invalid"])

    def test_recipient_off_the_allowlist_refuses(self):
        cfg = Config(send_enabled=True, recipient_allowlist=("me@mine.invalid",))
        with pytest.raises(mailer.SendRefused, match="not in"):
            mailer.check_gate(cfg, ["someone@else.invalid"])

    def test_one_bad_recipient_refuses_the_whole_send(self):
        cfg = Config(send_enabled=True, recipient_allowlist=("me@mine.invalid",))
        with pytest.raises(mailer.SendRefused):
            mailer.check_gate(cfg, ["me@mine.invalid", "someone@else.invalid"])

    def test_display_name_cannot_smuggle_an_address_past_the_allowlist(self):
        """Comparing raw strings would let 'Me <evil@x>' slip past an entry
        for 'me@mine.invalid'. It is also how a typo'd address gets through."""
        cfg = Config(send_enabled=True, recipient_allowlist=("me@mine.invalid",))
        mailer.check_gate(cfg, ["Sean Mitchell <ME@Mine.Invalid>"])
        with pytest.raises(mailer.SendRefused):
            mailer.check_gate(cfg, ["me@mine.invalid <evil@elsewhere.invalid>"])

    def test_no_recipients_refuses(self):
        cfg = Config(send_enabled=True, recipient_allowlist=("me@mine.invalid",))
        with pytest.raises(mailer.SendRefused):
            mailer.check_gate(cfg, [])

    def test_fully_open_gate_passes(self):
        cfg = Config(send_enabled=True, recipient_allowlist=("me@mine.invalid",))
        mailer.check_gate(cfg, ["me@mine.invalid"])

    def test_dry_run_writes_files_and_transmits_nothing(self, tmp_path, monkeypatch):
        cfg = Config(data_root=tmp_path)
        cfg.ensure_dirs()

        def explode(*a, **k):                      # pragma: no cover
            raise AssertionError("SMTP must not be touched in a dry run")

        monkeypatch.setattr("smtplib.SMTP", explode)

        d = digest.render(make_dataset(), top_n=5)
        result = mailer.send(d, cfg, dry_run=True, run_id="r1")

        assert result.dry_run is True
        assert result.html_path.read_text(encoding="utf-8")
        assert result.text_path.read_text(encoding="utf-8")

    def test_live_send_refuses_before_opening_a_socket(self, tmp_path, monkeypatch):
        cfg = Config(data_root=tmp_path, send_enabled=False)
        cfg.ensure_dirs()

        def explode(*a, **k):                      # pragma: no cover
            raise AssertionError("gate must refuse before any connection")

        monkeypatch.setattr("smtplib.SMTP", explode)

        d = digest.render(make_dataset(), top_n=5)
        with pytest.raises(mailer.SendRefused):
            mailer.send(d, cfg, dry_run=False, recipients=["a@b.invalid"],
                        run_id="r1")

    def test_a_refusal_raises_rather_than_silently_dry_running(self):
        """A scheduled job that quietly stops sending looks exactly like a
        quiet day — the same failure the canary system exists to prevent on
        the ingestion side."""
        cfg = Config(send_enabled=True, recipient_allowlist=("me@mine.invalid",))
        with pytest.raises(mailer.SendRefused):
            mailer.check_gate(cfg, ["nope@elsewhere.invalid"])


# ---------------------------------------------------------------------------
# Dashboard honesty requirements
# ---------------------------------------------------------------------------

class TestDashboardHonesty:
    def test_imputed_damages_are_labelled_and_never_shown_as_stated(self):
        data = make_dataset(prospects=[
            make_prospect(damages_usd=None, damages_conf="none",
                          damages_imputed=True)
        ])
        html = dashboard.render(data)
        assert "imputed" in html
        payload = _payload(html)
        assert payload["prospects"][0]["imputed"] is True

    def test_imputed_rows_land_in_not_stated_never_a_dollar_band(self):
        """Ranking on a thesis prior is legitimate; letting that prior answer
        'show me anything over $50M' is not. The user asked about STATED
        amounts."""
        assert dataset.size_band(400_000_000, imputed=True) == dataset.NOT_STATED
        assert dataset.size_band(None, imputed=False) == dataset.NOT_STATED
        assert dataset.size_band(0, imputed=False) == dataset.NOT_STATED
        assert dataset.size_band(400_000_000, imputed=False) == "$250M – $1B"

    def test_claim_size_column_never_prints_an_imputed_figure(self):
        data = make_dataset(prospects=[
            make_prospect(damages_usd=None, damages_imputed=True)
        ])
        payload = _payload(dashboard.render(data))
        assert payload["prospects"][0]["band"] == dataset.NOT_STATED
        js = dashboard._JS
        assert "p.imputed || !p.damages" in js

    def test_coverage_map_renders_on_the_same_page_as_results(self):
        """A venue with no feed produces no rows whether or not anything
        happened in it. On another page, that distinction is lost."""
        html = dashboard.render(make_dataset())
        assert 'id="coverage"' in html
        assert "D. Nev." in html
        assert "absence of signal" in html

    def test_dark_venue_count_is_on_the_results_page(self):
        """The count that says how far to trust an empty result sits in the
        coverage card, next to the map it describes."""
        html = dashboard.render(make_dataset())
        card = html.split('id="coverage"')[1].split("</section>")[0]
        assert ">15<" in card
        assert "absence of signal is not absence of activity" in card

    def test_broken_source_is_impossible_to_miss(self):
        """No banner strip any more, so the run stamp carries it: red, counted
        and linked, above everything else on the page."""
        data = make_dataset(sources=[
            SourceRow("sec_fts", "SEC FTS", "A", "public_domain_gov",
                      "BROKEN", "selector changed", "", 3, 0),
        ])
        html = dashboard.render(data)
        head = html.split('id="results"')[0]
        assert 'class="runpill down"' in head
        assert "1 SOURCE DOWN" in head
        assert 'href="#sources"' in head
        # And the card it points at says which one, and why.
        card = html.split('id="sources"')[1]
        assert "BROKEN" in card
        assert "selector changed" in card

    def test_funnel_counts_explain_an_empty_dashboard(self):
        """'0 prospects' is ambiguous between nothing collected, everything
        screened out, and extraction never run — three states needing three
        different responses."""
        data = make_dataset(prospects=[], counts={
            "items": 1695, "screened_out": 1502, "extracted": 0,
            "ranked": 0, "awaiting_extraction": 193,
        })
        html = dashboard.render(data)
        funnel = html.split('class="funnel"')[1].split("</header>")[0]
        assert "1,695" in funnel
        # A backlog is a funnel stage: collected, and unable to appear below.
        assert "193" in funnel
        assert "awaiting extraction" in funnel
        assert "litfin extract" in html

    def test_declared_purpose_is_in_the_header(self):
        html = dashboard.render(make_dataset())
        header = html.split("</header>")[0]
        assert "research" in header


class TestClaimSizeFilter:
    def test_bands_cover_the_whole_range_with_no_gaps(self):
        boundaries = [0, 1, 9_999_999, 10_000_000, 49_999_999, 50_000_000,
                      249_999_999, 250_000_000, 999_999_999, 1_000_000_000,
                      50_000_000_000]
        for amount in boundaries:
            band = dataset.size_band(amount, imputed=False)
            assert band in dataset.BAND_ORDER, amount

    def test_band_boundaries_are_half_open(self):
        """Exactly $50M belongs to one band, not two."""
        assert dataset.size_band(49_999_999, False) == "$10M – $50M"
        assert dataset.size_band(50_000_000, False) == "$50M – $250M"
        assert dataset.size_band(1_000_000_000, False) == "$1B and up"

    def test_not_stated_sorts_last(self):
        """A real figure must never be visually buried under rows that lack
        one."""
        assert dataset.BAND_ORDER[-1] == dataset.NOT_STATED

    def test_no_bands_checked_means_no_size_filter(self):
        js = dashboard._JS
        assert "bands.length === 0 || bands.includes(p.band)" in js

    def test_band_counts_ignore_the_size_filter_itself(self):
        """A count must say what checking that box WOULD return, not what it
        returns given that it is currently unchecked."""
        js = dashboard._JS
        assert "Object.assign({}, f, {bands: []})" in js


class TestJurisdictionFilter:
    def test_federal_districts_normalize_to_one_label(self):
        for venue in ("S.D.N.Y.", "United States District Court for the "
                      "Southern District of New York", "E.D.N.Y."):
            group, label = dataset.normalize_jurisdiction("federal", venue, "")
            assert group == "Federal"
            assert label == "Federal — New York", venue

    def test_state_courts_are_distinguished_from_federal(self):
        group, label = dataset.normalize_jurisdiction(
            "state", "Commercial Division", "Supreme Court of the State of New York"
        )
        assert group == "State"
        assert label == "State — New York"

    def test_delaware_chancery_is_state_not_federal(self):
        group, label = dataset.normalize_jurisdiction(
            "", "", "Court of Chancery of the State of Delaware"
        )
        assert group == "State"
        assert "Delaware" in label

    def test_federal_with_no_identifiable_state_stays_plain_federal(self):
        group, label = dataset.normalize_jurisdiction(
            "federal", "United States District Court", ""
        )
        assert (group, label) == ("Federal", "Federal")

    def test_unknown_is_labelled_unknown_not_guessed(self):
        group, label = dataset.normalize_jurisdiction("", "", "")
        assert group == label == dataset.UNKNOWN_JURISDICTION

    def test_a_bare_state_name_admits_the_court_is_unclear(self):
        """'New York' alone does not say whether it is state or federal, and
        inventing an answer would put rows in the wrong bucket silently."""
        group, label = dataset.normalize_jurisdiction("New York", "", "")
        assert group == dataset.UNKNOWN_JURISDICTION
        assert "court unclear" in label

    def test_detail_dropdown_is_scoped_to_the_selected_group(self):
        js = dashboard._JS
        assert "syncJurisdictionOptions" in js
        assert "!group || p.jgroup === group" in js


class TestRowDescription:
    def test_every_row_gets_a_description_even_with_empty_input(self):
        """A blank cell in the description column defeats the purpose of
        having one, so describe() is total."""
        d = dataset.describe(
            event_type="", deal_thesis="", practice_area="",
            damages_usd=None, damages_imputed=False, venue="", court="",
            defendant_is_public=False,
        )
        assert d and d.endswith(".") and len(d) > 15

    def test_reads_as_english_not_enum_soup(self):
        d = dataset.describe(
            event_type="settlement_reached", deal_thesis="antitrust_followon",
            practice_area="antitrust", damages_usd=400_000_000,
            damages_imputed=False, venue="S.D.N.Y.", court="",
            defendant_is_public=True,
        )
        assert d == (
            "The parties reached a settlement in an antitrust matter in "
            "S.D.N.Y. Stated amount $400M. It could lead to follow-on "
            "antitrust damages. Defendant is a public company."
        )
        for token in ("_", "settlement_reached", "antitrust_followon"):
            assert token not in d

    def test_never_states_an_imputed_amount(self):
        d = dataset.describe(
            event_type="judgment_entered", deal_thesis="judgment_monetization",
            practice_area="commercial", damages_usd=15_000_000,
            damages_imputed=True, venue="D. Del.", court="",
            defendant_is_public=False,
        )
        assert "No amount stated." in d
        assert "15" not in d and "$" not in d

    def test_placeless_court_names_are_dropped_not_echoed(self):
        """'in United States District Court' tells a reader nothing they had
        not already assumed."""
        d = dataset.describe(
            event_type="judgment_entered", deal_thesis="none",
            practice_area="commercial", damages_usd=None,
            damages_imputed=True, venue="United States District Court",
            court="", defendant_is_public=False,
        )
        assert "in United States District Court" not in d

    def test_proposed_judgment_is_not_described_as_entered(self):
        """The Tunney Act distinction survives into the prose. A proposed
        consent decree inside its 60-day comment window is not a decided
        matter, and the description must not imply it is."""
        d = dataset.describe(
            event_type="judgment_proposed", deal_thesis="antitrust_followon",
            practice_area="antitrust", damages_usd=None,
            damages_imputed=True, venue="", court="",
            defendant_is_public=False,
        )
        assert "not yet entered" in d
        assert "A court entered judgment" not in d

    def test_appears_in_the_table_and_in_the_digest(self):
        data = make_dataset()
        html = dashboard.render(data)
        assert 'class="desc"' in html
        assert data.prospects[0].description in html
        d = digest.render(data)
        assert data.prospects[0].description in d.html
        assert data.prospects[0].description in d.text

    def test_classification_tags_move_into_the_expanded_detail(self):
        """They left the table so it could be scannable; they did not vanish."""
        js = dashboard._JS
        assert "add('Classification'" in js
        assert "esc(p.thesis)" in js


class TestDashboardRendering:
    def test_is_self_contained(self):
        """No CDN, no external stylesheet, no remote font. It must render
        from a desktop shortcut on a laptop with no internet."""
        html = dashboard.render(make_dataset())
        assert "<script src=" not in html
        assert "<link rel=\"stylesheet\"" not in html
        for bad in ("cdn.", "googleapis", "unpkg", "jsdelivr", "http://", ):
            assert bad not in html.replace("http://www.w3.org", "")

    def test_embedded_json_cannot_close_the_script_tag(self):
        """A caption containing '</script>' would end the tag early and dump
        the rest of the dataset into the document as markup."""
        data = make_dataset(prospects=[
            make_prospect(caption="Evil </script><script>alert(1)</script> Corp")
        ])
        html = dashboard.render(data)
        script = html.split("const DATA = ")[1].split("</script>")[0]
        assert "</script>" not in script
        payload = json.loads(script.rstrip().rstrip(";").replace("<\\/", "</"))
        assert "Evil" in payload["prospects"][0]["caption"]

    def test_html_in_server_rendered_text_is_escaped(self):
        """Server-rendered sections go through _esc. The JSON payload is
        checked separately — inside <script> it is an inert string literal,
        and the JS escapes it again before it ever reaches innerHTML."""
        data = make_dataset(
            sources=[
                SourceRow("x", "srcname", "A", "s", "HEALTHY",
                          "<img src=x onerror=1>", "", 0, 1),
            ],
            courts=[CourtRow("<b>evil</b>", "D. Test", "FD", "", "low")],
        )
        markup = dashboard.render(data).split("const DATA = ")[0]
        assert "<img src=x onerror=1>" not in markup
        assert "&lt;img src=x onerror=1&gt;" in markup
        assert "<b>evil</b>" not in markup
        assert "&lt;b&gt;evil&lt;/b&gt;" in markup

    def test_js_escapes_every_value_it_writes_into_innerhtml(self):
        js = dashboard._JS
        # Row and detail rendering must never interpolate a raw value.
        assert "esc(p.caption)" in js
        assert "esc(p.summary" in js
        assert "esc(p.url)" in js
        for raw in ("+ p.caption +", "+ p.summary +", "+ p.url +"):
            assert raw not in js

    def test_static_file_has_no_dead_control_buttons(self):
        """The buttons only work behind the local server. Rendering them in
        the file you open from a shortcut would be a lie."""
        html = dashboard.render(make_dataset())
        assert 'id="panel"' not in html
        assert 'data-job=' not in html

    def test_server_panel_is_grafted_onto_the_same_renderer(self):
        html = dashboard.render(
            make_dataset(), panel_html='<div id="panel">x</div>',
            panel_js="var x=1;",
        )
        assert 'id="panel"' in html
        assert "var x=1;" in html

    def test_claims_census_is_separate_from_the_ranked_table(self):
        """Census rows carry no outcome language. Mixing them into the ranked
        table would imply an event where there is none."""
        data = make_dataset(
            claims=[ClaimsRow("ohsb", "26-11937", "Magellan Aerospace",
                              "stretto", "Stretto", "", "2026-07-22")],
            claims_by_vendor=[("stretto", 1)],
        )
        html = dashboard.render(data)
        assert 'id="claims"' in html
        assert "census records, not deal events" in html
        assert "D. Del. is absent" in html

    def test_unmapped_claims_agent_is_surfaced(self):
        data = make_dataset(
            claims=[ClaimsRow("nysb", "24-1", "X", "unmapped", "New Co", "", "")],
            claims_unmapped=[("New Co", "nysb", 1)],
        )
        html = dashboard.render(data)
        assert "unmapped claims agent" in html
        assert "The rows are kept" in html

    def test_write_produces_a_file_and_a_dated_archive(self, tmp_path):
        cfg = Config(data_root=tmp_path)
        cfg.ensure_dirs()

        class FakeDb:
            def pipeline_counts(self): return {"items": 0}
            def items_by_source(self): return []
            def source_rows(self): return []
            def all_court_coverage(self): return []
            def coverage_summary(self): return []
            def top_prospects(self, limit): return []
            def cluster_members(self, keys): return []
            def last_run(self): return None
            def claims_assignments(self, limit): return []
            def claims_vendor_counts(self): return []
            def claims_unmapped(self): return []

        path = dashboard.write(FakeDb(), cfg)
        assert path.is_file()
        archives = list(cfg.runs_dir.glob("*/dashboard.html"))
        assert archives, "a dated archive copy makes a past ranking reviewable"


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------

class TestDigest:
    def test_respects_top_n(self):
        data = make_dataset(prospects=[
            make_prospect(rank=i, item_uid=f"{i:064d}") for i in range(1, 31)
        ])
        d = digest.render(data, top_n=20)
        assert d.html.count("<tr><td") == 20

    def test_imputed_rows_say_no_figure_stated_in_both_formats(self):
        """An email gets forwarded away from its context, so the label has to
        travel with the row, not sit in a footnote."""
        data = make_dataset(prospects=[
            make_prospect(damages_usd=None, damages_imputed=True)
        ])
        d = digest.render(data)
        assert "no figure stated" in d.html
        assert "no figure stated" in d.text

    def test_coverage_warning_travels_with_every_send(self):
        d = digest.render(make_dataset())
        assert "publish no PACER RSS feed" in d.html
        assert "publish no PACER RSS feed" in d.text

    def test_broken_source_warning_is_in_the_digest(self):
        data = make_dataset(sources=[
            SourceRow("sec_fts", "SEC", "A", "s", "BROKEN", "", "", 2, 0),
        ])
        d = digest.render(data)
        assert "NOT healthy" in d.html

    def test_uses_inline_styles_not_a_style_block(self):
        """Gmail strips <style>. Inline styles on tables is ugly and correct."""
        d = digest.render(make_dataset())
        assert "<style" not in d.html
        assert "style=" in d.html

    def test_escapes_html_in_captions(self):
        data = make_dataset(prospects=[
            make_prospect(caption="<script>alert(1)</script>")
        ])
        d = digest.render(make_dataset(prospects=data.prospects))
        assert "<script>alert(1)</script>" not in d.html

    def test_empty_dataset_produces_a_sane_digest(self):
        data = make_dataset(prospects=[], counts={"items": 0, "extracted": 0,
                                                  "ranked": 0})
        d = digest.render(data)
        assert "no ranked prospects" in d.subject.lower()
        assert d.text.strip()

    def test_digest_and_dashboard_rank_identically(self):
        """One assembly step feeds both renderers. An email that disagrees
        with the dashboard about what ranked first, with no way to tell which
        is lying, is the failure this guards."""
        data = make_dataset(prospects=[
            make_prospect(rank=1, item_uid="1" * 64, score=0.9, caption="First"),
            make_prospect(rank=2, item_uid="2" * 64, score=0.5, caption="Second"),
        ])
        d = digest.render(data, top_n=2)
        payload = _payload(dashboard.render(data))
        assert payload["prospects"][0]["caption"] == "First"
        assert d.text.index("First") < d.text.index("Second")


# ---------------------------------------------------------------------------
# Local server
# ---------------------------------------------------------------------------

class TestServer:
    def test_binds_loopback_by_default(self):
        """This process can rescore a prospect list and spend money on an API.
        Loopback is the default; leaving it is gated (see TestHostedAuth)."""
        import inspect

        assert server.LOOPBACK == "127.0.0.1"
        assert (
            inspect.signature(server.serve).parameters["host"].default
            == "127.0.0.1"
        )

    def test_read_only_mode_is_enforced_server_side(self):
        """A hidden button is a UI convenience, not a control. The refusal has
        to live in the request handler."""
        src = Path(server.__file__).read_text(encoding="utf-8")
        assert "if self.read_only and name not in _READ_ONLY_JOBS" in src
        assert server._READ_ONLY_JOBS == {"rank", "dashboard", "digest", "screen"}
        assert "run" not in server._READ_ONLY_JOBS
        assert "extract" not in server._READ_ONLY_JOBS

    def test_job_runner_runs_one_at_a_time(self):
        r = server.JobRunner()
        done = []

        def slow(job):
            import time
            time.sleep(0.25)
            done.append(1)

        ok, _ = r.start("a", slow)
        assert ok
        ok2, msg = r.start("b", slow)
        assert not ok2 and "still running" in msg

    def test_job_failure_is_captured_not_raised(self):
        import time

        r = server.JobRunner()

        def boom(job):
            raise ValueError("nope")

        r.start("boom", boom)
        for _ in range(50):
            if not r.busy:
                break
            time.sleep(0.05)
        snap = r.snapshot()
        assert snap["current"]["status"] == "failed"
        assert "nope" in snap["current"]["error"]

    def test_job_stdout_is_captured_for_the_ui(self):
        import time

        r = server.JobRunner()
        r.start("talk", lambda job: print("hello from the job"))
        for _ in range(50):
            if not r.busy:
                break
            time.sleep(0.05)
        assert "hello from the job" in "\n".join(r.snapshot()["current"]["log"])

    def test_no_live_send_button_exists(self):
        """The server renders the digest to disk and never transmits. It must
        not be possible to mail a prospect list by clicking something."""
        text = open(server.__file__, encoding="utf-8").read()
        assert "dry_run=True" in text
        assert "dry_run=False" not in text

    def test_spending_actions_require_typed_confirmation(self):
        panel = server._panel_html(Config(), "tok")
        assert 'data-job="run" data-confirm="run"' in panel
        assert 'data-job="extract" data-confirm="extract"' in panel
        assert "COSTS MONEY" in panel
        # Free actions must NOT demand a confirmation, or the prompt becomes
        # noise people click through.
        assert 'data-job="screen">' in panel

    def test_panel_shows_the_send_gate_state(self):
        assert "CLOSED" in server._panel_html(Config(), "t")
        assert "OPEN" in server._panel_html(
            Config(send_enabled=True, recipient_allowlist=("a@b.invalid",)), "t"
        )


def _payload(html: str) -> dict:
    raw = html.split("const DATA = ")[1].split("</script>")[0]
    return json.loads(raw.rstrip().rstrip(";").replace("<\\/", "</"))


class TestExportLink:
    """The published bundle ships an .xlsx next to index.html. Without a link
    nothing on the page can reach it — which is exactly what happened on the
    first Cloudflare deploy."""

    def test_no_link_when_no_export_is_alongside(self):
        html = dashboard.render(make_dataset())
        assert 'class="dl"' not in html, "a download button that 404s is worse than none"

    def test_link_appears_when_an_export_ships_with_it(self):
        html = dashboard.render(
            make_dataset(), export_href="litfin-prospects-2026-08-18.xlsx"
        )
        assert 'href="litfin-prospects-2026-08-18.xlsx"' in html
        assert "download" in html
        assert "Excel" in html

    def test_href_is_relative_so_the_secret_path_is_not_baked_in(self):
        html = dashboard.render(make_dataset(), export_href="x.xlsx")
        assert 'href="x.xlsx"' in html
        assert "https://" not in html.split('class="dl"')[0][-200:]

    def test_href_is_escaped(self):
        html = dashboard.render(make_dataset(), export_href='a" onload="evil')
        assert 'onload="evil' not in html


class TestNothingAboveTheResults:
    """The banner strip is gone. A row of warnings that reads the same every
    morning stops informing and starts training the reader to skip the top of
    the page. Every fact it carried has to still be on the page, in the place
    where it is checkable."""

    def _data(self):
        return make_dataset(
            prospects=[make_prospect(damages_usd=None, damages_imputed=True)],
            counts={"items": 10, "screened_out": 5, "extracted": 3,
                    "ranked": 1, "awaiting_extraction": 0},
        )

    def test_no_banner_sits_above_the_results(self):
        data = make_dataset(
            prospects=[make_prospect(damages_usd=None, damages_imputed=True)],
            sources=[SourceRow("sec_fts", "SEC", "A", "s", "BROKEN", "", "", 2, 0)],
            counts={"items": 10, "screened_out": 1, "extracted": 1,
                    "ranked": 1, "awaiting_extraction": 42},
        )
        html = dashboard.render(data)
        assert 'class="banner' not in html.split('id="results"')[0]

    def test_the_coverage_map_carries_the_venue_caveat(self):
        html = dashboard.render(self._data())
        assert 'id="coverage"' in html
        assert "absence of signal" in html
        assert "D. Nev." in html

    def test_per_row_imputed_marking_is_where_the_damages_caveat_lives(self):
        html = dashboard.render(self._data())
        payload = _payload(html)
        assert payload["prospects"][0]["imputed"] is True
        assert payload["prospects"][0]["band"] == dataset.NOT_STATED
        # And the dollar filter still refuses to count an imputed figure.
        assert "p.imputed || !p.damages" in dashboard._JS

    def test_an_unhealthy_source_still_reaches_the_top_of_the_page(self):
        data = make_dataset(
            sources=[SourceRow("sec_fts", "SEC", "A", "s", "BROKEN", "", "", 2, 0),
                     SourceRow("ftc", "FTC", "A", "s", "BROKEN", "", "", 1, 0)],
        )
        head = dashboard.render(data).split('id="results"')[0]
        assert "2 SOURCES DOWN" in head

    def test_a_clean_run_says_nothing_alarming(self):
        """The alert state has to be absent when there is nothing wrong, or it
        is decoration rather than a signal."""
        head = dashboard.render(self._data()).split('id="results"')[0]
        assert "runpill down" not in head
        assert "DOWN" not in head


