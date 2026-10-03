"""Snapshots without twins (the owner's review, 2026-10-03, item 21: "autosave saves several versions at almost the
same time"). What did: every re-assembly took a «قبل إعادة التجميع» copy, and the manuscript screen's role marks
re-assemble the book at each click (book 29: ten copies within 22 seconds). Now:

- one checkpoint per burst of re-assemblies of an unedited text (the first stands for the burst), the next one
  after `CHECKPOINT_WINDOW_S` of quiet; an edited text is still kept at every run, for good
- the main version once, at the first edit (`manual`, never pruned), no twin of a copy taken a moment before
- «حفظ نسخة الآن» twice for the same version and name answers the first copy"""

from __future__ import annotations

import json
from datetime import timedelta

from django.utils import timezone

import pytest

from assembly import services as assembly_services
from editor import services
from editor.models import Manuscript, ManuscriptSnapshot
from editor.tests import assembled_book, editor_user, logged  # noqa: F401 - editor_user is a fixture

pytestmark = pytest.mark.django_db


def _rows(book) -> list[tuple[str, int, str]]:
    return list(
        ManuscriptSnapshot.objects.filter(manuscript__book=book)
        .order_by("created_at", "id")
        .values_list("reason", "version", "label")
    )


def _age(book, seconds: float) -> None:
    ManuscriptSnapshot.objects.filter(manuscript__book=book).update(
        created_at=timezone.now() - timedelta(seconds=seconds)
    )


def test_a_burst_of_reassemblies_keeps_one_checkpoint(editor_user):
    pages, _ = assembled_book(editor_user)
    book = pages.book
    for _ in range(4):  # four role marks in a row on the manuscript screen
        assembly_services.start_assembly(book, editor_user)
    assert Manuscript.objects.get(book=book).version == 5
    # the text before the burst, once
    assert _rows(book) == [("reassembly", 1, "قبل إعادة التجميع · الإصدار 1")]
    # after the quiet, the next burst gets its own
    _age(book, services.CHECKPOINT_WINDOW_S + 5)
    assembly_services.start_assembly(book, editor_user)
    assembly_services.start_assembly(book, editor_user)
    assert [row[:2] for row in _rows(book)] == [("reassembly", 1), ("reassembly", 5)]


def test_an_edited_text_is_kept_at_every_reassembly(editor_user):
    pages, _ = assembled_book(editor_user)
    book = pages.book
    assembly_services.start_assembly(book, editor_user)  # a checkpoint of version 1, a moment ago
    first = Manuscript.objects.get(book=book).chapters()[0].id
    chapter = services.chapter_document(book, first)
    chapter["content"]["content"][1]["content"][0]["text"] = "نص محرَّر."
    services.save_chapter(book, first, chapter["content"], chapter["version"], editor_user)
    assembly_services.start_assembly(book, editor_user, replace_edited=True)
    reasons = [row[0] for row in _rows(book)]
    kept = ManuscriptSnapshot.objects.get(manuscript__book=book, label__startswith="النص المحرَّر قبل إعادة التجميع")
    assert reasons.count("manual") == 2 and "نص محرَّر." in json.dumps(kept.document, ensure_ascii=False)


def test_the_first_edit_keeps_the_main_version_once(editor_user):
    pages, _ = assembled_book(editor_user)
    book = pages.book
    assembled = Manuscript.objects.get(book=book)
    first = assembled.chapters()[0].id
    for text in ("أول تعديل.", "ثاني تعديل."):
        chapter = services.chapter_document(book, first)
        chapter["content"]["content"][1]["content"][0]["text"] = text
        services.save_chapter(book, first, chapter["content"], chapter["version"], editor_user)
    assert _rows(book) == [("manual", 1, "النص قبل أول تحرير · الإصدار 1")]
    main = ManuscriptSnapshot.objects.get(manuscript__book=book)
    # the assembled text, its own base (a restore leaves an edited text with a base)
    assert main.document == assembled.document and main.base == assembled.document
    assert main.created_by == editor_user
    assert services.snapshots(book)[0]["reason_label"] == "يدوية"


def test_a_first_edit_by_replace_all_keeps_its_own_copy_for_good(editor_user):
    pages, _ = assembled_book(editor_user)
    book = pages.book
    first = Manuscript.objects.get(book=book).chapters()[0].id
    result = services.find_replace(book, first, "الفصل", "الباب", replace=True, user=editor_user)
    assert result["replaced"] >= 1
    # the copy the undo toast restores is the main version: no twin, never pruned
    assert _rows(book) == [("manual", 1, "قبل استبدال «الفصل» بـ«الباب»")]
    assert ManuscriptSnapshot.objects.get(manuscript__book=book).pk == result["snapshot"]


def test_save_a_version_twice_answers_the_first_copy(editor_user):
    pages, _ = assembled_book(editor_user)
    book = pages.book
    client = logged(editor_user)
    url = f"/api/books/{book.pk}/snapshots/"
    one = client.post(url, json.dumps({"label": "قبل المراجعة"}), content_type="application/json")
    two = client.post(url, json.dumps({"label": "قبل المراجعة"}), content_type="application/json")
    assert one.status_code == two.status_code == 201 and one.json()["id"] == two.json()["id"]
    other = services.snapshot(book, "اسم آخر", "manual", editor_user)
    unnamed = services.snapshot(book, "", "manual", editor_user)
    again = services.snapshot(book, "", "manual", editor_user)
    assert unnamed["id"] == again["id"] and len({one.json()["id"], other["id"], unnamed["id"]}) == 3
    # an old copy is no twin
    _age(book, services.TWIN_WINDOW_S + 5)
    later = services.snapshot(book, "قبل المراجعة", "manual", editor_user)
    assert later["id"] != one.json()["id"]
