# Word export spike (2026-09-26)

## Question
Can the book model the preview uses (`publishing.model.book_model`) become a professional `.docx`
that Microsoft Word lays out like the book page (WeasyPrint)? What breaks, and what must the real
renderer do?

## Set-up
`spike.py` builds «كتابي» (book 19: 6 chapters, 40 footnotes, Simplified Arabic 13 pt, 17×24 cm,
line height 1.7) with python-docx 1.2 plus direct Office XML: RTL sections and paragraphs, the Arabic
face in Word's complex-script slot, real Word styles (Normal, Heading 1/2, Quote, Title, Footnote Text /
Reference), a real footnotes part numbered per page (`numRestart eachPage`), a section per chapter,
mirrored margins, a PAGE field in the footer, a TOC field, a bookmark on every chapter title.
Microsoft Word 16.113 on this Mac then opens the file (AppleScript) and reports its page and line
counts. Every file is first checked against the ECMA-376 WordprocessingML schema (python-docx's
`ref/xsd`, plus a small `xml.xsd`), so Word never sees an invalid file.

## Results

| Word justification | Pages | Body lines | Note lines |
|---|---|---|---|
| **Preview (WeasyPrint)** | **84** | **≈ 1,854** | **101** |
| justify (`both`) | 85 | 1,855 | 101 |
| **kashida low (`lowKashida`)** | **85** | **1,855** | **101** |
| kashida medium | 91 | 2,020 | 112 |
| kashida high (measured before fix 3) | 104 | 2,246 | 121 |

Per chapter (low kashida, each chapter alone): 1/1, 3/3, 5/5, 16/16, 31/30, 27/27 pages (Word / preview).
Word breaks the lines exactly as WeasyPrint does when the line pitch is the same and kashida is low
or off; the book is one page longer. Medium and high kashida change Word's line breaking (+9 %, +21 %).

## What had to be right (the renderer's checklist)
1. **Line pitch exact, not "multiple".** CSS `line-height: 1.7` is 1.7 × the size; Word's "multiple
   1.7" multiplies the face's own line height (Simplified Arabic's is tall): 151 pages instead of 85.
   Use `w:spacing w:line=<1.7 × size in twips> w:lineRule="exact"` (at-least gave the same result).
2. **Element order.** Word refuses (as "unreadable content", then drops the whole styles part) any
   child out of the ECMA sequence (`pPr`, `rPr`, `sectPr`, `style`, `settings`, `footnotePr`) and
   any invalid value (a bad `w:jc` did exactly that in one run). Insert children in schema order and
   validate every export against the schema in the tests.
3. **python-docx copies section properties** into each new section: a copied `pgNumType start=1`
   restarted every chapter at 1 and, with mirrored margins, Word added a blank page per chapter to keep
   1 on an odd page (+4 pages). Only the first body section restarts.
4. **Footnotes need their own part** (python-docx has no API): separators `-1` / `0`, one `w:footnote`
   per note, the reference style, and the separators listed in `settings.xml`.
5. python-docx's template misses `w:zoom/@w:percent` (schema error; Word tolerates it): set it.
6. `updateFields` in settings makes Word ask on open «This document contains fields that may refer to
   other files. Do you want to update the fields?» — do not set it; fill the contents field's result
   with the preview's page numbers instead (Word's pages match within one).

## Not checked by script (Word 16.113's AppleScript answers "doesn't understand" to save, close and
range queries on documents; page counts and statistics work)
- Word's own PDF of the file, per-page footnote numbering, which side the mirrored inner margin is on
  in an RTL book, the kashida look: open `out/book-19-lowKashida.docx` and `out/book-19-mediumKashida.docx`
  in Word and look.
- Comments on uncertain words (python-docx 1.2 has comments), embedded fonts (Amiri, OFL).

## Re-running
```
.venv/bin/python playground/word/spike.py 19            # out/book-19.docx (KASHIDA=lowKashida RULE=exact)
.venv/bin/python playground/word/spike.py 19 --word     # ... and Word's page / line counts
```
