"""The stage bar and one term per concept (PHASE7_SPEC §5.1, §5.2, §5.5; D76, D77).

The partial (`templates/partials/_stage_bar.html`) in the top bar of every book screen, its component
(`static/src/js/stages.js`, `stageBar`) under Node against the contract fixtures of `api:book_stages`
(`editor/fixtures/contract/stages.json`): six steps in order, `aria-current`, the visually hidden state
words, a blocked step with no link, the collapse by the bar's own width (container queries), the refresh on
the events a screen dispatches, on the `nassakh` channel, on visibility and on `pageshow`, and the
dashboard's live status folded into its current step. Then the template test for the retired strings:
«لوحة الكتاب», «تحويل إلى كتاب» and «ضبط الأدلة» nowhere, and the pagination is «ترتيب الصفحات» (never
«يُخرَج»).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from django.template.loader import render_to_string
from django.test import RequestFactory

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "src" / "js"
CONTRACT = ROOT / "editor" / "fixtures" / "contract"
NODE = shutil.which("node")


def _stages() -> dict:
    return json.loads((CONTRACT / "stages.json").read_text(encoding="utf-8"))


def _response(label: str) -> dict:
    responses = _stages()["responses"]
    return next(v for k, v in responses.items() if label in k)


def _stages_url(book_id: int) -> str:
    """`api:book_stages` once the route exists (agent F's), else '' (the bar shows the embedded steps)."""
    from django.urls import NoReverseMatch, reverse

    try:
        return reverse("api:book_stages", args=[book_id])
    except NoReverseMatch:
        return ""


def _partial(**context) -> str:
    book = SimpleNamespace(pk=41, title="كتاب المراحل")
    return render_to_string("partials/_stage_bar.html", {"book": book, "current": "review", **context})


# ---------------------------------------------------------------- the partial and the shell


def test_the_partial_is_a_nav_of_an_ordered_list_with_its_states_written_out():
    html = _partial()
    assert '<nav class="stg" aria-label="مراحل الكتاب" data-stage-bar data-rail data-current="review"' in html
    assert f"x-data=\"stageBar({{ book: 41, current: 'review', url: '{_stages_url(41)}'" in html
    assert '<ol class="stg-list">' in html and '<template x-for="(s, i) in shown" :key="s.key">' in html
    # each step: its state on the item, the link (none when blocked), aria-current, the hint as the title
    assert '<li class="stg-item" :class="stepClass(s)" :data-step="s.key">' in html
    assert (
        '<a class="stg-step" :href="href(s)" :aria-current="s.current ? \'page\' : null" '
        ":aria-disabled=\"s.state === 'blocked' ? 'true' : null\" :title=\"title(s)\">" in html
    )
    # the ✓ of a done step, the count, the current step's detail, the state words for a screen reader
    assert '<svg class="icon" x-show="s.state === \'done\'"><use href="#i-check"/></svg>' in html
    assert '<bdi class="stg-count num" x-show="count(s)" x-text="count(s)" aria-hidden="true"></bdi>' in html
    assert 'x-show="s.current && s.detail" x-text="s.detail"' in html
    assert (
        "<span class=\"sr-only\" x-text=\"' ' + stateWord(s) + (s.detail ? '، ' + s.detail : '')\"></span>"
        in html
    )
    # separators: 12 px chevrons, mirrored, never before the first step
    assert (
        '<svg class="icon stg-sep icon-mirror" aria-hidden="true" x-show="i > 0"><use href="#i-chevron-end"/>'
        in html
    )
    # the narrowest bar: one button over a menu of the six (every detail there)
    assert 'x-ref="stgButton"' in html and 'x-text="compactLabel"' in html and 'aria-haspopup="menu"' in html
    assert '<div class="menu menu-popover stg-menu" role="menu" aria-label="مراحل الكتاب"' in html
    assert 'class="menu-item stg-menu-item" role="menuitem"' in html and 'x-text="s.detail"' in html
    # without the server's steps the bar fetches them once; with them they are embedded
    assert 'id="stage-steps"' not in html
    steps = _response("current=review")["steps"]
    html = _partial(stage_steps=steps)
    embedded = re.search(r'<script id="stage-steps" type="application/json">(.*?)</script>', html, re.S)
    assert embedded and json.loads(embedded.group(1)) == steps


@pytest.mark.django_db
def test_the_shell_places_the_bar_between_the_title_and_the_actions_and_loads_its_script():
    from django.contrib.auth.models import User

    request = RequestFactory().get("/books/1/")
    request.user = User.objects.get_or_create(username="editor")[0]
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    assert (
        base.index('<h1 class="page-title">')
        < base.index("{% block stagebar %}")
        < base.index('class="topbar-actions"')
    )
    body = render_to_string("base.html", {}, request=request)
    scripts = re.findall(r'<script src="/static/src/js/([\w/]+\.js)"></script>', body)
    assert scripts[:4] == ["ui.js", "keys.js", "decode.js", "stages.js"]


def test_every_book_screen_includes_the_bar_with_its_step():
    """The dashboard («التخطيط» in the mode, else «المعالجة»), review, the manuscript, the book page, the
    export page; the h1 holds the title only."""
    screens = {
        "books/detail.html": 'current=guides_mode|yesno:"pages,ocr"',
        "review/review.html": 'current="review"',
        "assembly/manuscript.html": 'current="manuscript"',
        "editor/layout.html": 'current="book"',
        "publishing/export.html": 'current="export"',
    }
    for name, current in screens.items():
        text = (ROOT / "templates" / name).read_text(encoding="utf-8")
        assert (
            f'{{% block stagebar %}}{{% include "partials/_stage_bar.html" with {current} %}}{{% endblock %}}'
            in text
        ), name
        assert "data-rail" in text, name  # the root folds the sidebar into the rail too (§5.2)
        assert "{% block title %}{{ book.title }}{% endblock %}" in text, name


# ---------------------------------------------------------------- the look and the collapse (layout.css)


def test_the_bar_collapses_by_its_own_width_and_the_rail_folds_on_every_book_screen():
    css = (ROOT / "static" / "src" / "components" / "layout.css").read_text(encoding="utf-8")
    assert ".stg { container: stg / inline-size; flex: 1 1 0; min-width: 0;" in css
    assert ".topbar:has(.stg) .page-title { flex: 0 4 auto; max-width: 26ch; min-width: 8ch; }" in css
    assert ".topbar .stg { flex: 1 1 520px; min-width: 140px; }" in css  # the title gives way first
    assert "@media (max-width: 900px) {\n    .topbar:has(.stg) .page-title { display: none; }" in css
    wide = css[css.index("@container stg (min-width: 840px)") :]
    assert ".stg-item.is-current .stg-detail { display: inline; }" in wide
    tight = css[css.index("@container stg (max-width: 620px)") :]
    assert ".stg-item:not(.is-current) .stg-count { display: none; }" in tight
    narrow = css[css.index("@container stg (max-width: 500px)") :]
    assert ".stg-list { display: none; }" in narrow and ".stg-compact { display: inline-flex; }" in narrow
    # 28 px steps, radius 6, 12.5 px / 500 in --text-2; the current one filled, 600
    step = css[css.index("  .stg-step {") : css.index("}", css.index("  .stg-step {"))]
    for rule in (
        "height: 28px;",
        "border-radius: var(--radius-sm);",
        "font-size: 12.5px;",
        "font-weight: 500;",
        "color: var(--color-text-2);",
    ):
        assert rule in step, rule
    assert (
        ".stg-item.is-current .stg-step, .stg-button { background: var(--color-active);"
        " color: var(--color-text); font-weight: 600; }" in css
    )
    # the marks: ✓ in --success, a static accent dot (no animation: chrome never loops, D24), warning, danger,
    # rings
    assert (
        ".stg-mark .icon { width: 12px; height: 12px; stroke-width: 2.2; color: var(--color-success); }"
        in css
    )
    marks = css[css.index(".is-active > .stg-step .stg-mark::before") : css.index(".stg-bar {")]
    assert "animation" not in marks
    assert "background: var(--color-accent)" in marks and "background: var(--color-warning)" in marks
    assert (
        "background: var(--color-danger)" in marks
        and "box-shadow: inset 0 0 0 1.5px var(--color-text-3)" in marks
    )
    assert (
        ".stg-item.is-stale .stg-label, .stg-menu-item.is-stale .stg-label"
        " { color: var(--color-warning-text); }" in css
    )
    # the rail: every book screen (the stage bar carries `data-rail`), wide windows only
    assert ".app-shell:has(.bp-screen) .sidebar" not in css
    rail = css[css.index("@media (min-width: 901px) {") :]
    assert ".app-shell:has([data-rail]) { grid-template-columns: 56px minmax(0, 1fr); }" in rail


# ---------------------------------------------------------------- the component under Node

HARNESS = r"""
const fs = require('fs');
const [, , stagesJs, fixturePath] = process.argv;
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
const reg = {}; const inits = []; const stores = {}; const calls = []; const docListeners = {};
const bus = new EventTarget();
globalThis.window = globalThis;
globalThis.addEventListener = bus.addEventListener.bind(bus);
globalThis.removeEventListener = bus.removeEventListener.bind(bus);
globalThis.dispatchEvent = bus.dispatchEvent.bind(bus);
globalThis.document = { hidden: false, addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); else docListeners[e] = fn; }, removeEventListener: () => {} };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: (n, v) => { if (v !== undefined) stores[n] = v; return stores[n]; } };
let now = 100000; Date.now = () => now;
const timers = []; globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; }; globalThis.clearTimeout = () => {};
const channels = []; const posted = [];
globalThis.BroadcastChannel = class { constructor(name) { this.name = name; this.fns = []; channels.push(this); } addEventListener(n, fn) { this.fns.push(fn); } removeEventListener() {} postMessage(m) { posted.push([this.name, m]); } };
const answers = [];
globalThis.fetch = async (url) => { calls.push(url); const data = answers.shift(); return { ok: Boolean(data), status: data ? 200 : 500, json: async () => data }; };
eval(fs.readFileSync(stagesJs, 'utf8'));
inits.forEach((fn) => fn());
const S = window.NassakhStages;
const flush = () => new Promise((r) => setImmediate(r));
const pick = (label) => Object.entries(fixture.responses).find(([k]) => k.includes(label))[1];
const out = {};
(async () => {
  // ---- pure: the server's steps as the bar's six
  const review = pick('current=review');
  const six = S.normalize(review, 'review');
  out.keys = six.map((s) => s.key);
  out.labels = six.map((s) => s.label);
  out.current = six.filter((s) => s.current).map((s) => s.key);
  out.blockedUrls = six.filter((s) => s.state === 'blocked').map((s) => s.url);
  out.counts = six.map((s) => S.shortCount(s));
  // a step left out waits as `todo` with its label; an unknown state reads `todo`
  const partial = S.normalize({ steps: [{ key: 'review', state: 'nonsense', url: '/r/' }] }, 'review');
  out.partial = partial.map((s) => [s.key, s.state, s.label]);
  out.fromDetail = S.shortCount({ detail: 'قيد المعالجة: 3 من 8' });
  // ---- the component: seeded by the server's steps, no request
  const c = reg.stageBar({ book: 41, current: 'review', url: '/api/books/41/stages/', steps: review.steps });
  c.init();
  out.seeded = { calls: calls.length, loaded: c.loaded };
  out.shown = c.shown.map((s) => ({ key: s.key, href: c.href(s), current: s.current, word: c.stateWord(s), title: c.title(s), count: c.count(s), cls: Object.keys(c.stepClass(s)).filter((k) => c.stepClass(s)[k]) }));
  out.compact = c.compactLabel;
  // every state of every step, from the fixture: the words a screen reader hears, and the link
  out.states = {};
  Object.entries(fixture.steps).forEach(([key, variants]) => Object.entries(variants).forEach(([name, raw]) => {
    const s = S.normalize([raw], key).find((x) => x.key === key);
    out.states[`${key}.${name}`] = [s.state, c.stateWord(s), c.href(s)];
  }));
  // ---- refresh: a screen's event (now), the channel (throttled), visibility, pageshow
  answers.push(pick('current=book'));
  window.NassakhStages.changed(41);
  await flush();
  out.onEvent = { url: calls[calls.length - 1], book: c.shown.find((s) => s.key === 'book').state, rebroadcast: posted.slice() };
  window.NassakhStages.changed(99); await flush(); // another book: nothing
  out.otherBook = calls.length;
  const listener = channels.find((ch) => ch.fns.length);
  now += 100;
  listener.fns.forEach((fn) => fn({ data: { type: 'review', book: 41, page: 3 } })); // inside the window: kept for its end
  out.throttled = { calls: calls.length, trailing: timers.length ? timers[timers.length - 1].ms : null };
  listener.fns.forEach((fn) => fn({ data: { type: 'review', book: 7, page: 3 } }));
  out.channelOtherBook = calls.length;
  answers.push(pick('current=review'));
  now += 2000; timers[timers.length - 1].fn(); await flush();
  out.afterTrailing = { calls: calls.length, review: c.shown.find((s) => s.key === 'review').state };
  answers.push(pick('current=review'));
  now += 2000; docListeners.visibilitychange(); await flush();
  out.visibility = calls.length;
  answers.push(pick('current=review'));
  now += 2000; bus.dispatchEvent(Object.assign(new Event('pageshow'), { persisted: false })); await flush();
  const notPersisted = calls.length;
  const ev = new Event('pageshow'); ev.persisted = true; bus.dispatchEvent(ev); await flush();
  out.pageshow = [notPersisted, calls.length];
  // a failed answer keeps the steps shown
  now += 2000; const before = JSON.stringify(c.steps); window.NassakhStages.changed(41); await flush();
  out.failedKeeps = JSON.stringify(c.steps) === before;
  // ---- the dashboard's live status over its current step (the chip and the bar folded in)
  const d = reg.stageBar({ book: 41, current: 'ocr', url: '/api/books/41/stages/', steps: pick('current=review').steps });
  stores.stages.set('ocr', { state: 'active', detail: 'قيد المعالجة: 3 من 8', count: '3/8', percent: 38 });
  const ocr = d.shown.find((s) => s.key === 'ocr');
  out.live = { state: ocr.state, count: d.count(ocr), detail: ocr.detail, bar: d.barStyle(ocr), cls: d.stepClass(ocr)['has-bar'], compact: d.compactLabel };
  stores.stages.set('ocr', null);
  out.liveCleared = d.shown.find((s) => s.key === 'ocr').state;
  // ---- no steps from the view: one fetch on mount, with the screen's step
  answers.push(pick('current=pages'));
  const e = reg.stageBar({ book: 41, current: 'pages', url: '/api/books/41/stages/?x=1', steps: [] });
  e.init(); await flush();
  out.mount = { url: calls[calls.length - 1], loaded: e.loaded, current: e.here.key, compact: e.compactLabel };
  // ---- the narrow bar's menu
  e.toggleMenu(); const opened = e.menuOpen; e.closeMenu(); out.menu = [opened, e.menuOpen];
  console.log(JSON.stringify(out));
})();
"""  # noqa: E501


@pytest.fixture(scope="module")
def node_out(tmp_path_factory) -> dict:
    if NODE is None:
        pytest.skip("node is not installed")
    harness = tmp_path_factory.mktemp("stages") / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    run = subprocess.run(
        [NODE, str(harness), str(JS / "stages.js"), str(CONTRACT / "stages.json")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr[-4000:]
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_six_steps_in_order_with_the_current_one(node_out):
    steps = {s["key"]: s for s in _response("current=review")["steps"]}
    assert node_out["keys"] == ["pages", "ocr", "review", "manuscript", "book", "export"]
    assert node_out["labels"] == ["التخطيط", "المعالجة", "المراجعة", "المخطوطة", "الكتاب", "الإخراج"]
    assert node_out["current"] == ["review"]
    assert node_out["blockedUrls"] == [""] * sum(s["state"] == "blocked" for s in steps.values())
    assert node_out["counts"] == [steps[k]["count"] or "" for k in node_out["keys"]]
    assert node_out["partial"] == [
        ["pages", "todo", "التخطيط"],
        ["ocr", "todo", "المعالجة"],
        ["review", "todo", "المراجعة"],
        ["manuscript", "todo", "المخطوطة"],
        ["book", "todo", "الكتاب"],
        ["export", "todo", "الإخراج"],
    ]
    assert node_out["fromDetail"] == "3/8"


def test_the_component_draws_aria_current_the_state_words_and_no_link_when_blocked(node_out):
    """What the bar draws is the contract's (values read from the fixture, so a corrected value never breaks
    the test): the link (none when blocked), the hint as the title, the count, the state's words."""
    steps = {s["key"]: s for s in _response("current=review")["steps"]}
    words = {
        "todo": "(لم تبدأ)",
        "active": "(جارية)",
        "done": "(مكتملة)",
        "stale": "(أقدم من النص)",
        "attention": "(تحتاج انتباهًا)",
        "blocked": "(غير متاحة بعد)",
    }
    assert node_out["seeded"] == {"calls": 0, "loaded": True}
    shown = {s["key"]: s for s in node_out["shown"]}
    assert [k for k, s in shown.items() if s["current"]] == ["review"]
    for key, step in steps.items():
        drawn = shown[key]
        assert drawn["href"] == (None if step["state"] == "blocked" else step["url"]), key
        assert drawn["title"] == (step["hint"] or step["detail"]), key
        assert drawn["word"] == words[step["state"]] == f"({step['state_label']})", key
        assert drawn["count"] == (step["count"] or ""), key
        assert f"is-{step['state']}" in drawn["cls"], key
    assert "is-current" in shown["review"]["cls"] and "has-count" in shown["review"]["cls"]
    assert node_out["compact"] == f"المراجعة {steps['review']['count']}"
    for name, (state, word, href) in node_out["states"].items():
        assert word == words[state], name
        assert (href is None) == (state == "blocked"), name
    key, variant = "book", "stale"
    assert node_out["states"][f"{key}.{variant}"][2] == _stages()["steps"][key][variant]["url"]


def test_the_bar_refreshes_on_events_the_channel_visibility_and_pageshow(node_out):
    # a screen's event: at once, with the screen's step, and re-broadcast to the book's other tabs
    assert node_out["onEvent"]["url"] == "/api/books/41/stages/?current=review"
    book_step = next(s for s in _response("current=book")["steps"] if s["key"] == "book")
    assert node_out["onEvent"]["book"] == book_step["state"]
    assert node_out["onEvent"]["rebroadcast"] == [["nassakh", {"type": "stages", "book": 41}]]
    assert node_out["otherBook"] == 1  # another book's event: nothing
    # the channel: at most one request every 1.5 s, a trigger inside the window kept for its end
    assert node_out["throttled"]["calls"] == 1 and 0 < node_out["throttled"]["trailing"] <= 1500
    assert node_out["channelOtherBook"] == 1
    assert node_out["afterTrailing"] == {"calls": 2, "review": "active"}
    assert node_out["visibility"] == 3
    assert node_out["pageshow"] == [3, 4]  # only a return from the back-forward cache
    assert node_out["failedKeeps"] is True


def test_the_dashboard_folds_its_live_status_into_the_current_step(node_out):
    assert node_out["live"] == {
        "state": "active",
        "count": "3/8",
        "detail": "قيد المعالجة: 3 من 8",
        "bar": "width:38%",
        "cls": True,
        "compact": "المعالجة 3/8",
    }
    assert node_out["liveCleared"] == "done"


def test_without_the_views_steps_the_bar_fetches_them_once(node_out):
    assert node_out["mount"] == {
        "url": "/api/books/41/stages/?x=1&current=pages",
        "loaded": True,
        "current": "pages",
        "compact": "التخطيط 3/7",
    }
    assert node_out["menu"] == [True, False]


def test_screens_announce_that_the_book_moved_on():
    """`nassakh:stages` after an approval, a finished assembly, an apply and a finished export (§5.1)."""
    review = (JS / "review.js").read_text(encoding="utf-8")
    assert "stagesChanged(this.book.id || this.page.book_id); // the stage bar" in review
    manuscript = (JS / "manuscript.js").read_text(encoding="utf-8")
    assert (
        "if (wasActive && !this.active && window.NassakhStages) window.NassakhStages.changed(this.bookId);"
        in manuscript
    )
    exports = (JS / "export.js").read_text(encoding="utf-8")
    assert "window.NassakhStages.changed(this.book && this.book.id)" in exports
    books = (JS / "books.js").read_text(encoding="utf-8")
    assert "window.NassakhStages.changed(cfg.bookId); // the bar: the step is done now" in books


# ---------------------------------------------------------------- one term per concept (D77, §5.5)

RETIRED = ("لوحة الكتاب", "تحويل إلى كتاب", "ضبط الأدلة")
# the pagination is «ترتيب الصفحات»: «الإخراج» means files only
PAGINATION_AS_EXPORT = (
    "يُخرَج الكتاب",
    "لم تُخرَج صفحاته",
    "لم تُخرَج صفحات",
    "تعذّر إخراج صفحات",
    "بعد أول إخراج",
    "تُخرَج الصفحات بخط",
    "تعذّر طلب الإخراج",
)


def test_the_retired_strings_are_in_no_template_and_no_script():
    files = [*sorted((ROOT / "templates").rglob("*.html")), *sorted(JS.rglob("*.js"))]
    assert len(files) > 40
    found = {}
    for path in files:
        text = path.read_text(encoding="utf-8")
        for needle in (*RETIRED, *PAGINATION_AS_EXPORT):
            if needle in text:
                found.setdefault(str(path.relative_to(ROOT)), []).append(needle)
    assert found == {}


def test_the_new_terms_are_in_place():
    detail = (ROOT / "templates" / "books" / "detail.html").read_text(encoding="utf-8")
    assert "<span>تجميع المخطوطة</span>" in detail and "<span>تجميع المخطوطة…</span>" in detail
    popover = (ROOT / "templates" / "assembly" / "_convert_popover.html").read_text(encoding="utf-8")
    assert 'x-text="NassakhBooks.convertLabel(ms.convert)">تجميع المخطوطة</span>' in popover
    assert "أو خذ تغييرات المراجعة وحدها من «الكتاب»." in popover
    options = (ROOT / "templates" / "assembly" / "_convert_options.html").read_text(encoding="utf-8")
    assert 'x-text="NassakhBooks.convertLeftOut(ms.convert)"' in options
    side = (ROOT / "templates" / "editor" / "_book_side.html").read_text(encoding="utf-8")
    assert 'aria-label="أدوات الكتاب"' in side
    stage = (JS / "book" / "stage.js").read_text(encoding="utf-8")
    assert "'تُرتَّب صفحات الكتاب…'" in stage and "'تعذّر طلب ترتيب الصفحات. حاول مرة أخرى.'" in stage
    assert "لم تُرتَّب الصفحات" in (ROOT / "templates" / "editor" / "layout.html").read_text(encoding="utf-8")
    assert "تُرتَّب الصفحات بخط" in (JS / "book" / "style.js").read_text(encoding="utf-8")


CONVERT_HARNESS = r"""
globalThis.window = globalThis;
globalThis.document = { addEventListener: () => {} };
eval(require('fs').readFileSync(process.argv[2], 'utf8'));
const B = window.NassakhBooks;
const c = (n, include, label = 'تجميع المخطوطة') => ({ label, unreviewed: n, options: { include_unreviewed: include } });
console.log(JSON.stringify({
  label: [B.convertLabel(c(0, true)), B.convertLabel(c(1, true)), B.convertLabel(c(2, true)), B.convertLabel(c(5, true)), B.convertLabel(c(12, true)), B.convertLabel(c(5, false)), B.convertLabel(c(5, true, 'إعادة التجميع'))],
  left: [B.convertLeftOut(c(5, false)), B.convertLeftOut(c(1, false)), B.convertLeftOut(c(2, false)), B.convertLeftOut(c(5, true)), B.convertLeftOut(c(0, false))],
}));
"""  # noqa: E501


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_convert_button_counts_the_unreviewed_pages(tmp_path):
    """Owner question 3: D35 stays; checked, the button reads «تجميع مع 5 صفحات غير مُراجَعة»; unchecked, the
    help names the pages left out."""
    harness = tmp_path / "convert.js"
    harness.write_text(CONVERT_HARNESS, encoding="utf-8")
    run = subprocess.run(
        [NODE, str(harness), str(JS / "books.js")], capture_output=True, text=True, timeout=60
    )
    assert run.returncode == 0, run.stderr[-3000:]
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["label"] == [
        "تجميع المخطوطة",
        "تجميع مع صفحة واحدة غير مُراجَعة",
        "تجميع مع صفحتين غير مُراجَعتين",
        "تجميع مع 5 صفحات غير مُراجَعة",
        "تجميع مع 12 صفحة غير مُراجَعة",
        "تجميع المخطوطة",
        "إعادة التجميع",
    ]
    assert out["left"] == [
        "تُترك 5 صفحات لم تُراجَع بعد، وتُضاف حين تُراجَع.",
        "تُترك صفحة واحدة لم تُراجَع بعد، وتُضاف حين تُراجَع.",
        "تُترك صفحتان لم تُراجَعا بعد، وتُضافان حين تُراجَعان.",
        "",
        "",
    ]


@pytest.mark.django_db
def test_the_real_api_answers_what_the_bar_reads(client):
    """Agent F's `api:book_stages` (once routed) against the shape the component normalises: six steps in
    order, a known state, no link when blocked, `current` on the screen's step."""
    from django.contrib.auth.models import User

    from books.models import Book

    book = Book.objects.create(title="كتاب المراحل")
    url = _stages_url(book.pk)
    if not url:
        pytest.skip("api:book_stages is not routed yet")
    client.force_login(User.objects.create_user("stages-reader"))
    data = client.get(f"{url}?current=review").json()
    steps = data["steps"]
    assert [s["key"] for s in steps] == ["pages", "ocr", "review", "manuscript", "book", "export"]
    states = {"todo", "active", "done", "stale", "attention", "blocked"}
    for step in steps:
        assert step["state"] in states and step["label"], step
        assert (step["url"] is None) == (step["state"] == "blocked"), step
        assert {"detail", "hint", "count", "current", "state_label"} <= set(step), step
    assert [s["key"] for s in steps if s["current"]] == ["review"]
    # a book with no page yet: review, the book and the export wait
    assert {s["key"] for s in steps if s["state"] == "blocked"} >= {"review", "book", "export"}
