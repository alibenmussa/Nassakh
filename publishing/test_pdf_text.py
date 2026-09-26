"""Tests of the PDF text layer (PHASE7_SPEC §4.7, §8.3): `publishing.pdf_text`.

- **The rule on a synthetic CMap:** an Arabic multi-character entry is reversed, a Latin «fi» is kept, a
  single-character entry and an empty one are untouched, a mixed Arabic/Latin entry and Arabic-Indic digit
  pairs are kept; `bfrange` in both forms (one destination, an array), a range over several codes expanded
  to its reversed destinations; the header and the spacing kept.
- **A real file** (Traditional Arabic, book 23's face, when installed): its lam-alef reads «لا» in PyMuPDF
  (MuPDF reorders as Chrome does) after the fix and «ال» before; the fix keeps the outline, the metadata,
  the page contents and the boxes; the same input gives the same bytes. An Amiri file (lam-alef as two
  glyphs, only Latin ligatures) comes back as it was.
- **The exporters:** both PDFs ship the fixed file, and the stats count the entries.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pymupdf
import pytest

from publishing import fonts
from publishing.engine import PdfMetadata, PdfOutput, RenderJob, get_engine
from publishing.exporters import ExportJob, NullProgress
from publishing.model import page_setup
from publishing.pdf_export import PrintPdfExporter, ScreenPdfExporter
from publishing.pdf_text import (
    fix_text_layer,
    is_arabic,
    reverse_cmap,
    reversible,
    tounicode_streams,
)
from publishing.tests import document, heading, para

CREATED = datetime(2026, 9, 26, 10, 2, 11, tzinfo=UTC)

HEADER = """/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<0000> <ffff>
endcodespacerange
"""
FOOTER = """endcmap
CMapName currentdict /CMap defineresource pop
end
end
"""


def cmap(*sections: str) -> str:
    return HEADER + "".join(sections) + FOOTER


def bfchar(*entries: tuple[str, str]) -> str:
    body = "".join(f"<{source}> <{destination}>\n" for source, destination in entries)
    return f"{len(entries)} beginbfchar\n{body}endbfchar\n"


# ====================================================================== the rule, pure


def test_an_arabic_multi_character_entry_is_reversed_and_the_rest_kept():
    text = cmap(
        bfchar(
            ("0866", "06440627"),  # lam-alef «لا» → «ال»
            ("1a2b", "0644064406470651"),  # a four-character Arabic ligature
            ("0041", "00660069"),  # Latin «fi»: kept
            ("0182", "0628"),  # a single character: untouched
            ("01ab", ""),  # an empty destination (WeasyPrint writes them): untouched
        )
    )
    result = reverse_cmap(text)
    assert result.changed == 2
    assert "<0866> <06270644>" in result.text
    assert "<1a2b> <0651064706440644>" in result.text
    assert "<0041> <00660069>" in result.text
    assert "<0182> <0628>" in result.text and "<01ab> <>" in result.text
    assert result.text.startswith(HEADER) and result.text.endswith(FOOTER)
    assert result.text.replace("06270644", "06440627").replace("0651064706440644", "0644064406470651") == text


def test_mixed_scripts_and_digit_pairs_keep_their_order():
    text = cmap(
        bfchar(
            ("0001", "06440041"),  # Arabic and Latin: not every character is Arabic
            ("0002", "06610662"),  # Arabic-Indic «١٢»: digits run left to right
            ("0003", "06F106F2"),  # Extended Arabic-Indic digits
            ("0004", "0644200d"),  # a joiner is not Arabic script
        )
    )
    result = reverse_cmap(text)
    assert result.changed == 0 and result.text == text


def test_marks_and_presentation_forms_count_as_arabic():
    assert is_arabic("ّ") and is_arabic("ﻻ") and is_arabic("ﷲ") and is_arabic("ݐ") and is_arabic("ࢠ")
    assert not is_arabic("f") and not is_arabic("١") and not is_arabic("۱") and not is_arabic("‍")
    assert reversible("بّ") and reversible("لا") and not reversible("ل") and not reversible("fi")
    result = reverse_cmap(cmap(bfchar(("0010", "0628 0651"))))  # whitespace inside a hex string
    assert result.changed == 1 and "<0010> <06510628>" in result.text


def test_a_range_is_reversed_in_both_forms():
    body = (
        "3 beginbfrange\n"
        "<0100> <0100> <06440627>\n"  # one code: reversed in place
        "<0200> <0202> [<06440623> <0041> <06440625>]\n"  # an array: each Arabic item reversed
        "<0300> <0302> <0041>\n"  # a single-character range: untouched
        "endbfrange\n"
    )
    result = reverse_cmap(cmap(body))
    assert result.changed == 3
    assert "<0100> <0100> <06270644>" in result.text
    assert "<0200> <0202> [<06230644> <0041> <06250644>]" in result.text
    assert "<0300> <0302> <0041>" in result.text


def test_a_range_over_several_codes_becomes_its_reversed_destinations():
    """`<lo> <hi> <dst>` increments the destination's last byte per code: «لا», «لب», «لة» reversed one by
    one (a plain reversal of the first would increment the lam)."""
    result = reverse_cmap(cmap("1 beginbfrange\n<0010> <0012> <06440627>\nendbfrange\n"))
    assert result.changed == 3
    assert "<0010> <0012> [<06270644> <06280644> <06290644>]" in result.text


def test_a_cmap_without_arabic_ligatures_is_returned_as_it_is():
    text = cmap(bfchar(("0003", "0020"), ("0041", "00660066"), ("0866", "0644")))
    result = reverse_cmap(text)
    assert result.changed == 0 and result.text == text


# ====================================================================== a real file


def setup_of(face: str):
    return page_setup({"body_font": face, "latin_font": "times", "heading_font": face})


def traditional_arabic():
    """Book 23's face, whose lam-alef is one ligature glyph (Amiri sets it as two glyphs); the tests that
    need it are skipped on a machine without it (it is never committed)."""
    if fonts.locate("traditional_arabic") is None:
        pytest.skip("Traditional Arabic is not installed")
    return setup_of("traditional_arabic")


LAM_ALEF = "لا إله إلا هو، والسلام على من اتبع الهدى. Office file"


def lam_alef_book() -> dict:
    return document(heading("h1", "الفصل الأول"), para("p1", LAM_ALEF), para("p2", "قال لا أعلم ولا أدري."))


def screen_output() -> PdfOutput:
    return PdfOutput("screen", metadata=PdfMetadata(subject="", created=CREATED))


@pytest.fixture(scope="module")
def rendered() -> bytes:
    setup = traditional_arabic()
    job = RenderJob(document=lam_alef_book(), stylesheet=setup, title="كتاب", output=screen_output())
    return get_engine().render(job).pdf


def page_text(data: bytes) -> str:
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        return "\n".join(page.get_text() for page in pdf)


def test_mupdf_reads_lam_alef_right_after_the_fix(rendered):
    before = page_text(rendered)
    assert "لا" not in before.split() and "ال" in before.split()  # WeasyPrint's logical entry, reordered
    fix = fix_text_layer(rendered)
    assert fix.entries >= 1 and fix.changed_cmaps >= 1 and fix.cmaps >= fix.changed_cmaps
    after = page_text(fix.data)
    assert "لا" in after.split() and "ولا" in after.split() and "إلا" in after.split()
    assert "Office" in after  # a Latin «ffi» keeps its order


def test_the_fix_changes_only_the_text_maps(rendered):
    fix = fix_text_layer(rendered)
    before = pymupdf.open(stream=rendered, filetype="pdf")
    with before as a, pymupdf.open(stream=fix.data, filetype="pdf") as b:
        assert a.page_count == b.page_count and a.metadata == b.metadata and a.get_toc() == b.get_toc()
        for key in ("ViewerPreferences", "PageMode", "Lang"):
            assert a.xref_get_key(a.pdf_catalog(), key) == b.xref_get_key(b.pdf_catalog(), key)
        for pa, pb in zip(a, b, strict=True):
            assert pa.read_contents() == pb.read_contents()
            assert pa.rect == pb.rect and pa.get_links() == pb.get_links()
        maps = tounicode_streams(b)
        assert len(maps) == len(tounicode_streams(a))
        assert any(b.xref_stream(xref) != a.xref_stream(xref) for xref in maps)
    assert len(fix.data) < len(rendered) * 1.2  # written with object streams, as WeasyPrint writes


def test_the_same_file_gives_the_same_bytes(rendered):
    assert fix_text_layer(rendered).data == fix_text_layer(rendered).data


def test_a_file_without_arabic_ligatures_comes_back_as_it_was():
    """Amiri sets lam-alef as two glyphs: its only multi-character entries are Latin («fi», «ffi»)."""
    job = RenderJob(document=lam_alef_book(), stylesheet=setup_of("amiri"), output=screen_output())
    data = get_engine().render(job).pdf
    fix = fix_text_layer(data)
    assert fix.entries == 0 and fix.data is data and fix.cmaps
    assert fix_text_layer(b"not a pdf").data == b"not a pdf"


# ====================================================================== the exporters


def job_of(doc: dict, fmt: str, setup, options: dict | None = None) -> ExportJob:
    return ExportJob(
        export_id=None,
        book_id=0,
        format=fmt,
        document=doc,
        setup=setup,
        title="كتاب",
        author="",
        digit_style="western",
        chapter_versions={},
        options=options or {},
        created=CREATED,
    )


@pytest.mark.django_db
@pytest.mark.parametrize("exporter", [ScreenPdfExporter(), PrintPdfExporter()], ids=["screen", "print"])
def test_both_pdfs_ship_the_fixed_text_layer(exporter):
    options = {"bleed_mm": 3} if exporter.format == "print_pdf" else {}
    job = job_of(lam_alef_book(), exporter.format, traditional_arabic(), options)
    result = exporter.export(job, NullProgress())
    assert result.stats["text_layer"] >= 1
    assert any(line.startswith("text layer:") for line in result.log)
    assert "لا" in page_text(result.data).split()
