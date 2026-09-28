# The call pass: the raised «(١)» of a footnote call, read from the ink (D83)

**Why.** On book 29 the models dropped or misread the small raised call mark of 63 of 114 notes
(`FULLBOOK_TEST_2026-09-28.md`, finding 1). The linker's lenient rules (D82) repair the misread ones
(63 → 31 orphans); what is left is a mark no model wrote at all. Like the numbers pass (D50), the call pass
crops the mark from the page image and lets Kraken read it, then writes a token back into the line.

**Where it runs.** `ocr/calls.py`, `read_page_calls(page, engine, dry_run)`: after the numbers pass, in the same
task (`ocr.tasks.read_numbers`, default queue, one Kraken process per page), so every finalised page of an
Arabic-Indic book gets it; `manage.py read_calls --book ID [--page N] [--dry-run] [--sample DIR]` runs it on
pages already read. Never on an approved page or a reviewed line; a line the reviewer changes while Kraken reads is
left as they made it (the page row locked, `updated_at` compared, as the numbers pass does).

**What is wanted.** Only a page with footnote lines has calls to find. Its *wanted* numbers are the markers
that start its footnote lines («(١) …», the same regex as the assembly's) minus the bracketed numbers already
in its body lines; a page whose first footnote line has no marker wants 1 too (that note heads the page).
Nothing wanted → nothing is read.

**Candidates** (body-kind lines only, `ocr.services.line_kind`, with a line box):
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
   mark, «(١) ،»); words on both sides drop the cluster («unplaced words in the gap»).

**Reading.** The crops go to Kraken in one call (`kraken_runner`, margin 0.3 × height; `scale` upscales the crop
by an integer factor before recognition and divides the positions back: the pass reads at 2×, chosen after
measuring 1× against 2× on the book). A reading is *accepted* when it is one or two digits, optionally in
brackets, and its value is a wanted number of the page (a reading «n1» whose n is wanted and which is no note's
number is n: the «(» stroke of this print reads as a one, on Kraken as on the models, D82); the first accepted crop of a number in reading
order takes it, a second one with the same number is rejected («duplicate»). Everything else is rejected
with its reason (not a number, not wanted, unplaced words).

**Write-back.** A misread token becomes «(N)» in place; a cluster becomes a new token «(N)» after its word
(replacing the lookalike tokens of its gap). The token is Kraken's (`src`), low (D17), `digit`, with its box,
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
