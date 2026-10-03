"""The book page's edit loop under Node (the harness of core/test_layout_ui.py): what the owner's review of
2026-10-03 found (docs/TODO_REVIEW_2026-10-03.md, editor-core).

- a paragraph made a chapter title splits its chapter: the save's answer asks for a reload; the reload never
  waits on the save it is part of (it did, and every later save, chapter switch and «حفظ نسخة الآن» hung on
  it), it follows the re-layout the save asked for (no second one), and text typed meanwhile is kept
- the save state between an edit and its pages (as in Google Docs): «غير محفوظ» → «يتم الحفظ…» → «تم الحفظ ·
  تُحدَّث الصفحات…» → «تم الحفظ»; a failed save says so and is tried again; a save that never answers is
  given up after a while instead of holding every later one
"""

from __future__ import annotations

import json

import pytest

from core.test_layout_ui import COMPONENT_HARNESS, ROOT, _component_fixture, _node_tmp, _run_node

pytestmark = pytest.mark.django_db


def _scenario(tmp_path, name: str, scenario: str) -> dict:
    folder = _node_tmp(tmp_path)
    fixture = _component_fixture()
    (folder / "fixture.json").write_text(json.dumps(fixture, ensure_ascii=False))
    base = COMPONENT_HARNESS.split("\nconst out = {};\n")[0]
    return _run_node(folder, name, base + scenario, str(ROOT), str(folder / "fixture.json"))


# The server splits h10 before p13 (p13 became a level-1 heading): h10 keeps h10–p12, the new chapter «p13»
# holds p13–p14. Its answer has no version (the editor must reload) and the re-layout it asked for.
SPLIT_SCENARIO = r"""
const out = {};
const hung = (p) => Promise.race([p, settle().then(() => 'hung')]);
(async () => {
  const v = mk();
  await settle();
  v.setMode('edit'); await settle();
  v.onSheetClick(lineTarget(v, 'right', 6, 0, 10), 'right'); await settle();
  const ed = editors[editors.length - 1];
  const h10 = clone(fixture.docs.h10);
  let split = false;
  server.put = (cid, body) => {
    if (cid === 'h10' && !split) {
      split = true;
      const sent = body.content.content;
      server.docs.h10 = sent.slice(0, 3);
      server.docs.p13 = sent.slice(3);
      server.versions.h10 = 'v5';
      server.versions.p13 = 'q1';
      return reply(200, { version: null, id: 'h10', chapters: [{ id: 'h10', version: 'v5', title: 'الفصل الأول' }, { id: 'p13', version: 'q1', title: 'عنوان' }], reload: true, manuscript_version: 3, changed: true,
        relayout: { id: 30, status: 'queued', url: '/api/books/1/relayout/30/', result: {} } });
    }
    server.docs[cid] = body.content.content;
    return reply(200, { version: `${cid}-saved`, id: cid, chapters: [{ id: cid, version: `${cid}-saved`, title: 't' }], reload: false, manuscript_version: 4, changed: true, relayout: null });
  };
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    if (url === '/api/books/1/chapters/' && split) {
      return reply(200, [
        { id: 'p1', number: 1, kind: 'front', title: 'قبل' },
        { id: 'h10', number: 2, kind: 'chapter', title: 'الفصل الأول', version: 'v5' },
        { id: 'p13', number: 3, kind: 'chapter', title: 'عنوان', version: 'q1' },
        { id: 'h20', number: 4, kind: 'chapter', title: 'الفصل الثاني', version: 'w1' },
      ]);
    }
    return realFetch(url, init);
  };
  calls.length = 0;
  // the style picker: the paragraph becomes a chapter title; the pause saves it
  ed.node = { ...ed.node, type: 'heading', attrs: { ...ed.node.attrs, level: 1 } };
  ed.opts.onChange();
  // typed while the save is on the wire: kept through the reload
  const firstPause = fire(500);
  ed.type('عنوان الفصل الجديد');
  await firstPause; await settle(); await settle();
  const puts = calls.filter((c) => c[0] === 'PUT');
  const reopened = editors[editors.length - 1];
  out.split = {
    puts: puts.map((c) => c[1]),
    chapter: v.editChapterId,
    version: v.version,
    open: v.open && v.open.block,
    reopened: reopened !== ed && !reopened.destroyed,
    text: convert.plainText(reopened.node),
    relayoutPosts: calls.filter((c) => c[0] === 'POST' && c[1].includes('/relayout/')).length,
    followed: calls.some((c) => c[0] === 'GET' && c[1].startsWith('/api/books/1/relayout/30/')),
    chapters: v.chapters.map((c) => c.id),
  };
  // the text typed meanwhile reached the new chapter in the same flight; nothing waits
  const more = await fire(500); await settle();
  out.kept = { more, puts: calls.filter((c) => c[0] === 'PUT').length, saved: convert.plainText(server.docs.p13[0]), type: server.docs.p13[0].type, level: server.docs.p13[0].attrs.level,
    state: v.editSave.state, pill: v.savePill.text, dirty: v.editDirty };
  // a later paragraph opens and saves; a snapshot can be taken; leaving edit mode is fine
  calls.length = 0;
  const p14 = v.ctx().pages.get(3).lines.findIndex((l) => l.block === 'p14');
  v.showPage(3, { instant: true });
  v.onSheetClick(lineTarget(v, 'right', p14, 0, 3), 'right'); await settle();
  const e14 = editors[editors.length - 1];
  e14.type('فقرة بعد العنوان');
  await fire(500); await settle();
  out.later = { open: v.open && v.open.block, chapter: v.editChapterId, puts: calls.filter((c) => c[0] === 'PUT').map((c) => [c[1], c[2].version]) };
  v.snapshots.label = 'قبل التجربة';
  const snap = await hung(v.createSnapshot());
  out.snapshot = { result: snap, posts: calls.filter((c) => c[0] === 'POST' && c[1] === '/api/books/1/snapshots/').length };
  globalThis.fetch = realFetch;
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


SAVE_STATE_SCENARIO = r"""
const out = {};
(async () => {
  const v = mk();
  await settle();
  v.setMode('edit'); await settle();
  v.onSheetClick(lineTarget(v, 'right', 6, 0, 10), 'right'); await settle();
  const ed = editors[editors.length - 1];
  // ---- typed → «يتم الحفظ…»; saved while its pages are laid out → «تم الحفظ · تُحدَّث الصفحات…»; then «تم الحفظ»
  // (the re-layout's long poll answers when the test lets it)
  const realFetch = globalThis.fetch;
  let finish = null;
  const held = new Promise((resolve) => { finish = resolve; });
  globalThis.fetch = async (url, init) => {
    if (String(url).startsWith('/api/books/1/relayout/7/')) { await held; return reply(200, { id: 7, status: 'done', url, result: { unchanged: true }, pages: [] }); }
    return realFetch(url, init);
  };
  ed.type('نص أول');
  const typed = v.savePill.text;
  await fire(500); await settle();
  const laying = [v.savePill.state, v.savePill.text, v.savePill.title.startsWith('آخر حفظ '), v.renderPill.text];
  finish(); await settle();
  out.flow = { typed, laying, done: [v.savePill.state, v.savePill.text], render: v.renderPill.text };
  globalThis.fetch = realFetch;

  // ---- a server error: said, and tried again by itself after 2 s
  calls.length = 0;
  let fail = 1;
  server.put = (cid) => (fail-- > 0 ? reply(502, {}) : reply(200, { version: 'v3', id: cid, chapters: [{ id: cid, version: 'v3', title: 'الفصل الأول' }], reload: false, changed: true, relayout: null }));
  ed.type('نص ثان');
  await fire(500); await settle();
  out.failed = { state: v.editSave.state, pill: v.savePill.text, retry: pending(2000).length, unload: v.guardUnload({ preventDefault() {}, returnValue: null }) };
  await fire(2000); await settle();
  out.retried = { puts: calls.filter((c) => c[0] === 'PUT').length, state: v.editSave.state, pill: v.savePill.text, unload: v.guardUnload(null) };

  // ---- offline: no timer; the connection back saves it
  calls.length = 0;
  fail = 1;
  Object.defineProperty(globalThis, 'navigator', { value: { onLine: false }, configurable: true, writable: true });
  globalThis.fetch = async (url, init) => { if (init && init.method === 'PUT') throw new TypeError('Failed to fetch'); return realFetch(url, init); };
  ed.type('نص ثالث');
  await fire(500); await settle();
  out.offline = { state: v.editSave.state, pill: v.savePill.text, retry: pending(2000).length + pending(5000).length };
  globalThis.fetch = realFetch; fail = 0;
  globalThis.navigator.onLine = true;
  v.onOnline(); await settle();
  out.online = { puts: calls.filter((c) => c[0] === 'PUT').length, state: v.editSave.state, pill: v.savePill.text };

  // ---- a save that never answers is given up after 30 s (and tried again), never holding later saves
  calls.length = 0;
  let hang = true;
  globalThis.fetch = async (url, init) => {
    if (init && init.method === 'PUT' && hang) {
      calls.push(['PUT', url, null]);
      return new Promise((_resolve, reject) => { init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError'))); });
    }
    return realFetch(url, init);
  };
  ed.type('نص رابع');
  const saving = fire(500); await settle();
  const waiting = [v.editSave.state, pending(30000).length];
  hang = false;
  await fire(30000); await saving; await settle();
  out.timeout = { waiting, state: v.editSave.state, pill: v.savePill.text, retry: pending(2000).length };
  await fire(2000); await settle();
  out.afterTimeout = { puts: calls.filter((c) => c[0] === 'PUT').length, state: v.editSave.state };
  globalThis.fetch = realFetch;
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_the_save_state_between_an_edit_and_its_pages_and_no_silent_loss(tmp_path):
    out = _scenario(tmp_path, "save_state.mjs", SAVE_STATE_SCENARIO)
    # «يتم الحفظ…» from the first key; saved while the pages are laid out; then saved (one pill says it all:
    # the render pill does not repeat the re-layout)
    assert out["flow"] == {
        "typed": "يتم الحفظ…",
        "laying": ["pages", "تم الحفظ · تُحدَّث الصفحات…", True, ""],
        "done": ["saved", "تم الحفظ"],
        "render": "",
    }
    # a server error: said, tried again by itself after 2 s; leaving the page meanwhile asks first
    assert out["failed"] == {"state": "error", "pill": "تعذّر الحفظ · تُعاد المحاولة…", "retry": 1, "unload": True}
    assert out["retried"] == {"puts": 2, "state": "saved", "pill": "تم الحفظ", "unload": False}
    # offline: no retry timer; the connection back saves what waited
    assert out["offline"] == {"state": "error", "pill": "لا اتصال · يُحفظ عند عودته", "retry": 0}
    assert out["online"] == {"puts": 1, "state": "saved", "pill": "تم الحفظ"}
    # a PUT that never answers is given up at 30 s and tried again
    assert out["timeout"] == {
        "waiting": ["saving", 1],
        "state": "error",
        "pill": "تعذّر الحفظ · تُعاد المحاولة…",
        "retry": 1,
    }
    assert out["afterTimeout"] == {"puts": 2, "state": "saved"}


def test_a_chapter_split_by_a_new_heading_never_freezes_the_editor(tmp_path):
    out = _scenario(tmp_path, "split.mjs", SPLIT_SCENARIO)
    split = out["split"]
    # the split, then (same flight) the chapter holding the open paragraph reloaded and the text typed while
    # the split was on the wire saved to it with its version; the paragraph open again
    assert split["puts"] == ["/api/books/1/chapters/h10/", "/api/books/1/chapters/p13/"]
    assert split["chapter"] == "p13" and split["version"] == "p13-saved"
    assert split["open"] == "p13" and split["reopened"] is True
    assert split["chapters"] == ["p1", "h10", "p13", "h20"]
    # the re-layout the save asked for is followed; no second one is requested
    assert split["followed"] is True and split["relayoutPosts"] == 0
    assert split["text"] == "عنوان الفصل الجديد"
    assert out["kept"] == {
        "more": False,
        "puts": 2,
        "saved": "عنوان الفصل الجديد",
        "type": "heading",
        "level": 1,
        "state": "saved",
        "pill": "تم الحفظ",
        "dirty": False,
    }
    # a later paragraph opens and saves with its chapter's version; «حفظ نسخة الآن» goes through
    assert out["later"]["open"] == "p14" and out["later"]["chapter"] == "p13"
    assert out["later"]["puts"] == [["/api/books/1/chapters/p13/", "p13-saved"]]
    assert out["snapshot"] == {"result": True, "posts": 1}
