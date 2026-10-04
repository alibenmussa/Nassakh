"""`manage.py research_reindex [book …]`: rebuild the search index (`research.PageText`, D107) of the given
books, or of every book with lines.

The index follows the lines by itself (`research.index`: OCR finalise, every review action, and a stamp
check before each search); this command builds it the first time (books read before D107) and after a
change of the normal form (`research.index.INDEX_VERSION`). It reads the lines only; it never changes them.
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError

from books.models import Book
from ocr.models import Line
from research import index
from research.models import PageText


class Command(BaseCommand):
    help = "Rebuild the search index of the given books (all books with lines when none is given)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("books", nargs="*", type=int, help="book ids (default: every book with lines)")

    def handle(self, *args, books=None, **options) -> None:
        ids = list(books or [])
        if ids:
            missing = sorted(set(ids) - set(Book.objects.filter(pk__in=ids).values_list("pk", flat=True)))
            if missing:
                raise CommandError(f"No book with id {', '.join(map(str, missing))}.")
        else:
            ids = sorted(set(Line.objects.values_list("page__book_id", flat=True).distinct()))
        total = 0
        for book_id in ids:
            started = time.monotonic()
            pages = index.reindex_book(book_id)
            words = sum(len(row.words) for row in PageText.objects.filter(book_id=book_id).only("words"))
            total += pages
            self.stdout.write(
                f"book {book_id}: {pages} page(s), {words} word(s) in {time.monotonic() - started:.1f}s"
            )
        self.stdout.write(self.style.SUCCESS(f"{total} page(s) indexed in {len(ids)} book(s)."))
