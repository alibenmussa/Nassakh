# Full-length book test (2026-09-28)

**Verdict.** One old letterpress scan of 294 pages went from upload to four valid exports in one afternoon, and
every stage after the OCR ran at book length without a change: preparation 2 min, OCR 101 min, then rebuild,
assembly, layout and the four exports in under two minutes of machine work per pass. What broke was at the edges
of the page image and of the model text: sparse pages turned black, short lines (chapter numbers, headings) had no
band and were glued onto the body, the hijri «هـ» became a digit, page numbers were voted wrong, a model wrote
footnote lines twice. Five of the seven bugs are fixed in six commits and each fix was checked on this book. What
stays open belongs to 7d: the footnote call marks (63 of 114 notes orphaned), a line only Tesseract read, weak
digits in small-type footnotes, and chapter headings that are lines now but not yet chapters. So the machine path
holds at book length and is an MVP; the reader's book is not yet, because it has no chapters and every dated line
needs the review pass (9.1 % of tokens flagged, 269 pages with uncertain words).

## The book

Book 29: **ولاة طرابلس من بداية الفتح العربي إلى نهاية العهد التركي** (الطاهر أحمد الزاوي, 1970), a bitonal
300 dpi scan of an old letterpress printing, 296 PDF pages, uploaded through the form path (`create.py`) with
skip 2 / 0 → 294 pages. A hijri history: dates on every page, footnotes on a third of the pages (91 footnote
regions), a chapter opening every few pages («– ٤ –» then a centred name), printer's signature marks («ولاة
طرابلس – ٢»), 17 blank leaves. Numbers below are from the data files and the dev database after the third pass
(15:49); where a number describes an earlier state, it says so.

## Timings

| Stage | Time | Source |
|---|---|---|
| Upload and page extraction | not timed separately; under a minute | `create.py` |
| «التخطيط» (preparation) | 294 pages in about 2 min (0.4 s / page); run twice, after fix 1 | `prep_progress.log`: 83 pages at 13:20:42, all 294 at 13:21:43 |
| «المعالجة» (OCR) | 101 min for 294 pages (20.6 s / page), 13:29:43–15:10:50; 2.4–3.4 pages / min per 10-minute window | `gpu-worker.log`, `ocr_progress.log` |
| Model time inside the OCR | Qari v0.3 53.7 min, v0.2 48.9 min, Tesseract 2.7 min: the GPU is the whole cost | `ocr_stats.py 29` |
| Numbers pass (Kraken) | 3.1 s median per call (p90 3.6 s), on the CPU worker beside the OCR; 359 calls during the OCR, then about 265 on every `rebuild_lines` | `ocr_stats.py`, OcrRun rows by time |
| `rebuild_lines 29` (no model calls) | 47 s, 51 s (passes 1, 2) | `after_ocr.log`, `after_ocr2.log` |
| «تجميع المخطوطة» (294 unreviewed pages) | 0.4–0.8 s of assembly work; the script measured 161–171 s because the task waited behind the numbers pass `rebuild_lines` had queued | AssemblyRun 45–47 `duration_ms` 437 / 784 / 719; `worker.log` |
| The book's layout | 12–13 s per render task (10.4–10.8 s in the renderer); 105 pages, then 108, at the default stylesheet (17×24 cm, Amiri; the book has none of its own) | `worker.log`, PreviewRender 465–467 |
| Exports | screen PDF 10.6–11.2 s · print PDF 10.5–10.9 s · Word 0.2 s · EPUB 0.1 s; all four valid, Word passes the schema check | the three pass logs |

The draft's "171 s" for assembly was the wait, not the work; the draft's "14 s" layout was the wait plus the render.

## Bugs found, in the order they showed

| # | Where | What | On this book | Fix |
|---|---|---|---|---|
| 1 | preparation | a page with under 1 % ink came out fully black (the contrast stretch took paper for ink) | 58 of 294 pages black, their lines lost; book 19 has 11 excluded pages and p. 62 read from a black image (commit message) | `d55c4d4` |
| 2 | numbers pass | «هـ» after a year taken for a digit «٥» | «سنة ٢٩ هـ» → «٢٩٩ ٨», «سنة ٤٥ هـ» → «٥ ٤٥» on dated lines (commit message) | `cf82c2e` |
| 3 | preparation, then linking | short lines under the profile threshold had no band (chapter numbers, centred headings, one-word last lines, list items), and the linker glued the primary's unanchored first lines onto the first body line | every chapter opening's number and name glued onto the body; the assembly found 0 chapters. On «كتابي» (book 19) 81 of 92 pages | `b750fd5` (630 bands added on the 535 stored pages, no other detection changed: `lines_final.json`) and `762023a` |
| 4 | numbers | Arabic-Indic digits stay weak on this book: word boxes slip around stretched words, Kraken reads extra or missing digits | 1 017 `number` flags on 32 188 tokens; no rate measured | open (7d) |
| 5 | OCR | the printed page number came from a vote of Tesseract and the two models, which misread isolated digits | agreed on 21 of 147 pages, several wrong («٦٧» for ٤٦) (commit message) | `14b8f20`: Kraken reads the region |
| 6 | OCR | a model wrote a footnote line twice in a row; one repeat passed the loop detector and the sanity check | 12 of the first 162 pages (commit message); the duplicates reached the text as «alone» flags: 381 before, 300 after | `8fc3e1d`: `strip_repeat` before the sanity check |
| 7 | OCR | a line both models skipped stays missing although Tesseract read it | p. 73 «العزيز بالله فطلب…» confirmed; 178 and 181 possible; on 294 pages | open: 7d's line audit (PHASE7_SPEC §6, the `unread` suspect) |

## The fixes, and how you will notice each

- `d55c4d4` Preparation: a page with under 1 % ink no longer turns black. Page 16 (five lines) reads; in
  «التخطيط» a sparse page shows paper, not a black rectangle.
- `cf82c2e` Numbers pass: «هـ» after a year is the era sign (D51). Dated lines keep it: p. 11 «ففتحها سنة ٢٢ هـ»;
  no «٥» or «٨» hangs after a year.
- `b750fd5` Preparation: short lines join the line bands. 103 pages now open with a «– N –» line of their own;
  in review p. 20's first line is «– ٤ –» alone.
- `14b8f20` Numbers pass: Kraken reads the printed page number (D50). «الرقم المطبوع» is set on 260 of 294 pages
  and on 250 of them equals page + 1 (the book's own numbering; the blank leaves have none). The sequence check
  has 10 to show: p. 2 «219», 39 «80», 69 «20», 76 «27», 83 «29», 146 «187», 172 «123», 227 «238», 286 «187»,
  294 «140».
- `8fc3e1d` OCR: a unit a model wrote twice in a row is read once. Page 112's footnote appears once; «alone»
  flags 381 → 300.
- `762023a` OCR linking: a region's first lines take the empty lines and bands above the first anchor. Openings
  20, 44 and 73 are number / name / body on three lines (`openings_before.json` has the glued versions); the
  second rebuild added 83 lines to the book (3 311 → 3 394).

## OCR quality (final state, `ocr_stats.py 29` re-run after the third pass)

- **Runs:** 661 per Qari model (300 body, of which 6 are the re-read openings; 95 footnote regions at 2×; 266
  page-number regions at 4×), 662 Tesseract, 1 157 Kraken. No failed run.
- **Loops:** 64 of 661 v0.3 runs (9.7 %) and 44 v0.2 runs (6.7 %) looped; 95 regions were retried at 2×. The
  book ends with 258 pages read by two models, 30 by one («قراءة واحدة»), 6 from Tesseract alone.
- **Flags:** 2 942 of 32 188 tokens (9.1 %): `disagree` 1 475, `number` 1 017 (a date on most lines), `alone` 300,
  `single` 52, `missing` 23, `script` 5; 70 low tokens carry no reason. The vote put v0.2's reading in the text
  402 times; 10 groups of words only the second model read; 21 open suggestions; Kraken's reading in 904 tokens.
- **Page flags:** `single_reader` 36, `lines_merged` 36, `missing_text` 29, `ocr_fallback` 23,
  `deskew_low_confidence` 17, `no_lines_detected` 17 (the blank leaves), `edge_strip_removed` 8, `alignment_poor` 1.
- **Worst pages** (flags): 73 (50: v0.2 looped on the body), 48 (40: a dated genealogy), 112 (30: a footnote in
  small type), 206 (30), 256 (26), 289 (26), 287 (25), 292 (24). Page 179 (34 in the draft) left the top twelve
  after fix 6.
- **Dropped lines:** the lead's proxy on the first pass (Tesseract lines of ≥ 5 words with under 35 % found in
  the text) gave 14 hits on 11 pages: one real (73), three possible (178, 181), the rest Tesseract garbage on lines
  the models read. Page 73's line is still absent after three rebuilds: Tesseract's run holds it («العزيز بالل
  فطلب منه بلككين…»), neither Qari run does.

## Manuscript, book page, exports

Three passes of rebuild → «تجميع المخطوطة» → layout → four exports (`after_ocr.log`, `after_ocr2.log`, `reline.log`):

| Pass | After | Lines | Paragraphs | Footnotes | Orphans | Words | Book pages | PDF page checks |
|---|---|---|---|---|---|---|---|---|
| 1 (15:12) | fix 6 | 3 307 | 896 | 113 | 61 | 28 907 | 105 | none |
| 2 (15:26) | fix 7 | 3 394 | 979 | 114 | 62 | 28 888 | 108 | a footnote of p. 70 ran on to 71; p. 108 held 3 lines |
| 3 (15:46) | the whole-book re-line | 3 413 | 998 | 114 | 63 | 28 889 | 108 | none |

- 75 joins across pages, 0 chapters and 0 headings in all three; running heads found: 3, 6, 6. Nothing in the
  later stages needed a fix; the size is no strain. The manuscript after the third assembly: one title and 998 paragraphs.
- **No chapters, no headings**: the `no_headings` warning («لا يوجد عنوان رئيسي في الكتاب»), an empty contents
  page, a PDF outline of 0 entries, EPUB of 12 documents. The openings are lines now (finding 4 below) but
  nothing marks them as headings.
- **Footnotes: 63 of 114 are orphans** (`note_orphan`, run 47, on 55 pages; attached to the last paragraph of their
  page). The notes themselves are found and kept; their calls are lost. See finding 1 below.
- Other warnings of run 47: 17 empty pages (7, 19, 41, 53, 55, 67, 75, 81, 97, 99, 109, 111, 121, 147, 149, 219,
  221: blank leaves with 0 lines, correctly excluded); 18 unmatched markers on 15 pages, read as «11» ×6, «21» ×4,
  «41» ×2 and five others (the misread calls seen from the other side); 9 notes without a marker linked by
  position (D74); 269 pages with uncertain words; 1 running head.
- Export warnings: Word `font_not_embedded`; EPUB `epub_notes`, `font_fallback`; the PDFs clean on pass 3.

## Open findings for the next design (7d)

1. **Footnote call marks.** 63 of 114 notes orphaned. A regex over the body-region lines of those pages (not the
   eye) sorts the 63 notes so: 34 (on 28 pages) have no bracketed mark at all: the models drop the tiny superscript
   «(١)»; 12 (11 pages) have a letter, «(ا)» or «(أ)», three of them glued to the word («أرطاة(ا)» p. 11, «الواحد(ا)»
   p. 112); 16 (15 pages) have a different number («(١١)», «(٢١)», «(٤١)» where the page prints (١) or (٢)); one
   (p. 82) has the right mark unlinked. The lead's hand count on pass 1 (61 orphans, 54 pages) was 34 / 10 / 10
   pages. This is the largest structural loss on the book and belongs with 7d's audit: a superscript-call pass over
   the line image, like the numbers pass, not the linker.
2. **The dropped line on p. 73** («العزيز بالله فطلب…»): both models skipped it, Tesseract read it, no line holds it.
   7d's `unread` suspect class (PHASE7_SPEC §6) is exactly this case; 178 and 181 are the other candidates.
3. **Numbers in small-type footnotes** stay weak: p. 112's note reads «في ٢٦ م من رجب سنة ٥ ه … ودام ملكها 8 ه سنة
   و ١ شهراً»; the 2× crop does not save the digits, and 1 017 tokens carry the `number` flag. No rate was measured.
4. **Headings are lines, not headings.** 103 pages open with a «– N –» line and the name on the next line (p. 17
   «عبد الله بن أبي سرح», 44, 73); the assembly has no rule for the pair, so 0 chapters. Also: p. 112's number reads
   «– ٠٠» with a stray «-» line under it; p. 4's calligraphic lines (the basmala, «مقدمة», «طرابلس المسلمة») exist as
   lines but read «يُسْمِلُ النَّاسُ…», «مُقَبَّدَةُمَا», «طارابلسِ لِمِتَةٍ».
5. **Two side effects to keep in view.** The tokens of the lines fix 7 creates have no `bbox` (p. 20, lines 0–1:
   7 tokens, none boxed), so the rebuild's "trailing" count rose 109 → 164 → 177 across the passes and the review
   screen cannot point at those words. And every `rebuild_lines` queues the numbers pass again (about 265 Kraken
   calls, 13 min of worker time), which is why the assembly waited three minutes each time.

## Decisions waiting for the owner

1. **Book 19 «كتابي»:** re-prepare its 12 black pages, the 11 excluded ones (2, 3, 5, 6, 10, 16, 32, 88, 90, 91, 92)
   and p. 62 (2 lines, `no_lines_detected`), then read them. The book is in review with 4 reviewed pages; those
   are untouched.
2. **Apply fix 3 to the older books.** The short-lines bands touch 138 pages of the other 27 books (348 bands;
   book 19: 81 pages / 220 bands, 16: 27, 26: 23, 21: 13, 20: 11; `lines_final.json`). Today's way was `reline.py`
   (recompute the bands from the stored original, write only `line_boxes` / `n_lines` when every other detection
   is unchanged) then `rebuild_lines`, which skips reviewed or edited pages (72 reviewed pages, 1 400 reviewed
   lines in the older books) and books with an edited manuscript unless `--include-edited`, and queues the numbers
   pass at 3 s a page. Which books, and whether `reline.py` becomes a management command, is the owner's call.
3. **The whole-book re-line of book 29** ran on the dev database at 15:45: 151 pages changed (+270 bands), 142
   unchanged, p. 83 skipped (its page-number box or footnote rule now detects differently, so the script left it),
   73 s; pass 3 above followed it. Keep it, and decide whether p. 83 is re-prepared by hand.

## What to look at

Review (`/books/29/review/<n>/`): **20** (the opening: «– ٤ –» alone, then «. معاوية بن ُحديج» with a stray
«.», then the body; before fixes 3 and 7 all three were one line), **44** and **73** (the same; on 73 also one reader
on the body, the footnote read once, 50 flags, and the line «العزيز بالله فطلب…» missing until 7d), **112**
(the number line «– ٠٠» and a «-» line above the name; the glued call «الواحد(ا)»; the footnote's digits), **4**
(the basmala, «مقدمة» and «طرابلس المسلمة» as three badly read lines to type over), **11** («أرطاة(ا)»: a letter
call, the note orphaned), **48** (40 flags: a dated genealogy), **16** (a five-line page that was black before
fix 1). The dashboard (`/books/29/`): «الرقم المطبوع» on 260 pages, and the 10 breaks listed above for the sequence
check. Then the book page (`/books/29/editor/`, `/books/29/layout/`: 108 pages, an empty contents page) and the four
exports in «الإخراج» (`/books/29/export/`). The files of this run are in the scratchpad's `fullbook/out/`, not in the
book's export history.
