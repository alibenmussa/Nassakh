"""The small OOXML writer (D53, PHASE6_SPEC §5.3): elements in schema order, units, fields, and the
package (parts, relationships, content types, a deterministic zip).

`w("pPr", w("pStyle", val="Normal"), w("jc", val="both"))` builds `<w:pPr>` with its children **sorted
into the order the ECMA-376 type requires** (`ORDER`; Word rejects a child out of sequence as
"unreadable content", spike lesson 2), so a caller never has to remember the sequence and nothing is
inserted into existing XML later. Elements outside `ORDER` keep the order they are given (`w:p`, `w:r`,
`w:body`).

`Package` collects the parts with their relationships and writes the zip: entries dated 1980-01-01 in a
fixed order with `[Content_Types].xml` first, so the same inputs give the same bytes (golden tests). A
picture part (the cover, D80) is typed by its extension (`<Default Extension="png">`), as Word writes it.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from xml.sax.saxutils import escape

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XML_NS = "http://www.w3.org/XML/1998/namespace"
NSMAP: dict[str, str] = {"w": W_NS, "r": R_NS}
# DrawingML (the cover's anchored picture, D80)
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"

# Relationship types
REL_DOCUMENT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
REL_CORE = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
REL_APP = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties"
REL_CUSTOM = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties"
REL_STYLES = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
REL_SETTINGS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings"
REL_FONT_TABLE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/fontTable"
REL_FOOTNOTES = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
REL_COMMENTS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
REL_HEADER = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header"
REL_FOOTER = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer"
REL_FONT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/font"
REL_IMAGE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"

# Content types
CT_DOCUMENT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
CT_STYLES = "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
CT_SETTINGS = "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"
CT_FONT_TABLE = "application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml"
CT_FOOTNOTES = "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
CT_COMMENTS = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
CT_HEADER = "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
CT_FOOTER = "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"
CT_CORE = "application/vnd.openxmlformats-package.core-properties+xml"
CT_APP = "application/vnd.openxmlformats-officedocument.extended-properties+xml"
CT_CUSTOM = "application/vnd.openxmlformats-officedocument.custom-properties+xml"
CT_RELS = "application/vnd.openxmlformats-package.relationships+xml"
CT_OBFUSCATED_FONT = "application/vnd.openxmlformats-officedocument.obfuscatedFont"
MEDIA_TYPE_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# ====================================================================== schema order

# The child sequences of the ECMA-376 complex types whose order Word checks (WordprocessingML, wml.xsd);
# `test_word` checks these lists against the vendored schema.
ORDER: dict[str, tuple[str, ...]] = {
    "pPr": (
        "pStyle keepNext keepLines pageBreakBefore framePr widowControl numPr suppressLineNumbers pBdr shd"
        " tabs suppressAutoHyphens kinsoku wordWrap overflowPunct topLinePunct autoSpaceDE autoSpaceDN bidi"
        " adjustRightInd snapToGrid spacing ind contextualSpacing mirrorIndents suppressOverlap jc"
        " textDirection textAlignment textboxTightWrap outlineLvl divId cnfStyle rPr sectPr pPrChange"
    ),
    "rPr": (
        "rStyle rFonts b bCs i iCs caps smallCaps strike dstrike outline shadow emboss imprint noProof"
        " snapToGrid vanish webHidden color spacing w kern position sz szCs highlight u effect bdr shd"
        " fitText vertAlign rtl cs em lang eastAsianLayout specVanish oMath rPrChange"
    ),
    "sectPr": (
        "headerReference footerReference footnotePr endnotePr type pgSz pgMar paperSrc pgBorders lnNumType"
        " pgNumType cols formProt vAlign noEndnote titlePg textDirection bidi rtlGutter docGrid"
        " printerSettings sectPrChange"
    ),
    "footnotePr": "pos numFmt numStart numRestart footnote",
    "style": (
        "name aliases basedOn next link autoRedefine hidden uiPriority semiHidden unhideWhenUsed qFormat"
        " locked personal personalCompose personalReply rsid pPr rPr tblPr trPr tcPr tblStylePr"
    ),
    "styles": "docDefaults latentStyles style",
    "docDefaults": "rPrDefault pPrDefault",
    "font": (
        "altName panose1 charset family notTrueType pitch sig embedRegular embedBold embedItalic"
        " embedBoldItalic"
    ),
    "pBdr": "top left bottom right between bar",
    "compat": (
        "useSingleBorderforContiguousCells wpJustification noTabHangInd noLeading spaceForUL"
        " noColumnBalance balanceSingleByteDoubleByteWidth noExtraLineSpacing doNotLeaveBackslashAlone"
        " ulTrailSpace doNotExpandShiftReturn spacingInWholePoints lineWrapLikeWord6"
        " printBodyTextBeforeHeader printColBlack wpSpaceWidth showBreaksInFrames subFontBySize"
        " suppressBottomSpacing suppressTopSpacing suppressSpacingAtTopOfPage suppressTopSpacingWP"
        " suppressSpBfAfterPgBrk swapBordersFacingPages convMailMergeEsc truncateFontHeightsLikeWP6"
        " mwSmallCaps usePrinterMetrics doNotSuppressParagraphBorders wrapTrailSpaces"
        " footnoteLayoutLikeWW8 shapeLayoutLikeWW8 alignTablesRowByRow forgetLastTabAlignment"
        " adjustLineHeightInTable autoSpaceLikeWord95 noSpaceRaiseLower doNotUseHTMLParagraphAutoSpacing"
        " layoutRawTableWidth layoutTableRowsApart useWord97LineBreakRules doNotBreakWrappedTables"
        " doNotSnapToGridInCell selectFldWithFirstOrLastChar applyBreakingRules doNotWrapTextWithPunct"
        " doNotUseEastAsianBreakRules useWord2002TableStyleRules growAutofit useFELayout"
        " useNormalStyleForList doNotUseIndentAsNumberingTabStop useAltKinsokuLineBreakRules"
        " allowSpaceOfSameStyleInTable doNotSuppressIndentation doNotAutofitConstrainedTables"
        " autofitToFirstFixedWidthCell underlineTabInNumList displayHangulFixedWidth"
        " splitPgBreakAndParaMark doNotVertAlignCellWithSp doNotBreakConstrainedForcedTable"
        " doNotVertAlignInTxbx useAnsiKerningPairs cachedColBalance compatSetting"
    ),
    "settings": (
        "writeProtection view zoom removePersonalInformation removeDateAndTime doNotDisplayPageBoundaries"
        " displayBackgroundShape printPostScriptOverText printFractionalCharacterWidth printFormsData"
        " embedTrueTypeFonts embedSystemFonts saveSubsetFonts saveFormsData mirrorMargins"
        " alignBordersAndEdges bordersDoNotSurroundHeader bordersDoNotSurroundFooter gutterAtTop"
        " hideSpellingErrors hideGrammaticalErrors activeWritingStyle proofState formsDesign attachedTemplate"
        " linkStyles stylePaneFormatFilter stylePaneSortMethod documentType mailMerge revisionView"
        " trackRevisions doNotTrackMoves doNotTrackFormatting documentProtection autoFormatOverride"
        " styleLockTheme styleLockQFSet defaultTabStop autoHyphenation consecutiveHyphenLimit hyphenationZone"
        " doNotHyphenateCaps showEnvelope summaryLength clickAndTypeStyle defaultTableStyle evenAndOddHeaders"
        " bookFoldRevPrinting bookFoldPrinting bookFoldPrintingSheets drawingGridHorizontalSpacing"
        " drawingGridVerticalSpacing displayHorizontalDrawingGridEvery displayVerticalDrawingGridEvery"
        " doNotUseMarginsForDrawingGridOrigin drawingGridHorizontalOrigin drawingGridVerticalOrigin"
        " doNotShadeFormData noPunctuationKerning characterSpacingControl printTwoOnOne"
        " strictFirstAndLastChars noLineBreaksAfter noLineBreaksBefore savePreviewPicture"
        " doNotValidateAgainstSchema saveInvalidXml ignoreMixedContent alwaysShowPlaceholderText"
        " doNotDemarcateInvalidXml saveXmlDataOnly useXSLTWhenSaving saveThroughXslt showXMLTags"
        " alwaysMergeEmptyNamespace updateFields hdrShapeDefaults footnotePr endnotePr compat docVars rsids"
        " mathPr attachedSchema themeFontLang clrSchemeMapping doNotIncludeSubdocsInStats"
        " doNotAutoCompressPictures forceUpgrade captions readModeInkLockDown smartTagType schemaLibrary"
        " shapeDefaults doNotEmbedSmartTags decimalSymbol listSeparator"
    ),
}
ORDER = {parent: tuple(sequence.split()) for parent, sequence in ORDER.items()}
_RANK: dict[str, dict[str, int]] = {
    parent: {name: index for index, name in enumerate(sequence)} for parent, sequence in ORDER.items()
}

# ====================================================================== elements


def _qname(name: str) -> str:
    """`val` → `{w}val`; `r_id` → `{r}id`; `xml_space` → `{xml}space`."""
    if name.startswith("r_"):
        return f"{{{R_NS}}}{name[2:]}"
    if name.startswith("xml_"):
        return f"{{{XML_NS}}}{name[4:]}"
    return f"{{{W_NS}}}{name}"


def _value(value) -> str:
    if value is True:
        return "1"
    if value is False:
        return "0"
    return str(value)


def local(node) -> str:
    """The local name of an element."""
    return etree.QName(node).localname


def w(tag: str, *children, **attrs) -> etree._Element:
    """`<w:tag>` with `w:` attributes (`val="x"`; `r_id="rId1"` for `r:id`; `xml_space="preserve"`) and
    children (None is skipped; a str child becomes the element's text). Children of a type in `ORDER`
    are sorted into schema order (stable, so repeated elements keep their order)."""
    node = etree.Element(f"{{{W_NS}}}{tag}", nsmap=NSMAP)
    for name, value in attrs.items():
        if value is not None:
            node.set(_qname(name), _value(value))
    items = [child for child in children if child is not None]
    rank = _RANK.get(tag)
    if rank is not None:
        items.sort(key=lambda child: rank.get(local(child), len(rank)) if not isinstance(child, str) else -1)
    for child in items:
        if isinstance(child, str):
            node.text = (node.text or "") + child
        else:
            node.append(child)
    return node


def text(value: str, space: bool = True) -> etree._Element:
    """`<w:t xml:space="preserve">value</w:t>`."""
    return w("t", value, xml_space="preserve" if space else None)


def root(tag: str, *children, nsmap: dict | None = None) -> etree._Element:
    """A part's root element (`w:document`, `w:styles`…) declaring the `w` and `r` namespaces."""
    node = etree.Element(f"{{{W_NS}}}{tag}", nsmap=nsmap or NSMAP)
    for child in children:
        if child is not None:
            node.append(child)
    return node


def serialize(node: etree._Element) -> bytes:
    """The part's bytes: an XML declaration, UTF-8, standalone."""
    return etree.tostring(node, xml_declaration=True, encoding="UTF-8", standalone=True)


# ====================================================================== units

TWIPS_PER_PT = 20
MM_PER_PT = 25.4 / 72


def twips(points: float) -> int:
    """Points → twips (twentieths of a point), rounded."""
    return round(points * TWIPS_PER_PT)


def twips_mm(millimetres: float) -> int:
    """Millimetres → twips."""
    return round(millimetres / MM_PER_PT * TWIPS_PER_PT)


def half_points(points: float) -> int:
    """Points → half-points (`w:sz`), rounded."""
    return round(points * 2)


def mm_to_pt(millimetres: float) -> float:
    return millimetres / MM_PER_PT


def pt_to_mm(points: float) -> float:
    return points * MM_PER_PT


EMU_PER_MM = 36_000  # DrawingML's English Metric Units


def emu_mm(millimetres: float) -> int:
    """Millimetres → EMU, rounded."""
    return round(millimetres * EMU_PER_MM)


# ====================================================================== text safety

# What XML 1.0 forbids: C0 controls but tab / newline / return, the surrogates, U+FFFE and U+FFFF.
_FORBIDDEN = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def xml_safe(value: str) -> str:
    """`value` without the characters XML 1.0 forbids (dropped)."""
    return _FORBIDDEN.sub("", value)


def guid(value) -> str:
    """A UUID as OOXML writes GUIDs: `{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}`."""
    return "{" + str(value).upper() + "}"


def iso_datetime(moment: datetime) -> str:
    """A W3CDTF / xsd:dateTime in UTC: `2026-09-26T10:02:11Z` (a naive datetime is taken as UTC)."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ====================================================================== fields


def run(*content, rpr: etree._Element | None = None) -> etree._Element:
    """`<w:r>` with optional run properties and its content elements."""
    node = w("r")
    if rpr is not None and len(rpr):
        node.append(rpr)
    for item in content:
        if item is not None:
            node.append(item)
    return node


def fld_char(kind: str, rpr: etree._Element | None = None) -> etree._Element:
    return run(w("fldChar", fldCharType=kind), rpr=rpr)


def instr_text(code: str, rpr: etree._Element | None = None) -> etree._Element:
    return run(w("instrText", f" {code.strip()} ", xml_space="preserve"), rpr=rpr)


def field_runs(code: str, result: Iterable[etree._Element], rpr_factory=None) -> list[etree._Element]:
    """A complex field: begin, ` code `, separate, the cached `result` runs, end. `rpr_factory()` gives a
    fresh `w:rPr` for the field's own runs (each run needs its own element)."""

    def rpr():
        return rpr_factory() if rpr_factory is not None else None

    return [
        fld_char("begin", rpr()),
        instr_text(code, rpr()),
        fld_char("separate", rpr()),
        *result,
        fld_char("end", rpr()),
    ]


# ====================================================================== the package


@dataclass(frozen=True)
class Rel:
    """A relationship from a part (or from the package): `rId`, its type and its target."""

    id: str
    type: str
    target: str
    external: bool = False


@dataclass
class Part:
    """One part of the package: its zip name, content type, bytes and outgoing relationships."""

    name: str
    content_type: str
    data: bytes
    rels: list[Rel] = field(default_factory=list)

    @property
    def rels_name(self) -> str:
        folder, _slash, base = self.name.rpartition("/")
        return f"{folder}/_rels/{base}.rels" if folder else f"_rels/{base}.rels"


def rels_xml(rels: Iterable[Rel]) -> bytes:
    """A `.rels` part."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
    ]
    for rel in rels:
        mode = ' TargetMode="External"' if rel.external else ""
        lines.append(
            f'<Relationship Id="{escape(rel.id, {chr(34): "&quot;"})}" Type="{escape(rel.type)}"'
            f' Target="{escape(rel.target, {chr(34): "&quot;"})}"{mode}/>'
        )
    lines.append("</Relationships>")
    return "".join(lines).encode("utf-8")


class Package:
    """The parts of a .docx and its package relationships, written as a deterministic zip."""

    def __init__(self) -> None:
        self.parts: list[Part] = []
        self.rels: list[Rel] = []

    def add(self, part: Part) -> Part:
        self.parts.append(part)
        return part

    def relate(self, type_: str, target: str) -> str:
        """A package relationship (`_rels/.rels`) to `target`; returns its id."""
        rel_id = f"rId{len(self.rels) + 1}"
        self.rels.append(Rel(rel_id, type_, target))
        return rel_id

    def content_types(self) -> bytes:
        defaults = {"rels": CT_RELS, "xml": "application/xml"}
        overrides = []
        for part in self.parts:
            extension = part.name.rpartition(".")[2]
            if part.content_type == CT_OBFUSCATED_FONT or part.content_type.startswith("image/"):
                defaults.setdefault(extension, part.content_type)
                continue
            overrides.append((part.name, part.content_type))
        lines = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        ]
        for extension, content_type in defaults.items():
            lines.append(f'<Default Extension="{extension}" ContentType="{content_type}"/>')
        for name, content_type in overrides:
            lines.append(f'<Override PartName="/{escape(name)}" ContentType="{content_type}"/>')
        lines.append("</Types>")
        return "".join(lines).encode("utf-8")

    def entries(self) -> list[tuple[str, bytes]]:
        """`(zip name, bytes)` in the order written."""
        out: list[tuple[str, bytes]] = [("[Content_Types].xml", self.content_types())]
        out.append(("_rels/.rels", rels_xml(self.rels)))
        for part in self.parts:
            out.append((part.name, part.data))
            if part.rels:
                out.append((part.rels_name, rels_xml(part.rels)))
        return out

    def to_bytes(self) -> bytes:
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in self.entries():
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, data)
        return buffer.getvalue()


# ====================================================================== docProps

_CORE_NS = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "dcmitype": "http://purl.org/dc/dcmitype/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}
_APP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
_CUSTOM_NS = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
_VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
_CUSTOM_FMTID = "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"


def core_xml(
    *,
    title: str,
    subject: str,
    creator: str,
    description: str,
    modified_by: str,
    created: datetime,
    language: str = "ar",
) -> bytes:
    """`docProps/core.xml` (§5.13)."""
    cp, dc, dcterms, xsi = (_CORE_NS[k] for k in ("cp", "dc", "dcterms", "xsi"))
    node = etree.Element(f"{{{cp}}}coreProperties", nsmap=_CORE_NS)
    stamp = iso_datetime(created)

    def add(tag: str, value: str, ns: str = dc, **attrs) -> None:
        if value == "" and tag not in ("title",):
            return
        child = etree.SubElement(node, f"{{{ns}}}{tag}")
        child.text = xml_safe(value)
        for key, item in attrs.items():
            child.set(key, item)

    add("title", title)
    add("subject", subject)
    add("creator", creator)
    add("description", description)
    add("lastModifiedBy", modified_by, cp)
    add("created", stamp, dcterms, **{f"{{{xsi}}}type": "dcterms:W3CDTF"})
    add("modified", stamp, dcterms, **{f"{{{xsi}}}type": "dcterms:W3CDTF"})
    add("language", language)
    return serialize(node)


def app_xml(*, application: str = "Nassakh", company: str = "") -> bytes:
    """`docProps/app.xml`."""
    node = etree.Element(f"{{{_APP_NS}}}Properties", nsmap={None: _APP_NS, "vt": _VT_NS})
    etree.SubElement(node, f"{{{_APP_NS}}}Application").text = application
    if company:
        etree.SubElement(node, f"{{{_APP_NS}}}Company").text = xml_safe(company)
    return serialize(node)


def custom_xml(properties: Iterable[tuple[str, str]]) -> bytes:
    """`docProps/custom.xml`: string properties in the order given."""
    node = etree.Element(f"{{{_CUSTOM_NS}}}Properties", nsmap={None: _CUSTOM_NS, "vt": _VT_NS})
    for pid, (name, value) in enumerate(properties, start=2):
        prop = etree.SubElement(node, f"{{{_CUSTOM_NS}}}property")
        prop.set("fmtid", _CUSTOM_FMTID)
        prop.set("pid", str(pid))
        prop.set("name", name)
        etree.SubElement(prop, f"{{{_VT_NS}}}lpwstr").text = xml_safe(str(value))
    return serialize(node)


# ====================================================================== settings


def settings_xml(
    *,
    embed_fonts: bool,
    even_and_odd: bool,
    footnote_numbering: str,
    number_format: str,
    compat_mode: int,
) -> etree._Element:
    """`word/settings.xml` (§5.3): print view, zoom, mirrored margins, spelling hidden, the footnote
    separators, compatibility mode 15 and the bidi language. Never `updateFields`."""
    compat = w(
        "compat",
        w("doNotExpandShiftReturn"),
        w(
            "compatSetting",
            name="compatibilityMode",
            uri="http://schemas.microsoft.com/office/word",
            val=compat_mode,
        ),
        w(
            "compatSetting",
            name="overrideTableStyleFontSizeAndJustification",
            uri="http://schemas.microsoft.com/office/word",
            val=1,
        ),
        w(
            "compatSetting",
            name="enableOpenTypeFeatures",
            uri="http://schemas.microsoft.com/office/word",
            val=1,
        ),
        w(
            "compatSetting",
            name="doNotFlipMirrorIndents",
            uri="http://schemas.microsoft.com/office/word",
            val=1,
        ),
    )
    return root(
        "settings",
        w("view", val="print"),
        w("zoom", percent=100),
        w("embedTrueTypeFonts") if embed_fonts else None,
        w("mirrorMargins"),
        w("hideSpellingErrors"),
        w("hideGrammaticalErrors"),
        w("defaultTabStop", val=720),
        w("evenAndOddHeaders") if even_and_odd else None,
        w("characterSpacingControl", val="doNotCompress"),
        w(
            "footnotePr",
            w("numFmt", val=number_format),
            w("numRestart", val=footnote_numbering),
            w("footnote", id=-1),
            w("footnote", id=0),
        ),
        compat,
        w("themeFontLang", val="en-US", bidi="ar-SA"),
        w("decimalSymbol", val="."),
        w("listSeparator", val=","),
    )
