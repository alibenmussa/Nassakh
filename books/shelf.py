"""The books home, «الكتب» (the owner's review list 2026-10-03, item 19): every book as a cover on a shelf
with the six steps of its stage bar (D76), the step it is at and its last activity; the counts of the filter
chips; and the book to resume.

The steps are the stage bar's own (`services._pages_step` … `services._export_step`, so the labels, details,
hints and addresses match the book screens), fed from grouped queries over every book instead of one
`services.StageFacts` per book (eight queries each): the shelf costs a constant number of queries, whatever
the number of books. One thing is left out on the shelf: the manuscript's staleness (pages changed after the
assembly, `assembly.services.page_changes`, one signature query per book), so a shelf step never reads
«أقدم من النص». The book to resume gets its exact steps (`services.book_stages`), and its book screens
always do.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.files.storage import default_storage
from django.db.models import Count, Max, Min, OuterRef, Subquery
from django.urls import reverse

from books import services
from books.models import Book, Page

# The step a shelf card is at, as a filter of its own: the six keys of the stage bar, then these two.
COMPLETE = "complete"  # every step done: the book has its files
ATTENTION = "attention"  # a step needs attention (a failed page, assembly or export): a filter across steps
FILTER_LABELS: dict[str, str] = {**services.STAGE_NAMES, COMPLETE: "مكتملة", ATTENTION: "تحتاج انتباهًا"}

# The resume card's primary action, by the step the book is at.
RESUME_ACTIONS: dict[str, str] = {
    "pages": "متابعة التخطيط",
    "ocr": "متابعة المعالجة",
    "review": "متابعة المراجعة",
    "manuscript": "فتح المخطوطة",
    "book": "فتح الكتاب",
    "export": "إخراج الكتاب",
}

# A typographic cover's cloth and ink when the book has no colours of its own: the cover presets of «التنسيق»
# (publishing.model.COVER_PRESETS) but white, picked by the book's id so a book keeps its colour.
SHELF_CLOTHS: tuple[str, ...] = ("cream", "navy", "green", "burgundy", "gray")
BOOKS_NOUN = ("كتاب واحد", "كتابان", "كتب", "كتابًا")

# The browser's choice of view and sort, `<view>:<sort>` (library.js writes it), so the page is drawn that way
# at once. The orders match library.js `SORTS` (titles by code point here, by the Arabic collation there).
PREFS_COOKIE = "nassakh_shelf"
VIEWS: tuple[str, ...] = ("grid", "list")
SORTS = {
    "activity": lambda row: (-row["activity"].timestamp(),),
    "progress": lambda row: (-row["rank"], -row["activity"].timestamp()),
    "title": lambda row: (row["book"].title,),
    "added": lambda row: (-row["book"].created_at.timestamp(),),
}


def prefs(value: str | None) -> tuple[str, str]:
    """`(view, sort)` from the cookie's `<view>:<sort>`; the defaults (`grid`, `activity`) otherwise."""
    view, _, sort = (value or "").partition(":")
    return (view if view in VIEWS else VIEWS[0], sort if sort in SORTS else "activity")


@dataclass
class _Facts:
    """What the stage bar's step functions read (`services.StageFacts`' attributes), from the shelf's grouped
    queries. `pending` holds the first page waiting for review only (the steps read its first item)."""

    book: Book
    total: int = 0
    uploaded: int = 0
    layout_errors: int = 0
    ocr_errors: int = 0
    read: int = 0
    reviewed: int = 0
    pending: list[int] = field(default_factory=list)
    first_page: int | None = None
    manuscript: dict = field(default_factory=dict)
    trim_label: str = ""
    page_count: int | None = None
    exports: list[dict] = field(default_factory=list)


def _manuscript(run: dict | None, row: dict | None) -> dict:
    """`assembly.services.manuscript_state`'s keys the steps read, without the staleness (module docstring):
    a manuscript exists once a run wrote it (as there, a book never assembled has no manuscript)."""
    state = {
        "exists": False,
        "version": 0,
        "active": False,
        "run": None,
        "edited": False,
        "stale": False,
        "stale_pages": [],
        "drift_pages": [],
        "stats": {},
    }
    if run is not None:
        error = (run["error"] or "").splitlines()
        state["run"] = {
            "status": run["status"],
            "stage": run["stage"],
            "error": error[0] if error and run["status"] == "error" else "",
        }
        state["active"] = run["status"] in ("queued", "running")
    if run is not None and row is not None:
        state.update(
            exists=True,
            version=row["version"],
            edited=row["origin"] == "editor",
            stats=dict(row["run__stats"] or {}),
        )
    return state


def _percent(step: dict) -> int:
    """How far a step is (0–100): 100 when done, the share of its `count` («12/40») while it runs, else 0."""
    if step["state"] == "done":
        return 100
    count = step.get("count") or ""
    done, _, total = count.partition("/")
    if done.isdigit() and total.isdigit() and int(total):
        return round(100 * int(done) / int(total))
    return 0


def _short(step: dict) -> str:
    """A step in a few words for a card: its count when it has one, else its detail."""
    return step.get("count") or step.get("detail") or ""


def _journey(steps: list[dict], dashboard: str) -> dict:
    """The card's view of its steps: each step's `percent`, the step it is at (the first one not done; None
    when every step is), its filter key, its rank for «التقدّم» and the address of «متابعة»."""
    for step in steps:
        step["percent"] = _percent(step)
    current = next((step for step in steps if step["state"] != "done"), None)
    attention = any(step["state"] == "attention" for step in steps)
    if current is None:
        last = steps[-1]
        return {
            "current": None,
            "stage_key": COMPLETE,
            "attention": attention,
            "rank": 100 * len(steps),
            "next_url": last["url"] or dashboard,
            "next_label": "مكتمل",
            "next_text": last["detail"],
        }
    index = steps.index(current)
    return {
        "current": current,
        "stage_key": current["key"],
        "attention": attention,
        "rank": 100 * index + current["percent"],
        "next_url": current["url"] or dashboard,
        "next_label": current["label"],
        "next_text": _short(current),
    }


def _rendered_cover(book_id: int) -> str | None:
    """The cover as last rendered on the book page (`publishing.cover.cover_payload`, D80): the newest
    render folder that is complete (its PDF is written last), as the URL of its `cover.webp`; None when
    there is none."""
    root = Path(settings.MEDIA_ROOT) / "books" / str(book_id) / "cover"
    try:
        folders = [entry for entry in os.scandir(root) if entry.is_dir()]
    except OSError:
        return None
    folders.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)
    for entry in folders:
        if os.path.isfile(os.path.join(entry.path, "cover.pdf")) and os.path.isfile(
            os.path.join(entry.path, "cover.webp")
        ):
            return default_storage.url(f"books/{book_id}/cover/{entry.name}/cover.webp")
    return None


def _cover(book: Book, cover: dict | None) -> dict:
    """The card's cover: `{url, cloth, ink}`. `url` is the rendered cover when the book has one (its
    stylesheet's cover mode is not `none`), else None and the card sets a typographic cover in `cloth` and
    `ink`: the cover's colours when they were changed from the default white, else a preset by the book's
    id."""
    from publishing.model import COVER_DEFAULTS, COVER_PRESETS, cover_settings

    url = None
    cloth, ink = COVER_PRESETS[SHELF_CLOTHS[book.pk % len(SHELF_CLOTHS)]][1:]
    if cover is not None:
        values = cover_settings(cover)
        if values["mode"] != "none":
            url = _rendered_cover(book.pk)
        if (values["background"], values["color"]) != (COVER_DEFAULTS["background"], COVER_DEFAULTS["color"]):
            cloth, ink = values["background"], values["color"]
    return {"url": url, "cloth": cloth, "ink": ink}


def _author(value: str) -> str:
    """The author as the shelf shows it: '' for a placeholder dash («-»)."""
    value = (value or "").strip()
    return "" if value.strip("-–— ") == "" else value


def _latest(*moments: datetime | None) -> datetime | None:
    stamps = [moment for moment in moments if moment is not None]
    return max(stamps) if stamps else None


def books_shelf(sort: str = "activity") -> dict:
    """The books home: `{rows, resume, filters, summary}`.

    `rows` (in the `sort` order, `SORTS`; newest activity first by default): `{book, author, byline, pages,
    pages_label, steps, current, stage_key, attention, rank, next_url, next_label, next_text, dashboard_url,
    cover, scan_url, pending_page, activity, activity_label}` — `steps` the stage bar's six (with a
    `percent`), `current` the step the book is at (None once every step is done), `stage_key` its filter (a
    step key, `complete`), `rank` the order of «الأبعد مرحلةً», `next_*` the card's «متابعة» link, `cover`
    as `_cover`, `scan_url` the first page's scan thumbnail, `activity` the latest of the book's change, a
    review, the manuscript's save, an assembly and an export. `resume` is the first row with a step left,
    with its exact steps (`services.book_stages`), `action` (the primary button's label) and `page_url`
    (the scan of the page the step continues at), or None. `filters`: `{key, label, count}` of «الكل», each
    step with books at it, `complete` and `attention` when they have books. `summary`: `{books,
    books_label, pages_label}`.
    """
    from assembly.models import AssemblyRun
    from editor.models import TRIM_PRESETS, Manuscript, StyleSheet
    from publishing.models import Export, LiveLayout, PreviewRender

    # the first scanned page, the latest assembly run and the latest whole-book preview of each book, as
    # subqueries of the one books query (portable: no DISTINCT ON)
    first_scan = (
        Page.objects.filter(book_id=OuterRef("pk"), is_excluded=False)
        .exclude(scan_thumbnail="")
        .order_by("number")
        .values("scan_thumbnail")[:1]
    )
    latest_run = (
        AssemblyRun.objects.filter(book_id=OuterRef("pk")).order_by("-created_at", "-id").values("pk")[:1]
    )
    latest_render = (
        PreviewRender.objects.filter(book_id=OuterRef("pk"), scope="book", status="done")
        .order_by("-created_at", "-id")
        .values("page_count")[:1]
    )
    books = list(
        Book.objects.annotate(
            shelf_scan=Subquery(first_scan),
            shelf_run=Subquery(latest_run),
            shelf_render=Subquery(latest_render),
        )
    )
    if not books:
        return {
            "rows": [],
            "resume": None,
            "filters": [],
            "summary": {"books": 0, "books_label": "", "pages_label": ""},
        }
    ids = [book.pk for book in books]
    facts = {book.pk: _Facts(book) for book in books}
    reviewed_at: dict[int, datetime | None] = {}

    pages = Page.objects.filter(book_id__in=ids, is_excluded=False)
    groups = pages.values("book_id", "status").annotate(
        n=Count("id"), first=Min("number"), last=Max("reviewed_at")
    )
    for row in groups:
        f = facts[row["book_id"]]
        n, status = row["n"], row["status"]
        f.total += n
        f.first_page = row["first"] if f.first_page is None else min(f.first_page, row["first"])
        reviewed_at[row["book_id"]] = _latest(reviewed_at.get(row["book_id"]), row["last"])
        if status == Page.Status.UPLOADED:
            f.uploaded += n
        if status in (Page.Status.OCR_DONE, Page.Status.REVIEWED, Page.Status.ASSEMBLED):
            f.read += n
        if status in (Page.Status.REVIEWED, Page.Status.ASSEMBLED):
            f.reviewed += n
        if status == Page.Status.OCR_DONE:
            f.pending = [row["first"]]
    errors = pages.filter(status=Page.Status.ERROR).values("book_id", "error_from").annotate(n=Count("id"))
    for row in errors:
        f = facts[row["book_id"]]
        if f.book.awaits_ocr_start or row["error_from"] == "preprocess":
            f.layout_errors += row["n"]
        else:
            f.ocr_errors += row["n"]
    runs = {
        row["book_id"]: row
        for row in AssemblyRun.objects.filter(
            pk__in=[book.shelf_run for book in books if book.shelf_run]
        ).values("book_id", "status", "stage", "error", "created_at")
    }
    manuscripts = {
        row["book_id"]: row
        for row in Manuscript.objects.filter(book_id__in=ids).values(
            "book_id", "version", "origin", "updated_at", "run__stats"
        )
    }
    sheets = {
        row["book_id"]: row
        for row in StyleSheet.objects.filter(book_id__in=ids).values(
            "book_id", "trim", "width_mm", "height_mm", "front_matter__cover"
        )
    }
    live = {
        book_id: count
        for book_id, count, revision in LiveLayout.objects.filter(book_id__in=ids).values_list(
            "book_id", "page_count", "revision"
        )
        if revision
    }
    exports: dict[int, list[dict]] = {}
    for row in (
        Export.objects.filter(book_id__in=ids)
        .exclude(status=Export.Status.CANCELLED)
        .order_by("-created_at", "-id")
        .values("book_id", "format", "status", "manuscript_version", "finished_at", "error", "created_at")
    ):
        exports.setdefault(row["book_id"], []).append(row)

    rows = []
    for book in books:
        f = facts[book.pk]
        run, manuscript = runs.get(book.pk), manuscripts.get(book.pk)
        f.manuscript = _manuscript(run, manuscript)
        if f.manuscript["exists"]:
            sheet = sheets.get(book.pk)
            trim = sheet["trim"] if sheet else StyleSheet().trim
            f.trim_label = (
                TRIM_PRESETS[trim][0]
                if trim in TRIM_PRESETS
                else f"{sheet['width_mm']:g}×{sheet['height_mm']:g} مم"
            )
            f.page_count = live[book.pk] if book.pk in live else book.shelf_render
            f.exports = exports.get(book.pk, [])[:60]
        steps = services.book_stages(book, facts=f)  # the stage bar's steps on the shelf's facts
        dashboard = reverse("books:detail", args=[book.pk])
        activity = _latest(
            book.updated_at,
            reviewed_at.get(book.pk),
            manuscript["updated_at"] if manuscript else None,
            run["created_at"] if run else None,
            exports[book.pk][0]["created_at"] if exports.get(book.pk) else None,
        )
        sheet = sheets.get(book.pk)
        scan = book.shelf_scan
        author = _author(book.author)
        year = str(book.original_year) if book.original_year else ""
        rows.append(
            {
                "book": book,
                "author": author,
                "byline": " · ".join(part for part in (author, year) if part),
                "pages": f.total,
                "pages_label": (
                    services._count(f.total, services.PAGES_NOUN)
                    if f.total
                    else f"{services._count(book.source_page_count, services.PAGES_NOUN)} في الملف"
                ),
                "steps": steps,
                **_journey(steps, dashboard),
                "dashboard_url": dashboard,
                "cover": _cover(book, sheet["front_matter__cover"] if sheet else None),
                "scan_url": default_storage.url(scan) if scan else None,
                "pending_page": f.pending[0] if f.pending else None,
                "activity": activity,
                "activity_label": services.relative_time(activity),
            }
        )
    rows.sort(key=SORTS["activity"])
    # the book to resume: the newest activity's; before the filters and the sort (it may correct its row)
    resume = _resume(rows)
    if sort != "activity":
        rows.sort(key=SORTS.get(sort, SORTS["activity"]))
    return {
        "rows": rows,
        "resume": resume,
        "filters": _filters(rows),
        "summary": {
            "books": len(rows),
            "books_label": services._count(len(rows), BOOKS_NOUN),
            "pages_label": services._count(sum(row["pages"] for row in rows), services.PAGES_NOUN),
        },
    }


def _resume(rows: list[dict]) -> dict | None:
    """The book to resume: the first row (newest activity) with a step left, with its exact steps (its shelf
    row takes them too, so the two agree); None when there are fewer than two books (the shelf shows the one
    book already) or every book is complete."""
    if len(rows) < 2:
        return None
    row = next((row for row in rows if row["current"] is not None), None)
    if row is None:
        return None
    book = row["book"]
    steps = services.book_stages(book)
    journey = _journey(steps, row["dashboard_url"])
    row.update(steps=steps, **journey)
    if journey["current"] is None:
        return None
    current = journey["current"]
    page_url = row["scan_url"]
    if current["key"] == "review" and row["pending_page"] is not None:
        scan = (
            Page.objects.filter(book_id=book.pk, number=row["pending_page"])
            .values_list("scan_thumbnail", flat=True)
            .first()
        )
        page_url = default_storage.url(scan) if scan else page_url
    return {
        **row,
        "action": RESUME_ACTIONS.get(current["key"], "متابعة"),
        "page_url": page_url,
    }


def _filters(rows: list[dict]) -> list[dict]:
    """The filter chips: «الكل», then each step with books at it, `complete` and `attention` with books."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["stage_key"]] = counts.get(row["stage_key"], 0) + 1
        if row["attention"]:
            counts[ATTENTION] = counts.get(ATTENTION, 0) + 1
    chips = [{"key": "all", "label": "الكل", "count": len(rows)}]
    for key in (*services.STAGE_KEYS, COMPLETE, ATTENTION):
        if counts.get(key):
            chips.append({"key": key, "label": FILTER_LABELS[key], "count": counts[key]})
    return chips
