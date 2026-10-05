#!/bin/sh
# Entrypoint of every Nassakh service (the Dockerfile's ENTRYPOINT): refuse to run with a placeholder
# SECRET_KEY, then run the service's command. The `init` service starts as root (docker-compose.yml) only to
# make the volumes writable, and steps down to the app user before it runs anything of ours.
set -eu

case "${SECRET_KEY:-}" in
    "" | django-insecure* | change-me* | CHANGE_ME* | replace-me*)
        echo "nassakh: SECRET_KEY is empty or still a placeholder: fill deploy/.env.production (docs/DEPLOY.md)." >&2
        exit 1
        ;;
esac

if [ "$(id -u)" = "0" ]; then
    for dir in /data/media /app/staticfiles; do
        # a volume made by root (or restored by it) is handed to the app user; once, not on every start
        if [ "$(stat -c %u "$dir")" != "1000" ]; then
            chown -R nassakh:nassakh "$dir"
        fi
    done
    exec gosu nassakh "$0" "$@"
fi

exec "$@"
