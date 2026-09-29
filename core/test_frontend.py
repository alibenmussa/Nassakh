"""Frontend regression tests: rendered templates (Django test client) and the Alpine components
(static/src/js, run under Node with a tiny Alpine/DOM stub). Phase 2 review, frontend findings."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from django.contrib.auth.models import Group, User
from django.template import Context, Template
from django.template.loader import render_to_string
from django.urls import NoReverseMatch, reverse

import pytest

from books.models import Book, Page

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "src" / "js"


def _user(name: str, group: str) -> User:
    user = User.objects.create_user(name, password="pass-1234")
    user.groups.add(Group.objects.get_or_create(name=group)[0])
    return user


@pytest.fixture
def editor_client(client):
    client.force_login(_user("editor", "editor"))
    return client


def _book(n: int = 3, status: str = Book.Status.OCR, page_status: str | None = None, **page_kwargs):
    book = Book.objects.create(title="كتاب " + "طويل العنوان " * 6, status=status)
    if page_status:
        page_kwargs["status"] = page_status
    pages = [
        Page.objects.create(book=book, number=i, source_index=i - 1, **page_kwargs) for i in range(1, n + 1)
    ]
    return book, pages


def _json_script(body: str, element_id: str):
    match = re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>', body, re.S)
    assert match, element_id
    return json.loads(match.group(1))


# ---------------------------------------------------------------- error pages (F31)


def test_403_and_404_render_arabic_inside_the_app_shell(client):
    client.force_login(_user("reader", "proofreader"))
    response = client.get(reverse("books:create"))
    assert response.status_code == 403
    body = response.content.decode()
    assert 'lang="ar" dir="rtl"' in body and "لا تملك صلاحية فتح هذه الصفحة" in body
    assert 'class="sidebar' in body and reverse("books:list") in body and "Forbidden" not in body

    response = client.get(reverse("books:detail", args=[999_999]))
    assert response.status_code == 404
    body = response.content.decode()
    assert "لم نجد هذه الصفحة" in body and 'class="sidebar' in body and "Not Found" not in body


def test_404_for_anonymous_users_and_500_without_request_context(client):
    body = client.get("/no-such-path/").content.decode()
    assert "لم نجد هذه الصفحة" in body and "تسجيل الخروج" not in body
    html = render_to_string("500.html")  # server_error renders without a request
    assert "حدث خطأ غير متوقّع في الخادم" in html and 'dir="rtl"' in html


# ---------------------------------------------------------------- page detail (F40 F43 F44 F49, goal 5)


# ---------------------------------------------------------------- dashboard (F33 F34 F39 F51, goals 1/3/4)


def test_dashboard_adds_live_tiles_counts_pages_plainly_and_offers_book_copy(editor_client):
    book, _ = _book(status=Book.Status.PROCESSING)
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    assert (
        body.count('<div class="page-tile') == 4
    )  # server first paint: three static tiles + the clone template
    assert (
        '<template id="tile-shell">' in body and '<template id="sheet-shell">' in body
    )  # pages added by the poll
    assert "page-tile-clean" in body and "sheet-img-clean" in body  # crossfade layers
    assert "تُحدَّث اللوحة كل ثانيتين" not in body and "spinner" not in body
    assert (
        '<bdi class="num" x-text="count(\'ocr_done\')">0</bdi>' in body
        and 'x-text="counts.all">3</bdi>' in body
    )
    assert "قيد المعالجة" in body and "d.pollState === 'error'" in body
    try:
        url = reverse("api:book_text", args=[book.pk])
    except NoReverseMatch:
        pytest.skip("api:book_text is not wired yet (backend); the copy action hides itself until it is")
    assert "نسخ نص الكتاب" in body and f"bookTextUrl: '{url}'" in body


def test_dashboard_before_ingest_keeps_the_grid_ready_for_the_first_pages(editor_client):
    book = Book.objects.create(title="كتاب", status=Book.Status.PROCESSING)
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    assert "لم تُستخرج الصفحات بعد" in body and 'x-show="nPages === 0"' in body
    assert (
        '<template id="tile-shell">' in body and '<template id="sheet-shell">' in body
    )  # the shells exist so ingested pages appear live


def test_empty_books_list_has_a_single_primary_button(editor_client):
    body = editor_client.get(reverse("books:list")).content.decode()
    assert body.count("btn btn-primary") == 1 and "لا كتب بعد" in body


# ---------------------------------------------------------------- tags and CSS (F44 F47 F45)


def test_stage_label_and_optional_url_tags():
    html = Template(
        "{% load nassakh %}{{ 'ocr_full'|stage_label }}|{{ 'odd'|stage_label }}|"
        "{% optional_url 'books:list' %}|{% optional_url 'api:missing_route' 1 %}"
    ).render(Context())
    assert html == f"التعرّف الكامل (Qari)|odd|{reverse('books:list')}|"


def test_compiled_css_has_focus_ring_underline_and_crossfade():
    css = (ROOT / "static" / "dist" / "app.css").read_text(encoding="utf-8")
    assert re.search(r"\.segmented>label:has\(input:focus-visible\)\{outline:2px", css)
    assert re.search(r"\.tok-low\{[^}]*border-bottom:1\.5px solid var\(--color-warning\)", css)
    assert ".tok+.tok" not in css
    assert re.search(r"\.page-tile-clean\{[^}]*transition:opacity \.2s", css)


# ------------------------------------------------ Alpine components under Node (F15 F42, goal 3)

HARNESS = r"""
const reg = {}; const inits = []; const timers = [];
globalThis.window = globalThis;
globalThis.isSecureContext = false;
globalThis.document = { hidden: false, addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); },
  getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: () => null };
globalThis.CustomEvent = class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } };
globalThis.dispatchEvent = () => true;
globalThis.setTimeout = (fn, ms) => { timers.push(ms); return timers.length; };
globalThis.clearTimeout = () => {};
const fs = require('fs');
for (const f of process.argv.slice(2)) eval(fs.readFileSync(f, 'utf8'));
inits.forEach((fn) => fn());
(async () => {
  const out = {};
  out.plain = Nassakh.plainText('  قال   الأمير في سنة ١٩٦٦ \r\n\n\n\n وَفِي ۱۲\t ');
  console.log(JSON.stringify(out));
})();
"""  # noqa: E501


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_plain_text_under_node(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    files = [str(JS / name) for name in ("ui.js", "processing.js")]
    run = subprocess.run(["node", str(harness), *files], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["plain"] == "قال الأمير في سنة 1966\n\nوَفِي 12"  # Western digits, diacritics kept


# ------------------------------------------------------------ PHASE7 §3.13–§3.14: the upload form, the CSS


def _between(body: str, start: str, end: str) -> str:
    i = body.index(start)
    return body[i : body.index(end, i)]


CSS = ROOT / "static" / "dist" / "app.css"


def test_upload_form_extracts_and_names_the_kept_range(editor_client):
    body = editor_client.get(reverse("books:create")).content.decode()
    assert '<span x-show="!submitting">استخراج الصفحات</span>' in body and "إنشاء الكتاب" not in body
    assert "تُستخرج الصفحات فور الرفع وتُكتشف مناطقها، ثم تنتظر «بدء المعالجة»." in body
    picker = _between(body, 'class="file-pick"', '<p class="help"')
    assert 'type="file" name="source_pdf"' in picker and 'accept="application/pdf,.pdf"' in picker
    assert "<span>اختيار ملف PDF</span>" in picker and "x-text=\"fileName || 'لم يُختر ملف'\"" in picker
    assert '@change="onFile($event)"' in picker
    assert '@input="readSkips($el)"' in body and 'x-text="range.text" data-range-line' in body
    assert "يمكن استثناء صفحات بعينها لاحقًا<" in body


def test_compiled_css_has_the_guides_mode():
    css = CSS.read_text(encoding="utf-8")
    assert re.search(
        r"\.bk-dashboard\.is-guides \.bk-viewer \.gd-page"
        r"\{width:min\(calc\(100cqh \* var\(--ar-n,\s*\.7\)\),\s*100cqw\)\}",
        css,
    )
    for cls in (
        "gd-page",
        "gd-band",
        "gd-chip",
        "gd-grip",
        "gd-menu",
        "gd-preview",
        "gd-legend",
        "gd-bands",
        "gd-b",
        "gd-savebar",
        "gd-lock",
        "guide-line",
        "guide-handle",
        "tile-mark-doubt",
        "file-pick",
        "range-line",
    ):
        assert re.search(rf"\.{cls}[{{.:\[ ,]", css), cls
    assert re.search(r"\.guide-line:before\{[^}]*height:20px", css)  # the 20 px hit area
    assert ".guides-layout" not in css  # the guides screen is gone
    reduced = css[css.rindex("prefers-reduced-motion:reduce") :]
    assert ".gd-band" in reduced or ".gd-band" in css[css.index("prefers-reduced-motion:reduce") :]


def test_the_guides_screen_template_is_gone():
    assert not (ROOT / "templates" / "processing" / "guides.html").exists()
