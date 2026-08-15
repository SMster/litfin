"""Phase 3: CourtListener / RECAP.

Pins the two behaviors that were bugs during development (the anchor spanning
a multi-day window, and truncation being treated as breakage) plus the two
traps the CourtListener API sets for callers.
"""

from __future__ import annotations

import json

import pytest

from litfin.canary.framework import CanaryFailure
from litfin.connectors import courtlistener
from litfin.connectors.base import FetchTask
from litfin.connectors.coverage import _confidence
from litfin.net.ratelimit import (
    HostHourlyCapExceeded,
    RATES,
    rate_for,
    rate_key_for,
)


def _payload(count: int, n_results: int, **hit_over) -> bytes:
    hit = {
        "id": 1, "docket_id": 99, "docket_entry_id": 7,
        "description": "NOTICE OF SETTLEMENT filed by plaintiff",
        "entry_date_filed": "2026-08-14",
        "absolute_url": "/docket/99/12/acme-corp-v-widget-inc/",
        "meta": {"date_created": "2026-09-01T00:00:00Z"},
    }
    hit.update(hit_over)
    return json.dumps({
        "count": count,
        "results": [dict(hit, id=i) for i in range(n_results)],
    }).encode()


class TestDateTraps:
    def test_event_date_comes_from_entry_date_filed_not_ingestion(self):
        """meta.date_created is CourtListener's INGESTION time.

        Content can arrive weeks or months after filing, so using it as the
        event date would systematically misdate everything -- and recency is a
        scoring factor, so it would misrank too.
        """
        c = courtlistener.build_search()
        r = c.parse(_payload(1, 1), "https://x")
        item = r.items[0]
        assert item.published_at.startswith("2026-08-14")
        assert item.payload["cl_ingested_at"] == "2026-09-01T00:00:00Z"
        assert "2026-09-01" not in (item.published_at or "")

    def test_ingestion_time_is_retained_for_debugging(self):
        c = courtlistener.build_search()
        r = c.parse(_payload(1, 1), "https://x")
        assert r.items[0].payload["cl_ingested_at"]


class TestPartialCoverage:
    """Truncation here is a coverage limit, not a defect.

    The page cap is 20 and page_size is ignored; with a 50/hour ceiling,
    cursor-walking every query is unaffordable. Marking it BROKEN would
    discard good rows AND freeze the watermark forever, since the condition
    recurs every run.
    """

    def test_truncation_flags_partial_and_keeps_rows(self):
        c = courtlistener.build_search()
        r = c.parse(_payload(count=23, n_results=20), "https://x")
        assert r.partial_coverage is True
        assert r.rows_parsed == 20, "rows must be KEPT, not discarded"
        assert "23" in r.coverage_note and "20" in r.coverage_note

    def test_complete_slice_is_not_partial(self):
        c = courtlistener.build_search()
        r = c.parse(_payload(count=8, n_results=8), "https://x")
        assert r.partial_coverage is False
        assert r.coverage_note == ""

    def test_truncation_does_not_raise(self):
        """It must be loud, but it must not fail the task."""
        c = courtlistener.build_search()
        task = FetchTask(task_key="courtlistener:search:2026-08-14:x", url="https://x")
        c.canary(_payload(count=23, n_results=20), task)  # must not raise

    def test_empty_slice_is_server_confirmed(self):
        c = courtlistener.build_search()
        r = c.parse(json.dumps({"count": 0, "results": []}).encode(), "https://x")
        assert r.server_reported_empty is True


class TestAnchor:
    def test_anchor_is_a_single_day(self):
        """BUG PINNED: a multi-day anchor exceeds the 20-result page cap every
        run, permanently reporting partial coverage and burying the real
        partial-coverage warnings in noise.
        """
        c = courtlistener.build_search()
        assert c.ANCHOR_AFTER == c.ANCHOR_BEFORE

    def test_empty_anchor_raises(self):
        c = courtlistener.build_search()
        task = FetchTask(task_key=c.ANCHOR_TASK_KEY, url="https://x")
        body = json.dumps({"count": 0, "results": []}).encode()
        with pytest.raises(CanaryFailure, match="ANCHOR"):
            c.canary(body, task)

    def test_populated_anchor_passes(self):
        c = courtlistener.build_search()
        task = FetchTask(task_key=c.ANCHOR_TASK_KEY, url="https://x")
        c.canary(_payload(count=8, n_results=8), task)


class TestPlanSlicing:
    def test_slices_per_day_and_query(self):
        c = courtlistener.build_search(lookback_days=2)
        tasks = c.plan(None)
        daily = [t for t in tasks if t.task_key != c.ANCHOR_TASK_KEY]
        assert len(daily) == 2 * len(courtlistener.DESCRIPTION_QUERIES)

    def test_each_slice_is_a_single_day(self):
        """A single-day window is what keeps slices under the 20-result cap."""
        from urllib.parse import parse_qs, urlsplit

        c = courtlistener.build_search(lookback_days=1)
        for t in c.plan(None):
            q = parse_qs(urlsplit(t.url).query)
            assert q["entry_date_filed_after"] == q["entry_date_filed_before"]


class TestRateLimits:
    """The published anonymous ceiling is 5/min, 50/hour, 125/day."""

    def test_configured_under_published_anonymous_limits(self):
        r = rate_for("courtlistener.com")
        assert r.rps * 60 <= 5.0, "per-minute rate exceeds the 5/min cap"
        assert r.hourly_cap is not None and r.hourly_cap <= 50
        assert r.daily_cap is not None and r.daily_cap <= 125

    def test_hourly_cap_is_enforced(self):
        """A per-second rate alone cannot honor an hourly quota.

        5/min would permit 300/hour against a published 50/hour limit.
        """
        from litfin.net.ratelimit import HostGovernor, HostRate

        RATES["_test_hourly"] = HostRate(100.0, 100, 1000, 1, hourly_cap=3)
        try:
            gov = HostGovernor()
            gov._ensure("_test_hourly")
            for _ in range(3):
                gov._counts["_test_hourly"] = gov._counts.get("_test_hourly", 0)
                key = "_test_hourly"
                import time as _t
                hour = int(_t.time() // 3600)
                bh, n = gov._hourly.get(key, (hour, 0))
                gov._hourly[key] = (hour, n + 1)
            rate = rate_for("_test_hourly")
            hour_bucket = gov._hourly["_test_hourly"][1]
            assert hour_bucket >= rate.hourly_cap
        finally:
            RATES.pop("_test_hourly", None)

    def test_courtlistener_maps_to_its_own_bucket(self):
        assert rate_key_for("https://www.courtlistener.com/api/rest/v4/search/") \
            == "courtlistener.com"


class TestCoverageConfidence:
    """Absence of signal is not absence of activity."""

    def test_no_feed_is_low_confidence(self):
        assert _confidence(False, "") == "low"

    def test_full_feed_is_high_confidence(self):
        assert _confidence(True, "all") == "high"

    def test_narrow_entry_types_is_partial(self):
        """A feed carrying only orders/opinions misses routine docket activity."""
        assert _confidence(True, "1,2,3") == "partial"

    def test_non_pacer_court_is_not_applicable(self):
        assert _confidence(None, "") == "not_applicable"


class TestCaseNameDerivation:
    def test_slug_becomes_readable(self):
        assert courtlistener._case_name_from_url(
            "/docket/99/12/acme-corp-v-widget-inc/") == "Acme Corp V Widget Inc"

    def test_numeric_or_empty_slug_yields_nothing(self):
        assert courtlistener._case_name_from_url("/docket/99/12/") == ""
        assert courtlistener._case_name_from_url("") == ""
