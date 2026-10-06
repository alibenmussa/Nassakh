"""`manage.py export_demo_bundle --books 29 31 41 [--out DIR] [--dry-run] [--force] [--include-pdf]`.

Writes everything the app shows for the given books into a directory (`data.json`, `manifest.json`,
`media/…`) that `manage.py import_demo_bundle` reads into another database (docs/challenge/DEMO_DATA.md). It only
reads the database and the media folder. The directory must be outside the repository: the bundle holds the
scans, which are copyrighted, and must never be committed.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from books import bundle


class Command(BaseCommand):
    help = "Export finished books (rows and files) to a bundle directory for import_demo_bundle."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--books", nargs="+", type=int, required=True, help="ids of the books to export")
        parser.add_argument(
            "--out",
            default=bundle.DEFAULT_OUT,
            help=f"the bundle's directory, outside the repository (default {bundle.DEFAULT_OUT})",
        )
        parser.add_argument("--dry-run", action="store_true", help="measure and print; write nothing")
        parser.add_argument("--force", action="store_true", help="replace an earlier bundle in --out")
        parser.add_argument(
            "--include-pdf",
            action="store_true",
            help="also carry each book's source PDF (left out by default)",
        )

    def handle(self, *args, **options) -> None:
        try:
            plan = bundle.export_bundle(
                options["books"],
                options["out"],
                dry_run=options["dry_run"],
                force=options["force"],
                include_pdf=options["include_pdf"],
                log=self.stdout.write,
            )
        except bundle.BundleError as error:
            raise CommandError(str(error)) from error
        files, size = plan.media_totals()
        verb = "would be written" if options["dry_run"] else "written"
        self.stdout.write(
            self.style.SUCCESS(
                f"{len(plan.books)} book(s), {sum(plan.counts().values())} row(s), {files} file(s), "
                f"{bundle._mb(size)} MB of media {verb}"
                + ("" if options["dry_run"] else f" to {bundle.check_out_dir(options['out'])}")
            )
        )
