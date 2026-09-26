"""The book model (PHASE5_SPEC §3, D43): one neutral structure for every renderer.

`book_model(document, stylesheet)` turns the manuscript (ProseMirror JSON, PHASE4_SPEC §2.8 plus the
Phase 5 styles, see `editor.document`) into plain dataclasses that know nothing of HTML, CSS or Word:

    Book(front, chapters[Chapter(id, title, blocks[Block(style, runs[Run(text, marks) | NoteRef |
         LineBreak | SourceMark], footnotes[Footnote])])], setup)

- **Blocks carry style names only**, never direct formatting (`STYLES`: `chapter-title`, `body`, `quote`,
  …). The HTML renderer maps a style to a class and the stylesheet's CSS, the Word renderer
  (`publishing.word`, Phase 6) to a real Word style id (`Style.word`: `Heading1`, `Normal`,
  `FootnoteText`, …), so the Word navigation pane, its table of contents and its footnote pane work.
- **Runs** are text with typographic marks (`bold`, `italic`); editorial marks (`uncertain`) are dropped
  unless `book_model(…, editorial=True)` asks for them (the Word export's comments on uncertain words).
  A footnote call is a `NoteRef` in the runs (Word: `w:footnoteReference`); a hard line break a
  `LineBreak`; a scan page mark a `SourceMark` (printed in the margin only when the stylesheet asks).
- **Footnotes are objects** attached to the block that calls them, in call order, with their own runs and
  their number in the chapter and in the book. Per-page numbering (D46, the default) is decided by the
  renderer: two passes in the PDF, `w:numRestart eachPage` in Word.
- **Chapters** are the editor's chapters (D40, `editor.document.chapters_of`): the same ids in the
  editor, the chapters panel, the preview's page ranges and, later, Word sections (one per chapter:
  recto openings as `oddPage` section breaks, per-chapter running headers).
- **Page setup** (`PageSetup`) is the stylesheet as plain values: trim, mirrored margins (inner / outer),
  faces (registry keys), sizes, running header, page numbers, openings, widows / orphans, headings kept
  with the next line, front matter and the book details (D47: title and copyright pages).
- **Page layout (D47)**: every block and note keeps its plain text (`Block.plain`, `Footnote.plain`,
  `editor.document.object_kinds`), so the layout export can give each laid-out line its character range;
  `break_before` / `keep_with_next` come from the block's `breakBefore` / `keepWithNext` attrs.

Empty paragraphs are left out (spacing comes from the styles). `direction_runs` splits a run's text into
right-to-left and left-to-right pieces for renderers that need explicit direction (Word's `w:rtl`).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from editor import document as doc

# ====================================================================== styles


@dataclass(frozen=True)
class Style:
    """A block style: its Arabic name (the editor's style picker) and the Word style id it maps to
    (PHASE6_SPEC §4.3; `publishing.word.styles` defines every id)."""

    key: str
    label: str
    word: str


STYLES: dict[str, Style] = {
    style.key: style
    for style in (
        Style("book-title", "عنوان الكتاب", "Title"),
        Style("book-author", "المؤلف", "NkAuthor"),
        Style("contents-title", "عنوان المحتويات", "TOCHeading"),
        Style("contents-1", "مدخل فصل في المحتويات", "TOC1"),
        Style("contents-2", "مدخل عنوان فرعي في المحتويات", "TOC2"),
        Style("chapter-title", "عنوان فصل", "Heading1"),
        Style("section-title", "عنوان فرعي", "Heading2"),
        Style("body", "فقرة", "Normal"),
        Style("quote", "اقتباس", "Quote"),
        Style("verse", "شعر", "NkVerse"),
        Style("center", "ملاحظة وسط", "NkCenter"),
        Style("separator", "فاصل", "NkSeparator"),
        Style("footnote-text", "حاشية", "FootnoteText"),
    )
}
PARAGRAPH_STYLE_OF: dict[str | None, str] = {
    None: "body",
    "": "body",
    "quote": "quote",
    "verse": "verse",
    "center": "center",
}
TYPOGRAPHIC_MARKS: dict[str, str] = {"bold": "bold", "strong": "bold", "italic": "italic", "em": "italic"}
EDITORIAL_MARKS: dict[str, str] = {"uncertain": "uncertain"}  # kept only with `editorial=True`
SEPARATOR_TEXT = "* * *"
# The book details of `StyleSheet.front_matter["fields"]` (D47), in the order the title and copyright pages
# print them; the title and the author default from the Book.
BOOK_FIELDS: tuple[str, ...] = (
    "title",
    "subtitle",
    "author",
    "editor",
    "translator",
    "publisher",
    "city",
    "year",
    "edition",
    "isbn",
    "rights",
)


# ====================================================================== the structure


@dataclass(frozen=True)
class Run:
    """Text with typographic marks (`bold`, `italic`, sorted)."""

    text: str
    marks: tuple[str, ...] = ()


@dataclass(frozen=True)
class NoteRef:
    """The call of footnote `note` (its id) at this point of the text."""

    note: str


@dataclass(frozen=True)
class LineBreak:
    """A line break inside a block (a verse's hemistichs, a line of an address)."""


@dataclass(frozen=True)
class SourceMark:
    """The start of scan page `page` (`printed`: its printed number when known)."""

    page: int
    printed: str = ""


Inline = Run | NoteRef | LineBreak | SourceMark


@dataclass
class Footnote:
    """A footnote: its runs, its number in the chapter and in the book, and where it came from."""

    id: str
    runs: list[Run | LineBreak]
    number: int
    book_number: int
    source_page: int | None = None
    marker: str = ""
    orphan: bool = False
    plain: str = ""  # the note's text as the layout counts it (`editor.document.object_kinds`)


@dataclass
class Block:
    """A paragraph-level block: a style name, its runs and the footnotes it calls (in call order)."""

    style: str
    runs: list[Inline]
    footnotes: list[Footnote] = field(default_factory=list)
    id: str = ""
    source_pages: tuple[int, ...] = ()
    level: int = 0  # 1 / 2 for chapter and section titles
    plain: str = ""  # the block's text as the layout counts it (`editor.document.object_kinds`)
    break_before: bool = False  # «ابدأ صفحة جديدة» (D47)
    keep_with_next: bool = False  # «مع التالية» (D47)

    def text(self) -> str:
        """The block's text without notes and marks."""
        return "".join(
            run.text if isinstance(run, Run) else " " if isinstance(run, LineBreak) else ""
            for run in self.runs
        )


@dataclass
class Chapter:
    """One chapter (D40): `kind` is `chapter` (it has a heading), `front` or `section` (no heading)."""

    id: str
    number: int
    kind: str
    title: str
    heading: str
    blocks: list[Block]

    @property
    def has_heading(self) -> bool:
        return self.kind == "chapter"


@dataclass
class Front:
    """The front matter: the title page, the copyright page and the contents page, when the stylesheet asks
    for them, with the book details (D47; empty details are left out)."""

    title: str
    author: str
    title_page: bool = True
    contents: bool = True
    copyright_page: bool = False
    subtitle: str = ""
    editor: str = ""
    translator: str = ""
    publisher: str = ""
    city: str = ""
    year: str = ""
    edition: str = ""
    isbn: str = ""
    rights: str = ""


@dataclass(frozen=True)
class PageSetup:
    """The stylesheet as plain values (millimetres, points, multiples of the body size)."""

    width_mm: float = 170.0
    height_mm: float = 240.0
    top_mm: float = 20.0
    bottom_mm: float = 22.0
    inner_mm: float = 22.0
    outer_mm: float = 18.0
    bleed_mm: float = 0.0
    body_font: str = "amiri"
    latin_font: str = "times"
    heading_font: str = "amiri"
    body_size_pt: float = 13.0
    line_height: float = 1.7
    indent_em: float = 1.5
    h1_scale: float = 1.6
    h2_scale: float = 1.25
    footnote_size_pt: float = 10.0
    footnote_numbering: str = "page"
    running_header: str = "none"  # owner, 2026-09-25: no running header unless chosen
    page_number: str = "bottom_center"
    chapter_opening: str = "any"
    title_page: bool = True
    contents: bool = True
    print_source_pages: bool = False
    widows: int = 2
    orphans: int = 2
    keep_headings: bool = True
    copyright_page: bool = False
    details: tuple[tuple[str, str], ...] = ()  # the non-empty book details, `(field, value)` in field order

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def detail(self, name: str) -> str:
        """One book detail ('' when not given)."""
        return next((value for key, value in self.details if key == name), "")


@dataclass(frozen=True)
class ContentsEntry:
    """A line of the table of contents: a chapter (level 1) or a section title (level 2)."""

    level: int
    text: str
    target: str  # the block id of the heading (HTML anchor, Word bookmark)
    chapter: str


@dataclass
class Book:
    """The whole book, ready for a renderer."""

    front: Front
    chapters: list[Chapter]
    setup: PageSetup
    language: str = "ar"
    direction: str = "rtl"

    @property
    def footnote_numbering(self) -> str:
        return self.setup.footnote_numbering

    def blocks(self) -> Iterator[Block]:
        for chapter in self.chapters:
            yield from chapter.blocks

    def footnotes(self) -> Iterator[Footnote]:
        for block in self.blocks():
            yield from block.footnotes

    def contents(self) -> list[ContentsEntry]:
        """Chapter and section titles in order (only headings the owner wrote, never «القسم n»)."""
        out: list[ContentsEntry] = []
        for chapter in self.chapters:
            for block in chapter.blocks:
                if block.style in ("chapter-title", "section-title") and block.text().strip():
                    out.append(
                        ContentsEntry(block.level or 1, " ".join(block.text().split()), block.id, chapter.id)
                    )
        return out


# ====================================================================== building


def _number(value, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value)


def page_setup(stylesheet) -> PageSetup:
    """`PageSetup` from an `editor.StyleSheet`, a dict of its fields, a `PageSetup` (as it is) or None (the
    defaults)."""
    if stylesheet is None:
        return PageSetup()
    if isinstance(stylesheet, PageSetup):
        return stylesheet  # (read field by field it would lose the heading scales and the front matter)
    get = (
        stylesheet.get
        if isinstance(stylesheet, dict)
        else (lambda name, default=None: getattr(stylesheet, name, default))
    )
    base = PageSetup()
    scale = get("heading_scale") or {}
    scale = scale if isinstance(scale, dict) else {}
    front = get("front_matter") or {}
    front = front if isinstance(front, dict) else {}
    values = {
        name: _number(get(name), getattr(base, name))
        for name in (
            "width_mm",
            "height_mm",
            "top_mm",
            "bottom_mm",
            "inner_mm",
            "outer_mm",
            "bleed_mm",
            "body_size_pt",
            "line_height",
            "indent_em",
            "footnote_size_pt",
        )
    }
    for name in (
        "body_font",
        "latin_font",
        "heading_font",
        "footnote_numbering",
        "running_header",
        "page_number",
        "chapter_opening",
    ):
        value = get(name)
        values[name] = value if isinstance(value, str) and value else getattr(base, name)
    fields = front.get("fields") if isinstance(front.get("fields"), dict) else {}
    details = tuple(
        (name, " ".join(str(fields[name]).split()) if name != "rights" else str(fields[name]).strip())
        for name in BOOK_FIELDS
        if isinstance(fields.get(name), str) and fields[name].strip()
    )
    keep = get("keep_headings", base.keep_headings)
    return PageSetup(
        **values,
        h1_scale=_number(scale.get("h1"), base.h1_scale),
        h2_scale=_number(scale.get("h2"), base.h2_scale),
        title_page=bool(front.get("title_page", base.title_page)),
        contents=bool(front.get("contents", base.contents)),
        print_source_pages=bool(get("print_source_pages", base.print_source_pages)),
        widows=_count(get("widows"), base.widows),
        orphans=_count(get("orphans"), base.orphans),
        keep_headings=keep if isinstance(keep, bool) else base.keep_headings,
        copyright_page=bool(front.get("copyright_page", base.copyright_page)),
        details=details,
    )


def _count(value, default: int) -> int:
    """A small line count (widows, orphans): 1 to 9, `default` for anything else."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return max(1, min(9, int(value)))


@dataclass
class _Counters:
    book: int = 0
    chapter: int = 0
    last_page: int | None = None
    editorial: bool = False  # keep the editorial marks (`uncertain`) on the runs


def _marks(node: dict, editorial: bool = False) -> tuple[str, ...]:
    known = {**TYPOGRAPHIC_MARKS, **EDITORIAL_MARKS} if editorial else TYPOGRAPHIC_MARKS
    out = {
        known[m["type"]] for m in node.get("marks") or [] if isinstance(m, dict) and m.get("type") in known
    }
    return tuple(sorted(out))


def _append_run(runs: list, text: str, marks: tuple[str, ...]) -> None:
    """Add text, merging with the previous run when the marks are the same."""
    if not text:
        return
    if runs and isinstance(runs[-1], Run) and runs[-1].marks == marks:
        runs[-1] = Run(runs[-1].text + text, marks)
    else:
        runs.append(Run(text, marks))


def _note_runs(content: list, editorial: bool = False) -> list[Run | LineBreak]:
    runs: list = []
    for item in content or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            _append_run(runs, str(item.get("text") or ""), _marks(item, editorial))
        elif item.get("type") == "hardBreak":
            runs.append(LineBreak())
    return _trim(_tidy_breaks(runs))


def _trim(runs: list) -> list:
    """Runs without leading / trailing whitespace (and without line breaks at the edges)."""
    while runs and (
        isinstance(runs[0], LineBreak) or (isinstance(runs[0], Run) and not runs[0].text.strip())
    ):
        runs.pop(0)
    while runs and (
        isinstance(runs[-1], LineBreak) or (isinstance(runs[-1], Run) and not runs[-1].text.strip())
    ):
        runs.pop()
    if runs and isinstance(runs[0], Run):
        runs[0] = Run(runs[0].text.lstrip(), runs[0].marks)
    if runs and isinstance(runs[-1], Run):
        runs[-1] = Run(runs[-1].text.rstrip(), runs[-1].marks)
    return runs


def _inline(content: list, counters: _Counters, notes: list[Footnote]) -> list[Inline]:
    runs: list[Inline] = []
    for item in content or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            _append_run(runs, str(item.get("text") or ""), _marks(item, counters.editorial))
        elif kind == "hardBreak":
            runs.append(LineBreak())
        elif kind == "pageBreak":
            attrs = doc.attrs_of(item)
            page = attrs.get("page")
            if isinstance(page, int) and not isinstance(page, bool):
                runs.append(SourceMark(page, str(attrs.get("printed") or "")))
                counters.last_page = max(page, counters.last_page or page)
        elif kind == "footnote":
            attrs = doc.attrs_of(item)
            counters.book += 1
            counters.chapter += 1
            note_id = str(attrs.get("id") or f"note-{counters.book}")
            page = attrs.get("sourcePage")
            notes.append(
                Footnote(
                    id=note_id,
                    runs=_note_runs(item.get("content") or [], counters.editorial),
                    number=counters.chapter,
                    book_number=counters.book,
                    source_page=page if isinstance(page, int) and not isinstance(page, bool) else None,
                    marker=str(attrs.get("marker") or ""),
                    orphan=bool(attrs.get("orphan")),
                    plain=doc.object_kinds(item.get("content") or []),
                )
            )
            runs.append(NoteRef(note_id))
    return runs


def _tidy_breaks(runs: list) -> list:
    """No space at a line break: the run before it loses its trailing spaces, the one after it its
    leading ones."""
    for i, run in enumerate(runs):
        if not isinstance(run, LineBreak):
            continue
        if i > 0 and isinstance(runs[i - 1], Run):
            runs[i - 1] = Run(runs[i - 1].text.rstrip(), runs[i - 1].marks)
        if i + 1 < len(runs) and isinstance(runs[i + 1], Run):
            runs[i + 1] = Run(runs[i + 1].text.lstrip(), runs[i + 1].marks)
    return [run for run in runs if not isinstance(run, Run) or run.text]


def _has_content(runs: list) -> bool:
    return any(isinstance(run, NoteRef) or (isinstance(run, Run) and run.text.strip()) for run in runs)


def _block(node: dict, counters: _Counters, style_override: str | None = None) -> list[Block]:
    """The model block(s) of one document block (a blockquote gives one block per paragraph; its own
    `breakBefore` goes to the first, its `keepWithNext` to the last)."""
    kind = node.get("type")
    attrs = doc.attrs_of(node)
    if kind == doc.BLOCKQUOTE:
        out: list[Block] = []
        for child in node.get("content") or []:
            if isinstance(child, dict) and child.get("type") == doc.PARAGRAPH:
                style = "verse" if doc.attrs_of(child).get("style") == "verse" else "quote"
                out.extend(_block(child, counters, style))
        if out:
            out[0].break_before = out[0].break_before or attrs.get("breakBefore") is True
            out[-1].keep_with_next = out[-1].keep_with_next or attrs.get("keepWithNext") is True
        return out
    pages = tuple(sorted(set(doc.source_pages(node))))
    block_id = doc.node_id(node)
    flags = {
        "break_before": attrs.get("breakBefore") is True,
        "keep_with_next": attrs.get("keepWithNext") is True,
    }
    if kind in (doc.SEPARATOR, "horizontalRule"):
        return [Block("separator", [Run(SEPARATOR_TEXT)], id=block_id, source_pages=pages, **flags)]
    notes: list[Footnote] = []
    runs: list[Inline] = []
    if pages and (counters.last_page is None or pages[0] > counters.last_page):
        runs.append(SourceMark(pages[0]))  # a new scan page starts with this block
    if pages:
        counters.last_page = max(pages[-1], counters.last_page or pages[-1])
    runs.extend(_inline(node.get("content") or [], counters, notes))
    level = 0
    if kind == doc.HEADING:
        raw = attrs.get("level", 1)
        level = 1 if raw in (1, None, "1") else 2
        style = "chapter-title" if level == 1 else "section-title"
    elif kind == doc.TITLE:
        style = "book-title"
        if not _has_content(runs) and str(attrs.get("text") or "").strip():
            runs.append(Run(str(attrs.get("text")).strip()))
    else:
        style = style_override or PARAGRAPH_STYLE_OF.get(attrs.get("style"), "body")
    runs = _trim_edges(_tidy_breaks(runs))
    if not _has_content(runs):
        return []
    plain = doc.object_kinds(node.get("content") or [])
    if kind == doc.TITLE and not plain.strip():
        plain = str(attrs.get("text") or "").strip()  # printed from its attrs
    return [Block(style, runs, notes, id=block_id, source_pages=pages, level=level, plain=plain, **flags)]


def _trim_edges(runs: list[Inline]) -> list[Inline]:
    """Leading / trailing spaces of a block's text go (source marks at the start stay first)."""
    lead = 0
    while lead < len(runs) and isinstance(runs[lead], SourceMark):
        lead += 1
    head, rest = runs[:lead], runs[lead:]
    return head + _trim(rest) if rest else head


def _title_of(content: list, fallback: str) -> tuple[str, str]:
    """Title and author from the leading `title` node (its content wins over its attrs)."""
    for node in content[: doc.preamble_end(content)]:
        attrs = doc.attrs_of(node)
        text = doc.plain_text(node) or str(attrs.get("text") or "").strip()
        return text or fallback, str(attrs.get("author") or "").strip()
    return fallback, ""


def book_model(
    document,
    stylesheet=None,
    *,
    title: str = "",
    author: str = "",
    chapter_ids=None,
    editorial: bool = False,
) -> Book:
    """The book model of a manuscript document with a stylesheet (`editor.StyleSheet`, a dict or None).

    The title and the author are the stylesheet's book details when given, else the document's title
    node's, else `title` / `author` (the book's own fields). `chapter_ids`, when given, keeps only those
    chapters (the chapter preview, the re-layout); footnote numbers still count from the start of the book.
    `editorial=True` keeps the `uncertain` mark on the runs (the Word export's comments); the default
    model is the same as before, byte for byte.
    """
    setup = page_setup(stylesheet)
    content = doc.content_of(document)
    doc_title, doc_author = _title_of(content, title)
    front = Front(
        title=setup.detail("title") or doc_title or title,
        author=setup.detail("author") or doc_author or author,
        title_page=setup.title_page,
        contents=setup.contents,
        copyright_page=setup.copyright_page,
        **{name: setup.detail(name) for name in BOOK_FIELDS if name not in ("title", "author")},
    )
    wanted = set(chapter_ids) if chapter_ids is not None else None
    counters = _Counters(editorial=editorial)
    chapters: list[Chapter] = []
    for chapter in doc.chapters_of(document):
        counters.chapter = 0
        blocks: list[Block] = []
        for node in chapter.nodes(document):
            if isinstance(node, dict):
                blocks.extend(_block(node, counters))
        if wanted is not None and chapter.id not in wanted:
            continue
        chapters.append(
            Chapter(chapter.id, chapter.number, chapter.kind, chapter.title, chapter.heading, blocks)
        )
    return Book(front=front, chapters=chapters, setup=setup)


# ====================================================================== direction

_RTL_RANGES = ((0x0590, 0x08FF), (0xFB1D, 0xFDFF), (0xFE70, 0xFEFF))


def _strong(char: str) -> str | None:
    """`rtl` for Hebrew / Arabic letters and marks, `ltr` for other letters, None for neutrals and digits."""
    code = ord(char)
    if any(lo <= code <= hi for lo, hi in _RTL_RANGES):
        return None if char.isdigit() else "rtl"
    return "ltr" if char.isalpha() else None


def direction_runs(text: str, base: str = "rtl") -> list[tuple[str, bool]]:
    """`text` cut into `(piece, is_rtl)` runs: letters give the direction; neutrals and digits between two
    left-to-right letters stay left-to-right, elsewhere they take the paragraph's `base` direction
    (a simplified Unicode bidi resolution, enough for Word's run-level `w:rtl`)."""
    if not text:
        return []
    strong = [_strong(char) for char in text]
    after: list[str | None] = [None] * len(text)
    following: str | None = None
    for i in range(len(text) - 1, -1, -1):
        after[i] = following
        following = strong[i] if strong[i] is not None else following
    resolved: list[str] = []
    before: str | None = None
    for i, value in enumerate(strong):
        if value is not None:
            resolved.append(value)
            before = value
            continue
        resolved.append("ltr" if before == "ltr" and after[i] == "ltr" else base)
    out: list[tuple[str, bool]] = []
    start = 0
    for i in range(1, len(text) + 1):
        if i == len(text) or resolved[i] != resolved[start]:
            out.append((text[start:i], resolved[start] == "rtl"))
            start = i
    return out
