# Cover spike (2026-09-27, D80)

WeasyPrint 70 places a full-page cover image correctly for the three fits (`spike-all.png`: fill =
`object-fit: cover`, fit width, fit height; `overflow: hidden` clips), and PyMuPDF
`insert_pdf(cover, start_at=0)` keeps the interior's outline and links on their pages. The Arabic
`/PageLabels` prefix must be a UTF-16BE text string (PyMuPDF's `set_page_labels` wrote UTF-8 hex).
See `docs/COVER_SPEC.md`.
