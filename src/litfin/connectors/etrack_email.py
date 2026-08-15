"""Phase 6 -- NY eTrack alert email ingestion.

New York's Commercial Division is one of the two most important commercial
benches in the country, and its decisions are unavailable free by any other
route. Scraping NY UCS is PERMANENTLY PROHIBITED here: its bot clause is
unconditional -- automated access for data extraction is barred "for any use"
-- and the site returns HTTP 200 to bots, which makes technical accessibility
a trap rather than an invitation.

eTrack is the lawful path. UCS emails alerts to subscribers who enrolled in
specific cases; receiving mail you subscribed to is not "accessing the site by
an automated program."

THIS MODULE IS DISABLED BY DEFAULT AND REQUIRES TWO SEPARATE DECISIONS.

  1. A legal one. `etrack_email` is RESTRICTED, not permitted. The residual
     question is the SEPARATE "may not be mined" clause, which is broad enough
     that a conservative reading could reach automated parsing of alerts you
     subscribed to. Lower stakes under a research purpose -- but it is a
     decision to make deliberately, not one to arrive at by running a command.
     Recording it means setting `[etrack] decision_recorded` in litfin.toml to
     a non-empty string naming who decided and when.

  2. An operational one. `[etrack] enabled = true`, plus a dedicated mailbox
     and IMAP credentials in the environment. Use a mailbox that receives
     NOTHING ELSE.

Both are required. `assert_enabled()` raises otherwise and is called before
any connection is opened.

ENROLLMENT IS MANUAL AND CANNOT BE AUTOMATED. A human fills in a UCS web form
per case. The pipeline's whole job around that is to produce a short ranked
worklist of index numbers worth the effort, and to mark one confirmed when its
first alert actually arrives -- which is the only proof of enrollment that
does not involve touching the site.

THE PARSER HAS NOT BEEN CALIBRATED AGAINST A REAL ALERT, and pretending
otherwise would be the exact false confidence this codebase is built to avoid.
The patterns below are written from eTrack's documented notification content.
Before enabling ingestion, save one real alert and run:

    litfin etrack --check path/to/alert.eml

which prints exactly what the parser extracted and what it missed. Messages
whose index number cannot be found are counted and reported, never silently
dropped -- an unparsed alert is the email equivalent of a broken selector.
"""

from __future__ import annotations

import email
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message
from typing import Iterable, Sequence

from ..store.db import Item, make_item_uid

log = logging.getLogger("litfin.etrack")

SOURCE_ID = "etrack_email"

# NY index numbers: six digits, a slash, a four-digit year -- "651234/2026".
# Commercial Division matters in NY County are overwhelmingly 65xxxx/YYYY.
# The label is optional because alert subjects often carry the bare number.
_INDEX_RE = re.compile(
    r"\b(?:index\s*(?:no\.?|number)?\s*[:#]?\s*)?"
    r"(\d{3,7}\s*/\s*(?:19|20)\d{2})\b",
    re.IGNORECASE,
)

# Anything with a colon-delimited label; eTrack alerts are label-driven text.
_FIELD_RE = re.compile(
    r"^\s*(index number|index no\.?|case name|caption|court|county|"
    r"appearance date|appearance type|justice|judge|document|"
    r"date filed|filed|efiling status|status|motion|notification type)"
    r"\s*[:\-]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Words that identify what the alert is telling you. Order matters: a decision
# is the reason this source exists, so it wins over a bare appearance notice.
_EVENT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("decision", "decision"),
    ("order", "decision"),
    ("judgment", "judgment"),
    ("disposed", "disposition"),
    ("disposition", "disposition"),
    ("motion", "motion"),
    ("appearance", "appearance"),
    ("document", "filing"),
    ("filing", "filing"),
)

# Only mail actually from UCS is parsed. An alert is a document you did not
# author arriving in a mailbox -- treating any message in the folder as an
# eTrack alert would let anything that lands there into the pipeline.
TRUSTED_SENDER_DOMAINS = ("nycourts.gov", "courts.state.ny.us")


class EtrackDisabled(RuntimeError):
    """Raised instead of connecting. Never caught inside this module."""


@dataclass(slots=True)
class EtrackAlert:
    """One parsed alert. `index_number` is the identity; without it, nothing."""

    index_number: str = ""
    caption: str = ""
    court: str = ""
    county: str = ""
    event_kind: str = ""
    event_date: str = ""
    subject: str = ""
    sender: str = ""
    message_id: str = ""
    received_at: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    body_text: str = ""
    unparsed_reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.index_number)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def assert_enabled(cfg) -> None:
    """Both decisions, checked before anything opens a socket."""
    if not getattr(cfg, "etrack_enabled", False):
        raise EtrackDisabled(
            "eTrack ingestion is disabled. It is off by default because "
            "etrack_email is RESTRICTED, not permitted: the 'may not be "
            "mined' clause in the UCS terms is broad enough that a "
            "conservative reading could reach automated parsing of alerts "
            "you subscribed to.\n\n"
            "To enable, set BOTH in litfin.toml:\n"
            "  [etrack]\n"
            "  decision_recorded = \"<who decided, and when>\"\n"
            "  enabled = true\n"
            "and put IMAP credentials in .env (LITFIN_IMAP_HOST, "
            "LITFIN_IMAP_USER, LITFIN_IMAP_PASSWORD)."
        )
    if not (getattr(cfg, "etrack_decision_recorded", "") or "").strip():
        raise EtrackDisabled(
            "eTrack ingestion is enabled but [etrack].decision_recorded is "
            "empty. The operational switch alone is not sufficient — the "
            "point of the second field is that somebody consciously decided "
            "the 'may not be mined' question and left their name on it. "
            "Record it in litfin.toml."
        )


# ---------------------------------------------------------------------------
# Pure parsing -- bytes in, one alert out. No I/O, no clock, no watermark.
# ---------------------------------------------------------------------------

def _decoded_body(msg: Message) -> str:
    """Plain text if offered, otherwise HTML stripped to text."""
    parts: list[str] = []
    html_parts: list[str] = []

    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        (parts if ctype == "text/plain" else html_parts).append(text)

    if parts:
        return "\n".join(parts)
    if html_parts:
        return "\n".join(_html_to_text(h) for h in html_parts)
    return ""


# Block-level tags whose boundaries are line breaks once the markup is gone.
_BLOCK_TAGS = (
    "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "blockquote", "section",
)
_BLOCK_RE = re.compile(
    r"</?(?:" + "|".join(_BLOCK_TAGS) + r")\b[^>]*>", re.IGNORECASE
)


def _html_to_text(html: str) -> str:
    """HTML -> text WITH block boundaries preserved as newlines.

    BUG PINNED: this used to be `lxml.html.fromstring(h).text_content()`,
    which concatenates block elements with no separator at all --
    "<p>Index Number: 651234/2026</p><p>Case Name: Acme</p>" collapses to
    "Index Number: 651234/2026Case Name: Acme".

    That breaks eTrack parsing in two ways at once. The field regex is
    line-anchored, so no label is ever recognized; and the index-number regex
    ends in \\b, which a following letter defeats -- so an HTML alert yields
    NOTHING while looking like a clean parse. eTrack templates are HTML, so
    this is the common case, not the edge case.
    """
    # Insert the breaks before stripping tags, so the structure survives.
    spaced = _BLOCK_RE.sub("\n", html)
    try:
        from lxml import html as LH

        text = LH.fromstring(spaced).text_content()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", spaced)

    # Collapse runs of blank lines but keep single ones: the parser needs the
    # line structure and nothing else.
    lines = [" ".join(l.split()) for l in text.splitlines()]
    return "\n".join(l for l in lines if l)


# Label spellings that mean the same field, mapped explicitly.
#
# BUG PINNED: this used to be `key.replace("no", "number")`, which turned
# "notification type" into "numbertification type" -- a substring replace
# applied to a whole key, corrupting every label containing the letters "no".
# Whole-key mapping cannot do that.
_LABEL_ALIASES = {
    "index no": "index number",
    "index": "index number",
    "caption": "case name",
    "filed": "date filed",
    "judge": "justice",
}


def _canonical_label(raw: str) -> str:
    key = " ".join((raw or "").lower().replace(".", " ").split())
    return _LABEL_ALIASES.get(key, key)


def _normalize_index(raw: str) -> str:
    """'651234 / 2026' and 'Index No. 651234/2026' both -> '651234/2026'."""
    return re.sub(r"\s+", "", raw or "")


def _sender_domain(addr: str) -> str:
    m = re.search(r"@([A-Za-z0-9.\-]+)", addr or "")
    return m.group(1).lower() if m else ""


def _event_kind(blob: str) -> str:
    low = blob.lower()
    for needle, kind in _EVENT_PATTERNS:
        if needle in low:
            return kind
    return "notification"


def _parse_date(raw: str) -> str:
    raw = (raw or "").strip()
    for fmt in ("%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def parse_alert(raw: bytes, *, require_trusted_sender: bool = True) -> EtrackAlert:
    """PURE: one RFC822 message -> one EtrackAlert.

    Never raises on malformed input. A message it cannot understand comes back
    with `ok == False` and `unparsed_reason` set, so the caller can COUNT the
    failures instead of losing them.
    """
    try:
        msg = email.message_from_bytes(raw)
    except Exception as exc:                                    # noqa: BLE001
        return EtrackAlert(unparsed_reason=f"unparsable MIME: {exc}")

    subject = str(msg.get("Subject") or "").replace("\n", " ").strip()
    sender = str(msg.get("From") or "").strip()
    alert = EtrackAlert(
        subject=subject,
        sender=sender,
        message_id=str(msg.get("Message-ID") or "").strip(),
        received_at=str(msg.get("Date") or "").strip(),
    )

    domain = _sender_domain(sender)
    if require_trusted_sender and not any(
        domain == d or domain.endswith("." + d) for d in TRUSTED_SENDER_DOMAINS
    ):
        alert.unparsed_reason = (
            f"sender domain {domain or '(none)'} is not a UCS domain "
            f"({', '.join(TRUSTED_SENDER_DOMAINS)}); refusing to treat this "
            f"as an eTrack alert"
        )
        return alert

    body = _decoded_body(msg)
    alert.body_text = body

    alert.fields = fields = {
        _canonical_label(k): v.strip() for k, v in _FIELD_RE.findall(body)
    }

    # The index number is the identity. Look in the labelled field first, then
    # anywhere in subject or body -- eTrack has more than one template and the
    # subject line often carries it alone.
    idx = ""
    for key in ("index number", "index"):
        if fields.get(key):
            m = _INDEX_RE.search(fields[key])
            if m:
                idx = m.group(1)
                break
    if not idx:
        m = _INDEX_RE.search(subject) or _INDEX_RE.search(body)
        if m:
            idx = m.group(1)

    if not idx:
        alert.unparsed_reason = (
            "no NY index number (\\d+/YYYY) found in subject or body. Either "
            "the alert template changed or this is not an eTrack alert."
        )
        return alert

    alert.index_number = _normalize_index(idx)
    alert.caption = (
        fields.get("case name") or fields.get("caption") or ""
    ).strip()
    alert.court = (fields.get("court") or "").strip()
    alert.county = (fields.get("county") or "").strip()
    alert.event_kind = _event_kind(f"{subject} {body[:800]}")
    alert.event_date = (
        _parse_date(fields.get("appearance date", ""))
        or _parse_date(fields.get("date filed", ""))
        or _parse_date(fields.get("filed", ""))
    )
    return alert


def alert_to_item(alert: EtrackAlert, *, source_url: str = "") -> Item:
    """One alert -> one Item, keyed on index number plus message id.

    Keyed on BOTH because a case generates many alerts over its life and each
    is a separate observation, but a re-delivered message is not.
    """
    key = f"{alert.index_number}:{alert.message_id or alert.subject[:80]}"
    caption = alert.caption or alert.subject or alert.index_number
    return Item(
        source_id=SOURCE_ID,
        natural_key=key,
        title=f"[NY {alert.index_number}] {caption}"[:300],
        body=(
            f"{alert.subject}\n\n{alert.body_text}"
        )[:8000],
        source_url=source_url,
        published_at=alert.event_date or None,
        payload={
            "record_kind": "etrack_alert",
            "index_number": alert.index_number,
            "caption": alert.caption,
            "court": alert.court,
            "county": alert.county,
            "event_kind": alert.event_kind,
            "event_date": alert.event_date,
            "subject": alert.subject,
            "message_id": alert.message_id,
            "fields": alert.fields,
        },
    )


# ---------------------------------------------------------------------------
# Enrollment worklist
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class WorklistEntry:
    index_number: str
    caption: str
    reason: str
    score_hint: float = 0.0


# Signals that a stored prospect is a NY state matter worth enrolling. The
# federal courts are already covered by RECAP, so a NY *federal* case is not a
# reason to spend a manual enrollment.
_NY_STATE_HINTS = (
    "commercial division",
    "supreme court of the state of new york",
    "new york county",
    "kings county",
    "westchester county",
    "nassau county",
    "suffolk county",
    "erie county",
    "n.y. sup",
    "nyscef",
)


def build_worklist(db, *, limit: int = 25) -> list[WorklistEntry]:
    """Rank NY state matters already in the corpus that are worth enrolling.

    Deliberately conservative: enrollment costs a human a web form per case,
    so a long list is worse than a short one. Anything already carrying an
    index number ranks first, because those can be enrolled immediately.
    """
    out: list[WorklistEntry] = []
    seen: set[str] = set()

    rows = db.top_prospects(limit=400)
    for r in rows:
        blob = " ".join(
            str(r[k] or "")
            for k in ("case_caption", "court", "venue", "jurisdiction", "summary")
        ).lower()
        if not any(h in blob for h in _NY_STATE_HINTS):
            continue

        payload_blob = f"{r['case_caption'] or ''} {r['summary'] or ''}"
        m = _INDEX_RE.search(payload_blob)
        index_number = _normalize_index(m.group(1)) if m else ""
        key = index_number or (r["case_caption"] or r["item_title"] or "")[:80]
        if not key or key in seen:
            continue
        seen.add(key)

        matched = next(h for h in _NY_STATE_HINTS if h in blob)
        out.append(
            WorklistEntry(
                index_number=index_number,
                caption=(r["case_caption"] or r["item_title"] or "").strip(),
                reason=(
                    f"NY state signal: {matched}"
                    + ("" if index_number else "; index number NOT found — "
                       "look it up on NYSCEF by hand before enrolling")
                ),
                # An entry with no index number cannot be enrolled without
                # more work, so it sorts below one that can.
                score_hint=float(r["score"] or 0) + (0.05 if index_number else 0.0),
            )
        )

    out.sort(key=lambda e: -e.score_hint)
    return out[:limit]


def record_candidates(db, entries: Iterable[WorklistEntry]) -> int:
    """Persist worklist entries that carry an index number."""
    n = 0
    for e in entries:
        if not e.index_number:
            continue
        if db.upsert_enrollment(
            index_number=e.index_number,
            caption=e.caption,
            reason=e.reason,
            score_hint=e.score_hint,
        ):
            n += 1
    return n


# ---------------------------------------------------------------------------
# IMAP ingestion -- gated
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class IngestStats:
    fetched: int = 0
    stored: int = 0
    duplicates: int = 0
    unparsed: int = 0
    confirmed: int = 0
    unparsed_reasons: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            "| stage | count |", "|---|---:|",
            f"| messages fetched | {self.fetched} |",
            f"| alerts stored | {self.stored} |",
            f"| already seen | {self.duplicates} |",
            f"| **unparsed** | **{self.unparsed}** |",
            f"| enrollments confirmed | {self.confirmed} |",
        ]
        if self.unparsed_reasons:
            # An unparsed alert is the email equivalent of a broken selector.
            # It gets its own loud section rather than a log line.
            lines += ["", "## UNPARSED MESSAGES", ""]
            for r in self.unparsed_reasons[:12]:
                lines.append(f"- {r}")
        return "\n".join(lines)


def ingest(cfg, db, *, folder: str = "INBOX", limit: int = 200,
           mark_seen: bool = True) -> IngestStats:
    """Fetch unseen alerts over IMAP, store them, confirm enrollments.

    Raises EtrackDisabled unless BOTH switches are set. That check runs before
    any credential is read or any socket is opened.
    """
    import imaplib
    import os

    assert_enabled(cfg)

    host = os.environ.get("LITFIN_IMAP_HOST", "").strip()
    user = os.environ.get("LITFIN_IMAP_USER", "").strip()
    password = os.environ.get("LITFIN_IMAP_PASSWORD", "")
    if not (host and user and password):
        raise EtrackDisabled(
            "eTrack ingestion needs LITFIN_IMAP_HOST, LITFIN_IMAP_USER and "
            "LITFIN_IMAP_PASSWORD in the environment (.env). Use a DEDICATED "
            "mailbox that receives nothing else."
        )

    stats = IngestStats()
    items: list[Item] = []
    seen_keys: list[str] = []

    conn = imaplib.IMAP4_SSL(host)
    try:
        conn.login(user, password)
        conn.select(folder)
        typ, data = conn.search(None, "UNSEEN")
        if typ != "OK":
            return stats

        ids = (data[0] or b"").split()[:limit]
        for msg_id in ids:
            typ, payload = conn.fetch(msg_id, "(RFC822)")
            if typ != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            raw = payload[0][1]
            stats.fetched += 1

            alert = parse_alert(raw)
            if not alert.ok:
                stats.unparsed += 1
                stats.unparsed_reasons.append(
                    f"{alert.subject[:70] or '(no subject)'} — "
                    f"{alert.unparsed_reason}"
                )
                # Deliberately NOT marked seen: an unparsed message stays in
                # the mailbox so it can be inspected and replayed once the
                # parser is fixed.
                continue

            item = alert_to_item(alert)
            uid = make_item_uid(SOURCE_ID, item.natural_key)
            if uid in seen_keys:
                stats.duplicates += 1
                continue
            items.append(item)
            seen_keys.append(item.natural_key)

            if db.confirm_enrollment(alert.index_number):
                stats.confirmed += 1

            if mark_seen:
                conn.store(msg_id, "+FLAGS", "\\Seen")
    finally:
        try:
            conn.close()
        except Exception:
            pass
        conn.logout()

    if items:
        run_id = datetime.now(timezone.utc).strftime("etrack_%Y%m%dT%H%M%S")
        db.start_run(run_id, str(cfg.purpose), note="etrack email ingestion")
        stats.stored = db.commit_task(
            run_id=run_id,
            task_id=f"{SOURCE_ID}:inbox",
            source_id=SOURCE_ID,
            task_key=f"{SOURCE_ID}:inbox",
            items=items,
            watermark_value=datetime.now(timezone.utc).isoformat(),
            seen_keys=seen_keys,
            rows_parsed=len(items),
            rows_new=len(items),
        )
        db.finish_run(run_id, "ok")

    return stats


def check_file(path: str) -> EtrackAlert:
    """Parse one saved .eml and report exactly what came out.

    This is the calibration tool. The parser has not been run against a real
    alert; save one, point this at it, and fix the patterns before enabling
    ingestion.
    """
    with open(path, "rb") as fh:
        return parse_alert(fh.read(), require_trusted_sender=False)
