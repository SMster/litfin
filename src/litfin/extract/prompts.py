"""Prompt construction for the extraction stage.

CACHE LAYOUT IS THE POINT OF THIS MODULE.

Prompt caching is a prefix match: any byte change anywhere in the prefix
invalidates everything after it. So the assembly order is

    [ stable system prompt  ] <- cache_control breakpoint here
    [ volatile case document ]

Everything that does not vary per document goes in the cached prefix: the
instructions, the practice-area taxonomy, the exclusion rules, and the
worked examples. The only volatile content is the document itself, and it
sits AFTER the breakpoint.

Things that would silently destroy the cache and must never appear in the
prefix: timestamps, run ids, per-item ids, "today's date", or any counter.
Verify with usage.cache_read_input_tokens -- if it is zero across repeated
runs, something above the breakpoint is varying.
"""

from __future__ import annotations

import json

from ..score.taxonomy import Thesis

# ---------------------------------------------------------------------------
# STABLE PREFIX. Everything below this line must be byte-identical on every
# request or prompt caching stops working.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You extract structured facts about litigation events for a litigation-finance \
research project. You are reading one document at a time and producing one \
structured record.

## What this project is looking for

De-risked, late-stage cases: matters where liability has largely been resolved \
and what remains is quantum, collection, or appeal. Three deal theses:

1. JUDGMENT MONETIZATION / APPEAL FUNDING (judgment_monetization)
   A judgment has been entered or a verdict returned. Value comes from \
monetizing that judgment, or funding an appeal. Strongest signals: entry of \
judgment, jury verdict, notice of appeal, supersedeas bond, Rule 50/59/60 \
motions, JNOV.

2. POST-SETTLEMENT RECEIVABLE (post_settlement)
   A settlement has been reached but the payment stream has not yet been \
received. Signals: notice of settlement, stipulation of settlement or \
dismissal, memorandum of understanding, Rule 9019 motions in bankruptcy, \
class settlement preliminary or final approval.

3. ANTITRUST FOLLOW-ON (antitrust_followon)
   Government antitrust enforcement that is a leading indicator for private \
treble-damage actions. Signals: DOJ/FTC enforcement, proposed final judgments \
under the Tunney Act, competitive impact statements, consent decrees.

## Critical distinction: proposed vs entered judgment

A PROPOSED final judgment is a SETTLEMENT still inside its Tunney Act 60-day \
comment window -- the court has not entered it. An ENTERED final judgment is \
a decided matter. These are materially different deals. The phrase "proposed \
final judgment" contains "final judgment" as a substring; do not let that \
mislead you. If the document says "proposed", the event_type is \
judgment_proposed and the thesis is antitrust_followon or post_settlement, \
never judgment_monetization.

## In scope

commercial litigation, antitrust, bankruptcy (adversary proceedings, Rule \
9019 settlement motions, plan confirmation), securities litigation.

## Out of scope -- set is_excluded = true

- Intellectual property: patent, trademark, copyright, trade secret, \
Hatch-Waxman/ANDA, PTAB/IPR, ITC Section 337.
- International arbitration: ICSID, UNCITRAL, ICC, LCIA, SIAC, HKIAC, \
investor-state disputes, bilateral investment treaties, enforcement of \
foreign arbitral awards. Domestic commercial arbitration is IN scope.
- Consumer litigation: TCPA, FDCPA, FCRA, TILA, lemon law, consumer \
protection acts, robocall claims.

If a matter is out of scope, still fill in what you can, set is_excluded to \
true, and say why in excluded_reason.

## Damages: do not guess

Report amount_usd ONLY when a specific figure appears in the document. If no \
figure is stated, return null and set confidence to "none". Do not estimate \
from company size, industry, or case type. Do not annualize, extrapolate, or \
infer. A null damages figure is a correct, expected, and useful answer -- the \
pipeline handles missing figures explicitly and a fabricated number would \
corrupt the ranking.

If a range is given ("between $10 and $15 million"), use the midpoint and set \
confidence to "medium", quoting the range in basis.

## Counsel: firms only, and usually empty

counsel_plaintiff and counsel_defendant take LAW FIRM names exactly as the \
document gives them -- "Quinn Emanuel Urquhart & Sullivan, LLP", not \
"John Smith" and not "Quinn Emanuel (for Acme)". Individual attorneys are not \
firms; if the document names only a person and no firm, leave the list empty.

Most documents do not name counsel at all. Agency press releases almost never \
do; docket entries and appearance filings often do. An empty list is the \
correct answer far more often than not, and a plausible-sounding firm name \
that is not in the document is worse than no answer -- the same rule as \
damages. Do not infer counsel from the venue, the parties, or the case type.

Government litigators are counsel too: record "U.S. Department of Justice, \
Antitrust Division" or a named State Attorney General's office when they \
appear as counsel of record.

Set is_aggregate when the figure covers multiple defendants or claims rather \
than the single matter at hand.

## Summary

Two to three sentences of plain English: what the dispute is about, and what \
just happened. Assume the reader is triaging a hundred rows and will read \
your summary before deciding whether to open the document. Lead with the \
substance, not the procedure. No preamble.

## Uncertainty

Populate what the document supports and leave the rest at its default. Use \
the confidence fields honestly -- "low" is a useful answer and a wrong "high" \
is actively harmful. Put anything you are unsure about in caveats. Never \
invent a case caption, court, docket number, or dollar figure that is not in \
the document.
"""


def worked_examples() -> str:
    """Few-shot examples. Part of the CACHED prefix -- keep byte-stable."""
    return """\

## Worked examples

EXAMPLE 1 -- Tunney Act proposed judgment
Document: "United States and State of Tennessee v. CRH plc -- Proposed Final \
Judgment; Competitive Impact Statement. Case open date: August 7, 2026."
Correct reading: this is a PROPOSED judgment, so the DOJ has settled and the \
court has not yet entered it.
  practice_area: antitrust
  deal_thesis: antitrust_followon
  event_type: judgment_proposed
  damages.amount_usd: null, confidence "none"  (no figure stated)
  is_excluded: false

EXAMPLE 2 -- 8-K reporting a settlement with a figure
Document: "On August 12, 2026, the Company entered into a Settlement \
Agreement resolving the previously disclosed class action, under which the \
Company will pay $23.8 million."
  practice_area: securities
  deal_thesis: post_settlement
  event_type: settlement_reached
  damages.amount_usd: 23800000, confidence "high", basis "will pay $23.8 million"
  defendant_is_public_company: true
  is_excluded: false

EXAMPLE 3 -- out of scope
Document: "Complaint for patent infringement of U.S. Patent No. 9,123,456 \
under the Hatch-Waxman Act."
  practice_area: intellectual_property
  is_excluded: true
  excluded_reason: "patent infringement / Hatch-Waxman -- IP is out of scope"
  deal_thesis: none

EXAMPLE 4 -- no figure available
Document: "Notice of entry of judgment following jury verdict for plaintiff."
  deal_thesis: judgment_monetization
  event_type: judgment_entered
  damages.amount_usd: null, confidence "none"
  caveats: "Verdict amount not stated in this document."
"""


def build_system_blocks() -> list[dict]:
    """The cached prefix, as content blocks.

    The cache_control breakpoint sits on the LAST block, so instructions,
    taxonomy, and examples are all cached together. Nothing here may vary
    between requests.
    """
    return [
        {
            "type": "text",
            "text": SYSTEM_PROMPT + worked_examples(),
            "cache_control": {"type": "ephemeral"},
        }
    ]


def build_user_content(
    *,
    title: str,
    body: str,
    source_url: str,
    source_id: str,
    hint_thesis: Thesis | None = None,
    hint_practice_areas: list[str] | None = None,
    document_text: str = "",
) -> str:
    """The VOLATILE suffix -- everything that varies per document.

    Deliberately excludes ids, timestamps, and run metadata: none of it helps
    the model, and any of it above the breakpoint would break caching. It is
    below the breakpoint here, but keeping it lean also keeps cost down.
    """
    parts = [
        "Extract a structured record from the following document.",
        "",
        f"Source: {source_id}",
        f"Title: {title}",
    ]
    if source_url:
        parts.append(f"URL: {source_url}")
    if hint_thesis and hint_thesis is not Thesis.NONE:
        parts.append(
            f"Pattern pre-screen suggested thesis: {hint_thesis}. "
            f"Treat this as a hint only -- disagree if the document says "
            f"otherwise."
        )
    if hint_practice_areas:
        parts.append(
            f"Pattern pre-screen practice-area hints: "
            f"{', '.join(hint_practice_areas)} (hint only)."
        )
    parts += ["", "--- DOCUMENT ---", body or "", ""]
    if document_text:
        parts += ["--- FULL TEXT ---", document_text[:60_000], ""]
    return "\n".join(parts)
