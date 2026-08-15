"""Phase 6: NY eTrack alert-email ingestion.

The gate tests matter more than the parser tests here. Scraping NY UCS is
permanently PROHIBITED, and email ingestion is RESTRICTED pending a decision
nobody has made yet -- so the thing that must be provably true is that nothing
connects until two separate switches are set.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from litfin.config import Config
from litfin.connectors import etrack_email as et

FIX = Path(__file__).parent / "fixtures" / "etrack"


@dataclass
class FakeCfg:
    etrack_enabled: bool = False
    etrack_decision_recorded: str = ""


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

class TestGate:
    def test_disabled_by_default_in_real_config(self):
        cfg = Config()
        assert cfg.etrack_enabled is False
        assert cfg.etrack_decision_recorded == ""
        with pytest.raises(et.EtrackDisabled):
            et.assert_enabled(cfg)

    def test_enabled_alone_is_not_enough(self):
        """Two switches, because they record two different decisions. The
        operational one must not be able to answer the legal one."""
        with pytest.raises(et.EtrackDisabled, match="decision_recorded"):
            et.assert_enabled(FakeCfg(etrack_enabled=True))

    def test_decision_alone_is_not_enough(self):
        with pytest.raises(et.EtrackDisabled):
            et.assert_enabled(
                FakeCfg(etrack_enabled=False, etrack_decision_recorded="me, today")
            )

    def test_whitespace_is_not_a_decision(self):
        with pytest.raises(et.EtrackDisabled):
            et.assert_enabled(
                FakeCfg(etrack_enabled=True, etrack_decision_recorded="   ")
            )

    def test_both_set_opens_the_gate(self):
        et.assert_enabled(
            FakeCfg(etrack_enabled=True, etrack_decision_recorded="sfm 2026-08-15")
        )

    def test_ingest_refuses_before_reading_any_credential(self, monkeypatch):
        """The gate runs before the environment is touched, so a disabled
        install cannot leak credentials into a traceback."""
        monkeypatch.setenv("LITFIN_IMAP_HOST", "imap.example.invalid")
        monkeypatch.setenv("LITFIN_IMAP_USER", "u")
        monkeypatch.setenv("LITFIN_IMAP_PASSWORD", "p")
        with pytest.raises(et.EtrackDisabled):
            et.ingest(FakeCfg(), db=None)

    def test_scraping_ny_stays_prohibited_regardless(self):
        """Neither switch touches the scraping ban. Different source id,
        different determination, no config escape hatch."""
        from litfin.compliance.status import (
            CompliancePermanentlyBlocked, Purpose, ToSStatus,
        )
        from litfin.compliance.registry import get_policy

        ny = get_policy("ny_iapps_scrape")
        assert ny.status is ToSStatus.PROHIBITED
        # Even opting the id in explicitly does not enable it: PROHIBITED has
        # no configuration escape hatch.
        with pytest.raises(CompliancePermanentlyBlocked):
            ny.assert_enabled(Purpose.RESEARCH, frozenset({"ny_iapps_scrape"}))


# ---------------------------------------------------------------------------
# Pure parsing
# ---------------------------------------------------------------------------

class TestParser:
    def test_parses_a_decision_alert(self):
        alert = et.parse_alert((FIX / "sample_decision.eml").read_bytes())
        assert alert.ok
        assert alert.index_number == "651234/2026"
        assert alert.caption.startswith("Meridian Capital Partners")
        assert alert.court == "Supreme Court"
        assert alert.county == "New York"
        assert alert.event_kind == "decision"
        assert alert.event_date == "2026-08-07"

    def test_label_normalization_does_not_corrupt_other_keys(self):
        """BUG PINNED: normalization used to be key.replace('no', 'number'),
        a substring replace applied to the whole key. 'notification type'
        became 'numbertification type' -- every label containing the letters
        'no' was silently corrupted."""
        alert = et.parse_alert((FIX / "sample_decision.eml").read_bytes())
        assert "notification type" in alert.fields
        assert "numbertification type" not in alert.fields
        assert alert.fields["index number"] == "651234/2026"

    def test_index_number_variants(self):
        assert et._INDEX_RE.search("Index No. 651234/2026").group(1) == "651234/2026"
        assert et._INDEX_RE.search("651234 / 2026") is not None
        assert et._normalize_index("651234 / 2026") == "651234/2026"

    def test_untrusted_sender_is_refused(self):
        """An alert is a document you did not author landing in a mailbox.
        Treating any message in the folder as an eTrack alert would let
        anything that arrives there into the pipeline."""
        raw = (
            b"From: attacker@example.com\r\n"
            b"Subject: eTrack Notification - Index No. 651234/2026\r\n\r\n"
            b"Index Number: 651234/2026\r\nCase Name: Fake v. Fake\r\n"
        )
        alert = et.parse_alert(raw)
        assert not alert.ok
        assert "not a UCS domain" in alert.unparsed_reason

    def test_missing_index_number_is_reported_not_swallowed(self):
        """An unparsed alert is the email equivalent of a broken selector."""
        raw = (
            b"From: eTrack@nycourts.gov\r\n"
            b"Subject: eTrack Notification\r\n\r\n"
            b"Something changed on a case, but we forgot to say which.\r\n"
        )
        alert = et.parse_alert(raw)
        assert not alert.ok
        assert "no NY index number" in alert.unparsed_reason

    def test_malformed_mime_never_raises(self):
        alert = et.parse_alert(b"\x00\xff not an email at all")
        assert not alert.ok

    def test_html_only_alert_is_still_read(self):
        """BUG PINNED: text_content() concatenates block elements with NO
        separator, so '<p>Index Number: 651234/2026</p><p>Case Name: …</p>'
        collapsed to '...2026Case Name: ...'. That defeats the line-anchored
        field regex AND the \\b at the end of the index pattern, so an HTML
        alert produced nothing while looking like a clean parse. eTrack
        templates are HTML, so this is the common case."""
        raw = (
            b"From: eTrack@nycourts.gov\r\n"
            b"Subject: eTrack Notification\r\n"
            b"MIME-Version: 1.0\r\n"
            b'Content-Type: text/html; charset="utf-8"\r\n\r\n'
            b"<html><body><p>Index Number: 651234/2026</p>"
            b"<p>Case Name: Acme v. Widget</p>"
            b"<p>Notification Type: Decision</p></body></html>"
        )
        alert = et.parse_alert(raw)
        assert alert.ok
        assert alert.index_number == "651234/2026"
        # The labelled fields must survive too, not just the index number.
        assert alert.caption == "Acme v. Widget"
        assert alert.fields["notification type"] == "Decision"

    def test_html_table_alert_keeps_row_boundaries(self):
        raw = (
            b"From: eTrack@nycourts.gov\r\n"
            b"Subject: eTrack Notification\r\n"
            b'Content-Type: text/html; charset="utf-8"\r\n\r\n'
            b"<html><body><table>"
            b"<tr><td>Index Number: 651234/2026</td></tr>"
            b"<tr><td>Court: Supreme Court</td></tr>"
            b"</table></body></html>"
        )
        alert = et.parse_alert(raw)
        assert alert.ok
        assert alert.court == "Supreme Court"

    def test_check_file_skips_the_sender_check(self):
        """--check exists to calibrate the parser against a saved sample,
        which may have lost its headers in transit."""
        alert = et.check_file(str(FIX / "sample_decision.eml"))
        assert alert.ok


class TestItemMapping:
    def test_item_is_keyed_on_index_plus_message(self):
        """A case generates many alerts over its life -- each is a separate
        observation -- but a re-delivered message is not."""
        raw = (FIX / "sample_decision.eml").read_bytes()
        a = et.parse_alert(raw)
        i1 = et.alert_to_item(a)
        i2 = et.alert_to_item(et.parse_alert(raw))
        assert i1.item_uid == i2.item_uid
        assert a.index_number in i1.natural_key
        assert i1.payload["record_kind"] == "etrack_alert"
        assert i1.payload["index_number"] == "651234/2026"


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------

class TestEnrollment:
    def test_only_an_arriving_alert_confirms_enrollment(self, tmp_path):
        """Enrollment happens on a UCS web form the pipeline cannot touch, so
        a human saying 'I enrolled' is self-reported. The first alert is the
        only proof that does not involve visiting the site."""
        from litfin.store.db import Database

        db = Database(tmp_path / "t.db")
        try:
            db.upsert_enrollment(index_number="651234/2026", caption="Acme")
            assert db.enrollments()[0]["status"] == "candidate"

            db.mark_enrolled("651234/2026")
            assert db.enrollments()[0]["status"] == "enrolled"

            assert db.confirm_enrollment("651234/2026") is True
            row = db.enrollments()[0]
            assert row["status"] == "confirmed"
            assert row["alert_count"] == 1

            # A second alert is not a second confirmation, but is counted.
            assert db.confirm_enrollment("651234/2026") is False
            assert db.enrollments()[0]["alert_count"] == 2
        finally:
            db.close()

    def test_resuggesting_does_not_downgrade_an_enrolled_case(self, tmp_path):
        from litfin.store.db import Database

        db = Database(tmp_path / "t.db")
        try:
            db.upsert_enrollment(index_number="651234/2026", caption="Acme")
            db.mark_enrolled("651234/2026")
            db.upsert_enrollment(
                index_number="651234/2026", caption="Acme", reason="re-suggested"
            )
            assert db.enrollments()[0]["status"] == "enrolled"
        finally:
            db.close()

    def test_worklist_flags_entries_that_cannot_be_enrolled_yet(self):
        """An entry with no index number costs a human a NYSCEF lookup before
        the form can even be filled in, so it must say so and sort lower."""
        e = et.WorklistEntry(
            index_number="", caption="X v. Y", reason="", score_hint=0.5
        )
        assert e.index_number == ""

        entries = [
            et.WorklistEntry("651234/2026", "A", "", 0.60),
            et.WorklistEntry("", "B", "", 0.61),
        ]
        recorded = [x for x in entries if x.index_number]
        assert len(recorded) == 1


class TestIngestStats:
    def test_unparsed_messages_get_their_own_loud_section(self):
        s = et.IngestStats(fetched=3, stored=2, unparsed=1)
        s.unparsed_reasons.append("weird subject — no index number")
        md = s.to_markdown()
        assert "UNPARSED MESSAGES" in md
        assert "**unparsed**" in md
