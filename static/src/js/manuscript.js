// Manuscript view (Phase 4, docs/PHASE4_SPEC.md §4.2): where the owner verifies the assembly (D22).
//   manuscriptView(config) – the view: polling while a run is on (the steps ticking, the reveal), the document
//                            swapped in place after re-runs (scroll anchored, focus restored, changed blocks
//                            flashing), the block «⋯» and its menu, the seam menu, the footnote popover, the
//                            source drawer, the side panel (contents with scroll spy, warnings), jump and the
//                            keyboard map; the top bar's primary leads on to the book page (D49), and once the
//                            text is edited there the structure tools rest (a run would replace the edits)
//   manuscriptBar          – the top-bar controls (base.html header_actions, outside the root) reading
//                            Alpine.store('manuscript').view
// Config (assembly.views.manuscript): { bookId, title, state, urls (with `book`, the book page), canEdit, canReview,
// pageCount, countsText }.
// The document is server-rendered (assembly/render.py) and swapped as one fragment: nothing inside the host
// carries an Alpine binding (x-ignore); hover, focus, clicks and keys are delegated on the host.
// One overlay (`pop`, the .ms-pop element) serves the block menu, the seam menu and the footnote: at most one
// is open, placed against the visible column like the review screen's word popover (below the anchor, flipped
// above near the bottom, never across an edge), closed by a click outside, Esc, scrolling it away, a resize,
// the document swap or opening another. Hover only previews (the «⋯» button, the source badge): every action
// is a click or a key.
// Motion (DESIGN.md §8, D24): the reveal (blocks rising in, join markers stitching), the flash after a
// re-run, the pending shimmer of the block whose re-run is on and the skeleton shimmer only;
// prefers-reduced-motion drops them all (manuscript.css).
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
  const isRtl = () => {
    try { return !(document.documentElement && document.documentElement.dir === 'ltr'); } catch (_) { return true; }
  };
  const storage = {
    get(key, fallback) { try { const v = window.localStorage.getItem(key); return v == null ? fallback : v; } catch (_) { return fallback; } },
    set(key, value) { try { window.localStorage.setItem(key, value); } catch (_) { /* private mode */ } },
  };
  const SEAMS_KEY = 'nassakh.manuscript.seams'; // '1' (default) | '0'
  const POLL_MS = 700; // §4.2: the steps tick every 700 ms
  const POLL_MAX_MS = 5000;
  const FAILURES_BEFORE_NOTICE = 3;
  const FLASH_MS = 600; // changed blocks after a re-run (accent-soft)
  const REVEAL_BLOCKS = 40; // the first blocks rise in with a 16 ms stagger (≤ 1 s in all)
  const REVEAL_MS = 1400; // the reveal classes are dropped after the stagger and the stitches (400 ms) ended
  const PULSE_MS = 500;
  const TOOLS_CLOSE_MS = 180; // hover intent: the «⋯» survives the gap between the block and the button
  const TICK_MS = 30000; // «قبل 5 دقائق» refreshes
  const LIVE_MS = 2000; // D70: the live state is fetched at most once every 2 s
  const CHANNEL = 'nassakh'; // review.js announces its saved changes there
  const POP_EDGE = 8; // the overlay keeps 8 px from the edges of the visible column (= review POP_EDGE)
  const POP_GAP = 6;
  const POP_SIZE = { menu: [250, 262], seam: [260, 200], note: [360, 96] }; // [w, h] before the first measure
  const TOOLS_TOP = 22; // the «⋯» button sits 22 px under the block's top, 24 px square (manuscript.css)
  const TOOLS_SIZE = 24;
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
  const setAttr = (el, name, value) => { if (el && typeof el.setAttribute === 'function') el.setAttribute(name, value); };
  const isDisabled = (el) => Boolean(el && (el.disabled || (typeof el.hasAttribute === 'function' && el.hasAttribute('disabled'))));
  const q = (root, sel) => (root && typeof root.querySelector === 'function' ? root.querySelector(sel) : null);
  const qa = (root, sel) => (root && typeof root.querySelectorAll === 'function' ? Array.from(root.querySelectorAll(sel)) : []);
  const closest = (el, sel) => (el && typeof el.closest === 'function' ? el.closest(sel) : null);
  const rect = (el) => (el && typeof el.getBoundingClientRect === 'function' ? el.getBoundingClientRect() : { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 });
  const focusEl = (el, opts) => { if (el && typeof el.focus === 'function') el.focus(opts || { preventScroll: true }); };
  const toast = (message) => { if (window.Nassakh && window.Nassakh.toast) window.Nassakh.toast(message); };
  const pagesOf = (el) => attr(el, 'data-pages').split(',').map((v) => parseInt(v, 10)).filter((n) => n > 0);
  const linesOf = (el) => attr(el, 'data-lines').split(',').map((v) => parseInt(v, 10)).filter((n) => n > 0);
  const kids = (el) => Array.from(el && el.children ? el.children : []); // HTMLCollection has no forEach / find
  const later = (fn) => { if (typeof queueMicrotask === 'function') queueMicrotask(fn); else Promise.resolve().then(fn); };

  // Placement of the overlay (pure, = review placePop): below the anchor, flipped above when it would leave the
  // visible column and there is more room above, its start edge on the anchor's start edge (RTL: the right),
  // never across a side edge. `a` anchor rect, `view` the visible area, `col` the column rect (the positioned
  // parent), `size` [w, h]. Returns { style, above } with the offsets relative to the column.
  function placeAgainst(a, view, col, size, rtl) {
    const w = Math.min(size[0], Math.max(120, view.right - view.left));
    const h = size[1];
    let top = a.bottom + POP_GAP;
    let above = false;
    if (top + h > view.bottom) {
      const roomAbove = a.top - POP_GAP - view.top;
      const roomBelow = view.bottom - top;
      if (roomAbove >= h || roomAbove > roomBelow) { top = Math.max(view.top, a.top - POP_GAP - h); above = true; } else top = Math.max(view.top, view.bottom - h);
    }
    let style;
    if (rtl) {
      let right = Math.min(a.right, view.right);
      if (right - w < view.left) right = Math.min(view.right, view.left + w);
      style = `top:${Math.round(top - col.top)}px;right:${Math.round(col.right - right)}px`;
    } else {
      let left = Math.max(a.left, view.left);
      if (left + w > view.right) left = Math.max(view.left, view.right - w);
      style = `top:${Math.round(top - col.top)}px;left:${Math.round(left - col.left)}px`;
    }
    return { style, above };
  }

  // Keyboard map (§4.2), pure so the tests can exercise it. ctx: inField (typing in a field), inMenu (the
  // focus is inside the overlay, a menu or the drawer, which keep their own arrows), hasBlock (a block is
  // focused: ↓/↑ move between blocks only then, so the page still scrolls with the arrows before any block is chosen).
  // Keys go through `NassakhKeys` (keys.js, D69): letters by their place, so the Arabic layout's «ش» on S is S;
  // ? also as «؟»; [ and ] by code (the Arabic layout types «ج» and «د» there); nothing while an IME composes.
  function keyAction(ev, ctx) {
    const K = window.NassakhKeys;
    if (K.composing(ev)) return null;
    const k = ev.key;
    if (k === 'Escape') return ctx.inField ? 'blur' : 'close';
    if (ctx.inField || ev.metaKey || ev.ctrlKey || ev.altKey) return null;
    if (K.is(ev, '?')) return 'sheet';
    const letter = K.letter(ev);
    if (letter === 'g') return 'jump';
    if (letter === 's') return 'seams';
    if (letter === 'o') return 'source';
    if (K.is(ev, ']')) return 'nextWarning';
    if (K.is(ev, '[')) return 'prevWarning';
    if (ctx.inMenu) return null;
    if (letter === 'j') return 'next';
    if (letter === 'k') return 'prev';
    if (k === 'ArrowDown') return ctx.hasBlock ? 'next' : null;
    if (k === 'ArrowUp') return ctx.hasBlock ? 'prev' : null;
    return null;
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
    keyAction, arCount, relativeTime, parseFragment, parsePageNumber, placeAgainst, STAGE_KEYS,
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
      let column = null; // .ms-column, the positioned parent of the floating tools and the overlay
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
      let toolsTimer = null;
      let flashTimer = null;
      let revealTimer = null;
      let pulseTimer = null;
      let tickTimer = null;
      let bound = false;
      let liveAt = 0; // the last live refresh (D70), at most one every LIVE_MS
      let liveTimer = null;
      let liveChannel = null;
      let justOpened = false; // the overlay opened in this event turn: the outside-click of the same click is not a close
      let returnTo = null; // block id the focus goes back to when the drawer closes
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
        sideOpen: false,
        countsText: cfg.countsText || '',
        warnings: [], // the run's warnings in document order (from the fragment's meta)
        warningsTotal: 0,
        toc: [],
        tocCount: 0,
        focused: null, // focused block id
        pending: null, // the block whose re-run is on (is-pending)
        tools: { blockId: null, style: '' },
        // the one overlay: kind 'menu' (block menu, anchored at the «⋯»), 'seam' (seam menu) or 'note' (footnote)
        pop: { kind: null, anchorId: '', style: '', above: false },
        menu: { blockId: null, src: '', reviewed: true, role: 'body', reviewUrl: '', lines: [] },
        seam: { page: 0, from: 0, mode: '', decision: 'auto', text: '', state: '' },
        note: { id: '', number: '', html: '', orphan: false, found: false },
        drawer: { open: false, blockId: null, pages: [], index: 0, lines: [], sheet: null, loading: false, error: '' },
        convert: { open: false, busy: false, error: '', label: 'تحويل', edited: false, options: { footnote_numbering: 'page', include_unreviewed: true, strip_tatweel: true, strip_running_heads: true }, unreviewed: 0 },
        busy: false, // an override is on the wire or its run is on: no second post meanwhile
        bookUrl: urls.book || '',
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
          this.bindLive();
        },
        destroy() {
          this.stopPolling();
          clearTimeout(liveTimer);
          if (liveChannel) { try { liveChannel.close(); } catch (_) { /* closed */ } liveChannel = null; }
          [toolsTimer, flashTimer, revealTimer, pulseTimer].forEach((t) => clearTimeout(t));
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
        // D49: the text was saved from the book page since the last run: it is the book now; the structure
        // tools here would re-assemble over it, so they rest and the book page takes the changes
        get edited() { return Boolean(this.state.edited) && this.hasDocument; },
        get editedText() {
          const n = this.stalePages.length;
          if (this.state.stale && n) return `حُرِّر نص الكتاب في صفحة الكتاب، ثم تغيّر نص ${arCount(n, PAGES)} في المراجعة: تُراجَع الفصول المتأثرة هناك.`;
          return 'حُرِّر نص الكتاب في صفحة الكتاب، فهو النص المعتمد الآن: تُغيَّر العناوين ووصل الفقرات هناك.';
        },
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
          if (this.edited) return { state: 'saved', text: 'حُرِّر في صفحة الكتاب' };
          if (this.state.stale && this.hasDocument) return { state: 'warn', text: 'تغيّر النص بعد التجميع' };
          if (this.state.exists && this.state.assembled_at) return { state: 'saved', text: `مُجمَّعة ${relativeTime(this.state.assembled_at, this.now)}` };
          return { state: 'idle', text: '' };
        },
        // exactly one primary (D49): re-assemble when the pages changed before any edit, or after a failure
        // (editors); else the next step, the book page; convert before the first run (editors); nothing while
        // a run is on
        get primary() {
          if (this.active) return '';
          if (this.canEdit && (this.failed || (this.state.stale && !this.edited)) && this.hasDocument) return 'reassemble';
          if (this.hasDocument && this.bookUrl) return 'book';
          if (this.canEdit && !this.hasDocument) return this.failed ? 'reassemble' : 'convert';
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
            if (waitingRun !== null) { this.endWait(); toast(run.error || 'فشل التجميع'); }
            if (!opts.initial) this.liveMessage = run.error || 'فشل التجميع';
            return;
          }
          if (s.exists) {
            if (!opts.initial && Number(s.version) !== this.loadedVersion) { this.reload({ reveal: !this.hasDocument, anchor: pendingAnchor }); return; }
            this.phase = this.hasDocument ? 'ready' : (opts.initial ? 'ready' : 'assembling');
            if (waitingRun !== null) this.endWait();
            return;
          }
          this.phase = 'empty';
        },
        // A post's run ended (the swap landed, or it failed): the pending block and the busy flag are released.
        endWait() {
          waitingRun = null;
          pendingAnchor = null;
          this.busy = false;
          this.clearPending();
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

        // ------------------------------------------------------------ live state (D70)
        // A change in review (another tab) shows here without a reload: the state is fetched again when the tab
        // comes back (focus, visibility, a return from the back-forward cache) and on the review screen's
        // message on BroadcastChannel('nassakh'). When the changed pages differ, the document is swapped too, so
        // the amber mark of a page approved meanwhile goes at once.
        bindLive() {
          const refresh = () => { this.refreshLive(); };
          if (typeof window !== 'undefined') {
            listen(window, 'focus', refresh);
            listen(window, 'pageshow', (e) => { if (e && e.persisted) refresh(); });
          }
          if (typeof document !== 'undefined') listen(document, 'visibilitychange', () => { if (!document.hidden) refresh(); });
          try {
            if (typeof BroadcastChannel === 'function') {
              liveChannel = new BroadcastChannel(CHANNEL);
              if (typeof liveChannel.unref === 'function') liveChannel.unref(); // Node (the tests): never hold the process open
              liveChannel.onmessage = (e) => {
                const m = e && e.data;
                if (m && m.type === 'review' && Number(m.book) === Number(this.bookId)) refresh();
              };
            }
          } catch (_) { liveChannel = null; }
        },
        // At most one request every LIVE_MS; a trigger inside that window is kept for its end (a burst of
        // review changes ends in one refresh that sees them all). Nothing while a run is polled anyway.
        refreshLive() {
          if (this.active || !this.state.exists) return false;
          const wait = liveAt + LIVE_MS - Date.now();
          if (wait > 0) {
            if (!liveTimer) liveTimer = setTimeout(() => { liveTimer = null; this.refreshLive(); }, wait);
            return false;
          }
          liveAt = Date.now();
          return this.fetchLive();
        },
        async fetchLive() {
          const r = await api(urls.state);
          if (!r.ok || !r.data) return false;
          const before = JSON.stringify(this.stalePages);
          const version = this.loadedVersion;
          this.applyState(r.data);
          if (this.active) { this.schedulePoll(POLL_MS); return true; }
          const same = Number(r.data.version) === version;
          if (same && this.hasDocument && JSON.stringify(this.stalePages) !== before) await this.reload();
          return true;
        },

        // ------------------------------------------------------------ starting runs
        resetConvert() {
          const s = this.state || {};
          this.convert.options = Object.assign({ footnote_numbering: 'page', include_unreviewed: true, strip_tatweel: true, strip_running_heads: true }, s.options || {});
          this.convert.unreviewed = Number(s.unreviewed_pages) || 0;
          this.convert.edited = Boolean(s.edited && s.exists);
          this.convert.label = this.convert.edited ? 'استبدال النص المحرَّر' : s.exists ? 'إعادة التجميع' : 'تحويل';
          this.convert.error = '';
        },
        openConvert() {
          this.closePop();
          this.resetConvert();
          this.convert.open = true;
          if (this.$nextTick) this.$nextTick(() => focusEl(q(document, '.ms-convert [role="radio"][aria-checked="true"]') || q(document, '.ms-convert-actions button')));
        },
        closeConvert() {
          this.convert.open = false;
        },
        // the popover's button is the one confirmation of replacing an edited text (D49)
        async submitConvert() {
          const ok = await this.startAssembly(Object.assign({}, this.convert.options, this.convert.edited ? { replace_edited: true } : {}));
          if (ok) this.convert.open = false;
        },
        reassemble() {
          if (this.edited) { this.openConvert(); return Promise.resolve(false); }
          return this.startAssembly(Object.assign({}, this.convert.options));
        },
        // POST assemble: the run is shown at once (the steps, or the pill over the old document), then polled.
        async startAssembly(options) {
          if (this.convert.busy) return false;
          this.convert.busy = true;
          this.convert.error = '';
          const r = await api(urls.assemble, { method: 'POST', body: options || {} });
          this.convert.busy = false;
          if (r.status === 409 && r.data && r.data.edited) { this.markEdited(); return false; }
          if (!r.ok) { this.convert.error = r.message; toast(r.message); return false; }
          this.beginRun(r.data, null);
          return true;
        },
        // The server says the text was edited meanwhile (another window, 409): the tools rest and the popover
        // offers the replacement.
        markEdited() {
          this.state = Object.assign({}, this.state, { edited: true });
          this.closePop();
          this.openConvert();
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
        // A seam / role / suggestion post: 202, then the document re-runs and swaps in place. `pendingId` is the
        // block shown as pending meanwhile; `anchor` the block ids to keep in place across the swap.
        async postRun(url, body, anchor, pendingId) {
          if (this.edited) { toast('حُرِّر النص في صفحة الكتاب؛ غيّر البنية هناك'); return false; }
          if (this.busy) { toast('انتظر انتهاء التجميع الجاري'); return false; }
          this.busy = true;
          this.markPending(pendingId || (anchor && anchor[0]) || null);
          const r = await api(url, { method: 'POST', body });
          if (r.status === 409 && r.data && r.data.edited) { this.busy = false; this.clearPending(); this.markEdited(); toast(r.message); return false; }
          if (!r.ok) { this.busy = false; this.clearPending(); toast(r.message); return false; }
          this.beginRun(r.data, anchor);
          return true;
        },
        markPending(id) {
          this.clearPending();
          const b = id ? this.blockById(id) : null;
          if (b && b.classList) b.classList.add('is-pending');
          this.pending = b ? id : null;
        },
        clearPending() {
          const b = this.pending ? this.blockById(this.pending) : null;
          if (b && b.classList) b.classList.remove('is-pending');
          this.pending = null;
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
            this.endWait();
            return false;
          }
          this.swapFragment(r.data, opts);
          return true;
        },
        // Replace the document with a rendered fragment: the scroll stays on the same block, the focus returns
        // to the same block (the «⋯» with it), the blocks whose markup changed flash once; the first document
        // rises in (the reveal). Every floating thing closes: it pointed at elements that are gone.
        swapFragment(html, opts = {}) {
          if (!host) this.bindDom();
          if (!host) return;
          const before = new Map();
          if (this.hasDocument) blocks.forEach((b) => before.set(attr(b, 'data-block'), b.innerHTML));
          const anchor = this.anchorBefore(opts.anchor);
          const hadFocus = this.focusInDocument();
          this.closePop();
          this.hideTools(true);
          this.pending = null;
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
          this.restoreFocus(anchor, hadFocus);
          waitingRun = null;
          pendingAnchor = null;
          this.busy = false;
          this.liveMessage = before.size ? 'حُدّثت المخطوطة' : 'اكتمل التجميع';
        },
        // Whether the keyboard focus sits in the document (a block, a seam, a reference): then it is put back
        // on the same block after the swap; a focus in the toolbar, the panel or the drawer is left alone.
        focusInDocument() {
          const active = typeof document !== 'undefined' ? document.activeElement : null;
          if (!active || typeof active.closest !== 'function') return false;
          return Boolean(closest(active, '[data-ms-host], .ms-tools, .ms-pop'));
        },
        restoreFocus(anchor, hadFocus) {
          const id = this.focused && blockIndex.has(this.focused) ? this.focused : (anchor && anchor.ids.find((c) => blockIndex.has(c))) || null;
          this.focused = id;
          const block = id ? this.blockById(id) : null;
          if (!block) return false;
          if (hadFocus) focusEl(block, { preventScroll: true });
          this.showTools(block);
          return true;
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
          listen(host, 'mouseleave', () => this.hideToolsSoon());
          listen(host, 'focusin', (e) => this.onHostFocusIn(e));
          listen(host, 'click', (e) => this.onHostClick(e));
          listen(host, 'keydown', (e) => this.onHostKey(e));
        },
        // The fragment carries the side panel's parts and the meta JSON: they move to their hosts.
        sideHost(name) {
          const root = this.$el || (hasDOM ? document.querySelector('[data-manuscript]') : null);
          return q(root, `[data-ms-${name}-host]`);
        },
        adoptParts(root) {
          const parts = { toc: this.sideHost('toc'), warnings: this.sideHost('warnings') };
          qa(root, '[data-ms-part]').forEach((part) => {
            const target = parts[attr(part, 'data-ms-part')];
            if (!target) { if (typeof part.remove === 'function') part.remove(); return; }
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
          this.tocCount = this.toc.reduce((n, h) => n + 1 + (Array.isArray(h.children) ? h.children.length : 0), 0);
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
        neighbours(id) {
          const i = blockIndex.get(id);
          if (i === undefined) return [id];
          return [id, i > 0 ? attr(blocks[i - 1], 'data-block') : null, i + 1 < blocks.length ? attr(blocks[i + 1], 'data-block') : null].filter(Boolean);
        },
        // The block to keep in place across a swap: the given candidates (the seam's block, then its
        // neighbours), else the first block under the toolbar.
        // Each candidate keeps its own place: the one found after the swap is put back where it was (a block
        // whose role changed comes back under its other id, `p` ↔ `h`: that id is tried first, in its place).
        anchorBefore(candidates) {
          if (!blocks.length) return null;
          let order = Array.isArray(candidates) ? candidates : [];
          let ids = order.filter((id) => blockIndex.has(id));
          if (!ids.length) {
            const top = TOPBAR_H + TOOLBAR_H;
            const first = blocks.find((b) => rect(b).bottom > top) || blocks[0];
            ids = this.neighbours(attr(first, 'data-block'));
            order = ids;
          }
          const tops = {};
          ids.forEach((id) => { tops[id] = rect(this.blockById(id)).top; });
          order.forEach((id, i) => { if (!(id in tops) && order[i + 1] in tops) tops[id] = tops[order[i + 1]]; });
          return { ids: order.filter((id) => id in tops), tops, top: tops[ids[0]] };
        },
        anchorAfter(anchor) {
          if (!anchor) return;
          const id = anchor.ids.find((candidate) => blockIndex.has(candidate));
          if (!id || typeof window.scrollBy !== 'function') return;
          const was = anchor.tops && id in anchor.tops ? anchor.tops[id] : anchor.top;
          const delta = rect(this.blockById(id)).top - was;
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

        // ------------------------------------------------------------ the overlay: one element, one open at a time
        // The visible part of the column: under the top bar and the toolbar, inside the window, 8 px from the edges.
        visibleArea() {
          const c = rect(column);
          const W = (typeof window !== 'undefined' && window.innerWidth) || 1200;
          const H = (typeof window !== 'undefined' && window.innerHeight) || 800;
          return { top: TOPBAR_H + TOOLBAR_H + POP_EDGE, bottom: H - POP_EDGE, left: Math.max(c.left, 0) + POP_EDGE, right: Math.min(c.right, W) - POP_EDGE };
        },
        // The element the open overlay hangs from, looked up afresh (never a reference that a swap detached).
        anchorEl() {
          const kind = this.pop.kind;
          const id = this.pop.anchorId;
          if (kind === 'menu') return this.blockById(id);
          if (kind === 'seam') return this.seamEl(id);
          if (kind === 'note') return this.refEl(id);
          return null;
        },
        // The rect to place against: the block menu hangs from the «⋯» button (a fixed spot in the start
        // margin, computed rather than measured: the button moves only on the next paint), the others from
        // the seam or the reference itself.
        anchorRect() {
          const el = this.anchorEl();
          if (!el) return null;
          const r = rect(el);
          if (this.pop.kind !== 'menu') return r;
          const c = rect(column);
          const top = r.top + TOOLS_TOP;
          const right = isRtl() ? c.right : c.left + TOOLS_SIZE;
          return { top, bottom: top + TOOLS_SIZE, right, left: right - TOOLS_SIZE, width: TOOLS_SIZE, height: TOOLS_SIZE };
        },
        placePop() {
          const a = this.anchorRect();
          if (!a) return false;
          const node = this.$refs && this.$refs.pop;
          const guess = POP_SIZE[this.pop.kind] || POP_SIZE.menu;
          const size = [node && node.offsetWidth ? node.offsetWidth : guess[0], node && node.offsetHeight ? node.offsetHeight : guess[1]];
          const placed = placeAgainst(a, this.visibleArea(), rect(column), size, isRtl());
          this.pop = Object.assign({}, this.pop, placed);
          return true;
        },
        openPop(kind, anchorId, opts = {}) {
          const same = this.pop.kind === kind && this.pop.anchorId === anchorId;
          if (!same) this.setExpanded(false);
          this.pop = { kind, anchorId, style: this.pop.style, above: false };
          this.setExpanded(true);
          justOpened = true;
          later(() => { justOpened = false; });
          this.placePop(); // a first placement from the size guess, then the measured one on the next tick
          if (this.$nextTick) {
            this.$nextTick(() => {
              if (this.pop.kind !== kind || this.pop.anchorId !== anchorId) return;
              this.placePop();
              if (opts.focus) this.focusPopItem(0);
            });
          }
          return true;
        },
        // Close the overlay (no-op when none is open); `refocus` puts the focus back on its anchor.
        closePop(refocus) {
          const kind = this.pop.kind;
          if (!kind) return null;
          const el = this.anchorEl();
          this.setExpanded(false);
          if (kind === 'note') qa(article, '.ms-note.is-hot').forEach((n) => n.classList.remove('is-hot'));
          this.pop = { kind: null, anchorId: '', style: this.pop.style, above: false };
          if (refocus && el) focusEl(el, { preventScroll: true });
          return kind;
        },
        setExpanded(on) {
          const el = this.anchorEl();
          if (el && this.pop.kind !== 'menu') setAttr(el, 'aria-expanded', on ? 'true' : 'false');
        },
        // A click anywhere outside the overlay closes it; the click that opened it (a seam, a reference, the
        // «⋯») is not a close, however the event reaches the document.
        onPopOutside() {
          if (!this.pop.kind || justOpened) return false;
          this.closePop();
          return true;
        },
        popItems() {
          const node = this.$refs && this.$refs.pop;
          return qa(node, '.menu-item, .ms-pop-note-link').filter((el) => !isDisabled(el));
        },
        focusPopItem(i) {
          const items = this.popItems();
          if (!items.length) return false;
          focusEl(items[(i + items.length) % items.length]);
          return true;
        },
        movePop(dir) {
          const items = this.popItems();
          if (!items.length) return false;
          const active = typeof document !== 'undefined' ? document.activeElement : null;
          const i = items.indexOf(active);
          return this.focusPopItem(i === -1 ? (dir > 0 ? 0 : -1) : i + dir);
        },
        // Scrolling the anchor out of the visible column closes the overlay (it scrolls with the column
        // meanwhile, being positioned inside it).
        onScroll() {
          if (!this.pop.kind) return false;
          const a = this.anchorRect();
          const view = this.visibleArea();
          if (!a || a.bottom < view.top - POP_EDGE || a.top > view.bottom + POP_EDGE) { this.closePop(); return true; }
          return false;
        },
        onResize() {
          if (this.tools.blockId) { const b = this.blockById(this.tools.blockId); if (b) this.showTools(b); }
          if (this.pop.kind) this.placePop();
        },

        // ------------------------------------------------------------ seams
        setSeams(on) {
          this.seams = Boolean(on);
          storage.set(SEAMS_KEY, this.seams ? '1' : '0');
          if (!this.seams && this.pop.kind === 'seam') this.closePop();
        },
        toggleSeams() { this.setSeams(!this.seams); },
        seamInfo(el) {
          return { page: parseInt(attr(el, 'data-page'), 10) || 0, from: parseInt(attr(el, 'data-from'), 10) || 0, mode: attr(el, 'data-mode'), decision: attr(el, 'data-decision') || 'auto' };
        },
        // What the seam menu says (§4.2): the decision, plainly, then the state (automatic or manual).
        seamText(s) {
          if (s.mode === 'join') return { text: `وُصلت الفقرة بين الصفحتين ${s.from} و${s.page}`, state: s.decision === 'override' ? 'قرار يدوي' : 'تلقائي' };
          if (s.mode === 'split') return { text: `فُصلت الفقرة عند الصفحة ${s.page}`, state: s.decision === 'override' ? 'قرار يدوي' : 'تلقائي' };
          return { text: `صفحة ${s.page - 1} غير مُضمَّنة`, state: '' };
        },
        seamEl(page) { return q(article, `.ms-seam[data-page="${page}"]`); },
        // The seam menu: from a click or Enter on a join marker or a split hairline (missing pages have none).
        openSeamMenu(el, opts = {}) {
          const s = this.seamInfo(el);
          if (!this.seams || !s.page || (s.mode !== 'join' && s.mode !== 'split')) return false;
          this.seam = Object.assign(s, this.seamText(s));
          return this.openPop('seam', String(s.page), opts);
        },
        // A choice in the seam menu: the mode already in effect (the checked item), or «تلقائي» on an automatic
        // decision, just closes it; else the override posts and the block re-runs.
        chooseSeam(mode) {
          const s = this.seam;
          const already = mode === 'auto' ? s.decision !== 'override' : mode === s.mode;
          this.closePop(true);
          if (!mode || already) return Promise.resolve(false);
          return this.postSeam(s.page, mode);
        },
        // The override of a seam: the anchor (and the pending block) is the block that holds it, or for a split
        // hairline (it sits between blocks) the first block of the page.
        postSeam(page, mode) {
          if (!this.canEdit || !page || !mode) return Promise.resolve(false);
          const el = this.seamEl(page);
          const block = closest(el, '.ms-block');
          let ids = [];
          if (block) ids = this.neighbours(attr(block, 'data-block'));
          else {
            const first = this.firstBlockOfPage(page);
            if (first) { const i = blockIndex.get(first); if (i > 0) ids.push(attr(blocks[i - 1], 'data-block')); ids.push(first); }
          }
          this.liveMessage = mode === 'auto' ? `أُعيد القرار التلقائي لفاصل الصفحة ${page}` : mode === 'join' ? `تُوصل الفقرة عبر الصفحة ${page}` : `تُفصل الفقرة عند الصفحة ${page}`;
          return this.postRun(urls.seams, { page, mode }, ids, block ? attr(block, 'data-block') : this.firstBlockOfPage(page));
        },

        // ------------------------------------------------------------ blocks: the «⋯», the menu, roles
        onHostOver(e) {
          const block = closest(e.target, '.ms-block');
          if (block && attr(block, 'data-block') !== this.tools.blockId) this.showTools(block);
          else if (block) clearTimeout(toolsTimer);
        },
        onHostFocusIn(e) {
          const t = e.target;
          const ref = closest(t, '.ms-ref');
          if (ref) { this.openNote(ref); return; }
          if (closest(t, '.ms-seam')) return;
          const block = closest(t, '.ms-block');
          if (block) { this.focused = attr(block, 'data-block'); this.showTools(block); }
        },
        onHostClick(e) {
          const t = e.target;
          const ref = closest(t, '.ms-ref');
          if (ref) { e.preventDefault(); this.openNote(ref); return; }
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
          // (a menu button: the focus goes into its menu, arrows and Enter work at once)
          if (seam) { e.preventDefault(); if (this.pop.kind === 'seam' && this.pop.anchorId === attr(seam, 'data-page')) this.closePop(); else this.openSeamMenu(seam, { focus: true }); }
        },
        onHostKey(e) {
          const t = e.target;
          const seam = closest(t, '.ms-seam');
          if (seam) {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); this.openSeamMenu(seam, { focus: true }); }
            return;
          }
          if (closest(t, 'button, a')) return; // refs, chips and back links keep their native keys
          const block = closest(t, '.ms-block');
          if (block && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); this.openMenu(attr(block, 'data-block'), { focus: true }); }
        },
        // The «⋯» follows the hovered / focused block, except while a menu is open (it keeps its block) unless
        // `force`: the menu itself moving to another block.
        showTools(block, force) {
          clearTimeout(toolsTimer);
          const id = attr(block, 'data-block');
          if (this.pop.kind === 'menu' && this.pop.anchorId !== id && !force) return;
          const previous = this.tools.blockId ? this.blockById(this.tools.blockId) : null;
          if (previous && previous !== block && previous.classList) previous.classList.remove('is-tools');
          if (block.classList) block.classList.add('is-tools');
          const top = Math.round(rect(block).top - rect(column).top);
          this.tools = { blockId: id, style: `top:${top}px` };
        },
        hideTools(force) {
          clearTimeout(toolsTimer);
          if (this.pop.kind === 'menu' && !force) return;
          const previous = this.tools.blockId ? this.blockById(this.tools.blockId) : null;
          if (previous && previous.classList) previous.classList.remove('is-tools');
          this.tools = { blockId: null, style: '' };
        },
        hideToolsSoon() {
          clearTimeout(toolsTimer);
          toolsTimer = setTimeout(() => { if (!this.focused || this.focused !== this.tools.blockId) this.hideTools(); }, TOOLS_CLOSE_MS);
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
          this.closePop();
          return this.focusBlock(attr(blocks[i], 'data-block'), { instant: true });
        },
        goToBlock(id, opts = {}) {
          const el = this.blockById(id);
          if (!el) { toast('لم تُعثر على الفقرة في المخطوطة'); return false; }
          this.closePop();
          if (opts.focus !== false) this.focusBlock(id, { block: 'center' });
          else if (typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'center', behavior: reducedMotion() ? 'auto' : 'smooth' });
          if (el.classList) {
            el.classList.add('is-pulse');
            clearTimeout(pulseTimer);
            pulseTimer = setTimeout(() => el.classList.remove('is-pulse'), PULSE_MS);
          }
          return true;
        },
        // The block menu, from the «⋯» (a click) or Enter on the focused block (the first item takes the focus).
        openMenu(id, opts = {}) {
          const block = this.blockById(id);
          if (!block) return false;
          this.showTools(block, true);
          const pages = pagesOf(block);
          this.menu = {
            blockId: id,
            src: attr(block, 'data-src'),
            reviewed: attr(block, 'data-reviewed') !== 'false',
            role: ROLE_OF_TAG[block.tagName] || 'body',
            reviewUrl: this.reviewUrl(pages[0]),
            lines: linesOf(block),
          };
          return this.openPop('menu', id, opts);
        },
        toggleMenu(id) {
          const target = id || this.tools.blockId;
          if (this.pop.kind === 'menu' && this.pop.anchorId === target) this.closePop(true); else if (target) this.openMenu(target, { focus: true });
        },
        closeMenu(refocus) { return this.pop.kind === 'menu' ? this.closePop(refocus) : null; },
        // The block's lines take the role (through the review service, D38), then the document re-runs.
        setRole(id, role) {
          const block = this.blockById(id);
          if (!block || !this.canReview) return Promise.resolve(false);
          const current = ROLE_OF_TAG[block.tagName] || 'body';
          this.closeMenu(true);
          if (role === current) return Promise.resolve(false);
          this.liveMessage = role === 'body' ? 'تصير الفقرة محتوى' : role === 'heading' ? 'تصير الفقرة عنوانًا رئيسيًا' : 'تصير الفقرة عنوانًا فرعيًا';
          // the block comes back under its other id (a paragraph `p…` becomes a heading `h…`): anchored first
          const renamed = `${role === 'body' ? 'p' : 'h'}${String(id).slice(1)}`;
          const anchor = renamed === id ? this.neighbours(id) : [renamed, ...this.neighbours(id)];
          return this.postRun(urls.roles, { line_ids: linesOf(block), role }, anchor, id);
        },
        acceptSuggestion(id) { return this.setRole(id, 'heading'); },
        dismissSuggestion(id) {
          if (!this.canEdit) return Promise.resolve(false);
          const i = blockIndex.get(id);
          const ids = i === undefined ? [id] : [id, i > 0 ? attr(blocks[i - 1], 'data-block') : null].filter(Boolean);
          this.liveMessage = 'أُهمل الاقتراح';
          return this.postRun(urls.suggestions, { block_id: id, action: 'dismiss' }, ids, id);
        },

        // ------------------------------------------------------------ footnotes: the popover, the jumps
        noteEl(noteId) { return q(article, `.ms-note[data-note="${noteId}"]`); },
        refEl(noteId) { return q(article, `.ms-ref[data-note="${noteId}"]`); },
        // The note of a reference in the overlay (a click, or the focus landing on the reference); the chapter's
        // notes list keeps it too, marked while the popover is open.
        openNote(ref) {
          const noteId = attr(ref, 'data-note');
          if (!noteId) return false;
          // the same reference again (a click after the focus that opened it) goes through openPop too: the
          // outside-click of that click must not close what is open
          const li = this.noteEl(noteId);
          const body = li ? kids(li).find((c) => hasClass(c, 'ms-note-body')) : null;
          qa(article, '.ms-note.is-hot').forEach((n) => n.classList.remove('is-hot'));
          if (li && li.classList) li.classList.add('is-hot');
          this.note = { id: noteId, number: attr(ref, 'data-number'), html: body ? body.innerHTML : '', orphan: attr(ref, 'data-orphan') === 'true', found: Boolean(li) };
          return this.openPop('note', noteId);
        },
        goToNote(noteId) {
          const li = this.noteEl(noteId);
          if (!li) return false;
          this.closePop();
          if (typeof li.scrollIntoView === 'function') li.scrollIntoView({ block: 'center', behavior: reducedMotion() ? 'auto' : 'smooth' });
          focusEl(q(li, '.ms-note-num'));
          if (li.classList) { li.classList.add('is-pulse'); clearTimeout(pulseTimer); pulseTimer = setTimeout(() => li.classList.remove('is-pulse'), PULSE_MS); }
          return true;
        },
        backToRef(noteId) {
          const ref = this.refEl(noteId);
          if (!ref) return false;
          this.closePop();
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
          this.closePop();
          returnTo = attr(block, 'data-block');
          const pages = pagesOf(block);
          this.drawer = { open: true, blockId: returnTo, pages, index: 0, lines: linesOf(block), sheet: null, loading: true, error: '' };
          this.liveMessage = `الأصل: صفحة ${pages[0] || ''}`;
          if (this.$nextTick) this.$nextTick(() => focusEl(this.$refs && this.$refs.drawerClose));
          await this.loadSheet(pages[0]);
          return true;
        },
        // Closing returns the focus to the block it opened from, looked up by id: a swap meanwhile is no matter.
        closeDrawer() {
          if (!this.drawer.open) return;
          this.drawer = Object.assign({}, this.drawer, { open: false });
          const target = returnTo ? this.blockById(returnTo) : null;
          if (target) { this.focused = returnTo; focusEl(target, { preventScroll: true }); this.showTools(target); }
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

        // ------------------------------------------------------------ jump, keyboard
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
          this.closePop();
          const field = this.$refs && this.$refs.jump;
          if (field && field.focus) { field.focus(); if (field.select) field.select(); }
        },
        openSheet() {
          this.closePop();
          this.sheetOpen = true;
          if (this.$nextTick) this.$nextTick(() => focusEl(this.$refs && this.$refs.sheetClose));
        },
        closeSheet() { this.sheetOpen = false; },
        // The layers, top-most first: the sheet, the convert popover, the overlay (its kind), the drawer.
        topLayer() {
          if (this.sheetOpen) return 'sheet';
          if (this.convert.open) return 'convert';
          if (this.pop.kind) return this.pop.kind;
          if (this.drawer.open) return 'drawer';
          return null;
        },
        // Esc closes the top-most layer only (DESIGN.md §10).
        closeTop() {
          const layer = this.topLayer();
          if (layer === 'sheet') this.closeSheet();
          else if (layer === 'convert') this.closeConvert();
          else if (layer === 'menu' || layer === 'seam' || layer === 'note') this.closePop(true);
          else if (layer === 'drawer') this.closeDrawer();
          return layer;
        },
        keyAction,
        onKey(e) {
          const t = e.target;
          const inField = Boolean(t && (['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName) || t.isContentEditable));
          const inMenu = Boolean(closest(t, '.menu, .ms-pop, .ms-drawer, .rv-modal'));
          const action = keyAction(e, { inField, inMenu, hasBlock: Boolean(this.focused) });
          if (!action) return;
          if (action === 'blur') { if (t && t.blur) t.blur(); return; }
          if (action === 'close') { if (this.closeTop()) e.preventDefault(); return; }
          if (action === 'sheet') { e.preventDefault(); this.openSheet(); return; }
          if (action === 'jump') { e.preventDefault(); this.focusJump(); return; }
          if (action === 'seams') { this.toggleSeams(); return; }
          if (action === 'source') { if (this.focused) { e.preventDefault(); this.openSource(this.focused); } return; }
          if (action === 'next' || action === 'prev') { if (this.moveFocus(action === 'next' ? 1 : -1)) e.preventDefault(); return; }
          if (action === 'nextWarning' || action === 'prevWarning') { e.preventDefault(); this.stepWarning(action === 'nextWarning' ? 1 : -1); }
        },
      };
    });
  });
})();
