"""`manage.py review_changes <book_id> [--pages 3,5] [--dry-run]`: print the plan of «تغييرات المراجعة» (D78).

The owner's smoke test of the page-by-page merge. With `--dry-run` the comparison runs in memory and nothing
is written; without it a plan is stored as the book page would ask for it (`ChangesPlan`, run in this
process), so the book page's tab shows it. The manuscript itself is never changed here: an apply is the
owner's, from the book page.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from books.models import Book
from editor import services
from editor.models import Manuscript


class Command(BaseCommand):
    help = "Print the plan of review changes for an edited book (page by page, D78)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("book_id", type=int)
        parser.add_argument(
            "--pages", default="", help="comma-separated page numbers (default: every drift page)"
        )
        parser.add_argument("--dry-run", action="store_true", help="compare in memory, store no plan")
        parser.add_argument("--json", action="store_true", help="print the plan as JSON")

    def handle(self, *args, **options) -> None:
        book = Book.objects.filter(pk=options["book_id"]).first()
        if book is None:
            raise CommandError(f"No book {options['book_id']}")
        raw = [part.strip() for part in options["pages"].split(",") if part.strip()]
        if any(not part.isdigit() for part in raw):
            raise CommandError("--pages takes page numbers, e.g. 3,5")
        pages = [int(part) for part in raw] or None
        manuscript = Manuscript.objects.filter(book_id=book.pk).select_related("run").first()
        if manuscript is None:
            raise CommandError("The book has no manuscript yet.")
        if options["dry_run"]:
            plan, _results = services.compute_plan(book, manuscript, pages)
            header = f"book {book.pk} «{book.title}»: dry run, manuscript v{manuscript.version}"
        else:
            try:
                row = services.plan_review_changes(book, None, pages)
            except services.EditorError as exc:
                raise CommandError(str(exc)) from exc
            row.refresh_from_db()
            if row.status != "done":
                raise CommandError(f"plan {row.pk}: {row.status} {row.error}")
            plan = row.plan
            header = f"book {book.pk} «{book.title}»: plan {row.pk}, manuscript v{row.manuscript_version}"
        if options["json"]:
            public = {key: value for key, value in plan.items() if key != "sigs"}
            self.stdout.write(json.dumps(public, ensure_ascii=False, indent=1))
            return
        self.stdout.write(header)
        self.stdout.write(f"base: {plan['base']}; counts: {plan['counts']}; approvals: {plan['approvals']}")
        items = {item["id"]: item for item in plan["items"]}
        for row in plan["pages"]:
            self.stdout.write(
                f"p{row['number']} {row['reason']} «{row['chapter_title']}»: {len(row['items'])} items"
            )
            for item_id in row["items"]:
                item = items[item_id]
                diff = " ".join(f"[{op}] {text}" for op, text in item["diff"])
                self.stdout.write(
                    f"  {item_id} {item['kind']} ({item['chip']}) {item['block'] or '-'}: {diff}"
                )
