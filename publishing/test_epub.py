"""Tests of the EPUB export (PHASE6_SPEC §10, 6c): `publishing.epub`.

- **The package:** `mimetype` first and stored; ebooklib reads it back; the spine right to left;
  `dc:language ar`; the identifier (`urn:isbn:` or a stable `urn:uuid:`); the book details as metadata
  with MARC roles; `dcterms:modified` the export's time; the same export gives the same bytes.
- **The documents:** every one well formed, `lang="ar" dir="rtl"`; the title and copyright pages as the
  stylesheet asks; `nav.xhtml` with the contents (sections under their chapter) and hidden landmarks, in
  the reading order only with a contents page; one XHTML per chapter; the blocks and marks.
- **Footnotes:** numbered per chapter, `noteref` calls «(n)» whose targets exist, `aside` notes at the
  chapter's end with back links; every note of the model is there.
- **Faces:** Amiri Regular and Bold embedded whole with `OFL.txt`; the CSS roles use the book's face
  through `local()` with Amiri behind it, Amiri alone for a face missing here.
- **The exporter:** the registry, the notes, `export` and the pipeline end to end, and `check_epub`
  catching a broken file.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import UTC, datetime

from django.test import override_settings

import pytest
from ebooklib import ITEM_DOCUMENT
from ebooklib import epub as ebooklib_epub
from lxml import etree

from assembly.models import AssemblyRun
from books.models import Book
from editor.models import Manuscript, StyleSheet
from publishing import exporters, exports, fonts
from publishing.epub import (
    EPUB_VERSION,
    NS_EPUB,
    EpubExporter,
    book_identifier,
    build_epub,
    check_epub,
    epub_css,
    epub_notes,
    publication_year,
)
from publishing.exporters import ExportJob, NullProgress
from publishing.model import book_model, page_setup
from publishing.models import Export
from publishing.tests import document, heading, note, para, role_user, text

CREATED = datetime(2026, 9, 26, 10, 2, 11, tzinfo=UTC)
X = "{http://www.w3.org/1999/xhtml}"
DETAILS = {
    "title": "كتاب الرحلة",
    "subtitle": "دراسة وتحقيق",
    "author": "المسعودي",
    "editor": "نوفل نيوف",
    "translator": "ترجمة: سامي",
    "publisher": "هنداوي",
    "city": "القاهرة",
    "year": "٢٠١٧",
    "edition": "الأولى",
    "isbn": "978-1-5273-2405-9",
    "rights": "جميع الحقوق محفوظة.",
}


def blockquote(*paragraphs):
    return {"type": "blockquote", "attrs": {"id": "q"}, "content": list(paragraphs)}


def sample_document() -> dict:
    """Two chapters: a heading and a section title with notes, bold and italic, a two-paragraph quote, a
    verse with a line break, a centred line and a separator; the second chapter's notes restart at 1."""
    return document(
        heading("h1", "الفصل الأول"),
        {
            "type": "heading",
            "attrs": {"level": 2, "id": "s1", "sourcePages": [1], "sourceLineIds": []},
            "content": [text("قسم فرعي"), note("n1", "حاشية العنوان", number=1)],
        },
        para(
            "p1",
            "نص ",
            text("غامق", "bold"),
            " و",
            text("مائل", "italic"),
            " ثم حاشية",
            note("n2", "حاشية ثانية"),
        ),
        blockquote(para("q1", "اقتباس أول"), para("q2", "اقتباس ثان")),
        para(
            "v1",
            "شطر أول",
            {"type": "hardBreak"},
            "شطر ثان",
            style="verse",
        ),
        para("c1", "سطر في الوسط", style="center"),
        {"type": "separator", "attrs": {"id": "sep1", "sourcePages": [1]}},
        heading("h2", "الفصل الثاني", pages=(2,)),
        para("p2", "نص الفصل الثاني وفيه حاشية", note("n3", "حاشية الفصل الثاني"), pages=(2,)),
        para("p3", "وحاشية أخرى", note("n4", "حاشية رابعة"), pages=(2,)),
        title="كتاب الرحلة",
        author="المسعودي",
    )


def sample_setup(**values):
    front = {"title_page": True, "contents": True, "copyright_page": True, "fields": DETAILS}
    return page_setup(
        {
            "body_font": "amiri",
            "latin_font": "amiri",
            "heading_font": "amiri",
            "front_matter": front,
            **values,
        }
    )


def build(doc=None, setup=None, **kwargs):
    setup = setup or sample_setup()
    book = book_model(doc or sample_document(), setup)
    resolved = fonts.resolve(setup.body_font, setup.latin_font, setup.heading_font)
    identifier = kwargs.pop("identifier", book_identifier(7, book.front.isbn))
    return book, build_epub(book, resolved, identifier=identifier, created=kwargs.pop("created", CREATED))


def read(data: bytes):
    return ebooklib_epub.read_epub(io.BytesIO(data), {"ignore_ncx": True})


def xml(data: bytes, name: str):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return etree.fromstring(archive.read(f"EPUB/{name}"))


def epub_type(root, kind: str) -> list:
    return root.xpath(f"//*[@epub:type='{kind}']", namespaces={"epub": NS_EPUB})


@pytest.fixture(scope="module")
def built():
    return build()


# ====================================================================== the package


def test_the_zip_starts_with_a_stored_mimetype(built):
    _book, result = built
    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        first = archive.infolist()[0]
        assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED and not first.extra
        assert archive.read("mimetype") == b"application/epub+zip"
        assert result.data[30:38] == b"mimetype"  # the name right after the first local header
    assert check_epub(result.data) == []


def test_the_package_reads_back_right_to_left_with_the_book_details(built):
    _book, result = built
    package = read(result.data)
    assert package.direction == "rtl"
    assert package.get_metadata("DC", "language") == [("ar", {})]
    assert package.get_metadata("DC", "identifier")[0][0] == "urn:isbn:9781527324059"
    titles = [value for value, _attrs in package.get_metadata("DC", "title")]
    assert titles == ["كتاب الرحلة", "دراسة وتحقيق"]
    assert package.get_metadata("DC", "creator") == [("المسعودي", {"id": "author"})]
    assert [value for value, _ in package.get_metadata("DC", "contributor")] == ["نوفل نيوف", "ترجمة: سامي"]
    assert package.get_metadata("DC", "publisher")[0][0] == "هنداوي"
    assert package.get_metadata("DC", "date")[0][0] == "2017"
    assert package.get_metadata("DC", "rights")[0][0] == "جميع الحقوق محفوظة."
    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        opf = archive.read("EPUB/content.opf").decode()
    assert '<meta property="dcterms:modified">2026-09-26T10:02:11Z</meta>' in opf
    for role, element in (("aut", "author"), ("edt", "editor"), ("trl", "translator")):
        assert f'refines="#{element}" property="role" scheme="marc:relators">{role}</meta>' in opf
    assert 'page-progression-direction="rtl"' in opf and 'property="ibooks:specified-fonts">true' in opf


def test_the_identifier_is_the_isbn_or_a_stable_uuid():
    assert book_identifier(3, "978-1-5273-2405-9") == "urn:isbn:9781527324059"
    assert book_identifier(3, "٩٧٨١٥٢٧٣٢٤٠٥٩") == "urn:isbn:9781527324059"
    assert book_identifier(3, "0-306-40615-2") == "urn:isbn:0306406152"
    stable = book_identifier(3, "")
    assert stable.startswith("urn:uuid:") and stable == book_identifier(3, "12") != book_identifier(4, "")
    assert publication_year("١٩٩٨م") == "1998" and publication_year("سنة ما") == ""


def test_the_same_export_gives_the_same_bytes():
    _book, first = build()
    _book, second = build()
    assert first.data == second.data
    _book, later = build(created=datetime(2026, 9, 27, tzinfo=UTC))
    assert later.data != first.data


# ====================================================================== the documents


def test_every_document_is_right_to_left_and_well_formed(built):
    _book, result = built
    package = read(result.data)
    documents = list(package.get_items_of_type(ITEM_DOCUMENT))
    assert {item.file_name for item in documents} >= {
        "title.xhtml",
        "copyright.xhtml",
        "nav.xhtml",
        "chapter-01.xhtml",
        "chapter-02.xhtml",
    }
    for item in documents:
        root = etree.fromstring(item.content)
        assert root.get("lang") == "ar" and root.get("dir") == "rtl"
        assert root.find(f"{X}body").get("dir") == "rtl"
        assert root.find(f"{X}head/{X}link").get("href") == "styles/book.css"


def test_the_reading_order(built):
    _book, result = built
    assert result.documents == [
        "title.xhtml",
        "copyright.xhtml",
        "nav.xhtml",
        "chapter-01.xhtml",
        "chapter-02.xhtml",
    ]
    setup = page_setup(
        {
            "body_font": "amiri",
            "latin_font": "amiri",
            "heading_font": "amiri",
            "front_matter": {"title_page": False, "contents": False, "copyright_page": False},
        }
    )
    _book, bare = build(setup=setup)
    assert bare.documents == ["chapter-01.xhtml", "chapter-02.xhtml"]  # the nav is in the manifest only
    assert check_epub(bare.data) == []


def test_the_title_and_copyright_pages_print_the_book_details(built):
    _book, result = built
    title = "".join(xml(result.data, "title.xhtml").itertext())
    for value in ("كتاب الرحلة", "دراسة وتحقيق", "المسعودي", "تحقيق: نوفل نيوف", "ترجمة: سامي"):
        assert value in title
    assert "هنداوي، القاهرة، ٢٠١٧" in title
    copyright_page = xml(result.data, "copyright.xhtml")
    lines = ["".join(p.itertext()) for p in copyright_page.iter(f"{X}p")]
    assert "الطبعة الأولى" in lines and "ردمك: 978-1-5273-2405-9" in lines
    assert lines[-1] == "جميع الحقوق محفوظة."
    assert epub_type(copyright_page, "copyright-page")


def test_the_nav_has_the_contents_and_the_landmarks(built):
    _book, result = built
    nav = xml(result.data, "nav.xhtml")
    toc = epub_type(nav, "toc")[0]
    top = toc.findall(f"{X}ol/{X}li")
    assert ["".join(li.find(f"{X}a").itertext()) for li in top] == ["الفصل الأول", "الفصل الثاني"]
    nested = top[0].findall(f"{X}ol/{X}li/{X}a")
    assert [a.text for a in nested] == ["قسم فرعي"]
    assert top[0].find(f"{X}a").get("href") == "chapter-01.xhtml#b-h1"
    assert nested[0].get("href") == "chapter-01.xhtml#b-s1"
    landmarks = epub_type(nav, "landmarks")[0]
    assert landmarks.get("hidden") == "hidden"
    kinds = [a.get(f"{{{NS_EPUB}}}type") for a in landmarks.iter(f"{X}a")]
    assert kinds == ["titlepage", "copyright-page", "toc", "bodymatter"]


def test_the_blocks_and_marks(built):
    _book, result = built
    chapter = xml(result.data, "chapter-01.xhtml")
    body = chapter.find(f"{X}body")
    section = body.find(f"{X}section")
    assert section.get(f"{{{NS_EPUB}}}type") == "chapter" and section.get("id") == "ch-h1"
    tags = [(child.tag.replace(X, ""), child.get("class")) for child in section]
    assert tags == [
        ("h1", "chapter-title"),
        ("h2", "section-title"),
        ("p", "body"),
        ("blockquote", "quote"),
        ("p", "verse"),
        ("p", "center"),
        ("p", "separator"),
    ]
    quote = section.find(f"{X}blockquote")
    assert ["".join(p.itertext()) for p in quote] == ["اقتباس أول", "اقتباس ثان"]
    verse = section.find(f"{X}p[@class='verse']")
    assert verse.find(f"{X}br") is not None and "".join(verse.itertext()) == "شطر أولشطر ثان"
    assert section.find(f"{X}p[@class='body']/{X}b").text == "غامق"
    assert section.find(f"{X}p[@class='body']/{X}i").text == "مائل"
    assert "".join(section.find(f"{X}p[@class='separator']").itertext()) == "* * *"


def test_every_block_of_the_model_is_in_the_book(built):
    book, result = built
    texts: list[str] = []
    for index in range(1, len(book.chapters) + 1):
        root = xml(result.data, f"chapter-{index:02d}.xhtml")
        for br in root.iter(f"{X}br"):  # a line break counts as a space in the model's text
            br.tail = " " + (br.tail or "")
        for element in root.iter(f"{X}p", f"{X}h1", f"{X}h2"):
            if element.xpath("ancestor::*[local-name()='aside']"):
                continue
            words = "".join(part for part in element.xpath(".//text()[not(ancestor::*[@class='noteref'])]"))
            texts.append(" ".join(words.split()))
    assert texts == [" ".join(block.text().split()) for block in book.blocks()]


# ====================================================================== footnotes


def test_the_notes_are_numbered_per_chapter_with_their_calls(built):
    book, result = built
    total = 0
    for index, wanted in ((1, 2), (2, 2)):
        root = xml(result.data, f"chapter-{index:02d}.xhtml")
        refs = epub_type(root, "noteref")
        notes = epub_type(root, "footnote")
        assert [ref.text for ref in refs] == [f"({n})" for n in range(1, wanted + 1)]
        assert len(notes) == wanted and all(aside.tag == f"{X}aside" for aside in notes)
        ids = set(root.xpath("//@id"))
        for ref in refs:
            assert ref.get("href")[1:] in ids and ref.get("role") == "doc-noteref"
        for aside in notes:
            back = aside.find(f"{X}p/{X}a")
            assert back.get("href")[1:] in ids and back.get("role") == "doc-backlink"
        body = root.find(f"{X}body")
        assert [child.tag for child in body][-wanted:] == [f"{X}aside"] * wanted  # at the chapter's end
        total += len(notes)
    assert total == sum(1 for _ in book.footnotes())
    first = xml(result.data, "chapter-01.xhtml")
    heading_call = first.find(f".//{X}h2/{X}a")
    assert heading_call is not None and heading_call.text == "(1)"
    assert "".join(epub_type(first, "footnote")[0].itertext()).strip() == "(1) حاشية العنوان"


# ====================================================================== faces


def test_amiri_is_embedded_whole_with_its_licence(built):
    _book, result = built
    package = read(result.data)
    fonts_in = {item.file_name: item for item in package.get_items() if item.media_type == "font/ttf"}
    assert set(fonts_in) == {"fonts/Amiri-Regular.ttf", "fonts/Amiri-Bold.ttf"}
    vendored = fonts.VENDORED_DIR / "amiri"
    assert fonts_in["fonts/Amiri-Regular.ttf"].content == (vendored / "Amiri-Regular.ttf").read_bytes()
    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        assert b"SIL Open Font License" in archive.read("EPUB/fonts/OFL.txt")
        css = archive.read("EPUB/styles/book.css").decode()
    assert 'font-family: "nk-body"' in css and 'url("../fonts/Amiri-Regular.ttf")' in css
    assert "local(" not in css  # an Amiri book uses the embedded file only
    assert 'body { font-family: "nk-body", "nk-latin", serif;' in css


def test_another_face_is_used_where_installed_with_amiri_behind_it():
    resolved = fonts.resolve("simplified_arabic", "times", "simplified_arabic")
    if resolved.body.key != "simplified_arabic":
        pytest.skip("Simplified Arabic is not installed")
    setup = page_setup({"body_font": "simplified_arabic", "latin_font": "times"})
    css = epub_css(setup, resolved)
    body = [rule for rule in css.splitlines() if '"nk-body"' in rule and "@font-face" in rule]
    assert body and all("local(" in rule and 'url("../fonts/Amiri-' in rule for rule in body)
    assert all("unicode-range" in rule for rule in body) == (resolved.latin.key != "simplified_arabic")


def test_a_face_missing_here_is_amiri_as_in_the_preview(tmp_path, settings):
    setup = page_setup({"body_font": "traditional_arabic", "latin_font": "amiri", "heading_font": "amiri"})
    with override_settings(NASSAKH={**settings.NASSAKH, "FONT_DIRS": [str(tmp_path)]}):
        resolved = fonts.resolve(setup.body_font, setup.latin_font, setup.heading_font)
        css = epub_css(setup, resolved)
    assert resolved.body.key == "amiri" and "local(" not in css


# ====================================================================== the exporter


def test_the_registry_resolves_the_epub_exporter():
    exporter = exporters.get_exporter("epub")
    assert isinstance(exporter, EpubExporter) and "epub" in exporters.available_formats()
    assert exporter.version == EPUB_VERSION and exporter.options == () and exporter.form(None, {}) == {}
    assert exporter.media_type == "application/epub+zip" and exporter.extension == ".epub"


def test_the_notes_tell_the_faces_and_the_notes_per_chapter():
    setup = page_setup({"body_font": "amiri", "latin_font": "amiri", "heading_font": "amiri"})
    rows = epub_notes(None, setup, fonts.resolve("amiri", "amiri", "amiri"))
    assert [row["code"] for row in rows] == ["font_embedded", "epub_notes"]
    resolved = fonts.resolve("simplified_arabic", "simplified_arabic", "amiri")
    if resolved.body.key == "simplified_arabic":
        rows = epub_notes(None, page_setup({"body_font": "simplified_arabic"}), resolved)
        fallback = [row for row in rows if row["code"] == "font_fallback"]
        assert len(fallback) == 1 and "«Simplified Arabic»" in fallback[0]["message"]
        assert fallback[0]["level"] == "info"


def job_of(doc: dict, setup, book_id: int = 7) -> ExportJob:
    return ExportJob(
        export_id=None,
        book_id=book_id,
        format="epub",
        document=doc,
        setup=setup,
        title="كتاب الرحلة",
        author="المسعودي",
        digit_style="western",
        chapter_versions={},
        options={},
        created=CREATED,
    )


@pytest.mark.django_db
def test_the_export_reports_its_steps():
    progress = NullProgress()
    result = EpubExporter().export(job_of(sample_document(), sample_setup()), progress)
    assert progress.steps == ["prepare", "write", "check"]
    assert result.page_count is None and result.stats["chapters"] == 2 and result.stats["footnotes"] == 4
    assert check_epub(result.data) == []
    assert all(row["code"] != "font_embedded" for row in result.warnings)
    assert [row["code"] for row in result.warnings] == ["epub_notes"]


@pytest.mark.django_db
def test_the_epub_export_runs_through_the_pipeline():
    book = Book.objects.create(title="كتاب الرحلة", author="المسعودي")
    run = AssemblyRun.objects.create(book=book, status="done")
    Manuscript.objects.create(book=book, document=sample_document(), version=3, run=run)
    StyleSheet.objects.create(
        book=book, front_matter={"title_page": True, "contents": True, "fields": DETAILS}
    )
    editor = role_user("editor-epub", "editor")
    row = exports.request_export(book, "epub", {}, editor)
    row.refresh_from_db()
    assert row.status == Export.Status.DONE, row.error
    assert row.filename == "كتاب الرحلة.epub" and row.page_count is None and row.renderer == EPUB_VERSION
    with row.file.open("rb") as handle:
        data = handle.read()
    assert check_epub(data) == []
    assert read(data).get_metadata("DC", "identifier")[0][0] == "urn:isbn:9781527324059"


def test_check_epub_catches_a_broken_file(built):
    _book, result = built
    source = zipfile.ZipFile(io.BytesIO(result.data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "EPUB/chapter-01.xhtml":
                data = data.replace(b'href="#fn-1"', b'href="#fn-9"')
            method = zipfile.ZIP_DEFLATED  # mimetype compressed too
            target.writestr(zipfile.ZipInfo(info.filename, date_time=info.date_time), data, method)
    errors = check_epub(out.getvalue())
    assert any("mimetype" in error for error in errors)
    assert any("#fn-9 has no target" in error for error in errors)
    assert check_epub(b"not a zip")[0].startswith("not a zip file")
    assert re.match(r"^urn:", book_identifier(1))


# ====================================================================== the Phase 6 review (E6, E7)


def _bare_setup(**front):
    return page_setup(
        {
            "body_font": "amiri",
            "latin_font": "amiri",
            "heading_font": "amiri",
            "front_matter": {"title_page": False, "contents": False, "copyright_page": False, **front},
        }
    )


def _opf(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read("EPUB/content.opf").decode()


def test_a_book_with_nothing_to_read_has_its_nav_as_the_one_document():
    """E6: an empty book (no text, no front matter) never gives an EPUB whose spine is empty."""
    _book, result = build(document(title="كتاب"), _bare_setup())
    assert result.documents == ["nav.xhtml"]
    assert '<itemref idref="nav"' in _opf(result.data)
    assert check_epub(result.data) == []


def test_check_epub_refuses_an_empty_spine(built):
    """E6: a package whose spine lists no document is not a readable book."""
    _book, result = built
    source = zipfile.ZipFile(io.BytesIO(result.data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "EPUB/content.opf":
                data = re.sub(rb"<itemref [^>]*/>", b"", data)
            method = zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED
            target.writestr(zipfile.ZipInfo(info.filename, date_time=info.date_time), data, method)
    errors = check_epub(out.getvalue())
    assert any("spine is empty" in error for error in errors)


def test_characters_xml_forbids_are_left_out(tmp_path):
    """E7: an OCR'd vertical tab, a form feed, a lone surrogate, U+FFFE / U+FFFF in the text, the notes,
    the headings and the book details give a valid file without them (was an unreadable chapter)."""
    bad = "\x0b\x0c\ud83d\ufffe\uffff\x01"
    details = {"title": f"كتاب{bad} الرحلة", "author": f"المسعودي{bad}", "publisher": f"هنداوي{bad}",
               "rights": f"الحقوق{bad} محفوظة."}  # fmt: skip
    doc = document(
        heading("h1", f"الفصل{bad} الأول"),
        para("p1", f"نص{bad} فيه محارف تحكّم", note("n1", f"حاشية{bad} فيها مثلها")),
        title=f"كتاب{bad}",
    )
    setup = _bare_setup(title_page=True, contents=True, copyright_page=True, fields=details)
    _book, result = build(doc, setup)
    assert check_epub(result.data) == []
    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        for name in archive.namelist():
            if name.endswith((".xhtml", ".opf", ".ncx")):
                text = archive.read(name).decode("utf-8")
                assert not any(char in text for char in bad), name
        chapter = etree.fromstring(archive.read("EPUB/chapter-01.xhtml"))
    assert "".join(chapter.find(f".//{X}h1").itertext()) == "الفصل الأول"
    opf = _opf(result.data)  # the metadata go through lxml, which refuses such characters
    assert '<dc:title id="title">كتاب' in opf and "الرحلة</dc:title>" in opf and "المسعودي" in opf


# ====================================================================== the cover (D80)

from dataclasses import replace as _replace  # noqa: E402

from PIL import Image  # noqa: E402

from publishing.cover import EPUB_HEIGHT_PX  # noqa: E402
from publishing.model import CoverSpec  # noqa: E402


def _png(width: int = 10, height: int = 14) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (31, 59, 45)).save(out, "PNG")
    return out.getvalue()


def test_the_cover_comes_first_and_is_the_library_picture():
    setup = sample_setup()
    book = book_model(sample_document(), setup)
    resolved = fonts.resolve("amiri", "amiri", "amiri")
    result = build_epub(book, resolved, identifier="urn:uuid:x", created=CREATED, cover=(_png(), "image/png"))
    assert check_epub(result.data) == []
    assert result.documents[0] == "cover.xhtml" and result.documents[1] == "title.xhtml"
    opf = _opf(result.data)
    assert re.search(r'<item [^>]*href="images/cover\.png"[^>]*properties="cover-image"', opf) or re.search(
        r'<item [^>]*properties="cover-image"[^>]*href="images/cover\.png"', opf
    )
    assert '<meta name="cover" content="cover-image"' in opf  # EPUB 2 readers
    assert re.search(r'<reference [^>]*type="cover"', opf) and '<itemref idref="cover"' in opf
    page = xml(result.data, "cover.xhtml")
    assert page.get("dir") == "rtl" and page.get("lang") == "ar"
    assert epub_type(page, "cover") and page.find(f".//{X}img").get("src") == "images/cover.png"
    nav = xml(result.data, "nav.xhtml")
    marks = [
        (a.get(f"{{{NS_EPUB}}}type"), a.get("href"), a.text)
        for a in nav.iter(f"{X}a")
        if a.get(f"{{{NS_EPUB}}}type")
    ]
    assert marks[0] == ("cover", "cover.xhtml", "الغلاف")
    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        assert archive.read("EPUB/images/cover.png") == _png()
        assert "div.cover img" in archive.read("EPUB/styles/book.css").decode()
    package = read(result.data)
    from ebooklib import ITEM_COVER

    assert [item.file_name for item in package.get_items() if item.get_type() == ITEM_COVER] == [
        "images/cover.png"
    ]


def test_a_book_without_a_cover_gets_the_epub_it_got_before():
    _book, before = build()
    off = sample_setup(
        front_matter={
            "title_page": True,
            "contents": True,
            "copyright_page": True,
            "fields": DETAILS,
            "cover": {"mode": "none", "center": "x"},
        }
    )
    _book, again = build(setup=off)
    assert again.data == before.data and "cover" not in _opf(before.data)


def test_check_epub_catches_a_missing_picture():
    setup = sample_setup()
    book = book_model(sample_document(), setup)
    result = build_epub(
        book,
        fonts.resolve("amiri", "amiri", "amiri"),
        identifier="urn:uuid:x",
        created=CREATED,
        cover=(_png(), "image/png"),
    )
    source = zipfile.ZipFile(io.BytesIO(result.data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "EPUB/cover.xhtml":
                data = data.replace(b"images/cover.png", b"images/gone.png")
            target.writestr(info, data)
    assert any("images/gone.png is not in the package" in error for error in check_epub(out.getvalue()))


@pytest.mark.django_db
def test_the_export_rasterises_the_cover_1600_px_tall():
    setup = _replace(sample_setup(), cover=CoverSpec(mode="text", center="كتاب الرحلة", background="#1f3b2d"))
    result = EpubExporter().export(job_of(sample_document(), setup), NullProgress())
    assert check_epub(result.data) == []
    assert (
        result.stats["documents"][0] == "cover.xhtml" and result.stats["cover"]["media_type"] == "image/png"
    )
    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        with Image.open(io.BytesIO(archive.read("EPUB/images/cover.png"))) as image:
            assert image.height == EPUB_HEIGHT_PX and image.width == round(1600 * 170 / 240)
