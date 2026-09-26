# Read-only: the page-end rule for stray footnote-like paragraphs: a marker-initial body paragraph in the
# trailing run of marker-initial paragraphs that ends its source page (its only page).
import re
from editor.models import Manuscript
from editor import document as doc
RE = re.compile(r'^\s*[\(\[]\s*([0-9٠-٩]{1,3}|\*{1,3})\s*[\)\]]\s*[-–.:،]?\s*\S')
for m in Manuscript.objects.order_by('book_id'):
    content = doc.content_of(m.document)
    blocks = list(doc.flat_blocks(content[doc.preamble_end(content):]))
    by_page = {}
    for n in blocks:
        pages = doc.source_pages(n)
        if len(set(pages)) == 1:
            by_page.setdefault(pages[0], []).append(n)
    flagged, candidates = [], 0
    for page, nodes in by_page.items():
        marks = [n.get('type') == 'paragraph' and bool(RE.match(doc.block_text(n))) for n in nodes]
        candidates += sum(marks)
        k = len(nodes)
        while k > 0 and marks[k - 1]:
            k -= 1
        run = nodes[k:]
        if run and len(run) < len(nodes):  # a trailing run, not the whole page
            flagged += [(page, doc.block_text(n)[:50]) for n in run]
    if candidates:
        print('book', m.book_id, 'marker-initial', candidates, 'flagged by the page-end rule', len(flagged), flagged[:4])
