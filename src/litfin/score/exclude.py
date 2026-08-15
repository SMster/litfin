"""Exclusion screen for IP, international arbitration, and consumer litigation.

Nature-of-suit codes are absent or unreliable in nearly every free source, so
exclusion cannot lean on them. Two stages:

  1. A cheap lexical screen (here), biased toward RECALL.
  2. LLM adjudication on survivors, which sets `practice_area` and
     `excluded_reason` in the extraction schema.

The recall/precision asymmetry is deliberate and worth stating plainly: a
false EXCLUSION is invisible -- the case never appears, nobody knows to look
for it, and a deal is lost silently. A false INCLUSION costs one row of the
user's attention on a 100-row list. So this stage only excludes on strong,
unambiguous signals and defers everything else to the LLM.

Every exclusion is logged with its reason so the filter can be audited. A
screen you cannot audit is a screen you cannot trust.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class ExcludedArea(StrEnum):
    IP = "intellectual_property"
    INTL_ARBITRATION = "international_arbitration"
    CONSUMER = "consumer"
    CRIMINAL = "criminal"
    FORFEITURE = "government_forfeiture"
    NONE = "none"


def _rx(*alts: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{a})" for a in alts), re.IGNORECASE)


# --- IP --------------------------------------------------------------------
# Note "patent" alone is NOT enough: "patently unfair" and "patent ambiguity"
# are ordinary commercial-litigation language. Require IP context.
IP_PATTERNS = _rx(
    r"\bpatent\s+(?:infringement|litigation|invalidity|troll)\b",
    r"\b(?:infringement|invalidity)\s+of\s+(?:the\s+)?(?:'\d+|patent)\b",
    r"\btrademark\s+(?:infringement|dilution|opposition)\b",
    r"\bcopyright\s+infringement\b",
    r"\btrade\s+secret\s+misappropriation\b",
    r"\bhatch[- ]waxman\b",
    r"\banda\b",
    r"\bpatent\s+trial\s+and\s+appeal\s+board\b",
    r"\bptab\b",
    r"\binter\s+partes\s+review\b",
    r"\bsection\s+337\b",
    r"\bitc\s+investigation\b",
    r"\blanham\s+act\b",
    r"\bU\.?S\.?\s*Patent\s+No\b",
)

# --- International arbitration ---------------------------------------------
# "arbitration" alone is far too broad -- domestic commercial arbitration is
# in scope. Require an international forum, treaty, or seat.
INTL_ARB_PATTERNS = _rx(
    r"\bicsid\b",
    r"\buncitral\b",
    r"\binternational\s+chamber\s+of\s+commerce\b",
    r"\bicc\s+arbitration\b",
    r"\blcia\b",
    r"\bsiac\b",
    r"\bhkiac\b",
    r"\bstockholm\s+chamber\s+of\s+commerce\b",
    r"\bpermanent\s+court\s+of\s+arbitration\b",
    r"\binvestor[- ]state\s+dispute\b",
    r"\bbilateral\s+investment\s+treaty\b",
    r"\benergy\s+charter\s+treaty\b",
    r"\bnew\s+york\s+convention\b",
    r"\bforeign\s+arbitral\s+award\b",
    r"\binternational\s+arbitration\b",
    r"\bseat\s+of\s+(?:the\s+)?arbitration\b",
)

# --- Consumer --------------------------------------------------------------
CONSUMER_PATTERNS = _rx(
    r"\btcpa\b",
    r"\btelephone\s+consumer\s+protection\s+act\b",
    r"\bfdcpa\b",
    r"\bfair\s+debt\s+collection\b",
    r"\bfcra\b",
    r"\bfair\s+credit\s+reporting\b",
    r"\btruth\s+in\s+lending\b",
    r"\btila\b",
    r"\blemon\s+law\b",
    r"\bconsumer\s+protection\s+(?:act|claim)\b",
    r"\bmagnuson[- ]moss\b",
    r"\bunfair\s+and\s+deceptive\s+(?:acts|trade)\b",
    r"\bdeceptive\s+trade\s+practices\b",
    r"\bconsumer\s+class\s+action\b",
    r"\brobocall\b",
    # High-precision consumer-protection prose. These appear in FTC press
    # releases, which carry no statutory citation.
    #
    # DELIBERATELY NOT INCLUDED: the bare word "consumer" or "consumers".
    # Consumer harm is the central standard in antitrust analysis, so
    # excluding on it would silently drop exactly the antitrust matters this
    # pipeline is built to find -- the most expensive kind of error here,
    # because a false exclusion is invisible.
    r"\bcredit\s+repair\s+(?:scheme|scam|operation)\b",
    r"\bconsumer\s+redress\b",
    r"\bdeceptive\s+marketing\b",
    r"\bscammed\s+consumers\b",
    r"\bdefrauded\s+consumers\b",
    r"\bdebt\s+relief\s+(?:scheme|scam)\b",
)

# --- Criminal prosecutions -------------------------------------------------
# A criminal case has no damages award to monetize and no receivable to
# advance against, so it is out of scope regardless of how "late stage" it is.
#
# FOUND IN LIVE DATA: RECAP docket-text search surfaced "Proposed Jury Verdict
# by USA as to William Stanley, Heather Morrow" -- a federal criminal
# prosecution. Opus correctly classified it no_event, but the call should
# never have been made.
#
# "USA as to <name>" is the distinctive PACER criminal-docket convention and
# is the highest-precision single signal here.
CRIMINAL_PATTERNS = _rx(
    r"\bUSA\s+as\s+to\b",
    r"\bUnited\s+States\s+as\s+to\b",
    r"\b(?:superseding\s+)?indictment\b",
    r"\bplea\s+agreement\b",
    r"\bchange\s+of\s+plea\b",
    r"\bpleaded\s+guilty\b|\bpled\s+guilty\b|\bguilty\s+plea\b",
    r"\bsentencing\s+(?:hearing|memorandum|guidelines)\b",
    r"\bpresentence\s+(?:report|investigation)\b",
    r"\barraignment\b",
    r"\bcriminal\s+complaint\b",
    r"\bcount(?:s)?\s+of\s+the\s+indictment\b",
    r"\bdetention\s+hearing\b",
    r"\bsupervised\s+release\b",
)

# Criminal ANTITRUST is the exception that must survive the criminal screen.
# DOJ price-fixing and bid-rigging prosecutions are the single best leading
# indicator for follow-on private treble-damage actions -- which is one of the
# three deal theses. Excluding them would drop exactly the cases the antitrust
# thesis exists to find.
CRIMINAL_ANTITRUST_CARVEOUT = _rx(
    r"\bantitrust\b",
    r"\bsherman\s+act\b",
    r"\bprice[- ]fixing\b",
    r"\bbid[- ]rigging\b",
    r"\bmarket\s+allocation\b",
    r"\bcartel\b",
    r"\bper\s+se\s+violation\b",
)

# --- Government asset forfeiture -------------------------------------------
# FOUND IN LIVE DATA, and it is the most instructive false positive so far:
# "United States v. Approximately 225,364,961 USDT" ranked in the top 100
# FIVE times on a "damages" figure of $225,364,961 -- which is the seized
# amount in the CASE NAME. The model read it correctly (a dollar figure is
# stated in the document); the pipeline was wrong to treat it as a claim.
#
# A government in rem forfeiture has no assignable claim and no counterparty
# to collect from. The claimants are third parties asking for their property
# back, not plaintiffs with a receivable. It fits none of the three theses at
# any score, so it is excluded rather than merely down-weighted.
#
# THE PRECISION PROBLEM IS THE WHOLE DESIGN HERE. The bare word "forfeiture"
# is ordinary commercial language -- forfeiture of a deposit, of unvested
# shares, of a lease, an anti-forfeiture clause -- and excluding on it would
# silently drop real commercial matters. That is the invisible failure the
# screen is built to avoid. So every pattern below requires either the in rem
# CAPTION convention (the defendant is a thing) or an explicit
# forfeiture-proceeding phrase or statute.
FORFEITURE_PATTERNS = _rx(
    # The in rem caption convention: United States v. <a thing>. This is the
    # single highest-precision signal -- civil plaintiffs do not sue objects.
    r"\bUnited\s+States(?:\s+of\s+America)?\s+v\.?\s+"
    r"(?:approximately|any\s+and\s+all|all\s+funds|one\s+\d|"
    r"real\s+property|\$[\d,]+|\d[\d,]*\.?\d*\s*(?:USDT|USDC|BTC|ETH|"
    r"bitcoin|dollars))",
    r"\bUSA\s+v\.?\s+approximately\b",
    # Explicit proceeding names.
    r"\bcivil\s+(?:asset\s+)?forfeiture\b",
    r"\b(?:verified\s+)?complaint\s+for\s+forfeiture\b",
    r"\bforfeiture\s+(?:complaint|action|proceeding|proceedings)\b",
    r"\bdecree\s+of\s+forfeiture\b",
    r"\bnotice\s+of\s+forfeiture\b",
    r"\bin\s+rem\s+forfeiture\b",
    # Forfeiture statutes. Unambiguous where they appear.
    r"\b18\s+U\.?S\.?C\.?\s*§*\s*98[1-5]\b",
    r"\b21\s+U\.?S\.?C\.?\s*§*\s*8(?:81|53)\b",
    r"\b28\s+U\.?S\.?C\.?\s*§*\s*2461\b",
    r"\bCAFRA\b",
    r"\bcivil\s+asset\s+forfeiture\s+reform\s+act\b",
)

# DELIBERATELY NOT PATTERNS, recorded so nobody "improves" the list:
#   "forfeiture"        -- deposits, unvested equity, leases, bonds
#   "forfeit"           -- same
#   "seizure"/"seized"  -- attachment and replevin are ordinary remedies a
#                          commercial plaintiff may well have won
#   "asset recovery"    -- that is a description of this pipeline's own
#                          subject matter, not of forfeiture

_AREA_PATTERNS: list[tuple[ExcludedArea, re.Pattern[str]]] = [
    (ExcludedArea.IP, IP_PATTERNS),
    (ExcludedArea.INTL_ARBITRATION, INTL_ARB_PATTERNS),
    (ExcludedArea.CONSUMER, CONSUMER_PATTERNS),
    (ExcludedArea.FORFEITURE, FORFEITURE_PATTERNS),
]


_REASONS: dict[ExcludedArea, str] = {
    ExcludedArea.FORFEITURE: (
        "government asset forfeiture -- no assignable claim and no "
        "counterparty to collect from; any dollar figure is the SEIZED "
        "amount, not a claim"
    ),
}


@dataclass(slots=True)
class ExclusionVerdict:
    excluded: bool
    area: ExcludedArea
    matched: list[str]
    reason: str = ""

    @property
    def audit_line(self) -> str:
        if not self.excluded:
            return "kept"
        return f"excluded[{self.area}] on {', '.join(self.matched[:3])}"


def screen(text: str) -> ExclusionVerdict:
    """Cheap lexical exclusion screen.

    Only fires on strong, unambiguous signals. Anything borderline passes
    through to the LLM stage, which has the context to judge properly.
    """
    t = text or ""
    for area, pattern in _AREA_PATTERNS:
        matches = [m.group(0).strip() for m in pattern.finditer(t)][:6]
        if matches:
            return ExclusionVerdict(
                excluded=True,
                area=area,
                matched=matches,
                reason=_REASONS.get(area, "lexical screen matched {area} terms")
                .format(area=area) + f": {', '.join(matches[:3])}",
            )

    # Criminal is checked last and carries a carve-out: DOJ criminal antitrust
    # (price-fixing, bid-rigging) is a leading indicator for follow-on private
    # damages and must NOT be excluded.
    criminal = [m.group(0).strip() for m in CRIMINAL_PATTERNS.finditer(t)][:6]
    if criminal:
        if CRIMINAL_ANTITRUST_CARVEOUT.search(t):
            return ExclusionVerdict(
                False, ExcludedArea.NONE, criminal,
                "criminal signals present but antitrust carve-out applies -- "
                "criminal antitrust leads to follow-on civil damages",
            )
        return ExclusionVerdict(
            excluded=True,
            area=ExcludedArea.CRIMINAL,
            matched=criminal,
            reason=(
                f"criminal prosecution -- no damages award to monetize: "
                f"{', '.join(criminal[:3])}"
            ),
        )

    return ExclusionVerdict(False, ExcludedArea.NONE, [], "")


def screen_many(texts: dict[str, str]) -> dict[str, ExclusionVerdict]:
    """Screen a batch, keyed by item id, for auditing."""
    return {k: screen(v) for k, v in texts.items()}


# Fields of a stored extraction worth re-screening. The caption matters most:
# the in rem forfeiture convention lives there and nowhere else, which is
# exactly why the pre-extraction screen over the raw document missed it -- a
# RECAP docket entry's body is procedural text, and only the caption says
# "United States v. Approximately 225,364,961 USDT".
_EXTRACTION_FIELDS = ("case_caption", "summary", "procedural_posture")


def screen_extraction(row: dict) -> ExclusionVerdict:
    """Re-run the exclusion screen over an ALREADY-EXTRACTED row.

    The pre-extraction screen reads the source document; this reads what the
    model made of it. Both are needed, and the second is not redundant:

      * the model normalizes a caption the raw document never stated cleanly
      * the pattern list changes over time, and a screen that only ever ran
        before extraction would leave every previously-stored row judged by
        an older, weaker filter

    Running offline over stored extractions is cheap and reversible, which is
    the same property that makes re-weighting cheap.
    """
    blob = " ".join(str(row.get(f) or "") for f in _EXTRACTION_FIELDS)
    return screen(blob)
