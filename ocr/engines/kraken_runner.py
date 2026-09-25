"""Kraken line reader, run as a separate process with Kraken's own Python (D50).

Kraken 6 pins torch <= 2.9 and numpy 2.0 while the project runs torch 2.14, so it lives in its own
environment (`make kraken` → `.venv-kraken/`) and the project talks to it through this script:

    <kraken python> ocr/engines/kraken_runner.py < request.json > response.json

request:  {"model": "<.mlmodel path>", "pages": [{"image": "<gray page image>",
           "lines": [{"id": <any>, "bbox": [x0, y0, x1, y1]}]}]}
response: {"ok": true, "model": "<file name>", "seconds": <float>,
           "lines": [{"id": <as given>, "text": "<the line, logical order>",
                      "chars": [[<char>, x0, x1, <confidence 0–1>], ...]}]}
          or {"ok": false, "error": "<message>"}

Each line is cut from the page with a margin, read as one line (a straight baseline at 75 % of the
crop, the crop as its boundary, as the OpenITI / Jawi model cards do) and its characters come back
in logical (reading) order, BiDi base direction right-to-left, with their horizontal extent in page
pixels. Nothing here imports Django or the project; the pure helpers are tested from the project.
"""

from __future__ import annotations

import json
import os
import sys
import time

# Run as a script, Python puts this folder first on the path, and it holds the project's own
# `kraken.py` (the engine that calls this runner): drop it so `import kraken` finds the Kraken package.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != _HERE]

MARGIN = 0.3  # of the line box height, kept around the line
BASELINE = 0.75  # the baseline's height in the crop, from the top


def padded(bbox: list[int], width: int, height: int) -> tuple[int, int, int, int]:
    """The line box with a margin of `MARGIN` × its height, clamped to the page."""
    x0, y0, x1, y1 = (int(v) for v in bbox)
    pad = max(4, int(MARGIN * max(1, y1 - y0)))
    return max(0, x0 - pad), max(0, y0 - pad), min(width, x1 + pad), min(height, y1 + pad)


def char_rows(prediction: str, cuts, confidences, dx: int) -> list[list]:
    """`[char, x0, x1, confidence]` per character, x in page pixels (the crop starts at `dx`)."""
    rows: list[list] = []
    for i, char in enumerate(prediction):
        polygon = cuts[i] if cuts is not None and i < len(cuts) else None
        xs = [float(point[0]) for point in polygon] if polygon else []
        conf = float(confidences[i]) if confidences is not None and i < len(confidences) else 0.0
        if xs:
            rows.append([char, round(min(xs) + dx, 1), round(max(xs) + dx, 1), round(conf, 3)])
        else:
            rows.append([char, None, None, round(conf, 3)])
    return rows


def _segmentation(width: int, height: int):
    from kraken.containers import BaselineLine, Segmentation

    y = int(height * BASELINE)
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


def read(request: dict) -> dict:
    from kraken import rpred
    from kraken.lib import models
    from PIL import Image

    started = time.time()
    net = models.load_any(request["model"], device="cpu")
    out: list[dict] = []
    for page in request.get("pages") or []:
        with Image.open(page["image"]) as img:
            img = img.convert("L")
            for line in page.get("lines") or []:
                box = padded(line["bbox"], img.width, img.height)
                crop = img.crop(box)
                if crop.width < 4 or crop.height < 4:
                    out.append({"id": line.get("id"), "text": "", "chars": []})
                    continue
                record = next(iter(rpred.rpred(net, crop, _segmentation(*crop.size), bidi_reordering="R")))
                prediction = record.prediction or ""
                rows = char_rows(prediction, record.cuts, record.confidences, box[0])
                out.append({"id": line.get("id"), "text": prediction, "chars": rows})
    name = str(request["model"]).rsplit("/", 1)[-1]
    return {"ok": True, "model": name, "seconds": round(time.time() - started, 3), "lines": out}


def main() -> int:
    try:
        request = json.loads(sys.stdin.read() or "{}")
        response = read(request)
    except Exception as exc:  # noqa: BLE001 - reported to the caller as JSON
        response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(response, ensure_ascii=False))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
