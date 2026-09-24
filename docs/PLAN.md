# Nassakh (نسّاخ) — Implementation Plan

Status: **Phases 1-3 built and reviewed; Phase 4 (assembly, manuscript view) built 2026-09-24, awaiting the owner's test; decisions D1–D39.** Phase specs in `docs/PHASE2_SPEC.md`, `docs/PHASE3_SPEC.md`, `docs/DASHBOARD_SPEC.md`, `docs/PHASE4_SPEC.md`. Run instructions in `docs/RUNBOOK.md`.
v2 integrates the owner's answers and the inspection of the four sample PDFs in `playground/poc/input/`.
Once Phase 1 starts, the decisions below are copied into `docs/DECISIONS.md`.

---

## 0. Starting point

| Item | State |
|---|---|
| Django | 6.1.1 project `nassakh/` scaffolded by `startproject`; SQLite; no apps; empty `templates/` |
| Python | 3.13.7 in `.venv`; `uv` available; PyMuPDF 1.28 installed (used for the inspection) |
| Node | 22.19 (build time only: Tailwind CLI, TipTap bundle) |
| Services | PostgreSQL 14 and Redis running locally via Homebrew |
| WeasyPrint deps | pango, cairo, harfbuzz, libffi present |
| Fonts | Amiri, IBM Plex Sans Arabic, Tajawal installed system-wide; OFL fonts will still be vendored in the repo |
| Hardware | Apple M5 Pro, 24 GB unified memory. **Everything runs on this Mac**; no production target for now |
| Git | not initialised |

### The four samples

| Sample | Pages | What it is | Findings that shape the design |
|---|---|---|---|
| 1 — *بعض الملامح التاريخية عن ليبيا* (1966) | 23 | One scan per page, 1654×2339 JPEG (~200 DPI), no text layer | Pages 1–4 are covers/title pages → **skip first/last N pages**. Clean typeset body, page number centred at bottom (— ٨ —), light bleed-through, slight skew, Latin words inline |
| 2 — Libyan texts (Herodotus…) | 19 sheets | **Two book pages per sheet**, landscape, PDF page rotation = 90°, 1700×2338 JPEG, no text layer | **Split each sheet into two pages; right half is the earlier page** (RTL). Footnotes under a short rule with markers ١ ٢ ٣, small footnote type. Staples in the gutter |
| 3 — *مساق في الهوية والدولة* | 6 | Born-digital Word PDF, full text layer, **tables**, bold/italic, Western digits | Text layer is corrupted: every lam-alef ligature is reversed (الإسلام → اإلسالم) → text-layer engine needs a **glyph-level lam-alef repair** and OCR cross-check. **Tables deferred** to a later phase |
| 4 — Christianity in Roman Libya (article) | 20 | Low-resolution photocopies, ~900×1300 px embedded (≈135 DPI), no text layer | Skew 1–2°, dark scan border, hole punches, handwritten marginalia, bold sub-headings, **footnotes under a rule with (٢) (٣) markers**, page number bottom. Hardest case; test **2× upscaling** before OCR |

Embedded scans are lower resolution than a 300 DPI render would be, so rendering would only upsample. **Decision: extract the embedded image natively when a PDF page is a single full-page image (applying the page rotation); render at 300 DPI otherwise.**

---

## 0b. Project goal

**Nassakh is done when one person at the publishing house, with no developer involved, can drop the scanned PDF of a
public-domain Arabic book of up to 800 pages into the browser and, within a single working week, generate a print-ready
PDF, a screen PDF and an EPUB that a reader cannot distinguish from a freshly typeset edition.** The process is visible
from the first second: pages clean themselves as they are processed, each page's text appears in a faded first pass and
settles into the final text, and the clean text of any page or of the whole book can be copied with one click. The
machine reads a clean scan with fewer than 3 errors per 100 characters and a poor photocopy with fewer than 8, and it
marks exactly where it is unsure, so verifying a page against its scan takes minutes rather than the hour retyping
takes. Every paragraph keeps a link to the scan it came from, diacritics survive untouched, digits and footnotes come
out as the house wants them, and the same book can be re-exported any time with another trim size or font from one
stylesheet. Everything runs on a single Mac today and a Linux server tomorrow. Experience goals: D22.

## 1. Architecture (Mac-native)

```
Browser — Arabic, RTL — Django templates + Alpine.js + Tailwind
   │  full-page HTML  +  small JSON endpoints (DRF function views) for autosave / polling
   ▼
Django dev server (runserver)          ┌───────────────────────┬─────────────────────────────┐
   │ ORM            │ Celery / Redis   │ worker (CPU)          │ gpu-worker (Apple Silicon)  │
   ▼                ▼                  │ queue: default        │ queue: gpu, --pool=solo     │
PostgreSQL 14    Redis                 │ render, preprocess,   │ OCR engines loaded once     │
                                       │ layout, assembly,     │ backend: MLX or PyTorch MPS │
                                       │ export                │                             │
                                       └───────────────────────┴─────────────────────────────┘
Files: Django storage API → local disk under MEDIA_ROOT (S3/MinIO later via django-storages)
```

- Three local processes started from a `Procfile` / `Makefile` (`make web`, `make worker`, `make gpu-worker`).
- Heavy work is always a Celery task; views only enqueue and read status. Progress via Alpine polling every 2 s.
- Docker Compose and the nginx → uWSGI deployment files are **deferred to an optional final step**; the code stays deployable (env settings, no Mac-only paths outside the OCR backend).

---

## 2. Django apps

| App | Owns | Notable modules |
|---|---|---|
| `core` | Shared helpers: storage paths, image I/O, Arabic text utils (digit conversion, tashkeel strip for comparison only, lam-alef repair), role decorators, progress helpers, template tags | `arabic.py`, `images.py`, `decorators.py`, `templatetags/` |
| `accounts` | Login/logout, roles via Django Groups (`admin`, `editor`, `proofreader`) | `views.py`, `services.py` |
| `books` | `Book`, `Page`; upload + metadata + **ingest options** (skip first/last pages, pages per sheet, split position); text-layer detection; page extraction/rendering; book dashboard with per-stage progress | `services.py`, `ingest.py`, `tasks.py` |
| `processing` | Stage 2 + 3: `Preprocess` (params + outputs), line detection, **`LayoutGuides` (book master + per-page override)**, `Region` derivation, thumbnail review, manual override UI (rotation slider, crop handles, guide lines, split divider) | `deskew.py`, `crop.py`, `clean.py`, `lines.py`, `guides.py`, `layout.py`, `services.py`, `tasks.py` |
| `ocr` | `OcrEngine` interface + registry, engines, `OcrRun`, `Line`, dual-model word diff | `engines/{base,registry,qari_mlx,qari_torch,tesseract,pdf_text,fake}.py`, `diff.py`, `align.py`, `tasks.py` |
| `review` | Stage 5 screen: side-by-side, line ↔ box linking, low-confidence words, autosave, keyboard nav, mark reviewed | `views.py`, `api.py`, `services.py` |
| `assembly` | Stage 6: header/page-number removal, cross-page paragraph merge, footnote linking, heading detection, **digit normalisation** → manuscript JSON; `AssemblyRun` warnings | `services.py` (pure functions), `tasks.py` |
| `editor` | `Manuscript`, `ManuscriptSnapshot`, `StyleSheet`; TipTap page; save/snapshot/find-replace/convert-digits API; "show original"; live preview | `views.py`, `api.py`, `schema.py` |
| `publishing` | Stage 8: `BookTemplate` presets, `Export`; manuscript → HTML + CSS Paged Media → WeasyPrint (print / screen PDF) and EPUB 3 | `html.py`, `css.py`, `pdf.py`, `epub.py`, `tasks.py` |

Conventions (owner style): function-based views; `views.py` thin; logic in `services.py` with type hints and docstrings; Celery tasks in `tasks.py` call services; JSON endpoints as DRF `@api_view` functions in `api.py`; settings from environment (`django-environ`). Apps at repo top level; project package stays `nassakh/`.

---

## 3. Data model

```
Book 1──n Page 1──1 Preprocess
     │           1──n Region
     │           1──n OcrRun
     │           1──n Line
     1──1 LayoutGuides (master; Page.guides_override JSON overrides per page)
     1──n AssemblyRun ──1 Manuscript 1──n ManuscriptSnapshot
     1──1 StyleSheet ──n BookTemplate (preset FK)
     1──n Export
```

| Model (app) | Fields (abridged) |
|---|---|
| `Book` (books) | title, author, original_year, notes, source_pdf (never modified), has_text_layer, source_page_count, page_count, status, **skip_first, skip_last, pages_per_sheet ∈ {1, 2}, split_ratio (default 0.5)**, settings JSON (footnote numbering scope, `digit_style` default `western`, use_text_layer), created_by, timestamps |
| `Page` (books) | book, number (1-based book order), **source_index (PDF page), source_half ∈ {full, right, left}, split_ratio_override**, original_image, width/height, status, error_message, attention_flags JSON, is_excluded, reviewed_by/at, text_layer_text, **guides_override JSON** |
| `Preprocess` (processing) | page 1:1, angle, crop_box, upscale_factor, denoise_strength, normalize_background, sauvola_window, sauvola_k, is_manual, auto_confidence, gray_image, bw_image, display_image (WebP), thumbnail, line_boxes JSON |
| `LayoutGuides` (processing) | book 1:1, **header_cut_y** (ratio; everything above is dropped as running header), **footnote_line_y** (ratio; everything below is footnote), **page_number_zone** (top/bottom/none + height), reference_page, source ∈ {auto, manual} |
| `Region` (processing) | page, kind ∈ {body, heading, running_header, page_number, footnote, poetry, other}, bbox, order, confidence, source ∈ {guides, auto, manual}, line_indices JSON |
| `OcrRun` (ocr) | page, region (nullable), engine_name, model_id, model_revision, backend ∈ {mlx, torch-mps, cpu, pdf}, prompt, input_variant ∈ {gray, gray_2x, bw, original}, raw_output, parsed_text, params JSON, duration_ms, status, error, created_at |
| `Line` (ocr) | page, region, order, bbox (nullable), text (current), ocr_text (primary original), alternatives JSON (`[{start, end, primary, secondary}]`), confidence, is_reviewed, updated_by/at |
| `AssemblyRun` (assembly) | book, manuscript, settings JSON, warnings JSON, created_at |
| `Manuscript` / `ManuscriptSnapshot` (editor) | document JSON (ProseMirror), version, label, updated_by/at |
| `StyleSheet` (editor) | book 1:1, template FK, trim size, margins (top/bottom/inner/outer), bleed, fonts, styles JSON, running_header_mode, page_number_style (**western digits default**), chapter_opening ∈ {any, recto} |
| `BookTemplate` (publishing) | name (Arabic), trim size, default margins/fonts, base CSS fragment, ornaments; seeded by data migration |
| `Export` (publishing) | book, snapshot, format ∈ {print_pdf, screen_pdf, epub}, settings JSON, file, status, duration_ms, log, created_by/at |

Source mapping: every block node in the manuscript JSON carries `attrs.sourcePages` and `attrs.sourceLineIds`; "show original" opens the scan with those lines highlighted.

Page state machine: `uploaded → preprocessed → layout_done → ocr_done → reviewed → assembled`, plus `error` (keeps `error_from`) and `excluded`. `run_stage(page, stage)` re-runs one stage for one page; tasks are idempotent and never touch originals. Book status is derived from pages.

---

## 4. Library choices and why

| Concern | Choice | Considered | Reason |
|---|---|---|---|
| PDF → images | **PyMuPDF** | pdf2image | Native embedded-image extraction (samples are ≤ 200 DPI, rendering would only upsample), rotation handling (sample 2), text-layer access, glyph-level `rawdict` for the lam-alef repair |
| Image processing | **OpenCV headless + scikit-image** | Pillow | Deskew, crop, background flattening, Sauvola, line detection |
| OCR models | **Qari v0.3** primary, **Qari v0.2** secondary, **Tesseract `ara+eng`** always (geometry, sanity check, fallback). KITAB dropped | KITAB LoRA | PoC result (D14–D16): v0.3 most reliable, v0.2 most accurate when it works, Tesseract never collapses and provides line/word boxes. **PDF text layer** with lam-alef repair replaces OCR for born-digital books |
| Inference on the Mac | **MLX (`mlx-vlm`)** as the fast Apple-Silicon backend, **PyTorch MPS + transformers** as the reference backend | vLLM | vLLM is CUDA-only. MLX runs Qwen2-VL natively on Apple Silicon and is typically 2–4× faster than MPS; the Qari checkpoints are Qwen2-VL-2B fine-tunes and convert with `mlx_vlm.convert` (bf16, no quantisation, to protect accuracy). PoC compares both on identical pages; MLX is adopted only if its CER matches MPS within noise. Both sit behind the same `OcrEngine` class, chosen by `OCR_BACKEND` |
| GPU worker link | **Celery `gpu` queue** | internal HTTP | One task infrastructure; model resident per worker process (`--pool=solo`) |
| Layout analysis | **Master guides + heuristics** behind a `LayoutAnalyzer` interface | Surya, Kraken | Owner's request: set the footnote line and header cut once per book, apply to all pages, adjust per page. Deterministic and easy to understand. Auto-detection proposes the guide positions (horizontal rule near the bottom, repeated top line). Surya's weights are non-commercial above a revenue threshold; Kraken is heavy |
| Task queue | **Celery + Redis** | Django 6 `django.tasks` | Built-in framework has no worker or queue routing |
| Web API | **DRF** function views | JsonResponse | Validation with Arabic messages; still FBV style |
| CSS | **Tailwind v4** via `@tailwindcss/cli`, built file committed | CDN | DESIGN.md tokens as `@theme` variables; logical utilities for RTL |
| JS | **Alpine.js 3** vendored | — | As required |
| Editor | **TipTap 2** (MIT) bundled with esbuild | ProseMirror raw, CKEditor 5 | Good RTL, custom semantic nodes, ProseMirror JSON. CKEditor is GPL/commercial |
| Preview | **Paged.js** in an iframe with the export CSS | — | Approximate live pagination; WeasyPrint is the truth |
| Typesetting | **WeasyPrint** | Chromium + Paged.js, XeTeX | Native footnotes, mirrored pages, running headers via `string-set`, TOC page numbers via `target-counter`, bookmarks, bleed/crop marks, font embedding, HarfBuzz shaping |
| EPUB | **ebooklib** | hand-built zip | RTL spine direction, embedded fonts |
| Metrics (PoC) | **jiwer** + **rapidfuzz** | — | CER/WER; token alignment for the dual-model diff |
| Fonts (output) | **Amiri**, **Scheherazade New**, **Noto Naskh Arabic**, heading option **Aref Ruqaa** (all OFL) | — | Vendored in `static/fonts/` |
| Font (UI) | **IBM Plex Sans Arabic** | Tajawal | Neutral, excellent at 12–14 px |
| Storage | Django storage API | — | Local disk now, S3 later without code changes |
| Settings / tests | `django-environ`; pytest + pytest-django + factory-boy; Celery eager mode in tests | — | — |

---

## 5. Key algorithms

- **Ingest**: for each PDF page in `[skip_first, N − skip_last)`, extract the embedded image if the page is one full-page image (apply rotation), else render at 300 DPI. If `pages_per_sheet = 2`, cut at `split_ratio` (per-page override) into **right page then left page**. Per-page exclude toggle for stray covers/blank pages. Born-digital PDFs get `has_text_layer = true` and the option to skip OCR.
- **Deskew**: binarise → coarse angle (Hough / `minAreaRect`) → refine by maximising projection-profile variance over ±5° in 0.1° steps. Confidence from peak sharpness; `|angle| > 3°` or low confidence ⇒ attention flag.
- **Crop**: remove dark scan borders (sample 4), morphological close → largest text block → bbox + constant margin; narrow ink strips at the edge that belong to the facing page are dropped (D18).
- **Clean**: background flattening (divide by large blur) → mild non-local-means → speck removal by component area (small enough to keep diacritics). Output grayscale PNG for Qari; Sauvola B&W for Tesseract and display. 2× upscaling was erratic in the PoC and is not applied (D15).
- **Line detection**: horizontal projection profile of the cropped, deskewed block; smooth; valleys separate lines; merge slivers < 35% of median line height.
- **Guides → regions**: `y < header_cut_y` ⇒ `running_header`; `y > footnote_line_y` ⇒ `footnote`; page-number zone ⇒ `page_number`; everything else `body`. Heuristics then refine inside body: short isolated/bold line ⇒ `heading`; paired lines with central gap ⇒ `poetry`. Auto-proposal of guides: detect the footnote rule (long thin horizontal component in the lower third) and a repeated top line across pages.
- **Dual-model diff**: strip v0.3 markup → whitespace tokens → opcodes via rapidfuzz/SequenceMatcher → disagreeing tokens become `alternatives` spans. Diacritic differences count as disagreement.
- **Line alignment**: Qari returns no line breaks (D12), so Qari words are anchored to Tesseract word boxes by fuzzy alignment; the detected line boxes then give each word its line. Pages where the anchoring is poor are flagged.
- **Paragraph merge**: last body paragraph of page *n* lacks terminal punctuation (`. ؟ ! : » ) ]`) and the next page's first body paragraph is not a heading/poetry ⇒ merge with a space; record both pages. Poetry never merges.
- **Footnotes**: markers `(١)`, `(1)`, `[١]`, superscripts, bare digit after a word (sample 2); footnote lines starting with the same marker are linked within the page; unmatched ⇒ warnings. Renumber per chapter (default) or per book.
- **Digits**: OCR raw output is stored untouched. At assembly, Arabic-Indic digits are converted to **Western digits (0–9) by default**; the editor has a one-click "convert digits" action and find & replace. Page numbers and footnote markers in output use Western digits.
- **Text-layer repair** (born-digital): using `rawdict` glyph boxes, two consecutive chars `ا`+`ل` produced by a single glyph ⇒ `لا`; then NFKC. Validated against OCR of the rendered page in the PoC.

---

## 6. Arabic and RTL specifics

- `<html lang="ar" dir="rtl">`, `LANGUAGE_CODE = 'ar'`, Arabic copy written directly in templates.
- Stored text is never normalised except the explicit digit conversion above. Tashkeel/alef folding exists only as options in metrics and find & replace.
- Book geometry: in an Arabic book the recto (odd) page is the **left-hand page**; chapter openings break to odd pages; the gutter margin flips accordingly. Setting, default on.
- Kashida justification is unavailable in WeasyPrint; justified text uses inter-word spacing.

---

## 7. Phases

### Phase 1 — Proof of concept (scripts only, `playground/poc/`)

Pages (≈17): sample 1 → 4 body pages (after the covers); sample 2 → 3 sheets split into 6 pages; sample 3 → 3 rendered pages (tables included, text layer as near-free ground truth after repair); sample 4 → 4 pages.

Scripts:
1. `extract_pages.py` — native extraction / render, rotation, sheet splitting → `pages/`.
2. `preprocess.py` — deskew, crop, clean → `gray/`, `gray_2x/` (sample 4), `bw/`, line boxes overlay for visual check, auto-proposed footnote rule.
3. `run_ocr.py` — engines × variants × backends → `runs/*.json` (model id, revision, backend, prompt, `max_pixels`, `max_new_tokens`, raw output, duration). Engines: Qari v0.2.2.1, Qari v0.3, KITAB LoRA, Tesseract `ara`. Backends: PyTorch MPS for all; MLX for the best engine on the same pages.
4. `repair_text_layer.py` — sample 3 text-layer repair; `gt/*.txt` — ground truth drafted by me from the best output and corrected against each scan.
5. `evaluate.py` → `REPORT.md`: CER/WER raw, without tashkeel, lenient-normalised; per-page seconds by backend; line-count match rate; v0.3 structure quality notes; failure gallery (small footnote text, numerals, headers, marginalia).

Exit criteria: primary/secondary engine, input variant (incl. whether 2× upscaling helps sample 4), backend (MLX vs MPS), `max_pixels`, whether KITAB LoRA stays, whether the text-layer repair is reliable enough to skip OCR for born-digital books.

Runtime: a full grid on MPS is several hours (≈1–2 min per page per model); MLX should cut that substantially. Model downloads ≈ 4.5 GB each.

### Phase 2 — Processing pipeline
`git init`, `pyproject.toml` (uv), Postgres/Redis settings, `.env.example`, Celery `default`/`gpu` queues, `Procfile`/`Makefile`, apps `core` `accounts` `books` `processing` `ocr` with migrations; ingest form with skip/split options; page extraction task; preprocessing tasks; thumbnail review with attention flags; manual override (rotation slider, crop handles, split divider); **guides screen** (draw header cut and footnote line on a reference page → apply to all → per-page adjust); line detection; region derivation; `OcrEngine` registry with MLX/MPS/Tesseract/PDF-text engines; dual-model OCR task; per-page re-run; roles + login; base template from DESIGN.md. Tests on synthetic pages.

### Phase 3 — Review screen
Split view, hover/focus linking both ways, low-confidence words with accept-A / accept-B / type, per-line autosave, keyboard shortcuts, book progress view.

### Phase 4 — Assembly
Pure-function pipeline → ProseMirror JSON with source mapping; digit conversion; `AssemblyRun` warnings; "Convert to book" action; re-assembly keeps the previous manuscript as a snapshot. Thorough unit tests.

### Phase 5 — Editor
TipTap bundle with semantic nodes (title, chapter, section, body, quote, poetry with two hemistichs, footnote, centred note, separator); style picker; `StyleSheet` form; "show original" drawer; diacritics-aware find & replace; convert-digits action; undo/redo; autosave; snapshots; Paged.js preview.

### Phase 6 — Export
HTML + CSS Paged Media from manuscript + StyleSheet; front matter; TOC with page numbers; footnotes at page bottom; running headers; chapter openings on recto; ornaments. Print PDF (bleed, crop marks, embedded fonts), screen PDF (outline), EPUB 3 (RTL). `Export` history.

### Later (not scheduled)
- **Tables** (sample 3): table node in the editor, table extraction from born-digital PDFs, table rendering in export.
- Docker Compose and nginx → uWSGI deployment files, S3 storage, dewarp for spine curvature.

At the end of each phase: summary, how to run/test, proposal for the next step, then wait for go-ahead.

---

## 8. Repository layout (target)

```
Nassakh/
├── manage.py  pyproject.toml  package.json  Procfile  Makefile  .env.example
├── nassakh/                  # settings.py, urls.py, celery.py, wsgi.py, asgi.py
├── core/ accounts/ books/ processing/ ocr/ review/ assembly/ editor/ publishing/
├── templates/                # base.html, partials/, <app>/
├── static/  src/css  src/js  vendor/  fonts/  dist/
├── playground/poc/           # input/ pages/ gray/ bw/ runs/ gt/ REPORT.md + scripts
├── docs/                     # PLAN.md, DECISIONS.md, RUNBOOK.md
└── tests/                    # shared fixtures; app tests in <app>/tests/
```

Media layout: `media/books/{book_id}/source.pdf`, `pages/{n:04d}/original.png|gray.png|bw.png|display.webp|thumb.webp`, `exports/{export_id}.pdf|epub`.

---

## 9. Non-functional handling (local)

- Async everywhere; per-page idempotent tasks with retry; `error` status with an actionable Arabic message and a retry button.
- GPU worker: one process, models resident, sequential v0.2 → v0.3 per page (both fit comfortably in 24 GB).
- Review concurrency: per-line autosave with `updated_at` check; soft "someone else is here" indicator.
- Security: all views behind login; media served through a protected Django view (nginx `X-Accel-Redirect` later).
- Observability: task ids on `Page`/`Export`; structured logs; optional Flower.

---

## 10. Owner decisions (2026-09-23)

| Topic | Decision |
|---|---|
| Samples | Four PDFs in `playground/poc/input/`; findings in §0 |
| Covers / stray pages | Skip first/last N at ingest + per-page exclude toggle |
| Two pages per sheet | Ingest option; split position per book with per-page override; right page first |
| Footnotes / running headers | Master guide lines per book, applied to all pages, adjustable per page (§5) |
| Tables | Deferred to a later phase |
| Environment | Everything runs on the Mac; no production plans. MLX vs MPS decided in the PoC |
| Team | Owner works alone; ground truth drafted by me, owner spot-checks |
| Digits | **Always Western digits (0–9)** in UI and output; OCR raw kept, converted at assembly, editor action to convert |

## 11. Remaining assumptions (proceeding unless told otherwise)

- `git init` at the start of Phase 1; time zone `Africa/Tripoli`.
- Footnotes renumbered per chapter by default; chapter openings on the recto; both configurable.
- OFL fonts bundled in the repo.
- Plain Django username/password auth (single user for now, roles kept for later).
- Dewarp as a parameter placeholder; Surya/Kraken only if guides + heuristics fail.
