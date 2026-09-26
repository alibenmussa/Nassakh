import os, sys, django
sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
django.setup()
import numpy as np, cv2
from PIL import Image
from processing import pipeline as P
from processing.models import Preprocess

def candidates(gray, lines, med_h, thick_factor=0.5):
    h, w = gray.shape
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kx = max(15, int(P.RULE_CLOSE_FRAC * w)) | 1
    closed = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (kx, 1)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
    strict = max(3.0, 0.3 * med_h) if med_h else 3.0
    loose = max(3.0, thick_factor * med_h) if med_h else 3.0
    min_width = max(20, int(P.RULE_MIN_WIDTH_FRAC * P._text_block_width(lines, w)))
    out = []
    for i in range(1, n):
        x, y, cw, ch, area = (int(v) for v in stats[i])
        if cw < min_width or y < 0.4 * h or ch > max(12, 0.06 * h):
            continue
        t = area / cw
        if t > loose:
            continue
        y0, y1 = y, y + ch
        below = [ln for ln in lines if ln["y0"] >= y1 - 2]
        above = [ln for ln in lines if ln["y1"] <= y0 + 2]
        if not (below and above) or y1 >= 0.97 * h:
            continue
        # fill ratio: a rule fills its bbox
        fill = area / max(1, cw * ch)
        out.append({"y": (y0 + y1)//2, "x0": x, "x1": x + cw, "t": round(t, 1), "strict": t <= strict, "fill": round(fill, 2)})
    return out

books = [int(a) for a in sys.argv[1:]]
for bid in books:
    tot = acc = near = 0
    rows = []
    for pre in Preprocess.objects.select_related("page").filter(page__book_id=bid, page__is_excluded=False).order_by("page__number"):
        path = pre.gray_image.path
        gray = np.array(Image.open(path).convert("L"))
        lines = pre.line_boxes or []
        c = candidates(gray, lines, pre.median_line_height)
        tot += 1
        strict = [x for x in c if x["strict"]]
        nm = [x for x in c if not x["strict"]]
        if pre.footnote_rule_y is not None: acc += 1
        if nm and pre.footnote_rule_y is None: near += 1
        rows.append((pre.page.number, pre.footnote_rule_y, [(x["y"], x["x1"]-x["x0"], x["t"], x["fill"]) for x in nm]))
    print(f"book {bid}: pages {tot}, rule accepted {acc}, near-miss only {near}")
    for r in rows:
        if r[2]: print("   ", r)
