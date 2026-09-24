# Phase 1 OCR report
_generated 2026-09-24 01:47_

Runs: 218 total, 218 ok, 0 error. Pages with ground truth: 17/17. Scored runs: 218.

## 1. Accuracy by engine, input variant and backend

CER = character error rate (lower is better). `raw` counts diacritic errors; `no_tashkeel` ignores diacritics; `lenient` also folds alef/ya/ta-marbuta, digits and punctuation.

| engine | variant | backend | runs | scored | CER raw | CER no tashkeel | CER lenient | WER lenient | word F1 | mean s/page | loops |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qari_kitab | bw | torch | 17 | 17 | 0.660 | 0.659 | 0.653 | 0.683 | 0.357 | 10.2 | 1 |
| qari_kitab | gray | torch | 17 | 17 | 0.473 | 0.471 | 0.463 | 0.506 | 0.525 | 13.2 | 1 |
| qari_kitab | gray_2x | torch | 4 | 4 | 0.359 | 0.355 | 0.348 | 0.418 | 0.635 | 16.6 | 0 |
| qari_kitab | regions | torch | 8 | 8 | 0.602 | 0.601 | 0.607 | 0.631 | 0.447 | 15.3 | 1 |
| qari_v02 | bw | torch | 17 | 17 | 0.880 | 0.849 | 0.694 | 0.943 | 0.667 | 39.8 | 9 |
| qari_v02 | gray | mlx | 17 | 17 | 0.353 | 0.349 | 0.321 | 0.400 | 0.747 | 25.4 | 8 |
| qari_v02 | gray | torch | 17 | 17 | 0.418 | 0.416 | 0.375 | 0.593 | 0.778 | 39.7 | 9 |
| qari_v02 | gray_2x | torch | 4 | 4 | 0.329 | 0.316 | 0.285 | 0.419 | 0.807 | 30.2 | 1 |
| qari_v02 | regions | torch | 8 | 8 | 0.080 | 0.071 | 0.067 | 0.139 | 0.891 | 16.8 | 1 |
| qari_v03 | bw | torch | 17 | 17 | 0.152 | 0.149 | 0.133 | 0.221 | 0.810 | 28.0 | 2 |
| qari_v03 | gray | mlx | 17 | 17 | 0.152 | 0.151 | 0.127 | 0.183 | 0.857 | 20.0 | 3 |
| qari_v03 | gray | torch | 17 | 17 | 0.153 | 0.151 | 0.128 | 0.186 | 0.857 | 30.0 | 3 |
| qari_v03 | gray_2x | torch | 4 | 4 | 0.230 | 0.222 | 0.217 | 0.285 | 0.768 | 44.6 | 1 |
| qari_v03 | regions | torch | 8 | 8 | 0.154 | 0.150 | 0.132 | 0.219 | 0.850 | 26.4 | 1 |
| tesseract | bw | cpu | 17 | 17 | 0.125 | 0.124 | 0.107 | 0.240 | 0.815 | 0.502 | 0 |
| tesseract | gray | cpu | 17 | 17 | 0.146 | 0.144 | 0.129 | 0.250 | 0.811 | 0.564 | 0 |
| tesseract | gray_2x | cpu | 4 | 4 | 0.122 | 0.121 | 0.103 | 0.217 | 0.825 | 0.927 | 0 |
| tesseract | regions | cpu | 8 | 8 | 0.101 | 0.100 | 0.080 | 0.203 | 0.829 | 0.549 | 0 |

`word F1` ignores word order (fair for tables and reordered blocks); 1.0 = every word present.

## 2. Accuracy per sample (best variant per engine, lenient CER)

| sample | what | engine | best variant | CER lenient | CER raw | pages |
|---|---|---|---|---|---|---|
| s1 | 1966 typeset book, ~200 DPI scans, covers on pages 1-4 | qari_kitab | gray | 0.271 | 0.287 | 4 |
| s1 | 1966 typeset book, ~200 DPI scans, covers on pages 1-4 | qari_v02 | gray | 0.340 | 0.351 | 8 |
| s1 | 1966 typeset book, ~200 DPI scans, covers on pages 1-4 | qari_v03 | gray | 0.299 | 0.321 | 8 |
| s1 | 1966 typeset book, ~200 DPI scans, covers on pages 1-4 | tesseract | bw | 0.104 | 0.114 | 4 |
| s2 | two book pages per landscape sheet, footnotes under a rule | qari_kitab | regions | 0.828 | 0.824 | 5 |
| s2 | two book pages per landscape sheet, footnotes under a rule | qari_v02 | regions | 0.080 | 0.087 | 5 |
| s2 | two book pages per landscape sheet, footnotes under a rule | qari_v03 | regions | 0.033 | 0.049 | 5 |
| s2 | two book pages per landscape sheet, footnotes under a rule | tesseract | regions | 0.063 | 0.083 | 5 |
| s3 | born-digital Word PDF with tables; text layer has reversed lam-alef | qari_kitab | bw | 0.166 | 0.176 | 3 |
| s3 | born-digital Word PDF with tables; text layer has reversed lam-alef | qari_v02 | bw | 0.159 | 0.169 | 3 |
| s3 | born-digital Word PDF with tables; text layer has reversed lam-alef | qari_v03 | bw | 0.127 | 0.139 | 3 |
| s3 | born-digital Word PDF with tables; text layer has reversed lam-alef | tesseract | gray | 0.161 | 0.181 | 3 |
| s4 | low-resolution photocopies (~900x1300 px), skew, marginalia, footnotes | qari_kitab | regions | 0.240 | 0.232 | 3 |
| s4 | low-resolution photocopies (~900x1300 px), skew, marginalia, footnotes | qari_v02 | regions | 0.045 | 0.067 | 3 |
| s4 | low-resolution photocopies (~900x1300 px), skew, marginalia, footnotes | qari_v03 | gray | 0.076 | 0.097 | 8 |
| s4 | low-resolution photocopies (~900x1300 px), skew, marginalia, footnotes | tesseract | gray | 0.088 | 0.108 | 4 |

## 3. Per page for the best combination: qari_v02 / regions / torch

| page | CER raw | CER no tashkeel | CER lenient | WER lenient | word F1 | s | loop |
|---|---|---|---|---|---|---|---|
| s2_p003R | 0.028 | 0.023 | 0.013 | 0.048 | 0.958 | 12.7 | no |
| s2_p010L | 0.032 | 0.028 | 0.024 | 0.076 | 0.935 | 12.2 | no |
| s2_p010R | 0.173 | 0.172 | 0.172 | 0.197 | 0.891 | 11.7 | no |
| s2_p015L | 0.033 | 0.028 | 0.019 | 0.077 | 0.931 | 10.7 | no |
| s2_p015R | 0.171 | 0.169 | 0.171 | 0.258 | 0.828 | 30.4 | yes |
| s4_p003 | 0.090 | 0.054 | 0.045 | 0.190 | 0.823 | 19.5 | no |
| s4_p006 | 0.045 | 0.037 | 0.033 | 0.090 | 0.935 | 18.6 | no |
| s4_p016 | 0.065 | 0.058 | 0.057 | 0.177 | 0.832 | 18.2 | no |

## 4. Backend comparison: PyTorch MPS vs MLX (same page, engine, variant)

| page | engine | variant | torch s | mlx s | speed-up | CER torch | CER mlx |
|---|---|---|---|---|---|---|---|
| s1_p006 | qari_v02 | gray | 17.6 | 10.8 | 1.640 | 0.012 | 0.014 |
| s1_p006 | qari_v03 | gray | 26.3 | 17.0 | 1.546 | 0.023 | 0.021 |
| s1_p009 | qari_v02 | gray | 61.5 | 9.370 | 6.565 | 0.274 | 0.181 |
| s1_p009 | qari_v03 | gray | 20.3 | 13.7 | 1.477 | 0.177 | 0.177 |
| s1_p012 | qari_v02 | gray | 14.7 | 10.6 | 1.391 | 0.007 | 0.007 |
| s1_p012 | qari_v03 | gray | 27.5 | 15.7 | 1.752 | 0.007 | 0.006 |
| s1_p018 | qari_v02 | gray | 61.9 | 42.1 | 1.469 | 1.257 | 0.964 |
| s1_p018 | qari_v03 | gray | 62.1 | 41.8 | 1.486 | 0.990 | 0.992 |
| s2_p003L | qari_v02 | gray | 61.9 | 7.750 | 7.990 | 0.093 | 0.042 |
| s2_p003L | qari_v03 | gray | 16.6 | 11.9 | 1.402 | 0.038 | 0.037 |
| s2_p003R | qari_v02 | gray | 62.4 | 43.2 | 1.443 | 0.227 | 0.411 |
| s2_p003R | qari_v03 | gray | 18.5 | 11.2 | 1.648 | 0.015 | 0.016 |
| s2_p010L | qari_v02 | gray | 61.9 | 44.1 | 1.405 | 1.971 | 0.107 |
| s2_p010L | qari_v03 | gray | 19.3 | 13.4 | 1.441 | 0.026 | 0.024 |
| s2_p010R | qari_v02 | gray | 9.430 | 7.690 | 1.226 | 0.148 | 0.155 |
| s2_p010R | qari_v03 | gray | 17.1 | 12.0 | 1.420 | 0.027 | 0.027 |
| s2_p015L | qari_v02 | gray | 9.390 | 7.420 | 1.265 | 0.019 | 0.021 |
| s2_p015L | qari_v03 | gray | 12.5 | 9.390 | 1.328 | 0.018 | 0.018 |
| s2_p015R | qari_v02 | gray | 63.2 | 43.2 | 1.463 | 0.037 | 0.534 |
| s2_p015R | qari_v03 | gray | 62.2 | 41.3 | 1.508 | 0.097 | 0.097 |
| s3_p001 | qari_v02 | gray | 13.0 | 9.980 | 1.307 | 0.133 | 0.131 |
| s3_p001 | qari_v03 | gray | 21.1 | 13.1 | 1.608 | 0.128 | 0.131 |
| s3_p003 | qari_v02 | gray | 11.6 | 42.7 | 0.271 | 0.194 | 0.890 |
| s3_p003 | qari_v03 | gray | 18.3 | 13.2 | 1.394 | 0.166 | 0.143 |
| s3_p004 | qari_v02 | gray | 62.6 | 43.6 | 1.435 | 0.152 | 0.153 |
| s3_p004 | qari_v03 | gray | 16.1 | 12.9 | 1.256 | 0.162 | 0.164 |
| s4_p003 | qari_v02 | gray | 68.0 | 41.0 | 1.659 | 1.281 | 1.284 |
| s4_p003 | qari_v03 | gray | 64.6 | 42.1 | 1.537 | 0.163 | 0.158 |
| s4_p006 | qari_v02 | gray | 17.7 | 13.2 | 1.341 | 0.045 | 0.045 |
| s4_p006 | qari_v03 | gray | 35.6 | 22.9 | 1.555 | 0.034 | 0.051 |
| s4_p011 | qari_v02 | gray | 18.3 | 14.0 | 1.303 | 0.024 | 0.027 |
| s4_p011 | qari_v03 | gray | 36.6 | 24.4 | 1.497 | 0.024 | 0.024 |
| s4_p016 | qari_v02 | gray | 59.3 | 40.6 | 1.461 | 0.496 | 0.496 |
| s4_p016 | qari_v03 | gray | 36.0 | 23.4 | 1.542 | 0.077 | 0.075 |

Mean speed-up: 1.77x; mean CER torch 0.2512659528591797, mlx 0.22418043883124122

## 5. Low-resolution pages: grayscale vs 2x upscaled (lenient CER, negative delta = upscaling helped)

| page | engine | CER gray | CER gray_2x | delta |
|---|---|---|---|---|
| s4_p003 | qari_kitab | 1.000 | 0.125 | -0.875 |
| s4_p003 | qari_v02 | 1.281 | 1.025 | -0.256 |
| s4_p003 | qari_v03 | 0.163 | 0.037 | -0.126 |
| s4_p003 | tesseract | 0.109 | 0.140 | 0.032 |
| s4_p006 | qari_kitab | 0.044 | 0.242 | 0.198 |
| s4_p006 | qari_v02 | 0.045 | 0.041 | -0.005 |
| s4_p006 | qari_v03 | 0.034 | 0.084 | 0.050 |
| s4_p006 | tesseract | 0.035 | 0.099 | 0.063 |
| s4_p011 | qari_kitab | 0.027 | 0.025 | -0.002 |
| s4_p011 | qari_v02 | 0.024 | 0.021 | -0.003 |
| s4_p011 | qari_v03 | 0.024 | 0.697 | 0.672 |
| s4_p011 | tesseract | 0.117 | 0.071 | -0.046 |
| s4_p016 | qari_kitab | 0.077 | 1.000 | 0.923 |
| s4_p016 | qari_v02 | 0.496 | 0.055 | -0.441 |
| s4_p016 | qari_v03 | 0.077 | 0.050 | -0.027 |
| s4_p016 | tesseract | 0.093 | 0.104 | 0.011 |

## 6. Line alignment: OCR line count vs detected image lines

Qari v0.2 returns the page as one paragraph and v0.3's tags do not follow visual lines, so this table mostly documents that line linking in the review screen must come from image geometry, not from OCR line breaks.

| engine | variant | pages | exact match | mean |diff| |
|---|---|---|---|---|
| qari_kitab | bw | 17 | 0 | 21.4 |
| qari_kitab | gray | 17 | 2 | 18.7 |
| qari_kitab | gray_2x | 4 | 0 | 31.2 |
| qari_kitab | regions | 8 | 0 | 21.5 |
| qari_v02 | bw | 17 | 0 | 23.1 |
| qari_v02 | gray | 34 | 0 | 23.1 |
| qari_v02 | gray_2x | 4 | 0 | 32 |
| qari_v02 | regions | 8 | 0 | 21.2 |
| qari_v03 | bw | 17 | 0 | 19.6 |
| qari_v03 | gray | 34 | 0 | 19.1 |
| qari_v03 | gray_2x | 4 | 0 | 27.5 |
| qari_v03 | regions | 8 | 0 | 18.8 |
| tesseract | bw | 17 | 4 | 1.941 |
| tesseract | gray | 17 | 8 | 1.412 |
| tesseract | gray_2x | 4 | 0 | 3 |
| tesseract | regions | 8 | 2 | 1.625 |

## 7. Grid coverage

| engine | backend | runs | ok | errors | pages |
|---|---|---|---|---|---|
| qari_kitab | torch | 46 | 46 | 0 | 17 |
| qari_v02 | mlx | 17 | 17 | 0 | 17 |
| qari_v02 | torch | 46 | 46 | 0 | 17 |
| qari_v03 | mlx | 17 | 17 | 0 | 17 |
| qari_v03 | torch | 46 | 46 | 0 | 17 |
| tesseract | cpu | 46 | 46 | 0 | 17 |

## 8. Findings and decisions

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
