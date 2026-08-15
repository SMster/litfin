"""Event taxonomy: mapping text to the three deal theses.

Division of labour: regex proposes, Opus disposes. Patterns here are a cheap
first pass that decides *what is worth sending to the LLM*, and they are
deliberately biased toward recall. The LLM stage makes the judgment calls that
patterns cannot:

  * whether a settlement resolves the WHOLE case or one defendant
  * damages figures embedded in prose ("a verdict of two hundred forty
    million dollars")
  * proposed vs entered judgment where the document label is ambiguous
  * whether a dismissal is a settlement tell or a merits loss

Ordering matters throughout. "proposed final judgment" must be tested before
"final judgment", or every Tunney Act settlement still inside its 60-day
comment window is misread as an entered judgment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Thesis(StrEnum):
    """The three deal theses this pipeline scores for."""

    JUDGMENT = "judgment_monetization"      # judgment entered, appeal pending
    SETTLEMENT = "post_settlement"          # settled, payment stream not yet received
    ANTITRUST = "antitrust_followon"        # enforcement -> private treble damages
    NONE = "none"


class EventClass(StrEnum):
    JUDGMENT_ENTERED = "judgment_entered"
    JUDGMENT_PROPOSED = "judgment_proposed"
    VERDICT = "verdict"
    APPEAL = "appeal"
    SETTLEMENT_REACHED = "settlement_reached"
    SETTLEMENT_PROPOSED = "settlement_proposed"
    SETTLEMENT_APPROVED = "settlement_approved"
    ENFORCEMENT = "enforcement"
    CASE_FILED = "case_filed"
    OTHER = "other"


def _rx(*alts: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{a})" for a in alts), re.IGNORECASE)


# --- Judgment monetization / appeal funding --------------------------------
# The classic de-risked asset: liability decided, quantum known, collection or
# appeal outstanding.
PROPOSED_JUDGMENT = _rx(
    r"\bproposed\s+final\s+judgment\b",
    r"\bproposed\s+consent\s+judgment\b",
    r"\bcompetitive\s+impact\s+statement\b",
)
JUDGMENT_ENTERED = _rx(
    r"\bentry\s+of\s+(?:final\s+)?judgment\b",
    r"\bjudgment\s+(?:was\s+|is\s+|be\s+)?entered\b",
    r"\bentered\s+(?:a\s+)?(?:final\s+)?judgment\b",
    r"\bfinal\s+judgment\b",
    r"\bamended\s+judgment\b",
    r"\bconsent\s+judgment\b",
    r"\bdefault\s+judgment\b",
)
VERDICT = _rx(
    r"\bjury\s+(?:verdict|returned|awarded|found)\b",
    r"\bverdict\s+(?:in\s+favor|for|against)\b",
    r"\breturned\s+a\s+verdict\b",
    r"\bbench\s+trial\s+(?:verdict|ruling)\b",
)

# The same substring trap as "proposed final judgment" / "final judgment".
#
# FOUND IN LIVE DATA: "Proposed Jury Verdict by USA as to William Stanley" is
# a blank verdict FORM filed during trial prep -- routine housekeeping, not an
# outcome. The VERDICT pattern above matches "jury verdict" inside it and
# scored these 0.9, sending pure noise to the LLM. Opus correctly returned
# no_event for every one, which is how the bug surfaced.
#
# Checked BEFORE VERDICT in classify_text().
PROPOSED_VERDICT = _rx(
    r"\bproposed\s+(?:jury\s+)?verdict\b",
    r"\bverdict\s+form\b",
    r"\bproposed\s+jury\s+(?:instructions|charge)\b",
    r"\bjoint\s+proposed\s+verdict\b",
)
APPEAL = _rx(
    r"\bnotice\s+of\s+appeal\b",
    r"\bsupersedeas\s+bond\b",
    r"\bappeal\s+bond\b",
    r"\brule\s*(?:50|59|60)\b",
    r"\bjudgment\s+notwithstanding\s+the\s+verdict\b",
    r"\bjnov\b",
    r"\bmotion\s+for\s+new\s+trial\b",
    r"\bpost[- ]trial\s+motion\b",
)

# --- Post-settlement receivable --------------------------------------------
SETTLEMENT_REACHED = _rx(
    r"\bnotice\s+of\s+settlement\b",
    r"\bsettlement\s+agreement\b",
    r"\bstipulation\s+of\s+(?:settlement|dismissal|discontinuance)\b",
    r"\bmemorandum\s+of\s+understanding\b",
    r"\bagreed\s+to\s+settle\b",
    r"\bhas\s+settled\b",
    r"\bresolved\s+all\s+claims\b",
    r"\bfully\s+and\s+finally\s+resolve\b",
    r"\breleased\s+all\s+claims\b",
    r"\bwithout\s+admitting\s+(?:any\s+)?liability\b",
    r"\bcompromise\s+(?:of|and)\s+controvers",
    r"\b(?:rule\s*)?9019\b",           # bankruptcy settlement motions
)
SETTLEMENT_APPROVED = _rx(
    r"\bpreliminary\s+approval\b",
    r"\bfinal\s+approval\b",
    r"\bapproved\s+the\s+settlement\b",
    r"\bfairness\s+hearing\b",
    r"\bnotice\s+of\s+pendency\b",
)

# --- Antitrust follow-on ---------------------------------------------------
ANTITRUST = _rx(
    r"\bantitrust\b",
    r"\bsherman\s+act\b",
    r"\bclayton\s+act\b",
    r"\brobinson[- ]patman\b",
    r"\bprice[- ]fixing\b",
    r"\bbid[- ]rigging\b",
    r"\bmarket\s+allocation\b",
    r"\bcartel\b",
    r"\btunney\s+act\b",
    r"\bconsent\s+decree\b",
    r"\btreble\s+damages\b",
    r"\bmonopoli[sz]",
    # Document types that exist ONLY under the Tunney Act / merger review and
    # are therefore definitionally antitrust. Without these, a DOJ
    # "Competitive Impact Statement" carries no antitrust keyword in its title
    # and gets classified post_settlement instead of antitrust_followon.
    r"\bcompetitive\s+impact\s+statement\b",
    r"\bexplanation\s+of\s+consent\s+decree\s+procedures\b",
    r"\basset\s+preservation\s+stipulation\b",
    r"\bhold\s+separate\s+(?:stipulation|agreement)\b",
    r"\bantitrust\s+procedures\s+and\s+penalties\s+act\b",
    r"\bhart[- ]scott[- ]rodino\b",
    r"\bsecond\s+request\b",
)

# --- Bankruptcy (a practice-area filter, not a thesis) ---------------------
BANKRUPTCY = _rx(
    r"\bchapter\s*(?:7|11|15)\b",
    r"\bdebtor[- ]in[- ]possession\b",
    r"\bplan\s+of\s+reorganization\b",
    r"\bplan\s+confirmation\b",
    r"\bdisclosure\s+statement\b",
    r"\badversary\s+proceeding\b",
    r"\bavoidance\s+action\b",
    r"\bpreference\s+(?:action|claim)\b",
    r"\bfraudulent\s+(?:transfer|conveyance)\b",
    r"\blitigation\s+trust\b",
    r"\b(?:rule\s*)?9019\b",
)

COMMERCIAL = _rx(
    r"\bbreach\s+of\s+contract\b",
    r"\bcommercial\s+(?:dispute|litigation)\b",
    r"\bbreach\s+of\s+fiduciary\s+dut",
    r"\bfraud\b",
    r"\bunjust\s+enrichment\b",
    r"\btortious\s+interference\b",
    r"\bshareholder\s+(?:suit|derivative)\b",
    r"\bsecurities\s+(?:class\s+action|fraud)\b",
)


@dataclass(slots=True)
class TaxonomyHit:
    thesis: Thesis
    event_class: EventClass
    matched: list[str]
    strength: float          # 0..1, how confidently the text signals the thesis

    @property
    def is_signal(self) -> bool:
        return self.thesis is not Thesis.NONE


def _found(pattern: re.Pattern[str], text: str) -> list[str]:
    return [m.group(0).strip() for m in pattern.finditer(text)][:6]


def classify_text(text: str) -> TaxonomyHit:
    """Cheap first-pass classification. Biased toward recall.

    A false positive costs one LLM call. A false negative is invisible and
    loses a deal, so when in doubt this lets the text through.
    """
    t = text or ""

    proposed = _found(PROPOSED_JUDGMENT, t)
    entered = _found(JUDGMENT_ENTERED, t)
    # A proposed verdict form is trial-prep paperwork, not an outcome. Must be
    # tested before VERDICT, whose pattern it contains as a substring.
    proposed_verdict = _found(PROPOSED_VERDICT, t)
    verdict = [] if proposed_verdict else _found(VERDICT, t)
    appeal = _found(APPEAL, t)
    settled = _found(SETTLEMENT_REACHED, t)
    approved = _found(SETTLEMENT_APPROVED, t)
    antitrust = _found(ANTITRUST, t)

    # Order matters: a proposed judgment is a SETTLEMENT still in its comment
    # window, not an entered judgment. Checking "final judgment" first would
    # misclassify every Tunney Act filing.
    if proposed:
        thesis = Thesis.ANTITRUST if antitrust else Thesis.SETTLEMENT
        return TaxonomyHit(thesis, EventClass.SETTLEMENT_PROPOSED, proposed,
                           0.85 if antitrust else 0.7)

    if verdict:
        return TaxonomyHit(Thesis.JUDGMENT, EventClass.VERDICT, verdict, 0.9)

    if entered and appeal:
        # Judgment entered AND an appeal in motion is the single most
        # on-thesis combination for appeal funding.
        return TaxonomyHit(Thesis.JUDGMENT, EventClass.APPEAL,
                           entered + appeal, 0.95)

    if entered:
        return TaxonomyHit(Thesis.JUDGMENT, EventClass.JUDGMENT_ENTERED,
                           entered, 0.8)

    if appeal:
        return TaxonomyHit(Thesis.JUDGMENT, EventClass.APPEAL, appeal, 0.6)

    if approved:
        return TaxonomyHit(Thesis.SETTLEMENT, EventClass.SETTLEMENT_APPROVED,
                           approved, 0.8)

    if settled:
        thesis = Thesis.ANTITRUST if antitrust else Thesis.SETTLEMENT
        return TaxonomyHit(thesis, EventClass.SETTLEMENT_REACHED, settled,
                           0.85 if antitrust else 0.75)

    if antitrust:
        # Enforcement activity with no outcome language yet: the leading
        # indicator for a follow-on private damages action.
        return TaxonomyHit(Thesis.ANTITRUST, EventClass.ENFORCEMENT,
                           antitrust, 0.4)

    return TaxonomyHit(Thesis.NONE, EventClass.OTHER, [], 0.0)


def practice_area_hints(text: str) -> list[str]:
    """Non-exclusive practice-area signals, for the LLM prompt and scoring."""
    t = text or ""
    hints: list[str] = []
    if _found(ANTITRUST, t):
        hints.append("antitrust")
    if _found(BANKRUPTCY, t):
        hints.append("bankruptcy")
    if _found(COMMERCIAL, t):
        hints.append("commercial")
    return hints
