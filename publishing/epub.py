"""The EPUB export (PHASE6_SPEC §10, 6c): `epub` «EPUB», an EPUB 3 book read right to left, built from
the book model (`publishing.model.book_model`) — the same text as the book page, without fixed pages.

**The package** (ebooklib 0.20 writes the OPF, the container and the NCX; the documents are Nassakh's own
XHTML, written whole): EPUB 3, `dc:language ar`, `page-progression-direction="rtl"`, `lang="ar"
dir="rtl"` on every document; the identifier is `urn:isbn:` when the book details give an ISBN, else a
stable `urn:uuid:` (uuid5 of the book); the metadata are the book details (title, subtitle, author,
editor and translator with their MARC roles, publisher, year, rights) and `dcterms:modified` is the
export's time. The zip is written again with fixed times (the same export gives the same bytes) and
`mimetype` first, stored.

**The documents:** the cover when the book has one (D80: `cover.xhtml`, first in the spine, `epub:type
cover`, landmark «الغلاف», showing the preview's cover rendered alone and rasterised 1600 px tall —
`images/cover.jpeg` for a picture, `.png` otherwise —, declared `properties="cover-image"` with the EPUB 2
`<meta name="cover">` and guide entry, so readers show it in the library), the title page and the
copyright page when the stylesheet asks for them (the preview's lines), `nav.xhtml` (the contents — the
book page's contents entries, chapters with their sections — and the landmarks; in the reading order when
the book has a contents page), then one XHTML file per chapter (D40's chapters; a chapter left with
nothing to print gets none, as in the preview). A book without a cover gets the file it got before.

**Blocks:** `h1` (chapter title), `h2` (section title), `p.body`, `blockquote.quote` (consecutive quote
paragraphs together), `p.verse` with `<br/>`, `p.center`, `p.separator`, `b`, `i`. Scan page marks are
not printed.

**Footnotes** are numbered per chapter (a reflowable book has no pages): the call is `a.noteref
epub:type="noteref"` «(n)», raised, and the note an `aside epub:type="footnote"` at the chapter's end
with a back link «(n)» to its call.

**Faces (D45, D57):** only Amiri may be embedded, so `fonts/Amiri-Regular.ttf` and `Amiri-Bold.ttf` go in
whole, with Amiri's `OFL.txt`. The CSS declares the preview's roles (`nk-body`, `nk-heading`, `nk-latin`,
with the same unicode ranges): each is the book's face where the reading device has it installed
(`local()`), else Amiri — so another face falls back to Amiri, as D45 says, and a face missing on this
Mac is Amiri, as in the preview. `ibooks:specified-fonts` lets Apple Books use them. An organisation's
face (D98) whose licence allows embedding goes in whole as `fonts/org-<pk>-<style>.<ttf|otf>`, with its
licence notice beside it (`fonts/org-<pk>-licence.txt`), and its roles use that file.

**The check** reads the file back with ebooklib and lxml: `mimetype` first and stored, every XHTML
document well formed and right to left, every noteref's target and every back link present, the spine
right to left and never empty (a book with nothing to read has its nav as the one document), the nav and
the fonts in the manifest, every picture a document shows in it. A file that fails is an `InvalidExport`.

**Text** goes in without the characters XML 1.0 forbids (`xml_safe`: C0 controls such as an OCR'd
vertical tab or form feed, lone surrogates, U+FFFE / U+FFFF), in the documents, the metadata and the NCX.
"""

from __future__ import annotations

import io
import re
import uuid
import zipfile
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from . import fonts as F
from .exporters import ExportCancelled, ExportJob, ExportResult, InvalidExport, OptionSpec, Progress
from .html import CONTENTS_TITLE, CREDIT_LABELS, EDITION_LABEL, ISBN_LABEL
from .model import Block, Book, Chapter, Footnote, LineBreak, NoteRef, PageSetup, Run, book_model
from .readiness import INFO, missing_font_rows, row

EPUB_VERSION = "nk-epub-1"
MEDIA_TYPE_EPUB = "application/epub+zip"
XHTML = "application/xhtml+xml"
FONT_TYPE = "font/ttf"
OFL_FILE = F.VENDORED_DIR / "amiri" / "OFL.txt"
AMIRI_FILES: tuple[tuple[str, str], ...] = (("normal", "Amiri-Regular.ttf"), ("bold", "Amiri-Bold.ttf"))
NS_EPUB = "http://www.idpf.org/2007/ops"
NS_XHTML = "http://www.w3.org/1999/xhtml"
IBOOKS_PREFIX = "http://vocabulary.itunes.apple.com/rdf/ibooks/vocabulary-extensions-1.0/"
MARC_ROLES: dict[str, str] = {"author": "aut", "editor": "edt", "translator": "trl"}
NOTE_CALL_SCALE = 0.62  # the call's size, as the preview's (`css.FOOTNOTE_CALL_SCALE`)

LANDMARKS_TITLE = "معالم الكتاب"
COVER_TITLE = "الغلاف"
COVER_ID = "cover-image"
TITLE_PAGE = "صفحة العنوان"
COPYRIGHT_PAGE = "صفحة الحقوق"
BODY_START = "بداية الكتاب"

FONT_EMBEDDED = "خط أميري مضمَّن في الملف."
FONT_EMBEDDED_ORG = "خط «{name}» من خطوط المؤسسة مضمَّن في الملف."  # D98
FONT_TYPES: dict[str, str] = {".ttf": "font/ttf", ".otf": "font/otf"}
_STYLE_NAMES: dict[tuple[str, str], str] = {
    ("400", "normal"): "regular",
    ("700", "normal"): "bold",
    ("400", "italic"): "italic",
    ("700", "italic"): "bolditalic",
}
FONT_FALLBACK = (
    "خط «{name}» لا يُضمَّن في EPUB (ترخيصه لا يسمح)؛ يظهر به النص على جهاز مثبّت عليه، وإلا فبخط أميري."
)
EPUB_NOTES = "لا صفحات ثابتة في EPUB: تُرقَّم الحواشي في كل فصل وتأتي في آخره."
SOURCE_PAGES = "أرقام الصفحات الأصلية في الهامش لا تُطبع في EPUB."

_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]")
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
# what XML 1.0 forbids in a document (C0 controls but tab, line feed and carriage return; lone surrogates;
# U+FFFE, U+FFFF): an OCR'd vertical tab or form feed would make a chapter unreadable
_NOT_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def xml_safe(value) -> str:
    """`value` as text without the characters XML 1.0 forbids."""
    return _NOT_XML.sub("", str(value))


def _text(value: str) -> str:
    return escape(xml_safe(value), quote=False)


def _attr(value) -> str:
    return escape(xml_safe(value), quote=True)


# ====================================================================== identifiers and metadata


def book_identifier(book_id: int, isbn: str = "") -> str:
    """`urn:isbn:<digits>` for a 10- or 13-digit ISBN, else a stable `urn:uuid:` of the book."""
    digits = re.sub(r"[^0-9Xx]", "", str(isbn or "").translate(_ARABIC_DIGITS)).upper()
    if len(digits) in (10, 13):
        return f"urn:isbn:{digits}"
    return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'nassakh:book:{book_id}')}"


def publication_year(value: str) -> str:
    """The year of the book details as a `dc:date` (four digits, Arabic-Indic ones read too), else ''."""
    found = re.search(r"(?<!\d)(\d{4})(?!\d)", str(value or "").translate(_ARABIC_DIGITS))
    return found.group(1) if found else ""


def _utc(value: datetime | None) -> datetime:
    value = value or datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _safe_front(front):
    """A copy of the front matter with every text `xml_safe`."""
    values = {
        item.name: xml_safe(getattr(front, item.name))
        for item in fields(front)
        if isinstance(getattr(front, item.name), str)
    }
    return replace(front, **values)


# ====================================================================== the CSS


def _em(value: float) -> str:
    """A multiple of the text size in CSS (`1.6em`)."""
    return f"{round(float(value), 3):g}em"


def _font_rules(family: str, face: F.Face, ranges: str | None) -> list[str]:
    """`@font-face` rules of a role: the book's face through `local()` where the reader has it, then the
    embedded Amiri (the face's own file when it is Amiri). A face without a bold file gets no bold rule:
    the reader emboldens its regular, as WeasyPrint does in the preview. An organisation's face that is
    embedded (D98) uses its own files only."""
    rules = []
    if F.org_embeds(face, "epub"):
        for weight, style, path in face.files.styles():
            extra = f" unicode-range: {F.face_ranges(path, ranges)};" if ranges else ""
            name = org_file_name(face, weight, style, path)
            rules.append(
                f'@font-face {{ font-family: "{family}"; src: url("../fonts/{name}");'
                f" font-weight: {weight}; font-style: {style};{extra} }}"
            )
        return rules
    for weight, amiri_name in AMIRI_FILES:
        path = face.files.regular if weight == "normal" else face.files.bold
        if path is None and face.key != "amiri":
            continue
        sources = []
        if face.key != "amiri" and path is not None:
            sources += [f'local("{name}")' for name in F.local_names(path)] or [f'local("{face.family}")']
        sources.append(f'url("../fonts/{amiri_name}")')
        extra = ""
        if ranges:
            extra = f" unicode-range: {F.face_ranges(path or face.files.regular, ranges)};"
        rules.append(
            f'@font-face {{ font-family: "{family}"; src: {", ".join(sources)}; font-weight: {weight};'
            f" font-style: normal;{extra} }}"
        )
    return rules


def org_file_name(face: F.Face, weight: str, style: str, path: Path) -> str:
    """The package name of an organisation face's file: `org-<pk>-<regular|bold|italic|bolditalic>.<ext>`."""
    return f"{face.key}-{_STYLE_NAMES.get((weight, style), 'regular')}{path.suffix.lower()}"


def org_font_items(fonts: F.ResolvedFonts) -> list[tuple[str, Path, str]]:
    """The organisation faces' files the EPUB carries (D98): `(package name, path, media type)`, each face
    once, and its licence notice as `(name, None, text)` when it has one."""
    out: list[tuple[str, Path | None, str]] = []
    seen: set[str] = set()
    for face in (fonts.body, fonts.heading, fonts.latin):
        if face.key in seen or not F.org_embeds(face, "epub"):
            continue
        seen.add(face.key)
        for weight, style, path in face.files.styles():
            media_type = FONT_TYPES.get(path.suffix.lower(), "font/ttf")
            out.append((org_file_name(face, weight, style, path), path, media_type))
        if face.licence.strip():
            out.append((f"{face.key}-licence.txt", None, face.licence.strip()))
    return out


def epub_css(setup: PageSetup, fonts: F.ResolvedFonts) -> str:
    """The book's stylesheet for reading systems: the preview's roles and proportions in relative units
    (the reader chooses the size)."""
    body = '"nk-body", "nk-latin", serif'
    heading = '"nk-heading", "nk-latin", serif'
    note = max(0.5, min(1.0, setup.footnote_size_pt / max(setup.body_size_pt, 1)))
    rules = [
        *_font_rules("nk-body", fonts.body, F.role_ranges(fonts.body, fonts.latin)),
        *_font_rules("nk-heading", fonts.heading, F.role_ranges(fonts.heading, fonts.latin)),
        *_font_rules("nk-latin", fonts.latin, None),
        f"body {{ font-family: {body}; line-height: {setup.line_height:g}; text-align: justify;"
        f" margin: 0 3%; widows: {int(setup.widows)}; orphans: {int(setup.orphans)}; }}",
        "p { margin: 0; text-indent: 0; }",
        f"h1, h2 {{ font-family: {heading}; font-weight: bold; text-align: center;"
        " page-break-after: avoid; break-after: avoid; }",
        f"h1.chapter-title {{ font-size: {_em(setup.h1_scale)}; line-height: 1.35; margin: 2em 0 1.2em; }}",
        f"h2.section-title {{ font-size: {_em(setup.h2_scale)}; line-height: 1.4; margin: 1.2em 0 0.6em; }}",
        f"p.body {{ text-indent: {_em(setup.indent_em)}; }}",
        "blockquote.quote { margin: 0.4em 2em; }",
        "p.verse, p.center, p.separator { text-align: center; text-indent: 0; margin: 0.4em 0; }",
        "p.separator { margin: 1em 0; }",
        f"p.book-title {{ font-family: {heading}; font-weight: bold; text-align: center;"
        f" font-size: {_em(setup.h1_scale)}; margin: 1em 0; }}",
        ".break { page-break-before: always; break-before: page; }",
        ".keep { page-break-after: avoid; break-after: avoid; }",
        "b { font-weight: bold; } i { font-style: italic; }",
        f"a.noteref {{ font-size: {_em(NOTE_CALL_SCALE)}; vertical-align: super; line-height: 0;"
        " text-decoration: none; color: inherit; }",
        f"aside.footnote {{ font-size: {_em(note)}; line-height: 1.5; text-align: justify;"
        " margin: 0.3em 0 0; }",
        "aside.footnote.first { border-top: 1px solid; margin-top: 2em; padding-top: 0.6em; }",
        "a.note-back { text-decoration: none; color: inherit; }",
        "section.title-page { text-align: center; padding-top: 20%; }",
        f"section.title-page h1 {{ font-size: {_em(setup.h1_scale * 1.4)}; line-height: 1.4;"
        " margin: 0 0 1em; }",
        "section.title-page p { text-align: center; margin: 0.4em 0; }",
        "p.book-author { font-size: 1.3em; } p.book-subtitle { font-size: 1.15em; }",
        "p.book-imprint { margin-top: 3em; }",
        f"section.copyright-page {{ text-align: center; padding-top: 30%; font-size: {_em(note)}; }}",
        "section.copyright-page p { text-align: center; margin: 0 0 0.3em; }",
        f"nav h1 {{ font-family: {heading}; font-size: {_em(setup.h1_scale)}; text-align: center; }}",
        "nav ol { list-style: none; margin: 0; padding: 0; }",
        "nav li { margin: 0.3em 0; } nav li li { padding-right: 1.5em; font-size: 0.95em; }",
        "nav a { text-decoration: none; color: inherit; }",
    ]
    return "\n".join(rules) + "\n"


# ====================================================================== the documents


def _document(title: str, body: str, *, body_type: str = "") -> bytes:
    """A whole XHTML content document, right to left, with the book's stylesheet."""
    body_attr = f' epub:type="{body_type}"' if body_type else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        f'<html xmlns="{NS_XHTML}" xmlns:epub="{NS_EPUB}" lang="ar" xml:lang="ar" dir="rtl">\n'
        f'<head>\n<meta charset="utf-8"/>\n<title>{_text(title)}</title>\n'
        '<link rel="stylesheet" type="text/css" href="styles/book.css"/>\n</head>\n'
        f'<body dir="rtl"{body_attr}>\n{body}\n</body>\n</html>\n'
    ).encode()


class _Ids:
    """Unique, safe element ids within one document."""

    def __init__(self) -> None:
        self.seen: set[str] = set()

    def make(self, prefix: str, value: str, fallback: str) -> str:
        base = _SAFE_ID.sub("", value or "") or fallback
        candidate, k = f"{prefix}{base}", 2
        while candidate in self.seen:
            candidate, k = f"{prefix}{base}-{k}", k + 1
        self.seen.add(candidate)
        return candidate


def _run_html(run: Run) -> str:
    html = _text(run.text)
    if "italic" in run.marks:
        html = f"<i>{html}</i>"
    if "bold" in run.marks:
        html = f"<b>{html}</b>"
    return html


@dataclass
class ChapterDoc:
    """One chapter's XHTML: its file, title, the heading element ids (block id → id) and the notes."""

    file_name: str
    title: str
    content: bytes
    headings: dict[str, str] = field(default_factory=dict)
    notes: int = 0


class _ChapterWriter:
    """Writes one chapter: its blocks, and its notes at the end (numbered in the chapter)."""

    def __init__(self, chapter: Chapter):
        self.chapter = chapter
        self.ids = _Ids()
        self.notes: list[tuple[int, Footnote]] = []
        self.headings: dict[str, str] = {}

    def inline(self, block: Block) -> str:
        notes = {note.id: note for note in block.footnotes}
        parts: list[str] = []
        for run in block.runs:
            if isinstance(run, Run):
                parts.append(_run_html(run))
            elif isinstance(run, LineBreak):
                parts.append("<br/>")
            elif isinstance(run, NoteRef):
                note = notes.get(run.note)
                if note is None:
                    continue
                number = len(self.notes) + 1
                self.notes.append((number, note))
                parts.append(
                    f'<a class="noteref" epub:type="noteref" role="doc-noteref" id="ref-{number}"'
                    f' href="#fn-{number}">({number})</a>'
                )
        return "".join(parts)

    def block(self, block: Block) -> str:
        classes = [block.style]
        if block.break_before:
            classes.append("break")
        if block.keep_with_next:
            classes.append("keep")
        names = _attr(" ".join(classes))
        body = self.inline(block)
        if block.style in ("chapter-title", "section-title"):
            tag = "h1" if block.style == "chapter-title" else "h2"
            element_id = self.ids.make("b-", block.id, f"h{len(self.headings) + 1}")
            self.headings.setdefault(block.id, element_id)
            return f'<{tag} class="{names}" id="{element_id}">{body}</{tag}>'
        return f'<p class="{names}">{body}</p>'

    def footnote(self, number: int, note: Footnote, first: bool) -> str:
        body = "".join(_run_html(run) if isinstance(run, Run) else "<br/>" for run in note.runs)
        names = "footnote first" if first else "footnote"
        return (
            f'<aside class="{names}" epub:type="footnote" role="doc-footnote" id="fn-{number}">'
            f'<p><a class="note-back" role="doc-backlink" href="#ref-{number}">({number})</a> {body}</p>'
            "</aside>"
        )

    def write(self, file_name: str) -> ChapterDoc:
        parts: list[str] = []
        quote: list[str] = []
        for block in self.chapter.blocks:
            html = self.block(block)
            if block.style == "quote":
                quote.append(html)
                continue
            if quote:
                parts.append(f'<blockquote class="quote">{"".join(quote)}</blockquote>')
                quote = []
            parts.append(html)
        if quote:
            parts.append(f'<blockquote class="quote">{"".join(quote)}</blockquote>')
        kind = "chapter" if self.chapter.kind == "chapter" else ""
        section_type = f' epub:type="{kind}" role="doc-chapter"' if kind else ""
        body = [
            f'<section class="chapter is-{_attr(self.chapter.kind)}"{section_type}'
            f' id="ch-{_SAFE_ID.sub("", self.chapter.id) or self.chapter.number}">',
            *parts,
            "</section>",
        ]
        body += [self.footnote(number, note, index == 0) for index, (number, note) in enumerate(self.notes)]
        title = self.chapter.heading or self.chapter.title
        content = _document(title, "\n".join(body), body_type="bodymatter")
        return ChapterDoc(file_name, title, content, self.headings, len(self.notes))


def _credit(value: str, label: str) -> str:
    return value if value.startswith(label) else f"{label}: {value}"


# the cover's page (D80): the picture alone, as large as the screen lets it be
COVER_CSS = (
    "div.cover { margin: 0; padding: 0; text-align: center; }",
    "div.cover img { max-width: 100%; max-height: 96vh; height: auto; }",
)


def cover_page(book: Book, image_name: str) -> bytes:
    """`cover.xhtml`: the cover's picture (`image_name`, the file in the package)."""
    body = f'<div class="cover"><img src="{_attr(image_name)}" alt="{_attr(COVER_TITLE)}"/></div>'
    return _document(COVER_TITLE, body, body_type="cover")


def title_page(book: Book) -> bytes:
    """The title page: the preview's lines (title, subtitle, author, editor, translator, imprint)."""
    front = book.front
    parts = [f'<section class="title-page" epub:type="titlepage"><h1>{_text(front.title)}</h1>']
    if front.subtitle:
        parts.append(f'<p class="book-subtitle">{_text(front.subtitle)}</p>')
    if front.author:
        parts.append(f'<p class="book-author">{_text(front.author)}</p>')
    for name in ("editor", "translator"):
        value = getattr(front, name)
        if value:
            parts.append(f'<p class="book-credit">{_text(_credit(value, CREDIT_LABELS[name]))}</p>')
    place = "، ".join(value for value in (front.publisher, front.city, front.year) if value)
    if place:
        parts.append(f'<p class="book-imprint">{_text(place)}</p>')
    parts.append("</section>")
    return _document(front.title, "\n".join(parts), body_type="frontmatter")


def copyright_page(book: Book) -> bytes:
    """The copyright page: the preview's lines (the book details, the ISBN, the rights)."""
    front = book.front
    lines = [front.title]
    if front.subtitle:
        lines.append(front.subtitle)
    if front.author:
        lines.append(front.author)
    for name in ("editor", "translator"):
        value = getattr(front, name)
        if value:
            lines.append(_credit(value, CREDIT_LABELS[name]))
    if front.edition:
        edition = front.edition
        lines.append(edition if edition.startswith(EDITION_LABEL) else f"{EDITION_LABEL} {edition}")
    place = "، ".join(value for value in (front.publisher, front.city, front.year) if value)
    if place:
        lines.append(place)
    if front.isbn:
        lines.append(f"{ISBN_LABEL}: {front.isbn}")
    if front.rights:
        lines.append(front.rights)
    body = "".join(f"<p>{_text(line)}</p>" for line in lines)
    content = f'<section class="copyright-page" epub:type="copyright-page">{body}</section>'
    return _document(front.title, content, body_type="frontmatter")


@dataclass(frozen=True)
class NavEntry:
    """A line of the contents: its level, text and target (`file#id`)."""

    level: int
    text: str
    href: str


def contents_entries(book: Book, docs: dict[str, ChapterDoc]) -> list[NavEntry]:
    """The book page's contents entries (chapters and sections), each pointing into its chapter's file;
    when the book has none, one entry per chapter file."""
    out: list[NavEntry] = []
    for entry in book.contents():
        doc = docs.get(entry.chapter)
        element = doc.headings.get(entry.target) if doc is not None else None
        if doc is None or element is None:
            continue
        out.append(NavEntry(min(entry.level, 2), xml_safe(entry.text), f"{doc.file_name}#{element}"))
    if not out:
        out = [NavEntry(1, xml_safe(doc.title or book.front.title), doc.file_name) for doc in docs.values()]
    return out


def _nav_list(entries: list[NavEntry]) -> str:
    """Nested `ol`s: a section under the chapter before it (at the top when none comes first)."""
    items: list[tuple[NavEntry, list[NavEntry]]] = []
    for entry in entries:
        if entry.level == 2 and items:
            items[-1][1].append(entry)
        else:
            items.append((entry, []))
    parts = ["<ol>"]
    for entry, children in items:
        link = f'<a href="{_attr(entry.href)}">{_text(entry.text)}</a>'
        if children:
            inner = "".join(f'<li><a href="{_attr(c.href)}">{_text(c.text)}</a></li>' for c in children)
            parts.append(f"<li>{link}<ol>{inner}</ol></li>")
        else:
            parts.append(f"<li>{link}</li>")
    parts.append("</ol>")
    return "".join(parts)


def nav_document(entries: list[NavEntry], landmarks: list[tuple[str, str, str]]) -> bytes:
    """`nav.xhtml`: the contents (`toc`) and the landmarks (`(type, href, label)`, hidden)."""
    marks = "".join(
        f'<li><a epub:type="{kind}" href="{_attr(href)}">{_text(label)}</a></li>'
        for kind, href, label in landmarks
    )
    body = (
        f'<nav epub:type="toc" id="toc" role="doc-toc"><h1>{CONTENTS_TITLE}</h1>{_nav_list(entries)}</nav>\n'
        f'<nav epub:type="landmarks" id="landmarks" hidden="hidden"><h2>{LANDMARKS_TITLE}</h2>'
        f"<ol>{marks}</ol></nav>"
    )
    return _document(CONTENTS_TITLE, body)


# ====================================================================== the package


@dataclass
class EpubBuild:
    """A built EPUB: its bytes and what is in it."""

    data: bytes
    chapters: int
    footnotes: int
    documents: list[str]
    stats: dict = field(default_factory=dict)


def _repack(data: bytes, when: datetime) -> bytes:
    """The zip again with fixed times: `mimetype` first and stored (no extra field), the rest deflated."""
    stamp = when.timetuple()[:6]
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, "w") as target:
        names = sorted(source.namelist(), key=lambda name: name != "mimetype")  # (a stable sort)
        for name in names:
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED
            target.writestr(info, source.read(name))
    return out.getvalue()


def build_epub(
    book: Book,
    fonts: F.ResolvedFonts,
    *,
    identifier: str,
    created: datetime | None = None,
    cover: tuple[bytes, str] | None = None,
) -> EpubBuild:
    """The EPUB of a book model (pure: no database). `cover` is the cover's picture `(bytes, media type)`
    (`publishing.cover.export_raster`), None for a book without one. See the module docstring."""
    from ebooklib import epub

    when = _utc(created)
    front = _safe_front(book.front)  # the metadata and the NCX go through lxml, which refuses them
    package = epub.EpubBook()
    package.set_identifier(identifier)
    package.title = front.title
    package.add_metadata("DC", "title", front.title, {"id": "title"})
    package.add_metadata(None, "meta", "main", {"refines": "#title", "property": "title-type"})
    if front.subtitle:
        package.add_metadata("DC", "title", front.subtitle, {"id": "subtitle"})
        package.add_metadata(None, "meta", "subtitle", {"refines": "#subtitle", "property": "title-type"})
    package.set_language("ar")
    package.set_direction("rtl")
    for name, element in (("author", "creator"), ("editor", "contributor"), ("translator", "contributor")):
        value = getattr(front, name)
        if value:
            package.add_metadata("DC", element, value, {"id": name})
            package.add_metadata(
                None,
                "meta",
                MARC_ROLES[name],
                {"refines": f"#{name}", "property": "role", "scheme": "marc:relators"},
            )
    if front.publisher:
        package.add_metadata("DC", "publisher", front.publisher)
    year = publication_year(front.year)
    if year:
        package.add_metadata("DC", "date", year)
    if front.rights:
        package.add_metadata("DC", "rights", front.rights)
    package.add_prefix("ibooks", IBOOKS_PREFIX)
    package.add_metadata(None, "meta", "true", {"property": "ibooks:specified-fonts"})

    def item(uid: str, file_name: str, media_type: str, content: bytes):
        found = epub.EpubItem(uid=uid, file_name=file_name, media_type=media_type, content=content)
        package.add_item(found)
        return found

    css = epub_css(book.setup, fonts) + ("\n".join(COVER_CSS) + "\n" if cover is not None else "")
    item("css", "styles/book.css", "text/css", css.encode())
    amiri = F.VENDORED_DIR / "amiri"
    for index, (_weight, name) in enumerate(AMIRI_FILES, start=1):
        item(f"font-{index}", f"fonts/{name}", FONT_TYPE, (amiri / name).read_bytes())
    item("ofl", "fonts/OFL.txt", "text/plain", Path(OFL_FILE).read_bytes())
    for index, (name, path, media_type) in enumerate(org_font_items(fonts), start=1):  # D98
        if path is None:  # the face's licence notice
            item(f"org-licence-{index}", f"fonts/{name}", "text/plain", media_type.encode())
        else:
            item(f"org-font-{index}", f"fonts/{name}", media_type, path.read_bytes())

    spine: list = []
    landmarks: list[tuple[str, str, str]] = []
    if cover is not None:  # D80: first in the spine, and the library's picture
        data, media_type = cover
        image_name = f"images/cover.{'jpeg' if media_type == 'image/jpeg' else 'png'}"
        picture = epub.EpubCover(uid=COVER_ID, file_name=image_name)
        picture.media_type = media_type
        picture.content = data
        package.add_item(picture)
        package.add_metadata(None, "meta", "", {"name": "cover", "content": COVER_ID})
        spine.append(item("cover", "cover.xhtml", XHTML, cover_page(book, image_name)))
        landmarks.append(("cover", "cover.xhtml", COVER_TITLE))
        package.guide.append({"type": "cover", "href": "cover.xhtml", "title": COVER_TITLE})
    if front.title_page and front.title:
        spine.append(item("title-page", "title.xhtml", XHTML, title_page(book)))
        landmarks.append(("titlepage", "title.xhtml", TITLE_PAGE))
    if front.copyright_page and front.title:
        spine.append(item("copyright-page", "copyright.xhtml", XHTML, copyright_page(book)))
        landmarks.append(("copyright-page", "copyright.xhtml", COPYRIGHT_PAGE))
    docs: dict[str, ChapterDoc] = {}
    for chapter in book.chapters:
        if not chapter.blocks:
            continue  # nothing to print (as in the preview)
        docs[chapter.id] = _ChapterWriter(chapter).write(f"chapter-{len(docs) + 1:02d}.xhtml")
    entries = contents_entries(book, docs)
    if not entries:  # a book with no text: the nav still needs an entry
        entries = [NavEntry(1, front.title or CONTENTS_TITLE, spine[0].file_name if spine else "nav.xhtml")]
    landmarks.append(("toc", "nav.xhtml#toc", CONTENTS_TITLE))
    if docs:
        landmarks.append(("bodymatter", next(iter(docs.values())).file_name, BODY_START))
    nav = item("nav", "nav.xhtml", XHTML, nav_document(entries, landmarks))
    nav.properties = ["nav"]
    if front.contents and book.contents():
        spine.append(nav)
    for index, doc in enumerate(docs.values(), start=1):
        spine.append(item(f"chapter-{index:02d}", doc.file_name, XHTML, doc.content))
    if not spine:  # a book with nothing to read: the nav is its one document (a spine is never empty)
        spine.append(nav)
    package.add_item(epub.EpubNcx())
    package.spine = spine
    package.toc = _ncx_toc(entries)

    raw = io.BytesIO()
    epub.write_epub(raw, package, {"mtime": when.replace(tzinfo=None), "raise_exceptions": True})
    data = _repack(raw.getvalue(), when)
    notes = sum(doc.notes for doc in docs.values())
    return EpubBuild(
        data=data,
        chapters=len(docs),
        footnotes=notes,
        documents=[entry.file_name for entry in spine],
        stats={"chapters": len(docs), "footnotes": notes, "contents": len(entries), "identifier": identifier},
    )


def _ncx_toc(entries: list[NavEntry]) -> list:
    """The NCX (EPUB 2 readers) from the contents entries: a section nested under its chapter."""
    from ebooklib import epub

    toc: list = []
    for index, entry in enumerate(entries, start=1):
        link = epub.Link(entry.href, entry.text, f"nav-{index}")
        if entry.level == 2 and toc:
            last = toc[-1]
            if isinstance(last, tuple):
                last[1].append(link)
            else:
                toc[-1] = (last, [link])
        else:
            toc.append(link)
    return toc


# ====================================================================== the check


def check_epub(data: bytes) -> list[str]:
    """What is wrong with an EPUB file (empty: nothing): see the module docstring."""
    from ebooklib import ITEM_DOCUMENT, epub
    from lxml import etree

    errors: list[str] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        return [f"not a zip file: {exc}"]
    infos = archive.infolist()
    if not infos or infos[0].filename != "mimetype":
        errors.append("mimetype is not the first entry")
    elif infos[0].compress_type != zipfile.ZIP_STORED or infos[0].extra:
        errors.append("mimetype is compressed or has an extra field")
    elif archive.read("mimetype") != MEDIA_TYPE_EPUB.encode():
        errors.append("mimetype is not application/epub+zip")
    try:
        package = epub.read_epub(io.BytesIO(data), {"ignore_ncx": True})
    except Exception as exc:  # noqa: BLE001 - an unreadable package is the error reported
        return [*errors, f"ebooklib cannot read the package: {exc}"]
    if package.direction != "rtl":
        errors.append(f"the spine's page-progression-direction is {package.direction!r}")
    if not package.spine:
        errors.append("the spine is empty: a reader has no document to open")
    fonts = [item.file_name for item in package.get_items() if item.media_type in FONT_TYPES.values()]
    if any(f"fonts/{name}" not in fonts for _weight, name in AMIRI_FILES):
        errors.append(f"the fonts in the manifest are {fonts}")
    for item in package.get_items():  # every face the CSS names is in the package (D98)
        if item.media_type != "text/css":
            continue
        for name in re.findall(r'url\("\.\./(fonts/[^"]+)"\)', item.content.decode("utf-8", "replace")):
            if name not in fonts:
                errors.append(f"{item.file_name}: the font {name} is not in the package")
    navs = [item for item in package.get_items() if isinstance(item, epub.EpubNav)]
    if len(navs) != 1:
        errors.append("the package has no nav document")
    documents = {item.file_name: item for item in [*package.get_items_of_type(ITEM_DOCUMENT), *navs]}
    roots = {}
    for name, item in documents.items():
        try:  # the file's own bytes (`get_content` of a read document rebuilds it from a template)
            roots[name] = etree.fromstring(item.content)
        except etree.XMLSyntaxError as exc:
            errors.append(f"{name} is not well formed: {exc}")
    ids = {name: set(root.xpath("//@id")) for name, root in roots.items()}
    files = {item.file_name for item in package.get_items()}
    for name, root in roots.items():
        if root.get("dir") != "rtl" or root.get("lang") != "ar":
            errors.append(f"{name} is not lang=ar dir=rtl")
        for link in root.xpath("//*[@href]"):
            href = link.get("href") or ""
            if "://" in href or link.tag.endswith("link"):
                continue
            target, _hash, anchor = href.partition("#")
            target = target or name
            if target not in documents or (anchor and anchor not in ids.get(target, ())):
                errors.append(f"{name}: the link {href} has no target")
        for picture in root.xpath("//*[local-name()='img']/@src"):
            if "://" not in picture and picture not in files:
                errors.append(f"{name}: the picture {picture} is not in the package")
        refs = root.xpath("//*[@epub:type='noteref']", namespaces={"epub": NS_EPUB})
        notes = root.xpath("//*[@epub:type='footnote']", namespaces={"epub": NS_EPUB})
        if len(refs) != len(notes):
            errors.append(f"{name}: {len(refs)} calls for {len(notes)} notes")
    return errors


# ====================================================================== the exporter


def epub_notes(book_id: int | None, setup: PageSetup, fonts: F.ResolvedFonts) -> list[dict]:
    """The known differences of the EPUB for this book (§7's rows for EPUB)."""
    rows = [row("font_embedded", INFO, FONT_EMBEDDED)]
    seen: set[str] = set()
    for face in (fonts.body, fonts.heading, fonts.latin):
        if face.key == "amiri" or face.key in seen:
            continue
        seen.add(face.key)
        if F.org_embeds(face, "epub"):  # D98: the organisation's face goes in whole
            rows.append(row("font_embedded_org", INFO, FONT_EMBEDDED_ORG.format(name=face.name)))
            continue
        rows.append(row("font_fallback", INFO, FONT_FALLBACK.format(name=face.name)))
    if book_id is not None:
        rows += missing_font_rows(book_id, setup, code="missing_font")
    rows.append(row("epub_notes", INFO, EPUB_NOTES))
    if setup.print_source_pages:
        rows.append(row("source_pages", INFO, SOURCE_PAGES))
    return rows


class EpubExporter:
    """The EPUB format (see the module docstring)."""

    format = "epub"
    label = "EPUB"
    extension = ".epub"
    media_type = MEDIA_TYPE_EPUB
    options: tuple[OptionSpec, ...] = ()
    version = EPUB_VERSION

    def form(self, book, values: dict) -> dict:
        """No options."""
        return {}

    def notes(self, book, setup: PageSetup) -> list[dict]:
        """The faces (Amiri embedded, the others shown where installed), the notes per chapter, a cover
        picture short of 300 dpi (`cover_resolution`, info)."""
        from .readiness import cover_resolution_row

        fonts = F.resolve(setup.body_font, setup.latin_font, setup.heading_font)
        resolution = cover_resolution_row(book.pk, setup, INFO)
        return epub_notes(book.pk, setup, fonts) + ([resolution] if resolution is not None else [])

    def export(self, job: ExportJob, progress: Progress) -> ExportResult:
        """prepare → write → check."""
        from .cover import EPUB_HEIGHT_PX, export_raster

        progress("prepare")
        book = book_model(job.document, job.setup, title=job.title, author=job.author)
        setup = book.setup
        fonts = F.resolve(setup.body_font, setup.latin_font, setup.heading_font)
        if progress.cancelled():
            raise ExportCancelled
        progress("write")
        cover = export_raster(book, fonts, height=EPUB_HEIGHT_PX) if book.front.cover is not None else None
        if progress.cancelled():
            raise ExportCancelled
        built = build_epub(
            book,
            fonts,
            identifier=book_identifier(job.book_id, book.front.isbn),
            created=job.created,
            cover=cover,
        )
        if progress.cancelled():
            raise ExportCancelled
        progress("check")
        errors = check_epub(built.data)
        if errors:
            raise InvalidExport("\n".join(errors[:30]))
        embedded = ("font_embedded", "font_embedded_org")
        warnings = [item for item in epub_notes(job.book_id, setup, fonts) if item["code"] not in embedded]
        stats = dict(built.stats)
        stats["documents"] = built.documents
        if cover is not None:
            stats["cover"] = {"media_type": cover[1], "bytes": len(cover[0])}
        lines = [
            f"epub: {built.chapters} chapters, {built.footnotes} notes, {len(built.documents)} documents,"
            f" {len(built.data)} bytes",
        ]
        return ExportResult(data=built.data, page_count=None, warnings=warnings, stats=stats, log=lines)


__all__ = [
    "EpubBuild",
    "EpubExporter",
    "book_identifier",
    "build_epub",
    "check_epub",
    "epub_css",
    "epub_notes",
]
