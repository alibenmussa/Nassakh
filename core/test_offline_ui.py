"""The connection cover (static/src/js/offline.js, templates/base.html), app-wide: offline, or the server silent,
the page is covered, inert and its keys held back until the connection is back; the book page saves what
waited on `nassakh-online`.

- the templates: the cover and its script on every screen built on base.html, the book page listening
- offline.js under Node with a tiny DOM: a blip shorter than the grace never covers; offline covers (the page
  inert, keys held back but the cover's own); the server asked every few seconds; back online the cover goes,
  `nassakh-online` fires and «عاد الاتصال» is said; a failed request asks the server, a silent server covers
  only when it is still silent a moment later"""

from __future__ import annotations

import json
import subprocess

from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse

import pytest

from core.test_layout_ui import NODE, ROOT

pytestmark = pytest.mark.django_db

OFFLINE_JS = ROOT / "static" / "src" / "js" / "offline.js"


def _editor() -> Client:
    user = User.objects.create_user("net-editor", password="pw")
    group, _ = Group.objects.get_or_create(name="editor")
    user.groups.add(group)
    client = Client()
    client.force_login(user)
    return client


def test_every_screen_carries_the_connection_cover():
    client = _editor()
    body = client.get(reverse("books:list")).content.decode()
    assert "data-net-cover" in body and 'x-data="offlineCover"' in body
    assert 'data-probe="/static/vendor/alpine.min.js"' in body
    assert "src/js/offline.js" in body and 'href="#i-wifi-off"' in body and 'id="i-wifi-off"' in body
    assert body.index("src/js/ui.js") < body.index("src/js/offline.js") < body.index("src/js/book/page.js")
    assert "لا اتصال بالإنترنت" in body and "إعادة المحاولة الآن" in body


def test_the_book_page_saves_what_waited_when_the_connection_is_back():
    layout = (ROOT / "templates" / "editor" / "layout.html").read_text(encoding="utf-8")
    assert '@nassakh-online.window="onOnline()"' in layout


HARNESS = r"""
import { readFileSync } from 'node:fs';
const [, , file] = process.argv;
const timers = []; let tid = 0;
globalThis.setTimeout = (fn, ms) => { tid += 1; timers.push({ id: tid, fn, ms: ms || 0, cleared: false }); return tid; };
globalThis.clearTimeout = (id) => { const t = timers.find((x) => x.id === id); if (t) t.cleared = true; };
const pending = (ms) => timers.filter((t) => !t.cleared && (ms === undefined || t.ms === ms));
const settle = async () => { for (let i = 0; i < 10; i += 1) await new Promise((r) => setImmediate(r)); };
const fire = async (ms) => { const list = pending(ms); const t = list[list.length - 1]; if (!t) return false; t.cleared = true; t.fn(); await settle(); return true; };
class El {
  constructor(tag, attrs = {}) { this.tagName = tag.toUpperCase(); this.attrs = attrs; this.inert = false; this.children = []; this.parent = null; }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; } setAttribute(k, v) { this.attrs[k] = String(v); } removeAttribute(k) { delete this.attrs[k]; }
  contains(n) { while (n) { if (n === this) return true; n = n.parent; } return false; }
  matches(sel) { return sel.split(',').some((s) => s.trim() === 'script' ? this.tagName === 'SCRIPT' : false); }
  blur() { blurred.push(this.tagName); }
}
const blurred = [];
const shell = new El('div', { class: 'app-shell' });
const field = new El('input'); field.parent = shell; shell.children.push(field);
const toastBox = new El('div');
const cover = new El('div', { 'data-net-cover': '', 'data-probe': '/static/vendor/alpine.min.js' });
const button = new El('button'); button.parent = cover; cover.children.push(button);
const script = new El('script');
globalThis.document = { body: { children: [shell, toastBox, cover, script] }, activeElement: field, querySelector: (s) => (s === '[data-net-cover]' ? cover : null), addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); } };
const inits = []; const listeners = {}; const events = []; const toasts = [];
globalThis.window = globalThis;
globalThis.addEventListener = (e, fn, capture) => { (listeners[e] = listeners[e] || []).push({ fn, capture: Boolean(capture) }); };
globalThis.dispatchEvent = (ev) => { events.push(ev.type); return true; };
globalThis.CustomEvent = class { constructor(type) { this.type = type; } };
Object.defineProperty(globalThis, 'navigator', { value: { onLine: true }, configurable: true, writable: true });
globalThis.Nassakh = { toast: (m) => toasts.push(m) };
let server = 'up'; const probes = [];
globalThis.fetch = async (url, init) => { probes.push([init.method, url.split('?')[0]]); if (server === 'down') throw new TypeError('Failed to fetch'); return { status: 200 }; };
const stores = {}; const data = {};
globalThis.Alpine = { store: (n, v) => { if (v !== undefined) stores[n] = v; return stores[n]; }, data: (n, f) => { data[n] = f; } };
(0, eval)(readFileSync(file, 'utf8'));
inits.forEach((fn) => fn());
const net = () => stores.net;
const key = (target) => { const ev = { key: 'e', target, stopped: false, prevented: false, stopImmediatePropagation() { this.stopped = true; }, preventDefault() { this.prevented = true; } }; listeners.keydown.forEach((l) => l.fn(ev)); return [ev.stopped, ev.prevented]; };
const card = data.offlineCover();
const out = {};
(async () => {
  out.start = { lost: net().lost, capture: listeners.keydown.map((l) => l.capture), key: key(shell) };
  // ---- a blip: offline, back within the grace: nothing shows
  navigator.onLine = false; listeners.offline.forEach((l) => l.fn());
  navigator.onLine = true; listeners.online.forEach((l) => l.fn()); await settle();
  out.blip = { lost: net().lost, grace: pending(1200).length, events: events.slice() };
  // ---- offline: covered after the grace; the page inert, its keys held back, the cover's own let through
  navigator.onLine = false; listeners.offline.forEach((l) => l.fn());
  await fire(1200);
  out.offline = { lost: net().lost, reason: net().reason, title: card.title, status: card.statusText, inert: [shell.inert, toastBox.inert, cover.inert, script.inert], hidden: shell.attrs['aria-hidden'],
    blurred: blurred.slice(), events: events.slice(), page: key(field), own: key(button), probe: pending(2000).length };
  // ---- still offline: asked again later, longer each time (no request while the browser says offline)
  await fire(2000);
  out.still = { lost: net().lost, tries: net().tries, next: pending(4000).length, probes: probes.length };
  // ---- back: the server answers, the cover goes, the page carries on
  navigator.onLine = true; listeners.online.forEach((l) => l.fn()); await settle();
  out.back = { lost: net().lost, inert: [shell.inert, toastBox.inert], hidden: shell.attrs['aria-hidden'] === undefined, events: events.slice(), toasts: toasts.slice(), probes: probes.slice(), timers: pending().length, key: key(shell) };
  // ---- a request failed on the network: the server asked; silent once, asked again; silent twice: covered
  server = 'down'; probes.length = 0;
  await Nassakh.netFailed(); await settle();
  const once = { lost: net().lost, again: pending(1200).length };
  await fire(1200);
  out.server = { once, lost: net().lost, reason: net().reason, title: card.title, probes: probes.length };
  server = 'up';
  await card.retry(); await settle();
  out.serverBack = { lost: net().lost, events: events.slice(-1) };
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_offline_cover_under_node(tmp_path):
    harness = tmp_path / "offline.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    run = subprocess.run(
        [NODE, str(harness), str(OFFLINE_JS)], capture_output=True, text=True, timeout=60, cwd=tmp_path
    )
    assert run.returncode == 0, run.stderr[-4000:]
    out = json.loads(run.stdout.strip().splitlines()[-1])
    # keys are watched in the capture phase (before every screen's own); online, nothing is held back
    assert out["start"] == {"lost": False, "capture": [True], "key": [False, False]}
    # a blip shorter than the grace never covers the page
    assert out["blip"] == {"lost": False, "grace": 0, "events": []}
    # offline: covered, everything but the cover inert (scripts left alone), the focus dropped, keys held back
    assert out["offline"] == {
        "lost": True,
        "reason": "offline",
        "title": "لا اتصال بالإنترنت",
        "status": "يُعاد التحقق تلقائيًا كل بضع ثوانٍ.",
        "inert": [True, True, False, False],
        "hidden": "true",
        "blurred": ["INPUT"],
        "events": ["nassakh-offline"],
        "page": [True, True],
        "own": [False, False],
        "probe": 1,
    }
    assert out["still"] == {"lost": True, "tries": 1, "next": 1, "probes": 0}
    # back: one HEAD of the probe file, the cover gone, the page live again, the book page told
    assert out["back"]["lost"] is False and out["back"]["inert"] == [False, False] and out["back"]["hidden"]
    assert out["back"]["events"] == ["nassakh-offline", "nassakh-online"]
    assert out["back"]["toasts"] == ["عاد الاتصال"] and out["back"]["timers"] == 0
    assert out["back"]["probes"] == [["HEAD", "/static/vendor/alpine.min.js"]]
    assert out["back"]["key"] == [False, False]
    # a failed request: the server asked; silent once → asked again shortly; silent twice → covered
    assert out["server"] == {
        "once": {"lost": False, "again": 1},
        "lost": True,
        "reason": "server",
        "title": "تعذّر الوصول إلى الخادم",
        "probes": 2,
    }
    assert out["serverBack"] == {"lost": False, "events": ["nassakh-online"]}
