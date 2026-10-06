"""`manage.py rebuild_lines [book …] [--report] [--book ID] [--page N] [--dry-run] [--include-edited]`.

Rebuilds OCR lines from the stored runs (D39): re-runs the line rescue (`ocr.services.rescue_lines`) on
each page's stored Tesseract runs and re-finalises its lines from the stored OCR runs, so flag policy v2
(D71: reasons, the vote), the groups and suggestions of words only the second model read (D72) and the
looped-prefix readings (D73) are recomputed; no OCR model is called (Kraken reads the printed lines for the
word boxes where no stored `boxes` run read the same lines, D92), and the numbers pass is scheduled again
(`finalize_page`). Only pages in `ocr_done` whose lines are untouched OCR output are rebuilt
(`ocr.services.rebuild_skip_reason`); reviewed or edited pages are listed and skipped. A book whose
manuscript was edited on the book page (`assembly.services.is_edited`) is refused unless
`--include-edited`: a rebuild there would flood the round trip with changes (§2.6). With `--dry-run`
everything happens in memory and nothing is written.

`--report` writes nothing: for every approved page it rebuilds the tokens the reviewer first saw and
prints, per book and in total, the flags, useful share and catch rate before 7b and under v2 (with and
without single-reader flags, and as 7b writes them), the suggestions 7b would make, and the pages with
zero flags but errors (`ocr.report`, docs/baseline/PHASE7_SPEC.md §2.4, §4.6). Without books it covers every
book with an approved page.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from books.models import Book, Page
from ocr import report, services


class Command(BaseCommand):
    help = "Rebuild the lines of OCR'd pages from their stored runs, or report flag policy v2 (--report)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("books", nargs="*", type=int, help="book ids (default: every book)")
        parser.add_argument("--book", type=int, help="only this book (id)")
        parser.add_argument("--page", type=int, help="only this page number of the book (needs one book)")
        parser.add_argument("--dry-run", action="store_true", help="compute and print, write nothing")
        parser.add_argument("--report", action="store_true", help="measure the flags on reviewed pages")
        parser.add_argument(
            "--include-edited", action="store_true", help="also rebuild books whose manuscript was edited"
        )
        parser.add_argument("--verbose-suggestions", action="store_true", help="list every suggestion")

    def handle(self, *args, **options) -> None:
        ids = list(options.get("books") or [])
        if options.get("book") is not None:
            ids.append(options["book"])
        missing = [pk for pk in ids if not Book.objects.filter(pk=pk).exists()]
        if missing:
            raise CommandError(f"No book {', '.join(map(str, missing))}.")
        if options.get("report"):
            self.report(ids, bool(options.get("verbose_suggestions")))
            return
        self.rebuild(
            ids, options.get("page"), bool(options.get("dry_run")), bool(options.get("include_edited"))
        )

    # ------------------------------------------------------------ the report (writes nothing)

    def report(self, ids: list[int], verbose: bool) -> None:
        if not ids:
            ids = sorted(
                set(Page.objects.filter(status__in=report.APPROVED).values_list("book_id", flat=True))
            )
        self.stdout.write(
            "rebuild_lines --report (nothing is written): approved pages as the reviewer first saw them "
            "(docs/baseline/PHASE7_SPEC.md §2.4)"
        )
        self.stdout.write(
            "useful = flags the reviewer acted on; caught = errors that were flagged; "
            "0-flag = pages without flags but with errors"
        )
        all_rows: list[report.Row] = []
        totals = report.Suggestions()
        for pk in ids:
            rows, failed = report.book_rows(pk)
            suggestions = report.book_suggestions(pk)
            all_rows.extend(rows)
            for key in ("pages", "groups", "gaps", "approved_groups", "approved_gaps"):
                setattr(totals, key, getattr(totals, key) + getattr(suggestions, key))
            pages = len({row.page for row in rows})
            note = f", {failed} page(s) unreadable" if failed else ""
            self.stdout.write(f"\nbook {pk}: {pages} approved page(s), {len(rows)} words{note}")
            self._scores(rows)
            self._suggestions(suggestions)
            if verbose:
                for number, kind, support, text in suggestions.items:
                    self.stdout.write(f"    p{number} {kind:5s} support {support:.2f}  «{text}»")
        self.stdout.write(f"\ntotal ({len(ids)} book(s)): {len(all_rows)} words")
        self._scores(all_rows)
        self._suggestions(totals)

    def _scores(self, rows: list[report.Row]) -> None:
        for policy in report.POLICIES:
            score = report.score(rows, policy)
            self.stdout.write(
                f"  {policy:14s} {score.flags:5d} flags, {score.precision:6.1%} useful, "
                f"{score.recall:6.1%} caught ({score.useful} of {score.errors} errors, "
                f"{score.missed} missed), 0-flag pages with errors {score.zero_pages_with_errors}"
            )
        unknown = report.score(rows, "today").unknown
        if unknown:
            self.stdout.write(
                f"  ({unknown} flagged word(s) left untouched on forced approvals are excluded)"
            )

    def _suggestions(self, suggestions: report.Suggestions) -> None:
        self.stdout.write(
            f"  suggestions on {suggestions.pages} read page(s): {suggestions.groups} group(s) in the text, "
            f"{suggestions.gaps} gap(s) offered (approved pages: {suggestions.approved_groups} / "
            f"{suggestions.approved_gaps})"
        )

    # ------------------------------------------------------------ the rebuild (D39, 7b)

    def rebuild(self, ids: list[int], number: int | None, dry: bool, include_edited: bool) -> None:
        from assembly.services import is_edited  # other app: lazy import

        if number is not None and len(ids) != 1:
            raise CommandError("--page needs --book (one book).")
        books = Book.objects.filter(pk__in=ids) if ids else Book.objects.all()
        edited = [book for book in books.order_by("pk") if is_edited(book)]
        if edited and not include_edited:
            names = ", ".join(f"{book.pk} «{book.title}»" for book in edited)
            if ids:
                raise CommandError(
                    f"The manuscript of book(s) {names} was edited on the book page; a rebuild would "
                    "change the text under it. Pass --include-edited to rebuild anyway."
                )
            self.stdout.write(
                f"skipping book(s) with an edited manuscript (--include-edited to add): {names}"
            )
        pages = Page.objects.order_by("book_id", "number")
        if ids:
            pages = pages.filter(book_id__in=ids)
        if edited and not include_edited:
            pages = pages.exclude(book_id__in=[book.pk for book in edited])
        if number is not None:
            pages = pages.filter(number=number)
            if not pages.exists():
                raise CommandError(f"Book {ids[0]} has no page {number}.")

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
