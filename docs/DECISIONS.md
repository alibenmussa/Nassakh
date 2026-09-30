# Decisions

Short records of choices that are not obvious from the code. Newest at the bottom.
Format: date, decision, why, consequences.

## D1 — Celery + Redis, not Django's built-in tasks (2026-09-23)
Django 6 ships `django.tasks` but only immediate/dummy backends, no worker and no queue routing.
The OCR work needs a separate GPU queue with retries. Celery stays.

## D2 — Extract scanned pages at native resolution (2026-09-23)
The sample scans are embedded at 150–200 DPI (sample 4 lower). Rendering at 300 DPI would only
upsample. Pages are rendered at the DPI that reproduces the embedded scan pixel for pixel, which also
honours PDF page rotation (sample 2 sheets are rotated 90°). Born-digital pages render at 300 DPI.

## D3 — Two pages per sheet are split at a detected gutter, right page first (2026-09-23)
Sample 2 has two book pages per landscape sheet. The gutter is the widest low-ink vertical band in the
central 30% of the sheet, bridging narrow dark spikes (fold shadow, staples). In an RTL book the right
half is the earlier page. Position is per book with per-page override in the product.

## D4 — Layout by master guide lines, not full automatic layout analysis (2026-09-23)
Owner request: set the footnote line and the header cut once per book, apply to all pages, adjust per
page. Heuristics only *propose* positions (footnote rule detection, repeated top line). Surya/Kraken
are not used: Surya weights are non-commercial above a revenue threshold, Kraken is heavy.

## D5 — Mac-native, no production target for now (2026-09-23)
Everything runs on the owner's Apple M5 Pro (24 GB). Docker, nginx and uWSGI files are deferred.
Inference backend on Apple Silicon: MLX (`mlx-vlm`) versus PyTorch MPS is decided by the PoC.

## D6 — Digits are always Western (0–9) (2026-09-23)
Owner decision. OCR raw output is stored untouched; assembly converts Arabic-Indic digits by default
and the editor offers a convert action. Diacritics are still never stripped.

## D7 — Qari LoRA adapters are merged manually onto the bf16 Qwen2-VL-2B base (2026-09-23)
Qari v0.2.2.1 and the KITAB checkpoint are LoRA adapters declared against a 4-bit bitsandbytes base
that cannot run on a Mac. `prepare_models.py` downloads `Qwen/Qwen2-VL-2B-Instruct`, applies
`W += (alpha/r) · B·A` per module, matching module names by canonical suffix so it survives the
transformers 4→5 module renames, and saves a full merged model. Training on a 4-bit base and merging
into bf16 is an approximation; the PoC measures whether it matters.

## D8 — Text layers from Word PDFs are repaired with glyph geometry (2026-09-23)
Sample 3's text layer has every lam-alef ligature reversed (الإسلام → اإلسالم). Text alone cannot fix
it because ال is also the article. In PyMuPDF's rawdict the alef that belongs to the ligature has a
zero-width box sitting on the lam's edge, so those pairs are swapped back. Word also emits trailing
commas and justification spaces at the visual line end but mid-stream; neutral glyphs whose pen position
contradicts their stream position are re-inserted by x. Validated against OCR in the PoC.

## D9 — Experiment runs are a folder of JSON files, one per run (2026-09-23)
`runs/<page>__<engine>__<variant>__<backend>.json`, written atomically after each page. This makes the
grid resumable (stop any time, rerun the same command) and keeps every raw output, prompt, model
revision and duration, which is also what the future `OcrRun` model stores.

## D10 — Prompts copied verbatim from the model cards (2026-09-23)
Qari v0.2 and v0.3 share one plain-text prompt; KITAB has its own. Stored in `config.py` and in every
run record, so a prompt change is visible in the data.

## D11 — OCR body and footnotes as separate regions (2026-09-23, from the quick run)
Whole-page Qari runs loop on footnote/reference lists on 3 of 12 quick runs (repeating one line until
the 3000-token cap). A repetition penalty fixed one page and not another, and changed digits. Sending the
body above the footnote rule and the footnote block (upscaled 2x) as two images stopped cleanly on the
same pages and read the footnotes better. The product OCRs per region derived from the guide lines (D4),
and the run store keeps a `looped` flag plus a truncated tail for whole-page runs.

## D12 — Line linking in the review screen comes from image geometry, not OCR line breaks (2026-09-23)
Qari v0.2 returns the page as one paragraph even when asked for line breaks; v0.3's HTML tags do not
follow visual lines. Detected line boxes (projection profile) are reliable, so the review screen will map
OCR words to lines by proportional length or by anchoring to a word-box detector (Tesseract) rather than
counting newlines. To be designed in Phase 3.

## D13 — Metrics include an order-insensitive word F1 (2026-09-23)
On the born-digital sample the OCR reads table cells in a different order than the text layer, which CER
counts as 12% error although the characters are right. The report shows word F1 next to CER so table
pages and looped outputs are judged fairly.

## D14 — Engine set after the PoC: Qari v0.3 primary, Qari v0.2 secondary, Tesseract always; KITAB dropped (2026-09-24)
On 17 ground-truth pages (lenient CER): v0.3 median 3.8% with one failure; v0.2 median 14.8% because it
looped on 9/17 whole pages although it is the most accurate model when it works (0.7–4.5%); KITAB
returned an empty output on 8/17 pages; Tesseract median 8.9%, never catastrophic, 0.5 s/page, and its
lines match the detected image lines. Full tables in `playground/poc/REPORT.md`.

## D15 — Region OCR is the default; grayscale for Qari, Sauvola B&W for Tesseract; no 2x upscaling (2026-09-24)
Body/footnote regions cut v0.2 loops from 9/17 to 1/8 and gave the best mean of the grid (6.7%). Black-
and-white input destroyed v0.2 (69% vs 38%) but helped Tesseract (10.7% vs 12.9%). Upscaling low-
resolution pages 2x was erratic across engines and pages and is not a default.

## D16 — Automatic failure detection with Tesseract as the reference (2026-09-24)
Qari failures are loops, empty outputs, garbage from a facing-page strip at the scan edge, and silent
paragraph omissions. All are visible by comparing word count and word overlap with Tesseract's output.
Pipeline: Qari v0.3 → if it fails the check, Qari v0.2 → if both fail, Tesseract text; page flagged.
Picking the better of two engines per page halves the error in the PoC (oracle 7.2% for v0.3+Tesseract,
3.8% for v0.2+v0.3 regions), so word-level agreement of three engines is the confidence signal.

## D17 — Digits are always low-confidence; Tesseract runs with `ara+eng` (2026-09-24)
Every model misread years and footnote numbers on several pages. Latin words inside Arabic pages are
kept by v0.2, sometimes dropped by v0.3, and garbled by Tesseract `ara` alone.

## D18 — Preprocessing removes facing-page strips at the scan edge (2026-09-24)
A narrow column of characters from the neighbouring page (sample 1 page 18) made both Qari models emit
nonsense for the whole page. The crop step must keep only the main text block: narrow ink bands at the
edge separated from the block by a wide empty gap are dropped.

## D19 — PostgreSQL 17 on port 5433 (2026-09-24)
Django 6.1 refuses PostgreSQL 14 (`minimum_database_version = (15,)`), which is what Homebrew had on the
owner's Mac. The integration agent installed `postgresql@17` as a second brew service on port 5433 and left
`postgresql@14` untouched on 5432. `.env` points at 5433. Alternative rejected: pinning Django to 5.2 LTS.
Reversible with `brew services stop postgresql@17 && brew uninstall postgresql@17`.

## D20 — Celery workers use spawned children (2026-09-24)
On macOS with Python 3.13 the prefork pool spawns rather than forks; without `FORKED_BY_MULTIPROCESSING=1`
every task died in `fast_trace_task`. Set in `nassakh/celery.py`. Children re-import Django and Torch on
start, so worker start-up is slower; `-P threads` is the fallback if that ever matters.

## D21 — Phase 2 deviations from the spec, accepted (2026-09-24)
`propose_guides` returns `(guides, confidence)`; `PdfTextEngine.recognize` takes a PDF page reference, not an
image; born-digital pages are finalised during fast OCR so they never wait for the GPU queue; `OcrRun.input_variant`
also uses `pdf` and `ingest`; crop box in the preprocess panel is numeric inputs (the display image is already
cropped); the "الأصل" tab shows the grayscale render; a manual preprocess re-run does not re-enqueue OCR (the
panel says so). Alternatives in the review screen come only from the other Qari model, never from Tesseract.

## D22 — Experience goals are acceptance criteria (2026-09-24)
Nassakh generates book formats; viewing exists to verify the machine's work, not to read. Owner-approved
essentials: the reveal on upload (thumbnails crossfade from scan to cleaned page), provisional Tesseract text
that settles into the final text, one-click copy of clean text per page and per book, plain progress
("X من Y صفحة", no persona, no time estimates), and failures that look designed (Arabic headline, fallback
text, retry). Rejected: a reading view, time-remaining estimates.

## D23 — Provisional text is shown as a "decode" effect, not as plain text (2026-09-24)
Owner decision. Tesseract's first pass is inaccurate; shown plainly it makes a poor first impression. While a
page is provisional its words oscillate between noise glyphs and Tesseract's letters (never fully settling), and
a right-to-left resolve wave lands them on the final text when the models finish. Words are mutated as whole
strings, never split into per-letter elements, so Arabic letters keep joining. Spec: PHASE3_SPEC §2.

## D24 — Motion exception for content being processed (2026-09-24)
DESIGN.md §8 ("no looping decoration") still governs the chrome. The owner explicitly asked for continuous
motion on content in a processing state (decode effect, scan sweep, line-reading shimmer). It stops when the
state ends, runs only near the viewport from one shared rAF loop, and is replaced by static states under
`prefers-reduced-motion`. Resolution moments are short transitions (≤ 600 ms).

## D25 — Two book views: stacked sheets (default) and a grid of processing cards (2026-09-24)
Owner decision. The default dashboard view stacks pages like a PDF viewer, each sheet showing the scan and its
text generating; the grid shows small animated processing cards without text. Sheets are lazily mounted and
fetched in batches (`/api/books/<id>/sheets/`) so an 800-page book stays smooth. Amended by D33: «صفحات» shows one
page at a time instead of a stack.

## D26 — Word-chooser hook for uncertain words (2026-09-24)
Owner decision. A future small classifier may pick the best candidate (primary / secondary / Tesseract) for
uncertain words as part of processing, with no UI. Phase 3 adds `ocr/chooser.py:choose_word()` returning None,
called from `finalize_page`; a chosen word keeps `conf = "low"` so it stays reviewable.

## D27 — Review defaults (2026-09-24)
Reviewers may edit a whole line and insert a missing line (models sometimes drop a phrase). A page is approved
only when every uncertain word is resolved, or with an explicit confirmation. Every review action is stored as a
`LineRevision`, which powers server-side undo.

## D28 — Provisional text: dwell-and-veil, not noise (2026-09-24, supersedes the "never settles" part of D23)
After testing, the owner found the first decode effect too noisy and objected that it never returned to
Tesseract's words. New behaviour: provisional words show Tesseract's reading in full, in muted gray with a slow
opacity fade; every few seconds a word is briefly "veiled" (one to three letters swapped for same-skeleton Arabic
letters, never the first letter, never digits, no ASCII, no length change) and then returns exactly to Tesseract's
reading. A reading cursor lights one scan line and one text line at a time (shimmer sheen, words forced exact
while lit). At most ~40 veiled words per sheet. The resolve into final text stays a right-to-left wave.

## D29 — Dashboard text mirrors its page (2026-09-24)
Owner decision. Each text group (body, footnotes) is a flex column with space-between placed on the page's own
geometry (final line boxes, else Tesseract line boxes, else detected boxes, else the region), font size derived
from the median line height of the page, justified with `text-align-last: start`, shrink-to-fit so the column
never scrolls. Both panes share one line index, so hovering a text line lights its box on the scan and back.
Spec: `docs/DASHBOARD_SPEC.md`.

## D30 — One type size per text group; lines fit by word spacing; paragraph edges shared (2026-09-24)
Owner feedback: lines of one page showed different font sizes, and full lines with nearly equal widths should
share one width while a paragraph's short last line keeps its own. The mirrored text now uses one size per
group (body, footnotes), lowered only as far as the full lines need (outlier lines such as merged ones excluded),
never per line. Lines fit the way print does: justified word spacing, then up to half a space tighter, then a
horizontal condense down to 90 %, then an end fade. Line edges within 3 % of the text width snap to the
paragraph's shared edge; indented first lines, headings and short last lines keep their shape. The scan keeps
the true boxes. Across pages, the book's median line height (`book_line_h_px` in `api:book_sheets`) sets the
size when a page agrees within ±20 %. Spec: `docs/DASHBOARD_SPEC.md` §4.6.

## D31 — Paragraph first lines keep their indent; reviewers can merge and delete words (2026-09-24)
Owner feedback. (1) On the dashboard, a paragraph's first line is detected by a finer start-edge tolerance
(1.2 % instead of 3 %, so a small printed indent is not snapped away); it keeps its indent and stays flush with
the paragraph's left edge: justified within its indented measure, end-aligned when its text is short (never
hanging from the right), and allowed to grow into the indent when its text needs the room. (2) The models
sometimes split a name or place in two («هير ودوت»): any word's menu in the review screen offers merging it
with the next or previous word (union of both boxes, a reviewer decision), with Alt+ArrowLeft / Alt+ArrowRight.
(3) A stray letter or number the OCR added can be removed from the menu or with Backspace / Delete. Both new
actions are `LineRevision` entries (`merge`, `drop_word`), so undo restores the words with their boxes and
readings. The word menu now opens for every word on an editable page, not only uncertain ones.

## D32 — Word menu: correction first, merge and delete one level deeper; line roles (2026-09-24)
Owner feedback after using the word menu. Correction is the main action: a confident word opens with its text
prefilled and selected, readings of uncertain words stay on top, and merge / delete move into a second-level
«إجراءات أخرى» menu (hover with a short grace period, click, or ArrowLeft) so they cannot be hit by mistake.
A click outside closes the menu (the release of a scan drag does not). The menu is placed against the visible
part of the lines column: below the word, flipped above near the bottom, never across a side edge; the submenu
opens on the side with room, or folds into the menu in a narrow column. Keys on the menu's buttons stay native
(Enter activates, arrows move inside the menus instead of turning pages). Lines carry a role, set from the line
menu: body «محتوى», main heading «عنوان رئيسي», subheading «عنوان فرعي» (`ocr.Line.role`; footnote lines stay
body; `LineRevision` action `role`, undoable). Assembly (Phase 4) uses the roles for chapters and the table of
contents. Also added: a test that fails on multi-line `{# #}` template comments, which Django renders as text.

## D33 — «صفحات» is a page viewer: one page fitted to the window, turned like the review screen (2026-09-24)
Owner feedback: the side-by-side preview is right, the long scroll is not (a book has 100 pages or more).
The «صفحات» view now shows one page at a time, scan beside its text, fitted to a stage so the whole
dashboard fits the window below the top bar. Pages turn with the review screen's movement: the page slides
and fades out in 200 ms and the next slides in from the other side; a press during a turn moves the target
instead of queueing turns. Turn with the side buttons, the arrow keys (RTL: left is forward), PageDown /
PageUp, Home / End, a trackpad swipe or the wheel (one page per gesture, inertia ignored), a touch swipe,
the jump field, or the filmstrip of thumbnails under the stage (reviewed mark, uncertain-word count, live
dot). The filter decides the sequence; the page on screen is never given `hidden`, because Tailwind's
`[hidden]` rule is `!important`, and it stays when a poll takes it out of the filter. The page is kept in
the address (`#sheet-N`) and the session; a plain click on a grid tile opens the viewer on that page. The
follow mode turns to the page that advanced. Only the page on screen is mounted and its neighbours' data is
prefetched. Replaces the scroll position chip; the grid view is unchanged.

## D34 — The pages beside a side panel; footnotes set solid (2026-09-24)
Owner feedback on the viewer: the page should be larger, and the filmstrip under it and the summary strip
above it took too much height. The dashboard now puts the pages beside a narrow side panel on the end side
(RTL: the left). The panel holds the summary as a column (stage counters, review state, the attention
list) and, in «صفحات», the filmstrip as a scrolling grid of thumbnails, two or three columns. The toolbar
hangs from the top bar, so the viewer's page gets almost the whole window height: about 630 px instead of
430 px on a 800 px tall window. In «شبكة» the panel is sticky under the toolbar. Under 900 px the panel
moves under the page, or above the grid. Also from the same feedback: footnotes had wide gaps between
their lines. Their type is smaller than the scan's, but their lines were spread over the printed block.
Footnotes are now set solid: packed from the first note down with a line box of 1.5 × their type size,
free to run on below their printed lines to 97 % of the page before the type shrinks. A footnote whose
lines have only some text boxes uses them; one with none is sized from the body's lines, because the
detected boxes measure the letters' core band (about a quarter of a line) and gave tiny type.

## D35 — Assembly may run before every page is reviewed (2026-09-24)
Unreviewed OCR'd pages are included, flagged on their blocks and listed as warnings; pages still in the pipeline or in
error are skipped with a warning; excluded pages never appear. Footnotes are numbered per page by default (owner, 2026-09-25; per chapter or
per book as options, remembered per book); exported numbering follows the stylesheet (Phase 6). Spec: PHASE4_SPEC §0.

## D36 — `assembled` means "in the current manuscript, unchanged since" (2026-09-24)
After a run, included reviewed pages become `assembled`. They stay editable in review; any change puts them back to
`reviewed` and the manuscript shows as out of date until re-assembled.

## D37 — Assembly normalises typography in the derived text only (2026-09-24)
Punctuation glued to the next word by the primary model («تاكنست .وتاكنست») is moved back, spaces before . ، ؛ : ؟ ! are
dropped, tatweel is removed (setting, on by default), digits follow `digit_style`. Stored lines, tokens and diacritics are
never changed.

## D38 — Assembly overrides live with the book (2026-09-24)
Seam overrides (join/split), dismissed heading suggestions and assembly options are stored in `Book.assembly_settings` and
re-applied by every run. Heading fixes from the manuscript view go through the review service (`set_line_role`).

## D39 — OCR lines: rescue lines Tesseract skips, place orphan words by evidence (2026-09-24, amends D12)
Tesseract sometimes drops a whole printed line (usually a paragraph's last line), so its Qari words were appended to the
line above (two printed lines shown as one). After each Tesseract run, detected bands and ink gaps with no Tesseract line
are re-read one line at a time (psm 7, tight crop) and inserted as rescued lines; unanchored words go to the line whose
unmatched Tesseract words they sit on (garbage line starts, rescued lines); visibly inverted Tesseract lines are reordered;
a line that still looks merged gets the attention flag «سطران مطبوعان في سطر واحد». `manage.py rebuild_lines` applies
this to existing pages from their stored runs without calling a model, skipping pages with any review edit.

## D40–D46 — Phase 5: editor, stylesheet, real-page preview (2026-09-25)
Owner decisions, detailed in `docs/PHASE5_SPEC.md` §0: one chapter edited at a time (D40); after the first editor save
the manuscript is the source of truth and review drift is resolved per chapter (D41); WeasyPrint renders pages behind
one engine interface, never the browser (D42); one neutral book model feeds the HTML/PDF renderer now and the Word
renderer first in Phase 6, EPUB after (D43); footprint rendered in the background and cached by hash (D44); trim
presets with 17×24 cm default, fonts Amiri (vendored) + Simplified Arabic, Traditional Arabic, Times New Roman and
Lotus loaded from the Mac's font folders, Aref Ruqaa dropped (D45); footnotes numbered per page in every format, the
PDF via a two-pass render because WeasyPrint's footnote counter is document-level (D46).

## D47 — One book page with live pages: edit on the real layout (2026-09-25, amends D40 and PHASE5 §4–§5)
Owner decision after testing Phase 5: the layout page is the book's only page; the separate editor page goes (it was not
formatted to the page). The page on screen must show the same text and the same footprint as the export. So the stage no
longer shows images: every WeasyPrint render also exports its layout (each line's box, text runs, block id and character
range; footnote area, running header, page number), and the page draws those exact lines as positioned text in the same
fonts (the dashboard's text-mirrors-its-page technique). Edit mode (toggle «معاينة | تحرير», E, double-click in preview)
lets a click place the caret on any line; the paragraph under it becomes editable in place, flowing live while the lines
below shift; after a short pause the chapter is re-laid-out by WeasyPrint and the pages refresh with the engine's breaks,
page count and per-page footnote numbers. Later pages renumber (and swap sides) from the chapter's page delta at once;
books without chapter breaks re-lay-out forward until the pages converge; the whole book is confirmed in the background.
One side panel with icon tabs: الفصول, الصفحات, بحث, التنسيق, الفقرة, الأصل (unchanged), غير المؤكَّدة. Also: page break
before a paragraph and keep-with-next, widows/orphans and headings kept with the next line by default, book details
(title and copyright pages), page checks. `/books/<id>/editor/` redirects to the book page. Spell check: not wanted.

## D48 — No running header by default; a note prints on its call's page (2026-09-25, amends D46 and D47)
Owner decision: a new book has no running header («بلا ترويسة») until one is chosen in التنسيق. Found while testing
the book page: WeasyPrint can drop a note to the next page when it steps back for widows/orphans at the page foot, so
the renderer finds each note printed after its call's page, relaxes widows/orphans at that page break and lays out
again (at most two passes). A footnote call is 0.62 of the body text size (it had come out 0.62 of the note size), and
the page editor draws calls and scan-page marks exactly as the engine lays them out, so the paragraph being edited
breaks its lines where the export does. `ENGINE_VERSION` is `nk-print-4`: every book lays out again when next opened.

## D49 — The manuscript is the structure check before the book (2026-09-25, amends PHASE4_SPEC §4 and D41)
Owner review of the manuscript view (reliability, simplicity, purpose). Its purpose: between the reviewed pages and the
book page, check the structure the assembly built (chapters and headings, paragraphs joined across pages, footnotes
linked to their markers) and fix it at its source (line roles, seam overrides), then go on to the book. So the primary
is «فتح الكتاب» (re-assembly only when the pages changed before any edit, or after a failure); copying the whole
text is gone (the book leaves Nassakh as Word, PDF, EPUB in Phase 6). A whole-book run asks for the book render at once,
cancelling a render left from the old text, so the book page opens on the new manuscript (it had kept the old pages).
Once the text is edited on the book page it is the book: the view says so, its structure tools rest (headings and
joins are paragraph styles and Enter/Backspace there), and a whole-book run is refused (409) unless the convert
popover's «استبدال النص المحرَّر» confirms it (the edited text stays as a snapshot). Assembly now drops running heads
the layout left in the body: a page's short, narrow first line repeated at the top of at least three pages (OCR slips
allowed; the chapter's own title, lower or taller, is kept and counts, as does a reviewed heading with that text); an
option «حذف الترويسات المتكرّرة», on by default. On «كتابي» (81 pages) it removed 60 heads, joins across pages went
from 9 to 39 and heading suggestions from 57 (mostly heads) to 10 real titles. Also: warning groups open only when
short, the side panel scrolls as one column, and menus opened by click take the focus.

## D50 — Kraken reads the Arabic-Indic numbers; its reading is the only one offered (2026-09-26, amends D17)
Measured on 222 labelled number crops from nine books (`playground/digits/REPORT.md`): on Arabic-Indic digits Qari v0.3
gets 43 % of the numbers exactly right, Qari v0.2 40 %, Tesseract 1–9 % (30 % with its Persian model), PaddleOCR 37–52 %,
and Kraken with the OpenITI printed Arabic-script base model (Zenodo 7050270, CC0, 16 MB) 92 %, at 6 ms a number on the
CPU. On Western digits Qari stays best (90 % against Kraken's 71 %: it turns slashes and full stops into digits). Owner
decision: in books printed with Arabic-Indic digits Kraken's reading is the only option; the reviewer confirms it or types
the true number. The numbers pass (`ocr.numbers`, task `read_numbers`) runs after Qari finalises a page: the book's
digits are told from Qari's own readings (v0.2 writes 90–100 % of them Arabic-Indic in such books, 0–12 % otherwise);
each number word is read in its word box (91 % on the labels), a number without a box in the gap between its boxed
neighbours when Kraken's digit runs match Qari's (55 %; otherwise Qari's reading stays). Numbers stay low-confidence (D17).
Kraken 6 pins torch ≤ 2.9 and the project runs 2.14, so it runs in its own environment (`make kraken`, `.venv-kraken/`)
through a small runner process, one call per page. Next: numbers found from the ink (half of the number words have no
word box), Kraken fine-tuned on reviewed lines, and numbers Qari wrote as letters («(ع ه – ه م)» for «(٧٥٤–٧٧٥م)»).

## D51 — Numbers Qari wrote as letters go through Kraken too; Qari's letter stays the second reading (2026-09-26, extends D50)
Qari writes some Arabic-Indic digits as the letter they look like (١ «ا», ٥ «ه», ٤ «ع») or drops a date's digits:
«(ج ا، ص٢٣٨)» for «(ج١، ص٢٣٨)», a footnote mark «ا» for «١», «(ه) الخزر» for «(٥) الخزر», «(هـ – م)» for
«(٨٤٧–٨٦١م)». The word then holds no digit, so D50 never reads it and review shows it as a sure word. Labelled by hand on
six books (`playground/digits/REPORT.md`, round 3): 56 such letters, 13 real letters of the same shape («(هـ)» in a
lettered list, the hijri «هـ» after a year, an index heading «(ع)») and 3 digit-less dates. In books printed with
Arabic-Indic digits the pass now also reads, for a line holding a lone «ا»/«ه»/«هـ»/«ع» (with brackets or punctuation)
or a bracketed date without digits, the whole line with Kraken, and the letter's own area (its word box, or a gap that
holds only it and punctuation, with a smaller side margin). The letter takes the number Kraken read between Qari's
neighbouring letters in the line, else the one number in its own area when the rest of the area matches Qari; nothing
when Kraken reads Qari's letter itself in the letter's box. Result: 45 of 56 read, 40 of the 41 of known value exact,
none of the 13 real letters changed; the dates all exact (read in their own area, the line as a fallback). The token is
Kraken's and low-confidence (D17); unlike a D50 number, Qari's letter stays offered as the second reading (labelled with
Qari's name), since it may be a real letter. A digit-less date (a bracket holding only short marks and a dash,
never words) becomes one token with Kraken's date and Qari's dash, Qari's reading offered second; its own area is
read only when no number or bracket shares it, else the whole line decides, anchored on the neighbours.
Two fixes to D50 found on the way: Kraken writes the Persian «۶» for the page's «٤» (same shape; it is mapped to «٤»),
and when it drops the spaces between numbers set apart by a comma or a slash it gives them in the wrong order («٥٢ ، ٥٣»
→ «٥٣،٥٢»); an area's numbers now take the page's right-to-left order when that agrees better with Qari's digits
(a slash date set left to right keeps Kraken's order). The pass saves a line only when nobody changed it while Kraken
read (the page row locked as review does; `left_to_reviewer` in the run's params otherwise). Not fixed: a letter Qari
split off a number («2 ه» for «٥٢», «4 ه ق» for «٥٥٤ ق») gets its digit but stays a separate word.


## D52–D61 — Phase 6: export, Word first (2026-09-26; details and reasons in docs/PHASE6_SPEC.md §0)
- **D52** Phase 6 in three steps: 6a the export history, the export page and Word; 6b PDF for print and screen as real
  exports; 6c EPUB 3. On the owner's go (§15.1) all three are built in one run.
- **D53** Nassakh writes the .docx itself (lxml + zipfile, every part in schema order); python-docx only reads files
  back in the tests (amends D43).
- **D54** The preview is the reference: exact line pitch, the same face per character, «(n)» calls, continuous page
  numbers, a contents field pre-filled with the preview's page numbers (never `updateFields`), footnotes restarting
  per page; what Word cannot follow is listed with the file.
- **D55** Kashida on by default as Word's *low* kashida (none / low / medium / high; medium and high lengthen the book).
- **D56** Uncertain words as Word comments by «نسّاخ»: an option, off by default.
- **D57** Only Amiri (OFL) is embedded; the Microsoft / Monotype / Linotype faces are named, not embedded.
- **D58** Every export is a `publishing.Export` row with its file, on its own Celery queue `export`; one active export
  per book and format; the newest 5 files per format kept.
- **D59** One export page «الإخراج» (`/books/<id>/export/`), not a modal: «قبل الإخراج», one block per format, «السجل».
- **D60** The Latin-face fix first (a role whose file is the Latin face's has no `unicode-range`), the preview drops
  bleed and crop marks, `ENGINE_VERSION = "nk-print-5"`.
- **D61** PDF for print (TrimBox, optional bleed and crop marks) and for screen (clean outline, RTL reading) are fresh
  final renders through the book page's engine, so they have its pages. No PDF/X or PDF/A claim.
- D62 (the Word calibration results) is recorded after the owner's calibration pass. Measured already (C12, by
  `word_check --c12` on this Mac, 2026-09-26): Word collapses the space between paragraphs like CSS (60 pairs took the
  pages of the larger space, not the sum), so `SPACING_ADDS = False` in `publishing/word/options.py`; direct spacing
  is written only where the preview's CSS differs. Compatibility mode 15 kept (mode 14 gave 7 more lines).

## D63 — Word boxes: clip, fill, clamp, keep marks in their gaps, flag weak boxes (2026-09-26)
An audit of book 22 (6 pages, 1,462 tokens, every box checked against the ink) found 250 bad word boxes: Tesseract's own
boxes overrunning the neighbour, unused Tesseract words never given to the tokens of a gap, punctuation paired with any
mark, Latin-looking misreads paired left to right, and boxes spanning two printed lines. `ocr/alignment.py` now clips a
box where its right-hand neighbour starts, pairs a gap's unused Tesseract words by position (or splits their ink at clean
gaps), clamps every word and line box to its printed line band, lets a mark match only a mark of its kind inside the gap
of its matched neighbours, orders Latin-looking evidence by position under Arabic tokens, and marks a box `bq: "weak"`
when it overlaps a neighbour by > 30 %, is taller than 1.35 pitches or far wider than its letters. A weak box is drawn
dashed in review and is never a numbers-pass area. Book 22: right word boxes 1,066 → 1,166, too wide 50 → 13, two-line
boxes 31 → 0, missing 103 → 61. Book 19 (in memory): boxes overlapping a neighbour 113 → 1. Existing pages change only
with `manage.py rebuild_lines` (book 22 rebuilt). Next: a position-aware matcher, the dropped-line rescue, an upscaled
Tesseract pass for low-resolution scans (measured worse alone).
- **D63 amended (2026-09-26, after its code review).** The review found that D63 let printed page numbers stay in the
  text (10 pages), pulled line-end numbers onto the next line, let a misread mark move words across lines, and kept
  numbers in weak boxes away from Kraken. Fixed: a region's last/first number or letter takes a lone short Tesseract
  line again (the page number is dropped as before D63); a mark or number may pair only where the gap's other tokens
  still fit by page position; the main alignment treats all marks alike again (the per-kind key only inside each gap);
  «ج/ص/ط» and its number form one unit; bands are chosen by the words' cover; Latin pieces split at the ink run left to
  right; a number its gap left unread is read in its own weak box. On all 229 pages: no page-number leak, no checked
  page worse than before D63, D63's geometry kept (book 22: 1,171 right word boxes of 1,298); of the 253 numbers whose
  reading changed on books 19 and 20, 124 are now right against 42 before D63.

## D64–D79 — Phase 7: «التخطيط» first, then «المعالجة»; a safe round trip; trust (2026-09-26; details in docs/PHASE7_SPEC.md §0)
Approved by the owner on 2026-09-26 ("start with Phase 7 … do the full plan"), with all five questions of §12 answered
as recommended: the PDF text layer reversed for Chrome/Firefox (one rule, both PDFs, Arabic entries only); flag policy
v2; unreviewed pages stay in «تجميع المخطوطة» with their count on the button (D35 kept); the moved keys; the names.
- **7a:** D64 two stages with a pause (`Book.awaits_ocr_start`, default False: today's path for every existing book);
  D65 the names («التخطيط»/«استخراج الصفحات», «بدء المعالجة»/«المعالجة»; «ضبط الأدلة» dropped); D66 the upload extracts
  at once and shows the kept page range, «حذف الكتاب»; D67 the «التخطيط» mode of the dashboard; D68 thick footnote rules
  accepted at detection, strict first; D69 one keymap by physical key; D70 no silent chapter replacement, approval is
  not drift, live drift, merges keep both source marks.
- **7b:** D71 flag policy v2 (reasons, three readers, punctuation never flagged); D72 words only the second model read
  kept as groups or suggestions; D73 honest page state; D74 footnote and verse line roles; D75 readiness rows for
  doubtful text.
- **7c:** D76 the stage bar and navigation; D77 one term per concept; D78 the page-by-page merge of review changes into
  an edited book; D79 «تصحيح في كل الكتاب».
- 7d (D81+) is designed after 7c.

## D80 — The cover: a separate page, four modes, in every output (2026-09-27, `docs/COVER_SPEC.md`)

The owner asked for a cover page that is its own page: generated from the book details, an image (fit width, fit
height or filling the page) or custom text (a centre block and a bottom block), or none. It is rendered alone (one
WeasyPrint page of the trim, no margins) and never enters the interior's layout, so page numbers, sides, openings, the
footprint and the contents' numbers are unchanged. Modes `none` (the default, so no existing export changes), `info`,
`image` (`fill` / `width` / `height`), `text`; colours with named presets; faces from the stylesheet. Outputs: the book
page shows it before page 1 (not counted); the screen PDF prepends it with `/PageLabels` («غلاف», then 1…); the print
PDF leaves it out unless its option asks (printers take the cover as its own file), and then gives it the interior's
boxes and marks; Word opens with a one-page section holding the cover rasterised at 300 dpi, the next section
restarting at 1; the EPUB has `cover.xhtml` first and the rasterised cover as its `cover-image`. Images are
`editor.BookImage` rows (checked, oriented, sRGB, stored by content hash, never deleted), the pipeline body images
will reuse. Readiness warns of a missing cover image and of a low resolution.

## D82 — A misread footnote call is linked to the note it can only be; a year at a note line's start is not a marker (2026-09-28)
On book 29 («ولاة طرابلس», 294 pages, 114 notes) 63 notes were orphans (`docs/FULLBOOK_TEST_2026-09-28.md`, finding 1):
the small raised «(١)» of a call comes out of the models as an alef «(ا)» / «(أ)» (12, three glued to the word:
«أرطاة(ا)»), a quote stroke «(”)» (5), empty brackets «( )» (8), a number with a «١» hung on it, «(١١)» «(٢١)» for
(١) (٢) (9), or nothing at all (27). Three linker rules in `assembly.pipeline`, each conservative: a call that has its
note is never taken from it, nothing links across pages, and every repaired link warns `note_call_repaired`
(«قُرئت «11» في المتن؛ رُبطت بالحاشية؛ تحقّق منها»; the note's own marker is printed). (1) `two_digit_call`: a
one-digit note left without a call takes its page's unused bracketed «(n١)» when no note of the page is numbered n1
(«(٤١)» for (٢), «(١١٧)», a bare or glued «١١» stay as they are). (2) `lookalike_calls`: a bracket holding an alef, a
quote stroke or nothing (`LOOKALIKE`, glued forms too) is a candidate without a number; a page's lookalikes pair one
to one with its notes still without a call (a small number, or the marker-less note that heads the page) only when
they are as many and each lies where its note's call must be: after the calls of the notes before it and before
those of the notes after it. It runs after D74's positional link, so a strong «(١)» wins over a «(ا)» for a
marker-less note; an unpaired lookalike stays text silently (it may be a real «(أ)»). (3) `split_note_marker`: a
bare number that starts a footnote line and is followed by an era sign («١ م ه ولم ترض», «٦٤ ه وعمره») is a year,
the line continues its note; it had made a second note «١» on p. 82 and a note «64» on p. 112. Measured on the book
(the harness runs the pipeline in memory on a copy of the database): orphans 63 → 31, unmatched markers 18 → 10,
false notes 2 → 0, 30 repaired links; the empty-bracket and quote-stroke links were checked against the ink (p. 22,
132, 160, 181, 205, 214: a raised «(١)» or «(٢)» every time). What is left are calls the models dropped altogether:
those are read from the ink (D83, the call pass), not guessed by the linker.

## D83 — The call pass: the raised «(١)» of a footnote call is read from the ink by Kraken (2026-09-28, `docs/NOTE_CALLS_SPEC.md`)
After D82 book 29 still had 31 orphan notes, mostly calls no model wrote at all: the small raised «(١)» is a third of the
line's height and sits above the x-height, and Qari and Tesseract skip it or box it into the neighbouring word. Like the
numbers pass (D50) the call pass (`ocr.calls`, run at the end of the `read_numbers` task, `manage.py read_calls
--book ID [--page N] [--dry-run] [--sample DIR]`, setting `CALLS_PASS`) crops the mark and lets Kraken read it. What it
looks for is bounded by the page: the numbers its footnote markers call for and its body does not hold in brackets
(a page without notes reads nothing). Candidates are a boxed token the models wrote for a call («(ا)», «(”)», «()»,
«(١١)») and clusters of two to four small ink components no sure word box covers, 0.9–1.6 line heights tall, whose
bottom lies above the centre of the line's core rows (letters sit on the baseline, dots and hamzas are too short), 1–3.5
line heights wide, searched five line heights left of the line box too (the call after the last boxed word lies outside
it). A cluster whose word before is a misread call token is that token's true ink and replaces it; one among unboxed
words is placed by the gap's other ink, and dropped when it is not understood or may be a small number's own digits
(a year read as «(۱)» on p. 100). Each crop is read as it is and upscaled by two: on 50 candidates the two readings
differed on 27, each right where the other was wrong, so the best-formed acceptable reading wins (both brackets, one,
none; a reading «n1» is n when n is wanted and no note is n1: the «(» stroke of this print reads as a one on Kraken as
on the models, D82). The written token is «(N)», Kraken's, low (D17), with the ink's box and `call: true` (review
names it a call mark); the linker then links it as any printed call. Measured on the book (in memory on a copy of the
database): 59 pages wanted a call, 50 candidates, 30 accepted (20 from the ink, 10 misread tokens), 11 dropped as not
understood, 5 not wanted, 3 not a call, 1 duplicate; every one of the 30 accepted crops is a printed bracketed call
mark by eye (`p64` was written «(١١)» because the note's own marker reads «(١١)»; the linker links it all the same).
Orphans on the book: 114 notes, 63 → 31 (D82) → 23 on 18 pages; unmatched markers 18 → 10 → 8; marker-less notes linked by
place 9 → 14 (their calls exist now). Whole book: 84 s of Kraken on the CPU worker (one
process per page that wants a call). Not done: a note whose own marker the models misread («(١١)», «(٢١)» on p. 29, 64)
is not re-read; a call the ink filter drops (words on both sides, a number in the gap) stays for the reviewer.

## D84 — The page screen is removed; the «التخطيط» mode has its own address (2026-09-29)

`/books/<id>/pages/<n>/` («تفاصيل المعالجة»: image tabs, the manual preprocessing parameters, a re-run of any
stage for one page, the OCR text panel and the engine runs) is gone, with its API routes `/api/pages/<id>/status/`
and `/api/pages/<id>/preprocess/`, the `processing_tags` template tag, `ocr.js` and `ocr.css`. The owner: the
screen let anyone change a page after processing, and it is not a screen to hand a publisher. What it offered
lives on where it belongs: the sheet in the «التخطيط» mode (guides, «بضبط خاص», «تجهيز الصفحة» on an error,
«استثناء»), the attention list (a retry from the failed stage), review (the text, the readings). A per-page re-run
of an arbitrary stage is no longer offered.

The «التخطيط» mode of a started book was `/books/<id>/?view=guides`; a mode behind a query looked like a
technicality. It is now `/books/<id>/guides/` (`books:guides`, `book_guides`), `#sheet-<n>` opening at a page;
the old query redirects there for good (301), and before «بدء المعالجة» the address redirects to the dashboard,
which is the mode then. `primary_url` (D76) falls back to the page's sheet (`sheet_url`), as do the tile link, the
attention list, the stage bar's «التخطيط» step and the redirect after a per-page action. The
«تفاصيل المعالجة» icons on tiles and sheets and the «⋯» menu of review that held only that item are removed.
`processing.urls` keeps its API routes only.

## D85 — The linker's text-side repairs: lookalike note markers, numbers inside notes, half brackets, leftover calls (2026-09-30)

A night of footnote work on books 29 and 31 (a hadith book, 75 notes, 63 orphans) and four six-page test books
started with a diagnosis of every orphan (`playground/footnotes-2026-09-30/diagnose.py`). Four of its causes are
visible in the text alone and are repaired by the assembly's linker (`assembly/pipeline.py`):

- **A note marker read as a lookalike** («(أ) ١ ـ (الوحي)» for (١), book 31 p. 28) starts a note; its number is
  one more than the page's numbered note before it, else one less than the one after it, else 1, and becomes the
  note's marker (`_number_lookalike_notes`). Among lettered items («(ب)», «(ج)» at note-line starts) the «(أ)» is a
  letter and continues the note.
- **A bare number at a note line's start on a page that prints bracketed markers** is a marker only when it
  continues the page's sequence (one more than its last note; on a page without one, 1 or one more than the
  previous page's last note). Otherwise it is text: «١٢١ ـ عن عائشة» inside a commentary (book 31 pp. 40, 102)
  made the false notes «121» and «19», each an orphan that also cut the real note short.
- **A call read with one bracket lost** («» (” .», book 31 p. 23; «(ا» ) is a lookalike when it is a word of its
  own, as the full-bracket ones of D82 are.
- **Leftover calls** (`leftover_calls`): the page's bracketed or superscript calls of 1–15 that no note carries —
  a misread digit, «(٦)» for (١) (book 29 p. 59, «زناتة (١)» on the image), «(3)» for (٢) (p. 256) — pair with the
  notes still without a call when they are as many and lie in the page's order (the D82 lookalike rule, whose
  order check is shared: `_pair_in_order`). A number above 15 stays text (a page reference «(22)», D74); a
  crossing order stays unpaired. Each pairing warns `note_call_repaired`.

Measured in memory (`assembly.services.preview`): book 29 orphans 21 → 19 (113 notes), book 31 63 → 54 (notes 75 →
73: the two false notes are gone); every new link on book 29 was checked on the page image. D82's test that kept
a lone «(١)» or «(٦)» from a lone note (٢) is replaced: one loose small call and one note without a call pair.

## D86 — A footnote rule high on the page, or a wavy one, counts when the lines below it are notes (2026-09-30)

`detect_footnote_rule` looked only in the lower 60% of a page. A commentary edition can fill two thirds of a page
with notes under a short text: the rule of «غاية المأمول» (book 34) sat at 34% and 36% of the page, and on book 31
(a hadith book: three to five lines of hadith, then the commentary) **38 of its 120 pages** had their rule between
18% and 39%. The notes of those pages were read as main text: the commentary landed in the book's text and its
notes were never notes, which no orphan count shows. A scanned rule can also be wavy: book 35 p. 6, 5.6 px thick on
average in a 10 px box, fills 0.56 of it, under D68's `RULE_MIN_FILL` 0.6.

Now a candidate between 15% and 40% of the page height, or a thick one with a fill from 0.5 to 0.6, counts when the
lines below it look like notes next to the lines above (`smaller_below`, on the lines `measure_line_sizes` sized):
their median type size is at most 0.93 of the size above (real rules measured 0.77–0.94), or, with three lines
below at least, their median line pitch is at most 0.88 of the pitch above (real rules 0.66–0.86). Glyph size alone
is not enough: book 34 p. 6 sets its notes at 28 against 30, and a vowelled body measures smaller than its notes
because the marks are components of their own (book 31: 1.22). The top 15% of a page (a running head's rule) is
never searched. Measured on the stored images of books 29, 31, 32–35: the three missed rules of the test books and
the 38 of book 31 are found, each of the four drawn from book 31 checked on the image (pp. 33, 39, 60, 115); book
29's 285 pages do not change. Pages are re-prepared and re-read to use it.
