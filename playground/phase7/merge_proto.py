"""Scratch prototype (not project code): a page-scoped three-way merge of review changes into an edited
manuscript, run read-only on book 26 (the UX test's incident). base = the assembly the edits started from,
mine = the edited text (snapshot 39, v3, taken just before the chapter re-assembly), theirs = a fresh
assembly of the current lines."""
import copy, difflib, json
from assembly import pipeline, services as asv
from editor import document as doc
from editor.models import ManuscriptSnapshot
from review.models import LineRevision
from assembly.models import AssemblyRun

BOOK = 26
book = __import__('books.models', fromlist=['Book']).Book.objects.get(pk=BOOK)
run41 = AssemblyRun.objects.get(pk=41)
loaded = asv.load_book(book)
# ---- base: the lines as the first run read them (one review revision after it: line 5427 on page 3)
base_pages = copy.deepcopy(loaded.pages)
rev = LineRevision.objects.get(page__book_id=BOOK, line_id=5427, action='resolve')
for p in base_pages:
    if p.number in (3, 4, 5, 6, 7, 8):
        p.status = 'ocr_done'; p.reviewed = False
    for line in p.lines:
        if line.id == 5427:
            line.text, line.uncertain = asv.line_words(rev.before['tokens'], rev.before['text'])
meta = asv.book_meta(book)
base = pipeline.assemble(base_pages, pipeline.normalize_settings(run41.settings), meta).document
theirs = pipeline.assemble(loaded.pages, pipeline.normalize_settings(run41.settings), meta).document
mine = ManuscriptSnapshot.objects.get(pk=39).document

VOLATILE = {'id', 'reviewed', 'suggestedRole', 'sourcePages', 'sourceLineIds', 'number', 'marker', 'orphan', 'sourcePage'}

def canon(node):
    if isinstance(node, dict):
        return {k: canon(v) for k, v in node.items() if k not in VOLATILE and v not in (None, [], {}, False)}
    if isinstance(node, list):
        return [canon(v) for v in node]
    return node

def lines_of(node):
    out = set(doc.attrs_of(node).get('sourceLineIds') or [])
    for _b, note, _c in doc.iter_containers([node]):
        if note is not None:
            out |= set(doc.attrs_of(note).get('sourceLineIds') or [])
    return out

def tokens(nodes):
    out = []
    for n, node in enumerate(nodes):
        a = doc.attrs_of(node)
        out.append(('¶', node.get('type'), a.get('level'), a.get('style')))
        for item in node.get('content') or []:
            kind = item.get('type')
            if kind == 'text':
                marks = tuple(sorted(m.get('type') for m in item.get('marks') or []))
                word = ''
                for ch in item.get('text') or '':
                    if ch.isspace():
                        if word: out.append(('w', word, marks)); word = ''
                        out.append((' ',))
                    else:
                        word += ch
                if word: out.append(('w', word, marks))
            elif kind == 'footnote':
                out.append(('fn', json.dumps(canon(item.get('content')), ensure_ascii=False, sort_keys=True)))
            else:
                out.append((kind, json.dumps(canon(item.get('attrs')), sort_keys=True)))
    return out

def matches(o, x):
    sm = difflib.SequenceMatcher(None, o, x, autojunk=False)
    m = {}
    for i, j, size in sm.get_matching_blocks():
        for k in range(size):
            m[i + k] = j + k
    return m

def diff3(o, a, b):
    ma, mb = matches(o, a), matches(o, b)
    out, conflicts = [], []
    io = ia = ib = 0
    for i in [k for k in range(len(o)) if k in ma and k in mb] + [len(o)]:
        ja = ma.get(i, len(a)) if i < len(o) else len(a)
        jb = mb.get(i, len(b)) if i < len(o) else len(b)
        oc, ac, bc = o[io:i], a[ia:ja], b[ib:jb]
        if ac == oc: out += bc
        elif bc == oc or ac == bc: out += ac
        else:
            conflicts.append((oc, ac, bc)); out += ac  # conflict: keep mine, report
        if i < len(o):
            out.append(o[i]); io, ia, ib = i + 1, ja + 1, jb + 1
    return out, conflicts

def text_of(toks):
    return ''.join(t[1] if t[0] == 'w' else ' ' if t[0] == ' ' else ' ¶ ' if t[0] == '¶' else '[' + t[0] + ']' for t in toks).strip()

def body(d):
    c = doc.content_of(d)
    return c[doc.preamble_end(c):]

S = {3}
def touching(nodes):
    return [n for n in nodes if set(doc.source_pages(n)) & S]

M, B, F = touching(body(mine)), touching(body(base)), touching(body(theirs))
print('blocks touching page 3: mine', len(M), 'base', len(B), 'fresh', len(F))
# clusters by shared lines
groups = []
for side, nodes in (('m', M), ('b', B), ('f', F)):
    for node in nodes:
        ls = lines_of(node)
        hit = [g for g in groups if g['lines'] & ls]
        g = {'lines': set(ls), 'm': [], 'b': [], 'f': []}
        for h in hit:
            groups.remove(h); g['lines'] |= h['lines']
            for k in 'mbf': g[k] += h[k]
        g[side].append(node); groups.append(g)
stats = {'unchanged': 0, 'take': 0, 'keep': 0, 'merged': 0, 'conflict': 0}
for g in groups:
    o, a, b = tokens(g['b']), tokens(g['m']), tokens(g['f'])
    if a == b: stats['unchanged'] += 1; continue
    if a == o: stats['take'] += 1; print('TAKE  ', text_of(o)[:70], '→', text_of(b)[:70]); continue
    if b == o: stats['keep'] += 1; continue
    merged, conflicts = diff3(o, a, b)
    if conflicts: stats['conflict'] += 1; print('CONFLICT', conflicts[:2])
    else:
        stats['merged'] += 1
        print('MERGED mine :', text_of(a)[:160]); print('       result:', text_of(merged)[:160])
print(stats)
# what did the owner change on the book page (mine vs base), book-wide?
bm, bb = body(mine), body(base)
print('book-wide: mine blocks', len(bm), 'base blocks', len(bb))
sm = difflib.SequenceMatcher(None, [repr(tokens([n])) for n in bb], [repr(tokens([n])) for n in bm], autojunk=False)
def plain(n):
    return doc.block_text(n).replace('\ufffc', '[*]')[:90]
for tag, i1, i2, j1, j2 in sm.get_opcodes():
    if tag != 'equal':
        for n in bb[i1:i2]: print('   base', doc.source_pages(n), n.get('type'), plain(n))
        for n in bm[j1:j2]: print('   mine', doc.source_pages(n), n.get('type'), doc.attrs_of(n).get('level'), plain(n))
        print('   --')
