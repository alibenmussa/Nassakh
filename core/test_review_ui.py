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
    assert 'class="progress rv-progress-bar" role="progressbar"' in body
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
    # designed states: error (Arabic headline, dashboard retry), pending (decode noise),
    # loading skeleton, read-only
    assert 'x-text="errorHeadline"' in body and "فتح لوحة الكتاب لإعادة المرحلة" in body
    assert 'x-ref="noise"' in body and "النص قيد التعرّف" in body
    assert 'class="rv-skeleton" x-show="loading"' in body
    assert "عرض فقط" in body and 'x-show="!canEdit && ready"' in body
    # approve dialog and shortcut sheet (modals with focus trap and Esc)
    assert (
        'role="alertdialog" aria-modal="true"' in body and "كلمة غير محسومة. اعتماد الصفحة رغم ذلك؟" in body
    )
    assert "متابعة المراجعة" in body and '@click="approve(true)"' in body
    assert 'aria-labelledby="rv-sheet-title"' in body and "اختصارات لوحة المفاتيح" in body
    assert body.count('@keydown.tab="trapTab($event, $el)"') == 2
    assert "الصفحة التالية / السابقة" in body  # ArrowLeft/ArrowRight row of the sheet
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


# ------------------------------------------------ the component under Node (small Alpine stub)

HARNESS = r"""
const reg = {}; const inits = []; const stores = {}; const calls = [];
globalThis.window = globalThis;
globalThis.document = { hidden: false, addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); },
  getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: (n, v) => { if (v !== undefined) stores[n] = v; return stores[n]; } };
globalThis.setTimeout = (fn) => 1; globalThis.clearTimeout = () => {};
globalThis.Nassakh = { toast: (m) => calls.push(['toast', m]), copyText: (t) => { calls.push(['copy', t]); return true; } };
const fs = require('fs');
const config = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
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
  out.rolledBack = { t: c.lines[1].tokens[0].t, res: c.lines[1].tokens[0].res, counts: clone(c.counts), save: c.save.state, retry: typeof c.save.retry, toast: calls.filter((x) => x[0] === 'toast').pop()[1] };
  // undo re-renders from the returned payload
  const undone = clone(config); undone.lines = undone.lines.slice(0, 2); undone.counts = { low_total: 3, unresolved: 2, resolved: 1 };
  globalThis.fetch = reply(200, undone);
  c.save.state = 'idle';
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
  out.keys = {
    left: ka({ key: 'ArrowLeft' }, ctx), right: ka({ key: 'ArrowRight' }, ctx),
    tab: ka({ key: 'Tab' }, ctx), shiftTab: ka({ key: 'Tab', shiftKey: true }, ctx), tabOutside: ka({ key: 'Tab' }, { ...ctx, inFlow: false }),
    undo: ka({ key: 'z', metaKey: true }, ctx), sheet: ka({ key: '?' }, ctx), enter: ka({ key: 'Enter' }, ctx), altEnter: ka({ key: 'Enter', altKey: true }, ctx),
    one: ka({ key: '1' }, ctx), three: ka({ key: '3' }, ctx), letter: ka({ key: 'ك' }, ctx), letterUnfocused: ka({ key: 'ك' }, { ...ctx, focused: false }),
    edit: ka({ key: 'e' }, ctx), approve: ka({ key: 'A' }, ctx), next: ka({ key: 'n' }, ctx), zoom: [ka({ key: '+' }, ctx), ka({ key: '-' }, ctx), ka({ key: '0' }, ctx)],
    inField: ka({ key: 'a' }, { ...ctx, inField: true }), escInField: ka({ key: 'Escape' }, { ...ctx, inField: true }),
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
        ["node", str(harness), str(JS / "review.js"), str(fixture)],
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
    assert out["mergeCall"][1] == "/api/lines/52/merge/" and out["mergeCall"][2] == {"index": 1}
    assert out["mergeRolledBack"] == {"same": True, "save": "error"}
    assert out["deleted"]["words"] == ["(1)", "انظر"] and out["deleted"]["toast"] == "حُذفت الكلمة"
    assert out["deleted"]["call"][1] == "/api/lines/53/delete-word/" and out["deleted"]["call"][2] == {
        "index": 2
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
    assert out["resolveCall"] == ["POST", "/api/lines/51/resolve/", {"index": 1, "choice": "secondary"}]
    # failure: rolled back, chip in error with a retry, Arabic toast
    assert out["rolledBack"]["t"] == "وَفِي" and out["rolledBack"]["res"] is None
    assert out["rolledBack"]["counts"] == {"low_total": 5, "unresolved": 3, "resolved": 2}
    assert (
        out["rolledBack"]["save"] == "error"
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
    assert (
        keys["one"] == "choose1" and keys["three"] == "type"
    )  # no third reading → the digit starts a correction
    assert keys["letter"] == "type" and keys["letterUnfocused"] is None
    assert keys["edit"] == "edit" and keys["approve"] == "approve" and keys["next"] == "nextReview"
    assert keys["zoom"] == ["zoomIn", "zoomOut", "zoomReset"]
    assert keys["inField"] is None and keys["escInField"] == "close"
    assert out["copy"] == "قال الأمير في سنة 1966\nوَفِي الكتاب حكاية\n\n(1) انظر المصدر"


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
    # the line menu marks what a line is (not for footnotes)
    assert (
        "نوع السطر" in body and "setRole(line, r.value)" in body and "line.region_kind !== 'footnote'" in body
    )
    assert 'role="menuitemradio"' in body and "roleLabel(line)" in body
    # the shortcut sheet lists the keys and the menus
    assert "دمج الكلمة مع التالية / السابقة" in body and "نوع السطر: محتوى، عنوان رئيسي، عنوان فرعي" in body


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
