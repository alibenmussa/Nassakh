"""The editor's text options, empty lines, page breaks and blank pages (D99, the owner's review of 2026-10-03,
items 11, 23, 25 and 26), through every renderer: the document's schema, the book model, the preview's markup
and CSS laid out by WeasyPrint (the live pages' layout: alignment, page-break and blank-line marks), Word,
EPUB, and the windowed re-layout of a long chapter (compared with the whole book laid out from scratch)."""

from __future__ import annotations

import io
import re
import zipfile

from django.conf import settings
from django.test import override_settings

import pytest
from lxml import etree

from editor import document as doc
from editor import merge
from editor.models import Manuscript
from publishing import fonts as F
from publishing import preview, relayout
from publishing.css import stylesheet_css
from publishing.engine import RenderJob, get_engine
from publishing.epub import build_epub
from publishing.html import render_markup
from publishing.layout import offset_problems, page_checks
from publishing.model import book_model, page_setup
from publishing.tests import LOREM, assert_same_pages, document, edit, full_layout, heading, make_book, para
from publishing.word.ooxml import W_NS
from publishing.word.options import WordOptions
from publishing.word.writer import DocMeta, build_docx

W = f"{{{W_NS}}}"
NO_FRONT = {"front_matter": {"title_page": False, "contents": False}}


@pytest.fixture(autouse=True)
def _amiri_only(tmp_path):
    """Every face resolves to the vendored Amiri: the same layout on any machine."""
    with override_settings(NASSAKH={**settings.NASSAKH, "FONT_DIRS": [str(tmp_path / "no-fonts")]}):
        yield


def options_document() -> dict:
    """A chapter with a subtitle, every text option, a run of five empty lines, a blank page and a break."""
    return document(
        heading("h1", "الفصل الأول"),
        heading("s1", "عنوان فرعي", level=2),
        para("p1", "فقرة في الوسط", align="center"),
        *(para(f"e{n}") for n in range(1, 6)),
        para("p2", "Left to right with عربي inside.", dir="ltr"),
        para("p3", "فقرة بإزاحتين وحجم أكبر ومسافة قبلها", indent=2, size="large", spaceBefore=1),
        para("p4", "فقرة في الطرف بلا إزاحة", align="end", firstLine=False, spaceAfter=0.5),
        para("bp", breakBefore=True, breakAfter=True),
        para("p5", "بعد الصفحة الفارغة"),
        para("p6", "تبدأ صفحة", breakBefore=True),
    )


# ====================================================================== the schema


def test_the_schema_accepts_the_text_options_and_refuses_bad_values():
    good = [
        para("a", "نص", align="justify", dir="ltr", indent=4, firstLine=False, spaceBefore=0.5, spaceAfter=2),
        para("b", "نص", size="xlarge", breakAfter=True),
        heading("h", "عنوان", level=2),
    ]
    good[2]["attrs"].update(align="end", size="small")
    assert doc.clean_nodes(good) == good
    for bad in (
        {"align": "left"},
        {"dir": "up"},
        {"indent": 5},
        {"indent": True},
        {"firstLine": "no"},
        {"spaceBefore": 3},
        {"spaceAfter": True},
        {"size": "huge"},
        {"breakAfter": "yes"},
    ):
        with pytest.raises(doc.DocumentError):
            doc.clean_nodes([para("x", "نص", **bad)])
    # the options that print; the defaults are not options
    node = para("y", "نص", align="start", dir="rtl", indent=0, firstLine=True, spaceBefore=0, size="large")
    assert doc.text_attrs(node) == {"align": "start", "size": "large"}
    assert doc.is_empty_block(para("z")) and doc.is_empty_block(para("w", "   "))
    assert not doc.is_empty_block(heading("h2", "")) and not doc.is_empty_block(para("v", "x"))


def test_a_take_of_review_keeps_the_owners_text_options():
    mine = para("p1", "النص المحرر", align="center", size="large", breakAfter=True)
    theirs = para("p1", "النص من المراجعة")
    sides = merge.Sides(m=[mine], f=[theirs], b=[])
    taken = merge.take_nodes(sides)[0]
    assert taken["attrs"]["align"] == "center" and taken["attrs"]["size"] == "large"
    assert taken["attrs"]["breakAfter"] is True and taken["content"][0]["text"] == "النص من المراجعة"


# ====================================================================== the model


def test_the_model_keeps_empty_lines_resolves_breaks_and_maps_the_options():
    book = book_model(options_document(), NO_FRONT)
    blocks = {block.id: block for block in book.blocks()}
    # a run of empty paragraphs prints at most MAX_EMPTY_RUN blank lines
    assert [b for b in blocks if b.startswith("e")] == ["e1", "e2", "e3"] and doc.MAX_EMPTY_RUN == 3
    assert blocks["e1"].empty and blocks["e1"].runs == [] and blocks["e1"].plain == ""
    assert (blocks["p1"].align, blocks["p2"].direction, blocks["p3"].indent, blocks["p3"].size) == (
        "center",
        "ltr",
        2,
        "large",
    )
    assert blocks["p3"].space_before == 1 and blocks["p4"].space_after == 0.5 and not blocks["p4"].first_line
    # a blank page: an empty paragraph with both breaks; the block after it starts a page
    assert blocks["bp"].blank_page and blocks["bp"].break_before and blocks["p5"].break_before
    assert blocks["p3"].size_scale == pytest.approx(1.15) and blocks["p1"].size_scale == 1.0
    # an empty heading prints nothing, and a chapter of empty lines only prints nothing at all
    only = book_model(document(para("x1"), para("x2"), heading("h1", "عنوان"), para("t", "نص")), NO_FRONT)
    assert [c.blocks for c in only.chapters][0] == [] and [b.id for b in only.chapters[1].blocks] == [
        "h1",
        "t",
    ]
    # a page asked to end after a chapter's last block starts the next chapter's first block on a page
    runs = book_model(
        document(para("a", "نص", breakAfter=True), heading("h2", "فصل"), para("b", "نص")), NO_FRONT
    )
    assert next(b for b in runs.blocks() if b.id == "h2").break_before


def test_the_empty_runs_restart_after_a_page_break():
    source = document(
        heading("h", "فصل"), *(para(f"e{n}") for n in range(1, 4)), para("e4", breakBefore=True)
    )
    ids = [b.id for b in book_model(source, NO_FRONT).blocks()]
    assert ids == ["h", "e1", "e2", "e3", "e4"]


# ====================================================================== the preview: markup, CSS, layout


def test_the_markup_and_the_css_of_the_options():
    book = book_model(options_document(), NO_FRONT)
    html = render_markup(book).html
    assert re.search(r'<p class="nk-body nk-a-center" id="b-p1"', html)
    assert 'class="nk-body nk-ltr" id="b-p2"' in html and 'dir="ltr"' in html
    assert 'class="nk-body nk-ind-2 nk-sb-1 nk-sz-large"' in html
    assert 'class="nk-body nk-a-end nk-nofirst nk-sa-05"' in html
    assert re.search(
        r'<p class="nk-body nk-break nk-empty" id="b-bp"[^>]*data-brk="1"[^>]*'
        r'data-empty="1" data-blank="1"><br></p>',
        html,
    )
    css = stylesheet_css(page_setup(None))
    # item 23: a subtitle starts on the right (the start side); the chapter title stays centred
    section = re.search(r"\.nk-section-title \{[^}]*\}", css).group(0)
    assert "text-align: start" in section and "center" not in section
    assert "text-align: center" in re.search(r"\.nk-chapter-title \{[^}]*\}", css).group(0)
    for rule in (
        ".nk-a-center { text-align: center; text-align-last: center; }",
        ".nk-ind-1 { margin-right: 2rem; }",
        ".nk-ltr.nk-ind-1 { margin-right: 0; margin-left: 2rem; }",
        ".nk-quote.nk-ind-1 { margin-right: 4rem; }",
        ".nk-body.nk-sz-large { font-size: 1.15em; }",
        ".nk-chapter-title.nk-sz-small { font-size: 1.36em; }",
        ".nk-quote { margin: 2mm 2rem; text-indent: 0; }",
    ):
        assert rule in css, rule
    # the options come after the styles they change, the page break after the options
    assert css.index(".nk-a-center") > css.index(".nk-section-title") and css.index(
        ".nk-break {"
    ) > css.index(".nk-a-center")
    assert ".nk-section-title.nk-sb-1 { margin-top: 12.8mm; }" in css  # 5 mm + one line (13 pt × 1.7)


def test_the_layout_shows_the_options_the_breaks_and_the_empty_lines():
    source = options_document()
    rendered = get_engine().render(RenderJob(document=source, stylesheet=page_setup(NO_FRONT), pdf=False))
    lines = {(page["n"], line["block"]): line for page in rendered.layout for line in page["lines"]}
    by_block = {block: line for (_n, block), line in lines.items()}
    assert by_block["s1"]["align"] == "right" and by_block["h1"]["align"] == "center"
    assert by_block["p1"]["align"] == "center" and by_block["p2"]["dir"] == "ltr"
    assert by_block["p2"]["align"] == "left" and by_block["p4"]["align"] == "left"
    empty = by_block["e1"]
    assert empty["empty"] is True and empty["runs"] == [] and (empty["start"], empty["end"]) == (0, 0)
    assert empty["w"] > 300 and "e4" not in by_block and "e5" not in by_block  # the measure; three lines only
    # the blank page holds the empty paragraph alone, marked; the next block opens the page after it
    page_of = {block: n for (n, block) in lines}
    assert page_of["p5"] == page_of["bp"] + 1 and page_of["p6"] == page_of["p5"] + 1
    blank = [page for page in rendered.layout if page["n"] == page_of["bp"]][0]
    assert [line["block"] for line in blank["lines"]] == ["bp"]
    assert by_block["bp"]["blank"] is True and by_block["bp"]["brk"] is True
    assert by_block["p5"]["brk"] is True and by_block["p6"]["brk"] is True and "brk" not in by_block["p1"]
    # the indent and the size: the start side moves in by two steps, the lines are taller
    assert by_block["p3"]["h"] > by_block["p1"]["h"] + 2
    # the layout reads as the text, and a blank page or blank lines raise no «almost empty page»
    texts = render_markup(book_model(source, NO_FRONT)).texts
    assert offset_problems(rendered.layout, texts) == []
    checks = [c for c in page_checks(rendered.layout, rendered.chapters) if c["code"] == "almost_empty_page"]
    assert [(c["page"], c["block"]) for c in checks] == [
        (page_of["p6"], "p6")
    ]  # one line of text, not the blank
    # a chapter that ends on its blank page: the blank line is no «almost empty page»
    upto = [page for page in rendered.layout if page["n"] <= page_of["bp"]]
    assert page_checks(upto, [dict(rendered.chapters[0], last=page_of["bp"])]) == []


# ====================================================================== Word


def _docx(source, sheet=None) -> etree._Element:
    book = book_model(source, sheet or NO_FRONT, title="كتاب", author="مؤلف")
    result = build_docx(
        book, F.resolve("amiri", "amiri", "amiri"), WordOptions(), meta=DocMeta.fixed(), embed_fonts=False
    )
    from publishing.word import schema

    assert schema.validate_package(result.data) == [] and schema.integrity_errors(result.data) == []
    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        return etree.fromstring(archive.read("word/document.xml"))


def _paragraphs(root) -> list:
    return root.findall(f".//{W}body/{W}p")


def _text(p) -> str:
    return "".join(t.text or "" for t in p.iter(f"{W}t"))


def test_word_writes_the_options_the_empty_lines_and_the_blank_page():
    root = _docx(options_document())
    paras = {_text(p): p for p in _paragraphs(root)}
    ppr = lambda p: p.find(f"{W}pPr")  # noqa: E731
    jc = lambda p: ppr(p).find(f"{W}jc").get(f"{W}val") if ppr(p).find(f"{W}jc") is not None else None  # noqa: E731
    assert jc(paras["فقرة في الوسط"]) == "center" and jc(paras["فقرة في الطرف بلا إزاحة"]) == "end"
    ltr = paras["Left to right with عربي inside."]
    assert ppr(ltr).find(f"{W}bidi").get(f"{W}val") == "0"
    sized = paras["فقرة بإزاحتين وحجم أكبر ومسافة قبلها"]
    ind = ppr(sized).find(f"{W}ind")
    assert ind.get(f"{W}left") == str(
        round(2 * 2 * 13 * 20)
    )  # two steps of twice the body size, the start side
    assert ind.get(f"{W}firstLine") == str(round(1.5 * 13 * 1.15 * 20))  # the body's first-line indent, sized
    spacing = ppr(sized).find(f"{W}spacing")
    assert (
        spacing.get(f"{W}line") == str(round(1.7 * 13 * 1.15 * 20)) and spacing.get(f"{W}lineRule") == "exact"
    )
    sizes = {r.find(f"{W}rPr/{W}sz").get(f"{W}val") for r in sized.iter(f"{W}r")}
    assert sizes == {str(round(13 * 1.15 * 2))}
    assert ppr(paras["فقرة في الطرف بلا إزاحة"]).find(f"{W}ind").get(f"{W}firstLine") == "0"
    # three empty paragraphs (the run's limit), the blank page's own, then the page breaks
    empties = [p for p in _paragraphs(root) if not _text(p) and p.find(f"{W}pPr/{W}sectPr") is None]
    assert len(empties) == 4
    blank = empties[-1]
    assert ppr(blank).find(f"{W}pageBreakBefore") is not None
    assert ppr(paras["بعد الصفحة الفارغة"]).find(f"{W}pageBreakBefore") is not None
    assert ppr(paras["تبدأ صفحة"]).find(f"{W}pageBreakBefore") is not None
    # item 23: the subtitle's style starts on the start side
    assert jc(paras["عنوان فرعي"]) is None  # its style's own alignment


def test_word_styles_set_the_subtitle_to_the_start_side():
    from publishing.word.styles import style_table

    table = style_table(page_setup(None))
    assert table["Heading2"].align == "start" and table["Heading1"].align == "center"


# ====================================================================== EPUB


def test_epub_writes_the_options_the_empty_lines_and_the_subtitle():
    book = book_model(options_document(), NO_FRONT, title="كتاب", author="مؤلف")
    result = build_epub(book, F.resolve("amiri", "amiri", "amiri"), identifier="urn:uuid:test")
    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        chapter = archive.read("EPUB/chapter-01.xhtml").decode()
        css = archive.read("EPUB/styles/book.css").decode()
    assert '<p class="body a-center">فقرة في الوسط</p>' in chapter
    assert '<p class="body ltr" dir="ltr">' in chapter
    assert '<p class="body ind-2 sb-1 sz-large">' in chapter
    assert chapter.count('<p class="body empty"><br/></p>') == 3
    assert '<p class="body break empty"><br/></p>' in chapter
    assert '<p class="body break">بعد الصفحة الفارغة</p>' in chapter
    assert "text-align: right; }" in re.search(r"h2\.section-title \{[^}]*\}", css).group(0)
    assert ".a-start { text-align: right; } .ltr.a-start { text-align: left; }" in css
    assert "p.body.sz-large { font-size: 1.15em; }" in css


# ====================================================================== the windowed re-layout


def long_chapter(paragraphs: int = 70) -> dict:
    blocks = [heading("h1", "الفصل الأول", pages=(1,)), para("a1", LOREM, pages=(1,))]
    blocks.append(heading("h2", "الفصل الطويل", pages=(2,)))
    for n in range(1, paragraphs + 1):
        blocks.append(para(f"p{n}", LOREM + " " + (LOREM if n % 3 == 0 else ""), pages=(2 + n // 4,)))
    blocks += [heading("h3", "الفصل الأخير", pages=(30,)), para("z1", LOREM, pages=(30,))]
    return document(*blocks)


def _at(document_: dict, block_id: str) -> int:
    return next(i for i, node in enumerate(document_["content"]) if doc.node_id(node) == block_id)


@pytest.mark.django_db
def test_the_window_lays_out_page_breaks_blank_pages_empty_lines_and_options(db):
    book = make_book(long_chapter())
    preview.render_preview(book, "book")

    def blank_page(document_):  # «صفحة فارغة» after p30
        document_["content"].insert(_at(document_, "p30") + 1, para("bp", breakBefore=True, breakAfter=True))

    def page_break(document_):  # ⌘↩ in the middle of p40: its second half starts a page
        at = _at(document_, "p40")
        first, second = (
            dict(document_["content"][at]),
            para("p40b", "النصف الثاني من الفقرة", breakBefore=True),
        )
        document_["content"][at] = first
        document_["content"].insert(at + 1, second)

    def empty_lines(document_):  # Enter twice after p50
        at = _at(document_, "p50")
        document_["content"][at + 1 : at + 1] = [para("e1"), para("e2")]

    def options(document_):  # a subtitle centred, a paragraph made larger and indented
        document_["content"][_at(document_, "p55")]["attrs"].update(align="center", size="xlarge", indent=1)

    def remove_blank(document_):  # the blank page's ×
        del document_["content"][_at(document_, "bp")]

    for change in (blank_page, page_break, empty_lines, options, remove_blank):
        edit(book, change)
        row = relayout.request_relayout(book, "h2")
        assert row.status == "done" and row.result["mode"] in ("window", "chapter"), (
            change.__name__,
            row.result,
        )
        live = relayout.live_pages(book.pk)
        fresh = full_layout(book)
        assert_same_pages(live, fresh)
        # the marks the editor draws follow too (a window opening at a page break marks it as the book does)
        marks = lambda pages: [  # noqa: E731
            (
                page["n"],
                line["block"],
                bool(line.get("brk")),
                bool(line.get("empty")),
                bool(line.get("blank")),
            )
            for page in pages
            for line in page["lines"]
            if line.get("brk") or line.get("empty")
        ]
        assert marks(live) == marks(fresh), change.__name__
    current = Manuscript.objects.get(book=book).document
    assert "bp" not in {doc.node_id(n) for n in current["content"]}
