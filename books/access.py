"""Who sees which book (D102): the books of the user's organisation, nothing else.

The rule:

- a signed-in user sees and acts on the books of the organisation of their `accounts.Membership`, and no
  other book;
- a user without a membership sees no book (the books home says why, `books:list`);
- a superuser sees every book, of every organisation;
- the roles (the global groups admin / editor / proofreader, `core.decorators`) keep their meaning inside
  that set: an editor edits the books of their organisation, a proofreader reviews them;
- a book without an organisation (its organisation was deleted) is the superusers' alone.

Every view and API that takes a book, a page, a line, a gap, an export or a render by id goes through this
module (`get_book_or_404`, `get_page_or_404`, `get_or_404`), every list starts from `books_for`, and
`/media/books/<id>/…` asks `may_access`. A row of another organisation's book answers 404 exactly like a
missing one (`Http404` with the same Arabic message; DRF turns it into `{"detail": …}`), so its existence is
not leaked. Management commands and Celery tasks are internal and stay unscoped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Model, QuerySet
from django.http import Http404

if TYPE_CHECKING:
    from books.models import Book, Page

BOOK_NOT_FOUND = "الكتاب غير موجود."
PAGE_NOT_FOUND = "الصفحة غير موجودة."
NOT_FOUND = "غير موجود."

__all__ = [
    "BOOK_NOT_FOUND",
    "NOT_FOUND",
    "PAGE_NOT_FOUND",
    "books_for",
    "get_book_or_404",
    "get_or_404",
    "get_page_or_404",
    "has_organization",
    "may_access",
    "pages_for",
    "scope",
]


def _organization_id(user) -> int | None:
    """The id of the organisation of the user's membership (None without one; no query for the organisation
    itself). Not for superusers, who see every book whatever their membership."""
    from accounts.services import _membership

    membership = _membership(user)
    return membership.organization_id if membership is not None else None


def has_organization(user) -> bool:
    """True when the user may see books at all: a superuser, or a member of an organisation."""
    if not getattr(user, "is_authenticated", False):
        return False
    return bool(user.is_superuser) or _organization_id(user) is not None


def scope(queryset: QuerySet, user, path: str = "") -> QuerySet:
    """`queryset` narrowed to the rows of the books `user` may access (see the module docstring).

    `path` leads from the queryset's model to its book: "" for `Book` itself, "book" for a page, an export,
    a render or anything with a `book` key, "page__book" for a line or a gap. The rule is a join to the
    user's membership in the same query (one membership per user: no row is repeated), so a scoped lookup
    costs no query of its own; a user without a membership matches nothing.
    """
    if not getattr(user, "is_authenticated", False) or user.pk is None:
        return queryset.none()
    if user.is_superuser:
        return queryset
    field = f"{path}__organization__memberships__user_id" if path else "organization__memberships__user_id"
    return queryset.filter(**{field: user.pk})


def books_for(user) -> QuerySet[Book]:
    """Every book the user may see and act on."""
    from books.models import Book

    return scope(Book.objects.all(), user)


def pages_for(user) -> QuerySet[Page]:
    """Every page of the books the user may see and act on."""
    from books.models import Page

    return scope(Page.objects.all(), user, "book")


def may_access(user, book: Book | int | None) -> bool:
    """True when the user may see and act on `book` (a `Book` or a book id)."""
    if book is None or not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    if isinstance(book, Model):
        organization_id = _organization_id(user)
        return organization_id is not None and book.organization_id == organization_id
    return books_for(user).filter(pk=book).exists()


def get_or_404(user, queryset: QuerySet, path: str, message: str = NOT_FOUND, **lookup) -> Model:
    """The one row of `queryset` matching `lookup` whose book the user may access, else 404 with `message`
    (another organisation's row answers exactly like a missing one)."""
    row = scope(queryset, user, path).filter(**lookup).first()
    if row is None:
        raise Http404(message)
    return row


def get_book_or_404(user, book_id, queryset: QuerySet | None = None) -> Book:
    """The book `book_id` when the user may access it, else 404 «الكتاب غير موجود.»."""
    from books.models import Book

    return get_or_404(
        user, queryset if queryset is not None else Book.objects.all(), "", BOOK_NOT_FOUND, pk=book_id
    )


def get_page_or_404(user, queryset: QuerySet | None = None, **lookup) -> Page:
    """The page matching `lookup` (`pk=…`, or `book_id=…, number=…`) when the user may access its book,
    else 404 «الصفحة غير موجودة.»; `queryset` defaults to the pages with their book."""
    from books.models import Page

    rows = queryset if queryset is not None else Page.objects.select_related("book")
    return get_or_404(user, rows, "book", PAGE_NOT_FOUND, **lookup)
