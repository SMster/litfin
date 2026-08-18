"""The morning dashboard: one self-contained HTML file.

No build step, no CDN, no network at render time. The whole dataset is
serialized into an inline <script> and the table is driven by vanilla JS. That
is not minimalism for its own sake -- it means the file keeps working when
opened from a desktop shortcut on a laptop with no internet, and it means a
dashboard from three weeks ago still renders exactly what it rendered then.

THREE HONESTY REQUIREMENTS, each of which the design says out loud:

1. The venue coverage map renders alongside the results, not on another page.
   Of ~200 federal district and bankruptcy courts, 15 publish no PACER RSS
   feed at all and 55 publish orders/opinions only. Without the map on the
   same screen, "no results in D. Nev." reads as "nothing happened in D. Nev."
   That single misreading is the difference between a sourcing tool and a
   false sense of completeness.

2. Imputed damages are marked everywhere they appear. A missing figure falls
   back to a thesis prior so large unlabeled cases are not buried, but a
   rendered prior that looks like a stated number is worse than no number.

3. The funnel counts are in the header. "0 prospects" is ambiguous between
   nothing collected, everything screened out, and extraction never run --
   three states needing three different responses. An unextracted backlog is
   a cell in that strip, because those items were collected and still cannot
   appear below.

NOTHING SITS ABOVE THE RESULTS. There is no banner strip: a row of warnings
that reads the same every morning stops informing and starts training the
reader to skip the top of the page. The facts that strip carried each moved to
the place they are checkable -- an unhealthy source into the run stamp, which
turns red, counts them and links to the source-health card; a backlog into the
funnel; partial venue coverage into the coverage card; an inferred damages
figure into its own cell and the `Not stated` band. None of the three
requirements above depends on a banner.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from ..store.db import Database
from .dataset import Dataset, load, to_json

# The design calls for IBM Plex Sans/Mono. A webfont <link> would break the
# offline guarantee and embedding two families as base64 would add a couple
# hundred KB to every dated archive, so the system stack keeps the sizes and
# the numeric columns take ui-monospace -- the part of the type system that
# carries meaning: tabular figures line up, labels stay quiet.
_CSS = """
:root {
  --bg: #ffffff; --fg: #16181d; --fg-2: #3e444e; --muted: #5c6370;
  --line: #e3e6ea; --line-soft: #edeff2; --panel: #f7f8fa;
  --accent: #1a4f8a; --warn: #8a5a00; --warn-bg: #fff6e0;
  --bad: #98221f; --bad-bg: #fdecec; --good: #14683f;
  --row-hover: #f0f4f9; --chip: #f1f3f6;
  --sans: -apple-system, "Segoe UI", Roboto, system-ui, sans-serif;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #14161a; --fg: #e6e8ec; --fg-2: #c4cad3; --muted: #99a0ab;
    --line: #2b2f36; --line-soft: #22262d; --panel: #1b1e24;
    --accent: #7fb3f0; --warn: #f0c05a; --warn-bg: rgba(240,192,90,0.07);
    --bad: #f08a86; --bad-bg: rgba(240,138,134,0.08); --good: #6ed49f;
    --row-hover: #1f242c; --chip: #262b33;
  }
}
:root[data-theme="dark"] {
  --bg: #14161a; --fg: #e6e8ec; --fg-2: #c4cad3; --muted: #99a0ab;
  --line: #2b2f36; --line-soft: #22262d; --panel: #1b1e24;
  --accent: #7fb3f0; --warn: #f0c05a; --warn-bg: rgba(240,192,90,0.07);
  --bad: #f08a86; --bad-bg: rgba(240,138,134,0.08); --good: #6ed49f;
  --row-hover: #1f242c; --chip: #262b33;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 13px/1.5 var(--sans);
}

/* -- top bar: identity, section jumps, run stamp, theme ------------------- */
.topbar { display: flex; align-items: center; gap: 32px; height: 52px;
          padding: 0 24px; background: var(--panel);
          border-bottom: 1px solid var(--line); }
.brand { display: flex; align-items: baseline; gap: 9px; }
.brand b { font-family: var(--mono); font-size: 15px; font-weight: 600;
           letter-spacing: 0.14em; }
.brand span { font-size: 11px; letter-spacing: 0.08em; color: var(--muted); }
.tabs { display: flex; gap: 22px; font-size: 13px; font-weight: 500; }
.tabs a { color: var(--muted); text-decoration: none; padding: 16px 0 15px;
          border-bottom: 2px solid transparent; }
.tabs a:hover { color: var(--fg); text-decoration: none; }
.tabs a.on { color: var(--fg); border-bottom-color: var(--accent); }
.topright { flex: 1; display: flex; justify-content: flex-end;
            align-items: center; gap: 10px; }
.runpill { display: inline-flex; align-items: center; gap: 7px;
           padding: 4px 10px; border: 1px solid var(--line);
           border-radius: 999px; font-family: var(--mono); font-size: 10.5px;
           letter-spacing: 0.06em; color: var(--muted); white-space: nowrap; }
.dot { width: 5px; height: 5px; border-radius: 50%; background: var(--good);
       display: block; flex: none; }
/* The run stamp carries the one alert that was worth the top of the page:
   a source that is down. It is a link, so the claim is checkable in one
   click rather than being a colour the reader has to interpret. */
a.runpill { text-decoration: none; }
a.runpill:hover { border-color: var(--bad); text-decoration: none; }
.runpill.down { border-color: var(--bad); color: var(--bad); }
.runpill.down .dot { background: var(--bad); }
.themetoggle { display: flex; gap: 2px; padding: 2px; border-radius: 6px;
               border: 1px solid var(--line); background: var(--bg); }
.themetoggle button { height: 24px; font-family: var(--mono);
                      font-size: 10.5px; letter-spacing: 0.04em;
                      padding: 0 9px; border: 0; border-radius: 4px;
                      background: transparent; color: var(--muted);
                      cursor: pointer; }
.themetoggle button.on { background: var(--chip); color: var(--fg); }

/* -- header: title and meta left, funnel strip right ---------------------- */
header { display: flex; align-items: flex-start; justify-content: space-between;
         gap: 40px; padding: 22px 24px 18px; }
h1 { margin: 0 0 8px; font-size: 24px; font-weight: 600; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 12px; }
.meta { display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
        font-family: var(--mono); font-size: 11.5px; color: var(--muted); }
.meta b { font-weight: 400; color: var(--fg); }
.meta .sep { color: var(--line); }
.funnel { display: flex; align-items: stretch; flex: none;
          border: 1px solid var(--line); border-radius: 8px;
          background: var(--panel); overflow: hidden; }
.stat { display: flex; flex-direction: column; gap: 5px; min-width: 104px;
        padding: 11px 18px; border-right: 1px solid var(--line); }
.stat:last-child { border-right: 0; }
.stat b { font-family: var(--mono); font-size: 21px; font-weight: 500;
          line-height: 1; font-variant-numeric: tabular-nums; }
.stat span { font-family: var(--mono); font-size: 10px; letter-spacing: 0.08em;
             text-transform: uppercase; color: var(--muted); }
.stat.quiet b { color: var(--muted); }
.stat.pending b { color: var(--warn); }
.stat.pending span { color: var(--warn); }
.stat.shown b { color: var(--accent); }

/* An unmapped claims vendor is a note inside its own card, not a page
   banner -- nothing sits above the results any more. */
.banner { display: flex; gap: 12px; padding: 10px 13px;
          border: 1px solid var(--warn); border-left-width: 3px;
          border-radius: 6px; background: var(--warn-bg); font-size: 12px;
          line-height: 1.55; color: var(--fg-2); }
.banner .kind { flex: none; min-width: 54px; padding-top: 2px;
                font-family: var(--mono); font-size: 10px;
                letter-spacing: 0.08em; color: var(--warn); }
.banner b { font-weight: 600; color: var(--fg); }
.banner code { font-family: var(--mono); font-size: 11.5px; }

/* -- controls ------------------------------------------------------------- */
.controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
            padding: 12px 24px; background: var(--panel);
            border-top: 1px solid var(--line);
            border-bottom: 1px solid var(--line); }
input, select, button {
  font: 12.5px/1.4 var(--sans); height: 30px; padding: 0 10px;
  border: 1px solid var(--line); border-radius: 5px;
  background: var(--bg); color: var(--fg-2);
}
input[type=search] { min-width: 300px; }
input[type=checkbox] { height: auto; min-width: 0; padding: 0; }
input:hover, select:hover { border-color: var(--accent); }
button { cursor: pointer; background: var(--chip); color: var(--fg-2); }
button:hover { background: var(--row-hover); border-color: var(--accent); }
button.primary { background: var(--accent); color: var(--bg);
                 border-color: var(--accent); font-weight: 600; }
a.dl { display: inline-flex; align-items: center; height: 30px; padding: 0 11px;
       border: 1px solid var(--line); border-radius: 5px; background: var(--chip);
       color: var(--fg-2); font-size: 12.5px; text-decoration: none;
       white-space: nowrap; cursor: pointer; }
a.dl:hover { background: var(--row-hover); border-color: var(--accent);
             text-decoration: none; }
label.inline { display: inline-flex; align-items: center; gap: 6px;
               color: var(--muted); font-size: 12.5px; }
.shown { margin-left: auto; display: flex; align-items: baseline; gap: 8px;
         font-family: var(--mono); font-size: 12px; color: var(--fg); }

/* -- claim-size bands ----------------------------------------------------- */
.sizebar { display: flex; flex-wrap: wrap; align-items: center; gap: 12px;
           padding: 11px 24px; border-bottom: 1px solid var(--line); }
.sizelabel { font-family: var(--mono); font-size: 10.5px; letter-spacing: .08em;
             text-transform: uppercase; color: var(--muted); }
#bands { display: flex; flex-wrap: wrap; gap: 6px; }
label.band { display: inline-flex; align-items: center; gap: 8px;
             border: 1px solid var(--line); border-radius: 999px;
             padding: 4px 11px; font-size: 12px; color: var(--fg-2);
             cursor: pointer; user-select: none; }
/* The pill IS the control, so the native box is hidden -- but only visually:
   it stays in the tab order, and the focus ring moves to the pill. */
label.band input { position: absolute; width: 1px; height: 1px; opacity: 0;
                   margin: 0; }
label.band:has(input:focus-visible) { outline: 2px solid var(--accent);
                                      outline-offset: 2px; }
label.band:hover { border-color: var(--accent); }
label.band:has(input:checked) { border-color: var(--accent);
                                background: var(--row-hover);
                                color: var(--fg); font-weight: 600; }
label.band.band-empty { opacity: .45; }
.band-n { font-family: var(--mono); font-size: 11px;
          font-variant-numeric: tabular-nums; color: var(--muted); }
.sizenote { flex: 1 1 260px; min-width: 240px; font-size: 11.5px;
            line-height: 1.5; color: var(--muted); }
.sizenote b { color: var(--fg); font-weight: 400; }

/* -- saved filters -------------------------------------------------------- */
.saved { display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
         padding: 10px 24px; border-bottom: 1px solid var(--line); }
.savedlabel { font-family: var(--mono); font-size: 10.5px; letter-spacing: .08em;
              text-transform: uppercase; color: var(--muted); }
.chip { display: inline-flex; align-items: center; gap: 8px;
        background: var(--chip); border: 1px solid var(--line);
        border-radius: 999px; padding: 3px 11px; font-size: 12px;
        color: var(--fg); cursor: pointer; }
.chip .x { color: var(--muted); }

/* -- ranked table --------------------------------------------------------- */
.wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; min-width: 1400px; }
th, td { text-align: left; padding: 12px 14px 12px 0; vertical-align: top;
         border-bottom: 1px solid var(--line-soft); }
th:first-child, td:first-child { padding-left: 24px; }
th:last-child, td:last-child { padding-right: 24px; }
th { position: sticky; top: 0; z-index: 2; height: 34px; cursor: pointer;
     padding-top: 0; padding-bottom: 0; vertical-align: middle;
     white-space: nowrap; background: var(--panel);
     border-bottom: 1px solid var(--line); font-family: var(--mono);
     font-size: 10px; font-weight: 400; letter-spacing: 0.09em;
     text-transform: uppercase; color: var(--muted); }
th:hover, th.on { color: var(--fg); }
th .arrow { opacity: 0.7; }
tbody tr.row:hover { background: var(--row-hover); cursor: pointer; }
td.num { text-align: right; white-space: nowrap; font-family: var(--mono);
         font-size: 13px; font-variant-numeric: tabular-nums; }
td.rank { text-align: left; font-family: var(--mono); font-size: 12.5px;
          color: var(--muted); }
td.score { font-weight: 500; color: var(--fg); }
td.caption { max-width: 300px; min-width: 240px; }
td.caption b { display: block; font-size: 13px; font-weight: 600;
               line-height: 1.35; color: var(--fg); text-wrap: pretty; }
td .desc { margin-top: 4px; font-size: 11.5px; line-height: 1.5;
           color: var(--fg-2); text-wrap: pretty; }
td .docs { margin-top: 4px; font-family: var(--mono); font-size: 10px;
           color: var(--muted); }
td.summary { max-width: 380px; min-width: 240px; font-size: 11.5px;
             line-height: 1.5; color: var(--fg-2); text-wrap: pretty; }
td.summary .venue { margin-top: 4px; font-family: var(--mono);
                    font-size: 10.5px; color: var(--muted); }
td.court { max-width: 200px; font-size: 11.5px; line-height: 1.5;
           color: var(--fg-2); }
td.firms { max-width: 190px; font-size: 11px; line-height: 1.45;
           color: var(--fg-2); }
.firmside { margin-bottom: 3px; }
.firmlabel { display: inline-block; min-width: 12px; font-family: var(--mono);
             font-size: 9.5px; color: var(--muted); }
td.jur, td.src { font-size: 11.5px; color: var(--fg-2); }
td.date { font-family: var(--mono); font-size: 11.5px; color: var(--fg-2); }
.stage { display: inline-flex; align-items: center; font-size: 11.5px;
         padding: 3px 9px; border-radius: 4px; background: var(--chip);
         border: 1px solid var(--line); color: var(--fg); white-space: nowrap; }
/* A stage read off an event type is a weaker claim than one read off an
   explicit posture, and now says so in text rather than only on hover. */
.stage-inferred { background: transparent; border-style: dashed;
                  border-color: var(--muted); color: var(--fg-2); opacity: .85; }
.stage .basis { color: var(--muted); }
.tag { display: inline-block; font-family: var(--mono); font-size: 10px;
       padding: 2px 7px; border-radius: 4px; background: var(--panel);
       border: 1px solid var(--line); color: var(--muted); white-space: nowrap; }
.imputed { color: var(--warn); }
.dmgband { margin-top: 3px; font-family: var(--mono); font-size: 10px;
           color: var(--muted); }
.detail td { background: var(--panel); padding: 14px 24px 18px; }
.detail dl { display: grid; grid-template-columns: 168px 1fr; gap: 0 14px;
             margin: 0; }
.detail dt { padding: 7px 0; font-size: 11.5px; color: var(--muted);
             border-bottom: 1px solid var(--line-soft); }
.detail dd { margin: 0; padding: 7px 0; font-size: 12px; line-height: 1.55;
             color: var(--fg-2); text-wrap: pretty;
             border-bottom: 1px solid var(--line-soft); }
@media (min-width: 1200px) {
  .detail dl { grid-template-columns: 168px 1fr 168px 1fr; column-gap: 40px; }
}
.detail .sm, .sm { color: var(--muted); font-size: 11.5px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* -- lower sections: siblings in one grid, coverage first ----------------- */
.panes { display: grid; grid-template-columns: 1fr 1fr; align-items: start;
         gap: 20px; padding: 20px 24px; }
.panes .col { display: flex; flex-direction: column; gap: 20px; }
section { border: 1px solid var(--line); border-radius: 8px;
          background: var(--panel); overflow: hidden; }
section > .head { padding: 14px 16px; border-bottom: 1px solid var(--line); }
h2 { margin: 0 0 4px; font-size: 14px; font-weight: 600; }
section .head p { margin: 0; font-size: 11.5px; line-height: 1.5;
                  color: var(--muted); }
section .head p b { color: var(--warn); font-weight: 400; }
section .pad { padding: 12px 16px; border-bottom: 1px solid var(--line); }
.cov { display: grid; grid-template-columns: repeat(4, 1fr); }
.cov .stat { min-width: 0; gap: 6px; padding: 13px 16px;
             border-bottom: 1px solid var(--line); }
.cov .stat b { font-size: 20px; }
.cov .stat span { font-family: var(--sans); font-size: 10.5px;
                  letter-spacing: 0; text-transform: none; line-height: 1.4; }
.cov .stat b.low { color: var(--bad); }
.cov .stat b.partial { color: var(--warn); }
.cov .stat b.high { color: var(--good); }
.mini { width: 100%; min-width: 0; font-size: 11.5px; }
.mini th { position: static; height: 30px; cursor: default; }
.mini th:first-child, .mini td:first-child { padding-left: 16px; }
.mini th:last-child, .mini td:last-child { padding-right: 16px; }
.mini td { height: 34px; padding: 0 10px 0 0; vertical-align: middle;
           color: var(--fg-2); }
.mini td.mono { font-family: var(--mono); font-size: 10.5px; color: var(--muted); }
.mini td.id { font-family: var(--mono); font-size: 11px; color: var(--fg-2); }
.mini td.num { font-size: 11px; color: var(--fg-2); }
.conf { display: inline-flex; align-items: center; gap: 6px;
        font-family: var(--mono); font-size: 10.5px; color: var(--muted); }
.conf.high { color: var(--good); }
.conf.partial { color: var(--warn); }
.conf.low { color: var(--bad); }
.conf .dot, .health .dot { background: currentColor; }
.health { display: inline-flex; align-items: center; gap: 6px;
          font-family: var(--mono); font-size: 10.5px; color: var(--good); }
.health.off { color: var(--muted); }
.health.broken { color: var(--bad); }
.tos { font-size: 11px; color: var(--muted); }
.tos.ok { color: var(--good); }
.tos.no { color: var(--bad); }
.tos.unverified { color: var(--warn); }
.scrollbox { max-height: 340px; overflow-y: auto; }
section input[type=search] { width: 100%; min-width: 0; }
details > summary { cursor: pointer; color: var(--accent); font-size: 11.5px; }
.empty { padding: 40px 24px; color: var(--muted); }
footer { display: flex; align-items: flex-start; justify-content: space-between;
         gap: 40px; padding: 18px 24px 34px; border-top: 1px solid var(--line);
         background: var(--panel); }
footer .note { max-width: 820px; font-size: 11.5px; line-height: 1.6;
               color: var(--muted); }
footer .build { font-family: var(--mono); font-size: 10.5px; color: var(--muted);
                opacity: .7; white-space: nowrap; }
.hide { display: none !important; }

/* Under 1100px the three widest secondary columns fold into the expanded
   row, which already carries all three; the wrapper keeps its scroll. */
@media (max-width: 1100px) {
  table { min-width: 900px; }
  th:nth-child(7), td:nth-child(7),
  th:nth-child(9), td:nth-child(9),
  th:nth-child(11), td:nth-child(11) { display: none; }
  .panes { grid-template-columns: 1fr; }
  header { flex-direction: column; gap: 18px; }
}
@media print {
  .topbar, .controls, .sizebar, .saved { display: none; }
  .wrap { overflow: visible; }
  table { min-width: 0; }
}
"""

# The table's behaviour lives here rather than in a framework: sorting,
# filtering, saved filters (localStorage), and row expansion are a few dozen
# lines each, and the whole point of the file is that it has no dependencies.
_JS = r"""
const $ = (s) => document.querySelector(s);
const fmtUSD = (n) => n == null ? '' : '$' + Math.round(n).toLocaleString('en-US');
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;');

let sortKey = 'score', sortDir = -1, expanded = new Set();

const COLUMNS = [
  {key:'rank',    label:'#',        cls:'rank',    get:p=>p.rank},
  {key:'score',   label:'Score',    cls:'num score', get:p=>p.score},
  {key:'caption', label:'Case',     cls:'caption', get:p=>p.caption.toLowerCase()},
  {key:'summary', label:'Summary',  cls:'summary', get:p=>(p.summary||'').toLowerCase()},
  // Sorted by life-cycle position, not alphabetically: a lexical sort puts
  // "Trial" before "Verdict returned" and "Closed" first, which tells you
  // nothing about how far along a matter is.
  {key:'stage',   label:'Stage',    cls:'',        get:p=>p.stage_rank},
  {key:'court',   label:'Court',    cls:'',        get:p=>(p.court_display||'').toLowerCase()},
  {key:'counsel', label:'Law firms', cls:'firms',
   get:p=>((p.counsel_p||[])[0] || (p.counsel_d||[])[0] || '￿').toLowerCase()},
  {key:'damages', label:'Claim size', cls:'num',   get:p=>p.damages || 0},
  {key:'jlabel',  label:'Juris.',   cls:'jur',     get:p=>(p.jlabel||'').toLowerCase()},
  {key:'event_date',   label:'Event', cls:'date',  get:p=>p.event_date || p.published_at || ''},
  {key:'source_id',    label:'Source', cls:'src',  get:p=>p.source_id},
];

function readFilters() {
  return {
    q:      $('#q').value.trim().toLowerCase(),
    thesis: $('#f-thesis').value,
    area:   $('#f-area').value,
    event:  $('#f-event').value,
    jgroup: $('#f-jgroup').value,
    juris:  $('#f-juris').value,
    stage:  $('#f-stage').value,
    firms:  $('#f-firms').checked,
    bands:  [...document.querySelectorAll('#bands input:checked')]
              .map(i => i.value),
  };
}

function applyFilters(f) {
  $('#q').value = f.q || '';
  $('#f-thesis').value = f.thesis || '';
  $('#f-area').value = f.area || '';
  $('#f-event').value = f.event || '';
  $('#f-jgroup').value = f.jgroup || '';
  syncJurisdictionOptions();
  $('#f-juris').value = f.juris || '';
  $('#f-stage').value = f.stage || '';
  $('#f-firms').checked = !!f.firms;
  const want = f.bands && f.bands.length ? new Set(f.bands) : null;
  document.querySelectorAll('#bands input').forEach(i => {
    i.checked = want ? want.has(i.value) : false;
  });
  render();
}

// A row passes the size filter if its BAND is checked. Nothing is checked =
// no size filter at all, which is different from every band being checked
// only in that it stays true as new bands appear.
function matchesSize(p, bands) {
  return bands.length === 0 || bands.includes(p.band);
}

function matches(p, f) {
  if (f.thesis && p.thesis !== f.thesis) return false;
  if (f.area && p.practice_area !== f.area) return false;
  if (f.event && p.event !== f.event) return false;
  if (f.jgroup && (p.jgroup || '') !== f.jgroup) return false;
  if (f.juris && (p.jlabel || '') !== f.juris) return false;
  if (f.stage && p.stage !== f.stage) return false;
  if (f.firms && !((p.counsel_p||[]).length || (p.counsel_d||[]).length))
    return false;
  if (!matchesSize(p, f.bands)) return false;
  if (f.q) {
    const hay = [p.caption, p.summary, p.description, p.court, p.venue,
                 p.court_display, p.stage, p.docket,
                 (p.plaintiffs||[]).join(' '), (p.defendants||[]).join(' '),
                 (p.counsel_p||[]).join(' '), (p.counsel_d||[]).join(' '),
                 p.posture, p.jlabel, p.source_id].join(' ').toLowerCase();
    if (!hay.includes(f.q)) return false;
  }
  return true;
}

// The jurisdiction dropdown lists only labels within the selected group, so
// picking "State" does not leave you scrolling past 30 federal districts.
function syncJurisdictionOptions() {
  const group = $('#f-jgroup').value;
  const prev = $('#f-juris').value;
  const labels = [...new Set(DATA.prospects
    .filter(p => !group || p.jgroup === group)
    .map(p => p.jlabel).filter(Boolean))].sort();
  $('#f-juris').innerHTML = '<option value="">any jurisdiction</option>'
    + labels.map(v => '<option>' + esc(v) + '</option>').join('');
  if (labels.includes(prev)) $('#f-juris').value = prev;
}

// Counts shown against each band are computed with every OTHER filter
// applied but the size filter itself ignored -- so the numbers tell you what
// checking that box would actually give you, rather than what it gives you
// given that it is already unchecked.
function bandCounts(f) {
  const counts = {};
  DATA.prospects.forEach(p => {
    const probe = Object.assign({}, f, {bands: []});
    if (matches(p, probe)) counts[p.band] = (counts[p.band] || 0) + 1;
  });
  return counts;
}

function detailHTML(p) {
  const rows = [];
  const add = (k, v) => { if (v) rows.push('<dt>' + esc(k) + '</dt><dd>' + v + '</dd>'); };
  add('In short', esc(p.description));
  add('Summary', esc(p.summary));
  // The tags left the table when the description column arrived; they belong
  // here, where someone who wants the exact classification can find it.
  add('Classification',
      '<span class="tag">' + esc(p.thesis) + '</span> '
    + '<span class="tag">' + esc(p.event) + '</span> '
    + '<span class="tag">' + esc(p.practice_area) + '</span> '
    + '<span class="tag">' + esc(p.jlabel) + '</span>');
  add('Stage', esc(p.stage) + ' <span class="tag">'
      + esc(p.stage_basis === 'posture' ? 'from procedural posture'
                                        : 'inferred from ' + p.stage_basis)
      + '</span>');
  add('Procedural posture', esc(p.posture));
  add('Appeal status', esc(p.appeal));
  add('Plaintiff counsel', (p.counsel_p || []).length
      ? esc(p.counsel_p.join('; ')) : '');
  add('Defendant counsel', (p.counsel_d || []).length
      ? esc(p.counsel_d.join('; ')) : '');
  add('Docket number', esc(p.docket));
  add('Court', esc([p.court, p.venue].filter(Boolean).join(' — ')));
  add('Plaintiffs', esc((p.plaintiffs||[]).join('; ')));
  add('Defendants', esc((p.defendants||[]).join('; ')) +
      (p.public_defendant ? ' <span class="tag">public company'
        + (p.ticker ? ' · ' + esc(p.ticker) : '') + '</span>' : ''));
  add('Collectability', esc(p.collectability));
  if (p.imputed) {
    add('Damages', '<span class="imputed">no figure stated — ranked on a '
      + esc(p.thesis) + ' prior with an uncertainty discount. This row\'s '
      + 'position does NOT rest on a real number.</span>');
  } else {
    add('Damages', esc(p.damages_display) + ' <span class="tag">confidence: '
      + esc(p.damages_conf) + '</span>'
      + (p.damages_basis ? '<div class="sm">“' + esc(p.damages_basis) + '”</div>' : ''));
  }
  add('Model caveats', esc(p.caveats));
  add('Extraction confidence', esc(p.confidence));
  const c = p.components || {};
  const comp = ['thesis_fit','damages','recency','collectability','venue',
                'practice_fit','source_confidence']
    .filter(k => c[k] !== undefined)
    .map(k => '<span class="tag">' + k + ' ' + Number(c[k]).toFixed(2) + '</span>')
    .join(' ');
  add('Score components', comp);
  if ((c.notes || []).length) add('Notes', esc(c.notes.join(' · ')));
  add('Source', '<a href="' + esc(p.url) + '" target="_blank" rel="noopener">'
      + esc(p.url) + '</a> <span class="tag">' + esc(p.source_id) + '</span>');
  // Nothing was deleted to make the list shorter -- the other documents that
  // reported this matter are listed here with their own links.
  if ((p.duplicates || []).length) {
    add('Also reported by',
      '<div class="sm">' + p.duplicates.length + ' other document'
      + (p.duplicates.length > 1 ? 's' : '') + ' describing the same matter, '
      + 'ranked lower and folded into this row.</div>'
      + p.duplicates.map(d =>
          '<div class="sm">· <a href="' + esc(d.url) + '" target="_blank" '
          + 'rel="noopener">' + (esc(d.title) || esc(d.url)) + '</a> '
          + '<span class="tag">' + esc(d.source_id) + '</span>'
          + (d.event_date ? ' ' + esc(d.event_date) : '') + '</div>').join(''));
  }
  add('Item uid', '<span class="sm">' + esc(p.uid) + '</span>');
  return '<tr class="detail"><td colspan="' + COLUMNS.length
       + '"><dl>' + rows.join('') + '</dl></td></tr>';
}

function rowHTML(p) {
  // Claim size shows the BAND, not a bare number, and an imputed figure is
  // never rendered as a dollar amount in this column -- the amount is only
  // ever shown when the source actually stated one. The band sits under the
  // figure so a stated amount and its bucket read as one unit.
  const dmg = p.imputed || !p.damages
    ? '<span class="imputed">not stated</span>'
      + '<div class="dmgband">imputed prior</div>'
    : fmtUSD(p.damages) + '<div class="dmgband">' + esc(p.band) + '</div>';
  const docs = p.docs > 1
    ? '<div class="docs">' + p.docs + ' documents · deduped</div>' : '';
  return '<tr class="row" data-uid="' + esc(p.uid) + '">'
    + '<td class="rank">' + p.rank + '</td>'
    + '<td class="num score">' + p.score.toFixed(3) + '</td>'
    + '<td class="caption"><b>' + esc(p.caption) + '</b>'
      + '<div class="desc">' + esc(p.description) + '</div>' + docs
    + '</td>'
    + '<td class="summary">' + summaryCell(p) + '</td>'
    + '<td>' + stageCell(p) + '</td>'
    + '<td class="court">' + (esc(p.court_display)
        || '<span class="sm">not stated</span>') + '</td>'
    + '<td class="firms">' + firmsCell(p) + '</td>'
    + '<td class="num">' + dmg + '</td>'
    + '<td class="jur">' + esc(p.jlabel) + '</td>'
    + '<td class="date">' + esc(p.event_date || p.published_at || '').slice(0,10) + '</td>'
    + '<td class="src"><span class="tag">' + esc(p.source_id) + '</span></td>'
    + '</tr>'
    + (expanded.has(p.uid) ? detailHTML(p) : '');
}

// The model's own summary, with the venue under it. The venue is the short
// form (S.D.N.Y.); the Court column carries the full name.
function summaryCell(p) {
  const text = p.summary
    ? esc(p.summary.length > 260 ? p.summary.slice(0,260) + '…' : p.summary)
    : '<span class="sm">no summary extracted</span>';
  const venue = p.venue && p.venue !== p.court_display
    ? '<div class="sm venue">' + esc(p.venue) + '</div>' : '';
  return text + venue;
}

// A stage read off an explicit procedural posture is a stronger claim than
// one inferred from the event type. The weaker one carries a dashed border
// AND the word "inferred": a border alone is a colour-like signal that has to
// be learned, and hover text does not exist in print or on a phone.
function stageCell(p) {
  const inferred = p.stage_basis !== 'posture';
  return '<span class="stage' + (inferred ? ' stage-inferred' : '') + '"'
    + ' title="' + esc(inferred
        ? 'inferred from ' + p.stage_basis + ' — no explicit procedural posture'
        : 'from the procedural posture') + '">'
    + esc(p.stage)
    + (inferred ? ' <span class="basis">· inferred</span>' : '')
    + '</span>';
}

// Counsel is empty far more often than not — agency press releases almost
// never name it. A blank must read as "not stated", and a row extracted
// before counsel capture existed must say THAT instead, because only one of
// the two is fixable by re-extracting.
function firmsCell(p) {
  const pl = p.counsel_p || [], df = p.counsel_d || [];
  if (!pl.length && !df.length) {
    return p.counsel_known
      ? '<span class="sm">not named</span>'
      : '<span class="sm imputed" title="extracted before counsel capture '
        + 'existed — run: litfin extract --refresh">not captured</span>';
  }
  const side = (label, firms) => firms.length
    ? '<div class="firmside"><span class="firmlabel">' + label + '</span> '
      + esc(firms.join('; ')) + '</div>'
    : '';
  return side('P', pl) + side('D', df);
}

function renderBands(f) {
  const counts = bandCounts(f);
  $('#bands').innerHTML = DATA.band_order.map(b => {
    const n = counts[b] || 0;
    const checked = f.bands.includes(b) ? ' checked' : '';
    const off = n === 0 ? ' band-empty' : '';
    return '<label class="band' + off + '"><input type="checkbox" value="'
      + esc(b) + '"' + checked + '> ' + esc(b)
      + '<span class="band-n">' + n + '</span></label>';
  }).join('');
}

function render() {
  const f = readFilters();
  renderBands(f);
  const col = COLUMNS.find(c => c.key === sortKey) || COLUMNS[1];
  const rows = DATA.prospects.filter(p => matches(p, f)).sort((a, b) => {
    const x = col.get(a), y = col.get(b);
    if (x < y) return -sortDir;
    if (x > y) return sortDir;
    return a.rank - b.rank;
  });
  $('#tbody').innerHTML = rows.map(rowHTML).join('')
    || '<tr><td colspan="' + COLUMNS.length + '" class="empty">'
       + 'No rows match these filters.</td></tr>';
  $('#shown').textContent = rows.length + ' of ' + DATA.prospects.length
    + ' shown';
  const imp = rows.filter(p => p.imputed).length;
  $('#shown-imputed').textContent = imp ? '· ' + imp + ' imputed' : '';
  document.querySelectorAll('th[data-key]').forEach(th => {
    const on = th.dataset.key === sortKey;
    th.classList.toggle('on', on);
    const a = th.querySelector('.arrow');
    if (a) a.textContent = on ? (sortDir < 0 ? '▼' : '▲') : '';
  });
}

// The page follows the OS by default. An explicit choice is remembered,
// because "the dashboard I read every morning" is a different context from
// "whatever this laptop is set to" -- and it must survive a re-render, which
// only [data-theme] on the root does.
const THEME = 'litfin.theme.v1';

function setTheme(mode) {
  const root = document.documentElement;
  if (mode) root.setAttribute('data-theme', mode);
  else root.removeAttribute('data-theme');
  document.querySelectorAll('.themetoggle button').forEach(b => {
    b.classList.toggle('on', b.dataset.theme === mode);
  });
}

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME); } catch (e) {}
  setTheme(saved === 'dark' || saved === 'light' ? saved : null);
  document.querySelector('.themetoggle').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-theme]');
    if (!b) return;
    // Clicking the active side hands control back to the OS.
    const next = document.documentElement.getAttribute('data-theme')
                 === b.dataset.theme ? null : b.dataset.theme;
    setTheme(next);
    try {
      if (next) localStorage.setItem(THEME, next);
      else localStorage.removeItem(THEME);
    } catch (e) {}
  });
}

function toggleRow(uid) {
  if (expanded.has(uid)) expanded.delete(uid); else expanded.add(uid);
  render();
}

// -- saved filters ---------------------------------------------------------
const STORE = 'litfin.savedFilters.v1';
const loadSaved = () => { try { return JSON.parse(localStorage.getItem(STORE)) || []; }
                          catch (e) { return []; } };
const putSaved = (v) => { try { localStorage.setItem(STORE, JSON.stringify(v)); }
                          catch (e) {} };

function renderSaved() {
  const saved = loadSaved();
  $('#saved-list').innerHTML = saved.map((s, i) =>
    '<span class="chip" data-i="' + i + '">' + esc(s.name)
    + '<span class="x" data-del="' + i + '">×</span></span>').join('')
    || '<span class="sm">No saved filters yet — set some filters and '
       + 'press Save filter.</span>';
}

function init() {
  // The theme control and the saved list work on any page. Everything after
  // the guard addresses the table, which an empty dashboard does not render
  // -- and a null #thead used to take the whole script down with it.
  initTheme();
  renderSaved();
  const covq = $('#covq');
  if (covq) covq.addEventListener('input', (e) => {
    const q = e.target.value.trim().toLowerCase();
    document.querySelectorAll('#covbody tr').forEach(tr => {
      tr.classList.toggle('hide', q && !tr.textContent.toLowerCase().includes(q));
    });
  });

  if (!document.getElementById('tbody')) return;

  const uniq = (k) => [...new Set(DATA.prospects.map(p => p[k]).filter(Boolean))].sort();
  const fill = (sel, vals, label) => {
    $(sel).innerHTML = '<option value="">' + label + '</option>'
      + vals.map(v => '<option>' + esc(v) + '</option>').join('');
  };
  fill('#f-thesis', uniq('thesis'), 'any thesis');
  fill('#f-area',   uniq('practice_area'), 'any practice area');
  fill('#f-event',  uniq('event'), 'any event');
  fill('#f-jgroup', uniq('jgroup'), 'federal or state');
  // Stage options follow the life cycle, not the alphabet, and only list
  // stages that actually occur in this dataset.
  const present = new Set(DATA.prospects.map(p => p.stage));
  $('#f-stage').innerHTML = '<option value="">any stage</option>'
    + DATA.stage_order.filter(s => present.has(s))
        .map(s => '<option>' + esc(s) + '</option>').join('');
  syncJurisdictionOptions();

  $('#thead').innerHTML = '<tr>' + COLUMNS.map(c =>
    '<th data-key="' + c.key + '">' + esc(c.label) + ' <span class="arrow"></span></th>'
  ).join('') + '</tr>';

  $('#thead').addEventListener('click', (e) => {
    const th = e.target.closest('th[data-key]');
    if (!th) return;
    if (sortKey === th.dataset.key) sortDir = -sortDir;
    else { sortKey = th.dataset.key; sortDir = (sortKey === 'caption' || sortKey === 'rank') ? 1 : -1; }
    render();
  });

  $('#tbody').addEventListener('click', (e) => {
    const tr = e.target.closest('tr.row');
    if (tr && !e.target.closest('a')) toggleRow(tr.dataset.uid);
  });

  ['#q','#f-thesis','#f-area','#f-event','#f-juris','#f-stage','#f-firms']
    .forEach(s => $(s).addEventListener('input', render));
  $('#f-jgroup').addEventListener('input', () => {
    syncJurisdictionOptions();
    render();
  });
  $('#bands').addEventListener('change', render);

  $('#reset').addEventListener('click', () => applyFilters({}));
  $('#expand').addEventListener('click', () => {
    if (expanded.size) expanded.clear();
    else DATA.prospects.forEach(p => expanded.add(p.uid));
    render();
  });
  $('#save').addEventListener('click', () => {
    const name = prompt('Name this filter:');
    if (!name) return;
    const saved = loadSaved();
    saved.push({name, f: readFilters()});
    putSaved(saved); renderSaved();
  });
  $('#saved').addEventListener('click', (e) => {
    const del = e.target.dataset.del;
    if (del !== undefined) {
      const saved = loadSaved(); saved.splice(+del, 1); putSaved(saved); renderSaved();
      return;
    }
    const chip = e.target.closest('.chip');
    if (chip) applyFilters(loadSaved()[+chip.dataset.i].f);
  });
  render();
}
document.addEventListener('DOMContentLoaded', init);
"""


def _esc(s: object) -> str:
    return (
        str(s if s is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _coverage_section(data: Dataset) -> str:
    labels = {
        "high": "full PACER RSS feed",
        "partial": "orders/opinions only",
        "low": "NO feed — absence of signal is not absence of activity",
        "not_applicable": "not a PACER court",
    }
    if not data.courts:
        return """<section id="coverage">
  <div class="head">
    <h2>Venue coverage</h2>
    <p>No coverage map yet. Build it with <code>litfin coverage --refresh</code>.
       Until then, an empty venue cannot be distinguished from an unmonitored
       one.</p>
  </div>
</section>"""

    stats = "".join(
        f'<div class="stat"><b class="{_esc(k)}">{v}</b>'
        f"<span>{_esc(labels.get(k, k))}</span></div>"
        for k, v in sorted(
            data.coverage_summary.items(), key=lambda kv: -kv[1]
        )
    )
    rows = "".join(
        f'<tr><td><span class="conf {_esc(c.confidence)}">'
        f'<span class="dot"></span>{_esc(c.confidence)}</span></td>'
        f'<td class="id">{_esc(c.court_id)}</td>'
        f"<td>{_esc(c.full_name)}</td>"
        f'<td class="mono">{_esc(c.entry_types)}</td></tr>'
        for c in data.courts
    )
    return f"""<section id="coverage">
  <div class="head">
    <h2>Venue coverage — how much to trust an empty result</h2>
    <p>On the same page as the results on purpose. A venue with no feed
       produces no rows whether or not anything happened in it.</p>
  </div>
  <div class="cov">{stats}</div>
  <div class="pad">
    <input type="search" id="covq" placeholder="filter {len(data.courts)} courts…">
  </div>
  <div class="wrap scrollbox">
    <table class="mini">
      <thead><tr><th>confidence</th><th>id</th><th>court</th>
                 <th>entry types</th></tr></thead>
      <tbody id="covbody">{rows}</tbody>
    </table>
  </div>
</section>"""


def _claims_section(data: Dataset) -> str:
    """The chapter 11 census. Separate from prospects, and labelled as such.

    These rows are a census, not deal signals — they carry no outcome
    language and are deliberately kept out of extraction. Mixing them into
    the ranked table would imply an event where there is none.
    """
    if not data.claims:
        return ""

    chips = " ".join(
        f'<span class="tag">{_esc(v)} · {n}</span>'
        for v, n in data.claims_by_vendor
    )
    unmapped = ""
    if data.claims_unmapped:
        rows = "; ".join(
            f"{_esc(a)} [{_esc(c)}] ×{n}" for a, c, n in data.claims_unmapped
        )
        unmapped = (
            f'<div class="pad"><div class="banner warn" style="margin:0">'
            f'<span class="kind">ACTION</span><span>'
            f"<b>{len(data.claims_unmapped)} unmapped claims agent(s):</b> "
            f"{rows}. The rows are kept — an unrecognized vendor is a new "
            f"entrant or a rename, both worth knowing. Add an alias to "
            f"<code>connectors/claims/agents.toml</code>.</span></div></div>"
        )

    rows = "".join(
        f'<tr><td class="mono">{_esc(c.court)}</td>'
        f'<td class="id">{_esc(c.case_number)}</td>'
        f"<td>{_esc(c.debtor)}</td>"
        f"<td>{'<a href=' + chr(34) + _esc(c.agent_case_url) + chr(34) + ' target=_blank rel=noopener>' + _esc(c.vendor_id) + '</a>' if c.agent_case_url else _esc(c.vendor_id)}</td>"
        f'<td class="mono">{_esc(c.date_filed)}</td></tr>'
        for c in data.claims
    )
    return f"""<section id="claims">
  <div class="head">
    <h2>Chapter 11 claims-agent census — {len(data.claims)} cases</h2>
    <p>Court-published assignment lists (S.D. Ohio, S.D.N.Y., D.N.J.). These
       are census records, not deal events: they carry no outcome language and
       cost zero extraction budget by design. <b>D. Del. is absent</b> — the
       most valuable of the four — because its assignment list sits on a host
       whose robots.txt disallows it.</p>
  </div>
  <div class="pad">{chips}</div>
  {unmapped}
  <div class="wrap scrollbox">
    <table class="mini">
      <thead><tr><th>court</th><th>case</th><th>debtor</th>
                 <th>agent</th><th>filed</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</section>"""


# A ToS status is three-valued and each value means something different to
# whoever reads the row: cleared = we may fetch it, prohibited = we must not
# and the connector is off, anything else = nobody has checked yet.
_TOS_CLASS = {
    "cleared": "ok", "public_domain_gov": "ok", "permitted": "ok",
    "prohibited": "no", "disallowed": "no", "blocked": "no",
}


def _sources_section(data: Dataset) -> str:
    def health_cls(h: str) -> str:
        if h == "HEALTHY":
            return ""
        return "off" if h in ("DISABLED", "unknown") else "broken"

    rows = "".join(
        f'<tr><td class="id">{_esc(s.source_id)}</td>'
        f'<td class="mono">{_esc(s.tier)}</td>'
        f'<td><span class="tos {_TOS_CLASS.get(s.status, "unverified")}">'
        f"{_esc(s.status)}</span></td>"
        f'<td><span class="health {health_cls(s.health)}">'
        f'<span class="dot"></span>{_esc(s.health)}</span></td>'
        f'<td class="num">{s.items:,}</td>'
        f'<td class="mono">{_esc(s.last_success_at[:19])}</td>'
        f'<td class="mono">{_esc(s.health_note[:90])}</td></tr>'
        for s in data.sources
    )
    return f"""<section id="sources">
  <div class="head">
    <h2>Source health</h2>
    <p>A BROKEN source does not advance its watermark, so data it failed to
       read is re-read once the parser is fixed — but nothing above will show
       it until then.</p>
  </div>
  <div class="wrap scrollbox">
    <table class="mini">
      <thead><tr><th>source</th><th>tier</th><th>ToS status</th><th>health</th>
                 <th>items</th><th>last success</th><th>note</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</section>"""


def render(
    data: Dataset, *, panel_html: str = "", panel_js: str = "",
    export_href: str = "",
) -> str:
    """Pure: Dataset -> a complete HTML document. No I/O, no clock.

    `panel_html`/`panel_js` let the local server graft a control panel onto the
    same page. The static file passes neither, so what you open from a desktop
    shortcut has no buttons that would do nothing.

    `export_href` is the same principle in reverse: the published bundle ships
    an .xlsx next to index.html, and without a link nothing on the page can
    reach it. Only set it when the file is genuinely there -- a download button
    that 404s is worse than no button.
    """
    c = data.counts
    # One row of the strip per funnel stage, in pipeline order. "Screened out"
    # is muted and "shown here" takes the accent: the first is a number you
    # want to be large, the last is the one the page is actually about.
    funnel = [
        ("items collected", c.get("items", 0), ""),
        ("screened out", c.get("screened_out", 0), " quiet"),
        ("extracted", c.get("extracted", 0), ""),
        ("ranked", c.get("ranked", 0), ""),
        ("shown here", len(data.prospects), " shown"),
    ]
    # A backlog is a funnel stage, not an aside: these items were collected
    # and cannot appear below until `litfin screen` and `litfin extract` run.
    # The cell only exists when there is one, so a clean run reads clean.
    pending = c.get("awaiting_extraction", 0)
    if pending:
        funnel.insert(2, ("awaiting extraction", pending, " pending"))
    stats = "".join(
        f'<div class="stat{cls}"><b>{v:,}</b><span>{_esc(k)}</span></div>'
        for k, v, cls in funnel
    )

    body = (
        '<div class="empty" id="results">No ranked prospects yet. The funnel '
        "above shows where the pipeline stopped: collect with "
        "<code>litfin run</code>, screen for free with "
        "<code>litfin screen</code>, extract with <code>litfin extract</code>, "
        "then <code>litfin rank</code>.</div>"
        if not data.prospects
        else """<div class="wrap" id="results">
  <table><thead id="thead"></thead><tbody id="tbody"></tbody></table>
</div>"""
    )

    export_link = (
        f'<a href="{_esc(export_href)}" class="dl" download>↓ Excel</a>'
        if export_href else ""
    )

    # Section jumps only for sections that exist: the census disappears when
    # no claims agent list has been collected, and a tab to nowhere is worse
    # than no tab.
    claims_html = _claims_section(data)
    tabs = [("#results", "Ranked table"), ("#coverage", "Venue coverage")]
    if claims_html:
        tabs.append(("#claims", "Ch. 11 census"))
    tabs.append(("#sources", "Source health"))
    nav = "".join(
        f'<a href="{href}" class="{cls}">{label}</a>'
        for href, label, cls in (
            (h, la, "on" if i == 0 else "")
            for i, (h, la) in enumerate(tabs)
        )
    )
    # Nothing sits above the results. The alert that used to lead the page
    # rides on the run stamp instead: an unhealthy source turns it red, names
    # the count, and links to the card that says which one and why. The
    # standing caveats live where they apply — the coverage card, the imputed
    # cells, and the `Not stated` band — which is where they were checkable
    # anyway.
    down = len(data.broken_sources)
    stamp = f"RUN {_esc(data.generated_at[:16])}Z"
    run_pill = (
        f'<a class="runpill down" href="#sources"><span class="dot"></span>'
        f'{stamp} · {down} SOURCE{"S" if down > 1 else ""} DOWN</a>'
        if down else
        f'<span class="runpill"><span class="dot"></span>{stamp}</span>'
    )

    payload = json.dumps(to_json(data), separators=(",", ":"), sort_keys=True)
    # </script> inside a JSON string would close the tag early. Only the
    # solidus needs escaping; < keeps the JSON valid either way.
    payload = payload.replace("</", "<\\/")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LitFin — {len(data.prospects)} prospects — {_esc(data.generated_at[:10])}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="topbar">
  <div class="brand"><b>LITFIN</b><span>PROSPECTS</span></div>
  <nav class="tabs">{nav}</nav>
  <div class="topright">
    {run_pill}
    <div class="themetoggle">
      <button type="button" data-theme="dark">DARK</button>
      <button type="button" data-theme="light">LIGHT</button>
    </div>
  </div>
</div>

<header>
  <div>
    <h1>Litigation-finance prospects</h1>
    <div class="meta">
      <span>Generated {_esc(data.generated_at)}</span><span class="sep">·</span>
      <span>declared purpose <b>{_esc(data.purpose)}</b></span><span class="sep">·</span>
      <span>{_esc(data.data_root)}</span>
      {'<span class="sep">·</span><span>run <b>' + _esc(data.last_run_id) + "</b></span>" if data.last_run_id else ""}
    </div>
  </div>
  <div class="funnel">{stats}</div>
</header>

{panel_html}

<div class="controls">
  <input type="search" id="q" placeholder="⌕  search case, description, parties…">
  <select id="f-stage" title="case stage"></select>
  <select id="f-jgroup" title="federal or state"></select>
  <select id="f-juris" title="specific jurisdiction"></select>
  <select id="f-thesis"></select>
  <select id="f-area"></select>
  <select id="f-event"></select>
  <label class="inline"><input type="checkbox" id="f-firms">
     counsel named</label>
  <button id="reset">Reset</button>
  <button id="expand">Expand all</button>
  <button id="save" class="primary">Save filter</button>
  {export_link}
  <span class="shown">
    <span id="shown"></span> <span id="shown-imputed" class="imputed"></span>
  </span>
</div>

<div class="sizebar">
  <span class="sizelabel">Claim size</span>
  <div id="bands"></div>
  <span class="sub sizenote">
    Counts show what each band would return under the other filters.
    <b>Not stated</b> is a band, not a hidden exclusion — a figure inferred
    from a thesis prior never counts toward a dollar range.
  </span>
</div>
<div class="saved" id="saved">
  <span class="savedlabel">Saved</span>
  <span id="saved-list"></span>
</div>

{body}

<div class="panes">
  {_coverage_section(data)}
  <div class="col">
    {claims_html}
    {_sources_section(data)}
  </div>
</div>

<footer>
  <span class="note">Research project. Every row links to its source document;
    nothing here is legal or investment advice. Damages marked
    <span class="imputed">imputed</span> were never stated in the source and
    are inferred from a thesis prior — do not quote them. Click any row to
    expand it.</span>
  <span class="build">self-contained · no CDN · renders offline</span>
</footer>

<script>const DATA = {payload};</script>
<script>{_JS}</script>
<script>{panel_js}</script>
</body>
</html>
"""


def write(
    db: Database, cfg: Config, *, out_path: Path | None = None,
    limit: int | None = None,
) -> Path:
    """Render the dashboard and write it where the desktop shortcut points."""
    data = load(db, cfg, limit=limit)
    html = render(data)

    target = out_path or (cfg.data_root / "dashboard.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")

    # Also keep a dated copy, so "what did the list look like on the 12th" is
    # answerable. The live file is what the shortcut opens; the archive is what
    # makes a ranking argument reviewable after the fact.
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    archive = cfg.runs_dir / stamp
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "dashboard.html").write_text(html, encoding="utf-8")

    return target
