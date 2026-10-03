"""Live pages (PHASE5_SPEC §9.1, D47): the book's live layout and the fast re-layout of a chapter.

**The live layout** (`LiveLayout`) is what the book page draws: every page of the book as laid out by
WeasyPrint (`publishing.layout`), each with a reference to its page image. A finished book render becomes
the live layout (`adopt_book_render`) when nothing newer is live; between two book renders, each chapter
re-layout splices its pages in and moves the pages after it, writing a new revision
(`books/<id>/layout/live-<revision>.json`). `layout_payload` serves a page range of it (the API).

**The fast re-layout** (`request_relayout` → task `relayout_chapter` on the `layout` queue → `run_relayout`)
lays out only what an edit can have moved, without writing a PDF or page images (they follow in a
low-priority task, `render_layout_images`):

- *A book with chapter breaks* (every chapter starts a page): the chapter is laid out alone from its first
  page in the live layout (numbering and side from the chapters before it). When its page count changes,
  the pages after it shift by the delta and, on an odd delta, swap sides (mirrored margins move the text,
  an outer page number goes to the other corner); with recto openings the blank page before the next
  chapter appears or disappears instead, so the chapters after it keep their sides. A chapter split in
  two by a new heading, or merged with the next one, is laid out with them.
- *A long chapter* (`_window`, the owner's review of 2026-10-03): only the pages around the edit — from a
  page before it that opens a paragraph, a few pages at a time (`stop_block`), until a page after it
  opens with the same line as before (convergence), else to the chapter's end. Where the edit begins and
  ends comes from the record of the text the pages show (`blocks`: each chapter's blocks with a digest,
  written by every book render and re-layout). Book 41's chapter of 120 pages: 0.2–0.5 s an edit, 3–4 s
  before. Notes numbered through the chapter or the book, or no record: the whole chapter.
- *A book without chapter breaks* (sections run on): the window starts at the last page before the
  section whose first line is the first line of a paragraph, and is laid out forward section by section
  until a page after the edited section starts with the same line as before (convergence: from there the
  old pages are the same, shifted by the delta), or up to the end of the book.
- *A heading added, removed, renamed or re-levelled* (the contents page changes, `_with_front`): the
  chapter as above, then the front matter alone (scope `front`, the contents' numbers written in); a front
  that grows or shrinks moves every later page. The result is `full` (the client fetches its pages again).

Every result says what changed, so the client can renumber at once:

    {mode, from, to (the old pages replaced), count (the new pages, numbered from `from`), delta (what
     the pages after them add to their numbers), chapter_delta, shifted_from (old number of the first
     page after the replaced ones), flip (sides swap: odd delta), side_shift_pt, blank_changes:
     [{page, change: "added"|"removed"}], page_count, chapters, checks, revision: {before, after}, …}

A newer request for the same chapter cancels a queued one (its task is revoked); the re-layouts of a
book run one at a time (the live layout row is locked), and a task that finds a newer manuscript version
with a newer request waiting leaves the work to it. The whole book is rendered again in the background
once edits settle (`publishing.preview.schedule_after_edit`) and confirms the live layout.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from pathlib import Path

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from books.models import Book
from editor import document as doc
from editor.models import Manuscript

from .engine import RenderJob, get_engine
from .fonts import browser_font_css, resolve
from .layout import (
    assign_chapters,
    blank_page,
    body_lines,
    note_numbers,
    page_checks,
    page_top,
    refresh_contents,
    shift_pages,
    side_shift,
)
from .model import page_setup
from .models import LiveLayout, PreviewRender

log = logging.getLogger(__name__)

WINDOW_MIN_PAGES = 6  # a chapter on fewer pages is laid out whole (about as fast as a window)
WINDOW_PAGES = 3  # a window ends this many old pages past the edit's old end; each next one goes twice as far
WINDOW_MAX = 12  # windows of one re-layout before the whole chapter is laid out instead
LIVE_KEPT = 3  # live revision files kept per book (the newest; older ones are deleted)
LAYOUT_ROWS_KEPT = 40  # re-layout rows kept per book beyond those the live layout's pages refer to
MAX_RANGE = 120  # pages served by one layout request
DEFAULT_RANGE = 40
WAIT_STEP_S = 0.05
RELAYOUT_ERROR = "تعذّرت إعادة ترتيب صفحات الفصل؛ ستُحدَّث الصفحات مع إخراج الكتاب التالي."
SUPERSEDED = "superseded"


class LayoutNotFound(LookupError):
    """No manuscript, chapter or layout (API 404)."""


# ====================================================================== the live layout


_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_CACHE_KEPT = 6


def _abs(path: str) -> Path:
    return Path(settings.MEDIA_ROOT) / path


def read_json(path: str) -> dict | None:
    """A layout file under MEDIA_ROOT, parsed (cached by path, size and mtime; None when missing)."""
    full = _abs(path)
    try:
        stat = full.stat()
    except OSError:
        return None
    key = (str(full), stat.st_size, stat.st_mtime_ns)
    found = _CACHE.get(key)
    if found is not None:
        _CACHE.move_to_end(key)
        return found
    try:
        data = json.loads(full.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    _CACHE[key] = data
    while len(_CACHE) > _CACHE_KEPT:
        _CACHE.popitem(last=False)
    return data


def live_of(book_id: int) -> LiveLayout | None:
    """The book's live layout row when it holds a layout (None before the first one)."""
    live = LiveLayout.objects.filter(book_id=book_id).first()
    return live if live is not None and live.revision and live.path else None


def current_setup(book: Book):
    """The book's page setup now (`PageSetup`)."""
    from .preview import stylesheet_for

    return page_setup(stylesheet_for(book))


def adopt_book_render(render: PreviewRender) -> bool:
    """Make a finished book render the live layout when nothing newer is live: no live layout yet, one of
    another page setup, or one of an older (or the same) manuscript version. Returns True when adopted."""
    from .preview import layout_path

    path = f"{render.folder}/layout.json"
    data = read_json(path) if layout_path(render).is_file() else None
    if data is None:
        return False
    with transaction.atomic():
        live, _created = LiveLayout.objects.select_for_update().get_or_create(book_id=render.book_id)
        if live.revision and live.path == path:
            return False  # live already (a cached render asked for again)
        if live.revision and live.path and live.setup_hash == data.get("setup_hash"):
            if render.version < live.manuscript_version:
                return False  # a re-layout of a later edit is live; the next book render confirms it
        live.revision += 1
        live.path = path
        live.base = render
        live.page_count = int(data.get("page_count") or 0)
        live.chapters = list(data.get("chapters") or [])
        live.manuscript_version = render.version
        live.setup_hash = str(data.get("setup_hash") or "")
        live.save()
    _prune_live(render.book_id, keep=path)
    return True


def live_summary(book: Book, setup=None) -> dict | None:
    """The live layout in a few fields (the preview payload): `{revision, page_count, chapters,
    manuscript_version, stale, url}`; None before the first layout. `stale` when the manuscript is newer
    or the page setup (`setup`, the book's current one when not given) is another one."""
    from .preview import setup_hash

    live = live_of(book.pk)
    if live is None:
        return None
    version = Manuscript.objects.filter(book_id=book.pk).values_list("version", flat=True).first() or 0
    other_setup = live.setup_hash != setup_hash(setup if setup is not None else current_setup(book))
    return {
        "revision": live.revision,
        "page_count": live.page_count,
        "chapters": live.chapters,
        "manuscript_version": live.manuscript_version,
        "stale": version > live.manuscript_version or other_setup,
        "url": reverse("api:preview_layout", args=[book.pk]),
    }


def _image_urls(pages: list[dict]) -> None:
    """Set each page's `url` / `url2x` from its image reference (None while the images are not ready)."""
    from .preview import page_file

    ids = {page["src"]["render"] for page in pages if isinstance(page.get("src"), dict)}
    rows = {
        row.pk: row
        for row in PreviewRender.objects.filter(pk__in=ids, status=PreviewRender.Status.DONE).only(
            "id", "folder", "images"
        )
    }
    for page in pages:
        src = page.get("src") if isinstance(page.get("src"), dict) else {}
        row = rows.get(src.get("render"))
        if row is None or not row.images or not row.folder:
            page["url"] = page["url2x"] = None
            continue
        page["url"] = default_storage.url(f"{row.folder}/{page_file(src['index'])}")
        page["url2x"] = default_storage.url(f"{row.folder}/{page_file(src['index'], True)}")


def _range(pages: list[dict], first: int | None, last: int | None) -> tuple[list[dict], int, int]:
    if not pages:
        return [], 0, 0
    low, high = pages[0]["n"], pages[-1]["n"]
    first = low if first is None else max(low, first)
    last = min(high, first + DEFAULT_RANGE - 1) if last is None else min(high, last)
    last = min(last, first + MAX_RANGE - 1)
    chosen = [dict(page) for page in pages if first <= page["n"] <= last]
    return chosen, first, last


def layout_payload(
    book: Book,
    scope: str = "book",
    chapter_id: str | None = None,
    *,
    first: int | None = None,
    last: int | None = None,
    render_id: int | None = None,
) -> dict:
    """The layout API (`api:preview_layout`): pages `first`…`last` (printed numbers; 40 from the first page
    when not given, at most 120) of the live layout (scope `book`), of the chapter's newest render (scope
    `chapter`) or of one render of the book (`render_id`, e.g. a re-layout's new pages):

    `{scope, chapter, revision, render, page_count, first_page, from, to, chapters, checks, geometry,
    font_css, stale, manuscript_version, pages}` — each page as `publishing.layout` describes it, plus
    `chapter`, `url` / `url2x` (its image; null while not ready). Raises `LayoutNotFound`."""
    from .preview import latest_done, layout_path, setup_hash

    revision = None
    manuscript_version = None
    live_setup = None
    if render_id is not None:
        row = PreviewRender.objects.filter(
            pk=render_id, book_id=book.pk, status=PreviewRender.Status.DONE
        ).first()
        data = read_json(f"{row.folder}/layout.json") if row is not None and row.folder else None
        render = row.pk if row is not None else None
    elif scope == PreviewRender.Scope.CHAPTER:
        row = latest_done(book.pk, PreviewRender.Scope.CHAPTER, chapter_id or "")
        data = (
            read_json(f"{row.folder}/layout.json") if row is not None and layout_path(row).is_file() else None
        )
        render = row.pk if row is not None else None
    else:
        live = live_of(book.pk)
        data = read_json(live.path) if live is not None else None
        render = live.base_id if live is not None else None
        revision = live.revision if live is not None else None
        manuscript_version = live.manuscript_version if live is not None else None
        live_setup = live.setup_hash if live is not None else None
    if data is None:
        raise LayoutNotFound("لا توجد صفحات مرتّبة بعد؛ انتظر حتى يكتمل الإخراج.")
    pages, low, high = _range(list(data.get("pages") or []), first, last)
    _image_urls(pages)
    setup = current_setup(book)
    fonts = resolve(setup.body_font, setup.latin_font, setup.heading_font)
    current = Manuscript.objects.filter(book_id=book.pk).values_list("version", flat=True).first() or 0
    all_pages = data.get("pages") or []
    return {
        "scope": "render" if render_id is not None else scope,
        "chapter": chapter_id if scope == PreviewRender.Scope.CHAPTER else None,
        "revision": revision,
        "render": render,
        "page_count": len(all_pages),
        "first_page": all_pages[0]["n"] if all_pages else 1,
        "from": low,
        "to": high,
        "chapters": list(data.get("chapters") or []),
        "checks": list(data.get("checks") or []),
        "geometry": data.get("geometry") or {},
        "font_css": browser_font_css(fonts),
        # older than the manuscript, or laid out with another page setup (a book render is on its way)
        "stale": manuscript_version is not None
        and (current > manuscript_version or live_setup != setup_hash(setup)),
        "manuscript_version": manuscript_version,
        "pages": pages,
    }


def live_pages(book_id: int) -> list[dict]:
    """Every page of the live layout (empty before the first one)."""
    live = live_of(book_id)
    data = read_json(live.path) if live is not None else None
    return list((data or {}).get("pages") or [])


def _write_live(live: LiveLayout, data: dict, *, version: int, base_id: int | None = None) -> str:
    """Write a new revision of the live layout (a new file) and point the row at it; returns its path."""
    from .preview import write_json

    live.revision += 1
    path = f"books/{live.book_id}/layout/live-{live.revision:05d}.json"
    data = dict(data, revision=live.revision, page_count=len(data.get("pages") or []))
    write_json(_abs(path), data)
    live.path = path
    live.page_count = data["page_count"]
    live.chapters = data.get("chapters") or []
    live.manuscript_version = max(live.manuscript_version, version)
    live.setup_hash = str(data.get("setup_hash") or live.setup_hash)
    if base_id is not None:
        live.base_id = base_id
    live.save()
    return path


def _prune_live(book_id: int, keep: str) -> None:
    folder = _abs(f"books/{book_id}/layout")
    if not folder.is_dir():
        return
    files = sorted(folder.glob("live-*.json"))
    for path in files[:-LIVE_KEPT]:
        if not str(path).endswith(keep):
            path.unlink(missing_ok=True)


# ====================================================================== requests


def _manuscript(book: Book) -> Manuscript | None:
    return Manuscript.objects.filter(book_id=book.pk).only("id", "book_id", "document", "version").first()


def relayout_hash(book: Book, document: dict, chapter_id: str, setup) -> str:
    """What a re-layout of the chapter lays out: its nodes, the notes before it, the page setup."""
    from .preview import setup_hash

    content = doc.content_of(document)
    chapter = doc.find_chapter(document, chapter_id)
    nodes = chapter.nodes(document) if chapter is not None else []
    before = content[doc.preamble_end(content) : chapter.start] if chapter is not None else []
    notes_before = sum(1 for _b, note, _c in doc.iter_containers(before) if note is not None)
    raw = json.dumps(
        {"setup": setup_hash(setup), "chapter": chapter_id, "nodes": nodes, "notes_before": notes_before},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def request_relayout(book: Book, chapter_id: str, version: int | None = None) -> PreviewRender | None:
    """Ask for the fast re-layout of a chapter (D47); returns the row to poll, or None when the live
    layout already shows this version of the chapter (nothing to do). A queued re-layout of the same
    chapter is cancelled (its task revoked). Raises `LayoutNotFound` without a manuscript or chapter."""
    from .preview import revoke, setup_hash

    manuscript = _manuscript(book)
    if manuscript is None:
        raise LayoutNotFound("لا توجد مخطوطة بعد.")
    document = manuscript.document or {}
    chapter = doc.find_chapter(document, chapter_id)
    if chapter is None:
        raise LayoutNotFound("الفصل غير موجود.")
    setup = current_setup(book)
    live = live_of(book.pk)
    if live is not None and live.setup_hash == setup_hash(setup):
        chapter_version = doc.chapter_version(chapter.nodes(document))
        shown = next((item for item in live.chapters or [] if item.get("id") == chapter_id), None)
        if shown is not None and shown.get("version") == chapter_version:
            return None
    digest = relayout_hash(book, document, chapter_id, setup)
    with transaction.atomic():
        # the book row serialises the requests; not the live layout's, which a running re-layout holds for
        # its whole layout (a save would wait for it)
        Book.objects.select_for_update().filter(pk=book.pk).first()
        queued = PreviewRender.objects.filter(
            book_id=book.pk,
            kind=PreviewRender.Kind.LAYOUT,
            chapter_id=chapter_id,
            status=PreviewRender.Status.QUEUED,
        )
        for row in queued:
            if row.content_hash == digest and row.version == manuscript.version:
                return row  # the same request is waiting already
        row = PreviewRender.objects.create(
            book_id=book.pk,
            scope=PreviewRender.Scope.CHAPTER,
            kind=PreviewRender.Kind.LAYOUT,
            chapter_id=chapter_id,
            content_hash=digest,
            status=PreviewRender.Status.QUEUED,
            images=False,
            version=manuscript.version,
        )
        cancelled = PreviewRender.objects.filter(
            book_id=book.pk,
            kind=PreviewRender.Kind.LAYOUT,
            chapter_id=chapter_id,
            status=PreviewRender.Status.QUEUED,
        ).exclude(pk=row.pk)
        superseded = list(cancelled.values_list("task_id", flat=True))
        cancelled.update(status=PreviewRender.Status.CANCELLED, error=SUPERSEDED, finished_at=timezone.now())
    for task_id in superseded:
        revoke(task_id)
    _enqueue(row)
    row.refresh_from_db()
    return row


def _enqueue(row: PreviewRender) -> None:
    from . import tasks
    from .preview import ENQUEUE_ERROR

    try:
        result = tasks.relayout_chapter.delay(row.book_id, row.chapter_id, row.version, row.pk)
    except Exception as exc:  # noqa: BLE001 - reported on the row
        log.exception("re-layout %s of book %s could not be enqueued", row.pk, row.book_id)
        PreviewRender.objects.filter(pk=row.pk, status=PreviewRender.Status.QUEUED).update(
            status=PreviewRender.Status.ERROR,
            error=f"{ENQUEUE_ERROR}\n{type(exc).__name__}: {exc}"[:4000],
            finished_at=timezone.now(),
        )
        return
    task_id = getattr(result, "id", "") or ""
    if task_id:
        PreviewRender.objects.filter(pk=row.pk, task_id="").update(task_id=task_id[:64])


def relayout_payload(row: PreviewRender | None, *, pages: bool = True) -> dict:
    """The re-layout API's answer: `{id, status, chapter, version, result, pages, error, url}` — `result`
    and `pages` (the new pages, numbered from `result.from`) once done; `status` `done` with `result:
    {unchanged: true}` and no id when nothing had to move."""
    if row is None:
        return {"id": None, "status": "done", "result": {"unchanged": True}, "pages": [], "error": ""}
    out = {
        "id": row.pk,
        "status": row.status if row.error != SUPERSEDED else "superseded",
        "chapter": row.chapter_id,
        "version": row.version,
        "result": row.result or {},
        "pages": [],
        "error": (row.error or "").splitlines()[0] if row.status == PreviewRender.Status.ERROR else "",
        "url": reverse("api:relayout_status", args=[row.book_id, row.pk]),
    }
    if (
        pages
        and row.status == PreviewRender.Status.DONE
        and row.folder
        and not (row.result or {}).get("full")
    ):
        data = read_json(f"{row.folder}/layout.json") or {}
        chosen = [dict(page) for page in data.get("pages") or []]
        _image_urls(chosen)
        out["pages"] = chosen
    return out


def wait_for(row: PreviewRender, seconds: float) -> PreviewRender:
    """The row once it is no longer queued or running, or after `seconds` (the API's long poll)."""
    deadline = time.monotonic() + max(0.0, min(seconds, 5.0))
    while row.status in (PreviewRender.Status.QUEUED, PreviewRender.Status.RUNNING):
        if time.monotonic() >= deadline:
            break
        time.sleep(WAIT_STEP_S)
        row.refresh_from_db()
    return row


# ====================================================================== running


def run_relayout(row_id: int) -> PreviewRender | None:
    """The task's job: lay out the row's chapter and splice it into the live layout (see the module
    docstring). Every failure is recorded on the row; the live layout is then left as it was."""
    row = PreviewRender.objects.filter(pk=row_id).first()
    if row is None:
        return None
    if not PreviewRender.objects.filter(pk=row.pk, status=PreviewRender.Status.QUEUED).update(
        status=PreviewRender.Status.RUNNING
    ):
        row.refresh_from_db()
        return row
    started = time.monotonic()
    try:
        with transaction.atomic():
            live, _created = LiveLayout.objects.select_for_update().get_or_create(book_id=row.book_id)
            book = Book.objects.get(pk=row.book_id)
            manuscript = _manuscript(book)
            if manuscript is None or doc.find_chapter(manuscript.document or {}, row.chapter_id) is None:
                raise LayoutNotFound("الفصل غير موجود.")
            if manuscript.version > row.version and _newer_request(row):
                PreviewRender.objects.filter(pk=row.pk).update(
                    status=PreviewRender.Status.CANCELLED, error=SUPERSEDED, finished_at=timezone.now()
                )
                row.refresh_from_db()
                return row
            result = _relayout(book, manuscript, row, live)
            result["duration_ms"] = int((time.monotonic() - started) * 1000)
            PreviewRender.objects.filter(pk=row.pk).update(
                status=PreviewRender.Status.DONE,
                result=result,
                version=manuscript.version,
                page_count=result.get("count") or 0,
                first_page=result.get("from") or 1,
                chapters=result.get("chapters") or [],
                checks=result.get("checks") or [],
                passes=result.get("passes") or 1,
                duration_ms=result["duration_ms"],
                folder=_folder(row),
                error="",
                finished_at=timezone.now(),
            )
    except Exception as exc:  # noqa: BLE001 - reported on the row (designed failure, D22)
        log.exception("re-layout %s of book %s failed", row.pk, row.book_id)
        PreviewRender.objects.filter(pk=row.pk).update(
            status=PreviewRender.Status.ERROR,
            error=f"{RELAYOUT_ERROR}\n{type(exc).__name__}: {exc}"[:4000],
            finished_at=timezone.now(),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        row.refresh_from_db()
        return row
    row.refresh_from_db()
    _prune_rows(row.book_id)
    _images_later(row)
    return row


def _newer_request(row: PreviewRender) -> bool:
    return (
        PreviewRender.objects.filter(
            book_id=row.book_id,
            kind=PreviewRender.Kind.LAYOUT,
            chapter_id=row.chapter_id,
            status__in=(PreviewRender.Status.QUEUED, PreviewRender.Status.RUNNING),
            pk__gt=row.pk,
        )
        .exclude(pk=row.pk)
        .exists()
    )


def _folder(row: PreviewRender) -> str:
    return f"books/{row.book_id}/preview/layout-{row.pk}"


def _job(book: Book, document: dict, setup, **values) -> RenderJob:
    return RenderJob(
        document=document,
        stylesheet=setup,
        title=book.title,
        author=book.author,
        pdf=False,
        reuse=True,
        **values,
    )


def _with_src(pages: list[dict], row: PreviewRender, offset: int = 0) -> list[dict]:
    return [dict(page, src={"render": row.pk, "index": offset + i}) for i, page in enumerate(pages, start=1)]


def _digest(value) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def chapter_blocks(document: dict, chapter) -> list[list[str]]:
    """A chapter's blocks in order as `[id, digest]` (a blockquote's paragraphs one by one, each with its
    quote's own attributes): what a layout records of the text its pages show (the live layout's `blocks`),
    so a later re-layout finds where an edit begins and ends (`_window`)."""
    rows: list[list[str]] = []
    for node in chapter.nodes(document):
        if not isinstance(node, dict):
            continue
        if node.get("type") == doc.BLOCKQUOTE:
            frame = {key: value for key, value in node.items() if key != "content"}
            rows.extend(
                [doc.node_id(child), _digest([frame, child])]
                for child in node.get("content") or []
                if isinstance(child, dict)
            )
        else:
            rows.append([doc.node_id(node), _digest(node)])
    return rows


def block_digests(document: dict, chapter_ids=None) -> dict[str, list[list[str]]]:
    """`{chapter id: chapter_blocks}` of every chapter of `document` (or of those in `chapter_ids`)."""
    wanted = set(chapter_ids) if chapter_ids is not None else None
    return {
        chapter.id: chapter_blocks(document, chapter)
        for chapter in doc.chapters_of(document)
        if wanted is None or chapter.id in wanted
    }


def _relayout(book: Book, manuscript: Manuscript, row: PreviewRender, live: LiveLayout) -> dict:
    from .preview import geometry, setup_hash, write_json

    document = manuscript.document or {}
    setup = current_setup(book)
    digest = setup_hash(setup)
    data = read_json(live.path) if live.revision and live.path else None
    chapters = doc.chapters_of(document)
    before = live.revision
    if data is None or live.setup_hash != digest or not data.get("pages"):
        outcome = _full(book, document, setup, row)
    elif _contents_changed(book, document, setup, data["pages"]):
        # a heading added, removed or renamed: the chapter as usual and the front matter alone
        outcome = _with_front(book, document, setup, row, data, chapters) or _full(book, document, setup, row)
    elif all(chapter.kind == "section" for chapter in chapters):
        outcome = _sections(book, document, setup, row, data, chapters)
    else:
        outcome = _chapters(book, document, setup, row, data, chapters)
    new_pages = outcome.pop("new_pages")
    all_pages = [dict(page) for page in outcome.pop("all_pages")]  # never change the cached old layout
    ranges = outcome.pop("ranges")
    assign_chapters(all_pages, ranges)
    refresh_contents(all_pages)
    fonts = resolve(setup.body_font, setup.latin_font, setup.heading_font)
    checks = page_checks(all_pages, ranges, fonts.missing)
    # the text the pages show, per chapter: the chapters laid out now are the document's; the others keep
    # what was recorded (a chapter with an edit not laid out yet keeps its old record)
    if outcome.get("mode") == "book":
        blocks = block_digests(document)
    else:
        blocks = {key: value for key, value in (data.get("blocks") or {}).items()} if data else {}
        current = {chapter.id for chapter in chapters}
        laid = [row.chapter_id] if outcome.get("mode") == "sections" else outcome.get("laid_out") or []
        blocks = {key: value for key, value in blocks.items() if key in current and key not in laid}
        blocks.update(block_digests(document, laid))
    live_data = {
        "format": 1,
        "setup_hash": digest,
        "geometry": geometry(setup),
        "chapters": ranges,
        "checks": checks,
        "blocks": blocks,
        "pages": all_pages,
    }
    _write_live(live, live_data, version=manuscript.version)
    low = outcome["from"]
    high = low + len(new_pages) - 1
    own_checks = [check for check in checks if check.get("page") is None or low <= check["page"] <= high]
    write_json(
        _abs(_folder(row)) / "layout.json",
        {
            "format": 1,
            "render": row.pk,
            "kind": "layout",
            "scope": "chapter",
            "chapter": row.chapter_id,
            "first_page": low,
            "page_count": len(new_pages),
            "unit": "pt",
            "setup_hash": digest,
            "geometry": geometry(setup),
            "chapters": ranges,
            "checks": own_checks,
            "pages": [page for page in all_pages if low <= page["n"] <= high],
        },
    )
    outcome.update(
        count=len(new_pages),
        page_count=len(all_pages),
        chapters=ranges,
        checks=own_checks,
        side_shift_pt=side_shift(setup),
        revision={"before": before, "after": live.revision},
    )
    _prune_live(book.pk, keep=live.path)
    return outcome


def _contents_changed(book: Book, document: dict, setup, pages: list[dict]) -> bool:
    """True when the contents page would list other headings than the live layout's (a heading added,
    removed, renamed, or made a chapter title from a section title: its entry's indent): the front matter
    must be laid out again (`_with_front`)."""
    from .model import book_model

    if not setup.contents:
        return False
    wanted = [
        (entry.target, " ".join(entry.text.split()), f"contents-{min(entry.level, 2)}")
        for entry in book_model(document, setup, title=book.title, author=book.author).contents()
    ]
    shown: dict[str, list] = {}
    for page in pages:
        for line in page.get("lines") or []:
            if line.get("kind") == "contents" and line.get("block"):
                item = shown.setdefault(line["block"], [line.get("target"), [], line.get("style")])
                item[1].extend(run["text"] for run in line.get("runs") or [] if run["end"] > run["start"])
    have = [(target, " ".join(" ".join(texts).split()), style) for target, texts, style in shown.values()]
    return have != wanted


def _heading_pages(pages: list[dict]) -> dict[str, int]:
    """Block id → the page of its first line (what a contents entry prints for its heading)."""
    first: dict[str, int] = {}
    for page in pages:
        for line in body_lines(page):
            if line.get("block") and line.get("first"):
                first.setdefault(line["block"], page["n"])
    return first


def _with_front(book: Book, document: dict, setup, row: PreviewRender, data: dict, chapters) -> dict | None:
    """The contents page lists other headings now (a heading added, removed or renamed; the owner's review,
    2026-10-03: making a paragraph a chapter title laid the whole book out again): the chapter laid out as
    usual (`_chapters` — a window of a long chapter — or `_sections`), then the front matter alone
    (`scope="front"`, the contents' page numbers written in from the new pages). When the front matter takes
    more or fewer pages, every page after it moves by the difference and the front is laid out once more
    with the moved numbers. The client fetches the pages it shows again (`full`). None when the whole book
    is laid out instead: no chapter ranges, an odd difference with recto openings, a front matter that will
    not settle."""
    by_id = {item["id"]: item for item in data.get("chapters") or [] if isinstance(item, dict)}
    if not by_id:
        return None
    old = list(data["pages"])
    body_old = _body_start(old, by_id)
    if all(chapter.kind == "section" for chapter in chapters):
        body = _sections(book, document, setup, row, data, chapters)
    else:
        body = _chapters(book, document, setup, row, data, chapters)
    if body.get("mode") == "book":
        return body  # laid out whole already (notes numbered through the book)
    body_pages = [page for page in body["all_pages"] if page["n"] >= body_old]
    heads = _heading_pages(body_pages)
    shift = 0
    passes = body.get("passes") or 1
    for _attempt in range(2):
        rendered = get_engine().render(
            _job(
                book,
                document,
                setup,
                scope="front",
                contents_pages={block: n + shift for block, n in heads.items()},
            )
        )
        passes = max(passes, rendered.passes)
        front = _with_src(rendered.layout, row)
        start = len(front) + 1
        blank = bool(front) and setup.chapter_opening == "recto" and start % 2 == 0
        if blank:
            start += 1
        moved = start - body_old
        if moved == shift:
            break
        if moved % 2 and setup.chapter_opening == "recto":
            return None
        shift = moved
    else:
        return None
    blanks = [blank_page(len(front) + 1, front[-1])] if blank else []
    tail = shift_pages(body_pages, shift, setup) if shift else body_pages
    ranges = [
        dict(item, first=item["first"] + shift, last=item["last"] + shift) if shift else dict(item)
        for item in body["ranges"]
    ]
    all_pages = front + blanks + tail
    return {
        "mode": "front",
        "full": True,  # the client fetches the pages it shows again: the front and the body both moved
        "laid_out": body.get("laid_out") or [row.chapter_id],
        "from": 1,
        "to": old[-1]["n"] if old else 0,
        "delta": len(all_pages) - len(old),
        "chapter_delta": body.get("chapter_delta", 0),
        "shifted_from": None,
        "flip": False,
        "blank_changes": [],
        "front_pages": len(front) + len(blanks),
        "front_shift": shift,
        "body": {
            key: body[key] for key in ("mode", "from", "to", "windows", "rendered_pages") if key in body
        },
        "passes": passes,
        "job": body.get("job"),
        "new_pages": front + blanks,
        "all_pages": all_pages,
        "ranges": ranges,
    }


def _full(book: Book, document: dict, setup, row: PreviewRender) -> dict:
    """No live layout to splice into (none yet, or of another page setup): the whole book, laid out."""
    rendered = get_engine().render(_job(book, document, setup, scope="book"))
    pages = _with_src(rendered.layout, row)
    old = live_of(book.pk)
    return {
        "mode": "book",
        "full": True,
        "from": 1,
        "to": old.page_count if old is not None else 0,
        "delta": len(pages) - (old.page_count if old is not None else 0),
        "chapter_delta": 0,
        "shifted_from": None,
        "flip": False,
        "blank_changes": [],
        "passes": rendered.passes,
        "job": {"scope": "book", "first_page": 1},
        "new_pages": pages,
        "all_pages": pages,
        "ranges": rendered.chapters,
    }


def _seed(pages: list[dict]) -> dict[str, str]:
    return note_numbers(pages)


def _chapters(book, document, setup, row, data, chapters) -> dict:
    """A book with chapter breaks: the chapter (and the chapters a split made of it) from its page."""
    old = list(data["pages"])
    by_id = {item["id"]: item for item in data.get("chapters") or [] if isinstance(item, dict)}
    ids = [chapter.id for chapter in chapters]
    index = ids.index(row.chapter_id)
    render_ids = [row.chapter_id]
    j = index + 1
    while j < len(ids) and ids[j] not in by_id:
        render_ids.append(ids[j])
        j += 1
    following = ids[j] if j < len(ids) else None
    if row.chapter_id in by_id:
        low = by_id[row.chapter_id]["first"]
    else:
        previous = next((by_id[item] for item in reversed(ids[:index]) if item in by_id), None)
        low = previous["last"] + 1 if previous is not None else _body_start(old, by_id)
    high = by_id[following]["first"] - 1 if following is not None else old[-1]["n"]
    high = max(high, low - 1)
    if render_ids == [row.chapter_id] and row.chapter_id in by_id:
        # a long chapter: only the pages around the edit, when the record of the pages' text allows it
        windowed = _window(book, document, setup, row, data, chapters, low, high, following is not None)
        if windowed is not None:
            return windowed
    replaced = [page for page in old if low <= page["n"] <= high]
    rendered = get_engine().render(
        _job(
            book,
            document,
            setup,
            scope="chapter",
            chapter_ids=tuple(render_ids),
            first_page=low,
            seed=_seed(replaced),
        )
    )
    # chapters left with nothing to print have no page in the book (WeasyPrint still gives an empty
    # document one blank page): no anchor laid out, no pages
    new_pages = _with_src(rendered.layout, row) if rendered.chapters else []
    if setup.footnote_numbering == "book" and _calls(new_pages) != _calls(replaced):
        return _full(book, document, setup, row)  # numbered through the book: every later number moves
    own = len(new_pages)
    if setup.chapter_opening == "recto" and following is not None and new_pages and (low + own) % 2 == 0:
        new_pages.append(blank_page(low + own, new_pages[-1]))
    delta = len(new_pages) - len(replaced)
    old_own = len(replaced) - (1 if replaced and replaced[-1].get("blank") else 0)
    changes = []
    old_blank = bool(replaced) and bool(replaced[-1].get("blank"))
    new_blank = bool(new_pages) and bool(new_pages[-1].get("blank"))
    if old_blank and not new_blank:
        changes.append({"page": high, "change": "removed"})
    if new_blank and not old_blank:
        changes.append({"page": low + len(new_pages) - 1, "change": "added"})
    head = [page for page in old if page["n"] < low]
    tail = shift_pages([page for page in old if page["n"] > high], delta, setup)
    all_pages = head + new_pages + tail
    rendered_ranges = {item["id"]: item for item in rendered.chapters}
    if rendered.chapters and new_pages:
        last_id = rendered.chapters[-1]["id"]
        rendered_ranges[last_id] = dict(rendered_ranges[last_id], last=low + len(new_pages) - 1)
    ranges = []
    for chapter in chapters:
        if chapter.id in rendered_ranges:
            ranges.append(rendered_ranges[chapter.id])
        elif chapter.id in by_id and chapter.id not in render_ids:
            item = dict(by_id[chapter.id])
            if item["first"] > high:
                item.update(first=item["first"] + delta, last=item["last"] + delta)
            ranges.append(item)
    return {
        "mode": "chapter",
        "full": False,
        "laid_out": render_ids,
        "from": low,
        "to": high,
        "delta": delta,
        "chapter_delta": own - old_own,
        "shifted_from": high + 1,
        "flip": delta % 2 != 0,
        "blank_changes": changes,
        "passes": rendered.passes,
        "job": {"scope": "chapter", "chapter_ids": render_ids, "first_page": low},
        "new_pages": new_pages,
        "all_pages": all_pages,
        "ranges": ranges,
    }


def _changed_span(old: list, new: list) -> tuple[int, int, int] | None:
    """Where `new` differs from `old` (two `chapter_blocks` lists): `(start, new_end, old_end)` — the length
    of their common head, and where their common tail begins in each; None when they are equal."""
    if old == new:
        return None
    start = 0
    limit = min(len(old), len(new))
    while start < limit and old[start] == new[start]:
        start += 1
    old_end, new_end = len(old), len(new)
    while old_end > start and new_end > start and old[old_end - 1] == new[new_end - 1]:
        old_end -= 1
        new_end -= 1
    return start, new_end, old_end


def _window(book, document, setup, row, data, chapters, low: int, high: int, followed: bool) -> dict | None:
    """A long chapter with chapter breaks (the owner's review, 2026-10-03: lay out only the pages around an
    edit): laid out from a page before the edit a few pages at a time, until a page after it opens with the
    same line as before (convergence: from there the old pages stay, renumbered by the delta), as `_sections`
    does for a book without breaks. Where the edit begins and ends comes from the live layout's record of the
    text its pages show (`blocks`). None when the whole chapter is laid out instead (`_chapters`): a short
    chapter, notes numbered through the chapter or the book, no record, nothing changed, no page to start
    from, an odd delta with recto openings (the blank page before the next chapter changes), no convergence
    before the chapter's end, or too many windows."""
    if setup.footnote_numbering != "page" or high - low + 1 < WINDOW_MIN_PAGES:
        return None
    chapter = next((item for item in chapters if item.id == row.chapter_id), None)
    recorded = (data.get("blocks") or {}).get(row.chapter_id)
    if chapter is None or not recorded:
        return None
    current = chapter_blocks(document, chapter)
    span = _changed_span(recorded, current)
    if span is None:
        return None
    start, new_end, old_end = span
    old = list(data["pages"])
    by_n = {page["n"]: page for page in old}
    mine = [page for page in old if low <= page["n"] <= high]
    starts: dict[str, int] = {}  # block id → the old page of its first line
    for page in mine:
        for line in body_lines(page):
            if line.get("first") and line.get("block"):
                starts.setdefault(line["block"], page["n"])
    # the window opens on the page of the block two before the edit (a heading kept with the next block moves
    # with it), back to a page whose first line opens a paragraph
    begin = low
    for index in range(max(0, start - 2), -1, -1):
        if recorded[index][0] in starts:
            begin = starts[recorded[index][0]]
            break
    while begin > low:
        lines = body_lines(by_n[begin]) if begin in by_n else []
        if lines and lines[0].get("first"):
            break
        begin -= 1
    # the unchanged tail (one block more: a block's scan page mark depends on the block before it): a page
    # that opens inside it with the same line as before is where the old pages take over
    tail_new = min(len(current), new_end + 1)
    tail_old = min(len(recorded), old_end + 1)
    settled_ids = {block_id for block_id, _digest_value in current[tail_new:]}
    tail_page = next((starts[item[0]] for item in recorded[tail_old:] if item[0] in starts), None)
    old_tops: dict[tuple, int] = {}
    for page in mine:
        top = page_top(page)
        if page["n"] > begin and top is not None and top[0] in settled_ids:
            old_tops.setdefault(top, page["n"])

    def stop_after(page_n: int) -> str | None:
        """The first block of the unchanged tail whose first line is on old page `page_n` or later."""
        return next(
            (item[0] for item in recorded[tail_old:] if item[0] in starts and starts[item[0]] >= page_n), None
        )

    lines = body_lines(by_n[begin]) if begin in by_n else []
    start_block = lines[0]["block"] if begin > low and lines else None
    first_job = {
        "scope": "window" if start_block else "chapter",
        "chapter_window": True,
        "chapter_ids": [row.chapter_id],
        "start_block": start_block,
        "continues": bool(start_block),
        "first_page": begin,
    }
    kept: list[dict] = []
    page_from = begin
    reach = (tail_page if tail_page is not None else begin) + WINDOW_PAGES
    stats = {"windows": 0, "rendered_pages": 0, "passes": 1}
    stop: str | None = None
    for attempt in range(WINDOW_MAX):
        stop = stop_after(reach) if tail_page is not None else None
        values = (
            {"scope": "window", "start_block": start_block, "continues": True}
            if start_block
            else {"scope": "chapter"}
        )
        rendered = get_engine().render(
            _job(
                book,
                document,
                setup,
                chapter_ids=(row.chapter_id,),
                first_page=page_from,
                seed=_seed([page for page in old if page["n"] >= page_from]),
                stop_block=stop,
                **values,
            )
        )
        stats["windows"] += 1
        stats["passes"] = max(stats["passes"], rendered.passes)
        new = _with_src(rendered.layout, row, offset=len(kept))
        stats["rendered_pages"] += len(new)
        settled = new[:-1] if stop else new  # the page the cut reaches is not the book's
        for position, page in enumerate(settled):
            top = page_top(page)
            if position == 0 or top is None or top[0] not in settled_ids:
                continue
            old_n = old_tops.get(top)
            if old_n is None:
                continue
            delta = page["n"] - old_n
            if delta % 2 and setup.chapter_opening == "recto" and followed:
                return None
            # converged: the old pages from `old_n` on (the rest of the chapter, the later ones) renumbered
            return _window_outcome(
                data,
                chapters,
                chapter,
                document,
                setup,
                old,
                begin,
                high,
                kept + new[:position],
                old_n,
                delta,
                {**stats, "converged": True, "job": {**first_job, "stop_block": stop}},
            )
        if not stop:
            # the chapter's end reached without meeting the old pages: the chapter's new end, as `_chapters`
            # makes it (the blank page before a recto opening comes or goes), the later chapters renumbered
            segment = kept + new
            own = begin - low + len(segment)  # the chapter's pages now (without a blank one)
            if setup.chapter_opening == "recto" and followed and segment and (low + own) % 2 == 0:
                segment = [*segment, blank_page(low + own, segment[-1])]
            delta = (begin - low + len(segment)) - (high - low + 1)
            old_blank = bool(mine) and bool(mine[-1].get("blank"))
            new_blank = bool(segment) and bool(segment[-1].get("blank"))
            changes = []
            if old_blank and not new_blank:
                changes.append({"page": high, "change": "removed"})
            if new_blank and not old_blank:
                changes.append({"page": begin + len(segment) - 1, "change": "added"})
            old_own = high - low + 1 - (1 if old_blank else 0)
            return _window_outcome(
                data,
                chapters,
                chapter,
                document,
                setup,
                old,
                begin,
                high,
                segment,
                high + 1,
                delta,
                {**stats, "converged": False, "job": {**first_job, "stop_block": None}},
                chapter_delta=own - old_own,
                blank_changes=changes,
            )
        # go on from the last settled page that opens a paragraph (nothing before it changes any more); with
        # none, from the same start, farther
        for position in range(len(settled) - 1, 0, -1):
            opening = body_lines(settled[position])
            if opening and opening[0].get("first"):
                kept += new[:position]
                start_block = opening[0]["block"]
                page_from = settled[position]["n"]
                break
        known = starts.get(start_block) if start_block else None
        reach = max(reach, known if known is not None else reach) + WINDOW_PAGES * 2 ** (attempt + 1)
    return None


def _window_outcome(
    data,
    chapters,
    chapter,
    document,
    setup,
    old,
    begin,
    high,
    segment,
    shifted_from,
    delta,
    extra,
    *,
    chapter_delta=None,
    blank_changes=(),
) -> dict:
    """A window's result (`_window`): the old pages before `begin`, the new `segment`, the old pages from
    `shifted_from` on renumbered by `delta`; the chapter's range ends `delta` later, later chapters move."""
    tail = shift_pages([item for item in old if item["n"] >= shifted_from], delta, setup)
    ids = {item.id for item in chapters}
    ranges = []
    for item in data.get("chapters") or []:
        if not isinstance(item, dict) or item.get("id") not in ids:
            continue
        if item["id"] == chapter.id:
            ranges.append(
                dict(item, last=item["last"] + delta, version=doc.chapter_version(chapter.nodes(document)))
            )
        elif item["first"] > high:
            ranges.append(dict(item, first=item["first"] + delta, last=item["last"] + delta))
        else:
            ranges.append(dict(item))
    return {
        "mode": "window",
        "full": False,
        "laid_out": [chapter.id],
        "from": begin,
        "to": shifted_from - 1,
        "delta": delta,
        "chapter_delta": delta if chapter_delta is None else chapter_delta,
        "shifted_from": shifted_from,
        "flip": delta % 2 != 0,
        "blank_changes": list(blank_changes),
        **extra,
        "new_pages": segment,
        "all_pages": [item for item in old if item["n"] < begin] + segment + tail,
        "ranges": ranges,
    }


def _body_start(pages: list[dict], by_id: dict) -> int:
    firsts = [item["first"] for item in by_id.values() if isinstance(item.get("first"), int)]
    return min(firsts) if firsts else pages[0]["n"]


def _section_blocks(document: dict, chapters) -> tuple[dict[str, int], list[list[str]]]:
    """Block id → its section's index, and each section's block ids (blockquote paragraphs included)."""
    of: dict[str, int] = {}
    blocks: list[list[str]] = []
    for index, chapter in enumerate(chapters):
        ids = []
        for node in doc.flat_blocks(chapter.nodes(document)):
            block_id = doc.node_id(node)
            if block_id:
                ids.append(block_id)
                of.setdefault(block_id, index)
        blocks.append(ids)
    return of, blocks


def _calls(pages: list[dict]) -> int:
    return sum(
        1
        for page in pages
        for line in body_lines(page)
        for run in line.get("runs") or []
        if run.get("note") and run.get("sup")
    )


def _sections(book, document, setup, row, data, chapters) -> dict:
    """A book without chapter breaks: forward from a page that opens with a paragraph, section by section,
    until a later page opens with the same line as before (convergence) or the book ends."""
    old = list(data["pages"])
    by_n = {page["n"]: page for page in old}
    ids = [chapter.id for chapter in chapters]
    edited = ids.index(row.chapter_id)
    section_of, section_blocks = _section_blocks(document, chapters)
    low = _window_start(old, by_n, data, row.chapter_id, edited, section_of, section_blocks)
    if low is None:
        return _full(book, document, setup, row)
    start_block = body_lines(by_n[low])[0]["block"]
    first_block = next((block for blocks in section_blocks for block in blocks), None)
    old_tops: dict[tuple, int] = {}
    for page in old:
        top = page_top(page)
        if page["n"] > low and top is not None:
            old_tops.setdefault(top, page["n"])
    numbering_book = setup.footnote_numbering == "book"
    calls_head = _calls([page for page in old if page["n"] < low])
    kept: list[dict] = []
    last = edited
    stats = {"windows": 0, "rendered_pages": 0, "passes": 1}

    def outcome(high: int, segment: list[dict], tail: list[dict], delta: int, converged: bool) -> dict:
        all_pages = [page for page in old if page["n"] < low] + segment + tail
        return {
            "mode": "sections",
            "full": False,
            "laid_out": ids[section_of.get(body_lines(by_n[low])[0]["block"], edited) : last + 1],
            "from": low,
            "to": high,
            "delta": delta,
            "chapter_delta": delta,
            "shifted_from": high + 1,
            "flip": delta % 2 != 0,
            "blank_changes": [],
            "converged": converged,
            **stats,
            "job": {"scope": "window", "first_page": low},
            "new_pages": segment,
            "all_pages": all_pages,
            "ranges": _section_ranges(all_pages, chapters, section_blocks, document),
        }

    while True:
        page_from = low + len(kept)
        rendered = get_engine().render(
            _job(
                book,
                document,
                setup,
                scope="window",
                chapter_ids=tuple(ids[section_of.get(start_block, edited) : last + 1]),
                start_block=start_block,
                first_page=page_from,
                seed=_seed([page for page in old if page["n"] >= page_from]),
                continues=start_block != first_block,
            )
        )
        stats["windows"] += 1
        stats["passes"] = max(stats["passes"], rendered.passes)
        new = _with_src(rendered.layout, row, offset=len(kept))
        stats["rendered_pages"] += len(new)
        at_end = last == len(ids) - 1
        settled = new if at_end else new[:-1]  # the last page of a window may go on with the next section
        for position, page in enumerate(settled):
            top = page_top(page)
            if position == 0 or top is None or section_of.get(top[0], -1) <= edited:
                continue
            old_n = old_tops.get(top)
            if old_n is None:
                continue
            if numbering_book and calls_head + _calls(kept + new[:position]) != _calls(
                [item for item in old if item["n"] < old_n]
            ):
                continue  # numbered through the book: the notes before this page must be as many as before
            delta = page["n"] - old_n
            tail = shift_pages([item for item in old if item["n"] >= old_n], delta, setup)
            return outcome(old_n - 1, kept + new[:position], tail, delta, True)
        if at_end:
            segment = kept + new
            return outcome(old[-1]["n"], segment, [], len(segment) - (old[-1]["n"] - low + 1), False)
        # go on from the last settled page that opens with a paragraph (nothing before it can change)
        for position in range(len(settled) - 1, 0, -1):
            lines = body_lines(settled[position])
            if lines and lines[0].get("first") and lines[0].get("block") in section_of:
                kept += new[:position]
                start_block = lines[0]["block"]
                break
        last += 1


def _window_start(old, by_n, data, chapter_id, edited, section_of, section_blocks) -> int | None:
    """The page the forward re-layout starts from: the last page at or before the edited section's first
    line whose first line opens a paragraph of the edited section or of one before it."""
    own = set(section_blocks[edited])
    pages_with = [page["n"] for page in old if any(line.get("block") in own for line in body_lines(page))]
    shown = next((item for item in data.get("chapters") or [] if item.get("id") == chapter_id), None)
    if pages_with:
        anchor = pages_with[0]
    elif shown is not None and isinstance(shown.get("first"), int):
        anchor = shown["first"]
    else:
        before = {block for blocks in section_blocks[:edited] for block in blocks}
        earlier = [page["n"] for page in old if any(line.get("block") in before for line in body_lines(page))]
        anchor = earlier[-1] if earlier else old[0]["n"]
    n = anchor
    while n >= old[0]["n"]:
        lines = body_lines(by_n[n]) if n in by_n else []
        if (
            lines
            and lines[0].get("first")
            and section_of.get(lines[0].get("block"), len(section_blocks)) <= edited
        ):
            return n
        n -= 1
    return None


def _section_ranges(pages: list[dict], chapters, blocks: list[list[str]], document: dict) -> list[dict]:
    """`[{id, title, first, last, version}]` of run-on sections from the pages: a section from the page of
    its first line to the page where the next one starts (they share it) or the last page."""
    first_of: dict[str, int] = {}
    for page in pages:
        for line in body_lines(page):
            block = line.get("block")
            if block is not None:
                first_of.setdefault(block, page["n"])
    starts = []
    for chapter, ids in zip(chapters, blocks, strict=True):
        found = [first_of[block] for block in ids if block in first_of]
        if found:
            starts.append((chapter, min(found)))
    out = []
    last_n = pages[-1]["n"] if pages else 0
    for i, (chapter, first) in enumerate(starts):
        last = starts[i + 1][1] if i + 1 < len(starts) else last_n
        out.append(
            {
                "id": chapter.id,
                "title": chapter.title,
                "first": first,
                "last": max(first, last),
                "version": doc.chapter_version(chapter.nodes(document)),
            }
        )
    return out


# ====================================================================== images and pruning


def _images_later(row: PreviewRender) -> None:
    """Queue the page images of a finished re-layout (low priority; the filmstrip waits for them)."""
    if row.status != PreviewRender.Status.DONE or (row.result or {}).get("full"):
        return
    from . import tasks

    try:
        tasks.render_layout_images.apply_async((row.pk,), priority=9)
    except Exception:  # noqa: BLE001 - the pages show without their thumbnails
        log.warning("could not queue the images of re-layout %s", row.pk, exc_info=True)


def render_layout_images(row_id: int) -> PreviewRender | None:
    """Write the page images of a finished re-layout into its folder: the same job laid out again with the
    footnote numbers it showed (one pass) and written as a PDF, then rasterised. Skipped when the chapter
    changed since (a newer re-layout has its own images)."""
    from .preview import rasterize

    row = PreviewRender.objects.filter(pk=row_id, kind=PreviewRender.Kind.LAYOUT).first()
    if row is None or row.status != PreviewRender.Status.DONE or row.images:
        return row
    book = Book.objects.filter(pk=row.book_id).first()
    manuscript = _manuscript(book) if book is not None else None
    if manuscript is None or manuscript.version != row.version:
        return row
    job = dict((row.result or {}).get("job") or {})
    data = read_json(f"{row.folder}/layout.json") or {}
    pages = data.get("pages") or []
    if not pages or job.get("scope") not in ("chapter", "window"):
        return row
    setup = current_setup(book)
    document = manuscript.document or {}
    values = {"scope": job["scope"], "first_page": int(job.get("first_page") or 1)}
    if job.get("chapter_window"):
        # a window of a long chapter (`_window`): the same start, to its last window's stop
        values.update(
            chapter_ids=tuple(job.get("chapter_ids") or [row.chapter_id]), stop_block=job.get("stop_block")
        )
        if job["scope"] == "window":
            values.update(start_block=job.get("start_block"), continues=bool(job.get("continues")))
    elif job["scope"] == "chapter":
        values["chapter_ids"] = tuple(job.get("chapter_ids") or [row.chapter_id])
    else:
        chapters = doc.chapters_of(document)
        section_of, _blocks = _section_blocks(document, chapters)
        start_block = body_lines(pages[0])[0]["block"] if body_lines(pages[0]) else None
        last_block = next(
            (line["block"] for page in reversed(pages) for line in reversed(body_lines(page))), None
        )
        first = section_of.get(start_block, 0)
        end = section_of.get(last_block, len(chapters) - 1)
        opening = next((block for blocks in _blocks for block in blocks), None)
        values.update(
            chapter_ids=tuple(c.id for c in chapters[first : end + 1]),
            start_block=start_block,
            continues=start_block != opening,
        )
    job_ = RenderJob(
        document=document,
        stylesheet=setup,
        title=book.title,
        author=book.author,
        seed=note_numbers(pages),
        **values,
    )
    rendered = get_engine().render(job_)
    folder = _abs(row.folder)
    count = min(len(pages), rendered.page_count)
    trimmed = _first_pages(rendered.pdf, count)
    rasterize(trimmed, folder)
    PreviewRender.objects.filter(pk=row.pk).update(images=True)
    row.refresh_from_db()
    return row


def _first_pages(pdf: bytes, count: int) -> bytes:
    """The first `count` pages of a PDF (a window laid out past its last settled page)."""
    import pymupdf

    document = pymupdf.open(stream=pdf, filetype="pdf")
    try:
        if document.page_count <= count:
            return pdf
        document.select(list(range(count)))
        return document.tobytes()
    finally:
        document.close()


def _prune_rows(book_id: int) -> None:
    """Delete old re-layout rows (and folders) that the live layout's pages no longer refer to."""
    import shutil

    used = {page["src"]["render"] for page in live_pages(book_id) if isinstance(page.get("src"), dict)}
    rows = list(
        PreviewRender.objects.filter(book_id=book_id, kind=PreviewRender.Kind.LAYOUT)
        .exclude(status__in=(PreviewRender.Status.QUEUED, PreviewRender.Status.RUNNING))
        .order_by("-created_at", "-id")
        .only("id", "folder")[LAYOUT_ROWS_KEPT:]
    )
    drop = [row for row in rows if row.pk not in used]
    for row in drop:
        if row.folder:
            shutil.rmtree(_abs(row.folder), ignore_errors=True)
    if drop:
        PreviewRender.objects.filter(pk__in=[row.pk for row in drop]).delete()
