// The connection, app-wide (templates/base.html): most of the work is API calls, so when the browser goes
// offline (or the server stops answering) the page is covered and its work waits, and no edit is made that
// could not be saved; when the connection is back the cover goes and the page carries on.
//   Alpine.store('net')          {online, lost, checking, reason, tries}: every screen may read it
//   Alpine.data('offlineCover')  the cover's card (base.html): its title, its line, «إعادة المحاولة الآن»
//   window.Nassakh.netFailed()   a request failed on the network: the server is asked at once (a page's API
//                                helper calls it: static/src/js/book/page.js)
//   window events                `nassakh-offline`, `nassakh-online` (the book page saves what waited)
// While covered, the page under the cover is `inert` (no click, no focus) and its keys are held back, the
// cover's own button excepted. A blip shorter than GRACE_MS never shows. The server is asked with a HEAD of a
// static file (`data-probe` on the cover), every few seconds while the connection is gone.
(function () {
  'use strict';

  const root = typeof window !== 'undefined' ? window : globalThis;
  const GRACE_MS = 1200;
  const PROBE_MS = [2000, 4000, 8000, 15000];
  const PROBE_TIMEOUT_MS = 6000;
  const COVER = '[data-net-cover]';
  const TEXT = {
    offline: 'لا اتصال بالإنترنت',
    server: 'تعذّر الوصول إلى الخادم',
    checking: 'يُتحقَّق من الاتصال…',
    waiting: 'يُعاد التحقق تلقائيًا كل بضع ثوانٍ.',
    back: 'عاد الاتصال',
  };

  const hasDOM = typeof document !== 'undefined' && document !== null && typeof document.querySelector === 'function';
  const browserOffline = () => typeof navigator !== 'undefined' && navigator !== null && navigator.onLine === false;

  // the state (the Alpine store once Alpine is there; a plain object before)
  let state = { online: true, lost: false, checking: false, reason: '', tries: 0 };
  let graceTimer = null;
  let probeTimer = null;
  let probing = null;
  let made = []; // the elements this script made inert

  function set(patch) {
    Object.assign(state, patch);
  }
  function emit(name) {
    try { if (typeof root.dispatchEvent === 'function' && typeof root.CustomEvent === 'function') root.dispatchEvent(new root.CustomEvent(name)); } catch (_) { /* fine */ }
  }
  function cover() { return hasDOM ? document.querySelector(COVER) : null; }

  // Everything but the cover inert (and back): no click or focus reaches the page while the connection is gone.
  function hold(on) {
    if (!hasDOM || !document.body) return;
    if (on) {
      const keep = cover();
      made = Array.from(document.body.children || []).filter((el) => el !== keep && !el.inert && !(el.matches && el.matches('script, style, svg.hidden')));
      made.forEach((el) => { el.inert = true; if (el.setAttribute) el.setAttribute('aria-hidden', 'true'); });
      try { if (document.activeElement && typeof document.activeElement.blur === 'function') document.activeElement.blur(); } catch (_) { /* fine */ }
    } else {
      made.forEach((el) => { el.inert = false; if (el.removeAttribute) el.removeAttribute('aria-hidden'); });
      made = [];
    }
  }
  // the page's keys wait behind the cover (its own button and Tab excepted)
  function onKey(e) {
    if (!state.lost) return;
    const box = cover();
    if (box && e.target && typeof box.contains === 'function' && box.contains(e.target)) return;
    e.stopImmediatePropagation();
    if (e.key !== 'Tab') e.preventDefault();
  }

  function lose(reason) {
    clearTimeout(graceTimer);
    graceTimer = null;
    if (state.lost) { set({ reason }); schedule(); return; }
    set({ lost: true, online: false, reason, tries: 0 });
    hold(true);
    emit('nassakh-offline');
    schedule();
  }
  function regain() {
    clearTimeout(graceTimer);
    clearTimeout(probeTimer);
    graceTimer = null;
    probeTimer = null;
    const was = state.lost;
    set({ lost: false, online: true, checking: false, reason: '', tries: 0 });
    if (!was) return;
    hold(false);
    if (root.Nassakh && typeof root.Nassakh.toast === 'function') root.Nassakh.toast(TEXT.back);
    emit('nassakh-online');
  }
  function schedule() {
    clearTimeout(probeTimer);
    const ms = PROBE_MS[Math.min(state.tries, PROBE_MS.length - 1)];
    probeTimer = setTimeout(() => { probeTimer = null; check(); }, ms);
  }

  // Ask the server (a HEAD of a static file, never cached): true when it answers.
  async function reachable() {
    const box = cover();
    const url = (box && box.getAttribute && box.getAttribute('data-probe')) || '';
    if (!url || typeof root.fetch !== 'function') return !browserOffline();
    const Abort = typeof root.AbortController === 'function' ? root.AbortController : null;
    const control = Abort ? new Abort() : null;
    const timer = control ? setTimeout(() => control.abort(), PROBE_TIMEOUT_MS) : null;
    try {
      const response = await root.fetch(`${url}${url.includes('?') ? '&' : '?'}ping=${Date.now()}`, { method: 'HEAD', cache: 'no-store', credentials: 'same-origin', signal: control ? control.signal : undefined });
      return Boolean(response) && response.status > 0 && response.status < 500;
    } catch (_) {
      return false;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }
  // One check at a time: back → regain; still gone → cover (or keep it) and ask again later.
  function check() {
    if (probing) return probing;
    set({ checking: true });
    const run = (async () => {
      const ok = !browserOffline() && (await reachable());
      set({ checking: false });
      if (ok) { regain(); return true; }
      const off = browserOffline();
      set({ tries: state.tries + 1 });
      // the server silent once (a restart): asked again shortly before the page is covered
      if (!state.lost && !off && state.tries < 2) {
        clearTimeout(probeTimer);
        probeTimer = setTimeout(() => { probeTimer = null; check(); }, GRACE_MS);
        return false;
      }
      lose(off ? 'offline' : 'server');
      return false;
    })();
    probing = run;
    // cleared once settled (an answer known at once must not leave `probing` set for good)
    run.then(() => { if (probing === run) probing = null; });
    return run;
  }

  function onOffline() {
    clearTimeout(graceTimer);
    graceTimer = setTimeout(() => { graceTimer = null; if (browserOffline()) lose('offline'); else check(); }, GRACE_MS);
  }
  function onOnline() {
    clearTimeout(graceTimer);
    graceTimer = null;
    if (state.lost) check();
  }
  // a request failed on the network: the server is asked (not twice at once); a blip changes nothing
  function netFailed() {
    if (state.lost) return probing || Promise.resolve(false);
    return check();
  }

  function start() {
    if (typeof root.addEventListener === 'function') {
      root.addEventListener('offline', onOffline);
      root.addEventListener('online', onOnline);
      root.addEventListener('keydown', onKey, true);
    }
    if (browserOffline()) onOffline();
  }

  const api = { netFailed, check, state: () => state, TEXT, GRACE_MS, PROBE_MS };
  root.Nassakh = Object.assign(root.Nassakh || {}, { netFailed, net: api });

  function register() {
    if (typeof Alpine === 'undefined') return;
    // the store replaces the plain object (Alpine's reactive copy): the cover follows every change
    if (typeof Alpine.store === 'function') {
      Alpine.store('net', Object.assign({}, state));
      state = Alpine.store('net');
    }
    Alpine.data('offlineCover', () => ({
      get net() { return typeof Alpine.store === 'function' ? Alpine.store('net') : state; },
      get title() { return this.net.reason === 'server' ? TEXT.server : TEXT.offline; },
      get statusText() { return this.net.checking ? TEXT.checking : TEXT.waiting; },
      retry() { return check(); },
    }));
  }

  if (hasDOM && typeof document.addEventListener === 'function') document.addEventListener('alpine:init', register);
  start();
  root.NassakhNet = { register, onOffline, onOnline, onKey, check, netFailed, state: () => state, hold };
})();
