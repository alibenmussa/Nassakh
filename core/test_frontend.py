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


def test_page_detail_error_state_names_the_stage_in_arabic_and_folds_the_detail(editor_client):
    book, pages = _book(
        page_status=Page.Status.ERROR,
        error_from="ocr_full",
        error_message="تعذّر التعرّف على النص بنماذج OCR.\nRuntimeError: MPS out of memory",
    )
    body = editor_client.get(reverse("books:page_detail", args=[book.pk, 2])).content.decode()
    assert "المرحلة: ocr_full" not in body and "التعرّف الكامل (Qari)" in body
    assert '<summary class="meta">التفاصيل</summary>' in body and "التفاصيل التقنية" not in body
    assert 'name="stage" value="ocr_full"' in body
    assert reverse("books:rerun", args=[book.pk, 2]) in body
    # live state card: the status payload is embedded and the text panel poll is listened to
    state = _json_script(body, "page-state")
    assert state["status"] == "error" and state["error_from"] == "ocr_full"
    assert "@nassakh:page-state.window" in body
    assert _json_script(body, "page-stage-labels")["ocr_full"] == "التعرّف الكامل (Qari)"
    # the page number comes before the (truncating) title; the pager counter is isolated LTR
    title = re.search(r'<h1 class="page-title">(.*?)</h1>', body, re.S).group(1)
    assert title.index("title-page") < title.index("title-main") and "صفحة 2" in title
    assert re.search(r'class="meta num pager-count" dir="ltr"[^>]*>2 / 3<', body)


def test_text_panel_error_banner_has_headline_details_and_retry(editor_client):
    book, _ = _book(page_status=Page.Status.ERROR, error_from="ocr_fast", error_message="تعذّر.\nboom")
    body = editor_client.get(reverse("books:page_detail", args=[book.pk, 1])).content.decode()
    panel = body[body.index('class="card text-panel"') :]
    assert "errorFrom: 'ocr_fast'" in panel
    assert 'x-text="errorHeadline"' in panel and 'x-text="errorDetail"' in panel
    assert ':value="errorFrom"' in panel and 'x-text="errorStageLabel"' in panel
    assert _json_script(body, "text-panel-stages")["ocr_fast"] == "التعرّف السريع (Tesseract)"


def test_text_panel_states_crossfade_underline_and_copy():
    book, pages = _book(
        n=1, page_status=Page.Status.LAYOUT_DONE, text_state="provisional", provisional_text="نص"
    )
    html = render_to_string("ocr/_text_panel.html", {"page": pages[0], "book": book, "role": "editor"})
    assert "bg-highlight" not in html and "'tok-low'" in html  # thin amber underline, no yellow fill
    assert 'class="text-stage"' in html and html.count("x-transition.opacity.duration.200ms") >= 3
    assert "نسخ النص" in html and '@click="copy()"' in html
    # tokens are followed by a real space so selecting a line copies separate words
    assert "i < line.tokens.length - 1 ? ' ' : ''" in html
    assert "spinner" not in html and "خلال ثوانٍ" not in html


# ---------------------------------------------------------------- dashboard (F33 F34 F39 F51, goals 1/3/4)


def test_dashboard_adds_live_tiles_counts_pages_plainly_and_offers_book_copy(editor_client):
    book, _ = _book(status=Book.Status.PROCESSING)
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    assert body.count('<div class="page-tile') == 3  # server first paint
    assert 'x-for="p in laterPages"' in body and "data-live-tile" in body  # tiles added by the poll
    assert "page-tile-clean" in body and "is-ready" in body  # crossfade layer
    assert "تُحدَّث اللوحة كل ثانيتين" not in body and "spinner" not in body
    assert '<bdi x-text="count(\'ocr_done\')">0</bdi> من <bdi x-text="total">3</bdi>' in body
    assert "قيد المعالجة · " in body and 'x-show="fetchFailed"' in body
    try:
        url = reverse("api:book_text", args=[book.pk])
    except NoReverseMatch:
        pytest.skip("api:book_text is not wired yet (backend); the copy action hides itself until it is")
    assert "نسخ نص الكتاب" in body and f'data-url="{url}"' in body


def test_dashboard_before_ingest_keeps_the_grid_ready_for_the_first_pages(editor_client):
    book = Book.objects.create(title="كتاب", status=Book.Status.PROCESSING)
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    assert "لم تُستخرج الصفحات بعد" in body and 'x-show="!hasPages"' in body
    assert 'x-for="p in laterPages"' in body  # the grid exists (cloaked) so ingested pages appear live


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
  // F15: an untouched panel sends no overrides; a changed k sends only k
  const auto = { angle: 1.2, crop_box: [93, 113, 800, 1017], sauvola_k: 0.2, sauvola_window: 41, nlm_h: 6 };
  const panel = reg.preprocessPanel({ has_preprocess: true, params: { ...auto }, auto_params: auto, frame: { width: 900, height: 1100 } });
  panel.load(panel.state);
  out.untouched = panel.manualOverrides();
  panel.k = 0.3;
  out.onlyK = panel.manualOverrides();
  panel.fullCrop();
  out.crop = panel.manualOverrides().crop_box;
  // F42: a 403 stops the text panel for good; a slow request never overlaps the next poll
  const tp = reg.textPanel({ statusUrl: '/s', textUrl: '/t', runsUrl: '/r' });
  tp.active = true; tp.status = 'layout_done';
  let release;
  globalThis.fetch = () => new Promise((resolve) => { release = resolve; });
  tp.start();
  const scheduledAtStart = timers.length;
  const pending = tp.poll();
  out.noOverlap = timers.length === scheduledAtStart;
  release({ status: 403, ok: false, json: async () => ({}) });
  await pending;
  out.stopped = { polling: tp.polling, authLost: tp.authLost, timers: timers.length - scheduledAtStart };
  // goal 3: final text is copied when present, else the provisional text
  tp.textState = 'provisional'; tp.provisional = 'مبدئي';
  out.copyProvisional = tp.copyable;
  tp.textState = 'final'; tp.finalText = 'نهائي';
  out.copyFinal = tp.copyable;
  // the status API splits the failure into `error` (headline) and `error_detail`
  tp.apply({ error: 'تعذّر.', error_detail: 'RuntimeError: boom' });
  out.errorSplit = [tp.errorHeadline, tp.errorDetail];
  console.log(JSON.stringify(out));
})();
"""  # noqa: E501


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_alpine_components_copy_overrides_and_polling(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    files = [str(JS / name) for name in ("ui.js", "processing.js", "ocr.js")]
    run = subprocess.run(["node", str(harness), *files], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["plain"] == "قال الأمير في سنة 1966\n\nوَفِي 12"  # Western digits, diacritics kept
    assert out["untouched"] == {} and out["onlyK"] == {"sauvola_k": 0.3}
    assert out["crop"] == [0, 0, 900, 1100]
    assert out["noOverlap"] is True
    assert out["stopped"] == {"polling": False, "authLost": True, "timers": 0}
    assert out["copyProvisional"] == "مبدئي" and out["copyFinal"] == "نهائي"
    assert out["errorSplit"] == ["تعذّر.", "RuntimeError: boom"]
