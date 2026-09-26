# Runbook — running Nassakh on the Mac

Everything runs locally on the owner's Apple Silicon Mac (D5): Django dev server, two Celery workers,
PostgreSQL and Redis from Homebrew. Commands below are the ones that were run and worked during the
Phase 2 integration (2026-09-24); run them from the repository root.

## 1. Requirements

| What | Version | Notes |
|---|---|---|
| Python | 3.13 in `.venv` | created with `uv venv --python 3.13 .venv`; `uv` installs the dependencies (`make install`) |
| **PostgreSQL** | **17** (≥ 15 required) | **Django 6.1 refuses PostgreSQL 14** (`NotSupportedError: PostgreSQL 15 or later is required (found 14.18)`). `postgresql@17` runs on **port 5433** next to the existing `postgresql@14` on 5432, which stays untouched for other projects. |
| Redis | 7 | `brew services start redis` |
| Node | 22 | build time only: Tailwind CSS, vendoring Alpine.js and the IBM Plex Sans Arabic fonts |
| Tesseract | 5.5 with `ara` + `eng` | `brew install tesseract tesseract-lang` |
| OCR models | Qari v0.3, Qari v0.2 (merged) | under `OCR_MODELS_DIR` (default `playground/poc/models`): `qari-v0.3/`, `qari-v0.2-merged/`, MLX conversions under `mlx/qari-v0.3`, `mlx/qari-v0.2`; prepared by `playground/poc/prepare_models.py` |
| PyTorch | 2.14 with MPS | installed by `make install`; `mlx-vlm` is the optional `mlx` extra |

## 2. First-time setup

```sh
# Tools and services (one-off)
brew install uv node redis tesseract tesseract-lang
uv venv --python 3.13 .venv                  # the Makefile targets use .venv/bin/*

# PostgreSQL 17 next to 14 (one-off)
brew install postgresql@17
sed -i '' -E 's/^#?port = 5432/port = 5433/' /opt/homebrew/var/postgresql@17/postgresql.conf
brew services start postgresql@17            # restarts at login
brew services start redis
/opt/homebrew/opt/postgresql@17/bin/pg_isready -p 5433   # "accepting connections"

# Project
cp .env.example .env                         # DATABASE_URL=postgres://localhost:5433/nassakh; set SECRET_KEY
make install                                 # uv pip install (+dev extras) · npm install · npm run build
make kraken                                  # Kraken for Arabic-Indic numbers (D50): .venv-kraken/ + models/kraken/
make db                                      # createdb nassakh (on the port in DATABASE_URL) + migrate (24 migrations)
make seed-groups                             # admin / editor / proofreader groups (also created by a migration)
make superuser                               # first user; superusers pass every role check
```

`psql`/`createdb` on the PATH are the PostgreSQL 14 clients; they work against the 17 server when
`PGPORT=5433` is set. The Makefile exports `PGPORT` from the port in `.env`'s `DATABASE_URL`, so
`make db` needs nothing else. From a shell: `PGPORT=5433 psql -d nassakh`.

Give a non-superuser a role in `/admin/` (groups `admin`, `editor`, `proofreader`). `editor` is needed to
create books («استخراج الصفحات»), set the layout in «التخطيط», press «بدء المعالجة», re-run stages, delete books and
change preprocessing parameters;
`proofreader` only reads (review screens come in Phase 3).

`.env` keys (all optional, defaults in `nassakh/settings.py`; comments must be on their own lines):

| Key | Default | Meaning |
|---|---|---|
| `DEBUG` | `false` | development mode |
| `SECRET_KEY` | insecure dev key | change it |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | comma separated |
| `CSRF_TRUSTED_ORIGINS` | (empty) | comma separated origins with scheme, e.g. `https://nassakh.local`; needed only when the site is not served from localhost (a LAN hostname or a tunnel), otherwise every POST, login included, fails the CSRF check |
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
| `NUMBERS_PASS` | `true` | the numbers pass after Qari (D50): Kraken reads the Arabic-Indic numbers of each finalised page |
| `KRAKEN_PYTHON` / `KRAKEN_MODEL` | `.venv-kraken/bin/python` / `models/kraken/all_arabic_scripts.mlmodel` | Kraken's own environment and model (`make kraken`) |

## 3. Running

Three terminals (or one `honcho start` with the `Procfile`; honcho is not a project dependency:
`uv pip install --python .venv/bin/python honcho` once, or `brew install overmind` for `overmind s`):

```sh
make web          # http://127.0.0.1:8000/  → /accounts/login/ → /books/
make worker       # queues "default", "layout", "export": ingest, preprocess, layout, Tesseract, page renders, exports (4 processes)
make gpu-worker   # queue "gpu": Qari v0.3 + v0.2, one solo process, models stay loaded (~9 GB)
```

The first page through the gpu worker is slow because both models load (about a minute); warm them up
after starting the worker with `.venv/bin/python manage.py shell -c "from ocr.tasks import warm_up_engines; warm_up_engines.delay()"`.

Without workers, for quick manual checks: `CELERY_TASK_ALWAYS_EAGER=true make web` runs every task
inside the web request (the OCR models too, so «بدء المعالجة» blocks for minutes on a real book).

Frontend while editing templates or `static/src/**`: `make css-watch` (rebuilds `static/dist/app.css`,
which is committed; `npm run build` also re-vendors Alpine and the fonts).

## 4. From «استخراج الصفحات» to the text: «التخطيط», then «المعالجة»

```
«استخراج الصفحات» (upload)  ingest_book_task → chord(preprocess_page × N) → after_preprocess(continue_ocr=False) → pause
«بدء المعالجة» (start_ocr)   start_ocr_task → per page: layout_page → ocr_page_fast → ocr_page_full
Book:  uploaded → processing «قيد التخطيط» → needs_guides «تم التخطيط» ─«بدء المعالجة»→ ocr «قيد المعالجة» → ready_for_review
Page:  uploaded → preprocessed «مُجهَّزة» ─«بدء المعالجة»→ layout_done «بانتظار التعرّف» → ocr_done  (error keeps error_from; excluded pages are skipped)
Text:  none → provisional (Tesseract, after ocr_page_fast) → final (after ocr_page_full)
```

- **Two stages (D64, Phase 7a).** A book made by the upload form or by `smoke_pipeline` (both through
  `books.services.create_book`) has `Book.awaits_ocr_start = True`. «استخراج الصفحات» extracts and prepares its pages,
  shows their regions (computed, not stored) and stops at «تم التخطيط». No model reads anything before
  «بدء المعالجة»: `books.services.start_ocr` claims the start once (flag → False, status `ocr`) and queues
  `start_ocr_task`, which enqueues the per-page chains. While the flag is True, `layout_page` and the OCR tasks return
  at once, and only «تجهيز الصفحات» can be re-run. Books with the flag False (every book made before 7a, fixtures and
  scripts that call `Book.objects.create`) go straight from preprocess to layout and OCR, as before.
- **Ingest** renders the selected PDF pages `[skip_first, N − skip_last)` at the scan's native DPI (D2), splits
  two-page sheets at the detected gutter, right page first (D3), and stores `original.png` per page.
- **Preprocess** deskews, flattens, removes dark borders and facing-page strips (D18), crops, binarises, detects lines
  and, **on each page separately**: the footnote separator (solid, dotted, dashed or short rule;
  `Preprocess.footnote_rule_y`), else a block of smaller type at the bottom (`footnote_block_y`), and the printed
  page number (a short isolated first/last line in the top 12 % / bottom 15 %; `page_number_box`). Since D68 a
  thicker solid bar (up to half a line high, filled ≥ 60 %) also counts as a rule when no thin rule is found; existing
  books change only when a page is prepared again. Flags
  `large_skew`, `deskew_low_confidence`, `no_lines_detected`, `edge_strip_removed`.
- **The layout («التخطيط»)**: the book-level proposal (median rule position and its confidence) is computed after
  preprocessing for display only; it stays inert until the owner changes something. The dashboard's «التخطيط» mode
  (§14) moves a page's running-head cut or footnote line, adds or removes a band, or sets a rule for all pages. While
  the book awaits the start a change is only saved (pages stay «مُجهَّزة»); on a started book (`?view=guides`) it
  re-derives the page's regions and re-reads its text, except on approved pages and pages with review corrections,
  which are locked.
- **Layout** derives the regions per page (`processing.services.resolve_layout`, through `page_layout` and
  `page_region_specs`; «التخطيط» draws the same bands before they are stored). Footnote top: page override →
  detected rule → detected smaller-type block → manual book footnote line → none (the body runs to the bottom; an
  automatically proposed book line is never applied). Page number: page override → detected box (+6 px) → manual
  book zone → none (no automatic bottom zone). A page can override from the «التخطيط» viewer or its detail screen
  (`POST /api/pages/<id>/guides/`).
- **Fast OCR** (Tesseract `ara+eng` on the B&W crops) gives the provisional text and the word boxes. Page numbers
  never reach the text: the page-number region is not transcribed, and as a safety net a first or last line that is
  only digits / dashes / dots / brackets is dropped from `provisional_text`, `final_text` and the `Line` rows. Its
  number (Western digits) is stored in `Page.printed_number` as metadata; page order always comes from the scan
  order. The dashboard's «تحتاج انتباهًا» lists pages whose printed numbers are not consecutive in scan order
  (a gap = missing scan, a repeat = duplicate scan); pages whose number was not read do not raise a gap.
- **Full OCR** (gpu queue) runs Qari v0.3 then v0.2 on the grayscale crops (footnotes at 2×), checks each
  against Tesseract (D16), builds the lines with low-confidence tokens (digits always low, D17), converts digits
  in `final_text` to Western (D6) and marks the page `ocr_done`; the book becomes `ready_for_review` when every
  non-excluded page is done. Flags: `ocr_fallback` (both models failed, Tesseract text used), `alignment_poor`.

## 5. Re-running a page or a stage

Stages: `preprocess` · `layout` · `ocr` (fast then full) · `ocr_fast` · `ocr_full`. Every re-run overwrites only
that stage's outputs and continues down the chain; originals are never touched; excluded pages are refused.
A re-run first puts the page back to the stage's input state (`uploaded`, `preprocessed`, `layout_done`), so the
book stays `processing`/`ocr` until its last page is through (`ocr_fast` alone keeps the final text and status).
If a task of the chain fails, the later tasks skip the page and keep that error, so the retry button points at
the stage that really failed. A book whose remaining pages all failed settles (`ready_for_review` when some pages
are done, `error` when none is) instead of staying active. A page re-included with «استثناء الصفحة» continues
from its last completed stage.

| Where | What it does |
|---|---|
| Dashboard `/books/<id>/` → «⋯» → a stage «…» | a dialog first names the pages that run, the approved pages kept and the model time (`books.services.rerun_estimate`); then all non-excluded, unapproved pages from that stage (`books.tasks.rerun_book_from`). In «التخطيط» only «إعادة تجهيز الصفحات…» is offered, and it stops again at «تم التخطيط» |
| Page detail `/books/<id>/pages/<n>/` → «إعادة التشغيل» | this page from the chosen stage (`books.services.run_stage(page, stage)`) |
| Page detail error banner → «إعادة المحاولة من هذه المرحلة» | the failed stage again |
| Dashboard → «تحتاج انتباهًا» → «إعادة <stage>» on a failed page | the failed stage again, then back to the dashboard (same `books:rerun` with `next`) |
| Page detail → panel «تجهيز الصفحة» → «إعادة المعالجة» / «استعادة القيم التلقائية» | preprocessing only, with manual angle / crop / Sauvola / denoise values (`POST /api/pages/<id>/preprocess/`); regions are re-derived (for very large originals the worker runs preprocess → layout, answer 202), OCR is **not** re-run: use the re-run menu → «التعرّف على النص» afterwards |
| Dashboard → «⋯» → «التخطيط» (`?view=guides`) → «تطبيق على كل الصفحات» | a preview first (pages that change, lines cut, pages with their own override, pages re-read); on a started book only the changed pages that are unapproved and have no review work are re-derived and re-read. The old address `/books/<id>/guides/` redirects there |
| Dashboard / page detail → «استثناء الصفحة» | excludes a page from every stage and from the book's progress; the toast's «تراجع» brings it back (so does toggling again) |

**After upgrading to per-page footnote / page-number detection** (migrations `processing/0003`, `books/0003`):
existing books still carry regions from the old book-level guides and no detection results. Re-run each book from
«المعالجة الأولية» (`preprocess`) so the rule / block / page number are detected, which continues through layout and
OCR; for a book whose preprocessing is otherwise fine a re-run from «التخطيط» (`layout`) re-derives the regions
from the stored detection, but pages preprocessed before the upgrade have no `footnote_block_y` / `page_number_box`
yet, so `preprocess` is the complete path. `printed_number` is filled by the OCR stages. A book left in
`needs_guides` by the old version is resumed the same way.

From a shell: `.venv/bin/python manage.py shell`, then
`from books.models import Page; from books.services import run_stage; run_stage(Page.objects.get(book_id=1, number=3), "ocr")`.

## 6. Where files live

```
media/books/{book_id}/source.pdf                       the upload, never modified
media/books/{book_id}/pages/{n:04d}/original.png       never overwritten
media/books/{book_id}/pages/{n:04d}/scan_thumb.webp    ≤ 240 px scan preview written at ingest (tile before preprocessing)
media/books/{book_id}/pages/{n:04d}/gray.png           OCR input (Qari), coordinate space of lines, regions and boxes
media/books/{book_id}/pages/{n:04d}/bw.png             Tesseract input / B&W view
media/books/{book_id}/pages/{n:04d}/display.webp       ≤ 1400 px, screens
media/books/{book_id}/pages/{n:04d}/thumb.webp         ≤ 240 px, grids
```

Media is served by Django at `/media/<path>` to signed-in users only (`core.views.protected_media`, also with
`DEBUG=false`). Every engine call is an `OcrRun` row (engine, model revision, prompt, raw output, duration,
sanity-check result) visible on the page detail screen and at `/api/pages/<id>/runs/`.

`/api/pages/<id>/status/` and `/api/pages/<id>/text/` return a failure as `error` (the Arabic headline) plus
`error_detail` (the technical lines); the status payload also carries `images` (the three image-tab URLs).
Manual preprocessing values are stored per key (`Preprocess.manual_params`): a chain re-run keeps only the keys
the user changed and re-detects the rest.

## 7. Tests and lint

```sh
make test                              # .venv/bin/pytest: the whole suite on SQLite in memory, Celery eager (nassakh/settings_test.py)
make lint                              # ruff check . && ruff format --check .
.venv/bin/pytest books processing      # one or more apps
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
```

No real model is loaded in the tests (the `fake` engine stands in); see §8 for the real-model check.

## 8. Smoke test of the real pipeline

On 2026-09-24 `playground/poc/input/sample 2.pdf` (19 landscape sheets with two book pages each, no text
layer, footnotes under a rule) went through the whole pipeline with the **real engines** (PyTorch on MPS) in
eager mode, through `books.services.create_book(...)` with `pages_per_sheet=2, skip_first=2, skip_last=15`
(so 2 sheets = 4 pages) and then `books.services.start_processing(book)`. The management command
`smoke_pipeline` does exactly that and prints one row per page. Since Phase 7a it stops at «تم التخطيط», calls
`books.services.start_ocr(book)` and then prints the rows; `--layout-only` stops at the pause and prints each page's
bands and doubts instead (run it on a copy of the database: it creates a book):

```sh
CELERY_TASK_ALWAYS_EAGER=true LOG_LEVEL=INFO .venv/bin/python manage.py smoke_pipeline \
    "playground/poc/input/sample 2.pdf" --pages-per-sheet 2 --skip-first 2 --skip-last 15 [--layout-only]
```

Phase 7a integration (2026-09-26, `OCR_BACKEND=mlx`, on the copy `nassakh_p7check`): `--layout-only` stopped at
`needs_guides` after 1 s with a rule on 4 of 4 pages and a page number on 3, no doubts; the full run reached
`ready_for_review` in 105 s, 4 pages `ocr_done` / `final`, 18–19 lines and 919–971 characters per page.

Result on the M5 Pro after the review fixes (2026-09-24, commit 35c18e8), 134 s wall clock for the 4 pages:

| Stage | Observed |
|---|---|
| ingest | 2 sheets → 4 pages, gutter found with confidence 0.75–0.77, native 200 DPI (1138–1201 × 1700 px), right page first |
| preprocess | 0.2–0.3 s per page; skew −1.6°, −1.0°, −0.2°, 0.0°; 18–20 lines per page; footnote rule on 3 of 4 pages; a 9 px facing-page strip removed on page 2 (`edge_strip_removed`) |
| guides | proposed automatically: footnote line at 0.7988 of the height (confidence 0.75 ≥ 0.4) → no `needs_guides` stop |
| layout | body · footnote · page number (bottom 6 %) per page |
| fast OCR | Tesseract `ara+eng` 0.7–1.0 s per page (3 regions), provisional text 860–940 characters |
| full OCR | Qari v0.3 loaded in 6 s, v0.2 in 3 s (about 9 GB resident); 28–42 s per page for the stage (both models, body + footnote at 2×) |
| sanity check (D16) | every region on every page kept the Qari v0.3 text (`fallback=False`); short footnote references are now checked for runaway length only, and `models_agree` keeps the Qari text when both models agree with each other. Before the review fixes the two-line footnotes of pages 3 and 4 had fallen back to Tesseract |
| result | 4 pages `ocr_done` / text `final`, 18–20 `Line` rows per page each with a box, 174–203 tokens per page (10–16 low-confidence, digits included), `final_text` 944–1014 characters with Western digits (e.g. «سنة 21 ه»), 7 `OcrRun` rows per page (3 tesseract, 2 qari_v03, 2 qari_v02), `edge_strip_removed` flagged on 3 pages, book `ready_for_review` |

Screens and endpoints were then checked against `make web` with the real session: anonymous requests are
redirected to `/accounts/login/` (HTML screens, media) or answered `403` (JSON API); signed in, `/books/`,
`/books/1/`, `/books/1/pages/1/`, `/books/1/guides/`, `/api/books/1/progress/`, `/api/pages/1/status/`,
`/api/pages/1/text/`, `/api/pages/1/runs/` and `/media/books/1/pages/0001/thumb.webp` all return `200`.

The same sheet was then run with **real workers** (`make worker` with the prefork pool, `make gpu-worker`,
Redis broker and result backend): `ingest_book_task` → chord of two `preprocess_page` → `after_preprocess` →
`layout_page` → `ocr_page_fast` on the default worker in 1.5 s, then `ocr_page_full` on the gpu worker (models
loaded once, 37 s for the first page, 28 s for the second); book `ready_for_review` after 69 s. This run is what
uncovered the prefork failure fixed in `nassakh/celery.py` (see §11).

Throughput estimate from these numbers: about 30–40 s per page on the gpu worker with the `torch` backend,
so a 400-page scanned book takes 3–4 hours unattended; `OCR_BACKEND=mlx` was about 1.8× faster in the PoC.

### Printed page numbers

The page-number region is excluded from the text. Its digits are read by a vote of three readers (Tesseract on
the fast pass, then both Qari models on a padded 4× crop with a 16-token cap during the full pass); a value two
readers agree on is stored in `Page.printed_number`, otherwise the number stays unknown. The dashboard's
sequence check reports a gap or a duplicate only when the following numbered page confirms it; a single odd
number is reported as an uncertain read instead. Nothing is reported for books with fewer than three numbered pages.

Known weakness: an isolated Arabic-Indic digit is read by its shape, so ٦ is often returned as «7» and ٧ as «Y»
by all three readers; the vote catches disagreement but not a shared misread. Treat `printed_number` as
best-effort metadata. A template-matching digit reader is the planned improvement if reliable numbers become important.

## 9. Known limitations of Phase 2

- No review screen yet (Phase 3); pages stop at `ocr_done` and books at `ready_for_review`.
- Users and roles are managed in Django admin (`/admin/`), linked from the sidebar for admins.
- Tables in born-digital books are not extracted (deferred).
- Running-header detection is manual: set the header cut in the dashboard's «التخطيط» mode (§14). Headings and poetry inside the
  body are not detected yet (regions are geometric: per-page footnote top and page-number box, manual header cut).
- Footnote detection needs a separator rule at least 12 % of the text width, or at least two lines of clearly
  smaller type (≤ 0.8 × the body size) after a visible gap; a single small-type footnote without a rule, or a
  footnote set in the body size without a rule, is not detected (use a page override). Footnotes that continue from
  the previous page are treated like any other footnote block.
- Page numbers are found only as a short, isolated first or last line (top 12 % / bottom 15 %); a number printed
  inside the running header line, or in the outer margin beside the text, is not detected. The OCR safety net only
  removes a first/last line made of digits and punctuation; a number fused with other text on the same line stays.
- The printed-number sequence check assumes plain numerals; Roman or letter-numbered front matter is ignored.
- The «الأصل» tab shows the grayscale render of the page (the pipeline consumes grayscale); there is no colour original.
- The crop box in the preprocessing panel is numeric (x0, y0, x1, y1 in the rotated frame), not drag handles.
- A manual preprocessing re-run does not re-run OCR by itself (see §5).
- Born-digital books with `use_text_layer` are finalised from the repaired text layer during fast OCR; the Qari
  models are not run on them.

## 10. Phase 3 — processing theatre and review screen

Upgrading an existing database: run `make migrate` once (new migrations `books.0004_page_n_unresolved`,
`books.0005_backfill_n_unresolved` — fills the uncertain-word count of pages OCR'd before Phase 3 from their lines —
`ocr.0002_line_is_manual` and `review.0001_initial`).

### Watching a book (dashboard)

The pages section has two views, «صفحات» (default, a page viewer like a PDF reader: one page at a time, scan beside
its text, which "generates" while the page is provisional and resolves into the final lines) and «شبكة» (small
animated processing cards, no text). In the viewer, turn pages with the side buttons, ← / → (← is forward),
PageDown / PageUp, Home / End, a trackpad swipe or the wheel over the page (one page per gesture), a touch swipe, the
jump field (`G`), or the thumbnails in the side panel. The side panel also holds the summary (stage counts, review
state, «تحتاج انتباهًا»). The filter chips decide which pages the viewer walks through.
The page on screen is kept in the address (`#sheet-N`, shareable, survives a reload); a click on a grid card opens
it in the viewer (⌘-click opens the page detail). Sheets are fetched in batches from `/api/books/<id>/sheets/?from=<n>&to=<n>` (at most 40 pages per
call, line boxes as 0..1 ratios of the page). The review summary «مُراجَعة X من Y صفحة» and the button
«الصفحة التالية للمراجعة» come from `book_progress["review"]` (`review.services.book_review_summary`).
Each page takes its aspect ratio from its tile's `width`/`height` (cleaned image, else the scan), so it fits the
viewer before its sheet is fetched. Only the page on screen animates; the grid runs the scan
sweep only (tiles have no line boxes). With `prefers-reduced-motion` every effect is replaced by a static state.

### Reviewing a page

`/books/<id>/review/<n>/` (`review:page`) shows the scan on one side and the lines on the other; hovering or focusing
a word lights its box on the scan and the reverse. Uncertain words (amber) are resolved by choosing a reading
(النموذج الأول / النموذج الثاني / Tesseract) or typing a correction; whole lines can be edited, a missing line
inserted, a garbage line deleted. Every action is saved at once. «اعتماد الصفحة» approves the page; with uncertain
words left it asks for confirmation first. `/books/<id>/review/next/?after=<n>` (`review:next`) opens the next page
waiting for review (status `ocr_done`, after page n, wrapping to the start) or returns to the dashboard with
«لا صفحات بانتظار المراجعة». GET endpoints need a login; every change needs the `proofreader`, `editor` or `admin`
role (superusers pass). Users without a role see the screen read-only.

Shortcuts: `Tab` / `Shift+Tab` next / previous uncertain word · `1` `2` `3` choose a reading · `Enter` accept the
current reading · typing starts a correction · `Esc` close · `E` edit the line · `Alt+Enter` insert a line below ·
`Ctrl/Cmd+Z` undo · `A` approve · `N` next page to review · `ArrowLeft` / `ArrowRight` next / previous page ·
`+` `-` `0` zoom · `Alt+ArrowLeft` / `Alt+ArrowRight` merge the focused word with the next / previous word ·
`Backspace` / `Delete` remove the focused word. Clicking any word (not only uncertain ones) opens its menu:
readings (for uncertain words) and the typed correction, which is the main action (a confident word opens with
its text prefilled and selected); merge with the next / previous word (with a preview of the result) and delete
sit one level deeper in «إجراءات أخرى», which opens on hover, click or ArrowLeft, so a destructive action is never
one mis-click away. A merged word gets the union of both boxes; deleting a line's only word deletes the line.
The menu opens below the word and flips above near the bottom of the column, never crosses a side edge, and the
submenu opens on whichever side has room; a click anywhere else closes it (D31, D32). The line menu «⋯» marks
what a line is: «محتوى», «عنوان رئيسي» or «عنوان فرعي» (not for footnotes); headings show larger in the column
with a small tag, and assembly will use them for chapters and the table of contents. Every action is undoable.
The «?» button shows the full sheet.

### What a review action changes

- A word is **unresolved** while `conf == "low"` and `res` is null; `Line.n_low` counts them per line and
  `Page.n_unresolved` per page (kept by `finalize_page` and every review action). Resolving sets `res`
  (`primary | secondary | tess | typed`) but keeps `conf`, so the word stays marked as once uncertain; the primary
  reading is kept in the token's `orig` when another one replaced it, so the readings popovers (review screen and
  page detail) still offer «النموذج الأول» after a resolution; a resolved word loses its amber underline everywhere.
- Every action rebuilds `Page.final_text` from the lines: body lines, a blank line, footnote lines, Western digits.
  `Line.ocr_text` never changes; diacritics are kept exactly as typed or read.
- Editing a line re-tokenises it on whitespace and aligns the new words to the old ones: unchanged words keep their
  boxes and resolutions, a replaced word keeps the old box, an added word has none. Inserted lines are `is_manual`.
- Approving marks the page `reviewed` (book → `reviewing`) and every line `is_reviewed`, which also protects them
  from a later OCR re-run; «إعادة فتح» takes the page back to `ocr_done`.

### Undo

Each action is stored as a `review.LineRevision` (snapshots before / after; visible in Django admin under
«سجل المراجعة»). `POST /api/pages/<id>/undo/` reverts the newest revision of the page that is not undone yet,
whatever its type (a resolved word, an edit, an insert, a delete — the line comes back with its old id and place —
an approval or a reopen), marks it undone and answers the fresh review payload. Undo walks back one action per call;
there is no redo. A new OCR pass of the page (re-run from «ocr» or «ocr_full») makes the older revisions final: their
lines were replaced, so they can no longer be undone.

### Endpoints (all under `/api/`, names in the `api` namespace)

| Method | URL | Name | Body → answer |
|---|---|---|---|
| GET | `pages/<id>/review/` | `page_review` | review payload (PHASE3_SPEC §4) |
| POST | `lines/<id>/resolve/` | `line_resolve` | `{index, choice, text?}` → `{line, counts, page}` |
| POST | `lines/<id>/merge/` | `line_merge` | `{index}` joins word `index` with the next one → `{line, counts}` |
| POST | `lines/<id>/role/` | `line_role` | `{role}` = `body` / `heading` / `subheading` → `{line}` |
| POST | `lines/<id>/delete-word/` | `line_delete_word` | `{index}` → `{line, counts}`, or `{deleted_id, counts}` when it was the line's only word |
| POST | `lines/<id>/edit/` | `line_edit` | `{text}` → `{line, counts}` |
| POST | `lines/<id>/delete/` | `line_delete` | → `{deleted_id, counts}` |
| POST | `pages/<id>/lines/` | `page_lines` | `{after: line id or null, text}` → 201 `{line, lines, counts}` |
| POST | `pages/<id>/undo/` | `page_undo` | → review payload |
| POST | `pages/<id>/approve/` | `page_approve` | `{force}` → `{status, next_review_url, next_payload_url, next_number, dashboard_url}`, or 409 `{unresolved, message}` |
| POST | `pages/<id>/reopen/` | `page_reopen` | → review payload |
| GET | `books/<id>/filmstrip/` | `book_filmstrip` | `{book_id, pages: [{id, number, thumb_url, is_reviewed, n_unresolved, status, url}]}` |
| GET | `books/<id>/sheets/?from&to` | `book_sheets` | `{book_id, from, to, total, pages: [...]}` (≤ 40 pages) |

`counts` = `{line_n_low, page_unresolved, page_low_total, book_unresolved_total}`. Refused actions answer 400
`{"message": <Arabic>}`; POSTs need the CSRF token (`X-CSRFToken`, read from `<meta name="csrf-token">`).

### Word-chooser hook (D26)

`ocr/chooser.py:choose_word(token, context)` is called by `finalize_page` for every unresolved low-confidence word
with at least two distinct readings (`t`, `alt`, `tess`) when `NASSAKH["WORD_CHOOSER"]` (env `WORD_CHOOSER`) is not
`none` (the default). It returns None today (placeholder for a future small classifier). A returned string that is
one of the readings becomes the word with `res = "chooser"`; `conf` stays `low`. No UI.

## 11. Phase 4 — assembly and the manuscript view

Upgrading: `make migrate` once (new `books.0006_book_assembly_settings`, `assembly.0001_initial`, `editor.0001_initial`).

**Existing pages with merged lines (D39).** Preview, then apply, from the stored OCR runs (no model is called):

```
.venv/bin/python manage.py rebuild_lines --dry-run            # every book
.venv/bin/python manage.py rebuild_lines --book 13            # apply to one book
```

It only touches `ocr_done` pages without review edits; reviewed or edited pages are listed and skipped (re-run OCR on
them knowingly, it replaces their lines). New books get the fix automatically.

**Assembling.** On the dashboard: «تحويل إلى كتاب» (primary once every page is reviewed, else in the «⋯» menu) opens the
options (footnote numbering, include unreviewed pages, remove tatweel) and goes to `/books/<id>/manuscript/`, where the
steps tick and the manuscript appears. The worker (`make worker`) must run; the task is on the default queue.
In the view: page seams show joins/splits (click to toggle, «تلقائي» to undo an override), `O` or «عرض الأصل» opens a
block's scan with its lines highlighted, «⋯» on a block sets its role (heading/subheading/body), suggested headings
have ✓ / ×, «ملاحظات» lists warnings with a jump and a review link. Keys: G jump, S seams, J/K blocks, ] / [ warnings,
C copy, ? shortcuts, Esc closes. Editing a page in review afterwards marks the manuscript out of date; «إعادة التجميع»
rebuilds it and keeps the previous one as a snapshot (the last 10).

From the shell: `manage.py assemble <book_id> --dry-run` prints stats and warnings without writing.

## 12. Phase 5 — editor, book stylesheet and page preview

Upgrading: `make install` (WeasyPrint, ebooklib, fonttools, TipTap and esbuild are new), then `make migrate` (new
`editor.0002_stylesheet_snapshot_reason_edit`, `publishing.0001_initial`), then `npm run build` (the CSS and the editor
bundle `static/dist/editor.js`; both are committed, so this is only needed after changing the sources). The worker must run
(`make worker`): page renders are Celery tasks on the default queue.

**Fonts (D45).** Amiri is vendored. Simplified Arabic, Traditional Arabic and Times New Roman are read from the Mac's font
folders; copy `Lotus.ttf` into `~/Library/Fonts` once. A font that is not installed shows «غير مثبّت» in the stylesheet panel
and the pages fall back to Amiri. Lotus has no Latin glyphs: Latin text and digits use the stylesheet's Latin face.

**The book page (D47).** Dashboard → «فتح الكتاب» opens `/books/<id>/layout/` (the old `/editor/` address redirects
there). The pages on screen are WeasyPrint's own layout drawn as text, so line breaks, page breaks, footnote numbers and page
numbers are the PDF's. «معاينة | تحرير» (E) switches modes; a double-click in preview edits at that line. In edit mode a click
opens the paragraph in place; Enter splits, Backspace at the start merges, ↑/↓ move between paragraphs, Esc closes. After a
half-second pause the chapter is saved and laid out again (about a second for 30 pages) on the Celery queue `layout`, which
`make worker` consumes with the default queue; the pages refresh in place, later pages renumber and swap sides, and the page
count shows its change. The one side panel has icon tabs: «الفصول» (page count, chapters, page checks), «الصفحات»
(thumbnails), «بحث» (find & replace, chapter or book), «التنسيق» (trim, margins, fonts, text, page, book details for the
title and copyright pages), «الفقرة» (style, «ابدأ صفحة جديدة», «مع التالية»), «الأصل» (the scan of the paragraph with its
lines), «غير المؤكَّدة» (remaining OCR-uncertain words with their readings). Preview opens «التنسيق», edit opens «الأصل».
«⋯» holds snapshots, digit conversion, chapter re-assembly after review drift, the PDF of the last render and the shortcut
sheet (?). After the first save the manuscript is the source of truth (D41); a full «إعادة التجميع» from the manuscript view
replaces the edited text but keeps it as a snapshot that is never pruned.

### The numbers pass (D50)
Every OCR model misreads Arabic-Indic digits; Kraken with the OpenITI printed Arabic-script model reads them far better
(92 % of 116 real numbers against Qari's 43 %, `playground/digits/REPORT.md`). After Qari finalises a page, `read_numbers`
(default queue, so `make worker`) runs Kraken on the page's number words, for books detected as printing Arabic-Indic
digits (from Qari's own readings). A number Kraken read shows one reading, «Kraken», in review and in «غير المؤكَّدة»:
confirm it (1 or Enter) or type the true number. Reviewed lines, resolved words and approved pages are never touched;
Qari's readings stay on the token (`qari`) and the run is recorded (`OcrRun` engine `kraken`). Books printed with Western
digits keep Qari's numbers (it reads those better). Kraken needs its own environment (it pins torch ≤ 2.9): `make kraken`
once; without it the pass is skipped. For pages OCR'd before: `manage.py read_numbers --book ID [--page N]` (about 2 s
a page); re-assemble the book afterwards to bring the numbers into the manuscript.

The same pass reads the numbers Qari wrote as letters (D51): a lone «ا», «ه» or «ع» (a footnote mark, «ج ا،», «(ه)») and
bracketed dates without digits («(هـ – م)»). Kraken reads the line; the letter becomes the number found between its
neighbours and shows two readings in review: «Kraken» (the number) and Qari's letter (for a real letter such as
«(هـ)» in a lettered list). A digit-less date becomes one word with Kraken's date (Qari's reading second). A line the reviewer changes while
Kraken reads is left as they made it.

**Known limits.** WeasyPrint cannot fake bold or italic: Lotus (no bold file) prints headings regular, Arabic italic prints
upright. A single footnote longer than a page spills over. Word export (Phase 6) paginates slightly differently, so its page
count can differ from the preview's.

## 13. Phase 6 — «الإخراج»: Word, PDF, EPUB

Upgrading: `make migrate` (new `publishing.0003_export`), then **restart `make worker`**: it now consumes
`-Q default,layout,export`, and a worker started before the change never takes an export (the row stays «في الانتظار»
and after a minute the page says «لم يبدأ الإخراج بعد؛ تأكّد من تشغيل عامل المهام بعد آخر تحديث (make worker).»). The
engine is `nk-print-5` (D60: Latin words in a book whose Latin face is the body face are set in that face, not in a system
"Serif Narrow"; the preview never prints bleed or crop marks), so every book lays out once more the first time it opens.
Book 19's new reference counts are in `playground/word/REFERENCE.md` (85 pages).

**The page.** The book page's «الإخراج» (top bar, and «⋯») opens `/books/<id>/export/`:
- «قبل الإخراج»: what is left (uncertain words and numbers, pages of the book still unreviewed, pages left out of it,
  paragraphs that look like footnotes left in the text, review drift, a running assembly, book details, no headings),
  each with a link to what fixes it (review, the dashboard, `/books/<id>/layout/?tab=uncertain|format|chapters`). It
  never blocks; «كل الصفحات مُراجَعة ولا ملاحظات؛ الكتاب جاهز للإخراج.» only when nothing is left.
- one block per format. Word: «الكشيدة» (بلا · خفيفة · متوسطة · قوية; default خفيفة — medium and high make the book longer
  than the preview) and «تعليقات على الكلمات غير المؤكَّدة» (a Word comment by «نسّاخ» on every uncertain word, with its
  readings and scan page; the file name ends «- مع التعليقات»). The notes under the options are the known differences
  of the file (faces not embedded, the contents numbers to update in Word…).
- «السجل»: every export with its time, options and state; «تنزيل» saves the file under its Arabic name.

Every click builds a new file from the text as it is when the export starts (an edit made meanwhile stays out, and the row
then reads «تغيّر النص بعد هذا الإخراج»). One export per book and format runs at a time (a second one is refused and
offers «إلغاء»); an export whose worker died reads «توقّف الإخراج قبل أن يكتمل؛ أعد المحاولة.» once it has not moved for
10 minutes, and the format is free again. The file is named after the title it prints («بيانات الكتاب», else the
manuscript's title). Files live in `media/books/<id>/exports/<export id>.<ext>`; the newest 5 finished files and 3 failed or
cancelled rows are kept per book and format, older ones are deleted with their files (a file no row records, left by a
worker killed while it wrote, is swept away). There is no delete in v1.

**Settings** (`.env`): `EXPORT_SOFT_LIMIT_S` (1800: an export stopped after that reads «استغرق الإخراج أطول من
المسموح.»; the hard limit is 120 s more), `EXPORTS_KEPT` (5), `EXPORT_VALIDATE` (true: Word files are validated against
the ECMA-376 schemas in `publishing/word/xsd/` before they are delivered; the integrity checks always run).

**Endpoints** (under `/api/`, names in the `api` namespace): `exports` (GET the page's payload; POST `{format, options}`
→ 202 · 409 while one runs · 400 · 404 without a manuscript), `export` (GET a row; `?wait=≤5&since=<updated_at>` long-polls),
`export_cancel` (POST). The download is `publishing:export_download` (`/books/<id>/exports/<eid>/download/[?inline=1]`),
a plain page that sends a signed-out visitor to the login page. Reading and downloading need a login; starting and
cancelling need an editor or an admin.

**Dev commands.**

```sh
# the book into a file now: no row, no queue, no worker (agents and the Word harness use it)
.venv/bin/python manage.py export_book 19 --format docx --out /tmp/out/          # «كتابي.docx»
.venv/bin/python manage.py export_book 19 --format docx --kashida medium --comments --out /tmp/out/book.docx
.venv/bin/python manage.py export_book 19 --format print_pdf --bleed-mm 3 --crop-marks --out /tmp/out/
.venv/bin/python manage.py export_book 19 --format docx --options '{"kashida": "none"}' --out /tmp/out/

# the Word harness (macOS with Microsoft Word, never in pytest): Word opens on the screen, so say so first
.venv/bin/python manage.py word_check 19                  # build, open in Word, pages and lines against the preview
.venv/bin/python manage.py word_check 19 --kashida medium
.venv/bin/python manage.py word_check 19 --chapters       # each chapter alone
.venv/bin/python manage.py word_check --calibration       # calibration.docx and its checklist (the owner, ~15 min)
```

`word_check` validates every file before Word sees it, copies it into Word's own container folder (no file-access
prompt), refuses with «أغلق مستندات Word المفتوحة أولًا» when Word has other documents open, and opens exactly one file.
The calibration pass (PHASE6_SPEC §11.6: margins, footnote numbers, kashida in each face, comments, the embedded Amiri
with Amiri disabled in Font Book, the contents field…) and a 5-minute reference file made in Word
(`playground/word/reference/r1.docx`) settle the Word conventions (D62) in `publishing/word/options.py`.

## 14. Phase 7a — «التخطيط» first, then «المعالجة»

Spec `docs/PHASE7_SPEC.md` §3, decisions D64–D70.

**Upgrading.** Stop both workers (`make worker`, `make gpu-worker`), then `make migrate` (`books.0007_book_awaits_ocr_start`:
the pause flag and the new labels; `processing.0004_labels`: labels only; no data change) and `npm run build`, then start
both workers and the web server again. Until the migration runs, every screen that loads a book fails with
`column books_book.awaits_ocr_start does not exist`. Messages already queued in Redis are safe: every existing book has
the flag False and keeps the old path. Rolling the code back leaves an unused column.

**A new book.**
1. «كتاب جديد» → choose the PDF («اختيار ملف PDF»), the pages to skip and the pages per sheet. The line under the fields
   says which pages will be extracted (read from the PDF in the browser), e.g. «الملف 555 صفحة · تُستخرج الصفحات
   187–193 (7 صفحات).». «استخراج الصفحات» creates the book and starts extraction at once; the message repeats the range.
2. The dashboard opens in «التخطيط» on the grid: pages appear one by one («قيد التخطيط · 3 من 7 صفحة»), each thumbnail
   with its bands (متن · حاشية · ترويسة · رقم الصفحة). When the last page is prepared: «اكتمل التخطيط · 7 صفحات».
3. One glance. «تستحق نظرة» (amber) lists the pages worth a look: «حاشية من حجم الخط», «خط يقطع سطرًا», «سطر من المتن خارج
   المتن», «لا أسطر في الصفحة». Most books need nothing.
4. Only when needed: click a page to open it in the viewer (V switches grid and viewer). Drag the running-head cut or the
   footnote line (it snaps to the gap between lines; ⌥ drags freely; ↑/↓ nudge a focused line), add one with
   «+ ترويسة» / «+ حاشية», or open a band's menu («إزالة من هذه الصفحة», «تطبيق على كل الصفحات…», «ليس رقم صفحة»). Each
   change is saved at once: «حُفظ لهذه الصفحة · تراجع». «التلقائي» puts the page back to what was detected. For every page
   at once use the side panel's «لكل الصفحات» (e.g. «ترويسة أعلى كل الصفحات» and its percentage): a dashed draft, then
   «معاينة» (pages that change, lines it would cut, pages with their own override), then «تطبيق على كل الصفحات» or
   «إلغاء»; «إزالة الضبط العام» returns to the automatic guides.
5. «بدء المعالجة» (disabled until every page is prepared) sends the pages into «المعالجة», and the dashboard is today's.
   A book nobody touched in «التخطيط» is read exactly as before, with one extra click.

**A started book.** «⋯» → «التخطيط» opens `?view=guides`: the same grid with bands. A change turns the line dashed and asks
«حفظ وإعادة التعرّف على الصفحة» · «إلغاء»; approved pages («معتمدة: لا تتغيّر») and pages with review corrections are
locked. «العودة إلى الصفحات» leaves the mode.

**Re-runs and exclusions.** Every book-wide re-run in «⋯» asks first (pages, approved pages kept, model time). In
«التخطيط» only «إعادة تجهيز الصفحات…» is offered; layout and OCR are refused with «لم تبدأ المعالجة بعد؛ اضغط «بدء
المعالجة» أولًا.». Excluding a page shows a toast with «تراجع» (it never runs by itself).

**Deleting a book.** «⋯» → «حذف الكتاب…» (also in the «التخطيط» side panel) asks once, naming what is lost (reviewed
pages, an edited manuscript), then removes the book and every row that hangs on it in one transaction, and its folder
`media/books/<id>/` after the commit (`books.services.delete_book`, `POST /books/<id>/delete/`, editor role). It cannot
be undone. Tasks still running for the book end quietly.

**Labels.**

| Where | Before | Now |
|---|---|---|
| Book `processing` / `needs_guides` / `ocr` | «قيد المعالجة» / «بانتظار ضبط الأدلة» / «قيد التعرّف على النص» | «قيد التخطيط» / «تم التخطيط» / «قيد المعالجة» |
| Page `preprocessed` / `layout_done` | «مُعالَجة» / «تم التخطيط» | «مُجهَّزة» / «بانتظار التعرّف» |
| Stage `preprocess` / `layout` | «المعالجة الأولية» / «التخطيط» | «تجهيز الصفحات» / «تحديد المناطق» |
| Guides, page override, region source | «أدلة التخطيط», «أدلة خاصة بالصفحة», «من الأدلة» | «التخطيط العام», «تخطيط خاص بالصفحة», «من التخطيط» |
| «ضبط الأدلة» (screen and button) | – | gone; the dashboard's «التخطيط» mode |

**Keys (D69).** Every screen matches letters by physical key (`static/src/js/keys.js`), so the shortcuts work with the
Arabic keyboard layout too. Review: with the word menu open, 1–9 in any script choose a reading and any other
character starts the correction (ش included); with it closed, A E N ? + − 0, ← →, Home / End drive the page and Space
opens the focused word's menu; ⌘↵ approves from anywhere. Dashboard: V switches the view (was 1 / 2). Book page: V one
page / spread, S «فواصل الصفحات الأصلية», + − 0 the fit (were 1 / 2 / 3).

**A safe round trip (D70).**
- «إعادة بناء الفصل من المراجعة…» asks before it replaces an edited chapter («استبدال الفصل»); the API answers 409
  `{edited: true}` without `replace_edited`.
- Approving a page is no longer drift: `assembly.services.drift_pages`.
- The book page refreshes the drift banner live (`GET /api/books/<id>/drift/`) when the tab comes back, on focus, and
  when review saves a change in another tab.
- The manuscript's amber mark follows each page's current review state.
- A merge keeps both paragraphs' source pages.
- A page with no uncertain words reads «لا علامات».

**Endpoints** (under `/api/`, names in the `api` namespace): `book_guides` (GET the mode's compact state; POST a change
for all pages, a reset or an undo), `book_guides_preview` (POST), `page_guides_override` (POST merge / replace / undo),
`book_sheets?guides=1` (each sheet's bands and doubts), `review_drift` (GET). HTML: `books:start` («استخراج الصفحات»),
`books:start_ocr`, `books:delete`. The request and answer bodies are fixed in `books/fixtures/guides/` (`index.json`).

## 15. Troubleshooting

| Symptom | Fix |
|---|---|
| `NotSupportedError: PostgreSQL 15 or later is required (found 14.18)` | `.env` still points at 5432; use `postgres://localhost:5433/nassakh` (§2) |
| `connection refused` on 5433 | `brew services start postgresql@17`; check `pg_isready -p 5433` |
| default worker: every task fails with `Task handler raised error: ValueError('not enough values to unpack (expected 3, got 0)')` | the prefork children are spawned, not forked, on macOS / Python 3.13; `nassakh/celery.py` sets `FORKED_BY_MULTIPROCESSING=1` for that reason. If it reappears (custom launcher that bypasses `nassakh.celery`), export the variable before `celery worker`, or run the worker with `-P threads` |
| `tesseract language(s) missing: ['ara']` | `brew install tesseract-lang`; `tesseract --list-langs` must show `ara` and `eng` |
| page error «تعذّر تحميل محرّك التعرّف …» / `… is not prepared` | weights missing under `OCR_MODELS_DIR`: run `playground/poc/prepare_models.py` (`--mlx` for the MLX backend) or fix `OCR_MODELS_DIR` |
| gpu worker very slow or swapping | both models need about 9 GB; close other GPU-heavy apps, or set `OCR_BACKEND=mlx` (about 1.8× faster in the PoC) |
| an export stays «في الانتظار» | the worker was started before Phase 6 and does not consume the `export` queue: stop it and `make worker` again (`-Q default,layout,export`) |
| the export page fails with `relation "publishing_export" does not exist` | `make migrate` (`publishing.0003_export`) |
| `column books_book.awaits_ocr_start does not exist` | the database is older than Phase 7a: stop the workers, `make migrate`, start them again (§14) |
| a new book stays «تم التخطيط» | by design: look at the pages, then press «بدء المعالجة» (§14) |
| «لم تبدأ المعالجة بعد؛ اضغط «بدء المعالجة» أولًا.» on a re-run | the book is still in «التخطيط»: only «تجهيز الصفحات» runs before «بدء المعالجة» |
| dashboard does not update | it polls `/api/books/<id>/progress/` every 2 s only while the book is `processing` («قيد التخطيط») or `ocr`; check that the workers are running (`make worker`, `make gpu-worker`) |
| `NoReverseMatch` after moving routes | API routes are reversed as `api:<name>` (`book_progress`, `book_text`, `book_sheets`, `page_status`, `page_preprocess`, `page_guides_override`, `page_text`, `page_runs`, and the review names in §10) |
| review screen read-only | the user has no `proofreader` / `editor` / `admin` group (Django admin → Users), or the page has no final text yet |
