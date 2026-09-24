"""OCR tasks (spec §7): `ocr_page_fast` (default queue), `ocr_page_full` and `warm_up_engines` (gpu queue).

Tasks call the services and return the `page_id` they received so they chain
(`layout_page → ocr_page_fast → ocr_page_full`). `OSError` is retried twice (network/storage
hiccups); every other failure marks the page `error` for its stage and does not re-raise, so a
broken page never blocks the rest of the book. Engines stay loaded between tasks through the
registry cache; the gpu worker runs with `--pool=solo`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from celery import shared_task

from books.models import Page

from . import services
from .engines import registry

log = logging.getLogger(__name__)

STAGE_FAST = "ocr_fast"
STAGE_FULL = "ocr_full"
HEADLINE_FAST = "تعذّر التعرّف السريع على النص (Tesseract)."
HEADLINE_FULL = "تعذّر التعرّف على النص بنماذج OCR."


def _run_stage(task, page_id: int, stage: str, action: Callable[[Page], None], headline: str) -> int:
    """Shared task body: load the page, remember the task id, run `action`, record failures."""
    try:
        page = Page.objects.select_related("book").get(pk=page_id)
    except Page.DoesNotExist:
        log.warning("%s: page %s does not exist", stage, page_id)
        return page_id
    if page.is_excluded:
        return page_id

    task_id = getattr(task.request, "id", None) or ""
    if task_id and page.task_id != task_id:
        page.task_id = task_id
        page.save(update_fields=["task_id"])

    try:
        action(page)
    except services.OcrError as exc:
        log.warning("%s: page %s: %s", stage, page_id, exc)
        page.set_error(stage, str(exc))
    except OSError as exc:
        retries = int(getattr(task.request, "retries", 0) or 0)
        if retries >= int(task.max_retries or 0):
            log.exception("%s: page %s failed after %d retries", stage, page_id, retries)
            page.set_error(stage, f"{headline}\n{type(exc).__name__}: {exc}")
            return page_id
        raise  # retried by autoretry_for
    except Exception as exc:  # noqa: BLE001 - recorded on the page, the chain goes on
        log.exception("%s: page %s failed", stage, page_id)
        page.set_error(stage, f"{headline}\n{type(exc).__name__}: {exc}")
    return page_id


@shared_task(bind=True, max_retries=2, autoretry_for=(OSError,), retry_backoff=True)
def ocr_page_fast(self, page_id: int) -> int:
    """Tesseract provisional text for one page (default queue, about half a second per page)."""
    return _run_stage(self, page_id, STAGE_FAST, services.run_fast_ocr, HEADLINE_FAST)


@shared_task(bind=True, max_retries=2, autoretry_for=(OSError,), retry_backoff=True)
def ocr_page_full(self, page_id: int) -> int:
    """Dual-model OCR and finalisation for one page (gpu queue; models stay resident)."""
    return _run_stage(self, page_id, STAGE_FULL, services.run_full_ocr, HEADLINE_FULL)


@shared_task(bind=True)
def warm_up_engines(self) -> list[str]:
    """Load the primary, secondary and fast engines in the gpu worker so the first page is not slow."""
    loaded: list[str] = []
    for name in dict.fromkeys(services.engine_names()):
        try:
            registry.get_engine(name)
            loaded.append(name)
        except Exception:  # noqa: BLE001 - report what could be loaded, keep going
            log.exception("warm-up: engine %s could not be loaded", name)
    log.info("warm-up done: %s", ", ".join(loaded) or "nothing loaded")
    return loaded
