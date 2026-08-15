"""JPML -- Judicial Panel on Multidistrict Litigation.

Value: MDL formation is the middle link in the antitrust follow-on chain.
DOJ/FTC enforcement is the leading indicator; a JPML transfer order
consolidating private suits confirms follow-on damages litigation is actually
underway; class settlement approval is the payoff. This supplies the middle
step, outside PACER.

THE LIST IS A DATED PDF, and that shapes the design.

Neither /pending-mdls nor /pending-mdls-0 contains any MDL data -- both are
navigation pages that return 200 with zero MDL numbers (the canary caught
exactly this). The actual list is published monthly as a PDF whose filename
carries its own date:

    /sites/jpml/files/Pending_MDL_Dockets_By_MDL_Number-August-3-2026.pdf

The date is the panel's publication date and is not derivable, so the URL
cannot be known when plan() runs. This connector therefore works in two
stages, using the plan-watermark the orchestrator now threads through:

    run 1: fetch the landing page, extract the current PDF URL, store it
    run 2: fetch that PDF, parse the MDL table out of it

A one-run warm-up on a WEEKLY source is a fair price for keeping the pure
parse contract intact -- and each run re-reads the landing page, so when the
panel publishes a new monthly PDF the URL updates on its own.
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from typing import Sequence

from lxml import html as LH

from ..canary.framework import CanaryFailure, ContentExpectation
from ..store.db import Item
from .base import FetchTask, ParseResult

BASE = "https://www.jpml.uscourts.gov"
LANDING = f"{BASE}/pending-mdls-0"

TASK_LANDING = "jpml:landing"
TASK_PDF = "jpml:pending-pdf"

# "MDL No. 3141", "MDL-3141", "MDL 3141", or a bare 4-digit number in the
# leftmost column of the PDF table.
_MDL_RE = re.compile(r"\bMDL[\s\-.]*(?:No\.?\s*)?(\d{3,5})\b", re.IGNORECASE)
_PDF_ROW_RE = re.compile(r"^\s*(\d{3,5})\s+(?:IN RE:?\s*)?(.{6,180}?)\s*$", re.I)

# Prefer the by-MDL-number report: one row per MDL, which is the granularity
# we want. The by-district and by-type reports cover the same MDLs sliced
# differently.
#
# NOT anchored with `$`. The same pattern is used two ways -- matched against
# an individual href, and searched across the whole HTML document by the
# canary -- and an end-anchor makes the document-wide search structurally
# incapable of ever matching.
_WANTED_PDF = re.compile(
    r"Pending_MDL_Dockets_By_MDL_Number[^\"'\s>]*\.pdf", re.IGNORECASE
)


class JpmlConnector:
    source_id = "jpml"
    schedule = "weekly"

    def __init__(self) -> None:
        self._discovered_pdf: str | None = None

    def plan(self, watermark: str | None) -> Sequence[FetchTask]:
        tasks = [
            FetchTask(
                task_key=TASK_LANDING,
                url=LANDING,
                accept="text/html",
                note="discover the current dated MDL-list PDF",
            )
        ]
        if watermark and watermark.startswith("http"):
            tasks.append(
                FetchTask(
                    task_key=TASK_PDF,
                    url=watermark,
                    accept="application/pdf",
                    note="pending MDL list (PDF discovered on a prior run)",
                )
            )
        return tasks

    def plan_watermark(self) -> str | None:
        """URL for plan() to fetch next run. Set while parsing the landing page."""
        return self._discovered_pdf

    def expectation(self, task: FetchTask) -> ContentExpectation:
        if task.task_key == TASK_PDF:
            return ContentExpectation(min_bytes=2_000)
        return ContentExpectation(
            content_types=("text/html",), min_bytes=5_000
        )

    def parse(self, raw: bytes, url: str) -> ParseResult:
        """PURE. Branches on CONTENT, not on task identity, so it stays pure."""
        if raw[:5] == b"%PDF-":
            return self._parse_pdf(raw, url)
        return self._parse_landing(raw, url)

    # -- stage 1: discover ------------------------------------------------

    def _parse_landing(self, raw: bytes, url: str) -> ParseResult:
        try:
            doc = LH.fromstring(raw)
        except Exception:
            return ParseResult()

        best: str | None = None
        for a in doc.xpath("//a[@href]"):
            href = a.get("href") or ""
            if _WANTED_PDF.search(href):
                best = href if href.startswith("http") else BASE + href
                break
        self._discovered_pdf = best

        # The landing page yields no MDL rows by design. Reporting
        # server_reported_empty keeps the canary from calling this BROKEN --
        # it is a pointer page, and its job is done when it hands over a URL.
        note = (
            f"discovered MDL list PDF: {best}" if best
            else "no Pending_MDL_Dockets_By_MDL_Number PDF link found"
        )
        return ParseResult(items=[], note=note, server_reported_empty=True)

    # -- stage 2: parse the PDF -------------------------------------------

    def _parse_pdf(self, raw: bytes, url: str) -> ParseResult:
        try:
            from pypdf import PdfReader
        except ImportError:
            return ParseResult(note="pypdf not installed")

        try:
            reader = PdfReader(io.BytesIO(raw))
            text = "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception as exc:
            return ParseResult(note=f"PDF parse failed: {type(exc).__name__}")

        items: list[Item] = []
        seen: set[str] = set()
        for idx, line in enumerate(text.splitlines()):
            line = " ".join(line.split())
            if not line:
                continue
            m = _PDF_ROW_RE.match(line)
            if not m:
                continue
            mdl_no, caption = m.group(1), m.group(2).strip(" .-")
            if mdl_no in seen or len(caption) < 6:
                continue
            # Page numbers and totals also match a bare-number pattern; a real
            # row always carries a case caption of real length.
            if caption.lower().startswith(("page", "total", "as of")):
                continue
            seen.add(mdl_no)
            items.append(
                Item(
                    source_id=self.source_id,
                    natural_key=f"mdl:{mdl_no}",
                    title=f"MDL {mdl_no} — {caption}"[:300],
                    body=(
                        f"Pending multidistrict litigation MDL No. {mdl_no}: "
                        f"{caption}."
                    ),
                    source_url=url,
                    extract_locator=f"pdf-line[{idx}]",
                    payload={
                        "record_kind": "mdl",
                        "mdl_number": mdl_no,
                        "caption": caption,
                        "report_url": url,
                    },
                )
            )
        return ParseResult(items=items, note=f"{len(items)} pending MDLs")

    # -- canary ------------------------------------------------------------

    def canary(self, raw: bytes, task: FetchTask) -> None:
        if task.task_key == TASK_LANDING:
            text = raw.decode("utf-8", errors="replace")
            if not _WANTED_PDF.search(text):
                raise CanaryFailure(
                    self.source_id,
                    "landing page carries no Pending_MDL_Dockets_By_MDL_Number "
                    "PDF link. The panel publishes this monthly, so its "
                    f"absence means the page layout changed. URL: {task.url}",
                )
            return

        if task.task_key == TASK_PDF:
            if raw[:5] != b"%PDF-":
                raise CanaryFailure(
                    self.source_id,
                    f"expected a PDF but got {raw[:16]!r} -- the dated URL has "
                    f"probably expired and now redirects. URL: {task.url}",
                )

    def watermark_for(self, items: Sequence[Item]) -> str | None:
        return datetime.now(timezone.utc).date().isoformat()


def build() -> JpmlConnector:
    return JpmlConnector()
