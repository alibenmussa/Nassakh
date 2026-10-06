# Sample book

`alwaraqat-sample.pdf` is a short "scanned" book for trying Nassakh. It has 2 pages and no text layer.

- **Text.** The opening of «الورقات» in أصول الفقه by إمام الحرمين الجويني (died 478 AH). It is a classical text in the public domain. It was typed for this sample. The one footnote is a short editorial note written for the sample.
- **Images.** The page images are generated, not scanned. `scripts/make_sample_pdf.py` sets the text in Amiri (`static/fonts/amiri`, SIL Open Font License) and renders each page at 300 dpi. It adds gray paper, light noise, a little blur and a slight skew, so the page looks like a scan. Each page has a running head, a page number and a rule.

Rebuild the file with:

```sh
.venv/bin/python scripts/make_sample_pdf.py
```

Use it as described in `docs/RUN_LOCALLY.md`, section "Try the sample book".
