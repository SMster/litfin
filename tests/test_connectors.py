"""Connector fixture-replay tests.

These are plain pytests ONLY because parse() is pure over bytes. That purity
is what makes fixture replay, canaries, and `litfin replay` all fall out for
free -- keep it.

Fixtures were captured from the live artifact store on 2026-08-15. To turn a
future production regression into a test case, copy the offending artifact
into tests/fixtures/<source_id>/ and assert on it here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from litfin.connectors import feeds
from litfin.connectors.rss import parse_feed

FIXTURES = Path(__file__).parent / "fixtures"


def _load(source_id: str) -> bytes:
    p = FIXTURES / source_id / "2026-08-15_feed.xml"
    if not p.is_file():
        pytest.skip(f"fixture missing: {p}")
    return p.read_bytes()


class TestSecLitrel:
    """SEC has the two gotchas that motivated the shared parser's design."""

    def test_parses_entries(self):
        c = feeds.build("sec_litrel")
        result = c.parse(_load("sec_litrel"), "https://example/feed")
        assert result.rows_parsed > 0

    def test_natural_key_is_release_number_not_opaque_guid(self):
        """dc:creator carries LR-#####; the guid is isPermaLink=false and opaque.

        Keying on the guid would produce a new 'item' every time SEC rotated
        it, silently duplicating the entire feed.
        """
        c = feeds.build("sec_litrel")
        result = c.parse(_load("sec_litrel"), "https://example/feed")
        keys = [i.natural_key for i in result.items]
        assert all(k.startswith("LR-") for k in keys), keys[:5]

    def test_links_have_no_trailing_whitespace(self):
        """SEC <link> values carry a trailing newline; unstripped they 404."""
        c = feeds.build("sec_litrel")
        result = c.parse(_load("sec_litrel"), "https://example/feed")
        for i in result.items:
            assert i.source_url == i.source_url.strip()
            assert "\n" not in i.source_url

    def test_dates_normalized_to_utc_iso(self):
        c = feeds.build("sec_litrel")
        result = c.parse(_load("sec_litrel"), "https://example/feed")
        dated = [i for i in result.items if i.published_at]
        assert dated
        for i in dated:
            assert "T" in i.published_at and i.published_at.endswith("+00:00")


class TestDojAtr:
    def test_parses_entries(self):
        c = feeds.build("doj_atr")
        result = c.parse(_load("doj_atr"), "https://example/feed")
        assert result.rows_parsed > 0

    def test_natural_key_is_permalink_url(self):
        c = feeds.build("doj_atr")
        result = c.parse(_load("doj_atr"), "https://example/feed")
        assert all(
            i.natural_key.startswith("https://") for i in result.items
        )


class TestFtc:
    def test_parses_entries(self):
        c = feeds.build("ftc")
        result = c.parse(_load("ftc"), "https://example/feed")
        assert result.rows_parsed > 0


class TestParserRobustness:
    """A broken/absent selector must yield ZERO rows so the canary fires.

    The parser must NOT invent rows to look healthy, and must not raise on
    junk input -- the runner classifies zero rows as BROKEN, which is the
    behavior we want.
    """

    def test_empty_input_yields_no_rows(self):
        assert parse_feed(b"") == []

    def test_html_error_page_yields_no_rows(self):
        body = b"<html><body><h1>503 Service Unavailable</h1></body></html>"
        assert parse_feed(body) == []

    def test_malformed_xml_does_not_raise(self):
        assert parse_feed(b"<rss><item><title>unclosed") is not None

    def test_valid_feed_with_zero_items_yields_no_rows(self):
        """An empty-but-valid feed is indistinguishable from a broken selector.

        Both give rows_parsed == 0, and both are reported BROKEN. That is the
        correct, conservative behavior: a feed that normally carries 25 items
        suddenly carrying none is worth a human look either way.
        """
        body = b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
        assert parse_feed(body) == []


class TestPurityContract:
    def test_parse_is_deterministic(self):
        """Same bytes in, same items out. No clock, no I/O, no watermark."""
        c = feeds.build("sec_litrel")
        raw = _load("sec_litrel")
        a = c.parse(raw, "https://example/feed")
        b = c.parse(raw, "https://example/feed")
        assert [i.natural_key for i in a.items] == [i.natural_key for i in b.items]
        assert [i.item_uid for i in a.items] == [i.item_uid for i in b.items]

    def test_parse_returns_everything_pre_watermark(self):
        """parse() must NOT filter -- the runner does, so the canary can compare."""
        c = feeds.build("doj_atr")
        result = c.parse(_load("doj_atr"), "https://example/feed")
        assert result.rows_parsed == len(result.items)
