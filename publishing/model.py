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
- **The cover (D80, COVER_SPEC §2)**: `front_matter["cover"]` parsed into `PageSetup.cover` (`CoverSpec`,
  frozen: the settings with their defaults — `cover_settings` — and the image's file, size and sha,
  looked up once when the setup is read from a stylesheet), and `Front.cover` when it can be drawn (mode
  `info` or `text`, or `image` with its image; `none` and an image mode without an image give None). The
  cover is a page of its own outside the book's pages, so `PageSetup.as_dict()` (the preview, layout and
  export hashes) leaves it out: a stylesheet without a cover key gives the setup, the hashes and the
  files it gave before.

**Empty paragraphs and the text options (D99).** An empty paragraph the owner made (Enter on an empty
line) is a block of its own (`empty`), printed as one blank line in every renderer; of a run of them only
the first `editor.document.MAX_EMPTY_RUN` print, and a chapter of empty lines only prints nothing. Empty
headings and titles are still left out. `breakAfter` sets the next block's `break_before` (across
chapters), so an empty paragraph with both breaks is a blank page (`Block.blank_page`). A paragraph's or a
heading's text options (`editor.document.TEXT_ATTRS`: alignment, direction, indent, first-line indent,
space before / after, size) are plain fields of its block; renderers map them as they map the styles.

`direction_flags` (over a paragraph's whole text) and `direction_runs` (over one text) cut text into
right-to-left and left-to-right pieces for renderers that need explicit direction (Word's `w:rtl`), so
that they order it as the preview does.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

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


# ====================================================================== the cover (D80)

# `front_matter["cover"]["mode"]` → its label in «التنسيق»; `none` (the default) prints no cover anywhere
COVER_MODES: dict[str, str] = {
    "none": "بلا غلاف",
    "info": "من بيانات الكتاب",
    "image": "صورة",
    "text": "نص مخصّص",
}
# how the image meets the page: `fill` covers it (the overflow cropped, centred), `width` / `height` show
# the whole width / height, centred, the background colour where the image does not reach
COVER_FITS: dict[str, str] = {"fill": "ملء الصفحة", "width": "ملاءمة العرض", "height": "ملاءمة الارتفاع"}
# the named colour pairs: key → (label, background, text colour)
COVER_PRESETS: dict[str, tuple[str, str, str]] = {
    "white": ("أبيض", "#ffffff", "#1b1b1b"),
    "cream": ("كريمي", "#f4efe4", "#2a2419"),
    "gray": ("رمادي", "#e9e9ec", "#1f1f24"),
    "navy": ("كحلي", "#1d2433", "#f3efe6"),
    "green": ("أخضر داكن", "#1f3b2d", "#f1ead8"),
    "burgundy": ("عنابي", "#4a1d24", "#f4e9d8"),
}
CUSTOM_PRESET = "custom"  # colours picked by hand
COVER_TEXT_MAX = 600  # characters of the centre and of the bottom text
COVER_SIZE_LIMITS: tuple[float, float] = (8.0, 96.0)  # centre and bottom text sizes (pt)
COVER_BOTTOM_LIMITS: tuple[float, float] = (0.0, 80.0)  # the bottom block's distance from the trim (mm)
COVER_BOTTOM_GAP_MM = 8.0  # the default distance: the page's bottom margin plus this
COVER_SUB_SCALE = 0.55  # the subtitle and the author of an `info` cover, as a share of the centre size
COVER_DEFAULTS: dict = {
    "mode": "none",
    "image": None,
    "fit": "fill",
    "center": "",
    "bottom": "",
    "center_pt": 28.0,
    "bottom_pt": 13.0,
    "background": "#ffffff",
    "color": "#1b1b1b",
    "preset": "white",
    "bottom_mm": None,  # None: the page's bottom margin + COVER_BOTTOM_GAP_MM
}
_RE_HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{6}")


def hex_colour(value) -> str | None:
    """`#rrggbb` in lower case, or None for anything else."""
    if isinstance(value, str) and _RE_HEX_COLOUR.fullmatch(value.strip()):
        return value.strip().lower()
    return None


def cover_text(value) -> str:
    """A cover text as it is kept: its lines (CRLF / CR as LF), each line's spaces collapsed, the empty lines
    at both ends dropped (those between lines stay)."""
    raw = str(value if value is not None else "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in raw.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def preset_of(background: str, color: str) -> str:
    """The preset whose two colours these are, else `custom`."""
    for key, (_label, bg, fg) in COVER_PRESETS.items():
        if (bg, fg) == (background, color):
            return key
    return CUSTOM_PRESET


def _cover_number(value, limits: tuple[float, float]) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return round(number, 2) if limits[0] <= number <= limits[1] else None


def cover_settings(value) -> dict:
    """`front_matter["cover"]` as a whole settings dict: every key of `COVER_DEFAULTS`, a stored value
    kept when it is valid, the default otherwise (the stylesheet's PUT validates what it saves, so this
    only mends hand-edited rows); `preset` follows the two colours."""
    value = value if isinstance(value, dict) else {}
    out = dict(COVER_DEFAULTS)
    if value.get("mode") in COVER_MODES:
        out["mode"] = value["mode"]
    image = value.get("image")
    if isinstance(image, int) and not isinstance(image, bool) and image > 0:
        out["image"] = image
    if value.get("fit") in COVER_FITS:
        out["fit"] = value["fit"]
    for key in ("center", "bottom"):
        if isinstance(value.get(key), str):
            out[key] = cover_text(value[key])[:COVER_TEXT_MAX]
    for key in ("center_pt", "bottom_pt"):
        number = _cover_number(value.get(key), COVER_SIZE_LIMITS)
        if number is not None:
            out[key] = number
    for key in ("background", "color"):
        colour = hex_colour(value.get(key))
        if colour is not None:
            out[key] = colour
    out["preset"] = preset_of(out["background"], out["color"])
    out["bottom_mm"] = _cover_number(value.get("bottom_mm"), COVER_BOTTOM_LIMITS)
    return out


@dataclass(frozen=True)
class CoverImage:
    """The cover's picture as a renderer needs it: the `editor.BookImage` row's id, the stored file's
    absolute path, its pixel size, sha256 and format (`jpeg` | `png`)."""

    id: int
    path: str
    width: int
    height: int
    sha256: str
    format: str


@dataclass(frozen=True)
class CoverSpec:
    """The cover's settings (`cover_settings`, mode other than `none`) and, for an image cover, its image
    (None when none is chosen or its file is gone)."""

    mode: str
    fit: str = "fill"
    center: str = ""
    bottom: str = ""
    center_pt: float = 28.0
    bottom_pt: float = 13.0
    background: str = "#ffffff"
    color: str = "#1b1b1b"
    bottom_mm: float | None = None
    image_id: int | None = None
    image: CoverImage | None = None

    @property
    def ready(self) -> bool:
        """True when the cover can be drawn: `info` and `text` always, `image` with its image."""
        return self.mode in ("info", "text") or (self.mode == "image" and self.image is not None)

    @property
    def photo(self) -> bool:
        """True when the cover shows a picture (rasters of it are JPEG, not PNG)."""
        return self.mode == "image" and self.image is not None

    def fingerprint(self) -> dict:
        """What identifies the cover's look (its render and export hashes): the settings and the image's
        sha (never its path)."""
        return {
            "mode": self.mode,
            "fit": self.fit,
            "center": self.center,
            "bottom": self.bottom,
            "center_pt": self.center_pt,
            "bottom_pt": self.bottom_pt,
            "background": self.background,
            "color": self.color,
            "bottom_mm": self.bottom_mm,
            "image": self.image.sha256 if self.image is not None else None,
        }


def cover_image(image_id: int | None, book_id: int | None = None) -> CoverImage | None:
    """The `editor.BookImage` `image_id` (of `book_id` when given) as a `CoverImage`; None when there is
    no such row or its file is missing (one query)."""
    if not image_id:
        return None
    from editor.models import BookImage

    rows = BookImage.objects.filter(pk=image_id)
    if book_id is not None:
        rows = rows.filter(book_id=book_id)
    row = rows.only("id", "file", "width", "height", "sha256", "format").first()
    if row is None or not row.file:
        return None
    try:
        path = row.file.path
    except (NotImplementedError, ValueError):  # a storage without local paths
        return None
    if not Path(path).is_file():
        return None
    return CoverImage(row.pk, path, int(row.width), int(row.height), row.sha256, row.format)


def cover_spec(value, book_id: int | None = None) -> CoverSpec | None:
    """`front_matter["cover"]` as a `CoverSpec` (None for mode `none`); an image cover's image is looked
    up (`cover_image`)."""
    settings = cover_settings(value)
    if settings["mode"] == "none":
        return None
    image = cover_image(settings["image"], book_id) if settings["mode"] == "image" else None
    return CoverSpec(
        mode=settings["mode"],
        fit=settings["fit"],
        center=settings["center"],
        bottom=settings["bottom"],
        center_pt=settings["center_pt"],
        bottom_pt=settings["bottom_pt"],
        background=settings["background"],
        color=settings["color"],
        bottom_mm=settings["bottom_mm"],
        image_id=settings["image"],
        image=image,
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
    break_before: bool = False  # «ابدأ صفحة جديدة» (D47); also set by the block before's `breakAfter` (D99)
    keep_with_next: bool = False  # «مع التالية» (D47)
    # D99: the page ends after this block (the next block's `break_before` is set from it)
    break_after: bool = False
    # D99: an empty paragraph the owner made (Enter on an empty line): one blank line in every renderer
    empty: bool = False
    # D99, the text options (`editor.document.TEXT_ATTRS`); the defaults are the style's own look
    align: str = ""  # start | center | end | justify ('' : the style's)
    direction: str = ""  # 'ltr' for a left-to-right paragraph ('' : the book's, right to left)
    indent: int = 0  # start-side indent steps (`editor.document.INDENT_STEP_REM` each)
    first_line: bool = True  # False: no first-line indent (a body paragraph's)
    space_before: float = 0.0  # lines of the body's pitch added before / after the style's own space
    space_after: float = 0.0
    size: str = ""  # small | large | xlarge ('' : the style's size), `editor.document.SIZE_SCALES`

    @property
    def blank_page(self) -> bool:
        """An empty paragraph alone on its page (D99, «صفحة فارغة»): a break before and after it."""
        return self.empty and self.break_after

    @property
    def size_scale(self) -> float:
        """The block's text size as a share of its style's (1 without a size option)."""
        return doc.SIZE_SCALES.get(self.size, 1.0)

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
    cover: CoverSpec | None = None  # D80: the cover when it can be drawn (`CoverSpec.ready`)


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
    cover: CoverSpec | None = None  # D80: `front_matter["cover"]` (None: mode `none`); never in `as_dict`

    def as_dict(self) -> dict:
        """The fields that lay the pages out (every preview, layout and export hash): the cover is left
        out, as it never changes a page of the book (D80)."""
        return {name: getattr(self, name) for name in self.__dataclass_fields__ if name != "cover"}

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

    def headings(self) -> list[tuple[Chapter, Block]]:
        """The heading blocks `contents()` lists, with their chapters, in the same order (the Word
        writer bookmarks each occurrence: two headings may share a block id)."""
        return [
            (chapter, block)
            for chapter in self.chapters
            for block in chapter.blocks
            if block.style in ("chapter-title", "section-title") and block.text().strip()
        ]

    def contents(self) -> list[ContentsEntry]:
        """Chapter and section titles in order (only headings the owner wrote, never «القسم n»)."""
        return [
            ContentsEntry(block.level or 1, " ".join(block.text().split()), block.id, chapter.id)
            for chapter, block in self.headings()
        ]


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
    # the cover's image is looked up once here (a stylesheet row knows its book; a dict of fields does not)
    book_id = None if isinstance(stylesheet, dict) else getattr(stylesheet, "book_id", None)
    cover = cover_spec(front["cover"], book_id) if isinstance(front.get("cover"), dict) else None
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
        cover=cover,
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


def _text_options(node: dict) -> dict:
    """The `Block` fields of a paragraph's or a heading's text options (D99, `editor.document.text_attrs`)."""
    found = doc.text_attrs(node)
    return {
        "align": found.get("align", ""),
        "direction": "ltr" if found.get("dir") == "ltr" else "",
        "indent": int(found.get("indent", 0)),
        "first_line": found.get("firstLine", True) is not False,
        "space_before": float(found.get("spaceBefore", 0)),
        "space_after": float(found.get("spaceAfter", 0)),
        "size": found.get("size", ""),
    }


def _block(node: dict, counters: _Counters, style_override: str | None = None) -> list[Block]:
    """The model block(s) of one document block (a blockquote gives one block per paragraph; its own
    `breakBefore` goes to the first, its `keepWithNext` and `breakAfter` to the last). An empty paragraph
    (D99) is a block of its own, `empty`, with no runs but its scan page marks."""
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
            out[-1].break_after = out[-1].break_after or attrs.get("breakAfter") is True
        return out
    pages = tuple(sorted(set(doc.source_pages(node))))
    block_id = doc.node_id(node)
    flags = {
        "break_before": attrs.get("breakBefore") is True,
        "keep_with_next": attrs.get("keepWithNext") is True,
        "break_after": attrs.get("breakAfter") is True,
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
    options = _text_options(node) if kind in (doc.HEADING, doc.PARAGRAPH) else {}
    plain = doc.object_kinds(node.get("content") or [])
    if not _has_content(runs):
        if kind != doc.PARAGRAPH:
            return []  # an empty heading or title prints nothing
        # D99: an empty paragraph the owner made is a blank line (its scan page marks stay)
        marks = [run for run in runs if isinstance(run, SourceMark)]
        return [
            Block(
                style, marks, [], id=block_id, source_pages=pages, plain=plain, empty=True, **flags, **options
            )
        ]
    if kind == doc.TITLE and not plain.strip():
        plain = str(attrs.get("text") or "").strip()  # printed from its attrs
    return [
        Block(
            style, runs, notes, id=block_id, source_pages=pages, level=level, plain=plain, **flags, **options
        )
    ]


@dataclass
class _Flow:
    """What the blocks before tell the next one (D99): a page asked to end after them, the empty lines in a
    row."""

    pending: bool = False
    empties: int = 0


def _flow(blocks: list[Block], flow: _Flow) -> list[Block]:
    """The blocks of a chapter as they print (D99): a block after one with `break_after` starts a page, and
    of a run of empty paragraphs only the first `MAX_EMPTY_RUN` print (a new page starts a new run)."""
    out: list[Block] = []
    for block in blocks:
        if flow.pending:
            block.break_before = True
            flow.pending = False
        if block.break_before:
            flow.empties = 0
        if block.empty:
            flow.empties += 1
            if flow.empties > doc.MAX_EMPTY_RUN:
                flow.pending = flow.pending or block.break_after
                continue
        else:
            flow.empties = 0
        out.append(block)
        if block.break_after:
            flow.pending = True
    return out


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
        cover=setup.cover if setup.cover is not None and setup.cover.ready else None,
    )
    wanted = set(chapter_ids) if chapter_ids is not None else None
    counters = _Counters(editorial=editorial)
    flow = _Flow()
    chapters: list[Chapter] = []
    for chapter in doc.chapters_of(document):
        counters.chapter = 0
        blocks: list[Block] = []
        for node in chapter.nodes(document):
            if isinstance(node, dict):
                blocks.extend(_block(node, counters))
        blocks = _flow(blocks, flow)
        if blocks and all(block.empty and not block.blank_page for block in blocks):
            blocks = []  # a chapter of empty lines only prints nothing (no blank page for it)
        if wanted is not None and chapter.id not in wanted:
            continue
        chapters.append(
            Chapter(chapter.id, chapter.number, chapter.kind, chapter.title, chapter.heading, blocks)
        )
    return Book(front=front, chapters=chapters, setup=setup)


# ====================================================================== direction
#
# Word's `w:rtl` is more than a font switch: ECMA-376 Part 1 §17.3.2.30 makes it a right-to-left override
# for the weak types other than EN, ET, CS and AN (so ES, NSM and BN) and for every neutral (B, S, WS, ON).
# A space or a hyphen inside a `w:rtl` run is a strong R in Word whatever its neighbours, so «Windows 10»
# or «COVID-19» written with the space or the hyphen right to left come out reordered, while the preview
# applies the Unicode Bidi Algorithm (UAX #9) to the paragraph's plain text. The direction is therefore
# decided once for a whole paragraph: resolve UAX #9 on its joined text, mark right to left what resolves
# right to left (and the numbers of a right-to-left context, which `w:rtl` never overrides: the
# complex-script face sets them, as the preview's Arabic face does), then read the result the way Word
# does and take the override off wherever it would change the order.

# one code per character: L, R, A (AL), E (EN), S (ES), T (ET), N (AN), C (CS), M (NSM), B (BN),
# W (white space and the separators B / S, which the preview collapses), O (ON), X (an explicit
# embedding, override or isolate). Callers add R for a footnote call (Word writes «(» and «)» as
# right-to-left runs) and «|» where a line break starts a new stretch of text.
_BIDI_CODES: dict[str, str] = {
    "L": "L",
    "R": "R",
    "AL": "A",
    "EN": "E",
    "ES": "S",
    "ET": "T",
    "AN": "N",
    "CS": "C",
    "NSM": "M",
    "BN": "B",
    "B": "W",
    "S": "W",
    "WS": "W",
    "ON": "O",
}
_RTL_BLOCKS = ((0x0590, 0x08FF), (0xFB1D, 0xFDFF), (0xFE70, 0xFEFF), (0x10800, 0x10FFF), (0x1E800, 0x1EFFF))


class _BidiTable(dict):
    """`str.translate` table: code point → bidi class code, filled on first use."""

    def __missing__(self, code: int) -> str:
        value = unicodedata.bidirectional(chr(code))
        if value:
            found = _BIDI_CODES.get(value, "X")
        else:  # unassigned: right to left inside the right-to-left blocks, else left to right
            found = "R" if any(lo <= code <= hi for lo, hi in _RTL_BLOCKS) else "L"
        self[code] = found
        return found


_BIDI_TABLE = _BidiTable()
_W1 = re.compile(r"([^M])(M+)")
_W1_START = re.compile(r"^M+")
_W2 = re.compile(r"A[^LRAE]*E[^LRA]*")  # from an Arabic letter to the next strong type, with a digit
_W4_EN = re.compile(r"(?<=E)[SC](?=E)")
_W4_AN = re.compile(r"(?<=N)C(?=N)")
_W5 = re.compile(r"T+(?=E)|(?<=E)T+")
_W6 = str.maketrans("STC", "OOO")
_W7 = re.compile(r"L[^LRE]*E[^LR]*")  # from a left-to-right letter to the next strong type, with a digit
_W7_START = re.compile(r"^[^LR]+")
_N_RTL = re.compile(r"(?<=L)[WO]+(?=L)")
_N_LTR = re.compile(r"(?<=[REN])[WO]+(?=[REN])")
_NEUTRAL_R = str.maketrans("WO", "RR")
_NEUTRAL_L = str.maketrans("WO", "LL")
_LEVELS_RTL = str.maketrans("RLEN", "1222")
_LEVELS_LTR = str.maketrans("RLEN", "1022")
_FLAG_OF_TYPE = str.maketrans("RLEN", "10nn")  # n: a number, `w:rtl` unless Word would override it
_TO_WORD = str.maketrans("SMBWO", "RRRRR")
_RUNS = re.compile(r"1+|0+")
_ONES = re.compile(r"1+")
_NUMBERS = re.compile(r"n+")
_OVERRIDDEN = frozenset("SMBWO")  # the codes `w:rtl` turns into R
_NEEDS_RESOLVING = re.compile(r"[LSTNBX]|[EC]M")  # without these a right-to-left text is all `w:rtl`
MAX_FIXES = 64


def bidi_classes(text: str) -> str:
    """One bidi class code per character of `text` (see `_BIDI_CODES`)."""
    return text.translate(_BIDI_TABLE)


def _resolve(classes: str, rtl: bool) -> str:
    """The resolved type (L, R, E or N) of each class code of one paragraph: UAX #9 rules W1–W7 and
    N1–N2 at paragraph level 1 (`rtl`) or 0, without explicit embeddings; BN is set aside (X9) and takes
    the type before it."""
    sos = "R" if rtl else "L"
    if "B" in classes:
        kept = classes.replace("B", "")
        types = iter(_resolve(kept, rtl))
        out: list[str] = []
        previous = sos
        for code in classes:
            if code != "B":
                previous = next(types)
            out.append(previous)
        return "".join(out)
    s = classes
    if "M" in s:  # W1: a mark takes the type of the character before it
        while "AM" in s:  # the common case, the marks of an Arabic letter
            s = s.replace("AM", "AA")
        if "M" in s:
            s = _W1.sub(lambda m: m.group(1) * (1 + len(m.group(2))), s)
            s = _W1_START.sub(lambda m: sos * len(m.group()), s)
    if "E" in s and "A" in s:  # W2: European digits after Arabic letters are Arabic numbers
        s = _W2.sub(lambda m: m.group().replace("E", "N"), s)
    s = s.replace("A", "R")  # W3
    if "E" in s:  # W4: one separator between two numbers of a kind joins them
        s = _W4_EN.sub("E", s)
    if "N" in s:
        s = _W4_AN.sub("N", s)
    if "T" in s and "E" in s:  # W5: terminators next to European digits
        s = _W5.sub(lambda m: "E" * len(m.group()), s)
    s = s.translate(_W6)  # W6
    if "E" in s:  # W7: European digits after a left-to-right letter are left to right
        if not rtl:
            s = _W7_START.sub(lambda m: m.group().replace("E", "L"), s)
        if "L" in s:
            s = _W7.sub(lambda m: m.group().replace("E", "L"), s)
    if rtl:  # N1, N2: neutrals between two left-to-right letters, else the paragraph's direction
        if "L" in s:
            s = _N_RTL.sub(lambda m: "L" * len(m.group()), s)
        return s.translate(_NEUTRAL_R)
    s = _N_LTR.sub(lambda m: "R" * len(m.group()), s)
    return s.translate(_NEUTRAL_L)


def _levels(types: str, rtl: bool) -> str:
    return types.translate(_LEVELS_RTL if rtl else _LEVELS_LTR)


def _first_flags(classes: str, types: str) -> str:
    """`w:rtl` where the text resolves right to left, and on the numbers of a right-to-left context that
    Word never overrides (digits, their terminators and separators); a hyphen joined into such a number,
    and anything left to right, stays without it."""
    flags = types.translate(_FLAG_OF_TYPE)
    if "n" not in flags:
        return flags
    out = list(flags)
    for match in _NUMBERS.finditer(flags):
        for i in range(match.start(), match.end()):
            out[i] = "0" if classes[i] in _OVERRIDDEN else "1"
    return "".join(out)


def _as_word(classes: str, flags: str) -> str:
    """The class codes as Word reads them: what `w:rtl` overrides becomes R."""
    parts: list[str] = []
    last = 0
    for match in _ONES.finditer(flags):
        parts.append(classes[last : match.start()])
        parts.append(classes[match.start() : match.end()].translate(_TO_WORD))
        last = match.end()
    parts.append(classes[last:])
    return "".join(parts)


def _stretch_flags(classes: str, rtl: bool) -> str:
    if not classes:
        return ""
    if rtl and not _NEEDS_RESOLVING.search(classes):
        return "1" * len(classes)
    if "X" in classes:  # explicit embeddings: no override at all, so Word and the preview read the same
        return "".join("1" if code in "RAN" else "0" for code in classes)
    types = _resolve(classes, rtl)
    want = _levels(types, rtl)
    flags = _first_flags(classes, types)
    for _attempt in range(MAX_FIXES):
        got = _levels(_resolve(_as_word(classes, flags), rtl), rtl)
        if got == want:
            return flags
        # an override that changes the order shields a number from the letter before it (W2): before
        # each stretch of differences, take the override off the nearest overridden characters
        chars = list(flags)
        changed = False
        previous_differs = False
        for index, (a, b) in enumerate(zip(got, want, strict=True)):
            differs = a != b
            if differs and not previous_differs:
                at = index - 1
                while at >= 0 and not (chars[at] == "1" and classes[at] in _OVERRIDDEN):
                    at -= 1
                while at >= 0 and chars[at] == "1" and classes[at] in _OVERRIDDEN:
                    chars[at] = "0"
                    changed = True
                    at -= 1
            previous_differs = differs
        if not changed:
            break
        flags = "".join(chars)
    return "".join("0" if code in _OVERRIDDEN else flag for code, flag in zip(classes, flags, strict=True))


def direction_flags(classes: str, base: str = "rtl") -> str:
    """For each code of `classes` (`bidi_classes`, plus R for a footnote call and «|» for a line break),
    `1` when Word should write it in a `w:rtl` run and `0` when not, so that Word orders the paragraph as
    the preview does (UAX #9 on its text) with the paragraph direction `base`."""
    rtl = base != "ltr"
    if "|" in classes:
        return "0".join(_stretch_flags(part, rtl) for part in classes.split("|"))
    return _stretch_flags(classes, rtl)


def direction_pieces(text: str, flags: str) -> list[tuple[str, bool]]:
    """`text` cut where its flags change: `(piece, is_rtl)`."""
    out: list[tuple[str, bool]] = []
    start = 0
    for match in _RUNS.finditer(flags):
        end = match.end()
        out.append((text[start:end], match.group()[0] == "1"))
        start = end
    return out


def direction_runs(text: str, base: str = "rtl") -> list[tuple[str, bool]]:
    """`text` cut into `(piece, is_rtl)` runs, decided over the whole text (see `direction_flags`)."""
    if not text:
        return []
    return direction_pieces(text, direction_flags(bidi_classes(text), base))
