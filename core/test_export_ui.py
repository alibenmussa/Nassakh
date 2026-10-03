"""The export page «الإخراج» (PHASE6_SPEC §8, §11.3): `/books/<id>/export/`.

- the rendered template against the §3.2 fixtures (`publishing/fixtures/export/`): the top bar, the meta
  line, «قبل الإخراج», one block per format drawn from its `form` (never a hardcoded choice), every state of
  the state row, «السجل», the read-only view and the page without a manuscript; the page view itself with
  `FakeExporter`
- the entry points: the book page's top-bar «الإخراج» (preview only) and its «⋯» item («PDF المعاينة»
  renamed), the dashboard's and the manuscript page's «⋯» items, the `i-download` symbol, `?tab=` in the
  side panel
- the compiled CSS (`make css`)
- the `exportPage` Alpine component under Node (a tiny Alpine/DOM stub, a fetch stub, hand-driven timers),
  fed the fixtures: POST → the queued and running states → the long poll → done with «تنزيل» and the page
  read once; 409 → the running export with «إلغاء»; 400 per field; the back-off and the hidden tab; resuming
  from `formats[].active`; cancel (the previous file comes back); a failed export; the kashida hint
  following the choice (←/→ in RTL); the comments checkbox disabled at 0; a proofreader; the menu's ↑/↓."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from django.contrib.auth.models import Group, User
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse

import pytest

from accounts.testing import member
from assembly.models import AssemblyRun
from books.models import Book
from editor.models import Manuscript
from editor.tests import sample_document
from publishing import exporters
from publishing.exporters import FakeExporter

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "src" / "js"
FIXTURES = ROOT / "publishing" / "fixtures" / "export"
TEMPLATES = ROOT / "templates" / "publishing"
CSS = ROOT / "static" / "dist" / "app.css"
NODE = shutil.which("node")


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _render(config: dict | None = None, **overrides) -> str:
    config = dict(config or _fixture("page"), **overrides)
    context = {
        "book": SimpleNamespace(title=config["book"]["title"], pk=config["book"]["id"]),
        "config": config,
        # base.html's shell needs a user (the `default:` filter argument must resolve)
        "user": SimpleNamespace(get_username=lambda: "editor", get_full_name=lambda: "علي"),
        "role_label": "محرّر",
    }
    return render_to_string("publishing/export.html", context)


def _json_script(body: str, element_id: str):
    match = re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>', body, re.S)
    assert match, element_id
    return json.loads(match.group(1))


def _between(body: str, start: str, end: str) -> str:
    i = body.index(start)
    return body[i : body.index(end, i)]


def _sources() -> str:
    """The export page's template sources (the partials included)."""
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(TEMPLATES.glob("*.html")))


# ---------------------------------------------------------------- the template


def test_export_page_shell_top_bar_and_config():
    config = _fixture("page")
    body = _render(config)
    assert 'lang="ar" dir="rtl"' in body and "<title>الإخراج · كتابي · نسّاخ</title>" in body
    # D76 (PHASE7 §5.1): the h1 holds the title only; «الإخراج» is the stage bar's current step
    assert '<h1 class="page-title">كتابي</h1>' in body
    assert 'data-stage-bar data-rail data-current="export"' in body and "data-export-page data-rail" in body
    assert _json_script(body, "export-config") == config
    assert "x-data=\"exportPage(JSON.parse(document.getElementById('export-config').textContent))\"" in body
    assert (
        '@visibilitychange.document="onVisible()"' in body and '@pageshow.window="onPageShow($event)"' in body
    )
    assert 'role="status" aria-live="polite" x-text="live"' in body
    assert "src/js/export.js" in body
    # the top bar: the poll pills and «⋯» (الكتاب، المخطوطة); the ghost «الكتاب» and «لوحة الكتاب» are
    # retired: the stage bar leads to every step (§5.2)
    bar = _between(body, '<div class="lo-bar"', "</header>")
    assert 'x-data="exportBar"' in bar
    assert "تعذّر التحديث · إعادة المحاولة" in bar and "انتهت الجلسة · تسجيل الدخول" in bar
    assert "data-back-link" not in bar and "لوحة الكتاب" not in body
    menu = _between(bar, "data-export-menu", "</div>\n    </div>")
    assert menu.index("/books/19/layout/") < menu.index("/books/19/manuscript/")
    assert (
        '@keydown.escape.window="open = false"' in bar
        and '@keydown.arrow-down.prevent="moveIn($el, 1)"' in bar
    )
    # the meta line and the pre-Alpine skeleton
    assert 'class="meta ex-meta" x-show="metaText" x-cloak x-text="metaText"' in body
    assert 'class="ex-boot" x-show="false" aria-hidden="true"' in body
    # the words: «إخراج», never «تصدير»; the page is a grid of cards: the readiness across the top, one card
    # per format (two columns on wide screens), the history across the bottom
    assert "تصدير" not in _sources()
    assert '<div class="ex-grid" data-formats>' in body
    assert body.index("data-readiness") < body.index("data-formats") < body.index("data-history")


def test_readiness_rows_with_their_links():
    body = _render()
    block = _between(body, "data-readiness", "</section>")
    assert '<h2 class="ex-title" id="ex-ready-title">قبل الإخراج</h2>' in block
    assert 'x-show="readiness.length"' in body  # no rows (no manuscript): no block
    # the head: the tone's icon (the alert while a warning is left, a check when the book is clear) and what
    # the count counts: «3 ملاحظات» (every row but the all-clear one), amber while any of them warns
    assert (
        '<section class="ex-card ex-ready" :class="\'is-\' + readyLevel" aria-labelledby="ex-ready-title" '
        'x-show="readiness.length" x-cloak data-readiness>' in body
    )
    assert ":href=\"'#' + levelIcon(readyLevel)\"" in block
    assert (
        '<span class="badge ex-ready-count" :class="{ \'badge-warning\': warnCount }" x-show="noteCount" '
        'x-text="noteText"></span>' in block
    )
    # a row: its level's icon (never a dot alone), the message
    assert (
        'x-for="(r, i) in readiness"' in block and '<li class="ex-check" :class="\'is-\' + r.level">' in block
    )
    assert ":href=\"'#' + levelIcon(r.level)\"" in block and 'x-text="r.message"' in block
    # the link to the fix on the end side, with a mirrored chevron
    assert '<template x-if="r.action && r.action.url">' in block
    # a fix in review carries `from=export`, so review leads back here (D76, §5.3: `fixUrl`)
    assert '<a class="link ex-fix" :href="fixUrl(r)"><span x-text="r.action.label"></span>' in block
    assert 'class="icon icon-sm icon-mirror" aria-hidden="true"><use href="#i-chevron-end"/>' in block


def test_every_format_is_drawn_from_its_form_never_hardcoded():
    body = _render()
    assert 'x-for="f in formats" :key="f.key"' in body
    block = _between(body, 'class="ex-card ex-format"', "</fieldset>")
    # a fieldset per format with an sr-only legend; a proofreader sees it disabled
    assert (
        ':data-ex-format="f.key" :disabled="!canEdit"' in block
        and 'class="sr-only" x-text="f.label"' in block
    )
    # the head: the format's icon and purpose from its key (with the state's dot at the icon's corner), the
    # label, the extension in its own LTR <bdi>, «لم يُخرَج بعد» / «غير متاحة بعد»
    assert ":href=\"'#' + formatIcon(f.key)\"" in block and 'x-text="formatPurpose(f.key)"' in block
    assert '<span class="dot ex-head-dot" :class="stateOf(f).dot" x-show="stateOf(f).dot"></span>' in block
    assert '<bdi dir="ltr" x-text="f.extension"></bdi><span x-text="headNote(f)"></span>' in block
    assert '<template x-if="f.available">' in block
    # a field with choices: the segmented control (aria-pressed, one Tab stop, ←/→) and the chosen hint
    assert 'x-for="fd in fieldsOf(f)" :key="fd.key"' in block
    assert "<template x-if=\"fd.kind === 'choice'\">" in block
    assert 'class="segmented lo-seg ex-seg" role="group"' in block
    assert 'x-for="(c, i) in fd.choices"' in block
    assert (
        ":aria-pressed=\"picked(f, fd, c) ? 'true' : 'false'\"" in block
        and ':tabindex="segTab(f, fd, i)"' in block
    )
    assert '@keydown="onSegKey($event, f, fd, i)" x-text="c.label"' in block
    assert 'class="help" x-show="choiceHint(f, fd)" x-text="choiceHint(f, fd)"' in block
    # a boolean field: the checkbox with its count and help, disabled at 0
    assert "<template x-if=\"fd.kind === 'bool'\">" in block
    assert ':disabled="fd.disabled" @change="setValue(f, fd, $el.checked)"' in block
    assert (
        '<template x-if="fd.available > 0"><span> (<bdi class="num" x-text="fd.available"></bdi>)</span>'
        in block
    )
    assert 'x-show="fd.hint" x-text="fd.hint"' in block and 'x-text="fieldError(f, fd)"' in block
    # the notes: a dot per level and the message
    assert 'x-for="(n, i) in notesOf(f)"' in block and ':class="levelDot(n.level)"' in block
    # the choices, their hints and the option labels live in the payload only (§3.1)
    sources = _sources()
    config = _fixture("page")
    for fmt in config["formats"]:  # Word's kashida and comments, the print PDF's bleed and crop marks…
        for field in fmt["form"].values():
            for choice in field.get("choices") or []:
                for text in (choice["title"], choice["hint"], f">{choice['label']}<"):
                    assert text not in sources, text
            for text in (field.get("label"), field.get("hint")):
                assert not text or text not in sources, text
    assert config["formats"][1]["form"]["bleed_mm"]["choices"]  # the real payload: the print form is there
    for fmt in config["formats"]:
        assert f">{fmt['label']}<" not in sources  # Word, «PDF للطباعة»… come from the payload


def test_the_state_row_has_every_state():
    body = _render()
    state = _between(body, 'class="ex-foot"', 'class="ex-card ex-history"')
    # the format's wide primary button at the card's foot, disabled while the request is on the wire and given
    # over to the running strip while an export runs; editors only
    assert 'class="btn btn-primary ex-go" x-show="canEdit && !isRunning(f)"' in state
    assert ':disabled="!canStart(f)" @click="start(f.key)"' in state and 'x-text="goLabel(f)"' in state
    # one state at a time, re-keyed so it cross-fades (the status line, the details under it, «تنزيل» in the
    # actions row); «إلغاء» is keyed by its export, so queued → running keeps the button (and its focus); the
    # status line takes the focus after a start
    assert state.count('x-for="s in [stateOf(f)]" :key="stateKey(s)"') == 3
    assert state.count('x-for="s in [stateOf(f)]" :key="cancelKey(s)"') == 1
    extra = _between(state, ':key="cancelKey(s)"', "</template>\n      </span>")
    assert "ex-cancel" in extra
    assert 'class="ex-status" tabindex="-1" :data-ex-status="f.key"' in state  # stays: it keeps its focus
    assert state.index("data-ex-status") < state.index('x-for="s in [stateOf(f)]"')
    assert "isFresh(f, s) ? 'is-fresh' : ''" in state  # the success dot blooms in once
    assert '<bdi x-text="s.name"></bdi>' in state  # the file name, truncated on its own
    assert 'x-for="(p, i) in s.parts" :key="i"' in state and '<bdi x-text="p"></bdi>' in state
    # done / stale: «تنزيل» (a real download link)
    assert "<template x-if=\"s.kind === 'done' || s.kind === 'stale'\">" in state
    assert ':href="s.row.download_url" download :data-ex-download="f.key"' in state
    assert re.search(r'<use href="#i-download"/></svg>\s*<span>تنزيل</span>', state)
    # queued / running: «إلغاء» (editors), the 4 px bar only when the progress is counted, the waiting hint
    assert "<template x-if=\"(s.kind === 'queued' || s.kind === 'running') && canEdit\">" in state
    assert '@click="cancel(f.key)" :data-ex-cancel="f.key">إلغاء</button>' in state
    assert 'class="progress ex-bar" role="progressbar"' in state and 'x-show="s.percent !== null"' in state
    assert '<p class="lo-note" x-show="s.hint" x-text="s.hint"></p>' in state
    # the file's warnings; a failure's technical line for editors (LTR)
    assert 'x-for="(w, i) in fileWarnings(s)"' in state
    assert "<template x-if=\"s.kind === 'error' && s.row.error_detail\">" in state
    assert '<details class="error-details ex-error-details">' in state and "التفاصيل التقنية" in state
    assert '<pre dir="ltr" x-text="s.row.error_detail"></pre>' in state
    # the request's own error (409, 400, 404…)
    assert 'class="field-error" role="alert" x-show="errorOf(f)" x-text="errorOf(f)"' in state


def test_the_history_rows_and_their_empty_and_loading_states():
    body = _render()
    history = _between(body, 'class="ex-card ex-history"', "</section>")
    assert '<h2 class="ex-title" id="ex-history-title">السجل</h2>' in history
    assert 'x-show="items.length" x-cloak x-text="itemsText"' in history  # «4 ملفات»
    # a row: the format's icon with the status dot at its corner
    assert ":href=\"'#' + formatIcon(row.format)\"" in history
    assert 'class="ed-snap-skeleton" x-show="false" aria-hidden="true"><span></span><span></span>' in history
    assert (
        'x-for="row in items" :key="row.id"' in history
        and 'class="ed-snap ex-row" :class="\'is-\' + row.status"' in history
    )
    assert ':class="historyDot(row)"' in history and 'x-for="(p, i) in historyParts(row)"' in history
    assert (
        '<span x-show="i" aria-hidden="true"> · </span><bdi x-text="p"></bdi>' in history
    )  # «318 KB» stays one run
    assert (
        ':title="absTime(row)" x-text="historyWhen(row)"' in history
        and 'x-text="historyRest(row)"' in history
    )
    assert '<template x-if="row.download_url">' in history
    assert ':aria-label="\'تنزيل \' + row.filename" title="تنزيل"' in history
    assert 'x-show="!items.length">لا ملفات بعد. يظهر هنا كل ملف أُخرج، بوقته وخياراته.</li>' in history


def test_read_only_view_and_the_page_without_a_manuscript():
    readonly = "يبدأ الإخراج محرّر الكتاب؛ الملفات الجاهزة تُنزَّل من السجل."
    assert readonly not in _render()
    body = _render(can_edit=False)
    assert f'<p class="lo-readonly meta ex-readonly" x-cloak>{readonly}</p>' in body
    assert _json_script(body, "export-config")["can_edit"] is False
    # no manuscript: the empty state, the manuscript to open, no component
    body = _render(_fixture("page-empty"))
    assert "لا كتاب للإخراج بعد" in body and "data-export-page" not in body
    assert "اجمع مخطوطة الكتاب أولًا، ثم افتحه في «الكتاب»؛ من هنا تُخرَج بعد ذلك ملفاته." in body
    assert '<a class="btn btn-primary" href="/books/21/manuscript/">فتح المخطوطة</a>' in body
    assert '<use href="#i-download"/>' in body and 'x-data="exportBar"' in body


def test_the_download_symbol_and_the_compiled_css():
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    assert (
        '<symbol id="i-download" viewBox="0 0 24 24"><path d="M12 4v12M7 11l5 5 5-5"/>'
        '<path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/></symbol>' in base
    )
    app = (ROOT / "static" / "src" / "app.css").read_text(encoding="utf-8")
    assert '@import "./components/export.css";' in app
    css = CSS.read_text(encoding="utf-8")
    assert re.search(r"\.ex-page\{[^}]*max-width:1120px", css)
    assert re.search(r"\.ex-grid\{[^}]*grid-template-columns:repeat\(2,minmax\(0,1fr\)\)", css)  # two columns
    assert re.search(r"\.ex-card\{[^}]*border:1px solid var\(--color-border\)", css)
    assert re.search(r"\.ex-go\{[^}]*flex:150px", css)  # the wide button (`flex: 1 1 150px`, minified)
    assert re.search(r"\.ex-dot\.is-fresh\{animation:\.3s [^}]*rv-stamp-in", css)  # the bloom, 300 ms
    assert re.search(r"\.ex-status-line\{[^}]*animation:\.2s [^}]*ex-fade-in", css)  # the cross-fade, 200 ms
    assert re.search(
        r"@media \(max-width:760px\)\{[^@]*\.ex-grid[^{]*\{[^}]*grid-template-columns:minmax\(0,1fr\)", css
    )  # one column on phones
    assert re.search(r"prefers-reduced-motion:reduce\)\{[^@]*\.ex-dot\.is-fresh[^{]*\{animation:none", css)
    # the history's hairline between two rows: the x-for <template> is the list's first child, so a
    # `:first-child` rule never matched and the first row carried a line above it
    assert ".ex-row+.ex-row{border-top:1px solid var(--color-border)}" in css
    assert re.search(r"\.ex-row\{border-top:0;", css) and ".ex-row:first-child" not in css
    assert ".ex-row:first-of-type{padding-top:0}" in css


# ---------------------------------------------------------------- the page view and the entry points


def _user(name: str, role: str) -> User:
    user = User.objects.create_user(name, password="pass-1234")
    user.groups.add(Group.objects.get_or_create(name=role)[0])
    return member(user)  # the books' organisation (D102)


def _logged(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def fake(monkeypatch):
    """Only the fake Word exporter (the other streams' exporters are not loaded)."""
    monkeypatch.setattr(exporters, "_registry", {})
    monkeypatch.setattr(exporters, "_load", lambda format: None)
    exporter = FakeExporter("docx")
    exporters.register(exporter)
    return exporter


@pytest.fixture
def book(db):
    book = Book.objects.create(title="كتاب الإخراج", author="المؤلف", status=Book.Status.REVIEWING)
    run = AssemblyRun.objects.create(book=book, status="done")
    Manuscript.objects.create(book=book, document=sample_document(), version=1, run=run)
    return book


def test_the_export_page_view_embeds_the_payload(fake, book):
    url = reverse("publishing:export", args=[book.pk])
    assert url == f"/books/{book.pk}/export/"
    assert Client().get(url).status_code == 302  # signed out: the login page
    body = _logged(_user("editor", "editor")).get(url).content.decode()
    config = _json_script(body, "export-config")
    assert set(config) == {"book", "can_edit", "readiness", "formats", "items", "urls"}
    assert config["book"]["has_manuscript"] is True and config["can_edit"] is True
    assert [f["key"] for f in config["formats"]] == ["docx", "print_pdf", "screen_pdf", "epub"]
    assert config["formats"][0]["available"] is True and set(config["formats"][0]["form"]) == {
        "kashida",
        "comments",
    }
    assert "<title>الإخراج · كتاب الإخراج · نسّاخ</title>" in body and "data-export-page" in body
    assert "lo-readonly" not in body
    reader = _logged(_user("reader", "proofreader")).get(url).content.decode()
    assert _json_script(reader, "export-config")["can_edit"] is False and "ex-readonly" in reader


def test_the_book_page_top_bar_and_menu_lead_to_the_export_page(fake, book):
    body = _logged(_user("editor", "editor")).get(reverse("editor:layout", args=[book.pk])).content.decode()
    url = f"/books/{book.pk}/export/"
    bar = _between(body, 'x-data="bookBar"', "</header>")
    link = _between(bar, '<a class="btn btn-primary btn-sm"', "</a>")
    assert (
        f'href="{url}"' in link and "x-show=\"v.mode !== 'edit'\"" in link and 'title="ملفات الكتاب"' in link
    )
    assert '<use href="#i-download"/>' in link and "<span>الإخراج</span>" in link
    assert bar.index("data-export-link") < bar.index('aria-label="المزيد من الإجراءات"')  # before «⋯»
    menu = _between(body, "data-book-menu", "</template>")
    assert "PDF المعاينة" in menu and "إخراج PDF" not in menu  # the preview's PDF, renamed (§8.2)
    nav = menu[menu.index('class="menu-sep"') :]
    assert (
        nav.index(f'href="{url}" data-export-menu-item') < nav.index("المخطوطة") and "لوحة الكتاب" not in body
    )


def test_the_book_page_passes_the_tab_asked_for(fake, book):
    """The readiness links' `?tab=` reaches the book page's config (`_context` → `page_config(tab=)`)."""
    client = _logged(_user("editor", "editor"))
    url = reverse("editor:layout", args=[book.pk])

    def tab(query: str):
        return _json_script(client.get(f"{url}{query}").content.decode(), "book-config")["tab"]

    assert tab("?tab=uncertain") == "uncertain"
    assert tab("?tab=nonsense") is None and tab("") is None


def test_the_dashboard_and_the_manuscript_menus_offer_the_export_page(fake, book):
    client = _logged(_user("editor", "editor"))
    url = f"/books/{book.pk}/export/"
    dashboard = client.get(reverse("books:detail", args=[book.pk])).content.decode()
    item = _between(dashboard, f'href="{url}"', "</a>")
    assert 'x-show="d.hasManuscript" data-export-menu-item' in item and "<span>الإخراج</span>" in item
    assert dashboard.index("data-book-menu-item") < dashboard.index(f'href="{url}"')  # after «الكتاب»
    manuscript = client.get(reverse("assembly:manuscript", args=[book.pk])).content.decode()
    item = _between(manuscript, f'href="{url}"', "</a>")
    assert 'x-show="v.hasDocument" data-export-menu-item' in item and "<span>الإخراج</span>" in item
    assert "لوحة الكتاب" not in manuscript  # D77: retired; the stage bar leads to «المعالجة»


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_side_panel_opens_the_tab_asked_for(tmp_path):
    """`?tab=` (page_config's `tab`, the readiness links) wins over the tab remembered for the mode."""
    probe = tmp_path / "panel.js"
    probe.write_text(
        r"""
globalThis.window = globalThis;
globalThis.document = { addEventListener() {}, querySelector: () => null, querySelectorAll: () => [] };
globalThis.localStorage = { getItem: () => 'chapters', setItem() {} };
const fs = require('fs');
for (const f of ['geometry.js', 'panel.js', 'page.js']) {
  eval(fs.readFileSync(process.argv[2] + '/' + f, 'utf8'));
}
const tab = (cfg) => globalThis.NassakhBook.parts.panel({ cfg, urls: {}, timers: {}, dom: {} }).tab;
console.log(JSON.stringify([tab({ mode: 'preview', tab: 'uncertain' }),
  tab({ mode: 'preview', tab: 'format' }),
  tab({ mode: 'preview', tab: 'nope' }), tab({ mode: 'preview', tab: null })]));
""",
        encoding="utf-8",
    )
    run = subprocess.run(["node", str(probe), str(JS / "book")], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout.strip().splitlines()[-1]) == ["uncertain", "format", "chapters", "chapters"]


# ---------------------------------------------------------------- the component under Node

HARNESS = r"""
const reg = {}; const inits = []; const stores = {}; const calls = []; const timers = []; const focused = [];
globalThis.window = globalThis;
const node = (sel) => ({ sel, focus: () => focused.push(sel), contains: () => globalThis.document.activeElement === 'inside' });
globalThis.document = { hidden: false, activeElement: null, documentElement: { dir: 'rtl' },
  addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); },
  createElement: () => ({}), querySelector: (sel) => (sel === 'meta[name="csrf-token"]' ? { content: 'tok' } : node(sel)), querySelectorAll: () => [] };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: (n, v) => { if (v !== undefined) stores[n] = v; return stores[n]; } };
let tid = 0;
globalThis.setTimeout = (fn, ms) => { tid += 1; timers.push({ fn, ms, id: tid }); return tid; };
globalThis.clearTimeout = (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); };
globalThis.setInterval = () => 1; globalThis.clearInterval = () => {};
const NOW = Date.parse('2026-09-26T12:02:13+02:00'); Date.now = () => NOW;
const fs = require('fs'); const path = require('path');
const [js, fixtures] = [process.argv[2], process.argv[3]];
eval(fs.readFileSync(path.join(js, 'manuscript.js'), 'utf8'));  // relativeTime, arCount
eval(fs.readFileSync(path.join(js, 'export.js'), 'utf8'));
inits.forEach((fn) => fn());
const load = (name) => JSON.parse(fs.readFileSync(path.join(fixtures, name + '.json'), 'utf8'));
const clone = (v) => JSON.parse(JSON.stringify(v));
const queue = [];
globalThis.fetch = async (url, init) => {
  const method = (init && init.method) || 'GET';
  calls.push([method, url, init && init.body ? JSON.parse(init.body) : null, init && init.headers && init.headers['X-CSRFToken']]);
  const [status, data] = queue.shift() || [500, null];
  return { ok: status < 400, status, json: async () => data };
};
const flush = () => new Promise((r) => setImmediate(r));
const runTimer = async () => { const t = timers.pop(); if (t) await t.fn(); await flush(); await flush(); };
const make = (config) => { const c = reg.exportPage(clone(config)); c.init(); return c; };
const X = NassakhExport;
(async () => {
  const out = {};
  const page = load('page'), running = load('page-running'), empty = load('page-empty');
  const rows = { queued: load('row-queued'), running: load('row-running'), done: load('row-done'), stale: load('row-done-stale'), error: load('row-error'), cancelled: load('row-cancelled') };
  const layout = page.book.layout;

  // ------------------------------------------------ pure helpers
  out.meta = [X.metaLine(layout), X.metaLine(Object.assign({}, layout, { state: 'none', page_count: null })),
    X.metaLine(Object.assign({}, layout, { state: 'rendering' })), X.metaLine(Object.assign({}, layout, { chapters: 1, body_size_pt: 10.5 })), X.metaLine(null)];
  out.fields = X.fieldsOf(page.formats[0]).map((fd) => [fd.key, fd.kind, fd.label, fd.available, fd.disabled, fd.choices.length]);
  out.fieldsEmpty = X.fieldsOf(empty.formats[0]).map((fd) => [fd.key, fd.disabled, fd.hint]);
  out.fieldsBare = X.fieldsOf({ form: { bleed_mm: { value: 0 }, crop_marks: { value: false } } }).map((fd) => [fd.key, fd.kind, fd.label]);
  out.values = [X.initialValues(page.formats[0]), X.initialValues(empty.formats[0])];
  const st = (active, latest) => { const s = X.stateOf({ active, latest }, NOW); return [s.kind, s.dot, s.name, s.text, s.percent, s.hint, s.title]; };
  const counted = Object.assign(clone(rows.running), { progress: { step: 'layout', label: 'ترتيب الصفحات', done: 21, total: 84, percent: 25 } });
  out.states = { never: st(null, null), done: st(null, rows.done), stale: st(null, rows.stale), queued: st(rows.queued, rows.done),
    running: st(rows.running, rows.done), counted: st(counted, null), error: st(null, rows.error), cancelledLatest: st(null, rows.done) };
  out.history = Object.fromEntries(Object.entries(rows).map(([k, r]) => [k, [X.historyTitle(r), X.historyRest(r), X.historyDot(r)]]));
  out.notes = X.notesOf(page.formats[0], NOW).map((n) => n.code);
  out.notesNever = X.notesOf(Object.assign({}, page.formats[0], { latest: null }), NOW).map((n) => n.code);
  out.fileWarnings = X.fileWarnings(rows.done).map((w) => w.code);
  out.backoff = [1, 2, 3, 4, 5, 6].map(X.backoff);
  out.fill = X.fill(page.urls.cancel, 14);

  // ------------------------------------------------ the page: the form, the kashida hint, the keys
  const c = make(page);
  const f = c.fmt('docx');
  const [kash, comm] = c.fieldsOf(f);
  out.init = { view: stores.exportPage.view === c, polls: timers.length, meta: c.metaText, warn: c.warnCount,
    primary: c.formats.map((x) => c.isPrimary(x)), headNote: c.formats.map((x) => c.headNote(x)), canStart: c.formats.map((x) => c.canStart(x)),
    go: c.goLabel(f), state: c.stateOf(f).kind, bar: reg.exportBar().v === c };
  out.hints = [c.choiceHint(f, kash)];
  c.pick(f, kash, kash.choices[2]);
  out.hints.push(c.choiceHint(f, kash));
  out.picked = kash.choices.map((ch) => c.picked(f, kash, ch));
  out.tabs = kash.choices.map((ch, i) => c.segTab(f, kash, i));
  const key = (k, i) => {
    const ev = { key: k, preventDefault() {}, target: { parentElement: { querySelectorAll: () => kash.choices.map((_, j) => ({ focus: () => focused.push('seg' + j) })) } } };
    const handled = c.onSegKey(ev, f, kash, i);
    return [handled, c.fieldValue(f, kash)];
  };
  out.segKeys = [key('ArrowLeft', 2), key('ArrowLeft', 3), key('ArrowRight', 0), key('Home', 3), key('End', 0), key('a', 3)];
  out.segFocus = focused.splice(0);
  out.hints.push(c.choiceHint(f, kash));
  c.pick(f, kash, kash.choices[1]);
  c.setValue(f, comm, true);
  out.options = c.optionsOf('docx');

  // ------------------------------------------------ start → queued → the long poll (running) → done → the page once
  timers.length = 0; calls.length = 0;
  queue.push([202, rows.queued]);
  const p = c.start('docx');
  out.busy = [c.busy.docx, c.canStart(f)];
  const ok = await p;
  out.started = { ok, post: calls[0], active: f.active.id, state: c.stateOf(f).kind, text: c.stateOf(f).text, hint: c.stateOf(f).hint,
    live: c.live, canStart: c.canStart(f), focus: focused.pop(), next: timers[timers.length - 1].ms, top: c.items[0].id, headNote: c.headNote(f) };
  queue.push([200, rows.running]);
  await runTimer();
  out.poll1 = { url: calls[calls.length - 1][1], state: c.stateOf(f).kind, text: c.stateOf(f).text, next: timers[timers.length - 1].ms, history: c.historyTitle(c.items[0]) };
  const doneRow = Object.assign(clone(rows.done), { id: 14, filename: 'كتابي - مع التعليقات.docx', options: { kashida: 'low', comments: true }, options_text: 'كشيدة خفيفة · تعليقات', updated_at: '2026-09-26T10:05:03.200000+02:00' });
  const after = clone(page); after.formats[0].latest = doneRow; after.items = [doneRow].concat(page.items); after.readiness = page.readiness.slice(1);
  queue.push([200, doneRow], [200, after]);
  document.activeElement = 'inside';
  const pending = timers.length;
  await runTimer();
  out.done = { state: c.stateOf(f).kind, name: c.stateOf(f).name, text: c.stateOf(f).text, fresh: c.isFresh(f, c.stateOf(f)), live: c.live,
    calls: calls.slice(-2).map((x) => [x[0], x[1]]), items: c.items.map((r) => r.id), focus: focused.pop(), polls: timers.length - (pending - 1),
    readiness: c.readiness.length, active: f.active, values: c.optionsOf('docx'), download: c.stateOf(f).row.download_url };
  document.activeElement = null;

  // ------------------------------------------------ 409: the export already running, shown with «إلغاء»; 400 per field; 404
  const c2 = make(page); const f2 = c2.fmt('docx');
  timers.length = 0; calls.length = 0;
  queue.push([409, load('response-conflict')]);
  const r409 = await c2.start('docx');
  out.conflict = { ok: r409, error: c2.errorOf(f2), active: f2.active.id, state: c2.stateOf(f2).kind, text: c2.stateOf(f2).text, canStart: c2.canStart(f2), next: timers[timers.length - 1].ms, focus: focused.pop() };
  const c3 = make(page); const f3 = c3.fmt('docx'); const [kash3] = c3.fieldsOf(f3);
  queue.push([400, load('response-bad-options')]);
  await c3.start('docx');
  out.badOptions = { error: c3.errorOf(f3), field: c3.fieldError(f3, kash3), live: c3.live };
  c3.pick(f3, kash3, kash3.choices[0]);
  out.badOptionsCleared = [c3.errorOf(f3), c3.fieldError(f3, kash3)];
  queue.push([404, load('response-no-manuscript')]);
  await c3.start('docx');
  out.noManuscript = c3.errorOf(f3);
  queue.push([202, Object.assign(clone(rows.error), { id: 15, error: 'تعذّر إرسال الإخراج إلى طابور المهام؛ تحقّق من تشغيل Redis وعامل المهام ثم أعد المحاولة.' })], [200, page]);
  const enqueued = await c3.start('docx'); await flush();
  out.enqueueFailed = { ok: enqueued, state: c3.stateOf(f3).kind, text: c3.stateOf(f3).text, live: c3.live };

  // ------------------------------------------------ resuming from formats[].active, the back-off, the hidden tab, the session
  timers.length = 0; calls.length = 0;
  const c4 = make(running); const f4 = c4.fmt('docx');
  out.resume = { polls: timers.map((t) => t.ms), state: c4.stateOf(f4).kind, text: c4.stateOf(f4).text, latest: f4.latest.id, headNote: c4.headNote(f4) };
  queue.push([500, null], [500, null], [500, null], [200, rows.running]);
  const delays = []; const states = [];
  for (let i = 0; i < 3; i++) { await runTimer(); delays.push(timers[timers.length - 1].ms); states.push(c4.pollState); }
  await runTimer();
  out.backoffRun = { delays, states, recovered: [c4.pollState, c4.failures] };
  document.hidden = true; c4.onVisible();
  const before = calls.length;
  await runTimer();
  out.hidden = calls.length - before;
  document.hidden = false; queue.push([200, rows.running]);
  c4.onVisible(); await flush(); await flush();
  out.visible = calls.length - before;
  timers.length = 0; queue.push([403, { detail: 'x' }]);
  await c4.poll('docx');
  out.auth = { pollState: c4.pollState, stopped: c4.stopped, polls: timers.length, canStart: c4.canStart(f4) };

  // ------------------------------------------------ cancel: the previous finished file comes back
  timers.length = 0; calls.length = 0;
  const c5 = make(running); const f5 = c5.fmt('docx');
  queue.push([200, Object.assign(clone(rows.cancelled), { id: 14 })], [200, page]);
  await c5.cancel('docx'); await flush();
  out.cancel = { call: calls[0].slice(0, 2).concat([calls[0][3]]), active: f5.active, state: c5.stateOf(f5).kind, name: c5.stateOf(f5).name, live: c5.live, focus: focused.pop(),
    refreshed: calls.length > 1 ? calls[1].slice(0, 2) : null, history: c5.items.map((r) => [r.id, r.status]) };
  const c6 = make(running); const f6 = c6.fmt('docx');
  queue.push([409, Object.assign(load('response-finished'), { row: Object.assign(clone(rows.done), { id: 14 }) })], [200, page]);
  await c6.cancel('docx'); await flush();
  out.cancelFinished = { error: c6.errorOf(f6), state: c6.stateOf(f6).kind, active: f6.active, latest: f6.latest.id, live: c6.live };

  // ------------------------------------------------ an export that fails while polled
  timers.length = 0;
  const c7 = make(running); const f7 = c7.fmt('docx');
  queue.push([200, Object.assign(clone(rows.error), { id: 14 })], [200, page]);
  await runTimer();
  out.failed = { state: c7.stateOf(f7).kind, text: c7.stateOf(f7).text, live: c7.live, detail: c7.stateOf(f7).row.error_detail };

  // ------------------------------------------------ the comments checkbox at 0, a proofreader, the menu's keys
  const c8 = make(empty); const f8 = c8.fmt('docx'); const [, comm8] = c8.fieldsOf(f8);
  out.empty = { disabled: comm8.disabled, value: c8.fieldValue(f8, comm8), hint: comm8.hint, state: c8.stateOf(f8).kind, headNote: c8.headNote(f8), meta: c8.metaText, items: c8.items.length };
  calls.length = 0;
  const c9 = make(Object.assign(clone(page), { can_edit: false }));
  out.readonly = { canStart: c9.canStart(c9.fmt('docx')), started: await c9.start('docx'), requests: calls.length };
  const items = [0, 1, 2].map((i) => ({ disabled: i === 1, getAttribute: () => null, focus() { document.activeElement = this; focused.push('item' + i); } }));
  const menu = { querySelectorAll: () => items };
  document.activeElement = items[0];
  X.moveIn(menu, 1); X.moveIn(menu, 1); X.moveIn(menu, -1);
  out.menu = focused.splice(-3);
  process.stdout.write(JSON.stringify(out) + '\n');
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_export_component_start_poll_done_conflict_backoff_cancel_and_keys(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    run = subprocess.run(
        ["node", str(harness), str(JS), str(FIXTURES)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    word_hint = "الأسطر كما في المعاينة تقريبًا."
    medium_hint = "يغيّر Word فواصل الأسطر فيطول الكتاب عن المعاينة."
    toc = "toc_update"

    # the meta line: the layout's numbers, «لم تُرتَّب الصفحات بعد», «يُحسب…», Arabic counts with Western digits
    assert out["meta"] == [
        "17×24 سم · 85 صفحة · 6 فصول · Simplified Arabic 13 نقطة",
        "17×24 سم · 6 فصول · Simplified Arabic 13 نقطة · لم تُرتَّب الصفحات بعد",
        "17×24 سم · 6 فصول · Simplified Arabic 13 نقطة · يُحسب…",
        "17×24 سم · 85 صفحة · فصل واحد · Simplified Arabic 10.5 نقطة",
        "",
    ]
    # the form: a choice (the kashida, its label from the fallback) and a boolean with its count
    assert out["fields"] == [
        ["kashida", "choice", "الكشيدة", None, False, 4],
        ["comments", "bool", "تعليقات على الكلمات غير المؤكَّدة", 12, False, 0],
    ]
    assert out["fieldsEmpty"] == [["kashida", False, ""], ["comments", True, "لا كلمات غير مؤكَّدة في الكتاب."]]
    assert out["fieldsBare"] == [["bleed_mm", "value", ""], ["crop_marks", "bool", ""]]  # a bare form block
    assert out["values"] == [{"kashida": "low", "comments": False}, {"kashida": "low", "comments": False}]

    # every state of the state row
    states = out["states"]
    assert states["never"] == ["never", "", "", "", None, "", ""]
    assert states["done"] == [
        "done",
        "dot-success",
        "كتابي.docx",
        "318 KB · قبل ساعتين",
        None,
        "",
        "2026-09-26 10:02",
    ]
    assert states["stale"][:4] == [
        "stale",
        "dot-warning",
        "كتابي.docx",
        "318 KB · قبل 17 ساعة · تغيّر النص بعد هذا الإخراج",
    ]
    assert states["queued"][:6] == [
        "queued",
        "dot-accent is-live",
        "",
        "في الانتظار…",
        None,
        "لم يبدأ الإخراج بعد؛ تأكّد من تشغيل عامل المهام بعد آخر تحديث (make worker).",
    ]
    assert states["running"][:5] == ["running", "dot-accent is-live", "", "كتابة الملف…", None]
    assert states["counted"][3:5] == ["ترتيب الصفحات…", 25]  # the 4 px bar only when the progress is counted
    assert states["error"][:4] == [
        "error",
        "dot-danger",
        "",
        "تعذّر الإخراج: الملف الناتج غير سليم فلم يُسلَّم؛ التفاصيل في سجل الخادم.",
    ]
    assert states["cancelledLatest"][0] == "done"
    # the history lines
    history = out["history"]
    assert history["done"] == ["Word · كتابي.docx · 318 KB", " · علي · كشيدة خفيفة", "dot-success"]
    assert history["stale"] == [
        "Word · كتابي.docx · 318 KB",
        " · علي · كشيدة خفيفة · تغيّر النص بعد هذا الإخراج",
        "dot-warning",
    ]
    assert history["running"] == [
        "Word · قيد الإخراج · كتابة الملف",
        " · علي · كشيدة خفيفة · تعليقات",
        "dot-accent is-live",
    ]
    assert history["queued"] == [
        "Word · قيد الإخراج · في الانتظار",
        " · علي · كشيدة خفيفة · تعليقات",
        "dot-accent is-live",
    ]
    assert history["error"] == ["Word · تعذّر الإخراج", " · علي · كشيدة متوسطة", "dot-danger"]
    assert history["cancelled"] == ["Word · أُلغي", " · علي · كشيدة خفيفة", "dot-neutral"]
    # a note the shown file already carries is not said twice
    assert out["notes"] == ["font_not_embedded"] and out["notesNever"] == ["font_not_embedded", toc]
    assert out["fileWarnings"] == [toc]
    assert out["backoff"] == [2000, 4000, 6000, 8000, 10000, 10000]
    assert out["fill"] == "/api/books/19/exports/14/cancel/"

    # the page: the store, the heads (all four formats available, as the real exporters answer: no single
    # primary format), the kashida hint following the choice
    init = out["init"]
    assert init["view"] is True and init["bar"] is True and init["polls"] == 0  # nothing runs: no poll
    assert init["meta"] == out["meta"][0] and init["warn"] == 3 and init["go"] == "إخراج Word"
    assert init["primary"] == [False, False, False, False]
    assert init["headNote"] == ["", " · لم يُخرَج بعد", " · لم يُخرَج بعد", " · لم يُخرَج بعد"]
    assert init["canStart"] == [True, True, True, True] and init["state"] == "done"
    assert out["hints"] == [word_hint, medium_hint, "يغيّر Word فواصل الأسطر فيطول الكتاب كثيرًا عن المعاينة."]
    assert out["picked"] == [False, False, True, False] and out["tabs"] == [-1, -1, 0, -1]
    # RTL: ← is the next choice (wrapping), → the previous, Home / End; other keys are left alone
    assert out["segKeys"] == [
        [True, "high"],
        [True, "none"],
        [True, "high"],
        [True, "none"],
        [True, "high"],
        [False, "high"],
    ]
    assert out["segFocus"] == ["seg3", "seg0", "seg3", "seg0", "seg3"]
    assert out["options"] == {"kashida": "low", "comments": True}

    # start: POST {format, options} with the CSRF token → queued, the button off, focus on the status line
    assert out["busy"] == ["start", False]
    started = out["started"]
    assert started["ok"] is True
    assert started["post"] == [
        "POST",
        "/api/books/19/exports/",
        {"format": "docx", "options": {"kashida": "low", "comments": True}},
        "tok",
    ]
    assert started["active"] == 14 and started["state"] == "queued" and started["text"] == "في الانتظار…"
    assert started["hint"].startswith("لم يبدأ الإخراج بعد") and started["live"] == "بدأ إخراج Word"
    assert started["canStart"] is False and started["focus"] == '[data-ex-status="docx"]'
    assert started["next"] == 0 and started["top"] == 14 and started["headNote"] == ""
    # the long poll: wait=4 since the row's updated_at (encoded), the backend's step label
    poll1 = out["poll1"]
    assert poll1["url"] == "/api/books/19/exports/14/?wait=4&since=2026-09-26T10%3A05%3A00.120931%2B02%3A00"
    assert poll1["state"] == "running" and poll1["text"] == "كتابة الملف…" and poll1["next"] == 400
    assert poll1["history"] == "Word · قيد الإخراج · كتابة الملف"
    # done: «تنزيل», the bloom, the live region, focus to «تنزيل» (it was inside the block), the page read
    # once
    done = out["done"]
    assert done["state"] == "done" and done["name"] == "كتابي - مع التعليقات.docx" and done["fresh"] is True
    assert done["text"] == "318 KB · قبل ساعتين" and done["live"] == "اكتمل ملف Word · 318 KB"
    assert done["calls"] == [
        ["GET", "/api/books/19/exports/14/?wait=4&since=2026-09-26T10%3A05%3A01.931774%2B02%3A00"],
        ["GET", "/api/books/19/exports/"],
    ]
    assert done["items"] == [14, 12, 11, 10] and done["focus"] == '[data-ex-download="docx"]'
    assert done["polls"] == 0 and done["readiness"] == 3 and done["active"] is None
    assert done["values"] == {"kashida": "low", "comments": True}  # the choices being made stay
    assert done["download"] == "/books/19/exports/12/download/"

    # 409: the running export is shown with «إلغاء» and polled; 400 per field (cleared by a new choice); 404
    conflict = out["conflict"]
    assert conflict["ok"] is False and conflict["error"] == "هذا الملف يُخرَج الآن؛ انتظر انتهاءه أو ألغِه."
    assert conflict["active"] == 14 and conflict["state"] == "running" and conflict["text"] == "كتابة الملف…"
    assert conflict["canStart"] is False and conflict["next"] == 0
    assert conflict["focus"] == '[data-ex-status="docx"]'  # the pressed button gave way to the running strip
    # the card's alert reads a refused start out: the live region stays quiet (not said twice)
    assert out["badOptions"] == {
        "error": "خيارات الإخراج غير صالحة.",
        "field": "قيمة غير معروفة لهذا الخيار.",
        "live": "",
    }
    assert out["badOptionsCleared"] == ["", ""]
    assert out["noManuscript"] == "لا توجد مخطوطة بعد."
    enqueue = out["enqueueFailed"]
    assert enqueue["ok"] is False and enqueue["state"] == "error" and enqueue["live"] == "تعذّر إخراج Word"
    assert enqueue["text"].startswith("تعذّر الإخراج: تعذّر إرسال الإخراج إلى طابور المهام")

    # resuming from formats[].active: polled at once; the latest file stays under it
    assert out["resume"] == {
        "polls": [0],
        "state": "running",
        "text": "كتابة الملف…",
        "latest": 12,
        "headNote": "",
    }
    # failures back off 2 → 4 → 6 s, the pill after the third; a success clears it
    assert out["backoffRun"] == {
        "delays": [2000, 4000, 6000],
        "states": ["ok", "ok", "error"],
        "recovered": ["ok", 0],
    }
    # nothing is fetched while the tab is hidden; back on it, the poll resumes
    assert out["hidden"] == 0 and out["visible"] == 1
    assert out["auth"] == {"pollState": "auth", "stopped": True, "polls": 0, "canStart": False}

    # cancel: the row cancelled, the previous file back, the page read once
    cancel = out["cancel"]
    assert cancel["call"] == ["POST", "/api/books/19/exports/14/cancel/", "tok"]
    assert cancel["active"] is None and cancel["state"] == "done" and cancel["name"] == "كتابي.docx"
    assert cancel["live"] == "أُلغي إخراج Word" and cancel["refreshed"] == ["GET", "/api/books/19/exports/"]
    assert cancel["focus"] == '[data-ex-go="docx"]'  # «إلغاء» is gone: the focus goes to «إخراج Word»
    assert cancel["history"] == [[12, "done"], [11, "done"], [10, "error"]]
    finished = out["cancelFinished"]
    assert (
        finished["error"] == "انتهى هذا الإخراج."
        and finished["state"] == "done"
        and finished["active"] is None
    )
    assert finished["latest"] == 14 and finished["live"] == "اكتمل ملف Word · 318 KB"
    # a failure while polled: «تعذّر الإخراج: …», the technical line kept for the details
    failed = out["failed"]
    assert failed["state"] == "error" and failed["live"] == "تعذّر إخراج Word"
    assert failed["text"] == "تعذّر الإخراج: الملف الناتج غير سليم فلم يُسلَّم؛ التفاصيل في سجل الخادم."
    assert failed["detail"].startswith("InvalidExport: ")

    # no uncertain word: the comments checkbox disabled and off; never exported: «لم يُخرَج بعد»
    assert out["empty"] == {
        "disabled": True,
        "value": False,
        "hint": "لا كلمات غير مؤكَّدة في الكتاب.",
        "state": "never",
        "headNote": " · لم يُخرَج بعد",
        "meta": "",
        "items": 0,
    }
    # a proofreader starts nothing (no request)
    assert out["readonly"] == {"canStart": False, "started": False, "requests": 0}
    # the menu: ↓ skips a disabled item and wraps, ↑ goes back
    assert out["menu"] == ["item2", "item0", "item2"]


# ---------------------------------------------------------------- answers out of order, the pill, a hung poll

RACES = r"""
const reg = {}; const inits = []; const stores = {}; const focused = [];
globalThis.window = globalThis;
const node = (sel) => ({ sel, focus: () => focused.push(sel), contains: () => globalThis.document.activeElement === 'inside' });
globalThis.document = { hidden: false, activeElement: null, documentElement: { dir: 'rtl' },
  addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); },
  createElement: () => ({}), querySelector: (sel) => (sel === 'meta[name="csrf-token"]' ? { content: 'tok' } : node(sel)), querySelectorAll: () => [] };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: (n, v) => { if (v !== undefined) stores[n] = v; return stores[n]; } };
// timers with real clear semantics, run by hand; a long poll's time limit (14 s) is told apart from the polls
let tid = 0; const timers = new Map();
globalThis.setTimeout = (fn, ms) => { tid += 1; timers.set(tid, { fn, ms: ms || 0 }); return tid; };
globalThis.clearTimeout = (id) => { timers.delete(id); };
globalThis.setInterval = () => 0; globalThis.clearInterval = () => {};
const NOW = Date.parse('2026-09-26T12:02:13+02:00'); Date.now = () => NOW;
const fs = require('fs'); const path = require('path');
const [js, fixtures] = [process.argv[2], process.argv[3]];
eval(fs.readFileSync(path.join(js, 'manuscript.js'), 'utf8'));
eval(fs.readFileSync(path.join(js, 'export.js'), 'utf8'));
inits.forEach((fn) => fn());
const load = (name) => JSON.parse(fs.readFileSync(path.join(fixtures, name + '.json'), 'utf8'));
const clone = (v) => JSON.parse(JSON.stringify(v));
// a fetch whose answers are given by hand, in any order; an aborted request rejects as the browser's does
const calls = [];
globalThis.fetch = (url, init) => new Promise((resolve, reject) => {
  const call = { method: (init && init.method) || 'GET', url, resolve, settled: false, signal: (init && init.signal) || null };
  calls.push(call);
  if (call.signal) call.signal.addEventListener('abort', () => {
    if (!call.settled) { call.settled = true; reject(Object.assign(new Error('aborted'), { name: 'AbortError' })); }
  });
});
const flush = async () => { for (let k = 0; k < 10; k += 1) await new Promise((r) => setImmediate(r)); };
const answer = async (call, status, data) => { call.settled = true; call.resolve({ ok: status < 400, status, json: async () => clone(data) }); await flush(); };
const open = (pred) => calls.find((c) => !c.settled && pred(c));
const LIMIT = 14000;
const polls = () => [...timers.values()].filter((t) => t.ms < LIMIT).map((t) => t.ms);
const fire = async (which) => {
  const due = [...timers.entries()].filter(([, t]) => which(t.ms));
  due.forEach(([id]) => timers.delete(id)); due.forEach(([, t]) => t.fn()); await flush();
};
const runPolls = () => fire((ms) => ms < LIMIT);
const make = (config) => { const c = reg.exportPage(clone(config)); c.init(); return c; };
const spy = (c) => { const said = []; const say = c.say.bind(c); c.say = (m) => { said.push(m); say(m); }; return said; };
const poll14 = (x) => x.url.startsWith('/api/books/19/exports/14/?');
const page = (x) => x.url === '/api/books/19/exports/';
const X = NassakhExport;
const LATER = '2026-09-26T10:05:04.100000+02:00';
(async () => {
  const out = {};
  // ------------------------------------------------ staleness: an export only moves on
  const r = (status, at, id = 1) => ({ id, status, updated_at: `2026-09-26T10:05:0${at}.000000+02:00` });
  out.staler = [X.staler(r('running', 5), r('done', 1)), X.staler(r('done', 1), r('running', 5)), X.staler(r('queued', 5), r('running', 1)),
    X.staler(r('running', 1), r('running', 2)), X.staler(r('running', 3), r('running', 2)), X.staler(r('running', 1), null),
    X.staler(r('running', 1, 2), r('done', 5))];

  // ------------------------------------------------ two exports ending close together (Word, then EPUB)
  {
    const cfg = load('page-running');
    const epubRun = Object.assign(clone(cfg.formats[0].active), { id: 20, format: 'epub', format_label: 'EPUB', filename: 'كتابي.epub', options: {}, options_text: '' });
    cfg.formats[3].active = epubRun; cfg.items.unshift(epubRun);
    const c = make(cfg); const fw = c.fmt('docx'); const fe = c.fmt('epub'); const said = spy(c);
    await runPolls();                                                    // both long polls on the wire
    const wordDone = Object.assign(clone(load('row-done')), { id: 14, updated_at: LATER, finished_at: LATER });
    await answer(open(poll14), 200, wordDone);                           // Word ends: the page is read (R1)
    const r1 = open(page);
    const r1Payload = clone(cfg);                                        // R1 is read before EPUB ends
    r1Payload.formats[0].active = null; r1Payload.formats[0].latest = wordDone;
    r1Payload.items = r1Payload.items.map((row) => (row.id === 14 ? wordDone : row));
    const epubDone = Object.assign(clone(epubRun), { status: 'done', status_label: 'اكتمل', size_text: '210 KB', size_bytes: 215040,
      download_url: '/books/19/exports/20/download/', finished_at: LATER, updated_at: LATER });
    document.activeElement = 'inside';
    await answer(open((x) => x.url.startsWith('/api/books/19/exports/20/?')), 200, epubDone); // EPUB ends while R1 is on the wire
    out.whileR1 = { refreshes: calls.filter(page).length, again: c.refreshAgain };
    await answer(r1, 200, r1Payload);                                    // R1: EPUB still running there
    const r2 = open(page);
    out.afterR1 = { epubActive: fe.active, epubState: c.stateOf(fe).kind, top: c.items.slice(0, 2).map((row) => [row.id, row.status]),
      polls: polls().length, second: Boolean(r2) };
    const r2Payload = clone(r1Payload); r2Payload.formats[3].active = null; r2Payload.formats[3].latest = epubDone;
    r2Payload.items = r2Payload.items.map((row) => (row.id === 20 ? epubDone : row));
    await answer(r2, 200, r2Payload);
    document.activeElement = null;
    out.ended = { word: c.stateOf(fw).kind, epub: c.stateOf(fe).kind, refreshes: calls.filter(page).length, said,
      focus: focused.filter((sel) => sel.includes('epub')), polls: polls().length };
  }

  // ------------------------------------------------ a cancel answered before the long poll that was on the wire
  {
    calls.length = 0; timers.clear(); focused.length = 0;
    const c = make(load('page-running')); const f = c.fmt('docx'); const said = spy(c);
    await runPolls();
    const p1 = open(poll14);
    const cancelled = Object.assign(clone(load('row-cancelled')), { id: 14, updated_at: '2026-09-26T10:05:03.000000+02:00' });
    const pending = c.cancel('docx'); await flush();
    await answer(open((x) => x.method === 'POST'), 200, cancelled); await pending;
    const now = clone(load('page-running')); now.formats[0].active = null; now.items = now.items.map((row) => (row.id === 14 ? cancelled : row));
    await answer(open(page), 200, now);
    const before = Object.assign(clone(load('row-running')), { updated_at: '2026-09-26T10:05:02.500000+02:00' });
    await answer(p1, 200, before);                                       // P1: the row as read before the cancel
    out.race = { active: f.active, state: c.stateOf(f).kind, polls: polls().length, history: c.items.find((row) => row.id === 14).status,
      said, focus: focused.slice(), requests: calls.length };
  }

  // ------------------------------------------------ the «تعذّر التحديث» pill: only a poll that succeeds clears it
  {
    calls.length = 0; timers.clear();
    const c = make(load('page-running'));
    for (let i = 0; i < 3; i += 1) { await runPolls(); await answer(open(poll14), 500, null); }
    out.pill = { afterThree: [c.pollState, c.failures] };
    const retry = c.pollNow(); await flush();
    out.pill.whileRetrying = c.pollState;
    await answer(open(poll14), 500, null); await retry;
    out.pill.failedRetry = [c.pollState, c.failures, polls()];
    await runPolls(); await answer(open(poll14), 200, load('row-running'));
    out.pill.recovered = [c.pollState, c.failures];
  }

  // ------------------------------------------------ a 404 after failed polls: the server answered, the pill goes
  {
    calls.length = 0; timers.clear();
    const c = make(load('page-running'));
    for (let i = 0; i < 3; i += 1) { await runPolls(); await answer(open(poll14), 500, null); }
    await runPolls(); await answer(open(poll14), 404, { detail: 'الإخراج غير موجود.' });
    await answer(open(page), 200, load('page'));
    out.gone = [c.pollState, c.failures, polls().length, c.fmt('docx').active];
  }

  // ------------------------------------------------ a long poll that never answers is given up and retried
  {
    calls.length = 0; timers.clear();
    const c = make(load('page-running'));
    await runPolls();
    const hung = open(poll14);
    out.hung = { signal: Boolean(hung.signal), limits: [...timers.values()].map((t) => t.ms) };
    await fire((ms) => ms === LIMIT);                                    // 14 s pass
    out.hung.after = [hung.settled, c.failures, polls()];
    await runPolls();
    out.hung.polls = calls.filter(poll14).length;
  }

  // ------------------------------------------------ queued → running keeps «إلغاء» (its key is the export)
  {
    calls.length = 0; timers.clear();
    const c = make(load('page')); const f = c.fmt('docx');
    const started = c.start('docx'); await flush();
    await answer(open((x) => x.method === 'POST'), 202, load('row-queued')); await started;
    const queued = [c.cancelKey(c.stateOf(f)), c.stateKey(c.stateOf(f))];
    await runPolls(); await answer(open(poll14), 200, load('row-running'));
    out.keys = [queued, [c.cancelKey(c.stateOf(f)), c.stateKey(c.stateOf(f))]];
  }
  process.stdout.write(JSON.stringify(out) + '\n');
  process.exit(0);
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_export_component_answers_out_of_order_the_pill_and_a_hung_poll(tmp_path):
    """F1–F3, F6, F7 of the Phase 6 review: an export seen final never comes back as running (two exports
    ending close together, a cancel racing a long poll), a refresh asked for while one is on the wire runs
    once more, the pill stays until a poll succeeds, a 404 clears it, a hung long poll is given up."""
    harness = tmp_path / "races.js"
    harness.write_text(RACES, encoding="utf-8")
    run = subprocess.run(
        ["node", str(harness), str(JS), str(FIXTURES)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])

    # a row only moves on: final never back to running, running never back to queued, older never over newer
    assert out["staler"] == [True, False, True, True, False, False, False]

    # EPUB ends while the payload read after Word's end is on the wire: that refresh is not dropped but runs
    # again, the stale payload never brings EPUB back as running, and each export is announced once
    assert out["whileR1"] == {"refreshes": 1, "again": True}
    assert out["afterR1"] == {
        "epubActive": None,
        "epubState": "done",
        "top": [[20, "done"], [14, "done"]],
        "polls": 0,
        "second": True,
    }
    ended = out["ended"]
    assert ended["word"] == "done" and ended["epub"] == "done" and ended["refreshes"] == 2
    assert ended["said"] == ["اكتمل ملف Word · 318 KB", "اكتمل ملف EPUB · 210 KB"]
    assert ended["focus"] == ['[data-ex-download="epub"]'] and ended["polls"] == 0

    # a long poll read before the cancel answers after it: nothing comes back, nothing is polled again
    race = out["race"]
    assert race["active"] is None and race["state"] == "done" and race["polls"] == 0
    assert race["history"] == "cancelled" and race["said"] == ["أُلغي إخراج Word"]
    assert race["focus"] == ['[data-ex-go="docx"]'] and race["requests"] == 3  # poll, cancel, the page

    # the pill: shown after three failures, kept while the retry is on the wire and after it fails (the
    # back-off goes on: 8 s), gone once a poll succeeds
    assert out["pill"] == {
        "afterThree": ["error", 3],
        "whileRetrying": "error",
        "failedRetry": ["error", 4, [8000]],
        "recovered": ["ok", 0],
    }
    # the row gone (404) after failures: the server answered, so no pill and no poll left
    assert out["gone"] == ["ok", 0, 0, None]
    # a hung long poll: sent with a time limit (wait + 10 s), given up then (a failure), retried after 2 s
    assert out["hung"] == {"signal": True, "limits": [14000], "after": [True, 1, [2000]], "polls": 2}
    # «إلغاء» keeps its key from queued to running (the status line's key moves on)
    assert out["keys"] == [["active:14", "queued:14"], ["active:14", "running:14"]]
