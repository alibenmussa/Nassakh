"""The reference fix (PHASE6_SPEC §4.1, D60): the preview is what every export is measured against.

- A role whose regular file is the Latin face's own file (Simplified Arabic, or Amiri, for both) is
  declared without a `unicode-range`, in the PDF's `@font-face` rules and in the live pages' faces, so
  its Latin words are set in the book's faces (`nk-body` or `nk-latin`), never in a system face such as
  "Serif Narrow".
- The preview CSS never prints bleed or crop marks, even with `bleed_mm = 3` (they belong to the print
  export, D61).
- `ENGINE_VERSION` is `nk-print-6` (D99: the subtitle's alignment, the text options, empty lines, blank
  pages), so every cached preview is laid out again.
"""

from __future__ import annotations

import pymupdf
import pytest

from publishing import fonts
from publishing.css import stylesheet_css
from publishing.engine import ENGINE_VERSION, RenderJob, get_engine
from publishing.model import page_setup

LATIN_TEXT = "نص عربي https www hindawi org وبعده Latin words 123 ثم نص."
BOOK_FACES = ("nk-body", "nk-heading", "nk-latin")


def _document(value: str) -> dict:
    return {
        "type": "doc",
        "attrs": {},
        "content": [
            {"type": "title", "attrs": {"text": "كتاب", "author": ""}},
            {
                "type": "heading",
                "attrs": {"level": 1, "id": "h1", "sourcePages": [1], "sourceLineIds": []},
                "content": [{"type": "text", "text": "الفصل"}],
            },
            {
                "type": "paragraph",
                "attrs": {"id": "p1", "sourcePages": [1], "sourceLineIds": []},
                "content": [{"type": "text", "text": value}],
            },
        ],
    }


def _setup(body: str, latin: str, **values):
    front = {"title_page": False, "contents": False}
    return page_setup(
        {"body_font": body, "latin_font": latin, "heading_font": body, "front_matter": front, **values}
    )


def _span_fonts(pdf: bytes) -> dict[str, set[str]]:
    """Font name → the (stripped) texts of the spans set in it."""
    out: dict[str, set[str]] = {}
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        for page in document:
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        if span["text"].strip():
                            out.setdefault(span["font"], set()).add(span["text"].strip())
    return out


def _family(name: str) -> str:
    """`ABCDEF+nk-body-Bold` → `nk-body`."""
    name = name.split("+", 1)[-1]
    for family in BOOK_FACES:
        if name == family or name.startswith(family + "-"):
            return family
    return name


def test_engine_version_is_nk_print_6():
    assert ENGINE_VERSION == "nk-print-6"
    assert get_engine().version.startswith("nk-print-6/weasyprint-")


def test_a_role_in_the_latin_faces_file_has_no_unicode_range():
    same = fonts.resolve("amiri", "amiri", "amiri")
    css = fonts.font_face_css(same)
    body = [rule for rule in css.splitlines() if '"nk-body"' in rule]
    heading = [rule for rule in css.splitlines() if '"nk-heading"' in rule]
    assert body and heading
    assert all("unicode-range" not in rule for rule in body + heading)
    assert fonts.role_ranges(same.body, same.latin) is None
    browser = [face for face in fonts.browser_faces(same) if face["family"] in ("nk-body", "nk-heading")]
    assert browser and all(face["unicode_range"] is None for face in browser)
    assert all("unicode-range" not in rule for rule in fonts.browser_font_css(same).splitlines())


def test_a_role_in_another_file_keeps_latin_letters_for_the_latin_face():
    other = fonts.resolve("amiri", "times", "amiri")
    if other.latin.key == "amiri":  # no Times New Roman on this machine: both roles are Amiri
        pytest.skip("Times New Roman is not installed")
    css = fonts.font_face_css(other)
    body = [rule for rule in css.splitlines() if '"nk-body"' in rule]
    assert body and all("unicode-range" in rule and "U+0041" not in rule for rule in body)
    browser = [face for face in fonts.browser_faces(other) if face["family"] == "nk-body"]
    assert browser and all(face["unicode_range"] for face in browser)


@pytest.mark.parametrize(("body", "latin"), [("amiri", "amiri"), ("simplified_arabic", "simplified_arabic")])
def test_latin_words_are_set_in_the_book_faces_when_the_latin_face_is_the_body_face(body, latin):
    resolved = fonts.resolve(body, latin, body)
    if resolved.body.key != body:
        pytest.skip(f"{body} is not installed")
    rendered = get_engine().render(RenderJob(document=_document(LATIN_TEXT), stylesheet=_setup(body, latin)))
    used = _span_fonts(rendered.pdf)
    foreign = {name: texts for name, texts in used.items() if _family(name) not in BOOK_FACES}
    assert not foreign, f"Latin text set in a system face: {foreign}"
    latin_spans = {
        text for name, texts in used.items() for text in texts if "hindawi" in text or "Latin" in text
    }
    assert latin_spans  # the Latin words are there, in the book's faces


def test_the_preview_css_never_bleeds():
    for bleed in (0, 3, 5):
        setup = page_setup({"bleed_mm": bleed})
        css = stylesheet_css(setup, fonts.resolve(setup.body_font, setup.latin_font, setup.heading_font))
        assert "bleed" not in css and "marks:" not in css


def test_the_preview_pdf_has_no_bleed_box_beyond_the_trim():
    setup = _setup("amiri", "times", bleed_mm=3)
    rendered = get_engine().render(RenderJob(document=_document("نص قصير."), stylesheet=setup))
    with pymupdf.open(stream=rendered.pdf, filetype="pdf") as document:
        page = document[0]
        width_pt, height_pt = setup.width_mm * 72 / 25.4, setup.height_mm * 72 / 25.4
        assert page.mediabox.width == pytest.approx(width_pt, abs=0.5)
        assert page.mediabox.height == pytest.approx(height_pt, abs=0.5)
