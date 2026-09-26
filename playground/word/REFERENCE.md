# The reference: book 19 «كتابي» under `nk-print-5` (2026-09-26)

The preview after the Latin-face fix (D60, PHASE6_SPEC §4.1). The Word parity gate (§11.7) and the PDF
exports' layout check (§9) compare against these numbers.

**How it was measured.** In memory, with no database writes: `publishing.preview.job_for(book, "book", None)`
and `publishing.engine.get_engine().render(job)` on the dev database's book 19 (manuscript version 36), in
one process on the owner's Mac. Line counts come from `Rendered.layout` (every line of every page, by its
`kind`); the faces from PyMuPDF (`page.get_text("dict")` spans, and `get_page_fonts`). The "before" column
is the same render with the old `unicode-range` rule patched back in (`nk-print-4` behaviour).

## Stylesheet
17×24 cm, Simplified Arabic for the body, the headings **and the Latin face** (all three resolved, none
missing), 13 pt, line height 1.7, notes 10 pt numbered per page, page numbers bottom centre, no running
header, chapters open on any page, title page and contents on, no copyright page, bleed 0.

## Counts

| | `nk-print-5` (the reference) | `nk-print-4` (before the fix) | Word spike, low kashida |
|---|---|---|---|
| Pages | **85** | 84 | 85 |
| Text lines (every line but notes and scan marks) | **1,856** | 1,854 | 1,855 |
| … body paragraphs | 1,840 | 1,838 | |
| … headings (chapter and section titles) | 6 | 6 | |
| … verse | 2 | 2 | |
| … title page, contents title, contents entries | 1, 1, 6 | 1, 1, 6 | |
| Note lines | **101** | 101 | 101 |
| Layout passes | 4 | 4 | |
| Render time (layout, 4 passes and the PDF) | 6.1 s | 5.7 s | |
| Blank pages | none | none | |
| Page checks | `almost_empty_page` p. 58 («الفصل الثاني», 2 lines) | | |

The Latin words now keep Simplified Arabic's own Latin letters, which are wider than the "Serif Narrow" the
bug used: two more body lines, and chapter 5 («الفصل الثاني») grows from 30 to 31 pages, exactly as Word
laid it out in the spike.

## Chapters (printed pages; the title page is 1, the contents 2)

| Chapter | First | Last | Pages | Text lines | Note lines | Word spike pages |
|---|---|---|---|---|---|---|
| «قبل الفصل الأول» (`p1758`) | 3 | 3 | 1 | 9 | 4 | 1 |
| «مقدمة» (`h1786`) | 4 | 6 | 3 | 60 | 0 | 3 |
| «المسعودي في سطور» (`h1806`) | 7 | 11 | 5 | 91 | 25 | 5 |
| «الفصل الأول وصف بغداد» (`h1991`) | 12 | 27 | 16 | 349 | 16 | 16 |
| «الفصل الثاني» (`h2344`) | 28 | 58 | **31** (was 30) | 705 | 34 | 31 |
| «الفصل الثالث» (`h3079`) | 59 | 85 | 27 | 634 | 22 | 27 |

The front matter (pages 1–2) has the other 8 text lines.

## Faces in the PDF

| Font (subset) | Where | What |
|---|---|---|
| `nk-body` | 84 pages | the text, the notes and now every Latin word (133,227 characters) |
| `nk-heading-Bold` | 8 pages | the title, the contents title and the headings |
| `Noto-Serif` | p. 47 | 2 Cyrillic letters («ил») Simplified Arabic lacks |
| `Times-New-Roman` | p. 69 | 1 character («※») |
| `MS-Mincho` | p. 82 | 2 CJK characters («德拉») |

Before the fix, "Serif Narrow" set the Latin words on 17 pages; it is gone. The three system faces left
are characters no book face has (fontconfig's fallback). They are what 6b's `foreign_fonts` warning
reports; `nk-latin` is not embedded here because the Latin face is the body face's own file.
