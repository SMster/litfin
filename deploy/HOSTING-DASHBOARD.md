# Hosting the dashboard only

The deployment shape you actually want first. The dashboard is one
self-contained HTML file — sorting, filtering, the coverage map, the
expandable rows and the whole dataset are inlined — so it needs no server, no
database, and no credentials on the host.

**Why this is better than hosting the pipeline, not a lesser version of it:**

- the hosted box does **zero fetching**, so no source's terms are engaged by
  it at all
- no API key, no database, and no credential ever leaves your machine
- nothing on the host can spend money
- a static file has no attack surface worth the name

Collection keeps running locally, where the research purpose is unambiguous.

## The one rule

**This file is confidential work product.** It names real parties in real
litigation, carries damages estimates, and describes how their cases might be
monetized. It must sit behind access control.

GitHub Pages, a naked S3 bucket, and default Netlify / Vercel / Cloudflare
Pages sites are **all public**. `litfin publish` refuses the ones it can
recognize and refuses to run at all without `--protected-by`.

## Build the bundle

```bash
litfin publish --target litfin.pages.dev --protected-by "Cloudflare Access, allowlist of 2 emails"
```

Writes to `<data_root>/publish`:

| File | |
|---|---|
| `index.html` | the dashboard, self-contained |
| `litfin-prospects-<date>.xlsx` | the Excel export |
| `robots.txt`, `_headers` | noindex / nofollow / no-store |
| `manifest.json` | what it contains and what protects it |

## Cloudflare Pages + Access (free, real auth)

Access does one-time-PIN login against an email allowlist. No password to
share, no user database, and you can revoke someone in one click.

**1. Install wrangler and log in** — this is your Cloudflare account, so you
run it:

```bash
npm install -g wrangler
wrangler login
```

**2. Create the project once:**

```bash
wrangler pages project create litfin --production-branch main
```

**3. Deploy the bundle:**

```bash
wrangler pages deploy "C:\LitFinData\publish" --project-name litfin
```

**4. Lock it down — do this BEFORE sharing the URL.** In the Cloudflare
dashboard: **Zero Trust → Access → Applications → Add an application →
Self-hosted**.

- Application domain: your `*.pages.dev` hostname
- Policy: **Allow**, rule type **Emails**, listing exactly the two addresses
- Identity: **One-time PIN** is enough; no IdP needed

Confirm it works by opening the URL in a private window. **If the dashboard
renders without asking for a code, Access is not attached — stop and fix it
before sending the link to anyone.**

## Refreshing it

Collection, extraction and ranking stay local, exactly as now:

```bash
litfin run
litfin extract && litfin extract --collect
litfin rank
litfin publish --target litfin.pages.dev --protected-by "Cloudflare Access, allowlist of 2 emails"
wrangler pages deploy "C:\LitFinData\publish" --project-name litfin
```

Worth a scheduled task on your machine (Task Scheduler) rather than a server.
The last two lines are the only ones that are new.

## Alternatives

- **Tailscale Funnel / Serve** — if you both already use Tailscale, serving
  `<data_root>/publish` on your tailnet needs no third party at all.
- **Netlify** with a site-wide password — password protection is a paid
  feature; the free tier has no access control.
- **S3 + CloudFront + signed URLs** — works, but more moving parts than two
  people need.

## When you want the full pipeline hosted

`deploy/README.md` and `deploy/docker-compose.yml` are ready for it. Re-read
the CourtListener determination in `litfin.toml` first — hosting the
*collection* is the thing that determination is about, and the triggers that
would invalidate it are listed there.
