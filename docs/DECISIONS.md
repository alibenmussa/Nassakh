# Decisions

Short records of choices that are not obvious from the code. Newest at the bottom.
Format: date, decision, why, consequences.

## D1 — Celery + Redis, not Django's built-in tasks (2026-09-23)
Django 6 ships `django.tasks` but only immediate/dummy backends, no worker and no queue routing.
The OCR work needs a separate GPU queue with retries. Celery stays.

## D2 — Extract scanned pages at native resolution (2026-09-23)
The sample scans are embedded at 150–200 DPI (sample 4 lower). Rendering at 300 DPI would only
upsample. Pages are rendered at the DPI that reproduces the embedded scan pixel for pixel, which also
honours PDF page rotation (sample 2 sheets are rotated 90°). Born-digital pages render at 300 DPI.

## D3 — Two pages per sheet are split at a detected gutter, right page first (2026-09-23)
Sample 2 has two book pages per landscape sheet. The gutter is the widest low-ink vertical band in the
central 30% of the sheet, bridging narrow dark spikes (fold shadow, staples). In an RTL book the right
half is the earlier page. Position is per book with per-page override in the product.

## D4 — Layout by master guide lines, not full automatic layout analysis (2026-09-23)
Owner request: set the footnote line and the header cut once per book, apply to all pages, adjust per
page. Heuristics only *propose* positions (footnote rule detection, repeated top line). Surya/Kraken
are not used: Surya weights are non-commercial above a revenue threshold, Kraken is heavy.

## D5 — Mac-native, no production target for now (2026-09-23)
Everything runs on the owner's Apple M5 Pro (24 GB). Docker, nginx and uWSGI files are deferred.
Inference backend on Apple Silicon: MLX (`mlx-vlm`) versus PyTorch MPS is decided by the PoC.

## D6 — Digits are always Western (0–9) (2026-09-23)
Owner decision. OCR raw output is stored untouched; assembly converts Arabic-Indic digits by default
and the editor offers a convert action. Diacritics are still never stripped.

## D7 — Qari LoRA adapters are merged manually onto the bf16 Qwen2-VL-2B base (2026-09-23)
Qari v0.2.2.1 and the KITAB checkpoint are LoRA adapters declared against a 4-bit bitsandbytes base
that cannot run on a Mac. `prepare_models.py` downloads `Qwen/Qwen2-VL-2B-Instruct`, applies
`W += (alpha/r) · B·A` per module, matching module names by canonical suffix so it survives the
transformers 4→5 module renames, and saves a full merged model. Training on a 4-bit base and merging
into bf16 is an approximation; the PoC measures whether it matters.

## D8 — Text layers from Word PDFs are repaired with glyph geometry (2026-09-23)
Sample 3's text layer has every lam-alef ligature reversed (الإسلام → اإلسالم). Text alone cannot fix
it because ال is also the article. In PyMuPDF's rawdict the alef that belongs to the ligature has a
zero-width box sitting on the lam's edge, so those pairs are swapped back. Word also emits trailing
commas and justification spaces at the visual line end but mid-stream; neutral glyphs whose pen position
contradicts their stream position are re-inserted by x. Validated against OCR in the PoC.

## D9 — Experiment runs are a folder of JSON files, one per run (2026-09-23)
`runs/<page>__<engine>__<variant>__<backend>.json`, written atomically after each page. This makes the
grid resumable (stop any time, rerun the same command) and keeps every raw output, prompt, model
revision and duration, which is also what the future `OcrRun` model stores.

## D10 — Prompts copied verbatim from the model cards (2026-09-23)
Qari v0.2 and v0.3 share one plain-text prompt; KITAB has its own. Stored in `config.py` and in every
run record, so a prompt change is visible in the data.

## D11 — OCR body and footnotes as separate regions (2026-09-23, from the quick run)
Whole-page Qari runs loop on footnote/reference lists on 3 of 12 quick runs (repeating one line until
the 3000-token cap). A repetition penalty fixed one page and not another, and changed digits. Sending the
body above the footnote rule and the footnote block (upscaled 2x) as two images stopped cleanly on the
same pages and read the footnotes better. The product OCRs per region derived from the guide lines (D4),
and the run store keeps a `looped` flag plus a truncated tail for whole-page runs.

## D12 — Line linking in the review screen comes from image geometry, not OCR line breaks (2026-09-23)
Qari v0.2 returns the page as one paragraph even when asked for line breaks; v0.3's HTML tags do not
follow visual lines. Detected line boxes (projection profile) are reliable, so the review screen will map
OCR words to lines by proportional length or by anchoring to a word-box detector (Tesseract) rather than
counting newlines. To be designed in Phase 3.

## D13 — Metrics include an order-insensitive word F1 (2026-09-23)
On the born-digital sample the OCR reads table cells in a different order than the text layer, which CER
counts as 12% error although the characters are right. The report shows word F1 next to CER so table
pages and looped outputs are judged fairly.
