"""Phase 3 theatre UI as rebuilt by docs/DASHBOARD_SPEC.md: the dashboard's static shells, toolbar and chrome
(Django test client), the text panel's decode hooks, the compiled CSS, and — under Node with a tiny DOM stub —
the decode engine (D28 dwell-and-veil), the pure text layout, the sheet handle and the dashboard logic."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from django.contrib.auth.models import Group, User
from django.template.loader import render_to_string
from django.urls import NoReverseMatch, reverse

import pytest

from books.models import Book, Page

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "src" / "js"
CSS = ROOT / "static" / "dist" / "app.css"


def _user(name: str, group: str) -> User:
    user = User.objects.create_user(name, password="pass-1234")
    user.groups.add(Group.objects.get_or_create(name=group)[0])
    return user


@pytest.fixture
def editor_client(client):
    client.force_login(_user("editor", "editor"))
    return client


def _book(states, status: str = Book.Status.OCR):
    """A book with one page per (page status, text_state) pair."""
    book = Book.objects.create(title="كتاب المسرح", status=status)
    pages = [
        Page.objects.create(
            book=book,
            number=i,
            source_index=i - 1,
            status=page_status,
            text_state=text_state,
            provisional_text="نص مبدئي من تسراكت" if text_state != "none" else "",
            final_text="نص نهائي" if text_state == "final" else "",
            width=1100,
            height=1600,
        )
        for i, (page_status, text_state) in enumerate(states, start=1)
    ]
    return book, pages


def _optional(name: str, *args) -> str:
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return ""


def _shell(body: str, n: int = 0) -> str:
    """The n-th server-rendered sheet shell (its markup up to the next shell or the grid)."""
    starts = [m.start() for m in re.finditer(r'<article class="page-sheet', body)]
    end = starts[n + 1] if n + 1 < len(starts) else body.index('<div class="page-grid"')
    return body[starts[n] : end]


# ---------------------------------------------------------- dashboard: chrome and static shells (§2, §3, §11)


def test_dashboard_toolbar_static_shells_and_grid_cards(editor_client):
    book, pages = _book(
        [
            (Page.Status.UPLOADED, "none"),
            (Page.Status.LAYOUT_DONE, "provisional"),
            (Page.Status.OCR_DONE, "final"),
        ]
    )
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    # sticky toolbar: segmented «صفحات | شبكة», five filter chips with live counts, jump field with G, follow
    # toggle
    toggle = re.search(
        r'<div class="segmented" role="group" aria-label="طريقة عرض الصفحات" data-view-toggle>(.*?)</div>',
        body,
        re.S,
    )
    assert toggle and "صفحات" in toggle.group(1) and "شبكة" in toggle.group(1)
    assert "@click=\"setView('sheets')\"" in body and "@click=\"setView('grid')\"" in body
    chips = re.search(r'<div class="bk-chips"[^>]*data-filter-chips>(.*?)</div>', body, re.S).group(1)
    assert chips.count('class="bk-chip"') == 5
    for name in ("all", "processing", "review", "attention", "reviewed"):
        assert f"setFilter('{name}')" in chips and f'x-text="counts.{name}"' in chips
    assert 'inputmode="numeric" dir="ltr" placeholder="إلى صفحة…" aria-label="الانتقال إلى صفحة"' in body
    assert '<kbd class="kbd" aria-hidden="true">G</kbd>' in body and 'class="btn-icon bk-follow"' in body
    assert 'class="bk-toolbar"' in body and 'class="bk-summary"' in body and 'class="bk-attention"' in body
    # summary strip: the five stage counters replace the cards
    assert "stage-card" not in body and "attention-card" not in body
    for key in ("uploaded", "preprocessed", "layout_done", "ocr_done", "error"):
        assert f"count('{key}')" in body
    # one static shell per page (+ the inert clone template), ids and data attributes from the first paint
    assert body.count('<article class="page-sheet') == 4 and '<template id="sheet-shell">' in body
    assert (
        f'id="sheet-2" data-page-id="{pages[1].pk}" data-number="2" '
        'data-status="layout_done" data-text="provisional"' in body
    )
    assert 'style="--sheet-ar: 1100 / 1600"' in body
    shell = _shell(body, 1)
    for needle in (
        "صفحة <bdi",
        "الرقم المطبوع",
        "غير مؤكَّدة",
        "bk-reviewed",
        'class="badge bk-provisional">نص مبدئي',
        ">مراجعة</a>",
        ">نسخ</span>",
        "sheet-retry",
        '<div class="sheet-body"></div>',
    ):
        assert needle in shell, needle
    assert "x-" not in shell and ":class" not in shell  # static: no per-shell Alpine bindings
    assert (
        'sheet-review" href="{}" hidden>'.format(_optional("review:page", book.pk, 2)) in shell
    )  # provisional: no review action
    assert 'sheet-review" href="{}">'.format(_optional("review:page", book.pk, 3)) in _shell(body, 2)
    # the body template: scan with line boxes, the mirrored text pane with its designed states
    tpl = body[
        body.index('<template id="sheet-body">') : body.index(
            "</template>", body.index('<template id="sheet-body">')
        )
    ]
    for needle in (
        "sheet-img-scan",
        "sheet-img-clean",
        '<div class="sheet-lines" dir="ltr"',
        '<div class="sheet-fac" dir="rtl"',
        "fac-state-empty",
        "fac-state-excluded",
        "fac-state-error",
        "fac-retry",
        "fac-restore",
    ):
        assert needle in tpl, needle
    # grid: static tiles with the muted marks, no text; the guarded URLs and the live region
    assert body.count('<div class="page-tile') == 4 and '<template id="tile-shell">' in body
    tile = body[
        body.index('<div class="page-tile') : body.index("</form>", body.index('<div class="page-tile'))
    ]
    assert (
        "tile-stage-fill" in tile
        and "bk-mark-check" in tile
        and "bk-mark-count" in tile
        and "sheet-fac" not in tile
    )
    assert 'class="tile-number num">1</span>' in tile and "page-tile-status" not in tile
    assert f"sheetsUrl: '{_optional('api:book_sheets', book.pk)}'" in body and "reviewNextUrl: '" in body
    assert '<p class="sr-only" aria-live="polite" x-text="liveMessage"></p>' in body
    assert 'class="toast bk-toast"' in body and "ابدأ المراجعة" in body
    # D33: «صفحات» is a book viewer: one stage with turn buttons and a filmstrip, no long scroll
    assert 'class="bk-viewer" data-viewer' in body and 'class="bk-stage" x-ref="stage"' in body
    assert '@wheel="onStageWheel($event)"' in body and 'class="bk-film" x-ref="film"' in body
    assert 'class="bk-turn bk-turn-prev"' in body and 'class="bk-turn bk-turn-next"' in body
    assert 'aria-label="الصفحة السابقة"' in body and 'aria-label="الصفحة التالية"' in body
    assert "showPage(f.number, { manual: true })" in body and 'class="bk-counter"' in body
    assert "'is-viewer': view === 'sheets' && nPages > 0" in body and "filmstripUrl: '" in body
    assert 'class="bk-pos"' not in body  # the scroll position chip went with the long scroll


def test_dashboard_top_bar_primary_candidates_menu_and_banners(editor_client):
    book, pages = _book([(Page.Status.OCR_DONE, "final"), (Page.Status.REVIEWED, "final")])
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    # every primary candidate is rendered role-gated and toggled from the poll (one visible per state)
    for state in ("start", "guides", "review", "copy"):
        assert f"x-show=\"d.primary === '{state}'\"" in body, state
    assert "data-review-next" in body and "الصفحة التالية للمراجعة" in body
    assert "x-text=\"d.status === 'error' ? 'إعادة بدء المعالجة' : 'بدء المعالجة'\"" in body
    # status chip, progress bar, poll pill, «⋯» menu with copy / guides / rerun forms / all books
    assert 'class="bk-status"' in body and 'aria-label="نسبة إتمام المعالجة"' in body
    assert "تعذّر التحديث · إعادة المحاولة" in body and "انتهت الجلسة · تسجيل الدخول" in body
    menu = body[
        body.index('class="menu menu-popover bk-menu"') : body.index(
            "</template>", body.index('class="menu menu-popover bk-menu"')
        )
    ]
    assert (
        menu.count('name="stage"') == 5
        and "إعادة التشغيل من مرحلة" in menu
        and "ضبط الأدلة" in menu
        and "كل الكتب" in menu
    )
    assert "نسخ نص الكتاب" in menu and "x-show=\"d.primary !== 'copy'\"" in menu
    # banners follow the poll, the review summary lives in the strip
    assert (
        "x-show=\"status === 'error'\"" in body
        and "x-show=\"status === 'needs_guides'\"" in body
        and 'x-text="errorHeadline' in body
    )
    assert (
        "مُراجَعة <bdi" in body
        and 'x-text="reviewSummary.reviewed"' in body
        and "كلمة غير مؤكَّدة" in body
        and "رُوجعت كل الصفحات" in body
    )
    # a proofreader sees no start form, no rerun forms, no retry / restore forms in the shells
    client = editor_client
    client.force_login(_user("reader", "proofreader"))
    body = client.get(reverse("books:detail", args=[book.pk])).content.decode()
    assert "d.primary === 'start'" not in body and 'name="stage"' not in body and "fac-restore" not in body


def test_dashboard_stays_light_with_800_sheet_placeholders(editor_client):
    book = Book.objects.create(title="كتاب ضخم", status=Book.Status.OCR)
    Page.objects.bulk_create(
        [
            Page.objects.model(
                book=book,
                number=i,
                source_index=i - 1,
                status=Page.Status.OCR_DONE,
                text_state="final",
                width=1000,
                height=1500,
            )
            for i in range(1, 801)
        ]
    )
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    assert body.count('<article class="page-sheet') == 801  # 800 shells + the clone template
    shell = _shell(body, 0)
    # off-screen shells carry no images, no text and no Alpine bindings: the body mounts from the template
    assert "sheet-img" not in shell and "sheet-fac" not in shell and "x-" not in shell
    assert shell.count("<") < 80
    css = CSS.read_text(encoding="utf-8")
    assert re.search(r"\.page-sheet\{[^}]*content-visibility:auto", css)
    assert re.search(r"\.page-sheet\{[^}]*contain-intrinsic-size:auto var\(--sheet-h,640px\)", css)


# ---------------------------------------------------------------- text panel (§2) and page detail


def test_text_panel_uses_the_decode_host_and_keeps_the_final_markup():
    book, pages = _book([(Page.Status.LAYOUT_DONE, "provisional")])
    html = render_to_string("ocr/_text_panel.html", {"page": pages[0], "book": book, "role": "editor"})
    assert 'x-ref="decode"' in html and "data-decode-host" in html and 'x-show="showDecode"' in html
    assert 'x-text="provisional"' not in html  # never the plain Tesseract text (D23)
    assert 'aria-live="polite" x-text="liveText"' in html
    assert "x-show=\"view === 'final'\"" in html and "'tok-low'" in html and "tok-pop" in html
    assert html.count("x-transition.opacity.duration.200ms") >= 3 and "spinner" not in html


def test_page_detail_offers_the_review_entry_point_when_the_route_exists(editor_client):
    book, pages = _book([(Page.Status.OCR_DONE, "final")])
    body = editor_client.get(reverse("books:page_detail", args=[book.pk, 1])).content.decode()
    assert "لوحة الكتاب" in body
    url = _optional("review:page", book.pk, 1)
    if url:
        assert "مراجعة الصفحة" in body and f'href="{url}"' in body and body.count("btn-primary") == 1
    else:
        assert "مراجعة الصفحة" not in body


def test_compiled_css_has_the_mirror_pane_effects_and_their_static_fallbacks():
    css = CSS.read_text(encoding="utf-8")
    # the mirror: same box, flex column with space-between, justified text, font from the page (§4)
    assert re.search(
        r"\.sheet-fac\{[^}]*aspect-ratio:var\(--sheet-ar,7 / 10\)[^}]*container-type:inline-size", css
    )
    assert re.search(
        r"\.fac-block\{[^}]*font-size:clamp\(9px, calc\(var\(--fs\) \* 100cqw\), 28px\)"
        r"[^}]*justify-content:space-between",
        css,
    )
    assert re.search(r"\.fac-text\{[^}]*text-align:justify;text-align-last:start", css)
    assert re.search(
        r"\.fac-line\[data-boxed\]\{margin-left:calc\(\(var\(--lx0\) - var\(--x0\)\) \* 100cqw\)", css
    )
    assert re.search(r"\.sheet-body\{[^}]*grid-template-columns:minmax\(0,1fr\) minmax\(0,1fr\)", css)
    # the generation: gray provisional layer, veil as opacity, the lit line's sheen, the scan band (§5)
    assert re.search(r"\.fac-layer\[data-phase=\"?provisional\"?\][^{]*\{color:var\(--color-text-3\)\}", css)
    assert re.search(r"\.fac-text \.tok\.is-veiled\{opacity:\.45", css)
    assert re.search(r"\.fac-line\.is-lit \.fac-text\{[^}]*background-clip:text[^}]*bk-sheen", css)
    assert "@keyframes bk-sheen{0%{background-position-x:0%}to{background-position-x:100%}}" in css
    assert (
        "@keyframes bk-shimmer{" in css
        and "@keyframes bk-line-in{" in css
        and "@keyframes bk-stamp-in{" in css
    )
    assert re.search(r"\.sheet-line\.is-lit\{opacity:1;background:var\(--color-accent-soft\)", css)
    assert re.search(r"\.sheet-line\.is-hot:before\{content:attr\(data-n\)", css)
    assert (
        re.search(r"\.fac-layer\[data-phase=\"?final\"?\] \.tok\.is-landed\{[^}]*decode-land", css)
        and "@keyframes decode-land" in css
    )
    assert re.search(r"\.scan-sweep:after\{[^}]*height:14%", css)
    assert "@" not in re.search(r"\.fac-bar\{[^}]*\}", css).group(0)[:0] and "bk-shimmer" in re.search(
        r"\.fac-bar\{[^}]*\}", css
    ).group(0)
    # no rv- class is referenced by the dashboard styles (review.css stays untouched)
    theatre = (ROOT / "static" / "src" / "components" / "theatre.css").read_text(encoding="utf-8")
    assert not re.search(r"\.rv-", theatre)
    # reduced motion: sweep, sheen, shimmer, line entrance and the landing wash are all off
    reduced = css[css.index("prefers-reduced-motion:reduce") :]
    assert re.search(r"[^{}]*\.scan-sweep:after[^{}]*\{display:none\}", reduced)
    assert re.search(r"[^{}]*\.fac-bar[^{}]*\{animation:none\}", reduced)
    assert re.search(r"[^{}]*\.fac-line\.is-lit \.fac-text\{[^}]*animation:none", reduced)
    assert re.search(r"[^{}]*\.decode-w\.is-landed[^{}]*\{animation:none\}", reduced)


# ---------------------------------------------------------------- Node: decode.js, books.js, ocr.js

HARNESS = r"""
// tiny DOM: enough for decode.js (text nodes, spans, classList, custom properties) and the Alpine components
class ClassList { constructor(){ this.s = new Set(); } add(...c){ c.forEach(x => this.s.add(x)); } remove(...c){ c.forEach(x => this.s.delete(x)); }
  toggle(c, f){ if (f === undefined) f = !this.s.has(c); f ? this.s.add(c) : this.s.delete(c); return f; } contains(c){ return this.s.has(c); } toString(){ return [...this.s].join(' '); } }
class Node { constructor(){ this.childNodes = []; this.parentNode = null; this.isConnected = true; }
  appendChild(n){ if (n.parentNode) n.parentNode.removeChild(n); n.parentNode = this; this.childNodes.push(n); return n; }
  insertBefore(n, ref){ if (n.parentNode) n.parentNode.removeChild(n); const i = ref ? this.childNodes.indexOf(ref) : -1; n.parentNode = this; if (i < 0) this.childNodes.push(n); else this.childNodes.splice(i, 0, n); return n; }
  removeChild(n){ const i = this.childNodes.indexOf(n); if (i >= 0) this.childNodes.splice(i, 1); n.parentNode = null; return n; }
  remove(){ if (this.parentNode) this.parentNode.removeChild(this); } }
class Text extends Node { constructor(d){ super(); this.nodeValue = d; } get textContent(){ return this.nodeValue; } }
class Element extends Node {
  constructor(tag){ super(); this.tagName = tag; this.classList = new ClassList(); this.attrs = {}; this.style = { setProperty: (k, v) => { this.style[k] = v; }, getPropertyValue: (k) => this.style[k] || '' }; this.dataset = {}; this.hidden = false; }
  set className(v){ this.classList = new ClassList(); v.split(/\s+/).filter(Boolean).forEach(c => this.classList.add(c)); } get className(){ return this.classList.toString(); }
  setAttribute(k, v){ this.attrs[k] = String(v); } getAttribute(k){ return k in this.attrs ? this.attrs[k] : null; }
  set textContent(v){ this.childNodes.forEach(n => { n.parentNode = null; }); this.childNodes = v ? [new Text(String(v))] : []; }
  get textContent(){ return this.childNodes.map(n => n.textContent).join(''); }
  get children(){ return this.childNodes.filter(n => n instanceof Element); }
  querySelectorAll(){ return []; } querySelector(){ return null; }
}
const inits = []; const reg = {}; const timers = []; const toasts = [];
globalThis.window = globalThis;
globalThis.document = { hidden: false, createElement: (t) => new Element(t), createTextNode: (d) => new Text(d),
  addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); }, getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: () => null };
globalThis.CustomEvent = class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } };
globalThis.dispatchEvent = () => true;
globalThis.location = { hash: '', assign: () => {}, reload: () => { out.reloaded = true; } };
globalThis.addEventListener = () => {};
let NOW = 1000; globalThis.performance = { now: () => NOW };
globalThis.requestAnimationFrame = () => 1; // the tests drive NassakhDecode.step(t) by hand
globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
globalThis.clearTimeout = () => {};
const store = {}; globalThis.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
globalThis.sessionStorage = { getItem: () => null, setItem: () => {} };
const fs = require('fs');
for (const f of process.argv.slice(2)) eval(fs.readFileSync(f, 'utf8'));
inits.forEach((fn) => fn());
window.Nassakh.toast = (m) => toasts.push(m);
const D = NassakhDecode;
D.measure = (text) => Array.from(text).length * 50; // a deterministic width at 100 px
const out = {};
const run = (el, ms, every = 41) => { for (let t = NOW; t <= NOW + ms; t += every) D.step(t); NOW += ms; };
const linesOf = (el) => el.children.map((row) => ({ region: row.getAttribute('data-region') || '', words: row.children.map((w) => ({ t: w.textContent, cls: w.className })) }));
const FAMILIES = ['بتثني', 'جحخ', 'دذ', 'رز', 'سش', 'صض', 'طظ', 'عغ', 'فق', 'هة', 'وؤ', 'اأإآ', 'كل'];
const familyOf = (ch) => FAMILIES.find((f) => f.includes(ch)) || 'بنت';
const BASE = /[ء-غف-يٮ-ٯٱ-ۓۺ-ۼ]/;
// D28 invariants of one shown string against its real word: exact, or 1–3 same-family base letters differ,
// never the first letter, never a digit or a mark, same length, no ASCII.
function veilOk(real, shown) {
  if (real === shown) return true;
  const r = Array.from(real), s = Array.from(shown);
  if (r.length !== s.length) return 'length';
  if (/[\x00-\x7f]/.test(shown)) return 'ascii';
  const firstBase = r.findIndex((c) => BASE.test(c));
  const diffs = r.map((c, i) => (c === s[i] ? -1 : i)).filter((i) => i >= 0);
  if (diffs.length < 1 || diffs.length > 3) return 'count ' + diffs.length;
  for (const i of diffs) {
    if (i === firstBase) return 'first';
    if (!BASE.test(r[i])) return 'not-base';
    if (!familyOf(r[i]).includes(s[i]) || s[i] === r[i]) return 'family';
  }
  return true;
}

// --- §5.2 provisional (text panel path): dwell on the exact word, veil briefly, return
const el = new Element('div');
D.attach(el, { mode: 'provisional', text: 'قال الأمير في سنة 1966\nوَفِي الشهر الثاني' });
const words = D.inspect(el);
out.words = words.map((w) => w.real);
out.lines = el.children.length;
const everExact = words.map(() => false), everVeiled = words.map(() => false);
let veilBad = null, maxVeiled = 0, changed = 0, veiledClassOk = true;
let prev = D.inspect(el).map((w) => w.shown);
for (let i = 0; i < 20000 / 41; i += 1) {
  NOW += 41; D.step(NOW);
  const snap = D.inspect(el);
  let nv = 0;
  snap.forEach((w, k) => {
    const ok = veilOk(w.real, w.shown);
    if (ok !== true && !veilBad) veilBad = [w.real, w.shown, ok];
    if (w.shown === w.real) everExact[k] = true; else everVeiled[k] = true;
    if (w.veiled) nv += 1;
    if (w.veiled !== el.children[w.line].children.filter((s) => s.textContent === w.shown).some((s) => s.classList.contains('is-veiled')) && w.veiled) veiledClassOk = false;
    if (w.shown !== prev[k]) changed += 1;
  });
  maxVeiled = Math.max(maxVeiled, nv);
  prev = snap.map((w) => w.shown);
}
const veilable = (real) => Array.from(real).filter((c) => BASE.test(c)).length >= 2;
out.veilBad = veilBad; out.maxVeiled = maxVeiled; out.changed = changed > 0; out.veiledClassOk = veiledClassOk;
out.everExact = everExact.every(Boolean);
out.everVeiled = words.every((w, k) => !veilable(w.real) || everVeiled[k]);
out.digitsStill = words.filter((w) => !veilable(w.real)).every((w, k) => !everVeiled[words.indexOf(w)]);
out.decodingClass = el.classList.contains('is-decoding') && el.getAttribute('aria-hidden') === 'true';
out.tokenNodes = el.children[0].children.every((w) => w.childNodes.length === 1 && w.childNodes[0] instanceof Text);
out.spaces = el.children[0].childNodes.filter((n) => n instanceof Text && n.nodeValue === ' ').length;

// --- noise mode: Arabic-only pseudo-words sized to the page, low-opacity class
const noise = new Element('div');
D.attach(noise, { mode: 'noise', lines: 7 });
run(noise, 300);
out.noiseLines = noise.children.length;
out.noiseClass = noise.classList.contains('is-noise');
out.noiseArabic = D.inspect(noise).every((w) => /^[ء-ي]+$/.test(w.shown) && w.nReal === 0);

// --- resolve wave: reading order, final tokens, tok-low only on unresolved low-confidence words
let done = 0;
const finalLines = [
  { region_kind: 'body', tokens: [{ t: 'قال', conf: 'high' }, { t: 'الأمير', conf: 'low' }, { t: 'في', conf: 'high' }] },
  { region_kind: 'footnote', tokens: [{ t: 'حاشية', conf: 'low', res: 'primary' }, { t: '12', conf: 'low', digit: true }] },
];
D.resolve(el, { lines: finalLines, onDone: () => { done += 1; } });
out.resolvingClass = el.classList.contains('is-resolving');
NOW += 41; D.step(NOW);
const mid = () => D.inspect(el).map((w) => w.landed);
let landedPrefix = true;
for (let i = 0; i < 12; i += 1) { // 5 words: the wave lasts 600 ms, onDone follows 300 ms later
  NOW += 41; D.step(NOW);
  const l = mid(); const firstNo = l.indexOf(false);
  if (firstNo !== -1 && l.slice(firstNo).some(Boolean)) landedPrefix = false; // words land right-to-left, line by line
}
out.landedPrefix = landedPrefix;
out.doneEarly = done;
run(el, 3000);
out.done = done;
out.final = linesOf(el);
out.resolvedClass = el.classList.contains('is-resolved') && el.getAttribute('aria-hidden') === 'false';

// --- an unmounted element is forgotten by the loop
const gone = new Element('div');
D.attach(gone, { mode: 'provisional', text: 'كلمة أخرى' });
const before = D.size; gone.isConnected = false; D.step(NOW + 41);
out.forgotten = [before, D.size];

// --- reduced motion: static faded text, instant resolve
D.reducedMotion = true;
const rm = new Element('div');
D.attach(rm, { mode: 'provisional', text: 'نص ثابت هنا' });
out.rmStatic = rm.classList.contains('is-static') && D.inspect(rm).every((w) => w.shown === w.real);
let rmDone = false;
D.resolve(rm, { lines: finalLines, onDone: () => { rmDone = true; } });
out.rmResolve = rmDone && linesOf(rm)[0].words.map((w) => w.t).join(' ');
D.reducedMotion = false;

// --- §4 layout(): pure geometry
const W = 1000, H = 1500, HW = H / W;
const long = 'كلمة كلمة كلمة كلمة كلمة كلمة كلمة كلمة'; // 8 words → r ≈ 1.02 at the box font: justify
const boxLine = (k, text, kind = 'body') => ({ region_kind: kind, bbox: [0.15, 0.1 + 0.05 * k, 0.85, 0.13 + 0.05 * k], words: text.split(' ') });
const regular = { width: W, height: H, text_state: 'provisional', line_boxes: [], provisional_lines: [...Array.from({ length: 9 }, (_, k) => boxLine(k, long)), boxLine(9, 'كلمة كلمة كلمة')] };
const L1 = D.layout(regular);
const g1 = L1.groups[0];
out.layRegular = { mode: L1.mode, groups: L1.groups.length, kind: g1.kind, source: g1.source, block: g1.block.map((v) => +v.toFixed(4)), fs: +g1.fsCw.toFixed(4), lh: +g1.lhCw.toFixed(4), few: g1.few,
  fits: g1.lines.map((l) => l.fit), rs: g1.lines.map((l) => l.r), ids: g1.lines.map((l) => l.i), lx: [g1.lines[0].lx0, g1.lines[0].lx1], rule: L1.rule };
out.layRegularExpect = { fs: +(0.78 * 0.03 * HW).toFixed(4), lh: +(1.24 * 0.03 * HW).toFixed(4), y0: +(0.1 - 0.12 * 0.03).toFixed(4), y1: +(0.58 + 0.12 * 0.03).toFixed(4) };
const plain = { width: W, height: H, text_state: 'provisional', provisional_text: Array.from({ length: 12 }, () => long).join('\n') };
const L2 = D.layout(plain); const g2 = L2.groups[0];
out.layPlain = { source: g2.source, block: g2.block.map((v) => +v.toFixed(4)), fs: +g2.fsCw.toFixed(4), few: g2.few, n: g2.lines.length, boxed: g2.lines.some((l) => l.box), lx: [g2.lines[3].lx0, g2.lines[3].lx1] };
const pitch = 0.8 / 12, hgPitch = 0.62 * pitch;
out.layPlainExpect = { fs: +Math.max(76 / 1950, 0.75 * hgPitch * HW).toFixed(4), y0: +(0.1 - 0.12 * hgPitch).toFixed(4) };
const title = { width: W, height: H, text_state: 'provisional', provisional_text: 'عنوان الكتاب\nاسم المؤلف' };
const L3 = D.layout(title); const g3 = L3.groups[0];
out.layTitle = { few: g3.few, fs: +g3.fsCw.toFixed(4), n: g3.lines.length, fits: g3.lines.map((l) => l.fit) };
out.layTitleExpect = +(0.016 * HW).toFixed(4);
const withNotes = { width: W, height: H, text_state: 'provisional', footnote_y: 0.8, median_line_h: 0.03,
  provisional_lines: [boxLine(0, long), boxLine(1, long), { region_kind: 'footnote', bbox: [0.15, 0.82, 0.85, 0.845], words: long.split(' ') }, { region_kind: 'footnote', bbox: [0.15, 0.86, 0.85, 0.885], words: ['حاشية', 'ثانية'] }] };
const L4 = D.layout(withNotes);
out.layNotes = { groups: L4.groups.map((g) => g.kind), rule: L4.rule, noteY0: +L4.groups[1].block[1].toFixed(3), noteFsCapped: L4.groups[1].fsCw <= 0.9 * L4.groups[0].fsCw + 1e-9, ids: L4.groups.flatMap((g) => g.lines.map((l) => l.i)), noteKinds: L4.groups[1].lines.map((l) => l.kind) };
const finalNoBox = { width: W, height: H, text_state: 'final', line_boxes: [], lines: Array.from({ length: 6 }, () => ({ region_kind: 'body', bbox: null, tokens: long.split(' ').map((t) => ({ t, conf: 'high' })) })) };
const L5 = D.layout(finalNoBox); const g5 = L5.groups[0];
out.layFinalNoBox = { mode: L5.mode, source: g5.source, boxed: g5.lines.some((l) => l.box), spans: g5.lines.every((l) => l.lx0 === g5.block[0] && l.lx1 === g5.block[2]), ids: g5.lines.map((l) => l.i), tokens: g5.lines[0].tokens.length };
const skeletonBoxes = { width: W, height: H, text_state: 'none', status: 'preprocessed', line_boxes: Array.from({ length: 5 }, (_, k) => [0.2, 0.2 + 0.06 * k, 0.8, 0.23 + 0.06 * k]) };
const L6 = D.layout(skeletonBoxes);
out.laySkeleton = { mode: L6.mode, n: L6.groups[0].lines.length, boxed: L6.groups[0].lines.every((l) => l.box), source: L6.groups[0].source };
const L7 = D.layout({ width: W, height: H, text_state: 'none', status: 'uploaded' });
out.layBare = { mode: L7.mode, n: L7.groups[0].lines.length, bars: L7.groups[0].lines.map((l) => l.bar).slice(0, 4), block: L7.groups[0].block.map((v) => +v.toFixed(3)) };
out.layEmpty = D.layout({ width: W, height: H, text_state: 'final', lines: [] }).mode;
const centred = { width: W, height: H, text_state: 'final', lines: [
  { region_kind: 'heading', bbox: [0.4, 0.1, 0.6, 0.13], tokens: [{ t: 'باب' }] },
  ...Array.from({ length: 4 }, (_, k) => ({ region_kind: 'body', bbox: [0.15, 0.2 + 0.05 * k, 0.85, 0.23 + 0.05 * k], tokens: long.split(' ').map((t) => ({ t })) })) ] };
out.layCentred = D.layout(centred).groups[0].lines[0].fit;

// --- §4.7 (D30): one size per group, shared paragraph edges, lines fitted by word spacing, book line height
const toks = (text) => text.split(' ').map((t) => ({ t }));
const jitter = [0.150, 0.152, 0.149, 0.153, 0.151, 0.150];
const para = { width: W, height: H, text_state: 'final', median_line_h: 0.03, lines: [
  { region_kind: 'body', bbox: [0.4, 0.06, 0.6, 0.09], tokens: toks('باب') }, // 0 centred heading
  { region_kind: 'body', bbox: [0.151, 0.10, 0.82, 0.13], tokens: toks(long) }, // 1 indented first line
  ...jitter.map((x0, k) => ({ region_kind: 'body', bbox: [x0, 0.15 + 0.05 * k, 0.85 - 0.002 * (k % 2), 0.18 + 0.05 * k], tokens: toks(long) })), // 2..7 full, jittered
  { region_kind: 'body', bbox: [0.55, 0.45, 0.851, 0.48], tokens: toks('كلمة كلمة') }, // 8 the paragraph's last line
  { region_kind: 'body', bbox: [0.15, 0.50, 0.85, 0.53], tokens: toks(long + ' ' + long) }, // 9 two lines merged: far too long
  { region_kind: 'body', bbox: [0.15, 0.55, 0.85, 0.58], tokens: toks(long + ' كلمة') }, // 10 one word too many
] };
const gp = D.layout(para).groups[0];
out.layPara = { fs: gp.fsCw, hasScale: gp.lines.some((l) => 'scale' in l),
  lines: gp.lines.map((l) => ({ shape: l.shape, fit: l.fit, lx0: +l.lx0.toFixed(4), lx1: +l.lx1.toFixed(4), ws: l.ws, sx: l.sx, r: l.r })) };
out.layParaHeightFs = 0.78 * 0.03 * HW;
// D31: a paragraph's first line stays flush left with its indent; little text → end-aligned; long text uses the indent
const indentPage = { width: W, height: H, text_state: 'final', median_line_h: 0.03, lines: [
  { region_kind: 'body', bbox: [0.15, 0.10, 0.83, 0.13], tokens: toks('كلمة كلمة') },
  ...Array.from({ length: 5 }, (_, k) => ({ region_kind: 'body', bbox: [0.15, 0.15 + 0.05 * k, 0.85, 0.18 + 0.05 * k], tokens: toks(long) })),
  { region_kind: 'body', bbox: [0.15, 0.45, 0.80, 0.48], tokens: toks(long + ' كلمة') },
] };
out.layIndent = D.layout(indentPage).groups[0].lines.map((l) => ({ shape: l.shape, fit: l.fit, lx0: +l.lx0.toFixed(4), lx1: +l.lx1.toFixed(4) }));
out.layParaSpace = (50 * gp.fsCw) / 100; // one space at the group size (the harness measures 50 per char)
const six = 'كلمة كلمة كلمة كلمة كلمة كلمة';
const bookBase = { width: W, height: H, text_state: 'final', median_line_h: 0.03,
  lines: Array.from({ length: 6 }, (_, k) => ({ region_kind: 'body', bbox: [0.15, 0.1 + 0.05 * k, 0.85, 0.13 + 0.05 * k], tokens: toks(six) })) };
out.layBook = { own: +D.layout(bookBase).groups[0].fsCw.toFixed(5), near: +D.layout({ ...bookBase, book_line_h_px: 49.5 }).groups[0].fsCw.toFixed(5),
  far: +D.layout({ ...bookBase, book_line_h_px: 70 }).groups[0].fsCw.toFixed(5) };
// rendered: word spacing and condense on the line element, never a per-line font size
const findAll = (node, cls, acc = []) => { (node.children || []).forEach((c) => { if (c.classList && c.classList.contains(cls)) acc.push(c); findAll(c, cls, acc); }); return acc; };
const hostP = new Element('div');
D.sheet(hostP, { scan: new Element('div'), figure: new Element('figure') }).update({ page: { ...para, id: 99, number: 9, status: 'ocr_done' }, active: false });
const facLines = findAll(hostP, 'fac-line');
out.layParaDom = { n: facLines.length, fits: facLines.map((e) => e.getAttribute('data-fit')), anyFit: facLines.some((e) => e.style['--fit'] !== undefined),
  tightWs: facLines[10] && facLines[10].style['--ws'], overSx: facLines[9] && facLines[9].style['--sx'], plainWs: facLines[3] && facLines[3].style['--ws'] };

// --- §13.1 sheet handle: skeleton → provisional (cursor, sheen, exact under the band) → final wave; hot both sides
const host = new Element('div'), scan = new Element('div'), figure = new Element('figure');
const h = D.sheet(host, { scan, figure });
const skel = { id: 1, number: 1, width: W, height: H, status: 'preprocessed', text_state: 'none', line_boxes: skeletonBoxes.line_boxes };
out.hSkel = { mode: h.update({ page: skel, active: true }), phase: host.children[0].getAttribute('data-phase'), bars: host.children[0].children[0].children.length, boxes: scan.children.length, dataMode: host.getAttribute('data-mode') };
NOW += 41; D.step(NOW);
out.hSkelCursor = { cursor: h.cursor, scanLit: scan.children[0].classList.contains('is-lit'), lineLit: host.children[0].children[0].children[0].classList.contains('is-lit') };
const provWords = Array.from({ length: 12 }, (_, k) => `س${k}طر كلمة أخرى ثالثة رابعة خامسة`.replace(/\d/g, ''));
const prov = { ...skel, status: 'layout_done', text_state: 'provisional', line_boxes: [], provisional_lines: provWords.map((t, k) => ({ region_kind: 'body', bbox: [0.15, 0.1 + 0.05 * k, 0.85, 0.13 + 0.05 * k], words: t.split(' ') })) };
out.hProv = { mode: h.update({ page: prov, active: true }), phase: host.children[0].getAttribute('data-phase'), layers: host.children.length, lines: h.lines, boxes: scan.children.length, toks: host.children[0].children[0].children[0].children[0].children.length };
let litOk = true, visited = new Set(), maxSheetVeiled = 0, sheetBad = null; const sheetExact = new Set(), sheetVeiled = new Set();
for (let i = 0; i < 20000 / 41; i += 1) {
  NOW += 41; D.step(NOW);
  const k = h.cursor; if (k >= 0) visited.add(k);
  const snap = D.inspect(host);
  let nv = 0;
  snap.forEach((w, j) => {
    const ok = veilOk(w.real, w.shown); if (ok !== true && !sheetBad) sheetBad = [w.real, w.shown, ok];
    if (w.veiled) nv += 1;
    if (w.shown === w.real) sheetExact.add(j); else sheetVeiled.add(j);
    if (k >= 0 && w.line === k && w.shown !== w.real) litOk = false; // the lit line's words are exact
  });
  if (k >= 0) { const lineEl = h.lineEl(k); if (!lineEl.classList.contains('is-lit') || !scan.children[k].classList.contains('is-lit')) litOk = false; }
  maxSheetVeiled = Math.max(maxSheetVeiled, nv);
}
const nWords = D.inspect(host).length;
out.hCycle = { litOk, visited: visited.size, words: nWords, maxVeiled: maxSheetVeiled, bad: sheetBad, allExact: sheetExact.size === nWords, allVeiled: sheetVeiled.size === nWords };
h.hot(2);
out.hHot = [h.hotIndex, scan.children[2].classList.contains('is-hot'), h.lineEl(2).classList.contains('is-hot')];
h.hot(-1);
out.hHotOff = [h.hotIndex, scan.children[2].classList.contains('is-hot')];
const finalPage = { ...prov, status: 'ocr_done', text_state: 'final', provisional_lines: [], lines: provWords.map((t, k) => ({ id: k + 1, region_kind: 'body', bbox: [0.15, 0.1 + 0.05 * k, 0.85, 0.13 + 0.05 * k], tokens: t.split(' ').map((w, j) => ({ t: w, conf: j === 1 ? 'low' : 'high' })) })) };
out.hFinal = { mode: h.update({ page: finalPage, active: true }), layers: host.children.length, phases: host.children.map((c) => c.getAttribute('data-phase')), leaving: host.children[1].classList.contains('is-leaving') };
let wavePrefix = true, front = [];
for (let i = 0; i < 20; i += 1) {
  NOW += 41; D.step(NOW);
  const l = D.inspect(host).map((w) => w.landed); const firstNo = l.indexOf(false);
  if (firstNo !== -1 && l.slice(firstNo).some(Boolean)) wavePrefix = false;
  const lit = scan.children.findIndex((b) => b.classList.contains('is-lit')); if (lit >= 0 && front[front.length - 1] !== lit) front.push(lit);
}
run(host, 3000);
out.hWave = { wavePrefix, frontMonotonic: front.every((v, i) => i === 0 || v > front[i - 1]), frontMoved: front.length > 3, landed: D.inspect(host).every((w) => w.landed), layers: host.children.length, resolved: host.children[0].classList.contains('is-resolved'), aria: host.children[0].getAttribute('aria-hidden'), lit: scan.children.some((b) => b.classList.contains('is-lit')), low: host.children[0].children[0].children[0].children[0].children[1].classList.contains('tok-low'), tabindex: h.lineEl(0).getAttribute('tabindex') };
// designed states and a static (inactive) provisional sheet
const host2 = new Element('div'); const h2 = D.sheet(host2, { scan: new Element('div') });
out.hStatic = { mode: h2.update({ page: prov, active: false }), cls: host2.children[0].classList.contains('is-static'), exact: D.inspect(host2).every((w) => w.shown === w.real) };
out.hResume = h2.update({ page: prov, active: true });
out.hStop = [h2.update({ page: prov, active: false }), host2.children.length];
out.hStates = [h2.update({ page: { ...prov, is_excluded: true } }), h2.update({ page: { ...prov, is_excluded: false, error: 'فشل' } }), h2.update({ page: { ...skel, text_state: 'final', lines: [] } }), h2.update({ page: { ...skel, text_state: 'final' } })];
const host3 = new Element('div'); const h3 = D.sheet(host3, {});
out.hLate = { mode: h3.update({ page: finalPage, active: false }), resolved: host3.children[0].classList.contains('is-resolved'), landed: D.inspect(host3).every((w) => w.landed) };
D.reducedMotion = true;
const host4 = new Element('div'); const h4 = D.sheet(host4, { scan: new Element('div') });
out.hReduced = [h4.update({ page: prov, active: true }), h4.update({ page: finalPage, active: true }), host4.children.length, host4.children[0].classList.contains('is-resolved')];
D.reducedMotion = false;
h.destroy(); h2.destroy(); h3.destroy(); h4.destroy();

// --- §13.2 books.js: compact merge, changed set, filters, jump, primary per state, no reload, follow target
const pagesCfg = [
  { id: 1, number: 1, status: 'ocr_done', status_label: 'تم التعرّف', dot: 'dot-success', text_state: 'final', n_unresolved: 4, url: '/books/1/pages/1/', review_url: '/books/1/review/1/', width: 700, height: 1000 },
  { id: 2, number: 2, status: 'reviewed', status_label: 'مُراجَعة', dot: 'dot-success', text_state: 'final', is_reviewed: true, url: '/books/1/pages/2/', width: 800, height: 1000 },
  { id: 3, number: 3, status: 'uploaded', status_label: 'مرفوعة', dot: 'dot-neutral', text_state: 'none', is_excluded: true, url: '/books/1/pages/3/' },
];
const mk = (extra = {}) => { const d = reg.bookDashboard({ progressUrl: '/p', sheetsUrl: '/api/books/1/sheets/', reviewNextUrl: '/books/1/review/next/', bookUrl: '/books/1/', bookTextUrl: '/api/books/1/text/', canEdit: true, bookId: 1, active: true, status: 'ocr',
  stages: [{ key: 'ocr_done', statuses: ['ocr_done', 'reviewed', 'assembled'] }], byStatus: { ocr_done: 1, reviewed: 1 }, pages: pagesCfg, ...extra }); d.$watch = () => {}; d.init(); return d; };
const dash = mk();
out.viewDefault = dash.view; dash.setView('grid'); out.viewStored = store['nassakh.bookView']; dash.setView('sheets');
out.ranges = dash.batchRanges([3, 1, 2, 5, 6, 7, 50, 51, ...Array.from({ length: 45 }, (_, i) => 100 + i)]);
out.counts = { ...dash.counts };
out.review = dash.reviewSummary; out.nextReview = dash.nextReviewUrl; out.primaryReview = dash.primary;
dash.review = { reviewed: 1, total: 2, unresolved_total: 9, next_review_url: '' };
out.reviewFromPoll = dash.reviewSummary.next_review_url;
out.aspect = [dash.aspectOf(dash.tile(2)), dash.aspectOf(dash.tile(3))];
dash.applySheets({ pages: [{ id: 1, number: 1, width: 700, height: 1000, text_state: 'final', status: 'ocr_done', line_boxes: [[0.1, 0.2, 0.9, 0.25]],
  lines: [{ region_kind: 'body', tokens: [{ t: 'قال' }, { t: 'الأمير' }] }, { region_kind: 'footnote', tokens: [{ t: 'حاشية' }] }] }] });
out.sheetText = dash.sheetText(1); out.canCopy = [dash.canCopy(1), dash.canCopy(2)];
out.stage = [dash.stagePercent({ status: 'uploaded' }), dash.stagePercent({ status: 'layout_done' }), dash.stagePercent({ status: 'ocr_done' }), dash.stagePercent({ status: 'error', error: true })];
dash.mountSheet({ dataset: { pageId: '1' } }); // a near sheet (no body template under Node: mounted only)
const fetched = [];
globalThis.fetch = (url) => { fetched.push(url); return Promise.resolve({ ok: true, json: async () => ({ pages: [] }) }); };
dash.apply({ total: 4, percent: 50, flags: 0, status: 'ocr', status_label: 'قيد التعرّف', dot: 'dot-accent', by_status: { reviewed: 2, uploaded: 2 }, active: true, error_headline: '', error_detail: '',
  pages: [{ id: 1, number: 1, status: 'reviewed', text_state: 'final', is_reviewed: true, n_unresolved: 0 }, { id: 2, number: 2, status: 'reviewed', text_state: 'final', is_reviewed: true },
          { id: 3, number: 3, status: 'uploaded', text_state: 'none', is_excluded: true }, { id: 4, number: 4, status: 'uploaded', text_state: 'none' }] });
out.merged = { label: dash.page(1).status_label, dot: dash.page(1).dot, url: dash.page(1).url, review: dash.page(1).review_url, stale: dash.page(1).stale, unresolved: dash.page(1).n_unresolved };
out.added = { url: dash.page(4).url, rerun: dash.page(4).rerun_url, label: dash.page(4).status_label, dot: dash.page(4).dot };
out.live = dash.liveMessage; out.countsAfter = { ...dash.counts };
const flush = timers.filter((t) => t.ms === 60).pop(); flush && flush.fn();
out.refetch = fetched;
// filters
dash.setFilter('reviewed'); out.filterStored = store['nassakh.bookFilter.1']; out.filteredOut1 = dash.filteredOut;
dash.setFilter('attention'); out.filteredOut2 = dash.filteredOut; dash.setFilter('all');
out.matches = ['processing', 'review', 'attention', 'reviewed'].map((f) => { dash.setFilter(f); return [dash.matches(dash.page(4)), dash.matches(dash.page(1)), dash.matches(dash.page(3))]; }); dash.setFilter('all');
// jump
out.jump = [dash.jumpTarget('٢'), dash.jumpTarget(' 4 '), dash.jumpTarget('99'), dash.jumpTarget('abc'), dash.jump('99'), toasts.slice(-1)[0]];
// primary per state
const states = [];
dash.status = 'uploaded'; states.push(dash.primary);
dash.status = 'error'; states.push(dash.primary);
dash.status = 'needs_guides'; states.push(dash.primary);
const dg = mk({ guidesUrl: '/processing/1/guides/', status: 'needs_guides', active: false }); states.push(dg.primary);
dash.status = 'ocr'; dash.review = { reviewed: 1, total: 2, unresolved_total: 0, next_review_url: '/books/1/review/next/' }; states.push(dash.primary);
dash.review = { reviewed: 2, total: 2, unresolved_total: 0, next_review_url: null }; states.push(dash.primary);
dash.canEdit = false; dash.status = 'uploaded'; states.push(dash.primary);
out.primary = states;
out.statusText = [dash.statusText, (dash.active = false, dash.statusText)];
// end of processing: no reload, effects stop, toast with the next review page
const dEnd = mk();
dEnd.apply({ total: 3, percent: 100, flags: 0, status: 'ready_for_review', status_label: 'جاهز للمراجعة', dot: 'dot-success', by_status: {}, active: false, review: { reviewed: 0, total: 2, unresolved_total: 4, next_review_url: '/books/1/review/1/' }, pages: [] });
out.end = { active: dEnd.active, toast: { ...dEnd.doneToast }, reloaded: out.reloaded || false, primary: dEnd.primary, label: dEnd.statusText };
out.follow = dEnd.followTarget([
  { before: { status: 'layout_done', text_state: 'none' }, after: { number: 5, status: 'layout_done', text_state: 'provisional' } },
  { before: { status: 'layout_done', text_state: 'provisional' }, after: { number: 7, status: 'ocr_done', text_state: 'final' } },
  { before: { status: 'uploaded', text_state: 'none' }, after: { number: 9, status: 'preprocessed', text_state: 'none' } },
  { before: { status: 'layout_done', text_state: 'none' }, after: { number: 11, status: 'ocr_done', text_state: 'final', is_excluded: true } },
]);
out.keys = [dash.keyAction({ key: 'g', code: 'KeyG' }, false), dash.keyAction({ key: 'g', code: 'KeyG' }, true), dash.keyAction({ key: 'ArrowLeft' }, false), dash.keyAction({ key: 'Escape' }, true), dash.keyAction({ key: 'n', code: 'KeyN', metaKey: true }, false), dash.keyAction({ key: '2' }, false)];
// auth loss stops polling quietly
const dAuth = mk();
globalThis.fetch = () => Promise.resolve({ ok: false, status: 403, json: async () => ({}) });

// --- ocr.js: the text panel attaches the decode effect and holds the final markup until the wave lands
(async () => {
  await dAuth.poll();
  out.auth = [dAuth.pollState, dAuth.stopped];
  const tphost = new Element('div');
  const tp = reg.textPanel({ statusUrl: '/s', textUrl: '/t', runsUrl: '/r' });
  tp.$refs = { decode: tphost };
  tp.apply({ status: 'layout_done', active: true, text_state: 'provisional', provisional_text: 'نص مبدئي من تسراكت' });
  tp.afterUpdate();
  out.tpAttached = [tp.decodeMode, tphost.classList.contains('is-decoding'), tp.view, tp.showDecode];
  tp.apply({ status: 'ocr_done', active: false, text_state: 'final' });
  tp.apply({ lines: [{ id: 1, order: 0, n_low: 1, tokens: [{ t: 'نص', conf: 'high' }, { t: 'نهائي', conf: 'low' }] }] });
  tp.afterUpdate();
  out.tpResolving = [tp.resolving, tp.view, tphost.classList.contains('is-resolving')];
  run(tphost, 3000);
  out.tpDone = [tp.resolving, tp.view, linesOf(tphost)[0].words.map((w) => w.t).join(' ')];
  // a page that stopped (error) shows Tesseract's text plainly instead of drifting forever
  const host2 = new Element('div');
  const tp2 = reg.textPanel({ statusUrl: '/s', textUrl: '/t', runsUrl: '/r' });
  tp2.$refs = { decode: host2 };
  tp2.apply({ status: 'error', active: false, text_state: 'provisional', provisional_text: 'نص مبدئي' });
  tp2.afterUpdate();
  out.tpStatic = [tp2.decodeMode, host2.classList.contains('is-static'), host2.textContent];

  // --- D33 the page viewer: one sheet at a time, turned with a timed transition, filter-aware sequence
  const viewerPages = [
    { id: 11, number: 1, status: 'ocr_done', status_label: 'تم التعرّف', text_state: 'final', n_unresolved: 2, width: 700, height: 1000, thumb_url: '/t/1.webp' },
    { id: 12, number: 2, status: 'reviewed', status_label: 'مُراجَعة', text_state: 'final', is_reviewed: true, width: 800, height: 1000 },
    { id: 13, number: 3, status: 'layout_done', status_label: 'تم التخطيط', text_state: 'provisional' },
    { id: 14, number: 4, status: 'ocr_done', status_label: 'تم التعرّف', text_state: 'final', n_unresolved: 1 },
  ];
  const shellsDom = viewerPages.map((pg) => { const el = new Element('article'); el.dataset.pageId = String(pg.id); el.classList.add('page-sheet'); return el; });
  const stack = new Element('div'); stack.querySelectorAll = () => shellsDom; stack.addEventListener = () => {}; stack.clientWidth = 1000;
  const root = new Element('div'); root.querySelector = (sel) => (sel === '[data-sheet-stack]' ? stack : null);
  const v = reg.bookDashboard({ progressUrl: '/p', sheetsUrl: '/api/books/2/sheets/', filmstripUrl: '/api/books/2/filmstrip/', bookUrl: '/books/2/', canEdit: true, bookId: 2,
    active: true, status: 'ocr', stages: [], byStatus: {}, pages: viewerPages });
  v.$el = root; v.$watch = () => {}; v.init();
  const cur = () => shellsDom.filter((el) => el.classList.contains('is-current')).map((el) => Number(el.dataset.pageId));
  out.vInit = { current: v.current, shown: cur(), film: v.film.map((f) => [f.number, f.thumb, f.reviewed, f.unresolved, f.live]), ar: shellsDom[0].style['--ar-n'] };
  // a turn: out (200 ms), then the new sheet lands; pressing again during the turn only moves the target
  const realRaf = globalThis.requestAnimationFrame; globalThis.requestAnimationFrame = (fn) => { fn(); return 1; };
  timers.length = 0;
  v.turn(1);
  const midTurn = { turning: v.turning, current: v.current, timer: timers.filter((t) => t.ms === 200).length };
  v.turn(1); // pressed again: lands on page 3, not 2
  timers.filter((t) => t.ms === 200).forEach((t) => t.fn());
  out.vTurn = { midTurn, after: { turning: v.turning, current: v.current, shown: cur(), hiddenOld: shellsDom[0].classList.contains('is-current') } };
  out.vEnds = { canPrev: v.canTurn(-1), canNextFrom4: (v.showPage(4, { instant: true }), v.canTurn(1)), turnAtEnd: v.turn(1) };
  // the filter decides the sequence; the page on screen is never hidden, and moves when filtered out
  v.showPage(1, { instant: true });
  v.setFilter('review'); // ocr_done, not reviewed: pages 1 and 4
  out.vFilter = { current: v.current, film: v.film.map((f) => f.number), next: v.neighbour(1), prev: v.neighbour(-1) };
  v.apply({ total: 4, percent: 80, flags: 0, status: 'ocr', status_label: 'قيد التعرّف', dot: 'dot-accent', by_status: {}, active: true,
    pages: [{ id: 11, number: 1, status: 'reviewed', text_state: 'final', is_reviewed: true, n_unresolved: 0 }] });
  out.vKeptOnScreen = { current: v.current, hidden: shellsDom[0].hidden, inFilm: v.film.map((f) => f.number) };
  v.setFilter('reviewed');
  out.vMoved = v.current; // page 1 (now reviewed) still matches «reviewed»: stays
  v.setFilter('processing');
  out.vMovedToProcessing = v.current;
  v.setFilter('all');
  // input: keys (RTL), a trackpad swipe, a wheel gesture that must pause before the next one counts, a touch swipe
  out.vKeys = ['PageDown', 'PageUp', 'Home', 'End', 'ArrowLeft', 'ArrowRight'].map((key) => v.keyAction({ key }, false));
  v.showPage(1, { instant: true }); timers.length = 0;
  const wheel = (dx, dy) => v.onStageWheel({ deltaX: dx, deltaY: dy, cancelable: true, preventDefault: () => {} });
  wheel(-30, 0); const afterOne = v.current; wheel(-30, 0); // 60 px to the right in total: one page forward
  timers.filter((t) => t.ms === 200).forEach((t) => t.fn());
  const afterSwipe = v.current; wheel(-80, 0); // inertia of the same gesture: ignored
  out.vWheel = { afterOne, afterSwipe, inertia: v.current, idle: timers.some((t) => t.ms === 260) };
  timers.filter((t) => t.ms === 260).forEach((t) => t.fn()); // the gesture ended
  wheel(0, 100); timers.filter((t) => t.ms === 200).forEach((t) => t.fn()); // scrolling down: forward
  out.vWheelDown = v.current;
  v.onStagePointerDown({ pointerType: 'touch', clientX: 100, clientY: 100 }); v.onStagePointerUp({ pointerType: 'touch', clientX: 30, clientY: 104 }); // finger to the left: back
  timers.filter((t) => t.ms === 200).forEach((t) => t.fn());
  out.vTouchBack = v.current;
  v.onStagePointerDown({ pointerType: 'mouse', clientX: 100, clientY: 100 }); v.onStagePointerUp({ pointerType: 'mouse', clientX: 300, clientY: 100 });
  out.vMouseDragIgnored = v.current;
  // reduced motion: turns are instant
  D.reducedMotion = true; v.turn(1); out.vReduced = { current: v.current, turning: v.turning }; D.reducedMotion = false;
  // a thumbnail arriving with the sheets data reaches the filmstrip
  v.applySheets({ pages: [{ id: 13, number: 3, width: 700, height: 1000, thumb_url: '/t/3.webp', text_state: 'provisional', status: 'layout_done' }] });
  out.vFilmThumb = v.film.find((f) => f.number === 3).thumb;
  globalThis.requestAnimationFrame = realRaf;
  console.log(JSON.stringify(out));
})();
"""  # noqa: E501


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_decode_engine_layout_sheet_handle_and_dashboard_logic_under_node(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    files = [str(JS / name) for name in ("ui.js", "decode.js", "books.js", "ocr.js")]
    run = subprocess.run(["node", str(harness), *files], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])

    # --- D33 the page viewer
    assert out["vInit"] == {
        "current": 1,
        "shown": [11],
        "film": [
            [1, "/t/1.webp", False, 2, False],
            [2, "", True, 0, False],
            [3, "", False, 0, True],
            [4, "", False, 1, False],
        ],
        "ar": "0.7",
    }
    # a turn slides out for 200 ms; a second press mid-turn moves the target, the sheet lands once
    assert out["vTurn"]["midTurn"] == {"turning": "out-next", "current": 1, "timer": 1}
    assert out["vTurn"]["after"] == {"turning": "", "current": 3, "shown": [13], "hiddenOld": False}
    assert out["vEnds"] == {"canPrev": True, "canNextFrom4": False, "turnAtEnd": False}
    # the filter decides the viewer's sequence and the filmstrip
    assert out["vFilter"] == {"current": 1, "film": [1, 4], "next": 4, "prev": None}
    # the page on screen stays visible when the poll takes it out of the filter
    assert out["vKeptOnScreen"] == {"current": 1, "hidden": False, "inFilm": [4]}
    assert out["vMoved"] == 1 and out["vMovedToProcessing"] == 3
    assert out["vKeys"] == ["nextSheet", "prevSheet", "firstSheet", "lastSheet", "nextSheet", "prevSheet"]
    # trackpad: a 60 px swipe to the right is one page forward (RTL), its inertia does not flip another page;
    # after the gesture pauses, scrolling down goes forward; a touch swipe to the left goes back; mouse drags
    # don't turn
    assert out["vWheel"] == {"afterOne": 1, "afterSwipe": 2, "inertia": 2, "idle": True}
    assert out["vWheelDown"] == 3 and out["vTouchBack"] == 2 and out["vMouseDragIgnored"] == 2
    assert out["vReduced"] == {"current": 3, "turning": ""}
    assert out["vFilmThumb"] == "/t/3.webp"

    # --- decode (D28): words and lines from the Tesseract text, one text node per word, real spaces between
    # words
    assert (
        out["words"] == ["قال", "الأمير", "في", "سنة", "1966", "وَفِي", "الشهر", "الثاني"] and out["lines"] == 2
    )
    assert out["tokenNodes"] is True and out["spaces"] == 4
    # every shown string is exact or 1–3 same-family letters off (never the first, never a digit or mark, no
    # ASCII,
    # same length); over 20 s every word was exact at some tick and every veilable word veiled at some tick
    assert out["veilBad"] is None, out["veilBad"]
    assert out["everExact"] is True and out["everVeiled"] is True and out["digitsStill"] is True
    assert out["changed"] is True and out["veiledClassOk"] is True and out["maxVeiled"] <= 40
    assert out["decodingClass"] is True
    # noise mode: Arabic letters only
    assert out["noiseLines"] == 7 and out["noiseClass"] is True and out["noiseArabic"] is True
    # resolve: right-to-left / line-by-line landing, exactly the final tokens, amber underline on unresolved
    assert out["resolvingClass"] is True and out["landedPrefix"] is True and out["doneEarly"] == 0
    assert out["done"] == 1 and out["resolvedClass"] is True
    body, note = out["final"]
    assert body["region"] == "body" and [w["t"] for w in body["words"]] == ["قال", "الأمير", "في"]
    assert [("tok-low" in w["cls"]) for w in body["words"]] == [False, True, False]
    assert note["region"] == "footnote" and [w["t"] for w in note["words"]] == ["حاشية", "12"]
    assert [("tok-low" in w["cls"]) for w in note["words"]] == [False, True]  # resolved word: no underline
    assert all("is-landed" in w["cls"] for w in body["words"] + note["words"])
    assert out["forgotten"] == [3, 2]
    assert out["rmStatic"] is True and out["rmResolve"] == "قال الأمير في"

    # --- layout(): regular body page with boxes → block from the boxes (padded), font from the median line
    # height
    lay = out["layRegular"]
    exp = out["layRegularExpect"]
    assert (
        lay["mode"] == "provisional"
        and lay["groups"] == 1
        and lay["kind"] == "body"
        and lay["source"] == "text"
    )
    assert (
        lay["block"] == [0.15, exp["y0"], 0.85, exp["y1"]]
        and lay["fs"] == exp["fs"]
        and lay["lh"] == exp["lh"]
    )
    assert (
        lay["few"] is False
        and lay["rule"] is None
        and lay["ids"] == list(range(10))
        and lay["lx"] == [0.15, 0.85]
    )
    assert (
        lay["fits"] == ["justify"] * 9 + ["start"] and all(r > 0 for r in lay["rs"]) and lay["rs"][-1] >= 1.4
    )
    # no boxes at all → default block, pitch-derived font, lines span the block
    plain = out["layPlain"]
    assert (
        plain["source"] == "default"
        and plain["n"] == 12
        and plain["boxed"] is False
        and plain["few"] is False
    )
    assert plain["fs"] == out["layPlainExpect"]["fs"] and plain["block"][1] == out["layPlainExpect"]["y0"]
    assert plain["lx"] == [0.12, 0.88]
    # a title page: 1–3 lines without geometry → centred group at the title size
    assert (
        out["layTitle"]["few"] is True
        and out["layTitle"]["n"] == 2
        and out["layTitle"]["fs"] == out["layTitleExpect"]
    )
    assert out["layTitle"]["fits"] == ["start", "start"]
    # footnotes: a second group under the printed rule, capped at 90 % of the body size, one index space
    notes = out["layNotes"]
    assert notes["groups"] == ["body", "footnote"] and notes["rule"] == 0.8 and notes["noteY0"] >= 0.8
    assert (
        notes["noteFsCapped"] is True
        and notes["ids"] == [0, 1, 2, 3]
        and notes["noteKinds"] == ["footnote", "footnote"]
    )
    # final lines with bbox null → distributed inside the default block
    fnb = out["layFinalNoBox"]
    assert (
        fnb["mode"] == "final"
        and fnb["source"] == "default"
        and fnb["boxed"] is False
        and fnb["spans"] is True
    )
    assert fnb["ids"] == list(range(6)) and fnb["tokens"] == 8
    # skeleton: bars at the detected boxes, else 14 default bars in the default block; empty final page
    assert out["laySkeleton"] == {"mode": "skeleton", "n": 5, "boxed": True, "source": "boxes"}
    assert out["layBare"] == {
        "mode": "skeleton",
        "n": 14,
        "bars": [100, 100, 92, 100],
        "block": [0.12, 0.096, 0.88, 0.904],
    }
    assert out["layEmpty"] == "empty" and out["layCentred"] == "center"

    # --- D30: one type size per group; lines share the paragraph edges; fitted by word spacing
    para = out["layPara"]
    lines = para["lines"]
    assert para["hasScale"] is False  # no per-line font size any more
    assert [ln["fit"] for ln in lines] == ["center", "justify"] + ["justify"] * 6 + ["start", "over", "tight"]
    assert [ln["shape"] for ln in lines] == ["center", "indent"] + ["full"] * 6 + ["short", "full", "full"]
    # jittered full lines snap to one width: the paragraph's end edge (left) and start edge (right)
    assert {(ln["lx0"], ln["lx1"]) for ln in lines[2:8]} == {(0.15, 0.85)}
    assert (lines[1]["lx0"], lines[1]["lx1"]) == (0.15, 0.82)  # the indented first line keeps its indent
    assert (lines[8]["lx0"], lines[8]["lx1"]) == (0.55, 0.85)  # the last line keeps its own width
    assert (lines[0]["lx0"], lines[0]["lx1"]) == (0.4, 0.6)  # the heading stays centred as printed
    # the size is lowered only as far as the regular full lines need (they fit at natural spacing)
    assert 0.75 * out["layParaHeightFs"] <= para["fs"] < out["layParaHeightFs"]
    assert all(ln["r"] >= 1 and ln["ws"] == 0 and ln["sx"] == 1 for ln in lines[1:8])
    # one word too many: word spaces tighten (within half a space), no condense needed
    assert lines[10]["ws"] < 0 and lines[10]["sx"] == 1
    assert abs(lines[10]["ws"]) <= 0.5 * out["layParaSpace"] + 1e-9
    # two lines merged: spaces at their limit, condensed to 90 % and clipped with a fade
    assert lines[9]["sx"] == 0.9 and abs(abs(lines[9]["ws"]) - 0.5 * out["layParaSpace"]) < 1e-5
    # D31: a paragraph's first line keeps its printed indent and stays flush left; with little text it is
    # end-aligned (never hanging from the right); when its text needs the room it grows into the indent
    indent = out["layIndent"]
    assert indent[0] == {"shape": "indent", "fit": "end", "lx0": 0.15, "lx1": 0.83}
    assert all(ln == {"shape": "full", "fit": "justify", "lx0": 0.15, "lx1": 0.85} for ln in indent[1:6])
    assert indent[6] == {"shape": "indent", "fit": "tight", "lx0": 0.15, "lx1": 0.85}
    # the book's typical line height sets the size when the page agrees within 20 %, else the page's own
    book = out["layBook"]
    assert book["own"] == round(0.78 * 0.03 * 1.5, 5)
    assert book["near"] == round(0.78 * 0.03 * 1.5 * 1.1, 5) and book["far"] == book["own"]
    # rendered: fits on the line elements, word spacing / condense as properties, no --fit anywhere
    dom = out["layParaDom"]
    assert dom["n"] == 11 and dom["fits"] == [ln["fit"] for ln in lines] and dom["anyFit"] is False
    assert (
        dom["tightWs"] and float(dom["tightWs"]) < 0 and dom["overSx"] == "0.9" and dom.get("plainWs") is None
    )

    # --- sheet handle: skeleton bars and scan boxes from one index space, the cursor lights both sides
    assert out["hSkel"] == {
        "mode": "skeleton",
        "phase": "skeleton",
        "bars": 5,
        "boxes": 5,
        "dataMode": "skeleton",
    }
    assert out["hSkelCursor"] == {"cursor": 0, "scanLit": True, "lineLit": True}
    prov = out["hProv"]
    assert prov["mode"] == "provisional" and prov["phase"] == "provisional" and prov["layers"] == 1
    assert prov["lines"] == 12 and prov["boxes"] == 12 and prov["toks"] == 6
    cyc = out["hCycle"]
    assert cyc["bad"] is None, cyc["bad"]
    assert cyc["litOk"] is True and cyc["visited"] == 12 and cyc["words"] == 72 and cyc["maxVeiled"] <= 40
    assert cyc["allExact"] is True and cyc["allVeiled"] is True
    assert out["hHot"] == [2, True, True] and out["hHotOff"] == [-1, False]
    # final arrives: the final layer beneath the leaving provisional one, a right-to-left wave with the band
    # following
    fin = out["hFinal"]
    assert (
        fin["mode"] == "final"
        and fin["layers"] == 2
        and fin["phases"] == ["final", "provisional"]
        and fin["leaving"] is True
    )
    wave = out["hWave"]
    assert wave["wavePrefix"] is True and wave["frontMonotonic"] is True and wave["frontMoved"] is True
    assert (
        wave["landed"] is True
        and wave["layers"] == 1
        and wave["resolved"] is True
        and wave["aria"] == "false"
    )
    assert wave["lit"] is False and wave["low"] is True and wave["tabindex"] == "-1"
    # inactive book: static gray text; resume / stop toggle without a rebuild; designed states; late final
    # sheets render at once
    assert out["hStatic"] == {"mode": "static", "cls": True, "exact": True}
    assert out["hResume"] == "provisional" and out["hStop"] == ["static", 1]
    assert out["hStates"] == ["excluded", "error", "empty", "skeleton"]
    assert out["hLate"] == {"mode": "final", "resolved": True, "landed": True}
    assert out["hReduced"] == ["static", "final", 1, True]

    # --- books.js: view, batches, counts, review summary, aspect
    assert out["viewDefault"] == "sheets" and out["viewStored"] == "grid"
    assert out["ranges"] == [[1, 3], [5, 7], [50, 51], [100, 139], [140, 144]]
    assert out["counts"] == {"all": 3, "processing": 0, "review": 1, "attention": 0, "reviewed": 1}
    assert out["review"] == {
        "reviewed": 1,
        "total": 2,
        "unresolved_total": 4,
        "next_review_url": "/books/1/review/next/",
    }
    assert (
        out["nextReview"] == "/books/1/review/next/"
        and out["primaryReview"] == "review"
        and out["reviewFromPoll"] == ""
    )
    assert out["aspect"] == ["800 / 1000", "0.8000 / 1"]  # the median ratio stands in for the unknown one
    assert out["sheetText"] == "قال الأمير\n\nحاشية" and out["canCopy"] == [True, False]
    assert out["stage"] == [12, 72, 100, 100]
    # compact poll merge: labels, dots and URLs are completed client-side; the changed set is patched and
    # refetched
    assert out["merged"] == {
        "label": "مُراجَعة",
        "dot": "dot-success",
        "url": "/books/1/pages/1/",
        "review": "/books/1/review/1/",
        "stale": True,
        "unresolved": 0,
    }
    assert out["added"] == {
        "url": "/books/1/pages/4/",
        "rerun": "/books/1/pages/4/rerun/",
        "label": "مرفوعة",
        "dot": "dot-neutral",
    }
    assert out["live"] == "الصفحة 1: مُراجَعة"
    assert out["countsAfter"] == {"all": 4, "processing": 1, "review": 0, "attention": 0, "reviewed": 2}
    assert out["refetch"] == ["/api/books/1/sheets/?from=1&to=1"]  # only the mounted sheet that changed
    # filters: persisted, empty message, excluded pages only under «الكل»
    assert out["filterStored"] == "reviewed" and out["filteredOut1"] is False and out["filteredOut2"] is True
    assert out["matches"] == [
        [True, False, False],
        [False, False, False],
        [False, False, False],
        [False, True, False],
    ]
    # jump parsing: Eastern digits, whitespace, unknown numbers
    assert out["jump"] == [2, 4, None, None, None, "لا صفحة بهذا الرقم"]
    # exactly one primary per state
    assert out["primary"] == [
        "start",
        "start",
        "",
        "guides",
        "review",
        "copy",
        "copy",
    ]  # a proofreader never gets «بدء المعالجة»
    assert out["statusText"] == ["قيد المعالجة · 2 من 4 صفحة", "قيد التعرّف"]
    # the end of processing: no reload, a toast with the review entry, the primary swaps
    assert out["end"] == {
        "active": False,
        "toast": {"visible": True, "count": 3, "url": "/books/1/review/1/"},
        "reloaded": False,
        "primary": "review",
        "label": "جاهز للمراجعة",
    }
    assert out["follow"] == 7  # highest page that entered provisional / ocr_done (excluded pages skipped)
    assert out["keys"] == ["jump", None, "nextSheet", "blur", None, "grid"]
    assert out["auth"] == ["auth", True]

    # --- ocr.js text panel
    assert out["tpAttached"] == ["provisional", True, "provisional", True]
    assert out["tpResolving"] == [True, "provisional", True]
    assert out["tpDone"] == [False, "final", "نص نهائي"]
    assert out["tpStatic"] == ["static", True, "نص مبدئي"]
