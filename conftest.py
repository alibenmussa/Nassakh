"""Fixtures shared by every suite.

`_books_belong_to_an_organisation` (autouse): a book a test creates without an organisation belongs to the
first organisation (created when there is none), as every book does outside the tests — the D98 migration
gave every older book one and `books.services.create_book` gives each new book its creator's. Who sees a
book follows its organisation (D102, `books.access`), so the suites' users join that organisation
explicitly (`accounts.testing.member`); a user who joins none sees no book. A book given an organisation
keeps it; a test of a book without one clears it after creating it (`Book.objects.filter(…).update(
organization=None)`).
"""

from __future__ import annotations

from django.db.models.signals import pre_save

import pytest

DISPATCH_UID = "tests-book-organisation"


def _first_organisation(sender, instance, raw: bool = False, **kwargs) -> None:
    if raw or instance.organization_id is not None or not instance._state.adding:
        return
    from accounts.services import default_organization

    instance.organization = default_organization()


@pytest.fixture(autouse=True)
def _books_belong_to_an_organisation():
    from books.models import Book

    pre_save.connect(_first_organisation, sender=Book, dispatch_uid=DISPATCH_UID)
    yield
    pre_save.disconnect(sender=Book, dispatch_uid=DISPATCH_UID)
