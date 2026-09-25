"""The manuscript document cut into chapters, and the edits made on it (PHASE5_SPEC §0 D40, §3).

Pure functions on the ProseMirror JSON of `editor.Manuscript.document` (PHASE4_SPEC §2.8): no ORM, no
I/O. `editor.services` wraps them with the database (locks, versions, snapshots) and the book model
(`publishing.model`) reads the same chapters, so a chapter id means the same thing in the editor, the
chapters panel and the page preview.

**Chapters (D40).** The leading `title` node(s) are the book's front matter and belong to no chapter.
After them, every level-1 heading starts a chapter (its id is the heading's id); blocks before the first
heading form a `front` chapter. A book without any level-1 heading is cut into `section`s of about
`SECTION_PAGES` source pages («القسم 1، 2…»): a section closes before the first block whose first source
page is `SECTION_PAGES` or more after the section's first page. Such a chapter's id is its first block's.

**Schema accepted from the editor** (Phase 4 nodes plus the Phase 5 styles):

- blocks: `title` {text, author} (optional inline content), `heading` {level 1–6, id, …}, `paragraph`
  {id, style: null | "quote" | "verse" | "center", …}, `separator` {id} (alias `horizontalRule`),
  `blockquote` (paragraphs inside; its paragraphs read as quotes); every block may carry
  `breakBefore` («ابدأ صفحة جديدة») and `keepWithNext` («مع التالية»), booleans (D47)
- inline: `text` with marks `bold`, `italic`, `uncertain`; `footnote` {id, number, marker, sourcePage,
  sourceLineIds, orphan} holding text / `hardBreak`; `pageBreak` {page, printed} (a source page mark,
  not a printed page break); `hardBreak`

**Versions.** A chapter's version is a short hash of its nodes: a stale editor tab is refused (409) only
when the chapter really changed, and saving one chapter never conflicts with another.

**Ids.** Blocks and notes keep their ids (`h` / `p` / `n` + a source line id from assembly). A saved
chapter's missing or duplicated ids are repaired deterministically: a duplicate becomes `<id>-2`, `-3`…,
a missing block id `e<n>`, a missing note id `ne<n>` (the smallest unused `n`).

**Plain text and offsets (D47).** `inline_text(content)` is a block's (or a note's) text as the page
layout counts it: text as it is, a hard break `"\\n"`, a footnote call or a scan page mark one U+FFFC. An
offset into it is the ProseMirror offset inside the textblock. Offsets that leave the server (the layout's
`start` / `end`, the uncertain words) are in UTF-16 code units, the unit of JavaScript strings and of
ProseMirror positions (`utf16_index` / `code_index` convert; identical for text without astral
characters). `replace_range` / `unmark_range` edit a container's inline content by such offsets.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass

from core.arabic import strip_tashkeel, to_western_digits

# ====================================================================== schema

TITLE = "title"
HEADING = "heading"
PARAGRAPH = "paragraph"
SEPARATOR = "separator"
BLOCKQUOTE = "blockquote"
BLOCK_TYPES: frozenset[str] = frozenset({TITLE, HEADING, PARAGRAPH, SEPARATOR, "horizontalRule", BLOCKQUOTE})
INLINE_TYPES: frozenset[str] = frozenset({"text", "footnote", "pageBreak", "hardBreak"})
NOTE_INLINE_TYPES: frozenset[str] = frozenset({"text", "hardBreak"})
MARK_TYPES: frozenset[str] = frozenset({"bold", "italic", "uncertain"})
PARAGRAPH_STYLES: tuple[str, ...] = ("quote", "verse", "center")
BLOCK_FLAGS: tuple[str, ...] = ("breakBefore", "keepWithNext")  # D47 page-break attrs (booleans)
OBJECT = "￼"  # a footnote call or a scan page mark in a block's plain text (one position)
BREAK = "\n"  # a hard line break in a block's plain text

SECTION_PAGES = 30
MAX_CHAPTER_BYTES = 8 * 1024 * 1024
MAX_NODES = 200_000

FRONT_TITLE = "قبل الفصل الأول"
UNTITLED = "(فصل بلا عنوان)"

_RE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class DocumentError(ValueError):
    """Content the editor cannot save; the message is Arabic (API 400)."""


# ====================================================================== reading


def content_of(document) -> list:
    """The top-level nodes of a document (an empty list for anything that is not a document)."""
    if not isinstance(document, dict):
        return []
    content = document.get("content")
    return content if isinstance(content, list) else []


def attrs_of(node) -> dict:
    """A node's attrs (an empty dict when missing or malformed)."""
    attrs = node.get("attrs") if isinstance(node, dict) else None
    return attrs if isinstance(attrs, dict) else {}


def preamble_end(content: list) -> int:
    """Index after the leading `title` nodes (the front matter that belongs to no chapter)."""
    index = 0
    while index < len(content) and isinstance(content[index], dict) and content[index].get("type") == TITLE:
        index += 1
    return index


def is_chapter_heading(node) -> bool:
    """True for a level-1 heading (it starts a chapter)."""
    if not isinstance(node, dict) or node.get("type") != HEADING:
        return False
    level = attrs_of(node).get("level", 1)
    return level in (1, None, "1")


def plain_text(node) -> str:
    """The text of a node without its footnotes (headings, titles), whitespace collapsed."""
    parts: list[str] = []

    def walk(item) -> None:
        if not isinstance(item, dict):
            return
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind == "hardBreak":
            parts.append(" ")
        elif kind == "footnote":
            return
        for child in item.get("content") or []:
            walk(child)

    walk(node)
    return " ".join("".join(parts).split())


def source_pages(node) -> list[int]:
    """Source page numbers of a block (its `sourcePages`, those of a blockquote's paragraphs)."""
    pages: list[int] = []
    for value in attrs_of(node).get("sourcePages") or []:
        if isinstance(value, int) and not isinstance(value, bool):
            pages.append(value)
    if isinstance(node, dict) and node.get("type") == BLOCKQUOTE:
        for child in node.get("content") or []:
            pages.extend(source_pages(child))
    return pages


def source_lines(nodes: list) -> set[int]:
    """Source line ids of the blocks in `nodes` (a blockquote's paragraphs included)."""
    out: set[int] = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for value in attrs_of(node).get("sourceLineIds") or []:
            if isinstance(value, int) and not isinstance(value, bool):
                out.add(value)
        if node.get("type") == BLOCKQUOTE:
            out |= source_lines(node.get("content") or [])
    return out


def node_id(node) -> str:
    """A block's id ('' when it has none)."""
    value = attrs_of(node).get("id")
    return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class ChapterSlice:
    """One chapter of a document: `content[start:end]` (indexes into the document's top-level nodes).

    `kind` is `chapter` (starts with a level-1 heading), `front` (the blocks before the first heading)
    or `section` (a cut of a book without headings); `title` is the heading's text, «قبل الفصل الأول»
    or «القسم n»; `heading` is the text of the chapter's own heading ('' for front / section).
    """

    id: str
    number: int
    kind: str
    title: str
    heading: str
    start: int
    end: int

    def nodes(self, document) -> list:
        """The chapter's nodes in `document`."""
        return content_of(document)[self.start : self.end]


def chapters_of(document) -> list[ChapterSlice]:
    """The chapters of a document in order (D40); empty when the document has no blocks."""
    content = content_of(document)
    start = preamble_end(content)
    heads = [i for i in range(start, len(content)) if is_chapter_heading(content[i])]
    cuts: list[tuple[int, str]] = []  # (start index, kind)
    if heads:
        if heads[0] > start:
            cuts.append((start, "front"))
        cuts.extend((i, "chapter") for i in heads)
    elif start < len(content):
        cuts.extend((i, "section") for i in _section_starts(content, start))
    chapters: list[ChapterSlice] = []
    seen: set[str] = set()
    for number, (begin, kind) in enumerate(cuts, start=1):
        end = cuts[number][0] if number < len(cuts) else len(content)
        first = content[begin]
        heading = plain_text(first) if kind == "chapter" else ""
        if kind == "chapter":
            title = heading or UNTITLED
        elif kind == "front":
            title = FRONT_TITLE
        else:
            title = f"القسم {number}"
        chapter_id = node_id(first)
        if not _RE_ID.fullmatch(chapter_id or ""):
            chapter_id = f"c{begin}"
        chapter_id = _unique(chapter_id, seen)
        seen.add(chapter_id)
        chapters.append(ChapterSlice(chapter_id, number, kind, title, heading, begin, end))
    return chapters


def _section_starts(content: list, start: int) -> list[int]:
    """Where the sections of a book without headings start (about `SECTION_PAGES` source pages each)."""
    starts = [start]
    first_page: int | None = None
    for index in range(start, len(content)):
        pages = source_pages(content[index])
        page = min(pages) if pages else None
        if page is None:
            continue
        if first_page is None:
            first_page = page
        elif page >= first_page + SECTION_PAGES and index > starts[-1]:
            starts.append(index)
            first_page = page
    return starts


def _unique(value: str, seen: set[str]) -> str:
    if value not in seen:
        return value
    k = 2
    while f"{value}-{k}" in seen:
        k += 1
    return f"{value}-{k}"


def find_chapter(document, chapter_id: str) -> ChapterSlice | None:
    """The chapter with this id (None when there is none)."""
    return next((chapter for chapter in chapters_of(document) if chapter.id == chapter_id), None)


def chapter_version(nodes: list) -> str:
    """The version of a chapter: 12 hex digits of a hash of its nodes (canonical JSON)."""
    raw = json.dumps(nodes, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def chapter_pages(nodes: list) -> list[int]:
    """Sorted source page numbers of a chapter's blocks and notes."""
    pages: set[int] = set(p for node in nodes for p in source_pages(node))
    for _block, note, _content in iter_containers(nodes):
        if note is not None:
            page = attrs_of(note).get("sourcePage")
            if isinstance(page, int) and not isinstance(page, bool):
                pages.add(page)
    return sorted(pages)


def word_count(nodes: list) -> int:
    """Words in a chapter: its text and its notes' text (whitespace separated)."""
    total = 0
    for _block, _note, content in iter_containers(nodes):
        parts = [
            str(n.get("text") or "") if n.get("type") == "text" else " "
            for n in content
            if isinstance(n, dict)
        ]
        total += len("".join(parts).split())
    return total


def document_stats(document) -> dict:
    """Counts of a document (the manuscript view's stats rows that the document itself can tell)."""
    nodes = content_of(document)[preamble_end(content_of(document)) :]
    flat = list(flat_blocks(nodes))
    notes = sum(1 for _b, note, _c in iter_containers(nodes) if note is not None)
    return {
        "headings": sum(1 for n in flat if n.get("type") == HEADING),
        "chapters": sum(1 for n in flat if is_chapter_heading(n)),
        "paragraphs": sum(1 for n in flat if n.get("type") == PARAGRAPH),
        "footnotes": notes,
        "words": word_count(nodes),
    }


def flat_blocks(nodes: list) -> Iterator[dict]:
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("type") == BLOCKQUOTE:
            yield from flat_blocks(node.get("content") or [])
        else:
            yield node


def iter_containers(nodes: list) -> Iterator[tuple[dict, dict | None, list]]:
    """Every list of inline nodes in `nodes`: `(block, None, block content)` for each block and
    `(block, note, note content)` for each footnote inside it (blockquotes are walked into)."""
    for block in flat_blocks(nodes):
        content = block.get("content")
        if not isinstance(content, list):
            continue
        yield block, None, content
        for item in content:
            if isinstance(item, dict) and item.get("type") == "footnote":
                inner = item.get("content")
                if isinstance(inner, list):
                    yield block, item, inner


def block_ids(nodes: list) -> set[str]:
    """Ids of the blocks (and blockquote paragraphs) in `nodes`."""
    return {node_id(block) for block in flat_blocks(nodes) if node_id(block)} | {
        node_id(node) for node in nodes if isinstance(node, dict) and node_id(node)
    }


def note_ids(nodes: list) -> set[str]:
    """Ids of the footnotes in `nodes`."""
    return {node_id(note) for _b, note, _c in iter_containers(nodes) if note is not None and node_id(note)}


def all_ids(nodes: list) -> set[str]:
    """Block and note ids of `nodes`."""
    return block_ids(nodes) | note_ids(nodes)


def has_uncertain(block: dict) -> bool:
    """True when a block (or one of its notes) still carries an `uncertain` mark."""
    for _b, _note, content in iter_containers([block]):
        for item in content:
            if isinstance(item, dict) and any(
                isinstance(m, dict) and m.get("type") == "uncertain" for m in item.get("marks") or []
            ):
                return True
    return False


# ====================================================================== plain text and offsets (D47)


def inline_text(content) -> str:
    """The plain text of a list of inline nodes (see the module docstring): text as is, a hard break
    `"\\n"`, a footnote call or a scan page mark one U+FFFC (`OBJECT`)."""
    parts: list[str] = []
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind == "hardBreak":
            parts.append(BREAK)
        elif kind in ("footnote", "pageBreak"):
            parts.append(OBJECT)
    return "".join(parts)


def block_text(node) -> str:
    """A block's (or a note's) plain text: `inline_text` of its content ('' for a separator)."""
    return inline_text(node.get("content") if isinstance(node, dict) else None)


def object_kinds(content) -> str:
    """`inline_text` with each object told apart: a footnote call stays U+FFFC, a scan page mark becomes
    U+0000 (the layout aligner skips page marks but matches calls; the positions are the same)."""
    parts: list[str] = []
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind == "hardBreak":
            parts.append(BREAK)
        elif kind == "footnote":
            parts.append(OBJECT)
        elif kind == "pageBreak":
            parts.append("\x00")
    return "".join(parts)


def _astral(text: str) -> bool:
    return any(ord(char) > 0xFFFF for char in text)


def utf16_index(text: str, index: int) -> int:
    """Code point index `index` in `text` → the same position in UTF-16 code units."""
    index = max(0, min(int(index), len(text)))
    if not _astral(text):
        return index
    return index + sum(1 for char in text[:index] if ord(char) > 0xFFFF)


def code_index(text: str, units: int) -> int:
    """UTF-16 position `units` in `text` → the code point index (a unit inside a surrogate pair rounds
    down to the character's start)."""
    units = max(0, int(units))
    if not _astral(text):
        return min(units, len(text))
    count = 0
    for i, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if count + width > units:
            return i
        count += width
    return len(text)


def _pieces(content: list) -> list[tuple[str, object]]:
    """A container's inline content as positions: `("c", (char, marks))` per text character, `("n",
    node)` per inline node (a hard break, a call, a page mark)."""
    out: list[tuple[str, object]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            marks = item.get("marks") or []
            out.extend(("c", (char, marks)) for char in str(item.get("text") or ""))
        else:
            out.append(("n", item))
    return out


def _rebuild(pieces: list[tuple[str, object]]) -> list:
    """Inline content from positions (neighbouring characters with the same marks merged)."""
    out: list = []
    chars: list[tuple[str, list]] = []
    for kind, value in pieces:
        if kind == "c":
            chars.append(value)  # type: ignore[arg-type]
            continue
        if chars:
            out.extend(_text_nodes(chars))
            chars = []
        out.append(value)
    if chars:
        out.extend(_text_nodes(chars))
    return out


def _without(marks, mark: str) -> list:
    return [m for m in marks or [] if not (isinstance(m, dict) and m.get("type") == mark)]


def has_mark_range(content: list, start: int, end: int, mark: str) -> bool:
    """True when every text character in `[start, end)` (code points of `inline_text`) carries `mark`
    and there is at least one."""
    pieces = _pieces(content)
    chars = [value for kind, value in pieces[start:end] if kind == "c"]
    return bool(chars) and all(
        any(isinstance(m, dict) and m.get("type") == mark for m in marks) for _char, marks in chars
    )


def unmark_range(content: list, start: int, end: int, mark: str = "uncertain") -> list:
    """`content` with `mark` taken off the characters in `[start, end)` (a new list)."""
    pieces = _pieces(content)
    for i in range(max(0, start), min(end, len(pieces))):
        kind, value = pieces[i]
        if kind == "c":
            char, marks = value  # type: ignore[misc]
            pieces[i] = ("c", (char, _without(marks, mark)))
    return _rebuild(pieces)


def replace_range(content: list, start: int, end: int, text: str, drop: str = "uncertain") -> list:
    """`content` with the characters in `[start, end)` replaced by `text` (a new list). The new text takes
    the marks of the first character replaced, less `drop` (a corrected word counts as resolved); inline
    nodes inside the range are kept after the new text."""
    pieces = _pieces(content)
    start, end = max(0, start), min(end, len(pieces))
    inside = pieces[start:end]
    marks = next((value[1] for kind, value in inside if kind == "c"), [])  # type: ignore[index]
    marks = _without(marks, drop)
    new = [("c", (char, marks)) for char in text]
    kept = [(kind, value) for kind, value in inside if kind == "n"]
    return _rebuild([*pieces[:start], *new, *kept, *pieces[end:]])


# ====================================================================== writing


def splice(document: dict, chapter: ChapterSlice, nodes: list) -> dict:
    """A copy of `document` with `chapter`'s nodes replaced by `nodes` (the top level is copied, the
    other nodes are shared)."""
    content = content_of(document)
    out = dict(document) if isinstance(document, dict) else {"type": "doc"}
    out["type"] = "doc"
    out["content"] = [*content[: chapter.start], *nodes, *content[chapter.end :]]
    return out


def as_nodes(content) -> list:
    """The posted chapter content as a list of block nodes (a `doc` node or a plain list)."""
    if isinstance(content, dict) and content.get("type") == "doc":
        content = content.get("content")
    if not isinstance(content, list):
        raise DocumentError("محتوى الفصل غير صالح.")
    return content


def clean_nodes(content) -> list:
    """Validate the editor's chapter content and return it as a list of block nodes (a deep copy).

    Raises `DocumentError` (Arabic) for an unknown node or mark, a malformed attribute, an empty text
    node or content beyond the size limits. Nothing is rewritten here besides the copy; ids are
    repaired by `repair_ids`.
    """
    nodes = as_nodes(content)
    try:
        size = len(json.dumps(nodes, ensure_ascii=False, separators=(",", ":")).encode())
    except (TypeError, ValueError):
        raise DocumentError("محتوى الفصل غير صالح.") from None
    if size > MAX_CHAPTER_BYTES:
        raise DocumentError("الفصل أكبر من الحد المسموح؛ قسّمه بعنوان رئيسي قبل الحفظ.")
    counter = [0]
    for node in nodes:
        _check_block(node, counter, top=True)
    return copy.deepcopy(nodes)


def _count(counter: list[int]) -> None:
    counter[0] += 1
    if counter[0] > MAX_NODES:
        raise DocumentError("الفصل أكبر من الحد المسموح؛ قسّمه بعنوان رئيسي قبل الحفظ.")


def _check_attrs(node: dict) -> dict:
    attrs = node.get("attrs", {})
    if attrs is None:
        return {}
    if not isinstance(attrs, dict):
        raise DocumentError("خصائص إحدى الفقرات غير صالحة.")
    value = attrs.get("id")
    if value not in (None, "") and not (isinstance(value, str) and _RE_ID.fullmatch(value)):
        raise DocumentError("معرّف إحدى الفقرات غير صالح.")
    return attrs


def _check_block(node, counter: list[int], top: bool) -> None:
    _count(counter)
    if not isinstance(node, dict) or not isinstance(node.get("type"), str):
        raise DocumentError("محتوى الفصل غير صالح.")
    kind = node["type"]
    if kind not in BLOCK_TYPES:
        raise DocumentError(f"نوع فقرة غير معروف: {kind[:40]}.")
    attrs = _check_attrs(node)
    for flag in BLOCK_FLAGS:
        if attrs.get(flag) is not None and not isinstance(attrs.get(flag), bool):
            raise DocumentError("خاصية فاصل الصفحة غير صالحة.")
    content = node.get("content", [])
    if content is None:
        content = []
    if not isinstance(content, list):
        raise DocumentError("محتوى الفصل غير صالح.")
    if kind == BLOCKQUOTE:
        if not top:
            raise DocumentError("الاقتباس داخل اقتباس غير مدعوم.")
        for child in content:
            if not isinstance(child, dict) or child.get("type") != PARAGRAPH:
                raise DocumentError("الاقتباس يحوي فقرات فقط.")
            _check_block(child, counter, top=False)
        return
    if kind in (SEPARATOR, "horizontalRule"):
        if content:
            raise DocumentError("الفاصل لا يحوي نصًّا.")
        return
    if kind == HEADING:
        level = attrs.get("level", 1)
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 6:
            raise DocumentError("مستوى العنوان غير صالح.")
    if kind == PARAGRAPH:
        style = attrs.get("style")
        if style not in (None, "", *PARAGRAPH_STYLES):
            raise DocumentError("نمط الفقرة غير معروف.")
    if kind == TITLE:
        for key in ("text", "author"):
            if attrs.get(key) is not None and not isinstance(attrs.get(key), str):
                raise DocumentError("عنوان الكتاب غير صالح.")
    for item in content:
        _check_inline(item, counter, INLINE_TYPES)


def _check_inline(node, counter: list[int], allowed: frozenset[str]) -> None:
    _count(counter)
    if not isinstance(node, dict) or node.get("type") not in allowed:
        kind = node.get("type") if isinstance(node, dict) else None
        raise DocumentError(f"عنصر غير معروف داخل الفقرة: {str(kind)[:40]}.")
    kind = node["type"]
    marks = node.get("marks", [])
    if marks is None:
        marks = []
    if not isinstance(marks, list) or any(
        not isinstance(m, dict) or m.get("type") not in MARK_TYPES for m in marks
    ):
        raise DocumentError("تنسيق غير معروف في النص.")
    if kind == "text":
        text = node.get("text")
        if not isinstance(text, str) or not text:
            raise DocumentError("عقدة نص فارغة.")
        return
    attrs = _check_attrs(node)
    if kind == "pageBreak":
        page = attrs.get("page")
        if page is not None and (isinstance(page, bool) or not isinstance(page, int)):
            raise DocumentError("رقم الصفحة الأصلية غير صالح.")
    if kind == "footnote":
        content = node.get("content", [])
        if content is None:
            content = []
        if not isinstance(content, list):
            raise DocumentError("نص الحاشية غير صالح.")
        for item in content:
            _check_inline(item, counter, NOTE_INLINE_TYPES)


def repair_ids(nodes: list, taken: set[str]) -> list:
    """Give every block and note of `nodes` a unique id (in place; returns `nodes`).

    `taken` are the ids used elsewhere in the document. A duplicate becomes `<id>-2`, `<id>-3`…; a
    missing block id `e<n>` and a missing note id `ne<n>`, with the smallest unused `n` — the same
    content always gets the same ids.
    """
    used = set(taken)

    def fresh(prefix: str) -> str:
        n = 1
        while f"{prefix}{n}" in used:
            n += 1
        return f"{prefix}{n}"

    def fix(node: dict, prefix: str) -> None:
        attrs = node.get("attrs")
        if not isinstance(attrs, dict):
            attrs = {}
            node["attrs"] = attrs
        value = attrs.get("id")
        if isinstance(value, str) and value:
            if value in used:
                value = _unique(value, used)
        else:
            value = fresh(prefix)
        attrs["id"] = value
        used.add(value)

    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("type") == TITLE:
            continue
        fix(node, "e")
        if node.get("type") == BLOCKQUOTE:
            for child in node.get("content") or []:
                if isinstance(child, dict):
                    fix(child, "e")
    for _block, note, _content in iter_containers(nodes):
        if note is not None:
            fix(note, "ne")
    return nodes


# ====================================================================== find & replace

ALEF_FORMS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ٲ": "ا", "ٳ": "ا"})
# Diacritics and tatweel: the characters `core.arabic.strip_tashkeel` removes (comparison only).
_DROPPED: frozenset[str] = frozenset(c for c in map(chr, range(0x0600, 0x0700)) if not strip_tashkeel(c))


@dataclass(frozen=True)
class FindOptions:
    """How `find_in_nodes` matches: diacritics must match or are ignored, alef forms folded, whole word."""

    match_tashkeel: bool = False
    fold_alef: bool = True
    whole_word: bool = False


def _is_mark(char: str) -> bool:
    """True for a combining mark (a haraka, tanween, shadda, sukun, a Quranic mark…)."""
    return unicodedata.combining(char) > 0


def fold(text: str, options: FindOptions) -> tuple[str, list[int]]:
    """`text` as compared (diacritics dropped unless they must match, alef forms folded, lower case)
    and the index in `text` of each character kept.

    When diacritics must match, the marks on one letter are put in canonical order (shadda + fatha
    typed either way compare equal), as Unicode normalisation would, without changing the letters."""
    out: list[str] = []
    index: list[int] = []
    run_start = 0  # where the current run of marks starts in `out`
    for i, char in enumerate(text):
        if not options.match_tashkeel and char in _DROPPED:
            continue
        if options.fold_alef:
            char = char.translate(ALEF_FORMS)
        low = char.lower()
        out.append(low if len(low) == 1 else char)
        index.append(i)
        if options.match_tashkeel:
            if not _is_mark(char):
                run_start = len(out)
            elif len(out) - run_start > 1:
                pairs = sorted(
                    zip(out[run_start:], index[run_start:], strict=True),
                    key=lambda pair: unicodedata.combining(pair[0]),
                )
                out[run_start:] = [c for c, _ in pairs]
                index[run_start:] = [k for _, k in pairs]
    return "".join(out), index


def fold_query(query: str, options: FindOptions) -> str:
    """The query as compared; raises `DocumentError` when nothing is left to search."""
    folded, _ = fold(str(query or ""), options)
    if not folded.strip():
        raise DocumentError("اكتب نصًّا للبحث.")
    return folded


@dataclass(frozen=True)
class Match:
    """A match in one container: `index` / `length` in the container's text (its text nodes joined)."""

    block: str
    note: str | None
    index: int
    length: int


def _segments(content: list) -> list[tuple[int, list[int]]]:
    """Runs of consecutive text nodes: `(offset in the container text, [node indexes])`."""
    runs: list[tuple[int, list[int]]] = []
    offset = 0
    current: list[int] | None = None
    for i, node in enumerate(content):
        if isinstance(node, dict) and node.get("type") == "text":
            if current is None:
                current = []
                runs.append((offset, current))
            current.append(i)
            offset += len(str(node.get("text") or ""))
        else:
            current = None
    return runs


def _segment_matches(text: str, query: str, options: FindOptions) -> list[tuple[int, int]]:
    """`(start, end)` of the matches of the folded `query` in `text` (original indexes)."""
    folded, index = fold(text, options)
    if query not in folded:
        return []
    out: list[tuple[int, int]] = []
    pos = folded.find(query)
    while pos >= 0:
        end = pos + len(query)
        if (
            options.whole_word
            and ((pos > 0 and folded[pos - 1].isalnum()) or (end < len(folded) and folded[end].isalnum()))
        ) or (
            # diacritics must match: a letter that carries more marks than the query's is another
            # reading («حت» is not in «حتّى»)
            options.match_tashkeel and end < len(folded) and _is_mark(folded[end])
        ):
            pos = folded.find(query, pos + 1)
            continue
        start = min(index[pos:end])
        stop = max(index[pos:end]) + 1
        if not options.match_tashkeel:
            while stop < len(text) and text[stop] in _DROPPED:
                stop += 1  # the diacritics of the last letter go with the match
        out.append((start, stop))
        pos = folded.find(query, end)
    return out


def find_in_nodes(nodes: list, query: str, options: FindOptions) -> list[Match]:
    """Every match of `query` in the chapter's text and notes, in document order.

    A match never spans a footnote, a page mark or a line break (it lies in one run of text nodes).
    """
    folded_query = fold_query(query, options)
    out: list[Match] = []
    for block, note, content in iter_containers(nodes):
        for offset, members in _segments(content):
            text = "".join(str(content[i].get("text") or "") for i in members)
            for start, stop in _segment_matches(text, folded_query, options):
                out.append(
                    Match(
                        node_id(block),
                        node_id(note) if note is not None else None,
                        offset + start,
                        stop - start,
                    )
                )
    return out


def _marks_key(marks) -> str:
    return json.dumps(marks or [], sort_keys=True, ensure_ascii=False)


def replace_in_nodes(nodes: list, query: str, replacement: str, options: FindOptions) -> int:
    """Replace every match of `query` by `replacement` (in place); returns the number replaced.

    The replacement takes the marks of the match's first character, less `uncertain` (a replaced word
    counts as resolved). Empty text nodes are removed; neighbours with the same marks are merged.
    """
    folded_query = fold_query(query, options)
    replacement = str(replacement or "")
    count = 0
    for _block, _note, content in list(iter_containers(nodes)):
        rebuilt: list = []
        cursor = 0
        changed = False
        for _offset, members in _segments(content):
            text = "".join(str(content[i].get("text") or "") for i in members)
            spans = _segment_matches(text, folded_query, options)
            rebuilt.extend(content[cursor : members[0]])
            cursor = members[-1] + 1
            if not spans:
                rebuilt.extend(content[i] for i in members)
                continue
            changed = True
            count += len(spans)
            chars: list[tuple[str, list]] = []
            for i in members:
                marks = content[i].get("marks") or []
                chars.extend((char, marks) for char in str(content[i].get("text") or ""))
            pieces: list[tuple[str, list]] = []
            pos = 0
            for start, stop in spans:
                pieces.extend(chars[pos:start])
                marks = [
                    m for m in chars[start][1] if not (isinstance(m, dict) and m.get("type") == "uncertain")
                ]
                pieces.extend((char, marks) for char in replacement)
                pos = stop
            pieces.extend(chars[pos:])
            rebuilt.extend(_text_nodes(pieces))
        rebuilt.extend(content[cursor:])
        if changed:
            content[:] = rebuilt
    return count


def _text_nodes(pieces: list[tuple[str, list]]) -> list[dict]:
    """Text nodes from `(char, marks)` pieces, merging neighbours with the same marks."""
    out: list[dict] = []
    key = None
    buffer: list[str] = []
    marks_now: list = []

    def flush() -> None:
        if buffer:
            node: dict = {"type": "text", "text": "".join(buffer)}
            if marks_now:
                node["marks"] = copy.deepcopy(marks_now)
            out.append(node)

    for char, marks in pieces:
        k = _marks_key(marks)
        if k != key:
            flush()
            buffer = []
            key = k
            marks_now = list(marks)
        buffer.append(char)
    flush()
    return out


# ====================================================================== digits

_TO_ARABIC_INDIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
DIGIT_STYLES: tuple[str, ...] = ("western", "arabic_indic")


def digits_to(text: str, style: str) -> str:
    """`text` with its digits in `style` (Western 0-9 or Arabic-Indic ٠-٩; Persian digits too)."""
    western = to_western_digits(text)
    return western.translate(_TO_ARABIC_INDIC) if style == "arabic_indic" else western


def convert_digits_in_nodes(nodes: list, style: str) -> int:
    """Convert the digits of the text and of the footnote markers (in place); returns the number of
    digits changed."""
    if style not in DIGIT_STYLES:
        raise DocumentError("نمط الأرقام غير معروف.")
    changed = 0
    for _block, note, content in iter_containers(nodes):
        if note is not None:
            attrs = attrs_of(note)
            marker = attrs.get("marker")
            if isinstance(marker, str) and marker:
                new = digits_to(marker, style)
                changed += sum(1 for a, b in zip(marker, new, strict=True) if a != b)
                attrs["marker"] = new
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = str(item.get("text") or "")
                new = digits_to(text, style)
                if new != text:
                    changed += sum(1 for a, b in zip(text, new, strict=True) if a != b)
                    item["text"] = new
    return changed
