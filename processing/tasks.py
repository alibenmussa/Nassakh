"""Celery tasks of the processing stage: `preprocess_page` and `layout_page` (default queue).

Both are idempotent, bound, retry on I/O errors and return the `page_id` they received so they
can be chained. Failures that are not retryable are recorded with `Page.set_error` and the task
returns normally, so a group/chord over many pages is not aborted by one bad page; downstream
tasks skip pages that are in `error` from an earlier stage.
"""

from __future__ import annotations

import logging

from celery import shared_task

from books.models import Page
from processing import services

logger = logging.getLogger(__name__)

RETRY_COUNTDOWN = 5


def _load_page(page_id: int) -> Page | None:
    return Page.objects.select_related("book").filter(pk=page_id).first()


def _remember_task(page: Page, task_id: str | None) -> None:
    page.task_id = task_id or ""
    page.save(update_fields=["task_id"])


def _failed_upstream(page: Page) -> bool:
    """True when the page failed in preprocessing, so layout must not run on stale outputs."""
    return page.status == Page.Status.ERROR and page.error_from == services.STAGE_PREPROCESS


@shared_task(bind=True, max_retries=2, autoretry_for=(OSError,), retry_backoff=True)
def preprocess_page(self, page_id: int, manual: dict | None = None) -> int:
    """Deskew, clean, crop and binarise one page.

    `manual=None` (the chain) keeps overrides already stored on the page; a dict runs with exactly
    those overrides, so `{}` forces the automatic values (the API uses this for large pages).
    """
    page = _load_page(page_id)
    if page is None or page.is_excluded:
        return page_id
    _remember_task(page, self.request.id)
    if manual is None:
        manual = services.stored_manual_params(page)
    try:
        services.preprocess_page(page, manual=manual)
    except OSError as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=RETRY_COUNTDOWN * (self.request.retries + 1)) from exc
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
            "فشلت المعالجة الأولية للصفحة. افحص الصورة الأصلية ثم أعد تشغيل المرحلة من صفحة التفاصيل.",
        )
    return page_id


@shared_task(bind=True, max_retries=2, autoretry_for=(OSError,), retry_backoff=True)
def layout_page(self, page_id: int) -> int:
    """Derive the page's regions from the effective guides; skipped for excluded or failed pages."""
    page = _load_page(page_id)
    if page is None or page.is_excluded or _failed_upstream(page):
        return page_id
    _remember_task(page, self.request.id)
    try:
        services.derive_regions(page)
    except OSError as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=RETRY_COUNTDOWN * (self.request.retries + 1)) from exc
        logger.exception("layout_page(%s): I/O failure after retries", page_id)
        page.set_error(
            services.STAGE_LAYOUT, "تعذّر الوصول إلى قاعدة البيانات أو الملفات أثناء التخطيط. أعد التشغيل."
        )
    except services.ProcessingError as exc:
        logger.warning("layout_page(%s): %s", page_id, exc)
        page.set_error(services.STAGE_LAYOUT, str(exc))
    except Exception:
        logger.exception("layout_page(%s) failed", page_id)
        page.set_error(services.STAGE_LAYOUT, "فشل تخطيط الصفحة. راجع الأدلة ثم أعد تشغيل المرحلة.")
    return page_id
