import os, sys, django
sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
django.setup()
from books.models import Page
b, n = int(sys.argv[1]), int(sys.argv[2])
page = Page.objects.get(book_id=b, number=n)
for run in page.ocr_runs.order_by("created_at"):
    if run.engine_name in ("qari_v02", "qari_v03") and run.region and run.region.kind in ("body", "footnote"):
        print(run.id, run.engine_name, run.region.kind, "looped", run.looped, "finish", run.finish, "tokens", run.output_tokens, "sanity", run.params.get("sanity"))
        print("   ", run.parsed_text[:600].replace("\n", " ⏎ "))
        print("   ...", run.parsed_text[-300:].replace("\n", " ⏎ "))
