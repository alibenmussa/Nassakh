"""Chapter editor (Phase 5, PHASE5_SPEC §4): the rendered template states (Django test client), the esbuild
bundle (built and current), the schema module under Node — the pure document ↔ editor conversion, the find
matcher against the server's own (`editor.document`), the ProseMirror plugins on a state without a DOM — and
the `bookEditor` Alpine component driven with a stub editor (autosave timing and the version conflict, the
find panel, the style picker, the footnote editor, the source pane, snapshots, digits, re-assembly, the
keyboard map, the unsaved-changes guard)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from django.test import Client
from django.urls import reverse

import pytest

from books.models import Book
from editor import document as doc
from editor.models import Manuscript
from editor.tests import logged, role_user, sample_document

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "src" / "js"
SRC = ROOT / "static" / "src" / "editor"
BUNDLE = ROOT / "static" / "dist" / "editor.js"
CSS = ROOT / "static" / "dist" / "app.css"


@pytest.fixture
def book(db):
    return Book.objects.create(title="كتاب المحرّر", author="المؤلف")


@pytest.fixture
def written(book):
    return Manuscript.objects.create(book=book, document=sample_document(), version=1)


@pytest.fixture
def editor_user(db):
    return role_user("editor", "editor")


@pytest.fixture
def reader_user(db):
    return role_user("reader", "proofreader")


def _json_script(body: str, element_id: str):
    match = re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>', body, re.S)
    assert match, element_id
    return json.loads(match.group(1))


def _page(client, book, query: str = "") -> str:
    return client.get(reverse("editor:edit", args=[book.pk]) + query).content.decode()


# ---------------------------------------------------------------- the template: every state


def test_editor_page_without_a_manuscript_shows_the_empty_state(book, editor_user):
    body = _page(logged(editor_user), book)
    assert "<title>المحرّر · كتاب المحرّر · نسّاخ</title>" in body
    assert "لا توجد مخطوطة بعد" in body and "فتح المخطوطة" in body
    assert _json_script(body, "editor-config")["exists"] is False
    assert "data-editor x-data" not in body and "dist/editor.js" in body
    assert Client().get(reverse("editor:edit", args=[book.pk])).status_code == 302


def test_editor_page_renders_the_chrome_for_an_editor(written, editor_user):
    body = _page(logged(editor_user), written.book, "?chapter=h20")
    config = _json_script(body, "editor-config")
    assert config["chapter"] == "h20" and config["canEdit"] is True and config["autosaveMs"] == 1500
    assert config["stylesheet"]["trim"] == "17x24" and config["faces"]["body"]["family"] == "Amiri"
    assert config["urls"]["chapter"].endswith("/chapters/__cid__/") and config["urls"]["sheets"]
    # the component root, the scripts (the glue from base.html, the bundle from the page)
    assert (
        'data-editor x-data="bookEditor(' in body and "src/js/editor.js" in body and "dist/editor.js" in body
    )
    assert '@beforeunload.window="guardUnload($event)"' in body and '@keydown.window="onKey($event)"' in body
    assert '<style x-text="fontCss"></style>' in body
    # the top bar: the chapter as meta, the save pill (= rv-save), undo / redo, the primary, the «⋯» menu
    assert 'x-text="v.chapterTitle"' in body and 'class="ed-pill"' in body and 'x-text="v.pill.text"' in body
    assert 'href="#i-undo"' in body and 'href="#i-redo"' in body and "معاينة الصفحات" in body
    menu = body[
        body.index('class="menu menu-popover ed-menu"') : body.index("</template>", body.index("ed-menu"))
    ]
    for item in (
        "تنسيق الكتاب",
        "نسخة محفوظة…",
        "تحويل الأرقام…",
        "إعادة تجميع الفصل من المراجعة",
        "المخطوطة",
        "لوحة الكتاب",
    ):
        assert item in menu, item
    # the toolbar: the style picker with its keys, B / I, the footnote, find, the page marks, the zoom
    toolbar = body[body.index('class="ed-toolbar"') : body.index('class="ed-find"')]
    assert (
        'x-text="styleLabel"' in toolbar
        and 'role="menuitemradio"' in toolbar
        and 'x-text="s.keys"' in toolbar
    )
    assert 'title="غامق (⌘B)"' in toolbar and 'title="مائل (⌘I)"' in toolbar
    assert "إدراج حاشية عند المؤشّر (⌘⇧F)" in toolbar and "بحث واستبدال (⌘F)" in toolbar
    # the mark and footnote buttons swallow mousedown: the selection in the text keeps its focus (as in Word)
    assert toolbar.count("@mousedown.prevent @click=") == 3
    # the find panel's measured height moves the side panel's sticky top (editor.css --ed-find-h)
    assert ':style="screenStyle"' in body
    assert "فواصل الصفحات الأصلية" in toolbar and toolbar.count('@click="setZoom(') == 3
    # find & replace: the query with its count, the options, the scope, the replace row
    find = body[body.index('class="ed-find"') : body.index('class="ed-banners"')]
    assert 'x-ref="findQuery"' in find and 'x-text="findCountText"' in find
    for option in ("مطابقة التشكيل", "توحيد الألف", "كلمة كاملة", "هذا الفصل", "الكتاب كلّه"):
        assert option in find, option
    assert ">استبدال</button>" in find and ">استبدال الكل</button>" in find
    # the banners: drift with the review links and the re-assembly, the conflict with its two ways out
    assert 'class="banner ed-drift"' in body and ':href="reviewUrl(n)"' in body and "الاحتفاظ بالنص" in body
    assert "تغيّر هذا الفصل في نافذة أخرى" in body and "إعادة التحميل" in body and "الاحتفاظ بنصّي" in body
    # the sheet with the stylesheet's vars, the skeleton, the status line, the one overlay with its two faces
    assert (
        'class="ed-sheet" x-ref="sheet" :style="sheetStyle" dir="rtl"' in body
        and 'class="ed-skeleton"' in body
    )
    assert 'class="ed-status meta" x-text="statusText"' in body
    assert body.count('class="menu ed-pop" x-ref="pop" x-show="pop.kind" x-cloak') == 1
    assert "x-if=\"pop.kind === 'note'\"" in body and 'x-ref="noteHost"' in body and "حذف الحاشية" in body
    assert "Enter للعودة إلى النص · ⇧Enter سطر جديد · Esc للإغلاق" in body
    assert (
        "x-if=\"pop.kind === 'word'\"" in body
        and "اختر القراءة الصحيحة" in body
        and "قبول الكلمة كما هي" in body
    )
    # the side panel (= bk-side, three bk-side-head sections), the drawer, the dialogs, the undo toast
    side = body[body.index('<aside class="bk-side ed-side"') : body.index("</aside>", body.index("ed-side"))]
    assert side.count('class="bk-side-head"') == 3
    assert "<span>الفصول</span>" in side and "<span>الأصل</span>" in side and "<span>ملاحظات</span>" in side
    assert 'data-ed-list @click="onListClick($event)"' in side and 'x-for="b in sourceBoxes()"' in side
    assert 'x-for="(w, i) in warnings"' in side and ">انتقال</button>" in side and ">مراجعة</a>" in side
    assert 'class="ed-drawer" role="dialog"' in body and 'x-ref="drawerClose"' in body
    assert 'class="rv-modal ed-snapshots"' in body and "حفظ نسخة الآن" in body and ">استعادة</button>" in body
    assert 'class="rv-modal ed-digits"' in body and "عربية هندية" in body
    # the dialogs open with the focus inside and close through one method (the focus returns to the trigger)
    assert (
        'x-ref="digitsFirst"' in body
        and 'x-ref="snapshotsClose"' in body
        and "digits.open = false" not in body
    )
    # the «⋯» menu's «تنسيق الكتاب» carries the chapter like the primary
    assert ':href="v.layoutUrl ||' in body
    assert "اختصارات لوحة المفاتيح" in body and "⌘⌥1" in body and "⌘⇧F" in body and "⌘[" in body
    assert "$store.editorToast.run()" in body and ">تراجع</button>" in body
    assert "ms-card" not in body and "{#" not in body.split("<body")[1]


def test_editor_page_for_a_proofreader_is_read_only(written, reader_user):
    body = _page(logged(reader_user), written.book)
    assert _json_script(body, "editor-config")["canEdit"] is False
    assert 'aria-label="نمط الفقرة" title="نمط الفقرة" disabled' in body
    assert 'href="#i-undo"' not in body and "تحويل الأرقام…" not in body
    find = body[body.index('class="ed-find"') : body.index('class="ed-banners"')]
    assert ">استبدال الكل</button>" not in find and "الكتاب كلّه" not in find and "كلمة كاملة" in find
    assert "قراءات هذه الكلمة" in body and "قبول الكلمة كما هي" not in body
    assert (
        'class="rv-modal ed-digits"' not in body
        and "حفظ نسخة الآن" not in body
        and ">استعادة</button>" not in body
    )
    assert "إعادة تجميع الفصل من المراجعة" not in body


# ---------------------------------------------------------------- the bundle


def test_editor_bundle_is_built_and_current(tmp_path):
    assert BUNDLE.is_file(), "run npm run build:editor"
    head = BUNDLE.read_bytes()[:120]
    assert b"Nassakh chapter editor bundle" in head
    check = subprocess.run(["node", "--check", str(BUNDLE)], capture_output=True, text=True, timeout=60)
    assert check.returncode == 0, check.stderr
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
    for name in ("convert.js", "schema.js", "index.js"):
        check = subprocess.run(
            ["node", "--check", str(SRC / name)], capture_output=True, text=True, timeout=60
        )
        assert check.returncode == 0, check.stderr
    check = subprocess.run(
        ["node", "--check", str(JS / "editor.js")], capture_output=True, text=True, timeout=60
    )
    assert check.returncode == 0, check.stderr


def test_editor_css_is_compiled():
    css = CSS.read_text(encoding="utf-8")
    for rule in (
        ".ed-sheet{",
        ".ed-toolbar{",
        ".ed-find{",
        ".ed-fn:before{",
        ".ed-pb:after{",
        ".ed-match{",
        ".ed-ch{",
        ".ProseMirror-focused .ProseMirror-gapcursor{",
    ):
        assert rule in css, rule
    assert "@media (prefers-reduced-motion:reduce)" in css and ".ed-pop," in css


# ---------------------------------------------------------------- the schema module under Node


def _chapter_nodes() -> list:
    """The sample document's blocks after the title (what a chapter save carries), plus every node kind."""
    nodes = sample_document()["content"][1:]
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
// ---- conversion: a chapter round-trips unchanged, the odd shapes are folded
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
// ---- ids never look like the server's (e<n>) and never repeat
const ids = new Set(Array.from({ length: 200 }, () => convert.newId('e')));
out.ids = { unique: ids.size, shape: [...ids].every((id) => /^ek[0-9a-z]+$/.test(id) && !/^e\d+$/.test(id)), note: /^nek/.test(convert.newId('ne')) };
// ---- find: the same matches as the server (compared in Python), the word count too
out.matches = fixture.queries.map(([query, opts]) => convert.findMatches(fixture.nodes, query, { matchTashkeel: Boolean(opts.match_tashkeel), foldAlef: opts.fold_alef !== false, wholeWord: Boolean(opts.whole_word) }));
out.words = convert.wordCount(fixture.nodes);
out.fold = [convert.fold('مَدِينَةُ', {}).text, convert.fold('أإآ', {}).text, convert.fold('Aب', {}).text, convert.foldQuery('  ', {})];
out.format = [convert.formatCount(2184), convert.formatCount(999), convert.formatCount(1234567), convert.pageRange({ first: 31, last: 58 }), convert.pageRange({ first: 4, last: 4 }), convert.pageRange(null)];
out.styles = convert.STYLES.map((s) => s.key);
out.styleOf = [convert.styleOf({ type: 'heading', attrs: { level: 2 } }), convert.styleOf({ type: 'paragraph', attrs: { style: 'quote' } }), convert.styleOf({ type: 'separator' }), convert.styleOf({ type: 'paragraph', attrs: {} })];
out.list = convert.chapterListHtml([{ id: 'h1', number: 1, kind: 'chapter', title: 'الفصل <الأول>', words: 2184, pages: { first: 31, last: 58 }, drift: true }, { id: 'p0', number: 2, kind: 'front', title: 'قبل', words: 3, pages: null, drift: false }], 'h1');
out.wordForms = [convert.wordsHtml(1), convert.wordsHtml(2), convert.wordsHtml(5), convert.wordsHtml(40)];
// the find cycle: forward, backward, and in place (after a replacement the index already points at the next match)
out.stepIndex = [convert.stepIndex(1, 0, 3), convert.stepIndex(2, 1, 3), convert.stepIndex(0, -1, 3), convert.stepIndex(-1, 1, 3), convert.stepIndex(-1, -1, 3), convert.stepIndex(0, 1, 0)];
// ---- the schema: every node and mark, a document from the chapter checks
const s = core.getSchema(schema.extensions({}));
out.schema = { nodes: Object.keys(s.nodes), marks: Object.keys(s.marks), noteContent: s.nodes.footnote.spec.content, noteDoc: core.getSchema(schema.noteExtensions()).topNodeType.spec.content };
const pmDoc = s.nodeFromJSON(editorJson);
pmDoc.check();
out.docChildren = pmDoc.childCount;
// ---- the plugins on a state without a DOM: fresh ids after a split, the numbers, the find highlights, the
// uncertain mark leaving a retyped word (and staying on a load, a large insert, a typed space after it)
const plugins = [schema.uniqueIdsPlugin(), schema.footnoteNumbersPlugin(), schema.caretBlockPlugin(), schema.findPlugin(), schema.resolveOnTypePlugin()];
let state = EditorState.create({ doc: pmDoc, plugins });
const blockIds = (st) => { const o = []; st.doc.forEach((n) => o.push(n.attrs.id)); return o; };
const p11 = (() => { let pos = null; state.doc.forEach((n, offset) => { if (n.attrs.id === 'p11') pos = offset; }); return pos; })();
let tr = state.tr.setSelection(TextSelection.create(state.doc, p11 + 5)).split(p11 + 5);
state = state.apply(tr);
out.split = { ids: blockIds(state), unique: new Set(blockIds(state)).size === blockIds(state).length, second: state.doc.child(3).attrs, kept: state.doc.child(2).attrs.id };
// a pasted copy of a block: the duplicate id is replaced
const copy = state.doc.child(2).toJSON();
tr = state.tr.insert(state.doc.content.size, s.nodeFromJSON(copy));
state = state.apply(tr);
out.dup = { unique: new Set(blockIds(state)).size === blockIds(state).length, last: blockIds(state).slice(-1)[0] };
// footnote numbers: data-seq 1 for n1; a second note inserted before it renumbers
const decos = (st) => schema.numbersKey.getState(st).find().map((d) => d.type.attrs['data-seq']);
out.numbers1 = decos(state);
const note = s.nodes.footnote.create({ id: 'n0' }, s.text('قبلها'));
tr = state.tr.insert(state.doc.child(0).nodeSize + 1, note);
state = state.apply(tr);
out.numbers2 = decos(state);
// the caret block decoration follows the selection head
const caret = (st) => schema.caretKey.getState(st).find().map((d) => st.doc.nodeAt(d.from).attrs.id);
out.caret = [caret(state), caret(state.apply(state.tr.setSelection(TextSelection.create(state.doc, 2))))];
// find highlights through the plugin meta, mapped through an edit before them
tr = state.tr.setMeta(schema.findKey, { ranges: [{ from: 20, to: 24, block: 'x', note: null }], current: 0 });
state = state.apply(tr);
const findState = (st) => schema.findKey.getState(st);
out.find1 = { n: findState(state).decos.find().length, cls: findState(state).decos.find()[0].type.attrs.class };
state = state.apply(state.tr.insertText('ab', 2));
out.find2 = findState(state).ranges[0];
// the uncertain mark: a typed letter inside «مليتية» resolves the whole word; a load keeps it
const uncertain = (st) => { const o = []; st.doc.descendants((n) => { if (n.isText && n.marks.some((m) => m.type.name === 'uncertain')) o.push(n.text); }); return o; };
out.uncertainBefore = uncertain(state);
const posOf = (st, text) => { let at = null; st.doc.descendants((n, pos) => { if (n.isText && n.text === text) at = pos; }); return at; };
state = state.apply(state.tr.insertText('ك', posOf(state, 'مليتية') + 2));
out.uncertainAfterTyping = uncertain(state);
state = EditorState.create({ doc: pmDoc, plugins });
state = state.apply(state.tr.insertText(' ', posOf(state, 'مليتية') + 6)); // a space right after the word: the mark stays
out.uncertainAfterSpace = uncertain(state);
state = state.apply(state.tr.insertText('نص طويل جدًّا أُلصق هنا', posOf(state, 'مليتية') + 1).setMeta('uiEvent', 'paste'));
out.uncertainAfterPaste = uncertain(state).length;
state = EditorState.create({ doc: pmDoc, plugins });
state = state.apply(state.tr.insertText('x', posOf(state, 'مليتية') + 2).setMeta('ed:load', true));
out.uncertainAfterLoad = uncertain(state);
console.log(JSON.stringify(out));
"""  # noqa: E501


def _node_tmp(tmp_path: Path) -> Path:
    """A folder whose bare imports resolve through the repo's node_modules (one copy of every package)."""
    if not (ROOT / "node_modules" / "@tiptap" / "core").is_dir():
        pytest.skip("node_modules not installed")
    os.symlink(ROOT / "node_modules", tmp_path / "node_modules", target_is_directory=True)
    return tmp_path


def test_schema_module_conversion_matching_and_plugins_under_node(tmp_path):
    folder = _node_tmp(tmp_path)
    nodes = _chapter_nodes()
    (folder / "fixture.json").write_text(json.dumps({"nodes": nodes, "queries": QUERIES}, ensure_ascii=False))
    harness = folder / "schema.mjs"
    harness.write_text(SCHEMA_HARNESS, encoding="utf-8")
    run = subprocess.run(
        ["node", str(harness), str(ROOT), str(folder / "fixture.json")],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    # a chapter round-trips as the server has it (an unedited save changes nothing), and the server accepts it
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
    # blockquotes → quote paragraphs, horizontalRule → separator, level 4 → 2, junk dropped, a title takes
    # its text
    assert out["odd"][0][:2] == ["paragraph", "quote"] and out["odd"][1][:2] == ["paragraph", "verse"]
    assert out["odd"][2][:2] == ["separator", "r1"] and out["odd"][3][:2] == ["heading", 2]
    assert json.loads(out["odd"][3][2]) == [{"type": "text", "text": "عنوان", "marks": [{"type": "bold"}]}]
    assert out["odd"][4][0] == "title" and json.loads(out["odd"][4][2]) == [
        {"type": "text", "text": "الكتاب"}
    ]
    assert json.loads(out["odd"][5][2]) == [
        {
            "type": "footnote",
            "attrs": {
                "id": "n9",
                "number": None,
                "marker": "",
                "sourcePage": None,
                "sourceLineIds": [],
                "orphan": False,
            },
            "content": [{"type": "text", "text": "ملاحظة"}],
        }
    ]
    assert len(out["odd"]) == 6 and out["empty"] == 1
    assert out["emptySaved"] == [
        {
            "type": "paragraph",
            "attrs": {"sourcePages": [], "sourceLineIds": [], "reviewed": True},
            "content": [],
        }
    ]
    doc.clean_nodes(out["emptySaved"])
    assert out["ids"] == {"unique": 200, "shape": True, "note": True}
    # the matcher answers exactly like the server for every option
    for (query, opts), js in zip(QUERIES, out["matches"], strict=True):
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
        # every query finds something, except the hamza spelled differently with the alef folding off
        assert bool(expected) is (opts != {"fold_alef": False}), (query, opts)
    assert out["words"] == doc.word_count(nodes)
    assert out["fold"] == ["مدينة", "ااا", "aب", ""]
    assert out["format"] == ["2 184", "999", "1 234 567", "31–58", "4", ""]
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
    assert 'data-cid="h1" aria-current="true"' in out["list"] and "الفصل &lt;الأول&gt;" in out["list"]
    assert '<bdi class="num">2 184</bdi> كلمة · ص <bdi class="num">31–58</bdi>' in out["list"]
    assert '<bdi class="num">3</bdi> كلمات' in out["list"]  # 3–10 take the plural
    assert out["wordForms"] == [
        "كلمة واحدة",
        "كلمتان",
        '<bdi class="num">5</bdi> كلمات',
        '<bdi class="num">40</bdi> كلمة',
    ]
    assert out["stepIndex"] == [1, 0, 2, 0, 2, -1]
    assert "تغيّر في المراجعة" in out["list"] and 'class="ed-ch is-front"' in out["list"]
    # the schema: the Phase 4 nodes one to one; notes hold text and breaks only
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
    assert out["schema"]["marks"] == ["bold", "italic", "uncertain"]
    assert out["schema"]["noteContent"] == "noteInline*" and out["schema"]["noteDoc"] == "noteInline*"
    assert out["docChildren"] == 9
    # the plugins: a split keeps the source attrs on both halves and gives the second a fresh id
    assert out["split"]["unique"] is True and out["split"]["kept"] == "p11"
    assert out["split"]["second"]["sourcePages"] == [2] and out["split"]["second"]["id"].startswith("ek")
    assert out["dup"]["unique"] is True and out["dup"]["last"].startswith("ek")
    assert out["numbers1"] == ["1"] and out["numbers2"] == ["1", "2"]
    assert out["caret"][0][0].startswith("ek") and out["caret"][1] == [
        "p1"
    ]  # the head sits in the split-off half
    assert out["find1"] == {"n": 1, "cls": "ed-match is-current"} and out["find2"]["from"] == 22
    assert out["uncertainBefore"] == ["مليتية"] and out["uncertainAfterTyping"] == []
    assert out["uncertainAfterSpace"] == ["مليتية"] and out["uncertainAfterPaste"] == 1
    assert out["uncertainAfterLoad"] == ["ملxيتية"]


# ---------------------------------------------------------------- the Alpine component under Node

HARNESS = r"""
// A stub in place of the TipTap bundle: the component only drives the object's methods, so every call is
// recorded and every answer scripted. The DOM is a handful of objects with the members editor.js touches.
const fs = require('fs');
const fixture = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const reg = {}; const inits = []; const stores = {}; const calls = []; const timers = []; const focused = []; const scrolled = [];
const RECT = { top: 300, left: 200, width: 600, height: 20 };
const rectOf = (r) => ({ top: r.top, left: r.left, width: r.width, height: r.height, bottom: r.top + r.height, right: r.left + r.width });
globalThis.window = globalThis;
globalThis.document = { hidden: false, activeElement: null, createElement: () => ({}), addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); }, getElementById: () => null, querySelector: () => null, querySelectorAll: () => [], documentElement: { dir: 'rtl' } };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: (n, v) => { if (v !== undefined) stores[n] = v; return stores[n]; } };
globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; }; globalThis.clearTimeout = () => {};
globalThis.innerHeight = 800; globalThis.innerWidth = 1200;
globalThis.history = { replaceState: (s, t, url) => calls.push(['url', url]) };
globalThis.location = { href: 'http://x/books/1/editor/?chapter=h1' };
globalThis.scrollTo = () => {};
const store = {}; globalThis.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
globalThis.Nassakh = { toast: (m) => calls.push(['toast', m]) };
// ---- the stub editor
const ed = { calls: [], content: null, editable: true, style: 'paragraph', bold: false, italic: false, undo: 0, block: fixture.block, findTable: { total: 3, index: 0 }, notes: { n1: { id: 'n1', seq: 1, sourcePage: 2, orphan: false, content: [{ type: 'text', text: 'حاشية' }], text: 'حاشية', dom: { getBoundingClientRect: () => rectOf(RECT) } } }, handlers: {}, options: null };
Object.assign(ed, {
  dom: { classList: { toggle: (c, on) => ed.calls.push(['class', c, on]) }, querySelectorAll: () => [] },
  view: { posAtDOM: () => 0 },
  on(name, fn) { ed.handlers[name] = fn; return ed; },
  getContent() { return ed.content; }, getJSON() { return { type: 'doc', content: ed.content }; },
  setContent(c) { ed.content = c; ed.calls.push(['setContent', JSON.stringify(c).length]); return ed; },
  setEditable(on) { ed.editable = on; ed.calls.push(['editable', on]); },
  focus(w) { ed.calls.push(['focus', w]); focused.push('editor'); }, blur() { ed.calls.push(['blur']); }, destroy() { ed.calls.push(['destroy']); },
  wordCount() { return 5; }, currentStyle() { return ed.style; }, isBold() { return ed.bold; }, isItalic() { return ed.italic; },
  canUndo() { return ed.undo > 0; }, canRedo() { return false; },
  undo() { ed.undo -= 1; ed.calls.push(['undo']); }, redo() { ed.calls.push(['redo']); },
  toggleBold() { ed.bold = !ed.bold; ed.calls.push(['bold']); }, toggleItalic() { ed.italic = !ed.italic; ed.calls.push(['italic']); },
  setStyle(k) { ed.style = k; ed.calls.push(['style', k]); return true; },
  caretBlock() { return ed.block; }, goToBlock(id) { ed.calls.push(['goTo', id]); return id === 'p11' || id === 'p22'; },
  find(q, o, keep) { ed.calls.push(['find', q, o, keep]); return ed.findTable; },
  findNext(dir) { ed.calls.push(['findNext', dir]); ed.findTable.index = (ed.findTable.index + dir + 3) % 3; return { from: 10, to: 14, block: 'p11', note: ed.findTable.index === 2 ? 'n1' : null, index: ed.findTable.index, total: 3 }; },
  findCurrent() { return { from: 10, to: 14, block: 'p11', note: null }; },
  replaceCurrent(t) { ed.calls.push(['replaceCurrent', t]); return true; }, replaceAll(t) { ed.calls.push(['replaceAll', t]); return 3; }, clearFind() { ed.calls.push(['clearFind']); return ed; },
  insertFootnote() { ed.calls.push(['insertFootnote']); ed.notes.ne1 = { id: 'ne1', seq: 2, sourcePage: 2, orphan: false, content: [], text: '', dom: { getBoundingClientRect: () => rectOf(RECT) } }; return { id: 'ne1', pos: 9, dom: ed.notes.ne1.dom }; },
  noteAt(id) { return ed.notes[id] || null; }, setNoteContent(id, c) { ed.calls.push(['setNote', id, c]); ed.notes[id].content = c; return true; }, deleteNote(id) { ed.calls.push(['deleteNote', id]); delete ed.notes[id]; return true; },
  acceptUncertain(a, b) { ed.calls.push(['accept', a, b]); return true; }, replaceRange(a, b, t) { ed.calls.push(['replaceRange', a, b, t]); return true; },
});
const noteStub = { calls: [], focus: (w) => noteStub.calls.push(['focus', w]), destroy: () => noteStub.calls.push(['destroy']), setContent: (c) => noteStub.calls.push(['set', c]), getContent: () => [], toggleBold: () => noteStub.calls.push(['bold']), toggleItalic: () => {} };
globalThis.NassakhEditor = {
  STYLES: fixture.styles, wordCount: (c) => (c && c.content ? c.content.length * 2 : 0),
  chapterListHtml: (chapters, current) => chapters.map((c) => `<button data-cid="${c.id}"${c.id === current ? ' class="is-current"' : ''}>${c.title}</button>`).join(''),
  create(el, options) { ed.options = options; ed.content = options.content; ed.editable = options.editable; ed.calls.push(['create', el && el.tag, options.editable]); return ed; },
  createNote(el, options) { noteStub.options = options; noteStub.calls.push(['create', JSON.stringify(options.content)]); return noteStub; },
};
eval(fs.readFileSync(process.argv[2], 'utf8'));
inits.forEach((fn) => fn());
const clone = (v) => JSON.parse(JSON.stringify(v));
const flush = async () => { for (let i = 0; i < 6; i += 1) await new Promise((r) => setImmediate(r)); };
const runTimers = (ms) => { const due = timers.filter((t) => t.ms === ms); timers.length = 0; due.forEach((t) => t.fn()); return due.length; };
const reqs = (method) => calls.filter((x) => x[0] === method);
const toasts = () => calls.filter((x) => x[0] === 'toast').map((x) => x[1]);
// ---- fetch routes
const routes = { put: null, chapter: null, state: null, find: null };
globalThis.fetch = async (url, init) => {
  const method = (init && init.method) || 'GET';
  const body = init && init.body ? JSON.parse(init.body) : null;
  calls.push([method, url, body, init && init.headers]);
  const json = (status, data) => ({ ok: status < 400, status, json: async () => clone(data) });
  if (url === '/api/books/1/chapters/') return json(200, fixture.chapters.map((c) => ({ ...c, drift: routes.state && routes.state.stale === false ? false : c.drift })));
  if (method === 'PUT' && url.startsWith('/api/books/1/chapters/')) return routes.put ? routes.put() : json(200, { version: 'v2', id: 'h1', chapters: [{ id: 'h1', version: 'v2', title: 'الفصل الأول' }], reload: false, manuscript_version: 2, changed: true });
  if (url.startsWith('/api/books/1/chapters/') && url.endsWith('/reassemble/')) return json(202, { run_id: 5, status: 'queued', stage: '' });
  if (url.startsWith('/api/books/1/chapters/')) { const cid = url.split('/')[5]; const c = fixture.docs[cid]; return c ? json(200, c) : json(404, { detail: 'الفصل غير موجود' }); }
  if (url === '/api/books/1/find-replace/') return routes.find ? routes.find(body) : json(200, body.replace ? { matches: [], total: 7, replaced: 7, snapshot: 41, manuscript_version: 3, version: 'v9' } : { matches: [], total: 7, replaced: 0 });
  if (url === '/api/books/1/convert-digits/') return json(200, { changed: 4, snapshot: 42, manuscript_version: 4, version: 'v10' });
  if (url === '/api/books/1/snapshots/') return method === 'POST' ? json(201, { id: 50, version: 4, label: body.label, reason: 'manual', reason_label: 'يدوية', created_by: 'owner', created_at: '2026-09-25T10:00:00Z', current: true }) : json(200, fixture.snapshots);
  if (/\/api\/books\/1\/snapshots\/\d+\/restore\/$/.test(url)) return json(200, { version: 5, snapshot: 60, restored: parseInt(url.split('/')[5], 10) });
  if (url === '/api/books/1/manuscript/state/') return json(200, routes.state || { active: false, run: null, stale: true, stale_pages: [2, 9] });
  if (url.startsWith('/api/books/1/sheets/')) return json(200, { pages: [{ ...fixture.sheet, number: parseInt(url.split('from=')[1], 10) }] });
  if (url === '/api/pages/7/review/') return json(200, fixture.review);
  return json(404, { detail: 'لا' });
};
const make = (over) => {
  const c = reg.bookEditor(clone({ ...fixture.config, ...(over || {}) }));
  c.$el = { querySelector: (sel) => (sel === '[data-ed-list]' ? c.listHost : null) };
  c.listHost = { innerHTML: '', querySelector: () => null };
  c.$refs = { sheet: { tag: 'sheet' }, column: { getBoundingClientRect: () => rectOf({ top: -100, left: 100, width: 700, height: 3000 }) }, pop: { offsetWidth: 0, offsetHeight: 0 }, findQuery: { focus: () => focused.push('findQuery'), select: () => {} }, findPanel: { offsetHeight: 100 }, noteHost: { textContent: '' }, drawerClose: { focus: () => focused.push('drawerClose') }, sheetClose: { focus: () => focused.push('sheetClose') }, snapshotLabel: { focus: () => focused.push('snapshotLabel') }, wordTyped: { focus: () => focused.push('wordTyped'), select: () => {} } };
  c.$nextTick = (fn) => fn();
  c.init();
  return c;
};
(async () => {
  const out = {};
  // ---- loading: the list and the chapter, the editor mounted with the content, the URL kept
  const c = make();
  out.loading = c.phase;
  await flush();
  out.ready = { phase: c.phase, chapter: c.chapter.id, title: c.chapterTitle, version: c.version, words: c.words, count: c.chapters.length, list: c.listHost.innerHTML, created: ed.calls[0], status: c.statusText, pill: c.pill, style: c.style, url: reqs('url').slice(-1)[0], reqs: reqs('GET').map((r) => r[1]), drift: c.chapter.drift, driftPages: c.driftPages, driftText: c.driftText, layoutUrl: c.layoutUrl };
  // «الاحتفاظ بالنص»: the drift banner stays away for this chapter when the list is refreshed after a save
  c.dismissDrift(); await c.refreshChapters(); out.driftDismissed = [c.chapter.drift, c.driftPages];
  // ---- autosave: an update marks the chapter dirty and arms 1.5 s; the PUT carries the content and the version
  calls.length = 0; timers.length = 0;
  ed.handlers.update();
  out.dirty = { dirty: c.dirty, pill: c.pill, timer: timers.filter((t) => t.ms === 1500).length };
  ed.handlers.update(); out.rearmed = timers.filter((t) => t.ms === 1500).length;
  out.guardDirty = c.guardUnload({ preventDefault: () => calls.push(['prevented']) });
  runTimers(1500); out.savingPill = c.pill;
  await flush();
  const put = reqs('PUT')[0];
  out.saved = { url: put[1], body: Object.keys(put[2]), version: put[2].version, csrf: 'X-CSRFToken' in put[3], newVersion: c.version, pill: c.pill, dirty: c.dirty, pagesStale: c.pagesStale, refresh: timers.filter((t) => t.ms === 25000).length, guard: c.guardUnload(null) };
  runTimers(25000); await flush(); out.refreshed = { pagesStale: c.pagesStale, reqs: reqs('GET').filter((r) => r[1] === '/api/books/1/chapters/').length };
  // ---- a 409: the conflict shows with the other window's text; reloading takes it, keeping mine snapshots first
  calls.length = 0; routes.put = () => ({ ok: false, status: 409, json: async () => ({ detail: 'تغيّر هذا الفصل في نافذة أخرى.', id: 'h1', version: 'v7', content: { type: 'doc', content: [{ type: 'paragraph', content: [] }] } }) });
  ed.handlers.update(); await c.saveNow(); await flush();
  out.conflict = { state: c.save.state, pill: c.pill, open: c.conflict.open, version: c.conflict.version, dirty: c.dirty };
  await c.reloadConflict();
  out.reloaded = { open: c.conflict.open, version: c.version, dirty: c.dirty, set: ed.calls.filter((x) => x[0] === 'setContent').length, pill: c.pill };
  // a 409 without the other text: reloading fetches the server's copy instead of pretending it is saved
  routes.put = () => ({ ok: false, status: 409, json: async () => ({ detail: 'x', id: 'h1', version: 'v8', content: null }) });
  ed.handlers.update(); await c.saveNow(); calls.length = 0;
  await c.reloadConflict(); await flush();
  out.reloadedNoText = [c.conflict.open, reqs('GET')[0] && reqs('GET')[0][1], c.version, c.dirty];
  routes.put = () => ({ ok: false, status: 409, json: async () => ({ detail: 'x', id: 'h1', version: 'v8', content: null }) });
  ed.handlers.update(); await c.saveNow(); routes.put = null; calls.length = 0;
  await c.keepMine(); await flush();
  out.keptMine = { snapshot: reqs('POST')[0] && reqs('POST')[0][2].label.startsWith('نص نافذة أخرى'), put: reqs('PUT')[0] && reqs('PUT')[0][2].version, version: c.version, pill: c.pill };
  // ---- a failed save: the pill offers a retry, the text stays dirty; the retry succeeds
  routes.put = () => ({ ok: false, status: 500, json: async () => ({}) });
  ed.handlers.update(); await c.saveNow(); await flush();
  out.failed = { state: c.save.state, pill: c.pill, dirty: c.dirty, guard: c.guardUnload(null) };
  routes.put = null; await c.saveNow(); await flush(); out.retried = { state: c.save.state, dirty: c.dirty };
  // ---- a split: the server asks for a reload; the list and the chapter are fetched again before any save
  routes.put = () => ({ ok: true, status: 200, json: async () => ({ version: null, id: 'h1', chapters: [{ id: 'h1', version: 'v3', title: 'الفصل الأول' }, { id: 'h9', version: 'v1', title: 'جديد' }], reload: true, manuscript_version: 5, changed: true }) });
  ed.block = { id: 'p22', type: 'paragraph', style: 'paragraph', sourcePages: [4], sourceLineIds: [], reviewed: true, src: '4', pos: 40, index: 3 };
  calls.length = 0; ed.handlers.update(); await c.saveNow(); await flush(); await flush();
  out.split = { reloadPending: c.reloadPending, version: c.version, chapter: c.chapter.id, gets: reqs('GET').map((r) => r[1]), toast: toasts().slice(-1)[0], goTo: ed.calls.filter((x) => x[0] === 'goTo').map((x) => x[1]) };
  routes.put = null; ed.block = fixture.block;
  // ---- keyboard map (pure) and dispatch
  const ka = c.keyAction; const base = { inField: false, inEditor: false, inNote: false };
  const mod = (k, extra) => ({ key: k, code: 'Key' + k.toUpperCase(), metaKey: true, ...(extra || {}) });
  out.keys = { save: ka(mod('s'), base), saveInEditor: ka(mod('s'), { ...base, inEditor: true }), saveInField: ka(mod('s'), { ...base, inField: true }), find: ka(mod('f'), base), footnote: ka(mod('f', { shiftKey: true }), base),
    prev: ka({ key: '[', code: 'BracketLeft', metaKey: true }, base), next: ka({ key: ']', code: 'BracketRight', metaKey: true }, base), undo: ka(mod('z'), base), redo: ka(mod('z', { shiftKey: true }), base), bold: ka(mod('b'), base), italic: ka(mod('i'), base),
    h1: ka({ key: '¡', code: 'Digit1', metaKey: true, altKey: true }, base), h2: ka({ key: '™', code: 'Digit2', metaKey: true, altKey: true }, base), p: ka({ key: 'º', code: 'Digit0', metaKey: true, altKey: true }, base), quote: ka({ key: '£', code: 'Digit3', metaKey: true, altKey: true }, base), sep: ka({ key: '§', code: 'Digit6', metaKey: true, altKey: true }, base),
    sheet: ka({ key: '?' }, base), sheetArabic: ka({ key: '؟' }, base), sheetInEditor: ka({ key: '?' }, { ...base, inEditor: true }), o: ka({ key: 'o', code: 'KeyO' }, base), oInEditor: ka({ key: 'o', code: 'KeyO' }, { ...base, inEditor: true }), oMod: ka(mod('o', { shiftKey: true }), { ...base, inEditor: true }), esc: ka({ key: 'Escape' }, { ...base, inField: true }), plain: ka({ key: 'f' }, base), arabic: ka({ key: 'ل', code: 'KeyG' }, { ...base, inEditor: true }), ctrl: ka({ key: 's', code: 'KeyS', ctrlKey: true }, base) };
  const key = (ev, target) => { let prevented = false; c.onKey({ ...ev, target: target || { tagName: 'BODY', closest: () => null }, preventDefault: () => { prevented = true; } }); return prevented; };
  const inEditor = { tagName: 'DIV', closest: (sel) => (sel === '.ed-doc' ? {} : null) };
  const inInput = { tagName: 'INPUT', closest: () => null };
  focused.length = 0;
  out.findOpen = [key(mod('f'), inEditor), c.find.open, focused.slice(-1)[0], c.topLayer()];
  out.escFind = [key({ key: 'Escape' }, inInput), c.find.open, ed.calls.filter((x) => x[0] === 'focus').length > 0];
  out.escBlur = [key({ key: 'Escape' }, inEditor), ed.calls.slice(-1)[0][0]];
  ed.calls.length = 0;
  out.boldHandled = [key({ ...mod('b'), defaultPrevented: true }, inEditor), ed.calls.length];
  out.boldOutside = [key(mod('b')), ed.calls.slice(-1)[0], c.bold];
  out.styleKey = [key({ key: '¡', code: 'Digit1', metaKey: true, altKey: true }), ed.calls.slice(-1)[0], c.style];
  calls.length = 0; ed.handlers.update(); out.saveKey = [key(mod('s'), inEditor)]; await flush(); out.saveKey.push(reqs('PUT').length);
  // ---- find & replace: the count follows the query after 150 ms, Enter / ⇧Enter cycle, options re-run
  c.openFind(); timers.length = 0; c.find.query = 'برقة'; c.onFindInput();
  out.findTimer = timers.filter((t) => t.ms === 150).length; ed.calls.length = 0; runTimers(150);
  out.find = { call: ed.calls[0], count: c.findCountText, total: c.find.total, index: c.find.index };
  c.onFindKey({ key: 'Enter', shiftKey: false, preventDefault: () => {} }); out.findNext = [ed.calls.slice(-1)[0], c.find.index, c.findCountText];
  c.onFindKey({ key: 'Enter', shiftKey: true, preventDefault: () => {} }); out.findPrev = [ed.calls.slice(-1)[0], c.find.index];
  c.find.wholeWord = true; c.runFind(); out.findOptions = ed.calls.slice(-1)[0][2];
  c.find.replacement = 'بَرقة'; ed.calls.length = 0; out.replaceOne = [c.replaceOne(), ed.calls.map((x) => x[0]), c.dirty];
  ed.calls.length = 0; out.replaceAll = [await c.replaceAll(), ed.calls[0], stores.editorToast.message, typeof stores.editorToast.action];
  stores.editorToast.run(); out.replaceUndo = ed.calls.slice(-1)[0];
  // a match in a footnote opens the note's editor; the find in the book scope counts through the server
  ed.findTable.index = 1; c.findStep(1); out.noteMatch = [c.pop.kind, c.note.id]; c.closePop();
  calls.length = 0; timers.length = 0; c.setFindScope('book'); runTimers(300); await flush();
  out.bookCount = { post: reqs('POST')[0] && reqs('POST')[0][2], count: c.findCountText, total: c.find.bookTotal };
  // an older count (the options changed while it was on the wire) never lands on the newer query
  let resolveFind = null; routes.find = () => new Promise((res) => { resolveFind = res; });
  timers.length = 0; c.scheduleBookCount(); runTimers(300); const firstCount = resolveFind;
  c.find.matchTashkeel = true; c.scheduleBookCount(); runTimers(300); const secondCount = resolveFind;
  firstCount({ ok: true, status: 200, json: async () => ({ matches: [], total: 99, replaced: 0 }) }); await flush();
  const afterOld = c.find.bookTotal;
  secondCount({ ok: true, status: 200, json: async () => ({ matches: [], total: 2, replaced: 0 }) }); await flush();
  out.bookCountRace = [afterOld, c.find.bookTotal]; routes.find = null; c.find.matchTashkeel = false;
  calls.length = 0; ed.calls.length = 0; c.dirty = false;
  out.bookReplace = [await c.replaceAllInBook(), reqs('POST')[0][2].replace, reqs('GET').map((r) => r[1]), stores.editorToast.message];
  calls.length = 0; stores.editorToast.run(); await flush(); out.bookUndo = reqs('POST')[0] && reqs('POST')[0][1];
  c.closeFind(); out.findClosed = [c.find.open, ed.calls.slice(-1)[0][0]];
  // ---- the style picker and the marks
  ed.calls.length = 0; c.styleMenu = true; out.style = [c.setStyle('heading2'), c.styleMenu, ed.calls[0], c.style, c.styleLabel];
  ed.style = 'quote'; ed.handlers.selection(); out.styleFollows = [c.style, c.styleLabel];
  c.toggleBold(); out.bold = [c.bold, ed.calls.slice(-1)[0][0]];
  // ---- footnotes: «حاشية» inserts and opens the small editor; Esc closes it and destroys the note editor
  ed.calls.length = 0; noteStub.calls.length = 0;
  out.noteInsert = [c.setStyle('footnote'), c.pop.kind, c.note.id, c.note.seq, noteStub.calls[0], c.pop.style, c.topLayer(), c.dirty];
  // the toolbar's click opened it: the outside-click of the same event (Alpine paints the overlay in a microtask
  // before the document's listener runs) is no close; a click after the event turn is
  await flush();
  const outside = { target: { closest: () => null } };
  out.noteOutside = [c.onPopOutside(outside), c.pop.kind, runTimers(0), c.onPopOutside(outside), c.pop.kind];
  c.openNote({ id: 'ne1' });
  noteStub.options.onUpdate([{ type: 'text', text: 'نص' }]); out.noteTyped = ed.calls.filter((x) => x[0] === 'setNote').slice(-1)[0];
  // Enter in the note is done: the popover closes and the text takes the caret back
  out.noteEnter = [typeof noteStub.options.onSubmit, (noteStub.options.onSubmit(), c.pop.kind), ed.calls.slice(-1)[0][0]];
  c.openNote({ id: 'ne1' });
  out.noteEsc = [c.closeTop(), c.pop.kind, noteStub.calls.slice(-1)[0][0]];
  ed.options.onFootnote({ id: 'n1', pos: 3, dom: ed.notes.n1.dom }); out.noteClick = [c.pop.kind, c.note.id, c.note.sourcePage];
  out.noteDelete = [c.deleteNote(), c.pop.kind, ed.calls.slice(-1)[0], stores.editorToast.message];
  // the undo toast of an editor-history undo retires with the next edit (it would undo that edit instead)
  out.toastRetired = [stores.editorToast.visible, stores.editorToast.local, (ed.handlers.update(), stores.editorToast.visible), stores.editorToast.action];
  // ---- uncertain words: the readings come from the review payload of the block's page
  calls.length = 0; ed.calls.length = 0;
  await c.openWord({ from: 30, to: 36, text: 'مليتية', dom: { getBoundingClientRect: () => rectOf(RECT) } }); await flush();
  out.word = { kind: c.pop.kind, readings: c.word.readings, loading: c.word.loading, reqs: reqs('GET').map((r) => r[1]), typed: c.word.typed, focus: focused.slice(-1)[0] };
  c.chooseReading('مليتة'); out.wordChosen = [ed.calls.slice(-1)[0], c.pop.kind, c.dirty];
  await c.openWord({ from: 30, to: 36, text: 'مليتية', dom: {} }); c.acceptWord(); out.wordAccepted = ed.calls.slice(-1)[0];
  await c.openWord({ from: 30, to: 36, text: 'مليتية', dom: {} }); c.word.typed = ' مليتيه '; c.submitTyped(); out.wordTyped = ed.calls.slice(-1)[0];
  // ---- the source pane follows the caret (250 ms), the drawer opens on O and closes on Esc
  calls.length = 0; timers.length = 0; ed.block = { id: 'p12', type: 'paragraph', style: 'paragraph', sourcePages: [2, 3], sourceLineIds: [10, 11], reviewed: true, src: '2–3', pos: 5, index: 1 };
  ed.handlers.selection(); out.sourceTimer = timers.filter((t) => t.ms === 250).length; runTimers(250); await flush();
  out.source = { pages: c.source.pages, page: c.sourcePage, lines: c.source.lines, loading: c.source.loading, boxes: c.sourceBoxes().map((b) => b.id), req: reqs('GET').map((r) => r[1]), aspect: c.sourceAspect, box: c.boxStyle([0.1, 0.2, 0.9, 0.25]) };
  focused.length = 0; out.drawer = [key({ key: 'o', code: 'KeyO' }), c.drawerOpen, focused[0], c.topLayer()];
  await c.sourceStep(1); out.drawerStep = [c.source.index, c.sourcePage, reqs('GET').map((r) => r[1]), c.source.sheet && c.source.sheet.number];
  out.drawerEsc = [c.closeTop(), c.drawerOpen];
  out.oInEditor = [key({ key: 'o', code: 'KeyO' }, inEditor), c.drawerOpen];
  // a neighbouring block on the same scan: the sheet stays (no skeleton, no request), only the line bands move
  calls.length = 0; timers.length = 0; ed.block = { id: 'p13', type: 'paragraph', style: 'paragraph', sourcePages: [3], sourceLineIds: [12], reviewed: true, src: '3', pos: 9, index: 2 };
  ed.handlers.selection(); runTimers(250); await flush();
  out.sourceSame = [c.source.loading, c.source.sheet && c.source.sheet.number, c.source.lines, reqs('GET').length, c.source.pages];
  // ---- chapters: a switch flushes the save, loads the chapter, marks the row, updates the URL; the ends toast
  c.dirty = true; calls.length = 0; ed.calls.length = 0;
  out.switch = [await c.openChapter('h20'), reqs('PUT').length, reqs('GET').map((r) => r[1]), c.chapter.id, c.version, ed.calls.filter((x) => x[0] === 'setContent').length, c.listHost.innerHTML.includes('data-cid="h20" class="is-current"'), reqs('url').slice(-1)[0][1], c.statusText];
  out.listClick = (c.onListClick({ target: { closest: () => ({ getAttribute: () => 'h1' }) }, preventDefault: () => {} }), await flush(), c.chapter.id);
  calls.length = 0; out.ends = [await c.prevChapter(), toasts().slice(-1)[0], await c.nextChapter(), c.chapter.id];
  await c.prevChapter(); out.prevOk = c.chapter.id;
  routes.put = () => ({ ok: false, status: 500, json: async () => ({}) }); c.dirty = true; calls.length = 0;
  out.switchBlocked = [await c.openChapter('h20'), c.chapter.id, toasts().slice(-1)[0]]; routes.put = null; c.dirty = false;
  // ---- a save in flight when the chapter switches: the switch waits for it, its answer lands on the chapter
  // it was made for (the title, the list row), never on the chapter opened next
  let resolvePut = null; routes.put = () => new Promise((res) => { resolvePut = res; });
  ed.handlers.update(); const pendingSave = c.saveNow(); await flush();
  const switching = c.openChapter('h20'); await flush();
  const midSwitch = { chapter: c.chapter.id, pill: c.pill.state };
  resolvePut({ ok: true, status: 200, json: async () => ({ version: 'v77', id: 'h1', chapters: [{ id: 'h1', version: 'v77', title: 'عنوان جديد' }], reload: false, manuscript_version: 9, changed: true }) });
  await pendingSave; await switching; await flush();
  out.inflight = { midSwitch, chapter: c.chapter.id, version: c.version, title: c.chapterTitle, h1Row: c.chapters.find((x) => x.id === 'h1').title, pill: c.pill.state };
  // typed while the PUT was on the wire: the pill stays «غير محفوظ» and the next save is armed
  await c.openChapter('h1'); timers.length = 0;
  ed.handlers.update(); const pendingSave2 = c.saveNow(); await flush(); ed.handlers.update();
  const armedBefore = timers.filter((t) => t.ms === 1500).length;
  resolvePut({ ok: true, status: 200, json: async () => ({ version: 'v78', id: 'h1', chapters: [{ id: 'h1', version: 'v78', title: 'الفصل الأول' }], reload: false, manuscript_version: 10, changed: true }) });
  await pendingSave2; await flush();
  out.typedDuringSave = [c.pill.state, c.dirty, c.version, armedBefore, timers.filter((t) => t.ms === 1500).length];
  routes.put = null; c.dirty = false; c.save = { state: 'saved', message: '' };
  // ---- snapshots, digits, re-assembly
  calls.length = 0; await c.openSnapshots(); c.snapshots.label = 'قبل التنسيق'; out.snapshots = [c.snapshots.open, c.snapshots.list.length, focused.slice(-1)[0]];
  out.snapshotCreate = [await c.createSnapshot(), reqs('POST')[0][2], c.snapshots.list[0].id, c.snapshots.list.length];
  calls.length = 0; out.snapshotRestore = [await c.restoreSnapshot(3), reqs('POST')[0][1], c.snapshots.open, reqs('GET')[0][1], stores.editorToast.message];
  // a server-side undo (the snapshot before the restore) outlives typing
  out.toastKept = [stores.editorToast.local, (ed.handlers.update(), stores.editorToast.visible)]; c.dirty = false;
  // a dialog gives the focus back to the control that opened it
  document.activeElement = { focus: () => focused.push('trigger'), isConnected: true }; c.openSheet(); c.closeSheet(); document.activeElement = null;
  out.sheetFocus = focused.slice(-2);
  calls.length = 0; c.digits.style = 'arabic_indic'; c.digits.open = true; out.digits = [await c.convertDigits(), reqs('POST')[0][2], c.digits.open, stores.editorToast.message];
  calls.length = 0; timers.length = 0; ed.calls.length = 0; out.reassemble = [await c.reassembleChapter(), reqs('POST')[0][1], c.reassembly.running, ed.calls[0], timers.filter((t) => t.ms === 1000).length];
  routes.state = { active: true, run: { id: 5, status: 'running', stage: 'collect' } }; runTimers(1000); await flush(); out.reassemblePolling = [c.reassembly.running, timers.filter((t) => t.ms === 1000).length];
  routes.state = { active: false, run: { id: 5, status: 'done', stage: 'save' }, stale: false, stale_pages: [] }; calls.length = 0; runTimers(1000); await flush();
  out.reassembled = [c.reassembly.running, ed.editable, reqs('GET').map((r) => r[1]), toasts().slice(-1)[0], c.chapter.drift];
  // ---- zoom and page marks are remembered; the sheet's vars and faces
  c.setZoom(120); c.togglePageMarks();
  out.prefs = [store['nassakh.editor.zoom'], store['nassakh.editor.marks'], ed.calls.slice(-1)[0], c.zoom, c.pageMarks];
  out.vars100 = NassakhEditorUI.sheetVars(fixture.config.stylesheet, 100); out.vars120 = NassakhEditorUI.sheetVars(fixture.config.stylesheet, 120);
  out.fonts = NassakhEditorUI.fontCss(fixture.config.faces);
  out.fontsSystem = NassakhEditorUI.fontCss({ body: { key: 'simplified_arabic', family: 'Simplified Arabic' }, latin: { key: 'times', family: 'Times New Roman' }, heading: { key: 'lotus', family: 'Lotus Linotype Exnd' } });
  out.status = NassakhEditorUI.statusText({ number: 3, count: 14, pages: { first: 31, last: 58 } }, 2184, {});
  out.statusStale = NassakhEditorUI.statusText({ number: 1, count: 2, pages: null }, 1, { pagesStale: true, readOnly: true });
  out.place = NassakhEditorUI.placeAgainst({ top: 700, bottom: 720, left: 300, right: 340 }, { top: 100, bottom: 792, left: 108, right: 792 }, { top: -100, left: 100, width: 700, height: 3000, bottom: 2900, right: 800 }, [380, 132], true);
  // ---- a proofreader: nothing saves, the pill hides
  const r = make({ canEdit: false }); await flush(); ed.handlers.update();
  out.readOnly = [r.dirty, r.pill, r.setStyle('quote'), ed.options.editable, r.statusText];
  // ---- no bundle, no manuscript
  const bundle = globalThis.NassakhEditor; globalThis.NassakhEditor = undefined;
  const e = make(); out.noBundle = [e.phase, e.errorHeadline.includes('static/dist/editor.js')]; globalThis.NassakhEditor = bundle;
  out.noManuscript = make({ exists: false }).phase;
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


def _chapter_payload(cid: str, number: int, title: str, prev, nxt, pages=None, drift=False) -> dict:
    return {
        "id": cid,
        "version": f"{cid}-v1",
        "content": {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "attrs": {"id": "p11", "sourcePages": [2]},
                    "content": [{"type": "text", "text": "نص"}],
                }
            ],
        },
        "warnings": [
            {
                "code": "uncertain_words",
                "message": "كلمة غير محسومة",
                "page": 2,
                "blockId": "p11",
                "severity": "info",
            }
        ],
        "number": number,
        "kind": "chapter",
        "title": title,
        "prev": prev,
        "next": nxt,
        "count": 2,
        "source_pages": {"first": 2, "last": 3},
        "pages": pages,
        "drift": drift,
        "manuscript_version": 1,
        "origin": "assembly",
    }


def test_editor_component_under_node(tmp_path):
    fixture = {
        "config": {
            "page": "editor",
            "bookId": 1,
            "title": "كتاب",
            "author": "",
            "exists": True,
            "manuscriptVersion": 1,
            "origin": "assembly",
            "chapter": "h1",
            "requestedChapter": "h1",
            "chapters": [
                {"id": "h1", "number": 1, "kind": "chapter", "title": "الفصل الأول"},
                {"id": "h20", "number": 2, "kind": "chapter", "title": "الفصل الثاني"},
            ],
            "canEdit": True,
            "stylesheet": {
                "trim": "17x24",
                "width_mm": 170,
                "height_mm": 240,
                "top_mm": 20,
                "bottom_mm": 22,
                "inner_mm": 22,
                "outer_mm": 18,
                "body_size_pt": 13,
                "line_height": 1.7,
                "indent_em": 1.5,
                "heading_scale": {"h1": 1.6, "h2": 1.25},
                "footnote_size_pt": 10,
            },
            "faces": {
                "body": {
                    "key": "amiri",
                    "requested": "amiri",
                    "name": "Amiri",
                    "family": "Amiri",
                    "fallback": False,
                },
                "latin": {
                    "key": "times",
                    "requested": "times",
                    "name": "Times New Roman",
                    "family": "Times New Roman",
                    "fallback": False,
                },
                "heading": {
                    "key": "amiri",
                    "requested": "amiri",
                    "name": "Amiri",
                    "family": "Amiri",
                    "fallback": False,
                },
            },
            "autosaveMs": 1500,
            "urls": {
                "editor": "/books/1/editor/",
                "layout": "/books/1/layout/",
                "chapters": "/api/books/1/chapters/",
                "chapter": "/api/books/1/chapters/__cid__/",
                "reassemble": "/api/books/1/chapters/__cid__/reassemble/",
                "findReplace": "/api/books/1/find-replace/",
                "convertDigits": "/api/books/1/convert-digits/",
                "snapshots": "/api/books/1/snapshots/",
                "restore": "/api/books/1/snapshots/__sid__/restore/",
                "stylesheet": "/api/books/1/stylesheet/",
                "preview": "/api/books/1/preview/",
                "manuscriptState": "/api/books/1/manuscript/state/",
                "manuscript": "/books/1/manuscript/",
                "dashboard": "/books/1/",
                "sheets": "/api/books/1/sheets/",
                "review": "/books/1/review/__n__/",
            },
        },
        "styles": [
            {"key": "paragraph", "label": "فقرة", "keys": "⌘⌥0"},
            {"key": "heading2", "label": "عنوان فرعي", "keys": "⌘⌥2"},
            {"key": "quote", "label": "اقتباس", "keys": "⌘⌥3"},
            {"key": "footnote", "label": "حاشية", "keys": "⌘⇧F"},
        ],
        "chapters": [
            {
                "id": "h1",
                "number": 1,
                "kind": "chapter",
                "title": "الفصل الأول",
                "version": "h1-v1",
                "blocks": 3,
                "words": 40,
                "pages": {"first": 3, "last": 7},
                "source_pages": {"first": 2, "last": 3},
                "drift": True,
            },
            {
                "id": "h20",
                "number": 2,
                "kind": "chapter",
                "title": "الفصل الثاني",
                "version": "h20-v1",
                "blocks": 2,
                "words": 12,
                "pages": {"first": 8, "last": 9},
                "source_pages": {"first": 4, "last": 5},
                "drift": False,
            },
        ],
        "docs": {
            "h1": _chapter_payload("h1", 1, "الفصل الأول", None, "h20", {"first": 3, "last": 7}, True),
            "h20": _chapter_payload("h20", 2, "الفصل الثاني", "h1", None, {"first": 8, "last": 9}),
        },
        "block": {
            "id": "p11",
            "type": "paragraph",
            "style": "paragraph",
            "sourcePages": [2],
            "sourceLineIds": [10, 11],
            "reviewed": True,
            "src": "2",
            "pos": 5,
            "index": 1,
        },
        "sheet": {
            "id": 7,
            "number": 2,
            "width": 1000,
            "height": 1500,
            "display_url": "/media/p2.webp",
            "scan_url": "/media/p2.png",
            "lines": [
                {"id": 10, "bbox": [0.1, 0.1, 0.9, 0.13], "tokens": []},
                {"id": 11, "bbox": [0.1, 0.14, 0.9, 0.17], "tokens": []},
                {"id": 12, "bbox": [0.1, 0.2, 0.9, 0.23], "tokens": []},
            ],
        },
        "review": {
            "labels": {"primary": "Qari v0.3", "secondary": "Qari v0.2"},
            "lines": [
                {
                    "id": 10,
                    "tokens": [
                        {"t": "مليتية", "alt": "مليتة", "tess": "ملبتية", "conf": "low"},
                        {"t": "قصر", "alt": None, "tess": None, "conf": "high"},
                    ],
                },
                {"id": 99, "tokens": [{"t": "مليتية", "alt": "أخرى", "tess": None, "conf": "low"}]},
            ],
        },
        "snapshots": [
            {
                "id": 3,
                "version": 2,
                "label": "قبل",
                "reason": "manual",
                "reason_label": "يدوية",
                "created_by": "owner",
                "created_at": "2026-09-25T09:00:00Z",
                "current": False,
            },
            {
                "id": 4,
                "version": 3,
                "label": "",
                "reason": "edit",
                "reason_label": "قبل تعديل",
                "created_by": "",
                "created_at": "2026-09-25T09:30:00Z",
                "current": True,
            },
        ],
    }
    (tmp_path / "fixture.json").write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    run = subprocess.run(
        ["node", str(harness), str(JS / "editor.js"), str(tmp_path / "fixture.json")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])

    # loading, then ready: the list rendered with the current row, the editor created on the sheet, the URL
    # kept
    assert out["loading"] == "loading"
    ready = out["ready"]
    assert (
        ready["phase"] == "ready"
        and ready["chapter"] == "h1"
        and ready["version"] == "h1-v1"
        and ready["count"] == 2
    )
    assert (
        ready["created"] == ["create", "sheet", True]
        and ready["words"] == 5
        and ready["style"] == "paragraph"
    )
    assert 'data-cid="h1" class="is-current"' in ready["list"] and "الفصل الثاني" in ready["list"]
    assert ready["status"] == "الفصل 1 من 2 · 5 كلمات · ص 3–7 في الكتاب" and ready["pill"] == {
        "state": "idle",
        "text": "",
    }
    assert (
        ready["url"] == ["url", "http://x/books/1/editor/?chapter=h1"]
        and ready["layoutUrl"] == "/books/1/layout/?chapter=h1"
    )
    assert (
        ready["reqs"][:2] == ["/api/books/1/chapters/", "/api/books/1/chapters/h1/"]
        and "/api/books/1/manuscript/state/" in ready["reqs"]
    )
    assert (
        ready["drift"] is True
        and ready["driftPages"] == [2]
        and ready["driftText"] == "تغيّر نص صفحة واحدة في المراجعة بعد التحرير:"
    )
    assert out["driftDismissed"] == [False, []]  # «الاحتفاظ بالنص» survives the list's refresh
    # autosave: dirty at once, one 1.5 s timer (re-armed by the next change), the PUT with the version
    assert (
        out["dirty"] == {"dirty": True, "pill": {"state": "dirty", "text": "غير محفوظ"}, "timer": 1}
        and out["rearmed"] == 2
    )
    assert out["guardDirty"] is True and out["savingPill"] == {"state": "saving", "text": "يُحفظ…"}
    saved = out["saved"]
    assert (
        saved["url"] == "/api/books/1/chapters/h1/"
        and saved["body"] == ["content", "version"]
        and saved["version"] == "h1-v1"
    )
    assert (
        saved["csrf"] is True
        and saved["newVersion"] == "v2"
        and saved["pill"] == {"state": "saved", "text": "محفوظ"}
    )
    assert (
        saved["dirty"] is False
        and saved["pagesStale"] is True
        and saved["refresh"] == 1
        and saved["guard"] is False
    )
    assert out["refreshed"] == {"pagesStale": False, "reqs": 1}
    # the conflict and its two ways out
    assert out["conflict"] == {
        "state": "conflict",
        "pill": {"state": "conflict", "text": "تغيّر في نافذة أخرى"},
        "open": True,
        "version": "v7",
        "dirty": True,
    }
    assert (
        out["reloaded"]["open"] is False
        and out["reloaded"]["version"] == "v7"
        and out["reloaded"]["dirty"] is False
        and out["reloaded"]["set"] == 1
    )
    assert out["reloadedNoText"] == [False, "/api/books/1/chapters/h1/", "h1-v1", False]
    assert out["keptMine"] == {
        "snapshot": True,
        "put": "v8",
        "version": "v2",
        "pill": {"state": "saved", "text": "محفوظ"},
    }
    assert out["failed"] == {
        "state": "error",
        "pill": {"state": "error", "text": "تعذّر الحفظ · إعادة المحاولة"},
        "dirty": True,
        "guard": True,
    }
    assert out["retried"] == {"state": "saved", "dirty": False}
    # a split: the list and the chapter again, no save meanwhile, the caret's block looked for
    split = out["split"]
    assert split["reloadPending"] is False and split["version"] == "h1-v1" and split["chapter"] == "h1"
    assert (
        split["gets"][:2] == ["/api/books/1/chapters/", "/api/books/1/chapters/h1/"]
        and split["toast"] == "انقسم الفصل؛ حُدّثت قائمة الفصول"
    )
    assert split["goTo"] == ["p22"]
    # the keyboard map
    keys = out["keys"]
    assert (
        keys["save"] == "save"
        and keys["saveInEditor"] == "save"
        and keys["saveInField"] == "save"
        and keys["ctrl"] == "save"
    )
    assert (
        keys["find"] == "find"
        and keys["footnote"] == "footnote"
        and keys["prev"] == "prevChapter"
        and keys["next"] == "nextChapter"
    )
    assert (
        keys["undo"] == "undo"
        and keys["redo"] == "redo"
        and keys["bold"] == "bold"
        and keys["italic"] == "italic"
    )
    assert [keys["h1"], keys["h2"], keys["p"], keys["quote"], keys["sep"]] == [
        "heading1",
        "heading2",
        "paragraph",
        "quote",
        "separator",
    ]
    assert (
        keys["sheet"] == "sheet"
        and keys["sheetArabic"] == "sheet"
        and keys["sheetInEditor"] is None
        and keys["o"] == "source"
        and keys["oInEditor"] is None
        and keys["oMod"] == "source"
    )
    assert keys["esc"] == "escape" and keys["plain"] is None and keys["arabic"] is None
    assert (
        out["findOpen"] == [True, True, "findQuery", "find"]
        and out["escFind"] == [True, False, True]
        and out["escBlur"] == [False, "blur"]
    )
    assert (
        out["boldHandled"] == [False, 0]
        and out["boldOutside"] == [True, ["bold"], True]
        and out["styleKey"] == [True, ["style", "heading1"], "heading1"]
    )
    assert out["saveKey"] == [True, 1]
    # find & replace
    assert out["findTimer"] == 1 and out["find"] == {
        "call": ["find", "برقة", {"matchTashkeel": False, "foldAlef": True, "wholeWord": False}, False],
        "count": "1 من 3",
        "total": 3,
        "index": 0,
    }
    assert out["findNext"] == [["findNext", 1], 1, "2 من 3"] and out["findPrev"] == [["findNext", -1], 0]
    assert out["findOptions"]["wholeWord"] is True
    assert out["replaceOne"] == [True, ["replaceCurrent", "find", "findNext"], True]
    assert out["replaceAll"] == [3, ["replaceAll", "بَرقة"], "استُبدلت 3 مطابقات", "function"] and out[
        "replaceUndo"
    ] == ["undo"]
    assert out["noteMatch"] == ["note", "n1"]
    assert (
        out["bookCount"]["post"]["replace"] is False
        and out["bookCount"]["count"] == "7 في الكتاب"
        and out["bookCount"]["total"] == 7
    )
    assert out["bookCountRace"] == [None, 2]  # the older answer is dropped, the newer lands
    assert (
        out["bookReplace"][0] == 7
        and out["bookReplace"][1] is True
        and "/api/books/1/chapters/h1/" in out["bookReplace"][2]
    )
    assert (
        out["bookReplace"][3] == "استُبدلت 7 مطابقات في الكتاب"
        and out["bookUndo"] == "/api/books/1/snapshots/41/restore/"
    )
    assert out["findClosed"] == [False, "clearFind"]
    # the style picker, the marks
    assert out["style"] == [True, False, ["style", "heading2"], "heading2", "عنوان فرعي"] and out[
        "styleFollows"
    ] == ["quote", "اقتباس"]
    assert out["bold"] == [False, "bold"]  # toggled on by ⌘B above, off again here
    # footnotes
    assert out["noteInsert"][:4] == [True, "note", "ne1", 2] and out["noteInsert"][4] == ["create", "[]"]
    assert (
        out["noteInsert"][5].startswith("top:")
        and out["noteInsert"][6] == "note"
        and out["noteInsert"][7] is True
    )
    assert out["noteTyped"] == ["setNote", "ne1", [{"type": "text", "text": "نص"}]]
    # the same click's outside-click is no close (a macrotask clears the guard); the next one closes
    assert out["noteOutside"] == [False, "note", 1, True, None]
    assert out["noteEnter"] == ["function", None, "focus"]
    assert out["noteEsc"] == ["note", None, "destroy"] and out["noteClick"] == ["note", "n1", 2]
    assert out["noteDelete"] == [True, None, ["deleteNote", "n1"], "حُذفت الحاشية"]
    assert out["toastRetired"] == [True, True, False, None]
    # uncertain words: readings from the block's own lines only, the typed field prefilled and focused
    word = out["word"]
    assert (
        word["kind"] == "word"
        and word["loading"] is False
        and word["typed"] == "مليتية"
        and word["focus"] == "wordTyped"
    )
    assert word["readings"] == [
        {"value": "مليتية", "label": "Qari v0.3"},
        {"value": "مليتة", "label": "Qari v0.2"},
        {"value": "ملبتية", "label": "Tesseract"},
    ]
    assert word["reqs"] == ["/api/books/1/sheets/?from=2&to=2", "/api/pages/7/review/"]
    assert out["wordChosen"] == [["replaceRange", 30, 36, "مليتة"], None, True]
    assert out["wordAccepted"] == ["accept", 30, 36] and out["wordTyped"] == [
        "replaceRange",
        30,
        36,
        "مليتيه",
    ]
    # the source pane and the drawer
    assert out["sourceTimer"] == 1
    # page 2 was cached by the readings; page 3 is fetched when the pager moves
    assert out["source"] == {
        "pages": [2, 3],
        "page": 2,
        "lines": [10, 11],
        "loading": False,
        "boxes": [10, 11],
        "req": [],
        "aspect": "0.6667",
        "box": "left:10.00%;top:20.00%;width:80.00%;height:5.00%",
    }
    assert out["drawer"] == [True, True, "drawerClose", "drawer"] and out["drawerEsc"] == ["drawer", False]
    assert out["drawerStep"] == [1, 3, ["/api/books/1/sheets/?from=3&to=3"], 3]
    assert out["oInEditor"] == [False, False]
    assert out["sourceSame"] == [False, 3, [12], 0, [3]]  # the same scan stays, no skeleton, no request
    # chapters
    switch = out["switch"]
    assert (
        switch[0] is True
        and switch[1] == 1
        and switch[2] == ["/api/books/1/chapters/h20/", "/api/books/1/manuscript/state/"]
        or switch[2][0] == "/api/books/1/chapters/h20/"
    )
    assert switch[3] == "h20" and switch[4] == "h20-v1" and switch[5] == 1 and switch[6] is True
    assert (
        switch[7] == "http://x/books/1/editor/?chapter=h20"
        and switch[8] == "الفصل 2 من 2 · 5 كلمات · ص 8–9 في الكتاب"
    )
    assert (
        out["listClick"] == "h1"
        and out["ends"] == [False, "هذا أول فصل", True, "h20"]
        and out["prevOk"] == "h1"
    )
    assert out["switchBlocked"] == [False, "h1", "تعذّر الحفظ؛ بقيت في هذا الفصل"]
    # a PUT in flight: the switch waits, the answer lands on the chapter it was made for, not on the next one
    assert out["inflight"] == {
        "midSwitch": {"chapter": "h1", "pill": "saving"},
        "chapter": "h20",
        "version": "h20-v1",
        "title": "الفصل الثاني",
        "h1Row": "عنوان جديد",
        "pill": "idle",
    }
    assert out["typedDuringSave"] == ["dirty", True, "v78", 2, 3]
    # snapshots, digits, re-assembly
    assert out["snapshots"] == [True, 2, "snapshotLabel"]
    assert out["snapshotCreate"] == [True, {"label": "قبل التنسيق"}, 50, 3]
    assert out["snapshotRestore"] == [
        True,
        "/api/books/1/snapshots/3/restore/",
        False,
        "/api/books/1/chapters/h1/",
        "استُعيدت النسخة",
    ]
    assert out["toastKept"] == [False, True] and out["sheetFocus"] == ["sheetClose", "trigger"]
    assert out["digits"] == [True, {"chapter": "h1", "style": "arabic_indic"}, False, "حُوّل 4 أرقام"]
    assert out["reassemble"] == [True, "/api/books/1/chapters/h1/reassemble/", True, ["editable", False], 1]
    assert out["reassemblePolling"] == [True, 1]
    assert out["reassembled"][:2] == [False, True] and "/api/books/1/chapters/h1/" in out["reassembled"][2]
    assert (
        out["reassembled"][3] == "أُعيد تجميع الفصل من المراجعة؛ النص السابق محفوظ نسخةً"
        and out["reassembled"][4] is False
    )
    # preferences, the sheet's vars and faces, the pure formatters
    assert out["prefs"] == ["120", "0", ["class", "hide-marks", True], 120, False]
    assert (
        "--ed-page-w:643px" in out["vars100"]
        and "--ed-size:17.33px" in out["vars100"]
        and "--ed-pad-start:83px" in out["vars100"]
    )
    assert (
        "--ed-page-w:771px" in out["vars120"]
        and "--ed-h1:1.6em" in out["vars120"]
        and "--ed-lh:1.7" in out["vars120"]
    )
    assert (
        'src: url("/static/fonts/amiri/Amiri-Regular.ttf")' in out["fonts"]
        and 'font-family: "nk-ed-latin"; src: local("Times New Roman")' in out["fonts"]
    )
    assert (
        'font-family: "nk-ed-body"; src: local("Simplified Arabic"); font-weight: 400; font-style: normal;'
        " unicode-range: U+0000-0040" in out["fontsSystem"]
    )
    assert (
        'font-family: "nk-ed-heading"; src: local("Lotus Linotype Exnd"); font-weight: 400;'
        " font-style: normal; unicode-range: U+0000-002F, U+003A-0040" in out["fontsSystem"]
    )
    assert out["status"] == "الفصل 3 من 14 · 2 184 كلمة · ص 31–58 في الكتاب"
    assert out["statusStale"] == "الفصل 1 من 2 · كلمة واحدة · للقراءة فقط"
    assert out["place"] == {
        "style": "top:662px;right:312px",
        "above": True,
    }  # flipped above, kept off the left edge
    # a proofreader, no bundle, no manuscript
    assert out["readOnly"] == [
        False,
        {"state": "idle", "text": ""},
        False,
        False,
        "الفصل 1 من 2 · 5 كلمات · ص 3–7 في الكتاب · للقراءة فقط",
    ]
    assert out["noBundle"] == ["error", True] and out["noManuscript"] == "empty"
