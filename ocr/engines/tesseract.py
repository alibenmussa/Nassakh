"""Tesseract through pytesseract: plain text plus line and word boxes with confidences.

Boxes are `[x0, y0, x1, y1]` in the pixel space of the image given to `recognize`; the service
offsets them into gray-image coordinates when it OCRs a region crop.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from pathlib import Path

from django.conf import settings

from core.arabic import normalize_ws

from .base import OcrEngine, OcrResult

log = logging.getLogger(__name__)


class TesseractEngine(OcrEngine):
    """CPU engine used for the fast provisional text, geometry and the sanity reference (D14, D16)."""

    name = "tesseract"
    backend = "cpu"
    kind = "classic"

    def __init__(self, langs: str | None = None, psm: int = 4, oem: int = 1):
        self.langs = langs or settings.NASSAKH["TESSERACT_LANGS"]
        self.psm = psm
        self.oem = oem
        self._version = ""

    @property
    def config(self) -> str:
        return f"--oem {self.oem} --psm {self.psm}"

    def load(self) -> None:
        """Check the binary and the language packs once; raises when a language is missing."""
        if self._version:
            return
        import pytesseract

        available = set(pytesseract.get_languages(config=""))
        missing = [lang for lang in self.langs.split("+") if lang not in available]
        if missing:
            raise RuntimeError(f"tesseract language(s) missing: {missing}; run `brew install tesseract-lang`")
        self._version = str(pytesseract.get_tesseract_version())
        log.info("tesseract %s langs=%s psm=%s", self._version, self.langs, self.psm)

    @property
    def model_id(self) -> str:
        return f"tesseract:{self.langs}"

    @property
    def model_revision(self) -> str:
        return self._version

    def recognize(self, image_path: str | Path, max_new_tokens: int | None = None) -> OcrResult:
        import pytesseract
        from PIL import Image

        with Image.open(image_path) as img:
            img.load()
            size = img.size
            t0 = time.time()
            data = pytesseract.image_to_data(
                img, lang=self.langs, config=self.config, output_type=pytesseract.Output.DICT
            )
            duration = time.time() - t0
        text, lines = parse_image_to_data(data)
        return OcrResult(
            text=text,
            duration_s=duration,
            finish="n/a",
            extra={
                "lines": lines,
                "psm": self.psm,
                "oem": self.oem,
                "langs": self.langs,
                "image_size": list(size),
            },
        )


def parse_image_to_data(data: dict) -> tuple[str, list[dict]]:
    """Turn `pytesseract.image_to_data(..., output_type=DICT)` into `(text, lines)`.

    Lines are `{"bbox": [x0, y0, x1, y1], "words": [{"text", "bbox", "conf"}]}` grouped by Tesseract's
    (block, paragraph, line) numbers in reading order; empty words and layout-only rows are dropped.
    `text` has one line per detected line and a blank line between paragraphs.
    """
    n = len(data.get("text", []))
    grouped: dict[tuple[int, int, int], list[dict]] = defaultdict(list)
    for i in range(n):
        word = str(data["text"][i]).strip()
        if not word:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        x, y = int(data["left"][i]), int(data["top"][i])
        w, h = int(data["width"][i]), int(data["height"][i])
        key = (int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))
        grouped[key].append({"text": word, "bbox": [x, y, x + w, y + h], "conf": conf})

    lines: list[dict] = []
    text_lines: list[str] = []
    previous_par: tuple[int, int] | None = None
    for key in sorted(grouped):
        words = grouped[key]
        # Arabic reading order: right to left within a line.
        words.sort(key=lambda w: -w["bbox"][2])
        bbox = [
            min(w["bbox"][0] for w in words),
            min(w["bbox"][1] for w in words),
            max(w["bbox"][2] for w in words),
            max(w["bbox"][3] for w in words),
        ]
        lines.append({"bbox": bbox, "words": words})
        par = key[:2]
        if previous_par is not None and par != previous_par and text_lines:
            text_lines.append("")
        previous_par = par
        text_lines.append(" ".join(w["text"] for w in words))
    text = "\n".join(text_lines)
    # keep paragraph breaks (double newline) but strip stray whitespace inside lines
    text = "\n".join(normalize_ws(part) if part else "" for part in text.split("\n"))
    return text.strip("\n"), lines
