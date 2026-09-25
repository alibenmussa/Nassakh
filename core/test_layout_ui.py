"""Layout page (Phase 5, PHASE5_SPEC §5): the rendered template in every state through the Django test client
(no manuscript, nothing rendered yet, a render queued, a finished render painted on first paint, a failed
render, a missing font, a proofreader), the compiled CSS, and — under Node with a tiny DOM and a fetch stub —
the `bookLayout` Alpine component: the first GET and the polling until the pages arrive, a stylesheet change
→ one debounced PUT → polling → the pages swapped in place with the current page kept (the chapter's own
render standing in for its range first, D44), the footprint delta, the 400 / network paths of the save, the
spread pairing (recto on the left), the fit modes, the keyboard map, the turn motion, wheel and touch, the
stale and error pills with the one automatic request, the filmstrip thumbs, jump and the chapter list."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

import pytest

from books.models import Book
from editor.models import Manuscript, StyleSheet
from publishing import preview
from publishing.models import PreviewRender

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "src" / "js"
CSS = ROOT / "static" / "dist" / "app.css"


# ---------------------------------------------------------------- a book with a manuscript of three chapters


def _text(value):
    return {"type": "text", "text": value}


def _block(kind, block_id, value, page, level=None):
    attrs = {"id": block_id, "sourcePages": [page], "sourceLineIds": [], "reviewed": True}
    if level:
        attrs["level"] = level
    return {"type": kind, "attrs": attrs, "content": [_text(value)]}


def _document():
    return {
        "type": "doc",
        "attrs": {"bookId": 1},
        "content": [
            {"type": "title", "attrs": {"text": "كتاب التنسيق", "author": "المؤلف"}},
            _block("paragraph", "p1", "تمهيد قبل الفصول", 1),
            _block("heading", "h10", "الفصل الأول", 2, level=1),
            _block("paragraph", "p11", "نص الفصل الأول", 2),
            _block("heading", "h20", "الفصل الثاني", 4, level=1),
            _block("paragraph", "p22", "نص الفصل الثاني", 4),
        ],
    }


def _book(title="كتاب التنسيق"):
    book = Book.objects.create(title=title, author="المؤلف", status=Book.Status.REVIEWING)
    Manuscript.objects.create(book=book, document=_document(), version=1)
    return book


def _user(name: str, role: str | None) -> User:
    user = User.objects.create_user(name, password="pass-1234")
    if role:
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


def _logged(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def editor(db):
    return _user("editor", "editor")


@pytest.fixture
def proofreader(db):
    return _user("reader", "proofreader")


def _json_script(body: str, element_id: str):
    match = re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>', body, re.S)
    assert match, element_id
    return json.loads(match.group(1))


def _page(client, book, query="") -> tuple[str, dict]:
    body = client.get(reverse("editor:layout", args=[book.pk]) + query).content.decode()
    return body, _json_script(body, "layout-config")


def _digest(book, scope="book", chapter_id=None) -> str:
    return preview.job_hash(preview.job_for(book, scope, chapter_id))


def _render(book, status="done", pages=3, chapters=None, scope="book", chapter_id=None, error=""):
    """A render row of the book's current content; a finished one gets its page files on disk."""
    digest = _digest(book, scope, chapter_id)
    folder = preview.preview_folder(book.pk, digest)
    if status == "done":
        target = Path(settings.MEDIA_ROOT) / folder
        target.mkdir(parents=True, exist_ok=True)
        for index in range(1, pages + 1):
            for retina in (False, True):
                (target / preview.page_file(index, retina)).write_bytes(b"RIFF")
        (target / ("chapter.pdf" if scope == "chapter" else "book.pdf")).write_bytes(b"%PDF")
    return PreviewRender.objects.create(
        book=book,
        scope=scope,
        chapter_id=chapter_id or "",
        content_hash=digest,
        status=status,
        page_count=pages if status == "done" else 0,
        first_page=1,
        chapters=chapters
        if chapters is not None
        else [
            {"id": "p1", "title": "قبل الفصل الأول", "first": 1, "last": 1},
            {"id": "h10", "title": "الفصل الأول", "first": 2, "last": 2},
            {"id": "h20", "title": "الفصل الثاني", "first": 3, "last": pages},
        ],
        folder=folder if status == "done" else "",
        error=error,
        finished_at=timezone.now() if status in ("done", "error") else None,
    )


# ---------------------------------------------------------------- the template: every state


def test_layout_page_without_a_manuscript_shows_the_empty_state(editor):
    book = Book.objects.create(title="بلا مخطوطة")
    body, config = _page(_logged(editor), book)
    assert "لا توجد مخطوطة بعد" in body and "data-layout" not in body and "lo-bar" not in body
    assert reverse("assembly:manuscript", args=[book.pk]) in body and "src/js/layout.js" in body
    assert config["exists"] is False and config["initial"]["preview"] is None
    assert config["initial"]["stylesheet"]["saved"] is False
    assert Client().get(reverse("editor:layout", args=[book.pk])).status_code == 302


def test_layout_page_first_paint_before_any_stylesheet_or_render(editor):
    book = _book()
    body, config = _page(_logged(editor), book)
    # the config: the page, the chapters, every URL, the embedded stylesheet payload and the cached preview
    assert config["page"] == "layout" and config["canEdit"] is True and config["exists"] is True
    assert [c["id"] for c in config["chapters"]] == ["p1", "h10", "h20"]
    initial = config["initial"]
    assert initial["stylesheet"]["saved"] is False and initial["stylesheet"]["stylesheet"]["trim"] == "17x24"
    assert [t["key"] for t in initial["stylesheet"]["trims"]][-1] == "custom"
    assert initial["preview"]["status"] == "none" and initial["preview"]["pages"] == []
    assert initial["preview"]["stale"] is True and initial["chapterPreview"] is None
    assert not PreviewRender.objects.exists()  # opening the page never queues a render by itself
    assert config["urls"]["preview"] == reverse("api:preview", args=[book.pk])
    # the top bar: the dashboard link, the save pill, the one primary, the «⋯» menu
    assert 'x-data="layoutBar"' in body and "لوحة الكتاب" in body
    assert "data-save-pill" in body and 'x-text="v.savePill.text"' in body
    # changes still waiting for the debounce go out when the page is left; the tab's return polls at once
    assert '@beforeunload.window="onUnload()"' in body and '@pagehide.window="onUnload()"' in body
    assert '@visibilitychange.document="onVisible()"' in body
    assert "data-layout-editor" in body and ':href="v.editorHref"' in body and "فتح المحرّر" in body
    menu = body[
        body.index('class="menu menu-popover lo-menu"') : body.index("</template>", body.index("lo-menu"))
    ]
    assert "المخطوطة" in menu and "فتح PDF المعاينة" in menu and "اختصارات" in menu and "لوحة الكتاب" in menu
    # the toolbar: the spread and fit segmented controls, the quiet pills, the counter, the jump field with G
    assert "data-spread-toggle" in body and "صفحتان" in body and '@click="setSpread(true)"' in body
    assert "data-fit-toggle" in body and "ملء الارتفاع" in body and "ملء العرض" in body and "100 %" in body
    assert "data-render-pill" in body and 'x-text="renderPill.text"' in body
    assert "تعذّر التحديث · إعادة المحاولة" in body and "انتهت الجلسة · تسجيل الدخول" in body
    assert 'class="bk-counter lo-counter"' in body and 'x-text="counterText"' in body
    assert 'inputmode="numeric" dir="ltr" placeholder="إلى صفحة…"' in body and ">G</kbd>" in body
    # the stage: skeleton sheets while nothing is cached, the two sheets, the designed error state, the turns
    assert 'class="lo-stage" x-ref="stage"' in body and '@wheel="onStageWheel($event)"' in body
    assert "data-skeleton" in body and "x-if=\"phase === 'skeleton'\"" in body
    assert 'data-sheet="right"' in body and 'data-sheet="left"' in body
    assert 'x-show="phase === \'pages\'" x-cloak data-sheet="right"' in body  # nothing to paint yet
    assert "data-error-state" in body and "إعادة المحاولة" in body and "لم تُخرَج صفحات." in body
    assert 'class="bk-turn bk-turn-prev lo-turn"' in body and 'aria-label="الصفحة التالية"' in body
    # the side panel: the footprint, the two tabs, the five sections in order, the filmstrip (empty)
    side = body[body.index('<aside class="bk-side lo-side"') :]
    assert "data-footprint" in side and "يُحسب…" in side and "لم تُخرَج الصفحات بعد" in side
    assert "data-chapters" in side and '@click="goToChapter(row.id)"' in side
    assert "data-panel-tabs" in side and "التنسيق" in side and "الصفحات" in side
    heads = [
        m.group(1)
        for m in re.finditer(r'<div class="bk-side-head"><span id="lo-head-\w+">([^<]+)</span>', side)
    ]
    assert heads == ["القطع", "الهوامش", "الخطوط", "النص", "الصفحة"]
    assert "data-trims" in side and "lo-trim-glyph" in side and '@click="setTrim(t.key)"' in side
    assert "data-diagram" in side and ':data-focus="focusMargin"' in side and "is-recto" in side
    for field in (
        "top_mm",
        "bottom_mm",
        "inner_mm",
        "outer_mm",
        "body_size_pt",
        "line_height",
        "indent_em",
        "footnote_size_pt",
        "heading_scale.h1",
        "width_mm",
    ):
        assert f'data-field="{field}"' in side, field
    assert side.count("lo-stepper-box") == 12 and "@click=\"step('top_mm', -1)\"" in side
    for role in ("body", "latin", "heading"):
        assert f'data-font-role="{role}"' in side, role
    assert "lo-font-sample" in side and "غير مثبّت" in side and "data-missing-fonts" in side
    assert (
        side.count('@click="above = fontMenuAbove($el); open = !open"') == 3
    )  # the menus flip up when clipped
    assert (
        'id="lo-page-number"' in side
        and "صفحة المحتويات" in side
        and "أرقام الصفحات الأصلية في الهامش" in side
    )
    assert 'class="lo-film-track" x-ref="film" data-film-track></div>' in side
    assert 'class="lo-panel"' in side and 'class="lo-panel" disabled' not in side  # editors: live controls
    assert "src/js/layout.js" in body


def test_layout_page_for_a_proofreader_is_read_only(proofreader):
    book = _book()
    body, config = _page(_logged(proofreader), book)
    assert config["canEdit"] is False
    assert 'class="lo-panel" disabled data-layout-panel' in body and "التنسيق يغيّره محرّر الكتاب" in body
    assert "data-layout-editor" not in body and "المخطوطة" in body
    assert '@click="retry()"' not in body.split("data-render-pill")[1]  # no retry buttons in the states


def test_layout_page_rendering_state(editor):
    book = _book()
    _render(book, status="queued")
    body, config = _page(_logged(editor), book)
    p = config["initial"]["preview"]
    assert p["status"] == "queued" and p["rendering"] is True and p["pages"] == [] and p["stale"] is True
    assert 'class="lo-page"' in body and 'src="/media' not in body  # nothing painted: the skeleton shows
    assert 'x-show="phase === \'pages\'" x-cloak data-sheet="right"' in body


def test_layout_page_ready_state_paints_the_first_page_and_the_filmstrip(editor):
    book = _book()
    row = _render(book, pages=4)
    body, config = _page(_logged(editor), book)
    p = config["initial"]["preview"]
    assert p["status"] == "done" and p["stale"] is False and p["render_id"] == row.pk and p["page_count"] == 4
    assert [page["n"] for page in p["pages"]] == [1, 2, 3, 4]
    first = p["pages"][0]
    assert first["url"].endswith("page-0001.webp") and first["url2x"].endswith("page-0001-2x.webp")
    assert p["pdf_url"].endswith("book.pdf") and p["chapters"][2] == {
        "id": "h20",
        "title": "الفصل الثاني",
        "first": 3,
        "last": 4,
    }
    # the first page is painted by the server (no skeleton flash), with its 2× source and an Arabic alt
    assert f'src="{first["url"]}" srcset="{first["url"]} 1x, {first["url2x"]} 2x" alt="صفحة 1"' in body
    assert 'x-show="phase === \'pages\'" data-sheet="right"' in body  # not cloaked
    # one static thumb per page, adopted by the component
    assert body.count('class="lo-thumb"') == 4
    assert 'data-index="2" data-number="3" title="صفحة 3" aria-label="صفحة 3"' in body
    assert 'loading="lazy"' in body
    # opened on a chapter: the chapter's first page is painted and its own render is looked up (none yet)
    body, config = _page(_logged(editor), book, "?chapter=h20")
    assert config["requestedChapter"] == "h20"
    assert f'src="{p["pages"][2]["url"]}"' in body and 'alt="صفحة 3"' in body
    assert config["initial"]["chapterPreview"]["scope"] == "chapter"
    assert config["initial"]["chapterPreview"]["chapter"] == "h20"
    assert config["initial"]["chapterPreview"]["status"] == "none"
    assert config["initial"]["chapterPreview"]["first_page"] == 3
    assert PreviewRender.objects.filter(scope="chapter").count() == 0  # never queued by the page
    # an unknown chapter falls back to the first page
    body, config = _page(_logged(editor), book, "?chapter=zz")
    assert config["requestedChapter"] == "zz" and config["initial"]["chapterPreview"] is None
    assert 'alt="صفحة 1"' in body


def test_layout_page_error_state(editor):
    book = _book()
    _render(book, status="error", error="تعذّر إخراج صفحات المعاينة.\nOSError: boom")
    body, config = _page(_logged(editor), book)
    p = config["initial"]["preview"]
    assert p["status"] == "error" and p["error"] == "تعذّر إخراج صفحات المعاينة." and p["pages"] == []
    assert "OSError" not in body  # the technical line stays in the log
    assert "data-error-state" in body and 'x-text="errorText"' in body and '@click="retry()"' in body


def test_layout_page_reports_a_missing_font(editor, settings, tmp_path):
    book = _book()
    StyleSheet.objects.create(book=book, body_font="simplified_arabic")
    settings.NASSAKH = {**settings.NASSAKH, "FONT_DIRS": [str(tmp_path)]}
    body, config = _page(_logged(editor), book)
    sheet = config["initial"]["stylesheet"]
    assert sheet["saved"] is True and sheet["stylesheet"]["body_font"] == "simplified_arabic"
    missing = sheet["missing_fonts"]  # the body face, and Times New Roman (the Latin default) with it
    assert [m["field"] for m in missing] == ["body_font", "latin_font"] and missing[0][
        "key"
    ] == "simplified_arabic"
    assert missing[0]["message"] == "الخط غير مثبّت على هذا الجهاز" and missing[0]["fallback"] == "Amiri"
    fonts = {f["key"]: f for f in sheet["fonts"]}
    assert fonts["simplified_arabic"]["installed"] is False and fonts["amiri"]["installed"] is True
    assert "lotus" not in sheet["latin_fonts"]
    assert config["initial"]["preview"]["missing_fonts"][0]["key"] == "simplified_arabic"
    assert "data-missing-fonts" in body and 'x-text="missingFontText"' in body


# ---------------------------------------------------------------- the compiled CSS


def test_compiled_css_has_the_layout_states_and_their_static_fallbacks():
    css = CSS.read_text(encoding="utf-8")
    # the screen takes the window like the dashboard viewer; the side panel scrolls as one column
    assert re.search(r"\.main:has\(>\.lo-screen\)\{[^}]*height:calc\(100dvh - var\(--topbar-height\)\)", css)
    assert re.search(r"\.main:has\(>\.lo-screen\)\{[^}]*flex:none", css)
    assert re.search(r"\.lo-side\{[^}]*overflow-y:auto", css)
    # one sheet fitted both ways at the trim's aspect; a spread halves it; the fit modes
    assert re.search(r"\.lo-sheet\{[^}]*aspect-ratio:var\(--lo-ar,\.7083\)", css)
    assert re.search(r"\.lo-sheet\{[^}]*width:min\(calc\(\(100cqh - 24px\) \* var\(--lo-ar,\.7083\)\)", css)
    assert re.search(r"\.lo-screen\.is-spread \.lo-sheet\{[^}]*/ 2\)\)", css)
    assert re.search(
        r"\.lo-screen\[data-fit=width\] \.lo-sheet\{[^}]*width:calc\(100cqw - 2 \* var\(--lo-gutter\)\)", css
    )
    assert re.search(
        r"\.lo-screen\[data-fit=actual\] \.lo-sheet\{[^}]*width:calc\(var\(--lo-w,170\) \* 1mm\)", css
    )
    assert re.search(r"\.lo-canvas\{[^}]*container-type:size", css)
    assert re.search(r"\.lo-sheet\.is-empty\{visibility:hidden\}", css)
    # the turn (= bk-stage.is-out-next): out to the right, in from the left
    assert re.search(r"\.lo-stage\.is-out-next \.lo-spread\{[^}]*transform:translate\(3%\)", css)
    assert re.search(
        r"\.lo-stage\.is-in-next \.lo-spread\{[^}]*transition:none[^}]*translate\(-3%\)", css
    ) or re.search(r"\.lo-stage\.is-in-next \.lo-spread\{[^}]*translate\(-3%\)", css)
    # skeleton pages and loading sheets shimmer; pills pulse; the delta reads LTR
    assert re.search(r"\.lo-sheet\.is-skeleton>span\{[^}]*lo-shimmer", css)
    assert re.search(r"\.lo-sheet\.is-loading:after\{[^}]*lo-shimmer", css)
    assert re.search(r"\.lo-pill\.is-saving \.lo-pill-dot\{[^}]*lo-pulse", css)
    assert re.search(r"\.lo-pill\.is-warn[^{]*\{[^}]*background:var\(--color-warning-bg\)", css)
    assert re.search(r"\.lo-delta\{[^}]*direction:ltr", css)
    # the diagram: the block inset by the margins, inner at the spine on both pages, the focused band lit
    assert re.search(r"\.is-verso \.lo-diagram-block\{[^}]*left:calc\(var\(--lo-i,\.13\) \* 100%\)", css)
    assert re.search(r"\.is-recto \.lo-diagram-block\{[^}]*right:calc\(var\(--lo-i,\.13\) \* 100%\)", css)
    assert re.search(r"\.lo-diagram\[data-focus=top\] \.lo-diagram-page:before\{[^}]*display:block", css)
    # steppers look like the jump field; the trims like a segmented list; thumbs like the dashboard's
    assert re.search(r"\.lo-stepper-box\{[^}]*background:var\(--color-bg-muted\)", css)
    assert re.search(r"\.lo-trim\[aria-checked=true\]\{[^}]*background:var\(--color-accent-soft\)", css)
    assert re.search(r"\.lo-thumb\.is-current\{box-shadow:0 0 0 2px var\(--color-accent\)\}", css)
    assert re.search(r"\.lo-font-menu\.is-above\{[^}]*bottom:calc\(100% - 22px\)", css)
    # narrow windows: the panel under the page, the filmstrip a row
    narrow = css[css.index(".lo-screen{--lo-gutter:44px}") :]  # the layout page's narrow-window block
    assert re.search(r"\.lo-layout\{[^}]*grid-template-columns:minmax\(0,1fr\)", narrow)
    assert re.search(r"\.lo-film-track\{[^}]*overflow:auto hidden", narrow)
    # reduced motion: no turn transition, no pulse, no shimmer
    reduced = css[css.rfind("prefers-reduced-motion:reduce") :]
    assert re.search(r"[^{}]*\.lo-stage\.is-out-next \.lo-spread[^{}]*\{transition:none\}", reduced)
    assert re.search(r"[^{}]*\.lo-pill\.is-saving \.lo-pill-dot[^{}]*\{animation:none\}", reduced)
    assert re.search(r"[^{}]*\.lo-sheet\.is-skeleton>span[^{}]*\{animation:none\}", reduced)
    # no rv- / ms- class is referenced by the layout styles
    layout = (ROOT / "static" / "src" / "components" / "layout.css").read_text(encoding="utf-8")
    assert not re.search(r"\.(rv|ms)-", layout)


# ---------------------------------------------------------------- Node: the bookLayout component

HARNESS = r"""
class ClassList { constructor(){ this.s = new Set(); } add(...c){ c.forEach(x => this.s.add(x)); } remove(...c){ c.forEach(x => this.s.delete(x)); }
  toggle(c, f){ if (f === undefined) f = !this.s.has(c); f ? this.s.add(c) : this.s.delete(c); return f; } contains(c){ return this.s.has(c); } toString(){ return [...this.s].join(' '); } }
class Element {
  constructor(tag){ this.tagName = String(tag).toUpperCase(); this.classList = new ClassList(); this.attrs = {}; this.dataset = {}; this.childNodes = []; this.parentNode = null; this.hidden = false; this.listeners = {};
    this.style = { setProperty: (k, v) => { this.style[k] = v; }, getPropertyValue: (k) => this.style[k] || '' }; }
  set className(v){ this.classList = new ClassList(); v.split(/\s+/).filter(Boolean).forEach(c => this.classList.add(c)); } get className(){ return this.classList.toString(); }
  setAttribute(k, v){ this.attrs[k] = String(v); } getAttribute(k){ return k in this.attrs ? this.attrs[k] : null; } removeAttribute(k){ delete this.attrs[k]; }
  appendChild(n){ if (n.parentNode) n.parentNode.removeChild(n); n.parentNode = this; this.childNodes.push(n); return n; }
  removeChild(n){ const i = this.childNodes.indexOf(n); if (i >= 0) this.childNodes.splice(i, 1); n.parentNode = null; return n; }
  remove(){ if (this.parentNode) this.parentNode.removeChild(this); }
  get children(){ return this.childNodes.slice(); }
  set textContent(v){ this._text = String(v); } get textContent(){ return this._text || ''; }
  addEventListener(ev, fn){ this.listeners[ev] = fn; }
  querySelector(sel){ const m = /^\[data-index="(\d+)"\]$/.exec(sel); if (m) return this.childNodes.find(c => c.dataset.index === m[1]) || null; return null; }
  closest(sel){ return sel === '.lo-thumb' && this.classList.contains('lo-thumb') ? this : null; }
}
const reg = {}; const inits = []; const stores = {}; const toasts = []; const assigned = []; const replaced = [];
globalThis.window = globalThis;
globalThis.document = { hidden: false, createElement: (t) => new Element(t), addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); }, getElementById: () => null, querySelector: () => ({ content: 'tok' }), querySelectorAll: () => [] };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: (n, v) => { if (v !== undefined) stores[n] = v; return stores[n]; } };
globalThis.location = { hash: '', pathname: '/books/1/layout/', search: '', assign: (u) => assigned.push(u) };
globalThis.history = { replaceState: (a, b, url) => replaced.push(url) };
globalThis.addEventListener = () => {};
let reducedFlag = false; globalThis.matchMedia = () => ({ matches: reducedFlag });
globalThis.requestAnimationFrame = (fn) => { fn(); return 1; };
const timers = []; let timerId = 0;
globalThis.setTimeout = (fn, ms) => { timerId += 1; timers.push({ id: timerId, fn, ms, cleared: false }); return timerId; };
globalThis.clearTimeout = (id) => { const t = timers.find((x) => x.id === id); if (t) t.cleared = true; };
const local = {}; globalThis.localStorage = { getItem: (k) => (k in local ? local[k] : null), setItem: (k, v) => { local[k] = String(v); } };
const session = {}; globalThis.sessionStorage = { getItem: (k) => (k in session ? session[k] : null), setItem: (k, v) => { session[k] = String(v); } };
const prefetched = []; globalThis.Image = class { set src(v) { prefetched.push(v); } };
globalThis.Nassakh = { toast: (m) => toasts.push(m) };
const fs = require('fs');
eval(fs.readFileSync(process.argv[2], 'utf8'));
inits.forEach((fn) => fn());

// ---- fetch stub: GET previews answered from per-scope queues (the last answer sticks), PUT / POST from hooks
const calls = [];
const queue = { book: [], chapter: [] };
let putReply = null; let postReply = null;
const reply = (status, data) => ({ ok: status < 300, status, json: async () => JSON.parse(JSON.stringify(data)) });
globalThis.fetch = async (url, init = {}) => {
  const method = init.method || 'GET';
  const body = init.body ? JSON.parse(init.body) : null;
  calls.push([method, url, body, init.headers && init.headers['X-CSRFToken'], Boolean(init.keepalive)]);
  if (method === 'GET') {
    const q = queue[/scope=chapter/.test(url) ? 'chapter' : 'book'];
    const next = q.length > 1 ? q.shift() : q[0];
    if (!next) return reply(500, {});
    return typeof next === 'function' ? next() : reply(200, next);
  }
  if (method === 'PUT') return typeof putReply === 'function' ? putReply(body) : putReply;
  return typeof postReply === 'function' ? postReply(body) : postReply;
};
const settle = async () => { for (let i = 0; i < 6; i += 1) await new Promise((r) => setImmediate(r)); };
const pending = (ms) => timers.filter((t) => !t.cleared && (ms === undefined || t.ms === ms));
const fire = async (ms) => { const list = pending(ms); const t = list[list.length - 1]; if (!t) return false; t.cleared = true; await t.fn(); await settle(); return true; };
const drop = () => { timers.forEach((t) => { t.cleared = true; }); };
const getCalls = () => calls.filter((c) => c[0] === 'GET').map((c) => c[1]);

// ---- payloads
const page = (n, tag = 'a', chapter) => ({ n, url: `/m/${tag}/page-${n}.webp`, url2x: `/m/${tag}/page-${n}-2x.webp`, chapter: chapter || (n === 1 ? 'p1' : n < 4 ? 'h10' : 'h20') });
const ranges5 = [{ id: 'p1', title: 'قبل الفصل الأول', first: 1, last: 1 }, { id: 'h10', title: 'الفصل الأول', first: 2, last: 3 }, { id: 'h20', title: 'الفصل الثاني', first: 4, last: 5 }];
const bookPayload = (over = {}) => ({ scope: 'book', chapter: null, status: 'done', hash: 'h1', stale: false, rendering: false, page_count: 5, first_page: 1, chapters: ranges5,
  pages: [1, 2, 3, 4, 5].map((n) => page(n)), render_id: 1, rendered_at: 't1', duration_ms: 300, pdf_url: '/m/a/book.pdf', error: '', missing_fonts: [], ...over });
const queued = (over = {}) => bookPayload({ status: 'queued', hash: 'h0', stale: true, rendering: true, page_count: 0, first_page: 1, chapters: [], pages: [], render_id: null, pdf_url: null, ...over });
const fonts = [
  { key: 'amiri', name: 'Amiri', label: 'أميري', family: 'Amiri', installed: true, latin: true },
  { key: 'simplified_arabic', name: 'Simplified Arabic', label: 'Simplified Arabic', family: 'Simplified Arabic', installed: false, latin: true },
  { key: 'times', name: 'Times New Roman', label: 'Times New Roman', family: 'Times New Roman', installed: true, latin: true },
  { key: 'lotus', name: 'Lotus', label: 'Lotus', family: 'Lotus Linotype Exnd', installed: true, latin: false },
];
const sheetPayload = (over = {}) => ({
  stylesheet: { trim: '17x24', width_mm: 170, height_mm: 240, top_mm: 20, bottom_mm: 22, inner_mm: 22, outer_mm: 18, bleed_mm: 0, body_font: 'amiri', latin_font: 'times', heading_font: 'amiri',
    body_size_pt: 13, line_height: 1.7, indent_em: 1.5, heading_scale: { h1: 1.6, h2: 1.25 }, footnote_size_pt: 10, footnote_numbering: 'page', running_header: 'chapter', page_number: 'bottom_center',
    chapter_opening: 'any', front_matter: { title_page: true, contents: true }, print_source_pages: false, updated_at: null, ...(over.stylesheet || {}) },
  saved: Boolean(over.saved),
  trims: [{ key: '17x24', label: '17×24 سم', width_mm: 170, height_mm: 240 }, { key: 'a5', label: 'A5', width_mm: 148, height_mm: 210 }, { key: 'custom', label: 'مقاس مخصّص', width_mm: null, height_mm: null }],
  fonts, latin_fonts: ['amiri', 'simplified_arabic', 'times'],
  choices: { running_header: [{ value: 'none', label: 'بلا ترويسة' }, { value: 'chapter', label: 'عنوان الفصل' }], page_number: [{ value: 'none', label: 'بلا ترقيم' }, { value: 'bottom_center', label: 'أسفل الصفحة في الوسط' }] },
  limits: { top_mm: [0, 80], inner_mm: [0, 80], body_size_pt: [7, 24], line_height: [1, 3], indent_em: [0, 6], 'heading_scale.h1': [1, 3] },
  missing_fonts: over.missing_fonts || [],
});
const chapters = [{ id: 'p1', number: 1, kind: 'front', title: 'قبل الفصل الأول' }, { id: 'h10', number: 2, kind: 'chapter', title: 'الفصل الأول' }, { id: 'h20', number: 3, kind: 'chapter', title: 'الفصل الثاني' }];
const urls = { stylesheet: '/api/books/1/stylesheet/', preview: '/api/books/1/preview/', editor: '/books/1/editor/', layout: '/books/1/layout/' };
const dom = () => {
  const film = new Element('div'); const canvas = new Element('div'); canvas.clientWidth = 1000;
  const root = new Element('div'); root.querySelector = (sel) => (sel === '[data-film-track]' ? film : sel === '[data-canvas]' ? canvas : null);
  return { root, film, canvas };
};
const mk = (extra = {}, initial = {}) => {
  const d = dom();
  const v = reg.bookLayout({ bookId: 1, title: 'كتاب', canEdit: true, chapters, urls, requestedChapter: null, initial: { stylesheet: sheetPayload(), preview: null, chapterPreview: null, ...initial }, ...extra });
  v.$el = d.root; v.$refs = {}; v.init();
  return Object.assign(v, { _dom: d });
};
const thumbsOf = (v) => v._dom.film.children.map((t) => [Number(t.dataset.number), t.getAttribute('src') || (t.childNodes[0] && t.childNodes[0].childNodes[0].getAttribute('src')), t.classList.contains('is-current'), t.classList.contains('is-fresh')]);

(async () => {
  const out = {};

  // --- init: nothing cached → the first GET, the skeleton, polling every second until the pages arrive
  queue.book = [queued(), queued({ status: 'running' }), bookPayload()];
  const v = mk();
  await settle();
  out.init = { phase: v.phase, gets: getCalls().length, firstGet: getCalls()[0], status: v.book.status, active: v.active, polling: pending(1000).length, pill: v.renderPill, footprint: { count: v.footprint.count, computing: v.footprint.computing, rows: v.footprint.rows.map((r) => [r.id, r.pages]) }, store: stores.layout.view === v };
  await fire(1000);
  const running = { status: v.book.status, phase: v.phase, polling: pending(1000).length };
  await fire(1000);
  out.ready = { running, status: v.book.status, phase: v.phase, pages: v.pageCount, current: v.current, cursor: v.cursor, active: v.active, polling: pending(1000).length, pill: v.renderPill.text,
    counter: v.counterText, chapter: v.counterChapter, thumbs: thumbsOf(v), footprint: { count: v.footprint.count, text: v.footprint.text, delta: v.footprint.deltaText, rows: v.footprint.rows.map((r) => [r.id, r.first, r.last, r.pages, r.current]) },
    editorHref: v.editorHref, pdf: v.pdfUrl, prefetched: prefetched.slice(), replaced: replaced.slice(-1)[0], session: session['nassakh.layout.page.1'], live: v.liveMessage };

  // --- a change: the value shows at once, one debounced PUT with the chapter under the eyes, the server's
  // values win, the preview payload of the answer starts the polling; the chapter's fresh render stands in
  // for its range while the book is stale; the book's new render swaps every page keeping the current one
  v.showPage(3, { instant: true });
  v.setTrim('a5'); v.step('top_mm', 1); v.step('top_mm', 1);
  const localNow = { trim: v.sheet.trim, width: v.sheet.width_mm, height: v.sheet.height_mm, top: v.sheet.top_mm, dirty: { ...v.dirty }, puts: calls.filter((c) => c[0] === 'PUT').length, debounce: pending(400).length };
  const stale5 = bookPayload({ status: 'queued', hash: 'h2', stale: true, rendering: true });
  putReply = (body) => reply(200, { ...sheetPayload({ saved: true, stylesheet: { trim: 'a5', width_mm: 148, height_mm: 210, top_mm: 22 } }), preview: stale5 });
  queue.book = [stale5];
  const chapterFresh = { scope: 'chapter', chapter: 'h10', status: 'done', hash: 'c1', stale: false, rendering: false, page_count: 3, first_page: 2, chapters: [{ id: 'h10', title: 'الفصل الأول', first: 2, last: 4 }], pages: [2, 3, 4].map((n) => page(n, 'c', 'h10')), render_id: 9, rendered_at: 't2', pdf_url: '/m/c/chapter.pdf', error: '', missing_fonts: [] };
  queue.chapter = [{ ...chapterFresh, status: 'running', rendering: true, pages: [], render_id: null, chapters: [], page_count: 0 }, chapterFresh];
  await fire(400);
  const put = calls.filter((c) => c[0] === 'PUT').pop();
  out.saved = { body: put[2], csrf: put[3], state: v.save.state, message: v.save.message, sheet: { trim: v.sheet.trim, top: v.sheet.top_mm, saved: v.saved }, tracked: v.chapterId, stale: v.stale, rendering: v.rendering, pill: v.renderPill.text, current: v.current, pages: v.pageCount, gets: getCalls().slice(-2), polling: pending(1000).length, savedTimer: pending(2000).length, ratio: v.sheetStyle.split(';')[0] };
  await fire(2000);
  out.savedCleared = v.save.state;
  await fire(1000); // the chapter render arrived (the book is still on its way): its pages stand in
  out.overlay = { current: v.current, pages: v.pageCount, scopes: v.pages.map((p) => p.scope), numbers: v.pages.map((p) => p.n), urls: v.pages.map((p) => p.url.split('/')[2]), range: v.ranges.find((r) => r.id === 'h10'), fresh: v.footprint.rows.map((r) => [r.id, r.fresh, r.pages]), pill: v.renderPill.text, thumbs: thumbsOf(v).map((t) => [t[0], t[3]]), counter: v.counterText };
  const book7 = bookPayload({ hash: 'h2', page_count: 7, chapters: [{ id: 'p1', title: 'قبل الفصل الأول', first: 1, last: 1 }, { id: 'h10', title: 'الفصل الأول', first: 2, last: 4 }, { id: 'h20', title: 'الفصل الثاني', first: 5, last: 7 }], pages: [1, 2, 3, 4, 5, 6, 7].map((n) => page(n, 'b', n === 1 ? 'p1' : n < 5 ? 'h10' : 'h20')), render_id: 2 });
  queue.book = [book7];
  await fire(1000);
  out.swapped = { current: v.current, pages: v.pageCount, scopes: [...new Set(v.pages.map((p) => p.scope))], urls: [...new Set(v.pages.map((p) => p.url.split('/')[2]))], delta: v.footprint.deltaText, chapterDeltas: { ...v.chapterDeltas }, rows: v.footprint.rows.map((r) => [r.id, r.pages, r.delta]), active: v.active, polling: pending(1000).length, pill: v.renderPill.text, thumbs: thumbsOf(v).length, counter: v.counterText, editorHref: v.editorHref, ratio: v.sheetStyle.split(';')[0] };

  // --- steppers and fields: parsing, clamping, formatting; a change while a PUT is on the wire waits for it
  out.fields = [];
  v.setField('body_size_pt', '٤٠'); out.fields.push(['clamp-eastern', v.sheet.body_size_pt]);
  v.setField('line_height', 'abc'); out.fields.push(['nan-kept', v.sheet.line_height]);
  v.setField('line_height', '1,75'); out.fields.push(['comma', v.sheet.line_height, v.fmt(v.sheet.line_height)]);
  v.step('heading_scale.h1', -1); out.fields.push(['nested', v.sheet.heading_scale.h1, v.dirty['heading_scale.h1']]);
  v.setField('front_matter.contents', false); out.fields.push(['bool', v.sheet.front_matter.contents]);
  out.fields.push(['fmt', v.fmt(20), v.fmt(1.7), v.fmt(12.5), v.fmt('x')]);
  out.fields.push(['takeDirty', v.takeDirty()]);
  out.fields.push(['trimLabel', v.trimLabel, v.textAreaText]);
  v.setTrim('custom'); v.setField('width_mm', 160); out.fields.push(['custom', v.sheet.trim, v.sheet.width_mm, { ...v.dirty }, v.trimLabel]);
  v.dirty = {}; drop();
  out.diagram = v.diagramStyle; v.focusField('inner_mm'); out.focus = v.focusMargin; v.focusField('');
  // a 400: the refused field keeps its message, the valid fields of the same PUT are sent again
  v.setField('inner_mm', 70); v.setField('top_mm', 25);
  putReply = (body) => reply(400, { detail: 'قيم غير صالحة.', errors: { inner_mm: 'الهوامش الجانبية أعرض من أن يبقى للنص مكان.' } });
  await fire(400);
  const invalid = { state: v.save.state, errors: { ...v.errors }, dirty: { ...v.dirty }, retryQueued: pending(400).length };
  putReply = (body) => reply(200, { ...sheetPayload({ saved: true, stylesheet: { top_mm: 25 } }), preview: bookPayload({ hash: 'h3', render_id: 2, page_count: 7 }) });
  await fire(400);
  const resent = calls.filter((c) => c[0] === 'PUT').pop()[2];
  out.invalid = { ...invalid, resent, after: { state: v.save.state, errors: { ...v.errors }, inner: v.sheet.inner_mm, top: v.sheet.top_mm } };
  // a network failure keeps the change for «إعادة المحاولة»; a 403 names the role; the corrected field clears
  v.setField('inner_mm', 22); v.setField('outer_mm', 19);
  putReply = () => { throw new Error('down'); };
  await fire(400);
  const failed = { state: v.save.state, message: v.save.message, dirty: { ...v.dirty } };
  putReply = () => reply(403, { detail: 'هذا الإجراء يتطلب صلاحية محرّر.' });
  const ok403 = await v.flush();
  const refused = { ok: ok403, message: v.save.message, dirty: { ...v.dirty } };
  putReply = () => reply(200, { ...sheetPayload({ saved: true, stylesheet: { outer_mm: 19 } }), preview: null });
  const okAgain = await v.flush();
  out.failure = { failed, refused, okAgain, state: v.save.state, dirty: { ...v.dirty } };
  drop();
  // leaving the page with changes still waiting for the debounce: one PUT the browser keeps alive
  v.setField('top_mm', 21); putReply = () => reply(200, { ...sheetPayload({ saved: true, stylesheet: { top_mm: 21 } }), preview: null });
  const unloaded = v.onUnload(); const unloadPut = calls.filter((c) => c[0] === 'PUT').pop(); await settle();
  out.unload = { sent: unloaded, keepalive: unloadPut[4], body: unloadPut[2], dirty: { ...v.dirty }, top: v.sheet.top_mm, idle: v.onUnload() };
  drop();
  // a change while a PUT is on the wire keeps its value on screen when the answer lands (the next PUT carries it)
  let resolvePut = null; putReply = () => new Promise((res) => { resolvePut = res; });
  v.setField('outer_mm', 20);
  const debounce = pending(400).pop(); debounce.cleared = true; const inflightPut = debounce.fn(); await settle(); // the PUT is on the wire
  v.setField('outer_mm', 24); const during = { value: v.sheet.outer_mm, saving: v.saving, armed: pending(400).length };
  resolvePut(reply(200, { ...sheetPayload({ saved: true, stylesheet: { outer_mm: 20 } }), preview: null })); await inflightPut; await settle();
  const landed = { value: v.sheet.outer_mm, dirty: { ...v.dirty }, state: v.save.state };
  putReply = () => reply(200, { ...sheetPayload({ saved: true, stylesheet: { outer_mm: 24 } }), preview: null });
  await fire(400);
  out.midflight = { during, landed, final: v.sheet.outer_mm, dirty: { ...v.dirty }, puts: calls.filter((c) => c[0] === 'PUT').length };
  drop();
  // a face menu low in the scrolling panel opens upwards (the panel would clip it); high up it opens down
  out.menuAbove = [v.menuAbove({ top: 600, bottom: 634 }, { top: 100, bottom: 700 }, 300), v.menuAbove({ top: 200, bottom: 234 }, { top: 100, bottom: 700 }, 300), v.menuAbove({ top: 300, bottom: 334 }, { top: 100, bottom: 500 }, 300), v.fontMenuAbove(null)];

  // --- spread mode: (even n, odd n + 1) with the recto on the left; page 1 alone; narrow stages show one page
  v.showPage(3, { instant: true });
  v.setSpread(true);
  const s23 = { cursor: v.cursor, right: v.rightPage && v.rightPage.n, left: v.leftPage && v.leftPage.n, counter: v.counterText, stored: local['nassakh.layout.spread'], marked: thumbsOf(v).filter((t) => t[2]).map((t) => t[0]) };
  v.turn(1); await fire(200);
  const s45 = { right: v.rightPage.n, left: v.leftPage.n, prev: v.neighbour(-1), next: v.neighbour(1) };
  v.showPage(1, { instant: true });
  const s1 = { right: v.rightPage, left: v.leftPage && v.leftPage.n, canPrev: v.canTurn(-1), turned: v.turn(-1) };
  v.showPage(7, { instant: true });
  const s67 = { right: v.rightPage.n, left: v.leftPage.n, canNext: v.canTurn(1) };
  v._dom.canvas.clientWidth = 500; v.onResize();
  const narrow = { spreadOn: v.spreadOn, spread: v.spread, right: v.rightPage.n, left: v.leftPage };
  v._dom.canvas.clientWidth = 1000; v.onResize();
  const wideAgain = { spreadOn: v.spreadOn, right: v.rightPage.n, left: v.leftPage.n };
  v.setSpread(false);
  out.spread = { s23, s45, s1, s67, narrow, wideAgain, single: [v.rightPage.n, v.leftPage] };

  // --- fit modes, the side panel's tabs, the keyboard map
  v.setFit('width'); const fitW = [v.fit, local['nassakh.layout.fit']]; v.setFit('bogus'); const fitBogus = v.fit; v.setFit('actual');
  v.setPanel('pages'); const panel = [v.panel, local['nassakh.layout.panel']]; v.setPanel('style');
  out.modes = { fitW, fitBogus, fitActual: v.fit, panel, sheetStyle: v.sheetStyle };
  out.keys = ['ArrowLeft', 'ArrowRight', 'PageDown', 'PageUp', 'Home', 'End'].map((key) => v.keyAction({ key }, false));
  out.keys.push(v.keyAction({ key: 'g', code: 'KeyG' }, false), v.keyAction({ key: 's', code: 'KeyS' }, false), v.keyAction({ key: 'e', code: 'KeyE' }, false), v.keyAction({ key: '1' }, false), v.keyAction({ key: '3' }, false),
    v.keyAction({ key: 'Escape' }, true), v.keyAction({ key: 'g', code: 'KeyG' }, true), v.keyAction({ key: 'g', code: 'KeyG', metaKey: true }, false), v.keyAction({ key: 'Escape' }, false));
  v.onKey({ key: 'e', code: 'KeyE', target: {} }); out.keyEditor = assigned.slice(-1)[0];
  v.onKey({ key: '1', target: {} }); out.keyFit = v.fit;
  v.onKey({ key: 's', code: 'KeyS', target: {} }); out.keySpread = v.spread; v.setSpread(false);
  v.onKey({ key: 'Home', target: {}, preventDefault: () => {} }); await fire(200); out.keyHome = v.current;

  // --- the turn: out for 200 ms, a second press mid-turn moves the target, the sheet lands once; reduced
  // motion is instant; wheel and touch (RTL) as on the dashboard
  drop(); v.showPage(1, { instant: true });
  v.turn(1);
  const midTurn = { turning: v.turning, current: v.current, timer: pending(200).length };
  v.turn(1);
  await fire(200);
  out.turn = { midTurn, after: { turning: v.turning, current: v.current, marked: thumbsOf(v).filter((t) => t[2]).map((t) => t[0]), replaced: replaced.slice(-1)[0] } };
  reducedFlag = true; v.turn(1); out.reduced = { current: v.current, turning: v.turning }; reducedFlag = false;
  v.showPage(1, { instant: true }); drop();
  const wheel = (dx, dy) => v.onStageWheel({ deltaX: dx, deltaY: dy, cancelable: true, preventDefault: () => {} });
  wheel(-30, 0); const afterOne = v.current; wheel(-30, 0); await fire(200);
  const afterSwipe = v.current; wheel(-80, 0); const inertia = v.current;
  await fire(260); wheel(0, 100); await fire(200);
  const wheelDown = v.current;
  v.onStagePointerDown({ pointerType: 'touch', clientX: 100, clientY: 100 }); v.onStagePointerUp({ pointerType: 'touch', clientX: 30, clientY: 104 }); await fire(200);
  const touchBack = v.current;
  v.onStagePointerDown({ pointerType: 'mouse', clientX: 100, clientY: 100 }); v.onStagePointerUp({ pointerType: 'mouse', clientX: 300, clientY: 100 });
  const mouseIgnored = v.current;
  v.setFit('width'); wheel(0, 200); const scrollingStage = v.current; v.setFit('height');
  out.wheel = { afterOne, afterSwipe, inertia, wheelDown, touchBack, mouseIgnored, scrollingStage };
  // a click on a thumb (delegated on the track) turns to that page
  v._dom.film.listeners.click({ target: v._dom.film.children[4] }); await fire(200);
  out.thumbClick = { current: v.current, marked: thumbsOf(v).filter((t) => t[2]).map((t) => t[0]) };

  // --- jump, the chapter list, the fonts
  out.jump = [v.jumpTarget('٣'), v.jumpTarget(' 7 '), v.jumpTarget('99'), v.jump('99'), toasts.slice(-1)[0]];
  v.goToChapter('h20'); await fire(200);
  out.chapter = { current: v.current, focus: v.focusChapter, editorHref: v.editorHref, unknown: v.goToChapter('zz'), toast: toasts.slice(-1)[0] };
  out.fonts = { latin: v.fontChoices('latin').map((f) => f.key), all: v.fontChoices('body').length, label: v.fontLabel('amiri'), installed: [v.fontInstalled('amiri'), v.fontInstalled('simplified_arabic')], style: v.faceStyle('lotus'), field: v.fontField('latin'), sample: v.sample('latin'), missing: v.missingFontText };
  v.missingFonts = [{ field: 'body_font', key: 'simplified_arabic', name: 'Simplified Arabic', message: 'الخط غير مثبّت على هذا الجهاز', fallback: 'Amiri' }];
  out.missingText = v.missingFontText;

  // --- the stale pill: a stale content with nothing running is asked for once (POST), never twice
  postReply = (body) => reply(202, queued({ hash: 'h9', scope: body.scope }));
  const stalePayload = bookPayload({ status: 'none', hash: 'h9', stale: true, rendering: false });
  v.applyPreview('book', stalePayload);
  await settle();
  const posts1 = calls.filter((c) => c[0] === 'POST');
  v.applyPreview('book', stalePayload);
  await settle();
  out.stale = { pill: (v.book = { ...stalePayload }, v.renderPill), posts: posts1.length, body: posts1[0] && posts1[0][2], postsAfterRepeat: calls.filter((c) => c[0] === 'POST').length, pagesKept: v.pageCount };
  v.book = { ...stalePayload, rendering: true, status: 'queued' }; out.updatingPill = v.renderPill.text;

  // --- the error state: nothing cached → the designed state; with pages → the banner and the pill; retry POSTs
  // (the tracked chapter's fresh render is let go first: it would stand in for the missing pages)
  drop(); v.chapterId = null; v.chapter = null;
  const errPayload = bookPayload({ status: 'error', hash: 'h10', stale: true, error: 'تعذّر إخراج صفحات المعاينة.', pages: [], chapters: [], page_count: 0, render_id: null });
  v.applyPreview('book', errPayload, { quiet: true });
  const noPages = { phase: v.phase, error: v.errorText, pill: v.renderPill.text, posts: calls.filter((c) => c[0] === 'POST').length };
  postReply = () => reply(500, {}); // the one automatic request after a failure; refused here, so the state stays
  v.applyPreview('book', errPayload); await settle();
  v.applyPreview('book', errPayload); await settle();
  const autoOnce = { posts: calls.filter((c) => c[0] === 'POST').length - noPages.posts, phase: v.phase };
  v.applyPreview('book', bookPayload({ status: 'error', hash: 'h11', stale: true, error: 'فشل' }), { quiet: true });
  const withPages = { phase: v.phase, error: v.errorText, pill: v.renderPill, pages: v.pageCount };
  postReply = (body) => reply(202, queued({ hash: 'h11' }));
  const retried = await v.retry();
  out.error = { noPages, autoOnce, withPages, retried, afterRetry: { status: v.book.status, phase: v.phase, polling: pending(1000).length, pill: v.renderPill.text } };
  // the tab back in front while a render runs: polled at once; nothing to wait for → nothing polled
  drop(); calls.length = 0; queue.book = [bookPayload({ hash: 'h11', render_id: 3 })];
  const polledBack = v.onVisible(); await settle();
  out.visible = { polled: polledBack, gets: getCalls().length, phase: v.phase, idle: (drop(), v.onVisible()) };
  // a swap that loses the page's number lands on the nearest position: the address and the counter follow
  drop(); v.showPage(5, { instant: true }); v.applyPreview('book', bookPayload({ hash: 'h11', render_id: 4, page_count: 3, chapters: [{ id: 'p1', title: 'قبل الفصل الأول', first: 1, last: 1 }, { id: 'h10', title: 'الفصل الأول', first: 2, last: 3 }], pages: [1, 2, 3].map((n) => page(n, 'z')) }), { quiet: true });
  out.swapLost = { current: v.current, replaced: replaced.slice(-1)[0], session: session['nassakh.layout.page.1'], live: v.liveMessage };

  // --- a page painted by the server is drawn already; the address opens the page it names
  drop(); calls.length = 0;
  queue.book = [bookPayload()];
  globalThis.location.hash = '#page-4';
  const v2 = mk({}, { preview: bookPayload() });
  await settle();
  out.fromHash = { current: v2.current, phase: v2.phase, gets: getCalls().length, replaced: replaced.slice(-1)[0], loading: [v2.isLoading(v2.rightPage)], polling: pending(1000).length };
  v2.onLoad('right'); out.loaded = v2.isLoading(v2.rightPage);
  globalThis.location.hash = '';

  // --- opened from the editor on a chapter: the chapter's first page, its own render tracked and polled
  drop(); calls.length = 0;
  queue.book = [bookPayload()]; queue.chapter = [{ ...chapterFresh, chapter: 'h20', status: 'running', rendering: true, pages: [], render_id: null, chapters: [], page_count: 0, first_page: 4 }];
  const v3 = mk({ requestedChapter: 'h20' }, { preview: bookPayload(), chapterPreview: { ...chapterFresh, chapter: 'h20', status: 'none', rendering: false, stale: true, pages: [], render_id: null, chapters: [], page_count: 0, first_page: 4 } });
  await settle();
  out.fromEditor = { current: v3.current, tracked: v3.chapterId, gets: getCalls(), active: v3.active, polling: pending(1000).length, focus: v3.focusChapter };

  // --- a proofreader: no PUT, no automatic request; the session's end stops the polling; failures back off
  drop(); calls.length = 0;
  queue.book = [bookPayload({ status: 'none', stale: true, hash: 'h12' })];
  const r = mk({ canEdit: false });
  await settle();
  r.setField('top_mm', 30); await fire(400);
  out.reader = { puts: calls.filter((c) => c[0] === 'PUT').length, posts: calls.filter((c) => c[0] === 'POST').length, pill: r.renderPill, value: r.sheet.top_mm };
  drop(); calls.length = 0;
  queue.book = [() => reply(403, {})];
  const a = mk();
  await settle();
  out.auth = { state: a.pollState, stopped: a.stopped, polling: pending(1000).length };
  drop(); calls.length = 0;
  queue.book = [() => { throw new Error('down'); }];
  const f = mk();
  await settle();
  const backoff = [pending().map((t) => t.ms)];
  await fire(); backoff.push(pending().map((t) => t.ms)); await fire(); backoff.push(pending().map((t) => t.ms));
  out.backoff = { state: f.pollState, failures: f.failures, timers: backoff };
  queue.book = [bookPayload()]; await fire();
  out.recovered = { state: f.pollState, phase: f.phase, polling: pending().length };
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_layout_component_under_node(tmp_path):
    harness = tmp_path / "layout.js"
    harness.write_text(HARNESS, encoding="utf-8")
    run = subprocess.run(
        ["node", str(harness), str(JS / "layout.js")], capture_output=True, text=True, timeout=60
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])

    # --- init: nothing cached → one GET (the server queues), the skeleton, the pill, polling every second
    init = out["init"]
    assert (
        init["phase"] == "skeleton"
        and init["gets"] == 1
        and init["firstGet"] == "/api/books/1/preview/?scope=book"
    )
    assert (
        init["status"] == "queued"
        and init["active"] is True
        and init["polling"] == 1
        and init["store"] is True
    )
    assert init["pill"] == {"state": "saving", "text": "تُحدَّث المعاينة…", "action": ""}
    assert init["footprint"] == {
        "count": 0,
        "computing": True,
        "rows": [["p1", None], ["h10", None], ["h20", None]],
    }
    ready = out["ready"]
    assert ready["running"] == {"status": "running", "phase": "skeleton", "polling": 1}
    assert ready["status"] == "done" and ready["phase"] == "pages" and ready["pages"] == 5
    assert (
        ready["current"] == 1 and ready["cursor"] == 0 and ready["active"] is False and ready["polling"] == 0
    )
    assert ready["pill"] == "" and ready["counter"] == "صفحة 1 من 5" and ready["live"] == "صفحة 1 من 5"
    assert ready["chapter"] == {"id": "p1", "title": "قبل الفصل الأول", "first": 1, "last": 1, "fresh": False}
    assert ready["thumbs"] == [
        [1, "/m/a/page-1.webp", True, False],
        [2, "/m/a/page-2.webp", False, False],
        [3, "/m/a/page-3.webp", False, False],
        [4, "/m/a/page-4.webp", False, False],
        [5, "/m/a/page-5.webp", False, False],
    ]
    assert ready["footprint"] == {
        "count": 5,
        "text": "5 صفحات",
        "delta": "",
        "rows": [["p1", 1, 1, 1, True], ["h10", 2, 3, 2, False], ["h20", 4, 5, 2, False]],
    }
    assert ready["editorHref"] == "/books/1/editor/?chapter=p1" and ready["pdf"] == "/m/a/book.pdf"
    assert (
        ready["prefetched"] == ["/m/a/page-2.webp"]
        and ready["replaced"] == "/books/1/layout/#page-1"
        and ready["session"] == "1"
    )

    # --- a change → one debounced PUT (values shown at once, the chapter under the eyes sent) → polling; the
    # chapter's fresh render stands in for its range; the book's new render swaps every page, page 3 kept
    saved = out["saved"]
    assert saved["body"] == {"trim": "a5", "top_mm": 22, "chapter": "h10"} and saved["csrf"] == "tok"
    assert (
        saved["state"] == "saved"
        and saved["message"] == "حُفظ"
        and saved["sheet"] == {"trim": "a5", "top": 22, "saved": True}
    )
    assert (
        saved["tracked"] == "h10"
        and saved["rendering"] is True
        and saved["stale"] is False
        and saved["pill"] == "تُحدَّث المعاينة…"
    )
    assert (
        saved["current"] == 3 and saved["pages"] == 5 and saved["polling"] == 1 and saved["savedTimer"] == 1
    )
    assert saved["gets"] == [
        "/api/books/1/preview/?scope=book",
        "/api/books/1/preview/?scope=chapter&chapter=h10",
    ]
    assert saved["ratio"] == "--lo-ar: 0.7083"  # the sheets keep the shape of the pages drawn in them
    assert out["savedCleared"] == ""
    overlay = out["overlay"]
    assert overlay["current"] == 3 and overlay["pages"] == 6
    assert overlay["scopes"] == ["book", "chapter", "chapter", "chapter", "book", "book"]
    assert overlay["numbers"] == [1, 2, 3, 4, 4, 5] and overlay["urls"] == ["a", "c", "c", "c", "a", "a"]
    assert overlay["range"] == {"id": "h10", "title": "الفصل الأول", "first": 2, "last": 4, "fresh": True}
    assert overlay["fresh"] == [["p1", False, 1], ["h10", True, 3], ["h20", False, 2]]
    assert overlay["pill"] == "تُحدَّث المعاينة…" and overlay["counter"] == "صفحة 3 من 6"
    assert overlay["thumbs"] == [[1, False], [2, True], [3, True], [4, True], [4, False], [5, False]]
    swapped = out["swapped"]
    assert (
        swapped["current"] == 3
        and swapped["pages"] == 7
        and swapped["scopes"] == ["book"]
        and swapped["urls"] == ["b"]
    )
    assert swapped["delta"] == "+2" and swapped["chapterDeltas"] == {"h10": 1, "h20": 1}
    assert swapped["rows"] == [["p1", 1, 0], ["h10", 3, 1], ["h20", 3, 1]]
    assert (
        swapped["active"] is False
        and swapped["polling"] == 0
        and swapped["pill"] == ""
        and swapped["thumbs"] == 7
    )
    assert swapped["counter"] == "صفحة 3 من 7" and swapped["editorHref"] == "/books/1/editor/?chapter=h10"
    assert swapped["ratio"] == "--lo-ar: 0.7048"  # A5 now that the A5 render is on screen

    # --- fields: parsing (Eastern digits, a comma), clamping, nested paths, booleans, formatting, custom trim
    fields = dict((f[0], f[1:]) for f in out["fields"])
    assert (
        fields["clamp-eastern"] == [24] and fields["nan-kept"] == [1.7] and fields["comma"] == [1.75, "1.75"]
    )
    assert fields["nested"] == [1.55, 1.55] and fields["bool"] == [False]
    assert fields["fmt"] == ["20", "1.7", "12.5", ""]
    assert fields["takeDirty"] == [
        {
            "body_size_pt": 24,
            "line_height": 1.75,
            "heading_scale": {"h1": 1.55},
            "front_matter": {"contents": False},
        }
    ]
    assert fields["trimLabel"] == ["A5", "108×166 مم"]  # 148 − 22 − 18, 210 − 22 − 22
    assert fields["custom"] == [
        "custom",
        160,
        {"trim": "custom", "width_mm": 160, "height_mm": 210},
        "160×210 مم",
    ]
    assert out["diagram"].startswith("--lo-ar: 0.7619; --lo-t: 0.1048;") and out["focus"] == "inner"
    invalid = out["invalid"]
    assert invalid["state"] == "invalid" and invalid["errors"] == {
        "inner_mm": "الهوامش الجانبية أعرض من أن يبقى للنص مكان."
    }
    assert invalid["dirty"] == {"top_mm": 25} and invalid["retryQueued"] == 1
    assert invalid["resent"] == {"top_mm": 25, "chapter": "h10"}
    # the refused field keeps its typed value and its message after the valid ones were saved
    assert invalid["after"] == {
        "state": "invalid",
        "errors": {"inner_mm": "الهوامش الجانبية أعرض من أن يبقى للنص مكان."},
        "inner": 70,
        "top": 25,
    }
    failure = out["failure"]
    assert failure["failed"] == {
        "state": "error",
        "message": "تعذّر الحفظ · إعادة المحاولة",
        "dirty": {"inner_mm": 22, "outer_mm": 19},
    }
    assert failure["refused"] == {
        "ok": False,
        "message": "هذا الإجراء يتطلب صلاحية محرّر.",
        "dirty": {"inner_mm": 22, "outer_mm": 19},
    }
    assert failure["okAgain"] is True and failure["state"] == "saved" and failure["dirty"] == {}
    # leaving with a change waiting: it is sent with keepalive; nothing waiting → nothing sent
    assert out["unload"] == {
        "sent": True,
        "keepalive": True,
        "body": {"top_mm": 21, "chapter": "h10"},
        "dirty": {},
        "top": 21,
        "idle": False,
    }
    # a change during a PUT keeps its value when the older answer lands, and goes out in the next PUT
    mid = out["midflight"]
    assert mid["during"] == {"value": 24, "saving": True, "armed": 1}
    assert mid["landed"] == {"value": 24, "dirty": {"outer_mm": 24}, "state": "saved"}
    assert mid["final"] == 24 and mid["dirty"] == {} and mid["puts"] >= 2
    assert out["menuAbove"] == [True, False, False, False]

    # --- spread mode: (2, 3) with the recto on the left, page 1 alone on the left, (6, 7) at the end; a
    # narrow stage shows one page and the pairing returns with the width
    spread = out["spread"]
    assert spread["s23"] == {
        "cursor": 1,
        "right": 2,
        "left": 3,
        "counter": "الصفحتان 2–3 من 7",
        "stored": "1",
        "marked": [2, 3],
    }
    assert spread["s45"] == {"right": 4, "left": 5, "prev": 1, "next": 5}
    assert spread["s1"] == {"right": None, "left": 1, "canPrev": False, "turned": False}
    assert spread["s67"] == {"right": 6, "left": 7, "canNext": False}
    assert spread["narrow"] == {"spreadOn": False, "spread": True, "right": 6, "left": None}
    assert spread["wideAgain"] == {"spreadOn": True, "right": 6, "left": 7}
    assert spread["single"] == [6, None]

    # --- fit modes and the panel's tabs are remembered; the keyboard map (RTL)
    modes = out["modes"]
    assert (
        modes["fitW"] == ["width", "width"]
        and modes["fitBogus"] == "height"
        and modes["fitActual"] == "actual"
    )
    assert (
        modes["panel"] == ["pages", "pages"]
        and modes["sheetStyle"] == "--lo-ar: 0.7083; --lo-w: 170; --lo-gap: 12px"
    )  # the server's values won on the last save
    assert out["keys"] == [
        "next",
        "prev",
        "next",
        "prev",
        "first",
        "last",
        "jump",
        "spread",
        "editor",
        "fitHeight",
        "fitActual",
        "blur",
        None,
        None,
        None,
    ]
    assert (
        out["keyEditor"] == "/books/1/editor/?chapter=h20"
        and out["keyFit"] == "height"
        and out["keySpread"] is True
        and out["keyHome"] == 1
    )

    # --- the turn motion, reduced motion, wheel and touch (RTL), a thumb click
    assert out["turn"]["midTurn"] == {"turning": "out-next", "current": 1, "timer": 1}
    assert out["turn"]["after"] == {
        "turning": "",
        "current": 3,
        "marked": [3],
        "replaced": "/books/1/layout/#page-3",
    }
    assert out["reduced"] == {"current": 4, "turning": ""}
    assert out["wheel"] == {
        "afterOne": 1,
        "afterSwipe": 2,
        "inertia": 2,
        "wheelDown": 3,
        "touchBack": 2,
        "mouseIgnored": 2,
        "scrollingStage": 2,
    }
    assert out["thumbClick"] == {"current": 5, "marked": [5]}

    # --- jump, the chapter list, the fonts
    assert out["jump"] == [3, 7, None, None, "لا صفحة بهذا الرقم"]
    assert out["chapter"] == {
        "current": 5,
        "focus": "h20",
        "editorHref": "/books/1/editor/?chapter=h20",
        "unknown": False,
        "toast": "لم تُخرَج صفحات هذا الفصل بعد",
    }
    fonts = out["fonts"]
    assert (
        fonts["latin"] == ["amiri", "simplified_arabic", "times"]
        and fonts["all"] == 4
        and fonts["label"] == "أميري"
    )
    assert (
        fonts["installed"] == [True, False]
        and fonts["style"] == 'font-family: "Lotus Linotype Exnd", var(--font-sans)'
    )
    assert (
        fonts["field"] == "latin_font" and fonts["sample"] == "Nassakh, 1234 pages" and fonts["missing"] == ""
    )
    assert out["missingText"] == "Simplified Arabic غير مثبّت على هذا الجهاز؛ تُخرَج الصفحات بخط Amiri بدلًا منه."

    # --- stale: the pill, one automatic POST per content; updating: the pill
    stale = out["stale"]
    assert stale["pill"] == {"state": "warn", "text": "المعاينة أقدم من النص", "action": "retry"}
    assert stale["posts"] == 1 and stale["body"] == {"scope": "book"} and stale["postsAfterRepeat"] == 1
    assert stale["pagesKept"] == 6  # the old pages stay, the fresh chapter still standing in for its range
    assert out["updatingPill"] == "تُحدَّث المعاينة…"

    # --- error: the designed state without pages (asked for once by itself), the banner with pages, the retry
    error = out["error"]
    assert error["noPages"] == {
        "phase": "error",
        "error": "تعذّر إخراج صفحات المعاينة.",
        "pill": "",
        "posts": 1,
    }
    assert error["autoOnce"] == {"posts": 1, "phase": "error"}
    assert error["withPages"] == {
        "phase": "pages",
        "error": "فشل",
        "pill": {"state": "error", "text": "تعذّر تحديث المعاينة · إعادة المحاولة", "action": "retry"},
        "pages": 5,
    }
    assert error["retried"] is True and error["afterRetry"] == {
        "status": "queued",
        "phase": "skeleton",
        "polling": 1,
        "pill": "تُحدَّث المعاينة…",
    }
    assert out["visible"] == {"polled": True, "gets": 1, "phase": "pages", "idle": False}
    assert out["swapLost"] == {
        "current": 3,
        "replaced": "/books/1/layout/#page-3",
        "session": "3",
        "live": "صفحة 3 من 3",
    }

    # --- the address names the page; the server-painted page shows at once; opened from the editor
    assert out["fromHash"] == {
        "current": 4,
        "phase": "pages",
        "gets": 1,
        "replaced": "/books/1/layout/#page-4",
        "loading": [True],
        "polling": 0,
    }
    assert out["loaded"] is False
    assert out["fromEditor"] == {
        "current": 4,
        "tracked": "h20",
        "gets": ["/api/books/1/preview/?scope=book", "/api/books/1/preview/?scope=chapter&chapter=h20"],
        "active": True,
        "polling": 1,
        "focus": "h20",
    }

    # --- a proofreader changes nothing on the server; auth loss stops the polling; failures back off
    assert out["reader"] == {
        "puts": 0,
        "posts": 0,
        "pill": {"state": "warn", "text": "المعاينة أقدم من النص", "action": ""},
        "value": 30,
    }
    assert out["auth"] == {"state": "auth", "stopped": True, "polling": 0}
    assert out["backoff"] == {"state": "error", "failures": 3, "timers": [[2000], [3000], [4000]]}
    assert out["recovered"] == {"state": "ok", "phase": "pages", "polling": 0}
