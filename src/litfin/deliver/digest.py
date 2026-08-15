"""The morning email digest: top-N, with the dashboard as the destination.

The digest is a nudge, not the product. It carries enough to decide whether to
open the dashboard and nothing more -- which is why it is capped at
`deliver.top_n_email` and why each row is one line plus a summary.

Email HTML is not web HTML. No external CSS, no flexbox/grid, no <style> block
that survives Gmail reliably: everything is inline styles on tables. That is
ugly and it is correct.

The same two honesty rules as the dashboard apply, and matter MORE here
because an email gets forwarded away from its context:

  * imputed damages are labelled in the row, not just in a footnote
  * dark-venue coverage is stated in the footer of every send, so a digest
    read on a phone still says what it does not cover
"""

from __future__ import annotations

from dataclasses import dataclass

from .dataset import Dataset

_HDR = "background:#f2f4f7;font:12px -apple-system,Segoe UI,sans-serif;color:#5c6370;text-align:left;padding:6px 8px;border-bottom:1px solid #dfe3e8"
_TD = "font:13px -apple-system,Segoe UI,sans-serif;color:#16181d;padding:7px 8px;border-bottom:1px solid #eef1f4;vertical-align:top"
_MUTED = "color:#5c6370;font-size:12px"
_WARN = "color:#8a5a00"


@dataclass(slots=True)
class Digest:
    subject: str
    html: str
    text: str


def _esc(s: object) -> str:
    return (
        str(s if s is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _damages_cell(p) -> str:
    if p.damages_imputed:
        return (
            f'<span style="{_WARN}">no figure stated<br>'
            f"<span style=\"font-size:11px\">ranked on a {_esc(p.deal_thesis)} "
            f"prior</span></span>"
        )
    return _esc(p.damages_display)


def render(
    data: Dataset, *, top_n: int = 20, dashboard_url: str = ""
) -> Digest:
    """Pure: Dataset -> Digest. No I/O, no clock, no send."""
    rows = data.prospects[:top_n]
    day = data.generated_at[:10]

    if rows:
        subject = (
            f"LitFin {day}: {len(rows)} prospects, top "
            f"{rows[0].caption[:52]}"
        )
    else:
        subject = f"LitFin {day}: no ranked prospects"

    # -- warnings, identical in substance to the dashboard banners
    warnings: list[str] = []
    if data.broken_sources:
        warnings.append(
            f"{len(data.broken_sources)} source(s) NOT healthy: "
            + ", ".join(f"{s.source_id} ({s.health})" for s in data.broken_sources)
            + " — rows from these sources may be missing entirely."
        )
    if data.counts.get("awaiting_extraction"):
        warnings.append(
            f"{data.counts['awaiting_extraction']:,} collected items have not "
            f"been screened or extracted, so they cannot appear here."
        )
    if data.dark_venues or data.partial_venues:
        warnings.append(
            f"{data.dark_venues} courts publish no PACER RSS feed and "
            f"{data.partial_venues} publish orders/opinions only. In those "
            f"venues, nothing below is evidence that nothing happened."
        )
    if rows:
        imputed = sum(1 for p in rows if p.damages_imputed)
        if imputed:
            warnings.append(
                f"{imputed} of the {len(rows)} rows below have NO stated "
                f"damages figure and are ranked on a thesis prior."
            )

    # -- html ---------------------------------------------------------------
    tr = []
    for p in rows:
        docs = (
            f'<span style="{_MUTED}"> · {p.document_count} documents</span>'
            if p.document_count > 1 else ""
        )
        tr.append(
            f'<tr><td style="{_TD};text-align:right;white-space:nowrap">'
            f"{p.rank}</td>"
            f'<td style="{_TD}">'
            f'<a href="{_esc(p.source_url)}" style="color:#1a4f8a;'
            f'text-decoration:none"><b>{_esc(p.caption) or "(untitled)"}</b></a>'
            f"{docs}"
            # The same plain-English line the dashboard shows, first, so a
            # digest read on a phone does not require decoding enum tags.
            f'<div>{_esc(p.description)}</div>'
            f'<div style="{_MUTED}">{_esc(p.summary[:170])}</div></td>'
            f'<td style="{_TD};text-align:right;white-space:nowrap">'
            f"{_damages_cell(p)}</td>"
            f'<td style="{_TD};white-space:nowrap">'
            f'{_esc(p.jurisdiction_label)}<br>'
            f'<span style="{_MUTED}">{_esc((p.event_date or p.published_at)[:10])}'
            f"</span></td>"
            f'<td style="{_TD};text-align:right">{p.score:.3f}</td></tr>'
        )

    warn_html = "".join(
        f'<div style="background:#fff6e0;border:1px solid #e8c87a;'
        f'border-radius:5px;padding:8px 10px;margin:0 0 8px;'
        f'font:12.5px -apple-system,Segoe UI,sans-serif;color:#6b4a08">'
        f"{_esc(w)}</div>"
        for w in warnings
    )

    link = (
        f'<p style="font:13px -apple-system,Segoe UI,sans-serif">'
        f'<a href="{_esc(dashboard_url)}" style="color:#1a4f8a">'
        f"Open the full dashboard ({data.counts.get('ranked', 0)} ranked, "
        f"sortable and filterable) →</a></p>"
        if dashboard_url
        else ""
    )

    table = (
        f'<table cellpadding="0" cellspacing="0" width="100%" '
        f'style="border-collapse:collapse;max-width:840px">'
        f'<thead><tr><th style="{_HDR};text-align:right">#</th>'
        f'<th style="{_HDR}">Case</th>'
        f'<th style="{_HDR};text-align:right">Claim size</th>'
        f'<th style="{_HDR}">Jurisdiction / date</th>'
        f'<th style="{_HDR};text-align:right">Score</th></tr></thead>'
        f"<tbody>{''.join(tr)}</tbody></table>"
        if rows
        else f'<p style="{_MUTED}">No ranked prospects. Items collected: '
             f"{data.counts.get('items', 0):,}; extracted: "
             f"{data.counts.get('extracted', 0):,}.</p>"
    )

    html = f"""<!doctype html>
<html><body style="margin:0;padding:18px;background:#ffffff">
<h2 style="font:17px -apple-system,Segoe UI,sans-serif;margin:0 0 2px">
  LitFin — top {len(rows)} for {_esc(day)}</h2>
<div style="{_MUTED};margin-bottom:12px">
  declared purpose <b>{_esc(data.purpose)}</b> ·
  {data.counts.get('items', 0):,} items ·
  {data.counts.get('extracted', 0):,} extracted ·
  {data.counts.get('ranked', 0):,} ranked
</div>
{warn_html}
{table}
{link}
<p style="{_MUTED};margin-top:18px;border-top:1px solid #eef1f4;padding-top:10px">
  Research project. Every row links to its source document; nothing here is
  legal or investment advice. Rows marked
  <span style="{_WARN}">no figure stated</span> were never given a dollar
  amount in the source — their rank rests on a thesis prior, not a number.
</p>
</body></html>
"""

    # -- plain text ---------------------------------------------------------
    lines = [f"LitFin — top {len(rows)} for {day}", ""]
    for w in warnings:
        lines.append(f"! {w}")
    if warnings:
        lines.append("")
    for p in rows:
        dmg = "no figure stated" if p.damages_imputed else p.damages_display
        lines += [
            f"{p.rank:>3}. [{p.score:.3f}] {p.caption or '(untitled)'}",
            f"     {p.description}",
            f"     {p.jurisdiction_label} | claim size: {dmg}",
        ]
        lines += [f"     {p.source_url}", ""]
    if not rows:
        lines.append("No ranked prospects.")
    if dashboard_url:
        lines += ["", f"Full dashboard: {dashboard_url}"]

    return Digest(subject=subject, html=html, text="\n".join(lines))
