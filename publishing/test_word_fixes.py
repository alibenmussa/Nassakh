"""Regression tests of the Phase 6 review of the Word builder (PHASE6_SPEC §5.7, §5.9, §7): page numbers at
the outer edge in every header and footer (and on chapter openers of either side), one bookmark per
heading, no first-page parts on continuous sections, the page plan only for a contents field and from a
live layout that is current for the chapters that print, and the notes' texts (the fallback face, the
blank verso's header, the contents of a book with only section titles)."""

from __future__ import annotations

import io
import re
import zipfile

from django.conf import settings
from django.test import override_settings

import pytest
from lxml import etree

from publishing import fonts as F
from publishing.exporters import NullProgress, parse_options
from publishing.model import book_model, page_setup
from publishing.tests import LOREM, document, heading, long_book, make_book, para
from publishing.word import schema
from publishing.word.exporter import DOCX_OPTIONS, DocxExporter, live_plan, static_notes
from publishing.word.ooxml import R_NS, W_NS, twips_mm
from publishing.word.options import WordOptions
from publishing.word.styles import text_width_mm
from publishing.word.writer import DocMeta, PagePlan, build_docx

W = f"{{{W_NS}}}"
R_ID = f"{{{R_NS}}}id"


@pytest.fixture(autouse=True)
def _no_system_fonts(tmp_path):
    with override_settings(NASSAKH={**settings.NASSAKH, "FONT_DIRS": [str(tmp_path / "no-fonts")]}):
        yield


def build(source, sheet=None, *, plan=None):
    book = book_model(source, sheet, title="كتابي", author="المؤلف")
    return build_docx(
        book,
        F.resolve("amiri", "amiri", "amiri"),
        WordOptions(),
        plan=plan,
        meta=DocMeta.fixed(),
        embed_fonts=False,
    )


def parts(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def xml(data: bytes, name: str) -> etree._Element:
    return etree.fromstring(parts(data)[name])


def check(data: bytes) -> None:
    assert schema.validate_package(data) == []
    assert schema.integrity_errors(data) == []


def sections(data: bytes) -> list[etree._Element]:
    return list(xml(data, "word/document.xml").iter(f"{W}sectPr"))


def part_of(data: bytes, section: etree._Element, kind: str, type_: str) -> etree._Element | None:
    """The header (`hdr`) or footer (`ftr`) part a section declares for `type_`."""
    rels = etree.fromstring(parts(data)["word/_rels/document.xml.rels"])
    targets = {rel.get("Id"): rel.get("Target") for rel in rels}
    tag = "headerReference" if kind == "hdr" else "footerReference"
    for ref in section.iter(f"{W}{tag}"):
        if ref.get(f"{W}type") == type_:
            return xml(data, "word/" + targets[ref.get(R_ID)])
    return None


# ====================================================================== what a header or footer shows


def _tokens(paragraph: etree._Element) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for run_ in paragraph.iter(f"{W}r"):
        for child in run_:
            name = etree.QName(child).localname
            if name == "fldChar":
                out.append((child.get(f"{W}fldCharType"), ""))
            elif name == "instrText":
                out.append(("instr", child.text or ""))
            elif name == "t":
                out.append(("text", child.text or ""))
            elif name == "tab":
                out.append(("text", "\t"))
    return out


def _evaluate(code: str, page: int) -> str:
    """The result of a field code on page `page`: PAGE, `=MOD(n,2)`, `IF a = b "x" "y"`."""
    code = code.strip()
    if code == "PAGE":
        return str(page)
    found = re.fullmatch(r"=MOD\((\d+),(\d+)\)", code.replace(" ", ""))
    if found:
        return str(int(found.group(1)) % int(found.group(2)))
    found = re.fullmatch(r'IF (\S+) = (\S+) "([^"]*)" "([^"]*)"', code)
    if found:
        return found.group(3) if found.group(1) == found.group(2) else found.group(4)
    raise AssertionError(f"unknown field code {code!r}")


def _tree(tokens: list[tuple[str, str]]) -> list[tuple]:
    """`("text", value)` and `("field", code items, cached result items)` items."""

    def parse(i: int, stop: tuple[str, ...]) -> tuple[list[tuple], int]:
        items: list[tuple] = []
        while i < len(tokens) and tokens[i][0] not in stop:
            kind, value = tokens[i]
            if kind == "begin":
                code, i = parse(i + 1, ("separate", "end"))
                result: list[tuple] = []
                if tokens[i][0] == "separate":
                    result, i = parse(i + 1, ("end",))
                items.append(("field", code, result))
            else:
                items.append(("text", value))
            i += 1
        return items, i

    return parse(0, ())[0]


def _value(items: list[tuple], page: int) -> str:
    out = ""
    for item in items:
        out += item[1] if item[0] == "text" else _evaluate(_value(item[1], page), page)
    return out


def shown(paragraph: etree._Element, page: int) -> str:
    """What a header or footer paragraph prints on page `page` (nested fields evaluated, a tab «\t»)."""
    return _value(_tree(_tokens(paragraph)), page)


def number_edge(part: etree._Element, page: int, width: int) -> str:
    """Where the page number of a header or footer part sits on page `page`: `left`, `right`, `center`
    (the paragraph is left to right, so its sides are physical), or `none`."""
    paragraph = part.find(f"{W}p")
    ppr = paragraph.find(f"{W}pPr")
    style = ppr.find(f"{W}pStyle").get(f"{W}val")
    bidi = ppr.find(f"{W}bidi")
    ltr = style == "Footer" or (bidi is not None and bidi.get(f"{W}val") == "0")
    value = shown(paragraph, page)
    if str(page) not in value:
        return "none"
    assert ltr, "a page number paragraph is left to right (§5.4)"
    before = value[: value.index(str(page))]
    tabs = before.count("\t")
    if tabs == 0:
        jc = ppr.find(f"{W}jc")
        return jc.get(f"{W}val") if jc is not None else "center"  # the Header and Footer styles centre
    stops = sorted((int(t.get(f"{W}pos")), t.get(f"{W}val")) for t in ppr.iter(f"{W}tab"))
    pos, kind = stops[tabs - 1]
    if kind == "right" and abs(pos - width) <= 1:
        return "right"
    return "center" if kind == "center" and abs(pos - width / 2) <= 1 else f"{kind}@{pos}"


COMBOS = [
    (numbers, header, opening)
    for numbers in ("bottom_center", "bottom_outer", "top_outer")
    for header in ("none", "book", "chapter")
    for opening in ("any", "recto")
]


@pytest.mark.parametrize(("numbers", "header", "opening"), COMBOS)
def test_every_page_number_sits_where_the_preview_prints_it(numbers, header, opening):
    """Odd pages (left-hand in an RTL book) have the number at the left, even pages at the right; a
    centred number is centred; a chapter opener prints its number at its own outer edge (finding 1, 4)."""
    source = long_book(chapters=2, paragraphs=2, notes_every=5)
    result = build(source, {"page_number": numbers, "running_header": header, "chapter_opening": opening})
    check(result.data)
    width = twips_mm(text_width_mm(page_setup(None)))
    body = sections(result.data)[2]  # the title page, the contents, then the first chapter
    kind = "hdr" if numbers == "top_outer" else "ftr"
    outer = numbers != "bottom_center"
    odd, even = part_of(result.data, body, kind, "default"), part_of(result.data, body, kind, "even")
    for page in (3, 5, 7):
        assert number_edge(odd, page, width) == ("left" if outer else "center")
    if outer:
        for page in (4, 6, 8):
            assert number_edge(even, page, width) == "right"
    else:
        assert even is None
    first = part_of(result.data, body, kind, "first")
    if header == "none":  # no running header: no opener parts, every page uses the ones above
        assert first is None and body.find(f"{W}titlePg") is None
        return
    assert body.find(f"{W}titlePg") is not None
    pages = (3, 5) if opening == "recto" else (3, 4, 5, 6)  # recto openers are odd pages
    for page in pages:
        expected = "center" if not outer else ("left" if page % 2 else "right")
        assert number_edge(first, page, width) == expected, (page, shown(first.find(f"{W}p"), page))


def test_top_outer_without_a_running_header_puts_the_even_number_at_the_right_edge():
    """The review's case: `[tab, PAGE]` with a centre tab first printed the even pages' number centred."""
    result = build(
        long_book(chapters=2, paragraphs=2), {"page_number": "top_outer", "running_header": "none"}
    )
    even = xml(result.data, "word/header2.xml").find(f"{W}p")
    ppr = even.find(f"{W}pPr")
    assert ppr.find(f"{W}jc").get(f"{W}val") == "right" and ppr.find(f"{W}bidi").get(f"{W}val") == "0"
    assert even.find(f".//{W}tab") is None  # no tab stop for the number to land on
    odd = xml(result.data, "word/header1.xml").find(f"{W}p")
    assert odd.find(f"{W}pPr/{W}jc").get(f"{W}val") == "left" and odd.find(f".//{W}tab") is None


def test_an_opener_on_either_side_gets_its_number_from_a_parity_field():
    """With `chapter_opening` any, the first-page footer holds `IF {=MOD({PAGE},2)} = 1 …` before a right
    tab at the text's end and the even one after it (finding 4)."""
    result = build(
        long_book(chapters=3, paragraphs=2),
        {"page_number": "bottom_outer", "running_header": "book", "chapter_opening": "any"},
    )
    check(result.data)
    body = sections(result.data)[2]
    first = part_of(result.data, body, "ftr", "first").find(f"{W}p")
    codes = "".join(t.text for t in first.iter(f"{W}instrText"))
    assert codes.count("IF") == 2 and codes.count("=MOD(") == 2 and codes.count("PAGE") == 4
    assert shown(first, 9) == "9\t" and shown(first, 10) == "\t10"
    tab = first.find(f"{W}pPr/{W}tabs/{W}tab")
    assert tab.get(f"{W}val") == "right" and first.find(f"{W}pPr/{W}jc").get(f"{W}val") == "left"
    later = [part_of(result.data, s, "ftr", "first") for s in sections(result.data)[3:]]
    assert later == [None, None]  # inherited
    recto = build(
        long_book(chapters=3, paragraphs=2),
        {"page_number": "bottom_outer", "running_header": "book", "chapter_opening": "recto"},
    )
    plain = part_of(recto.data, sections(recto.data)[2], "ftr", "first")
    assert "IF" not in "".join(t.text for t in plain.iter(f"{W}instrText"))  # every opener is odd


# ====================================================================== continuous sections (finding 5)


def test_a_continuous_section_never_sets_title_pg():
    blocks = [para(f"p{i}", LOREM, pages=(i * 40,)) for i in range(1, 4)]  # far-apart scan pages: 3 sections
    for numbers in ("bottom_outer", "top_outer", "none"):
        result = build(
            document(*blocks),
            {"footnote_numbering": "chapter", "running_header": "book", "page_number": numbers},
        )
        check(result.data)
        found = sections(result.data)
        kinds = [s.find(f"{W}type").get(f"{W}val") if s.find(f"{W}type") is not None else None for s in found]
        assert kinds == [None, "nextPage", "continuous", "continuous"]
        assert found[1].find(f"{W}titlePg") is not None  # the text's first page: an opener
        for section in found[2:]:
            assert section.find(f"{W}titlePg") is None
            assert part_of(result.data, section, "hdr", "first") is None


# ====================================================================== bookmarks (finding 3)


def test_headings_with_the_same_id_or_none_each_get_their_own_bookmark():
    same = document(
        heading("h1", "الفصل الأول"), para("p1", LOREM), heading("h1", "الفصل الثاني"), para("p2", LOREM)
    )
    first, second = heading("x", "الفصل الأول"), heading("y", "الفصل الثاني")
    first["attrs"].pop("id")
    second["attrs"].pop("id")
    none = document(first, para("p1", LOREM), second, para("p2", LOREM))
    for source in (same, none):
        result = build(source, {"running_header": "chapter"}, plan=PagePlan("live", 5, {"h1": 3}, {}))
        check(result.data)
        body = xml(result.data, "word/document.xml").find(f"{W}body")
        marks = [b.get(f"{W}name") for b in body.iter(f"{W}bookmarkStart")]
        assert len(marks) == len(set(marks)) == 2
        anchors = [h.get(f"{W}anchor") for h in body.iter(f"{W}hyperlink")]
        assert anchors == marks  # the k-th entry leads to the k-th heading
        refs = [i.text.split()[1] for i in body.iter(f"{W}instrText") if i.text.strip().startswith("PAGEREF")]
        assert refs == marks


def test_duplicate_heading_ids_no_longer_fail_the_export(db):
    source = document(
        heading("h1", "الفصل الأول"), para("p1", LOREM), heading("h1", "الفصل الثاني"), para("p2", LOREM)
    )
    result = DocxExporter().export(job_for(make_book(source)), NullProgress())
    check(result.data)


# ====================================================================== the page plan (findings 6, 7, 10)


def job_for(book, options=None):
    from publishing.exports import read_inputs

    values = parse_options(DOCX_OPTIONS, options or {})
    job, _inputs, _digest = read_inputs(book, "docx", values)
    return job


EMPTY_FRONT = {
    "type": "paragraph",
    "attrs": {"id": "e0", "sourcePages": [1], "sourceLineIds": []},
    "content": [],
}


def test_a_live_layout_is_current_although_a_chapter_prints_nothing(db):
    from publishing import preview
    from publishing.word.exporter import printed_versions

    source = document(
        EMPTY_FRONT,
        heading("h1", "الفصل الأول"),
        para("p1", LOREM),
        heading("h2", "الفصل الثاني"),
        para("p2", LOREM),
    )
    book = make_book(source)
    assert preview.render_preview(book, "book").status == "done"
    job = job_for(book)
    assert set(job.chapter_versions) == {"e0", "h1", "h2"} and set(printed_versions(job)) == {"h1", "h2"}
    progress = NullProgress()
    result = DocxExporter().export(job, progress)
    assert progress.steps == ["prepare", "write", "check"] and result.stats["plan"] == "live"
    assert "layout_first" not in [row["code"] for row in result.warnings]


def test_no_contents_field_no_page_plan(db):
    for sheet in ({"front_matter": {"title_page": True, "contents": False}}, {}):
        source = (
            document(heading("h1", "الفصل الأول"), para("p1", LOREM))
            if sheet
            else document(para("p1", LOREM))
        )
        book = make_book(source, **sheet)
        progress = NullProgress()
        result = DocxExporter().export(job_for(book), progress)
        assert progress.steps == ["prepare", "write", "check"]  # no layout render
        assert result.stats["plan"] is None and "TOC" not in parts(result.data)["word/document.xml"].decode()
        codes = [row["code"] for row in result.warnings]
        assert (
            "layout_first" not in codes and "toc_update" not in codes and "toc_numbers_missing" not in codes
        )


def test_the_live_plan_reads_the_live_row_once(db, monkeypatch):
    from publishing import preview, relayout

    book = make_book(document(heading("h1", "الفصل الأول"), para("p1", LOREM)))
    assert preview.render_preview(book, "book").status == "done"
    calls = []
    real = relayout.live_of
    monkeypatch.setattr(relayout, "live_of", lambda book_id: calls.append(book_id) or real(book_id))
    plan = live_plan(job_for(book))
    assert plan is not None and plan.source == "live" and calls == [book.pk]


# ====================================================================== notes (findings 8, 9)


def missing_messages(fonts: F.ResolvedFonts) -> list[str]:
    rows = static_notes(page_setup(None), fonts, has_contents=False, layout_current=True)
    return [row["message"] for row in rows if row["code"] == "font_missing"]


def test_the_font_note_names_the_face_that_stands_in(tmp_path):
    folder = tmp_path / "fonts"
    folder.mkdir()
    (folder / "Times New Roman.ttf").write_bytes(F.locate("amiri").regular.read_bytes())  # Times stands in
    with override_settings(NASSAKH={**settings.NASSAKH, "FONT_DIRS": [str(folder)]}):
        # a missing Latin face falls back to Times New Roman first
        assert missing_messages(F.resolve("amiri", "simplified_arabic", "amiri")) == [
            "الخط «Simplified Arabic» غير مثبّت على هذا الجهاز؛ يُستعمل «Times New Roman» بدلًا منه، "
            "كما في المعاينة."
        ]
    # nothing installed: Amiri stands in (for the Latin face too, after Times)
    assert missing_messages(F.resolve("traditional_arabic", "times", "amiri")) == [
        "الخط «Traditional Arabic» غير مثبّت على هذا الجهاز؛ يُستعمل أميري بدلًا منه، كما في المعاينة.",
        "الخط «Times New Roman» غير مثبّت على هذا الجهاز؛ يُستعمل أميري بدلًا منه، كما في المعاينة.",
    ]


@pytest.mark.parametrize(
    ("sheet", "message"),
    [
        ({"page_number": "bottom_center"}, "الصفحة البيضاء قبل الفصل تحمل رقمها في Word."),
        (
            {"page_number": "none", "running_header": "book"},
            "الصفحة البيضاء قبل الفصل تحمل الترويسة في Word.",
        ),
        (
            {"page_number": "top_outer", "running_header": "chapter"},
            "الصفحة البيضاء قبل الفصل تحمل رقمها والترويسة في Word.",
        ),
        ({"page_number": "none", "running_header": "none"}, None),
    ],
)
def test_the_blank_verso_note_names_what_word_prints_on_it(sheet, message):
    setup = page_setup({**sheet, "chapter_opening": "recto"})
    rows = static_notes(setup, F.resolve("amiri", "amiri", "amiri"), has_contents=False, layout_current=True)
    found = [r["message"] for r in rows if r["code"] == "blank_versos"]
    assert found == ([message] if message else [])
    any_opening = page_setup({**sheet, "chapter_opening": "any"})
    rows = static_notes(
        any_opening, F.resolve("amiri", "amiri", "amiri"), has_contents=False, layout_current=True
    )
    assert "blank_versos" not in [r["code"] for r in rows]


def test_a_book_with_only_section_titles_gets_the_contents_note(db):
    source = document(
        para("p0", LOREM),
        heading("s1", "مبحث أول", level=2),
        para("p1", LOREM),
        heading("s2", "مبحث ثان", level=2),
    )
    book = make_book(source)
    notes = [row["code"] for row in DocxExporter().notes(book, page_setup(None))]
    assert "toc_update" in notes
    result = DocxExporter().export(job_for(book), NullProgress())
    assert "TOC" in parts(result.data)["word/document.xml"].decode()
    assert "toc_update" in [row["code"] for row in result.warnings]
