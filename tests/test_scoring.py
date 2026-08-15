"""Taxonomy, exclusion, and scoring tests.

The exclusion tests encode a deliberate asymmetry: a false EXCLUSION is
invisible and loses a deal, while a false INCLUSION costs one row of
attention. So the screen is tested as much for what it must NOT exclude as
for what it must.
"""

from __future__ import annotations

import json

import pytest

from litfin.score.exclude import ExcludedArea, screen
from litfin.score.scoring import (
    DEFAULT_WEIGHTS,
    THESIS_PRIORS,
    _norm_damages,
    score_row,
)
from litfin.score.taxonomy import EventClass, Thesis, classify_text


class TestTaxonomyOrdering:
    def test_proposed_judgment_is_not_entered_judgment(self):
        """The Tunney Act distinction that matters most.

        'proposed final judgment' contains 'final judgment' as a substring.
        If the substring wins, every settlement still inside its 60-day
        comment window is misread as a decided matter.
        """
        hit = classify_text("United States v. Acme — Proposed Final Judgment")
        assert hit.event_class is EventClass.SETTLEMENT_PROPOSED
        assert hit.thesis is not Thesis.JUDGMENT

    def test_entered_judgment_is_judgment_thesis(self):
        hit = classify_text("Notice of entry of final judgment against defendant")
        assert hit.thesis is Thesis.JUDGMENT
        assert hit.event_class is EventClass.JUDGMENT_ENTERED

    def test_judgment_plus_appeal_scores_highest(self):
        """Judgment entered AND appeal in motion is the most on-thesis combo."""
        hit = classify_text(
            "Final judgment entered; defendant filed a notice of appeal and "
            "posted a supersedeas bond"
        )
        assert hit.thesis is Thesis.JUDGMENT
        assert hit.event_class is EventClass.APPEAL
        assert hit.strength >= 0.9

    def test_competitive_impact_statement_is_antitrust(self):
        """BUG PINNED: a CIS exists only under the Tunney Act, so it is
        definitionally antitrust -- but its title carries no antitrust keyword,
        and it was previously classified post_settlement.
        """
        hit = classify_text("U.S. v. CRH plc — Competitive Impact Statement")
        assert hit.thesis is Thesis.ANTITRUST

    def test_no_signal_returns_none(self):
        assert classify_text("Company announces quarterly earnings").thesis is Thesis.NONE

    def test_proposed_jury_verdict_is_not_a_verdict(self):
        """FOUND IN LIVE DATA, same substring trap as proposed final judgment.

        "Proposed Jury Verdict by USA as to William Stanley" is a blank
        verdict FORM filed during trial prep. The VERDICT pattern matches
        "jury verdict" inside it and scored these 0.9, sending pure noise to
        the LLM -- Opus correctly returned no_event for every one, which is
        how the bug surfaced.
        """
        for text in (
            "Proposed Jury Verdict by USA as to William Stanley",
            "Joint Proposed Verdict Form filed by both parties",
            "Proposed jury instructions and verdict form",
        ):
            assert classify_text(text).thesis is Thesis.NONE, text

    def test_real_verdict_still_detected(self):
        hit = classify_text("JURY VERDICT returned in favor of plaintiff")
        assert hit.thesis is Thesis.JUDGMENT
        assert hit.event_class is EventClass.VERDICT


class TestCriminalExclusion:
    """Criminal cases have no damages award to monetize."""

    @pytest.mark.parametrize("text", [
        "Proposed Jury Verdict by USA as to William Stanley, Heather Morrow",
        "Superseding indictment returned against defendant",
        "Change of plea hearing set; defendant pleaded guilty",
        "Sentencing memorandum and presentence report filed",
        "Arraignment held; detention hearing scheduled",
    ])
    def test_criminal_excluded(self, text):
        v = screen(text)
        assert v.excluded and v.area is ExcludedArea.CRIMINAL

    @pytest.mark.parametrize("text", [
        "Plea agreement filed; defendant pleaded guilty to bid-rigging under the Sherman Act",
        "USA as to Acme Corp -- price-fixing cartel prosecution",
        "Indictment charging a per se violation of the antitrust laws",
    ])
    def test_criminal_antitrust_survives(self, text):
        """DOJ criminal antitrust is the leading indicator for follow-on
        private treble-damage actions -- one of the three deal theses.
        Excluding it would drop exactly what the antitrust thesis exists for.
        """
        assert not screen(text).excluded, text

    def test_civil_case_unaffected(self):
        assert not screen(
            "Notice of settlement in a breach of contract action").excluded


class TestExclusionMustFire:
    @pytest.mark.parametrize("text,area", [
        ("Complaint for patent infringement of U.S. Patent No. 9,123,456",
         ExcludedArea.IP),
        ("Hatch-Waxman ANDA litigation", ExcludedArea.IP),
        ("inter partes review before the PTAB", ExcludedArea.IP),
        ("ICSID arbitration under a bilateral investment treaty",
         ExcludedArea.INTL_ARBITRATION),
        ("enforcement of a foreign arbitral award under the New York Convention",
         ExcludedArea.INTL_ARBITRATION),
        ("TCPA robocall class action", ExcludedArea.CONSUMER),
        ("FDCPA fair debt collection claim", ExcludedArea.CONSUMER),
        ("FTC halts credit repair scheme that scammed consumers",
         ExcludedArea.CONSUMER),
    ])
    def test_excluded(self, text, area):
        v = screen(text)
        assert v.excluded and v.area is area


class TestExclusionMustNotFire:
    """False exclusions are invisible and therefore the expensive error."""

    @pytest.mark.parametrize("text", [
        # 'consumers' is the central term in antitrust harm analysis. Excluding
        # on it would silently drop the matters we most want.
        "DOJ sues to block a merger that would harm consumers and raise prices",
        "Antitrust settlement returns money to consumers injured by price-fixing",
        # A company NAME containing 'patent' is not a patent case.
        "QUEST PATENT RESEARCH CORP (QPRC) 10-Q quarterly report",
        # Domestic commercial arbitration is IN scope.
        "The parties proceeded to arbitration under the AAA commercial rules",
        # Ordinary commercial language that merely resembles excluded terms.
        "The contract contained a patent ambiguity requiring extrinsic evidence",
        "Breach of contract action seeking $40 million in damages",
    ])
    def test_not_excluded(self, text):
        assert not screen(text).excluded, f"over-excluded: {text}"


class TestDamagesScaling:
    def test_log_scaled(self):
        """$1M -> $10M must matter far more than $500M -> $510M."""
        small = _norm_damages(10_000_000) - _norm_damages(1_000_000)
        large = _norm_damages(510_000_000) - _norm_damages(500_000_000)
        assert small > large * 10

    def test_bounds(self):
        assert _norm_damages(1_000) == 0.0
        assert _norm_damages(5_000_000_000) == 1.0


def _row(**kw):
    payload = kw.pop("payload", {})
    base = {
        "payload_json": json.dumps(payload),
        "deal_thesis": "judgment_monetization",
        "event_type": "judgment_entered",
        "event_date": None,
        "published_at": None,
        "practice_area": "commercial",
        "venue": "", "court": "", "jurisdiction": "",
        "damages_conf": "none",
    }
    base.update(kw)
    return base


class TestMissingDamages:
    """The most consequential scoring decision in the system."""

    def test_missing_damages_does_not_score_zero(self):
        """Imputing zero would bury exactly the large unlabeled cases.

        Most free sources never state a figure, so a zero-impute would rank
        every unlabeled matter last -- precisely backwards.
        """
        _, comps = score_row(_row(payload={"damages": {"amount_usd": None,
                                                       "confidence": "none"}}))
        assert comps.damages > 0.0
        assert comps.damages_imputed is True

    def test_stated_damages_beat_imputed(self):
        stated, _ = score_row(_row(payload={
            "damages": {"amount_usd": 250_000_000, "confidence": "high"}}))
        imputed, _ = score_row(_row(payload={
            "damages": {"amount_usd": None, "confidence": "none"}}))
        assert stated > imputed

    def test_imputation_is_flagged_for_the_user(self):
        _, comps = score_row(_row(payload={"damages": {"amount_usd": None}}))
        assert comps.damages_imputed
        assert any("imputed" in n for n in comps.notes)

    def test_low_confidence_figure_is_discounted(self):
        high, _ = score_row(_row(payload={
            "damages": {"amount_usd": 50_000_000, "confidence": "high"}}))
        low, _ = score_row(_row(payload={
            "damages": {"amount_usd": 50_000_000, "confidence": "low"}}))
        assert high > low


class TestScoreShape:
    def test_score_within_bounds(self):
        total, _ = score_row(_row(payload={
            "damages": {"amount_usd": 1_000_000_000, "confidence": "high"},
            "defendant_is_public_company": True}))
        assert 0.0 <= total <= 1.0

    def test_weights_sum_to_one(self):
        assert abs(sum(DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9

    def test_insolvency_reduces_collectability(self):
        solvent, cs = score_row(_row(payload={
            "defendant_is_public_company": True, "damages": {}}))
        broke, cb = score_row(_row(payload={
            "collectability_note": "defendant filed chapter 7 and is insolvent",
            "damages": {}}))
        assert cs.collectability > cb.collectability
        assert solvent > broke

    def test_better_venue_scores_higher(self):
        de, _ = score_row(_row(venue="Delaware Court of Chancery", payload={}))
        other, _ = score_row(_row(venue="D. Idaho", payload={}))
        assert de > other

    def test_recent_event_beats_stale(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        fresh, _ = score_row(_row(event_date=now.isoformat(), payload={}))
        stale, _ = score_row(
            _row(event_date=(now - timedelta(days=365)).isoformat(), payload={}))
        assert fresh > stale

    def test_case_filed_scores_below_judgment(self):
        """This pipeline wants DE-RISKED matters, not new filings."""
        filed, _ = score_row(_row(event_type="case_filed", payload={}))
        judged, _ = score_row(_row(event_type="judgment_entered", payload={}))
        assert judged > filed
