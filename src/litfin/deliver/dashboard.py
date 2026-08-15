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
   three states needing three different responses.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from ..store.db import Database
from .dataset import Dataset, load, to_json

_CSS = """
:root {
  --bg: #ffffff; --fg: #16181d; --muted: #5c6370; --line: #e3e6ea;
  --panel: #f7f8fa; --accent: #1a4f8a; --warn: #8a5a00; --warn-bg: #fff6e0;
  --bad: #98221f; --bad-bg: #fdecec; --good: #14683f;
  --row-hover: #f0f4f9; --chip: #eef1f5;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #14161a; --fg: #e6e8ec; --muted: #99a0ab; --line: #2b2f36;
    --panel: #1b1e24; --accent: #7fb3f0; --warn: #f0c05a; --warn-bg: #33290f;
    --bad: #f08a86; --bad-bg: #3a1c1b; --good: #6ed49f;
    --row-hover: #1f242c; --chip: #262b33;
  }
}
:root[data-theme="dark"] {
  --bg: #14161a; --fg: #e6e8ec; --muted: #99a0ab; --line: #2b2f36;
  --panel: #1b1e24; --accent: #7fb3f0; --warn: #f0c05a; --warn-bg: #33290f;
  --bad: #f08a86; --bad-bg: #3a1c1b; --good: #6ed49f;
  --row-hover: #1f242c; --chip: #262b33;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.5 -apple-system, "Segoe UI", Roboto, system-ui, sans-serif;
}
header { padding: 18px 22px 12px; border-bottom: 1px solid var(--line); }
h1 { margin: 0 0 4px; font-size: 19px; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 12.5px; }
.funnel { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 10px; }
.stat { background: var(--panel); border: 1px solid var(--line);
        border-radius: 6px; padding: 7px 11px; min-width: 88px; }
.stat b { display: block; font-size: 17px; font-variant-numeric: tabular-nums; }
.stat span { color: var(--muted); font-size: 11px; text-transform: uppercase;
             letter-spacing: 0.04em; }
.banner { margin: 12px 22px 0; padding: 10px 13px; border-radius: 6px;
          border: 1px solid; font-size: 13px; }
.banner.warn { background: var(--warn-bg); border-color: var(--warn); color: var(--warn); }
.banner.bad  { background: var(--bad-bg);  border-color: var(--bad);  color: var(--bad); }
.controls { display: flex; flex-wrap: wrap; gap: 9px; align-items: center;
            padding: 13px 22px; border-bottom: 1px solid var(--line); }
input, select, button {
  font: inherit; padding: 5px 8px; border: 1px solid var(--line);
  border-radius: 5px; background: var(--bg); color: var(--fg);
}
input[type=search] { min-width: 230px; }
button { cursor: pointer; background: var(--panel); }
button:hover { background: var(--row-hover); }
button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
label.inline { display: inline-flex; align-items: center; gap: 5px;
               color: var(--muted); font-size: 12.5px; }
.sizebar { display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
           padding: 10px 22px; border-bottom: 1px solid var(--line);
           background: var(--panel); }
.sizelabel { font-size: 12px; text-transform: uppercase; letter-spacing: .04em;
             color: var(--muted); }
#bands { display: flex; flex-wrap: wrap; gap: 6px; }
label.band { display: inline-flex; align-items: center; gap: 6px;
             background: var(--bg); border: 1px solid var(--line);
             border-radius: 999px; padding: 4px 11px; font-size: 12.5px;
             cursor: pointer; user-select: none; }
label.band:hover { border-color: var(--accent); }
label.band:has(input:checked) { border-color: var(--accent);
                                background: var(--row-hover); font-weight: 600; }
label.band.band-empty { opacity: .45; }
.band-n { font-variant-numeric: tabular-nums; color: var(--muted);
          font-size: 11.5px; }
.sizenote { flex: 1 1 240px; min-width: 220px; }
td .desc { color: var(--fg); opacity: .82; font-size: 12.5px; margin-top: 3px; }
.saved { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 22px 12px; }
.chip { background: var(--chip); border: 1px solid var(--line); border-radius: 999px;
        padding: 3px 11px; font-size: 12px; cursor: pointer; }
.chip .x { color: var(--muted); margin-left: 6px; }
.wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; min-width: 1500px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { position: sticky; top: 0; background: var(--bg); cursor: pointer;
     white-space: nowrap; font-size: 12px; text-transform: uppercase;
     letter-spacing: 0.03em; color: var(--muted); z-index: 2; }
th:hover { color: var(--fg); }
th .arrow { opacity: 0.45; }
tbody tr.row:hover { background: var(--row-hover); cursor: pointer; }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.caption { max-width: 300px; min-width: 210px; }
td.summary { max-width: 380px; min-width: 240px; font-size: 12.5px; }
td.summary .venue { margin-top: 3px; font-variant-numeric: tabular-nums; }
td.court { max-width: 220px; font-size: 12.5px; }
td.firms { max-width: 210px; font-size: 12px; }
.firmside { margin-bottom: 2px; }
.firmlabel { display: inline-block; min-width: 14px; font-weight: 600;
             color: var(--muted); font-size: 10.5px; }
.stage { display: inline-block; font-size: 11.5px; padding: 2px 8px;
         border-radius: 4px; background: var(--chip); white-space: nowrap;
         border: 1px solid transparent; }
.stage-inferred { opacity: .7; border-style: dashed;
                  border-color: var(--line); }
.tag { display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 4px;
       background: var(--chip); color: var(--muted); white-space: nowrap; }
.tag.dupe { border: 1px solid var(--line); }
.imputed { color: var(--warn); font-size: 11px; white-space: nowrap; }
.detail td { background: var(--panel); }
.detail dl { display: grid; grid-template-columns: 165px 1fr; gap: 5px 14px;
             margin: 4px 0 0; }
.detail dt { color: var(--muted); font-size: 12.5px; }
.detail dd { margin: 0; }
.detail .sm { color: var(--muted); font-size: 12px; }
a { color: var(--accent); }
section { padding: 20px 22px; border-top: 1px solid var(--line); }
h2 { font-size: 15px; margin: 0 0 4px; }
details > summary { cursor: pointer; color: var(--accent); margin-top: 8px; }
.cov { display: flex; flex-wrap: wrap; gap: 12px; margin: 10px 0; }
.cov .stat b.low { color: var(--bad); }
.cov .stat b.partial { color: var(--warn); }
.cov .stat b.high { color: var(--good); }
.mini { width: 100%; font-size: 12.5px; }
.mini th { position: static; }
.empty { padding: 40px 22px; color: var(--muted); }
footer { padding: 16px 22px 34px; color: var(--muted); font-size: 12px; }
.hide { display: none !important; }
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
  {key:'rank',    label:'#',        cls:'num',     get:p=>p.rank},
  {key:'score',   label:'Score',    cls:'num',     get:p=>p.score},
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
  {key:'jlabel',  label:'Jurisdiction', cls:'',    get:p=>(p.jlabel||'').toLowerCase()},
  {key:'event_date',   label:'Event date', cls:'', get:p=>p.event_date || p.published_at || ''},
  {key:'source_id',    label:'Source',     cls:'', get:p=>p.source_id},
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
  // ever shown when the source actually stated one.
  const dmg = p.imputed || !p.damages
    ? '<span class="imputed">not stated</span>'
    : '<b>' + fmtUSD(p.damages) + '</b><div class="sm">' + esc(p.band) + '</div>';
  return '<tr class="row" data-uid="' + esc(p.uid) + '">'
    + '<td class="num">' + p.rank + '</td>'
    + '<td class="num">' + p.score.toFixed(3) + '</td>'
    + '<td class="caption">' + esc(p.caption)
      + (p.docs > 1 ? ' <span class="tag dupe">' + p.docs
                    + ' documents</span>' : '')
      + '<div class="desc">' + esc(p.description) + '</div>'
    + '</td>'
    + '<td class="summary">' + summaryCell(p) + '</td>'
    + '<td>' + stageCell(p) + '</td>'
    + '<td class="court">' + (esc(p.court_display)
        || '<span class="sm">not stated</span>') + '</td>'
    + '<td class="firms">' + firmsCell(p) + '</td>'
    + '<td class="num">' + dmg + '</td>'
    + '<td>' + esc(p.jlabel) + '</td>'
    + '<td>' + esc(p.event_date || p.published_at || '').slice(0,10) + '</td>'
    + '<td><span class="tag">' + esc(p.source_id) + '</span></td>'
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
// one inferred from the event type, so the weaker one says so on hover.
function stageCell(p) {
  const inferred = p.stage_basis !== 'posture';
  return '<span class="stage' + (inferred ? ' stage-inferred' : '') + '"'
    + ' title="' + esc(inferred
        ? 'inferred from ' + p.stage_basis + ' — no explicit procedural posture'
        : 'from the procedural posture') + '">'
    + esc(p.stage) + '</span>';
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
      + ' <span class="band-n">' + n + '</span></label>';
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
  $('#shown').textContent = rows.length + ' of ' + DATA.prospects.length;
  const imp = rows.filter(p => p.imputed).length;
  $('#shown-imputed').textContent = imp
    ? imp + ' of these have no stated amount' : '';
  document.querySelectorAll('th[data-key]').forEach(th => {
    const a = th.querySelector('.arrow');
    if (a) a.textContent = th.dataset.key === sortKey ? (sortDir < 0 ? '▼' : '▲') : '';
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
  $('#saved').innerHTML = saved.map((s, i) =>
    '<span class="chip" data-i="' + i + '">' + esc(s.name)
    + '<span class="x" data-del="' + i + '">×</span></span>').join('')
    || '<span class="sm" style="color:var(--muted)">'
       + 'No saved filters yet — set some filters and press Save.</span>';
}

function init() {
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
  $('#covq').addEventListener('input', (e) => {
    const q = e.target.value.trim().toLowerCase();
    document.querySelectorAll('#covbody tr').forEach(tr => {
      tr.classList.toggle('hide', q && !tr.textContent.toLowerCase().includes(q));
    });
  });

  renderSaved();
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


def _banners(data: Dataset) -> str:
    """Warnings that must be impossible to miss, above the table."""
    out: list[str] = []

    broken = data.broken_sources
    if broken:
        names = ", ".join(f"{s.source_id} ({s.health})" for s in broken)
        out.append(
            f'<div class="banner bad"><b>{len(broken)} source(s) not healthy:'
            f"</b> {_esc(names)}. Rows below may be missing entirely from "
            f"these sources — this is not a quiet day.</div>"
        )

    pending = data.counts.get("awaiting_extraction", 0)
    if pending:
        out.append(
            f'<div class="banner warn">{pending:,} collected item(s) have not '
            f"been screened or extracted yet, so they cannot appear below. "
            f"Run <code>litfin screen</code> (free) then "
            f"<code>litfin extract</code>.</div>"
        )

    dark = data.dark_venues
    partial = data.partial_venues
    if dark or partial:
        out.append(
            f'<div class="banner warn"><b>Venue coverage is incomplete.</b> '
            f"{dark} court(s) publish no PACER RSS feed and {partial} publish "
            f"orders/opinions only. In those venues an empty result is "
            f"<em>absence of signal, not absence of activity</em>. Full map "
            f'at the bottom of this page.</div>'
        )

    if data.prospects and data.imputed_count:
        pct = 100.0 * data.imputed_count / len(data.prospects)
        out.append(
            f'<div class="banner warn">{data.imputed_count} of '
            f"{len(data.prospects)} rows ({pct:.0f}%) carry <b>no stated "
            f"damages figure</b> and are ranked on a thesis prior with an "
            f"uncertainty discount. They are marked <span "
            f'class="imputed">imputed</span> throughout; the dollar filter '
            f"deliberately excludes them.</div>"
        )
    return "\n".join(out)


def _coverage_section(data: Dataset) -> str:
    labels = {
        "high": "full PACER RSS feed",
        "partial": "orders/opinions only",
        "low": "NO feed — absence of signal is not absence of activity",
        "not_applicable": "not a PACER court",
    }
    if not data.courts:
        return (
            '<section id="coverage"><h2>Venue coverage</h2>'
            '<p class="sub">No coverage map yet. Build it with '
            "<code>litfin coverage --refresh</code>. Until then, an empty "
            "venue cannot be distinguished from an unmonitored one.</p></section>"
        )

    stats = "".join(
        f'<div class="stat"><b class="{_esc(k)}">{v}</b>'
        f"<span>{_esc(labels.get(k, k))}</span></div>"
        for k, v in sorted(
            data.coverage_summary.items(), key=lambda kv: -kv[1]
        )
    )
    rows = "".join(
        f"<tr><td>{_esc(c.confidence)}</td><td>{_esc(c.court_id)}</td>"
        f"<td>{_esc(c.full_name)}</td><td>{_esc(c.entry_types)}</td></tr>"
        for c in data.courts
    )
    return f"""<section id="coverage">
  <h2>Venue coverage — how much to trust an empty result</h2>
  <p class="sub">This is on the same page as the results on purpose. A venue
     with no feed produces no rows whether or not anything happened in it.</p>
  <div class="cov">{stats}</div>
  <input type="search" id="covq" placeholder="filter courts…">
  <details open>
    <summary>{len(data.courts)} courts</summary>
    <div class="wrap" style="max-height:340px;overflow-y:auto">
      <table class="mini">
        <thead><tr><th>confidence</th><th>id</th><th>court</th>
                   <th>entry types</th></tr></thead>
        <tbody id="covbody">{rows}</tbody>
      </table>
    </div>
  </details>
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
            f'<div class="banner warn" style="margin:10px 0"><b>'
            f"{len(data.claims_unmapped)} unmapped claims agent(s):</b> "
            f"{rows}. The rows are kept — an unrecognized vendor is a new "
            f"entrant or a rename, both worth knowing. Add an alias to "
            f"<code>connectors/claims/agents.toml</code>.</div>"
        )

    rows = "".join(
        f"<tr><td>{_esc(c.court)}</td><td>{_esc(c.case_number)}</td>"
        f"<td>{_esc(c.debtor)}</td>"
        f"<td>{'<a href=' + chr(34) + _esc(c.agent_case_url) + chr(34) + ' target=_blank rel=noopener>' + _esc(c.vendor_id) + '</a>' if c.agent_case_url else _esc(c.vendor_id)}</td>"
        f"<td>{_esc(c.date_filed)}</td></tr>"
        for c in data.claims
    )
    return f"""<section id="claims">
  <h2>Chapter 11 claims-agent census — {len(data.claims)} cases</h2>
  <p class="sub">Court-published assignment lists (S.D. Ohio, S.D.N.Y.,
     D.N.J.). These are census records, not deal events: they carry no
     outcome language and cost zero extraction budget by design.
     <b>D. Del. is absent</b> — the most valuable of the four — because its
     assignment list sits on a host whose robots.txt disallows it.</p>
  <div style="margin:8px 0">{chips}</div>
  {unmapped}
  <details>
    <summary>{len(data.claims)} assignments</summary>
    <div class="wrap" style="max-height:340px;overflow-y:auto">
      <table class="mini">
        <thead><tr><th>court</th><th>case</th><th>debtor</th>
                   <th>agent</th><th>filed</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </details>
</section>"""


def _sources_section(data: Dataset) -> str:
    rows = "".join(
        f"<tr><td>{_esc(s.source_id)}</td><td>{_esc(s.tier)}</td>"
        f"<td>{_esc(s.status)}</td>"
        f'<td class="{"" if s.health == "HEALTHY" else "imputed"}">'
        f"{_esc(s.health)}</td>"
        f'<td class="num">{s.items:,}</td>'
        f"<td>{_esc(s.last_success_at[:19])}</td>"
        f"<td>{_esc(s.health_note[:90])}</td></tr>"
        for s in data.sources
    )
    return f"""<section id="sources">
  <h2>Source health</h2>
  <p class="sub">A BROKEN source does not advance its watermark, so data it
     failed to read is re-read once the parser is fixed — but nothing below
     will show it until then.</p>
  <div class="wrap">
    <table class="mini">
      <thead><tr><th>source</th><th>tier</th><th>ToS status</th><th>health</th>
                 <th>items</th><th>last success</th><th>note</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</section>"""


def render(data: Dataset, *, panel_html: str = "", panel_js: str = "") -> str:
    """Pure: Dataset -> a complete HTML document. No I/O, no clock.

    `panel_html`/`panel_js` let the local server graft a control panel onto the
    same page. The static file passes neither, so what you open from a desktop
    shortcut has no buttons that would do nothing.
    """
    c = data.counts
    funnel = [
        ("items collected", c.get("items", 0)),
        ("screened out", c.get("screened_out", 0)),
        ("extracted", c.get("extracted", 0)),
        ("ranked", c.get("ranked", 0)),
        ("shown here", len(data.prospects)),
    ]
    stats = "".join(
        f'<div class="stat"><b>{v:,}</b><span>{_esc(k)}</span></div>'
        for k, v in funnel
    )

    body = (
        '<div class="empty">No ranked prospects yet. The funnel above shows '
        "where the pipeline stopped: collect with <code>litfin run</code>, "
        "screen for free with <code>litfin screen</code>, extract with "
        "<code>litfin extract</code>, then <code>litfin rank</code>.</div>"
        if not data.prospects
        else """<div class="wrap">
  <table><thead id="thead"></thead><tbody id="tbody"></tbody></table>
</div>"""
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
<header>
  <h1>LitFin prospects</h1>
  <div class="sub">
    Generated {_esc(data.generated_at)} ·
    declared purpose <b>{_esc(data.purpose)}</b> ·
    {_esc(data.data_root)}
    {" · run " + _esc(data.last_run_id) if data.last_run_id else ""}
  </div>
  <div class="funnel">{stats}</div>
</header>

{panel_html}

{_banners(data)}

<div class="controls">
  <input type="search" id="q" placeholder="search case, description, parties…">
  <select id="f-stage" title="case stage"></select>
  <select id="f-jgroup" title="federal or state"></select>
  <select id="f-juris" title="specific jurisdiction"></select>
  <select id="f-thesis"></select>
  <select id="f-area"></select>
  <select id="f-event"></select>
  <label class="inline"><input type="checkbox" id="f-firms">
     counsel named</label>
  <button id="reset">Reset</button>
  <button id="expand">Expand / collapse all</button>
  <button id="save" class="primary">Save filter</button>
  <span class="sub" style="margin-left:auto">
    <b id="shown"></b> <span id="shown-imputed" class="imputed"></span>
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
<div class="saved" id="saved"></div>

{body}

{_coverage_section(data)}
{_claims_section(data)}
{_sources_section(data)}

<footer>
  Research project. Every row links to its source document; nothing here is
  legal or investment advice. Damages marked
  <span class="imputed">imputed</span> were never stated in the source and are
  inferred from a thesis prior — do not quote them.
  Click any row to expand it.
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
