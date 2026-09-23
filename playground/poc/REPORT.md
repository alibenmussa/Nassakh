# Phase 1 OCR report
_generated 2026-09-23 23:39_

Runs: 13 total, 13 ok, 0 error. Pages with ground truth: 3/17. Scored runs: 2.

## 1. Accuracy by engine, input variant and backend

CER = character error rate (lower is better). `raw` counts diacritic errors; `no_tashkeel` ignores diacritics; `lenient` also folds alef/ya/ta-marbuta, digits and punctuation.

| engine | variant | backend | runs | scored | CER raw | CER no tashkeel | CER lenient | WER lenient | word F1 | mean s/page | loops |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qari_v02 | gray | torch | 6 | 1 | 0.136 | 0.138 | 0.133 | 0.124 | 0.993 | 32.3 | 2 |
| qari_v02 | regions | torch | 1 | 0 |  |  |  |  |  | 12.7 | 0 |
| qari_v03 | gray | torch | 6 | 1 | 0.134 | 0.134 | 0.128 | 0.124 | 0.991 | 32.4 | 1 |

`word F1` ignores word order (fair for tables and reordered blocks); 1.0 = every word present.

## 2. Accuracy per sample (best variant per engine, lenient CER)

| sample | what | engine | best variant | CER lenient | CER raw | pages |
|---|---|---|---|---|---|---|
| s3 | born-digital Word PDF with tables; text layer has reversed lam-alef | qari_v02 | gray | 0.133 | 0.136 | 1 |
| s3 | born-digital Word PDF with tables; text layer has reversed lam-alef | qari_v03 | gray | 0.128 | 0.134 | 1 |

## 3. Per page for the best combination: qari_v03 / gray / torch

| page | CER raw | CER no tashkeel | CER lenient | WER lenient | word F1 | s | loop |
|---|---|---|---|---|---|---|---|
| s3_p001 | 0.134 | 0.134 | 0.128 | 0.124 | 0.991 | 21.1 | no |

## 6. Line alignment: OCR line count vs detected image lines

Qari v0.2 returns the page as one paragraph and v0.3's tags do not follow visual lines, so this table mostly documents that line linking in the review screen must come from image geometry, not from OCR line breaks.

| engine | variant | pages | exact match | mean |diff| |
|---|---|---|---|---|
| qari_v02 | gray | 6 | 0 | 24.7 |
| qari_v02 | regions | 1 | 0 | 18 |
| qari_v03 | gray | 6 | 0 | 20.5 |

## 7. Grid coverage

| engine | backend | runs | ok | errors | pages |
|---|---|---|---|---|---|
| qari_v02 | torch | 7 | 7 | 0 | 6 |
| qari_v03 | torch | 6 | 6 | 0 | 6 |

## 8. Notes

_Qualitative observations are added here by hand after reading the outputs (structure tags of v0.3, footnote text, numerals, headers, marginalia)._
