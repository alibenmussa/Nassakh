"""Tests of the 7c review fixes to «تصحيح في كل الكتاب» (`review.corrections`, D79): a suggestion (TextGap,
D72) after a word the fix corrects keeps its place, since the fix moves its `after_t` with the word."""

from __future__ import annotations

import pytest

from books.models import Book
from ocr.models import TextGap
from processing.models import Region
from review import corrections, services
from review.tests import make_line, make_page, role_user, tok


@pytest.fixture
def gapped(db):
    """A line «ذكر السعودي ثم السعودي كذا» with an open suggestion after word 1 (as finalize_page writes)."""
    book = Book.objects.create(title="ك", status=Book.Status.READY_FOR_REVIEW)
    page = make_page(book, 1, with_lines=False)
    body = Region.objects.create(page=page, kind="body", bbox=[0, 0, 100, 150], order=0)
    line = make_line(page, 0, body, [tok("ذكر"), tok("السعودي"), tok("ثم"), tok("السعودي"), tok("كذا")])
    gap = TextGap.objects.create(page=page, line=line, index=1, after_t="السعودي", text="رحمه الله")
    services.refresh_page_text(page)
    return book, line, gap


def test_a_fix_everywhere_moves_the_gap_anchor_with_the_word_it_corrects(gapped):
    book, line, gap = gapped
    user = role_user("fixer", "proofreader")
    pick = [{"line_id": line.pk, "index": 1, "t": "السعودي"}]  # the first «السعودي» only
    fixed = corrections.fix_everywhere(book, "السعودي", "المسعودي", pick, user)
    gap.refresh_from_db()
    assert fixed["applied"] == 1 and (gap.index, gap.after_t) == (1, "المسعودي")
    # review draws the ▏ after word 1 (its `index`): accepting it puts the words there, not after the other
    # «السعودي» that still reads the old `after_t`
    accepted, _payload = services.accept_gap(TextGap.objects.get(pk=gap.pk))
    assert accepted.text == "ذكر المسعودي رحمه الله ثم السعودي كذا"


def test_undoing_the_fix_puts_the_gap_anchor_back(gapped):
    book, line, gap = gapped
    user = role_user("fixer", "proofreader")
    fixed = corrections.fix_everywhere(
        book, "السعودي", "المسعودي", [{"line_id": line.pk, "index": 1, "t": "السعودي"}], user
    )
    corrections.undo_fix(book, fixed["batch"], user)
    gap.refresh_from_db()
    line.refresh_from_db()
    assert line.text == "ذكر السعودي ثم السعودي كذا" and (gap.index, gap.after_t) == (1, "السعودي")
