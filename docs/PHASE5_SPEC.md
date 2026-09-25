# Phase 5 specification — Editor, book stylesheet and real-page preview

Approved by the owner on 2026-09-25 (decisions D40–D46 below). Read first: `docs/PLAN.md` §2–§7,
`docs/PHASE4_SPEC.md` (document schema §2.8, the manuscript view), `docs/DECISIONS.md` (D6, D22, D33–D39),
`DESIGN.md`, and the approved visual reference: the review screen (`templates/review/review.html`,
`static/src/components/review.css`, `static/src/js/review.js`), the dashboard viewer and side panel
(`templates/books/detail.html`, `static/src/components/theatre.css`, `static/src/js/books.js`) and the reworked
manuscript view (`templates/assembly/manuscript.html`, `static/src/js/manuscript.js`, `static/src/components/manuscript.css`).

Phase 5 turns the assembled manuscript into the book. The owner edits text and structure in a real editor that feels
like a modern, simpler Word; chooses the book's physical form (trim size, margins, fonts, type size); and sees the
**real pages**, the same WeasyPrint output Phase 6 exports as PDF, in the dashboard-style page viewer, with the page
count (footprint) per chapter and for the book. **UI/UX and the front end are the priority of this phase.**

## 0. Decisions (D40–D46)

- **D40 — Edit one chapter at a time.** The manuscript stays one JSON document; the editor opens one chapter (a
  level-1 heading and what follows; a book without headings is cut into sections of about 30 source pages, shown as
  «القسم 1، 2…») and saves it back by chapter id with a per-chapter version check. The chapters panel switches.
- **D41 — After the first editor save, the manuscript is the source of truth.** Review corrections no longer flow in.
  The dashboard, manuscript view and editor show «تغيّر نص N صفحة في المراجعة بعد التحرير»; per chapter the owner
  keeps the edited text or re-assembles that chapter from the reviewed pages (the old chapter kept as a snapshot).
- **D42 — WeasyPrint renders pages; never the browser.** Chosen over Playwright/Chrome (no page-foot footnotes, no
  real page numbers, no crop marks), Prince (commercial), Typst (Arabic still maturing, no EPUB) and LaTeX (heavy).
  Kept behind one interface (`publishing.engine`) so the engine can change later. Verified on this Mac (WeasyPrint 70):
  HarfBuzz shaping, justification, footnotes at the page foot, running headers, page numbers, recto openings, font
  embedding and subsetting; 0.1 s per chapter.
- **D43 — One book model, several renderers (Word first in Phase 6).** The manuscript is turned into a neutral book
  structure (`publishing/model.py`: front matter, chapters, blocks with style names, runs with marks, footnotes)
  consumed by the HTML+CSS renderer (preview, PDF, EPUB) and, in Phase 6, by the Word renderer (`python-docx`: real
  Word styles, real footnotes, RTL sections, mirrored margins, page setup from the stylesheet). Blocks carry style
  names only, never direct formatting. Word paginates slightly differently from WeasyPrint; the preview's page
  count is the PDF's.
- **D44 — Footprint.** The whole book is rendered in the background after a stylesheet change and after edits settle
  (debounced), cached by content hash; the chapter being edited renders first (seconds) with its page numbers offset
  from the last full render. Counts per chapter and for the book show in the panels.
- **D45 — Trim sizes and fonts.** Presets: 17×24 cm (default), A4, A5, B5, 14×21 cm, 12×17 cm, custom. Fonts: Amiri
  (vendored, OFL), Simplified Arabic, Traditional Arabic, Times New Roman and Lotus (loaded from the Mac's font
  folders, never committed: Monotype/Microsoft licences; all five allow PDF embedding). Aref Ruqaa dropped. Lotus has
  no Latin glyphs: Latin text and digits use the stylesheet's Latin face (default Times New Roman). EPUB embeds Amiri
  only; other faces fall back to Amiri there (Phase 6).
- **D46 — Footnotes numbered per page in every format (D35).** WeasyPrint's footnote counter is document-level
  (`counter-reset: footnote` on `@page` has no effect, verified), so the PDF renderer runs two passes: pass 1 learns
  the page of every footnote, pass 2 sets each call and marker from `data-n` (`content: attr(data-n)`); a third pass
  runs only if a page assignment moved. Word restarts natively. Options per chapter / per book remain.

## 1. Installed (done by the lead)
`weasyprint` 70, `ebooklib`, `fonttools` (pyproject); `esbuild` 0.28, `@tiptap/core` 3.31, `@tiptap/pm`,
`@tiptap/starter-kit`, `@tiptap/extension-placeholder`, `@tiptap/extension-character-count` (devDependencies).
The editor bundle is built by `npm run build:editor` (`scripts/build-editor.mjs` → `static/dist/editor.js`, committed
like `app.css`); Node stays build-time only.

## 2. Data model (`editor` and new `publishing` apps)

| Model | Fields |
|---|---|
| `editor.StyleSheet` (book 1:1) | `trim` preset key or `custom`, `width_mm`, `height_mm`, margins `top_mm bottom_mm inner_mm outer_mm`, `bleed_mm`, `body_font` (family key), `latin_font`, `heading_font`, `body_size_pt`, `line_height` (multiple), `indent_em`, `heading_scale` (h1 / h2 sizes as multiples), `footnote_size_pt`, `running_header` ∈ {none, book, chapter}, `page_number` ∈ {none, bottom_center, bottom_outer, top_outer}, `chapter_opening` ∈ {any, recto}, `front_matter` JSON {title_page, contents}, `print_source_pages` (bool: scan page marks in the margin), `updated_at` |
| `editor.Manuscript` | unchanged; `origin = editor` on save; `chapters()` helper splits by level-1 heading id (D40) |
| `editor.ManuscriptSnapshot` | unchanged; `reason` gains `edit` (before a chapter re-assembly, D41) |
| `publishing.PreviewRender` | book, `scope` ∈ {book, chapter}, `chapter_id`, `content_hash`, `status`, `page_count`, `chapters` JSON `[{id, title, first, last}]`, `folder`, `duration_ms`, `error`, `task_id`, `created_at` |

`publishing/fonts.py`: the font registry `{key: {name, files: {regular, bold}, licence, latin: bool}}` with the vendored
Amiri and the system paths (`~/Library/Fonts`, `/Library/Fonts`, `/System/Library/Fonts/Supplemental`), a check that
reports missing files in the stylesheet panel («الخط غير مثبّت على هذا الجهاز») and falls back to Amiri.

## 3. Services, tasks, API

- `editor.services`: `chapters_of(document)`, `chapter_document(book, chapter_id)`, `save_chapter(book, chapter_id,
  content, version, user)` (splice by id, version conflict → 409 with the current chapter), `snapshot(book, label,
  reason)`, `restore(book, snapshot_id)`, `find_replace(book, chapter_id | None, query, replacement, options)` with
  tashkeel-insensitive and alef-folded matching (uses `core.arabic`), `convert_digits(book, chapter_id, style)`,
  `review_drift(book)` (D41: chapters whose source pages changed in review since the first edit) and
  `reassemble_chapter(book, chapter_id, user)`.
- `publishing.model`: `book_model(document, stylesheet) -> Book(front, chapters[Chapter(id, title, blocks[Block(style,
  runs[Run(text, marks)], footnotes[])])])`; `publishing.html`: `render_html(model, stylesheet, scope)`;
  `publishing.css`: `stylesheet_css(stylesheet)` (`@page` size and margins, mirrored margins, `string-set` running
  headers, page numbers, footnote area, recto openings, the fonts' `@font-face` from the registry); `publishing.pdf`:
  `render_pdf(html, css, fonts) -> bytes, pages` with the D46 passes; `publishing.preview`: `render_preview(book, scope,
  chapter_id)` → PDF → page images (PyMuPDF, WebP at 1× ≈ 1100 px tall and 2×) under `media/books/<id>/preview/<hash>/`;
  `publishing.engine`: the interface the rest calls.
- Tasks (default queue): `render_chapter_preview(book_id, chapter_id)`, `render_book_preview(book_id)`; a new request
  for the same hash reuses the cache; a running book render is revoked and restarted when the hash changes.

| Method | URL (name) | Who | Body → response |
|---|---|---|---|
| GET | `/books/<id>/editor/[?chapter=<cid>]` (`editor:edit`) | login | the editor page |
| GET | `/api/books/<id>/chapters/` (`api:chapters`) | login | `[{id, title, version, blocks, words, pages: {first, last} \| null, drift: bool}]` |
| GET | `/api/books/<id>/chapters/<cid>/` (`api:chapter`) | login | `{id, version, content, warnings}` |
| PUT | `/api/books/<id>/chapters/<cid>/` (`api:chapter`) | editor, admin | `{content, version}` → `{version}` or 409 `{version, content}` |
| POST | `/api/books/<id>/chapters/<cid>/reassemble/` | editor, admin | → 202 run (D41) |
| POST | `/api/books/<id>/find-replace/` (`api:find_replace`) | editor, admin | `{chapter, query, replacement, match_tashkeel, fold_alef, whole_word, replace: bool}` → `{matches: [{chapter, block, index, length}], replaced}` |
| POST | `/api/books/<id>/convert-digits/` | editor, admin | `{chapter, style}` → `{changed}` |
| GET/POST | `/api/books/<id>/snapshots/` and `/<sid>/restore/` | login / editor | list, create, restore |
| GET/PUT | `/api/books/<id>/stylesheet/` (`api:stylesheet`) | login / editor | the stylesheet; PUT validates and enqueues the renders |
| GET | `/api/books/<id>/preview/?scope=book\|chapter&chapter=<cid>` (`api:preview`) | login | `{status, hash, page_count, chapters, pages: [{n, url, url2x, chapter}], stale}` |
| GET | `/books/<id>/layout/` (`editor:layout`) | login | the stylesheet + preview page |

Dashboard (`book_progress` / config): `editor` state {edited, version, drift_pages}, `layout` state {trim, page_count,
rendering}; primary button after the manuscript is fresh: «فتح المحرّر»; the «⋯» menu gains «تنسيق الكتاب ومعاينته».

## 4. UI — the editor `/books/<id>/editor/`

Direction: **a modern Word, simpler**, in the Nassakh vocabulary. The owner is a publisher: everything they expect from
Word's print layout is there (a page-shaped sheet, a style picker, bold/italic, find & replace, undo/redo, word count,
zoom), nothing they don't need (no ribbon, no per-paragraph fonts, no colours). Looks come from the stylesheet.

- **Top bar**: the book title, chapter title as meta; save pill (`= rv-save`: «محفوظ», «يُحفظ…», «تعذّر الحفظ · إعادة
  المحاولة»); undo / redo buttons; the primary «معاينة الصفحات» (opens the layout page on this chapter); «⋯» menu:
  «تنسيق الكتاب», «نسخة محفوظة…» (snapshots), «تحويل الأرقام», «إعادة تجميع الفصل من المراجعة» (when drift), «المخطوطة»,
  «لوحة الكتاب».
- **Toolbar** (hangs from the top bar, = bk-toolbar): the **style picker** (a menu button showing the current block
  style: عنوان الكتاب · عنوان فصل · عنوان فرعي · فقرة · اقتباس · شعر · ملاحظة وسط · فاصل · حاشية) with keys
  (⌘⌥1/2/0…), **B / I** (marks, rendered in every format), **حاشية** (inserts a footnote at the caret and opens its
  small editor), **بحث واستبدال** (⌘F), zoom (segmented 90 / 100 / 120 %), **فواصل الصفحات الأصلية** toggle (the scan
  page marks from Phase 4, shown as faint inline marks «ص 12»).
- **The sheet**: a white page-shaped surface on the muted background, its width the trim width at the zoom (17×24 →
  about 640 px at 100 %), the stylesheet's margins as the text measure, the body face and size from the stylesheet
  (Amiri 12 pt → 16 px at 100 %), justified, first-line indents, headings centred. It flows continuously (no fake
  page breaks; real pages are the preview). Uncertain words keep the amber underline with the readings popover
  (primary / secondary / Tesseract / typed) reused from the review vocabulary; footnote calls are superscript
  buttons that open the note's inline editor; a caret in a paragraph shows its source badge «ص 12» in the margin and
  «عرض الأصل» (O) opens the source drawer from the manuscript view.
- **Side panel** (end side, = bk-side): «الفصول» (chapters with word and page counts, the current one highlighted,
  drift badge «تغيّر في المراجعة»), «الأصل» (the scan of the block under the caret with its lines highlighted, the
  Phase 4 drawer as a pane), «ملاحظات» (assembly warnings still open in this chapter, jump to block).
- **Find & replace**: a panel under the toolbar (= review popover style), «مطابقة التشكيل», «توحيد الألف», «كلمة
  كاملة», count «3 من 41», Enter next, ⇧Enter previous, «استبدال» / «استبدال الكل» with an undo toast.
- **Status line** (bottom of the sheet column, 12 px): «الفصل 3 من 14 · 2 184 كلمة · ص 31–58 في الكتاب» (pages from
  the last render, «يُحسب…» while rendering).
- **Autosave**: per chapter, 1.5 s after the last change, version-checked; a 409 shows «تغيّر هذا الفصل في نافذة
  أخرى» with «إعادة التحميل»; leaving with unsaved changes warns. Snapshots: «حفظ نسخة باسم…», list with restore.
- **Keyboard**: ⌘S save now, ⌘Z/⇧⌘Z, ⌘F find, ⌘B/⌘I, ⌘⌥1/2/0 heading 1 / 2 / paragraph, ⌘⇧F footnote, O source,
  ⌘[ / ⌘] previous / next chapter, ? shortcut sheet. No shortcut fires inside the find fields.
- **Reduced motion**: no transitions; **RTL**: the sheet is `dir="rtl"`, Latin runs isolate correctly (`unicode-bidi`).
- **Performance**: a chapter of 30 source pages (≈ 15 k words) types without lag; the panel lists 800 chapters cheaply.

## 5. UI — stylesheet and preview `/books/<id>/layout/`

- **Layout**: the dashboard's grid: the page viewer (D33: one page or a spread, turns, keys, wheel, filmstrip in the
  side panel, fit to the window, «صفحة 37 من 412 · الفصل الثالث ص 31–58») in the main column; a **stylesheet panel**
  on the end side (= bk-side sections): «القطع» (presets as a segmented list with a small page-shape glyph, custom
  fields), «الهوامش» (four steppers with a live page diagram; inner/outer), «الخطوط» (body, Latin, heading: menus
  with a sample line in each face; a missing font shows «غير مثبّت»), «النص» (size, line height, indent), «الصفحة»
  (running header, page number, chapter opening, contents page, source page marks). Every change saves (PUT) and
  re-renders; the viewer shows a quiet «تُحدَّث المعاينة…» pill and swaps pages in place when ready, keeping the
  current page.
- **Footprint** block at the top of the panel: «412 صفحة» with the delta from the last change («+12») and the
  per-chapter list (click → that chapter's first page); «يُحسب…» while the book render runs.
- **Spread mode** (toggle in the toolbar): two facing pages, recto on the left (Arabic books), one page on narrow
  windows. **Zoom**: fit height (default), fit width, 100 %.
- **Rendering states**: skeleton pages with the shimmer while nothing is cached; an error state with the Arabic
  headline and «إعادة المحاولة»; stale (edits after the render) shows the pill «المعاينة أقدم من النص».
- The dashboard's side panel gains a «الكتاب» block: trim and page count, «تنسيق الكتاب ومعاينته».

## 6. Tests
- `editor/tests.py`: chapter split and splice (ids stable, versions, 409), autosave conflicts, snapshots and restore,
  find & replace with and without tashkeel and alef folding, digit conversion, review drift and chapter re-assembly,
  permissions, query counts.
- `publishing/tests.py`: book model from a Phase 4 document (every node type), stylesheet → CSS golden snippets,
  the fonts registry (missing files), WeasyPrint on a small book: page count, footnotes on the right page and numbered
  per page across pages (D46), running headers, recto openings, mirrored margins, page images written, cache by hash,
  task revoke/restart.
- `core/test_editor_ui.py`, `core/test_layout_ui.py`: rendered templates (states), the Alpine components under the
  Node harness (autosave and conflict, find panel, style picker, footnote editor, source pane, stylesheet panel
  changes → PUT + poll, viewer swap keeping the page); the esbuild bundle exists and `node --check`s.

## 7. Ownership for the build agents
- **Backend**: `editor/` (models, migrations, services, api, urls, admin, tests), `publishing/` (model, html, css, pdf,
  fonts, preview, engine, tasks, api, urls, tests), `books/services.py` (dashboard states), settings, `nassakh/urls.py`,
  a placeholder view + template for `editor:edit` and `editor:layout` that the UI agents replace.
- **Editor UI**: `scripts/build-editor.mjs`, `static/src/editor/*.js` (TipTap schema from the Phase 4 nodes, extensions),
  `static/dist/editor.js`, `templates/editor/edit.html` + partials, `static/src/js/editor.js` (Alpine `bookEditor`),
  `static/src/components/editor.css`, `core/test_editor_ui.py`, `templates/base.html` include.
- **Layout UI**: `templates/editor/layout.html` + partials, `static/src/js/layout.js` (Alpine `bookLayout`, reusing the
  dashboard viewer's turn/filmstrip logic through a shared module if practical), `static/src/components/layout.css`,
  `core/test_layout_ui.py`, the dashboard integration (`templates/books/detail.html`, `static/src/js/books.js`,
  `static/src/components/theatre.css`).
- Nobody writes to the development database; real books (12, 13, 15, 17) are previewed in memory.

## 8. Acceptance
- [ ] From the dashboard, «فتح المحرّر» opens the first chapter in under a second; typing is instant; autosave shows.
- [ ] Style picker, B/I, footnotes, find & replace with tashkeel options, digit conversion, snapshots and restore work.
- [ ] «الأصل» shows the scan of the paragraph under the caret with its lines highlighted.
- [ ] The layout page shows real pages; changing the trim size or font re-renders and the footprint updates.
- [ ] Footnotes sit at the page foot, numbered per page; running headers and page numbers follow the settings;
      chapters open on the recto when set.
- [ ] A review edit after editing shows the drift and per-chapter re-assembly works, keeping a snapshot.
- [ ] Reduced motion, keyboard reach, RTL, Western digits, Arabic copy throughout.

## 9. Amendment D47 — one book page with live pages (supersedes §4 and the editor page of §7)

The book page `/books/<id>/layout/` (title «الكتاب») is the only place to preview and edit. `/books/<id>/editor/`
redirects there with `?mode=edit` and the chapter.

### 9.1 Layout export (backend)
- Every WeasyPrint render (chapter or book) also produces a **layout**: per page `{n, side: "right"|"left", width_pt,
  height_pt, margins, lines: [...], header: {...}|null, number: {...}|null, footnote_rule: {x, y, w}|null}`; each line
  `{block, kind: "body"|"heading"|"note"|"title"|..., x, y, w, h, baseline, dir: "rtl"|"ltr", justify: bool, start, end,
  runs: [{text, font, size_pt, weight, italic, sup: bool, note: id|null}]}` in points from the page's top-left, where
  `start`/`end` are character offsets into the block's plain text (footnote calls counted as one object placeholder, their
  note text laid out as lines with `block = note id`). Extracted from WeasyPrint's box tree (`document.pages[i]._page_box`:
  LineBox / TextBox positions, `element.get('data-block')`); the renderer tags blocks and notes with `data-block` and each
  footnote call with `data-note`. Stored per render as `layout.json` beside the page images; served per page range.
- **Fast re-layout**: `relayout_chapter(book, chapter_id, version)` renders layout only (no rasterisation) on a dedicated
  Celery queue `layout` (the default worker consumes `default,layout`); images for the filmstrip follow in a separate low
  priority task. Target: < 1 s for a 30-page chapter. A newer request for the same chapter supersedes a queued one.
- **Offsets across chapters**: the chapter render uses the first page number and side from the current layout of the
  chapters before it; when a chapter's page count changes, later chapters' numbers shift by the delta and their sides
  flip on an odd delta (mirrored margins swap; with recto chapter openings a blank page appears or disappears). The
  service returns `{chapter pages, delta, shifted_from, blank_changes}` so the client can renumber at once; the full book
  layout is confirmed in the background. Books without chapter page breaks (sections): re-lay-out forward section by
  section until a section's first line lands on the same page and position as before (convergence), capped at the book.
- **Paragraph attributes** in the document schema: `breakBefore: bool` («ابدأ صفحة جديدة»), `keepWithNext: bool`; the
  stylesheet gains `widows`, `orphans` (default 2) and `keep_headings` (default true).
- **Book details** in `StyleSheet.front_matter.fields`: title, subtitle, author, editor, translator, publisher, city,
  year, edition, isbn, rights (title and author default from the Book); the renderer prints a title page and a
  copyright page when enabled (also used by the Word/EPUB metadata in Phase 6).
- **Page checks** returned with each render: `[{code, page, block, message}]` for a footnote that overflowed its page, a
  missing font, a chapter ending on an almost empty page, a heading at the foot of a page.
- **Uncertain words** API `api:uncertain`: every remaining `uncertain` mark in the manuscript as `{chapter, block, start,
  end, word, page, context, readings: [{engine, label, text}]}` (readings resolved from the source lines' tokens), plus
  accept / choose-reading / type endpoints that edit the manuscript (version-checked) and trigger the re-layout.

### 9.2 The page (UI)
- **Stage**: the dashboard viewer (turns, keys, wheel, spread, fit modes) drawing **live pages**: a white page of the trim
  proportions scaled to the fit, its lines absolutely positioned from the layout (same `@font-face` families as the render,
  each line `width = w`, justified when `justify`, runs with their weight/italic/superscript), the footnote rule, footnote
  lines, running header and page number. Pages outside the viewport are not in the DOM. Text is selectable.
- **Modes**: segmented «معاينة | تحرير» in the toolbar, E toggles; double-click in preview enters edit at the clicked line.
  In edit mode a click maps the point to (block, offset) via `caretPositionFromPoint` on the line text + the line's `start`,
  and opens that paragraph as an in-place editor (a one-block TipTap instance from the bundle, same width, face, size, line
  height, indent) over its lines; the lines below on that page shift by the height difference while typing; a paragraph
  that continues on the next page hides its continuation while open. Enter at the end creates the next paragraph, Backspace
  at the start merges with the previous, ↑ on the first line / ↓ on the last line move to the neighbouring block, Esc
  closes the paragraph then leaves edit mode. After a 500 ms pause: patch the chapter JSON, PUT it (version, 409 banner),
  request the re-layout, then swap the affected pages in place with the new layout (no jump, the open paragraph stays open
  at the caret), update footprint and page numbers.
- **Toolbar in edit mode**: undo/redo (per chapter), style picker, B / I, «حاشية», «فواصل الصفحات الأصلية»; the primary
  «تم». Preview: spread, fit, jump, counter, «تحرير». Top bar: title «الكتاب», save pill, render pill, «⋯» (snapshots,
  «تحويل الأرقام», chapter re-assembly on drift, «إخراج PDF» from the last render, «المخطوطة», «لوحة الكتاب»).
- **One side panel** (end side, the dashboard's .bk-side look, one scrolling column) with an **icon tab bar** (icon +
  tooltip, active tab labelled, badges): «الفصول» (footprint with delta, chapters with page ranges and drift, page checks),
  «الصفحات» (thumbnails), «بحث» (find & replace over the chapter model; matches listed with page numbers; click turns to
  the page and highlights the line; replace re-lays-out), «التنسيق» (accordions: القطع، الهوامش، الخطوط، النص، الصفحة،
  بيانات الكتاب), «الفقرة» (style, «ابدأ صفحة جديدة», «مع التالية», source pages), «الأصل» (exactly the editor page's pane:
  scan thumbnail with the block's line bands, pager, «عرض الأصل» drawer, review link), «غير المؤكَّدة» (the uncertain list
  grouped by chapter and page with context; click turns to the page and selects the word; readings / typed correction /
  accept in place; count badge). Preview opens «التنسيق», edit opens «الأصل»; the owner's choice is remembered per mode.
- **Space**: on this page the app's navigation sidebar folds into an icon rail (toggle to expand).
- **Keyboard**: preview ←/→, PageUp/Down, Home/End, G, S, 1/2/3, E, ?; edit ⌘S, ⌘Z/⇧⌘Z, ⌘F, ⌘B/⌘I, ⌘⌥1/2/0/3–6 styles,
  ⌘⇧F footnote, ⌘[ / ⌘], O source, Esc. Nothing fires inside fields.

