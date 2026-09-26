# Export parity: the files against the book page (2026-09-26)

What the owner asked for: the exported files match what the book page (the WeasyPrint preview) shows —
the same pages, the same faces, the same footprint. PDF exactly, Word within about one page, EPUB the
same text and faces (no fixed pages). Measured by the integration stage on the owner's Mac (Microsoft
Word 16.113), against the `nk-print-5` preview rendered fresh in memory (no database writes). The dev
database's live layouts still date from `nk-print-4` (book 19 shows 84 pages there), so the preview here
is the reference render of `REFERENCE.md`, not the live layout.

## Book 19 «كتابي» (manuscript v36; 17×24 cm, Simplified Arabic 13 pt, notes per page, contents page)

| | Preview (`nk-print-5`) | Word (`nk-word-1`, low kashida, mode 15) | Print PDF | Screen PDF | EPUB |
|---|---|---|---|---|---|
| Pages | **85** | **85** (with comments: 85) | **85** | **85** | – (reflowable) |
| Text lines | 1,856 | 1,858 (+2, +0.11 %) | the preview's | the preview's | – |
| Note lines | 101 | 101 | the preview's | the preview's | – |
| Chapter first pages | 3, 4, 7, 12, 28, 59 | contents field 4, 7, 12, 14 (section), 28, 59 (the preview's numbers) | identical | identical | 6 chapter files |
| Every word's position | – | – | identical on every page (bleed 0; and 3 mm + marks, offset by 9 mm) | identical | – |
| Text | the book model | identical (body and the 40 notes) | identical | identical | identical per chapter; 40/40 notes |
| Faces | `nk-body`, `nk-heading-Bold` + 3 system fallbacks | «Simplified Arabic» in every style, named, not embedded (licence) | the preview's set | the preview's set | Amiri Regular + Bold embedded with `OFL.txt`; the book's face by `local()` |
| Size, time | 5.3 s render | 84 KB, 4.3 s (3.9 s of it the layout-only render: the live layout is stale); with 1,135 comments 131 KB | 318 KB, 5.8 s | 319 KB, 5.8 s | 559 KB, 0.1 s |

The §11.7 Word gate: pages 85 ≤ 85 + 2, text lines +0.11 % ≤ 0.5 %, note lines 0 % ≤ 1 % → **PASS**.
The exported Word file (`export_book`, and the `Export` rows #1 and #2 run through the `export` task) has
the same `document.xml`, `styles.xml`, `settings.xml`, `footnotes.xml` and footer as the file
`word_check` opened in Word; only the dates and the export id in the properties differ.

### Per chapter (Word: each chapter built alone, `word_check --chapters`)

| Chapter | Preview pages | Word pages | Δ |
|---|---|---|---|
| «قبل الفصل الأول» (`p1758`) | 1 (p. 3) | 1 | 0 |
| «مقدمة» (`h1786`) | 3 (4–6) | 3 | 0 |
| «المسعودي في سطور» (`h1806`) | 5 (7–11) | 5 | 0 |
| «الفصل الأول وصف بغداد» (`h1991`) | 16 (12–27) | 16 | 0 |
| «الفصل الثاني» (`h2344`) | 31 (28–58) | 31 | 0 |
| «الفصل الثالث» (`h3079`) | 27 (59–85) | 28 | +1 |

All within ±1 page (the gate). A chapter laid out alone differs in its line counts (`h1991`: 349 → 352
text lines, 16 → 12 note lines; `h3079`: 22 → 18 note lines), which the harness now prints as «differs
(not gated)»; the whole book's totals are the gated ones. Word's AppleScript cannot give a page per
bookmark, so the chapters' first pages inside the whole Word book are not measured.

### Faces

- **Preview and both PDFs:** `nk-body` (84 pages: text, notes and every Latin word), `nk-heading-Bold` (8
  pages), and three system faces for characters no book face has: Noto Serif p. 47 («ил»), Times New
  Roman p. 69 («※»), MS Mincho p. 82 («德拉»). The PDFs carry exactly the preview's set and report the
  three as `foreign_fonts` warnings.
- **Word:** the font table names only «Simplified Arabic», with `ascii/hAnsi/eastAsia/cs` all set in the 26
  styles; it is not embedded (D57), and the `font_not_embedded` note says so. The five rare characters
  are left to Word's own fallback (not looked at).
- **EPUB:** Amiri Regular and Bold embedded whole (D45/D57), `OFL.txt` alongside; the roles ask for the
  book's face where the reader has it installed, else Amiri (`font_fallback` note).

## The other books with a manuscript

| Book | Setup | Preview | Word | PDFs (print, 3 mm + marks, screen) | EPUB |
|---|---|---|---|---|---|
| 18 «تجربة ٦٦» | Times body and headings, Simplified Arabic Latin, running header, no front matter, 1 chapter | 4 p · 70 · 7 | 4 · 70 · 7 · PASS | 4 p, words, chapters and faces identical | text and 7/7 notes identical |
| 21 «سليمان الباروني» | Simplified Arabic, Amiri Latin (embedded in the .docx), copyright and contents pages, 3 chapters | 12 p · 195 · 13 | 12 · 198 (+3) · 13 | 12 p, identical | text and 4/4 notes identical |
| 13 «تجربة 6» | Amiri body and headings (embedded), Times Latin, title and contents pages | 7 p · 100 · 13 | 7 · 102 (+2) · 13 | 7 p, identical | text and 12/12 notes identical |

(pages · text lines · note lines). Books 21 and 13 miss the 0.5 % line gate only because they are small;
the pages are equal. Where the extra lines are (book 13):

| Book 13 built as | Preview text lines | Word text lines |
|---|---|---|
| the chapter alone (no front matter) | 97 | **97** (notes 13 → 13, 5 → 5 pages) |
| without the title page (contents kept) | 99 | 100 |
| without the contents page (title kept) | 98 | 99 |
| the whole book | 100 | 102 |

The body is exact; **Word counts one more line on each front-matter page** (title, copyright, contents):
+2 on books 19 and 13, +3 on book 21 (three front pages), 0 on book 18 (none). It has cost no page on any
book. Whether it is visible (the title or a contents line taking two lines in Word) is not known; the
owner should look at the title and contents pages.

## Checks run on every file

- Every `word/*.xml` part of every .docx (book 19 with and without comments, books 18, 21, 13) validates
  against `publishing/word/xsd/wml.xsd` (an independent loop, Word 2010+ `mc:Ignorable` namespaces
  dropped), and `publishing.word.schema.check` (schema + integrity) finds nothing. python-docx 1.2 reads
  each back: book 19 has 510 paragraphs, 8 sections, 26 styles, 40 footnotes, 0 or 1,135 comments,
  `compatibilityMode 15`, title «كتابي», language `ar`.
- Word opened every file and returned its statistics (the harness validates first and quits Word first).
  Nobody watched the screen, so the absence of a dialog is not confirmed by eye.
- Every EPUB reads back with ebooklib: `dc:language ar`, `page-progression-direction="rtl"`, one
  XHTML per chapter plus the title/copyright pages and `nav.xhtml`, 40 `noteref` calls and 40 footnote
  `aside`s on book 19. No epubcheck on this Mac.
- The print PDF: TrimBox 481.89 × 680.31 pt; with 3 mm and marks the BleedBox is the trim + 8.50 pt and
  the MediaBox the trim + 25.51 pt (3 mm bleed + 6 mm slug) on every side. Both PDFs: `/Lang (ar)`,
  `ViewerPreferences {Direction /R2L, DisplayDocTitle}`, creator «نسّاخ», title; the screen PDF opens on
  its outline (`/UseOutlines`, 7 entries: «المحتويات» and the chapters at level 1, the section at 2).

## What differs, and why

1. **Word's pages are Word's.** Equal on all four books; per chapter within ±1 (`h3079` +1 alone). The
   contents numbers are the preview's; «تحديث الحقل» corrects any that drift (the `toc_update` note).
2. **Word counts +1 line per front-matter page** (above). No page cost measured.
3. **Faces that are not Amiri are not embedded** in Word and EPUB (licences, D57): the reader needs
   Simplified Arabic / Times New Roman installed, or Word substitutes (and its pages may change) and the
   EPUB shows Amiri.
4. **Characters no book face has** fall back to system faces in the preview and the PDFs (flagged as
   `foreign_fonts`), and to Word's own fallback in Word.
5. **EPUB has no pages:** notes are numbered per chapter and sit at its end (the `epub_notes` note); scan
   page marks are not printed.
6. **Crop marks** are drawn in RGB black by WeasyPrint, outside the trim; the page content is pure K.
7. **The live layout is stale on the dev database** (`nk-print-4`, 84 pages for book 19): the export page's
   meta line reads 84 until the book page lays the book out again, the Word export takes the layout-only
   render (the `layout_first` note), and the PDFs' `layout_mismatch` check has no finished `nk-print-5`
   preview to compare with yet.

## C0 and C12 (measured by the Word stage, `word_check --compat 14` and `--c12`)

| Mode | Pages | Text lines | Note lines |
|---|---|---|---|
| 15 (`COMPAT_MODE`) | 85 | 1,858 (+2) | 101 |
| 14 | 85 | 1,863 (+7) | 101 |

Mode 15 breaks lines closer to the preview and shows no «وضع التوافق»: it stays.

C12: 60 pairs of a paragraph with 10 mm after followed by one with 10 mm before; a sum would take 11 pages,
the larger of the two 8. Word laid out 8: it takes the larger, as CSS collapsing does, so
`publishing/word/options.py` has `SPACING_ADDS = False` and direct `w:spacing` is written only where CSS
differs (the Subtitle's negative margin after the Title, the imprint's 30 mm, the copyright page's
padding). The spec's hypothesis (§5.4, "True") was wrong; D62 should record this.

## Not measured

The owner's calibration items (C1–C11, C13–C15: the binding side, headers on odd and even pages,
per-page note numbers by eye, kashida in each face, comments, the embedded Amiri with Amiri disabled,
the contents links, clipped tashkeel, blank versos, space before at page tops): `calibration.docx` is
built and validated (`manage.py word_check --calibration`). Medium and high kashida were not re-measured
(D55: medium 91 pages).

## Reproduce

`manage.py export_book <id> --format docx|print_pdf|screen_pdf|epub [--kashida low] [--comments]
[--bleed-mm 3 --crop-marks] --out DIR`; `manage.py word_check <id> [--comments] [--chapters]` (opens
Word). The integration's scripts and files: the session scratchpad's `phase6/` (`e2e.py`: rows through the
task; `parity.py <id>`: PDFs against a fresh preview; `verify_files.py`, `text_check.py`,
`front_probe.py`; the files in `out/`).
