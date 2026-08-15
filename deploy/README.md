# Deploying LitFin on an always-on host

Read `SECURITY.md` first. This file is the mechanics; that one is why the
mechanics are shaped the way they are.

## The two things that block a deploy, and neither is technical

`litfin preflight` exits non-zero until both are answered. Run it now:

```bash
litfin preflight
```

### 1. The CourtListener scope question

CourtListener/RECAP is your largest source and it is marked `RESEARCH_ONLY`.
Free Law Project permits *"personal, educational, research, journalistic, and
exploratory use"* but bars building *"tools for for-profit or non-profit
organizations, even if those tools aren't sold."*

A research project on a laptop clears that clause. An always-on host serving a
team is, at best, arguable — and **this is the deploy you are doing.** No
amount of code can settle it.

Email `partnerships@free.law`, describe the deployment plainly, and record
what they say:

```toml
[deployment]
courtlistener_scope_resolved = "emailed FLP 2026-08-20, reply 08-22 confirms our use is in scope — docs/tos/flp-reply.eml"
```

The field is free text because no boolean could honestly represent "we asked
and they said yes", and a boolean is exactly what somebody would flip without
asking.

**The alternative is to set `purpose = "commercial"`.** That is the honest
move if this really is firm infrastructure. The compliance gate will then
disable CourtListener *loudly* rather than continuing under terms that no
longer apply. You lose the source; you keep the ability to say what you did.

### 2. Web authentication

The panel can spend money on the Anthropic API and displays case analysis
about named parties in real litigation. Binding anything other than loopback
**refuses to start** without all three:

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"   # password
python -c "import secrets; print(secrets.token_urlsafe(32))"   # session secret
```

```
LITFIN_WEB_USER=you
LITFIN_WEB_PASSWORD=<24+ chars>
LITFIN_SESSION_SECRET=<32+ chars>
```

The check runs *before* the socket is created, because a misconfigured hosted
panel that boots successfully looks exactly like a working one.

## Bringing it up

```bash
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
```

`preflight` runs as a compose service and the others wait on it, so a
misconfigured stack fails at start rather than a week later.

Three services:

| Service | What it does | Exposure |
|---|---|---|
| `scheduler` | the pipeline on cron — full rights | none |
| `web` | dashboard + Excel export, **`--read-only`** | `127.0.0.1:8788` |
| `webhook` | CourtListener push receiver (profile `webhook`) | `127.0.0.1:8787` |

**The panel runs `--read-only` on purpose.** `run`, `extract` and `collect`
are refused *by the server*, not merely hidden from the page — a hidden button
is a UI convenience, not a control. Fetching and spending belong to the
scheduler, which nothing can reach over the network.

## TLS is not optional

Both ports bind to **loopback on the host**. Put a reverse proxy in front:

```nginx
server {
    listen 443 ssl;
    server_name litfin.example.com;
    ssl_certificate     /etc/letsencrypt/live/litfin.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/litfin.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8788;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header Host $host;
    }
}
```

Session cookies are set `Secure`, so **sign-in will not work over plain HTTP**
— that is deliberate, not a bug to work around.

The webhook endpoint is the one thing that genuinely wants to be public:

```
https://litfin.example.com/webhook/<LITFIN_WEBHOOK_SECRET>
```

Register that in the CourtListener webhooks panel. It carries no HMAC
signature, so the long random path plus the IP allowlist is the whole
authentication.

## The schedule

See `deploy/crontab`. In summary:

| When | What |
|---|---|
| daily 05:00 | collect |
| daily 05:40 / 06:40 | extract, then collect the batch |
| daily 07:00 | rank + regenerate the dashboard |
| **Mon 04:30** | weekly sources (JPML, claims agents, Stretto) |
| **Tue 07:30** | **the weekly email digest** |
| Sun 03:00 | refresh the venue coverage map |

Weekly sources run Monday so Tuesday's digest includes them.

## Turning the Tuesday email on

The schedule is already in place. The send is gated, and the last step is
yours because it needs credentials.

1. Put SMTP settings in `.env`. Gmail requires an
   [App Password](https://myaccount.google.com/apppasswords), not the account
   password:

   ```
   LITFIN_SMTP_HOST=smtp.gmail.com
   LITFIN_SMTP_PORT=587
   LITFIN_SMTP_USER=<sending account>
   LITFIN_SMTP_PASSWORD=<app password>
   LITFIN_SMTP_FROM=<sending account>
   ```

2. Send one test to yourself before arming the schedule:

   ```bash
   litfin digest --send --to you@example.com
   ```

   Add your address to `LITFIN_RECIPIENTS` first — the allowlist refuses any
   address not on it, and refuses the whole send if even one recipient is off
   it.

3. Only then set `deliver.send_enabled = true` in `litfin.toml`.

**Until step 3, the Tuesday job fails every week and says why in
`/data/logs/digest.log`.** That is intended. `--send` raises rather than
falling back to a dry run, because a digest that quietly stops sending is
indistinguishable from a quiet week.

```bash
docker compose -f deploy/docker-compose.yml logs -f scheduler
```

## Backups

Everything lives in the `litfin-data` volume: the SQLite corpus, the
watermarks, and the raw artifacts — which together represent real money in
extraction spend.

```bash
docker run --rm -v litfin_litfin-data:/data -v "$PWD:/backup" \
  alpine tar czf /backup/litfin-$(date +%F).tar.gz /data
```

A crash mid-run is safe by design — items and watermarks advance in one
transaction — but a deleted volume is not.
