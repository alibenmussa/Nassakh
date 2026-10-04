"""`manage.py make_demo_account --email … --password … --books 29 31 41` (D106, docs/CHALLENGE_SPEC.md §1).

Creates (or brings up to date) the organisation «حساب التجربة» (kind organisation, unlimited) and its user
(the email is the login; active, the organisation's admin, in the `editor` group), then moves the books into
it:

- each book's organisation becomes the demo account (D102: only its members, and superusers, see them);
- an organisation face (D98) the moved books use moves with them when no book left behind uses it; one that
  other books still use stays, and the moved book falls back to Amiri with a notice as D98 does;
- a format template taken from a moved book (`source_book`) moves with it (renamed when the name is taken);
- the quota holds of their pages follow the book (the demo account is unlimited: never charged). The ledger's
  past entries stay with the account that paid for them.

Run once after D106 lands; running it again changes nothing that is already in place.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts import fonts as org_fonts
from accounts.models import Membership, Organization, OrganizationFont, QuotaHold, StyleTemplate
from core.decorators import ROLE_EDITOR

DEMO_NAME = "حساب التجربة"
FONT_ROLES = ("body_font", "latin_font", "heading_font")


class Command(BaseCommand):
    help = (
        "Create the demo account «حساب التجربة» (unlimited) and its user, and move the given books into it."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--email", required=True, help="the demo user's email (its login)")
        parser.add_argument("--password", required=True, help="the demo user's password")
        parser.add_argument("--books", nargs="+", type=int, required=True, help="ids of the books to move")
        parser.add_argument("--name", default=DEMO_NAME, help=f"the account's name (default «{DEMO_NAME}»)")

    def handle(self, *args, **options) -> None:
        from books.models import Book

        email = options["email"].strip().lower()
        if "@" not in email or len(email) > 150:
            raise CommandError("an email of at most 150 characters is needed")
        books = list(
            Book.objects.filter(pk__in=options["books"]).select_related("organization").order_by("pk")
        )
        missing = sorted(set(options["books"]) - {book.pk for book in books})
        if missing:
            raise CommandError(f"no book with id {', '.join(map(str, missing))}")

        with transaction.atomic():
            organization = self._account(options["name"])
            user = self._user(email, options["password"], organization)
            self.stdout.write(f"account {organization.pk} «{organization.name}», user {user.pk} <{email}>")
            for book in books:
                self._move(book, organization)

    # ------------------------------------------------------------------ the account and its user

    def _account(self, name: str) -> Organization:
        organization = Organization.objects.filter(name=name).order_by("id").first()
        if organization is None:
            return Organization.objects.create(name=name, kind=Organization.Kind.ORGANIZATION, unlimited=True)
        if not organization.unlimited or organization.kind != Organization.Kind.ORGANIZATION:
            organization.unlimited = True
            organization.kind = Organization.Kind.ORGANIZATION
            organization.save(update_fields=["unlimited", "kind"])
        return organization

    def _user(self, email: str, password: str, organization: Organization):
        model = get_user_model()
        user = model._default_manager.filter(email__iexact=email).order_by("id").first()
        if user is None:
            user = model._default_manager.filter(username__iexact=email).order_by("id").first()
        if user is None:
            user = model._default_manager.create_user(
                username=email, email=email, password=password, first_name=organization.name[:150]
            )
        else:
            user.set_password(password)
            user.is_active = True
            user.save(update_fields=["password", "is_active"])
        Membership.objects.update_or_create(
            user=user, defaults={"organization": organization, "role": Membership.Role.ADMIN}
        )
        user.groups.add(Group.objects.get_or_create(name=ROLE_EDITOR)[0])
        return user

    # ------------------------------------------------------------------ a book

    def _move(self, book, organization: Organization) -> None:
        moved_from = book.organization
        if book.organization_id != organization.pk:
            book.organization = organization
            book.save(update_fields=["organization"])
        QuotaHold.objects.filter(page__book=book).exclude(organization=organization).update(
            organization=organization
        )
        notes = [f"book {book.pk} «{book.title}»: from {moved_from.name if moved_from else 'no account'}"]
        notes += self._fonts(book, organization)
        notes += self._templates(book, organization)
        self.stdout.write(" · ".join(notes))

    def _fonts(self, book, organization: Organization) -> list[str]:
        from publishing.fonts import org_font_id

        sheet = getattr(book, "stylesheet", None)
        if sheet is None:
            return []
        notes = []
        for pk in sorted({org_font_id(getattr(sheet, role, "")) for role in FONT_ROLES} - {None}):
            font = OrganizationFont.objects.filter(pk=pk).first()
            if font is None or font.organization_id == organization.pk:
                continue
            others = org_fonts.books_using(font).exclude(organization=organization).exclude(pk=book.pk)
            if others.exists():
                notes.append(f"font «{font.name}» stays (other books use it): Amiri here")
                continue
            font.organization = organization
            font.save(update_fields=["organization"])
            notes.append(f"font «{font.name}» moved")
        return notes

    def _templates(self, book, organization: Organization) -> list[str]:
        notes = []
        for template in StyleTemplate.objects.filter(source_book=book).exclude(organization=organization):
            name = template.name
            taken = set(
                StyleTemplate.objects.filter(organization=organization).values_list("name", flat=True)
            )
            n = 2
            while name in taken:
                name = f"{template.name} ({n})"
                n += 1
            template.organization = organization
            template.name = name
            template.save(update_fields=["organization", "name"])
            notes.append(f"template «{name}» moved")
        return notes
