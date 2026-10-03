"""Test helpers of the organisation rule (D102): a user sees only the books of their organisation, so a test
user joins one (`member`) and a test book belongs to one (`owned`). Both default to the first organisation
(`accounts.services.default_organization`, created when there is none), the one of a single publisher.
Not a test module (pytest collects `tests.py` and `test_*.py` only)."""

from __future__ import annotations


def member(user, organization=None, role: str = "member"):
    """`user` belongs to `organization` (the first one when None), as `role` (`member` | `admin`); the user
    is returned. Joining again moves the membership (one organisation per user)."""
    from accounts.models import Membership
    from accounts.services import default_organization

    organization = organization or default_organization()
    Membership.objects.update_or_create(user=user, defaults={"organization": organization, "role": role})
    if hasattr(user, "_state"):
        user._state.fields_cache.pop("membership", None)  # the next `user.membership` reads the new row
    return user


def owned(book, organization=None):
    """`book` belongs to `organization` (the first one when None); the book is returned."""
    from accounts.services import default_organization

    organization = organization or default_organization()
    if book.organization_id != organization.pk:
        book.organization = organization
        type(book).objects.filter(pk=book.pk).update(organization=organization)
    return book
