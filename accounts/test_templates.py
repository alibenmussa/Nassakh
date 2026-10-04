"""An organisation's format templates (D98): what a template keeps, the changes it brings to a book,
applying it through the stylesheet's own save, the book page's «قوالب المؤسسة» (its API and its component
under Node), the organisation's page, and the organisation a book and a user belong to (the migration
and `create_book`)."""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.urls import reverse

import pytest

from accounts import fonts as org_fonts
from accounts import styles
from accounts.models import Membership, Organization, StyleTemplate
from accounts.test_fonts import add_naskh, logged, org_book, user
from books.models import Book
from editor.models import StyleSheet
from editor.tests import post_json

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


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


@pytest.fixture
def reader(org):
    return user("org-reader", "proofreader", org)


def styled_book(org, **values):
    sheet = {
        "trim": "a5",
        "width_mm": 148.0,
        "height_mm": 210.0,
        "top_mm": 18.0,
        "body_size_pt": 12.0,
        "line_height": 1.6,
        "heading_scale": {"h1": 2.0, "h2": 1.4},
        "running_header": "chapter",
        "front_matter": {
            "title_page": True,
            "contents": False,
            "copyright_page": True,
            "fields": {"publisher": "دار المدار", "rights": "محفوظة"},
            "cover": {"mode": "text", "center": "غلاف"},
        },
        **values,
    }
    return org_book(org, **sheet)


# ====================================================================== what a template keeps


def test_a_template_keeps_the_format_not_the_book(org, editor):
    book = styled_book(org)
    template = styles.create_template(org, book, "  سلسلة   التراث ", "قطع A5", editor)
    values = template.values
    assert template.name == "سلسلة التراث" and template.source_book == book and template.created_by == editor
    assert (
        values["trim"] == "a5"
        and values["body_size_pt"] == 12.0
        and values["heading_scale"] == {"h1": 2.0, "h2": 1.4}
    )
    assert values["running_header"] == "chapter" and values["body_font"] == "amiri"
    assert values["front_matter"] == {"title_page": True, "contents": False, "copyright_page": True}
    assert "updated_at" not in values and "fields" not in values["front_matter"]
    # every field of the stylesheet's own list is kept (a field it gains later too)
    from editor.services import STYLESHEET_FIELDS

    assert set(STYLESHEET_FIELDS) <= set(values)
    with pytest.raises(styles.TemplateError, match="بهذا الاسم"):
        styles.create_template(org, book, "سلسلة التراث")
    with pytest.raises(styles.TemplateError, match="اسمًا"):
        styles.create_template(org, book, "   ")
    stranger = styled_book(Organization.objects.create(name="ثالثة"))
    with pytest.raises(styles.TemplateError, match="لا ينتمي"):
        styles.create_template(org, stranger, "من كتاب آخر")


def test_the_changes_are_said_in_the_panels_words(org):
    source = styled_book(org)
    template = styles.create_template(org, source, "A5")
    target = org_book(org)  # the defaults: 17×24, 13 pt, 1.7
    preview = styles.template_changes(target, template)
    rows = {row["field"]: (row["label"], row["before"], row["after"]) for row in preview["changes"]}
    assert rows["trim"] == ("القطع", "17×24 سم", "A5")
    assert rows["top_mm"] == ("الهامش العلوي", "20 مم", "18 مم")
    assert rows["body_size_pt"] == ("حجم المتن", "13 نقطة", "12 نقطة")
    assert rows["line_height"] == ("تباعد الأسطر", "1.7", "1.6")
    assert rows["heading_scale.h1"] == ("حجم عنوان الفصل", "1.6 ×", "2 ×")
    assert rows["running_header"] == ("الترويسة", "بلا ترويسة", "عنوان الفصل")
    assert rows["front_matter.contents"] == ("صفحة المحتويات", "نعم", "لا")
    assert "width_mm" not in rows and "body_font" not in rows and not preview["same"]
    assert styles.template_changes(source, template) == {"changes": [], "skipped": [], "same": True}
    assert styles.summary(template) == ["A5", "Amiri", "12 نقطة", "تباعد 1.6"]


def test_applying_goes_through_the_stylesheet_and_keeps_the_books_own(org, editor):
    source = styled_book(org)
    template = styles.create_template(org, source, "A5")
    target = org_book(org, front_matter={"fields": {"title": "عنوان خاص"}, "cover": {"mode": "info"}})
    result = styles.apply_template(target, template, editor)
    sheet = StyleSheet.objects.get(book=target)
    assert result["changed"] and not result["skipped"]
    assert (sheet.trim, sheet.width_mm, sheet.height_mm, sheet.top_mm) == ("a5", 148.0, 210.0, 18.0)
    assert sheet.running_header == "chapter" and sheet.heading_scale == {"h1": 2.0, "h2": 1.4}
    front = sheet.front_matter
    assert front["fields"] == {"title": "عنوان خاص"} and front["cover"]["mode"] == "info"
    assert front["contents"] is False and front["copyright_page"] is True
    assert styles.apply_template(target, template, editor)["changed"] is False


def test_a_face_the_organisation_removed_is_left_out(org):
    font = add_naskh(org)
    template = styles.create_template(org, styled_book(org, body_font=font.key, heading_font=font.key), "نسخ")
    org_fonts.remove_font(font)
    target = org_book(org)
    preview = styles.template_changes(target, template)
    assert [item["field"] for item in preview["skipped"]] == ["body_font", "heading_font"]
    assert preview["skipped"][0]["name"] == "نسخ تجريبي" and "حُذف" in preview["skipped"][0]["message"]
    result = styles.apply_template(target, template)
    sheet = StyleSheet.objects.get(book=target)
    assert sheet.body_font == "amiri" and sheet.trim == "a5" and len(result["skipped"]) == 2
    org_fonts.restore_font(font)
    assert styles.apply_template(target, template)["skipped"] == []
    assert StyleSheet.objects.get(book=target).body_font == font.key


# ====================================================================== the book page's API


def test_the_book_page_lists_saves_and_applies_templates(org, editor, reader, admin):
    source = styled_book(org)
    target = org_book(org)
    template = styles.create_template(org, source, "A5")
    url = reverse("api:book_templates", args=[target.pk])
    listed = logged(editor).get(url).json()
    assert listed["organization"] == {"id": org.pk, "name": "دار المدار"}
    assert listed["can_save"] is True and listed["can_manage"] is False
    row = listed["templates"][0]
    assert row["name"] == "A5" and row["source"] == {"id": source.pk, "title": source.title}
    assert not row["same"] and any(change["field"] == "trim" for change in row["changes"])
    # a proofreader reads them; saving and applying need an editor
    assert logged(reader).get(url).json()["can_save"] is False
    assert post_json(logged(reader), url, {"name": "ممنوع"}).status_code == 403
    apply_url = reverse("api:book_template_apply", args=[target.pk, template.pk])
    assert post_json(logged(reader), apply_url).status_code == 403
    # applied: the stylesheet's answer, as a PUT gives it
    applied = post_json(logged(editor), apply_url, {"chapter": "h1"})
    data = applied.json()
    assert applied.status_code == 200 and data["stylesheet"]["trim"] == "a5" and "preview" in data
    assert data["applied"] == {"id": template.pk, "name": "A5", "changed": True, "skipped": []}
    assert "cover_render" in data and data["fonts"] and data["saved"] is True
    assert logged(editor).get(url).json()["templates"][0]["same"] is True
    # saved from this book; the name is the organisation's own
    created = post_json(logged(editor), url, {"name": "قالب الكتاب", "description": "وصف"})
    assert created.status_code == 201 and created.json()["same"] is True
    assert post_json(logged(editor), url, {"name": "قالب الكتاب"}).json()["detail"] == styles.NAME_TAKEN
    # an admin takes a book's format into a template
    StyleSheet.objects.filter(book=target).update(body_size_pt=15)
    update_url = reverse("api:book_template_update", args=[target.pk, template.pk])
    assert post_json(logged(editor), update_url).status_code == 403
    assert post_json(logged(admin), update_url).status_code == 200
    template.refresh_from_db()
    assert template.values["body_size_pt"] == 15 and template.source_book == target


def test_another_organisations_book_sees_none_of_it(org, other_org, editor):
    styles.create_template(org, styled_book(org), "A5")
    stranger_book = org_book(other_org)
    url = reverse("api:book_templates", args=[stranger_book.pk])
    assert logged(editor).get(url).status_code == 404  # D102: another organisation's book, as a missing one
    template = StyleTemplate.objects.get()
    apply_url = reverse("api:book_template_apply", args=[stranger_book.pk, template.pk])
    assert post_json(logged(editor), apply_url).status_code == 404
    outsider = user("outsider", "editor", other_org)
    own = org_book(other_org)
    assert logged(outsider).get(reverse("api:book_templates", args=[own.pk])).json()["templates"] == []
    wrong = reverse("api:book_template_apply", args=[own.pk, template.pk])
    assert post_json(logged(outsider), wrong).status_code == 404


def test_the_format_tab_carries_the_section(org, editor):
    book = org_book(org)
    body = logged(editor).get(reverse("editor:layout", args=[book.pk])).content.decode()
    assert 'data-section="templates"' in body and "bookTemplates(" in body
    assert reverse("api:book_templates", args=[book.pk]) in body
    assert reverse("api:book_template_apply", args=[book.pk, 0]) in body and "/templates/__tid__/" in body
    assert "src/js/org.js" in body


# ====================================================================== the organisation's page


def test_the_page_saves_renames_updates_applies_and_deletes(org, admin, editor):
    source = styled_book(org)
    target = org_book(org)
    client = logged(editor)
    created = client.post(
        reverse("accounts:template_create"),
        {"book": source.pk, "name": "A5", "description": "صغير"},
        follow=True,
    )
    assert "حُفظ القالب «A5»" in created.content.decode()
    template = StyleTemplate.objects.get()
    page = client.get(reverse("accounts:organization")).content.decode()
    assert "A5" in page and "تطبيق على كتاب…" in page and "تحديث من كتاب" not in page
    # the preview of the changes, then the apply
    preview = client.get(reverse("accounts:template_apply", args=[template.pk]) + f"?book={target.pk}")
    body = preview.content.decode()
    assert (
        preview.status_code == 200 and "ما يتغيّر في" in body and "حجم المتن" in body and "data-apply" in body
    )
    applied = client.post(reverse("accounts:template_apply", args=[template.pk]), {"book": target.pk})
    assert applied.status_code == 302 and applied["Location"].endswith(
        f"/books/{target.pk}/layout/?tab=format"
    )
    assert StyleSheet.objects.get(book=target).trim == "a5"
    same = client.get(reverse("accounts:template_apply", args=[template.pk]) + f"?book={target.pk}")
    assert "لا شيء يتغيّر" in same.content.decode()
    # admins only: rename, update, delete
    rename = reverse("accounts:template_edit", args=[template.pk])
    assert client.post(rename, {"name": "B"}).status_code == 403
    admin_client = logged(admin)
    admin_client.post(rename, {"name": "سلسلة A5", "description": "وصف"})
    template.refresh_from_db()
    assert (template.name, template.description) == ("سلسلة A5", "وصف")
    StyleSheet.objects.filter(book=target).update(body_size_pt=16)
    admin_client.post(reverse("accounts:template_update", args=[template.pk]), {"book": target.pk})
    template.refresh_from_db()
    assert template.values["body_size_pt"] == 16
    admin_client.post(reverse("accounts:template_delete", args=[template.pk]))
    assert not StyleTemplate.objects.exists() and StyleSheet.objects.get(book=target).trim == "a5"


def test_the_page_needs_an_organisation(org, other_org):
    loner = User.objects.create_user("loner", password="pass-1234")
    assert logged(loner).get(reverse("accounts:organization")).status_code == 403
    member = user("member", "proofreader", other_org)
    page = logged(member).get(reverse("accounts:organization"))
    assert page.status_code == 200 and "دار أخرى" in page.content.decode()
    assert "data-template-create" not in page.content.decode()  # a proofreader saves none


# ====================================================================== the component under Node


COMPONENT_HARNESS = r"""
global.window = global;
const calls = [];
const answers = {
  '/list/': { ok: true, status: 200, data: { templates: [{ id: 7, name: 'A5', summary: ['A5'], changes: [{ field: 'trim', label: 'القطع', before: '17×24 سم', after: 'A5' }], skipped: [], same: false }], can_save: true, can_manage: true } },
  '/apply/7/': { ok: true, status: 200, data: { stylesheet: { trim: 'a5' }, preview: { status: 'queued' }, cover_render: null, applied: { id: 7, name: 'A5', changed: true, skipped: [{ label: 'خط المتن' }] } } },
};
global.NassakhBook = { util: { api: async (url, options) => { calls.push([url, options ? options.method : 'GET', options ? options.body : null]); return answers[url] || { ok: false, status: 404, data: null, message: 'x' }; } } };
const toasts = []; global.Nassakh = { toast: (m) => toasts.push(m) };
const view = { focusChapter: 'h1', log: [], errors: { trim: 'x' }, flushSheet() { this.log.push('flush'); return Promise.resolve(true); }, applyStylesheet(d) { this.log.push(['sheet', d.stylesheet.trim]); }, applyPreview(p, o) { this.log.push(['preview', p.status, o.quiet]); }, requestRelayout(c) { this.log.push(['relayout', c]); }, pollNow() { this.log.push('poll'); }, afterSheetSaved(b, d) { this.log.push(['after', d.applied.name]); } };
global.Alpine = { store: () => ({ view }) };
__SOURCE__
(async () => {
  const c = window.NassakhOrg.bookTemplates({ list: '/list/', apply: '/apply/__tid__/', update: '/update/__tid__/' });
  await c.load();
  c.select(7);
  c.askApply();
  const asked = c.confirming;
  await c.apply();
  console.log(JSON.stringify({ meta: c.headMeta, asked, closed: !c.confirming, log: view.log, errors: view.errors, calls, toasts }));
})();
"""  # noqa: E501


def test_the_component_applies_through_the_book_page(tmp_path):
    """«تطبيق القالب»: the panel's waiting changes saved first, then the apply, its answer taken as a
    stylesheet save's (values, preview, the chapter laid out again, the poll, the cover), the list again."""
    if NODE is None:
        pytest.skip("node is not installed")
    source = (ROOT / "static" / "src" / "js" / "org.js").read_text(encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(COMPONENT_HARNESS.replace("__SOURCE__", source), encoding="utf-8")
    run = subprocess.run([NODE, str(harness)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-4000:]
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["meta"] == "1" and out["asked"] is True and out["closed"] is True and out["errors"] == {}
    assert out["log"] == [
        "flush",
        ["sheet", "a5"],
        ["preview", "queued", True],
        ["relayout", "h1"],
        "poll",
        ["after", "A5"],
    ]
    assert out["calls"][1] == ["/apply/7/", "POST", {"chapter": "h1"}] and out["calls"][2][0] == "/list/"
    assert out["toasts"] == ["طُبّق القالب «A5» (لم يُطبَّق: خط المتن)."]


# ====================================================================== the organisation of books and users


def test_the_migration_gives_every_book_and_user_the_first_organisation():
    migration = importlib.import_module("accounts.migrations.0003_default_organization")
    root = User.objects.create_superuser("root", password="pass-1234")
    boss = user("boss", "admin")
    plain = user("plain", "editor")
    book = Book.objects.create(title="قديم")
    Organization.objects.all().delete()  # before D98 there was none (conftest gave the book one)
    migration.join(django_apps, None)
    organization = Organization.objects.get()
    book.refresh_from_db()
    assert book.organization == organization and organization.name == "المؤسسة"
    roles = dict(Membership.objects.values_list("user__username", "role"))
    assert roles == {"root": "admin", "boss": "admin", "plain": "member"}
    assert root.membership.organization == organization and plain.membership.role == "member"
    migration.join(django_apps, None)  # twice: nothing more
    assert Organization.objects.count() == 1 and Membership.objects.count() == 3
    assert boss.membership.role == "admin"


def test_a_new_book_belongs_to_its_creators_organisation(org, editor):
    from django.core.files.uploadedfile import SimpleUploadedFile

    import pymupdf

    from books.services import create_book

    pdf = pymupdf.open()
    pdf.new_page()
    data = pdf.tobytes()
    book = create_book({"title": "جديد"}, SimpleUploadedFile("a.pdf", data, "application/pdf"), editor)
    assert book.organization == org
    second = Organization.objects.create(name="ثانية")
    stranger = User.objects.create_user("nobody", password="pass-1234")
    other = create_book({"title": "آخر"}, SimpleUploadedFile("b.pdf", data, "application/pdf"), stranger)
    assert other.organization == org and second.pk  # no membership among several: the first organisation
