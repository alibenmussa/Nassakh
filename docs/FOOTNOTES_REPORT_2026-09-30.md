# Footnotes report (2026-09-30): calls found and linked, unreadable pages cleared

The owner's goal for the night: footnote calls detected and linked "near-perfect, detect most at least", then
Tesseract-only garbage pages. This report gives the measured result, what changed, what is left and what the
owner should do next.

## Result

Measured with `playground/footnotes-2026-09-30/bench.py --fresh` on the same stored readings for both versions:
the call tokens an earlier pass wrote are undone first, the call pass runs, the book is assembled in memory, the
lines are restored. The old version is commit d0a0e39 (before tonight's call pass v2), run from a separate
checkout against the same database.

Four test books of six pages each, with 74 calls marked by hand on the page images (a link is right when the
note sits right after the word that carries the call on the image):

| Book | Calls | Right before | Right now |
|---|---|---|---|
| 32 قيمة الزمن عند العلماء (low resolution) | 11 | 10 | 11 |
| 33 هداية المتعبد السالك (Western digits) | 13 | 13 | 13 |
| 34 غاية المأمول (dense notes) | 29 | 8 | 27 |
| 35 إيصال السالك (modern print) | 21 | 5 | 19 |
| All | 74 | 36 | 70 |

The two full books, counted by the notes the assembly leaves without a call (no hand count):

| Book | Notes (before → now) | Unlinked before | Unlinked now |
|---|---|---|---|
| 29 (Libyan history, the owner's sample) | 109 → 110 | 17 | 3 |
| 31 مختصر صحيح البخاري (vowelled hadith with commentary) | 133 → 119 | 106 | 33 |

A hand-marked sample of the two full books (every fourth page with notes, 19 pages each, calls marked on the
page images; scored like the test books):

| Book | Calls | Right before | Right now | Wrong now |
|---|---|---|---|---|
| 29 | 25 | 22 | 24 | 0 |
| 31 | 27 | 6 | 17 | 1 |

Book 29's one miss is a call whose number the models and Kraken both misread. Book 31's ten failures: three are
reading failures before any footnote logic (p. 100's body dropped after both models looped, p. 106's second hadith
line missing from the text), six are calls not found or not numbered on vowelled lines (p. 86 alone has three,
with one call shape found for three notes), and one call went to an earlier word that repeats the phrase before
the real call (p. 57, «ثم سلم»).

The note counts differ between the two versions because the new one joins the parts of a note carried over a
page break into one note, and drops notes that were Tesseract's noise (D89).

## What changed

Commits 2c8c6e7, 7b90576, 76a6282 and 982085d; decisions D87, D88 and D89 in `docs/DECISIONS.md`, the pass in
`docs/NOTE_CALLS_SPEC.md`.

1. **The call is found by its shape (D87).** A pair of raised brackets sized by the line pitch, not by the thin
   core band that made book 31's calls look twice too tall. A word's box no longer hides it.
2. **Calls are numbered by count and order (D87).** When a page has as many call shapes as notes still wanting
   a call, they take the numbers in reading order. Kraken's digit is evidence only: on book 35 p. 2 it read
   «(٢)» as «۶» and «(٣)» as «٢».
3. **The model's reading of a call gives way to it (D87).** «المخالفة"٢"، (٢)» is now «المخالفة (٢) ،»; the forms
   «"٢"», «٤»», «"ا"», «(”», «‘‘» are parsed once, and the text keeps its own quotes and marks.
4. **Western-digit books get the pass too (D87).** Book 33 was skipped before.
5. **The notes' own sequence repairs them (D87).** A marker misread against its neighbours («(١) (٤) (٣)»), a
   marker the model dropped, a stray «أ» before «(١)», «(٢١)» for «(٢)», a next note merged into the line before,
   a «=» line carried from the page before, and a hadith number «١٩ ـ» taken for a marker.
6. **Carried commentary joins its note (D87).** On book 31 a note runs over pages and often breaks at a
   sentence's end; the lines above a page's first new note now continue the note before. This alone joined 18
   notes of book 31.
7. **The page number is never taken above a line of text (D88).** Book 34 p. 5 lost its sixth note because the
   last short line of note 5 was taken for the page number. That page was re-prepared and re-read.
8. **Tesseract's reading of a photo is no text (D89).** When both models fail and Tesseract's words have a mean
   confidence under 50, are half Latin, are an ornament's few letters or hold no letter, the region keeps no
   text. A page left empty is flagged «صورة، لا نص مقروء» on its sheet, next to «استثناء» in «التخطيط». A page
   with text elsewhere is flagged «نص قد يكون ناقصًا». The provisional text skips such regions too.

## What is left

1. **Line shifts in the model's text.** The model sometimes puts a line's last words at the start of the next
   line (book 34 p. 6 «جداره», book 35 p. 2 «علم الأصول»). The call lands one word early. The cause is in
   `ocr/alignment.py`: the model's words are matched to Tesseract's boxes across lines, and a tie takes the
   wrong line. The fix needs the model's own line breaks as a tie-break, measured on more pages than two.
2. **Invented text.** On book 35 p. 6 the models added a line that is not printed and put a call on it.
3. **Book 31's remaining unlinked notes** (33). Five belong to the three pages of item 4, whose body is gone.
   Most others are on vowelled pages where the call's ink sits among the marks and is not found, or Kraken
   reads a number the page does not want; a few are on pages whose body was read as one line or has no body.
   The next step there is visual: the page's own note markers, read at 2×, as templates for its calls.
4. **Vowelled hadith pages where both models loop.** Book 31 pp. 88, 93 and 100 lost their body text under
   D89: Tesseract read them at 44–50 with much noise. They are flagged «نص قد يكون ناقصًا». The right fix is to
   read such a region again with the models in smaller pieces before falling back to Tesseract.

## For the owner

- **Workers.** At 02:46 the owner's CPU worker (pid 42303) was stopped and two workers were started instead:
  `cpu@` (queues default, layout, export) and `gpu@`. They were restarted on the newest code at 10:38. Their logs
  are `playground/footnotes-2026-09-30/cpu-worker.log` and `gpu-worker.log`.
- **Books changed in the database** (all test data; book 30 untouched): book 31's 38 pages re-prepared and
  re-read under D86 (night), book 34 p. 5 re-prepared and re-read under D88, book 31 pp. 11–19, 21, 45, 49, 52,
  88, 93, 100 and book 29 pp. 2 and 263 re-finalised from their stored readings under D89. The backup taken
  before last night's cleanup is `~/Nassakh-backups/2026-09-29-before-cleanup/`.
- **Existing books keep the old call pass's tokens** until the new pass runs on them. To give book 29 or 31 the
  new pass, run `.venv/bin/python manage.py read_calls --book 29 --redo` (add `--dry-run` first to see what it
  would write). `--redo` undoes the calls the old pass wrote, then reads again; approved pages and reviewed
  lines are never touched. Books printed with Western digits are read too now.
- **To check by eye:** book 34 (pp. 1, 2, 5, 6) and book 35 (pp. 2, 3) in review, where most calls now appear;
  book 31 p. 11 in «التخطيط» for the new «صورة، لا نص مقروء» flag.
