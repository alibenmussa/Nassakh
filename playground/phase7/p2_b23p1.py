import os, sys, django
sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
django.setup()
from books.models import Book, Page
from ocr.models import Line, OcrRun
from review.models import LineRevision
page = Page.objects.get(book_id=23, number=1)
pre = page.preprocess
print("page", page.id, page.status, "flags", page.attention_flags, "size", pre.output_width, pre.output_height, "n_lines(detected)", pre.n_lines, "median_h", pre.median_line_height, "rule", pre.footnote_rule_y, "block", pre.footnote_block_y)
print("bands:", len(pre.line_boxes))
for b in pre.line_boxes:
    print("  band", b)
print("regions:", [(r.id, r.kind, r.bbox) for r in page.regions.order_by("order")])
for run in page.ocr_runs.order_by("created_at"):
    print("RUN", run.id, run.engine_name, run.input_variant, run.region.kind if run.region else None, "status", run.status, "looped", run.looped, "sanity", (run.params or {}).get("sanity"), "rescue", (run.params or {}).get("rescue"))
    print("   text:", run.parsed_text[:3000].replace("\n", " ⏎ "))
    if run.engine_name == "tesseract":
        for ln in run.params.get("lines") or []:
            print("     tline", ln.get("bbox"), "rescued" if ln.get("rescued") else "", " ".join(w.get("text","") for w in ln.get("words") or []))
print("LINES now:")
for line in page.lines.order_by("order"):
    print(line.order, line.id, "manual" if line.is_manual else "", line.role, line.bbox, "|", line.text, "|| ocr:", line.ocr_text)
print("REVISIONS:")
for r in page.revisions.order_by("created_at"):
    b = (r.before or {}); a = (r.after or {})
    print(r.id, r.action, r.undone, r.created_at.strftime("%H:%M:%S"), "line", r.line_id, "|", str((b or {}).get("text") if isinstance(b, dict) else "")[:120], "=>", str((a or {}).get("text") if isinstance(a, dict) else "")[:120])
