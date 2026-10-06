# Phase 4 specification — Assembly: from reviewed pages to one manuscript

Single source of truth for Phase 4. Read first: `docs/baseline/PLAN.md` §2–§5 and §7 (Phase 4), `docs/DECISIONS.md`
(D6 digits, D11 regions, D22 experience goals, D32 line roles, D33–D34 dashboard), `DESIGN.md`, and the approved
visual reference: `templates/review/review.html`, `static/src/components/review.css`, `static/src/js/review.js`,
plus the dashboard viewer and side panel (`templates/books/detail.html`, `static/src/components/theatre.css`).

Assembly turns a book's page lines into **one continuous manuscript**: running headers and page numbers left out,
paragraphs joined across page breaks, headings from the reviewer's line roles, footnotes linked to their markers,
punctuation placed and digits normalised, every block linked to the scan lines it came from. The manuscript is a
ProseMirror/TipTap JSON document that the Phase 5 editor opens and Phase 6 exports.

The **manuscript view** is where the owner verifies the assembly (D22: viewing verifies the machine's work; it is
not a reading view). Its tools are always visible: page seams with a join/split control, the source scan of any
block, heading suggestions, footnote links and a warnings list. UI/UX is the priority of this phase.

## 0. Decisions taken for this phase (D35–D38; D39 covers the OCR line fix built alongside)

- **D35 — Assembly may run before every page is reviewed.** OCR'd pages that are not reviewed yet are included
  and flagged (a warning per page and a mark on their blocks). Pages still in the pipeline or in error are skipped
  with a warning; excluded pages are skipped silently. Footnote numbering is **per chapter** by default; per book
  or as printed (restart on each page) are options chosen in the convert popover and remembered per book. The
  numbers in the manuscript are for display; exported numbering follows the stylesheet (Phase 6).
- **D36 — `assembled` means "in the current manuscript, unchanged since".** After a successful run, included
  pages that were `reviewed` become `assembled`. The review screen still edits them: any change to an assembled
  page puts it back to `reviewed`, and the manuscript shows as out of date until it is re-assembled.
- **D37 — Assembly normalises typography in the derived text only.** The primary model writes a period or comma
  glued to the next word (`تاكنست .وتاكنست` for `تاكنست. وتاكنست`, seen on real pages); assembly moves such marks
  back, drops spaces before `. ، ؛ : ؟ !`, collapses spaces and (setting, on by default) removes tatweel
  (U+0640), which is a justification artefact of the print. Stored lines and tokens are never changed;
  diacritics are never touched; digits follow the book's `digit_style` (Western by default, D6).
- **D38 — Assembly overrides live with the book.** Seam overrides (join / split at a page boundary), dismissed
  heading suggestions and the assembly options are stored in `Book.assembly_settings` and re-applied by every run.
  Structure fixes that are line facts (a heading) go through the review service (`set_line_role`), so they are
  recorded and undoable like any review action.

## 1. Data model

New app **`assembly`** (pipeline, runs) and new app **`editor`** (the manuscript; Phase 5 adds the editor UI).
Both in `INSTALLED_APPS` and in pytest `testpaths`.

| Model | Fields |
|---|---|
| `editor.Manuscript` | `book` 1:1 (`related_name="manuscript"`), `document` JSON, `version` (int, +1 per save), `origin` ∈ {`assembly`, `editor`}, `run` FK `assembly.AssemblyRun` (nullable, the run that produced it), `updated_by` (nullable), `created_at`, `updated_at` |
| `editor.ManuscriptSnapshot` | `manuscript` FK, `document` JSON, `version`, `label` (Arabic), `reason` ∈ {`reassembly`, `manual`}, `created_by`, `created_at`. A re-assembly snapshots the previous document first; assembly-origin snapshots beyond the newest 10 are pruned |
| `assembly.AssemblyRun` | `book` FK, `status` ∈ {`queued`, `running`, `done`, `error`}, `stage` (current step key, §4.2), `settings` JSON (the options used), `warnings` JSON (list, §2.9), `stats` JSON (§2.10), `included` JSON (`{page_id: {"number", "reviewed", "sig"}}`, for staleness), `duration_ms`, `error`, `task_id`, `created_by`, `created_at`, `finished_at` |
| `books.Book.assembly_settings` | JSON, default `{}`: `footnote_numbering` ∈ {`chapter`, `book`, `page`} (default `chapter`), `include_unreviewed` (default true), `strip_tatweel` (default true), `seams` (`{"<page number>": "join" | "split"}`), `dismissed_suggestions` (list of block ids) |

Line geometry: `ocr.Line.bbox` is `[x0, y0, x1, y1]` in gray-image pixels; the page size for ratios is the
page's `Preprocess.output_width/output_height` (fall back to `Page.width/height`). Region kinds: `footnote` →
the page's notes; `running_header` and `page_number` are skipped (`ocr.services.SKIPPED_KINDS`); every other kind
(and a line without region) is body.

## 2. The pipeline (`assembly/pipeline.py`, pure functions, no ORM)

Inputs are plain dataclasses built by one loader in `assembly/services.py` (a fixed number of queries,
whatever the page count: pages with preprocess, lines with region kind, ordered):

```python
@dataclass
class LineIn:  id: int; order: int; kind: str  # "body" | "footnote"
               role: str; text: str; box: tuple[float, float, float, float] | None  # ratios of the page
               uncertain: list[int]  # token indexes still unresolved (conf == "low" and no res)
@dataclass
class PageIn:  id: int; number: int; printed: str; status: str; reviewed: bool; lines: list[LineIn]
```

`assemble(pages, settings, book_meta) -> Result(document, warnings, stats, seams)` runs the steps below in order.
Every step is a separately tested pure function.

### 2.1 Page selection
Included: status `ocr_done` (only when `include_unreviewed`), `reviewed`, `assembled`. Skipped with a warning:
`uploaded` / `preprocessed` / `layout_done` (`page_pending`), `error` (`page_error`). Excluded pages never
appear. A skipped page breaks the join chain (no paragraph joins across it; its seam shows `missing`).

### 2.2 Text normalisation per line (D37)
1. Punctuation glued to the start of an Arabic word moves to the end of the previous word:
   `"وتاكنست .وتاكنست"` → `"وتاكنست. وتاكنست"`; at a line start the mark moves to the end of the previous
   line of the same block (after joining). Marks: `. ، ؛ : ؟ ! , ; ?`. Opening brackets and quotes
   (`( [ « "`) are legitimate at a word start and stay.
2. No space before `. ، ؛ : ؟ !` and before a closing `» ) ]`; no space after an opening `« ( [`.
3. Tatweel removed when `strip_tatweel` (`بـــين` → `بين`).
4. Runs of spaces collapse to one. Diacritics untouched (test with fully vowelled text).
5. Digits per `book.digit_style` (Western by default) — applied last, to text and footnote markers.

### 2.3 Paragraphs inside a page (geometry first)
For the body lines of a page, with boxes as ratios: the text block's right edge `R` and left edge `L` are the
medians of `x1` / `x0` over the lines at least 60 % as wide as the widest; measure `M = R − L`.

- a line is **indented** when `R − x1 > 0.02 · M` (Arabic first-line indent sits on the right);
- a line is **short** when `x0 − L > 0.06 · M` (the paragraph ended before the left edge);
- a line is **centred** when both gaps exceed `0.06 · M` and they differ by less than 35 % of the larger.

A paragraph break falls between line *i* and *i + 1* when line *i* is short, or line *i + 1* is indented, or
either is centred, or their roles differ. Lines without a box break only on role changes and on terminal
punctuation (`. ؟ ! : » ) ]` at the end of line *i*). Consecutive `heading` lines form one level-1 heading,
consecutive `subheading` lines one level-2 heading.

### 2.4 Seams: joining across pages
At each boundary between consecutive included pages *n* and *n + 1*: take the last body block of *n* and the
first body block of *n + 1* (footnotes are not involved). **Join** when both are paragraphs, the last line of *n*
is not short, and the first line of *n + 1* is not indented; without boxes, join when the text of *n* does not
end with terminal punctuation. Otherwise **split**. Overrides in `settings.seams[str(n + 1)]` win. Every boundary
yields a seam record `{page: n + 1, from_page: n, mode: "join"|"split"|"missing", decision: "auto"|"override",
reason: "geometry"|"punctuation"|"heading"|"skipped_page"}`. A join concatenates with one space and inserts an
inline `pageBreak` node at the join point.

### 2.5 Footnotes
**Notes of a page.** A footnote line that starts with a marker starts a note; a line without a marker continues
the current note; the first footnote line of a page without a marker continues the previous page's last note when
that page ended with a note, else it starts a note without marker. Marker at a note start:
`^\s*[\(\[]?\s*(\d{1,3}|[٠-٩]{1,3}|\*{1,3})\s*[\)\]]?\s*[-–.:،]?\s+` (strip it from the note text).

**Markers in the body.** Candidates, in reading order: `(n)` `[n]` `(*)`; superscript digits `¹²³…`; a digit
run glued to the end of a word (`الفيل٢`); a standalone digit token of 1–2 digits (`الفيل ٢ مرحلة`). A candidate
is linked only when a note of the same page carries that number (or `*`); each note takes the first unused
matching candidate. The marker text is replaced by an inline `footnote` node whose content is the note text.

**Warnings.** A bracketed, superscript or glued candidate that finds no note on a page that has notes →
`marker_unmatched` (the text stays as printed). A note that finds no marker → kept as a footnote node appended to
the page's last body block with `orphan: true` → `note_orphan`.

**Numbering** (`footnote_numbering`): `chapter` restarts at each level-1 heading, `book` runs through, `page`
restarts on each source page. Stored in the node's `number`; the original marker in `marker`.

### 2.6 Headings and suggestions
Line roles (D32) give headings: `heading` → level 1 (starts a chapter), `subheading` → level 2. A book without any
level-1 heading gets one warning `no_headings` (info). **Suggestions** (never applied automatically): a body
paragraph of 1–2 lines that is centred, not ending with terminal punctuation, at most 8 words, and followed by a
paragraph start gets `suggestedRole: "heading"` unless its id is in `dismissed_suggestions`.

### 2.7 Uncertain words
Tokens still unresolved (`conf == "low"` and no `res`) are carried into the text as an `uncertain` mark, so the
view underlines them exactly like the review screen. Their count per page feeds `uncertain_words` warnings (one
per page, info).

### 2.8 Document (ProseMirror JSON, the Phase 5 schema)
```json
{"type": "doc", "attrs": {"bookId": 7, "runId": 31, "assembledAt": "…", "footnoteNumbering": "chapter",
                          "seams": [{"page": 13, "from_page": 12, "mode": "join", "decision": "auto", "reason": "geometry"}]},
 "content": [
  {"type": "title", "attrs": {"text": "…", "author": "…"}},
  {"type": "heading", "attrs": {"level": 1, "id": "h812", "sourcePages": [3], "sourceLineIds": [812], "reviewed": true},
   "content": [{"type": "text", "text": "الفصل الأول"}]},
  {"type": "paragraph", "attrs": {"id": "p815", "sourcePages": [12, 13], "sourceLineIds": [815, 816, 901],
                                   "reviewed": true, "suggestedRole": null},
   "content": [{"type": "text", "text": "… قصر الفيل"},
               {"type": "footnote", "attrs": {"id": "n902", "number": 2, "marker": "٢", "sourcePage": 12,
                                              "sourceLineIds": [902, 903], "orphan": false},
                "content": [{"type": "text", "text": "نلاحظ أن قصر الفيل …"}]},
               {"type": "text", "text": " مرحلة، ثم إلى"},
               {"type": "pageBreak", "attrs": {"page": 13, "printed": "13"}},
               {"type": "text", "text": " مليتية", "marks": [{"type": "uncertain"}]}]}
 ]}
```
Block ids are stable across runs: `h`/`p` + the first source line id; note ids `n` + the first note line id.
Every block carries `sourcePages` and `sourceLineIds`; `reviewed` is false when any source page is unreviewed.

### 2.9 Warnings
`[{code, severity: "warning"|"info", page, blockId, lineIds, message}]`, messages in Arabic, Western digits.
Codes: `page_unreviewed`, `page_pending`, `page_error`, `marker_unmatched`, `note_orphan`, `uncertain_words`,
`no_headings`, `empty_page` (included page without body text, info).

### 2.10 Stats
`pages_included`, `pages_skipped`, `pages_unreviewed`, `headings`, `paragraphs`, `footnotes`, `joins`, `words`.

## 3. Services, task, API

- `assembly.services.start_assembly(book, user, options) -> AssemblyRun`: merges `options` into
  `book.assembly_settings`, returns the queued/running run if one exists (idempotent), else creates a run and
  enqueues `assembly.tasks.assemble_book(run_id)` on the default queue. Views only enqueue (hard rule).
- `assemble_book` loads, runs the pipeline, updating `run.stage` at each step (`collect`, `paragraphs`, `seams`,
  `footnotes`, `headings`, `typography`, `save`), then in one transaction: snapshot the previous document,
  write the `Manuscript` (`origin="assembly"`, version + 1), move included `reviewed` pages to `assembled`,
  `book.refresh_status()`. Any exception → `status=error` with an Arabic headline; the old manuscript stays.
- `manuscript_state(book) -> dict`: `{exists, version, assembled_at, run: {id, status, stage, error}, stale,
  stale_pages: [n…], warnings_count, stats}`. **Stale** when the eligible page set differs from `run.included`,
  a page's reviewed flag changed, or a line of an included page was updated after `run.finished_at`.
- Review (`review/services.py`): `REVIEWABLE_STATUSES` gains `assembled`; every mutating service on an assembled
  page sets it to `reviewed` in the same transaction (undo restores the snapshot as today).
- Dashboard (`books/services.py`): `book_dashboard` config and the progress payload gain `manuscript`
  (= `manuscript_state`, compact) and the URLs below.

| Method | URL (name) | Who | Body → response |
|---|---|---|---|
| POST | `/api/books/<id>/assemble/` (`api:book_assemble`) | editor, admin | `{footnote_numbering?, include_unreviewed?, strip_tatweel?}` → 202 `{run_id, status, manuscript_url, state_url}` |
| GET | `/api/books/<id>/manuscript/state/` (`api:manuscript_state`) | login | `manuscript_state` |
| GET | `/api/books/<id>/manuscript/` (`api:manuscript`) | login | `{document, warnings, stats, seams, version}` |
| POST | `/api/books/<id>/manuscript/seams/` (`api:manuscript_seam`) | editor, admin | `{page, mode: "join"|"split"|"auto"}` → 202 run |
| POST | `/api/books/<id>/manuscript/roles/` (`api:manuscript_roles`) | proofreader, editor, admin | `{line_ids: [...], role}` → 202 run (roles via `set_line_role`, one revision per changed line) |
| POST | `/api/books/<id>/manuscript/suggestions/` (`api:manuscript_suggestion`) | editor, admin | `{block_id, action: "dismiss"}` → 202 run |
| GET | `/books/<id>/manuscript/` (`assembly:manuscript`) | login | the manuscript view |
| GET | `/books/<id>/manuscript/document/` (`assembly:document`) | login | the rendered document fragment (in-place swap after a re-run) |

Errors are DRF responses with Arabic `detail`; 403 for roles, 404 for other books, 400 for bad input.
A management command `manage.py assemble <book_id> [--dry-run]` runs the pipeline synchronously and prints stats
and warnings (`--dry-run` writes nothing), for the owner's smoke test.

## 4. UI (the priority of this phase)

Visual vocabulary: the review screen and the dashboard viewer (D33–D34): calm hairline chrome, DESIGN.md tokens,
the toolbar hanging from the top bar, a side panel on the end side (RTL: left), status pills, review-style
skeleton shimmer, accent only for focus/selection/progress, warning amber for uncertainty. Western digits,
numbers in `<bdi>`, Arabic copy that states facts. `prefers-reduced-motion` replaces every motion by a static
state. Everything reachable by keyboard.

### 4.1 Dashboard integration
- Primary button (one per view): unreviewed pages remain → «الصفحة التالية للمراجعة» (as today); all reviewed and
  no manuscript or a stale one → «تحويل إلى كتاب» / «إعادة التجميع»; manuscript fresh → «فتح المخطوطة».
- The top-bar «⋯» menu gains «تحويل إلى كتاب…» (always, for editors) and «فتح المخطوطة» when one exists; «نسخ نص
  الكتاب» stays in the menu.
- The convert action opens a small popover (the menu-popover style): footnote numbering (segmented control
  «حسب الفصل | حسب الكتاب | حسب الصفحة»), «تضمين الصفحات غير المُراجَعة (N)» checkbox, «حذف التطويل» checkbox,
  and the button «تحويل». It posts, then navigates to the manuscript view, which shows the assembly as it runs.
- The side panel gains a «المخطوطة» block under the summary: state line («مُجمَّعة · 214 صفحة · 3 ملاحظات», or
  «تغيّر نص 4 صفحات بعد التجميع», or «قيد التجميع»), and a link «فتح المخطوطة».

### 4.2 Manuscript view `/books/<id>/manuscript/`
- **Top bar**: title «المخطوطة» with the book title as meta; a status pill (`= rv-save`): «مُجمَّعة قبل 5 دقائق»,
  «قيد التجميع…», «تغيّر النص بعد التجميع», «فشل التجميع»; the primary: «إعادة التجميع» when stale or failed, else
  «فتح الكتاب» (D49: no copy); «⋯» menu: assembly options (the same popover), re-assembly, «لوحة الكتاب».
- **Toolbar** (the dashboard's): segmented «فواصل الصفحات: إظهار | إخفاء» (S), jump «إلى صفحة…» (G, scrolls to the
  first block from that page), counts («214 صفحة · 38 فصلًا · 612 حاشية»).
- **Document column** (max 720 px, centred in its column): the book face (**Amiri**, vendored from the system font
  folder into `static/fonts/amiri/` with its OFL licence; `@font-face` in `manuscript.css`), 19 px, line-height
  1.9, justified, first-line indent 1.5 em, headings centred (level 1 26 px, level 2 20 px), a quiet title block.
  Footnote references are superscript numbers (buttons): hover/focus shows the note in a popover and highlights
  it in the chapter's notes list printed after each chapter (small type, hairline above); the note links back.
  Orphan notes and unmatched markers carry the warning tint. Uncertain words keep the review screen's amber
  underline. Blocks from unreviewed pages have a thin amber start rule and the title «من صفحة لم تُراجَع بعد».
- **Seams** (on by default): at a join, an inline marker — a thin vertical hairline with the new page's number
  (`--color-text-3`, 11 px) — with the hover card «وُصلت الفقرة بين الصفحتين 12 و13 · فصل هنا»; at a split, a
  faint full-width hairline between the blocks with «ص 13» at the start and «وصل بما قبلها» on hover; a
  `missing` seam shows «صفحة 14 غير مُضمَّنة» in amber. Clicking join/split posts the override; the document
  re-runs and swaps in place, the scroll anchored to the same block, the changed blocks flashing once
  (`accent-soft`, 600 ms). An override seam shows a small dot and «تلقائي» to undo the override.
- **Block tools**: hovering or focusing a block shows its source badge in the start margin («ص 12–13») and a «⋯»
  menu: «عرض الأصل» (O), «محتوى» / «عنوان رئيسي» / «عنوان فرعي» (sets the block's lines' role, re-runs),
  «فتح في المراجعة». Blocks are focusable in order (↓/↑ or J/K move focus, the view follows).
- **Heading suggestions**: a suggested block gets a dotted underline and a chip «عنوان؟» with «✓» (apply as
  «عنوان رئيسي») and «×» (dismiss); both re-run.
- **Source drawer** («عرض الأصل»): slides in from the start side (42 % wide, min 360 px), the page scan at
  fit-height with the block's lines highlighted in the review band style and the page number; ‹ › between the
  block's pages; «فتح في المراجعة» (the review screen of that page). Data from `api:book_sheets` for the page
  (display image, line boxes as ratios, line ids). Esc or the close button returns focus to the block.
- **Side panel** (end side, the dashboard panel's look): tabs «المحتويات» (the headings as a tree, scroll-spy
  highlight of the current chapter, click to scroll) and «ملاحظات <n>» (warnings grouped by code with counts,
  each row: page number, message, «انتقال» to the block and «مراجعة» to the review page); a stats block below.
- **Assembling state** (the reveal): while the run is queued/running the document column shows review-style
  skeleton paragraphs and the side panel lists the steps («جمع الأسطر من 214 صفحة», «وصل الفقرات عبر الصفحات»,
  «ربط الحواشي», «بناء العناوين», «ضبط علامات الترقيم والأرقام»), each ticking as `run.stage` advances (poll every
  700 ms). When done, the document appears: the first ~40 blocks rise in with a 16 ms stagger (≤ 1 s in all), and
  each join marker "stitches" (a short accent line that settles into the hairline, 400 ms). Later re-runs skip
  the skeleton and swap in place. Failure: a designed error state with the Arabic headline and «إعادة المحاولة».
- **Never assembled**: an empty state explaining what assembly does in one sentence, with the options and
  «تحويل إلى كتاب».
- **Stale**: a quiet banner «تغيّر نص 4 صفحات بعد التجميع» with the page numbers as links and «إعادة التجميع».
- **Copy** «نسخ نص المخطوطة»: title, headings and paragraphs separated by blank lines; footnote references as
  `[n]`, each chapter's notes after it; the «تم النسخ» toast.
- **Keyboard** (no field focused): G jump, S seams, O source of the focused block, J/K or ↓/↑ block focus,
  `]` / `[` next / previous warning, Esc closes the drawer, popovers and menus. Hints in `title`s.
- **Responsive**: < 1100 px the side panel narrows; < 900 px it moves above the document as a collapsible
  section and the drawer becomes full width.

### 4.3 Performance (800 pages ≈ 5 000 blocks)
Server-rendered document (`assembly/render.py`, used by the view and later by Phase 6), each chapter a
`<section>` with `content-visibility: auto`; delegated listeners on the document; scroll spy observes headings
only; re-runs swap the fragment once; no per-block Alpine bindings.

### 4.4 Renderer contract (`assembly/render.py`)
`render_document(doc, *, notes="chapter") -> str` escapes all text (test with `<script>`), and emits:
`article.ms-doc` > `header.ms-title`, `section.ms-chapter[data-chapter]` > `h2.ms-h1` / `h3.ms-h2` / `p.ms-p`
with `data-block`, `data-pages`, `data-lines`, `data-reviewed`, `data-suggested`; inline `button.ms-ref
[data-note]` (the number), `span.ms-seam[data-page][data-mode][data-decision]`, `mark.ms-uncertain`;
`div.ms-split[data-page][data-mode][data-decision]` between blocks at split/missing seams; `ol.ms-notes` >
`li.ms-note[data-note][data-orphan]` after each chapter.

## 5. Tests
- **Pipeline** (`assembly/tests.py`, synthetic `PageIn`): indent / short / centred segmentation; line roles to
  headings; joins and splits by geometry and by punctuation, overrides, missing pages; footnotes in every marker
  style (Western and Arabic-Indic, parentheses, brackets, superscript, glued, standalone, `*`), multi-line notes,
  notes continuing on the next page, orphans, unmatched markers, the three numbering modes; suggestions and
  dismissals; punctuation repair on the real line `بـــين سلوق وتاكنست .وتاكنست هي قرية تاكنس`; tatweel;
  fully vowelled text unchanged; digits; stable ids; every block has sources; stats.
- **Services/API**: permissions per role, idempotent start, eager task end to end on a factory book, snapshots and
  pruning, `assembled` status and the flip back on a review edit, staleness, the roles/seams/suggestion endpoints,
  loader query count independent of page count, the management command.
- **UI**: rendered templates (dashboard states, manuscript view states), renderer escaping, and the Alpine
  component `manuscriptView` under the Node harness (seam toggle posts and swaps with the scroll anchor, drawer
  open/close and focus return, suggestions, keyboard map, polling to done).

## 6. Ownership for the build agents
- **Backend** (runs first): `assembly/` (models, `pipeline.py`, `services.py`, `tasks.py`, `api.py`, `urls.py` API
  routes, `management/commands/assemble.py`, `tests.py`), `editor/` (models, admin, migrations, `tests.py`), the
  `Book.assembly_settings` migration, `review/services.py` (D36), `books/services.py` (dashboard/progress
  `manuscript`), settings and pytest paths, `nassakh/urls.py` wiring.
- **UI** (runs second, on the working backend): `assembly/render.py`, `assembly/views.py` and the page routes,
  `templates/assembly/`, `static/src/js/manuscript.js`, `static/src/components/manuscript.css` (imported in
  `static/src/app.css`), `static/fonts/amiri/`, the dashboard integration (`templates/books/detail.html`,
  `static/src/js/books.js`, `static/src/components/theatre.css`), `templates/base.html` script include,
  `core/test_manuscript_ui.py` and dashboard test updates.
- Neither writes to the development database: real books are previewed by running the pure pipeline on data
  loaded read-only and rendering to a scratch HTML file.

## 7. Acceptance
- [ ] A reviewed book converts from the dashboard; the manuscript view shows the steps ticking, then the document.
- [ ] Running headers and printed page numbers never appear; paragraphs cut by a page break read as one, with a
      visible seam that can be split; wrong splits can be joined.
- [ ] Heading roles become chapters and sections in the table of contents; suggestions can be accepted or dismissed.
- [ ] Footnote references link to their notes, numbered per chapter by default; orphan notes and unmatched
      markers appear in «ملاحظات» with a jump to the block and to the review page.
- [ ] `وتاكنست .وتاكنست` comes out `وتاكنست. وتاكنست`; diacritics survive; digits are Western.
- [ ] Every block opens its source scan with its lines highlighted.
- [ ] Editing a page after assembly marks the manuscript out of date; re-assembly keeps a snapshot.
- [ ] Reduced motion: no stagger, no stitch, no shimmer. Keyboard reaches every tool.
