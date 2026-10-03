"""An organisation's fonts (D98): the upload checks, the registry's `org-<pk>` faces, removal, the page and
its permissions, the font menus, and the face in every output — the preview, both PDFs, Word and EPUB (the
owner's rule: what changes printed pages works and is checked in all of them).

Fonts are made here: `naskh()` is Amiri cut down to the Arabic and Latin blocks and renamed «Nassakh Test
Naskh» (a real face: shaping, bold, embedding), `synthetic()` a box-glyph face built with fontTools for the
edge cases (a licence that forbids embedding, a variable face, CFF outlines, no letters)."""

from __future__ import annotations

import hashlib
import io
import zipfile
from functools import lru_cache
from pathlib import Path

from django.contrib.auth.models import Group, User
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

import pymupdf
import pytest
from fontTools.ttLib import TTFont

from accounts import fonts as org_fonts
from accounts.models import Membership, Organization, OrganizationFont
from editor.models import StyleSheet
from publishing import fonts as F

pytestmark = pytest.mark.django_db

AMIRI = F.VENDORED_DIR / "amiri"
FAMILY = "Nassakh Test Naskh"


# ====================================================================== fonts for the tests


@lru_cache(maxsize=8)
def naskh(style: str = "Regular", family: str = FAMILY, fs_type: int = 0) -> bytes:
    """Amiri (regular or bold) cut down to the Arabic block, Latin and a few marks, renamed `family`."""
    from fontTools import subset

    font = TTFont(AMIRI / ("Amiri-Bold.ttf" if style == "Bold" else "Amiri-Regular.ttf"))
    options = subset.Options()
    options.layout_features = ["*"]
    options.name_IDs = ["*"]
    options.name_languages = ["*"]
    options.notdef_outline = True
    options.drop_tables += ["FFTM"]
    text = "".join(map(chr, range(0x0600, 0x0700))) + "".join(map(chr, range(0x20, 0x7F))) + "«»—–…"
    subsetter = subset.Subsetter(options)
    subsetter.populate(text=text)
    subsetter.subset(font)
    names = font["name"]
    for name_id in (16, 17, 21, 22):
        names.removeNames(nameID=name_id)
    full = family if style == "Regular" else f"{family} {style}"
    for name_id, value in (
        (1, family),
        (2, style),
        (4, full),
        (6, full.replace(" ", "")),
        (13, "Test licence"),
    ):
        names.setName(value, name_id, 3, 1, 0x409)
        names.setName(value, name_id, 1, 0, 0)
    font["OS/2"].fsType = fs_type
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def synthetic(
    family: str = "Box Face",
    style: str = "Regular",
    *,
    weight: int = 400,
    italic: bool = False,
    fs_type: int = 0,
    cff: bool = False,
    arabic: bool = True,
    latin: bool = True,
    variable: bool = False,
) -> bytes:
    """A face of box glyphs built with fontTools (see the module docstring)."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.t2CharStringPen import T2CharStringPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    chars = [" ", "."]
    if arabic:
        chars += [chr(code) for code in range(0x0621, 0x064B)]
    if latin:
        chars += [chr(code) for code in (*range(0x41, 0x5B), *range(0x61, 0x7B), *range(0x30, 0x3A))]
    names = [".notdef", *(f"uni{ord(char):04X}" for char in chars)]
    builder = FontBuilder(1000, isTTF=not cff)
    builder.setupGlyphOrder(names)
    builder.setupCharacterMap({ord(char): f"uni{ord(char):04X}" for char in chars})

    def box(pen):
        pen.moveTo((50, 0))
        pen.lineTo((50, 600))
        pen.lineTo((450, 600))
        pen.lineTo((450, 0))
        pen.closePath()

    if cff:
        strings = {}
        for name in names:
            pen = T2CharStringPen(500, None)
            box(pen)
            strings[name] = pen.getCharString()
        builder.setupCFF(family.replace(" ", ""), {"FullName": family}, strings, {})
    else:
        glyphs = {}
        for name in names:
            pen = TTGlyphPen(None)
            box(pen)
            glyphs[name] = pen.glyph()
        builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (500, 50) for name in names})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": family, "styleName": style, "licenseDescription": "Box licence"})
    selection = (
        (0x01 if italic else 0)
        | (0x20 if weight >= 700 else 0)
        | (0x40 if not italic and weight < 700 else 0)
    )
    builder.setupOS2(usWeightClass=weight, fsType=fs_type, fsSelection=selection, sTypoAscender=800)
    builder.setupPost()
    if variable:
        builder.setupFvar(axes=[("wght", 100, 400, 900, "Weight")], instances=[])
    out = io.BytesIO()
    builder.save(out)
    return out.getvalue()


def woff2(data: bytes) -> bytes:
    font = TTFont(io.BytesIO(data))
    font.flavor = "woff2"
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def upload(name: str, data: bytes) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, data, content_type="application/octet-stream")


# ====================================================================== people and books


def user(name: str, role: str | None = None, organization=None, org_role: str = "member", **extra) -> User:
    person = User.objects.create_user(name, password="pass-1234", **extra)
    if role:
        person.groups.add(Group.objects.get_or_create(name=role)[0])
    if organization is not None:
        Membership.objects.create(organization=organization, user=person, role=org_role)
    return person


def logged(person) -> Client:
    client = Client()
    client.force_login(person)
    return client


@pytest.fixture
def org(db):
    return Organization.objects.create(name="دار المدار")


@pytest.fixture
def other_org(db):
    return Organization.objects.create(name="دار أخرى")


@pytest.fixture
def admin(org):
    return user("org-admin", "editor", org, "admin")


@pytest.fixture
def editor(org):
    return user("org-editor", "editor", org)


def add_naskh(organization, person=None, bold: bool = True) -> OrganizationFont:
    files = [upload("TestNaskh-Regular.ttf", naskh())]
    if bold:
        files.append(upload("TestNaskh-Bold.ttf", naskh("Bold")))
    return org_fonts.add_fonts(organization, files, user=person, name="نسخ تجريبي", confirmed=True)[0]


def org_book(organization, **sheet):
    from publishing.tests import long_book, make_book

    book = make_book(long_book(chapters=2, paragraphs=4), **sheet)
    book.organization = organization
    book.save(update_fields=["organization"])
    return book


# ====================================================================== the upload checks


def test_a_face_is_read_from_its_file():
    item = org_fonts.inspect_font(naskh(), "TestNaskh-Regular.ttf")
    assert (item.family, item.group, item.style, item.weight, item.italic) == (
        FAMILY,
        FAMILY,
        "regular",
        400,
        False,
    )
    assert item.arabic and item.latin and item.outlines == "glyf" and item.extension == "ttf"
    assert item.fs_type == 0 and item.glyphs > 100
    # the licence notice: the licence, its address and the copyright records of the file
    assert item.licence.startswith("Test licence\nhttp://scripts.sil.org/OFL\nCopyright")
    bold = org_fonts.inspect_font(naskh("Bold"), "TestNaskh-Bold.ttf")
    assert bold.style == "bold" and bold.weight == 700
    italic = org_fonts.inspect_font(synthetic(style="Italic", italic=True), "box-italic.ttf")
    assert italic.style == "italic"
    bold_italic = org_fonts.inspect_font(
        synthetic(style="Bold Italic", weight=700, italic=True), "box-bi.ttf"
    )
    assert bold_italic.style == "bold_italic"
    semibold = org_fonts.inspect_font(synthetic(style="SemiBold", weight=600), "box-sb.ttf")
    assert semibold.style == "bold"
    latin_only = org_fonts.inspect_font(synthetic(arabic=False), "box-latin.ttf")
    assert latin_only.latin and not latin_only.arabic
    arabic_only = org_fonts.inspect_font(synthetic(latin=False), "box-arabic.ttf")
    assert arabic_only.arabic and not arabic_only.latin


def test_woff2_is_stored_decompressed_and_cff_as_otf():
    data = naskh()
    item = org_fonts.inspect_font(woff2(data), "TestNaskh-Regular.woff2")
    assert item.data[:4] == b"\x00\x01\x00\x00" and item.extension == "ttf" and item.family == FAMILY
    cff = org_fonts.inspect_font(synthetic(cff=True), "box.otf")
    assert cff.data[:4] == b"OTTO" and cff.extension == "otf" and cff.outlines == "cff"


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("box.exe", synthetic(), "نوع الملف غير مقبول"),
        ("box.ttf", b"not a font at all, only bytes", "تعذّرت قراءة الملف خطًّا"),
        ("box.ttf", b"\x00\x01\x00\x00" + b"\x00" * 40, "تعذّرت قراءة الملف خطًّا"),
        ("box.ttf", b"ttcf" + b"\x00" * 40, "مجموعة خطوط"),
        ("box.ttf", synthetic(variable=True), "المتغيّرة"),
        ("box.ttf", synthetic(fs_type=0x0002), "يمنع تضمينه"),
        ("box.ttf", synthetic(fs_type=0x0100), "تضمين جزء منه"),
        ("box.ttf", synthetic(fs_type=0x0200), "bitmap"),
        ("box.ttf", synthetic(arabic=False, latin=False), "لا يحوي حروفًا"),
    ],
)
def test_a_file_that_cannot_serve_is_refused_with_its_reason(name, data, message):
    with pytest.raises(org_fonts.FontRefused) as refused:
        org_fonts.inspect_font(data, name)
    assert message in str(refused.value) and f"«{name}»" in str(refused.value)


def test_a_file_too_large_is_refused_before_it_is_read(org, monkeypatch):
    monkeypatch.setattr(org_fonts, "max_bytes", lambda: 1000)
    with pytest.raises(org_fonts.FontRefused) as refused:
        org_fonts.add_fonts(org, [upload("big.ttf", naskh())], confirmed=True)
    assert "أكبر من" in str(refused.value)
    assert not OrganizationFont.objects.exists()


def test_an_upload_needs_files_and_the_licence_confirmed(org):
    with pytest.raises(org_fonts.FontRefused, match="ملف خط"):
        org_fonts.add_fonts(org, [], confirmed=True)
    with pytest.raises(org_fonts.FontRefused, match="أقرّ"):
        org_fonts.add_fonts(org, [upload("a.ttf", naskh())], confirmed=False)


def test_files_of_one_family_make_one_face_stored_by_content(org, admin, settings):
    font = add_naskh(org, admin)
    assert font.key == f"org-{font.pk}" and font.name == "نسخ تجريبي" and font.family == FAMILY
    assert font.styles() == ["regular", "bold"] and font.latin and font.arabic
    assert font.licence.startswith("Test licence")
    assert font.licence_confirmed_by == admin and font.uploaded_by == admin
    sha = font.files["regular"]["sha256"]
    assert font.regular.name == f"orgs/{org.pk}/fonts/{sha}.ttf"
    assert (Path(settings.MEDIA_ROOT) / font.regular.name).read_bytes() == naskh()
    assert font.files["bold"]["source_name"] == "TestNaskh-Bold.ttf" and font.files["bold"]["weight"] == 700
    # an italic uploaded later joins the face; a regular file uploaded again replaces the regular one
    org_fonts.add_fonts(
        org, [upload("TestNaskh-Italic.ttf", synthetic(FAMILY, "Italic", italic=True))], confirmed=True
    )
    font.refresh_from_db()
    assert font.styles() == ["regular", "bold", "italic"] and OrganizationFont.objects.count() == 1


def test_a_batch_with_an_error_stores_nothing(org):
    good = naskh(family="Batch Naskh")
    files = [upload("a.ttf", good), upload("b.ttf", good), upload("c.ttf", synthetic(fs_type=2))]
    with pytest.raises(org_fonts.FontRefused) as refused:
        org_fonts.add_fonts(org, files, confirmed=True)
    reasons = refused.value.messages
    assert any("كلاهما" in reason for reason in reasons)
    assert any("يمنع تضمينه" in reason for reason in reasons)
    assert not OrganizationFont.objects.exists()
    sha = hashlib.sha256(good).hexdigest()
    assert not default_storage.exists(f"orgs/{org.pk}/fonts/{sha}.ttf")
    with pytest.raises(org_fonts.FontRefused, match="بلا ملف عادي"):
        org_fonts.add_fonts(org, [upload("bold.ttf", naskh("Bold"))], confirmed=True)


# ====================================================================== the registry


def test_an_organisation_face_resolves_like_any_face(org, other_org):
    font = add_naskh(org)
    resolved = F.resolve(font.key, "times", font.key)
    assert (
        resolved.body.key == font.key
        and resolved.body.name == "نسخ تجريبي"
        and resolved.body.family == FAMILY
    )
    assert resolved.body.files.regular.read_bytes() == naskh() and resolved.body.files.bold is not None
    assert resolved.body.licence.startswith("Test licence") and not resolved.missing
    assert F.display_name(font.key) == "نسخ تجريبي" and F.spec_of(font.key).org
    # the menus: after the registry's faces, shown in itself
    status = F.font_status(org)
    keys = [item["key"] for item in status]
    assert keys[: len(F.FONTS)] == list(F.FONTS) and keys[-1] == font.key
    entry = status[-1]
    assert (
        entry["family"] == f"nk-org-{font.pk}"
        and entry["org"]
        and entry["installed"]
        and not entry["removed"]
    )
    assert font.key in F.latin_keys(org) and font.key not in F.latin_keys(other_org)
    assert all(item["key"] != font.key for item in F.font_status(other_org))
    # what a book of each organisation may choose
    assert F.face_error(font.key, org, "body") == "" and F.face_error(font.key, org, "latin") == ""
    assert F.face_error(font.key, other_org, "body") == F.UNKNOWN_FACE
    assert F.face_error(font.key, None, "heading") == F.UNKNOWN_FACE
    assert F.face_error("org-999999", org, "latin") == F.NO_LATIN
    latin_only = org_fonts.add_fonts(
        org, [upload("latin.ttf", synthetic("Latin Box", arabic=False))], confirmed=True
    )[0]
    assert F.face_error(latin_only.key, org, "body") == F.NO_ARABIC
    assert F.face_error(latin_only.key, org, "latin") == ""
    # the browser takes the stored file from its members-only address
    css = F.browser_font_css(resolved)
    url = reverse("accounts:font_file", args=[font.pk, Path(font.regular.name).name])
    assert f'url("{url}")' in css and "local(" not in css.split("nk-latin")[0]
    assert f'"nk-org-{font.pk}"' in F.org_sample_css(org) and url in F.org_sample_css(org)


def test_a_removed_face_falls_back_to_amiri_and_says_so(org):
    font = add_naskh(org)
    org_fonts.remove_font(font)
    resolved = F.resolve(font.key, "amiri", "amiri")
    assert resolved.body.key == "amiri" and resolved.body.fallback
    assert resolved.missing == [
        {
            "field": "body_font",
            "key": font.key,
            "name": "نسخ تجريبي",
            "message": F.REMOVED_MESSAGE,
            "fallback": "Amiri",
            "removed": True,
        }
    ]
    assert F.face_error(font.key, org, "body") == F.REMOVED_FACE
    assert all(item["key"] != font.key for item in F.font_status(org))
    kept = [item for item in F.font_status(org, keep=[font.key]) if item["key"] == font.key]
    assert kept and kept[0]["removed"] and not kept[0]["installed"] and kept[0]["label"] == "نسخ تجريبي"
    org_fonts.restore_font(font)
    assert F.resolve(font.key, "amiri", "amiri").body.key == font.key
    # deleted for good: the key is all that is left
    key = font.key
    org_fonts.remove_font(font)
    org_fonts.delete_font(font)
    gone = F.resolve(key, "amiri", "amiri")
    assert gone.body.key == "amiri" and gone.missing[0]["removed"]
    assert gone.missing[0]["name"] == F.DELETED_NAME
    assert F.font_status(org, keep=[key])[-1]["label"] == F.DELETED_NAME
    assert F.display_name(key) == F.DELETED_NAME


def test_delete_keeps_a_face_books_use_and_files_another_face_shares(org, other_org):
    font = add_naskh(org)
    book = org_book(org, body_font=font.key)
    with pytest.raises(org_fonts.FontRefused, match="احذف الخط من القائمة أولًا"):
        org_fonts.delete_font(font)
    org_fonts.remove_font(font)
    with pytest.raises(org_fonts.FontRefused, match="كتاب واحد"):
        org_fonts.delete_font(font)
    assert list(org_fonts.books_using(font)) == [book]
    twin = add_naskh(other_org, bold=False)  # the same regular bytes, another organisation's folder
    StyleSheet.objects.filter(book=book).update(body_font="amiri")
    names = [font.regular.name, font.bold.name]
    org_fonts.delete_font(font)
    assert not OrganizationFont.objects.filter(pk=font.pk).exists()
    assert not any(default_storage.exists(name) for name in names)
    assert default_storage.exists(twin.regular.name)


# ====================================================================== the page, its permissions, the files


def test_the_page_lists_the_faces_and_only_admins_change_them(org, admin, editor):
    font = add_naskh(org, admin)
    org_book(org, body_font=font.key)
    page = logged(editor).get(reverse("accounts:organization"))
    body = page.content.decode()
    assert page.status_code == 200 and "نسخ تجريبي" in body and f"nk-org-{font.pk}" in body
    assert "كتاب واحد" in body and "إضافة الخط" not in body and "data-remove-font" not in body
    assert logged(editor).post(reverse("accounts:font_remove", args=[font.pk])).status_code == 403
    admin_body = logged(admin).get(reverse("accounts:organization")).content.decode()
    assert (
        "إضافة الخط" in admin_body
        and "data-remove-font" in admin_body
        and "تُرتَّب صفحاتها بخط أميري" in admin_body
    )
    # the sidebar leads there
    assert reverse("accounts:organization") in logged(editor).get(reverse("books:list")).content.decode()


def test_upload_rename_remove_restore_through_the_page(org, admin):
    client = logged(admin)
    url = reverse("accounts:font_upload")
    refused = client.post(
        url, {"files": [upload("a.ttf", synthetic(fs_type=2))], "confirm": "on"}, follow=True
    )
    assert "يمنع تضمينه" in refused.content.decode() and not OrganizationFont.objects.exists()
    response = client.post(
        url,
        {"files": [upload("r.ttf", naskh()), upload("b.ttf", naskh("Bold"))], "confirm": "on", "name": "نسخ"},
        follow=True,
    )
    assert "أُضيف إلى خطوط المؤسسة" in response.content.decode()
    font = OrganizationFont.objects.get()
    assert font.name == "نسخ" and font.styles() == ["regular", "bold"]
    client.post(reverse("accounts:font_edit", args=[font.pk]), {"name": "نسخ المدار", "licence": "OFL"})
    font.refresh_from_db()
    assert (font.name, font.licence) == ("نسخ المدار", "OFL")
    book = org_book(org, body_font=font.key)
    removed = client.post(reverse("accounts:font_remove", args=[font.pk]), follow=True).content.decode()
    assert "تُرتَّب الآن بخط أميري" in removed and "الخطوط المحذوفة" in removed
    font.refresh_from_db()
    assert font.removed and StyleSheet.objects.get(book=book).body_font == font.key  # the book keeps the key
    item = removed[removed.index(f'data-removed-font="{font.key}"') :]
    item = item[: item.index("</li>")]
    assert "إعادة" in item and "حذف نهائي" not in item  # a book uses it: no delete for good
    client.post(reverse("accounts:font_restore", args=[font.pk]))
    font.refresh_from_db()
    assert not font.removed


def test_the_font_files_go_to_the_organisations_members_only(org, other_org, admin):
    font = add_naskh(org, admin)
    filename = Path(font.regular.name).name
    url = reverse("accounts:font_file", args=[font.pk, filename])
    response = logged(admin).get(url)
    assert response.status_code == 200 and response["Content-Type"] == "font/ttf"
    assert b"".join(response.streaming_content) == naskh() and "immutable" in response["Cache-Control"]
    stranger = user("stranger", "editor", other_org)
    assert logged(stranger).get(url).status_code == 404
    assert logged(admin).get(reverse("accounts:font_file", args=[font.pk, "x.ttf"])).status_code == 404
    assert Client().get(url).status_code == 302
    # /media/ never serves an organisation's files, however the path is written
    for path in (font.regular.name, f"books/../{font.regular.name}"):
        assert logged(admin).get(f"/media/{path}").status_code == 404


def test_a_superuser_and_a_user_of_the_only_organisation_are_its_members(org):
    from accounts.services import is_member, is_org_admin, organization_for

    root = User.objects.create_superuser("root", password="pass-1234")
    loner = user("loner", "editor")
    boss = user("boss", "admin")
    assert organization_for(loner) == org and is_member(loner, org) and not is_org_admin(loner, org)
    assert is_org_admin(root, org) and is_org_admin(boss, org)
    Organization.objects.create(name="ثانية")
    assert organization_for(loner) is None and not is_member(
        loner, org
    )  # among several: a membership is needed
    assert organization_for(root) == org and is_member(root, org)


# ====================================================================== the book page


def test_the_book_page_offers_the_faces_and_checks_them(org, other_org, editor):
    from editor.tests import put_json

    font = add_naskh(org)
    stranger = add_naskh(other_org, bold=False)
    book = org_book(org)
    client = logged(editor)
    url = reverse("api:stylesheet", args=[book.pk])
    payload = client.get(url).json()
    entry = next(item for item in payload["fonts"] if item["key"] == font.key)
    assert entry["org"] and entry["label"] == "نسخ تجريبي" and font.key in payload["latin_fonts"]
    assert all(item["key"] != stranger.key for item in payload["fonts"])
    saved = put_json(client, url, {"body_font": font.key, "heading_font": font.key})
    assert saved.status_code == 200 and saved.json()["stylesheet"]["body_font"] == font.key
    bad = put_json(client, url, {"body_font": stranger.key, "latin_font": stranger.key})
    assert bad.status_code == 400 and set(bad.json()["errors"]) == {"body_font", "latin_font"}
    # the page: the face in itself for the menus, the live pages from the members-only file
    body = client.get(reverse("editor:layout", args=[book.pk])).content.decode()
    assert "<style data-org-fonts>" in body and f"nk-org-{font.pk}" in body
    assert reverse("accounts:font_file", args=[font.pk, Path(font.regular.name).name]) in body
    # removed: the menus keep its name with «محذوف», the panel says why Amiri stands in
    org_fonts.remove_font(font)
    payload = client.get(url).json()
    entry = next(item for item in payload["fonts"] if item["key"] == font.key)
    assert entry["removed"] and payload["missing_fonts"][0]["removed"]
    assert (
        put_json(client, url, {"heading_font": font.key}).json()["errors"]["heading_font"] == F.REMOVED_FACE
    )
    assert put_json(client, url, {"heading_font": "amiri"}).status_code == 200  # another face is chosen


# ====================================================================== the face in every output


@pytest.fixture
def naskh_book(org):
    font = add_naskh(org)
    return org_book(org, body_font=font.key, heading_font=font.key, latin_font=font.key), font


def _job(book, fmt: str, options: dict | None = None):
    from publishing.exporters import get_exporter, parse_options
    from publishing.exports import read_inputs

    values = parse_options(get_exporter(fmt).options, options or {})
    return read_inputs(book, fmt, values)[0]


def _pdf_fonts(data: bytes) -> list[str]:
    """The full names (name ID 4) of the font programs a PDF embeds (WeasyPrint names its fonts by the CSS
    family, `nk-body`; the subset keeps the face's own name table)."""
    names: set[str] = set()
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        for xref in {font[0] for page in pdf for font in page.get_fonts(full=True)}:
            _name, _ext, _type, buffer = pdf.extract_font(xref)
            try:
                font = TTFont(io.BytesIO(buffer))
                names.add(font["name"].getDebugName(4) or "")
            except Exception:  # noqa: BLE001 - not a TrueType program
                continue
    return sorted(names)


def test_the_preview_and_both_pdfs_embed_the_face(naskh_book):
    from publishing.engine import RenderJob, get_engine
    from publishing.exporters import NullProgress, get_exporter
    from publishing.model import page_setup
    from publishing.preview import stylesheet_for

    book, font = naskh_book
    document = book.manuscript.document
    rendered = get_engine().render(RenderJob(document=document, stylesheet=page_setup(stylesheet_for(book))))
    assert not rendered.missing_fonts and rendered.page_count > 1 and rendered.misses == 0
    names = _pdf_fonts(rendered.pdf)
    assert FAMILY in names and f"{FAMILY} Bold" in names, names
    for fmt in ("print_pdf", "screen_pdf"):
        result = get_exporter(fmt).export(_job(book, fmt), NullProgress())
        names = _pdf_fonts(result.data)
        assert FAMILY in names and f"{FAMILY} Bold" in names, (fmt, names)
        assert all(row["code"] not in ("foreign_fonts", "missing_font") for row in result.warnings), fmt
        with pymupdf.open(stream=result.data, filetype="pdf") as pdf:
            assert "الفصل" in pdf[1].get_text() or "الفصل" in "".join(page.get_text() for page in pdf)


def test_word_embeds_the_face_whole_and_names_it(naskh_book):
    from publishing.exporters import NullProgress, get_exporter
    from publishing.word import faces
    from publishing.word.exporter import static_notes
    from publishing.word.writer import build_docx  # noqa: F401 - the exporter's writer

    book, font = naskh_book
    result = get_exporter("docx").export(_job(book, "docx"), NullProgress())
    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        table = archive.read("word/fontTable.xml").decode()
        assert f'w:name="{FAMILY}"' in table and "w:embedRegular" in table and "w:embedBold" in table
        embedded = [name for name in archive.namelist() if name.startswith("word/fonts/")]
        key = faces.font_key(FAMILY, "regular")
        datas = [faces.obfuscate(archive.read(name), key) for name in embedded]
        assert naskh() in datas
        document = archive.read("word/document.xml").decode()
        styles = archive.read("word/styles.xml").decode()
    assert FAMILY in styles and "<w:t" in document
    assert all(row["code"] not in ("font_missing", "font_removed") for row in result.warnings)
    from publishing.model import page_setup

    setup = page_setup(StyleSheet.objects.get(book=book))
    rows = static_notes(
        setup, F.resolve(font.key, font.key, font.key), has_contents=False, layout_current=True
    )
    assert [row["code"] for row in rows][:1] == ["font_embedded_org"] and "نسخ تجريبي" in rows[0]["message"]


def test_epub_carries_the_face_and_its_licence(naskh_book):
    from publishing.epub import check_epub
    from publishing.exporters import NullProgress, get_exporter

    book, font = naskh_book
    result = get_exporter("epub").export(_job(book, "epub"), NullProgress())
    assert check_epub(result.data) == []
    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        regular = archive.read(f"EPUB/fonts/{font.key}-regular.ttf")
        bold = archive.read(f"EPUB/fonts/{font.key}-bold.ttf")
        css = archive.read("EPUB/styles/book.css").decode()
        licence = archive.read(f"EPUB/fonts/{font.key}-licence.txt").decode()
        package = archive.read("EPUB/content.opf").decode()
    assert regular == naskh() and bold == naskh("Bold") and licence.startswith("Test licence")
    assert f'url("../fonts/{font.key}-regular.ttf")' in css and f'url("../fonts/{font.key}-bold.ttf")' in css
    assert f"fonts/{font.key}-regular.ttf" in package and 'media-type="font/ttf"' in package
    assert all(row["code"] not in ("font_fallback", "missing_font") for row in result.warnings)


def test_a_removed_face_is_amiri_in_every_output_with_a_notice(naskh_book):
    from publishing.exporters import NullProgress, get_exporter
    from publishing.readiness import missing_font_rows

    book, font = naskh_book
    org_fonts.remove_font(font)
    setup = _job(book, "epub").setup
    rows = missing_font_rows(book.pk, setup)
    assert rows and "حُذف الخط «نسخ تجريبي» من خطوط المؤسسة" in rows[0]["message"]
    epub = get_exporter("epub").export(_job(book, "epub"), NullProgress())
    with zipfile.ZipFile(io.BytesIO(epub.data)) as archive:
        assert not any(name.startswith(f"EPUB/fonts/{font.key}") for name in archive.namelist())
    word = get_exporter("docx").export(_job(book, "docx"), NullProgress())
    assert any(row["code"] == "font_removed" for row in word.warnings)
    pdf = get_exporter("screen_pdf").export(_job(book, "screen_pdf"), NullProgress())
    names = _pdf_fonts(pdf.data)
    assert FAMILY not in names and "Amiri" in names, names
    assert any("حُذف" in row["message"] for row in pdf.warnings if row["code"] == "missing_font")


def test_word_names_a_face_it_may_not_carry_and_says_why(org):
    from publishing.model import page_setup
    from publishing.word import faces
    from publishing.word.exporter import static_notes

    cff = org_fonts.add_fonts(org, [upload("box.otf", synthetic("CFF Box", cff=True))], confirmed=True)[0]
    printing = org_fonts.add_fonts(
        org, [upload("p.ttf", synthetic("Print Box", fs_type=0x0004))], confirmed=True
    )[0]
    for font, reason in ((cff, "OpenType CFF"), (printing, "بالعرض والطباعة فقط")):
        resolved = F.resolve(font.key, "amiri", "amiri")
        assert not faces.embeddable(resolved.body) and F.org_embeds(resolved.body, "epub")
        assert F.org_embeds(resolved.body, "pdf")
        rows = static_notes(
            page_setup({"body_font": font.key}), resolved, has_contents=False, layout_current=True
        )
        note = next(row for row in rows if row["code"] == "font_not_embedded_org")
        assert reason in note["message"]
