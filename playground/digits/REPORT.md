# Digits experiment: reading Arabic-Indic numbers (2026-09-25)

## Question
All our readers misread Arabic-Indic numbers (٠١٢٣٤٥٦٧٨٩). Does Tesseract with a digits-only
whitelist fix it? Does Qari read them better when it is given the number (or its line) alone?

## Sample
- One book per distinct source PDF in the dev database: books 9, 13, 15, 16, 17, 18, 19, 20, 21.
- Candidates: OCR words that hold a number and have a word box. A seeded random pick per book (24),
  plus 30 more each from «كتابي» (19) and «قائمة المراجع» (20), so the Arabic-Indic side is large enough.
- 222 crops, each labelled by hand from the image (`truth.json`, `sheets/`). Left out of the scores:
  books 9 and 18 (their pages repeat book 13's scans), crops with no digit in them (misplaced word boxes,
  ornaments), crops that were cut or unclear.
- **Scored: 188 crops, 213 numbers — 116 crops with Arabic-Indic digits** (books 13, 17, 19, 20) and
  72 with Western digits (books 15, 16, 21, the control group).

## Metrics
- **exact**: the crop's numbers are exactly right (a crop with one number, most of them: right or wrong).
- **numbers**: share of the true numbers read exactly.
- **nothing**: the reader returned no digit at all.

## Results — Arabic-Indic digits (116 crops)

| Reader | exact | numbers | nothing |
|---|---|---|---|
| Qari v0.3 — what the page run already gave | **43.1%** | 38.9% | 0.9% |
| Qari v0.2 — what the page run already gave | 39.7% | 35.9% | 3.4% |
| Qari v0.3 on the number alone (x1 / x4) | 12.1% / 15.5% | 12.2% / 14.5% | 33% |
| Qari v0.2 on the number alone (x1 / x4) | 12.9% / 16.4% | 13.7% / 16.8% | 31–33% |
| Qari v0.3 / v0.2 on the whole line, 2x | – | 29.8% / 24.4% | 15.5% |
| Tesseract — the page run's word | 0.9% | 1.5% | 62.9% |
| Tesseract ara+eng on the number (x1 / x3) | 6.0% / 8.6% | 5.3% / 7.6% | 50–53% |
| **Tesseract digits only** (ara, x1 / x3) | 9.5% / 8.6% | 9.2% / 7.6% | 25% |
| Tesseract digits only, single-word mode | 0.0% | 0.0% | 37.9% |
| Tesseract digits + nearby marks and letters | 8.6% | 7.6% | 27.6% |
| Tesseract Persian model, digits only | 30.2% | 26.7% | 11.2% |

Per book (exact): the page run of Qari v0.3 gets 81% on book 13 (clean, large digits), 31% on
«كتابي», 36% on «قائمة المراجع» (grey scans).

Control — Western digits (72 crops): Qari 87–90% exact (page run or crop), Tesseract at best 67%.
So the readers are fine with Western digits; the problem is specific to Arabic-Indic ones.

Speed on this Mac: Tesseract ≈ 20 ms a number; Qari (MLX) ≈ 0.3–0.5 s a number, ≈ 1.6–1.9 s a line.

## What goes wrong
- **Tesseract's Arabic model does not know these digits.** Forced to digits, it answers «١» or a
  Western digit for almost anything: «٢٤» → 34, «٣» → 5, «(١)» → 00. Its Persian model knows the shapes
  (it read «١٩٣٩» and «٣٣٢» right) but mixes «٢» and «٣» and turns brackets into digits.
- **Qari needs the page around a number, and still misreads it.** On a lone number the page prompt
  misfires: it answers like a chat («The plain text representation of the document is: ص 38)»),
  wraps text in markup, drops «١» from «(١)» and reads «٨» as the Greek «λ». With the line around the
  number it does better than alone but still worse than on the whole page.
- **The most common slip is «٣» → «٢»** (9 times for each Qari model), then wrong digit counts
  (a quarter of v0.3's numbers have digits added or lost; it writes «1328» for many different years).
- **Combining today's readers cannot fix it**: for only 55% of the numbers is at least one reader
  (page v0.3, page v0.2, Tesseract Persian) right, and when two of them agree the value is right only
  half the time (they share the same slips).
- Side finding: the word boxes we would use to find numbers are sometimes wrong (3 of 162 first-batch
  crops held a different word, several were cut).

## Conclusion
Neither Tesseract with a digits-only whitelist nor Qari on the number alone is good enough; both are
worse than what the page run already gives (43%). Arabic-Indic numbers need a reader of their own.

## Round 2 (2026-09-26): Kraken and PaddleOCR

Same 188 crops and labels. Both engines run in their own environment under `playground/digits/`
(`.venv-kraken`, `.venv-paddle`, 830 MB each), never in the project's `.venv`.

| Reader | Arabic-Indic exact | Western exact | Model file | Time per number |
|---|---|---|---|---|
| **Kraken, OpenITI printed Arabic-script base model** (`all_arabic_scripts.mlmodel`, CC0) | **92.2%** | 70.8% | 16 MB | **6 ms** (CPU) |
| Kraken, Jawi model (fine-tuned from the base on Jawi newspapers) | 72.4% | 81.9% | 16 MB | ≈ 6 ms |
| PaddleOCR, Arabic PP-OCRv5 mobile | 51.7% (22% nothing) | 84.7% | 8 MB | 49 ms |
| PaddleOCR, Arabic PP-OCRv3 mobile, digit order fixed | 37.1% | 20.8% | 9 MB | 28 ms |
| Qari v0.3, the page run we have today | 43.1% | 90.3% | 4.1 GB | ≈ 400 ms (MLX) |
| Tesseract, digits only | 9.5% | 59.7% | 1.4 MB | 20 ms |

Kraken's base model per book (exact): book 13 95%, book 17 100%, «كتابي» 94–96%, «قائمة المراجع» (grey
scans) 86–89%. Its 9 misses: tiny superscript footnote markers («٣» → «٢», a tiny «٤» → commas), a
comma «،» beside a number read as an extra «٤», and the grey scans («٨٥٥٢» → «٨٠٠٢»). BiDi base direction R
is right for it (L loses 4–7 points). It writes some digits as Western or Persian code points (the value
is right; convert them). PP-OCRv3 gives the characters right to left, so numbers come out reversed;
PP-OCRv5 does not, but returns no digit on a fifth of the numbers.

**Winner for Arabic-Indic numbers: Kraken with the OpenITI base model**, more than twice Qari's
accuracy, small and the fastest. Western digits stay with Qari (90%). A book prints one digit style,
so the choice is per book; Kraken's code points do not reveal the style (it writes Western digits for
Arabic-Indic print too), so the style needs a book setting (detected, confirmable).
Integration note: Kraken 6 pins torch ≤ 2.9 and numpy 2.0 (the project runs torch 2.14), so it needs
its own environment (a small worker) or a port of its recognition network.

## Round 3 (2026-09-26): numbers Qari wrote as letters
Qari writes some Arabic-Indic digits as the letter they look like, so the word holds no digit and the
numbers pass (D50) never reads it: «(ج ا، ص٢٣٨)» for «(ج١، ص٢٣٨)», footnote marks «ا» for «١» / «١١»,
«(ه) الخزر» for «(٥) الخزر», and dates with their digits gone, «(ع ه – ه م)» for «(٧٥٤–٧٧٥م)»,
«(هـ – م)» for «(٨٤٧–٨٦١م)». `letters.py` collects, in the six books printed with Arabic-Indic digits
(one per source PDF), every lone «ا»/«ه»/«هـ»/«ع» (85), bracketed single letter (21) and bracketed date
without digits (3). 80 of the 109 were labelled from the crops (29 crops showed another word: the
word box belonged to a neighbour): 56 letters standing for (part of) a number, 13 real letters
(«(هـ)» in a lettered list, the hijri «هـ» after a year, the index heading «(ع)»), 6 punctuation or
nothing, 3 dates. Nearly every lone «ا» is a digit (Arabic has no one-letter word «ا»); «ه» is both.

| Rule (77 labelled letters) | Numbers read (of 56) | Exact (known value) | Real letters changed (of 13) |
|---|---|---|---|
| Kraken on the letter's own area, margin 0.3 (the pass's) | 21 | 13 | 1 |
| own area, side margin 0.15 | 23 | 17 of 17 | 0 |
| Kraken on the whole line, the number between Qari's neighbours | 39 | 36 of 37 | 1 |
| **line, else own area; not when the box shows Qari's letter** | **45** | **40 of 41** | **0** |

"Own area" means one number there and the rest of Kraken's reading equal to Qari's (brackets,
punctuation); "between Qari's neighbours" means the digits found exactly once in the line between the
last two letters of the word before and the first two of the word after (punctuation, marks, alef
forms ignored). The line wins because a third of the letters' word boxes hold the next word
(`(ج ا، ص٢٣٨)`: the box of «ا،» shows «ص٢٣٨).»), and a gap reading takes in bits of the neighbours.
The veto (Kraken reads Qari's letter in its box) keeps «(ع)» a letter; the line alone read it «(۶)».

The pass's own code on the same pages (`letters_dryrun.py`, unreviewed lines only, nothing saved):
38 of 47 letters read, 33 of 34 exact, no real letter changed, the 3 dates exact (read in their own
area; the whole line read «٨٤٧» as «٨٥٧»). Kraken takes about 2 s a page for it. Misses: a letter Qari
split off a number («2 ه ، 3 ه» for «٥٢ ، ٥٣») gets its digit but stays a word of its own; tiny marks.

Found on the way, fixed in the pass: Kraken writes the Persian six «۶» for the page's «٤» (the same
shape: «٧٥۶» for «٧٥٤», «ص۲۶٠» for «ص٢٤٠», «(۶)» for «(ع)»), so «۶» is read as «٤»; it writes «0» for
a bold «٥» in a tight box (read as «٥» where Qari saw «ه»); and when it drops the spaces between
numbers set apart by a comma it gives them left to right («٥٢ ، ٥٣» → «٥٣،٥٢»; 16 of 114 multi-number
areas of the pass disagree with the page's order), so the page's order is used when it agrees better
with Qari's digits (a slash date set left to right keeps Kraken's order).

## Ideas for the next round (not tested yet)
- Fine-tune Kraken's base model on our books' typefaces (the OpenITI study reached > 97.5% character
  accuracy with 800–1,000 lines of a typeface); corrected lines from review are that training data.
- A small digit classifier (CNN) trained on digits we render in the books' fonts, run on each digit
  shape (digits are separate pieces of ink, read left to right).
- Matching digit shapes against examples taken from the same book.
- Finding numbers from the ink itself (the word boxes are not reliable enough), and showing each
  number's image next to its reading in review.

## Files and re-running (from the project root)
```
.venv/bin/python playground/digits/collect.py              # crops/, manifest.json, sheets/
.venv/bin/python playground/digits/collect.py --extra 19:30 20:30
.venv/bin/python playground/digits/run_tesseract.py        # results/tesseract.json   (≈ 40 s)
.venv/bin/python playground/digits/run_qari.py             # results/qari.json        (≈ 6 min)
.venv/bin/python playground/digits/run_qari_lines.py       # results/qari_lines.json  (≈ 11 min)
playground/digits/.venv-kraken/bin/python playground/digits/run_kraken.py   # results/kraken.json (≈ 20 s)
PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True playground/digits/.venv-paddle/bin/python playground/digits/run_paddle.py
.venv/bin/python playground/digits/score.py                # the tables above
```
Round 3 (letters):
```
.venv/bin/python playground/digits/letters.py find          # letters.json, letters_request.json
.venv-kraken/bin/python -P ocr/engines/kraken_runner.py < playground/digits/letters_request.json > playground/digits/letters_result.json
.venv/bin/python playground/digits/letters.py sheet         # sheets/letters-NN.png + the readings
.venv/bin/python playground/digits/letters_eval.py request  # then the runner on letters_eval_request.json
.venv/bin/python playground/digits/letters_eval.py score    # the table above
.venv/bin/python playground/digits/letters_dryrun.py        # the pass's own code, not saved
```
`letters_truth.json` holds round 3's labels.

`truth.json` holds the hand labels; add a reader by writing `results/<name>.json` as
`{variant: {crop id: text}}` and `score.py` picks it up.
