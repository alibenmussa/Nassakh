# Phase 5 plan — Editor, book stylesheet and real-page preview (draft, awaiting the owner's go)

Read first: `docs/PLAN.md` §2–§7, `docs/PHASE4_SPEC.md` (document schema §2.8), `docs/DECISIONS.md` (D6, D22, D33–D39),
`DESIGN.md`, and the approved visual reference (review screen, dashboard viewer and side panel).

Phase 5 turns the assembled manuscript into the book: the owner edits text and structure in a real editor, chooses the
book's physical form (trim size, margins, fonts, type size), and sees the **real pages** — the same WeasyPrint output
Phase 6 exports — in a viewer that looks like a clean PDF, with the page count (footprint) of each chapter and of the book.

## 0. Decisions to confirm (proposed D40–D45)

- **D40 — Edit one chapter at a time.** An 800-page book in one editable document is slow in any browser editor. The
  manuscript stays one JSON document, but the editor opens one chapter (a level-1 heading and what follows it; a book
  without headings is cut into sections of about 30 source pages) and saves it back into the document by chapter id,
  with a per-chapter version check. The contents panel switches chapters.
- **D41 — After the first editor save, the manuscript is the source of truth.** Review corrections no longer flow in by
  themselves. The manuscript view and the editor show «تغيّر نص N صفحة في المراجعة بعد التحرير» with two choices per
  chapter: keep the edited text, or re-assemble that chapter from the reviewed pages (the old chapter kept as a snapshot).
- **D42 — The preview is WeasyPrint, never the browser.** Pages are rendered on the server by the same code Phase 6
  exports with, turned into page images (PyMuPDF, already installed) and shown in the dashboard-style page viewer: one
  page at a time, turns, filmstrip, fit to the window. What you see is what you export.
- **D43 — Footprint.** The whole book is rendered in the background after every stylesheet change and after edits
  settle (debounced), cached by content hash; the chapter being edited re-renders first (seconds), with its page numbers
  offset from the last full render. The page count per chapter and for the book shows in the side panel.
- **D44 — Trim sizes.** Presets: A4 (210×297), A5 (148×210), B5 (176×250), 17×24 cm, 14×21 cm, 12×17 cm, plus custom.
  Default 17×24 cm (the common size for Arabic books of this kind); A5 is the trade alternative.
- **D45 — Fonts.** Body faces (OFL, vendored): Amiri (already in `static/fonts/amiri/`), Scheherazade New and Noto Naskh
  Arabic (downloaded once into `static/fonts/`); headings may use Aref Ruqaa. Footnotes numbered per page by default
  (D35), which WeasyPrint does natively with a counter reset on each page.

## 1. Installs (one-off, needs the network)
- Python: `weasyprint` (pango, cairo and harfbuzz are already installed with Homebrew), `ebooklib` for Phase 6.
- JS build: `esbuild` and TipTap core packages (MIT, no paid extensions) → one bundle `static/dist/editor.js`,
  committed like `app.css`; Node stays build-time only.

## 2. Data model
- `editor.StyleSheet` (book 1:1): trim size (preset or custom width/height mm), margins (top, bottom, inner, outer),
  bleed, body font, heading font, body size (pt), line height, paragraph indent, heading scale, footnote size,
  running header mode (none / book title / chapter title), page number position and style (Western digits), chapter
  opening (any page / recto — the left-hand page in an Arabic book), front matter options (title page, contents).
- `editor.Manuscript` gains nothing new except `origin = editor` on save; chapters are addressed by their heading ids.
- `editor.PreviewRender`: book, scope (book | chapter id), content hash, status, page count, per-chapter page ranges,
  page image folder, duration, error — a cache, rebuilt when the hash changes.

## 3. The editor (`/books/<id>/editor/`)
- **Layout** (the review screen's vocabulary): top bar with the book title, save pill («محفوظ» / «يُحفظ…» / error with
  retry), undo/redo; toolbar with the block style picker; the chapter being edited in the main column; side panel on
  the end side with tabs «الفصول» (chapters with their page counts, current highlighted), «الأصل» (the scan of the block
  under the cursor with its lines highlighted — the Phase 4 source drawer, now a pane) and «ملاحظات» (assembly warnings
  still open).
- **Block styles** (semantic, from the Phase 4 schema): title, chapter heading, section heading, paragraph, quote,
  poetry (two hemistichs per verse, a table-like node), centred note, separator, footnote (inline, edited in a small
  popover). The owner never picks fonts per paragraph: looks come from the stylesheet.
- **Editing aids**: diacritics-aware find & replace (match with or without tashkeel, alef forms folded as an option),
  «تحويل الأرقام» (Western ↔ Arabic-Indic for a selection or the chapter), uncertain words still underlined in amber
  with their readings on click (primary, secondary, Tesseract), keyboard shortcuts shown in a sheet (?), autosave
  (debounced, per chapter, version-checked), named snapshots and restore.
- **Page seams** from Phase 4 stay visible as a faint inline mark in the editor (they become the original-page markers
  a scholarly edition can print in the margin, an option in the stylesheet).

## 4. Stylesheet and preview (`/books/<id>/layout/` or a mode of the editor)
- **Stylesheet panel**: trim size presets as a segmented list with a small page-shape icon, margins with a live
  diagram, fonts with a sample line in each face, type size and line height steppers, chapter opening, running header,
  page numbers. Every change saves and re-renders.
- **Preview viewer**: the dashboard page viewer (one page, turns, keys, wheel, filmstrip), showing spreads (two facing
  pages, recto on the left in RTL) as an option; the page on screen and the chapter under the cursor stay in sync with
  the editor; «صفحة 37 من 412 · الفصل الثالث ص 31–58».
- **Footprint**: book page count, per-chapter counts, and the change caused by the last stylesheet edit («+12 صفحة»).
- **Render pipeline** (`publishing/` app, shared with Phase 6): manuscript JSON + stylesheet → HTML + CSS Paged Media
  (`@page` size, margins, running headers with `string-set`, footnotes with `float: footnote` and a per-page counter,
  chapter openings, Arabic justification by inter-word spacing) → WeasyPrint PDF → page images (WebP, 1× and 2×) under
  `media/books/<id>/preview/<hash>/`. Celery default queue; the chapter render first, the full book after.

## 5. Performance targets (800 pages)
- Opening a chapter: < 1 s; typing latency unaffected by book size (only the chapter is in the editor).
- Chapter preview after a pause in typing: a few seconds; full-book footprint in the background (about 1–3 min for 800
  pages), cancelled and restarted when something changes.
- Preview images lazy-loaded in the filmstrip; only the visible page at full resolution.

## 6. Tests
- Chapter split and splice (ids stable, version conflicts), autosave, snapshots, find & replace with and without
  tashkeel, digit conversion, the stylesheet → CSS generation (golden snippets), WeasyPrint rendering of a small book
  (page count, footnotes on the right page, running headers, recto openings), preview cache by hash, and the editor and
  viewer components under the Node harness.

## 7. Build order (agents, once approved)
1. Backend: installs, StyleSheet and PreviewRender models, chapter split/splice API, the `publishing` render pipeline
   (HTML + CSS + WeasyPrint + page images), tasks and tests.
2. Editor UI (TipTap bundle, schema from Phase 4, editing aids, side panel) on the working backend.
3. Stylesheet panel and preview viewer.
4. Verification after the owner's test.

## 8. What is ready and what is not
- Ready: the document schema (Phase 4), the manuscript storage and snapshots, the source links, the page viewer and
  side panel patterns to reuse, the fonts Amiri and Aref Ruqaa.
- Needs the owner: confirm D40–D45 (especially D41, what happens to review corrections after editing, and the default
  trim size), and allow the installs in §1 (network access for pip/npm and the two font downloads).
- Phase 6 then adds the final exports on the same pipeline: print PDF (bleed, crop marks, embedded fonts), screen PDF
  (outline, links), EPUB 3 (RTL), front matter, contents with page numbers, export history.
