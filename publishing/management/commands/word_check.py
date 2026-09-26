"""`manage.py word_check <book_id> [--kashida K] [--comments] [--chapters] [--compat 14] [--no-word]`
and `manage.py word_check --calibration | --c12` (PHASE6_SPEC §11.5): the dev-only Word harness. macOS
with Microsoft Word; never in pytest.

Builds the book's Word file as the export does (the same page plan, the same checks), **validates it
before Word sees it** (an invalid file makes Word show an «unreadable content» dialog), copies it into
Word's own container folder (`~/Library/Containers/com.microsoft.Word/Data/Documents/nassakh/`, so Word
asks for no file access), refuses to run while Word has other documents open, quits Word (nothing to
save: the open files are the harness's unchanged copies), opens exactly one file, reads `compute
statistics` (pages, lines, lines with notes) and prints a table against the preview's layout: pages,
text lines and note lines, per chapter with `--chapters` (each chapter alone, `front_matter=False`),
with the §11.7 gate. Word's AppleScript cannot save, close or answer range queries on this Mac, so
there is no Word-made PDF and no page number per bookmark. The file stays open for looking.

`--calibration` writes `calibration.docx` (§11.6), opens it and prints the checklist; `--c12` measures
whether Word adds a paragraph's space after to the next one's space before (§5.4). `--compat 14`
builds in compatibility mode 14 (C0). Tell the owner before a run: Word opens on the screen.
"""

from __future__ import annotations

import math
import subprocess
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from books.models import Book
from publishing import exports
from publishing.exporters import parse_options
from publishing.word import schema
from publishing.word.calibration import CHECKLIST, c12_docx, c12_expected, calibration_docx
from publishing.word.exporter import DOCX_OPTIONS, build_book, live_plan
from publishing.word.options import WordOptions
from publishing.word.writer import PagePlan

BOX = Path.home() / "Library/Containers/com.microsoft.Word/Data/Documents/nassakh"
DEFAULT_OUT = "playground/word/out"
OPEN_TIMEOUT_S = 280
WORD_APP = "Microsoft Word"
REFUSE = "أغلق مستندات Word المفتوحة أولًا"


class Command(BaseCommand):
    help = "Build a book's Word file, open it in Word and compare its pages and lines with the preview."

    def add_arguments(self, parser):
        parser.add_argument("book_id", type=int, nargs="?")
        parser.add_argument("--kashida", choices=("none", "low", "medium", "high"), default="low")
        parser.add_argument("--comments", action="store_true")
        parser.add_argument("--chapters", action="store_true", help="each chapter alone (no front matter)")
        parser.add_argument(
            "--compat", type=int, choices=(14, 15), default=None, help="C0: compatibility mode"
        )
        parser.add_argument("--calibration", action="store_true", help="write calibration.docx and open it")
        parser.add_argument(
            "--c12", action="store_true", help="C12: does Word add space after to space before?"
        )
        parser.add_argument("--no-word", action="store_true", help="build and validate only")
        parser.add_argument("--out", default=DEFAULT_OUT, help="the folder for the files")

    # ------------------------------------------------------------------ entry

    def handle(self, *args, **options):
        out = Path(options["out"]).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        if options["calibration"]:
            return self.calibration(out, open_word=not options["no_word"])
        if options["c12"]:
            return self.c12(out, open_word=not options["no_word"])
        if options["book_id"] is None:
            raise CommandError("give a book id, or --calibration / --c12")
        book = Book.objects.filter(pk=options["book_id"]).first()
        if book is None:
            raise CommandError(f"no book {options['book_id']}")
        values = parse_options(DOCX_OPTIONS, {"kashida": options["kashida"], "comments": options["comments"]})
        word_options = WordOptions.parse(values)
        job, _inputs, _digest = exports.read_inputs(book, "docx", values)
        pages, chapters, source = self.preview_pages(job)
        plan = PagePlan.from_pages(pages, source, chapters)
        self.stdout.write(f"preview ({source}): {plan.page_count} pages")
        preview = self.counts(pages, chapters)
        readings = None
        if word_options.comments:
            from publishing.word.exporter import readings_of

            readings = readings_of(job)
        if options["chapters"]:
            return self.chapters(job, word_options, plan, preview, out, options, readings)
        result = build_book(job, word_options, plan=plan, readings=readings, compat_mode=options["compat"])
        suffix = f"-compat{options['compat']}" if options["compat"] else ""
        suffix += "-comments" if word_options.comments else ""
        path = out / f"book-{book.pk}-{word_options.kashida}{suffix}.docx"
        path.write_bytes(result.data)
        self.stdout.write(f"{path} · {len(result.data)} bytes · {result.stats.get('build_ms')} ms build")
        for warning in result.warnings:
            self.stdout.write(f"  {warning['level']} {warning['code']}: {warning['message']}")
        self.validate(result.data, path)
        if options["no_word"]:
            return
        stats = self.word_stats(path)
        self.report(preview["total"], stats, chapter=None)
        self.stdout.write("The file is open in Word for looking.")

    # ------------------------------------------------------------------ the preview

    def preview_pages(self, job) -> tuple[list[dict], list[dict], str]:
        """The preview's pages: the live layout when current for the exported text, else a render."""
        from publishing.engine import RenderJob, get_engine
        from publishing.relayout import live_of, live_pages

        if live_plan(job) is not None:
            live = live_of(job.book_id)
            return live_pages(job.book_id), list(live.chapters or []), "live"
        self.stdout.write("the live layout is not current: rendering the preview in memory (no writes)…")
        rendered = get_engine().render(
            RenderJob(
                document=job.document, stylesheet=job.setup, title=job.title, author=job.author, pdf=False
            )
        )
        return rendered.layout or [], rendered.chapters, "render"

    @staticmethod
    def counts(pages: list[dict], chapters: list[dict]) -> dict:
        """`{total: {pages, lines, note_lines}, chapters: {id: {pages, lines, note_lines, first, last}}}`."""

        def count(page_list):
            lines = notes = 0
            for page in page_list:
                for line in page.get("lines") or []:
                    kind = line.get("kind")
                    if kind == "note":
                        notes += 1
                    elif kind != "source":
                        lines += 1
            return {"pages": len(page_list), "lines": lines, "note_lines": notes}

        out = {"total": count(pages), "chapters": {}}
        for item in chapters:
            if not isinstance(item, dict) or not isinstance(item.get("first"), int):
                continue
            own = [p for p in pages if item["first"] <= p.get("n", 0) <= item.get("last", item["first"])]
            out["chapters"][item["id"]] = {**count(own), "first": item["first"], "last": item.get("last")}
        return out

    # ------------------------------------------------------------------ per chapter

    def chapters(self, job, word_options, plan, preview, out, options, readings) -> None:
        from editor import document as doc

        rows = []
        for chapter in doc.chapters_of(job.document):
            result = build_book(
                job,
                word_options,
                chapter_ids=[chapter.id],
                plan=plan,
                readings=readings,
                front_matter=False,
                compat_mode=options["compat"],
            )
            path = out / f"book-{job.book_id}-{chapter.id}.docx"
            path.write_bytes(result.data)
            self.validate(result.data, path)
            expected = preview["chapters"].get(chapter.id)
            if options["no_word"]:
                self.stdout.write(f"{chapter.id} «{chapter.title}»: {path}")
                continue
            stats = self.word_stats(path)
            rows.append((chapter, expected, stats))
            self.report(expected, stats, chapter=chapter)
        if rows:
            self.stdout.write("")
            self.stdout.write("chapter · preview pages · Word pages · Δ")
            for chapter, expected, stats in rows:
                pages = expected["pages"] if expected else None
                delta = (stats["pages"] - pages) if pages is not None else None
                flag = "" if delta is not None and abs(delta) <= 1 else "  ← more than ±1 page"
                self.stdout.write(
                    f"{chapter.id} «{chapter.title}» · {pages} · {stats['pages']} · {delta}{flag}"
                )

    # ------------------------------------------------------------------ reporting

    def report(self, expected: dict | None, stats: dict, *, chapter) -> None:
        label = f"chapter {chapter.id} «{chapter.title}»" if chapter is not None else "the book"
        self.stdout.write("")
        self.stdout.write(f"{label}: preview → Word")
        if expected is None:
            self.stdout.write(f"  Word: {stats}")
            return
        rows = [
            ("pages", expected["pages"], stats["pages"], stats["pages"] <= expected["pages"] + 2),
            (
                "text lines",
                expected["lines"],
                stats["lines"],
                _within(expected["lines"], stats["lines"], 0.005),
            ),
            (
                "note lines",
                expected["note_lines"],
                stats["note_lines"],
                _within(expected["note_lines"], stats["note_lines"], 0.01),
            ),
        ]
        if chapter is not None:
            rows[0] = (
                "pages",
                expected["pages"],
                stats["pages"],
                abs(stats["pages"] - expected["pages"]) <= 1,
            )
        for name, before, after, ok in rows:
            delta = after - before
            share = f" ({delta / before:+.2%})" if before else ""
            miss = "FAIL" if chapter is None or name == "pages" else "differs (not gated)"
            self.stdout.write(f"  {name}: {before} → {after} · Δ {delta:+d}{share} · {'ok' if ok else miss}")
        # §11.7 gates a chapter by its pages only: laid out alone, its notes fall on other pages
        gate = rows[0][3] if chapter is not None else all(ok for _n, _b, _a, ok in rows)
        self.stdout.write(f"  gate: {'PASS' if gate else 'FAIL'}")

    # ------------------------------------------------------------------ calibration

    def calibration(self, out: Path, *, open_word: bool) -> None:
        data = calibration_docx()
        path = out / "calibration.docx"
        path.write_bytes(data)
        self.stdout.write(f"{path} · {len(data)} bytes")
        self.validate(data, path)
        for code, title, expectation in CHECKLIST:
            self.stdout.write(f"  {code} {title}: {expectation}")
        self.stdout.write("  C7: disable Amiri in Font Book, reopen the file; the Arabic must stay in Amiri.")
        self.stdout.write(
            "  Record each result in docs/DECISIONS.md D62 and the constants of publishing/word/options.py."
        )
        if open_word:
            stats = self.word_stats(path)
            self.stdout.write(f"  Word: {stats['pages']} pages; the file is open for looking.")

    def c12(self, out: Path, *, open_word: bool) -> None:
        data = c12_docx()
        path = out / "c12.docx"
        path.write_bytes(data)
        self.validate(data, path)
        expected = c12_expected()
        height = expected["text_height_mm"]
        adds = math.ceil(expected["adds"] / height)
        larger = math.ceil(expected["max"] / height)
        self.stdout.write(
            f"{path}: {adds} pages if Word adds the two spacings, {larger} if it takes the larger"
        )
        if not open_word:
            return
        stats = self.word_stats(path)
        verdict = "adds them (SPACING_ADDS = True)" if abs(stats["pages"] - adds) <= 1 else "takes the larger"
        if abs(stats["pages"] - adds) > 1 and abs(stats["pages"] - larger) > 1:
            verdict = "neither rule fits; look at the file"
        self.stdout.write(f"  Word: {stats['pages']} pages → Word {verdict}")

    # ------------------------------------------------------------------ Word

    def validate(self, data: bytes, path: Path) -> None:
        errors = schema.check(data, schema=bool(settings.NASSAKH.get("EXPORT_VALIDATE", True)))
        if errors:
            for error in errors[:20]:
                self.stderr.write(f"  {error}")
            raise CommandError(f"{path} is not valid; Word must not open it")
        self.stdout.write("  schema and integrity: ok")

    def word_stats(self, path: Path) -> dict:
        """Quit Word (only the harness's own files may be open), open exactly `path`, read its statistics."""
        BOX.mkdir(parents=True, exist_ok=True)
        if _word_running():
            names = _open_documents()
            foreign = [name for name in names if not (BOX / name).exists()]
            if foreign:
                raise CommandError(f"{REFUSE}: {', '.join(foreign)}")
            _osascript(f'tell application "{WORD_APP}" to quit saving no', timeout=60)
            time.sleep(3)
        for lock in BOX.glob("~$*"):
            lock.unlink()
        target = BOX / path.name
        target.write_bytes(path.read_bytes())
        script = f"""
        with timeout of {OPEN_TIMEOUT_S} seconds
            set f to POSIX file "{target}" as alias
            tell application "{WORD_APP}"
                open f
                repeat 120 times
                    if (count of documents) > 0 then exit repeat
                    delay 0.5
                end repeat
                set d to active document
                set a to (compute statistics d statistic statistic pages) as string
                set b to (compute statistics d statistic statistic lines) as string
                set c to (compute statistics d statistic statistic lines ¬
                    include footnotes and endnotes true) as string
                return a & "," & b & "," & c
            end tell
        end timeout"""
        output = _osascript(script, timeout=OPEN_TIMEOUT_S + 20)
        pages, lines, with_notes = (int(value) for value in output.strip().split(","))
        return {"pages": pages, "lines": lines, "note_lines": with_notes - lines}


def _within(before: int, after: int, share: float) -> bool:
    if not before:
        return after == before
    return abs(after - before) / before <= share


def _word_running() -> bool:
    done = subprocess.run(["pgrep", "-x", WORD_APP], capture_output=True, text=True)
    return done.returncode == 0


def _open_documents() -> list[str]:
    output = _osascript(f'tell application "{WORD_APP}" to get name of every document', timeout=60)
    return [name.strip() for name in output.split(",") if name.strip()]


def _osascript(script: str, timeout: int) -> str:
    done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if done.returncode:
        raise CommandError(f"AppleScript failed: {done.stderr.strip()}")
    return done.stdout
