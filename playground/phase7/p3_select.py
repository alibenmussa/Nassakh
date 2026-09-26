import os, sys, django
sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
django.setup()
from collections import Counter
from books.models import Page
from ocr.services import _collect_region_texts
tally = Counter()
for page in Page.objects.filter(book_id__in=[int(x) for x in sys.argv[1].split(",")], is_excluded=False, status__in=("ocr_done","reviewed","assembled")).order_by("book_id","number"):
    try:
        rts = _collect_region_texts(page)
    except Exception as e:
        print("err", page.pk, e); continue
    for rt in rts:
        has_alt = bool(rt.alt_text)
        key = (rt.target.kind, rt.source, has_alt, rt.reason.split(" ")[0][:40])
        tally[key] += 1
        if not has_alt and rt.target.kind in ("body", "page"):
            print(page.book_id, page.number, rt.target.kind, "source", rt.source, "reason", rt.reason, "words", len(rt.text.split()), "n_unres", page.n_unresolved)
for k, v in sorted(tally.items(), key=lambda kv: -kv[1]): print(v, k)
