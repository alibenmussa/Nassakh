"""Processing tests: pipeline on synthetic pages, guides, regions, services, tasks, views and API.

Synthetic pages are rendered with PIL (an Arabic system font when one is available, else the
default Latin font: the geometry is what the pipeline sees). Tests run on SQLite with a temporary
MEDIA_ROOT (settings_test) and Celery in eager mode.
"""

from __future__ import annotations

import io
import json
import re
import types
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.urls import clear_url_caches, reverse

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from books.models import Book, Page
from processing import pipeline, services, tasks
from processing.models import LayoutGuides, Preprocess, Region

# ---------------------------------------------------------------- synthetic pages

FONT_CANDIDATES = [
    Path.home() / "Library/Fonts/Amiri Regular.ttf",
    Path.home() / "Library/Fonts/Amiri-Regular.ttf",
    Path.home() / "Library/Fonts/Amiri Bold.ttf",
    Path("/Library/Fonts/Arial Unicode.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/System/Library/Fonts/GeezaPro.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf"),
]
ARABIC_WORDS = (
    "قال المؤلف في مقدمة الكتاب إن تاريخ المدينة القديمة يمتد إلى عصور بعيدة وقد ذكر ذلك المؤرخون".split()
)
LATIN_WORDS = "the quick brown fox jumps over the lazy dog while history repeats itself".split()


def _font(size: int) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, bool]:
    for candidate in FONT_CANDIDATES:
        if candidate.exists():
            try:
                return ImageFont.truetype(str(candidate), size), True
            except OSError:
                continue
    return ImageFont.load_default(size=size), False


def _line_text(words: list[str], font, max_width: int, start: int) -> str:
    text = ""
    i = start
    while True:
        candidate = (text + " " + words[i % len(words)]).strip()
        if font.getlength(candidate) > max_width:
            return text or words[i % len(words)]
        text = candidate
        i += 1


def render_page(
    width: int = 900,
    height: int = 1200,
    n_lines: int = 20,
    line_gap: int = 44,
    top: int = 120,
    margin: int = 110,
    font_size: int = 26,
    rule_y: int | None = None,
    footnote_lines: int = 0,
    strip: str | None = None,
    angle: float = 0.0,
) -> np.ndarray:
    """A white page with text lines; optional footnote rule, facing-page strip and rotation."""
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    font, arabic = _font(font_size)
    words = ARABIC_WORDS if arabic else LATIN_WORDS
    text_width = width - 2 * margin
    y = top
    for i in range(n_lines):
        draw.text((margin, y), _line_text(words, font, text_width, i * 3), font=font, fill=0)
        y += line_gap
    if rule_y is not None:
        # footnote rule on the start side of an RTL page (right), a third of the text width
        x1 = width - margin
        draw.line([(x1 - int(text_width * 0.45), rule_y), (x1, rule_y)], fill=0, width=3)
        small, _ = _font(int(font_size * 0.75))
        fy = rule_y + 24
        for i in range(footnote_lines):
            draw.text((margin, fy), _line_text(words, small, text_width, 7 + i * 2), font=small, fill=0)
            fy += int(line_gap * 0.8)
    if strip:
        # a column of glyph fragments from the neighbouring page, 20 px from the scan edge
        glyph = "ك" if arabic else "k"
        x = 18 if strip == "left" else width - 18 - int(font.getlength(glyph))
        yy = top
        for _ in range(n_lines):
            draw.text((x, yy), glyph, font=font, fill=0)
            yy += line_gap
    if angle:
        img = img.rotate(angle, resample=Image.BICUBIC, fillcolor=255)
    return np.asarray(img, dtype=np.uint8).copy()


def png_bytes(array: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(array).save(buf, format="PNG")
    return buf.getvalue()


def json_block(html: str, element_id: str) -> dict:
    """Parse the `{% json_script %}` block with the given id out of rendered HTML."""
    match = re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>', html, re.S)
    assert match, f"no json_script block {element_id!r}"
    return json.loads(match.group(1))


# ---------------------------------------------------------------- pipeline (pure functions)


def test_pipeline_straight_page_keeps_angle_near_zero_and_counts_lines():
    result = pipeline.run_pipeline(render_page(n_lines=20))
    assert abs(result.angle) <= 0.2
    assert abs(result.n_lines - 20) <= 1
    assert result.footnote_rule_y is None
    assert result.edge_strips_removed == []
    assert result.output_width == result.gray.shape[1] and result.output_height == result.gray.shape[0]
    assert result.bw.shape == result.gray.shape and set(np.unique(result.bw)) <= {0, 255}
    assert result.median_line_height > 5
    assert pipeline.FLAG_NO_LINES not in result.flags and pipeline.FLAG_EDGE_STRIP not in result.flags
    assert result.auto_params["angle"] == result.angle
    assert result.auto_params["crop_box"] == result.crop_box


def test_pipeline_deskews_a_page_rotated_by_two_degrees():
    # PIL rotates counter-clockwise for a positive angle; the corrective rotation is -2°.
    result = pipeline.run_pipeline(render_page(angle=2.0))
    assert abs(result.angle - (-2.0)) <= 0.3
    assert result.skew_confidence >= pipeline.LOW_SKEW_CONFIDENCE
    assert pipeline.FLAG_LOW_CONFIDENCE not in result.flags
    assert abs(result.n_lines - 20) <= 1

    # the straightened lines are horizontal again: every line box is short (about one line height)
    heights = [ln["y1"] - ln["y0"] for ln in result.line_boxes]
    assert max(heights) < 2.2 * result.median_line_height


def test_estimate_skew_direct_on_an_ink_mask():
    gray = render_page(angle=-1.5)
    ink = pipeline.binarize_clean(gray, min_area=8)
    angle, confidence = pipeline.estimate_skew(ink, 5.0)
    assert abs(angle - 1.5) <= 0.3
    assert confidence > 0


def test_pipeline_detects_a_drawn_footnote_rule():
    rule_y = 800
    gray = render_page(n_lines=14, rule_y=rule_y, footnote_lines=3)
    result = pipeline.run_pipeline(gray)
    assert result.footnote_rule_y is not None
    # footnote_rule_y is in the cropped output; add the crop offset to compare with the drawing
    assert abs(result.footnote_rule_y + result.crop_box[1] - rule_y) <= 6
    assert result.n_lines >= 16  # body lines + footnote lines; the rule itself is not a line


@pytest.mark.parametrize("side", ["left", "right"])
def test_pipeline_removes_a_facing_page_strip_at_the_edge(side):
    gray = render_page(strip=side)
    result = pipeline.run_pipeline(gray)
    assert len(result.edge_strips_removed) == 1
    strip = result.edge_strips_removed[0]
    assert strip["side"] == side and strip["kind"] == pipeline.STRIP_TEXT
    assert pipeline.FLAG_EDGE_STRIP in result.flags
    # the crop now hugs the main text block (margin 110 px ± the 2% crop margin), not the strip
    if side == "left":
        assert strip["x1"] < 60 and result.crop_box[0] >= 80
    else:
        assert strip["x0"] > 840 and result.crop_box[2] <= 820
    assert abs(result.n_lines - 20) <= 1
    # the output image contains no ink where the strip was
    x0, x1 = strip["x0"], strip["x1"]
    assert (
        result.gray[:, max(0, x0 - result.crop_box[0]) : max(0, x1 - result.crop_box[0])] < 128
    ).sum() == 0


def test_remove_edge_strips_direct_and_no_op_on_a_clean_page():
    gray = render_page(strip="left")
    ink = pipeline.binarize_clean(gray, min_area=8)
    out, strips = pipeline.remove_edge_strips(ink, gray)
    assert len(strips) == 1 and strips[0]["side"] == "left"
    assert out.shape == gray.shape
    assert (out[:, strips[0]["x0"] : strips[0]["x1"]] == 255).all()
    assert (gray[:, strips[0]["x0"] : strips[0]["x1"]] < 128).any()  # the input is untouched

    clean = render_page()
    out2, strips2 = pipeline.remove_edge_strips(pipeline.binarize_clean(clean, min_area=8), clean)
    assert strips2 == [] and (out2 == clean).all()


def test_remove_edge_strips_leaves_a_real_second_column_alone():
    # two columns of similar width separated by a gap: nothing is a "strip"
    gray = render_page(width=1400, margin=60, n_lines=12)
    right = render_page(width=1400, margin=60, n_lines=12)
    gray[:, 760:] = right[:, 760:]  # duplicate the text into a second column
    gray[:, 700:760] = 255
    ink = pipeline.binarize_clean(gray, min_area=8)
    _, strips = pipeline.remove_edge_strips(ink, gray)
    assert strips == []


def test_manual_params_override_detection_and_a_manual_crop_disables_strip_removal():
    gray = render_page(strip="left")
    manual = pipeline.PreprocessParams(
        angle=1.0, crop_box=[0, 0, 900, 1200], sauvola_window=31, sauvola_k=0.3, nlm_h=0
    )
    assert manual.is_manual
    result = pipeline.run_pipeline(gray, manual)
    assert result.angle == 1.0
    assert result.crop_box == [0, 0, 900, 1200]
    assert result.edge_strips_removed == [] and pipeline.FLAG_EDGE_STRIP not in result.flags
    assert result.sauvola_window == 31 and result.sauvola_k == 0.3 and result.nlm_h == 0
    assert (result.output_width, result.output_height) == (900, 1200)
    # the detected values are still reported for "reset"
    assert abs(result.auto_params["angle"]) <= 0.2
    assert result.auto_params["crop_box"][2] <= 900 and result.auto_params["crop_box"][3] <= 1200
    assert not pipeline.PreprocessParams().is_manual


def test_remove_dark_borders_and_clamp_box():
    gray = render_page()
    gray[:, :40] = 10  # a black scanner-bed band on the left
    gray[:25, :] = 5
    trimmed, box = pipeline.remove_dark_borders(gray)
    assert box[0] >= 40 and box[1] >= 25
    assert trimmed.shape == (box[3] - box[1], box[2] - box[0])
    assert pipeline.clamp_box([-5, 3.6, 950, 100], 900, 1200) == [0, 4, 900, 100]
    with pytest.raises(ValueError):
        pipeline.clamp_box([10, 10, 12, 500], 900, 1200)


def test_sauvola_window_rule():
    assert pipeline.sauvola_window_for(0) == 41
    assert pipeline.sauvola_window_for(21) == 43
    assert pipeline.sauvola_window_for(5) == 25
    assert pipeline.sauvola_window_for(60) == 75


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def book(db) -> Book:
    return Book.objects.create(title="كتاب تجريبي", status=Book.Status.PROCESSING)


def make_page(
    book: Book, number: int, rule_y: int | None = None, w: int = 1000, h: int = 1500, pre: bool = True
):
    page = Page.objects.create(
        book=book, number=number, source_index=number - 1, status=Page.Status.PREPROCESSED, width=w, height=h
    )
    if pre:
        Preprocess.objects.create(
            page=page,
            output_width=w,
            output_height=h,
            footnote_rule_y=rule_y,
            border_crop=[0, 0, w, h],
            crop_box=[0, 0, w, h],
            display_image=f"books/{book.pk}/pages/{number:04d}/display.webp",
        )
    return page


@pytest.fixture
def page_with_original(book) -> Page:
    page = Page.objects.create(book=book, number=1, source_index=0, width=900, height=1200)
    page.original_image.save("original.png", ContentFile(png_bytes(render_page(angle=1.0))), save=True)
    return page


@pytest.fixture
def users(db):
    editor = User.objects.create_user("editor", password="x")
    editor.groups.add(Group.objects.get(name="editor"))
    plain = User.objects.create_user("plain", password="x")
    return types.SimpleNamespace(editor=editor, plain=plain)


@pytest.fixture
def urls():
    """The real URLconf (the books app now provides `books:list` / `books:detail` / `books:page_detail`)."""
    clear_url_caches()
    yield
    clear_url_caches()


# ---------------------------------------------------------------- services: preprocess_page


@pytest.mark.django_db
def test_preprocess_page_writes_images_and_updates_the_page(page_with_original):
    page = page_with_original
    page.attention_flags = ["ocr_fallback", "large_skew"]
    page.save()

    pre = services.preprocess_page(page)

    assert isinstance(pre, Preprocess) and pre.page_id == page.pk
    for field, name in (
        (pre.gray_image, "gray.png"),
        (pre.bw_image, "bw.png"),
        (pre.display_image, "display.webp"),
        (pre.thumbnail, "thumb.webp"),
    ):
        assert field.name == f"books/{page.book_id}/pages/0001/{name}"
        assert default_storage.exists(field.name)
    with default_storage.open(pre.thumbnail.name, "rb") as fh:
        assert Image.open(fh).size[0] <= services.THUMB_MAX_WIDTH
    with default_storage.open(pre.display_image.name, "rb") as fh:
        img = Image.open(fh)
        assert img.format == "WEBP" and img.size == (pre.output_width, pre.output_height)
    with default_storage.open(pre.gray_image.name, "rb") as fh:
        assert Image.open(fh).size == (pre.output_width, pre.output_height)

    assert abs(pre.angle - (-1.0)) <= 0.3
    assert pre.is_manual is False
    assert pre.auto_params["angle"] == pre.angle
    assert pre.n_lines >= 18 and len(pre.line_boxes) == pre.n_lines
    assert pre.sauvola_window % 2 == 1 and pre.sauvola_k == 0.2 and pre.nlm_h == 6
    assert pre.border_crop == [0, 0, 900, 1200]

    page.refresh_from_db()
    assert page.status == Page.Status.PREPROCESSED
    assert page.attention_flags == ["ocr_fallback"]  # stage flags replaced, other flags kept


@pytest.mark.django_db
def test_preprocess_page_manual_params_are_stored_and_kept_by_the_task(page_with_original):
    page = page_with_original
    pre = services.preprocess_page(page, {"angle": 0.5, "sauvola_k": 0.3, "bogus": 1})
    assert pre.is_manual is True and pre.angle == 0.5 and pre.sauvola_k == 0.3
    assert abs(pre.auto_params["angle"] - (-1.0)) <= 0.3
    assert services.stored_manual_params(page)["angle"] == 0.5

    # a chain re-run keeps the user's values; an explicit auto run drops them
    assert tasks.preprocess_page.delay(page.pk).get() == page.pk
    pre.refresh_from_db()
    assert pre.is_manual is True and pre.angle == 0.5
    pre = services.preprocess_page(page, None)
    assert pre.is_manual is False and abs(pre.angle - (-1.0)) <= 0.3

    # the task also takes explicit overrides ({} = automatic), which the API uses for queued pages
    assert tasks.preprocess_page.delay(page.pk, {"angle": 0.7}).get() == page.pk
    pre.refresh_from_db()
    assert pre.is_manual is True and pre.angle == 0.7
    assert tasks.preprocess_page.delay(page.pk, {}).get() == page.pk
    pre.refresh_from_db()
    assert pre.is_manual is False


@pytest.mark.django_db
def test_preprocess_page_downscales_display_and_thumbnail(book):
    page = Page.objects.create(book=book, number=2, source_index=1, width=2000, height=700)
    wide = render_page(width=2000, height=700, n_lines=8, margin=150)
    page.original_image.save("original.png", ContentFile(png_bytes(wide)), save=True)
    pre = services.preprocess_page(page)
    assert pre.output_width > services.DISPLAY_MAX_WIDTH
    with default_storage.open(pre.display_image.name, "rb") as fh:
        assert Image.open(fh).size[0] == services.DISPLAY_MAX_WIDTH
    with default_storage.open(pre.thumbnail.name, "rb") as fh:
        assert Image.open(fh).size[0] == services.THUMB_MAX_WIDTH


@pytest.mark.django_db
def test_preprocess_page_without_original_raises_an_arabic_error(book):
    page = Page.objects.create(book=book, number=3, source_index=2)
    with pytest.raises(services.ProcessingError) as exc:
        services.preprocess_page(page)
    assert "صورة أصلية" in str(exc.value)


def test_clean_manual_params_validates():
    assert services.clean_manual_params(None) == {}
    assert services.clean_manual_params({"angle": "1.5", "crop_box": [1, 2, 300, 400.4], "nlm_h": ""}) == {
        "angle": 1.5,
        "crop_box": [1, 2, 300, 400],
    }
    with pytest.raises(ValidationError) as exc:
        services.clean_manual_params({"angle": 50, "crop_box": [5, 5, 6, 6], "sauvola_k": "x"})
    assert len(exc.value.messages) == 3


# ---------------------------------------------------------------- services: guides


@pytest.mark.django_db
def test_propose_guides_uses_the_median_rule_when_enough_pages_have_one(book):
    for n, rule in enumerate([1200, 1230, 1170, None, None], start=1):
        make_page(book, n, rule_y=rule)
    make_page(book, 6, rule_y=1400).__class__.objects.filter(number=6).update(is_excluded=True)

    guides, confidence = services.propose_guides(book)
    assert confidence == 0.6
    assert guides.footnote_line == 0.8  # median of 0.80, 0.82, 0.78
    assert guides.header_cut is None
    assert guides.page_number_zone == "bottom"
    assert guides.source == "auto"
    assert guides.reference_page.number == 1  # the page closest to the median
    stats = services.guides_stats(book)
    assert (stats.n_pages, stats.n_with_rule, stats.percent) == (5, 3, 60)


@pytest.mark.django_db
def test_propose_guides_low_confidence_gives_no_footnote_line_and_keeps_manual_guides(book):
    for n, rule in enumerate([1200, None, None, None, None], start=1):
        make_page(book, n, rule_y=rule)
    guides, confidence = services.propose_guides(book)
    assert confidence == 0.2 and guides.footnote_line is None
    assert guides.reference_page.number == 1

    guides.footnote_line = 0.7
    guides.source = LayoutGuides.Source.MANUAL
    guides.save()
    Preprocess.objects.update(footnote_rule_y=1200)
    guides2, confidence2 = services.propose_guides(book)
    assert confidence2 == 1.0
    assert guides2.pk == guides.pk and guides2.footnote_line == 0.7 and guides2.source == "manual"


@pytest.mark.django_db
def test_propose_guides_on_a_book_without_pages(book):
    guides, confidence = services.propose_guides(book)
    assert confidence == 0.0 and guides.footnote_line is None and guides.reference_page is None


@pytest.mark.django_db
def test_effective_guides_merges_book_values_and_page_override(book):
    page = make_page(book, 1)
    assert services.effective_guides(page) == services.DEFAULT_GUIDES
    LayoutGuides.objects.create(book=book, header_cut=0.1, footnote_line=0.8, page_number_zone="top")
    assert services.effective_guides(page)["header_cut"] == 0.1
    page.guides_override = {"footnote_line": None, "page_number_zone": "none", "ignored": 1}
    page.save()
    effective = services.effective_guides(page)
    assert effective["footnote_line"] is None and effective["page_number_zone"] == "none"
    assert effective["header_cut"] == 0.1 and "ignored" not in effective


def test_clean_guides_full_and_partial():
    clean = services.clean_guides({"header_cut": "0.1", "footnote_line": "", "page_number_zone": "top"})
    assert clean == {
        "header_cut": 0.1,
        "footnote_line": None,
        "page_number_zone": "top",
        "page_number_height": 0.06,
    }
    partial = services.clean_guides({"footnote_line": None}, partial=True)
    assert partial == {"footnote_line": None}
    with pytest.raises(ValidationError) as exc:
        services.clean_guides({"header_cut": 0.45, "footnote_line": 0.4, "page_number_zone": "left"})
    assert any("أعلى من خط الحاشية" in m for m in exc.value.messages)
    assert any("موضع رقم الصفحة" in m for m in exc.value.messages)


# ---------------------------------------------------------------- services: regions


def test_guide_regions_geometry():
    guides = {
        "header_cut": 0.1,
        "footnote_line": 0.8,
        "page_number_zone": "bottom",
        "page_number_height": 0.06,
    }
    assert services.guide_regions(guides, 1000, 1500) == [
        ("running_header", [0, 0, 1000, 150]),
        ("body", [0, 150, 1000, 1200]),
        ("footnote", [0, 1200, 1000, 1410]),
        ("page_number", [0, 1410, 1000, 1500]),
    ]
    assert services.guide_regions(services.DEFAULT_GUIDES, 1000, 1500) == [
        ("body", [0, 0, 1000, 1410]),
        ("page_number", [0, 1410, 1000, 1500]),
    ]
    top = {"header_cut": 0.1, "footnote_line": None, "page_number_zone": "top", "page_number_height": 0.05}
    assert services.guide_regions(top, 1000, 1500) == [
        ("page_number", [0, 0, 1000, 75]),
        ("running_header", [0, 75, 1000, 150]),
        ("body", [0, 150, 1000, 1500]),
    ]
    # a footnote line inside the page-number zone is ignored; no zone means the body reaches the bottom
    odd = {"header_cut": None, "footnote_line": 0.97, "page_number_zone": "none", "page_number_height": 0.06}
    assert services.guide_regions(odd, 1000, 1500) == [
        ("body", [0, 0, 1000, 1455]),
        ("footnote", [0, 1455, 1000, 1500]),
    ]
    odd["page_number_zone"] = "bottom"
    assert services.guide_regions(odd, 1000, 1500) == [
        ("body", [0, 0, 1000, 1410]),
        ("page_number", [0, 1410, 1000, 1500]),
    ]
    assert services.guide_regions(guides, 0, 0) == []


@pytest.mark.django_db
def test_derive_regions_creates_ordered_regions_and_keeps_manual_ones(book):
    LayoutGuides.objects.create(book=book, header_cut=0.1, footnote_line=0.8)
    page = make_page(book, 1)
    manual = Region.objects.create(
        page=page, kind="heading", bbox=[0, 200, 1000, 260], order=9, source="manual"
    )

    regions = services.derive_regions(page)
    assert [(r.kind, r.bbox, r.order, r.source) for r in regions] == [
        ("running_header", [0, 0, 1000, 150], 0, "guides"),
        ("body", [0, 150, 1000, 1200], 1, "guides"),
        ("footnote", [0, 1200, 1000, 1410], 2, "guides"),
        ("page_number", [0, 1410, 1000, 1500], 3, "guides"),
    ]
    page.refresh_from_db()
    assert page.status == Page.Status.LAYOUT_DONE
    assert Region.objects.filter(page=page).count() == 5
    assert Region.objects.filter(pk=manual.pk).exists()

    # per-page override: this page has no footnotes
    page.guides_override = {"footnote_line": None}
    page.save()
    regions = services.derive_regions(page)
    assert [r.kind for r in regions] == ["running_header", "body", "page_number"]
    assert regions[1].bbox == [0, 150, 1000, 1410]
    assert Region.objects.filter(page=page, source="guides").count() == 3


@pytest.mark.django_db
def test_derive_regions_unchanged_geometry_does_not_regress_a_finished_page(book):
    LayoutGuides.objects.create(book=book, footnote_line=0.8)
    page = make_page(book, 1)
    first = services.derive_regions(page)
    Page.objects.filter(pk=page.pk).update(status=Page.Status.OCR_DONE)
    page.refresh_from_db()

    again, changed = services._derive_regions(page)
    assert changed is False
    assert [r.pk for r in again] == [r.pk for r in first]
    page.refresh_from_db()
    assert page.status == Page.Status.OCR_DONE

    LayoutGuides.objects.filter(book=book).update(footnote_line=0.7)
    _regions, changed = services._derive_regions(page)
    assert changed is True
    page.refresh_from_db()
    assert page.status == Page.Status.LAYOUT_DONE


@pytest.mark.django_db
def test_derive_regions_requires_a_preprocess(book):
    page = make_page(book, 1, pre=False)
    with pytest.raises(services.ProcessingError):
        services.derive_regions(page)


@pytest.mark.django_db
def test_apply_guides_saves_re_derives_and_enqueues_ocr_only_for_changed_pages(book, users):
    pages = [make_page(book, n, rule_y=1200) for n in (1, 2)]
    make_page(book, 3, pre=False)  # not preprocessed yet: skipped
    excluded = make_page(book, 4)
    Page.objects.filter(pk=excluded.pk).update(is_excluded=True)
    book.status = Book.Status.NEEDS_GUIDES
    book.save()
    data = {
        "header_cut": "",
        "footnote_line": "0.8",
        "page_number_zone": "bottom",
        "page_number_height": "0.06",
    }

    with patch("books.services.run_stage", create=True) as run_stage:
        guides = services.apply_guides(book, data, users.editor)
    assert guides.source == "manual" and guides.footnote_line == 0.8 and guides.header_cut is None
    assert sorted(call.args[0].pk for call in run_stage.call_args_list) == sorted(p.pk for p in pages)
    assert {call.args[1] for call in run_stage.call_args_list} == {"ocr"}
    for page in pages:
        page.refresh_from_db()
        assert page.status == Page.Status.LAYOUT_DONE
        assert [r.kind for r in page.regions.all()] == ["body", "footnote", "page_number"]
    assert not Region.objects.filter(page__number__in=[3, 4]).exists()
    book.refresh_from_db()
    assert book.status == Book.Status.OCR

    # the same guides again: nothing changed, nothing enqueued
    with patch("books.services.run_stage", create=True) as run_stage:
        services.apply_guides(book, data, users.editor)
    assert run_stage.call_count == 0

    # invalid guides are rejected before anything is saved
    with pytest.raises(ValidationError):
        services.apply_guides(book, {**data, "header_cut": "0.45", "footnote_line": "0.4"}, users.editor)
    assert LayoutGuides.objects.get(book=book).footnote_line == 0.8


@pytest.mark.django_db
def test_set_page_guides_override_derives_and_resets(book):
    LayoutGuides.objects.create(book=book, footnote_line=0.8)
    page = make_page(book, 1)
    with patch("books.services.run_stage", create=True) as run_stage:
        regions, enqueued = services.set_page_guides_override(page, {"footnote_line": None})
    assert enqueued is True and run_stage.call_args.args == (page, "ocr")
    assert [r.kind for r in regions] == ["body", "page_number"]
    page.refresh_from_db()
    assert page.guides_override == {"footnote_line": None}

    with patch("books.services.run_stage", create=True) as run_stage:
        regions, enqueued = services.set_page_guides_override(page, {"reset": True})
    assert enqueued is True and page.guides_override is None
    assert [r.kind for r in regions] == ["body", "footnote", "page_number"]

    with patch("books.services.run_stage", create=True) as run_stage:
        _regions, enqueued = services.set_page_guides_override(page, {})
    assert enqueued is False and run_stage.call_count == 0


# ---------------------------------------------------------------- tasks


@pytest.mark.django_db
def test_tasks_run_the_chain_and_record_errors(page_with_original):
    page = page_with_original
    assert tasks.preprocess_page.delay(page.pk).get() == page.pk
    page.refresh_from_db()
    assert page.status == Page.Status.PREPROCESSED and page.task_id

    assert tasks.layout_page.delay(page.pk).get() == page.pk
    page.refresh_from_db()
    assert page.status == Page.Status.LAYOUT_DONE
    assert [r.kind for r in page.regions.all()] == ["body", "page_number"]

    broken = Page.objects.create(book=page.book, number=9, source_index=8)
    assert tasks.preprocess_page.delay(broken.pk).get() == broken.pk
    broken.refresh_from_db()
    assert broken.status == Page.Status.ERROR and broken.error_from == "preprocess"
    assert "صورة أصلية" in broken.error_message

    # layout skips a page that failed upstream and leaves the error message intact
    assert tasks.layout_page.delay(broken.pk).get() == broken.pk
    broken.refresh_from_db()
    assert broken.error_from == "preprocess" and not broken.regions.exists()

    # excluded pages and unknown ids are no-ops
    Page.objects.filter(pk=page.pk).update(is_excluded=True)
    assert tasks.preprocess_page.delay(page.pk).get() == page.pk
    assert tasks.layout_page.delay(999999).get() == 999999


@pytest.mark.django_db
def test_preprocess_task_unexpected_failure_sets_an_arabic_error(page_with_original):
    page = page_with_original
    with patch("processing.services.preprocess_page", side_effect=RuntimeError("boom")):
        assert tasks.preprocess_page.delay(page.pk).get() == page.pk
    page.refresh_from_db()
    assert page.status == Page.Status.ERROR and page.error_from == "preprocess"
    assert "boom" not in page.error_message and "المعالجة الأولية" in page.error_message


# ---------------------------------------------------------------- views


@pytest.mark.django_db
def test_guides_screen_requires_an_editor(client, book, users, urls):
    url = reverse("processing:guides", kwargs={"book_id": book.pk})
    response = client.get(url)
    assert response.status_code == 302 and reverse("accounts:login") in response["Location"]
    client.force_login(users.plain)
    assert client.get(url).status_code == 403


@pytest.mark.django_db
def test_guides_screen_empty_state_and_editor(client, book, users, urls):
    client.force_login(users.editor)
    url = reverse("processing:guides", kwargs={"book_id": book.pk})
    body = client.get(url).content.decode()
    assert "لا توجد صفحات معالَجة بعد" in body
    assert f'href="/books/{book.pk}/"' in body

    for n, rule in enumerate([1200, 1230, 1170, None], start=1):
        make_page(book, n, rule_y=rule)
    services.propose_guides(book)
    response = client.get(url)
    body = response.content.decode()
    assert response.status_code == 200
    assert "تطبيق على كل الصفحات" in body
    assert 'من <b class="tabular-nums">4</b> صفحة معالَجة' in body
    assert '<b class="tabular-nums">3</b>' in body and ">75</span>%" in body
    assert ">80.0</span>%" in body  # proposed ratio, Western digits
    assert f"/media/books/{book.pk}/pages/0001/display.webp" in body  # page 1 sits on the median
    config = json_block(body, "guides-config")
    assert config["footnote_line"] == 0.8 and config["proposal"] == 0.8
    assert config["detected_rule"] == 0.8 and config["output"] == {"width": 1000, "height": 1500}
    # another reference page via ?page=
    body2 = client.get(url + "?page=4").content.decode()
    assert f"/media/books/{book.pk}/pages/0004/display.webp" in body2
    assert json_block(body2, "guides-config")["detected_rule"] is None


@pytest.mark.django_db
def test_guides_screen_post_applies_and_redirects(client, book, users, urls):
    client.force_login(users.editor)
    page = make_page(book, 1, rule_y=1200)
    url = reverse("processing:guides", kwargs={"book_id": book.pk})
    data = {
        "header_cut": "0.0800",
        "footnote_line": "0.8000",
        "page_number_zone": "bottom",
        "page_number_height": "0.0600",
        "reference_page": str(page.pk),
    }
    with patch("books.services.run_stage", create=True) as run_stage:
        response = client.post(url, data)
    assert response.status_code == 302 and response["Location"] == f"/books/{book.pk}/"
    guides = LayoutGuides.objects.get(book=book)
    assert (guides.header_cut, guides.footnote_line, guides.source) == (0.08, 0.8, "manual")
    assert guides.reference_page_id == page.pk
    assert run_stage.call_count == 1
    assert [r.kind for r in page.regions.all()] == ["running_header", "body", "footnote", "page_number"]

    bad = client.post(url, {**data, "header_cut": "0.45", "footnote_line": "0.4"}, follow=True)
    assert "أعلى من خط الحاشية" in bad.content.decode()


# ---------------------------------------------------------------- API


@pytest.mark.django_db
def test_api_page_preprocess_reruns_with_manual_params(client, page_with_original, users):
    page = page_with_original
    url = reverse("api:page_preprocess", kwargs={"page_id": page.pk})
    assert client.post(url, "{}", content_type="application/json").status_code == 403
    client.force_login(users.plain)
    assert client.post(url, "{}", content_type="application/json").status_code == 403

    client.force_login(users.editor)
    response = client.post(
        url, json.dumps({"angle": 1.0, "sauvola_k": 0.25}), content_type="application/json"
    )
    assert response.status_code == 200, response.content
    data = response.json()
    assert (
        data["is_manual"] is True and data["params"]["angle"] == 1.0 and data["params"]["sauvola_k"] == 0.25
    )
    assert data["images"]["display"].startswith(f"/media/books/{page.book_id}/pages/0001/display.webp?v=")
    assert data["images"]["thumb"].endswith(".webp?v=" + data["images"]["display"].rsplit("=", 1)[1])
    assert data["status"] == "preprocessed" and data["queued"] is False
    assert data["frame"] == {"width": 900, "height": 1200}
    assert data["output"]["width"] > 0 and data["stats"]["n_lines"] >= 18
    assert data["regions"] == []  # never laid out: regions are not invented

    bad = client.post(url, json.dumps({"angle": 50}), content_type="application/json")
    assert bad.status_code == 400 and "زاوية" in bad.json()["errors"][0]

    reset = client.post(url, json.dumps({"reset": True}), content_type="application/json").json()
    assert reset["is_manual"] is False and abs(reset["params"]["angle"] - (-1.0)) <= 0.3


@pytest.mark.django_db
def test_api_page_preprocess_re_derives_existing_regions_and_queues_huge_pages(
    client, page_with_original, users
):
    page = page_with_original
    client.force_login(users.editor)
    services.preprocess_page(page)
    services.derive_regions(page)
    url = reverse("api:page_preprocess", kwargs={"page_id": page.pk})
    data = client.post(url, json.dumps({"angle": 0.0}), content_type="application/json").json()
    assert [r["kind"] for r in data["regions"]] == ["body", "page_number"]
    assert data["regions"][0]["bbox"][2] == data["output"]["width"]

    Page.objects.filter(pk=page.pk).update(width=5000, height=5000)
    with patch("processing.tasks.preprocess_page.delay") as delay:
        delay.return_value.id = "task-1"
        queued = client.post(url, json.dumps({"angle": 2.0}), content_type="application/json")
        reset = client.post(url, json.dumps({"reset": True}), content_type="application/json")
    assert queued.status_code == 202 and queued.json() == {
        "queued": True,
        "task_id": "task-1",
        "detail": "الصورة كبيرة؛ أُرسلت المعالجة إلى العامل الخلفي.",
    }
    assert reset.status_code == 202
    assert [call.args for call in delay.call_args_list] == [(page.pk, {"angle": 2.0}), (page.pk, {})]

    missing = Page.objects.create(book=page.book, number=7, source_index=6)
    gone = client.post(
        reverse("api:page_preprocess", kwargs={"page_id": missing.pk}), "{}", content_type="application/json"
    )
    assert gone.status_code == 422 and "صورة أصلية" in gone.json()["errors"][0]


@pytest.mark.django_db
def test_api_page_guides_override(client, book, users):
    LayoutGuides.objects.create(book=book, footnote_line=0.8)
    page = make_page(book, 1)
    url = reverse("api:page_guides_override", kwargs={"page_id": page.pk})
    client.force_login(users.editor)
    with patch("books.services.run_stage", create=True) as run_stage:
        response = client.post(url, json.dumps({"footnote_line": None}), content_type="application/json")
    assert response.status_code == 200, response.content
    data = response.json()
    assert [r["kind"] for r in data["regions"]] == ["body", "page_number"]
    assert data["regions"][0]["label"] == "متن" and data["regions"][0]["bbox"] == [0, 0, 1000, 1410]
    assert data["guides"]["footnote_line"] is None and data["override"] == {"footnote_line": None}
    assert data["ocr_enqueued"] is True and data["status"] == "layout_done"
    assert run_stage.call_count == 1

    bad = client.post(url, json.dumps({"footnote_line": 1.5}), content_type="application/json")
    assert bad.status_code == 400 and "خط الحاشية" in bad.json()["errors"][0]

    no_pre = make_page(book, 2, pre=False)
    response = client.post(
        reverse("api:page_guides_override", kwargs={"page_id": no_pre.pk}),
        "{}",
        content_type="application/json",
    )
    assert response.status_code == 422


# ---------------------------------------------------------------- templates


@pytest.mark.django_db
def test_preprocess_panel_partial_renders_config_for_a_page(book, urls):
    from django.template.loader import render_to_string

    page = make_page(book, 1, rule_y=1200)
    html = render_to_string("processing/_preprocess_panel.html", {"page": page, "book": book})
    assert 'id="preprocess-config"' in html and "preprocessPanel(" in html
    assert "إعادة المعالجة" in html and "استعادة القيم التلقائية" in html
    config = json_block(html, "preprocess-config")
    assert config["api_url"] == f"/api/pages/{page.pk}/preprocess/"
    assert config["has_preprocess"] is True and config["stats"]["footnote_rule_y"] == 1200
    assert config["guides"]["page_number_zone"] == "bottom"

    empty = render_to_string("processing/_preprocess_panel.html", {"page": None, "book": None})
    assert "لا توجد صفحة" in empty and "preprocess-config" not in empty

    bare = Page.objects.create(book=book, number=5, source_index=4)
    html2 = render_to_string("processing/_preprocess_panel.html", {"page": bare, "book": book})
    config2 = json_block(html2, "preprocess-config")
    assert config2["has_preprocess"] is False and "تشغيل المعالجة" in html2
