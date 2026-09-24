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
from collections.abc import Iterable, Sequence

from celery import chain
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile
from django.urls import NoReverseMatch, reverse

import numpy as np
import pymupdf

from books.models import Book, Page
from core.arabic import arabic_ratio
from core.storage import book_source_path, save_array
from core.templatetags.nassakh import FLAG_LABELS, status_dot

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
    "preprocess": "المعالجة الأولية",
    "layout": "التخطيط",
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

# Text-layer detection (spec §6): average characters per sampled page and Arabic share of letters.
TEXT_LAYER_MIN_CHARS = 200
TEXT_LAYER_MIN_ARABIC = 0.5
TEXT_LAYER_SAMPLE_PAGES = 40

# Rendering.
MIN_NATIVE_DPI = 72.0
MAX_NATIVE_DPI = 600.0  # bounds memory for absurdly large embedded images
MIN_IMAGE_COVERAGE = (
    0.5  # the largest embedded image must cover this share of the page to count as "the scan"
)
GUTTER_SEARCH_SPAN = 0.15  # search ± this share of the width around the book's split ratio
GUTTER_MIN_CONFIDENCE = 0.3  # below this the split ratio is used as is
SPLIT_MIN_RATIO, SPLIT_MAX_RATIO = 0.2, 0.8

# Weight of each page status towards the book's pipeline percentage (3 = through Phase 2).
_STATUS_WEIGHT: dict[str, int] = {
    Page.Status.UPLOADED: 0,
    Page.Status.PREPROCESSED: 1,
    Page.Status.LAYOUT_DONE: 2,
    Page.Status.OCR_DONE: 3,
    Page.Status.REVIEWED: 3,
    Page.Status.ASSEMBLED: 3,
    Page.Status.ERROR: 0,
    Page.Status.EXCLUDED: 0,
}


# ====================================================================== book creation


def create_book(data: dict, pdf: UploadedFile, user) -> Book:
    """Create a book from validated form data and its PDF, store the file and inspect it.

    Only keys in `BOOK_FIELDS` are taken from `data`. The PDF lands at `books/{id}/source.pdf`
    (`core.storage.book_source_path`). When the PDF cannot be read the book is kept with status
    `error` and an actionable Arabic message instead of raising.
    """
    fields = {key: value for key, value in data.items() if key in BOOK_FIELDS and value is not None}
    book = Book(**fields)
    if getattr(user, "is_authenticated", False):
        book.created_by = user
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
    Returns every page of the book in order. Raises ValueError (Arabic message) when the skip
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
            raise ValueError(
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
    page.save()
    return page


# ====================================================================== pipeline control


def start_processing(book: Book) -> None:
    """Mark the book `processing` and enqueue `ingest_book_task`, which fans out preprocessing.

    Allowed from `uploaded` and `error` (a restart resumes the idempotent ingest). Raises
    ValueError with an Arabic message otherwise.
    """
    if book.status in ACTIVE_BOOK_STATUSES:
        raise ValueError("المعالجة جارية بالفعل.")
    if book.status == Book.Status.NEEDS_GUIDES:
        raise ValueError("اضبط الأدلة أولًا ثم طبّقها على كل الصفحات لتستكمل المعالجة.")
    if book.status not in (Book.Status.UPLOADED, Book.Status.ERROR):
        raise ValueError("انتهت معالجة هذا الكتاب. استخدم «إعادة التشغيل» لإعادة مرحلة معيّنة.")
    if not book.source_pdf:
        raise ValueError("لا يوجد ملف PDF مرفق بهذا الكتاب.")

    book.status = Book.Status.PROCESSING
    book.error_message = ""
    book.save(update_fields=["status", "error_message", "updated_at"])

    from books.tasks import ingest_book_task

    ingest_book_task.delay(book.pk)


def _stage_signatures(page_id: int, stage: str) -> list:
    """Celery signatures for the pipeline from `stage` onwards; each task returns the page id."""
    from ocr.tasks import ocr_page_fast, ocr_page_full
    from processing.tasks import layout_page, preprocess_page  # other apps: lazy imports

    steps = {
        "preprocess": [preprocess_page, layout_page, ocr_page_fast, ocr_page_full],
        "layout": [layout_page, ocr_page_fast, ocr_page_full],
        "ocr": [ocr_page_fast, ocr_page_full],
        "ocr_fast": [ocr_page_fast],
        "ocr_full": [ocr_page_full],
    }[stage]
    return [steps[0].s(page_id)] + [task.s() for task in steps[1:]]


def run_stage(page: Page, stage: str):
    """Enqueue the pipeline for one page from `stage` (preprocess | layout | ocr | ocr_fast | ocr_full).

    A page in `error` is cleared first (it falls back to its last completed status). Excluded pages
    are refused. Stores the chain's task id on the page and returns the AsyncResult.
    """
    if stage not in STAGES:
        raise ValueError(f"مرحلة غير معروفة: {stage}")
    if page.is_excluded:
        raise ValueError("الصفحة مستثناة؛ أعد ضمّها إلى الكتاب أولًا.")
    if page.status == Page.Status.ERROR:
        page.clear_error()

    result = chain(*_stage_signatures(page.pk, stage)).apply_async()
    page.task_id = str(getattr(result, "id", "") or "")[:64]
    page.save(update_fields=["task_id"])
    return result


def rerun_book(book: Book, stage: str) -> int:
    """Re-run every non-excluded page of the book from `stage`; returns how many were enqueued.

    The book goes back to `processing` (stage preprocess) or `ocr` (later stages) so the dashboard
    polls until the pages come through again.
    """
    if stage not in STAGES:
        raise ValueError(f"مرحلة غير معروفة: {stage}")
    pages = list(book.pages.filter(is_excluded=False).order_by("number"))
    if not pages:
        return 0
    book.status = Book.Status.PROCESSING if stage == "preprocess" else Book.Status.OCR
    book.error_message = ""
    book.save(update_fields=["status", "error_message", "updated_at"])
    for page in pages:
        run_stage(page, stage)
    return len(pages)


def toggle_exclude(page: Page) -> Page:
    """Flip `is_excluded`; an excluded page shows `excluded`, a re-included one its completed stage."""
    page.is_excluded = not page.is_excluded
    if page.is_excluded:
        page.status = Page.Status.EXCLUDED
        page.error_from = ""
        page.error_message = ""
    else:
        page.status = page._completed_status()
    page.save(update_fields=["is_excluded", "status", "error_from", "error_message"])
    # A book that has not started yet stays `uploaded`; otherwise the derived status may change.
    if page.book.status not in (Book.Status.UPLOADED, Book.Status.ERROR):
        page.book.refresh_status()
    return page


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


def _pipeline_percent(by_status: dict[str, int], total: int) -> int:
    if total <= 0:
        return 0
    weight = sum(_STATUS_WEIGHT.get(status, 0) * count for status, count in by_status.items())
    return int(round(100 * weight / (3 * total)))


def book_progress(book: Book) -> dict:
    """Dashboard numbers: `total`, `by_status`, `percent`, `active`, `flags` (+ status and its label).

    `percent` weights each non-excluded page by how far it is through the Phase 2 pipeline
    (preprocessed 1/3, layout done 2/3, OCR done 3/3). `flags` counts pages that carry attention
    flags or are in error. `active` is true while the book is processing or in OCR.
    """
    by_status = book.progress()
    total = sum(by_status.values())
    rows = book.pages.filter(is_excluded=False).values_list("attention_flags", "status")
    flags = sum(1 for page_flags, status in rows if page_flags or status == Page.Status.ERROR)
    return {
        "total": total,
        "by_status": by_status,
        "percent": _pipeline_percent(by_status, total),
        "active": book.status in ACTIVE_BOOK_STATUSES,
        "flags": flags,
        "status": book.status,
        "status_label": book.get_status_display(),
        "dot": status_dot(book.status),
        "bar_state": _bar_state(book.status),
    }


def books_overview() -> list[dict]:
    """Rows for the books list: each book with its page count, progress and status colour."""
    rows = []
    for book in Book.objects.all():
        progress = book_progress(book)
        rows.append({"book": book, **progress})
    return rows


def _file_url(field) -> str | None:
    return field.url if field else None


def _headline(message: str | None) -> str:
    """First line of an error message (the Arabic headline); the rest is technical detail."""
    lines = (message or "").splitlines()
    return lines[0] if lines else ""


def _detail(message: str | None) -> str:
    """Everything after the first line of an error message."""
    return "\n".join((message or "").splitlines()[1:]).strip()


def _preprocess_of(page: Page):
    try:
        return page.preprocess
    except ObjectDoesNotExist:
        return None


def flag_labels(flags: Iterable[str]) -> list[dict]:
    """`[{"code", "label"}]` for a page's attention flags (unknown codes shown as they are)."""
    return [{"code": str(flag), "label": FLAG_LABELS.get(str(flag), str(flag))} for flag in flags or []]


def page_tile(page: Page) -> dict:
    """One thumbnail tile of the dashboard grid (also the per-page item of the progress API)."""
    preprocess = _preprocess_of(page)
    return {
        "id": page.pk,
        "number": page.number,
        "status": page.status,
        "status_label": page.get_status_display(),
        "dot": status_dot(page.status),
        "text_state": page.text_state,
        "flags": list(page.attention_flags or []),
        "flag_labels": [item["label"] for item in flag_labels(page.attention_flags)],
        "n_flags": len(page.attention_flags or []),
        "is_excluded": page.is_excluded,
        "error": page.status == Page.Status.ERROR,
        "error_headline": _headline(page.error_message) if page.status == Page.Status.ERROR else "",
        "thumb_url": _file_url(preprocess.thumbnail) if preprocess else None,
        "url": reverse("books:page_detail", args=[page.book_id, page.number]),
    }


def page_tiles(book: Book) -> list[dict]:
    """Tiles for every page of the book in order (excluded pages included, marked)."""
    pages = book.pages.select_related("preprocess").order_by("number")
    return [page_tile(page) for page in pages]


def attention_pages(book: Book) -> list[dict]:
    """Non-excluded pages that carry attention flags or are in error, for the dashboard list."""
    items = []
    for page in book.pages.filter(is_excluded=False).order_by("number"):
        if not page.attention_flags and page.status != Page.Status.ERROR:
            continue
        items.append(
            {
                "page": page,
                "flags": flag_labels(page.attention_flags),
                "error": page.status == Page.Status.ERROR,
                "error_headline": _headline(page.error_message),
                "url": reverse("books:page_detail", args=[book.pk, page.number]),
            }
        )
    return items


def guides_url(book: Book) -> str | None:
    """URL of the guides screen (processing app) or None while that route does not exist yet."""
    try:
        return reverse("processing:guides", args=[book.pk])
    except NoReverseMatch:
        return None


def has_guides(book: Book) -> bool:
    """True when the book already has `LayoutGuides` (auto-proposed or manual)."""
    try:
        return book.guides is not None
    except ObjectDoesNotExist:
        return False


def book_dashboard(book: Book) -> dict:
    """Everything the dashboard template needs, including the Alpine component's initial state."""
    progress = book_progress(book)
    by_status = progress["by_status"]
    stages = [
        {"key": "uploaded", "statuses": [Page.Status.UPLOADED], "label": "مرفوعة", "state": "neutral"},
        {"key": "preprocessed", "statuses": [Page.Status.PREPROCESSED], "label": "مُعالَجة", "state": ""},
        {"key": "layout_done", "statuses": [Page.Status.LAYOUT_DONE], "label": "تم التخطيط", "state": ""},
        {
            "key": "ocr_done",
            "statuses": [Page.Status.OCR_DONE, Page.Status.REVIEWED, Page.Status.ASSEMBLED],
            "label": "تم التعرّف",
            "state": "success",
        },
        {"key": "error", "statuses": [Page.Status.ERROR], "label": "خطأ", "state": "danger"},
    ]
    for stage in stages:
        stage["count"] = sum(by_status.get(status, 0) for status in stage["statuses"])
        stage["percent"] = int(round(100 * stage["count"] / progress["total"])) if progress["total"] else 0
        stage["dot"] = status_dot(stage["statuses"][0])
    tiles = page_tiles(book)
    config = {
        "progressUrl": reverse("api:book_progress", args=[book.pk]),
        "active": progress["active"],
        "total": progress["total"],
        "percent": progress["percent"],
        "flags": progress["flags"],
        "status": progress["status"],
        "statusLabel": progress["status_label"],
        "dot": progress["dot"],
        "byStatus": by_status,
        "stages": [{"key": stage["key"], "statuses": stage["statuses"]} for stage in stages],
        "pages": tiles,
    }
    return {
        "book": book,
        "progress": progress,
        "stages": stages,
        "tiles": tiles,
        "attention": attention_pages(book),
        "has_guides": has_guides(book),
        "guides_url": guides_url(book),
        "can_start": book.status in (Book.Status.UPLOADED, Book.Status.ERROR),
        "rerun_stages": [{"value": value, "label": STAGE_LABELS[value]} for value in STAGES],
        "error_headline": _headline(book.error_message),
        "error_detail": _detail(book.error_message),
        "config": config,
    }


def page_neighbours(page: Page) -> tuple[Page | None, Page | None]:
    """`(previous, next)` pages by book order (None at the ends)."""
    siblings = Page.objects.filter(book_id=page.book_id)
    previous = siblings.filter(number__lt=page.number).order_by("-number").first()
    following = siblings.filter(number__gt=page.number).order_by("number").first()
    return previous, following


def page_images(page: Page) -> dict:
    """URLs of the three image tabs (`original`, `gray` = display image, `bw`), None when missing."""
    preprocess = _preprocess_of(page)
    gray = None
    bw = None
    if preprocess is not None:
        gray = _file_url(preprocess.display_image) or _file_url(preprocess.gray_image)
        bw = _file_url(preprocess.bw_image)
    return {"original": _file_url(page.original_image), "gray": gray, "bw": bw}


def page_regions(page: Page) -> list[dict]:
    """Regions of the page as plain dicts (bbox in gray-image pixel space) for the overlay."""
    return [
        {
            "id": region.pk,
            "kind": region.kind,
            "label": region.get_kind_display(),
            "bbox": region.bbox,
            "order": region.order,
            "source": region.source,
        }
        for region in page.regions.all()
    ]


def page_status(page: Page) -> dict:
    """Payload of `/api/pages/<id>/status/`: status, text state, texts, flags and error."""
    active = page.status in ACTIVE_PAGE_STATUSES and page.book.status in ACTIVE_BOOK_STATUSES
    return {
        "id": page.pk,
        "number": page.number,
        "status": page.status,
        "status_label": page.get_status_display(),
        "dot": status_dot(page.status),
        "text_state": page.text_state,
        "text_state_label": page.get_text_state_display(),
        "provisional_text": page.provisional_text,
        "final_text": page.final_text,
        "flags": list(page.attention_flags or []),
        "flag_labels": [item["label"] for item in flag_labels(page.attention_flags)],
        "error": page.error_message if page.status == Page.Status.ERROR else "",
        "error_from": page.error_from if page.status == Page.Status.ERROR else "",
        "is_excluded": page.is_excluded,
        "active": active,
    }


def page_detail_context(page: Page) -> dict:
    """Everything the page-detail template needs, including the Alpine component's config."""
    previous, following = page_neighbours(page)
    preprocess = _preprocess_of(page)
    images = page_images(page)
    size = (
        [preprocess.output_width, preprocess.output_height]
        if preprocess is not None and preprocess.output_width and preprocess.output_height
        else None
    )
    book = page.book
    prev_url = reverse("books:page_detail", args=[book.pk, previous.number]) if previous else None
    next_url = reverse("books:page_detail", args=[book.pk, following.number]) if following else None
    return {
        "book": book,
        "page": page,
        "prev_page": previous,
        "next_page": following,
        "prev_url": prev_url,
        "next_url": next_url,
        "page_images": images,
        "page_regions": page_regions(page),
        "page_flags": flag_labels(page.attention_flags),
        "page_state": page_status(page),
        "error_headline": _headline(page.error_message) if page.status == Page.Status.ERROR else "",
        "error_detail": _detail(page.error_message) if page.status == Page.Status.ERROR else "",
        "retry_stage": page.error_from
        if page.status == Page.Status.ERROR and page.error_from in STAGES
        else None,
        "rerun_stages": [{"value": value, "label": STAGE_LABELS[value]} for value in STAGES],
        "total_pages": Page.objects.filter(book_id=page.book_id).count(),
        "dpi": int(round(page.dpi)) if page.dpi else 0,
        "source_page": page.source_index + 1,
        "viewer": {
            "pageId": page.pk,
            "images": images,
            "size": size,
            "regions": page_regions(page),
            "prevUrl": prev_url,
            "nextUrl": next_url,
            "initialTab": "gray" if images["gray"] else "original",
        },
    }
