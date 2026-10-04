"""The page's pure helpers (static/src/js/research.js) under Node: page labels, the verify diff, the MCP
client snippets with the key filled in."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parent.parent / "static" / "src" / "js" / "research.js"

HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const R = globalThis.NassakhResearch;
const out = {
  printed: R.pageLabel({ number: 5, printed: '15' }, { number: 6, printed: '16' }),
  scan: R.pageLabel({ number: 40, printed: null }),
  pieces: R.diffPieces([
    { op: 'equal', source: 'إنما الأعمال' },
    { op: 'replaced', source: 'نوى', quote: 'أراد', needs_image: false },
    { op: 'missing', source: 'وإنما', needs_image: true },
    { op: 'added', quote: 'مسلم' },
  ]),
  snippets: R.snippets('https://nassakh.example/mcp', 'nsk_abc'),
  placeholder: R.snippets('https://nassakh.example/mcp', null).code,
  date: R.dateLabel('2026-10-04T09:30:00Z'),
};
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_research_helpers_under_node(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    run = subprocess.run(["node", str(harness), str(JS)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["printed"] == "ص 15–16" and out["scan"] == "صفحة المسح 40"
    assert [p["kind"] for p in out["pieces"]] == ["same", "replaced", "missing", "added"]
    assert out["pieces"][1]["quote"] == "أراد" and out["pieces"][2]["image"] is True
    code = out["snippets"]["code"]
    assert code.startswith("claude mcp add --transport http nassakh https://nassakh.example/mcp ")
    assert code.endswith('--header "Authorization: Bearer nsk_abc"')
    desktop = json.loads(out["snippets"]["desktop"])["mcpServers"]["nassakh"]
    assert (
        desktop["args"][-1] == "Authorization:${NASSAKH_AUTH}"
        and desktop["env"]["NASSAKH_AUTH"] == "Bearer nsk_abc"
    )
    http = json.loads(out["snippets"]["http"])["mcpServers"]["nassakh"]
    assert http == {
        "type": "http",
        "url": "https://nassakh.example/mcp",
        "headers": {"Authorization": "Bearer nsk_abc"},
    }
    assert "nsk_…" in out["placeholder"]
    assert "2026" in out["date"] and not any("٠" <= ch <= "٩" for ch in out["date"])  # Western digits
