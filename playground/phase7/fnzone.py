"""Footnote zone from Tesseract's lines before the models run (read-only probe).

Per page: the body region's Tesseract lines (stored run), the printed bands, a rule candidate (thin solid bar,
thickness <= 0.5 x core height, fill >= 0.6). A zone starts at line k (lower half of the text block) when every
line from k down is note-like, scored by: rule above (+3), a wide gap above (+2, or +1), a marker «(n)» at the
start of line k (+1) with a matching call in the body above (+2), smaller lines (+1); a list continuing from
above (-3). Accept at >= 4.
"""
from __future__ import annotations

import os
import re
import statistics
import sys

sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
import django  # noqa: E402

django.setup()

from books.models import Page  # noqa: E402
from core.arabic import to_western_digits  # noqa: E402
from core.images import load_gray  # noqa: E402
from ocr.services import _latest_runs, _targets, engine_names  # noqa: E402
from processing import pipeline as pp  # noqa: E402
from rules import rule_candidates  # noqa: E402

MARK = re.compile(r"^\s*[\(\)\[\]]\s*([0-9٠-٩۰-۹]{1,2}|[^\s\(\)\[\]]{1,2})\s*[\(\)\[\]]")  # «(١)», also Tesseract's «(')»
CALL = re.compile(r"[\(\)\[\]]\s*([0-9٠-٩۰-۹]{1,2})\s*[\(\)\[\]]")
LIST = re.compile(r"^\s*[\(\[]?\s*([0-9٠-٩۰-۹]{1,2})\s*[\)\]]?\s*[-–ـ.]")


def text_of(line):
    return " ".join(str(w.get("text") or "") for w in line.get("words") or [])


def zone(lines, h, rule_y):
    lines = [ln for ln in lines if ln.get("bbox")]
    lines.sort(key=lambda ln: ln["bbox"][1])
    if len(lines) < 3:
        return None
    heights = [ln["bbox"][3] - ln["bbox"][1] for ln in lines]
    med_h = statistics.median(heights)
    gaps = [b["bbox"][1] - a["bbox"][3] for a, b in zip(lines, lines[1:])]
    med_gap = statistics.median(gaps) if gaps else 0
    top_block, bottom_block = lines[0]["bbox"][1], lines[-1]["bbox"][3]
    best = None
    for k in range(len(lines) - 1, 0, -1):
        y = lines[k]["bbox"][1]
        if y < top_block + 0.5 * (bottom_block - top_block):
            break
        score, why = 0, []
        prev = lines[k - 1]
        gap = y - prev["bbox"][3]
        if rule_y is not None and prev["bbox"][3] - 5 <= rule_y <= y + 5:
            score += 3; why.append("rule")
        if med_gap and gap >= max(1.5 * med_gap, med_gap + 0.8 * med_h):
            score += 2; why.append("gap")
        elif med_gap and gap >= 1.25 * med_gap:
            score += 1; why.append("gap-")
        t = text_of(lines[k])
        m = MARK.match(t)
        if m:
            score += 1; why.append("marker")
            key = to_western_digits(m.group(1))
            calls = []
            for ln in lines[:k]:
                tl = text_of(ln)
                calls += [mm.group(1) for mm in CALL.finditer(tl) if tl[:mm.start()].strip()]  # not at a line start
            if any(to_western_digits(c) == key for c in calls):
                score += 2; why.append("call")
        zone_h = statistics.median(heights[k:])
        if zone_h <= 0.9 * statistics.median(heights[:k]):
            score += 1; why.append("smaller")
        lm = LIST.match(t)
        if lm and any(LIST.match(text_of(ln)) for ln in lines[max(0, k - 3):k]):
            score -= 3; why.append("list")
        if len(lines) - k >= 3 and "rule" not in why and "smaller" not in why:
            continue  # a long zone needs a rule or smaller type
        if score >= 4 and (best is None or score > best[0]):
            best = (score, k, y, why, t[:40])
    return best


def main(book_ids):
    _, _, fast = engine_names()
    for page in Page.objects.filter(book_id__in=book_ids, is_excluded=False).select_related("preprocess").order_by("book_id", "number"):
        pre = page.preprocess
        try:
            with pre.gray_image.open("rb") as fh:
                gray = load_gray(fh)
        except Exception:  # noqa: BLE001
            continue
        min_area = max(4, int(gray.size * 1e-5))
        ink = pp.binarize_clean(gray, min_area=min_area)
        bands, med = pp.detect_lines(ink)
        cand = rule_candidates(gray, bands, med, 0.5)
        rule_y = cand[0] if cand and (cand[2] <= 0.3 * med or cand[3] >= 0.6) else None
        h, w = pre.output_height, pre.output_width
        lines = []
        has_fn_region = False
        for target in _targets(page, (h, w), ocr_only=True):
            if target.kind == "footnote":
                has_fn_region = True
                continue
            tess = _latest_runs(page, target).get(fast)
            if tess is not None and tess.status == "ok":
                lines.extend(tess.params.get("lines") or [])
        z = zone(lines, h, rule_y)
        print(f"b{page.book_id} p{page.number}: fn_region={has_fn_region} rule={rule_y} zone={z}")


if __name__ == "__main__":
    main([int(x) for x in sys.argv[1].split(",")])
