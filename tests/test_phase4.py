"""Phase 4: govinfo, EDGAR daily index, state AG feeds, JPML.

Three of these pin bugs found by running against live data, and two pin
deliberate design decisions that look like omissions if you don't know why.
"""

from __future__ import annotations

import json

import pytest

from litfin.canary.framework import CanaryFailure
from litfin.connectors import edgar_index, govinfo, jpml, state_ag
from litfin.connectors.base import FetchTask


# ---------------------------------------------------------------------------
# EDGAR daily index
# ---------------------------------------------------------------------------

# The header really is wrapped across two physical lines. Reproduced exactly.
IDX = (
    "Description:           Daily Index of EDGAR Dissemination Feed by Form Type\n"
    "Last Data Received:    Aug 14, 2026\n"
    " \n"
    "Form Type   Company Name                                                  CIK\n"
    "      Date Filed  File Name\n"
    "---------------------------------------------------------------------------\n"
    "8-K         ACME CORP                                                     1234567     "
    "20260814    edgar/data/1234567/0001234567-26-000001.txt\n"
    "10-Q        WIDGET INC                                                    7654321     "
    "20260814    edgar/data/7654321/0007654321-26-000002.txt\n"
    "1-A         IRRELEVANT LLC                                                1111111     "
    "20260814    edgar/data/1111111/0001111111-26-000003.txt\n"
).encode()


class TestEdgarIndexParsing:
    def test_parses_rows_despite_wrapped_header(self):
        """BUG PINNED: the header spans TWO physical lines --
            'Form Type   Company Name ... CIK'
            '      Date Filed  File Name'
        so deriving column offsets from "the header row" only ever captured
        three of five fields and produced zero rows. Rows are matched by
        SHAPE instead.
        """
        r = edgar_index.build().parse(IDX, "https://x")
        assert r.rows_parsed == 2, "8-K and 10-Q should parse; 1-A filtered"

    def test_only_forms_of_interest_kept(self):
        r = edgar_index.build().parse(IDX, "https://x")
        forms = {i.payload["form"] for i in r.items}
        assert forms == {"8-K", "10-Q"}

    def test_fields_extracted_correctly(self):
        r = edgar_index.build().parse(IDX, "https://x")
        item = next(i for i in r.items if i.payload["form"] == "8-K")
        assert item.payload["cik"] == "1234567"
        assert item.payload["company"] == "ACME CORP"
        assert item.payload["date_filed"] == "20260814"
        assert item.source_url.endswith("0001234567-26-000001.txt")

    def test_natural_key_is_the_archive_path(self):
        r = edgar_index.build().parse(IDX, "https://x")
        assert all(i.natural_key.startswith("edgar/data/") for i in r.items)

    def test_no_synthetic_event_language(self):
        """The index carries no document text. Inventing words like
        'settlement' here would manufacture a signal the source lacks."""
        r = edgar_index.build().parse(IDX, "https://x")
        for i in r.items:
            low = i.body.lower()
            assert "judgment" not in low and "settlement" not in low

    def test_canary_rejects_a_short_or_wrong_body(self):
        with pytest.raises(CanaryFailure):
            edgar_index.build().canary(
                b"<html>error</html>", FetchTask(task_key="t", url="u")
            )

    def test_canary_rejects_a_format_change(self):
        """Header present but rows unmatched = the row format moved."""
        broken = (
            "Form Type   Company Name   CIK\n"
            "      Date Filed  File Name\n"
            "-----\n"
        ) + "\n".join(f"garbage row {i}" for i in range(200))
        with pytest.raises(CanaryFailure, match="row format"):
            edgar_index.build().canary(
                broken.encode(), FetchTask(task_key="t", url="u")
            )

    def test_weekend_days_are_not_planned(self):
        """EDGAR publishes nothing on weekends; planning those tasks would
        generate guaranteed 404s."""
        import datetime as dt

        tasks = edgar_index.build(lookback_days=7).plan(None)
        for t in tasks:
            day = dt.date.fromisoformat(t.task_key.split(":")[-1])
            assert day.weekday() < 5


# ---------------------------------------------------------------------------
# govinfo
# ---------------------------------------------------------------------------

def _gov(pid: str, title: str = "Some v. Case") -> bytes:
    return json.dumps({
        "count": 1,
        "packages": [{
            "packageId": pid, "title": title,
            "dateIssued": "2026-08-14",
            "lastModified": "2026-08-15T02:00:00Z",
            "docClass": "USCOURTS",
            "packageLink": f"https://api.govinfo.gov/packages/{pid}/summary",
        }],
    }).encode()


class TestGovinfo:
    def test_case_type_extracted_from_package_id(self):
        """The case number embeds its own type, so civil-vs-criminal costs
        zero extra requests -- the alternative is one metadata call PER
        PACKAGE at ~2,000 packages/day."""
        court, ctype, caseno = govinfo._parse_package_id(
            "USCOURTS-caed-2_22-cv-00177")
        assert court == "caed" and ctype == "cv"
        assert "cv" in caseno

    def test_criminal_filtered_out(self):
        c = govinfo.GovinfoConnector(courts=frozenset({"caed"}), civil_only=True)
        r = c.parse(_gov("USCOURTS-caed-2_22-cr-00136", "USA v. Hernandez"), "u")
        assert r.rows_parsed == 0
        assert "criminal" in r.note

    def test_civil_kept(self):
        c = govinfo.GovinfoConnector(courts=frozenset({"caed"}), civil_only=True)
        r = c.parse(_gov("USCOURTS-caed-2_22-cv-00177"), "u")
        assert r.rows_parsed == 1

    def test_out_of_scope_court_filtered(self):
        c = govinfo.GovinfoConnector(courts=frozenset({"nysd"}), civil_only=True)
        r = c.parse(_gov("USCOURTS-caed-2_22-cv-00177"), "u")
        assert r.rows_parsed == 0

    def test_marked_as_index_not_event(self):
        """Deliberate: package titles are bare case names with no event
        language, so these must NOT masquerade as deal signals."""
        c = govinfo.GovinfoConnector(courts=frozenset(), civil_only=True)
        r = c.parse(_gov("USCOURTS-nysd-1_24-cv-09999"), "u")
        assert r.items[0].payload["record_kind"] == "opinion_index"

    def test_index_items_carry_no_deal_signal(self):
        """The screen must drop these rather than spend extraction budget."""
        from litfin.score.taxonomy import Thesis, classify_text

        c = govinfo.GovinfoConnector(courts=frozenset(), civil_only=True)
        r = c.parse(_gov("USCOURTS-nysd-1_24-cv-09999", "Smith v. Jones"), "u")
        item = r.items[0]
        assert classify_text(f"{item.title}\n{item.body}").thesis is Thesis.NONE

    def test_empty_page_after_filtering_is_not_broken(self):
        """The collection is ordered by lastModified and one busy court can
        fill an entire page with cases we deliberately skip."""
        c = govinfo.GovinfoConnector(courts=frozenset({"nysd"}), civil_only=True)
        r = c.parse(_gov("USCOURTS-caed-2_22-cr-00001"), "u")
        assert r.rows_parsed == 0
        assert r.server_reported_empty is True


# ---------------------------------------------------------------------------
# State AG
# ---------------------------------------------------------------------------

class TestStateAg:
    def test_only_verified_feeds_are_planned(self):
        c = state_ag.build()
        feeds, blocked = state_ag.load_feeds()
        assert len(c.plan(None)) == len(feeds)
        assert len(feeds) >= 5

    def test_blocked_states_recorded_not_silently_dropped(self):
        """NY and TX are the two highest-value states and both are dark.
        Recording WHY stops anyone re-probing them blindly."""
        _, blocked = state_ag.load_feeds()
        states = {b["state"] for b in blocked}
        assert {"NY", "TX"} <= states
        for b in blocked:
            assert b.get("reason") and b.get("urls_tried")

    def test_coverage_note_is_honest(self):
        note = state_ag.build().coverage_note
        assert "of 50" in note
        assert "NY" in note and "TX" in note

    def test_natural_key_namespaced_by_state(self):
        """Two AGs announcing the same multistate settlement are separate
        observations; collapsing them would lose which states participated."""
        c = state_ag.build()
        feed_url = c.feeds[0].url
        rss = (
            '<?xml version="1.0"?><rss version="2.0"><channel>'
            '<item><title>AG announces settlement</title>'
            '<link>https://x/1</link><guid>abc</guid></item>'
            "</channel></rss>"
        ).encode()
        r = c.parse(rss, feed_url)
        assert r.items[0].natural_key.startswith(f"{c.feeds[0].state}:")


# ---------------------------------------------------------------------------
# JPML
# ---------------------------------------------------------------------------

LANDING_HTML = (
    '<html><body><a href="/sites/jpml/files/'
    'Pending_MDL_Dockets_By_MDL_Number-August-3-2026.pdf">As of August 3, 2026</a>'
    '<a href="/sites/jpml/files/Pending_MDL_Dockets_By_District-August-3-2026.pdf">x</a>'
    "</body></html>"
).encode()


class TestJpml:
    def test_landing_page_discovers_the_pdf_url(self):
        """Neither /pending-mdls nor /pending-mdls-0 contains MDL data; the
        list is a monthly PDF whose dated filename is not derivable."""
        c = jpml.build()
        r = c.parse(LANDING_HTML, jpml.LANDING)
        assert r.rows_parsed == 0
        assert c.plan_watermark().endswith(
            "Pending_MDL_Dockets_By_MDL_Number-August-3-2026.pdf")

    def test_prefers_by_mdl_number_report(self):
        c = jpml.build()
        c.parse(LANDING_HTML, jpml.LANDING)
        assert "By_MDL_Number" in c.plan_watermark()

    def test_landing_page_is_not_broken_despite_zero_rows(self):
        """It is a pointer page; its job is done when it yields a URL."""
        r = jpml.build().parse(LANDING_HTML, jpml.LANDING)
        assert r.server_reported_empty is True

    def test_plan_adds_pdf_task_once_url_is_known(self):
        c = jpml.build()
        assert len(c.plan(None)) == 1
        tasks = c.plan("https://www.jpml.uscourts.gov/x.pdf")
        assert len(tasks) == 2
        assert any(t.task_key == jpml.TASK_PDF for t in tasks)

    def test_canary_regex_is_not_end_anchored(self):
        """BUG PINNED: the pattern was `...\\.pdf$`, which matches an href but
        can NEVER match inside a whole HTML document -- so the canary failed
        on every run even though the link was present.
        """
        assert jpml._WANTED_PDF.search(LANDING_HTML.decode()) is not None

    def test_canary_fails_when_link_absent(self):
        with pytest.raises(CanaryFailure, match="PDF link"):
            jpml.build().canary(
                b"<html><body>nothing here</body></html>",
                FetchTask(task_key=jpml.TASK_LANDING, url="u"),
            )

    def test_canary_rejects_non_pdf_for_the_pdf_task(self):
        with pytest.raises(CanaryFailure, match="expected a PDF"):
            jpml.build().canary(
                b"<html>redirected</html>",
                FetchTask(task_key=jpml.TASK_PDF, url="u"),
            )

    def test_parse_branches_on_content_not_task(self):
        """Keeps parse() pure: it dispatches on the bytes, never on which
        FetchTask produced them, so fixture replay works for both stages."""
        import inspect
        src = inspect.getsource(jpml.JpmlConnector.parse)
        # Strip the docstring before inspecting the code itself.
        body = src.split('"""')[-1]
        assert "%PDF-" in body
        assert "task" not in body, "parse() must not consult task identity"
