"""`manage.py export_book <book_id> --format docx|print_pdf|screen_pdf|epub [--kashida K] [--comments]
[--bleed-mm N] [--crop-marks] [--options JSON] [--out DIR|FILE]` (PHASE6_SPEC §6.5).

Exports the book now, in this process, from its current manuscript: no `Export` row, no queue, no worker.
The file goes to `--out` (a folder, where it takes its Arabic export name, or a file path; the current
folder by default). Prints the file's path, its size, the steps and every warning. Agents and the Word
harness use it; nothing is written to the database.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from books.models import Book
from publishing import exports
from publishing.exporters import FORMATS, NullProgress


class Command(BaseCommand):
    help = "Export a book into a file now (no row, no queue): Word, PDF for print or screen, EPUB."

    def add_arguments(self, parser):
        parser.add_argument("book_id", type=int)
        parser.add_argument("--format", required=True, choices=list(FORMATS))
        parser.add_argument("--kashida", choices=("none", "low", "medium", "high"), help="docx")
        parser.add_argument("--comments", action="store_true", help="docx: comments on the uncertain words")
        parser.add_argument("--bleed-mm", type=int, dest="bleed_mm", help="print_pdf: 0, 3 or 5")
        parser.add_argument("--crop-marks", action="store_true", dest="crop_marks", help="print_pdf")
        parser.add_argument("--options", default="", help="the options as JSON (merged under the flags)")
        parser.add_argument("--out", default=".", help="a folder (the export's own file name) or a file path")

    def _options(self, options: dict) -> dict:
        values: dict = {}
        if options["options"]:
            try:
                values = json.loads(options["options"])
            except ValueError as exc:
                raise CommandError(f"--options is not JSON: {exc}") from None
            if not isinstance(values, dict):
                raise CommandError("--options must be a JSON object")
        if options["kashida"]:
            values["kashida"] = options["kashida"]
        if options["comments"]:
            values["comments"] = True
        if options["bleed_mm"] is not None:
            values["bleed_mm"] = options["bleed_mm"]
        if options["crop_marks"]:
            values["crop_marks"] = True
        return values

    def handle(self, *args, **options):
        book = Book.objects.filter(pk=options["book_id"]).first()
        if book is None:
            raise CommandError(f"no book {options['book_id']}")
        format = options["format"]
        values = self._options(options)
        started = time.monotonic()
        steps: list[tuple[str, float]] = []
        progress = NullProgress(on_step=lambda step: steps.append((step, time.monotonic() - started)))
        try:
            job, result = exports.export_now(book, format, values, progress)
        except exports.ExportError as exc:
            details = "; ".join(f"{key}: {message}" for key, message in exc.errors.items())
            raise CommandError(f"{exc.detail} {details}".strip()) from None
        out = Path(options["out"]).expanduser()
        if out.suffix.lower() == FORMATS[format].extension:
            path = out
        else:
            path = out / exports.file_name(book.title, format, job.options, book.pk)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(result.data)
        seconds = time.monotonic() - started
        self.stdout.write(str(path.resolve()))
        self.stdout.write(
            f"{format} · {exports.size_text(len(result.data))} · {seconds:.1f} s"
            f" · options {json.dumps(job.options)} · manuscript v{job.manuscript_version}"
            + (f" · {result.page_count} pages" if result.page_count is not None else "")
        )
        if steps:
            self.stdout.write("steps: " + ", ".join(f"{step} {at:.1f}s" for step, at in steps))
        for warning in result.warnings:
            self.stdout.write(
                f"{warning.get('level', 'info')} {warning.get('code', '')}: {warning.get('message', '')}"
            )
