"""Phase 5 -- the claims-agent routing table and chapter 11 census.

Four bankruptcy courts publish, for free and with no terms to review, which
claims agent was retained in which chapter 11 case. That is worth having on
its own: it is a standing census of large chapter 11 cases with the vendor
named, which is the routing key Phase 7 would need before it could ever crawl
a vendor's docket.

MEASURED 2026-08-15. The four courts publish in four different shapes, and
three separate things had to be discovered by reading their live pages:

    ohsb  /claims-agents   -> assignment TABLE inline
    nysb  /claims-agents   -> vendor DIRECTORY inline; the assignments are a
                              separate page, /megaCases
    njb   /claim-agent-cases-and-protocols -> links only; assignments at
                              /content/claims-agent-case-assignments-district-new-jersey
    deb   /claims-agents-and-assignments   -> links only; vendor directory at
                              /claims-agency-list

DELAWARE'S ASSIGNMENT LIST IS NOT AVAILABLE, and that is a determination, not
a gap to route around. It lives at media.deb.uscourts.gov, whose robots.txt
disallows it. Delaware is the most important chapter 11 venue in the country
and this is the single most valuable page of the four -- which is exactly why
the rule matters. We fetch Delaware's vendor directory (allowed) and its
landing page (allowed, and it tells us if the court ever moves the list
somewhere fetchable), and we report the assignment list as refused rather than
quietly omitting it.

TWO PARSING TRAPS, both encoded below:

1. **Column order is not stable across courts and the columns do not even
   mean the same things.** ohsb and nysb are (case, debtor, agent, date); njb
   is (case+judge, vicinage, title, agent) with NO date column at all. A
   positional parser writes "Newark" into the debtor field and looks like it
   worked. Columns are therefore resolved by matching header text to a role,
   and a table missing a required role fails the canary instead of producing
   confident nonsense.

2. **An unmapped agent name must alert, never silently drop.** A vendor that
   is not in the routing table is either a new entrant or a renaming -- both
   things you want to know about. The row is KEPT with `vendor_id="unmapped"`
   and surfaced in the run note, because dropping it would make the census
   quietly wrong in exactly the direction that is hardest to notice.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from lxml import html as LH

from ...canary.framework import CanaryFailure, ContentExpectation
from ...store.db import Item
from ..base import FetchTask, ParseResult

_AGENTS_FILE = Path(__file__).with_name("agents.toml")

UNMAPPED = "unmapped"


# ---------------------------------------------------------------------------
# The routing table
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Agent:
    id: str
    name: str
    tos_source_id: str
    case_index: str
    aliases: tuple[str, ...]
    note: str = ""


@dataclass(slots=True)
class RoutingTable:
    agents: dict[str, Agent] = field(default_factory=dict)
    # Longest alias first: "kurtzman carson consultants" must win over
    # "kcc" if both appear, and "epiq corporate restructuring" over "epiq".
    _ordered: list[tuple[str, str]] = field(default_factory=list)

    def resolve(self, raw_name: str) -> str:
        """Raw printed name -> canonical vendor id, or UNMAPPED."""
        hay = _norm(raw_name)
        if not hay:
            return UNMAPPED
        for alias, agent_id in self._ordered:
            if alias in hay:
                return agent_id
        return UNMAPPED

    def get(self, agent_id: str) -> Agent | None:
        return self.agents.get(agent_id)


def _norm(s: str) -> str:
    """Lowercase, collapse whitespace, drop punctuation that varies by court."""
    s = (s or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def load_routing_table(path: Path | None = None) -> RoutingTable:
    f = path or _AGENTS_FILE
    if not f.is_file():
        return RoutingTable()
    with f.open("rb") as fh:
        data = tomllib.load(fh)

    agents: dict[str, Agent] = {}
    pairs: list[tuple[str, str]] = []
    for a in data.get("agent", []):
        agent = Agent(
            id=a["id"],
            name=a.get("name", a["id"]),
            tos_source_id=a.get("tos_source_id", ""),
            case_index=a.get("case_index", ""),
            aliases=tuple(a.get("aliases", ())),
            note=a.get("note", ""),
        )
        agents[agent.id] = agent
        for alias in agent.aliases:
            pairs.append((_norm(alias), agent.id))

    pairs.sort(key=lambda p: -len(p[0]))
    return RoutingTable(agents=agents, _ordered=pairs)


# ---------------------------------------------------------------------------
# Court pages
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CourtPage:
    court: str
    kind: str          # "assignments" | "vendors" | "landing"
    url: str
    note: str


PAGES: tuple[CourtPage, ...] = (
    CourtPage("ohsb", "assignments",
              "https://www.ohsb.uscourts.gov/claims-agents",
              "S.D. Ohio -- assignment table inline"),
    CourtPage("nysb", "assignments",
              "https://www.nysb.uscourts.gov/megaCases",
              "S.D.N.Y. -- mega case list with retained agent"),
    CourtPage("nysb", "vendors",
              "https://www.nysb.uscourts.gov/claims-agents",
              "S.D.N.Y. -- approved claims agents directory"),
    CourtPage("njb", "assignments",
              "https://www.njb.uscourts.gov/content/"
              "claims-agent-case-assignments-district-new-jersey",
              "D.N.J. -- claims agent case assignments"),
    CourtPage("deb", "vendors",
              "https://www.deb.uscourts.gov/claims-agency-list",
              "D. Del. -- approved claims agencies directory"),
    CourtPage("deb", "landing",
              "https://www.deb.uscourts.gov/claims-agents-and-assignments",
              "D. Del. -- watched: its assignment list is robots-disallowed"),
)

# Delaware's assignment list, recorded so the refusal is visible rather than
# looking like an oversight. NOT fetched.
DEB_ASSIGNMENTS_REFUSED = (
    "https://media.deb.uscourts.gov/moveit/ClaimsAgentCases.html"
)

# Header text -> the role that column plays. Matched on a normalized
# substring, because "Case Number and Judge Initials" and "Case No." are the
# same column wearing different hats.
_ROLE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("case", "case_number"),
    ("debtor", "debtor"),
    ("title", "debtor"),
    ("claims agent", "agent"),
    ("link to claims agent website", "agent"),
    ("agent", "agent"),
    ("date filed", "date_filed"),
    ("filed", "date_filed"),
    ("vicinage", "vicinage"),
    ("judge", "judge"),
)

_REQUIRED_ROLES = ("case_number", "agent")

_CASE_RE = re.compile(r"\b(\d{2}-\d{4,6})(?:-([A-Za-z]{2,4}))?\b")


def _roles_for(headers: list[str]) -> dict[int, str]:
    """Map column index -> role, by header text. Never by position."""
    roles: dict[int, str] = {}
    for i, h in enumerate(headers):
        n = _norm(h)
        if not n:
            continue
        for needle, role in _ROLE_PATTERNS:
            if needle not in n:
                continue
            # First column claiming a role keeps it: njb's "Case Number and
            # Judge Initials" must not be re-labelled by a later match.
            if role in roles.values():
                # BUG PINNED: this used to `break` here, which silently cost
                # njb its debtor column. Its header is "Case Title" -- the
                # "case" needle matches first, finds case_number already
                # taken, and stopped. Every njb row then carried an empty
                # debtor while the parse looked entirely successful. Keep
                # trying the remaining patterns instead of giving up on the
                # column.
                continue
            roles[i] = role
            break
    return roles


def _cell_text(cell) -> str:
    return " ".join(cell.text_content().split())


def _cell_href(cell) -> str:
    hrefs = cell.xpath(".//a/@href")
    return hrefs[0].strip() if hrefs else ""


def _parse_date(raw: str) -> str:
    """'July 22, 2026' or '06/15/2026' -> ISO. Empty string if neither."""
    raw = raw.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

class ClaimsRoutingConnector:
    """One task per court page. A dead page degrades one task, not the run."""

    source_id = "claims_routing"
    # These lists change when a mega case is filed -- a handful of rows a
    # month. Daily polling would spend budget on four unchanged pages.
    schedule = "weekly"

    def __init__(self, table: RoutingTable | None = None) -> None:
        self.table = table or load_routing_table()
        self._by_url = {p.url: p for p in PAGES}
        # Populated during parse so the run report can name them. Reset per
        # parse call so a replay does not accumulate.
        self.unmapped_seen: set[str] = set()

    @property
    def coverage_note(self) -> str:
        return (
            "3 of 4 courts publish a fetchable assignment list. Delaware's -- "
            f"the most valuable of the four -- is at {DEB_ASSIGNMENTS_REFUSED}, "
            "whose robots.txt disallows it, so D. Del. contributes its vendor "
            "directory only."
        )

    def plan(self, watermark: str | None) -> Sequence[FetchTask]:
        return [
            FetchTask(
                task_key=f"{self.source_id}:{p.court}:{p.kind}",
                url=p.url,
                accept="text/html",
                note=p.note,
            )
            for p in PAGES
        ]

    def expectation(self, task: FetchTask) -> ContentExpectation:
        return ContentExpectation(content_types=("text/html",), min_bytes=2_000)

    # -- parse -------------------------------------------------------------

    def parse(self, raw: bytes, url: str) -> ParseResult:
        """PURE. Reads both shapes off whatever page it is given."""
        page = self._by_url.get(url)
        court = page.court if page else _court_from_url(url)

        try:
            doc = LH.fromstring(raw)
        except Exception:
            return ParseResult(note="unparsable HTML")

        assignments, unmapped = self._parse_assignments(doc, court, url)
        vendors = self._parse_vendor_directory(doc, court, url)

        items = assignments + vendors
        self.unmapped_seen |= unmapped

        notes = []
        if assignments:
            notes.append(f"{len(assignments)} case assignments")
        if vendors:
            notes.append(f"{len(vendors)} approved vendors")
        if unmapped:
            # Loud, and in the run report -- an unrecognized agent is either a
            # new entrant or a rename, and both are worth a human glance.
            notes.append(
                "UNMAPPED AGENT(S): " + "; ".join(sorted(unmapped))
                + " -- add an alias to connectors/claims/agents.toml"
            )

        # A landing page legitimately carries neither shape. Saying so
        # affirmatively keeps the canary from calling a pointer page BROKEN.
        empty_by_design = (page is not None and page.kind == "landing")

        return ParseResult(
            items=items,
            note="; ".join(notes) or f"{court}: no rows",
            server_reported_empty=empty_by_design,
        )

    def _parse_assignments(
        self, doc, court: str, url: str
    ) -> tuple[list[Item], set[str]]:
        items: list[Item] = []
        unmapped: set[str] = set()

        for table in doc.xpath("//table"):
            headers = [_cell_text(c) for c in table.xpath(".//tr[1]/th")]
            if not headers:
                continue
            roles = _roles_for(headers)
            if not all(r in roles.values() for r in _REQUIRED_ROLES):
                continue

            by_role = {role: idx for idx, role in roles.items()}
            for row_i, tr in enumerate(table.xpath(".//tr")):
                cells = tr.xpath("./td")
                if not cells:
                    continue

                def get(role: str) -> str:
                    i = by_role.get(role, -1)
                    return _cell_text(cells[i]) if 0 <= i < len(cells) else ""

                case_raw = get("case_number")
                m = _CASE_RE.search(case_raw)
                if not m:
                    continue
                case_number = m.group(1)

                agent_i = by_role.get("agent", -1)
                agent_raw = get("agent")
                agent_url = (
                    _cell_href(cells[agent_i])
                    if 0 <= agent_i < len(cells) else ""
                )
                # nysb lists mega cases with no agent retained. That is a real
                # row -- a large chapter 11 with no claims agent -- and it is
                # not an unmapped vendor.
                if agent_raw:
                    vendor_id = self.table.resolve(agent_raw)
                    if vendor_id == UNMAPPED:
                        unmapped.add(agent_raw[:80])
                else:
                    vendor_id = "none_retained"

                debtor = get("debtor")
                date_filed = _parse_date(get("date_filed"))
                agent = self.table.get(vendor_id)

                items.append(
                    Item(
                        source_id="claims_routing",
                        natural_key=f"{court}:{case_number}",
                        title=f"[{court}] {case_number} — {debtor}"[:300],
                        # Deliberately factual and free of event language.
                        # This is a census record, not a deal signal: writing
                        # "settlement" or "judgment" here would manufacture a
                        # signal the source does not carry and burn extraction
                        # budget. Same discipline as sec_daily_index/govinfo.
                        body=(
                            f"Chapter 11 case {case_number} in {court}. "
                            f"Debtor: {debtor or 'not stated'}. "
                            f"Claims agent: {agent_raw or 'none retained'}."
                        ),
                        source_url=url,
                        published_at=date_filed or None,
                        extract_locator=f"tr[{row_i}]",
                        payload={
                            "record_kind": "claims_assignment",
                            "court": court,
                            "case_number": case_number,
                            # njb prints jointly-administered cases in one
                            # cell ("19-12809-JKS and 19-12812-JKS"). The
                            # first is the lead case and keys the row; the
                            # raw string is kept so the companion case is not
                            # lost.
                            "case_number_raw": case_raw,
                            "judge_initials": (m.group(2) or ""),
                            "debtor": debtor,
                            "vicinage": get("vicinage"),
                            "agent_raw": agent_raw,
                            "vendor_id": vendor_id,
                            "vendor_name": agent.name if agent else "",
                            "vendor_tos_source_id": (
                                agent.tos_source_id if agent else ""
                            ),
                            "agent_case_url": agent_url,
                            "date_filed": date_filed,
                            "list_url": url,
                        },
                    )
                )
        return items, unmapped

    def _parse_vendor_directory(self, doc, court: str, url: str) -> list[Item]:
        """Approved-vendor directories: layout tables, one vendor per cell.

        Discriminated from an assignment table by the absence of a case-number
        header. Both courts that publish a directory use a 3-column layout
        table with no <th> at all.
        """
        items: list[Item] = []
        for table in doc.xpath("//table"):
            headers = [_cell_text(c) for c in table.xpath(".//tr[1]/th")]
            if headers and "case_number" in _roles_for(headers).values():
                continue                    # that is an assignment table

            for cell_i, cell in enumerate(table.xpath(".//td")):
                lines = [
                    " ".join(l.split())
                    for l in cell.text_content().splitlines()
                    if l.strip()
                ]
                if not lines:
                    continue
                name = lines[0]
                # A vendor block always has contact detail under the name; a
                # stray one-line layout cell does not.
                if len(lines) < 2 or len(name) < 4 or len(name) > 110:
                    continue
                vendor_id = self.table.resolve(name)
                agent = self.table.get(vendor_id)
                items.append(
                    Item(
                        source_id="claims_routing",
                        natural_key=f"vendor:{court}:{vendor_id}:{_norm(name)[:40]}",
                        title=f"[{court}] approved claims agent — {name}"[:300],
                        body=(
                            f"{name} is an approved claims and noticing agent "
                            f"in {court}. " + " ".join(lines[1:])[:600]
                        ),
                        source_url=url,
                        extract_locator=f"td[{cell_i}]",
                        payload={
                            "record_kind": "claims_agent_directory",
                            "court": court,
                            "vendor_id": vendor_id,
                            "vendor_name_raw": name,
                            "vendor_name": agent.name if agent else "",
                            "vendor_tos_source_id": (
                                agent.tos_source_id if agent else ""
                            ),
                            "case_index": agent.case_index if agent else "",
                            "contact_block": " | ".join(lines[1:])[:600],
                        },
                    )
                )
        return items

    # -- canary ------------------------------------------------------------

    def canary(self, raw: bytes, task: FetchTask) -> None:
        page = self._by_url.get(task.url)
        kind = page.kind if page else ""

        if kind == "landing":
            # Delaware's landing page earns its request by answering one
            # question: has the court moved the assignment list onto a host we
            # are permitted to read? If the link to the disallowed host is
            # gone, something changed and a human should look.
            text = raw.decode("utf-8", errors="replace")
            if "ClaimsAgentCases" not in text:
                raise CanaryFailure(
                    self.source_id,
                    "D. Del.'s claims page no longer links to "
                    "ClaimsAgentCases.html. Either the court moved the "
                    "assignment list -- possibly somewhere fetchable, which "
                    "would be good news -- or the page layout changed. "
                    f"Check {task.url} by hand.",
                )
            return

        try:
            doc = LH.fromstring(raw)
        except Exception as exc:
            raise CanaryFailure(
                self.source_id, f"unparsable HTML from {task.url}: {exc}"
            ) from exc

        if kind == "assignments":
            for table in doc.xpath("//table"):
                headers = [_cell_text(c) for c in table.xpath(".//tr[1]/th")]
                roles = _roles_for(headers).values()
                if all(r in roles for r in _REQUIRED_ROLES):
                    return
            raise CanaryFailure(
                self.source_id,
                f"no table at {task.url} carries both a case-number and a "
                f"claims-agent header. Columns are resolved by header text on "
                f"purpose -- the three courts order them differently and njb "
                f"has no date column -- so a header change must fail loudly "
                f"rather than fall back to positional guessing.",
            )

        if kind == "vendors":
            if not doc.xpath("//table//td"):
                raise CanaryFailure(
                    self.source_id,
                    f"vendor directory at {task.url} has no table cells; the "
                    f"court changed its layout.",
                )

    def watermark_for(self, items: Sequence[Item]) -> str | None:
        return datetime.now(timezone.utc).date().isoformat()


def _court_from_url(url: str) -> str:
    m = re.search(r"//(?:www|media)\.([a-z]+)\.uscourts\.gov", url)
    return m.group(1) if m else "?"


def build(table: RoutingTable | None = None) -> ClaimsRoutingConnector:
    return ClaimsRoutingConnector(table)
