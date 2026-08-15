"""Digest delivery, with a send gate that is closed by default.

THE GATE IS THE POINT OF THIS MODULE. Everything else here is twenty lines of
smtplib.

`dry_run=True` is the default on the function signature, not a config value
that could be absent, mistyped, or overwritten. A caller must pass
`dry_run=False` *explicitly*, and even then the send is refused unless BOTH:

    deliver.send_enabled = true          in litfin.toml
    deliver.recipient_allowlist = [...]  contains every recipient

Two independent conditions, because each catches a different mistake.
`send_enabled` catches "I did not mean to send anything at all"; the allowlist
catches "I meant to send, but not there." An automated pipeline that mails the
wrong address cannot be un-sent, and the addresses in a litigation-finance
prospect list are exactly the ones you would least like to leak.

A refusal RAISES. It does not warn and continue, and it does not silently fall
back to dry-run: a scheduled job that quietly stops sending looks identical to
a quiet day, which is the same failure the canary system exists to prevent on
the ingestion side.
"""

from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path

from ..config import Config
from .digest import Digest

log = logging.getLogger("litfin.deliver.mailer")


class SendRefused(RuntimeError):
    """A live send was requested but the gate is not open. Never caught here."""


@dataclass(slots=True)
class SendResult:
    dry_run: bool
    recipients: tuple[str, ...]
    html_path: Path | None
    text_path: Path | None
    subject: str
    note: str = ""

    def to_markdown(self) -> str:
        lines = [
            f"| field | value |", "|---|---|",
            f"| mode | {'DRY RUN — nothing sent' if self.dry_run else 'LIVE SEND'} |",
            f"| subject | {self.subject} |",
            f"| recipients | {', '.join(self.recipients) or '(none)'} |",
        ]
        if self.html_path:
            lines.append(f"| rendered html | {self.html_path} |")
        if self.text_path:
            lines.append(f"| rendered text | {self.text_path} |")
        if self.note:
            lines.append(f"| note | {self.note} |")
        return "\n".join(lines)


def _normalize(addr: str) -> str:
    """Compare on the bare address, case-insensitively.

    `parseaddr` strips a display name, so 'Sean <a@b.com>' and 'a@b.com' are
    recognized as the same recipient. Without this an allowlist entry could be
    trivially bypassed by wrapping the address in a display name -- which is
    also precisely how a typo'd address slips past a naive string compare.
    """
    return parseaddr(addr)[1].strip().lower()


def check_gate(cfg: Config, recipients: list[str]) -> None:
    """Raise SendRefused unless every condition for a live send is met."""
    if not cfg.send_enabled:
        raise SendRefused(
            "Live send refused: deliver.send_enabled is false in litfin.toml. "
            "This is the default and it is deliberate. Set it to true only "
            "once you have confirmed the recipient and SMTP settings."
        )
    if not cfg.recipient_allowlist:
        raise SendRefused(
            "Live send refused: deliver.recipient_allowlist is empty. "
            "send_enabled alone is not sufficient — the allowlist is the "
            "second, independent check on WHERE mail goes."
        )
    if not recipients:
        raise SendRefused("Live send refused: no recipients given.")

    allowed = {_normalize(a) for a in cfg.recipient_allowlist}
    rejected = [r for r in recipients if _normalize(r) not in allowed]
    if rejected:
        raise SendRefused(
            f"Live send refused: {', '.join(rejected)} not in "
            f"deliver.recipient_allowlist ({', '.join(sorted(allowed))}). "
            f"Add the address to litfin.toml deliberately — this check exists "
            f"so a scheduled job cannot mail a prospect list somewhere you "
            f"did not intend."
        )


def _write_rendered(
    cfg: Config, digest: Digest, run_id: str
) -> tuple[Path, Path]:
    out = cfg.runs_dir / run_id
    out.mkdir(parents=True, exist_ok=True)
    html_path = out / "digest.html"
    text_path = out / "digest.txt"
    html_path.write_text(digest.html, encoding="utf-8")
    text_path.write_text(digest.text, encoding="utf-8")
    return html_path, text_path


def _build_message(
    digest: Digest, *, sender: str, recipients: list[str]
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = digest.subject
    msg["From"] = formataddr(("LitFin", _normalize(sender)))
    msg["To"] = ", ".join(recipients)
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(digest.text)
    msg.add_alternative(digest.html, subtype="html")
    return msg


def send(
    digest: Digest,
    cfg: Config,
    *,
    dry_run: bool = True,
    recipients: list[str] | None = None,
    run_id: str | None = None,
) -> SendResult:
    """Render to disk always; transmit only when the gate is fully open.

    `dry_run` defaults to True. Do not change that default.
    """
    rid = run_id or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    to = [r.strip() for r in (recipients or list(cfg.recipient_allowlist)) if r.strip()]

    # The rendered files are written in BOTH modes. A live send that is never
    # archived is a report you cannot audit after the fact.
    html_path, text_path = _write_rendered(cfg, digest, rid)

    if dry_run:
        return SendResult(
            dry_run=True,
            recipients=tuple(to),
            html_path=html_path,
            text_path=text_path,
            subject=digest.subject,
            note=(
                "Nothing was transmitted. Open the rendered html to review it. "
                "A live send additionally requires deliver.send_enabled = true "
                "and every recipient in deliver.recipient_allowlist."
            ),
        )

    check_gate(cfg, to)   # raises SendRefused

    host = os.environ.get("LITFIN_SMTP_HOST", "").strip()
    port = int(os.environ.get("LITFIN_SMTP_PORT", "587") or 587)
    user = os.environ.get("LITFIN_SMTP_USER", "").strip()
    password = os.environ.get("LITFIN_SMTP_PASSWORD", "")
    sender = os.environ.get("LITFIN_SMTP_FROM", "").strip() or user

    if not host or not sender:
        raise SendRefused(
            "Live send refused: LITFIN_SMTP_HOST and LITFIN_SMTP_FROM must be "
            "set in the environment (.env). Credentials deliberately do not "
            "live in litfin.toml, which is meant to be committed."
        )

    msg = _build_message(digest, sender=sender, recipients=to)

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.ehlo()
        if smtp.has_extn("starttls"):
            smtp.starttls()
            smtp.ehlo()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)

    log.info("digest sent to %s", ", ".join(to))
    return SendResult(
        dry_run=False,
        recipients=tuple(to),
        html_path=html_path,
        text_path=text_path,
        subject=digest.subject,
        note=f"Transmitted via {host}:{port}.",
    )
