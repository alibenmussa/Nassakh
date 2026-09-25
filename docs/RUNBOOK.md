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
create books, start processing, re-run stages, apply guides and change preprocessing parameters;
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
Book:  uploaded → processing → ocr → ready_for_review                         (error when the PDF cannot be read)
Page:  uploaded → preprocessed → layout_done → ocr_done                       (error keeps error_from; excluded pages are skipped)
Text:  none → provisional (Tesseract, after ocr_page_fast) → final (after ocr_page_full)
```

- **Ingest** renders the selected PDF pages `[skip_first, N − skip_last)` at the scan's native DPI (D2), splits
  two-page sheets at the detected gutter, right page first (D3), and stores `original.png` per page.
- **Preprocess** deskews, flattens, removes dark borders and facing-page strips (D18), crops, binarises, detects lines
  and, **on each page separately**: the footnote separator (solid, dotted, dashed or short rule;
  `Preprocess.footnote_rule_y`), else a block of smaller type at the bottom (`footnote_block_y`), and the printed
  page number (a short isolated first/last line in the top 12 % / bottom 15 %; `page_number_box`). Flags
  `large_skew`, `deskew_low_confidence`, `no_lines_detected`, `edge_strip_removed`.
- **Guides**: the book-level proposal (median rule position and its confidence) is computed for the guides screen
  only. Processing never stops for guides: every book goes straight on to layout + OCR (`needs_guides` is kept as a
  status value but is no longer set). The lines on «ضبط الأدلة» are a manual fallback: the header cut applies to
  every page; the footnote line and page-number zone apply only after «تطبيق على كل الصفحات» (guides become
  `manual`) and only on pages where nothing was detected.
- **Layout** derives the regions per page (`processing.services.page_layout`). Footnote top: page override →
  detected rule → detected smaller-type block → manual book footnote line → none (the body runs to the bottom; an
  automatically proposed book line is never applied). Page number: page override → detected box (+6 px) → manual
  book zone → none (no automatic bottom zone). A page can override from its detail screen
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
| Dashboard `/books/<id>/` → «إعادة التشغيل» | all non-excluded pages from the chosen stage (`books.tasks.rerun_book_from`). Runs the per-page chain directly (the guides proposal of §4 is not repeated; nothing waits for it). Also resumes a book an earlier version parked in `needs_guides` |
| Page detail `/books/<id>/pages/<n>/` → «إعادة التشغيل» | this page from the chosen stage (`books.services.run_stage(page, stage)`) |
| Page detail error banner → «إعادة المحاولة من هذه المرحلة» | the failed stage again |
| Dashboard → «تحتاج انتباهًا» → «إعادة <stage>» on a failed page | the failed stage again, then back to the dashboard (same `books:rerun` with `next`) |
| Page detail → panel «المعالجة الأولية» → «إعادة المعالجة» / «استعادة القيم التلقائية» | preprocessing only, with manual angle / crop / Sauvola / denoise values (`POST /api/pages/<id>/preprocess/`); regions are re-derived (for very large originals the worker runs preprocess → layout, answer 202), OCR is **not** re-run: use the re-run menu → «التعرّف على النص» afterwards |
| Guides `/books/<id>/guides/` → «تطبيق على كل الصفحات» | regions re-derived for every preprocessed page; OCR re-queued only for pages whose regions changed |
| Dashboard / page detail → «استثناء الصفحة» | excludes a page from every stage and from the book's progress; toggle again to bring it back |

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
`smoke_pipeline` does exactly that and prints one row per page:

```sh
CELERY_TASK_ALWAYS_EAGER=true LOG_LEVEL=INFO .venv/bin/python manage.py smoke_pipeline \
    "playground/poc/input/sample 2.pdf" --pages-per-sheet 2 --skip-first 2 --skip-last 15
```

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
- Running-header detection is manual: set the header cut on the guides screen. Headings and poetry inside the
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

**Known limits.** WeasyPrint cannot fake bold or italic: Lotus (no bold file) prints headings regular, Arabic italic prints
upright. A single footnote longer than a page spills over. Word export (Phase 6) paginates slightly differently, so its page
count can differ from the preview's.

## 13. Troubleshooting

| Symptom | Fix |
|---|---|
| `NotSupportedError: PostgreSQL 15 or later is required (found 14.18)` | `.env` still points at 5432; use `postgres://localhost:5433/nassakh` (§2) |
| `connection refused` on 5433 | `brew services start postgresql@17`; check `pg_isready -p 5433` |
| default worker: every task fails with `Task handler raised error: ValueError('not enough values to unpack (expected 3, got 0)')` | the prefork children are spawned, not forked, on macOS / Python 3.13; `nassakh/celery.py` sets `FORKED_BY_MULTIPROCESSING=1` for that reason. If it reappears (custom launcher that bypasses `nassakh.celery`), export the variable before `celery worker`, or run the worker with `-P threads` |
| `tesseract language(s) missing: ['ara']` | `brew install tesseract-lang`; `tesseract --list-langs` must show `ara` and `eng` |
| page error «تعذّر تحميل محرّك التعرّف …» / `… is not prepared` | weights missing under `OCR_MODELS_DIR`: run `playground/poc/prepare_models.py` (`--mlx` for the MLX backend) or fix `OCR_MODELS_DIR` |
| gpu worker very slow or swapping | both models need about 9 GB; close other GPU-heavy apps, or set `OCR_BACKEND=mlx` (about 1.8× faster in the PoC) |
| dashboard does not update | it polls `/api/books/<id>/progress/` every 2 s only while the book is `processing` or `ocr`; check that the workers are running (`make worker`, `make gpu-worker`) |
| `NoReverseMatch` after moving routes | API routes are reversed as `api:<name>` (`book_progress`, `book_text`, `book_sheets`, `page_status`, `page_preprocess`, `page_guides_override`, `page_text`, `page_runs`, and the review names in §10) |
| review screen read-only | the user has no `proofreader` / `editor` / `admin` group (Django admin → Users), or the page has no final text yet |
