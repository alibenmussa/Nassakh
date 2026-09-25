"""Digits experiment: PaddleOCR's Arabic text-line recogniser on every number crop (its own environment).

Recognition only (each crop is one line): PaddleOCR 3's `TextRecognition` with the Arabic models it
offers (PP-OCRv3 mobile, and PP-OCRv5 mobile when the installed version has it). Variants: model ×
scale (x1, x2). Writes results/paddle.json: {variant: {crop id: text}} and results/paddle_conf.json.

    playground/digits/.venv-paddle/bin/python playground/digits/run_paddle.py
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageOps

HERE = Path(__file__).resolve().parent
MODEL_NAMES = ("arabic_PP-OCRv3_mobile_rec", "arabic_PP-OCRv5_mobile_rec")
SCALES = (1, 2)


def prepare(path: Path, scale: int, folder: Path) -> Path:
    img = Image.open(path).convert("RGB")
    if scale != 1:
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    img = ImageOps.expand(img, border=6 * scale, fill=(255, 255, 255))
    out = folder / f"{path.stem}-x{scale}.png"
    img.save(out)
    return out


def main() -> None:
    from paddleocr import TextRecognition

    manifest = json.loads((HERE / "manifest.json").read_text())
    out: dict[str, dict[str, str]] = {}
    conf: dict[str, dict[str, float]] = {}
    with tempfile.TemporaryDirectory(prefix="nassakh-paddle-") as tmp:
        folder = Path(tmp)
        for name in MODEL_NAMES:
            try:
                t0 = time.time()
                model = TextRecognition(model_name=name)
                print(f"{name}: loaded in {time.time() - t0:.1f} s", flush=True)
            except Exception as exc:  # noqa: BLE001 - a model this version does not offer is skipped
                print(f"{name}: not available ({type(exc).__name__}: {str(exc)[:120]})", flush=True)
                continue
            for scale in SCALES:
                variant = f"{name.replace('_mobile_rec', '')}_x{scale}"
                out[variant], conf[variant] = {}, {}
                t0 = time.time()
                for m in manifest:
                    image = prepare(HERE / "crops" / f"{m['id']}.png", scale, folder)
                    result = next(iter(model.predict(input=str(image), batch_size=1)))
                    data = result.json.get("res", result.json) if hasattr(result, "json") else dict(result)
                    out[variant][m["id"][:3]] = str(data.get("rec_text", "")).strip()
                    conf[variant][m["id"][:3]] = round(float(data.get("rec_score", 0.0)), 3)
                print(f"{variant}: {len(manifest)} crops in {time.time() - t0:.1f} s", flush=True)
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "paddle.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    (HERE / "results" / "paddle_conf.json").write_text(json.dumps(conf, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
