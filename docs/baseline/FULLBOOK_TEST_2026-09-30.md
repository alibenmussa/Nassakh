# Full-length book test 2: a vowelled hadith book (2026-09-29/30)

**Verdict.** The whole machine path ran on a second, very different book without a crash: 120 pages from the upload
form to four valid exports. The reading took 95 minutes, 47 s a page, 2.3 times book 29, because fully vowelled
text is about twice as many characters for the models to write. On vowelled hadith text the reading is good but not
clean. On two hadith pages checked word by word against Sahih al-Bukhari (al-Bugha's edition, from the owner's
Shamela library) and against the page image, 10 of 257 words had a wrong letter and 10 more a wrong vowel mark;
half the letter errors and none of the mark errors were flagged. Three systematic patterns explain most of it, and
one is fixable by a simple rule. The book's structure came out weaker than book 29's: 63 of 75 footnotes are
orphans because the call pass rejected all 30 of its candidates, 22 pages fell back to Tesseract, and there are no
chapters. No code was changed during the test; the fixes are proposed at the end.

## The book and the setup

- **Book 31, «مختصر صحيح البخاري»** (ابن أبي جمرة), a 402-page bitonal scan from `~/Documents/Books/`. Pages 31–150
  uploaded through the new-book form (skip 30 at the start, 252 at the end), as the owner would.
- **Mixed content:** the editor's introduction (pages 1–8), photographs of the old manuscripts (about pages 10–20),
  the author's preface, then numbered hadith with long commentary footnotes by the editor.
- **A decorative frame on every page.** The printed page number sits in a small cartouche inside the bottom border.
- **Guides set in «التخطيط» before «بدء المعالجة»:** a header cut at 3.4 % of the page height (the top border ends
  by 2.6 %, the text starts at 4.2 % or lower on all 120 pages) and a bottom page-number zone of 4.5 %.
- **Cleanup before the test:** the 27 old test books were deleted through the app; books 29 and 30 were kept. The
  backup is in `~/Nassakh-backups/2026-09-29-before-cleanup/`: a full database dump (11 MB, all 28 tables) and
  the image folders of the deleted books (620 MB).

## Timings

| Stage | Time |
|---|---|
| Upload and page extraction | under a minute |
| «التخطيط» (preparation) | about 1 minute for 120 pages |
| «المعالجة» (reading) | 95 minutes, 23:14–00:49, 47.5 s a page; about 40 s early, 1.3 pages a minute late |
| Numbers pass (Kraken) | ran beside the reading; caught up within a minute of the end |
| «تجميع المخطوطة» | 0.6 s |
| The book's layout | 14 s (worker), 77 pages |
| Exports | screen PDF 12.5 s · print PDF 12.6 s · Word 0.1 s · EPUB 0.1 s |

## What worked

- No crash, no worker error, no manual step beyond the guides.
- Preparation found the footnote rule on 49 pages, each in the right place, and the regions follow it.
- The hadith text keeps its full vowelling: 0.25 vowel marks per letter over the whole book.
- All four exports are valid: no page-check warning on either PDF, and the Word file passes the schema check.

## Accuracy on vowelled text, measured

Two hadith pages, compared first with al-Bugha's Bukhari, then every difference checked against the page image.
Differences that are only wording between the two editions («يَا بْنَ», «أَنْزَلَ», «جَذَعاً», «أَيُّ»…) are not
counted as errors; our reading matched the print on each of them.

| Page | Words | Letter errors | of which flagged | Mark errors | of which flagged | Edition differences |
|---|---|---|---|---|---|---|
| 28 (start of revelation) | 148 | 5 | 3 | 6 | 0 | 7 |
| 40 (Asma, the grave) | 109 | 5 | 2 | 4 | 0 | 6 |
| **Both** | **257** | **10 (3.9 %)** | **5** | **10 (3.9 %)** | **0** | 13 |

Three systematic patterns:

1. **The superscript alef.** The print writes «هَـٰذَا» with a small alef above; the models read it as «هَنْذَا»,
   with a nun. It happened **40 times** in the book, never flagged. «هنذا» is not an Arabic word, so a rule can fix
   it safely.
2. **The wasla alef.** The print has «ٱ» at the start of «ٱبْنَ», «ٱمْرَأً», «ٱسْمَعْ», «ٱتَّبَعْنَاهُ»; the models
   write «أَ», a hamza with a fatha, which changes the pronunciation. 5 of the 257 words. A rule cannot fix it,
   because real «أَ» words have the same shape.
3. **Marks never reach the doubt flags.** A missing shadda («الدَّجَالِ»), a missing tanween («لَمُوقِنَا»), a wrong
   vowel («هَوْ» for «هُوَ»): the vote compares the two readings, and when both models agree on the wrong mark,
   nothing is flagged.

Over the whole book, 5.6 % of the words are flagged for review (1,698 of 30,417); book 29 had 9.1 %.

## Findings, most important first

1. **Footnote calls: 63 of 75 notes are orphans.** On the 48 pages that need a call mark, the call pass found 30
   candidates and accepted none. Eighteen were rejected as "unplaced words in the gap": the models wrote the raised
   «(١)» after a hadith as «‘‘», or glued it to the closing «»», and the pass treats that token as a word sitting in
   the gap. The rest were not wanted or not a call.
2. **22 pages fell back to Tesseract, for three different reasons** (checked by eye on the pages named; the
   others are grouped by their line counts and are not verified one by one):
   - **The manuscript photographs** (pages 10–19) came out as nonsense text; page 14 has 470 such words, only 6
     flagged. The page carries the fallback flag, but nothing proposes excluding a photo page.
   - **Pages whose main text is only a row of dots and an ornament** (page 49; probably 45 and 52). The models read
     the dots correctly, and the loop detector called it a loop. Pages 21 and 26 are near-empty too.
   - **Pages filled by a long commentary in small print** (pages 61 and 87; probably 88, 93, 100, 103, 109): both
     models looped on the main text region at normal scale, while footnote regions are read at double scale.
3. **The frame.** The borders were detected as text lines and the page number was found on 0 of 120 pages; the
   guides fixed both. The printed number was then read on 45 pages, several wrongly («٥١» as «01», «٦٠» as nothing),
   because the 4.5 % zone I chose cuts the digits in half: the cartouche rises above the border. My setting, and a
   lesson: the zone preview should show the whole number, and the pass could pad its crop upward.
4. **Hadith numbers.** «١٢ ـ ٨٦» came out as «12 ـ ٨٨١»: model v0.3 read «81», model v0.2 «١١ ـ ١٢», and Kraken's
   «٨٨١» was applied. The small bold digits defeat all three readers. In a hadith book these numbers are the
   references, so they matter.
5. **Headings.** Decorative headings are misread («منهج العمل في الكتاب» as «منج عمصل في الكتاب»); the book has 0
   chapters and 0 headings.
6. **Short lines merged into the line above** on 23 pages. The words are all there; only the line structure is off.

## Proposed fixes, in order of value

1. **Call pass:** a quote-like or punctuation token in the gap, where the raised ink is, is the misread call itself,
   not a blocking word. Then measure the orphans again on books 29 and 31.
2. **Normalization rule:** «هنذا / هنذه» (with their prefixes) become «هٰذا / هٰذه»; also v0.2's «ذللك».
3. **Page kinds:** detect a photograph page and propose excluding it in «التخطيط»; a line of dots or an ornament
   is not a loop.
4. **Small print filling a page:** read the main text region at double scale when its type is footnote-sized.
5. **Page numbers in a frame:** the zone preview shows the whole number; the numbers pass pads its crop upward.
6. **Hadith numbers:** a sequence check across pages (each number one more than the last) flags misreads.

Fixes 1, 3 and 4 belong with the 7d audit; fix 2 is a small rule with a test.

## Files

`playground/fullbook-2026-09-29/` (ignored by git): the upload, status, preparation, statistics and comparison
scripts, the reading log, the GPU worker log, the call-pass dry run (`calls_dry.log`), the image crops used above,
and the four exports in `out/`.
