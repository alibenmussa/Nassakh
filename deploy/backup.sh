#!/usr/bin/env bash
# Backups and the daily housekeeping (docs/DEPLOY.md, step 11). Run by cron as the deploy user:
#
#   bash deploy/backup.sh db         a gzip'd pg_dump in backups/db-<date>.sql.gz; the newest 7 are kept
#   bash deploy/backup.sh predeploy  the same as predeploy-<date>.sql.gz, the newest 5 kept (deploy.sh makes one)
#   bash deploy/backup.sh media      a tar.gz of the books' files in backups/media-<date>.tar.gz; the newest 2 are kept
#   bash deploy/backup.sh quota      manage.py expire_quota (page-quota grants that ran out)
#   bash deploy/backup.sh all        db, quota, and media on Sundays
#
# BACKUP_DIR (default <project>/backups), DB_KEEP (7), PREDEPLOY_KEEP (5), MEDIA_KEEP (2).

set -euo pipefail
# cron starts with a short PATH
export PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
BACKUP_DIR="${BACKUP_DIR:-$PROJECT_DIR/backups}"
DB_KEEP="${DB_KEEP:-7}"
PREDEPLOY_KEEP="${PREDEPLOY_KEEP:-5}"
MEDIA_KEEP="${MEDIA_KEEP:-2}"
STAMP="$(date +%Y-%m-%d-%H%M%S)"

mkdir -p "$BACKUP_DIR"
log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

# one backup at a time (a slow media archive must not overlap the next job)
if command -v flock > /dev/null; then
    exec 9> "$BACKUP_DIR/.lock"
    flock -n 9 || { log "another backup is running: skipped"; exit 0; }
fi
trap 'rm -f "$BACKUP_DIR"/*.part' EXIT

env_value() { grep -E "^$1=" deploy/.env.production | tail -n 1 | cut -d= -f2- | tr -d '"' || true; }

keep_newest() {  # keep_newest <glob prefix> <suffix> <count>
    # shellcheck disable=SC2012
    ls -1t "$BACKUP_DIR"/"$1"*"$2" 2> /dev/null | tail -n +"$(($3 + 1))" | xargs -r rm -f --
}

backup_db() {  # backup_db <file prefix> <how many to keep>
    local user db file
    user="$(env_value POSTGRES_USER)"; user="${user:-nassakh}"
    db="$(env_value POSTGRES_DB)"; db="${db:-nassakh}"
    file="$BACKUP_DIR/$1-$STAMP.sql.gz"
    log "database $db -> $file"
    docker compose exec -T postgres pg_dump -U "$user" -d "$db" --no-owner --no-privileges | gzip -9 > "$file.part"
    gzip -t "$file.part"
    [ "$(stat -c %s "$file.part")" -gt 1024 ] || { rm -f "$file.part"; log "ERROR: the dump is empty"; exit 1; }
    mv "$file.part" "$file"
    keep_newest "$1-" .sql.gz "$2"
    log "done: $(du -h "$file" | cut -f1)"
}

backup_media() {
    local file rc=0
    file="$BACKUP_DIR/media-$STAMP.tar.gz"
    log "books' files -> $file (this can take a while)"
    # tar exit 1 = "a file changed while it was read": normal while someone works; the archive is still good
    docker compose run --rm --no-deps -T --entrypoint tar web czf - -C /data media > "$file.part" || rc=$?
    if [ "$rc" -gt 1 ]; then
        rm -f "$file.part"
        log "ERROR: tar failed ($rc)"
        exit 1
    fi
    gzip -t "$file.part"
    mv "$file.part" "$file"
    keep_newest media- .tar.gz "$MEDIA_KEEP"
    log "done: $(du -h "$file" | cut -f1)"
}

expire_quota() {
    log "expire_quota"
    docker compose exec -T web python manage.py expire_quota
}

case "${1:-}" in
    db) backup_db db "$DB_KEEP" ;;
    predeploy) backup_db predeploy "$PREDEPLOY_KEEP" ;;
    media) backup_media ;;
    quota) expire_quota ;;
    all)
        backup_db db "$DB_KEEP"
        expire_quota
        [ "$(date +%u)" = "7" ] && backup_media || true
        ;;
    *) echo "usage: $0 db | predeploy | media | quota | all" >&2; exit 2 ;;
esac
