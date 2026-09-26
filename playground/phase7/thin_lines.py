import os, sys, django, statistics
sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
django.setup()
from books.models import Book, Page
from processing.models import Preprocess
from collections import defaultdict
res = defaultdict(lambda: [0,0,0,0,0])  # pages, with_rule, cand_no_rule, cand_with_rule_match, cand_with_rule_nomatch
examples = defaultdict(list)
for pre in Preprocess.objects.select_related("page").filter(page__is_excluded=False):
    p = pre.page
    lb = pre.line_boxes or []
    r = res[p.book_id]; r[0] += 1
    if len(lb) < 3: continue
    hs = sorted(l["y1"]-l["y0"] for l in lb)
    med = statistics.median(hs)
    x0 = min(l["x0"] for l in lb); x1 = max(l["x1"] for l in lb)
    bw = x1 - x0
    H = pre.output_height
    cands = []
    for i, l in enumerate(lb):
        h = l["y1"]-l["y0"]; w = l["x1"]-l["x0"]
        if h <= 0.6*med and 0.12*bw <= w <= 0.7*bw and l["y0"] >= 0.4*H and i < len(lb)-1:
            # text below
            cands.append((l["y0"], h, w))
    rule = pre.footnote_rule_y if pre.footnote_rule_y is not None else pre.footnote_block_y
    if rule is not None:
        r[1] += 1
        if cands:
            if any(abs(c[0]-rule) < 3*med for c in cands): r[3] += 1
            else: r[4] += 1
    elif cands:
        r[2] += 1
        examples[p.book_id].append((p.number, cands[:2]))
print("book pages with_rule cand_no_rule cand_match_rule cand_other")
for b, v in sorted(res.items()):
    print(b, v, examples[b][:4])
