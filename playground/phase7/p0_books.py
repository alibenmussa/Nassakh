import os, sys, django
sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
django.setup()
from books.models import Book, Page
from ocr.models import Line, OcrRun
from review.models import LineRevision
from django.db.models import Count, Sum
for b in Book.objects.order_by("id"):
    pages = b.pages.all()
    n = pages.count()
    st = dict(pages.values_list("status").annotate(c=Count("id")))
    lines = Line.objects.filter(page__book=b).count()
    revs = LineRevision.objects.filter(page__book=b).count()
    unres = pages.aggregate(s=Sum("n_unresolved"))["s"]
    print(b.id, repr(b.title[:40]), "pages", n, st, "lines", lines, "revs", revs, "unres", unres, "digit", b.digit_style)
