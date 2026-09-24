"""Tesseract through pytesseract: plain text plus line and word boxes with confidences.

Boxes are `[x0, y0, x1, y1]` in the pixel space of the image given to `recognize`; the service
offsets them into gray-image coordinates when it OCRs a region crop.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from pathlib import Path

from django.conf import settings

from core.arabic import normalize_ws

from .base import OcrEngine, OcrResult

log = logging.getLogger(__name__)

# Word kinds for `reading_order`: Arabic letters or Arabic-Indic digits read right to left; Latin
# letters and Western digits left to right; anything else (punctuation) is neutral.
_RTL_CHARS = re.compile(
    r"[\u0621-\u064A\u0660-\u0669\u066E-\u06D3\u06F0-\u06F9\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFC]"
)
_LTR_CHARS = re.compile(r"[A-Za-z0-9\u00C0-\u024F]")
ORDER_TOLERANCE = 3  # pixels of overlap below which a box counts as wholly beside another


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

    def recognize(
        self, image_path: str | Path, max_new_tokens: int | None = None, hints: dict | None = None
    ) -> OcrResult:
        import pytesseract
        from PIL import Image

        psm = int((hints or {}).get("psm") or self.psm)
        config = f"--oem {self.oem} --psm {psm}"
        with Image.open(image_path) as img:
            img.load()
            size = img.size
            t0 = time.time()
            data = pytesseract.image_to_data(
                img, lang=self.langs, config=config, output_type=pytesseract.Output.DICT
            )
            duration = time.time() - t0
        text, lines = parse_image_to_data(data)
        return OcrResult(
            text=text,
            duration_s=duration,
            finish="n/a",
            extra={
                "lines": lines,
                "psm": psm,
                "oem": self.oem,
                "langs": self.langs,
                "image_size": list(size),
            },
        )


def parse_image_to_data(data: dict) -> tuple[str, list[dict]]:
    """Turn `pytesseract.image_to_data(..., output_type=DICT)` into `(text, lines)`.

    Lines are `{"bbox": [x0, y0, x1, y1], "words": [{"text", "bbox", "conf"}]}` grouped by Tesseract's
    (block, paragraph, line) numbers, words in Tesseract's `word_num` order (logical reading order,
    so Latin runs inside Arabic lines stay left to right); empty words and layout-only rows are dropped.
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
        word_num = int(data["word_num"][i]) if "word_num" in data else i
        grouped[key].append(
            {"text": word, "bbox": [x, y, x + w, y + h], "conf": conf, "_order": (word_num, i)}
        )

    lines: list[dict] = []
    text_lines: list[str] = []
    previous_par: tuple[int, int] | None = None
    for key in sorted(grouped):
        # Tesseract's word_num order is the logical (bidi) reading order: Arabic words right to left
        # with embedded Latin runs left to right (sorting by x would reverse those runs), except on
        # the lines it lays out left to right, which `reading_order` rebuilds from the boxes.
        words = reading_order(
            [
                {k: v for k, v in w.items() if k != "_order"}
                for w in sorted(grouped[key], key=lambda w: w["_order"])
            ]
        )
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


def _kind(word: dict) -> str:
    text = str(word.get("text") or "")
    if _RTL_CHARS.search(text):
        return "rtl"
    return "ltr" if _LTR_CHARS.search(text) else "neutral"


def _centre(word: dict) -> float:
    box = word.get("bbox") or [0, 0, 0, 0]
    return (box[0] + box[2]) / 2


def _span(words: list[dict]) -> tuple[float, float]:
    return min(w["bbox"][0] for w in words), max(w["bbox"][2] for w in words)


def _wholly_right_of(later: tuple[float, float], earlier: tuple[float, float]) -> bool:
    """True when the span `later` starts where `earlier` ends or further right (out of RTL order)."""
    return later[0] >= earlier[1] - ORDER_TOLERANCE


def _runs_right_to_left(words: list[dict]) -> bool:
    """False when the line is visibly laid out left to right: a word of a right-to-left run, or a whole
    run, lies wholly to the right of the one before it. Overlapping boxes are not an inversion."""
    runs: list[tuple[str, list[dict]]] = []
    for word in words:
        kind = _kind(word)
        if kind == "neutral":
            continue
        if runs and runs[-1][0] == kind:
            runs[-1][1].append(word)
        else:
            runs.append((kind, [word]))
    for kind, members in runs:
        if kind == "rtl" and any(
            _wholly_right_of(_span([b]), _span([a])) for a, b in zip(members, members[1:], strict=False)
        ):
            return False
    spans = [_span(members) for _, members in runs]
    return not any(_wholly_right_of(b, a) for a, b in zip(spans, spans[1:], strict=False))


def reading_order(words: list[dict]) -> list[dict]:
    """The words of one Tesseract line in reading order for a right-to-left page.

    Tesseract's order is kept when it reads right to left: Arabic words right to left and the runs of
    Latin words placed right to left among them. Some lines, often those where an Arabic word was
    misread as Latin ("EX,", "Tay"), come back laid out left to right, the end of the line first;
    those are rebuilt from the boxes: every word right to left, then each run of Latin words and
    Western numbers (with the punctuation between them) turned back to left to right.
    """
    if len(words) < 2 or any(not w.get("bbox") for w in words) or _runs_right_to_left(words):
        return words
    visual = sorted(words, key=lambda w: -_centre(w))
    out: list[dict] = []
    i = 0
    while i < len(visual):
        if _kind(visual[i]) == "rtl":
            out.append(visual[i])
            i += 1
            continue
        j = i
        while j < len(visual) and _kind(visual[j]) != "rtl":
            j += 1
        group = visual[i:j]
        a, b = 0, len(group)
        while a < b and _kind(group[a]) == "neutral":
            a += 1
        while b > a and _kind(group[b - 1]) == "neutral":
            b -= 1
        out.extend([*group[:a], *reversed(group[a:b]), *group[b:]])
        i = j
    return out
