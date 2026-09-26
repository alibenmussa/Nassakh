# Phase 6 specification: export (Word first, then PDF, EPUB later)

Draft for the owner's approval, 2026-09-26. Decisions D52–D62 below are proposed; nothing is built before the owner's
"go". Read first: `docs/PLAN.md` §7 (Phase 6) and the `Export` row of §4, `docs/PHASE5_SPEC.md` (D43 one book model,
D44 footprint, D45 trims and fonts, D46 per-page footnotes, §9 the D47 book page), `docs/DECISIONS.md` D40–D51,
`DESIGN.md`, the approved visual reference (the review screen: `templates/review/review.html`,
`static/src/components/review.css`, `static/src/js/review.js`) and the book page (`templates/editor/layout.html`,
`_book_side.html`, `static/src/js/book/*.js`). The evidence: `playground/word/REPORT.md` and `playground/word/spike.py`.

Phase 6 takes the book out of Nassakh. The files must look like the book page: the same text, the same faces, and the
same pages wherever the format lets Nassakh decide the pages. That is exact for PDF, within about one page for Word
(Word lays out its own pages), and not applicable to EPUB, which has no fixed pages. **Word (.docx) comes first and it is
the main feature**, so it must be a professional file: real Word styles, real footnotes numbered per page, a real
contents field, RTL sections, mirrored margins, document properties, the Amiri font embedded, no dialog when Word
opens it, and no «وضع التوافق» label. The file is checked against the ECMA-376 schema before anyone opens it.

## 0. Decisions (D52–D62)

- **D52: Phase 6 comes in three steps, Word first.**
  - 6a: the export history, the export page and the Word export.
  - 6b: PDF for print and for screen as real exports. The book page's PDF today is only the preview's file.
  - 6c: EPUB 3. It is designed here and built after the owner has tested 6a and 6b.
  - Reason: the owner's order. Word is what proofreaders and printers receive, and most of the PDF path already exists.
- **D53: Nassakh writes the Word file itself (amends D43's "python-docx").**
  - A small OOXML writer (lxml and zipfile) builds every part in schema order from the D43 book model.
  - python-docx 1.2 stays installed, but only as an independent reader in the tests.
  - Reason: python-docx's template brings about 164 styles (Word 2010 blue headings), Calibri theme fonts, a
    thumbnail, `compatibilityMode 14` (Word then shows «وضع التوافق») and a `w:zoom` without its required percent. It
    copies section properties into new sections (spike lesson 3) and has no footnotes API. With its own writer, Nassakh
    never inserts into existing XML (lesson 2), and the output is the same bytes every time, so golden tests work.
- **D54: the preview is the reference, and Word follows it wherever Word can say the same thing.**
  - Word follows it in:
    - an exact line pitch, never Word's "multiple";
    - CSS margin collapsing, reproduced;
    - the same face for each character (Lotus digits in the Latin face);
    - calls and markers printed «(n)» as the preview prints them;
    - page numbering as the preview does it (continuous from the title page, Q2);
    - the contents field filled in advance with the preview's page numbers (never `updateFields`);
    - footnotes restarting on every page (Word does this natively).
  - What Word cannot follow is listed with the file:
    - its pages can drift by about one page, so the contents numbers after the drift point need Word's «تحديث الحقل»;
    - a blank page before a recto chapter carries a page number;
    - scan-page marks are not printed;
    - bleed does not apply.
  - Custom styles get the Arabic names of the style picker (for example «شعر», «ملاحظة وسط»). Spelling squiggles are
    hidden, because OCR'd Arabic would be underlined everywhere and spell checking is not wanted (D47).
  - Reason: the owner's goal, which is that the files match the book page.
- **D55: kashida is on by default, as Word's *low* kashida.**
  - The choices are none, low, medium and high; the default is low.
  - Measured on «كتابي»:
    - low and none keep the preview's line count: 1,855 body lines against about 1,854, 101 note lines, 85 pages
      against 84;
    - medium changes Word's line breaking: 9 % more lines, 91 pages;
    - high adds about 21 % more lines.
  - Medium and high are therefore offered with the note that the book gets longer than the preview (Q4).
  - Calibration checks the look in each face. A face whose kashida looks wrong defaults to none.
- **D56: uncertain words can become Word comments. This is an option, off by default.**
  - Each uncertain word left in the text gets one comment by «نسّاخ». The comment lists the word's readings as the
    «غير المؤكَّدة» tab shows them (D50–D51 labels) and gives its scan page.
  - The copy with comments is for proofreading, not for the printer. Its file name ends «- مع التعليقات», and the
    option's help says that Word prints comments in the margin unless they are hidden.
  - Reason: owner decision 4. It is off by default because the default file is the book itself.
- **D57: only open-licence (OFL) faces are embedded, and that means Amiri.**
  - Simplified Arabic, Traditional Arabic, Times New Roman and Lotus are named in the file but not embedded, because of
    their Microsoft, Monotype and Linotype licences (D45). The export notes that the reader needs them installed.
  - A face missing on this Mac falls back to Amiri, as in the preview.
  - Amiri is embedded whole, not subset, so a proofreader can type in it. The UI has no toggle for this.
- **D58: every export is a row with its file (`publishing.Export`, the history).**
  - Each row records its format, options, the manuscript version and stylesheet values it used, its status, progress,
    warnings, file and timings.
  - The task reads the manuscript and the stylesheet once, when it starts, so an edit made during an export never
    gets into the file.
  - Exports run on their own Celery queue, `export`.
  - At most one export per book and format is queued or running. A second request gets 409 together with the running
    export.
  - Every click builds a new file; an old one is never silently handed back.
  - The newest 5 finished files per format are kept, and older rows go together with their files. There is no delete
    in v1.
  - The PLAN's `snapshot` field is dropped, because the recorded version is enough. Restoring the text of an export
    is left for later.
  - Reason: simple and reliable for one person, and files must be easy to find again.
- **D59: one export page «الإخراج» (`/books/<id>/export/`), not a modal.**
  - The page has three kinds of block: «قبل الإخراج», one block per format, and «السجل».
  - It is one click away from the book page (the «الإخراج» button in the top bar, and the «⋯» menu).
  - The readiness checks warn and link to the fix; they never block an export.
  - Reason: exports run in the background for seconds to minutes, their files must be found again later, and the
    fixes live on other screens. A page holds all three well; a dialog holds none of them well (§8.1).
- **D60: the Latin-face fix comes first, with `ENGINE_VERSION = "nk-print-5"`.**
  - The bug: when the Latin face is the same file as the body face (book 19 uses Simplified Arabic for both),
    WeasyPrint sets Latin words in the system's "Serif Narrow". The PDF and the book page then differ, which breaks
    D47 today and skews the spike's comparison.
  - The fix declares such a role without a `unicode-range`.
  - The preview also stops printing bleed and crop marks, which belong to the print export (D61).
  - Every book lays out once more, and the Word parity gate is measured after that.
  - Reason: the reference must be right before Word is measured against it.
- **D61: PDF for print and for screen become real exports (6b).**
  - Both are a fresh final render of the exported text through the book page's engine and passes, so they have the
    same pages.
  - Print PDF:
    - the TrimBox is the trim;
    - bleed and crop marks are options of the print export, so the stylesheet's `bleed_mm` no longer matters;
    - black is pure K, and the BleedBox is exact;
    - fonts are embedded as subsets.
  - Screen PDF:
    - a clean outline: chapters at level 1 and sections at level 2, with no title-page entry and no note calls in the
      labels;
    - right-to-left reading direction, and the outline pane open when the file opens.
  - Phase 6 makes no PDF/X or PDF/A claim: Nassakh has no CMYK profile and no local validator.
- **D62: the Word calibration results. They are recorded after the owner's calibration pass (§11.6).**
  - This covers what the spike could not check:
    - which side the mirrored inner margin lands on in an RTL book;
    - what left and right mean in bidi paragraphs;
    - compatibility mode 15 against 14;
    - the space before a paragraph at the top of a page;
    - per-page footnote numbers;
    - kashida in each face;
    - comments inside footnotes;
    - the embedded Amiri;
    - clipped glyphs at an exact line pitch;
    - the Arabic-Indic number format.
  - Each result is recorded with what the owner saw and fixed as a constant in `publishing/word/options.py`.

## 1. Scope

| Step | What is in it | When |
|---|---|---|
| 6a | The Latin fix and `nk-print-5` (D60); the `Export` model, queue, API and history; readiness; the export page with the Word section; the Word renderer; the dev-only Word check and the calibration file | after "go" |
| 6b | Print PDF and screen PDF exporters (D61); the page's two PDF sections; «PDF المعاينة» leaves the book page menu | after the owner tests 6a |
| 6c | EPUB 3, right to left (§10) | after the owner tests 6b |

**Deferred, not in Phase 6:**
- PDF/X and PDF/A conformance, and tagged PDF;
- a cover image, and EPUB's page-list;
- ornaments (the separator «* * *» exists);
- tables (PLAN "Later");
- restoring the text of an old export, and deleting exports;
- a Word-made PDF (Word's AppleScript cannot save);
- the book page's export pill and completion toast, exports in the preview and dashboard polls, and the dashboard's
  "last export" line;
- roles beyond editor and admin (SaaS).

## 2. Evidence

### 2.1 The Word spike (`playground/word/`, 2026-09-26)

`spike.py` built «كتابي» (book 19: 6 chapters, 40 footnotes, Simplified Arabic 13 pt, 17×24 cm, line height 1.7) with
python-docx plus direct XML. Microsoft Word 16.113 on the owner's Mac opened it through AppleScript and reported its
counts. Every file was first validated against the ECMA-376 WordprocessingML XSD.

| Word justification | Pages | Body lines | Note lines |
|---|---|---|---|
| **Preview (WeasyPrint)** | **84** | **≈ 1,854** | **101** |
| justify (`both`) | 85 | 1,855 | 101 |
| **low kashida (`lowKashida`)** | **85** | **1,855** | **101** |
| medium kashida | 91 (+7 %) | 2,020 (+9 %) | 112 |
| high kashida (measured before fix 3) | 104 | 2,246 (+21 %) | 121 |

Per chapter, with low kashida and each chapter alone, the pages (Word / preview) were 1/1, 3/3, 5/5, 16/16, **31/30**
and 27/27.

**What had to be right:**
1. The line pitch is exact: `w:spacing w:line = 1.7 × size` in twips with `lineRule="exact"`. Word's "multiple 1.7"
   gave 151 pages.
2. Every child element is in strict schema order (`pPr`, `rPr`, `sectPr`, `style`, `settings`, `footnotePr`). Out of
   order, Word reports "unreadable content" and drops the styles part.
3. Section properties must never be copied into new sections. A copied `pgNumType start=1` with mirrored margins
   added a blank page for every chapter.
4. Footnotes need their own part, with separators `-1` and `0` listed in the settings.
5. `w:zoom/@w:percent` is required.
6. Never set `updateFields`: Word would ask «update fields that may refer to other files?» when the file opens.

### 2.2 What the spike did not measure (the final build must measure again)
- Body calls were bare superscript numbers, where the design uses «(n)» at 0.62 of the body size.
- Page numbers restarted at 1 on the first chapter; there was no running header; the header and footer distances were
  a fixed 10 mm.
- The contents field held a placeholder. The mode was `compatibilityMode 14`.
- "Identical line breaks" is inferred from equal counts. Word's AppleScript refuses range queries, so no
  line-by-line check was possible.
- The high-kashida page count predates fix 3.
- **Chapter 5 is already one page longer in Word at low kashida**, so every contents number from chapter 6 on is one
  page off in «كتابي». Hence the note to update the field is always shown (§7).

### 2.3 The preview itself (read-only probes of book 19, 2026-09-26)
- **Latin face bug (D60).**
  - With `latin_font == body_font`, Latin words («https www hindawi org») are set in "Serif Narrow" on pages 3, 10, 13,
    24 and more.
  - With `latin_font = times` they go to `nk-latin` as intended.
  - The PDF also embeds Noto Serif (2 Cyrillic letters, p. 47) and MS Mincho (2 CJK characters, p. 81).
- **A fresh book render:** 5.4 s for 84 pages in 4 passes. A layout without passes takes 1.3 s, and writing the PDF
  0.4 s. The estimate for 800 pages is 50–70 s.
- **The outline is wrong:**
  - the title page's h1 is a top-level bookmark and «المحتويات» nests under it;
  - a heading's footnote call leaks into its label («الفصل الأول(1)»).
- **Bleed and crop marks.** The preview already adds them when `bleed_mm > 0`. The format panel does not show
  `bleed_mm`, but the API accepts it.

### 2.4 Not verified, and how Word behaves on this Mac
- **Not checked by eye:**
  - per-page footnote numbers;
  - the inner-margin side in RTL with `mirrorMargins`;
  - the kashida look;
  - comments;
  - embedded fonts.

  These go to the calibration file (§11.6).
- **AppleScript limits.** Word's AppleScript answers "doesn't understand" to save, close and range queries. So a
  Word-made PDF cannot be scripted, while page and line statistics can.
- **Dialogs and duplicate windows.** Driving Word shows dialogs on the owner's screen. During the spike the owner had
  to answer a Word prompt, and the file ended up open twice. The dev check must therefore:
  - validate every file first;
  - tell the owner before it runs;
  - never open a file Word already has open.

## 3. The shared contract (fixed before any agent starts)

Every stream builds against these names and shapes. Stage 0 copies them into JSON fixtures, and the tests check the
real payloads against the fixtures.

### 3.1 Names

| Item | Values |
|---|---|
| Formats | `docx` «Word» (.docx), `print_pdf` «PDF للطباعة», `screen_pdf` «PDF للشاشة», `epub` «EPUB». Available: 6a `docx`; 6b adds both PDFs; 6c `epub` |
| Status | `queued` «في الانتظار», `running` «قيد الإخراج», `done` «اكتمل», `error` «خطأ», `cancelled` «أُلغي» (the `PreviewRender` labels) |
| docx options | `kashida` ∈ `none` \| `low` \| `medium` \| `high` (default `low`); `comments` bool (default `false`) |
| print_pdf options (6b) | `bleed_mm` ∈ {0, 3, 5} (default 0 until Q7); `crop_marks` bool (default `false`) |
| screen_pdf, epub | no options |
| Progress steps | `queued` «في الانتظار», `prepare` «تحضير النص», `layout` «ترتيب الصفحات», `footnotes` «ترقيم الحواشي», `relax` «ضبط الحواشي مع أسطرها», `write` «كتابة الملف», `check` «فحص الملف», `done` «اكتمل». Word: prepare → (layout) → write → check → done. PDF: prepare → layout → footnotes → (relax) → write → check → done |
| Renderer versions | docx `nk-word-1`; PDF `nk-print-5/weasyprint-70.0` (`engine.version`) |
| File names | `{title}.docx`, `{title} - مع التعليقات.docx`, `{title} - للطباعة.pdf`, `{title}.pdf` (screen), `{title}.epub` |
| Words | «إخراج» (never «تصدير»), «تعليقات» for Word comments («ملاحظات» already means page checks) |

The kashida labels live in the backend (`publishing/word/options.py`) and are sent in the payload. Templates render the
choices and never hardcode them.

| Key | Short label | Full label | Hint |
|---|---|---|---|
| `none` | «بلا» | «بلا كشيدة» | «تُضبط الأسطر بالمسافات وحدها، كما في المعاينة.» |
| `low` | «خفيفة» | «كشيدة خفيفة» | «الأسطر كما في المعاينة تقريبًا.» |
| `medium` | «متوسطة» | «كشيدة متوسطة» | «يغيّر Word فواصل الأسطر فيطول الكتاب عن المعاينة.» |
| `high` | «قوية» | «كشيدة قوية» | «يغيّر Word فواصل الأسطر فيطول الكتاب كثيرًا عن المعاينة.» |

The hints do not quote one book's percentages. The measured numbers are in D55.

### 3.2 JSON shapes

`GET api:exports` (the page's payload; the page view embeds the same object as `config`):

```json
{"book": {"id": 19, "title": "كتابي", "has_manuscript": true,
          "layout": {"state": "current", "trim_label": "17×24 سم", "page_count": 84, "chapters": 6,
                     "body_font": "Simplified Arabic", "body_size_pt": 13}},
 "can_edit": true,
 "readiness": [{"code": "uncertain_words", "level": "warn", "message": "بقيت 12 كلمة غير مؤكَّدة في النص، منها 3 أرقام.",
                "action": {"label": "غير المؤكَّدة", "url": "/books/19/layout/?tab=uncertain"}}],
 "formats": [{"key": "docx", "label": "Word", "extension": ".docx", "available": true,
              "form": {"kashida": {"value": "low", "choices": [{"value": "none", "label": "بلا", "title": "بلا كشيدة", "hint": "…"}]},
                       "comments": {"value": false, "available": 12, "label": "تعليقات على الكلمات غير المؤكَّدة", "hint": "…"}},
              "notes": [{"code": "font_not_embedded", "level": "info", "message": "…"}],
              "active": null, "latest": {"…": "a row"}}],
 "items": [{"…": "rows, newest first, at most 30"}],
 "urls": {"create": "/api/books/19/exports/", "row": "/api/books/19/exports/__eid__/",
          "cancel": "/api/books/19/exports/__eid__/cancel/"}}
```

`layout.state` is one of `current`, `stale`, `rendering` or `none`. The `form` value is the last options used for this
book and format, or the defaults.

A row (`api:export`, and the items of the list):

```json
{"id": 12, "format": "docx", "format_label": "Word", "status": "running", "status_label": "قيد الإخراج",
 "options": {"kashida": "low", "comments": true}, "options_text": "كشيدة خفيفة · تعليقات",
 "manuscript_version": 57, "stale": false, "stale_text": "",
 "progress": {"step": "write", "label": "كتابة الملف", "done": null, "total": null, "percent": null},
 "waiting_hint": "", "page_count": null, "pages_hint": 84, "size_bytes": 0, "size_text": "",
 "duration_ms": 0, "warnings": [], "error": "", "error_detail": "",
 "filename": "كتابي - مع التعليقات.docx", "download_url": null,
 "created_by": "علي", "created_at": "2026-09-26T10:02:11+02:00", "updated_at": "…", "finished_at": null}
```

- `warnings` are `[{code, level: "info" | "warn", message}]`.
- `error` is the Arabic headline; `error_detail` (the technical line) is sent to editors only.
- `size_text` reads «318 KB» or «1.2 MB».
- `stale_text` is one of «تغيّر النص بعد هذا الإخراج», «تغيّر التنسيق بعد هذا الإخراج» or «تحدّث نسّاخ بعد هذا الإخراج».
- `page_count` is exact for PDF and null for Word, which lays out its own pages; `pages_hint` gives the preview's
  count instead.

`POST api:exports` `{format, options}` returns:
- **202** with the new row;
- **409** `{detail: «هذا الملف يُخرَج الآن؛ انتظر انتهاءه أو ألغِه.», active: row}`;
- **400** `{detail: «خيارات الإخراج غير صالحة.», errors: {key: message}}`;
- **404** `{detail: «لا توجد مخطوطة بعد.»}`;
- **403** for read-only roles.

## 4. Changes to the book model and the preview

1. **The Latin fix (D60, Stage 1).**
   - In `publishing/fonts.py` `font_face_css` and `browser_faces`, a role whose regular file equals the Latin face's
     file is declared without `unicode-range`, so the face sets its own Latin letters. The PDF and the live page then
     agree.
   - `css.py` drops the preview's `bleed`/`marks`.
   - `ENGINE_VERSION = "nk-print-5"`.
   - Regression tests: `latin_font == body_font` puts Latin letters in `nk-body` or `nk-latin`, never a system face;
     the preview CSS has no bleed even with `bleed_mm = 3`.
2. **Editorial marks for Word comments.**
   - `book_model(document, stylesheet, …, editorial: bool = False)`. With `editorial=True`, `_marks()` also keeps
     `uncertain`. `_trim` and `_tidy_breaks` already carry `marks`, and `html.py` ignores marks it does not know.
   - The default is byte-identical to today: the preview, the re-layout and the layout export never pass the flag.
   - No change to `editor/uncertain.py` or `editor/document.py` is needed. The Word writer rebuilds the words from the
     runs (§5.11).
3. **`Style.word` becomes the Word style id** that the writer uses:

   | key | id | key | id |
   |---|---|---|---|
   | `book-title` | `Title` | `body` | `Normal` |
   | `book-author` | `NkAuthor` | `quote` | `Quote` |
   | `contents-title` | `TOCHeading` | `verse` | `NkVerse` |
   | `contents-1` / `contents-2` | `TOC1` / `TOC2` | `center` | `NkCenter` |
   | `chapter-title` / `section-title` | `Heading1` / `Heading2` | `separator` | `NkSeparator` |
   | `footnote-text` | `FootnoteText` | | |

   The two assertions in `publishing/tests.py` that check the Word names change with it, and a new test checks that
   every `STYLES` id exists in the written `styles.xml`.
4. **Digits (only if Q3 is yes).**
   - `PageSetup` and `RenderJob` gain `digit_style` from `books.Book.digit_style`.
   - The preview's page counters and `data-n` numbers use Arabic-Indic digits in such books, and Word uses its
     Arabic-Indic number format (C8).
   - This is one more `ENGINE_VERSION` bump. Text digits are already in the book's style (assembly, «تحويل الأرقام»).

Nothing else changes in `html.py`, `css.py` or `pdf.py` in 6a.

## 5. The Word builder (`publishing/word/`)

### 5.1 Where it sits

It is a second renderer of the D43 book model, not an `Engine`: Word lays out its own pages, so
`Engine.render() -> Rendered` does not fit. It plugs into the export pipeline as the `docx` exporter:

```
exports.run_export(id) ─► DocxExporter.export(job: ExportJob, progress)
   prepare  model = book_model(job.document, job.setup, title, author, editorial=options.comments)
   layout   plan  = page_plan(job)           # the preview's page numbers (§5.9); a pdf=False render only when needed
            words = uncertain words by (block, note)   # only with comments: editor.uncertain.words_of +
                                                       # attach_readings(normalizer(book)), one query
   write    result = build_docx(model, fonts.resolve(...), options, plan=plan, readings=words, meta=…)   # pure
   check    schema.validate_package(data) + schema.integrity_errors(data) → an invalid file fails the export
   ◄ ExportResult(data, page_count=None, warnings, stats, log)
```

- `build_docx` touches no database, so the tests are fast and can compare against golden files.
- `WORD_VERSION = "nk-word-1"` is recorded on every Word export.

### 5.2 Modules

| Module | Holds |
|---|---|
| `__init__.py` | `WORD_VERSION`, `DocxExporter` (the registry imports `publishing.word.DocxExporter`) |
| `options.py` | `WordOptions` (`parse`, `as_dict`), the `KASHIDA` table (§3.1), the note and warning codes with their Arabic text (§7), and the calibration **conventions** (§5.4) |
| `ooxml.py` | the element builder `w()`, units, `xml_safe`, field builders; the package: parts, rels, content types, deterministic zip; `settings.xml`, `fontTable.xml`, docProps |
| `faces.py` | `FacePlan` (the family for each role, the face for each character, font-table entries), `EMBEDDABLE = {"amiri"}`, `obfuscate()` |
| `styles.py` | the style table, the margin-collapse rule, widows, `styles_xml()` |
| `runs.py` | `RunWriter` (direction, faces, marks, breaks, calls, comment ranges), `FootnotesPart`, `CommentsPart` |
| `sections.py` | page setup, the section plan, headers and footers, the title, copyright and contents pages, bookmarks |
| `writer.py` | `PagePlan`, `BookWriter`, `build_docx()` |
| `schema.py` | `validate_package()`, `integrity_errors()` |
| `exporter.py` | `DocxExporter` (form block, notes, export) |
| `xsd/` | the vendored ECMA-376 schemas (python-docx's `ref/xsd` plus a small `xml.xsd`), the spike's validator, `SOURCE.txt` |
| `golden/` | canonical XML for the golden tests |

It also brings `publishing/management/commands/word_check.py` (the dev-only Word harness and the calibration file,
§11.5) and `publishing/test_word.py`.

```python
@dataclass(frozen=True)
class WordOptions:
    kashida: str = "low"
    comments: bool = False
    @classmethod
    def parse(cls, data: dict | None) -> "WordOptions": ...   # ValueError with an Arabic message

def build_docx(book: Book, fonts: ResolvedFonts, options: WordOptions, *, plan: PagePlan | None = None,
               readings: Mapping[tuple[str, str | None], list[uncertain.Word]] | None = None,
               meta: DocMeta, embed_fonts: bool = True, front_matter: bool = True) -> WordResult:
    """The book as a .docx (pure). `embed_fonts=False` keeps the golden files small; `front_matter=False` is the
    harness's one-chapter check. Same inputs and `meta.now` give the same bytes."""
```

### 5.3 Package and settings

- **Parts:**
  - `[Content_Types].xml` and `_rels/.rels`;
  - `docProps/core.xml`, `app.xml` and `custom.xml`;
  - `word/document.xml` with its rels, `styles.xml`, `settings.xml`, `fontTable.xml` (plus its rels and
    `fonts/fontN.odttf` when a font is embedded);
  - `footnotes.xml`, always written for the separators;
  - `comments.xml`, only when there are comments;
  - `headerN.xml` and `footerN.xml`.

  There is no theme, `webSettings`, `numbering`, `customXml` or thumbnail. Word adds a theme itself when it saves.
- **Element order.** Every element's children are written in the order its XSD type requires, and nothing is
  inserted later. The one exception is a section's `w:sectPr`, which goes last in the pPr of its last paragraph.
- **Relationship ids** are `rId1…` in a fixed order. Zip entries are dated 1980-01-01 and written in a fixed order,
  with `[Content_Types].xml` first.
- **`settings.xml`** (schema order):

  ```xml
  <w:view w:val="print"/> <w:zoom w:percent="100"/> <w:embedTrueTypeFonts/>(when embedding) <w:mirrorMargins/>
  <w:hideSpellingErrors/> <w:hideGrammaticalErrors/> <w:defaultTabStop w:val="720"/>
  <w:evenAndOddHeaders/>(outer page numbers only) <w:characterSpacingControl w:val="doNotCompress"/>
  <w:footnotePr><w:numFmt w:val="decimal"/><w:numRestart w:val="eachPage"/><w:footnote w:id="-1"/><w:footnote w:id="0"/></w:footnotePr>
  <w:compat><w:doNotExpandShiftReturn/> compatSetting compatibilityMode=15 (C0), overrideTableStyleFontSizeAndJustification=1,
    enableOpenTypeFeatures=1, doNotFlipMirrorIndents=1</w:compat>
  <w:themeFontLang w:val="en-US" w:bidi="ar-SA"/> <w:decimalSymbol w:val="."/> <w:listSeparator w:val=","/>
  ```

  Never `updateFields`, `rsids` or `docId`. With compatibility mode 15 Word shows no «وضع التوافق»; the harness
  measures it against mode 14 (C0) and falls back to 14 only if mode 15 breaks lines differently from the preview.

### 5.4 Conventions settled by calibration (constants in `options.py`, each citing its C-item)

| Constant | Hypothesis | Settled by |
|---|---|---|
| `COMPAT_MODE` | 15 | C0 (harness, automatic) |
| `MIRROR_PGMAR_LEFT` | `"inner"`: pgMar `left` = inner, so odd pages bind on the right (an RTL book opens with a left-hand recto, like `css.py`'s `@page :left`) | C1 and the Word-made file r1 |
| `BIDI_LEFT_IS_START` | True: in a bidi paragraph, left and right in `jc`, `ind` and tabs mean start and end | C10, r1 |
| `SPACING_ADDS` | True: Word adds space after to the next paragraph's space before | C12 (automatic) |
| `CALL_RAISE` | 0.31 × body. WeasyPrint's `super` is 0.5 × the call's own size, and the call is 0.62 of the body, so `w:position` = 8 half-points at 13 pt | harness line counts |
| `COMMENTS_IN_NOTES` | True: comments anchored inside footnote text | C6 |
| `ARABIC_INDIC_FORMAT` | `hindiNumbers` | C8 (only with Q3) |

The writer avoids constructs whose meaning is uncertain:
- start-aligned paragraphs carry no `jc` (a bidi paragraph's default is the start);
- quote indents are symmetric;
- page-number paragraphs are LTR (`<w:bidi w:val="0"/>`), so their left and right are physical.

Only the TOC2 indent, the TOC's end tab and the page margins depend on the constants.

### 5.5 Styles

- **`docDefaults`:**
  - `rFonts` ascii/hAnsi/eastAsia take the Latin face and `cs` the body face (no theme fonts, which would beat
    explicit ones);
  - `sz` and `szCs` take the body size;
  - `lang` is `en-US` with `bidi="ar-SA"`;
  - the pPr default has `bidi` and `after=0`, so every story is RTL.
- **Sizes** are the unrounded CSS values written as whole half-points. h1 is 20.8 pt in CSS and 21 pt in Word; this
  is accepted and noted in D62.
- **Line pitch.** Always `lineRule="exact"` with `line = twips(multiple × the style's unrounded size)`.

| Role | styleId (name) | Size | Face | Pitch | Before/after mm | Align | Other |
|---|---|---|---|---|---|---|---|
| body | `Normal` («Normal», built in) | body | body | `line_height` | 0/0 | kashida `jc` | firstLine `indent_em`; widowControl |
| chapter title | `Heading1` («heading 1») | h1 × body | heading | 1.35 | 16/9 | center | b+bCs, outlineLvl 0, keepNext if `keep_headings` |
| section title | `Heading2` («heading 2») | h2 × body | heading | 1.4 | 5/3 | center | b+bCs, outlineLvl 1, keepNext |
| quote | `Quote` | body | body | `line_height` | 2/2 | kashida | start = end = 2 × body |
| verse | `NkVerse` «شعر» | body | body | `line_height` | 2/2 | center | |
| center | `NkCenter` «ملاحظة وسط» | body | body | `line_height` | 2/2 | center | |
| separator | `NkSeparator` «فاصل» | body | body | `line_height` | 4/4 | center | text `* * *` |
| footnote | `FootnoteText` («footnote text») | `footnote_size_pt` | body | 1.5 | 0/0 | kashida | |
| book title | `Title` | h1 × 1.4 × body | heading | 1.4 | 0/10 | center | b+bCs |
| subtitle / author | `Subtitle` / `NkAuthor` «المؤلف» | 1.15 / 1.3 × body | body | `line_height` | −6/10 (§ collapse) / 0/0 | center | |
| credits, imprint, rights | `NkCredit` «سطر التحقيق والترجمة», `NkImprint` «سطر النشر», `NkCopyright` «صفحة الحقوق» | 1.05 × body, body, footnote | body | `line_height`, `line_height`, 1.6 | 3/0, 0/0, 0/1 | center | |
| contents | `TOCHeading`, `TOC1`, `TOC2` («toc 1», «toc 2») | h1, body, 0.95 × body | heading, body | 1.4, `line_height` | 6/10, 0/1.8 | center, start | the end-side tab with dot leaders at the text width; TOC2 start indent 1.5 em |
| header, footer | `Header`, `Footer` | footnote − 0.5 (min 6), footnote | body, number | 1.5 | 0/0 | center / LTR | |
| comment text | `CommentText` («annotation text») | 10 | body | single | 0/0 | start | |
| call (char) | `FootnoteReference` | 0.62 × body | number | – | – | – | `w:position` = `CALL_RAISE`, no `vertAlign` |
| note number (char) | `NkNoteNumber` «رقم الحاشية» | footnote | number | – | – | – | on the baseline, like the preview's marker |
| Latin in RTL (char) | `NkLatin` «حروف لاتينية» | – | Latin | – | – | – | cs = ascii = hAnsi = the Latin face |
| page number, comment ref (char) | `PageNumber`, `CommentReference` | footnote, 8 | number, body | – | – | – | |

- All paragraph styles are based on `Normal` and reset the first-line indent, `jc` and spacing.
- Built-in styles use Word's canonical names, which Word localizes itself (for example «عنوان 1»). Custom styles
  carry the Arabic names.
- Bold and italic are always written both ways (`b`+`bCs`, `i`+`iCs`), because Word uses the complex-script flags for
  RTL runs.
- The "number" face is the body face, or the Latin face when the body face has no digits (Lotus).
- **Margin collapsing.** CSS collapses adjacent vertical margins, while Word adds them together (`SPACING_ADDS`).
  - `collapse(bottom, top)` gives the CSS gap: the larger of two positives, the smaller of two negatives, the sum when
    the signs are mixed.
  - `split_gap()` writes a direct `w:spacing before/after` only where the collapsed gap differs from what Word would
    add up.
  - This applies to consecutive paragraphs of one flow: the same section, and no `pageBreakBefore` on the second.
  - Examples: a quote after a quote gives 2 mm, a heading 1 followed by a heading 2 gives 9 mm, and a title followed by
    a subtitle gives 4 mm.
- **Widows and orphans.** Word's `widowControl` is on/off and means two lines:
  - widows = orphans = 2 → on;
  - both 1 → off;
  - any other combination → on, with the note `widows_approx`.
- **Block attributes.** `Block.keep_with_next` becomes a direct `keepNext`, and `Block.break_before` a direct
  `pageBreakBefore`. The page break is skipped when the block opens a page anyway, as in `html.py`.

### 5.6 Runs

- **Direction.** Each `Run.text` goes through `model.direction_runs(text, "rtl")`:
  - RTL pieces get `w:rtl`, so Word uses the cs font, `szCs`, `bCs` and `iCs`;
  - LTR pieces (Latin words, and the neutrals and digits between Latin letters) get no `w:rtl`;
  - other neutrals take the paragraph's direction.
- **Face per character, as the preview does it.** `FacePlan.split()` cuts an RTL piece into what the role's Arabic
  face covers and what it lacks, using the same coverage ranges as the preview's `unicode-range`.
  - What the face lacks gets `rStyle NkLatin`. With Lotus that means digits, the en dash, curly quotes, the ellipsis
    and alef wasla.
  - Spaces inside Latin phrases come from the Latin face in Word but from the Arabic face in the preview. This is
    negligible and accepted.
- **Marks and breaks.** `bold` becomes `b`+`bCs` and `italic` becomes `i`+`iCs`. A `LineBreak` becomes `<w:br/>`, and
  `doNotExpandShiftReturn` keeps the line before it unstretched, as CSS does. A `SourceMark` writes nothing (note
  `source_pages`).
- **Calls.** A `NoteRef` becomes three runs styled `FootnoteReference` + `w:rtl`: `(`, then
  `<w:footnoteReference w:id="n"/>`, then `)`. The notes are taken from `block.footnotes` through a queue per id, so a
  repeated id cannot swap two notes.
- **XML safety.** Tabs and newlines in text become `w:tab` and `w:br`. Characters that XML 1.0 forbids are dropped.

### 5.7 Page setup, sections, headers and footers

- **Page and margins:**
  - `pgSz` comes from the trim (17×24 cm → 9638 × 13606 twips);
  - `pgMar` top and bottom come from the stylesheet, and left and right from `MIRROR_PGMAR_LEFT`;
  - `gutter` is 0;
  - the header distance is `max(4, top − 3 − L)` mm and the footer distance `max(4, bottom − 4 − L)` mm, with L the
    exact line height of the header or footer paragraph. This mirrors the preview's 3 mm and 4 mm paddings and never
    pushes the text block.
  - Every `sectPr` carries `w:bidi` and its own `footnotePr`, because `numFmt` and `numRestart` are set per section.
- **Front sections** (in book scope):
  1. the title page and the copyright page (after a `pageBreakBefore`) share one section;
  2. the contents page has its own section: `oddPage` with recto openings, else `nextPage`.

  Front sections declare no headers or footers, like `@page front`.
- **Body sections:**
  - one section per chapter of kind `chapter` or `front`: `oddPage` with recto openings, else `nextPage`;
  - chapters of kind `section` (a book without headings) run on, except for a `continuous` section when footnotes are
    numbered per chapter;
  - empty chapters are skipped;
  - the first section has no `w:type`.
- **Section properties.** Every `sectPr` is built fresh; nothing is copied (spike lesson 3). A section's `sectPr` is
  the last child of its last paragraph's pPr, and the last section's is the last child of `w:body`.
- **Page numbering** is continuous from the title page, as the preview's `counter(page)` is: front pages count but
  print nothing, and the contents numbers agree. `pgNumType@start` is written only if Q2 chooses a restart, and then
  only on the first body section.
- **Headers and footers.**
  - The default part covers odd pages, the even part even pages, and the first part the chapter opener (`titlePg`).
  - An RTL book's odd pages are left-hand pages, so the outer edge of an odd page is its left.

  | `page_number` | Parts |
  |---|---|
  | `none` | none |
  | `bottom_center` | default footer: PAGE centered |
  | `bottom_outer` | `evenAndOddHeaders`; default footer PAGE `jc=left`, even footer PAGE `jc=right` |
  | `top_outer` | `evenAndOddHeaders`; the header holds PAGE on the outer side and the header text at a center tab (LTR paragraph, tabs at W/2 and W) |

  - **Running header.** `running_header` `book` or `chapter` puts the book title, or `chapter.heading` (the book title
    for `front` and `section` chapters), centered in the header. Each chapter section declares its own parts.
  - **Chapter openers.** `titlePg` is set on every chapter section with a header, and the first header is empty, as
    the preview's `first-except` makes it. With `top_outer`, the first header keeps only the number.
  - **Declaring parts.** The first body section declares every reference it uses. Later sections declare only the
    references that differ, because Word inherits the rest.
  - **The PAGE field** is written with the cached result `1`.
- **Known difference.** With recto openings, the blank page Word inserts for an `oddPage` break belongs to the previous
  section and shows its page number (note `blank_versos`). A conditional-footer fix is possible later (R-list).

### 5.8 Footnotes

- **The part.** `word/footnotes.xml` holds separators `-1` (separator) and `0` (continuation), both listed in the
  settings. Then one `w:footnote` per note, with ids 1, 2… in call order.
- **The separator matches the preview's rule** (0.4 pt, 4 mm above, 1.6 mm below, the full text width). It is an empty
  paragraph with `pBdr/top sz=3`, `spacing before=4mm line=1.6mm exact` and a 1 pt run. A `w:separator` character
  would draw Word's short rule instead.
- **Numbering.** `footnote_numbering` `page` (the default, D46) becomes `numRestart eachPage`; `chapter` becomes
  `eachSect` (one section per chapter, §5.7); `book` becomes `continuous`. The format is `decimal`, or the Arabic-Indic
  format with Q3.
- **A note's text.** `FootnoteText` with a marker «(n) » at the note size, on the baseline (`NkNoteNumber` runs around
  `w:footnoteRef`, then a no-break space), then the note's runs.
- **Left to Word.** Word keeps a note on its call's page natively, so there is no relax pass.

### 5.9 Front matter, contents and the page plan

- **Title page** (`front.title_page`):
  - `Title` with a direct `before` of 0.28 × the text width (`padding-top: 28%`);
  - `Subtitle`, `NkAuthor`, and the `NkCredit` lines («تحقيق: …», «ترجمة: »);
  - `NkImprint` «الناشر، المدينة، السنة» with `before` 30 mm.
- **Copyright page** (`front.copyright_page`): a `pageBreakBefore`, a `before` of 0.70 × the width, then the lines as
  `html.copyright_page` writes them (title, subtitle, author, credits, «الطبعة …», imprint, «ردمك: …», rights).
- **Contents** (`front.contents` and a book that has contents): `TOCHeading` «المحتويات», then a **real TOC field**
  whose result is written in advance.
  - The field is ` TOC \o "1-2" \h \z \u ` with one `TOC1`/`TOC2` paragraph per entry. Each entry is a hyperlink to
    the bookmark `_Toc_<id>`, then the end tab, then ` PAGEREF _Toc_<id> \h ` with the cached page number.
  - There is no `updateFields` and no `w:dirty`, so Word opens the file without asking anything. Word's own «تحديث
    الحقل» rebuilds the same entries from the outline levels.
  - The note `toc_update` is always shown (§2.2).
- **Bookmarks.**
  - Each heading with text gets `_Toc_<id>`: the block id with characters outside `[A-Za-z0-9_]` replaced by `_`, at
    most 40 characters, made unique.
  - A chapter without a heading gets `_nk_ch_<id>` on its first paragraph.
- **The page plan:**

  ```python
  @dataclass(frozen=True)
  class PagePlan:
      source: str                            # "live" | "render"
      page_count: int
      headings: dict[str, int]               # heading block id → printed page of its first line
      chapters: dict[str, tuple[int, int]]   # chapter id → (first, last)
  ```

  - `page_plan(job)` uses the live layout when it is current for the exported text:
    - every chapter's `version` in `LiveLayout.chapters` equals `editor.document.chapter_version` of that chapter in
      the exported document;
    - the chapter set is the same;
    - `live.setup_hash == preview.setup_hash(setup)`.

    `LiveLayout.manuscript_version` alone is not enough, because it is the maximum over the re-layouts.
  - Otherwise, one layout-only render (`pdf=False`) of the exported document runs, with the step `layout`. That takes
    about 1.3 s for 84 pages, and roughly a minute for 800 pages right after the `nk-print-5` bump.
  - With no plan, the entries are written without numbers (warning `toc_numbers_missing`).

### 5.10 Kashida

The body, quote and footnote styles take the chosen `jc`:

| Option | `w:jc` |
|---|---|
| `none` | `both` |
| `low` | `lowKashida` |
| `medium` | `mediumKashida` |
| `high` | `highKashida` |

Centered and start-aligned styles are unaffected. Readers other than Word (Pages, LibreOffice) may treat the kashida
values as plain justify, which is acceptable.

### 5.11 Comments on uncertain words

- **Finding the words.** With `editorial=True`, uncertain words keep `uncertain` in `Run.marks`.
  - The writer joins consecutive uncertain runs, and cuts them at white space and at any non-`Run` inline, exactly as
    `editor.uncertain._spans` counts words.
  - It pairs the k-th word of a container with the k-th `uncertain.Word` of `words_of()` for that `(block, note)`.
  - It writes a comment only when the texts are equal. A mismatch, or a duplicate block or note id, skips the word and
    raises `comments_skipped`.
- **The anchor.** `w:commentRangeStart` goes before the word's first run and `w:commentRangeEnd` after its last,
  followed by a `CommentReference` run.
- **The comment.** Author «نسّاخ», initials «ن», and the date of the export (UTC). Its `CommentText` paragraphs are:
  1. «كلمة غير مؤكَّدة: {word}»;
  2. «القراءات:», then «{label}: {text}» for each reading, with « (في النص)» on the current one;
  3. «الصفحة الأصلية: {page}»;
  4. with no readings (the word was changed since): «لا قراءات أخرى لهذه الكلمة؛ راجعها على الأصل.»

  Latin engine labels are written as LTR runs.
- **Words inside notes** are commented inside `footnotes.xml`. If C6 shows that Word does not display these, the
  comment is anchored on the note's call in the body instead, starting «في الحاشية: ».
- **No words.** With the option on and no uncertain words, no comments part is written. The option is then disabled in
  the UI.

### 5.12 Fonts

- **Families** are the resolved faces' families. A missing face becomes Amiri, as in the preview, with the note
  `font_missing`. Lotus's family is «Lotus Linotype Exnd».
- **The font table.** `fontTable.xml` has one `w:font` per family, with `panose1` from the OS/2 table, `charset 00`,
  `family auto` and `pitch variable`, and `embedRegular`/`embedBold` for an embedded face.
- **Embedding.** Only faces in `EMBEDDABLE = {"amiri"}` are embedded, and only when their OS/2 `fsType` is not
  restricted.
  - Amiri regular and bold (about 0.6 MB each) are obfuscated as ECMA-376 Part 1 §17.8.1 requires: the first 32
    bytes are XORed with the GUID key, whose 16 bytes are taken in reverse order of the key's hex digits.
  - The key is `uuid5("nassakh:<family>:<style>")`.
  - `embedTrueTypeFonts` is set. There is no subsetting.
- **Faces not embedded** get the note `font_not_embedded`.
- **Where it works.** Word for Mac reads embedded fonts from version 16.17 on (the owner has 16.113).

### 5.13 Document properties

- **`core.xml`:**
  - `dc:title`, `dc:subject` (the subtitle), `dc:creator` (the author), `dc:description` (the rights) and
    `dc:language ar`;
  - `cp:lastModifiedBy` «نسّاخ»;
  - `created` and `modified` set to the export time.
- **`app.xml`:** `Application` is `Nassakh` and `Company` is the publisher.
- **`custom.xml`:** `ISBN`, `Edition`, `Publisher`, `City` and `Year` when they are given, plus `NassakhBook`,
  `NassakhManuscriptVersion`, `NassakhExport` and `NassakhRenderer`, so a file can be traced back to its export.

### 5.14 Edge cases (each one is a test)

- A manuscript with only a title, or with no chapters: the body still gets one paragraph and a `sectPr`.
- Duplicate block or note ids: the comment guard.
- A heading with a note call: the contents entry uses `Block.text()`, which is already clean.
- A note with a line break, a paragraph that is all Latin, a 300-character word, XML-forbidden characters, empty notes,
  and a book whose headings all come after its first page of text.

## 6. Exports: model, service, task, API

### 6.1 `publishing.Export`

| Field | Type | Notes |
|---|---|---|
| `book` | FK Book, CASCADE, `related_name="exports"` | |
| `format` | Char(12), choices §3.1 | |
| `status` | Char(10), choices §3.1 | |
| `options` | JSON | normalised, every default filled in |
| `inputs` | JSON | recorded when the task starts: `{stylesheet: stylesheet_values() without updated_at, title, author, digit_style, chapters: {id: chapter_version}}` |
| `manuscript_version` | PositiveInteger, null | the version the task read |
| `stylesheet_hash` | Char(24) | the hash of `setup.as_dict()` and the faces, without the engine version (so a WeasyPrint update does not show Word files as "format changed") |
| `renderer` | Char(64) | §3.1 |
| `file`, `filename`, `size_bytes` | FileField (`books/<id>/exports/<pk>.<ext>`), Char(255), BigInteger | |
| `page_count` | PositiveInteger, null | null for docx |
| `stats`, `progress`, `warnings` | JSON | `progress = {step, done, total}` |
| `log`, `error` | Text | technical lines; the Arabic headline, then `Type: message` |
| `duration_ms`, `task_id` | Integer, Char(64) | |
| `created_by` | FK User, SET_NULL | |
| `created_at`, `started_at`, `finished_at`, `updated_at` | DateTime | `updated_at` is set explicitly on every `update()` |

- **Meta:**
  - ordering `-created_at, -id`;
  - index `(book, format, -created_at)`;
  - `UniqueConstraint(book, format, condition=status in (queued, running))`, which works on PostgreSQL 17 and on
    SQLite in the tests.
- **Migration:** `publishing/0003_export.py`. No editor migration is needed, because there is no snapshot.
- **Admin:** a read-only list with a download link.
- **PLAN.md's `Export` row** is updated to match: the format `docx` is added, `options` and `inputs` replace
  `settings`, and there is no snapshot.

### 6.2 Service (`publishing/exports.py`, typed, with docstrings)

- **`request_export(book, format, options, user) -> Export`.** Inside `transaction.atomic()`, with the Book row
  locked:
  1. the format must be available (400) and the book must have a manuscript (404);
  2. the options are normalised through the exporter's `OptionSpec`s (400 with per-key errors);
  3. an abandoned active row is moved to `error`;
  4. if another row of this book and format is still active, `ExportConflict` → 409 with that row;
  5. the new row is created as `queued`.

  After the atomic block, `tasks.run_export.delay(row.pk)` is called and the `task_id` recorded. If the enqueue fails,
  the row becomes `error` with `ENQUEUE_ERROR` (the `preview._enqueue` pattern).
- **`run_export(export_id, task_id) -> Export | None`**, the task's job. It never raises for an export failure.
  1. **Claim, inside atomic with `select_for_update`.** A missing row, or one that is final, is left as it is. A row
     running under another task id is left alone. The same task id (an `acks_late` redelivery) restarts. Otherwise the
     row becomes `running`.
  2. **Read the inputs once:** the manuscript document and version, the stylesheet and the chapter versions, recorded
     in `inputs`.
  3. **Export.** `get_exporter(format).export(ExportJob(...), Reporter(id))`:
     - `ExportCancelled` or `RenderCancelled` leaves the row cancelled, with no file;
     - `SoftTimeLimitExceeded` becomes `TIMEOUT`;
     - any other exception is logged and the row gets `error = EXPORT_ERROR + "\n" + Type: message`.
  4. **Write the file**, then `filter(pk, status=RUNNING).update(status=DONE, …)`. If that touches 0 rows, the export
     was cancelled during the write and the file is deleted.
  5. **Prune.** Keep the newest `EXPORTS_KEPT` (5) finished rows and 3 failed or cancelled rows per book and format;
     older rows are deleted together with their files.
- **`Reporter(export_id)`.** `__call__(step, done=None, total=None)` writes `progress` at most every 0.5 s and always
  when the step changes, with `filter(pk, status=RUNNING).update(progress=…, updated_at=now)`. An update that touches
  0 rows means the export was cancelled. `cancelled()` returns that flag, and otherwise re-reads the status at most
  once a second (the engine asks it between passes).
- **`cancel_export(row)`.** A queued or running row becomes `cancelled` and its task is revoked (`preview.revoke`).
  Cancelling an already cancelled row is a no-op; a done or failed row gives 409 `FINISHED`.
- **Abandoned exports**, detected as `preview._abandoned` does, from `created_at`:
  - a queued or running row older than the soft limit plus 5 minutes shows as `error ABANDONED`;
  - a row queued for more than 60 s carries `waiting_hint` «لم يبدأ الإخراج بعد؛ تأكّد من تشغيل عامل المهام بعد آخر تحديث (make worker).»
- **Stale exports.** `stale_reasons(row, current)` returns `text` (a different manuscript version), `format` (a
  different stylesheet hash) or `renderer` (a different renderer).
- **Payloads.** `export_payload(row, user)` builds a row, and `page_payload(book, user)` builds the §3.2 object in a
  bounded number of queries (tested with `django_assert_max_num_queries`).
- **`export_filename(row)`** cleans the title:
  - removes control characters, `/ \ : * ? " < > |` and the bidi controls U+200E/F, U+202A–202E and U+2066–2069;
  - collapses spaces and trims dots and spaces at the ends;
  - keeps at most 100 code points;
  - falls back to `كتاب-<id>`.

  It then adds the suffix for the format.

**Messages:**

| Constant | Text |
|---|---|
| `EXPORT_ERROR` | «تعذّر إخراج الملف. أعد المحاولة، وإن تكرّر الخطأ فراجع سجل الخادم.» |
| `ENQUEUE_ERROR` | «تعذّر إرسال الإخراج إلى طابور المهام؛ تحقّق من تشغيل Redis وعامل المهام ثم أعد المحاولة.» |
| `ABANDONED` | «توقّف الإخراج قبل أن يكتمل؛ أعد المحاولة.» |
| `CANCELLED_BY_USER` | «أُلغي الإخراج.» |
| `TIMEOUT` | «استغرق الإخراج أطول من المسموح.» |
| `INVALID_FILE` | «الملف الناتج غير سليم فلم يُسلَّم؛ التفاصيل في سجل الخادم.» |
| `NO_MANUSCRIPT` / `UNKNOWN_FORMAT` / `NOT_AVAILABLE` / `BAD_OPTIONS` / `BUSY` / `FINISHED` | «لا توجد مخطوطة بعد.» / «صيغة الإخراج غير معروفة.» / «هذه الصيغة غير متاحة بعد.» / «خيارات الإخراج غير صالحة.» / «هذا الملف يُخرَج الآن؛ انتظر انتهاءه أو ألغِه.» / «انتهى هذا الإخراج.» |

### 6.3 The exporter interface (`publishing/exporters.py`)

```python
@dataclass(frozen=True)
class ExportJob:
    export_id: int | None; book_id: int; format: str
    document: dict; setup: PageSetup; title: str; author: str; digit_style: str
    chapter_versions: dict[str, str]; options: dict; created: datetime

@dataclass
class ExportResult:
    data: bytes; page_count: int | None = None
    warnings: list[dict] = field(default_factory=list); stats: dict = field(default_factory=dict)
    log: list[str] = field(default_factory=list)

class Exporter(Protocol):
    format: str; label: str; extension: str; media_type: str
    options: tuple[OptionSpec, ...]                         # validation only: key, kind, default, choices, bounds
    version: str
    def form(self, book, values: dict) -> dict: ...         # the page's form block (labels, hints, counts)
    def notes(self, book, setup) -> list[dict]: ...         # the known differences for this book (info/warn rows)
    def export(self, job: ExportJob, progress: Progress) -> ExportResult: ...
```

- **Registry.** `EXPORTERS` maps the format to a dotted path, so the Word, PDF and EPUB streams never edit this file:
  - `publishing.word.DocxExporter`;
  - `publishing.pdf_export.PrintPdfExporter` and `ScreenPdfExporter` (6b);
  - `publishing.epub.EpubExporter` (6c).

  `available_formats()` lists the formats whose module imports.
- **`FakeExporter`** writes fixed bytes, reports its steps and honours cancelling. The pipeline and UI tests use it.

### 6.4 Celery and settings

- **The task.** `publishing.tasks.run_export(export_id)` is routed to the queue `export`, with
  `soft_time_limit = EXPORT_SOFT_LIMIT_S` (1800) and a hard limit 120 s above it.
- **The worker.** The CPU worker consumes `-Q default,layout,export -c 4`. The `Makefile`, the `Procfile` and the
  RUNBOOK change together, because a worker started before the change leaves exports waiting (the `waiting_hint`).
- **Settings (env):**
  - `NASSAKH["EXPORT_SOFT_LIMIT_S"]` = 1800;
  - `NASSAKH["EXPORTS_KEPT"]` = 5;
  - `NASSAKH["EXPORT_VALIDATE"]` = on: the `check` step validates against the XSD. W4 measures its cost, and if it
    takes more than 3 s for 300 pages only the integrity checks run at export time.

### 6.5 API and URLs

| Method | URL (name) | Who | Request → response |
|---|---|---|---|
| GET | `/books/<id>/export/` (`publishing:export`) | login | the export page (`config` = the §3.2 payload) |
| GET | `/api/books/<id>/exports/` (`api:exports`) | login | §3.2 |
| POST | `/api/books/<id>/exports/` | editor, admin | `{format, options}` → 202 row · 409 · 400 · 404 |
| GET | `/api/books/<id>/exports/<eid>/?wait=≤5&since=<iso>` (`api:export`) | login | the row; `wait` long-polls until `updated_at > since` or a final status (like `api.relayout_status`) |
| POST | `/api/books/<id>/exports/<eid>/cancel/` (`api:export_cancel`) | editor, admin | the row; 409 `FINISHED` |
| GET | `/books/<id>/exports/<eid>/download/[?inline=1]` (`publishing:export_download`) | login (a plain Django view, so it redirects to the login page) | `FileResponse(as_attachment=not inline, filename=row.filename)` with `filename*=utf-8''…`, `Cache-Control: private, no-cache`; 404 «الملف غير جاهز.» when not done, when the file is gone, or for another book's export |

- Views are FBVs and stay thin. Anyone signed in may read and download; only editors and admins create and cancel.
- `publishing/urls.py` gains `app_name = "publishing"` and `urlpatterns` (the page and the download) next to
  `api_urlpatterns`. `nassakh/urls.py` gains `path("books/", include("publishing.urls"))`.
- `editor.services.page_config` accepts `?tab=` on `/books/<id>/layout/` (the panel's first tab); `panel.js` prefers it
  over the tab remembered in localStorage.
- **Dev command.** `manage.py export_book <id> --format docx [--options '{…}'] --out PATH` builds the `ExportJob` in
  memory from the current manuscript, with no row, and writes PATH. Agents and the Word harness use it.

## 7. Readiness (`publishing/readiness.py`)

Readiness is a set of warnings with links. It never blocks an export. The rows are ready-made
`{code, level, message, action?: {label, url}}` objects, rendered by the UI without knowing any codes. Counts use a
server-side Arabic count helper (the `layout._lines_phrase` pattern), with Western digits.

**About the book** (`readiness`, the same for every format):

| Code | Source | Level | Message | Action |
|---|---|---|---|---|
| `uncertain_words` | `uncertain.count` + the Kraken numbers (`Word.number`, added in `attach_readings` when the token's `src` is `kraken`) | warn | «بقيت 12 كلمة غير مؤكَّدة في النص، منها 3 أرقام.» | «غير المؤكَّدة» → `?tab=uncertain` |
| `review_drift` | `editor.services.review_drift` | warn | «تغيّر نص 4 صفحات في المراجعة بعد التحرير.» | «الكتاب» → the book page |
| `assembly_running` | an `AssemblyRun` queued or running | warn | «يجري تجميع الكتاب الآن؛ يُخرَج النص كما هو عند بدء الإخراج.» | |
| `book_details` | `setup.details` and the front flags | info | «بيانات الكتاب بلا مؤلف؛ يُكتب في صفحة العنوان وخصائص الملف.» / «صفحة الحقوق بلا ناشر ولا سنة.» | «بيانات الكتاب» → `?tab=format` |
| `no_headings` | `book_model(...).contents() == []` | info | «لا عناوين فصول في الكتاب؛ ستخلو المحتويات.» | |
| `clear` | none of the above | success | «لا ملاحظات؛ الكتاب جاهز للإخراج.» | |

**The Word differences** (`formats[docx].notes`, from `DocxExporter.notes`):

| Code | When | Level | Message |
|---|---|---|---|
| `font_embedded` | a role uses Amiri | info | «خط أميري مضمَّن في الملف.» |
| `font_not_embedded` | per non-OFL face | info | «خط «Simplified Arabic» غير مضمَّن في الملف (ترخيصه لا يسمح)؛ يلزم أن يكون مثبّتًا على الجهاز الذي يُفتح عليه.» |
| `font_missing` | `fonts.resolve().missing` | warn | «الخط «Lotus» غير مثبّت على هذا الجهاز؛ يُستعمل أميري بدلًا منه، كما في المعاينة.» (action «الخطوط» → `?tab=format`) |
| `toc_update` | the book has a contents page | warn | «يرتّب Word صفحاته بنفسه، وقد تختلف أرقام المحتويات بصفحة؛ حدّثها في Word قبل الطباعة: زر الفأرة الأيمن على المحتويات ← تحديث الحقل.» |
| `layout_first` | the live layout is not current for the text | info | «الصفحات أقدم من النص؛ تُرتَّب أولًا لأرقام المحتويات، فيطول الإخراج قليلًا.» |
| `blank_versos` | recto openings and page numbers | info | «الصفحة البيضاء قبل الفصل تحمل رقمها في Word.» |
| `source_pages` | `print_source_pages` | info | «أرقام الصفحات الأصلية في الهامش لا تُطبع في ملف Word.» |
| `widows_approx` | widows/orphans other than 1/1 or 2/2 | info | «يضبط Word الأرامل واليتامى بسطرين دائمًا؛ اختيار {n} لا ينتقل إلى الملف.» |

- **After the build,** the row's `warnings` hold the notes that applied to the file, plus what the build found:
  - `comments_skipped` (warn): «تعذّر وضع تعليق على كلمتين من الكلمات غير المؤكَّدة (تغيّر نصهما).»;
  - `toc_numbers_missing` (warn): «كُتبت المحتويات بلا أرقام صفحات؛ حدّثها في Word.»;
  - `comments` (info): «12 تعليقًا للمدقّق.».
- **The UI** shows the `warn` ones under the latest file.
- **PDF (6b)** adds, as notes, `page_checks` (warn, «3 ملاحظات على الصفحات» → `?tab=chapters`) and `missing_font`. It
  adds, as build warnings, `foreign_fonts` and `layout_mismatch` (§9).

## 8. The export page (UI)

### 8.1 Page or modal: the comparison

| Criterion | (a) Modal on the book page (like «النسخ المحفوظة») | (b) Separate page with blocks | (c) A tab in the book page's side panel |
|---|---|---|---|
| Background jobs (seconds to minutes) | Must be closed to keep working, so the state has to show somewhere else anyway; progress in a dialog invites waiting | Natural: leave and come back; the page resumes the running export | Fine: the panel already polls |
| History and download again | A list in a dialog, with no address to return to | Room for the rows (date, size, options, state) at a stable URL | Cramped at 272–336 px; competes with the other tabs |
| Readiness and fixes | The fixes sit behind the modal: close, fix, reopen | Links go to the exact tab; coming back is one click | Closest to the fixes |
| Options per format | Three formats × 2–4 options is a small app in a box | One block per format; EPUB is one more block | Accordions that compete with the chapters for scroll |
| Alone now, SaaS later | No shareable place for "the files" | A proofreader can open the page and download | The panel assumes someone editing |
| Calm and focused | Quick for defaults, crowded otherwise | One column, three blocks | Adds weight to the densest screen |

**The choice is (b).** It keeps the one real strength of (a), getting the file quickly from the pages: the book page's
top bar gets a primary «الإخراج» that leads to the page, and the page remembers the last options for each book. A
hybrid (a modal to start, a page for the history) was rejected, because it makes two surfaces for one feature and the
modal would still need the readiness rows.

### 8.2 Entry points (v1)

| Where | What |
|---|---|
| Book page top bar (`layout.html`, before «⋯») | `a.btn.btn-primary.btn-sm` with `i-download`, «الإخراج», title «ملفات الكتاب»; only in preview mode (in edit mode «تم» is the one primary) |
| Book page «⋯» | «الإخراج» first in the navigation group; «إخراج PDF» is renamed «PDF المعاينة» until 6b removes it |
| Dashboard «⋯» | «الإخراج» after «الكتاب», when a manuscript exists |
| Manuscript page «⋯» | «الإخراج», when the document exists |

### 8.3 The page (desktop; the right edge is the start side)

> **Amendment (2026-09-26, the owner's request after the build):** the page is a card grid. «قبل الإخراج» spans the
> top (one calm line when clear, a count that says what it counts otherwise); the four format cards sit in two
> columns from 760 px (one column below), each with a generic icon (document, printer, screen, open book), its name
> and a one-line purpose, its options, and a tinted foot with the file's status and a wide primary «إخراج …» beside
> «تنزيل»; «السجل» spans the bottom. Small text on the page is 12.5–13 px in `--color-text-2`. The mockup below shows
> the original one-column layout.

```
┌───────────────────────────────────────────────────────────────────────────────┐
│ ⋯                                                  ‹ الكتاب    الإخراج · كتابي │  top bar
├───────────────────────────────────────────────────────────────────────────────┤
│            17×24 سم · 84 صفحة · 6 فصول · Simplified Arabic 13 نقطة            │  meta line
│                                                                               │
│ ─────────────────────────────────────────────────────────────── قبل الإخراج 2 │  readiness
│ غير المؤكَّدة ›            بقيت 12 كلمة غير مؤكَّدة في النص، منها 3 أرقام     ● │
│ الكتاب ›                      تغيّر نص 4 صفحات في المراجعة بعد التحرير      ● │
│                                                                               │
│ ─────────────────────────────────────────────────────── ‎.docx           Word │  format
│                            [ قوية | متوسطة | خفيفة | بلا ]           الكشيدة │
│                                         الأسطر كما في المعاينة تقريبًا.       │
│                           ☐ تعليقات على الكلمات غير المؤكَّدة (12)             │
│   تعليق Word لكل كلمة بقراءاتها وصفحتها الأصلية، للمدقّق. نسخة للمراجعة لا    │
│                  للمطبعة: يطبع Word التعليقات في الهامش ما لم تُخفِها.       │
│                                                                               │
│  خط «Simplified Arabic» غير مضمَّن في الملف؛ يلزم أن يكون مثبّتًا عند فتحه. ○ │  notes
│                                                                               │
│ [ ⤓ تنزيل ]     كتابي.docx · 1.2 MB · قبل ساعتين ●              [ إخراج Word ] │  state + action
│   يرتّب Word صفحاته بنفسه…؛ حدّث المحتويات في Word قبل الطباعة.              │  warn of the file
│                                                                               │
│ ──────────────────────────────────────────────────────────────────── السجل 3 │  history
│ [ ⤓ ]  ● Word · كتابي.docx · 1.2 MB                  قبل ساعتين · كشيدة خفيفة │
│ [ ⤓ ]  ● Word · كتابي - مع التعليقات.docx · 1.3 MB    أمس · كشيدة خفيفة · تعليقات │
│        ● Word · تعذّر الإخراج                        قبل 3 أيام · كشيدة متوسطة │
└───────────────────────────────────────────────────────────────────────────────┘
```

While it runs, the state row reads `[ إلغاء ]  كتابة الملف…  ●(pulsing)  [ إخراج Word ](disabled)`.

- **Shell.** The normal app shell with the sidebar. A reading column of `max-width: 720px` centered, with 32 px of page
  padding (16 px on mobile).
- **Blocks.** Each block is a head row (13 px/600 title on the start side, `meta` on the end side) over a hairline
  (`border-top: 1px solid var(--color-border)`). There are no cards, boxes or shadows, and blocks are 32 px apart.
- **Fields** reuse the «التنسيق» look: `lo-field`, `lo-stepper-label`, `segmented lo-seg`, `lo-checks lo-check`,
  `help`, `lo-note`.
- **Top bar.** A ghost «‹ الكتاب» link (to `editor:layout`), and «⋯» with «الكتاب», «المخطوطة», a separator and «لوحة
  الكتاب». Title `<span class="title-page">الإخراج</span> · <span class="title-main">{title}</span>`, and head title
  «الإخراج · {title} · نسّاخ».
- **Meta line** from `book.layout`: «17×24 سم · 84 صفحة · 6 فصول · Simplified Arabic 13 نقطة». With no layout it ends
  «… · لم تُرتَّب الصفحات بعد»; while a layout renders, «… · يُحسب…».
- **«قبل الإخراج».** The count badge counts the `warn` rows. Rows use the `bp-check` look: a dot, the message, and the
  link on the end side with a mirrored chevron. The dots are warn, neutral (info) and success, and every dot has text
  beside it.
- **The Word section.**
  - **Kashida:** a 4-way segmented control labelled «الكشيدة», with the chosen choice's hint under it.
  - **Comments:** a checkbox with the count and its help. It is disabled at zero, with the help «لا كلمات غير مؤكَّدة
    في الكتاب.».
  - **Notes:** the `notes` of this format, as `lo-note` lines.
  - **The button:** «إخراج Word». It is `btn-primary` while Word is the only format; with 6b all the format buttons
    become plain `btn`, since none of them is *the* next step.
- **State row** (one state at a time):

  | State | Dot | Text | Extra |
  |---|---|---|---|
  | never exported | – | the head's meta reads «لم يُخرَج بعد» | |
  | done | success | «كتابي.docx · 1.2 MB · قبل ساعتين» | «⤓ تنزيل» (`<a download>`); the file's `warn` warnings under it |
  | done, stale | warning | «… · تغيّر النص بعد هذا الإخراج» | «تنزيل» |
  | queued | accent, pulsing | «في الانتظار…» (after 60 s, the `waiting_hint` as a `lo-note`) | «إلغاء» |
  | running | accent, pulsing | the backend's `progress.label` with «…»; a 4 px bar only when `percent` is known | «إلغاء» |
  | error | danger | «تعذّر الإخراج: {error}» | `details.error-details` «التفاصيل التقنية» (editors, `error_detail`, `pre dir=ltr`) |
  | cancelled | – | the previous finished file comes back | |

- **«السجل».** Rows use the `ed-snap` look, newest first, all formats together (at most about 30, because of
  retention).
  - Line 1: a dot, then «Word · كتابي.docx · 1.2 MB».
  - Line 2 (`meta`): the relative time (the absolute date in `title`), the author, `options_text`, and `stale_text`.
  - «تنزيل» appears when the file is there.
  - Running rows read «قيد الإخراج · {label}», failed rows «تعذّر الإخراج», cancelled rows «أُلغي».
  - Empty: «لا ملفات بعد. يظهر هنا كل ملف أُخرج، بوقته وخياراته.» While loading: two skeleton rows.
- **Page states:**
  - **No manuscript:** `partials/_empty_state.html` (`i-download`), «لا كتاب للإخراج بعد» · «حوّل الكتاب إلى مخطوطة
    وافتحه في صفحة الكتاب أولًا؛ من هنا يُخرَج بعد ذلك ملف Word.» · «فتح المخطوطة».
  - **Read-only role:** the options are disabled, with a `lo-readonly` line «يبدأ الإخراج محرّر الكتاب؛ الملفات
    الجاهزة تُنزَّل من السجل.».
  - **Poll trouble:** the existing `bk-poll` strings.
  - **409:** an inline `field-error` with «هذا الملف يُخرَج الآن؛ انتظر انتهاءه أو ألغِه.», and the state row switches
    to the running row it received.
- **Polling.** After a POST, or on load with `formats[].active`, the page long-polls
  `api:export?wait=4&since=<updated_at>` until the status is final, then fetches the list once. On errors it backs off
  from 2 s to 10 s, as `stage.js` does. It pauses while the tab is hidden and does not poll when idle. Leaving the page
  never stops an export.
- **Keyboard and focus.**
  - No single-key shortcuts. `Esc` closes a menu.
  - The segmented control uses `aria-pressed`, ←/→ and Space/Enter.
  - Each section is a `fieldset` with an sr-only `legend`.
  - **Start:** the button disables, focus moves to the status line (`tabindex=-1`), and the live region says «بدأ
    إخراج Word».
  - **Done:** if focus is inside the section, it moves to «تنزيل»; the live region says «اكتمل ملف Word · 1.2 MB».
  - **Failure:** the live region says «تعذّر إخراج Word».
- **Motion** (muted):
  - blocks fade in (200 ms) and states cross-fade (200 ms);
  - the only continuous motion is the pulsing dot of a running export and the skeleton shimmer;
  - when a file is done, its success dot blooms in once with `rv-stamp-in` (300 ms);
  - `prefers-reduced-motion` turns all of it off.
- **Narrow screens.** Below 560 px the state row stacks (the state first, then a full-width button); file names are
  truncated with an ellipsis. The page never scrolls horizontally, and the 4-way segmented control fits 320 px.
- **Digits.** Western everywhere, inside `<bdi class="num">`; times come from the existing `relativeTime`.

### 8.4 Files

- **Create:**
  - `templates/publishing/export.html`;
  - partials `_export_readiness.html`, `_export_word.html`, `_export_state.html` (shared by the formats, included with
    `key`) and `_export_history.html`;
  - `static/src/js/export.js` (`Alpine.data('exportPage')`: `start`, `cancel`, `poll`, `stepText`; small and readable);
  - `static/src/components/export.css` (`ex-` prefix, imported from `static/src/app.css`; everything else reuses the
    shared classes).
- **Change:**
  - `templates/base.html` (the `i-download` symbol, 24-grid, stroke 1.6);
  - `templates/editor/layout.html` (the button, the menu item, the rename);
  - `static/src/js/book/panel.js` (`?tab=`);
  - `templates/books/detail.html` and `templates/assembly/manuscript.html` (menu items).

### 8.5 Deferred UI (after the owner's test, if wanted)
- the export pill and the completion toast on the book page;
- exports in the preview and dashboard polls, and the dashboard's «آخر إخراج» line;
- «الخيارات نفسها» in the history, delete with undo, «عرض الأقدم»;
- `section=` deep links.

## 9. PDF for print and screen (6b)

| | Print (`print_pdf`) | Screen (`screen_pdf`) |
|---|---|---|
| Boxes | TrimBox = the trim (170×240 mm → 481.89×680.31 pt); the MediaBox grows with bleed and marks; **the BleedBox is rewritten to trim ± `bleed_mm` exactly** (WeasyPrint caps it at 10 pt) | MediaBox = TrimBox |
| Bleed, marks | options; with crop marks, `@page { bleed: bleed + 6 mm slug; marks: crop }`, so the marks sit outside the bleed (WeasyPrint alone draws 1.5 mm marks inside a 3 mm bleed) | none |
| Black | pure K: every literal `#000` in the print CSS becomes `device-cmyk(0 0 0 1)`, including the page numbers, the header and the footnote rule | RGB |
| Outline | kept | chapters at level 1 and sections at level 2 (`bookmark-level`, `bookmark-label: attr(data-label)`, where `data-label` is the heading's text without calls); the title page has none; «المحتويات» at level 1; `PageMode /UseOutlines` |
| Viewer | `ViewerPreferences {Direction /R2L, DisplayDocTitle true}`, `Trapped /False` | `Direction /R2L`, `DisplayDocTitle` |
| Links | the contents links (they exist already) | the same; footnote call → note links are not needed (a note is always on its call's page) and cannot be built |
| Fonts | subsets embedded, then audited | the same |
| Metadata | title, author, subject, creator «نسّاخ», `/Lang (ar)`, dates | the same |

- **Code:**
  - `engine.py`: `PdfOutput(kind, bleed_mm, crop_marks, metadata)` on `RenderJob.output`, where None means the preview,
    unchanged;
  - `css.py` and `html.py`: the rules above;
  - `pdf.py`: `render_pdf(..., write_options, finisher, on_pass)`. `on_pass(step)` is called where `render_pdf`
    already asks `cancelled()` between passes, so no WeasyPrint internals and no logging handler are needed;
  - `pdf_export.py`: the two exporters.
- **Font audit.** PyMuPDF lists each span's font. Any font that is not a book face (`nk-body`, `nk-heading`,
  `nk-latin`, or the resolved faces' names) becomes `foreign_fonts`: «حروف في الصفحات 47، 81 طُبعت بخط «MS Mincho»،
  وهو ليس من خطوط الكتاب.»
- **Layout consistency.** The export's page count and chapter ranges are compared with a finished `PreviewRender` of
  the same job hash. A difference becomes `layout_mismatch`: «عدد صفحات الملف 86 وصفحات الكتاب 84.»
- **Cost.** About 5 s for 84 pages and 50–70 s for 800. The memory use is measured on the largest real book during the
  build.
- **PDF/X-4** needs an ICC profile from the printer, and no local tool validates it. The design offers it only as a
  later option (`NASSAKH["CMYK_PROFILE"]`), without a conformance claim.
- **PDF/A-3b** fits the screen PDF only, and tagging costs 58 % more size. Both are left for later.

## 10. EPUB 3 (6c, deferred)

- **Why deferred:** the owner said "later"; it needs its own reflowable writer and a footnote decision; epubcheck is
  not installed.
- **What exists now.** The model, the registry and the API include `epub` from the start (`available: false`), so 6c
  adds one module and its tests.
- **`publishing/epub.py`** (ebooklib 0.20, built from `book_model`):
  - **Package:**
    - EPUB 3, `dc:language ar`, `page-progression-direction="rtl"`, and `lang="ar" dir="rtl"` on every document;
    - the identifier is `urn:isbn:` when an ISBN is given, else a stable `uuid5`;
    - metadata: title, author, editor and translator roles, publisher, year and rights.
  - **Documents:**
    - title and copyright pages;
    - `nav.xhtml` with the contents and landmarks;
    - one XHTML file per chapter.
  - **Blocks:** `h1`, `h2`, `p`, `blockquote`, `p.verse` with `<br/>`, `p.center`, `p.separator`, `b`, `i`.
  - **Footnotes:**
    - numbered per chapter, because a reflowable book has no pages (an owner decision in 6c);
    - `epub:type="noteref"` calls «(n)»;
    - `aside epub:type="footnote"` at the chapter's end, with a back link.
  - **Fonts:** Amiri Regular and Bold embedded whole, with its `OFL.txt` (Amiri is a Reserved Font Name). Other faces
    fall back to Amiri (D45), with a note.
- **Checks:** read the file back with ebooklib, parse every XHTML file, check every noteref target, the spine
  direction and the manifest's fonts. epubcheck runs in a dev check when it is installed.

## 11. Tests and quality gates

All tests use pytest with `nassakh.settings_test` (eager Celery, SQLite, a throw-away media folder), run as
`.venv/bin/python -m pytest <paths> -q`.

### 11.1 Word (`publishing/test_word.py`)
1. **Schema.** Every `word/*.xml` part of every file the tests generate is validated against the vendored XSDs, loaded
   once per process.
   - This includes `fontTable.xml`: our writer uses no Word 2010+ extension namespaces. If the XSD set cannot handle
     it, the integrity checks cover it.
   - The OPC and docProps parts are covered by reading the file back with python-docx, not by XSD.
2. **Integrity** (`schema.integrity_errors`), what the XSD cannot check:
   - every `pStyle` and `rStyle` is a style id;
   - every footnote is referenced once;
   - comment starts, ends and references are paired;
   - every `r:id` resolves, every rel target exists, and every part has a content type;
   - hyperlink anchors and PAGEREF targets are bookmarks, and bookmarks are unique and paired;
   - fields are balanced;
   - only the first body section may carry `pgNumType@start`;
   - there is no `updateFields`, and `zoom@percent` is present.
3. **Golden files** (canonical XML in `publishing/word/golden/`, updated with `NASSAKH_UPDATE_GOLDEN=1`, built with the
   `publishing/tests.py` fixture helpers):

   | Golden | Covers |
   |---|---|
   | G1 | one chapter: the body, the `Normal`/`Heading1` styles, `settings.xml` |
   | G2 | marks and direction: bold, italic, a Latin word, digits, a break; the same with Lotus (`NkLatin`) |
   | G3 | footnotes with numbering per page, per chapter and per book, including the separator |
   | G4 | front matter with every detail, and the prefilled contents field |
   | G5 | recto openings × chapter header × outer numbers: the sectPr sequence and the header/footer parts |
   | G6 | comments, including a word across a bold boundary and a word in a note |
   | G7 | a book without headings, numbered per chapter (continuous sections) |
   | G8 | block attributes and the margin-collapse overrides |

4. **Options matrix.** About 16 pairwise combinations of:
   - kashida;
   - comments;
   - footnote numbering;
   - running header;
   - page number;
   - chapter opening;
   - front-matter toggles;
   - body font (Amiri or Lotus).

   Each combination must pass the schema and integrity checks and be read back by python-docx: paragraph count,
   styles and `document.comments`.
5. **Model tests:**
   - `editorial=False` leaves `book_model` unchanged, and every existing test stays green;
   - `editorial=True` gives the same `Block.text()`, and the uncertain words the writer rebuilds equal `words_of()`
     for the same container. Cases: a word across marks, two words in a row, next to a call or a scan mark, in a note,
     in a quote.
6. **Fonts:** applying `obfuscate` twice gives the file back; the key is derived as specified; only Amiri is embedded;
   a missing face falls back to Amiri with a note.
7. **Determinism:** two builds with the same `meta.now` are byte-identical.
8. **Performance:** a synthetic 300-page book (20 chapters, about 2,400 paragraphs, 600 notes, 100 uncertain words,
   Amiri embedded) builds in 6 s or less (the target is 2 s), and the file is 3 MB or less.
9. **Edge cases** of §5.14.

### 11.2 Pipeline (`publishing/test_exports.py`, using `FakeExporter`)
- **Model:** the partial unique constraint (`IntegrityError` for a second active row; other formats and books are
  fine).
- **Options:** defaults, choices, bounds, unknown keys dropped, and the last options used.
- **`request_export`:**
  - 202;
  - 409 carrying the active row;
  - 404 without a manuscript;
  - 400 for an unavailable format or bad options;
  - an abandoned row is replaced;
  - an enqueue failure gives `ENQUEUE_ERROR`.
- **`run_export`:**
  - the file ends up at `books/<id>/exports/<pk>.<ext>`;
  - `inputs` and the manuscript version are recorded;
  - a run on a final row does nothing;
  - a running row under another task id is left alone, and a redelivery restarts;
  - an edit during the run does not get into the file, and the row then reads stale `text`.
- **Cancel:**
  - a queued row gets no run;
  - a running row's `Reporter` sees 0 rows updated, and `ExportCancelled` leaves no file;
  - a cancel during the write deletes the file.
- **Errors:** the Arabic headline plus the technical line; `TIMEOUT`; `INVALID_FILE`.
- **Retention:** 5 done rows and 3 failed ones per format, with their files deleted.
- **File names:** an Arabic title with `/`, quotes, a bidi control and 300 characters; the `filename*` header.
- **API:**
  - anonymous users get 403, or a login redirect for the download;
  - a proofreader can read and download, but gets 403 on POST and cancel;
  - another book's export gives 404;
  - `wait` is capped at 5 s and returns early on a change;
  - `download_url` appears only when the export is done.
- **Other:** task routing to `export`; bounded query counts for the page payload; each readiness row; `export_book`
  writes its file without creating a row.

### 11.3 UI (`core/test_export_ui.py`)
- **Rendered templates:** every state of the Word section and of the history, readiness with and without rows, the
  read-only view, the empty page, the entry points on the book page, the dashboard and the manuscript page.
- **The Alpine component under the Node harness,** against the §3.2 fixtures:
  - POST → the running state → the long-poll loop → done with «تنزيل»;
  - 409 → the active row shown with «إلغاء»;
  - back-off, and resuming from `formats[].active`;
  - cancel;
  - the kashida hint follows the selection;
  - the comments checkbox is disabled at 0.
- **Also:** the test that fails on multi-line `{# #}` comments covers the new templates. No Chrome MCP is used unless
  the owner asks.

### 11.4 PDF (6b, `publishing/test_pdf_export.py`)
- **CSS:** the preview CSS never has bleed; the print CSS has `bleed: 9mm; marks: crop` for 3 mm with marks, and only
  `device-cmyk` colours.
- **Print PDF:** the boxes in points, the marks outside the TrimBox, `0 0 0 1 scn` and no `rg` on body pages, and the
  same page count and chapter ranges as the preview job.
- **Screen PDF:** `get_toc()` levels and clean labels, `Direction /R2L`, the metadata, the contents links.
- **Fonts:** the Latin-fix regression, subset prefixes and no Type 3 fonts, and `foreign_fonts` flagging a CJK
  character with its page.
- **Passes:** `on_pass` reports each pass, and a cancel between passes raises `RenderCancelled`.

### 11.5 The dev-only Word check (`manage.py word_check`, macOS with Microsoft Word, never in pytest)

```
.venv/bin/python manage.py word_check 19                  # build, open in Word, compare pages and lines with the preview
.venv/bin/python manage.py word_check 19 --kashida medium
.venv/bin/python manage.py word_check 19 --chapters       # each chapter alone (front_matter=False)
.venv/bin/python manage.py word_check --calibration       # write calibration.docx, open it, print the checklist
```

- **Safety first**, so no dialog and no duplicate window:
  - every file is validated before Word sees it;
  - the file is copied into `~/Library/Containers/com.microsoft.Word/Data/Documents/nassakh/`, so Word asks for no
    file access;
  - if Word has documents open that are not the harness's own, it refuses with «أغلق مستندات Word المفتوحة أولًا»;
  - if only its own files are open, it quits Word without saving (they are unchanged copies) and opens exactly one
    file;
  - the build agent tells the owner before a run, because Word opens on the owner's screen.
- **What it reads:** `compute statistics` for pages, lines, and lines including notes. It prints a table against the
  preview's layout (pages, body lines and note lines, per chapter) and leaves the file open for looking.
- **Limits:** no Word-made PDF, no per-page positions, and no page number for a bookmark.

### 11.6 Calibration (looked at once by the owner, about 15 minutes; results → D62 and the constants)

`calibration.docx` has one labelled page per item, each with its expected result in Arabic.

| Item | Checks |
|---|---|
| C1 | inner 40 mm / outer 10 mm, variants A (`left=inner`) and B (`left=outer`), 4 pages each: «الهامش العريض في الصفحة 1 على اليمين وفي الصفحة 2 على اليسار» |
| C2 | outer page numbers and the header on odd and even pages |
| C3 | 3 pages with 2 notes each: (1) (2) on every page |
| C4 | the separator's width and gaps |
| C5 | one paragraph with none, low, medium and high kashida, **in each face**: Amiri, Simplified, Traditional, Lotus |
| C6 | a comment on a body word and on a word in a note |
| C7 | embedded Amiri: disable Amiri in Font Book and reopen; the Arabic must stay in Amiri |
| C9 | the contents: leader dots, numbers at the end side, ⌘-click follows the link |
| C10 | bidi meaning: paragraphs with `jc` left/right, `ind` left and a right tab, each with its expectation |
| C11 | Amiri with stacked tashkeel at pitch 1.35 and 1.7: no clipped tops |
| C13 | a justified line ending in `w:br` stays unstretched |
| C14 | recto openings: what the blank page shows |
| C15 | space before at the top of a page: after a forced break (chapter 16 mm, title 28 %) and after a natural one (a 5 mm section title) |
| C8 | the Arabic-Indic page and note numbers (only if Q3 is yes) |

- **Automatic in the harness:** C0 (book 19 in mode 15 against mode 14) and C12 (60 pairs of 10 mm after plus 10 mm
  before; the page count tells a sum from a maximum).
- **A reference file made in Word (5 minutes, `playground/word/reference/r1.docx`).** In Word, the owner makes:
  - an RTL paragraph with a 1 cm start indent;
  - mirror margins (inside 3 cm, outside 1 cm);
  - outside page numbers on odd and even pages;
  - a contents field;
  - a footnote and a comment.

  How Word writes its own XML settles C1 and C10 without guessing, and the owner can make it any time after "go".

### 11.7 Gates

| Gate | Pass condition | When |
|---|---|---|
| Automatic | the whole suite green: schema and integrity on every generated file, goldens, matrix, determinism, performance, pipeline, UI; `manage.py check`; `make css` | end of every stage |
| Parity (harness) | `word_check 19` at low kashida: Word pages ≤ preview + 2; body lines within 0.5 % and note lines within 1 % of the preview; each chapter within ±1 page. C0 and C12 recorded | stage 3, measured against the `nk-print-5` preview |
| Calibration | the owner's pass; D62 recorded; the constants fixed; the goldens updated | stage 5 |
| Owner test | §13 | end of 6a |

If the parity gate fails, the fallbacks are tried in order and measured:
1. compatibility mode 14;
2. bare superscript calls, as in the spike;
3. escalation to the owner with the numbers.

## 12. Build plan: the multi-agent workflow

- **How it runs.** The lead orchestrates one Workflow script. Agents are few and efficient (the owner, 2026-09-26:
  "few efficient can do the job … save my usage on Fable").
- **Fable** builds the critical piece, the Word builder.
- **Opus** builds the rest: the reference fix, the pipeline, the export page and the integration. This narrows
  decision 6's "Fable for the UI parts" as the owner later asked; to put the export page on Fable instead, swap stage
  2c's model and nothing else changes.
- **What every agent receives:**
  - the spec sections it owns, the §3 contract and its file list;
  - the owner's style: FBVs, thin views, typed services with docstrings, small Alpine components, Arabic UI text,
    English code, and settings from env;
  - the rules: `.venv/bin/python` only, never `uv run`; no writes to the dev database (books are exported with
    `export_book --out` into the scratchpad; the integrator may create Export rows for book 19, since the dev books
    are test data); no Chrome MCP.
- **What every agent returns:** its files, its test counts and its open issues.

| Stage | Agent | Owns (files) | Needs | Ends with |
|---|---|---|---|---|
| 0 | Lead | `docs/DECISIONS.md` (D52–D61, D62 pending), `docs/PLAN.md` (Export row, Phase 6 line); copies the XSDs and the spike's validator from the scratchpad into `publishing/word/xsd/` with `SOURCE.txt` **first**, because `/private/tmp` does not survive a reboot; `publishing/fixtures/export/*.json` (§3.2: the page payload and a row in every state) | "go" | the contract frozen |
| 1a | Opus "reference" | `publishing/fonts.py`, `publishing/css.py` (preview bleed), `publishing/engine.py` (version), new `publishing/test_reference.py` | 0 | the Latin fix and `nk-print-5`; book 19's new preview counts (pages, lines, per chapter) rendered in memory and written to `playground/word/REFERENCE.md` |
| 1b | Opus "contract" | `publishing/models.py`, `migrations/0003_export.py`, `admin.py`, `exporters.py` (types, `OptionSpec`, registry, `FakeExporter`) | 0 | model tests green |
| 2a | Opus "pipeline" | `publishing/exports.py`, `readiness.py`, `tasks.py`, `api.py`, `urls.py`, `views.py` (page and download), `management/commands/export_book.py`, `nassakh/urls.py` (one line), `nassakh/settings.py`, `.env.example`, `Makefile`, `Procfile`, `docs/RUNBOOK.md` (a «الإخراج» section: worker queues, `export_book`, `word_check`, calibration), `editor/uncertain.py` (`Word.number`, count by kind), `editor/services.py` (`page_config` `?tab=`), a placeholder `templates/publishing/export.html`, `publishing/test_exports.py` | 1b | the pipeline suite green, payloads equal to the fixtures |
| 2b | **Fable** "Word" | `publishing/word/**`, `publishing/model.py` (`editorial`, `Style.word` ids), the two `Style.word` assertions in `publishing/tests.py`, `management/commands/word_check.py`, `publishing/test_word.py` | 0 (the pure core), 1b (the exporter) | W0 foundation (ooxml, package, schema, conventions, the calibration file) → W1 styles, runs, sections, body → W2 footnotes, front matter, contents, plan → W3 comments, fonts, properties, `DocxExporter`; goldens G1–G8, matrix, model and font tests green |
| 2c | Opus "page" (Fable if the owner prefers) | the §8.4 files, `core/test_export_ui.py` | 1b fixtures | the UI suite green against `FakeExporter` and the fixtures; `make css` |
| 3 | Opus "integration" | seams only (it reports cross-stream changes rather than rewriting) | 1a, 2a–2c | `DocxExporter` registered; `export_book 19 --format docx` end to end; the performance test; the full suite; **after telling the owner**, `word_check 19` (parity gate, C0, C12) |
| 4 | Opus "verifier" (light, as the owner asked: small checks during the build) | none | 3 | gates re-run on a clean checkout; the fixtures against real payloads; the file-ownership rules held; a list of issues. The full code review runs only after the owner's hand test, when the owner asks |
| 5 | Owner, then the lead | – | 4 | the calibration pass and r1 → D62 and the constants (one Opus fix-up agent if needed) → the owner's test (§13) |
| 6b | Opus "PDF" + the same page agent as 2c | `engine.py`, `css.py`, `html.py`, `pdf.py`, `pdf_export.py`, `test_pdf_export.py`; the PDF sections `_export_pdf_print.html`, `_export_pdf_screen.html` | the owner's OK on 6a, Q7 | the 6b gates; the owner tests |
| 6c | Opus "EPUB" | `publishing/epub.py`, `test_epub.py`, the EPUB section | the owner's OK on 6b | the owner tests |

**Rules:**
- No two stages edit the same file at the same time.
- Word keeps `EMBEDDABLE` in `faces.py`, not in `fonts.py`.
- There is no `engine.export_word` façade.
- Only 2a edits `api.py`, `urls.py` and `editor/uncertain.py`. Only 2b edits `model.py`.
- 1a writes its tests in a new file. 2b changes only the two `Style.word` assertions in `publishing/tests.py`.
- The UI agent replaces 2a's placeholder template.

## 13. Acceptance: what the owner tests at the end of 6a

- [ ] From the book page, «الإخراج» opens the export page. «قبل الإخراج» lists what is left, and its links land on
      the right tab.
- [ ] «إخراج Word» with the defaults makes «كتابي.docx» in seconds. The history shows it, and «تنزيل» saves it under
      its Arabic name.
- [ ] **Opening the file.** Word opens it with no dialog and without «وضع التوافق». It is RTL throughout, its styles
      appear in Word's list («عادي», «عنوان 1», «شعر»…), and the text is in the book's faces.
- [ ] **Pages.** They match the book page within about one page; `word_check` shows the counts.
- [ ] **Footnotes** sit at the page foot, their «(1)» restarts on every page, and the calls «(n)» are raised as in the
      preview.
- [ ] **Margins.** The wide margin is on the binding side of every page, and the page numbers and running header
      follow the stylesheet.
- [ ] **Contents.** The contents page has page numbers, and ⌘-click goes to the chapter. «تحديث الحقل» gives the same
      entries and corrects any number that drifted. The note about this is shown with the file.
- [ ] **Front matter.** The title and copyright pages are there, and File › Properties shows the title, author and
      ISBN.
- [ ] **Kashida.** Low kashida looks right in the book's face, and medium makes the book longer, as its hint says.
- [ ] **Comments.** With «تعليقات», every uncertain word has a comment by «نسّاخ» with its readings, and the file name
      ends «- مع التعليقات».
- [ ] **Amiri.** A book set in Amiri stays in Amiri on a Mac without it (C7).
- [ ] **Running exports.** A second «إخراج Word» while one runs is refused and offers «إلغاء», and cancelling works.
      An edit made after an export marks it «تغيّر النص بعد هذا الإخراج».
- [ ] **Throughout.** RTL, Western digits, Arabic copy, keyboard reach and reduced motion.

## 14. Risks

| # | Risk | Handling |
|---|---|---|
| R1 | **The mirror side in RTL is unverified.** One wrong constant puts every book's binding margin on the wrong side | C1 and r1 before the owner's test; one constant |
| R2 | **Word paginates about ±1 page differently** («كتابي»: chapter 5 is 31 pages against 30), so the contents numbers after the drift point are one off, more with medium or high kashida | the unconditional `toc_update` note; the real field updates in one click; the harness per chapter |
| R3 | **The reference moves.** The Latin fix changes Latin widths | stage 1a lands first; parity is measured only against `nk-print-5` |
| R4 | **The bump invalidates every live layout at once**, so the first exports take the layout-only render (about 1.3 s for 84 pages, about a minute for 800) | the `layout` step and the `layout_first` note are shown; the "≤ 5 s" figure holds only for a current layout |
| R5 | **The spike did not measure the final build** («(n)» calls, continuous numbers, header, real contents, mode 15) | the stage 3 parity gate, with the fallbacks in §11.7 |
| R6 | **Stacked tashkeel can be clipped at an exact pitch** (Amiri's winAscent is 1.86 em). No fallback keeps parity (`atLeast` with Amiri gives about 2.75× lines) | C11; escalate to the owner if it fails |
| R7 | **Space before at a page top follows Word's rules, not CSS's** | C15; direct spacing where they differ |
| R8 | **Which Word opens the file.** Windows Word has other builds of Simplified Arabic and a digit-shape setting that can show ١٢٣ | Q1: the Mac is the reference; one Windows check if proofreaders or printers use it |
| R9 | **Faces missing at the recipient.** Word substitutes, and the pages change | the notes; Amiri is embedded |
| R10 | **Font obfuscation mistakes** | a round-trip test; C7 |
| R11 | **Comments inside footnotes may not show** | C6; a fallback on the call |
| R12 | **Word dialogs and duplicate windows on the owner's screen** (seen in the spike) | validate first; the harness's refusal rules; the owner is told before each run |
| R13 | **An old worker ignores the new queue**, so exports wait forever | the `waiting_hint` after 60 s; the Makefile, Procfile and RUNBOOK change together |
| R14 | **The XSDs exist only in the scratchpad** | stage 0 copies them first; otherwise they can be rebuilt from python-docx's repository `ref/xsd` |
| R15 | **Blank versos show a page number** in Word | accepted in v1 (Q5); a later fix: per-chapter footers `IF {PAGE} > {PAGEREF _nk_end_<id>}` if C14 shows that Word for Mac evaluates it on every page |
| R16 | **Large PDF exports (6b)**: 50–70 s, and memory not yet measured | one export per format, the soft limit, measured on the largest book in 6b |

## 15. Open questions for the owner

1. **Who opens the Word files, and on which Word?** The reference is Word 16 on your Mac. If a proofreader or the
   printer uses Word for Windows, one look at the calibration file there is worth it. Recommended: the Mac is the
   reference, and a Windows check only if you know someone will use it.
2. **Page numbers.** Keep them continuous from the title page, as the book page does today (front pages count but
   print nothing)? Or restart at 1 on the first chapter, in the book page and in Word alike? Recommended: continuous.
3. **Arabic-Indic numbers.** In books printed with Arabic-Indic digits, should the page numbers, footnote numbers and
   contents numbers print ١٢٣ in the preview and in Word? It is one change in both renderers. Recommended: yes.
4. **Kashida.** Offer medium and high at all (both make the Word book longer than the preview), or only «بلا» and
   «خفيفة»? Recommended: offer all four, with the hint.
5. **Accept for v1 in Word:** a page number on the blank page before a recto chapter; scan-page marks not printed;
   contents numbers taken from the preview, with the "update the field" note. Recommended: accept.
6. **Calibration.** About 15 minutes looking at `calibration.docx` (with Font Book for C7), plus 5 minutes making
   `r1.docx` in Word. When suits you?
7. **For 6b, can wait:** what does your printer ask for (bleed 0, 3 or 5 mm, crop marks, PDF/X with a profile)? Until
   then, the print PDF's defaults are no bleed and no marks.

### 15.1 The owner's go and the lead's choices (2026-09-26, night)
The owner said "go", asked for Word, PDF and EPUB by the next morning ("Files (Word/PDF/EPUB) match what we see in the
preview editor: same pages, same fonts, same footprint"), few efficient agents, Fable only for the critical piece, small
checks during the build and the review after his hand test. So 6a, 6b and 6c are built in one run (§12's stages, 6b and
6c after 6a's backend, not after the owner's tests), and these answers stand until the owner changes them:
1. The Mac's Word 16 is the reference (Q1).
2. Page numbers stay continuous from the title page (Q2).
3. Digits stay Western in page, footnote and contents numbers (Q3: no; D6 "Western digits always" holds).
4. All four kashida levels are offered, with the hints (Q4).
5. The v1 Word differences are accepted (Q5).
6. Calibration waits for the owner (Q6); until then the constants keep the spike's evidence and the best reading of
   the OOXML specification, and D62 stays pending.
7. The print PDF defaults to no bleed and no crop marks, both offered as options (Q7).
