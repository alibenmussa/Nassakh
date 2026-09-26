// The stage bar (PHASE7_SPEC §5.1, D76): «التخطيط · المعالجة · المراجعة · المخطوطة · الكتاب · الإخراج» in the top bar of
// every book screen (templates/partials/_stage_bar.html, styles in static/src/components/layout.css `stg-`).
//   stageBar(cfg)            – the Alpine component: cfg = {book, current, url, steps}. `steps` is the server's
//                              `books.services.book_stages(book, current)` when the view passed it, else the bar
//                              fetches `api:book_stages` once on mount.
//   Alpine.store('stages')   – `live`: what a screen that polls anyway (the dashboard) knows sooner than the
//                              server's steps: {key: {state, detail, count, percent}}, drawn over that step
//   window.NassakhStages     – pure helpers for the Node tests, and `changed(book)`: a screen says its book moved
//                              on (an approval, a finished assembly, an apply, a finished export)
// Live without a poll: the window event `nassakh:stages` (re-broadcast to the book's other tabs on
// BroadcastChannel('nassakh'), where review.js already announces its saves), the channel itself, the tab coming
// back into view and a return from the back-forward cache. At most one request every REFRESH_MS; a trigger
// inside that window is kept for its end. The width collapse is CSS only (a container query on the bar): a wide bar
// shows every step's count and the current step's detail («17×24 سم · 84 صفحة»), then the counts, then the current
// step's count only, then one button; the details are always in the menu and read by a screen reader.
(function () {
  'use strict';

  const root = typeof window !== 'undefined' ? window : globalThis;
  const STEPS = [
    { key: 'pages', label: 'التخطيط' },
    { key: 'ocr', label: 'المعالجة' },
    { key: 'review', label: 'المراجعة' },
    { key: 'manuscript', label: 'المخطوطة' },
    { key: 'book', label: 'الكتاب' },
    { key: 'export', label: 'الإخراج' },
  ];
  const STATES = ['todo', 'active', 'done', 'stale', 'attention', 'blocked'];
  // each state written out for a screen reader (the marks are drawn only)
  const STATE_WORDS = {
    done: '(مكتملة)',
    active: '(جارية)',
    stale: '(أقدم من النص)',
    attention: '(تحتاج انتباهًا)',
    todo: '(لم تبدأ)',
    blocked: '(غير متاحة بعد)',
  };
  const REFRESH_MS = 1500;
  const CHANNEL = 'nassakh';
  const EVENT = 'nassakh:stages';

  // «رُوجعت 6 من 8 صفحات» → «6/8»: the short count drawn beside a step's label (the server may send it as `count`).
  function shortCount(step) {
    if (!step) return '';
    if (step.count !== undefined && step.count !== null && step.count !== '') return String(step.count);
    const m = /(\d+)\s*من\s*(\d+)/.exec(String(step.detail || ''));
    return m ? `${m[1]}/${m[2]}` : '';
  }

  // The server's steps (a list, or `{steps}` / `{stages}`) as the bar's six, in their fixed order: a step it
  // leaves out keeps its label and waits as `todo`; an unknown state reads `todo`; `current` is the screen's.
  function normalize(raw, current) {
    const list = Array.isArray(raw) ? raw : (raw && (raw.steps || raw.stages)) || [];
    const byKey = new Map(list.filter((s) => s && s.key).map((s) => [s.key, s]));
    const here = current || (list.find((s) => s && s.current) || {}).key || '';
    return STEPS.map((base) => {
      const s = byKey.get(base.key) || {};
      const state = STATES.includes(s.state) ? s.state : 'todo';
      return {
        key: base.key,
        label: s.label || base.label,
        url: state === 'blocked' ? '' : String(s.url || ''),
        state,
        stateLabel: String(s.state_label || ''),
        detail: String(s.detail || ''),
        hint: String(s.hint || ''),
        count: s.count === undefined || s.count === null ? '' : String(s.count),
        current: base.key === here,
      };
    });
  }

  // A step with what a screen knows live (`{state, detail, count, percent}`) drawn over the server's.
  function merge(step, live) {
    if (!live) return step;
    const out = Object.assign({}, step);
    if (live.state && STATES.includes(live.state) && step.state !== 'blocked') out.state = live.state;
    if (live.detail !== undefined) out.detail = String(live.detail || '');
    if (live.count !== undefined) out.count = String(live.count || '');
    if (live.percent !== undefined && live.percent !== null) out.percent = Math.max(0, Math.min(100, Number(live.percent) || 0));
    return out;
  }

  let channel = null;
  function openChannel() {
    if (channel) return channel;
    try {
      if (typeof BroadcastChannel === 'function') {
        channel = new BroadcastChannel(CHANNEL);
        if (typeof channel.unref === 'function') channel.unref(); // Node (the tests): never hold the process open
      }
    } catch (_) { channel = null; }
    return channel;
  }

  // A screen's news: this book moved on. The bar of this tab refreshes; the book's other tabs hear it on the channel.
  function changed(book) {
    try {
      if (typeof CustomEvent === 'function' && typeof root.dispatchEvent === 'function') {
        root.dispatchEvent(new CustomEvent(EVENT, { detail: { book: Number(book) || null } }));
      }
    } catch (_) { /* no events (a sandbox) */ }
  }

  root.NassakhStages = { STEPS, STATES, STATE_WORDS, REFRESH_MS, normalize, merge, shortCount, changed };

  const register = () => {
    if (typeof Alpine === 'undefined') return;
    if (typeof Alpine.store === 'function') {
      Alpine.store('stages', {
        live: {},
        // `null` clears the key: the server's step shows again
        set(key, patch) { const next = Object.assign({}, this.live); if (patch) next[key] = patch; else delete next[key]; this.live = next; },
      });
    }

    Alpine.data('stageBar', (cfg = {}) => {
      let lastAt = 0;
      let trailing = null;
      let own = null; // this bar's channel listener
      const handlers = [];
      const listen = (target, name, fn) => {
        if (target && typeof target.addEventListener === 'function') { target.addEventListener(name, fn); handlers.push([target, name, fn]); }
      };
      return {
        book: Number(cfg.book) || 0,
        current: String(cfg.current || ''),
        url: String(cfg.url || ''),
        steps: normalize(cfg.steps || [], cfg.current),
        loaded: Array.isArray(cfg.steps) ? cfg.steps.length > 0 : Boolean(cfg.steps && (cfg.steps.steps || cfg.steps.stages)),
        menuOpen: false,

        init() {
          if (!this.loaded) this.refresh(true);
          listen(root, EVENT, (e) => this.onChanged(e));
          const ch = openChannel();
          if (ch) {
            own = (e) => this.onMessage(e && e.data);
            listen(ch, 'message', own);
          }
          if (typeof document !== 'undefined') listen(document, 'visibilitychange', () => { if (!document.hidden) this.refresh(); });
          listen(root, 'pageshow', (e) => { if (e && e.persisted) this.refresh(); });
        },
        destroy() {
          handlers.forEach(([target, name, fn]) => { try { target.removeEventListener(name, fn); } catch (_) { /* gone */ } });
          handlers.length = 0;
          clearTimeout(trailing);
        },

        // ---- what the template draws
        get live() {
          try { const store = Alpine.store('stages'); return (store && store.live) || {}; } catch (_) { return {}; }
        },
        get shown() { const live = this.live; return this.steps.map((s) => merge(s, live[s.key])); },
        get here() { return this.shown.find((s) => s.current) || null; },
        // «(مكتملة)»: the server's words for the state when it sends them, else the bar's own
        stateWord(s) { return s && s.stateLabel ? `(${s.stateLabel})` : STATE_WORDS[s && s.state] || STATE_WORDS.todo; },
        href(s) { return s && s.state !== 'blocked' && s.url ? s.url : null; },
        // the hint explains a state («يُفتح الكتاب بعد تجميع المخطوطة»); else the step's numbers
        title(s) { return (s && (s.hint || s.detail)) || ''; },
        count(s) { return shortCount(s); },
        // «المراجعة 6/8»: the one button of the narrowest bar
        get compactLabel() { const s = this.here || this.shown[0]; const n = this.count(s); return n ? `${s.label} ${n}` : s.label; },
        stepClass(s) {
          return { [`is-${s.state}`]: true, 'is-current': Boolean(s.current), 'has-count': Boolean(this.count(s)), 'has-detail': Boolean(s.detail), 'has-bar': s.percent !== undefined };
        },
        barStyle(s) { return s && s.percent !== undefined ? `width:${s.percent}%` : ''; },

        // ---- live
        onChanged(e) {
          const book = e && e.detail ? Number(e.detail.book) : 0;
          if (book && this.book && book !== this.book) return;
          this.refresh(true);
          // the book's other tabs (their bars and pages) hear it too
          const ch = openChannel();
          try { if (ch) ch.postMessage({ type: 'stages', book: this.book }); } catch (_) { /* closed */ }
        },
        onMessage(m) {
          if (!m || !m.type) return;
          if (Number(m.book) !== this.book) return;
          this.refresh();
        },
        // `now` skips the wait (a local event: the screen knows the book moved on)
        refresh(now) {
          if (!this.url) return Promise.resolve(false);
          const wait = lastAt + REFRESH_MS - Date.now();
          if (!now && wait > 0) {
            if (!trailing) trailing = setTimeout(() => { trailing = null; this.refresh(true); }, wait);
            return Promise.resolve(false);
          }
          lastAt = Date.now();
          return this.fetchSteps();
        },
        async fetchSteps() {
          const sep = this.url.includes('?') ? '&' : '?';
          const target = this.current ? `${this.url}${sep}current=${encodeURIComponent(this.current)}` : this.url;
          let data = null;
          try {
            const res = await fetch(target, { headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store' });
            if (!res.ok) return false;
            data = await res.json();
          } catch (_) { return false; }
          const list = Array.isArray(data) ? data : (data && (data.steps || data.stages)) || [];
          if (!list.length) return false;
          this.steps = normalize(list, this.current);
          this.loaded = true;
          return true;
        },

        // ---- the narrow bar's menu
        toggleMenu() { this.menuOpen = !this.menuOpen; },
        closeMenu(focusButton) {
          if (!this.menuOpen) return;
          this.menuOpen = false;
          const btn = focusButton && this.$refs ? this.$refs.stgButton : null;
          if (btn && btn.focus) btn.focus();
        },
      };
    });
  };

  if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') document.addEventListener('alpine:init', register);
  root.NassakhStages.register = register;
})();
