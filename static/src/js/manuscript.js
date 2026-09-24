// Manuscript view (Phase 4, docs/PHASE4_SPEC.md §4.2): where the owner verifies the assembly (D22).
//   manuscriptView(config) – the view: polling while a run is on (the steps ticking, the reveal), the document
//                            swapped in place after re-runs (scroll anchored, changed blocks flashing), seams
//                            with their join / split override, block tools and menu, the source drawer, the
//                            side panel (contents with scroll spy, warnings), jump, copy and the keyboard map
//   manuscriptBar          – the top-bar controls (base.html header_actions, outside the root) reading
//                            Alpine.store('manuscript').view
// Config (assembly.views.manuscript): { bookId, title, state, urls, canEdit, canReview, pageCount, countsText }.
// The document is server-rendered (assembly/render.py) and swapped as one fragment: nothing inside the host
// carries an Alpine binding (x-ignore); hover, focus, clicks and keys are delegated on the host, and the
// floating tools (block «⋯», block menu, seam card, note popover) are single elements moved to their anchor.
// Motion (DESIGN.md §8, D24): the reveal (blocks rising in, join markers stitching), the flash after a
// re-run and the skeleton shimmer only; prefers-reduced-motion drops them all (manuscript.css).
(function () {
  'use strict';

  const hasDOM = typeof window !== 'undefined' && typeof document !== 'undefined'
    && typeof document.createElement === 'function' && typeof document.querySelector === 'function';
  const csrfToken = () => {
    const meta = hasDOM ? document.querySelector('meta[name="csrf-token"]') : null;
    return (meta && meta.content) || '';
  };
  const reducedMotion = () => {
    try { return Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); } catch (_) { return false; }
  };
  const storage = {
    get(key, fallback) { try { const v = window.localStorage.getItem(key); return v == null ? fallback : v; } catch (_) { return fallback; } },
    set(key, value) { try { window.localStorage.setItem(key, value); } catch (_) { /* private mode */ } },
  };
  const SEAMS_KEY = 'nassakh.manuscript.seams'; // '1' (default) | '0'
  const TAB_KEY = 'nassakh.manuscript.tab';
  const POLL_MS = 700; // §4.2: the steps tick every 700 ms
  const POLL_MAX_MS = 5000;
  const FAILURES_BEFORE_NOTICE = 3;
  const FLASH_MS = 600; // changed blocks after a re-run (accent-soft)
  const REVEAL_BLOCKS = 40; // the first blocks rise in with a 16 ms stagger (≤ 1 s in all)
  const REVEAL_MS = 1400; // the reveal classes are dropped after the stagger and the stitches (400 ms) ended
  const PULSE_MS = 500;
  const CARD_CLOSE_MS = 180; // hover intent: the card survives the gap between the seam and the card
  const NOTE_CLOSE_MS = 180;
  const TICK_MS = 30000; // «قبل 5 دقائق» refreshes
  const POP_EDGE = 8;
  const CARD_W = 300;
  const NOTE_W = 360;
  const TOOLBAR_H = 44;
  const TOPBAR_H = 52;
  const STAGES = [
    { key: 'collect', label: (n) => `جمع الأسطر من ${arCount(n, PAGES)}` },
    { key: 'paragraphs', label: () => 'بناء الفقرات' },
    { key: 'seams', label: () => 'وصل الفقرات عبر الصفحات' },
    { key: 'footnotes', label: () => 'ربط الحواشي' },
    { key: 'headings', label: () => 'بناء العناوين' },
    { key: 'typography', label: () => 'ضبط علامات الترقيم والأرقام' },
    { key: 'save', label: () => 'حفظ المخطوطة' },
  ];
  const STAGE_KEYS = STAGES.map((s) => s.key);
  const ROLES = [
    { value: 'body', label: 'محتوى' },
    { value: 'heading', label: 'عنوان رئيسي' },
    { value: 'subheading', label: 'عنوان فرعي' },
  ];
  const ROLE_OF_TAG = { H2: 'heading', H3: 'subheading', P: 'body' };
  const PAGES = ['صفحة واحدة', 'صفحتان', 'صفحات', 'صفحة'];
  const MINUTES = ['دقيقة', 'دقيقتين', 'دقائق', 'دقيقة'];
  const HOURS = ['ساعة', 'ساعتين', 'ساعات', 'ساعة'];
  const DAYS = ['يوم', 'يومين', 'أيام', 'يومًا'];
  const EASTERN_DIGITS = /[٠-٩۰-۹]/g;

  // ---------------------------------------------------------------- pure helpers (exported for the tests)
  // «صفحة واحدة», «صفحتان», «5 صفحات», «214 صفحة», «103 صفحات» (= assembly.render.ar_count).
  function arCount(n, forms) {
    n = Number(n) || 0;
    if (n === 1) return forms[0];
    if (n === 2) return forms[1];
    const units = n % 100;
    return `${n} ${units >= 3 && units <= 10 ? forms[2] : forms[3]}`;
  }
  // «قبل لحظات», «قبل 5 دقائق», «قبل ساعتين», «قبل 3 أيام», then the date (Western digits).
  function relativeTime(iso, now) {
    const t = Date.parse(iso || '');
    if (!Number.isFinite(t)) return '';
    const seconds = Math.max(0, Math.round(((now || Date.now()) - t) / 1000));
    if (seconds < 45) return 'قبل لحظات';
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `قبل ${arCount(Math.max(1, minutes), MINUTES)}`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `قبل ${arCount(hours, HOURS)}`;
    const days = Math.round(hours / 24);
    if (days < 30) return `قبل ${arCount(days, DAYS)}`;
    return `في ${String(iso).slice(0, 10)}`;
  }
  function parsePageNumber(raw) {
    const digits = String(raw || '')
      .replace(EASTERN_DIGITS, (d) => { const c = d.charCodeAt(0); return String(c >= 0x06f0 ? c - 0x06f0 : c - 0x0660); })
      .replace(/\D/g, '');
    return digits ? parseInt(digits, 10) : NaN;
  }
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const fill = (template, n) => String(template || '').replace('__n__', String(n));
  const hasClass = (el, cls) => Boolean(el && el.classList && el.classList.contains(cls));
  const attr = (el, name) => (el && typeof el.getAttribute === 'function' ? el.getAttribute(name) : null) || '';
  const q = (root, sel) => (root && typeof root.querySelector === 'function' ? root.querySelector(sel) : null);
  const qa = (root, sel) => (root && typeof root.querySelectorAll === 'function' ? Array.from(root.querySelectorAll(sel)) : []);
  const closest = (el, sel) => (el && typeof el.closest === 'function' ? el.closest(sel) : null);
  const rect = (el) => (el && typeof el.getBoundingClientRect === 'function' ? el.getBoundingClientRect() : { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 });
  const focusEl = (el, opts) => { if (el && typeof el.focus === 'function') el.focus(opts || { preventScroll: true }); };
  const toast = (message) => { if (window.Nassakh && window.Nassakh.toast) window.Nassakh.toast(message); };
  const pagesOf = (el) => attr(el, 'data-pages').split(',').map((v) => parseInt(v, 10)).filter((n) => n > 0);
  const linesOf = (el) => attr(el, 'data-lines').split(',').map((v) => parseInt(v, 10)).filter((n) => n > 0);
  const kids = (el) => Array.from(el && el.children ? el.children : []); // HTMLCollection has no forEach / find
  const nodes = (el) => Array.from(el && el.childNodes ? el.childNodes : []);

  // Keyboard map (§4.2), pure so the tests can exercise it. ctx: inField (typing in a field), inMenu (the
  // focus is inside a menu or the drawer, which keep their own arrows), hasBlock (a block is focused: ↓/↑
  // move between blocks only then, so the page still scrolls with the arrows before any block is chosen).
  function keyAction(ev, ctx) {
    const k = ev.key;
    const code = ev.code || '';
    if (k === 'Escape') return ctx.inField ? 'blur' : 'close';
    if (ctx.inField || ev.metaKey || ev.ctrlKey || ev.altKey) return null;
    if (k === '?') return 'sheet';
    if (code === 'KeyG' || k === 'g' || k === 'G') return 'jump';
    if (code === 'KeyS' || k === 's' || k === 'S') return 'seams';
    if (code === 'KeyO' || k === 'o' || k === 'O') return 'source';
    if (code === 'KeyC' || k === 'c' || k === 'C') return 'copy';
    if (k === ']') return 'nextWarning';
    if (k === '[') return 'prevWarning';
    if (ctx.inMenu) return null;
    if (code === 'KeyJ' || k === 'j' || k === 'J') return 'next';
    if (code === 'KeyK' || k === 'k' || k === 'K') return 'prev';
    if (k === 'ArrowDown') return ctx.hasBlock ? 'next' : null;
    if (k === 'ArrowUp') return ctx.hasBlock ? 'prev' : null;
    return null;
  }

  // The clipboard text of a block: references as [n], seams and suggestion chips dropped, words kept.
  function textOfBlock(el) {
    let out = '';
    nodes(el).forEach((node) => {
      if (node.nodeValue !== undefined && node.nodeValue !== null && !node.tagName) { out += node.nodeValue; return; }
      if (hasClass(node, 'ms-ref')) { out += `[${attr(node, 'data-number')}]`; return; }
      if (hasClass(node, 'ms-seam') || hasClass(node, 'ms-suggest')) { out += hasClass(node, 'ms-seam') ? ' ' : ''; return; }
      out += textOfBlock(node);
    });
    return out;
  }
  const squeeze = (text) => String(text || '').replace(/\s+/g, ' ').trim();
  // The manuscript as text (§4.2 copy): title, headings and paragraphs separated by blank lines, references
  // as [n], each chapter's notes after it.
  function manuscriptText(article) {
    const out = [];
    const walkNotes = (list) => {
      kids(list).forEach((li) => {
        const body = kids(li).find((c) => hasClass(c, 'ms-note-body'));
        out.push(`[${attr(li, 'data-number')}] ${squeeze(body ? textOfBlock(body) : '')}`);
      });
    };
    kids(article).forEach((child) => {
      if (hasClass(child, 'ms-title')) {
        kids(child).forEach((part) => { const t = squeeze(part.textContent); if (t) out.push(t); });
      } else if (hasClass(child, 'ms-chapter')) {
        kids(child).forEach((el) => {
          if (attr(el, 'data-block')) { const t = squeeze(textOfBlock(el)); if (t) out.push(t); } else if (hasClass(el, 'ms-notes')) walkNotes(el);
        });
      } else if (hasClass(child, 'ms-notes')) walkNotes(child);
    });
    return out.join('\n\n');
  }
  // The fragment HTML as DOM nodes (a <template> keeps the scripts inert); the tests replace it.
  function parseFragment(html) {
    const tpl = document.createElement('template');
    tpl.innerHTML = html;
    return tpl.content;
  }

  // fetch wrapper: never throws; `{ ok, status, data, message }` with an Arabic message on failure.
  async function api(url, options) {
    const opts = options || {};
    const method = opts.method || 'GET';
    const headers = { Accept: opts.accept || 'application/json' };
    const init = { method, credentials: 'same-origin', headers, cache: 'no-store' };
    if (method !== 'GET') {
      headers['Content-Type'] = 'application/json';
      headers['X-CSRFToken'] = csrfToken();
      init.body = JSON.stringify(opts.body || {});
    }
    let response;
    try {
      response = await fetch(url, init);
    } catch (_) {
      return { ok: false, status: 0, data: null, message: 'انقطع الاتصال بالخادم. تحقّق من الشبكة ثم أعد المحاولة.' };
    }
    let data = null;
    try { data = opts.accept === 'text/html' ? await response.text() : await response.json(); } catch (_) { data = null; }
    let message = data && typeof data === 'object' && (data.message || data.detail);
    if (!message) {
      if (response.status === 401) message = 'انتهت الجلسة. سجّل الدخول من جديد.';
      else if (response.status === 403) message = 'لا تملك صلاحية هذا الإجراء.';
      else message = 'تعذّر تنفيذ الطلب. حاول مرة أخرى.';
    }
    return { ok: response.ok, status: response.status, data, message };
  }

  window.NassakhManuscript = Object.assign(window.NassakhManuscript || {}, {
    keyAction, arCount, relativeTime, manuscriptText, textOfBlock, parseFragment, parsePageNumber, STAGE_KEYS,
  });

  document.addEventListener('alpine:init', () => {
    if (typeof Alpine.store === 'function') Alpine.store('manuscript', { view: null });

    Alpine.data('manuscriptBar', () => ({
      get v() { return this.$store.manuscript.view; },
    }));

    Alpine.data('manuscriptView', (cfg = {}) => {
      // Non-reactive plumbing lives in the closure: the host, the block index, timers, caches.
      const urls = cfg.urls || {};
      let host = null; // [data-ms-host]
      let column = null; // .ms-column, the positioned parent of the floating tools
      let article = null;
      let blocks = []; // block elements in document order
      const blockIndex = new Map(); // block id → index in `blocks`
      const pageFirst = new Map(); // page number → id of the first block from that page
      let headings = [];
      const visibleHeadings = new Set();
      let spy = null;
      let pollTimer = null;
      let pollGen = 0;
      let reloadGen = 0;
      let failures = 0;
      let cardTimer = null;
      let noteTimer = null;
      let toolsTimer = null;
      let flashTimer = null;
      let revealTimer = null;
      let pulseTimer = null;
      let tickTimer = null;
      let bound = false;
      let returnFocus = null; // where the focus goes back to when the drawer or the menu closes
      let pendingAnchor = null; // block ids to anchor the scroll on after the swap that follows a post
      let waitingRun = null; // the run id a post started
      let lastFlash = [];
      const sheetCache = new Map(); // page number → api:book_sheets item
      let warnCursor = -1;
      const listeners = [];
      const listen = (el, ev, fn, opts) => { if (el && typeof el.addEventListener === 'function') { el.addEventListener(ev, fn, opts); listeners.push([el, ev, fn]); } };

      return {
        bookId: cfg.bookId || 0,
        title: cfg.title || '',
        canEdit: Boolean(cfg.canEdit),
        canReview: Boolean(cfg.canReview),
        pageCount: Number(cfg.pageCount) || 0,
        state: cfg.state || {},
        phase: '', // 'empty' | 'assembling' | 'ready' | 'error'
        hasDocument: false,
        loadedVersion: 0,
        seams: storage.get(SEAMS_KEY, '1') !== '0',
        tab: storage.get(TAB_KEY, 'toc') === 'notes' ? 'notes' : 'toc',
        sideOpen: false,
        countsText: cfg.countsText || '',
        warnings: [], // the run's warnings in document order (from the fragment's meta)
        warningsTotal: 0,
        toc: [],
        focused: null, // focused block id
        tools: { blockId: null, style: '' },
        menu: { open: false, blockId: null, style: '', src: '', reviewed: true, role: 'body', reviewUrl: '', lines: [] },
        card: { open: false, page: 0, from: 0, mode: '', decision: '', text: '', action: '', actionLabel: '', style: '', seamId: '' },
        notePop: { open: false, note: '', number: '', html: '', orphan: false, style: '' },
        drawer: { open: false, blockId: null, pages: [], index: 0, lines: [], sheet: null, loading: false, error: '' },
        convert: { open: false, busy: false, error: '', label: 'تحويل', options: { footnote_numbering: 'page', include_unreviewed: true, strip_tatweel: true }, unreviewed: 0 },
        busy: false, // an override is on the wire or its run is on: no second post meanwhile
        copying: false,
        reveal: false,
        sheetOpen: false,
        pollState: 'ok', // ok | error | auth
        liveMessage: '',
        now: Date.now(),
        roles: ROLES,
        currentHeading: '',

        init() {
          this.resetConvert();
          this.bindDom();
          this.applyState(this.state, { initial: true });
          if (typeof Alpine.store === 'function' && Alpine.store('manuscript')) Alpine.store('manuscript').view = this;
          if (this.active) this.schedulePoll(POLL_MS);
          if (typeof setInterval === 'function') tickTimer = setInterval(() => { this.now = Date.now(); }, TICK_MS);
        },
        destroy() {
          this.stopPolling();
          [cardTimer, noteTimer, toolsTimer, flashTimer, revealTimer, pulseTimer].forEach((t) => clearTimeout(t));
          if (tickTimer && typeof clearInterval === 'function') clearInterval(tickTimer);
          if (spy) spy.disconnect();
          listeners.forEach(([el, ev, fn]) => el.removeEventListener(ev, fn));
          if (typeof Alpine.store === 'function' && Alpine.store('manuscript')) Alpine.store('manuscript').view = null;
        },

        // ------------------------------------------------------------ derived state
        get run() { return this.state.run || null; },
        get active() { return Boolean(this.state.active); },
        get assembling() { return this.phase === 'assembling'; },
        get failed() { return Boolean(this.run && this.run.status === 'error') && !this.active; },
        get stale() { return Boolean(this.state.stale) && this.hasDocument && !this.active; },
        get stalePages() { return Array.isArray(this.state.stale_pages) ? this.state.stale_pages : []; },
        get staleText() {
          const n = this.stalePages.length;
          return n ? `تغيّر نص ${arCount(n, PAGES)} بعد التجميع:` : 'تغيّر النص بعد التجميع.';
        },
        get errorHeadline() { return (this.run && this.run.error) || 'تعذّر تجميع المخطوطة.'; },
        get stageIndex() { return this.run && this.run.status === 'running' ? STAGE_KEYS.indexOf(this.run.stage) : this.run && this.run.status === 'done' ? STAGE_KEYS.length : -1; },
        // the side panel's steps while a run is on (queued: none current yet)
        get steps() {
          const idx = this.stageIndex;
          return STAGES.map((s, i) => ({ key: s.key, label: s.label(this.pageCount), done: idx > i, current: idx === i }));
        },
        get stepsDone() { return clamp(this.stageIndex, 0, STAGES.length); },
        // the status pill (= rv-save): assembling, failed, stale, assembled … ago
        get pill() {
          if (this.active) return { state: 'saving', text: 'قيد التجميع…' };
          if (this.failed) return { state: 'error', text: 'فشل التجميع' };
          if (this.state.stale && this.hasDocument) return { state: 'warn', text: 'تغيّر النص بعد التجميع' };
          if (this.state.exists && this.state.assembled_at) return { state: 'saved', text: `مُجمَّعة ${relativeTime(this.state.assembled_at, this.now)}` };
          return { state: 'idle', text: '' };
        },
        // exactly one primary: re-assemble when stale or failed (editors), copy when a document is there,
        // convert before the first run (editors), nothing while a run is on
        get primary() {
          if (this.active) return '';
          if (this.canEdit && (this.state.stale || this.failed)) return 'reassemble';
          if (this.hasDocument) return 'copy';
          if (this.canEdit) return 'convert';
          return '';
        },
        reviewUrl(n) { return n ? fill(urls.review, n) : ''; },

        // ------------------------------------------------------------ the state poll (700 ms while a run is on)
        applyState(s, opts = {}) {
          if (!s || typeof s !== 'object') return;
          this.state = s;
          this.convert.unreviewed = Number(s.unreviewed_pages) || 0;
          if (s.options && !this.convert.open) this.convert.options = Object.assign({}, this.convert.options, s.options);
          const run = s.run;
          if (s.active) { // queued or running: the steps tick (over the old document when there is one)
            this.phase = 'assembling';
            return;
          }
          if (run && run.status === 'error') {
            this.phase = this.hasDocument ? 'ready' : 'error';
            if (waitingRun !== null) { waitingRun = null; pendingAnchor = null; this.busy = false; toast(run.error || 'فشل التجميع'); }
            if (!opts.initial) this.liveMessage = run.error || 'فشل التجميع';
            return;
          }
          if (s.exists) {
            if (!opts.initial && Number(s.version) !== this.loadedVersion) { this.reload({ reveal: !this.hasDocument, anchor: pendingAnchor }); return; }
            this.phase = this.hasDocument ? 'ready' : (opts.initial ? 'ready' : 'assembling');
            if (waitingRun !== null) { waitingRun = null; pendingAnchor = null; this.busy = false; }
            return;
          }
          this.phase = 'empty';
        },
        schedulePoll(ms) {
          clearTimeout(pollTimer);
          pollTimer = setTimeout(() => this.poll(), ms);
        },
        stopPolling() {
          clearTimeout(pollTimer);
          pollTimer = null;
          pollGen += 1;
        },
        pollNow() {
          clearTimeout(pollTimer);
          this.pollState = 'ok';
          this.poll();
        },
        async poll() {
          const gen = pollGen;
          if (typeof document !== 'undefined' && document.hidden) { this.schedulePoll(POLL_MS * 2); return; }
          const r = await api(urls.state);
          if (gen !== pollGen) return; // stopped meanwhile
          if (r.status === 401 || r.status === 403) { this.pollState = 'auth'; this.stopPolling(); return; }
          if (!r.ok || !r.data) {
            failures += 1;
            if (failures >= FAILURES_BEFORE_NOTICE) this.pollState = 'error';
            this.schedulePoll(Math.min(POLL_MS * (1 + failures), POLL_MAX_MS));
            return;
          }
          failures = 0;
          this.pollState = 'ok';
          this.applyState(r.data);
          if (this.active) this.schedulePoll(POLL_MS);
          else this.stopPolling();
        },

        // ------------------------------------------------------------ starting runs
        resetConvert() {
          const s = this.state || {};
          this.convert.options = Object.assign({ footnote_numbering: 'page', include_unreviewed: true, strip_tatweel: true }, s.options || {});
          this.convert.unreviewed = Number(s.unreviewed_pages) || 0;
          this.convert.label = s.exists ? 'إعادة التجميع' : 'تحويل';
          this.convert.error = '';
        },
        openConvert() {
          this.resetConvert();
          this.convert.open = true;
          if (this.$nextTick) this.$nextTick(() => focusEl(q(document, '.ms-convert [role="radio"][aria-checked="true"]') || q(document, '.ms-convert-actions button')));
        },
        closeConvert() {
          this.convert.open = false;
        },
        async submitConvert() {
          const ok = await this.startAssembly(Object.assign({}, this.convert.options));
          if (ok) this.convert.open = false;
        },
        reassemble() {
          return this.startAssembly(Object.assign({}, this.convert.options));
        },
        // POST assemble: the run is shown at once (the steps, or the pill over the old document), then polled.
        async startAssembly(options) {
          if (this.convert.busy) return false;
          this.convert.busy = true;
          this.convert.error = '';
          const r = await api(urls.assemble, { method: 'POST', body: options || {} });
          this.convert.busy = false;
          if (!r.ok) { this.convert.error = r.message; toast(r.message); return false; }
          this.beginRun(r.data, null);
          return true;
        },
        // A 202 run answer: the state turns active locally until the poll says otherwise.
        beginRun(data, anchor) {
          const run = { id: data.run_id, status: data.status, stage: data.stage || '', error: '' };
          waitingRun = run.id;
          pendingAnchor = anchor;
          this.state = Object.assign({}, this.state, { run, active: true });
          this.phase = 'assembling';
          this.liveMessage = 'بدأ التجميع';
          this.pollNow();
        },
        // A seam / role / suggestion post: 202, then the document re-runs and swaps in place.
        async postRun(url, body, anchor) {
          if (this.busy) return false;
          this.busy = true;
          const r = await api(url, { method: 'POST', body });
          if (!r.ok) { this.busy = false; toast(r.message); return false; }
          this.beginRun(r.data, anchor);
          return true;
        },

        // ------------------------------------------------------------ the fragment: load, swap, index
        // Only the latest fetch lands: two polls that both saw a new version must not swap an older fragment
        // over a newer one.
        async reload(opts = {}) {
          reloadGen += 1;
          const gen = reloadGen;
          const r = await api(urls.document, { accept: 'text/html' });
          if (gen !== reloadGen) return false; // a later reload is on the wire
          if (!r.ok || typeof r.data !== 'string') {
            if (!this.hasDocument) { this.phase = 'error'; this.state = Object.assign({}, this.state, { run: Object.assign({}, this.run || {}, { status: 'error', error: 'تعذّر تحميل المخطوطة.' }) }); }
            else this.phase = 'ready';
            toast(r.message);
            waitingRun = null;
            pendingAnchor = null;
            this.busy = false;
            return false;
          }
          this.swapFragment(r.data, opts);
          return true;
        },
        // Replace the document with a rendered fragment: the scroll stays on the same block, the blocks whose
        // markup changed flash once; the first document rises in (the reveal).
        swapFragment(html, opts = {}) {
          if (!host) this.bindDom();
          if (!host) return;
          const before = new Map();
          if (this.hasDocument) blocks.forEach((b) => before.set(attr(b, 'data-block'), b.innerHTML));
          const anchor = this.anchorBefore(opts.anchor);
          const frag = (window.NassakhManuscript.parseFragment || parseFragment)(html);
          host.textContent = '';
          Array.from(frag.childNodes || []).forEach((node) => host.appendChild(node));
          this.adoptParts(host);
          this.indexBlocks();
          this.hasDocument = Boolean(article);
          if (this.hasDocument && !this.active) this.phase = 'ready';
          this.anchorAfter(anchor);
          if (before.size) this.flashChanged(before);
          if (opts.reveal) this.playReveal();
          this.setupSpy();
          this.hideTools();
          this.closeMenu();
          this.hideCard();
          this.hideNote();
          waitingRun = null;
          pendingAnchor = null;
          this.busy = false;
          this.liveMessage = before.size ? 'حُدّثت المخطوطة' : 'اكتمل التجميع';
        },
        // The host, the column and the side hosts are found by selector: init runs before the x-refs exist.
        bindDom() {
          if (bound) return;
          const root = this.$el || (hasDOM ? document.querySelector('[data-manuscript]') : null);
          host = q(root, '[data-ms-host]');
          column = q(root, '.ms-column');
          if (!host) return;
          bound = true;
          this.adoptParts(host);
          this.indexBlocks();
          this.hasDocument = Boolean(article);
          this.setupSpy();
          listen(host, 'mouseover', (e) => this.onHostOver(e));
          listen(host, 'mouseout', (e) => this.onHostOut(e));
          listen(host, 'mouseleave', () => this.hideToolsSoon());
          listen(host, 'focusin', (e) => this.onHostFocusIn(e));
          listen(host, 'focusout', (e) => this.onHostFocusOut(e));
          listen(host, 'click', (e) => this.onHostClick(e));
          listen(host, 'keydown', (e) => this.onHostKey(e));
        },
        // The fragment carries the side panel's parts and the meta JSON: they move to their hosts.
        sideHost(name) {
          const root = this.$el || (hasDOM ? document.querySelector('[data-manuscript]') : null);
          return q(root, `[data-ms-${name}-host]`);
        },
        adoptParts(root) {
          const parts = { toc: this.sideHost('toc'), warnings: this.sideHost('warnings'), stats: this.sideHost('stats') };
          qa(root, '[data-ms-part]').forEach((part) => {
            const target = parts[attr(part, 'data-ms-part')];
            if (!target) return;
            target.textContent = '';
            target.appendChild(part);
          });
          const meta = q(root, 'script[id="ms-meta"]') || q(root, '#ms-meta');
          if (meta) {
            let data = null;
            try { data = JSON.parse(meta.textContent); } catch (_) { data = null; }
            if (data) this.applyMeta(data);
            if (typeof meta.remove === 'function') meta.remove();
          }
        },
        applyMeta(meta) {
          this.loadedVersion = Number(meta.version) || 0;
          this.countsText = meta.countsText || this.countsText;
          this.warnings = Array.isArray(meta.warnings) ? meta.warnings : [];
          this.warningsTotal = this.warnings.length;
          this.toc = Array.isArray(meta.toc) ? meta.toc : [];
          if (meta.assembledAt) this.state = Object.assign({}, this.state, { assembled_at: meta.assembledAt, exists: true, version: this.loadedVersion });
          warnCursor = -1;
        },
        indexBlocks() {
          article = q(host, 'article.ms-doc');
          blocks = article ? qa(article, '.ms-block') : []; // not the suggestion chips' buttons (they carry data-block too)
          blockIndex.clear();
          pageFirst.clear();
          blocks.forEach((b, i) => {
            const id = attr(b, 'data-block');
            blockIndex.set(id, i);
            pagesOf(b).forEach((n) => { if (!pageFirst.has(n)) pageFirst.set(n, id); });
          });
          headings = blocks.filter((b) => b.tagName === 'H2' || b.tagName === 'H3');
          if (this.focused && !blockIndex.has(this.focused)) this.focused = null;
        },
        blockById(id) {
          const i = blockIndex.get(id);
          return i === undefined ? null : blocks[i];
        },
        blockCount() { return blocks.length; },
        firstBlockOfPage(n) { return pageFirst.get(Number(n)) || null; },
        // The block to keep in place across a swap: the given candidates (the seam's block, then its
        // neighbours), else the first block under the toolbar.
        anchorBefore(candidates) {
          if (!blocks.length) return null;
          let ids = Array.isArray(candidates) ? candidates.filter((id) => blockIndex.has(id)) : [];
          if (!ids.length) {
            const top = TOPBAR_H + TOOLBAR_H;
            const first = blocks.find((b) => rect(b).bottom > top) || blocks[0];
            const i = blockIndex.get(attr(first, 'data-block'));
            ids = [attr(first, 'data-block'), i > 0 ? attr(blocks[i - 1], 'data-block') : null, i + 1 < blocks.length ? attr(blocks[i + 1], 'data-block') : null].filter(Boolean);
          }
          return { ids, top: rect(this.blockById(ids[0])).top };
        },
        anchorAfter(anchor) {
          if (!anchor) return;
          const id = anchor.ids.find((candidate) => blockIndex.has(candidate));
          if (!id || typeof window.scrollBy !== 'function') return;
          const delta = rect(this.blockById(id)).top - anchor.top;
          if (Math.abs(delta) > 0.5) window.scrollBy(0, delta);
        },
        flashChanged(before) {
          clearTimeout(flashTimer);
          lastFlash.forEach((b) => { if (b.classList) b.classList.remove('is-flash'); });
          lastFlash = blocks.filter((b) => before.get(attr(b, 'data-block')) !== b.innerHTML);
          lastFlash.forEach((b) => { if (b.classList) b.classList.add('is-flash'); });
          flashTimer = setTimeout(() => { lastFlash.forEach((b) => { if (b.classList) b.classList.remove('is-flash'); }); lastFlash = []; }, FLASH_MS);
        },
        flashingIds() { return lastFlash.map((b) => attr(b, 'data-block')); },
        // The reveal: the first blocks rise in with a 16 ms stagger, join markers stitch (manuscript.css).
        // The host is x-ignore (no Alpine binding reaches it), so its `is-reveal` class is set here.
        playReveal() {
          if (reducedMotion()) return;
          clearTimeout(revealTimer);
          this.reveal = true;
          if (host && host.classList) host.classList.add('is-reveal');
          blocks.slice(0, REVEAL_BLOCKS).forEach((b, i) => { if (b.classList) b.classList.add('is-rise'); if (b.style && b.style.setProperty) b.style.setProperty('--i', String(i)); });
          qa(article, '.ms-seam[data-mode="join"]').forEach((s) => { if (s.classList) s.classList.add('is-stitch'); });
          revealTimer = setTimeout(() => {
            this.reveal = false;
            if (host && host.classList) host.classList.remove('is-reveal');
            blocks.forEach((b) => { if (b.classList) b.classList.remove('is-rise'); });
            qa(article, '.ms-seam.is-stitch').forEach((s) => s.classList.remove('is-stitch'));
          }, REVEAL_MS);
        },

        // ------------------------------------------------------------ seams
        setSeams(on) {
          this.seams = Boolean(on);
          storage.set(SEAMS_KEY, this.seams ? '1' : '0');
          if (!this.seams) this.hideCard();
        },
        toggleSeams() { this.setSeams(!this.seams); },
        seamInfo(el) {
          return { page: parseInt(attr(el, 'data-page'), 10) || 0, from: parseInt(attr(el, 'data-from'), 10) || 0, mode: attr(el, 'data-mode'), decision: attr(el, 'data-decision') || 'auto' };
        },
        // The hover card's text and action (§4.2): join → «فصل هنا», split → «وصل بما قبلها».
        seamCard(el) {
          const s = this.seamInfo(el);
          const override = s.decision === 'override' ? ' · قرار يدوي' : '';
          if (s.mode === 'join') return { ...s, text: `وُصلت الفقرة بين الصفحتين ${s.from} و${s.page}${override}`, action: 'split', actionLabel: 'فصل هنا' };
          if (s.mode === 'split') return { ...s, text: `فاصل بين الصفحتين ${s.from} و${s.page}${override}`, action: 'join', actionLabel: 'وصل بما قبلها' };
          return { ...s, text: `صفحة ${s.page - 1} غير مُضمَّنة`, action: '', actionLabel: '' };
        },
        showCard(el) {
          clearTimeout(cardTimer);
          if (!this.seams) return;
          const info = this.seamCard(el);
          const r = rect(el);
          const c = rect(column);
          const left = clamp(r.left - c.left + r.width / 2 - CARD_W / 2, POP_EDGE, Math.max(POP_EDGE, c.width - CARD_W - POP_EDGE));
          this.card = { open: true, page: info.page, from: info.from, mode: info.mode, decision: info.decision, text: info.text, action: info.action, actionLabel: info.actionLabel, style: `top:${Math.round(r.bottom - c.top + 6)}px;left:${Math.round(left)}px`, seamId: `${info.mode}-${info.page}` };
        },
        hideCard(refocus) {
          clearTimeout(cardTimer);
          const wasOpen = this.card.open;
          this.card = Object.assign({}, this.card, { open: false });
          if (refocus && wasOpen) focusEl(this.seamEl(this.card.page));
        },
        keepCard() { clearTimeout(cardTimer); },
        closeCardSoon() { clearTimeout(cardTimer); cardTimer = setTimeout(() => this.hideCard(), CARD_CLOSE_MS); },
        seamEl(page) { return q(article, `.ms-seam[data-page="${page}"]`); },
        // The override of the open card's seam (or of a seam element): the anchor is the block that holds it.
        applySeam() { return this.postSeam(this.card.page, this.card.action); },
        resetSeam() { return this.postSeam(this.card.page, 'auto'); },
        applySeamFor(el) {
          const info = this.seamCard(el);
          if (!info.action) return Promise.resolve(false);
          return this.postSeam(info.page, info.action);
        },
        resetSeamFor(el) {
          const info = this.seamInfo(el);
          if (info.decision !== 'override') return Promise.resolve(false);
          return this.postSeam(info.page, 'auto');
        },
        postSeam(page, mode) {
          if (!this.canEdit || !page || !mode) return Promise.resolve(false);
          const el = this.seamEl(page);
          const block = closest(el, '.ms-block'); // null for a split marker (it sits between blocks)
          const ids = [];
          if (block) {
            const i = blockIndex.get(attr(block, 'data-block'));
            ids.push(attr(block, 'data-block'));
            if (i > 0) ids.push(attr(blocks[i - 1], 'data-block'));
            if (i + 1 < blocks.length) ids.push(attr(blocks[i + 1], 'data-block'));
          } else {
            const first = this.firstBlockOfPage(page);
            if (first) { const i = blockIndex.get(first); if (i > 0) ids.push(attr(blocks[i - 1], 'data-block')); ids.push(first); }
          }
          this.hideCard();
          this.liveMessage = mode === 'auto' ? `أُعيد القرار التلقائي لفاصل الصفحة ${page}` : mode === 'join' ? `تُوصل الفقرة عبر الصفحة ${page}` : `تُفصل الفقرة عند الصفحة ${page}`;
          return this.postRun(urls.seams, { page, mode }, ids);
        },

        // ------------------------------------------------------------ blocks: hover / focus tools, menu, roles
        onHostOver(e) {
          const t = e.target;
          const seam = closest(t, '.ms-seam');
          if (seam) { this.showCard(seam); return; }
          const ref = closest(t, '.ms-ref');
          if (ref) { this.showNote(ref); return; }
          const block = closest(t, '.ms-block');
          if (block) this.showTools(block);
        },
        onHostOut(e) {
          const t = e.target;
          const to = e.relatedTarget;
          if (closest(t, '.ms-seam') && !closest(to, '.ms-seam')) this.closeCardSoon();
          if (closest(t, '.ms-ref') && !closest(to, '.ms-ref')) this.closeNoteSoon();
        },
        onHostFocusIn(e) {
          const t = e.target;
          const seam = closest(t, '.ms-seam');
          if (seam) { this.showCard(seam); return; }
          const ref = closest(t, '.ms-ref');
          if (ref) { this.showNote(ref); return; }
          const block = closest(t, '.ms-block');
          if (block) { this.focused = attr(block, 'data-block'); this.showTools(block); }
        },
        onHostFocusOut(e) {
          const t = e.target;
          const to = e.relatedTarget;
          if (closest(t, '.ms-seam') && !closest(to, '.ms-card')) this.closeCardSoon();
          if (closest(t, '.ms-ref')) this.closeNoteSoon();
        },
        onHostClick(e) {
          const t = e.target;
          const ref = closest(t, '.ms-ref');
          if (ref) { e.preventDefault(); this.goToNote(attr(ref, 'data-note')); return; }
          const back = closest(t, '.ms-note-num');
          if (back) { e.preventDefault(); this.backToRef(attr(back, 'data-ref')); return; }
          const suggest = closest(t, '.ms-suggest-btn');
          if (suggest) {
            e.preventDefault();
            const id = attr(suggest, 'data-block');
            if (attr(suggest, 'data-suggest') === 'accept') this.acceptSuggestion(id); else this.dismissSuggestion(id);
            return;
          }
          const seam = closest(t, '.ms-seam');
          if (seam) { e.preventDefault(); this.applySeamFor(seam); }
        },
        onHostKey(e) {
          const t = e.target;
          const seam = closest(t, '.ms-seam');
          if (seam) {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); this.applySeamFor(seam); }
            else if (e.key === 'Backspace' || e.key === 'Delete') { e.preventDefault(); this.resetSeamFor(seam); }
            return;
          }
          if (closest(t, 'button, a')) return; // refs, chips and back links keep their native keys
          const block = closest(t, '.ms-block');
          if (block && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); this.openMenu(attr(block, 'data-block')); }
        },
        showTools(block) {
          clearTimeout(toolsTimer);
          const id = attr(block, 'data-block');
          if (this.menu.open && this.menu.blockId !== id) return; // the open menu keeps its block
          const previous = this.tools.blockId ? this.blockById(this.tools.blockId) : null;
          if (previous && previous !== block && previous.classList) previous.classList.remove('is-tools');
          if (block.classList) block.classList.add('is-tools');
          const top = Math.round(rect(block).top - rect(column).top);
          this.tools = { blockId: id, style: `top:${top}px` };
        },
        hideTools() {
          clearTimeout(toolsTimer);
          if (this.menu.open) return;
          const previous = this.tools.blockId ? this.blockById(this.tools.blockId) : null;
          if (previous && previous.classList) previous.classList.remove('is-tools');
          this.tools = { blockId: null, style: '' };
        },
        hideToolsSoon() {
          clearTimeout(toolsTimer);
          toolsTimer = setTimeout(() => { if (!this.focused || this.focused !== this.tools.blockId) this.hideTools(); }, CARD_CLOSE_MS);
        },
        keepTools() { clearTimeout(toolsTimer); },
        focusBlock(id, opts = {}) {
          const el = this.blockById(id);
          if (!el) return false;
          this.focused = id;
          focusEl(el, { preventScroll: true });
          if (typeof el.scrollIntoView === 'function' && !opts.noScroll) el.scrollIntoView({ block: opts.block || 'nearest', behavior: reducedMotion() || opts.instant ? 'auto' : 'smooth' });
          this.showTools(el);
          return true;
        },
        moveFocus(dir) {
          if (!blocks.length) return false;
          let i = this.focused && blockIndex.has(this.focused) ? blockIndex.get(this.focused) + dir : (dir > 0 ? 0 : blocks.length - 1);
          i = clamp(i, 0, blocks.length - 1);
          return this.focusBlock(attr(blocks[i], 'data-block'), { instant: true });
        },
        goToBlock(id, opts = {}) {
          const el = this.blockById(id);
          if (!el) { toast('لم تُعثر على الفقرة في المخطوطة'); return false; }
          if (opts.focus !== false) this.focusBlock(id, { block: 'center' });
          else if (typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'center', behavior: reducedMotion() ? 'auto' : 'smooth' });
          if (el.classList) {
            el.classList.add('is-pulse');
            clearTimeout(pulseTimer);
            pulseTimer = setTimeout(() => el.classList.remove('is-pulse'), PULSE_MS);
          }
          return true;
        },
        openMenu(id) {
          const block = this.blockById(id);
          if (!block) return;
          this.showTools(block);
          returnFocus = block;
          const pages = pagesOf(block);
          const btn = this.$refs && this.$refs.toolsBtn;
          const top = Math.round((btn ? rect(btn).bottom : rect(block).top + 44) - rect(column).top + 4);
          this.menu = {
            open: true,
            blockId: id,
            style: `top:${top}px`,
            src: attr(block, 'data-src'),
            reviewed: attr(block, 'data-reviewed') !== 'false',
            role: ROLE_OF_TAG[block.tagName] || 'body',
            reviewUrl: this.reviewUrl(pages[0]),
            lines: linesOf(block),
          };
          if (this.$nextTick) this.$nextTick(() => focusEl(q(this.$refs && this.$refs.menu, '.menu-item')));
        },
        toggleMenu(id) {
          if (this.menu.open && (!id || this.menu.blockId === id)) this.closeMenu(true); else this.openMenu(id || this.tools.blockId);
        },
        closeMenu(refocus) {
          const wasOpen = this.menu.open;
          this.menu = Object.assign({}, this.menu, { open: false });
          if (wasOpen && refocus) focusEl(returnFocus);
        },
        // The block's lines take the role (through the review service, D38), then the document re-runs.
        setRole(id, role) {
          const block = this.blockById(id);
          if (!block || !this.canReview) return Promise.resolve(false);
          const current = ROLE_OF_TAG[block.tagName] || 'body';
          this.closeMenu(true);
          if (role === current) return Promise.resolve(false);
          const i = blockIndex.get(id);
          const ids = [id, i > 0 ? attr(blocks[i - 1], 'data-block') : null, i + 1 < blocks.length ? attr(blocks[i + 1], 'data-block') : null].filter(Boolean);
          this.liveMessage = role === 'body' ? 'تصير الفقرة محتوى' : role === 'heading' ? 'تصير الفقرة عنوانًا رئيسيًا' : 'تصير الفقرة عنوانًا فرعيًا';
          return this.postRun(urls.roles, { line_ids: linesOf(block), role }, ids);
        },
        acceptSuggestion(id) { return this.setRole(id, 'heading'); },
        dismissSuggestion(id) {
          if (!this.canEdit) return Promise.resolve(false);
          const i = blockIndex.get(id);
          const ids = i === undefined ? [id] : [id, i > 0 ? attr(blocks[i - 1], 'data-block') : null].filter(Boolean);
          this.liveMessage = 'أُهمل الاقتراح';
          return this.postRun(urls.suggestions, { block_id: id, action: 'dismiss' }, ids);
        },

        // ------------------------------------------------------------ footnotes: popover, jumps
        noteEl(noteId) { return q(article, `.ms-note[data-note="${noteId}"]`); },
        refEl(noteId) { return q(article, `.ms-ref[data-note="${noteId}"]`); },
        showNote(ref) {
          clearTimeout(noteTimer);
          const noteId = attr(ref, 'data-note');
          const li = this.noteEl(noteId);
          const body = li ? kids(li).find((c) => hasClass(c, 'ms-note-body')) : null;
          qa(article, '.ms-note.is-hot').forEach((n) => n.classList.remove('is-hot'));
          if (li && li.classList) li.classList.add('is-hot');
          const r = rect(ref);
          const c = rect(column);
          const width = Math.min(NOTE_W, Math.max(120, c.width - 2 * POP_EDGE));
          const left = clamp(r.left - c.left + r.width / 2 - width / 2, POP_EDGE, Math.max(POP_EDGE, c.width - width - POP_EDGE));
          const viewportH = (typeof window !== 'undefined' && window.innerHeight) || 800;
          const above = r.bottom + 160 > viewportH;
          const top = above ? Math.round(r.top - c.top) : Math.round(r.bottom - c.top + 6);
          this.notePop = {
            open: true,
            note: noteId,
            number: attr(ref, 'data-number'),
            html: body ? body.innerHTML : '',
            orphan: attr(ref, 'data-orphan') === 'true',
            style: `top:${top}px;left:${Math.round(left)}px;width:${Math.round(width)}px${above ? ';transform:translateY(-100%) translateY(-6px)' : ''}`,
          };
        },
        hideNote() {
          clearTimeout(noteTimer);
          if (this.notePop.open) qa(article, '.ms-note.is-hot').forEach((n) => n.classList.remove('is-hot'));
          this.notePop = Object.assign({}, this.notePop, { open: false });
        },
        keepNote() { clearTimeout(noteTimer); },
        closeNoteSoon() { clearTimeout(noteTimer); noteTimer = setTimeout(() => this.hideNote(), NOTE_CLOSE_MS); },
        goToNote(noteId) {
          const li = this.noteEl(noteId);
          if (!li) return false;
          this.hideNote();
          if (typeof li.scrollIntoView === 'function') li.scrollIntoView({ block: 'center', behavior: reducedMotion() ? 'auto' : 'smooth' });
          focusEl(q(li, '.ms-note-num'));
          if (li.classList) { li.classList.add('is-pulse'); clearTimeout(pulseTimer); pulseTimer = setTimeout(() => li.classList.remove('is-pulse'), PULSE_MS); }
          return true;
        },
        backToRef(noteId) {
          const ref = this.refEl(noteId);
          if (!ref) return false;
          if (typeof ref.scrollIntoView === 'function') ref.scrollIntoView({ block: 'center', behavior: reducedMotion() ? 'auto' : 'smooth' });
          focusEl(ref);
          return true;
        },

        // ------------------------------------------------------------ the source drawer («عرض الأصل»)
        get drawerPage() { return this.drawer.pages[this.drawer.index] || 0; },
        get drawerAspect() {
          const s = this.drawer.sheet;
          return s && s.width > 0 && s.height > 0 ? (s.width / s.height).toFixed(4) : '0.7';
        },
        async openSource(id) {
          const block = this.blockById(id || this.focused);
          if (!block) { toast('حدّد فقرة أولًا (J / K)'); return false; }
          this.closeMenu();
          returnFocus = block;
          const pages = pagesOf(block);
          this.drawer = { open: true, blockId: attr(block, 'data-block'), pages, index: 0, lines: linesOf(block), sheet: null, loading: true, error: '' };
          this.liveMessage = `الأصل: صفحة ${pages[0] || ''}`;
          if (this.$nextTick) this.$nextTick(() => focusEl(this.$refs && this.$refs.drawerClose));
          await this.loadSheet(pages[0]);
          return true;
        },
        closeDrawer() {
          if (!this.drawer.open) return;
          this.drawer = Object.assign({}, this.drawer, { open: false });
          const target = returnFocus;
          if (target) focusEl(target, { preventScroll: true });
        },
        drawerStep(dir) {
          const i = this.drawer.index + dir;
          if (i < 0 || i >= this.drawer.pages.length) return false;
          this.drawer.index = i;
          return this.loadSheet(this.drawer.pages[i]);
        },
        async loadSheet(n, force) {
          if (!n) { this.drawer.loading = false; this.drawer.error = 'لا صفحة لهذه الفقرة.'; return false; }
          if (!force && sheetCache.has(n)) { this.drawer.sheet = sheetCache.get(n); this.drawer.loading = false; this.drawer.error = ''; return true; }
          this.drawer.loading = true;
          this.drawer.error = '';
          const sep = String(urls.sheets || '').includes('?') ? '&' : '?';
          const r = await api(`${urls.sheets}${sep}from=${n}&to=${n}`);
          if (this.drawerPage !== n || !this.drawer.open) return false; // moved on meanwhile
          const item = r.ok && r.data ? (Array.isArray(r.data) ? r.data : r.data.pages || []).find((p) => p && p.number === n) : null;
          this.drawer.loading = false;
          if (!item) { this.drawer.sheet = null; this.drawer.error = r.ok ? 'لا بيانات لهذه الصفحة.' : `تعذّر تحميل صورة الصفحة. ${r.message}`; return false; }
          sheetCache.set(n, item);
          this.drawer.sheet = item;
          return true;
        },
        // The block's lines on the page (review band style), from the sheet's line boxes (ratios).
        drawerBoxes() {
          const s = this.drawer.sheet;
          if (!s || !Array.isArray(s.lines)) return [];
          const own = new Set(this.drawer.lines);
          return s.lines.filter((l) => Array.isArray(l.bbox) && l.bbox.length === 4 && own.has(l.id)).map((l) => ({ id: l.id, bbox: l.bbox, own: true }));
        },
        // Physical left/top: image pixels do not flip with the writing direction.
        boxStyle(b) {
          const pct = (v) => `${(100 * clamp(Number(v) || 0, 0, 1)).toFixed(2)}%`;
          return `left:${pct(b[0])};top:${pct(b[1])};width:${pct(b[2] - b[0])};height:${pct(b[3] - b[1])}`;
        },
        trapTab(e, root) {
          const items = qa(root, 'a[href], button:not([disabled]), input, [tabindex="0"]').filter((el) => !el.hidden);
          if (!items.length) return;
          const first = items[0];
          const last = items[items.length - 1];
          const active = typeof document !== 'undefined' ? document.activeElement : null;
          if (e.shiftKey && active === first) { e.preventDefault(); focusEl(last); }
          else if (!e.shiftKey && active === last) { e.preventDefault(); focusEl(first); }
        },

        // ------------------------------------------------------------ side panel: contents, warnings, scroll spy
        setTab(name) {
          this.tab = name === 'notes' ? 'notes' : 'toc';
          storage.set(TAB_KEY, this.tab);
        },
        onSideClick(e) {
          const t = e.target;
          const goto = closest(t, '[data-goto]');
          if (!goto) return;
          e.preventDefault();
          const index = attr(goto, 'data-warn-index');
          if (index !== '') { this.goToWarning(parseInt(index, 10)); return; }
          this.goToBlock(attr(goto, 'data-goto'));
        },
        goToWarning(i) {
          const w = this.warnings[i];
          if (!w) return false;
          warnCursor = i;
          this.setTab('notes');
          const hostEl = this.sideHost('warnings');
          qa(hostEl, '.ms-warn.is-current').forEach((el) => el.classList.remove('is-current'));
          const row = q(hostEl, `.ms-warn[data-warn="${i}"]`);
          if (row) {
            if (row.classList) row.classList.add('is-current');
            const group = closest(row, 'details');
            if (group && 'open' in group) group.open = true;
            if (typeof row.scrollIntoView === 'function') row.scrollIntoView({ block: 'nearest' });
          }
          this.liveMessage = w.message || '';
          if (w.blockId) return this.goToBlock(w.blockId);
          const first = w.page ? this.firstBlockOfPage(w.page) : null;
          if (first) return this.goToBlock(first);
          return true;
        },
        // ] / [: the next / previous warning after the focused block (else the next in the list), wrapping.
        stepWarning(dir) {
          const n = this.warnings.length;
          if (!n) { toast('لا ملاحظات'); return false; }
          let i = -1;
          if (warnCursor === -1 && this.focused && blockIndex.has(this.focused)) {
            // the warning whose block is nearest after (before) the focused block in document order
            const pos = blockIndex.get(this.focused);
            let best = null;
            this.warnings.forEach((w, k) => {
              const at = w.blockId && blockIndex.has(w.blockId) ? blockIndex.get(w.blockId) : null;
              if (at === null || (dir > 0 ? at <= pos : at >= pos)) return;
              if (best === null || (dir > 0 ? at < best : at > best)) { best = at; i = k; }
            });
            if (i === -1) i = dir > 0 ? 0 : n - 1;
          } else {
            i = (warnCursor + dir + n) % n;
          }
          return this.goToWarning(i);
        },
        warningCursor() { return warnCursor; },
        setupSpy() {
          if (spy) { spy.disconnect(); spy = null; }
          visibleHeadings.clear();
          if (typeof IntersectionObserver !== 'function' || !headings.length) return;
          spy = new IntersectionObserver((entries) => this.onSpy(entries), { rootMargin: `-${TOPBAR_H + TOOLBAR_H}px 0px -55% 0px`, threshold: 0 });
          headings.forEach((h) => spy.observe(h));
        },
        onSpy(entries) {
          let left = null;
          entries.forEach((e) => {
            const id = attr(e.target, 'data-block');
            if (e.isIntersecting) visibleHeadings.add(id);
            else { visibleHeadings.delete(id); if (e.boundingClientRect && e.boundingClientRect.top < TOPBAR_H + TOOLBAR_H) left = id; }
          });
          const first = headings.map((h) => attr(h, 'data-block')).find((id) => visibleHeadings.has(id));
          this.setCurrentHeading(first || left || this.currentHeading);
        },
        setCurrentHeading(id) {
          if (!id || id === this.currentHeading) return;
          this.currentHeading = id;
          const hostEl = this.sideHost('toc');
          qa(hostEl, '.ms-toc-item.is-current').forEach((el) => el.classList.remove('is-current'));
          const item = q(hostEl, `.ms-toc-item[data-toc="${id}"]`);
          if (item && item.classList) {
            item.classList.add('is-current');
            const parent = closest(item.parentNode, '.ms-toc-item');
            if (parent && parent.classList) parent.classList.add('is-current');
            const link = q(item, '.ms-toc-link');
            if (link && typeof link.scrollIntoView === 'function') link.scrollIntoView({ block: 'nearest' });
          }
        },
        onResize() {
          if (this.tools.blockId) { const b = this.blockById(this.tools.blockId); if (b) this.showTools(b); }
          if (this.menu.open) this.menu = Object.assign({}, this.menu, { open: false });
          this.hideCard();
          this.hideNote();
        },

        // ------------------------------------------------------------ jump, copy, keyboard
        jumpTarget(value) {
          const n = parsePageNumber(value);
          if (!Number.isFinite(n)) return null;
          return this.firstBlockOfPage(n) ? n : null;
        },
        jump(value) {
          const n = this.jumpTarget(value);
          if (n === null) { toast('لا نصّ من هذه الصفحة في المخطوطة'); return null; }
          this.goToBlock(this.firstBlockOfPage(n));
          this.liveMessage = `صفحة ${n}`;
          return n;
        },
        focusJump() {
          const field = this.$refs && this.$refs.jump;
          if (field && field.focus) { field.focus(); if (field.select) field.select(); }
        },
        manuscriptText() { return article ? manuscriptText(article) : ''; },
        async copyText() {
          if (this.copying) return false;
          const text = this.manuscriptText();
          if (!text) { toast('لا نص لنسخه بعد'); return false; }
          this.copying = true;
          try { return await window.Nassakh.copyText(text); } finally { this.copying = false; }
        },
        openSheet() {
          this.sheetOpen = true;
          if (this.$nextTick) this.$nextTick(() => focusEl(this.$refs && this.$refs.sheetClose));
        },
        closeSheet() { this.sheetOpen = false; },
        topLayer() {
          if (this.sheetOpen) return 'sheet';
          if (this.convert.open) return 'convert';
          if (this.menu.open) return 'menu';
          if (this.drawer.open) return 'drawer';
          if (this.card.open) return 'card';
          if (this.notePop.open) return 'note';
          return null;
        },
        // Esc closes the top-most layer only (DESIGN.md §10).
        closeTop() {
          const layer = this.topLayer();
          if (layer === 'sheet') this.closeSheet();
          else if (layer === 'convert') this.closeConvert();
          else if (layer === 'menu') this.closeMenu(true);
          else if (layer === 'drawer') this.closeDrawer();
          else if (layer === 'card') this.hideCard(true);
          else if (layer === 'note') this.hideNote();
          return layer;
        },
        keyAction,
        onKey(e) {
          const t = e.target;
          const inField = Boolean(t && (['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName) || t.isContentEditable));
          const inMenu = Boolean(closest(t, '.menu, .ms-drawer, .ms-card, .rv-modal'));
          const action = keyAction(e, { inField, inMenu, hasBlock: Boolean(this.focused) });
          if (!action) return;
          if (action === 'blur') { if (t && t.blur) t.blur(); return; }
          if (action === 'close') { if (this.closeTop()) e.preventDefault(); return; }
          if (action === 'sheet') { e.preventDefault(); this.openSheet(); return; }
          if (action === 'jump') { e.preventDefault(); this.focusJump(); return; }
          if (action === 'seams') { this.toggleSeams(); return; }
          if (action === 'source') { if (this.focused) { e.preventDefault(); this.openSource(this.focused); } return; }
          if (action === 'copy') { this.copyText(); return; }
          if (action === 'next' || action === 'prev') { if (this.moveFocus(action === 'next' ? 1 : -1)) e.preventDefault(); return; }
          if (action === 'nextWarning' || action === 'prevWarning') { e.preventDefault(); this.stepWarning(action === 'nextWarning' ? 1 : -1); }
        },
      };
    });
  });
})();
