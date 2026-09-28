"""`manage.py read_calls --book ID [--page N] [--dry-run] [--sample DIR]`: the call pass (D83) on pages
already OCR'd.

New pages get it after the numbers pass (`ocr.tasks.read_numbers`); this runs it on a book's finalised
pages now, synchronously, and prints per page the wanted numbers and each candidate with Kraken's
reading and whether it was accepted. `--dry-run` writes nothing; `--sample DIR` saves the crop of every
accepted call as a PNG for a check by eye. Reviewed lines and approved pages are never touched.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from books.models import Book, Page
from ocr import calls, numbers
from ocr.engines.kraken import KrakenEngine, KrakenError


class Command(BaseCommand):
    help = "Read the raised footnote call marks of a book's OCR'd pages with Kraken (the call pass, D83)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--book", type=int, required=True, help="the book (id)")
        parser.add_argument("--page", type=int, help="only this page number")
        parser.add_argument("--dry-run", action="store_true", help="read and report, write nothing")
        parser.add_argument("--sample", help="folder for a PNG crop of every accepted call")

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
            self.stdout.write("nothing to read: the call pass is for books printed with Arabic-Indic digits")
            return
        candidates = accepted = applied = 0
        for page in pages:
            try:
                done = calls.read_page_calls(
                    page,
                    engine=engine,
                    style=style,
                    dry_run=options["dry_run"],
                    sample_dir=options.get("sample"),
                )
            except KrakenError as exc:
                self.stderr.write(f"page {page.number}: {exc}")
                continue
            if done.skipped or not done.wanted:
                continue
            candidates += len(done.candidates)
            accepted += done.accepted
            applied += done.applied
            self.stdout.write(
                f"page {page.number}: wanted {', '.join(done.wanted)}: {len(done.candidates)} candidates, "
                f"{done.accepted} accepted, {done.applied} written"
            )
            for cand in done.candidates:
                where = f"line {cand.line.order} {cand.source} {cand.bbox}"
                self.stdout.write(f"  {where}: read «{cand.reading}» → {cand.status}")
        verb = "would write" if options["dry_run"] else "wrote"
        self.stdout.write(
            self.style.SUCCESS(f"done: {candidates} candidates, {accepted} accepted, {verb} {applied} calls")
        )
