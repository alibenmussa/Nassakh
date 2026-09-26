"""Phase 7 probe (read-only): signals of text the primary model dropped, and what reviewers added.

Per approved page and OCR region, recompute from the stored runs (no model call):
  S1 secondary-only runs: words the secondary read where the primary has nothing (align on words);
  S2 Tesseract lines whose words the primary barely matched (tess_words >= 4, matched share < 0.35);
  S3 under-full lines: a line box as wide as the text block holding far fewer words than its width holds;
  S4 printed bands (Preprocess.line_boxes) that no line box covers.
and compare with the reviewer's additions: words added inside lines, and inserted lines.
"""
from __future__ import annotations

import difflib
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
import django  # noqa: E402

django.setup()

from books.models import Page  # noqa: E402
from ocr.alignment import align_tokens, build_lines, is_word, norm_token  # noqa: E402
from ocr.services import _collect_region_texts, _page_geometry  # noqa: E402
from measure import original_lines  # noqa: E402


def secondary_only_runs(p: list[str], s: list[str], min_words=1):
    """Runs of secondary tokens with no primary counterpart: [(start index in primary, words)]."""
    runs = []
    cur: list[str] = []
    anchor = 0
    for i, j in align_tokens(p, s):
        if i is None and j is not None:
            cur.append(s[j])
        else:
            if cur:
                words = [w for w in cur if is_word(w)]
                if len(words) >= min_words:
                    runs.append((anchor, cur))
                cur = []
            if i is not None:
                anchor = i + 1
    if cur and len([w for w in cur if is_word(w)]) >= min_words:
        runs.append((anchor, cur))
    return runs


def added_by_reviewer(page):
    """Words the reviewer added: [(line id or None, words)], plus inserted lines."""
    added = []
    for lid, orig, final, manual, line in original_lines(page):
        if orig is None:
            added.append(("inserted", [t["t"] for t in final or []]))
            continue
        ot = [str(t.get("t") or "") for t in orig]
        ft = [str(t.get("t") or "") for t in (final or [])]
        sm = difflib.SequenceMatcher(None, ot, ft, autojunk=False)
        for tag, i0, i1, j0, j1 in sm.get_opcodes():
            if tag == "insert" or (tag == "replace" and (j1 - j0) > (i1 - i0)):
                extra = ft[j0:j1] if tag == "insert" else ft[j0:j1]
                if any(is_word(w) for w in extra):
                    added.append((lid, extra))
    return added


def main(book_ids):
    tally = Counter()
    report = []
    for page in Page.objects.filter(book_id__in=book_ids, is_excluded=False, status__in=("reviewed", "assembled", "ocr_done")).order_by("book_id", "number"):
        approved = page.status in ("reviewed", "assembled")
        try:
            rts = _collect_region_texts(page)
        except Exception as exc:  # noqa: BLE001
            continue
        bands, gray = _page_geometry(page)
        s1 = []
        s2 = []
        s3 = []
        for rt in rts:
            p = rt.text.split()
            s = (rt.alt_text or "").split()
            if s:
                for at, words in secondary_only_runs(p, s):
                    s1.append((rt.target.kind, at, " ".join(words), len([w for w in words if is_word(w)])))
            if rt.tess_lines:
                built = build_lines(rt.text, rt.alt_text, rt.tess_lines, bands, gray)
                counts = [sum(1 for t in b["tokens"] if is_word(t["t"])) for b in built]
                widths = [(b["bbox"][2] - b["bbox"][0]) if b.get("bbox") else 0 for b in built]
                dens = sorted(c / w for c, w in zip(counts, widths) if w and c >= 3)
                density = dens[len(dens) // 2] if len(dens) >= 4 else 0
                for b, c, w in zip(built, counts, widths):
                    tw, tm = int(b.get("tess_words") or 0), int(b.get("tess_matched") or 0)
                    if tw >= 4 and tm < 0.35 * tw:
                        s2.append((rt.target.kind, b["order"], b["text"], tw, tm))
                    if density and w and c + 3 <= 0.5 * density * w:
                        s3.append((rt.target.kind, b["order"], b["text"], c, round(density * w, 1)))
        # S4: bands no current line covers (current lines, reviewed or not)
        boxes = [ln.bbox for ln in page.lines.all() if ln.bbox]
        s4 = []
        for band in bands or []:
            cy = (band["y0"] + band["y1"]) / 2
            if not any(b[1] <= cy <= b[3] and min(b[2], band["x1"]) > max(b[0], band["x0"]) for b in boxes):
                s4.append((band["y0"], band["y1"], band["x0"], band["x1"]))
        added = added_by_reviewer(page) if approved else []
        tally["pages"] += 1
        tally["pages_approved"] += approved
        tally["s1_runs"] += len(s1)
        tally["s1_runs_3plus"] += sum(1 for x in s1 if x[3] >= 3)
        tally["s2_lines"] += len(s2)
        tally["s3_lines"] += len(s3)
        tally["s4_bands"] += len(s4)
        if s1 or s2 or s3 or s4 or added:
            report.append({"book": page.book_id, "page": page.number, "approved": approved, "s1": s1, "s2": s2, "s3": s3, "s4": s4, "added": added})
    print(tally)
    out = os.path.join(os.path.dirname(__file__), "dropped_%s.json" % "_".join(map(str, book_ids)))
    json.dump(report, open(out, "w"), ensure_ascii=False, indent=1)
    print("->", out)


if __name__ == "__main__":
    main([int(x) for x in sys.argv[1].split(",")])
