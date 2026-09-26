import os, sys, django
sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
django.setup()
from books.models import Page
from ocr.services import _collect_region_texts
from ocr.alignment import align_tokens, is_word
from core.arabic import normalize
from features import split_punct
def runs(p, s):
    pp = [x for x, _ in split_punct(p)]; sp = [x for x, _ in split_punct(s)]
    out = []; cur = []; anchor = 0
    for i, j in align_tokens(pp, sp, key=lambda t: normalize(t, "lenient") or ("\x00" + t)):
        if i is None and j is not None:
            cur.append(sp[j])
        else:
            if cur and any(is_word(w) for w in cur): out.append((anchor, cur))
            cur = []
            if i is not None: anchor = i + 1
    if cur and any(is_word(w) for w in cur): out.append((anchor, cur))
    return pp, out
for b in [int(x) for x in sys.argv[1].split(",")]:
    for page in Page.objects.filter(book_id=b, is_excluded=False, status__in=("reviewed","assembled")).order_by("number"):
        final = normalize(page.final_text, "lenient")
        for rt in _collect_region_texts(page):
            if not rt.alt_text: continue
            pp, rs = runs(rt.text.split(), rt.alt_text.split())
            for at, words in rs:
                w = " ".join(words)
                ctx_b = " ".join(pp[max(0, at-4):at]); ctx_a = " ".join(pp[at:at+3])
                # is the run in the approved text between its context?
                probe = normalize(" ".join(pp[max(0, at-2):at] + words + pp[at:at+1]), "lenient")
                inside = probe in final
                print(b, page.number, rt.target.kind, "| +", w, "| ctx:", ctx_b, "⟨+⟩", ctx_a, "| in approved text:", inside)
