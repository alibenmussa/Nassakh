# Read-only probe: drift per edited book, block/line claims in the edited documents vs a fresh pipeline run.
from collections import Counter
from books.models import Book
from editor.models import Manuscript
from editor import document as doc
from editor.services import review_drift
from assembly import services as asv, pipeline
for m in Manuscript.objects.select_related('book', 'run').order_by('book_id'):
    b = m.book
    d = review_drift(b, m)
    content = doc.content_of(m.document)
    flat = [n for n in doc.flat_blocks(content[doc.preamble_end(content):])]
    no_src = sum(1 for n in flat if not doc.attrs_of(n).get('sourceLineIds'))
    claims = Counter()
    for n in flat:
        for i in set(doc.attrs_of(n).get('sourceLineIds') or []):
            claims[i] += 1
    shared = sum(1 for i, c in claims.items() if c > 1)
    fresh = asv.preview(b)
    fcontent = doc.content_of(fresh.document)
    fflat = list(doc.flat_blocks(fcontent[doc.preamble_end(fcontent):]))
    flines = set()
    for n in fflat:
        flines |= set(doc.attrs_of(n).get('sourceLineIds') or [])
    mlines = set(claims)
    print(f"book {b.pk:>3} v{m.version:<3} {m.origin:<8} blocks {len(flat):>4} fresh {len(fflat):>4} no_src {no_src:>3} shared_lines {shared:>3} "
          f"lines m {len(mlines):>5} f {len(flines):>5} only_m {len(mlines - flines):>4} only_f {len(flines - mlines):>4} drift {d['pages']}")
