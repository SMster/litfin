"""Shared RSS/Atom parsing.

All three Phase 1 sources (SEC Litigation Releases, DOJ Antitrust, FTC) are
RSS 2.0 with near-identical shape, so this is one parser plus three config
blocks -- the highest signal-to-code ratio in the project.

Two live parsing gotchas found during reconnaissance are handled here because
they will bite any feed, not just SEC's:

  1. <link> values can carry trailing whitespace/newlines. SEC's do. Unstripped,
     every URL 404s.
  2. guid isPermaLink="false" can be an opaque UUID that changes. It is NOT
     safe as a stable natural key by default -- each connector declares which
     field is actually stable for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from lxml import etree

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass(slots=True)
class FeedEntry:
    guid: str | None
    guid_is_permalink: bool
    link: str
    title: str
    description: str
    published: str | None      # normalized ISO-8601, UTC
    published_raw: str | None
    creator: str | None        # dc:creator -- carries LR-##### for SEC
    categories: tuple[str, ...]

    def best_key(self, prefer: str = "guid") -> str:
        """Pick a natural key. Connectors override `prefer` per their feed."""
        if prefer == "creator" and self.creator:
            return self.creator
        if prefer == "link" and self.link:
            return self.link
        if self.guid:
            return self.guid
        return self.link or self.title


def parse_feed(raw: bytes) -> list[FeedEntry]:
    """Parse RSS 2.0 or Atom into normalized entries. Pure; no I/O."""
    if not raw.strip():
        return []

    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
    try:
        root = etree.fromstring(raw, parser=parser)
    except etree.XMLSyntaxError:
        return []
    if root is None:
        return []

    entries: list[FeedEntry] = []

    # RSS 2.0
    for item in root.iter("item"):
        entries.append(_rss_item(item))

    # Atom
    if not entries:
        for item in root.iter("{http://www.w3.org/2005/Atom}entry"):
            entries.append(_atom_entry(item))

    return entries


def _rss_item(item: etree._Element) -> FeedEntry:
    guid_el = item.find("guid")
    guid = _clean(guid_el.text) if guid_el is not None else None
    is_permalink = True
    if guid_el is not None:
        attr = (guid_el.get("isPermaLink") or "true").strip().lower()
        is_permalink = attr != "false"

    link = _clean(_text(item, "link"))
    title = _clean(_text(item, "title"))
    desc = _strip_html(_text(item, "description") or _text_ns(item, "content:encoded"))
    pub_raw = _clean(_text(item, "pubDate"))
    creator = _clean(_text_ns(item, "dc:creator"))
    cats = tuple(
        _clean(c.text) for c in item.findall("category") if _clean(c.text)
    )

    return FeedEntry(
        guid=guid,
        guid_is_permalink=is_permalink,
        link=link or "",
        title=title or "",
        description=desc or "",
        published=_normalize_date(pub_raw),
        published_raw=pub_raw,
        creator=creator,
        categories=cats,
    )


def _atom_entry(item: etree._Element) -> FeedEntry:
    link = ""
    for le in item.findall("atom:link", _NS):
        rel = (le.get("rel") or "alternate").lower()
        if rel == "alternate":
            link = _clean(le.get("href")) or ""
            break
    guid = _clean(_text_ns(item, "atom:id"))
    title = _clean(_text_ns(item, "atom:title"))
    summary = _strip_html(
        _text_ns(item, "atom:summary") or _text_ns(item, "atom:content")
    )
    pub_raw = _clean(
        _text_ns(item, "atom:updated") or _text_ns(item, "atom:published")
    )
    return FeedEntry(
        guid=guid,
        guid_is_permalink=False,
        link=link,
        title=title or "",
        description=summary or "",
        published=_normalize_date(pub_raw),
        published_raw=pub_raw,
        creator=None,
        categories=(),
    )


def _text(el: etree._Element, tag: str) -> str | None:
    found = el.find(tag)
    return found.text if found is not None else None


def _text_ns(el: etree._Element, qname: str) -> str | None:
    found = el.find(qname, _NS)
    return found.text if found is not None else None


def _clean(value: str | None) -> str | None:
    """Strip surrounding whitespace INCLUDING newlines.

    This is the SEC <link> trailing-newline fix. Do not remove it.
    """
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _strip_html(value: str | None) -> str | None:
    if not value:
        return None
    text = _TAG_RE.sub(" ", value)
    text = (
        text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    )
    return _WS_RE.sub(" ", text).strip() or None


def _normalize_date(value: str | None) -> str | None:
    """RFC-822 (RSS) or ISO-8601 (Atom) -> ISO-8601 UTC."""
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()
