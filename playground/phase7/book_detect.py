import os, sys, django, statistics
sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
django.setup()
from books.models import Book, Page
from processing.models import Region, Preprocess
ids = [int(a) for a in sys.argv[1:]] or [25]
for bid in ids:
    b = Book.objects.get(pk=bid)
    print("== book", bid, b.title[:50], b.status)
    for p in b.pages.order_by("number").select_related("preprocess"):
        pre = getattr(p, "preprocess", None)
        if pre is None:
            print(p.number, "no preprocess"); continue
        lb = pre.line_boxes or []
        hs = [l["y1"]-l["y0"] for l in lb]
        med = statistics.median(hs) if hs else 0
        tail = [(l["y0"], l["y1"]-l["y0"], l["x1"]-l["x0"]) for l in lb[-4:]]
        regs = [(r.kind, r.bbox) for r in p.regions.order_by("order")]
        print(f"p{p.number} {p.status} ex={p.is_excluded} W={pre.output_width} H={pre.output_height} lines={pre.n_lines} medh={med} rule={pre.footnote_rule_y} block={pre.footnote_block_y} pn={pre.page_number_box} flags={p.attention_flags}")
        print("   tail(y0,h,w):", tail)
        print("   regions:", regs)
