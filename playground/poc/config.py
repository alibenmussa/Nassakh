"""Phase 1 proof-of-concept configuration.

Everything the experiment grid depends on lives here: which pages are used,
which OCR engines and prompts, which image variants, and where files go.
Edit this file to change the grid; the scripts never hard-code these values.
"""
from __future__ import annotations

from pathlib import Path

POC = Path(__file__).resolve().parent
INPUT = POC / "input"          # the four sample PDFs (not committed)
PAGES = POC / "pages"          # extracted page images + manifest + preprocessing params
GRAY = POC / "gray"            # cleaned grayscale pages (OCR input)
GRAY_2X = POC / "gray_2x"      # 2x upscaled grayscale for low-resolution pages
REGIONS = POC / "regions"      # <id>_body.png and <id>_foot.png split at the footnote rule
BW = POC / "bw"                # Sauvola black & white (display / comparison)
OVERLAYS = POC / "overlays"    # visual checks: line boxes, footnote rule, gutter split
RUNS = POC / "runs"            # one JSON per OCR run (the resumable store)
GT = POC / "gt"                # ground truth: gt/<page_id>.txt (drafts in gt/drafts/)
MODELS = POC / "models"        # merged / converted model weights (not committed)
TMP = POC / "tmp"
MANIFEST = PAGES / "manifest.json"
REPORT = POC / "REPORT.md"

# --------------------------------------------------------------------------
# Pages. PDF page numbers are 1-based. `pages_per_sheet: 2` splits each sheet
# into a right page (first, RTL) and a left page.
# --------------------------------------------------------------------------
SAMPLES: dict[str, dict] = {
    "s1": {
        "file": "sample 1.pdf",
        "pages": [6, 9, 12, 18],
        "pages_per_sheet": 1,
        "born_digital": False,
        "low_res": False,
        "note": "1966 typeset book, ~200 DPI scans, covers on pages 1-4",
    },
    "s2": {
        "file": "sample 2.pdf",
        "pages": [3, 10, 15],
        "pages_per_sheet": 2,
        "born_digital": False,
        "low_res": False,
        "note": "two book pages per landscape sheet, footnotes under a rule",
    },
    "s3": {
        "file": "sample 3.pdf",
        "pages": [1, 3, 4],
        "pages_per_sheet": 1,
        "born_digital": True,
        "render_dpi": 300,
        "low_res": False,
        "note": "born-digital Word PDF with tables; text layer has reversed lam-alef",
    },
    "s4": {
        "file": "sample 4.pdf",
        "pages": [3, 6, 11, 16],
        "pages_per_sheet": 1,
        "born_digital": False,
        "low_res": True,
        "note": "low-resolution photocopies (~900x1300 px), skew, marginalia, footnotes",
    },
}

# A small subset for a first quick read (about 20-40 minutes on the Mac).
QUICK_PAGES = ["s1_p006", "s2_p003R", "s3_p001", "s4_p003", "s1_p012", "s4_p011"]

# --------------------------------------------------------------------------
# Prompts, copied verbatim from the model cards.
# --------------------------------------------------------------------------
PROMPT_QARI = (
    "Below is the image of one page of a document, as well as some raw textual "
    "content that was previously extracted for it. Just return the plain text "
    "representation of this document as if you were reading it naturally. "
    "Do not hallucinate."
)
PROMPT_KITAB = (
    "Below is the image of one page of a document. Please provide the plain text "
    "representation of this document as if you were reading it naturally, "
    "ensuring high accuracy."
)

# --------------------------------------------------------------------------
# Engines. `adapter: True` means the Hugging Face repo is a LoRA adapter that
# must be merged onto `base` first (prepare_models.py does this).
# --------------------------------------------------------------------------
BASE_MODEL = "Qwen/Qwen2-VL-2B-Instruct"

ENGINES: dict[str, dict] = {
    "qari_v02": {
        "kind": "qwen2vl",
        "hf_id": "NAMAA-Space/Qari-OCR-0.2.2.1-VL-2B-Instruct",
        "adapter": True,
        "base": BASE_MODEL,
        "local": MODELS / "qari-v0.2-merged",
        "mlx": MODELS / "mlx" / "qari-v0.2",
        "prompt": PROMPT_QARI,
    },
    "qari_v03": {
        "kind": "qwen2vl",
        "hf_id": "NAMAA-Space/Qari-OCR-v0.3-VL-2B-Instruct",
        "adapter": False,
        "local": MODELS / "qari-v0.3",
        "mlx": MODELS / "mlx" / "qari-v0.3",
        "prompt": PROMPT_QARI,
    },
    "qari_kitab": {
        "kind": "qwen2vl",
        "hf_id": "FatimahEmadEldin/Qari-OCR-Fine-Tuned-Kitab-Benchmark",
        "adapter": True,
        "base": BASE_MODEL,
        "local": MODELS / "qari-kitab-merged",
        "mlx": MODELS / "mlx" / "qari-kitab",
        "prompt": PROMPT_KITAB,
    },
    "tesseract": {
        "kind": "tesseract",
        "lang": "ara",
        "psm": 4,
    },
}
QWEN_ENGINES = [k for k, v in ENGINES.items() if v["kind"] == "qwen2vl"]
DEFAULT_ENGINES = ["qari_v02", "qari_v03", "qari_kitab", "tesseract"]

# Image variants fed to the engines. gray_2x exists only for low_res pages.
VARIANTS: dict[str, Path] = {"gray": GRAY, "bw": BW, "gray_2x": GRAY_2X, "regions": REGIONS}
DEFAULT_VARIANTS = ["gray", "bw", "regions"]
# The "regions" variant OCRs <id>_body.png and <id>_foot.png (footnotes, 2x) separately
# and joins the texts. It exists only for pages where a footnote rule was found.
REGION_MAX_TOKENS = {"body": 2500, "foot": 1000}

# Generation parameters (recorded in every run JSON).
GEN = {
    # Qwen2-VL turns every 28x28 patch group into one visual token. 2048 tokens
    # is about 1060x1500 px for a book page: enough for footnote-size type
    # while keeping prefill cheap on the Mac.
    "max_pixels": 2048 * 28 * 28,
    "min_pixels": 256 * 28 * 28,
    "max_new_tokens": 3000,
    "repetition_penalty": 1.0,   # 1.0 = off; 1.1 stopped one looping page but changed digits
    "dtype": "bfloat16",
}
DEFAULT_BACKEND = "torch"   # torch (PyTorch on MPS) | mlx (Apple MLX)

# Preprocessing defaults (also recorded per page).
PREP = {
    "max_skew_deg": 5.0,
    "crop_margin_frac": 0.02,
    "nlm_h": 6,
    "sauvola_k": 0.2,
    "upscale_factor": 2,
}
