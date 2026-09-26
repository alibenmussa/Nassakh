import os, sys, django
sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
django.setup()
import numpy as np
from books.models import Page
from core.images import load_gray
from ocr.alignment import ink_columns, _blank_runs
res = []
for page in Page.objects.filter(is_excluded=False, book_id__in=[int(x) for x in sys.argv[1].split(",")]).select_related("preprocess").order_by("book_id", "number"):
    pre = page.preprocess
    try:
        with pre.gray_image.open("rb") as fh: gray = load_gray(fh)
    except Exception: continue
    bands = sorted(pre.line_boxes or [], key=lambda b: b["y0"])
    if len(bands) < 3: continue
    L = int(np.median([b["x0"] for b in bands])); R = int(np.median([b["x1"] for b in bands])); M = max(1, R - L)
    for i, b in enumerate(bands):
        h = b["y1"] - b["y0"]
        prof = ink_columns(gray, (b["y0"] - h, b["y1"] + h), L, R)
        runs = [(s, e) for s, e in _blank_runs(prof) if s > 0 and e < len(prof)]
        if not runs: continue
        s, e = max(runs, key=lambda r: r[1] - r[0])
        width = (e - s) / M; centre = ((s + e) / 2) / M
        res.append((page.book_id, page.number, i, round(width, 3), round(centre, 2), b["x0"] - L, R - b["x1"]))
import collections
by = collections.defaultdict(list)
for r in res:
    if r[3] >= 0.05 and 0.3 <= r[4] <= 0.7 and r[5] < 0.1 * 2000 and r[6] < 0.1 * 2000:
        by[(r[0], r[1])].append(r)
for k, v in sorted(by.items()):
    if len(v) >= 2: print(k, len(v), [x[2:5] for x in v][:12])
