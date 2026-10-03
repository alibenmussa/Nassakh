"""Books tests: PDF inspection and ingest on generated PDFs, pipeline control, views and API.

PDFs are built with PyMuPDF: born-digital pages with Arabic text (a system Arabic font when one is
available, else PyMuPDF's HTML box with its built-in fallback fonts) and "scanned" pages that are a
single full-page image, including landscape sheets holding two book pages. Tasks and services of the
other apps are mostly mocked (without `create=True`, so a rename there fails here); one test runs the
real chain end to end.
"""

from __future__ import annotations

import io
import json
import os
import re
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import Group, User
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

import numpy as np
import pymupdf
import pytest
from PIL import Image

from books import services, tasks
from books.forms import BookForm
from books.models import ALL_PAGES_FAILED, ALL_PAGES_FAILED_LAYOUT, Book, Page
from processing.models import LayoutGuides, Preprocess, Region

pytestmark = pytest.mark.django_db

ARABIC_LINE = "قال الأمير في سنة 1966 إن الكتاب العربي المصوّر يحتاج إلى إحياء وعناية."
FONT_CANDIDATES = (
    os.path.expanduser("~/Library/Fonts/Amiri Regular.ttf"),
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
)

# Portrait scan: 1240×1754 px on an A4 page → 150 DPI. Landscape sheet: 1700×1200 px on 842×595 pt → 145 DPI.
SCAN_W, SCAN_H = 1240, 1754
SHEET_W, SHEET_H = 1700, 1200
# Two text blocks on the sheet with the gutter centred at x=950 (ratio 0.559, off the 0.5 default).
LEFT_BLOCK = (150, 850)
RIGHT_BLOCK = (1050, 1550)
MARKER = (1080, 1180, 60, 160)  # x0, x1, y0, y1 of a black square that only the right page carries


# ====================================================================== PDF builders


def _arabic_font() -> str | None:
    return next((path for path in FONT_CANDIDATES if os.path.exists(path)), None)


def make_text_pdf(n_pages: int = 3, lines: int = 8) -> bytes:
    """Born-digital PDF: `n_pages` A4 pages, each with `lines` Arabic lines in the text layer."""
    font = _arabic_font()
    doc = pymupdf.open()
    for _ in range(n_pages):
        page = doc.new_page(width=595, height=842)
        if font:
            for i in range(lines):
                page.insert_text((50, 90 + 26 * i), ARABIC_LINE, fontname="ar", fontfile=font, fontsize=13)
        else:
            html = '<div dir="rtl" style="font-size:13px">' + "<br>".join([ARABIC_LINE] * lines) + "</div>"
            page.insert_htmlbox(pymupdf.Rect(40, 60, 555, 800), html)
    data = doc.tobytes()
    doc.close()
    return data


def _png(array: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(array).save(buf, format="PNG")
    return buf.getvalue()


def _text_block(array: np.ndarray, x0: int, x1: int, y0: int = 120, y1: int | None = None) -> None:
    """Draw black 'text lines' (12 px bars every 40 px) between x0 and x1."""
    y1 = y1 or array.shape[0] - 120
    for y in range(y0, y1, 40):
        array[y : y + 12, x0:x1] = 0


def scan_page_image() -> np.ndarray:
    array = np.full((SCAN_H, SCAN_W), 255, np.uint8)
    _text_block(array, 150, SCAN_W - 150)
    return array


def sheet_image() -> np.ndarray:
    array = np.full((SHEET_H, SHEET_W), 255, np.uint8)
    _text_block(array, *LEFT_BLOCK)
    _text_block(array, *RIGHT_BLOCK)
    x0, x1, y0, y1 = MARKER
    array[y0:y1, x0:x1] = 0
    return array


def make_scan_pdf(n_pages: int = 3, landscape: bool = False) -> bytes:
    """Scanned-looking PDF: every page is one full-page embedded image (no text layer)."""
    doc = pymupdf.open()
    image = _png(sheet_image() if landscape else scan_page_image())
    for _ in range(n_pages):
        if landscape:
            page = doc.new_page(width=842, height=595)
        else:
            page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=image)
    data = doc.tobytes()
    doc.close()
    return data


# ====================================================================== fixtures


@pytest.fixture
def editor() -> User:
    user = User.objects.create_user("editor", password="pass-1234")
    user.groups.add(Group.objects.get_or_create(name="editor")[0])
    return user


@pytest.fixture
def proofreader() -> User:
    user = User.objects.create_user("reader", password="pass-1234")
    user.groups.add(Group.objects.get_or_create(name="proofreader")[0])
    return user


@pytest.fixture
def editor_client(client, editor):
    client.force_login(editor)
    return client


def make_book(pdf_bytes: bytes, user=None, **options) -> Book:
    data = {"title": "كتاب الاختبار", "author": "مؤلف", "original_year": 1966, **options}
    upload = SimpleUploadedFile("scan.pdf", pdf_bytes, content_type="application/pdf")
    return services.create_book(data, upload, user)


def _load_original(page: Page) -> np.ndarray:
    with page.original_image.open("rb") as handle:
        return np.asarray(Image.open(handle).convert("L"))


def _json_config(body: str, element_id: str) -> dict:
    """Decode a `{{ value|json_script:"id" }}` block (non-ASCII arrives as \\uXXXX escapes)."""
    match = re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>', body, re.S)
    assert match, f"no json_script block {element_id!r}"
    return json.loads(match.group(1))


# ====================================================================== create + inspect


def test_create_book_stores_pdf_at_the_contract_path_and_inspects_it(editor):
    book = make_book(make_text_pdf(3), user=editor)
    assert book.pk and book.created_by == editor
    assert book.source_pdf.name == f"books/{book.pk}/source.pdf"
    assert default_storage.exists(book.source_pdf.name)
    assert book.source_page_count == 3
    assert book.has_text_layer is True
    assert book.status == Book.Status.UPLOADED


def test_create_book_with_unreadable_pdf_keeps_the_book_in_error():
    book = make_book(b"%PDF-1.4 definitely not a real pdf")
    assert book.status == Book.Status.ERROR
    assert book.error_message.startswith("تعذّر قراءة ملف PDF")
    assert "\n" in book.error_message  # technical detail on the second line, never in the headline


def test_inspect_pdf_detects_arabic_text_layer():
    book = make_book(make_text_pdf(3))
    info = services.inspect_pdf(book)
    assert info["page_count"] == 3 and info["has_text_layer"] is True
    assert info["avg_chars"] >= services.TEXT_LAYER_MIN_CHARS
    assert info["arabic_ratio"] >= services.TEXT_LAYER_MIN_ARABIC


def test_inspect_pdf_scanned_pdf_has_no_text_layer():
    book = make_book(make_scan_pdf(3))
    info = services.inspect_pdf(book)
    assert (info["page_count"], info["has_text_layer"]) == (3, False)
    book.refresh_from_db()
    assert book.has_text_layer is False and book.source_page_count == 3


# ====================================================================== ingest


def test_ingest_book_renders_scans_at_native_dpi_in_order():
    book = make_book(make_scan_pdf(3))
    pages = services.ingest_book(book)

    assert [p.number for p in pages] == [1, 2, 3]
    assert [p.source_index for p in pages] == [0, 1, 2]
    assert all(p.source_half == Page.SourceHalf.FULL for p in pages)
    assert all(p.status == Page.Status.UPLOADED for p in pages)
    for page in pages:
        assert page.original_image.name == f"books/{book.pk}/pages/{page.number:04d}/original.png"
        assert default_storage.exists(page.original_image.name)
        assert abs(page.width - SCAN_W) <= 2 and abs(page.height - SCAN_H) <= 3
        assert abs(page.dpi - 150) < 1
        assert page.text_layer_text == ""
    array = _load_original(pages[0])
    assert array.shape == (pages[0].height, pages[0].width)
    assert (array < 128).mean() > 0.05  # the drawn lines survived the round trip


def test_ingest_writes_a_scan_thumbnail_the_tile_shows_before_preprocessing():
    # F34: the dashboard tile has a scan preview under the cleaned thumbnail
    book = make_book(make_scan_pdf(1))
    (page,) = services.ingest_book(book)
    assert page.scan_thumbnail.name == f"books/{book.pk}/pages/0001/scan_thumb.webp"
    with page.scan_thumbnail.open("rb") as handle:
        assert Image.open(handle).width <= services.SCAN_THUMB_MAX_WIDTH
    tile = services.page_tile(page)
    assert tile["scan_thumb_url"] == page.scan_thumbnail.url and tile["thumb_url"] is None


def test_ingest_book_renders_born_digital_pages_at_render_dpi_with_text_layer():
    book = make_book(make_text_pdf(2))
    pages = services.ingest_book(book)
    assert len(pages) == 2
    for page in pages:
        assert abs(page.dpi - 300) < 0.01
        assert abs(page.width - round(595 / 72 * 300)) <= 2
        assert page.text_layer_text.strip()
        assert "1966" in page.text_layer_text


def test_ingest_book_skips_first_and_last_pages():
    book = make_book(make_scan_pdf(4), skip_first=1, skip_last=2)
    pages = services.ingest_book(book)
    assert [(p.number, p.source_index) for p in pages] == [(1, 1)]


def test_ingest_book_raises_when_nothing_is_left():
    book = make_book(make_scan_pdf(2), skip_first=1, skip_last=1)
    with pytest.raises(ValueError, match="لا تبقى صفحات"):
        services.ingest_book(book)
    assert book.pages.count() == 0


def test_ingest_book_splits_sheets_at_the_detected_gutter_right_then_left():
    book = make_book(make_scan_pdf(2, landscape=True), pages_per_sheet=2, split_ratio=0.5)
    pages = services.ingest_book(book)

    assert [(p.number, p.source_index, p.source_half) for p in pages] == [
        (1, 0, "right"),
        (2, 0, "left"),
        (3, 1, "right"),
        (4, 1, "left"),
    ]
    right, left = pages[0], pages[1]
    # The gutter is at x≈950 of 1700 (ratio 0.559), found although the book says 0.5.
    assert abs(right.width - (SHEET_W - 950)) <= 12
    assert abs(left.width - 950) <= 12
    assert right.height == left.height and abs(right.height - SHEET_H) <= 3
    assert abs(right.dpi - 145.4) < 1
    # The marker square lives on the right page only.
    right_img, left_img = _load_original(right), _load_original(left)
    x0, x1, y0, y1 = MARKER
    offset = SHEET_W - right.width
    assert (right_img[y0 + 5 : y1 - 5, x0 - offset + 5 : x1 - offset - 5] < 128).mean() > 0.95
    # Above the first text line (y=120) the left page is blank while the right page carries the marker.
    assert (left_img[y0:115, :] < 128).mean() < 0.01
    assert (right_img[y0:115, :] < 128).mean() > 0.05


def test_half_text_clips_in_the_displayed_space_of_rotated_sheets():
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 60), "TOPTEXT", fontname="helv", fontsize=14)  # top of the unrotated page
    page.set_rotation(90)  # displayed as a landscape sheet: the text lands on the right half
    data = doc.tobytes()
    doc.close()
    sheet = pymupdf.open(stream=data, filetype="pdf")[0]
    assert sheet.rect.width > sheet.rect.height
    assert "TOPTEXT" in services._half_text(sheet, Page.SourceHalf.RIGHT, 0.5)
    assert "TOPTEXT" not in services._half_text(sheet, Page.SourceHalf.LEFT, 0.5)
    assert "TOPTEXT" in services._half_text(sheet, Page.SourceHalf.FULL, None)


def test_toggle_exclude_keeps_an_unstarted_book_uploaded():
    book, pages = _book_with_pages(2)
    services.toggle_exclude(pages[0])
    book.refresh_from_db()
    assert book.status == Book.Status.UPLOADED


def test_find_gutter_and_split_position_fall_back_to_the_ratio():
    gray = np.full((400, 1000), 255, np.uint8)
    gray[:, :] = 0  # solid ink: no gutter anywhere
    x, confidence = services.find_gutter(gray)
    assert (x, confidence) == (500, 0.0)
    x, confidence = services.split_position(gray, 0.6)
    assert (x, confidence) == (600, 0.0)
    gray[:, 380:420] = 255  # a 40 px white band → detected
    x, confidence = services.find_gutter(gray)
    assert abs(x - 400) <= 3 and confidence > 0


def test_ingest_book_is_idempotent_and_never_rerenders_existing_pages():
    book = make_book(make_scan_pdf(3))
    first = services.ingest_book(book)
    stamps = {p.number: default_storage.get_modified_time(p.original_image.name) for p in first}

    with patch("books.services.render_gray", wraps=services.render_gray) as render:
        second = services.ingest_book(book)
    assert render.call_count == 0
    assert [p.pk for p in second] == [p.pk for p in first]
    assert Page.objects.filter(book=book).count() == 3
    assert {p.number: default_storage.get_modified_time(p.original_image.name) for p in second} == stamps

    # Resume: a missing page is filled in, the others are untouched.
    Page.objects.get(book=book, number=2).delete()
    third = services.ingest_book(book)
    assert [p.number for p in third] == [1, 2, 3]
    assert third[0].pk == first[0].pk and third[2].pk == first[2].pk


# ====================================================================== pipeline control


def _book_with_pages(n: int = 3, **page_kwargs) -> tuple[Book, list[Page]]:
    book = Book.objects.create(title="كتاب", status=Book.Status.UPLOADED)
    pages = [
        Page.objects.create(book=book, number=i, source_index=i - 1, **page_kwargs) for i in range(1, n + 1)
    ]
    return book, pages


def _end_runs(book: Book) -> None:
    """What the last task of each chain does when the chains are mocked away: give the run's claim back."""
    Page.objects.filter(book=book).update(run_token="", run_claimed_at=None)


def test_start_processing_sets_status_and_enqueues_ingest():
    book, _ = _book_with_pages(0)
    book.source_pdf.save("source.pdf", io.BytesIO(make_scan_pdf(1)), save=True)
    with patch("books.tasks.ingest_book_task.delay") as delay:
        services.start_processing(book)
    delay.assert_called_once_with(book.pk)
    book.refresh_from_db()
    assert book.status == Book.Status.PROCESSING


@pytest.mark.parametrize(
    "status", [Book.Status.PROCESSING, Book.Status.NEEDS_GUIDES, Book.Status.READY_FOR_REVIEW]
)
def test_start_processing_refuses_when_not_startable(status):
    book = Book.objects.create(title="كتاب", status=status)
    with patch("books.tasks.ingest_book_task.delay") as delay, pytest.raises(ValueError):
        services.start_processing(book)
    delay.assert_not_called()


def _mock_pipeline_tasks():
    """Patch the four pipeline tasks of the other apps and `chain` (a renamed task fails loudly)."""
    return (
        patch("processing.tasks.preprocess_page"),
        patch("processing.tasks.layout_page"),
        patch("ocr.tasks.ocr_page_fast"),
        patch("ocr.tasks.ocr_page_full"),
        patch("books.services.chain"),
    )


@pytest.mark.parametrize(
    "stage, expected",
    [
        ("preprocess", ["preprocess_page", "layout_page", "ocr_page_fast", "ocr_page_full"]),
        ("layout", ["layout_page", "ocr_page_fast", "ocr_page_full"]),
        ("ocr", ["ocr_page_fast", "ocr_page_full"]),
        ("ocr_fast", ["ocr_page_fast"]),
        ("ocr_full", ["ocr_page_full"]),
    ],
)
def test_run_stage_enqueues_the_chain_from_that_stage(stage, expected):
    _, pages = _book_with_pages(1)
    page = pages[0]
    p_pre, p_lay, p_fast, p_full, p_chain = _mock_pipeline_tasks()
    with p_pre as pre, p_lay as lay, p_fast as fast, p_full as full, p_chain as chain:
        mocks = {"preprocess_page": pre, "layout_page": lay, "ocr_page_fast": fast, "ocr_page_full": full}
        for name, mock in mocks.items():
            mock.s.return_value = f"sig:{name}"
        chain.return_value.apply_async.return_value.id = "task-123"

        services.run_stage(page, stage)

        page.refresh_from_db()
        token = page.run_token  # the run's claim (item 29): every task carries it, the last one gives it back
        assert len(token) == 32 and page.run_claimed_at is not None
        chain.assert_called_once_with(*[f"sig:{name}" for name in expected])
        chain.return_value.apply_async.assert_called_once()
        errback = chain.return_value.apply_async.call_args.kwargs["link_error"]
        assert errback.task == "books.tasks.page_run_failed"
        assert errback.kwargs == {"page_id": page.pk, "run": token}
        last = {"run": token, "last": True}
        first = last if len(expected) == 1 else {"run": token}
        mocks[expected[0]].s.assert_called_once_with(page.pk, **first)  # first task gets the page id
        for name in expected[1:]:
            want = last if name == expected[-1] else {"run": token}
            mocks[name].s.assert_called_once_with(**want)  # the rest receive it from the chain
    assert page.task_id == "task-123"


def test_run_stage_clears_errors_and_refuses_excluded_pages():
    _, pages = _book_with_pages(2)
    errored, excluded = pages
    errored.set_error("preprocess", "فشل")
    excluded.is_excluded = True
    excluded.save()
    patches = _mock_pipeline_tasks()
    with patches[0], patches[1], patches[2], patches[3], patches[4] as chain:
        chain.return_value.apply_async.return_value.id = "t"
        services.run_stage(errored, "preprocess")
        with pytest.raises(ValueError):
            services.run_stage(excluded, "preprocess")
        with pytest.raises(ValueError):
            services.run_stage(errored, "bogus")
    errored.refresh_from_db()
    assert errored.status == Page.Status.UPLOADED and errored.error_from == ""


def test_rerun_book_requeues_every_included_page_and_sets_the_book_status():
    book, pages = _book_with_pages(3, status=Page.Status.OCR_DONE)
    pages[2].is_excluded = True
    pages[2].save()
    with patch("books.services.run_stage") as run_stage:
        assert tasks.rerun_book_from(book.pk, "ocr") == book.pk
    assert [call.args for call in run_stage.call_args_list] == [(pages[0], "ocr"), (pages[1], "ocr")]
    token = Page.objects.get(pk=pages[0].pk).run_token  # claimed by the re-run itself (no token given)
    assert token and {call.kwargs["run"] for call in run_stage.call_args_list} == {token}
    book.refresh_from_db()
    assert book.status == Book.Status.OCR
    _end_runs(book)
    with patch("books.services.run_stage"):
        services.rerun_book(book, "preprocess")
    book.refresh_from_db()
    assert book.status == Book.Status.PROCESSING


def test_book_progress_counts_percent_active_and_flags():
    book, pages = _book_with_pages(5)
    Page.objects.filter(pk=pages[0].pk).update(status=Page.Status.OCR_DONE)
    Page.objects.filter(pk=pages[1].pk).update(status=Page.Status.LAYOUT_DONE, attention_flags=["large_skew"])
    Page.objects.filter(pk=pages[2].pk).update(status=Page.Status.ERROR)
    Page.objects.filter(pk=pages[3].pk).update(status=Page.Status.EXCLUDED, is_excluded=True)
    book.status = Book.Status.OCR
    book.save()

    progress = services.book_progress(book)
    assert progress["total"] == 4
    assert progress["by_status"]["ocr_done"] == 1
    assert progress["by_status"]["layout_done"] == 1
    assert progress["by_status"]["error"] == 1
    assert progress["by_status"]["uploaded"] == 1
    assert progress["by_status"]["excluded"] == 0
    assert progress["percent"] == 25  # owner 13: one page read of four, the step's «1/4»
    assert progress["active"] is True
    assert progress["flags"] == 2  # one flagged page, one errored page
    assert progress["status_label"] == "قيد المعالجة"

    book.status = Book.Status.READY_FOR_REVIEW
    book.save()
    assert services.book_progress(book)["active"] is False
    assert services.book_progress(Book.objects.create(title="فارغ"))["percent"] == 0


def test_processing_percent_follows_the_read_pages_not_the_preparation():
    """Owner 13: a book that leaves «التخطيط» brings its pages prepared (D64); «المعالجة»'s bar starts at 0 and fills
    with the pages read, the same ratio as the step's count «n/total», never a weight for preparing or layout."""
    book, pages = _book_with_pages(4)
    Page.objects.filter(book=book).update(status=Page.Status.PREPROCESSED)
    book.status = Book.Status.OCR
    book.save()
    assert services.book_progress(book)["percent"] == 0  # every page prepared, none read: nothing done in this step
    Page.objects.filter(pk=pages[0].pk).update(status=Page.Status.LAYOUT_DONE, text_state=Page.TextState.PROVISIONAL)
    assert services.book_progress(book)["percent"] == 0  # Tesseract's provisional text is not the models' reading
    Page.objects.filter(pk__in=[pages[1].pk, pages[2].pk]).update(status=Page.Status.OCR_DONE)
    Page.objects.filter(pk=pages[3].pk).update(status=Page.Status.REVIEWED)
    progress = services.book_progress(book)
    assert progress["percent"] == 75 and progress["percent"] == round(100 * 3 / progress["total"])
    stages = {s["key"]: s for s in services.book_stages(book, "ocr")}
    assert stages["ocr"]["count"] == "3/4"  # the step's count and its bar say the same thing
    # in «التخطيط» the bar is still the prepared share
    Book.objects.filter(pk=book.pk).update(awaits_ocr_start=True, status=Book.Status.PROCESSING)
    Page.objects.filter(book=book).update(status=Page.Status.UPLOADED)
    Page.objects.filter(pk=pages[0].pk).update(status=Page.Status.PREPROCESSED)
    book.refresh_from_db()
    assert services.book_progress(book)["percent"] == 25


def test_toggle_exclude_flips_status_and_restores_completed_stage():
    book, pages = _book_with_pages(2)
    page = pages[0]
    Preprocess.objects.create(page=page)
    page.status = Page.Status.PREPROCESSED
    page.save()

    services.toggle_exclude(page)
    page.refresh_from_db()
    assert page.is_excluded is True and page.status == Page.Status.EXCLUDED
    assert book.page_count == 1

    services.toggle_exclude(page)
    page.refresh_from_db()
    assert page.is_excluded is False and page.status == Page.Status.PREPROCESSED


def _approve(page: Page) -> Page:
    Page.objects.filter(pk=page.pk).update(
        status=Page.Status.REVIEWED, text_state=Page.TextState.FINAL, reviewed_at=timezone.now()
    )
    page.refresh_from_db()
    return page


def test_toggle_exclude_brings_an_approved_page_back_as_reviewed():
    # backend-6: excluding and re-including an approved page keeps its approval
    book, pages = _book_with_pages(2, status=Page.Status.OCR_DONE, text_state=Page.TextState.FINAL)
    book.status = Book.Status.REVIEWING
    book.save()
    approved, pending = _approve(pages[0]), pages[1]
    for page in (approved, pending):
        services.toggle_exclude(page)
        services.toggle_exclude(page)
    approved.refresh_from_db()
    pending.refresh_from_db()
    assert approved.status == Page.Status.REVIEWED and approved.reviewed_at is not None
    assert pending.status == Page.Status.OCR_DONE
    # a stale failure on an approved page clears back to `reviewed` as well
    approved.set_error("ocr_fast", "فشل")
    approved.clear_error()
    assert approved.status == Page.Status.REVIEWED


def test_run_stage_refuses_an_approved_page_until_it_is_reopened():
    # backend-1: a new pass would renumber, misplace or drop the reviewed lines
    _, pages = _book_with_pages(2)
    approved = _approve(pages[0])
    failed = _approve(pages[1])
    failed.set_error("ocr_fast", "فشل")  # a stale task failed after the approval
    patches = _mock_pipeline_tasks()
    with patches[0], patches[1], patches[2], patches[3], patches[4] as chain:
        for stage in services.STAGES:
            with pytest.raises(ValueError, match="أعد فتحها"):
                services.run_stage(approved, stage)
        with pytest.raises(ValueError, match="أعد فتحها"):
            services.run_stage(failed, "ocr")
    chain.assert_not_called()
    for page in (approved, failed):
        page.refresh_from_db()
        assert page.status == Page.Status.REVIEWED and page.text_state == Page.TextState.FINAL


def test_rerun_book_skips_approved_pages_and_refuses_a_fully_approved_book():
    book, pages = _book_with_pages(3, status=Page.Status.OCR_DONE, text_state=Page.TextState.FINAL)
    approved = _approve(pages[1])
    with patch("books.services.run_stage") as run_stage:
        assert services.rerun_book(book, "ocr") == 2
    assert [call.args for call in run_stage.call_args_list] == [(pages[0], "ocr"), (pages[2], "ocr")]
    approved.refresh_from_db()
    assert approved.status == Page.Status.REVIEWED and approved.text_state == Page.TextState.FINAL
    assert services.approved_page_count(book) == 1

    for page in (pages[0], pages[2]):
        _approve(page)
    with pytest.raises(ValueError, match="كل صفحات الكتاب معتمدة"):
        services.validate_rerun(book, "layout")
    with patch("books.services.run_stage") as run_stage, pytest.raises(ValueError):
        services.rerun_book(book, "layout")
    run_stage.assert_not_called()


# ====================================================================== tasks


def test_ingest_book_task_ingests_and_fans_out_preprocessing():
    book = make_book(make_scan_pdf(3))
    book.status = Book.Status.PROCESSING
    book.save()
    with (
        patch("processing.tasks.preprocess_page") as preprocess_page,
        patch("books.tasks.group") as group,
        patch("books.tasks.chord") as chord,
    ):
        preprocess_page.s.side_effect = lambda pid: f"pre:{pid}"
        assert tasks.ingest_book_task(book.pk) == book.pk

    page_ids = list(book.pages.order_by("number").values_list("pk", flat=True))
    assert len(page_ids) == 3
    group.assert_called_once()
    assert list(group.call_args.args[0]) == [f"pre:{pid}" for pid in page_ids]
    chord.assert_called_once_with(group.return_value)
    # D64: the book awaits «بدء المعالجة», so the callback is fixed at ingest to pause
    chord.return_value.assert_called_once_with(tasks.after_preprocess.s(book.pk, continue_ocr=False))
    book.refresh_from_db()
    assert book.status == Book.Status.PROCESSING


def test_ingest_book_task_reports_failures_on_the_book_instead_of_raising():
    book = Book.objects.create(title="معطوب", status=Book.Status.PROCESSING)
    book.source_pdf.save("source.pdf", io.BytesIO(b"not a pdf at all"), save=True)
    with patch("books.tasks.chord") as chord:
        assert tasks.ingest_book_task(book.pk) == book.pk
    chord.assert_not_called()
    book.refresh_from_db()
    assert book.status == Book.Status.ERROR
    assert book.error_message.splitlines()[0] == tasks.INGEST_ERROR


@pytest.mark.parametrize("confidence", [0.0, 0.2])
def test_after_preprocess_never_waits_for_guides_even_with_low_confidence(confidence):
    # footnotes and page numbers are detected per page: no `needs_guides` stop
    book, pages = _book_with_pages(3, status=Page.Status.PREPROCESSED)
    book.status = Book.Status.PROCESSING
    book.save()
    propose = MagicMock(side_effect=lambda b: (LayoutGuides.objects.create(book=b), confidence))
    with (
        patch("processing.services.propose_guides", propose),
        patch("books.services.run_stage") as run_stage,
    ):
        assert tasks.after_preprocess([p.pk for p in pages], book.pk) == book.pk
    propose.assert_called_once_with(book)
    assert [call.args for call in run_stage.call_args_list] == [(p, "layout") for p in pages]
    book.refresh_from_db()
    assert book.status == Book.Status.OCR


def test_after_preprocess_with_confident_guides_enqueues_layout_for_preprocessed_pages():
    book, pages = _book_with_pages(4, status=Page.Status.PREPROCESSED)
    pages[2].set_error("preprocess", "فشل")  # skipped, stays visible in the attention list
    pages[3].is_excluded = True
    pages[3].status = Page.Status.EXCLUDED
    pages[3].save()
    book.status = Book.Status.PROCESSING
    book.save()
    propose = MagicMock(side_effect=lambda b: (LayoutGuides.objects.create(book=b), 0.9))
    with (
        patch("processing.services.propose_guides", propose),
        patch("books.services.run_stage") as run_stage,
    ):
        tasks.after_preprocess([p.pk for p in pages], book.pk)
    assert [call.args for call in run_stage.call_args_list] == [(pages[0], "layout"), (pages[1], "layout")]
    book.refresh_from_db()
    assert book.status == Book.Status.OCR


def test_after_preprocess_skips_the_proposal_when_guides_exist():
    book, pages = _book_with_pages(2, status=Page.Status.PREPROCESSED)
    LayoutGuides.objects.create(book=book, footnote_line=0.8, source=LayoutGuides.Source.MANUAL)
    propose = MagicMock()
    with (
        patch("processing.services.propose_guides", propose),
        patch("books.services.run_stage") as run_stage,
    ):
        tasks.after_preprocess([], book.pk)
    propose.assert_not_called()
    assert run_stage.call_count == 2


def test_ingest_book_task_shows_the_actionable_skip_message_as_the_headline():
    # F57: the Arabic reason is the headline, not the generic "corrupt file" message
    book = make_book(make_scan_pdf(2), skip_first=1, skip_last=1)
    with patch("books.tasks.chord") as chord:
        tasks.ingest_book_task(book.pk)
    chord.assert_not_called()
    book.refresh_from_db()
    assert book.status == Book.Status.ERROR
    assert book.error_message.splitlines()[0].startswith("لا تبقى صفحات")


def _done_book(n: int = 2) -> tuple[Book, list[Page]]:
    book, pages = _book_with_pages(
        n, status=Page.Status.OCR_DONE, text_state=Page.TextState.FINAL, provisional_text="مبدئي"
    )
    for page in pages:
        Preprocess.objects.create(page=page, output_width=100, output_height=100)
        Region.objects.create(page=page, kind=Region.Kind.BODY, bbox=[0, 0, 100, 100], order=0)
    book.status = Book.Status.READY_FOR_REVIEW
    book.save()
    return book, pages


def test_rerun_book_resets_pages_so_the_book_waits_for_its_last_page():
    # F1: a re-run from OCR must not conclude when the first page is through
    book, pages = _done_book(2)
    Page.objects.filter(pk=pages[0].pk).update(attention_flags=["ocr_fallback", "large_skew"])
    with patch("books.services.chain"):
        assert services.rerun_book(book, "ocr") == 2
    pages = list(book.pages.order_by("number"))
    assert [(p.status, p.text_state) for p in pages] == [("layout_done", "none")] * 2
    assert pages[0].attention_flags == ["large_skew"]  # the OCR flag is recomputed by the re-run
    book.refresh_from_db()
    assert book.status == Book.Status.OCR and services.book_progress(book)["active"]

    Page.objects.filter(pk=pages[0].pk).update(status=Page.Status.OCR_DONE)  # page 1 finalised
    assert book.refresh_status() == Book.Status.OCR  # page 2 is still queued

    Page.objects.filter(pk=pages[1].pk).update(status=Page.Status.OCR_DONE, text_state=Page.TextState.FINAL)
    _end_runs(book)  # both chains are through
    pages[1].refresh_from_db()
    with patch("books.services.chain"):
        services.run_stage(pages[1], "ocr_full")
    pages[1].refresh_from_db()
    assert (pages[1].status, pages[1].text_state) == ("layout_done", "provisional")
    assert (
        pages[1].status in services.ACTIVE_PAGE_STATUSES
        and pages[1].book.status in services.ACTIVE_BOOK_STATUSES
    )


def test_rerun_book_resumes_a_book_parked_in_needs_guides_by_an_earlier_version():
    # regions are derived per page now: a legacy `needs_guides` book is re-run from layout
    book, pages = _book_with_pages(2, status=Page.Status.PREPROCESSED)
    book.status = Book.Status.NEEDS_GUIDES
    book.save()
    with patch("books.services.chain") as chain:
        assert services.rerun_book(book, "layout") == 2
    assert chain.call_count == 2
    book.refresh_from_db()
    assert book.status == Book.Status.PROCESSING  # pages queued (the chain is mocked here)


def test_re_including_an_unprocessed_page_enqueues_its_pipeline():
    # F3: the book goes back to work only when work is actually enqueued
    book, pages = _done_book(2)
    extra = Page.objects.create(
        book=book, number=3, source_index=2, is_excluded=True, status=Page.Status.EXCLUDED
    )
    with patch("books.services.chain") as chain:
        chain.return_value.apply_async.return_value.id = "task-9"
        services.toggle_exclude(extra)
    chain.assert_called_once()
    extra.refresh_from_db()
    assert extra.status == Page.Status.UPLOADED and extra.task_id == "task-9"
    book.refresh_from_db()
    assert book.status == Book.Status.OCR

    # a page that already has its text simply rejoins: nothing to run, the book stays done
    services.toggle_exclude(pages[0])
    with patch("books.services.chain") as chain:
        services.toggle_exclude(pages[0])
    chain.assert_not_called()


def test_books_overview_uses_a_constant_number_of_queries(django_assert_max_num_queries):
    for _ in range(5):
        book, pages = _book_with_pages(2)
        Page.objects.filter(pk=pages[0].pk).update(attention_flags=["large_skew"])
    with django_assert_max_num_queries(3):
        rows = services.books_overview()
    assert len(rows) == 5 and all(row["total"] == 2 and row["flags"] == 1 for row in rows)


def test_book_text_joins_the_clean_text_of_included_pages(client, editor):
    book, pages = _book_with_pages(4)
    Page.objects.filter(pk=pages[0].pk).update(
        final_text="سطر  أول   ١٢٣\n\n\n(١) حاشية", provisional_text="قديم"
    )
    Page.objects.filter(pk=pages[1].pk).update(provisional_text="نصٌّ مبدئيّ ٤٥")
    Page.objects.filter(pk=pages[2].pk).update(final_text="مستثناة", is_excluded=True)
    # pages[3] has no text yet
    expected = "سطر أول 123\n\n(1) حاشية\n\nنصٌّ مبدئيّ 45"
    assert services.book_text(book) == {"text": expected, "pages": 2}

    url = reverse("api:book_text", args=[book.pk])
    assert url == f"/api/books/{book.pk}/text/"
    assert client.get(url).status_code == 403  # login required
    client.force_login(editor)
    assert client.get(url).json() == {"text": expected, "pages": 2}


def test_run_stage_runs_the_real_chain_on_a_page_with_a_rule_and_a_strip():
    # F55: real Celery chain (eager) from preprocessing to the final text, then a guides re-run
    from django.core.files.base import ContentFile

    from ocr.engines import registry
    from ocr.tests import engines
    from processing import services as proc
    from processing.tests import png_bytes, render_page

    book = Book.objects.create(title="ك", status=Book.Status.PROCESSING)
    page = Page.objects.create(book=book, number=1, source_index=0, width=900, height=1200)
    gray = render_page(n_lines=14, rule_y=800, footnote_lines=3, strip="left")
    page.original_image.save("original.png", ContentFile(png_bytes(gray)), save=True)
    with registry.override(engines()):
        services.run_stage(page, "preprocess")
        page.refresh_from_db()
        assert page.status == Page.Status.OCR_DONE and page.text_state == Page.TextState.FINAL
        assert page.run_token == "" and page.run_claimed_at is None  # the chain's last task gave the claim back
        pre = page.preprocess
        assert pre.edge_strips_removed and pre.footnote_rule_y is not None
        proc.apply_guides(book, {"footnote_line": pre.footnote_rule_y / pre.output_height})
    page.refresh_from_db()
    assert page.status == Page.Status.OCR_DONE and page.error_message == ""
    assert "footnote" in set(page.regions.values_list("kind", flat=True))
    assert "\n\n" in page.final_text  # footnotes after a blank line
    book.refresh_from_db()
    assert book.status == Book.Status.READY_FOR_REVIEW


def test_smoke_pipeline_command_runs_a_pdf_through_the_pipeline(tmp_path):
    # F65: the RUNBOOK §8 smoke test is repeatable (fake engines stand in for the real ones here)
    from django.core.management import call_command

    from ocr.engines import registry
    from ocr.tests import engines

    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(make_scan_pdf(1))
    out = io.StringIO()
    with registry.override(engines()):
        call_command("smoke_pipeline", str(pdf), stdout=out)
    book = Book.objects.get()
    assert book.pages.get().status == Page.Status.OCR_DONE
    assert book.awaits_ocr_start is False  # paused at «تم التخطيط», then `start_ocr`
    assert f"book {book.pk}: needs_guides after «التخطيط»" in out.getvalue()
    assert f"book {book.pk}: {book.status}" in out.getvalue()


# ====================================================================== views


@pytest.mark.parametrize(
    "name, kwargs",
    [
        ("books:list", {}),
        ("books:create", {}),
        ("books:detail", {"book_id": 1}),
        ("books:guides", {"book_id": 1}),
    ],
)
def test_html_views_require_login(client, name, kwargs):
    response = client.get(reverse(name, kwargs=kwargs))
    assert response.status_code == 302
    assert response["Location"].startswith("/accounts/login/?next=")


@pytest.mark.parametrize(
    "name, kwargs",
    [("api:book_progress", {"book_id": 1}), ("api:book_stages", {"book_id": 1})],
)
def test_api_views_require_login(client, name, kwargs):
    response = client.get(reverse(name, kwargs=kwargs))
    assert response.status_code in (401, 403)


def test_post_actions_require_login_and_the_editor_role(client, proofreader):
    book, pages = _book_with_pages(1)
    urls = [
        reverse("books:start", args=[book.pk]),
        reverse("books:rerun", args=[book.pk]),
        reverse("books:toggle_exclude", args=[book.pk, 1]),
        reverse("books:rerun", args=[book.pk, 1]),
    ]
    for url in urls:
        assert client.post(url).status_code == 302
    client.force_login(proofreader)
    for url in urls:
        assert client.post(url, {"stage": "ocr"}).status_code == 403
    assert client.get(reverse("books:create")).status_code == 403
    assert client.get(reverse("books:list")).status_code == 200  # reading is allowed


def test_list_shows_an_empty_state_then_rows(editor_client):
    response = editor_client.get(reverse("books:list"))
    assert response.status_code == 200
    body = response.content.decode()
    assert "لا كتب بعد" in body and "بدء المعالجة" in body
    assert reverse("books:create") in body

    book = make_book(make_scan_pdf(2))
    services.ingest_book(book)
    body = editor_client.get(reverse("books:list")).content.decode()
    assert "كتاب الاختبار" in body and "مؤلف" in body
    assert reverse("books:detail", args=[book.pk]) in body
    assert 'class="dot dot-neutral"' in body and "مرفوع" in body
    # the shelf (books/test_shelf.py): its six steps, the step it is at with its share of the pages
    assert 'class="lb-steps"' in body and "التخطيط" in body and '<bdi class="lb-next-text">0/2</bdi>' in body


def test_create_view_renders_the_form(editor_client):
    response = editor_client.get(reverse("books:create"))
    assert response.status_code == 200
    body = response.content.decode()
    assert 'enctype="multipart/form-data"' in body
    for label in ("العنوان", "المؤلف", "ملف PDF", "تجاوز الصفحات الأولى", "صفحات في كل ورقة", "موضع القص"):
        assert label in body
    assert 'name="pages_per_sheet" value="2"' in body and 'class="segmented"' in body
    assert 'name="split_ratio"' in body and 'type="range"' in body
    assert "bookForm(" in body


def test_create_view_creates_the_book_and_redirects_to_the_dashboard(editor_client, editor):
    with patch("books.tasks.ingest_book_task.delay") as delay:  # «استخراج الصفحات» starts at once (D66)
        response = editor_client.post(
            reverse("books:create"),
            {
                "title": "بعض الملامح التاريخية",
                "author": "مؤلف",
                "original_year": "1966",
                "notes": "",
                "skip_first": "0",
                "skip_last": "0",
                "pages_per_sheet": "2",
                "split_ratio": "0.55",
                "use_text_layer": "on",
                "source_pdf": SimpleUploadedFile(
                    "book.pdf", make_text_pdf(3), content_type="application/pdf"
                ),
            },
        )
    book = Book.objects.get(title="بعض الملامح التاريخية")
    delay.assert_called_once_with(book.pk)
    assert response.status_code == 302 and response["Location"] == reverse("books:detail", args=[book.pk])
    assert book.created_by == editor
    # item 20: the text layer is off, so the posted option is ignored and the book's text comes from OCR
    assert (book.pages_per_sheet, book.split_ratio, book.use_text_layer) == (2, 0.55, False)
    assert book.source_page_count == 3 and book.has_text_layer is True
    assert book.source_pdf.name == f"books/{book.pk}/source.pdf"


def test_the_text_layer_option_is_offered_only_while_the_setting_is_on(editor_client, settings):
    # item 20: hidden at book creation (OCR only); `NASSAKH["TEXT_LAYER"]` brings it back as it was
    body = editor_client.get(reverse("books:create")).content.decode()
    assert 'name="use_text_layer"' not in body and "الطبقة النصية" not in body
    assert "use_text_layer" not in BookForm().fields
    make_book(make_text_pdf(1), use_text_layer=True)  # a script passing it still gets OCR
    assert not services.book_uses_text_layer(Book.objects.get())

    settings.NASSAKH = {**settings.NASSAKH, "TEXT_LAYER": True}
    body = editor_client.get(reverse("books:create")).content.decode()
    assert 'name="use_text_layer"' in body
    assert services.book_uses_text_layer(Book.objects.get())
    form = BookForm(
        {"title": "ك", "skip_first": 0, "skip_last": 0, "pages_per_sheet": 1, "use_text_layer": "on"},
        {"source_pdf": SimpleUploadedFile("b.pdf", make_text_pdf(1), content_type="application/pdf")},
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["use_text_layer"] is True


def test_create_view_rejects_a_non_pdf_and_bad_skip_values(editor_client):
    base = {
        "title": "كتاب",
        "skip_first": "0",
        "skip_last": "0",
        "pages_per_sheet": "1",
        "split_ratio": "0.5",
    }
    response = editor_client.post(
        reverse("books:create"),
        {**base, "source_pdf": SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")},
    )
    assert response.status_code == 200
    assert "يُقبل ملف PDF فقط" in response.content.decode()
    assert Book.objects.count() == 0

    response = editor_client.post(
        reverse("books:create"),
        {
            **base,
            "source_pdf": SimpleUploadedFile("x.pdf", b"%PDF-1.4 broken", content_type="application/pdf"),
        },
    )
    assert response.status_code == 200 and "تعذّر فتح الملف" in response.content.decode()

    response = editor_client.post(
        reverse("books:create"),
        {
            **base,
            "skip_first": "2",
            "skip_last": "1",
            "source_pdf": SimpleUploadedFile("x.pdf", make_scan_pdf(3), content_type="application/pdf"),
        },
    )
    assert response.status_code == 200
    assert "لا تترك أي صفحة" in response.content.decode()
    assert Book.objects.count() == 0

    response = editor_client.post(reverse("books:create"), {**base, "title": ""})
    assert "أدخل عنوان الكتاب." in response.content.decode() and "اختر ملف PDF." in response.content.decode()


def test_detail_shows_the_empty_state_before_ingest_and_the_dashboard_after(editor_client):
    book = make_book(make_scan_pdf(2))
    url = reverse("books:detail", args=[book.pk])
    body = editor_client.get(url).content.decode()
    assert "لم تُستخرج الصفحات بعد" in body
    assert reverse("books:start", args=[book.pk]) in body
    assert services.book_dashboard(book)["rerun_stages"] == []  # «⋯» offers no re-run before extraction

    pages = services.ingest_book(book)
    Page.objects.filter(pk=pages[0].pk).update(status=Page.Status.ERROR, error_message="فشل\nTraceback")
    Page.objects.filter(pk=pages[1].pk).update(attention_flags=["large_skew"])
    book.status = Book.Status.OCR
    book.awaits_ocr_start = False  # «بدء المعالجة» clicked: today's dashboard
    book.save()

    response = editor_client.get(url)
    assert response.status_code == 200
    body = response.content.decode()
    assert "bookDashboard(" in body and 'id="dashboard-config"' in body
    assert reverse("api:book_progress", args=[book.pk]) in body
    for label in ("مرفوعة", "مُجهَّزة", "بانتظار التعرّف", "تم التعرّف", "خطأ", "التقدّم", "الصفحات"):
        assert label in body
    # two server-rendered tiles plus the inert <template id="tile-shell"> clone for later pages
    assert body.count('<div class="page-tile') == 3 and 'id="tile-shell"' in body
    assert body.count('data-page-id="') - body.count('data-page-id=""') == 4  # 2 tiles + 2 sheets
    assert reverse("books:guides", args=[book.pk]) in body  # «⋯» «التخطيط» and the sheet addresses (D84)
    assert reverse("books:toggle_exclude", args=[book.pk, 1]) in body
    assert reverse("books:rerun", args=[book.pk]) in body and "إعادة التشغيل" in body
    config = _json_config(body, "dashboard-config")
    # the start form is rendered role-gated and chosen client-side (spec §2.1.5): not startable while in OCR
    assert config["status"] == Book.Status.OCR and "x-show=\"d.primary === 'start'\"" in body
    assert config["progressUrl"] == reverse("api:book_progress", args=[book.pk])
    assert config["active"] is True and config["total"] == 2 and config["flags"] == 2
    assert [p["number"] for p in config["pages"]] == [1, 2]
    assert config["pages"][0]["error"] is True and config["pages"][0]["error_headline"] == "فشل"
    assert config["pages"][1]["flag_labels"] == ["انحراف كبير"]
    assert {s["key"] for s in config["stages"]} == {
        "uploaded",
        "preprocessed",
        "layout_done",
        "ocr_done",
        "error",
    }


def test_detail_shows_error_banner_and_restart(editor_client):
    book = make_book(make_scan_pdf(1))
    services.set_book_error(book, "تعذّر استخراج الصفحات.", RuntimeError("boom"))
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    assert "banner banner-danger" in body and "تعذّر استخراج الصفحات." in body
    assert "RuntimeError: boom" in body and "إعادة استخراج الصفحات" in body  # the «التخطيط» mode's primary
    config = _json_config(body, "dashboard-config")  # the banner's Alpine bindings start from these
    assert (
        config["errorHeadline"] == "تعذّر استخراج الصفحات." and "RuntimeError: boom" in config["errorDetail"]
    )
    assert config["barState"] == "danger"


def test_toggle_exclude_view_flips_and_redirects_back(editor_client):
    book, pages = _book_with_pages(2)
    url = reverse("books:toggle_exclude", args=[book.pk, 1])
    response = editor_client.post(url, {"next": reverse("books:detail", args=[book.pk])})
    assert response.status_code == 302 and response["Location"] == reverse("books:detail", args=[book.pk])
    pages[0].refresh_from_db()
    assert pages[0].is_excluded is True and pages[0].status == Page.Status.EXCLUDED
    response = editor_client.post(url, {"next": "https://evil.example/"})  # external next is ignored
    assert response["Location"] == reverse("books:detail", args=[book.pk])
    pages[0].refresh_from_db()
    assert pages[0].is_excluded is False and pages[0].status == Page.Status.UPLOADED
    assert editor_client.get(url).status_code == 405


def test_start_view_enqueues_and_reports(editor_client):
    book = make_book(make_scan_pdf(1))
    with patch("books.tasks.ingest_book_task.delay") as delay:
        response = editor_client.post(reverse("books:start", args=[book.pk]), follow=True)
    delay.assert_called_once_with(book.pk)
    assert "بدأ استخراج الصفحات" in response.content.decode()
    book.refresh_from_db()
    assert book.status == Book.Status.PROCESSING
    # Starting again while processing is refused with an Arabic message, not an exception.
    with patch("books.tasks.ingest_book_task.delay") as delay:
        response = editor_client.post(reverse("books:start", args=[book.pk]), follow=True)
    delay.assert_not_called()
    assert "المعالجة جارية بالفعل" in response.content.decode()


@pytest.fixture
def superuser_client(client):
    client.force_login(User.objects.create_superuser("owner", password="pass-1234"))
    return client


def test_rerun_view_for_the_book_and_for_one_page(superuser_client):
    book, pages = _book_with_pages(2, status=Page.Status.OCR_DONE)
    with patch("books.tasks.rerun_book_from.delay") as delay:
        response = superuser_client.post(reverse("books:rerun", args=[book.pk]), {"stage": "layout"})
    token = Page.objects.get(pk=pages[0].pk).run_token  # both pages claimed in the request (item 29)
    assert token and set(book.pages.values_list("run_token", flat=True)) == {token}
    delay.assert_called_once_with(book.pk, "layout", run=token)
    assert response["Location"] == reverse("books:detail", args=[book.pk])
    _end_runs(book)

    with patch("books.services.run_stage") as run_stage:
        response = superuser_client.post(reverse("books:rerun", args=[book.pk, 2]) + "?stage=ocr_full")
    assert run_stage.call_args.args[0].pk == pages[1].pk and run_stage.call_args.args[1] == "ocr_full"
    assert response["Location"] == services.sheet_url(book.pk, 2)

    with patch("books.tasks.rerun_book_from.delay") as delay:
        url = reverse("books:rerun", args=[book.pk])
        response = superuser_client.post(url, {"stage": "nope"}, follow=True)
    delay.assert_not_called()
    assert "اختر مرحلة صحيحة" in response.content.decode()


def test_rerun_view_reports_approved_pages_and_refuses_a_fully_approved_book(superuser_client):
    book, pages = _book_with_pages(2, status=Page.Status.OCR_DONE, text_state=Page.TextState.FINAL)
    _approve(pages[0])
    url = reverse("books:rerun", args=[book.pk])
    with patch("books.tasks.rerun_book_from.delay") as delay:
        response = superuser_client.post(url, {"stage": "ocr"}, follow=True)
    delay.assert_called_once()
    assert Page.objects.get(pk=pages[0].pk).run_token == ""  # the approved page is not claimed
    assert "تُركت 1 صفحة معتمدة كما هي." in response.content.decode()
    _end_runs(book)

    _approve(pages[1])
    with patch("books.tasks.rerun_book_from.delay") as delay:
        response = superuser_client.post(url, {"stage": "ocr"}, follow=True)
    delay.assert_not_called()
    assert "كل صفحات الكتاب معتمدة" in response.content.decode()
    response = superuser_client.post(reverse("books:rerun", args=[book.pk, 1]), {"stage": "ocr"}, follow=True)
    assert "الصفحة معتمدة؛ أعد فتحها من شاشة المراجعة" in response.content.decode()


# ====================================================================== one run per page (item 29)


def test_a_second_book_rerun_queues_nothing_while_the_first_holds_the_pages(superuser_client):
    # the owner's double «إعادة التعرّف»: the second submit is refused in the request, nothing is queued twice
    book, pages = _book_with_pages(3, status=Page.Status.OCR_DONE)
    url = reverse("books:rerun", args=[book.pk])
    with patch("books.tasks.rerun_book_from.delay") as delay:
        superuser_client.post(url, {"stage": "ocr"})
        again = superuser_client.post(url, {"stage": "ocr_full"}, follow=True)
    delay.assert_called_once()
    assert "تجري معالجة 3 صفحات من هذا الكتاب الآن" in again.content.decode()
    # a page re-run of a held page is refused as well, and so is the re-run task itself
    with patch("books.services.chain") as chain:
        page_url = reverse("books:rerun", args=[book.pk, 2])
        page_again = superuser_client.post(page_url, {"stage": "ocr"}, follow=True)
        with pytest.raises(ValueError, match="تجري معالجة"):
            services.rerun_book(book, "ocr")
    chain.assert_not_called()
    assert "تجري معالجة هذه الصفحة الآن" in page_again.content.decode()


def test_the_rerun_task_runs_only_the_pages_its_claim_holds():
    book, pages = _book_with_pages(3, status=Page.Status.OCR_DONE)
    with patch("books.tasks.rerun_book_from.delay") as delay:
        assert services.queue_book_rerun(book, "ocr") == 3
    token = delay.call_args.kwargs["run"]
    # meanwhile page 3 was excluded and page 2 approved: their claims are given back, they are not re-run
    Page.objects.filter(pk=pages[2].pk).update(is_excluded=True, status=Page.Status.EXCLUDED)
    _approve(pages[1])
    with patch("books.services.chain") as chain:
        assert tasks.rerun_book_from(book.pk, "ocr", run=token) == book.pk
    assert chain.call_count == 1
    assert [p.run_token for p in book.pages.order_by("number")] == [token, "", ""]
    # the broker refusing the task gives every claim back
    _end_runs(book)
    Page.objects.filter(pk=pages[1].pk).update(status=Page.Status.OCR_DONE, reviewed_at=None)
    with patch("books.tasks.rerun_book_from.delay", side_effect=ConnectionError("down")):
        with pytest.raises(ValueError, match="تعذّر إرسال العمل"):
            services.queue_book_rerun(book, "ocr")
    assert set(book.pages.values_list("run_token", flat=True)) == {""}


def test_run_stage_claims_the_page_and_gives_the_claim_back_when_it_refuses():
    _, pages = _book_with_pages(2)
    page, approved = pages
    with patch("books.services.chain") as chain:
        services.run_stage(page, "ocr")
        with pytest.raises(ValueError, match="تجري معالجة هذه الصفحة الآن"):
            services.run_stage(page, "ocr_full")  # a second click
    assert chain.call_count == 1 and Page.objects.get(pk=page.pk).run_token
    _approve(approved)
    with patch("books.services.chain"), pytest.raises(ValueError, match="أعد فتحها"):
        services.run_stage(approved, "ocr")  # refused after the claim: the claim is given back
    fresh = Page.objects.create(book=page.book, number=3, source_index=2)
    with patch("books.services.chain") as chain:
        chain.return_value.apply_async.side_effect = ConnectionError("down")  # the broker refused
        with pytest.raises(ConnectionError):
            services.run_stage(fresh, "ocr")
    assert list(page.book.pages.exclude(pk=page.pk).values_list("run_token", flat=True)) == ["", ""]


def test_a_claim_past_the_claim_hours_no_longer_blocks(settings):
    from datetime import timedelta

    from books import runs

    _, pages = _book_with_pages(1)
    page = pages[0]
    assert runs.claim_page(page.pk)
    assert runs.claim_page(page.pk) == "" and runs.is_active(page.pk)
    Page.objects.filter(pk=page.pk).update(run_claimed_at=timezone.now() - timedelta(hours=13))
    assert not runs.is_active(page.pk)  # 12 hours by default: a run whose messages were lost
    newer = runs.claim_page(page.pk)
    assert newer and runs.is_current(page.pk, newer) and runs.is_current(page.pk, "")
    settings.NASSAKH = {**settings.NASSAKH, "RUN_CLAIM_HOURS": 1}
    Page.objects.filter(pk=page.pk).update(run_claimed_at=timezone.now() - timedelta(hours=2))
    assert runs.claim_page(page.pk)


def test_tasks_of_a_superseded_run_skip_the_page_and_the_last_task_releases():
    from ocr import tasks as ocr_tasks
    from processing import tasks as processing_tasks

    _, pages = _book_with_pages(1, status=Page.Status.LAYOUT_DONE)
    page = pages[0]
    Page.objects.filter(pk=page.pk).update(run_token="b" * 32, run_claimed_at=timezone.now())
    with (
        patch("processing.services.preprocess_page") as preprocess,
        patch("processing.services.derive_regions") as derive,
        patch("ocr.services.run_fast_ocr") as fast,
        patch("ocr.services.run_full_ocr") as full,
    ):
        old = "a" * 32  # an earlier run's messages, still in the queue
        processing_tasks.preprocess_page(page.pk, run=old)
        processing_tasks.layout_page(page.pk, run=old)
        ocr_tasks.ocr_page_fast(page.pk, run=old)
        ocr_tasks.ocr_page_full(page.pk, run=old, last=True)
        for mock in (preprocess, derive, fast, full):
            mock.assert_not_called()
        assert Page.objects.get(pk=page.pk).run_token == "b" * 32  # an old run never releases the new one
        ocr_tasks.ocr_page_fast(page.pk, run="b" * 32)
        assert Page.objects.get(pk=page.pk).run_token == "b" * 32
        ocr_tasks.ocr_page_full(page.pk, run="b" * 32, last=True)
        fast.assert_called_once()
        full.assert_called_once()
    assert Page.objects.get(pk=page.pk).run_token == ""
    with patch("ocr.services.run_fast_ocr") as fast:  # a task queued without a run (ingest, older messages)
        ocr_tasks.ocr_page_fast(page.pk)
    fast.assert_called_once()


def test_the_numbers_pass_of_an_earlier_run_skips_a_page_a_newer_run_holds(settings):
    from ocr import tasks as ocr_tasks

    settings.NASSAKH = {**settings.NASSAKH, "CALLS_PASS": False}
    _, pages = _book_with_pages(1, status=Page.Status.OCR_DONE)
    page = pages[0]
    Page.objects.filter(pk=page.pk).update(run_token="n" * 32, run_claimed_at=timezone.now())
    with patch("ocr.numbers.read_page_numbers") as read:
        ocr_tasks.read_numbers(page.pk, run="o" * 32)  # queued by the run before: the newer one reads again
        read.assert_not_called()
        ocr_tasks.read_numbers(page.pk, run="n" * 32)  # queued by the run that holds the page
        assert read.call_count == 1
        _end_runs(page.book)
        ocr_tasks.read_numbers(page.pk, run="o" * 32)  # no run holds the page: the pass runs
        ocr_tasks.read_numbers(page.pk)
        assert read.call_count == 3


def test_release_page_runs_frees_the_claims_of_a_book_or_a_page():
    from django.core.management import CommandError, call_command

    book, pages = _book_with_pages(3)
    other, _ = _book_with_pages(1)
    Page.objects.update(run_token="f" * 32, run_claimed_at=timezone.now())
    out = io.StringIO()
    call_command("release_page_runs", "--book", str(book.pk), "--page", "2", stdout=out)
    assert "1 page run claim(s) released." in out.getvalue()
    assert [p.run_token for p in book.pages.order_by("number")] == ["f" * 32, "", "f" * 32]
    call_command("release_page_runs", "--book", str(book.pk), stdout=io.StringIO())
    assert set(book.pages.values_list("run_token", flat=True)) == {""}
    assert other.pages.get().run_token == "f" * 32
    call_command("release_page_runs", "--all", stdout=io.StringIO())
    assert not Page.objects.exclude(run_token="").exists()
    with pytest.raises(CommandError):
        call_command("release_page_runs")


def test_a_chain_that_dies_releases_its_claim_and_marks_the_waiting_page():
    book, pages = _book_with_pages(2, status=Page.Status.LAYOUT_DONE)
    book.status = Book.Status.OCR
    book.save()
    page, other = pages
    Page.objects.filter(pk=page.pk).update(run_token="c" * 32, run_claimed_at=timezone.now())
    Page.objects.filter(pk=other.pk).update(status=Page.Status.OCR_DONE, text_state=Page.TextState.FINAL)
    request = MagicMock(task="ocr.tasks.ocr_page_full")
    tasks.page_run_failed(request, TimeoutError("hard time limit"), None, page_id=page.pk, run="d" * 32)
    assert Page.objects.get(pk=page.pk).run_token == "c" * 32  # not this run's claim: nothing happens
    tasks.page_run_failed(request, TimeoutError("hard time limit"), None, page_id=page.pk, run="c" * 32)
    page.refresh_from_db()
    assert (page.run_token, page.status, page.error_from) == ("", Page.Status.ERROR, "ocr_full")
    assert page.error_message.startswith(tasks.RUN_DIED_ERROR)
    book.refresh_from_db()
    assert book.status == Book.Status.READY_FOR_REVIEW  # nothing left to wait for


def test_book_reruns_are_the_super_admins_and_editors_may_only_retry_failed_pages(editor_client):
    book, pages = _book_with_pages(2, status=Page.Status.OCR_DONE)
    with patch("books.tasks.rerun_book_from.delay") as delay, patch("books.services.chain") as chain:
        assert editor_client.post(reverse("books:rerun", args=[book.pk]), {"stage": "ocr"}).status_code == 403
        page_url = reverse("books:rerun", args=[book.pk, 1])
        assert editor_client.post(page_url, {"stage": "ocr"}).status_code == 403
        pages[1].set_error("ocr_full", "تعذّر التعرّف على النص.")
        response = editor_client.post(reverse("books:rerun", args=[book.pk, 2]), {"stage": "ocr_full"})
    delay.assert_not_called()
    assert response.status_code == 302 and chain.call_count == 1  # the retry of a failed page
    assert services.may_rerun(User(is_superuser=True)) and not services.may_rerun(User())


def test_the_dashboard_offers_book_reruns_to_the_super_admin_only(editor_client, client):
    book, _ = _book_with_pages(2, status=Page.Status.OCR_DONE)
    book.status = Book.Status.READY_FOR_REVIEW
    book.save()
    url = reverse("books:detail", args=[book.pk])
    paused = Book.objects.create(title="ك", status=Book.Status.NEEDS_GUIDES, awaits_ocr_start=True)
    Page.objects.create(book=paused, number=1, source_index=0, status=Page.Status.PREPROCESSED)
    paused_url = reverse("books:detail", args=[paused.pk])
    assert "data-rerun-stage" not in editor_client.get(url).content.decode()
    assert "data-guides-reprepare" not in editor_client.get(paused_url).content.decode()
    client.force_login(User.objects.create_superuser("owner", password="pass-1234"))
    body = client.get(url).content.decode()
    assert body.count("data-rerun-stage=") == 5 and "إعادة التشغيل من مرحلة" in body
    body = client.get(paused_url).content.decode()
    assert "data-guides-reprepare" in body and "إعادة تجهيز الصفحات…" in body


def test_guide_changes_wait_for_the_runs_of_a_started_book():
    from processing import services as processing

    book, pages = _done_book(2)
    Page.objects.filter(pk=pages[0].pk).update(run_token="e" * 32, run_claimed_at=timezone.now())
    with pytest.raises(processing.ProcessingError, match="تجري معالجة هذه الصفحة الآن"):
        processing.set_page_guides(pages[0], {"footnote_line": 0.8}, stage="ocr")
    with patch("books.services.chain") as chain, pytest.raises(processing.ProcessingError, match="واحدة"):
        processing.apply_book_guides(book, {"footnote_line": 0.8}, stage="ocr")
    chain.assert_not_called()
    assert Page.objects.get(pk=pages[0].pk).guides_override is None
    assert not processing.LayoutGuides.objects.filter(book=book, source="manual").exists()


def test_dashboard_attention_list_offers_a_retry_for_a_failed_page(editor_client):
    # D22: a failed page shows its Arabic headline and a retry from the failed stage on the dashboard
    book, pages = _book_with_pages(2, status=Page.Status.OCR_DONE)
    pages[1].set_error("ocr_full", "تعذّر التعرّف على النص.\nRuntimeError: boom")
    tile = services.page_tile(pages[1])
    assert tile["retry_stage"] == "ocr_full" and tile["retry_label"] == services.STAGE_LABELS["ocr_full"]
    assert tile["rerun_url"] == reverse("books:rerun", args=[book.pk, 2])
    assert services.page_tile(pages[0])["retry_stage"] == ""
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    assert ':action="item.rerun_url"' in body and 'x-text="item.retry_label"' in body

    # the retry comes back to the dashboard it was sent from
    with patch("books.services.run_stage"):
        response = editor_client.post(
            reverse("books:rerun", args=[book.pk, 2]),
            {"stage": "ocr_full", "next": reverse("books:detail", args=[book.pk])},
        )
    assert response["Location"] == reverse("books:detail", args=[book.pk])


def test_api_progress(editor_client):
    book, pages = _book_with_pages(3)
    Page.objects.filter(pk=pages[0].pk).update(
        status=Page.Status.OCR_DONE,
        text_state=Page.TextState.FINAL,
        final_text="النص النهائي",
        provisional_text="نص مبدئي",
    )
    Page.objects.filter(pk=pages[1].pk).update(
        status=Page.Status.ERROR, error_from="ocr_full", error_message="فشل"
    )
    book.status = Book.Status.OCR
    book.save()

    data = editor_client.get(reverse("api:book_progress", args=[book.pk])).json()
    assert set(data) >= {"total", "by_status", "percent", "active", "flags", "pages"}
    assert data["total"] == 3 and data["active"] is True and data["flags"] == 1
    assert data["by_status"]["ocr_done"] == 1 and data["by_status"]["error"] == 1
    assert [p["number"] for p in data["pages"]] == [1, 2, 3]
    assert data["pages"][1]["error"] is True and data["pages"][1]["dot"] == "dot-danger"
    assert data["pages"][0]["url"] == services.sheet_url(book.pk, 1)


# ---------------------------------------------------------------- printed page numbers


def _numbered(numbers: list[str], excluded: tuple[int, ...] = ()) -> tuple[Book, list[Page]]:
    book, pages = _book_with_pages(len(numbers), status=Page.Status.OCR_DONE)
    for page, printed in zip(pages, numbers, strict=True):
        page.printed_number = printed
        page.is_excluded = page.number in excluded
        page.save()
    return book, pages


def test_page_sequence_issues_flags_a_confirmed_gap_and_a_confirmed_duplicate():
    book, pages = _numbered(["40", "41", "43", "44", "44", "45"])
    issues = services.page_sequence_issues(book)
    assert issues == {
        pages[2].pk: "ترقيم غير متسلسل: بعد 41 جاءت 43",  # 44 confirms the new numbering
        pages[4].pk: "ترقيم مكرّر: بعد 44 جاءت 44 مرة أخرى",  # 45 confirms the duplicate scan
    }
    items = services.attention_pages(book)
    assert [(i["page"].number, i["sequence_issue"]) for i in items] == [
        (3, "ترقيم غير متسلسل: بعد 41 جاءت 43"),
        (5, "ترقيم مكرّر: بعد 44 جاءت 44 مرة أخرى"),
    ]
    tiles = {t["number"]: t for t in services.page_tiles(book)}
    assert tiles[3]["sequence_issue"].startswith("ترقيم غير متسلسل") and tiles[6]["sequence_issue"] == ""
    assert tiles[1]["printed_number"] == "40"


def test_page_sequence_issues_treats_a_single_odd_number_as_an_uncertain_read():
    # 47 breaks the sequence but 43 continues from 41: a misread digit, not a missing scan
    book, pages = _numbered(["40", "41", "47", "43", "44"])
    assert services.page_sequence_issues(book) == {pages[2].pk: "رقم مطبوع غير مؤكد: قُرئ 47 والمتوقع 42"}
    # fewer than three numbered pages: nothing is reported
    book_2, _ = _numbered(["10", "", "30"])
    assert services.page_sequence_issues(book_2) == {}


def test_page_sequence_issues_tolerates_unread_numbers_and_ignores_excluded_pages():
    # page 2 has no number (not read, or an unnumbered plate); page 4 is an excluded duplicate scan
    book, _pages = _numbered(["10", "", "12", "12", "13", "15"], excluded=(4,))
    book_2, _ = _numbered(["10", "", "11"])
    issues = services.page_sequence_issues(book)
    assert list(issues.values()) == ["ترقيم غير متسلسل: بعد 13 جاءت 15"]
    assert services.page_sequence_issues(book_2) == {}


# ---------------------------------------------------------------- Phase 3: review state and stacked sheets


def _ocr_line(page: Page, order: int, tokens: list[dict], region=None):
    from ocr.models import Line

    text = " ".join(t["t"] for t in tokens)
    return Line.objects.create(page=page, order=order, region=region, text=text, ocr_text=text, tokens=tokens)


def test_tiles_and_progress_carry_the_review_state(editor_client):
    book, pages = _book_with_pages(3)
    Page.objects.filter(pk=pages[0].pk).update(status=Page.Status.OCR_DONE, n_unresolved=5)
    Page.objects.filter(pk=pages[1].pk).update(status=Page.Status.REVIEWED, n_unresolved=1)
    Page.objects.filter(pk=pages[2].pk).update(width=700, height=1000)
    data = editor_client.get(reverse("api:book_progress", args=[book.pk])).json()
    first, second, third = data["pages"]
    # image size for the stacked view's placeholders (no preprocess yet: the scan's size)
    assert (third["width"], third["height"]) == (700, 1000)
    assert first["n_unresolved"] == 5 and first["is_reviewed"] is False
    assert second["is_reviewed"] is True
    assert first["review_url"] == reverse("review:page", args=[book.pk, 1])
    assert data["review"] == {
        "reviewed": 1,
        "total": 3,
        "pending": 1,
        "unresolved_total": 6,
        "next_review_url": reverse("review:next", args=[book.pk]),
    }
    dashboard = services.book_dashboard(book)
    assert dashboard["review"]["reviewed"] == 1
    assert dashboard["config"]["sheetsUrl"] == reverse("api:book_sheets", args=[book.pk])


def test_sheets_api_shape(editor_client):
    book, pages = _book_with_pages(3, width=300, height=500)
    page = pages[1]
    Page.objects.filter(pk=page.pk).update(
        status=Page.Status.OCR_DONE, text_state=Page.TextState.FINAL, n_unresolved=1, printed_number="12"
    )
    pre = Preprocess.objects.create(
        page=page,
        output_width=200,
        output_height=400,
        n_lines=2,
        line_boxes=[{"x0": 20, "y0": 40, "x1": 180, "y1": 60}, {"x0": 10, "y0": 100, "x1": 200, "y1": 120}],
    )
    from core.storage import save_array

    save_array(pre.display_image, np.full((40, 20), 200, dtype=np.uint8), "display.webp")
    save_array(pre.thumbnail, np.full((20, 10), 200, dtype=np.uint8), "thumb.webp")
    pre.save()
    foot = Region.objects.create(page=page, kind="footnote", bbox=[0, 300, 200, 400], order=1)
    first_line = _ocr_line(
        page, 0, [{"t": "قال", "conf": "high"}, {"t": "الكتب", "conf": "low", "alt": "الكتاب", "res": None}]
    )
    first_line.bbox = [20, 40, 180, 60]
    first_line.save()
    foot_line = _ocr_line(page, 1, [{"t": "(١)", "conf": "low", "res": "primary"}], region=foot)

    url = reverse("api:book_sheets", args=[book.pk])
    data = editor_client.get(f"{url}?from=2&to=3").json()
    assert data["from"] == 2 and data["to"] == 3 and data["total"] == 3
    assert [p["number"] for p in data["pages"]] == [2, 3]
    sheet = data["pages"][0]
    assert set(sheet) >= {
        "id", "number", "status", "status_label", "text_state", "provisional_text", "lines", "display_url",
        "scan_url", "thumb_url", "width", "height", "line_boxes", "n_unresolved", "is_reviewed",
        "printed_number", "url", "review_url",
    }  # fmt: skip
    assert sheet["width"] == 200 and sheet["height"] == 400
    assert sheet["line_boxes"] == [[0.1, 0.1, 0.9, 0.15], [0.05, 0.25, 1.0, 0.3]]
    assert sheet["lines"] == [
        {"id": first_line.pk, "order": 0, "region_kind": "body", "kind": "body",
         "bbox": [0.1, 0.1, 0.9, 0.15], "tokens": [
            {"t": "قال", "conf": "high", "res": None}, {"t": "الكتب", "conf": "low", "res": None}]},
        {"id": foot_line.pk, "order": 1, "region_kind": "footnote", "kind": "footnote", "bbox": None,
         "tokens": [{"t": "(١)", "conf": "low", "res": "primary"}]},
    ]  # fmt: skip
    assert sheet["display_url"].endswith("display.webp") and sheet["thumb_url"].endswith("thumb.webp")
    assert sheet["n_unresolved"] == 1 and sheet["printed_number"] == "12" and sheet["is_reviewed"] is False
    assert sheet["review_url"] == reverse("review:page", args=[book.pk, 2])
    blank = data["pages"][1]
    assert blank["width"] == 300 and blank["height"] == 500  # original size before preprocessing
    assert blank["lines"] == [] and blank["line_boxes"] == [] and blank["display_url"] is None


def test_sheets_api_caps_the_range_and_rejects_bad_numbers(editor_client, client):
    book, _ = _book_with_pages(45)
    url = reverse("api:book_sheets", args=[book.pk])
    data = editor_client.get(url).json()
    assert data["from"] == 1 and data["to"] == 40 and len(data["pages"]) == 40
    data = editor_client.get(f"{url}?from=10&to=200").json()
    assert data["to"] == 49 and [p["number"] for p in data["pages"]] == list(range(10, 46))
    for query in ("from=abc", "from=0", "from=5&to=2", "to=-1", "from=٣"):
        response = editor_client.get(f"{url}?{query}")
        assert response.status_code == 400 and response.json()["message"]
    client.logout()
    assert client.get(url).status_code == 403


def test_sheets_query_count_does_not_grow_with_pages(editor_client, django_assert_max_num_queries):
    book, pages = _book_with_pages(12)
    for page in pages:
        _ocr_line(page, 0, [{"t": "كلمة", "conf": "high"}])
    with django_assert_max_num_queries(5):
        data = services.book_sheets(book, 1, 12)
    assert len(data["pages"]) == 12 and all(len(p["lines"]) == 1 for p in data["pages"])


# ---------------------------------------------------------------- D28 dashboard: sheet geometry, compact poll


def _fast_run(page: Page, lines: list[tuple[list[int] | None, list[str]]], region=None, **kwargs):
    """A fast-engine OcrRun with Tesseract-style `params["lines"]` (bbox in gray px)."""
    from ocr.models import OcrRun
    from ocr.services import engine_names

    params = {"scope": "region" if region else "page", "kind": region.kind if region else "page"}
    params["lines"] = [
        {"bbox": bbox, "words": [{"text": w, "bbox": None, "conf": 90} for w in words]}
        for bbox, words in lines
    ]
    return OcrRun.objects.create(
        page=page,
        region=region,
        engine_name=kwargs.pop("engine_name", engine_names()[2]),
        params=params,
        **kwargs,
    )


def _sheet_page(**pre_kwargs) -> tuple[Book, Page]:
    book, pages = _book_with_pages(1, width=300, height=500)
    page = pages[0]
    Page.objects.filter(pk=page.pk).update(
        status=Page.Status.LAYOUT_DONE, text_state=Page.TextState.PROVISIONAL, provisional_text="نص"
    )
    Preprocess.objects.create(page=page, output_width=200, output_height=400, **pre_kwargs)
    page.refresh_from_db()
    return book, page


def test_sheets_provisional_lines_follow_region_order_with_footnotes_last():
    from ocr.models import OcrRun

    book, page = _sheet_page()
    header = Region.objects.create(page=page, kind="running_header", bbox=[0, 0, 200, 20], order=0)
    foot = Region.objects.create(page=page, kind="footnote", bbox=[0, 300, 200, 400], order=1)
    body = Region.objects.create(page=page, kind="body", bbox=[0, 20, 200, 300], order=2)
    number = Region.objects.create(page=page, kind="page_number", bbox=[80, 380, 120, 400], order=3)
    _fast_run(page, [([0, 0, 200, 20], ["عنوان", "الكتاب"])], region=header)
    _fast_run(page, [([0, 0, 200, 20], ["قديم"])], region=body)  # older run of the same target
    _fast_run(
        page, [([20, 40, 180, 60], ["قال", "الشيخ"]), ([10, 100, 200, 120], ["رحمه", "الله"])], region=body
    )
    _fast_run(page, [([0, 320, 200, 340], ["(١)", "انظر"]), ([90, 380, 110, 396], ["- ١٢ -"])], region=foot)
    _fast_run(page, [([80, 380, 120, 400], ["12"])], region=number)
    _fast_run(page, [([0, 0, 1, 1], ["خطأ"])], region=body, status=OcrRun.Status.ERROR)
    _fast_run(page, [([0, 0, 1, 1], ["قارئ"])], region=body, engine_name="qari")
    Page.objects.filter(pk=page.pk).update(printed_number="12")  # what the fast pass read in the region

    sheet = services.book_sheets(book, 1, 1)["pages"][0]
    assert sheet["provisional_lines"] == [
        {"region_kind": "body", "bbox": [0.1, 0.1, 0.9, 0.15], "words": ["قال", "الشيخ"]},
        {"region_kind": "body", "bbox": [0.05, 0.25, 1.0, 0.3], "words": ["رحمه", "الله"]},
        {"region_kind": "footnote", "bbox": [0.0, 0.8, 1.0, 0.85], "words": ["(١)", "انظر"]},
    ]  # the trailing line repeating the page number is dropped; header and page number are not text
    for entry in sheet["provisional_lines"]:
        assert all(0 <= v <= 1 for v in entry["bbox"])
    assert sheet["regions"] == [
        {"kind": "footnote", "bbox": [0.0, 0.75, 1.0, 1.0]},
        {"kind": "body", "bbox": [0.0, 0.05, 1.0, 0.75]},
    ]


def test_sheets_provisional_lines_keep_a_number_line_that_is_not_the_page_number():
    # the page has a page-number region reading «21»: a footnote that ends on a wrapped page
    # reference «٣٤» keeps that line (it is text, not the page number)
    book, page = _sheet_page()
    foot = Region.objects.create(page=page, kind="footnote", bbox=[0, 300, 200, 380], order=0)
    Region.objects.create(page=page, kind="page_number", bbox=[80, 380, 120, 400], order=1)
    _fast_run(page, [([0, 320, 200, 340], ["(١)", "انظر", "ص"]), ([180, 350, 200, 366], ["٣٤"])], region=foot)
    Page.objects.filter(pk=page.pk).update(printed_number="21")
    sheet = services.book_sheets(book, 1, 1)["pages"][0]
    assert [entry["words"] for entry in sheet["provisional_lines"]] == [["(١)", "انظر", "ص"], ["٣٤"]]


def test_sheets_provisional_lines_page_level_run_fallback_and_empty():
    book, page = _sheet_page()
    _fast_run(page, [([20, 40, 180, 60], ["سطر", "أول"]), (None, ["سطر", "ثان"])])
    sheet = services.book_sheets(book, 1, 1)["pages"][0]
    assert sheet["provisional_lines"] == [
        {"region_kind": "body", "bbox": [0.1, 0.1, 0.9, 0.15], "words": ["سطر", "أول"]},
        {"region_kind": "body", "bbox": None, "words": ["سطر", "ثان"]},
    ]
    assert sheet["regions"] == []  # before layout

    # no run with lines (text layer, old runs): derived from provisional_text, bbox null
    page.ocr_runs.all().delete()
    Page.objects.filter(pk=page.pk).update(provisional_text="السطر الأول\n\n  \nالسطر الثاني")
    sheet = services.book_sheets(book, 1, 1)["pages"][0]
    assert sheet["provisional_lines"] == [
        {"region_kind": "body", "bbox": None, "words": ["السطر", "الأول"]},
        {"region_kind": "body", "bbox": None, "words": ["السطر", "الثاني"]},
    ]

    _fast_run(page, [([20, 40, 180, 60], ["سطر"])])
    Page.objects.filter(pk=page.pk).update(text_state=Page.TextState.NONE, provisional_text="")
    assert services.book_sheets(book, 1, 1)["pages"][0]["provisional_lines"] == []


def test_sheets_footnote_y_median_line_height_and_missing_preprocess():
    book, page = _sheet_page(footnote_rule_y=300, footnote_block_y=320, median_line_height=24)
    sheet = services.book_sheets(book, 1, 1)["pages"][0]
    assert sheet["footnote_y"] == 0.75 and sheet["median_line_h"] == 0.06
    Preprocess.objects.filter(page=page).update(footnote_rule_y=None, median_line_height=0)
    sheet = services.book_sheets(book, 1, 1)["pages"][0]
    assert sheet["footnote_y"] == 0.8 and sheet["median_line_h"] == 0
    Preprocess.objects.filter(page=page).update(footnote_block_y=None)
    assert services.book_sheets(book, 1, 1)["pages"][0]["footnote_y"] is None

    bare, _ = _book_with_pages(1, width=300, height=500)
    sheet = services.book_sheets(bare, 1, 1)["pages"][0]
    assert sheet["footnote_y"] is None and sheet["median_line_h"] == 0
    assert sheet["regions"] == [] and sheet["provisional_lines"] == [] and sheet["lines"] == []


def test_sheets_query_count_stays_at_five_with_regions_and_runs(django_assert_max_num_queries):
    book, pages = _book_with_pages(10)
    for page in pages:
        Preprocess.objects.create(page=page, output_width=200, output_height=400, footnote_rule_y=300)
        Page.objects.filter(pk=page.pk).update(text_state=Page.TextState.PROVISIONAL)
        body = Region.objects.create(page=page, kind="body", bbox=[0, 0, 200, 300], order=0)
        foot = Region.objects.create(page=page, kind="footnote", bbox=[0, 300, 200, 400], order=1)
        _fast_run(page, [([0, 10, 200, 30], ["متن"])], region=body)
        _fast_run(page, [([0, 310, 200, 330], ["حاشية"])], region=foot)
        _ocr_line(page, 0, [{"t": "كلمة", "conf": "high"}], region=body)
    with django_assert_max_num_queries(5):
        data = services.book_sheets(book, 1, 10)
    assert all(len(p["provisional_lines"]) == 2 and len(p["regions"]) == 2 for p in data["pages"])


COMPACT_KEYS = {
    "id", "number", "status", "text_state", "n_unresolved", "n_flags", "sequence_issue", "is_excluded",
    "is_reviewed", "error", "printed_number",
}  # fmt: skip


def test_progress_compact_tiles_keys_size_and_book_error(editor_client, django_assert_max_num_queries):
    book, pages = _book_with_pages(30)
    Page.objects.filter(pk=pages[0].pk).update(attention_flags=["alignment_poor"])
    Page.objects.filter(pk=pages[1].pk).update(
        status=Page.Status.ERROR, error_from="ocr_full", error_message="فشل التعرّف\nTraceback"
    )
    url = reverse("api:book_progress", args=[book.pk])
    data = editor_client.get(f"{url}?compact=1").json()
    assert data["error_headline"] == "" and data["error_detail"] == ""
    flagged, failed, plain = data["pages"][:3]
    assert set(plain) == COMPACT_KEYS
    assert (
        set(flagged) == COMPACT_KEYS | {"flag_labels"} and flagged["n_flags"] == 1 and flagged["flag_labels"]
    )
    assert set(failed) == COMPACT_KEYS | {"error_headline", "retry_stage", "retry_label"}
    assert failed["error_headline"] == "فشل التعرّف" and failed["retry_stage"] == "ocr_full"
    assert failed["retry_label"] == services.STAGE_LABELS["ocr_full"]
    # Compact JSON as rendered: the fixed key set alone is ~190 B per page, far below the full tile.
    sizes = [
        len(json.dumps(tile, ensure_ascii=False, separators=(",", ":")).encode())
        for tile in data["pages"][2:]
    ]
    full = editor_client.get(url).json()["pages"]
    full_sizes = [
        len(json.dumps(tile, ensure_ascii=False, separators=(",", ":")).encode()) for tile in full[2:]
    ]
    assert max(sizes) <= 200 and max(sizes) * 2 < min(full_sizes)
    assert "url" in full[2] and "thumb_url" in full[2] and "status_label" in full[2]  # the default stays full

    with django_assert_max_num_queries(4):
        services.book_progress(book)
        services.page_tiles(book, compact=True)

    Book.objects.filter(pk=book.pk).update(
        status=Book.Status.ERROR, error_message="تعذّر فتح الملف\nPDF broken"
    )
    data = editor_client.get(f"{url}?compact=1").json()
    assert data["error_headline"] == "تعذّر فتح الملف" and data["error_detail"] == "PDF broken"
    Book.objects.filter(pk=book.pk).update(status=Book.Status.OCR)
    assert editor_client.get(url).json()["error_headline"] == ""  # a stale message is not an error


def test_compact_tiles_keep_the_sequence_issues():
    book, pages = _numbered(["10", "11", "12", "20", "21"])
    compact = services.page_tiles(book, compact=True)
    full = services.page_tiles(book)
    assert [t["sequence_issue"] for t in compact] == [t["sequence_issue"] for t in full]
    assert any(t["sequence_issue"] for t in compact)


def test_dashboard_config_carries_status_labels_dots_and_url_templates():
    book = Book.objects.create(pk=10, title="كتاب")  # a book id with a 0 in it
    config = services.book_dashboard(book)["config"]
    assert set(config["statusLabels"]) == set(Page.Status.values) == set(config["statusDots"])
    assert config["statusLabels"]["ocr_done"] == Page.Status.OCR_DONE.label
    assert config["statusDots"]["error"] == "dot-danger"
    assert config["urls"] == {
        "page": "/books/10/guides/#sheet-__n__",
        "review": "/books/10/review/__n__/",
        "rerun": "/books/10/pages/__n__/rerun/",
        "exclude": "/books/10/pages/__n__/exclude/",
    }
    n = 7
    assert config["urls"]["page"].replace("__n__", str(n)) == services.sheet_url(10, n)
    assert config["urls"]["review"].replace("__n__", str(n)) == reverse("review:page", args=[10, n])


def test_sheets_carry_the_books_typical_line_height(django_assert_max_num_queries):
    """D30: the median detected line height of the book's included, preprocessed pages, in pixels."""
    book, pages = _book_with_pages(5)
    for page, height in zip(pages, [40, 42, 41, 90, 0], strict=True):
        Preprocess.objects.create(page=page, output_width=200, output_height=400, median_line_height=height)
    Page.objects.filter(pk=pages[3].pk).update(is_excluded=True)  # an excluded cover set in large type
    with django_assert_max_num_queries(5):
        data = services.book_sheets(book, 1, 5)
    assert data["book_line_h_px"] == 41 and data["total"] == 5
    empty, _ = _book_with_pages(2)
    assert services.book_sheets(empty, 1, 2)["book_line_h_px"] == 0


# ====================================================================== Phase 7a: «التخطيط» then «المعالجة»

GUIDES_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "guides")
# The geometry of the §3.11 fixture books (prepared image 1000 × 3000 px): a narrow running head, body
# lines, two footnote lines and the page number; rules on pages 1, 3–6, a smaller-type block on 7.
G_W, G_H = 1000, 3000
G_LINES = [
    {"x0": 400, "y0": 120, "x1": 600, "y1": 160},
    {"x0": 100, "y0": 250, "x1": 900, "y1": 290},
    {"x0": 100, "y0": 330, "x1": 900, "y1": 370},
    {"x0": 100, "y0": 2250, "x1": 900, "y1": 2290},
    {"x0": 100, "y0": 2400, "x1": 900, "y1": 2430},
    {"x0": 100, "y0": 2460, "x1": 900, "y1": 2490},
    {"x0": 466, "y0": 2922, "x1": 524, "y1": 2964},
]
G_RULES = {1: 2343, 3: 2346, 4: 2340, 5: 2343, 6: 2349}
G_BLOCKS = {7: 2380}
G_OVERRIDE_7 = {"header_cut": 0.05, "footnote_line": 0.805}


def guides_fixture(name: str) -> dict:
    with open(os.path.join(GUIDES_FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


def _normalise(value, like=None):
    """A live payload made comparable to its fixture: image versions and region ids take the fixture's."""
    if isinstance(value, dict):
        return {k: _normalise(v, like.get(k) if isinstance(like, dict) else None) for k, v in value.items()}
    if isinstance(value, list):
        likes = like if isinstance(like, list) else []
        return [_normalise(v, likes[i] if i < len(likes) else None) for i, v in enumerate(value)]
    if isinstance(value, str) and isinstance(like, str) and "?v=" in value and "?v=" in like:
        return re.sub(r"\?v=\d+", like[like.index("?v=") :], value)
    return value


def _normalise_regions(payload: dict, fixture: dict) -> dict:
    for got, want in zip(payload.get("regions") or [], fixture.get("regions") or [], strict=False):
        got["id"] = want["id"]
    return payload


def _prepare(page: Page, number: int) -> Preprocess:
    return Preprocess.objects.create(
        page=page,
        output_width=G_W,
        output_height=G_H,
        line_boxes=G_LINES,
        n_lines=len(G_LINES),
        median_line_height=40,
        footnote_rule_y=G_RULES.get(number),
        footnote_block_y=G_BLOCKS.get(number),
        page_number_box={"bbox": [466, 2922, 524, 2964], "position": "bottom"},
        display_image=f"books/25/pages/{number:04d}/display.webp",
    )


def guides_book(state: str) -> tuple[Book, list[Page]]:
    """Book 25 of the fixtures: `uploaded`, `preparing`, `layout`, `error` (all awaiting «بدء المعالجة»)
    or `started` («المعالجة» started, locked and overridden pages, regions derived)."""
    from ocr.models import Line
    from processing import services as proc

    status = {
        "uploaded": Book.Status.UPLOADED,
        "preparing": Book.Status.PROCESSING,
        "layout": Book.Status.NEEDS_GUIDES,
        "error": Book.Status.ERROR,
        "started": Book.Status.REVIEWING,
    }[state]
    book = Book.objects.create(
        pk=25,
        title="الحوليات الليبية",
        source_page_count=555,
        skip_first=186,
        skip_last=362,
        has_text_layer=False,
        status=status,
        awaits_ocr_start=state != "started",
        error_message=ALL_PAGES_FAILED_LAYOUT if state == "error" else "",
    )
    pages: list[Page] = []
    if state == "uploaded":
        return book, pages
    for n in range(1, 8):
        page = Page.objects.create(
            pk=811 + n, book=book, number=n, source_index=185 + n, width=G_W, height=G_H
        )
        pages.append(page)
        if state == "error":
            page.set_error("preprocess", "فشل تجهيز الصفحة.")
            continue
        if state == "preparing" and n > 3:
            if n == 4:
                page.set_error("preprocess", "فشل تجهيز الصفحة.")
            elif n == 7:
                Page.objects.filter(pk=page.pk).update(is_excluded=True, status=Page.Status.EXCLUDED)
            continue
        _prepare(page, n)
        Page.objects.filter(pk=page.pk).update(status=Page.Status.PREPROCESSED)
    if state == "layout":
        proc.propose_guides(book)
    if state == "started":
        LayoutGuides.objects.create(
            book=book, header_cut=0.062, footnote_line=None, page_number_zone="none", source="manual"
        )
        Page.objects.filter(pk=pages[6].pk).update(guides_override=G_OVERRIDE_7)
        for page in pages:
            page.refresh_from_db()
            proc.derive_regions(page)
        Page.objects.filter(book=book).update(status=Page.Status.OCR_DONE, text_state=Page.TextState.FINAL)
        Page.objects.filter(pk=pages[0].pk).update(status=Page.Status.REVIEWED, reviewed_at=timezone.now())
        Line.objects.create(page=pages[1], order=0, text="سطر", is_reviewed=True)
        Page.objects.filter(pk=pages[5].pk).update(is_excluded=True, status=Page.Status.EXCLUDED)
    for page in pages:
        page.refresh_from_db()
    book.refresh_from_db()
    return book, pages


# ---------------------------------------------------------------------- the §3.11 contract (fixtures)


@pytest.mark.parametrize(
    "state, book_state, view",
    [
        ("uploaded", "uploaded", None),
        ("processing", "preparing", None),
        ("needs_guides", "layout", None),
        ("error", "error", None),
        ("started_view_guides", "started", "guides"),
        ("started", "started", None),
    ],
)
def test_dashboard_config_equals_the_contract(state, book_state, view):
    book, _pages = guides_book(book_state)
    fixture = guides_fixture("dashboard_config.json")[state]
    config = services.book_dashboard(book, view)["config"]
    assert {key: config.get(key) for key in fixture} == fixture
    if state == "started":  # outside the mode the config is today's plus the re-run estimates
        assert not {"startAction", "guides", "guidesStats", "keptRange", "guidesUrls"} & set(config)


@pytest.mark.parametrize(
    "state, book_state", [("processing", "preparing"), ("needs_guides", "layout"), ("started", "started")]
)
def test_progress_payload_equals_the_contract(state, book_state):
    book, _pages = guides_book(book_state)
    fixture = guides_fixture("progress.json")[state]
    progress = services.book_progress(book)
    assert {key: progress[key] for key in fixture} == fixture
    row = next(r for r in services.books_overview() if r["book"].pk == book.pk)
    assert (row["layout_stage"], row["waiting"]) == (fixture["layout_stage"], fixture["waiting"])


@pytest.mark.parametrize(
    "case, book_state, number",
    [
        ("computed", "layout", 1),
        ("no_footnote", "layout", 2),
        ("footnote_from_type", "layout", 7),
        ("uploaded", "preparing", 5),
        ("derived", "started", 3),
        ("approved", "started", 1),
        ("review", "started", 2),
        ("derived_override_cut", "started", 7),
    ],
)
def test_sheet_guides_block_equals_the_contract(case, book_state, number):
    book, _pages = guides_book(book_state)
    fixture = guides_fixture("sheet_guides_blocks.json")[case]
    item = services.book_sheets(book, number, number, guides=True)["pages"][0]
    assert _normalise(item["guides"], fixture) == fixture


def test_sheet_guides_block_of_a_page_override_equals_the_contract():
    book, pages = guides_book("layout")
    Page.objects.filter(pk=pages[1].pk).update(guides_override={"footnote_line": 0.8})
    fixture = guides_fixture("sheet_guides_blocks.json")["override"]
    item = services.book_sheets(book, 2, 2, guides=True)["pages"][0]
    assert _normalise(item["guides"], fixture) == fixture


def test_sheets_with_guides_equal_the_contract_in_five_queries(client, editor, django_assert_max_num_queries):
    book, _pages = guides_book("layout")
    fixture = guides_fixture("sheets_guides.json")
    with django_assert_max_num_queries(5):
        data = services.book_sheets(book, 1, 2, guides=True)
    assert _normalise(data, fixture) == fixture
    client.force_login(editor)
    response = client.get(reverse("api:book_sheets", args=[book.pk]) + "?from=1&to=2&guides=1")
    assert _normalise(response.json(), fixture) == fixture
    started, _ = guides_book_replace("started")
    with django_assert_max_num_queries(5):
        services.book_sheets(started, 1, 7, guides=True)


def guides_book_replace(state: str) -> tuple[Book, list[Page]]:
    """`guides_book` after deleting the book 25 already built in the test."""
    Book.objects.filter(pk=25).delete()
    return guides_book(state)


@pytest.mark.parametrize(
    "name, book_state",
    [
        ("book_guides_layout.json", "layout"),
        ("book_guides_preparing.json", "preparing"),
        ("book_guides_started.json", "started"),
    ],
)
def test_book_guides_state_equals_the_contract(client, editor, name, book_state):
    book, _pages = guides_book(book_state)
    fixture = guides_fixture(name)
    from processing import services as proc

    assert proc.book_guides_state(book) == fixture
    client.force_login(editor)
    assert client.get(reverse("api:book_guides", args=[book.pk])).json() == fixture
    narrowed = client.get(reverse("api:book_guides", args=[book.pk]) + "?from=2&to=3").json()
    assert [p["n"] for p in narrowed["pages"]] == [2, 3] and narrowed["counts"] == fixture["counts"]


def test_book_guides_state_stays_at_four_queries_whatever_the_page_count(django_assert_max_num_queries):
    from processing import services as proc

    book, _pages = guides_book("started")
    for n in range(8, 30):
        page = Page.objects.create(book=book, number=n, source_index=185 + n, status=Page.Status.OCR_DONE)
        _prepare(page, n)
    with django_assert_max_num_queries(4):
        state = proc.book_guides_state(book)
    assert len(state["pages"]) == 29


def _post_json(client, url: str, body: dict):
    return client.post(url, json.dumps(body), content_type="application/json")


def test_book_guides_apply_and_undo_equal_the_contract(client, editor):
    book, _pages = guides_book("layout")
    cases = guides_fixture("book_guides_apply.json")
    client.force_login(editor)
    url = reverse("api:book_guides", args=[book.pk])
    with patch("books.services.run_stage") as run_stage:
        response = _post_json(client, url, cases["layout"]["request"])
        assert response.status_code == 200 and response.json() == cases["layout"]["response"]
        undo = _post_json(client, url, cases["undo"]["request"])
        assert undo.status_code == 200 and undo.json() == cases["undo"]["response"]
    run_stage.assert_not_called()  # «التخطيط»: only values are saved
    assert not Region.objects.filter(page__book=book).exists()
    assert set(book.pages.values_list("status", flat=True)) == {Page.Status.PREPROCESSED}
    guides = LayoutGuides.objects.get(book=book)
    assert (guides.source, guides.footnote_line, guides.page_number_zone) == ("auto", 0.781, "bottom")


def test_book_guides_apply_in_ocr_and_conflict_equal_the_contract(client, editor):
    book, _pages = guides_book("started")
    cases = guides_fixture("book_guides_apply.json")
    client.force_login(editor)
    url = reverse("api:book_guides", args=[book.pk])
    conflict = _post_json(client, url, cases["conflict"]["request"])
    assert conflict.status_code == 409 and conflict.json() == cases["conflict"]["response"]
    with patch("processing.services._run_stage") as run_stage:
        response = _post_json(client, url, cases["ocr"]["request"])
    assert response.status_code == 200 and response.json() == cases["ocr"]["response"]
    assert sorted(call.args[0].number for call in run_stage.call_args_list) == [3, 4, 5]
    # the approved page and the page with review work keep their regions
    for number in (1, 2):
        header = Region.objects.get(page__book=book, page__number=number, kind="running_header")
        assert header.bbox[3] == 186


@pytest.mark.parametrize("case, book_state", [("layout", "layout"), ("ocr", "started")])
def test_book_guides_preview_equals_the_contract(
    client, editor, case, book_state, django_assert_max_num_queries
):
    book, _pages = guides_book(book_state)
    fixture = guides_fixture("book_guides_preview.json")[case]
    client.force_login(editor)
    url = reverse("api:book_guides_preview", args=[book.pk])
    before = LayoutGuides.objects.filter(book=book).values().first()
    response = _post_json(client, url, fixture["request"])
    assert response.status_code == 200 and response.json() == fixture["response"]
    assert LayoutGuides.objects.filter(book=book).values().first() == before  # nothing written
    from processing import services as proc

    body = fixture["request"]
    with django_assert_max_num_queries(6):
        proc.preview_book_guides(book, body["set"], body["reset_overrides"], stage=body["stage"])


@pytest.mark.parametrize("case", ["merge", "remove_page_number", "ocr_save"])
def test_page_guides_equal_the_contract(client, editor, case):
    fixture = guides_fixture("page_guides.json")[case]
    book, _pages = guides_book("started" if case == "ocr_save" else "layout")
    client.force_login(editor)
    url = reverse("api:page_guides_override", args=[fixture["page"]])
    with patch("processing.services._run_stage") as run_stage:
        response = _post_json(client, url, fixture["request"])
    assert response.status_code == fixture["status"], response.content
    data = _normalise_regions(response.json(), fixture["response"])
    assert _normalise(data, fixture["response"]) == fixture["response"]
    assert run_stage.call_count == (1 if case == "ocr_save" else 0)


def test_page_guides_undo_equals_the_contract(client, editor):
    cases = guides_fixture("page_guides.json")
    book, _pages = guides_book("layout")
    client.force_login(editor)
    url = reverse("api:page_guides_override", args=[813])
    merged = _post_json(client, url, cases["merge"]["request"]).json()
    response = _post_json(client, url, {**merged["undo"], "stage": "layout"})
    assert cases["undo"]["request"] == {**merged["undo"], "stage": "layout"}
    fixture = cases["undo"]["response"]
    assert _normalise(response.json(), fixture) == fixture
    assert Page.objects.get(pk=813).guides_override is None


@pytest.mark.parametrize("case", ["invalid", "approved", "review", "started"])
def test_guides_errors_equal_the_contract(client, editor, case):
    fixture = guides_fixture("errors.json")[case]
    guides_book("layout" if case == "invalid" else "started")
    client.force_login(editor)
    with patch("processing.services._run_stage") as run_stage:
        response = _post_json(client, fixture["url"], fixture["request"])
    assert response.status_code == fixture["status"] and response.json() == fixture["response"]
    run_stage.assert_not_called()


def test_guides_writes_need_an_editor_and_reads_a_login(client, proofreader):
    book, _pages = guides_book("layout")
    url = reverse("api:book_guides", args=[book.pk])
    assert client.get(url).status_code == 403
    client.force_login(proofreader)
    assert client.get(url).status_code == 200
    for target in (url, reverse("api:book_guides_preview", args=[book.pk])):
        response = _post_json(client, target, {"set": {"header_cut": 0.06}, "stage": "layout"})
        assert response.status_code == 403 and "محرّر" in response.json()["detail"]
    assert LayoutGuides.objects.get(book=book).source == "auto"


def test_book_guides_reset_returns_to_the_automatic_guides(client, editor):
    book, _pages = guides_book("layout")
    client.force_login(editor)
    url = reverse("api:book_guides", args=[book.pk])
    _post_json(client, url, {"set": {"header_cut": 0.062}, "stage": "layout"})
    response = _post_json(client, url, {"reset": True, "stage": "layout"}).json()
    assert response["book"] == {
        "source": "auto",
        "header_cut": None,
        "footnote_line": None,
        "awaits_ocr_start": True,
    }
    assert response["changed"] == [1, 2, 3, 4, 5, 6, 7] and response["undo"]["book"]["source"] == "manual"
    guides = LayoutGuides.objects.get(book=book)
    assert (guides.source, guides.footnote_line, guides.page_number_zone) == ("auto", 0.781, "bottom")


def test_book_guides_reset_overrides_and_from_page_round_trip_through_undo():
    from processing import services as proc

    book, pages = guides_book("layout")
    Page.objects.filter(pk=pages[1].pk).update(guides_override={"footnote_line": 0.79, "header_cut": 0.03})
    Page.objects.filter(pk=pages[2].pk).update(guides_override={"header_cut": 0.04})
    answer = proc.apply_book_guides(
        book, {"header_cut": 0.05}, ["header_cut"], from_page=pages[1].pk, stage="layout"
    )
    assert Page.objects.get(pk=pages[1].pk).guides_override == {"footnote_line": 0.79}
    assert Page.objects.get(pk=pages[2].pk).guides_override is None
    assert answer["undo"]["overrides"] == {
        str(pages[1].pk): {"footnote_line": 0.79, "header_cut": 0.03},
        str(pages[2].pk): {"header_cut": 0.04},
    }
    proc.restore_book_guides(book, answer["undo"], stage="layout")
    assert Page.objects.get(pk=pages[1].pk).guides_override == {"footnote_line": 0.79, "header_cut": 0.03}
    assert Page.objects.get(pk=pages[2].pk).guides_override == {"header_cut": 0.04}
    guides = LayoutGuides.objects.get(book=book)
    assert (guides.source, guides.header_cut, guides.footnote_line) == ("auto", None, 0.781)


# ---------------------------------------------------------------------- the pause (D64)


def test_create_book_sets_the_flag_and_objects_create_does_not():
    assert make_book(make_scan_pdf(1)).awaits_ocr_start is True
    assert Book.objects.create(title="قديم").awaits_ocr_start is False


def _paused_book(statuses: list[str], status=Book.Status.PROCESSING) -> Book:
    book = Book.objects.create(title="ك", status=status, awaits_ocr_start=True)
    for n, page_status in enumerate(statuses, start=1):
        Page.objects.create(book=book, number=n, source_index=n - 1, status=page_status)
    return book


def test_refresh_status_in_the_layout_stage():
    ps = Page.Status
    book = _paused_book([ps.PREPROCESSED, ps.UPLOADED])
    assert book.refresh_status() == Book.Status.PROCESSING
    book = _paused_book([ps.PREPROCESSED, ps.ERROR])
    assert book.refresh_status() == Book.Status.NEEDS_GUIDES and book.error_message == ""
    book = _paused_book([ps.ERROR, ps.ERROR])
    assert book.refresh_status() == Book.Status.ERROR and book.error_message == ALL_PAGES_FAILED_LAYOUT
    # excluded pages are ignored; an error it owns is re-derived (both texts)
    Page.objects.filter(book=book, number=1).update(status=ps.PREPROCESSED)
    Page.objects.filter(book=book, number=2).update(is_excluded=True, status=ps.EXCLUDED)
    assert book.refresh_status() == Book.Status.NEEDS_GUIDES and book.error_message == ""
    old = _paused_book([ps.PREPROCESSED], status=Book.Status.ERROR)
    Book.objects.filter(pk=old.pk).update(error_message=ALL_PAGES_FAILED)
    old.refresh_from_db()
    assert old.refresh_status() == Book.Status.NEEDS_GUIDES
    ingest = _paused_book([ps.PREPROCESSED], status=Book.Status.ERROR)
    Book.objects.filter(pk=ingest.pk).update(error_message="تعذّر استخراج الصفحات")
    ingest.refresh_from_db()
    assert ingest.refresh_status() == Book.Status.ERROR  # an ingest error is left alone
    # with the flag False a prepared-only book is today's `processing`
    today = Book.objects.create(title="ك", status=Book.Status.PROCESSING)
    Page.objects.create(book=today, number=1, source_index=0, status=ps.PREPROCESSED)
    assert today.refresh_status() == Book.Status.PROCESSING


def test_ingest_passes_continue_ocr_true_for_books_that_do_not_wait():
    book = make_book(make_scan_pdf(1))
    Book.objects.filter(pk=book.pk).update(awaits_ocr_start=False, status=Book.Status.PROCESSING)
    with (
        patch("books.tasks.group"),
        patch("books.tasks.chord") as chord,
        patch("processing.tasks.preprocess_page"),
    ):
        tasks.ingest_book_task(book.pk)
    chord.return_value.assert_called_once_with(tasks.after_preprocess.s(book.pk, continue_ocr=True))


def test_after_preprocess_pauses_continues_and_leaves_a_started_book_alone():
    ps = Page.Status
    book = _paused_book([ps.PREPROCESSED, ps.PREPROCESSED])
    with patch("books.services.run_stage") as run_stage:
        tasks.after_preprocess([], book.pk, continue_ocr=False)
    run_stage.assert_not_called()
    book.refresh_from_db()
    assert book.status == Book.Status.NEEDS_GUIDES and LayoutGuides.objects.filter(book=book).exists()

    # a chord queued before 7a (no kwarg) decides by the live flag
    legacy = _paused_book([ps.PREPROCESSED])
    with patch("books.services.run_stage") as run_stage:
        tasks.after_preprocess([], legacy.pk)
    run_stage.assert_not_called()
    today = Book.objects.create(title="ك", status=Book.Status.PROCESSING)
    Page.objects.create(book=today, number=1, source_index=0, status=ps.PREPROCESSED)
    with patch("books.services.run_stage") as run_stage:
        tasks.after_preprocess([], today.pk)
    assert run_stage.call_count == 1 and run_stage.call_args.args[1] == "layout"

    # continue_ocr=True: today's path
    go = _paused_book([ps.PREPROCESSED])
    with patch("books.services.run_stage") as run_stage:
        tasks.after_preprocess([], go.pk, continue_ocr=True)
    assert run_stage.call_count == 1

    # the owner started while the chord ran: the callback leaves the status alone
    started = _paused_book([ps.PREPROCESSED])
    Book.objects.filter(pk=started.pk).update(awaits_ocr_start=False, status=Book.Status.OCR)
    with patch("books.services.run_stage") as run_stage:
        tasks.after_preprocess([], started.pk, continue_ocr=False)
    run_stage.assert_not_called()
    started.refresh_from_db()
    assert started.status == Book.Status.OCR
    # every page failed: the book is in error with the layout message
    failed = _paused_book([ps.ERROR])
    tasks.after_preprocess([], failed.pk, continue_ocr=False)
    failed.refresh_from_db()
    assert failed.status == Book.Status.ERROR and failed.error_message == ALL_PAGES_FAILED_LAYOUT
    assert tasks.after_preprocess([], 999999, continue_ocr=False) == 999999  # deleted meanwhile


def test_start_ocr_claims_once_and_enqueues_the_layout_task():
    ps = Page.Status
    book = _paused_book([ps.PREPROCESSED, ps.PREPROCESSED], status=Book.Status.NEEDS_GUIDES)
    with patch("books.tasks.start_ocr_task.delay") as delay:
        services.start_ocr(book)
        delay.assert_called_once_with(book.pk)
        assert (book.status, book.awaits_ocr_start) == (Book.Status.OCR, False)
        with pytest.raises(ValueError, match="بدأت المعالجة بالفعل"):
            services.start_ocr(Book.objects.get(pk=book.pk))
        assert delay.call_count == 1

    preparing = _paused_book([ps.PREPROCESSED, ps.UPLOADED])
    with pytest.raises(ValueError, match="لم يكتمل التخطيط بعد"):
        services.start_ocr(preparing)
    nothing = _paused_book([ps.ERROR], status=Book.Status.NEEDS_GUIDES)
    with pytest.raises(ValueError, match="لا صفحات جاهزة للمعالجة"):
        services.start_ocr(nothing)
    excluded = _paused_book([ps.PREPROCESSED], status=Book.Status.NEEDS_GUIDES)
    Page.objects.filter(book=excluded).update(is_excluded=True)
    with pytest.raises(ValueError, match="لا صفحات جاهزة للمعالجة"):
        services.start_ocr(excluded)


def test_start_ocr_reverts_the_claim_when_the_broker_refuses():
    book = _paused_book([Page.Status.PREPROCESSED], status=Book.Status.NEEDS_GUIDES)
    with (
        patch("books.tasks.start_ocr_task.delay", side_effect=ConnectionError("redis down")),
        pytest.raises(ValueError, match="تعذّر إرسال العمل إلى العامل الخلفي"),
    ):
        services.start_ocr(book)
    book.refresh_from_db()
    assert (book.status, book.awaits_ocr_start) == (Book.Status.NEEDS_GUIDES, True)


def test_start_ocr_task_enqueues_layout_chains_for_the_prepared_pages():
    ps = Page.Status
    book = _paused_book([ps.PREPROCESSED, ps.ERROR, ps.PREPROCESSED], status=Book.Status.NEEDS_GUIDES)
    Book.objects.filter(pk=book.pk).update(awaits_ocr_start=False, status=Book.Status.OCR)
    with patch("books.services.run_stage") as run_stage:
        tasks.start_ocr_task(book.pk)
    assert [(c.args[0].number, c.args[1]) for c in run_stage.call_args_list] == [(1, "layout"), (3, "layout")]
    assert tasks.start_ocr_task(999999) == 999999


def test_start_ocr_view_redirects_with_its_message(editor_client, proofreader):
    from django.test import Client

    book = _paused_book([Page.Status.PREPROCESSED], status=Book.Status.NEEDS_GUIDES)
    url = reverse("books:start_ocr", args=[book.pk])
    assert url == f"/books/{book.pk}/start-ocr/"
    reader = Client()
    reader.force_login(proofreader)
    assert reader.post(url).status_code == 403
    with patch("books.tasks.start_ocr_task.delay"):
        response = editor_client.post(url)
    assert response.status_code == 302 and response["Location"] == f"/books/{book.pk}/"
    messages = [str(m) for m in response.wsgi_request._messages]
    assert messages == ["بدأت المعالجة. تُحدَّث هذه الصفحة تلقائيًا أثناء العمل."]
    again = editor_client.post(url)
    assert [str(m) for m in again.wsgi_request._messages][-1] == "بدأت المعالجة بالفعل."
    assert editor_client.get(url).status_code == 405


def test_the_gate_refuses_every_stage_but_preprocess_while_the_book_waits():
    ps = Page.Status
    book = _paused_book([ps.PREPROCESSED, ps.PREPROCESSED], status=Book.Status.NEEDS_GUIDES)
    page = book.pages.get(number=1)
    for stage in ("layout", "ocr", "ocr_fast", "ocr_full"):
        with pytest.raises(ValueError, match="لم تبدأ المعالجة بعد"):
            services.run_stage(page, stage)
        with pytest.raises(ValueError, match="لم تبدأ المعالجة بعد"):
            services.validate_rerun(book, stage)
    with patch("books.services.chain") as chain:
        services.run_stage(page, "preprocess")
    assert [sig.task for sig in chain.call_args.args] == ["processing.tasks.preprocess_page"]
    assert chain.call_args.args[0].kwargs == {"run": Page.objects.get(pk=page.pk).run_token, "last": True}
    with pytest.raises(ValueError, match="لم يكتمل التخطيط"):  # page 1 is being prepared again
        services.validate_rerun(book, "preprocess")
    _end_runs(book)  # page 1 prepared
    Page.objects.filter(pk=page.pk).update(status=ps.PREPROCESSED)
    book.refresh_status()
    with patch("books.services.chain") as chain:
        assert services.rerun_book(book, "preprocess") == 2
    assert all(len(call.args) == 1 for call in chain.call_args_list)
    book.refresh_from_db()
    assert book.status == Book.Status.PROCESSING and book.awaits_ocr_start
    assert (
        services._stage_signatures(1, "layout", layout_stage=True)[0].task
        == "processing.tasks.preprocess_page"
    )


def test_toggle_exclude_in_the_layout_stage_queues_nothing_for_ocr(editor_client):
    ps = Page.Status
    book = _paused_book([ps.PREPROCESSED, ps.EXCLUDED, ps.EXCLUDED], status=Book.Status.NEEDS_GUIDES)
    prepared, unprepared = book.pages.get(number=2), book.pages.get(number=3)
    Page.objects.filter(pk__in=[prepared.pk, unprepared.pk]).update(is_excluded=True)
    Preprocess.objects.create(page=prepared, output_width=10, output_height=10)
    with patch("books.services.chain") as chain:
        response = editor_client.post(reverse("books:toggle_exclude", args=[book.pk, 2]))
        editor_client.post(reverse("books:toggle_exclude", args=[book.pk, 3]))
    # only the unprepared page is queued, preprocessing alone, under a run claim (item 29)
    queued = [[sig.task for sig in call.args] for call in chain.call_args_list]
    assert queued == [["processing.tasks.preprocess_page"]]
    assert chain.call_args.args[0].args == (unprepared.pk,)
    assert Page.objects.get(pk=unprepared.pk).run_token and not Page.objects.get(pk=prepared.pk).run_token
    assert Page.objects.get(pk=prepared.pk).status == ps.PREPROCESSED
    message = list(response.wsgi_request._messages)[0]
    assert str(message) == "أُعيدت الصفحة 2 إلى الكتاب."
    assert f"undo:{reverse('books:toggle_exclude', args=[book.pk, 2])}" in message.extra_tags
    book.refresh_from_db()
    assert book.status == Book.Status.PROCESSING  # page 3 is being prepared


def test_start_processing_extracts_refuses_after_layout_and_reverts_on_a_broker_failure():
    book = make_book(make_scan_pdf(1))
    with patch("books.tasks.ingest_book_task.delay", side_effect=ConnectionError("down")):
        with pytest.raises(ValueError, match="تعذّر إرسال العمل"):
            services.start_processing(book)
    book.refresh_from_db()
    assert book.status == Book.Status.UPLOADED and book.error_message == ""
    Book.objects.filter(pk=book.pk).update(status=Book.Status.ERROR, error_message="قديم")
    book.refresh_from_db()
    with patch("books.tasks.ingest_book_task.delay", side_effect=ConnectionError("down")):
        with pytest.raises(ValueError):
            services.start_processing(book)
    book.refresh_from_db()
    assert (book.status, book.error_message) == (Book.Status.ERROR, "قديم")
    with patch("books.tasks.ingest_book_task.delay") as delay:
        services.start_processing(book)
    delay.assert_called_once_with(book.pk)
    Book.objects.filter(pk=book.pk).update(status=Book.Status.NEEDS_GUIDES)
    book.refresh_from_db()
    with pytest.raises(ValueError) as exc:
        services.start_processing(book)
    assert str(exc.value) == "اكتمل التخطيط؛ اضغط «بدء المعالجة»، أو أعد تجهيز الصفحات من القائمة «⋯»."


@pytest.mark.parametrize(
    "options, pages, expected",
    [
        (
            {"skip_first": "1", "skip_last": "1"},
            4,
            "أُنشئ الكتاب «كتاب الاختبار»، وتُستخرج الآن الصفحات 2–3 من 4 صفحات في الملف (صفحتان).",
        ),
        ({}, 3, "أُنشئ الكتاب «كتاب الاختبار»، وتُستخرج الآن صفحات الملف كلها (3 صفحات)."),
        (
            {"skip_first": "1", "pages_per_sheet": "2"},
            3,
            "أُنشئ الكتاب «كتاب الاختبار»، وتُستخرج الآن الصفحات 2–3 من 3 صفحات في الملف (4 صفحات في الكتاب).",
        ),
    ],
)
def test_book_create_extracts_at_once_and_names_the_range(editor_client, options, pages, expected):
    upload = SimpleUploadedFile("scan.pdf", make_scan_pdf(pages), content_type="application/pdf")
    data = {
        "title": "كتاب الاختبار",
        "source_pdf": upload,
        "skip_first": "0",
        "skip_last": "0",
        "pages_per_sheet": "1",
        "split_ratio": "0.5",
        "use_text_layer": "on",
        **options,
    }
    with patch("books.tasks.ingest_book_task.delay") as delay:
        response = editor_client.post(reverse("books:create"), data)
    book = Book.objects.get()
    assert response.status_code == 302 and response["Location"] == f"/books/{book.pk}/", response.content
    delay.assert_called_once_with(book.pk)
    assert [str(m) for m in response.wsgi_request._messages] == [expected]
    assert (book.status, book.awaits_ocr_start) == (Book.Status.PROCESSING, True)


def test_book_create_keeps_the_book_uploaded_when_the_extraction_cannot_be_queued(editor_client):
    upload = SimpleUploadedFile("scan.pdf", make_scan_pdf(1), content_type="application/pdf")
    data = {"title": "ك", "source_pdf": upload, "skip_first": "0", "skip_last": "0", "pages_per_sheet": "1"}
    with patch("books.tasks.ingest_book_task.delay", side_effect=ConnectionError("down")):
        response = editor_client.post(reverse("books:create"), data)
    book = Book.objects.get()
    assert response.status_code == 302 and book.status == Book.Status.UPLOADED
    assert [str(m) for m in response.wsgi_request._messages] == [services.BROKER_ERROR]


def test_kept_range():
    book = Book(title="ك", source_page_count=555, skip_first=186, skip_last=362)
    assert services.kept_range(book) == {
        "source_pages": 555,
        "first": 187,
        "last": 193,
        "sheets": 7,
        "pages": 7,
        "text": "الصفحات 187–193 من 555",
    }
    two = Book(title="ك", source_page_count=52, skip_first=2, skip_last=2, pages_per_sheet=2)
    assert services.kept_range(two)["text"] == "الصفحات 3–50 من 52، وفي كل منها صفحتان"
    assert services.kept_range(two)["pages"] == 96
    assert services.kept_range(book, source_pages=190)["last"] is None  # nothing left
    single = Book(title="ك", source_page_count=5, skip_first=4)
    assert services.kept_range(single)["text"] == "الصفحة 5 من 5"


def test_rerun_estimate_uses_the_median_qari_time_and_falls_back():
    from ocr.models import OcrRun
    from ocr.services import engine_names

    book, pages = _book_with_pages(4, status=Page.Status.OCR_DONE)
    Page.objects.filter(pk=pages[0].pk).update(status=Page.Status.REVIEWED)
    assert services.rerun_estimate(book, "ocr") == {"pages": 3, "kept": 1, "minutes": 1}  # 3 × 20 s fallback
    assert services.rerun_estimate(book, "ocr_fast")["minutes"] is None
    primary, secondary, fast = engine_names()
    for page, seconds in zip(pages[1:], (30, 60, 90), strict=True):
        OcrRun.objects.create(page=page, engine_name=primary, duration_ms=1000)  # older: ignored
        OcrRun.objects.create(page=page, engine_name=primary, duration_ms=seconds * 500)
        OcrRun.objects.create(page=page, engine_name=secondary, duration_ms=seconds * 500)
        OcrRun.objects.create(page=page, engine_name=fast, duration_ms=10_000)  # Tesseract: not counted
    assert services.qari_seconds(book) == 60
    assert services.rerun_estimate(book, "layout") == {"pages": 3, "kept": 1, "minutes": 3}
    Book.objects.filter(pk=book.pk).update(awaits_ocr_start=True)
    book.refresh_from_db()
    assert services.rerun_estimate(book, "preprocess")["minutes"] is None


def test_delete_book_removes_rows_and_the_folder_after_commit(
    editor_client, proofreader, django_capture_on_commit_callbacks
):
    from django.conf import settings
    from django.test import Client

    book, _pages = guides_book("started")
    folder = os.path.join(settings.MEDIA_ROOT, "books", str(book.pk))
    os.makedirs(os.path.join(folder, "pages", "0001"), exist_ok=True)
    with open(os.path.join(folder, "pages", "0001", "display.webp"), "wb") as handle:
        handle.write(b"x")
    url = reverse("books:delete", args=[book.pk])
    reader = Client()
    reader.force_login(proofreader)
    assert reader.post(url).status_code == 403
    assert editor_client.get(url).status_code == 405
    with django_capture_on_commit_callbacks(execute=True):
        response = editor_client.post(url)
    assert response.status_code == 302 and response["Location"] == reverse("books:list")
    assert [str(m) for m in response.wsgi_request._messages] == ["حُذف الكتاب «الحوليات الليبية»."]
    assert not Book.objects.filter(pk=book.pk).exists()
    assert (
        not Page.objects.filter(book_id=25).exists() and not Region.objects.filter(page__book_id=25).exists()
    )
    assert not os.path.exists(folder)


def test_delete_book_keeps_the_folder_when_the_transaction_rolls_back(settings):
    from django.db import transaction

    book = Book.objects.create(title="ك")
    book_id = book.pk
    folder = os.path.join(settings.MEDIA_ROOT, "books", str(book_id))
    os.makedirs(folder, exist_ok=True)
    with pytest.raises(RuntimeError), transaction.atomic():
        services.delete_book(book)
        raise RuntimeError("rolled back")
    assert os.path.exists(folder) and Book.objects.filter(pk=book_id).exists()


def test_dashboard_offers_only_the_preprocess_rerun_while_the_book_waits():
    book, _ = guides_book("layout")
    context = services.book_dashboard(book)
    assert context["rerun_stages"] == [{"value": "preprocess", "label": "تجهيز الصفحات"}]
    assert context["guides_mode"] and context["start_action"] == "startOcr"
    assert context["kept_range"]["text"] == "الصفحات 187–193 من 555"
    started, _ = guides_book_replace("started")
    context = services.book_dashboard(started)
    assert [s["value"] for s in context["rerun_stages"]] == list(services.STAGES)
    assert not context["guides_mode"] and context["start_action"] == ""
    assert services.book_dashboard(started, "guides")["guides_mode"] is True
    assert context["guides_url"] == f"/books/{started.pk}/guides/"


def test_the_guides_address_opens_the_mode_and_the_old_query_redirects_there(editor_client):
    # D84: the «التخطيط» mode has its own address; `?view=guides` (old links) goes there for good
    book, _ = guides_book("started")
    url = reverse("books:guides", args=[book.pk])
    with patch("books.services.book_dashboard", wraps=services.book_dashboard) as dashboard:
        assert editor_client.get(url).status_code == 200
    assert dashboard.call_args.args[1] == "guides"
    response = editor_client.get(reverse("books:detail", args=[book.pk]) + "?view=guides")
    assert response.status_code == 301 and response["Location"] == url
    Book.objects.filter(pk=book.pk).update(
        awaits_ocr_start=True
    )  # before «بدء المعالجة» the dashboard is the mode
    response = editor_client.get(url)
    assert response.status_code == 302 and response["Location"] == reverse("books:detail", args=[book.pk])


def test_labels_of_the_split():
    assert Book.Status.PROCESSING.label == "قيد التخطيط"
    assert Book.Status.NEEDS_GUIDES.label == "تم التخطيط"
    assert Book.Status.OCR.label == "قيد المعالجة"
    assert Page.Status.PREPROCESSED.label == "مُجهَّزة"
    assert Page.Status.LAYOUT_DONE.label == "بانتظار التعرّف"
    assert services.STAGE_LABELS["preprocess"] == "تجهيز الصفحات"
    assert services.STAGE_LABELS["layout"] == "تحديد المناطق"
    assert Region.Source.GUIDES.label == "من التخطيط"
    assert LayoutGuides._meta.verbose_name == "التخطيط العام"
    assert Page._meta.get_field("guides_override").verbose_name == "تخطيط خاص بالصفحة"
    assert Book._meta.get_field("awaits_ocr_start").verbose_name == "بانتظار «بدء المعالجة»"
    book = Book.objects.create(title="ك")
    stages = {s["key"]: s["label"] for s in services.book_dashboard(book)["stages"]}
    assert stages["preprocessed"] == "مُجهَّزة" and stages["layout_done"] == "بانتظار التعرّف"


def test_smoke_pipeline_layout_only_stops_at_the_pause(tmp_path):
    from django.core.management import call_command

    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(make_scan_pdf(2))
    out = io.StringIO()
    with patch("books.tasks.start_ocr_task.delay") as start:
        call_command("smoke_pipeline", str(pdf), "--layout-only", stdout=out)
    start.assert_not_called()
    book = Book.objects.get()
    assert (book.status, book.awaits_ocr_start) == (Book.Status.NEEDS_GUIDES, True)
    assert set(book.pages.values_list("status", flat=True)) == {Page.Status.PREPROCESSED}
    text = out.getvalue()
    assert f"book {book.pk}: needs_guides" in text and "   1  preprocessed" in text and " b " in text


def test_the_mode_markup_shows_only_in_the_mode(editor_client):
    # §3.12: the server chooses the mode; outside it the dashboard renders today's markup
    book, _ = guides_book("layout")
    url = reverse("books:detail", args=[book.pk])
    body = editor_client.get(url).content.decode()
    assert "bk-dashboard is-guides" in body and 'x-data="bookGuides()"' in body
    assert 'id="sheet-guides"' in body and reverse("books:start_ocr", args=[book.pk]) in body
    started, _ = guides_book_replace("started")
    url = reverse("books:detail", args=[started.pk])
    body = editor_client.get(url).content.decode()
    assert "is-guides" not in body and "bookGuides(" not in body and 'id="sheet-guides"' not in body
    assert f'href="{url}guides/"' in body  # «⋯» «التخطيط»
    body = editor_client.get(url + "guides/").content.decode()
    assert "bk-dashboard is-guides" in body and 'x-data="bookGuides()"' in body


# ---------------------------------------------------------------- 7b: honest page state (D73)


def test_approved_pages_leave_the_attention_list_unless_their_numbering_is_off():
    book, pages = _numbered(["40", "41", "43", "44", "45"])
    for page, flags in zip(
        pages, (["single_reader"], ["missing_text"], [], ["ocr_fallback"], []), strict=True
    ):
        page.attention_flags = flags
        page.save()
    Page.objects.filter(pk__in=[pages[0].pk, pages[2].pk]).update(status=Page.Status.REVIEWED)
    Page.objects.filter(pk=pages[3].pk).update(status=Page.Status.ASSEMBLED)
    items = services.attention_pages(book)
    # page 1 approved (gone), page 2 open (stays), page 3 approved but out of sequence (stays), page 4 gone
    assert [item["page"].number for item in items] == [2, 3]
    assert [flag["label"] for flag in items[0]["flags"]] == ["نص قد يكون ناقصًا"]
    Page.objects.filter(pk=pages[0].pk).update(status=Page.Status.ERROR)
    assert [item["page"].number for item in services.attention_pages(book)] == [1, 2, 3]


def test_the_tile_says_how_the_page_was_read():
    book, pages = _book_with_pages(4, status=Page.Status.OCR_DONE)
    for page, reading in zip(
        pages, ({"readers": "two"}, {"readers": "one"}, {"readers": "tesseract"}, {}), strict=True
    ):
        page.reading = reading
        page.save()
    full = [tile["readers"] for tile in services.page_tiles(book)]
    assert full == ["two", "one", "tesseract", ""]
    compact = [tile.get("readers") for tile in services.page_tiles(book, compact=True)]
    assert compact == [None, "one", "tesseract", None]  # the compact tile carries only a weak reading
    labels = services.page_tile(pages[1])["flag_labels"]
    assert labels == []
    pages[1].attention_flags = ["single_reader"]
    assert services.page_tile(pages[1])["flag_labels"] == ["قراءة واحدة"]


# ====================================================================== 7c: the stage bar (D76), the tile

CONTRACT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "editor", "fixtures", "contract")
STAGE_BOOK = 41


def contract_fixture(name: str):
    with open(os.path.join(CONTRACT_DIR, name), encoding="utf-8") as handle:
        return json.load(handle)


def write_contract(name: str, content) -> None:
    """Rewrite a 7c contract file from live answers (`NASSAKH_WRITE_CONTRACT_FIXTURES=1`)."""
    if os.environ.get("NASSAKH_WRITE_CONTRACT_FIXTURES"):
        with open(os.path.join(CONTRACT_DIR, name), "w", encoding="utf-8") as handle:
            handle.write(json.dumps(content, ensure_ascii=False, indent=1) + "\n")


def stage_book(statuses=(), *, awaits=False, status=Book.Status.REVIEWING, error_from="ocr") -> Book:
    """Book 41 «كتاب المراحل» with one page per status (`error` pages failed at `error_from`)."""
    from assembly.models import AssemblyRun
    from editor.models import Manuscript
    from publishing.models import Export, LiveLayout

    for model in (Export, LiveLayout, Manuscript, AssemblyRun):
        model.objects.filter(book_id=STAGE_BOOK).delete()
    Book.objects.filter(pk=STAGE_BOOK).delete()
    book = Book.objects.create(pk=STAGE_BOOK, title="كتاب المراحل", status=status, awaits_ocr_start=awaits)
    for number, page_status in enumerate(statuses, start=1):
        Page.objects.create(
            book=book,
            number=number,
            source_index=number - 1,
            status=page_status,
            error_from=error_from if page_status == Page.Status.ERROR else "",
        )
    return book


def with_manuscript(book: Book, *, edited=False, drift=(), approvals=(), version=3, chapters=2):
    """A manuscript built from the book's pages as they are, `drift` pages with another signature and
    `approvals` pages with the other reviewed flag."""
    from assembly.models import AssemblyRun
    from assembly.services import page_signatures
    from editor.models import Manuscript

    pages = list(book.pages.order_by("number"))
    sigs = page_signatures([page.pk for page in pages])
    included = {}
    for page in pages:
        reviewed = page.status in (Page.Status.REVIEWED, Page.Status.ASSEMBLED)
        included[str(page.pk)] = {
            "number": page.number,
            "reviewed": (not reviewed) if page.number in approvals else reviewed,
            "sig": "9:changed" if page.number in drift else sigs[page.pk],
        }
    run = AssemblyRun.objects.create(
        book=book, status="done", included=included, stats={"chapters": chapters}, finished_at=timezone.now()
    )
    return Manuscript.objects.create(
        book=book,
        document={"type": "doc", "content": []},
        version=version,
        origin="editor" if edited else "assembly",
        run=run,
    )


R8 = [Page.Status.REVIEWED] * 8


def _exports(book, *rows):
    from publishing.models import Export

    now = timezone.now()
    for fmt, status, version, hours in rows:
        Export.objects.create(
            book=book,
            format=fmt,
            status=status,
            manuscript_version=version,
            finished_at=now - timezone.timedelta(hours=hours) if status in ("done", "error") else None,
            error="تعذّر إخراج الملف. أعد المحاولة، وإن تكرّر الخطأ فراجع سجل الخادم.\nValueError: x"
            if status == "error"
            else "",
        )


def _layout(book, pages=84):
    from publishing.models import LiveLayout

    LiveLayout.objects.create(book=book, revision=1, page_count=pages)


def _run(book, status):
    from assembly.models import AssemblyRun
    from assembly.services import RUN_ERROR

    AssemblyRun.objects.create(
        book=book,
        status=status,
        error=f"{RUN_ERROR}\nX" if status == "error" else "",
    )


def _stage_cases():
    """Every state of every step: `{step: {state: builder}}` (each builder leaves book 41 in that state)."""
    PRE, UP, LAY, READ, ERR = (
        Page.Status.PREPROCESSED,
        Page.Status.UPLOADED,
        Page.Status.LAYOUT_DONE,
        Page.Status.OCR_DONE,
        Page.Status.ERROR,
    )
    return {
        "pages": {
            "todo": lambda: stage_book(status=Book.Status.UPLOADED, awaits=True),
            "extracting": lambda: stage_book(status=Book.Status.PROCESSING, awaits=True),
            "active": lambda: stage_book(
                [PRE, PRE, PRE, UP, UP, UP, UP], awaits=True, status=Book.Status.PROCESSING
            ),
            "done": lambda: stage_book([PRE] * 7, awaits=True, status=Book.Status.NEEDS_GUIDES),
            "done_started": lambda: stage_book([READ] * 8),
            "attention": lambda: stage_book(
                [PRE] * 6 + [ERR], awaits=True, status=Book.Status.PROCESSING, error_from="preprocess"
            ),
        },
        "ocr": {
            "todo": lambda: stage_book([PRE] * 7, awaits=True, status=Book.Status.NEEDS_GUIDES),
            "active": lambda: stage_book([READ] * 3 + [LAY] * 5, status=Book.Status.OCR),
            "done": lambda: stage_book([READ] * 8),
            "attention": lambda: stage_book([READ] * 7 + [ERR]),
        },
        "review": {
            "blocked": lambda: stage_book([LAY] * 8, status=Book.Status.OCR),
            "todo": lambda: stage_book([READ] * 8),
            "active": lambda: stage_book(R8[:6] + [READ, READ]),
            "done": lambda: stage_book(R8),
        },
        "manuscript": {
            "todo": lambda: stage_book(R8),
            "active": lambda: _run(stage_book(R8), "queued"),
            "done": lambda: with_manuscript(stage_book(R8)),
            "done_edited": lambda: with_manuscript(stage_book(R8), edited=True),
            "stale": lambda: with_manuscript(stage_book(R8), drift=(3, 4)),
            "stale_approvals": lambda: with_manuscript(stage_book(R8), approvals=(3, 4)),
            "attention": lambda: (
                with_manuscript(stage_book(R8)),
                _run(Book.objects.get(pk=STAGE_BOOK), "error"),
            ),
        },
        "book": {
            "blocked": lambda: stage_book(R8),
            "todo": lambda: with_manuscript(stage_book(R8)),
            "done": lambda: _layout(with_manuscript(stage_book(R8)).book),
            "stale": lambda: _layout(with_manuscript(stage_book(R8), edited=True, drift=(3, 4)).book),
        },
        "export": {
            "blocked": lambda: stage_book(R8),
            "todo": lambda: with_manuscript(stage_book(R8)),
            "active": lambda: _exports(with_manuscript(stage_book(R8)).book, ("docx", "running", None, 0)),
            "done": lambda: _exports(
                with_manuscript(stage_book(R8)).book, ("print_pdf", "done", 3, 3), ("docx", "done", 3, 2)
            ),
            "stale": lambda: _exports(with_manuscript(stage_book(R8)).book, ("docx", "done", 2, 5)),
            "attention": lambda: _exports(
                with_manuscript(stage_book(R8)).book, ("docx", "done", 3, 5), ("docx", "error", 3, 1)
            ),
        },
    }


def _step_of(key: str) -> dict:
    book = Book.objects.get(pk=STAGE_BOOK)
    return next(step for step in services.book_stages(book) if step["key"] == key)


def stages_contract(client) -> dict:
    steps: dict = {}
    for key, states in _stage_cases().items():
        steps[key] = {}
        for state, build in states.items():
            build()
            steps[key][state] = _step_of(key)
    responses = {}

    def answer(label, current):
        response = client.get(reverse("api:book_stages", args=[STAGE_BOOK]) + f"?current={current}")
        assert response.status_code == 200
        responses[label] = response.json()

    cases = _stage_cases()
    cases["pages"]["active"]()
    answer("GET /api/books/41/stages/?current=pages (the «التخطيط» mode, preparing)", "pages")
    cases["review"]["active"]()
    answer("GET /api/books/41/stages/?current=review (reviewing)", "review")
    book = with_manuscript(stage_book(R8), edited=True, drift=(3, 4)).book
    _layout(book)
    _exports(book, ("docx", "done", 2, 5))
    answer("GET /api/books/41/stages/?current=book (edited, with drift)", "book")
    cases["export"]["done"]()
    _layout(Book.objects.get(pk=STAGE_BOOK))
    answer("GET /api/books/41/stages/?current=export (exported)", "export")
    return {"steps": steps, "responses": responses}


def test_stage_payloads_equal_the_contract(editor_client):
    contract = stages_contract(editor_client)
    write_contract("stages.json", contract)
    assert contract == contract_fixture("stages.json")
    index = contract_fixture("index.json")
    assert "stages.json" in index["files"] and "tile.json" in index["files"]


def test_every_step_has_its_states_and_blocked_steps_have_no_link():
    seen = {}
    for key, states in _stage_cases().items():
        for state, build in states.items():
            build()
            step = _step_of(key)
            assert step["state"] == ("active" if state == "extracting" else state.split("_")[0]), (key, state)
            assert (step["url"] is None) == (step["state"] == "blocked"), (key, state)
            assert step["state_label"] == services.STATE_WORDS[step["state"]]
            seen.setdefault(key, set()).add(step["state"])
    assert seen == {
        "pages": {"todo", "active", "done", "attention"},
        "ocr": {"todo", "active", "done", "attention"},
        "review": {"blocked", "todo", "active", "done"},
        "manuscript": {"todo", "active", "done", "stale", "attention"},
        "book": {"blocked", "todo", "done", "stale"},
        "export": {"blocked", "todo", "active", "done", "stale"} | {"attention"},
    }


def test_the_stage_bar_marks_the_current_step_and_links_the_guides_view_after_the_start(editor_client):
    stage_book([Page.Status.OCR_DONE] * 8)
    book = Book.objects.get(pk=STAGE_BOOK)
    steps = services.book_stages(book, "review")
    assert [s["key"] for s in steps] == list(services.STAGE_KEYS)
    assert [s["label"] for s in steps] == ["التخطيط", "المعالجة", "المراجعة", "المخطوطة", "الكتاب", "الإخراج"]
    assert [s["current"] for s in steps] == [False, False, True, False, False, False]
    assert steps[0]["url"] == f"/books/{STAGE_BOOK}/guides/"
    assert steps[2]["url"] == f"/books/{STAGE_BOOK}/review/next/"
    unknown = editor_client.get(reverse("api:book_stages", args=[STAGE_BOOK]) + "?current=zz").json()
    assert unknown["current"] is None and not any(step["current"] for step in unknown["steps"])
    assert editor_client.get(reverse("api:book_stages", args=[10**6])).status_code == 404


@pytest.mark.parametrize("pages", [3, 30])
def test_the_stage_bar_costs_at_most_eight_queries(pages, django_assert_max_num_queries):
    """§8.4 gate 3: the stage bar's data in ≤ 8 queries, whatever the book's size."""
    book = stage_book([Page.Status.REVIEWED] * pages)
    with_manuscript(book, edited=True, drift=(2,))
    _exports(book, ("docx", "done", 3, 2), ("epub", "done", 2, 4))
    book = Book.objects.get(pk=STAGE_BOOK)
    with django_assert_max_num_queries(8):
        steps = services.book_stages(book, "book")
    assert [s["state"] for s in steps] == ["done", "done", "done", "done", "stale", "done"]
    stage_book([Page.Status.REVIEWED] * pages)
    with_manuscript(Book.objects.get(pk=STAGE_BOOK))
    book = Book.objects.get(pk=STAGE_BOOK)
    with django_assert_max_num_queries(8):  # no live layout: the newest render is read instead
        services.book_stages(book)


def test_the_tile_leads_to_review_once_the_text_is_final():
    book, pages = _book_with_pages(3, status=Page.Status.OCR_DONE)
    Page.objects.filter(pk=pages[0].pk).update(text_state=Page.TextState.FINAL)
    Page.objects.filter(pk=pages[1].pk).update(text_state=Page.TextState.FINAL, is_excluded=True)
    tiles = {tile["number"]: tile for tile in services.page_tiles(book)}
    assert tiles[1]["primary_url"] == f"/books/{book.pk}/review/1/"
    assert tiles[2]["primary_url"] == f"/books/{book.pk}/guides/#sheet-2"  # excluded: its sheet (D84)
    assert tiles[3]["primary_url"] == f"/books/{book.pk}/guides/#sheet-3"  # no final text yet
    assert "primary_url" not in services.page_tiles(book, compact=True)[0]


def test_tile_contract():
    book = Book.objects.create(pk=40, title="رحلة النص", status=Book.Status.REVIEWING)
    final = Page.objects.create(book=book, number=1, source_index=0, text_state=Page.TextState.FINAL)
    busy = Page.objects.create(book=book, number=8, source_index=7)
    out = Page.objects.create(
        book=book, number=5, source_index=4, text_state=Page.TextState.FINAL, is_excluded=True
    )
    pick = ("primary_url", "url", "review_url")

    def keys(page):
        return {key: services.page_tile(page)[key] for key in pick}

    contract = {
        "page_tile (full): the new key": {
            "a page with final text": keys(final),
            "a page still processing (no final text)": keys(busy),
            "an excluded page": keys(out),
        },
        "compact tile (the poll)": contract_fixture("tile.json")["compact tile (the poll)"],
    }
    write_contract("tile.json", contract)
    assert contract == contract_fixture("tile.json")


# ---------------------------------------------------------------- 7 review: «تراجع» of an exclusion


@pytest.mark.parametrize("awaits", [True, False])
def test_reincluding_a_page_brings_an_all_failed_book_back(awaits):
    # the toast's «تراجع» re-includes the one good page of a book whose other pages failed: every included
    # page had failed, so the book was «تعذّر تجهيز/معالجة كل صفحات الكتاب»; with the page back it is not
    done = {} if awaits else {"text_state": Page.TextState.FINAL}
    status = Page.Status.PREPROCESSED if awaits else Page.Status.OCR_DONE
    book, (good, bad) = _book_with_pages(2, status=status)
    Page.objects.filter(pk=good.pk).update(**done)
    Preprocess.objects.create(page=good)
    Book.objects.filter(pk=book.pk).update(
        awaits_ocr_start=awaits, status=Book.Status.NEEDS_GUIDES if awaits else Book.Status.READY_FOR_REVIEW
    )
    failure = {"status": Page.Status.ERROR, "error_from": "preprocess", "error_message": "x"}
    Page.objects.filter(pk=bad.pk).update(**failure)
    good.refresh_from_db()
    services.toggle_exclude(good)
    book.refresh_from_db()
    failed = ALL_PAGES_FAILED_LAYOUT if awaits else ALL_PAGES_FAILED
    assert (book.status, book.error_message) == (Book.Status.ERROR, failed)
    good.refresh_from_db()
    with patch("books.services.chain") as chain:
        services.toggle_exclude(good)
    chain.assert_not_called()  # a prepared page in «التخطيط», a read page after it: nothing to run
    good.refresh_from_db()
    book.refresh_from_db()
    assert good.status == (Page.Status.PREPROCESSED if awaits else Page.Status.OCR_DONE)
    assert book.status == (Book.Status.NEEDS_GUIDES if awaits else Book.Status.READY_FOR_REVIEW)
    assert book.error_message == ""


def test_an_ingest_error_stays_when_a_page_is_reincluded():
    book, pages = _book_with_pages(2, status=Page.Status.PREPROCESSED)
    Preprocess.objects.create(page=pages[0])
    Book.objects.filter(pk=book.pk).update(status=Book.Status.ERROR, error_message="تعذّر قراءة ملف PDF.")
    pages[0].refresh_from_db()
    services.toggle_exclude(pages[0])
    services.toggle_exclude(pages[0])
    book.refresh_from_db()
    assert (book.status, book.error_message) == (Book.Status.ERROR, "تعذّر قراءة ملف PDF.")
