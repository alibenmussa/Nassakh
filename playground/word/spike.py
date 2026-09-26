"""Word export spike (Phase 6): can a real book become a professional .docx? Not the renderer.

Builds a .docx of a book from the same book model the preview uses (`publishing.model.book_model`),
testing the parts Word export depends on: right-to-left sections and paragraphs with the Arabic face in
Word's complex-script slot, the page setup (trim, mirrored margins), real Word styles, real footnotes
numbered per page, kashida justification, a section per chapter opening on a right-hand page, page
numbers and a contents field. Then Microsoft Word itself (AppleScript) opens it and saves it as PDF,
so its pages can be compared with the preview's.

    .venv/bin/python playground/word/spike.py 19            # out/book-19.docx
    .venv/bin/python playground/word/spike.py 19 --word     # ... and Word's PDF of it
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")

import django  # noqa: E402

django.setup()

from docx import Document  # noqa: E402
from docx.enum.section import WD_SECTION  # noqa: E402
from docx.opc.constants import RELATIONSHIP_TYPE as RT  # noqa: E402
from docx.opc.packuri import PackURI  # noqa: E402
from docx.opc.part import Part  # noqa: E402
from docx.oxml import parse_xml  # noqa: E402
from docx.oxml.ns import nsdecls, qn  # noqa: E402
from docx.shared import Mm, Pt  # noqa: E402

from books.models import Book  # noqa: E402
from publishing import fonts as F  # noqa: E402
from publishing.model import LineBreak, NoteRef, Run, SourceMark, book_model  # noqa: E402
from publishing.preview import job_for  # noqa: E402

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
TW = 20  # twips a point


def el(tag: str, **attrs) -> object:
    """An OXML element `w:tag` with `w:` attributes."""
    node = parse_xml(f"<w:{tag} {nsdecls('w')}/>")
    for key, value in attrs.items():
        node.set(qn(f"w:{key}"), str(value))
    return node


# Word checks the order of the children of these elements (ECMA-376 sequences); others append
ORDER = {
    "pPr": "pStyle keepNext keepLines pageBreakBefore framePr widowControl numPr suppressLineNumbers pBdr shd"
    " tabs suppressAutoHyphens kinsoku wordWrap overflowPunct topLinePunct autoSpaceDE autoSpaceDN bidi"
    " adjustRightInd snapToGrid spacing ind contextualSpacing mirrorIndents suppressOverlap jc textDirection"
    " textAlignment textboxTightWrap outlineLvl divId cnfStyle rPr sectPr pPrChange",
    "rPr": "rStyle rFonts b bCs i iCs caps smallCaps strike dstrike outline shadow emboss imprint noProof"
    " snapToGrid vanish webHidden color spacing w kern position sz szCs highlight u effect bdr shd fitText"
    " vertAlign rtl cs em lang eastAsianLayout specVanish oMath",
    "sectPr": "headerReference footerReference footnotePr endnotePr type pgSz pgMar paperSrc pgBorders"
    " lnNumType pgNumType cols formProt vAlign noEndnote titlePg textDirection bidi rtlGutter docGrid"
    " printerSettings sectPrChange",
    "footnotePr": "footnote pos numFmt numStart numRestart",
    "style": "name aliases basedOn next link autoRedefine hidden uiPriority semiHidden unhideWhenUsed qFormat"
    " locked personal personalCompose personalReply rsid pPr rPr tblPr trPr tcPr tblStylePr",
    "settings": "writeProtection view zoom removePersonalInformation removeDateAndTime"
    " doNotDisplayPageBoundaries displayBackgroundShape printPostScriptOverText printFractionalCharacterWidth"
    " printFormsData embedTrueTypeFonts embedSystemFonts saveSubsetFonts saveFormsData mirrorMargins"
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
    " shapeDefaults doNotEmbedSmartTags decimalSymbol listSeparator",
}
ORDER = {parent: sequence.split() for parent, sequence in ORDER.items()}


def _local(node) -> str:
    return node.tag.rsplit("}", 1)[-1]


def child(parent, tag: str, **attrs):
    """`parent`'s `w:tag` child, made (with `attrs`) when missing, in the order Word requires."""
    found = parent.find(qn(f"w:{tag}"))
    if found is None:
        found = el(tag, **attrs)
        order = ORDER.get(_local(parent))
        if order and tag in order:
            rank = order.index(tag)
            later = next((c for c in parent if _local(c) in order and order.index(_local(c)) > rank), None)
            if later is not None:
                later.addprevious(found)
                return found
        parent.append(found)
    else:
        for key, value in attrs.items():
            found.set(qn(f"w:{key}"), str(value))
    return found


# ---------------------------------------------------------------- styles


def run_fonts(rpr, arabic: str, latin: str, size_pt: float, bold: bool = False) -> None:
    """The run properties Word uses for Arabic: the complex-script slot (cs), its size and bold."""
    child(rpr, "rFonts", ascii=latin, hAnsi=latin, cs=arabic, eastAsia=latin)
    if bold:
        child(rpr, "b")
        child(rpr, "bCs")
    child(rpr, "sz", val=round(size_pt * 2))
    child(rpr, "szCs", val=round(size_pt * 2))
    child(rpr, "lang", val="en-US", bidi="ar-SA")


def para_props(ppr, *, justify: str, line: float, indent_pt: float = 0, before_pt: float = 0,
               after_pt: float = 0, keep_next: bool = False, widows: bool = True, size_pt: float = 0,
               rule: str = "exact") -> None:
    """`line`: a multiple of `size_pt` set as an exact pitch (CSS `line-height`), or of Word's own line
    height when `size_pt` is 0 (Word's "multiple")."""
    child(ppr, "bidi")
    if keep_next:
        child(ppr, "keepNext")
    if widows:
        child(ppr, "widowControl")
    if size_pt:
        child(ppr, "spacing", before=round(before_pt * TW), after=round(after_pt * TW),
              line=round(line * size_pt * TW), lineRule=rule)
    else:
        child(ppr, "spacing", before=round(before_pt * TW), after=round(after_pt * TW),
              line=round(240 * line), lineRule="auto")
    if indent_pt:
        child(ppr, "ind", firstLine=round(indent_pt * TW))
    child(ppr, "jc", val=justify)


def define_styles(document, setup, faces, kashida: str) -> None:
    body, latin, heading = faces
    styles = document.styles.element
    size = setup.body_size_pt

    def style(style_id: str, name: str, kind: str = "paragraph", based: str | None = None):
        node = styles.find(f"{{{W}}}style[@{{{W}}}styleId='{style_id}']")
        if node is None:
            node = el("style", type=kind, styleId=style_id)
            node.append(el("name", val=name))
            styles.append(node)
        if based:
            child(node, "basedOn", val=based)
        child(node, "qFormat")
        return node

    normal = style("Normal", "Normal")
    para_props(child(normal, "pPr"), justify=kashida, line=setup.line_height, indent_pt=setup.indent_em * size,
               size_pt=size, rule=os.environ.get("RULE", "exact"))
    run_fonts(child(normal, "rPr"), body, latin, size)
    mm = 72 / 25.4
    for level, scale, line, before, after in ((1, setup.h1_scale, 1.35, 16, 9), (2, setup.h2_scale, 1.4, 5, 3)):
        h = style(f"Heading{level}", f"heading {level}", based="Normal")
        ppr = child(h, "pPr")
        para_props(ppr, justify="center", line=line, before_pt=before * mm, after_pt=after * mm,
                   keep_next=True, size_pt=size * scale)
        child(ppr, "ind", firstLine=0)
        child(ppr, "outlineLvl", val=level - 1)
        run_fonts(child(h, "rPr"), heading, latin, size * scale, bold=True)
    note = style("FootnoteText", "footnote text", based="Normal")
    para_props(child(note, "pPr"), justify=kashida, line=1.5, size_pt=setup.footnote_size_pt)
    child(child(note, "pPr"), "ind", firstLine=0)
    run_fonts(child(note, "rPr"), body, latin, setup.footnote_size_pt)
    ref = style("FootnoteReference", "footnote reference", kind="character")
    child(child(ref, "rPr"), "vertAlign", val="superscript")
    quote = style("Quote", "Quote", based="Normal")
    ind = child(child(quote, "pPr"), "ind")
    ind.set(qn("w:left"), str(round(size * 2 * TW)))
    ind.set(qn("w:right"), str(round(size * 2 * TW)))
    for style_id, name in (("Title", "Title"), ("Subtitle", "Subtitle")):
        s = style(style_id, name, based="Normal")
        para_props(child(s, "pPr"), justify="center", line=1.4, after_pt=size, size_pt=size * 2)
        run_fonts(child(s, "rPr"), heading, latin, size * (2.2 if style_id == "Title" else 1.4),
                  bold=style_id == "Title")


# ---------------------------------------------------------------- footnotes part


class Notes:
    """The footnotes part (python-docx has none): separators, then one `w:footnote` per note."""

    def __init__(self, document):
        # the rule above the notes as the preview draws it: 4 mm above, 1.6 mm below, a thin line;
        # an exact pitch, not one line of the body face (Simplified Arabic's is about 25 pt at 13 pt)
        gap = os.environ.get("SEP", "exact")
        pitch = round((4 + 1.6) * 72 / 25.4 * TW) if gap == "exact" else 240
        rule = "exact" if gap == "exact" else "auto"
        sep = (f'<w:pPr><w:bidi/><w:spacing w:before="0" w:after="0" w:line="{pitch}" w:lineRule="{rule}"/>'
               '<w:rPr><w:sz w:val="2"/><w:szCs w:val="2"/></w:rPr></w:pPr>')
        xml = (
            f'<w:footnotes {nsdecls("w")}>'
            f'<w:footnote w:type="separator" w:id="-1"><w:p>{sep}<w:r><w:separator/></w:r></w:p></w:footnote>'
            f'<w:footnote w:type="continuationSeparator" w:id="0"><w:p>{sep}<w:r><w:continuationSeparator/>'
            "</w:r></w:p></w:footnote></w:footnotes>"
        )
        self.root = parse_xml(xml)
        self.part = Part(
            PackURI("/word/footnotes.xml"),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
            b"",
            document.part.package,
        )
        document.part.relate_to(self.part, RT.FOOTNOTES)
        self.next = 1

    def add(self, runs) -> int:
        number = self.next
        self.next += 1
        note = el("footnote", id=number)
        p = el("p")
        ppr = child(p, "pPr")
        child(ppr, "pStyle", val="FootnoteText")
        ref = el("r")
        child(child(ref, "rPr"), "rStyle", val="FootnoteReference")
        ref.append(el("footnoteRef"))
        p.append(text_run("("))
        p.append(ref)
        p.append(text_run(")\u00a0"))
        for run in runs:
            if isinstance(run, Run):
                p.append(text_run(run.text, run.marks))
        note.append(p)
        self.root.append(note)
        return number

    def finish(self) -> None:
        from lxml import etree

        self.part._blob = etree.tostring(self.root, xml_declaration=True, encoding="UTF-8", standalone=True)


def text_run(text: str, marks=()) -> object:
    r = el("r")
    rpr = child(r, "rPr")
    child(rpr, "rtl")
    if "bold" in marks:
        child(rpr, "b")
        child(rpr, "bCs")
    if "italic" in marks:
        child(rpr, "i")
        child(rpr, "iCs")
    t = el("t")
    t.text = text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    r.append(t)
    return r


def field(paragraph, code: str, shown: str = "") -> None:
    """A Word field (`PAGE`, `TOC \\o "1-2" \\h`) with a cached result `shown`."""
    for kind in ("begin",):
        r = el("r")
        r.append(el("fldChar", fldCharType=kind))
        paragraph._p.append(r)
    r = el("r")
    instr = el("instrText")
    instr.text = f" {code} "
    instr.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    r.append(instr)
    paragraph._p.append(r)
    r = el("r")
    r.append(el("fldChar", fldCharType="separate"))
    paragraph._p.append(r)
    paragraph._p.append(text_run(shown or "1"))
    r = el("r")
    r.append(el("fldChar", fldCharType="end"))
    paragraph._p.append(r)


# ---------------------------------------------------------------- the book


def setup_section(section, setup, *, first: bool = False) -> None:
    section.page_width = Mm(setup.width_mm)
    section.page_height = Mm(setup.height_mm)
    section.top_margin = Mm(setup.top_mm)
    section.bottom_margin = Mm(setup.bottom_mm)
    # with mirrored margins Word's left margin is the inside one, its right margin the outside one
    section.left_margin = Mm(setup.inner_mm)
    section.right_margin = Mm(setup.outer_mm)
    section.header_distance = Mm(10)
    section.footer_distance = Mm(10)
    sect = section._sectPr
    child(sect, "bidi")
    notes = child(sect, "footnotePr")
    child(notes, "numRestart", val="eachPage")
    child(notes, "numFmt", val="decimal")


def build(book_id: int, kashida: str = "lowKashida", only: str | None = None) -> Path:
    book = Book.objects.get(pk=book_id)
    job = job_for(book, "book", None)
    model = book_model(job.document, job.stylesheet, title=book.title, author=book.author)
    setup = model.setup
    resolved = F.resolve(setup.body_font, setup.latin_font, setup.heading_font)
    faces = (resolved.body.family, resolved.latin.family, resolved.heading.family)

    document = Document()
    settings = document.settings.element
    child(settings, "zoom", percent=100)  # python-docx's template leaves out the required percent
    child(settings, "mirrorMargins")
    # no `updateFields`: Word would ask on open «This document contains fields that may refer to other
    # files. Do you want to update the fields?» (and block the script); the contents are refreshed below
    child(settings, "themeFontLang", val="en-US", bidi="ar-SA")
    separators = child(settings, "footnotePr")
    for number in (-1, 0):
        separators.append(el("footnote", id=number))
    define_styles(document, setup, faces, kashida)
    notes = Notes(document)
    first = document.sections[0]
    setup_section(first, setup, first=True)
    # remove the default empty paragraph
    body = document.element.body
    for p in list(body.iterchildren(qn("w:p"))):
        body.remove(p)

    # title page and contents (not for one chapter alone)
    if only is None:
        document.add_paragraph(model.front.title, style="Title")
        if model.front.author:
            document.add_paragraph(model.front.author, style="Subtitle")
        toc = document.add_paragraph()
        toc.paragraph_format.page_break_before = True
        field(toc, 'TOC \\o "1-2" \\h \\z \\u', "المحتويات")

    footer_done = False
    bookmarks: list[str] = []
    chapters = [c for c in model.chapters if only is None or c.id == only]
    for index, chapter in enumerate(chapters):
        if index or only is None:
            kind = WD_SECTION.ODD_PAGE if setup.chapter_opening == "recto" else WD_SECTION.NEW_PAGE
            section = document.add_section(kind)
            setup_section(section, setup)
            # python-docx copies the previous section's properties: a copied «start at 1» restarts every
            # chapter's numbers at 1, and with mirrored margins Word then adds a blank page to keep 1 odd
            for restart in section._sectPr.findall(qn("w:pgNumType")):
                section._sectPr.remove(restart)
            if not footer_done:
                footer = section.footer
                footer.is_linked_to_previous = False
                p = footer.paragraphs[0]
                p.alignment = 1
                child(p._p.get_or_add_pPr(), "bidi")
                field(p, "PAGE")
                pg = child(section._sectPr, "pgNumType", start=1)
                footer_done = True
                del pg
        for block in chapter.blocks:
            style = {"chapter-title": "Heading1", "section-title": "Heading2", "quote": "Quote"}.get(
                block.style, "Normal")
            p = document.add_paragraph(style=style)
            if block.break_before:
                p.paragraph_format.page_break_before = True
            if block.keep_with_next:
                p.paragraph_format.keep_with_next = True
            if block.style == "chapter-title" and block.id:
                bookmarks.append(block.id)
                n = len(bookmarks)
                p._p.append(el("bookmarkStart", id=n, name=f"nk_{block.id}"))
            note_of = {note.id: note for note in block.footnotes}
            for run in block.runs:
                if isinstance(run, Run):
                    p._p.append(text_run(run.text, run.marks))
                elif isinstance(run, NoteRef) and run.note in note_of:
                    number = notes.add(note_of[run.note].runs)
                    r = el("r")
                    child(child(r, "rPr"), "rStyle", val="FootnoteReference")
                    r.append(el("footnoteReference", id=number))
                    p._p.append(r)
                elif isinstance(run, LineBreak):
                    r = el("r")
                    r.append(el("br"))
                    p._p.append(r)
                elif isinstance(run, SourceMark):
                    continue
            if block.style == "chapter-title" and block.id:
                p._p.append(el("bookmarkEnd", id=len(bookmarks)))
    notes.finish()
    core = document.core_properties
    core.title = model.front.title
    core.author = model.front.author or ""
    core.language = "ar"
    out = HERE / "out"
    out.mkdir(exist_ok=True)
    path = out / (f"book-{book_id}.docx" if only is None else f"book-{book_id}-{only}.docx")
    document.save(path)
    print(path, f"{notes.next - 1} footnotes, {len(model.chapters)} chapters, faces {faces}")
    path.with_suffix(".bookmarks").write_text("\n".join(bookmarks))
    return path


def word_pdf(docx: Path) -> Path:
    """Microsoft Word opens `docx` and saves it as PDF (in Word's own container: no access prompt)."""
    box = Path.home() / "Library/Containers/com.microsoft.Word/Data/Documents/nassakh"
    box.mkdir(parents=True, exist_ok=True)
    src = box / docx.name
    src.write_bytes(docx.read_bytes())
    pdf = src.with_suffix(".pdf")
    if pdf.exists():
        pdf.unlink()
    script = f'''
    with timeout of 280 seconds
        set f to POSIX file "{src}" as alias
        tell application "Microsoft Word"
            open f
            repeat 60 times
                if (count of documents) > 0 then exit repeat
                delay 0.5
            end repeat
            set d to active document
            repeat with t in (tables of contents of d)
                update t
            end repeat
            save as d file name "{pdf}" file format format PDF
            close d saving no
        end tell
    end timeout'''
    subprocess.run(["osascript", "-e", script], check=True, timeout=300)
    target = HERE / "out" / pdf.name
    target.write_bytes(pdf.read_bytes())
    return target


def word_pages(docx: Path) -> tuple[int, dict[str, int]]:
    """Microsoft Word lays out `docx`: its page count and the page of each chapter bookmark. (Word
    16.113 answers «doesn't understand the save message» to every save from AppleScript, so its PDF
    cannot be scripted; its own page numbers can.)"""
    box = Path.home() / "Library/Containers/com.microsoft.Word/Data/Documents/nassakh"
    box.mkdir(parents=True, exist_ok=True)
    src = box / docx.name
    src.write_bytes(docx.read_bytes())
    marks = [m for m in docx.with_suffix(".bookmarks").read_text().split() if m]
    names = "{" + ", ".join(f'"nk_{m}"' for m in marks) + "}"
    script = f'''
    with timeout of 280 seconds
        set f to POSIX file "{src}" as alias
        tell application "Microsoft Word"
            close every document saving no
            open f
            repeat 60 times
                if (count of documents) > 0 then exit repeat
                delay 0.5
            end repeat
            set d to active document
            set out to (compute statistics d statistic statistic pages) as string
            repeat with b in {names}
                set r to text object of bookmark (b as string) of d
                set out to out & "," & ((get range information r information type active end page number) as string)
            end repeat
            close d saving no
            return out
        end tell
    end timeout'''
    done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
    if done.returncode:
        raise RuntimeError(done.stderr.strip())
    values = [int(v) for v in done.stdout.strip().split(",")]
    return values[0], dict(zip(marks, values[1:], strict=True))


def word_stats(docx: Path) -> dict:
    """Word (quit first, so no stale copy stays open) lays out `docx`: pages and lines."""
    subprocess.run(["osascript", "-e", 'tell application "Microsoft Word" to quit saving no'],
                   capture_output=True, timeout=60)
    import time

    time.sleep(3)
    box = Path.home() / "Library/Containers/com.microsoft.Word/Data/Documents/nassakh"
    box.mkdir(parents=True, exist_ok=True)
    for lock in box.glob("~$*"):
        lock.unlink()
    src = box / docx.name
    src.write_bytes(docx.read_bytes())
    script = f'''
    with timeout of 280 seconds
        set f to POSIX file "{src}" as alias
        tell application "Microsoft Word"
            open f
            repeat 120 times
                if (count of documents) > 0 then exit repeat
                delay 0.5
            end repeat
            set d to active document
            set a to (compute statistics d statistic statistic pages) as string
            set b to (compute statistics d statistic statistic lines) as string
            set c to (compute statistics d statistic statistic lines include footnotes and endnotes true) as string
            return a & "," & b & "," & c
        end tell
    end timeout'''
    done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
    if done.returncode:
        raise RuntimeError(done.stderr.strip())
    pages, lines, with_notes = (int(v) for v in done.stdout.strip().split(","))
    return {"pages": pages, "lines": lines, "note_lines": with_notes - lines}


if __name__ == "__main__":
    path = build(int(sys.argv[1]), kashida=os.environ.get("KASHIDA", "lowKashida"))
    if "--word" in sys.argv:
        print("Word:", word_stats(path))
