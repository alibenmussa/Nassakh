"""The Word renderer of the book model (D53, PHASE6_SPEC §5): `build_docx(book, fonts, options, …)` is
pure — no database, the same bytes for the same inputs and `meta.now` — and gives the .docx bytes with
what the build found.

The `BookWriter` walks the model once: the front sections (title and copyright page; the contents page
with its prefilled field), then one section per chapter (recto or new-page openings, running headers,
page numbers), writing every paragraph as a `Para` whose direct spacing the collapse rule decides per
flow (§5.5), the footnotes into their part, the comments of uncertain words into theirs, then the
styles, settings, font table (Amiri embedded), document properties and the package.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from lxml import etree

from publishing.fonts import ResolvedFonts
from publishing.model import STYLES, Block, Book, Chapter

from . import ooxml
from .faces import FacePlan, font_rels
from .ooxml import Package, Part, Rel, iso_datetime, root, serialize, w
from .options import COMPAT_MODE, WORD_VERSION, WordOptions, comments_phrase, note, words_phrase
from .runs import CommentsPart, FootnotesPart, RunWriter, WriteStats
from .sections import (
    NUMBERING_RESTART,
    Bookmarks,
    HeaderFooterPlan,
    Para,
    contents_page,
    copyright_page,
    resolve_flow,
    running_text,
    sect_pr,
    title_page,
)
from .styles import style_table, styles_xml, text_width_mm

LAST_MODIFIED_BY = "نسّاخ"
# the run face role of each model style
_ROLE: dict[str, str] = {
    "chapter-title": "heading",
    "section-title": "heading",
    "book-title": "heading",
    "contents-title": "heading",
}


@dataclass(frozen=True)
class PagePlan:
    """The preview's pages for the contents field (§5.9): where it came from, its page count, the
    printed page of every heading's first line, and each chapter's first and last page."""

    source: str  # "live" | "render"
    page_count: int
    headings: dict[str, int] = field(default_factory=dict)
    chapters: dict[str, tuple[int, int]] = field(default_factory=dict)

    @classmethod
    def from_pages(cls, pages: list[dict], source: str, chapters: list[dict] | None = None) -> PagePlan:
        """A plan from a layout's pages (`publishing.layout`): a heading's page is its first line's."""
        headings: dict[str, int] = {}
        ranges: dict[str, list[int]] = {}
        for page in pages:
            number = page.get("n")
            if not isinstance(number, int):
                continue
            chapter = page.get("chapter")
            if isinstance(chapter, str) and chapter:
                ranges.setdefault(chapter, [number, number])
                ranges[chapter][0] = min(ranges[chapter][0], number)
                ranges[chapter][1] = max(ranges[chapter][1], number)
            for line in page.get("lines") or []:
                if line.get("kind") == "heading" and line.get("block"):
                    headings.setdefault(line["block"], number)
        for item in chapters or []:
            if (
                isinstance(item, dict)
                and isinstance(item.get("first"), int)
                and isinstance(item.get("last"), int)
            ):
                ranges.setdefault(str(item.get("id") or ""), [item["first"], item["last"]])
        return cls(
            source=source,
            page_count=len(pages),
            headings=headings,
            chapters={key: (lo, hi) for key, (lo, hi) in ranges.items() if key},
        )


@dataclass(frozen=True)
class DocMeta:
    """What the document properties record (§5.13) and the export time (`now`, UTC)."""

    now: datetime
    book_id: int | None = None
    manuscript_version: int | None = None
    export_id: int | None = None
    renderer: str = WORD_VERSION

    @classmethod
    def fixed(cls) -> DocMeta:
        """A meta with a fixed time (the golden and determinism tests)."""
        return cls(now=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))


@dataclass(frozen=True)
class Properties:
    """The document properties (§5.13): what `docProps/core.xml`, `app.xml` and `custom.xml` record."""

    title: str = ""
    subject: str = ""
    creator: str = ""
    description: str = ""
    company: str = ""
    language: str = "ar"
    custom: tuple[tuple[str, str], ...] = ()


def assemble(
    *,
    document: etree._Element,
    document_rels: list[Rel],
    styles: etree._Element,
    settings: etree._Element,
    faces: FacePlan,
    embedded: list,
    footnotes: etree._Element,
    comments: etree._Element | None,
    parts: list,
    properties: Properties,
    now: datetime,
) -> Package:
    """The package of a document: the OPC relationships, the properties, then every part in the fixed
    order (`document_rels` already lists the styles, settings, font table, footnotes, comments and the
    header / footer `parts`, which carry their names and rIds)."""
    package = Package()
    package.relate(ooxml.REL_DOCUMENT, "word/document.xml")
    package.relate(ooxml.REL_CORE, "docProps/core.xml")
    package.relate(ooxml.REL_APP, "docProps/app.xml")
    package.relate(ooxml.REL_CUSTOM, "docProps/custom.xml")
    package.add(Part("docProps/app.xml", ooxml.CT_APP, ooxml.app_xml(company=properties.company)))
    package.add(
        Part(
            "docProps/core.xml",
            ooxml.CT_CORE,
            ooxml.core_xml(
                title=properties.title,
                subject=properties.subject,
                creator=properties.creator,
                description=properties.description,
                modified_by=LAST_MODIFIED_BY,
                created=now,
                language=properties.language,
            ),
        )
    )
    package.add(Part("docProps/custom.xml", ooxml.CT_CUSTOM, ooxml.custom_xml(properties.custom)))
    package.add(Part("word/document.xml", ooxml.CT_DOCUMENT, serialize(document), document_rels))
    package.add(Part("word/styles.xml", ooxml.CT_STYLES, serialize(styles)))
    package.add(Part("word/settings.xml", ooxml.CT_SETTINGS, serialize(settings)))
    font_table = faces.font_table(embedded)
    package.add(Part("word/fontTable.xml", ooxml.CT_FONT_TABLE, serialize(font_table), font_rels(embedded)))
    for item in embedded:
        package.add(Part(item.name, ooxml.CT_OBFUSCATED_FONT, item.data))
    package.add(Part("word/footnotes.xml", ooxml.CT_FOOTNOTES, serialize(footnotes)))
    if comments is not None:
        package.add(Part("word/comments.xml", ooxml.CT_COMMENTS, serialize(comments)))
    for part in parts:
        content_type = ooxml.CT_HEADER if part.kind == "hdr" else ooxml.CT_FOOTER
        package.add(Part(part.name, content_type, serialize(part.element)))
    return package


def name_parts(parts: list, document_rels: list[Rel]) -> None:
    """Give the header / footer parts their zip names (`headerN.xml`, `footerN.xml`) and add their
    document relationships."""
    headers = footers = 0
    for part in parts:
        if part.kind == "hdr":
            headers += 1
            part.name = f"word/header{headers}.xml"
        else:
            footers += 1
            part.name = f"word/footer{footers}.xml"
        rel_type = ooxml.REL_HEADER if part.kind == "hdr" else ooxml.REL_FOOTER
        document_rels.append(Rel(part.rel_id, rel_type, part.name.removeprefix("word/")))


def base_rels(with_comments: bool) -> list[Rel]:
    """The document's relationships to its fixed parts (rId5 is the comments part, kept free otherwise
    so header ids never move)."""
    rels = [
        Rel("rId1", ooxml.REL_STYLES, "styles.xml"),
        Rel("rId2", ooxml.REL_SETTINGS, "settings.xml"),
        Rel("rId3", ooxml.REL_FONT_TABLE, "fontTable.xml"),
        Rel("rId4", ooxml.REL_FOOTNOTES, "footnotes.xml"),
    ]
    if with_comments:
        rels.append(Rel("rId5", ooxml.REL_COMMENTS, "comments.xml"))
    return rels


FIRST_PART_REL = 6  # the rId of the first header or footer part


@dataclass
class WordResult:
    """The .docx bytes and what the build found."""

    data: bytes
    warnings: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    log: list[str] = field(default_factory=list)


def prints_contents(book: Book, front_matter: bool = True) -> bool:
    """True when the file prints the contents field: the front matter is written (the book has a
    title), the setup asks for contents and the book has a heading with text."""
    return bool(front_matter and book.front.title and book.front.contents and book.headings())


def duplicate_containers(book: Book) -> frozenset[tuple[str, str | None]]:
    """`(block id, note id)` keys that occur more than once (their words get no comment, §5.11)."""
    counts: Counter[tuple[str, str | None]] = Counter()
    for block in book.blocks():
        counts[(block.id, None)] += 1
        for note_ in block.footnotes:
            counts[(block.id, note_.id)] += 1
    return frozenset(key for key, count in counts.items() if count > 1)


@dataclass
class Section:
    """A section being written: its paragraphs, the break that opens it (None: the first section),
    its running header text and whether it is a body section."""

    paras: list[Para]
    break_type: str | None
    running: str = ""
    body: bool = False


class BookWriter:
    """One build of one book (see the module docstring)."""

    def __init__(
        self,
        book: Book,
        fonts: ResolvedFonts,
        options: WordOptions,
        *,
        plan: PagePlan | None,
        readings: Mapping[tuple[str, str | None], list] | None,
        meta: DocMeta,
        embed_fonts: bool,
        front_matter: bool,
        compat_mode: int | None = None,
    ):
        self.book = book
        self.setup = book.setup
        self.options = options
        self.plan = plan
        self.meta = meta
        self.embed_fonts = embed_fonts
        self.front_matter = front_matter
        self.compat_mode = compat_mode if compat_mode is not None else COMPAT_MODE
        self.faces = FacePlan(fonts)
        self.table = style_table(self.setup)
        self.stats = WriteStats()
        self.notes = FootnotesPart()
        self.comments: CommentsPart | None = None
        self.bookmarks = Bookmarks()
        self.warnings: list[dict] = []
        self.log: list[str] = []
        self.sections: list[Section] = []
        self.writer = RunWriter(self.faces, notes=self.notes, stats=self.stats)
        if options.comments and readings is not None:
            self.comments = CommentsPart(self.writer.text_runs, iso_datetime(meta.now))
            self.writer.comments = self.comments
            self.writer.readings = readings
            self.writer.duplicates = duplicate_containers(book)
        self.headers = HeaderFooterPlan(self.setup, self.writer)

    @property
    def opening(self) -> str:
        return "oddPage" if self.setup.chapter_opening == "recto" else "nextPage"

    def _open(
        self, paras: list[Para], running: str = "", body: bool = False, break_type: str | None = None
    ) -> Section:
        section = Section(paras, None if not self.sections else (break_type or self.opening), running, body)
        self.sections.append(section)
        return section

    # ------------------------------------------------------------------ the front matter (§5.9)

    def front(self) -> None:
        front = self.book.front
        if not self.front_matter or not front.title:
            return
        paras: list[Para] = []
        if front.title_page:
            paras += title_page(self.book, self.writer)
        if front.copyright_page:
            paras += copyright_page(self.book, self.writer)
            if not front.title_page:  # the copyright page opens the book: no break before it
                paras[0].page_break = False
        if paras:
            self._open(paras)
        if prints_contents(self.book, self.front_matter):
            pages = self.plan.headings if self.plan is not None else None
            contents = contents_page(self.book, self.writer, self.bookmarks, pages)
            if contents:
                if self.plan is None:
                    self.warnings.append(note("toc_numbers_missing"))
                elif any(self.plan.headings.get(entry.target) is None for entry in self.book.contents()):
                    self.log.append("contents: some entries have no page in the plan")
                self._open(contents)

    # ------------------------------------------------------------------ the body (§5.7)

    def block_para(self, block: Block, chapter: Chapter, opens_page: bool) -> Para:
        style = STYLES[block.style].word if block.style in STYLES else "Normal"
        role = _ROLE.get(block.style, "body")
        content = self.writer.inline(block.runs, role, block_id=block.id, footnotes=block.footnotes)
        if block.style in ("chapter-title", "section-title") and block.text().strip():
            content = self.bookmarks.wrap(self.bookmarks.heading(block), content)
        elif block is chapter.blocks[0] and chapter.kind != "chapter":
            content = self.bookmarks.wrap(self.bookmarks.new("_nk_ch_", chapter.id), content)
        return Para(
            style, content, page_break=block.break_before and not opens_page, keep_next=block.keep_with_next
        )

    def body(self) -> None:
        """One section per chapter of kind `chapter` or `front`; `section` chapters run on (their own
        continuous section only when notes are numbered per chapter); empty chapters are skipped."""
        per_chapter = self.setup.footnote_numbering == "chapter"
        current: Section | None = None
        body_started = False
        for chapter in self.book.chapters:
            if not chapter.blocks:
                continue
            if chapter.kind != "section" or current is None or per_chapter:
                break_type = "continuous" if chapter.kind == "section" and current is not None else None
                current = self._open(
                    [], running_text(self.book, chapter.heading), body=True, break_type=break_type
                )
            for index, block in enumerate(chapter.blocks):
                opens = not body_started or (index == 0 and chapter.kind != "section")
                current.paras.append(self.block_para(block, chapter, opens))
                body_started = True
        if current is None:  # no text at all: the body still gets one paragraph and a sectPr
            self._open([Para("Normal")], running_text(self.book, ""), body=True)

    # ------------------------------------------------------------------ assembling

    def document(self) -> etree._Element:
        """`word/document.xml`: every section's paragraphs, with the section properties in its last
        paragraph (the last section's at the end of the body)."""
        body = w("body")
        for index, section in enumerate(self.sections):
            references: list[tuple[str, str, str]] = []
            title_pg = False
            if section.body:
                parts, title_pg = self.headers.references(
                    section.running, continuous=section.break_type == "continuous"
                )
                references = [(part.kind, part.type, part.rel_id) for part in parts]
            props = sect_pr(
                self.setup,
                self.table,
                break_type=section.break_type,
                references=references,
                title_pg=title_pg,
            )
            if not section.paras:
                section.paras.append(Para("Normal"))
            resolve_flow(section.paras, self.table)
            last = index == len(self.sections) - 1
            for position, para in enumerate(section.paras):
                if position == len(section.paras) - 1 and not last:
                    para.sect_pr = props
                body.append(para.element())
            if last:
                body.append(props)
        return root("document", body)

    def properties(self) -> Properties:
        front = self.book.front
        custom: list[tuple[str, str]] = [
            (name, value)
            for name, value in (
                ("ISBN", front.isbn),
                ("Edition", front.edition),
                ("Publisher", front.publisher),
                ("City", front.city),
                ("Year", front.year),
            )
            if value
        ]
        for name, value in (
            ("NassakhBook", self.meta.book_id),
            ("NassakhManuscriptVersion", self.meta.manuscript_version),
            ("NassakhExport", self.meta.export_id),
            ("NassakhRenderer", self.meta.renderer),
        ):
            if value is not None:
                custom.append((name, str(value)))
        return Properties(
            title=front.title,
            subject=front.subtitle,
            creator=front.author,
            description=" ".join(front.rights.split()),
            company=front.publisher,
            language=self.book.language,
            custom=tuple(custom),
        )

    def build(self) -> WordResult:
        started = time.monotonic()
        self.front()
        self.body()
        # the comments part exists only when a comment was written, so the document is built first;
        # its rId is kept free either way, and the header parts count from FIRST_PART_REL
        self.headers.next_rel = FIRST_PART_REL
        document = self.document()
        with_comments = self.comments is not None and self.comments.count > 0
        document_rels = base_rels(with_comments)
        name_parts(self.headers.parts, document_rels)
        embedded = self.faces.embedded_files(self.embed_fonts)
        settings = ooxml.settings_xml(
            embed_fonts=bool(embedded),
            even_and_odd=self.headers.even_and_odd,
            footnote_numbering=NUMBERING_RESTART.get(self.setup.footnote_numbering, "eachPage"),
            number_format="decimal",
            compat_mode=self.compat_mode,
        )
        package = assemble(
            document=document,
            document_rels=document_rels,
            styles=styles_xml(self.setup, self.faces, self.options.jc),
            settings=settings,
            faces=self.faces,
            embedded=embedded,
            footnotes=self.notes.root,
            comments=self.comments.root if with_comments else None,
            parts=self.headers.parts,
            properties=self.properties(),
            now=self.meta.now,
        )
        data = package.to_bytes()
        if self.stats.comments_skipped:
            self.warnings.append(note("comments_skipped", phrase=words_phrase(self.stats.comments_skipped)))
        if self.stats.comments:
            self.warnings.append(note("comments", phrase=comments_phrase(self.stats.comments)))
        stats = {
            "paragraphs": sum(len(section.paras) for section in self.sections),
            "footnotes": self.stats.footnotes,
            "comments": self.stats.comments,
            "comments_skipped": self.stats.comments_skipped,
            "sections": len(self.sections),
            "front_sections": sum(1 for section in self.sections if not section.body),
            "embedded_fonts": [item.name for item in embedded],
            "families": self.faces.families(),
            "parts": len(package.parts),
            "size_bytes": len(data),
            "plan": self.plan.source if self.plan is not None else None,
            "text_width_mm": round(text_width_mm(self.setup), 2),
            "build_ms": int((time.monotonic() - started) * 1000),
        }
        return WordResult(data=data, warnings=self.warnings, stats=stats, log=self.log)


def build_docx(
    book: Book,
    fonts: ResolvedFonts,
    options: WordOptions,
    *,
    plan: PagePlan | None = None,
    readings: Mapping[tuple[str, str | None], list] | None = None,
    meta: DocMeta,
    embed_fonts: bool = True,
    front_matter: bool = True,
    compat_mode: int | None = None,
) -> WordResult:
    """The book as a .docx (pure). `embed_fonts=False` keeps the golden files small; `front_matter=False`
    is the harness's one-chapter check; `compat_mode` overrides `COMPAT_MODE` (the harness's C0). Same
    inputs and `meta.now` give the same bytes."""
    writer = BookWriter(
        book,
        fonts,
        options,
        plan=plan,
        readings=readings,
        meta=meta,
        embed_fonts=embed_fonts,
        front_matter=front_matter,
        compat_mode=compat_mode,
    )
    return writer.build()
