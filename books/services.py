"""Books services: create a book from an upload, inspect and ingest its PDF, drive the pipeline.

Views, API functions and tasks stay thin and call into here. Page rendering follows the Phase 1
PoC (`playground/poc/extract_pages.py`, decisions D2 and D3): a PDF page that is a single
full-page scan is rendered at the *native* DPI of the embedded image so no pixels are invented and
the PDF rotation is honoured; any other page is rendered at `NASSAKH["RENDER_DPI"]`. Sheets that
hold two book pages are cut at the detected gutter into a right page (first, RTL) and a left page.

Images are 2-D `uint8` numpy arrays (grayscale). Tasks of the other apps are imported lazily
inside the functions that enqueue them.
"""

from __future__ import annotations

import logging
import math
import re
import shutil
import statistics
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from celery import chain
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.db.models import Count
from django.urls import reverse
from django.utils import timezone

import numpy as np
import pymupdf

from accounts.services import default_organization, organization_for
from books import runs
from books.models import ALL_PAGES_FAILED_MESSAGES, Book, Page
from core.arabic import arabic_ratio, normalize_ws, to_western_digits
from core.images import fit_width
from core.serializers import flag_items
from core.storage import book_source_path, save_array
from core.templatetags.nassakh import status_dot

if TYPE_CHECKING:
    from ocr.models import Line, OcrRun
    from processing.models import Region

    RunsByTarget = dict[tuple[int, int | None], OcrRun]  # latest fast run per (page_id, region_id)

log = logging.getLogger(__name__)

# Book fields a caller may set through `create_book` (everything else is derived).
BOOK_FIELDS: tuple[str, ...] = (
    "title",
    "author",
    "original_year",
    "notes",
    "skip_first",
    "skip_last",
    "pages_per_sheet",
    "split_ratio",
    "use_text_layer",
    "digit_style",
)

# Pipeline stages a page can be re-run from, in pipeline order, with their Arabic labels.
STAGES: tuple[str, ...] = ("preprocess", "layout", "ocr", "ocr_fast", "ocr_full")
STAGE_LABELS: dict[str, str] = {
    "preprocess": "تجهيز الصفحات",
    "layout": "تحديد المناطق",
    "ocr": "التعرّف على النص (سريع ثم كامل)",
    "ocr_fast": "التعرّف السريع (Tesseract)",
    "ocr_full": "التعرّف الكامل (Qari)",
}

# Book statuses during which the dashboard keeps polling.
ACTIVE_BOOK_STATUSES: frozenset[str] = frozenset({Book.Status.PROCESSING, Book.Status.OCR})
# Page statuses that still have pipeline work ahead of them.
ACTIVE_PAGE_STATUSES: frozenset[str] = frozenset(
    {Page.Status.UPLOADED, Page.Status.PREPROCESSED, Page.Status.LAYOUT_DONE}
)
# Approved pages are frozen: no stage re-runs on them until they are reopened in the review screen
# (a new layout or OCR pass would renumber, misplace or drop the reviewed lines).
APPROVED_PAGE_STATUSES: frozenset[str] = frozenset({Page.Status.REVIEWED, Page.Status.ASSEMBLED})
APPROVED_PAGE_ERROR = "الصفحة معتمدة؛ أعد فتحها من شاشة المراجعة قبل إعادة تشغيلها."
ALL_APPROVED_ERROR = "كل صفحات الكتاب معتمدة؛ أعد فتح الصفحات التي تريد إعادة تشغيلها من شاشة المراجعة أولًا."

# «التخطيط» and «المعالجة» (D64, D65): the messages of the split.
LAYOUT_STAGES: tuple[str, ...] = ("preprocess",)  # the only re-run while a book awaits «بدء المعالجة»
NOT_STARTED_ERROR = "لم تبدأ المعالجة بعد؛ اضغط «بدء المعالجة» أولًا."
LAYOUT_DONE_ERROR = "اكتمل التخطيط؛ اضغط «بدء المعالجة»، أو اختر «إعادة التخطيط» من القائمة «⋯»."
BROKER_ERROR = "تعذّر إرسال العمل إلى العامل الخلفي. تأكّد من تشغيل Redis والعامل ثم أعد المحاولة."
ALREADY_STARTED_ERROR = "بدأت المعالجة بالفعل."
STILL_PREPARING_ERROR = "لم يكتمل التخطيط بعد؛ انتظر حتى تُجهَّز كل الصفحات."
NOTHING_READY_ERROR = "لا صفحات جاهزة للمعالجة؛ أعد صفحةً إلى الكتاب أو اختر «إعادة التخطيط»."

# The book re-runs the «⋯» menu offers (D101), one per view and named by it: «إعادة التخطيط» in
# «التخطيط» (the pages prepared and laid out again, then read again once «المعالجة» has started) and
# «إعادة المعالجة» outside it (Tesseract, both models, finalisation and the numbers pass, as one chain
# over the kept layout). The other stages stay for the retry of a failed page, the views, the tasks and
# the shell.
RELAYOUT_STAGE = "preprocess"
REPROCESS_STAGE = "ocr"
RERUN_ACTION_LABELS: dict[str, str] = {RELAYOUT_STAGE: "إعادة التخطيط", REPROCESS_STAGE: "إعادة المعالجة"}

# The model time a book re-run costs (§3.6): stages whose chain ends with the models, and the
# per-page Qari time used when the book has no Qari run yet.
MODEL_STAGES: frozenset[str] = frozenset({"preprocess", "layout", "ocr", "ocr_full"})
QARI_FALLBACK_SECONDS = 20.0

# Arabic count forms (`assembly.render.ar_count`): «7 صفحات» and «من 555 صفحة».
PAGE_FORMS: tuple[str, str, str, str] = ("صفحة واحدة", "صفحتان", "صفحات", "صفحة")
PAGE_FORMS_OF: tuple[str, str, str, str] = ("صفحة واحدة", "صفحتين", "صفحات", "صفحة")

# Text-layer detection (spec §6): average characters per sampled page and Arabic share of letters.
TEXT_LAYER_MIN_CHARS = 200
TEXT_LAYER_MIN_ARABIC = 0.5
TEXT_LAYER_SAMPLE_PAGES = 40

# Rendering.
MIN_NATIVE_DPI = 72.0
MAX_NATIVE_DPI = 600.0  # bounds memory for absurdly large embedded images
SCAN_THUMB_MAX_WIDTH = 240  # dashboard tile preview written at ingest (matches the cleaned thumbnail)
MIN_IMAGE_COVERAGE = (
    0.5  # the largest embedded image must cover this share of the page to count as "the scan"
)
GUTTER_SEARCH_SPAN = 0.15  # search ± this share of the width around the book's split ratio
GUTTER_MIN_CONFIDENCE = 0.3  # below this the split ratio is used as is
SPLIT_MIN_RATIO, SPLIT_MAX_RATIO = 0.2, 0.8

# The statuses of a page whose text the models have read: «المعالجة»'s percentage counts these (owner 13).
_READ_STATUSES: frozenset[str] = frozenset({Page.Status.OCR_DONE, Page.Status.REVIEWED, Page.Status.ASSEMBLED})


class IngestError(ValueError):
    """Raised by `ingest_book` with an actionable Arabic message (shown as the book's headline)."""


# ====================================================================== book creation


def create_book(data: dict, pdf: UploadedFile, user, organization=None) -> Book:
    """Create a book from validated form data and its PDF, store the file and inspect it.

    Only keys in `BOOK_FIELDS` are taken from `data`. The PDF lands at `books/{id}/source.pdf`
    (`core.storage.book_source_path`). When the PDF cannot be read the book is kept with status
    `error` and an actionable Arabic message instead of raising. The book awaits «بدء المعالجة»
    (D64): its pages are extracted and prepared, then nothing is read before the owner starts.
    The book belongs to `organization` (the view passes the one the user works in), else to the user's,
    else (a command, no user) to the first organisation: who may see it follows (D98, D102).
    """
    fields = {key: value for key, value in data.items() if key in BOOK_FIELDS and value is not None}
    book = Book(**fields, awaits_ocr_start=True)
    if getattr(user, "is_authenticated", False):
        book.created_by = user
    book.organization = organization or organization_for(user) or default_organization()
    book.save()

    target = book_source_path(book, pdf.name)
    if default_storage.exists(target):  # keep the path stable even if a stale file is lying around
        default_storage.delete(target)
    book.source_pdf.save(pdf.name, pdf, save=True)

    try:
        inspect_pdf(book)
    except Exception as exc:  # noqa: BLE001 - any PyMuPDF failure means an unusable file
        log.warning("inspect_pdf failed for book %s: %s", book.pk, exc)
        set_book_error(book, "تعذّر قراءة ملف PDF. تأكد أن الملف سليم وغير محمي بكلمة مرور ثم أعد رفعه.", exc)
    return book


def set_book_error(book: Book, message: str, exc: BaseException | None = None) -> None:
    """Put the book in `error` with an Arabic headline; technical detail goes on a second line."""
    detail = f"\n{type(exc).__name__}: {exc}" if exc is not None else ""
    book.status = Book.Status.ERROR
    book.error_message = f"{message}{detail}"
    book.save(update_fields=["status", "error_message", "updated_at"])


# ====================================================================== PDF inspection


def _open_pdf(book: Book) -> pymupdf.Document:
    """Open the book's PDF with PyMuPDF from the storage (path when available, else a byte stream)."""
    if not book.source_pdf:
        raise ValueError("لا يوجد ملف PDF مرفق بهذا الكتاب.")
    try:
        path = book.source_pdf.path
    except NotImplementedError:  # storage without local paths
        with book.source_pdf.open("rb") as handle:
            return pymupdf.open(stream=handle.read(), filetype="pdf")
    return pymupdf.open(path)


def selected_indices(book: Book, page_count: int) -> list[int]:
    """0-based PDF page indices that belong to the book: `[skip_first, page_count - skip_last)`."""
    start, stop = int(book.skip_first), page_count - int(book.skip_last)
    if page_count <= 0 or start >= stop:
        return []
    return list(range(max(0, start), stop))


def _spread(items: Sequence[int], limit: int) -> list[int]:
    """At most `limit` items spread evenly over the sequence (all of them when it is short)."""
    if len(items) <= limit:
        return list(items)
    step = len(items) / limit
    return [items[int(i * step)] for i in range(limit)]


def text_layer_enabled() -> bool:
    """Whether a PDF's text layer may stand for OCR at all (`NASSAKH["TEXT_LAYER"]`, off: item 20)."""
    return bool(settings.NASSAKH.get("TEXT_LAYER", False))


def book_uses_text_layer(book: Book) -> bool:
    """The book's text comes from its PDF's text layer (= `ocr.services.uses_text_layer` for its pages)."""
    return text_layer_enabled() and bool(book.has_text_layer) and bool(book.use_text_layer)


def inspect_pdf(book: Book) -> dict:
    """Count the PDF pages and decide whether the file carries a usable text layer; store both.

    A text layer counts when the sampled pages (up to `TEXT_LAYER_SAMPLE_PAGES`, spread over the
    selected range) average at least `TEXT_LAYER_MIN_CHARS` characters and the Arabic share of their
    letters is at least `TEXT_LAYER_MIN_ARABIC`. Returns the numbers behind the decision.
    """
    doc = _open_pdf(book)
    try:
        page_count = doc.page_count
        indices = selected_indices(book, page_count) or list(range(page_count))
        sample = _spread(indices, TEXT_LAYER_SAMPLE_PAGES)
        texts = [doc[index].get_text("text") for index in sample]
    finally:
        doc.close()

    avg_chars = sum(len(text.strip()) for text in texts) / len(texts) if texts else 0.0
    ratio = arabic_ratio("".join(texts))
    has_text_layer = avg_chars >= TEXT_LAYER_MIN_CHARS and ratio >= TEXT_LAYER_MIN_ARABIC

    book.source_page_count = page_count
    book.has_text_layer = has_text_layer
    book.save(update_fields=["source_page_count", "has_text_layer", "updated_at"])
    return {
        "page_count": page_count,
        "has_text_layer": has_text_layer,
        "avg_chars": round(avg_chars, 1),
        "arabic_ratio": round(ratio, 3),
        "sampled_pages": len(sample),
    }


# ====================================================================== rendering (PoC port)


def native_dpi(page: pymupdf.Page, img_w: int, img_h: int) -> float:
    """DPI at which rendering reproduces an embedded `img_w`×`img_h` scan pixel for pixel."""
    rect = page.rect  # already rotated as displayed
    long_in, short_in = sorted([rect.width, rect.height], reverse=True)
    long_px, short_px = sorted([img_w, img_h], reverse=True)
    return max(long_px / (long_in / 72.0), short_px / (short_in / 72.0))


def largest_embedded_image(page: pymupdf.Page) -> tuple[int, int, int] | None:
    """`(xref, width, height)` of the largest image the page references, or None."""
    best: tuple[int, int, int] | None = None
    for info in page.get_images(full=True):
        xref, width, height = info[0], info[2], info[3]
        if best is None or width * height > best[1] * best[2]:
            best = (xref, width, height)
    return best


def _covers_page(page: pymupdf.Page, xref: int) -> bool:
    """True when the image is drawn over at least `MIN_IMAGE_COVERAGE` of the page area."""
    try:
        rects = page.get_image_rects(xref)
    except Exception:  # noqa: BLE001 - malformed resources: trust the largest image
        return True
    if not rects:  # placed through a nested form XObject; trust it
        return True
    area = sum(rect.get_area() for rect in rects)
    return area >= MIN_IMAGE_COVERAGE * page.rect.get_area()


def page_render_dpi(page: pymupdf.Page) -> tuple[float, str]:
    """`(dpi, source)` for a PDF page: native DPI of a full-page scan, else RENDER_DPI ("render")."""
    embedded = largest_embedded_image(page)
    if embedded is not None and _covers_page(page, embedded[0]):
        dpi = native_dpi(page, embedded[1], embedded[2])
        return min(max(dpi, MIN_NATIVE_DPI), MAX_NATIVE_DPI), "native"
    return float(settings.NASSAKH["RENDER_DPI"]), "render"


def render_gray(page: pymupdf.Page, dpi: float) -> np.ndarray:
    """Render a PDF page (rotation applied) as a 2-D uint8 grayscale array at `dpi`."""
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()


def find_gutter(gray: np.ndarray, center: float = 0.5, span: float = GUTTER_SEARCH_SPAN) -> tuple[int, float]:
    """`(x, confidence)` of the gutter between two book pages on a sheet.

    Looks for the widest low-ink vertical band within ±`span` of `center` (ratios of the width),
    bridging narrow dark spikes (fold shadow, staples). Confidence 0 means nothing convincing was
    found and `x` is the centre of the search window.
    """
    h, w = gray.shape
    x0 = max(0, int((center - span) * w))
    x1 = min(w, int((center + span) * w))
    fallback = int(round(center * w))
    if x1 - x0 < 3 or h < 3:
        return fallback, 0.0
    band = gray[int(0.08 * h) : int(0.92 * h), x0:x1]
    ink = (band < 128).mean(axis=0)
    k = max(5, w // 200)
    ink_s = np.convolve(ink, np.ones(k) / k, mode="same")
    low = ink_s < 0.02
    # bridge gaps shorter than 1.5% of the width (staples / fold line)
    gap_max = max(3, int(0.015 * w))
    idx = np.where(~low)[0]
    if idx.size:
        for run in np.split(idx, np.where(np.diff(idx) > 1)[0] + 1):
            if run.size <= gap_max and run[0] > 0 and run[-1] < low.size - 1:
                low[run[0] : run[-1] + 1] = True
    best_len, best_center = 0, None
    lows = np.where(low)[0]
    if lows.size:
        for run in np.split(lows, np.where(np.diff(lows) > 1)[0] + 1):
            if run.size > best_len:
                best_len, best_center = run.size, int((run[0] + run[-1]) / 2)
    if best_center is None or best_len < 0.02 * w:
        return fallback, 0.0
    return x0 + best_center, min(1.0, best_len / (0.15 * w))


def split_position(gray: np.ndarray, ratio: float) -> tuple[int, float]:
    """Column at which a two-page sheet is cut: the detected gutter near `ratio`, else `ratio` itself."""
    ratio = min(max(float(ratio), SPLIT_MIN_RATIO), SPLIT_MAX_RATIO)
    w = gray.shape[1]
    x, confidence = find_gutter(gray, center=ratio)
    if confidence < GUTTER_MIN_CONFIDENCE:
        x, confidence = int(round(ratio * w)), 0.0
    x = min(max(x, int(SPLIT_MIN_RATIO * w)), int(SPLIT_MAX_RATIO * w))
    return x, confidence


def _half_image(gray: np.ndarray, half: str, split_x: int | None) -> np.ndarray:
    """The right or left part of a sheet cut at `split_x`, or the whole sheet for `full`."""
    if half == Page.SourceHalf.RIGHT:
        return gray[:, split_x:]
    if half == Page.SourceHalf.LEFT:
        return gray[:, :split_x]
    return gray


def _half_text(page: pymupdf.Page, half: str, split_ratio: float | None) -> str:
    """Text layer of a page or of one half of a sheet (clip given in the displayed coordinate space)."""
    if half == Page.SourceHalf.FULL or split_ratio is None:
        return page.get_text("text")
    rect = page.rect
    cut = rect.x0 + split_ratio * rect.width
    if half == Page.SourceHalf.RIGHT:
        clip = pymupdf.Rect(cut, rect.y0, rect.x1, rect.y1)
    else:
        clip = pymupdf.Rect(rect.x0, rect.y0, cut, rect.y1)
    # get_text works in the unrotated page space while the pixmap follows the rotation.
    clip = (clip * page.derotation_matrix).normalize()
    return page.get_text("text", clip=clip)


# ====================================================================== ingest


def ingest_book(book: Book) -> list[Page]:
    """Turn the selected PDF pages into `Page` rows with an `original_image`; idempotent.

    Pages that already exist (by book order number) are kept untouched, so a crashed or repeated
    ingest resumes where it stopped and originals are never overwritten. Sheets with two book
    pages are split at the detected gutter (`split_position`), right page first. When the book has
    a text layer, `text_layer_text` is filled per page. New pages start in status `uploaded`.
    Returns every page of the book in order. Raises IngestError (Arabic message) when the skip
    values leave no pages.
    """
    doc = _open_pdf(book)
    try:
        page_count = doc.page_count
        if book.source_page_count != page_count:
            book.source_page_count = page_count
            book.save(update_fields=["source_page_count", "updated_at"])

        indices = selected_indices(book, page_count)
        if not indices:
            raise IngestError(
                "لا تبقى صفحات بعد تجاوز الصفحات الأولى والأخيرة. قلّل قيم التجاوز ثم أعد المحاولة."
            )

        two_up = int(book.pages_per_sheet) == 2
        halves = (Page.SourceHalf.RIGHT, Page.SourceHalf.LEFT) if two_up else (Page.SourceHalf.FULL,)
        existing = {page.number: page for page in book.pages.all()}
        pages: list[Page] = []
        number = 0

        for index in indices:
            targets = []
            for half in halves:
                number += 1
                targets.append((number, half))
            if all(num in existing for num, _ in targets):
                pages.extend(existing[num] for num, _ in targets)
                continue

            pdf_page = doc[index]
            dpi, source = page_render_dpi(pdf_page)
            gray = render_gray(pdf_page, dpi)
            split_x: int | None = None
            split_ratio: float | None = None
            if two_up:
                ratio = book.split_ratio
                for num, _ in targets:  # a manual override on an already ingested half wins
                    override = getattr(existing.get(num), "split_ratio_override", None)
                    if override is not None:
                        ratio = override
                        break
                split_x, confidence = split_position(gray, ratio)
                split_ratio = split_x / gray.shape[1]
                log.info(
                    "book %s pdf page %s: gutter at x=%s (confidence %.2f)",
                    book.pk,
                    index,
                    split_x,
                    confidence,
                )

            for num, half in targets:
                if num in existing:
                    pages.append(existing[num])
                    continue
                image = _half_image(gray, half, split_x)
                text = _half_text(pdf_page, half, split_ratio) if book.has_text_layer else ""
                pages.append(_create_page(book, num, index, half, image, dpi, text))
                log.info(
                    "book %s: page %s from pdf page %s (%s, %sx%s px, %.0f dpi, %s)",
                    book.pk,
                    num,
                    index,
                    half,
                    image.shape[1],
                    image.shape[0],
                    dpi,
                    source,
                )
    finally:
        doc.close()
    return pages


def _create_page(
    book: Book, number: int, index: int, half: str, image: np.ndarray, dpi: float, text: str
) -> Page:
    """Write the original PNG first, then the row, so a crash never leaves a page without its image."""
    height, width = image.shape[:2]
    page = Page(
        book=book,
        number=number,
        source_index=index,
        source_half=half,
        width=int(width),
        height=int(height),
        dpi=round(float(dpi), 2),
        text_layer_text=text or "",
        status=Page.Status.UPLOADED,
    )
    save_array(page.original_image, image, "original.png")
    # A small scan preview so the dashboard tile shows the page before preprocessing finishes.
    save_array(page.scan_thumbnail, fit_width(image, SCAN_THUMB_MAX_WIDTH), "scan_thumb.webp")
    page.save()
    return page


# ====================================================================== pipeline control


def start_processing(book: Book) -> None:
    """«استخراج الصفحات»: mark the book `processing` and enqueue `ingest_book_task` (ingest → preprocess).

    Allowed from `uploaded` and `error` (a restart resumes the idempotent ingest; a book whose
    «المعالجة» had started goes on through OCR, as its flag is False). Raises ValueError with an
    Arabic message otherwise. When the task cannot be queued the status and the error message go
    back and ValueError says so (`BROKER_ERROR`), so the book never waits for work nobody runs.
    """
    if book.status in ACTIVE_BOOK_STATUSES:
        raise ValueError("المعالجة جارية بالفعل.")
    if book.status == Book.Status.NEEDS_GUIDES:
        raise ValueError(LAYOUT_DONE_ERROR)
    if book.status not in (Book.Status.UPLOADED, Book.Status.ERROR):
        raise ValueError("انتهت معالجة هذا الكتاب. استخدم «إعادة المعالجة» من القائمة «⋯».")
    if not book.source_pdf:
        raise ValueError("لا يوجد ملف PDF مرفق بهذا الكتاب.")

    previous = (book.status, book.error_message)
    book.status = Book.Status.PROCESSING
    book.error_message = ""
    book.save(update_fields=["status", "error_message", "updated_at"])

    from books.tasks import ingest_book_task

    try:
        ingest_book_task.delay(book.pk)
    except Exception as exc:  # noqa: BLE001 - the broker refused (Redis down): nothing will run
        log.warning("start_processing: book %s not queued: %s", book.pk, exc)
        book.status, book.error_message = previous
        book.save(update_fields=["status", "error_message", "updated_at"])
        raise ValueError(BROKER_ERROR) from exc


def start_ocr(book: Book) -> None:
    """«بدء المعالجة» (D64): send every prepared page into «المعالجة».

    Only from «تم التخطيط» (`needs_guides` with `awaits_ocr_start`). Refused with an Arabic ValueError
    when no page is ready. One conditional UPDATE claims the start (flag → False, status → `ocr`), so
    a double submit or a second tab starts nothing twice; `start_ocr_task` then runs `_enqueue_layout`.
    When the task cannot be queued the claim is undone.
    """
    ready = book.pages.filter(is_excluded=False, status=Page.Status.PREPROCESSED).exists()
    if not ready:
        current = Book.objects.filter(pk=book.pk).values("awaits_ocr_start", "status").first() or {}
        if current and not current["awaits_ocr_start"]:
            raise ValueError(ALREADY_STARTED_ERROR)
        if current.get("status") == Book.Status.PROCESSING:
            raise ValueError(STILL_PREPARING_ERROR)
        raise ValueError(NOTHING_READY_ERROR)
    claimed = Book.objects.filter(pk=book.pk, awaits_ocr_start=True, status=Book.Status.NEEDS_GUIDES).update(
        awaits_ocr_start=False, status=Book.Status.OCR, error_message="", updated_at=timezone.now()
    )
    if not claimed:
        current = Book.objects.filter(pk=book.pk).values("awaits_ocr_start", "status").first() or {}
        if current and not current["awaits_ocr_start"]:
            raise ValueError(ALREADY_STARTED_ERROR)
        if current.get("status") == Book.Status.PROCESSING:
            raise ValueError(STILL_PREPARING_ERROR)
        raise ValueError(NOTHING_READY_ERROR)
    book.refresh_from_db(fields=["awaits_ocr_start", "status", "error_message", "updated_at"])

    from books.tasks import start_ocr_task

    try:
        start_ocr_task.delay(book.pk)
    except Exception as exc:  # noqa: BLE001 - the broker refused: give the start back
        log.warning("start_ocr: book %s not queued: %s", book.pk, exc)
        Book.objects.filter(pk=book.pk).update(
            awaits_ocr_start=True, status=Book.Status.NEEDS_GUIDES, updated_at=timezone.now()
        )
        book.refresh_from_db(fields=["awaits_ocr_start", "status", "updated_at"])
        raise ValueError(BROKER_ERROR) from exc


def _stage_signatures(page_id: int, stage: str, layout_stage: bool = False, run: str = "") -> list:
    """Celery signatures for the pipeline from `stage` onwards; each task returns the page id.

    With `layout_stage` (the book awaits «بدء المعالجة») the only chain is `preprocess_page`. Every task
    carries the run's token (`run=`, `books.runs`) and the last one `last=True`: it gives the claim back.
    """
    from ocr.tasks import ocr_page_fast, ocr_page_full
    from processing.tasks import layout_page, preprocess_page  # other apps: lazy imports

    if layout_stage:
        steps = [preprocess_page]
    else:
        steps = {
            "preprocess": [preprocess_page, layout_page, ocr_page_fast, ocr_page_full],
            "layout": [layout_page, ocr_page_fast, ocr_page_full],
            "ocr": [ocr_page_fast, ocr_page_full],
            "ocr_fast": [ocr_page_fast],
            "ocr_full": [ocr_page_full],
        }[stage]
    signatures = []
    for i, task in enumerate(steps):
        options = {"run": run, "last": True} if run and i == len(steps) - 1 else {"run": run} if run else {}
        signatures.append(task.s(page_id, **options) if i == 0 else task.s(**options))
    return signatures


# Status a page is put back to before a stage re-runs (the stage's input) and its text state.
_STAGE_INPUT_STATUS: dict[str, str] = {
    "preprocess": Page.Status.UPLOADED,
    "layout": Page.Status.PREPROCESSED,
    "ocr": Page.Status.LAYOUT_DONE,
    "ocr_full": Page.Status.LAYOUT_DONE,
}
_STATUS_RANK: dict[str, int] = {
    Page.Status.UPLOADED: 0,
    Page.Status.PREPROCESSED: 1,
    Page.Status.LAYOUT_DONE: 2,
    Page.Status.OCR_DONE: 3,
    Page.Status.REVIEWED: 4,
    Page.Status.ASSEMBLED: 5,
}
# Attention flags written by the OCR stage (recomputed by `ocr.services.finalize_page`).
_OCR_FLAGS: frozenset[str] = frozenset({"ocr_fallback", "alignment_poor", "lines_merged", "no_readable_text"})
# Pipeline stage that continues a re-included page from its completed status.
_NEXT_STAGE: dict[str, str] = {
    Page.Status.UPLOADED: "preprocess",
    Page.Status.PREPROCESSED: "layout",
    Page.Status.LAYOUT_DONE: "ocr",
}


def _reset_to_stage_input(page: Page, stage: str) -> None:
    """Move a page back to the input state of `stage` so the book and the panels see work pending.

    Only moves backwards (a page that never reached the stage's input keeps its status, and the
    stage then reports the missing input). The text state drops to `none`, or to `provisional` for
    `ocr_full`, which keeps the Tesseract text; the OCR attention flags are dropped because the
    re-run recomputes them. `ocr_fast` alone does not reset: it only refreshes the provisional
    text and the Tesseract geometry of a page whose final text stays valid. An approved page is
    never moved back (`run_stage` refuses it, `rerun_book` skips it).
    """
    target = _STAGE_INPUT_STATUS.get(stage)
    if page.status in APPROVED_PAGE_STATUSES:
        return
    if target is None or _STATUS_RANK.get(page.status, -1) <= _STATUS_RANK[target]:
        return
    page.status = target
    if stage == "ocr_full" and page.provisional_text:
        page.text_state = Page.TextState.PROVISIONAL
    else:
        page.text_state = Page.TextState.NONE
    page.attention_flags = [f for f in (page.attention_flags or []) if f not in _OCR_FLAGS]
    page.save(update_fields=["status", "text_state", "attention_flags"])


def run_stage(page: Page, stage: str, refresh_book: bool = True, run: str = ""):
    """Enqueue the pipeline for one page from `stage` (preprocess | layout | ocr | ocr_fast | ocr_full).

    One run per page at a time (`books.runs`, item 29): the page is claimed first, and while another run
    holds it nothing is queued (`runs.PAGE_RUN_ACTIVE_ERROR`); `run` is the token of a claim the caller
    already holds (a book re-run claims all its pages at once). A page in `error` is cleared first (it
    falls back to its last completed status), then put back to the stage's input state
    (`_reset_to_stage_input`) and the book status is re-derived so the dashboard polls until the page is
    through. Excluded pages are refused, and so are approved pages (`APPROVED_PAGE_ERROR`: reopen them
    first). While the book awaits «بدء المعالجة» only `preprocess` runs, and alone (`NOT_STARTED_ERROR`
    for the other stages; the flag is read fresh). A refusal or a failure to queue gives the claim back.
    Stores the chain's task id on the page and returns the AsyncResult.
    """
    from books import tasks  # the errback task (lazy: books.tasks imports this module)

    if stage not in STAGES:
        raise ValueError(f"مرحلة غير معروفة: {stage}")
    layout_stage = awaits_start(page.book_id)
    if layout_stage and stage not in LAYOUT_STAGES:
        raise ValueError(NOT_STARTED_ERROR)
    if page.is_excluded:
        raise ValueError("الصفحة مستثناة؛ أعد ضمّها إلى الكتاب أولًا.")
    token = run or runs.claim_page(page.pk)
    if not token:
        raise ValueError(runs.PAGE_RUN_ACTIVE_ERROR)
    try:
        if page.status == Page.Status.ERROR:
            page.clear_error()
        if page.status in APPROVED_PAGE_STATUSES:
            raise ValueError(APPROVED_PAGE_ERROR)
        _reset_to_stage_input(page, stage)
        if refresh_book:
            _refresh_started_book(page.book)
        signatures = _stage_signatures(page.pk, stage, layout_stage=layout_stage, run=token)
        errback = tasks.page_run_failed.s(page_id=page.pk, run=token)  # a task that died: release, mark
        result = chain(*signatures).apply_async(link_error=errback)
    except BaseException:
        runs.release(page.pk, token)
        raise
    page.task_id = str(getattr(result, "id", "") or "")[:64]
    page.save(update_fields=["task_id"])
    return result


def awaits_start(book_id: int) -> bool:
    """The book's `awaits_ocr_start`, read fresh from the database (D64)."""
    return bool(Book.objects.filter(pk=book_id).values_list("awaits_ocr_start", flat=True).first())


def _refresh_started_book(book: Book) -> None:
    """Re-derive the status of a book whose processing has started (not `uploaded` or ingest `error`)."""
    book.refresh_from_db(fields=["status", "error_message"])
    if book.status != Book.Status.UPLOADED:
        book.refresh_status()


def validate_rerun(book: Book, stage: str, run: str = "") -> None:
    """Raise ValueError (Arabic) when the whole book cannot be re-run from `stage` (`run`: the token of the
    re-run's own claims, which do not count as another run).

    Every stage can be re-run, including on a book that an earlier version parked in
    `needs_guides` (regions are now derived per page, so nothing waits for the guides). While the
    book awaits «بدء المعالجة» only `preprocess` can («إعادة التخطيط»). Refused when every
    non-excluded page is approved (`rerun_book` would have nothing to run), while the pages of a book in
    «التخطيط» are still being extracted and prepared (`STILL_PREPARING_ERROR`), and while a run holds any
    page of the book (`runs.book_run_active_message`: one run per page at a time, item 29).
    """
    if stage not in STAGES:
        raise ValueError(f"مرحلة غير معروفة: {stage}")
    current = Book.objects.filter(pk=book.pk).values("awaits_ocr_start", "status").first() or {}
    if stage not in LAYOUT_STAGES and current.get("awaits_ocr_start"):
        raise ValueError(NOT_STARTED_ERROR)
    if current.get("awaits_ocr_start") and current.get("status") == Book.Status.PROCESSING:
        raise ValueError(STILL_PREPARING_ERROR)  # the ingest chord is still preparing the pages
    pages = book.pages.filter(is_excluded=False)
    if pages.exists() and not pages.exclude(status__in=APPROVED_PAGE_STATUSES).exists():
        raise ValueError(ALL_APPROVED_ERROR)
    busy = runs.active_count(book.pages.exclude(run_token=run) if run else book.pages.all())
    if busy:
        raise ValueError(runs.book_run_active_message(busy))


def approved_page_count(book: Book) -> int:
    """Non-excluded approved pages of the book: a book re-run leaves them as they are."""
    return book.pages.filter(is_excluded=False, status__in=APPROVED_PAGE_STATUSES).count()


def _claim_book_rerun(book: Book, stage: str) -> tuple[str, int]:
    """Validate a book re-run and claim every page it runs with one token (`books.runs`), atomically.

    The book row is locked first, so two submits of the same re-run are serialised: the second one finds
    the pages claimed and is refused (`validate_rerun`). Returns `(token, pages claimed)`.
    """
    with transaction.atomic():
        Book.objects.select_for_update().filter(pk=book.pk).first()
        validate_rerun(book, stage)
        pages = book.pages.filter(is_excluded=False).exclude(status__in=APPROVED_PAGE_STATUSES)
        return runs.claim_pages(pages)


def queue_book_rerun(book: Book, stage: str) -> int:
    """The «⋯» menu's book re-run: validate it, claim its pages and queue `rerun_book_from`.

    The claim is taken here, in the request, so a second click (or tab) is refused at once with a clear
    message instead of queueing a second run of every page (item 29). Raises ValueError (Arabic): see
    `validate_rerun`, and `BROKER_ERROR` when the task cannot be queued (the claims are given back).
    Returns the number of pages claimed.
    """
    from books.tasks import rerun_book_from

    token, count = _claim_book_rerun(book, stage)
    try:
        rerun_book_from.delay(book.pk, stage, run=token)
    except Exception as exc:  # noqa: BLE001 - the broker refused (Redis down): nothing will run
        log.warning("queue_book_rerun: book %s not queued: %s", book.pk, exc)
        runs.release_book(book.pk, token)
        raise ValueError(BROKER_ERROR) from exc
    return count


def rerun_book(book: Book, stage: str, run: str | None = None) -> int:
    """Re-run every non-excluded, unapproved page of the book from `stage`; returns how many were enqueued.

    `run` is the token of the claims `queue_book_rerun` took; without it (a shell, a script) the pages are
    claimed here. Only the pages that run holds are re-run. Approved pages are skipped (frozen until
    reopened, see `APPROVED_PAGE_STATUSES`). Every other page is first put back to the stage's input
    state, then the chains are enqueued and the book status is re-derived (`processing` or `ocr` while
    work is pending), so the book cannot settle before its last page is through. Raises ValueError (see
    `validate_rerun`); the claims are then given back.
    """
    if run is None:
        run, _count = _claim_book_rerun(book, stage)
    else:
        try:
            validate_rerun(book, stage, run=run)
        except ValueError:
            runs.release_book(book.pk, run)
            raise
    pages = list(book.pages.filter(run_token=run).order_by("number"))
    for page in pages:
        if page.status == Page.Status.ERROR:
            page.clear_error()  # an approved page that failed a stale task falls back to `reviewed`
    for page in pages:
        if page.status in APPROVED_PAGE_STATUSES or page.is_excluded:
            runs.release(page.pk, run)
    pages = [page for page in pages if page.status not in APPROVED_PAGE_STATUSES and not page.is_excluded]
    if not pages:
        return 0
    book.status = Book.Status.PROCESSING if stage == "preprocess" else Book.Status.OCR
    book.error_message = ""
    book.save(update_fields=["status", "error_message", "updated_at"])
    for page in pages:
        _reset_to_stage_input(page, stage)
    queued = 0
    for i, page in enumerate(pages):
        try:
            run_stage(page, stage, refresh_book=False, run=run)
            queued += 1
        except ValueError as exc:  # changed meanwhile (approved, excluded): its claim was given back
            log.info("rerun_book: book %s page %s not re-run: %s", book.pk, page.number, exc)
        except BaseException:  # nothing more can be queued: the pages not reached give their claims back
            for rest in pages[i + 1 :]:
                runs.release(rest.pk, run)
            raise
    book.refresh_status()
    return queued


def toggle_exclude(page: Page) -> Page:
    """Flip `is_excluded`; an excluded page shows `excluded`, a re-included one its completed stage.

    A page re-included into a book whose processing has started continues from its completed
    stage (preprocess, layout or OCR is enqueued), so the book never waits for work nobody runs.
    In a book that awaits «بدء المعالجة» (D64) a prepared page simply joins the waiting pages and an
    unprepared one is only prepared; nothing is queued for OCR. A book in `error` because every page
    failed (`ALL_PAGES_FAILED_MESSAGES`) is re-derived like any other (the «تراجع» of an exclusion
    brings back its one good page); an ingest error stays.
    """
    page.is_excluded = not page.is_excluded
    if page.is_excluded:
        page.status = Page.Status.EXCLUDED
        page.error_from = ""
        page.error_message = ""
    else:
        page.status = page._completed_status()
    page.save(update_fields=["is_excluded", "status", "error_from", "error_message"])
    book = page.book
    # A book that has not started yet stays `uploaded`, an ingest error stays; otherwise the derived status
    # may change (the all-failed error is `refresh_status`'s own).
    if book.status == Book.Status.UPLOADED or (
        book.status == Book.Status.ERROR and book.error_message not in ALL_PAGES_FAILED_MESSAGES
    ):
        return page
    next_stage = None if page.is_excluded else _NEXT_STAGE.get(page.status)
    if next_stage is not None and book.awaits_ocr_start and next_stage != "preprocess":
        next_stage = None  # a prepared page joins the waiting pages; an unprepared one is only prepared
    if next_stage is not None:
        try:
            run_stage(page, next_stage)  # in «التخطيط» a preprocess-only run (`run_stage`'s layout stage)
            return page
        except ValueError as exc:  # its earlier run still holds it (excluded and brought back meanwhile)
            log.info("toggle_exclude: page %s not queued: %s", page.pk, exc)
    book.refresh_status()
    return page


def qari_seconds(book: Book) -> float:
    """Typical model time of one page of the book, in seconds (one query over `OcrRun`).

    A page's Qari time is the duration of the newest primary (Qari v0.3) run plus the newest
    secondary (Qari v0.2) run of each of its regions (page-level runs count as one target); the
    result is the median over the book's pages that have one, else `QARI_FALLBACK_SECONDS`.
    """
    from ocr.models import OcrRun  # other app: lazy imports
    from ocr.services import engine_names

    primary, secondary, _fast = engine_names()
    rows = (
        OcrRun.objects.filter(page__book=book, engine_name__in=(primary, secondary), status=OcrRun.Status.OK)
        .order_by("-created_at", "-id")
        .values_list("page_id", "region_id", "engine_name", "duration_ms")
    )
    newest: dict[tuple, int] = {}
    for page_id, region_id, engine, duration in rows:
        newest.setdefault((page_id, region_id, engine), int(duration or 0))
    per_page: dict[int, int] = {}
    for (page_id, _region, _engine), duration in newest.items():
        per_page[page_id] = per_page.get(page_id, 0) + duration
    times = [ms / 1000 for ms in per_page.values() if ms > 0]
    return float(statistics.median(times)) if times else QARI_FALLBACK_SECONDS


def _rerun_counts(book: Book) -> tuple[int, int]:
    """`(pages, kept)`: the non-excluded unapproved pages a book re-run runs, and the approved ones."""
    rows = list(book.pages.filter(is_excluded=False).values_list("status", flat=True))
    kept = sum(1 for status in rows if status in APPROVED_PAGE_STATUSES)
    return len(rows) - kept, kept


def rerun_estimate(
    book: Book, stage: str, seconds: float | None = None, counts: tuple[int, int] | None = None
) -> dict:
    """The numbers of the book re-run confirmation (§3.6): `{pages, kept, minutes}`.

    `pages` are the non-excluded, unapproved pages (what `rerun_book` would run), `kept` the approved
    ones; `minutes` = ⌈pages × s ÷ 60⌉ with s = `qari_seconds`, only in «المعالجة» and only for the
    stages whose chain ends with the models (not `ocr_fast`, Tesseract alone), else null. `seconds`
    and `counts` let a caller that estimates several stages pay each query once.
    """
    pages, kept = counts if counts is not None else _rerun_counts(book)
    minutes = None
    if stage in MODEL_STAGES and not book.awaits_ocr_start:
        s = qari_seconds(book) if seconds is None else seconds
        minutes = math.ceil(pages * s / 60) if pages else 0
    return {"pages": pages, "kept": kept, "minutes": minutes}


def rerun_action(guides_mode: bool) -> dict:
    """The one book re-run «⋯» offers in a view (D101): `{stage, label}`, «إعادة التخطيط» (`preprocess`) in
    «التخطيط», «إعادة المعالجة» (`ocr`) outside it."""
    stage = RELAYOUT_STAGE if guides_mode else REPROCESS_STAGE
    return {"stage": stage, "label": RERUN_ACTION_LABELS[stage]}


def offered_rerun_stages(book: Book, has_pages: bool | None = None, guides_mode: bool = False) -> list[str]:
    """The stage of the book re-run «⋯» offers (`rerun_action`, D101), as a list of none or one: a
    started book always offers its view's; a book awaiting «بدء المعالجة» offers «إعادة التخطيط» only
    once it is `needs_guides`, or in `error` with pages."""
    if not book.awaits_ocr_start:
        return [rerun_action(guides_mode)["stage"]]
    if book.status == Book.Status.NEEDS_GUIDES:
        return list(LAYOUT_STAGES)
    if book.status == Book.Status.ERROR:
        has = book.pages.exists() if has_pages is None else has_pages
        return list(LAYOUT_STAGES) if has else []
    return []


def rerun_estimates(book: Book, stages: Sequence[str]) -> dict[str, dict]:
    """`{stage: rerun_estimate}` for the offered stages (the `OcrRun` query runs at most once)."""
    if not stages:
        return {}
    needs_time = not book.awaits_ocr_start and any(stage in MODEL_STAGES for stage in stages)
    seconds = qari_seconds(book) if needs_time else QARI_FALLBACK_SECONDS
    counts = _rerun_counts(book)
    return {stage: rerun_estimate(book, stage, seconds, counts) for stage in stages}


def kept_range(book: Book, source_pages: int | None = None) -> dict:
    """The PDF pages the book keeps, 1-based and inclusive (D66): `{source_pages, first, last, sheets,
    pages, text}`.

    The count is `source_pages` (the form's reading) or `Book.source_page_count` (`inspect_pdf`).
    `sheets` = last − first + 1 and `pages` = sheets × `pages_per_sheet`; `text` is the side panel's
    line («الصفحات 187–193 من 555», two per sheet «…، وفي كل منها صفحتان»). Nothing kept (or an
    unknown count): first / last null, 0 sheets and an empty text.
    """
    total = int(source_pages if source_pages is not None else book.source_page_count or 0)
    first = int(book.skip_first) + 1
    last = total - int(book.skip_last)
    if total <= 0 or first > last:
        return {"source_pages": total, "first": None, "last": None, "sheets": 0, "pages": 0, "text": ""}
    sheets = last - first + 1
    two_up = int(book.pages_per_sheet) == 2
    span = f"الصفحات {first}–{last}" if sheets > 1 else f"الصفحة {first}"
    text = f"{span} من {total}"
    if two_up:
        text += "، وفي كل منها صفحتان" if sheets > 1 else "، وفيها صفحتان"
    return {
        "source_pages": total,
        "first": first,
        "last": last,
        "sheets": sheets,
        "pages": sheets * (2 if two_up else 1),
        "text": text,
    }


def extraction_message(book: Book) -> str:
    """The success message of «استخراج الصفحات» on the new-book form, with the exact kept range."""
    from assembly.render import ar_count  # other app: lazy import

    kept = kept_range(book)
    two_up = int(book.pages_per_sheet) == 2
    count = ar_count(kept["pages"], PAGE_FORMS) + (" في الكتاب" if two_up else "")
    head = f"أُنشئ الكتاب «{book.title}»، وتُستخرج الآن"
    if not book.skip_first and not book.skip_last:
        return f"{head} صفحات الملف كلها ({count})."
    span = f"الصفحات {kept['first']}–{kept['last']}" if kept["sheets"] > 1 else f"الصفحة {kept['first']}"
    of = ar_count(kept["source_pages"], PAGE_FORMS_OF)
    return f"{head} {span} من {of} في الملف ({count})."


def delete_book(book: Book) -> str:
    """«حذف الكتاب»: delete the book and every row that hangs on it, then its files; returns the title.

    Every related table cascades (pages, guides, runs, lines, revisions, manuscript, snapshots,
    stylesheet, renders, exports). After the commit `MEDIA_ROOT/books/<id>/` is removed: every file
    of a book lives under it. Tasks still running for the book end quietly when their row is gone; a
    task that was mid-write can leave a stray file, which is harmless.
    """
    title, book_id = book.title, book.pk
    folder = Path(settings.MEDIA_ROOT) / "books" / str(book_id)
    with transaction.atomic():
        book.delete()
        transaction.on_commit(lambda: shutil.rmtree(folder, ignore_errors=True))
    log.info("book %s («%s») deleted", book_id, title)
    return title


# ====================================================================== read models for screens


def _bar_state(status: str) -> str:
    """Progress-fill modifier for a book status: done = success, error = danger, attention = warning."""
    if status == Book.Status.ERROR:
        return "danger"
    if status == Book.Status.NEEDS_GUIDES:
        return "warning"
    if status in (Book.Status.READY_FOR_REVIEW, Book.Status.REVIEWING, Book.Status.ASSEMBLED):
        return "success"
    return ""


def _pipeline_percent(by_status: dict[str, int], total: int, layout_stage: bool = False) -> int:
    """Share (0-100) of the current step the non-excluded pages have gone through.

    In «التخطيط» (`layout_stage`) it is the prepared share: pages past `uploaded` that did not fail. In
    «المعالجة» it is the read share, pages whose text the models have read (`_READ_STATUSES`) over all pages:
    the same ratio as the step's count «3/40», so the bar fills with it (owner 13). The old weighting (a
    third for preprocessing, a third for layout) put a book that had just left «التخطيط» at 33-67 % with no
    page read yet, since its pages arrive there already prepared (D64).
    """
    if total <= 0:
        return 0
    if layout_stage:
        prepared = total - by_status.get(Page.Status.UPLOADED, 0) - by_status.get(Page.Status.ERROR, 0)
        return int(round(100 * prepared / total))
    read = sum(by_status.get(status, 0) for status in _READ_STATUSES)
    return int(round(100 * read / total))


def book_progress(book: Book) -> dict:
    """Dashboard numbers: `total`, `by_status`, `percent`, `active`, `flags` (+ status and its label).

    `error_headline` / `error_detail` split the book's error message ('' unless the book is in error).
    `percent` is the current step's share of the non-excluded pages (`_pipeline_percent`): prepared pages
    in «التخطيط», pages whose text was read in «المعالجة». `flags` counts pages that carry attention
    flags or are in error. `active` is true while the book is processing or in OCR. `review` is
    `review.services.book_review_summary` (reviewed / total pages, unresolved words, next URL).
    `manuscript` is `assembly.services.manuscript_state` (exists, latest run, stale pages, …).
    `editor` is `{edited, version, drift_pages}` (D41: after the first editor save, the pages changed in
    review since) and `layout` `{trim, trim_label, page_count, rendering, rendered_at}` (the newest book
    render, D44); neither costs a query before the first assembly.
    """
    by_status: dict[str, int] = {status: 0 for status in Page.Status.values}
    flags = 0
    rows: list[tuple[int, int, str]] = []
    pages = book.pages.filter(is_excluded=False).values_list("id", "number", "status", "attention_flags")
    for pk, number, status, page_flags in pages:
        by_status[status] = by_status.get(status, 0) + 1
        flags += 1 if page_flags or status == Page.Status.ERROR else 0
        rows.append((pk, number, status))
    from assembly.services import manuscript_state  # other apps: lazy imports
    from editor.services import editor_state
    from publishing.engine import layout_state
    from review.services import book_review_summary

    failed = book.status == Book.Status.ERROR
    manuscript = manuscript_state(book, rows)
    return {
        **_progress_payload(book, by_status, flags),
        "review": book_review_summary(book),
        "manuscript": manuscript,
        "editor": editor_state(book, manuscript),
        "layout": layout_state(book, bool(manuscript.get("exists"))),
        "error_headline": _headline(book.error_message) if failed else "",
        "error_detail": _detail(book.error_message) if failed else "",
    }


def _progress_payload(book: Book, by_status: dict[str, int], flags: int) -> dict:
    """The `book_progress` dict from already counted pages (shared with `books_overview`).

    `layout_stage` is true while the book awaits «بدء المعالجة» (D64), and `waiting` once its pages
    are prepared (`needs_guides`): the books list then reads «بانتظار «بدء المعالجة»» instead of a bar.
    """
    total = sum(by_status.values())
    layout_stage = bool(book.awaits_ocr_start)
    return {
        "total": total,
        "by_status": by_status,
        "percent": _pipeline_percent(by_status, total, layout_stage),
        "active": book.status in ACTIVE_BOOK_STATUSES,
        "flags": flags,
        "status": book.status,
        "status_label": book.get_status_display(),
        "dot": status_dot(book.status),
        "bar_state": _bar_state(book.status),
        "layout_stage": layout_stage,
        "waiting": layout_stage and book.status == Book.Status.NEEDS_GUIDES,
    }


def books_overview() -> list[dict]:
    """Rows for the books list: each book with its page count, progress and status colour.

    Two grouped queries over the pages serve every book (no query per book).
    """
    books = list(Book.objects.all())
    by_book: dict[int, dict[str, int]] = {book.pk: {s: 0 for s in Page.Status.values} for book in books}
    flags: dict[int, int] = dict.fromkeys(by_book, 0)
    pages = Page.objects.filter(is_excluded=False, book_id__in=list(by_book))
    for row in pages.values("book_id", "status").annotate(n=Count("id")):
        by_book[row["book_id"]][row["status"]] = row["n"]
    for book_id, page_flags, status in pages.values_list("book_id", "attention_flags", "status"):
        if page_flags or status == Page.Status.ERROR:
            flags[book_id] += 1
    return [{"book": book, **_progress_payload(book, by_book[book.pk], flags[book.pk])} for book in books]


def _file_url(field) -> str | None:
    """URL of a stored file field, None when it is empty."""
    return field.url if field else None


def _headline(message: str | None) -> str:
    """First line of an error message (the Arabic headline); the rest is technical detail."""
    lines = (message or "").splitlines()
    return lines[0] if lines else ""


def _detail(message: str | None) -> str:
    """Everything after the first line of an error message."""
    return "\n".join((message or "").splitlines()[1:]).strip()


def _preprocess_of(page: Page):
    """The page's `processing.models.Preprocess` row, or None before preprocessing."""
    try:
        return page.preprocess
    except ObjectDoesNotExist:
        return None


MIN_NUMBERED_PAGES = 3


def _in_sequence(prev: tuple[int, int], cur: tuple[int, int]) -> bool:
    """Is `cur` (position, number) a plausible successor of `prev`: larger, by at most the scan distance?"""
    return prev[1] < cur[1] <= prev[1] + (cur[0] - prev[0])


def page_sequence_issues(book: Book) -> dict[int, str]:
    """Printed page numbers that do not follow the scan order: `{page_id: Arabic label}`.

    Only non-excluded pages with a `printed_number` take part, and nothing is reported for a book
    with fewer than `MIN_NUMBERED_PAGES` numbered pages. Walking the numbered pages in scan order,
    a number is in sequence when it is larger than the previous one by at least 1 and at most the
    number of scan positions between them (unread numbers and unnumbered plates do not raise gaps).
    Because a single digit is misread now and then, a break is only reported as a gap or duplicate
    when the *next* numbered page confirms the new numbering; when the next page instead continues
    from the page before the break, the odd page is reported as an uncertain read and skipped. A break
    on the last numbered page cannot be confirmed and is reported as a gap. Digits are Western (D6).
    """
    rows = book.pages.filter(is_excluded=False).order_by("number").values_list("pk", "printed_number")
    return sequence_issues_of(list(rows))


def sequence_issues_of(rows: Sequence[tuple[int, str]]) -> dict[int, str]:
    """`page_sequence_issues` over `(page_id, printed_number)` of the non-excluded pages in scan order."""
    numbered: list[tuple[int, int, int]] = [  # (position, page_id, number)
        (pos, page_id, int(printed))
        for pos, (page_id, printed) in enumerate(rows)
        if printed and printed.isdigit()
    ]
    if len(numbered) < MIN_NUMBERED_PAGES:
        return {}
    issues: dict[int, str] = {}
    prev: tuple[int, int] | None = None
    i = 0
    while i < len(numbered):
        pos, page_id, num = numbered[i]
        cur = (pos, num)
        if prev is None or _in_sequence(prev, cur):
            prev = cur
            i += 1
            continue
        nxt = (numbered[i + 1][0], numbered[i + 1][2]) if i + 1 < len(numbered) else None
        if nxt is not None and not _in_sequence(cur, nxt) and _in_sequence(prev, nxt):
            expected = prev[1] + (pos - prev[0])
            issues[page_id] = f"رقم مطبوع غير مؤكد: قُرئ {num} والمتوقع {expected}"
            i += 1  # keep `prev`: the odd page is treated as a misread
            continue
        if num == prev[1]:
            issues[page_id] = f"ترقيم مكرّر: بعد {prev[1]} جاءت {num} مرة أخرى"
        else:
            issues[page_id] = f"ترقيم غير متسلسل: بعد {prev[1]} جاءت {num}"
        prev = cur
        i += 1
    return issues


def _readers(page: Page) -> str:
    """How the page was read (D73, `ocr.services.readers_of`): two / one / tesseract, '' before 7b."""
    from ocr.services import readers_of  # other app: lazy import

    return readers_of(page.reading)


def page_tile(page: Page, sequence_issue: str = "", compact: bool = False) -> dict:
    """One thumbnail tile of the dashboard grid (also the per-page item of the progress API).

    `sequence_issue` is the page's label from `page_sequence_issues` ('' when in order).
    `compact` keeps only what changes while processing (the progress poll, ≤ 150 B per page): no
    URLs, labels, dots, thumbnails or sizes; `flag_labels` only when flagged, the error fields only on error.
    """
    failed = page.status == Page.Status.ERROR
    retry_stage = page.error_from if failed and page.error_from in STAGES else ""
    if compact:
        return _compact_tile(page, sequence_issue, failed, retry_stage)
    preprocess = _preprocess_of(page)
    return {
        "id": page.pk,
        "number": page.number,
        "status": page.status,
        "status_label": page.get_status_display(),
        "dot": status_dot(page.status),
        "text_state": page.text_state,
        "flags": list(page.attention_flags or []),
        "flag_labels": [item["label"] for item in flag_items(page.attention_flags)],
        "n_flags": len(page.attention_flags or []),
        "printed_number": page.printed_number,
        "sequence_issue": sequence_issue,
        "is_excluded": page.is_excluded,
        "error": page.status == Page.Status.ERROR,
        "error_headline": _headline(page.error_message) if page.status == Page.Status.ERROR else "",
        # The dashboard's attention list offers a retry from the failed stage (D22 designed failure).
        "retry_stage": retry_stage,
        "retry_label": STAGE_LABELS[retry_stage] if retry_stage else "",
        "rerun_url": reverse("books:rerun", args=[page.book_id, page.number]),
        "thumb_url": _file_url(preprocess.thumbnail) if preprocess else None,
        "scan_thumb_url": _file_url(page.scan_thumbnail),
        "url": sheet_url(page.book_id, page.number),
        "n_unresolved": page.n_unresolved,
        # How the page was read (D73): the half-disc marks one reader ('' before 7b).
        "readers": _readers(page),
        "is_reviewed": page.status in (Page.Status.REVIEWED, Page.Status.ASSEMBLED),
        "review_url": reverse("review:page", args=[page.book_id, page.number]),
        # D76: the tile's link — review once the page's text is final (and the page is in the book), else
        # the page detail; the compact tile has no URLs (the grid derives it from `text_state`)
        "primary_url": primary_url(page),
        # Image size for the stacked view's aspect-ratio placeholders (cleaned image first, then the scan).
        "width": (preprocess.output_width if preprocess else 0) or page.width,
        "height": (preprocess.output_height if preprocess else 0) or page.height,
    }


def primary_url(page: Page) -> str:
    """Where a page's tile leads (D76, D84): review when its text is final and it is not excluded, else the
    page's sheet in the «التخطيط» mode."""
    if page.text_state == Page.TextState.FINAL and not page.is_excluded:
        return reverse("review:page", args=[page.book_id, page.number])
    return sheet_url(page.book_id, page.number)


def sheet_url(book_id: int, number: int) -> str:
    """The page's sheet in the «التخطيط» mode: `/books/<id>/guides/#sheet-<n>` (D84)."""
    return f"{reverse('books:guides', args=[book_id])}#sheet-{number}"


def _compact_tile(page: Page, sequence_issue: str, failed: bool, retry_stage: str) -> dict:
    """The `compact` form of `page_tile`."""
    n_flags = len(page.attention_flags or [])
    tile = {
        "id": page.pk,
        "number": page.number,
        "status": page.status,
        "text_state": page.text_state,
        "n_unresolved": page.n_unresolved,
        "n_flags": n_flags,
        "sequence_issue": sequence_issue,
        "is_excluded": page.is_excluded,
        "is_reviewed": page.status in (Page.Status.REVIEWED, Page.Status.ASSEMBLED),
        "error": failed,
        "printed_number": page.printed_number,
    }
    if n_flags:
        tile["flag_labels"] = [item["label"] for item in flag_items(page.attention_flags)]
    readers = _readers(page)
    if readers and readers != "two":  # the compact tile carries only a weak reading (the half-disc)
        tile["readers"] = readers
    if failed:
        tile["error_headline"] = _headline(page.error_message)
        tile["retry_stage"] = retry_stage
        tile["retry_label"] = STAGE_LABELS[retry_stage] if retry_stage else ""
    return tile


# Large columns the tiles never read (the progress API polls every two seconds).
_TILE_DEFERRED: tuple[str, ...] = (
    "text_layer_text",
    "provisional_text",
    "final_text",
    "guides_override",
    "preprocess__line_boxes",
    "preprocess__auto_params",
    "preprocess__edge_strips_removed",
)


def page_tiles(book: Book, compact: bool = False) -> list[dict]:
    """Tiles for every page of the book in order (excluded pages included, marked); one query.

    `compact` gives the small poll form of `page_tile` (and skips the Preprocess join).
    """
    if compact:
        pages = list(book.pages.defer(*_TILE_DEFERRED[:4]).order_by("number"))
    else:
        pages = list(book.pages.select_related("preprocess").defer(*_TILE_DEFERRED).order_by("number"))
    issues = sequence_issues_of([(page.pk, page.printed_number) for page in pages if not page.is_excluded])
    return [page_tile(page, issues.get(page.pk, ""), compact=compact) for page in pages]


def attention_pages(book: Book) -> list[dict]:
    """Non-excluded pages with attention flags, an error or a page-numbering issue, for the dashboard.

    An approved page (`reviewed`, `assembled`) leaves the list (D73): the reviewer has looked at it.
    It stays only for a page-numbering issue, which approval does not settle.
    """
    items = []
    issues = page_sequence_issues(book)
    pages = book.pages.filter(is_excluded=False).defer(*_TILE_DEFERRED[:4]).order_by("number")
    for page in pages:
        if not page.attention_flags and page.status != Page.Status.ERROR and page.pk not in issues:
            continue
        if page.status in (Page.Status.REVIEWED, Page.Status.ASSEMBLED) and page.pk not in issues:
            continue
        items.append(
            {
                "page": page,
                "flags": flag_items(page.attention_flags),
                "sequence_issue": issues.get(page.pk, ""),
                "error": page.status == Page.Status.ERROR,
                "error_headline": _headline(page.error_message),
                "url": primary_url(page),
            }
        )
    return items


def guides_url(book: Book) -> str:
    """URL of the «التخطيط» mode of the dashboard (the «⋯» item «التخطيط», D67; its own address, D84)."""
    return reverse("books:guides", args=[book.pk])


def has_guides(book: Book) -> bool:
    """True when the book already has `LayoutGuides` (auto-proposed or manual)."""
    try:
        return book.guides is not None
    except ObjectDoesNotExist:
        return False


PAGE_NUMBER_SLOT = "__n__"


def _url_template(name: str, book_id: int) -> str:
    """`reverse(name, [book_id, 0])` with the page number replaced by `PAGE_NUMBER_SLOT`."""
    url = reverse(name, args=[book_id, 0])
    head, sep, tail = url.rpartition("/0/")
    return f"{head}/{PAGE_NUMBER_SLOT}/{tail}" if sep else url


def page_url_templates(book: Book) -> dict[str, str]:
    """Per-page URLs of the dashboard with `__n__` for the page number (pages ingested after load)."""
    return {
        "page": f"{reverse('books:guides', args=[book.pk])}#sheet-{PAGE_NUMBER_SLOT}",
        "review": _url_template("review:page", book.pk),
        "rerun": _url_template("books:rerun", book.pk),
        "exclude": _url_template("books:toggle_exclude", book.pk),
    }


GUIDES_VIEW = "guides"  # `book_dashboard(book, GUIDES_VIEW)`: the «التخطيط» mode once «المعالجة» started


def start_action(book: Book, guides_mode: bool) -> str:
    """The primary of the «التخطيط» mode (§3.11 `startAction`), '' outside the mode."""
    if not guides_mode:
        return ""
    if not book.awaits_ocr_start:
        return "back"
    return {
        Book.Status.UPLOADED: "extract",
        Book.Status.ERROR: "reextract",
        Book.Status.NEEDS_GUIDES: "startOcr",
        Book.Status.PROCESSING: "startOcrDisabled",
    }.get(book.status, "startOcrDisabled")


def guides_urls(book: Book) -> dict[str, str]:
    """The URLs the «التخطيط» mode posts to (`__id__` stands for a page id)."""
    page_url = reverse("api:page_guides_override", args=[0]).replace("/0/", "/__id__/")
    return {
        "state": reverse("api:book_guides", args=[book.pk]),
        "apply": reverse("api:book_guides", args=[book.pk]),
        "preview": reverse("api:book_guides_preview", args=[book.pk]),
        "page": page_url,
        "startOcr": reverse("books:start_ocr", args=[book.pk]),
        "extract": reverse("books:start", args=[book.pk]),
        "delete": reverse("books:delete", args=[book.pk]),
        "back": reverse("books:detail", args=[book.pk]),
    }


def book_dashboard(book: Book, view: str | None = None) -> dict:
    """Everything the dashboard template needs, including the Alpine component's initial state.

    `config.manuscript` is the compact manuscript state (as in the progress poll) and
    `config.manuscriptUrls` the assemble / state / manuscript URLs (`assembly.services.manuscript_urls`);
    `config.editor` / `config.layout` the editor and layout states of the progress poll and
    `config.editorUrls` the editor, layout, chapters, stylesheet and preview URLs
    (`editor.services.editor_urls`).

    The «التخطيط» mode (D67) is on while the book awaits «بدء المعالجة», or with `view == "guides"`;
    it adds `guidesMode`, `layoutStage`, `startAction`, the book guides, the detection line, the kept
    range, `textLayer` and `guidesUrls` to the config (§3.11). Every book carries `rerun`: the
    estimate of the offered book re-run (`rerun_action`, D101: «إعادة التخطيط» in the mode, «إعادة المعالجة»
    outside it), so the confirmation opens without a request.
    """
    from assembly.services import manuscript_urls  # other app: lazy import
    from editor.services import editor_urls
    from processing import services as processing

    progress = book_progress(book)
    by_status = progress["by_status"]
    labels = dict(Page.Status.choices)
    stages = [
        {"key": "uploaded", "statuses": [Page.Status.UPLOADED], "label": labels[Page.Status.UPLOADED]},
        {
            "key": "preprocessed",
            "statuses": [Page.Status.PREPROCESSED],
            "label": labels[Page.Status.PREPROCESSED],
        },
        {
            "key": "layout_done",
            "statuses": [Page.Status.LAYOUT_DONE],
            "label": labels[Page.Status.LAYOUT_DONE],
        },
        {
            "key": "ocr_done",
            "statuses": [Page.Status.OCR_DONE, Page.Status.REVIEWED, Page.Status.ASSEMBLED],
            "label": labels[Page.Status.OCR_DONE],
        },
        {"key": "error", "statuses": [Page.Status.ERROR], "label": labels[Page.Status.ERROR]},
    ]
    for stage, state in zip(stages, ("neutral", "", "", "success", "danger"), strict=True):
        stage["label"] = str(stage["label"])
        stage["state"] = state
        stage["count"] = sum(by_status.get(status, 0) for status in stage["statuses"])
        stage["percent"] = int(round(100 * stage["count"] / progress["total"])) if progress["total"] else 0
        stage["dot"] = status_dot(stage["statuses"][0])
    tiles = page_tiles(book)
    layout_stage = bool(book.awaits_ocr_start)
    guides_mode = layout_stage or view == GUIDES_VIEW
    offered = offered_rerun_stages(book, has_pages=bool(tiles), guides_mode=guides_mode)
    rerun = rerun_estimates(book, offered)
    config = {
        "progressUrl": reverse("api:book_progress", args=[book.pk]),
        "active": progress["active"],
        "total": progress["total"],
        "percent": progress["percent"],
        "flags": progress["flags"],
        "status": progress["status"],
        "statusLabel": progress["status_label"],
        "dot": progress["dot"],
        "barState": progress["bar_state"],
        "errorHeadline": progress["error_headline"],
        "errorDetail": progress["error_detail"],
        "byStatus": by_status,
        "stages": [{"key": stage["key"], "statuses": stage["statuses"]} for stage in stages],
        "pages": tiles,
        "review": progress["review"],
        "manuscript": progress["manuscript"],
        "manuscriptUrls": manuscript_urls(book),
        "editor": progress["editor"],
        "layout": progress["layout"],
        "editorUrls": editor_urls(book),
        "sheetsUrl": reverse("api:book_sheets", args=[book.pk]),
        "sheetsMax": SHEETS_MAX,
        "statusLabels": {status: str(label) for status, label in Page.Status.choices},
        "statusDots": {status: status_dot(status) for status in Page.Status.values},
        "urls": page_url_templates(book),
        "guidesMode": guides_mode,
        "layoutStage": layout_stage,
        "rerun": rerun,
        # the stage bar (D76): the dashboard is «التخطيط» in its mode, «المعالجة» otherwise
        "stageBar": book_stages(book, "pages" if guides_mode else "ocr"),
        "stageBarUrl": reverse("api:book_stages", args=[book.pk]),
    }
    context: dict = {"guides_mode": guides_mode, "layout_stage": layout_stage, "start_action": ""}
    if guides_mode:
        from processing.models import LayoutGuides

        guides = LayoutGuides.objects.filter(book=book).first()
        stats = processing.guides_summary(book)
        kept = kept_range(book)
        action = start_action(book, guides_mode)
        config.update(
            {
                "startAction": action,
                "guides": processing.book_guides_view(guides),
                "guidesStats": stats,
                "keptRange": kept,
                "textLayer": book_uses_text_layer(book),
                "guidesUrls": guides_urls(book),
            }
        )
        context.update({"start_action": action, "kept_range": kept, "guides_stats": stats})
    return {
        "book": book,
        "progress": progress,
        "stages": stages,
        "tiles": tiles,
        "attention": attention_pages(book),
        "has_guides": has_guides(book),
        "guides_url": guides_url(book),
        "can_start": book.status in (Book.Status.UPLOADED, Book.Status.ERROR),
        "rerun_action": rerun_action(guides_mode),  # the «⋯» item (D101); `rerun` is {} while none is offered
        "rerun": rerun,
        "start_ocr_url": reverse("books:start_ocr", args=[book.pk]),
        "delete_url": reverse("books:delete", args=[book.pk]),
        "extract_url": reverse("books:start", args=[book.pk]),
        "error_headline": _headline(book.error_message),
        "error_detail": _detail(book.error_message),
        "review": progress["review"],
        "manuscript": progress["manuscript"],
        "manuscript_urls": config["manuscriptUrls"],
        "editor": progress["editor"],
        "layout": progress["layout"],
        "editor_urls": config["editorUrls"],
        "config": config,
        "stage_steps": config["stageBar"],
        **context,
    }


SHEETS_MAX = 40  # pages per call of the stacked-sheets API


class SheetsRangeError(ValueError):
    """A bad `from` / `to` query of the sheets API (Arabic message)."""


def sheets_range(start: str | None, end: str | None) -> tuple[int, int]:
    """Validated `(from, to)` page numbers for `book_sheets`: defaults 1 and from + 39, capped at 40 pages."""

    def number(raw: str | None, default: int) -> int:
        if raw in (None, ""):
            return default
        text = str(raw).strip()
        if not (text.isascii() and text.isdigit()) or int(text) < 1:
            raise SheetsRangeError("رقم الصفحة في الطلب غير صالح.")
        return int(text)

    first = number(start, 1)
    last = number(end, first + SHEETS_MAX - 1)
    if last < first:
        raise SheetsRangeError("نطاق الصفحات غير صالح: «إلى» قبل «من».")
    return first, min(last, first + SHEETS_MAX - 1)


def _ratio_boxes(line_boxes: list | None, width: int, height: int) -> list[list[float]]:
    """Detected line boxes (`{x0, y0, x1, y1}` gray pixels) as `[x0, y0, x1, y1]` ratios of the page."""
    if not width or not height:
        return []
    out = []
    for box in line_boxes or []:
        try:
            x0, y0, x1, y1 = (float(box[k]) for k in ("x0", "y0", "x1", "y1"))
        except (KeyError, TypeError, ValueError):
            continue
        out.append([round(x0 / width, 4), round(y0 / height, 4), round(x1 / width, 4), round(y1 / height, 4)])
    return out


def _ratio_box(box, width: int, height: int) -> list[float] | None:
    """A `[x0, y0, x1, y1]` box in gray pixels as ratios of the page (4 decimals); None when unusable."""
    if not width or not height or not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    return [round(x0 / width, 4), round(y0 / height, 4), round(x1 / width, 4), round(y1 / height, 4)]


def _ratio(value: float | None, size: int) -> float | None:
    """`value / size` to 4 decimals, None when either is missing."""
    if value is None or not size:
        return None
    return round(float(value) / size, 4)


def _fast_runs_by_target(page_ids: list[int]) -> RunsByTarget:
    """Latest OK fast-engine (Tesseract) run per `(page_id, region_id)`; page-level runs under region None.

    One query for the whole range; a region run whose region was deleted (region None, scope region)
    is ignored.
    """
    from ocr.models import OcrRun  # other app: lazy import
    from ocr.services import PAGE_SCOPE, engine_names

    runs = (
        OcrRun.objects.filter(page_id__in=page_ids, engine_name=engine_names()[2], status=OcrRun.Status.OK)
        .only("id", "page_id", "region_id", "params", "created_at")
        .order_by("-created_at", "-id")
    )
    latest: RunsByTarget = {}
    for run in runs:
        params = run.params or {}
        if run.region_id is None and params.get("scope") != PAGE_SCOPE:
            continue
        latest.setdefault((run.page_id, run.region_id), run)
    return latest


def provisional_lines(
    page: Page, regions: list[Region], runs: RunsByTarget, width: int, height: int
) -> list[dict]:
    """Tesseract lines of the provisional text: `[{"region_kind", "bbox" (ratios | None), "words"}]`.

    Targets are the page's OCR-able regions in `order` (non-footnote kinds first, footnotes last, as in
    `ocr.services.join_region_texts`), or the page-level run without regions; one entry per
    `params["lines"]` item. A first / last line that is only a page number is dropped (as
    `provisional_text` does: with a page-number region only when it repeats `printed_number`).
    Without any Tesseract lines, the non-blank lines of `provisional_text` are used with
    `bbox: None`. Empty while the page has no text.
    """
    from ocr.services import FOOTNOTE_KINDS, SKIPPED_KINDS, page_number_edges
    from processing.models import Region

    if page.text_state == Page.TextState.NONE:
        return []
    if regions:
        targets = [(r.kind, runs.get((page.pk, r.pk))) for r in regions if r.kind not in SKIPPED_KINDS]
        targets.sort(key=lambda item: item[0] in FOOTNOTE_KINDS)  # stable: region order kept
    else:
        targets = [("body", runs.get((page.pk, None)))]
    out: list[dict] = []
    for kind, run in targets:
        for line in ((run.params or {}).get("lines") or []) if run is not None else []:
            words = [str(w.get("text", "")) for w in line.get("words") or [] if isinstance(w, dict)]
            box = _ratio_box(line.get("bbox"), width, height)
            out.append({"region_kind": kind, "bbox": box, "words": words})
    if not out:
        return [
            {"region_kind": "body", "bbox": None, "words": text.split()}
            for text in (page.provisional_text or "").split("\n")
            if text.strip()
        ]
    has_region = any(r.kind == Region.Kind.PAGE_NUMBER for r in regions)
    drop, _digits = page_number_edges(
        [" ".join(entry["words"]) for entry in out], has_region=has_region, known=page.printed_number
    )
    return [entry for i, entry in enumerate(out) if i not in drop]


def book_sheets(book: Book, first: int, last: int, guides: bool = False) -> dict:
    """Pages `first..last` of the book for the stacked-sheets view (five queries in all).

    Per page: status, provisional text and its Tesseract lines (`provisional_lines`), final lines
    with their id, box and tokens (`t`, `conf`, `res`), image URLs and size (gray-image pixels, or
    the original's before preprocessing), detected line boxes, regions (without running header and
    page number), footnote start and median line height, all boxes and heights as 0..1 ratios of the
    page, review state and URLs. The response also carries `book_line_h_px`: the book's typical
    printed line height (median of the pages' detected line heights in gray-image pixels, excluded and
    unprocessed pages left out; 0 when unknown), so every sheet sets its text at the same size (D30).

    With `guides` (the «التخطيط» mode, §3.11) each item carries the `guides` block
    (`processing.services.page_guides_payload`) and the lines and fast runs are not loaded
    (`lines` and `provisional_lines` stay empty); still at most five queries.
    """
    from ocr.models import Line  # other apps: lazy imports
    from processing.models import Region

    pages = list(
        book.pages.filter(number__gte=first, number__lte=last)
        .select_related("preprocess")
        .defer("text_layer_text", "final_text", "preprocess__auto_params")
        .order_by("number")
    )
    page_ids = [page.pk for page in pages]
    lines_of: dict[int, list] = {pk: [] for pk in page_ids}
    if not guides:
        rows = (
            Line.objects.filter(page_id__in=page_ids)
            .select_related("region")
            .only("page_id", "order", "bbox", "tokens", "role", "region__kind")
            .order_by("page_id", "order", "id")
        )
        for line in rows:
            lines_of[line.page_id].append(line)
    regions_of: dict[int, list] = {pk: [] for pk in page_ids}
    for region in Region.objects.filter(page_id__in=page_ids).order_by("page_id", "order", "id"):
        regions_of[region.page_id].append(region)
    runs = _fast_runs_by_target(page_ids) if page_ids and not guides else {}
    out = [
        _sheet(book, page, lines_of[page.pk], regions_of[page.pk], runs, text=not guides) for page in pages
    ]
    if guides and pages:
        _add_guides_blocks(book, pages, regions_of, out)
    # one query for both the page total and the book's typical line height (was a plain count)
    heights = list(book.pages.values_list("is_excluded", "preprocess__median_line_height"))
    typical = [float(h) for excluded, h in heights if not excluded and h]
    return {
        "book_id": book.pk,
        "from": first,
        "to": last,
        "total": len(heights),
        "book_line_h_px": round(statistics.median(typical), 2) if typical else 0,
        "pages": out,
    }


def _add_guides_blocks(book: Book, pages: list[Page], regions_of: dict[int, list], items: list[dict]) -> None:
    """Put the `guides` block on each sheet item (two queries: the book guides with the reference page their
    running-head cut is anchored on, owner 18, and the review work)."""
    from processing import services as processing  # other app: lazy imports
    from processing.models import LayoutGuides

    guides = LayoutGuides.objects.select_related("reference_page__preprocess").filter(book=book).first()
    values, manual = processing.guides_values(guides), processing.is_manual(guides)
    anchor = processing.guides_anchor(guides)
    awaits = bool(book.awaits_ocr_start)
    review = set() if awaits else processing.review_work_pages(book.pk, [page.pk for page in pages])
    for page, item in zip(pages, items, strict=True):
        locked = processing.page_lock(page, awaits, review)
        pre = _preprocess_of(page)
        item["guides"] = processing.page_guides_payload(
            page, pre, values, manual, regions_of[page.pk], locked, anchor
        )


def _sheet(
    book: Book, page: Page, lines: list[Line], regions: list[Region], runs: RunsByTarget, text: bool = True
) -> dict:
    """One page of `book_sheets` from its already loaded lines, regions and fast runs (`text=False`:
    no provisional lines, which need the fast runs)."""
    from ocr.services import SKIPPED_KINDS, line_kind

    pre = _preprocess_of(page)
    width = (pre.output_width if pre is not None else 0) or page.width
    height = (pre.output_height if pre is not None else 0) or page.height
    display = (_file_url(pre.display_image) or _file_url(pre.gray_image)) if pre is not None else None
    footnote_y = None
    if pre is not None:
        start = pre.footnote_rule_y if pre.footnote_rule_y is not None else pre.footnote_block_y
        footnote_y = _ratio(start, height)
    region_boxes = []
    for region in regions:
        box = _ratio_box(region.bbox, width, height)
        if region.kind not in SKIPPED_KINDS and box is not None:
            region_boxes.append({"kind": region.kind, "bbox": box})
    return {
        "id": page.pk,
        "number": page.number,
        "status": page.status,
        "status_label": page.get_status_display(),
        "dot": status_dot(page.status),
        "text_state": page.text_state,
        "provisional_text": page.provisional_text,
        "provisional_lines": provisional_lines(page, regions, runs, width, height) if text else [],
        "lines": [
            {
                "id": line.pk,
                "order": line.order,
                "region_kind": (kind := line.region.kind if line.region_id and line.region else "body"),
                # what the line is in the book (D74): the footnote role makes a note, `main` body text
                "kind": line_kind(line.role, kind),
                "bbox": _ratio_box(line.bbox, width, height),
                "tokens": [
                    {"t": token.get("t", ""), "conf": token.get("conf", "high"), "res": token.get("res")}
                    for token in line.tokens or []
                ],
            }
            for line in lines
        ],
        "regions": region_boxes,
        "footnote_y": footnote_y,
        "median_line_h": (_ratio(pre.median_line_height, height) or 0) if pre is not None else 0,
        "display_url": display,
        "scan_url": _file_url(page.original_image),
        "thumb_url": _file_url(pre.thumbnail) if pre is not None else None,
        "scan_thumb_url": _file_url(page.scan_thumbnail),
        "width": width,
        "height": height,
        "line_boxes": _ratio_boxes(pre.line_boxes, width, height) if pre is not None else [],
        "n_lines": pre.n_lines if pre is not None else 0,
        "n_unresolved": page.n_unresolved,
        "is_reviewed": page.status in (Page.Status.REVIEWED, Page.Status.ASSEMBLED),
        "is_excluded": page.is_excluded,
        "printed_number": page.printed_number,
        "error": _headline(page.error_message) if page.status == Page.Status.ERROR else "",
        "url": sheet_url(book.pk, page.number),
        "review_url": reverse("review:page", args=[book.pk, page.number]),
    }


def _clean_text(text: str) -> str:
    """Western digits, whitespace collapsed per line, paragraphs kept apart by one blank line."""
    paragraphs = re.split(r"\n\s*\n", to_western_digits(text).replace("\r\n", "\n"))
    return "\n\n".join(p for p in (normalize_ws(par) for par in paragraphs) if p)


def book_text(book: Book) -> dict:
    """Clean text of the whole book for one-click copy: `{"text", "pages"}`.

    Non-excluded pages in order, each contributing its `final_text` when present, else its
    `provisional_text`; digits are Western (D6) and whitespace is normalised (line breaks kept).
    Pages are separated by one blank line; `pages` counts the pages that contributed text.
    Diacritics are kept as stored.
    """
    rows = (
        book.pages.filter(is_excluded=False).order_by("number").values_list("final_text", "provisional_text")
    )
    parts = [_clean_text(final or provisional or "") for final, provisional in rows]
    parts = [part for part in parts if part]
    return {"text": "\n\n".join(parts), "pages": len(parts)}


# ====================================================================== the stage bar (D76, PHASE7_SPEC §5.1)

STAGE_KEYS: tuple[str, ...] = ("pages", "ocr", "review", "manuscript", "book", "export")
STAGE_NAMES: dict[str, str] = {
    "pages": "التخطيط",
    "ocr": "المعالجة",
    "review": "المراجعة",
    "manuscript": "المخطوطة",
    "book": "الكتاب",
    "export": "الإخراج",
}
STATE_WORDS: dict[str, str] = {  # the visually hidden words of each state («(مكتملة)» …)
    "todo": "لم تبدأ",
    "active": "جارية",
    "done": "مكتملة",
    "stale": "أقدم من النص",
    "attention": "تحتاج انتباهًا",
    "blocked": "غير متاحة بعد",
}
PAGES_NOUN = ("صفحة واحدة", "صفحتان", "صفحات", "صفحة")
PAGES_OF = ("صفحة واحدة", "صفحتين", "صفحات", "صفحة")  # after a noun («نص صفحتين»)
CHAPTERS_NOUN = ("فصل واحد", "فصلان", "فصول", "فصلًا")
MINUTES = ("دقيقة", "دقيقتين", "دقائق", "دقيقة")
HOURS = ("ساعة", "ساعتين", "ساعات", "ساعة")
DAYS = ("يوم", "يومين", "أيام", "يومًا")
EXPORT_NAMES: dict[str, str] = {"docx": "Word", "print_pdf": "PDF", "screen_pdf": "PDF", "epub": "EPUB"}
_READ = frozenset({Page.Status.OCR_DONE, Page.Status.REVIEWED, Page.Status.ASSEMBLED})
_APPROVED = frozenset({Page.Status.REVIEWED, Page.Status.ASSEMBLED})


def _count(n: int, forms: tuple[str, str, str, str]) -> str:
    from assembly.render import ar_count  # other app: lazy import

    return ar_count(n, forms)


def relative_time(when, now=None) -> str:
    """«قبل لحظات», «قبل 5 دقائق», «قبل ساعتين», «قبل 3 أيام», then «في 2026-08-01» (as the manuscript's
    `relativeTime`); '' without a time."""
    if when is None:
        return ""
    seconds = max(0, round(((now or timezone.now()) - when).total_seconds()))
    if seconds < 45:
        return "قبل لحظات"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"قبل {_count(max(1, minutes), MINUTES)}"
    hours = round(minutes / 60)
    if hours < 24:
        return f"قبل {_count(hours, HOURS)}"
    days = round(hours / 24)
    if days < 30:
        return f"قبل {_count(days, DAYS)}"
    return f"في {when.date().isoformat()}"


class StageFacts:
    """What the stage bar and review's next step read about a book, in a fixed number of queries: the pages'
    counts (one query), the manuscript state (`assembly.services.manuscript_state`, up to three), the book's
    trim and page count (two, three with no live layout) and the exports (one)."""

    def __init__(self, book: Book):
        from assembly.services import manuscript_state  # other apps: lazy imports
        from editor.models import TRIM_PRESETS, StyleSheet
        from publishing.models import Export, LiveLayout, PreviewRender

        self.book = book
        rows = list(book.pages.filter(is_excluded=False).values_list("id", "number", "status", "error_from"))
        self.rows = rows
        self.total = len(rows)
        statuses = [status for _pk, _n, status, _from in rows]
        self.uploaded = statuses.count(Page.Status.UPLOADED)
        layout_errors = [
            n
            for _pk, n, status, where in rows
            if status == Page.Status.ERROR and (book.awaits_ocr_start or where == "preprocess")
        ]
        self.layout_errors = len(layout_errors)
        self.ocr_errors = statuses.count(Page.Status.ERROR) - self.layout_errors
        self.read = sum(1 for status in statuses if status in _READ)
        self.reviewed = sum(1 for status in statuses if status in _APPROVED)
        pending = sorted(n for _pk, n, status, _from in rows if status == Page.Status.OCR_DONE)
        self.pending = pending
        self.processing = sum(
            1
            for status in statuses
            if status in (Page.Status.UPLOADED, Page.Status.PREPROCESSED, Page.Status.LAYOUT_DONE)
        )
        self.first_page = min((n for _pk, n, _s, _f in rows), default=None)
        self.manuscript = manuscript_state(book, [(pk, n, status) for pk, n, status, _from in rows])
        self.trim_label = ""
        self.page_count = None
        if self.manuscript.get("exists"):
            sheet = (
                StyleSheet.objects.filter(book_id=book.pk).only("id", "trim", "width_mm", "height_mm").first()
            )
            sheet = sheet or StyleSheet()
            self.trim_label = (
                TRIM_PRESETS[sheet.trim][0]
                if sheet.trim in TRIM_PRESETS
                else f"{sheet.width_mm:g}×{sheet.height_mm:g} مم"
            )
            live = LiveLayout.objects.filter(book_id=book.pk).only("page_count", "revision").first()
            if live is not None and live.revision:
                self.page_count = live.page_count
            else:
                render = (
                    PreviewRender.objects.filter(book_id=book.pk, scope="book", status="done")
                    .only("page_count")
                    .order_by("-created_at", "-id")
                    .first()
                )
                self.page_count = render.page_count if render is not None else None
        self.exports = (
            list(
                Export.objects.filter(book_id=book.pk)
                .exclude(status=Export.Status.CANCELLED)
                .order_by("-created_at", "-id")
                .values("format", "status", "manuscript_version", "finished_at", "error")[:60]
            )
            if self.manuscript.get("exists")
            else []
        )


def _step(key: str, url: str | None, state: str, detail: str, hint: str, count: str | None = None) -> dict:
    return {
        "key": key,
        "label": STAGE_NAMES[key],
        "url": url if state != "blocked" else None,
        "state": state,
        "state_label": STATE_WORDS[state],
        "detail": detail,
        "hint": hint,
        "count": count,
        "current": False,
    }


def _pages_step(facts: StageFacts) -> dict:
    book = facts.book
    dashboard = reverse("books:detail", args=[book.pk])
    url = dashboard if book.awaits_ocr_start else guides_url(book)
    if not facts.total:
        if book.status == Book.Status.ERROR:
            return _step(
                "pages", dashboard, "attention", "تعذّر استخراج الصفحات", _headline(book.error_message)
            )
        if book.status == Book.Status.PROCESSING:
            return _step("pages", dashboard, "active", "تُستخرج الصفحات…", "تُستخرج صفحات الملف وتُجهَّز.")
        return _step("pages", dashboard, "todo", "لم تُستخرج الصفحات بعد", "تبدأ باستخراج صفحات الملف.")
    if facts.layout_errors:
        return _step(
            "pages",
            url,
            "attention",
            f"تعذّر تجهيز {_count(facts.layout_errors, PAGES_OF)}",
            "أعد تجهيز الصفحات التي تعذّر تجهيزها أو استثنِها.",
        )
    if facts.uploaded:
        prepared = facts.total - facts.uploaded
        return _step(
            "pages",
            url,
            "active",
            f"قيد التخطيط: {prepared} من {facts.total}",
            "تُجهَّز الصفحات وتُكشف مناطقها.",
            f"{prepared}/{facts.total}",
        )
    hint = "اكتمل التخطيط؛ اضغط «بدء المعالجة»." if book.awaits_ocr_start else "صفحات الكتاب ومناطقها."
    return _step("pages", url, "done", f"تم التخطيط · {_count(facts.total, PAGES_NOUN)}", hint)


def _ocr_step(facts: StageFacts) -> dict:
    url = reverse("books:detail", args=[facts.book.pk])
    if facts.book.awaits_ocr_start or not facts.total:
        return _step("ocr", url, "todo", "بانتظار «بدء المعالجة»", "تبدأ المعالجة بعد التخطيط.")
    if facts.ocr_errors:
        return _step(
            "ocr",
            url,
            "attention",
            f"تعذّرت معالجة {_count(facts.ocr_errors, PAGES_OF)}",
            "أعد تشغيل الصفحات التي تعذّرت معالجتها من «تحتاج انتباهًا».",
        )
    if facts.read == facts.total:
        return _step(
            "ocr",
            url,
            "done",
            f"عولجت {_count(facts.total, PAGES_NOUN)}",
            "تعرّف النموذجان على نص كل الصفحات.",
        )
    return _step(
        "ocr",
        url,
        "active",
        f"قيد المعالجة: {facts.read} من {facts.total}",
        "يُتعرّف على نص الصفحات.",
        f"{facts.read}/{facts.total}",
    )


def _review_step(facts: StageFacts) -> dict:
    book = facts.book
    blocked = "تبدأ المراجعة حين تنتهي معالجة أول صفحة"
    if not facts.read:
        return _step("review", None, "blocked", blocked, f"{blocked}.")
    if facts.pending:
        url = reverse("review:next", args=[book.pk])
        hint = f"الصفحة التالية للمراجعة: {facts.pending[0]}"
    else:
        url = reverse("review:page", args=[book.pk, facts.first_page])
        hint = "رُوجعت كل صفحات الكتاب." if facts.reviewed == facts.total else "لا صفحة بانتظار المراجعة الآن."
    count = f"{facts.reviewed}/{facts.total}"
    if facts.reviewed == facts.total:
        return _step("review", url, "done", f"رُوجعت {_count(facts.total, PAGES_NOUN)}", hint, count)
    state = "active" if facts.reviewed else "todo"
    detail = f"رُوجعت {facts.reviewed} من {_count(facts.total, PAGES_NOUN)}"
    return _step("review", url, state, detail, hint, count)


def _manuscript_step(facts: StageFacts) -> dict:
    state = facts.manuscript
    url = reverse("assembly:manuscript", args=[facts.book.pk])
    run = state.get("run") or {}
    if state.get("active"):
        return _step("manuscript", url, "active", "يجري التجميع…", "تُجمَع الصفحات في المخطوطة.")
    if run.get("status") == "error":
        return _step("manuscript", url, "attention", "تعذّر التجميع", run.get("error") or "")
    if not state.get("exists"):
        return _step(
            "manuscript",
            url,
            "todo",
            "لم تُجمَع المخطوطة بعد",
            "تُجمَع الصفحات في نص واحد متّصل: فصول وفقرات وحواشٍ.",
        )
    if state.get("edited"):
        return _step(
            "manuscript",
            url,
            "done",
            "النص يُحرَّر الآن في «الكتاب»",
            "حُرِّر النص في «الكتاب»؛ تصله تغييرات المراجعة من «تغييرات المراجعة».",
        )
    if state.get("stale"):
        hint = "أعد التجميع؛ لم يُحرَّر الكتاب بعد، فلا يضيع شيء."
        drift = state.get("drift_pages") or []
        if drift:
            return _step(
                "manuscript", url, "stale", f"تغيّر نص {_count(len(drift), PAGES_OF)} بعد التجميع", hint
            )
        approved = state.get("stale_pages") or []
        return _step(
            "manuscript", url, "stale", f"اعتُمدت {_count(len(approved), PAGES_NOUN)} بعد التجميع", hint
        )
    chapters = int((state.get("stats") or {}).get("chapters") or 0)
    detail = f"جُمعت: {_count(chapters, CHAPTERS_NOUN)}" if chapters else "جُمعت المخطوطة"
    return _step("manuscript", url, "done", detail, "المخطوطة محدَّثة.")


def _book_step(facts: StageFacts) -> dict:
    state = facts.manuscript
    url = reverse("editor:layout", args=[facts.book.pk])
    if not state.get("exists"):
        text = "يُفتح الكتاب بعد تجميع المخطوطة"
        return _step("book", None, "blocked", text, f"{text}.")
    drift = state.get("drift_pages") or []
    if state.get("edited") and drift:
        return _step(
            "book",
            f"{url}?tab=changes",
            "stale",
            f"تغيّر نص {_count(len(drift), PAGES_OF)} في المراجعة بعد تحرير الكتاب",
            "خذ التغييرات من «تغييرات المراجعة».",
        )
    if facts.page_count:
        return _step(
            "book",
            url,
            "done",
            f"{facts.trim_label} · {_count(facts.page_count, PAGES_NOUN)}",
            f"الكتاب بقطع {facts.trim_label}.",
        )
    return _step("book", url, "todo", "لم تُرتَّب الصفحات بعد", "افتح الكتاب لتنسيقه وترتيب صفحاته.")


def _formats(rows: list[dict]) -> str:
    names = []
    for key in ("docx", "print_pdf", "screen_pdf", "epub"):
        if any(row["format"] == key for row in rows) and EXPORT_NAMES[key] not in names:
            names.append(EXPORT_NAMES[key])
    return " و".join(names)


def _export_step(facts: StageFacts) -> dict:
    state = facts.manuscript
    url = reverse("publishing:export", args=[facts.book.pk])
    if not state.get("exists"):
        text = "يُتاح الإخراج بعد تجميع المخطوطة"
        return _step("export", None, "blocked", text, f"{text}.")
    rows = facts.exports
    active = [row for row in rows if row["status"] in ("queued", "running")]
    if active:
        return _step("export", url, "active", f"يُخرَج {_formats(active)}…", "يُخرَج الملف الآن.")
    if rows and rows[0]["status"] == "error":
        return _step("export", url, "attention", "تعذّر آخر إخراج", _headline(rows[0]["error"]))
    newest: dict[str, dict] = {}
    for row in rows:
        if row["status"] == "done":
            newest.setdefault(row["format"], row)
    current = [row for row in newest.values() if row["manuscript_version"] == state.get("version")]
    if current:
        when = max((row["finished_at"] for row in current if row["finished_at"]), default=None)
        detail = f"{_formats(current)} · {relative_time(when)}" if when else _formats(current)
        return _step("export", url, "done", detail, "ملفات الكتاب محدَّثة.")
    if newest:
        return _step("export", url, "stale", "تغيّر النص بعد آخر ملف", "أعد الإخراج ليحمل الملف النص الأخير.")
    return _step("export", url, "todo", "لم يُخرَج ملف بعد", "أخرج الكتاب Word أو PDF أو EPUB.")


def book_stages(book: Book, current: str | None = None, facts: StageFacts | None = None) -> list[dict]:
    """The stage bar of a book's screens (D76): six steps `{key, label, url, state, state_label, detail,
    hint, count, current}` in the order of `STAGE_KEYS` (editor/fixtures/contract/stages.json and its
    index.json give every state). `current` is the step of the screen shown (the dashboard passes `pages` in
    the «التخطيط» mode, else `ocr`). At most eight queries (`StageFacts`), whatever the book's size."""
    facts = facts if facts is not None else StageFacts(book)
    steps = [
        _pages_step(facts),
        _ocr_step(facts),
        _review_step(facts),
        _manuscript_step(facts),
        _book_step(facts),
        _export_step(facts),
    ]
    for step in steps:
        step["current"] = step["key"] == current
    return steps


def stages_payload(book: Book, current: str | None = None) -> dict:
    """`api:book_stages`: `{book, current, steps}`; an unknown `current` is null."""
    current = current if current in STAGE_KEYS else None
    return {"book": book.pk, "current": current, "steps": book_stages(book, current)}
