"""Compliance gate tests.

The purpose-mismatch test is the one that keeps the project honest as it ages:
if this ever becomes firm infrastructure, flipping `purpose` must disable the
research-only sources loudly rather than letting them keep running under terms
that no longer apply.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from litfin.compliance.registry import get_policy
from litfin.compliance.status import (
    CompliancePermanentlyBlocked,
    CompliancePurposeMismatch,
    ComplianceReviewStale,
    ComplianceUrlOutOfScope,
    Purpose,
    SourcePolicy,
    ToSStatus,
    assert_fetch_allowed,
)

NO_OPT_IN: frozenset[str] = frozenset()


class TestPurposeGating:
    def test_courtlistener_enabled_for_research(self):
        get_policy("courtlistener").assert_enabled(Purpose.RESEARCH, NO_OPT_IN)

    def test_courtlistener_blocked_for_commercial(self):
        """Flipping purpose must disable it -- loudly, not silently."""
        with pytest.raises(CompliancePurposeMismatch):
            get_policy("courtlistener").assert_enabled(Purpose.COMMERCIAL, NO_OPT_IN)

    def test_public_domain_gov_unaffected_by_purpose(self):
        for purpose in (Purpose.RESEARCH, Purpose.COMMERCIAL):
            get_policy("doj_atr").assert_enabled(purpose, NO_OPT_IN)


class TestProhibited:
    @pytest.mark.parametrize("purpose", [Purpose.RESEARCH, Purpose.COMMERCIAL])
    def test_ny_scraping_blocked_under_every_purpose(self, purpose):
        """NY's bot clause is unconditional -- 'for any use'.

        Research purpose does not reach it. This must never regress.
        """
        with pytest.raises(CompliancePermanentlyBlocked):
            get_policy("ny_iapps_scrape").assert_enabled(purpose, NO_OPT_IN)

    def test_prohibited_cannot_be_opted_into(self):
        """There is deliberately no config escape hatch for PROHIBITED."""
        everything = frozenset({"ny_iapps_scrape", "claims_kroll", "pacer"})
        for sid in everything:
            with pytest.raises(CompliancePermanentlyBlocked):
                get_policy(sid).assert_enabled(Purpose.RESEARCH, everything)


class TestUnverified:
    # Synthetic policies, deliberately. These test the GATE, not any
    # particular source -- and pinning them to real source ids meant that
    # completing a ToS review broke three unrelated tests (claims_epiq became
    # PROHIBITED and claims_stretto VERIFIED_PERMITTED in the Phase 7 sweep).
    UNREVIEWED_A = SourcePolicy(
        source_id="unreviewed_a", tier="B", status=ToSStatus.UNVERIFIED,
    )
    UNREVIEWED_B = SourcePolicy(
        source_id="unreviewed_b", tier="B", status=ToSStatus.UNVERIFIED,
    )

    def test_unverified_blocked_by_default(self):
        from litfin.compliance.status import ComplianceGateBlocked

        with pytest.raises(ComplianceGateBlocked):
            self.UNREVIEWED_A.assert_enabled(Purpose.RESEARCH, NO_OPT_IN)

    def test_unverified_allowed_with_specific_opt_in(self):
        self.UNREVIEWED_A.assert_enabled(
            Purpose.RESEARCH, frozenset({"unreviewed_a"})
        )

    def test_opt_in_is_per_source_not_global(self):
        """Opting into one Tier B source must not enable its neighbours."""
        from litfin.compliance.status import ComplianceGateBlocked

        opt = frozenset({"unreviewed_a"})
        with pytest.raises(ComplianceGateBlocked):
            self.UNREVIEWED_B.assert_enabled(Purpose.RESEARCH, opt)

    def test_real_unreviewed_sources_are_still_disabled(self):
        """The live registry check, kept separate so a future review moves
        only this assertion."""
        from litfin.compliance.status import ComplianceGateBlocked

        for source_id in ("claims_verita", "claims_bmc", "de_courtconnect"):
            with pytest.raises(ComplianceGateBlocked):
                get_policy(source_id).assert_enabled(Purpose.RESEARCH, NO_OPT_IN)

    def test_unregistered_source_fails_closed(self):
        """A connector that forgets to register is disabled, not permitted."""
        from litfin.compliance.status import ComplianceGateBlocked

        with pytest.raises(ComplianceGateBlocked):
            get_policy("totally_made_up").assert_enabled(Purpose.RESEARCH, NO_OPT_IN)


class TestExpiry:
    def test_stale_review_raises(self):
        stale = SourcePolicy(
            source_id="stale_src",
            tier="B",
            status=ToSStatus.VERIFIED_PERMITTED,
            expires_at=date.today() - timedelta(days=1),
        )
        with pytest.raises(ComplianceReviewStale):
            stale.assert_enabled(Purpose.RESEARCH, NO_OPT_IN)

    def test_fresh_review_passes(self):
        fresh = SourcePolicy(
            source_id="fresh_src",
            tier="B",
            status=ToSStatus.VERIFIED_PERMITTED,
            expires_at=date.today() + timedelta(days=30),
        )
        fresh.assert_enabled(Purpose.RESEARCH, NO_OPT_IN)


class TestUrlScope:
    def test_offsite_url_rejected(self):
        """A parser bug producing an off-site link must not cause a fetch."""
        with pytest.raises(ComplianceUrlOutOfScope):
            assert_fetch_allowed(
                get_policy("doj_atr"),
                "https://evil.example.com/steal",
                purpose=Purpose.RESEARCH,
                opt_in=NO_OPT_IN,
            )

    def test_in_scope_url_allowed(self):
        assert_fetch_allowed(
            get_policy("doj_atr"),
            "https://www.justice.gov/atr/case-document/file/12345",
            purpose=Purpose.RESEARCH,
            opt_in=NO_OPT_IN,
        )
