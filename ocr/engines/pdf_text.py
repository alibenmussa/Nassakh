"""Repaired text layer of born-digital PDFs (D8). Port of `playground/poc/repair_text_layer.py`.

Word-generated PDFs map the lam-alef ligature glyph to two code points in the wrong order, so
"الإسلام" is extracted as "اإلسالم". Text alone cannot fix this ("ال" is also the article), but
glyph geometry can: in PyMuPDF's rawdict the alef that belongs to the ligature has a zero-width
box sitting exactly on the lam's edge. Per page this engine

  1. rebuilds each visual line from rawdict segments that share a baseline, right-to-left,
  2. swaps every zero-width (alef, lam) pair back to lam-alef,
  3. suppresses MuPDF's synthesised spaces and moves spaces / punctuation whose pen position
     contradicts their stream position,
  4. applies NFKC.

`recognize()` takes a `PdfPageRef` (or a path to a PDF, page 0) instead of an image path.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from core.arabic import normalize_ws

from .base import OcrEngine, OcrResult

ALEFS = {"ا": "لا", "أ": "لأ", "إ": "لإ", "آ": "لآ"}


@dataclass(frozen=True)
class PdfPageRef:
    """One PDF page (0-based `index`), optionally one half of a two-page sheet."""

    pdf_path: str
    index: int = 0
    half: str = "full"  # full | right | left
    split_ratio: float = 0.5


def _w(c: dict) -> float:
    return c["bbox"][2] - c["bbox"][0]


def _touches(c1: dict, c2: dict, tol: float = 0.6) -> bool:
    """Do two glyph boxes share a vertical edge (either direction)?"""
    return abs(c1["bbox"][0] - c2["bbox"][2]) <= tol or abs(c2["bbox"][0] - c1["bbox"][2]) <= tol


def _is_neutral(ch: str) -> bool:
    return ch.isspace() or unicodedata.category(ch).startswith("P")


def fix_misplaced(chars: list[dict]) -> list[dict]:
    """Move spaces/punctuation whose pen position contradicts their stream position.

    Word emits a trailing comma or a justification space at the visual end of the line but in the
    middle (or start) of the character stream. Such glyphs have an origin far outside the span of
    their stream neighbours; they are removed and re-inserted where their x position fits (RTL).
    """
    solids = [c for c in chars if not _is_neutral(c["c"])]
    if len(solids) < 2:
        return chars
    widths = sorted(_w(c) for c in solids if _w(c) > 0)
    tol = max(2.0, 0.5 * (widths[len(widths) // 2] if widths else 4.0))
    keep: list[dict] = []
    misplaced: list[dict] = []
    for i, c in enumerate(chars):
        if not _is_neutral(c["c"]):
            keep.append(c)
            continue
        prev = next((chars[j] for j in range(i - 1, -1, -1) if not _is_neutral(chars[j]["c"])), None)
        nxt = next((chars[j] for j in range(i + 1, len(chars)) if not _is_neutral(chars[j]["c"])), None)
        x = c["origin"][0]
        if prev and nxt:
            lo = min(prev["origin"][0], nxt["origin"][0]) - tol
            hi = max(prev["bbox"][2], nxt["bbox"][2]) + tol
            ok = lo <= x <= hi
        else:
            anchor = prev or nxt
            ok = abs(x - anchor["origin"][0]) <= 4 * tol
        (keep if ok else misplaced).append(c)
    for c in misplaced:
        idx = len(keep)
        for j, k in enumerate(keep):
            if not _is_neutral(k["c"]) and k["origin"][0] < c["origin"][0] - 0.5:
                idx = j
                break
        keep.insert(idx, c)
    return keep


def repair_chars(chars: list[dict]) -> tuple[str, int]:
    """Join glyphs into text, swapping reversed lam-alef pairs. Returns (text, n_fixed)."""
    out: list[str] = []
    i, fixes = 0, 0
    while i < len(chars):
        c = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else None
        # zero-width alef followed by lam at the same x -> reversed ligature
        if c["c"] in ALEFS and _w(c) < 0.05 and nxt and nxt["c"] == "ل" and _touches(c, nxt):
            out.append(ALEFS[c["c"]])
            fixes += 1
            i += 2
            continue
        # lam followed by zero-width alef (other emission order)
        if c["c"] == "ل" and nxt and nxt["c"] in ALEFS and _w(nxt) < 0.05 and _touches(c, nxt):
            out.append(ALEFS[nxt["c"]])
            fixes += 1
            i += 2
            continue
        out.append(c["c"])
        i += 1
    return "".join(out), fixes


def visual_lines(block: dict) -> list[list[dict]]:
    """Group rawdict line segments by baseline and order them right-to-left."""
    by_baseline: dict[int, list[dict]] = defaultdict(list)
    for seg in block["lines"]:
        chars = [ch for span in seg["spans"] for ch in span["chars"]]
        if not chars:
            continue
        y = round(chars[0]["origin"][1] / 2.0)  # 2pt tolerance
        solid = [c for c in chars if not c["c"].isspace()] or chars
        by_baseline[y].append({"chars": chars, "solid": solid, "x1": max(c["bbox"][2] for c in solid)})
    lines = []
    for y in sorted(by_baseline):
        segs = sorted(by_baseline[y], key=lambda s: -s["x1"])
        merged: list[dict] = []
        last_solid = None
        for s in segs:
            if last_solid is not None and not _touches(last_solid, s["solid"][0], tol=1.5):
                # separate text objects that do not touch: keep a word boundary
                merged.append({"c": " ", "bbox": (0, 0, 0, 0), "origin": (0, 0)})
            merged.extend(s["chars"])
            last_solid = s["solid"][-1]
        lines.append(merged)
    return lines


def repair_page(page, clip=None) -> tuple[str, dict]:
    """Repaired plain text of a `pymupdf.Page` (optionally only inside `clip`) and repair statistics."""
    import pymupdf

    flags = pymupdf.TEXT_PRESERVE_LIGATURES | pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_INHIBIT_SPACES
    raw = page.get_text("rawdict", flags=flags, clip=clip)
    blocks_out: list[str] = []
    fixes, n_chars = 0, 0
    for block in raw["blocks"]:
        if block.get("type") != 0:
            continue
        lines_out = []
        for chars in visual_lines(block):
            n_chars += len(chars)
            text, f = repair_chars(fix_misplaced(chars))
            fixes += f
            if text.strip():
                lines_out.append(text)
        if lines_out:
            blocks_out.append("\n".join(lines_out))
    text = normalize_ws(unicodedata.normalize("NFKC", "\n\n".join(blocks_out)))
    text = re.sub(r" +([،.:؛!؟,;])", r"\1", text)  # no space before punctuation
    stats = {
        "chars": n_chars,
        "lam_alef_fixed": fixes,
        "suspicious_left": text.count("اإل") + text.count("األ"),
    }
    return text, stats


def half_clip(page_rect, half: str, split_ratio: float):
    """Clip rectangle for the right/left half of a two-page sheet (None for a full page)."""
    if half not in ("right", "left"):
        return None
    import pymupdf

    x_cut = page_rect.x0 + page_rect.width * float(split_ratio)
    if half == "right":
        return pymupdf.Rect(x_cut, page_rect.y0, page_rect.x1, page_rect.y1)
    return pymupdf.Rect(page_rect.x0, page_rect.y0, x_cut, page_rect.y1)


class PdfTextEngine(OcrEngine):
    """Text-layer engine for born-digital books: no model, near-free, exact when the repair holds."""

    name = "pdf_text"
    backend = "pdf"
    kind = "pdf"

    @property
    def model_id(self) -> str:
        return "pymupdf:rawdict-repair"

    @property
    def model_revision(self) -> str:
        try:
            import pymupdf

            return str(pymupdf.VersionBind)
        except Exception:  # noqa: BLE001
            return ""

    def recognize(self, image_path: str | Path | PdfPageRef, max_new_tokens: int | None = None) -> OcrResult:
        """`image_path` is a `PdfPageRef`, a `path.pdf` (page 0) or `path.pdf#<index>`."""
        import pymupdf

        ref = _as_ref(image_path)
        t0 = time.time()
        with pymupdf.open(ref.pdf_path) as doc:
            if not 0 <= ref.index < doc.page_count:
                raise IndexError(f"page {ref.index} outside {ref.pdf_path} ({doc.page_count} pages)")
            page = doc[ref.index]
            clip = half_clip(page.rect, ref.half, ref.split_ratio)
            text, stats = repair_page(page, clip=clip)
        return OcrResult(
            text=text,
            duration_s=time.time() - t0,
            finish="n/a",
            extra={**stats, "pdf_index": ref.index, "half": ref.half},
        )


def _as_ref(source: str | Path | PdfPageRef) -> PdfPageRef:
    if isinstance(source, PdfPageRef):
        return source
    text = str(source)
    if "#" in text:
        path, _, index = text.rpartition("#")
        return PdfPageRef(pdf_path=path, index=int(index))
    return PdfPageRef(pdf_path=text, index=0)
