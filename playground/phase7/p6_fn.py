import os, sys, django
sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
django.setup()
from books.models import Page
from processing.services import page_layout
for page in Page.objects.filter(book_id=int(sys.argv[1]), is_excluded=False).order_by("number"):
    pre = page.preprocess
    lay = page_layout(page, pre)
    print(f"p{page.number} h={pre.output_height} rule={pre.footnote_rule_y} block={pre.footnote_block_y} src={lay.footnote_source} med_h={pre.median_line_height} regions={[(r.kind, r.bbox) for r in page.regions.order_by('order')]}")
    lines = list(page.lines.select_related('region').order_by("order"))
    for line in lines[-8:]:
        print(f"    {line.order:2d} {line.region.kind if line.region else '-':9s} {line.bbox} {line.text[:90]}")
    bands = sorted(pre.line_boxes, key=lambda b: b['y0'])
    print("    bands(last 8):", [(b['y0'], b['y1'], b['x0'], b['x1'], b.get('size')) for b in bands[-8:]])
