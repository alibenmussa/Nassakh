"""`manage.py research_eval`: measure the quotation checker (D107, CHALLENGE_SPEC §6), write the results page.

Quotations with a known truth are cut from the indexed text of `--books` (exact, typed the way people type,
a word replaced, dropped or swapped, a vowel changed, misattributed, built on a doubtful OCR reading) and from
`--absent-books` (not in the books). The real `research.services.verify_quote` answers them as a non-superuser
member of an account that holds exactly `--books`, beside a raw substring search (Ctrl+F), a search on the
normal form, and the checker with its doubt switched off. The account lives in a transaction that is rolled
back: the data is not changed (a stale index may be refreshed). Cases come from `random.Random(seed)`: the
same seed gives the same cases. Logic in `research.evaluation`, the page in `research.eval_report`.

    manage.py research_eval --books 29 31 41 --absent-books 33 34 35 36 37 38 --seeds 3 --n 60 \\
        --out docs/CHALLENGE_RESULTS.md --json /tmp/research_eval.json
"""

from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from books.models import Book
from research import eval_report, evaluation


class Command(BaseCommand):
    help = "Measure the quotation checker against a raw search and a normalised search; write the results."

    def add_arguments(self, parser) -> None:
        defaults = evaluation.Config()
        parser.add_argument(
            "--books", nargs="+", type=int, default=list(defaults.books), help="books under test"
        )
        parser.add_argument(
            "--absent-books",
            nargs="+",
            type=int,
            default=list(defaults.absent_books),
            help="other books whose text gives the quotations that are not in the books under test",
        )
        parser.add_argument("--seeds", type=int, default=defaults.seeds, help="seeds 1..N (default 3)")
        parser.add_argument("--n", type=int, default=defaults.n, help="cases per class per seed (default 60)")
        parser.add_argument("--out", default="docs/CHALLENGE_RESULTS.md", help="the results page (Markdown)")
        parser.add_argument("--json", default=None, help="also write every number as JSON to this path")
        parser.add_argument("--no-repeat", action="store_true", help="skip running the first seed twice")
        parser.add_argument(
            "--no-index-timing", action="store_true", help="skip the timed rebuild of the books' index"
        )

    def handle(self, *args, **options) -> None:
        books, absent = options["books"], options["absent_books"]
        if options["seeds"] < 1 or options["n"] < 1:
            raise CommandError("--seeds and --n must be at least 1.")
        if set(books) & set(absent):
            raise CommandError("--books and --absent-books must not share a book.")
        missing = sorted(
            set(books + absent) - set(Book.objects.filter(pk__in=books + absent).values_list("pk", flat=True))
        )
        if missing:
            raise CommandError(f"No book with id {', '.join(map(str, missing))}.")
        config = evaluation.Config(
            books=tuple(books),
            absent_books=tuple(absent),
            seeds=options["seeds"],
            n=options["n"],
            repeat=not options["no_repeat"],
            index_timing=not options["no_index_timing"],
        )
        report = evaluation.evaluate(config, log=self.stdout.write)
        if options["json"]:
            self._write(options["json"], json.dumps(report, ensure_ascii=False, indent=1) + "\n")
        self._write(options["out"], eval_report.render(report))
        self._summary(report)

    def _write(self, path: str, text: str) -> None:
        target = Path(path)
        if not target.is_absolute():
            target = Path(settings.BASE_DIR) / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        self.stdout.write(f"wrote {target}")

    def _summary(self, report: dict) -> None:
        rates = report["aggregate"]["rates"]
        lines = [
            ("altered, located", "altered", "located"),
            ("correct, accepted", "correct", "accepted"),
            ("orthography, accepted", "orthography", "accepted"),
            ("misattributed, caught", "misattributed", "caught"),
            ("absent, not found", "absent", "correct"),
            ("ocr doubt, accused", "ocr_doubt", "false_accusation"),
        ]
        for label, key, metric in lines:
            cells = [
                eval_report.pct(eval_report.rate(report, key, system, metric), False)
                for system in evaluation.SYSTEMS
            ]
            self.stdout.write(f"{label:24s} " + "  ".join(f"{c:>7s}" for c in cells))
        self.stdout.write("columns: " + ", ".join(evaluation.SYSTEMS))
        if report.get("repeatability"):
            repeat = report["repeatability"]
            self.stdout.write(
                f"repeat of seed {repeat['seed']}: {repeat['identical']}/{repeat['cases']} identical"
            )
        self.stdout.write(self.style.SUCCESS(f"{len(rates)} result groups."))
