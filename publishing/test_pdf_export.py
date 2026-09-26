"""Tests of the PDF exports (PHASE6_SPEC §9, §11.4, D61): `print_pdf` and `screen_pdf`.

- **CSS:** the preview CSS is unchanged without an output and never bleeds; the print CSS has `bleed:
  9mm; marks: crop` for 3 mm with marks and only `device-cmyk` colours; the export adds only rules that
  cannot move a line (outline, bleed, marks, colours); the outline labels come from `data-label`.
- **Print PDF:** the boxes in points (TrimBox the trim, BleedBox the trim plus the bleed exactly, MediaBox
  plus the slug), the crop marks outside the bleed, `0 0 0 1 scn` and no `rg` on the pages, `Trapped`, and
  the same page count, chapter ranges and word positions as the preview job.
- **Screen PDF:** `get_toc()` levels and clean labels (no title page, «المحتويات» at level 1, no note
  call), `Direction /R2L`, `PageMode /UseOutlines`, the metadata, the contents links, the preview's pages.
- **Fonts:** the Latin-fix regression in the export, subset prefixes and no Type 3 fonts, `foreign_fonts`
  flagging a CJK character with its page.
- **Passes:** `on_pass` reports each pass, and a cancel between passes raises `RenderCancelled`.
- **The exporters:** the registry, the options and the form, the notes, `layout_mismatch` against a
  finished preview render of the same job, the pipeline end to end, determinism.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime

from django.test import override_settings

import pymupdf
import pytest

from assembly.models import AssemblyRun
from books.models import Book
from editor.models import Manuscript
from publishing import exporters, exports, fonts
from publishing.css import OUTLINE_CSS, PURE_BLACK, export_rules, pure_black, stylesheet_css
from publishing.engine import PdfMetadata, PdfOutput, RenderJob, get_engine
from publishing.exporters import ExportJob, NullProgress, OptionsError, parse_options
from publishing.html import render_markup
from publishing.model import book_model, page_setup
from publishing.models import Export, PreviewRender
from publishing.pdf import RenderCancelled, pdf_date
from publishing.pdf_export import (
    PrintPdfExporter,
    Reference,
    ScreenPdfExporter,
    audit_pdf,
    display_name,
    foreign_font_rows,
    layout_mismatch,
    pages_phrase,
)
from publishing.preview import job_hash
from publishing.tests import document, heading, long_book, note, para, text

MM = 72 / 25.4
CREATED = datetime(2026, 9, 26, 10, 2, 11, tzinfo=UTC)
LATIN_TEXT = "نص عربي https www hindawi org وبعده Latin words 123 ثم نص."


def amiri_setup(**values):
    """Amiri everywhere (vendored: every machine has it), title page and contents on."""
    return page_setup({"body_font": "amiri", "latin_font": "amiri", "heading_font": "amiri", **values})


def outline_book() -> dict:
    """Two chapters of several pages; the first has a section title that calls a footnote."""
    blocks = [heading("h1", "الفصل الأول")]
    blocks.append(
        {
            "type": "heading",
            "attrs": {"level": 2, "id": "s1", "sourcePages": [1], "sourceLineIds": []},
            "content": [text("قسم فرعي"), note("n-s1", "حاشية على العنوان", number=1)],
        }
    )
    body = long_book(chapters=2, paragraphs=5)["content"][1:]
    blocks += [block for block in body if block["type"] == "paragraph"][:5]
    blocks.append(heading("h2", "الفصل الثاني"))
    blocks += [block for block in body if block["type"] == "paragraph"][5:]
    return document(*blocks, title="كتاب التصدير", author="مؤلف الكتاب")


def render(doc: dict, setup, output=None, **kwargs):
    return get_engine().render(RenderJob(document=doc, stylesheet=setup, output=output), **kwargs)


def print_output(bleed=0, marks=False, subject="") -> PdfOutput:
    return PdfOutput("print", bleed, marks, PdfMetadata(subject=subject, created=CREATED))


def screen_output(subject="") -> PdfOutput:
    return PdfOutput("screen", metadata=PdfMetadata(subject=subject, created=CREATED))


def raw_box(pdf: pymupdf.Document, page: int, key: str) -> list[float]:
    kind, value = pdf.xref_get_key(pdf[page].xref, key)
    assert kind == "array"
    return [float(item) for item in value.strip("[]").split()]


def words(data: bytes, offset: float = 0.0) -> list[list[tuple]]:
    """Every word of every page with its position (relative to the trim's top-left corner)."""
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        return [
            [(round(w[0] - offset, 1), round(w[1] - offset, 1), w[4]) for w in page.get_text("words")]
            for page in pdf
        ]


def catalog_key(pdf: pymupdf.Document, key: str):
    return pdf.xref_get_key(pdf.pdf_catalog(), key)


@pytest.fixture(scope="module")
def outline_doc():
    return outline_book()


@pytest.fixture(scope="module")
def preview_render(outline_doc):
    return render(outline_doc, amiri_setup())


@pytest.fixture(scope="module")
def print_render(outline_doc):
    return render(outline_doc, amiri_setup(), print_output(3, True, subject="عنوان فرعي"))


@pytest.fixture(scope="module")
def screen_render(outline_doc):
    return render(outline_doc, amiri_setup(), screen_output(subject="عنوان فرعي"))


# ====================================================================== CSS


def test_the_preview_css_is_the_same_without_an_output():
    setup = amiri_setup(bleed_mm=3)
    css = stylesheet_css(setup)
    assert css == stylesheet_css(setup, output=None)
    assert "bleed" not in css and "marks:" not in css and "bookmark" not in css
    assert "device-cmyk" not in css and "color: #000" in css


@pytest.mark.parametrize(
    ("bleed", "marks", "rule"),
    [
        (3, True, "@page { bleed: 9mm; marks: crop; }"),
        (3, False, "@page { bleed: 3mm; }"),
        (0, True, "@page { bleed: 6mm; marks: crop; }"),
        (5, False, "@page { bleed: 5mm; }"),
        (0, False, None),
    ],
)
def test_the_print_css_has_the_bleed_and_marks_asked_for(bleed, marks, rule):
    css = stylesheet_css(amiri_setup(), output=print_output(bleed, marks))
    if rule is None:
        assert "bleed" not in css and "marks:" not in css
    else:
        assert rule in css and css.count("bleed:") == 1


def test_the_print_css_has_only_device_cmyk_colours():
    setup = amiri_setup(print_source_pages=True)
    css = stylesheet_css(setup, output=print_output(3, True))
    rules = [line for line in css.splitlines() if not line.startswith("@font-face")]
    assert not [line for line in rules if re.search(r"#[0-9A-Fa-f]{3,6}\b", line)]
    assert PURE_BLACK in css and "device-cmyk(0 0 0 0.667)" in css  # the scan marks' gray
    assert re.findall(r"color: ([^;]+);", "\n".join(rules)) and all(
        value.startswith("device-cmyk") or value == "inherit"
        for value in re.findall(r"color: ([^;]+);", "\n".join(rules))
    )


def test_the_screen_css_has_no_bleed_and_keeps_rgb_black():
    css = stylesheet_css(amiri_setup(bleed_mm=5), output=screen_output())
    assert "bleed" not in css and "marks:" not in css and "device-cmyk" not in css
    assert all(rule in css for rule in OUTLINE_CSS)


def test_the_export_css_adds_only_rules_that_never_move_a_line():
    """The print CSS is the preview's, recoloured, plus the outline, the bleed and the marks."""
    setup = amiri_setup(print_source_pages=True, running_header="chapter", page_number="bottom_outer")
    output = print_output(5, True)
    preview = stylesheet_css(setup)
    exported = stylesheet_css(setup, output=output)
    extra = export_rules(output)
    assert exported == pure_black(preview) + "\n" + "\n".join(extra)
    properties = {name.strip() for rule in extra for name in re.findall(r"([a-z-]+):", rule.split("{", 1)[1])}
    assert properties <= {"bookmark-level", "bookmark-label", "bleed", "marks", "color", "text-decoration"}


def test_the_outline_labels_are_the_headings_without_note_calls(outline_doc):
    model = book_model(outline_doc, amiri_setup())
    plain = render_markup(model).html
    labelled = render_markup(model, labels=True).html
    assert "data-label" not in plain
    assert re.sub(r' data-label="[^"]*"', "", labelled) == plain
    assert 'data-label="الفصل الأول"' in labelled and 'data-label="قسم فرعي"' in labelled
    assert labelled.count("data-label=") == 3  # two chapter titles and the section title, not the book title


def test_the_note_links_wrap_each_footnote_element(outline_doc):
    model = book_model(outline_doc, amiri_setup())
    plain = render_markup(model).html
    linked = render_markup(model, note_links=True).html
    wrappers = re.findall(
        r'<a class="nk-note-link" href="#(fn-[^"]+)"><span class="nk-fn" id="(fn-[^"]+)"', linked
    )
    assert wrappers and all(href == element for href, element in wrappers)
    assert len(wrappers) == plain.count('<span class="nk-fn"')
    unwrapped = re.sub(
        r'<a class="nk-note-link" href="#[^"]+">(<span class="nk-fn".*?</span>)</a>', r"\1", linked
    )
    assert unwrapped == plain


# ====================================================================== the print PDF


def test_the_print_pdf_boxes_are_the_trim_and_the_bleed_in_points(print_render):
    width, height = 170 * MM, 240 * MM
    with pymupdf.open(stream=print_render.pdf, filetype="pdf") as pdf:
        for page in range(pdf.page_count):
            trim = raw_box(pdf, page, "TrimBox")
            assert trim == pytest.approx([0, 0, 481.89, 680.31], abs=0.01)
            assert raw_box(pdf, page, "BleedBox") == pytest.approx(
                [-3 * MM, -3 * MM, width + 3 * MM, height + 3 * MM], abs=0.01
            )
            assert raw_box(pdf, page, "MediaBox") == pytest.approx(
                [-9 * MM, -9 * MM, width + 9 * MM, height + 9 * MM], abs=0.01
            )


def test_the_crop_marks_sit_outside_the_bleed(print_render):
    with pymupdf.open(stream=print_render.pdf, filetype="pdf") as pdf:
        page = pdf[2]
        # page coordinates run from the MediaBox's top-left corner: the trim starts 9 mm in
        bleed = pymupdf.Rect(6 * MM, 6 * MM, (170 + 12) * MM, (240 + 12) * MM)
        lines = [
            item["rect"] for item in page.get_drawings() if not item["rect"].width or not item["rect"].height
        ]
        assert len(lines) >= 8  # four corners, two marks each
        for rect in lines:
            outside = (
                rect.x1 <= bleed.x0 + 0.01
                or rect.x0 >= bleed.x1 - 0.01
                or rect.y1 <= bleed.y0 + 0.01
                or rect.y0 >= bleed.y1 - 0.01
            )
            assert outside, rect


def test_the_print_pdf_is_pure_black(print_render):
    with pymupdf.open(stream=print_render.pdf, filetype="pdf") as pdf:
        for page in pdf:
            content = page.read_contents()
            assert not re.search(rb"\brg\b", content)
        body = pdf[3].read_contents()
        assert b"/DeviceCMYK cs" in body and b"0 0 0 1 scn" in body


def test_the_print_pdf_has_the_preview_pages(outline_doc, preview_render, print_render):
    assert print_render.page_count == preview_render.page_count >= 4
    assert print_render.chapters == preview_render.chapters
    assert words(print_render.pdf, 9 * MM) == words(preview_render.pdf)


def test_a_print_pdf_without_bleed_has_one_box(outline_doc, preview_render):
    rendered = render(outline_doc, amiri_setup(), print_output())
    with pymupdf.open(stream=rendered.pdf, filetype="pdf") as pdf:
        media = raw_box(pdf, 0, "MediaBox")
        assert media == raw_box(pdf, 0, "TrimBox") == raw_box(pdf, 0, "BleedBox")
        assert media == pytest.approx([0, 0, 481.89, 680.31], abs=0.01)
    assert words(rendered.pdf) == words(preview_render.pdf)


def test_the_print_pdf_properties(print_render):
    with pymupdf.open(stream=print_render.pdf, filetype="pdf") as pdf:
        assert catalog_key(pdf, "ViewerPreferences") == ("dict", "<</Direction/R2L/DisplayDocTitle true>>")
        assert catalog_key(pdf, "PageMode") == ("null", "null")
        info = int(re.search(r"/Info (\d+)", pdf.pdf_trailer()).group(1))
        assert pdf.xref_get_key(info, "Trapped") == ("name", "/False")
        assert pdf.metadata["creator"] == "نسّاخ" and pdf.metadata["subject"] == "عنوان فرعي"
        assert pdf.metadata["creationDate"] == "D:20260926100211Z"
        assert pdf.metadata["title"] == "كتاب التصدير" and pdf.metadata["author"] == "مؤلف الكتاب"


# ====================================================================== the screen PDF


def test_the_screen_pdf_outline_is_clean(screen_render):
    first = {item["id"]: item["first"] for item in screen_render.chapters}
    with pymupdf.open(stream=screen_render.pdf, filetype="pdf") as pdf:
        toc = pdf.get_toc()
    assert [entry[:2] for entry in toc] == [
        [1, "المحتويات"],
        [1, "الفصل الأول"],
        [2, "قسم فرعي"],
        [1, "الفصل الثاني"],
    ]
    assert toc[0][2] == 2 and toc[1][2] == first["h1"] and toc[3][2] == first["h2"]
    assert all("(" not in entry[1] and "كتاب التصدير" != entry[1] for entry in toc)


def test_the_preview_outline_is_left_as_it_was(preview_render):
    with pymupdf.open(stream=preview_render.pdf, filetype="pdf") as pdf:
        toc = pdf.get_toc()
    assert toc[0][:2] == [1, "كتاب التصدير"]  # the preview's own outline is not the export's


def test_the_screen_pdf_opens_right_to_left_with_its_outline(screen_render):
    with pymupdf.open(stream=screen_render.pdf, filetype="pdf") as pdf:
        assert catalog_key(pdf, "ViewerPreferences") == ("dict", "<</Direction/R2L/DisplayDocTitle true>>")
        assert catalog_key(pdf, "PageMode") == ("name", "/UseOutlines")
        assert catalog_key(pdf, "Lang") == ("string", "ar")
        assert pdf.metadata["subject"] == "عنوان فرعي" and pdf.metadata["creator"] == "نسّاخ"
        assert pdf.metadata["modDate"] == "D:20260926100211Z"
        assert raw_box(pdf, 0, "MediaBox") == raw_box(pdf, 0, "TrimBox") == raw_box(pdf, 0, "BleedBox")
        assert re.search(rb"\brg\b", pdf[3].read_contents())  # RGB


def test_the_screen_pdf_contents_links_go_to_the_chapters(screen_render):
    first = {item["id"]: item["first"] for item in screen_render.chapters}
    with pymupdf.open(stream=screen_render.pdf, filetype="pdf") as pdf:
        targets = {link["page"] + 1 for link in pdf[1].get_links() if link.get("page") is not None}
    assert {first["h1"], first["h2"]} <= targets


def test_the_screen_pdf_calls_link_to_their_notes(screen_render, print_render):
    notes = 0
    with pymupdf.open(stream=screen_render.pdf, filetype="pdf") as pdf:
        for index, page in enumerate(pdf):
            to_notes = [link for link in page.get_links() if str(link.get("nameddest", "")).startswith("fn-")]
            assert all(link["page"] == index for link in to_notes)  # a note is on its call's page
            notes += len({link["nameddest"] for link in to_notes})
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", ()):
                    assert all(span["color"] == 0 for span in line["spans"])  # not a blue link
    assert notes == len(screen_render.footnotes) >= 6
    with pymupdf.open(stream=print_render.pdf, filetype="pdf") as pdf:
        assert not [link for page in pdf for link in page.get_links() if "fn-" in str(link.get("nameddest"))]


def test_the_screen_pdf_has_the_preview_pages(preview_render, screen_render):
    assert screen_render.page_count == preview_render.page_count
    assert screen_render.chapters == preview_render.chapters
    assert words(screen_render.pdf) == words(preview_render.pdf)


def test_pdf_dates():
    assert pdf_date(CREATED) == "D:20260926100211Z"
    local = datetime(2026, 9, 26, 12, 2, 11).astimezone()
    stamp = pdf_date(local)
    assert stamp.startswith("D:20260926120211") and (stamp.endswith("Z") or stamp.endswith("'"))


# ====================================================================== fonts


def test_the_export_fonts_are_subsets_and_never_type_3(screen_render):
    audit = audit_pdf(screen_render.pdf, fonts.resolve("amiri", "amiri", "amiri"))
    assert audit.fonts and not audit.type3 and not audit.not_subset
    assert all(re.match(r"^[A-Z]{6}\+", item["name"]) for item in audit.fonts)
    assert not audit.foreign


@pytest.mark.parametrize(("body", "latin"), [("amiri", "amiri"), ("simplified_arabic", "simplified_arabic")])
def test_latin_words_stay_in_the_book_faces_in_the_export(body, latin):
    resolved = fonts.resolve(body, latin, body)
    if resolved.body.key != body:
        pytest.skip(f"{body} is not installed")
    setup = page_setup({"body_font": body, "latin_font": latin, "heading_font": body})
    doc = document(heading("h1", "الفصل"), para("p1", LATIN_TEXT))
    rendered = render(doc, setup, print_output())
    audit = audit_pdf(rendered.pdf, resolved)
    assert audit.characters and not audit.foreign
    assert not foreign_font_rows(audit)


def test_foreign_fonts_names_a_cjk_character_and_its_page():
    doc = document(heading("h1", "الفصل"), para("p1", "نص عربي فيه حرفان صينيان 德拉 ثم نص."))
    rendered = render(doc, amiri_setup(), screen_output())
    audit = audit_pdf(rendered.pdf, fonts.resolve("amiri", "amiri", "amiri"))
    if not audit.foreign:
        pytest.skip("no system face covers CJK here (the characters print as boxes in Amiri)")
    rows = foreign_font_rows(audit)
    assert rows and all(row["code"] == "foreign_fonts" and row["level"] == "warn" for row in rows)
    assert any(
        "في الصفحة 3 " in row["message"] and "وهو ليس من خطوط الكتاب." in row["message"] for row in rows
    )
    assert any("德" in found["chars"] for found in audit.foreign.values())


def test_foreign_font_wording():
    assert pages_phrase([81, 47]) == "في الصفحات 47، 81"
    assert pages_phrase([5]) == "في الصفحة 5"
    assert pages_phrase(list(range(1, 12))).endswith("8 وغيرها")
    assert display_name("PMUTZQ+MS-Mincho") == "MS Mincho"
    assert display_name("ESSOFH+Times-New-Roman,") == "Times New Roman"


# ====================================================================== passes and cancelling


def test_on_pass_reports_each_pass(outline_doc):
    steps: list[str] = []
    render(outline_doc, amiri_setup(), screen_output(), on_pass=steps.append)
    assert steps[0] == "layout" and steps[-1] == "write"
    assert set(steps) <= {"layout", "footnotes", "relax", "write"}
    preview: list[str] = []
    render(outline_doc, amiri_setup(), on_pass=preview.append)  # the preview reports too, when asked
    assert preview == steps


def test_a_cancel_between_passes_raises(outline_doc):
    asked: list[str] = []

    def cancelled() -> bool:
        return len(asked) >= 2  # cancelled when the second pass is about to start

    with pytest.raises(RenderCancelled):
        render(outline_doc, amiri_setup(), print_output(), cancelled=cancelled, on_pass=asked.append)
    assert len(asked) == 2 and asked[0] == "layout" and asked[1] in ("footnotes", "relax")


class CancellingProgress(NullProgress):
    """Cancels once the given step is reported."""

    def __init__(self, at: str):
        super().__init__()
        self.at = at

    def cancelled(self) -> bool:
        return self.at in self.steps


def job_of(doc: dict, setup, fmt: str, options: dict | None = None, book_id: int = 1) -> ExportJob:
    return ExportJob(
        export_id=None,
        book_id=book_id,
        format=fmt,
        document=doc,
        setup=setup,
        title="كتاب التصدير",
        author="مؤلف الكتاب",
        digit_style="western",
        chapter_versions={},
        options=options or {},
        created=CREATED,
    )


@pytest.mark.django_db
def test_the_exporter_stops_when_cancelled(outline_doc):
    with pytest.raises((RenderCancelled, exporters.ExportCancelled)):
        ScreenPdfExporter().export(
            job_of(outline_doc, amiri_setup(), "screen_pdf"), CancellingProgress("layout")
        )


# ====================================================================== the exporters


def test_the_registry_resolves_the_pdf_exporters():
    assert isinstance(exporters.get_exporter("print_pdf"), PrintPdfExporter)
    assert isinstance(exporters.get_exporter("screen_pdf"), ScreenPdfExporter)
    assert {"print_pdf", "screen_pdf"} <= set(exporters.available_formats())
    printer = exporters.get_exporter("print_pdf")
    assert printer.version == get_engine().version and printer.media_type == "application/pdf"
    assert parse_options(printer.options, {"bleed_mm": "3", "crop_marks": "true"}) == {
        "bleed_mm": 3,
        "crop_marks": True,
    }
    assert parse_options(printer.options, {}) == {"bleed_mm": 0, "crop_marks": False}
    with pytest.raises(OptionsError):
        parse_options(printer.options, {"bleed_mm": 4})
    assert exporters.options_text(printer, {"bleed_mm": 3, "crop_marks": True}) == "نزف 3 مم · علامات القص"
    assert exporters.options_text(printer, {"bleed_mm": 0, "crop_marks": False}) == ""
    assert exporters.get_exporter("screen_pdf").options == ()


def test_the_print_form_block():
    form = PrintPdfExporter().form(None, {"bleed_mm": 3, "crop_marks": True})
    assert form["bleed_mm"]["value"] == 3 and form["bleed_mm"]["label"] == "النزف"
    assert [choice["value"] for choice in form["bleed_mm"]["choices"]] == [0, 3, 5]
    assert all({"value", "label", "title", "hint"} <= set(choice) for choice in form["bleed_mm"]["choices"])
    assert form["crop_marks"] == {"value": True, "label": "علامات القص", "hint": form["crop_marks"]["hint"]}
    assert ScreenPdfExporter().form(None, {}) == {}


@pytest.mark.django_db
def test_the_notes_name_a_missing_face(tmp_path, settings):
    book = Book.objects.create(title="كتاب")
    setup = page_setup({"body_font": "traditional_arabic", "latin_font": "amiri", "heading_font": "amiri"})
    with override_settings(NASSAKH={**settings.NASSAKH, "FONT_DIRS": [str(tmp_path)]}):
        notes = PrintPdfExporter().notes(book, setup)
    missing = [row for row in notes if row["code"] == "missing_font"]
    assert len(missing) == 1 and missing[0]["level"] == "warn"
    assert "«Traditional Arabic»" in missing[0]["message"] and "أميري" in missing[0]["message"]
    assert missing[0]["action"]["url"].endswith("?tab=format")


def test_layout_mismatch_messages():
    chapters = [
        {"id": "a", "title": "الأول", "first": 3, "last": 5},
        {"id": "b", "title": "الثاني", "first": 6, "last": 9},
    ]
    assert layout_mismatch(9, chapters, None) is None
    assert layout_mismatch(9, chapters, Reference("preview", 9, chapters)) is None
    found = layout_mismatch(10, chapters, Reference("preview", 9, chapters))
    assert found == {
        "code": "layout_mismatch",
        "level": "warn",
        "message": "عدد صفحات الملف 10 وصفحات الكتاب 9.",
    }
    moved = [chapters[0], {**chapters[1], "first": 7}]
    found = layout_mismatch(9, moved, Reference("live", 9, chapters))
    assert found["message"] == "يبدأ «الثاني» في الملف في الصفحة 7 وفي الكتاب في الصفحة 6."


@pytest.fixture
def pdf_book(db):
    book = Book.objects.create(title="كتاب التصدير", author="مؤلف الكتاب")
    run = AssemblyRun.objects.create(book=book, status="done")
    Manuscript.objects.create(book=book, document=outline_book(), version=1, run=run)
    return book


def test_the_export_is_compared_with_a_finished_preview_of_the_same_job(pdf_book):
    job, _inputs, _digest = exports.read_inputs(pdf_book, "screen_pdf", {})
    digest = job_hash(
        RenderJob(document=job.document, stylesheet=job.setup, title=job.title, author=job.author)
    )
    exporter = ScreenPdfExporter()
    first = exporter.export(job, NullProgress())
    assert first.stats["reference"] is None and "no finished preview" in first.log[-1]
    row = PreviewRender.objects.create(
        book=pdf_book,
        scope="book",
        content_hash=digest,
        status="done",
        page_count=first.page_count,
        chapters=first.stats["chapters"],
        checks=[{"code": "almost_empty_page", "page": 4}, {"code": "missing_font", "page": 1}],
    )
    same = exporter.export(job, NullProgress())
    assert same.stats["reference"] == "preview"
    assert not [item for item in same.warnings if item["code"] == "layout_mismatch"]
    checks = [item for item in same.warnings if item["code"] == "page_checks"]
    assert len(checks) == 1 and checks[0]["message"] == "ملاحظة واحدة على الصفحات"
    assert checks[0]["action"]["url"].endswith("?tab=chapters")
    PreviewRender.objects.filter(pk=row.pk).update(page_count=first.page_count - 1)
    other = exporter.export(job, NullProgress())
    mismatch = [item for item in other.warnings if item["code"] == "layout_mismatch"]
    assert mismatch == [
        {
            "code": "layout_mismatch",
            "level": "warn",
            "message": f"عدد صفحات الملف {first.page_count} وصفحات الكتاب {first.page_count - 1}.",
        }
    ]


def test_the_print_export_runs_through_the_pipeline(pdf_book):
    from publishing.tests import role_user

    editor = role_user("editor-pdf", "editor")
    row = exports.request_export(pdf_book, "print_pdf", {"bleed_mm": 3, "crop_marks": True}, editor)
    row.refresh_from_db()
    assert row.status == Export.Status.DONE, row.error
    assert row.filename == "كتاب التصدير - للطباعة.pdf" and row.page_count and row.size_bytes > 1000
    assert row.options == {"bleed_mm": 3, "crop_marks": True}
    assert row.renderer == get_engine().version and row.progress["step"] == "done"
    with row.file.open("rb") as handle, pymupdf.open(stream=handle.read(), filetype="pdf") as pdf:
        assert pdf.page_count == row.page_count
        assert raw_box(pdf, 0, "BleedBox") == pytest.approx([-3 * MM, -3 * MM, 173 * MM, 243 * MM], abs=0.01)
    payload = exports.export_payload(row, editor)
    assert payload["options_text"] == "نزف 3 مم · علامات القص" and payload["page_count"] == row.page_count
    screen = exports.request_export(pdf_book, "screen_pdf", {}, editor)
    screen.refresh_from_db()
    assert screen.status == Export.Status.DONE and screen.filename == "كتاب التصدير.pdf"
    assert screen.page_count == row.page_count


def test_the_same_export_gives_the_same_bytes(outline_doc):
    setup = amiri_setup()
    exporter = PrintPdfExporter()
    job = job_of(outline_doc, setup, "print_pdf", {"bleed_mm": 3, "crop_marks": False})
    first = exporter.output(job)
    assert first.bleed_mm == 3 and not first.crop_marks and first.metadata.created == CREATED
    a = render(outline_doc, setup, first).pdf
    b = render(outline_doc, setup, replace(first)).pdf
    assert a == b
