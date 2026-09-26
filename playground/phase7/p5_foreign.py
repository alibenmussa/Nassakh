import os, sys, django, re, unicodedata
sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
django.setup()
from collections import Counter
from ocr.models import Line, OcrRun
def script(ch):
    try: name = unicodedata.name(ch)
    except ValueError: return "UNNAMED"
    return name.split(" ")[0]
ALLOWED_SCRIPTS = {"ARABIC", "LATIN", "DIGIT", "SPACE"}
c = Counter(); ex = {}
for line in Line.objects.all().only("tokens", "page_id").iterator():
    for tok in line.tokens or []:
        t = tok.get("t") or ""
        for ch in t:
            if ch.isalpha() or unicodedata.category(ch).startswith(("S", "Lo", "Lm", "No")):
                s = script(ch)
                if s in ALLOWED_SCRIPTS: continue
                cat = unicodedata.category(ch)
                key = (s, cat)
                c[key] += 1
                ex.setdefault(key, set())
                if len(ex[key]) < 6: ex[key].add((t, tok.get("conf"), tok.get("alt")))
for k, v in c.most_common(40): print(v, k, list(ex[k])[:6])
