"""Preview tasks (D44, D47): render the whole book or one chapter into pages, re-lay-out a chapter.

- `render_book_preview`, `render_chapter_preview` (queue `default`) run `publishing.preview.render_preview`,
  which records every failure on the render's row (Arabic headline), so a task never raises for a render
  error. `version`, when given, is the manuscript version the render was scheduled for after an editor
  save: the task does nothing when a later save happened.
- `relayout_chapter` (queue `layout`, `CELERY_TASK_ROUTES`: the fast re-layout must not wait behind page
  renders; the default worker consumes `default,layout`) runs `publishing.relayout.run_relayout`.
- `render_layout_images` (queue `default`, low priority) writes a finished re-layout's page images.
"""

from __future__ import annotations

from celery import shared_task

from books.models import Book

from . import preview


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
