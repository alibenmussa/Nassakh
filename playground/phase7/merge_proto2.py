"""Scratch prototype, generalised: base rebuilt from the review history after the run, mine = the snapshot
taken just before the chapter re-assembly, theirs = the fresh pipeline; page-scoped three-way merge."""
import copy, difflib, json, sys
from assembly import pipeline, services as asv
from assembly.models import AssemblyRun
from books.models import Book
from editor import document as doc
from editor.models import ManuscriptSnapshot
from review.models import LineRevision
exec(open('/private/tmp/claude-501/-Users-alibenmussa-PycharmProjects-me-Nassakh/5e011468-4f0f-4097-b8c6-1a1084b7f4be/scratchpad/phase7/merge_lib.py').read())
for BOOK, RUN, SNAP in ((23, 37, 34), (26, 41, 39)):
    book = Book.objects.get(pk=BOOK); run = AssemblyRun.objects.get(pk=RUN)
    loaded = asv.load_book(book)
    base_pages = copy.deepcopy(loaded.pages)
    revs = list(LineRevision.objects.filter(page__book_id=BOOK, created_at__gt=run.finished_at, undone=False).order_by('-created_at', '-id'))
    changed_pages = {r.page.number for r in revs if r.action not in ('approve', 'reopen')}
    by_line = {l.id: l for p in base_pages for l in p.lines}
    for r in revs:  # newest first: revert line content; statuses back to the run's reviewed flags
        if r.action in ('approve', 'reopen'): continue
        snap = r.before
        if snap and snap.get('id') in by_line:
            line = by_line[snap['id']]
            line.text, line.uncertain = asv.line_words(snap.get('tokens'), snap.get('text'))
    info = {int(k): v for k, v in run.included.items()}
    for p in base_pages:
        if p.id in info:
            p.reviewed = bool(info[p.id]['reviewed']); p.status = 'reviewed' if p.reviewed else 'ocr_done'
    opts = pipeline.normalize_settings(run.settings); meta = asv.book_meta(book)
    base = pipeline.assemble(base_pages, opts, meta).document
    theirs = pipeline.assemble(loaded.pages, opts, meta).document
    mine = ManuscriptSnapshot.objects.get(pk=SNAP).document
    print(f'== book {BOOK}: review-changed pages {sorted(changed_pages)}')
    plan = plan_merge(mine, base, theirs, changed_pages)
    for item in plan: print('  ', item['kind'], '|', item['summary'])
    edits = owner_edits(mine, base)
    print(f'   owner edits kept outside the pages: {len(edits)} blocks, e.g.', edits[:3])
