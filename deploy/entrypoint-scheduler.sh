#!/bin/sh
# Scheduler entrypoint: install the crontab and run cron in the foreground.
set -eu

mkdir -p /data/logs

# cron does not inherit the container environment. Without this, every
# scheduled job would run with no API key, no contact address and no
# recipients -- and would fail in a way that looks like a code problem rather
# than a config one.
printenv | grep -E '^(LITFIN_|ANTHROPIC_|COURTLISTENER_|TZ=)' \
  | sed 's/^/export /' > /data/.cron-env
chmod 600 /data/.cron-env

{
  echo "SHELL=/bin/sh"
  echo "BASH_ENV=/data/.cron-env"
  grep -v '^SHELL=' /app/deploy/crontab
} > /tmp/litfin-crontab

crontab /tmp/litfin-crontab

echo "LitFin scheduler started. Schedule:"
grep -E '^[0-9*]' /app/deploy/crontab | sed 's/^/  /'
echo
echo "Logs: /data/logs/"

# Tail the logs into the container's stdout so `docker compose logs` shows
# what the schedule is doing. A scheduler whose output is invisible is a
# scheduler nobody notices has stopped.
touch /data/logs/run.log /data/logs/extract.log /data/logs/rank.log \
      /data/logs/digest.log /data/logs/coverage.log
tail -F /data/logs/*.log &

exec cron -f
