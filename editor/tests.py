"""Tests of the editor app's models (Phase 4: the manuscript written by assembly, and its snapshots)."""

from __future__ import annotations

from django.contrib import admin
from django.db import IntegrityError, transaction

import pytest

from assembly.models import AssemblyRun
from assembly.services import prune_snapshots
from books.models import Book
from editor.models import Manuscript, ManuscriptSnapshot


@pytest.fixture
def book(db):
    return Book.objects.create(title="كتاب المخطوطة")


def test_manuscript_defaults_and_one_per_book(book):
    manuscript = Manuscript.objects.create(book=book)
    assert manuscript.document == {} and manuscript.version == 0 and manuscript.origin == "assembly"
    assert manuscript.run is None and manuscript.updated_by is None
    assert book.manuscript == manuscript and str(manuscript) == f"manuscript of book {book.pk} v0"
    with pytest.raises(IntegrityError), transaction.atomic():
        Manuscript.objects.create(book=book)


def test_a_deleted_run_leaves_the_manuscript(book):
    run = AssemblyRun.objects.create(book=book, status="done")
    manuscript = Manuscript.objects.create(book=book, run=run, version=1)
    run.delete()
    manuscript.refresh_from_db()
    assert manuscript.run is None and manuscript.version == 1


def test_snapshots_are_newest_first_and_go_with_the_manuscript(book):
    manuscript = Manuscript.objects.create(book=book, version=3)
    old = ManuscriptSnapshot.objects.create(manuscript=manuscript, version=1, reason="reassembly", label="أ")
    new = ManuscriptSnapshot.objects.create(manuscript=manuscript, version=2, reason="manual", label="ب")
    assert list(manuscript.snapshots.all()) == [new, old]
    assert str(new) == f"snapshot v2 of manuscript {manuscript.pk}"
    book.delete()
    assert not ManuscriptSnapshot.objects.exists() and not Manuscript.objects.exists()


def test_prune_keeps_the_newest_reassembly_snapshots_and_every_manual_one(book):
    manuscript = Manuscript.objects.create(book=book)
    for version in range(5):
        ManuscriptSnapshot.objects.create(manuscript=manuscript, version=version, reason="reassembly")
    manual = ManuscriptSnapshot.objects.create(manuscript=manuscript, version=9, reason="manual")
    assert prune_snapshots(manuscript, keep=2) == 3
    kept = manuscript.snapshots.filter(reason="reassembly").values_list("version", flat=True)
    assert sorted(kept) == [3, 4] and ManuscriptSnapshot.objects.filter(pk=manual.pk).exists()
    assert prune_snapshots(manuscript, keep=2) == 0


def test_models_are_in_the_admin():
    assert admin.site.is_registered(Manuscript) and admin.site.is_registered(ManuscriptSnapshot)
    assert admin.site.is_registered(AssemblyRun)
