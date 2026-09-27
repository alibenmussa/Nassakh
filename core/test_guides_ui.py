"""The «التخطيط» mode's `bookGuides` component (static/src/js/processing.js) under Node with a tiny Alpine
stub: the order of Esc's layers and the body a started book's pending change posts. The dashboard it reads is
a stub (`Alpine.store('book').dash`); core/test_theatre.py runs the whole mode on the rendered templates."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "src" / "js"

HARNESS = r"""
const fs = require('fs');
const reg = {}; const inits = []; const stores = {}; const calls = [];
globalThis.window = globalThis;
globalThis.document = { addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); }, querySelector: () => null };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: (n, v) => { if (v !== undefined) { stores[n] = v; return v; } return stores[n]; } };
globalThis.setTimeout = () => 0; globalThis.clearTimeout = () => {};
globalThis.fetch = async (url, opts) => { calls.push({ url, body: JSON.parse(opts.body) }); return { ok: true, status: 200, json: async () => ({ guides: {} }) }; };
eval(fs.readFileSync(process.argv[2], 'utf8'));
inits.forEach((fn) => fn());
// a started book (?view=guides): changes wait for «حفظ وإعادة التعرّف على الصفحة»
const rerendered = [];
const dash = { layoutStage: false, guidesUrls: { page: '/api/pages/__id__/guides/' }, rerenderGuides(pid) { rerendered.push(pid); }, setPageGuides() {}, page: () => ({ number: 4 }), guidesBook: {} };
Alpine.store('book', { dash });
const mount = () => { const g = reg.bookGuides(); g.$el = null; g.$nextTick = (fn) => fn(); g.init(); g.pageId = '815'; return g; };
(async () => {
  const out = {};
  // a moved line not saved yet, then a band chip's menu opened: Esc closes the menu and keeps the move
  let g = mount();
  g.change('815', { set: { footnote_line: 0.83 } }, null);
  g.menu = { pid: '815', kind: 'footnote' };
  out.escMenu = [g.escape(), g.menu, g.hasPending('815')];
  out.escPending = [g.escape(), g.hasPending('815')]; // the second Esc drops the move
  // «التلقائي» then a drag of the running head: one save posts both (the server clears, then sets)
  g = mount();
  g.change('815', { reset: true }, null);
  g.change('815', { set: { header_cut: 0.07 } }, null);
  await g.savePending('815');
  out.resetThenDrag = calls.map((c) => [c.url, c.body]);
  console.log(JSON.stringify(out));
})();
"""  # noqa: E501


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_esc_closes_the_band_menu_before_it_drops_a_pending_move_and_a_reset_then_drag_posts_both(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    run = subprocess.run(
        ["node", str(harness), str(JS / "processing.js")], capture_output=True, text=True, timeout=60
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    # §3.12: Esc closes the top layer first; the unsaved move survives the menu's Esc
    assert out["escMenu"] == [True, None, True]
    assert out["escPending"] == [True, False]
    # processing/tests.py::test_a_pending_automatic_then_a_drag_saves_the_dragged_line is the server's half
    assert out["resetThenDrag"] == [
        [
            "/api/pages/815/guides/",
            {"merge": True, "set": {"header_cut": 0.07}, "unset": [], "reset": True, "stage": "ocr"},
        ]
    ]
