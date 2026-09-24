# Phase 2 specification — processing pipeline

Single source of truth for the agents building Phase 2. Read `docs/PLAN.md` (architecture), `docs/DECISIONS.md`
(D1–D18) and `DESIGN.md` (UI system) first. This file fixes names, fields, signatures and screens so that
independently built apps fit together. When this file and the plan disagree, this file wins.

## 0. Conventions (owner's style, non-negotiable)

- Django 6.1, Python 3.13, venv at `.venv` (`.venv/bin/python`, `.venv/bin/pytest`). Postgres DB `nassakh`
  (local, brew) for dev; **tests run on SQLite in memory** via `nassakh/settings_test.py`.
- **Function-based views only.** `views.py` thin: parse request → call a service → render/redirect.
  Logic lives in `services.py` (typed, docstrings). Celery tasks in `tasks.py` call services and return the
  `page_id`/`book_id` they received. JSON endpoints are DRF `@api_view` functions in `api.py`.
- Apps live at repo top level: `core`, `accounts`, `books`, `processing`, `ocr`. Project package: `nassakh/`.
- Templates: `templates/base.html`, `templates/<app>/<name>.html`. Alpine.js components small; larger ones in
  `static/src/js/<app>.js` as `Alpine.data(...)`. Tailwind v4 utilities + the component classes defined in
  `static/src/app.css`. **No CDN.** Vendored Alpine at `static/vendor/alpine.min.js`.
- **UI is Arabic, RTL** (`<html lang="ar" dir="rtl">`). All labels, messages, validation errors and status
  names in Arabic, written directly in templates/`verbose_name`/`choices`. Code, comments, commit messages in
  English. Use logical CSS (`ms-`, `me-`, `ps-`, `pe-`, `start-`, `end-`, `text-start`).
- Digits in the UI: Western 0–9. Never strip diacritics from stored text.
- Settings from environment via `django-environ` (`nassakh/settings.py` reads `.env`). All Nassakh-specific
  settings under one dict `settings.NASSAKH` (keys below).
- Storage: Django storage API only (`default_storage`, `FileField`). Paths from `core/storage.py` helpers.
  Never overwrite `Page.original_image` or `Book.source_pdf`.
- Errors shown to users: Arabic, actionable. Never raw exception text in headlines (store it in
  `error_message` for the details panel).
- Do not commit. Do not touch another app's directory. Shared files you may edit are listed per agent.

## 1. Dependencies

`pyproject.toml` `[project] dependencies`: django>=6.1,<6.2 · djangorestframework · django-environ ·
psycopg[binary] · celery[redis] · redis · pillow · pymupdf · opencv-python-headless · scikit-image · numpy ·
rapidfuzz · pytesseract · torch · torchvision · transformers>=4.51 · accelerate · safetensors · huggingface_hub.
Extras: `mlx = ["mlx-vlm"]`, `dev = ["pytest", "pytest-django", "factory-boy", "ruff"]`.
Install: `uv pip install --python .venv/bin/python -r pyproject.toml --extra dev`.
`[tool.pytest.ini_options]`: `DJANGO_SETTINGS_MODULE = "nassakh.settings_test"`, testpaths = the five apps.

Node (build time only): `package.json` devDependencies `tailwindcss`, `@tailwindcss/cli`, `alpinejs`,
`@fontsource/ibm-plex-sans-arabic`. Scripts: `vendor` (copy Alpine + woff2 files into `static/vendor`,
`static/fonts`), `build:css` (`tailwindcss -i static/src/app.css -o static/dist/app.css --minify`), `build`.
`static/dist/app.css` is committed so Node is not needed at runtime.

## 2. Settings (`nassakh/settings.py`)

```
env = environ.Env(); environ.Env.read_env(BASE_DIR / ".env")
DATABASES = {"default": env.db("DATABASE_URL", default="postgres://localhost:5432/nassakh")}
LANGUAGE_CODE = "ar"; TIME_ZONE = env("TIME_ZONE", default="Africa/Tripoli"); USE_TZ = True
STATIC_URL = "static/"; STATICFILES_DIRS = [BASE_DIR / "static"]; STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"; MEDIA_ROOT = env("MEDIA_ROOT", default=str(BASE_DIR / "media"))
LOGIN_URL = "accounts:login"; LOGIN_REDIRECT_URL = "books:list"; LOGOUT_REDIRECT_URL = "accounts:login"
CELERY_BROKER_URL = CELERY_RESULT_BACKEND = env("REDIS_URL", default="redis://localhost:6379/0")
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_ROUTES = {"ocr.tasks.ocr_page_full": {"queue": "gpu"}, "ocr.tasks.warm_up_engines": {"queue": "gpu"}}
CELERY_TASK_ACKS_LATE = True; CELERY_WORKER_PREFETCH_MULTIPLIER = 1; CELERY_TASK_TIME_LIMIT = 3600
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
NASSAKH = {
    "OCR_BACKEND": env("OCR_BACKEND", default="torch"),          # torch | mlx
    "OCR_DEVICE": env("OCR_DEVICE", default="auto"),             # auto | mps | cpu
    "OCR_MODELS_DIR": Path(env("OCR_MODELS_DIR", default=str(BASE_DIR / "playground/poc/models"))),
    "OCR_PRIMARY": env("OCR_PRIMARY", default="qari_v03"),
    "OCR_SECONDARY": env("OCR_SECONDARY", default="qari_v02"),
    "OCR_FAST": "tesseract",
    "TESSERACT_LANGS": env("TESSERACT_LANGS", default="ara+eng"),
    "RENDER_DPI": 300, "FOOTNOTE_UPSCALE": 2,
    "MAX_PIXELS": 2048 * 28 * 28, "MIN_PIXELS": 256 * 28 * 28,
    "MAX_NEW_TOKENS": {"page": 3000, "body": 2500, "footnote": 1000, "other": 600},
}
REST_FRAMEWORK: SessionAuthentication, IsAuthenticated default.
```
`nassakh/celery.py` standard (`app = Celery("nassakh")`, `config_from_object("django.conf:settings",
namespace="CELERY")`, `autodiscover_tasks()`); imported in `nassakh/__init__.py`.
`nassakh/settings_test.py`: `from .settings import *`, SQLite in-memory DB, `CELERY_TASK_ALWAYS_EAGER = True`,
`CELERY_TASK_EAGER_PROPAGATES = True`, `MEDIA_ROOT` = a temp dir, `PASSWORD_HASHERS` = MD5 for speed.

Root `Makefile` targets: `install`, `db` (createdb + migrate), `migrate`, `superuser`, `web` (runserver 8000),
`worker` (`celery -A nassakh worker -Q default -c 4 -l info`), `gpu-worker`
(`celery -A nassakh worker -Q gpu -c 1 -P solo -l info`), `css`, `css-watch`, `test`, `lint`, `seed-groups`.
`Procfile`: web, worker, gpu-worker lines (for honcho/overmind users).

## 3. Roles (`accounts`)

Django Groups `admin`, `editor`, `proofreader` created by a data migration. `core.decorators.role_required(*roles)`
(superuser always passes). Proofreader: review screens only (Phase 3); Editor: everything except user
management and deletion; Admin: all. Login/logout views (FBV wrappers around `django.contrib.auth` forms),
templates `accounts/login.html`. Users are managed in Django admin for now (link in sidebar for admins).

## 4. Models (exact field names; agents must not rename)

### books.Book
```
title CharField(300) "العنوان" · author CharField(300, blank) "المؤلف" · original_year PositiveIntegerField(null, blank) "سنة النشر الأصلية"
notes TextField(blank) "ملاحظات" · source_pdf FileField(upload_to=core.storage.book_source_path) "ملف PDF"
has_text_layer BooleanField(null=True) · source_page_count PositiveIntegerField(default=0)
skip_first, skip_last PositiveSmallIntegerField(default=0) · pages_per_sheet PositiveSmallIntegerField(default=1, choices 1/2)
split_ratio FloatField(default=0.5) · use_text_layer BooleanField(default=True) · digit_style CharField(default="western")
status CharField(choices Book.Status) · error_message TextField(blank)
created_by FK(User, null, on_delete=SET_NULL) · created_at auto_now_add · updated_at auto_now
Status: uploaded "مرفوع" · processing "قيد المعالجة" · needs_guides "بانتظار ضبط الأدلة" · ocr "قيد التعرّف على النص"
        · ready_for_review "جاهز للمراجعة" · reviewing "قيد المراجعة" · assembled "مُجمَّع" · error "خطأ"
methods: progress() -> dict[status -> count] over non-excluded pages; page_count property; refresh_status() (derive from pages)
```
### books.Page
```
book FK(CASCADE, related_name="pages") · number PositiveIntegerField (1-based book order)
source_index PositiveIntegerField (0-based PDF page) · source_half CharField(choices full/right/left, default full)
split_ratio_override FloatField(null) · original_image FileField(upload_to=core.storage.page_original_path)
width, height PositiveIntegerField(default=0) · dpi FloatField(default=0)
status CharField(choices Page.Status, default uploaded) · error_from CharField(blank) · error_message TextField(blank) · task_id CharField(blank)
attention_flags JSONField(default=list) · is_excluded BooleanField(default=False)
text_layer_text TextField(blank) · guides_override JSONField(null, blank)
provisional_text TextField(blank) · final_text TextField(blank)
text_state CharField(choices none "لا نص"/provisional "نص مبدئي"/final "نص نهائي", default none)
reviewed_by FK(User, null) · reviewed_at DateTimeField(null)
Status: uploaded "مرفوعة" · preprocessed "مُعالَجة" · layout_done "تم التخطيط" · ocr_done "تم التعرّف" · reviewed "مُراجَعة"
        · assembled "مُجمَّعة" · error "خطأ" · excluded "مستثناة"
Meta: unique_together (book, number); ordering ["book", "number"]
methods: set_error(stage: str, message: str) ; clear_error()
```
### processing.Preprocess (OneToOne Page, related_name="preprocess")
```
angle FloatField(0) · skew_confidence FloatField(0) · border_crop JSONField(list) · crop_box JSONField(list)
bg_kernel IntegerField(0) · nlm_h IntegerField(6) · sauvola_window IntegerField(41) · sauvola_k FloatField(0.2)
is_manual BooleanField(False) · auto_params JSONField(dict)   # the auto-detected values, for "reset"
gray_image, bw_image, display_image, thumbnail FileField(upload_to=core.storage.page_derived_path)
output_width, output_height PositiveIntegerField(0) · line_boxes JSONField(list) · median_line_height FloatField(0)
n_lines PositiveIntegerField(0) · footnote_rule_y IntegerField(null) · edge_strips_removed JSONField(list)
created_at, updated_at
```
Coordinates of `line_boxes`, `footnote_rule_y`, regions and Tesseract boxes are all in **gray_image pixel space**.
### processing.LayoutGuides (OneToOne Book, related_name="guides")
```
header_cut FloatField(null)      # ratio of gray height; everything above is running_header. None = no header
footnote_line FloatField(null)   # ratio; everything below is footnote. None = no footnotes
page_number_zone CharField(choices none/top/bottom, default bottom) · page_number_height FloatField(default 0.06)
reference_page FK(Page, null, SET_NULL) · source CharField(choices auto/manual, default auto) · updated_at
```
`Page.guides_override` JSON has the same keys and overrides the book values for that page.
### processing.Region
```
page FK(CASCADE, related_name="regions") · kind CharField(choices body/heading/running_header/page_number/footnote/poetry/other)
bbox JSONField [x0,y0,x1,y1] · order PositiveIntegerField · source CharField(choices guides/auto/manual, default guides)
Meta ordering ["page", "order"]
```
### ocr.OcrRun
```
page FK(CASCADE, related_name="ocr_runs") · region FK(Region, null, SET_NULL) · engine_name CharField(40)
model_id CharField(200, blank) · model_revision CharField(64, blank) · backend CharField(20) · prompt TextField(blank)
input_variant CharField(20)   # gray | bw | gray_2x
raw_output TextField(blank) · parsed_text TextField(blank) · params JSONField(dict) · duration_ms PositiveIntegerField(0)
output_tokens PositiveIntegerField(null) · finish CharField(20, blank) · looped BooleanField(False)
status CharField(choices ok/error, default ok) · error TextField(blank) · created_at
```
`params` for Tesseract runs also stores `{"lines": [{"bbox": [...], "words": [{"text": ..., "bbox": [...], "conf": ...}]}]}`.
### ocr.Line
```
page FK(CASCADE, related_name="lines") · order PositiveIntegerField · region FK(Region, null) · bbox JSONField(null)
text TextField · ocr_text TextField · tokens JSONField(list) · confidence FloatField(1.0) · n_low PositiveIntegerField(0)
is_reviewed BooleanField(False) · updated_by FK(User, null) · updated_at
Meta ordering ["page", "order"]
```
`tokens`: `[{"t": "الكلمة", "alt": "الكلمه" | null, "conf": "high" | "low", "digit": false, "bbox": [x0,y0,x1,y1] | null}]`.

## 5. Storage paths (`core/storage.py`)
`book_source_path(book, filename) -> "books/{book.id}/source.pdf"` ·
`page_original_path(page, filename) -> "books/{book_id}/pages/{number:04d}/original.png"` ·
`page_derived_path(preprocess, filename) -> "books/{book_id}/pages/{number:04d}/{filename}"` (filename one of
`gray.png`, `bw.png`, `display.webp`, `thumb.webp`). Helper `save_array(field, array_or_pil, filename)` writes a
numpy image / PIL image into a FileField via `ContentFile`. Media is served by Django in DEBUG; in production
behind a login-protected view (`core.views.protected_media`) — implement the view now, wire it in urls.

## 6. Services and tasks (signatures)

### core
- `core/arabic.py`: port `playground/poc/common.py` text functions: `to_western_digits`, `strip_tashkeel`, `fold_letters`,
  `normalize_ws`, `strip_markup`, `normalize(text, level)`, `truncate_repetition(text, hit_cap)`, `parse_output(raw, hit_cap)`,
  `is_digit_token(tok)`, `arabic_ratio(text)`.
- `core/images.py`: `smart_resize`, `prepare_image` (from PoC), `to_webp_bytes(array, max_width)`, `crop(array, bbox)`.
- `core/decorators.py`: `role_required(*roles)`.
- `core/templatetags/nassakh.py`: `status_dot(status)` (colour class per DESIGN.md mapping), `percent(a, b)`, `ar_status(page_or_book)`.
- `core/views.py`: `protected_media(request, path)` (login required; `FileResponse`).
- `core/context_processors.py`: `nav(request)` → `{"is_admin": ..., "role": ...}`.

### books
- `services.create_book(data: dict, pdf: UploadedFile, user) -> Book`
- `services.inspect_pdf(book) -> dict` sets `source_page_count`, `has_text_layer` (avg ≥ 200 chars/page and Arabic ratio ≥ 0.5).
- `services.ingest_book(book) -> list[Page]` (idempotent: existing pages kept): selects PDF pages `[skip_first, N-skip_last)`,
  renders at native DPI when the page is a single full-page image else at RENDER_DPI (port `playground/poc/extract_pages.py`
  `native_dpi`, `render_gray`, `find_gutter`), splits when `pages_per_sheet == 2` (right first), saves
  `original_image`, sets `text_layer_text` when `has_text_layer`, status `uploaded`.
- `services.start_processing(book)`: sets status processing, enqueues `tasks.ingest_book_task` which chains
  `group(processing.tasks.preprocess_page.s(pid) for pages) | books.tasks.after_preprocess.s(book_id)`.
- `services.book_progress(book) -> dict` `{"total", "by_status": {...}, "percent", "active": bool, "flags": n}`.
- `services.run_stage(page, stage: str)`: stage ∈ preprocess | layout | ocr | ocr_fast | ocr_full; enqueues the chain from that
  stage (`processing.tasks.preprocess_page → processing.tasks.layout_page → ocr.tasks.ocr_page_fast → ocr.tasks.ocr_page_full`).
- `tasks.ingest_book_task(book_id)`, `tasks.after_preprocess(results, book_id)`: proposes guides if the book has none
  (`processing.services.propose_guides`), sets book status `needs_guides` if proposal confidence is low else derives regions for all
  pages and enqueues `layout → ocr_fast → ocr_full` per page; `tasks.rerun_book_from(book_id, stage)`.
- Views (FBV): `list` `/books/`, `create` `/books/new/`, `detail` `/books/<id>/` (dashboard), `page_detail` `/books/<id>/pages/<n>/`,
  `toggle_exclude` POST, `start` POST, `rerun` POST (`?stage=`). API: `api.book_progress` GET `/api/books/<id>/progress/`,
  `api.page_status` GET `/api/pages/<id>/status/` (status, text_state, provisional_text, final_text, flags, error).
- Templates: `books/list.html`, `books/form.html`, `books/detail.html`, `books/page_detail.html`.

### processing
- `pipeline.py` (pure functions, unit-tested on synthetic images): port `playground/poc/preprocess.py`
  (`remove_dark_borders`, `flatten_background`, `estimate_skew`, `rotate`, `binarize_clean`, `content_bbox`, `detect_lines`,
  `detect_footnote_rule`) and add `remove_edge_strips(ink, gray) -> (gray, strips)` (D18: drop narrow ink bands at the left/right
  edge separated from the main text block by a gap ≥ 3× median inter-word gap or ≥ 4% of width; band width < 12% of width).
  `run_pipeline(gray: np.ndarray, params: PreprocessParams | None) -> PreprocessResult` where manual params (angle, crop_box,
  sauvola) override detection.
- `services.preprocess_page(page, manual: dict | None = None) -> Preprocess`: runs the pipeline, saves gray/bw/display(≤1400px webp)/
  thumb(≤240px webp), line boxes, footnote rule, flags (`large_skew`, `deskew_low_confidence`, `no_lines_detected`,
  `edge_strip_removed`), status `preprocessed`.
- `services.propose_guides(book) -> LayoutGuides`: footnote_line = median of `footnote_rule_y / output_height` over pages with a
  rule if ≥ 40% of pages have one else None; header_cut = None (Phase 2 leaves running-header detection manual);
  page_number_zone bottom. `confidence` returned alongside (fraction of pages with a rule).
- `services.effective_guides(page) -> dict` (book guides + page override).
- `services.derive_regions(page) -> list[Region]`: from effective guides in gray-image coordinates: running_header
  `[0, header_cut)`, body `[header_cut or 0, footnote_line or bottom-of-page-number-zone)`, footnote `[footnote_line, ...)`,
  page_number zone; deletes previous `source=guides` regions, keeps manual ones; status `layout_done`.
- `services.apply_guides(book, data: dict, user) -> LayoutGuides` saves and re-derives regions for all non-excluded pages,
  then enqueues OCR for pages whose regions changed (`books.services.run_stage(page, "ocr")`).
- `tasks.preprocess_page(page_id) -> page_id`, `tasks.layout_page(page_id) -> page_id`.
- Views: `guides` `/books/<id>/guides/` (GET screen, POST save → apply_guides); `page_preprocess` POST
  `/api/pages/<id>/preprocess/` (manual params → re-run preprocess synchronously if quick, else task; returns new image URLs);
  `page_guides_override` POST `/api/pages/<id>/guides/`.
- Templates: `processing/guides.html` (reference page image with two draggable horizontal lines and a page-number zone selector,
  Alpine component in `static/src/js/processing.js`), partial `processing/_preprocess_panel.html` (rotation slider −5..5 step 0.1,
  crop box handles, Sauvola k, "إعادة المعالجة", "استعادة القيم التلقائية") included by `books/page_detail.html`.

### ocr
- `engines/base.py`: `@dataclass OcrResult(text, duration_s, output_tokens, finish, looped=False, extra=dict)`;
  `class OcrEngine: name, backend, kind ("vlm"|"classic"|"pdf"); load(); recognize(image_path, max_new_tokens=None) -> OcrResult; unload();
  model_id, model_revision properties`.
- `engines/qari_torch.py`, `engines/qari_mlx.py`, `engines/tesseract.py` (`image_to_data` → lines/words with boxes; langs from settings),
  `engines/pdf_text.py` (repaired text layer, port `playground/poc/repair_text_layer.py`), `engines/fake.py` (deterministic, for tests).
  Port from `playground/poc/engines.py`; prompts from `playground/poc/config.py`. Model directories: `OCR_MODELS_DIR/qari-v0.3`,
  `OCR_MODELS_DIR/qari-v0.2-merged`, MLX under `OCR_MODELS_DIR/mlx/<name>`.
- `engines/registry.py`: `get_engine(name) -> OcrEngine` (module-level cache; loads once per process), `available_engines()`,
  `unload_all()`. Names: `qari_v03`, `qari_v02`, `tesseract`, `pdf_text`, `fake`. Backend from settings.
- `services.run_fast_ocr(page) -> None`: Tesseract on each region (bw image crops; page-level if no regions); stores OcrRuns with
  word/line boxes in params; `page.provisional_text` = joined region texts (body first, footnotes after a blank line; running_header
  and page_number omitted); `text_state = provisional`. Born-digital pages with `use_text_layer`: use `pdf_text` engine result as
  provisional **and** final immediately (`text_state = final`), Tesseract still runs for geometry.
- `services.run_full_ocr(page) -> None` (GPU): for each region (body, footnote at 2x, heading, other; skip running_header/page_number):
  primary engine → `sanity_check(text, tesseract_text)`; if it fails → secondary; if both fail → Tesseract text, flag `ocr_fallback`.
  Then run the secondary engine anyway on regions where the primary passed (for alternatives). All runs stored as OcrRun.
  Finally `finalize_page(page)`.
- `services.sanity_check(text, reference, looped) -> tuple[bool, str]`: fail if looped, if len(words) < 20% of reference words or
  > 180%, or word F1 with reference < 0.45 (lenient normalisation). Returns reason.
- `alignment.py`: `align_tokens(a: list[str], b: list[str]) -> list[tuple[int|None, int|None]]` (rapidfuzz/difflib on
  `normalize(..., "lenient")`); `build_lines(primary_text, secondary_text, tesseract_lines) -> list[dict]`: anchor primary tokens
  to Tesseract word boxes, assign each token to the Tesseract line whose words it matched (unmatched tokens inherit the previous
  token's line), attach `alt` from the secondary alignment when normalised forms differ, `conf="low"` when alt differs or token
  is a digit (D17); returns per-line dicts `{order, bbox, text, tokens, n_low}`.
- `services.finalize_page(page)`: builds `Line` rows (replace existing unreviewed lines), `final_text` (D6: convert Arabic-Indic
  digits to Western in `final_text` only, keep `Line.ocr_text` raw), `text_state = final`, status `ocr_done`; sets book status
  `ready_for_review` when all non-excluded pages are `ocr_done`.
- `tasks.ocr_page_fast(page_id) -> page_id` (default queue), `tasks.ocr_page_full(page_id) -> page_id` (gpu queue),
  `tasks.warm_up_engines()` (gpu).
- Views/API: `api.page_text` GET `/api/pages/<id>/text/` (lines with tokens), `api.page_runs` GET `/api/pages/<id>/runs/`.
  Partial `ocr/_text_panel.html` included by `books/page_detail.html`: shows provisional text in `text-text-3` with badge
  "نص مبدئي (Tesseract)" while `text_state == provisional`, swaps to final lines (low-confidence tokens highlighted with
  `bg-highlight`) when final; polls `/api/pages/<id>/status/` every 2 s while the page is active.

## 7. Task chain and statuses

```
ingest_book_task ─► group(preprocess_page ×N) ─► after_preprocess ─► [propose guides] ─► per page:
                    layout_page ─► ocr_page_fast (default queue, 0.5 s) ─► ocr_page_full (gpu queue) ─► finalize
Page.status: uploaded → preprocessed → layout_done → ocr_done   (error keeps error_from; excluded skipped everywhere)
Page.text_state: none → provisional (after ocr_page_fast) → final (after ocr_page_full)
Book.status: uploaded → processing → (needs_guides) → ocr → ready_for_review; error if ingest fails
```
Tasks are idempotent (re-running a stage overwrites that stage's outputs only), `bind=True`, `max_retries=2`,
`autoretry_for=(OSError,)`, store `page.task_id`, and on unexpected exceptions call `page.set_error(stage, message)` and re-raise
only for retryable errors. A page in `error` can be re-run from any stage.

## 8. Screens (Phase 2)

1. **Login** `accounts/login.html`: centred card, title "نسّاخ", username/password, Arabic errors.
2. **Books list** `/books/`: page header "الكتب" + primary button "كتاب جديد"; table rows: title/author, pages, status dot + label,
   progress bar (4px), updated time; empty state explains how to start.
3. **New book** `/books/new/`: form with metadata + PDF + ingest options (skip first/last, pages per sheet 1/2 segmented control,
   split ratio slider shown only for 2), help texts in Arabic.
4. **Book dashboard** `/books/<id>/`: header with title, author, year, status; action row (بدء المعالجة / ضبط الأدلة / إعادة التشغيل menu);
   stage progress (counts per status as bars); attention list (pages with flags/errors); pages grid of thumbnails (status dot, number,
   flags icon, excluded toggle on hover); Alpine polls `/api/books/<id>/progress/` every 2 s while active.
5. **Guides** `/books/<id>/guides/`: reference page (display image) with draggable header-cut and footnote lines, page-number zone
   selector, "تطبيق على كل الصفحات"; note on how many pages have a detected rule.
6. **Page detail** `/books/<id>/pages/<n>/`: image panel with tabs (الأصل / المعالَجة / أبيض وأسود) and region overlay; side panel
   with preprocessing controls (partial), text panel (partial, provisional → final), engine runs list (engine, variant, seconds,
   loop flag), re-run menu per stage, prev/next page navigation (keyboard ← → follow RTL reading direction: → = previous page).

Base layout per DESIGN.md: sidebar 232px (الكتب, المستخدمون for admins, تسجيل الخروج + user name), sticky top bar 52px with
page title slot, main column padding 32px. Component classes in `static/src/app.css`: `.btn`, `.btn-primary`, `.btn-secondary`,
`.btn-ghost`, `.btn-icon`, `.input`, `.select`, `.label`, `.help`, `.card`, `.badge`, `.dot`, `.progress`, `.progress-fill`,
`.table`, `.segmented`, `.toast`, `.banner`. Colours via `@theme` tokens: `--color-bg`, `--color-bg-subtle`, `--color-bg-muted`,
`--color-surface`, `--color-border`, `--color-border-strong`, `--color-text`, `--color-text-2`, `--color-text-3`, `--color-accent`,
`--color-accent-text`, `--color-accent-soft`, `--color-success`, `--color-warning`, `--color-warning-bg`, `--color-danger`,
`--color-neutral`, `--color-highlight`. Font: `--font-sans: "IBM Plex Sans Arabic", system-ui, sans-serif` with `@font-face`
from `/static/fonts/`. Status → colour: in progress = accent, done = success, pending = neutral, error = danger, attention = warning.

## 9. Tests (pytest, SQLite, Celery eager)
- core: arabic normalisation, digit conversion, loop truncation, markup stripping.
- books: `inspect_pdf` + `ingest_book` on a small generated PDF (PyMuPDF renders 3 pages of Arabic text with Amiri or any system
  font; one test with `pages_per_sheet=2`), skip_first/skip_last, `run_stage` enqueues, progress dict, views require login.
- processing: pipeline on synthetic pages (render Arabic lines with PIL, rotate by 2°, assert deskew within 0.3°; footnote rule
  detection with a drawn rule; edge-strip removal with a fake strip; line count), `propose_guides`, `derive_regions` geometry,
  `apply_guides` re-derives.
- ocr: registry with `fake`, `sanity_check` cases, `align_tokens`/`build_lines` (tokens land on the right lines; digits low),
  `run_fast_ocr` with the fake engine substituted for tesseract via settings override, `finalize_page` sets state and book status.

## 10. Runbook (`docs/RUNBOOK.md`)
Local setup on the Mac: brew services (postgres, redis), `make install`, `make db`, `make seed-groups`, `make superuser`,
`npm install && npm run build`, three terminals `make web` / `make worker` / `make gpu-worker`, `.env` keys, how to re-run a
page/stage, where files live, how to run tests, known limitations of Phase 2 (no review screen yet, users via admin, tables).
