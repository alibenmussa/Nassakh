import os, sys, django
sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
django.setup()
import numpy as np
from books.models import Page
from core.images import load_gray
from processing import pipeline as pp
from assembly.pipeline import NOTE_MARKER
def block_variant(lines, median_h, height, min_lines, one_gap=1.8):
    if len(lines) < 3 or not median_h or height <= 0: return None
    ordered = sorted(lines, key=lambda ln: ln["y0"])
    pitch = float(np.median(np.diff([ln["y0"] for ln in ordered])))
    limit = pp.BLOCK_MAX_SIZE_RATIO * median_h
    i = len(ordered)
    while i > 0 and pp._line_size(ordered[i - 1]) <= limit: i -= 1
    block = ordered[i:]
    if not block or i == 0: return None
    top, above = block[0], ordered[i - 1]
    gap = top["y0"] - above["y1"]
    need_gap = pp.BLOCK_MIN_GAP_PITCH * pitch if len(block) >= 2 else one_gap * pitch
    if len(block) < min_lines: return None
    if gap < need_gap: return None
    if top["y0"] < (1.0 - pp.BLOCK_LOWER_FRAC) * height: return None
    return (top["y0"], len(block), round(gap / pitch, 2), [round(pp._line_size(b) / median_h, 2) for b in block])
for page in Page.objects.filter(book_id__in=[int(x) for x in sys.argv[1].split(",")], is_excluded=False).select_related("preprocess").order_by("book_id", "number"):
    pre = page.preprocess
    try:
        with pre.gray_image.open("rb") as fh: gray = load_gray(fh)
    except Exception: continue
    min_area = max(4, int(gray.size * 1e-5))
    ink = pp.binarize_clean(gray, min_area=min_area)
    lines, med_h = pp.detect_lines(ink)
    sized = pp.measure_line_sizes(ink, lines)
    ts = pp.median_line_size(sized)
    cands = pp.page_number_candidates(ink, lines, ts)
    pn = pp.detect_page_number(cands, gray.shape[1], gray.shape[0], ts)
    pnb = pn["bbox"] if pn else None
    body = [ln for ln in sized if not (pnb and pp._overlaps(ln, pnb))]
    b2 = block_variant(body, ts, gray.shape[0], 2)
    b1 = block_variant(body, ts, gray.shape[0], 1)
    last = sorted(body, key=lambda l: l["y0"])[-3:]
    marker = [ln.text[:30] for ln in page.lines.order_by("order") if NOTE_MARKER.match(ln.text or "") and ln.bbox and ln.bbox[1] > 0.55 * pre.output_height]
    if b1 != b2 or marker:
        print(f"b{page.book_id} p{page.number}: rule={pre.footnote_rule_y} block2={b2} block1={b1} last sizes={[round(pp._line_size(l)/ts,2) for l in last]} markers={marker[:1]}")
