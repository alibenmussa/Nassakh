"""Page previews (PHASE5_SPEC §3, D44): the book (or one chapter) rendered to a PDF and page images.

`render_preview(book, scope, chapter_id)` is the job the tasks run: build the render job from the
manuscript and the stylesheet, hash it, reuse a finished render of the same hash, otherwise render with
`publishing.engine` and write under `media/books/<id>/preview/<hash>/`:

    book.pdf | chapter.pdf, page-0001.webp (1×, ≈ 1100 px tall), page-0001-2x.webp, …, layout.json

`layout.json` (D47) is the pages' layout (`publishing.layout`) with the chapters, the page checks and the
footnote numbers shown; a finished book render becomes the book's live layout (`publishing.relayout`).

**Cache by hash.** The hash covers the content rendered (the whole document, or the chapter with the
title node and the number of notes before it), the page setup, the font files (path, size, mtime), the
renderer version and, for a chapter, its first page. A request for a finished hash costs nothing.

**One render per scope at a time.** A new request for a different hash of the same scope (the book, or
the same chapter) cancels the queued or running one: its row becomes `cancelled`, its Celery task is
revoked (a queued task never starts) and a running job stops at its next check (between layout passes,
every 20 page images) — the worker process is not killed. Then the new hash is rendered.

**After edits (D44, D47).** An editor save asks for the chapter's fast re-layout at once
(`publishing.relayout`, queue `layout`) and schedules the book's render twenty seconds later; the task
carries the manuscript version it was scheduled for and does nothing if a later save happened (that save
scheduled its own), so the book renders once edits settle. A chapter rendered alone starts at its first
page in the current layout (odd pages recto).

Old renders are pruned: the newest 3 of the book, 2 per chapter, 24 chapter renders per book.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from books.models import Book
from editor import document as doc
from editor.models import TRIM_PRESETS, Manuscript, StyleSheet

from .engine import Rendered, RenderJob, get_engine
from .fonts import resolve
from .layout import side_shift
from .model import page_setup
from .models import LiveLayout, PreviewRender
from .pdf import RenderCancelled

log = logging.getLogger(__name__)

PAGE_HEIGHT_PX = 1100
WEBP_QUALITY = 78
WEBP_METHOD = 3
ENCODERS = min(8, os.cpu_count() or 2)
ENCODE_BACKLOG = ENCODERS * 3  # page images drawn and waiting for an encoder
BOOK_SETTLE_S = 20
ABANDONED_AFTER = timedelta(minutes=30)
KEEP_BOOK = 3
KEEP_PER_CHAPTER = 2
KEEP_CHAPTERS = 24
CHECK_EVERY_PAGES = 20

RENDER_ERROR = "تعذّر إخراج صفحات المعاينة. أعد المحاولة، وإن تكرّر الخطأ فراجع سجل الخادم."
ABANDONED_ERROR = "توقّف إخراج المعاينة قبل أن يكتمل؛ أعد المحاولة."
ENQUEUE_ERROR = "تعذّر إرسال المعاينة إلى طابور المهام؛ تحقّق من تشغيل Redis وعامل Celery ثم أعد المحاولة."

ACTIVE = (PreviewRender.Status.QUEUED, PreviewRender.Status.RUNNING)
LAYOUT_FILE = "layout.json"
LAYOUT_FORMAT = 1


class PreviewNotFound(LookupError):
    """No manuscript yet, or no chapter with that id (API 404)."""


# ====================================================================== inputs


def stylesheet_for(book: Book) -> StyleSheet:
    """The book's stylesheet, or an unsaved one with the defaults (nothing is written)."""
    sheet = StyleSheet.objects.filter(book_id=book.pk).first()
    return sheet if sheet is not None else StyleSheet(book=book)


def _manuscript(book: Book) -> Manuscript | None:
    return (
        Manuscript.objects.filter(book_id=book.pk)
        .only("id", "book_id", "document", "version", "updated_at")
        .first()
    )


def _rows(book_id: int, scope: str, chapter_id: str, kind: str = PreviewRender.Kind.PAGES):
    rows = PreviewRender.objects.filter(book_id=book_id, scope=scope, kind=kind)
    return rows.filter(chapter_id=chapter_id) if scope == PreviewRender.Scope.CHAPTER else rows


def scope_key(scope: str, chapter_id: str | None) -> str:
    """The `chapter_id` column of a scope: the chapter's id for a chapter render, '' for the book."""
    return (chapter_id or "") if scope == PreviewRender.Scope.CHAPTER else ""


def latest_done(book_id: int, scope: str = "book", chapter_id: str = "") -> PreviewRender | None:
    """The newest finished render of a scope (its pages are what the viewer shows)."""
    return (
        _rows(book_id, scope, chapter_id)
        .filter(status=PreviewRender.Status.DONE)
        .order_by("-created_at", "-id")
        .first()
    )


def chapter_first_page(book_id: int, chapter_id: str) -> int:
    """The chapter's first page in the book's live layout, else in the newest finished book render (1 when
    unknown)."""
    live = LiveLayout.objects.filter(book_id=book_id, revision__gt=0).only("chapters").first()
    render = latest_done(book_id, PreviewRender.Scope.BOOK) if live is None else None
    items = live.chapters if live is not None else (render.chapters if render is not None else [])
    for item in items or []:
        if isinstance(item, dict) and item.get("id") == chapter_id and isinstance(item.get("first"), int):
            return max(1, item["first"])
    return 1


def job_for(
    book: Book, scope: str, chapter_id: str | None, manuscript: Manuscript | None = None, stylesheet=None
) -> RenderJob | None:
    """The render job of the book's current manuscript (None without one, or without that chapter)."""
    manuscript = manuscript if manuscript is not None else _manuscript(book)
    if manuscript is None:
        return None
    document = manuscript.document or {}
    setup = page_setup(stylesheet if stylesheet is not None else stylesheet_for(book))
    first_page = 1
    if scope == PreviewRender.Scope.CHAPTER:
        if not chapter_id or doc.find_chapter(document, chapter_id) is None:
            return None
        first_page = chapter_first_page(book.pk, chapter_id)
    elif scope != PreviewRender.Scope.BOOK:
        raise ValueError(f"unknown scope {scope!r}")
    return RenderJob(
        document=document,
        stylesheet=setup,
        title=book.title,
        author=book.author,
        scope=scope,
        chapter_id=chapter_id if scope == PreviewRender.Scope.CHAPTER else None,
        first_page=first_page,
    )


def job_hash(job: RenderJob) -> str:
    """24 hex digits identifying what a job renders (see the module docstring)."""
    content = doc.content_of(job.document)
    preamble = content[: doc.preamble_end(content)]
    if job.scope == PreviewRender.Scope.CHAPTER:
        chapter = doc.find_chapter(job.document, job.chapter_id or "")
        nodes = chapter.nodes(job.document) if chapter is not None else []
        before = content[doc.preamble_end(content) : chapter.start] if chapter is not None else []
        notes_before = sum(1 for _b, note, _c in doc.iter_containers(before) if note is not None)
        part: object = {"preamble": preamble, "nodes": nodes, "notes_before": notes_before}
    else:
        part = content
    setup = page_setup(job.stylesheet)
    fonts = resolve(setup.body_font, setup.latin_font, setup.heading_font)
    payload = {
        "engine": get_engine().version,
        "scope": job.scope,
        "chapter": job.chapter_id,
        "first_page": job.first_page,
        "title": job.title,
        "author": job.author,
        "setup": setup.as_dict(),
        "fonts": fonts.fingerprint(),
        "content": part,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def preview_folder(book_id: int, content_hash: str) -> str:
    """Media path of a render's files: `books/<id>/preview/<hash>`."""
    return f"books/{book_id}/preview/{content_hash}"


def _abs(folder: str) -> Path:
    return Path(settings.MEDIA_ROOT) / folder


def page_file(index: int, retina: bool = False) -> str:
    """File name of page `index` (1-based within the render)."""
    return f"page-{index:04d}{'-2x' if retina else ''}.webp"


def _files_exist(render: PreviewRender) -> bool:
    if not render.folder or not render.page_count:
        return False
    folder = _abs(render.folder)
    if render.kind == PreviewRender.Kind.LAYOUT:
        return (folder / LAYOUT_FILE).is_file()
    return (folder / page_file(1)).is_file() and (folder / page_file(render.page_count)).is_file()


def layout_path(render: PreviewRender) -> Path:
    """The file of a render's layout (`<folder>/layout.json`)."""
    return _abs(render.folder) / LAYOUT_FILE


def layout_document(
    render: PreviewRender, rendered: Rendered, setup, *, first_page: int, chapters: list[dict]
) -> dict:
    """What `layout.json` holds (D47): the render's pages (each with its image reference `src`), chapters,
    checks, the footnote numbers shown and the page geometry the client needs to move pages."""
    pages = []
    for index, page in enumerate(rendered.layout, start=1):
        pages.append(dict(page, src={"render": render.pk, "index": index}))
    return {
        "format": LAYOUT_FORMAT,
        "engine": get_engine().version,
        "render": render.pk,
        "kind": render.kind,
        "scope": render.scope,
        "chapter": render.chapter_id or None,
        "first_page": first_page,
        "page_count": len(pages),
        "unit": "pt",
        "setup_hash": setup_hash(setup),
        "geometry": geometry(setup),
        "chapters": chapters,
        "checks": rendered.checks,
        "numbers": rendered.numbers,
        "misses": rendered.misses,
        "missing_fonts": rendered.missing_fonts,
        "pages": pages,
    }


def setup_hash(setup) -> str:
    """24 hex digits identifying how pages are laid out: the renderer, the page setup and the font files
    (a live layout of another setup is replaced, never spliced into)."""
    fonts = resolve(setup.body_font, setup.latin_font, setup.heading_font)
    payload = {"engine": get_engine().version, "setup": setup.as_dict(), "fonts": fonts.fingerprint()}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def geometry(setup) -> dict:
    """The page geometry of a setup for the client: size, margins, the text's move when a page changes
    side (`side_shift_pt`: right → left; left → right is the negative) and where the number sits."""
    mm = 72 / 25.4
    return {
        "width_pt": round(setup.width_mm * mm, 2),
        "height_pt": round(setup.height_mm * mm, 2),
        "margins_pt": {
            "top": round(setup.top_mm * mm, 2),
            "bottom": round(setup.bottom_mm * mm, 2),
            "inner": round(setup.inner_mm * mm, 2),
            "outer": round(setup.outer_mm * mm, 2),
        },
        "side_shift_pt": side_shift(setup),
        "page_number": setup.page_number,
        "running_header": setup.running_header,
        "chapter_opening": setup.chapter_opening,
    }


def write_json(path: Path, data: dict) -> None:
    """Write `data` as compact UTF-8 JSON, atomically (a reader never sees half a file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def _abandoned(render: PreviewRender) -> bool:
    return render.created_at is not None and timezone.now() - render.created_at > ABANDONED_AFTER


# ====================================================================== rendering


def rasterize(pdf: bytes, folder: Path, cancelled=None) -> int:
    """Write every page of `pdf` as WebP at 1× (`PAGE_HEIGHT_PX` tall) and 2×; returns the page count.

    Pages are drawn on this thread (PyMuPDF) and encoded on a small thread pool (libwebp runs without
    the GIL): about 15 ms a page on the owner's Mac.
    """
    import pymupdf
    from PIL import Image

    folder.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open(stream=pdf, filetype="pdf")

    def encode(image, path: Path) -> None:
        image.save(path, "WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)

    try:
        with ThreadPoolExecutor(max_workers=ENCODERS) as pool:
            jobs: deque = deque()
            for index, page in enumerate(document, start=1):
                if cancelled is not None and index % CHECK_EVERY_PAGES == 0 and cancelled():
                    raise RenderCancelled
                scale = PAGE_HEIGHT_PX / page.rect.height
                for retina in (False, True):
                    factor = scale * (2 if retina else 1)
                    pix = page.get_pixmap(
                        matrix=pymupdf.Matrix(factor, factor), colorspace=pymupdf.csGRAY, alpha=False
                    )
                    image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
                    jobs.append(pool.submit(encode, image, folder / page_file(index, retina)))
                    # a bounded backlog: a long book never holds all its page bitmaps in memory
                    while len(jobs) > ENCODE_BACKLOG:
                        jobs.popleft().result()
            while jobs:
                jobs.popleft().result()
        return document.page_count
    finally:
        document.close()


def _claim(
    book: Book, scope: str, chapter_id: str, digest: str, first_page: int, task_id: str
) -> tuple[PreviewRender | None, bool]:
    """`(render, should_render)` for this hash: a finished one is returned as is; one running under
    another task is left to it; otherwise the queued row (or a new one) becomes `running`, and the other
    active renders of the scope are cancelled."""
    with transaction.atomic():
        Book.objects.select_for_update().filter(pk=book.pk).first()  # one claim per book at a time
        row = (
            _rows(book.pk, scope, chapter_id)
            .filter(content_hash=digest)
            .order_by("-created_at", "-id")
            .first()
        )
        if row is not None and row.status == PreviewRender.Status.DONE and _files_exist(row):
            return row, False
        if (
            row is not None
            and row.status == PreviewRender.Status.RUNNING
            and not _abandoned(row)
            and row.task_id
            and row.task_id != task_id
        ):
            return row, False
        if row is None or row.status not in (PreviewRender.Status.QUEUED, PreviewRender.Status.RUNNING):
            row = PreviewRender.objects.create(
                book_id=book.pk,
                scope=scope,
                chapter_id=chapter_id,
                content_hash=digest,
                first_page=first_page,
                status=PreviewRender.Status.QUEUED,
                task_id=task_id[:64],
            )
        row.status = PreviewRender.Status.RUNNING
        row.task_id = (task_id or row.task_id)[:64]
        row.first_page = first_page
        row.save(update_fields=["status", "task_id", "first_page"])
        cancel_others(book.pk, scope, chapter_id, keep=row.pk)
    return row, True


def cancel_others(book_id: int, scope: str, chapter_id: str, keep: int | None = None) -> int:
    """Cancel the queued / running renders of a scope except `keep`: rows become `cancelled`, their Celery
    tasks are revoked (a queued task never starts; a running job stops at its next check)."""
    rows = list(
        _rows(book_id, scope, chapter_id).filter(status__in=ACTIVE).exclude(pk=keep).only("id", "task_id")
    )
    if not rows:
        return 0
    PreviewRender.objects.filter(pk__in=[row.pk for row in rows]).update(
        status=PreviewRender.Status.CANCELLED, finished_at=timezone.now()
    )
    for row in rows:
        revoke(row.task_id)
    return len(rows)


def revoke(task_id: str) -> None:
    """Revoke a Celery task (no-op without an id or when tasks run eagerly)."""
    if not task_id or getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        return
    try:
        from nassakh.celery import app

        app.control.revoke(task_id)
    except Exception:  # noqa: BLE001 - the cooperative check still stops the job
        log.warning("could not revoke preview task %s", task_id, exc_info=True)


def render_preview(
    book: Book,
    scope: str = "book",
    chapter_id: str | None = None,
    *,
    version: int | None = None,
    task_id: str = "",
) -> PreviewRender | None:
    # (D47: the finished render also writes `layout.json`; a book render becomes the live layout)
    """Render the book's current content for `scope` (the tasks' job; synchronous).

    Returns the finished (or failed, or cancelled) render, the cached one for an unchanged hash, the one
    another task is rendering, or None when there is nothing to render (no manuscript, no such chapter,
    or `version` given and the manuscript changed since: a later save scheduled its own render).
    """
    manuscript = _manuscript(book)
    if manuscript is None or (version is not None and manuscript.version != version):
        return None
    job = job_for(book, scope, chapter_id, manuscript)
    if job is None:
        return None
    digest = job_hash(job)
    row, work = _claim(book, scope, scope_key(scope, chapter_id), digest, job.first_page, task_id)
    if not work or row is None:
        _adopt(row)
        return row
    PreviewRender.objects.filter(pk=row.pk).update(version=manuscript.version)
    row.version = manuscript.version
    _run(row, job)
    row.refresh_from_db()
    _adopt(row)
    return row


def _adopt(row: PreviewRender | None) -> None:
    """A finished book render of the current content becomes the live layout when nothing newer is live
    (`relayout.adopt_book_render`): a new one, or a cached one found again — the page setup changed back to
    one rendered before, whose pages are not the live ones."""
    if row is None or row.status != PreviewRender.Status.DONE or row.scope != PreviewRender.Scope.BOOK:
        return
    from .relayout import adopt_book_render

    adopt_book_render(row)


def _cancel_check(render_id: int):
    def cancelled() -> bool:
        return PreviewRender.objects.filter(pk=render_id, status=PreviewRender.Status.CANCELLED).exists()

    return cancelled


def _run(row: PreviewRender, job: RenderJob) -> None:
    """Render `row`'s job into its folder and record the result on the row."""
    cancelled = _cancel_check(row.pk)
    folder = preview_folder(row.book_id, row.content_hash)
    target = _abs(folder)
    try:
        rendered = get_engine().render(job, cancelled)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        (target / ("chapter.pdf" if row.scope == PreviewRender.Scope.CHAPTER else "book.pdf")).write_bytes(
            rendered.pdf
        )
        pages = rasterize(rendered.pdf, target, cancelled)
        chapters = rendered.chapters
        if row.scope == PreviewRender.Scope.CHAPTER:
            chapters = [
                dict(item, first=row.first_page, last=row.first_page + pages - 1) for item in chapters[:1]
            ]
        write_json(
            target / LAYOUT_FILE,
            layout_document(
                row, rendered, page_setup(job.stylesheet), first_page=job.first_page, chapters=chapters
            ),
        )
        finished = PreviewRender.objects.filter(pk=row.pk, status=PreviewRender.Status.RUNNING).update(
            status=PreviewRender.Status.DONE,
            page_count=pages,
            chapters=chapters,
            folder=folder,
            passes=rendered.passes,
            duration_ms=rendered.duration_ms,
            checks=rendered.checks,
            error="",
            finished_at=timezone.now(),
        )
        if not finished:  # cancelled while writing the images
            shutil.rmtree(target, ignore_errors=True)
            return
        prune(row.book_id, row.scope, row.chapter_id)
    except RenderCancelled:
        shutil.rmtree(target, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 - reported on the row (designed failure, D22)
        log.exception("preview render %s of book %s failed", row.pk, row.book_id)
        PreviewRender.objects.filter(pk=row.pk).exclude(status=PreviewRender.Status.CANCELLED).update(
            status=PreviewRender.Status.ERROR,
            error=f"{RENDER_ERROR}\n{type(exc).__name__}: {exc}"[:4000],
            finished_at=timezone.now(),
        )


def prune(book_id: int, scope: str, chapter_id: str = "") -> int:
    """Delete superseded renders and their folders (see the module docstring); returns the rows deleted."""
    rows = list(_rows(book_id, scope, chapter_id).order_by("-created_at", "-id"))
    done = [row for row in rows if row.status == PreviewRender.Status.DONE]
    keep = {row.pk for row in done[: KEEP_BOOK if scope == PreviewRender.Scope.BOOK else KEEP_PER_CHAPTER]}
    newest_done = done[0].created_at if done else None
    drop = [
        row
        for row in rows
        if row.pk not in keep
        and not row.is_active
        and (
            row.status == PreviewRender.Status.DONE
            or (newest_done is not None and row.created_at <= newest_done)
        )
    ]
    if scope == PreviewRender.Scope.CHAPTER:
        others = list(
            PreviewRender.objects.filter(book_id=book_id, scope=scope, status=PreviewRender.Status.DONE)
            .exclude(pk__in=[row.pk for row in drop])
            .order_by("-created_at", "-id")[KEEP_CHAPTERS:]
        )
        drop += others
    live = LiveLayout.objects.filter(book_id=book_id).values_list("base_id", flat=True).first()
    drop = [row for row in drop if row.pk != live]  # the live layout's base keeps its files
    if not drop:
        return 0
    kept_folders = set(
        PreviewRender.objects.filter(book_id=book_id)
        .exclude(pk__in=[row.pk for row in drop])
        .values_list("folder", flat=True)
    )
    for row in drop:
        if row.folder and row.folder not in kept_folders:
            shutil.rmtree(_abs(row.folder), ignore_errors=True)
    PreviewRender.objects.filter(pk__in=[row.pk for row in drop]).delete()
    return len(drop)


# ====================================================================== requests


def _enqueue(row: PreviewRender) -> None:
    """Send the render of `row`'s scope to the default queue and remember the task id."""
    from . import tasks

    try:
        if row.scope == PreviewRender.Scope.CHAPTER:
            result = tasks.render_chapter_preview.delay(row.book_id, row.chapter_id)
        else:
            result = tasks.render_book_preview.delay(row.book_id)
    except Exception as exc:  # noqa: BLE001 - reported on the row
        log.exception("preview render %s of book %s could not be enqueued", row.pk, row.book_id)
        PreviewRender.objects.filter(pk=row.pk, status=PreviewRender.Status.QUEUED).update(
            status=PreviewRender.Status.ERROR,
            error=f"{ENQUEUE_ERROR}\n{type(exc).__name__}: {exc}"[:4000],
            finished_at=timezone.now(),
        )
        return
    task_id = getattr(result, "id", "") or ""
    if task_id:
        PreviewRender.objects.filter(pk=row.pk, task_id="").update(task_id=task_id[:64])


def request_preview(
    book: Book, scope: str = "book", chapter_id: str | None = None, *, force: bool = False
) -> PreviewRender | None:
    """The render for the current content of `scope` (see `engine.request_preview`)."""
    job = job_for(book, scope, chapter_id)
    if job is None:
        return None
    key = scope_key(scope, chapter_id)
    digest = job_hash(job)
    cached = None
    with transaction.atomic():
        Book.objects.select_for_update().filter(pk=book.pk).first()
        row = _rows(book.pk, scope, key).filter(content_hash=digest).order_by("-created_at", "-id").first()
        if row is not None and row.status == PreviewRender.Status.DONE and _files_exist(row):
            cached = row
        elif row is not None and row.is_active and not _abandoned(row):
            return row
        elif row is not None and row.status == PreviewRender.Status.ERROR and not force:
            return row
        else:
            if row is not None and row.is_active:  # abandoned
                PreviewRender.objects.filter(pk=row.pk).update(
                    status=PreviewRender.Status.ERROR, error=ABANDONED_ERROR, finished_at=timezone.now()
                )
            row = PreviewRender.objects.create(
                book_id=book.pk,
                scope=scope,
                chapter_id=key,
                content_hash=digest,
                first_page=job.first_page,
                status=PreviewRender.Status.QUEUED,
            )
            cancel_others(book.pk, scope, key, keep=row.pk)
    if cached is not None:
        _adopt(cached)  # a cached book render (the page setup changed back) becomes the live layout again
        return cached
    _enqueue(row)
    row.refresh_from_db()
    return row


def schedule_after_edit(book: Book, chapter_id: str | None, version: int) -> PreviewRender | None:
    """After an editor save (setting `NASSAKH["PREVIEW_AUTORENDER"]`, on by default): the chapter's fast
    re-layout now (D47, returned so the save can tell the client what to poll) and the book's render once
    edits settle (debounced, D44). Nothing is scheduled for a whole-book edit but the book render."""
    if not settings.NASSAKH.get("PREVIEW_AUTORENDER", True):
        return None
    from . import tasks
    from .relayout import request_relayout

    relayout = None
    if chapter_id:
        try:
            relayout = request_relayout(book, chapter_id, version)
        except Exception:  # noqa: BLE001 - a save never fails because the layout could not be asked for
            log.warning("could not ask for the re-layout of book %s", book.pk, exc_info=True)
    try:
        tasks.render_book_preview.apply_async((book.pk, version), countdown=BOOK_SETTLE_S)
    except Exception:  # noqa: BLE001 - a save never fails because the queue is down
        log.warning("could not schedule the previews of book %s", book.pk, exc_info=True)
    return relayout


def render_after_assembly(book: Book) -> PreviewRender | None:
    """After a whole-book assembly wrote a new text (D49): the book render at once, not after the settle
    delay of an edit, so the book page opens on the new pages (or their render); `request_preview`
    cancels a render left over from the old text. Off with `NASSAKH["PREVIEW_AUTORENDER"]`."""
    if not settings.NASSAKH.get("PREVIEW_AUTORENDER", True):
        return None
    return request_preview(book, PreviewRender.Scope.BOOK)


# ====================================================================== read models


def _page_chapter(chapters: list, number: int) -> str | None:
    for item in chapters or []:
        if (
            isinstance(item, dict)
            and isinstance(item.get("first"), int)
            and isinstance(item.get("last"), int)
        ):
            if item["first"] <= number <= item["last"]:
                return item.get("id")
    return None


def pages_of(render: PreviewRender) -> list[dict]:
    """`[{n, url, url2x, chapter}]` of a finished render (`n` is the printed page number)."""
    if render is None or render.status != PreviewRender.Status.DONE or not render.folder:
        return []
    out = []
    for index in range(1, render.page_count + 1):
        number = render.first_page + index - 1
        out.append(
            {
                "n": number,
                "url": default_storage.url(f"{render.folder}/{page_file(index)}"),
                "url2x": default_storage.url(f"{render.folder}/{page_file(index, True)}"),
                "chapter": _page_chapter(render.chapters, number),
            }
        )
    return out


def _headline(message: str) -> str:
    lines = (message or "").splitlines()
    return lines[0] if lines else ""


def preview_payload(
    book: Book, scope: str = "book", chapter_id: str | None = None, *, enqueue: bool = True
) -> dict:
    """The preview API's answer (PHASE5_SPEC §3):

    `{scope, chapter, status, hash, stale, rendering, page_count, first_page, chapters, pages: [{n, url,
    url2x, chapter}], render_id, rendered_at, pdf_url, error, missing_fonts}` — `status` is the state of
    the render of the current content (`none`, `queued`, `running`, `done`, `error`); the pages are those
    of the newest finished render (kept on screen while a newer one runs), `stale` when they are older
    than the content. With `enqueue`, a content with no render yet (and nothing running) is queued.
    Raises `PreviewNotFound` without a manuscript or for an unknown chapter.
    """
    job = job_for(book, scope, chapter_id)
    if job is None:
        raise PreviewNotFound("لا توجد مخطوطة بعد." if _manuscript(book) is None else "الفصل غير موجود.")
    key = scope_key(scope, chapter_id)
    digest = job_hash(job)
    rows = _rows(book.pk, scope, key)
    current = rows.filter(content_hash=digest).order_by("-created_at", "-id").first()
    live = [row for row in rows.filter(status__in=ACTIVE) if not _abandoned(row)]
    if enqueue and current is None and not live:
        current = request_preview(book, scope, chapter_id)
        live = [row for row in rows.filter(status__in=ACTIVE) if not _abandoned(row)]
    if current is not None and current.status == PreviewRender.Status.DONE and not _files_exist(current):
        current = None
    shown = (
        current
        if current is not None and current.status == PreviewRender.Status.DONE
        else latest_done(book.pk, scope, key)
    )
    status = current.status if current is not None else "none"
    if status == PreviewRender.Status.CANCELLED:
        status = "none"
    setup = page_setup(job.stylesheet)
    fonts = resolve(setup.body_font, setup.latin_font, setup.heading_font)
    pdf_name = "chapter.pdf" if scope == PreviewRender.Scope.CHAPTER else "book.pdf"
    from .relayout import live_summary

    layout_url = reverse("api:preview_layout", args=[book.pk]) + (
        f"?scope=chapter&chapter={chapter_id}" if scope == PreviewRender.Scope.CHAPTER else ""
    )
    return {
        "scope": scope,
        "chapter": chapter_id if scope == PreviewRender.Scope.CHAPTER else None,
        "status": status,
        "hash": digest,
        "stale": shown is None or shown.content_hash != digest,
        "rendering": bool(live),
        "page_count": shown.page_count if shown is not None else 0,
        "first_page": shown.first_page if shown is not None else job.first_page,
        "chapters": list(shown.chapters or []) if shown is not None else [],
        "pages": pages_of(shown) if shown is not None else [],
        "render_id": shown.pk if shown is not None else None,
        "rendered_at": shown.finished_at.isoformat() if shown is not None and shown.finished_at else None,
        "duration_ms": shown.duration_ms if shown is not None else 0,
        "pdf_url": default_storage.url(f"{shown.folder}/{pdf_name}")
        if shown is not None and shown.folder
        else None,
        "error": _headline(current.error)
        if current is not None and current.status == PreviewRender.Status.ERROR
        else "",
        "missing_fonts": fonts.missing,
        "checks": list(shown.checks or []) if shown is not None else [],
        "layout_url": layout_url,
        "layout": live_summary(book, setup) if scope == PreviewRender.Scope.BOOK else None,
    }


def layout_state(book: Book, manuscript_exists: bool = True) -> dict:
    """The dashboard's «الكتاب» block: `{trim, trim_label, page_count, rendering, rendered_at}`.

    Without a manuscript no query is made (the stylesheet's defaults are reported).
    """
    default = StyleSheet()
    if not manuscript_exists:
        label = TRIM_PRESETS[default.trim][0]
        return {
            "trim": default.trim,
            "trim_label": label,
            "page_count": None,
            "rendering": False,
            "rendered_at": None,
        }
    sheet = (
        StyleSheet.objects.filter(book_id=book.pk).only("id", "trim", "width_mm", "height_mm").first()
        or default
    )
    label = (
        TRIM_PRESETS[sheet.trim][0]
        if sheet.trim in TRIM_PRESETS
        else f"{sheet.width_mm:g}×{sheet.height_mm:g} مم"
    )
    render = latest_done(book.pk, PreviewRender.Scope.BOOK)
    live = LiveLayout.objects.filter(book_id=book.pk).only("page_count", "revision").first()
    rendering = PreviewRender.objects.filter(
        book_id=book.pk,
        scope=PreviewRender.Scope.BOOK,
        status__in=ACTIVE,
        created_at__gte=timezone.now() - ABANDONED_AFTER,
    ).exists()
    return {
        "trim": sheet.trim,
        "trim_label": label,
        "page_count": live.page_count
        if live is not None and live.revision
        else (render.page_count if render is not None else None),
        "rendering": rendering,
        "rendered_at": render.finished_at.isoformat() if render is not None and render.finished_at else None,
    }
