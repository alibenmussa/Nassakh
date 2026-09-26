"""Verse candidates from line geometry + rhyme (read-only probe)."""
import os, sys, django, re, statistics
sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
django.setup()
from books.models import Page
from core.arabic import strip_tashkeel
AR = re.compile(r"[ء-ي]")
def rhyme(text):
    words = [w for w in strip_tashkeel(text or "").split() if AR.search(w)]
    if not words: return ""
    w = re.sub(r"[^ء-ي]", "", words[-1])
    w = w.rstrip("اى") or w   # alif of ithlaq / maqsura
    return w[-1:] if w else ""
def classify(box, L, R, M):
    x0, y0, x1, y1 = box
    w = (x1 - x0) / M
    if w > 0.68 or w < 0.2: return None
    right = (R - x1) / M < 0.08
    left = (x0 - L) / M < 0.08
    centred = abs(((x0 + x1) / 2 - (L + R) / 2) / M) < 0.08
    if right and not left: return "R"
    if left and not right: return "L"
    if centred: return "C"
    return None
for page in Page.objects.filter(is_excluded=False, book_id__in=[int(x) for x in sys.argv[1].split(",")]).order_by("book_id", "number"):
    lines = [l for l in page.lines.select_related("region").order_by("order") if l.bbox and (not l.region or l.region.kind != "footnote")]
    wide = [l.bbox for l in lines if l.bbox[2] - l.bbox[0] > 0]
    if len(wide) < 4: continue
    widest = max(b[2] - b[0] for b in wide)
    ref = [b for b in wide if b[2] - b[0] >= 0.6 * widest]
    L = statistics.median(b[0] for b in ref); R = statistics.median(b[2] for b in ref); M = R - L
    kinds = [classify(l.bbox, L, R, M) for l in lines]
    # staggered pairs R then L
    i = 0; runs = []
    while i < len(lines) - 1:
        j = i; pairs = []
        while j + 1 < len(lines) and kinds[j] == "R" and kinds[j + 1] == "L":
            pairs.append((lines[j], lines[j + 1])); j += 2
        if len(pairs) >= 2:
            rh = [rhyme(b.text) for a, b in pairs]
            top = max(set(rh), key=rh.count)
            runs.append((len(pairs), rh.count(top) / len(rh), top, pairs[0][0].text[:30]))
            i = j
        else:
            i += 1
    # centred runs
    c_runs = []
    i = 0
    while i < len(lines):
        j = i
        while j < len(lines) and kinds[j] == "C": j += 1
        if j - i >= 4:
            texts = [l.text for l in lines[i:j]]
            ends = [rhyme(t) for t in texts[1::2]]
            top = max(set(ends), key=ends.count) if ends else ""
            c_runs.append((j - i, round(ends.count(top) / max(1, len(ends)), 2), top, texts[0][:30]))
        i = max(j, i + 1)
    if runs or c_runs:
        print(page.book_id, page.number, "staggered:", runs, "centred:", c_runs)
