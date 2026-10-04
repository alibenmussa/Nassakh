"""The page's pure helpers (static/src/js/research.js) under Node: page labels, counts, the verify diff, the
clients' steps and exactly what each one pastes, with the key filled in."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parent.parent / "static" / "src" / "js" / "research.js"
URL = "https://nassakh.example/mcp"

HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const R = globalThis.NassakhResearch;
const url = 'https://nassakh.example/mcp';
const out = {
  printed: R.pageLabel({ number: 5, printed: '15' }, { number: 6, printed: '16' }),
  scan: R.pageLabel({ number: 40, printed: null }),
  counts: [1, 2, 7, 11].map((n) => R.countLabel(n, ['نتيجة واحدة', 'نتيجتان', 'نتائج', 'نتيجة'])),
  pieces: R.diffPieces([
    { op: 'equal', source: 'إنما الأعمال' },
    { op: 'replaced', source: 'نوى', quote: 'أراد', needs_image: false },
    { op: 'missing', source: 'وإنما', needs_image: true },
    { op: 'added', quote: 'مسلم' },
  ]),
  pastes: R.pastes(url, 'nsk_abc'),
  placeholder: R.pastes(url, null),
  clients: R.CLIENTS.map((c) => [c.id, c.urlOnly]),
  steps: Object.fromEntries(R.CLIENTS.map((c) => [c.id, R.steps(c.id, false)])),
  localSteps: R.steps('claude', true),
  date: R.dateLabel('2026-10-04T09:30:00Z'),
};
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def helpers(tmp_path_factory) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    harness = tmp_path_factory.mktemp("research-js") / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    run = subprocess.run(["node", str(harness), str(JS)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_labels_counts_and_the_diff_pieces(helpers):
    assert helpers["printed"] == "ص 15–16" and helpers["scan"] == "صفحة المسح 40"
    assert helpers["counts"] == ["نتيجة واحدة", "نتيجتان", "7 نتائج", "11 نتيجة"]  # Western digits
    pieces = helpers["pieces"]
    assert [p["kind"] for p in pieces] == ["same", "replaced", "missing", "added"]
    assert pieces[1]["quote"] == "أراد" and pieces[2]["image"] is True
    assert "2026" in helpers["date"] and not any("٠" <= ch <= "٩" for ch in helpers["date"])


def test_each_client_pastes_exactly_its_format_with_the_key(helpers):
    pastes = helpers["pastes"]
    headers = {"Authorization": "Bearer nsk_abc"}
    assert pastes["url"] == URL
    assert pastes["secret"] == f"{URL}/k/nsk_abc"  # the URL-only clients (Claude, ChatGPT)
    assert pastes["header"] == "Authorization: Bearer nsk_abc"
    header = '--header "Authorization: Bearer nsk_abc"'
    assert pastes["code"] == f"claude mcp add --transport http nassakh {URL} {header}"
    server = {"type": "http", "url": URL, "headers": headers}
    assert json.loads(pastes["http"]) == {"mcpServers": {"nassakh": server}}
    assert json.loads(pastes["cursor"]) == {"mcpServers": {"nassakh": {"url": URL, "headers": headers}}}
    assert json.loads(pastes["vscode"]) == {"servers": {"nassakh": server}}
    # until a key is made every paste shows where it goes
    placeholder = helpers["placeholder"]
    names = ("secret", "header", "code", "http", "cursor", "vscode")
    assert all("nsk_…" in placeholder[name] for name in names)


def test_the_clients_steps_name_the_menus_and_paste_the_right_thing(helpers):
    assert helpers["clients"] == [
        ["claude", True],
        ["chatgpt", True],
        ["code", False],
        ["cursor", False],
        ["vscode", False],
        ["other", False],
    ]
    steps = helpers["steps"]
    claude = " ".join(step["html"] for step in steps["claude"])
    assert "الإعدادات (Settings)" in claude and "الموصِّلات (Connectors)" in claude
    assert "أضف موصِّلًا مخصَّصًا (Add custom connector)" in claude
    assert [step["paste"] for step in steps["claude"] if step["paste"]] == ["secret"]
    chatgpt = " ".join(step["html"] for step in steps["chatgpt"])
    assert "وضع المطوّر (Developer mode)" in chatgpt and "No authentication" in chatgpt
    assert [step["paste"] for step in steps["chatgpt"] if step["paste"]] == ["secret"]
    assert [step["paste"] for step in steps["code"] if step["paste"]] == ["code"]
    assert [step["paste"] for step in steps["cursor"] if step["paste"]] == ["cursor"]
    assert [step["paste"] for step in steps["vscode"] if step["paste"]] == ["vscode"]
    assert [step["paste"] for step in steps["other"] if step["paste"]] == ["url", "header", "http"]
    # the secret URL is called a password once, where it is pasted
    secret_step = next(step for step in steps["claude"] if step["paste"] == "secret")
    assert "كلمة المرور" in secret_step["note"]
    # a local public URL: the URL-only clients are told it works after deployment
    local = next(step for step in helpers["localSteps"] if step["paste"] == "secret")
    assert "بعد نشر الموقع" in local["note"] and "كلمة المرور" in local["note"]
    assert "بعد نشر الموقع" not in secret_step["note"]
