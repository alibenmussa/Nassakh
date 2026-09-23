### Headline (lenient CER, 17 pages, whole page unless noted)

| engine / input | median CER | pages < 10% | pages 10–30% | pages > 30% | mean s/page |
|---|---|---|---|---|---|
| Qari v0.3 / gray | **0.038** | 11 | 5 | 1 | 30 |
| Qari v0.2 / gray | 0.148 | 7 | 6 | 4 | 40 |
| Qari v0.2 / regions (8 pages) | 0.039 | 6 | 2 | 0 | 17 |
| KITAB / gray | 0.163 | 7 | 2 | 8 | 13 |
| Tesseract / bw | 0.089 | 10 | 6 | 1 | 0.5 |

Best single page results are 0.7–1.3% (v0.2 and v0.3 on clean 1966 print and on the footnote book). The worst are total failures, and the failures decide the averages.

### What the grid showed

1. **Accuracy vs reliability.** Qari v0.2 is the most accurate model when it works (0.007–0.045 on 9 pages) but looped or derailed on 9 of 17 whole pages. Qari v0.3 is the most reliable (one failure, the edge-strip page) with a median of 3.8%. KITAB is all-or-nothing: 8 pages near-perfect (it even read the edge-strip page at 2.2%), 8 pages **empty** (one token, immediate end-of-text). Tesseract is never brilliant and never collapses.
2. **Region OCR fixes most loops.** Splitting a page at the footnote rule and sending body and footnotes separately cut v0.2's loops from 9/17 to 1/8 and gave the best mean of the whole grid (6.7%). It also reads footnotes better (small type, 2x upscaled). Loops still happen occasionally (v0.3 footnotes on one page), so loop detection stays.
3. **Input variant.** Grayscale for Qari: Sauvola black-and-white destroyed v0.2 (0.69 vs 0.38). Black-and-white for Tesseract (0.107 vs 0.129). 2x upscaling of low-resolution pages is erratic (huge gains and huge losses on the same pages across engines) and is not adopted as a default.
4. **Failure modes are detectable.** (a) loops: repeated tail, token cap hit; (b) empty output; (c) garbage caused by a strip of the facing page at the scan edge (s1_p018: both Qari models produced nonsense, KITAB and Tesseract were fine); (d) silent omissions: v0.2 skipped a whole paragraph on two pages, v0.3 skipped the French footnotes on another. All four are visible by comparing word counts and word overlap with Tesseract's output, which costs 0.5 s.
5. **An ensemble beats every single engine.** Picking the better of v0.3 and Tesseract per page gives a mean of 7.2%; the better of v0.2 and v0.3 on region pages gives 3.8%, against 6.7–13% for the best single engine. Word-level agreement among three engines is therefore a strong confidence signal, and a 2-of-3 vote is worth trying in Phase 3.
6. **Line geometry.** Qari models return no usable line breaks (v0.2 one paragraph; v0.3 tags do not follow lines). Tesseract's line count matches the detected image lines within 1.4 lines on average (8/17 exact), and it provides word boxes. Review-screen linking will anchor Qari words to Tesseract word boxes.
7. **Digits are the weakest point for every model.** Years and footnote numbers are frequently wrong (v0.3 wrote 1871 three times for 1883/1884; v0.2 read ٢٠ for ٢٤; footnote page numbers wrong in most engines). Digits should be highlighted for review by default.
8. **Latin in Arabic pages.** v0.2 keeps inline Latin words (Henri Duveyrier, Imperialism, Asbystae); v0.3 dropped a French footnote block; Tesseract with `ara` alone garbles Latin and needs `ara+eng` (or `fra`).
9. **v0.3 structure tags are noise on real scans**: random bold/italic/underline, arbitrary heading levels. Only its paragraph breaks carry some information. Layout comes from the guide lines, as planned.
10. **Born-digital PDFs.** The repaired text layer is near-perfect; OCR scores 13% on those pages only because the table cells are read in a different order (word F1 0.99). Use the text layer, skip OCR, keep OCR as a cross-check.
11. **Diacritics.** Raw and no-tashkeel CER are nearly identical: these books carry little tashkeel. The one vocalised proverb was read partially right by v0.2.
12. **Speed on the M5 Pro (PyTorch MPS).** v0.3 about 30 s per page, v0.2 20–40 s, regions 17 s, Tesseract 0.5 s. An 800-page book through v0.3 + v0.2 regions + Tesseract is roughly 10 hours unattended on this Mac. The MLX backend comparison is still to be run (`make models-mlx`, `make mlx`).

### Decisions for Phase 2

- **Engines**: Qari v0.3 as primary text, Qari v0.2 as second opinion and alternative text, Tesseract (`ara+eng`, Sauvola input) always run for geometry, sanity check and fallback. **KITAB dropped** (empty output on half the pages).
- **Region OCR** driven by the book's guide lines (body, footnotes at 2x); grayscale input for Qari.
- **Automatic failure handling**: loop truncation, empty/short output and low overlap with Tesseract trigger a retry with the other Qari model, then fall back to Tesseract text; the page is flagged for review.
- **Confidence**: any word where the three engines disagree, and every digit, is marked low-confidence.
- **Preprocessing**: remove narrow ink strips at the page edge that belong to the facing page before OCR.
- **Born-digital**: text-layer repair path instead of OCR.
- **Not adopted**: 2x upscaling, black-and-white input for Qari, v0.3 formatting tags as layout.
