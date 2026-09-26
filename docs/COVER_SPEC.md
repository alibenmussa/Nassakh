# The cover (الغلاف) — spec (2026-09-27, D80)

The owner (2026-09-27): "the cover page should be a separate page. It is either generated from the book info,
or an image (fit width, fit height or 100 %), or custom text (text in the centre and text at the bottom); and I
can disable it. Do this in a good, perfect way … whenever we change the editor, make sure it works in all
exports." The cover is also the first place the system handles an uploaded image and colours, so the image
pipeline (`editor.BookImage`) is built to serve body images later.

## 1. Decisions (D80)

1. **A separate page, outside the book's pages.** The cover is rendered on its own (its own small WeasyPrint
   document, one page, the book's trim, no margins) and never enters the interior's layout: page numbers,
   recto / verso, chapter openings, the footprint («N صفحة»), the contents' page numbers and the live layout
   stay exactly as they are. Each output adds it in front of the interior.
2. **Four modes** (`front_matter.cover.mode`): `none` «بلا غلاف» · `info` «من بيانات الكتاب» · `image` «صورة» ·
   `text` «نص مخصّص». A stylesheet without a `cover` key, and the defaults, are `none`: no existing book or
   export changes until the owner picks a cover.
3. **Image fits** (`fit`): `fill` «ملء الصفحة» (the page is covered, the overflow cropped, centred; CSS
   `object-fit: cover`), `width` «ملاءمة العرض» (the whole width, centred vertically, the background above and
   below), `height` «ملاءمة الارتفاع» (the whole height, centred horizontally, the background at the sides). Fill
   is the default. The page's background colour shows wherever the image does not reach.
4. **Text layout.** `info`: the title (heading face, bold), the subtitle and the author in a block centred in
   the page; the imprint (publisher، city، year, as the title page writes it) at the bottom. `text`: the owner's
   centre text (lines kept, each centred) and bottom text. The bottom block sits `bottom_mm` from the trim edge
   (default: the page's bottom margin + 8 mm). Faces from the stylesheet: centre = heading face, the rest =
   body face; sizes `center_pt` (default 28) and `bottom_pt` (default 13), the subtitle and the author at
   0.55 × `center_pt`.
5. **Colours ("the theme").** `background` and `color` (hex `#rrggbb`), with named presets: «أبيض» #ffffff /
   #1b1b1b, «كريمي» #f4efe4 / #2a2419, «رمادي» #e9e9ec / #1f1f24, «كحلي» #1d2433 / #f3efe6, «أخضر داكن»
   #1f3b2d / #f1ead8, «عنابي» #4a1d24 / #f4e9d8. Default «أبيض». Any colour can be set by hand.
6. **Per output.**
   - **Book page:** a sheet «الغلاف» before page 1 in the stage (alone, on the right, in the spread view) and
     a first thumb «الغلاف» in the filmstrip; neither is counted as a page. A click opens the cover section
     of «التنسيق». It updates about a second after a change.
   - **Screen PDF:** the cover is page 1, prepended to the interior (PyMuPDF `insert_pdf`; the outline and the
     links move with their pages). `/PageLabels`: the cover «غلاف», the interior from 1, so the viewer's page
     numbers are the printed ones. The outline gets no entry for it.
   - **Print PDF:** printers take the cover as its own file (with the spine and the back), so the interior has
     no cover by default. Option `cover` «تضمين الغلاف في أول الملف» (off), hint «تطبع المطابع الغلاف عادةً
     ملفًّا منفصلًا مع الكعب والظهر؛ لا تحدّده إلا إن طلبته المطبعة.». Included, the cover gets the interior's
     boxes (trim, bleed, slug) and crop marks, and an image or background runs into the bleed.
   - **Word:** a first section of one page holding one picture: the cover rasterised from the same render at
     300 dpi (JPEG q90 when the cover shows a photo, PNG otherwise), anchored to the page at 0,0, exactly the
     page's size, behind text, no header or footer. The next section restarts page numbering at 1, so the
     interior's numbers, odd / even pages and contents field are unchanged. (A picture, not live text: it is
     the preview's cover exactly, fonts included.)
   - **EPUB:** `cover.xhtml` first in the spine (`epub:type="cover"`, landmark «الغلاف»), showing the cover
     rasterised from the same render (1600 px tall), declared `properties="cover-image"` (and the EPUB 2
     `meta name="cover"`), so readers show it in the library.
7. **Images** (`editor.BookImage`, reusable for body images): JPEG, PNG or WebP up to 30 MB and 12 000 px a
   side, checked with Pillow (`verify`, then load); EXIF orientation applied; CMYK and palette images
   converted to sRGB; stored normalised (JPEG q92 when opaque, PNG when it has transparency) under
   `media/books/<id>/images/<sha256>.<ext>`, with width, height and the source's name. Files are immutable
   (named by content), never deleted when the cover changes (a queued export keeps its image).
8. **Readiness** (the export page's «قبل الإخراج»): `cover_image_missing` (warn) «اختير غلاف بصورة ولم تُرفع
   صورة؛ لن يكون للكتاب غلاف.»; `cover_resolution` (warn for print, info otherwise) «دقة صورة الغلاف 150 نقطة
   في البوصة على هذا القطع؛ يُستحسن 300 للطباعة.» (the effective dpi of the image as fitted).

## 2. Data

- `StyleSheet.front_matter["cover"]` (JSON, no migration):
  `{mode, image (BookImage id | null), fit, center, bottom, center_pt, bottom_pt, background, color, preset,
  bottom_mm}`. `editor.services.front_matter_values` fills the defaults; `stylesheet` PATCH validates every
  key (modes, fits, hex colours, 0–600 characters for each text, sizes 8–96 pt, `bottom_mm` 0–80, the image
  belongs to this book) with per-key errors `front_matter.cover.<key>`, like `front_matter.fields.*`.
- `editor.BookImage` (migration `editor/0006_bookimage.py`): `book` FK CASCADE, `file`, `sha256` (unique per
  book), `width`, `height`, `format` (jpeg | png), `source_name`, `purpose` (cover | body), `uploaded_by`,
  `created_at`.
- `publishing.model.Front.cover` (`CoverSpec`, frozen): the parsed settings plus the image's path, size and
  sha; `book_model` fills it; `none` or an image mode without an image → `Front.cover = None`.

## 3. Rendering (`publishing/cover.py`, new)

- `cover_html(book, setup, *, bleed_mm=0, slug_mm=0)`: one page, `@page { size; margin: 0 }` (plus bleed and
  slug for print), a `div.nk-cover` of the trim (+ bleed) size with the background, the image (`img`,
  absolutely placed per fit), and the two text blocks. The faces are the preview's `@font-face` roles
  (`publishing.css`), so text shapes as on the pages.
- `render_cover(book, fonts, *, bleed_mm=0, slug_mm=0) -> bytes` (the PDF), `cover_raster(pdf, *, dpi | height)
  -> (bytes, media_type)`.
- The book page's cover: `GET /api/books/<id>/cover/` → `{mode, hash, image_1x, image_2x, width, height}`
  (null images for `none`); the render is cached by hash (the cover settings, the image sha, the trim, the
  faces' files, the renderer version) under `media/books/<id>/cover/<hash>/` (`cover.pdf`, `cover.webp`,
  `cover-2x.webp`), rendered in the request (one page, under a second) when missing; the newest 3 are kept.
- `POST /api/books/<id>/images/` (multipart `file`, `purpose`) → `{id, url, width, height, format}`; 413 / 422
  with an Arabic message for a file too large, not an image, or a type not allowed. Editors and admins only.

## 4. The book page (UI)

- «التنسيق» gets a first section «الغلاف» (above «الصفحات والترقيم»…): the mode as a segmented control; then
  per mode —
  - `image`: a drop zone «اسحب صورة الغلاف إلى هنا أو اخترها» with «اختيار صورة…» (a thumbnail once
    uploaded, «تغيير الصورة…», «إزالة الصورة»), upload progress, the size note «للطباعة: 300 نقطة في البوصة،
    أي 2008 × 2835 بكسل على هذا القطع» (computed from the trim), the fit segmented control;
  - `text`: «نص الوسط» and «نص الأسفل» (textareas, lines kept);
  - `info`: the note «يُؤخذ العنوان والعنوان الفرعي والمؤلف من «بيانات الكتاب»، والناشر والمدينة والسنة في
    الأسفل.» with a link that opens «بيانات الكتاب»;
  - every mode but `none`: «الألوان» (the presets as swatches with their names, then «لون الخلفية» and «لون
    النص» pickers), «حجم نص الوسط» and «حجم نص الأسفل» steppers (pt, not for `image`).
- Saves through the stylesheet PATCH as every format field does (debounced for text); errors under their
  field. The stage's cover sheet and the filmstrip thumb follow `api:cover`.
- RTL, Western digits, keyboard reach, reduced motion, the calm look of the rest of the panel.

## 5. Tests and checks

- `publishing/test_cover.py`: settings parsing and defaults; HTML per mode and fit; the three fits' image boxes
  on a portrait and a landscape image (PyMuPDF `get_image_info`); the hash; raster sizes.
- `editor/tests.py`: upload (types, size, EXIF rotation, CMYK → RGB, WebP → JPEG / PNG, other book's image
  refused, reader 403); the PATCH validation; `api:cover`.
- Exports: screen PDF (page 1 is the cover, labels, the outline and every link still on their pages, the page
  count = the interior's + 1); print PDF (no cover by default; with the option, the cover's boxes equal the
  interior's, marks drawn); Word (XSD-valid; the first section one picture of the page's size; the second
  restarts at 1; with `none` the file is byte-identical to before); EPUB (cover-image, cover.xhtml first, the
  landmark, the check passes); readiness rows.
- The integrator exports a real book in all four formats with each mode (and each fit) on a copy of the dev
  database, rasterises the PDFs' first pages and the EPUB cover and looks at them; Word is checked by schema
  (driving Word only with the owner's word).
