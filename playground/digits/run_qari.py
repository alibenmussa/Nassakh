"""Digits experiment, step 4: Qari v0.3 and v0.2 on every number crop (the app's MLX engines, its prompt).

Variants per model: x1 (the crop as is; the processor enlarges small images to its minimum pixel count)
and x4 (Lanczos 4x, as the app does for the page-number vote), a white margin around both, at most
24 new tokens. One model is loaded at a time. Writes results/qari.json: {variant: {crop id: text}}.

Run from the project root:  .venv/bin/python playground/digits/run_qari.py [qari_v03 qari_v02]
"""

from __future__ import annotations

import gc
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")

import django  # noqa: E402

django.setup()

from PIL import Image, ImageOps  # noqa: E402

from core.arabic import parse_output  # noqa: E402
from ocr.engines.registry import build_engine  # noqa: E402

SCALES = (1, 4)
MAX_TOKENS = 24


def prepared(path: Path, scale: int, folder: Path) -> Path:
    img = Image.open(path).convert("L")
    if scale != 1:
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    img = ImageOps.expand(img, border=8 * scale, fill=255)
    out = folder / f"{path.stem}-x{scale}.png"
    img.save(out)
    return out


def main() -> None:
    names = sys.argv[1:] or ["qari_v03", "qari_v02"]
    manifest = json.loads((HERE / "manifest.json").read_text())
    target = HERE / "results" / "qari.json"
    out: dict[str, dict[str, str]] = json.loads(target.read_text()) if target.exists() else {}
    with tempfile.TemporaryDirectory(prefix="nassakh-digits-") as tmp:
        folder = Path(tmp)
        for name in names:
            engine = build_engine(name)
            t0 = time.time()
            engine.load()
            print(f"{name}: loaded in {time.time() - t0:.0f} s", flush=True)
            for scale in SCALES:
                variant = f"{name}_x{scale}"
                table: dict[str, str] = {}
                t0 = time.time()
                for m in manifest:
                    image = prepared(HERE / "crops" / f"{m['id']}.png", scale, folder)
                    result = engine.recognize(image, max_new_tokens=MAX_TOKENS)
                    text, _ = parse_output(result.text)
                    table[m["id"][:3]] = text.strip()
                out[variant] = table
                target.parent.mkdir(exist_ok=True)
                target.write_text(json.dumps(out, ensure_ascii=False, indent=1))
                print(f"{variant}: {len(manifest)} crops in {time.time() - t0:.0f} s", flush=True)
            engine.unload() if hasattr(engine, "unload") else None
            del engine
            gc.collect()


if __name__ == "__main__":
    main()
