"""Structured output schema for the Opus extraction stage.

Two design points worth keeping:

1. Every uncertain field carries an explicit confidence, and damages carry
   their own. Most free sources never state a figure, so the pipeline must be
   able to distinguish "settled for $40M" from "probably eight figures" and
   show the user which is which. A model that silently guesses a number is far
   worse than one that says it does not know.

2. `excluded_area` is part of the extraction, not a separate pass. The model
   is already reading the document; asking it to adjudicate practice area in
   the same call is cheaper and better-informed than a second call.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class PracticeArea(StrEnum):
    COMMERCIAL = "commercial"
    ANTITRUST = "antitrust"
    BANKRUPTCY = "bankruptcy"
    SECURITIES = "securities"
    # Excluded areas, still enumerated so the model can name what it saw.
    INTELLECTUAL_PROPERTY = "intellectual_property"
    INTERNATIONAL_ARBITRATION = "international_arbitration"
    CONSUMER = "consumer"
    OTHER = "other"
    UNKNOWN = "unknown"


class DealThesis(StrEnum):
    JUDGMENT_MONETIZATION = "judgment_monetization"
    POST_SETTLEMENT = "post_settlement"
    ANTITRUST_FOLLOWON = "antitrust_followon"
    NONE = "none"


class EventType(StrEnum):
    JUDGMENT_ENTERED = "judgment_entered"
    JUDGMENT_PROPOSED = "judgment_proposed"
    JURY_VERDICT = "jury_verdict"
    APPEAL_FILED = "appeal_filed"
    SETTLEMENT_REACHED = "settlement_reached"
    SETTLEMENT_PRELIMINARY_APPROVAL = "settlement_preliminary_approval"
    SETTLEMENT_FINAL_APPROVAL = "settlement_final_approval"
    ENFORCEMENT_ACTION = "enforcement_action"
    PLAN_CONFIRMATION = "plan_confirmation"
    CASE_FILED = "case_filed"
    NO_EVENT = "no_event"


Confidence = Literal["high", "medium", "low", "none"]


class Damages(BaseModel):
    """Monetary quantum. `amount_usd` is null far more often than not."""

    amount_usd: float | None = Field(
        default=None,
        description=(
            "Total monetary amount in USD, if a specific figure is stated. "
            "Null if no figure appears -- do NOT estimate, infer, or "
            "annualize. A null here is a correct and expected answer."
        ),
    )
    confidence: Confidence = Field(
        default="none",
        description=(
            "'high' only if an explicit figure is stated in the document. "
            "'medium' if a range or approximation is stated. 'low' if it is "
            "implied but not stated. 'none' if absent."
        ),
    )
    basis: str = Field(
        default="",
        description="Short verbatim quote supporting the figure, if any.",
    )
    is_aggregate: bool = Field(
        default=False,
        description="True if the figure covers multiple defendants or claims.",
    )


class CaseExtraction(BaseModel):
    """What the model returns for one candidate document."""

    # -- identification
    case_caption: str = Field(
        default="", description="Full case caption, e.g. 'Acme Corp v. Widget Inc.'"
    )
    court: str = Field(default="", description="Deciding court, as named in the document.")
    venue: str = Field(default="", description="City/district, e.g. 'S.D.N.Y.'")
    jurisdiction: str = Field(
        default="",
        description="'federal', 'state', or the specific state name.",
    )
    docket_number: str = Field(default="")

    # -- classification
    practice_area: PracticeArea = PracticeArea.UNKNOWN
    practice_area_confidence: Confidence = "none"
    deal_thesis: DealThesis = DealThesis.NONE
    event_type: EventType = EventType.NO_EVENT
    event_date: str | None = Field(
        default=None, description="ISO-8601 date of the event, if stated."
    )

    # -- exclusion adjudication (done in the same call, not a second pass)
    is_excluded: bool = Field(
        default=False,
        description=(
            "True if this is IP/patent, international arbitration, or consumer "
            "litigation -- all out of scope."
        ),
    )
    excluded_reason: str = Field(default="")

    # -- substance
    summary: str = Field(
        default="",
        description=(
            "2-3 sentences in plain English: what the dispute is about and "
            "what just happened. Written for a reader triaging 100 rows."
        ),
    )
    damages: Damages = Field(default_factory=Damages)
    parties_plaintiff: list[str] = Field(default_factory=list)
    parties_defendant: list[str] = Field(default_factory=list)
    defendant_is_public_company: bool = False
    defendant_ticker: str = Field(default="")

    # -- counsel
    #
    # Firms, not individual lawyers. Who is on a matter is a real signal for
    # a funder: a contingency-fee plaintiff firm is a different conversation
    # from an hourly one, and repeat-player defense counsel says something
    # about how the case will be run.
    #
    # Expected to be EMPTY most of the time, and the prompt says so. Docket
    # entries name counsel; agency press releases almost never do. An empty
    # list is a correct answer and is rendered as a blank, never guessed --
    # inventing a plausible firm name would be worse than showing nothing.
    counsel_plaintiff: list[str] = Field(
        default_factory=list,
        description=(
            "Law FIRMS representing the plaintiff(s), exactly as named in the "
            "document. Firm names only, not individual attorneys. Empty list "
            "if counsel is not stated -- do NOT infer or guess."
        ),
    )
    counsel_defendant: list[str] = Field(
        default_factory=list,
        description=(
            "Law FIRMS representing the defendant(s), exactly as named in the "
            "document. Firm names only, not individual attorneys. Empty list "
            "if counsel is not stated -- do NOT infer or guess."
        ),
    )

    # -- posture
    procedural_posture: str = Field(
        default="",
        description="Where the case stands now, one sentence.",
    )
    appeal_status: str = Field(
        default="",
        description="'none', 'notice filed', 'pending', 'decided', or unknown.",
    )
    collectability_note: str = Field(
        default="",
        description=(
            "Anything bearing on whether a judgment could actually be "
            "collected: solvency, insurance, bonding, bankruptcy."
        ),
    )

    # -- model self-assessment
    extraction_confidence: Confidence = "medium"
    caveats: str = Field(
        default="",
        description="Anything the model is unsure about. Empty is fine.",
    )


def _harden(node: object) -> object:
    """Make a Pydantic-generated schema acceptable to structured outputs.

    Two things the API requires that Pydantic does not emit:

      1. `additionalProperties: false` on EVERY object, including nested $defs.
         Without it the request fails with:
           "For 'object' type, 'additionalProperties' must be explicitly set
            to false"

      2. Every property listed in `required`. Pydantic omits fields that have
         defaults, but structured outputs wants the full key set so the shape
         is guaranteed. Our fields all have defaults, so the model can still
         return an empty string / null / [] where it has nothing -- which is
         exactly the "do not guess" behavior the prompt asks for.

    Applied recursively so $defs (Damages, and every enum-backed object) are
    covered too.
    """
    if isinstance(node, dict):
        out = {k: _harden(v) for k, v in node.items()}
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"].keys())
        return out
    if isinstance(node, list):
        return [_harden(v) for v in node]
    return node


# Bumped whenever a FIELD is added or its meaning changes. Stored alongside
# each extraction so `litfin extract --refresh` can find rows captured under
# an older schema and re-run only those -- re-extracting the whole corpus
# because one field was added is a real bill.
#
#   1  initial
#   2  added counsel_plaintiff / counsel_defendant
SCHEMA_VERSION = 2


def json_schema() -> dict:
    """JSON Schema for output_config.format."""
    return _harden(CaseExtraction.model_json_schema())
