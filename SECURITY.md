# Security and operating notes

## Reporting

Open a private security advisory on the repository. Please do not file a
public issue for anything exploitable.

## What this software can do if misconfigured

Three capabilities are worth knowing about before you host it anywhere.

**It can spend money.** The extraction stage calls the Anthropic API. The
`extract` action is reachable from the control panel. Cost is bounded by
`extract.max_candidates_per_day` in `litfin.toml` and by the pre-LLM screens,
but a wide crawl plus a raised cap is a real bill.

**It can send email.** `deliver/mailer.py` defaults to `dry_run=True` on the
function signature, and a live send additionally requires
`deliver.send_enabled = true` AND every recipient present in
`deliver.recipient_allowlist`. Both conditions are checked before a socket
opens. The control panel has no live-send button at all.

**It fetches from third-party sites under a declared purpose.** See below.

## `litfin serve` is loopback-only by default, and that default is load-bearing

The control panel binds `127.0.0.1`. On your own machine that is the whole
security model, plus a per-start CSRF token because loopback is *not* a
security boundary — any page in your browser can POST to `127.0.0.1:8788`.

Binding anywhere else requires, and refuses to start without:

- `LITFIN_WEB_USER` and `LITFIN_WEB_PASSWORD`
- `LITFIN_SESSION_SECRET`
- HTTPS terminated in front of it (the app does not terminate TLS itself and
  will warn if it cannot detect a proxy)

Run `litfin preflight` before exposing it. It checks these and several
compliance conditions, and exits non-zero when any of them fail.

Consider `litfin serve --read-only` for a hosted instance: it serves the
dashboard and the export and removes every action that spends money or
fetches.

## The compliance gate is a real control, not documentation

`purpose` in `litfin.toml` is read on every outbound request.

`RESEARCH_ONLY` sources — currently CourtListener/RECAP — are enabled when
purpose is `"research"` and **raise** otherwise. Free Law Project permits
"personal, educational, research, journalistic, and exploratory use" but bars
building "tools for for-profit or non-profit organizations, even if those
tools aren't sold."

**Hosting this for other people is very plausibly outside that permission.**
A personal research project clears the clause; the same code running as firm
infrastructure does not obviously clear it. That question is answered by an
email to Free Law Project's partnerships team, not by a config value.
`litfin preflight` will not let a hosted deployment start until somebody has
recorded an answer in `[deployment].courtlistener_scope_resolved`.

`PROHIBITED` sources have no configuration escape hatch by design. Do not add
one.

## Secrets

`.env` is gitignored and holds every credential. `litfin.toml` is meant to be
committed and holds none. Existing environment variables always win over the
file, so a shell, CI, or secrets-manager value is never overridden by
something stale on disk.

Rotate `ANTHROPIC_API_KEY` if it has ever appeared in a log, a transcript, or
a screenshot.

## Data

Collected data lives at `paths.data_root` (default `C:\LitFinData`),
deliberately outside the repository. It contains case analysis, damages
estimates, and deal commentary about named parties in real litigation. Treat
an exported dashboard or `.xlsx` as confidential work product; neither is
redacted and both name real cases and real people.
