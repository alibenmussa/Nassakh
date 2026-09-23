# Phase 1 — OCR proof of concept

Scripts only, no Django. Goal: pick the OCR model(s), the input image variant
and the Mac inference backend with measurements on the four sample books.

All commands run from this folder. `make` targets wrap the Python scripts;
`../../.venv/bin/python` is used, so no virtualenv activation is needed.

## 0. One-time setup

```bash
make install            # Python packages into ../../.venv (already done once)
brew install tesseract-lang   # Arabic data for the Tesseract baseline
```

## 1. Pages and preprocessing (fast, CPU)

```bash
make pages    # extract 17 pages from input/*.pdf  -> pages/, pages/manifest.json
make prep     # deskew, clean, crop, lines        -> gray/, bw/, gray_2x/, overlays/
make repair   # sample 3 text layer, lam-alef fix -> gt/drafts/s3_*.txt
```

Look at `overlays/` after `make prep`: green boxes are detected lines, the red
line is the footnote rule, `*_split.png` shows where a two-page sheet was cut.

## 2. Models (downloads ~9 GB, once)

```bash
make models          # base + v0.3 + two adapters, merges adapters -> models/
make models-mlx      # additionally convert the merged models to MLX format
make selftest        # 10 MB download; runs a tiny random model on MPS to check the pipeline
```

## 3. OCR runs (the slow part, resumable)

```bash
make quick                       # 6 pages, gray + regions, v0.2 + v0.3  (~10 min)
make full                        # the whole grid, PyTorch on MPS         (~1-2 h)
MAX_MINUTES=60 make full         # work 1 hour, stop cleanly, continue later
make mlx ENGINE=qari_v02         # same pages through MLX for the speed/accuracy comparison
make status                      # show the grid and what is done / pending / error
```

Image variants: `gray` (cleaned page), `bw` (Sauvola), `gray_2x` (low-res pages
upscaled), `regions` (body and footnotes OCR'd separately, footnotes at 2x; only
pages where a footnote rule was found). `regions` exists because whole-page runs
tend to loop on footnote lists; the run JSON then carries `looped: true` and the
repeated tail is cut before scoring. `python run_ocr.py --repetition-penalty 1.1`
adds a penalised variant (`gray+rp1.1`) for comparison.

Stopping: press Ctrl+C at any time, or set `MAX_MINUTES`. Every finished page
is already saved in `runs/<run_id>.json`; the run in progress is lost and
redone next time. Re-run the same command to continue. Runs that ended in an
error are skipped until you pass `RETRY=1`.

## 4. Ground truth and report

```bash
make drafts                      # gt/drafts/<page>.txt from the best run per page
# correct the drafts by reading the scans, then:
make promote                     # copy every draft to gt/<page>.txt
make report                      # REPORT.md + runs_summary.csv
```

The report works before ground truth exists (timing, coverage, line counts);
accuracy tables fill in as pages are promoted.

## Files

| Path | What |
|---|---|
| `config.py` | page selection, engines, prompts, variants, generation parameters |
| `pages/manifest.json` | one entry per page: source PDF, page, half, size, dpi, split position |
| `pages/<id>.prep.json` | preprocessing parameters and detected line boxes for the page |
| `runs/<id>__<engine>__<variant>__<backend>.json` | one OCR run: model, revision, prompt, raw output, duration, loop flag, region parts |
| `regions/<id>_body.png`, `<id>_foot.png` | page split at the footnote rule (footnotes upscaled 2x) |
| `gt/<id>.txt` | ground truth used by the evaluation |
| `REPORT.md` | generated comparison tables |
