"""`manage.py import_demo_bundle --bundle DIR --email LOGIN [--password PW] [--org-name NAME] [--replace]
[--dry-run]`.

Reads a bundle made by `export_demo_bundle` into this database as the books of the demo account
(`books.bundle`, docs/challenge/DEMO_DATA.md): the account (an organisation, unlimited) and its user (the email is the
login; the account's admin, in the `editor` group) are created or found; every row gets a fresh primary key;
the files go to `MEDIA_ROOT/books/<new id>/`; the search index of the new books is built at the end. All
in one transaction, with the files removed again when anything fails. A book already in the account (same
title and source page count) is skipped, or with `--replace` deleted first. `--dry-run` runs the import and
rolls it back. The password is taken from the command line only; it is never printed or stored in a file.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from books import bundle


class Command(BaseCommand):
    help = "Import a bundle from export_demo_bundle as the books of the demo account."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--bundle", required=True, help="the bundle's directory")
        parser.add_argument("--email", required=True, help="the demo user's email (its login)")
        parser.add_argument(
            "--password",
            default=None,
            help="the user's password (needed to create the user; an existing one keeps its own)",
        )
        parser.add_argument(
            "--org-name",
            default=bundle.DEMO_ACCOUNT_NAME,
            help=f"the account's name (default «{bundle.DEMO_ACCOUNT_NAME}»)",
        )
        parser.add_argument(
            "--replace", action="store_true", help="delete a copy that is already there and import again"
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="run the import and roll it back; write no file"
        )

    def handle(self, *args, **options) -> None:
        try:
            report = bundle.import_bundle(
                options["bundle"],
                email=options["email"],
                password=options["password"],
                organization_name=options["org_name"],
                replace=options["replace"],
                dry_run=options["dry_run"],
                log=self.stdout.write,
            )
        except bundle.BundleError as error:
            raise CommandError(str(error)) from error
        except Exception as error:
            raise CommandError(
                "The import failed and was rolled back (no row and no file was kept): "
                f"{type(error).__name__}: {error}"
            ) from error
        bundle.log_import(report, self.stdout.write)
        made = len(report.created_books())
        if report.dry_run:
            self.stdout.write(
                self.style.WARNING(f"dry run: {made} book(s) would be imported; nothing was kept.")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"{made} book(s) imported, {len(report.books) - made} skipped.")
            )
