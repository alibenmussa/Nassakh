"""The search index (D107, `research.index`): words mapped to their lines and tokens, their states, the kinds,
and the index following the lines (OCR finalise, review actions, the stamp check)."""

from __future__ import annotations

import pytest

from ocr.models import Line, TextGap
from research import index
from research.conftest import make_page
from research.models import PageText
from review import services as review

pytestmark = pytest.mark.django_db


@pytest.fixture
def page(library):
    return library["pages"][0]


def _row(page, kind="body") -> PageText:
    return PageText.objects.get(page=page, kind=kind)


def test_words_map_back_to_their_line_and_token(page):
    index.reindex_page(page)
    row = _row(page)
    first = Line.objects.filter(page=page).order_by("order").first()
    assert row.words[0] == {"line": first.pk, "i": 0, "text": "حدثنا", "norm": "حدثنا", "state": "unreviewed"}
    assert row.norm.split(" ") == [w["norm"] for w in row.words]  # one normal form per word, in order
    vowelled = next(w for w in row.words if w["norm"] == "الاعمال")
    assert vowelled["text"] == "الأَعْمَالُ"  # the raw word keeps its diacritics


def test_body_and_notes_are_apart_and_a_footnote_mark_is_no_word(page):
    index.reindex_page(page)
    notes = _row(page, "notes")
    assert notes.norm.startswith("هذا الحديث رواه البخاري")  # «(١)» dropped
    assert "هذا" not in _row(page).norm.split(" ")


def test_states_from_the_token_flags_and_the_line(library):
    book = library["book"]
    page = make_page(book, 20, ["كلمة~ مراجعة^ عادية"])
    index.reindex_page(page)
    states = {w["norm"]: w["state"] for w in _row(page).words}
    assert states == {"كلمه": "doubtful", "مراجعه": "reviewed", "عاديه": "unreviewed"}
    Line.objects.filter(page=page).update(is_reviewed=True)
    index.reindex_page(page)
    states = {w["norm"]: w["state"] for w in _row(page).words}
    # an approved page: confident words are reviewed; an open word stays doubtful (approved with it open)
    assert states == {"كلمه": "doubtful", "مراجعه": "reviewed", "عاديه": "reviewed"}


@pytest.mark.parametrize(
    ("token", "line_reviewed", "state"),
    [
        ({"t": "x", "conf": "low"}, False, "doubtful"),
        ({"t": "x", "conf": "low", "pick": "vote"}, False, "doubtful"),  # the vote's reading, still open
        ({"t": "x", "conf": "low", "res": "chooser"}, False, "doubtful"),
        ({"t": "x", "conf": "low", "res": "secondary"}, False, "reviewed"),
        ({"t": "x", "conf": "high", "res": "typed"}, False, "reviewed"),
        ({"t": "x", "conf": "low", "res": "words"}, False, "unreviewed"),  # a year the words settled
        ({"t": "x", "conf": "high"}, False, "unreviewed"),
        ({"t": "x", "conf": "high"}, True, "reviewed"),
        ({"t": "x", "conf": "low"}, True, "doubtful"),
    ],
)
def test_token_state(token, line_reviewed, state):
    assert index.token_state(token, line_reviewed) == state


def test_an_open_suggestion_marks_the_word_it_follows(page):
    line = Line.objects.filter(page=page).order_by("order").first()
    TextGap.objects.create(page=page, line=line, index=1, after_t="عبد", text="الله")
    index.reindex_page(page)
    marked = [w for w in _row(page).words if w.get("gap")]
    assert [(w["line"], w["i"]) for w in marked] == [(line.pk, 1)]


def test_review_actions_keep_the_index_current(page, reader):
    index.reindex_page(page)
    line = Line.objects.filter(page=page).order_by("order").first()
    review.edit_line(line, "حدثنا عبد الله بن يوسف قال أنبأنا مالك عن نافع", user=reader)
    assert "انبانا" in _row(page).norm.split(" ")
    review.approve_page(page, reader, force=True)
    assert {w["state"] for w in _row(page).words} == {"reviewed"}
    review.reopen_page(page, reader)
    assert "unreviewed" in {w["state"] for w in _row(page).words}


def test_a_stale_row_is_rebuilt_before_a_search(page, reader):
    index.reindex_book(page.book_id)
    assert index.refresh_stale([page.book_id]) == 0
    # a change behind the hooks' back (a bulk update): the stamp no longer matches
    changed = [{"t": "تغيير", "conf": "high"}]
    Line.objects.filter(page=page, order=0).update(text="x", tokens=changed, is_reviewed=True)
    assert index.refresh_stale([page.book_id]) == 1
    assert _row(page).words[0]["norm"] == "تغيير"
    assert index.refresh_stale([page.book_id]) == 0  # nothing left to do


def test_a_page_without_lines_has_no_row(page):
    index.reindex_page(page)
    Line.objects.filter(page=page).delete()
    index.refresh_stale([page.book_id])
    assert not PageText.objects.filter(page=page).exists()


def test_reindex_book_and_the_command(library, capsys):
    from django.core.management import call_command

    call_command("research_reindex", str(library["book"].pk))
    assert PageText.objects.filter(book=library["book"], kind="body").count() == 6
    assert "6 page(s)" in capsys.readouterr().out


def test_an_index_failure_never_breaks_a_review_action(page, reader, monkeypatch):
    def boom(_page):
        raise RuntimeError("index down")

    monkeypatch.setattr(index, "reindex_page", boom)
    line = Line.objects.filter(page=page).order_by("order").first()
    review.edit_line(line, "حدثنا عبد الله", user=reader)  # no exception
    assert Line.objects.get(pk=line.pk).text == "حدثنا عبد الله"
