"""`manage.py release_page_runs --book ID [--page N] | --all`: give back pages' run claims (`books.runs`).

A run claim keeps a second run off a page while its chain is queued or running and is given back by the
chain's last task (or its errback). A chain whose queue messages were lost (a purged Redis queue, workers
reset by hand) leaves its claim until `NASSAKH["RUN_CLAIM_HOURS"]` pass; this frees those pages at once.
Only do it when no worker is still working on them: a task of the released run that arrives later skips
a page once a new run holds it, but one that is already running finishes its work. The pages' quota holds
(D106: pages promised to a run, not read yet) are released with the claims.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from accounts import billing
from books.models import Page


class Command(BaseCommand):
    help = "Release the pipeline run claims of a book's pages (or of every page) so a new run can start."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--book", type=int, help="the book whose pages are released")
        parser.add_argument("--page", type=int, help="only this page number of --book")
        parser.add_argument("--all", action="store_true", help="every page of every book")

    def handle(self, *args, book=None, page=None, all=False, **options) -> None:  # noqa: A002 - the flag's name
        if not all and book is None:
            raise CommandError("Give --book ID (optionally --page N) or --all.")
        pages = Page.objects.exclude(run_token="")
        if not all:
            pages = pages.filter(book_id=book)
            if page is not None:
                pages = pages.filter(number=page)
        held = list(pages.order_by("book_id", "number").values_list("book_id", "number"))
        billing.release_pages(pages.values_list("pk", flat=True))
        released = pages.update(run_token="", run_claimed_at=None)
        for book_id, number in held:
            self.stdout.write(f"book {book_id} page {number}: released")
        self.stdout.write(self.style.SUCCESS(f"{released} page run claim(s) released."))
