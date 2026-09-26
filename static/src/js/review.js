// Review screen (Phase 3, PHASE3_SPEC §4).
//   reviewScreen(config)  – the whole screen: scan viewer, lines column, popover, autosave, filmstrip
//   reviewBar             – the top-bar controls (rendered in base.html's header_actions, outside the
//                           screen's root) reading Alpine.store('review').bar and calling .act(name, arg)
// Config = review_payload (docs/PHASE3_SPEC.md §4), read from <script id="review-config">.
// Every action is optimistic: the UI changes at once, the POST follows, a failure rolls back and the
// save chip offers a retry. Undo, approve and page swaps re-render from the payload the server returns.
// Motion: chrome 120–200 ms; resolution moments ≤ 400 ms; prefers-reduced-motion drops glides and waves.
(function () {
  'use strict';

  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const hasDOM = typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function'
    && typeof document !== 'undefined' && typeof document.createElement === 'function';
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
  const SWAP_KEY = 'nassakh.review.swapped';
  const FIT_KEY = 'nassakh.review.fit'; // 'height' (default) | 'width'
  const ZOOM_MIN = 1;
  const ZOOM_MAX = 6;
  const SAVED_MS = 1600;
  const FLASH_MS = 700;
  const POLL_MS = 3000;
  const UNDO_TOAST_MS = 8000;
  // word popover placement (DESIGN.md: open below, flip above when needed, keep 8 px from the edges)
  const POP_EDGE = 8;
  const POP_GAP = 8;
  const POP_W = 272;
  const POP_H_GUESS = 220;
  const MORE_W = 248; // the «إجراءات أخرى» submenu
  const MORE_CLOSE_MS = 180; // hover intent: the submenu survives the gap between the two panels
  const ROLES = [
    { value: 'body', label: 'محتوى' },
    { value: 'heading', label: 'عنوان رئيسي' },
    { value: 'subheading', label: 'عنوان فرعي' },
  ];
  const isDigits = (word) => /^[0-9٠-٩۰-۹]+$/.test(word);

  // fetch wrapper: never throws; `{ ok, status, data, message }` with an Arabic message on failure.
  async function api(url, options) {
    const opts = options || {};
    const method = opts.method || 'GET';
    const headers = { Accept: 'application/json' };
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
    try { data = await response.json(); } catch (_) { data = null; }
    let message = data && (data.message || data.detail);
    if (!message) {
      if (response.status === 401) message = 'انتهت الجلسة. سجّل الدخول من جديد.';
      else if (response.status === 403) message = 'لا تملك صلاحية المراجعة.';
      else message = 'تعذّر تنفيذ الطلب. حاول مرة أخرى.';
    }
    return { ok: response.ok, status: response.status, data, message };
  }

  // D70: every change saved here is announced to the book's other tabs on `BroadcastChannel('nassakh')` as
  // `{type: 'review', book, page}`: the book page refreshes its review drift, the manuscript its state. Where the
  // channel is missing, their focus and visibility checks cover it.
  const CHANNEL = 'nassakh';
  let channel = null;
  function announce(message) {
    try {
      if (!channel && typeof BroadcastChannel === 'function') {
        channel = new BroadcastChannel(CHANNEL);
        if (typeof channel.unref === 'function') channel.unref(); // Node (the tests): never hold the process open
      }
      if (channel) channel.postMessage(message);
    } catch (_) { /* no channel (an old browser, a sandbox) */ }
  }

  const fill = (template, id) => String(template || '').replace('__id__', String(id));
  const plainToken = (word) => ({ t: word, alt: null, tess: null, conf: 'high', digit: isDigits(word), bbox: null, res: 'typed' });
  const splitWords = (text) => String(text || '').replace(/\s+/g, ' ').trim().split(' ').filter(Boolean);
  const isBox = (b) => Array.isArray(b) && b.length === 4;
  const unionBox = (a, b) => {
    const boxes = [a, b].filter(isBox);
    if (!boxes.length) return null;
    return [Math.min(...boxes.map((x) => x[0])), Math.min(...boxes.map((x) => x[1])), Math.max(...boxes.map((x) => x[2])), Math.max(...boxes.map((x) => x[3]))];
  };

  // Keyboard map (PHASE3_SPEC §4, PHASE7_SPEC §3.15 D69), RTL: ArrowLeft = next page. Pure, so the tests can
  // exercise it. Keys are read through `NassakhKeys` (keys.js): letters by their place, so the Arabic layout's
  // «ش» on the A key is A; digits in any script; nothing while an IME composes.
  // ctx: inField (typing in an input), inPop (the target is inside the word menu), inFlow (Tab may be taken
  // over), focused (a word is focused and the page is editable), open (the word menu is open), optionKeys
  // (digit hints of the focused word's readings).
  // Two modes, decided by the word menu:
  //   word mode (the menu open on an editable word): 1–9 choose reading n, a digit beyond the readings and any
  //     other printable character (Latin or Arabic, ؟ and − too) start the correction with that character;
  //   page mode (the menu closed): A approve, E edit the line, N the next page to review, ? the sheet, + − 0
  //     zoom, Space opens the focused word's menu, a digit still chooses a reading of a focused word; other
  //     letters do nothing, so no correction starts by accident.
  // In both: ← → PageDown PageUp Home End turn pages, Enter accepts, Tab / ⇧Tab move, ⌥← / ⌥→ merge the
  // focused word with the next / previous one (D31, RTL: the next word is on the left), ⌫ deletes it, ⌘Z undoes,
  // ⌘↵ approves the page from anywhere (the correction field of the word menu too).
  function keyAction(ev, ctx) {
    const K = window.NassakhKeys;
    if (K.composing(ev)) return null;
    const k = ev.key;
    const c = ctx || {};
    if (k === 'Escape') return 'close';
    if (k === 'Enter' && K.mod(ev) && !ev.altKey && !ev.shiftKey) return !c.inField || c.inPop ? 'approve' : null;
    if (c.inField) return null; // an input keeps its own keys, including the native ⌘Z of its draft
    if (K.mod(ev)) return K.letter(ev, 'mod') === 'z' ? 'undo' : null;
    if (k === 'Tab') return c.inFlow ? (ev.shiftKey ? 'prev' : 'next') : null;
    if (k === 'Enter') return ev.altKey ? 'insert' : (c.focused ? 'accept' : null);
    if (ev.altKey) {
      if (c.focused && k === 'ArrowLeft') return 'mergeNext';
      if (c.focused && k === 'ArrowRight') return 'mergePrev';
      return null;
    }
    if ((k === 'Backspace' || k === 'Delete') && c.focused) return 'deleteWord';
    if (k === 'ArrowLeft' || k === 'PageDown') return 'nextPage';
    if (k === 'ArrowRight' || k === 'PageUp') return 'prevPage';
    if (k === 'Home') return 'firstPage';
    if (k === 'End') return 'lastPage';
    const n = K.digit(ev);
    const reading = n !== null && n >= 1 && c.focused && (c.optionKeys || []).includes(String(n));
    if (reading) return 'choose' + n;
    if (c.focused && c.open) {
      // word mode: what the key types starts the correction (a space never does)
      const ch = K.printable(ev);
      return ch && ch.trim() ? 'type' : null;
    }
    if ((k === ' ' || ev.code === 'Space') && !ev.shiftKey) return c.focused ? 'openWord' : null;
    if (K.is(ev, '?')) return 'sheet';
    if (K.plus(ev)) return 'zoomIn';
    if (K.minus(ev)) return 'zoomOut';
    if (n === 0) return 'zoomReset';
    const letter = K.letter(ev);
    if (letter === 'a') return 'approve';
    if (letter === 'e') return 'edit';
    if (letter === 'n') return 'nextReview';
    return null;
  }

  window.NassakhReview = Object.assign(window.NassakhReview || {}, { keyAction, api });

  document.addEventListener('alpine:init', () => {
    // Shared with the top bar: a plain snapshot (`bar`) and an action dispatcher (`act`).
    Alpine.store('review', { bar: null, act: null });

    Alpine.data('reviewBar', () => ({
      get b() { return this.$store.review.bar; },
      act(name, arg) { const fn = this.$store.review.act; if (fn) fn(name, arg); },
    }));

    Alpine.data('reviewScreen', (config) => ({
      // ---- payload
      page: {},
      book: {},
      image: {},
      regions: [],
      lines: [],
      counts: { low_total: 0, unresolved: 0, resolved: 0 },
      labels: {},
      nav: {},
      urls: {},
      canEdit: false,
      // ---- words
      focus: null,        // { lineId, index } focused word (keyboard + popover)
      hot: null,          // { lineId, index } hovered word, from either side
      hotLine: null,      // hovered line id
      flashing: {},       // "lineId-index" → true while the resolve animation plays
      pop: { open: false, style: '', typed: '', typing: false, above: false, more: false, moreSide: 'left', moreStyle: '' },
      roles: ROLES,
      // ---- lines
      edit: null,         // { lineId, text }
      insert: null,       // { afterId, text }
      menuFor: null,      // line id whose «…» menu is open
      // ---- saving
      save: { state: 'idle', pending: 0, failed: [] }, // failed: [{ message, retry }] in order, until retried
      queue: Promise.resolve(),
      actionSeq: 0,       // counts requests; only the newest one may offer an undo toast
      gen: 0,             // page generation: a page swap bumps it so late responses of the old page are dropped
      undoToast: null,
      // ---- page
      shown: 0,           // animated resolved counter
      dialog: { open: false, count: 0 },
      sheetOpen: false,
      stamp: false,
      approving: false,
      slide: '',          // '' | 'out' | 'in' | 'out-back' | 'in-back' (turning to an earlier page)
      loading: false,
      entering: false,
      pulsing: false,
      film: { items: [], state: 'idle' },
      // ---- scan viewer
      tab: 'display',
      swapped: false,
      zoom: { scale: 1, x: 0, y: 0 },
      fit: 'height',
      pane: { w: 0, h: 0 },
      gliding: false,
      drag: null,
      dragMoved: false,
      pinch: null,
      pointers: null,
      reduced: false,
      decodeEl: null,

      init() {
        this.reduced = reducedMotion();
        this.swapped = storage.get(SWAP_KEY, '0') === '1';
        this.fit = storage.get(FIT_KEY, 'height') === 'width' ? 'width' : 'height';
        this.apply(config);
        this.shown = this.counts.resolved;
        this.syncBar();
        if (!hasDOM) return;
        if (this.$watch) {
          ['counts', 'save', 'page', 'shown', 'loading', 'canEdit'].forEach((key) => this.$watch(key, () => this.syncBar()));
          // one toast at a time (DESIGN.md): a shared toast («تم النسخ», an error) takes the place of the undo offer
          this.$watch('$store.toast.visible', (visible) => { if (visible) this.dismissUndo(); });
        }
        this.tick(() => { this.measure(); this.observe(); this.enter(); this.mountDecode(); });
        setTimeout(() => this.loadFilm(), 60);
        if (this.isPending) this.schedulePoll();
      },

      destroy() {
        if (this.ro) this.ro.disconnect();
        clearTimeout(this.pollTimer);
        clearTimeout(this.saveTimer);
        clearTimeout(this.undoTimer);
        this.unmountDecode();
        try { const store = Alpine.store('review'); store.bar = null; store.act = null; } catch (_) { /* no store */ }
      },

      tick(fn) { if (hasDOM && this.$nextTick) this.$nextTick(fn); },

      // ------------------------------------------------------------ payload
      apply(data) {
        if (!data) return;
        if (data.page) this.page = data.page;
        if (data.book) this.book = data.book;
        if (data.image) this.image = data.image;
        if (data.regions) this.regions = data.regions;
        if (data.lines) this.lines = data.lines.slice().sort((a, b) => a.order - b.order);
        if (data.labels) this.labels = data.labels;
        if (data.nav) this.nav = data.nav;
        if (data.urls) this.urls = data.urls;
        if (data.can_edit !== undefined) this.canEdit = Boolean(data.can_edit);
        if (data.counts) this.setCounts(data.counts); else this.recount();
        this.syncBar();
      },

      setCounts(c) {
        const lowTotal = Number(c.low_total) || 0;
        const unresolved = Number(c.unresolved) || 0;
        const resolved = c.resolved != null ? Number(c.resolved) || 0 : Math.max(0, lowTotal - unresolved);
        this.counts = { low_total: lowTotal, unresolved, resolved };
        this.tickCounter();
        this.syncBar();
      },

      // Local recount from the lines (optimistic updates); the server's counts replace it afterwards.
      recount() {
        let low = 0; let open = 0;
        this.lines.forEach((line) => {
          let lineLow = 0;
          (line.tokens || []).forEach((tok) => {
            if (tok.conf === 'low') { low += 1; if (tok.res == null) { open += 1; lineLow += 1; } }
          });
          line.n_low = lineLow;
        });
        this.setCounts({ low_total: low, unresolved: open });
      },

      // API `counts` = { line_n_low, page_unresolved, page_low_total, book_unresolved_total }.
      applyApiCounts(c, line) {
        if (!c) return;
        if (line && c.line_n_low != null) line.n_low = c.line_n_low;
        this.setCounts({ low_total: c.page_low_total, unresolved: c.page_unresolved });
        if (c.book_unresolved_total != null) this.book.unresolved_total = c.book_unresolved_total;
      },

      // The line's server version (`v` in its payload) as it is now; sent with whole-line actions.
      versionOf(line) {
        const current = line ? this.lineById(line.id) : null;
        const v = (current && current.v) || (line && line.v);
        return v === undefined || v === null ? undefined : v;
      },
      replaceLine(line) {
        const i = this.lines.findIndex((l) => l.id === line.id);
        if (i >= 0) this.lines.splice(i, 1, line); else { this.lines.push(line); this.lines.sort((a, b) => a.order - b.order); }
      },

      syncBar() {
        try {
          const store = Alpine.store('review');
          if (!store) return;
          store.bar = {
            number: this.page.number,
            total: this.book.total_pages,
            shown: this.shown,
            resolved: this.counts.resolved,
            lowTotal: this.counts.low_total,
            unresolved: this.counts.unresolved,
            pct: this.pct,
            save: this.save.state,
            failed: this.save.failed.length,
            reviewed: this.isReviewed,
            canEdit: this.canEdit,
            canApprove: this.editable && !this.isReviewed,
            pending: this.isPending,
            error: this.isError,
            statusLabel: this.page.status_label || '',
            dashboardUrl: this.nav.dashboard_url || '',
            hasPrev: Boolean(this.nav.prev_url),
            hasNext: Boolean(this.nav.next_url),
            loading: this.loading,
          };
          store.act = (name, arg) => { if (typeof this[name] === 'function') this[name](arg); };
        } catch (_) { /* no Alpine store (tests) */ }
      },

      // ------------------------------------------------------------ derived
      get isError() { return this.page.status === 'error'; },
      get isPending() { return !this.isError && this.page.text_state !== 'final'; },
      get ready() { return !this.isError && !this.isPending; },
      get isReviewed() { return this.page.status === 'reviewed' || this.page.status === 'assembled' || Boolean(this.page.is_reviewed); },
      get editable() { return this.canEdit && this.ready && !this.loading && !this.isReviewed; },
      get pct() { return this.counts.low_total ? Math.round((100 * this.counts.resolved) / this.counts.low_total) : 100; },
      get currentLineId() {
        if (this.edit) return this.edit.lineId;
        if (this.focus) return this.focus.lineId;
        if (this.hot) return this.hot.lineId;
        return this.hotLine;
      },
      get currentLine() { return this.lineById(this.currentLineId); },
      get focused() {
        const line = this.lineById(this.focus && this.focus.lineId);
        return (line && line.tokens[this.focus.index]) || null;
      },
      get errorHeadline() { return String(this.page.error || 'تعطّلت معالجة هذه الصفحة').split('\n')[0].trim(); },
      get zoomLabel() { return Math.round(this.zoom.scale * 100) + '%'; },
      get hasScan() { return Boolean(this.image.scan_url); },
      get thumbAspect() {
        const ratio = this.image.width && this.image.height ? this.image.width / this.image.height : 0.68;
        return `aspect-ratio: ${Math.round(ratio * 1000) / 1000};`;
      },

      lineById(id) { return id == null ? null : this.lines.find((l) => l.id === id) || null; },
      same(a, b) { return Boolean(a && b && a.lineId === b.lineId && a.index === b.index); },
      isUnresolved(tok) { return Boolean(tok) && tok.conf === 'low' && tok.res == null; },
      tokId(lineId, i) { return `rv-tok-${lineId}-${i}`; },

      // ------------------------------------------------------------ words: navigation
      unresolvedRefs() {
        const out = [];
        this.lines.forEach((line, li) => (line.tokens || []).forEach((tok, i) => {
          if (this.isUnresolved(tok)) out.push({ lineId: line.id, index: i, pos: li * 10000 + i });
        }));
        return out;
      },

      posOf(ref) {
        const li = this.lines.findIndex((l) => l.id === ref.lineId);
        return li < 0 ? -1 : li * 10000 + ref.index;
      },

      // Next (dir 1) / previous (dir -1) unresolved word in reading order, wrapping around.
      nextUnresolved(dir, from) {
        const refs = this.unresolvedRefs();
        if (!refs.length) return null;
        const start = from === undefined ? this.focus : from;
        if (!start) return dir > 0 ? refs[0] : refs[refs.length - 1];
        const cur = this.posOf(start);
        if (dir > 0) return refs.find((r) => r.pos > cur) || refs[0];
        for (let i = refs.length - 1; i >= 0; i -= 1) if (refs[i].pos < cur) return refs[i];
        return refs[refs.length - 1];
      },

      move(dir) {
        const ref = this.nextUnresolved(dir);
        if (!ref) {
          this.toast(this.counts.low_total ? 'حُسمت كل الكلمات في هذه الصفحة' : 'لا علامات في هذه الصفحة؛ اقرأ الأسطر مع الصورة ثم اعتمدها.');
          return false;
        }
        this.focusWord(ref, { open: true });
        return true;
      },

      focusWord(ref, options) {
        const opts = options || {};
        this.focus = { lineId: ref.lineId, index: ref.index };
        this.menuFor = null;
        this.pop.typed = '';
        this.pop.typing = false;
        const tok = this.focused;
        // any word opens the popover on an editable page (merge / delete, D31); read-only: uncertain words only
        this.pop.open = opts.open !== false && Boolean(tok) && (tok.conf === 'low' || this.editable);
        this.pop.more = false;
        // D32: correction is the main action; a click on a confident word opens it prefilled and selected
        const prefill = this.pop.open && opts.fromClick && tok && tok.conf !== 'low' && this.editable;
        if (prefill) { this.pop.typing = true; this.pop.typed = tok.t; }
        if (!hasDOM) return;
        this.tick(() => {
          const el = document.getElementById(this.tokId(ref.lineId, ref.index));
          if (el) {
            if (document.activeElement !== el) el.focus({ preventScroll: true });
            el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
            this.placePop(el);
          }
          if (prefill && this.$refs.typed) { this.$refs.typed.focus({ preventScroll: true }); this.$refs.typed.select(); }
          if (opts.pan !== false && tok && tok.bbox) this.panTo(tok.bbox);
        });
      },

      onTokFocus(line, i) {
        if (this.same(this.focus, { lineId: line.id, index: i })) return;
        const tok = line.tokens[i];
        if (tok && tok.conf === 'low') this.focusWord({ lineId: line.id, index: i }, { open: true });
      },

      onTokClick(line, i) {
        const tok = line.tokens[i];
        if (tok && (tok.conf === 'low' || this.editable)) this.focusWord({ lineId: line.id, index: i }, { open: true, fromClick: true });
        else { this.focus = null; this.pop.open = false; this.hotLine = line.id; }
      },

      onBoxClick(line, i) {
        if (this.dragMoved) return;
        this.onTokClick(line, i);
      },

      // The part of the lines column the reader can see (the scroller clipped to the window), minus the margin.
      visibleArea(node) {
        const scroller = node && node.closest ? node.closest('.rv-lines-scroll') : null;
        const r = scroller && scroller.getBoundingClientRect ? scroller.getBoundingClientRect() : null;
        const W = window.innerWidth || 0;
        const H = window.innerHeight || 0;
        return {
          top: Math.max(r ? r.top : 0, 0) + POP_EDGE,
          bottom: Math.min(r ? r.bottom : H, H) - POP_EDGE,
          left: Math.max(r ? r.left : 0, 0) + POP_EDGE,
          right: Math.min(r ? r.right : W, W) - POP_EDGE,
        };
      },

      // Open below the word, flip above when it would leave the visible column and there is more room
      // above; the popover's right edge sits on the word's right edge (RTL) and never crosses a side edge.
      placePop(el) {
        const host = this.$refs && this.$refs.stage;
        if (!el || !host || !el.getBoundingClientRect) return;
        const tok = el.getBoundingClientRect();
        const ref = host.getBoundingClientRect();
        const view = this.visibleArea(host);
        const node = this.$refs.pop;
        const w = node && node.offsetWidth ? node.offsetWidth : POP_W;
        const h = node && node.offsetHeight ? node.offsetHeight : POP_H_GUESS;
        let top = tok.bottom + POP_GAP;
        let above = false;
        if (top + h > view.bottom) {
          const roomAbove = tok.top - POP_GAP - view.top;
          const roomBelow = view.bottom - top;
          if (roomAbove >= h || roomAbove > roomBelow) { top = Math.max(view.top, tok.top - POP_GAP - h); above = true; }
          else top = Math.max(view.top, view.bottom - h);
        }
        let right = Math.min(tok.right, view.right);
        if (right - w < view.left) right = Math.min(view.right, view.left + w);
        this.pop.above = above;
        this.pop.style = `top:${Math.round(top - ref.top)}px; right:${Math.round(ref.right - right)}px;`;
      },

      repositionPop() {
        if (!hasDOM || !this.pop.open || !this.focus) return;
        this.placePop(document.getElementById(this.tokId(this.focus.lineId, this.focus.index)));
        if (this.pop.more) this.placeMore();
      },

      // A click anywhere outside the popover closes it (clicks on words and scan boxes stop propagation and
      // open their own popover instead); the release of a scan drag does not count.
      onPopOutside() {
        if (!this.pop.open) return;
        if (this.dragMoved) { this.dragMoved = false; return; }
        this.closePop();
        this.focus = null;
      },

      closePop() { this.pop.open = false; this.pop.typing = false; this.pop.typed = ''; this.pop.more = false; },

      // ---- «إجراءات أخرى»: a second-level menu (hover, click or ArrowLeft) with merge and delete, kept
      // away from the correction so a destructive action is never one mis-click away (D32).
      openMore(focusFirst) {
        clearTimeout(this.moreTimer);
        if (!this.pop.more) this.pop.more = true;
        this.tick(() => {
          this.placeMore();
          if (focusFirst && this.$refs.more) {
            const first = this.$refs.more.querySelector('button:not([disabled])');
            if (first) first.focus();
          }
        });
      },

      closeMoreSoon() {
        clearTimeout(this.moreTimer);
        this.moreTimer = setTimeout(() => { this.pop.more = false; }, MORE_CLOSE_MS);
      },

      closeMore(focusRow) {
        clearTimeout(this.moreTimer);
        this.pop.more = false;
        const row = this.$refs && this.$refs.moreRow;
        if (focusRow && row && row.focus) row.focus();
      },

      toggleMore() { if (this.pop.more) this.closeMore(); else this.openMore(); },

      // Beside the popover on the side with room (RTL: the left first), level with its row and kept inside
      // the visible column; folded into the popover when neither side has room (a narrow column).
      placeMore() {
        const pop = this.$refs && this.$refs.pop;
        const row = this.$refs && this.$refs.moreRow;
        if (!pop || !row || !pop.getBoundingClientRect) return;
        const fly = this.$refs.more;
        const p = pop.getBoundingClientRect();
        const view = this.visibleArea(pop);
        const w = fly && fly.offsetWidth ? fly.offsetWidth : MORE_W;
        const h = fly && fly.offsetHeight ? fly.offsetHeight : 132;
        const roomLeft = p.left - view.left;
        const roomRight = view.right - p.right;
        let side = 'left';
        if (roomLeft < w + 6) side = roomRight >= w + 6 ? 'right' : 'below';
        let top = (row.offsetTop || 0) - 6;
        if (p.top + top + h > view.bottom) top -= p.top + top + h - view.bottom;
        if (p.top + top < view.top) top = view.top - p.top;
        this.pop.moreSide = side;
        this.pop.moreStyle = side === 'below' ? '' : `top:${Math.round(top)}px;`;
      },

      moveInMore(ev, dir) {
        const fly = this.$refs && this.$refs.more;
        const items = fly ? Array.from(fly.querySelectorAll('button:not([disabled])')) : [];
        if (!items.length) return;
        const i = items.indexOf(ev && ev.target);
        const next = items[(i + dir + items.length) % items.length];
        if (next) next.focus();
      },

      // ------------------------------------------------------------ words: readings
      options(token) {
        const tok = token === undefined ? this.focused : token;
        if (!tok || tok.conf !== 'low') return [];
        const rows = [];
        // `orig` (sent once a resolution changed `t`) keeps the primary model's reading, so all three
        // readings stay offered after a word was resolved; `current` marks the one now in the text.
        const primary = tok.orig || tok.t;
        // a number Kraken read (D50) is its one reading: confirm it, or type the true number; where Qari
        // wrote a letter for it (D51), Qari's letter is the second reading (it may be a real letter)
        const kraken = tok.src === 'kraken';
        const label = kraken ? 'Kraken' : this.labels.primary || 'النموذج الأول';
        const second = kraken ? this.labels.primary || 'النموذج الأول' : this.labels.secondary || 'النموذج الثاني';
        rows.push({ choice: 'primary', value: primary, label, current: primary === tok.t });
        if (tok.alt && tok.alt !== primary) rows.push({ choice: 'secondary', value: tok.alt, label: second, current: tok.alt === tok.t });
        if (tok.tess && tok.tess !== primary && tok.tess !== tok.alt) rows.push({ choice: 'tess', value: tok.tess, label: 'Tesseract', current: tok.tess === tok.t });
        rows.forEach((row, i) => { row.key = String(i + 1); });
        return rows;
      },

      chooseNth(n) {
        const row = this.options()[n - 1];
        if (row) return this.choose(row.choice);
        return Promise.resolve(false);
      },

      // Close the popover and move on to the next unresolved word; false when none is left (focus cleared).
      advance(ref) {
        this.closePop();
        const next = this.nextUnresolved(1, ref);
        if (next) this.focusWord(next, { open: true }); else this.focus = null;
        return Boolean(next);
      },

      // Enter: confirm the reading now in the text. An unresolved word takes the primary reading (PHASE3_SPEC
      // §4); a word the chooser picked (D26) records the reviewer's confirmation of that reading; a word the
      // reviewer already resolved, or a confident one, needs no request: the popover closes and focus moves on.
      accept() {
        const tok = this.focused;
        if (!tok || !this.editable) return Promise.resolve(false);
        if (this.isUnresolved(tok)) return this.choose('primary');
        if (tok.res === 'chooser') {
          const cur = this.options().find((o) => o.current);
          if (cur) return this.choose(cur.choice);
        }
        this.advance(this.focus);
        return Promise.resolve(false);
      },

      // Resolve the focused word. Optimistic: the word lands, the counter ticks, focus moves on; the
      // POST follows and a failure restores the word and the counts.
      choose(choice, text) {
        const ref = this.focus;
        const line = this.lineById(ref && ref.lineId);
        const tok = line && line.tokens[ref.index];
        if (!tok || !this.editable) return Promise.resolve(false);
        let value = tok.orig || tok.t;
        if (choice === 'secondary') value = tok.alt;
        else if (choice === 'tess') value = tok.tess;
        else if (choice === 'typed') value = String(text || '').replace(/\s+/g, ' ').trim();
        if (!value) return Promise.resolve(false);
        if (choice === 'typed' && value === tok.t && !this.isUnresolved(tok)) { this.closePop(); return Promise.resolve(false); }
        // the reading already in the text, chosen again: a confirmation, nothing to record
        if (choice !== 'typed' && choice === tok.res && value === tok.t) { this.advance(ref); return Promise.resolve(false); }

        const before = Object.assign({}, tok);
        const beforeCounts = Object.assign({}, this.counts);
        const beforeNLow = line.n_low;
        const wasOpen = this.isUnresolved(tok);
        if (value !== tok.t && !tok.orig) tok.orig = tok.t;
        tok.t = value;
        tok.res = choice;
        if (wasOpen) {
          line.n_low = Math.max(0, (line.n_low || 0) - 1);
          this.setCounts({ low_total: this.counts.low_total, unresolved: this.counts.unresolved - 1, resolved: this.counts.resolved + 1 });
        }
        this.flash(ref);
        if (!this.advance(ref) && wasOpen && this.counts.unresolved === 0) this.toast('حُسمت كل الكلمات · اعتمد الصفحة بـ A');

        const body = { index: ref.index, choice, t: before.t }; // t: the word as seen, so a stale tab gets a 409
        if (choice === 'typed') body.text = value;
        return this.request(() => api(fill(this.urls.resolve, line.id), { method: 'POST', body }), {
          apply: (data) => {
            if (data.line) this.replaceLine(data.line);
            this.applyApiCounts(data.counts, this.lineById(line.id));
            if (data.page) Object.assign(this.page, data.page);
          },
          rollback: () => {
            const l = this.lineById(line.id);
            if (l && l.tokens[ref.index]) { const t = l.tokens[ref.index]; if (!('orig' in before)) delete t.orig; Object.assign(t, before); l.n_low = beforeNLow; }
            this.setCounts(beforeCounts);
          },
          retry: () => { this.focus = { lineId: ref.lineId, index: ref.index }; return this.choose(choice, text); },
        });
      },

      // ------------------------------------------------------------ words: merge / delete (D31)
      // What the popover may offer for the focused word: merge with the next / previous word on its line.
      wordActions() {
        const ref = this.focus;
        const line = this.lineById(ref && ref.lineId);
        const n = line ? (line.tokens || []).length : 0;
        if (!line || !this.editable || !n) return { next: false, prev: false, del: false };
        return { next: ref.index + 1 < n, prev: ref.index > 0, del: true };
      },

      // The word a merge would produce («هير» + «ودوت» → «هيرودوت»); '' when there is nothing to merge with.
      mergePreview(dir) {
        const ref = this.focus;
        const line = this.lineById(ref && ref.lineId);
        const tokens = line ? line.tokens || [] : [];
        const k = dir < 0 ? (ref ? ref.index - 1 : -1) : (ref ? ref.index : -1);
        if (k < 0 || k + 1 >= tokens.length) return '';
        return tokens[k].t + tokens[k + 1].t;
      },

      // Join the focused word with the next (dir 1, reading order: to its left) or previous (dir -1) word.
      // Optimistic: the merged word lands at once with the union of both boxes; a failure restores both.
      mergeWord(dir) {
        const ref = this.focus;
        const line = this.lineById(ref && ref.lineId);
        if (!line || !this.editable) return Promise.resolve(false);
        const tokens = line.tokens || [];
        const k = dir < 0 ? ref.index - 1 : ref.index;
        if (k < 0 || k + 1 >= tokens.length) return Promise.resolve(false);
        const before = tokens.map((t) => Object.assign({}, t));
        const beforeText = line.text;
        const beforeCounts = Object.assign({}, this.counts);
        const merged = plainToken(tokens[k].t + tokens[k + 1].t);
        merged.bbox = unionBox(tokens[k].bbox, tokens[k + 1].bbox);
        tokens.splice(k, 2, merged);
        line.text = tokens.map((t) => t.t).join(' ');
        this.recount();
        this.closePop();
        const at = { lineId: line.id, index: k };
        this.focusWord(at, { open: false });
        this.flash(at);
        const seen = { index: k, t: before[k].t, t_next: before[k + 1].t };
        return this.request(() => api(fill(this.urls.merge, line.id), { method: 'POST', body: seen }), {
          apply: (data) => {
            if (data.line) this.replaceLine(data.line);
            this.applyApiCounts(data.counts, this.lineById(line.id));
          },
          rollback: () => {
            const l = this.lineById(line.id);
            if (l) { l.tokens = before; l.text = beforeText; }
            this.setCounts(beforeCounts);
            this.recount();
          },
          retry: () => { this.focus = { lineId: ref.lineId, index: ref.index }; return this.mergeWord(dir); },
        });
      },

      // Remove the focused word (a stray letter or number); the line's only word removes the line.
      deleteWord() {
        const ref = this.focus;
        const line = this.lineById(ref && ref.lineId);
        if (!line || !this.editable) return Promise.resolve(false);
        const tokens = line.tokens || [];
        if (!tokens[ref.index]) return Promise.resolve(false);
        if (tokens.length === 1) { this.closePop(); this.focus = null; return this.removeLine(line); }
        const before = tokens.map((t) => Object.assign({}, t));
        const beforeText = line.text;
        const beforeCounts = Object.assign({}, this.counts);
        tokens.splice(ref.index, 1);
        line.text = tokens.map((t) => t.t).join(' ');
        this.recount();
        this.closePop();
        this.focusWord({ lineId: line.id, index: Math.min(ref.index, tokens.length - 1) }, { open: false });
        const seen = { index: ref.index, t: before[ref.index].t };
        return this.request(() => api(fill(this.urls.delete_word, line.id), { method: 'POST', body: seen }), {
          apply: (data, seq) => {
            if (data.line) this.replaceLine(data.line);
            this.applyApiCounts(data.counts, this.lineById(line.id));
            this.showUndoToast('حُذفت الكلمة', seq);
          },
          rollback: () => {
            const l = this.lineById(line.id);
            if (l) { l.tokens = before; l.text = beforeText; }
            this.setCounts(beforeCounts);
            this.recount();
          },
          retry: () => { this.focus = { lineId: ref.lineId, index: ref.index }; return this.deleteWord(); },
        });
      },

      startTyping(ch) {
        if (!this.focused || !this.editable) return;
        this.pop.open = true;
        this.pop.typing = true;
        this.pop.typed = ch || '';
        this.tick(() => {
          const input = this.$refs.typed;
          if (input) { input.focus(); try { input.setSelectionRange(input.value.length, input.value.length); } catch (_) { /* fine */ } }
        });
      },

      submitTyped() {
        const value = this.pop.typed.trim();
        if (!value) return Promise.resolve(false);
        return this.choose('typed', value);
      },

      // Tab in the correction input: a draft that changes the word (or confirms an unresolved one) is saved,
      // and choose() moves on; an untouched prefill just moves to the next / previous unresolved word.
      onTypedTab(shift) {
        const value = this.pop.typed.trim();
        const tok = this.focused;
        if (value && tok && (value !== tok.t || this.isUnresolved(tok))) return this.submitTyped();
        return this.move(shift ? -1 : 1);
      },

      flash(ref) {
        const key = `${ref.lineId}-${ref.index}`;
        this.flashing = Object.assign({}, this.flashing, { [key]: true });
        setTimeout(() => { const next = Object.assign({}, this.flashing); delete next[key]; this.flashing = next; }, FLASH_MS);
      },

      tokClass(line, tok, i) {
        const ref = { lineId: line.id, index: i };
        const low = tok.conf === 'low';
        return {
          'is-low': low,
          'is-open': low && tok.res == null,
          'is-resolved': low && tok.res != null,
          'is-focus': this.same(this.focus, ref),
          'is-hot': this.same(this.hot, ref),
          'is-flash': Boolean(this.flashing[`${line.id}-${i}`]),
          'is-digit': Boolean(tok.digit),
        };
      },

      boxClass(line, tok, i) {
        const ref = { lineId: line.id, index: i };
        return {
          'is-open': this.isUnresolved(tok),
          'is-hot': this.same(this.hot, ref) || this.same(this.focus, ref),
          'is-weak': tok.bq === 'weak', // the alignment is unsure of this box: drawn dashed
        };
      },

      // D32: what the line is in the book (body text, main heading, subheading); undo restores it.
      setRole(line, role) {
        this.menuFor = null;
        if (!line || !this.editable || (line.role || 'body') === role) return Promise.resolve(false);
        const before = line.role || 'body';
        line.role = role;
        return this.request(() => api(fill(this.urls.role, line.id), { method: 'POST', body: { role, v: this.versionOf(line) } }), {
          apply: (data) => { if (data.line) this.replaceLine(data.line); },
          rollback: () => { const l = this.lineById(line.id); if (l) l.role = before; },
          retry: () => this.setRole(this.lineById(line.id), role),
        });
      },

      roleLabel(line) {
        const role = ROLES.find((r) => r.value === (line && line.role));
        return role && role.value !== 'body' ? role.label : '';
      },

      lineClass(line) {
        return {
          'is-heading': line.role === 'heading',
          'is-subheading': line.role === 'subheading',
          'is-current': this.currentLineId === line.id,
          'is-reviewed': Boolean(line.is_reviewed),
          'is-manual': Boolean(line.is_manual),
          'is-editing': Boolean(this.edit && this.edit.lineId === line.id),
          'is-footnote': line.region_kind === 'footnote',
        };
      },

      showGroup(line, li) {
        const prev = li > 0 ? this.lines[li - 1] : null;
        return line.region_kind === 'footnote' && (!prev || prev.region_kind !== 'footnote');
      },

      // ------------------------------------------------------------ lines: edit / insert / delete
      startEdit(target) {
        const line = target || this.currentLine;
        if (!line || !this.editable) return;
        this.closePop();
        this.insert = null;
        this.menuFor = null;
        this.edit = { lineId: line.id, text: line.text || (line.tokens || []).map((t) => t.t).join(' ') };
        this.tick(() => this.focusEditor());
      },

      cancelEdit() { this.edit = null; this.insert = null; },

      // Only one line editor exists at a time; it is found by class because the three templates that render
      // one (edit, insert after a line, insert first) would otherwise fight over a single shared x-ref.
      focusEditor() {
        const root = this.$root && this.$root.querySelector ? this.$root : (hasDOM ? document : null);
        const el = root && root.querySelector ? root.querySelector('textarea.rv-editor') : null;
        if (!el) return;
        this.autosize(el);
        el.focus();
        try { el.setSelectionRange(el.value.length, el.value.length); } catch (_) { /* fine */ }
      },

      autosize(el) {
        if (!el || !el.style) return;
        el.style.height = 'auto';
        el.style.height = `${el.scrollHeight}px`;
      },

      saveEdit() {
        const draft = this.edit;
        if (!draft) return Promise.resolve(false);
        const line = this.lineById(draft.lineId);
        const text = splitWords(draft.text).join(' ');
        this.edit = null;
        if (!line) return Promise.resolve(false);
        if (!text) { this.toast('السطر فارغ؛ احذفه من قائمة السطر بدلًا من ذلك'); return Promise.resolve(false); }
        if (text === (line.text || '')) return Promise.resolve(false);

        const before = JSON.parse(JSON.stringify(line));
        const beforeCounts = Object.assign({}, this.counts);
        const words = splitWords(text);
        line.text = text;
        line.tokens = words.map((w, i) => (before.tokens[i] && before.tokens[i].t === w ? before.tokens[i] : plainToken(w)));
        this.recount();
        this.hotLine = line.id;

        return this.request(() => api(fill(this.urls.edit, line.id), { method: 'POST', body: { text, v: this.versionOf(line) } }), {
          apply: (data) => {
            if (data.line) this.replaceLine(data.line);
            this.applyApiCounts(data.counts, this.lineById(line.id));
          },
          rollback: () => {
            const i = this.lines.findIndex((l) => l.id === line.id);
            if (i >= 0) this.lines.splice(i, 1, before);
            this.setCounts(beforeCounts);
          },
          retry: () => { this.edit = { lineId: line.id, text }; return this.saveEdit(); },
        });
      },

      startInsert(target) {
        if (!this.editable) return;
        const anchor = target || this.currentLine || this.lines[this.lines.length - 1] || null;
        this.closePop();
        this.edit = null;
        this.menuFor = null;
        this.insert = { afterId: anchor ? anchor.id : null, text: '' };
        this.tick(() => this.focusEditor());
      },

      saveInsert() {
        const draft = this.insert;
        if (!draft) return Promise.resolve(false);
        const text = splitWords(draft.text).join(' ');
        this.insert = null;
        if (!text) return Promise.resolve(false);
        const idx = this.lines.findIndex((l) => l.id === draft.afterId);
        const anchor = idx >= 0 ? this.lines[idx] : null;
        const temp = {
          id: `tmp-${Date.now()}`, order: anchor ? anchor.order + 1 : 0,
          region_id: anchor ? anchor.region_id : null, region_kind: anchor ? anchor.region_kind : 'body',
          bbox: null, text, ocr_text: '', is_manual: true, is_reviewed: false, n_low: 0,
          tokens: splitWords(text).map(plainToken),
        };
        this.lines.splice(idx + 1, 0, temp);
        this.renumber();

        return this.request(() => api(this.urls.insert, { method: 'POST', body: { after: draft.afterId, text } }), {
          apply: (data) => {
            const i = this.lines.findIndex((l) => l.id === temp.id);
            if (data.line) { if (i >= 0) this.lines.splice(i, 1, data.line); else this.lines.push(data.line); }
            else if (i >= 0) this.lines.splice(i, 1);
            if (Array.isArray(data.lines)) this.reorder(data.lines);
            this.applyApiCounts(data.counts);
          },
          rollback: () => {
            const i = this.lines.findIndex((l) => l.id === temp.id);
            if (i >= 0) this.lines.splice(i, 1);
            this.renumber();
          },
          retry: () => { this.insert = { afterId: draft.afterId, text }; return this.saveInsert(); },
        });
      },

      renumber() { this.lines.forEach((line, i) => { line.order = i; }); },

      reorder(list) {
        const orders = new Map(list.map((x) => [x.id, x.order]));
        this.lines.forEach((line) => { if (orders.has(line.id)) line.order = orders.get(line.id); });
        this.lines.sort((a, b) => a.order - b.order);
      },

      removeLine(line) {
        if (!line || !this.editable) return Promise.resolve(false);
        const i = this.lines.findIndex((l) => l.id === line.id);
        if (i < 0) return Promise.resolve(false);
        const snapshot = this.lines[i];
        const beforeCounts = Object.assign({}, this.counts);
        this.menuFor = null;
        this.lines.splice(i, 1);
        this.renumber();
        this.recount();
        if (this.focus && this.focus.lineId === line.id) { this.focus = null; this.closePop(); }
        if (this.hotLine === line.id) this.hotLine = null;

        return this.request(() => api(fill(this.urls.delete, line.id), { method: 'POST', body: { v: this.versionOf(line) } }), {
          apply: (data, seq) => { this.applyApiCounts(data.counts); this.showUndoToast('حُذف السطر', seq); },
          rollback: () => {
            this.lines.splice(Math.min(i, this.lines.length), 0, snapshot);
            this.renumber();
            this.setCounts(beforeCounts);
          },
          retry: () => this.removeLine(snapshot),
        });
      },

      // Offer «تراجع» for the action that just landed. `seq` is that request's number: when a later action has
      // been sent since, the newest revision is no longer this one and the offer would undo the wrong thing.
      showUndoToast(message, seq) {
        if (seq !== undefined && seq !== this.actionSeq) return;
        try { const shared = Alpine.store('toast'); if (shared && shared.hide) shared.hide(); } catch (_) { /* no store */ }
        clearTimeout(this.undoTimer);
        this.undoToast = { message };
        this.undoTimer = setTimeout(() => { this.undoToast = null; }, UNDO_TOAST_MS);
      },

      dismissUndo() {
        clearTimeout(this.undoTimer);
        if (this.undoToast) this.undoToast = null;
      },

      // Server-side undo (LineRevision): the whole payload comes back and the screen re-renders from it.
      undo() {
        if (!this.canEdit || this.loading) return Promise.resolve(false);
        this.dismissUndo();
        this.edit = null;
        this.insert = null;
        this.closePop();
        return this.request(() => api(this.urls.undo, { method: 'POST' }), {
          apply: (payload) => { this.focus = null; this.apply(payload); this.pulse(); },
          soft: true,
        });
      },

      // ------------------------------------------------------------ saving
      // Runs `send` after the previous request (actions stay in order). `apply(data)` on success,
      // `rollback()` + save chip error with `retry` on failure; `onFail(res)` may claim a failure
      // (returns true), `soft` failures only toast (nothing to roll back).
      request(send, handlers) {
        const h = handlers || {};
        const gen = this.gen;
        const seq = ++this.actionSeq;
        const where = { type: 'review', book: this.book.id || this.page.book_id || null, page: this.page.number };
        this.dismissUndo(); // a newer action makes the undo offer stale
        this.save.pending += 1;
        this.save.state = 'saving';
        this.syncBar();
        const run = this.queue.then(() => send()).then((res) => {
          this.save.pending = Math.max(0, this.save.pending - 1);
          if (res.ok) announce(where); // saved on the server, whichever page is on screen now
          if (gen !== this.gen) {
            // the page this belonged to was swapped away meanwhile: nothing here to apply or roll back
            if (!res.ok) this.toast(res.message);
            if (!this.save.pending) this.settle('idle');
            return null;
          }
          if (res.ok) {
            if (h.apply) h.apply(res.data || {}, seq);
            if (!this.save.pending) this.settle('saved');
            return res.data || {};
          }
          if (h.onFail && h.onFail(res)) { if (!this.save.pending) this.settle('idle'); return null; }
          if (res.status === 409 && res.data && res.data.line) {
            // the line changed elsewhere (another tab): show it as it is now instead of offering a retry
            if (h.rollback) h.rollback();
            this.replaceLine(res.data.line);
            this.recount();
            if (!this.save.pending) this.settle('idle');
            this.toast(res.message || 'تغيّر هذا السطر في نافذة أخرى، فعُرض كما هو الآن.');
            return null;
          }
          if (h.rollback) h.rollback();
          if (h.soft) { this.settle('idle'); this.toast(res.message); return null; }
          if (h.retry) this.save.failed.push({ message: res.message, retry: h.retry });
          this.settle('error');
          this.toast(res.message);
          return null;
        });
        this.queue = run.catch(() => {});
        return run;
      },

      // The chip never says «محفوظ» while an earlier action is still unsaved: the failures outrank a later success.
      settle(state) {
        this.save.state = state !== 'saving' && this.save.failed.length ? 'error' : state;
        this.syncBar();
        clearTimeout(this.saveTimer);
        if (this.save.state === 'saved') {
          this.saveTimer = setTimeout(() => { if (this.save.state === 'saved') { this.save.state = 'idle'; this.syncBar(); } }, SAVED_MS);
        }
      },

      // Replay every failed action in its original order; one that fails again rejoins the list.
      retrySave() {
        const failed = this.save.failed.slice();
        this.save.failed = [];
        this.settle('idle');
        failed.forEach((f) => f.retry());
      },

      // ------------------------------------------------------------ approve / reopen / pages
      approve(force) {
        if (!this.canEdit || !this.ready || this.isReviewed || this.approving || this.loading) return Promise.resolve(false);
        if (!force && this.counts.unresolved > 0) {
          this.openDialog(this.counts.unresolved);
          return Promise.resolve(false);
        }
        this.dialog.open = false;
        this.approving = true;
        this.closePop();
        return this.request(() => api(this.urls.approve, { method: 'POST', body: { force: Boolean(force) } }), {
          apply: (data) => {
            this.approving = false;
            this.page.status = data.status || 'reviewed';
            this.page.is_reviewed = true;
            this.page.status_label = 'مُراجَعة';
            this.book.reviewed_pages = (Number(this.book.reviewed_pages) || 0) + 1;
            this.markFilm({ is_reviewed: true, status: this.page.status });
            this.syncBar();
            this.celebrate(data);
          },
          onFail: (res) => {
            this.approving = false;
            if (res.status === 409) {
              this.openDialog((res.data && res.data.unresolved) || this.counts.unresolved);
              return true;
            }
            return false;
          },
        });
      },

      openDialog(count) {
        this.dialog = { open: true, count: Number(count) || 0 };
        this.tick(() => { const el = this.$refs.dialogCancel; if (el) el.focus(); });
      },

      // Stamp (400 ms), then the next page needing review slides in from the reading direction.
      celebrate(data) {
        const d = data || {};
        this.stamp = true;
        const after = () => {
          this.stamp = false;
          const item = d.next_payload_url ? null : this.nextFromFilm();
          const payloadUrl = d.next_payload_url || this.payloadUrlFor(item && item.id);
          if (payloadUrl) { this.swapTo(payloadUrl, d.next_review_url, this.turnDir(item)); return; }
          this.toast('لا صفحات بانتظار المراجعة');
        };
        if (this.reduced || !hasDOM) after(); else setTimeout(after, 800);
      },

      reopen() {
        if (!this.canEdit || !this.isReviewed || this.loading) return Promise.resolve(false);
        return this.request(() => api(this.urls.reopen, { method: 'POST' }), {
          apply: (data) => {
            // The API answers the full review payload; the local patch is the fallback.
            if (data && data.page) this.apply(data);
            else {
              this.page.status = 'ocr_done';
              this.page.is_reviewed = false;
              this.page.status_label = 'تم التعرّف';
              this.book.reviewed_pages = Math.max(0, (Number(this.book.reviewed_pages) || 0) - 1);
            }
            this.markFilm({ is_reviewed: false, status: 'ocr_done' });
            this.syncBar();
          },
        });
      },

      markFilm(patch) {
        const item = this.film.items.find((p) => p.number === this.page.number);
        if (item) Object.assign(item, patch, { n_unresolved: this.counts.unresolved });
      },

      // The next page still to review: the first one after this page, else the first open page of the book.
      nextFromFilm() {
        const items = this.film.items || [];
        const open = (p) => p.id && p.status === 'ocr_done' && !p.is_reviewed && p.number !== this.page.number;
        return items.find((p) => open(p) && p.number > this.page.number) || items.find(open) || null;
      },

      // Reading direction of a turn to `item`: 1 forward (also when unknown), -1 back to an earlier page.
      turnDir(item) { return item && item.number < this.page.number ? -1 : 1; },

      payloadUrlFor(id) {
        if (!id || !this.urls.payload) return null;
        return String(this.urls.payload).replace(/\/pages\/\d+\//, `/pages/${id}/`);
      },

      pageUrlFor(number) {
        const item = this.film.items.find((p) => p.number === number);
        if (item && item.url) return item.url;
        if (!hasDOM) return null;
        return window.location.pathname.replace(/\/\d+\/?$/, `/${number}/`) + window.location.search;
      },

      // Fetch another page's payload and swap it in without a reload (old page leaves toward the end
      // side, the new one arrives from the start of the reading direction); the URL follows.
      // `dir`: 1 turns forward (the default), -1 back to an earlier page (the motion is mirrored).
      async swapTo(payloadUrl, fallbackUrl, dir) {
        if (!payloadUrl) return false;
        const back = dir < 0;
        this.loading = true;
        this.slide = this.reduced || !hasDOM ? '' : (back ? 'out-back' : 'out');
        clearTimeout(this.pollTimer);
        // every queued action lands on the page it belongs to before the next page is fetched
        await this.queue.catch(() => {});
        const res = await api(payloadUrl);
        if (!res.ok || !res.data || !res.data.page) {
          this.loading = false;
          this.slide = '';
          if (fallbackUrl && hasDOM) { window.location.assign(fallbackUrl); return false; }
          this.toast(res.message);
          return false;
        }
        const land = () => {
          this.gen += 1; // responses still in flight for the old page are dropped when they arrive
          this.focus = null; this.hot = null; this.hotLine = null; this.closePop();
          this.edit = null; this.insert = null; this.menuFor = null; this.dialog.open = false; this.dismissUndo();
          if (this.save.failed.length) {
            // their retries target lines of the page being left: say so instead of keeping a chip that lies
            this.save.failed = [];
            this.toast('لم تُحفظ بعض الإجراءات في الصفحة التي غادرتها');
          }
          if (this.save.state === 'error') this.settle(this.save.pending ? 'saving' : 'idle');
          this.unmountDecode();
          this.apply(res.data);
          this.shown = this.counts.resolved;
          this.zoomReset(false);
          if (hasDOM) {
            const dest = this.pageUrlFor(res.data.page.number) || fallbackUrl;
            try {
              if (dest && window.history && window.history.replaceState) window.history.replaceState(null, '', dest);
              document.title = `مراجعة · صفحة ${res.data.page.number} · ${this.book.title || ''} · نسّاخ`;
            } catch (_) { /* fine */ }
          }
          this.loading = false;
          this.slide = this.reduced || !hasDOM ? '' : (back ? 'in-back' : 'in');
          const settle = () => {
            this.slide = '';
            this.enter();
            this.centreFilm();
            this.mountDecode();
            if (this.isPending) this.schedulePoll();
          };
          if (hasDOM) requestAnimationFrame(() => requestAnimationFrame(settle)); else settle();
        };
        if (this.reduced || !hasDOM) land(); else setTimeout(land, 220);
        return true;
      },

      goTo(item) {
        if (!item || item.number === this.page.number) return;
        const payloadUrl = this.payloadUrlFor(item.id);
        if (payloadUrl) this.swapTo(payloadUrl, item.url, this.turnDir(item));
        else if (item.url && hasDOM) window.location.assign(item.url);
      },

      goNextPage() {
        const item = this.film.items.find((p) => p.number === this.page.number + 1);
        if (item) { this.goTo(item); return; }
        if (this.nav.next_url && hasDOM) window.location.assign(this.nav.next_url);
        else this.toast('هذه آخر صفحة');
      },

      goPrevPage() {
        const item = this.film.items.find((p) => p.number === this.page.number - 1);
        if (item) { this.goTo(item); return; }
        if (this.nav.prev_url && hasDOM) window.location.assign(this.nav.prev_url);
        else this.toast('هذه أول صفحة');
      },

      // Home / End: the book's first (dir -1) or last (dir 1) page, from the filmstrip.
      goEdgePage(dir) {
        const items = (this.film.items || []).filter((p) => p && p.number);
        if (!items.length) return false;
        const numbers = items.map((p) => p.number);
        const target = dir < 0 ? Math.min(...numbers) : Math.max(...numbers);
        if (target === this.page.number) { this.toast(dir < 0 ? 'هذه أول صفحة' : 'هذه آخر صفحة'); return false; }
        this.goTo(items.find((p) => p.number === target));
        return true;
      },

      goNextReview() {
        const item = this.nextFromFilm();
        const payloadUrl = this.payloadUrlFor(item && item.id);
        if (payloadUrl) { this.swapTo(payloadUrl, this.nav.next_review_url, this.turnDir(item)); return; }
        if (this.nav.next_review_url && hasDOM) window.location.assign(this.nav.next_review_url);
        else this.toast('لا صفحات بانتظار المراجعة');
      },

      thumbTitle(p) {
        const parts = [`صفحة ${p.number}`];
        if (p.is_reviewed) parts.push('مُراجَعة');
        else if (p.n_unresolved > 0) parts.push(`${p.n_unresolved} كلمة غير محسومة`);
        return parts.join(' · ');
      },

      // ------------------------------------------------------------ filmstrip
      async loadFilm() {
        if (!this.urls.filmstrip || this.film.state === 'loading') return;
        this.film.state = 'loading';
        const res = await api(this.urls.filmstrip);
        if (!res.ok) { this.film.state = 'error'; return; }
        const items = Array.isArray(res.data) ? res.data : (res.data && (res.data.pages || res.data.items)) || [];
        this.film = { items, state: 'ready' };
        this.tick(() => this.centreFilm());
      },

      centreFilm() {
        if (!hasDOM) return;
        const host = this.$refs && this.$refs.film;
        const el = host && host.querySelector('.is-current');
        if (el && el.scrollIntoView) el.scrollIntoView({ inline: 'center', block: 'nearest', behavior: this.reduced ? 'auto' : 'smooth' });
      },

      // ------------------------------------------------------------ pending / error states
      schedulePoll() {
        if (!hasDOM) return;
        clearTimeout(this.pollTimer);
        this.pollTimer = setTimeout(() => this.poll(), POLL_MS);
      },

      async poll() {
        if (!this.isPending || document.hidden) { if (this.isPending) this.schedulePoll(); return; }
        const gen = this.gen;
        const res = await api(this.urls.payload);
        if (gen !== this.gen) return; // another page landed meanwhile and schedules its own poll
        if (res.ok && res.data && res.data.page) {
          const p = res.data.page;
          if (p.text_state === 'final' || p.status === 'error') { this.landFinal(res.data); return; }
          this.page = p;
        }
        this.schedulePoll();
      },

      // The models finished while we watched: the noise resolves in a right-to-left wave, then the
      // real lines take over (staggered in).
      landFinal(data) {
        const el = this.decodeEl;
        const gen = this.gen;
        const finish = () => {
          if (gen !== this.gen) return; // the wave outlived a page swap
          this.unmountDecode();
          this.apply(data);
          this.shown = this.counts.resolved;
          this.tick(() => this.enter());
        };
        const decode = hasDOM && window.NassakhDecode;
        if (el && decode && data.page.text_state === 'final' && !this.reduced && Array.isArray(data.lines) && data.lines.length) {
          try {
            decode.resolve(el, {
              lines: data.lines.map((l) => ({ region_kind: l.region_kind, tokens: (l.tokens || []).map((t) => ({ t: t.t, conf: t.conf, res: t.res })) })),
              onDone: finish,
            });
            return;
          } catch (_) { /* fall through */ }
        }
        finish();
      },

      mountDecode() {
        if (!hasDOM || !window.NassakhDecode) return;
        const el = this.$refs && this.$refs.noise;
        if (!el || !this.isPending || this.decodeEl === el) return;
        this.decodeEl = el;
        const count = this.regions.length ? 14 : 10;
        try { window.NassakhDecode.attach(el, { mode: 'noise', lines: count }); } catch (_) { this.decodeEl = null; }
      },

      unmountDecode() {
        if (this.decodeEl && hasDOM && window.NassakhDecode) { try { window.NassakhDecode.detach(this.decodeEl); } catch (_) { /* fine */ } }
        this.decodeEl = null;
      },

      // ------------------------------------------------------------ scan viewer
      measure() {
        const el = this.$refs && this.$refs.scan;
        if (!el) return;
        this.pane = { w: el.clientWidth, h: el.clientHeight };
        this.clampPan();
      },

      observe() {
        if (!hasDOM || typeof window.ResizeObserver !== 'function') return;
        const el = this.$refs && this.$refs.scan;
        if (!el) return;
        this.ro = new ResizeObserver(() => this.measure());
        this.ro.observe(el);
      },

      // Base (scale 1) size of the page sheet. Fit height: the whole page height fits the pane; fit width: the page
      // width fits the pane. Word boxes and the line band are % of the sheet, so they follow any size unchanged.
      get sheetW() {
        const W = this.image.width;
        const H = this.image.height;
        if (this.fit === 'height' && W && H && this.pane.h > 0) return (this.pane.h * W) / H;
        return this.pane.w;
      },
      get sheetH() { return this.image.width ? (this.sheetW * this.image.height) / this.image.width : this.pane.h; },
      get sheetStyle() {
        const w = this.sheetW > 0 ? `width:${Math.round(this.sheetW)}px; ` : '';
        return `${w}transform: translate(${Math.round(this.zoom.x)}px, ${Math.round(this.zoom.y)}px) scale(${this.zoom.scale});`;
      },
      get fitLabel() { return this.fit === 'height' ? 'ملاءمة الارتفاع' : 'ملاءمة العرض'; },

      setFit(mode) {
        const next = mode === 'width' ? 'width' : 'height';
        if (next === this.fit) { this.zoomReset(); return; }
        this.fit = next;
        storage.set(FIT_KEY, next);
        this.zoomReset();
        // keep the focused word in view in the new layout
        const tok = this.focused;
        if (tok && tok.bbox) this.tick(() => this.panTo(tok.bbox));
      },

      clampPan() {
        const z = this.zoom;
        const w = this.sheetW * z.scale;
        const h = this.sheetH * z.scale;
        z.x = w <= this.pane.w ? (this.pane.w - w) / 2 : clamp(z.x, this.pane.w - w, 0);
        z.y = h <= this.pane.h ? (this.fit === 'height' ? (this.pane.h - h) / 2 : 0) : clamp(z.y, this.pane.h - h, 0);
      },

      zoomBy(factor, cx, cy) {
        const z = this.zoom;
        const scale = clamp(z.scale * factor, ZOOM_MIN, ZOOM_MAX);
        const px = cx == null ? this.pane.w / 2 : cx;
        const py = cy == null ? this.pane.h / 2 : cy;
        const k = scale / z.scale;
        z.x = px - (px - z.x) * k;
        z.y = py - (py - z.y) * k;
        z.scale = scale;
        this.clampPan();
      },

      zoomIn() { this.glide(); this.zoomBy(1.25); },
      zoomOut() { this.glide(); this.zoomBy(0.8); },
      zoomReset(animate) {
        if (animate !== false) this.glide();
        this.zoom = { scale: 1, x: 0, y: 0 };
        this.clampPan();
      },

      // Pinch / ctrl+wheel zooms around the cursor; a plain wheel pans (the sheet is taller than the pane).
      onWheel(ev) {
        const el = this.$refs && this.$refs.scan;
        const r = el && el.getBoundingClientRect ? el.getBoundingClientRect() : { left: 0, top: 0 };
        if (ev.ctrlKey || ev.metaKey) {
          this.zoomBy(Math.exp(-ev.deltaY * 0.01), ev.clientX - r.left, ev.clientY - r.top);
        } else {
          this.zoom.y -= ev.deltaY;
          this.zoom.x -= ev.deltaX;
          this.clampPan();
        }
      },

      pointerDist() {
        const pts = Array.from(this.pointers.values());
        if (pts.length < 2) return 1;
        return Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
      },

      onPointerDown(ev) {
        if (ev.pointerType === 'mouse' && ev.button !== 0) return;
        if (!this.pointers) this.pointers = new Map();
        this.pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
        this.dragMoved = false;
        if (this.pointers.size === 2) {
          this.pinch = { dist: this.pointerDist(), scale: this.zoom.scale };
          this.drag = null;
          return;
        }
        this.drag = { x: ev.clientX, y: ev.clientY, ox: this.zoom.x, oy: this.zoom.y, moved: false };
        const el = ev.currentTarget;
        if (el && el.setPointerCapture) { try { el.setPointerCapture(ev.pointerId); } catch (_) { /* fine */ } }
      },

      onPointerMove(ev) {
        if (this.pointers && this.pointers.has(ev.pointerId)) this.pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
        if (this.pinch && this.pointers && this.pointers.size === 2) {
          const el = this.$refs && this.$refs.scan;
          const r = el && el.getBoundingClientRect ? el.getBoundingClientRect() : { left: 0, top: 0 };
          const pts = Array.from(this.pointers.values());
          const cx = (pts[0].x + pts[1].x) / 2 - r.left;
          const cy = (pts[0].y + pts[1].y) / 2 - r.top;
          const target = (this.pinch.scale * this.pointerDist()) / this.pinch.dist;
          this.zoomBy(target / this.zoom.scale, cx, cy);
          this.dragMoved = true;
          return;
        }
        if (!this.drag) return;
        const dx = ev.clientX - this.drag.x;
        const dy = ev.clientY - this.drag.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) { this.drag.moved = true; this.dragMoved = true; }
        this.zoom.x = this.drag.ox + dx;
        this.zoom.y = this.drag.oy + dy;
        this.clampPan();
      },

      onPointerUp(ev) {
        if (this.pointers) this.pointers.delete(ev.pointerId);
        if (!this.pointers || this.pointers.size < 2) this.pinch = null;
        this.drag = null;
      },

      // Keep the focused word in view: glide the sheet so the word sits mid-pane when it is near an edge.
      panTo(bbox) {
        if (!bbox || !this.pane.w || !this.image.width) return;
        const z = this.zoom;
        const k = (this.sheetW / this.image.width) * z.scale; // display px per image px (sheet, not pane)
        const cx = ((bbox[0] + bbox[2]) / 2) * k + z.x;
        const cy = ((bbox[1] + bbox[3]) / 2) * k + z.y;
        const my = Math.min(96, this.pane.h * 0.22);
        const mx = 48;
        let dx = 0;
        let dy = 0;
        if (cy < my || cy > this.pane.h - my) dy = this.pane.h / 2 - cy;
        if (cx < mx || cx > this.pane.w - mx) dx = this.pane.w / 2 - cx;
        if (!dx && !dy) return;
        this.glide();
        z.x += dx;
        z.y += dy;
        this.clampPan();
      },

      glide() {
        if (this.reduced || !hasDOM) return;
        this.gliding = true;
        clearTimeout(this.glideTimer);
        this.glideTimer = setTimeout(() => { this.gliding = false; }, 300);
      },

      boxStyle(bbox) {
        const W = this.image.width || 1;
        const H = this.image.height || 1;
        const pad = Math.max(1, (bbox[3] - bbox[1]) * 0.12);
        return `left:${(100 * (bbox[0] - pad)) / W}%; top:${(100 * (bbox[1] - pad)) / H}%; width:${(100 * (bbox[2] - bbox[0] + 2 * pad)) / W}%; height:${(100 * (bbox[3] - bbox[1] + 2 * pad)) / H}%;`;
      },

      get bandStyle() {
        const line = this.currentLine;
        if (!line || !line.bbox) return 'opacity:0;';
        const H = this.image.height || 1;
        const pad = (line.bbox[3] - line.bbox[1]) * 0.3;
        return `top:${(100 * (line.bbox[1] - pad)) / H}%; height:${(100 * (line.bbox[3] - line.bbox[1] + 2 * pad)) / H}%; opacity:1;`;
      },

      setTab(tab) { if (tab === 'scan' && !this.hasScan) return; this.tab = tab; },

      toggleSwap() {
        this.swapped = !this.swapped;
        storage.set(SWAP_KEY, this.swapped ? '1' : '0');
        this.tick(() => this.measure());
      },

      // ------------------------------------------------------------ entrances and small effects
      enter() {
        if (this.reduced || !hasDOM) return;
        this.entering = true;
        clearTimeout(this.enterTimer);
        this.enterTimer = setTimeout(() => { this.entering = false; }, 900);
      },

      pulse() {
        if (this.reduced || !hasDOM) return;
        this.pulsing = true;
        clearTimeout(this.pulseTimer);
        this.pulseTimer = setTimeout(() => { this.pulsing = false; }, 500);
      },

      tickCounter() {
        const target = this.counts.resolved;
        if (this.reduced || !hasDOM) { this.shown = target; this.syncBar(); return; }
        if (this.counterRaf) cancelAnimationFrame(this.counterRaf);
        const from = this.shown;
        if (from === target) return;
        const start = performance.now();
        const duration = 360;
        const step = (now) => {
          const p = Math.min(1, (now - start) / duration);
          const eased = 1 - Math.pow(1 - p, 3);
          this.shown = Math.round(from + (target - from) * eased);
          this.syncBar();
          if (p < 1) this.counterRaf = requestAnimationFrame(step); else this.counterRaf = null;
        };
        this.counterRaf = requestAnimationFrame(step);
      },

      // The shared toast (one at a time, DESIGN.md): it takes the place of an undo offer still showing.
      toast(message) {
        if (!message) return;
        this.dismissUndo();
        if (window.Nassakh && window.Nassakh.toast) window.Nassakh.toast(message);
      },

      // Clean text of the page for the clipboard: body, blank line, footnotes.
      pageText() {
        const body = []; const notes = [];
        this.lines.forEach((line) => {
          const text = line.text || (line.tokens || []).map((t) => t.t).join(' ');
          (line.region_kind === 'footnote' ? notes : body).push(text);
        });
        return [body.join('\n'), notes.join('\n')].filter(Boolean).join('\n\n');
      },

      copyPage() {
        if (window.Nassakh && window.Nassakh.copyText) return window.Nassakh.copyText(this.pageText());
        return false;
      },

      // ------------------------------------------------------------ modals
      openSheet() {
        this.sheetReturn = hasDOM ? document.activeElement : null;
        this.sheetOpen = true;
        this.tick(() => { const el = this.$refs.sheetClose; if (el) el.focus(); });
      },

      closeSheet() {
        this.sheetOpen = false;
        const el = this.sheetReturn;
        this.sheetReturn = null;
        if (el && el.focus) this.tick(() => el.focus());
      },

      // Focus trap for the small modals: Tab cycles inside the dialog.
      trapTab(ev, root) {
        if (!root || !root.querySelectorAll) return;
        const items = Array.from(root.querySelectorAll('button, [href], input, textarea, select, [tabindex]:not([tabindex="-1"])'))
          .filter((el) => !el.disabled && el.offsetParent !== null);
        if (!items.length) { ev.preventDefault(); return; }
        const first = items[0];
        const last = items[items.length - 1];
        if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
        else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
      },

      // Esc closes the top-most layer only.
      closeTop() {
        if (this.menuFor != null) { this.menuFor = null; return; }
        if (this.pop.more) { this.closeMore(true); return; }
        if (this.pop.typing) {
          // readings above the input: Esc returns to them; a correction-only popover (a confident word) just closes
          if (this.options().length) { this.pop.typing = false; this.pop.typed = ''; } else this.closePop();
          this.refocusWord();
          return;
        }
        // the menu closes and the focus stays on its word (page mode: A, E, N are commands again)
        if (this.pop.open) { this.closePop(); this.refocusWord(); return; }
        if (this.edit || this.insert) { this.cancelEdit(); return; }
        if (this.undoToast) { this.dismissUndo(); return; }
        if (this.focus) { this.focus = null; return; }
      },

      refocusWord() {
        this.tick(() => { const el = this.focus && document.getElementById(this.tokId(this.focus.lineId, this.focus.index)); if (el) el.focus(); });
      },

      // ------------------------------------------------------------ keyboard
      onKey(ev) {
        if (!ev || ev.defaultPrevented) return;
        const target = ev.target || {};
        const tag = String(target.tagName || '').toLowerCase();
        const inField = tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable === true;
        if (this.sheetOpen) { if (ev.key === 'Escape') { ev.preventDefault(); this.closeSheet(); } return; }
        if (this.dialog.open) { if (ev.key === 'Escape') { ev.preventDefault(); this.dialog.open = false; } return; }
        // Buttons and links keep their own keys (Enter / Space activate them, nothing deletes a word from
        // them), and inside the word popover the arrows belong to its menus, not to page navigation.
        // (⌘↵ approves from anywhere, a button included)
        const k = ev.key;
        const chord = ev.metaKey || ev.ctrlKey;
        if ((tag === 'button' || tag === 'a') && ((k === 'Enter' && !chord) || k === ' ' || k === 'Backspace' || k === 'Delete')) return;
        const inPop = Boolean(target.closest && target.closest('.rv-pop'));
        if (inPop && !inField && (k === 'ArrowLeft' || k === 'ArrowRight' || k === 'ArrowUp' || k === 'ArrowDown')) return;
        const outside = Boolean(target.closest && target.closest('.rv-bar, .rv-film, .rv-toolbar, .sidebar, .topbar'));
        const inFlow = !inField && !outside && tag !== 'button' && tag !== 'a';
        const focused = Boolean(this.focused) && this.editable;
        const open = focused && this.pop.open;
        const action = keyAction(ev, { inField, inPop, inFlow, focused, open, optionKeys: this.options().map((o) => o.key) });
        if (action) this.runAction(action, ev);
      },

      // ⌘↵ / A: approve the page. From the word menu's correction a changed draft is saved first (the queue keeps
      // the order, and the approval then counts it); an untouched prefill is dropped with the menu.
      approveFromKeys() {
        if (this.pop.open && this.pop.typing) {
          const value = this.pop.typed.trim();
          const tok = this.focused;
          if (value && tok && this.editable && (value !== tok.t || this.isUnresolved(tok))) this.submitTyped();
        }
        this.closePop();
        return this.approve(false);
      },

      // Space on a focused word: its menu, as a click opens it.
      openWord() {
        if (!this.focus || !this.focused) return false;
        this.focusWord(this.focus, { open: true, pan: false });
        return true;
      },

      runAction(action, ev) {
        const stop = () => { if (ev && ev.preventDefault) ev.preventDefault(); };
        switch (action) {
          case 'undo': stop(); this.undo(); break;
          case 'close': this.closeTop(); break;
          case 'next': stop(); this.move(1); break;
          case 'prev': stop(); this.move(-1); break;
          case 'insert': stop(); this.startInsert(); break;
          case 'accept': stop(); this.accept(); break;
          case 'type': stop(); this.startTyping(ev.key); break;
          case 'openWord': stop(); this.openWord(); break;
          case 'edit': stop(); this.startEdit(); break;
          case 'mergeNext': stop(); this.mergeWord(1); break;
          case 'mergePrev': stop(); this.mergeWord(-1); break;
          case 'deleteWord': stop(); this.deleteWord(); break;
          case 'approve': stop(); this.approveFromKeys(); break;
          case 'nextReview': stop(); this.goNextReview(); break;
          case 'nextPage': stop(); this.goNextPage(); break;
          case 'prevPage': stop(); this.goPrevPage(); break;
          case 'firstPage': stop(); this.goEdgePage(-1); break;
          case 'lastPage': stop(); this.goEdgePage(1); break;
          case 'zoomIn': stop(); this.zoomIn(); break;
          case 'zoomOut': stop(); this.zoomOut(); break;
          case 'zoomReset': stop(); this.zoomReset(); break;
          case 'sheet': stop(); this.openSheet(); break;
          default: {
            const reading = /^choose([1-9])$/.exec(action);
            if (reading) { stop(); this.chooseNth(Number(reading[1])); }
            break;
          }
        }
      },
    }));
  });
})();
