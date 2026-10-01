# Nassakh — local development on the Mac. Python lives in .venv, Node is build-time only.
PY      := .venv/bin/python
CELERY  := .venv/bin/celery
DB_NAME := nassakh
# createdb/psql talk to the server named in .env's DATABASE_URL (port 5433 = postgresql@17).
DB_URL  := $(shell grep -E '^DATABASE_URL=' .env 2>/dev/null | cut -d= -f2-)
PGPORT  ?= $(shell echo "$(DB_URL)" | sed -nE 's,.*:([0-9]+)/.*,\1,p')
export PGPORT
# The gpu queue's pool follows OCR_BACKEND (.env, or the shell): the local models stay resident in one solo
# process; with `runpod` the models run on Runpod and the worker is a threads pool of GPU_THREADS HTTP clients,
# so several pages are read at once (docs/RUNBOOK.md section 18).
OCR_BACKEND ?= $(shell grep -E '^OCR_BACKEND=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' ")
GPU_THREADS ?= 4
ifeq ($(OCR_BACKEND),runpod)
GPU_POOL := -P threads -c $(GPU_THREADS)
else
GPU_POOL := -P solo -c 1
endif

.PHONY: install kraken db migrate superuser seed-groups web worker gpu-worker css css-watch editor test lint

install:            ## Python deps (+dev extras) and the frontend build
	uv pip install --python .venv/bin/python -r pyproject.toml --extra dev
	npm install --no-audit --no-fund
	npm run build

kraken:             ## Kraken reads Arabic-Indic numbers (D50): its own environment (torch <= 2.9) and the model
	test -x .venv-kraken/bin/python || uv venv .venv-kraken --python 3.11
	uv pip install --python .venv-kraken/bin/python "kraken>=6.0.3,<7" Pillow
	mkdir -p models/kraken
	test -s models/kraken/all_arabic_scripts.mlmodel || curl -L -o models/kraken/all_arabic_scripts.mlmodel \
		"https://zenodo.org/records/7050270/files/all_arabic_scripts.mlmodel?download=1"

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

worker:             ## CPU worker: ingest, preprocess, layout, fast OCR, page renders; the re-layout and export queues
	$(CELERY) -A nassakh worker -Q default,layout,export -c 4 -l info

gpu-worker:         ## GPU worker: the OCR models resident in one solo process, or Runpod's HTTP clients in threads
	$(CELERY) -A nassakh worker -Q gpu $(GPU_POOL) -l info

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
