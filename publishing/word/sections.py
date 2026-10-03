"""Page setup, sections, headers and footers, the front matter and bookmarks (PHASE6_SPEC §5.7, §5.9).

A `Para` is a paragraph waiting to be written: its style, content, the CSS margins the collapse rule
needs, and the direct spacing that rule decides (`resolve_flow`). Section properties are built fresh for
every section (spike lesson 3) by `sect_pr`; `HeaderFooterPlan` decides which header and footer parts a
body section declares (the first declares all it uses, later ones only what differs, since Word
inherits the rest). The cover (D80) is `cover_para`: one paragraph holding the cover's picture anchored
to the page at 0, 0, the page's exact size, behind the text; its section declares no header or footer,
and the section after it restarts the page numbers at 1 (`sect_pr(page_start=1)`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

from publishing.html import CONTENTS_TITLE, CREDIT_LABELS, EDITION_LABEL, ISBN_LABEL
from publishing.model import Block, Book, PageSetup

from .ooxml import (
    A_NS,
    PIC_NS,
    R_NS,
    WP_NS,
    emu_mm,
    field_runs,
    fld_char,
    mm_to_pt,
    pt_to_mm,
    root,
    run,
    text,
    twips,
    twips_mm,
    w,
)
from .options import BIDI_LEFT_IS_START, MIRROR_PGMAR_LEFT
from .runs import RunWriter, rpr
from .styles import ParaStyle, collapse, split_gap, text_width_mm

_BOOKMARK_SAFE = re.compile(r"[^A-Za-z0-9_]")
BOOKMARK_MAX = 40
TOC_INSTRUCTION = 'TOC \\o "1-2" \\h \\z \\u'
NUMBERING_RESTART: dict[str, str] = {"page": "eachPage", "chapter": "eachSect", "book": "continuous"}


# ====================================================================== paragraphs


@dataclass
class Para:
    """A paragraph to write: `style` (a Word style id), its content elements, the CSS margins used by
    the collapse rule (None: the style's), a padding that never collapses (`extra_before`, the title
    page's 28 %), flags, and what the flow decided (`direct_before` / `direct_after`, mm)."""

    style: str
    content: list[etree._Element] = field(default_factory=list)
    css_before: float | None = None
    css_after: float | None = None
    extra_before: float = 0.0
    page_break: bool = False
    keep_next: bool = False
    ltr: bool = False
    jc: str | None = None
    tabs: etree._Element | None = None
    direct_before: float | None = None
    direct_after: float | None = None
    sect_pr: etree._Element | None = None
    extra: list[etree._Element] = field(default_factory=list)  # more pPr children (direct formatting)
    line: int | None = None  # D99: an exact line pitch of its own (twips: a block's text size option)

    def element(self) -> etree._Element:
        spacing = None
        if self.direct_before is not None or self.direct_after is not None or self.line is not None:
            spacing = w(
                "spacing",
                before=twips_mm(self.direct_before) if self.direct_before is not None else None,
                after=twips_mm(self.direct_after) if self.direct_after is not None else None,
                line=self.line,
                lineRule="exact" if self.line is not None else None,
            )
        ppr = w(
            "pPr",
            w("pStyle", val=self.style) if self.style != "Normal" else None,  # Word's convention
            w("keepNext") if self.keep_next else None,
            w("pageBreakBefore") if self.page_break else None,
            self.tabs,
            w("bidi", val=0) if self.ltr else None,
            spacing,
            w("jc", val=self.jc) if self.jc else None,
            *self.extra,
            self.sect_pr,
        )
        return w("p", ppr, *self.content)


def resolve_flow(paras: list[Para], table: dict[str, ParaStyle]) -> None:
    """Decide the direct spacing of consecutive paragraphs of one flow (§5.5): where the CSS collapsed
    gap differs from what Word adds up, one of the two gets a direct value. A page break starts a new
    flow; a paragraph's own CSS override (an imprint's 30 mm) counts as its margin."""
    flows: list[list[Para]] = []
    for para in paras:
        if not flows or para.page_break:
            flows.append([para])
        else:
            flows[-1].append(para)
    for flow in flows:
        for previous, current in zip(flow, flow[1:], strict=False):
            prev_style, cur_style = table[previous.style], table[current.style]
            css_after = previous.css_after if previous.css_after is not None else prev_style.after
            css_before = current.css_before if current.css_before is not None else cur_style.before
            gap = collapse(css_after, css_before)
            after, before = split_gap(prev_style.after, cur_style.written_before, gap)
            if after is not None:
                previous.direct_after = after
            if before is not None:
                current.direct_before = before
        first, last = flow[0], flow[-1]
        first_style, last_style = table[first.style], table[last.style]
        if first.css_before is not None and first.direct_before is None:
            if abs(max(first.css_before, 0) - first_style.written_before) > 0.005:
                first.direct_before = max(first.css_before, 0)
        if last.css_after is not None and last.direct_after is None:
            if abs(last.css_after - last_style.after) > 0.005:
                last.direct_after = last.css_after
        for para in flow:
            if para.extra_before:
                base = (
                    para.direct_before if para.direct_before is not None else table[para.style].written_before
                )
                para.direct_before = base + para.extra_before


# ====================================================================== bookmarks


class Bookmarks:
    """Unique bookmark names (`_Toc_<id>`, `_nk_ch_<id>`, at most 40 characters) and their integer ids.

    `heading(block)` names a heading once per occurrence, so its contents entry and the heading share
    the name while two headings with the same block id (or none) each get their own; `new` gives a
    fresh name on every call; `name` one per `(prefix, value)`, for callers whose values are unique
    (the calibration file's item codes)."""

    def __init__(self) -> None:
        self.names: dict[tuple[str, str], str] = {}
        self.headings: dict[int, str] = {}
        self.taken: set[str] = set()
        self.next_id = 0

    def new(self, prefix: str, value: str) -> str:
        """A name not given before: the prefix and `value` made safe, cut to 40 characters, then `_2`,
        `_3`… while it is taken."""
        base = _BOOKMARK_SAFE.sub("_", value or "") or "x"
        candidate = (prefix + base)[:BOOKMARK_MAX]
        k = 2
        while candidate in self.taken:
            suffix = f"_{k}"
            candidate = (prefix + base)[: BOOKMARK_MAX - len(suffix)] + suffix
            k += 1
        self.taken.add(candidate)
        return candidate

    def name(self, prefix: str, value: str) -> str:
        """The name of `(prefix, value)`, the same on every call."""
        key = (prefix, value)
        if key not in self.names:
            self.names[key] = self.new(prefix, value)
        return self.names[key]

    def heading(self, block: Block) -> str:
        """The `_Toc_<id>` bookmark of this heading block (the same object, the same name)."""
        key = id(block)
        if key not in self.headings:
            self.headings[key] = self.new("_Toc_", block.id)
        return self.headings[key]

    def wrap(self, name: str, content: list[etree._Element]) -> list[etree._Element]:
        """`content` between a `bookmarkStart` and its `bookmarkEnd`."""
        mark_id = self.next_id
        self.next_id += 1
        return [w("bookmarkStart", id=mark_id, name=name), *content, w("bookmarkEnd", id=mark_id)]


# ====================================================================== page setup and sections


def page_size(setup: PageSetup) -> etree._Element:
    return w("pgSz", w=twips_mm(setup.width_mm), h=twips_mm(setup.height_mm))


def header_footer_distances(setup: PageSetup, table: dict[str, ParaStyle]) -> tuple[float, float]:
    """(header, footer) distances in mm: the preview's 3 mm and 4 mm paddings, never pushing the text."""
    header_line = pt_to_mm(table["Header"].line_pt)
    footer_line = pt_to_mm(table["Footer"].line_pt)
    return max(4.0, setup.top_mm - 3 - header_line), max(4.0, setup.bottom_mm - 4 - footer_line)


def page_margins(
    setup: PageSetup, table: dict[str, ParaStyle], pgmar_left: str | None = None
) -> etree._Element:
    """`w:pgMar`: `pgmar_left` (`inner` | `outer`, the calibration file's C1) overrides the convention."""
    inner, outer = setup.inner_mm, setup.outer_mm
    side = pgmar_left or MIRROR_PGMAR_LEFT
    left, right = (inner, outer) if side == "inner" else (outer, inner)
    header, footer = header_footer_distances(setup, table)
    return w(
        "pgMar",
        top=twips_mm(setup.top_mm),
        right=twips_mm(right),
        bottom=twips_mm(setup.bottom_mm),
        left=twips_mm(left),
        header=twips_mm(header),
        footer=twips_mm(footer),
        gutter=0,
    )


def sect_pr(
    setup: PageSetup,
    table: dict[str, ParaStyle],
    *,
    break_type: str | None,
    references: list[tuple[str, str, str]] = (),
    title_pg: bool = False,
    number_format: str = "decimal",
    pgmar_left: str | None = None,
    page_start: int | None = None,
) -> etree._Element:
    """A section's properties, built fresh: its header and footer references `(hdr|ftr, type, rId)`,
    its own footnote numbering, the break type that opens it (None for the first section), the trim,
    the mirrored margins (`pgmar_left` overrides the C1 convention), the page number it restarts at
    (`page_start`: the section after the cover, D80), `titlePg` and `bidi`."""
    restart = NUMBERING_RESTART.get(setup.footnote_numbering, "eachPage")
    refs = [
        w("headerReference" if kind == "hdr" else "footerReference", type=type_, r_id=rel_id)
        for kind, type_, rel_id in references
    ]
    return w(
        "sectPr",
        *refs,
        w("footnotePr", w("numFmt", val=number_format), w("numRestart", val=restart)),
        w("type", val=break_type) if break_type else None,
        page_size(setup),
        page_margins(setup, table, pgmar_left),
        w("pgNumType", start=page_start) if page_start is not None else None,
        w("titlePg") if title_pg else None,
        w("bidi"),
    )


# ====================================================================== the cover (D80)

COVER_NAME = "الغلاف"


def _dml(namespace: str, prefix: str, tag: str, *children, **attrs) -> etree._Element:
    """A DrawingML element (`wp:`, `a:`, `pic:`) with its attributes as strings (`r_embed` → `r:embed`)."""
    nsmap = {prefix: namespace, **({"r": R_NS} if any(name.startswith("r_") for name in attrs) else {})}
    node = etree.Element(f"{{{namespace}}}{tag}", nsmap=nsmap)
    for name, value in attrs.items():
        key = f"{{{R_NS}}}{name[2:]}" if name.startswith("r_") else name
        node.set(key, str(int(value) if isinstance(value, bool) else value))
    for child in children:
        if isinstance(child, str):
            node.text = child
        elif child is not None:
            node.append(child)
    return node


def _wp(tag: str, *children, **attrs) -> etree._Element:
    return _dml(WP_NS, "wp", tag, *children, **attrs)


def _a(tag: str, *children, **attrs) -> etree._Element:
    return _dml(A_NS, "a", tag, *children, **attrs)


def _pic(tag: str, *children, **attrs) -> etree._Element:
    return _dml(PIC_NS, "pic", tag, *children, **attrs)


def cover_drawing(rel_id: str, width_mm: float, height_mm: float, file_name: str) -> etree._Element:
    """`w:drawing` of the cover's picture: anchored to the page at 0, 0, `width_mm` × `height_mm` (the
    page's size), behind the text, no wrapping, locked; the picture's part is relationship `rel_id`."""
    cx, cy = emu_mm(width_mm), emu_mm(height_mm)
    picture = _pic(
        "pic",
        _pic("nvPicPr", _pic("cNvPr", id=0, name=file_name), _pic("cNvPicPr")),
        _pic("blipFill", _a("blip", r_embed=rel_id), _a("stretch", _a("fillRect"))),
        _pic(
            "spPr",
            _a("xfrm", _a("off", x=0, y=0), _a("ext", cx=cx, cy=cy)),
            _a("prstGeom", _a("avLst"), prst="rect"),
        ),
    )
    anchor = _wp(
        "anchor",
        _wp("simplePos", x=0, y=0),
        _wp("positionH", _wp("posOffset", "0"), relativeFrom="page"),
        _wp("positionV", _wp("posOffset", "0"), relativeFrom="page"),
        _wp("extent", cx=cx, cy=cy),
        _wp("effectExtent", l=0, t=0, r=0, b=0),
        _wp("wrapNone"),
        _wp("docPr", id=1, name=COVER_NAME),
        _wp("cNvGraphicFramePr", _a("graphicFrameLocks", noChangeAspect=True)),
        _a("graphic", _a("graphicData", picture, uri=PIC_NS)),
        distT=0,
        distB=0,
        distL=0,
        distR=0,
        simplePos=False,
        relativeHeight=0,
        behindDoc=True,
        locked=True,
        layoutInCell=True,
        allowOverlap=True,
    )
    drawing = w("drawing")
    drawing.append(anchor)
    return drawing


def cover_para(rel_id: str, width_mm: float, height_mm: float, file_name: str) -> Para:
    """The cover section's one paragraph: the anchored picture (`cover_drawing`) in its only run."""
    return Para("Normal", [run(cover_drawing(rel_id, width_mm, height_mm, file_name))])


# ====================================================================== headers and footers


@dataclass
class HdrFtrPart:
    """A header or footer part: its root element and, once registered, its zip name and rId."""

    kind: str  # hdr | ftr
    type: str  # default | even | first
    element: etree._Element
    name: str = ""
    rel_id: str = ""


def page_field(rpr_factory=None) -> list[etree._Element]:
    """The PAGE field with the cached result `1`."""
    return field_runs("PAGE", [run(text("1"), rpr=rpr_factory() if rpr_factory else None)], rpr_factory)


def _instruction(value: str, rpr_factory) -> etree._Element:
    """An `instrText` run holding `value` exactly (a piece of a field code around a nested field)."""
    return run(w("instrText", value, xml_space="preserve"), rpr=rpr_factory())


def parity_page_field(odd: bool, rpr_factory) -> list[etree._Element]:
    """The page number on odd pages only (`odd`) or on even pages only:
    `IF { =MOD({ PAGE },2) } = 1 "{ PAGE }" ""` (`= 0` for even), which Word evaluates on every page
    of a header or footer. The cached results are page 1's."""

    def begin():
        return fld_char("begin", rpr_factory())

    def separate():
        return fld_char("separate", rpr_factory())

    def end():
        return fld_char("end", rpr_factory())

    def one():
        return run(text("1"), rpr=rpr_factory())

    remainder = [begin(), _instruction(" =MOD(", rpr_factory), *page_field(rpr_factory)]
    remainder += [_instruction(",2) ", rpr_factory), separate(), one(), end()]
    return [
        begin(),
        _instruction(" IF ", rpr_factory),
        *remainder,
        _instruction(f' = {1 if odd else 0} "', rpr_factory),
        *page_field(rpr_factory),
        _instruction('" "" ', rpr_factory),
        separate(),
        *([one()] if odd else []),
        end(),
    ]


def _number_rpr() -> etree._Element:
    return rpr(rstyle="PageNumber")


class HeaderFooterPlan:
    """The header and footer parts of the body sections (§5.7's table).

    A part's signature is `(kind, side, running text)`. `side` is where the page number sits (`left`,
    `right`, `center`), `empty` for an opener's header without a number, or `parity` for the part of a
    chapter opener with outer numbers when an opener may fall on either side (`chapter_opening` any):
    the number at the left on odd pages and at the right on even ones, by a field."""

    def __init__(self, setup: PageSetup, writer: RunWriter):
        self.setup = setup
        self.writer = writer
        self.effective: dict[tuple[str, str], tuple] = {}
        self.parts: list[HdrFtrPart] = []
        self.next_rel = 1  # the document relationship id of the next part declared

    @property
    def even_and_odd(self) -> bool:
        return self.setup.page_number in ("bottom_outer", "top_outer")

    @property
    def numbers(self) -> str:
        return (
            self.setup.page_number
            if self.setup.page_number in ("bottom_center", "bottom_outer", "top_outer")
            else "none"
        )

    def needed(
        self, running_text: str, *, continuous: bool = False
    ) -> tuple[dict[tuple[str, str], tuple], bool]:
        """`(kind, type) → signature` of the parts a section wants, and whether it sets `titlePg`: a
        chapter opener (a section that starts a page) with a running header; never a continuous
        section, whose first page is not an opener."""
        numbers = self.numbers
        title_pg = bool(running_text) and not continuous
        # with recto openings every opener is an odd page; else an opener's number follows its page
        opener_side = "left" if self.setup.chapter_opening == "recto" else "parity"
        wanted: dict[tuple[str, str], tuple] = {}
        if numbers == "bottom_center":
            wanted[("ftr", "default")] = ("ftr", "center", "")
        elif numbers == "bottom_outer":
            wanted[("ftr", "default")] = ("ftr", "left", "")
            wanted[("ftr", "even")] = ("ftr", "right", "")
        if numbers == "top_outer":
            wanted[("hdr", "default")] = ("hdr", "left", running_text)
            wanted[("hdr", "even")] = ("hdr", "right", running_text)
            if title_pg:
                wanted[("hdr", "first")] = ("hdr", opener_side, "")
        elif running_text:
            wanted[("hdr", "default")] = ("hdr", "center", running_text)
            if self.even_and_odd:
                wanted[("hdr", "even")] = ("hdr", "center", running_text)
            if title_pg:
                wanted[("hdr", "first")] = ("hdr", "empty", "")
        if title_pg and ("ftr", "default") in wanted:  # the opener keeps its page number (the preview does)
            footer = wanted[("ftr", "default")]
            wanted[("ftr", "first")] = ("ftr", opener_side, "") if numbers == "bottom_outer" else footer
        return wanted, title_pg

    def references(self, running_text: str, *, continuous: bool = False) -> tuple[list[HdrFtrPart], bool]:
        """The parts a section declares (new parts, to register) and its `titlePg`."""
        wanted, title_pg = self.needed(running_text, continuous=continuous)
        declared: list[HdrFtrPart] = []
        for key, signature in wanted.items():
            if self.effective.get(key) == signature:
                continue
            part = HdrFtrPart(key[0], key[1], self.build(signature), rel_id=f"rId{self.next_rel}")
            self.next_rel += 1
            self.parts.append(part)
            declared.append(part)
            self.effective[key] = signature
        return declared, title_pg

    def build(self, signature: tuple) -> etree._Element:
        kind, side, running_text = signature
        style = "Header" if kind == "hdr" else "Footer"
        width = mm_to_pt(text_width_mm(self.setup))
        if side == "parity":
            # an LTR paragraph (left and right are physical) with a right tab at the text's end: the odd
            # pages' number before the tab, the even pages' after it
            ppr = w(
                "pPr",
                w("pStyle", val=style),
                w("tabs", w("tab", val="right", pos=twips(width))),
                w("bidi", val=0),
                w("jc", val="left"),
            )
            content = [
                *parity_page_field(True, _number_rpr),
                run(w("tab")),
                *parity_page_field(False, _number_rpr),
            ]
            return root(kind, w("p", ppr, *content))
        if kind == "ftr":
            jc = None if side == "center" else side
            paragraph = w(
                "p",
                w("pPr", w("pStyle", val="Footer"), w("jc", val=jc) if jc else None),
                *page_field(_number_rpr),
            )
            return root("ftr", paragraph)
        if side == "empty":
            return root("hdr", w("p", w("pPr", w("pStyle", val="Header"))))
        if side == "center":
            return root(
                "hdr", w("p", w("pPr", w("pStyle", val="Header")), *self.writer.text_runs(running_text))
            )
        # top_outer: an LTR paragraph (left and right are physical) with the number at the outer edge
        text_runs = self.writer.text_runs(running_text) if running_text else []
        if not text_runs:  # the number alone: aligned to its edge, no tab stops to land on
            ppr = w("pPr", w("pStyle", val="Header"), w("bidi", val=0), w("jc", val=side))
            return root("hdr", w("p", ppr, *page_field(_number_rpr)))
        # with the running text at a centre tab
        tabs = w(
            "tabs", w("tab", val="center", pos=twips(width / 2)), w("tab", val="right", pos=twips(width))
        )
        ppr = w("pPr", w("pStyle", val="Header"), tabs, w("bidi", val=0), w("jc", val="left"))
        if side == "left":
            content = [*page_field(_number_rpr), run(w("tab")), *text_runs]
        else:
            content = [run(w("tab")), *text_runs, run(w("tab")), *page_field(_number_rpr)]
        return root("hdr", w("p", ppr, *content))


# ====================================================================== front matter (§5.9)


def running_text(book: Book, heading: str) -> str:
    """The running header's text for a chapter (`html._Writer.running_text`)."""
    mode = book.setup.running_header
    if mode == "book":
        return book.front.title
    if mode == "chapter":
        return heading or book.front.title
    return ""


def _credit(name: str, value: str) -> str:
    label = CREDIT_LABELS[name]
    return value if value.startswith(label) else f"{label}: {value}"


def imprint_line(book: Book) -> str:
    front = book.front
    return "، ".join(value for value in (front.publisher, front.city, front.year) if value)


def title_page(book: Book, writer: RunWriter) -> list[Para]:
    """The title page's paragraphs: the title padded 28 % of the text width from the top, the subtitle,
    the author, the credits and the imprint 30 mm lower."""
    front = book.front
    width = text_width_mm(book.setup)
    paras = [Para("Title", writer.text_runs(front.title, role="heading"), extra_before=0.28 * width)]
    if front.subtitle:
        paras.append(Para("Subtitle", writer.text_runs(front.subtitle)))
    if front.author:
        paras.append(Para("NkAuthor", writer.text_runs(front.author)))
    for name in ("editor", "translator"):
        value = getattr(front, name)
        if value:
            paras.append(Para("NkCredit", writer.text_runs(_credit(name, value))))
    place = imprint_line(book)
    if place:
        paras.append(Para("NkImprint", writer.text_runs(place), css_before=30))
    return paras


def copyright_lines(book: Book) -> list[str]:
    """The copyright page's lines as `html.copyright_page` writes them."""
    front = book.front
    lines = [front.title]
    if front.subtitle:
        lines.append(front.subtitle)
    if front.author:
        lines.append(front.author)
    for name in ("editor", "translator"):
        value = getattr(front, name)
        if value:
            lines.append(_credit(name, value))
    if front.edition:
        edition = front.edition
        lines.append(edition if edition.startswith(EDITION_LABEL) else f"{EDITION_LABEL} {edition}")
    place = imprint_line(book)
    if place:
        lines.append(place)
    if front.isbn:
        lines.append(f"{ISBN_LABEL}: {front.isbn}")
    if front.rights:
        lines.append(" ".join(front.rights.split()))
    return lines


def copyright_page(book: Book, writer: RunWriter) -> list[Para]:
    """The copyright page: a page break, 70 % of the width from the top, then the lines."""
    width = text_width_mm(book.setup)
    paras: list[Para] = []
    for index, line in enumerate(copyright_lines(book)):
        paras.append(
            Para(
                "NkCopyright",
                writer.text_runs(line),
                page_break=index == 0,
                extra_before=0.70 * width if index == 0 else 0.0,
            )
        )
    return paras


def contents_page(
    book: Book, writer: RunWriter, bookmarks: Bookmarks, pages: dict[str, int] | None
) -> list[Para]:
    """«المحتويات» and the TOC field with its result written in advance: one `TOC1` / `TOC2` paragraph
    per entry, each a hyperlink to its own heading's bookmark (`Bookmarks.heading`, the one the heading
    gets), the end tab and a PAGEREF with the page the preview printed (no number when the plan has
    none)."""
    entries = book.contents()
    if not entries:
        return []
    paras = [Para("TOCHeading", writer.text_runs(CONTENTS_TITLE, role="heading"))]
    for index, (entry, (_chapter, block)) in enumerate(zip(entries, book.headings(), strict=True)):
        name = bookmarks.heading(block)
        page = (pages or {}).get(entry.target)
        result = [run(text(str(page)))] if page is not None else []
        link = w(
            "hyperlink",
            *writer.text_runs(entry.text),
            run(w("tab")),
            *field_runs(f"PAGEREF {name} \\h", result),
            anchor=name,
            history=1,
        )
        content: list[etree._Element] = []
        if index == 0:
            content += field_runs(TOC_INSTRUCTION, [])[:3]  # begin, the instruction, separate
        content.append(link)
        if index == len(entries) - 1:
            content.append(fld_char("end"))
        paras.append(Para("TOC2" if min(entry.level, 2) == 2 else "TOC1", content))
    return paras


def start_tab_side() -> str:
    """The `w:jc` / tab side that means the start of a bidi paragraph (§5.4)."""
    return "left" if BIDI_LEFT_IS_START else "right"
