from collections import Counter
from books.models import Book, Page
from editor.models import Manuscript
from assembly import services as asv, pipeline
from review.models import LineRevision
for bid in (19, 26, 13):
    m = Manuscript.objects.select_related('run').get(book_id=bid)
    run = m.run
    rows = asv.page_rows(m.book)
    options = pipeline.normalize_settings(m.book.assembly_settings)
    allowed = asv.eligible_statuses(options)
    known = {int(k): v for k, v in (run.included or {}).items()}
    sigs = asv.page_signatures([pk for pk, n, s in rows])
    reasons = Counter()
    detail = {}
    for pk, number, status in rows:
        info = known.get(pk)
        elig = status in allowed
        if info is None and elig: r = 'added'
        elif info is not None and not elig: r = 'removed'
        elif info is None: continue
        elif bool(info.get('reviewed')) != (status in pipeline.REVIEWED_STATUSES):
            r = 'status' if sigs.get(pk) == info.get('sig') else 'status+text'
        elif sigs.get(pk) != info.get('sig'): r = 'text'
        else: continue
        reasons[r] += 1
        detail.setdefault(r, []).append(number)
    print('book', bid, 'run', run.pk, 'finished', run.finished_at, dict(reasons))
    for r, ns in detail.items():
        print('   ', r, ns[:30])
    # for 'text' pages: what revisions came after the run?
    text_pages = detail.get('text', []) + detail.get('status+text', [])
    revs = Counter(LineRevision.objects.filter(page__book_id=bid, page__number__in=text_pages, created_at__gt=run.finished_at).values_list('action', flat=True))
    print('    revisions after run on text pages:', dict(revs))
