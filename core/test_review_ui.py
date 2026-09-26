"""Review screen (Phase 3, PHASE3_SPEC §4): the rendered template with a fixture config, the compiled
CSS, and the `reviewScreen` Alpine component run under Node with a tiny Alpine/DOM stub.
The backend is exercised elsewhere; here the config dict is built by hand in the payload shape."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from django.template.loader import render_to_string

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "src" / "js"
TEMPLATE = ROOT / "templates" / "review" / "review.html"


def _tok(t, conf="high", alt=None, tess=None, bbox=None, res=None, digit=False):
    return {"t": t, "alt": alt, "tess": tess, "conf": conf, "digit": digit, "bbox": bbox, "res": res}


def _config() -> dict:
    """A review payload (PHASE3_SPEC §4) with three lines and four uncertain words, one already resolved."""
    lines = [
        {
            "id": 51,
            "order": 0,
            "region_id": 3,
            "region_kind": "body",
            "bbox": [80, 100, 1000, 160],
            "text": "قال الأمير في سنة 1966",
            "ocr_text": "قال الامير في سنة 1966",
            "is_manual": False,
            "is_reviewed": False,
            "n_low": 2,
            "tokens": [
                _tok("قال", bbox=[900, 100, 1000, 160]),
                _tok("الأمير", "low", alt="الامير", tess="الاميز", bbox=[760, 100, 890, 160]),
                _tok("في", bbox=[700, 100, 750, 160]),
                _tok("سنة", bbox=[620, 100, 690, 160]),
                _tok("1966", "low", alt="1965", tess="1966", bbox=[520, 100, 610, 160], digit=True),
            ],
        },
        {
            "id": 52,
            "order": 1,
            "region_id": 3,
            "region_kind": "body",
            "bbox": [80, 170, 1000, 230],
            "text": "وَفِي الكتاب حكاية",
            "ocr_text": "وفي الكتاب حكاية",
            "is_manual": False,
            "is_reviewed": False,
            "n_low": 1,
            "tokens": [
                _tok("وَفِي", "low", alt="وفي", tess=None, bbox=[900, 170, 1000, 230]),
                _tok("الكتاب", bbox=[760, 170, 890, 230]),
                _tok("حكاية", bbox=[620, 170, 750, 230]),
            ],
        },
        {
            "id": 53,
            "order": 2,
            "region_id": 4,
            "region_kind": "footnote",
            "bbox": [80, 1300, 1000, 1340],
            "text": "(1) انظر المصدر",
            "ocr_text": "(1) انظر المصدر",
            "is_manual": False,
            "is_reviewed": False,
            "n_low": 1,
            "tokens": [
                _tok(
                    "(1)",
                    "low",
                    alt="(1)",
                    tess="(1)",
                    bbox=[940, 1300, 1000, 1340],
                    digit=True,
                    res="primary",
                ),
                _tok("انظر", "low", alt="أنظر", tess="انظر", bbox=[840, 1300, 930, 1340]),
                _tok("المصدر", bbox=[720, 1300, 830, 1340]),
            ],
        },
    ]
    return {
        "page": {
            "id": 7,
            "number": 3,
            "book_id": 1,
            "status": "ocr_done",
            "status_label": "تم التعرّف",
            "is_reviewed": False,
            "text_state": "final",
            "printed_number": "21",
            "error": "",
            "error_from": "",
        },
        "book": {
            "id": 1,
            "title": "كتاب التجربة",
            "total_pages": 120,
            "reviewed_pages": 14,
            "unresolved_total": 380,
        },
        "image": {
            "display_url": "/media/books/1/pages/0003/display.webp",
            "scan_url": "/media/books/1/pages/0003/original.png",
            "width": 1106,
            "height": 1634,
        },
        "regions": [{"id": 3, "kind": "body", "label": "المتن", "bbox": [0, 0, 1106, 1270]}],
        "lines": lines,
        "counts": {"low_total": 5, "unresolved": 4, "resolved": 1},
        "labels": {"primary": "Qari v0.3", "secondary": "Qari v0.2"},
        "nav": {
            "prev_url": "/books/1/review/2/",
            "next_url": "/books/1/review/4/",
            "next_review_url": "/books/1/review/next/?after=3",
            "dashboard_url": "/books/1/",
        },
        "urls": {
            "payload": "/api/pages/7/review/",
            "resolve": "/api/lines/__id__/resolve/",
            "edit": "/api/lines/__id__/edit/",
            "delete": "/api/lines/__id__/delete/",
            "merge": "/api/lines/__id__/merge/",
            "role": "/api/lines/__id__/role/",
            "delete_word": "/api/lines/__id__/delete-word/",
            "insert": "/api/pages/7/lines/",
            "undo": "/api/pages/7/undo/",
            "approve": "/api/pages/7/approve/",
            "reopen": "/api/pages/7/reopen/",
            "filmstrip": "/api/books/1/filmstrip/",
        },
        "can_edit": True,
    }


def _render(config: dict | None = None) -> str:
    config = config or _config()
    context = {
        "book": SimpleNamespace(title=config["book"]["title"], pk=1),
        "page": SimpleNamespace(number=config["page"]["number"], pk=7),
        "prev_url": config["nav"]["prev_url"],
        "next_url": config["nav"]["next_url"],
        "config": config,
        # base.html's shell needs a user (the `default:` filter argument must resolve)
        "user": SimpleNamespace(get_username=lambda: "reviewer", get_full_name=lambda: "مراجع"),
        "role_label": "مدقّق",
    }
    return render_to_string("review/review.html", context)


def _json_script(body: str, element_id: str):
    match = re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>', body, re.S)
    assert match, element_id
    return json.loads(match.group(1))


# ---------------------------------------------------------------- template


def test_review_template_structure_rtl_counters_and_config():
    body = _render()
    assert 'lang="ar" dir="rtl"' in body and "<title>مراجعة · صفحة 3 · كتاب التجربة · نسّاخ</title>" in body
    assert _json_script(body, "review-config")["page"]["id"] == 7
    assert "x-data=\"reviewScreen(JSON.parse(document.getElementById('review-config').textContent))\"" in body
    assert 'x-data="reviewBar"' in body and 'x-text="b.number"' in body  # the bar reads the shared snapshot
    # top bar: page counter and progress isolated with <bdi>, 4 px bar, save chip, one primary action
    assert (
        'صفحة <bdi class="num" x-text="b.number"></bdi> من <bdi class="num" x-text="b.total"></bdi>' in body
    )
    assert (
        'حُسمت <bdi class="num rv-counter" x-text="b.shown"></bdi> من '
        '<bdi class="num" x-text="b.lowTotal"></bdi> كلمة' in body
    )
    assert 'class="progress rv-progress-bar" role="progressbar" x-show="b.lowTotal > 0"' in body
    # D73: a page with no uncertain words reads «لا علامات», never «حُسمت 0 من 0 كلمة»
    assert (
        '<span class="rv-progress-text meta rv-no-flags" x-show="!b.lowTotal" data-no-flags>لا علامات</span>'
        in body
    )
    assert "'لا كلمات غير مؤكَّدة في هذه الصفحة؛ اقرأ الأسطر مع الصورة ثم اعتمدها.'" in body
    assert '<span class="rv-progress-text meta" x-show="b.lowTotal > 0">حُسمت' in body
    assert "يحفظ…" in body and "محفوظ" in body and "تعذّر الحفظ · إعادة المحاولة" in body
    assert body.count("btn btn-primary") == 1 and "اعتماد الصفحة" in body
    # split view: scan pane, lines pane, swap toggle, tabs, filmstrip
    assert 'class="rv-pane rv-scan-pane"' in body and 'class="rv-pane rv-lines-pane"' in body
    assert "toggleSwap()" in body and "المعالَجة" in body and "الأصل" in body
    assert 'class="rv-film" aria-label="شريط الصفحات"' in body and 'loading="lazy"' in body
    # the pre-Alpine skeleton hides itself once the component mounts
    assert 'class="rv-boot" x-show="false"' in body


def test_review_template_words_popover_editing_and_states():
    body = _render()
    # tokens separated by real spaces; uncertain words are focusable buttons with a popover
    assert "i < line.tokens.length - 1 ? ' ' : ''" in body
    assert (
        ":tabindex=\"tok.conf === 'low' ? 0 : -1\"" in body
        and ":role=\"tok.conf === 'low' ? 'button' : null\"" in body
    )
    assert 'class="rv-pop"' in body and 'role="dialog" aria-label="قائمة الكلمة"' in body
    assert 'x-for="opt in options()"' in body and '<kbd class="kbd" x-text="opt.key"' in body
    assert 'placeholder="تصحيح…"' in body and '@submit.prevent="submitTyped()"' in body
    # inline line editor, insert, delete from a menu with the undo toast
    assert '@dblclick.prevent="startEdit(line)"' in body and '@keydown.enter.prevent="saveEdit()"' in body
    assert (
        "إدراج سطر بعده" in body
        and 'class="menu-item is-danger" role="menuitem" @click="removeLine(line)"' in body
    )
    assert 'x-text="undoToast && undoToast.message"' in body and ">تراجع</button>" in body
    # one editor at a time, found by class (three templates render one; a shared x-ref broke the upward move)
    assert body.count('class="rv-editor"') == 3 and 'x-ref="editor"' not in body
    # Tab in the correction input saves a changed draft (Enter and Tab are both named in the sheet)
    assert '@keydown.tab.prevent="onTypedTab($event.shiftKey)"' in body and "لحفظه والانتقال" in body
    # the save chip counts the unsaved actions; the page turn carries its direction
    assert "من الإجراءات · إعادة المحاولة" in body and ''':class="slide ? 'is-' + slide : ''"''' in body
    # the sheet's 0 row names the fit the key returns to
    assert 'العودة إلى <span x-text="fitLabel"></span>' in body
    # designed states: error (Arabic headline, dashboard retry), pending (decode noise),
    # loading skeleton, read-only
    assert 'x-text="errorHeadline"' in body and "فتح لوحة الكتاب لإعادة المرحلة" in body
    assert 'x-ref="noise"' in body and "النص قيد التعرّف" in body
    assert 'class="rv-skeleton" x-show="loading"' in body
    assert "عرض فقط" in body and 'x-show="!canEdit && ready"' in body
    # approve dialog and shortcut sheet (modals with focus trap and Esc)
    # (D73: the title counts words and suggestions, `dialogTitle`; its wording is tested under Node)
    assert 'role="alertdialog" aria-modal="true"' in body and 'x-text="dialogTitle"' in body
    assert "لا يدخل الكتابَ نصٌّ مقترح لم يُحسم." in body and 'x-show="dialog.gaps > 0"' in body
    # open groups are words already in the text (D72): they enter the book as they are
    assert "تبقى الكلمات المضافة التي لم تُحسم في النص كما هي." in body and 'x-show="dialog.suggested > dialog.gaps"' in body
    assert "متابعة المراجعة" in body and '@click="approve(true)"' in body
    assert 'aria-labelledby="rv-sheet-title"' in body and "اختصارات لوحة المفاتيح" in body
    assert body.count('@keydown.tab="trapTab($event, $el)"') == 2
    assert "الصفحة التالية / السابقة" in body  # ArrowLeft/ArrowRight row of the sheet
    # D69: the sheet in two groups, letters as Latin capitals that work on the Arabic layout; ⌘↵ approves
    sheet = body[body.index('<dl class="rv-keys">') : body.index("</dl>", body.index('<dl class="rv-keys">'))]
    assert sheet.index("في قائمة الكلمة") < sheet.index("⌘↵") < sheet.index("في الصفحة") < sheet.index(">A<")
    assert '<kbd class="kbd">Space</kbd>' in sheet and '<kbd class="kbd">Home</kbd>' in sheet
    assert "تعمل الاختصارات بلوحة المفاتيح العربية أيضًا: المفتاح نفسه في مكانه." in body
    assert 'title="اعتماد الصفحة (A، أو ⌘↵ من قائمة الكلمة)"' in body
    assert 'أو اكتب التصحيح مباشرة؛ <kbd class="kbd">⌘↵</kbd> لاعتماد الصفحة.' in body
    assert "spinner" not in body


def test_review_js_and_css_are_built():
    js = (JS / "review.js").read_text(encoding="utf-8")
    assert "Alpine.data('reviewScreen'" in js and "Alpine.data('reviewBar'" in js
    assert "history.replaceState" in js and "X-CSRFToken" in js and "prefers-reduced-motion" in js
    assert "NassakhDecode.attach(el, { mode: 'noise'" in js
    css = (ROOT / "static" / "dist" / "app.css").read_text(encoding="utf-8")
    assert ".rv-tok.is-open{border-bottom-color:var(--color-warning)}" in css
    assert re.search(r"\.rv-tok\.is-flash\{animation:\.4s [^}]*rv-tok-land", css)  # resolve morph ≤ 400 ms
    assert re.search(r"\.rv-stamp-mark\{[^}]*animation:\.4s [^}]*rv-stamp-in", css)
    assert re.search(r"\.rv-sheet\.is-gliding\{transition:transform \.28s", css)  # smooth pan 200–300 ms
    assert ".main:has(>.review-screen){gap:0;padding:0}" in css
    assert re.search(r"\.is-swapped \.rv-scan-pane\{[^}]*order:1", css)
    # turning back mirrors the forward motion (source: the built file follows the lead's rebuild)
    src = (ROOT / "static" / "src" / "components" / "review.css").read_text(encoding="utf-8")
    assert ".rv-split.is-out-back { transform: translateX(-3%)" in src
    assert ".rv-split.is-in-back { transform: translateX(3%); opacity: 0; transition: none; }" in src


# ------------------------------------------------ the component under Node (small Alpine stub)

HARNESS = r"""
const reg = {}; const inits = []; const stores = {}; const calls = [];
globalThis.window = globalThis;
globalThis.document = { hidden: false, addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); },
  getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: (n, v) => { if (v !== undefined) stores[n] = v; return stores[n]; } };
globalThis.setTimeout = (fn) => 1; globalThis.clearTimeout = () => {};
globalThis.Nassakh = { toast: (m) => calls.push(['toast', m]), copyText: (t) => { calls.push(['copy', t]); return true; } };
const posted = []; globalThis.BroadcastChannel = class { constructor(name) { this.name = name; } postMessage(m) { posted.push([this.name, m]); } };
const fs = require('fs');
const config = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
eval(fs.readFileSync(process.argv[4], 'utf8')); // keys.js (NassakhKeys)
eval(fs.readFileSync(process.argv[2], 'utf8'));
inits.forEach((fn) => fn());
const reply = (status, data) => async (url, init) => { calls.push([init && init.method || 'GET', url, init && init.body ? JSON.parse(init.body) : null]); return { ok: status < 400, status, json: async () => data }; };
const clone = (v) => JSON.parse(JSON.stringify(v));
(async () => {
  const out = {};
  const c = reg.reviewScreen(clone(config));
  c.init();
  out.bar = { number: stores.review.bar.number, total: stores.review.bar.total, resolved: stores.review.bar.resolved, lowTotal: stores.review.bar.lowTotal, canApprove: stores.review.bar.canApprove };
  // reading order of the unresolved words, wrapping
  out.order = c.unresolvedRefs().map((r) => [r.lineId, r.index]);
  c.move(1); const f1 = clone(c.focus); c.move(1); const f2 = clone(c.focus); c.move(1); c.move(1); const f4 = clone(c.focus); c.move(1); const wrap = clone(c.focus); c.move(-1); const back = clone(c.focus);
  out.nav = [f1, f2, f4, wrap, back].map((r) => [r.lineId, r.index]);
  out.popOpenOnFocus = c.pop.open;
  out.optionKeysNoTess = c.options().map((o) => [o.key, o.choice]);   // «انظر»: Tesseract agrees with the primary → no third row
  c.focusWord({ lineId: 51, index: 1 });
  out.optionKeys = c.options().map((o) => [o.key, o.choice]);
  // a number Kraken read (D50): its one reading, labelled Kraken (confirm it or type the true number)
  out.krakenOptions = c.options({ t: '(٣٢٠–٣٢٢هـ)', conf: 'low', src: 'kraken', alt: null, tess: null, digit: true }).map((o) => [o.key, o.choice, o.label]);
  // a letter Qari wrote for a digit (D51): Kraken's number first, Qari's letter second under Qari's label
  out.letterOptions = c.options({ t: '(٥)', conf: 'low', src: 'kraken', alt: '(ه)', tess: null, digit: true }).map((o) => [o.key, o.choice, o.label, o.value]);
  // choose secondary: optimistic, then the server line/counts land
  c.focusWord({ lineId: 51, index: 1 });
  const serverLine = clone(config.lines[0]); serverLine.tokens[1].t = 'الامير'; serverLine.tokens[1].res = 'secondary'; serverLine.n_low = 1;
  globalThis.fetch = reply(200, { line: serverLine, counts: { line_n_low: 1, page_unresolved: 3, page_low_total: 5, book_unresolved_total: 379 }, page: { is_reviewed: false } });
  const p = c.choose('secondary');
  out.optimistic = { t: c.lines[0].tokens[1].t, res: c.lines[0].tokens[1].res, counts: clone(c.counts), save: c.save.state, focus: clone(c.focus), flash: Object.keys(c.flashing) };
  out.resolvedOptions = c.options(c.lines[0].tokens[1]).map((o) => [o.choice, o.value === config.lines[0].tokens[1][{ primary: 't', secondary: 'alt', tess: 'tess' }[o.choice]], o.current]);
  await p;
  out.settled = { save: c.save.state, counts: clone(c.counts), bookUnresolved: c.book.unresolved_total, nLow: c.lines[0].n_low };
  out.resolveCall = calls.filter((x) => x[0] === 'POST').pop();
  // a failed POST rolls the word and the counts back and arms the retry
  c.focusWord({ lineId: 52, index: 0 });
  globalThis.fetch = reply(500, { message: 'تعذّر' });
  await c.choose('primary');
  out.rolledBack = { t: c.lines[1].tokens[0].t, res: c.lines[1].tokens[0].res, counts: clone(c.counts), save: c.save.state, failed: c.save.failed.length, retry: typeof c.save.failed[0].retry, toast: calls.filter((x) => x[0] === 'toast').pop()[1] };
  // undo re-renders from the returned payload (the failed resolve is dropped first: the chip stays in error while one is unsaved)
  const undone = clone(config); undone.lines = undone.lines.slice(0, 2); undone.counts = { low_total: 3, unresolved: 2, resolved: 1 };
  globalThis.fetch = reply(200, undone);
  c.save.failed = []; c.save.state = 'idle';
  await c.undo();
  out.undone = { lines: c.lines.length, counts: clone(c.counts), save: c.save.state, focus: c.focus };
  // approve with unresolved words asks first (no request); a 409 from the server opens the same dialog
  calls.length = 0;
  await c.approve(false);
  out.clientDialog = { open: c.dialog.open, count: c.dialog.count, posts: calls.filter((x) => x[0] === 'POST').length };
  c.dialog.open = false; c.counts.unresolved = 0;
  globalThis.fetch = reply(409, { unresolved: 4, message: 'بقيت 4 كلمات' });
  await c.approve(false);
  out.serverDialog = { open: c.dialog.open, count: c.dialog.count, save: c.save.state, approving: c.approving, body: calls.filter((x) => x[0] === 'POST').pop()[2] };
  // approve success: stamp, reviewed state, next payload swapped in from next_payload_url
  c.dialog.open = false;
  const nextPayload = clone(config); nextPayload.page.id = 8; nextPayload.page.number = 4; nextPayload.counts = { low_total: 2, unresolved: 2, resolved: 0 };
  globalThis.fetch = async (url, init) => { calls.push([init && init.method || 'GET', url]); return init && init.method === 'POST' ? { ok: true, status: 200, json: async () => ({ status: 'reviewed', next_review_url: '/books/1/review/4/', next_payload_url: '/api/pages/8/review/' }) } : { ok: true, status: 200, json: async () => nextPayload }; };
  await c.approve(true);
  await new Promise((r) => setImmediate(r));
  out.approved = { page: c.page.number, counts: clone(c.counts), reviewedPages: c.book.reviewed_pages, fetched: calls.filter((x) => x[0] === 'GET').map((x) => x[1]) };
  // keyboard map (RTL: ArrowLeft = next page)
  const ka = NassakhReview.keyAction;
  const ctx = { inField: false, inFlow: true, focused: true, optionKeys: ['1', '2'] };
  const word = { ...ctx, open: true }; // the word menu is open: letters and digits edit the word (D69)
  out.keys = {
    left: ka({ key: 'ArrowLeft' }, ctx), right: ka({ key: 'ArrowRight' }, ctx),
    tab: ka({ key: 'Tab' }, ctx), shiftTab: ka({ key: 'Tab', shiftKey: true }, ctx), tabOutside: ka({ key: 'Tab' }, { ...ctx, inFlow: false }),
    undo: ka({ key: 'z', metaKey: true }, ctx), sheet: ka({ key: '?' }, ctx), enter: ka({ key: 'Enter' }, ctx), altEnter: ka({ key: 'Enter', altKey: true }, ctx),
    one: ka({ key: '1' }, ctx), three: ka({ key: '3' }, word), threePage: ka({ key: '3' }, ctx), letter: ka({ key: 'ك' }, word), letterPage: ka({ key: 'ك' }, ctx),
    letterUnfocused: ka({ key: 'ك' }, { ...ctx, focused: false }),
    edit: ka({ key: 'e' }, ctx), approve: ka({ key: 'A' }, ctx), next: ka({ key: 'n' }, ctx), zoom: [ka({ key: '+' }, ctx), ka({ key: '-' }, ctx), ka({ key: '0' }, ctx)],
    inField: ka({ key: 'a' }, { ...ctx, inField: true }), escInField: ka({ key: 'Escape' }, { ...ctx, inField: true }),
    undoInField: ka({ key: 'z', metaKey: true }, { ...ctx, inField: true }), redo: ka({ key: 'z', metaKey: true, shiftKey: true }, ctx),
  };
  // page text for the clipboard: body, blank line, footnotes
  out.copy = c.pageText();
  // fit modes: the sheet base size follows the mode; boxes are % of the sheet; pan maths use the sheet width
  const f = reg.reviewScreen(clone(config)); f.init();
  f.image = { width: 1000, height: 1500 }; f.pane = { w: 800, h: 600 }; f.zoom = { scale: 1, x: 0, y: 0 };
  out.fitDefault = f.fit;
  f.clampPan();
  out.fitHeight = { w: f.sheetW, h: f.sheetH, x: f.zoom.x, y: f.zoom.y, style: f.sheetStyle, box: f.boxStyle([100, 300, 200, 330]) };
  f.panTo([100, 1400, 200, 1430]);  // near the bottom: fully visible already at fit height -> no move
  out.fitHeightPan = { x: f.zoom.x, y: f.zoom.y };
  f.setFit('width');
  out.fitWidth = { fit: f.fit, w: f.sheetW, h: f.sheetH, scale: f.zoom.scale, box: f.boxStyle([100, 300, 200, 330]), label: f.fitLabel };
  f.panTo([100, 1400, 200, 1430]);  // bottom word is off-pane at fit width -> sheet glides up to centre it
  const k = f.sheetW / f.image.width;
  out.fitWidthPan = { y: f.zoom.y, wordCentreOnPane: 1415 * k + f.zoom.y };
  // D31: any word opens the popover; merge with the next / previous word; delete a stray word
  const m = reg.reviewScreen(clone(config)); m.init();
  m.onTokClick(m.lines[1], 1); // «الكتاب»: a confident word
  out.wordPop = { open: m.pop.open, options: m.options().length, acts: m.wordActions(), next: m.mergePreview(1), prev: m.mergePreview(-1) };
  out.prefill = { typing: m.pop.typing, typed: m.pop.typed }; // D32: the correction is the main action
  const mergedLine = clone(config.lines[1]);
  mergedLine.tokens = [mergedLine.tokens[0], { t: 'الكتابحكاية', alt: null, tess: null, conf: 'high', digit: false, bbox: [620, 170, 890, 230], res: 'typed' }];
  globalThis.fetch = reply(200, { line: mergedLine, counts: { line_n_low: 1, page_unresolved: 4, page_low_total: 5, book_unresolved_total: 380 } });
  calls.length = 0;
  const mp = m.mergeWord(1);
  out.mergeOptimistic = { words: m.lines[1].tokens.map((t) => t.t), bbox: m.lines[1].tokens[1].bbox, text: m.lines[1].text, focus: clone(m.focus), popOpen: m.pop.open };
  await mp;
  out.mergeCall = calls.filter((x) => x[0] === 'POST').pop();
  // a failed merge restores both words and arms the retry
  m.focusWord({ lineId: 51, index: 0 });
  const before51 = JSON.stringify(m.lines[0].tokens.map((t) => t.t));
  globalThis.fetch = reply(500, { message: 'تعذّر' });
  await m.mergeWord(1);
  out.mergeRolledBack = { same: JSON.stringify(m.lines[0].tokens.map((t) => t.t)) === before51, save: m.save.state };
  // delete a word: it goes at once and the undo toast offers it back; a line's only word removes the line
  m.save.state = 'idle';
  m.focusWord({ lineId: 53, index: 2 });
  const delLine = clone(config.lines[2]); delLine.tokens = delLine.tokens.slice(0, 2);
  globalThis.fetch = reply(200, { line: delLine, counts: { line_n_low: 1, page_unresolved: 3, page_low_total: 4, book_unresolved_total: 379 } });
  calls.length = 0;
  await m.deleteWord();
  out.deleted = { words: m.lines[2].tokens.map((t) => t.t), toast: m.undoToast && m.undoToast.message, call: calls.filter((x) => x[0] === 'POST').pop() };
  m.lines[1].tokens = [m.lines[1].tokens[0]];
  m.focusWord({ lineId: 52, index: 0 });
  globalThis.fetch = reply(200, { deleted_id: 52, counts: { line_n_low: 0, page_unresolved: 2, page_low_total: 3, book_unresolved_total: 378 } });
  calls.length = 0;
  await m.deleteWord();
  out.deletedLine = { ids: m.lines.map((l) => l.id), call: calls.filter((x) => x[0] === 'POST').pop()[1] };
  // D32: a click outside the popover closes it (not the release of a scan drag)
  const o = reg.reviewScreen(clone(config)); o.init();
  o.onTokClick(o.lines[0], 1); o.dragMoved = true; o.onPopOutside();
  const afterDrag = { open: o.pop.open, dragMoved: o.dragMoved };
  o.onPopOutside();
  out.outside = { afterDrag, afterClick: { open: o.pop.open, focus: o.focus } };
  // D32: placement against the visible column (scroller clipped to the window), 8 px from every edge
  globalThis.innerWidth = 1200; globalThis.innerHeight = 800;
  const scroller = { getBoundingClientRect: () => ({ top: 100, bottom: 780, left: 380, right: 1120 }) };
  const pl = reg.reviewScreen(clone(config)); pl.init();
  pl.$refs = { stage: { getBoundingClientRect: () => ({ top: 100, bottom: 2000, left: 400, right: 1100, width: 700 }), closest: () => scroller }, pop: { offsetWidth: 272, offsetHeight: 200 } };
  const at = (top, left, right) => { pl.placePop({ getBoundingClientRect: () => ({ top, bottom: top + 30, left, right }) }); return { style: pl.pop.style, above: pl.pop.above }; };
  out.place = { middle: at(300, 800, 860), bottom: at(700, 800, 860), leftEdge: at(300, 390, 450), rightEdge: at(300, 1080, 1130) };
  const mv = (popRect) => {
    pl.$refs.pop = { getBoundingClientRect: () => popRect, closest: () => scroller, offsetWidth: 272, offsetHeight: 200 };
    pl.$refs.more = { offsetWidth: 248, offsetHeight: 130 }; pl.$refs.moreRow = { offsetTop: 150 };
    pl.placeMore();
    return { side: pl.pop.moreSide, style: pl.pop.moreStyle };
  };
  out.more = { roomLeft: mv({ top: 338, bottom: 538, left: 700, right: 972 }), roomRight: mv({ top: 338, bottom: 538, left: 500, right: 772 }),
    narrow: mv({ top: 338, bottom: 538, left: 588, right: 860 }), low: mv({ top: 600, bottom: 800, left: 700, right: 972 }) };
  // the submenu opens on hover and closes after a short grace (hover intent)
  const mm = reg.reviewScreen(clone(config)); mm.init();
  mm.onTokClick(mm.lines[1], 1); mm.openMore(); const openedByHover = mm.pop.more;
  let fired = null; const realTimeout = globalThis.setTimeout; globalThis.setTimeout = (fn, ms) => { fired = ms; fn(); return 1; };
  mm.closeMoreSoon(); globalThis.setTimeout = realTimeout;
  out.moreHover = { opened: openedByHover, closedAfter: fired, closed: mm.pop.more === false };
  mm.openMore(); mm.closeTop(); out.moreEsc = { more: mm.pop.more, popStillOpen: mm.pop.open };
  // D32: line roles, optimistic with rollback
  const rl = reg.reviewScreen(clone(config)); rl.init();
  const headLine = clone(config.lines[0]); headLine.role = 'heading';
  globalThis.fetch = reply(200, { line: headLine }); calls.length = 0;
  await rl.setRole(rl.lines[0], 'heading');
  out.role = { role: rl.lines[0].role, call: calls.filter((x) => x[0] === 'POST').pop(), cls: rl.lineClass(rl.lines[0])['is-heading'], label: rl.roleLabel(rl.lines[0]) };
  globalThis.fetch = reply(500, { message: 'تعذّر' });
  await rl.setRole(rl.lines[1], 'subheading');
  out.roleRollback = { role: rl.lines[1].role || 'body', save: rl.save.state };
  // keys on buttons stay native; arrows inside the popover belong to its menus
  const kk = reg.reviewScreen(clone(config)); kk.init(); kk.onTokClick(kk.lines[1], 1); calls.length = 0;
  const onButton = (key) => { let prevented = false; kk.onKey({ key, target: { tagName: 'BUTTON', closest: (sel) => (sel === '.rv-pop' ? {} : null) }, preventDefault: () => { prevented = true; } }); return prevented; };
  out.keyGuard = { enter: onButton('Enter'), backspace: onButton('Backspace'), arrowLeft: onButton('ArrowLeft'), words: kk.lines[1].tokens.length, posts: calls.filter((x) => x[0] === 'POST').length };
  out.keysD31 = {
    mergeNext: ka({ key: 'ArrowLeft', altKey: true }, ctx), mergePrev: ka({ key: 'ArrowRight', altKey: true }, ctx),
    del: ka({ key: 'Backspace' }, ctx), del2: ka({ key: 'Delete' }, ctx),
    delUnfocused: ka({ key: 'Backspace' }, { ...ctx, focused: false }), delInField: ka({ key: 'Backspace' }, { ...ctx, inField: true }),
    altArrowUnfocused: ka({ key: 'ArrowLeft', altKey: true }, { ...ctx, focused: false }),
  };
  // ------------------------------------------------------------ Phase 3 review fixes
  const flush = () => new Promise((r) => setImmediate(r));
  const deferred = () => { let release; const done = new Promise((r) => { release = r; }); return { release, done }; };
  const reqs = () => calls.filter((x) => x[0] !== 'toast').map((x) => x[1]);
  const posts = () => calls.filter((x) => x[0] === 'POST');
  // every answer is a fresh object: replaceLine keeps the object it is given, so a shared one would alias components
  const answer = (make) => async (url, init) => { calls.push([init && init.method || 'GET', url, init && init.body ? JSON.parse(init.body) : null]); return { ok: true, status: 200, json: async () => make() }; };
  const line51 = () => { const l = clone(config.lines[0]); l.tokens[1].t = 'الامير'; l.tokens[1].res = 'secondary'; l.n_low = 1; return l; };
  const page4 = () => { const n = clone(config); n.page.id = 8; n.page.number = 4; n.lines = [Object.assign(clone(config.lines[0]), { id: 91 })]; n.counts = { low_total: 0, unresolved: 0, resolved: 0 }; n.urls.undo = '/api/pages/8/undo/'; return n; };
  const film = { items: [{ id: 6, number: 2, url: '/books/1/review/2/', status: 'ocr_done' }, { id: 7, number: 3, status: 'ocr_done' }, { id: 8, number: 4, url: '/books/1/review/4/', status: 'ocr_done' }], state: 'ready' };
  // a resolve answered after the next page's GET must land on its own page: the swap waits for the queue
  const s1 = reg.reviewScreen(clone(config)); s1.init(); s1.film = clone(film);
  const post1 = deferred();
  globalThis.fetch = async (url, init) => { const method = (init && init.method) || 'GET'; calls.push([method, url]); if (method === 'POST') { await post1.done; return { ok: true, status: 200, json: async () => ({ line: line51(), counts: { line_n_low: 1, page_unresolved: 3, page_low_total: 5 }, page: { id: 7, number: 3 } }) }; } return { ok: true, status: 200, json: async () => page4() }; };
  s1.focusWord({ lineId: 51, index: 1 }); calls.length = 0;
  const s1p = s1.choose('secondary'); s1.runAction('nextPage'); await flush(); await flush();
  out.swapWaits = { reqs: reqs(), loading: s1.loading };
  post1.release(); await s1p; await flush(); await flush(); await flush();
  out.afterSwap = { reqs: reqs(), page: s1.page.number, id: s1.page.id, lines: s1.lines.map((l) => l.id), counts: clone(s1.counts), save: s1.save.state, undo: s1.urls.undo };
  // a failed answer for the old page (an editor's Enter during the load) neither rolls back onto the new page nor arms a retry
  const s2 = reg.reviewScreen(clone(config)); s2.init();
  const post2 = deferred(); const get2 = deferred();
  globalThis.fetch = async (url, init) => { const method = (init && init.method) || 'GET'; calls.push([method, url]); if (method === 'POST') { await post2.done; return { ok: false, status: 500, json: async () => ({ message: 'تعذّر' }) }; } await get2.done; return { ok: true, status: 200, json: async () => page4() }; };
  const s2sw = s2.swapTo('/api/pages/8/review/', '/books/1/review/4/', 1); await flush();
  s2.edit = { lineId: 52, text: 'نص جديد' }; const s2e = s2.saveEdit(); await flush();
  get2.release(); await s2sw; await flush(); await flush();
  post2.release(); await s2e; await flush();
  out.lateFail = { page: s2.page.number, counts: clone(s2.counts), lines: s2.lines.map((l) => l.id), save: s2.save.state, failed: s2.save.failed.length, toast: calls.filter((x) => x[0] === 'toast').pop()[1] };
  // a poll of the old page answered after the swap is dropped
  const s3 = reg.reviewScreen(clone(config)); s3.init(); s3.page.text_state = 'provisional';
  const finalOld = clone(config); finalOld.page.text_state = 'final'; const poll3 = deferred();
  globalThis.fetch = async (url) => { calls.push(['GET', url]); if (url === '/api/pages/7/review/') { await poll3.done; return { ok: true, status: 200, json: async () => finalOld }; } return { ok: true, status: 200, json: async () => page4() }; };
  const s3p = s3.poll(); await flush();
  await s3.swapTo('/api/pages/8/review/', null, 1); await flush();
  poll3.release(); await s3p; await flush();
  out.latePoll = { page: s3.page.number, id: s3.page.id, lines: s3.lines.map((l) => l.id) };
  // ⌘Z inside the line editor stays the field's own undo: no server undo, the draft survives
  const s4 = reg.reviewScreen(clone(config)); s4.init(); s4.startEdit(s4.lines[1]); s4.edit.text = 'مسودة'; calls.length = 0;
  let zPrevented = false; s4.onKey({ key: 'z', metaKey: true, target: { tagName: 'TEXTAREA' }, preventDefault: () => { zPrevented = true; } });
  out.cmdZInEditor = { prevented: zPrevented, draft: s4.edit && s4.edit.text, posts: posts().length };
  // the undo offer retires once a later action is sent, also when the delete's answer is still pending
  const s5 = reg.reviewScreen(clone(config)); s5.init(); const del5 = deferred();
  globalThis.fetch = async (url) => { if (url.includes('delete-word')) { await del5.done; return { ok: true, status: 200, json: async () => ({ line: clone(delLine), counts: {} }) }; } return { ok: true, status: 200, json: async () => ({ line: line51(), counts: {} }) }; };
  s5.focusWord({ lineId: 53, index: 2 }); const s5d = s5.deleteWord(); s5.focusWord({ lineId: 51, index: 1 }); const s5r = s5.choose('secondary');
  del5.release(); await s5d; await s5r; await flush();
  out.undoPipelined = { toast: s5.undoToast, save: s5.save.state };
  globalThis.fetch = answer(() => ({ line: clone(delLine), counts: {} }));
  s5.focusWord({ lineId: 53, index: 1 }); await s5.deleteWord(); const offered = s5.undoToast && s5.undoToast.message;
  globalThis.fetch = answer(() => ({ line: line51(), counts: {} })); s5.focusWord({ lineId: 51, index: 4 }); await s5.choose('primary');
  out.undoRetired = { offered, after: s5.undoToast };
  // Enter confirms the reading in the text: no request for a word the reviewer already resolved (or chose again),
  // the primary reading for an unresolved word, the current reading's choice for one the chooser picked (D26)
  const s6 = reg.reviewScreen(clone(config)); s6.init();
  globalThis.fetch = answer(() => ({ line: line51(), counts: { line_n_low: 1, page_unresolved: 3, page_low_total: 5 } }));
  s6.focusWord({ lineId: 51, index: 1 }); await s6.choose('secondary');
  const enterOn = (inst) => inst.onKey({ key: 'Enter', target: { tagName: 'SPAN', closest: () => null }, preventDefault: () => {} });
  s6.onTokClick(s6.lines[0], 1); calls.length = 0; enterOn(s6); await flush();
  out.acceptResolved = { t: s6.lines[0].tokens[1].t, res: s6.lines[0].tokens[1].res, posts: posts().length, focus: clone(s6.focus) };
  s6.onTokClick(s6.lines[0], 1); await s6.choose('secondary');
  out.rechooseCurrent = { posts: posts().length, focus: clone(s6.focus) };
  s6.focusWord({ lineId: 51, index: 4 }); enterOn(s6); await flush();
  out.acceptUnresolved = posts().pop();
  Object.assign(s6.lines[1].tokens[0], { res: 'chooser', t: 'وفي', orig: 'وَفِي' });
  s6.focusWord({ lineId: 52, index: 0 }); enterOn(s6); await flush();
  out.acceptChooser = posts().pop();
  // Tab in the correction input saves a changed draft; an untouched prefill just moves on
  const s7 = reg.reviewScreen(clone(config)); s7.init(); globalThis.fetch = answer(() => ({ line: line51(), counts: {} }));
  s7.onTokClick(s7.lines[0], 1); s7.pop.typed = 'كلمة'; calls.length = 0; await s7.onTypedTab(false);
  out.tabSaves = { post: posts().pop(), focus: clone(s7.focus) };
  s7.onTokClick(s7.lines[1], 1); calls.length = 0; await s7.onTypedTab(false);
  out.tabMoves = { posts: posts().length, focus: clone(s7.focus) };
  // Esc on a confident word's prefilled correction closes the popover at once; with readings it steps back to them
  const s8 = reg.reviewScreen(clone(config)); s8.init();
  s8.onTokClick(s8.lines[1], 1); s8.closeTop(); out.escConfident = { open: s8.pop.open, typing: s8.pop.typing };
  s8.onTokClick(s8.lines[0], 1); s8.startTyping('ك'); s8.closeTop(); out.escUncertain = { open: s8.pop.open, typing: s8.pop.typing, typed: s8.pop.typed };
  // two failed saves then a success: the chip stays in error with both retries, which replay in order
  const s9 = reg.reviewScreen(clone(config)); s9.init(); globalThis.fetch = reply(500, { message: 'تعذّر' });
  s9.focusWord({ lineId: 51, index: 1 }); await s9.choose('secondary');
  s9.focusWord({ lineId: 52, index: 0 }); await s9.choose('primary');
  globalThis.fetch = reply(200, { counts: {} }); s9.focusWord({ lineId: 53, index: 1 }); await s9.choose('primary');
  out.failures = { save: s9.save.state, failed: s9.save.failed.length, bar: stores.review.bar.failed, res: [s9.lines[0].tokens[1].res, s9.lines[1].tokens[0].res] };
  calls.length = 0; s9.retrySave(); await s9.queue; await flush();
  out.retried = { save: s9.save.state, failed: s9.save.failed.length, posts: posts().map((x) => [x[1], x[2].choice]) };
  // failures do not follow a page swap (their retries target the old page): dropped, and said so
  globalThis.fetch = reply(500, { message: 'تعذّر' }); s9.focusWord({ lineId: 51, index: 4 }); await s9.choose('primary');
  globalThis.fetch = reply(200, page4()); calls.length = 0; await s9.swapTo('/api/pages/8/review/', null, 1); await flush();
  out.failedDropped = { save: s9.save.state, failed: s9.save.failed.length, toast: calls.filter((x) => x[0] === 'toast').pop()[1] };
  // one toast at a time: the shared toast replaces the undo offer, and the undo offer hides the shared toast
  const hidden = []; stores.toast = { visible: true, hide: () => hidden.push(1) };
  const s10 = reg.reviewScreen(clone(config)); s10.init(); s10.showUndoToast('حُذفت الكلمة'); const undoShown = Boolean(s10.undoToast); s10.toast('تم النسخ');
  out.oneToast = { hidShared: hidden.length, undoShown, after: s10.undoToast }; delete stores.toast;
  // the turn's direction follows the page order (back for an earlier page; N wraps forward here)
  const s11 = reg.reviewScreen(clone(config)); s11.init(); s11.film = clone(film);
  const turns = []; s11.swapTo = (url, fb, dir) => { turns.push([url, dir]); return Promise.resolve(true); };
  s11.goPrevPage(); s11.goNextPage(); s11.goTo(s11.film.items[0]); s11.goNextReview();
  out.turns = turns;
  // stale tab: a 409 carries the line as it is now; the view shows it and arms no retry (it would conflict again)
  const cf = reg.reviewScreen(clone(config)); cf.init();
  const nowLine = clone(config.lines[0]); nowLine.tokens[1].t = 'تغيّر'; nowLine.v = 'v2';
  globalThis.fetch = reply(409, { message: 'تغيّر السطر في نافذة أخرى', line: nowLine }); calls.length = 0;
  cf.focusWord({ lineId: 51, index: 1 });
  await cf.choose('secondary');
  const cfPost = calls.filter((x) => x[0] === 'POST').pop();
  out.conflict = { t: cf.lines[0].tokens[1].t, failed: cf.save.failed.length, save: cf.save.state, sentT: Boolean(cfPost && cfPost[2] && cfPost[2].t),
    toast: (calls.filter((x) => x[0] === 'toast').pop() || [])[1] || null };
  // whole-line actions carry the line's version as it is when the request leaves the queue
  cf.lines[0].v = 'v7'; globalThis.fetch = reply(200, { line: Object.assign(clone(cf.lines[0]), { role: 'heading', v: 'v8' }) }); calls.length = 0;
  await cf.setRole(cf.lines[0], 'heading');
  out.roleSent = (calls.filter((x) => x[0] === 'POST').pop() || [])[2];
  // ------------------------------------------------------------ Phase 7a (D69, D70, D73)
  // every saved change is announced on BroadcastChannel('nassakh'); a failed one is not
  const bc = reg.reviewScreen(clone(config)); bc.init(); posted.length = 0;
  globalThis.fetch = answer(() => ({ line: line51(), counts: {} }));
  bc.focusWord({ lineId: 51, index: 1 }); await bc.choose('secondary');
  bc.startEdit(bc.lines[1]); bc.edit.text = 'وفي الكتاب حكايات'; await bc.saveEdit();
  globalThis.fetch = answer(() => ({ line: Object.assign(clone(config.lines[0]), { role: 'heading' }) })); await bc.setRole(bc.lines[0], 'heading');
  globalThis.fetch = answer(() => ({ line: clone(delLine), counts: {} })); bc.focusWord({ lineId: 53, index: 2 }); await bc.deleteWord();
  globalThis.fetch = answer(() => ({ line: clone(mergedLine), counts: {} })); bc.focusWord({ lineId: 52, index: 1 }); await bc.mergeWord(1);
  globalThis.fetch = answer(() => ({ line: Object.assign(clone(config.lines[1]), { id: 60, order: 2 }), counts: {} })); bc.startInsert(bc.lines[1]); bc.insert.text = 'سطر جديد'; await bc.saveInsert();
  globalThis.fetch = answer(() => ({ deleted_id: 60, counts: {} })); await bc.removeLine(bc.lineById(60));
  globalThis.fetch = reply(500, { message: 'تعذّر' }); await bc.setRole(bc.lines[1], 'subheading');
  globalThis.fetch = answer(() => clone(config)); bc.save.failed = []; await bc.undo();
  globalThis.fetch = answer(() => ({ status: 'reviewed' })); bc.counts.unresolved = 0; bc.celebrate = () => {}; await bc.approve(true);
  globalThis.fetch = answer(() => clone(config)); await bc.reopen();
  out.channel = posted.slice();
  // «لا علامات»: Tab on a page with no uncertain words
  const nf = clone(config); nf.lines.forEach((l) => l.tokens.forEach((t) => { t.conf = 'high'; })); nf.counts = { low_total: 0, unresolved: 0, resolved: 0 };
  const nfc = reg.reviewScreen(nf); nfc.init(); calls.length = 0; nfc.move(1);
  out.noFlags = { toast: calls.filter((x) => x[0] === 'toast').pop()[1], bar: stores.review.bar.lowTotal };
  // the two modes through onKey (Arabic layout): ش types into an open menu, approves with it closed; Esc keeps the word
  const md = reg.reviewScreen(clone(config)); md.init();
  const press = (inst, ev, target) => { let prevented = false; inst.onKey(Object.assign({ target: target || { tagName: 'SPAN', closest: () => null }, preventDefault: () => { prevented = true; } }, ev)); return prevented; };
  md.focusWord({ lineId: 51, index: 1 }, { open: true });
  press(md, { key: 'ش', code: 'KeyA' });
  out.wordMode = { typing: md.pop.typing, typed: md.pop.typed, open: md.pop.open };
  md.closeTop(); md.closeTop();
  out.escKeeps = { open: md.pop.open, focus: clone(md.focus) };
  let approved = 0; md.approve = () => { approved += 1; return Promise.resolve(true); };
  press(md, { key: 'ش', code: 'KeyA' });
  out.pageMode = { approved, typing: md.pop.typing };
  press(md, { key: ' ', code: 'Space' });
  out.space = { open: md.pop.open, focus: clone(md.focus) };
  press(md, { key: '٢', code: 'Digit2' });
  out.arabicDigit = { t: md.lines[0].tokens[1].t, res: md.lines[0].tokens[1].res };
  // ⌘↵ from the correction field: a changed draft is saved first, then the page approved
  globalThis.fetch = answer(() => ({ line: line51(), counts: {} })); calls.length = 0;
  md.focusWord({ lineId: 51, index: 4 }, { open: true }); md.startTyping('1'); md.pop.typed = '1967';
  press(md, { key: 'Enter', code: 'Enter', metaKey: true }, { tagName: 'INPUT', closest: (sel) => (sel === '.rv-pop' ? {} : null) });
  await flush();
  out.cmdEnter = { approved, post: (calls.filter((x) => x[0] === 'POST').pop() || [])[2], open: md.pop.open };
  // ⌘↵ in the line editor (a field outside the word menu) stays the field's
  md.startEdit(md.lines[1]); const before = approved;
  press(md, { key: 'Enter', metaKey: true }, { tagName: 'TEXTAREA', closest: () => null });
  out.cmdEnterInEditor = approved - before;
  // Home / End: the first and last page of the filmstrip
  const he = reg.reviewScreen(clone(config)); he.init(); he.film = clone(film); const went = []; he.goTo = (item) => went.push(item.number);
  he.goEdgePage(-1); he.goEdgePage(1);
  out.edges = went;
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_review_component_navigation_optimistic_saves_undo_approve_and_keys(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    fixture = tmp_path / "config.json"
    fixture.write_text(json.dumps(_config(), ensure_ascii=False), encoding="utf-8")
    run = subprocess.run(
        ["node", str(harness), str(JS / "review.js"), str(fixture), str(JS / "keys.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])

    assert out["bar"] == {"number": 3, "total": 120, "resolved": 1, "lowTotal": 5, "canApprove": True}
    # D31: a confident word opens the popover with merge / delete (no readings rows)
    assert out["wordPop"] == {
        "open": True,
        "options": 0,
        "acts": {"next": True, "prev": True, "del": True},
        "next": "الكتابحكاية",
        "prev": "وَفِيالكتاب",
    }
    assert out["prefill"] == {"typing": True, "typed": "الكتاب"}
    # D32: outside click closes the popover (a scan drag's release does not), and clears the focus
    assert out["outside"]["afterDrag"] == {"open": True, "dragMoved": False}
    assert out["outside"]["afterClick"] == {"open": False, "focus": None}
    # placement: below the word, flipped above near the bottom, kept 8 px inside the visible column
    place = out["place"]
    assert place["middle"] == {"style": "top:238px; right:240px;", "above": False}
    assert place["bottom"] == {"style": "top:392px; right:240px;", "above": True}
    assert place["leftEdge"] == {"style": "top:238px; right:440px;", "above": False}  # left edge at 388
    assert place["rightEdge"] == {"style": "top:238px; right:-12px;", "above": False}  # right edge at 1112
    more = out["more"]
    assert more["roomLeft"] == {"side": "left", "style": "top:144px;"}
    assert more["roomRight"] == {"side": "right", "style": "top:144px;"}
    assert more["narrow"] == {"side": "below", "style": ""}  # neither side has room: folded in
    assert more["low"] == {"side": "left", "style": "top:42px;"}  # shifted up to stay above the bottom
    assert out["moreHover"] == {"opened": True, "closedAfter": 180, "closed": True}
    assert out["moreEsc"] == {"more": False, "popStillOpen": True}  # Esc closes the submenu first
    # D32: line roles
    role = out["role"]
    assert role["role"] == "heading" and role["cls"] is True and role["label"] == "عنوان رئيسي"
    assert role["call"][1] == "/api/lines/51/role/" and role["call"][2] == {"role": "heading"}
    assert out["roleRollback"] == {"role": "body", "save": "error"}
    assert out["keyGuard"] == {"enter": False, "backspace": False, "arrowLeft": False, "words": 3, "posts": 0}
    mo = out["mergeOptimistic"]
    assert mo["words"] == ["وَفِي", "الكتابحكاية"] and mo["bbox"] == [620, 170, 890, 230]
    assert (
        mo["text"] == "وَفِي الكتابحكاية"
        and mo["focus"] == {"lineId": 52, "index": 1}
        and mo["popOpen"] is False
    )
    assert out["mergeCall"][1] == "/api/lines/52/merge/"
    assert (
        out["conflict"]["t"] == "تغيّر" and out["conflict"]["failed"] == 0 and out["conflict"]["sentT"] is True
    )
    assert out["conflict"]["save"] != "error"
    assert out["roleSent"] == {"role": "heading", "v": "v7"}
    # the two words the reviewer saw travel with the merge, so a stale tab gets a 409 instead of a wrong merge
    assert out["mergeCall"][2] == {"index": 1, "t": "الكتاب", "t_next": "حكاية"}
    assert out["mergeRolledBack"] == {"same": True, "save": "error"}
    assert out["deleted"]["words"] == ["(1)", "انظر"] and out["deleted"]["toast"] == "حُذفت الكلمة"
    assert out["deleted"]["call"][1] == "/api/lines/53/delete-word/" and out["deleted"]["call"][2] == {
        "index": 2,
        "t": "المصدر",
    }
    assert out["deletedLine"] == {"ids": [51, 53], "call": "/api/lines/52/delete/"}
    assert out["keysD31"] == {
        "mergeNext": "mergeNext",
        "mergePrev": "mergePrev",
        "del": "deleteWord",
        "del2": "deleteWord",
        "delUnfocused": None,
        "delInField": None,
        "altArrowUnfocused": None,
    }
    # fit height is the default; the whole page height fits the pane, the sheet is centred horizontally
    assert out["fitDefault"] == "height"
    fh = out["fitHeight"]
    assert fh["h"] == 600 and fh["w"] == 400 and fh["x"] == 200 and fh["y"] == 0
    assert fh["style"].startswith("width:400px; transform: translate(200px, 0px) scale(1)")
    assert out["fitHeightPan"] == {"x": 200, "y": 0}
    # fit width: page width fills the pane; box percentages are identical in both modes
    fw = out["fitWidth"]
    assert fw["fit"] == "width" and fw["w"] == 800 and fw["h"] == 1200 and fw["scale"] == 1
    assert fw["box"] == fh["box"] and fw["label"] == "ملاءمة العرض"
    # pan uses the sheet width: the bottom word ends up inside the pane
    assert 0 < out["fitWidthPan"]["wordCentreOnPane"] < 600 and out["fitWidthPan"]["y"] < 0
    # unresolved words in reading order (the already resolved «(1)» is skipped),
    # Tab wraps, Shift+Tab goes back
    assert out["order"] == [[51, 1], [51, 4], [52, 0], [53, 1]]
    assert out["nav"] == [[51, 1], [51, 4], [53, 1], [51, 1], [53, 1]]
    assert out["popOpenOnFocus"] is True
    assert out["optionKeysNoTess"] == [["1", "primary"], ["2", "secondary"]]
    assert out["optionKeys"] == [["1", "primary"], ["2", "secondary"], ["3", "tess"]]
    assert out["krakenOptions"] == [["1", "primary", "Kraken"]]
    assert out["letterOptions"] == [
        ["1", "primary", "Kraken", "(٥)"],
        ["2", "secondary", "Qari v0.3", "(ه)"],  # Qari's own letter, under the primary model's label
    ]
    # after a secondary choice the primary reading survives in `orig`: all three rows, secondary current
    assert out["resolvedOptions"] == [
        ["primary", True, False],
        ["secondary", True, True],
        ["tess", True, False],
    ]
    # choosing: the word changes at once, the counter moves, focus jumps to the next unresolved word
    assert out["optimistic"]["t"] == "الامير" and out["optimistic"]["res"] == "secondary"
    assert out["optimistic"]["counts"] == {"low_total": 5, "unresolved": 3, "resolved": 2}
    assert out["optimistic"]["save"] == "saving" and out["optimistic"]["focus"] == {"lineId": 51, "index": 4}
    assert out["optimistic"]["flash"] == ["51-1"]
    assert out["settled"] == {
        "save": "saved",
        "counts": {"low_total": 5, "unresolved": 3, "resolved": 2},
        "bookUnresolved": 379,
        "nLow": 1,
    }
    assert out["resolveCall"][:2] == ["POST", "/api/lines/51/resolve/"]
    assert out["resolveCall"][2]["index"] == 1 and out["resolveCall"][2]["choice"] == "secondary"
    assert out["resolveCall"][2]["t"]  # the word as seen before the change
    # failure: rolled back, chip in error with a retry, Arabic toast
    assert out["rolledBack"]["t"] == "وَفِي" and out["rolledBack"]["res"] is None
    assert out["rolledBack"]["counts"] == {"low_total": 5, "unresolved": 3, "resolved": 2}
    assert (
        out["rolledBack"]["save"] == "error"
        and out["rolledBack"]["failed"] == 1
        and out["rolledBack"]["retry"] == "function"
        and out["rolledBack"]["toast"] == "تعذّر"
    )
    # undo re-renders from the payload
    assert out["undone"] == {
        "lines": 2,
        "counts": {"low_total": 3, "unresolved": 2, "resolved": 1},
        "save": "saved",
        "focus": None,
    }
    # approve: client-side confirm without a request; server 409 opens the dialog with its count
    assert out["clientDialog"] == {"open": True, "count": 2, "posts": 0}
    assert out["serverDialog"]["open"] is True and out["serverDialog"]["count"] == 4
    assert (
        out["serverDialog"]["save"] == "idle"
        and out["serverDialog"]["approving"] is False
        and out["serverDialog"]["body"] == {"force": False}
    )
    # approve success swaps the next page in from its payload URL
    assert out["approved"]["page"] == 4 and out["approved"]["counts"] == {
        "low_total": 2,
        "unresolved": 2,
        "resolved": 0,
    }
    assert (
        out["approved"]["reviewedPages"] == 14
    )  # the next page's payload (its own book counts) replaced the local +1
    assert "/api/pages/8/review/" in out["approved"]["fetched"]
    # keyboard map
    keys = out["keys"]
    assert keys["left"] == "nextPage" and keys["right"] == "prevPage"
    assert keys["tab"] == "next" and keys["shiftTab"] == "prev" and keys["tabOutside"] is None
    assert (
        keys["undo"] == "undo"
        and keys["sheet"] == "sheet"
        and keys["enter"] == "accept"
        and keys["altEnter"] == "insert"
    )
    # no third reading: in the word menu the digit starts a correction; with the menu closed it does nothing
    assert keys["one"] == "choose1" and keys["three"] == "type" and keys["threePage"] is None
    assert keys["letter"] == "type" and keys["letterPage"] is None and keys["letterUnfocused"] is None
    assert keys["edit"] == "edit" and keys["approve"] == "approve" and keys["next"] == "nextReview"
    assert keys["zoom"] == ["zoomIn", "zoomOut", "zoomReset"]
    assert keys["inField"] is None and keys["escInField"] == "close"
    assert keys["undoInField"] is None and keys["redo"] is None  # a field keeps its native ⌘Z
    assert out["copy"] == "قال الأمير في سنة 1966\nوَفِي الكتاب حكاية\n\n(1) انظر المصدر"
    # --- Phase 3 review fixes
    # a page swap waits for the queued actions, so a late resolve lands on its own page
    assert out["swapWaits"] == {"reqs": ["/api/lines/51/resolve/"], "loading": True}
    assert out["afterSwap"] == {
        "reqs": ["/api/lines/51/resolve/", "/api/pages/8/review/"],
        "page": 4,
        "id": 8,
        "lines": [91],
        "counts": {"low_total": 0, "unresolved": 0, "resolved": 0},
        "save": "saved",
        "undo": "/api/pages/8/undo/",
    }
    # a request of the old page that fails after the swap: no rollback onto the new page, no stale retry
    assert out["lateFail"] == {
        "page": 4,
        "counts": {"low_total": 0, "unresolved": 0, "resolved": 0},
        "lines": [91],
        "save": "idle",
        "failed": 0,
        "toast": "تعذّر",
    }
    assert out["latePoll"] == {"page": 4, "id": 8, "lines": [91]}
    # ⌘Z in an editor is the field's own undo
    assert out["cmdZInEditor"] == {"prevented": False, "draft": "مسودة", "posts": 0}
    # the undo toast never outlives a newer action
    assert out["undoPipelined"] == {"toast": None, "save": "saved"}
    assert out["undoRetired"] == {"offered": "حُذفت الكلمة", "after": None}
    # Enter confirms the current reading
    assert out["acceptResolved"] == {
        "t": "الامير",
        "res": "secondary",
        "posts": 0,
        "focus": {"lineId": 51, "index": 4},
    }
    assert out["rechooseCurrent"] == {"posts": 0, "focus": {"lineId": 51, "index": 4}}

    def unseen(call):  # the request without `t`, the word as the reviewer saw it (stale-tab check)
        assert call[2].get("t"), call
        return [call[0], call[1], {k: v for k, v in call[2].items() if k != "t"}]

    assert unseen(out["acceptUnresolved"]) == [
        "POST",
        "/api/lines/51/resolve/",
        {"index": 4, "choice": "primary"},
    ]
    assert unseen(out["acceptChooser"]) == [
        "POST",
        "/api/lines/52/resolve/",
        {"index": 0, "choice": "secondary"},
    ]
    # Tab in the correction input
    assert unseen(out["tabSaves"]["post"]) == [
        "POST",
        "/api/lines/51/resolve/",
        {"index": 1, "choice": "typed", "text": "كلمة"},
    ]
    assert out["tabSaves"]["focus"] == {"lineId": 51, "index": 4}
    assert out["tabMoves"] == {"posts": 0, "focus": {"lineId": 53, "index": 1}}
    # Esc in the correction input
    assert out["escConfident"] == {"open": False, "typing": False}
    assert out["escUncertain"] == {"open": True, "typing": False, "typed": ""}
    # failed saves are kept, counted on the chip, replayed in order, and dropped (with a word) on a page swap
    assert out["failures"] == {"save": "error", "failed": 2, "bar": 2, "res": [None, None]}
    assert out["retried"] == {
        "save": "saved",
        "failed": 0,
        "posts": [["/api/lines/51/resolve/", "secondary"], ["/api/lines/52/resolve/", "primary"]],
    }
    assert out["failedDropped"] == {
        "save": "idle",
        "failed": 0,
        "toast": "لم تُحفظ بعض الإجراءات في الصفحة التي غادرتها",
    }
    # one toast at a time
    assert out["oneToast"] == {"hidShared": 1, "undoShown": True, "after": None}
    # --- Phase 7a
    # D70: a message on the channel after every saved change (resolve, edit, role, drop word, merge, insert,
    # delete a line, undo, approve, reopen); the failed role change posts nothing
    msg = ["nassakh", {"type": "review", "book": 1, "page": 3}]
    assert out["channel"] == [msg] * 10
    # D73: no uncertain words → «لا علامات» on Tab
    assert out["noFlags"] == {"toast": "لا علامات في هذه الصفحة؛ اقرأ الأسطر مع الصورة ثم اعتمدها.", "bar": 0}
    # D69: the word menu decides the mode
    assert out["wordMode"] == {"typing": True, "typed": "ش", "open": True}
    assert out["escKeeps"] == {"open": False, "focus": {"lineId": 51, "index": 1}}
    assert out["pageMode"] == {"approved": 1, "typing": False}
    assert out["space"] == {"open": True, "focus": {"lineId": 51, "index": 1}}
    assert out["arabicDigit"] == {"t": "الامير", "res": "secondary"}  # «٢» chose the second reading
    assert out["cmdEnter"]["approved"] == 2 and out["cmdEnter"]["open"] is False
    assert out["cmdEnter"]["post"]["choice"] == "typed" and out["cmdEnter"]["post"]["text"] == "1967"
    assert out["cmdEnterInEditor"] == 0
    assert out["edges"] == [2, 4]
    # direction-aware page turns
    assert out["turns"] == [
        ["/api/pages/6/review/", -1],
        ["/api/pages/8/review/", 1],
        ["/api/pages/6/review/", -1],
        ["/api/pages/8/review/", 1],
    ]


def test_review_popover_keeps_merge_and_delete_in_a_second_level_menu():
    """D31/D32: correction is the main action; merge / delete sit in the «إجراءات أخرى» submenu."""
    body = _render()
    assert '@click.outside="onPopOutside()"' in body and "is-above" in body
    assert 'class="rv-fly"' in body and "إجراءات أخرى" in body and 'aria-haspopup="menu"' in body
    assert '@mouseenter="openMore()"' in body and '@mouseleave="closeMoreSoon()"' in body
    assert "mergeWord(1)" in body and "mergeWord(-1)" in body and "deleteWord()" in body
    assert "mergePreview(1)" in body and "دمج مع التالية" in body and "دمج مع السابقة" in body
    assert "حذف الكلمة" in body
    # the correction form comes before the «إجراءات أخرى» row
    assert body.index('class="rv-typed"') < body.index("إجراءات أخرى")
    # the line menu marks what a line is (D74: on footnote lines too, and on a ⇧-click range)
    assert (
        "نوع السطر" in body
        and "setMenuRole(line, r.value)" in body
        and "menuRoleChecked(line, r.value)" in body
    )
    assert "line.region_kind !== 'footnote'" not in body and 'x-text="roleMenuLabel(line)"' in body
    assert 'role="menuitemradio"' in body and "roleLabel(line)" in body
    # the shortcut sheet lists the keys and the menus
    assert "دمج الكلمة مع التالية / السابقة" in body
    assert "نوع السطر: محتوى، عنوان رئيسي، عنوان فرعي، شعر، حاشية" in body and "⇧ + نقرة على سطر" in body


def test_no_template_uses_a_multiline_comment_tag():
    """Django's {# #} comments end at the line end; a multi-line one would render as visible page text."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "templates"
    offenders = [
        f"{path.relative_to(root)}:{n}"
        for path in root.rglob("*.html")
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "{#" in line and "#}" not in line
    ]
    assert offenders == [], f"use {{% comment %}} for multi-line comments: {offenders}"


# ------------------------------------------------ 7b: trust in the text (PHASE7_SPEC §4.1–§4.4, D71–D74)


def _ttok(t, why=None, **keys):
    """A token of the 7b contract (review/fixtures/trust/index.json): low exactly when `why` is set."""
    tok = {"t": t, "alt": None, "tess": None, "conf": "low" if why else "high", "digit": False, "bbox": None}
    tok["res"] = None
    if why:
        tok["why"] = why
    tok.update(keys)
    return tok


def _gap(gap_id, index, after_t, text, status="open", support=0.4):
    return {
        "id": gap_id,
        "index": index,
        "after_t": after_t,
        "text": text,
        "support": support,
        "status": status,
    }


def _tline(line_id, order, tokens, region_kind="body", role="body", kind=None, gaps=()):
    footnote = role == "footnote" or (role == "body" and region_kind == "footnote")
    return {
        "id": line_id,
        "order": order,
        "region_id": 40 if region_kind == "body" else 41,
        "region_kind": region_kind,
        "kind": kind or ("footnote" if footnote else "body"),
        "bbox": [80, 100 + order * 60, 1000, 150 + order * 60],
        "text": " ".join(tok["t"] for tok in tokens),
        "ocr_text": " ".join(tok["t"] for tok in tokens),
        "is_manual": False,
        "is_reviewed": False,
        "n_low": 0,
        "role": role,
        "v": f"v{line_id}",
        "tokens": tokens,
        "gaps": list(gaps),
    }


def _trust_config() -> dict:
    """Page 900 of «كتاب الثقة» in the 7b payload shape: one of each flag reason (D71), a group of added words
    over two lines and three suggestions (D72, one of them dismissed), a page one model read (D73), and lines
    whose kind a role or the region decides (D74). 9 open words + 1 group + 2 open gaps = 12 open items."""
    box = [520, 100, 610, 150]
    lines = [
        _tline(
            101,
            0,
            [
                _ttok("قال"),
                _ttok(
                    "يحيى", ["disagree"], alt="يحيى", orig="يجي", tess="يحبى", pick="vote", tc=81, bbox=box
                ),
                _ttok("الأمين", ["disagree"], alt="الأمير", tess="الامين", tc=88),
                _ttok("وكان", ["disagree"], alt="فكان", tess="ركان", tc=64),
                _ttok("1966", ["number"], alt="1965", digit=True),
                _ttok("مилادية", ["script"], alt="ميلادية"),
            ],
        ),
        _tline(
            102,
            1,
            [
                _ttok("※", ["script"]),
                _ttok("فقيهاً", ["alone"]),
                _ttok("ونشأ"),
                _ttok("تعالى", ["missing"], ins=1),
                _ttok("بطرابلس", ["missing"], ins=1),
            ],
        ),
        _tline(
            103,
            2,
            [
                _ttok("واخذ", ["missing"], ins=1),
                _ttok("عن", ["missing"], ins=1),
                _ttok("جماعة"),
                _ttok("رحمه"),
            ],
            gaps=[_gap(9, 0, "واخذ", "و", status="dismissed"), _gap(7, 2, "جماعة", "من الفضلاء")],
        ),
        _tline(
            104,
            3,
            [_ttok("(1)"), _ttok("انظر", ["single"], tess="أنظر", tc=91)],
            region_kind="footnote",
            gaps=[_gap(8, -1, "", "الاجابة", support=0.5)],
        ),
        _tline(
            105,
            4,
            [
                _ttok("هامش"),
                _ttok(
                    "٢٤٢",
                    ["number", "year"],
                    digit=True,
                    src="kraken",
                    sug={"t": "٢٤٣", "src": "words", "label": "من الحروف", "words": "ثلاث واربعين ومايتين"},
                ),
            ],
            role="footnote",
        ),
    ]
    return {
        "page": {
            "id": 900,
            "number": 1,
            "book_id": 30,
            "status": "ocr_done",
            "status_label": "تم التعرّف",
            "is_reviewed": False,
            "text_state": "final",
            "printed_number": "",
            "n_unresolved": 12,
            "reading": {"readers": "one", "partial": False, "groups": 1, "gaps": 3},
            "error": "",
            "error_from": "",
        },
        "book": {
            "id": 30,
            "title": "كتاب الثقة",
            "total_pages": 3,
            "reviewed_pages": 0,
            "unresolved_total": 20,
        },
        "image": {"display_url": "/d.webp", "scan_url": "/s.png", "width": 1106, "height": 1634},
        "regions": [],
        "lines": lines,
        "counts": {"low_total": 13, "unresolved": 12, "resolved": 1, "words": 9, "groups": 1, "gaps": 2},
        "labels": {"primary": "Qari v0.3", "secondary": "Qari v0.2"},
        "nav": {
            "prev_url": None,
            "next_url": "/books/30/review/2/",
            "next_review_url": "",
            "dashboard_url": "",
        },
        "urls": {
            "payload": "/api/pages/900/review/",
            "resolve": "/api/lines/__id__/resolve/",
            "edit": "/api/lines/__id__/edit/",
            "delete": "/api/lines/__id__/delete/",
            "merge": "/api/lines/__id__/merge/",
            "role": "/api/lines/__id__/role/",
            "delete_word": "/api/lines/__id__/delete-word/",
            "insert": "/api/pages/900/lines/",
            "undo": "/api/pages/900/undo/",
            "approve": "/api/pages/900/approve/",
            "reopen": "/api/pages/900/reopen/",
            "filmstrip": "/api/books/30/filmstrip/",
            "insertion": "/api/pages/900/insertions/__group__/",
            "gap_accept": "/api/gaps/__id__/accept/",
            "gap_dismiss": "/api/gaps/__id__/dismiss/",
            "roles": "/api/pages/900/roles/",
        },
        "can_edit": True,
    }


TRUST_HARNESS = r"""
const reg = {}; const inits = []; const stores = {}; const calls = [];
globalThis.window = globalThis;
globalThis.document = { hidden: false, addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); },
  getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: (n, v) => { if (v !== undefined) stores[n] = v; return stores[n]; } };
globalThis.setTimeout = (fn) => 1; globalThis.clearTimeout = () => {};
globalThis.Nassakh = { toast: (m) => calls.push(['toast', m]), copyText: () => true };
globalThis.BroadcastChannel = class { postMessage() {} };
const fs = require('fs');
const config = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
eval(fs.readFileSync(process.argv[4], 'utf8')); // keys.js (NassakhKeys)
eval(fs.readFileSync(process.argv[2], 'utf8'));
inits.forEach((fn) => fn());
const clone = (v) => JSON.parse(JSON.stringify(v));
const flush = () => new Promise((r) => setImmediate(r));
// the server: `answer(fn)` replies 200 with fn(url, body); every request is recorded
const answer = (fn, status = 200) => async (url, init) => { const body = init && init.body ? JSON.parse(init.body) : null; calls.push([init && init.method || 'GET', url, body]); return { ok: status < 400, status, json: async () => clone(fn(url, body)) }; };
const posts = () => calls.filter((x) => x[0] === 'POST').map((x) => [x[1], x[2]]);
const toasts = () => calls.filter((x) => x[0] === 'toast').map((x) => x[1]);
const make = (cfg) => { const c = reg.reviewScreen(clone(cfg || config)); c.init(); calls.length = 0; return c; };
const at = (c, id) => c.lineById(id);
const words = (c, id) => at(c, id).tokens.map((t) => t.t);
const refOf = (f) => (f ? (f.gap != null ? ['gap', f.gap] : [f.lineId, f.index]) : null);
const press = (c, ev, target) => { let prevented = false; c.onKey(Object.assign({ target: target || { tagName: 'SPAN', closest: () => null }, preventDefault: () => { prevented = true; } }, ev)); return prevented; };
const R = window.NassakhReview;
(async () => {
  const out = {};
  // ---- D71: the reason line of every flag, and the one in the text marked «● في النص»
  const c = make();
  const reason = (id, i) => { c.focusWord({ lineId: id, index: i }, { open: true }); return [c.popKind, c.popTitle, c.popReason]; };
  out.reasons = {
    vote: reason(101, 1), v03: reason(101, 2), disagree: reason(101, 3), number: reason(101, 4), letters: reason(101, 5),
    symbol: reason(102, 0), alone: reason(102, 1), single: reason(104, 1), year: reason(105, 1), sure: reason(101, 0),
  };
  // a kept word of the group reads its own reason; a number stored before 7b (no `why`) keeps the number's line
  out.reasonMissing = c.reasonOf(Object.assign(clone(config.lines[1].tokens[3]), { res: 'secondary' }));
  out.reasonOldNumber = c.reasonOf({ t: '12', conf: 'low', digit: true, alt: '13', res: null });
  out.reasonOldWord = c.reasonOf({ t: 'قال', conf: 'low', alt: 'قيل', res: null });
  c.focusWord({ lineId: 101, index: 1 }, { open: true });
  out.voteOptions = c.options().map((o) => [o.key, o.choice, o.value, o.label, o.current]);
  c.focusWord({ lineId: 105, index: 1 }, { open: true });
  out.yearOptions = c.options().map((o) => [o.key, o.choice, o.value, o.label]);
  // Enter confirms the reading in the text: Qari v0.2's for the vote (choice `secondary`), v0.3's otherwise
  globalThis.fetch = answer(() => ({ line: null, counts: null }));
  c.focusWord({ lineId: 101, index: 1 }, { open: true });
  press(c, { key: 'Enter', code: 'Enter' }); await flush();
  out.enterVote = { post: posts().pop(), t: at(c, 101).tokens[1].t, res: at(c, 101).tokens[1].res, orig: at(c, 101).tokens[1].orig, next: refOf(c.focus), counts: clone(c.counts) };
  press(c, { key: 'Enter', code: 'Enter' }); await flush();
  out.enterPlain = { post: posts().pop(), res: at(c, 101).tokens[2].res, t: at(c, 101).tokens[2].t };

  // ---- D72: Tab visits the words, one stop per group and the open gaps, in reading order; ⇧Tab goes back
  const tb = make();
  const order = [];
  for (let i = 0; i < 13; i += 1) { press(tb, { key: 'Tab', code: 'Tab' }); order.push(refOf(tb.focus)); }
  out.tabOrder = order;
  tb.focusWord({ lineId: 102, index: 4 }, { open: true }); // the group's last word on its first line
  press(tb, { key: 'Tab', code: 'Tab' }); out.tabFromGroup = refOf(tb.focus);
  press(tb, { key: 'Tab', code: 'Tab', shiftKey: true }); out.shiftTabToGroup = refOf(tb.focus);
  press(tb, { key: 'Tab', code: 'Tab', shiftKey: true }); out.shiftTabBeforeGroup = refOf(tb.focus);
  out.stops = tb.unresolvedRefs().length;

  // ---- the group's popover: keep (Enter) or drop, with the group's count; its words carry the dotted underline
  const g = make();
  g.focusWord({ lineId: 103, index: 1 }, { open: true });
  out.group = { kind: g.popKind, title: g.popTitle, reason: g.popReason, size: g.groupSize, keep: g.groupCountLabel('إبقاء الكلمات'), drop: g.groupCountLabel('حذف الكلمات'),
    options: g.options().length, cls: [g.tokClass(at(g, 102), at(g, 102).tokens[3], 3), g.tokClass(at(g, 102), at(g, 102).tokens[2], 2)].map((x) => Object.keys(x).filter((k) => x[k]).sort()),
    sep: [g.sepClass(at(g, 102), 3), g.sepClass(at(g, 102), 2), g.sepClass(at(g, 103), 1)], box: Object.keys(g.boxClass(at(g, 102), at(g, 102).tokens[3], 3)).filter((k) => g.boxClass(at(g, 102), at(g, 102).tokens[3], 3)[k]) };
  const ka = R.keyAction;
  const kctx = { inField: false, inFlow: true, focused: true, open: true, optionKeys: [], group: true };
  out.groupKeys = { back: ka({ key: 'Backspace' }, kctx), letter: ka({ key: 'ب', code: 'KeyF' }, kctx), enter: ka({ key: 'Enter' }, kctx), digit: ka({ key: '1', code: 'Digit1' }, kctx) };
  const kept = (url, body) => {
    const l2 = clone(config.lines[1]); const l3 = clone(config.lines[2]);
    [l2.tokens[3], l2.tokens[4], l3.tokens[0], l3.tokens[1]].forEach((t) => { t.res = 'secondary'; });
    l2.n_low = 2; l3.n_low = 0; l2.v = 'v102b'; l3.v = 'v103b';
    return { lines: [l2, l3], deleted_ids: [], order: config.lines.map((l) => ({ id: l.id, order: l.order })), counts: { line_n_low: 0, page_unresolved: 11, page_low_total: 13, book_unresolved_total: 19, page_words: 9, page_groups: 0, page_gaps: 2 }, page: { n_unresolved: 11 } };
  };
  globalThis.fetch = answer(kept);
  press(g, { key: 'Enter', code: 'Enter' });
  out.keepOptimistic = { res: [at(g, 102).tokens[3].res, at(g, 102).tokens[4].res, at(g, 103).tokens[0].res, at(g, 103).tokens[1].res], counts: clone(g.counts), focus: refOf(g.focus), popKind: g.popKind, save: g.save.state };
  await flush(); await flush();
  out.keepSettled = { post: posts().pop(), counts: clone(g.counts), v: [at(g, 102).v, at(g, 103).v], nUnresolved: g.page.n_unresolved, save: g.save.state, groupClass: g.tokClass(at(g, 102), at(g, 102).tokens[3], 3)['is-group'] };

  // drop: the group's words leave both lines, a gap after them moves back with its word; one ⌘Z undoes it
  const d = make();
  d.focusWord({ lineId: 102, index: 3 }, { open: true });
  const dropped = () => { const l2 = clone(config.lines[1]); const l3 = clone(config.lines[2]); l2.tokens.splice(3, 2); l3.tokens.splice(0, 2); l3.gaps[1].index = 0; l3.gaps[0].index = -1;
    return { lines: [l2, l3], deleted_ids: [], order: config.lines.map((l) => ({ id: l.id, order: l.order })), counts: { line_n_low: 0, page_unresolved: 11, page_low_total: 12, book_unresolved_total: 19, page_words: 9, page_groups: 0, page_gaps: 2 }, page: { n_unresolved: 11 } }; };
  globalThis.fetch = answer(dropped);
  const dp = d.dropGroup();
  out.dropOptimistic = { l102: words(d, 102), l103: words(d, 103), gaps: at(d, 103).gaps.map((x) => [x.id, x.index, x.status]), counts: clone(d.counts), focus: refOf(d.focus) };
  await dp; await flush();
  out.dropSettled = { post: posts().pop(), toast: d.undoToast && d.undoToast.message, lines: d.lines.map((l) => l.id) };
  // a group that fills a whole line: the line goes with it (and the answer's `deleted_ids` confirms it)
  const whole = clone(config); whole.lines[2].tokens = whole.lines[2].tokens.slice(0, 2); whole.lines[2].gaps = [];
  const w = make(whole);
  w.focusWord({ lineId: 103, index: 0 }, { open: true });
  globalThis.fetch = answer(() => { const l2 = clone(config.lines[1]); l2.tokens.splice(3, 2); return { lines: [l2], deleted_ids: [103], order: [101, 102, 104, 105].map((id, i) => ({ id, order: i })), counts: { line_n_low: 0, page_unresolved: 10, page_low_total: 11, book_unresolved_total: 18, page_words: 9, page_groups: 0, page_gaps: 1 } }; });
  const wp = w.dropGroup();
  out.dropLineOptimistic = w.lines.map((l) => l.id);
  await wp; await flush();
  out.dropLine = { lines: w.lines.map((l) => [l.id, l.order]), post: posts().pop(), counts: clone(w.counts) };

  // ---- a gap ▏: its words, editable, inserted by Enter («إدراج»), or dismissed by ⌫ («تجاهل»)
  const gp = make();
  gp.onGapClick(at(gp, 103), at(gp, 103).gaps[1]);
  out.gap = { kind: gp.popKind, title: gp.popTitle, reason: gp.popReason, typed: gp.pop.typed, open: gp.pop.open, focus: clone(gp.focus), offer: gp.gapOffer, drawn: [gp.gapsAt(at(gp, 103), 0).length, gp.gapsAt(at(gp, 103), 2).map((x) => x.id), gp.gapsAt(at(gp, 104), -1).map((x) => x.id)], label: gp.gapTitle(at(gp, 103).gaps[1]), options: gp.options().length };
  const gctx = { inField: false, inFlow: true, focused: true, open: true, optionKeys: [], gap: true };
  out.gapKeys = { back: ka({ key: 'Backspace' }, gctx), del: ka({ key: 'Delete' }, gctx), enter: ka({ key: 'Enter' }, gctx), letter: ka({ key: 'ب', code: 'KeyF' }, gctx), merge: ka({ key: 'ArrowLeft', altKey: true }, gctx), space: ka({ key: ' ', code: 'Space' }, Object.assign({}, gctx, { open: false })) };
  globalThis.fetch = answer((url, body) => { const l3 = clone(config.lines[2]); l3.tokens.splice(3, 0, { t: 'من', alt: null, tess: null, conf: 'high', digit: false, bbox: null, res: 'typed' }, { t: 'الفضلاء', alt: null, tess: null, conf: 'high', digit: false, bbox: null, res: 'typed' }); l3.gaps[1].status = 'inserted';
    return { line: l3, gap: { id: 7, status: 'inserted' }, counts: { line_n_low: 2, page_unresolved: 11, page_low_total: 13, book_unresolved_total: 19, page_words: 9, page_groups: 1, page_gaps: 1 }, page: { n_unresolved: 11 } }; });
  press(gp, { key: 'Enter', code: 'Enter' });
  out.gapInsertOptimistic = { words: words(gp, 103), status: at(gp, 103).gaps[1].status, counts: clone(gp.counts), focus: refOf(gp.focus), flash: Object.keys(gp.flashing).sort() };
  await flush(); await flush();
  out.gapInsert = { post: posts().pop(), words: words(gp, 103), gaps: at(gp, 103).gaps.map((x) => [x.id, x.status]), drawn: gp.gapsAt(at(gp, 103), 2).length };
  // typed words travel as `text`: a key starts them afresh in the gap's field, Esc brings the offer back
  const gt = make();
  gt.focusWord({ lineId: 104, index: -1, gap: 8 }, { open: true });
  press(gt, { key: 'ا', code: 'KeyH' });
  out.gapTyping = { typing: gt.pop.typing, typed: gt.pop.typed };
  gt.closeTop(); out.gapEsc = { typing: gt.pop.typing, typed: gt.pop.typed, open: gt.pop.open };
  gt.startTyping('ا'); gt.pop.typed = 'الإجابة  الصحيحة';
  globalThis.fetch = answer(() => ({ line: null, gap: { id: 8, status: 'inserted' }, counts: null }));
  gt.acceptGap(gt.pop.typed); await flush();
  out.gapTyped = { post: posts().pop(), words: words(gt, 104) };
  // ⌫ dismisses: the text stays, the offer closes, «تراجع» is offered
  const gd = make();
  gd.focusWord({ lineId: 104, index: -1, gap: 8 }, { open: true });
  globalThis.fetch = answer(() => { const l4 = clone(config.lines[3]); l4.gaps[0].status = 'dismissed'; return { line: l4, gap: { id: 8, status: 'dismissed' }, counts: { line_n_low: 1, page_unresolved: 11, page_low_total: 13, book_unresolved_total: 19, page_words: 9, page_groups: 1, page_gaps: 1 } }; });
  press(gd, { key: 'Backspace', code: 'Backspace' });
  out.gapDismissOptimistic = { status: at(gd, 104).gaps[0].status, words: words(gd, 104), counts: clone(gd.counts), focus: refOf(gd.focus) };
  await flush(); await flush();
  out.gapDismiss = { post: posts().pop(), toast: gd.undoToast && gd.undoToast.message, drawn: gd.gapsAt(at(gd, 104), -1).length };
  // a failed dismissal puts the gap back
  const gf = make(); gf.focusWord({ lineId: 104, index: -1, gap: 8 }, { open: true });
  globalThis.fetch = answer(() => ({ detail: 'تعذّر' }), 500);
  await gf.dismissGap();
  out.gapDismissFailed = { status: at(gf, 104).gaps[0].status, save: gf.save.state, counts: clone(gf.counts) };
  // an edit that moves words shifts the line's gaps with them (the server's answer then places them)
  const ge = make(); ge.focusWord({ lineId: 103, index: 0 }, { open: true });
  globalThis.fetch = answer(() => ({ line: null, counts: null }));
  ge.deleteWord(); out.gapShift = at(ge, 103).gaps.map((x) => [x.id, x.index]);

  // ---- D73: the pill and the banner of a page one model read; none for two readers or a page read before 7b
  const rd = (reading) => { const cfg = clone(config); cfg.page.reading = reading; const x = make(cfg); return x.readersInfo ? [x.readers, x.readersInfo.label, x.readersInfo.tone, x.readersInfo.banner] : [x.readers, null]; };
  out.readers = { one: rd({ readers: 'one' }), tesseract: rd({ readers: 'tesseract', partial: false }), two: rd({ readers: 'two' }), before: rd({}) };
  out.marksLabel = [make().marksLabel, R.arCount(1, ['علامة واحدة', 'علامتان', 'علامات', 'علامة']), R.arCount(2, ['علامة واحدة', 'علامتان', 'علامات', 'علامة']), R.arCount(3, ['علامة واحدة', 'علامتان', 'علامات', 'علامة'])];
  // the filmstrip: the half-disc and the thumb's title name a single reader and the open items
  out.thumb = [c.thumbTitle({ number: 3, n_unresolved: 3, readers: 'one' }), c.thumbTitle({ number: 4, n_unresolved: 0, readers: 'tesseract', is_reviewed: false }), c.thumbTitle({ number: 5, is_reviewed: true, readers: 'two' }),
    c.readersOf({ readers: 'one' }), c.readersOf({ readers: 'tesseract' }), c.readersOf({ readers: 'two' }), c.readersOf({})];

  // ---- D73: the approve dialog counts the open words and the suggestions (groups and gaps)
  const ap = make();
  ap.approve(false);
  out.dialogLocal = { dialog: clone(ap.dialog), title: ap.dialogTitle, posts: posts().length };
  ap.dialog.open = false;
  const ap2 = make(); ap2.counts.unresolved = 0; // the server still finds open items
  globalThis.fetch = answer(() => ({ unresolved: 3, words: 2, groups: 1, gaps: 0, message: 'بقيت 3 علامات: كلمتان غير محسومتين وكلمات مقترحة لم تُحسم. اعتماد الصفحة رغم ذلك؟' }), 409);
  await ap2.approve(false);
  out.dialogServer = { dialog: clone(ap2.dialog), title: ap2.dialogTitle, post: posts().pop() };
  const ap3 = make(); ap3.counts.unresolved = 0;
  globalThis.fetch = answer(() => ({ unresolved: 4, words: 4, groups: 0, gaps: 0, message: 'بقيت 4 كلمة غير محسومة. اعتماد الصفحة رغم ذلك؟' }), 409);
  await ap3.approve(false);
  out.dialogWords = { dialog: clone(ap3.dialog), title: ap3.dialogTitle };
  const ap4 = make(); ap4.counts.unresolved = 0;
  globalThis.fetch = answer(() => ({ unresolved: 2, words: 0, groups: 0, gaps: 2 }), 409);
  await ap4.approve(false);
  out.dialogGaps = { dialog: clone(ap4.dialog), title: ap4.dialogTitle };

  // ---- D74: «نوع السطر» with the effective choice; a footnote-region line and a body line made a note
  const ro = make();
  out.roleMenu = ro.roles.map((r) => [r.value, r.label]);
  out.effective = [101, 104, 105].map((id) => [ro.lineRole(at(ro, id)), ro.lineKindOf(at(ro, id)), ro.roleLabel(at(ro, id)), ro.lineClass(at(ro, id))['is-footnote']]);
  out.showGroup = ro.lines.map((l, i) => ro.showGroup(l, i));
  out.pure = [R.lineKind('body', 'footnote'), R.lineKind('main', 'footnote'), R.lineKind('footnote', 'body'), R.lineKind('verse', 'footnote'), R.storedRole('footnote', 'footnote'), R.storedRole('body', 'footnote'), R.storedRole('footnote', 'body'), R.storedRole('verse', 'footnote'), R.effectiveRole('main', 'footnote'), R.effectiveRole('heading', 'footnote')];
  globalThis.fetch = answer((url, body) => { const l4 = clone(config.lines[3]); l4.role = 'main'; l4.kind = 'body'; return { line: l4 }; });
  const rp = ro.setRole(at(ro, 104), 'body');
  out.roleOptimistic = { role: at(ro, 104).role, kind: at(ro, 104).kind, label: ro.roleLabel(at(ro, 104)), checked: ro.menuRoleChecked(at(ro, 104), 'body') };
  await rp;
  out.rolePost = posts().pop();
  globalThis.fetch = answer(() => { const l1 = clone(config.lines[0]); l1.role = 'verse'; return { line: l1 }; });
  await ro.setRole(at(ro, 101), 'verse');
  out.verse = { post: posts().pop(), label: ro.roleLabel(at(ro, 101)), cls: ro.lineClass(at(ro, 101))['is-verse'], same: await ro.setRole(at(ro, 101), 'verse') };
  // a ⇧-click range: the line menu sets the role of every selected line, one request (one batch, one ⌘Z)
  const rg = make();
  rg.onLineClick(at(rg, 101), {});
  rg.onLineClick(at(rg, 103), { shiftKey: true });
  out.range = { ids: rg.range.ids.slice(), label: rg.roleMenuLabel(at(rg, 102)), outside: rg.roleMenuLabel(at(rg, 104)), selected: rg.lineClass(at(rg, 102))['is-selected'], checked: rg.menuRoleChecked(at(rg, 102), 'body') };
  rg.onTokClick(at(rg, 101), 0, { shiftKey: false }); rg.onLineClick(at(rg, 105), { shiftKey: true }); // anchor 101 again → 101…105
  out.rangeWide = rg.range.ids.slice();
  rg.onLineClick(at(rg, 103), {}); rg.onTokClick(at(rg, 101), 1, { shiftKey: true }); // a ⇧-click on a word extends too
  out.rangeUp = { ids: rg.range.ids.slice(), focus: rg.focus, pop: rg.pop.open };
  globalThis.fetch = answer((url, body) => ({ lines: body.line_ids.map((id) => { const l = clone(config.lines.find((x) => x.id === id)); l.role = 'footnote'; l.kind = 'footnote'; return l; }) }));
  const mp = rg.setMenuRole(at(rg, 102), 'footnote');
  out.rangeOptimistic = [101, 102, 103].map((id) => [at(rg, id).role, at(rg, id).kind]);
  await mp; await flush();
  out.rangePost = { post: posts().pop(), cleared: rg.range.ids.length, menu: rg.menuFor };
  // the lines already of the chosen kind stay out of the request; Esc clears a range
  const rs = make(); rs.onLineClick(at(rs, 104), {}); rs.onLineClick(at(rs, 105), { shiftKey: true });
  out.rangeNoop = { ids: rs.range.ids.slice(), sent: await rs.setMenuRole(at(rs, 104), 'footnote'), posts: posts().length };
  rs.onLineClick(at(rs, 103), {}); rs.onLineClick(at(rs, 105), { shiftKey: true });
  globalThis.fetch = answer((url, body) => ({ lines: [] }));
  await rs.setMenuRole(at(rs, 104), 'footnote');
  out.rangeSkip = posts().pop();
  rs.onLineClick(at(rs, 101), {}); rs.onLineClick(at(rs, 102), { shiftKey: true }); rs.closeTop();
  out.rangeEsc = rs.range.ids.length;
  // a failed range change restores every line
  const rf = make(); rf.onLineClick(at(rf, 101), {}); rf.onLineClick(at(rf, 102), { shiftKey: true });
  globalThis.fetch = answer(() => ({ detail: 'تعذّر' }), 500);
  await rf.setMenuRole(at(rf, 101), 'heading');
  out.rangeFailed = { roles: [at(rf, 101).role, at(rf, 102).role], save: rf.save.state };
  // the copied text follows the lines' kind: the notes after a blank line
  out.copyText = make().pageText ? make().pageText() : null;
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


def test_review_trust_under_node(tmp_path):
    """7b in the review component: the reason lines and «● في النص» (D71), groups and gaps with their keys and
    requests (D72), the pill, banner, filmstrip mark and approve dialog (D73), and line roles with ⇧-click
    ranges (D74). The requests are the contract's (review/fixtures/trust/index.json)."""
    harness = tmp_path / "harness.js"
    harness.write_text(TRUST_HARNESS, encoding="utf-8")
    fixture = tmp_path / "config.json"
    fixture.write_text(json.dumps(_trust_config(), ensure_ascii=False), encoding="utf-8")
    run = subprocess.run(
        ["node", str(harness), str(JS / "review.js"), str(fixture), str(JS / "keys.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])

    # D71: one reason line under the title, for each reason (Arabic copy of §4.1)
    word = ["word", "اختر القراءة الصحيحة"]
    assert out["reasons"] == {
        "vote": [*word, "النموذجان مختلفان؛ Tesseract يوافق Qari v0.2، فقراءته في النص."],
        "v03": [*word, "النموذجان مختلفان؛ Tesseract يوافق Qari v0.3."],
        "disagree": [*word, "النموذجان مختلفان."],
        "number": [*word, "رقم: قابِله بالصورة."],
        "letters": [*word, "في الكلمة حروف ليست عربية («и»، «л»)."],
        "symbol": [*word, "في الكلمة رمز غريب («※»)."],
        "alone": [*word, "لم يقرأ Qari v0.2 هذه الكلمة، ولم يؤكّدها Tesseract."],
        "single": [*word, "قرأ هذه المنطقةَ نموذجٌ واحد، ويقرأ Tesseract هنا «أنظر»."],
        "year": [*word, "السنة مكتوبة بعدها بالحروف: «ثلاث واربعين ومايتين» = ٢٤٣."],
        "sure": ["word", "تصحيح الكلمة", ""],
    }
    assert (
        out["reasonMissing"]
        == "كلمات أضافتها القراءة الثانية: قرأها Qari v0.2 وTesseract ولم يقرأها Qari v0.3."
    )
    assert out["reasonOldNumber"] == "رقم: قابِله بالصورة." and out["reasonOldWord"] == ""
    # the keys stay 1 = v0.3, 2 = v0.2, 3 = Tesseract; the vote's reading is the one in the text («● في النص»)
    assert out["voteOptions"] == [
        ["1", "primary", "يجي", "Qari v0.3", False],
        ["2", "secondary", "يحيى", "Qari v0.2", True],
        ["3", "tess", "يحبى", "Tesseract", False],
    ]
    assert out["yearOptions"] == [["1", "primary", "٢٤٢", "Kraken"], ["2", "sug", "٢٤٣", "من الحروف"]]
    # Enter confirms the current reading: `secondary` for the vote (the word stays v0.2's), else `primary`
    assert out["enterVote"]["post"] == [
        "/api/lines/101/resolve/",
        {"index": 1, "choice": "secondary", "t": "يحيى"},
    ]
    assert out["enterVote"]["t"] == "يحيى" and out["enterVote"]["res"] == "secondary"
    assert out["enterVote"]["orig"] == "يجي" and out["enterVote"]["next"] == [101, 2]
    assert out["enterVote"]["counts"] == {
        "low_total": 13,
        "unresolved": 11,
        "resolved": 2,
        "words": 8,
        "groups": 1,
        "gaps": 2,
    }
    assert out["enterPlain"] == {
        "post": ["/api/lines/101/resolve/", {"index": 2, "choice": "primary", "t": "الأمين"}],
        "res": "primary",
        "t": "الأمين",
    }

    # D72: Tab visits words, one stop per group (its first word) and the open gaps, in reading order, wrapping
    assert out["tabOrder"] == [
        [101, 1], [101, 2], [101, 3], [101, 4], [101, 5], [102, 0], [102, 1], [102, 3],
        ["gap", 7], ["gap", 8], [104, 1], [105, 1], [101, 1],
    ]  # fmt: skip
    assert out["stops"] == 12  # = the page's open items
    assert out["tabFromGroup"] == ["gap", 7]  # the group is left as one stop, from any of its words
    assert out["shiftTabToGroup"] == [102, 3] and out["shiftTabBeforeGroup"] == [102, 1]
    # the group's popover: title, reason line, its two actions with the count; the dotted underline runs
    # over its words and the spaces between them, on the scan too
    group = out["group"]
    assert group["kind"] == "group" and group["title"] == "كلمات أضافتها القراءة الثانية"
    assert group["reason"] == "قرأها Qari v0.2 وTesseract ولم يقرأها Qari v0.3."
    assert group["size"] == 4 and group["keep"] == "إبقاء الكلمات (4)" and group["drop"] == "حذف الكلمات (4)"
    assert group["cls"] == [["is-group", "is-group-focus", "is-low"], []]
    assert group["sep"] == [{"rv-sep": True, "is-group": True, "is-group-focus": True}, {}, {}]
    assert group["box"] == ["is-group"]
    # on a group ⌫ deletes one word as usual; its menu starts no correction and chooses no reading
    assert out["groupKeys"] == {"back": "deleteWord", "letter": None, "enter": "accept", "digit": None}
    # Enter keeps the group: one request for both lines, the words lose the underline at once
    assert out["keepOptimistic"] == {
        "res": ["secondary"] * 4,
        "counts": {"low_total": 13, "unresolved": 11, "resolved": 2, "words": 9, "groups": 0, "gaps": 2},
        "focus": ["gap", 7],
        "popKind": "gap",
        "save": "saving",
    }
    assert out["keepSettled"] == {
        "post": ["/api/pages/900/insertions/1/", {"keep": True}],
        "counts": {"low_total": 13, "unresolved": 11, "resolved": 2, "words": 9, "groups": 0, "gaps": 2},
        "v": ["v102b", "v103b"],
        "nUnresolved": 11,
        "save": "saved",
        "groupClass": False,
    }
    # «حذف الكلمات»: the words leave both lines, a gap after them moves back with its word; undo is offered
    assert out["dropOptimistic"] == {
        "l102": ["※", "فقيهاً", "ونشأ"],
        "l103": ["جماعة", "رحمه"],
        "gaps": [[9, -1, "dismissed"], [7, 0, "open"]],
        "counts": {"low_total": 12, "unresolved": 11, "resolved": 1, "words": 9, "groups": 0, "gaps": 2},
        "focus": ["gap", 7],
    }
    assert out["dropSettled"] == {
        "post": ["/api/pages/900/insertions/1/", {"keep": False}],
        "toast": "حُذفت الكلمات المضافة",
        "lines": [101, 102, 103, 104, 105],
    }
    # a group that fills a line: the line goes (at once, and by the answer's `deleted_ids` and `order`)
    assert out["dropLineOptimistic"] == [101, 102, 104, 105]
    assert out["dropLine"]["lines"] == [[101, 0], [102, 1], [104, 2], [105, 3]]
    assert out["dropLine"]["post"] == ["/api/pages/900/insertions/1/", {"keep": False}]
    assert out["dropLine"]["counts"]["unresolved"] == 10 and out["dropLine"]["counts"]["gaps"] == 1
    # a gap ▏: drawn after its word (−1: before the first), only while open; the popover offers its words
    gap = out["gap"]
    assert gap["kind"] == "gap" and gap["title"] == "قد تكون هنا كلمات ناقصة"
    assert gap["reason"] == "يقرأ النموذج الثاني هنا: «من الفضلاء»" and gap["typed"] == "من الفضلاء"
    assert (
        gap["open"] is True and gap["focus"] == {"lineId": 103, "index": 2, "gap": 7} and gap["options"] == 0
    )
    assert gap["drawn"] == [0, [7], [8]]  # the dismissed gap 9 is not drawn
    assert gap["label"] == "قد تكون هنا كلمات ناقصة: «من الفضلاء»"
    assert out["gapKeys"] == {
        "back": "dismissGap",
        "del": "dismissGap",
        "enter": "accept",
        "letter": "type",
        "merge": None,
        "space": "openWord",
    }
    # Enter inserts the offered words after the gap's word (sent as `{}`); focus moves on
    ins = out["gapInsertOptimistic"]
    assert ins["words"] == ["واخذ", "عن", "جماعة", "من", "الفضلاء", "رحمه"] and ins["status"] == "inserted"
    assert ins["counts"]["unresolved"] == 11 and ins["counts"]["gaps"] == 1
    assert ins["focus"] == ["gap", 8] and ins["flash"] == ["103-3", "103-4"]
    assert out["gapInsert"] == {
        "post": ["/api/gaps/7/accept/", {}],
        "words": ["واخذ", "عن", "جماعة", "من", "الفضلاء", "رحمه"],
        "gaps": [[9, "dismissed"], [7, "inserted"]],
        "drawn": 0,
    }
    # a key starts the words afresh; Esc brings the offer back; changed words travel as `text`
    assert out["gapTyping"] == {"typing": True, "typed": "ا"}
    assert out["gapEsc"] == {"typing": False, "typed": "الاجابة", "open": True}
    assert out["gapTyped"] == {
        "post": ["/api/gaps/8/accept/", {"text": "الإجابة الصحيحة"}],
        "words": ["الإجابة", "الصحيحة", "(1)", "انظر"],
    }
    # ⌫ dismisses: the text is unchanged, the gap no longer drawn, «تراجع» offered; a failure reopens it
    assert out["gapDismissOptimistic"]["status"] == "dismissed"
    assert out["gapDismissOptimistic"]["words"] == ["(1)", "انظر"]
    assert out["gapDismissOptimistic"]["counts"]["unresolved"] == 11
    assert out["gapDismissOptimistic"]["focus"] == [104, 1]
    assert out["gapDismiss"] == {
        "post": ["/api/gaps/8/dismiss/", {}],
        "toast": "تُجوهل النص المقترح",
        "drawn": 0,
    }
    assert out["gapDismissFailed"]["status"] == "open" and out["gapDismissFailed"]["save"] == "error"
    assert out["gapDismissFailed"]["counts"]["unresolved"] == 12
    assert out["gapShift"] == [[9, -1], [7, 1]]  # a word deleted before them

    # D73: the pill and the banner for one reader and for Tesseract alone; nothing for two (or before 7b)
    assert out["readers"] == {
        "one": [
            "one",
            "قراءة واحدة",
            "warning",
            "قرأ هذه الصفحةَ نموذجٌ واحد، فالعلامات فيها أقل من الحقيقة. قابِل كل سطر بالصورة.",
        ],
        "tesseract": [
            "tesseract",
            "نص Tesseract وحده",
            "danger",
            "تعذّرت قراءة هذه الصفحة بالنموذجين، ونصّها من Tesseract وحده. قابِل كل سطر بالصورة.",
        ],
        "two": ["two", None],
        "before": ["two", None],
    }
    assert out["marksLabel"] == ["12 علامة", "علامة واحدة", "علامتان", "3 علامات"]
    assert out["thumb"] == [
        "صفحة 3 · 3 علامات · قراءة واحدة",
        "صفحة 4 · نص Tesseract وحده",
        "صفحة 5 · مُراجَعة",
        "one",
        "tesseract",
        "",
        "",
    ]
    # the approve dialog: the open words and the suggestions (the page's own split, or the server's 409)
    assert out["dialogLocal"] == {
        "dialog": {"open": True, "count": 12, "words": 9, "suggested": 3, "gaps": 2},
        "title": "بقيت 12 علامة: 9 كلمات غير محسومة وكلمات مقترحة لم تُحسم. اعتماد الصفحة رغم ذلك؟",
        "posts": 0,
    }
    assert out["dialogServer"] == {
        "dialog": {"open": True, "count": 3, "words": 2, "suggested": 1, "gaps": 0},
        "title": "بقيت 3 علامات: كلمتان غير محسومتين وكلمات مقترحة لم تُحسم. اعتماد الصفحة رغم ذلك؟",
        "post": ["/api/pages/900/approve/", {"force": False}],
    }
    assert out["dialogWords"]["title"] == "بقيت 4 كلمة غير محسومة. اعتماد الصفحة رغم ذلك؟"
    assert out["dialogWords"]["dialog"]["suggested"] == 0
    assert out["dialogGaps"]["title"] == "بقيت علامتان: كلمات مقترحة لم تُحسم. اعتماد الصفحة رغم ذلك؟"

    # D74: «نوع السطر» offers five choices; the menu shows the effective one; the request carries it
    assert out["roleMenu"] == [
        ["body", "محتوى"],
        ["heading", "عنوان رئيسي"],
        ["subheading", "عنوان فرعي"],
        ["verse", "شعر"],
        ["footnote", "حاشية"],
    ]
    # a body line; a body-role line of a footnote region; a body-region line with the footnote role
    assert out["effective"] == [
        ["body", "body", "", False],
        ["footnote", "footnote", "", True],
        ["footnote", "footnote", "حاشية", True],
    ]
    assert out["showGroup"] == [False, False, False, True, False]  # «الحواشي» over the first note line
    assert out["pure"] == [
        "footnote", "body", "footnote", "body", "body", "main", "footnote", "verse", "body", "heading",
    ]  # fmt: skip
    # «محتوى» on a footnote-region line stores `main` and pulls it into the body
    assert out["roleOptimistic"] == {"role": "main", "kind": "body", "label": "محتوى", "checked": True}
    assert out["rolePost"] == ["/api/lines/104/role/", {"role": "body", "v": "v104"}]
    assert out["verse"] == {
        "post": ["/api/lines/101/role/", {"role": "verse", "v": "v101"}],
        "label": "شعر",
        "cls": True,
        "same": False,
    }
    # ⇧-click: a range from the anchor (a click on a line or a word), «نوع الأسطر المحدَّدة (3)»
    rng = out["range"]
    assert rng == {
        "ids": [101, 102, 103],
        "label": "نوع الأسطر المحدَّدة (3)",
        "outside": "نوع السطر",
        "selected": True,
        "checked": True,
    }
    assert out["rangeWide"] == [101, 102, 103, 104, 105]
    assert out["rangeUp"] == {"ids": [101, 102, 103], "focus": None, "pop": False}
    assert out["rangeOptimistic"] == [["footnote", "footnote"]] * 3
    assert out["rangePost"] == {
        "post": ["/api/pages/900/roles/", {"line_ids": [101, 102, 103], "role": "footnote"}],
        "cleared": 0,
        "menu": None,
    }
    # lines already of that kind stay out of the request (none left: nothing is sent); Esc clears the range
    assert out["rangeNoop"] == {"ids": [104, 105], "sent": False, "posts": 0}
    assert out["rangeSkip"] == ["/api/pages/900/roles/", {"line_ids": [103], "role": "footnote"}]
    assert out["rangeEsc"] == 0
    assert out["rangeFailed"] == {"roles": ["body", "body"], "save": "error"}
    # «نسخ» follows the lines' kind: the notes after a blank line
    assert (
        out["copyText"] == "قال يحيى الأمين وكان 1966 مилادية\n※ فقيهاً ونشأ تعالى بطرابلس\nواخذ عن جماعة رحمه"
        "\n\n(1) انظر\nهامش ٢٤٢"
    )


def test_review_template_trust_marks_popovers_and_ranges():
    """7b's markup: the pill and the banner (D73), the reason line and «● في النص» (D71), the group's and the
    gap's popovers and their marks in the lines (D72), the ⇧-click range and the half-disc (D73, D74)."""
    body = _render(_trust_config())
    assert _json_script(body, "review-config")["page"]["reading"]["readers"] == "one"
    # the toolbar: the readers' pill (warning / danger tone from the component) before the marks' count
    toolbar = _between(body, '<div class="rv-toolbar-start">', '<div class="rv-toolbar-end">')
    assert toolbar.index("data-readers") < toolbar.index("data-marks") < toolbar.index("الرقم المطبوع")
    assert ":class=\"readersInfo ? 'badge-' + readersInfo.tone : ''\"" in toolbar
    assert 'x-text="marksLabel"' in toolbar and "غير محسومة" not in toolbar
    assert "الأسطر المحدَّدة (" in toolbar and '@click="clearRange()"' in toolbar
    # the banner sits at the top of the lines column, before the lines
    assert body.index("data-readers-banner") < body.index('<ol class="rv-lines"')
    assert "readersInfo.tone === 'danger' ? 'banner-danger' : ''" in body
    # the popover: its title and reason line, «● في النص» on the current reading of an uncertain word
    pop = _between(body, '<div class="rv-pop"', '<ol class="rv-lines"')
    assert (
        'x-text="popTitle"' in pop and '<p class="rv-pop-reason" x-show="popReason" x-text="popReason"' in pop
    )
    assert "● في النص" in pop and "opt.current && focused && focused.conf === 'low'" in pop
    assert pop.index('x-text="popReason"') < pop.index('x-for="opt in options()"')
    # a group: keep (Enter) and drop with the count; a gap: its words in a field, «إدراج» and «تجاهل ⌫»
    assert "groupCountLabel('إبقاء الكلمات')" in pop and "groupCountLabel('حذف الكلمات')" in pop
    assert '@click="keepGroup()"' in pop and '@click="dropGroup()"' in pop
    assert (
        '@submit.prevent="acceptGap(pop.typed)"' in pop
        and 'x-ref="gapTyped"' in pop
        and ">إدراج</button>" in pop
    )
    assert '@click="dismissGap()"' in pop and '<span class="rv-act-label">تجاهل</span>' in pop
    # the correction and «إجراءات أخرى» belong to a word's popover only
    assert pop.count("popKind === 'word'") >= 3
    # the lines: a gap ▏ before the first word and after each word, the group's spaces underlined too
    lines = _between(body, '<ol class="rv-lines"', "</ol>")
    assert 'x-for="gap in gapsAt(line, -1)"' in lines and 'x-for="gap in gapsAt(line, i)"' in lines
    assert lines.count('class="rv-gap"') == 2 and lines.count('@click.stop="onGapClick(line, gap)"') == 2
    assert ':class="sepClass(line, i)"' in lines and '@click.stop="onTokClick(line, i, $event)"' in lines
    assert '@click="onLineClick(line, $event)"' in lines and '@mousedown="onLineMouseDown($event)"' in lines
    assert ":aria-selected=\"inRange(line) ? 'true' : null\"" in lines
    # the line menu: «نوع السطر» (or the range's label) on every line, footnote lines included
    assert 'x-text="roleMenuLabel(line)"' in lines and '@click="setMenuRole(line, r.value)"' in lines
    # the two new marks named once under the lines, when the page has them
    assert 'x-show="hasGroups"' in body and "خط منقَّط: كلمات أضافتها القراءة الثانية" in body
    assert 'x-show="hasGaps"' in body and "قد تكون هنا كلمات ناقصة" in body
    # the filmstrip's half-disc, the danger tint for Tesseract alone
    film = _between(body, '<nav class="rv-film"', "</nav>")
    assert (
        '<span class="mark-reader" x-show="readersOf(p)" '
        ":class=\"{ 'is-tesseract': readersOf(p) === 'tesseract' }\"" in film
    )
    # the approve dialog's note on suggestions, and the sheet's new rows
    assert "لا يدخل الكتابَ نصٌّ مقترح لم يُحسم." in body
    assert (
        "قبول القراءة التي في النص («● في النص»)" in body
        and "على موضع كلمات ناقصة: تجاهل الكلمات المقترحة" in body
    )


def _between(body: str, start: str, end: str) -> str:
    i = body.index(start)
    return body[i : body.index(end, i + len(start))]


def test_review_css_trust_marks_are_quiet():
    """The new marks keep the review screen's muted palette: dotted amber for a group, a thin amber bar for
    a gap, a small half-disc for one reader (D72, D73)."""
    src = (ROOT / "static" / "src" / "components" / "review.css").read_text(encoding="utf-8")
    assert ".rv-tok.is-group { cursor: pointer; border-bottom: 1.5px dotted rgba(217, 119, 6, 0.8); }" in src
    assert ".rv-sep.is-group { border-bottom: 1.5px dotted rgba(217, 119, 6, 0.8); }" in src
    gap = src[src.index("  .rv-gap {") : src.index("}", src.index("  .rv-gap {"))]
    assert "width: 2px;" in gap and "background: rgba(217, 119, 6, 0.5);" in gap
    mark = src[src.index("  .mark-reader {") : src.index("}", src.index("  .mark-reader {"))]
    assert (
        "width: 9px;" in mark
        and "linear-gradient(90deg, var(--color-warning) 50%, var(--color-surface) 50%)" in mark
    )
    assert ".mark-reader.is-tesseract { border-color: var(--color-danger);" in src
    assert ".rv-pop-reason {" in src and ".rv-opt-now {" in src and ".rv-line.is-selected {" in src


# ------------------------------------------------ 7b on D1's contract fixtures (review/fixtures/trust/)

TRUST_FIXTURES = ROOT / "review" / "fixtures" / "trust"

CONTRACT_RUN = r"""
const FX = JSON.parse(fs.readFileSync(process.argv[5], 'utf8'));
const step = (file, name) => FX[file].find((x) => x.name === name);
// every request is recorded; the server answers with the fixture's response and status
const serve = (entry) => { globalThis.fetch = answer(() => entry.response, entry.status); };
const last = () => { const x = calls.filter((c) => c[0] === 'POST').pop(); return x ? [x[1], x[2]] : null; };
(async () => {
  const out = { requests: {} };
  const P = FX['review_payload.json'].response;
  const c = make(P);
  out.start = { counts: clone(c.counts), stops: c.unresolvedRefs().length, readers: c.readers, info: c.readersInfo, groups: c.hasGroups, gaps: c.hasGaps, marks: c.marksLabel,
    nLow: c.lines.map((l) => [l.id, l.n_low, c.lineOpen(l)]) };
  const T = FX['tokens.json'];
  out.reasons = Object.fromEntries(Object.entries(T).map(([name, tok]) => [name, c.reasonOf(tok)]));
  out.current = Object.fromEntries(['vote', 'disagree_v03', 'kraken', 'year', 'single'].map((name) => [name, c.options(T[name]).map((o) => [o.key, o.choice, o.value, o.current])]));
  const tab = make(P); const order = []; for (let i = 0; i < 15; i += 1) { press(tab, { key: 'Tab', code: 'Tab' }); order.push(refOf(tab.focus)); }
  out.tabOrder = order;
  // ---- groups: keep group 1 over two lines, undo; drop it; drop group 2, which fills its line; undo
  const g = make(P); g.focusWord({ lineId: 9101, index: 4 }, { open: true });
  serve(step('insertion.json', 'keep group 1 (lines 9101 and 9102)')); press(g, { key: 'Enter', code: 'Enter' }); await flush(); await flush();
  out.requests.keep = last();
  out.keep = { counts: clone(g.counts), res: words(g, 9101).map((_, i) => at(g, 9101).tokens[i].res), nLow: [at(g, 9101).n_low, at(g, 9102).n_low], page: g.page.n_unresolved, focus: refOf(g.focus) };
  serve(step('insertion.json', 'undo the keep (one step)')); await g.undo(); await flush();
  out.requests.undoKeep = last(); out.undoKeep = { counts: clone(g.counts), open: g.hasGroups };
  const d = make(P); d.focusWord({ lineId: 9102, index: 0 }, { open: true });
  serve(step('insertion.json', 'drop group 1 (lines 9101 and 9102)'));
  const dp = d.dropGroup(); out.dropOptimistic = { l9101: words(d, 9101), l9102: words(d, 9102), gap: at(d, 9102).gaps.map((x) => [x.id, x.index]) };
  await dp; await flush();
  out.requests.drop = last(); out.drop = { l9101: words(d, 9101), l9102: words(d, 9102), gap: at(d, 9102).gaps.map((x) => [x.id, x.index]), counts: clone(d.counts), toast: d.undoToast && d.undoToast.message };
  const d2 = make(P); d2.focusWord({ lineId: 9103, index: 2 }, { open: true });
  serve(step('insertion.json', 'drop group 2 (it fills line 9103: the line goes)'));
  const dp2 = d2.dropGroup(); out.dropLineOptimistic = d2.lines.map((l) => l.id); await dp2; await flush();
  out.requests.dropLine = last(); out.dropLine = { lines: d2.lines.map((l) => [l.id, l.order]), counts: clone(d2.counts) };
  serve(step('insertion.json', 'undo the drop of group 2 (line 9103 is back)')); await d2.undo(); await flush();
  out.requests.undoDrop = last(); out.undoDrop = d2.lines.map((l) => l.id);
  // ---- gaps: the offered words, typed words, a dismissal, an edit that re-anchors a gap
  const a = make(P); a.onGapClick(at(a, 9102), at(a, 9102).gaps[0]);
  out.gapPop = [a.popKind, a.popTitle, a.popReason, a.pop.typed];
  serve(step('gaps.json', 'accept gap 9500 with the offered words')); press(a, { key: 'Enter', code: 'Enter' });
  out.gapOptimistic = words(a, 9102); await flush(); await flush();
  out.requests.accept = last(); out.accept = { words: words(a, 9102), res: at(a, 9102).tokens.map((t) => t.res), gap: at(a, 9102).gaps.map((x) => x.status), counts: clone(a.counts) };
  serve(step('gaps.json', 'undo: the words go, gap 9500 is open again')); await a.undo(); await flush();
  out.requests.undoAccept = last(); out.undoAccept = { words: words(a, 9102), gap: at(a, 9102).gaps.map((x) => x.status), drawn: a.gapsAt(at(a, 9102), 3).length };
  const ty = make(P); ty.focusWord({ lineId: 9100, index: -1, gap: 9501 }, { open: true }); ty.startTyping('و'); ty.pop.typed = '  وقد   كان ';
  serve(step('gaps.json', 'accept gap 9501 with typed words')); await ty.acceptGap(ty.pop.typed); await flush();
  out.requests.typed = last(); out.typed = words(ty, 9100);
  const ds = make(P); ds.focusWord({ lineId: 9102, index: 3, gap: 9500 }, { open: true });
  serve(step('gaps.json', 'dismiss gap 9500')); press(ds, { key: 'Backspace', code: 'Backspace' }); await flush(); await flush();
  out.requests.dismiss = last(); out.dismiss = { gap: at(ds, 9102).gaps.map((x) => x.status), counts: clone(ds.counts), toast: ds.undoToast && ds.undoToast.message };
  serve(step('gaps.json', 'undo: gap 9500 is open again')); await ds.undo(); await flush();
  out.requests.undoDismiss = last(); out.undoDismiss = at(ds, 9102).gaps.map((x) => x.status);
  const ed = make(P); ed.edit = { lineId: 9102, text: 'رحمه الله ٢٤٢ ثلاث واربعين ومايتين' };
  serve(step('gaps.json', 'an edit of line 9102 re-anchors gap 9500 (after «الله»: index 1)')); await ed.saveEdit(); await flush();
  out.requests.edit = last(); out.edit = at(ed, 9102).gaps.map((x) => [x.id, x.index, x.status]);
  // ---- roles: one line with the effective choice, then a ⇧-click range, then its undo in one step
  const r = make(P);
  const role = async (id, choice, name) => { serve(step('roles.json', name)); await r.setRole(at(r, id), choice); await flush(); const l = at(r, id); return { req: last(), stored: l.role, kind: l.kind, menu: r.lineRole(l), tag: r.roleLabel(l) }; };
  out.roles = [
    await role(9100, 'footnote', '«حاشية» on a body-region line stores footnote'),
    await role(9104, 'body', '«محتوى» on a footnote-region line stores main'),
    await role(9104, 'footnote', '«حاشية» on that footnote-region line stores body again'),
    await role(9104, 'heading', '«عنوان رئيسي» on a footnote-region line is allowed'),
    await role(9102, 'verse', '«شعر» on a body line'),
  ];
  const rg = make(P); rg.onLineClick(at(rg, 9101), {}); rg.onLineClick(at(rg, 9103), { shiftKey: true });
  out.rangeLabel = rg.roleMenuLabel(at(rg, 9102));
  serve(step('roles.json', 'a range: «حاشية» on lines 9101–9103')); await rg.setMenuRole(at(rg, 9102), 'footnote'); await flush();
  out.requests.range = last(); out.range = { kinds: [9101, 9102, 9103].map((id) => at(rg, id).kind), groups: rg.lines.map((l, i) => rg.showGroup(l, i)), cleared: rg.range.ids.length };
  serve(step('roles.json', 'undo the range (one step)')); await rg.undo(); await flush();
  out.requests.undoRange = last(); out.undoRange = [9101, 9102, 9103].map((id) => at(rg, id).kind);
  // ---- approve: the page's own count names what is open, as the server's 409 does; then the forced approval
  const ap = make(P); ap.approve(false);
  out.approveLocal = { title: ap.dialogTitle, posts: calls.filter((x) => x[0] === 'POST').length, note: ap.dialog.suggested > 0 };
  const ap2 = make(P); ap2.counts.unresolved = 0;
  serve(step('approve.json', 'open items left: 409')); await ap2.approve(false);
  out.requests.approve409 = last(); out.approve409 = { title: ap2.dialogTitle, dialog: clone(ap2.dialog) };
  serve(step('approve.json', 'forced approval')); ap2.approve(true); await flush();
  out.requests.force = last();
  // ---- how the page was read: the header each reading implies
  out.header = Object.fromEntries(Object.entries(FX['reading.json'].readings).map(([k, payload]) => {
    const x = make(Object.assign(clone(P), { page: payload.page }));
    return [k, x.readersInfo ? { pill: { text: x.readersInfo.label, level: x.readersInfo.tone }, banner: x.readersInfo.banner } : { pill: null, banner: null }];
  }));
  // ---- the filmstrip's thumbs
  out.film = FX['tile.json'].filmstrip.pages.map((p) => [c.thumbTitle(p), c.readersOf(p)]);
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


def _request(entry: dict) -> list:
    """A fixture's request as the harness records it: [url, body] (a POST without a body sends `{}`)."""
    return [entry["request"]["url"], entry["request"].get("body", {})]


def test_review_trust_on_the_contract_fixtures(tmp_path):
    """The review component on D1's real payloads (review/fixtures/trust/): what it shows for each token and
    each reading, and every request it sends equals the fixture's (`v`, the line version the client adds to
    whole-line actions, aside; typed words go whitespace-collapsed, as the server stores them)."""
    fixtures = {
        path.name: json.loads(path.read_text(encoding="utf-8")) for path in TRUST_FIXTURES.glob("*.json")
    }
    (tmp_path / "fx.json").write_text(json.dumps(fixtures, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "config.json").write_text(json.dumps({}), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(
        TRUST_HARNESS[: TRUST_HARNESS.index("(async () => {")] + CONTRACT_RUN, encoding="utf-8"
    )
    run = subprocess.run(
        [
            "node",
            str(harness),
            str(JS / "review.js"),
            str(tmp_path / "config.json"),
            str(JS / "keys.js"),
            str(tmp_path / "fx.json"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    ins, gaps = fixtures["insertion.json"], fixtures["gaps.json"]
    roles, approve = fixtures["roles.json"], fixtures["approve.json"]

    # every request is the contract's
    sent = out["requests"]
    typed = gaps[2]["request"]["body"]["text"]
    assert sent == {
        "keep": _request(ins[0]),
        "undoKeep": _request(ins[1]),
        "drop": _request(ins[2]),
        "dropLine": _request(ins[3]),
        "undoDrop": _request(ins[4]),
        "accept": _request(gaps[0]),
        "undoAccept": _request(gaps[1]),
        "typed": [gaps[2]["request"]["url"], {"text": " ".join(typed.split())}],
        "dismiss": _request(gaps[3]),
        "undoDismiss": _request(gaps[4]),
        "edit": [gaps[5]["request"]["url"], {**gaps[5]["request"]["body"], "v": sent["edit"][1]["v"]}],
        "range": _request(roles[5]),
        "undoRange": _request(roles[6]),
        "approve409": _request(approve[0]),
        "force": _request(approve[1]),
    }
    for got, entry in zip(out["roles"], roles[:5], strict=True):
        assert got["req"][0] == entry["request"]["url"]
        assert {k: v for k, v in got["req"][1].items() if k != "v"} == entry["request"]["body"]
        line = entry["response"]["line"]
        assert [got["stored"], got["kind"]] == [line["role"], line["kind"]]
    # what each role shows: the menu's effective choice and the quiet tag after the line
    assert [[r["menu"], r["tag"]] for r in out["roles"]] == [
        ["footnote", "حاشية"],
        ["body", "محتوى"],
        ["footnote", ""],
        ["heading", "عنوان رئيسي"],
        ["verse", "شعر"],
    ]
    payload = fixtures["review_payload.json"]["response"]
    assert (
        out["start"]["counts"] == payload["counts"]
        and out["start"]["stops"] == payload["counts"]["unresolved"]
    )
    assert (
        out["start"]["readers"] == "two"
        and out["start"]["info"] is None
        and out["start"]["marks"] == "14 علامة"
    )
    assert out["start"]["groups"] is True and out["start"]["gaps"] is True
    assert all(n_low == local for _, n_low, local in out["start"]["nLow"])  # `Line.n_low`, recounted alike
    # the reason line of every token shape (§4.1)
    assert out["reasons"] == {
        "sure": "",
        "vote": "النموذجان مختلفان؛ Tesseract يوافق Qari v0.2، فقراءته في النص.",
        "disagree_v03": "النموذجان مختلفان؛ Tesseract يوافق Qari v0.3.",
        "disagree": "النموذجان مختلفان.",
        "number": "رقم: قابِله بالصورة.",
        "alone": "لم يقرأ Qari v0.2 هذه الكلمة، ولم يؤكّدها Tesseract.",
        "script_letters": "في الكلمة حروف ليست عربية («и»، «л»).",
        "script_symbol": "في الكلمة رمز غريب («※»).",
        "kraken": "رقم: قابِله بالصورة.",
        "missing": "كلمات أضافتها القراءة الثانية: قرأها Qari v0.2 وTesseract ولم يقرأها Qari v0.3.",
        "year": "السنة مكتوبة بعدها بالحروف: «ثلاث واربعين ومايتين» = ٢٤٣.",
        "single": "قرأ هذه المنطقةَ نموذجٌ واحد، ويقرأ Tesseract هنا «يحيى».",
        "kept": "كلمات أضافتها القراءة الثانية: قرأها Qari v0.2 وTesseract ولم يقرأها Qari v0.3.",
        "typed": "",
    }
    # «● في النص» marks the reading in the text: Qari v0.2's after the vote, else the primary's
    assert out["current"]["vote"] == [["1", "primary", "يجي", False], ["2", "secondary", "يحيى", True]]
    assert out["current"]["disagree_v03"][0] == ["1", "primary", "فاضلا", True]
    assert out["current"]["year"] == [["1", "primary", "٢٤٢", True], ["2", "sug", "٢٤٣", False]]
    assert out["current"]["single"] == [["1", "primary", "يجي", True], ["2", "tess", "يحيى", False]]
    # Tab: the gap before the page's first word, the words, one stop per group, the gap after «الله»
    assert out["tabOrder"] == [
        ["gap", 9501], [9100, 1], [9100, 2], [9100, 3], [9100, 4], [9101, 0], [9101, 1], [9101, 2],
        [9101, 3], [9101, 4], ["gap", 9500], [9102, 4], [9103, 0], [9104, 0], ["gap", 9501],
    ]  # fmt: skip
    # a group kept over two lines, and its undo in one step
    keep = ins[0]["response"]
    assert out["keep"]["res"] == [t["res"] for t in keep["lines"][0]["tokens"]]
    assert out["keep"]["nLow"] == [line["n_low"] for line in keep["lines"]]
    assert out["keep"]["counts"]["unresolved"] == keep["counts"]["page_unresolved"] == out["keep"]["page"]
    assert out["keep"]["counts"]["groups"] == keep["counts"]["page_groups"] and out["keep"]["focus"] == [
        "gap",
        9500,
    ]
    assert out["undoKeep"] == {"counts": ins[1]["response"]["counts"], "open": True}
    # a drop: the optimistic lines and the gap's new place are the server's
    drop = ins[2]["response"]
    words = {line["id"]: [t["t"] for t in line["tokens"]] for line in drop["lines"]}
    expected = {
        "l9101": words[9101],
        "l9102": words[9102],
        "gap": [[9500, drop["lines"][1]["gaps"][0]["index"]]],
    }
    assert out["dropOptimistic"] == expected
    assert out["drop"] == {**expected, "counts": out["drop"]["counts"], "toast": "حُذفت الكلمات المضافة"}
    assert out["drop"]["counts"]["unresolved"] == drop["counts"]["page_unresolved"]
    order = ins[3]["response"]["order"]
    assert out["dropLineOptimistic"] == [o["id"] for o in order]
    assert out["dropLine"]["lines"] == [[o["id"], o["order"]] for o in order]
    assert out["undoDrop"] == [9100, 9101, 9102, 9103, 9104]
    # gaps: the popover, the offered words inserted (optimistic = the server's line), undo, typed, dismissed
    assert out["gapPop"] == [
        "gap",
        "قد تكون هنا كلمات ناقصة",
        "يقرأ النموذج الثاني هنا: «تعالى الاجابة»",
        "تعالى الاجابة",
    ]
    accepted = gaps[0]["response"]["line"]
    assert out["gapOptimistic"] == out["accept"]["words"] == [t["t"] for t in accepted["tokens"]]
    assert out["accept"]["res"] == [t["res"] for t in accepted["tokens"]] and out["accept"]["gap"] == [
        "inserted"
    ]
    assert out["accept"]["counts"]["gaps"] == gaps[0]["response"]["counts"]["page_gaps"]
    assert out["undoAccept"] == {
        "words": ["ونشأ", "بها", "رحمه", "الله", "٢٤٢", "ثلاث", "واربعين", "ومايتين"],
        "gap": ["open"],
        "drawn": 1,
    }
    assert out["typed"] == [t["t"] for t in gaps[2]["response"]["line"]["tokens"]]
    assert out["dismiss"]["gap"] == ["dismissed"] and out["dismiss"]["toast"] == "تُجوهل النص المقترح"
    assert out["dismiss"]["counts"]["unresolved"] == gaps[3]["response"]["counts"]["page_unresolved"]
    assert out["undoDismiss"] == ["open"] and out["edit"] == [[9500, 1, "open"]]
    # a range of three lines made notes, «الحواشي» over the first; undone in one step
    assert out["rangeLabel"] == "نوع الأسطر المحدَّدة (3)"
    assert out["range"] == {
        "kinds": ["footnote"] * 3,
        "groups": [False, True, False, False, False],
        "cleared": 0,
    }
    assert out["undoRange"] == ["body"] * 3
    # the dialog's question is the server's, from the page's own counts or from the 409
    message = approve[0]["response"]["message"]
    assert out["approveLocal"] == {"title": message, "posts": 0, "note": True}
    assert out["approve409"]["title"] == message
    assert out["approve409"]["dialog"] == {"open": True, "count": 14, "words": 10, "suggested": 4, "gaps": 2}
    # the header each reading implies, and the filmstrip
    assert out["header"] == fixtures["reading.json"]["header"]
    assert out["film"] == [
        ["صفحة 1 · 14 علامة", ""],
        ["صفحة 2 · علامة واحدة · قراءة واحدة", "one"],
        ["صفحة 3 · نص Tesseract وحده", "tesseract"],
        ["صفحة 4", ""],
    ]
