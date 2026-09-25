"""Digits experiment, step 2: Tesseract on every number crop, in several set-ups.

Variants (name → language, page mode, whitelist, upscale):
    app            the word Tesseract gave on the page run (stored on the token; nothing re-run)
    base_x1/_x3    ara+eng, single line, no whitelist (what the app does, but on the crop)
    digits_x1/_x3  ara, single line, only digits allowed (Arabic-Indic + Western)
    digits_x3_w    ara, single word (psm 8), only digits
    context_x3     ara, single line, digits + the marks and letters that sit beside numbers
    fas_x3         Persian model (its digits share most shapes), only digits

Writes results/tesseract.json: {variant: {crop id: text}}.
Run from the project root:  .venv/bin/python playground/digits/run_tesseract.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytesseract
from PIL import Image, ImageOps

HERE = Path(__file__).resolve().parent
ARABIC_INDIC = "٠١٢٣٤٥٦٧٨٩"
PERSIAN = "۰۱۲۳۴۵۶۷۸۹"
WESTERN = "0123456789"
CONTEXT = "()[]-–/.،,:هـمصجطحز "

VARIANTS: dict[str, dict] = {
    "base_x1": {"lang": "ara+eng", "psm": 7, "whitelist": None, "scale": 1},
    "base_x3": {"lang": "ara+eng", "psm": 7, "whitelist": None, "scale": 3},
    "digits_x1": {"lang": "ara", "psm": 7, "whitelist": ARABIC_INDIC + WESTERN, "scale": 1},
    "digits_x3": {"lang": "ara", "psm": 7, "whitelist": ARABIC_INDIC + WESTERN, "scale": 3},
    "digits_x3_w": {"lang": "ara", "psm": 8, "whitelist": ARABIC_INDIC + WESTERN, "scale": 3},
    "context_x3": {"lang": "ara", "psm": 7, "whitelist": ARABIC_INDIC + WESTERN + CONTEXT, "scale": 3},
    "fas_x3": {"lang": "fas", "psm": 7, "whitelist": PERSIAN + ARABIC_INDIC + WESTERN, "scale": 3},
}


def prepare(path: Path, scale: int) -> Image.Image:
    img = Image.open(path).convert("L")
    if scale != 1:
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    return ImageOps.expand(img, border=12 * scale, fill=255)  # Tesseract likes a white margin


def read(img: Image.Image, cfg: dict) -> str:
    config = f"--oem 1 --psm {cfg['psm']}"
    if cfg["whitelist"]:
        config += f" -c tessedit_char_whitelist={cfg['whitelist'].replace(' ', '')}"
    return pytesseract.image_to_string(img, lang=cfg["lang"], config=config).strip()


def main() -> None:
    manifest = json.loads((HERE / "manifest.json").read_text())
    out: dict[str, dict[str, str]] = {"app": {m["id"][:3]: m.get("tess") or "" for m in manifest}}
    for name, cfg in VARIANTS.items():
        t0 = time.time()
        out[name] = {}
        for m in manifest:
            img = prepare(HERE / "crops" / f"{m['id']}.png", cfg["scale"])
            out[name][m["id"][:3]] = read(img, cfg)
        print(f"{name}: {len(manifest)} crops in {time.time() - t0:.1f} s")
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "tesseract.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
