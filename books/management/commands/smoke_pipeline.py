"""`manage.py smoke_pipeline <pdf>`: run a real PDF through the whole pipeline in-process (RUNBOOK §8).

Creates a book with `books.services.create_book`, calls `start_processing` with Celery in eager mode
(every task runs inline, with the real engines configured in `.env`) and prints one row per page.
"""

from __future__ import annotations

import time
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError

from books import services
from books.models import Book


class Command(BaseCommand):
    help = "Run a PDF through ingest → preprocess → guides → layout → OCR in-process and print the result."

    def add_arguments(self, parser) -> None:
        parser.add_argument("pdf", help="path of the PDF to process")
        parser.add_argument("--title", default="اختبار الدخان")
        parser.add_argument("--pages-per-sheet", type=int, choices=(1, 2), default=1)
        parser.add_argument("--skip-first", type=int, default=0)
        parser.add_argument("--skip-last", type=int, default=0)

    def handle(self, *args, **options) -> None:
        if not settings.CELERY_TASK_ALWAYS_EAGER:
            raise CommandError("Run with CELERY_TASK_ALWAYS_EAGER=true so every task runs in this process.")
        path = Path(options["pdf"])
        if not path.is_file():
            raise CommandError(f"No such file: {path}")

        data = {
            "title": options["title"],
            "pages_per_sheet": options["pages_per_sheet"],
            "skip_first": options["skip_first"],
            "skip_last": options["skip_last"],
        }
        started = time.monotonic()
        with path.open("rb") as handle:
            book = services.create_book(data, File(handle, name=path.name), None)
        if book.status != Book.Status.ERROR:
            services.start_processing(book)
        book.refresh_from_db()
        elapsed = time.monotonic() - started

        self.stdout.write(f"book {book.pk}: {book.status} in {elapsed:.0f} s  {book.error_message}".rstrip())
        self.stdout.write("page  status        text         lines  runs  chars  flags / error")
        for page in book.pages.order_by("number"):
            note = ", ".join(page.attention_flags or []) or page.error_message.split("\n")[0]
            self.stdout.write(
                f"{page.number:>4}  {page.status:<12}  {page.text_state:<11}  {page.lines.count():>5}"
                f"  {page.ocr_runs.count():>4}  {len(page.final_text):>5}  {note}"
            )
