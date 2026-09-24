# Runbook — running Nassakh on the Mac

Everything runs locally on the owner's Apple Silicon Mac (D5): Django dev server, two Celery workers,
PostgreSQL and Redis from Homebrew. Commands below are the ones that were run and worked during the
Phase 2 integration (2026-09-24); run them from the repository root.

## 1. Requirements

| What | Version | Notes |
|---|---|---|
| Python | 3.13 in `.venv` | `uv` installs the dependencies (`make install`) |
| **PostgreSQL** | **17** (≥ 15 required) | **Django 6.1 refuses PostgreSQL 14** (`NotSupportedError: PostgreSQL 15 or later is required (found 14.18)`). `postgresql@17` runs on **port 5433** next to the existing `postgresql@14` on 5432, which stays untouched for other projects. |
| Redis | 7 | `brew services start redis` |
| Node | 22 | build time only: Tailwind CSS, vendoring Alpine.js and the IBM Plex Sans Arabic fonts |
| Tesseract | 5.5 with `ara` + `eng` | `brew install tesseract tesseract-lang` |
| OCR models | Qari v0.3, Qari v0.2 (merged) | under `OCR_MODELS_DIR` (default `playground/poc/models`): `qari-v0.3/`, `qari-v0.2-merged/`, MLX conversions under `mlx/qari-v0.3`, `mlx/qari-v0.2`; prepared by `playground/poc/prepare_models.py` |
| PyTorch | 2.14 with MPS | installed by `make install`; `mlx-vlm` is the optional `mlx` extra |

## 2. First-time setup

```sh
# PostgreSQL 17 next to 14 (one-off)
brew install postgresql@17
sed -i '' -E 's/^#?port = 5432/port = 5433/' /opt/homebrew/var/postgresql@17/postgresql.conf
brew services start postgresql@17            # restarts at login
brew services start redis
/opt/homebrew/opt/postgresql@17/bin/pg_isready -p 5433   # "accepting connections"

# Project
cp .env.example .env                         # DATABASE_URL=postgres://localhost:5433/nassakh; set SECRET_KEY
make install                                 # uv pip install (+dev extras) · npm install · npm run build
make db                                      # createdb nassakh (on the port in DATABASE_URL) + migrate (22 migrations)
make seed-groups                             # admin / editor / proofreader groups (also created by a migration)
make superuser                               # first user; superusers pass every role check
```

`psql`/`createdb` on the PATH are the PostgreSQL 14 clients; they work against the 17 server when
`PGPORT=5433` is set. The Makefile exports `PGPORT` from the port in `.env`'s `DATABASE_URL`, so
`make db` needs nothing else. From a shell: `PGPORT=5433 psql -d nassakh`.

Give a non-superuser a role in `/admin/` (groups `admin`, `editor`, `proofreader`). `editor` is needed to
create books, start processing, re-run stages, apply guides and change preprocessing parameters;
`proofreader` only reads (review screens come in Phase 3).

`.env` keys (all optional, defaults in `nassakh/settings.py`; comments must be on their own lines):

| Key | Default | Meaning |
|---|---|---|
| `DEBUG` | `false` | development mode |
| `SECRET_KEY` | insecure dev key | change it |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | comma separated |
| `DATABASE_URL` | `postgres://localhost:5432/nassakh` | use `postgres://localhost:5433/nassakh` for `postgresql@17` |
| `REDIS_URL` | `redis://localhost:6379/0` | Celery broker and result backend |
| `CELERY_TASK_ALWAYS_EAGER` | `false` | run tasks inline in the calling process (no workers; see §3) |
| `MEDIA_ROOT` | `media` | uploads and derived images; relative to the repo |
| `TIME_ZONE` | `Africa/Tripoli` | |
| `LOG_LEVEL` | `INFO` | console logging |
| `OCR_BACKEND` | `torch` | `torch` (PyTorch on MPS) or `mlx` (`mlx-vlm`, needs the `mlx` extra and the converted weights) |
| `OCR_DEVICE` | `auto` | `auto` (MPS when available), `mps`, `cpu` |
| `OCR_MODELS_DIR` | `playground/poc/models` | model directories, relative to the repo or absolute |
| `OCR_PRIMARY` / `OCR_SECONDARY` | `qari_v03` / `qari_v02` | engine names from `ocr.engines.registry` |
| `TESSERACT_LANGS` | `ara+eng` | D17 |

## 3. Running

Three terminals (or `honcho start` / `overmind s` with the `Procfile`):

```sh
make web          # http://127.0.0.1:8000/  → /accounts/login/ → /books/
make worker       # queue "default": ingest, preprocess, layout, Tesseract (4 processes)
make gpu-worker   # queue "gpu": Qari v0.3 + v0.2, one solo process, models stay loaded (~9 GB)
```

The first page through the gpu worker is slow because both models load (about a minute); warm them up
after starting the worker with `.venv/bin/python manage.py shell -c "from ocr.tasks import warm_up_engines; warm_up_engines.delay()"`.

Without workers, for quick manual checks: `CELERY_TASK_ALWAYS_EAGER=true make web` runs every task
inside the web request (the OCR models too, so «بدء المعالجة» blocks for minutes on a real book).

Frontend while editing templates or `static/src/**`: `make css-watch` (rebuilds `static/dist/app.css`,
which is committed; `npm run build` also re-vendors Alpine and the fonts).

## 4. What happens after «بدء المعالجة»

```
ingest_book_task  →  group(preprocess_page × N)  →  after_preprocess  →  per page: layout_page → ocr_page_fast → ocr_page_full
Book:  uploaded → processing → (needs_guides) → ocr → ready_for_review        (error when the PDF cannot be read)
Page:  uploaded → preprocessed → layout_done → ocr_done                       (error keeps error_from; excluded pages are skipped)
Text:  none → provisional (Tesseract, after ocr_page_fast) → final (after ocr_page_full)
```

- **Ingest** renders the selected PDF pages `[skip_first, N − skip_last)` at the scan's native DPI (D2), splits
  two-page sheets at the detected gutter, right page first (D3), and stores `original.png` per page.
- **Preprocess** deskews, flattens, removes dark borders and facing-page strips (D18), crops, binarises, detects lines
  and the footnote rule; flags `large_skew`, `deskew_low_confidence`, `no_lines_detected`, `edge_strip_removed`.
- **Guides**: when fewer than 40 % of the pages show a footnote rule the book stops in `needs_guides`; open
  «ضبط الأدلة», place the header cut and the footnote line on a reference page and press «تطبيق على كل الصفحات».
  Otherwise the median rule position becomes the book's footnote line automatically and layout + OCR continue.
- **Layout** derives the regions (running header, body, footnote, page number) from the guides; a page can
  override the guides from its detail screen (`POST /api/pages/<id>/guides/`).
- **Fast OCR** (Tesseract `ara+eng` on the B&W crops) gives the provisional text and the word boxes.
- **Full OCR** (gpu queue) runs Qari v0.3 then v0.2 on the grayscale crops (footnotes at 2×), checks each
  against Tesseract (D16), builds the lines with low-confidence tokens (digits always low, D17), converts digits
  in `final_text` to Western (D6) and marks the page `ocr_done`; the book becomes `ready_for_review` when every
  non-excluded page is done. Flags: `ocr_fallback` (both models failed, Tesseract text used), `alignment_poor`.

## 5. Re-running a page or a stage

Stages: `preprocess` · `layout` · `ocr` (fast then full) · `ocr_fast` · `ocr_full`. Every re-run overwrites only
that stage's outputs and continues down the chain; originals are never touched; excluded pages are refused.

| Where | What it does |
|---|---|
| Dashboard `/books/<id>/` → «إعادة التشغيل» | all non-excluded pages from the chosen stage (`books.tasks.rerun_book_from`) |
| Page detail `/books/<id>/pages/<n>/` → «إعادة التشغيل» | this page from the chosen stage (`books.services.run_stage(page, stage)`) |
| Page detail error banner → «إعادة المحاولة من هذه المرحلة» | the failed stage again |
| Page detail → panel «المعالجة الأولية» → «إعادة المعالجة» / «استعادة القيم التلقائية» | preprocessing only, with manual angle / crop / Sauvola / denoise values (`POST /api/pages/<id>/preprocess/`); regions are re-derived, OCR is **not** re-run: use the re-run menu → «التعرّف على النص» afterwards |
| Guides `/books/<id>/guides/` → «تطبيق على كل الصفحات» | regions re-derived for every preprocessed page; OCR re-queued only for pages whose regions changed |
| Dashboard / page detail → «استثناء الصفحة» | excludes a page from every stage and from the book's progress; toggle again to bring it back |

From a shell: `.venv/bin/python manage.py shell`, then
`from books.models import Page; from books.services import run_stage; run_stage(Page.objects.get(book_id=1, number=3), "ocr")`.

## 6. Where files live

```
media/books/{book_id}/source.pdf                       the upload, never modified
media/books/{book_id}/pages/{n:04d}/original.png       never overwritten
media/books/{book_id}/pages/{n:04d}/gray.png           OCR input (Qari), coordinate space of lines, regions and boxes
media/books/{book_id}/pages/{n:04d}/bw.png             Tesseract input / B&W view
media/books/{book_id}/pages/{n:04d}/display.webp       ≤ 1400 px, screens
media/books/{book_id}/pages/{n:04d}/thumb.webp         ≤ 240 px, grids
```

Media is served by Django at `/media/<path>` to signed-in users only (`core.views.protected_media`, also with
`DEBUG=false`). Every engine call is an `OcrRun` row (engine, model revision, prompt, raw output, duration,
sanity-check result) visible on the page detail screen and at `/api/pages/<id>/runs/`.

## 7. Tests and lint

```sh
make test                              # .venv/bin/pytest: 198 tests on SQLite in memory, Celery eager (nassakh/settings_test.py)
make lint                              # ruff check . && ruff format --check .
.venv/bin/pytest books processing      # one or more apps
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
```

No real model is loaded in the tests (the `fake` engine stands in); see §8 for the real-model check.

## 8. Smoke test of the real pipeline

On 2026-09-24 `playground/poc/input/sample 2.pdf` (19 landscape sheets with two book pages each, no text
layer, footnotes under a rule) went through the whole pipeline with the **real engines** (PyTorch on MPS) in
eager mode, from a script that calls `books.services.create_book(...)` with `pages_per_sheet=2, skip_first=2,
skip_last=15` (so 2 sheets = 4 pages) and then `books.services.start_processing(book)`:

```sh
CELERY_TASK_ALWAYS_EAGER=true LOG_LEVEL=INFO PYTHONPATH=. .venv/bin/python <script>.py
```

Result on the M5 Pro, 131 s wall clock for the 4 pages:

| Stage | Observed |
|---|---|
| ingest | 2 sheets → 4 pages, gutter found with confidence 0.75–0.77, native 200 DPI (1138–1201 × 1700 px), right page first |
| preprocess | 0.2–0.3 s per page; skew −1.6°, −1.0°, −0.2°, 0.0°; 18–20 lines per page; footnote rule on 3 of 4 pages; a 9 px facing-page strip removed on page 2 (`edge_strip_removed`) |
| guides | proposed automatically: footnote line at 0.7988 of the height (confidence 0.75 ≥ 0.4) → no `needs_guides` stop |
| layout | body · footnote · page number (bottom 6 %) per page |
| fast OCR | Tesseract `ara+eng` 0.7–1.0 s per page (3 regions), provisional text 860–940 characters |
| full OCR | Qari v0.3 loaded in 4 s, v0.2 in 3 s (about 9 GB resident); per page v0.3 ≈ 18 s (body 15 s, footnote at 2× 3 s), v0.2 ≈ 11 s; 28–40 s per page for the stage |
| sanity check (D16) | body passed on every page with both models; the two-line footnotes of pages 3 and 4 failed on both models (`low_overlap`) → Tesseract text used for those regions, pages flagged `ocr_fallback` |
| result | 4 pages `ocr_done` / text `final`, 19–20 `Line` rows per page each with a box, 173–198 tokens per page (9–16 low-confidence, digits included), `final_text` 944–988 characters with Western digits (e.g. «سنة 21 ه»), 7 `OcrRun` rows per page (3 tesseract, 2 qari_v03, 2 qari_v02), book `ready_for_review` |

Screens and endpoints were then checked against `make web` with the real session: anonymous requests are
redirected to `/accounts/login/` (HTML screens, media) or answered `403` (JSON API); signed in, `/books/`,
`/books/1/`, `/books/1/pages/1/`, `/books/1/guides/`, `/api/books/1/progress/`, `/api/pages/1/status/`,
`/api/pages/1/text/`, `/api/pages/1/runs/` and `/media/books/1/pages/0001/thumb.webp` all return `200`.

The same sheet was then run with **real workers** (`make worker` with the prefork pool, `make gpu-worker`,
Redis broker and result backend): `ingest_book_task` → chord of two `preprocess_page` → `after_preprocess` →
`layout_page` → `ocr_page_fast` on the default worker in 1.5 s, then `ocr_page_full` on the gpu worker (models
loaded once, 37 s for the first page, 28 s for the second); book `ready_for_review` after 69 s. This run is what
uncovered the prefork failure fixed in `nassakh/celery.py` (see §10).

Throughput estimate from these numbers: about 30–40 s per page on the gpu worker with the `torch` backend,
so a 400-page scanned book takes 3–4 hours unattended; `OCR_BACKEND=mlx` was about 1.8× faster in the PoC.

## 9. Known limitations of Phase 2

- No review screen yet (Phase 3); pages stop at `ocr_done` and books at `ready_for_review`.
- Users and roles are managed in Django admin (`/admin/`), linked from the sidebar for admins.
- Tables in born-digital books are not extracted (deferred).
- Running-header detection is manual: set the header cut on the guides screen. Headings and poetry inside the
  body are not detected yet (regions are purely geometric from the guides).
- The «الأصل» tab shows the grayscale render of the page (the pipeline consumes grayscale); there is no colour original.
- The crop box in the preprocessing panel is numeric (x0, y0, x1, y1 in the rotated frame), not drag handles.
- A manual preprocessing re-run does not re-run OCR by itself (see §5).
- Born-digital books with `use_text_layer` are finalised from the repaired text layer during fast OCR; the Qari
  models are not run on them.

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| `NotSupportedError: PostgreSQL 15 or later is required (found 14.18)` | `.env` still points at 5432; use `postgres://localhost:5433/nassakh` (§2) |
| `connection refused` on 5433 | `brew services start postgresql@17`; check `pg_isready -p 5433` |
| default worker: every task fails with `Task handler raised error: ValueError('not enough values to unpack (expected 3, got 0)')` | the prefork children are spawned, not forked, on macOS / Python 3.13; `nassakh/celery.py` sets `FORKED_BY_MULTIPROCESSING=1` for that reason. If it reappears (custom launcher that bypasses `nassakh.celery`), export the variable before `celery worker`, or run the worker with `-P threads` |
| `tesseract language(s) missing: ['ara']` | `brew install tesseract-lang`; `tesseract --list-langs` must show `ara` and `eng` |
| page error «تعذّر تحميل محرّك التعرّف …» / `… is not prepared` | weights missing under `OCR_MODELS_DIR`: run `playground/poc/prepare_models.py` (`--mlx` for the MLX backend) or fix `OCR_MODELS_DIR` |
| gpu worker very slow or swapping | both models need about 9 GB; close other GPU-heavy apps, or set `OCR_BACKEND=mlx` (about 1.8× faster in the PoC) |
| dashboard does not update | it polls `/api/books/<id>/progress/` every 2 s only while the book is `processing` or `ocr`; check that the workers are running (`make worker`, `make gpu-worker`) |
| `NoReverseMatch` after moving routes | API routes are reversed as `api:<name>` (`book_progress`, `page_status`, `page_preprocess`, `page_guides_override`, `page_text`, `page_runs`) |
