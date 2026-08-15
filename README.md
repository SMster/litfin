# LitFin — De-Risked Case Sourcing Pipeline

A **research project** that surfaces litigation-finance prospects: cases where a
judgment has been entered or a settlement reached, in commercial litigation,
antitrust, and bankruptcy — excluding IP, international arbitration, and
consumer.

Design rationale and every measured finding are recorded inline in the code,
and the running state is in `HANDOFF.md`.

## Status

**All phases (0–7) are complete and verified against live data.** 532 tests
pass; ~2,190 items across 12 sources; 193 documents extracted by Opus and
**131 distinct matters** ranked. See `HANDOFF.md` for full session state,
blockers, and next steps.

| Phase | What | State |
|---|---|---|
| 0 | HTTP layer, compliance gate, storage, canary, runner | done |
| 1 | SEC Litigation Releases, DOJ Antitrust, FTC | done |
| 2 | DOJ Tunney Act filings, EDGAR full-text search, taxonomy, exclusion screen, Opus extraction, scoring | done |
| 3 | CourtListener/RECAP search, venue coverage, docket alerts, webhook receiver | done† |
| 4 | govinfo opinion index, EDGAR daily index, state AG feeds, JPML | done‡ |
| — | **`deliver/`: dashboard, digest, mailer, local control panel** | **done** |
| 5 | Claims-agent routing table + chapter 11 census | done§ |
| 6 | NY eTrack email ingestion | built, **disabled** ¶ |
| 7 | Tier B sources, each gated on its own ToS review | reviewed — **1 of 8 cleared** ‖ |

§ Three of the four courts publish a fetchable assignment list. **D. Del. does
not**: its list lives on `media.deb.uscourts.gov`, whose robots.txt disallows
it. Delaware is the most valuable of the four, which is exactly why the rule
matters — we take its vendor directory and report the refusal instead of
routing around it.

¶ Built and tested, off by default, and it takes **two** switches to turn on:
`[etrack].enabled` (operational) and `[etrack].decision_recorded` (somebody's
name against the unresolved "may not be mined" question). The parser has not
been calibrated against a real alert — save one and run
`litfin etrack --check alert.eml` first.

‖ **Every Tier B source's terms were read. Seven of eight said no.** The plan
expected these reviews to clear under a research purpose; they did not, and
the verbatim clauses are in `compliance/registry.py`.

| Source | Outcome | Why |
|---|---|---|
| **claims_stretto** | ✅ **cleared** | no general automated-access clause; its only scraping bar is scoped to its AI assistant |
| stanford_ssc | ❌ prohibited | 403s an identified client on its homepage *and* its disclaimer |
| claims_epiq | ❌ prohibited | *"Any use of direct or indirect automated searches or data queries … is strictly prohibited without Epiq's prior written consent."* |
| claims_angeion | ❌ prohibited | *"Use any robot, spider or other automatic device … for any purpose"* + personal/non-commercial only |
| claims_omni | ❌ prohibited | 403s an identified client |
| claims_verita | ⏸ unverified | broad reproduction bar, no automated-access clause — a human judgment, not one to infer |
| claims_bmc | ⏸ unverified | publishes no terms of use at all; absence of terms is not permission |
| de_courtconnect | ⏸ unverified | click-through disclaimer — accepting it is a legal act the pipeline will not perform |

`claims_kroll` and `nc_business_court` remain prohibited and were not
re-examined: they require affirmative written permission that does not exist.

Two of these reversed an earlier expectation, and both reversals are worth
keeping. Stanford was named the *easiest* Tier B win because its robots.txt is
permissive — and the server 403s us anyway. Omni's robots.txt carries a
Content-Signal reading `use=reference` (yes) — and the server 403s us anyway.
**When robots says yes and the server says no, the server wins.** A permissive
robots file is a statement about crawlers, not consent.

The reviews were done with a new command the plan had specified and nobody had
built:

```bash
.venv/Scripts/python.exe -m litfin.cli compliance review claims_verita --fetch
```

It fetches a source's terms so they can be read, and it is narrow on purpose:
the URL must be one of that source's own declared `tos_urls` (a literal
membership test, not a pattern), `PROHIBITED` sources still refuse, and
robots/rate limits/budget all still apply. It exists because the gate is
otherwise circular — a source stays unverified until someone reads its terms,
and its terms cannot be fetched because it is unverified. Reading a terms page
to decide whether you may use a site is not the same act as harvesting it.
`--record` prints the registry diff to commit; it never edits the registry
itself, because a determination should arrive as a reviewed commit.

† Docket alerts are built but need a `COURTLISTENER_TOKEN` (the endpoint
requires auth). The webhook receiver is built and tested locally — 4.6 ms
response, well inside the 1-second contract — but registering it needs a
publicly reachable URL. See `HANDOFF.md`.

‡ Two of the four Phase 4 sources are deliberately **indexes**, not event
sources: `sec_daily_index` has no document text and `govinfo` has only case
names, so neither carries event language and both are correctly dropped by the
screen. Their value is joins — govinfo answers "does this case have a
published opinion?", and the daily index gives the honest denominator (903 of
11,140 filings on 2026-08-14 were 8-K/10-Q/10-K, against ~200 that phrase
search found).

## Pipeline

```bash
.venv/Scripts/python.exe -m litfin.cli run       # fetch + parse + store
```
```bash
.venv/Scripts/python.exe -m litfin.cli screen    # dry-run the screens, no API cost
```
```bash
.venv/Scripts/python.exe -m litfin.cli extract   # Opus extraction via Batches API
```
```bash
.venv/Scripts/python.exe -m litfin.cli rank      # score and print the top list
```
```bash
.venv/Scripts/python.exe -m litfin.cli coverage  # which venues you can trust an empty result from
```
```bash
.venv/Scripts/python.exe -m litfin.cli alerts --subscribe   # monitor discovered dockets
```
```bash
.venv/Scripts/python.exe -m litfin.cli webhook              # receive pushed docket activity
```

`screen` is free and calls nothing — use it to see what would be sent for
extraction and why.

## Looking at the results

```bash
.venv/Scripts/python.exe -m litfin.cli serve
```

The local control panel: the dashboard plus buttons for screen / re-rank /
re-weight / regenerate. **Loopback only** — it binds `127.0.0.1` and there is
no `--host` flag, because this process can rescore a prospect list and spend
money on an API. Mutating requests carry a per-start CSRF token, since
loopback is not a security boundary: any page in your browser can POST to
`http://127.0.0.1:8788`. The two actions that cost something (`run` spends
request budget, `extract` costs money) need a typed confirmation.

```bash
.venv/Scripts/python.exe -m litfin.cli dashboard --open
```

Writes one self-contained HTML file — no CDN, no build step — sortable and
filterable on every column, with expandable rows and saved filters. It renders
from a desktop shortcut on a laptop with no internet, and a dated copy lands
in `runs/` so a past ranking stays reviewable.

```bash
.venv/Scripts/python.exe -m litfin.cli digest
```

Renders the top-20 email to `runs/<date>/digest.html` and **sends nothing**.

```bash
.venv/Scripts/python.exe -m litfin.cli export --open
```

Writes a formatted `.xlsx` — frozen header, autofilter, sized columns,
hyperlinked sources, plus *Venue coverage* and *Sources* sheets. Also
available as a download button on the local server.

**The Damages column holds STATED figures only.** An imputed figure is never
written into a numeric cell: a spreadsheet gets sorted, summed and mailed
onward, detached from every caveat the dashboard puts around a number, and a
thesis prior that ends up in a SUM() is a fabricated total. Those rows are
blank there and marked `Not stated` in the claim-size band.

```bash
.venv/Scripts/python.exe -m litfin.cli claims
```

The chapter 11 claims-agent census and the vendor routing table (`--table`).

```bash
.venv/Scripts/python.exe -m litfin.cli etrack
```

NY eTrack enrollment worklist and gate status. `--check <file.eml>` parses one
saved alert and prints exactly what it extracted.

### Reading the table

Every row carries a **plain-English description** composed from the structured
fields — "The parties reached a settlement in an antitrust matter in S.D.N.Y.
Stated amount $400M. It could lead to follow-on antitrust damages. Defendant
is a public company." — so the table is scannable without decoding
`antitrust_followon / settlement_reached`. It is deterministic rather than
model-written, which means it is always present (the LLM summary is empty on
a fair number of rows) and it can be tested. The exact classification tags and
the model's own summary are one click away in the expanded row.

**Claim size** filters by band, with live counts:

```
Under $10M 0 | $10M–$50M 1 | $50M–$250M 5 | $250M–$1B 1 | $1B and up 0 | Not stated 93
```

Each count shows what checking that box would return *under the other filters*
— not what it returns given that it is currently unchecked. **Not stated is a
band, not a hidden exclusion.** On this corpus it is 93% of rows, which is a
fact about free sources worth seeing rather than burying.

**Jurisdiction** is normalized before it reaches the dropdown. The model
returns whatever the document said — `federal`, `S.D.N.Y.`, `New York`,
`U.S. District Court` — which made the raw list a pile of near-duplicates that
filtered almost nothing. Now it is a coarse Federal/State selector plus a
detail list scoped to it, so picking *State* does not leave you scrolling past
thirty federal districts. A bare state name with no court marker is labelled
`New York (court unclear)` rather than guessed into a bucket.

### One matter, one row

A third of the first real ranked list was the same matters repeated — DOJ
publishes a press release *and* a case-filing page for one consent decree,
RECAP carries four docket entries from one week of a bankruptcy. `rank` now
clusters them: **150 scored documents → 125 distinct matters**, with the
highest-scoring document representing each one and the rest listed under
*Also reported by* in the expanded row. Nothing is deleted; `top_prospects`
just filters to primaries, and every absorbed document is still queryable.

Clustering runs at rank time rather than ingestion, because two documents
about one matter are two real observations and collapsing them in storage
would lose which source saw what.

**The over-merge risk is what shapes the matcher.** A missed merge costs a
duplicate row; a wrong merge deletes a matter, and a deleted matter is
invisible. In the live corpus nine *unrelated* items shared an empty caption
— a consent decree, an HSR annual report, a speech — so a row only joins a
cluster when it has a key worth trusting, and everything else stands alone.
An `X, et al.` caption folds into one that names every co-defendant only when
the shorter caption carries an explicit "et al.", exactly one candidate
extends it, and the extension lands on a token boundary.

### The columns

| Column | What it is |
|---|---|
| Case | caption plus the plain-English description |
| Summary | the model's own summary, with the short venue under it |
| Stage | where the matter stands — read off the procedural posture, or inferred from the event type and marked when so |
| Court | the specific court |
| Law firms | plaintiff- and defendant-side counsel |
| Claim size | stated figure and its band |
| Jurisdiction | normalized Federal/State + state |

**Stage** comes from `procedural_posture` where that field is specific and
falls back to `event_type` otherwise — `event_type` says what just happened,
`procedural_posture` says where the case stands, and the second is the
question a triager actually asks. An inferred stage renders dashed and says so
on hover. Sorting the column runs by **proximity to a fundable claim**, not
chronology: *Closed* is the latest thing that can happen to a case and the
least interesting, so it sorts near the bottom, and *On appeal* sits below an
entered judgment because an appeal re-opens risk the judgment had closed.

**Law firms is mostly empty, and that is the honest answer.** Agency press
releases almost never name counsel and RECAP docket-entry snippets often
don't either; on the current corpus 8 of 131 matters carry a named firm, most
of them government litigators. A row shows `not named` when the document
named none, and `not captured` when it was extracted before counsel capture
existed — only the second is fixable, by `litfin extract --refresh`.

### The three things the dashboard refuses to let you misread

1. **Imputed damages never enter a dollar band.** A figure inferred from a
   thesis prior lands in *Not stated*, so filtering to ">$50M" can never
   return a row whose number was invented. Ranking on a prior is legitimate;
   letting that prior answer a question about stated amounts is not.
2. **The venue coverage map is on the same page as the results**, and on its
   own sheet in the Excel export. A court with no feed produces no rows
   whether or not anything happened in it.
3. **The funnel counts are in the header.** "0 prospects" is ambiguous between
   nothing collected, everything screened out, and extraction never run —
   three states needing three different responses.

### The send gate

`mailer.py` defaults to `dry_run=True` **on the function signature**, not in
config where it could be absent or overwritten. A live send additionally
requires *both*:

```toml
[deliver]
send_enabled = true
recipient_allowlist = ["you@example.com"]
```

Two independent conditions, because each catches a different mistake:
`send_enabled` catches "I did not mean to send anything at all", the allowlist
catches "I meant to send, but not there." Recipients are compared on the bare
address, so a display name cannot smuggle one past the list. A refusal
**raises** — it does not warn and continue, and it does not silently fall back
to dry-run, because a scheduled job that quietly stops sending looks exactly
like a quiet day. The local server has no live-send button at all.

`coverage` reports, of 200 federal district and bankruptcy courts: 118 publish
a full PACER RSS feed, 55 publish only orders/opinions, and **15 publish
nothing at all**. It exists so an empty venue is never read as a quiet venue.

`rank` recomputes entirely from stored extractions, so tuning weights costs
seconds and zero API calls. Weights and per-event fit are editable in
`litfin.toml` under `[score.weights]` and `[score.event_fit]`.

**Scoring is weighted toward near and post settlement.** A settled case has an
agreed number and a payer who has already decided not to fight; a judgment has
a number the loser did not agree to and still faces post-trial motions,
appeal, and collection. So the ordering runs approved settlement (1.00) >
preliminary approval (0.97) > settlement reached (0.95) > judgment entered
(0.88) > jury verdict (0.82) — a verdict sits *below* an entered judgment
because it is the most appealable moment in a case's life. Raise
`judgment_entered` above `settlement_final_approval` in `litfin.toml` to get
the litigator's ordering back.

## Quick start

```bash
.venv/Scripts/python.exe -m litfin.cli run
```

```bash
.venv/Scripts/python.exe -m litfin.cli status
```

```bash
.venv/Scripts/python.exe -m litfin.cli compliance
```

`run` exits non-zero when any source is BROKEN, so a scheduled task surfaces
problems instead of swallowing them.

## The two things to understand before changing anything

### 1. `purpose` in `litfin.toml` is load-bearing

```toml
purpose = "research"
```

This is not a label. The compliance layer reads it on every fetch.
CourtListener/RECAP is marked `RESEARCH_ONLY` because Free Law Project permits
"personal, educational, research, journalistic, and exploratory use" but bars
building "tools for for-profit or non-profit organizations." Flip this to
`"commercial"` and that source **raises** rather than quietly continuing under
terms that no longer apply.

There is one open question you should resolve before leaning on CourtListener:
a personal research project clears their scope clause; the same code running as
firm infrastructure does not. One email to FLP's partnerships team settles it.

### 2. `parse()` is pure over bytes

```python
def parse(self, raw: bytes, url: str) -> ParseResult: ...   # no I/O, no clock, no watermark
```

Fixture replay, canaries, and historical replay all fall out of that purity.
Watermark filtering happens *outside* `parse()`, in the runner — which is what
makes the `rows_parsed` vs `rows_new` comparison possible. A parser that filters
internally silently breaks the canary system.

## How silent failure is prevented

| `rows_parsed` | `rows_new` | verdict |
|---|---|---|
| 0 | 0 | **BROKEN** — selector/schema changed |
| >0 | 0 | HEALTHY — the normal quiet day |
| >0 | >0 | HEALTHY — new items |
| 0, HTTP 304, no body | — | HEALTHY — nothing to parse |
| 0, HTTP 304, **cached body** | — | **BROKEN** — parser regression hiding behind the cache |
| 0, server reported count 0 | — | HEALTHY — affirmative "no news" (a Saturday) |

The cached-body row was a real bug found by an end-to-end test: on a 304 we
serve and parse the *cached* body, which is known-good, so zero rows means the
parser broke — not that nothing changed.

There is also a third state, **PARTIAL COVERAGE**: rows truncated by a hard API
page cap that cannot be paginated affordably. Rows are *kept*, the slice is
flagged, and a `## PARTIAL COVERAGE` section appears in the run report.
Deliberately not BROKEN — that would discard good rows and freeze the watermark
forever, since the condition recurs every run.

A BROKEN task deliberately **does not advance the watermark**, so once the parser
is fixed the missed data is re-read rather than skipped forever.

## Measured findings encoded in the code

These came from probing live hosts; each is recorded where it is enforced.

- **Never put a URL or an HTTP library token in the User-Agent.** Both
  `sec.gov` and `ftc.gov` return 403 on either, independent of domain. Name +
  contact email returns 200. (`config.py:Identity.user_agent`)
- **Rate-limit keys are not hostnames.** SEC's cap applies to the requester
  across `www.`/`efts.`/`data.sec.gov`; per-hostname buckets would let three
  connectors each run at the cap and get the IP banned. (`net/ratelimit.py`)
- **SEC's feed `guid` is an opaque UUID.** `dc:creator` carries the monotonic
  `LR-#####` number and is the stable key. Its `<link>` values also carry a
  trailing newline that 404s unstripped. (`connectors/feeds.py`, `connectors/rss.py`)
- **DOJ publishes one parameterized feed, not three.** `field_component=376`
  selects the Antitrust Division.
- **NY UCS bars bot access "for any use."** Permanently `PROHIBITED`; the site
  returning HTTP 200 to bots makes it a trap, not an invitation. Email
  ingestion is the only lawful path.
- **Kroll and NC Business Court 403 identified clients.** Treated as refusal of
  consent. Do not work around a WAF with a headless browser.
- **`efts.sec.gov` 403s its robots.txt but serves its API.** Distinguished from
  the Kroll pattern and recorded as an explicit per-source determination
  (`SourcePolicy.robots_unavailable`): the content endpoint returns 200, SEC
  publishes a written fair-access policy, and www.sec.gov's robots
  affirmatively allows `/Archives/edgar/data`.
- **EDGAR truncates twice, both silently.** 10,000 results is the ceiling
  (`relation: "gte"`) and 100 is the per-page cap. A combined-form query
  reported 104 and returned 100 — four filings lost behind a valid 200.
  Slicing by day *and* form keeps every slice under both; two canaries back
  it up.
- **`justice.gov` 403s `?page=1`** while page 0 serves fine. Paginated crawling
  of that view is refused, so only page 0 is planned.
- **A Competitive Impact Statement is definitionally antitrust** — it exists
  only under the Tunney Act — but its title carries no antitrust keyword, so
  it was being classified `post_settlement` until the document type itself was
  added to the antitrust patterns.
- **"Proposed Jury Verdict" is a blank trial-prep form, not a verdict** — the
  same substring trap as "proposed final judgment". Found only by running live
  extraction: Opus returned `no_event` for four straight candidates the regex
  had scored 0.9.
- **CourtListener search caps at 20/page and ignores `page_size`.** Cursor-
  walking is unaffordable at 50 requests/hour, so slices are per-day and any
  residual truncation is reported as partial coverage rather than silently
  dropped.
- **CourtListener webhooks carry no HMAC signature.** The only available
  authentication is an IP allowlist (`34.210.230.218`, `54.189.59.91`) plus a
  long random secret URL path, compared in constant time.
- **EDGAR's `.idx` header wraps across two physical lines** (`Form Type /
  Company Name / CIK`, then `Date Filed / File Name`). Deriving column offsets
  from "the header row" captures three of five fields and yields zero rows;
  rows are matched by shape instead.
- **JPML's MDL list is a dated monthly PDF.** Neither `/pending-mdls` nor
  `/pending-mdls-0` contains any MDL data — both return 200 as navigation
  pages. The connector discovers the PDF URL on one run and fetches it the
  next, which is why `plan()` now actually receives a watermark.
- **State AG coverage is 7 of 50 states.** NY 403s an identified client and TX
  404s everywhere, so the two highest-value states are dark. Every failed
  probe is recorded in `connectors/state_ag_feeds.toml` so nobody re-probes
  them blindly.
- **The four claims-agent courts publish in four different shapes, and their
  columns do not agree.** `ohsb` and `nysb` are (case, debtor, agent, date);
  `njb` is (case+judge, vicinage, title, agent) with **no date column at
  all**. A positional parser writes "Newark" into the debtor field and looks
  like it worked, so columns are resolved by header text and a missing
  required header fails the canary. (`connectors/claims/routing.py`)
- **`media.deb.uscourts.gov` disallows its robots.txt** — and it hosts
  Delaware's claims-agent assignment list, the most valuable of the four.
  Treated as refusal. The landing page is still polled, so if the court ever
  moves the list somewhere fetchable the canary says so.
- **eTrack's HTML alerts were unparseable via `text_content()`.** lxml
  concatenates block elements with no separator, so
  `<p>Index Number: 651234/2026</p><p>Case Name: …</p>` collapses to
  `…2026Case Name: …`. That defeats both the line-anchored field regex and the
  `\b` ending the index pattern, so an HTML alert yielded *nothing* while
  looking like a clean parse. Block tags are converted to newlines first.
  (`connectors/etrack_email.py:_html_to_text`)
- **A whole-key `replace("no", "number")` corrupted every label containing
  "no"** — "notification type" became "numbertification type". Label aliases
  are now whole-key lookups. (`connectors/etrack_email.py:_LABEL_ALIASES`)
- **Stretto's case endpoint returns `recordsTotal: 2992` with an EMPTY `data`
  array if the DataTables `columns[...]` parameters are omitted.** HTTP 200,
  valid JSON, a plausible total, and no rows — indistinguishable from a quiet
  week to anything checking only the status code. The canary asserts rows
  *against* a non-zero total rather than trusting either number alone.
  (`connectors/claims/stretto.py:canary`)
- **`recordsTotal` and `data` do not measure the same thing.** The total
  counts every debtor row (2,992); `data` carries only lead debtors (369) with
  affiliates nested. Comparing them would report permanent partial coverage.
- **A permissive robots.txt is not consent.** Stanford and Omni both allow our
  agent in robots and both 403 it at the server. Where the two disagree, the
  server is the answer.
- **A dollar figure in a case NAME is not a claim.** `United States v.
  Approximately 225,364,961 USDT` ranked five times on a "damages" figure that
  is the seized amount in its own caption. Government in rem forfeiture has no
  assignable claim and no counterparty to collect from, so it is excluded —
  but on the in rem caption convention and explicit forfeiture statutes only,
  never on the bare word "forfeiture", which is ordinary commercial language
  (deposits, unvested equity, leases). (`score/exclude.py:FORFEITURE_PATTERNS`)
- **An HTTP handler that rejects without reading the request body sends an
  RST, not a FIN.** The webhook receiver's 403/404 paths answered without
  draining, so on Windows the caller saw a connection reset instead of the
  status the server actually sent. CourtListener counts a reset toward the 8
  consecutive failures that auto-disable an endpoint. (`net/webhook.py:_drain_body`)
- **`CREATE TABLE IF NOT EXISTS` never adds a column to an existing table**, so
  a new column in schema.sql silently never reaches a live database. Additive
  migrations run explicitly, and an index on a migrated column cannot live in
  the schema script at all. (`store/db.py:_migrate`)

## Hosting it

**Start with the dashboard, not the pipeline** — see
`deploy/HOSTING-DASHBOARD.md`.

```bash
.venv/Scripts/python.exe -m litfin.cli publish     --target litfin.pages.dev --protected-by "Cloudflare Access, 2 emails"
```

The dashboard is one self-contained HTML file, so hosting it needs no server,
no database and no credentials on the host — and the hosted box **fetches
nothing**, so no source's terms are engaged by it at all. Collection stays on
your machine, where the research purpose is unambiguous.

`publish` refuses to target a host that is public by default (GitHub Pages, a
naked S3 bucket), and refuses to run without `--protected-by` naming what
restricts access. The dashboard names real parties in real litigation and
carries damages estimates; it is confidential work product, not a web page.

### Hosting the whole pipeline

**Render** — `deploy/render.yaml` is a ready Blueprint; see
`deploy/HOSTING-RENDER.md`. Three things differ from local: a Render disk
attaches to **one** service so cron and the panel share a process tree, the
**free tier cannot work** (no persistent disk, spins down — it would lose the
corpus on the first idle timeout), and datacenter IPs get blocked more often
than residential ones, so hosted collection may see fewer sources than local.

Vercel is the wrong shape for this: serverless functions, an ephemeral
filesystem, and timeouts well under the several minutes a polite `run` takes.

**Read `SECURITY.md` and `deploy/README.md` first, and run the preflight:**

```bash
.venv/Scripts/python.exe -m litfin.cli preflight
```

It exits non-zero until the deployment is actually safe and lawful to run,
and prints exactly what is blocking. Two of its checks are not technical:

**The CourtListener scope question.** RECAP is your largest source and it is
`RESEARCH_ONLY`. Free Law Project permits *"personal, educational, research,
journalistic, and exploratory use"* but bars building *"tools for for-profit
or non-profit organizations, even if those tools aren't sold."* A research
project on a laptop clears that clause; an always-on host serving a team is
at best arguable, and no amount of code can settle it. Email
`partnerships@free.law`, then record the answer:

```toml
[deployment]
courtlistener_scope_resolved = "emailed FLP 2026-08-20, reply confirms our use is in scope"
```

The alternative is `purpose = "commercial"`, which disables CourtListener
*loudly* rather than using it under terms that may no longer apply.

**Web authentication.** `litfin serve` binds loopback and is unauthenticated
by default — correct for a laptop, unsafe anywhere else, because the panel can
spend money on the Anthropic API. Any non-loopback bind **refuses to start**
without `LITFIN_WEB_USER`, `LITFIN_WEB_PASSWORD` (16+ chars) and
`LITFIN_SESSION_SECRET`, checked before the socket is created.

```bash
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
```

Three services: a `scheduler` with full rights and no network exposure, a
`web` panel running **`--read-only`** (run/extract/collect are refused by the
*server*, not merely hidden), and an optional `webhook` receiver. Both ports
bind to host loopback — put TLS in front, since session cookies are `Secure`.

## Secrets

`.env` is gitignored and holds `ANTHROPIC_API_KEY`, `COURTLISTENER_TOKEN`, and
`LITFIN_WEBHOOK_SECRET`. Existing environment variables win over the file, so
a shell or CI value is never overridden by something stale on disk.
`litfin.toml` is meant to be committed and holds no secrets. Start from
`.env.example`.

## Two asymmetries the design is built around

**A false exclusion is invisible; a false inclusion costs one row.** The
lexical screen only fires on unambiguous signals and defers everything else to
the LLM. It deliberately does *not* exclude on the bare word "consumers" —
consumer harm is the central standard in antitrust analysis, so that would
silently drop the matters the pipeline exists to find. Every exclusion is
logged with its reason and printed by `litfin screen`.

**Missing damages must not score zero.** Most free sources never state a
figure, so a zero-impute would rank every unlabeled matter last — exactly
backwards, since the largest cases often lack a figure in the first public
document. Missing figures fall back to a thesis prior, take an uncertainty
discount, and are flagged `[IMPUTED]` in the output so you can see at a glance
which rankings rest on a real number.

## Layout

```
src/litfin/
  compliance/   status.py registry.py     <- every legal determination, in git
  net/          client.py ratelimit.py robots.py httpcache.py breaker.py
                budget.py webhook.py
  connectors/   base.py rss.py feeds.py doj_cases.py sec_fts.py
                courtlistener.py cl_alerts.py coverage.py edgar_index.py
                govinfo.py jpml.py state_ag.py etrack_email.py
                claims/routing.py claims/agents.toml claims/stretto.py
  extract/      schema.py prompts.py runner.py    <- Opus layer
  score/        taxonomy.py exclude.py scoring.py cluster.py
  deliver/      dataset.py dashboard.py digest.py mailer.py server.py
                excel.py auth.py
  deploy/       preflight.py publish.py
deploy/         Dockerfile docker-compose.yml crontab render.yaml
                README.md HOSTING-DASHBOARD.md HOSTING-RENDER.md
  store/        db.py artifacts.py schema.sql
  canary/       framework.py
  runner/       orchestrator.py
tests/          532 tests + captured fixtures
```

`deliver/dataset.py` is one assembly step feeding three renderers. The
alternative — each renderer running its own queries — guarantees they drift,
and the failure mode is the worst kind: an email that disagrees with the
dashboard about what ranked first, with no way to tell which one is lying.

Data lives at `C:\LitFinData\` — deliberately off the OneDrive-synced tree,
because OneDrive corrupts SQLite.

## Tests

```bash
.venv/Scripts/python.exe -m pytest -q
```
