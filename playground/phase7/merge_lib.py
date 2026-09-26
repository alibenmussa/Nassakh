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
    for node in nodes:
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
                        if not out or out[-1] != (' ',): out.append((' ',))
                    else: word += ch
                if word: out.append(('w', word, marks))
            elif kind == 'footnote':
                out.append(('fn', json.dumps(canon(item.get('content')), ensure_ascii=False, sort_keys=True)))
            else:
                out.append((kind, json.dumps(canon(item.get('attrs')), sort_keys=True)))
    return out
def matches(o, x):
    m = {}
    for i, j, size in difflib.SequenceMatcher(None, o, x, autojunk=False).get_matching_blocks():
        for k in range(size): m[i + k] = j + k
    return m
def diff3(o, a, b):
    ma, mb = matches(o, a), matches(o, b)
    out, conflicts = [], []
    io = ia = ib = 0
    for i in [k for k in range(len(o)) if k in ma and k in mb] + [len(o)]:
        ja = ma[i] if i < len(o) else len(a)
        jb = mb[i] if i < len(o) else len(b)
        oc, ac, bc = o[io:i], a[ia:ja], b[ib:jb]
        if ac == oc: out += bc
        elif bc == oc or ac == bc: out += ac
        else: conflicts.append((oc, ac, bc)); out += ac
        if i < len(o): out.append(o[i]); io, ia, ib = i + 1, ja + 1, jb + 1
    return out, conflicts
def text_of(toks):
    return ''.join(t[1] if t[0] == 'w' else ' ' if t[0] == ' ' else ' ¶ ' if t[0] == '¶' else '[' + t[0] + ']' for t in toks).strip()
def body(d):
    c = doc.content_of(d); return c[doc.preamble_end(c):]
def plan_merge(mine, base, theirs, pages):
    S = set(pages)
    touch = lambda nodes: [n for n in nodes if set(doc.source_pages(n)) & S]
    groups = []
    for side, nodes in (('m', touch(body(mine))), ('b', touch(body(base))), ('f', touch(body(theirs)))):
        for node in nodes:
            ls = lines_of(node); g = {'lines': set(ls), 'm': [], 'b': [], 'f': []}
            for h in [g2 for g2 in groups if g2['lines'] & ls]:
                groups.remove(h); g['lines'] |= h['lines']
                for k in 'mbf': g[k] += h[k]
            g[side].append(node); groups.append(g)
    items = []
    for g in groups:
        o, a, b = tokens(g['b']), tokens(g['m']), tokens(g['f'])
        if a == b: continue
        if a == o: items.append({'kind': 'take', 'summary': f"{text_of(o)[-60:]} → {text_of(b)[-60:]}"}); continue
        if b == o: continue
        merged, conflicts = diff3(o, a, b)
        items.append({'kind': 'conflict' if conflicts else 'merged', 'summary': text_of(merged)[:120]})
    return items
def owner_edits(mine, base):
    bb, bm = body(base), body(mine)
    sm = difflib.SequenceMatcher(None, [repr(tokens([n])) for n in bb], [repr(tokens([n])) for n in bm], autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != 'equal':
            out += [(tag, n.get('type'), doc.source_pages(n), doc.block_text(n)[:40]) for n in bm[j1:j2]]
    return out
