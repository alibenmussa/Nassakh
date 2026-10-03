"""The Word styles of a book (PHASE6_SPEC §5.5): the style table derived from the page setup, the CSS
margin-collapse rule Word needs spelled out, widows, and `styles.xml`.

Every value is the preview's: sizes are the unrounded CSS values written as whole half-points, the line
pitch is always exact (`lineRule="exact"`, spike lesson 1), before / after are the CSS margins in mm.
Built-in styles keep Word's canonical names (Word localizes them); custom styles carry the Arabic names
of the editor's style picker.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from lxml import etree

from publishing.model import PageSetup

from .faces import FacePlan
from .ooxml import half_points, root, twips, twips_mm, w
from .options import BIDI_LEFT_IS_START, CALL_RAISE, SPACING_ADDS

FOOTNOTE_CALL_SCALE = 0.62  # `publishing.css.FOOTNOTE_CALL_SCALE`
COMMENT_SIZE_PT = 10.0
COMMENT_REF_SIZE_PT = 8.0

# The paragraph styles that take the kashida `w:jc` (§5.10)
JUSTIFIED: tuple[str, ...] = ("Normal", "Quote", "FootnoteText")
# What `w:jc` each alignment writes; start-aligned paragraphs carry `start` (a bidi paragraph's start)
_JC = {"center": "center", "start": "start", "left": "left", "right": "right"}


@dataclass(frozen=True)
class ParaStyle:
    """A paragraph style: its Word id and name, size, face role, exact pitch (a multiple of the size),
    CSS margins in mm (`before` may be negative: the Subtitle's −6 mm), alignment and flags."""

    id: str
    name: str
    size: float  # pt
    face: str  # body | heading
    pitch: float
    before: float  # mm, the CSS margin-top (negative allowed)
    after: float  # mm, the CSS margin-bottom
    align: str  # kashida | center | start
    bold: bool = False
    outline: int | None = None
    keep_next: bool = False
    first_line: float = 0.0  # pt
    indent: float = 0.0  # pt, start = end (the quote)
    custom: bool = False
    tab_end: float | None = None  # pt, an end-side tab with dot leaders (the contents)
    ltr: bool = False
    ui_priority: int | None = None
    hidden: bool = False

    @property
    def written_before(self) -> float:
        """The `w:spacing w:before` written (mm): Word has no negative space."""
        return max(self.before, 0.0)

    @property
    def line_twips(self) -> int:
        return twips(self.pitch * self.size)

    @property
    def line_pt(self) -> float:
        return self.pitch * self.size


@dataclass(frozen=True)
class CharStyle:
    id: str
    name: str
    size: float | None  # pt
    face: str | None  # body | latin | number
    position: float = 0.0  # pt, raised
    custom: bool = False
    all_slots: bool = False  # cs = ascii = hAnsi (the Latin face for characters an Arabic face lacks)


def text_width_mm(setup: PageSetup) -> float:
    return setup.width_mm - setup.inner_mm - setup.outer_mm


def style_table(setup: PageSetup) -> dict[str, ParaStyle]:
    """The paragraph styles of a book, by id (§5.5's table)."""
    body = setup.body_size_pt
    note = setup.footnote_size_pt
    lh = setup.line_height
    width_pt = text_width_mm(setup) / 25.4 * 72
    styles = [
        ParaStyle("Normal", "Normal", body, "body", lh, 0, 0, "kashida", first_line=setup.indent_em * body),
        ParaStyle(
            "Heading1",
            "heading 1",
            setup.h1_scale * body,
            "heading",
            1.35,
            16,
            9,
            "center",
            bold=True,
            outline=0,
            keep_next=setup.keep_headings,
            ui_priority=9,
        ),  # fmt: skip
        ParaStyle(
            "Heading2",
            "heading 2",
            setup.h2_scale * body,
            "heading",
            1.4,
            5,
            3,
            "start",  # D99: a subtitle starts on the start side, as `.nk-section-title` (the title: centre)
            bold=True,
            outline=1,
            keep_next=setup.keep_headings,
            ui_priority=9,
        ),  # fmt: skip
        ParaStyle("Quote", "Quote", body, "body", lh, 2, 2, "kashida", indent=2 * body, ui_priority=29),
        ParaStyle("NkVerse", "شعر", body, "body", lh, 2, 2, "center", custom=True),
        ParaStyle("NkCenter", "ملاحظة وسط", body, "body", lh, 2, 2, "center", custom=True),
        ParaStyle("NkSeparator", "فاصل", body, "body", lh, 4, 4, "center", custom=True),
        ParaStyle("FootnoteText", "footnote text", note, "body", 1.5, 0, 0, "kashida", ui_priority=99),
        ParaStyle(
            "Title",
            "Title",
            setup.h1_scale * 1.4 * body,
            "heading",
            1.4,
            0,
            10,
            "center",
            bold=True,
            ui_priority=10,
        ),
        ParaStyle("Subtitle", "Subtitle", 1.15 * body, "body", lh, -6, 10, "center", ui_priority=11),
        ParaStyle("NkAuthor", "المؤلف", 1.3 * body, "body", lh, 0, 0, "center", custom=True),
        ParaStyle("NkCredit", "سطر التحقيق والترجمة", 1.05 * body, "body", lh, 3, 0, "center", custom=True),
        ParaStyle("NkImprint", "سطر النشر", body, "body", lh, 0, 0, "center", custom=True),
        ParaStyle("NkCopyright", "صفحة الحقوق", note, "body", 1.6, 0, 1, "center", custom=True),
        ParaStyle(
            "TOCHeading",
            "TOC Heading",
            setup.h1_scale * body,
            "heading",
            1.4,
            6,
            10,
            "center",
            bold=True,
            ui_priority=39,
        ),
        ParaStyle("TOC1", "toc 1", body, "body", lh, 0, 1.8, "start", tab_end=width_pt, ui_priority=39),
        ParaStyle(
            "TOC2",
            "toc 2",
            0.95 * body,
            "body",
            lh,
            0,
            1.8,
            "start",
            tab_end=width_pt,
            indent=1.5 * 0.95 * body,
            ui_priority=39,
        ),  # fmt: skip
        ParaStyle("Header", "header", max(note - 0.5, 6), "body", 1.5, 0, 0, "center", ui_priority=99),
        ParaStyle("Footer", "footer", note, "number", 1.5, 0, 0, "center", ltr=True, ui_priority=99),
        ParaStyle(
            "CommentText", "annotation text", COMMENT_SIZE_PT, "body", 1.0, 0, 0, "start", ui_priority=99
        ),
    ]
    return {style.id: style for style in styles}


def char_styles(setup: PageSetup) -> dict[str, CharStyle]:
    body = setup.body_size_pt
    note = setup.footnote_size_pt
    # the number face fills every font slot, so a call, a marker or a page number takes it whatever the
    # run's direction (the preview: the body face, or the Latin face when the body face has no digits)
    styles = [
        CharStyle(
            "FootnoteReference",
            "footnote reference",
            FOOTNOTE_CALL_SCALE * body,
            "number",
            position=CALL_RAISE * body,
            all_slots=True,
        ),  # fmt: skip
        CharStyle("NkNoteNumber", "رقم الحاشية", note, "number", custom=True, all_slots=True),
        CharStyle("NkLatin", "حروف لاتينية", None, "latin", custom=True, all_slots=True),
        CharStyle("PageNumber", "page number", note, "number", all_slots=True),
        CharStyle("CommentReference", "annotation reference", COMMENT_REF_SIZE_PT, "body"),
    ]
    return {style.id: style for style in styles}


# ====================================================================== margin collapsing (§5.5)


def collapse(bottom: float, top: float) -> float:
    """The CSS gap between two adjacent vertical margins: the larger of two positives, the smaller of two
    negatives, the sum when the signs are mixed."""
    if bottom >= 0 and top >= 0:
        return max(bottom, top)
    if bottom <= 0 and top <= 0:
        return min(bottom, top)
    return bottom + top


def word_gap(after_prev: float, before_next: float) -> float:
    """The gap Word leaves between two paragraphs from their written spacing (`SPACING_ADDS`, C12)."""
    return after_prev + before_next if SPACING_ADDS else max(after_prev, before_next)


def split_gap(after_prev: float, before_next: float, gap: float) -> tuple[float | None, float | None]:
    """The direct spacing two consecutive paragraphs need (mm): `(prev's after, next's before)`, None
    where the style's value stands. `after_prev` / `before_next` are the values the styles write (never
    negative); `gap` is the CSS gap. Nothing is written when Word's own rule already gives the gap."""
    gap = max(gap, 0.0)  # Word has no negative space
    if abs(word_gap(after_prev, before_next) - gap) < 0.005:
        return None, None
    if SPACING_ADDS:
        if gap >= after_prev:
            return None, gap - after_prev
        return gap, 0.0
    if gap >= after_prev:
        return None, gap  # max(after, gap) = gap
    return gap, 0.0  # the previous paragraph's after shrinks to the gap


def widow_control(setup: PageSetup) -> tuple[bool, bool]:
    """`(on, approximate)`: Word's widow control is on / off and means two lines (§5.5)."""
    if setup.widows == 1 and setup.orphans == 1:
        return False, False
    return True, not (setup.widows == 2 and setup.orphans == 2)


# ====================================================================== styles.xml


def _fonts(faces: FacePlan, role: str, all_slots: bool = False) -> etree._Element:
    latin = faces.family["latin"]
    family = faces.family[role]
    if all_slots:
        return w("rFonts", ascii=family, hAnsi=family, eastAsia=family, cs=family)
    return w("rFonts", ascii=latin, hAnsi=latin, eastAsia=latin, cs=family)


def _para_rpr(style: ParaStyle, faces: FacePlan) -> etree._Element:
    size = half_points(style.size)
    return w(
        "rPr",
        _fonts(faces, style.face if style.face != "number" else "number"),
        w("b") if style.bold else None,
        w("bCs") if style.bold else None,
        w("sz", val=size),
        w("szCs", val=size),
    )


def _para_ppr(style: ParaStyle, jc: str, widows: bool) -> etree._Element:
    align = jc if style.align == "kashida" else _JC[style.align]
    tabs = None
    if style.tab_end is not None:
        end = "right" if BIDI_LEFT_IS_START else "left"
        tabs = w("tabs", w("tab", val=end, leader="dot", pos=twips(style.tab_end)))
    ind = None
    if style.indent:
        ind = w("ind", left=twips(style.indent), right=twips(style.indent), firstLine=0)
        if style.id == "TOC2":  # the start-side indent only (§5.4: left means start)
            start = "left" if BIDI_LEFT_IS_START else "right"
            ind = w("ind", **{start: twips(style.indent), "firstLine": 0})
    elif style.id != "Normal":
        ind = w("ind", firstLine=0)
    else:
        ind = w("ind", firstLine=twips(style.first_line))
    return w(
        "pPr",
        w("keepNext") if style.keep_next else None,
        w("widowControl", val=None if widows else 0) if style.id == "Normal" else None,
        tabs,
        w("bidi", val=0) if style.ltr else None,
        w(
            "spacing",
            before=twips_mm(style.written_before),
            after=twips_mm(style.after),
            line=style.line_twips,
            lineRule="exact",
        ),
        ind,
        w("jc", val=align),
        w("outlineLvl", val=style.outline) if style.outline is not None else None,
    )


def _char_rpr(style: CharStyle, faces: FacePlan) -> etree._Element:
    size = half_points(style.size) if style.size is not None else None
    return w(
        "rPr",
        _fonts(faces, style.face, style.all_slots) if style.face else None,
        w("position", val=half_points(style.position)) if style.position else None,
        w("sz", val=size) if size is not None else None,
        w("szCs", val=size) if size is not None else None,
    )


def styles_xml(setup: PageSetup, faces: FacePlan, jc: str) -> etree._Element:
    """`word/styles.xml`: the document defaults (§5.5), then every paragraph and character style."""
    table = style_table(setup)
    widows, _approximate = widow_control(setup)
    body = half_points(setup.body_size_pt)
    doc_defaults = w(
        "docDefaults",
        w(
            "rPrDefault",
            w(
                "rPr",
                _fonts(faces, "body"),
                w("sz", val=body),
                w("szCs", val=body),
                w("lang", val="en-US", eastAsia="en-US", bidi="ar-SA"),
            ),
        ),
        w("pPrDefault", w("pPr", w("bidi"), w("spacing", after=0))),
    )
    styles = root("styles", doc_defaults)
    normal = table["Normal"]
    styles.append(
        w(
            "style",
            w("name", val=normal.name),
            w("qFormat"),
            _para_ppr(normal, jc, widows),
            _para_rpr(normal, faces),
            type="paragraph",
            default=1,
            styleId="Normal",
        )
    )
    styles.append(
        w(
            "style",
            w("name", val="Default Paragraph Font"),
            w("uiPriority", val=1),
            w("semiHidden"),
            w("unhideWhenUsed"),
            type="character",
            default=1,
            styleId="DefaultParagraphFont",
        )
    )
    for style in table.values():
        if style.id == "Normal":
            continue
        styles.append(
            w(
                "style",
                w("name", val=style.name),
                w("basedOn", val="Normal"),
                w("next", val="Normal")
                if style.id in ("Heading1", "Heading2", "Title", "Subtitle")
                else None,
                w("uiPriority", val=style.ui_priority) if style.ui_priority is not None else None,
                w("qFormat")
                if not style.id.startswith("TOC") and style.id not in ("Header", "Footer", "CommentText")
                else None,
                _para_ppr(style, jc, widows),
                _para_rpr(style, faces),
                type="paragraph",
                styleId=style.id,
                customStyle=1 if style.custom else None,
            )
        )
    for style in char_styles(setup).values():
        styles.append(
            w(
                "style",
                w("name", val=style.name),
                w("basedOn", val="DefaultParagraphFont"),
                w("uiPriority", val=99),
                _char_rpr(style, faces),
                type="character",
                styleId=style.id,
                customStyle=1 if style.custom else None,
            )
        )
    return styles


def with_size(style: ParaStyle, size: float) -> ParaStyle:
    return replace(style, size=size)
