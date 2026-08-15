# Hosting the full pipeline on Render

Render can genuinely run this, and there's a real reason to want it: the daily
schedule and the Tuesday digest stop depending on your laptop being awake.

Three things change from the local setup. One is architectural, one costs
money, and one is an operational risk nobody can design away.

## 1. One service, not four

**A Render persistent disk attaches to exactly one service.** The
docker-compose layout runs `scheduler` / `web` / `webhook` as separate
containers sharing a named volume — that does not map onto Render at all.
Everything touching `/data` has to be one process tree.

`deploy/render.yaml` does that: cron and the web panel in a single service.
That's fine here — the corpus is ~15 MB and there is exactly one writer, which
is also what SQLite in WAL mode wants. The blueprint pins `numInstances: 1`
for the same reason.

**Consequence:** the CourtListener webhook receiver can't be a second service,
because it also needs the database. It's parked for now — docket alerts are
already blocked on the missing API token, so nothing is lost today. If you
want it later it has to be folded into the same server as a route.

## 2. The free tier will not work, and it fails like success

Free Render instances have **no persistent disk** and **spin down when idle**.
The service would come up, serve the dashboard, and then quietly lose the
corpus — every watermark, every raw artifact, and the extraction spend they
represent — on the first idle timeout. Spin-down also stops cron, so the
schedule silently never fires.

You need a paid instance plus a disk. Check Render's current pricing yourself;
the blueprint asks for `plan: starter` and a 1 GB disk, which is generous for
15 MB of data.

## 3. Datacenter IPs get blocked more often than home ones

This one is worth going in with your eyes open. Several sources already refuse
an identified client from a residential address — Stanford, Omni and Kroll all
403 us today — and SEC and FTC both run WAFs that were fussy enough about the
User-Agent to need measuring. Cloud egress ranges are widely known and more
aggressively filtered.

If a source starts refusing from Render, **the compliance layer will read that
403 as refusal of consent, which is correct** — and coverage shrinks. The
canary catches it (a source going to zero rows is BROKEN, not quiet), so you
will know. But the honest expectation is that hosted collection may see fewer
sources than local collection does, for reasons that have nothing to do with
behaving well.

Mitigation that's worth doing regardless: **get a CourtListener API token.**
An authenticated, identified client is treated very differently from an
anonymous one, and it raises your rate ceiling.

## What does not change

**The compliance determination still holds.** The clause turns on *who* the
tool serves and for what — not on where the socket opens. Hosting the
collection on Render was contemplated when the determination was written and
is noted in it. `litfin preflight` runs at container start and refuses to boot
if that stops being true.

## Deploying

**1. Push the blueprint** (already in the repo at `deploy/render.yaml`).

**2. Render dashboard → New → Blueprint → point at `SMster/litfin`.**
It reads `render.yaml`, creates the service and prompts for every secret
marked `sync: false`. `LITFIN_WEB_PASSWORD` and `LITFIN_SESSION_SECRET` are
generated for you — copy the password out of the dashboard, it's how you sign
in.

Fill in at minimum:

| Variable | |
|---|---|
| `ANTHROPIC_API_KEY` | extraction fails without it |
| `LITFIN_CONTACT_EMAIL` | a real mailbox; preflight blocks on a placeholder |
| `LITFIN_WEB_USER` | your login name |
| `LITFIN_RECIPIENTS` | digest allowlist |
| `COURTLISTENER_TOKEN` | optional but strongly recommended |

**3. Watch the first deploy.** The entrypoint runs `litfin preflight` and
**exits non-zero rather than starting misconfigured**, so any FAIL shows up in
the deploy log with its fix. A hosted deployment that boots successfully looks
exactly like a working one, which is why this is a hard gate.

**4. Seed the corpus** — optional but saves a week of warm-up. The disk starts
empty and the schedule will fill it, but you already have 2,190 items locally.
Render Shell (paid plans) or a one-off job can copy `litfin.db` up.

**5. Sign in** at `https://litfin.onrender.com`. Render terminates TLS, so the
`Secure` session cookie works. The panel is `--read-only`: `run`, `extract`
and `collect` are refused **by the server**, not merely hidden — the schedule
does those, and a money-spending button on the public internet is a bad idea
even behind a password.

Flip `LITFIN_WEB_READ_ONLY=false` if you want them back. Think about it first.

## Turning on the Tuesday email

Same last mile as before, now that a machine is actually up at 07:30 on a
Tuesday:

1. SMTP settings in Render's Environment tab (Gmail needs an
   [App Password](https://myaccount.google.com/apppasswords))
2. `litfin digest --send --to you@example.com` from the Render Shell as a test
3. Only then set `deliver.send_enabled = true` and redeploy

Until step 3 the job fails weekly and says why in `/data/logs/digest.log`.
Deliberate: a digest that silently stops sending is indistinguishable from a
quiet week.

## Backups

The disk is the only copy. Render snapshots disks on paid plans, but pull your
own periodically — the corpus represents real extraction spend.

## If you'd rather not pay yet

`deploy/HOSTING-DASHBOARD.md` is still there: Cloudflare Pages + Access, free,
collection stays local. Same dashboard, no server, nothing to lose.
