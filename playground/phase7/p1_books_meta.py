import os, sys, django
sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
django.setup()
from books.models import Book, Page
from processing.models import LayoutGuides, Region
for b in Book.objects.filter(id__in=[11,13,15,17,18,19,20,21,22,23,24,25,26]).order_by("id"):
    try:
        g = b.guides
        gs = f"hdr={g.header_cut} fn={g.footnote_line} pn={g.page_number_zone} src={g.source}"
    except Exception as e:
        gs = "no guides"
    kinds = {}
    for r in Region.objects.filter(page__book=b):
        kinds[r.kind] = kinds.get(r.kind, 0) + 1
    print(b.id, b.title[:50], "|", b.source_pdf.name, "| skip", b.skip_first, b.skip_last, "| sheet", b.pages_per_sheet, "|", gs, "| regions", kinds, "| asm", b.assembly_settings)
