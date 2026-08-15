"""Phase 2 connector tests: DOJ Tunney Act filings and EDGAR full-text search.

Several of these pin behaviors that were BUGS during development. Each such
test names the bug, because the mistake is easy to make again.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from litfin.canary.framework import CanaryFailure, Verdict, classify
from litfin.connectors import doj_cases, sec_fts
from litfin.connectors.base import FetchTask

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(source_id: str, name: str) -> bytes:
    p = FIXTURES / source_id / name
    if not p.is_file():
        pytest.skip(f"fixture missing: {p}")
    return p.read_bytes()


class TestEmptyResultSemantics:
    """A date-sliced query legitimately returns nothing on weekends.

    This is the one place the plain rows_parsed==0 -> BROKEN rule is wrong,
    and getting it wrong pages a human every Saturday.
    """

    def test_server_reported_zero_is_healthy(self):
        r = classify("sec_fts", rows_parsed=0, rows_new=0, not_modified=False,
                     server_reported_empty=True)
        assert r.verdict is Verdict.HEALTHY
        assert "affirmatively" in r.note

    def test_zero_without_server_confirmation_is_still_broken(self):
        """Absence of evidence is NOT evidence of absence."""
        r = classify("sec_fts", rows_parsed=0, rows_new=0, not_modified=False,
                     server_reported_empty=False)
        assert r.verdict is Verdict.BROKEN

    def test_parse_sets_flag_on_explicit_zero(self):
        body = json.dumps({
            "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}
        }).encode()
        result = sec_fts.build().parse(body, "https://efts.sec.gov/x")
        assert result.server_reported_empty is True
        assert result.rows_parsed == 0

    def test_parse_does_not_set_flag_on_malformed_body(self):
        """Junk must NOT be mistaken for a confirmed-empty result."""
        result = sec_fts.build().parse(b"<html>503</html>", "https://efts.sec.gov/x")
        assert result.server_reported_empty is False
        assert result.rows_parsed == 0


class TestTruncationCanaries:
    """Silent truncation is the failure mode this connector is most prone to."""

    def _task(self, key: str = "sec_fts:2026-08-14:8-K:x") -> FetchTask:
        return FetchTask(task_key=key, url="https://efts.sec.gov/x")

    def test_page_truncation_detected(self):
        """total > returned means filings are being silently dropped.

        MEASURED: a combined-form query reported 104 and returned 100.
        """
        body = json.dumps({
            "hits": {"total": {"value": 104, "relation": "eq"},
                     "hits": [{"_id": f"a-{i}:f.htm", "_source": {}} for i in range(100)]}
        }).encode()
        with pytest.raises(CanaryFailure, match="page truncation"):
            sec_fts.build().canary(body, self._task())

    def test_result_ceiling_detected(self):
        body = json.dumps({
            "hits": {"total": {"value": 10000, "relation": "gte"}, "hits": []}
        }).encode()
        with pytest.raises(CanaryFailure, match="saturated"):
            sec_fts.build().canary(body, self._task())

    def test_complete_slice_passes(self):
        body = json.dumps({
            "hits": {"total": {"value": 2, "relation": "eq"},
                     "hits": [{"_id": "a:f", "_source": {}},
                              {"_id": "b:f", "_source": {}}]}
        }).encode()
        sec_fts.build().canary(body, self._task())


class TestAnchor:
    """The anchor is what guards this connector, since empty slices are OK."""

    def test_empty_anchor_fails(self):
        body = json.dumps({
            "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}
        }).encode()
        c = sec_fts.build()
        task = FetchTask(task_key=c.ANCHOR_TASK_KEY, url="https://efts.sec.gov/anchor")
        with pytest.raises(CanaryFailure, match="ANCHOR"):
            c.canary(body, task)

    def test_empty_ordinary_slice_does_not_fail(self):
        """BUG PINNED: the anchor was once detected by sniffing its DATE out of
        the URL, which also matched every ordinary daily slice filed that day
        and failed all of them. Rare phrases legitimately return zero for some
        form/day combinations. Detection must key on task_key.
        """
        body = json.dumps({
            "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}
        }).encode()
        c = sec_fts.build()
        colliding = FetchTask(
            task_key=f'sec_fts:{c.ANCHOR_START}:8-K:"jury verdict"',
            url=(f"https://efts.sec.gov/LATEST/search-index?q=x"
                 f"&startdt={c.ANCHOR_START}&enddt={c.ANCHOR_END}"),
        )
        c.canary(body, colliding)  # must not raise

    def test_anchor_window_is_outside_lookback(self):
        """Keeps the anchor structurally un-confusable with a daily slice."""
        from datetime import date, datetime, timezone

        c = sec_fts.build(lookback_days=7)
        anchor = date.fromisoformat(c.ANCHOR_START)
        today = datetime.now(timezone.utc).date()
        assert (today - anchor).days > 30


class TestFormSlicing:
    def test_plan_slices_by_day_and_form(self):
        c = sec_fts.build(lookback_days=1)
        tasks = c.plan(None)
        daily = [t for t in tasks if t.task_key != c.ANCHOR_TASK_KEY]
        assert len(daily) == len(sec_fts.QUERIES) * len(sec_fts.FORM_SLICES)
        for form in sec_fts.FORM_SLICES:
            assert any(f":{form}:" in t.task_key for t in daily)

    def test_plan_includes_anchor(self):
        c = sec_fts.build(lookback_days=1)
        assert any(t.task_key == c.ANCHOR_TASK_KEY for t in c.plan(None))


class TestDojCaseFilings:
    def test_parses_case_documents(self):
        raw = _fixture("doj_atr_case_filings", "2026-08-15_page0.html")
        result = doj_cases.build().parse(raw, "https://www.justice.gov/atr/x")
        assert result.rows_parsed > 0

    def test_natural_key_is_document_href_not_case(self):
        """Documents accumulate on a case over its life, so a case-level key
        would go stale the moment a proposed judgment was entered. One item
        per document keeps event granularity.
        """
        raw = _fixture("doj_atr_case_filings", "2026-08-15_page0.html")
        result = doj_cases.build().parse(raw, "https://www.justice.gov/atr/x")
        keys = [i.natural_key for i in result.items]
        assert len(keys) == len(set(keys)), "document keys must be unique"
        assert all(k.startswith("/atr/") for k in keys)

    def test_proposed_judgment_not_misread_as_entered(self):
        """The Tunney Act distinction that matters most.

        'proposed final judgment' contains 'final judgment' as a substring. If
        the substring wins, every settlement still inside its 60-day comment
        window is misreported as an entered judgment -- a materially different
        deal.
        """
        assert doj_cases._classify_document(
            "Proposed Final Judgment") == "settlement_proposed"
        assert doj_cases._classify_document(
            "Final Judgment") == "judgment_entered"
        assert doj_cases._classify_document(
            "Competitive Impact Statement") == "settlement_proposed"

    def test_dates_parsed_from_case_open_date(self):
        raw = _fixture("doj_atr_case_filings", "2026-08-15_page0.html")
        result = doj_cases.build().parse(raw, "https://www.justice.gov/atr/x")
        dated = [i for i in result.items if i.published_at]
        assert dated, "no case carried a parsable open date"

    def test_error_page_yields_no_rows(self):
        assert doj_cases.build().parse(b"<html>403</html>", "u").rows_parsed == 0

    def test_only_page_zero_is_planned(self):
        """MEASURED: justice.gov 403s ?page=1 for an identified client."""
        assert len(doj_cases.build().plan(None)) == 1
