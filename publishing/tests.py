"""Tests of the publishing app (PHASE5_SPEC §6): the book model from a Phase 4 document (every node type),
the stylesheet's CSS (golden snippets), the font registry (missing files), WeasyPrint on small books (page
count, footnotes at the page foot numbered per page across pages — D46 —, running headers, recto openings,
mirrored margins), the page images, the cache by hash, cancel / revoke / restart, the tasks and the API.

D47 (§9.1): the layout export (every line's box, runs and range; the offsets round-trip, UTF-16 units, the
boxes sit on the PDF's ink), page checks, page-break attrs, widows / orphans, title and copyright pages, the
live layout and the fast re-layout (a later chapter's delta and side swap, recto blank pages, footnotes
renumbered per page when a note moves, forward convergence without chapter breaks, a split chapter), the
queue routing, supersede, failures and the layout / re-layout API."""

from __future__ import annotations

import json
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.test import Client, override_settings
from django.urls import reverse

import numpy
import pymupdf
import pytest
from PIL import Image

from assembly import pipeline
from books.models import Book
from editor import document as doc
from editor.models import Manuscript, StyleSheet
from publishing import engine, fonts, preview, tasks
from publishing import layout as layout_module
from publishing.css import stylesheet_css
from publishing.html import note_order, render_html, with_numbers
from publishing.model import (
    STYLES,
    LineBreak,
    NoteRef,
    PageSetup,
    Run,
    SourceMark,
    book_model,
    direction_runs,
    page_setup,
)
from publishing.models import PreviewRender
from publishing.pdf import per_page_numbers

# ====================================================================== documents


def text(value, *marks):
    node = {"type": "text", "text": value}
    if marks:
        node["marks"] = [{"type": mark} for mark in marks]
    return node


def para(block_id, *content, pages=(1,), **attrs):
    return {
        "type": "paragraph",
        "attrs": {"id": block_id, "sourcePages": list(pages), "sourceLineIds": [], "reviewed": True, **attrs},
        "content": [text(c) if isinstance(c, str) else c for c in content],
    }


def heading(block_id, value, level=1, pages=(1,)):
    return {
        "type": "heading",
        "attrs": {
            "level": level,
            "id": block_id,
            "sourcePages": list(pages),
            "sourceLineIds": [],
            "reviewed": True,
        },
        "content": [text(value)],
    }


def note(note_id, *content, number=1, page=1):
    return {
        "type": "footnote",
        "attrs": {
            "id": note_id,
            "number": number,
            "marker": str(number),
            "sourcePage": page,
            "sourceLineIds": [],
            "orphan": False,
        },
        "content": [text(c) if isinstance(c, str) else c for c in content],
    }


def document(*blocks, title="كتاب التجربة", author="مؤلف الكتاب"):
    return {
        "type": "doc",
        "attrs": {"bookId": 1},
        "content": [{"type": "title", "attrs": {"text": title, "author": author}}, *blocks],
    }


LOREM = (
    "هذا نص تجريبي بالعربية يُكتب ليملأ الصفحة بأسطر كثيرة حتى تنتقل الفقرات إلى الصفحة التالية وتظهر الحواشي "
    "في أسفل كل صفحة كما في الكتب المطبوعة، ويستمر الكلام على هذا النحو زمنًا"
)


def long_book(chapters=2, paragraphs=14, notes_every=2) -> dict:
    """Chapters of long paragraphs, a footnote in every other paragraph (enough for several pages each)."""
    blocks = []
    n = 0
    for c in range(1, chapters + 1):
        blocks.append(heading(f"h{c}", f"الفصل {c}", pages=(c * 10,)))
        for p in range(paragraphs):
            n += 1
            content: list = [LOREM + " "]
            if p % notes_every == 0:
                content.append(note(f"n{n}", f"حاشية رقم {n} في الفصل {c}", number=n))
            content.append(" ثم يكمل النص بعد الحاشية.")
            blocks.append(para(f"p{n}", *content, pages=(c * 10 + p // 4,)))
    return document(*blocks)


# ====================================================================== the book model (D43)


def phase4_document() -> dict:
    """A real Phase 4 document: the assembly pipeline on two synthetic pages (title, headings, paragraphs,
    a join with its page break, footnotes, an orphan note, uncertain words)."""
    L, P = pipeline.LineIn, pipeline.PageIn
    full, indent, centred = (0.1, 0.9), (0.1, 0.85), (0.3, 0.7)

    def ln(i, t, edges=full, kind="body", role="body", uncertain=()):
        return L(
            id=i,
            order=0,
            kind=kind,
            role=role,
            text=t,
            box=(edges[0], i * 0.01, edges[1], i * 0.01 + 0.02),
            uncertain=list(uncertain),
        )

    one = [
        ln(10, "الفصل الأول", centred, role="heading"),
        ln(11, "عنوان فرعي", centred, role="subheading"),
        ln(12, "نص الفقرة (١) يبدأ هنا", indent),
        ln(13, "ويستمر حتى آخر السطر", full, uncertain=[1]),
        ln(14, "(١) حاشية الصفحة الأولى", kind="footnote"),
        ln(15, "(٢) حاشية بلا علامة", kind="footnote"),
    ]
    two = [ln(20, "الصفحة ويتم الكلام.", full), ln(21, "فقرة ثانية", indent)]
    pages = [P(1, 1, "11", "reviewed", True, one), P(2, 2, "12", "reviewed", True, two)]
    for page in pages:
        for order, line in enumerate(page.lines):
            line.order = order
    return pipeline.assemble(pages, {}, {"id": 7, "title": "كتاب التجميع", "author": "المؤلف"}).document


def test_the_book_model_walks_a_phase4_document_with_every_node_type():
    source = phase4_document()
    types = set()

    def walk(node):
        types.add(node["type"])
        for mark in node.get("marks") or []:
            types.add("mark:" + mark["type"])
        for child in node.get("content") or []:
            walk(child)

    walk(source)
    assert {"title", "heading", "paragraph", "footnote", "pageBreak", "text", "mark:uncertain"} <= types
    book = book_model(source, None)
    assert (book.front.title, book.front.author) == ("كتاب التجميع", "المؤلف")
    assert [chapter.kind for chapter in book.chapters] == ["chapter"]
    styles = [block.style for block in book.chapters[0].blocks]
    assert styles == ["chapter-title", "section-title", "body", "body"]
    assert book.chapters[0].blocks[0].runs[0] == SourceMark(1)  # scan page 1 starts with the chapter title
    first = book.chapters[0].blocks[2]
    assert [type(run).__name__ for run in first.runs[1:]].count(
        "NoteRef"
    ) == 2  # the linked note and the orphan
    assert any(isinstance(run, SourceMark) and run.page == 2 for run in first.runs)  # the join's page break
    assert all(not isinstance(run, Run) or run.marks == () for run in first.runs)  # `uncertain` is editorial
    assert "".join(run.text for run in first.runs if isinstance(run, Run)).startswith("نص الفقرة")
    assert [(n.number, n.book_number, n.source_page) for n in first.footnotes] == [(1, 1, 1), (2, 2, 1)]
    assert first.footnotes[1].orphan is True and first.footnotes[0].runs == [Run("حاشية الصفحة الأولى")]
    assert all(block.style in STYLES for block in book.blocks())
    assert [entry.text for entry in book.contents()] == ["الفصل الأول", "عنوان فرعي"]


def test_the_model_maps_the_phase5_styles_and_marks_to_style_names():
    source = document(
        heading("h1", "الفصل"),
        para(
            "p1",
            text("غامق", "bold"),
            text(" ومائل", "italic"),
            text(" معًا", "bold", "italic"),
            text(" ثم", "uncertain"),
        ),
        para("p2", "اقتباس", style="quote"),
        para("p3", "شطر", {"type": "hardBreak"}, "عجز", style="verse"),
        para("p4", "ملاحظة", style="center"),
        {"type": "separator", "attrs": {"id": "s1"}},
        {"type": "horizontalRule"},
        {
            "type": "blockquote",
            "attrs": {"id": "q1"},
            "content": [para("q2", "داخل الاقتباس"), para("q3", "بيت", style="verse")],
        },
        para("p5", "   "),  # empty: left out
        para("p6", "نص", note("n1", text("حاشية ", "bold"), {"type": "hardBreak"}, "ثانية"), " بعدها"),
    )
    book = book_model(source, {"footnote_numbering": "book", "body_font": "lotus"})
    blocks = list(book.blocks())
    assert [b.style for b in blocks] == [
        "chapter-title", "body", "quote", "verse", "center",
        "separator", "separator", "quote", "verse", "body",
    ]  # fmt: skip
    assert blocks[1].runs == [
        Run("غامق", ("bold",)),
        Run(" ومائل", ("italic",)),
        Run(" معًا", ("bold", "italic")),
        Run(" ثم"),
    ]
    assert blocks[3].runs == [Run("شطر"), LineBreak(), Run("عجز")]
    assert blocks[5].runs == [Run("* * *")]
    last = blocks[-1]
    assert last.runs == [Run("نص"), NoteRef("n1"), Run(" بعدها")]
    assert last.footnotes[0].runs == [Run("حاشية", ("bold",)), LineBreak(), Run("ثانية")]
    assert book.setup.footnote_numbering == "book" and book.setup.body_font == "lotus"
    assert {STYLES[b.style].word for b in blocks} >= {
        "Heading 1",
        "Normal",
        "Quote",
        "Verse",
        "Centered",
        "Separator",
    }
    assert STYLES["footnote-text"].word == "Footnote Text"


def test_numbers_count_per_chapter_and_through_the_book_and_scope_keeps_them():
    source = long_book(chapters=2, paragraphs=4, notes_every=1)
    book = book_model(source, None)
    notes = [(n.id, n.number, n.book_number) for n in book.footnotes()]
    assert notes == [
        ("n1", 1, 1),
        ("n2", 2, 2),
        ("n3", 3, 3),
        ("n4", 4, 4),
        ("n5", 1, 5),
        ("n6", 2, 6),
        ("n7", 3, 7),
        ("n8", 4, 8),
    ]
    only = book_model(source, None, chapter_ids=["h2"])
    assert [c.id for c in only.chapters] == ["h2"] and next(only.footnotes()).book_number == 5


def test_page_setup_reads_a_stylesheet_row_a_dict_or_nothing(db):
    assert page_setup(None) == PageSetup()
    sheet = StyleSheet(
        trim="a5", width_mm=148, height_mm=210, heading_scale={"h1": 2}, front_matter={"contents": False}
    )
    setup = page_setup(sheet)
    assert (setup.width_mm, setup.height_mm, setup.h1_scale, setup.h2_scale) == (148, 210, 2.0, 1.25)
    assert setup.contents is False and setup.title_page is True
    assert page_setup({"body_size_pt": "x", "running_header": ""}).body_size_pt == 13.0


def test_direction_runs_split_arabic_and_latin_for_word():
    assert direction_runs("قال Ibn Khaldun في 1377 كتابه") == [
        ("قال ", True),
        ("Ibn Khaldun", False),
        (" في 1377 كتابه", True),
    ]
    assert direction_runs("Latin only") == [("Latin only", False)]
    assert direction_runs("") == []


# ====================================================================== fonts (D45)


def test_the_registry_falls_back_to_amiri_when_files_are_missing(tmp_path):
    with override_settings(NASSAKH={**settings.NASSAKH, "FONT_DIRS": [str(tmp_path)]}):
        status = {row["key"]: row for row in fonts.font_status()}
        assert status["amiri"]["installed"] is True and status["amiri"]["vendored"] is True
        assert (
            status["simplified_arabic"]["installed"] is False
            and status["simplified_arabic"]["message"] == fonts.MISSING_MESSAGE
        )
        resolved = fonts.resolve("simplified_arabic", "times", "amiri")
        assert resolved.body.key == "amiri" and resolved.body.fallback and resolved.latin.key == "amiri"
        assert [(m["field"], m["key"], m["fallback"]) for m in resolved.missing] == [
            ("body_font", "simplified_arabic", "Amiri"),
            ("latin_font", "times", "Amiri"),
        ]
        (tmp_path / "simpo.ttf").write_bytes(
            Path(fonts.VENDORED_DIR / "amiri/Amiri-Regular.ttf").read_bytes()
        )
        again = fonts.resolve("simplified_arabic", "times", "amiri")
        assert again.body.key == "simplified_arabic" and again.body.files.bold is None
        assert again.fingerprint()[0][0].endswith("simpo.ttf")


def test_font_faces_leave_latin_letters_and_lotus_digits_to_the_latin_face(tmp_path):
    lotus = tmp_path / "Lotus.ttf"
    lotus.write_bytes(Path(fonts.VENDORED_DIR / "amiri/Amiri-Regular.ttf").read_bytes())
    with override_settings(NASSAKH={**settings.NASSAKH, "FONT_DIRS": [str(tmp_path)]}):
        css = fonts.font_face_css(fonts.resolve("lotus", "amiri", "amiri"))
    body = [rule for rule in css.splitlines() if '"nk-body"' in rule]
    heading = [rule for rule in css.splitlines() if '"nk-heading"' in rule]
    latin = [rule for rule in css.splitlines() if '"nk-latin"' in rule]
    assert body and f'url("{lotus.as_uri()}")' in body[0]
    # Lotus: no digits, no Latin (and only the characters its file has: no control characters)
    assert "U+0020-002F, U+003A-0040" in body[0] and "U+0041" not in body[0]
    assert "U+0020-0040," in heading[0] and "font-weight: 700" in heading[1]  # Amiri keeps its digits
    assert latin and "unicode-range" not in latin[0]
    assert fonts.families("heading") == '"nk-heading", "nk-latin", serif'


# ====================================================================== CSS (golden snippets)


def css_of(**values) -> str:
    setup = page_setup(values)
    return stylesheet_css(
        setup, fonts.resolve(setup.body_font, setup.latin_font, setup.heading_font), **values.pop("_kw", {})
    )


def test_page_rules_size_mirrored_margins_and_page_numbers():
    css = css_of()
    assert "@page { size: 170mm 240mm; margin: 20mm 18mm 22mm 22mm;" in css
    assert (
        "@page :left { margin-left: 18mm; margin-right: 22mm;" in css
    )  # recto (odd, RTL): inner on the right
    assert "@page :right { margin-left: 22mm; margin-right: 18mm;" in css
    assert "string(running" not in css  # no running header by default (owner, 2026-09-25)
    assert "@top-center { content: string(running, first-except);" in css_of(running_header="chapter")
    assert "@bottom-center { content: counter(page);" in css
    assert "@footnote { border-top: 0.4pt solid #000;" in css
    assert ".nk-chapter.is-chapter, .nk-chapter.is-front { break-before: page; }" in css
    assert "bleed" not in css and "counter-reset" not in css
    outer = css_of(page_number="bottom_outer", running_header="none", chapter_opening="recto", bleed_mm=3)
    assert (
        "@page :left { margin-left: 18mm; margin-right: 22mm; @bottom-left { content: counter(page);" in outer
    )
    assert "@bottom-right { content: counter(page);" in outer and "string(running" not in outer
    assert "break-before: recto;" in outer and "bleed: 3mm; marks: crop;" in outer
    top = css_of(page_number="top_outer")
    assert "@top-left { content: counter(page);" in top and "@top-right { content: counter(page);" in top


def test_type_rules_follow_the_stylesheet():
    css = css_of(
        body_size_pt=12.5,
        line_height=1.8,
        indent_em=2,
        footnote_size_pt=9,
        heading_scale={"h1": 2, "h2": 1.5},
    )
    assert 'html { font-family: "nk-body", "nk-latin", serif; font-size: 12.5pt; line-height: 1.8;' in css
    assert '.nk-body::before { content: ""; display: inline-block; width: 2em; }' in css
    assert "font-size: 2em; font-weight: bold; line-height: 1.35;" in css and "font-size: 1.5em;" in css
    assert (
        ".nk-fn { float: footnote; footnote-display: block;" in css
        and "font-size: 9pt; line-height: 1.5;" in css
    )
    # the call is 0.62 of the body text (7.75pt), not of the note's own 9pt
    call = '.nk-fn::footnote-call { content: "(" attr(data-n) ")"; font-size: 7.75pt; vertical-align: super;'
    assert call in css
    assert ".nk-chapter-title { string-set: running attr(data-running);" in css
    assert ".nk-src" not in css and ".nk-src" in css_of(print_source_pages=True)


def test_a_chapter_alone_starts_at_its_page_on_the_right_side():
    odd = css_of(_kw={"scope": "chapter", "first_page": 37})
    assert "@page :first { counter-reset: page 37; }" in odd and "html { break-before: recto; }" in odd
    even = css_of(_kw={"scope": "chapter", "first_page": 38})
    assert "html { break-before: verso; }" in even
    assert "counter-reset" not in css_of(_kw={"scope": "chapter", "first_page": 1})


# ====================================================================== HTML


def test_html_escapes_text_and_marks_the_contract():
    source = document(
        heading("h1", "<script>x</script>"),
        para("p1", text("غامق", "bold"), note("n1", "حاشية & <b>")),
        title="عنوان «الكتاب»",
    )
    html = render_html(book_model(source, {"running_header": "book"}))
    assert "<script>x" not in html and "&lt;script&gt;" in html and "&amp; &lt;b&gt;" in html
    assert '<section class="nk-chapter is-chapter" id="ch-h1" data-chapter="h1">' in html
    assert (
        '<h1 class="nk-chapter-title" id="b-h1" data-block="h1" data-kind="heading"'
        ' data-style="chapter-title" data-running="عنوان «الكتاب»">' in html
    )  # book mode: the title; the layout tags (D47)
    assert (
        '<span class="nk-fn" id="fn-n1" data-n="1" data-block="n1" data-note="n1" data-kind="note"'
        ' data-style="footnote-text">'
        in html
        and "<b>غامق</b>" in html
    )
    assert '<nav class="nk-front nk-contents" id="front-contents">' in html and 'href="#b-h1"' in html
    assert note_order(html) == ["fn-n1"] and 'data-n="7"' in with_numbers(html, {"fn-n1": "7"})
    chapter_only = render_html(book_model(source, None, chapter_ids=["h1"]), scope="chapter")
    assert "nk-title-page" not in chapter_only and "nk-contents" not in chapter_only


# ====================================================================== WeasyPrint (D42, D46)


def pdf_pages(pdf: bytes) -> list[pymupdf.Page]:
    return list(pymupdf.open(stream=pdf, filetype="pdf"))


def bracketed(page: pymupdf.Page) -> list[int]:
    """The numbers printed in parentheses on a page (footnote calls and markers; RTL text comes out
    mirrored, «)3(», so both orders count)."""
    return sorted(int(n) for n in re.findall(r"[()]\s*(\d{1,2})\s*[()]", page.get_text()))


def job(source, **sheet) -> engine.RenderJob:
    return engine.RenderJob(document=source, stylesheet=sheet, title="كتاب", author="")


def test_a_note_stays_on_the_page_of_its_call_under_widows_and_orphans():
    """WeasyPrint drops a note to the next page when it steps back for widows / orphans at the page foot
    (the call stays behind, the page is full or has room left): the renderer relaxes widows / orphans at
    that page break and lays out again, so the note prints on its call's page."""
    blocks = []
    for i, k in enumerate((1, 2, 3, 3, 2)):
        content: list = [(LOREM + " ") * k]
        if i == 0:
            content += [note("nx", "حاشية طويلة للاختبار", number=1), " ثم يكمل النص بعد الحاشية."]
        blocks.append(para(f"p{i}", *content))
    rendered = engine.get_engine().render(job(document(*blocks), trim="a5", widows=2, orphans=2))
    pages = [page.get_text() for page in pdf_pages(rendered.pdf)]
    call_page = next(i for i, page in enumerate(pages, start=1) if "ثم يكمل النص بعد الحاشية" in page)
    assert rendered.footnotes["fn-nx"] == call_page
    assert "حاشية طويلة للاختبار" in pages[call_page - 1]
    assert rendered.passes >= 2  # the relaxing pass ran


def test_footnotes_sit_at_the_page_foot_numbered_per_page_across_pages():
    rendered = engine.get_engine().render(
        job(long_book(chapters=1, paragraphs=24), running_header="chapter", page_number="bottom_center")
    )
    assert rendered.passes >= 2 and rendered.page_count >= 5
    by_page: dict[int, list[str]] = {}
    for element_id, page in rendered.footnotes.items():
        by_page.setdefault(page, []).append(element_id)
    assert len(by_page) >= 3  # the notes spread over several pages
    pages = pdf_pages(rendered.pdf)
    for page_number, ids in by_page.items():
        # every note of the page is printed twice (call and marker), numbered from 1 on that page
        expected = sorted([n for n in range(1, len(ids) + 1) for _ in (0, 1)])
        assert bracketed(pages[page_number - 1]) == expected, (page_number, bracketed(pages[page_number - 1]))
    chapter_page = rendered.chapters[0]["first"]
    later = pages[chapter_page]  # the page after the opening shows the chapter title at the top
    top = [b for b in later.get_text("dict")["blocks"] if b.get("lines") and b["bbox"][3] < 20 * 72 / 25.4]
    header = " ".join("".join(span["text"] for line in b["lines"] for span in line["spans"]) for b in top)
    assert set(header.split()) == {"الفصل", "1"}  # (extracted in visual order: «1 الفصل»)
    opening = pages[chapter_page - 1]
    top_opening = [
        b for b in opening.get_text("dict")["blocks"] if b.get("lines") and b["bbox"][3] < 20 * 72 / 25.4
    ]
    assert not top_opening  # no running header on the chapter's opening page


def test_per_page_numbering_helper_ranks_notes_by_page():
    order = ["fn-a", "fn-b", "fn-c", "fn-d"]
    assert per_page_numbers(order, {"fn-a": 3, "fn-b": 3, "fn-c": 4, "fn-d": 4}) == {
        "fn-a": "1",
        "fn-b": "2",
        "fn-c": "1",
        "fn-d": "2",
    }


def test_chapter_and_book_numbering_take_one_pass():
    source = long_book(chapters=2, paragraphs=6, notes_every=1)
    by_chapter = engine.get_engine().render(job(source, footnote_numbering="chapter"))
    assert by_chapter.passes == 1
    by_book = engine.get_engine().render(job(source, footnote_numbering="book"))
    assert by_book.passes == 1 and 12 in {n for page in pdf_pages(by_book.pdf) for n in bracketed(page)}
    assert 12 not in {n for page in pdf_pages(by_chapter.pdf) for n in bracketed(page)}
    html = render_html(book_model(source, {"footnote_numbering": "book"}))
    assert 'id="fn-n7" data-n="7"' in html
    html = render_html(book_model(source, {"footnote_numbering": "chapter"}))
    assert 'id="fn-n7" data-n="1"' in html


def test_recto_openings_and_mirrored_margins():
    rendered = engine.get_engine().render(
        job(
            long_book(chapters=3, paragraphs=9, notes_every=9),
            chapter_opening="recto",
            page_number="bottom_outer",
        )
    )
    firsts = [chapter["first"] for chapter in rendered.chapters]
    assert all(first % 2 == 1 for first in firsts)  # every chapter opens on a recto (odd, left-hand in RTL)
    pages = pdf_pages(rendered.pdf)
    mm = 72 / 25.4
    width, height = pages[0].rect.width, pages[0].rect.height

    def body_edges(page):
        lines = [
            line["bbox"]
            for block in page.get_text("dict")["blocks"]
            for line in block.get("lines", [])
            if 25 * mm < line["bbox"][1] and line["bbox"][3] < height * 0.7
        ]
        return (min(b[0] for b in lines), max(b[2] for b in lines)) if lines else None

    body_pages = range(firsts[0], rendered.page_count + 1)
    right_hand = next(
        n for n in body_pages if n % 2 == 0 and body_edges(pages[n - 1])
    )  # even: a right-hand page
    left_hand = next(
        n for n in body_pages if n % 2 == 1 and body_edges(pages[n - 1])
    )  # odd: a left-hand recto
    left, right = body_edges(pages[right_hand - 1])
    assert left == pytest.approx(22 * mm, abs=2.5 * mm) and width - right == pytest.approx(
        18 * mm, abs=2.5 * mm
    )
    left, right = body_edges(pages[left_hand - 1])
    assert left == pytest.approx(18 * mm, abs=2.5 * mm) and width - right == pytest.approx(
        22 * mm, abs=2.5 * mm
    )
    blank = [i + 1 for i, page in enumerate(pages) if not page.get_text().strip()]
    assert blank and all(n % 2 == 0 for n in blank)  # blank versos before recto openings
    foot = [
        line["bbox"]
        for block in pages[left_hand - 1].get_text("dict")["blocks"]
        for line in block.get("lines", [])
        if line["bbox"][1] > height - 22 * mm
    ]
    assert foot and foot[0][2] < width / 2  # bottom outer: the left corner of a left-hand page


def test_the_title_page_and_contents_come_first_and_link_to_their_pages():
    rendered = engine.get_engine().render(job(long_book(chapters=2, paragraphs=3, notes_every=9)))
    pages = pdf_pages(rendered.pdf)
    assert "كتاب التجربة" in pages[0].get_text() and "مؤلف الكتاب" in pages[0].get_text()
    contents = pages[1].get_text()
    assert "المحتويات" in contents and contents.count("الفصل") == 2
    assert re.search(rf"(?<!\d){rendered.chapters[1]['first']}(?!\d)", contents)  # chapter 2's page number
    assert rendered.chapters[0]["first"] == 3 and rendered.chapters[-1]["last"] == rendered.page_count


def test_a_chapter_renders_alone_from_its_page():
    source = long_book(chapters=2, paragraphs=3)
    rendered = engine.get_engine().render(
        engine.RenderJob(document=source, stylesheet={}, scope="chapter", chapter_id="h2", first_page=9)
    )
    version = doc.chapter_version(doc.find_chapter(source, "h2").nodes(source))
    assert rendered.chapters == [
        {"id": "h2", "title": "الفصل 2", "first": 9, "last": 9 + rendered.page_count - 1, "version": version}
    ]
    first = pdf_pages(rendered.pdf)[0]
    assert "9" in first.get_text().split() and "الفصل 1" not in "".join(
        p.get_text() for p in pdf_pages(rendered.pdf)
    )
    assert [page["n"] for page in rendered.layout] == list(range(9, 9 + rendered.page_count))


# ====================================================================== previews, cache, tasks, API


def role_user(name: str, role: str | None) -> User:
    user = User.objects.create_user(name, password="pass-1234")
    if role:
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


def logged(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def book(db):
    from assembly.models import AssemblyRun

    book = Book.objects.create(title="كتاب المعاينة", author="المؤلف")
    run = AssemblyRun.objects.create(book=book, status="done")
    Manuscript.objects.create(book=book, document=long_book(chapters=2, paragraphs=6), version=1, run=run)
    return book


def media(path: str) -> Path:
    return Path(settings.MEDIA_ROOT) / path


def test_render_preview_writes_the_pdf_and_page_images(book):
    render = preview.render_preview(book, "book")
    assert render.status == "done" and render.page_count >= 4 and render.passes >= 1
    folder = media(render.folder)
    assert (
        render.folder == f"books/{book.pk}/preview/{render.content_hash}" and (folder / "book.pdf").is_file()
    )
    one, two = Image.open(folder / "page-0001.webp"), Image.open(folder / "page-0001-2x.webp")
    assert (
        one.height == 1100
        and two.height == 2200
        and (folder / f"page-{render.page_count:04d}-2x.webp").is_file()
    )
    assert [c["id"] for c in render.chapters] == ["h1", "h2"]
    assert engine.chapter_pages(book)["h2"]["last"] == render.page_count


def test_a_request_for_the_same_hash_reuses_the_cache(book, monkeypatch):
    first = engine.request_preview(book, "book")
    assert first.status == "done"
    calls = []
    real = engine.WeasyPrintEngine.render
    monkeypatch.setattr(
        engine.WeasyPrintEngine, "render", lambda self, j, c=None: calls.append(j) or real(self, j, c)
    )
    again = engine.request_preview(book, "book")
    assert again.pk == first.pk and calls == []
    assert preview.render_preview(book, "book").pk == first.pk and calls == []
    StyleSheet.objects.create(book=book, body_size_pt=12)
    changed = engine.request_preview(book, "book")
    assert changed.pk != first.pk and changed.status == "done" and len(calls) == 1


def test_a_new_hash_cancels_and_revokes_the_running_render(book, monkeypatch):
    revoked = []
    monkeypatch.setattr(preview, "revoke", lambda task_id: revoked.append(task_id))
    monkeypatch.setattr(preview, "prune", lambda *args: 0)  # keep the cancelled rows to look at them
    stale = PreviewRender.objects.create(
        book=book, scope="book", content_hash="old", status="running", task_id="task-old"
    )
    queued = PreviewRender.objects.create(
        book=book, scope="book", content_hash="older", status="queued", task_id="task-q"
    )
    other = PreviewRender.objects.create(
        book=book, scope="chapter", chapter_id="h1", content_hash="c", status="running", task_id="t-c"
    )
    render = engine.request_preview(book, "book")
    assert render.status == "done" and sorted(revoked) == ["task-old", "task-q"]
    assert (
        PreviewRender.objects.get(pk=stale.pk).status == "cancelled"
        and PreviewRender.objects.get(pk=queued.pk).status == "cancelled"
    )
    assert PreviewRender.objects.get(pk=other.pk).status == "running"  # another scope is left alone


def test_a_cancelled_job_stops_at_its_next_check(book, monkeypatch):
    render = PreviewRender.objects.create(book=book, scope="book", content_hash="x", status="running")
    job_ = preview.job_for(book, "book", None)
    PreviewRender.objects.filter(pk=render.pk).update(status="cancelled")
    preview._run(render, job_)
    render.refresh_from_db()
    assert (
        render.status == "cancelled"
        and render.page_count == 0
        and not media(preview.preview_folder(book.pk, "x")).exists()
    )


def test_a_failed_render_is_reported_and_retried_on_request(book, monkeypatch):
    def boom(self, j, c=None):
        raise RuntimeError("layout exploded")

    monkeypatch.setattr(engine.WeasyPrintEngine, "render", boom)
    failed = engine.request_preview(book, "book")
    assert (
        failed.status == "error"
        and failed.error.startswith(preview.RENDER_ERROR)
        and "layout exploded" in failed.error
    )
    assert engine.request_preview(book, "book").pk == failed.pk  # not retried by itself
    payload = engine.preview_payload(book, "book")
    assert (
        payload["status"] == "error" and payload["error"] == preview.RENDER_ERROR and payload["pages"] == []
    )
    monkeypatch.undo()
    retried = engine.request_preview(book, "book", force=True)
    assert retried.pk != failed.pk and retried.status == "done"


def test_scheduled_renders_skip_when_a_later_save_happened(book):
    assert tasks.render_book_preview.delay(book.pk, 99).get() is None and not PreviewRender.objects.exists()
    assert tasks.render_chapter_preview.delay(book.pk, "h2", 1).get() is not None
    chapter = PreviewRender.objects.get(scope="chapter")
    assert chapter.chapter_id == "h2" and chapter.status == "done" and chapter.first_page == 1
    assert tasks.render_book_preview.delay(book.pk).get() is not None
    assert tasks.render_chapter_preview.delay(book.pk, "missing").get() is None


def test_a_chapter_preview_starts_at_its_page_in_the_book_render(book):
    whole = preview.render_preview(book, "book")
    first = next(c["first"] for c in whole.chapters if c["id"] == "h2")
    chapter = preview.render_preview(book, "chapter", "h2")
    assert chapter.first_page == first and chapter.chapters[0]["first"] == first
    pages = preview.pages_of(chapter)
    assert (
        pages[0]["n"] == first and pages[0]["chapter"] == "h2" and pages[0]["url"].endswith("/page-0001.webp")
    )


def test_prune_keeps_the_newest_renders(book):
    rows = [
        PreviewRender.objects.create(
            book=book,
            scope="book",
            content_hash=f"h{i}",
            status="done",
            folder=f"books/{book.pk}/preview/h{i}",
        )
        for i in range(5)
    ]
    for row in rows:
        media(row.folder).mkdir(parents=True, exist_ok=True)
    assert preview.prune(book.pk, "book") == 2
    assert set(PreviewRender.objects.values_list("content_hash", flat=True)) == {"h2", "h3", "h4"}
    assert not media(rows[0].folder).exists() and media(rows[4].folder).exists()


def test_preview_api_queues_renders_and_reports_stale_pages(book, db):
    reader = role_user("reader", "proofreader")
    client = logged(reader)
    url = reverse("api:preview", args=[book.pk])
    data = client.get(url).json()
    assert data["status"] == "done" and data["stale"] is False and data["rendering"] is False
    assert set(data) >= {"status", "hash", "page_count", "chapters", "pages", "stale"}
    assert data["pages"][0] == {
        "n": 1,
        "url": f"/media/books/{book.pk}/preview/{data['hash']}/page-0001.webp",
        "url2x": f"/media/books/{book.pk}/preview/{data['hash']}/page-0001-2x.webp",
        "chapter": None,
    }
    assert client.get(data["pages"][0]["url"]).status_code == 200
    manuscript = Manuscript.objects.get(book=book)
    manuscript.document["content"][2]["content"][0]["text"] = "نص معدل " + LOREM
    manuscript.save()
    shown = client.get(f"{url}?scope=book").json()  # a new content: queued (eager: rendered at once)
    assert shown["hash"] != data["hash"] and shown["status"] == "done" and shown["stale"] is False
    chapter = client.get(f"{url}?scope=chapter&chapter=h1").json()
    assert (
        chapter["scope"] == "chapter"
        and chapter["chapter"] == "h1"
        and chapter["pages"][0]["chapter"] == "h1"
    )
    assert client.get(f"{url}?scope=chapter&chapter=zz").status_code == 404
    assert client.get(f"{url}?scope=shelf").status_code == 400
    assert client.post(url, json.dumps({"scope": "book"}), content_type="application/json").status_code == 403
    editor = logged(role_user("editor", "editor"))
    posted = editor.post(url, json.dumps({"scope": "book"}), content_type="application/json")
    assert posted.status_code == 202 and posted.json()["status"] == "done"
    empty = Book.objects.create(title="بلا مخطوطة")
    assert client.get(reverse("api:preview", args=[empty.pk])).status_code == 404


def test_stale_pages_stay_on_screen_while_the_new_render_waits(book):
    done = preview.render_preview(book, "book")
    manuscript = Manuscript.objects.get(book=book)
    manuscript.document["content"][2]["content"][0]["text"] = "تغيير"
    manuscript.save()
    payload = engine.preview_payload(book, "book", enqueue=False)
    assert payload["status"] == "none" and payload["stale"] is True and payload["render_id"] == done.pk
    assert payload["page_count"] == done.page_count and len(payload["pages"]) == done.page_count


def test_the_stylesheet_put_renders_the_book_and_the_dashboard_counts_its_pages(book):
    from books.services import book_progress

    editor = logged(role_user("editor", "editor"))
    response = editor.put(
        reverse("api:stylesheet", args=[book.pk]),
        json.dumps({"trim": "a5", "chapter": "h1"}),
        content_type="application/json",
    )
    assert response.status_code == 200
    data = response.json()
    assert data["preview"]["status"] == "done" and data["preview"]["page_count"] > 0
    assert PreviewRender.objects.filter(scope="chapter", chapter_id="h1", status="done").exists()
    layout = book_progress(book)["layout"]
    assert (
        layout["trim"] == "a5"
        and layout["trim_label"] == "A5"
        and layout["page_count"] == data["preview"]["page_count"]
    )
    assert layout["rendering"] is False


def test_the_book_pdf_export_uses_the_same_engine(book):
    rendered = engine.render_book_pdf(book)
    assert rendered.pdf.startswith(b"%PDF") and rendered.page_count >= 4
    assert engine.render_book_pdf(Book.objects.create(title="فارغ")) is None


# ====================================================================== review fixes (Phase 5 backend review)


def calls_and_markers(page: pymupdf.Page) -> tuple[list[int], list[int]]:
    """The footnote numbers of a page: the calls in the text (0.62 of the 13pt body) and the markers at its
    foot (the 10pt notes)."""
    calls: list[int] = []
    markers: list[int] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                found = [int(n) for n in re.findall(r"[()]\s*(\d{1,2})\s*[()]", span["text"])]
                (calls if span["size"] < 9 else markers).extend(found)
    return sorted(calls), sorted(markers)


def test_a_note_that_does_not_fit_takes_its_call_to_the_next_page():
    """D46: a long note never lands on a page after its call (the call's line moves with it), so every
    page shows the same numbers in its text and at its foot, numbered from 1."""
    long_note = "نص حاشية طويلة تمتد على أسطر كثيرة في أسفل الصفحة " * 28
    blocks = [heading("h1", "الفصل")]
    n = 0
    for i in range(36):
        content: list = [LOREM + " " + LOREM + " "]
        if i % 3 == 2:
            n += 1
            content += [note(f"n{n}", long_note if n % 2 else "حاشية قصيرة"), " تتمة الفقرة."]
        blocks.append(para(f"p{i}", *content))
    rendered = engine.get_engine().render(
        job(document(*blocks), front_matter={"title_page": False, "contents": False})
    )
    pages = pdf_pages(rendered.pdf)
    with_notes = 0
    for number, page in enumerate(pages, start=1):
        calls, markers = calls_and_markers(page)
        assert calls == markers, (number, calls, markers)
        assert markers == list(range(1, len(markers) + 1)), (number, markers)
        with_notes += bool(markers)
    assert with_notes >= 5 and sorted(set(rendered.footnotes.values())) != [1]


def subset_font(tmp_path: Path, name: str, text_: str) -> Path:
    """A copy of Amiri with only the glyphs of `text_` (a face that lacks the rest)."""
    from fontTools import subset

    target = tmp_path / name
    options = subset.Options()
    font = subset.load_font(str(fonts.VENDORED_DIR / "amiri/Amiri-Regular.ttf"), options)
    subsetter = subset.Subsetter(options)
    subsetter.populate(text=text_)
    subsetter.subset(font)
    subset.save_font(font, str(target), options)
    return target


def test_a_character_the_arabic_face_lacks_falls_back_to_the_latin_face(tmp_path):
    """The unicode-range of an Arabic face lists only what its file has (Lotus has no en dash, curly
    quotes or ellipsis): the rest is set in the Latin face instead of a missing-glyph box."""
    subset_font(tmp_path, "Lotus.ttf", "ابتثجحخدذرزسشصضطظعغفقكلمنهوي ،؛؟.()")
    with override_settings(NASSAKH={**settings.NASSAKH, "FONT_DIRS": [str(tmp_path)]}):
        resolved = fonts.resolve("lotus", "times", "amiri")
        css = fonts.font_face_css(resolved)
    body = next(rule for rule in css.splitlines() if '"nk-body"' in rule)
    ranges = fonts.parse_ranges(body.split("unicode-range:")[1].split(";")[0])
    covered = {code for lo, hi in ranges for code in range(lo, hi + 1)}
    assert ord("ب") in covered and ord("،") in covered and ord("(") in covered
    assert not covered & {0x2013, 0x201C, 0x201D, 0x2026, ord("A"), ord("5")}
    assert fonts.format_ranges([0x20, 0x21, 0x22, 0x28, 0x600]) == "U+0020-0022, U+0028, U+0600"
    # the whole range when the file cannot be read
    broken = tmp_path / "broken.ttf"
    broken.write_bytes(b"not a font")
    assert fonts.face_ranges(broken, "U+0600-06FF") == "U+0600-06FF"


def test_latin_italic_uses_the_italic_files_when_installed(tmp_path):
    for name in ("Times New Roman.ttf", "Times New Roman Italic.ttf", "Times New Roman Bold Italic.ttf"):
        (tmp_path / name).write_bytes(Path(fonts.VENDORED_DIR / "amiri/Amiri-Regular.ttf").read_bytes())
    with override_settings(NASSAKH={**settings.NASSAKH, "FONT_DIRS": [str(tmp_path)]}):
        resolved = fonts.resolve("amiri", "times", "amiri")
        css = fonts.font_face_css(resolved)
    latin = [rule for rule in css.splitlines() if '"nk-latin"' in rule]
    styles = sorted(re.search(r"font-weight: (\d+); font-style: (\w+)", rule).groups() for rule in latin)
    assert styles == [("400", "italic"), ("400", "normal"), ("700", "italic")]
    assert any("Italic.ttf" in str(item) for item in resolved.fingerprint() if item)


def test_run_on_sections_share_the_page_where_the_next_one_starts():
    blocks = [para(f"p{n}", LOREM + " " + LOREM, pages=(n,)) for n in range(1, 71)]
    rendered = engine.get_engine().render(job(document(*blocks)))
    ranges = rendered.chapters
    assert [c["title"] for c in ranges] == ["القسم 1", "القسم 2", "القسم 3"]
    for before, after in zip(ranges, ranges[1:], strict=False):
        assert before["last"] == after["first"]  # no page break between sections
    assert ranges[-1]["last"] == rendered.page_count


def test_an_empty_chapter_prints_no_blank_page():
    source = document(para("p0", text(" ")), heading("h1", "الفصل الأول"), para("p1", LOREM))
    book = book_model(source, {})
    assert [c.id for c in book.chapters] == ["p0", "h1"] and not book.chapters[0].blocks
    html = render_html(book)
    assert 'id="ch-p0"' not in html and 'id="ch-h1"' in html
    rendered = engine.get_engine().render(job(source, front_matter={"title_page": False, "contents": False}))
    assert rendered.page_count == 1 and [c["id"] for c in rendered.chapters] == ["h1"]


# ====================================================================== D47: the layout export


def plain_texts(source: dict) -> dict[str, str]:
    """Block / note id → the plain text the layout's offsets count in (`object_kinds`)."""
    out = {}
    for block, note_, content in doc.iter_containers(doc.content_of(source)):
        out[doc.node_id(note_ if note_ is not None else block)] = doc.object_kinds(content)
    return out


def rich_book() -> dict:
    """Two chapters with every inline kind: marks, footnotes (with a mark inside), hard breaks, scan page
    marks, a verse, a quote, a separator, a section title and Latin text with digits."""
    page_mark = {"type": "pageBreak", "attrs": {"page": 12, "printed": "12"}}
    blocks = [heading("h1", "الفصل الأول", pages=(10,))]
    for n in range(10):
        blocks.append(
            para(
                f"p{n}",
                LOREM + " ",
                text("كلمة غامقة", "bold"),
                " و",
                text("مائلة", "italic"),
                note(f"n{n}", "حاشية ", text("مهمة", "bold"), f" رقم {n}", number=n + 1),
                " تتمة English 2024 النص.",
                {"type": "hardBreak"},
                "سطر بعد الفاصل",
                page_mark,
                " " + LOREM,
                pages=(10 + n,),
            )
        )
        if n == 4:
            blocks.append(heading(f"s{n}", "عنوان فرعي", level=2, pages=(14,)))
            blocks.append(para(f"v{n}", "بيت من الشعر", {"type": "hardBreak"}, "عجز البيت", style="verse"))
            blocks.append({"type": "separator", "attrs": {"id": f"x{n}"}})
            blocks.append(para(f"q{n}", "اقتباس " + LOREM, style="quote"))
    blocks.append(heading("h2", "الفصل الثاني", pages=(30,)))
    blocks.append(para("p99", LOREM, note("n99", "حاشية الفصل الثاني"), " " + LOREM, pages=(30,)))
    return document(*blocks)


def test_every_render_exports_its_layout_and_the_offsets_round_trip():
    source = rich_book()
    for sheet in (
        {},
        {"print_source_pages": True, "running_header": "chapter", "page_number": "bottom_outer"},
    ):
        rendered = engine.get_engine().render(job(source, **sheet))
        assert rendered.misses == 0 and rendered.layout and len(rendered.layout) == rendered.page_count
        assert layout_module.offset_problems(rendered.layout, plain_texts(source)) == []
    pages = rendered.layout
    assert [page["n"] for page in pages] == list(range(1, rendered.page_count + 1))
    assert [page["side"] for page in pages[:4]] == ["left", "right", "left", "right"]  # odd pages recto
    body = [line for page in pages for line in page["lines"] if line["block"] == "p0"]
    assert body[0]["first"] and not body[1]["first"] and body[0]["kind"] == "body" and body[0]["dir"] == "rtl"
    ends = [line for line in body if line["runs"][-1]["text"].endswith("النص.")]
    assert (
        body[0]["justify"] and ends and not ends[0]["justify"]
    )  # a line ended by a hard break is not justified
    assert not body[-1]["justify"] and body[-1]["end"] == len(
        doc.object_kinds(source["content"][2]["content"])
    )
    assert set(body[0]) >= {
        "block",
        "kind",
        "x",
        "y",
        "w",
        "h",
        "baseline",
        "dir",
        "justify",
        "start",
        "end",
        "runs",
    }
    assert body[0]["y"] < body[0]["baseline"] < body[0]["y"] + body[0]["h"]
    calls = [run for line in body for run in line["runs"] if run["note"]]
    assert calls == [dict(calls[0], text="(1)", sup=True, note="n0")]
    text_p0 = doc.object_kinds(source["content"][2]["content"])
    assert text_p0[calls[0]["start"]] == doc.OBJECT and calls[0]["end"] == calls[0]["start"] + 1
    bold = next(run for line in body for run in line["runs"] if run["weight"] == 700)
    assert bold["text"] == "كلمة غامقة" and text_p0[bold["start"] : bold["end"]] == "كلمة غامقة"
    assert any(run["italic"] for line in body for run in line["runs"])
    note_lines = [line for page in pages for line in page["lines"] if line["block"] == "n0"]
    assert note_lines[0]["kind"] == "note" and note_lines[0]["runs"][0]["text"].startswith("(1)")
    assert note_lines[0]["runs"][0]["start"] == note_lines[0]["runs"][0]["end"] == 0  # the marker: generated
    page_of_p0 = next(page for page in pages if any(line["block"] == "p0" for line in page["lines"]))
    assert page_of_p0["footnote_rule"]["y"] < note_lines[0]["y"]
    marks = [line for page in pages for line in page["lines"] if line["kind"] == "source"]
    assert marks and marks[0]["runs"][0]["text"] == "ص 12" and marks[0]["page"] == 12
    verse = next(line for page in pages for line in page["lines"] if line["block"] == "v4")
    assert verse["style"] == "verse" and not verse["justify"]
    separator = next(line for page in pages for line in page["lines"] if line["block"] == "x4")
    assert separator["kind"] == "separator" and separator["start"] == separator["end"] == 0
    later = pages[rendered.chapters[0]["first"]]  # a page after the chapter's opening
    assert later["header"]["text"] == "الفصل الأول" and later["number"]["text"] == str(later["n"])
    assert later["number"]["align"] in ("left", "right")  # bottom outer
    front = [line["block"] for line in pages[0]["lines"]]
    assert front == ["front-title", "front-author"] and pages[1]["lines"][1]["target"] == "h1"
    toc = pages[1]["lines"][1]["runs"]
    assert (
        toc[1]["leader"] is True
        and toc[1]["w"] > 100
        and toc[-1]["text"] == str(rendered.chapters[0]["first"])
    )


def test_layout_offsets_are_utf16_units():
    source = document(heading("h1", "الفصل"), para("p1", "𝕏 كلمة ", note("n1", "حاشية"), " بعد"))
    rendered = engine.get_engine().render(job(source, front_matter={"title_page": False, "contents": False}))
    line = next(line for page in rendered.layout for line in page["lines"] if line["block"] == "p1")
    call = next(run for run in line["runs"] if run["note"])
    js_text = "𝕏 كلمة ".encode("utf-16-le")
    assert call["start"] == len(js_text) // 2 and line["end"] == call["end"] + len(" بعد")


def ink_extent(pixels, scale: float, line: dict) -> tuple[float, float] | None:
    """The left and right edges (points) of the dark pixels inside a line's box (its line height holds its
    letters and their marks; the lines around it keep to their own boxes). `pixels`: a 2-D gray array."""
    top = max(0, int((line["y"] + 1) * scale))
    bottom = min(pixels.shape[0], int((line["y"] + line["h"] - 1) * scale))
    left = max(0, int((line["x"] - 12) * scale))
    right = min(pixels.shape[1], int((line["x"] + line["w"] + 12) * scale))
    columns = (pixels[top:bottom, left:right] < 110).any(axis=0).nonzero()[0]
    if not len(columns):
        return None
    return (left + columns[0]) / scale, (left + columns[-1] + 1) / scale


def test_the_layout_boxes_sit_on_the_pdf_text():
    """Every line of the layout covers the ink of its line on the rasterised PDF page, edge to edge."""
    rendered = engine.get_engine().render(job(rich_book(), running_header="chapter"))
    scale = 3
    checked = 0
    for page, pdf_page in zip(rendered.layout, pdf_pages(rendered.pdf), strict=True):
        pixmap = pdf_page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csGRAY)
        pixels = numpy.frombuffer(pixmap.samples, dtype=numpy.uint8).reshape(pixmap.height, pixmap.width)
        for line in page["lines"]:
            if line["kind"] in ("source", "separator", "note") or not line["runs"]:
                continue
            found = ink_extent(pixels, scale, line)
            assert found is not None, (page["n"], line["block"])
            left, right = found
            # (ink is not the advance box: an initial alef of Amiri reaches 2 pt past its box, a final
            # letter may stop short of it)
            assert left == pytest.approx(line["x"], abs=3), (page["n"], line["block"], left, line["x"])
            assert right == pytest.approx(line["x"] + line["w"], abs=3), (page["n"], line["block"], right)
            checked += 1
    assert checked > 60


def test_page_checks_find_what_a_typesetter_looks_for(tmp_path):
    def page(n, lines, rule=None, blank=False):
        return {
            "n": n,
            "side": "left" if n % 2 else "right",
            "blank": blank,
            "width_pt": 480,
            "height_pt": 680,
            "margins": {"top": 56, "bottom": 62, "left": 51, "right": 62},
            "lines": lines,
            "footnote_rule": rule,
        }

    def line(block, kind="body", y=100, h=20, runs=()):
        return {"block": block, "kind": kind, "y": y, "h": h, "start": 0, "end": 1, "runs": list(runs)}

    call = {"text": "(1)", "note": "n1", "sup": True, "start": 3, "end": 4}
    pages = [
        page(
            1,
            [line("h1", "heading"), line("p1", runs=[call]), line("n1", "note", y=600, h=30)],
            rule={"x": 51, "y": 590, "w": 360},
        ),
        page(2, [line("p1"), line("n1", "note", y=560), line("s2", "heading", y=520)]),
        page(3, [line("p2"), line("p3")]),
        page(4, [], blank=True),
        page(5, [line("h2", "heading"), line("p4")]),
    ]
    pages[1]["lines"] = [line("p1"), line("s2", "heading", y=520), line("n1", "note", y=560)]
    chapters = [
        {"id": "h1", "title": "الفصل الأول", "first": 1, "last": 4},
        {"id": "h2", "title": "الثاني", "first": 5, "last": 5},
    ]
    missing = [{"name": "Lotus", "fallback": "Amiri"}]
    checks = layout_module.page_checks(pages, chapters, missing)
    codes = [(check["code"], check["page"], check["block"]) for check in checks]
    assert ("missing_font", None, None) in codes
    assert ("footnote_overflow", 1, "p1") in codes  # into the bottom margin of page 1 and on over page 2
    assert ("heading_at_foot", 2, "s2") in codes
    assert ("almost_empty_page", 3, "p2") in codes  # the blank verso before the recto is skipped
    assert all(set(check) >= {"code", "page", "block", "message"} for check in checks)
    assert "سطران" in next(check["message"] for check in checks if check["code"] == "almost_empty_page")
    # a real render: a face that is not installed is reported with each render
    with override_settings(NASSAKH={**settings.NASSAKH, "FONT_DIRS": [str(tmp_path)]}):
        rendered = engine.get_engine().render(
            job(document(heading("h1", "الفصل"), para("p1", LOREM)), body_font="lotus")
        )
    assert [check["code"] for check in rendered.checks] == ["missing_font"]


def test_a_note_longer_than_a_page_is_reported():
    huge = "نص حاشية طويلة جدًّا لا تسعها صفحة واحدة مهما صغر خطها " * 120
    source = document(
        heading("h1", "الفصل"), para("p1", LOREM, note("n1", huge), " " + LOREM), para("p2", LOREM * 4)
    )
    rendered = engine.get_engine().render(job(source, front_matter={"title_page": False, "contents": False}))
    assert "footnote_overflow" in [check["code"] for check in rendered.checks]


# ====================================================================== D47: page breaks, widows, details


def test_block_page_break_attrs_and_the_stylesheet_line_rules():
    source = document(
        heading("h1", "الفصل"),
        para("p1", LOREM),
        para("p2", LOREM, breakBefore=True),
        para("p3", "قبل الجدول", keepWithNext=True),
        para("p4", LOREM),
    )
    model = book_model(source, {})
    flags = {block.id: (block.break_before, block.keep_with_next) for block in model.blocks()}
    assert flags["p2"] == (True, False) and flags["p3"] == (False, True) and flags["p1"] == (False, False)
    html = render_html(model)
    assert 'class="nk-body nk-break" id="b-p2"' in html and 'class="nk-body nk-keep" id="b-p3"' in html
    css = css_of()
    assert "orphans: 2; widows: 2;" in css and ".nk-break { break-before: page; }" in css
    assert css.index(".nk-keep { break-after: avoid; }") > css.index(".nk-section-title")
    assert "break-after: avoid; }" in css.split(".nk-chapter-title")[1].split("}")[0] + "}"
    loose = css_of(widows=3, orphans=4, keep_headings=False)
    assert "orphans: 4; widows: 3;" in loose and "margin: 16mm 0 9mm; break-after: auto;" in loose
    rendered = engine.get_engine().render(job(source, front_matter={"title_page": False, "contents": False}))
    page_of = {
        line["block"]: page["n"] for page in rendered.layout for line in page["lines"] if line["first"]
    }
    assert page_of["p2"] == page_of["p1"] + 1  # a new page before p2
    clamped = (page_setup({"widows": 0}).widows, page_setup({"orphans": 12}).orphans)
    assert clamped == (1, 9) and page_setup({"keep_headings": "yes"}).keep_headings is True  # not a bool


def test_book_details_print_a_title_page_and_a_copyright_page():
    fields = {
        "subtitle": "دراسة تاريخية",
        "editor": "محمد علي",
        "translator": "",
        "publisher": "دار نسّاخ",
        "city": "طرابلس",
        "year": "2026",
        "edition": "الأولى",
        "isbn": "978-9959-26-123-4",
        "rights": "جميع الحقوق محفوظة للناشر",
    }
    sheet = {"front_matter": {"title_page": True, "contents": True, "copyright_page": True, "fields": fields}}
    source = long_book(chapters=1, paragraphs=3)
    model = book_model(source, sheet, title="كتاب", author="المؤلف")
    assert model.front.title == "كتاب التجربة" and model.front.subtitle == "دراسة تاريخية"  # the title node's
    titled = book_model(source, {"front_matter": {"fields": {"title": "عنوان آخر"}}})
    assert titled.front.title == "عنوان آخر"
    rendered = engine.get_engine().render(job(source, **sheet))
    pages = rendered.layout
    title_page = [(line["block"], line["runs"][0]["text"]) for line in pages[0]["lines"]]
    assert title_page == [
        ("front-title", "كتاب التجربة"),
        ("front-subtitle", "دراسة تاريخية"),
        ("front-author", "مؤلف الكتاب"),
        ("front-editor", "تحقيق: محمد علي"),
        ("front-imprint", "دار نسّاخ، طرابلس، 2026"),
    ]
    copyright_page = " | ".join("".join(run["text"] for run in line["runs"]) for line in pages[1]["lines"])
    assert "الطبعة الأولى" in copyright_page and "ردمك: 978-9959-26-123-4" in copyright_page
    assert "جميع الحقوق محفوظة للناشر" in copyright_page and pages[1]["lines"][0]["kind"] == "copyright"
    assert pages[2]["lines"][0]["block"] == "front-contents-title" and rendered.chapters[0]["first"] == 4
    assert "ردمك" in pdf_pages(rendered.pdf)[1].get_text() or "ﺭﺩﻣﻚ" in pdf_pages(rendered.pdf)[1].get_text()


# ====================================================================== D47: live layout, re-layout

from publishing import relayout  # noqa: E402
from publishing.models import LiveLayout  # noqa: E402


def make_book(source: dict, **sheet) -> Book:
    from assembly.models import AssemblyRun

    book = Book.objects.create(title="كتاب الترتيب", author="المؤلف")
    run = AssemblyRun.objects.create(book=book, status="done")
    Manuscript.objects.create(book=book, document=source, version=1, run=run)
    if sheet:
        StyleSheet.objects.create(book=book, **sheet)
    return book


def edit(book: Book, change) -> None:
    """Change the manuscript in place (`change(document)`), one version up (as a save does)."""
    manuscript = Manuscript.objects.get(book=book)
    change(manuscript.document)
    manuscript.version += 1
    manuscript.save()


def full_layout(book: Book) -> list[dict]:
    """The whole book laid out now, from scratch (what the live layout must equal)."""
    job_ = preview.job_for(book, "book", None)
    return engine.get_engine().render(engine.RenderJob(**{**job_.__dict__, "pdf": False})).layout


def assert_same_pages(live: list[dict], full: list[dict]) -> None:
    """The spliced live layout reads like a fresh render: page numbers, sides, blank pages, every line's
    block, range and place, the call and marker numbers, the header and the page number."""
    assert [page["n"] for page in live] == [page["n"] for page in full]
    for mine, fresh in zip(live, full, strict=True):
        where = f"page {fresh['n']}"
        assert (mine["side"], mine["blank"]) == (fresh["side"], fresh["blank"]), where
        assert len(mine["lines"]) == len(fresh["lines"]), where
        for a, b in zip(mine["lines"], fresh["lines"], strict=True):
            assert (a["block"], a["kind"], a["start"], a["end"]) == (
                b["block"],
                b["kind"],
                b["start"],
                b["end"],
            ), where
            tolerance = 12 if a["kind"] == "contents" else 0.06
            assert a["x"] == pytest.approx(b["x"], abs=tolerance) and a["y"] == pytest.approx(
                b["y"], abs=0.06
            ), where
            assert [run["text"] for run in a["runs"]] == [run["text"] for run in b["runs"]], where
        for key in ("header", "number"):
            if fresh[key] is None:
                assert mine[key] is None, (where, key)
            else:
                assert mine[key]["text"] == fresh[key]["text"], (where, key)
                assert mine[key]["align"] == fresh[key]["align"], (where, key)
                # the side the text keeps (a renumbered page keeps the old number's box: «9» → «10»)
                assert anchor(mine[key]) == pytest.approx(anchor(fresh[key]), abs=0.6), (where, key)
        assert (mine["footnote_rule"] is None) == (fresh["footnote_rule"] is None), where


def anchor(item: dict) -> float:
    """Where a header or page number is held: its centre, or the edge it is aligned to."""
    if item["align"] == "left":
        return item["x"]
    if item["align"] == "right":
        return item["x"] + item["w"]
    return item["x"] + item["w"] / 2


def grow_chapter(book: Book, chapter_id: str, paragraphs: int) -> None:
    def change(document):
        content = document["content"]
        at = next(i for i, node in enumerate(content) if doc.node_id(node) == chapter_id) + 1
        for k in range(paragraphs):
            content.insert(at, para(f"g{chapter_id}{k}", LOREM + " " + LOREM, pages=(1,)))

    edit(book, change)


def chapter_pages_now(book: Book, chapter_id: str) -> int:
    job_ = preview.job_for(book, "chapter", chapter_id)
    return engine.get_engine().render(engine.RenderJob(**{**job_.__dict__, "pdf": False})).page_count


def odd_growth(book: Book, chapter_id: str) -> int:
    """Grow the chapter until its page count changed by an odd number; returns the change."""
    before = chapter_pages_now(book, chapter_id)
    for _step in range(8):
        grow_chapter(book, chapter_id, 3)
        change = chapter_pages_now(book, chapter_id) - before
        if change % 2:
            return change
    raise AssertionError("no odd growth")


@pytest.mark.parametrize("numbering", ["bottom_outer", "bottom_center"])
def test_a_chapter_relayout_shifts_the_later_chapters_and_swaps_their_sides(db, numbering):
    book = make_book(
        long_book(chapters=3, paragraphs=8, notes_every=3), page_number=numbering, running_header="chapter"
    )
    assert preview.render_preview(book, "book").status == "done"
    live = LiveLayout.objects.get(book=book)
    assert live.revision == 1 and live.page_count > 6 and live.path.endswith("/layout.json")
    before = {item["id"]: item for item in live.chapters}
    change = odd_growth(book, "h1")
    row = relayout.request_relayout(book, "h1")
    assert row.status == "done" and row.kind == "layout", row.error
    result = row.result
    assert result["mode"] == "chapter" and result["laid_out"] == ["h1"]
    assert result["from"] == before["h1"]["first"] and result["to"] == before["h2"]["first"] - 1
    assert result["delta"] == change and result["chapter_delta"] == change and result["flip"] is True
    assert result["shifted_from"] == before["h2"]["first"] and result["blank_changes"] == []
    assert result["revision"] == {"before": 1, "after": 2} and result["side_shift_pt"] == pytest.approx(
        -11.34, abs=0.01
    )
    after = {item["id"]: item for item in result["chapters"]}
    assert after["h3"]["first"] == before["h3"]["first"] + change
    live.refresh_from_db()
    assert live.revision == 2 and live.page_count == result["page_count"]
    assert_same_pages(relayout.live_pages(book.pk), full_layout(book))
    payload = relayout.relayout_payload(row)
    assert [page["n"] for page in payload["pages"]] == list(
        range(result["from"], result["from"] + result["count"])
    )


def test_with_recto_openings_the_blank_page_comes_and_goes_instead(db):
    book = make_book(long_book(chapters=3, paragraphs=8, notes_every=4), chapter_opening="recto")
    preview.render_preview(book, "book")
    before = {item["id"]: item for item in LiveLayout.objects.get(book=book).chapters}
    change = odd_growth(book, "h1")
    row = relayout.request_relayout(book, "h1")
    result = row.result
    assert result["chapter_delta"] == change and result["delta"] % 2 == 0 and result["flip"] is False
    kinds = {item["change"] for item in result["blank_changes"]}
    assert len(result["blank_changes"]) == 1 and kinds <= {"added", "removed"}
    after = {item["id"]: item for item in result["chapters"]}
    assert after["h2"]["first"] % 2 == 1 and after["h2"]["first"] == before["h2"]["first"] + result["delta"]
    assert_same_pages(relayout.live_pages(book.pk), full_layout(book))


def test_footnotes_are_renumbered_per_page_when_a_note_moves_to_the_next_page(db):
    book = make_book(long_book(chapters=2, paragraphs=14, notes_every=1))
    preview.render_preview(book, "book")
    old_pages = {
        note_id: page["n"]
        for page in relayout.live_pages(book.pk)
        for line in page["lines"]
        for note_id in [run["note"] for run in line["runs"] if run["note"] and run["sup"]]
    }

    def lengthen(document):  # a longer first paragraph pushes every later line (and call) down
        document["content"][2]["content"][0]["text"] = LOREM + " " + LOREM + " " + LOREM + " "

    edit(book, lengthen)
    row = relayout.request_relayout(book, "h1")
    assert row.status == "done"
    live = relayout.live_pages(book.pk)
    new_pages = {
        note_id: page["n"]
        for page in live
        for line in page["lines"]
        for note_id in [run["note"] for run in line["runs"] if run["note"] and run["sup"]]
    }
    moved = [note_id for note_id in old_pages if new_pages.get(note_id, 0) > old_pages[note_id]]
    assert moved  # some notes went to the next page
    for page in live:
        calls = [
            run["text"]
            for line in page["lines"]
            if line["kind"] != "note"
            for run in line["runs"]
            if run["sup"]
        ]
        markers = [
            line["runs"][0]["text"].strip("  ")
            for line in page["lines"]
            if line["kind"] == "note" and line["first"]
        ]
        assert calls == [f"({n})" for n in range(1, len(calls) + 1)] and markers == calls, page["n"]
    assert_same_pages(live, full_layout(book))
    assert row.passes == 1  # numbers as long as before, Amiri's digits all as wide: set without a 2nd pass


def test_a_book_without_chapter_breaks_is_laid_out_forward_until_it_converges(db):
    blocks = [para(f"p{n}", LOREM + " " + LOREM, pages=(n,)) for n in range(1, 71)]
    blocks[30]["attrs"]["breakBefore"] = True  # section 2 opens on a new page: the pages after it do not move
    book = make_book(document(*blocks))
    preview.render_preview(book, "book")
    old = relayout.live_pages(book.pk)
    ids = [c.id for c in doc.chapters_of(Manuscript.objects.get(book=book).document)]
    assert len(ids) == 3

    def lengthen(document):
        document["content"][5]["content"][0]["text"] += " " + LOREM

    edit(book, lengthen)
    row = relayout.request_relayout(book, ids[0])
    result = row.result
    assert row.status == "done" and result["mode"] == "sections" and result["converged"] is True
    assert result["to"] < old[-1]["n"] and result["windows"] >= 2
    assert_same_pages(relayout.live_pages(book.pk), full_layout(book))

    def shorten(document):  # no page break after section 3's start: forward to the end of the book
        document["content"][40]["content"][0]["text"] = "قصير."

    edit(book, shorten)
    row = relayout.request_relayout(book, ids[1])
    assert row.status == "done" and row.result["mode"] == "sections"
    assert_same_pages(relayout.live_pages(book.pk), full_layout(book))


def test_a_new_heading_lays_out_both_chapters_it_makes(db):
    book = make_book(long_book(chapters=2, paragraphs=10, notes_every=5))
    preview.render_preview(book, "book")

    def split(document):
        document["content"].insert(7, heading("hnew", "فصل جديد من الفصل الأول"))

    edit(book, split)
    row = relayout.request_relayout(book, "h1")
    assert row.result["full"] is True  # a new entry on the contents page: the front matter moves too
    assert [item["id"] for item in row.result["chapters"]] == ["h1", "hnew", "h2"]
    assert_same_pages(relayout.live_pages(book.pk), full_layout(book))
    StyleSheet.objects.update_or_create(book=book, defaults={"front_matter": {"contents": False}})
    preview.render_preview(book, "book")

    def split_again(document):
        document["content"].insert(4, heading("hnew2", "فصل آخر"))

    edit(book, split_again)
    row = relayout.request_relayout(book, "h1")  # no contents page: only the chapters the split made
    assert row.result["laid_out"] == ["h1", "hnew2"] and row.result["full"] is False
    assert [item["id"] for item in row.result["chapters"]] == ["h1", "hnew2", "hnew", "h2"]
    assert_same_pages(relayout.live_pages(book.pk), full_layout(book))


def test_nothing_to_do_when_the_live_layout_shows_the_chapter(db):
    book = make_book(long_book(chapters=2, paragraphs=4))
    with pytest.raises(relayout.LayoutNotFound):
        relayout.request_relayout(book, "zz")
    first = relayout.request_relayout(book, "h1")  # no live layout yet: the whole book, laid out
    assert first.status == "done" and first.result["full"] is True and first.result["mode"] == "book"
    assert LiveLayout.objects.get(book=book).page_count == first.result["page_count"]
    assert relayout.request_relayout(book, "h1") is None
    assert relayout.relayout_payload(None)["result"] == {"unchanged": True}


def test_the_relayout_task_has_its_own_queue_and_the_images_a_low_priority():
    from nassakh.celery import app

    route = app.amqp.router.route({}, "publishing.tasks.relayout_chapter")
    assert route["queue"].name == "layout"
    images = app.amqp.router.route({}, "publishing.tasks.render_layout_images")
    assert images["queue"].name == "default" and images.get("priority") == 9
    assert app.amqp.router.route({}, "publishing.tasks.render_book_preview")["queue"].name == "default"
    root = Path(settings.BASE_DIR)
    for name in ("Makefile", "Procfile"):
        assert "worker -Q default,layout" in (root / name).read_text(), name


def test_a_newer_request_supersedes_a_queued_one(db, monkeypatch):
    book = make_book(long_book(chapters=2, paragraphs=4))
    preview.render_preview(book, "book")
    sent, revoked = [], []
    monkeypatch.setattr(relayout, "_enqueue", lambda row: sent.append(row.pk))
    monkeypatch.setattr(preview, "revoke", lambda task_id: revoked.append(task_id))
    edit(book, lambda d: d["content"][2]["content"][0].update(text="نص أول"))
    first = relayout.request_relayout(book, "h1")
    PreviewRender.objects.filter(pk=first.pk).update(task_id="task-1")
    assert relayout.request_relayout(book, "h1").pk == first.pk  # the same request waits already
    edit(book, lambda d: d["content"][2]["content"][0].update(text="نص ثان"))
    second = relayout.request_relayout(book, "h1")
    first.refresh_from_db()
    assert first.status == "cancelled" and revoked == ["task-1"] and sent == [first.pk, second.pk]
    assert relayout.relayout_payload(first)["status"] == "superseded"
    assert relayout.run_relayout(first.pk).status == "cancelled"  # a revoked task that ran anyway: nothing
    done = relayout.run_relayout(second.pk)
    assert done.status == "done" and done.version == 3
    # a task that finds a newer manuscript with a newer request waiting leaves the work to it
    edit(book, lambda d: d["content"][2]["content"][0].update(text="نص ثالث"))
    third = relayout.request_relayout(book, "h1")
    edit(book, lambda d: d["content"][2]["content"][0].update(text="نص رابع"))
    fourth = relayout.request_relayout(book, "h1")
    PreviewRender.objects.filter(pk=third.pk).update(status="queued")
    assert relayout.run_relayout(third.pk).status == "cancelled"
    assert relayout.run_relayout(fourth.pk).status == "done"


def test_a_failed_relayout_is_reported_and_leaves_the_live_layout(db, monkeypatch):
    book = make_book(long_book(chapters=2, paragraphs=4))
    preview.render_preview(book, "book")
    before = LiveLayout.objects.get(book=book).revision
    edit(book, lambda d: d["content"][2]["content"][0].update(text="تغيير"))

    def boom(self, j, c=None):
        raise RuntimeError("layout exploded")

    monkeypatch.setattr(engine.WeasyPrintEngine, "render", boom)
    row = relayout.request_relayout(book, "h1")
    assert row.status == "error" and row.error.startswith(relayout.RELAYOUT_ERROR)
    assert relayout.relayout_payload(row)["error"] == relayout.RELAYOUT_ERROR
    assert LiveLayout.objects.get(book=book).revision == before


def test_the_layout_and_relayout_api(db):
    book = make_book(long_book(chapters=2, paragraphs=6))
    reader = logged(role_user("reader-l", "proofreader"))
    editor_client = logged(role_user("editor-l", "editor"))
    layout_url = reverse("api:preview_layout", args=[book.pk])
    assert reader.get(layout_url).status_code == 404  # no layout yet
    data = reader.get(reverse("api:preview", args=[book.pk])).json()  # queues (eager: renders) the book
    assert data["layout_url"] == layout_url and data["layout"]["revision"] == 1 and data["checks"] == []
    page_range = reader.get(f"{layout_url}?from=2&to=3").json()
    assert [page["n"] for page in page_range["pages"]] == [2, 3] and page_range["revision"] == 1
    assert (
        page_range["from"] == 2 and page_range["to"] == 3 and page_range["page_count"] == data["page_count"]
    )
    assert page_range["pages"][0]["url"].endswith("/page-0002.webp") and page_range["pages"][0]["lines"]
    assert (
        '"nk-body"' in page_range["font_css"]
        and "/static/fonts/amiri/Amiri-Regular.ttf" in page_range["font_css"]
    )
    assert (
        page_range["geometry"]["width_pt"] == pytest.approx(481.89, abs=0.01) and page_range["stale"] is False
    )
    assert reader.get(f"{layout_url}?from=x").status_code == 400
    assert reader.get(f"{layout_url}?scope=shelf").status_code == 400
    capped = reader.get(f"{layout_url}?from=1&to=999").json()
    assert capped["to"] == data["page_count"]
    url = reverse("api:relayout", args=[book.pk, "h2"])
    assert reader.post(url, "{}", content_type="application/json").status_code == 403
    unchanged = editor_client.post(url, "{}", content_type="application/json")
    assert unchanged.status_code == 200 and unchanged.json()["result"] == {"unchanged": True}
    edit(book, lambda d: d["content"][-1]["content"][0].update(text=LOREM + " " + LOREM))
    answer = editor_client.post(url, json.dumps({"version": 2}), content_type="application/json")
    assert answer.status_code == 200  # eager: done at once (202 while it waits in the queue)
    body = answer.json()
    assert body["status"] == "done" and body["result"]["mode"] == "chapter" and body["pages"]
    status_ = reader.get(body["url"] + "?wait=1").json()
    assert status_["id"] == body["id"] and status_["result"] == body["result"]
    assert status_["pages"][0]["url"] is not None  # the images followed (eager)
    assert reader.get(f"{layout_url}?render={body['id']}").json()["scope"] == "render"
    assert reader.get(reverse("api:relayout_status", args=[book.pk, 999999])).status_code == 404
    assert (
        editor_client.post(
            reverse("api:relayout", args=[book.pk, "zz"]), "{}", content_type="application/json"
        ).status_code
        == 404
    )
    live = reader.get(layout_url).json()
    assert live["revision"] == 2 and live["render"] is not None


def test_a_book_render_confirms_the_live_layout_unless_a_later_edit_is_live(db):
    book = make_book(long_book(chapters=2, paragraphs=4))
    first = preview.render_preview(book, "book")
    live = LiveLayout.objects.get(book=book)
    assert live.base_id == first.pk and live.manuscript_version == 1
    edit(book, lambda d: d["content"][2]["content"][0].update(text="تغيير أول"))
    relayout.request_relayout(book, "h1")
    live.refresh_from_db()
    assert (
        live.revision == 2
        and live.manuscript_version == 2
        and live.path.startswith(f"books/{book.pk}/layout/live-")
    )
    stale = PreviewRender.objects.create(
        book=book, scope="book", content_hash="old", status="done", version=1, folder=first.folder
    )
    assert relayout.adopt_book_render(stale) is False  # an older manuscript than the live layout
    confirmed = preview.render_preview(book, "book")
    live.refresh_from_db()
    assert (
        live.base_id == confirmed.pk and live.revision == 3 and live.path == f"{confirmed.folder}/layout.json"
    )
    assert preview.prune(book.pk, "book") >= 0 and media(confirmed.folder).is_dir()
    StyleSheet.objects.create(book=book, trim="a5", width_mm=148, height_mm=210)  # another page setup
    edit(book, lambda d: d["content"][2]["content"][0].update(text="تغيير ثان"))
    row = relayout.request_relayout(book, "h1")
    assert row.result["full"] is True  # never spliced into pages of another size
    assert relayout.live_pages(book.pk)[0]["width_pt"] == pytest.approx(148 * 72 / 25.4, abs=0.01)


def test_a_render_job_keeps_the_stylesheet_front_matter_and_heading_sizes(db):
    """`job_for` passes a `PageSetup`; read again field by field it used to lose the contents / title page
    flags and the heading scales (every book rendered its contents page and default heading sizes)."""
    book = make_book(
        long_book(chapters=2, paragraphs=3),
        front_matter={"title_page": False, "contents": False},
        heading_scale={"h1": 2.4, "h2": 1.1},
    )
    setup = preview.job_for(book, "book", None).stylesheet
    assert page_setup(setup) is setup and (setup.contents, setup.title_page, setup.h1_scale) == (
        False,
        False,
        2.4,
    )
    rendered = engine.get_engine().render(preview.job_for(book, "book", None))
    assert rendered.chapters[0]["first"] == 1  # no title page, no contents page
    title = next(line for line in rendered.layout[0]["lines"] if line["block"] == "h1")
    assert title["runs"][0]["size_pt"] == pytest.approx(13 * 2.4, abs=0.05)


def test_with_notes_numbered_through_the_book_a_new_note_lays_out_the_whole_book(db):
    book = make_book(long_book(chapters=2, paragraphs=6, notes_every=2), footnote_numbering="book")
    preview.render_preview(book, "book")
    edit(book, lambda d: d["content"][3]["content"].append(note("nnew", "حاشية جديدة")))
    row = relayout.request_relayout(book, "h1")
    assert row.result["full"] is True
    assert_same_pages(relayout.live_pages(book.pk), full_layout(book))
    edit(book, lambda d: d["content"][3]["content"][0].update(text="نص آخر " + LOREM))
    assert relayout.request_relayout(book, "h1").result["mode"] == "chapter"  # same notes: the chapter only


# ====================================================================== D47 review: fixes


@pytest.mark.parametrize("opening", ["any", "recto"])
def test_a_chapter_emptied_by_an_edit_leaves_no_page_behind(db, opening):
    """A chapter with nothing left to print has no page in the book; laid out alone, WeasyPrint still gives
    the empty document one blank page, which the splice must not keep."""
    book = make_book(
        long_book(chapters=3, paragraphs=4), chapter_opening=opening, front_matter={"contents": False}
    )
    preview.render_preview(book, "book")
    before = LiveLayout.objects.get(book=book).page_count

    def empty(document):
        chapter = doc.find_chapter(document, "h2")
        for node in chapter.nodes(document):
            node["content"] = []

    edit(book, empty)
    row = relayout.request_relayout(book, "h2")
    assert row.status == "done" and row.result["mode"] == "chapter" and row.result["count"] == 0
    assert "h2" not in [item["id"] for item in row.result["chapters"]]
    live = relayout.live_pages(book.pk)
    assert len(live) == row.result["page_count"] == before + row.result["delta"]
    assert_same_pages(live, full_layout(book))


def test_a_page_setup_changed_back_makes_its_cached_render_live_again(db):
    """The book page draws the live layout: after the trim goes back to one rendered before, the cached
    render of that trim (found by its hash, not rendered again) must be live again; while the setup
    differs from the live layout's, the layout says it is stale."""
    book = make_book(long_book(chapters=2, paragraphs=4), trim="b5")
    first = preview.request_preview(book, "book")
    b5 = 170 * 72 / 25.4
    assert relayout.live_pages(book.pk)[0]["width_pt"] == pytest.approx(b5, abs=0.01)
    StyleSheet.objects.filter(book=book).update(trim="a5", width_mm=148, height_mm=210)
    assert relayout.layout_payload(book)["stale"] is True  # another setup, its render not done yet
    assert preview.preview_payload(book, "book", enqueue=False)["layout"]["stale"] is True
    other = preview.request_preview(book, "book")
    assert other.pk != first.pk
    assert relayout.live_pages(book.pk)[0]["width_pt"] == pytest.approx(148 * 72 / 25.4, abs=0.01)
    revision = LiveLayout.objects.get(book=book).revision
    StyleSheet.objects.filter(book=book).update(trim="b5", width_mm=170, height_mm=240)
    assert preview.request_preview(book, "book").pk == first.pk  # cached: not rendered again
    live = LiveLayout.objects.get(book=book)
    assert live.base_id == first.pk and live.revision == revision + 1
    assert relayout.live_pages(book.pk)[0]["width_pt"] == pytest.approx(b5, abs=0.01)
    assert relayout.layout_payload(book)["stale"] is False
    preview.request_preview(book, "book")  # asked again: already live, no new revision
    assert LiveLayout.objects.get(book=book).revision == revision + 1


def test_asking_for_a_relayout_never_waits_for_the_live_layout_lock(db, monkeypatch):
    """A running re-layout holds the live layout row for its whole layout; a save asking for the next one
    must not wait for it (the book row serialises the requests)."""
    book = make_book(long_book(chapters=2, paragraphs=4))
    preview.render_preview(book, "book")
    edit(book, lambda d: d["content"][2]["content"][0].update(text="تغيير"))
    monkeypatch.setattr(relayout, "_enqueue", lambda row: None)

    def locked(*args, **kwargs):
        raise AssertionError("the live layout row was locked")

    monkeypatch.setattr(LiveLayout.objects, "select_for_update", locked)
    row = relayout.request_relayout(book, "h1")
    assert row.status == "queued"


def test_a_live_layout_row_without_a_layout_does_not_hide_the_book_renders_pages(db):
    """A re-layout that failed before its first layout leaves an empty live layout row; a chapter preview
    must still start at the chapter's page in the last book render (not at page 1)."""
    book = make_book(long_book(chapters=2, paragraphs=6))
    preview.render_preview(book, "book")
    first = {item["id"]: item["first"] for item in LiveLayout.objects.get(book=book).chapters}["h2"]
    LiveLayout.objects.filter(book=book).update(revision=0, path="", chapters=[])
    assert relayout.live_of(book.pk) is None and first > 1
    assert preview.chapter_first_page(book.pk, "h2") == first
