"""The book page (PHASE5_SPEC §9, D47): `/books/<id>/layout/`, live pages with preview and edit.

- the rendered template in every state through the Django test client (no manuscript, nothing laid out yet,
  a real render's first live pages embedded, edit mode asked for, a proofreader), the redirect of the old
  editor address, the compiled CSS
- the esbuild bundle (built and current) and its pure modules under Node: the document ↔ editor conversion
  and the find matcher against the server's own (`editor.document`), the D47 block helpers by plain offsets
  (split, merge, paste, the chapter's nodes by block id, replace, unmark) against `inline_text`, the offsets ↔
  positions of the one-block editor's schema, the ProseMirror plugins on a state without a DOM
- `static/src/js/book/geometry.js` under Node: a layout page drawn as positioned text (geometry, justify,
  runs, the page furniture), clicks mapped to offsets through collapsed white space, the flow around an open
  paragraph, a re-layout spliced in (renumbering, the side swap on an odd delta), the virtualisation window,
  the keyboard maps of both modes, the side panel's lists
- the `bookPage` Alpine component under Node with a tiny DOM, a fetch stub and a stub one-block editor: the
  first paint and the layout windows, the mode switch, a click opening a paragraph in place at the offset, the
  lines below moving with it, the pause → PUT → re-layout → pages swapped keeping the caret, the delta and the
  side flip, the 409 banner, Enter / Backspace / the chapter's undo, the tabs per mode and their memory, the
  uncertain words' actions, find navigating pages, the stylesheet's change laying the book out again, the
  keyboard, a proofreader."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse

import pytest

from books.models import Book
from editor import document as doc
from editor.models import Manuscript
from editor.tests import heading, note, para, sample_document

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "src" / "js"
BOOK_JS = JS / "book"
SRC = ROOT / "static" / "src" / "editor"
BUNDLE = ROOT / "static" / "dist" / "editor.js"
CSS = ROOT / "static" / "dist" / "app.css"
BOOK_FILES = ("geometry.js", "stage.js", "style.js", "cover.js", "edit.js", "panel.js", "page.js")
NODE = shutil.which("node")


# ---------------------------------------------------------------- users, books


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


def _book(title="كتاب التنسيق"):
    book = Book.objects.create(title=title, author="المؤلف", status=Book.Status.REVIEWING)
    Manuscript.objects.create(book=book, document=sample_document(), version=1)
    return book


def _json_script(body: str, element_id: str):
    match = re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>', body, re.S)
    assert match, element_id
    return json.loads(match.group(1))


def _page(client, book, query="") -> tuple[str, dict]:
    body = client.get(reverse("editor:layout", args=[book.pk]) + query).content.decode()
    return body, _json_script(body, "book-config")


def _between(body: str, start: str, end: str) -> str:
    i = body.index(start)
    return body[i : body.index(end, i)]


# ---------------------------------------------------------------- the template: every state


def test_book_page_without_a_manuscript_shows_the_empty_state(editor):
    book = Book.objects.create(title="بلا مخطوطة")
    body, config = _page(_logged(editor), book)
    assert "لا توجد مخطوطة بعد" in body and "data-book " not in body and "lo-bar" not in body
    assert reverse("assembly:manuscript", args=[book.pk]) in body
    assert config["exists"] is False and config["initial"]["preview"] is None
    # the book page's scripts come with every page; the editor bundle with this one
    for name in BOOK_FILES:
        assert f"src/js/book/{name}" in body, name
    assert "dist/editor.js" in body and "src/js/editor.js" not in body and "src/js/layout.js" not in body
    assert Client().get(reverse("editor:layout", args=[book.pk])).status_code == 302


def test_book_page_first_paint_before_any_layout(editor):
    book = _book()
    body, config = _page(_logged(editor), book)
    assert "<title>الكتاب · كتاب التنسيق · نسّاخ</title>" in body
    # D76 (PHASE7 §5.1): the h1 holds the title only; «الكتاب» is the stage bar's current step, the rail folds
    assert '<h1 class="page-title">كتاب التنسيق</h1>' in body
    assert 'data-stage-bar data-rail data-current="book"' in body and "data-book data-rail" in body
    assert config["page"] == "layout" and config["mode"] == "preview" and config["canEdit"] is True
    assert config["initial"]["layout"] is None and config["initial"]["preview"]["status"] == "none"
    assert config["relayoutMs"] == 500 and '@font-face { font-family: "nk-body"' in config["fontCss"]
    urls = config["urls"]
    for key in (
        "pageLayout",
        "relayout",
        "relayoutStatus",
        "chapter",
        "chapters",
        "findReplace",
        "uncertain",
    ):
        assert urls[key], key
    # the root: live pages in both modes, the window's events, the render's faces in a <style>
    assert (
        'data-book data-rail x-data="bookPage('
        "JSON.parse(document.getElementById('book-config').textContent))\"" in body
    )
    assert "'is-edit': mode === 'edit'" in body and ':data-mode="mode"' in body
    assert '@keydown.window="onKey($event)"' in body and '@beforeunload.window="guardUnload($event)"' in body
    assert '<style data-font-css x-text="fontCss">@font-face { font-family: &quot;nk-body&quot;' in body or (
        '<style data-font-css x-text="fontCss">@font-face { font-family: "nk-body"' in body
    )
    # the top bar: the save pill, the render pill, the poll pills, the «⋯» menu (snapshots, digits, drift,
    # the PDF, the shortcuts, the manuscript, the dashboard)
    assert 'x-data="bookBar"' in body and "data-save-pill" in body and "data-render-pill" in body
    assert "تعذّر التحديث · إعادة المحاولة" in body and "انتهت الجلسة · تسجيل الدخول" in body
    menu = _between(body, "data-book-menu", "</template>")
    for label in (
        "نسخة محفوظة…",
        "تحويل الأرقام…",
        "إعادة بناء الفصل من المراجعة…",
        "PDF المعاينة",
        "اختصارات لوحة المفاتيح",
        "المخطوطة",
    ):
        assert label in menu, label
    assert "لوحة الكتاب" not in body  # D77: retired (§5.5); the side panel is «أدوات الكتاب»
    assert '<aside class="bk-side lo-side bp-side" aria-label="أدوات الكتاب" data-side>' in body
    assert 'x-show="v.driftChapter"' in menu and ':href="v.pdfUrl' in menu
    # the toolbar: «معاينة | تحرير» first, preview's spread and fit, edit's history, style picker, B / I,
    # «حاشية», the scan page marks, the counter, the jump field (preview), the primary «تم» (edit)
    toolbar = _between(body, 'class="lo-toolbar bp-toolbar"', 'class="lo-banners"')
    assert (
        toolbar.index("data-mode-toggle")
        < toolbar.index("data-preview-tools")
        < toolbar.index("data-edit-tools")
    )
    assert ">معاينة</button>" in toolbar and ">تحرير</button>" in toolbar and "setMode('edit')" in toolbar
    assert "data-spread-toggle" in toolbar and "data-fit-toggle" in toolbar and "<bdi>100 %</bdi>" in toolbar
    edit_tools = _between(toolbar, "data-edit-tools", "lo-toolbar-end")
    for needle in (
        'aria-label="تراجع"',
        'aria-label="إعادة"',
        "data-style-picker",
        'aria-label="غامق"',
        'aria-label="مائل"',
        "data-footnote-button",
        "فواصل الصفحات الأصلية",
    ):
        assert needle in edit_tools, needle
    assert "x-transition.opacity.duration.200ms" in toolbar  # the groups swap with a 200 ms fade
    assert (
        "data-counter" in toolbar
        and 'placeholder="إلى صفحة…"' in toolbar
        and "data-done" in toolbar
        and ">تم</button>" in toolbar
    )
    # the stage: skeleton, two sheets, each a live page (lines + the paragraph editor's host), the error state
    for side in ("right", "left"):
        sheet = _between(body, f'data-sheet="{side}"', "</figure>")
        assert f"onSheetClick($event, '{side}')" in sheet and f"onSheetDblClick($event, '{side}')" in sheet
        assert 'class="lp-lines" data-lines' in sheet and 'class="lp-edit" data-edit-host' in sheet
    assert (
        "data-skeleton" in body
        and "data-error-state" in body
        and 'class="bk-turn bk-turn-prev lo-turn"' in body
    )
    # the one side panel: seven icon tabs in order, each with its icon and name, one panel each, and «تغييرات
    # المراجعة» (D78) shown only while review's changes wait
    side = _between(body, '<aside class="bk-side lo-side bp-side"', "</aside>")
    tabs = re.findall(r'data-tab="(\w+)" title="([^"]+)"', side)
    assert tabs == [
        ("chapters", "الفصول"),
        ("pages", "الصفحات"),
        ("find", "بحث"),
        ("format", "التنسيق"),
        ("block", "الفقرة"),
        ("source", "الأصل"),
        ("uncertain", "غير المؤكَّدة"),
        ("changes", "تغييرات المراجعة"),
    ]
    icons = re.findall(
        r'class="bp-tab"[^>]*>\s*<svg class="icon" aria-hidden="true"><use href="#(i-[\w-]+)"/>', side
    )
    assert icons == [
        "i-list",
        "i-pages",
        "i-search",
        "i-sliders",
        "i-pilcrow",
        "i-image",
        "i-uncertain",
        "i-merge",
    ]
    assert 'x-show="hasChangesTab" x-cloak>' in _between(side, 'data-tab="changes"', "</button>")
    for key in ("chapters", "pages", "find", "format", "block", "source", "uncertain", "changes"):
        assert f'data-panel="{key}"' in side and f"x-show=\"tab === '{key}'\"" in side, key
    assert 'x-text="uncertain.count"' in side and "bp-badge is-warn" in side
    # «التنسيق»: eight accordions in order (the cover first, D80: core/test_cover_ui.py), with the book
    # details and the D47 fields; the organisation's templates last (D98: accounts/test_templates.py)
    sections = re.findall(r'data-section="(\w+)"', side)
    assert sections == ["cover", "trim", "margins", "fonts", "text", "page", "details", "templates"]
    for needle in (
        'field="widows"',
        'field="orphans"',
        "keep_headings",
        "front_matter.copyright_page",
        "data-book-details",
        "بيانات الكتاب",
    ):
        assert needle in side, needle
    # «الأصل»: exactly the editor page's pane (the scan with its bands, the pager, «عرض الأصل», the review
    # link)
    source = _between(side, 'data-panel="source"', "</section>")
    for needle in (
        "ed-scan ed-scan-thumb",
        "sourceBoxes()",
        "ed-band",
        "sourceStep(-1)",
        "عرض الأصل <kbd",
        "فتح في المراجعة",
    ):
        assert needle in source, needle
    # «غير المؤكَّدة»: groups by chapter and page, readings, the typed correction, accept
    unc = _between(side, 'data-panel="uncertain"', "</section>")
    for needle in (
        "uncertainGroups",
        "g.pages",
        "resolveUncertain(w, r.current ? 'accept' : 'choose'",
        "resolveUncertain(w, 'type'",
        "قبول الكلمة كما هي",
    ):
        assert needle in unc, needle
    # «بحث»: the options, the scopes, the matches with their pages; «الفقرة»: the flags
    find = _between(side, 'data-panel="find"', "</section>")
    for needle in (
        "مطابقة التشكيل",
        "توحيد الألف",
        "كلمة كاملة",
        "هذا الفصل",
        "الكتاب كلّه",
        "data-find-results",
        "m.page",
        "استبدال الكل",
    ):
        assert needle in find, needle
    block = _between(side, 'data-panel="block"', "</section>")
    assert "ابدأ صفحة جديدة" in block and "مع التالية" in block and "toggleFlag('breakBefore')" in block
    # the layers: the one overlay, the drawer, the dialogs, the undo toast
    for needle in (
        "data-pop",
        "data-drawer",
        "النسخ المحفوظة",
        "تحويل الأرقام",
        "data-shortcuts",
        "data-undo-toast",
    ):
        assert needle in body, needle
    # the app's sidebar folds to an icon rail on this page (base.html + layout.css), with its toggle
    assert 'class="btn-icon rail-toggle"' in body and "'is-rail-open': railOpen" in body
    for icon in ("i-list", "i-pages", "i-pilcrow", "i-uncertain", "i-sidebar"):
        assert f'<symbol id="{icon}"' in body, icon


def test_book_page_in_edit_mode_on_a_chapter(editor):
    book = _book()
    body, config = _page(_logged(editor), book, "?chapter=h20&mode=edit")
    assert config["mode"] == "edit" and config["requestedChapter"] == "h20" and config["chapter"] == "h20"
    # the old editor address sends to the book page in edit mode on the chapter
    response = _logged(editor).get(reverse("editor:edit", args=[book.pk]) + "?chapter=h20")
    assert response.status_code == 302
    assert response["Location"] == reverse("editor:layout", args=[book.pk]) + "?chapter=h20&mode=edit"
    # an editor's sections are enabled; the uncertain words are buttons (the keyboard reaches each), their
    # page number isolated; the PDF item stays inert before the first render; the style menu and the
    # popover never show together
    assert re.search(r'<fieldset class="lo-section[^"]*"[^>]*\sdisabled>', body) is None
    assert '<button type="button" class="bp-unc-context"' in body and 'ص <bdi x-text="p.page"></bdi>' in body
    assert '@click="if (!v.pdfUrl) $event.preventDefault(); else open = false" data-pdf-link' in body
    assert '@click="closePop(); styleMenu = !styleMenu"' in body


def test_book_page_for_a_proofreader_is_read_only(proofreader):
    book = _book()
    body, config = _page(_logged(proofreader), book, "?mode=edit")
    assert config["canEdit"] is False and config["mode"] == "edit"  # the component refuses it (canEdit)
    assert "data-mode-toggle" not in body and "data-edit-tools" not in body and "data-done" not in body
    # every section's fields are disabled, its head still opens and closes (the reader sees every value)
    assert '<fieldset class="lo-panel is-readonly"' in body and "التنسيق يغيّره محرّر الكتاب" in body
    assert body.count('x-show="sections.') == 7 and body.count("{% if") == 0
    assert all(
        f'x-show="sections.{k}"' in body
        for k in ("cover", "trim", "margins", "fonts", "text", "page", "details")
    )
    assert len(re.findall(r'<fieldset class="lo-section[^"]*"[^>]*\sdisabled>', body)) == 7
    assert re.search(r'<button type="button" class="bp-acc-head"[^>]*disabled', body) is None
    assert (
        "تحويل الأرقام…" not in body
        and "استبدال الكل" not in body
        and "resolveUncertain(w, 'type'" not in body
    )
    assert "data-preview-tools" in body and "data-spread-toggle" in body


def test_book_page_embeds_the_first_live_pages_of_a_real_render(editor):
    from publishing import preview

    book = _book()
    preview.render_preview(book, "book")
    body, config = _page(_logged(editor), book, "?chapter=h20")
    layout = config["initial"]["layout"]
    assert layout and layout["revision"] == 1 and layout["pages"]
    first = layout["pages"][0]
    h20 = next(c for c in layout["chapters"] if c["id"] == "h20")
    assert first["n"] == h20["first"]
    # a page as the stage draws it: its size, margins, lines with boxes, runs and ranges
    assert first["width_pt"] > 0 and set(first["margins"]) == {"top", "right", "bottom", "left"}
    line = next(line for line in first["lines"] if line["block"] == "h20")
    assert (
        line["kind"] == "heading" and line["runs"][0]["font"] == "nk-heading" and line["end"] > line["start"]
    )
    assert {"x", "y", "w", "h", "baseline", "dir", "justify", "first"} <= set(line)
    assert (
        layout["geometry"]["side_shift_pt"] is not None
        and '@font-face { font-family: "nk-body"' in layout["font_css"]
    )


def _rule(css: str, selector: str) -> set[str]:
    """The declarations of every rule with exactly this selector (the minifier reorders them), also the first
    rule of a media query."""
    out: set[str] = set()
    for match in re.finditer(r"(?:^|[{}\s])" + re.escape(selector) + r"\{([^{}]*)\}", css):
        out |= {d.strip() for d in match.group(1).split(";") if d.strip()}
    assert out, selector
    return out


def test_compiled_css_draws_live_pages_and_the_panel():
    css = CSS.read_text(encoding="utf-8")
    # the screen takes the window; the sidebar folds to an icon rail on every book screen (`data-rail`, 7c
    # §5.2), unfolds over it
    assert "height:calc(100dvh - var(--topbar-height))" in _rule(css, ".main:has(>.lo-screen)")
    assert "grid-template-columns:56px minmax(0,1fr)" in _rule(css, ".app-shell:has([data-rail])")
    assert {"position:fixed", "width:var(--sidebar-width)", "box-shadow:var(--shadow-pop)"} <= _rule(
        css, ".app-shell:has([data-rail]).is-rail-open .sidebar"
    )
    # a live page: an inline-size container; one point = 100cqw / its width in points; lines placed in points
    assert {
        "--u:calc(100cqw / var(--pw,481.89))",
        "container-type:inline-size",
        "background:#fff",
        "overflow:hidden",
    } <= _rule(css, ".lp-page")
    line = _rule(css, ".lp-line")
    assert {
        "position:absolute",
        "left:calc(var(--x) * var(--u))",
        "top:calc(var(--y) * var(--u))",
        "width:calc(var(--w) * var(--u))",
    } <= line
    assert {
        "height:calc(var(--h) * var(--u))",
        "line-height:calc(var(--h) * var(--u))",
        "white-space:nowrap",
    } <= line
    assert "transform:translateY(calc(var(--dy,0) * var(--u)))" in line
    assert {"text-align:justify", "text-align-last:justify"} <= _rule(css, ".lp-line.is-j")
    assert "font-family:nk-body,nk-latin,serif" in _rule(
        css, ".f-b"
    ) and "font-family:nk-heading,nk-latin,serif" in _rule(css, ".f-h")
    assert {"vertical-align:super", "line-height:0"} <= _rule(css, ".lp-run.is-sup")
    assert "justify-content:center" in _rule(
        css, ".lp-header[data-align=center],.lp-number[data-align=center]"
    )
    assert "visibility:hidden" in _rule(css, ".lp-line.is-hidden,.lp-line.is-over")
    # the paragraph opened in place: the page's measure, face, size and leading in points
    patch = _rule(css, ".lp-patch")
    assert {
        "position:absolute",
        "left:calc(var(--x) * var(--u))",
        "top:calc(var(--top,0) * var(--u))",
        "width:calc(var(--w) * var(--u))",
    } <= patch
    assert {
        "line-height:calc(var(--lh,22.1) * var(--u))",
        "font-size:calc(var(--fs,13) * var(--u))",
        "text-align:justify",
    } <= patch
    assert "text-indent:calc(var(--indent,0) * var(--u))" in _rule(css, ".lp-patch.is-body .ed-p")
    # the icon tab bar: the active tab labelled, badges; one scrolling body
    assert "display:inline" in _rule(css, ".bp-tab.is-active .bp-tab-label")
    assert "overflow-y:auto" in _rule(css, ".bp-tab-body")
    # reduced motion drops the turns, the fades, the shimmer
    assert "@media (prefers-reduced-motion:reduce)" in css
    assert "animation:none" in _rule(
        css, ".bp-panel,.ed-pop,.ed-drawer,.lp-line.is-flash,.lp-line.is-applied,.bp-changes-skel span"
    )
    # the old editor page's styles are gone
    assert ".ed-sheet{" not in css and ".ed-toolbar{" not in css


# ---------------------------------------------------------------- the bundle


def test_editor_bundle_is_built_and_current():
    assert BUNDLE.is_file(), "run npm run build:editor"
    head = BUNDLE.read_bytes()[:120]
    assert b"Nassakh chapter editor bundle" in head
    if NODE is None:
        pytest.skip("node is not installed")
    check = subprocess.run([NODE, "--check", str(BUNDLE)], capture_output=True, text=True, timeout=60)
    assert check.returncode == 0, check.stderr
    for path in [
        *(SRC / name for name in ("convert.js", "schema.js", "index.js")),
        *(BOOK_JS / name for name in BOOK_FILES),
    ]:
        check = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=60)
        assert check.returncode == 0, (path, check.stderr)
    if not (ROOT / "node_modules" / "esbuild").is_dir():
        pytest.skip("node_modules not installed")
    before = BUNDLE.read_bytes()
    build = subprocess.run(
        ["npm", "run", "-s", "build:editor"], cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    assert build.returncode == 0, build.stderr
    assert BUNDLE.read_bytes() == before, (
        "static/dist/editor.js is stale: run npm run build:editor and commit it"
    )
    assert b"createBlock" in before and b"createNote" in before


def _node_tmp(tmp_path: Path) -> Path:
    """A folder whose bare imports resolve through the repo's node_modules (one copy of every package)."""
    if NODE is None:
        pytest.skip("node is not installed")
    if not (ROOT / "node_modules" / "@tiptap" / "core").is_dir():
        pytest.skip("node_modules not installed")
    os.symlink(ROOT / "node_modules", tmp_path / "node_modules", target_is_directory=True)
    return tmp_path


def _run_node(tmp_path: Path, name: str, source: str, *args: str) -> dict:
    harness = tmp_path / name
    harness.write_text(source, encoding="utf-8")
    run = subprocess.run(
        [NODE, str(harness), *args], capture_output=True, text=True, timeout=120, cwd=tmp_path
    )
    assert run.returncode == 0, run.stderr[-6000:]
    return json.loads(run.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------- the schema module under Node


def _chapter_nodes() -> list:
    """The sample document's blocks after the title (what a chapter save carries), plus every node kind and
    the D47 page-break flags."""
    nodes = sample_document()["content"][1:]
    nodes[1] = dict(nodes[1], attrs=dict(nodes[1]["attrs"], breakBefore=True))
    nodes[2] = dict(nodes[2], attrs=dict(nodes[2]["attrs"], keepWithNext=False))
    nodes.append(
        {
            "type": "paragraph",
            "attrs": {
                "id": "p30",
                "sourcePages": [5, 6],
                "sourceLineIds": [50, 51],
                "reviewed": False,
                "style": "verse",
            },
            "content": [
                {"type": "text", "text": "شطر أول"},
                {"type": "hardBreak"},
                {"type": "text", "text": "شطر ثانٍ ", "marks": [{"type": "italic"}]},
                {"type": "pageBreak", "attrs": {"page": 6, "printed": "6"}},
                {"type": "text", "text": "مليتية", "marks": [{"type": "uncertain"}]},
                {"type": "text", "text": " وبرقة", "marks": [{"type": "bold"}]},
            ],
        }
    )
    nodes.append(
        {
            "type": "separator",
            "attrs": {"id": "s1", "sourcePages": [6], "sourceLineIds": [], "reviewed": True},
        }
    )
    return nodes


QUERIES = [
    ("برقة", {}),
    ("بَرقَة", {}),
    ("برقة", {"whole_word": True}),
    ("ابراهيم", {}),
    ("ابراهيم", {"fold_alef": False}),
    ("مدينة", {"match_tashkeel": True}),
    ("مَدِينَةُ", {"match_tashkeel": True}),
    ("شطر", {"whole_word": True}),
]

SCHEMA_HARNESS = r"""
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const [, , root, fixturePath] = process.argv;
const fixture = JSON.parse(readFileSync(fixturePath, 'utf8'));
const convert = await import(pathToFileURL(`${root}/static/src/editor/convert.js`));
const schema = await import(pathToFileURL(`${root}/static/src/editor/schema.js`));
const core = await import('@tiptap/core');
const { EditorState, TextSelection } = await import('@tiptap/pm/state');
const out = {};
// ---- conversion: a chapter round-trips unchanged (the page-break flags too), the odd shapes are folded
const editorJson = convert.toEditor({ type: 'doc', content: fixture.nodes });
out.roundTrip = convert.fromEditor(editorJson);
out.editorTypes = editorJson.content.map((n) => n.type + (n.attrs.style ? ':' + n.attrs.style : n.attrs.level ? ':' + n.attrs.level : ''));
const odd = convert.toEditor([
  { type: 'blockquote', content: [{ type: 'paragraph', attrs: { id: 'q1' }, content: [{ type: 'text', text: 'اقتباس' }] }, { type: 'paragraph', attrs: { id: 'q2', style: 'verse' }, content: [{ type: 'text', text: 'بيت' }] }] },
  { type: 'horizontalRule', attrs: { id: 'r1' } },
  { type: 'heading', attrs: { level: 4, id: 'h4' }, content: [{ type: 'text', text: '' }, { type: 'text', text: 'عنوان', marks: [{ type: 'strike' }, { type: 'bold' }, { type: 'bold' }] }] },
  { type: 'title', attrs: { text: 'الكتاب', author: 'فلان' } },
  { type: 'paragraph', attrs: { id: 'p9' }, content: [{ type: 'footnote', attrs: { id: 'n9' }, content: [{ type: 'pageBreak', attrs: { page: 3 } }, { type: 'text', text: 'ملاحظة' }] }, { type: 'unknown' }] },
  { type: 'weird' },
]);
out.odd = odd.content.map((n) => [n.type, n.attrs.style || n.attrs.level || n.attrs.id, JSON.stringify(n.content)]);
out.empty = convert.toEditor([]).content.length;
out.emptySaved = convert.fromEditor(convert.toEditor([]));
const ids = new Set(Array.from({ length: 200 }, () => convert.newId('e')));
out.ids = { unique: ids.size, shape: [...ids].every((id) => /^ek[0-9a-z]+$/.test(id) && !/^e\d+$/.test(id)), note: /^nek/.test(convert.newId('ne')) };
// ---- find: the same matches as the server; by plain offsets too
out.matches = fixture.queries.map(([query, opts]) => convert.findMatches(fixture.nodes, query, { matchTashkeel: Boolean(opts.match_tashkeel), foldAlef: opts.fold_alef !== false, wholeWord: Boolean(opts.whole_word) }));
out.plainMatches = fixture.queries.map(([query, opts]) => convert.findPlain(fixture.nodes, query, { matchTashkeel: Boolean(opts.match_tashkeel), foldAlef: opts.fold_alef !== false, wholeWord: Boolean(opts.whole_word) }));
out.words = convert.wordCount(fixture.nodes);
out.fold = [convert.fold('مَدِينَةُ', {}).text, convert.fold('أإآ', {}).text, convert.fold('Aب', {}).text, convert.foldQuery('  ', {})];
out.format = [convert.formatCount(2184), convert.formatCount(999), convert.pageRange({ first: 31, last: 58 }), convert.pageRange({ first: 4, last: 4 }), convert.pageRange(null)];
out.styles = convert.STYLES.map((s) => s.key);
out.styleOf = [convert.styleOf({ type: 'heading', attrs: { level: 2 } }), convert.styleOf({ type: 'paragraph', attrs: { style: 'quote' } }), convert.styleOf({ type: 'separator' }), convert.styleOf({ type: 'paragraph', attrs: {} })];
out.stepIndex = [convert.stepIndex(1, 0, 3), convert.stepIndex(2, 1, 3), convert.stepIndex(0, -1, 3), convert.stepIndex(-1, 1, 3), convert.stepIndex(-1, -1, 3), convert.stepIndex(0, 1, 0)];
// ---- D47: plain text (= editor.document.inline_text), offsets in UTF-16
const p30 = fixture.nodes.find((n) => n.attrs && n.attrs.id === 'p30');
const p11 = fixture.nodes.find((n) => n.attrs && n.attrs.id === 'p11');
out.plain = fixture.nodes.map((n) => convert.plainText(n));
out.marks = convert.pageMarks(p30);
// Enter: the split halves (the page break stays with the first, «مع التالية» moves to the second), a heading
// cut at its end continues with a paragraph
const [a, b] = convert.splitNode({ ...p11, attrs: { ...p11.attrs, breakBefore: true, keepWithNext: true } }, 5);
out.split = { a, b: { ...b, attrs: { ...b.attrs, id: b.attrs.id.startsWith('ek') ? 'NEW' : b.attrs.id } }, plainA: convert.plainText(a), plainB: convert.plainText(b) };
const h = fixture.nodes.find((n) => n.type === 'heading');
const [ha, hb] = convert.splitNode(h, convert.plainText(h).length);
out.headingSplit = [ha.type, hb.type, hb.attrs.level === undefined, hb.content.length];
out.midHeading = convert.splitNode(h, 3).map((n) => [n.type, n.attrs.level]);
// Backspace at the start: the texts joined, the caret at the seam; nothing to join to a separator
out.merge = convert.mergeNodes(a, b);
out.mergeSeparator = convert.mergeNodes({ type: 'separator', attrs: {} }, b);
// D70: a merge keeps both blocks' source marks (union, sorted, unique) and is reviewed only when both were
const para = (id, attrs, text) => ({ type: 'paragraph', attrs: { id, ...attrs }, content: [{ type: 'text', text }] });
out.mergeMarks = [
  convert.mergeNodes(para('p1', { sourcePages: [4, 3], sourceLineIds: [31, 30], reviewed: true }, 'أ'), para('p2', { sourcePages: [4, 5], sourceLineIds: [40, 31, 41], reviewed: false }, 'ب')).node.attrs,
  convert.mergeNodes(para('p1', {}, 'أ'), para('p2', { sourcePages: [6], sourceLineIds: [60] }, 'ب')).node.attrs,
  convert.mergeNodes(para('p1', { sourcePages: [2], reviewed: true }, 'أ'), para('p2', {}, 'ب')).node.attrs,
];
// a paste of three paragraphs inside the text
const pasted = [{ type: 'paragraph', content: [{ type: 'text', text: 'أ' }] }, { type: 'heading', attrs: { level: 2 }, content: [{ type: 'text', text: 'ب' }] }, { type: 'paragraph', attrs: { style: 'quote' }, content: [{ type: 'text', text: 'ج' }] }];
const ins = convert.insertBlocks(p11, 3, 6, pasted);
out.paste = { types: ins.blocks.map((n) => n.type + (n.attrs.style ? ':' + n.attrs.style : '') + (n.attrs.level ? ':' + n.attrs.level : '')), plain: ins.blocks.map((n) => convert.plainText(n)), caret: ins.caret, firstId: ins.blocks[0].attrs.id };
// the chapter's nodes by block id: never mutated; a blockquote's paragraphs stay in it, others lift out
const frozen = JSON.stringify(fixture.nodes);
const replaced = convert.replaceBlock(fixture.nodes, 'p12', [a, b]);
out.replace = { ids: convert.flatBlocks(replaced).map((x) => x.id.startsWith('ek') ? 'NEW' : x.id), unchanged: JSON.stringify(fixture.nodes) === frozen, removed: convert.flatBlocks(convert.replaceBlock(fixture.nodes, 'p12', [])).length };
const bq = [{ type: 'blockquote', attrs: { id: 'bq' }, content: [{ type: 'paragraph', attrs: { id: 'q1' }, content: [] }, { type: 'paragraph', attrs: { id: 'q2' }, content: [] }, { type: 'paragraph', attrs: { id: 'q3' }, content: [] }] }];
out.quote = {
  kept: convert.replaceBlock(bq, 'q2', [{ type: 'paragraph', attrs: { id: 'q2', style: 'quote' }, content: [] }]),
  lifted: convert.replaceBlock(bq, 'q2', [{ type: 'paragraph', attrs: { id: 'q2' }, content: [] }]),
  emptied: convert.replaceBlock([{ type: 'blockquote', content: [{ type: 'paragraph', attrs: { id: 'q9' }, content: [] }] }], 'q9', []),
};
out.flags = convert.locate(convert.setBlockAttrs(fixture.nodes, 'p12', { breakBefore: true, keepWithNext: null }), 'p12').node.attrs;
out.note = [convert.noteOwner(fixture.nodes, 'n1'), convert.noteIds(p11), convert.findNote(fixture.nodes, 'zz')];
// find & replace by plain offsets, an uncertain word accepted (the mark leaves only that range)
out.replacePlain = convert.replacePlain(p30.content, 18, 24, 'مدينة');
out.unmark = convert.unmarkPlain(p30.content, 18, 21);
out.inNote = convert.replaceInBlock(p11, 'n1', 0, 5, 'ملاحظة').content[1];
out.html = [convert.nodeHtml(p30, { numberOf: () => '' }), convert.nodeHtml(p11, { numberOf: (id) => (id === 'n1' ? '3' : '') }), convert.nodeHtml(h), convert.nodeHtml({ type: 'paragraph', content: [{ type: 'text', text: '<b>' }] })];
// ---- the schemas: the chapter's and the one-block editor's (exactly one block); offsets ↔ positions
const s = core.getSchema(schema.extensions({}));
out.schema = { nodes: Object.keys(s.nodes), marks: Object.keys(s.marks), noteContent: s.nodes.footnote.spec.content, flags: Object.keys(s.nodes.paragraph.spec.attrs).filter((k) => ['breakBefore', 'keepWithNext'].includes(k)) };
const one = core.getSchema(schema.blockExtensions({}));
out.blockDoc = one.topNodeType.spec.content;
const pm = one.nodeFromJSON(convert.toEditor([p11]).content[0]);
out.positions = [0, 5, 23, 24, 25, 31].map((off) => [off, schema.posOfOffset(pm, off), schema.offsetOfPos(pm, schema.posOfOffset(pm, off))]);
out.insideNote = schema.offsetOfPos(pm, 26);
const pmDoc = s.nodeFromJSON(editorJson);
pmDoc.check();
out.docChildren = pmDoc.childCount;
// ---- the plugins on a state without a DOM
const plugins = [schema.uniqueIdsPlugin(), schema.footnoteNumbersPlugin(), schema.caretBlockPlugin(), schema.findPlugin(), schema.resolveOnTypePlugin()];
let state = EditorState.create({ doc: pmDoc, plugins });
const blockIds = (st) => { const o = []; st.doc.forEach((n) => o.push(n.attrs.id)); return o; };
const at11 = (() => { let pos = null; state.doc.forEach((n, offset) => { if (n.attrs.id === 'p11') pos = offset; }); return pos; })();
state = state.apply(state.tr.setSelection(TextSelection.create(state.doc, at11 + 5)).split(at11 + 5));
out.split2 = { unique: new Set(blockIds(state)).size === blockIds(state).length, second: state.doc.child(3).attrs.id.startsWith('ek'), kept: state.doc.child(2).attrs.id };
// a pasted copy of a block: the duplicate id is replaced
state = state.apply(state.tr.insert(state.doc.content.size, s.nodeFromJSON(state.doc.child(2).toJSON())));
out.dup = { unique: new Set(blockIds(state)).size === blockIds(state).length, last: blockIds(state).slice(-1)[0] };
// find highlights through the plugin meta (the one-block editor's), mapped through an edit before them
state = state.apply(state.tr.setMeta(schema.findKey, { ranges: [{ from: 20, to: 24, block: 'x', note: null }], current: 0 }));
const findState = (st) => schema.findKey.getState(st);
out.find1 = { n: findState(state).decos.find().length, cls: findState(state).decos.find()[0].type.attrs.class };
state = state.apply(state.tr.insertText('ab', 2));
out.find2 = findState(state).ranges[0];
const uncertain = (st) => { const o = []; st.doc.descendants((n) => { if (n.isText && n.marks.some((m) => m.type.name === 'uncertain')) o.push(n.text); }); return o; };
const posOf = (st, t) => { let at = null; st.doc.descendants((n, pos) => { if (n.isText && n.text === t) at = pos; }); return at; };
out.uncertainBefore = uncertain(state);
state = state.apply(state.tr.insertText('ك', posOf(state, 'مليتية') + 2));
out.uncertainAfterTyping = uncertain(state);
// the mark stays on a space typed right after the word, a large paste, a load
state = EditorState.create({ doc: pmDoc, plugins });
state = state.apply(state.tr.insertText(' ', posOf(state, 'مليتية') + 6));
out.uncertainAfterSpace = uncertain(state);
state = state.apply(state.tr.insertText('نص طويل جدًّا أُلصق هنا', posOf(state, 'مليتية') + 1).setMeta('uiEvent', 'paste'));
out.uncertainAfterPaste = uncertain(state).length;
state = EditorState.create({ doc: pmDoc, plugins });
state = state.apply(state.tr.insertText('x', posOf(state, 'مليتية') + 2).setMeta('ed:load', true));
out.uncertainAfterLoad = uncertain(state);
// the one-block editor's call numbers: the page's printed number, else the order
let bst = EditorState.create({ doc: one.nodeFromJSON({ type: 'doc', content: [convert.toEditor([p11]).content[0]] }), plugins: [schema.pageNumbersPlugin((id) => (id === 'n1' ? '7' : ''))] });
out.pageNumber = schema.numbersKey.getState(bst).find().map((d) => d.type.attrs['data-seq']);
bst = EditorState.create({ doc: bst.doc, plugins: [schema.pageNumbersPlugin(() => '')] });
out.orderNumber = schema.numbersKey.getState(bst).find().map((d) => d.type.attrs['data-seq']);
console.log(JSON.stringify(out));
"""  # noqa: E501


def test_schema_module_conversion_matching_and_block_helpers_under_node(tmp_path):
    folder = _node_tmp(tmp_path)
    nodes = _chapter_nodes()
    (folder / "fixture.json").write_text(json.dumps({"nodes": nodes, "queries": QUERIES}, ensure_ascii=False))
    out = _run_node(folder, "schema.mjs", SCHEMA_HARNESS, str(ROOT), str(folder / "fixture.json"))
    # a chapter round-trips as the server has it (the D47 flags too), and the server accepts it
    assert out["roundTrip"] == nodes
    assert doc.chapter_version(
        doc.clean_nodes({"type": "doc", "content": out["roundTrip"]})
    ) == doc.chapter_version(nodes)
    assert out["editorTypes"] == [
        "paragraph",
        "heading:1",
        "paragraph",
        "paragraph",
        "heading:1",
        "heading:2",
        "paragraph",
        "paragraph:verse",
        "separator",
    ]
    assert out["odd"][0][:2] == ["paragraph", "quote"] and out["odd"][1][:2] == ["paragraph", "verse"]
    assert out["odd"][2][:2] == ["separator", "r1"] and out["odd"][3][:2] == ["heading", 2]
    assert json.loads(out["odd"][3][2]) == [{"type": "text", "text": "عنوان", "marks": [{"type": "bold"}]}]
    assert out["odd"][4][0] == "title" and len(out["odd"]) == 6 and out["empty"] == 1
    doc.clean_nodes(out["emptySaved"])
    assert out["ids"] == {"unique": 200, "shape": True, "note": True}
    for (query, opts), js, plain in zip(QUERIES, out["matches"], out["plainMatches"], strict=True):
        options = doc.FindOptions(
            match_tashkeel=bool(opts.get("match_tashkeel")),
            fold_alef=opts.get("fold_alef", True),
            whole_word=bool(opts.get("whole_word")),
        )
        expected = [
            {"block": m.block, "note": m.note, "index": m.index, "length": m.length}
            for m in doc.find_in_nodes(nodes, query, options)
        ]
        assert js == expected, (query, opts)
        # by plain offsets: the same matches, each reading as the query in the container's inline_text
        assert len(plain) == len(expected), (query, opts)
        for m in plain:
            container = next(n for n in doc.flat_blocks(nodes) if doc.node_id(n) == m["block"])
            if m["note"]:
                container = next(
                    i
                    for i in container["content"]
                    if i.get("type") == "footnote" and doc.node_id(i) == m["note"]
                )
            found = doc.inline_text(container["content"])[m["start"] : m["end"]]
            assert doc.fold_query(found, options) == doc.fold_query(query, options), (query, found)
    assert out["words"] == doc.word_count(nodes)
    assert out["fold"] == ["مدينة", "ااا", "aب", ""]
    assert out["format"] == ["2\u202f184", "999", "31–58", "4", ""]
    assert out["styles"] == [
        "title",
        "heading1",
        "heading2",
        "paragraph",
        "quote",
        "verse",
        "center",
        "separator",
        "footnote",
    ]
    assert out["styleOf"] == ["heading2", "quote", "separator", "paragraph"]
    assert out["stepIndex"] == [1, 0, 2, 0, 2, -1]
    # plain text = editor.document.inline_text (a call or a page mark one U+FFFC, a break "\n")
    assert out["plain"] == [doc.inline_text(n.get("content") or []) for n in nodes]
    assert out["marks"] == [{"offset": 17, "page": 6, "printed": "6"}]
    split = out["split"]
    assert (
        split["plainA"] == doc.inline_text(nodes[2]["content"])[:5]
        and split["plainB"] == doc.inline_text(nodes[2]["content"])[5:]
    )
    assert split["a"]["attrs"] == {
        "id": "p11",
        "sourcePages": [2],
        "sourceLineIds": [],
        "reviewed": True,
        "breakBefore": True,
    }
    assert split["b"]["attrs"] == {
        "id": "NEW",
        "sourcePages": [2],
        "sourceLineIds": [],
        "reviewed": True,
        "keepWithNext": True,
    }
    assert split["b"]["content"][1]["type"] == "footnote"  # the call goes with the text after the caret
    assert out["headingSplit"] == ["heading", "paragraph", True, 0]
    assert out["midHeading"] == [["heading", 1], ["heading", 1]]
    assert out["merge"]["offset"] == 5 and doc.inline_text(
        out["merge"]["node"]["content"]
    ) == doc.inline_text(nodes[2]["content"])
    assert (
        out["merge"]["node"]["attrs"]["id"] == "p11" and out["merge"]["node"]["attrs"]["keepWithNext"] is True
    )
    assert out["mergeSeparator"] is None
    # D70: both paragraphs' source marks survive a merge
    assert out["mergeMarks"] == [
        {"id": "p1", "sourcePages": [3, 4, 5], "sourceLineIds": [30, 31, 40, 41], "reviewed": False},
        {"id": "p1", "sourcePages": [6], "sourceLineIds": [60]},
        {"id": "p1", "sourcePages": [2], "reviewed": True},
    ]
    paste = out["paste"]
    base = doc.inline_text(nodes[2]["content"])
    assert paste["types"] == ["paragraph", "heading:2", "paragraph:quote"] and paste["firstId"] == "p11"
    assert paste["plain"] == [base[:3] + "أ", "ب", "ج" + base[6:]] and paste["caret"] == {
        "index": 2,
        "offset": 1,
    }
    replace = out["replace"]
    assert (
        replace["ids"] == ["p1", "h10", "p11", "p11", "NEW", "h20", "h21", "p22", "p30", "s1"]
        and replace["unchanged"] is True
    )
    assert replace["removed"] == len(list(doc.flat_blocks(nodes))) - 1
    quote = out["quote"]
    assert [p["attrs"] for p in quote["kept"][0]["content"]] == [{"id": "q1"}, {"id": "q2"}, {"id": "q3"}]
    assert [n["type"] for n in quote["lifted"]] == ["blockquote", "paragraph", "blockquote"]
    assert (
        quote["lifted"][0]["attrs"] == {"id": "bq"}
        and "attrs" not in quote["lifted"][2]
        and quote["emptied"] == []
    )
    assert out["flags"]["breakBefore"] is True and "keepWithNext" not in out["flags"]
    assert out["note"] == ["p11", ["n1"], None]
    text_after = doc.inline_text(out["replacePlain"])
    assert (
        text_after
        == doc.inline_text(nodes[7]["content"])[:18] + "مدينة" + doc.inline_text(nodes[7]["content"])[24:]
    )
    assert not any(
        m.get("type") == "uncertain" for item in out["replacePlain"] for m in item.get("marks", [])
    )
    marked = [
        item for item in out["unmark"] if any(m.get("type") == "uncertain" for m in item.get("marks", []))
    ]
    assert [item["text"] for item in marked] == ["تية"]
    assert out["inNote"]["content"] == [{"type": "text", "text": "ملاحظة عن برقة"}]
    assert (
        out["html"][0]
        == '<p class="ed-p is-verse">شطر أول<br><i>شطر ثانٍ </i><span class="ed-pb" data-page="6"></span><mark class="ed-uncertain">مليتية</mark><b> وبرقة</b></p>'  # noqa: E501
    )
    assert '<sup class="ed-fn" data-seq="3"></sup>' in out["html"][1] and out["html"][2].startswith(
        '<h2 class="ed-h1">'
    )
    assert out["html"][3] == '<p class="ed-p">&lt;b&gt;</p>'
    # the schemas: the Phase 4 nodes one to one, the flags on every block; the one-block editor holds one
    # block
    assert out["schema"]["nodes"] == [
        "doc",
        "text",
        "paragraph",
        "heading",
        "title",
        "separator",
        "hardBreak",
        "pageBreak",
        "footnote",
    ]
    assert out["schema"]["marks"] == ["bold", "italic", "uncertain"] and out["schema"]["flags"] == [
        "breakBefore",
        "keepWithNext",
    ]
    assert out["blockDoc"] == "block"
    # p11: «مَدِينَةُ برقة القديمة » (23 units), the call (one offset, a node of 15), « وأهلها»
    assert out["positions"] == [[0, 1, 0], [5, 6, 5], [23, 24, 23], [24, 39, 24], [25, 40, 25], [31, 46, 31]]
    assert out["insideNote"] == 24
    assert out["docChildren"] == 9
    assert out["split2"] == {"unique": True, "second": True, "kept": "p11"}
    assert out["dup"]["unique"] is True and out["dup"]["last"].startswith("ek")
    assert out["find1"] == {"n": 1, "cls": "ed-match is-current"} and out["find2"]["from"] == 22
    assert out["uncertainBefore"] == ["مليتية"] and out["uncertainAfterTyping"] == []
    assert out["uncertainAfterSpace"] == ["مليتية"] and out["uncertainAfterPaste"] == 1
    assert out["uncertainAfterLoad"] == ["ملxيتية"]
    assert out["pageNumber"] == ["7"] and out["orderNumber"] == ["1"]


# ---------------------------------------------------------------- the live pages' geometry under Node


def _real_layout(book) -> dict:
    """The sample document rendered by WeasyPrint (test database): the live layout's pages."""
    from publishing import preview, relayout

    preview.render_preview(book, "book")
    return relayout.layout_payload(book, first=1, last=40)


def _line(
    block,
    kind,
    y,
    start,
    end,
    text,
    *,
    x=62.36,
    w=368.5,
    h=22.1,
    justify=True,
    first=False,
    style="body",
    runs=None,
):
    return {
        "block": block,
        "kind": kind,
        "style": style,
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "baseline": y + 14.2,
        "dir": "rtl",
        "justify": justify,
        "start": start,
        "end": end,
        "first": first,
        "runs": runs
        or [
            {
                "text": text,
                "font": "nk-body",
                "size_pt": 13.0,
                "weight": 400,
                "italic": False,
                "sup": False,
                "note": None,
                "start": start,
                "end": end,
            }
        ],
    }


def _page_of(n, lines, *, side=None, number=True, rule=None, chapter="h10"):
    side = side or ("left" if n % 2 else "right")
    margins = (
        {"top": 56.69, "right": 62.36, "bottom": 62.36, "left": 51.02}
        if side == "left"
        else {"top": 56.69, "right": 51.02, "bottom": 62.36, "left": 62.36}
    )
    return {
        "n": n,
        "side": side,
        "blank": False,
        "width_pt": 481.89,
        "height_pt": 680.31,
        "margins": margins,
        "header": None,
        "number": {
            "text": str(n),
            "x": 238.3,
            "y": 629.29,
            "w": 5.32,
            "h": 17.58,
            "baseline": 640.23,
            "font": "nk-body",
            "size_pt": 10.0,
            "weight": 400,
            "italic": False,
            "align": "center",
        }
        if number
        else None,
        "footnote_rule": rule,
        "chapter": chapter,
        "lines": lines,
    }


GEOMETRY_HARNESS = r"""
const fs = require('fs');
const fixture = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
globalThis.window = globalThis;
eval(fs.readFileSync(process.argv[5], 'utf8')); // keys.js (NassakhKeys, D69)
eval(fs.readFileSync(process.argv[2], 'utf8'));
eval(fs.readFileSync(process.argv[3], 'utf8'));
const G = globalThis.NassakhBook.geo;
const F = globalThis.NassakhBook.editFlow;
const out = {};
// ---- a real page drawn: every line placed at its box, the call a superscript run, the note, the rule, the number
const real = fixture.real.pages.find((p) => p.lines.some((l) => l.block === 'p11'));
out.realHtml = G.pageHtml(real, {});
out.realStyle = G.pageStyle(real);
out.real = real;
// ---- a justified line, runs with faces and weights, a leader, a header anchored right
const crafted = { n: 7, width_pt: 481.89, height_pt: 680.31, margins: {}, header: { text: 'الفصل الأول', x: 300, y: 30, w: 80, h: 15, font: 'nk-heading', size_pt: 9.5, weight: 700, align: 'right' }, number: null, footnote_rule: null,
  lines: [
    { block: 'p1', kind: 'body', style: 'body', x: 62.36, y: 56.69, w: 368.5, h: 22.1, dir: 'rtl', justify: true, start: 0, end: 20, first: true, runs: [
      { text: 'نص ', font: 'nk-body', size_pt: 13, weight: 400, italic: false, sup: false, note: null, start: 0, end: 3 },
      { text: 'غامق', font: 'nk-body', size_pt: 13, weight: 700, italic: false, sup: false, note: null, start: 3, end: 7 },
      { text: 'Latin', font: 'nk-latin', size_pt: 13, weight: 400, italic: true, sup: false, note: null, start: 7, end: 12 },
      { text: 'Other', font: 'Gill Sans', size_pt: 11, weight: 400, italic: false, sup: false, note: null, start: 12, end: 17 } ] },
    { block: 'toc-1', kind: 'contents', style: 'contents-1', target: 'h10', x: 62.36, y: 100, w: 368.5, h: 22.1, dir: 'rtl', justify: false, start: 0, end: 5, first: true, runs: [
      { text: 'الفصل', font: 'nk-body', size_pt: 13, weight: 400, italic: false, sup: false, note: null, start: 0, end: 5 },
      { text: '.', font: 'nk-body', size_pt: 13, weight: 400, italic: false, sup: false, note: null, start: 5, end: 5, leader: true, w: 282.11 },
      { text: '4', font: 'nk-body', size_pt: 13, weight: 400, italic: false, sup: false, note: null, start: 5, end: 5 } ] } ] };
out.craftedHtml = G.pageHtml(crafted, { hidden: new Set(['nothing']), selected: 'toc-1', flash: { block: 'p1', i: 0 } });
// ---- a click: the run's text ↔ the block's plain text through collapsed spaces and unprinted page marks
const plain = 'أهل  برقة￼ يزرعون القمح';
out.runMap = G.runMap('أهل برقة يزرعون', plain, 0, 17);
out.runMapBare = G.runMap('أهل برقة', undefined, 4, 12);
const hitLine = { block: 'p1', start: 0, end: 22, runs: [{ text: 'أهل برقة يزرعون', start: 0, end: 17 }, { text: '(1)', sup: true, note: 'n1', start: 17, end: 18 }, { text: ' القمح', start: 18, end: 22 }] };
out.hits = [G.hitOffset(hitLine, 0, 0, plain), G.hitOffset(hitLine, 0, 4, plain), G.hitOffset(hitLine, 0, 9, plain), G.hitOffset(hitLine, 1, 1, plain), G.hitOffset(hitLine, 2, 3, undefined), G.hitOffset(hitLine, 9, 0, plain)];
// ---- decorations cut a run (find, the uncertain word, the picked word); page marks inserted with no width
out.pieces = G.runPieces({ text: 'أهل برقة يزرعون', start: 0, end: 17 }, plain, [{ start: 5, end: 9, cls: 'lp-match is-current' }, { start: 11, end: 17, cls: 'lp-uncertain' }]);
out.marked = G.lineHtml({ block: 'p1', kind: 'body', x: 1, y: 2, w: 3, h: 4, dir: 'rtl', justify: false, start: 0, end: 17, runs: [{ text: 'أهل برقة يزرعون', font: 'nk-body', size_pt: 13, start: 0, end: 17 }] }, 0,
  { plain: () => plain, marks: () => [{ offset: 9, page: 6 }], decos: () => [{ start: 0, end: 3, cls: 'lp-picked' }] });
// ---- lines by offset, across pages
const pages = new Map(fixture.pages.map((p) => [p.n, p]));
out.lineAt = [G.lineAt(pages.get(2), 'p13', 0), G.lineAt(pages.get(2), 'p13', 61), G.lineAt(pages.get(2), 'p13', 60), G.lineAt(pages.get(2), 'zz', 0)];
out.caretLine = [G.caretLine(pages, 'p13', 10), G.caretLine(pages, 'p13', 130), G.caretLine(pages, 'p13', 175), G.caretLine(pages, 'n12', 3), G.caretLine(pages, 'nope', 0)];
out.blocksOn = G.blocksOn(pages.get(2));
out.bodyBottom = [G.bodyBottom(pages.get(2)), G.bodyBottom(pages.get(3))];
out.measure = [G.measure(pages.get(2)), G.measure(pages.get(3))];
// ---- an open paragraph: its lines, the lines below it; a new block under the one before it
const around = G.flowAround(pages.get(2), 'p12');
out.around = { lines: around.lines, top: around.top, bottom: around.bottom, below: pages.get(2).lines.map((l, i) => around.below(l, i)) };
const after = G.flowAfter(pages.get(2), 'p12');
out.after = { top: after.top, below: pages.get(2).lines.map((l, i) => after.below(l, i)) };
// the flow with the open paragraph one line taller (22.1): the lines below move; the last one falls off the body
const f = F.flow(pages.get(2), [{ key: 'p12', open: true, oldTop: around.top, oldBottom: around.bottom, anchorTop: around.top, height: around.bottom - around.top + 22.1 }], G.bodyBottom(pages.get(2)));
out.flow = { regions: f.regions.map((r) => [r.key, r.screenTop, Math.round(r.after * 100) / 100]), lines: f.lines };
// a removed block closes its room, a new one opens under the block before it
const f2 = F.flow(pages.get(2), [{ key: 'p11', oldTop: 155.64, oldBottom: 199.84, height: 0 }, { key: 'new', oldTop: 199.85, oldBottom: 199.85, height: 22.1 }], 600);
out.flow2 = f2.lines.map((l) => Math.round(l.dy * 100) / 100);
// ---- the overlay's placement (viewport coordinates): below the anchor, above near the bottom, never across an edge
const view = { top: 100, bottom: 800, left: 300, right: 1200 };
out.place = [F.placeAgainst({ top: 200, bottom: 220, left: 700, right: 900 }, view, [380, 132], true), F.placeAgainst({ top: 740, bottom: 760, left: 700, right: 900 }, view, [380, 132], true),
  F.placeAgainst({ top: 200, bottom: 220, left: 300, right: 400 }, view, [380, 132], true), F.placeAgainst({ top: 200, bottom: 220, left: 1100, right: 1190 }, view, [380, 132], false)];
// ---- a page moved by a re-layout: renumbered; an odd delta swaps its side (the text moves by the side shift,
// an outer number goes to the other corner, a centred one moves with the text)
const p3 = pages.get(3);
out.shiftEven = G.shiftPage(p3, 2, { side_shift_pt: -11.34, page_number: 'bottom_center' });
out.shiftOdd = G.shiftPage(p3, 1, { side_shift_pt: -11.34, page_number: 'bottom_center' });
const outer = Object.assign({}, p3, { number: Object.assign({}, p3.number, { x: 51.02, align: 'left' }) });
out.shiftOuter = G.shiftPage(outer, -1, { side_shift_pt: -11.34, page_number: 'bottom_outer' }).number;
out.noShift = G.shiftPage(p3, 0, {}) === p3;
// ---- a re-layout spliced in: pages 2–3 of the chapter become three pages (delta +1, flip); later pages move
const held = { pages: new Map(fixture.pages.map((p) => [p.n, p])), pageCount: 5, revision: 3 };
const fresh = [2, 3, 4].map((n) => Object.assign({}, fixture.pages.find((p) => p.n === 2), { n, lines: [], fresh: true }));
const result = { mode: 'chapter', full: false, from: 2, to: 3, count: 3, delta: 1, shifted_from: 4, flip: true, side_shift_pt: -11.34, page_count: 6, chapters: [{ id: 'h10', first: 2, last: 4 }, { id: 'h20', first: 5, last: 6 }], checks: [{ code: 'almost_empty_page', page: 4 }], revision: { before: 3, after: 4 } };
const applied = G.applyRelayout(held, result, fresh, { page_number: 'bottom_center' });
out.applied = { numbers: [...applied.pages.keys()].sort((a, b) => a - b), fresh: [2, 3, 4].map((n) => Boolean(applied.pages.get(n).fresh)), moved: [1, 2, 3, 4, 5].map((n) => applied.moved(n)),
  p5: { side: applied.pages.get(5).side, number: applied.pages.get(5).number.text, x0: applied.pages.get(5).lines[0].x, margins: applied.pages.get(5).margins }, pageCount: applied.pageCount, revision: applied.revision, delta: applied.delta, flip: applied.flip, chapters: applied.chapters, checks: applied.checks };
out.contentsDropped = G.applyRelayout({ pages: new Map([[1, { n: 1, lines: [{ kind: 'contents', block: 'toc-1' }] }], [5, fixture.pages[3]]]), pageCount: 5, revision: 3 }, result, [], {}).pages.has(1);
out.refetch = [G.applyRelayout(held, Object.assign({}, result, { revision: { before: 2, after: 4 } }), fresh, {}), G.applyRelayout(held, Object.assign({}, result, { full: true }), fresh, {}), G.applyRelayout(held, { unchanged: true }, [], {})];
// ---- which pages: fetched around the page shown; drawn: one page, or the two of a spread (recto on the left)
out.around2 = [G.around(1, 1, 400, false), G.around(200, 1, 400, true), G.around(399, 1, 400, false), G.around(3, 1, 2, false)];
const seq = [1, 2, 3, 4, 5, 6, 7];
out.shown = [G.shown(seq, 0, false), G.shown(seq, 0, true), G.shown(seq, 1, true), G.shown(seq, 2, true), G.shown(seq, 6, true), G.shown(seq, 9, true)];
// ---- the keyboard maps (nothing fires inside a field; the open paragraph keeps its own keys)
const k = (key, extra = {}, ctx = {}) => G.keyAction(Object.assign({ key, code: extra.code || '' }, extra), Object.assign({ mode: 'preview' }, ctx));
out.preview = [k('ArrowLeft'), k('ArrowRight'), k('PageDown'), k('PageUp'), k('Home'), k('End'), k('g', { code: 'KeyG' }), k('s', { code: 'KeyS' }), k('e', { code: 'KeyE' }), k('ث', { code: 'KeyE' }), k('1'), k('2'), k('3'), k('?'), k('؟'), k('f', { code: 'KeyF', metaKey: true }), k('z', { code: 'KeyZ', metaKey: true }), k('o', { code: 'KeyO' }), k('Escape')];
// D69: V is the spread, S the scan page marks, + − 0 the fits (0 the height, + up to the width then 100 %, − back);
// by the key's place on the Arabic layout too; nothing while an IME composes
out.moved = [k('v', { code: 'KeyV' }), k('ر', { code: 'KeyV' }), k('س', { code: 'KeyS' }), k('+', { code: 'Equal', shiftKey: true }), k('=', { code: 'Equal' }), k('-', { code: 'Minus' }), k('+', { code: 'NumpadAdd' }),
  k('0', { code: 'Digit0' }), k('٠', { code: 'Digit0' }), k('v', { code: 'KeyV', shiftKey: true }), k('v', { code: 'KeyV', isComposing: true }), k('Process', { code: 'KeyV', keyCode: 229 })];
out.fitSteps = [G.stepFit('height', 1), G.stepFit('width', 1), G.stepFit('actual', 1), G.stepFit('actual', -1), G.stepFit('width', -1), G.stepFit('height', -1), G.stepFit('nonsense', 1)];
const e = (key, extra = {}, ctx = {}) => k(key, extra, Object.assign({ mode: 'edit' }, ctx));
out.edit = [e('s', { code: 'KeyS', metaKey: true }), e('z', { code: 'KeyZ', metaKey: true }), e('z', { code: 'KeyZ', metaKey: true, shiftKey: true }), e('y', { code: 'KeyY', ctrlKey: true }), e('f', { code: 'KeyF', metaKey: true }), e('f', { code: 'KeyF', metaKey: true, shiftKey: true }),
  e('b', { code: 'KeyB', metaKey: true }), e('i', { code: 'KeyI', metaKey: true }), e('1', { code: 'Digit1', metaKey: true, altKey: true }), e('0', { code: 'Digit0', metaKey: true, altKey: true }), e('6', { code: 'Digit6', metaKey: true, altKey: true }),
  e('[', { code: 'BracketLeft', metaKey: true }), e(']', { code: 'BracketRight', metaKey: true }), e('o', { code: 'KeyO' }), e('e', { code: 'KeyE' }), e('ArrowLeft'), e('Delete'), e('g', { code: 'KeyG' }), e('s', { code: 'KeyS' }), e('Escape')];
out.fields = [e('o', { code: 'KeyO' }, { inField: true }), e('Escape', {}, { inField: true }), e('s', { code: 'KeyS', metaKey: true }, { inField: true }), e('z', { code: 'KeyZ', metaKey: true }, { inField: true }), e('e', { code: 'KeyE' }, { inBlock: true }), e('ArrowLeft', {}, { inBlock: true }), k('ArrowLeft', {}, { inField: true }), e('1', { code: 'Digit1', metaKey: true, altKey: true }, { inField: true })];
// ---- the side panel's lists
out.snippet = [G.snippet('قال الراوي إن أهل برقة كانوا يزرعون القمح في السهول الواسعة', 18, 22, 10), G.snippet('قصير', 0, 4)];
out.groups = G.groupUncertain([{ chapter: 'h10', block: 'p11', start: 1, page: 4 }, { chapter: 'h10', block: 'p12', start: 1, page: 3 }, { chapter: 'h10', block: 'p13', start: 1, page: null }, { chapter: 'h20', block: 'p21', start: 1, page: 9 }, { chapter: 'h10', block: 'p11', start: 9, page: 4 }], { h10: 'الفصل الأول' });
out.rows = G.chapterRowsHtml([{ id: 'h1', kind: 'chapter', title: 'الفصل <الأول>', first: 3, last: 9, pages: 7, delta: 2, drift: true, current: true }, { id: 'p0', kind: 'front', title: 'قبل', first: null, last: null, pages: null, delta: 0 }, { id: 'h2', kind: 'chapter', title: 'ثان', first: 10, last: 10, pages: 1, delta: -1 }]);
out.count = [G.arCount(1, ['صفحة واحدة', 'صفحتان', 'صفحات', 'صفحة']), G.arCount(2, ['صفحة واحدة', 'صفحتان', 'صفحات', 'صفحة']), G.arCount(7, ['صفحة واحدة', 'صفحتان', 'صفحات', 'صفحة']), G.arCount(412, ['صفحة واحدة', 'صفحتان', 'صفحات', 'صفحة'])];
console.log(JSON.stringify(out));
"""  # noqa: E501


WORDS = "أهل برقة يزرعون القمح في السهول الواسعة حول المدينة القديمة منذ عصور بعيدة "


def _words(n: int, shift: int = 0) -> str:
    """Arabic text of exactly `n` characters (words and single spaces)."""
    base = WORDS * 20
    return base[shift : shift + n]


def _h10_texts() -> dict:
    return {
        "h10": "الفصل الأول",
        "p11": _words(80),
        "p12a": _words(30, 3),
        "p12b": _words(29, 7),
        "p13": _words(180, 11),
        "p14": _words(30, 13),
        "n12": "حاشية عن برقة",
    }


def _h10_nodes() -> list:
    t = _h10_texts()
    return [
        heading("h10", t["h10"], pages=(2,)),
        para("p11", t["p11"], pages=(2,)),
        para("p12", t["p12a"], note("n12", t["n12"], page=2), t["p12b"], pages=(2, 3)),
        para("p13", t["p13"], pages=(3,)),
        para("p14", t["p14"], pages=(3,)),
    ]


def _run(value, start, end, **extra):
    return {
        "text": value,
        "font": "nk-body",
        "size_pt": 13.0,
        "weight": 400,
        "italic": False,
        "sup": False,
        "note": None,
        "start": start,
        "end": end,
        **extra,
    }


def _chapter_pages() -> list[dict]:
    """Chapter h10 on pages 2–3 (p13 runs over), h20 on 4–5: every line's runs are its block's text between
    its offsets (the call is its own superscript run)."""
    t = _h10_texts()
    p12 = t["p12a"] + doc.OBJECT + t["p12b"]
    rule = {"x": 62.36, "y": 560.0, "w": 368.5}
    call = _run("(1)", 30, 31, size_pt=6.2, sup=True, note="n12")

    def body(block, text, y, a, b, **k):
        return _line(block, "body", y, a, b, text[a:b].strip(), runs=[_run(text[a:b].strip(), a, b)], **k)

    page2 = _page_of(
        2,
        [
            _line(
                "h10",
                "heading",
                102.05,
                0,
                11,
                t["h10"],
                x=201.92,
                w=89.38,
                h=28.08,
                justify=False,
                first=True,
                style="chapter-title",
                runs=[_run(t["h10"], 0, 11, font="nk-heading", size_pt=20.8, weight=700)],
            ),
            body("p11", t["p11"], 155.64, 0, 40, first=True),
            body("p11", t["p11"], 177.74, 41, 80, justify=False),
            _line(
                "p12",
                "body",
                199.84,
                0,
                35,
                "",
                first=True,
                runs=[_run(p12[0:30], 0, 30), call, _run(p12[31:35], 31, 35)],
            ),
            body("p12", p12, 221.94, 36, 60, justify=False),
            body("p13", t["p13"], 244.04, 0, 60, first=True),
            body("p13", t["p13"], 266.14, 61, 120),
            body("p13", t["p13"], 530.0, 121, 150),
            _line(
                "n12",
                "note",
                565.0,
                0,
                13,
                t["n12"],
                h=15.0,
                justify=False,
                style="footnote-text",
                runs=[_run("(1)\u00a0", 0, 0, size_pt=10.0, note="n12"), _run(t["n12"], 0, 13, size_pt=10.0)],
            ),
        ],
        rule=rule,
    )
    page3 = _page_of(
        3,
        [
            body("p13", t["p13"], 56.69, 151, 180, justify=False),
            body("p14", t["p14"], 78.79, 0, 30, first=True, justify=False),
        ],
    )
    page4 = _page_of(
        4,
        [
            _line(
                "h20",
                "heading",
                102.05,
                0,
                12,
                "الفصل الثاني",
                justify=False,
                first=True,
                style="chapter-title",
            ),
            _line("p21", "body", 155.64, 0, 20, "…", first=True, justify=False),
        ],
        chapter="h20",
    )
    page5 = _page_of(5, [_line("p22", "body", 56.69, 0, 10, "…", first=True, justify=False)], chapter="h20")
    return [page2, page3, page4, page5]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_live_page_geometry_under_node(tmp_path):
    book = _book()
    fixture = {"real": _real_layout(book), "pages": _chapter_pages()}
    (tmp_path / "fixture.json").write_text(json.dumps(fixture, ensure_ascii=False))
    out = _run_node(
        tmp_path,
        "geometry.js",
        GEOMETRY_HARNESS,
        str(BOOK_JS / "geometry.js"),
        str(BOOK_JS / "edit.js"),
        str(tmp_path / "fixture.json"),
        str(JS / "keys.js"),
    )

    # --- a real page: one element per line at its box, in points (the CSS scales them with the page)
    real, html = out["real"], out["realHtml"]
    assert out["realStyle"] == f"--pw:{real['width_pt']:g};--ph:{real['height_pt']:g}"
    drawn = re.findall(
        r'<div class="(lp-line [^"]*)" data-i="(\d+)" data-b="([^"]*)" dir="rtl" style="([^"]*)">', html
    )
    assert [int(i) for _c, i, _b, _s in drawn] == list(range(len(real["lines"])))
    for (cls, _i, block, style), line in zip(drawn, real["lines"], strict=True):
        assert block == line["block"]
        box = [f"--{k}:{round(line[k], 2):g}" for k in ("x", "y", "w", "h")]
        want = ";".join(box)
        assert style.startswith(want), (style, want)
        assert f"k-{line['kind']}" in cls and ("is-j" in cls) == line["justify"]
    heading_cls = next(c for c, _i, b, _s in drawn if b == "h10")
    assert "is-c" in heading_cls and "f-h" in heading_cls
    # the call: a superscript run of the note (0.62 of the 13pt body), its one position; the note at the
    # foot; the rule; the number
    assert re.search(
        r'<span class="lp-run f-b is-sup" data-r="1" data-s="23" data-e="24" data-note="n1" style="--fs:8.06">\(1\)</span>',  # noqa: E501
        html,
    )
    note_cls = next(c for c, _i, b, _s in drawn if b == "n1")
    assert "k-note" in note_cls
    rule = real["footnote_rule"]
    assert (
        f'<div class="lp-rule" style="--x:{rule["x"]:g};--y:{rule["y"]:g};--w:{rule["w"]:g}"></div>' in html
    )
    number = real["number"]
    assert (
        f'<div class="lp-number f-b" data-align="center" style="--x:{number["x"]:g};--y:{number["y"]:g};--w:{number["w"]:g};--h:{number["h"]:g};--fs:10"><span>{number["text"]}</span></div>'  # noqa: E501
        in html
    )

    # --- a justified line with runs in their faces, weights and styles; a contents line with its leader
    crafted = out["craftedHtml"]
    assert 'class="lp-line k-body is-j f-b is-flash"' in crafted
    assert (
        '<span class="lp-run f-b is-b" data-r="1" data-s="3" data-e="7" style="--fs:13">غامق</span>'
        in crafted
    )
    assert (
        '<span class="lp-run f-l is-i" data-r="2" data-s="7" data-e="12" style="--fs:13">Latin</span>'
        in crafted
    )
    assert (
        'style="--fs:11;font-family:&quot;Gill Sans&quot;,serif"' in crafted
        or 'font-family:"Gill Sans",serif' in crafted
    )
    assert (
        'class="lp-line k-contents is-link is-front f-b is-selected" data-i="1" data-b="toc-1" data-target="h10"'  # noqa: E501
        in crafted
    )
    assert '<span class="lp-leader f-b" data-r="1" style="--fs:13;--lw:282.11" aria-hidden="true">' in crafted
    assert (
        '<div class="lp-header f-h is-b" data-align="right"' in crafted
        and "<span>الفصل الأول</span>" in crafted
    )

    # --- clicks: the run's characters mapped through the doubled space and the unprinted page mark
    assert (
        out["runMap"][:5] == [0, 1, 2, 3, 5] and out["runMap"][7:10] == [8, 9, 11] and out["runMap"][-1] == 17
    )
    assert out["runMapBare"] == [4, 5, 6, 7, 8, 9, 10, 11, 12]
    assert out["hits"] == [0, 5, 11, 18, 21, 0]
    # decorations: the current match and the uncertain word cut the run; a page mark sits between letters
    assert out["pieces"] == [
        {"text": "أهل ", "cls": "", "before": ""},
        {"text": "برقة", "cls": "lp-match is-current", "before": ""},
        {"text": " ", "cls": "", "before": ""},
        {"text": "يزرعون", "cls": "lp-uncertain", "before": ""},
    ]
    assert (
        '<mark class="lp-picked">أهل</mark>' in out["marked"]
        and '<span class="lp-pb" data-page="6"></span>' in out["marked"]
    )
    assert out["marked"].index("برقة") < out["marked"].index('class="lp-pb"') < out["marked"].index("يزرعون")

    # --- lines by offset (the end of a line holds the caret), across pages; the page's blocks; the body's end
    assert out["lineAt"] == [5, 6, 5, -1]
    assert out["caretLine"] == [{"n": 2, "i": 5}, {"n": 2, "i": 7}, {"n": 3, "i": 0}, {"n": 2, "i": 8}, None]
    assert out["blocksOn"] == ["h10", "p11", "p12", "p13", "n12"]
    assert out["bodyBottom"] == [560.0, 680.31 - 62.36]
    assert out["measure"] == [
        {"x": 62.36, "w": pytest.approx(368.51)},
        {"x": 51.02, "w": pytest.approx(368.51)},
    ]
    # --- the open paragraph: its lines, the body lines under it move (not the note); a new block under p12
    around = out["around"]
    assert around["lines"] == [3, 4] and around["top"] == 199.84 and around["bottom"] == pytest.approx(244.04)
    assert around["below"] == [False, False, False, False, False, True, True, True, False]
    assert out["after"]["top"] == pytest.approx(244.04) and out["after"]["below"] == around["below"]
    flow = out["flow"]
    assert flow["regions"] == [["p12", 199.84, 22.1]]
    assert [round(line["dy"], 2) for line in flow["lines"]] == [0, 0, 0, 0, 0, 22.1, 22.1, 22.1, 0]
    assert [line["over"] for line in flow["lines"]] == [False] * 7 + [True, False]  # 530 + 22.1 + 22.1 > 560
    assert out["flow2"] == [0, 0, 0, -22.1, -22.1, -22.1, -22.1, -22.1, 0]
    # --- the overlay: below the anchor with its start (right) edge on the anchor's; flipped above at the
    # bottom;
    # kept inside the side edges
    assert out["place"][0] == {"style": "top:226px;left:520px", "above": False}
    assert out["place"][1] == {"style": "top:602px;left:520px", "above": True}
    assert out["place"][2] == {"style": "top:226px;left:300px", "above": False}
    assert out["place"][3] == {"style": "top:226px;left:820px", "above": False}
    # --- renumbered pages: an even delta keeps the side; an odd one swaps it and moves the text
    p3 = fixture["pages"][1]
    assert (
        out["shiftEven"]["n"] == 5
        and out["shiftEven"]["side"] == "left"
        and out["shiftEven"]["number"]["text"] == "5"
    )
    assert out["shiftEven"]["lines"][0]["x"] == p3["lines"][0]["x"]
    odd = out["shiftOdd"]
    assert odd["n"] == 4 and odd["side"] == "right" and odd["number"]["text"] == "4"
    assert odd["lines"][0]["x"] == pytest.approx(p3["lines"][0]["x"] + 11.34)  # left → right: −side_shift
    assert odd["margins"] == {"top": 56.69, "right": 51.02, "bottom": 62.36, "left": 62.36}
    assert odd["number"]["x"] == pytest.approx(
        p3["number"]["x"] + 11.34
    )  # a centred number moves with the text
    assert (
        out["shiftOuter"]["x"] == pytest.approx(481.89 - 51.02 - 5.32)
        and out["shiftOuter"]["align"] == "right"
        and out["shiftOuter"]["text"] == "2"
    )
    assert out["noShift"] is True
    # --- a re-layout spliced in: 2–3 replaced by three pages; 4–5 become 5–6 on the other side
    applied = out["applied"]
    assert applied["numbers"] == [2, 3, 4, 5, 6] and applied["fresh"] == [True, True, True]
    assert applied["moved"] == [1, None, None, 5, 6]
    # the old page 4 (a right-hand page) is page 5 now, a left-hand one: its text moves by the side shift
    assert applied["p5"]["side"] == "left" and applied["p5"]["number"] == "5"
    assert applied["p5"]["x0"] == pytest.approx(62.36 - 11.34) and applied["p5"]["margins"]["left"] == 51.02
    assert (
        applied["pageCount"] == 6
        and applied["revision"] == 4
        and applied["delta"] == 1
        and applied["flip"] is True
    )
    assert applied["chapters"][1] == {"id": "h20", "first": 5, "last": 6} and applied["checks"] == [
        {"code": "almost_empty_page", "page": 4}
    ]
    assert out["contentsDropped"] is False  # a contents page's numbers may have moved: fetched again
    assert out["refetch"] == [{"refetch": True}, {"refetch": True}, {"unchanged": True}]
    # --- the pages worth fetching around the one shown; the pages drawn (one, or a spread: even right, odd
    # left)
    assert out["around2"] == [[1, 6], [197, 207], [397, 400], [1, 2]]
    assert out["shown"] == [
        {"right": 1, "left": 0},
        {"right": 0, "left": 1},
        {"right": 2, "left": 3},
        {"right": 2, "left": 3},
        {"right": 6, "left": 7},
        {"right": 0, "left": 0},
    ]
    # --- the keyboard: preview turns (RTL: ← is the next page), jumps, spreads, fits, toggles the mode
    assert out["preview"] == [
        "next",
        "prev",
        "next",
        "prev",
        "first",
        "last",
        "jump",
        "marks",  # D69: S is «فواصل الصفحات الأصلية» (the spread moved to V)
        "mode",
        "mode",
        None,  # 1 / 2 / 3 are free (the fits moved to 0 + −)
        None,
        None,
        "sheet",
        "sheet",
        "find",
        None,
        None,
        "escape",
    ]
    assert out["moved"] == [
        "spread",
        "spread",
        "marks",
        "fitIn",
        "fitIn",
        "fitOut",
        "fitIn",
        "fitHeight",
        "fitHeight",
        None,  # ⇧V is not V
        None,  # composing
        None,
    ]
    assert out["fitSteps"] == ["width", "actual", "actual", "width", "height", "height", "width"]
    assert out["edit"] == [
        "save",
        "undo",
        "redo",
        "redo",
        "find",
        "footnote",
        "bold",
        "italic",
        "heading1",
        "paragraph",
        "separator",
        "prevChapter",
        "nextChapter",
        "source",
        "mode",
        "next",
        "deleteSelected",
        None,
        "marks",  # S in edit mode too
        "escape",
    ]
    # inside a field or the open paragraph nothing fires but Esc and ⌘S / ⌘F
    assert out["fields"] == [None, "blur", "save", None, None, None, None, None]
    # --- the lists
    assert out["snippet"][0] == {"before": "… إن أهل ", "match": "برقة", "after": " كانوا …"}
    assert out["snippet"][1] == {"before": "", "match": "قصير", "after": ""}
    groups = out["groups"]
    assert [(g["chapter"], g["title"], g["count"]) for g in groups] == [
        ("h10", "الفصل الأول", 4),
        ("h20", "", 1),
    ]
    assert [(p["page"], [i["block"] for i in p["items"]]) for p in groups[0]["pages"]] == [
        (3, ["p12"]),
        (4, ["p11", "p11"]),
        (None, ["p13"]),
    ]
    rows = out["rows"]
    assert 'class="bp-ch is-current" data-cid="h1" aria-current="true" title="الفصل &lt;الأول&gt;"' in rows
    assert (
        '<bdi class="bp-ch-count">7</bdi><bdi class="lo-delta">+2</bdi><span class="bp-ch-range">ص <bdi>3–9</bdi></span>'  # noqa: E501
        in rows
    )
    assert '<span class="bp-ch-drift">تغيّر في المراجعة</span>' in rows and 'class="bp-ch is-front"' in rows
    assert (
        '<bdi class="lo-delta">−1</bdi><span class="bp-ch-range">ص <bdi>10</bdi></span>' in rows
        and "is-none" in rows
    )
    assert out["count"] == ["صفحة واحدة", "صفحتان", "7 صفحات", "412 صفحة"]


# ---------------------------------------------------------------- the bookPage component under Node

COMPONENT_HARNESS = r"""
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const [, , root, fixturePath] = process.argv;
const fixture = JSON.parse(readFileSync(fixturePath, 'utf8'));
const convert = await import(pathToFileURL(`${root}/static/src/editor/convert.js`));
const clone = (v) => JSON.parse(JSON.stringify(v));

// ---------------------------------------------------------------- a tiny DOM
class ClassList { constructor(el) { this.s = new Set(); this.el = el; } add(...c) { c.forEach((x) => this.s.add(x)); } remove(...c) { c.forEach((x) => this.s.delete(x)); }
  toggle(c, f) { if (f === undefined) f = !this.s.has(c); if (f) this.s.add(c); else this.s.delete(c); return f; } contains(c) { return this.s.has(c); } toString() { return [...this.s].join(' '); } }
const camel = (k) => k.replace(/-([a-z])/g, (_m, c) => c.toUpperCase());
function matchOne(el, sel) {
  if (!el || el.nodeType !== 1) return false;
  const m = /^([a-z]+)?((?:\.[\w-]+)*)((?:\[[^\]]+\])*)$/i.exec(sel.trim());
  if (!m) return false;
  if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
  const classes = (m[2] || '').split('.').filter(Boolean);
  if (!classes.every((c) => el.classList.contains(c))) return false;
  const attrs = [...(m[3] || '').matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)];
  return attrs.every(([, name, value]) => {
    const got = name.startsWith('data-') ? el.dataset[camel(name.slice(5))] : el.attrs[name];
    return value === undefined ? got !== undefined : String(got) === value;
  });
}
const matches = (el, sel) => sel.split(',').some((s) => matchOne(el, s));
class Element {
  constructor(tag) { this.tagName = String(tag).toUpperCase(); this.nodeType = 1; this.classList = new ClassList(this); this.attrs = {}; this.dataset = {}; this.childNodes = []; this.parentNode = null; this.listeners = {}; this.offsetHeight = 0; this.rect = null; this.isConnected = true;
    const style = {}; style.setProperty = (k, v) => { style[k] = String(v); }; style.getPropertyValue = (k) => style[k] || ''; this.style = style; }
  set className(v) { this.classList = new ClassList(this); String(v).split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c)); } get className() { return this.classList.toString(); }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k.startsWith('data-')) this.dataset[camel(k.slice(5))] = String(v); } getAttribute(k) { return k in this.attrs ? this.attrs[k] : k.startsWith('data-') && this.dataset[camel(k.slice(5))] !== undefined ? this.dataset[camel(k.slice(5))] : null; }
  removeAttribute(k) { delete this.attrs[k]; } hasAttribute(k) { return k in this.attrs; }
  appendChild(n) { if (n.parentNode) n.parentNode.removeChild(n); n.parentNode = this; this.childNodes.push(n); return n; }
  removeChild(n) { const i = this.childNodes.indexOf(n); if (i >= 0) this.childNodes.splice(i, 1); n.parentNode = null; return n; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); this.isConnected = false; }
  get children() { return this.childNodes.filter((c) => c.nodeType === 1); }
  get parentElement() { return this.parentNode; }
  set textContent(v) { this._text = String(v); this.childNodes = []; } get textContent() { return this._text || ''; }
  set innerHTML(v) {
    this._html = String(v); this.childNodes = [];
    if (this.classList.contains('lp-lines')) {
      for (const m of this._html.matchAll(/<div class="(lp-line [^"]*)" data-i="(\d+)" data-b="([^"]*)"/g)) {
        const line = new Element('div'); line.className = m[1]; line.dataset.i = m[2]; line.dataset.b = m[3]; this.appendChild(line);
      }
    }
  }
  get innerHTML() { return this._html || ''; }
  addEventListener(ev, fn) { this.listeners[ev] = fn; }
  querySelector(sel) { for (const c of this.childNodes) { if (matches(c, sel)) return c; const d = c.querySelector ? c.querySelector(sel) : null; if (d) return d; } return null; }
  querySelectorAll(sel) { const out = []; const walk = (n) => n.childNodes.forEach((c) => { if (matches(c, sel)) out.push(c); if (c.childNodes) walk(c); }); walk(this); return out; }
  closest(sel) { let n = this; while (n && n.nodeType === 1) { if (matches(n, sel)) return n; n = n.parentNode; } return null; }
  contains(node) { let n = node; while (n) { if (n === this) return true; n = n.parentNode; } return false; }
  getBoundingClientRect() { const r = this.rect || { top: 0, left: 0, width: 0, height: 0 }; return { ...r, right: r.left + r.width, bottom: r.top + r.height }; }
  getClientRects() { return [1]; }
  focus() { focused.push(this.dataset.name || this.tagName); } blur() {} scrollIntoView() {} scrollBy() {}
}
const textNode = (data, parent) => { const n = { nodeType: 3, data, parentNode: parent, get parentElement() { return parent; } }; parent.childNodes.push(n); return n; };
const el = (tag, cls, data) => { const e = new Element(tag); if (cls) e.className = cls; Object.entries(data || {}).forEach(([k, v]) => { e.dataset[k] = v; }); return e; };

// ---------------------------------------------------------------- globals: Alpine, timers, storage, fetch
const reg = {}; const inits = []; const stores = {}; const toasts = []; const focused = []; const replaced = [];
let caretHit = null;
globalThis.window = globalThis;
globalThis.document = { hidden: false, activeElement: null, createElement: (t) => new Element(t), addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); },
  querySelector: (sel) => (sel === 'meta[name="csrf-token"]' ? { content: 'tok' } : null), querySelectorAll: () => [], caretPositionFromPoint: () => caretHit };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: (n, v) => { if (v !== undefined) stores[n] = v; return stores[n]; } };
globalThis.location = { hash: '', pathname: '/books/1/layout/', search: '' };
globalThis.history = { replaceState: (a, b, url) => replaced.push(url) };
globalThis.innerWidth = 1400; globalThis.innerHeight = 900;
let reducedFlag = false; globalThis.matchMedia = () => ({ matches: reducedFlag });
globalThis.requestAnimationFrame = (fn) => { fn(); return 1; };
const timers = []; let timerId = 0;
globalThis.setTimeout = (fn, ms) => { timerId += 1; timers.push({ id: timerId, fn, ms: ms || 0, cleared: false }); return timerId; };
globalThis.clearTimeout = (id) => { const t = timers.find((x) => x.id === id); if (t) t.cleared = true; };
const local = {}; globalThis.localStorage = { getItem: (k) => (k in local ? local[k] : null), setItem: (k, v) => { local[k] = String(v); } };
const session = {}; globalThis.sessionStorage = { getItem: (k) => (k in session ? session[k] : null), setItem: (k, v) => { session[k] = String(v); } };
globalThis.Nassakh = { toast: (m) => toasts.push(m) };
const channels = []; globalThis.BroadcastChannel = class { constructor(name) { this.name = name; this.onmessage = null; channels.push(this); } postMessage() {} close() { this.closed = true; } };
const settle = async () => { for (let i = 0; i < 12; i += 1) await new Promise((r) => setImmediate(r)); };
const pending = (ms) => timers.filter((t) => !t.cleared && (ms === undefined || t.ms === ms));
const fire = async (ms) => { const list = pending(ms); const t = list[list.length - 1]; if (!t) return false; t.cleared = true; await t.fn(); await settle(); return true; };
const drop = () => { timers.forEach((t) => { t.cleared = true; }); };

// the server: the live layout (pages by number, a revision), the chapters, scripted answers
const server = { revision: 3, pages: new Map(fixture.pages.map((p) => [p.n, p])), pageCount: 5, chapters: fixture.ranges, stale: false, docs: clone(fixture.docs), versions: { h10: 'v1', h20: 'w1' }, put: null, relayout: null, uncertain: fixture.uncertain, choose: null, find: null, sheet: null };
const calls = [];
const reply = (status, data) => ({ ok: status < 400, status, json: async () => clone(data) });
const layoutPayload = (from, to) => ({ scope: 'book', revision: server.revision, page_count: server.pageCount, first_page: 1, from, to, chapters: server.chapters, checks: [], geometry: fixture.geometry, font_css: '@font-face{}', stale: server.stale,
  pages: [...server.pages.values()].filter((p) => p.n >= from && p.n <= to).map((p) => ({ ...p, url: `/m/page-${p.n}.webp`, url2x: `/m/page-${p.n}-2x.webp` })) });
const previewPayload = () => ({ scope: 'book', status: 'done', stale: false, rendering: false, page_count: server.pageCount, pages: [], pdf_url: '/m/book.pdf', error: '', missing_fonts: [], checks: [],
  layout: { revision: server.revision, page_count: server.pageCount, chapters: server.chapters, stale: server.stale } });
globalThis.fetch = async (url, init = {}) => {
  const method = init.method || 'GET';
  const body = init.body ? JSON.parse(init.body) : null;
  calls.push([method, url, body]);
  let m;
  if ((m = /^\/api\/books\/1\/preview\/layout\/\?from=(\d+)&to=(\d+)$/.exec(url))) return reply(200, layoutPayload(Number(m[1]), Number(m[2])));
  if (url.startsWith('/api/books/1/preview/')) return method === 'POST' ? reply(202, previewPayload()) : reply(200, previewPayload());
  if ((m = /^\/api\/books\/1\/chapters\/(\w+)\/relayout\/$/.exec(url))) return reply(202, server.relayout ? server.relayout(m[1]) : { id: 8, status: 'queued', url: '/api/books/1/relayout/8/', result: {} });
  if ((m = /^\/api\/books\/1\/relayout\/(\d+)\//.exec(url))) return reply(200, server.status ? server.status(Number(m[1]), url) : { id: Number(m[1]), status: 'done', result: { unchanged: true }, pages: [] });
  if ((m = /^\/api\/books\/1\/chapters\/(\w+)\/$/.exec(url))) {
    const cid = m[1];
    if (method === 'PUT') return server.put ? server.put(cid, body) : reply(200, { version: 'v2', id: cid, chapters: [{ id: cid, version: 'v2', title: 'الفصل الأول' }], reload: false, manuscript_version: 2, changed: true, relayout: { id: 7, status: 'queued', url: '/api/books/1/relayout/7/', result: {} } });
    const nodes = server.docs[cid];
    return nodes ? reply(200, { id: cid, version: server.versions[cid], content: { type: 'doc', content: nodes }, warnings: [], number: cid === 'h10' ? 2 : 3, kind: 'chapter', title: cid, prev: cid === 'h20' ? 'h10' : 'p1', next: cid === 'h10' ? 'h20' : null, count: 3, source_pages: { first: 2, last: 3 }, pages: null, drift: false }) : reply(404, { detail: 'الفصل غير موجود.' });
  }
  if (url === '/api/books/1/chapters/') return reply(200, fixture.summaries);
  if (url === '/api/books/1/uncertain/') return reply(200, { count: server.uncertain.length, manuscript_version: 2, items: server.uncertain });
  if (url === '/api/books/1/uncertain/choose/' || url === '/api/books/1/uncertain/accept/' || url === '/api/books/1/uncertain/type/') return server.choose ? server.choose(url, body) : reply(200, { chapter: body.chapter, version: 'v5', manuscript_version: 5, word: 'كيانها', start: body.start, end: body.start + 6, remaining: server.uncertain.length - 1, relayout: null });
  if (url === '/api/books/1/find-replace/') return server.find ? server.find(body) : reply(200, { matches: [{ chapter: 'h10', block: 'p11', note: null, index: 4, length: 4 }, { chapter: 'h20', block: 'p21', note: null, index: 0, length: 4 }], total: 2, replaced: 0 });
  if (url === '/api/books/1/stylesheet/') return server.sheet ? server.sheet(body) : reply(200, { ...fixture.sheetPayload, saved: true });
  if (url.startsWith('/api/books/1/sheets/')) return reply(200, { pages: [{ number: Number(/from=(\d+)/.exec(url)[1]), id: 9, width: 1000, height: 1400, display_url: '/m/scan.webp', lines: [{ id: 1, bbox: [0.1, 0.2, 0.9, 0.25] }, { id: 2, bbox: [0.1, 0.3, 0.9, 0.35] }] }] });
  if (url === '/api/books/1/snapshots/') return reply(200, []);
  if (url === '/api/books/1/drift/') return reply(200, server.drift || { edited: false, pages: [], chapters: [] });
  if ((m = /^\/api\/books\/1\/chapters\/(\w+)\/reassemble\/$/.exec(url))) return server.reassemble ? server.reassemble(m[1], body) : reply(202, { run_id: 12, status: 'queued', stage: '' });
  return reply(404, { detail: 'لا' });
};

// ---------------------------------------------------------------- the one-block editor, stubbed
const editors = [];
const notes = [];
globalThis.NassakhEditor = Object.assign({}, convert, {
  createBlock(host, opts) {
    const ed = { host, opts, node: clone(opts.node), at: opts.offset, destroyed: false, focusedNow: false, calls: [], top: 0 };
    host.offsetHeight = 22.1 * 2 * (600 / 481.89);
    Object.assign(ed, {
      get isFocused() { return ed.focusedNow; },
      getNode: () => clone(ed.node),
      style: () => convert.styleOf(ed.node),
      offset: () => ed.at,
      range: () => ({ from: ed.at, to: ed.at }),
      setOffset(o) { ed.at = o; ed.calls.push(['setOffset', o]); return ed; },
      setNode(node, o) { ed.node = clone(node); ed.at = o; ed.calls.push(['setNode', convert.plainText(node).length, o]); return ed; },
      focus() { ed.focusedNow = true; return ed; }, blur() { ed.focusedNow = false; },
      destroy() { ed.destroyed = true; },
      // the top of the caret's line inside the paragraph: as the engine broke it (the fixture's lines), in pixels
      lineTop(off) {
        const lines = fixture.pages.flatMap((p) => p.lines.filter((l) => l.block === ed.node.attrs.id && l.kind !== 'note').map((l) => ({ n: p.n, ...l })));
        if (!lines.length) return 0;
        const i = lines.findIndex((l) => off >= l.start && off <= l.end);
        const before = lines.slice(0, i < 0 ? 0 : i);
        return before.reduce((sum, l) => sum + l.h, 0) * (600 / 481.89);
      },
      isBold: () => false, isItalic: () => false,
      toggleBold() { ed.calls.push(['bold']); }, toggleItalic() { ed.calls.push(['italic']); },
      setStyle(k) { ed.calls.push(['style', k]); if (k === 'heading2') ed.node = { ...ed.node, type: 'heading', attrs: { ...ed.node.attrs, level: 2 } }; opts.onChange(); return true; },
      setAttrs(patch) { ed.node = { ...ed.node, attrs: { ...ed.node.attrs } }; Object.entries(patch).forEach(([k, v]) => { if (v === null) delete ed.node.attrs[k]; else ed.node.attrs[k] = v; }); opts.onChange(); return ed; },
      setNumbers() { ed.calls.push(['numbers']); return ed; }, togglePageMarks(on) { ed.calls.push(['marks', on]); return ed; },
      placeAtX(x, which) { ed.calls.push(['placeAtX', x, which]); return ed; },
      setFind(ranges) { ed.calls.push(['find', ranges]); return ed; },
      insertFootnote() { ed.calls.push(['insertFootnote']); return { id: 'nek1', dom: el('sup', 'ed-fn') }; },
      noteAt: (id) => ({ id, content: [{ type: 'text', text: 'حاشية' }], text: 'حاشية', sourcePage: 2, orphan: false, dom: el('sup', 'ed-fn') }),
      setNoteContent(id, c) { ed.calls.push(['note', id, c]); return true; }, deleteNote(id) { ed.calls.push(['deleteNote', id]); return true; },
      replaceOffsets(a, b, t) { ed.calls.push(['replaceOffsets', a, b, t]); return true; },
      acceptUncertain(a, b) { ed.calls.push(['accept', a, b]); return true; }, replaceRange(a, b, t) { ed.calls.push(['replaceRange', a, b, t]); return true; },
      // test helpers: typing replaces the text, a taller paragraph grows the box
      type(text) { ed.node = { ...ed.node, content: [{ type: 'text', text }] }; opts.onChange(); },
      grow(lines) { host.offsetHeight = 22.1 * lines * (600 / 481.89); },
    });
    editors.push(ed);
    return ed;
  },
  createNote(host, opts) { const n = { host, opts, calls: [] }; Object.assign(n, { focus() { n.calls.push('focus'); }, destroy() { n.calls.push('destroy'); }, setContent(c) { n.calls.push(['set', c]); }, toggleBold() {}, toggleItalic() {} }); notes.push(n); return n; },
});

(0, eval)(readFileSync(`${root}/static/src/js/keys.js`, 'utf8')); // NassakhKeys (D69)
for (const name of ['geometry.js', 'stage.js', 'style.js', 'cover.js', 'edit.js', 'panel.js', 'page.js']) (0, eval)(readFileSync(`${root}/static/src/js/book/${name}`, 'utf8'));
globalThis.NassakhBook.register();
// the filmstrip's page thumbs (the cover's thumb, D80, comes first and is hidden without a cover)
const pageThumbs = (v) => v._dom.film.children.filter((t) => t.dataset.number !== undefined);

const mkDom = () => {
  const rootEl = el('div', '', { book: '' });
  const canvas = el('div', 'lo-canvas', { canvas: '' }); canvas.clientWidth = 1000; rootEl.appendChild(canvas);
  const sheets = {};
  ['right', 'left'].forEach((side) => {
    const fig = el('figure', 'lo-sheet', { sheet: side });
    const page = el('div', 'lp-page'); page.rect = { top: 100, left: side === 'right' ? 700 : 90, width: 600, height: 846.6 };
    const lines = el('div', 'lp-lines', { lines: '' }); const host = el('div', 'lp-edit', { editHost: '' });
    page.appendChild(lines); page.appendChild(host); fig.appendChild(page); canvas.appendChild(fig);
    sheets[side] = { fig, page, lines, host };
  });
  const body = el('div', 'bp-tab-body'); rootEl.appendChild(body);
  const film = el('div', 'lo-film-track', { filmTrack: '' }); body.appendChild(film);
  const list = el('nav', 'bp-chapters', { chapterList: '' }); body.appendChild(list);
  return { root: rootEl, canvas, sheets, film, list };
};
const refs = () => ({ stage: el('div', 'lo-stage'), jump: Object.assign(el('input'), { select() {} }), findQuery: Object.assign(el('input'), { select() {} }), noteHost: el('div'), pop: el('div'), wordTyped: Object.assign(el('input'), { select() {} }), drawerClose: el('button'), snapshotLabel: el('input'), snapshotsClose: el('button'), sheetClose: el('button'), digitsFirst: el('button') });
const mk = (extra = {}, initial = {}) => {
  const d = mkDom();
  const cfg = clone({ ...fixture.config, ...extra, initial: { ...fixture.config.initial, ...initial } });
  const v = reg.bookPage(cfg);
  v.$el = d.root; v.$refs = refs(); v.$nextTick = (fn) => fn();
  v.init();
  return Object.assign(v, { _dom: d });
};
const shownLines = (v, side = 'right') => v._dom.sheets[side].lines.children.map((c) => [Number(c.dataset.i), c.dataset.b, c.classList.contains('is-hidden'), c.style['--dy'] || '0', c.classList.contains('is-over')]);
const gets = () => calls.filter((c) => c[0] === 'GET').map((c) => c[1]);
const lineTarget = (v, side, i, runIndex, charIndex) => {
  const page = v.ctx().pages.get(v.shownNumbers[side]);
  const lineEl = v._dom.sheets[side].lines.children.find((c) => Number(c.dataset.i) === i);
  const run = el('span', 'lp-run', { r: String(runIndex) });
  lineEl.appendChild(run);
  const node = textNode(page.lines[i].runs[runIndex].text, run);
  caretHit = { offsetNode: node, offset: charIndex };
  lineEl.rect = { top: 300, left: 150, width: 400, height: 20 };
  return { target: node.parentElement, clientX: 400, clientY: 310 };
};

const out = {};
(async () => {
  // ---- first paint: the embedded live pages, the page asked for, the pages around it fetched once
  const v = mk();
  await settle();
  out.first = { pageCount: v.pageCount, revision: v.revision, current: v.current, phase: v.phase, painted: v._dom.sheets.right.page.dataset.n,
    lines: shownLines(v).map((l) => l[1]), left: v._dom.sheets.left.lines.innerHTML, gets: gets(), thumbs: pageThumbs(v).map((t) => [t.dataset.number, t.childNodes[0].childNodes[0].getAttribute('src'), t.classList.contains('is-current')]),
    tab: v.tab, mode: v.mode, counter: v.counterText, store: stores.bookPage.view === v, html: v._dom.sheets.right.lines.innerHTML.slice(0, 400) };
  // ---- virtualisation: one page in the DOM, two in a spread; turning redraws, fetching only what is missing
  v.setSpread(true);
  out.spread = { right: v._dom.sheets.right.page.dataset.n, left: v._dom.sheets.left.page.dataset.n, leftLines: shownLines(v, 'left').length };
  v.setSpread(false);
  calls.length = 0;
  v.turn(1); await fire(200); await settle();
  out.turned = { current: v.current, painted: v._dom.sheets.right.page.dataset.n, left: v._dom.sheets.left.page.dataset.n, gets: gets(), replaced: replaced.slice(-1)[0] };
  v.showPage(2, { instant: true });

  // ---- the mode switch: edit loads the chapter under the eyes, the panel shows the mode's tab; both remembered
  calls.length = 0;
  v.setMode('edit'); await settle();
  out.edit = { mode: v.mode, tab: v.tab, chapter: v.editChapterId, version: v.version, gets: gets(), address: replaced.slice(-1)[0] };
  v.setTab('find'); v.setMode('preview'); await settle();
  out.preview = { mode: v.mode, tab: v.tab, stored: [local['nassakh.book.tab.edit'], local['nassakh.book.tab.preview']] };
  v.setMode('edit'); await settle();
  out.editAgain = v.tab;
  v.setTab('source');

  // ---- a click on a line: the point → (block, offset); the paragraph opens in place over its lines, anchored
  // so the clicked line stays where it was (its second line: the box starts one line higher, on its first)
  const click = lineTarget(v, 'right', 6, 0, 10);
  v.onSheetClick(click, 'right'); await settle();
  const ed = editors[editors.length - 1];
  const host = v._dom.sheets.right.host;
  out.open = { block: v.open && v.open.block, n: v.open && v.open.n, offset: ed.at, node: ed.node.attrs.id, inHost: host.childNodes.includes(ed.host), cls: ed.host.className, style: { x: ed.host.style['--x'], w: ed.host.style['--w'], fs: ed.host.style['--fs'], lh: ed.host.style['--lh'], indent: ed.host.style['--indent'], top: ed.host.style['--top'] },
    focused: ed.focusedNow, lines: shownLines(v).filter((l) => l[1] === 'p13').map((l) => l[2]), others: shownLines(v).filter((l) => l[1] !== 'p13').some((l) => l[2]), tab: v.tab, clip: host.style['--clip'] };
  // a second click in the same paragraph only moves the caret
  v.onSheetClick(lineTarget(v, 'right', 5, 0, 3), 'right'); await settle();
  out.sameBlock = { editors: editors.length, offset: ed.at };

  // ---- the pause → PUT (the chapter's nodes, its version) → re-layout (long poll) → pages spliced in
  // place: the chapter now takes 3 pages (delta +1), the later pages renumbered and on the other side
  calls.length = 0;
  const grownText = convert.plainText(fixture.docs.h10[3]) + ' وزاد النص سطرًا';
  const fresh = fixture.relaid.map((p) => ({ ...p, url: null, url2x: null }));
  const relaid = (id) => ({ id, status: 'done', chapter: 'h10', version: 2, url: `/api/books/1/relayout/${id}/`, error: '', pages: fresh,
    result: { mode: 'chapter', full: false, from: 2, to: 3, count: 3, delta: 1, chapter_delta: 1, shifted_from: 4, flip: true, side_shift_pt: -11.34, page_count: 6, chapters: [{ id: 'p1', first: 1, last: 1 }, { id: 'h10', first: 2, last: 4 }, { id: 'h20', first: 5, last: 6 }], checks: [], revision: { before: 3, after: 4 } } });
  server.status = (id) => relaid(id);
  ed.type(grownText);
  const beforePause = { state: v.editSave.state, pill: v.savePill.text, timer: pending(500).length, puts: calls.filter((c) => c[0] === 'PUT').length };
  await fire(500); await settle();
  const put = calls.find((c) => c[0] === 'PUT');
  out.saved = { beforePause, url: put[1], putVersion: put[2].version, texts: put[2].content.content.map((n) => convert.plainText(n).length), p13: convert.plainText(put[2].content.content[3]) === grownText, relayoutGets: gets().filter((u) => u.includes('/relayout/7/')),
    pageCount: v.pageCount, revision: v.revision, numbers: [...v.ctx().pages.keys()].sort((a, b) => a - b), sides: [...v.ctx().pages.values()].sort((a, b) => a.n - b.n).map((p) => [p.n, p.side, p.number && p.number.text]),
    p5x: v.ctx().pages.get(5).lines[0].x, chapterDeltas: v.chapterDeltas, delta: v.delta, footprint: [v.footprint.text, v.footprint.deltaText], version: v.version, state: v.editSave.state, pill: v.savePill.text,
    stillOpen: !ed.destroyed && v.open && v.open.block, editors: editors.length, relayout: v.relayout.state, current: v.current, numbersAsked: ed.calls.filter((c) => c[0] === 'numbers').length,
    thumbs: pageThumbs(v).map((t) => [t.dataset.number, t.classList.contains('is-pending')]), laid: convert.plainText(convert.locate(v.ctx().laid, 'p13').node) === grownText };
  // a re-layout seen from a later page: the page shown moves with its content (5 → 6)
  v.ctx().pages = new Map(fixture.pages.map((p) => [p.n, p])); v.revision = 3; v.pageCount = 5; v.rebuildSequence();
  v.closeBlock({ commit: false }); drop(); v.showPage(5, { instant: true });
  v.applyRelayoutPayload(relaid(11));
  out.later = { current: v.current, side: v.ctx().pages.get(6).side, painted: v._dom.sheets.right.page.dataset.n };
  v.showPage(2, { instant: true });
  v.onSheetClick(lineTarget(v, 'right', 6, 0, 10), 'right'); await settle();
  const edB = editors[editors.length - 1];

  // ---- the open paragraph grows: the lines below it move; one pushed past the body is hidden
  edB.opts.onBoundary('escape', {}); await settle();
  v.onSheetClick(lineTarget(v, 'right', 4, 0, 4), 'right'); await settle();
  const e12 = editors[editors.length - 1];
  const grownTop = e12.host.style['--top'];
  e12.grow(4); v.layoutPatches();
  out.grown = { block: e12.node.attrs.id, top: grownTop, lines: shownLines(v).map((l) => [l[1], l[2], l[3], l[4]]) };
  e12.grow(2); v.layoutPatches();
  e12.opts.onBoundary('escape', {}); await settle();

  // ---- a 409: the conflict banner, the pill; «إعادة التحميل» takes the other window's text
  server.put = () => reply(409, { detail: 'تغيّر هذا الفصل في نافذة أخرى.', version: 'v9', content: [...clone(fixture.docs.h10).slice(0, 3), { type: 'paragraph', attrs: { id: 'p13' }, content: [{ type: 'text', text: 'نص النافذة الأخرى' }] }, clone(fixture.docs.h10[4])] });
  v.onSheetClick(lineTarget(v, 'right', 6, 0, 10), 'right'); await settle();
  const eC = editors[editors.length - 1];
  calls.length = 0;
  eC.type('نص جديد');
  await fire(500); await settle();
  out.conflict = { open: v.conflict.open, version: v.conflict.version, state: v.editSave.state, pill: v.savePill.text };
  v.reloadConflict(); await settle();
  out.reloaded = { open: v.conflict.open, version: v.version, p13: convert.plainText(v.nodes()[3]), editorClosed: eC.destroyed, openNow: v.open, relayoutPosts: calls.filter((c) => c[0] === 'POST' && c[1].includes('/relayout/')).length };
  server.put = null; server.status = null;

  // ---- Enter splits the paragraph (the second half opens under the first, the first half drawn by the
  // browser until the engine lays it out again); Backspace at the start joins it back
  server.status = (id) => ({ id, status: 'running', url: `/api/books/1/relayout/${id}/`, result: {}, pages: [] });
  const at = v.ctx().pages.get(2).lines.findIndex((l) => l.block === 'p11');
  v.onSheetClick(lineTarget(v, 'right', at, 0, 5), 'right'); await settle();
  const e2 = editors[editors.length - 1];
  calls.length = 0;
  e2.opts.onBoundary('split', { node: e2.getNode(), from: 5, to: 5 }); await settle();
  const e3 = editors[editors.length - 1];
  const ids = convert.flatBlocks(v.nodes()).map((b) => b.id);
  const patch = v.ctx().patches.get('p11');
  out.split = { e2closed: e2.destroyed, e3block: e3.node.attrs.id === ids[2] && ids[2] !== 'p11', e3offset: e3.at, ids: ids.length, first: convert.plainText(v.nodes()[1]).length, second: convert.plainText(v.nodes()[2]).length, undo: v.canUndo,
    patch: Boolean(patch), patchHtml: patch && patch.el.innerHTML, patchCls: patch && patch.el.className, patchTop: patch && patch.el.style['--top'], newTop: e3.host.style['--top'], hidden: shownLines(v).filter((l) => l[1] === 'p11').map((l) => l[2]),
    puts: calls.filter((c) => c[0] === 'PUT').length, relayout: v.relayout.state, pill: v.renderPill.text };
  server.status = null;
  e3.opts.onBoundary('mergeBackward', {}); await settle();
  const e4 = editors[editors.length - 1];
  out.merged = { block: e4.node.attrs.id, offset: e4.at, count: convert.flatBlocks(v.nodes()).length, text: convert.plainText(v.nodes()[1]).length };
  // the chapter's undo: back to the split, forward again
  v.undo(); await settle();
  const afterUndo = convert.flatBlocks(v.nodes()).length;
  v.redo(); await settle();
  out.history = { afterUndo, afterRedo: convert.flatBlocks(v.nodes()).length, canUndo: v.canUndo, canRedo: v.canRedo };
  // ↓ past the last line: the next block, the caret under the same x
  e4.opts.onBoundary('down', { x: 420 }); await settle();
  let e5 = editors[editors.length - 1];
  out.down = { block: e5.node.attrs.id, calls: e5.calls.filter((c) => c[0] === 'placeAtX') };
  // the style picker and «ابدأ صفحة جديدة» on the open paragraph (a flag saves at once)
  calls.length = 0;
  v.setStyle('heading2'); await settle();
  const styled = { calls: e5.calls.filter((c) => c[0] === 'style'), label: v.styleLabel, cls: e5.host.className };
  v.setStyle('paragraph');
  v.toggleFlag('breakBefore'); await settle();
  const flagPut = calls.filter((c) => c[0] === 'PUT').pop();
  out.styled = { ...styled, flag: v.flags.breakBefore, put: flagPut && convert.locate(flagPut[2].content.content, e5.node.attrs.id).node.attrs.breakBefore, block: v.blockInfo && [v.blockInfo.id, v.blockInfo.breakBefore, v.blockInfo.pages] };
  // a footnote: the call clicked opens its note's small editor (one overlay); ⌘⇧F inserts one at the caret
  const callLine = v.ctx().pages.get(2).lines.findIndex((l) => l.block === 'p12');
  const callTarget = lineTarget(v, 'right', callLine, 1, 1);
  callTarget.target.classList.add('is-sup'); callTarget.target.dataset.note = 'n12';
  v.onSheetClick(callTarget, 'right'); await settle();
  out.note = { pop: v.pop.kind, note: v.note.id, number: v.note.number, open: v.open && v.open.block, notes: notes.length, content: notes[notes.length - 1] && notes[notes.length - 1].opts.content };
  notes[notes.length - 1].opts.onUpdate([{ type: 'text', text: 'حاشية معدّلة' }]);
  out.noteEdit = editors[editors.length - 1].calls.filter((c) => c[0] === 'note');
  v.closePop(); v.insertFootnote(); await settle();
  out.inserted = { pop: v.pop.kind, id: v.note.id, calls: editors[editors.length - 1].calls.filter((c) => c[0] === 'insertFootnote').length };
  v.closePop();
  // Esc closes the paragraph, a second Esc leaves edit mode
  e5 = editors[editors.length - 1];
  v.onKey({ key: 'Escape', target: {}, preventDefault() {} }); await settle();
  const escOnce = { open: v.open, mode: v.mode, destroyed: e5.destroyed };
  v.onKey({ key: 'Escape', target: {}, preventDefault() {} }); await settle();
  out.escape = { escOnce, mode: v.mode, tab: v.tab };

  // ---- the keyboard: preview turns and toggles; ⌘Z is the chapter's undo in edit mode; fields keep their keys
  drop(); v.showPage(2, { instant: true });
  v.onKey({ key: 'ArrowLeft', target: {}, preventDefault() {} }); await fire(200);
  const keyNext = v.current;
  v.onKey({ key: 'e', code: 'KeyE', target: {}, preventDefault() {} }); await settle();
  const keyMode = v.mode;
  v.onKey({ key: 'z', code: 'KeyZ', metaKey: true, target: {}, preventDefault() {} });
  const keyUndo = v.canRedo;
  v.onKey({ key: 'e', code: 'KeyE', target: { tagName: 'INPUT' }, preventDefault() {} });
  out.keys = { keyNext, keyMode, keyUndo, field: v.mode };
  v.setMode('preview'); await settle();

  // ---- the uncertain words: loaded with the tab, grouped; a pick shows the word's page lit; a reading chosen
  calls.length = 0;
  v.setTab('uncertain'); await settle();
  const groups = v.uncertainGroups;
  const word = v.uncertain.items[0];
  v.pickUncertain(word); await fire(200); await settle();
  const deco = v.decorationsFor(v.current);
  out.uncertain = { count: v.uncertain.count, groups: groups.map((g) => [g.chapter, g.count, g.pages.map((p) => p.page)]), current: v.current, deco: deco.get(word.block), picked: v.isPicked(word), html: v._dom.sheets.right.lines.innerHTML.includes('lp-picked') };
  await v.resolveUncertain(word, 'choose', { engine: 'secondary' }); await settle();
  const post = calls.find((c) => c[0] === 'POST' && c[1].includes('/uncertain/'));
  out.chosen = { url: post[1], body: post[2], count: v.uncertain.count, left: v.uncertain.items.length, live: v.liveMessage };
  // a 409 (the chapter moved on): once more with the chapter's version, then the list is loaded again
  server.choose = (url, body) => (body.version === 'v7' ? reply(200, { chapter: 'h10', version: 'v8', word: 'اهل', start: body.start, end: body.end, remaining: 0, relayout: null }) : reply(409, { detail: 'تغيّر', version: 'v7' }));
  await v.resolveUncertain(v.uncertain.items[0], 'accept'); await settle();
  out.retried = calls.filter((c) => c[0] === 'POST' && c[1].includes('/uncertain/accept/')).map((c) => c[2].version);
  server.choose = null;

  // ---- find in the chapter: the matches with their pages; Enter turns to the next match's page and lights it
  v.setTab('find'); v.find.query = 'برقة'; v.onFindInput(); await fire(200); await settle();
  const results = v.find.results.map((m) => [m.block, m.note, m.start, m.page]);
  const idx0 = v.find.index;
  v.findStep(1); await fire(200); await settle();
  out.find = { results, idx0, idx1: v.find.index, current: v.current, flash: v.flash, count: v.findCountText, deco: [...v.decorationsFor(v.current).keys()] };
  v.setFindScope('book'); await settle();
  out.findBook = { groups: v.find.groups.map((g) => [g.id, g.count]), total: v.find.total, text: v.findCountText, post: calls.filter((c) => c[1].includes('find-replace')).pop()[2] };
  v.setFindScope('chapter'); await settle();

  // ---- the stylesheet: a change → one PUT; the page setup changed → the chapter under the eyes laid out again
  // (a whole-book layout: the pages shown are fetched again from the new revision)
  calls.length = 0; drop();
  server.relayout = () => ({ id: 9, status: 'queued', url: '/api/books/1/relayout/9/', result: {} });
  server.status = (id) => ({ id, status: 'done', result: { full: true, revision: { before: 4, after: 5 } }, pages: [] });
  server.revision = 5; // the whole-book layout the re-layout made (an answer older than the pages held is ignored)
  v.setField('body_size_pt', 14);
  await fire(400); await settle();
  server.revision = 5;
  await settle();
  out.sheet = { puts: calls.filter((c) => c[0] === 'PUT').map((c) => [c[1], c[2]]), posts: calls.filter((c) => c[0] === 'POST').map((c) => c[1]), gets: gets().filter((u) => u.includes('layout') || u.includes('relayout')), pill: v.sheetSave.state, after: [v.revision, v.pageCount] };

  // ---- a newer live revision in the polling (the book render adopted): the pages shown are fetched again
  calls.length = 0;
  server.revision = 6;
  v.applyPreview(previewPayload()); await settle();
  out.adopted = { revision: v.revision, gets: gets() };

  // ---- a double-click in preview: edit mode, the paragraph open at the point
  drop(); v.showPage(2, { instant: true });
  const before = editors.length;
  await v.onSheetDblClick(lineTarget(v, 'right', 1, 0, 6), 'right'); await settle();
  out.dbl = { mode: v.mode, tab: v.tab, opened: editors.length - before, block: v.open && v.open.block, offset: editors[editors.length - 1].at,
    find: editors[editors.length - 1].calls.filter((c) => c[0] === 'find') };
  v.setMode('preview'); await settle();

  // ---- a proofreader: no edit mode, no save
  drop(); calls.length = 0;
  const r = mk({ canEdit: false, mode: 'edit' });
  await settle();
  out.reader = { mode: r.mode, refused: r.setMode('edit'), toast: toasts.slice(-1)[0], puts: calls.filter((c) => c[0] === 'PUT').length, tab: r.tab };
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


def _component_fixture() -> dict:
    pages = _chapter_pages()
    first = _page_of(
        1,
        [
            _line(
                "front-title",
                "title",
                159.87,
                0,
                4,
                "كتاب",
                x=210.17,
                w=50.21,
                h=40.77,
                justify=False,
                first=True,
                style="book-title",
            )
        ],
        number=False,
        chapter=None,
    )
    pages = [first, *pages]
    ranges = [
        {"id": "p1", "title": "قبل", "first": 1, "last": 1},
        {"id": "h10", "title": "الفصل الأول", "first": 2, "last": 3},
        {"id": "h20", "title": "الفصل الثاني", "first": 4, "last": 5},
    ]
    geometry = {
        "width_pt": 481.89,
        "height_pt": 680.31,
        "margins_pt": {"top": 56.69, "bottom": 62.36, "inner": 62.36, "outer": 51.02},
        "side_shift_pt": -11.34,
        "page_number": "bottom_center",
    }
    # the chapter laid out again on three pages (2–4), fresh from the engine
    relaid = [dict(page, n=n) for n, page in zip((2, 3, 4), (pages[1], pages[2], pages[2]), strict=True)]
    relaid[2] = dict(
        relaid[2],
        side="right",
        lines=[
            _line("p14", "body", 56.69, 0, 30, _h10_texts()["p14"][:30].strip(), first=True, justify=False)
        ],
    )
    t = _h10_texts()
    uncertain = [
        {
            "chapter": "h10",
            "block": "p11",
            "note": None,
            "start": 4,
            "end": 8,
            "word": "برقة",
            "page": 2,
            "source_page": 2,
            "context": {"before": "أهل", "after": "يزرعون"},
            "readings": [
                {"engine": "primary", "label": "Qari v0.3", "text": "برقة", "current": True},
                {"engine": "secondary", "label": "Qari v0.2", "text": "برقه", "current": False},
            ],
        },
        {
            "chapter": "h10",
            "block": "p14",
            "note": None,
            "start": 0,
            "end": 3,
            "word": t["p14"][:3],
            "page": 3,
            "source_page": 3,
            "context": {"before": "", "after": "…"},
            "readings": [],
        },
    ]
    sheet = {
        "stylesheet": {
            "trim": "17x24",
            "width_mm": 170,
            "height_mm": 240,
            "top_mm": 20,
            "bottom_mm": 22,
            "inner_mm": 22,
            "outer_mm": 18,
            "bleed_mm": 0,
            "body_font": "amiri",
            "latin_font": "times",
            "heading_font": "amiri",
            "body_size_pt": 13,
            "line_height": 1.7,
            "indent_em": 1.5,
            "heading_scale": {"h1": 1.6, "h2": 1.25},
            "footnote_size_pt": 10,
            "footnote_numbering": "page",
            "running_header": "chapter",
            "page_number": "bottom_center",
            "chapter_opening": "any",
            "widows": 2,
            "orphans": 2,
            "keep_headings": True,
            "front_matter": {
                "title_page": True,
                "contents": True,
                "copyright_page": False,
                "fields": {"title": "", "author": ""},
            },
            "print_source_pages": False,
        },
        "saved": False,
        "trims": [{"key": "17x24", "label": "17×24 سم", "width_mm": 170, "height_mm": 240}],
        "fonts": [{"key": "amiri", "label": "أميري", "family": "Amiri", "installed": True, "latin": True}],
        "latin_fonts": ["amiri"],
        "choices": {},
        "limits": {"body_size_pt": [7, 24]},
        "missing_fonts": [],
        "field_defaults": {"title": "كتاب", "author": "م"},
    }
    urls = {
        "chapter": "/api/books/1/chapters/__cid__/",
        "chapters": "/api/books/1/chapters/",
        "relayout": "/api/books/1/chapters/__cid__/relayout/",
        "relayoutStatus": "/api/books/1/relayout/__rid__/",
        "pageLayout": "/api/books/1/preview/layout/",
        "preview": "/api/books/1/preview/",
        "stylesheet": "/api/books/1/stylesheet/",
        "uncertain": "/api/books/1/uncertain/",
        "uncertainAccept": "/api/books/1/uncertain/accept/",
        "uncertainChoose": "/api/books/1/uncertain/choose/",
        "uncertainType": "/api/books/1/uncertain/type/",
        "findReplace": "/api/books/1/find-replace/",
        "snapshots": "/api/books/1/snapshots/",
        "restore": "/api/books/1/snapshots/__sid__/restore/",
        "sheets": "/api/books/1/sheets/",
        "review": "/books/1/review/__n__/",
        "manuscriptState": "/api/books/1/manuscript/state/",
        "reassemble": "/api/books/1/chapters/__cid__/reassemble/",
        "convertDigits": "/api/books/1/convert-digits/",
    }
    initial_layout = {
        "scope": "book",
        "revision": 3,
        "page_count": 5,
        "first_page": 1,
        "from": 1,
        "to": 4,
        "chapters": ranges,
        "checks": [],
        "geometry": geometry,
        "font_css": "",
        "stale": False,
        "pages": [dict(p, url=f"/m/page-{p['n']}.webp", url2x=None) for p in pages[:4]],
    }
    config = {
        "bookId": 1,
        "title": "كتاب",
        "canEdit": True,
        "mode": "preview",
        "chapter": "h10",
        "requestedChapter": "h10",
        "relayoutMs": 500,
        "uncertainCount": 2,
        "fontCss": "",
        "chapters": [
            {"id": "p1", "number": 1, "kind": "front", "title": "قبل"},
            {"id": "h10", "number": 2, "kind": "chapter", "title": "الفصل الأول"},
            {"id": "h20", "number": 3, "kind": "chapter", "title": "الفصل الثاني"},
        ],
        "chapterSummaries": [
            {"id": "h10", "version": "v1", "drift": False, "source_pages": {"first": 2, "last": 3}},
            {"id": "h20", "version": "w1", "drift": False},
        ],
        "drift": {"edited": True, "pages": [], "chapters": []},
        "urls": urls,
        "initial": {"stylesheet": sheet, "preview": None, "layout": initial_layout},
    }
    docs = {
        "h10": _h10_nodes(),
        "h20": [
            heading("h20", "الفصل الثاني", pages=(4,)),
            para("p21", "برقة مدينة قديمة كبيرة", pages=(4,)),
            para("p22", "برقة مدينة", pages=(5,)),
        ],
    }
    return {
        "pages": pages,
        "ranges": ranges,
        "geometry": geometry,
        "relaid": relaid,
        "uncertain": uncertain,
        "config": config,
        "docs": docs,
        "sheetPayload": sheet,
        "summaries": config["chapterSummaries"],
    }


def test_book_page_component_under_node(tmp_path):
    folder = _node_tmp(tmp_path)
    fixture = _component_fixture()
    (folder / "fixture.json").write_text(json.dumps(fixture, ensure_ascii=False))
    out = _run_node(folder, "component.mjs", COMPONENT_HARNESS, str(ROOT), str(folder / "fixture.json"))
    scale = 600 / 481.89

    # --- the first paint: the embedded pages (1–4) drawn at once on the requested chapter's first page; the
    # one
    # page missing around it fetched; the filmstrip from the pages' images; the polling of the book render
    first = out["first"]
    assert (
        first["pageCount"] == 5
        and first["revision"] == 3
        and first["current"] == 2
        and first["phase"] == "pages"
    )
    assert first["painted"] == "2" and first["lines"] == [
        "h10",
        "p11",
        "p11",
        "p12",
        "p12",
        "p13",
        "p13",
        "p13",
        "n12",
    ]
    assert first["left"] == "" and first["gets"] == [
        "/api/books/1/preview/layout/?from=5&to=5",
        "/api/books/1/preview/?scope=book",
    ]
    assert [t[0] for t in first["thumbs"]] == ["1", "2", "3", "4", "5"] and first["thumbs"][1] == [
        "2",
        "/m/page-2.webp",
        True,
    ]
    assert (
        first["tab"] == "format"
        and first["mode"] == "preview"
        and first["counter"] == "صفحة 2 من 5"
        and first["store"] is True
    )
    assert first["html"].startswith(
        '<div class="lp-line k-heading is-c f-h" data-i="0" data-b="h10" dir="rtl" style="--x:201.92;--y:102.05;--w:89.38;--h:28.08;--fs:20.8">'  # noqa: E501
    )
    # --- virtualisation: one page in the DOM; a spread draws the facing page; a turn redraws (nothing
    # fetched)
    assert out["spread"] == {"right": "2", "left": "3", "leftLines": 2}
    assert out["turned"] == {
        "current": 3,
        "painted": "3",
        "left": "",
        "gets": [],
        "replaced": "/books/1/layout/?tab=format#page-3",  # §5.4: the address keeps the tab
    }
    # --- the mode switch: edit loads the chapter under the eyes once, the panel opens «الأصل»; the choice
    # of tab
    # is remembered per mode
    edit = out["edit"]
    assert (
        edit["mode"] == "edit"
        and edit["tab"] == "source"
        and edit["chapter"] == "h10"
        and edit["version"] == "v1"
    )
    assert (
        edit["gets"].count("/api/books/1/chapters/h10/") == 1
        and edit["address"] == "/books/1/layout/?mode=edit&tab=source#page-2"
    )
    assert (
        out["preview"] == {"mode": "preview", "tab": "format", "stored": ["find", None]}
        and out["editAgain"] == "find"
    )
    # --- a click on the second line of p13 at its 10th character: offset 61 + 10; the paragraph opens in
    # place,
    # on the page's measure, face, size and leading, its box on the block's first line (the clicked line
    # stays under the pointer), its own lines hidden, nothing else
    opened = out["open"]
    assert (
        opened["block"] == "p13" and opened["n"] == 2 and opened["offset"] == 71 and opened["node"] == "p13"
    )
    assert (
        opened["inHost"] is True and opened["cls"] == "lp-patch is-body is-open" and opened["focused"] is True
    )
    assert opened["style"] == {
        "x": "62.36",
        "w": "368.51",
        "fs": "13",
        "lh": "22.1",
        "indent": "19.5",
        "top": f"{244.04 - 52.69:.2f}",
    }
    assert opened["lines"] == [True, True, True] and opened["others"] is False and opened["clip"] == "52.69"
    assert out["sameBlock"] == {"editors": 1, "offset": 3}
    # --- the pause: one PUT of the chapter with its version, the re-layout followed by its long poll, three
    # new
    # pages for the chapter spliced in, the later pages renumbered (+1) and on the other side, the footprint's
    # delta, the paragraph still open (re-anchored), the new pages' thumbs waiting for their images
    saved = out["saved"]
    # the pill says «يتم الحفظ…» from the first key (as Google Docs), through the pause and the PUT
    assert saved["beforePause"] == {"state": "dirty", "pill": "يتم الحفظ…", "timer": 1, "puts": 0}
    assert (
        saved["url"] == "/api/books/1/chapters/h10/" and saved["putVersion"] == "v1" and saved["p13"] is True
    )
    assert saved["texts"][:3] == [11, 80, 60] and saved["relayoutGets"] == ["/api/books/1/relayout/7/?wait=2"]
    assert saved["pageCount"] == 6 and saved["revision"] == 4 and saved["numbers"] == [1, 2, 3, 4, 5, 6]
    assert [s[:2] for s in saved["sides"]] == [
        [1, "left"],
        [2, "right"],
        [3, "left"],
        [4, "right"],
        [5, "left"],
        [6, "right"],
    ]
    assert (
        saved["sides"][4][2] == "5"
        and saved["sides"][5][2] == "6"
        and saved["p5x"] == pytest.approx(62.36 - 11.34)
    )
    assert (
        saved["chapterDeltas"] == {"h10": 1}
        and saved["delta"] == 1
        and saved["footprint"] == ["6 صفحات", "+1"]
    )
    assert (
        saved["version"] == "v2"
        and saved["state"] == "saved"
        and saved["pill"] == "تم الحفظ"
        and saved["relayout"] == ""
    )
    assert (
        saved["stillOpen"] == "p13"
        and saved["editors"] == 1
        and saved["current"] == 2
        and saved["numbersAsked"] == 1
        and saved["laid"] is True
    )
    assert saved["thumbs"] == [
        ["1", False],
        ["2", True],
        ["3", True],
        ["4", True],
        ["5", False],
        ["6", False],
    ]
    assert out["later"] == {"current": 6, "side": "right", "painted": "6"}
    # --- a 409: the banner and the pill; «إعادة التحميل» takes the other window's text and version, closes
    # the
    # paragraph and lays the chapter out again
    assert out["conflict"] == {
        "open": True,
        "version": "v9",
        "state": "conflict",
        "pill": "تغيّر في نافذة أخرى",
    }
    reloaded = out["reloaded"]
    assert (
        reloaded["open"] is False and reloaded["version"] == "v9" and reloaded["p13"] == "نص النافذة الأخرى"
    )
    assert reloaded["editorClosed"] is True and reloaded["openNow"] is None and reloaded["relayoutPosts"] == 1
    # --- the open paragraph grows to four lines: every body line under it moves by 44.2 pt, the one pushed
    # past
    # the body (the footnote rule) is hidden; its own lines hidden; the note stays
    grown = out["grown"]
    assert grown["block"] == "p12" and grown["top"] == f"{199.84 - 52.69:.2f}"
    assert grown["lines"] == [
        ["h10", False, "0", False],
        ["p11", False, "0", False],
        ["p11", False, "0", False],
        ["p12", True, "0", False],
        ["p12", True, "0", False],
        ["p13", False, "44.2", False],
        ["p13", False, "44.2", False],
        ["p13", False, "44.2", True],
        ["n12", False, "0", False],
    ]
    # --- Enter at the 5th character of p11: two blocks (the second opens at its start), the first half
    # drawn by
    # the browser in its place until the re-layout, the new paragraph under it; one PUT
    split = out["split"]
    assert (
        split["e2closed"] is True
        and split["e3block"] is True
        and split["e3offset"] == 0
        and split["ids"] == 6
    )
    assert split["first"] == 5 and split["second"] == 75 and split["undo"] is True and split["puts"] == 1
    assert (
        split["patch"] is True
        and split["patchHtml"] == '<p class="ed-p">أهل ب</p>'
        and split["patchCls"] == "lp-patch is-body is-static"
    )
    assert split["patchTop"] == f"{155.64 - 52.69:.2f}" and split["hidden"] == [True, True]
    assert float(split["newTop"]) == pytest.approx(
        float(split["patchTop"]), abs=0.02
    )  # the patch measures 0 here
    # Backspace at the start of the second half: one block again, the caret at the seam; the chapter's undo
    assert out["merged"] == {"block": "p11", "offset": 5, "count": 5, "text": 80}
    assert out["history"] == {"afterUndo": 6, "afterRedo": 5, "canUndo": True, "canRedo": False}
    # ↓ past the last line: the next block, the caret on its first line under the same x
    assert out["down"] == {"block": "p12", "calls": [["placeAtX", 420, "first"]]}
    # the style picker restyles the open paragraph; «ابدأ صفحة جديدة» is saved at once
    styled = out["styled"]
    assert (
        styled["calls"] == [["style", "heading2"]]
        and styled["label"] == "عنوان فرعي"
        and styled["cls"] == "lp-patch is-h2 is-open"
    )
    assert styled["flag"] is True and styled["put"] is True and styled["block"] == ["p12", True, [2, 3]]
    # a click on the call opens the note's small editor (its paragraph opened first), numbered as printed
    assert out["note"] == {
        "pop": "note",
        "note": "n12",
        "number": "1",
        "open": "p12",
        "notes": 1,
        "content": [{"type": "text", "text": "حاشية"}],
    }
    assert out["noteEdit"] == [["note", "n12", [{"type": "text", "text": "حاشية معدّلة"}]]]
    assert out["inserted"] == {"pop": "note", "id": "nek1", "calls": 1}
    # Esc: the paragraph closes, then edit mode (the panel back on its preview tab)
    assert out["escape"] == {
        "escOnce": {"open": None, "mode": "edit", "destroyed": True},
        "mode": "preview",
        "tab": "format",
    }
    # --- the keyboard: ← turns (RTL), E toggles the mode, ⌘Z undoes in edit mode, a field keeps its keys
    assert out["keys"] == {"keyNext": 3, "keyMode": "edit", "keyUndo": True, "field": "edit"}
    # --- the uncertain words: by chapter and page; a pick turns to its page and lights the word; a reading
    # chosen on the server with the chapter's version (its edits saved first); a 409 retried with the new one
    unc = out["uncertain"]
    assert unc["count"] == 2 and unc["groups"] == [["h10", 2, [2, 3]]] and unc["current"] == 2
    assert (
        unc["deco"] == [{"start": 4, "end": 8, "cls": "lp-picked"}]
        and unc["picked"] is True
        and unc["html"] is True
    )
    chosen = out["chosen"]
    assert chosen["url"] == "/api/books/1/uncertain/choose/" and chosen["count"] == 1 and chosen["left"] == 1
    assert chosen["body"] == {
        "chapter": "h10",
        "block": "p11",
        "note": None,
        "start": 4,
        "end": 8,
        "word": "برقة",
        "version": "v2",
        "engine": "secondary",
    }
    assert out["retried"] == ["v1", "v7"]
    # --- find in the chapter: every match with its page (a note's too); Enter goes to the next one, lit
    find = out["find"]
    assert find["results"] == [
        ["p11", None, 4, 2],
        ["p12", None, 1, 2],
        ["p12", "n12", 9, 2],
        ["p13", None, 68, 2],
        ["p13", None, 143, 2],
    ]
    assert (
        find["idx0"] == 0
        and find["idx1"] == 1
        and find["flash"] == {"n": 2, "block": "p12", "i": 3}
        and find["count"] == "2 من 5"
    )
    assert set(find["deco"]) == {"p11", "p12", "n12", "p13"}
    # in the book: the server's matches grouped by chapter
    assert out["findBook"]["groups"] == [["h10", 1], ["h20", 1]] and out["findBook"]["text"] == "2 في الكتاب"
    assert out["findBook"]["post"] == {
        "query": "برقة",
        "replacement": "",
        "replace": False,
        "match_tashkeel": False,
        "fold_alef": True,
        "whole_word": False,
    }
    # --- the stylesheet: one PUT; the page setup changed → the chapter under the eyes laid out again (the
    # whole
    # book: the pages shown fetched again from the new revision)
    sheet = out["sheet"]
    assert sheet["puts"] == [["/api/books/1/stylesheet/", {"body_size_pt": 14}]] and sheet["posts"] == [
        "/api/books/1/chapters/h10/relayout/"
    ]
    # (the six pages held after the splice above: the pages around the one shown, 1–6, from revision 5)
    assert (
        sheet["gets"] == ["/api/books/1/relayout/9/?wait=2", "/api/books/1/preview/layout/?from=1&to=6"]
        and sheet["pill"] == "saved"
        and sheet["after"] == [5, 5]
    )
    # --- a newer live revision (the book render adopted): the pages shown fetched again
    assert out["adopted"] == {"revision": 6, "gets": ["/api/books/1/preview/layout/?from=1&to=5"]}
    # --- a double-click in preview: edit mode with the paragraph open at the point
    # the find match inside the opened paragraph is drawn by its editor (its lines are hidden), once
    assert out["dbl"] == {
        "mode": "edit",
        "tab": "source",
        "opened": 1,
        "block": "p11",
        "offset": 6,
        "find": [["find", [{"start": 4, "end": 8, "current": True}]]],
    }
    # --- a proofreader: preview only, nothing saved
    assert (
        out["reader"]["mode"] == "preview"
        and out["reader"]["refused"] is False
        and out["reader"]["puts"] == 0
    )
    assert out["reader"]["toast"] == "التحرير لمحرّر الكتاب؛ يمكنك تصفّح الصفحات."
    assert scale > 1


# ---------------------------------------------------------------- the review's fixes (D47 UI), under Node
# The component harness above (its tiny DOM, the scripted server, the stubbed one-block editor) with its own
# scenario: stale layout answers, the refetch path, the checks after a splice, Esc and the keys behind layers,
# bidi of the counter, «الفقرة» in preview, a word picked in edit mode, the contents fallback, the body clip.

REVIEW_SCENARIO = r"""
const out = {};
(async () => {
  // ---- an answer older than the pages held (a splice landed while it was on the wire) never replaces them
  const v = mk();
  await settle();
  server.revision = 2;
  const stale = await v.refetchShown();
  out.stale = { accepted: stale, revision: v.revision, pages: v.ctx().pages.size };
  server.revision = 3;

  // ---- the page checks after a splice: the replaced pages' (old 2–3) go, later ones move by the delta
  v.checks = [{ code: 'a', page: 1 }, { code: 'b', page: 3 }, { code: 'c', page: 4 }, { code: 'd', page: 5 }, { code: 'e', page: null }];
  const fresh = fixture.relaid.map((p) => ({ ...p, url: null, url2x: null }));
  const relaid = (id, result = {}) => ({ id, status: 'done', chapter: 'h10', url: `/api/books/1/relayout/${id}/`, error: '', pages: fresh,
    result: Object.assign({ mode: 'chapter', full: false, from: 2, to: 3, count: 3, delta: 1, shifted_from: 4, flip: true, side_shift_pt: -11.34, page_count: 6,
      chapters: [{ id: 'p1', first: 1, last: 1 }, { id: 'h10', first: 2, last: 4 }, { id: 'h20', first: 5, last: 6 }], checks: [{ code: 'n', page: 4 }], revision: { before: 3, after: 4 } }, result) });
  v.applyRelayoutPayload(relaid(20));
  out.checks = v.checks.map((c) => [c.code, c.page]);

  // ---- the counter of a spread: the range isolated left to right
  v.setSpread(true); v.showPage(2, { instant: true });
  out.counter = v.counterText;
  v.setSpread(false);

  // ---- a whole-book re-layout: the edited paragraph's patch stays until the new pages are drawn
  v.ctx().pages = new Map(fixture.pages.map((p) => [p.n, p])); v.revision = 3; v.pageCount = 5; v.rebuildSequence();
  v.showPage(2, { instant: true });
  v.setMode('edit'); await settle();
  v.onSheetClick(lineTarget(v, 'right', 6, 0, 10), 'right'); await settle();
  const ed = editors[editors.length - 1];
  out.clip = { top: v._dom.sheets.right.host.style['--clip'], bottom: v._dom.sheets.right.host.style['--clip-b'] };
  server.status = (id) => ({ id, status: 'done', url: `/api/books/1/relayout/${id}/`, result: { full: true, revision: { before: 3, after: 4 } }, pages: [] });
  const seen = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    if (String(url).includes('/preview/layout/')) seen.push(v.ctx().laid === v.ctx().nodes);
    return realFetch(url, init);
  };
  server.revision = 4;
  ed.type('نص جديد للفقرة');
  await fire(500); await settle();
  globalThis.fetch = realFetch;
  out.refetch = { laidDuringFetch: seen, laidAfter: v.ctx().laid === v.ctx().nodes, revision: v.revision, open: v.open && v.open.block };
  server.status = null;

  // ---- Esc: a menu open outside the component (the top bar's «⋯») takes it alone
  const menu = el('div', 'menu'); menu.getClientRects = () => [1];
  const realQuery = globalThis.document.querySelector;
  globalThis.document.querySelector = (sel) => (sel === '[data-book-menu]' ? menu : realQuery(sel));
  const prevented = [];
  v.onKey({ key: 'Escape', target: {}, preventDefault() { prevented.push('menu'); } }); await settle();
  const withMenu = { open: v.open && v.open.block, mode: v.mode, prevented: prevented.length };
  menu.style.display = 'none';
  v.onKey({ key: 'Escape', target: {}, preventDefault() {} }); await settle();
  out.escMenu = { withMenu, afterClose: [v.open, v.mode] };
  globalThis.document.querySelector = realQuery;

  // ---- a dialog open: the page's keys wait behind it; Esc closes the dialog only
  drop(); v.showPage(2, { instant: true });
  v.openSheet();
  v.onKey({ key: 'ArrowLeft', target: {}, preventDefault() {} }); await settle();
  v.onKey({ key: 'e', code: 'KeyE', target: {}, preventDefault() {} }); await settle();
  const behind = { current: v.current, mode: v.mode, sheet: v.sheetOpen };
  v.onKey({ key: 'Escape', target: {}, preventDefault() {} }); await settle();
  out.dialog = { behind, closed: !v.sheetOpen, mode: v.mode };

  // ---- the fit keys by their place (an Arabic keyboard types «١»)
  const K = globalThis.NassakhBook.geo.keyAction;
  // D69: the fits are + − 0 by their place (the Arabic layout's «٠»), ⇧ with a digit key is a symbol
  out.fitKeys = [K({ key: '٠', code: 'Digit0' }, { mode: 'preview' }), K({ key: '+', code: 'Equal', shiftKey: true }, { mode: 'preview' }), K({ key: '-', code: 'Minus' }, { mode: 'preview' }), K({ key: ')', code: 'Digit0', shiftKey: true }, { mode: 'preview' }), K({ key: '١', code: 'Digit1' }, { mode: 'preview' })];

  // ---- the contents line: a heading whose page is not held yet goes to its printed page number
  v.ranges = v.ranges.filter((c) => c.id !== 'hx');
  out.contents = v.goToBlock('hx', { runs: [{ text: 'الفصل' }, { text: '4' }] });
  await fire(200);
  out.contentsPage = v.current;

  // ---- a word picked in edit mode: its page now (not the list's stale one), its paragraph open, the word selected
  drop(); v.setMode('edit'); await settle(); v.showPage(4, { instant: true });
  const word = Object.assign({}, fixture.uncertain[0], { page: 5 });
  v.uncertain.items = [word];
  await v.pickUncertain(word); await settle();
  const eW = editors[editors.length - 1];
  out.picked = { current: v.current, open: v.open && v.open.block, calls: eW.calls.filter((c) => c[0] === 'setOffset').slice(-1) };
  // the style menu and the word's overlay never show together
  v.styleMenu = true;
  eW.opts.onUncertain({ from: 5, to: 9, start: 4, end: 8, text: 'برقة', dom: el('mark', 'ed-uncertain') });
  out.oneOverlay = { pop: v.pop.kind, styleMenu: v.styleMenu };
  v.closePop();
  // «الفقرة»'s flags act on the paragraph it shows, once that paragraph is closed too
  await v.closeBlock({ commit: false }); await settle();
  calls.length = 0;
  const shownBlock = v.blockInfo && v.blockInfo.id;
  v.toggleFlag('keepWithNext'); await settle();
  const flagPut = calls.filter((c) => c[0] === 'PUT').pop();
  out.flagClosed = { shownBlock, put: Boolean(flagPut) && convert.locate(flagPut[2].content.content, 'p11').node.attrs.keepWithNext, shown: v.blockInfo && v.blockInfo.keepWithNext };
  v.setMode('preview'); await settle();

  // ---- «الفقرة» for a proofreader: the paragraph clicked in preview, from its chapter's nodes
  // (the server is at revision 4 now, the page embeds revision 3: the window fetched ahead finds the newer
  // one, and the pages shown are fetched again from it — never left as skeletons)
  drop();
  const r = mk({ canEdit: false });
  await settle();
  r.showPage(2, { instant: true });
  r.setTab('block');
  r.onSheetClick(lineTarget(r, 'right', 6, 0, 10), 'right'); await settle();
  out.reader = { block: r.blockInfo && [r.blockInfo.id, r.blockInfo.style, r.blockInfo.pages], mode: r.mode, editChapter: r.editChapterId, revision: r.revision, held: [...r.ctx().pages.keys()].sort() };
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


def test_book_page_review_fixes_under_node(tmp_path):
    folder = _node_tmp(tmp_path)
    fixture = _component_fixture()
    (folder / "fixture.json").write_text(json.dumps(fixture, ensure_ascii=False))
    base = COMPONENT_HARNESS.split("\nconst out = {};\n")[0]
    out = _run_node(folder, "review.mjs", base + REVIEW_SCENARIO, str(ROOT), str(folder / "fixture.json"))
    # an older layout answer is ignored: the pages held stay
    assert out["stale"] == {"accepted": False, "revision": 3, "pages": 5}
    # the checks of the replaced pages 2–3 go (not those of the old page 4, which moves to 5), the ones after
    # them move by +1, the book-wide one goes; the new pages' checks come with the answer
    assert out["checks"] == [["a", 1], ["c", 5], ["d", 6], ["n", 4]]
    # «الصفحتان 2–3» with the range isolated (an RTL line would print «3–2»)
    assert out["counter"] == "الصفحتان ⁦2–3⁩ من 6"
    # the edit host ends at the body's foot (the footnote rule on this page): the open box never covers notes
    page2 = fixture["pages"][1]
    assert float(out["clip"]["bottom"]) == pytest.approx(
        page2["height_pt"] - page2["footnote_rule"]["y"], abs=0.01
    )
    # a whole-book re-layout: while the new pages are on the wire the page still shows the patch (the text
    # laid out is the old one); once drawn, the saved text is the laid one; the paragraph stays open
    assert out["refetch"]["laidDuringFetch"] and not any(out["refetch"]["laidDuringFetch"])
    assert out["refetch"]["laidAfter"] is True and out["refetch"]["revision"] == 4
    assert out["refetch"]["open"] == "p13"
    # Esc with the top bar's menu open closes only the menu; the next Esc closes the paragraph
    assert out["escMenu"]["withMenu"] == {"open": "p13", "mode": "edit", "prevented": 0}
    assert out["escMenu"]["afterClose"] == [None, "edit"]
    # behind the shortcut sheet no key turns a page or switches the mode; Esc closes the sheet alone
    assert out["dialog"] == {
        "behind": {"current": 2, "mode": "edit", "sheet": True},
        "closed": True,
        "mode": "edit",
    }
    assert out["fitKeys"] == ["fitHeight", "fitIn", "fitOut", None, None]
    assert out["contents"] is True and out["contentsPage"] == 4
    # the word's page from the layout held (2), not the list's (5); its paragraph open with the word selected
    assert out["picked"] == {"current": 2, "open": "p11", "calls": [["setOffset", 4]]}
    assert out["oneOverlay"] == {"pop": "word", "styleMenu": False}
    assert out["flagClosed"] == {"shownBlock": "p11", "put": True, "shown": True}
    # a proofreader clicks a paragraph in preview: «الفقرة» describes it (nothing is loaded for editing)
    assert out["reader"] == {
        "block": ["p13", "paragraph", [3]],
        "mode": "preview",
        "editChapter": None,
        "revision": 4,
        "held": [1, 2, 3, 4, 5],
    }


# ---------------------------------------------------------------- fixes found testing the page in Chrome
# (2026-09-25, book 18): the chapters list painted from the component root, the scan page label in the margin,
# the open paragraph cut on a whole line at the body's foot, the caret followed onto the next page, the edit
# layer never scrolled, and a conflict reload fetching the pages again.

CHROME_SCENARIO = r"""
const out = {};
(async () => {
  // ---- «الفصول»: the list is painted although the call comes from the tab button (Alpine's `$el` there)
  const c = mk();
  await settle();
  c.$el = el('button', 'bp-tab');
  c.setTab('chapters');
  out.chapterList = c._dom.list.innerHTML.includes('bp-ch');
  drop();

  // ---- a scan page mark: the hairline stays in the text, its «ص N» label goes on the line (the margin)
  const G = globalThis.NassakhBook.geo;
  const page2 = fixture.pages[1];
  const line = page2.lines[5];
  const html = G.lineHtml(line, 5, { marks: (b) => (b === line.block ? [{ offset: line.start + 3, page: 7 }] : []) });
  out.pbLine = { hasClass: /class="[^"]*has-pb/.test(html), attr: /data-pb="7"/.test(html), mark: html.includes('class="lp-pb"') };
  out.pbNone = /has-pb|data-pb/.test(G.lineHtml(line, 5, {}));

  // ---- p13 runs from page 2 onto page 3: open it on page 2
  const v = mk();
  await settle();
  v.showPage(2, { instant: true });
  v.setMode('edit'); await settle();
  v.onSheetClick(lineTarget(v, 'right', 5, 0, 10), 'right'); await settle();
  const ed = editors[editors.length - 1];
  const host = v._dom.sheets.right.host;
  // the edit layer never scrolls (a browser scrolls even a clipped box to show the caret)
  host.scrollTop = 40;
  if (host.listeners.scroll) host.listeners.scroll();
  out.pinned = { listener: typeof host.listeners.scroll, scrollTop: host.scrollTop };
  // taller than the body: the cut falls on a whole line of the paragraph, above the footnote rule
  ed.grow(24);
  v.paint();
  const box = v.ctx().box;
  const clipB = Number(host.style['--clip-b']);
  const visibleBottom = page2.height_pt - clipB;
  const top = Number(box.style['--top']) + Number(host.style['--clip']);
  const lh = Number(box.style['--lh']);
  out.cut = { visibleBottom, rule: page2.footnote_rule.y, lines: (visibleBottom - top) / lh, lh };
  // a footnote call is drawn at the book stylesheet's size: 0.62 of the body text
  out.call = { fs: Number(box.style['--call-fs']), body: Number((v.sheet || {}).body_size_pt) || 13 };
  ed.grow(3); v.paint();
  out.cutShort = Number(host.style['--clip-b']);
  // the caret moved (↓) onto the part the engine put on page 3: the view turns there, anchored on that line
  ed.at = 160;
  ed.opts.onSelection(); await settle();
  out.follow = { current: v.current, anchor: v.ctx().anchor && v.ctx().anchor.n, open: v.open && v.open.block };
  // while typing, the offsets run ahead of the layout: no turn (the re-layout turns the page)
  v.showPage(2, { instant: true, keepOpen: true }); await settle();
  ed.at = 10; ed.opts.onSelection(); await settle();
  const before = v.current;
  ed.type('نص مكتوب الآن لم تُرتَّب صفحاته بعد');
  ed.at = 20; ed.opts.onSelection(); await settle();
  out.typing = { before, after: v.current };
  drop();

  // ---- a conflict answered with the other window's text: the pages on screen are fetched again
  const w = mk();
  await settle();
  w.showPage(2, { instant: true });
  w.setMode('edit'); await settle();
  let refetched = 0;
  const realRefetch = w.refetchShown;
  w.refetchShown = function () { refetched += 1; return realRefetch.call(this); };
  w.conflict = { open: true, version: 'v9', content: { type: 'doc', content: clone(server.docs.h10) } };
  w.reloadConflict(); await settle();
  out.reload = { refetched, open: w.conflict.open, version: w.version };
  drop();

  // ---- a #page-N link opened in the same tab turns to that page; an unknown page says so
  const h = mk();
  await settle();
  h.showPage(1, { instant: true });
  location.hash = '#page-3'; h.onHashChange(); await fire(200); await settle();
  const turned = h.current;
  const toastsBefore = toasts.length;
  location.hash = '#page-99'; h.onHashChange(); await fire(200); await settle();
  out.hash = { turned, stayed: h.current, toast: toasts.slice(toastsBefore).pop() || null };
  location.hash = '';

  // ---- a dialog opened from the «⋯» menu gives the focus back to the menu's button (its item is hidden)
  const anchor = el('div', 'menu-anchor');
  const opener = anchor.appendChild(el('button', 'btn-icon', { name: 'more' }));
  opener.setAttribute('aria-haspopup', 'menu');
  const menu = anchor.appendChild(el('div', 'menu'));
  menu.setAttribute('role', 'menu');
  const item = menu.appendChild(el('button', 'menu-item', { name: 'digits-item' }));
  document.activeElement = item;
  h.openDigits(); await settle();
  h.closeDigits(); await settle();
  out.trigger = focused[focused.length - 1];
  document.activeElement = null;
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


# ---------------------------------------------------------------- round-trip safety (D70) and the keys (D69)

ROUNDTRIP_SCENARIO = r"""
const out = {};
const realNow = Date.now; let clock = realNow(); Date.now = () => clock;
const later = () => { clock += 2100; };
const driftGets = () => gets().filter((u) => u === '/api/books/1/drift/').length;
const posts = () => calls.filter((c) => c[0] === 'POST');
(async () => {
  const urls = { ...fixture.config.urls, drift: '/api/books/1/drift/' };
  // ---- the live drift: the review screen's message for this book refreshes it (another book's is ignored)
  server.drift = { edited: true, pages: [3], chapters: ['h10'] };
  const d = mk({ urls, origin: 'editor' }); await settle();
  d.showPage(2, { instant: true }); await settle();
  const ch = channels[channels.length - 1];
  out.before = { banner: d.driftChapter, channel: ch.name };
  calls.length = 0;
  ch.onmessage({ data: { type: 'review', book: 2, page: 3 } }); await settle();
  const otherBook = driftGets();
  ch.onmessage({ data: { type: 'review', book: 1, page: 3 } }); await settle();
  out.message = { otherBook, gets: driftGets(), drift: clone(d.drift), banner: d.driftChapter, text: d.driftText, summary: d.summaryOf('h10').drift };
  // inside 2 s a trigger waits for the window's end: one trailing request for a burst
  calls.length = 0;
  d.onWindowFocus(); d.onVisible(); d.onPageShow({ persisted: true });
  out.throttled = { gets: driftGets(), trailing: Boolean(d.ctx().timers.drift) };
  later(); await d.ctx().timers.drift && pending().length; drop(); d.ctx().timers.drift = null;
  // each trigger on its own, after the window: focus, visibility, a return from the back-forward cache
  const each = {};
  for (const [name, fn] of [['focus', () => d.onWindowFocus()], ['visible', () => d.onVisible()], ['pageshow', () => d.onPageShow({ persisted: true })], ['pageshowFresh', () => d.onPageShow({ persisted: false })]]) {
    later(); calls.length = 0; fn(); await settle(); each[name] = driftGets();
  }
  out.each = each;
  // «الاحتفاظ بالنص» hides the banner until other pages change in review
  d.dismissDrift();
  const dismissed = d.driftChapter;
  server.drift = { edited: true, pages: [2, 3], chapters: ['h10'] };
  later(); ch.onmessage({ data: { type: 'review', book: 1, page: 2 } }); await settle();
  out.dismiss = { dismissed, back: d.driftChapter };
  // no drift any more (the page approved without a text change, D70): the banner goes
  server.drift = { edited: true, pages: [], chapters: [] };
  later(); d.onWindowFocus(); await settle();
  out.cleared = { banner: d.driftChapter, summary: d.summaryOf('h10').drift };
  // ---- «إعادة بناء الفصل من المراجعة…» over an edited text: the dialog first, «إلغاء» posts nothing,
  // «استبدال الفصل» posts `replace_edited`
  server.drift = { edited: true, pages: [3], chapters: ['h10'] };
  later(); d.onWindowFocus(); await settle();
  calls.length = 0;
  d.askReassemble(); await settle();
  out.ask = { open: d.rebuild.open, chapter: d.rebuild.chapter, title: d.rebuild.title, posts: posts().length, layer: d.topLayer() };
  const turnBehind = d.current; d.onKey({ key: 'ArrowLeft', code: 'ArrowLeft', target: el('div'), preventDefault() {} }); await settle();
  out.behind = d.current === turnBehind;
  d.escape(); out.escaped = d.rebuild.open;
  d.askReassemble(); await d.confirmRebuild(); await settle();
  out.confirm = { post: posts().map((c) => [c[1], c[2]]), running: d.reassembly.running, open: d.rebuild.open };
  // an unedited text is rebuilt at once without the flag; a 409 (edited meanwhile, another tab) opens the dialog
  const u = mk({ urls, origin: 'assembly', drift: { edited: false, pages: [3], chapters: ['h10'] } }); await settle();
  u.showPage(2, { instant: true }); await settle();
  server.reassemble = () => reply(409, { detail: 'حُرِّر نص هذا الفصل في «الكتاب»', edited: true });
  calls.length = 0;
  u.askReassemble(); await settle();
  out.unedited = { post: posts().map((c) => [c[1], c[2]]), open: u.rebuild.open, edited: u.drift.edited };
  server.reassemble = null;
  // ---- D69 through onKey: V the spread, S the page marks (a toast in preview), + − 0 the fits; the Arabic layout
  const k = mk({ urls }); await settle();
  const press = (key, code, extra = {}) => k.onKey(Object.assign({ key, code, target: el('div'), preventDefault() {} }, extra));
  const spread0 = k.spread; press('ر', 'KeyV'); const spread1 = k.spread;
  const marks0 = k.pageMarks; toasts.length = 0; press('س', 'KeyS');
  out.keys = { spread: [spread0, spread1], marks: [marks0, k.pageMarks], toast: toasts.slice(-1)[0] };
  press('+', 'Equal', { shiftKey: true }); const f1 = k.fit; press('=', 'Equal'); const f2 = k.fit; press('=', 'Equal'); const f3 = k.fit;
  press('-', 'Minus'); const f4 = k.fit; press('٠', 'Digit0'); const f5 = k.fit; press('٢', 'Digit2'); const f6 = k.fit;
  out.fits = [f1, f2, f3, f4, f5, f6];
  Date.now = realNow;
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


def test_book_page_round_trip_safety_and_keys_under_node(tmp_path):
    folder = _node_tmp(tmp_path)
    fixture = _component_fixture()
    (folder / "fixture.json").write_text(json.dumps(fixture, ensure_ascii=False))
    base = COMPONENT_HARNESS.split("\nconst out = {};\n")[0]
    out = _run_node(
        folder, "roundtrip.mjs", base + ROUNDTRIP_SCENARIO, str(ROOT), str(folder / "fixture.json")
    )
    banner = {"id": "h10", "title": "الفصل الأول", "pages": [3]}
    # the config's drift (none) until the review screen's message; then the banner, from live state
    assert out["before"] == {"banner": None, "channel": "nassakh"}
    assert out["message"] == {
        "otherBook": 0,
        "gets": 1,
        "drift": {"edited": True, "pages": [3], "chapters": ["h10"], "reasons": {}, "approvals": []},
        "banner": banner,
        "text": "تغيّر نص صفحة واحدة من هذا الفصل في المراجعة بعد التحرير:",
        "summary": True,
    }
    assert out["throttled"] == {"gets": 0, "trailing": True}
    assert out["each"] == {"focus": 1, "visible": 1, "pageshow": 1, "pageshowFresh": 0}
    assert out["dismiss"] == {
        "dismissed": None,
        "back": {"id": "h10", "title": "الفصل الأول", "pages": [2, 3]},
    }
    assert out["cleared"] == {"banner": None, "summary": False}
    # the rebuild asks first; the keys behind it wait; Esc closes it; the confirmation posts the flag
    assert out["ask"] == {
        "open": True,
        "chapter": "h10",
        "title": "الفصل الأول",
        "posts": 0,
        "layer": "rebuild",
    }
    assert out["behind"] is True and out["escaped"] is False
    assert out["confirm"]["post"] == [["/api/books/1/chapters/h10/reassemble/", {"replace_edited": True}]]
    assert out["confirm"]["running"] is True and out["confirm"]["open"] is False
    assert out["unedited"] == {
        "post": [["/api/books/1/chapters/h10/reassemble/", {}]],
        "open": True,
        "edited": True,
    }
    # D69: V, S and the fits by the key's place (Arabic layout)
    assert out["keys"] == {
        "spread": [False, True],
        "marks": [True, False],
        "toast": "أُخفيت فواصل الصفحات الأصلية",
    }
    assert out["fits"] == ["width", "actual", "actual", "width", "height", "height"]


def test_book_page_rebuild_dialog_and_moved_keys_in_the_templates(editor):
    body, _config = _page(_logged(editor), _book())
    dialog = _between(body, "data-rebuild-dialog", "</div>\n</div>")
    assert (
        'role="alertdialog"' in body
        and 'إعادة بناء الفصل «<span x-text="rebuild.title"></span>» من المراجعة؟' in body
    )
    assert (
        "يُستبدل نص الفصل كله بنص صفحاته في المراجعة، فتضيع تعديلاته في الكتاب: العناوين والحواشي والكلمات."
        " تبقى منه نسخة في «نسخة محفوظة»." in dialog
    )
    assert dialog.index('x-ref="rebuildCancel"') < dialog.index("استبدال الفصل")
    assert 'class="btn btn-danger"' in dialog and '@click="confirmRebuild()"' in dialog
    assert body.count("إعادة بناء الفصل من المراجعة…") == 2 and "إعادة تجميع الفصل من المراجعة" not in body
    # D78: the banner offers «عرض التغييرات» and «لاحقًا»; the rebuild is the menu's and the tab's second way
    assert body.count('@click="askReassemble()"') == 2 and "الاحتفاظ بالنص" not in body
    assert "عرض التغييرات" in body and "لاحقًا" in body
    assert '@focus.window="onWindowFocus()"' in body and '@pageshow.window="onPageShow($event)"' in body
    # D69: the toolbar titles and the sheet name the moved keys, Latin capitals that work on the Arabic layout
    assert body.count('title="صفحة واحدة أو صفحتان (V)"') == 2
    assert body.count('title="ملء الارتفاع (0) · تكبير (+) · تصغير (−)"') == 3
    assert 'title="فواصل الصفحات الأصلية (S)"' in body
    sheet = _between(body, 'id="bp-sheet-title"', "</dl>")
    assert '<kbd class="kbd">V</kbd></dt><dd>صفحة واحدة أو صفحتان' in sheet
    assert '<kbd class="kbd">S</kbd></dt><dd>فواصل الصفحات الأصلية' in sheet
    assert '<kbd class="kbd">0</kbd> <kbd class="kbd">+</kbd> <kbd class="kbd">−</kbd>' in sheet
    assert '<kbd class="kbd">1</kbd>' not in sheet
    assert "تعمل الاختصارات بلوحة المفاتيح العربية أيضًا: المفتاح نفسه في مكانه." in body


def test_book_page_chrome_fixes_under_node(tmp_path):
    folder = _node_tmp(tmp_path)
    fixture = _component_fixture()
    (folder / "fixture.json").write_text(json.dumps(fixture, ensure_ascii=False))
    base = COMPONENT_HARNESS.split("\nconst out = {};\n")[0]
    out = _run_node(folder, "chrome.mjs", base + CHROME_SCENARIO, str(ROOT), str(folder / "fixture.json"))
    assert out["chapterList"] is True
    assert out["pbLine"] == {"hasClass": True, "attr": True, "mark": True} and out["pbNone"] is False
    assert out["pinned"] == {"listener": "function", "scrollTop": 0}
    # the open paragraph shows whole lines only, ending above the footnote rule, less than a line above it
    cut = out["cut"]
    assert cut["visibleBottom"] <= cut["rule"] + 1e-6 and cut["rule"] - cut["visibleBottom"] < cut["lh"]
    assert abs(cut["lines"] - round(cut["lines"])) < 1e-6 and round(cut["lines"]) >= 1
    # a paragraph that fits cuts at the body's foot as before
    page2 = fixture["pages"][1]
    assert out["cutShort"] == pytest.approx(page2["height_pt"] - page2["footnote_rule"]["y"], abs=0.01)
    assert out["call"]["fs"] == pytest.approx(out["call"]["body"] * 0.62, abs=0.01)
    assert out["follow"] == {"current": 3, "anchor": 3, "open": "p13"}
    assert out["typing"]["after"] == out["typing"]["before"]
    assert (
        out["reload"]["refetched"] >= 1
        and out["reload"]["open"] is False
        and out["reload"]["version"] == "v9"
    )
    assert out["hash"] == {"turned": 3, "stayed": 3, "toast": "لا صفحة بهذا الرقم"}
    assert out["trigger"] == "more"


def test_compiled_css_modal_scrolls_page_labels_and_open_bar():
    css = (ROOT / "static" / "dist" / "app.css").read_text(encoding="utf-8")
    modal = re.search(r"\.rv-modal\{([^}]*)\}", css)
    assert modal and "max-height:calc(88dvh - 16px)" in modal.group(1) and "overflow-y:auto" in modal.group(1)
    label = re.search(r"\.lp-line\.has-pb:after\{([^}]*)\}", css)
    assert label and "attr(data-pb)" in label.group(1) and "inset-inline-end" in label.group(1)
    bar = re.search(r"\.lp-patch\.is-open:before\{([^}]*)\}", css)
    assert bar and "width:2px" in bar.group(1)
    # the editor sets a footnote call and a page hairline as the engine does (a call's size, no hairline room)
    call = re.search(r"\.lp-patch \.ed-fn\{([^}]*)\}", css)
    assert call and "font-size:calc(var(--call-fs,8) * var(--u))" in call.group(1)
    hairline = re.search(r"\.lp-patch \.ed-pb:before\{([^}]*)\}", css)
    assert hairline and "margin-inline-end:-1px" in hairline.group(1)
    assert not re.search(r"\.lp-patch\.is-open\{[^}]*box-shadow:0 0 0 calc", css)


def test_compiled_css_keeps_the_editors_caret_hack_inline():
    """The open paragraph is as tall as its text when it ends in a footnote call (owner, 2026-10-03). A block
    whose last child is not editable (a call, a page mark) gets ProseMirror's `<img class="ProseMirror-separator">`
    before its trailing `<br>` in Chrome and Safari; the preflight's `img { display: block }` put the two on a
    line of their own, so the box was a line taller than its text (`flow()` pushed the page's lines down by its
    measured height) and the caret after the call sat on that empty line. Headless Chromium on the built bundle
    and sheet: 3 text lines measured 4 lines, 3 with the rule; an empty paragraph keeps 1 line, a paragraph
    ending in a line break keeps its empty last line. The editors inject no TipTap CSS, so this rule is the only
    one keeping the image inline (as prosemirror.css)."""
    css = (ROOT / "static" / "dist" / "app.css").read_text(encoding="utf-8")
    assert {
        "display:inline!important",
        "border:none!important",
        "margin:0!important",
        "width:0!important",
        "height:0!important",
    } <= _rule(css, "img.ProseMirror-separator")
    # and the trailing break itself is never hidden: an empty paragraph and a block ending in a line break need it
    assert not re.search(r"ProseMirror-trailingBreak[^{]*\{[^}]*display:(?:none|block)", css)
    source = (ROOT / "static" / "src" / "editor" / "index.js").read_text(encoding="utf-8")
    assert source.count("injectCSS: false") == 2


# ---------------------------------------------------------------- the undo toast (UX test, 2026-09-26)


def test_the_undo_toast_never_runs_its_undo_by_being_shown(tmp_path):
    """Showing an undo toast must not undo. Alpine calls a function that a directive's expression evaluates
    to, and the «تراجع» button was shown with `x-show="$store.bookToast && $store.bookToast.action"`: every
    snapshot restore, digit conversion and replace-all was undone the moment its toast appeared. The test
    evaluates the button's own x-show expression the way Alpine does (a function result is called)."""
    if NODE is None:
        pytest.skip("node is not installed")
    overlays = (ROOT / "templates" / "editor" / "_book_overlays.html").read_text(encoding="utf-8")
    match = re.search(r'<button[^>]*x-show="([^"]+)"[^>]*@click="\$store\.bookToast\.run\(\)"', overlays)
    assert match, "the undo button of the book page's toast"
    source = f"""
const {{ readFileSync }} = require('fs');
const stores = {{}};
globalThis.document = {{ addEventListener() {{}} }};
globalThis.Alpine = {{
  data() {{}},
  store: (n, v) => {{ if (v !== undefined) stores[n] = v; return stores[n]; }},
}};
globalThis.window = globalThis;
(0, eval)(readFileSync({json.dumps(str(BOOK_JS / "page.js"))}, 'utf8'));
globalThis.NassakhBook.register();
const toast = stores.bookToast;
let undone = 0;
toast.show('استُعيدت النسخة', () => {{ undone += 1; }});
// Alpine's rule: evaluate the expression; a function result is called with the scope
const alpine = (expr) => {{
  const v = new Function('$store', `return (${{expr}})`)(stores);
  return typeof v === 'function' ? v() : v;
}};
const shown = [alpine({json.dumps(match.group(1))}), alpine({json.dumps(match.group(1))})];
const afterShow = undone;
toast.run();
const afterRun = undone;
toast.show('بلا تراجع');
const noAction = alpine({json.dumps(match.group(1))});
clearTimeout(toast.timer);
const result = {{ shown, afterShow, afterRun, noAction: Boolean(noAction), hasAction: toast.hasAction }};
console.log(JSON.stringify(result));
"""
    out = _run_node(tmp_path, "toast.cjs", source)
    assert out["shown"] == [True, True]  # the button shows ...
    assert out["afterShow"] == 0  # ... without running the undo
    assert out["afterRun"] == 1  # «تراجع» runs it once
    assert out["noAction"] is False and out["hasAction"] is False  # a toast without an undo shows no button


# -------------------------------------------------------- 7c: «تغييرات المراجعة», the address, the note

CONTRACT = ROOT / "editor" / "fixtures" / "contract"


def _contract_all() -> dict:
    return {path.name: json.loads(path.read_text(encoding="utf-8")) for path in CONTRACT.glob("*.json")}


def _pick(fixture: dict, label: str):
    return next(v for k, v in fixture.items() if label in k)


def _pick_key(fixture: dict, label: str) -> str:
    return next(k for k in fixture if label in k)


CHANGES_SCENARIO = r"""
const contract = JSON.parse(readFileSync(process.argv[4], 'utf8'));
const pickOf = (obj, label) => Object.entries(obj).find(([k]) => k.includes(label))[1];
const DR = Object.assign({ 'review only': { edited: true, pages: [1, 2], reasons: { 1: 'review', 2: 'review' }, approvals: [], chapters: ['h40011'], chapter_pages: { h40011: [1, 2] } } }, contract['drift.json']); const CP = contract['changes_plan.json']; const CR = contract['changes_requests.json'];
const NOTE = contract['to_footnote.json'];
const out = {};
// the server gains the 7c answers: the plan (POST 202, then the GETs in turn), the apply, the note, the restore
const baseFetch = globalThis.fetch;
let planGets = []; let planPost = null; let applyAnswer = null; let noteAnswer = null;
globalThis.fetch = async (url, init = {}) => {
  const method = init.method || 'GET';
  const body = init.body ? JSON.parse(init.body) : null;
  if (url === '/api/books/1/review-changes/') {
    calls.push([method, url, body]);
    if (method === 'POST') return reply(planPost.status, planPost.response);
    return reply(200, planGets.length > 1 ? planGets.shift() : planGets[0]);
  }
  if (/^\/api\/books\/1\/review-changes\/\d+\/apply\/$/.test(url)) { calls.push([method, url, body]); return reply(applyAnswer.status, applyAnswer.response); }
  if (url === '/api/books/1/to-footnote/') { calls.push([method, url, body]); return reply(noteAnswer.status, noteAnswer.response); }
  if (/^\/api\/books\/1\/snapshots\/\d+\/restore\/$/.test(url)) { calls.push([method, url, body]); return reply(200, pickOf(CR, 'undo').response); }
  return baseFetch(url, init);
};
const posts = () => calls.filter((c) => c[0] === 'POST').map((c) => [c[1], c[2]]);
const urls = { ...fixture.config.urls, drift: '/api/books/1/drift/', reviewChanges: '/api/books/1/review-changes/', reviewChangesApply: '/api/books/1/review-changes/__pid__/apply/', toFootnote: '/api/books/1/to-footnote/' };
const toastStore = () => stores.bookToast;
(async () => {
  // ---- the banner: book-wide, by reason; «لاحقًا» puts it off until the drift changes
  const b = mk({ urls, drift: pickOf(DR, 'review only') }); await settle();
  const banner = (drift) => { b.applyDrift(drift); return b.driftBanner ? [b.driftBanner.kind, b.driftBanner.text, b.driftBanner.pages, b.driftBanner.more] : null; };
  const drift = (reasons, edited = true) => ({ edited, pages: Object.keys(reasons).map(Number), reasons, approvals: [], chapters: ['h10'] });
  out.banner = {
    review: banner(drift({ 1: 'review', 2: 'review' })),
    processing: banner(drift({ 7: 'processing' })),
    mixed: banner(drift({ 1: 'review', 2: 'review', 3: 'review', 4: 'added', 5: 'removed', 7: 'processing' })),
    added: banner({ edited: true, pages: [91, 92, 93], reasons: { 91: 'added', 92: 'added', 93: 'added' }, chapters: [] }),
    many: banner({ edited: true, pages: [4, 12, 13, 20, 21, 22, 30, 31, 40, 41], reasons: Object.fromEntries([4, 12, 13, 20, 21, 22, 30, 31, 40, 41].map((n) => [n, 'processing'])), chapters: [] }),
    approvals: banner(pickOf(DR, 'approvals only')),
    unedited: banner(drift({ 1: 'review' }, false)),
  };
  // the contract's drifts: an edited book with content drift is announced; approvals, never
  out.contractKinds = Object.fromEntries(Object.entries(contract['drift.json']).map(([k, v]) => [k, banner(v) ? banner(v)[0] : null]));
  b.applyDrift(pickOf(DR, 'review only'));
  b.putOffBanner();
  const later = b.driftBanner;
  b.applyDrift(pickOf(DR, '(every reason)'));
  out.later = { hidden: later, back: Boolean(b.driftBanner), stored: Object.values(session).includes('1:review,2:review') };
  drop();
  // ---- the tab: only with content drift; the address keeps it; opening it posts a plan and polls it
  const done = pickOf(CP, 'done, a stored base');
  const c = mk({ urls, drift: pickOf(DR, 'approvals only') }); await settle();
  out.noTab = { has: c.hasChangesTab, tabs: c.tabs.map((t) => t.key).includes('changes') };
  c.applyDrift(done.drift);
  out.tab = { has: c.hasChangesTab, badge: c.tabs.find((t) => t.key === 'changes').badge, count: c.changesCount };
  planPost = pickOf(CR, '{} (all drift pages)');
  planGets = [pickOf(CP, '(queued)'), pickOf(CP, '(running)'), done];
  calls.length = 0; replaced.length = 0;
  c.setTab('changes'); await settle();
  out.posting = { post: posts()[0], state: c.changes.state, address: replaced.slice(-1)[0], wait: pending(700).length };
  await fire(700); await fire(700);
  out.polled = { gets: calls.filter((x) => x[1] === '/api/books/1/review-changes/' && x[0] === 'GET').length, state: c.changes.state };
  out.rows = c.changesRows.map((r) => [r.number, r.reason, c.reasonLabel(r), r.chapter_title, r.items.map((it) => [it.id, it.kind, it.chip, c.choiceOf(it), it.ask])]);
  out.apply = { label: c.changesApplyLabel, count: c.changesApplyCount };
  // «نصّي | المراجعة» on page 2's conflict: «المراجعة»; the apply sends only the choices that differ from the default
  // (the ids are digests of the items: the two conflicts are page 2's and page 3's deleted paragraph)
  const [I3, I4] = done.plan.items.filter((it) => it.kind === 'conflict').map((it) => it.id);
  const i3 = c.changesItems.get(I3);
  c.setChoice(i3, 'theirs');
  out.defaultBody = c.applyBody(false);
  // «خذ ما جاء من المراجعة في هذه الصفحة» / «أبقِ نصّي» per page, and a page left out
  const row3 = c.changesRows.find((r) => r.number === 3);
  c.setPageChoice(row3, 'theirs');
  const pageTheirs = c.choiceOf(c.changesItems.get(I4));
  c.setPageChoice(row3, 'mine');
  out.page = { theirs: pageTheirs, mine: c.choiceOf(c.changesItems.get(I4)) };
  applyAnswer = pickOf(CR, '(the defaults, the conflict on «المراجعة»)');
  calls.length = 0;
  const applied = await c.applyChanges(false); await settle();
  out.applied = { post: posts().find((p) => p[0].includes('/apply/')), chapters: gets().includes('/api/books/1/chapters/'), toast: toastStore().message, has: toastStore().hasAction, version: applied.version, flash: c.appliedBlocks ? [...c.appliedBlocks].sort() : null, stages: typeof window.NassakhStages };
  calls.length = 0;
  toastStore().run(); await settle();
  out.undo = posts().find((p) => p[0].includes('/restore/'));
  drop();
  // keep-all: the baseline moves, the text is not written
  const k = mk({ urls, drift: done.drift }); await settle();
  planPost = pickOf(CR, '{} (all drift pages)'); planGets = [done];
  k.setTab('changes'); await settle();
  applyAnswer = pickOf(CR, 'keep_all');
  calls.length = 0;
  await k.applyChanges(true); await settle();
  out.keep = { post: posts().find((p) => p[0].includes('/apply/')), toast: toastStore().message };
  drop();
  // one page only: the other rows unticked
  const o = mk({ urls, drift: done.drift }); await settle();
  planPost = pickOf(CR, '{} (all drift pages)'); planGets = [done];
  o.setTab('changes'); await settle();
  o.changesRows.filter((r) => r.number !== 2).forEach((r) => o.togglePage(r));
  applyAnswer = pickOf(CR, '{pages: [2]}');
  calls.length = 0;
  await o.applyChanges(false); await settle();
  out.onePage = posts().find((p) => p[0].includes('/apply/'));
  drop();
  // a stale answer (409): the comparison is made again and the choices of the items that remain are kept
  const s = mk({ urls, drift: done.drift }); await settle();
  planPost = pickOf(CR, '{} (all drift pages)'); planGets = [done];
  s.setTab('changes'); await settle();
  s.setChoice(s.changesItems.get(I3), 'theirs');
  applyAnswer = pickOf(CR, 'stale version');
  calls.length = 0; toasts.length = 0;
  await s.applyChanges(false); await settle();
  out.stale = { toast: toasts.slice(-1)[0], replanned: posts().filter((p) => p[0] === '/api/books/1/review-changes/').length, kept: s.choiceOf(s.changesItems.get(I3)) };
  drop();
  // a failed plan; a book edited before 7c (every item «للمقارنة», starting on «نصّي»); nothing to take («تم»)
  const f = mk({ urls, drift: done.drift }); await settle();
  planPost = pickOf(CR, '{} (all drift pages)'); planGets = [pickOf(CP, '(error)')];
  f.setTab('changes'); await settle();
  out.failed = { state: f.changes.state, error: f.changes.error };
  planGets = [pickOf(CP, 'no base')];
  await f.planChanges({}); await settle();
  out.noBase = f.changesRows.flatMap((r) => r.items.map((it) => [it.kind, it.chip, f.choiceOf(it), it.ask, it.help]));
  const agree = JSON.parse(JSON.stringify(done)); agree.plan.items = []; agree.plan.pages = agree.plan.pages.map((p) => Object.assign(p, { items: [] }));
  planGets = [agree];
  await f.planChanges({}); await settle();
  applyAnswer = pickOf(CR, 'keep_all');
  calls.length = 0;
  await f.settleChanges(); await settle();
  out.agree = { rows: f.changesRows.length, post: posts().find((p) => p[0].includes('/apply/')) };
  drop();
  // the text is not edited (409 reassemble): nothing to compare, a re-assembly loses nothing
  const u = mk({ urls, drift: done.drift }); await settle();
  planPost = pickOf(CR, 'unedited manuscript');
  u.setTab('changes'); await settle();
  out.notEdited = u.changes.notEdited;
  drop();
  // ---- the address: `?tab=` survives a landing and a tab switch; `?block=` lands on the paragraph, lit
  replaced.length = 0;
  const a = mk({ urls, tab: 'uncertain' }); await settle();
  a.showPage(3, { instant: true }); await settle();
  const landed = replaced.slice(-1)[0];
  a.setTab('find'); const switched = replaced.slice(-1)[0];
  a.setMode('edit'); await settle(); const edit = replaced.slice(-1)[0];
  out.address = { landed, switched, edit };
  drop();
  const bl = mk({ urls, block: 'p13' }); await settle();
  out.block = { current: bl.current, flash: bl.flash && bl.flash.block, pending: bl.pendingBlock };
  drop();
  // ---- the find prefill (fix everywhere on an edited book): «استبدال الكل», then the batch's pages compared
  const pf = mk({ urls, tab: 'find', findPrefill: { query: 'برقة', replacement: 'برقه', fix: 'b-1' } }); await settle();
  out.prefill = { tab: pf.tab, query: pf.find.query, replacement: pf.find.replacement, scope: pf.find.scope };
  server.find = (body) => reply(200, { matches: [], total: 2, replaced: body.replace ? 2 : 0, snapshot: 5 });
  planPost = pickOf(CR, '{fix: <batch>}'); planGets = [done];
  calls.length = 0;
  await pf.replaceAll(); await settle();
  out.fixPlan = posts().filter((p) => p[0] === '/api/books/1/review-changes/').map((p) => p[1]);
  server.find = null;
  drop();
  // ---- «تحويل إلى حاشية للعلامة (n)»: the chapter as this page holds it, the answer as one step of its undo
  const n = mk({ urls, mode: 'edit' }); await settle();
  n.showPage(3, { instant: true }); await settle();
  await n.loadChapter('h10'); await settle();
  noteAnswer = pickOf(NOTE, '(the chapter the book page holds');
  calls.length = 0;
  const made = await n.toFootnote('p13'); await settle();
  const sent = posts().find((p) => p[0] === '/api/books/1/to-footnote/');
  out.note = { block: sent && sent[1].block, doc: sent && sent[1].content.type, same: sent && JSON.stringify(sent[1].content.content) === JSON.stringify(server.docs.h10), nodes: JSON.stringify(n.ctx().nodes) === JSON.stringify(noteAnswer.response.content.content), put: calls.some((x) => x[0] === 'PUT'), toast: toastStore().message, marker: made && made.note.marker };
  noteAnswer = pickOf(NOTE, 'no call in the page');
  n.ctx().nodes = clone(server.docs.h10); // the chapter before the note
  toasts.length = 0;
  await n.toFootnote('p13'); await settle();
  out.noteRefused = toasts.slice(-1)[0];
  out.marker = [n.noteMarkerOf({ type: 'paragraph', attrs: { noteFor: '3' }, content: [] }), n.noteMarkerOf({ type: 'paragraph', attrs: {}, content: [{ type: 'text', text: '(٢) انظر' }] }), n.noteMarkerOf({ type: 'paragraph', attrs: {}, content: [{ type: 'text', text: 'نص عادي' }] })];
  drop();
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


def test_book_page_review_changes_the_address_and_the_note_under_node(tmp_path):
    """D78 (§5.6) on the contract: the banner's wording per reason, the tab (post, poll, rows, choices, the
    apply's body, the toast and its undo, keep-all, one page, a stale answer, a failure, a book with no base,
    nothing to take), §5.4's addresses (`?tab=`, `?block=`, §5.7's find prefill) and «تحويل إلى حاشية»."""
    folder = _node_tmp(tmp_path)
    fixture = _component_fixture()
    (folder / "fixture.json").write_text(json.dumps(fixture, ensure_ascii=False))
    contract = _contract_all()
    (folder / "contract.json").write_text(json.dumps(contract, ensure_ascii=False))
    base = COMPONENT_HARNESS.split("\nconst out = {};\n")[0]
    out = _run_node(
        folder,
        "changes.mjs",
        base + CHANGES_SCENARIO,
        str(ROOT),
        str(folder / "fixture.json"),
        str(folder / "contract.json"),
    )
    banner = out["banner"]
    assert banner["review"] == ["edited", "تغيّر نص صفحتين في المراجعة بعد تحرير الكتاب:", [1, 2], False]
    assert banner["processing"] == ["edited", "أعادت المعالجة قراءة صفحة واحدة بعد تحرير الكتاب:", [7], False]
    assert banner["mixed"] == [
        "edited",
        "تغيّر نص 6 صفحات بعد تحرير الكتاب: 3 في المراجعة، 1 أعادت المعالجة قراءتها، 1 رُوجعت ولم تدخل الكتاب"
        " و1 أُخرجت من الكتاب.",
        [],
        False,
    ]
    assert banner["added"] == ["edited", "رُوجعت 3 صفحات لم تدخل الكتاب بعد:", [91, 92, 93], False]
    assert banner["many"] == [
        "edited",
        "أعادت المعالجة قراءة 10 صفحات بعد تحرير الكتاب:",
        [4, 12, 13, 20, 21, 22, 30, 31],
        True,
    ]
    assert banner["approvals"] is None  # approvals are never announced
    assert banner["unedited"] == [
        "assembled",
        "تغيّر نص صفحة واحدة في المراجعة بعد التجميع؛ لم يُحرَّر الكتاب بعد، فإعادة التجميع لا تُضيّع شيئًا.",
        [],
        False,
    ]
    assert out["later"] == {"hidden": None, "back": True, "stored": True}
    kinds = out["contractKinds"]
    assert kinds[_pick_key(_contract_all()["drift.json"], "(every reason)")] == "edited"
    assert kinds[_pick_key(_contract_all()["drift.json"], "approvals only")] is None
    assert kinds[_pick_key(_contract_all()["drift.json"], "no manuscript")] is None
    # the tab: only with content drift, its badge the drift's pages; opening it posts and polls every 700 ms
    requests = _contract_all()["changes_requests.json"]
    plan_id = _pick(_contract_all()["changes_plan.json"], "done, a stored base")["plan"]["id"]
    apply_url = f"/api/books/1/review-changes/{plan_id}/apply/"
    assert out["noTab"] == {"has": False, "tabs": False}
    assert out["tab"] == {"has": True, "badge": "6", "count": 6}
    assert out["posting"]["post"] == [
        "/api/books/1/review-changes/",
        _pick(requests, "{} (all drift pages)")["request"],
    ]
    assert out["posting"]["address"] == "/books/1/layout/?tab=changes#page-2" and out["posting"]["wait"] == 1
    assert out["polled"] == {"gets": 3, "state": "ready"}
    plan = _pick(_contract_all()["changes_plan.json"], "done, a stored base")["plan"]
    items = {it["id"]: it for it in plan["items"]}
    assert out["rows"] == [
        [
            row["number"],
            row["reason"],
            row["reason_label"],
            row["chapter_title"],
            [
                [i, items[i]["kind"], items[i]["chip"], items[i]["default"], items[i]["ask"]]
                for i in row["items"]
            ],
        ]
        for row in plan["pages"]
    ]
    labels = {
        "review": "المراجعة",
        "processing": "إعادة المعالجة",
        "added": "صفحة جديدة",
        "removed": "أُخرجت من الكتاب",
    }
    assert all(row[2] == labels[row[1]] for row in out["rows"])  # the chips' words (§5.6)
    assert out["apply"] == {"label": f"أخذ التغييرات ({len(items)})", "count": len(items)}
    defaults = _pick(requests, "(the defaults, the conflict on «المراجعة»)")
    assert out["defaultBody"] == defaults["request"]
    assert out["page"] == {"theirs": "theirs", "mine": "mine"}
    conflict = next(item for item in plan["items"] if item["kind"] == "conflict")
    assert out["applied"]["post"][1] == {"choices": {conflict["id"]: "theirs"}, "pages": [1, 2, 3, 4, 5, 7]}
    assert out["applied"]["post"][0] == apply_url
    assert out["applied"]["chapters"] is True and out["applied"]["version"] == defaults["response"]["version"]
    assert out["applied"]["toast"] == "أُخذت تغييرات 6 صفحات من المراجعة" and out["applied"]["has"] is True
    assert out["applied"]["flash"] and "p40012" in out["applied"]["flash"]
    assert out["undo"] == [f"/api/books/1/snapshots/{defaults['response']['snapshot']}/restore/", {}]
    keep = _pick(requests, "keep_all")
    assert out["keep"] == {
        "post": [apply_url, keep["request"]],
        "toast": "بقي نصّك كما هو؛ لن تعود هذه الصفحات إلى التغييرات",
    }
    assert out["onePage"] == [
        apply_url,
        _pick(requests, "{pages: [2]}")["request"],
    ]
    assert out["stale"] == {
        "toast": "تغيّر النص منذ المقارنة؛ أُعيدت المقارنة.",
        "replanned": 1,
        "kept": "theirs",
    }
    assert out["failed"] == {"state": "error", "error": "تعذّرت المقارنة؛ بقي الكتاب كما هو."}
    help_choose = "لا يُعرف ما عدّلتَه هنا قبل هذا الإصدار من نسّاخ؛ قارن واختر."
    # a book edited before 7c: every paragraph that differs is «للمقارنة» on «نصّي»; a page new to the book
    # (`added`) still brings its paragraph in
    chosen = [item for item in out["noBase"] if item[0] != "insert"]
    assert len(chosen) == 6 and all(
        item == ["choose", "للمقارنة", "mine", True, help_choose] for item in chosen
    )
    assert [item for item in out["noBase"] if item[0] == "insert"] == [
        ["insert", "فقرة جديدة", "theirs", False, ""]
    ]
    assert out["agree"] == {"rows": 6, "post": [apply_url, keep["request"]]}
    assert out["notEdited"] == _pick(requests, "unedited manuscript")["response"]["detail"]
    # §5.4: the address keeps the tab through a landing, a tab switch and edit mode
    assert out["address"] == {
        "landed": "/books/1/layout/?tab=uncertain#page-3",
        "switched": "/books/1/layout/?tab=find#page-3",
        "edit": "/books/1/layout/?mode=edit&tab=source#page-3",
    }
    assert out["block"] == {"current": 2, "flash": "p13", "pending": None}  # p13 starts at the foot of page 2
    assert out["prefill"] == {"tab": "find", "query": "برقة", "replacement": "برقه", "scope": "book"}
    assert out["fixPlan"] == [_pick(requests, "{fix: <batch>}")["request"] | {"fix": "b-1"}]
    note = out["note"]
    assert note["block"] == "p13" and note["doc"] == "doc" and note["same"] is True
    assert note["nodes"] is True and note["put"] is True and note["marker"] == "1"
    assert note["toast"] == "صارت الفقرة حاشية للعلامة (1)"
    refused = _pick(_contract_all()["to_footnote.json"], "no call in the page")["response"]["detail"]
    assert out["noteRefused"] == refused
    assert out["marker"] == ["3", "2", ""]


def test_book_page_templates_for_review_changes_and_the_note(editor):
    body, _config = _page(_logged(editor), _book())
    banner = _between(body, "data-drift-banner", "data-conflict-banner")
    assert 'x-show="driftBanner"' in body and "عرض التغييرات" in banner and "لاحقًا" in banner
    assert "@click=\"setTab('changes')\" data-changes-button" in banner and "إعادة التجميع" in banner
    menu = _between(body, "data-book-menu", "</template>")
    assert (
        "تغييرات المراجعة…" in menu
        and 'x-show="v.hasChangesTab"' in menu
        and 'x-text="v.changesCount"' in menu
    )
    panel = _between(body, 'data-panel="changes"', "</section>")
    for needle in (
        "تُؤخذ فقرات هذه الصفحات وحدها، ويبقى كل ما سواها كما حرّرته. تُحفظ نسخة قبل الأخذ.",
        "يُقارَن نص الصفحات بنص الكتاب…",
        "لا فرق في النص؛ المراجعة والكتاب متّفقان في هذه الصفحات.",
        "خذ ما جاء من المراجعة في هذه الصفحة",
        "أبقِ نصّي في هذه الصفحة",
        ">نصّي</button>",
        ">المراجعة</button>",
        "الاحتفاظ بنصّي في الكل",
        "إعادة بناء الفصل من المراجعة…",
        '<del x-text="part[1]"></del>',
        '<ins x-text="part[1]"></ins>',
        'x-text="changesApplyLabel"',
        ':href="reviewUrl(row.number)"',
        "إعادة المحاولة",
    ):
        assert needle in panel, needle
    block = _between(body, 'data-panel="block"', "</section>")
    assert "data-to-footnote" in block and "'تحويل إلى حاشية للعلامة (' + blockInfo.noteFor + ')'" in block
    assert '<symbol id="i-merge"' in body
    css = (ROOT / "static" / "src" / "components" / "layout.css").read_text(encoding="utf-8")
    assert ".lp-line.is-applied { animation: lp-applied 600ms ease-out; }" in css
    assert ".bp-diff del { color: var(--color-danger-text); background: var(--color-danger-bg);" in css
    assert ".bp-diff ins {" in css and "text-decoration: underline" in css


CHANGES_FIXES_SCENARIO = r"""
const contract = JSON.parse(readFileSync(process.argv[4], 'utf8'));
const pickOf = (obj, label) => Object.entries(obj).find(([k]) => k.includes(label))[1];
const CP = contract['changes_plan.json']; const CR = contract['changes_requests.json'];
const out = {};
const baseFetch = globalThis.fetch;
let planGets = []; let applyAnswers = [];
globalThis.fetch = async (url, init = {}) => {
  const method = init.method || 'GET';
  const body = init.body ? JSON.parse(init.body) : null;
  if (url === '/api/books/1/review-changes/') {
    calls.push([method, url, body]);
    if (method === 'POST') return reply(202, pickOf(CR, '{} (all drift pages)').response);
    return reply(200, planGets.length > 1 ? planGets.shift() : planGets[0]);
  }
  if (/^\/api\/books\/1\/review-changes\/\d+\/apply\/$/.test(url)) { calls.push([method, url, body]); const a = applyAnswers.shift(); return reply(a.status, a.response); }
  return baseFetch(url, init);
};
const posts = () => calls.filter((c) => c[0] === 'POST').map((c) => [c[1], c[2]]);
const urls = { ...fixture.config.urls, drift: '/api/books/1/drift/', reviewChanges: '/api/books/1/review-changes/', reviewChangesApply: '/api/books/1/review-changes/__pid__/apply/' };
(async () => {
  // the contract's plan, with page 1's paragraph running onto page 2 (listed under both rows, as compute_plan does)
  const done = JSON.parse(JSON.stringify(pickOf(CP, 'done, a stored base')));
  const take = done.plan.items.find((it) => it.kind === 'take');
  const merged = done.plan.items.find((it) => it.kind === 'merged');
  const [conflict] = done.plan.items.filter((it) => it.kind === 'conflict');
  take.pages = [1, 2];
  const row2 = done.plan.pages.find((r) => r.number === 2);
  row2.items = [take.id, ...row2.items];
  planGets = [done];
  const c = mk({ urls, drift: done.drift }); await settle();
  c.setTab('changes'); await settle();
  out.count = { count: c.changesApplyCount, label: c.changesApplyLabel, items: done.plan.items.length };
  // «خذ ما جاء من المراجعة في هذه الصفحة» on page 2: a merged paragraph keeps the owner's own edits («merged»)
  c.setPageChoice(c.changesRows.find((r) => r.number === 2), 'theirs');
  out.page = { merged: c.choiceOf(c.changesItems.get(merged.id)), conflict: c.choiceOf(c.changesItems.get(conflict.id)), take: c.choiceOf(c.changesItems.get(take.id)) };
  out.body = c.applyBody(false);
  // a 409: the comparison again; the pages left out stay out and the choices stay on their items
  c.togglePage(c.changesRows.find((r) => r.number === 3));
  applyAnswers = [pickOf(CR, 'stale version'), pickOf(CR, '{pages: [2]}')];
  planGets = [done];
  calls.length = 0;
  await c.applyChanges(false); await settle();
  out.stale = { pages: c.changesRows.filter((r) => !r.taken).map((r) => r.number), conflict: c.choiceOf(c.changesItems.get(conflict.id)), replanned: posts().filter((p) => p[0] === '/api/books/1/review-changes/').length };
  calls.length = 0;
  await c.applyChanges(false); await settle();
  out.again = posts().find((p) => p[0].includes('/apply/'))[1];
  drop();
  // the find prefill of a fix everywhere: the review sheet's options replace the book page's defaults
  const opts = { match_tashkeel: false, fold_alef: true, whole_word: true };
  const pf = mk({ urls, tab: 'find', findPrefill: { query: 'السعودي', replacement: 'المسعودي', fix: null, options: opts } }); await settle();
  server.find = (body) => reply(200, { matches: [], total: 1, replaced: body.replace ? 1 : 0, snapshot: 5 });
  calls.length = 0;
  await pf.replaceAll(); await settle();
  const sent = calls.find((x) => x[1] === '/api/books/1/find-replace/' && x[2] && x[2].replace);
  out.prefill = { options: pf.findOptions(), sent: sent && [sent[2].match_tashkeel, sent[2].fold_alef, sent[2].whole_word] };
  const plain = mk({ urls, tab: 'find', findPrefill: { query: 'نص', replacement: 'نصوص', fix: null, options: null } }); await settle();
  out.plain = plain.findOptions();
  server.find = null;
  drop();
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


def test_book_page_review_changes_fixes_under_node(tmp_path):
    """7c review fixes on the contract's plan: an item over two pages counts once in «أخذ التغييرات (n)»;
    «خذ ما جاء من المراجعة في هذه الصفحة» gives a merged paragraph «merged» (review's change with the owner's
    edits kept), never «theirs»; a 409 re-plan keeps the pages left out and the choices; a fix everywhere's
    find prefill brings the review sheet's options (a link without them leaves the page's own)."""
    folder = _node_tmp(tmp_path)
    fixture = _component_fixture()
    (folder / "fixture.json").write_text(json.dumps(fixture, ensure_ascii=False))
    (folder / "contract.json").write_text(json.dumps(_contract_all(), ensure_ascii=False))
    base = COMPONENT_HARNESS.split("\nconst out = {};\n")[0]
    out = _run_node(
        folder,
        "changes_fixes.mjs",
        base + CHANGES_FIXES_SCENARIO,
        str(ROOT),
        str(folder / "fixture.json"),
        str(folder / "contract.json"),
    )
    items = out["count"]["items"]
    assert out["count"] == {"count": items, "label": f"أخذ التغييرات ({items})", "items": items}
    assert out["page"] == {"merged": "merged", "conflict": "theirs", "take": "theirs"}
    assert "merged" not in out["body"]["choices"].values() and "theirs" in out["body"]["choices"].values()
    assert out["stale"]["pages"] == [3] and out["stale"]["conflict"] == "theirs"
    assert out["stale"]["replanned"] == 1
    assert 3 not in out["again"]["pages"] and "theirs" in out["again"]["choices"].values()
    assert out["prefill"] == {
        "options": {"matchTashkeel": False, "foldAlef": True, "wholeWord": True},
        "sent": [False, True, True],
    }
    assert out["plain"] == {"matchTashkeel": False, "foldAlef": True, "wholeWord": False}
