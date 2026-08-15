#!/bin/sh
# Render entrypoint: preflight, then cron and the web panel in ONE process
# tree, because a Render disk attaches to exactly one service.
set -eu

mkdir -p /data/logs

# ---------------------------------------------------------------------------
# 1. Preflight. Refuse to start rather than come up misconfigured.
#
# A hosted deployment that boots successfully looks exactly like a working
# one, which is why this is a hard gate and not a warning in the logs. Render
# will show the failure and the reason in the deploy output.
# ---------------------------------------------------------------------------
echo "=== preflight ==="
if ! litfin preflight --host 0.0.0.0; then
    echo
    echo "Refusing to start. Fix the FAIL items above in the Render"
    echo "dashboard (Environment), then redeploy."
    exit 1
fi

# ---------------------------------------------------------------------------
# 2. cron does not inherit the container environment.
#
# Without this every scheduled job runs with no API key, no contact address
# and no recipients -- and fails in a way that looks like a code problem
# rather than a configuration one.
# ---------------------------------------------------------------------------
printenv | grep -E '^(LITFIN_|ANTHROPIC_|COURTLISTENER_|TZ=)' \
  | sed 's/^/export /' > /data/.cron-env
chmod 600 /data/.cron-env

{
  echo "SHELL=/bin/sh"
  echo "BASH_ENV=/data/.cron-env"
  grep -v '^SHELL=' /app/deploy/crontab
} > /tmp/litfin-crontab
crontab /tmp/litfin-crontab
cron

echo "=== schedule installed ==="
grep -E '^[0-9*]' /app/deploy/crontab | sed 's/^/  /'

# Surface scheduled-job output in Render's log stream. A scheduler whose
# output is invisible is one nobody notices has stopped.
touch /data/logs/run.log /data/logs/extract.log /data/logs/rank.log \
      /data/logs/digest.log /data/logs/coverage.log
tail -F /data/logs/*.log 2>/dev/null &

# ---------------------------------------------------------------------------
# 3. The panel, in the foreground so it is PID 1's child and Render can see
#    the port. Render terminates TLS, so Secure cookies work.
# ---------------------------------------------------------------------------
READ_ONLY=""
if [ "${LITFIN_WEB_READ_ONLY:-true}" = "true" ]; then
    READ_ONLY="--read-only"
fi

echo "=== starting panel on 0.0.0.0:${PORT:-8788} ${READ_ONLY} ==="
exec litfin serve \
    --host 0.0.0.0 \
    --port "${PORT:-8788}" \
    --no-browser \
    ${READ_ONLY}
