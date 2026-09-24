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
error are skipped with a warning; excluded pages never appear. Footnotes are numbered per chapter by default (per book or
per page as options, remembered per book); exported numbering follows the stylesheet (Phase 6). Spec: PHASE4_SPEC §0.

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

