# LitFin — Session Handoff

Written 2026-08-15. Read this first if you are picking the project up cold.
The full design lives in the author's local plan file (not in this repo);
this file is the running state, the decisions already made, and what is next.

---

## What this is

A **research project** (not a commercial product — that distinction is load-
bearing, see below) that surfaces litigation-finance prospects: cases where a
judgment has been entered or a settlement reached, in commercial litigation,
antitrust, and bankruptcy, excluding IP, international arbitration, and
consumer. The goal is a daily ranked list of ≤100 with enough detail to triage.

Three deal theses drive scoring: judgment monetization / appeal funding,
post-settlement receivable monetization, and antitrust follow-on damages.

---

## Current state

**All phases 0–7 complete and verified against live data. 449 tests pass.
~2,190 real items across 12 sources. 193 documents extracted by Opus, and
**131 distinct matters** ranked after entity resolution. The dashboard,
digest, Excel export and local control panel are live.**

| Phase | Scope | State |
|---|---|---|
| 0 | HTTP layer, compliance gate, storage, canary, runner | done |
| 1 | SEC Litigation Releases, DOJ Antitrust, FTC (RSS) | done |
| 2 | DOJ Tunney Act filings, EDGAR full-text search, taxonomy, exclusion screen, Opus extraction, scoring | done |
| 3 | CourtListener/RECAP search, venue coverage map, docket alerts, webhook receiver | done¹ |
| 4 | govinfo opinion index, EDGAR daily index, state AG feeds, JPML | done² |
| — | **`deliver/`: dashboard, digest, mailer, local control panel** | **done** |
| 5 | Claims-agent routing table + chapter 11 census | done³ |
| 6 | NY eTrack email ingestion | built, DISABLED⁴ |
| 7 | Tier B sources, each gated on its own ToS review | reviewed, 1 of 8 cleared⁵ |

² Two Phase 4 sources are deliberately INDEXES rather than event sources, and
the distinction matters — see "What Phase 4 actually gives you" below.

³ 102 chapter 11 assignments from three courts. **Delaware's list is refused,
not missing** — see "Phase 5" below.

⁴ Two switches required, and the parser needs one real alert to calibrate
against — see "Phase 6" below.

⁵ Every Tier B source's terms were read. Only Stretto cleared. See "Phase 7"
below — the refusals are the substance, and each carries its verbatim clause.

---

## What shipped this session

### `deliver/` — the piece the user could not see before

| file | what |
|---|---|
| `dataset.py` | ONE assembly step feeding all three renderers |
| `dashboard.py` | self-contained HTML: sortable, filterable, expandable rows, saved filters |
| `digest.py` | top-20 email, inline styles only (Gmail strips `<style>`) |
| `mailer.py` | the send gate |
| `server.py` | `litfin serve` — loopback control panel |

**`dataset.py` exists so the email and the dashboard cannot disagree.** Three
renderers running their own queries drift, and the failure mode is an email
that says a different case ranked first with no way to tell which is lying.

**The send gate.** `dry_run=True` is a default on the function signature, not
a config value that could be absent or overwritten. Live sending needs BOTH
`deliver.send_enabled = true` AND every recipient in
`deliver.recipient_allowlist` — two conditions because each catches a
different mistake ("I did not mean to send at all" vs "I meant to send, but
not there"). Recipients compare on the bare address via `parseaddr`, so
`me@mine.invalid <evil@elsewhere>` does not slip through. **A refusal raises**
rather than falling back to dry-run, because a scheduled job that quietly
stops sending looks exactly like a quiet day. The server has no live-send
button at all. Currently: gate CLOSED, allowlist empty, nothing has ever been
transmitted.

**`litfin serve` is loopback-only and there is no `--host` flag.** This
process can rescore a prospect list and spend money on an API. Mutating
requests need a per-start CSRF token, because loopback is not a security
boundary — any page in your browser can POST to `127.0.0.1:8788`, and a
hostile page can issue the request but cannot read the token. `run` and
`extract` need a typed confirmation and are labelled with what they cost.

**The table reads in English.** Every row carries a description composed from
the structured fields — "The parties reached a settlement in an antitrust
matter in S.D.N.Y. Stated amount $400M. It could lead to follow-on antitrust
damages. Defendant is a public company." Composed rather than model-written on
purpose: it is always present (the LLM summary is empty on a fair number of
rows) and it is testable. The `thesis / event / practice_area` tags left the
table for the expanded row, which is what made space for it.

**Claim size filters by band, not by a number you have to guess.** Each band
shows a live count computed with the *other* filters applied but the size
filter itself ignored — so the number tells you what checking that box would
give you. `Not stated` is a band rather than a hidden exclusion: it is 93% of
this corpus, which is a fact about free sources worth seeing. An imputed
figure lands there and can never satisfy a dollar range.

**Jurisdiction is normalized before it reaches the dropdown.** The model
returns `federal`, `S.D.N.Y.`, `New York`, `U.S. District Court` — a raw list
of near-duplicates that filtered almost nothing. It is now a Federal/State
selector plus a detail list scoped to it. A bare state name with no court
marker renders `New York (court unclear)` rather than being guessed into a
bucket.

### Scoring rebalanced toward settlement

`EVENT_FIT` used to put `judgment_entered` (1.00) above
`settlement_final_approval` (0.90) — the litigator's view of how far a case
has gone rather than the funder's view of how certain the money is. Inverted:

```
settlement_final_approval 1.00 > preliminary 0.97 > reached 0.95
  > judgment_entered 0.88 > jury_verdict 0.82 > appeal_filed 0.78
```

A verdict sits BELOW an entered judgment because it is the most appealable
moment in a case's life. A `THESIS_PRIORITY` multiplier (post_settlement 1.00,
antitrust_followon 0.95, judgment_monetization 0.92) tilts further without
burying antitrust follow-on, which is one of the three theses and whose events
are usually `judgment_proposed`.

**`[score.weights]` and `[score.event_fit]` now actually load from
litfin.toml.** The scoring docstring promised they were tunable there from the
first commit and nothing ever read them, so every "try a different weighting"
run meant editing Python. Explicit arguments still beat the config file (the
server's sliders must win), and the config file beats the hardcoded defaults.

### Four new columns

| Column | Source | Note |
|---|---|---|
| Summary | the model's own summary | short venue underneath |
| Stage | `procedural_posture`, else `event_type` | inferred stages render dashed and say so |
| Court | `court`, falling back to `venue` | |
| Law firms | new `counsel_plaintiff` / `counsel_defendant` | mostly empty, honestly |

**Stage sorts by proximity to a fundable claim, not chronology.** *Closed* is
the latest thing that can happen to a case and the least interesting, so it
sorts near the bottom; *On appeal* sits below an entered judgment because an
appeal re-opens risk the judgment had closed. Patterns are scanned
latest-stage-first, so "the motion to dismiss was denied and the parties
reached a settlement" reads as a settlement without parsing the sentence.

**Counsel required new data.** The schema had parties but not firms, so
`SCHEMA_VERSION` was added, bumped to 2, and `litfin extract --refresh`
re-extracts only the rows captured under an older version — adding one field
should not mean re-extracting the corpus, and should not mean living with a
permanently blank column either. 180 rows refreshed.

Capture is thin and the UI says so: **8 of 131 matters carry a named firm**,
most of them government litigators (DOJ Antitrust, State AGs). Agency press
releases almost never name counsel and RECAP snippets often don't. `not named`
and `not captured` are different cells because only the second is fixable.

### Excel export (`deliver/excel.py`)

`litfin export`, or a download button on the server. Three sheets: Prospects,
Venue coverage, Sources. Frozen header through the Case column, autofilter,
sized columns, currency and date formats, hyperlinked sources.

**A spreadsheet is read without its caveats** — it gets sorted, summed,
charted and mailed onward, away from every banner the dashboard put around a
number. So the distinctions survive into the cells: an imputed damages figure
is NEVER written into the numeric column (it would land in a SUM() as a
fabricated total), the coverage warning is repeated in the header block and on
its own sheet, and a non-HEALTHY source is visible in the file.

### Phase 5 — claims-agent routing (102 cases)

```
kroll 26 · epiq 18 · none_retained 16 · stretto 15 · verita 12
omni 5 · angeion 5 · gcg 4 · bmc 1
```

**Four courts, four different page shapes**, all discovered by reading their
live pages:

| court | landing | assignments |
|---|---|---|
| ohsb | `/claims-agents` | inline table on the same page |
| nysb | `/claims-agents` (vendor directory) | `/megaCases` |
| njb | `/claim-agent-cases-and-protocols` | `/content/claims-agent-case-assignments-…` |
| deb | `/claims-agents-and-assignments` | **REFUSED** — robots-disallowed |

**Delaware's assignment list is a determination, not a gap.** It lives at
`media.deb.uscourts.gov/moveit/ClaimsAgentCases.html`, whose robots.txt
disallows it. Delaware is the most important chapter 11 venue in the country
and this is the single most valuable page of the four — which is exactly why
the rule matters. We take its vendor directory (allowed) and poll its landing
page, whose canary fires if the court ever moves the list somewhere fetchable.

**The routing table is data, not scraped** (`connectors/claims/agents.toml`).
Every district prints the same vendor differently; resolution has to happen
against a curated alias list. `litfin claims --table` shows it. Nothing in
Phase 5 fetches a vendor site — a test pins that.

**Unmapped agents alert, never drop.** `GCG, Inc.` surfaced this way on
nysb's mega case list and is now a routing entry — deliberately NOT folded
into Epiq despite the 2018 acquisition, because the census records who the
court actually retained and a pre-2018 GCG case is not reachable from Epiq's
current index.

All 121 claims items screen as `no_signal` and cost **zero** extraction
budget, same discipline as `sec_daily_index` and `govinfo`.

### Phase 6 — eTrack, built and switched off

`connectors/etrack_email.py`: pure parser, IMAP ingestion, ranked enrollment
worklist, auto-confirm on first alert. **Two switches, both required:**

```toml
[etrack]
decision_recorded = ""   # the LEGAL decision — who resolved "may not be mined"
enabled = false          # the OPERATIONAL one
```

Two fields because they record two different decisions, and an operational
boolean must not be able to answer a legal question. `assert_enabled()` runs
before any credential is read or socket opened. Scraping NY UCS stays
PROHIBITED regardless of either switch — different source id, no escape hatch.

**Enrollment cannot be automated.** A human fills in a UCS form per case. The
status ladder is `candidate → enrolled → confirmed`, and only an arriving
alert confirms — a human saying "I enrolled" is self-reported, and the first
alert is the only proof that does not involve touching the site.

### Phase 7 — reviewed. Seven of eight refused.

The blocker was always the reviews, and the reviews are now done. The plan
expected them to clear under a research purpose. They mostly did not.

| Source | Outcome | Ground |
|---|---|---|
| **claims_stretto** | **VERIFIED_PERMITTED** | no general automated-access clause; the only scraping bar is scoped to its AI assistant |
| stanford_ssc | PROHIBITED | 403s an identified client on homepage AND disclaimer |
| claims_epiq | PROHIBITED | terms bar automated searches absent prior written consent |
| claims_angeion | PROHIBITED | terms bar robots "for any purpose"; personal use only |
| claims_omni | PROHIBITED | 403s an identified client |
| claims_verita | UNVERIFIED | broad reproduction bar, no automated-access clause — needs a human |
| claims_bmc | UNVERIFIED | publishes no terms of use at all |
| de_courtconnect | UNVERIFIED | click-through disclaimer; accepting it is a legal act |

**Two reversals worth keeping.** Stanford was the plan's named easiest win
*because its robots.txt is permissive* — and the server 403s us. Omni's
robots.txt carries `use=reference` (a yes) — and the server 403s us. When
robots says yes and the server says no, **the server wins**. A permissive
robots file is a statement about crawlers, not consent. Both notes preserve
the reasoning so nobody re-reads the robots file and re-enables them.

**The reviews needed a tool that did not exist.** The plan specified
`litfin compliance review`; it was never built, and without it the gate is
circular — a source stays unverified until someone reads its terms, and its
terms cannot be fetched because it is unverified. It now exists, with a narrow
`reading_terms` path through `PoliteClient`:

- the URL must be one of that source's own declared `tos_urls` — a literal
  membership test, not a pattern, so it cannot widen into a data fetch
- `PROHIBITED` still refuses: a site that already refused consent does not get
  re-litigated by re-reading it
- robots, rate limits, breaker and budget all still apply
- `--record` prints the registry diff to commit; it never edits the registry,
  because a determination should arrive as a reviewed commit

Two recorded ToS URLs were stale and 404ing (Stretto, Angeion); both corrected
in the registry with a note.

**Every determination is marked `machine-assisted read ... confirm before
relying`.** They rest on real fetched documents with verbatim quotes, but a
model reading a contract is not counsel signing off. The refusals are the safe
direction; the one permission — Stretto — is the one to confirm before leaning
on it commercially.

`claims_kroll` and `nc_business_court` were not re-examined and must NOT be
built absent affirmative written permission, per the original design.

### What Stretto adds

369 lead chapter 11 cases with 2,623 affiliated debtors, against the 102 the
four court-published lists carry. Same record kind as the Phase 5 census — no
event language, screens out at zero extraction cost — but roughly four times
the coverage, plus affiliated debtors and a docket URL per case.

**One hard rule, enforced by `allowed_url_patterns` and a test:** the
connector never touches "Stretto Conductor", their AI assistant. Section 21 of
their terms is the one clause in the document this pipeline could actually
violate.

**One trap, pinned by the canary:** omit the DataTables `columns[...]` query
parameters and the endpoint returns HTTP 200, valid JSON, `recordsTotal: 2992`
and an EMPTY `data` array. To anything checking only the status code that is
indistinguishable from a quiet week, forever.

¹ Docket alerts are built but **cannot be exercised without a CourtListener
token** — the endpoint requires auth. The webhook receiver is built and tested
end-to-end locally (4.6 ms response, well under the 1-second contract), but
has not been registered with CourtListener because that needs a publicly
reachable URL.

### What Phase 4 actually gives you

Two of the four are **event sources**; two are **indexes**. Conflating them
would either waste extraction budget or create false confidence.

| Source | Role | Live result |
|---|---|---|
| **state_ag** | event source — multistate AG settlements carry dollar figures | 90 releases from 7 states |
| **jpml** | event source — MDL formation is the middle link in the antitrust follow-on chain | 160 pending MDLs, incl. Payment Card Interchange Fee, Respimat Pharmaceuticals, Construction Equipment Rental |
| **sec_daily_index** | INDEX — coverage accounting | 903 of 11,140 daily filings were 8-K/10-Q/10-K |
| **govinfo** | INDEX — federal opinion cross-reference | civil-only filtering, ~half the volume dropped free |

**The indexes carry no event language and must not.** `sec_daily_index` has no
document text; `govinfo` has only case names. Writing words like "settlement"
into their bodies would manufacture a signal the source does not have, so both
store `record_kind` markers and the screen correctly drops them from
extraction. A test pins this. Their value is joins: govinfo answers "does this
case have a published opinion?", and the daily index gives the honest
denominator — FTS found ~200 relevant filings for 2026-08-14 out of 903 that
existed, so phrase search examined roughly a fifth of them.

**State AG coverage is 7 of 50 and the connector says so.** Forty candidate
URLs were probed. NY 403s an identified client on every path (treated as
refusal of consent, per the standing rule) and TX 404s everywhere — the two
highest-value states are dark. The full probe record, including URLs tried
per failed state, is in `connectors/state_ag_feeds.toml` so nobody re-probes
them blindly.

### Screen behaviour on the full corpus (verified)

```
candidates considered   1689
dropped: out of scope     25
dropped: no deal signal 1477
sent to extraction       187
```

Per-source verdicts confirm the index design works: **all 903
`sec_daily_index` and all 30 `govinfo` items screen as `no_signal`** and cost
zero extraction budget, while still being queryable for joins. `jpml`
contributes 118 no-signal and 8 excluded — MDL captions name the litigation
but carry no outcome language, which is correct.

### Verified live, end to end

`run` → `screen` → `extract` → `rank` produces a ranked list from real data.
The top six on the last run were all genuine DOJ antitrust consent decrees
(CRH plc, Cal-Maine Foods, Taiheiyo Cement, Columbus McKinnon, Constellation
Energy, Reddy Ice), every one correctly classified `judgment_proposed` rather
than `judgment_entered` — the Tunney Act distinction holding under real data.

### Commands

The one to start with:

```bash
.venv/Scripts/python.exe -m litfin.cli serve
```

```bash
.venv/Scripts/python.exe -m litfin.cli run
```
```bash
.venv/Scripts/python.exe -m litfin.cli screen
```
```bash
.venv/Scripts/python.exe -m litfin.cli coverage
```
```bash
.venv/Scripts/python.exe -m litfin.cli extract
```
```bash
.venv/Scripts/python.exe -m litfin.cli rank
```
```bash
.venv/Scripts/python.exe -m litfin.cli dashboard --open
```
```bash
.venv/Scripts/python.exe -m litfin.cli digest
```
```bash
.venv/Scripts/python.exe -m litfin.cli claims
```
```bash
.venv/Scripts/python.exe -m litfin.cli etrack
```
```bash
.venv/Scripts/python.exe -m litfin.cli alerts --subscribe
```
```bash
.venv/Scripts/python.exe -m litfin.cli webhook --drain
```

`screen` is free and calls no API — use it to see what would be sent for
extraction and why. `rank` recomputes entirely from stored extractions, so
tuning weights costs seconds and zero API calls.

Data lives at `C:\LitFinData\` — deliberately off the OneDrive-synced tree,
because OneDrive corrupts SQLite.

---

## Secrets

`.env` is gitignored and holds `ANTHROPIC_API_KEY`, `COURTLISTENER_TOKEN`, and
`LITFIN_WEBHOOK_SECRET`. `config.load_dotenv()` loads it, and **existing
environment variables win** so a shell or CI value is never overridden by a
stale file. `litfin.toml` is meant to be committed and holds no secrets.

> ⚠️ **The Anthropic key currently in `.env` was pasted into a chat transcript
> and should be rotated** at console.anthropic.com. Replace the value in
> `.env`; nothing else needs to change.

## Blocked / needs a human

1. **The CourtListener scope question — resolve before relying on it.**
   Free Law Project permits "personal, educational, research, journalistic,
   and exploratory use", but the same terms bar building "tools for for-profit
   or non-profit organizations, even if those tools aren't sold." A personal
   research project clears that; the same code running as firm infrastructure
   does not. One email to their partnerships team settles it and costs nothing
   compared to rebuilding later.

3. **`purpose` must stay truthful.** `litfin.toml` declares
   `purpose = "research"`. This is not a label — the compliance layer reads it
   on every fetch, and CourtListener is marked `RESEARCH_ONLY`. If this ever
   becomes firm infrastructure, flip it to `"commercial"`; the affected sources
   will then disable loudly instead of continuing under terms that no longer
   apply. A test pins this behavior.

4. **No CourtListener token — this is the one thing gating docket alerts.**
   Everything runs unauthenticated at 5/min, 50/hour, 125/day. A $10/mo
   membership raises that to 10/min, 75/hour, 300/day and makes docket alerts
   unlimited (free tier caps at 5). Set `COURTLISTENER_TOKEN` in `.env`; the
   client detects it, sends `Authorization: Token <value>`, and raises the
   configured rate limits automatically. Then:
   `litfin alerts --subscribe`.
   25 candidate dockets are already discovered and waiting — FTX Trading,
   Yellow Corporation, Zohar III, Gjovik v. Apple among them.

5. **Webhook needs a public URL.** The receiver runs and is tested, but
   CourtListener must be able to reach it. Options: a tunnel (cloudflared,
   ngrok) for testing, or a small always-on host for production. Register
   `https://<public-host>/webhook/<LITFIN_WEBHOOK_SECRET>` in the
   CourtListener webhooks panel.

---

## START HERE next session

1. Read this file, then `README.md`.
2. Sanity-check the build:
   `.venv/Scripts/python.exe -m pytest -q` → expect **449 passed**.
   `.venv/Scripts/python.exe -m litfin.cli status` → expect **12 sources, all HEALTHY**.
   `.venv/Scripts/python.exe -m litfin.cli serve` → the dashboard, with buttons.
3. **Rotate the Anthropic key in `.env`** — it was pasted into a chat
   transcript. Nothing else changes.

Do NOT start by re-probing hosts. Every measured finding is already recorded
in the tables below and enforced in code; re-probing wastes rate budget and
several of these hosts throttle or reset connections when pushed.

## Data-quality fixes — both done

### Entity resolution (`score/cluster.py`)

150 scored documents → **125 distinct matters**, 25 duplicates absorbed.
Clustering runs at rank time, never at ingestion: two documents about one
matter are two real observations, and collapsing them in storage would lose
which source saw what and make the decision irreversible.

Nothing is deleted. `top_prospects` filters to the primary and
`cluster_members()` returns the rest, which the dashboard lists under *Also
reported by*. Rank numbers count matters, not documents.

**THE TRAP THAT SHAPES THE MATCHER.** `case_caption` is empty on a meaningful
fraction of rows, and in the live corpus NINE unrelated matters shared that
empty caption — an antitrust consent decree, an HSR annual report, a speech,
several docket entries. Clustering without checking that the key is
substantive would have merged all nine and silently deleted eight real
prospects. A missed merge costs a duplicate row; a wrong merge deletes a
matter, and a deleted matter is invisible. So a row only clusters when it has
a key worth trusting; everything else stands alone.

Three normalizations, each from an observed caption pair:

| Pattern | Example |
|---|---|
| corporate suffixes | `Acme Corp.` = `Acme Corporation, et al.` |
| government plaintiff enumeration | `United States and 17 State Attorneys General v. Cal-Maine` = `United States and Plaintiff States v. Cal-Maine Foods, Inc.` |
| truncated defendant list | `Taiheiyo Cement, et al.` = `Taiheiyo Cement Corporation and CalPortland Company` |

The `us` token is deliberately KEPT in the key: dropping it would merge
`Gjovik v. Apple` with `United States v. Apple`. The truncation merge requires
the short caption to carry an explicit "et al.", exactly one candidate to
extend it, and the extension to land on a token boundary — without the "et
al." admission, a shorter caption may simply be a narrower case.

Residual after clustering: two apparent duplicates in the top 100, both
correct non-merges. `National Alliance to End Homelessness v. HUD` and `State
of Washington v. HUD` are different suits against one agency; two uncaptioned
RECAP rows have no key and are left alone rather than guessed at.

### Government forfeiture (`score/exclude.py`)

13 rows dropped, including the five copies of `United States v. Approximately
225,364,961 USDT` that were ranking on the seized amount printed in their own
case name. A government in rem forfeiture has no assignable claim and no
counterparty to collect from — it fits none of the three theses at any score.

Precision is the whole design. The bare word "forfeiture" is ordinary
commercial language (deposits, unvested equity, leases, anti-forfeiture
clauses), so every pattern requires either the in rem CAPTION convention — the
defendant is a *thing* — or an explicit forfeiture proceeding or statute.
Eleven "must exclude" and eleven "must not exclude" cases are pinned.

`rank` now RE-SCREENS stored extractions before scoring, because the in rem
convention lives in the caption and only exists once the model has produced
one — the pre-extraction screen over a RECAP docket entry's procedural body
could never have seen it. Late exclusions are written to a separate
`excluded_reason_late` column so the pipeline's call never overwrites the
model's.

**3. Confirm the Stretto permission before leaning on it.** It is the one
source now enabled on a machine-assisted contract read. The refusals are safe
either way; a wrong permission is not.

After those: the CourtListener token, still the one thing gating docket
alerts, and — if any Tier B source is genuinely wanted — an email asking for
written permission, which is the only remaining route for Epiq, Angeion, Omni,
Stanford and Kroll.

### Ingestion cadence

```bash
.venv/Scripts/python.exe -m litfin.cli run --weekly
```

Most sources are daily. `--weekly` adds JPML, whose list changes by a handful
of entries a month and whose host declares `Crawl-delay: 10`. A full run
across all ten sources takes several minutes because the rate limits are
deliberately conservative — that is the design working, not a hang.

### Running the webhook

```bash
.venv/Scripts/python.exe -m litfin.cli webhook --port 8787
```
```bash
.venv/Scripts/python.exe -m litfin.cli webhook --drain
```

`--drain` does the actual processing, out of band. Add `--allow-any-ip` for
local testing only — with no HMAC available, the IP allowlist is half the
authentication.

---

## Design rules — do not "simplify" these

**`parse()` is pure over bytes.** No I/O, no clock, no watermark. Fixture
replay, canaries, and `litfin replay` all fall out of that purity. Watermark
filtering happens *outside* parse, in the runner, which is what makes the
`rows_parsed` vs `rows_new` comparison possible. A parser that filters
internally silently breaks the entire canary system.

**Items and watermark advance in ONE transaction** (`db.commit_task`). A crash
rolls back all three writes together, so the watermark can never advance past
durably-stored items. At-least-once delivery + idempotent writes on a
deterministic `item_uid` = exactly-once effect. Do not split this function.

**A BROKEN task must not advance the watermark**, or the data the parser failed
to read is skipped forever once the parser is fixed.

**Never defeat a WAF with a headless browser.** A 403 to an honest, identified
client is treated as refusal of consent. Working around it converts a technical
block into deliberate circumvention.

### The canary decision table

| `rows_parsed` | `rows_new` | verdict |
|---|---|---|
| 0 | 0 | **BROKEN** — selector/schema changed |
| >0 | 0 | HEALTHY — the normal quiet day |
| >0 | >0 | HEALTHY — new items |
| 0, HTTP 304, no body | — | HEALTHY — nothing to parse |
| 0, HTTP 304, **cached body** | — | **BROKEN** — parser regression hiding behind the cache |
| 0, server reported count 0 | — | HEALTHY — affirmative "no news" (a Saturday) |

Plus a third state, **PARTIAL COVERAGE**: rows were truncated by a hard API
page cap that cannot be paginated affordably. Rows are KEPT, the slice is
flagged, and a `## PARTIAL COVERAGE` section appears in the run report. This is
deliberately *not* BROKEN — marking it so would discard good rows and freeze
the watermark permanently, since the condition recurs every run.

### Two asymmetries the design is built around

**A false exclusion is invisible; a false inclusion costs one row.** The
lexical screen only fires on unambiguous signals and defers everything else to
the LLM. It deliberately does **not** exclude on the bare word "consumers" —
consumer harm is the central standard in antitrust analysis, so that would
silently drop exactly the matters this pipeline exists to find. Tests pin both
directions.

**Missing damages must not score zero.** Most free sources never state a
figure, so a zero-impute would rank every unlabeled matter last — exactly
backwards, since the largest cases often lack a figure in the first public
document. Missing figures fall back to a thesis prior, take an uncertainty
discount, and are flagged `[IMPUTED]` so you can see which rankings rest on a
real number.

---

## Measured findings (each encoded where it is enforced)

These cost real debugging time. They are recorded in code comments at the point
of enforcement, not just here.

| Finding | Where |
|---|---|
| **Never put a URL or an HTTP-library token in the User-Agent.** Both sec.gov and ftc.gov 403 on either, independent of domain. Name + contact email → 200. | `config.py:Identity.user_agent` |
| **Rate-limit keys are not hostnames.** SEC's cap applies to the requester across `www.`/`efts.`/`data.sec.gov`. Per-hostname buckets would let three connectors each run at the cap and get the IP banned. | `net/ratelimit.py` |
| **A per-second rate cannot honor an hourly quota.** 5/min permits 300/hour against a published 50/hour limit. Hence `HostRate.hourly_cap`. | `net/ratelimit.py` |
| **SEC's feed `guid` is an opaque UUID**; `dc:creator` carries the monotonic `LR-#####`. Its `<link>` values also carry a trailing newline that 404s unstripped. | `connectors/feeds.py`, `connectors/rss.py` |
| **DOJ publishes ONE parameterized feed**, not three (`field_component=376` = Antitrust Division). `?page=1` 403s — only page 0 is fetchable. | `connectors/feeds.py`, `connectors/doj_cases.py` |
| **EDGAR truncates twice, both silently**: 10,000-result ceiling (`relation: "gte"`) and a 100-per-page cap. A combined-form query reported 104 and returned 100. Slicing by day AND form fixes both. | `connectors/sec_fts.py` |
| **CourtListener search caps at 20/page and IGNORES `page_size`.** Cursor-walking is unaffordable at 50/hour, so slices are per-day and truncation is reported as partial coverage. | `connectors/courtlistener.py` |
| **`meta.date_created` is CourtListener's INGESTION time**, not the court's — it can lag by weeks. Event dates come from `entry_date_filed`. | `connectors/courtlistener.py` |
| **A Competitive Impact Statement is definitionally antitrust** (it exists only under the Tunney Act) but carries no antitrust keyword in its title. | `score/taxonomy.py` |
| **"proposed final judgment" contains "final judgment"** as a substring. Order of checks matters, or every settlement still inside its 60-day comment window is misread as a decided matter. | `score/taxonomy.py`, `connectors/doj_cases.py` |
| **"Proposed Jury Verdict" is a blank trial-prep FORM, not a verdict** — the same substring trap. Found only by running live extraction: Opus returned `no_event` for four straight candidates the regex had scored 0.9. | `score/taxonomy.py` |
| **Criminal prosecutions have no damages to monetize** — but DOJ *criminal antitrust* must survive the screen, since it is the leading indicator for follow-on private treble damages. Carve-out implemented. | `score/exclude.py` |
| **Structured outputs rejects a stock Pydantic schema** — every object needs `additionalProperties: false`, including nested `$defs`. | `extract/schema.py:_harden` |
| **SQL alias collision collapsed 96 dockets into 1.** Aliasing `json_extract(...) AS docket_id` while LEFT JOINing a table that also has `docket_id` made SQLite resolve `GROUP BY` to the joined column — NULL for every row. Aggregate in a subquery first. | `store/db.py:candidate_dockets` |
| **EDGAR's .idx header wraps across TWO physical lines** (`Form Type/Company/CIK` then `Date Filed/File Name`). Deriving column offsets from "the header row" captured three of five fields and produced zero rows. Match rows by shape instead. | `connectors/edgar_index.py:_ROW_RE` |
| **`Pending_MDL_Dockets...\.pdf$` can match an href but never a whole document.** The end-anchor made the JPML canary fail on every run even though the link was present. | `connectors/jpml.py:_WANTED_PDF` |
| **JPML's MDL list is a dated monthly PDF**, and neither `/pending-mdls` nor `/pending-mdls-0` contains any MDL data — both are navigation pages returning 200. Two-stage: discover the URL, fetch it next run. | `connectors/jpml.py` |
| **`plan(watermark)` was always passed `None`**, making the declared interface a lie and ruling out any connector whose URLs are discovered rather than known upfront. Now threaded through via a `_plan` watermark key. | `runner/orchestrator.py` |
| **The four claims-agent courts do not agree on column order OR meaning.** ohsb/nysb are (case, debtor, agent, date); njb is (case+judge, vicinage, title, agent) with no date column. A positional parser writes "Newark" into the debtor field and looks like it worked. Columns resolve by header text; a missing required header fails the canary. | `connectors/claims/routing.py:_roles_for` |
| **A role-matching loop that `break`s on the first matching needle silently loses columns.** njb's "Case Title" matched the `case` needle, found `case_number` already taken, and gave up — every njb row carried an empty debtor while the parse reported success. Keep trying the remaining patterns. | `connectors/claims/routing.py:_roles_for` |
| **`media.deb.uscourts.gov` disallows robots** — and it hosts Delaware's claims assignment list, the most valuable of the four. Refusal, recorded and reported, not routed around. | `connectors/claims/routing.py:DEB_ASSIGNMENTS_REFUSED` |
| **`lxml.text_content()` concatenates block elements with NO separator**, so an HTML eTrack alert collapses to `…651234/2026Case Name:…`. That defeats both the line-anchored field regex and the `\b` ending the index pattern — an HTML alert yielded nothing while looking like a clean parse. Block tags → newlines first. | `connectors/etrack_email.py:_html_to_text` |
| **A whole-key `replace("no", "number")` corrupts every label containing "no"** — "notification type" → "numbertification type". Whole-key alias lookup instead. | `connectors/etrack_email.py:_LABEL_ALIASES` |
| **Loopback is not a security boundary.** Any page in a browser can POST to `127.0.0.1:8788`. A per-start CSRF token in a custom header fixes it: a hostile page can issue the request but cannot read the token to include. | `deliver/server.py:_authorized` |
| **JSON embedded in a `<script>` must escape `</`**, or a case caption containing `</script>` closes the tag early and dumps the rest of the dataset into the document as markup. | `deliver/dashboard.py:render` |
| **Stretto's case endpoint returns `recordsTotal: 2992` with an EMPTY `data` array when the DataTables `columns[...]` params are omitted.** HTTP 200, valid JSON, plausible total, no rows — a silent "quiet week" forever. The canary asserts rows *against* a non-zero total. | `connectors/claims/stretto.py:canary` |
| **`recordsTotal` (2,992 debtor rows) and `data` (369 lead debtors) do not measure the same thing.** Comparing them would report permanent partial coverage. | `connectors/claims/stretto.py:parse` |
| **A permissive robots.txt is not consent.** Stanford and Omni both allow our agent in robots and both 403 it at the server. Where they disagree, the server is the answer. | `compliance/registry.py` |
| **The compliance gate was circular.** A source stays UNVERIFIED until its terms are read, and its terms could not be fetched because it was UNVERIFIED. Broken by a `reading_terms` path restricted to the policy's own `tos_urls`. | `net/client.py:get` |
| **Two recorded ToS URLs were stale and 404ing** (Stretto's `/terms-of-use`, Angeion's). A review against a 404 is not a review. | `compliance/registry.py` |
| **`stretto.com` 403s its own robots.txt while `www.stretto.com` serves it 200.** The bare host reads as a refusal and the www host does not — same terms document, two different verdicts depending on which URL you happened to record. | `compliance/registry.py:claims_stretto` |
| **A dollar figure in a case NAME is not a claim.** `United States v. Approximately 225,364,961 USDT` ranked five times on the seized amount in its own caption. Excluded on the in rem convention and forfeiture statutes only — never on the bare word "forfeiture", which is ordinary commercial language. | `score/exclude.py:FORFEITURE_PATTERNS` |
| **Nine unrelated matters shared an empty `case_caption`.** Clustering on a normalized key without checking it is SUBSTANTIVE would have merged them and deleted eight real prospects. Unkeyable rows become singletons. | `score/cluster.py:normalize_caption` |
| **An HTTP handler that rejects without reading the request body sends an RST, not a FIN.** The webhook 403/404 paths answered without draining, so the caller saw a connection reset instead of the status actually sent — and CourtListener counts a reset toward the 8 failures that auto-disable an endpoint. Surfaced as a flaky test. | `net/webhook.py:_drain_body` |
| **`CREATE TABLE IF NOT EXISTS` never adds a column to an existing table.** A new column in schema.sql silently never reaches a live database, and an index on that column fails the whole schema load. Additive migrations run explicitly, after which the index is created. | `store/db.py:_migrate` |
| **An imputed damages figure must never reach a spreadsheet's numeric column.** On screen it can carry a caveat; in a cell it gets summed, averaged and charted as real. The Excel export writes stated figures only and leaves the rest blank. | `deliver/excel.py:build` |
| **`event_type` and `procedural_posture` answer different questions.** The first says what just happened, the second says where the case stands — and the second is what a triager asks. Posture wins where specific; an event-type fallback is marked as inferred. | `deliver/dataset.py:derive_stage` |
| **Stage sorted chronologically puts "Closed" on top**, which is the least fundable stage there is. Ordered by proximity to a fundable claim instead. | `deliver/dataset.py:STAGE_ORDER` |
| **"reached a settlement" and "settlement reached" are the same fact in two word orders.** Matching only the latter let an earlier clause in the same sentence decide the stage. | `deliver/dataset.py:_STAGE_PATTERNS` |
| **`[score.weights]` was documented as tunable in litfin.toml and never loaded.** Three sessions of "tune the weights" meant editing Python. | `config.py:load_config` |
| **`lxml` abbreviation matching breaks on dots.** `S.D.N.Y.` never matched a district pattern until dots were stripped first; `C.D. Cal.` then still missed because the district-letter class omitted Central. | `deliver/dataset.py:_state_in` |
| **govinfo case numbers embed their own case type** (`-cv-` vs `-cr-`), so civil-only filtering costs zero extra requests — the alternative is one metadata call per package at ~2,000/day. | `connectors/govinfo.py` |

### robots.txt determinations

Three hosts 403 their robots.txt. They are **not** all the same case, and the
distinction is recorded per source in `compliance/registry.py` via
`SourcePolicy.robots_unavailable`:

- **Kroll, NC Business Court → PROHIBITED.** 403 on robots.txt *and* on the
  content. Both refuse. Treated as refusal of consent.
- **`efts.sec.gov`, `courtlistener.com` → allowed.** robots.txt 403s (CDN
  artifact) but the content endpoints return 200 to the same identified
  client, *and* both operators publish written policies governing programmatic
  access. CourtListener is the stronger case: they publish a documented REST
  API with per-tier rate limits, which is an affirmative invitation.

**NY UCS is permanently PROHIBITED** on quoted terms — its bot clause is
unconditional ("for any use"), so the research purpose does not reach it. The
site returns HTTP 200 to bots, which makes it a trap: technical accessibility
is not permission. Email ingestion (Phase 6) is the only lawful path.

---

## Venue coverage — the honesty layer

`litfin coverage` reports, of 200 federal district and bankruptcy courts:

- **118** publish a full PACER RSS feed (high confidence)
- **55** publish a partial feed — orders/opinions only, so routine docket
  activity never appears
- **15** publish nothing at all

This exists so an empty venue is never mistaken for a quiet venue. The
dashboard must surface it alongside results.

---

## Known gaps

**Delaware Court of Chancery** — paywalled behind File & ServeXpress; the most
important commercial venue in the country and unavailable free at any purpose
level. **California entirely** — 58 counties, 58 systems, no statewide search.
**Most state commercial courts** outside NY (email-only) and Delaware Superior.
And within RECAP, coverage is contribution-driven rather than comprehensive.

---

## Layout

```
src/litfin/
  compliance/   status.py registry.py     <- every legal determination, in git
  net/          client.py ratelimit.py robots.py httpcache.py breaker.py budget.py
  connectors/   base.py rss.py feeds.py doj_cases.py sec_fts.py
                courtlistener.py coverage.py
  extract/      schema.py prompts.py runner.py    <- Opus layer
  score/        taxonomy.py exclude.py scoring.py
  store/        db.py artifacts.py schema.sql
  canary/       framework.py
  runner/       orchestrator.py
tests/          152 tests + captured fixtures
```

`connectors/cl_alerts.py` (docket alert subscribe/manage) and
`net/webhook.py` (receiver + out-of-band drain) are the Phase 3 monitoring
half.

Added this session:

```
src/litfin/
  deliver/      dataset.py dashboard.py digest.py mailer.py server.py
  connectors/   etrack_email.py
                claims/routing.py claims/agents.toml claims/stretto.py
tests/          test_phase5.py test_phase6.py test_phase7.py test_deliver.py
                fixtures/claims/*.html   <- the four courts, captured live
                fixtures/stretto/case_list.json
                fixtures/etrack/*.eml
```

Not built, and now for a recorded reason rather than an unanswered question:
`stanford_ssc`, `de_courtconnect`, and every claims-agent crawler except
Stretto. Each carries its verdict and its verbatim clause in the registry.
