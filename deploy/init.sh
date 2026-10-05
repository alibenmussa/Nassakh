#!/bin/sh
# The `init` service (docker-compose.yml): runs once per deploy, before web, workers and MCP start.
set -eu

python manage.py migrate --noinput
python manage.py collectstatic --noinput
# printed in `docker compose logs init`; it never stops the deploy (until SMTP is set, mail.E001 is an error there)
python manage.py check --deploy || echo "nassakh: check --deploy reported problems (above); the deploy goes on." >&2
