// The book page (PHASE5_SPEC §9, D47): `/books/<id>/layout/`, the one place to preview and to edit the book.
//   bookPage(config) – the Alpine component, composed of the parts in this folder:
//                      stage.js  the viewer drawing live pages from the WeasyPrint layout, the layout cache, the
//                                re-layouts spliced in place, the footprint, the polling, the filmstrip
//                      style.js  the stylesheet («التنسيق»: القطع، الهوامش، الخطوط، النص، الصفحة، بيانات الكتاب)
//                      cover.js  «الغلاف» (D80): the cover section's controls, the image upload, the render the
//                                stage's cover sheet and the filmstrip's first thumb show
//                      edit.js   edit mode: the chapter's nodes, a paragraph opened in place on its page (the
//                                one-block editor of static/dist/editor.js), the pause → save → re-layout loop,
//                                the chapter's undo, styles, marks, footnotes, «الفقرة»
//                      panel.js  the side panel's tabs, find & replace, the uncertain words, «الأصل», dialogs,
//                                the review drift, the keyboard
//   bookBar         – the top-bar controls (base.html header_actions, outside the root), reading
//                     Alpine.store('bookPage').view
// Config (editor.views.layout → editor.services.page_config): bookId, title, canEdit, mode, chapter, chapters,
// chapterSummaries, drift, uncertainCount, fontCss, relayoutMs, stylesheet, faces, urls, initial {stylesheet,
// preview, layout}. Every part is a function (ctx) → an object of state and methods; `ctx` holds what must not
// be reactive (the layout cache, timers, the open editor, DOM hosts). Western digits everywhere.
(function () {
  'use strict';

  const root = typeof window !== 'undefined' ? window : globalThis;
  const NS = (root.NassakhBook = root.NassakhBook || {});
  NS.parts = NS.parts || {};

  const hasDOM = typeof document !== 'undefined' && typeof document.createElement === 'function' && typeof document.querySelector === 'function';
  const csrfToken = () => {
    const meta = hasDOM ? document.querySelector('meta[name="csrf-token"]') : null;
    return (meta && meta.content) || '';
  };

  const OFFLINE_MESSAGE = 'انقطع الاتصال بالخادم. تحقّق من الشبكة ثم أعد المحاولة.';
  const TIMEOUT_MESSAGE = 'لم يردّ الخادم في الوقت المعتاد؛ تُعاد المحاولة.';
  const API_TIMEOUT_MS = 60000; // no request holds the page for ever (a save waits for its answer)

  // fetch wrapper: never throws; `{ok, status, data, message}` with an Arabic message on failure. `timeout`
  // (ms, default 60 s) gives up a request that does not answer (`{status: 0, timeout: true}`); a network
  // failure tells offline.js, which checks the connection and covers the page while it is gone.
  async function api(url, options) {
    const opts = options || {};
    const method = opts.method || 'GET';
    const headers = { Accept: 'application/json' };
    const init = { method, credentials: 'same-origin', headers, cache: 'no-store' };
    if (opts.keepalive) init.keepalive = true;
    if (method !== 'GET') {
      headers['Content-Type'] = 'application/json';
      headers['X-CSRFToken'] = csrfToken();
      init.body = JSON.stringify(opts.body || {});
    }
    const ms = Number(opts.timeout) || API_TIMEOUT_MS;
    const Abort = typeof AbortController === 'function' && !opts.keepalive ? AbortController : null;
    const control = Abort ? new Abort() : null;
    let timedOut = false;
    const timer = control ? setTimeout(() => { timedOut = true; control.abort(); }, ms) : null;
    if (control) init.signal = control.signal;
    let response;
    try {
      response = await fetch(url, init);
    } catch (_) {
      if (timer) clearTimeout(timer);
      if (!timedOut && root.Nassakh && typeof root.Nassakh.netFailed === 'function') root.Nassakh.netFailed();
      return { ok: false, status: 0, data: null, timeout: timedOut, message: timedOut ? TIMEOUT_MESSAGE : OFFLINE_MESSAGE };
    }
    if (timer) clearTimeout(timer);
    let data = null;
    try { data = await response.json(); } catch (_) { data = null; }
    let message = data && typeof data === 'object' && (data.message || data.detail);
    if (!message) {
      if (response.status === 401) message = 'انتهت الجلسة. سجّل الدخول من جديد.';
      else if (response.status === 403) message = 'هذا الإجراء يتطلب صلاحية محرّر.';
      else message = 'تعذّر تنفيذ الطلب. حاول مرة أخرى.';
    }
    return { ok: response.ok, status: response.status, data, message };
  }

  const util = {
    hasDOM,
    api,
    csrfToken,
    // `__n__` / `__cid__` / `__sid__` / `__rid__` / `__pid__` (a changes plan) in a URL template
    fill: (template, value) => String(template || '').replace(/__(n|cid|sid|rid|pid)__/, String(value)),
    reduced() {
      try { return Boolean(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches); } catch (_) { return false; }
    },
    readLocal(key, fallback) { try { const v = root.localStorage.getItem(key); return v === null || v === undefined ? fallback : v; } catch (_) { return fallback; } },
    writeLocal(key, value) { try { root.localStorage.setItem(key, String(value)); } catch (_) { /* private mode */ } },
    readSession(key) { try { return root.sessionStorage.getItem(key); } catch (_) { return null; } },
    writeSession(key, value) { try { root.sessionStorage.setItem(key, String(value)); } catch (_) { /* fine */ } },
    toast(message) { if (root.Nassakh && root.Nassakh.toast) root.Nassakh.toast(message); },
    // the browser says it is offline, or offline.js covers the page (the server silent)
    offline() {
      if (typeof navigator !== 'undefined' && navigator !== null && navigator.onLine === false) return true;
      const net = root.Nassakh && root.Nassakh.net && typeof root.Nassakh.net.state === 'function' ? root.Nassakh.net.state() : null;
      return Boolean(net && net.lost);
    },
    frame(fn) { if (typeof requestAnimationFrame === 'function') requestAnimationFrame(fn); else setTimeout(fn, 16); },
    q: (el, sel) => (el && typeof el.querySelector === 'function' ? el.querySelector(sel) : null),
    qa: (el, sel) => (el && typeof el.querySelectorAll === 'function' ? Array.from(el.querySelectorAll(sel)) : []),
    closest: (el, sel) => (el && typeof el.closest === 'function' ? el.closest(sel) : null),
    rect: (el) => (el && typeof el.getBoundingClientRect === 'function' ? el.getBoundingClientRect() : { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }),
    focus(el) { if (el && typeof el.focus === 'function') el.focus({ preventScroll: true }); },
    clone: (v) => (v === undefined ? v : JSON.parse(JSON.stringify(v))),
    bundle: () => root.NassakhEditor || null,
    westernDigits: (raw) => String(raw || '').replace(/[٠-٩۰-۹]/g, (d) => { const c = d.charCodeAt(0); return String(c >= 0x06f0 ? c - 0x06f0 : c - 0x0660); }),
  };
  NS.util = util;

  // Parts are merged with their getters intact (Object.assign would copy a getter's value once).
  function compose(parts) {
    const out = {};
    parts.forEach((part) => Object.defineProperties(out, Object.getOwnPropertyDescriptors(part)));
    return out;
  }
  NS.compose = compose;

  function register() {
    if (typeof Alpine === 'undefined') return;
    if (typeof Alpine.store === 'function') Alpine.store('bookPage', { view: null });

    Alpine.data('bookBar', () => ({
      get v() { return typeof Alpine.store === 'function' && Alpine.store('bookPage') ? Alpine.store('bookPage').view : null; },
    }));

    Alpine.data('bookPage', (cfg = {}) => {
      const ctx = {
        cfg,
        urls: cfg.urls || {},
        initial: cfg.initial || {},
        timers: {},
        pages: new Map(), // n → the page's layout (the live layout, fetched in windows)
        thumbs: new Map(), // n → {url, url2x} of the filmstrip
        dom: {},
      };
      const names = ['stage', 'style', 'cover', 'edit', 'panel'];
      const parts = names.map((name) => (NS.parts[name] ? NS.parts[name](ctx) : {}));
      const self = compose(parts);
      const inits = names.map((name) => `_init_${name}`);
      const destroys = names.map((name) => `_destroy_${name}`);
      Object.defineProperties(self, Object.getOwnPropertyDescriptors({
        init() {
          inits.forEach((fn) => { if (typeof this[fn] === 'function') this[fn](); });
          if (typeof Alpine.store === 'function' && Alpine.store('bookPage')) Alpine.store('bookPage').view = this;
          if (typeof this.start === 'function') this.start();
        },
        destroy() {
          destroys.forEach((fn) => { if (typeof this[fn] === 'function') this[fn](); });
          Object.values(ctx.timers).forEach((t) => clearTimeout(t));
          if (typeof Alpine.store === 'function' && Alpine.store('bookPage')) Alpine.store('bookPage').view = null;
        },
        ctx() { return ctx; },
      }));
      return self;
    });

    // The undo toast (DESIGN.md §6): one at a time, one action; a new one replaces the old. `local` marks a
    // toast whose undo is the chapter's own history (the next edit retires it). The template binds its button
    // to `hasAction`, never to `action`: Alpine *calls* a function that a directive's expression evaluates
    // to, so `x-show="…action"` ran every undo the moment its toast appeared (a restore, a digit conversion
    // or a replace-all undone at once, again on every re-evaluation).
    if (typeof Alpine.store === 'function') {
      Alpine.store('bookToast', {
        message: '',
        visible: false,
        action: null,
        hasAction: false,
        local: false,
        timer: null,
        show(message, action, ms = 8000, local = false) {
          clearTimeout(this.timer);
          this.message = message;
          this.action = typeof action === 'function' ? action : null;
          this.hasAction = this.action !== null;
          this.local = Boolean(local);
          this.visible = true;
          this.timer = setTimeout(() => this.hide(), ms);
        },
        run() { const fn = this.action; this.hide(); if (fn) fn(); },
        hide() { clearTimeout(this.timer); this.visible = false; this.action = null; this.hasAction = false; },
      });
    }
  }

  if (hasDOM && typeof document.addEventListener === 'function') document.addEventListener('alpine:init', register);
  NS.register = register;
})();
