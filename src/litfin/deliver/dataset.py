"""The view model shared by the dashboard, the digest, and the local server.

One assembly function, three renderers. The alternative -- each renderer
running its own queries -- guarantees they drift, and the failure mode is the
worst kind: an email that disagrees with the dashboard about which case ranks
first, with no way to tell which one is lying.

Everything here is a plain dataclass built from SQLite rows. The renderers are
pure functions over these objects, which is what makes them testable without a
database.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from ..config import Config
from ..store.db import Database


# ---------------------------------------------------------------------------
# Plain English
#
# The table used to make you decode `antitrust_followon / settlement_reached`
# to know what a row was. These maps turn the enums back into the sentence a
# person would have said, and `describe()` assembles one line per case.
#
# Composed from the STRUCTURED fields rather than the model's prose, for two
# reasons: the summary is empty or unhelpful on a meaningful fraction of rows,
# and a deterministic description is one that can be tested. The model's own
# summary still shows underneath and in the expanded detail.
# ---------------------------------------------------------------------------

_EVENT_PHRASE: dict[str, str] = {
    "judgment_entered": "A court entered judgment",
    "judgment_proposed": "A proposed judgment was filed (not yet entered)",
    "jury_verdict": "A jury returned a verdict",
    "appeal_filed": "An appeal was filed",
    "settlement_reached": "The parties reached a settlement",
    "settlement_preliminary_approval": "A settlement got preliminary approval",
    "settlement_final_approval": "A settlement was finally approved",
    "enforcement_action": "A regulator brought an enforcement action",
    "plan_confirmation": "A bankruptcy plan was confirmed",
    "case_filed": "A case was filed",
    "no_event": "No decided event yet",
}

_THESIS_PHRASE: dict[str, str] = {
    "judgment_monetization": "could support judgment or appeal funding",
    "post_settlement": "could support a settlement-receivable purchase",
    "antitrust_followon": "could lead to follow-on antitrust damages",
    "none": "no clear funding angle",
}

_AREA_PHRASE: dict[str, str] = {
    "antitrust": "antitrust",
    "commercial": "commercial",
    "bankruptcy": "bankruptcy",
    "securities": "securities",
    "other": "",
    "unknown": "",
}


def _money(amount: float | None) -> str:
    if not amount:
        return ""
    if amount >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.1f}B".replace(".0B", "B")
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.0f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.0f}K"
    return f"${amount:,.0f}"


# ---------------------------------------------------------------------------
# Case stage
#
# `event_type` says what JUST HAPPENED; `procedural_posture` says where the
# case STANDS. They are different questions and the second is the one a
# triager actually asks — "is this thing still being fought, or is it done?"
#
# Posture wins where it is specific, because it is the field whose whole job
# is to answer this. Event type is the fallback, since posture is free text
# and empty on plenty of rows.
#
# Ordered latest-stage-first: a posture that mentions both a settlement and an
# earlier motion is at the settlement stage, and scanning in this order gets
# that right without needing to parse the sentence.
# ---------------------------------------------------------------------------

STAGE_UNKNOWN = "Stage unclear"

# (stage label, posture patterns). Order is significant.
_STAGE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Closed", (r"\bcase\s+closed\b", r"\bdismissed\s+with\s+prejudice\b",
                r"\bterminated\b", r"\bfully\s+(?:paid|satisfied)\b",
                r"\bsatisfaction\s+of\s+judgment\b")),
    ("On appeal", (r"\bon\s+appeal\b", r"\bappeal\s+(?:is\s+)?pending\b",
                   r"\bnotice\s+of\s+appeal\b", r"\bappellate\b",
                   r"\bcert(?:iorari)?\s+petition\b")),
    ("Settlement — final approval", (
        r"\bfinal\s+approval\b", r"\bfinally\s+approved\b",
        r"\bsettlement\s+approved\b", r"\bapproved\s+the\s+settlement\b",
        r"\b9019\s+(?:motion\s+)?granted\b")),
    ("Settlement — preliminary approval", (
        r"\bpreliminar(?:y|ily)\s+approv", r"\bapproval\s+pending\b",
        r"\bmotion\s+for\s+approval\b", r"\bfairness\s+hearing\b",
        r"\bawaiting\s+court\s+approval\b")),
    ("Settlement reached", (
        r"\bsettlement\s+(?:reached|agreement|in\s+principle|executed)\b",
        # Both word orders. "reached a settlement" is at least as common as
        # "settlement reached", and matching only the latter let an earlier
        # clause in the same sentence ("the motion to dismiss was denied and
        # the parties reached a settlement") decide the stage.
        r"\breach(?:ed|ing)?\s+(?:an?\s+)?settlement\b",
        r"\bexecut(?:ed|ing)\s+(?:an?\s+)?settlement\b",
        r"\bentered\s+into\s+(?:an?\s+)?settlement\b",
        r"\bagreed\s+to\s+settle\b", r"\bsettled\b", r"\bterm\s+sheet\b",
        r"\bconsent\s+decree\s+(?:lodged|filed)\b",
        r"\bstipulation\s+of\s+settlement\b")),
    ("Settlement talks", (
        r"\bmediation\b", r"\bsettlement\s+conference\b",
        r"\bsettlement\s+(?:talks|negotiations|discussions)\b")),
    ("Judgment entered", (
        r"\bjudgment\s+(?:was\s+)?entered\b", r"\bentry\s+of\s+judgment\b",
        r"\bfinal\s+judgment\b", r"\bjudgment\s+for\s+(?:the\s+)?plaintiff\b")),
    ("Post-trial motions", (
        r"\bpost[- ]trial\b", r"\brenewed\s+motion\b", r"\bjnov\b",
        r"\bmotion\s+for\s+new\s+trial\b", r"\bremittitur\b")),
    ("Verdict returned", (r"\bverdict\b", r"\bjury\s+(?:found|returned)\b")),
    ("Trial", (r"\bat\s+trial\b", r"\btrial\s+(?:is\s+)?(?:underway|ongoing|"
               r"scheduled|set)\b", r"\bbench\s+trial\b", r"\bjury\s+selection\b")),
    ("Summary judgment", (
        r"\bsummary\s+judgment\b", r"\brule\s+56\b")),
    ("Class certification", (
        r"\bclass\s+certification\b", r"\bmotion\s+to\s+certify\b",
        r"\brule\s+23\b")),
    ("Plan confirmation", (
        r"\bplan\s+(?:of\s+reorganization\s+)?confirm", r"\bdisclosure\s+statement\b",
        r"\bchapter\s+11\s+plan\b")),
    ("Motion to dismiss", (
        r"\bmotion\s+to\s+dismiss\b", r"\brule\s+12\(b\)", r"\b12\(b\)\(6\)",
        r"\bdemurrer\b")),
    ("Discovery", (
        r"\bdiscovery\b", r"\bdeposition", r"\binterrogator", r"\bsubpoena\b")),
    ("Pleadings", (
        r"\bcomplaint\s+filed\b", r"\banswer\s+filed\b",
        r"\bamended\s+complaint\b", r"\bnewly\s+filed\b")),
    ("Enforcement action", (
        r"\benforcement\s+action\b", r"\badministrative\s+proceeding\b")),
)

_STAGE_COMPILED = tuple(
    (label, re.compile("|".join(pats), re.IGNORECASE))
    for label, pats in _STAGE_PATTERNS
)

# Fallback when posture says nothing usable.
_STAGE_FROM_EVENT: dict[str, str] = {
    "settlement_final_approval": "Settlement — final approval",
    "settlement_preliminary_approval": "Settlement — preliminary approval",
    "settlement_reached": "Settlement reached",
    "judgment_entered": "Judgment entered",
    "judgment_proposed": "Judgment proposed",
    "jury_verdict": "Verdict returned",
    "appeal_filed": "On appeal",
    "plan_confirmation": "Plan confirmation",
    "enforcement_action": "Enforcement action",
    "case_filed": "Pleadings",
    "no_event": "Active litigation",
}

# Stages ordered by PROXIMITY TO A FUNDABLE CLAIM, not by chronology. Sorting
# the column descending should put the most fundable matters on top, which is
# the whole reason to sort by stage at all.
#
# Two consequences that a plain chronological order gets wrong:
#
#   * "Closed" sits near the BOTTOM despite being the latest thing that can
#     happen to a case. The money has already moved; there is nothing left to
#     fund. Chronologically last, commercially least interesting.
#   * "On appeal" sits below an entered judgment rather than above it. An
#     appeal re-opens risk that the judgment had closed.
#
# A lexical sort gets everything wrong -- it puts "Trial" before "Verdict
# returned" and "Closed" first.
STAGE_ORDER: tuple[str, ...] = (
    STAGE_UNKNOWN,
    "Closed",
    "Pleadings",
    "Motion to dismiss",
    "Discovery",
    "Class certification",
    "Summary judgment",
    "Active litigation",
    "Enforcement action",
    "Trial",
    "Verdict returned",
    "Post-trial motions",
    "On appeal",
    "Judgment proposed",
    "Judgment entered",
    "Plan confirmation",
    "Settlement talks",
    "Settlement reached",
    "Settlement — preliminary approval",
    "Settlement — final approval",
)
_STAGE_RANK = {s: i for i, s in enumerate(STAGE_ORDER)}


def stage_rank(stage: str) -> int:
    return _STAGE_RANK.get(stage, len(STAGE_ORDER))


def derive_stage(procedural_posture: str, event_type: str) -> tuple[str, str]:
    """-> (stage, what decided it).

    Returning the basis matters for the same reason the imputed-damages flag
    does: a stage inferred from an event type is a weaker claim than one read
    off an explicit posture, and the row should be able to say which it is.
    """
    posture = (procedural_posture or "").strip()
    if posture:
        for label, pattern in _STAGE_COMPILED:
            if pattern.search(posture):
                return label, "posture"

    event = (event_type or "").strip()
    if event in _STAGE_FROM_EVENT:
        return _STAGE_FROM_EVENT[event], "event type"
    return STAGE_UNKNOWN, "neither posture nor event type was specific"


# ---------------------------------------------------------------------------
# Claim-size bands
#
# A bare "min $__M" box asked the user to guess a threshold and told them
# nothing about the distribution. Bands with live counts show the shape of the
# book at a glance, and NOT STATED is a band of its own rather than a hidden
# exclusion — on this corpus it is 93% of rows, which is a fact about free
# sources that the filter should surface, not bury.
# ---------------------------------------------------------------------------

NOT_STATED = "Not stated"

SIZE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("Under $10M", 0.0, 10_000_000.0),
    ("$10M – $50M", 10_000_000.0, 50_000_000.0),
    ("$50M – $250M", 50_000_000.0, 250_000_000.0),
    ("$250M – $1B", 250_000_000.0, 1_000_000_000.0),
    ("$1B and up", 1_000_000_000.0, float("inf")),
)

# The order the UI renders them in, NOT_STATED last so a real figure is never
# visually buried under the ones that lack one.
BAND_ORDER: tuple[str, ...] = tuple(b[0] for b in SIZE_BANDS) + (NOT_STATED,)


def size_band(amount: float | None, imputed: bool) -> str:
    """Which claim-size band a row belongs to.

    An imputed figure lands in NOT_STATED, never in a dollar band. Ranking on
    a thesis prior is legitimate; letting that prior answer "show me anything
    over $50M" is not — the user asked about stated amounts.
    """
    if imputed or not amount or amount <= 0:
        return NOT_STATED
    for label, lo, hi in SIZE_BANDS:
        if lo <= amount < hi:
            return label
    return SIZE_BANDS[-1][0]


# ---------------------------------------------------------------------------
# Jurisdiction
#
# The model returns whatever the document said: "federal", "Federal", "New
# York", "U.S. District Court", "". Feeding that straight into a dropdown
# produced a list of near-duplicates that filtered almost nothing. These
# normalize to one label per real jurisdiction, derived from jurisdiction,
# venue AND court together, because the useful signal is often in whichever
# field the model happened to fill.
# ---------------------------------------------------------------------------

_STATES = (
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine",
    "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire",
    "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota",
    "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island",
    "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont",
    "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
    "District of Columbia",
)

# Federal court abbreviations carry their state in a two-letter code:
# "S.D.N.Y." -> NY, "N.D. Cal." -> CA, "D. Del." -> DE.
_ABBREV_STATE = {
    "ny": "New York", "cal": "California", "ca": "California",
    "del": "Delaware", "de": "Delaware", "tex": "Texas", "tx": "Texas",
    "ill": "Illinois", "il": "Illinois", "mass": "Massachusetts",
    "ma": "Massachusetts", "fla": "Florida", "fl": "Florida",
    "pa": "Pennsylvania", "nj": "New Jersey", "ohio": "Ohio", "oh": "Ohio",
    "va": "Virginia", "md": "Maryland", "ga": "Georgia", "mich": "Michigan",
    "mi": "Michigan", "minn": "Minnesota", "mn": "Minnesota",
    "wash": "Washington", "wa": "Washington", "colo": "Colorado",
    "co": "Colorado", "ariz": "Arizona", "az": "Arizona", "nc": "North Carolina",
    "sc": "South Carolina", "tenn": "Tennessee", "tn": "Tennessee",
    "mo": "Missouri", "la": "Louisiana", "conn": "Connecticut",
    "ct": "Connecticut", "nev": "Nevada", "nv": "Nevada", "or": "Oregon",
    "wis": "Wisconsin", "wi": "Wisconsin", "ind": "Indiana", "in": "Indiana",
    "kan": "Kansas", "ks": "Kansas", "ky": "Kentucky", "ala": "Alabama",
    "al": "Alabama", "ark": "Arkansas", "ar": "Arkansas", "utah": "Utah",
    "iowa": "Iowa", "neb": "Nebraska", "okla": "Oklahoma", "ok": "Oklahoma",
    "dc": "District of Columbia", "d.c": "District of Columbia",
}

_FEDERAL_HINTS = (
    "federal", "united states district", "u.s. district", "us district",
    "bankruptcy court", "court of appeals", "circuit", "d.c. cir",
    "united states bankruptcy", "u.s. bankruptcy", "sec ", "ftc ", "doj ",
)

_STATE_HINTS = (
    "state court", "supreme court of the state", "superior court",
    "commercial division", "court of chancery", "county",
    "district court of", "state of",
)

FEDERAL = "Federal"
UNKNOWN_JURISDICTION = "Unknown"


def _state_in(blob: str) -> str:
    """Pull a state out of a jurisdiction/venue/court blob, or ''."""
    for name in _STATES:
        if name.lower() in blob:
            return name

    # Federal district abbreviations. Dots are stripped FIRST, because they
    # are inconsistent in the wild and they break word boundaries:
    # "S.D.N.Y." -> "sdny", "N.D. Cal." -> "nd cal", "D. Del." -> "d del".
    flat = " ".join(blob.replace(".", "").split())

    # Run-together form: sdny, edny, ndcal, dde.
    m = re.search(r"\b[nsewmc]?d([a-z]{2,4})\b", flat)
    if m and m.group(1) in _ABBREV_STATE:
        return _ABBREV_STATE[m.group(1)]
    # Spaced form: "nd cal", "d del", "sd tex".
    m = re.search(r"\b[nsewmc]?d\s+([a-z]{2,5})\b", flat)
    if m and m.group(1) in _ABBREV_STATE:
        return _ABBREV_STATE[m.group(1)]
    return ""


def normalize_jurisdiction(
    jurisdiction: str, venue: str, court: str
) -> tuple[str, str]:
    """-> (group, label).

    group is 'Federal' | 'State' | 'Unknown' and drives the coarse filter.
    label is what the dropdown shows: 'Federal — New York', 'State — Delaware',
    plain 'Federal' when no state is identifiable, or 'Unknown'.
    """
    blob = " ".join((jurisdiction, venue, court)).lower().strip()
    if not blob:
        return UNKNOWN_JURISDICTION, UNKNOWN_JURISDICTION

    state = _state_in(blob)
    is_federal = any(h in blob for h in _FEDERAL_HINTS)
    is_state = any(h in blob for h in _STATE_HINTS)

    # "state" as a bare jurisdiction value, or a named state with no federal
    # marker, means a state court.
    if not is_federal and (is_state or jurisdiction.strip().lower() == "state"):
        return "State", f"State — {state}" if state else "State"
    if is_federal:
        return FEDERAL, f"Federal — {state}" if state else FEDERAL
    if state:
        # A bare state name with no court marker either way. Say so rather
        # than guessing a court system.
        return UNKNOWN_JURISDICTION, f"{state} (court unclear)"
    return UNKNOWN_JURISDICTION, UNKNOWN_JURISDICTION


# Court names that identify no place. "in United States District Court" tells
# a reader nothing they did not already assume, so the clause is dropped
# rather than padded out.
_PLACELESS = {
    "united states district court", "united states bankruptcy court",
    "u.s. district court", "us district court", "district court",
    "bankruptcy court", "federal court", "unknown", "n/a", "?", "-", "",
}


def _short_place(raw: str) -> str:
    """A venue string reduced to the part that names somewhere, or ''."""
    where = " ".join((raw or "").split())
    for noise in ("United States District Court for the ",
                  "United States District Court, ",
                  "United States Bankruptcy Court for the ",
                  "United States Bankruptcy Court, ",
                  "In the United States District Court for the "):
        if where.startswith(noise):
            where = where[len(noise):].strip()
            break
    if where.lower().strip(" .,") in _PLACELESS:
        return ""
    return where


def describe(
    *, event_type: str, deal_thesis: str, practice_area: str,
    damages_usd: float | None, damages_imputed: bool, venue: str,
    court: str, defendant_is_public: bool,
) -> str:
    """One plain-English sentence saying what this row IS.

    Deterministic and total: every combination of inputs produces a sentence,
    including the empty one, because a blank cell in the description column
    would defeat the purpose of having it.
    """
    event = _EVENT_PHRASE.get(event_type, "Something happened")
    area = _AREA_PHRASE.get(practice_area, "")

    where = _short_place(venue) or _short_place(court)

    parts = [event]
    if area:
        parts.append(f"in {'an' if area[0] in 'aeiou' else 'a'} {area} matter")
    if where:
        parts.append(f"in {where}")
    sentence = " ".join(parts).rstrip(".") + "."

    # The money clause NEVER states an imputed figure as if it were real.
    if damages_usd and not damages_imputed:
        sentence += f" Stated amount {_money(damages_usd)}."
    else:
        sentence += " No amount stated."

    thesis = _THESIS_PHRASE.get(deal_thesis, "")
    if thesis:
        sentence += f" {'It' if deal_thesis != 'none' else 'Currently'} {thesis}."
    if defendant_is_public:
        sentence += " Defendant is a public company."
    return sentence


@dataclass(slots=True)
class Prospect:
    """One ranked row, flattened for display."""

    rank: int
    item_uid: str
    score: float
    caption: str
    summary: str
    court: str
    venue: str
    jurisdiction: str
    practice_area: str
    deal_thesis: str
    event_type: str
    event_date: str
    published_at: str
    source_id: str
    source_url: str
    damages_usd: float | None
    damages_conf: str
    damages_imputed: bool
    damages_basis: str
    docket_number: str
    procedural_posture: str
    appeal_status: str
    collectability_note: str
    defendant_is_public: bool
    defendant_ticker: str
    parties_plaintiff: list[str]
    parties_defendant: list[str]
    caveats: str
    extraction_confidence: str
    artifact_sha256: str
    components: dict[str, Any]

    # Derived at load time so the dashboard, the digest and the server all
    # show the same words and filter on the same buckets.
    description: str = ""
    size_band: str = NOT_STATED
    jurisdiction_group: str = UNKNOWN_JURISDICTION
    jurisdiction_label: str = UNKNOWN_JURISDICTION
    # Other documents reporting the SAME matter. The row shown is one of
    # several; these are the rest, listed rather than discarded so "one
    # matter" never quietly means "we threw evidence away".
    cluster_key: str = ""
    duplicates: list[dict[str, str]] = field(default_factory=list)

    stage: str = STAGE_UNKNOWN
    stage_basis: str = ""
    counsel_plaintiff: list[str] = field(default_factory=list)
    counsel_defendant: list[str] = field(default_factory=list)
    counsel_known: bool = False

    @property
    def document_count(self) -> int:
        return 1 + len(self.duplicates)

    @property
    def court_display(self) -> str:
        """The specific court, falling back to the venue when the model filled
        one field and not the other."""
        return (self.court or self.venue or "").strip()

    @property
    def damages_display(self) -> str:
        """Never render an imputed figure as if it were a stated one."""
        if self.damages_usd:
            return f"${self.damages_usd:,.0f}"
        return "not stated"

    @property
    def damages_sort_key(self) -> float:
        return float(self.damages_usd or 0.0)


@dataclass(slots=True)
class CourtRow:
    court_id: str
    full_name: str
    jurisdiction: str
    entry_types: str
    confidence: str


@dataclass(slots=True)
class SourceRow:
    source_id: str
    display_name: str
    tier: str
    status: str
    health: str
    health_note: str
    last_success_at: str
    consecutive_failures: int
    items: int


@dataclass(slots=True)
class ClaimsRow:
    court: str
    case_number: str
    debtor: str
    vendor_id: str
    agent_raw: str
    agent_case_url: str
    date_filed: str


@dataclass(slots=True)
class Dataset:
    generated_at: str
    purpose: str
    data_root: str
    prospects: list[Prospect] = field(default_factory=list)
    courts: list[CourtRow] = field(default_factory=list)
    sources: list[SourceRow] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    coverage_summary: dict[str, int] = field(default_factory=dict)
    last_run_id: str = ""
    claims: list[ClaimsRow] = field(default_factory=list)
    claims_by_vendor: list[tuple[str, int]] = field(default_factory=list)
    claims_unmapped: list[tuple[str, str, int]] = field(default_factory=list)

    @property
    def dark_venues(self) -> int:
        return self.coverage_summary.get("low", 0)

    @property
    def partial_venues(self) -> int:
        return self.coverage_summary.get("partial", 0)

    @property
    def broken_sources(self) -> list[SourceRow]:
        return [s for s in self.sources if s.health not in ("HEALTHY", "unknown")]

    @property
    def imputed_count(self) -> int:
        return sum(1 for p in self.prospects if p.damages_imputed)


def _payload(row: Any) -> dict[str, Any]:
    try:
        return json.loads(row["payload_json"] or "{}")
    except (json.JSONDecodeError, TypeError, KeyError):
        return {}


def _components(row: Any) -> dict[str, Any]:
    try:
        return json.loads(row["components_json"] or "{}")
    except (json.JSONDecodeError, TypeError, KeyError):
        return {}


def load(db: Database, cfg: Config, *, limit: int | None = None) -> Dataset:
    """Assemble everything the delivery layer needs, in one pass."""
    top_n = limit if limit is not None else cfg.top_n_dashboard

    counts = db.pipeline_counts()
    per_source = {r["source_id"]: r["n"] for r in db.items_by_source()}

    sources = [
        SourceRow(
            source_id=r["source_id"],
            display_name=r["display_name"] or r["source_id"],
            tier=r["tier"] or "?",
            status=r["status"] or "",
            health=r["health"] or "unknown",
            health_note=r["health_note"] or "",
            last_success_at=r["last_success_at"] or "",
            consecutive_failures=int(r["consecutive_failures"] or 0),
            items=int(per_source.get(r["source_id"], 0)),
        )
        for r in db.source_rows()
    ]

    courts = [
        CourtRow(
            court_id=r["court_id"],
            full_name=r["full_name"] or "",
            jurisdiction=r["jurisdiction"] or "",
            entry_types=r["entry_types"] or "",
            confidence=r["confidence"] or "not_applicable",
        )
        for r in db.all_court_coverage()
    ]
    coverage_summary = {r["confidence"]: int(r["n"]) for r in db.coverage_summary()}

    top_rows = db.top_prospects(limit=top_n)

    # One query for every cluster's other documents, rather than one per row.
    dupes_by_key: dict[str, list[dict[str, str]]] = {}
    for d in db.cluster_members([r["cluster_key"] for r in top_rows]):
        dupes_by_key.setdefault(d["cluster_key"], []).append({
            "source_id": d["source_id"] or "",
            "url": d["source_url"] or "",
            "title": (d["case_caption"] or d["item_title"] or "").strip(),
            "event_date": (d["event_date"] or "")[:10],
        })

    prospects: list[Prospect] = []
    for i, r in enumerate(top_rows, start=1):
        p = _payload(r)
        comps = _components(r)
        dmg = p.get("damages") or {}

        imputed = bool(comps.get("damages_imputed"))
        amount = r["damages_usd"]
        venue = (r["venue"] or "").strip()
        court = (r["court"] or "").strip()
        jgroup, jlabel = normalize_jurisdiction(
            (r["jurisdiction"] or "").strip(), venue, court
        )
        stage, stage_basis = derive_stage(
            (p.get("procedural_posture") or ""), (r["event_type"] or "")
        )
        cp = [str(x).strip() for x in (p.get("counsel_plaintiff") or []) if str(x).strip()]
        cd = [str(x).strip() for x in (p.get("counsel_defendant") or []) if str(x).strip()]
        prospects.append(
            Prospect(
                rank=i,
                item_uid=r["item_uid"],
                score=float(r["score"] or 0.0),
                caption=(r["case_caption"] or r["item_title"] or "").strip(),
                summary=(r["summary"] or "").strip(),
                court=(r["court"] or "").strip(),
                venue=(r["venue"] or "").strip(),
                jurisdiction=(r["jurisdiction"] or "").strip(),
                practice_area=(r["practice_area"] or "unknown").strip(),
                deal_thesis=(r["deal_thesis"] or "none").strip(),
                event_type=(r["event_type"] or "no_event").strip(),
                event_date=(r["event_date"] or "") or "",
                published_at=(r["published_at"] or "") or "",
                source_id=r["source_id"] or "",
                source_url=r["source_url"] or "",
                damages_usd=r["damages_usd"],
                damages_conf=(r["damages_conf"] or "none"),
                damages_imputed=bool(comps.get("damages_imputed")),
                damages_basis=(dmg.get("basis") or "").strip(),
                docket_number=(p.get("docket_number") or "").strip(),
                procedural_posture=(p.get("procedural_posture") or "").strip(),
                appeal_status=(p.get("appeal_status") or "").strip(),
                collectability_note=(p.get("collectability_note") or "").strip(),
                defendant_is_public=bool(p.get("defendant_is_public_company")),
                defendant_ticker=(p.get("defendant_ticker") or "").strip(),
                parties_plaintiff=list(p.get("parties_plaintiff") or []),
                parties_defendant=list(p.get("parties_defendant") or []),
                caveats=(p.get("caveats") or "").strip(),
                extraction_confidence=(p.get("extraction_confidence") or "medium"),
                artifact_sha256="",
                components=comps,
                description=describe(
                    event_type=(r["event_type"] or "no_event").strip(),
                    deal_thesis=(r["deal_thesis"] or "none").strip(),
                    practice_area=(r["practice_area"] or "unknown").strip(),
                    damages_usd=amount,
                    damages_imputed=imputed,
                    venue=venue,
                    court=court,
                    defendant_is_public=bool(p.get("defendant_is_public_company")),
                ),
                size_band=size_band(amount, imputed),
                jurisdiction_group=jgroup,
                jurisdiction_label=jlabel,
                cluster_key=r["cluster_key"] or "",
                duplicates=dupes_by_key.get(r["cluster_key"] or "", []),
                stage=stage,
                stage_basis=stage_basis,
                counsel_plaintiff=cp,
                counsel_defendant=cd,
                # Distinguishes "the document named no counsel" from "this row
                # predates counsel capture". A blank cell means the same thing
                # either way to the reader, but only one of them is fixable by
                # re-extracting.
                counsel_known=int(r["schema_version"] or 0) >= 2,
            )
        )

    last = db.last_run()

    claims = [
        ClaimsRow(
            court=r["court"] or "",
            case_number=r["case_number"] or "",
            debtor=r["debtor"] or "",
            vendor_id=r["vendor_id"] or "",
            agent_raw=r["agent_raw"] or "",
            agent_case_url=r["agent_case_url"] or "",
            date_filed=r["date_filed"] or "",
        )
        for r in db.claims_assignments(limit=400)
    ]
    claims_by_vendor = [
        (r["vendor_id"] or "?", int(r["n"])) for r in db.claims_vendor_counts()
    ]
    claims_unmapped = [
        (r["agent_raw"] or "", r["court"] or "", int(r["n"]))
        for r in db.claims_unmapped()
    ]

    return Dataset(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        purpose=str(cfg.purpose),
        data_root=str(cfg.data_root),
        prospects=prospects,
        courts=courts,
        sources=sources,
        counts=counts,
        coverage_summary=coverage_summary,
        last_run_id=(last["run_id"] if last else ""),
        claims=claims,
        claims_by_vendor=claims_by_vendor,
        claims_unmapped=claims_unmapped,
    )


def to_json(data: Dataset) -> dict[str, Any]:
    """Serialize for embedding in the dashboard's inline <script>."""
    return {
        "generated_at": data.generated_at,
        "purpose": data.purpose,
        "last_run_id": data.last_run_id,
        "counts": data.counts,
        "coverage_summary": data.coverage_summary,
        "band_order": list(BAND_ORDER),
        "stage_order": list(STAGE_ORDER),
        "prospects": [
            {
                "rank": p.rank,
                "uid": p.item_uid,
                "score": round(p.score, 4),
                "caption": p.caption,
                "summary": p.summary,
                "description": p.description,
                "band": p.size_band,
                "jgroup": p.jurisdiction_group,
                "jlabel": p.jurisdiction_label,
                "court": p.court,
                "venue": p.venue,
                "jurisdiction": p.jurisdiction,
                "practice_area": p.practice_area,
                "thesis": p.deal_thesis,
                "event": p.event_type,
                "event_date": p.event_date,
                "published_at": p.published_at,
                "source_id": p.source_id,
                "url": p.source_url,
                "damages": p.damages_usd,
                "damages_display": p.damages_display,
                "damages_conf": p.damages_conf,
                "imputed": p.damages_imputed,
                "damages_basis": p.damages_basis,
                "docket": p.docket_number,
                "posture": p.procedural_posture,
                "appeal": p.appeal_status,
                "collectability": p.collectability_note,
                "public_defendant": p.defendant_is_public,
                "ticker": p.defendant_ticker,
                "plaintiffs": p.parties_plaintiff,
                "defendants": p.parties_defendant,
                "caveats": p.caveats,
                "confidence": p.extraction_confidence,
                "components": p.components,
                "docs": p.document_count,
                "duplicates": p.duplicates,
                "stage": p.stage,
                "stage_rank": stage_rank(p.stage),
                "stage_basis": p.stage_basis,
                "court_display": p.court_display,
                "counsel_p": p.counsel_plaintiff,
                "counsel_d": p.counsel_defendant,
                "counsel_known": p.counsel_known,
            }
            for p in data.prospects
        ],
        "courts": [
            {
                "id": c.court_id,
                "name": c.full_name,
                "jurisdiction": c.jurisdiction,
                "entry_types": c.entry_types,
                "confidence": c.confidence,
            }
            for c in data.courts
        ],
        "sources": [
            {
                "id": s.source_id,
                "name": s.display_name,
                "tier": s.tier,
                "status": s.status,
                "health": s.health,
                "note": s.health_note,
                "last_success": s.last_success_at,
                "fails": s.consecutive_failures,
                "items": s.items,
            }
            for s in data.sources
        ],
    }


def top(data: Dataset, n: int) -> Sequence[Prospect]:
    return data.prospects[:n]
