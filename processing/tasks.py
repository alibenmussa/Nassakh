"""Celery tasks of the processing stage: `preprocess_page` and `layout_page` (default queue).

Both are idempotent, bound, retry on I/O errors and return the `page_id` they received so they
can be chained. Failures that are not retryable are recorded with `Page.set_error` and the task
returns normally, so a group/chord over many pages is not aborted by one bad page; downstream
tasks skip pages that are in `error` from an earlier stage.

«التخطيط» (D64): while a book awaits «بدء المعالجة», `preprocess_page` refreshes the book after each
page (under the book's row lock), which gives the dashboard its live count and moves the book to
«تم التخطيط»; `layout_page` does nothing, since no region is written before the start.

One run per page at a time (`books.runs`, item 29): in a chain both carry the run's token (`run`), skip a
page another run holds now, and the chain's last task (`last`) gives the claim back.
"""

from __future__ import annotations

import logging

from celery import shared_task

from books import runs
from books.models import Page
from processing import services

logger = logging.getLogger(__name__)

RETRY_COUNTDOWN = 5


def _load_page(page_id: int) -> Page | None:
    """The page with its book, or None when it was deleted meanwhile."""
    return Page.objects.select_related("book").filter(pk=page_id).first()


def _remember_task(page: Page, task_id: str | None) -> None:
    """Store the running task's id on the page (the page detail shows it)."""
    page.task_id = task_id or ""
    page.save(update_fields=["task_id"])


def _failed_upstream(page: Page) -> bool:
    """True when an earlier task of the chain failed (`run_stage` clears errors before enqueuing),
    so this stage must not run on stale outputs nor overwrite that error."""
    return page.status == Page.Status.ERROR


def _superseded(name: str, page_id: int, run: str) -> bool:
    """True when the run `run` no longer holds the page (`books.runs`): this task must not work on it."""
    if runs.is_current(page_id, run):
        return False
    logger.info("%s(%s): skipped, run %s no longer holds the page", name, page_id, run)
    return True


@shared_task(bind=True, max_retries=2, autoretry_for=(OSError,), retry_backoff=True)
def preprocess_page(self, page_id: int, manual: dict | None = None, run: str = "", last: bool = False) -> int:
    """Deskew, clean, crop and binarise one page.

    `manual=None` (the chain) keeps overrides already stored on the page; a dict runs with exactly
    those overrides, so `{}` forces the automatic values (the API uses this for large pages). `run` is the
    page run's token (`books.runs`): a task of a run that no longer holds the page does nothing; `last`
    (the chain's last task) gives the claim back once the page is through.
    """
    if _superseded("preprocess_page", page_id, run):
        return page_id
    page = _preprocess(self, page_id, manual)  # raises only to be retried
    if last:
        runs.release(page_id, run)
    # after the release: a book this makes «تم التخطيط» can be started at once, its pages all free
    if page is not None and page.book.awaits_ocr_start:
        services.refresh_waiting_book(page.book_id)
    return page_id


def _preprocess(task, page_id: int, manual: dict | None) -> Page | None:
    """`preprocess_page`'s work: failures are recorded on the page; an I/O error is retried twice.
    Returns the page (None when it is gone or excluded)."""
    page = _load_page(page_id)
    if page is None or page.is_excluded:
        return None
    _remember_task(page, task.request.id)
    if manual is None:
        manual = services.stored_manual_params(page)
    try:
        services.preprocess_page(page, manual=manual)
    except OSError as exc:
        if task.request.retries < task.max_retries:
            raise task.retry(exc=exc, countdown=RETRY_COUNTDOWN * (task.request.retries + 1)) from exc
        logger.exception("preprocess_page(%s): I/O failure after retries", page_id)
        page.set_error(
            services.STAGE_PREPROCESS, "تعذّر الوصول إلى ملفات الصفحة. تحقّق من مجلد الوسائط ثم أعد التشغيل."
        )
    except services.ProcessingError as exc:
        logger.warning("preprocess_page(%s): %s", page_id, exc)
        page.set_error(services.STAGE_PREPROCESS, str(exc))
    except Exception:
        logger.exception("preprocess_page(%s) failed", page_id)
        page.set_error(
            services.STAGE_PREPROCESS,
            "فشل تجهيز الصفحة. افحص الصورة الأصلية ثم أعد تشغيل المرحلة من صفحة التفاصيل.",
        )
    return page


@shared_task(bind=True, max_retries=2, autoretry_for=(OSError,), retry_backoff=True)
def layout_page(self, page_id: int, run: str = "", last: bool = False) -> int:
    """Derive the page's regions from the effective guides; skipped for excluded or failed pages.

    Does nothing while the book awaits «بدء المعالجة» (D64): no region exists before the start. `run` /
    `last`: the page run's token and the chain's last task (`preprocess_page`).
    """
    if _superseded("layout_page", page_id, run):
        return page_id
    _layout(self, page_id)  # raises only to be retried
    if last:
        runs.release(page_id, run)
    return page_id


def _layout(task, page_id: int) -> None:
    """`layout_page`'s work: failures are recorded on the page; an I/O error is retried twice."""
    page = _load_page(page_id)
    if page is None or page.is_excluded or _failed_upstream(page):
        return
    if page.book.awaits_ocr_start:
        logger.info("layout_page(%s): skipped, book %s awaits «بدء المعالجة»", page_id, page.book_id)
        return
    _remember_task(page, task.request.id)
    try:
        services.derive_regions(page)
    except OSError as exc:
        if task.request.retries < task.max_retries:
            raise task.retry(exc=exc, countdown=RETRY_COUNTDOWN * (task.request.retries + 1)) from exc
        logger.exception("layout_page(%s): I/O failure after retries", page_id)
        page.set_error(
            services.STAGE_LAYOUT,
            "تعذّر الوصول إلى قاعدة البيانات أو الملفات أثناء تحديد المناطق. أعد التشغيل.",
        )
    except services.ProcessingError as exc:
        logger.warning("layout_page(%s): %s", page_id, exc)
        page.set_error(services.STAGE_LAYOUT, str(exc))
    except Exception:
        logger.exception("layout_page(%s) failed", page_id)
        page.set_error(services.STAGE_LAYOUT, "فشل تحديد مناطق الصفحة. راجع تخطيطها ثم أعد تشغيل المرحلة.")
