#!/usr/bin/env bash
# First-run data for a new server (docs/DEPLOY.md, step 7), after the first ./deploy/deploy.sh: the role groups,
# the first superuser from ADMIN_EMAIL / ADMIN_PASSWORD in deploy/.env.production, and the search index of the
# books that are there. Safe to run again.
#
#   bash deploy/bootstrap.sh

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
run() { docker compose exec -T web "$@"; }

docker compose ps --status running --services 2> /dev/null | grep -qx web || die "the web service is not running: run: bash deploy/deploy.sh first"

echo "==> role groups (admin, editor, proofreader)"
run python manage.py seed_groups

echo "==> first superuser"
if run python deploy/create_admin.py; then
    admin_done=1
else
    admin_done=0
    echo "No superuser was made. Fill ADMIN_EMAIL and ADMIN_PASSWORD in deploy/.env.production, run bash deploy/deploy.sh" >&2
    echo "(the containers read the file when they start), then run this again; or make one by hand:" >&2
    echo "    docker compose exec web python manage.py createsuperuser" >&2
fi

echo "==> search index (every book that has lines; none yet on a new server)"
run python manage.py research_reindex

echo "==> expiring page-quota grants"
run python manage.py expire_quota

if [ "$admin_done" = 1 ]; then
    echo
    echo "Done. Now delete the password from the file (it is in the database now) and let the containers forget it:"
    echo "    sed -i '/^ADMIN_PASSWORD=/d' deploy/.env.production && bash deploy/deploy.sh --no-pull"
fi
