"""Books tasks: ingest the PDF, continue (or pause) after preprocessing, start «المعالجة», re-run a book.

Tasks call `books.services` and return the `book_id` they received. Tasks of the other apps
(processing, ocr) are imported lazily inside the function bodies.

«التخطيط» (D64): a book made by `create_book` awaits «بدء المعالجة»; its chord callback pauses at
«تم التخطيط», and `start_ocr_task` sends the prepared pages on once the owner starts. Books with the
flag False (every older book and fixture) take today's path.
"""

from __future__ import annotations

import logging

from celery import chord, group, shared_task
from django.core.exceptions import ObjectDoesNotExist

from books import services
from books.models import Book, Page

log = logging.getLogger(__name__)

INGEST_ERROR = (
    "تعذّر استخراج الصفحات من الملف. تأكد أن الملف PDF سليم وغير محمي، ثم أعد المحاولة من الزر أعلى الصفحة."
)
NO_PAGES_ERROR = "لم تُستخرج أي صفحة. راجع قيم تجاوز الصفحات الأولى والأخيرة ثم أعد المحاولة."


@shared_task(bind=True, max_retries=2, autoretry_for=(OSError,))
def ingest_book_task(self, book_id: int) -> int:
    """Ingest the book's pages, then fan out `processing.tasks.preprocess_page` into `after_preprocess`.

    Disk errors are retried (twice); any other failure puts the book in `error` with an actionable
    Arabic message and ends the run without raising. Whether the callback goes on to OCR is fixed
    here (`continue_ocr`), so a click on «بدء المعالجة» that lands before the callback runs cannot
    enqueue the pages twice.
    """
    book = Book.objects.get(pk=book_id)
    try:
        services.ingest_book(book)
    except services.IngestError as exc:  # actionable Arabic message (e.g. the skip values leave no pages)
        log.warning("ingest of book %s refused: %s", book_id, exc)
        services.set_book_error(book, str(exc))
        return book_id
    except OSError as exc:
        if self.request.retries >= self.max_retries:
            log.exception("ingest failed for book %s after retries", book_id)
            services.set_book_error(book, INGEST_ERROR, exc)
            return book_id
        raise self.retry(exc=exc, countdown=5) from exc
    except Exception as exc:  # noqa: BLE001 - reported on the book, never left as a raw traceback
        log.exception("ingest failed for book %s", book_id)
        services.set_book_error(book, INGEST_ERROR, exc)
        return book_id

    pages = list(book.pages.filter(is_excluded=False).order_by("number"))
    if not pages:
        services.set_book_error(book, NO_PAGES_ERROR)
        return book_id

    from processing.tasks import preprocess_page

    header = group(preprocess_page.s(page.pk) for page in pages)
    chord(header)(after_preprocess.s(book_id, continue_ocr=not book.awaits_ocr_start))
    return book_id


@shared_task(bind=True, max_retries=2, autoretry_for=(OSError,))
def after_preprocess(self, results, book_id: int, continue_ocr: bool | None = None) -> int:
    """Chord callback once every page is preprocessed.

    Proposes layout guides when the book has none (`processing.services.propose_guides`, for
    display only; automatic guides never apply to a page). Then:

    - `continue_ocr` true (today's path): moves the book to `ocr` and gives every preprocessed page
      the chain layout → ocr_fast → ocr_full (`_enqueue_layout`);
    - false (the book awaits «بدء المعالجة»): the book pauses at «تم التخطيط» (`refresh_status`),
      unless the owner already started meanwhile, in which case it is left alone;
    - None (a chord queued before this change): decided by the book's live flag.
    """
    try:
        book = Book.objects.get(pk=book_id)
    except ObjectDoesNotExist:
        log.warning("after_preprocess: book %s no longer exists", book_id)
        return book_id
    if continue_ocr is None:
        continue_ocr = not book.awaits_ocr_start
    if not book.pages.filter(is_excluded=False, status=Page.Status.PREPROCESSED).exists():
        if continue_ocr or book.awaits_ocr_start:
            book.refresh_status()  # every page failed preprocessing: nothing to lay out
        return book_id

    if not services.has_guides(book):
        from processing.services import propose_guides

        _guides, confidence = propose_guides(book)
        log.info("book %s: guides proposed with confidence %.2f (display only)", book_id, confidence)

    if continue_ocr:
        _enqueue_layout(book)
        return book_id
    book.refresh_from_db(fields=["awaits_ocr_start", "status", "error_message"])
    if not book.awaits_ocr_start:  # «بدء المعالجة» landed meanwhile: `start_ocr_task` owns the book
        return book_id
    book.refresh_status()
    log.info("book %s: prepared, waits for «بدء المعالجة» (%s)", book_id, book.status)
    return book_id


@shared_task(bind=True, max_retries=2, autoretry_for=(OSError,))
def start_ocr_task(self, book_id: int) -> int:
    """«بدء المعالجة» (D64): layout → ocr_fast → ocr_full for every prepared page (`_enqueue_layout`).

    A task because an 800-page book means 800 chains; `services.start_ocr` has already claimed the
    start (flag False, status `ocr`).
    """
    try:
        book = Book.objects.get(pk=book_id)
    except ObjectDoesNotExist:
        log.warning("start_ocr_task: book %s no longer exists", book_id)
        return book_id
    count = _enqueue_layout(book)
    log.info("book %s: «المعالجة» started for %s pages", book_id, count)
    return book_id


def _enqueue_layout(book: Book) -> int:
    """Move the book to `ocr` and enqueue layout → ocr_fast → ocr_full for each preprocessed page."""
    pages = list(book.pages.filter(is_excluded=False, status=Page.Status.PREPROCESSED).order_by("number"))
    if not pages:
        book.refresh_status()
        return 0
    book.status = Book.Status.OCR
    book.save(update_fields=["status", "updated_at"])
    for page in pages:
        services.run_stage(page, "layout", refresh_book=False)
    return len(pages)


@shared_task(bind=True, max_retries=2, autoretry_for=(OSError,))
def rerun_book_from(self, book_id: int, stage: str) -> int:
    """Re-run every non-excluded page of the book from `stage` (see `services.rerun_book`)."""
    try:
        book = Book.objects.get(pk=book_id)
    except ObjectDoesNotExist:
        log.warning("rerun_book_from: book %s no longer exists", book_id)
        return book_id
    try:
        count = services.rerun_book(book, stage)
    except ValueError as exc:  # the view validates first; a state change in between lands here
        log.warning("rerun_book_from: book %s not re-run from %s: %s", book_id, stage, exc)
        return book_id
    log.info("book %s: %s pages re-queued from %s", book_id, count, stage)
    return book_id
