# Owner's full-pipeline review notes (2026-10-03), translated and grouped

Owner: "order is not important"; wants the work done overnight. Items grouped by area so agents do not collide.

## Wave 1 (parallel)
- **review-ui**: 1 correction dialog lists the word chosen "in the text" as option 1 so «1» accepts it; 15 clicking a word
  on the processed page image opens a dialog that does not close on outside click and does not show the word in the
  correction field: wanted = the same dialog as the transcribed text, same behaviours, correction text inside, closes
  on outside click; 2 better generation animation of the Tesseract text on the processing and review pages (stronger
  motion, line shading like a shimmer or the blue box moving over the page).
- **sheet-layout**: 3 layout grid view shows the original image, must show the processed (preprocessed) image because
  regions drawn over the original land wrong; 4 an `overflow hidden` in the layout grid cuts the yellow review
  circles (z-index?); 13 progress shown well: the completed-lines share of the processing progress must follow the
  percentage; 14 the «تتبع المعالجة» button is crowded, does not work, purpose unknown; 17 nicer side lists in layout
  and manuscript, especially the blue links; 18 applying a header (ترويسة) to all pages uses the same x% on every page
  although the lengths differ; 28 line number on hover over a line in the processed image of the processing page must
  appear on the right (not left) and above (z-index) so div edges do not eat it.
- **editor-core**: 5 «تحضير الغلاف» in the editor is slow and ugly; edits should stop everything, something seems to be
  loading to show them; 6 changing a paragraph to a chapter title/title: the change happens, the page re-splits after
  a while, then no later paragraph can be edited and even saving a version does not work (many pages; render only the
  current page or its neighbours; optimisation matters); 7 an indicator between an edit and its refresh, like Google
  Docs; 21 autosave seems to save several versions at once (check): on entry save one main version, checkpoints later;
  8 offline overlay: many things depend on the API, show a "no internet" cover and block work offline.
- **backend**: 16 make sure Tesseract is current; 20 hide/turn off «استخراج النص من الكتاب» at book creation (weak on
  Arabic, OCR only for now); 29 tasks like ocr.tasks.read_numbers run after processing ended: starting processing must
  never start another run (a second click on re-recognise queues one); re-processing / full re-recognition for super
  admin only; 30 database indexes: check what exists, add what is missing.
- **books-home**: 19 the books home page is boring, not professional; wants something creative and beautiful.
- **manuscript**: 12 on the manuscript page the type of a heading or poetry etc. cannot be set: needs detail;
  22 option to remove footnotes when converting to a manuscript (manuscript settings).

## Wave 2 (after wave 1, editor files)
- **editor-features**: 11 full text options in the editor (e.g. heading directions); 23 subtitle default alignment right,
  not centre, unless a style says otherwise (main title centre); 24 the review-changes list in the editor is ugly;
  25 add a page and a page break in the editor (like Word), removable; 26 an empty paragraph made with Enter is soon
  deleted; 27 a slider in the page list to jump first to last page (or bottom-left).
- **org-fonts-templates**: 9 add fonts at organisation level; 10 templates at organisation level containing font styles
  and measurements.

## Asked back
- 31 clip a region of the original in the editor and insert it as an image (or a layout option): unclear if wanted.
