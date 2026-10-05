#!/usr/bin/env bash
# Deploy Nassakh: first deploy and every update (docs/DEPLOY.md). Run it as the deploy user, in /opt/nassakh:
#
#   bash deploy/deploy.sh           git pull, build, start, wait until every service is healthy
#   bash deploy/deploy.sh --no-pull the same without `git pull` (after `git checkout <commit>`: a rollback)
#
# Safe to run again at any time: nothing is rebuilt that did not change, and the database and the books live in
# volumes that a deploy never touches. The `init` service (migrations, collectstatic) runs once per deploy,
# before web, the workers and the MCP server start.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ENV_FILE="deploy/.env.production"
WAIT_SECONDS="${WAIT_SECONDS:-420}"
SERVICES=(postgres redis web worker gpu-worker mcp caddy)

die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '\n==> %s\n' "$*"; }

# health of one service's container: healthy, starting, unhealthy, running, exited, missing
status_of() {
    local container
    container=$(docker compose ps -q "$1" 2> /dev/null | head -n 1)
    [ -n "$container" ] || { echo "missing"; return; }
    docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2> /dev/null || echo "missing"
}

image_id() { docker image inspect -f '{{.Id}}' nassakh-app:latest 2> /dev/null || true; }

# Everything runs inside main(): bash reads the whole function before it runs, so the `git pull` below may replace
# this very file without breaking the run that is in progress.
main() {
    local pull=1 arg before after site code deadline failed pending service
    for arg in "$@"; do
        case "$arg" in
            --no-pull) pull=0 ;;
            -h | --help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; return 0 ;;
            *) echo "unknown option: $arg (use --no-pull)" >&2; return 2 ;;
        esac
    done

    # -------------------------------------------------------------- before anything
    command -v docker > /dev/null || die "docker is not installed: run deploy/server-setup.sh first"
    docker info > /dev/null 2>&1 || die "cannot talk to docker: log out and in again (the docker group), or the daemon is down"
    docker compose version > /dev/null 2>&1 || die "the Docker Compose plugin is missing: run deploy/server-setup.sh"
    [ -f "$ENV_FILE" ] || die "$ENV_FILE does not exist: cp deploy/.env.production.example $ENV_FILE and fill it (docs/DEPLOY.md, step 5)"
    if grep -qE '^[A-Z_]+=CHANGE_ME' "$ENV_FILE"; then
        grep -E '^[A-Z_]+=CHANGE_ME' "$ENV_FILE" | sed 's/=.*//' >&2
        die "the lines above in $ENV_FILE still say CHANGE_ME"
    fi
    chmod 600 "$ENV_FILE" 2> /dev/null || true
    # docker compose takes its variables from ./.env: a link to the production file
    if [ -L .env ] && [ "$(readlink .env)" = "$ENV_FILE" ]; then
        :
    elif [ -e .env ]; then
        die ".env exists and is not a link to $ENV_FILE: move it away (mv .env .env.old) and run this again"
    else
        ln -s "$ENV_FILE" .env
        echo "linked .env -> $ENV_FILE"
    fi

    # -------------------------------------------------------------- code
    if [ "$pull" = 1 ]; then
        log "git pull"
        git pull --ff-only
    else
        log "skipping git pull (--no-pull)"
    fi
    echo "commit: $(git rev-parse --short HEAD) $(git log -1 --format=%s | cut -c1-80)"

    # -------------------------------------------------------------- build and start
    before=$(image_id)
    log "build the image (the first build takes 10 to 20 minutes)"
    docker compose build

    # keep the image that ran until now, for a quick way back (docs/DEPLOY.md, rollback)
    after=$(image_id)
    if [ -n "$before" ] && [ "$before" != "$after" ]; then
        docker tag "$before" nassakh-app:previous
    fi

    # a dump of the database before the migrations run: the way back from a bad one (skipped on a first deploy)
    if [ "$(status_of postgres)" = "healthy" ]; then
        log "database backup before the update"
        bash ./deploy/backup.sh predeploy || echo "warning: the backup failed; the deploy goes on"
    fi

    log "start (init runs the migrations first)"
    # a finished init container is removed, so `up` runs init again: migrations once per deploy
    docker compose rm -f init > /dev/null 2>&1 || true
    if ! docker compose up -d --remove-orphans; then
        echo >&2
        echo "---- init (migrations, collectstatic) ----" >&2
        docker compose logs --no-color --tail 60 init >&2 || true
        die "the stack did not start: read the lines above (docker compose logs init)"
    fi

    # Caddy's config is one bind-mounted file, and `git pull` replaces a file instead of changing it, so a running
    # Caddy can keep the old contents: restart it when it differs from the file on disk.
    if ! docker compose exec -T caddy cat /etc/caddy/Caddyfile 2> /dev/null | cmp -s - deploy/Caddyfile; then
        log "Caddyfile changed: restarting caddy"
        docker compose restart caddy
    fi

    # -------------------------------------------------------------- wait for health
    log "waiting for every service to be healthy (up to ${WAIT_SECONDS} s)"
    deadline=$((SECONDS + WAIT_SECONDS))
    failed=0
    while :; do
        pending=()
        for service in "${SERVICES[@]}"; do
            [ "$(status_of "$service")" = "healthy" ] || pending+=("$service")
        done
        [ "${#pending[@]}" -eq 0 ] && break
        if [ "$SECONDS" -ge "$deadline" ]; then
            failed=1
            break
        fi
        printf '  waiting for: %s\n' "${pending[*]}"
        sleep 5
    done

    log "status"
    docker compose ps
    if [ "$failed" = 1 ]; then
        for service in "${pending[@]}"; do
            echo >&2
            echo "---- $service: $(status_of "$service") ----" >&2
            docker compose logs --no-color --tail 40 "$service" >&2 || true
        done
        die "not healthy after ${WAIT_SECONDS} s: ${pending[*]} (see docs/DEPLOY.md, troubleshooting)"
    fi

    log "last log lines"
    for service in web worker gpu-worker mcp caddy; do
        echo "---- $service ----"
        docker compose logs --no-color --tail 5 "$service" 2>&1 | cut -c1-220
    done

    # -------------------------------------------------------------- the public address (informational)
    site=$(grep -E '^SITE_ADDRESS=' "$ENV_FILE" | tail -n 1 | cut -d= -f2- | tr -d '"' || true)
    site="${site:-nassakh.tech}"
    code=$(curl -sS -o /dev/null -m 15 -w '%{http_code}' "https://${site}/healthz" 2> /dev/null || true)
    echo
    if [ "$code" = "200" ]; then
        echo "https://${site}/healthz answers 200: the site is up."
    else
        echo "https://${site}/healthz answered '${code:-no reply}'. If this is the first deploy, the certificate can take"
        echo "a minute: check 'docker compose logs caddy' and that the DNS A records point at this server."
    fi

    # drop the layers an update left behind (the previous image stays tagged)
    docker image prune -f > /dev/null 2>&1 || true
    log "done"
}

main "$@"
exit $?
