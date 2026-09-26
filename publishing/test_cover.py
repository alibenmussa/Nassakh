"""Tests of the cover (D80, COVER_SPEC §3, §5): the settings, the page it draws and the rows it adds.

- **Settings:** `cover_settings` fills the defaults and mends bad stored values, `preset` follows the two
  colours; `PageSetup.cover` never enters `as_dict()` (the preview, layout and export hashes of a book
  without a cover are what they were), `Front.cover` only for a cover that can be drawn; the image is
  looked up once, of its own book, with its file.
- **The page:** the markup of each mode; the three fits' image boxes on a portrait and a landscape image
  (PyMuPDF `get_image_info`); the centre block centred in the page and the bottom block at its distance
  from the trim; the text in the book's faces; the print cover's boxes, its background in the bleed and
  never in the slug; the hash; raster sizes and kinds.
- **Prepending:** the outline, the links and the named destinations stay on their pages, the labels read
  «غلاف», 1, 2….
- **Readiness:** `cover_image_missing` and `cover_resolution` (warn for print, info otherwise), and the
  export page's cover rows equal the contract (`editor/fixtures/cover/exports.json`).
"""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from books.models import Book
from editor.models import BookImage, Manuscript, StyleSheet
from editor.tests import (
    COVER_FIELDS_80,
    check_cover_contract,
    cover_file,
    encoded,
    picture,
    role_user,
    sideways_jpeg,
    upload,
)
from publishing import cover as C
from publishing.engine import CROP_SLUG_MM, PdfOutput
from publishing.model import (
    COVER_DEFAULTS,
    CoverImage,
    CoverSpec,
    book_model,
    cover_settings,
    cover_spec,
    page_setup,
    preset_of,
)
from publishing.pdf import finisher_for

MM = 72 / 25.4
TRIM = (170.0, 240.0)


def base_setup(**values):
    return page_setup({"front_matter": {"fields": dict(COVER_FIELDS_80)}, **values})


def cover_model(spec: CoverSpec | None, document=None, **values):
    """The book model of a cover: the setup's book details and `spec` as its cover."""
    setup = replace(base_setup(**values), cover=spec)
    return book_model(document or {"type": "doc", "content": []}, setup, title="كتاب")


def image_of(tmp_path: Path, width: int, height: int, name: str = "picture.jpg") -> CoverImage:
    path = tmp_path / name
    path.write_bytes(encoded(picture(width, height), "JPEG", quality=85))
    return CoverImage(7, str(path), width, height, "a" * 64, "jpeg")


def spans_mm(pdf: bytes) -> list[tuple[str, tuple[float, ...], str]]:
    """Every text span of page 1: `(text, bbox in mm from the top left, font)`."""
    out = []
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        for block in document[0].get_text("dict")["blocks"]:
            for line in block.get("lines", ()):
                for span in line["spans"]:
                    if span["text"].strip():
                        out.append((span["text"], tuple(value / MM for value in span["bbox"]), span["font"]))
    return out


# ====================================================================== settings


def test_the_settings_have_their_defaults_and_bad_values_are_mended():
    assert cover_settings(None) == COVER_DEFAULTS and cover_settings("info") == COVER_DEFAULTS
    bad = {
        "mode": "poster",
        "image": True,
        "fit": "stretch",
        "center": 5,
        "center_pt": 200,
        "bottom_pt": "9",
        "background": "red",
        "color": "#12345",
        "bottom_mm": -1,
    }
    assert cover_settings(bad) == COVER_DEFAULTS
    good = cover_settings(
        {
            "mode": "text",
            "image": 3,
            "fit": "width",
            "center": " أ  ب \n\nج \n",
            "center_pt": 30,
            "bottom_mm": 12,
            "background": "#1D2433",
            "color": "#F3EFE6",
            "preset": "white",
        }
    )
    assert (good["mode"], good["image"], good["fit"], good["center"]) == ("text", 3, "width", "أ ب\n\nج")
    assert (good["center_pt"], good["bottom_mm"], good["background"], good["preset"]) == (
        30.0,
        12.0,
        "#1d2433",
        "navy",
    )
    assert cover_settings({"center": "ن" * 700})["center"] == "ن" * 600
    assert preset_of("#ffffff", "#1b1b1b") == "white" and preset_of("#ffffff", "#000000") == "custom"


def test_the_cover_never_enters_the_page_hashes():
    from publishing.exports import stylesheet_hash
    from publishing.preview import setup_hash

    plain = base_setup()
    covered = page_setup({"front_matter": {"fields": dict(COVER_FIELDS_80), "cover": {"mode": "info"}}})
    assert plain.cover is None and covered.cover == CoverSpec(mode="info")
    assert "cover" not in covered.as_dict() and covered.as_dict() == plain.as_dict()
    assert setup_hash(covered) == setup_hash(plain)
    # the export's «تغيّر التنسيق» sees a cover that is drawn, and only then
    assert stylesheet_hash(covered) != stylesheet_hash(plain)
    off = page_setup(
        {"front_matter": {"fields": dict(COVER_FIELDS_80), "cover": {"mode": "none", "center": "x"}}}
    )
    assert off.cover is None and stylesheet_hash(off) == stylesheet_hash(plain)
    missing = page_setup({"front_matter": {"cover": {"mode": "image"}}})
    assert missing.cover.mode == "image" and not missing.cover.ready
    assert stylesheet_hash(missing) == stylesheet_hash(page_setup({}))
    assert book_model({}, missing).front.cover is None and book_model({}, covered).front.cover.mode == "info"


@pytest.mark.django_db
def test_the_image_is_looked_up_of_its_own_book(tmp_path):
    from editor import services

    book, other = Book.objects.create(title="أ"), Book.objects.create(title="ب")
    row, _created = services.upload_image(book, io.BytesIO(encoded(picture(60, 90), "JPEG")))
    found = cover_spec({"mode": "image", "image": row.pk}, book.pk)
    assert found.ready and found.image.width == 60 and found.image.sha256 == row.sha256
    assert Path(found.image.path).is_file()
    assert cover_spec({"mode": "image", "image": row.pk}, other.pk).image is None
    sheet = StyleSheet.objects.create(book=book, front_matter={"cover": {"mode": "image", "image": row.pk}})
    assert page_setup(sheet).cover.image.id == row.pk
    Path(found.image.path).unlink()
    assert cover_spec({"mode": "image", "image": row.pk}, book.pk).image is None  # its file is gone


# ====================================================================== the page


def test_the_markup_of_each_mode(tmp_path):
    info = C.cover_html(cover_model(CoverSpec(mode="info")))
    assert '<p class="nk-cover-title">الأمالي</p>' in info
    assert '<p class="nk-cover-sub">مجالس في الأدب</p>' in info
    assert '<p class="nk-cover-author">أبو علي القالي</p>' in info
    assert '<div class="nk-cover-bottom"><p class="nk-cover-foot">دار المدار، طرابلس، 2026</p></div>' in info
    text = C.cover_html(cover_model(CoverSpec(mode="text", center="أ <ب>\n\nج", bottom="")))
    assert (
        '<p class="nk-cover-title">أ &lt;ب&gt;<br/>&#160;<br/>ج</p>' in text and "nk-cover-bottom" not in text
    )
    image = C.cover_html(cover_model(CoverSpec(mode="image", image=image_of(tmp_path, 60, 90))))
    assert 'class="nk-cover-img" src="file://' in image and "nk-cover-center" not in image
    css = C.cover_css(
        cover_model(CoverSpec(mode="info", background="#1d2433")), C.F.resolve("amiri", "times", "amiri")
    )
    assert "@page { size: 170mm 240mm; margin: 0; }" in css and "background: #1d2433" in css
    with pytest.raises(ValueError):
        C.cover_html(cover_model(None))


FITS = {
    # (image pixels, fit) → the image's box in mm (left, top, right, bottom) on 170 × 240
    ((600, 1200), "fill"): (0, -50, 170, 290),
    ((600, 1200), "width"): (0, -50, 170, 290),
    ((600, 1200), "height"): (25, 0, 145, 240),
    ((1200, 800), "fill"): (-95, 0, 265, 240),
    ((1200, 800), "width"): (0, 63.333, 170, 176.667),
    ((1200, 800), "height"): (-95, 0, 265, 240),
}


@pytest.mark.parametrize(("size", "fit"), list(FITS))
def test_the_three_fits_place_the_image(tmp_path, size, fit):
    spec = CoverSpec(mode="image", fit=fit, image=image_of(tmp_path, *size), background="#4a1d24")
    pdf = C.render_cover(cover_model(spec))
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        page = document[0]
        (info,) = page.get_image_info()
        box = tuple(value / MM for value in info["bbox"])
        assert box == pytest.approx(FITS[(size, fit)], abs=0.3)
        pix = page.get_pixmap(dpi=20)
        # where the image does not reach, the page's background shows
        if fit == "width" and size == (1200, 800):
            assert pix.pixel(pix.width // 2, 2) == pytest.approx((0x4A, 0x1D, 0x24), abs=2)
    assert C.effective_dpi(spec, *TRIM) == pytest.approx(
        size[0] / ((FITS[(size, fit)][2] - FITS[(size, fit)][0]) / 25.4)
    )


def test_the_text_is_centred_and_the_bottom_text_sits_at_its_distance():
    pdf = C.render_cover(cover_model(CoverSpec(mode="info", background="#1d2433", color="#f3efe6")))
    spans = spans_mm(pdf)
    texts = [text for text, _box, _font in spans]
    assert texts[:3] == ["الأمالي", "مجالس في الأدب", "أبو علي القالي"]  # the Arabic reads in order
    # (the imprint's line mixes directions: PyMuPDF hands its runs back in the line's visual order)
    assert sorted(texts[3].replace("،", " ").split()) == sorted(["دار", "المدار", "طرابلس", "2026"])
    centre = [box for text, box, _font in spans[:3]]
    for box in centre:
        assert (box[0] + box[2]) / 2 == pytest.approx(85, abs=0.5)  # centred across the page
    middle = (centre[0][1] + centre[-1][3]) / 2
    assert middle == pytest.approx(120, abs=2.5)  # the block's middle on the page's middle
    foot = spans[3][1]
    assert (foot[0] + foot[2]) / 2 == pytest.approx(85, abs=0.5)
    assert 240 - foot[3] == pytest.approx(22 + 8, abs=2.5)  # the bottom margin + 8 mm
    fonts = {font for _text, _box, font in spans}
    assert all("Amiri" in font or "nk-" in font for font in fonts), fonts  # the book's faces
    raised = spans_mm(C.render_cover(cover_model(CoverSpec(mode="text", bottom="سطر", bottom_mm=50))))
    assert 240 - raised[0][1][3] == pytest.approx(50, abs=2.5)
    # sizes: the title at center_pt, the subtitle at 0.55 × center_pt
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        sizes = [
            span["size"]
            for block in document[0].get_text("dict")["blocks"]
            for line in block.get("lines", ())
            for span in line["spans"]
        ]
    assert sizes[0] == pytest.approx(28, abs=0.1) and sizes[1] == pytest.approx(15.4, abs=0.1)


def test_the_print_cover_runs_into_the_bleed_never_into_the_slug():
    output = PdfOutput("print", bleed_mm=3, crop_marks=True)
    spec = CoverSpec(mode="info", background="#1f3b2d", color="#f1ead8")
    pdf = C.render_cover(cover_model(spec), bleed_mm=3, slug_mm=CROP_SLUG_MM, finisher=finisher_for(output))
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        page = document[0]
        boxes = {
            key: [float(v) for v in document.xref_get_key(page.xref, key)[1].strip("[]").split()]
            for key in ("MediaBox", "TrimBox", "BleedBox")
        }
        assert boxes["TrimBox"] == pytest.approx([0, 0, 170 * MM, 240 * MM], abs=0.05)
        assert boxes["BleedBox"] == pytest.approx([-3 * MM, -3 * MM, 173 * MM, 243 * MM], abs=0.05)
        assert boxes["MediaBox"] == pytest.approx([-9 * MM, -9 * MM, 179 * MM, 249 * MM], abs=0.05)
        pix = page.get_pixmap(dpi=72)  # the whole media box: 1 px = 1 pt
        bleed = pix.pixel(round(7.5 * MM), round(100 * MM))  # 1.5 mm outside the trim, inside the bleed
        slug = pix.pixel(round(2 * MM), round(100 * MM))  # in the slug
        assert bleed == (0x1F, 0x3B, 0x2D) and slug == (255, 255, 255)
        assert len([d for d in page.get_drawings() if d.get("color") is not None]) >= 1  # the crop marks
    spans = spans_mm(pdf)
    assert (spans[0][1][0] + spans[0][1][2]) / 2 - 9 == pytest.approx(
        85, abs=0.5
    )  # still centred on the trim


def test_the_hash_follows_what_the_cover_shows(tmp_path):
    fonts = C.F.resolve("amiri", "times", "amiri")
    info = cover_model(CoverSpec(mode="info"))
    digest = C.cover_hash(info, fonts)
    assert len(digest) == 24 and C.cover_hash(info, fonts) == digest
    assert C.cover_hash(cover_model(CoverSpec(mode="info", background="#000000")), fonts) != digest
    assert (
        C.cover_hash(cover_model(CoverSpec(mode="info"), trim="a5", width_mm=148, height_mm=210), fonts)
        != digest
    )
    retitled = replace(info, front=replace(info.front, title="عنوان آخر"))
    assert C.cover_hash(retitled, fonts) != digest  # an info cover prints the title
    text = cover_model(CoverSpec(mode="text", center="نص"))
    assert C.cover_hash(replace(text, front=replace(text.front, title="آخر")), fonts) == C.cover_hash(
        text, fonts
    )
    image = image_of(tmp_path, 60, 90)
    one = C.cover_hash(cover_model(CoverSpec(mode="image", image=image)), fonts)
    other = C.cover_hash(cover_model(CoverSpec(mode="image", image=replace(image, sha256="b" * 64))), fonts)
    moved = C.cover_hash(
        cover_model(CoverSpec(mode="image", image=replace(image, path="/elsewhere.jpg"))), fonts
    )
    assert one != other and one == moved  # the image's content, never its path


def test_the_rasters_have_the_sizes_asked_for(tmp_path):
    pdf = C.render_cover(cover_model(CoverSpec(mode="text", center="نص")))
    for kwargs, size in (
        ({"height": 1100}, (779, 1100)),
        ({"height": 2200}, (1558, 2200)),
        ({"dpi": 300}, (2008, 2835)),
    ):
        data, media = C.cover_raster(pdf, **kwargs)
        with Image.open(io.BytesIO(data)) as image:
            assert image.size == size and image.mode == "RGB" and media == "image/png"
    assert C.cover_raster(pdf, height=100, kind="webp")[1] == "image/webp"
    photo = cover_model(CoverSpec(mode="image", image=image_of(tmp_path, 60, 90)))
    assert C.export_raster(photo, height=160)[1] == "image/jpeg"
    assert C.export_raster(cover_model(CoverSpec(mode="info")), height=160)[1] == "image/png"


def _interior() -> bytes:
    """Three pages with an outline, an internal link on page 1 to page 3, and a named destination."""
    from weasyprint import HTML

    html = (
        '<html lang="ar" dir="rtl"><body>'
        '<h1 style="break-before: page">الأول</h1><p><a href="#third">إلى الثالث</a></p>'
        '<h1 style="break-before: page">الثاني</h1><p>نص</p>'
        '<h1 id="third" style="break-before: page">الثالث</h1><p>نص</p></body></html>'
    )
    return HTML(string=html).write_pdf()


def test_prepending_keeps_the_outline_and_the_links_on_their_pages():
    interior = _interior()
    cover = C.render_cover(cover_model(CoverSpec(mode="info")))
    data = C.prepend_cover(interior, cover)
    with (
        pymupdf.open(stream=interior, filetype="pdf") as before,
        pymupdf.open(stream=data, filetype="pdf") as after,
    ):
        assert after.page_count == before.page_count + 1
        assert [[level, title, page + 1] for level, title, page in before.get_toc()] == after.get_toc()
        links_before = [(i, link["page"]) for i, page in enumerate(before) for link in page.get_links()]
        links_after = [(i, link["page"]) for i, page in enumerate(after) for link in page.get_links()]
        assert links_after == [(i + 1, target + 1) for i, target in links_before] and links_after
        assert C.page_labels_of(data) == ["غلاف", "1", "2", "3"]  # read by MuPDF's own decoder
        assert "الأمالي" in after[0].get_text() and "الثالث" in after[1].get_text()
        assert after.resolve_names()["third"]["page"] == before.resolve_names()["third"]["page"] + 1
    assert C.prepend_cover(interior, cover) == data  # the same inputs, the same bytes


def test_the_page_label_is_a_utf16_text_string(tmp_path):
    import subprocess
    import sys

    assert C.pdf_text_string("غلاف") == "<FEFF063A064406270641>"
    assert C.page_labels() == "<</Nums[0<</P<FEFF063A064406270641>>>1<</S/D/St 1>>]>>"
    assert C.text_string("<FEFF063A064406270641>") == "غلاف" and C.text_string("(a\\051\\(b)") == "a)(b"
    assert C.text_string("(\\376\\377\\006\\072)") == "غ" and C.text_string("<EFBBBF41>") == "A"
    # MuPDF's own reader (C `fz_page_label`) decodes the file's label as «غلاف»; it writes into the buffer
    # it is given, which Python cannot hand it safely, so it runs in a throwaway process
    path = tmp_path / "labels.pdf"
    path.write_bytes(C.prepend_cover(_interior(), C.render_cover(cover_model(CoverSpec(mode="info")))))
    script = (
        "import sys, pymupdf; d = pymupdf.open(sys.argv[1]); m = pymupdf.mupdf; "
        "labels = [m.fz_page_label(m.fz_load_page(d.this, i), ' ' * 64, 64) for i in range(3)]; "
        "sys.stdout.buffer.write('|'.join(labels).encode())"
    )
    found = subprocess.run(
        [sys.executable, "-c", script, str(path)], capture_output=True, check=True, timeout=60
    )
    assert found.stdout.decode() == "غلاف|1|2"


# ====================================================================== readiness and the export page


@pytest.fixture
def covered_book(db):
    """Book 80 of the contract with its manuscript and images 801 (1004 × 1417) and 802, and book 81's 803."""
    from django.test import Client

    book = Book.objects.create(pk=80, title="كتاب الغلاف")
    other = Book.objects.create(pk=81, title="كتاب آخر")
    StyleSheet.objects.create(book=book, front_matter={"fields": dict(COVER_FIELDS_80)})
    Manuscript.objects.create(
        book=book,
        version=1,
        document={
            "type": "doc",
            "content": [
                {"type": "title", "attrs": {"text": "الأمالي", "author": ""}},
                {
                    "type": "heading",
                    "attrs": {"level": 1, "id": "h1", "sourcePages": [1]},
                    "content": [{"type": "text", "text": "الفصل الأول"}],
                },
                {
                    "type": "paragraph",
                    "attrs": {"id": "p1", "sourcePages": [1]},
                    "content": [{"type": "text", "text": "نص الفصل الأول."}],
                },
            ],
        },
    )
    BookImage.objects.create(pk=800, book=other, sha256="0", width=1, height=1, format="png").delete()
    editor = role_user("editor-cover", "editor")
    client = Client()
    client.force_login(editor)
    assert upload(client, 80, "غلاف.jpg", sideways_jpeg()).json()["id"] == 801
    assert upload(client, 80, "logo.webp", encoded(picture(1200, 800, "RGBA"), "WEBP")).json()["id"] == 802
    assert upload(client, 81, "theirs.jpg", encoded(picture(40, 40), "JPEG")).json()["id"] == 803
    book.editor_user = editor
    return book


def set_cover(book, **cover):
    sheet = StyleSheet.objects.get(book=book)
    sheet.front_matter = {**sheet.front_matter, "cover": {**COVER_DEFAULTS, **cover}}
    sheet.save()


def rows_of(rows: list[dict], code: str) -> list[dict]:
    return [item for item in rows if item["code"] == code]


def test_the_readiness_rows_of_the_cover(covered_book):
    from publishing.readiness import book_readiness, cover_resolution_row

    assert not rows_of(book_readiness(covered_book), "cover_image_missing")
    set_cover(covered_book, mode="image")
    (missing,) = rows_of(book_readiness(covered_book), "cover_image_missing")
    assert missing["level"] == "warn" and missing["action"]["url"] == "/books/80/layout/?tab=format"
    set_cover(covered_book, mode="image", image=801)
    assert not rows_of(book_readiness(covered_book), "cover_image_missing")
    setup = page_setup(StyleSheet.objects.get(book=covered_book))
    row = cover_resolution_row(80, setup, "warn")
    assert row["message"] == "دقة صورة الغلاف 150 نقطة في البوصة على هذا القطع؛ يُستحسن 300 للطباعة."
    fine = replace(
        setup, cover=replace(setup.cover, image=replace(setup.cover.image, width=2008, height=2835))
    )
    assert cover_resolution_row(80, fine, "warn") is None  # 300 dpi as fitted
    set_cover(covered_book, mode="info")
    assert cover_resolution_row(80, page_setup(StyleSheet.objects.get(book=covered_book)), "warn") is None


def test_the_export_page_rows_equal_the_contract(covered_book, monkeypatch):
    from publishing import exports

    monkeypatch.setattr(exports, "_enqueue", lambda row: None)
    editor = covered_book.editor_user
    keys = list(cover_file("exports.json"))

    def formats() -> dict:
        return {
            item["key"]: item for item in exports.page_payload(Book.objects.get(pk=80), editor)["formats"]
        }

    set_cover(covered_book, mode="image", image=801)
    page = formats()
    with_cover = page["print_pdf"]["form"]["cover"]
    print_note = rows_of(page["print_pdf"]["notes"], "cover_resolution")[0]
    others = [rows_of(page[key]["notes"], "cover_resolution")[0] for key in ("screen_pdf", "docx", "epub")]
    assert others[0] == others[1] == others[2]
    set_cover(covered_book, mode="none")
    without = formats()["print_pdf"]["form"]["cover"]
    set_cover(covered_book, mode="info")
    options = {"bleed_mm": 3, "crop_marks": True, "cover": True}
    payload = exports.export_payload(
        exports.request_export(covered_book, "print_pdf", options, editor), editor
    )
    set_cover(covered_book, mode="image")
    readiness = exports.page_payload(Book.objects.get(pk=80), editor)["readiness"]
    contract = dict(
        zip(
            keys,
            [
                with_cover,
                without,
                {"options": payload["options"], "options_text": payload["options_text"]},
                rows_of(readiness, "cover_image_missing")[0],
                print_note,
                others[0],
            ],
            strict=True,
        )
    )
    check_cover_contract({"exports.json": contract})
