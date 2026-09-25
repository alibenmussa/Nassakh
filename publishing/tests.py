"""Tests of the publishing app (PHASE5_SPEC §6): the book model from a Phase 4 document (every node type),
the stylesheet's CSS (golden snippets), the font registry (missing files), WeasyPrint on small books (page
count, footnotes at the page foot numbered per page across pages — D46 —, running headers, recto openings,
mirrored margins), the page images, the cache by hash, cancel / revoke / restart, the tasks and the API."""

from __future__ import annotations

import json
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.test import Client, override_settings
from django.urls import reverse

import pymupdf
import pytest
from PIL import Image

from assembly import pipeline
from books.models import Book
from editor.models import Manuscript, StyleSheet
from publishing import engine, fonts, preview, tasks
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
    assert "@top-center { content: string(running, first-except);" in css
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
    assert '.nk-fn::footnote-call { content: "(" attr(data-n) ")";' in css
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
        '<h1 class="nk-chapter-title" id="b-h1" data-running="عنوان «الكتاب»">' in html
    )  # book mode: the title
    assert '<span class="nk-fn" id="fn-n1" data-n="1">' in html and "<b>غامق</b>" in html
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
    rendered = engine.get_engine().render(
        engine.RenderJob(
            document=long_book(chapters=2, paragraphs=3),
            stylesheet={},
            scope="chapter",
            chapter_id="h2",
            first_page=9,
        )
    )
    assert rendered.chapters == [
        {"id": "h2", "title": "الفصل 2", "first": 9, "last": 9 + rendered.page_count - 1}
    ]
    first = pdf_pages(rendered.pdf)[0]
    assert "9" in first.get_text().split() and "الفصل 1" not in "".join(
        p.get_text() for p in pdf_pages(rendered.pdf)
    )


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
    """The footnote numbers of a page: the calls in the text (set small) and the markers at its foot."""
    calls: list[int] = []
    markers: list[int] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                found = [int(n) for n in re.findall(r"[()]\s*(\d{1,2})\s*[()]", span["text"])]
                (calls if span["size"] < 8 else markers).extend(found)
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
