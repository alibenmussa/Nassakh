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

import numpy as np
import pymupdf
import pytest
from PIL import Image

from books import services, tasks
from books.models import Book, Page
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

        chain.assert_called_once_with(*[f"sig:{name}" for name in expected])
        chain.return_value.apply_async.assert_called_once_with()
        mocks[expected[0]].s.assert_called_once_with(page.pk)  # first task gets the page id
        for name in expected[1:]:
            mocks[name].s.assert_called_once_with()  # the rest receive it from the chain
    page.refresh_from_db()
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
    book.refresh_from_db()
    assert book.status == Book.Status.OCR
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
    assert progress["percent"] == round(100 * (3 + 2) / 12)  # ocr_done 3/3 + layout_done 2/3 over 4 pages
    assert progress["active"] is True
    assert progress["flags"] == 2  # one flagged page, one errored page
    assert progress["status_label"] == "قيد التعرّف على النص"

    book.status = Book.Status.READY_FOR_REVIEW
    book.save()
    assert services.book_progress(book)["active"] is False
    assert services.book_progress(Book.objects.create(title="فارغ"))["percent"] == 0


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
    chord.return_value.assert_called_once_with(tasks.after_preprocess.s(book.pk))
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
    pages[1].refresh_from_db()
    with patch("books.services.chain"):
        services.run_stage(pages[1], "ocr_full")
    pages[1].refresh_from_db()
    assert (pages[1].status, pages[1].text_state) == ("layout_done", "provisional")
    assert services.page_status(pages[1])["active"] is True


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


def test_page_status_returns_the_headline_and_the_detail_apart():
    # F26: the banner shows the Arabic headline only
    _, pages = _book_with_pages(1)
    pages[0].set_error("ocr_full", "تعذّر التعرّف على النص.\nOSError: [Errno 2] No such file")
    state = services.page_status(pages[0])
    assert state["error"] == "تعذّر التعرّف على النص."
    assert state["error_detail"] == "OSError: [Errno 2] No such file"
    # F43: the page detail enables its image tabs live from the same payload
    assert state["images"] == {"original": None, "gray": None, "bw": None}


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
    assert book.pages.get().status in (Page.Status.OCR_DONE, Page.Status.PREPROCESSED)
    assert f"book {book.pk}: {book.status}" in out.getvalue()


# ====================================================================== views


@pytest.mark.parametrize(
    "name, kwargs",
    [
        ("books:list", {}),
        ("books:create", {}),
        ("books:detail", {"book_id": 1}),
        ("books:page_detail", {"book_id": 1, "number": 1}),
    ],
)
def test_html_views_require_login(client, name, kwargs):
    response = client.get(reverse(name, kwargs=kwargs))
    assert response.status_code == 302
    assert response["Location"].startswith("/accounts/login/?next=")


@pytest.mark.parametrize(
    "name, kwargs",
    [("api:book_progress", {"book_id": 1}), ("api:page_status", {"page_id": 1})],
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
    assert 'role="progressbar"' in body and "0%" in body


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
            "source_pdf": SimpleUploadedFile("book.pdf", make_text_pdf(3), content_type="application/pdf"),
        },
    )
    book = Book.objects.get(title="بعض الملامح التاريخية")
    assert response.status_code == 302 and response["Location"] == reverse("books:detail", args=[book.pk])
    assert book.created_by == editor
    assert (book.pages_per_sheet, book.split_ratio, book.use_text_layer) == (2, 0.55, True)
    assert book.source_page_count == 3 and book.has_text_layer is True
    assert book.source_pdf.name == f"books/{book.pk}/source.pdf"


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
    assert "إعادة التشغيل" not in body

    pages = services.ingest_book(book)
    Page.objects.filter(pk=pages[0].pk).update(status=Page.Status.ERROR, error_message="فشل\nTraceback")
    Page.objects.filter(pk=pages[1].pk).update(attention_flags=["large_skew"])
    book.status = Book.Status.OCR
    book.save()

    response = editor_client.get(url)
    assert response.status_code == 200
    body = response.content.decode()
    assert "bookDashboard(" in body and 'id="dashboard-config"' in body
    assert reverse("api:book_progress", args=[book.pk]) in body
    for label in ("مرفوعة", "مُعالَجة", "تم التخطيط", "تم التعرّف", "خطأ", "التقدّم", "الصفحات"):
        assert label in body
    assert body.count('<div class="page-tile') == 2
    assert reverse("books:page_detail", args=[book.pk, 1]) in body
    assert reverse("books:toggle_exclude", args=[book.pk, 1]) in body
    assert reverse("books:rerun", args=[book.pk]) in body and "إعادة التشغيل" in body
    assert "بدء المعالجة" not in body  # not startable while in OCR
    config = _json_config(body, "dashboard-config")
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
    assert "RuntimeError: boom" in body and "إعادة بدء المعالجة" in body


def test_page_detail_renders_neighbours_partials_and_viewer_config(editor_client):
    book = make_book(make_scan_pdf(3))
    pages = services.ingest_book(book)
    page = pages[1]
    Preprocess.objects.create(page=page, output_width=1000, output_height=1500)
    Region.objects.create(page=page, kind=Region.Kind.BODY, bbox=[10, 20, 900, 1200], order=0)
    Region.objects.create(page=page, kind=Region.Kind.FOOTNOTE, bbox=[10, 1250, 900, 1480], order=1)
    Page.objects.filter(pk=page.pk).update(attention_flags=["edge_strip_removed"])

    response = editor_client.get(reverse("books:page_detail", args=[book.pk, 2]))
    assert response.status_code == 200
    body = response.content.decode()
    prev_url = reverse("books:page_detail", args=[book.pk, 1])
    next_url = reverse("books:page_detail", args=[book.pk, 3])
    assert f'href="{prev_url}"' in body and f'href="{next_url}"' in body
    assert "pageDetail(" in body and 'id="viewer-config"' in body
    assert "الأصل" in body and "المعالَجة" in body and "أبيض وأسود" in body
    assert page.original_image.url in body
    assert "أُزيل شريط من حافة الصفحة" in body  # flag label
    viewer = _json_config(body, "viewer-config")
    assert viewer["prevUrl"] == prev_url and viewer["nextUrl"] == next_url
    assert viewer["size"] == [1000, 1500]
    assert viewer["images"]["original"] == page.original_image.url and viewer["images"]["gray"] is None
    assert [(r["kind"], r["label"], r["bbox"]) for r in viewer["regions"]] == [
        ("body", "متن", [10, 20, 900, 1200]),
        ("footnote", "حاشية", [10, 1250, 900, 1480]),
    ]
    assert viewer["initialTab"] == "original"
    assert "تشغيلات المحرّكات" in body
    assert reverse("books:rerun", args=[book.pk, 2]) in body
    assert reverse("books:toggle_exclude", args=[book.pk, 2]) in body
    assert "2 / 3" in body

    # First and last pages have only one neighbour.
    first = editor_client.get(reverse("books:page_detail", args=[book.pk, 1])).content.decode()
    assert f'href="{reverse("books:page_detail", args=[book.pk, 2])}"' in first
    assert first.count('aria-disabled="true"') == 1
    assert editor_client.get(reverse("books:page_detail", args=[book.pk, 9])).status_code == 404


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
    assert "بدأت المعالجة" in response.content.decode()
    book.refresh_from_db()
    assert book.status == Book.Status.PROCESSING
    # Starting again while processing is refused with an Arabic message, not an exception.
    with patch("books.tasks.ingest_book_task.delay") as delay:
        response = editor_client.post(reverse("books:start", args=[book.pk]), follow=True)
    delay.assert_not_called()
    assert "المعالجة جارية بالفعل" in response.content.decode()


def test_rerun_view_for_the_book_and_for_one_page(editor_client):
    book, pages = _book_with_pages(2, status=Page.Status.OCR_DONE)
    with patch("books.tasks.rerun_book_from.delay") as delay:
        response = editor_client.post(reverse("books:rerun", args=[book.pk]), {"stage": "layout"})
    delay.assert_called_once_with(book.pk, "layout")
    assert response["Location"] == reverse("books:detail", args=[book.pk])

    with patch("books.services.run_stage") as run_stage:
        response = editor_client.post(reverse("books:rerun", args=[book.pk, 2]) + "?stage=ocr_full")
    assert run_stage.call_args.args[0].pk == pages[1].pk and run_stage.call_args.args[1] == "ocr_full"
    assert response["Location"] == reverse("books:page_detail", args=[book.pk, 2])

    with patch("books.tasks.rerun_book_from.delay") as delay:
        response = editor_client.post(reverse("books:rerun", args=[book.pk]), {"stage": "nope"}, follow=True)
    delay.assert_not_called()
    assert "اختر مرحلة صحيحة" in response.content.decode()


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


def test_api_progress_and_page_status(editor_client):
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
    assert data["pages"][0]["url"] == reverse("books:page_detail", args=[book.pk, 1])

    status = editor_client.get(reverse("api:page_status", args=[pages[0].pk])).json()
    assert status["status"] == "ocr_done" and status["text_state"] == "final"
    assert status["final_text"] == "النص النهائي" and status["provisional_text"] == "نص مبدئي"
    assert status["flags"] == [] and status["error"] == "" and status["active"] is False

    status = editor_client.get(reverse("api:page_status", args=[pages[1].pk])).json()
    assert status["error"] == "فشل" and status["error_from"] == "ocr_full"
    status = editor_client.get(reverse("api:page_status", args=[pages[2].pk])).json()
    assert status["active"] is True  # uploaded page in an active book
    assert editor_client.get(reverse("api:page_status", args=[99999])).status_code == 404


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


def test_page_detail_shows_the_printed_number(editor_client):
    book = make_book(make_scan_pdf(1))
    page = services.ingest_book(book)[0]
    Page.objects.filter(pk=page.pk).update(printed_number="41")
    body = editor_client.get(reverse("books:page_detail", args=[book.pk, 1])).content.decode()
    assert "الرقم المطبوع:" in body and ">41</bdi>" in body


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
    _ocr_line(
        page, 0, [{"t": "قال", "conf": "high"}, {"t": "الكتب", "conf": "low", "alt": "الكتاب", "res": None}]
    )
    _ocr_line(page, 1, [{"t": "(١)", "conf": "low", "res": "primary"}], region=foot)

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
        {"order": 0, "region_kind": "body", "tokens": [
            {"t": "قال", "conf": "high", "res": None}, {"t": "الكتب", "conf": "low", "res": None}]},
        {"order": 1, "region_kind": "footnote", "tokens": [{"t": "(١)", "conf": "low", "res": "primary"}]},
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
    with django_assert_max_num_queries(4):
        data = services.book_sheets(book, 1, 12)
    assert len(data["pages"]) == 12 and all(len(p["lines"]) == 1 for p in data["pages"])
