"""Entity resolution and the government-forfeiture exclusion.

Both were found by looking at the first real ranked list rather than by
reasoning about the design, and both fail in the direction that is hard to
see: a third of the list was the same matters repeated, and a crypto seizure
ranked five times on the amount printed in its own case name.

The over-merge tests matter more than the merge tests. A missed merge costs a
duplicate row; a wrong merge DELETES a matter, and a deleted matter is
invisible — the same asymmetry the exclusion screen is built around.
"""

from __future__ import annotations

import pytest

from litfin.score import cluster
from litfin.score.exclude import ExcludedArea, screen, screen_extraction


# ---------------------------------------------------------------------------
# Forfeiture
# ---------------------------------------------------------------------------

class TestForfeitureExclusion:
    # The live false positive, and the variants of the in rem convention.
    FORFEITURE = [
        "United States v. Approximately 225,364,961 USDT",
        "United States v. Approximately 225,364,961 USDT (as to Wallet 0x82e)",
        "United States of America v. Approximately $1,200,000 in U.S. Currency",
        "United States v. Real Property Located at 123 Main Street",
        "USA v. approximately 4.5 bitcoin",
        "Verified Complaint for Forfeiture In Rem",
        "civil asset forfeiture proceeding under 18 U.S.C. 981",
        "The government filed a forfeiture complaint against the assets",
        "Decree of forfeiture entered as to the seized funds",
        "brought under 21 U.S.C. § 881",
        "CAFRA claim filed by a third party",
    ]

    # Ordinary commercial language that uses the same words. Excluding any of
    # these would silently drop a real matter.
    NOT_FORFEITURE = [
        "The court ordered forfeiture of the earnest money deposit",
        "The employment agreement contained an anti-forfeiture clause",
        "Plaintiff seeks forfeiture of unvested restricted stock units",
        "The lease provides for forfeiture upon default",
        "Forfeiture of the performance bond was the agreed remedy",
        "asset recovery specialists retained by the chapter 7 trustee",
        "The receiver seized the collateral under the security agreement",
        "Writ of attachment issued against the judgment debtor's accounts",
        "United States and State of Tennessee v. CRH plc, et al.",
        "United States v. Columbus McKinnon Corporation",
        "Judgment entered for $225,364,961 against Acme Corporation",
    ]

    @pytest.mark.parametrize("text", FORFEITURE)
    def test_forfeiture_is_excluded(self, text):
        v = screen(text)
        assert v.excluded, text
        assert v.area is ExcludedArea.FORFEITURE, text

    @pytest.mark.parametrize("text", NOT_FORFEITURE)
    def test_commercial_forfeiture_language_survives(self, text):
        """The bare word 'forfeiture' is ordinary commercial language. This is
        the same call as not excluding on the bare word 'consumers'."""
        v = screen(text)
        assert not (v.excluded and v.area is ExcludedArea.FORFEITURE), (
            f"{text!r} was wrongly excluded as forfeiture"
        )

    def test_the_live_false_positive_by_its_full_row(self):
        """The row as it actually appeared: no event, and a 'damages' figure
        that is the seized amount from the caption."""
        row = {
            "case_caption": "United States v. Approximately 225,364,961 USDT",
            "summary": "Docket entry in a civil forfeiture action concerning "
                       "seized cryptocurrency.",
            "procedural_posture": "In rem forfeiture proceeding pending.",
        }
        v = screen_extraction(row)
        assert v.excluded
        assert v.area is ExcludedArea.FORFEITURE
        assert "SEIZED amount" in v.reason

    def test_reason_explains_why_it_is_not_a_prospect(self):
        v = screen(self.FORFEITURE[0])
        assert "no assignable claim" in v.reason

    def test_screen_extraction_reads_the_caption(self):
        """The in rem convention lives in the CAPTION and nowhere else, which
        is exactly why the pre-extraction screen over a RECAP docket entry's
        procedural body missed it."""
        row = {"case_caption": "United States v. Approximately 500 Bitcoin",
               "summary": "", "procedural_posture": ""}
        assert screen_extraction(row).excluded

    def test_screen_extraction_is_clean_on_a_real_prospect(self):
        row = {
            "case_caption": "State of North Carolina et al. v. Sandoz Inc.",
            "summary": "43 state attorneys general reached a $400 million "
                       "settlement with generic drug manufacturer Sandoz.",
            "procedural_posture": "Settlement in principle reached.",
        }
        assert not screen_extraction(row).excluded


# ---------------------------------------------------------------------------
# Caption normalization
# ---------------------------------------------------------------------------

class TestCaptionKeys:
    def test_corporate_suffixes_do_not_split_a_matter(self):
        n = cluster.normalize_caption
        assert n("Acme Corp. v. Widget Inc.") == n("Acme Corporation v. Widget, Inc.")
        assert n("In re Yellow Corporation") == n("In re Yellow Corp., et al.")

    def test_parentheticals_are_stripped(self):
        n = cluster.normalize_caption
        assert n("In re Zohar III, Corp. (appeal)") == n("In re Zohar III Corp.")

    def test_government_plaintiff_enumeration_is_normalized(self):
        """MEASURED: one DOJ matter, two captions differing only in which
        states are listed as co-plaintiffs."""
        n = cluster.normalize_caption
        assert n("United States and 17 State Attorneys General v. Cal-Maine Foods") \
            == n("United States and Plaintiff States v. Cal-Maine Foods, Inc.")
        assert n("United States v. OhioHealth Corporation") \
            == n("United States and State of Ohio v. OhioHealth Corporation")

    def test_the_us_token_is_kept_so_private_suits_do_not_merge(self):
        """Keying on the defendant alone would merge 'Gjovik v. Apple' with
        'United States v. Apple' — unrelated matters."""
        n = cluster.normalize_caption
        assert n("Gjovik v. Apple Inc.") != n("United States v. Apple Inc.")

    def test_different_plaintiffs_suing_one_agency_stay_separate(self):
        """LIVE CASE: two genuinely different suits against HUD."""
        n = cluster.normalize_caption
        assert n("National Alliance to End Homelessness v. U.S. Dept of Housing") \
            != n("State of Washington v. U.S. Dept of Housing")


class TestUnusableKeys:
    """THE TRAP. Nine unrelated matters in the live corpus shared an empty
    caption — an antitrust consent decree, an HSR annual report, a speech,
    several docket entries. Clustering them would have deleted eight real
    prospects and looked like a cleaner list."""

    @pytest.mark.parametrize("caption", [
        "", "   ", "In re", "in re", "United States", "USA", "unknown",
        "Not stated", "The", "et al.",
    ])
    def test_generic_or_empty_captions_produce_no_key(self, caption):
        assert cluster.normalize_caption(caption) == ""

    def test_unkeyable_rows_become_singletons_not_one_giant_cluster(self):
        rows = [
            {"item_uid": f"uid{i}", "score": 0.5, "case_caption": "",
             "docket_number": "", "source_id": "doj_atr", "title": f"story {i}"}
            for i in range(9)
        ]
        clusters = cluster.build(rows)
        assert len(clusters) == 9
        assert all(c.size == 1 for c in clusters)
        assert len({c.key for c in clusters}) == 9

    def test_docket_number_rescues_an_uncaptioned_row(self):
        rows = [
            {"item_uid": "a", "score": 0.6, "case_caption": "",
             "docket_number": "1:26-cv-01234", "source_id": "s"},
            {"item_uid": "b", "score": 0.5, "case_caption": "",
             "docket_number": "26-cv-1234 (entry 17)", "source_id": "s"},
        ]
        clusters = cluster.build(rows)
        assert len(clusters) == 1
        assert clusters[0].size == 2


# ---------------------------------------------------------------------------
# Truncated defendant lists
# ---------------------------------------------------------------------------

class TestTruncatedDefendantLists:
    def test_et_al_caption_absorbs_into_the_enumerated_one(self):
        """MEASURED: 'Taiheiyo Cement Corporation, et al.' and 'Taiheiyo
        Cement Corporation and CalPortland Company' are one DOJ matter."""
        rows = [
            {"item_uid": "long", "score": 0.59, "source_id": "doj",
             "case_caption": "United States v. Taiheiyo Cement Corporation "
                             "and CalPortland Company", "docket_number": ""},
            {"item_uid": "short", "score": 0.58, "source_id": "doj",
             "case_caption": "United States and State of California v. "
                             "Taiheiyo Cement Corporation, et al.",
             "docket_number": ""},
        ]
        clusters = cluster.build(rows)
        assert len(clusters) == 1
        assert clusters[0].size == 2
        assert clusters[0].primary.item_uid == "long"

    def test_a_shorter_caption_WITHOUT_et_al_does_not_absorb(self):
        """Without the explicit admission that the list is incomplete, a
        shorter caption may simply be a narrower case — and merging would
        delete one of them."""
        rows = [
            {"item_uid": "both", "score": 0.6, "source_id": "doj",
             "case_caption": "United States v. Apple Inc. and Google LLC",
             "docket_number": ""},
            {"item_uid": "one", "score": 0.5, "source_id": "doj",
             "case_caption": "United States v. Apple Inc.", "docket_number": ""},
        ]
        clusters = cluster.build(rows)
        assert len(clusters) == 2

    def test_ambiguous_extension_refuses_to_merge(self):
        """Two candidates could absorb the short caption. The safe answer to
        an ambiguous merge is not to merge."""
        rows = [
            {"item_uid": "a", "score": 0.6, "source_id": "doj",
             "case_caption": "United States v. Acme Corp and Widget Inc",
             "docket_number": ""},
            {"item_uid": "b", "score": 0.6, "source_id": "doj",
             "case_caption": "United States v. Acme Corp and Gadget LLC",
             "docket_number": ""},
            {"item_uid": "short", "score": 0.5, "source_id": "doj",
             "case_caption": "United States v. Acme Corp, et al.",
             "docket_number": ""},
        ]
        clusters = cluster.build(rows)
        assert len(clusters) == 3

    def test_prefix_must_land_on_a_token_boundary(self):
        rows = [
            {"item_uid": "a", "score": 0.6, "source_id": "s",
             "case_caption": "United States v. Applebees Grill", "docket_number": ""},
            {"item_uid": "b", "score": 0.5, "source_id": "s",
             "case_caption": "United States v. Apple, et al.", "docket_number": ""},
        ]
        clusters = cluster.build(rows)
        assert len(clusters) == 2


# ---------------------------------------------------------------------------
# Representative selection
# ---------------------------------------------------------------------------

class TestPrimarySelection:
    def _rows(self, **overrides):
        base = [
            {"item_uid": "hi", "score": 0.8, "case_caption": "Acme v. Widget",
             "source_id": "a", "docket_number": ""},
            {"item_uid": "lo", "score": 0.4, "case_caption": "Acme v. Widget",
             "source_id": "b", "docket_number": ""},
        ]
        for r in base:
            r.update(overrides.get(r["item_uid"], {}))
        return base

    def test_highest_score_represents_the_matter(self):
        c = cluster.build(self._rows())[0]
        assert c.primary.item_uid == "hi"
        assert [m.item_uid for m in c.others] == ["lo"]

    def test_tie_breaks_toward_a_stated_damages_figure(self):
        rows = self._rows(
            hi={"score": 0.6}, lo={"score": 0.6, "has_stated_damages": True},
        )
        assert cluster.build(rows)[0].primary.item_uid == "lo"

    def test_tie_then_breaks_toward_having_an_event_date(self):
        rows = self._rows(
            hi={"score": 0.6}, lo={"score": 0.6, "event_date": "2026-08-01"},
        )
        assert cluster.build(rows)[0].primary.item_uid == "lo"

    def test_selection_is_deterministic_across_runs(self):
        """A list that reshuffles between identical runs is one nobody can
        review."""
        rows = self._rows(hi={"score": 0.6}, lo={"score": 0.6})
        first = cluster.build(rows)[0].primary.item_uid
        for _ in range(5):
            assert cluster.build(list(reversed(rows)))[0].primary.item_uid == first


class TestSummary:
    def test_counts_documents_and_matters_separately(self):
        rows = [
            {"item_uid": "a", "score": 0.8, "case_caption": "Acme v. Widget",
             "source_id": "s", "docket_number": ""},
            {"item_uid": "b", "score": 0.7, "case_caption": "Acme v. Widget",
             "source_id": "s", "docket_number": ""},
            {"item_uid": "c", "score": 0.6, "case_caption": "Beta v. Gamma",
             "source_id": "s", "docket_number": ""},
        ]
        stats = cluster.summarize(cluster.build(rows))
        assert stats == {
            "rows": 3, "matters": 2,
            "clusters_with_duplicates": 1, "rows_absorbed": 1,
        }


# ---------------------------------------------------------------------------
# End to end through the database
# ---------------------------------------------------------------------------

class TestRankIntegration:
    def _seed(self, tmp_path):
        import json

        from litfin.store.db import Database, Item

        db = Database(tmp_path / "t.db")
        rows = [
            # One matter, three documents.
            ("a", "Acme Corp v. Widget Inc.", 0.0, "doj_atr"),
            ("b", "Acme Corporation v. Widget, Inc.", 0.0, "courtlistener"),
            ("c", "Acme Corp. v. Widget Inc", 0.0, "doj_atr_case_filings"),
            # A separate matter.
            ("d", "Beta LLC v. Gamma Ltd", 0.0, "doj_atr"),
            # A forfeiture that must not survive the re-screen.
            ("e", "United States v. Approximately 900,000 USDC", 0.0, "courtlistener"),
        ]
        for uid, caption, _score, source in rows:
            db.commit_task(
                run_id="r", task_id=f"t{uid}", source_id=source,
                task_key=f"k{uid}",
                items=[Item(source_id=source, natural_key=uid,
                            title=caption, body="settlement reached")],
                watermark_value=None, seen_keys=[], rows_parsed=1, rows_new=1,
            )
            real_uid = Item(source_id=source, natural_key=uid).item_uid
            db.store_extraction(real_uid, {
                "case_caption": caption,
                "deal_thesis": "post_settlement",
                "event_type": "settlement_reached",
                "practice_area": "commercial",
                "summary": "A settlement.",
                "damages": {"amount_usd": None, "confidence": "none"},
            }, model="test")
        return db

    def test_one_matter_becomes_one_row(self, tmp_path):
        from litfin.score.scoring import rank_all

        db = self._seed(tmp_path)
        try:
            report = rank_all(db)
            assert report.late_excluded == 1, "the forfeiture row must drop"
            assert report.scored == 4
            assert report.matters == 2, "Acme x3 + Beta x1"
            assert report.duplicates_absorbed == 2

            shown = db.top_prospects(limit=50)
            assert len(shown) == 2
            captions = sorted((r["case_caption"] or "") for r in shown)
            assert "Beta LLC v. Gamma Ltd" in captions
        finally:
            db.close()

    def test_duplicates_are_kept_and_retrievable(self, tmp_path):
        """Filtered, not deleted. 'One matter' must never quietly mean 'we
        threw evidence away'."""
        from litfin.score.scoring import rank_all

        db = self._seed(tmp_path)
        try:
            rank_all(db)
            all_rows = db.top_prospects(limit=50, include_duplicates=True)
            assert len(all_rows) == 4

            primary = [r for r in db.top_prospects(limit=50)
                       if r["cluster_size"] > 1][0]
            members = db.cluster_members([primary["cluster_key"]])
            assert len(members) == 2
            assert {m["source_id"] for m in members}
        finally:
            db.close()

    def test_rank_numbers_count_matters_not_documents(self, tmp_path):
        from litfin.score.scoring import rank_all

        db = self._seed(tmp_path)
        try:
            rank_all(db)
            ranks = [
                r[0] for r in db.conn.execute(
                    "SELECT rank_in_run FROM prospect WHERE is_primary=1 "
                    "ORDER BY rank_in_run"
                )
            ]
            assert ranks == [1, 2]
        finally:
            db.close()

    def test_rerunning_rank_is_idempotent(self, tmp_path):
        from litfin.score.scoring import rank_all

        db = self._seed(tmp_path)
        try:
            first = rank_all(db)
            second = rank_all(db)
            # The forfeiture row is already excluded by the second pass, so it
            # is no longer "considered" -- but the ranked output must match.
            assert second.matters == first.matters
            assert second.scored == first.scored
            assert second.late_excluded == 0
            assert len(db.top_prospects(limit=50)) == 2
        finally:
            db.close()

    def test_late_exclusion_is_recorded_separately_from_the_models(self, tmp_path):
        """The pipeline's own post-hoc call must not overwrite the model's
        excluded_reason, or the audit trail stops showing who decided what."""
        from litfin.score.scoring import rank_all

        db = self._seed(tmp_path)
        try:
            rank_all(db)
            row = db.conn.execute(
                "SELECT is_excluded, excluded_reason_late FROM extraction "
                "WHERE case_caption LIKE '%Approximately%'"
            ).fetchone()
            assert row["is_excluded"] == 1
            assert "forfeiture" in row["excluded_reason_late"]
        finally:
            db.close()


class TestMigration:
    def test_columns_are_added_to_a_pre_existing_database(self, tmp_path):
        """CREATE TABLE IF NOT EXISTS is a no-op on an existing database, so a
        new column in schema.sql never reaches one. This is the failure that
        broke `litfin rank` with 'no such column: cluster_key'."""
        import sqlite3

        from litfin.store.db import Database

        path = tmp_path / "old.db"
        con = sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE prospect (item_uid TEXT PRIMARY KEY, scored_at TEXT "
            "NOT NULL, score REAL NOT NULL, rank_in_run INTEGER, "
            "components_json TEXT NOT NULL DEFAULT '{}');"
        )
        con.commit()
        con.close()

        db = Database(path)
        try:
            cols = {r[1] for r in db.conn.execute("PRAGMA table_info(prospect)")}
            assert {"cluster_key", "cluster_size", "is_primary"} <= cols
            # Idempotent: opening again must not fail.
            Database(path).close()
        finally:
            db.close()
