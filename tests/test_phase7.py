"""Phase 7: Tier B, gated on per-source ToS review.

Seven of eight reviews REFUSED. Most of this file pins those refusals, because
the expensive mistake here is not a broken parser — it is a later session
quietly re-enabling a source whose terms say no, on the grounds that it
"probably would have cleared."

The one source that passed, Stretto, gets parser and canary tests like any
other connector.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from litfin.canary.framework import CanaryFailure
from litfin.compliance.registry import get_policy
from litfin.compliance.status import (
    CompliancePermanentlyBlocked,
    ComplianceGateBlocked,
    ComplianceUrlOutOfScope,
    Purpose,
    ToSStatus,
)
from litfin.connectors.claims import stretto

FIX = Path(__file__).parent / "fixtures" / "stretto"
CASE_LIST = FIX / "case_list.json"

# The Stretto fixture is a live capture of a PRIVATE VENDOR's case data and is
# gitignored. Their terms permit us to READ the case index; redistributing
# their data in a public repository is a different act, and one nobody
# reviewed. The compliance tests below are what matter most here and run
# without it — only the parser tests need the bytes.
#
# Regenerate locally with:
#   litfin run --weekly --source claims_stretto
# then save one response, or see HANDOFF.md.
needs_fixture = pytest.mark.skipif(
    not CASE_LIST.is_file(),
    reason="tests/fixtures/stretto/case_list.json is gitignored (vendor data); "
           "regenerate locally to run the parser tests",
)


def load() -> bytes:
    return CASE_LIST.read_bytes()


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------

# Every source whose review refused, with the ground it refused on. The
# parametrization is the point: adding a Tier B source without a review makes
# this list wrong.
REFUSED = {
    "stanford_ssc": "403 to an identified client on homepage AND disclaimer",
    "claims_epiq": "terms bar automated searches without prior written consent",
    "claims_angeion": "terms bar robots 'for any purpose'; personal use only",
    "claims_omni": "403 to an identified client",
    "claims_kroll": "403 on robots.txt and on content",
    "nc_business_court": "403 on robots.txt and on content",
}

INCONCLUSIVE = {
    "claims_verita": "no automated-access clause; general reproduction bar only",
    "claims_bmc": "publishes no terms of use at all",
    "de_courtconnect": "click-through disclaimer is a legal act we will not automate",
}


class TestRefusals:
    @pytest.mark.parametrize("source_id", sorted(REFUSED))
    def test_refused_sources_are_prohibited(self, source_id):
        assert get_policy(source_id).status is ToSStatus.PROHIBITED

    @pytest.mark.parametrize("source_id", sorted(REFUSED))
    def test_prohibited_has_no_config_escape_hatch(self, source_id):
        """Opting the id in explicitly must still fail. This is the property
        that stops somebody enabling a refused source at 2am."""
        policy = get_policy(source_id)
        with pytest.raises(CompliancePermanentlyBlocked):
            policy.assert_enabled(Purpose.RESEARCH, frozenset({source_id}))

    @pytest.mark.parametrize("source_id", sorted(REFUSED))
    def test_every_refusal_records_its_grounds(self, source_id):
        """A status with no reasoning is a status the next session will
        reverse. Each refusal carries either a verbatim quote or an explicit
        statement that it rests on observed behavior."""
        note = get_policy(source_id).review_note or ""
        assert len(note) > 120, f"{source_id} has no substantive review_note"
        assert ("VERBATIM" in note or "PROVISIONAL" in note), (
            f"{source_id} must say whether it rests on read terms or on "
            f"observed behavior"
        )

    def test_epiq_records_the_operative_clause_verbatim(self):
        note = get_policy("claims_epiq").review_note
        assert "automated searches or data queries" in note
        assert "prior written consent" in note

    def test_angeion_records_the_unconditional_robot_clause(self):
        note = get_policy("claims_angeion").review_note
        assert "robot, spider or other automatic device" in note
        assert "for any purpose" in note
        assert "personal, non-commercial use only" in note

    def test_omni_reversal_is_explained(self):
        """robots.txt leaned permissive and the server 403'd anyway. The note
        must keep the reasoning, or somebody re-reads the robots file and
        re-enables it."""
        note = get_policy("claims_omni").review_note
        assert "403" in note
        assert "server wins" in note

    @pytest.mark.parametrize("source_id", sorted(INCONCLUSIVE))
    def test_inconclusive_sources_stay_unverified_and_disabled(self, source_id):
        """An unanswered question resting at 'off' is the whole design."""
        policy = get_policy(source_id)
        assert policy.status is ToSStatus.UNVERIFIED
        with pytest.raises(ComplianceGateBlocked):
            policy.assert_enabled(Purpose.RESEARCH, frozenset())

    def test_bmc_absence_of_terms_is_not_permission(self):
        note = get_policy("claims_bmc").review_note
        assert "NO TERMS OF USE" in note
        assert "absence of terms is NOT permission" in note.replace("The ", "")


# ---------------------------------------------------------------------------
# The one that cleared
# ---------------------------------------------------------------------------

class TestStrettoPermission:
    def test_stretto_is_the_only_tier_b_source_enabled(self):
        from litfin.compliance.registry import POLICIES

        enabled = {
            pid for pid, p in POLICIES.items()
            if p.tier == "B" and p.is_enabled(Purpose.RESEARCH, frozenset())
        }
        assert enabled == {"claims_stretto"}

    def test_review_quotes_the_ai_tools_scoping(self):
        """Stretto's ONLY scraping clause is scoped to its AI tools. That
        scoping is the entire basis for the permission, so it has to be in
        the record verbatim."""
        note = get_policy("claims_stretto").review_note
        assert "web scraping, web harvesting, web data extraction" in note
        assert "AI TOOLS" in note
        assert "NO general prohibition" in note

    def test_review_expires(self):
        policy = get_policy("claims_stretto")
        assert policy.expires_at is not None
        assert policy.reviewed_at is not None

    def test_url_scope_is_limited_to_the_case_surface(self):
        policy = get_policy("claims_stretto")
        policy.assert_url_in_scope(stretto.case_list_url())
        for off_site in (
            "https://www.stretto.com/legal-policies/",
            "https://case.stretto.com/Marelli",
            "https://cases.stretto.com.evil.invalid/x",
        ):
            with pytest.raises(ComplianceUrlOutOfScope):
                policy.assert_url_in_scope(off_site)


class TestStrettoAiBoundary:
    def test_no_chatbot_endpoint_is_ever_constructed(self):
        """Section 21 is the one clause in Stretto's terms this connector
        could actually violate.

        Tests the URLs the module can build, not whether it mentions the
        assistant in prose — the docstring names it precisely to say it is
        off-limits, and a naive string search would flag that as a violation.
        """
        import re

        src = Path(stretto.__file__).read_text(encoding="utf-8")
        # Every string literal that looks like a URL or a path fragment.
        literals = re.findall(r'["\']((?:https?://|/)[^"\']*)["\']', src)
        assert literals, "expected some URL literals to check"
        for lit in literals:
            low = lit.lower()
            assert "chat" not in low, f"chat endpoint literal: {lit}"
            assert "conductor" not in low, f"assistant endpoint literal: {lit}"
        # And the only host it ever addresses is the case index.
        hosts = {l for l in literals if l.startswith("http")}
        assert hosts <= {stretto.BASE, stretto.LEGACY_BASE}, hosts

    def test_planned_urls_are_only_the_case_index(self):
        for task in stretto.build().plan(None):
            assert task.url.startswith(stretto.AJAX)
            assert "case_list_data" in task.url

    @needs_fixture
    def test_chatbot_flag_is_recorded_but_inert(self):
        """The flag is in the payload so it is stored; storing a boolean is
        not calling an endpoint."""
        r = stretto.build().parse(load(), stretto.case_list_url())
        assert any("chatbot_enabled" in i.payload for i in r.items)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@needs_fixture
class TestStrettoParsing:
    def test_parses_lead_debtors(self):
        r = stretto.build().parse(load(), stretto.case_list_url())
        assert r.rows_parsed == 13
        p = r.items[0].payload
        assert p["vendor_id"] == "stretto"
        assert p["record_kind"] == "claims_assignment"
        assert p["case_number"]

    def test_records_total_counts_debtors_not_cases(self):
        """recordsTotal is every debtor row (2,992); data carries only lead
        debtors. Treating them as the same measure would report permanent
        partial coverage."""
        r = stretto.build().parse(load(), stretto.case_list_url())
        assert "13 lead chapter 11 cases" in r.note
        assert "2992 debtor rows" in r.note
        assert r.partial_coverage is False

    def test_affiliated_debtors_are_captured(self):
        r = stretto.build().parse(load(), stretto.case_list_url())
        biggest = max(r.items, key=lambda i: len(i.payload["affiliated_debtors"]))
        assert len(biggest.payload["affiliated_debtors"]) > 5

    def test_legacy_cases_get_the_legacy_host(self):
        r = stretto.build().parse(load(), stretto.case_list_url())
        legacy = [i for i in r.items if i.payload["is_legacy"]]
        assert legacy, "fixture should contain a legacy case"
        assert legacy[0].payload["agent_case_url"].startswith(stretto.LEGACY_BASE)

    def test_no_docket_url_is_invented_when_the_site_offers_none(self):
        """68 of 369 rows carry PROVIDE_LINK=0. A synthesized URL that 404s
        is worse than an honest blank."""
        r = stretto.build().parse(load(), stretto.case_list_url())
        nolink = [i for i in r.items if not i.payload["has_public_docket"]]
        assert nolink, "fixture should contain a no-link case"
        assert all(i.payload["agent_case_url"] == "" for i in nolink)

    def test_null_filing_dates_are_tolerated(self):
        """DATE_FILED is null on ~2% of rows."""
        r = stretto.build().parse(load(), stretto.case_list_url())
        undated = [i for i in r.items if not i.payload["date_filed"]]
        assert undated, "fixture should contain an undated case"
        assert undated[0].published_at is None

    def test_parse_never_raises_on_garbage(self):
        c = stretto.build()
        for junk in (b"", b"not json", b"[]", b'{"data": "nope"}', b"null"):
            assert c.parse(junk, "u").rows_parsed == 0

    def test_column_params_are_in_the_planned_url(self):
        """Drop these and the endpoint answers 200 with zero rows."""
        url = stretto.case_list_url()
        for col in ("CASE_NAME", "DATE_FILED", "COURT_DISTRICT"):
            assert col in url
        assert "columns%5B0%5D%5Bdata%5D" in url


@needs_fixture
class TestStrettoCensusDiscipline:
    def test_bodies_carry_no_event_language(self):
        """Same rule as the Phase 5 census and the Phase 4 indexes: a census
        record must not be dressed up as a deal signal."""
        banned = ("settlement", "judgment", "verdict", "damages", "awarded")
        for item in stretto.build().parse(load(), "u").items:
            low = item.body.lower()
            for word in banned:
                assert word not in low, f"'{word}' in {item.body!r}"

    def test_screen_drops_every_row(self):
        from litfin.score.taxonomy import classify_text

        for item in stretto.build().parse(load(), "u").items:
            hit = classify_text(f"{item.title}\n{item.body}")
            assert not hit.is_signal, f"{item.title!r} would cost budget"


# ---------------------------------------------------------------------------
# The silent-empty trap
# ---------------------------------------------------------------------------

@needs_fixture
class TestStrettoCanary:
    def test_passes_on_a_real_response(self):
        stretto.build().canary(load(), None)

    def test_catches_the_columns_dropped_silent_empty(self):
        """MEASURED: omit the DataTables columns[...] params and the endpoint
        returns HTTP 200, valid JSON, recordsTotal 2992, and zero rows. To
        anything checking only the status code that is indistinguishable from
        a quiet week — forever."""
        payload = json.dumps({"recordsTotal": "2992", "data": []}).encode()
        with pytest.raises(CanaryFailure, match="ZERO"):
            stretto.build().canary(payload, None)

    def test_empty_with_no_total_also_fails(self):
        payload = json.dumps({"data": []}).encode()
        with pytest.raises(CanaryFailure, match="not credible"):
            stretto.build().canary(payload, None)

    def test_a_waf_interstitial_fails_loudly(self):
        html = b"<html><body>Please enable JavaScript to continue</body></html>"
        with pytest.raises(CanaryFailure, match="WAF|JSON"):
            stretto.build().canary(html, None)

    def test_shape_change_fails(self):
        payload = json.dumps(
            {"recordsTotal": "10", "data": [{"SOMETHING_ELSE": 1}]}
        ).encode()
        with pytest.raises(CanaryFailure, match="missing expected field"):
            stretto.build().canary(payload, None)


# ---------------------------------------------------------------------------
# The terms-reading exemption
# ---------------------------------------------------------------------------

class TestReadingTermsExemption:
    def test_reading_terms_cannot_reach_a_data_url(self):
        """The exemption exists to break a circular gate, not to open one. It
        is a literal membership test against the policy's own tos_urls."""
        from litfin.config import Config
        from litfin.net.budget import GlobalBudget
        from litfin.net.client import PoliteClient
        from litfin.store.db import Database
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            cfg = Config(data_root=Path(td))
            cfg.ensure_dirs()
            db = Database(cfg.db_path)
            client = PoliteClient(
                cfg, budget=GlobalBudget(db.conn, max_per_day=10),
            )
            try:
                # claims_verita is UNVERIFIED, so it gets PAST the
                # prohibited check and reaches the URL membership test —
                # which is the thing under test here.
                with pytest.raises(ComplianceUrlOutOfScope):
                    client.get(
                        "https://www.veritaglobal.net/api/cases",
                        source_id="claims_verita", reading_terms=True,
                    )
            finally:
                client.close()
                db.close()

    def test_reading_terms_still_refuses_a_prohibited_source(self):
        """A site that has already refused consent does not get re-litigated
        by re-reading it."""
        from litfin.config import Config
        from litfin.net.budget import GlobalBudget
        from litfin.net.client import PoliteClient
        from litfin.store.db import Database
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            cfg = Config(data_root=Path(td))
            cfg.ensure_dirs()
            db = Database(cfg.db_path)
            client = PoliteClient(
                cfg, budget=GlobalBudget(db.conn, max_per_day=10),
            )
            try:
                policy = get_policy("claims_kroll")
                with pytest.raises(CompliancePermanentlyBlocked):
                    client.get(
                        policy.tos_urls[0], source_id="claims_kroll",
                        reading_terms=True,
                    )
            finally:
                client.close()
                db.close()
