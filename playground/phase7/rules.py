"""Footnote rule / block detection re-run on stored gray images with parameter variants (read-only)."""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
import django  # noqa: E402

django.setup()
import cv2  # noqa: E402
import numpy as np  # noqa: E402

from assembly.pipeline import NOTE_MARKER  # noqa: E402
from books.models import Page  # noqa: E402
from core.images import load_gray  # noqa: E402
from processing import pipeline as pp  # noqa: E402


def rule_candidates(gray, lines, med_h, thick_ratio):
    h, w = gray.shape
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kx = max(15, int(pp.RULE_CLOSE_FRAC * w)) | 1
    closed = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (kx, 1)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
    max_thickness = max(3.0, thick_ratio * med_h) if med_h else 3.0
    min_width = max(20, int(pp.RULE_MIN_WIDTH_FRAC * pp._text_block_width(lines, w)))
    best = None
    for i in range(1, n):
        x, y, cw, ch, area = (int(v) for v in stats[i])
        if cw < min_width or y < 0.4 * h or ch > max(12, 0.06 * h):
            continue
        if area / cw > max_thickness:
            continue
        # a rule is a solid bar: most of its box is ink
        fill = area / max(1, cw * ch)
        y0, y1 = y, y + ch
        below = [ln for ln in lines if ln["y0"] >= y1 - 2]
        above = [ln for ln in lines if ln["y1"] <= y0 + 2]
        if not (below and above) or y1 >= 0.97 * h:
            continue
        if best is None or cw > best[1]:
            best = ((y0 + y1) // 2, cw, round(area / cw, 1), round(fill, 2), x)
    return best


def main(book_ids):
    for page in Page.objects.filter(book_id__in=book_ids, is_excluded=False).select_related("preprocess").order_by("book_id", "number"):
        pre = page.preprocess
        try:
            with pre.gray_image.open("rb") as fh:
                gray = load_gray(fh)
        except Exception:  # noqa: BLE001
            continue
        min_area = max(4, int(gray.size * 1e-5))
        ink = pp.binarize_clean(gray, min_area=min_area)
        lines, med_h = pp.detect_lines(ink)
        r3 = rule_candidates(gray, lines, med_h, 0.3)
        r5 = rule_candidates(gray, lines, med_h, 0.5)
        r6 = rule_candidates(gray, lines, med_h, 0.6)
        # truth proxy: a line starting with a note marker in the lower 45 % of the page
        marker_lines = [
            (ln.bbox[1] if ln.bbox else None, ln.text[:40])
            for ln in page.lines.order_by("order")
            if NOTE_MARKER.match(ln.text or "") and ln.bbox and ln.bbox[1] > 0.55 * pre.output_height
        ]
        stored = pre.footnote_rule_y
        changed = (r3 is None) != (r5 is None) or (r5 is None) != (r6 is None)
        if changed or marker_lines:
            print(f"b{page.book_id} p{page.number}: stored={stored} t0.3={r3} t0.5={r5} t0.6={r6} block={pre.footnote_block_y} markers={marker_lines[:2]}")


if __name__ == "__main__":
    main([int(x) for x in sys.argv[1].split(",")])
