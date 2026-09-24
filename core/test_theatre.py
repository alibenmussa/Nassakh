"""Phase 3 theatre UI (PHASE3_SPEC §1–§3): the dashboard's two views, the stacked sheets, the text panel's
decode hooks (Django test client) and the decode engine + dashboard logic under Node with a tiny DOM stub."""

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


# ---------------------------------------------------------------- dashboard: two views (§3)


def test_dashboard_has_the_view_toggle_stacked_sheets_and_grid_cards(editor_client):
    book, pages = _book(
        [
            (Page.Status.UPLOADED, "none"),
            (Page.Status.LAYOUT_DONE, "provisional"),
            (Page.Status.OCR_DONE, "final"),
        ]
    )
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    # segmented control «صفحات | شبكة», default صفحات, persisted by books.js (localStorage)
    toggle = re.search(
        r'<div class="segmented" role="group" aria-label="طريقة عرض الصفحات" data-view-toggle>(.*?)</div>',
        body,
        re.S,
    )
    assert toggle and "صفحات" in toggle.group(1) and "شبكة" in toggle.group(1)
    assert "@click=\"setView('sheets')\"" in body and "@click=\"setView('grid')\"" in body
    assert "<template x-if=\"view === 'sheets'\">" in body and "<template x-if=\"view === 'grid'\">" in body
    # one sheet shell and one tile per page (+ one inert x-for template each for pages ingested later);
    # sheets carry the page's aspect ratio from the first paint
    assert body.count('<article class="page-sheet') == 3 and body.count('<div class="page-tile') == 3
    assert body.count("--sheet-ar:${aspectOf(") == 4 and 'style="--sheet-ar: 7 / 10"' in body
    assert body.count('x-init="observeSheet($el,') == 4
    assert "data-live-sheet" in body and "data-live-tile" in body  # pages ingested later join both views
    # sheet header: number, status dot + label, printed number, uncertain count, review + copy actions
    sheet = body[
        body.index('class="page-sheet') : body.index('class="page-sheet', body.index('class="page-sheet') + 1)
    ]
    for needle in (
        "صفحة <bdi",
        "الرقم المطبوع",
        "غير مؤكَّدة",
        "مُراجَعة",
        ">مراجعة</a>",
        ">نسخ</span>",
        "sheet-paper-inner",
        "sheet-lines",
    ):
        assert needle in sheet, needle
    # the config carries the guarded URLs (empty strings until the backend routes land)
    assert "sheetsUrl: '" in body and "reviewNextUrl: '" in body
    assert f"sheetsUrl: '{_optional('api:book_sheets', book.pk)}'" in body
    # polite live region for state changes, and the decode host in every sheet body
    assert '<p class="sr-only" aria-live="polite" x-text="liveMessage"></p>' in body
    assert body.count('x-effect="syncSheetText($el,') == 4 and body.count('x-effect="syncScanFx($el,') == 8
    # grid cards: sweep hook, stage bar, reviewed check, uncertain badge — and no text column
    tile = body[
        body.index('<div class="page-tile') : body.index("</form>", body.index('<div class="page-tile'))
    ]
    assert (
        "tile-stage-fill" in tile
        and "is-reviewed" in tile
        and "is-uncertain" in tile
        and "decode-host" not in tile
    )


def test_dashboard_review_summary_and_single_primary_action(editor_client):
    book, pages = _book([(Page.Status.OCR_DONE, "final"), (Page.Status.REVIEWED, "final")])
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    assert "مُراجَعة <bdi" in body and 'x-text="reviewSummary.reviewed"' in body and "كلمة غير مؤكَّدة" in body
    assert "data-review-next" in body and "الصفحة التالية للمراجعة" in body
    # while the book is in OCR nothing else is primary, so the review action is
    assert re.search(r'class="btn btn-primary" x-show="nextReviewUrl"', body)
    assert body.count("btn btn-primary") == 1
    # a startable book keeps «بدء المعالجة» as its only primary
    book.status = Book.Status.UPLOADED
    book.save()
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    assert body.count("btn btn-primary") == 1 and "بدء المعالجة" in body
    assert 'class="btn" x-show="nextReviewUrl"' in body


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
    assert body.count('<article class="page-sheet') == 800
    # off-screen shells carry no images or text: everything heavy sits inside inert x-if templates
    first = body.index('<article class="page-sheet')
    second = body.index('<article class="page-sheet', first + 1)
    shell = body[first:second]
    assert shell.index("sheet-img") > shell.index('<template x-if="near[')
    assert shell.index("decode-host") > shell.index('<template x-if="near[')
    assert shell.count('<template x-if="near[') == 2
    css = CSS.read_text(encoding="utf-8")
    assert re.search(r"\.page-sheet\{[^}]*content-visibility:auto", css)
    assert re.search(r"\.page-sheet\{[^}]*contain-intrinsic-size:auto 640px", css)


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


def test_compiled_css_has_the_theatre_effects_and_their_static_fallbacks():
    css = CSS.read_text(encoding="utf-8")
    assert (
        re.search(r"\.decode-w\.is-landed\{[^}]*animation:[^;}]*decode-land", css)
        and ".4s ease-out both" in css
    )
    assert "@keyframes decode-land" in css
    assert re.search(r"\.scan-sweep:after\{[^}]*translateY\(", css)
    assert re.search(r"\.sheet-line\.is-lit\{opacity:\.22\}", css)
    reduced = css[css.index("prefers-reduced-motion:reduce") :]
    assert ".scan-sweep:after{display:none}" in reduced and ".decode-w.is-landed{animation:none}" in reduced
    assert re.search(r"\.sheet-body\{[^}]*grid-template-columns:minmax\(0,1fr\) minmax\(0,1fr\)", css)
    assert re.search(r"\.sheet-scan\{[^}]*aspect-ratio:var\(--sheet-ar,7 / 10\)", css)


# ---------------------------------------------------------------- Node: decode.js, books.js, ocr.js

HARNESS = r"""
// tiny DOM: enough for decode.js (text nodes, spans, classList) and the Alpine components
class ClassList { constructor(){ this.s = new Set(); } add(...c){ c.forEach(x => this.s.add(x)); } remove(...c){ c.forEach(x => this.s.delete(x)); }
  toggle(c, f){ if (f === undefined) f = !this.s.has(c); f ? this.s.add(c) : this.s.delete(c); return f; } contains(c){ return this.s.has(c); } toString(){ return [...this.s].join(' '); } }
class Node { constructor(){ this.childNodes = []; this.parentNode = null; this.isConnected = true; } appendChild(n){ n.parentNode = this; this.childNodes.push(n); return n; } }
class Text extends Node { constructor(d){ super(); this.nodeValue = d; } get textContent(){ return this.nodeValue; } }
class Element extends Node {
  constructor(tag){ super(); this.tagName = tag; this.classList = new ClassList(); this.attrs = {}; this.style = { setProperty: (k, v) => { this.style[k] = v; } }; this.dataset = {}; }
  set className(v){ this.classList = new ClassList(); v.split(/\s+/).filter(Boolean).forEach(c => this.classList.add(c)); } get className(){ return this.classList.toString(); }
  setAttribute(k, v){ this.attrs[k] = String(v); } getAttribute(k){ return this.attrs[k]; }
  set textContent(v){ this.childNodes.forEach(n => { n.parentNode = null; }); this.childNodes = v ? [new Text(String(v))] : []; }
  get textContent(){ return this.childNodes.map(n => n.textContent).join(''); }
  get children(){ return this.childNodes.filter(n => n instanceof Element); }
  querySelectorAll(){ return []; } querySelector(){ return null; }
}
const inits = []; const reg = {}; const timers = [];
globalThis.window = globalThis;
globalThis.document = { hidden: false, createElement: (t) => new Element(t), createTextNode: (d) => new Text(d),
  addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); }, getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: () => null };
globalThis.CustomEvent = class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } };
globalThis.dispatchEvent = () => true;
let NOW = 1000; globalThis.performance = { now: () => NOW };
globalThis.requestAnimationFrame = () => 1; // the tests drive NassakhDecode.step(t) by hand
globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
globalThis.clearTimeout = () => {};
const store = {}; globalThis.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
const fs = require('fs');
for (const f of process.argv.slice(2)) eval(fs.readFileSync(f, 'utf8'));
inits.forEach((fn) => fn());
const D = NassakhDecode;
const out = {};
const run = (el, ms, every = 41) => { for (let t = NOW; t <= NOW + ms; t += every) D.step(t); NOW += ms; };
const linesOf = (el) => el.children.map((row) => ({ region: row.getAttribute('data-region') || '', words: row.children.map((w) => ({ t: w.textContent, cls: w.className })) }));
const cap = (len) => Math.floor(0.6 * len);

// --- §2 provisional: noise ↔ letters, per-word, never settles, length ±1, real ratio ≤ 60 %
const el = new Element('div');
D.attach(el, { mode: 'provisional', text: 'قال الأمير في سنة 1966\nوَفِي الشهر الثاني' });
const words = D.inspect(el);
out.words = words.map((w) => w.real);
out.lines = el.children.length;
let lenOk = true, capOk = true, settled = 0, ever = 0, changed = 0;
let prev = D.inspect(el).map((w) => w.shown);
for (let i = 0; i < 240; i += 1) {
  NOW += 41; D.step(NOW);
  const snap = D.inspect(el);
  snap.forEach((w, k) => {
    const len = Array.from(w.real).length, shown = Array.from(w.shown).length;
    if (Math.abs(shown - len) > 1) lenOk = false;
    if (w.nReal > cap(len)) capOk = false;
    if (w.shown === w.real) settled += 1;
    if (w.nReal > 0) ever += 1;
    if (w.shown !== prev[k]) changed += 1;
  });
  prev = snap.map((w) => w.shown);
}
out.lenOk = lenOk; out.capOk = capOk; out.settled = settled; out.everReal = ever > 0; out.changed = changed > 0;
out.decodingClass = el.classList.contains('is-decoding') && el.getAttribute('aria-hidden') === 'true';
out.tokenNodes = el.children[0].children.every((w) => w.childNodes.length === 1 && w.childNodes[0] instanceof Text);
out.spaces = el.children[0].childNodes.filter((n) => n instanceof Text && n.nodeValue === ' ').length;

// --- noise mode: lines sized to the page, low-opacity class, no real letters
const noise = new Element('div');
D.attach(noise, { mode: 'noise', lines: 7 });
run(noise, 300);
out.noiseLines = noise.children.length;
out.noiseClass = noise.classList.contains('is-noise');
out.noiseReal = D.inspect(noise).every((w) => w.nReal === 0);

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

// --- books.js: view toggle, batches, review summary, aspect, boxes, sheet text, refetch on change
const dash = reg.bookDashboard({ progressUrl: '/p', sheetsUrl: '/api/books/1/sheets/', reviewNextUrl: '/books/1/review/next/', active: true,
  stages: [{ key: 'ocr_done', statuses: ['ocr_done', 'reviewed', 'assembled'] }], byStatus: { ocr_done: 1, reviewed: 1 },
  pages: [{ id: 1, number: 1, status: 'ocr_done', text_state: 'final', n_unresolved: 4 }, { id: 2, number: 2, status: 'reviewed', text_state: 'final', width: 800, height: 1000 }, { id: 3, number: 3, status: 'uploaded', text_state: 'none', is_excluded: true }] });
dash.$watch = () => {}; dash.init();
out.viewDefault = dash.view; dash.setView('grid'); out.viewStored = store['nassakh.bookView']; dash.setView('sheets');
out.ranges = dash.batchRanges([3, 1, 2, 5, 6, 7, 50, 51, ...Array.from({ length: 45 }, (_, i) => 100 + i)]);
out.review = dash.reviewSummary; out.nextReview = dash.nextReviewUrl;
dash.review = { reviewed: 1, total: 2, unresolved_total: 9, next_review_url: '' };
out.reviewFromPoll = dash.reviewSummary.next_review_url;
out.aspect = [dash.aspectOf(dash.tile(2)), dash.aspectOf(dash.tile(1))];
dash.applySheets({ pages: [{ id: 1, number: 1, width: 700, height: 1000, text_state: 'final', line_boxes: [[0.1, 0.2, 0.9, 0.25]],
  lines: [{ region_kind: 'body', tokens: [{ t: 'قال' }, { t: 'الأمير' }] }, { region_kind: 'footnote', tokens: [{ t: 'حاشية' }] }] }] });
out.aspectAfter = dash.aspectOf(dash.tile(1));
out.box = dash.boxStyle(dash.lineBoxes(1)[0]);
out.sheetText = dash.sheetText(1);
out.stage = [dash.stagePercent({ status: 'uploaded' }), dash.stagePercent({ status: 'layout_done' }), dash.stagePercent({ status: 'ocr_done' }), dash.stagePercent({ status: 'error', error: true })];
out.cleaning = [dash.isCleaning({ status: 'uploaded' }), dash.isCleaning({ status: 'uploaded', is_excluded: true }), dash.isReading({ status: 'layout_done' })];
dash.near[1] = true;
const fetched = [];
globalThis.fetch = (url) => { fetched.push(url); return Promise.resolve({ ok: true, json: async () => ({ pages: [] }) }); };
dash.apply({ total: 3, percent: 50, flags: 0, status: 'ocr', status_label: 'قيد التعرّف', dot: 'dot-accent', by_status: {}, active: true,
  pages: [{ id: 1, number: 1, status: 'reviewed', text_state: 'final', status_label: 'مُراجَعة' }, { id: 2, number: 2, status: 'reviewed', text_state: 'final' }] });
out.live = dash.liveMessage;
const flush = timers.filter((t) => t.ms === 60).pop(); flush && flush.fn();
out.refetch = fetched;

// --- ocr.js: the text panel attaches the decode effect and holds the final markup until the wave lands
(async () => {
  const host = new Element('div');
  const tp = reg.textPanel({ statusUrl: '/s', textUrl: '/t', runsUrl: '/r' });
  tp.$refs = { decode: host };
  tp.apply({ status: 'layout_done', active: true, text_state: 'provisional', provisional_text: 'نص مبدئي من تسراكت' });
  tp.afterUpdate();
  out.tpAttached = [tp.decodeMode, host.classList.contains('is-decoding'), tp.view, tp.showDecode];
  tp.apply({ status: 'ocr_done', active: false, text_state: 'final' });
  tp.apply({ lines: [{ id: 1, order: 0, n_low: 1, tokens: [{ t: 'نص', conf: 'high' }, { t: 'نهائي', conf: 'low' }] }] });
  tp.afterUpdate();
  out.tpResolving = [tp.resolving, tp.view, host.classList.contains('is-resolving')];
  run(host, 3000);
  out.tpDone = [tp.resolving, tp.view, linesOf(host)[0].words.map((w) => w.t).join(' ')];
  // a page that stopped (error) shows Tesseract's text plainly instead of scrambling forever
  const host2 = new Element('div');
  const tp2 = reg.textPanel({ statusUrl: '/s', textUrl: '/t', runsUrl: '/r' });
  tp2.$refs = { decode: host2 };
  tp2.apply({ status: 'error', active: false, text_state: 'provisional', provisional_text: 'نص مبدئي' });
  tp2.afterUpdate();
  out.tpStatic = [tp2.decodeMode, host2.classList.contains('is-static'), host2.textContent];
  console.log(JSON.stringify(out));
})();
"""  # noqa: E501


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_decode_engine_and_dashboard_logic_under_node(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    files = [str(JS / name) for name in ("ui.js", "decode.js", "books.js", "ocr.js")]
    run = subprocess.run(["node", str(harness), *files], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])

    # decode: words and lines from the Tesseract text, one text node per word, real spaces between words
    assert (
        out["words"] == ["قال", "الأمير", "في", "سنة", "1966", "وَفِي", "الشهر", "الثاني"] and out["lines"] == 2
    )
    assert out["tokenNodes"] is True and out["spaces"] == 4
    assert out["lenOk"] is True and out["capOk"] is True  # length ±1, at most 60 % real letters
    assert out["settled"] == 0 and out["everReal"] is True and out["changed"] is True  # alive, never final
    assert out["decodingClass"] is True
    # noise mode
    assert out["noiseLines"] == 7 and out["noiseClass"] is True and out["noiseReal"] is True
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
    # reduced motion: static faded text, instant resolve
    assert out["rmStatic"] is True and out["rmResolve"] == "قال الأمير في"

    # books.js
    assert out["viewDefault"] == "sheets" and out["viewStored"] == "grid"
    assert out["ranges"] == [[1, 3], [5, 7], [50, 51], [100, 139], [140, 144]]
    assert out["review"] == {
        "reviewed": 1,
        "total": 2,
        "unresolved_total": 4,
        "next_review_url": "/books/1/review/next/",
    }
    assert (
        out["nextReview"] == "/books/1/review/next/" and out["reviewFromPoll"] == ""
    )  # the poll's null wins
    assert out["aspect"] == ["800 / 1000", "7 / 10"] and out["aspectAfter"] == "700 / 1000"
    assert out["box"] == "left:10.00%;top:20.00%;width:80.00%;height:5.00%"
    assert out["sheetText"] == "قال الأمير\n\nحاشية"
    assert out["stage"] == [12, 72, 100, 100] and out["cleaning"] == [True, False, True]
    assert out["live"] == "الصفحة 1: مُراجَعة"
    assert out["refetch"] == ["/api/books/1/sheets/?from=1&to=1"]  # only the visible sheet that changed

    # ocr.js text panel
    assert out["tpAttached"] == ["provisional", True, "provisional", True]
    assert out["tpResolving"] == [True, "provisional", True]
    assert out["tpDone"] == [False, "final", "نص نهائي"]
    assert out["tpStatic"] == ["static", True, "نص مبدئي"]
