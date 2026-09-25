# Nassakh — local development on the Mac. Python lives in .venv, Node is build-time only.
PY      := .venv/bin/python
CELERY  := .venv/bin/celery
DB_NAME := nassakh
# createdb/psql talk to the server named in .env's DATABASE_URL (port 5433 = postgresql@17).
DB_URL  := $(shell grep -E '^DATABASE_URL=' .env 2>/dev/null | cut -d= -f2-)
PGPORT  ?= $(shell echo "$(DB_URL)" | sed -nE 's,.*:([0-9]+)/.*,\1,p')
export PGPORT

.PHONY: install db migrate superuser seed-groups web worker gpu-worker css css-watch editor test lint

install:            ## Python deps (+dev extras) and the frontend build
	uv pip install --python .venv/bin/python -r pyproject.toml --extra dev
	npm install --no-audit --no-fund
	npm run build

db:                 ## create the Postgres database (if missing) and migrate
	@psql -lqt | cut -d '|' -f 1 | grep -qw $(DB_NAME) || createdb $(DB_NAME)
	$(PY) manage.py migrate

migrate:
	$(PY) manage.py migrate

superuser:
	$(PY) manage.py createsuperuser

seed-groups:        ## make sure the admin / editor / proofreader groups exist
	$(PY) manage.py seed_groups

web:                ## Django dev server on :8000
	$(PY) manage.py runserver 8000

worker:             ## CPU worker: ingest, preprocess, layout, fast OCR, page renders; also the re-layout queue
	$(CELERY) -A nassakh worker -Q default,layout -c 4 -l info

gpu-worker:         ## GPU worker: the OCR models, one process, models resident
	$(CELERY) -A nassakh worker -Q gpu -c 1 -P solo -l info

css:                ## build static/dist/app.css once
	npm run build:css

editor:             ## build the TipTap editor bundle static/dist/editor.js
	npm run build:editor

css-watch:          ## rebuild the CSS on every template change
	npm run watch:css

test:
	.venv/bin/pytest

lint:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .
