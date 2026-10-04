"""Book access per organisation (D102, `books.access`): two organisations, each with a book that has a page
with its images, a line, a suggestion, a finished export and a re-layout. A member of one never lists,
opens, posts to, downloads or fetches the images of the other's book (404, as for a missing one), across
every route that takes a book, page, line, suggestion, export or render id (found from the URL
configuration, so a new route is covered by itself); a member still reaches their own book; a superuser
reaches both; a user without a membership sees no book."""

from __future__ import annotations

import json
import re

from django.contrib.auth.models import Group, User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver, reverse

import numpy as np
import pytest

from accounts.models import Organization
from accounts.services import NOT_A_MEMBER, ORGANIZATION_SESSION_KEY
from accounts.testing import member, owned
from books import access
from books.models import Book, Page
from core.storage import save_array
from ocr.models import Line, TextGap
from processing.models import Preprocess, Region
from publishing.models import Export, PreviewRender

pytestmark = pytest.mark.django_db

W, H = 100, 200
# the ids a route takes that lead to a book (the routes the test walks); the other arguments get a value
SCOPED_KEYS = ("book_id", "page_id", "line_id", "gap_id", "export_id", "render_id")
OTHER_VALUES = {"number": "1", "chapter_id": "h1", "plan_id": "1", "snapshot_id": "1", "template_id": "1"}
OTHER_VALUES |= {"batch": "x", "group": "0"}
# the 404 messages of `books.access` and of the lookups built on it: another organisation's row answers one
ACCESS_MESSAGES = {
    access.BOOK_NOT_FOUND,
    access.PAGE_NOT_FOUND,
    "السطر غير موجود.",
    "الاقتراح غير موجود.",
    "الإخراج غير موجود.",
    "إعادة الترتيب غير موجودة.",
    "الملف غير جاهز.",
}


# ====================================================================== two organisations


def _user(name: str, role: str | None, organization=None, **extra) -> User:
    person = User.objects.create_user(name, password="pass-1234", **extra)
    if role:
        person.groups.add(Group.objects.get_or_create(name=role)[0])
    if organization is not None:
        member(person, organization)
    return person


def _client(person) -> Client:
    client = Client()
    client.force_login(person)
    return client


def _library(organization, title: str) -> dict:
    """A book of `organization` with one page (its images on disk), a line, an open suggestion, a finished
    export with its file and a re-layout row: every kind of id a route takes."""
    book = owned(Book.objects.create(title=title, status=Book.Status.READY_FOR_REVIEW), organization)
    page = Page.objects.create(
        book=book,
        number=1,
        source_index=0,
        status=Page.Status.OCR_DONE,
        text_state=Page.TextState.FINAL,
        width=W,
        height=H,
    )
    pre = Preprocess.objects.create(page=page, output_width=W, output_height=H)
    for field, name in ((pre.gray_image, "gray.png"), (pre.display_image, "display.webp")):
        save_array(field, np.full((H, W), 230, dtype=np.uint8), name)
    save_array(pre.thumbnail, np.full((20, 10), 230, dtype=np.uint8), "thumb.webp")
    pre.save()
    region = Region.objects.create(page=page, kind="body", bbox=[0, 0, W, H], order=0)
    tokens = [{"t": "قال", "alt": None, "tess": None, "conf": "high", "digit": False, "bbox": [0, 0, W, 20]}]
    line = Line.objects.create(
        page=page, order=0, region=region, bbox=[0, 0, W, 20], text="قال", ocr_text="قال", tokens=tokens
    )
    gap = TextGap.objects.create(page=page, line=line, index=0, after_t="قال", text="الشيخ")
    export = Export.objects.create(book=book, format=Export.Format.DOCX, filename=f"{title}.docx")
    name = default_storage.save(f"books/{book.pk}/exports/{export.pk}.docx", ContentFile(b"PK-word"))
    Export.objects.filter(pk=export.pk).update(status=Export.Status.DONE, file=name)
    render = PreviewRender.objects.create(book=book, kind=PreviewRender.Kind.LAYOUT, content_hash="x" * 16)
    return {
        "organization": organization,
        "book": book,
        "page": page,
        "line": line,
        "gap": gap,
        "export": Export.objects.get(pk=export.pk),
        "render": render,
        "thumb": pre.thumbnail.name,
    }


@pytest.fixture
def ours(db) -> dict:
    return _library(Organization.objects.create(name="دار الأولى"), "كتابنا")


@pytest.fixture
def theirs(db) -> dict:
    return _library(Organization.objects.create(name="دار الثانية"), "كتابهم")


@pytest.fixture
def editor(ours) -> User:
    """An editor of our organisation: every role check passes, so only the organisation refuses."""
    return _user("our-editor", "editor", ours["organization"])


# ====================================================================== the routes


def _walk(patterns, prefix: str = "", namespace: str = ""):
    for entry in patterns:
        if isinstance(entry, URLResolver):
            if str(entry.pattern).startswith("admin/"):
                continue  # Django admin: superusers and staff (internal)
            inner = f"{namespace}{entry.namespace}:" if entry.namespace else namespace
            yield from _walk(entry.url_patterns, prefix + str(entry.pattern), inner)
        elif isinstance(entry, URLPattern):
            yield f"{namespace}{entry.name}", prefix + str(entry.pattern)


def scoped_routes() -> list[tuple[str, str]]:
    """Every route that takes the id of a book, a page, a line, a suggestion, an export or a render:
    `(name, route)`, one per distinct route."""
    seen: dict[str, str] = {}
    for name, route in _walk(get_resolver().url_patterns):
        keys = re.findall(r"<(?:\w+:)?(\w+)>", route)
        if any(key in SCOPED_KEYS for key in keys):
            seen.setdefault(route, name)
    return sorted((name, route) for route, name in seen.items())


ROUTES = scoped_routes()


def _url(route: str, rows: dict) -> str:
    ids = {
        "book_id": rows["book"].pk,
        "page_id": rows["page"].pk,
        "line_id": rows["line"].pk,
        "gap_id": rows["gap"].pk,
        "export_id": rows["export"].pk,
        "render_id": rows["render"].pk,
    }

    def value(match: re.Match) -> str:
        key = match.group(1)
        return str(ids[key]) if key in ids else OTHER_VALUES[key]

    return "/" + re.sub(r"<(?:\w+:)?(\w+)>", value, route)


def _access_404(response) -> bool:
    """A 404 of the access rule (or of a missing row: they are the same answer)."""
    if response.status_code != 404:
        return False
    if response.get("Content-Type", "").startswith("application/json"):
        return json.loads(response.content or b"{}").get("detail") in ACCESS_MESSAGES
    return True  # an HTML 404 page says nothing more


def test_the_walk_finds_every_kind_of_route():
    names = {name for name, _route in ROUTES}
    assert len(ROUTES) >= 70
    for name in (
        "books:detail",
        "books:delete",
        "books:rerun",
        "books:toggle_exclude",
        "review:page",
        "assembly:manuscript",
        "editor:layout",
        "publishing:export_download",
        "api:book_sheets",
        "api:page_review",
        "api:line_edit",
        "api:gap_accept",
        "api:page_text",
        "api:page_runs",
        "api:page_guides_override",
        "api:book_guides",
        "api:chapter",
        "api:stylesheet",
        "api:book_images",
        "api:cover",
        "api:preview",
        "api:relayout_status",
        "api:export",
        "api:export_cancel",
        "api:book_templates",
        "api:book_template_apply",
        "api:fix_everywhere",
    ):
        assert name in names, name


@pytest.mark.parametrize(("name", "route"), ROUTES, ids=[name for name, _ in ROUTES])
def test_another_organisations_book_answers_404_on_every_route(name, route, ours, theirs, editor):
    """GET, POST, PUT and PATCH to the other organisation's book, page, line, suggestion, export or render:
    404 (405 where the method is not served), never an answer, and nothing changes."""
    client = _client(editor)
    url = _url(route, theirs)
    before = Book.objects.filter(pk=theirs["book"].pk).values().get()
    statuses = {}
    for method in ("get", "post", "put", "patch"):
        if method == "get":
            response = client.get(url)
        else:
            response = getattr(client, method)(url, json.dumps({}), content_type="application/json")
        statuses[method] = response.status_code
        assert response.status_code in (404, 405), (method, response.status_code)
        if response.status_code == 404:
            assert _access_404(response), (method, response.content[:200])
    assert 404 in statuses.values()
    assert Book.objects.filter(pk=theirs["book"].pk).values().get() == before
    assert Line.objects.get(pk=theirs["line"].pk).text == "قال"
    assert TextGap.objects.get(pk=theirs["gap"].pk).status == TextGap.Status.OPEN


# The routes whose GET on a book never assembled answers 404 for a reason of their own (no manuscript, no
# layout yet), with their own message: never the access rule's. (`assembly:document` is an HTML fragment.)
OWN_404 = {
    "assembly:document",
    "api:manuscript",
    "api:chapter",
    "api:chapters",
    "api:uncertain",
    "api:preview",
    "api:preview_layout",
    "api:snapshots",
}


@pytest.mark.parametrize(("name", "route"), ROUTES, ids=[name for name, _ in ROUTES])
def test_a_member_and_a_superuser_still_reach_the_book(name, route, ours, theirs, editor):
    """The same GET: a member of the book's organisation and a superuser (of no organisation, the other
    book) get the screen or the answer (200, a redirect, 400 for the missing query, 405 for a POST-only
    route: their POSTs are the other suites' tests, run by members), never the access rule's 404."""
    root = User.objects.create_superuser("root", password="pass-1234")
    for person, rows in ((editor, ours), (root, theirs)):
        response = _client(person).get(_url(route, rows))
        if response.status_code == 404:
            assert name in OWN_404, (person.username, response.content[:200])
            assert name == "assembly:document" or not _access_404(response), response.content[:200]
        else:
            assert response.status_code in (200, 302, 400, 405), (person.username, response.status_code)


# ====================================================================== the home, media, the API by hand


def test_the_books_home_lists_the_organisations_books_only(ours, theirs, editor):
    body = _client(editor).get(reverse("books:list")).content.decode()
    assert "كتابنا" in body and "كتابهم" not in body
    root = User.objects.create_superuser("root", password="pass-1234")
    body = _client(root).get(reverse("books:list")).content.decode()
    assert "كتابنا" in body and "كتابهم" in body


def test_a_user_without_a_membership_sees_no_book(ours, theirs):
    loner = _user("loner", "editor")
    client = _client(loner)
    body = client.get(reverse("books:list")).content.decode()
    assert "لا تنتمي إلى مؤسسة بعد" in body and "كتابنا" not in body and "كتابهم" not in body
    assert "كتاب جديد" not in body  # no book to add either
    for rows in (ours, theirs):
        assert client.get(reverse("books:detail", args=[rows["book"].pk])).status_code == 404
        assert client.get(f"/media/{rows['thumb']}").status_code == 404
        assert client.get(reverse("api:page_text", args=[rows["page"].pk])).status_code == 404
    refused = client.get(reverse("books:create"), follow=True)
    assert refused.redirect_chain == [(reverse("books:list"), 302)]
    assert NOT_A_MEMBER in refused.content.decode()
    assert access.books_for(loner).count() == 0 and not access.has_organization(loner)


def test_media_is_served_for_the_organisations_books_only(ours, theirs, editor):
    client = _client(editor)
    own = client.get(f"/media/{ours['thumb']}")
    assert own.status_code == 200 and own["Cache-Control"] == "private, no-cache"
    own.close()
    for path in (
        theirs["thumb"],
        theirs["export"].file.name,
        f"books/{ours['book'].pk}/../{theirs['thumb']}",  # normalised before the check: book B's
        f"books/{ours['book'].pk}/../../{theirs['thumb']}",
        "books/",
        "other/file.txt",
    ):
        assert client.get(f"/media/{path}").status_code == 404, path
    download = reverse("publishing:export_download", args=[theirs["book"].pk, theirs["export"].pk])
    assert client.get(download).status_code == 404
    mine = client.get(reverse("publishing:export_download", args=[ours["book"].pk, ours["export"].pk]))
    assert mine.status_code == 200 and b"".join(mine.streaming_content) == b"PK-word"
    root = User.objects.create_superuser("root", password="pass-1234")
    theirs_file = _client(root).get(f"/media/{theirs['thumb']}")
    assert theirs_file.status_code == 200
    theirs_file.close()


def test_a_proofreader_of_the_organisation_reads_but_another_ones_gets_404(ours, theirs):
    reader = _user("our-reader", "proofreader", ours["organization"])
    client = _client(reader)
    assert client.get(reverse("review:page", args=[ours["book"].pk, 1])).status_code == 200
    assert client.get(reverse("review:page", args=[theirs["book"].pk, 1])).status_code == 404
    edit = reverse("api:line_edit", args=[theirs["line"].pk])
    response = client.post(edit, json.dumps({"text": "قال الشيخ"}), content_type="application/json")
    assert response.status_code == 404 and Line.objects.get(pk=theirs["line"].pk).text == "قال"
    own = client.post(
        reverse("api:line_edit", args=[ours["line"].pk]),
        json.dumps({"text": "قال الشيخ"}),
        content_type="application/json",
    )
    assert own.status_code == 200 and Line.objects.get(pk=ours["line"].pk).text == "قال الشيخ"


def test_a_book_without_an_organisation_is_the_superusers_alone(ours, editor):
    Book.objects.filter(pk=ours["book"].pk).update(organization=None)
    assert _client(editor).get(reverse("books:detail", args=[ours["book"].pk])).status_code == 404
    root = User.objects.create_superuser("root", password="pass-1234")
    assert _client(root).get(reverse("books:detail", args=[ours["book"].pk])).status_code == 200
    assert access.may_access(root, ours["book"].pk) and not access.may_access(editor, ours["book"].pk)


def test_the_helpers(ours, theirs, editor):
    root = User.objects.create_superuser("root", password="pass-1234")
    assert set(access.books_for(editor)) == {ours["book"]}
    assert set(access.books_for(root)) == {ours["book"], theirs["book"]}
    assert set(access.pages_for(editor)) == {ours["page"]}
    assert access.may_access(editor, ours["book"]) and not access.may_access(editor, theirs["book"])
    assert not access.may_access(editor, theirs["book"].pk) and not access.may_access(editor, None)
    assert access.get_book_or_404(editor, ours["book"].pk) == ours["book"]
    with pytest.raises(access.Http404):
        access.get_book_or_404(editor, theirs["book"].pk)
    with pytest.raises(access.Http404):
        access.get_page_or_404(editor, book_id=theirs["book"].pk, number=1)
    assert access.get_page_or_404(root, book_id=theirs["book"].pk, number=1) == theirs["page"]


def test_a_new_book_takes_the_organisation_the_user_works_in(ours, theirs, editor, settings):
    from django.core.files.uploadedfile import SimpleUploadedFile

    import pymupdf

    from books.services import create_book

    pdf = pymupdf.open()
    pdf.new_page()
    data = pdf.tobytes()
    book = create_book({"title": "جديد"}, SimpleUploadedFile("a.pdf", data, "application/pdf"), editor)
    assert book.organization == ours["organization"]
    other = create_book(
        {"title": "آخر"},
        SimpleUploadedFile("b.pdf", data, "application/pdf"),
        editor,
        organization=theirs["organization"],
    )
    assert other.organization == theirs["organization"]


def test_a_superuser_switches_the_organisation_they_work_in(ours, theirs, editor):
    root = User.objects.create_superuser("root", password="pass-1234")
    client = _client(root)
    page = client.get(reverse("accounts:organization")).content.decode()
    assert 'id="og-switch"' in page and "العمل في" in page and "دار الثانية" in page
    switch = reverse("accounts:organization_switch")
    assert client.post(switch, {"organization": theirs["organization"].pk}).status_code == 302
    assert client.session[ORGANIZATION_SESSION_KEY] == theirs["organization"].pk
    assert "<title>دار الثانية · نسّاخ" in client.get(reverse("accounts:organization")).content.decode()
    assert client.post(switch, {"organization": "x"}).status_code == 404
    assert _client(editor).post(switch, {"organization": theirs["organization"].pk}).status_code == 403
    page = _client(editor).get(reverse("accounts:organization")).content.decode()
    assert 'id="og-switch"' not in page


def test_the_user_admin_sets_the_organisation(ours, editor):
    """The user's page in Django admin carries their organisation (one), the users' list shows it."""
    root = User.objects.create_superuser("root", password="pass-1234")
    loner = _user("loner", "editor")
    client = _client(root)
    page = client.get(reverse("admin:auth_user_change", args=[loner.pk])).content.decode()
    assert 'name="membership-0-organization"' in page and 'name="membership-0-role"' in page
    assert 'name="membership-MAX_NUM_FORMS" value="1"' in page
    added = client.get(reverse("admin:auth_user_add")).content.decode()
    assert 'name="membership-0-organization"' in added
    listing = client.get(reverse("admin:auth_user_changelist")).content.decode()
    assert "دار الأولى" in listing  # the editor's organisation, in its column
