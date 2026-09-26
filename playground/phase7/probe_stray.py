# Read-only: body paragraphs that start like a footnote «(1) …» in the manuscripts, and where they sit.
import re
from editor.models import Manuscript
from editor import document as doc
RE = re.compile(r'^\s*[\(\[]\s*([0-9٠-٩]{1,3}|\*{1,3})\s*[\)\]]\s*[-–.:،]?\s*\S')
RE_MID = re.compile(r'[\(\[]\s*[0-9٠-٩]{1,3}\s*[\)\]]')
for m in Manuscript.objects.order_by('book_id'):
    content = doc.content_of(m.document)
    blocks = [n for n in doc.flat_blocks(content[doc.preamble_end(content):])]
    hits = []
    for i, n in enumerate(blocks):
        if n.get('type') != 'paragraph':
            continue
        text = doc.block_text(n)
        if RE.match(text):
            # where on its page: last blocks of the page? how long? followed by more such?
            pages = doc.source_pages(n)
            same_page = [b for b in blocks if doc.source_pages(b) and doc.source_pages(b)[0] == (pages[0] if pages else None)]
            pos = same_page.index(n) if n in same_page else -1
            hits.append((pages, f'{pos + 1}/{len(same_page)}', len(text.split()), text[:60]))
    if hits:
        print('book', m.book_id, len(hits))
        for h in hits[:6]: print('   ', h)
