"""Preview tasks (D44, D47): render the whole book or one chapter into pages, re-lay-out a chapter.

- `render_book_preview`, `render_chapter_preview` (queue `default`) run `publishing.preview.render_preview`,
  which records every failure on the render's row (Arabic headline), so a task never raises for a render
  error. `version`, when given, is the manuscript version the render was scheduled for after an editor
  save: the task does nothing when a later save happened.
- `relayout_chapter` (queue `layout`, `CELERY_TASK_ROUTES`: the fast re-layout must not wait behind page
  renders; the default worker consumes `default,layout`) runs `publishing.relayout.run_relayout`.
- `render_layout_images` (queue `default`, low priority) writes a finished re-layout's page images.
- `run_export` (queue `export`, PHASE6_SPEC §6.4; the CPU worker consumes `default,layout,export`) runs
  `publishing.exports.run_export`, which records every failure on the export's row. Its soft time limit is
  `NASSAKH["EXPORT_SOFT_LIMIT_S"]` (a timeout reads «استغرق الإخراج أطول من المسموح.»), the hard limit
  120 s above it.
"""

from __future__ import annotations

from celery import shared_task
from django.conf import settings

from books.models import Book

from . import preview

EXPORT_SOFT_LIMIT_S = int(settings.NASSAKH.get("EXPORT_SOFT_LIMIT_S", 1800))
EXPORT_HARD_MARGIN_S = 120


def _task_id(task) -> str:
    request = getattr(task, "request", None)
    return str(getattr(request, "id", "") or "")


@shared_task(bind=True)
def render_book_preview(self, book_id: int, version: int | None = None) -> int | None:
    """Render the book's current manuscript (cached by hash); returns the render id (None: nothing to do)."""
    book = Book.objects.filter(pk=book_id).first()
    if book is None:
        return None
    render = preview.render_preview(book, "book", None, version=version, task_id=_task_id(self))
    return render.pk if render is not None else None


@shared_task(bind=True)
def render_chapter_preview(self, book_id: int, chapter_id: str, version: int | None = None) -> int | None:
    """Render one chapter, its pages numbered from its place in the last full render."""
    book = Book.objects.filter(pk=book_id).first()
    if book is None:
        return None
    render = preview.render_preview(book, "chapter", chapter_id, version=version, task_id=_task_id(self))
    return render.pk if render is not None else None


@shared_task
def relayout_chapter(book_id: int, chapter_id: str, version: int, render_id: int) -> int | None:
    """Lay out one chapter again and splice it into the live layout (D47); returns the row id."""
    from . import relayout

    row = relayout.run_relayout(render_id)
    return row.pk if row is not None else None


@shared_task
def render_layout_images(render_id: int) -> int | None:
    """The page images of a finished re-layout (the filmstrip's thumbnails)."""
    from . import relayout

    row = relayout.render_layout_images(render_id)
    return row.pk if row is not None else None


@shared_task(
    bind=True, soft_time_limit=EXPORT_SOFT_LIMIT_S, time_limit=EXPORT_SOFT_LIMIT_S + EXPORT_HARD_MARGIN_S
)
def run_export(self, export_id: int) -> int | None:
    """Export a book into its file (D58, `publishing.exports.run_export`); returns the row id (None: no
    such row). A redelivered task (`acks_late`) with the same id starts the export again."""
    from . import exports

    row = exports.run_export(export_id, task_id=_task_id(self))
    return row.pk if row is not None else None
