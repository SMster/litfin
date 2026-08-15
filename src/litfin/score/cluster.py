"""Entity resolution: one matter, however many documents reported it.

A third of the ranked list was duplicates. DOJ publishes a press release AND a
case-filing page for the same consent decree; RECAP carries four docket
entries from one week of the same bankruptcy; a multistate settlement shows up
under two state AGs. Every one of those rows is individually correct, and
seeing the same matter five times is still a worse list.

WHY THIS IS NOT DONE AT INGESTION. Two documents about one matter are two real
observations. Collapsing them in storage would lose which source saw what and
when, break the per-source health accounting, and make the decision
irreversible. Clustering here runs offline over stored extractions, costs
seconds, and changes nothing that cannot be recomputed -- the same property
that makes re-weighting cheap.

THE TRAP THAT SHAPES THE DESIGN. `case_caption` is empty on a meaningful
fraction of rows: DOJ press releases with headline titles, RECAP entries the
model could not caption. In the live corpus NINE unrelated matters shared the
empty caption -- an antitrust consent decree, an HSR annual report, a speech,
several unrelated docket entries. Clustering on a normalized key without
checking that the key is SUBSTANTIVE would have merged all nine into one row
and silently deleted eight real prospects.

So the rule is: a row only joins a cluster when it has a key worth trusting.
Everything else stands alone. Under-merging costs a duplicate row; over-merging
deletes a matter, and a deleted matter is invisible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# Corporate suffixes and citation noise that vary between documents describing
# the same case. "Acme Corp." / "Acme Corporation" / "Acme Corp., et al." are
# one defendant.
_NOISE = re.compile(
    r"\b(?:et\s+al|inc|llc|l\.l\.c|corp|corporation|company|co|ltd|limited|"
    r"lp|l\.p|llp|plc|pllc|n\.a|s\.a|gmbh|ag|nv|bv|the|and|of)\b",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^a-z0-9 ]+")
_PARENTHETICAL = re.compile(r"\([^)]*\)")

# A usable caption key needs real content. Two tokens and eight characters
# after normalization is the floor -- it admits "in re yellow" and rejects
# "", "usa", "in re".
_MIN_KEY_TOKENS = 2
_MIN_KEY_CHARS = 8

# Captions that are structurally generic: they name a procedural posture
# rather than a matter, so two rows sharing one are not necessarily the same
# case.
_GENERIC_KEYS = {
    "in re", "in the matter", "united states", "united states america",
    "usa", "unknown", "not stated", "recap document", "confidential",
}


# Government antitrust captions enumerate their plaintiffs differently in
# every document describing the same case. MEASURED on the live corpus:
#
#   "United States and 17 State Attorneys General v. Cal-Maine Foods"
#   "United States and Plaintiff States v. Cal-Maine Foods, Inc."
#
#   "United States v. OhioHealth Corporation"
#   "United States and State of Ohio v. OhioHealth Corporation"
#
# One matter, two captions, and the only difference is on the plaintiff side.
# Collapsing that enumeration to a single "us" token merges them.
#
# The "us" token is KEPT rather than dropped, deliberately: keying on the
# defendant alone would merge "Gjovik v. Apple" with "United States v. Apple",
# which are unrelated matters. The plaintiff still participates in identity --
# only its internal variation is normalized away.
_VS = re.compile(r"\bv\.?\s", re.IGNORECASE)
_US_PLAINTIFF = re.compile(
    r"^(?:the\s+)?(?:united\s+states(?:\s+of\s+america)?|usa|u\.s\.a?\.?)\b",
    re.IGNORECASE,
)


def _normalize_plaintiff_side(left: str) -> str | None:
    """'United States and 17 State Attorneys General' -> 'us'. None if not
    a government plaintiff enumeration."""
    return "us" if _US_PLAINTIFF.match(left.strip()) else None


def normalize_caption(caption: str) -> str:
    """Caption -> comparison key. Empty string means 'not usable as a key'."""
    raw = (caption or "").strip()

    # Split on the first " v. " and normalize a government plaintiff side.
    head = ""
    body = raw
    m = _VS.search(raw)
    if m:
        left, right = raw[: m.start()], raw[m.end():]
        if _normalize_plaintiff_side(left) and right.strip():
            head, body = "us v ", right

    s = body.lower()
    s = _PARENTHETICAL.sub(" ", s)      # "(as to Wallet 0x82e)", "(appeal)"
    s = s.replace("&", " and ")
    s = _PUNCT.sub(" ", s)
    s = _NOISE.sub(" ", s)
    s = " ".join(s.split())
    if not s:
        return ""
    s = head + s
    if s in _GENERIC_KEYS:
        return ""
    if len(s) < _MIN_KEY_CHARS or len(s.split()) < _MIN_KEY_TOKENS:
        return ""
    return s


# A real case number: "18-10512", "1:26-cv-01234", "26-11937-KBO". Docket
# fields in this corpus are messy free text ("BAP 26-48 (appeal); underlying
# bankruptcy docket entry Nos. 4049, 4051") so a canonical number is extracted
# rather than the string being compared whole.
_CASE_NO = re.compile(r"\b(\d{1,2}:)?(\d{2}-[a-z]{0,3}-?\d{3,6})\b", re.IGNORECASE)


_DOCKET_PARTS = re.compile(r"^(\d{2})-([a-z]{0,3})-?(\d+)$", re.IGNORECASE)


def normalize_docket(docket: str) -> str:
    """Pull a canonical case number out of a messy docket field, or ''.

    Zero-padding is stripped: courts and clerks write the same case as
    `1:26-cv-01234` and `26-cv-1234`, and comparing the strings whole would
    treat one matter as two.
    """
    m = _CASE_NO.search(docket or "")
    if not m:
        return ""
    raw = m.group(2).lower().replace("--", "-")
    parts = _DOCKET_PARTS.match(raw)
    if not parts:
        return raw
    year, kind, number = parts.groups()
    return f"{year}-{kind}-{number.lstrip('0') or '0'}"


# "et al." is the caption telling you its own defendant list is incomplete.
# MEASURED: the same DOJ matter appears as
#
#   "United States and State of California v. Taiheiyo Cement Corporation, et al."
#   "United States v. Taiheiyo Cement Corporation and CalPortland Company"
#
# and as
#
#   "United States and Plaintiff States v. Cal-Maine Foods, Inc., et al."
#   "United States and 17 State Attorneys General v. Cal-Maine Foods Inc.;
#    Hickman's Egg Ranch Inc.; Centrum Valley Holdings LLC; ..."
#
# One caption truncates the defendant list, the other enumerates it, so the
# short key is a token-prefix of the long one.
#
# Prefix-merging on its own would be UNSAFE -- "United States v. Apple" and
# "United States v. Apple and Google" could be genuinely different cases, and
# merging them deletes one. Requiring the shorter caption to carry an explicit
# "et al." is what makes it safe: the document has stated that it is not
# listing everyone.
_ET_AL = re.compile(r"\bet\s+al\b|\band\s+others\b|\bet\s+ano\b", re.IGNORECASE)


def caption_is_open_ended(caption: str) -> bool:
    """Does this caption admit that its party list is incomplete?"""
    return bool(_ET_AL.search(caption or ""))


@dataclass(slots=True)
class Member:
    item_uid: str
    score: float
    source_id: str
    source_url: str
    title: str
    caption: str
    event_date: str
    has_stated_damages: bool
    open_ended: bool = False


@dataclass(slots=True)
class Cluster:
    key: str
    members: list[Member] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def primary(self) -> Member:
        """The row that represents the matter in the ranked list.

        Highest score wins. Ties break toward the row carrying a STATED
        damages figure, then the one with an event date, then item_uid so the
        result is deterministic across runs -- a list that reshuffles between
        identical runs is one nobody can review.
        """
        return max(
            self.members,
            key=lambda m: (
                round(m.score, 6),
                m.has_stated_damages,
                bool(m.event_date),
                m.item_uid,
            ),
        )

    @property
    def others(self) -> list[Member]:
        p = self.primary
        return [m for m in self.members if m.item_uid != p.item_uid]


def cluster_key_for(caption: str, docket: str) -> str:
    """The key a row clusters on, or '' if it must stand alone.

    Caption first, docket second. That ordering is deliberate: the Zohar III
    bankruptcy and its BAP appeal carry DIFFERENT docket numbers but describe
    one matter, and for sourcing purposes they are one prospect. Docket only
    rescues rows whose caption is unusable.
    """
    key = normalize_caption(caption)
    if key:
        return f"cap:{key}"
    dk = normalize_docket(docket)
    if dk:
        return f"dkt:{dk}"
    return ""


def build(rows: Iterable[dict[str, Any]]) -> list[Cluster]:
    """Group scored rows into clusters. Unkeyed rows become singletons."""
    by_key: dict[str, Cluster] = {}
    out: list[Cluster] = []

    for r in rows:
        member = Member(
            item_uid=r["item_uid"],
            score=float(r.get("score") or 0.0),
            source_id=str(r.get("source_id") or ""),
            source_url=str(r.get("source_url") or ""),
            title=str(r.get("title") or ""),
            caption=str(r.get("case_caption") or ""),
            event_date=str(r.get("event_date") or ""),
            has_stated_damages=bool(r.get("has_stated_damages")),
            open_ended=caption_is_open_ended(str(r.get("case_caption") or "")),
        )
        key = cluster_key_for(member.caption, str(r.get("docket_number") or ""))
        if not key:
            # No trustworthy key: its own cluster, keyed on identity so it can
            # never collide with another row.
            out.append(Cluster(key=f"uid:{member.item_uid}", members=[member]))
            continue
        cluster = by_key.get(key)
        if cluster is None:
            cluster = Cluster(key=key)
            by_key[key] = cluster
            out.append(cluster)
        cluster.members.append(member)

    return _absorb_truncated(out)


def _absorb_truncated(clusters: list[Cluster]) -> list[Cluster]:
    """Fold an 'X, et al.' cluster into the one that names every defendant.

    Three guards, and each is load-bearing:

      * EVERY member of the short cluster must carry an explicit "et al." --
        without that admission, a shorter caption may simply be a narrower
        case, and merging would delete a real matter.
      * the extension must land on a TOKEN boundary, so "us v apple" does not
        absorb into "us v applebees".
      * exactly ONE candidate may extend it. Two candidates means the caption
        is ambiguous about which matter it belongs to, and the safe answer to
        an ambiguous merge is not to merge.
    """
    caption_clusters = [c for c in clusters if c.key.startswith("cap:")]
    absorbed: dict[str, Cluster] = {}

    for short in caption_clusters:
        if not short.members or not all(m.open_ended for m in short.members):
            continue
        prefix = short.key + " "
        candidates = [
            c for c in caption_clusters
            if c.key != short.key and c.key.startswith(prefix)
        ]
        if len(candidates) != 1:
            continue
        target = candidates[0]
        # Do not chain: a cluster already absorbed elsewhere cannot also be a
        # destination, or the result depends on iteration order.
        if target.key in absorbed or short.key in absorbed:
            continue
        absorbed[short.key] = target

    if not absorbed:
        return clusters

    out: list[Cluster] = []
    for c in clusters:
        target = absorbed.get(c.key)
        if target is not None:
            target.members.extend(c.members)
            continue
        out.append(c)
    return out


def summarize(clusters: Sequence[Cluster]) -> dict[str, int]:
    merged = sum(c.size - 1 for c in clusters if c.size > 1)
    return {
        "rows": sum(c.size for c in clusters),
        "matters": len(clusters),
        "clusters_with_duplicates": sum(1 for c in clusters if c.size > 1),
        "rows_absorbed": merged,
    }
