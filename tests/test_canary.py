"""The canary tests.

These are the most important tests in the project. The failure mode they
guard against -- a scraper whose selector stopped matching, silently
returning zero rows and looking exactly like a quiet day -- is the one that
would cause the pipeline to go dead without anyone noticing.
"""

from __future__ import annotations

import pytest

from litfin.canary.framework import (
    CanaryFailure,
    ContentExpectation,
    Verdict,
    classify,
)


class TestDecisionTable:
    """rows_parsed vs rows_new is the whole mechanism."""

    def test_zero_parsed_is_broken_not_quiet(self):
        """THE critical case: 0 parsed on a 200 is BROKEN, never 'no news'."""
        r = classify("x", rows_parsed=0, rows_new=0, not_modified=False)
        assert r.verdict is Verdict.BROKEN
        assert r.is_failure

    def test_parsed_but_none_new_is_healthy(self):
        """The normal quiet day: parser works, watermark filters everything."""
        r = classify("x", rows_parsed=25, rows_new=0, not_modified=False)
        assert r.verdict is Verdict.HEALTHY
        assert not r.is_failure

    def test_parsed_and_new_is_healthy(self):
        r = classify("x", rows_parsed=25, rows_new=10, not_modified=False)
        assert r.verdict is Verdict.HEALTHY
        assert r.rows_new == 10

    def test_304_with_no_body_is_healthy(self):
        """A 304 with nothing to parse is positive evidence of no change."""
        r = classify("x", rows_parsed=0, rows_new=0, not_modified=True,
                     has_body=False)
        assert r.verdict is Verdict.HEALTHY
        assert "304" in r.note

    def test_304_with_cached_body_still_parses_healthy(self):
        r = classify("x", rows_parsed=25, rows_new=0, not_modified=True,
                     has_body=True)
        assert r.verdict is Verdict.HEALTHY

    def test_304_with_cached_body_and_zero_rows_is_broken(self):
        """A parser regression must not hide behind the HTTP cache.

        On a 304 we serve and parse the CACHED body. That body is known-good
        -- it parsed before -- so zero rows means the parser broke, not that
        nothing changed. Caught by an end-to-end test that this file's
        original decision table got wrong.
        """
        r = classify("x", rows_parsed=0, rows_new=0, not_modified=True,
                     has_body=True)
        assert r.verdict is Verdict.BROKEN
        assert "regression" in r.note


class TestStructuralAssertions:
    def test_wrong_content_type_fails(self):
        exp = ContentExpectation(content_types=("application/rss+xml",))
        with pytest.raises(CanaryFailure, match="content-type"):
            exp.assert_ok("x", body=b"<html>oops</html>", content_type="text/html")

    def test_undersized_body_fails(self):
        """A 3 KB response from a normally-327 KB page is an error page."""
        exp = ContentExpectation(min_bytes=1000)
        with pytest.raises(CanaryFailure, match="below the expected floor"):
            exp.assert_ok("x", body=b"tiny", content_type="text/html")

    def test_missing_marker_fails(self):
        exp = ContentExpectation(must_contain=("<item",))
        with pytest.raises(CanaryFailure, match="expected marker"):
            exp.assert_ok("x", body=b"<rss></rss>", content_type="text/xml")

    def test_missing_anchor_fails(self):
        """Anchors validate query SEMANTICS, not just reachability.

        A changed filter parameter can return HTTP 200 with well-formed but
        silently-narrowed output. Only an anchor catches that.
        """
        exp = ContentExpectation(anchors=("LR-26000",))
        with pytest.raises(CanaryFailure, match="anchor record"):
            exp.assert_ok("x", body=b"<rss><item/></rss>", content_type="text/xml")

    def test_valid_body_passes(self):
        exp = ContentExpectation(
            content_types=("application/rss+xml",),
            min_bytes=10,
            must_contain=("<item",),
        )
        exp.assert_ok(
            "x",
            body=b"<rss><item><title>hi</title></item></rss>",
            content_type="application/rss+xml",
        )
