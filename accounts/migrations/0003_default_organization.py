"""D98: every book and every user that already exists joins one organisation.

On a database with books or users, the first organisation is created (named `NASSAKH["ORGANIZATION_NAME"]`,
«المؤسسة» by default; it is renamed on its page), every book without one is given it, and every user without
a membership joins it: superusers and members of the `admin` group as organisation admins, the others as
members. An empty database (the tests) gets nothing: the first book or sign-in creates it
(`accounts.services.default_organization`).
"""

from django.conf import settings
from django.db import migrations

DEFAULT_NAME = "المؤسسة"


def join(apps, schema_editor):
    Organization = apps.get_model("accounts", "Organization")
    Membership = apps.get_model("accounts", "Membership")
    Book = apps.get_model("books", "Book")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    if not Book.objects.exists() and not User.objects.exists():
        return
    organization = Organization.objects.order_by("id").first()
    if organization is None:
        name = (getattr(settings, "NASSAKH", {}) or {}).get("ORGANIZATION_NAME") or DEFAULT_NAME
        organization = Organization.objects.create(name=name)
    Book.objects.filter(organization__isnull=True).update(organization=organization)
    for user in User.objects.filter(membership__isnull=True):
        admin = user.is_superuser or user.groups.filter(name="admin").exists()
        Membership.objects.create(organization=organization, user=user, role="admin" if admin else "member")


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_organization_fonts_templates"),
        ("books", "0010_book_organization"),
    ]

    operations = [
        migrations.RunPython(join, migrations.RunPython.noop),
    ]
