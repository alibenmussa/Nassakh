"""`manage.py rebuild_lines [--book ID] [--page N] [--dry-run]`: rebuild OCR lines from the stored runs.

Re-runs the line rescue (`ocr.services.rescue_lines`) on each page's stored Tesseract runs and
re-finalises its lines from the stored OCR runs; no OCR model is called. Only pages in `ocr_done`
whose lines are untouched OCR output are rebuilt (`ocr.services.rebuild_skip_reason`); reviewed or
edited pages are listed and skipped. Prints the counts before and after for every page. With
`--dry-run` everything happens in memory and nothing is written.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from books.models import Book, Page
from ocr import services


class Command(BaseCommand):
    help = "Rescue lines Tesseract dropped and rebuild the lines of OCR'd pages from their stored runs."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--book", type=int, help="only this book (id)")
        parser.add_argument("--page", type=int, help="only this page number of the book (needs --book)")
        parser.add_argument("--dry-run", action="store_true", help="compute and print, write nothing")

    def handle(self, *args, **options) -> None:
        book_id, number, dry = options.get("book"), options.get("page"), bool(options.get("dry_run"))
        if number is not None and book_id is None:
            raise CommandError("--page needs --book.")
        pages = Page.objects.order_by("book_id", "number")
        if book_id is not None:
            if not Book.objects.filter(pk=book_id).exists():
                raise CommandError(f"No book {book_id}.")
            pages = pages.filter(book_id=book_id)
        if number is not None:
            pages = pages.filter(number=number)
            if not pages.exists():
                raise CommandError(f"Book {book_id} has no page {number}.")

        mode = "dry run, nothing is written" if dry else "saving"
        self.stdout.write(f"rebuild_lines ({mode}); trailing = lines ending with 3+ words without a box")
        self.stdout.write("book  page  lines        trailing  rescued  merged")
        totals = {"pages": 0, "before": 0, "after": 0, "tb": 0, "ta": 0, "rescued": 0, "merged": 0}
        skipped: list[tuple[Page, str]] = []
        failed: list[tuple[Page, str]] = []
        for page in pages:
            reason = services.rebuild_skip_reason(page)
            if reason:
                skipped.append((page, reason))
                continue
            try:
                result = services.rebuild_page_lines(page, save=not dry)
            except services.OcrError as exc:
                failed.append((page, str(exc).splitlines()[0]))
                continue
            b, a = result.before, result.after
            self.stdout.write(
                f"{page.book_id:>4}  {page.number:>4}  {b.lines:>3} -> {a.lines:<3}  "
                f"{b.trailing:>2} -> {a.trailing:<2}  {result.rescued:>7}  {a.merged or 0:>6}"
            )
            totals["pages"] += 1
            totals["before"] += b.lines
            totals["after"] += a.lines
            totals["tb"] += b.trailing
            totals["ta"] += a.trailing
            totals["rescued"] += result.rescued
            totals["merged"] += int(bool(a.merged))

        self.stdout.write(
            f"{totals['pages']} page(s): lines {totals['before']} -> {totals['after']}, "
            f"trailing {totals['tb']} -> {totals['ta']}, {totals['rescued']} line(s) rescued, "
            f"{totals['merged']} page(s) flagged lines_merged"
        )
        if skipped:
            self.stdout.write(f"skipped {len(skipped)} page(s):")
            for page, reason in skipped:
                self.stdout.write(f"  book {page.book_id} page {page.number}: {reason}")
        for page, message in failed:
            self.stderr.write(f"  book {page.book_id} page {page.number} failed: {message}")
