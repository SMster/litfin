"""The ranking engine.

Weighted linear score over normalized 0..1 factors. Weights live in
litfin.toml, and rescoring runs entirely off stored extractions -- no re-fetch,
no re-extraction -- so trying a different weighting costs seconds rather than
a day and an API bill.

THE MISSING-DAMAGES PROBLEM is the most consequential design decision here.
Most free sources never state a figure. Imputing zero would rank every
unlabeled case last, which is exactly backwards: the largest matters are
frequently the ones whose figures are not in the first public document. So a
missing figure falls back to a venue-and-thesis prior, takes an explicit
uncertainty discount, and is flagged in the output. The user can sort by
`damages_confidence` and see instantly which rankings rest on a real number.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# --- default weights (override in litfin.toml [score.weights]) -------------
DEFAULT_WEIGHTS: dict[str, float] = {
    "thesis_fit": 0.25,
    "damages": 0.20,
    "recency": 0.15,
    "collectability": 0.15,
    "venue": 0.10,
    "practice_fit": 0.10,
    "source_confidence": 0.05,
}

RECENCY_HALFLIFE_DAYS = 30.0

# Damages are log-scaled: the difference between $1M and $10M matters far more
# than between $500M and $510M.
DAMAGES_FLOOR = 100_000.0
DAMAGES_CEILING = 1_000_000_000.0

# Fallback priors (USD) when no figure is stated. Deliberately conservative --
# these exist to avoid burying unlabeled cases, not to invent precision.
THESIS_PRIORS: dict[str, float] = {
    "judgment_monetization": 15_000_000.0,
    "antitrust_followon": 40_000_000.0,
    "post_settlement": 10_000_000.0,
    "none": 5_000_000.0,
}

# Venues with dedicated commercial benches, deep commercial case law, or
# demonstrated willingness to enter large judgments.
VENUE_QUALITY: dict[str, float] = {
    "delaware": 1.00, "chancery": 1.00, "ded": 0.95,
    "s.d.n.y": 0.95, "sdny": 0.95, "new york": 0.90, "commercial division": 0.95,
    "n.d.cal": 0.90, "cand": 0.90, "northern district of california": 0.90,
    "e.d.tex": 0.75, "n.d.ill": 0.85, "ilnd": 0.85,
    "d.mass": 0.85, "business litigation session": 0.90,
    "north carolina business court": 0.90,
    "texas": 0.75, "florida": 0.70, "new jersey": 0.75,
    "delaware superior": 0.95, "ccld": 0.95,
}

PRACTICE_FIT: dict[str, float] = {
    "antitrust": 1.00,
    "commercial": 0.95,
    "bankruptcy": 0.85,
    "securities": 0.80,
    "other": 0.30,
    "unknown": 0.25,
    # In-scope score of zero; these are filtered before scoring anyway.
    "intellectual_property": 0.0,
    "international_arbitration": 0.0,
    "consumer": 0.0,
}

# How well each event type embodies a de-risked, late-stage asset.
#
# REBALANCED TOWARD SETTLEMENT. The original ordering put judgment_entered and
# jury_verdict at the top, which is the litigator's view of "how far has this
# case gone" rather than the funder's view of "how certain is the money".
#
# A settled case has an AGREED number and an identified payer who has already
# decided not to fight. A judgment has a number the loser did not agree to,
# and still faces post-trial motions, appeal, and collection -- three ways for
# the asset to shrink or vanish. Post-settlement receivable monetization is
# one of the three deal theses precisely because that risk is gone.
#
# So the ordering now runs: approved settlement > agreed settlement >
# judgment > verdict > appeal. A verdict sits BELOW an entered judgment
# because it is the most appealable moment in a case's life.
#
# Override any of these in litfin.toml under [score.event_fit] -- these are
# starting weights, not physics.
EVENT_FIT: dict[str, float] = {
    # Settled and blessed by the court: the money is as certain as it gets.
    "settlement_final_approval": 1.00,
    "settlement_preliminary_approval": 0.97,
    # Agreed but not yet approved -- "near settlement".
    "settlement_reached": 0.95,
    # Decided, but the loser did not agree and can still appeal or fail to pay.
    "judgment_entered": 0.88,
    "jury_verdict": 0.82,
    "appeal_filed": 0.78,
    "plan_confirmation": 0.80,
    "judgment_proposed": 0.70,
    "enforcement_action": 0.45,
    "case_filed": 0.10,
    "no_event": 0.0,
}

# An extra multiplier on thesis fit, so the settlement theses outrank the
# others at equal event strength. Deliberately gentle: antitrust follow-on is
# still a core thesis and this must not bury it.
THESIS_PRIORITY: dict[str, float] = {
    "post_settlement": 1.00,
    "antitrust_followon": 0.95,
    "judgment_monetization": 0.92,
    "none": 0.0,
}

CONFIDENCE_FACTOR: dict[str, float] = {
    "high": 1.0, "medium": 0.75, "low": 0.5, "none": 0.35, "": 0.35,
}


@dataclass(slots=True)
class ScoreComponents:
    thesis_fit: float = 0.0
    damages: float = 0.0
    recency: float = 0.0
    collectability: float = 0.0
    venue: float = 0.0
    practice_fit: float = 0.0
    source_confidence: float = 0.0
    damages_imputed: bool = False
    damages_confidence: str = "none"
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "thesis_fit": round(self.thesis_fit, 4),
            "damages": round(self.damages, 4),
            "recency": round(self.recency, 4),
            "collectability": round(self.collectability, 4),
            "venue": round(self.venue, 4),
            "practice_fit": round(self.practice_fit, 4),
            "source_confidence": round(self.source_confidence, 4),
            "damages_imputed": self.damages_imputed,
            "damages_confidence": self.damages_confidence,
            "notes": self.notes,
        }


def _norm_damages(amount: float) -> float:
    """Log-scale a dollar figure into 0..1."""
    if amount <= DAMAGES_FLOOR:
        return 0.0
    if amount >= DAMAGES_CEILING:
        return 1.0
    lo, hi = math.log10(DAMAGES_FLOOR), math.log10(DAMAGES_CEILING)
    return (math.log10(amount) - lo) / (hi - lo)


def _recency(event_date: str | None, published_at: str | None) -> float:
    """Exponential decay with a ~30-day half-life."""
    stamp = event_date or published_at
    if not stamp:
        return 0.3  # unknown date: mid-low, not zero
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0.3
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    if age_days < 0:
        age_days = 0.0
    return float(0.5 ** (age_days / RECENCY_HALFLIFE_DAYS))


def _venue_quality(row: dict) -> tuple[float, str]:
    blob = " ".join(
        str(row.get(k) or "") for k in ("venue", "court", "jurisdiction")
    ).lower()
    best, matched = 0.5, "default"
    for needle, q in VENUE_QUALITY.items():
        if needle in blob and q > best:
            best, matched = q, needle
    return best, matched


def _collectability(payload: dict) -> tuple[float, list[str]]:
    """How likely is a judgment here to actually be collected?"""
    notes: list[str] = []
    score = 0.5
    if payload.get("defendant_is_public_company"):
        score = 0.9
        notes.append("public-company defendant")
    note = (payload.get("collectability_note") or "").lower()
    if any(w in note for w in ("insolven", "bankrupt", "chapter 7", "liquidat")):
        score = min(score, 0.25)
        notes.append("insolvency signal")
    if any(w in note for w in ("insured", "insurance", "bond", "escrow")):
        score = max(score, 0.8)
        notes.append("insurance/bond signal")
    return score, notes


def score_row(
    row: dict,
    *,
    weights: dict[str, float] | None = None,
    source_confidence: float = 0.8,
    event_fit: dict[str, float] | None = None,
) -> tuple[float, ScoreComponents]:
    """Score one extraction. `row` is an extraction joined to its item."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    fit = {**EVENT_FIT, **(event_fit or {})}
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except json.JSONDecodeError:
        payload = {}

    c = ScoreComponents()

    # -- thesis fit: how on-thesis is this event, and how on-thesis is the
    #    thesis? The second multiplier is what tilts the list toward
    #    settlement-stage matters at equal event strength.
    thesis = (row.get("deal_thesis") or "none").strip()
    event = (row.get("event_type") or "no_event").strip()
    c.thesis_fit = fit.get(event, 0.2) * THESIS_PRIORITY.get(thesis, 0.0)

    # -- damages, with explicit handling of the common missing case
    dmg = payload.get("damages") or {}
    amount = dmg.get("amount_usd")
    conf = (dmg.get("confidence") or row.get("damages_conf") or "none").strip()
    c.damages_confidence = conf
    if isinstance(amount, (int, float)) and amount > 0:
        c.damages = _norm_damages(float(amount)) * CONFIDENCE_FACTOR.get(conf, 0.5)
    else:
        # Do NOT impute zero -- that buries exactly the large unlabeled cases
        # this pipeline exists to surface.
        prior = THESIS_PRIORS.get(thesis, THESIS_PRIORS["none"])
        c.damages = _norm_damages(prior) * 0.45   # uncertainty discount
        c.damages_imputed = True
        c.notes.append("damages imputed from thesis prior -- no figure stated")

    c.recency = _recency(row.get("event_date"), row.get("published_at"))
    c.collectability, coll_notes = _collectability(payload)
    c.notes.extend(coll_notes)

    c.venue, venue_match = _venue_quality(row)
    if venue_match != "default":
        c.notes.append(f"venue match: {venue_match}")

    area = (row.get("practice_area") or "unknown").strip()
    area_conf = (payload.get("practice_area_confidence") or "medium").strip()
    c.practice_fit = PRACTICE_FIT.get(area, 0.25) * CONFIDENCE_FACTOR.get(area_conf, 0.75)

    c.source_confidence = source_confidence

    total = (
        w["thesis_fit"] * c.thesis_fit
        + w["damages"] * c.damages
        + w["recency"] * c.recency
        + w["collectability"] * c.collectability
        + w["venue"] * c.venue
        + w["practice_fit"] * c.practice_fit
        + w["source_confidence"] * c.source_confidence
    )
    return round(total, 6), c


@dataclass(slots=True)
class RankReport:
    scored: int = 0
    late_excluded: int = 0
    matters: int = 0
    duplicates_absorbed: int = 0
    late_exclusions: list[tuple[str, str]] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            "| stage | count |", "|---|---:|",
            f"| extractions considered | {self.scored + self.late_excluded} |",
            f"| dropped: out of scope on re-screen | {self.late_excluded} |",
            f"| scored | {self.scored} |",
            f"| duplicate documents absorbed | {self.duplicates_absorbed} |",
            f"| **distinct matters ranked** | **{self.matters}** |",
        ]
        if self.late_exclusions:
            lines += ["", "Re-screen exclusions (first 10):", ""]
            for caption, reason in self.late_exclusions[:10]:
                lines.append(f"- `{caption[:52]}` — {reason}")
        return "\n".join(lines)


def rank_all(
    db, *, weights: dict[str, float] | None = None, limit: int = 100,
    event_fit: dict[str, float] | None = None, cfg=None,
) -> RankReport:
    """Rescore every stored extraction. Offline -- no API calls, no fetches.

    Three passes, in this order and for a reason:

      1. RE-SCREEN. The exclusion patterns have changed since some rows were
         extracted, and some signals (the in rem forfeiture caption) only
         exist in the model's output rather than the source document. A row
         that fails now is marked excluded in the database, so `rank`, the
         dashboard and the digest all agree.
      2. SCORE the survivors.
      3. CLUSTER, so one matter reported by five documents is one row.
         Clustering AFTER scoring is what lets the highest-scoring document
         represent its matter.
    """
    from ..compliance.registry import get_policy
    from . import cluster as cluster_mod
    from .exclude import screen_extraction

    # litfin.toml overrides apply unless the caller passed something explicit
    # -- the server's weight sliders must beat the config file, and the config
    # file must beat the hardcoded defaults.
    if cfg is not None:
        weights = weights if weights else (cfg.weights or None)
        event_fit = event_fit if event_fit else (cfg.event_fit or None)

    report = RankReport()
    scored: list[tuple[float, str, ScoreComponents, dict]] = []

    for r in db.extractions(include_excluded=False):
        d = dict(r)

        verdict = screen_extraction(d)
        if verdict.excluded:
            db.set_extraction_excluded(d["item_uid"], verdict.reason)
            report.late_excluded += 1
            report.late_exclusions.append(
                (d.get("case_caption") or d.get("title") or d["item_uid"][:12],
                 verdict.reason)
            )
            continue

        policy = get_policy(d.get("source_id") or "")
        total, comps = score_row(
            d, weights=weights, source_confidence=policy.base_confidence,
            event_fit=event_fit,
        )
        scored.append((total, d["item_uid"], comps, d))

    scored.sort(key=lambda x: x[0], reverse=True)

    # -- cluster -----------------------------------------------------------
    by_uid = {uid: (total, comps, d) for total, uid, comps, d in scored}
    clusters = cluster_mod.build([
        {
            "item_uid": uid,
            "score": total,
            "source_id": d.get("source_id"),
            "source_url": d.get("source_url"),
            "title": d.get("title"),
            "case_caption": d.get("case_caption"),
            "event_date": d.get("event_date"),
            "docket_number": _docket_of(d),
            "has_stated_damages": not comps.damages_imputed,
        }
        for total, uid, comps, d in scored
    ])

    primaries = {c.primary.item_uid: c for c in clusters}
    member_cluster = {
        m.item_uid: c for c in clusters for m in c.members
    }

    # Rank numbers count MATTERS, not documents, so rank 7 means the seventh
    # distinct matter rather than the seventh row that happened to survive.
    rank = 0
    for total, uid, comps, _d in scored:
        c = member_cluster[uid]
        is_primary = uid in primaries
        if is_primary:
            rank += 1
        db.store_prospect(
            uid, total, comps.as_dict(),
            rank=rank if is_primary else None,
            cluster_key=c.key, cluster_size=c.size, is_primary=is_primary,
        )

    stats = cluster_mod.summarize(clusters)
    report.scored = len(scored)
    report.matters = stats["matters"]
    report.duplicates_absorbed = stats["rows_absorbed"]
    return report


def _docket_of(row: dict) -> str:
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except json.JSONDecodeError:
        return ""
    return str(payload.get("docket_number") or "")
