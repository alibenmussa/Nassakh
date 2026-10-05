"""Tests for the shared core helpers: Arabic text, storage, roles, media view, landing page, template tags."""

import re
from types import SimpleNamespace

from django.contrib.auth.models import AnonymousUser, Group, User
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse

import numpy as np
import pytest

from books.models import ALL_PAGES_FAILED, Book, Page
from core import arabic, images, storage
from core.context_processors import nav
from core.decorators import has_role, role_required, user_role
from core.templatetags.nassakh import ar_flag, ar_status, percent, status_dot
from processing.models import LayoutGuides, Preprocess

# ---------------------------------------------------------------- arabic


def test_to_western_digits_converts_arabic_indic_and_persian():
    assert arabic.to_western_digits("سنة ١٩٦٦ و ۲۰۲۴") == "سنة 1966 و 2024"
    assert arabic.to_western_digits("no digits") == "no digits"


def test_strip_tashkeel_removes_diacritics_and_tatweel_only():
    assert arabic.strip_tashkeel("مُحَمَّدٌ رَسُولُ اللهِ") == "محمد رسول الله"
    assert arabic.strip_tashkeel("الكتــــاب") == "الكتاب"
    # letters, digits and punctuation are untouched
    assert arabic.strip_tashkeel("كتاب، ١٢٣.") == "كتاب، ١٢٣."


def test_fold_letters_normalises_variants():
    assert arabic.fold_letters("أإآٱ ى ة ؤ ئ") == "اااا ي ه و ي"


def test_normalize_ws_collapses_spaces_and_keeps_lines():
    text = "  سطر  أول ‏\n\n\tسطر ثانٍ  \r\n"
    assert arabic.normalize_ws(text) == "سطر أول\nسطر ثانٍ"


def test_normalize_levels():
    text = "قَالَ الأَمِيرُ: «سَنَةَ ١٩٦٦»"
    assert arabic.normalize(text, "raw") == "قَالَ الأَمِيرُ: «سَنَةَ ١٩٦٦»"
    assert arabic.normalize(text, "no_tashkeel") == "قال الأمير: «سنة ١٩٦٦»"
    assert arabic.normalize(text, "lenient") == "قال الامير سنه 1966"
    with pytest.raises(ValueError):
        arabic.normalize(text, "bogus")


def test_strip_markup_turns_html_and_markdown_into_lines():
    raw = (
        "<h1># العنوان</h1><p>فقرة <b>أولى</b> &amp; ثانية</p>line<br/>break\n"
        "**bold** `code` | cell |\n---\n- bullet"
    )
    assert arabic.strip_markup(raw) == "العنوان\nفقرة أولى & ثانية\nline\nbreak\nbold code cell\nbullet"


def test_strip_markup_table_cells_join_with_spaces():
    raw = "<table><tr><td>أ</td><td>ب</td></tr><tr><td>ج</td><td>د</td></tr></table>"
    assert arabic.strip_markup(raw) == "أ ب\nج د"


def test_truncate_repetition_cuts_verbatim_loop_to_one_copy():
    unit = "هذا سطر يتكرر بلا نهاية. "
    text = "مقدمة سليمة. " + unit * 6
    cut, looped = arabic.truncate_repetition(text)
    assert looped is True
    assert cut == ("مقدمة سليمة. " + unit).rstrip()


def test_truncate_repetition_leaves_normal_text_alone():
    text = "نص عادي فيه بعض التكرار البسيط مثل: نعم نعم نعم."
    cut, looped = arabic.truncate_repetition(text)
    assert (cut, looped) == (text, False)


def test_truncate_repetition_with_hit_cap_masks_digits_and_keeps_five_copies():
    lines = [f"({n}) مرجع الحاشية رقم {n} في الكتاب.\n" for n in range(100, 112)]
    text = "متن الصفحة.\n" + "".join(lines)
    cut, looped = arabic.truncate_repetition(text, hit_cap=True)
    assert looped is True
    assert cut.count("مرجع الحاشية") == 5
    # without the cap the incrementing loop is not treated as a repetition
    cut2, looped2 = arabic.truncate_repetition(text, hit_cap=False)
    assert looped2 is False and cut2 == text.rstrip()


def test_parse_output_combines_truncation_and_markup_stripping():
    raw = "<p>نص</p>" + "<p>تكرار طويل بما يكفي</p>" * 5
    text, looped = arabic.parse_output(raw)
    assert looped is True
    assert text == "نص\nتكرار طويل بما يكفي"


@pytest.mark.parametrize(
    "tok,expected",
    [
        ("1966", True),
        ("(٣)", True),
        ("[۲]", True),
        ("١٢/٣", True),
        ("12.5", True),
        ("ص٣", False),
        ("كتاب", False),
        ("", False),
        ("...", False),
    ],
)
def test_is_digit_token(tok, expected):
    assert arabic.is_digit_token(tok) is expected


def test_arabic_ratio():
    assert arabic.arabic_ratio("كتاب عربي") == 1.0
    assert arabic.arabic_ratio("Latin only 123") == 0.0
    assert arabic.arabic_ratio("١٢٣ ...") == 0.0  # digits and punctuation are not letters
    assert 0.4 < arabic.arabic_ratio("كتاب book") < 0.6


# ---------------------------------------------------------------- images


def test_smart_resize_respects_bounds_and_factor():
    h, w = images.smart_resize(3000, 2000, 28, 256 * 28 * 28, 2048 * 28 * 28)
    assert h % 28 == 0 and w % 28 == 0
    assert h * w <= 2048 * 28 * 28
    h2, w2 = images.smart_resize(50, 50, 28, 256 * 28 * 28, 2048 * 28 * 28)
    assert h2 * w2 >= 256 * 28 * 28


def test_crop_clamps_and_rounds():
    arr = np.arange(100, dtype=np.uint8).reshape(10, 10)
    out = images.crop(arr, [2.4, 3.6, 50, -1])
    assert out.shape == (4, 8)  # y 0..4, x 2..10
    assert out[0, 0] == arr[0, 2]


def test_to_webp_bytes_downscales_to_max_width():
    arr = np.full((100, 400), 200, dtype=np.uint8)
    data = images.to_webp_bytes(arr, max_width=200)
    assert data[:4] == b"RIFF"
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(data))
    assert img.size == (200, 50)


# ---------------------------------------------------------------- storage paths


def test_storage_paths_follow_the_media_layout():
    book = SimpleNamespace(pk=7)
    page = SimpleNamespace(book_id=7, number=3)
    preprocess = SimpleNamespace(page=page)
    assert storage.book_source_path(book, "whatever.PDF") == "books/7/source.pdf"
    assert storage.page_original_path(page, "x.png") == "books/7/pages/0003/original.png"
    assert storage.page_derived_path(preprocess, "gray.png") == "books/7/pages/0003/gray.png"
    assert storage.page_derived_path(preprocess, "thumb.webp") == "books/7/pages/0003/thumb.webp"


def test_book_source_path_requires_a_saved_book():
    with pytest.raises(ValueError):
        storage.book_source_path(SimpleNamespace(pk=None), "a.pdf")


@pytest.mark.django_db
def test_save_array_writes_a_stable_path_and_replaces_in_place():
    book = Book.objects.create(title="كتاب")
    page = Page.objects.create(book=book, number=1, source_index=0)
    pre = Preprocess.objects.create(page=page)

    name = storage.save_array(pre.gray_image, np.full((20, 30), 128, dtype=np.uint8), "gray.png")
    assert name == f"books/{book.pk}/pages/0001/gray.png"
    assert default_storage.exists(name)
    pre.save()

    # second write: same path, new content, no random suffix
    name2 = storage.save_array(pre.gray_image, np.zeros((10, 10), dtype=np.uint8), "gray.png")
    assert name2 == name
    with default_storage.open(name, "rb") as fh:
        assert images.load_gray(fh).shape == (10, 10)

    webp = storage.save_array(pre.thumbnail, np.zeros((10, 10), dtype=np.uint8), "thumb.webp")
    assert webp.endswith("/thumb.webp")


# ---------------------------------------------------------------- roles


@pytest.fixture
def users(db):
    editor = User.objects.create_user("editor", password="x")
    editor.groups.add(Group.objects.get(name="editor"))
    admin = User.objects.create_user("admin", password="x")
    admin.groups.add(Group.objects.get(name="admin"))
    plain = User.objects.create_user("plain", password="x")
    superuser = User.objects.create_superuser("root", password="x")
    return SimpleNamespace(editor=editor, admin=admin, plain=plain, superuser=superuser)


@role_required("editor")
def _editor_view(request):
    return HttpResponse("ok")


def test_role_required_redirects_anonymous_users(users):
    request = RequestFactory().get("/books/")
    request.user = AnonymousUser()
    response = _editor_view(request)
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("accounts:login"))


def test_role_required_denies_users_without_the_role(users):
    request = RequestFactory().get("/books/")
    request.user = users.plain
    with pytest.raises(PermissionDenied):
        _editor_view(request)


@pytest.mark.parametrize("who", ["editor", "admin", "superuser"])
def test_role_required_allows_role_admin_and_superuser(users, who):
    request = RequestFactory().get("/books/")
    request.user = getattr(users, who)
    assert _editor_view(request).status_code == 200


def test_user_role_and_has_role(users):
    assert user_role(users.editor) == "editor"
    assert user_role(users.admin) == "admin"
    assert user_role(users.superuser) == "admin"
    assert user_role(users.plain) is None
    assert user_role(AnonymousUser()) is None
    assert has_role(users.editor, "editor", "proofreader")
    assert not has_role(users.editor, "proofreader")
    assert has_role(users.admin, "proofreader")
    assert not has_role(AnonymousUser(), "editor")


def test_nav_context_processor(users):
    request = RequestFactory().get("/")
    request.user = users.admin
    # no organisation yet: the sidebar shows no «المؤسسة» (D98: accounts/test_fonts.py)
    expected = {"is_admin": True, "role": "admin", "role_label": "مدير", "nav_organization": None}
    assert nav(request) == {**expected, "nav_quota": None}  # D106: no organisation, no «الرصيد»
    request.user = users.editor
    assert nav(request)["is_admin"] is False
    request.user = AnonymousUser()
    assert nav(request) == {
        "is_admin": False,
        "role": None,
        "role_label": "",
        "nav_organization": None,
        "nav_quota": None,
    }


# ---------------------------------------------------------------- protected media


@pytest.mark.django_db
def test_protected_media_requires_login_and_serves_files(client, users):
    from accounts.testing import member

    book = Book.objects.create(title="كتاب")
    name = default_storage.save(f"books/{book.pk}/pages/0001/original.png", ContentFile(b"\x89PNG fake"))
    url = reverse("media", kwargs={"path": name})

    anonymous = client.get(url)
    assert anonymous.status_code == 302 and reverse("accounts:login") in anonymous["Location"]

    client.force_login(users.plain)
    assert client.get(url).status_code == 404  # no organisation, no book's files (D102)
    member(users.plain)
    response = client.get(url)
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    assert b"".join(response.streaming_content) == b"\x89PNG fake"
    assert "Last-Modified" in response
    assert response["Cache-Control"] == "private, no-cache"

    # revalidation answers 304 when the file has not changed
    again = client.get(url, HTTP_IF_MODIFIED_SINCE=response["Last-Modified"])
    assert again.status_code == 304

    missing = f"books/{book.pk}/missing.png"
    assert client.get(reverse("media", kwargs={"path": missing})).status_code == 404
    assert client.get("/media/../settings.py").status_code == 404
    assert client.get(reverse("media", kwargs={"path": f"books/{book.pk}/pages"})).status_code == 404


# ---------------------------------------------------------------- the landing page and the home redirect


@pytest.mark.django_db
def test_home_shows_a_visitor_the_landing_page_with_both_ways_in(client):
    response = client.get(reverse("core:home"))
    assert response.status_code == 200
    body = response.content.decode()
    assert 'lang="ar" dir="rtl"' in body
    assert body.count("<h1") == 1 and "مربوطٍ بصفحاته" in body
    assert f'href="{reverse("accounts:signup")}"' in body and f'href="{reverse("accounts:login")}"' in body
    assert "إنشاء حساب" in body and "تسجيل الدخول" in body
    # the shell of a signed-in page is not there
    assert 'class="sidebar' not in body and "تسجيل الخروج" not in body
    # the head: a title, a description, Open Graph
    assert "<title>نسّاخ · من الكتاب المطبوع إلى نص مراجَع</title>" in body
    assert '<meta name="description"' in body
    assert 'property="og:title"' in body and 'property="og:description"' in body


@pytest.mark.django_db
def test_landing_page_tells_both_sides_and_the_four_answers_without_numbers_or_prices(client):
    body = client.get(reverse("core:home")).content.decode()
    assert "للناشر والمحقق" in body and "للباحث ولأي مساعد ذكي" in body
    for answer in ("مطابق", "مختلف", "يحتاج مطابقة مع الصورة", "لم يوجد"):
        assert answer in body
    for client_name in ("Claude", "ChatGPT", "Claude Code", "Cursor", "VS Code"):
        assert client_name in body
    assert "الحقوق" in body and "الملك العام" in body
    # no prices, plans, percentages or claims of accuracy (the owner's rule until production)
    for forbidden in ("دولار", "خطة", "باقة", "دقة", "٪"):
        assert forbidden not in body, forbidden


@pytest.mark.django_db
def test_landing_page_requests_nothing_from_other_hosts(client):
    body = client.get(reverse("core:home")).content.decode()
    assert not re.search(r'(?:href|src)="(?:https?:)?//', body)
    assert "fonts.googleapis" not in body and "cdn." not in body


@pytest.mark.django_db
def test_home_sends_a_signed_in_user_to_the_books_list(client):
    client.force_login(User.objects.create_user("reader", password="x"))
    response = client.get(reverse("core:home"))
    assert response.status_code == 302
    assert response["Location"] == reverse("books:list")


# ---------------------------------------------------------------- template tags


def test_status_dot_maps_statuses_to_colour_classes():
    assert status_dot("uploaded") == "dot-neutral"
    assert status_dot("preprocessed") == "dot-accent"
    assert status_dot("ocr_done") == "dot-success"
    assert status_dot("error") == "dot-danger"
    assert status_dot("needs_guides") == "dot-warning"
    assert status_dot("unknown") == "dot-neutral"


def test_percent_filter():
    assert percent(1, 4) == 25
    assert percent(2, 3) == 67
    assert percent(3, 0) == 0
    assert percent(None, 5) == 0


@pytest.mark.django_db
def test_ar_status_uses_the_display_label():
    book = Book(title="x", status=Book.Status.NEEDS_GUIDES)
    assert ar_status(book) == "تم التخطيط"
    page = Page(status=Page.Status.OCR_DONE)
    assert ar_status(page) == "تم التعرّف"
    assert ar_status(None) == ""
    assert ar_flag("large_skew") == "انحراف كبير"
    assert ar_flag("custom") == "custom"


# ---------------------------------------------------------------- models


@pytest.mark.django_db
def test_book_progress_page_count_and_refresh_status():
    book = Book.objects.create(title="كتاب", status=Book.Status.PROCESSING)
    pages = [Page.objects.create(book=book, number=n, source_index=n - 1) for n in range(1, 5)]
    pages[3].is_excluded = True
    pages[3].status = Page.Status.EXCLUDED
    pages[3].save()

    assert book.page_count == 3
    progress = book.progress()
    assert progress["uploaded"] == 3 and sum(progress.values()) == 3

    # nothing done yet: stays processing
    assert book.refresh_status() == Book.Status.PROCESSING

    Page.objects.filter(pk__in=[p.pk for p in pages[:3]]).update(status=Page.Status.PREPROCESSED)
    book.status = Book.Status.NEEDS_GUIDES
    assert book.refresh_status() == Book.Status.NEEDS_GUIDES  # waiting for guides is kept

    # one page moved on through a per-page action: the others still wait for the guides (F6)
    Page.objects.filter(pk=pages[0].pk).update(status=Page.Status.LAYOUT_DONE)
    assert book.refresh_status() == Book.Status.NEEDS_GUIDES
    LayoutGuides.objects.create(book=book, source=LayoutGuides.Source.MANUAL)
    assert book.refresh_status() == Book.Status.OCR
    assert Book.objects.get(pk=book.pk).status == Book.Status.OCR

    Page.objects.filter(pk__in=[p.pk for p in pages[:3]]).update(status=Page.Status.OCR_DONE)
    assert book.refresh_status() == Book.Status.READY_FOR_REVIEW

    # an errored page with nothing else left to run settles the book (it never stays active, F7)
    Page.objects.filter(pk=pages[1].pk).update(status=Page.Status.ERROR)
    assert book.refresh_status() == Book.Status.READY_FOR_REVIEW
    Page.objects.filter(pk=pages[2].pk).update(status=Page.Status.LAYOUT_DONE)
    assert book.refresh_status() == Book.Status.OCR  # work pending: still in OCR

    # every page failed: `error` with a re-derivable message, left again once a page recovers
    Page.objects.filter(pk__in=[p.pk for p in pages[:3]]).update(status=Page.Status.ERROR)
    assert book.refresh_status() == Book.Status.ERROR
    assert Book.objects.get(pk=book.pk).error_message == ALL_PAGES_FAILED
    Page.objects.filter(pk=pages[0].pk).update(status=Page.Status.OCR_DONE)
    assert book.refresh_status() == Book.Status.READY_FOR_REVIEW and book.error_message == ""

    Page.objects.filter(pk=pages[1].pk).update(status=Page.Status.REVIEWED)
    assert book.refresh_status() == Book.Status.REVIEWING

    Page.objects.filter(book=book, is_excluded=False).update(status=Page.Status.ASSEMBLED)
    assert book.refresh_status() == Book.Status.ASSEMBLED

    book.status = Book.Status.ERROR
    assert book.refresh_status() == Book.Status.ERROR  # ingest errors are never overridden


@pytest.mark.django_db
def test_page_set_error_and_clear_error_fall_back_to_completed_stage():
    book = Book.objects.create(title="كتاب")
    page = Page.objects.create(book=book, number=1, source_index=0)

    page.set_error("preprocess", "تعذّر قراءة الصورة.")
    page.refresh_from_db()
    assert page.status == Page.Status.ERROR
    assert page.error_from == "preprocess"
    assert page.error_message == "تعذّر قراءة الصورة."

    page.clear_error()
    page.refresh_from_db()
    assert (page.status, page.error_from, page.error_message) == (Page.Status.UPLOADED, "", "")

    Preprocess.objects.create(page=page)
    page.set_error("layout", "x")
    page.clear_error()
    assert page.status == Page.Status.PREPROCESSED

    page.regions.create(kind="body", bbox=[0, 0, 10, 10], order=0)
    page.set_error("ocr", "x")
    page.clear_error()
    assert page.status == Page.Status.LAYOUT_DONE

    page.text_state = Page.TextState.FINAL
    page.set_error("ocr", "x")
    page.clear_error()
    assert page.status == Page.Status.OCR_DONE


# ---------------------------------------------------------------- base layout


def _urlconf_with_books_list() -> str:
    """The real URLconf plus a stub `books:list`, which the books agent adds later."""
    import sys
    import types

    from django.urls import include, path

    import nassakh.urls

    stub = [path("", lambda request: HttpResponse("list"), name="list")]
    module = types.ModuleType("core._test_urlconf")
    # The stub goes first: Django populates namespaces from the last pattern backwards, so the
    # first include with a given instance namespace wins over the (still empty) real one.
    module.urlpatterns = [path("books/", include((stub, "books")))] + nassakh.urls.urlpatterns
    sys.modules[module.__name__] = module
    return module.__name__


PAGE_TEMPLATE = """
{% extends "base.html" %}
{% load nassakh %}
{% block title %}عنوان الصفحة{% endblock %}
{% block header_actions %}<a class="btn btn-primary" href="#">كتاب جديد</a>{% endblock %}
{% block content %}
  {% include "partials/_status_dot.html" with status="ocr_done" label="تم التعرّف" %}
  {% progress_bar value=3 total=4 %}
  {% include "partials/_progress.html" with percent=0 thin=True state="success" %}
  {% include "partials/_empty_state.html" with title="لا كتب بعد" action_url="/b/new/" action_label="جديد" %}
  {% include "partials/_empty_state.html" with title="فارغ" text="ابدأ برفع ملف PDF." icon="i-book" %}
{% endblock %}
"""


@pytest.mark.django_db
def test_base_layout_renders_shell_nav_and_partials(users):
    from django.contrib.messages import constants, storage
    from django.template import engines
    from django.test import override_settings

    request = RequestFactory().get("/books/")
    request.user = users.admin
    from django.contrib.sessions.backends.db import SessionStore

    request.session = SessionStore()
    request._messages = storage.default_storage(request)
    request._messages.add(constants.SUCCESS, "تم الحفظ")
    request._messages.add(constants.ERROR, "حدث خطأ")

    with override_settings(ROOT_URLCONF=_urlconf_with_books_list()):
        from django.urls import clear_url_caches, set_urlconf

        clear_url_caches()
        set_urlconf(None)
        from django.urls import resolve

        request.resolver_match = resolve("/books/")
        html = engines["django"].from_string(PAGE_TEMPLATE).render({}, request=request)
        clear_url_caches()

    assert '<html lang="ar" dir="rtl">' in html
    assert "<title>نسّاخ</title>" in html
    assert 'class="page-title">عنوان الصفحة</h1>' in html
    assert "كتاب جديد" in html
    assert 'href="/books/"' in html and "is-active" in html  # الكتب is the active item
    assert 'href="/admin/"' in html and "المستخدمون" in html  # admin link for admins
    assert 'action="/accounts/logout/"' in html and "تسجيل الخروج" in html
    assert "dist/app.css" in html and "vendor/alpine.min.js" in html
    assert "src/js/books.js" in html and "src/js/processing.js" in html
    # messages: success → toast, error → banner
    assert 'class="toast"' in html and "تم الحفظ" in html
    assert "banner banner-danger" in html and "حدث خطأ" in html
    # partials
    assert 'class="dot dot-success"' in html and "تم التعرّف" in html
    assert 'aria-valuenow="75"' in html and "width: 75%" in html
    assert "progress progress-thin" in html and 'aria-valuenow="0"' in html and "is-success" in html
    assert "لا كتب بعد" in html and 'href="/b/new/"' in html


@pytest.mark.django_db
def test_base_layout_hides_admin_link_for_editors(users):
    from django.template import engines
    from django.test import override_settings

    request = RequestFactory().get("/books/")
    request.user = users.editor
    with override_settings(ROOT_URLCONF=_urlconf_with_books_list()):
        from django.urls import clear_url_caches

        clear_url_caches()
        html = engines["django"].from_string(PAGE_TEMPLATE).render({}, request=request)
        clear_url_caches()
    assert "المستخدمون" not in html
    assert "محرّر" in html  # role label in the sidebar footer


# ---------------------------------------------------------------- Phase 7a


def test_needs_guides_keeps_the_warning_dot_your_turn():
    # «تم التخطيط»: the owner acts next («بدء المعالجة»)
    assert status_dot("needs_guides") == "dot-warning"
    assert status_dot("processing") == "dot-accent" and status_dot("ocr") == "dot-accent"


def test_the_labels_of_the_split_reach_the_templates():
    from books.models import Book, Page

    assert ar_status(Book(title="x", status=Book.Status.PROCESSING)) == "قيد التخطيط"
    assert ar_status(Book(title="x", status=Book.Status.OCR)) == "قيد المعالجة"
    assert ar_status(Page(status=Page.Status.PREPROCESSED)) == "مُجهَّزة"
    assert ar_status(Page(status=Page.Status.LAYOUT_DONE)) == "بانتظار التعرّف"
    from core.templatetags.nassakh import stage_label

    assert stage_label("preprocess") == "تجهيز الصفحات" and stage_label("layout") == "تحديد المناطق"


def test_undo_action_reads_the_undo_url_of_a_message():
    from types import SimpleNamespace

    from core.templatetags.nassakh import undo_action

    assert (
        undo_action(SimpleNamespace(extra_tags="undo:/books/3/pages/12/exclude/"))
        == "/books/3/pages/12/exclude/"
    )
    assert undo_action(SimpleNamespace(extra_tags="toast undo:/books/3/")) == "/books/3/"
    assert undo_action(SimpleNamespace(extra_tags="")) == ""
    assert undo_action(SimpleNamespace(extra_tags="undo://evil.example/")) == ""  # same-site paths only
    assert undo_action(SimpleNamespace(extra_tags="undo:javascript:alert(1)")) == ""
    assert undo_action(None) == ""
