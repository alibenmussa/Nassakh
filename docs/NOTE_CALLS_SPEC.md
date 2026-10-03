# The call pass: the raised «(١)» of a footnote call, read from the ink (D83; v2 D87)

**Why.** On book 29 the models dropped or misread the small raised call mark of 63 of 114 notes
(`FULLBOOK_TEST_2026-09-28.md`, finding 1). The linker's lenient rules (D82) repair the misread ones
(63 → 31 orphans); what is left is a mark no model wrote at all. Like the numbers pass (D50), the call pass
crops the mark from the page image and lets Kraken read it, then writes a token back into the line.

**Where it runs.** `ocr/calls.py`, `read_page_calls(page, engine, dry_run)`: after the numbers pass, in the same
task (`ocr.tasks.read_numbers`, default queue, one Kraken process per page), so every finalised page gets it
(D87: a Western-digit book too, numbered by order without Kraken, whose digits model reads Arabic-Indic only); `manage.py read_calls --book ID [--page N] [--dry-run] [--sample DIR]` runs it on
pages already read. Never on an approved page or a reviewed line; a line the reviewer changes while Kraken reads is
left as they made it (the page row locked, `updated_at` compared, as the numbers pass does).

**What is wanted.** Only a page with footnote lines has calls to find. Their markers are set when the lines are
built, from Kraken's reading of the footnote lines and the page's run of numbers (D103, `ocr/markers.py`): the
models renumber the notes after one they dropped. Its *wanted* numbers are the numbers of
its notes as the linker parses them (`note_numbers` calls `assembly.pipeline.page_notes`, so its repairs count:
lookalike markers, a number restored or put back in sequence, a «=» line continuing the note before; D85, D87)
minus the bracketed numbers already in its body lines; a page whose first note has no marker wants 1 too (that
note heads the page). Nothing wanted → nothing is read.

**Candidates** (body-kind lines only, `ocr.services.line_kind`, with a line box):
0. *A bracketed call by its shape* (D87, `bracket_calls`, the first source): raised components 0.14–0.46 of the
   line pitch tall whose bottom ends above the line's core top, joined while close at one height; a group of 2–5
   whose outer parts are thin brackets of one height and top, 0.1–3.2 heights apart, is a call. Sized by the
   line pitch, not by `median_line_height` (the core band: 12 px on book 31, where the brackets are 25). A word's
   box does not hide it (Tesseract boxes «الشافعي(٢)» as one word). The search reaches 0.45 pitch above the line
   box (D93: Kraken's bands are tight at the top); ink there whose middle lies above the box and which sits
   inside the line above's box is that line's and is left out (D103). The clusters of item 2 follow where none
   overlaps. A model's reading of the call boxed on its ink (`is_reading_token`, or a bracket and one letter,
   «(ك» for «(٣)», D103) is replaced by it.
1. *A misread token with a box*: a bracket holding an alef, a quote stroke, nothing, or one or two digits
   («(ا)», «(”)», «()», «(١١)»): its own word box is the crop.
2. *Uncovered ink*: the line box, widened to its left by 5 line heights (a call after the last boxed
   word lies outside the box the word boxes make), is thresholded (gray < 128) and its connected components
   taken. A component more than 3 px inside a sure word box (not a weak one: a weak box may have swallowed
   the call; a box clipped at its neighbour may end on the call's ink) is that word's. Of the rest, a
   component is *call-like* when its height is 0.9–1.6 × the page's median line height, its width at most
   1.0 × it, and its bottom lies at or above the centre of the line's core rows (the longest run of rows
   holding half the densest row's ink: the x-height band): letters sit on the baseline, dots and hamzas
   are too short. Call-like components closer than 0.8 × the line height to each other form a cluster; a
   cluster of 2–4 components, 1.0–3.5 line heights wide, is a candidate and its union box the crop. The
   word before a cluster is the last boxed token whose box centre lies right of the cluster's; when that
   token is itself a misread call token (a «(١١)» boxed on the wrong ink), the cluster is its true ink and
   replaces it. Otherwise the cluster goes after that word, in place of the unboxed lookalike glyph tokens
   of its gap; unboxed real words in the gap are placed by the gap's other ink (all of it right of the
   cluster: the call follows the run; all left: it precedes it; a side narrower than one line height is a
   mark, «(١) ،»); words on both sides drop the cluster («unplaced words in the gap»). D87: an unboxed word
   carrying the call's reading glued to it takes the call after it; words on both sides are placed by the
   run's letters spread over its width (`estimate_place`, marked `estimated`), a single reading of a call in
   the run being the call itself.

**What the models write for a call** (D87, `call_reading`): strokes (brackets, quotes, «»») around at most two
digits or an alef, or digits followed by closing quotes — «"٢"», «٤»», «"٢٢»», «"ا"», «(”», «‘‘» — glued to the
word or alone; never bare digits and never digits before a bracket («٢٣]» ends a verse number). The word's own
closers stay its: a closer whose opener is in the word («(يؤذيهما)», «"الموطأ"»), a leading «»» («الخبيثَ»٤»»: the
quotation ends there).

**Reading.** The crops go to Kraken in one call (`kraken_runner`, margin 0.3 × height; `scale` upscales the crop
by an integer factor before recognition and divides the positions back: the pass reads at 2×, chosen after
measuring 1× against 2× on the book). A reading *supports* a number when it is one or two digits, optionally in
brackets, and its value is a wanted number of the page (a reading «n1» whose n is wanted and which is no note's
number is n: the «(» stroke of this print reads as a one, on Kraken as on the models, D82).

**Numbering** (D87, `number_calls`). The calls already in the text fix their numbers at their places. Between two
of them (or a page end), when the ink candidates are as many as the numbers still wanted there, they take those
numbers in reading order whatever Kraken read («accepted» when the reading agrees, «accepted by order» when not):
one note per call is the printer's rule, Kraken's small raised digits are not reliable. Otherwise the readings that
agree with each other and with the order keep their numbers (the most evidence, by dynamic programming, `_aligned`)
and the order numbers the candidates between them when the counts match there. A token candidate (no ink shape)
takes a number only from its reading. Everything else keeps its reason (not a call, not wanted, out of order).

**Write-back.** A misread token becomes «(N)» in place; a cluster becomes a new token «(N)» after its word
(replacing the lookalike tokens of its gap). D87: the call follows the gap's closers and precedes its marks
(«الجنة » (١) .»), a reading glued to the word gives way to it («المخالفة"٢"،» → «المخالفة (٢) ،»), a Western-digit
book gets «(1)»; a line's edits apply from its last token back so none moves a token still to edit. The token is Kraken's (`src`), low (D17), `digit`, with its box,
and carries `call: true` so review names it a call mark («علامة حاشية قرأها Kraken من الصورة») and keeps what the
models wrote under `qari`. `Line.text` and `n_low` follow, the line's suggestions follow their words
(`token_moves`, `_shift_gaps`), `final_text` is refreshed. One `OcrRun` row per page (engine kraken,
`input_variant` gray_2x, `params` = `{"pass": "calls", "wanted", "candidates", "read", "accepted",
"rejected"}`).

**How the linker consumes it.** Nothing new: the written token is a bracketed «(N)» in the body text, the
strongest candidate style; `link_footnotes` links it to note N as any printed call.

**Dry run and measurement.** `--dry-run` runs everything but the write and prints, per page, each candidate:
its line, box, source (token / ink), Kraken's reading, accepted or the rejection reason; `--sample DIR`
saves the crop of every accepted call as a PNG named `p<page>_L<line>_<N>.png` for a check by eye. The
measurement harness (assembly in memory on a database copy) gives the orphans before and after.
