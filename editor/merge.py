"""The page-by-page merge of review changes into an edited book (PHASE7_SPEC §5.6, D78). Pure: no ORM, no I/O.

Three documents meet here, all in the manuscript's ProseMirror shape (`editor.document`):

- **M** (`document`): the manuscript as the owner edited it on the book page;
- **B** (`base`): the assembled text the edits started from (`Manuscript.base`), or None for a book edited
  before 7c. Its `attrs.unknownPages` lists pages whose base is not known (drift pages left out of an
  apply on such a book): clusters touching them are merged as if there were no base;
- **F** (`fresh`): `assembly.pipeline.assemble` on the lines as they are now.

`plan(M, B, F, pages)` compares only the blocks of the pages asked for (S) and returns one item per cluster
that differs (`page` the first page of S it touches, `pages` every page it touches: a paragraph over two
drift pages belongs to both; `id` a digest of the item, the same in every plan while the item does not
change: `item_id`); `apply(M, items, choices)` replaces those blocks and leaves every other block the very
same object, so chapter versions do not move without an edit; `splice_base(B, F, pages)` moves the base over
the pages taken.

**Select and cluster.** A top-level body block (after the leading `title`) is selected when one of its
`sourcePages`, one of its notes' `sourcePage`, or (with `lines`, the page of each line id) the page of one of
its lines is in S. `lines_of(block)` is its `sourceLineIds` plus its notes' and its blockquote paragraphs'.
Blocks of the three documents that share a source line are one cluster (union-find over every block, so the
other half of a paragraph that runs onto a page outside S comes along); a cluster is compared when one of its
blocks is selected. A block the owner typed (no source line) is never clustered and never moves.

**Tokens.** A block list becomes tokens compared by `key`: `("¶", type, level, style)` at each block start,
`("w", text, marks)` per run of non-space characters with one set of marks, `(" ",)` for a collapsed space
(none at a block's edges), `("fn", note tokens)`, `("pb", page)`, `("br",)`; a blockquote is one opaque
`("bq", …)` token. Volatile attributes never enter a key (`VOLATILE`, and every null or empty attribute):
TipTap splits text nodes and adds null attributes, so node JSON would call every block changed. `noteFor`
(D74, the pipeline's «حاشية للعلامة (n)» hint, derived from the open calls of the page) is volatile too: it is
recomputed by every assembly and changes no text. Each token keeps its source (the text node's marks, the
footnote node, the block), so a rebuild restores the real nodes.

**Decide** (T = the tokens of a cluster's M, B and F blocks):

| case | kind | default |
|---|---|---|
| T(m) = T(f) | – | |
| base known, T(m) = T(b) | take | theirs |
| base known, T(f) = T(b) | – (the owner's edit stays) | |
| base known, both changed, diff3 clean | merged | merged |
| … and the merged result is mine (the owner made review's change too: a fix everywhere's book side) | – | |
| base known, both changed, diff3 conflicts | conflict | mine |
| no base, T(m) ≠ T(f) | choose | mine |
| m empty, f new: base known with no partner, or the page is `added` | insert | theirs |
| m empty, f new, no base, the page not added | choose | mine |
| m empty, b = f (the owner deleted it) | – | |
| m empty, b ≠ f | conflict | mine |
| f empty, T(m) = T(b) | remove | theirs |
| f empty, T(m) ≠ T(b) | conflict | mine |

diff3 runs over tokens: the anchors are the base tokens `difflib` (autojunk off) matches in both mine and
theirs; between anchors it takes theirs where mine equals base, mine where theirs equals base, either where
both made the same change, and otherwise records a conflict (keeping mine). A conflict of one footnote token
against one footnote token of the same note is merged again on the note's own tokens.

**Rebuild.** A 1:1 take keeps mine's `id`, `breakBefore`, `keepWithNext`, `breakAfter` and the text options
(D99: alignment, direction, indent, spacing, size — the owner's own formatting) (theirs' attributes otherwise,
all of theirs when the block type changed). A merged result is cut into blocks at its `¶` tokens; each block
takes the attributes of the block that produced its `¶` (mine's, else the fresh one's), `sourceLineIds` and
`sourcePages` are the union of the blocks its tokens came from and `reviewed` comes from the fresh blocks
sharing its lines. An insert goes after the M block holding the lines of the nearest preceding F block that
has an M partner, else before the successor's, else at the end of the chapter covering its page.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
from dataclasses import dataclass, field

from . import document as doc

VOLATILE: frozenset[str] = frozenset(
    {
        "id",
        "reviewed",
        "suggestedRole",
        "sourcePages",
        "sourceLineIds",
        "number",
        "marker",
        "orphan",
        "sourcePage",
        "noteFor",
    }
)
CONTEXT_WORDS = 5  # words of context around each change in an item's `diff`
CONFLICT_WORDS = 3  # words of context around a conflicting stretch
# mine's page-break attrs and (D99) text options — the owner's choices for the paragraph — kept by a 1:1 take
FLAGS: tuple[str, ...] = (*doc.BLOCK_FLAGS, *doc.TEXT_ATTRS)

TAKE, MERGED, CONFLICT, CHOOSE, INSERT, REMOVE = "take", "merged", "conflict", "choose", "insert", "remove"
MINE, THEIRS, MERGE = "mine", "theirs", "merged"
DEFAULTS: dict[str, str] = {
    TAKE: THEIRS,
    MERGED: MERGE,
    CONFLICT: MINE,
    CHOOSE: MINE,
    INSERT: THEIRS,
    REMOVE: THEIRS,
}
CHOICES: dict[str, tuple[str, ...]] = {
    TAKE: (THEIRS, MINE),
    MERGED: (MERGE, MINE, THEIRS),
    CONFLICT: (MINE, THEIRS),
    CHOOSE: (MINE, THEIRS),
    INSERT: (THEIRS, MINE),
    REMOVE: (THEIRS, MINE),
}
CHIPS: dict[str, str] = {
    TAKE: "من المراجعة",
    MERGED: "مع تعديلك",
    CONFLICT: "تعارض",
    CHOOSE: "للمقارنة",
    INSERT: "فقرة جديدة",
    REMOVE: "تُحذف",
}
HELP_CONFLICT = "عدّلتَ هذه الكلمات في الكتاب، وغيّرتها المراجعة أيضًا."
HELP_DELETED = "حذفتَ هذه الفقرة من الكتاب، وغيّرتها المراجعة."
HELP_CHOOSE = "لا يُعرف ما عدّلتَه هنا قبل هذا الإصدار من نسّاخ؛ قارن واختر."
UNKNOWN_PAGES = "unknownPages"  # base attrs: pages whose base is not known (merged as without a base)


# ====================================================================== canonical form and tokens


def canon(value):
    """A node without its volatile attributes and without null / empty values (a comparison form)."""
    if isinstance(value, dict):
        return {
            k: canon(v)
            for k, v in sorted(value.items())
            if k not in VOLATILE and v not in (None, "", [], {}, False)
        }
    if isinstance(value, list):
        return [canon(v) for v in value]
    return value


def _canon_json(value) -> str:
    return json.dumps(canon(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Token:
    """One unit of a block list: `key` is what is compared; the rest is where it came from.

    `text` is a word (or " "), `marks` the source text node's marks (a JSON string), `node` the inline node
    (a footnote, a page mark, a hard break) or, for `¶` and `bq`, the block; `src` the index of the source
    block in the list the token was made from (−1: made here), `inner` a footnote's own tokens."""

    key: tuple
    text: str = ""
    marks: str = "[]"
    node: dict | None = field(default=None, compare=False, hash=False)
    src: int = -1
    inner: tuple = ()


SPACE = (" ",)


def _marks_of(item: dict) -> tuple[tuple[str, ...], str]:
    marks = [m for m in item.get("marks") or [] if isinstance(m, dict)]
    return tuple(sorted(str(m.get("type")) for m in marks)), json.dumps(
        marks, ensure_ascii=False, sort_keys=True
    )


def inline_tokens(content, src: int = -1) -> list[Token]:
    """Tokens of a list of inline nodes (a block's content or a note's): spaces collapsed, none at the
    edges."""
    out: list[Token] = []
    word: list[str] = []
    word_marks: tuple = ()
    word_json = "[]"

    def flush() -> None:
        if word:
            out.append(Token(("w", "".join(word), word_marks), "".join(word), word_json, src=src))
            word.clear()

    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            types, marks_json = _marks_of(item)
            for char in str(item.get("text") or ""):
                if char.isspace():
                    flush()
                    if out and out[-1].key != SPACE:
                        out.append(Token(SPACE, " ", marks_json, src=src))
                    continue
                if word and types != word_marks:
                    flush()
                if not word:
                    word_marks, word_json = types, marks_json
                word.append(char)
            continue
        flush()
        if kind == "footnote":
            inner = tuple(inline_tokens(item.get("content"), src))
            out.append(Token(("fn", tuple(t.key for t in inner)), node=item, src=src, inner=inner))
        elif kind == "pageBreak":
            out.append(Token(("pb", doc.attrs_of(item).get("page")), node=item, src=src))
        elif kind == "hardBreak":
            out.append(Token(("br",), node=item, src=src))
        else:
            out.append(Token(("?", str(kind), _canon_json(item)), node=item, src=src))
    flush()
    while out and out[-1].key == SPACE:
        out.pop()
    while out and out[0].key == SPACE:
        out.pop(0)
    return out


def block_tokens(block: dict, src: int = -1) -> list[Token]:
    """The tokens of one top-level block: its `¶` then its inline content (a blockquote: one `bq` token)."""
    kind = block.get("type") if isinstance(block, dict) else None
    if kind == doc.BLOCKQUOTE:
        return [Token(("bq", _canon_json(block.get("content"))), node=block, src=src)]
    attrs = doc.attrs_of(block)
    level = attrs.get("level") if kind == doc.HEADING else None
    style = attrs.get("style") or None
    head = Token(("¶", "separator" if kind == "horizontalRule" else kind, level, style), node=block, src=src)
    return [head, *inline_tokens(block.get("content"), src)]


def tokens(blocks: list) -> list[Token]:
    """The tokens of a list of blocks, in order (each token's `src` is its block's index in the list)."""
    out: list[Token] = []
    for index, block in enumerate(blocks):
        out.extend(block_tokens(block, index))
    return out


def keys(items: list[Token]) -> list[tuple]:
    return [item.key for item in items]


# ====================================================================== reading the documents


def body(document) -> list:
    """The top-level blocks after the leading `title` nodes."""
    content = doc.content_of(document)
    return content[doc.preamble_end(content) :]


def _ints(values) -> list[int]:
    return [v for v in values or [] if isinstance(v, int) and not isinstance(v, bool)]


def lines_of(block) -> set[int]:
    """The source lines of a block: its own, its notes' and a blockquote's paragraphs'."""
    out = set(doc.source_lines([block]))
    for _block, note, _content in doc.iter_containers([block]):
        if note is not None:
            out.update(_ints(doc.attrs_of(note).get("sourceLineIds")))
    return out


def pages_of(block, lines: dict[int, tuple[int, int]] | None = None) -> set[int]:
    """The source pages a block touches: its `sourcePages`, its notes' `sourcePage` and, with `lines` (line id
    → (page, order)), the pages of its lines."""
    out = set(doc.source_pages(block))
    for _block, note, _content in doc.iter_containers([block]):
        if note is not None:
            page = doc.attrs_of(note).get("sourcePage")
            if isinstance(page, int) and not isinstance(page, bool):
                out.add(page)
    if lines:
        for line in lines_of(block):
            where = lines.get(line)
            if where is not None:
                out.add(where[0])
    return out


def unknown_pages(base) -> set[int]:
    """Pages whose base is not known (`attrs.unknownPages` of a base document)."""
    attrs = base.get("attrs") if isinstance(base, dict) else None
    return set(_ints((attrs or {}).get(UNKNOWN_PAGES))) if isinstance(attrs, dict) else set()


# ====================================================================== diff3


def _matches(o: list, x: list) -> dict[int, int]:
    """Index in `o` → index in `x` of the tokens `difflib` matches (autojunk off)."""
    out: dict[int, int] = {}
    for i, j, size in difflib.SequenceMatcher(None, o, x, autojunk=False).get_matching_blocks():
        for k in range(size):
            out[i + k] = j + k
    return out


def diff3(o: list[Token], a: list[Token], b: list[Token]) -> tuple[list[Token], list[Stretch]]:
    """Three-way merge of token lists (base `o`, mine `a`, theirs `b`); a conflict keeps mine's tokens.

    Returns `(merged, conflicts)`; an anchor is taken as mine's token, so the result keeps mine's nodes."""
    merged, conflicts = diff3_tagged(o, a, b)
    return [token for _side, token in merged], conflicts


def _same_note(x: Token, y: Token) -> bool:
    lx = set(_ints(doc.attrs_of(x.node).get("sourceLineIds")))
    ly = set(_ints(doc.attrs_of(y.node).get("sourceLineIds")))
    return bool(lx & ly) or (bool(doc.node_id(x.node)) and doc.node_id(x.node) == doc.node_id(y.node))


def _merge_note(o: list[Token], a: list[Token], b: list[Token]) -> Token | None:
    """One footnote against one footnote of the same note (and its base): merged on the note's own tokens,
    or None (a conflict)."""
    if not (len(o) == len(a) == len(b) == 1) or not all(t.key[0] == "fn" for t in (o[0], a[0], b[0])):
        return None
    if not (_same_note(a[0], b[0]) and _same_note(o[0], a[0])):
        return None
    merged, conflicts = diff3(list(o[0].inner), list(a[0].inner), list(b[0].inner))
    if conflicts:
        return None
    node = copy.deepcopy(a[0].node)
    node["content"] = inline_nodes(merged)
    return Token(("fn", tuple(t.key for t in merged)), node=node, src=a[0].src, inner=tuple(merged))


# ====================================================================== rebuilding nodes from tokens


def inline_nodes(items: list[Token]) -> list[dict]:
    """Inline content from tokens: words and spaces as text nodes (neighbours with the same marks merged),
    footnotes, page marks and hard breaks as their nodes (deep copies); no space at the edges."""
    items = list(items)
    while items and items[0].key == SPACE:
        items.pop(0)
    while items and items[-1].key == SPACE:
        items.pop()
    out: list[dict] = []
    buffer: list[str] = []
    marks_now: str | None = None

    def flush() -> None:
        if buffer and marks_now is not None:
            node: dict = {"type": "text", "text": "".join(buffer)}
            marks = json.loads(marks_now)
            if marks:
                node["marks"] = marks
            out.append(node)
        buffer.clear()

    previous_space = False
    for item in items:
        kind = item.key[0]
        if kind in ("w", " "):
            if kind == " ":
                if previous_space:
                    continue
                previous_space = True
            else:
                previous_space = False
            if item.marks != marks_now:
                flush()
                marks_now = item.marks
            buffer.append(item.text)
            continue
        previous_space = False
        flush()
        marks_now = None
        if item.node is not None:
            out.append(copy.deepcopy(item.node))
    flush()
    return out


def _union(values: list[list[int]]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for group in values:
        for value in group:
            if value not in seen:
                seen.add(value)
                out.append(value)
    return out


@dataclass
class Sides:
    """A cluster's blocks per side (`m`, `b`, `f`), each in its document order."""

    m: list[dict]
    b: list[dict]
    f: list[dict]


def _rebuild(merged: list[tuple[str, Token]], sides: Sides) -> list[dict]:
    """Blocks from merged tokens tagged with their side (`m` or `f`), cut at their `¶` tokens."""
    blocks: list[dict] = []
    parts: list[list[tuple[str, Token]]] = []
    for side, item in merged:
        if item.key[0] in ("¶", "bq") or not parts:
            parts.append([])
        parts[-1].append((side, item))
    fresh_lines = [(block, lines_of(block)) for block in sides.f]
    for part in parts:
        side, head = part[0]
        if head.key[0] == "bq":
            blocks.append(copy.deepcopy(head.node))
            rest = part[1:]
            if rest:  # inline tokens after a blockquote: a paragraph of their own (the fresh one's attrs)
                blocks.append(_block_from(("f", _first_block(sides)), rest, sides, fresh_lines))
            continue
        if head.key[0] != "¶":  # tokens before any `¶`: they open a paragraph with the fresh attrs
            blocks.append(_block_from(("f", _first_block(sides)), part, sides, fresh_lines))
            continue
        blocks.append(_block_from((side, head.node), part[1:], sides, fresh_lines))
    return blocks


def _first_block(sides: Sides) -> dict:
    for block in (*sides.f, *sides.m):
        if isinstance(block, dict) and block.get("type") in (doc.PARAGRAPH, doc.HEADING):
            return block
    return {"type": doc.PARAGRAPH, "attrs": {}}


def _block_from(head: tuple[str, dict], part: list[tuple[str, Token]], sides: Sides, fresh_lines) -> dict:
    side, source = head
    node: dict = {"type": source.get("type") or doc.PARAGRAPH}
    attrs = copy.deepcopy(doc.attrs_of(source))
    lists = sides.m if side == "m" else sides.f
    own = [lists[t.src] for s, t in part if t.src >= 0 and s == side and 0 <= t.src < len(lists)]
    other = [
        (sides.m if s == "m" else sides.f)[t.src]
        for s, t in part
        if t.src >= 0 and s != side and 0 <= t.src < len(sides.m if s == "m" else sides.f)
    ]
    contributors = [source, *own, *other]
    lines = _union([_ints(doc.attrs_of(block).get("sourceLineIds")) for block in contributors])
    pages = sorted({p for block in contributors for p in doc.source_pages(block)})
    if "sourceLineIds" in attrs or lines:
        attrs["sourceLineIds"] = lines
    if "sourcePages" in attrs or pages:
        attrs["sourcePages"] = pages
    wanted = set(lines)
    partners = [block for block, found in fresh_lines if found & wanted]
    if partners:
        attrs["reviewed"] = all(bool(doc.attrs_of(block).get("reviewed")) for block in partners)
    node["attrs"] = attrs
    if node["type"] not in (doc.SEPARATOR, "horizontalRule"):
        node["content"] = inline_nodes([t for _s, t in part])
    return node


def take_nodes(sides: Sides) -> list[dict]:
    """Theirs: the fresh blocks (deep copies); a 1:1 take of the same block type keeps mine's id and page
    flags."""
    out = [copy.deepcopy(block) for block in sides.f]
    if len(sides.m) == 1 and len(out) == 1 and out[0].get("type") == sides.m[0].get("type"):
        mine = doc.attrs_of(sides.m[0])
        attrs = out[0].setdefault("attrs", {})
        if doc.node_id(sides.m[0]):
            attrs["id"] = doc.node_id(sides.m[0])
        for flag in FLAGS:
            if mine.get(flag) is not None:
                attrs[flag] = mine[flag]
            else:
                attrs.pop(flag, None)
    return out


# ====================================================================== display: the item's diff


def _unit(item: Token) -> str | None:
    kind = item.key[0]
    if kind == "w":
        return item.text
    if kind == "fn":
        text = " ".join(t.text for t in item.inner if t.key[0] == "w")
        return f"[حاشية: {text}]"
    if kind == "¶":
        return "¶"
    if kind == "bq":
        return "[اقتباس]"
    if kind == "br":
        return "↵"
    return None  # spaces and page marks are not shown


def _units(items: list[Token], leading: bool = False) -> list[tuple[tuple, str]]:
    """The shown words of tokens (`(key, text)`); a list's opening `¶` is not shown unless `leading`."""
    out: list[tuple[tuple, str]] = []
    for index, item in enumerate(items):
        text = _unit(item)
        if text is None or (index == 0 and item.key[0] == "¶" and not leading):
            continue
        out.append((item.key, text))
    return out


def _words(units: list[tuple[tuple, str]]) -> str:
    return " ".join(text for _key, text in units)


def diff_ops(mine: list[Token], result: list[Token], context: int = CONTEXT_WORDS) -> list[list[str]]:
    """`[[op, text]…]` (eq / del / ins) of mine against `result`, word by word, with `context` words of
    context around the changes and «…» where the text is cut."""
    a, b = _units(mine), _units(result)
    matcher = difflib.SequenceMatcher(None, [k for k, _t in a], [k for k, _t in b], autojunk=False)
    codes = matcher.get_opcodes()
    out: list[list[str]] = []
    for n, (tag, i1, i2, j1, j2) in enumerate(codes):
        if tag == "equal":
            run = a[i1:i2]
            first, last = n == 0, n == len(codes) - 1
            if first and last:
                continue
            if first:
                text = ("… " if len(run) > context else "") + _words(run[-context:])
            elif last:
                text = _words(run[:context]) + (" …" if len(run) > context else "")
            elif len(run) > 2 * context:
                text = f"{_words(run[:context])} … {_words(run[-context:])}"
            else:
                text = _words(run)
            if text:
                out.append(["eq", text])
            continue
        if i2 > i1:
            out.append(["del", _words(a[i1:i2])])
        if j2 > j1:
            out.append(["ins", _words(b[j1:j2])])
    return out


def _snippet(before: list[Token], chunk: list[Token], after: list[Token]) -> str:
    """A conflicting stretch as the UI quotes it: `CONFLICT_WORDS` words before and after, «…» where cut."""
    head_units, tail_units = _units(before), _units(after, leading=True)
    head, tail = head_units[-CONFLICT_WORDS:], tail_units[:CONFLICT_WORDS]
    text = _words(_units(chunk, leading=bool(before)))
    parts = []
    if head:
        parts.append(("… " if len(head_units) > CONFLICT_WORDS else "") + _words(head))
    if text:
        parts.append(text)
    if tail:
        parts.append(_words(tail) + (" …" if len(tail_units) > CONFLICT_WORDS else ""))
    return " ".join(parts)


# ====================================================================== the plan


class _Union:
    def __init__(self) -> None:
        self.parent: dict = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def join(self, x, y) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self.parent[max(rx, ry)] = min(rx, ry)


@dataclass
class Cluster:
    """Blocks of the three documents joined by shared source lines (indexes into each `body`)."""

    m: list[int] = field(default_factory=list)
    b: list[int] = field(default_factory=list)
    f: list[int] = field(default_factory=list)
    selected: bool = False


def clusters(
    mine: list, base: list, fresh: list, pages: set[int], lines: dict | None = None
) -> tuple[list[Cluster], dict[tuple[str, int], int]]:
    """The clusters of the three bodies, and the cluster of each block (`(side, index)` → cluster index)."""
    union = _Union()
    owner: dict[int, tuple[str, int]] = {}
    sides = (("m", mine), ("b", base), ("f", fresh))
    for side, blocks in sides:
        for index, block in enumerate(blocks):
            node = (side, index)
            found = lines_of(block)
            if not found:
                continue
            union.find(node)
            for line in found:
                if line in owner:
                    union.join(owner[line], node)
                else:
                    owner[line] = node
    groups: dict = {}
    for side, blocks in sides:
        for index, block in enumerate(blocks):
            node = (side, index)
            if node not in union.parent:
                continue
            root = union.find(node)
            cluster = groups.setdefault(root, Cluster())
            getattr(cluster, side).append(index)
            if pages_of(block, lines) & pages:
                cluster.selected = True
    ordered = sorted(groups.values(), key=lambda c: (c.f[0] if c.f else 10**9, c.m[0] if c.m else 10**9))
    where: dict[tuple[str, int], int] = {}
    for n, cluster in enumerate(ordered):
        for side in ("m", "b", "f"):
            for index in getattr(cluster, side):
                where[(side, index)] = n
    return ordered, where


def _chapter_at(chapters: list[doc.ChapterSlice], index: int) -> doc.ChapterSlice | None:
    for chapter in chapters:
        if chapter.start <= index < chapter.end:
            return chapter
    return chapters[-1] if chapters else None


def _anchor(
    cluster: Cluster, ordered: list[Cluster], where: dict, fresh_count: int
) -> tuple[str, int] | None:
    """Where an item with no M block goes: `("after", m index)` of the M block holding the lines of the
    nearest preceding F block with an M partner, else `("before", m index)` of the successor's, else None."""
    if not cluster.f:
        return None
    first, last = cluster.f[0], cluster.f[-1]
    for index in range(first - 1, -1, -1):
        partner = ordered[where[("f", index)]] if ("f", index) in where else None
        if partner is not None and partner.m:
            return ("after", max(partner.m))
    for index in range(last + 1, fresh_count):
        partner = ordered[where[("f", index)]] if ("f", index) in where else None
        if partner is not None and partner.m:
            return ("before", min(partner.m))
    return None


def plan(
    document: dict,
    base: dict | None,
    fresh: dict,
    pages,
    *,
    added=(),
    lines: dict[int, tuple[int, int]] | None = None,
) -> list[dict]:
    """The items of the merge for the pages `pages` (see the module docstring), in page then document order.

    `added` are pages new to the book since the manuscript (an insert with no base); `lines` the page and
    order of each current line id. Each item is the UI's dict (`id`, `kind`, `default`, `chip`, `choices`,
    `ask`, `help`, `page`, `pages`, `chapter`, `chapter_title`, `block`, `blocks`, `diff`, `conflicts`,
    `base`) plus `work`, what `apply` needs: `m` (indexes of its M blocks in the document's content),
    `anchor`, `theirs` and `merged` (the result nodes)."""
    wanted = {int(p) for p in pages}
    added = {int(p) for p in added}
    content = doc.content_of(document)
    offset = doc.preamble_end(content)
    mine, fresh_body = content[offset:], body(fresh)
    base_body = body(base) if base is not None else []
    unknown = unknown_pages(base) if base is not None else set()
    ordered, where = clusters(mine, base_body, fresh_body, wanted, lines)
    chapters = doc.chapters_of(document)
    items: list[dict] = []
    for cluster in ordered:
        if not cluster.selected:
            continue
        sides = Sides(
            [mine[i] for i in cluster.m],
            [base_body[i] for i in cluster.b],
            [fresh_body[i] for i in cluster.f],
        )
        all_pages = set()
        for block in (*sides.m, *sides.b, *sides.f):
            all_pages |= pages_of(block, lines)
        known = base is not None and not (all_pages & unknown)
        item = _decide(sides, known, bool(all_pages & added))
        if item is None:
            continue
        in_s = sorted(all_pages & wanted) or sorted(all_pages)
        anchor = _anchor(cluster, ordered, where, len(fresh_body)) if not cluster.m else None
        at = (offset + cluster.m[0]) if cluster.m else None
        if at is None and anchor is not None:
            at = offset + anchor[1]
        chapter = _chapter_at(chapters, at) if at is not None else (chapters[-1] if chapters else None)
        if not cluster.m and anchor is None:
            anchor = ("end", chapter.end if chapter is not None else len(content))
        elif anchor is not None:
            anchor = (anchor[0], offset + anchor[1])
        m_ids = [doc.node_id(block) for block in sides.m]
        item.update(
            page=in_s[0],
            pages=sorted(all_pages),
            chapter=chapter.id if chapter is not None else None,
            chapter_title=chapter.title if chapter is not None else "",
            block=m_ids[0] if m_ids else _anchor_id(content, anchor),
            blocks=[i for i in m_ids if i],
            base="stored" if known else "none",
        )
        item["work"].update(m=[offset + i for i in cluster.m], anchor=list(anchor) if anchor else None)
        item["_order"] = (in_s[0], at if at is not None else len(content), cluster.f[0] if cluster.f else 0)
        item["id"] = item_id(item["kind"], sides)
        items.append(item)
    items.sort(key=lambda it: it.pop("_order"))
    seen: set[str] = set()
    for item in items:  # clusters never share a line, so a repeat is a digest collision: made unique
        first, n = item["id"], 1
        while item["id"] in seen:
            n += 1
            item["id"] = f"{first}-{n}"
        seen.add(item["id"])
    return items


def item_id(kind: str, sides: Sides) -> str:
    """An item's id from what it is, not from its place in the plan: «i» and a digest of its kind, its
    source lines, the ids of its book blocks and the tokens of its three sides. A new plan (after a 409, or
    the tab opened again) gives an item that did not change the same id, so the book page keeps the owner's
    choice on it; an item whose text moved on either side gets a new id and its choice goes back to the
    default. (Positional ids, i1 i2 …, shifted onto other paragraphs as soon as one item went away.)"""
    blocks = (*sides.m, *sides.b, *sides.f)
    identity = [
        kind,
        sorted(set().union(*(lines_of(block) for block in blocks))),
        [doc.node_id(block) for block in sides.m],
        [keys(tokens(side)) for side in (sides.m, sides.b, sides.f)],
    ]
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)
    return "i" + hashlib.blake2b(raw.encode("utf-8"), digest_size=6).hexdigest()


def _anchor_id(content: list, anchor) -> str | None:
    """The id of the block an item without M blocks goes after (or before); None at a chapter's end."""
    if not anchor or anchor[0] == "end" or not 0 <= anchor[1] < len(content):
        return None
    return doc.node_id(content[anchor[1]]) or None


def _item(
    kind: str, diff: list, conflicts: list, theirs: list, merged: list | None, help_text: str = ""
) -> dict:
    return {
        "id": "",
        "kind": kind,
        "default": DEFAULTS[kind],
        "chip": CHIPS[kind],
        "choices": list(CHOICES[kind]),
        "ask": kind in (CONFLICT, CHOOSE),
        "help": help_text,
        "diff": diff,
        "conflicts": conflicts,
        "work": {"theirs": theirs, "merged": merged},
    }


def _decide(sides: Sides, known: bool, added: bool) -> dict | None:
    tm, tb, tf = tokens(sides.m), tokens(sides.b), tokens(sides.f)
    km, kb, kf = keys(tm), keys(tb), keys(tf)
    if km == kf:
        return None
    theirs = take_nodes(sides) if sides.f else []
    offer = diff_ops(tm, tf)
    whole = [{"mine": _snippet([], tm, []), "theirs": _snippet([], tf, [])}]
    if not sides.m:
        if known and sides.b:
            if kb == kf:
                return None  # the owner deleted it and review left it alone
            return _item(
                CONFLICT, offer, [{"mine": "", "theirs": whole[0]["theirs"]}], theirs, None, HELP_DELETED
            )
        if known or added:
            return _item(INSERT, offer, [], theirs, None)
        return _item(CHOOSE, offer, [], theirs, None, HELP_CHOOSE)
    if not sides.f:
        if known:
            if km == kb:
                return _item(REMOVE, offer, [], [], None)
            return _item(CONFLICT, offer, whole, [], None, HELP_CONFLICT)
        return _item(CHOOSE, offer, [], [], None, HELP_CHOOSE)
    if not known:
        return _item(CHOOSE, offer, [], theirs, None, HELP_CHOOSE)
    if km == kb:
        return _item(TAKE, offer, [], theirs, None)
    if kf == kb:
        return None  # only the owner changed it
    merged, conflicts = diff3_tagged(tb, tm, tf)
    if conflicts:
        snippets = [
            {
                "mine": _snippet(c.before, c.mine, c.after_mine),
                "theirs": _snippet(c.before, c.theirs, c.after_theirs),
            }
            for c in conflicts
        ]
        return _item(CONFLICT, offer, snippets, theirs, None, HELP_CONFLICT)
    result = [t for _s, t in merged]
    if keys(result) == km:
        return None  # the owner's text holds review's change already (D79's «استبدال الكل»): nothing to take
    nodes = _rebuild(merged, sides)
    return _item(MERGED, diff_ops(tm, result), [], theirs, nodes)


@dataclass
class Stretch:
    """A stretch both sides changed differently: the result before it, mine's and theirs' tokens, and what
    follows each."""

    before: list[Token]
    mine: list[Token]
    theirs: list[Token]
    after_mine: list[Token]
    after_theirs: list[Token]


def diff3_tagged(
    o: list[Token], a: list[Token], b: list[Token]
) -> tuple[list[tuple[str, Token]], list[Stretch]]:
    """`diff3` with each result token tagged with its side (`m` or `f`), and the conflicts with their
    context."""
    ok, ak, bk = keys(o), keys(a), keys(b)
    ma, mb = _matches(ok, ak), _matches(ok, bk)
    out: list[tuple[str, Token]] = []
    stretches: list[Stretch] = []
    io = ia = ib = 0
    anchors = [i for i in range(len(o)) if i in ma and i in mb]
    for i in [*anchors, len(o)]:
        ja = ma[i] if i < len(o) else len(a)
        jb = mb[i] if i < len(o) else len(b)
        oc, ac, bc = ok[io:i], ak[ia:ja], bk[ib:jb]
        if ac == oc:
            out.extend(("f", t) for t in b[ib:jb])
        elif bc == oc or ac == bc:
            out.extend(("m", t) for t in a[ia:ja])
        else:
            note = _merge_note(o[io:i], a[ia:ja], b[ib:jb])
            if note is not None:
                out.append(("m", note))
            else:
                stretches.append(
                    Stretch([t for _s, t in out], list(a[ia:ja]), list(b[ib:jb]), list(a[ja:]), list(b[jb:]))
                )
                out.extend(("m", t) for t in a[ia:ja])
        if i < len(o):
            out.append(("m", a[ja]))
            io, ia, ib = i + 1, ja + 1, jb + 1
    return out, stretches


# ====================================================================== applying a plan


def apply(
    document: dict,
    items: list[dict],
    choices: dict[str, str] | None = None,
    *,
    approvals=(),
    reviewed=(),
) -> tuple[dict, dict]:
    """The document with the chosen results of `items` in place (`choices`: item id → theirs | mine | merged;
    an item left out takes its `default`), and what was done: `{taken, merged, kept, changed: [block ids],
    written}`.

    Each item's chosen nodes replace its first M block and its other M blocks go; an item with no M block goes
    at its anchor. With no item changing anything the very same document object comes back (`written`
    false). Otherwise the blocks of the approval-only pages (`approvals`) get `reviewed` from `reviewed` (the
    pages reviewed now), and the new blocks' ids are made unique (`doc.repair_ids`); every other block stays
    the same object."""
    choices = choices or {}
    content = doc.content_of(document)
    replace: dict[int, list] = {}
    drop: set[int] = set()
    before: dict[int, list] = {}
    after: dict[int, list] = {}
    stats = {"taken": 0, "merged": 0, "kept": 0}
    new_nodes: list[dict] = []
    for item in items:
        choice = choices.get(item["id"], item["default"])
        if choice not in item["choices"]:
            raise ValueError(item["id"])
        if choice == MINE:
            stats["kept"] += 1
            continue
        work = item["work"]
        nodes = copy.deepcopy(work["merged"] if choice == MERGE else work["theirs"]) or []
        stats["merged" if choice == MERGE else "taken"] += 1
        new_nodes.extend(nodes)
        indexes = list(work.get("m") or [])
        if indexes:
            replace[indexes[0]] = [*replace.get(indexes[0], []), *nodes]
            drop.update(indexes[1:])
            continue
        anchor = work.get("anchor") or ["end", len(content)]
        where, index = anchor[0], int(anchor[1])
        if where == "after":
            after.setdefault(index, []).extend(nodes)
        else:  # before a block, or at a chapter's end (before the next chapter's first block)
            before.setdefault(index, []).extend(nodes)
    if not replace and not drop and not before and not after:
        return document, {**stats, "changed": [], "written": False}
    out: list = []
    for index, node in enumerate(content):
        out.extend(before.get(index, []))
        if index in replace:
            out.extend(replace[index])
        elif index not in drop:
            out.append(node)
        out.extend(after.get(index, []))
    out.extend(before.get(len(content), []))
    untouched = [node for index, node in enumerate(content) if index not in replace and index not in drop]
    doc.repair_ids(new_nodes, doc.all_ids(untouched))
    marked = set(int(p) for p in approvals)
    now = set(int(p) for p in reviewed)
    if marked:
        fresh_ids = {id(node) for node in new_nodes}
        for index, node in enumerate(out):
            if id(node) in fresh_ids or not isinstance(node, dict):
                continue
            pages = set(doc.source_pages(node))
            if not pages & marked:
                continue
            value = all(p in now for p in pages)
            if "reviewed" in doc.attrs_of(node) and doc.attrs_of(node).get("reviewed") != value:
                out[index] = {**node, "attrs": {**doc.attrs_of(node), "reviewed": value}}
    new_document = dict(document)
    new_document["content"] = out
    changed = [doc.node_id(node) for node in new_nodes if doc.node_id(node)]
    return new_document, {**stats, "changed": changed, "written": True}


# ====================================================================== the base


def splice_base(base: dict | None, fresh: dict, pages, *, unknown=(), lines: dict | None = None) -> dict:
    """The base after the pages `pages` were taken: the base's blocks touching them (and the blocks sharing
    their lines) replaced with the fresh ones, ordered by page. With no base (a book edited before 7c) the new
    base is `fresh`, and the pages in `unknown` (drift pages left out) are recorded as not known."""
    wanted = {int(p) for p in pages}
    if base is None:
        new = copy.deepcopy(fresh)
        attrs = dict(new.get("attrs") or {})
        attrs[UNKNOWN_PAGES] = sorted({int(p) for p in unknown} - wanted)
        new["attrs"] = attrs
        return new
    base_body, fresh_body = body(base), body(fresh)
    picked_f = {i for i, block in enumerate(fresh_body) if pages_of(block, lines) & wanted}
    picked_b = {i for i, block in enumerate(base_body) if pages_of(block, lines) & wanted}
    while True:  # the blocks sharing lines with the picked ones come along (a paragraph over two pages)
        found = set().union(*(lines_of(fresh_body[i]) for i in picked_f)) | set().union(
            *(lines_of(base_body[i]) for i in picked_b)
        )
        more_f = {i for i, block in enumerate(fresh_body) if i not in picked_f and lines_of(block) & found}
        more_b = {i for i, block in enumerate(base_body) if i not in picked_b and lines_of(block) & found}
        if not more_f and not more_b:
            break
        picked_f |= more_f
        picked_b |= more_b

    def order_key(block, carry: dict) -> tuple[int, int]:
        found = pages_of(block, lines)
        page = min(found) if found else carry.get("page", 0)
        positions = [
            lines[line][1] for line in lines_of(block) if lines and line in lines and lines[line][0] == page
        ]
        position = min(positions) if positions else carry.get((page, "pos"), -1)
        carry["page"], carry[(page, "pos")] = page, position
        return (page, position)

    carry: dict = {}
    kept = [(order_key(block, carry), 0, n, block) for n, block in enumerate(base_body) if n not in picked_b]
    carry = {}
    taken = [
        (order_key(block, carry), 1, n, copy.deepcopy(block))
        for n, block in enumerate(fresh_body)
        if n in picked_f
    ]
    merged = [
        block for _k, _s, _n, block in sorted([*kept, *taken], key=lambda row: (row[0], row[1], row[2]))
    ]
    content = doc.content_of(base)
    new = dict(base)
    new["content"] = [*content[: doc.preamble_end(content)], *merged]
    attrs = dict(new.get("attrs") or {})
    if UNKNOWN_PAGES in attrs:
        attrs[UNKNOWN_PAGES] = sorted(unknown_pages(base) - wanted)
    new["attrs"] = attrs
    return new
