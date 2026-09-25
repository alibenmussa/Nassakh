"""Digits experiment, step 5: Qari on the whole line around each number, enlarged.

The page run gives Qari its context but shrinks the page to the model's pixel budget; a lone number
crop has the pixels but no context (and the page prompt then misfires). This reads the full line that
holds the number (its box from the OCR, a margin around it) at 2x, so the model sees the words around
the number with more pixels per digit. Writes results/qari_lines.json: {variant: {crop id: line text}}.
Scored on the numbers found (the line holds other numbers too, so crop exact does not apply).

Run from the project root:  .venv/bin/python playground/digits/run_qari_lines.py [qari_v03 qari_v02]
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
from ocr.models import Line  # noqa: E402

SCALE = 2
MAX_TOKENS = 160


def line_images(manifest: list[dict], folder: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    lines = {
        line.pk: line
        for line in Line.objects.filter(pk__in={m["line"] for m in manifest}).select_related(
            "page__preprocess"
        )
    }
    for m in manifest:
        line = lines.get(m["line"])
        if line is None or not line.bbox:
            continue
        path = folder / f"line-{line.pk}.png"
        out[m["id"][:3]] = path
        if path.exists():
            continue
        x0, y0, x1, y1 = line.bbox
        pad = max(6, int(0.3 * (y1 - y0)))
        with Image.open(line.page.preprocess.gray_image.path) as img:
            img = img.convert("L")
            crop = img.crop(
                (max(0, x0 - pad), max(0, y0 - pad), min(img.width, x1 + pad), min(img.height, y1 + pad))
            )
        crop = crop.resize((crop.width * SCALE, crop.height * SCALE), Image.LANCZOS)
        ImageOps.expand(crop, border=10 * SCALE, fill=255).save(path)
    return out


def main() -> None:
    names = sys.argv[1:] or ["qari_v03", "qari_v02"]
    manifest = json.loads((HERE / "manifest.json").read_text())
    target = HERE / "results" / "qari_lines.json"
    out: dict[str, dict[str, str]] = json.loads(target.read_text()) if target.exists() else {}
    with tempfile.TemporaryDirectory(prefix="nassakh-digit-lines-") as tmp:
        images = line_images(manifest, Path(tmp))
        for name in names:
            engine = build_engine(name)
            engine.load()
            variant = f"{name}_line_x{SCALE}"
            table: dict[str, str] = {}
            t0 = time.time()
            done: dict[Path, str] = {}  # several numbers can share a line
            for key, path in images.items():
                if path not in done:
                    result = engine.recognize(path, max_new_tokens=MAX_TOKENS)
                    done[path] = parse_output(result.text)[0].strip()
                table[key] = done[path]
            out[variant] = table
            target.parent.mkdir(exist_ok=True)
            target.write_text(json.dumps(out, ensure_ascii=False, indent=1))
            print(f"{variant}: {len(done)} lines in {time.time() - t0:.0f} s", flush=True)
            del engine
            gc.collect()


if __name__ == "__main__":
    main()
