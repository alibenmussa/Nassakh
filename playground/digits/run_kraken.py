"""Digits experiment: Kraken on every number crop (its own environment, not the project's).

Models (models/):
    base   all_arabic_scripts.mlmodel — the OpenITI printed Arabic-script base model (Zenodo 7050270,
           CC0): ~23k Arabic, ~17k Persian, ~11k Urdu, ~7k Ottoman printed lines; 16 MB
    jawi   Jawi-OCR-Kraken-v1 (Hugging Face culturalheritagenus, Apache-2.0): the base model fine-tuned on
           1,800 lines of 1930s–60s Jawi newspapers, "handles Eastern Arabic numerals"; 16 MB
Each crop is one line: a straight baseline at 75 % of its height, the whole crop as its boundary (as the
Jawi model card does). Variants: model × scale (x1, x2) × BiDi base direction of the output (R, L).
Writes results/kraken.json: {variant: {crop id: text}} and results/kraken_conf.json (mean confidence).

    playground/digits/.venv-kraken/bin/python playground/digits/run_kraken.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from kraken import rpred
from kraken.containers import BaselineLine, Segmentation
from kraken.lib import models
from PIL import Image, ImageOps

HERE = Path(__file__).resolve().parent
MODELS = {"base": HERE / "models" / "all_arabic_scripts.mlmodel", "jawi": HERE / "models" / "jawi.mlmodel"}
SCALES = (1, 2)
BIDI = ("R", "L")


def one_line(width: int, height: int) -> Segmentation:
    y = int(height * 0.75)
    line = BaselineLine(
        id="line_0",
        baseline=[(1, y), (width - 1, y)],
        boundary=[(1, 1), (width - 1, 1), (width - 1, height - 1), (1, height - 1), (1, 1)],
        text=None,
        base_dir="R",
    )
    return Segmentation(
        type="baselines",
        imagename="",
        text_direction="horizontal-rl",
        script_detection=False,
        lines=[line],
        regions={},
        line_orders=[],
    )


def prepare(path: Path, scale: int) -> Image.Image:
    img = Image.open(path).convert("L")
    if scale != 1:
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    return ImageOps.expand(img, border=6 * scale, fill=255)


def main() -> None:
    manifest = json.loads((HERE / "manifest.json").read_text())
    out: dict[str, dict[str, str]] = {}
    conf: dict[str, dict[str, float]] = {}
    for name, path in MODELS.items():
        t0 = time.time()
        net = models.load_any(str(path), device="cpu")
        print(f"{name}: loaded in {time.time() - t0:.1f} s ({path.stat().st_size / 1e6:.1f} MB)", flush=True)
        for scale in SCALES:
            for bidi in BIDI:
                variant = f"{name}_x{scale}_{bidi}"
                out[variant], conf[variant] = {}, {}
                t0 = time.time()
                for m in manifest:
                    img = prepare(HERE / "crops" / f"{m['id']}.png", scale)
                    try:
                        record = next(iter(rpred.rpred(net, img, one_line(*img.size), bidi_reordering=bidi)))
                        text = record.prediction
                        cs = list(record.confidences or [])
                        conf[variant][m["id"][:3]] = round(float(sum(cs)) / len(cs), 3) if cs else 0.0
                    except Exception as exc:  # noqa: BLE001 - a crop Kraken cannot read counts as nothing
                        text = ""
                        print(f"  {m['id']}: {type(exc).__name__}: {exc}", flush=True)
                    out[variant][m["id"][:3]] = text.strip()
                print(f"{variant}: {len(manifest)} crops in {time.time() - t0:.1f} s", flush=True)
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "kraken.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    (HERE / "results" / "kraken_conf.json").write_text(json.dumps(conf, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
