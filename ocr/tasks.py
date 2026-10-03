"""OCR tasks (spec §7): `ocr_page_fast` (default queue), `ocr_page_full` and `warm_up_engines` (gpu queue),
`read_numbers` (default queue, D50: Kraken reads the Arabic-Indic numbers of a finalised page, then
its footnote call marks, D83).

Tasks call the services and return the `page_id` they received so they chain
(`layout_page → ocr_page_fast → ocr_page_full`). `OSError` is retried twice (network/storage
hiccups); every other failure marks the page `error` for its stage and does not re-raise, so a
broken page never blocks the rest of the book. A page already in `error` (an earlier task of the
chain failed) is skipped so the original error is kept, and so is every page of a book that awaits
«بدء المعالجة» (D64: the gate; books with OCR history all have the flag False). Engines stay loaded
between tasks through the registry cache; the gpu worker runs with `--pool=solo`.

One run per page at a time (`books.runs`, item 29): the chain's tasks carry the run's token (`run`) and
skip a page another run holds now; the last one (`last`) gives the claim back.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from celery import shared_task
from django.conf import settings

from books import runs
from books.models import Page

from . import runpod, services
from .engines import registry

log = logging.getLogger(__name__)

STAGE_FAST = "ocr_fast"
STAGE_FULL = "ocr_full"
HEADLINE_FAST = "تعذّر التعرّف السريع على النص (Tesseract)."
HEADLINE_FULL = "تعذّر التعرّف على النص بنماذج OCR."
HEADLINE_NO_GPU = "لم يتوفّر GPU في Runpod بعد عدة محاولات؛ أعد التعرّف على الصفحة لاحقًا."


def _run_stage(
    task,
    page_id: int,
    stage: str,
    action: Callable[[Page], None],
    headline: str,
    run: str = "",
    last: bool = False,
) -> int:
    """Shared task body of a page run's task (`books.runs`): nothing when the run `run` no longer holds the
    page; else `_stage_body`, then the chain's last task (`last`) gives the claim back. A retry (an I/O
    error) raises before the release, so the retried task still holds the page."""
    if not runs.is_current(page_id, run):
        log.info("%s: page %s skipped, run %s no longer holds it", stage, page_id, run)
        return page_id
    _stage_body(task, page_id, stage, action, headline)
    if last:
        runs.release(page_id, run)
    return page_id


def _stage_body(task, page_id: int, stage: str, action: Callable[[Page], None], headline: str) -> int:
    """Load the page, remember the task id, run `action`, record failures."""
    try:
        page = Page.objects.select_related("book").get(pk=page_id)
    except Page.DoesNotExist:
        log.warning("%s: page %s does not exist", stage, page_id)
        return page_id
    if page.is_excluded:
        return page_id
    if page.book.awaits_ocr_start:  # «التخطيط» (D64): nothing is read before «بدء المعالجة»
        log.info("%s: page %s skipped, book %s awaits «بدء المعالجة»", stage, page_id, page.book_id)
        return page_id
    if page.status == Page.Status.ERROR:
        # `run_stage` clears errors before enqueuing, so this is a failure earlier in the same
        # chain: keep its message (the retry button points at that stage) and do not run on stale
        # or missing inputs.
        log.info("%s: page %s skipped, it failed in %s", stage, page_id, page.error_from or "?")
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
    except runpod.RemoteNoCapacity as exc:
        # D104: Runpod had no free GPU for RUNPOD_QUEUE_WAIT_S; the page waits in the queue again
        # (RUNPOD_REQUEUE_S later, RUNPOD_REQUEUE_TIMES times) before it is an error
        retries = int(getattr(task.request, "retries", 0) or 0)
        times = int(settings.NASSAKH.get("RUNPOD_REQUEUE_TIMES", 6))
        if retries < times:
            countdown = int(settings.NASSAKH.get("RUNPOD_REQUEUE_S", 300))
            log.warning(
                "%s: page %s: no Runpod GPU, back to the queue in %s s (%s)", stage, page_id, countdown, exc
            )
            raise task.retry(exc=exc, countdown=countdown, max_retries=times) from exc
        log.warning("%s: page %s: no Runpod GPU after %d requeues", stage, page_id, retries)
        page.set_error(stage, f"{HEADLINE_NO_GPU}\n{exc}")
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
def ocr_page_fast(self, page_id: int, run: str = "", last: bool = False) -> int:
    """Tesseract provisional text for one page (default queue, about half a second per page)."""
    return _run_stage(self, page_id, STAGE_FAST, services.run_fast_ocr, HEADLINE_FAST, run, last)


@shared_task(bind=True, max_retries=2, autoretry_for=(OSError,), retry_backoff=True)
def ocr_page_full(self, page_id: int, run: str = "", last: bool = False) -> int:
    """Dual-model OCR and finalisation for one page (gpu queue; models stay resident)."""
    return _run_stage(self, page_id, STAGE_FULL, services.run_full_ocr, HEADLINE_FULL, run, last)


@shared_task(bind=True, max_retries=1, autoretry_for=(OSError,), retry_backoff=True)
def read_numbers(self, page_id: int, run: str = "") -> int:
    """The numbers pass of one finalised page (D50). A failure is logged and the page keeps Qari's
    numbers: the pass only ever improves a page, it never blocks it.

    `run` is the token of the page run that finalised the page (`numbers.schedule`). When another run holds
    the page by the time this pass is reached (a re-run started meanwhile), the pass is skipped: that run
    finalises the page again and queues its own pass, so a page is never read twice for one text."""
    from . import numbers

    holder = runs.active_token(page_id)
    if holder and holder != run:
        log.info("read_numbers: page %s skipped, a newer run (%s) holds it", page_id, holder)
        return page_id
    page = Page.objects.select_related("book", "preprocess").filter(pk=page_id).first()
    if page is None or page.is_excluded:
        return page_id
    try:
        done = numbers.read_page_numbers(page)
        log.info("read_numbers: page %s: %s", page_id, done.as_dict())
    except OSError:
        raise  # retried once
    except Exception:  # noqa: BLE001 - Qari's numbers stay; the log says why
        log.exception("read_numbers: page %s failed", page_id)
    if settings.NASSAKH.get("CALLS_PASS", True):
        from . import calls  # the call pass (D83): the raised «(١)» of a footnote call, read from the ink

        try:
            found = calls.read_page_calls(page)
            log.info("read_calls: page %s: %s", page_id, found.as_dict())
        except Exception:  # noqa: BLE001 - the page keeps its text; the log says why
            log.exception("read_calls: page %s failed", page_id)
    return page_id


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
