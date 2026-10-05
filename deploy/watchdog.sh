#!/usr/bin/env bash
# Keeps the stack up without anyone watching (docs/DEPLOY.md, «Watchdog»). Run by cron every 5 minutes:
#
#   */5 * * * * bash /opt/nassakh/deploy/watchdog.sh
#
# Docker already restarts a container that exits; this also brings back a service that is missing or stopped and
# restarts one that is running but reports `unhealthy` (a hung worker, a stuck gunicorn). It never touches a service
# that is healthy or still starting, and it writes only when it acts (backups/watchdog.log, kept small).

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

LOG="backups/watchdog.log"
SERVICES=(postgres redis web worker gpu-worker mcp caddy)
SITE="${SITE_ADDRESS:-nassakh.tech}"

mkdir -p backups
# one run at a time
exec 9> /tmp/nassakh-watchdog.lock
flock -n 9 || exit 0
# a log that never grows past about 1 MB
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 1048576 ]; then
    tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
note() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }

for svc in "${SERVICES[@]}"; do
    cid=$(docker compose ps -a -q "$svc" 2> /dev/null | head -n 1)
    if [ -z "$cid" ]; then
        note "$svc: no container, starting it"
        docker compose up -d --no-build "$svc" >> "$LOG" 2>&1
        continue
    fi
    state=$(docker inspect -f '{{.State.Status}}' "$cid" 2> /dev/null || echo unknown)
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2> /dev/null || true)
    if [ "$state" != "running" ]; then
        note "$svc: $state, starting it"
        docker compose up -d --no-build "$svc" >> "$LOG" 2>&1
    elif [ "$health" = "unhealthy" ]; then
        note "$svc: unhealthy, restarting it"
        docker compose restart "$svc" >> "$LOG" 2>&1
    fi
done

# the public address, from this server: written down when it fails, so a problem outside the containers
# (DNS, the certificate, the firewall) shows in the log too
code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "https://${SITE}/healthz" 2> /dev/null || true)
if [ "$code" != "200" ]; then
    note "https://${SITE}/healthz answered '${code:-no reply}'"
fi
exit 0
