"""`manage.py read_numbers --book ID [--page N]`: the numbers pass (D50) on pages already OCR'd.

New pages get it by themselves after Qari (`ocr.numbers.schedule`); this runs it on a book's
finalised pages now, synchronously, and prints what Kraken read on each. The book's printed digits are
decided once (`ocr.numbers.book_style`); a book printed with Western digits is left alone. Reviewed
lines, resolved words and approved pages are never touched; numbers already read are not read again.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from books.models import Book, Page
from ocr import numbers
from ocr.engines.kraken import KrakenEngine, KrakenError


class Command(BaseCommand):
    help = "Read the Arabic-Indic numbers of a book's OCR'd pages with Kraken (the numbers pass, D50)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--book", type=int, required=True, help="the book (id)")
        parser.add_argument("--page", type=int, help="only this page number")

    def handle(self, *args, **options) -> None:
        book = Book.objects.filter(pk=options["book"]).first()
        if book is None:
            raise CommandError(f"No book {options['book']}.")
        engine = KrakenEngine()
        try:
            engine.load()
        except KrakenError as exc:
            raise CommandError(str(exc)) from exc
        pages = (
            book.pages.filter(text_state=Page.TextState.FINAL).select_related("preprocess").order_by("number")
        )
        if options.get("page") is not None:
            pages = pages.filter(number=options["page"])
        style = numbers.book_style(book)
        self.stdout.write(f"book {book.pk} «{book.title}»: prints {style or 'unknown'} digits")
        if style != numbers.ARABIC_INDIC:
            self.stdout.write(
                "nothing to read: the numbers pass is for books printed with Arabic-Indic digits"
            )
            return
        areas = applied = 0
        for page in pages:
            try:
                done = numbers.read_page_numbers(page, engine=engine, style=style)
            except KrakenError as exc:
                self.stderr.write(f"page {page.number}: {exc}")
                continue
            areas += done.areas
            applied += done.applied
            note = f" ({done.skipped})" if done.skipped else ""
            self.stdout.write(f"page {page.number}: {done.applied} of {done.areas} numbers read{note}")
        self.stdout.write(self.style.SUCCESS(f"done: Kraken read {applied} of {areas} number areas"))
