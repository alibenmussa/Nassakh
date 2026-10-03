"""The book page's text options, empty lines, page breaks and blank pages, the pages' scrubber and the review
changes' list (D99, the owner's review of 2026-10-03, items 11, 23, 24, 25, 26 and 27), under Node:

- `static/src/editor/convert.js` and `schema.js`: the options round-trip through the editor's JSON and
  TipTap's one-block schema (bad values dropped; the server accepts the result), a split moves «مع التالية»
  and the page's end to its second half, a heading's options stay with it, ⌘↩'s cut, the runs of empty
  lines, a blank page, a block's static markup (an empty paragraph keeps its line)
- `geometry.js`: a line kept to its side by the engine, an empty line, the page-break and blank-page marks
- the `bookPage` component (the harness of `core/test_layout_ui.py`): the alignment toggled and saved, an
  indent step and a size on the open box, a left-to-right paragraph; Enter makes empty lines up to the
  limit; ⌘↩ and Backspace at the page's start; a blank page added, removed and undone; a marker's ×; the
  scrubber
- the templates: the toolbar's two menus, «النص» and the page's acts in «الفقرة», the scrubber, the changes'
  list, the shortcut sheet"""

from __future__ import annotations

import json
import re

import pytest

from core.test_layout_ui import (
    COMPONENT_HARNESS,
    ROOT,
    _between,
    _book,
    _component_fixture,
    _logged,
    _node_tmp,
    _page,
    _run_node,
    _user,
)
from editor import document as doc

pytestmark = pytest.mark.django_db

CONVERT_HARNESS = r"""
import { pathToFileURL } from 'node:url';
const [, , root] = process.argv;
const convert = await import(pathToFileURL(`${root}/static/src/editor/convert.js`));
const schema = await import(pathToFileURL(`${root}/static/src/editor/schema.js`));
const core = await import('@tiptap/core');
const out = {};
const p = (id, text, attrs = {}) => ({ type: 'paragraph', attrs: { ...(id ? { id } : {}), ...attrs }, content: text ? [{ type: 'text', text }] : [] });
const all = { align: 'center', dir: 'ltr', indent: 2, firstLine: false, spaceBefore: 0.5, spaceAfter: 2, size: 'large', breakAfter: true };
out.round = convert.fromEditor(convert.toEditor([p('a', 'نص', all), { type: 'heading', attrs: { id: 'h', level: 2, align: 'end' }, content: [{ type: 'text', text: 'ع' }] }]));
out.bad = convert.fromEditor(convert.toEditor([p('b', 'نص', { align: 'left', indent: 9, size: 'huge', spaceBefore: 3, dir: 'up' })]));
// the one-block editor's schema keeps every option (an unknown attr would be dropped by ProseMirror)
const one = core.getSchema(schema.blockExtensions({}));
const pm = one.nodeFromJSON({ type: 'doc', content: [convert.toEditor([p('a', 'نص', all)]).content[0]] });
out.pm = convert.fromEditor(pm.toJSON());
const dom = one.nodes.paragraph.spec.toDOM(pm.child(0));
out.dom = dom[1];
const norm = (attrs) => Object.fromEntries(Object.entries(attrs).map(([k, v]) => [k, k === 'id' && String(v).startsWith('ek') ? 'NEW' : v]));
out.split = convert.splitNode(p('s', 'أول ثان', { breakBefore: true, breakAfter: true, keepWithNext: true, align: 'center' }), 3).map((n) => norm(n.attrs));
out.headSplit = norm(convert.splitNode({ type: 'heading', attrs: { id: 'h', level: 2, align: 'center', size: 'large', dir: 'ltr', spaceAfter: 1 }, content: [{ type: 'text', text: 'عنوان' }] }, 5)[1].attrs);
out.merge = [convert.mergeNodes(p('a', 'أ', { breakAfter: true }), p('b', 'ب', { breakAfter: true })).node.attrs, convert.mergeNodes(p('a', 'أ', { breakAfter: true }), p('b', 'ب')).node.attrs];
out.paste = convert.insertBlocks(p('n', 'نص', { breakAfter: true }), 1, 1, [p(null, 'x'), p(null, 'y'), p(null, 'z')]).blocks.map((b) => b.attrs.breakAfter === true);
out.pageBreak = [convert.pageBreakAt(p('a', 'أبجد'), 0), convert.pageBreakAt(p('a', 'أبجد'), 2), convert.pageBreakAt(p('a', 'أبجد'), 4), convert.pageBreakAt(p('e', ''), 0)]
  .map((r) => ({ n: r.blocks.length, breaks: r.blocks.map((b) => b.attrs.breakBefore === true), texts: r.blocks.map(convert.plainText), caret: r.caret }));
const run = [p('x', 'نص'), p('e1'), p('e2'), p('y', 'نص'), p('e3'), p('e4', '', { breakBefore: true }), p('e5')];
out.runs = ['x', 'e1', 'e2', 'e3', 'e4', 'e5'].map((id) => convert.emptyRunAt(run, id));
out.runsExtra = convert.emptyRunAt(run, 'e1', 1);
out.blank = [convert.isBlankPage(convert.blankPage()), norm(convert.blankPage().attrs), convert.isBlankPage(p('e')), convert.isEmptyBlock(p('w', '  ')), convert.isEmptyBlock({ type: 'paragraph', content: [{ type: 'footnote', attrs: { id: 'n' }, content: [] }] }), convert.MAX_EMPTY_RUN];
out.set = convert.locate(convert.setTextAttrs([p('a', 'نص', { align: 'center', size: 'large' })], 'a', { align: null, indent: 2, dir: 'rtl', firstLine: true, spaceBefore: 0, nope: 1 }), 'a').node.attrs;
out.html = [convert.nodeHtml(p('e')), convert.nodeHtml(p('a', 'نص', { align: 'end', dir: 'ltr', indent: 3, firstLine: false, size: 'xlarge' })), convert.nodeHtml({ type: 'heading', attrs: { level: 2, align: 'center' }, content: [{ type: 'text', text: 'ع' }] })];
console.log(JSON.stringify(out));
"""  # noqa: E501


def test_the_text_options_and_page_helpers_of_the_editor_modules_under_node(tmp_path):
    folder = _node_tmp(tmp_path)
    out = _run_node(folder, "convert.mjs", CONVERT_HARNESS, str(ROOT))
    every = {
        "id": "a",
        "align": "center",
        "dir": "ltr",
        "indent": 2,
        "firstLine": False,
        "spaceBefore": 0.5,
        "spaceAfter": 2,
        "size": "large",
        "breakAfter": True,
    }
    # the options round-trip, through the editor's JSON and TipTap's schema; the server takes them as they are
    assert out["round"][0]["attrs"] == {**every, "sourcePages": [], "sourceLineIds": [], "reviewed": True}
    assert out["round"][1]["attrs"]["align"] == "end"
    assert out["pm"][0]["attrs"] == out["round"][0]["attrs"]
    assert doc.clean_nodes(out["round"]) == out["round"]
    # bad values are dropped (never saved)
    assert out["bad"][0]["attrs"] == {"id": "b", "sourcePages": [], "sourceLineIds": [], "reviewed": True}
    # the editor's own markup draws them as the static patch does
    assert {
        "dir": "ltr",
        "data-align": "center",
        "data-indent": "2",
        "data-first": "none",
        "data-size": "large",
    }.items() <= out["dom"].items()
    # a split: the page break stays with the first half, «مع التالية» and the page's end go to the second; the
    # alignment goes with both halves (Word carries a paragraph's format)
    first, second = out["split"]
    assert first == {"id": "s", "breakBefore": True, "align": "center"}
    assert second == {"id": "NEW", "keepWithNext": True, "breakAfter": True, "align": "center"}
    # a heading's alignment, size and space stay with it; its direction goes on into the paragraph after it
    assert out["headSplit"] == {"id": "NEW", "dir": "ltr"}
    assert out["merge"][0]["breakAfter"] is True and "breakAfter" not in out["merge"][1]
    assert out["paste"] == [False, False, True]
    # ⌘↩: at the start the block itself starts the page; inside, its second half; at the end an empty one
    assert out["pageBreak"] == [
        {"n": 1, "breaks": [True], "texts": ["أبجد"], "caret": {"index": 0, "offset": 0}},
        {"n": 2, "breaks": [False, True], "texts": ["أب", "جد"], "caret": {"index": 1, "offset": 0}},
        {"n": 2, "breaks": [False, True], "texts": ["أبجد", ""], "caret": {"index": 1, "offset": 0}},
        {"n": 1, "breaks": [True], "texts": [""], "caret": {"index": 0, "offset": 0}},
    ]
    # the runs of empty paragraphs (a page break starts a new one), and with one more there
    assert out["runs"] == [0, 2, 2, 1, 2, 2] and out["runsExtra"] == 3
    blank, attrs, plain_empty, spaces, call, limit = out["blank"]
    assert blank is True and attrs == {"id": "NEW", "breakBefore": True, "breakAfter": True}
    assert plain_empty is False and spaces is True and call is False and limit == doc.MAX_EMPTY_RUN
    # the defaults are not spelled out; an unknown name is ignored
    assert out["set"] == {"id": "a", "indent": 2, "size": "large"}
    assert out["html"][0] == '<p class="ed-p"><br></p>'
    assert (
        out["html"][1] == '<p class="ed-p" dir="ltr" data-align="end" data-indent="3" data-first="none"'
        ' data-size="xlarge">نص</p>'
    )
    assert out["html"][2] == '<h2 class="ed-h2" data-align="center">ع</h2>'


FEATURES_SCENARIO = r"""
const out = {};
// the one-block editor's text options, on the stub (the real editor sets them through its node's attrs)
const stubCreate = globalThis.NassakhEditor.createBlock;
globalThis.NassakhEditor.createBlock = (host, opts) => {
  const ed = stubCreate(host, opts);
  ed.textAttrs = () => convert.textAttrsOf(ed.node.attrs);
  ed.setTextAttrs = (patch) => {
    if (!['paragraph', 'heading'].includes(ed.node.type)) return false;
    const clean = {};
    Object.entries(patch).forEach(([k, val]) => { const x = convert.textAttr(k, val); clean[k] = x === null || (k === 'indent' && x === 0) || (k === 'dir' && x === 'rtl') || (k === 'firstLine' && x === true) || ((k === 'spaceBefore' || k === 'spaceAfter') && x === 0) ? null : x; });
    ed.setAttrs(clean);
    return true;
  };
  return ed;
};
const G = globalThis.NassakhBook.geo;
const empties = (v) => convert.flatBlocks(v.nodes()).filter((b) => convert.isEmptyBlock(b.node)).map((b) => b.id);
const lastPut = () => calls.filter((c) => c[0] === 'PUT').pop();
(async () => {
  const v = mk(); await settle();
  v.showPage(2, { instant: true });
  v.setMode('edit'); await settle();
  // ---- the text options of the open paragraph p11: saved, drawn on its box
  v.onSheetClick(lineTarget(v, 'right', 1, 0, 3), 'right'); await settle();
  const ed = editors[editors.length - 1];
  const w0 = Number(ed.host.style['--w']);
  calls.length = 0;
  v.alignTo('center'); await settle();
  const put = lastPut();
  const centred = { attr: ed.node.attrs.align, side: v.alignSide, put: put && convert.locate(put[2].content.content, 'p11').node.attrs.align };
  v.alignTo('center'); await settle();
  const cleared = { attr: ed.node.attrs.align === undefined, side: v.alignSide };
  v.alignTo('right'); await settle();
  const right = ed.node.attrs.align;
  v.alignTo('left'); await settle();
  const left = ed.node.attrs.align;
  v.stepIndent(1); await settle();
  const w1 = Number(ed.host.style['--w']);
  v.setTextAttr('size', 'large'); await settle();
  const fs = ed.host.style['--fs'];
  v.setTextAttr('dir', 'ltr'); await settle();
  const margin = Number(v.ctx().pages.get(2).margins.left);
  const ltr = { attr: ed.node.attrs.dir, side: v.alignSide, x: Math.round((Number(ed.host.style['--x']) - margin) * 100) / 100 };
  v.resetText(); await settle();
  out.text = { centred, cleared, right, left, indentW: Math.round((w0 - w1) * 100) / 100, fs, ltr, reset: convert.textAttrsOf(ed.node.attrs), has: v.hasTextOptions };
  // ---- Enter at the end, then on the empty line: three blank lines, the fourth refused; they are saved
  const end = convert.plainText(ed.node).length;
  ed.opts.onBoundary('split', { node: ed.getNode(), from: end, to: end }); await settle();
  for (let i = 0; i < 3; i += 1) { const e = editors[editors.length - 1]; e.opts.onBoundary('split', { node: e.getNode(), from: 0, to: 0 }); await settle(); }
  const lastEmpty = editors[editors.length - 1];
  lastEmpty.opts.onBlur && lastEmpty.opts.onBlur();
  await v.closeBlock({ commit: true }); await fire(500); await settle();
  const saved = lastPut();
  out.empty = { ids: empties(v).length, refused: toasts.slice(-1)[0], saved: saved ? saved[2].content.content.filter((n) => convert.isEmptyBlock(n)).length : 0 };
  // ---- ⌘↩ inside p13: its second half starts a page and opens; Backspace there takes the break off, no merge
  v.onSheetClick(lineTarget(v, 'right', v.ctx().pages.get(2).lines.findIndex((l) => l.block === 'p13'), 0, 3), 'right'); await settle();
  const e13 = editors[editors.length - 1];
  e13.opts.onBoundary('pageBreak', { node: e13.getNode(), from: 10, to: 10 }); await settle();
  const half = editors[editors.length - 1];
  const flat = convert.flatBlocks(v.nodes());
  const i13 = flat.findIndex((b) => b.id === 'p13');
  out.pageBreak = { first: convert.plainText(flat[i13].node).length, second: flat[i13 + 1].node.attrs.breakBefore, open: half.node.attrs.id === flat[i13 + 1].id, offset: half.at, live: v.liveMessage };
  half.opts.onBoundary('mergeBackward', {}); await settle();
  out.unbreak = { flag: convert.locate(v.nodes(), half.node.attrs.id).node.attrs.breakBefore === undefined, count: convert.flatBlocks(v.nodes()).length === flat.length, toast: stores.bookToast.message, open: v.open && v.open.block === half.node.attrs.id };
  // ---- a blank page after the open block: opened; Backspace in it removes it whole (the undo brings it back)
  v.insertBlankPage(); await settle();
  const bpEd = editors[editors.length - 1];
  const bpId = bpEd.node.attrs.id;
  const before = convert.flatBlocks(v.nodes());
  const at = before.findIndex((b) => b.id === bpId);
  out.blank = { blank: convert.isBlankPage(before[at].node), after: before[at - 1].id === half.node.attrs.id, open: v.blankOpen, info: v.blockInfo && v.blockInfo.blank, toast: stores.bookToast.message };
  bpEd.opts.onBoundary('mergeBackward', {}); await settle();
  out.blankRemoved = { gone: !convert.locate(v.nodes(), bpId), reopened: editors[editors.length - 1].node.attrs.id === half.node.attrs.id, offset: editors[editors.length - 1].at, toast: stores.bookToast.message };
  v.undo(); await settle();
  out.blankUndo = Boolean(convert.locate(v.nodes(), bpId));
  // Delete at the end of the block before it removes it too
  const hostEd = editors[editors.length - 1];
  await v.closeBlock({ commit: true }); await settle();
  v.ctx().nodes = convert.locate(v.nodes(), bpId) ? v.nodes() : v.nodes();
  await v.openBlock(half.node.attrs.id, 0, { n: 2 }); await settle();
  const prevEd = editors[editors.length - 1];
  prevEd.opts.onBoundary('mergeForward', {}); await settle();
  out.blankDelete = { gone: !convert.locate(v.nodes(), bpId), still: prevEd.destroyed === false };
  void hostEd;
  // ---- the marker's ×: a page break drawn in edit mode over p12's first line; a click takes the break off
  await v.closeBlock({ commit: true }); await settle();
  v.ctx().nodes = convert.setBlockAttrs(v.nodes(), 'p12', { breakBefore: true });
  const page2 = v.ctx().pages.get(2);
  const i12 = page2.lines.findIndex((l) => l.block === 'p12');
  page2.lines[i12] = { ...page2.lines[i12], brk: true };
  v.paint();
  const html = v._dom.sheets.right.lines.innerHTML;
  const x = el('button', 'lp-brk-x', { unbreak: 'p12' });
  v._dom.sheets.right.lines.appendChild(x);
  await v.onSheetClick({ target: x, clientX: 0, clientY: 0, stopPropagation() {} }, 'right'); await settle();
  out.marker = { drawn: html.includes('data-unbreak="p12"') && html.includes('فاصل صفحة'), flag: convert.locate(v.nodes(), 'p12').node.attrs.breakBefore === undefined, toast: stores.bookToast.message };
  // in preview the page breaks are not drawn
  v.setMode('preview'); await settle();
  out.previewMarks = v._dom.sheets.right.lines.innerHTML.includes('lp-brk');
  // ---- the scrubber: a drag shows the page under the thumb and turns there after a rest; the release at once
  drop(); v.showPage(2, { instant: true });
  const s0 = [v.scrubValue, v.scrubPct];
  v.onScrubInput({ target: { value: '4.4' } });
  const during = [v.scrub.active, v.scrubValue, v.current, pending(140).length];
  await fire(140); await settle();
  const turned = v.current;
  v.onScrubChange({ target: { value: '3' } }); await settle();
  out.scrub = { s0, during, turned, end: [v.current, v.scrub.active, v.scrubValue], clamp: [G.scrubTarget(99, 1, 5), G.scrubTarget('x', 2, 5), G.scrubTarget(0, 1, 5)] };
  // ---- the live page: a line kept to its side by the engine, an empty line, the blank page's mark
  const page = { n: 9, width_pt: 481.89, height_pt: 680.31, margins: { top: 56.69, right: 62.36, bottom: 62.36, left: 51.02 }, lines: [
    { block: 'bp', kind: 'body', x: 51.02, y: 56.69, w: 368.5, h: 22.1, start: 0, end: 0, first: true, runs: [], empty: true, blank: true, brk: true, align: 'right', dir: 'rtl', justify: false },
  ] };
  const blankHtml = G.pageHtml(page, { breaks: { removable: true } });
  const line = (extra) => G.lineHtml({ block: 'x', kind: 'heading', x: 0, y: 0, w: 10, h: 10, start: 0, end: 1, runs: [], ...extra }, 0, {}).match(/class="([^"]*)"/)[1];
  out.geo = { blank: blankHtml.includes('صفحة فارغة') && blankHtml.includes('data-unbreak="bp"') && !blankHtml.includes('فاصل صفحة'), plain: G.pageHtml(page, {}).includes('lp-brk'),
    readonly: G.pageHtml(page, { breaks: { removable: false } }).includes('data-unbreak'), empty: blankHtml.includes('is-empty'),
    cls: ['right', 'left', 'center'].map((a) => line({ align: a })), old: line({}), justified: line({ align: 'right', justify: true }) };
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


def test_the_book_page_text_options_empty_lines_page_breaks_and_the_scrubber_under_node(tmp_path):
    folder = _node_tmp(tmp_path)
    fixture = _component_fixture()
    (folder / "fixture.json").write_text(json.dumps(fixture, ensure_ascii=False))
    base = COMPONENT_HARNESS.split("\nconst out = {};\n")[0]
    out = _run_node(folder, "features.mjs", base + FEATURES_SCENARIO, str(ROOT), str(folder / "fixture.json"))
    text = out["text"]
    # the alignment: set, saved with the chapter, shown by its side; the same choice again is the style's own
    assert text["centred"] == {"attr": "center", "side": "center", "put": "center"}
    assert text["cleared"] == {"attr": True, "side": "justify"}
    assert (text["right"], text["left"]) == ("start", "end")  # a right-to-left paragraph: right is its start
    # an indent step narrows the box by twice the body size; a size scales its text; a left-to-right paragraph
    # starts on the left (its box moves right by its indent), its end-aligned text now on the right
    assert text["indentW"] == 26
    assert float(text["fs"]) == pytest.approx(13 * 1.15, abs=0.01)
    assert text["ltr"] == {"attr": "ltr", "side": "right", "x": 26}
    assert text["reset"] == dict.fromkeys(doc.TEXT_ATTRS) and text["has"] is False
    # Enter: blank lines that stay and are saved, three in a row at most
    empty = out["empty"]
    assert empty["ids"] == 3 and empty["saved"] == 3
    assert "لا يُطبع أكثر من 3 أسطر فارغة متتالية" in empty["refused"]
    # ⌘↩ cuts the paragraph: its second half starts a page and opens at its start
    assert out["pageBreak"] == {"first": 10, "second": True, "open": True, "offset": 0, "live": "فاصل صفحة"}
    assert out["unbreak"] == {"flag": True, "count": True, "toast": "أُزيل فاصل الصفحة", "open": True}
    # a blank page: added after the open block and opened; Backspace in it removes it whole; undo; Delete
    assert out["blank"] == {
        "blank": True,
        "after": True,
        "open": True,
        "info": True,
        "toast": "أُضيفت صفحة فارغة",
    }
    assert out["blankRemoved"]["gone"] is True and out["blankRemoved"]["reopened"] is True
    assert out["blankRemoved"]["toast"] == "حُذفت الصفحة الفارغة"
    assert out["blankUndo"] is True and out["blankDelete"]["gone"] is True
    # the marker's × on the page takes the break off; preview draws no marks
    assert out["marker"] == {"drawn": True, "flag": True, "toast": "أُزيل فاصل الصفحة"}
    assert out["previewMarks"] is False
    # the scrubber
    scrub = out["scrub"]
    assert scrub["s0"] == [2, "0.2500"]
    assert scrub["during"] == [True, 4, 2, 1] and scrub["turned"] == 4
    assert scrub["end"] == [3, False, 3] and scrub["clamp"] == [5, 2, 1]
    geo = out["geo"]
    assert geo["blank"] and not geo["plain"] and not geo["readonly"] and geo["empty"]
    assert [re.search(r"is-[rlc]\b", c).group() for c in geo["cls"]] == ["is-r", "is-l", "is-c"]
    assert "is-c" in geo["old"]  # a layout from before the engine's `align`: the kind decides (a heading)
    assert "is-j" in geo["justified"] and "is-r" not in geo["justified"]


@pytest.fixture
def editor(db):
    return _user("editor", "editor")


def test_the_templates_for_the_text_options_the_page_and_the_scrubber(editor):
    body, _config = _page(_logged(editor), _book())
    toolbar = _between(body, "data-edit-tools", "data-edit-status")
    assert "data-align-picker" in toolbar and "alignTo(a[0])" in toolbar and "محاذاة النمط" in toolbar
    assert "data-page-picker" in toolbar and "pageBreak()" in toolbar and "insertBlankPage()" in toolbar
    assert "فاصل صفحة عند المؤشّر" in toolbar and "صفحة فارغة بعد الفقرة" in toolbar
    block = _between(body, 'data-panel="block"', "</section>")
    for needle in (
        "data-text-options",
        "setTextAttr('dir', 'ltr')",
        "stepIndent(1)",
        "بلا إزاحة أول السطر",
        "setTextAttr(side[0], v[0])",
        "كما في النمط",
        "data-page-actions",
        "removeBlankPage(blockInfo.id, -1)",
    ):
        assert needle in block, needle
    pages = _between(body, 'data-panel="pages"', "</section>")
    assert (
        "data-scrub-range" in pages
        and '@input="onScrubInput($event)"' in pages
        and ':max="lastPage"' in pages
    )
    changes = _between(body, 'data-panel="changes"', "</section>")
    assert "bp-cgroup" in changes and "bp-reason-dot" in changes and "bp-citem-ask" in changes
    assert '<symbol id="i-align-right"' in body and '<symbol id="i-page-blank"' in body
    sheet = _between(body, "rv-shortcuts", "</dl>")
    assert "⌘↩" in sheet and "⇧⌘E" in sheet
    # a proofreader sees no text options and no page acts
    reader = _logged(_user("reader2", "proofreader"))
    plain, _ = _page(reader, _book("كتاب آخر"))
    assert "data-text-options" not in plain and "data-page-actions" not in plain


def test_the_css_of_the_features():
    css = (ROOT / "static" / "src" / "components" / "layout.css").read_text(encoding="utf-8")
    for needle in (
        ".lp-line.is-r { text-align: right; text-align-last: right; }",
        ".lo-screen.is-edit .lp-line.is-empty::before",
        ".lp-brk {",
        ".lp-brk-x:hover",
        ".lp-patch.is-h2 { text-align: start; text-align-last: start; }",
        '.lp-patch :is(.ed-p, .ed-h1, .ed-h2)[data-align="center"]',
        ".bp-scrub { position: sticky;",
        ".bp-changes-foot { position: sticky;",
        ".bp-cgroup {",
        ".bp-reason-dot {",
        '.bp-diff { font-family: "nk-body"',
    ):
        assert needle in css, needle
