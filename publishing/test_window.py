"""A long chapter is laid out again only around an edit (the owner's review, 2026-10-03, item 6: "render only
the current page or its neighbours; optimisation matters"): book 41 has a chapter of 1159 paragraphs, about
120 pages, and every pause in typing laid all of them out again (3–4 s; 0.2 s now for most edits).
`relayout._window` lays out from the page before the edit a few pages at a time until a page opens with the
same line as before; the live layout and each book render record the text their pages show (`blocks`), so
the edit's span is known. Every result is compared with the whole book laid out from scratch
(`assert_same_pages`)."""

from __future__ import annotations

import pytest

from editor import document as doc
from editor.models import Manuscript
from publishing import preview, relayout
from publishing.models import LiveLayout
from publishing.tests import (
    LOREM,
    assert_same_pages,
    document,
    edit,
    full_layout,
    heading,
    make_book,
    note,
    para,
)

pytestmark = pytest.mark.django_db


def long_chapter(paragraphs: int = 90, notes_every: int = 7) -> dict:
    """A short opening chapter, one long chapter (about 16 pages: paragraphs of three to seven lines, a note
    every seventh, pages 7 and 14 of it opening with a paragraph by «ابدأ صفحة جديدة») and a closing one."""
    blocks = [heading("h1", "الفصل الأول", pages=(1,)), para("a1", LOREM, pages=(1,))]
    blocks.append(heading("h2", "الفصل الطويل", pages=(2,)))
    for n in range(1, paragraphs + 1):
        content: list = [LOREM + " " + (LOREM if n % 3 == 0 else "")]
        if n % notes_every == 0:
            content.append(note(f"n{n}", f"حاشية رقم {n}", number=n, page=2 + n // 4))
        attrs = {"breakBefore": True} if n in (35, 70) else {}
        blocks.append(para(f"p{n}", *content, pages=(2 + n // 4,), **attrs))
    blocks += [heading("h3", "الفصل الأخير", pages=(30,)), para("z1", LOREM, pages=(30,))]
    return document(*blocks)


def _block(document_: dict, block_id: str) -> dict:
    return next(node for node in document_["content"] if doc.node_id(node) == block_id)


def test_a_book_render_records_the_text_its_pages_show(db):
    book = make_book(long_chapter(paragraphs=12))
    preview.render_preview(book, "book")
    live = LiveLayout.objects.get(book=book)
    data = relayout.read_json(live.path)
    current = Manuscript.objects.get(book=book).document
    assert data["blocks"] == relayout.block_digests(current)
    assert [row[0] for row in data["blocks"]["h2"]] == ["h2", *[f"p{n}" for n in range(1, 13)]]


def test_an_edit_inside_a_long_chapter_lays_out_only_the_pages_around_it(db):
    book = make_book(long_chapter(), running_header="chapter")
    preview.render_preview(book, "book")
    before = {item["id"]: item for item in LiveLayout.objects.get(book=book).chapters}
    span = before["h2"]["last"] - before["h2"]["first"] + 1
    assert span >= 15

    def lengthen(document_):  # a paragraph in the middle of the long chapter grows by a sentence
        _block(document_, "p40")["content"][0]["text"] += " وزيادة في آخر الفقرة تملأ سطرًا آخر أو سطرين."

    edit(book, lengthen)
    row = relayout.request_relayout(book, "h2")
    result = row.result
    assert row.status == "done" and result["mode"] == "window" and result["converged"] is True, result
    assert result["rendered_pages"] < span / 2 and result["from"] > before["h2"]["first"]
    assert result["laid_out"] == ["h2"] and result["to"] < before["h2"]["last"]
    assert_same_pages(relayout.live_pages(book.pk), full_layout(book))
    # the record follows: a second edit further on is a window too
    data = relayout.read_json(LiveLayout.objects.get(book=book).path)
    assert data["blocks"]["h2"] == relayout.block_digests(Manuscript.objects.get(book=book).document)["h2"]

    def shorten(document_):  # a paragraph near the end of the chapter loses most of its text
        _block(document_, "p75")["content"][0]["text"] = "فقرة قصيرة."

    edit(book, shorten)
    row = relayout.request_relayout(book, "h2")
    assert row.status == "done" and row.result["mode"] == "window", row.result
    assert_same_pages(relayout.live_pages(book.pk), full_layout(book))
    # the window's page images: the same start and stop, the pages it made
    relayout.render_layout_images(row.pk)
    row.refresh_from_db()
    assert row.images is True


def test_a_block_added_or_removed_in_a_long_chapter_and_a_new_page_break(db):
    book = make_book(long_chapter())
    preview.render_preview(book, "book")

    def add_and_remove(document_):
        content = document_["content"]
        at = content.index(_block(document_, "p30"))
        content.insert(at + 1, para("pnew", LOREM + " " + LOREM, pages=(7,)))
        content.remove(_block(document_, "p33"))

    edit(book, add_and_remove)
    row = relayout.request_relayout(book, "h2")
    assert row.status == "done" and row.result["mode"] == "window", row.result
    assert_same_pages(relayout.live_pages(book.pk), full_layout(book))

    def page_break(document_):  # «ابدأ صفحة جديدة» on a paragraph: an attribute, no text changed
        _block(document_, "p60")["attrs"]["breakBefore"] = True

    edit(book, page_break)
    row = relayout.request_relayout(book, "h2")
    assert row.status == "done" and row.result["mode"] in ("window", "chapter"), row.result
    assert_same_pages(relayout.live_pages(book.pk), full_layout(book))


def test_the_whole_chapter_is_laid_out_when_a_window_cannot_be(db):
    # notes numbered through the chapter: every later number may move, the chapter is laid out whole
    book = make_book(long_chapter(), footnote_numbering="chapter")
    preview.render_preview(book, "book")

    def lengthen(document_):
        _block(document_, "p30")["content"][0]["text"] += " " + LOREM

    edit(book, lengthen)
    row = relayout.request_relayout(book, "h2")
    assert row.status == "done" and row.result["mode"] == "chapter"
    assert_same_pages(relayout.live_pages(book.pk), full_layout(book))
    # a live layout without a record (an older one): the whole chapter, then the record is there
    other = make_book(long_chapter())
    preview.render_preview(other, "book")
    live = LiveLayout.objects.get(book=other)
    data = relayout.read_json(live.path)
    data.pop("blocks")
    preview.write_json(relayout._abs(live.path), data)
    edit(other, lengthen)
    row = relayout.request_relayout(other, "h2")
    assert row.result["mode"] == "chapter"
    assert "h2" in relayout.read_json(LiveLayout.objects.get(book=other).path)["blocks"]
    assert_same_pages(relayout.live_pages(other.pk), full_layout(other))


# ---------------------------------------------------------------- a heading's edit: the front matter alone


def test_a_new_chapter_title_lays_out_its_chapters_and_the_front_matter_only(db):
    book = make_book(long_chapter(), running_header="chapter")
    preview.render_preview(book, "book")

    def split(document_):  # a paragraph of the long chapter made a chapter title: two chapters now
        node = _block(document_, "p40")
        node.update(type="heading", attrs={**node["attrs"], "level": 1})

    edit(book, split)
    row = relayout.request_relayout(book, "h2")
    result = row.result
    assert row.status == "done" and result["mode"] == "front" and result["full"] is True, result
    assert result["laid_out"] == ["h2", "p40"] and result["body"]["mode"] == "chapter"
    assert [item["id"] for item in result["chapters"]] == ["h1", "h2", "p40", "h3"]
    assert_same_pages(relayout.live_pages(book.pk), full_layout(book))

    def section(document_):  # a section title inside the long chapter: a window and the contents
        node = _block(document_, "p60")
        node.update(type="heading", attrs={**node["attrs"], "level": 2})

    edit(book, section)
    row = relayout.request_relayout(book, "p40")
    result = row.result
    assert result["mode"] == "front" and result["body"]["mode"] == "window", result
    assert_same_pages(relayout.live_pages(book.pk), full_layout(book))

    def rename(document_):  # a chapter title renamed: its entry on the contents page
        _block(document_, "h3")["content"][0]["text"] = "الفصل الأخير بعنوان أطول من قبل"

    edit(book, rename)
    row = relayout.request_relayout(book, "h3")
    assert row.result["mode"] == "front" and row.result["front_shift"] == 0
    assert_same_pages(relayout.live_pages(book.pk), full_layout(book))


@pytest.mark.parametrize("opening", ["any", "recto"])
def test_a_contents_page_that_grows_moves_every_page_after_it(db, opening):
    blocks = []
    for n in range(1, 25):
        blocks.append(heading(f"c{n}", f"الفصل {n} وعنوانه طويل بعض الطول", pages=(n,)))
        blocks.append(para(f"t{n}", LOREM, pages=(n,)))
    book = make_book(document(*blocks), chapter_opening=opening)
    preview.render_preview(book, "book")
    shifted = None
    for n in range(1, 25):  # a section title in each chapter until the contents take one page more

        def add(document_, n=n):
            content = document_["content"]
            at = content.index(_block(document_, f"t{n}"))
            content.insert(at, heading(f"s{n}", f"قسم {n} من الفصل {n}", level=2, pages=(n,)))

        edit(book, add)
        row = relayout.request_relayout(book, f"c{n}")
        assert row.status == "done", row.error
        if row.result.get("front_shift") or row.result["mode"] == "book":
            shifted = row.result
            break
    assert shifted is not None
    if opening == "any":
        # one page more in front: every later page one on, its side swapped
        assert shifted["mode"] == "front" and shifted["front_shift"] == 1
    else:
        # with recto openings the body starts on a recto: the front grows by two pages (its blank page comes
        # or goes), or by an odd count that moves every chapter's blank page (then the whole book)
        assert (shifted["mode"] == "front" and shifted["front_shift"] % 2 == 0) or shifted["mode"] == "book"
    assert_same_pages(relayout.live_pages(book.pk), full_layout(book))


def test_a_section_title_in_a_book_without_chapter_breaks(db):
    blocks = [para(f"p{n}", LOREM + " " + LOREM, pages=(n,)) for n in range(1, 61)]
    book = make_book(document(*blocks))
    preview.render_preview(book, "book")
    ids = [c.id for c in doc.chapters_of(Manuscript.objects.get(book=book).document)]

    def titled(document_):  # a paragraph made a section title: the first entry of the contents page
        node = _block(document_, "p30")
        node.update(type="heading", attrs={**node["attrs"], "level": 2})

    edit(book, titled)
    current = Manuscript.objects.get(book=book).document
    section = next(
        c.id for c in doc.chapters_of(current) if "p30" in [doc.node_id(node) for node in c.nodes(current)]
    )
    assert section in ids  # a section title is no chapter break: the sections stay
    row = relayout.request_relayout(book, section)
    assert row.status == "done" and row.result["mode"] == "front", row.result
    assert row.result["body"]["mode"] == "sections"
    assert_same_pages(relayout.live_pages(book.pk), full_layout(book))
