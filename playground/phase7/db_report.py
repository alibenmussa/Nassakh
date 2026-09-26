import os, sys, django
sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
django.setup()
from collections import Counter
from django.db.models import Count
from books.models import Book, Page
from processing.models import Region, LayoutGuides, Preprocess

print("books:", Book.objects.count())
for b in Book.objects.order_by("pk"):
    st = Counter(b.pages.values_list("status", flat=True))
    g = LayoutGuides.objects.filter(book=b).first()
    print(b.pk, b.status, b.pages.count(), dict(st), "guides:", (g.source, g.header_cut, g.footnote_line, g.page_number_zone) if g else None, "|", b.title[:40])
print("region kinds/sources:", Counter(Region.objects.values_list("kind", "source")))
print("pages with guides_override:", Page.objects.exclude(guides_override=None).count())
print("override samples:", list(Page.objects.exclude(guides_override=None).values_list("book_id","number","guides_override")[:10]))
print("pages excluded:", Page.objects.filter(is_excluded=True).count())
# region x-extent: are they always full width?
full = 0; partial = 0
for r in Region.objects.select_related("page__preprocess")[:5000]:
    try:
        w = r.page.preprocess.output_width
    except Exception:
        continue
    x0,y0,x1,y1 = r.bbox
    if x0 == 0 and x1 == w: full += 1
    else: partial += 1
print("regions full-width:", full, "partial:", partial)
# regions per page distribution
per = Counter(Page.objects.annotate(n=Count("regions")).values_list("n", flat=True))
print("regions per page:", sorted(per.items()))
# detection stats
pre = Preprocess.objects.all()
print("preprocess rows:", pre.count(), "with rule:", pre.exclude(footnote_rule_y=None).count(), "with block:", pre.exclude(footnote_block_y=None).count(), "with pn:", pre.exclude(page_number_box=None).count())
print("attention flag counts:", Counter(f for fl in Page.objects.values_list("attention_flags", flat=True) for f in (fl or [])))
