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
    rule_style: str = "solid",
    rule_frac: float = 0.45,
    footnote_scale: float = 0.75,
    page_number: str | None = None,
    page_number_at: str = "bottom",
) -> np.ndarray:
    """A white page with text lines; optional footnote rule / block, page number, strip and rotation.

    `rule_style` is solid, dotted or dashed; `rule_frac` its length as a share of the text width.
    With `footnote_lines` and no `rule_y`, the smaller-type footnote block starts two line pitches
    below the body. `page_number_at` is `bottom` (centred) or `top` (at the right margin).
    """
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
        # footnote rule on the start side of an RTL page (right)
        x1 = width - margin
        x0 = x1 - int(text_width * rule_frac)
        if rule_style == "solid":
            draw.line([(x0, rule_y), (x1, rule_y)], fill=0, width=3)
        else:
            dash, gap = (3, 5) if rule_style == "dotted" else (12, 8)
            for x in range(x0, x1, dash + gap):
                draw.rectangle([x, rule_y - 1, min(x + dash, x1), rule_y + 1], fill=0)
    if footnote_lines:
        small, _ = _font(int(font_size * footnote_scale))
        fy = rule_y + 24 if rule_y is not None else y + line_gap
        for i in range(footnote_lines):
            draw.text((margin, fy), _line_text(words, small, text_width, 7 + i * 2), font=small, fill=0)
            fy += int(line_gap * footnote_scale)
    if page_number:
        tw = int(font.getlength(page_number))
        if page_number_at == "bottom":
            draw.text(((width - tw) // 2, height - 90), page_number, font=font, fill=0)
        else:
            draw.text((width - margin - tw, 40), page_number, font=font, fill=0)
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


@pytest.mark.parametrize(
    ("style", "frac"), [("solid", 0.45), ("dotted", 0.45), ("dashed", 0.45), ("solid", 0.15)]
)
def test_pipeline_detects_solid_dotted_dashed_and_short_rules(style, frac):
    rule_y = 820
    gray = render_page(n_lines=14, rule_y=rule_y, footnote_lines=3, rule_style=style, rule_frac=frac)
    result = pipeline.run_pipeline(gray)
    assert result.footnote_rule_y is not None, style
    assert abs(result.footnote_rule_y + result.crop_box[1] - rule_y) <= 6
    assert result.footnote_block_y is None  # the rule wins; no block fallback


def test_pipeline_page_without_footnotes_has_neither_rule_nor_block():
    result = pipeline.run_pipeline(render_page(n_lines=20))
    assert result.footnote_rule_y is None and result.footnote_block_y is None


def test_pipeline_detects_a_smaller_type_block_without_a_rule():
    gray = render_page(n_lines=14, footnote_lines=3, footnote_scale=0.6)
    result = pipeline.run_pipeline(gray)
    assert result.footnote_rule_y is None
    assert result.footnote_block_y is not None
    body_bottom = 120 + 13 * 44 + 26  # last body line of the drawing (text origin + font size)
    block_top = 120 + 15 * 44  # text origin of the first footnote line
    y = result.footnote_block_y + result.crop_box[1]  # back to page coordinates
    assert body_bottom <= y <= block_top + 22  # between the body and the first footnote glyphs


def test_detect_footnote_block_needs_two_small_lines_a_gap_and_the_lower_page():
    body = [{"x0": 0, "y0": 100 + 50 * i, "x1": 800, "y1": 125 + 50 * i, "size": 25} for i in range(14)]
    small = [{"x0": 0, "y0": 900 + 30 * i, "x1": 800, "y1": 915 + 30 * i, "size": 15} for i in range(2)]
    y = pipeline.detect_footnote_block(body + small, 25, 1200)
    assert y is not None and body[-1]["y1"] < y <= 900
    assert pipeline.detect_footnote_block(body + small[:1], 25, 1200) is None  # one line only
    close = [dict(ln, y0=ln["y0"] - 130, y1=ln["y1"] - 130) for ln in small]  # no gap above
    assert pipeline.detect_footnote_block(body[:-1] + close, 25, 1200) is None
    assert pipeline.detect_footnote_block(body + small, 25, 3000) is None  # not in the lower 45%


@pytest.mark.parametrize(("text", "at"), [("— ٢٢ —", "bottom"), ("٢٢", "top")])
def test_pipeline_detects_the_page_number_at_the_bottom_centre_and_the_top_right(text, at):
    gray = render_page(n_lines=18, top=160, page_number=text, page_number_at=at)
    result = pipeline.run_pipeline(gray)
    box = result.page_number_box
    assert box is not None and box["position"] == at
    x0, y0, x1, y1 = box["bbox"]
    width, height = result.output_width, result.output_height
    if at == "bottom":
        assert y0 >= 0.85 * height and abs((x0 + x1) / 2 - width / 2) < 0.1 * width
    else:
        assert y1 <= 0.12 * height and x0 > width / 2
    # the number is metadata of the page, not a line of text: the stored line boxes are unchanged
    plain = pipeline.run_pipeline(render_page(n_lines=18, top=160))
    assert result.n_lines in (plain.n_lines, plain.n_lines + 1)
    assert result.footnote_block_y is None


def test_pipeline_finds_a_rule_whose_only_footnote_is_a_short_line():
    # a short footnote line is too weak for `detect_lines`; it still counts as text below the rule
    img = Image.fromarray(render_page(n_lines=14, rule_y=820, rule_style="dotted", rule_frac=0.3))
    font, arabic = _font(20)
    draw = ImageDraw.Draw(img)
    words = " ".join((ARABIC_WORDS if arabic else LATIN_WORDS)[:6])
    draw.text((900 - 110 - int(font.getlength(words)), 850), words, font=font, fill=0)
    result = pipeline.run_pipeline(np.asarray(img, dtype=np.uint8).copy())
    assert result.footnote_rule_y is not None
    assert abs(result.footnote_rule_y + result.crop_box[1] - 820) <= 6


def test_pipeline_page_number_is_not_joined_to_a_mark_at_the_other_end_of_its_row():
    img = Image.fromarray(render_page(n_lines=18, top=160, page_number="٣٥", page_number_at="bottom"))
    ImageDraw.Draw(img).rectangle([760, 1080, 766, 1140], fill=0)  # a tall handwritten stroke
    result = pipeline.run_pipeline(np.asarray(img, dtype=np.uint8).copy())
    box = result.page_number_box
    assert box is not None and box["position"] == "bottom"
    x0, _y0, x1, _y1 = box["bbox"]
    assert abs((x0 + x1) / 2 - result.output_width / 2) < 0.1 * result.output_width


def test_detect_page_number_ignores_a_normal_width_line():
    lines = [{"x0": 50, "y0": 100 + 50 * i, "x1": 850, "y1": 125 + 50 * i} for i in range(20)]
    last = {"x0": 50, "y0": 1120, "x1": 850, "y1": 1145}  # a full text line at the bottom
    assert pipeline.detect_page_number(lines + [last], 900, 1200, 25) is None
    short = {"x0": 430, "y0": 1120, "x1": 470, "y1": 1145}
    assert pipeline.detect_page_number(lines + [short], 900, 1200, 25) == {
        "bbox": [430, 1120, 470, 1145],
        "position": "bottom",
    }
    touching = {"x0": 430, "y0": 1080, "x1": 470, "y1": 1105}  # no gap to the line above
    assert pipeline.detect_page_number(lines + [touching], 900, 1200, 25) is None


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


def _two_columns(width: int = 1600) -> np.ndarray:
    """Two text columns (150 px outer margins) separated by an 80 px gutter."""
    gray = render_page(width=width, margin=150, n_lines=12)
    gray[:, width // 2 - 40 : width // 2 + 40] = 255
    return gray


def _glyph_column(gray: np.ndarray, x: int, glyph: str, n: int = 12, size: int = 26) -> np.ndarray:
    img = Image.fromarray(gray)
    draw = ImageDraw.Draw(img)
    font, _ = _font(size)
    for i in range(n):
        draw.text((x, 120 + 44 * i), glyph, font=font, fill=0)
    return np.asarray(img).copy()


def test_remove_edge_strips_keeps_narrow_bands_between_two_columns():
    # a column of verse numbers in the gutter is not at the edge of the scan (F14)
    gray = _glyph_column(_two_columns(), 790, "7")
    _, strips = pipeline.remove_edge_strips(pipeline.binarize_clean(gray, min_area=8), gray)
    assert strips == []


def test_remove_edge_strips_removes_a_strip_beyond_the_lighter_second_column():
    # the heavier column is on the right; the facing-page strip sits beyond the left column (F14)
    gray = _two_columns()
    gray[350:, 100:800] = 255
    gray = _glyph_column(gray, 10, "ك")
    _, strips = pipeline.remove_edge_strips(pipeline.binarize_clean(gray, min_area=8), gray)
    assert [(s["side"], s["kind"]) for s in strips] == [("left", pipeline.STRIP_TEXT)]
    assert strips[0]["x1"] < 40


def test_a_page_number_removed_from_the_outer_margin_is_flagged():
    # a single glyph-sized item is text-like ink: it is removed but never silently (F18)
    gray = render_page(width=2000, height=2800, margin=250, n_lines=30)
    img = Image.fromarray(gray)
    ImageDraw.Draw(img).text((1830, 1400), "217", font=_font(44)[0], fill=0)
    result = pipeline.run_pipeline(np.asarray(img).copy())
    assert [s["kind"] for s in result.edge_strips_removed] == [pipeline.STRIP_TEXT]
    assert pipeline.FLAG_EDGE_STRIP in result.flags


def test_a_manual_crop_still_reports_what_the_automatic_run_would_do():
    gray = render_page(strip="left")
    auto = pipeline.run_pipeline(gray)
    manual = pipeline.run_pipeline(gray, pipeline.PreprocessParams(crop_box=[0, 0, 900, 1200]))
    assert manual.edge_strips_removed == []  # nothing whitened under a manual crop
    assert manual.auto_params["crop_box"] == auto.crop_box  # F17: auto_params stay truthful
    assert manual.auto_params["edge_strips"] == auto.edge_strips_removed != []


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
    # only the posted keys are pinned; crop and window stay automatic on chain re-runs (F15)
    assert services.stored_manual_params(page) == {"angle": 0.5, "sauvola_k": 0.3}

    # a chain re-run keeps the user's values; an explicit auto run drops them
    assert tasks.preprocess_page.delay(page.pk).get() == page.pk
    pre.refresh_from_db()
    assert pre.is_manual is True and pre.angle == 0.5
    assert pre.manual_params == {"angle": 0.5, "sauvola_k": 0.3}
    pre = services.preprocess_page(page, None)
    assert pre.is_manual is False and abs(pre.angle - (-1.0)) <= 0.3
    assert pre.manual_params == {} and services.stored_manual_params(page) is None

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
    LayoutGuides.objects.create(book=book, header_cut=0.1, footnote_line=0.8, source="manual")
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
    LayoutGuides.objects.create(book=book, footnote_line=0.8, source="manual")
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


def _kinds_and_boxes(page: Page) -> list[tuple[str, list[int]]]:
    return [(r.kind, r.bbox) for r in services.derive_regions(page)]


@pytest.mark.django_db
def test_derive_regions_per_page_from_rule_block_or_nothing(book):
    with_rule = make_page(book, 1, rule_y=1100)
    with_block = make_page(book, 2)
    Preprocess.objects.filter(page=with_block).update(footnote_block_y=1250)
    plain = make_page(book, 3)
    assert _kinds_and_boxes(with_rule) == [("body", [0, 0, 1000, 1100]), ("footnote", [0, 1100, 1000, 1500])]
    assert _kinds_and_boxes(with_block) == [("body", [0, 0, 1000, 1250]), ("footnote", [0, 1250, 1000, 1500])]
    # no footnotes detected: the body runs to the bottom, and no automatic page-number zone
    assert _kinds_and_boxes(plain) == [("body", [0, 0, 1000, 1500])]


@pytest.mark.django_db
def test_derive_regions_uses_the_detected_page_number_box(book):
    bottom = make_page(book, 1, rule_y=1100)
    Preprocess.objects.filter(page=bottom).update(
        page_number_box={"bbox": [480, 1440, 520, 1470], "position": "bottom"}
    )
    pad = services.PAGE_NUMBER_PAD
    assert _kinds_and_boxes(bottom) == [
        ("body", [0, 0, 1000, 1100]),
        ("footnote", [0, 1100, 1000, 1440 - pad]),
        ("page_number", [480 - pad, 1440 - pad, 520 + pad, 1470 + pad]),
    ]
    top = make_page(book, 2)
    Preprocess.objects.filter(page=top).update(
        page_number_box={"bbox": [900, 10, 950, 40], "position": "top"}
    )
    assert _kinds_and_boxes(top) == [
        ("page_number", [900 - pad, 10 - pad, 950 + pad, 40 + pad]),
        ("body", [0, 40 + pad, 1000, 1500]),
    ]
    # a page override of the zone wins over the detection ("none" switches it off)
    top.guides_override = {"page_number_zone": "none"}
    top.save()
    assert _kinds_and_boxes(top) == [("body", [0, 0, 1000, 1500])]


@pytest.mark.django_db
def test_derive_regions_never_applies_an_automatic_book_line(book):
    # an automatic proposal (propose_guides) is display only: no footnote, no 6% zone on a page
    LayoutGuides.objects.create(book=book, footnote_line=0.8, page_number_zone="bottom", source="auto")
    page = make_page(book, 1)
    assert _kinds_and_boxes(page) == [("body", [0, 0, 1000, 1500])]
    # once the owner applies the guides by hand they are the fallback for pages without detection
    LayoutGuides.objects.filter(book=book).update(source="manual")
    assert _kinds_and_boxes(page) == [
        ("body", [0, 0, 1000, 1200]),
        ("footnote", [0, 1200, 1000, 1410]),
        ("page_number", [0, 1410, 1000, 1500]),
    ]
    # ... but what was detected on a page wins over the manual book line
    Preprocess.objects.filter(page=page).update(footnote_rule_y=1000)
    assert [box for kind, box in _kinds_and_boxes(page) if kind == "footnote"] == [[0, 1000, 1000, 1410]]


@pytest.mark.django_db
def test_derive_regions_page_override_wins_over_detection(book):
    page = make_page(book, 1, rule_y=1100)
    page.guides_override = {"footnote_line": 0.9}
    page.save()
    assert _kinds_and_boxes(page) == [("body", [0, 0, 1000, 1350]), ("footnote", [0, 1350, 1000, 1500])]
    page.guides_override = {"footnote_line": None}  # "this page has no footnotes", even with a rule
    page.save()
    assert _kinds_and_boxes(page) == [("body", [0, 0, 1000, 1500])]


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

    with patch("books.services.run_stage") as run_stage:
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
    with patch("books.services.run_stage") as run_stage:
        services.apply_guides(book, data, users.editor)
    assert run_stage.call_count == 0

    # invalid guides are rejected before anything is saved
    with pytest.raises(ValidationError):
        services.apply_guides(book, {**data, "header_cut": "0.45", "footnote_line": "0.4"}, users.editor)
    assert LayoutGuides.objects.get(book=book).footnote_line == 0.8


@pytest.mark.django_db
def test_set_page_guides_override_derives_and_resets(book):
    LayoutGuides.objects.create(book=book, footnote_line=0.8, source="manual")
    page = make_page(book, 1)
    with patch("books.services.run_stage") as run_stage:
        regions, enqueued = services.set_page_guides_override(page, {"footnote_line": None})
    assert enqueued is True and run_stage.call_args.args == (page, "ocr")
    assert [r.kind for r in regions] == ["body", "page_number"]
    page.refresh_from_db()
    assert page.guides_override == {"footnote_line": None}

    with patch("books.services.run_stage") as run_stage:
        regions, enqueued = services.set_page_guides_override(page, {"reset": True})
    assert enqueued is True and page.guides_override is None
    assert [r.kind for r in regions] == ["body", "footnote", "page_number"]

    with patch("books.services.run_stage") as run_stage:
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
    # no guides, no detected rule / block / page number: one body region
    assert [r.kind for r in page.regions.all()] == ["body"]

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
    assert "boom" not in page.error_message and "فشل تجهيز الصفحة" in page.error_message


# ---------------------------------------------------------------- views


@pytest.mark.django_db
def test_guides_screen_requires_an_editor(client, book, users, urls):
    url = reverse("processing:guides", kwargs={"book_id": book.pk})
    response = client.get(url)
    assert response.status_code == 302 and reverse("accounts:login") in response["Location"]
    client.force_login(users.plain)
    assert client.get(url).status_code == 403


@pytest.mark.django_db
def test_guides_url_redirects_to_the_dashboard(client, book, users, urls):
    # D67: the guides screen is gone; its address lands on the dashboard («التخطيط» mode)
    client.force_login(users.editor)
    url = reverse("processing:guides", kwargs={"book_id": book.pk})
    response = client.get(url)
    assert response.status_code == 302 and response["Location"] == f"/books/{book.pk}/?view=guides"
    response = client.get(url + "?page=4")
    assert response["Location"] == f"/books/{book.pk}/?view=guides#sheet-4"
    Book.objects.filter(pk=book.pk).update(awaits_ocr_start=True)
    response = client.get(url + "?page=²")
    assert response["Location"] == f"/books/{book.pk}/"
    assert client.post(url, {"footnote_line": "0.8"}).status_code == 405  # the form's POST branch is gone
    assert not LayoutGuides.objects.filter(book=book).exists()


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
    assert [r["kind"] for r in data["regions"]] == ["body"]
    assert data["regions"][0]["bbox"][2] == data["output"]["width"]

    # Huge originals go through the worker (eager here) as preprocess → layout, so the regions are
    # re-derived in the new gray-image space exactly like the synchronous path (F4).
    Page.objects.filter(pk=page.pk).update(width=5000, height=5000)
    old_width = data["output"]["width"]
    box = [0, 0, old_width // 2, data["output"]["height"]]
    queued = client.post(url, json.dumps({"crop_box": box}), content_type="application/json")
    assert queued.status_code == 202 and queued.json()["queued"] is True
    assert queued.json()["detail"] == "الصورة كبيرة؛ أُرسلت المعالجة إلى العامل الخلفي."
    page.refresh_from_db()
    new_width = page.preprocess.output_width
    assert new_width < old_width
    assert page.status == Page.Status.LAYOUT_DONE
    assert all(r.bbox[2] <= new_width for r in page.regions.all())
    assert page.regions.get(kind="body").bbox[2] == new_width

    reset = client.post(url, json.dumps({"reset": True}), content_type="application/json")
    assert reset.status_code == 202
    page.refresh_from_db()
    assert page.preprocess.is_manual is False

    # an excluded page is refused instead of being turned back into `preprocessed` (F19)
    Page.objects.filter(pk=page.pk).update(is_excluded=True, status=Page.Status.EXCLUDED)
    refused = client.post(url, "{}", content_type="application/json")
    assert refused.status_code == 422 and "مستثناة" in refused.json()["errors"][0]
    assert Page.objects.get(pk=page.pk).status == Page.Status.EXCLUDED

    missing = Page.objects.create(book=page.book, number=7, source_index=6)
    gone = client.post(
        reverse("api:page_preprocess", kwargs={"page_id": missing.pk}), "{}", content_type="application/json"
    )
    assert gone.status_code == 422 and "صورة أصلية" in gone.json()["errors"][0]


@pytest.mark.django_db
def test_api_page_guides_override(client, book, users):
    LayoutGuides.objects.create(book=book, footnote_line=0.8, source="manual")
    page = make_page(book, 1)
    url = reverse("api:page_guides_override", kwargs={"page_id": page.pk})
    client.force_login(users.editor)
    with patch("books.services.run_stage") as run_stage:
        response = client.post(url, json.dumps({"footnote_line": None}), content_type="application/json")
    assert response.status_code == 200, response.content
    data = response.json()
    assert [r["kind"] for r in data["regions"]] == ["body", "page_number"]
    assert data["regions"][0]["label"] == "متن" and data["regions"][0]["bbox"] == [0, 0, 1000, 1410]
    assert data["effective"]["footnote_line"] is None and data["override"] == {"footnote_line": None}
    assert data["guides"]["derived"] is True and data["undo"] is None  # «المعالجة»: re-read, no undo
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
    assert "إعادة التجهيز" in html and "استعادة القيم التلقائية" in html
    config = json_block(html, "preprocess-config")
    assert config["api_url"] == f"/api/pages/{page.pk}/preprocess/"
    assert config["has_preprocess"] is True and config["stats"]["footnote_rule_y"] == 1200
    assert config["guides"]["page_number_zone"] == "bottom"

    empty = render_to_string("processing/_preprocess_panel.html", {"page": None, "book": None})
    assert "لا توجد صفحة" in empty and "preprocess-config" not in empty

    bare = Page.objects.create(book=book, number=5, source_index=4)
    html2 = render_to_string("processing/_preprocess_panel.html", {"page": bare, "book": book})
    config2 = json_block(html2, "preprocess-config")
    assert config2["has_preprocess"] is False and "تشغيل التجهيز" in html2


# ---------------------------------------------------------------- review regressions


@pytest.mark.django_db
def test_guides_override_on_an_excluded_page_is_refused_and_keeps_the_status(client, book, users):
    # F5: no 500 and no `layout_done` on an excluded page
    LayoutGuides.objects.create(book=book, footnote_line=0.8)
    page = make_page(book, 1)
    Page.objects.filter(pk=page.pk).update(is_excluded=True, status=Page.Status.EXCLUDED)
    client.force_login(users.editor)
    url = reverse("api:page_guides_override", kwargs={"page_id": page.pk})
    response = client.post(url, json.dumps({"footnote_line": 0.7}), content_type="application/json")
    assert response.status_code == 422 and "مستثناة" in response.json()["errors"][0]
    page.refresh_from_db()
    assert page.status == Page.Status.EXCLUDED and not page.regions.exists()
    # the service itself never moves an excluded page to layout_done
    services.derive_regions(page)
    assert Page.objects.get(pk=page.pk).status == Page.Status.EXCLUDED


@pytest.mark.django_db
def test_approved_pages_keep_their_regions_when_guides_or_preprocessing_change(client, book, users):
    # backend-1: an approved page is frozen until it is reopened (no re-derive, no re-OCR, no status change)
    approved = make_page(book, 1, rule_y=1200)
    pending = make_page(book, 2, rule_y=1200)
    for page in (approved, pending):
        services.derive_regions(page)
    Page.objects.filter(pk=approved.pk).update(
        status=Page.Status.REVIEWED, text_state=Page.TextState.FINAL, reviewed_at="2026-09-24T10:00:00Z"
    )
    before = _kinds_and_boxes_stored(approved)
    data = {
        "header_cut": "0.05",
        "footnote_line": "",
        "page_number_zone": "none",
        "page_number_height": "0.06",
    }
    with patch("books.services.run_stage") as run_stage:
        services.apply_guides(book, data, users.editor)
    assert [call.args[0].pk for call in run_stage.call_args_list] == [pending.pk]
    approved.refresh_from_db()
    assert approved.status == Page.Status.REVIEWED and _kinds_and_boxes_stored(approved) == before

    with pytest.raises(services.ProcessingError, match="الصفحة معتمدة"):
        services.set_page_guides_override(approved, {"footnote_line": None})
    with pytest.raises(services.ProcessingError, match="الصفحة معتمدة"):
        services.rerun_preprocess(approved, None)
    client.force_login(users.editor)
    url = reverse("api:page_guides_override", kwargs={"page_id": approved.pk})
    response = client.post(url, json.dumps({"footnote_line": 0.7}), content_type="application/json")
    assert response.status_code == 422 and "أعد فتحها" in response.json()["errors"][0]
    response = client.post(
        reverse("api:page_preprocess", kwargs={"page_id": approved.pk}),
        json.dumps({"reset": True}),
        content_type="application/json",
    )
    assert response.status_code == 422
    approved.refresh_from_db()
    assert approved.status == Page.Status.REVIEWED and approved.guides_override is None
    assert _kinds_and_boxes_stored(approved) == before


@pytest.mark.django_db
def test_drop_detected_page_number_only_forgets_a_detected_box(book):
    # backend-2: a detected "page number" that OCR read as words joins the body
    page = make_page(book, 1)
    Preprocess.objects.filter(page=page).update(
        page_number_box={"bbox": [900, 1440, 950, 1470], "position": "bottom"}
    )
    services.derive_regions(page)
    assert [r.kind for r in page.regions.order_by("order")] == ["body", "page_number"]
    assert services.drop_detected_page_number(page) is True
    assert Preprocess.objects.get(page=page).page_number_box is None
    assert _kinds_and_boxes_stored(page) == [("body", [0, 0, 1000, 1500])]
    assert services.drop_detected_page_number(page) is False  # nothing left to drop

    # a zone the owner set (page override) is their decision: never dropped
    other = make_page(book, 2)
    Preprocess.objects.filter(page=other).update(
        page_number_box={"bbox": [900, 1440, 950, 1470], "position": "bottom"}
    )
    other.guides_override = {"page_number_zone": "bottom"}
    other.save()
    services.derive_regions(other)
    assert services.drop_detected_page_number(other) is False
    assert Preprocess.objects.get(page=other).page_number_box is not None


def _kinds_and_boxes_stored(page: Page) -> list[tuple[str, list[int]]]:
    return [(r.kind, [int(v) for v in r.bbox]) for r in page.regions.order_by("order", "pk")]


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["api:page_preprocess", "api:page_guides_override"])
def test_mutating_json_routes_require_the_editor_role(client, book, users, name):
    # F56: anonymous and non-editor users are refused before anything runs
    page = make_page(book, 1)
    url = reverse(name, kwargs={"page_id": page.pk})
    with (
        patch("processing.services.set_page_guides_override") as override,
        patch("processing.services.rerun_preprocess") as rerun,
    ):
        assert client.post(url, "{}", content_type="application/json").status_code == 403
        client.force_login(users.plain)
        response = client.post(url, "{}", content_type="application/json")
        assert response.status_code == 403 and "محرّر" in response.json()["detail"]
    override.assert_not_called()
    rerun.assert_not_called()


@pytest.mark.django_db
def test_json_api_enforces_csrf_and_base_layout_exposes_the_token(book, users):
    # F60: processing.js sends X-CSRFToken read from <meta name="csrf-token"> in base.html
    from django.test import Client

    page = make_page(book, 1)
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(users.editor)
    url = reverse("api:page_guides_override", kwargs={"page_id": page.pk})
    with patch("books.services.run_stage"):
        assert strict.post(url, "{}", content_type="application/json").status_code == 403
        html = strict.get(reverse("books:detail", args=[book.pk])).content.decode()
        token = re.search(r'<meta name="csrf-token" content="([^"]+)"', html).group(1)
        ok = strict.post(url, "{}", content_type="application/json", HTTP_X_CSRFTOKEN=token)
    assert ok.status_code == 200


@pytest.mark.parametrize("requested", ["²", "abc", "", None, "٢", "4"])
def test_parse_page_number_ignores_non_decimal_page_numbers(requested):
    # F23: "²" passes str.isdigit() but int() rejects it; Arabic-Indic digits are accepted
    expected = {"٢": 2, "4": 4}.get(requested)
    assert services.parse_page_number(requested) == expected


# ---------------------------------------------------------------- Phase 7a: thick rules (D68)

RULE_LINES = [
    {"x0": 100, "y0": 400, "x1": 900, "y1": 420},
    {"x0": 100, "y0": 950, "x1": 900, "y1": 970},
    {"x0": 100, "y0": 1100, "x1": 900, "y1": 1120},
]


def _blank(width: int = 1000, height: int = 1500) -> np.ndarray:
    page = np.full((height, width), 255, dtype=np.uint8)
    for ln in RULE_LINES:  # specks where the lines are (too far apart to close into a bar), so Otsu sees ink
        for x in range(ln["x0"], ln["x1"], 40):
            page[ln["y0"] + 6 : ln["y1"] - 6, x : x + 3] = 0
    return page


def test_a_thick_rule_is_accepted_when_there_is_no_strict_one():
    # book 25: 7 px bars at a line height of 20 (limit max(3, 0.3 × 20) = 6), fill ≥ 0.6
    gray = _blank()
    gray[900:907, 500:900] = 0
    y = pipeline.detect_footnote_rule(gray, RULE_LINES, 20.0)
    assert y is not None and abs(y - 903) <= 1
    # before D68 the same bar was refused: thicker than the strict limit
    assert pipeline.NEAR_RULE_THICKNESS == 0.5 and pipeline.RULE_MIN_FILL == 0.6


def test_a_closed_text_row_with_a_low_fill_stays_refused():
    # a 6 px band with 10 px "ascenders" every 50 px: mean thickness 8 (thick range), fill 0.5
    gray = _blank()
    gray[910:916, 300:900] = 0
    for x in range(300, 900, 50):
        gray[900:910, x : x + 10] = 0
    assert pipeline.detect_footnote_rule(gray, RULE_LINES, 20.0) is None


def test_a_strict_rule_wins_over_a_wider_thick_bar():
    # book 16 page 2: a 3 px rule and a wider, thicker bar; a single "widest wins" would move the rule
    gray = _blank()
    gray[900:903, 600:900] = 0
    gray[1000:1007, 200:900] = 0
    y = pipeline.detect_footnote_rule(gray, RULE_LINES, 20.0)
    assert y is not None and abs(y - 901) <= 1
    too_thick = _blank()
    too_thick[1000:1012, 200:900] = 0  # 12 px > 0.5 × 20: a text row, not a rule
    assert pipeline.detect_footnote_rule(too_thick, RULE_LINES, 20.0) is None


# ---------------------------------------------------------------- Phase 7a: bands before regions (§3.9)


def _pre(page: Page, **kwargs) -> Preprocess:
    defaults = {
        "output_width": 1000,
        "output_height": 1500,
        "line_boxes": [
            {"x0": 100, "y0": 100, "x1": 900, "y1": 130},
            {"x0": 100, "y0": 1100, "x1": 900, "y1": 1130},
        ],
        "n_lines": 2,
        "footnote_rule_y": None,
        "footnote_block_y": None,
        "page_number_box": None,
    }
    defaults.update(kwargs)
    pre, _ = Preprocess.objects.update_or_create(page=page, defaults=defaults)
    return pre


@pytest.mark.django_db
@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize(
    "override",
    [
        None,
        {},
        {"footnote_line": None},
        {"footnote_line": 0.7},
        {"page_number_zone": "none"},
        {"header_cut": 0.05},
    ],
)
@pytest.mark.parametrize("detected", ["rule", "block", "none"])
def test_resolve_layout_and_page_bands_equal_page_layout_and_page_region_specs(
    book, manual, override, detected
):
    LayoutGuides.objects.create(
        book=book,
        header_cut=0.04,
        footnote_line=0.85,
        page_number_zone="bottom",
        source="manual" if manual else "auto",
    )
    page = make_page(book, 1)
    Page.objects.filter(pk=page.pk).update(guides_override=override)
    page.refresh_from_db()
    pre = _pre(
        page,
        footnote_rule_y=1000 if detected == "rule" else None,
        footnote_block_y=1050 if detected == "block" else None,
        page_number_box={"bbox": [480, 1420, 520, 1450], "position": "bottom"}
        if detected != "none"
        else None,
    )
    guides = LayoutGuides.objects.get(book=book)
    values = services.guides_values(guides)
    assert services.resolve_layout(values, manual, override, pre) == services.page_layout(page, pre)
    assert services.page_bands(pre, values, manual, override) == services.page_region_specs(page, pre)


@pytest.mark.django_db
def test_page_layout_costs_one_query(book, django_assert_num_queries):
    page = make_page(book, 1, rule_y=1000)
    pre = Preprocess.objects.get(page=page)
    with django_assert_num_queries(1):
        services.page_layout(page, pre)


@pytest.mark.django_db
def test_layout_doubts_one_per_code_and_their_suppression(book):
    page = make_page(book, 1)
    values, manual = services.guides_values(None), False

    def doubts(pre, override=None):
        layout = services.resolve_layout(values, manual, override, pre)
        return services.layout_doubts(pre, layout, override, services.layout_specs(layout, pre))

    assert doubts(_pre(page, footnote_rule_y=1000)) == []
    assert doubts(_pre(page, footnote_block_y=1050)) == ["footnote_from_type"]
    assert doubts(_pre(page, footnote_block_y=1050), {"footnote_line": 0.7}) == []  # an explicit choice
    assert doubts(_pre(page, footnote_rule_y=1115)) == ["line_cut"]  # the rule runs through a line
    assert doubts(_pre(page, footnote_rule_y=1102)) == []  # within 2 px of the edge: no cut
    assert doubts(_pre(page), {"header_cut": 0.1}) == ["text_hidden"]  # a full line in the running head
    narrow = [{"x0": 400, "y0": 100, "x1": 600, "y1": 130}, {"x0": 100, "y0": 1100, "x1": 900, "y1": 1130}]
    assert doubts(_pre(page, line_boxes=narrow), {"header_cut": 0.1}) == []  # a running head is narrow
    assert doubts(_pre(page, line_boxes=[], n_lines=0)) == ["no_lines"]
    # a rule that `detect_lines` kept as a thin box lies inside it: not a cut (book 25 after D68)
    rule_box = {"x0": 500, "y0": 996, "x1": 900, "y1": 1004}
    lines = [*_pre(page).line_boxes, rule_box]
    assert doubts(_pre(page, line_boxes=lines, median_line_height=30, footnote_rule_y=1000)) == []
    assert doubts(_pre(page, line_boxes=lines, median_line_height=12, footnote_rule_y=1000)) == ["line_cut"]
    assert services.doubt_items(["line_cut"]) == [{"code": "line_cut", "label": "خط يقطع سطرًا"}]


# ---------------------------------------------------------------- Phase 7a: guide edits and the stage rule


@pytest.fixture
def waiting(book) -> Book:
    Book.objects.filter(pk=book.pk).update(awaits_ocr_start=True, status=Book.Status.NEEDS_GUIDES)
    book.refresh_from_db()
    return book


@pytest.mark.django_db
def test_guide_edits_in_the_layout_stage_only_save(waiting):
    pages = [make_page(waiting, n, rule_y=1000 if n == 1 else None) for n in (1, 2)]
    services.propose_guides(waiting)
    with patch("books.services.run_stage") as run_stage:
        answer = services.apply_book_guides(waiting, {"header_cut": 0.05}, stage="layout")
        services.set_page_guides(pages[1], {"footnote_line": 0.8}, stage="layout")
        services.set_page_guides_override(pages[0], {"footnote_line": None}, stage="layout")
    run_stage.assert_not_called()
    assert not Region.objects.filter(page__book=waiting).exists()
    assert set(waiting.pages.values_list("status", flat=True)) == {Page.Status.PREPROCESSED}
    # the inert start: only the running head became live, no zone and no proposal line
    guides = LayoutGuides.objects.get(book=waiting)
    assert (guides.source, guides.header_cut, guides.footnote_line, guides.page_number_zone) == (
        "manual",
        0.05,
        None,
        "none",
    )
    assert answer["changed"] == [1, 2] and answer["reocr"] == 0 and answer["undo"]["book"]["source"] == "auto"


@pytest.mark.django_db
def test_set_page_guides_merges_unsets_and_resets(waiting):
    page = make_page(waiting, 1)
    Page.objects.filter(pk=page.pk).update(guides_override={"header_cut": 0.05})
    page.refresh_from_db()
    result = services.set_page_guides(page, {"footnote_line": 0.8}, stage="layout")
    assert Page.objects.get(pk=page.pk).guides_override == {"header_cut": 0.05, "footnote_line": 0.8}
    assert result["undo"] == {"replace": {"header_cut": 0.05}} and result["ocr_enqueued"] is False
    services.set_page_guides(page, {}, unset=["header_cut"], stage="layout")
    assert Page.objects.get(pk=page.pk).guides_override == {"footnote_line": 0.8}
    services.set_page_guides(page, {}, reset=True, stage="layout")
    assert Page.objects.get(pk=page.pk).guides_override is None
    with pytest.raises(ValidationError):
        services.set_page_guides(page, {"header_cut": 0.4, "footnote_line": 0.3}, stage="layout")
    with pytest.raises(ValidationError):
        services.set_page_guides(page, {}, unset=["colour"], stage="layout")
    with pytest.raises(services.GuidesConflict):
        services.set_page_guides(page, {"footnote_line": 0.8}, stage="ocr")


@pytest.mark.django_db
def test_preview_counts_changed_cut_kept_and_locked_in_few_queries(book, django_assert_max_num_queries):
    from ocr.models import Line

    LayoutGuides.objects.create(book=book, header_cut=None, source="manual", page_number_zone="none")
    pages = []
    for n in range(1, 9):
        page = make_page(book, n)
        _pre(page)
        pages.append(page)
    Page.objects.filter(pk=pages[2].pk).update(guides_override={"header_cut": 0.02})
    for page in pages:
        page.refresh_from_db()
        services.derive_regions(page)
    Page.objects.filter(pk=pages[0].pk).update(
        status=Page.Status.REVIEWED, reviewed_at="2026-09-26T10:00:00Z"
    )
    Line.objects.create(page=pages[1], order=0, is_reviewed=True)
    with django_assert_max_num_queries(6):
        preview = services.preview_book_guides(book, {"header_cut": 0.08}, stage="ocr")
    assert preview["locked"] == [1, 2] and preview["kept_overrides"] == [3]
    assert preview["pages"] == [4, 5, 6, 7, 8] and preview["changed"] == 5 and preview["reocr"] == 5
    assert preview["cut"] == [4, 5, 6, 7, 8]  # 0.08 × 1500 = 120 runs through the first line
    assert preview["minutes"] == 2  # 5 pages × 20 s without Qari runs
    # reset_overrides takes page 3 along
    again = services.preview_book_guides(book, {"header_cut": 0.08}, ["header_cut"], stage="ocr")
    assert again["kept_overrides"] == [] and 3 in again["pages"]


@pytest.mark.django_db
def test_in_ocr_only_unlocked_changed_pages_are_rederived_and_reread(book):
    from ocr.models import Line

    LayoutGuides.objects.create(book=book, source="manual", page_number_zone="none")
    pages = [make_page(book, n) for n in (1, 2, 3)]
    for page in pages:
        _pre(page)
        services.derive_regions(page)
    Page.objects.filter(pk=pages[0].pk).update(
        status=Page.Status.REVIEWED, reviewed_at="2026-09-26T10:00:00Z"
    )
    Line.objects.create(page=pages[1], order=0, is_reviewed=True)
    with patch("processing.services._run_stage") as run_stage:
        answer = services.apply_book_guides(book, {"header_cut": 0.03}, stage="ocr")
    assert [call.args[0].number for call in run_stage.call_args_list] == [3]
    assert answer["changed"] == [3] and answer["undo"] is None
    for page in pages[:2]:
        assert not page.regions.filter(kind="running_header").exists()
    # the full legacy form follows the same rule
    with patch("processing.services._run_stage") as run_stage:
        services.apply_guides(book, {"header_cut": "0.04", "page_number_zone": "none"})
    assert [call.args[0].number for call in run_stage.call_args_list] == [3]
    # a page with review work is refused (422 in the API)
    with pytest.raises(services.ProcessingError, match="تصحيحات مراجعة"):
        services.set_page_guides(Page.objects.get(pk=pages[1].pk), {"footnote_line": 0.8}, stage="ocr")
    with pytest.raises(services.ProcessingError, match="تصحيحات مراجعة"):
        services.set_page_guides_override(Page.objects.get(pk=pages[1].pk), {"footnote_line": 0.8})


@pytest.mark.django_db
def test_rerun_preprocess_still_rederives_a_page_with_review_work(page_with_original):
    # §2.6: freezing regions inside `_derive_regions` would leave them in the old image's pixels
    from ocr.models import Line

    page = page_with_original
    services.preprocess_page(page)
    services.derive_regions(page)
    Line.objects.create(page=page, order=0, is_reviewed=True)
    with patch("processing.services.derive_regions", wraps=services.derive_regions) as derive:
        services.rerun_preprocess(page, {"angle": 0.5})
    derive.assert_called_once()


@pytest.mark.django_db
def test_preprocess_task_refreshes_a_waiting_book_and_layout_page_waits(book):
    Book.objects.filter(pk=book.pk).update(awaits_ocr_start=True, status=Book.Status.PROCESSING)
    first = Page.objects.create(book=book, number=1, source_index=0, width=900, height=1200)
    second = Page.objects.create(book=book, number=2, source_index=1, width=900, height=1200)
    for page in (first, second):
        page.original_image.save("original.png", ContentFile(png_bytes(render_page(n_lines=8))), save=True)
    with patch("processing.services.refresh_waiting_book", wraps=services.refresh_waiting_book) as refresh:
        tasks.preprocess_page.delay(first.pk).get()
        book.refresh_from_db()
        assert book.status == Book.Status.PROCESSING  # page 2 is still being prepared
        tasks.preprocess_page.delay(second.pk).get()
    assert refresh.call_count == 2
    book.refresh_from_db()
    assert book.status == Book.Status.NEEDS_GUIDES
    assert tasks.layout_page.delay(first.pk).get() == first.pk
    first.refresh_from_db()
    assert first.status == Page.Status.PREPROCESSED and not first.regions.exists()
    # a book whose «المعالجة» started is not touched by the preprocess refresh
    Book.objects.filter(pk=book.pk).update(awaits_ocr_start=False, status=Book.Status.OCR)
    services.refresh_waiting_book(book.pk)
    book.refresh_from_db()
    assert book.status == Book.Status.OCR


@pytest.mark.django_db
def test_preprocess_api_refreshes_a_waiting_book(client, page_with_original, users):
    from books.models import ALL_PAGES_FAILED_LAYOUT

    page = page_with_original
    Book.objects.filter(pk=page.book_id).update(
        awaits_ocr_start=True, status=Book.Status.ERROR, error_message=ALL_PAGES_FAILED_LAYOUT
    )
    page.set_error("preprocess", "فشل")
    client.force_login(users.editor)
    response = client.post(
        reverse("api:page_preprocess", kwargs={"page_id": page.pk}), "{}", content_type="application/json"
    )
    assert response.status_code == 200, response.content
    book = Book.objects.get(pk=page.book_id)
    assert book.status == Book.Status.NEEDS_GUIDES and book.error_message == ""


@pytest.mark.django_db
def test_book_guides_api_enforces_csrf(book, users):
    from django.test import Client

    Book.objects.filter(pk=book.pk).update(awaits_ocr_start=True, status=Book.Status.NEEDS_GUIDES)
    make_page(book, 1)
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(users.editor)
    url = reverse("api:book_guides", kwargs={"book_id": book.pk})
    body = json.dumps({"set": {"header_cut": 0.05}, "stage": "layout"})
    assert strict.post(url, body, content_type="application/json").status_code == 403
    assert strict.get(url).status_code == 200
    assert not LayoutGuides.objects.filter(book=book, source="manual").exists()


# ---------------------------------------------------------------- 7 review: «إزالة الضبط العام», «التلقائي»


@pytest.mark.django_db
@pytest.mark.parametrize("awaits", [True, False])
def test_the_reset_preview_names_the_pages_the_reset_changes(book, users, client, awaits):
    # the side panel previews «إزالة الضبط العام» with `{reset: true}` (processing.js draftBody): the preview
    # must say what the apply then does, or «تطبيق على كل الصفحات» stays disabled
    Book.objects.filter(pk=book.pk).update(
        awaits_ocr_start=awaits, status=Book.Status.NEEDS_GUIDES if awaits else Book.Status.READY_FOR_REVIEW
    )
    LayoutGuides.objects.create(book=book, header_cut=0.05, source="manual", page_number_zone="none")
    pages = []
    for n in (1, 2, 3):
        page = make_page(book, n)
        _pre(page)
        pages.append(page)
    if not awaits:
        for page in pages:
            page.refresh_from_db()
            services.derive_regions(page)
    client.force_login(users.editor)
    body = json.dumps({"reset": True, "stage": "layout" if awaits else "ocr"})
    preview = client.post(
        reverse("api:book_guides_preview", args=[book.pk]), body, content_type="application/json"
    ).json()
    assert LayoutGuides.objects.get(book=book).source == "manual"  # a preview writes nothing
    with patch("processing.services._run_stage") as run_stage:
        applied = client.post(
            reverse("api:book_guides", args=[book.pk]), body, content_type="application/json"
        ).json()
    assert applied["changed"] == [1, 2, 3]  # the running head goes from every page
    assert preview["changed"] == 3 and preview["pages"] == applied["changed"]
    assert preview["kept_overrides"] == [] and preview["locked"] == []
    assert preview["reocr"] == run_stage.call_count == (0 if awaits else 3)
    assert (preview["minutes"] is None) is awaits


@pytest.mark.django_db
def test_a_pending_automatic_then_a_drag_saves_the_dragged_line(book, users, client):
    # a started book: «التلقائي» (pending), then a drag of the running head; «حفظ وإعادة التعرّف على الصفحة»
    # posts both (processing.js change() keeps `reset`): the override is cleared, then the drag applies
    Book.objects.filter(pk=book.pk).update(awaits_ocr_start=False, status=Book.Status.READY_FOR_REVIEW)
    LayoutGuides.objects.create(book=book, source="manual", page_number_zone="none")
    page = make_page(book, 4)
    _pre(page)
    Page.objects.filter(pk=page.pk).update(guides_override={"footnote_line": 0.8})
    page.refresh_from_db()
    services.derive_regions(page)
    client.force_login(users.editor)
    body = {"merge": True, "set": {"header_cut": 0.07}, "unset": [], "reset": True, "stage": "ocr"}
    with patch("processing.services._run_stage") as run_stage:
        answer = client.post(
            reverse("api:page_guides_override", args=[page.pk]),
            json.dumps(body),
            content_type="application/json",
        ).json()
    page.refresh_from_db()
    assert page.guides_override == {"header_cut": 0.07}  # the old footnote line went, the drag stayed
    assert answer["guides"]["lines"]["header"]["y"] == 0.07 and run_stage.call_count == 1
    # the reset alone still clears the override, and the order of the two lines is still checked
    with patch("processing.services._run_stage"):
        services.set_page_guides(page, {}, reset=True, stage="ocr")
    assert Page.objects.get(pk=page.pk).guides_override is None
    with pytest.raises(ValidationError):
        services.set_page_guides(page, {"header_cut": 0.4, "footnote_line": 0.3}, reset=True, stage="ocr")
