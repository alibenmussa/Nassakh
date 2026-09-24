"""`manage.py assemble <book_id> [--dry-run] [--show N]`: assemble a book synchronously (owner's smoke test).

Without `--dry-run` a run is created and executed in this process (it writes the manuscript and
moves reviewed pages to `assembled`, like the dashboard's convert button). With `--dry-run` the
pipeline runs in memory on the book as it is and nothing is written. Prints the stats, the seams,
the warnings and, with `--show N`, the first N blocks.
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError

from assembly import services
from assembly.models import AssemblyRun
from books.models import Book


class Command(BaseCommand):
    help = "Assemble a book into its manuscript and print the stats and warnings."

    def add_arguments(self, parser) -> None:
        parser.add_argument("book_id", type=int)
        parser.add_argument(
            "--dry-run", action="store_true", help="run the pipeline in memory, write nothing"
        )
        parser.add_argument("--show", type=int, default=0, metavar="N", help="print the first N blocks")

    def handle(self, *args, **options) -> None:
        book = Book.objects.filter(pk=options["book_id"]).first()
        if book is None:
            raise CommandError(f"No book {options['book_id']}")
        started = time.monotonic()
        if options["dry_run"]:
            result = services.preview(book)
            document, warnings, stats, seams = result.document, result.warnings, result.stats, result.seams
            self.stdout.write(f"book {book.pk} «{book.title}»: dry run in {time.monotonic() - started:.2f} s")
        else:
            run = AssemblyRun.objects.create(
                book=book, status=AssemblyRun.Status.QUEUED, settings=book.assembly_settings or {}
            )
            run = services.run_assembly(run.pk)
            if run.status != AssemblyRun.Status.DONE:
                raise CommandError(f"run {run.pk} failed: {run.error}")
            payload = services.manuscript_payload(book) or {}
            document, warnings, stats = payload["document"], run.warnings, run.stats
            seams = payload["seams"]
            self.stdout.write(
                f"book {book.pk} «{book.title}»: run {run.pk} done in {run.duration_ms} ms, "
                f"manuscript v{payload['version']}"
            )
        self.stdout.write("stats: " + ", ".join(f"{key}={value}" for key, value in stats.items()))
        for seam in seams:
            where = f"{seam['from_page']}→{seam['page']}"
            self.stdout.write(f"seam {where}: {seam['mode']} ({seam['decision']}, {seam['reason']})")
        self.stdout.write(f"warnings: {len(warnings)}")
        for warning in warnings:
            page = warning["page"] if warning["page"] is not None else "-"
            self.stdout.write(f"  [{warning['severity']}] {warning['code']} p{page}: {warning['message']}")
        for node in document.get("content", [])[1 : 1 + max(0, options["show"])]:
            self.stdout.write(f"{node['type']} {node['attrs'].get('id')} {node['attrs'].get('sourcePages')}:")
            self.stdout.write("  " + _inline_text(node.get("content", [])))


def _inline_text(nodes: list[dict]) -> str:
    """Readable text of inline nodes: footnotes as [n: text], page breaks as ⟨p N⟩, uncertain words ‹…›."""
    out = []
    for node in nodes:
        kind = node.get("type")
        if kind == "text":
            text = node.get("text", "")
            out.append(f"‹{text}›" if node.get("marks") else text)
        elif kind == "footnote":
            attrs = node.get("attrs", {})
            orphan = " orphan" if attrs.get("orphan") else ""
            out.append(f"[{attrs.get('number')}{orphan}: {_inline_text(node.get('content', []))}]")
        elif kind == "pageBreak":
            out.append(f"⟨p {node['attrs'].get('page')}⟩")
    return "".join(out)
