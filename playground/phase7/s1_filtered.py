"""S1 runs after the proposed filters (numbers, duplicates, split words, region-start heads)."""
import os, sys
sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh"); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
import django; django.setup()
from collections import Counter
from books.models import Page
from core.arabic import normalize
from ocr.alignment import is_word
from ocr.services import _collect_region_texts
from s1_support import runs_of
def L(t): return normalize(t, "lenient")
def keep(p, at, words, kind):
    ws = [w for w in words if is_word(w)]
    if not ws: return "numbers/punct"
    window = [L(t) for t in p[max(0, at - 12): at + 12]]
    present = sum(1 for w in ws if L(w) in window)
    if present >= 0.6 * len(ws): return "duplicate"
    if len(ws) == 1:
        for t in p[max(0, at - 1): at + 1]:
            lt, lw = L(t), L(ws[0])
            if lw and lt and lw != lt and (lt.startswith(lw) or lt.endswith(lw)): return "split word"
    if at == 0 and len(ws) <= 4 and kind != "footnote": return "region start (head?)"
    return ""
tally = Counter(); kept = []
for page in Page.objects.filter(is_excluded=False, status__in=("ocr_done", "reviewed", "assembled")).order_by("book_id", "number"):
    try: rts = _collect_region_texts(page)
    except Exception: continue
    for rt in rts:
        if not rt.alt_text: continue
        p = rt.text.split()
        for at, words in runs_of(p, rt.alt_text.split()):
            why = keep(p, at, words, rt.target.kind)
            tally[why or "KEPT"] += 1
            if not why:
                nw = sum(1 for w in words if is_word(w))
                tally["kept_words"] += nw
                tally["kept_3plus"] += nw >= 3
                kept.append((page.book_id, page.number, rt.target.kind, " ".join(words)[:80]))
print(tally)
print("pages scanned:", Page.objects.filter(is_excluded=False, status__in=("ocr_done", "reviewed", "assembled")).count())
for k in kept:
    if k[0] in (19, 23, 25, 26, 21, 24): print("  ", k)
