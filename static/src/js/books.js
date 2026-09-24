// Alpine components for the books screens: new-book form, dashboard, page detail viewer.
// This file runs before the deferred Alpine bundle, so components are registered on `alpine:init`.
// Digits shown to the user are always Western (no locale-aware number formatting here).
document.addEventListener('alpine:init', () => {
  const POLL_INTERVAL = 2000;
  const POLL_MAX_INTERVAL = 15000;
  const FAILURES_BEFORE_NOTICE = 3;

  // ---------------------------------------------------------------- new book form
  Alpine.data('bookForm', (cfg = {}) => ({
    pagesPerSheet: Number(cfg.pagesPerSheet) || 1,
    splitRatio: Number(cfg.splitRatio) || 0.5,
    submitting: false,

    get splitPercent() {
      return `${Math.round(Number(this.splitRatio) * 100)}%`;
    },
    // The ratio is measured from the physical left edge of the sheet, like the pixel column it maps to.
    get splitLineStyle() {
      return `left:${Number(this.splitRatio) * 100}%`;
    },
  }));

  // ---------------------------------------------------------------- book dashboard (docs/DASHBOARD_SPEC.md)
  // Two views (D25): «صفحات» stacks static sheet shells whose bodies mount near the viewport and become
  // NassakhDecode.sheet handles (scan + mirrored text pane); «شبكة» shows static tiles. Both are patched from
  // the compact progress poll (no per-page Alpine bindings, no reload at the end of processing).
  const VIEW_KEY = 'nassakh.bookView';
  const FILTER_KEY = 'nassakh.bookFilter.';
  const FOLLOW_KEY = 'nassakh.bookFollow';
  const SCROLL_KEY = 'nassakh.bookScroll.';
  const SHEET_BATCH = 40; // api:book_sheets serves at most 40 pages per call
  const NEAR_MARGIN = '1500px'; // a sheet this close to the viewport mounts and fetches its data
  const FAR_MARGIN = '4000px'; // and unmounts again once it is this far away (800-page books stay light)
  const TILE_MARGIN = '600px'; // grid tiles animate only this close to the viewport
  const SHEET_FLUSH_MS = 60;
  const FOLLOW_MIN_MS = 2000;
  const POS_SHOW_MS = 150;
  const POS_HIDE_MS = 1200;
  const DONE_TOAST_MS = 8000;
  const PULSE_MS = 500;
  const STAGE_PERCENT = { uploaded: 12, preprocessed: 46, layout_done: 72, ocr_done: 100, reviewed: 100, assembled: 100 };
  const DONE_STATUSES = ['ocr_done', 'reviewed', 'assembled'];
  const REVIEWED_STATUSES = ['reviewed', 'assembled'];
  const PROCESSING_STATUSES = ['uploaded', 'preprocessed', 'layout_done'];
  const CHANGE_KEYS = ['status', 'text_state', 'n_unresolved', 'is_reviewed', 'n_flags', 'sequence_issue', 'error', 'is_excluded', 'printed_number'];
  // Fallbacks until the backend config carries statusLabels / statusDots / urls (§12.2).
  const STATUS_LABELS = { uploaded: 'مرفوعة', preprocessed: 'مُعالَجة', layout_done: 'تم التخطيط', ocr_done: 'تم التعرّف', reviewed: 'مُراجَعة', assembled: 'مُجمَّعة', error: 'خطأ', excluded: 'مستثناة' };
  const STATUS_DOTS = { uploaded: 'dot-neutral', preprocessed: 'dot-accent', layout_done: 'dot-accent', ocr_done: 'dot-success', reviewed: 'dot-success', assembled: 'dot-success', error: 'dot-danger', excluded: 'dot-neutral' };
  const FILTERS = {
    all: () => true,
    processing: (p) => !p.is_excluded && PROCESSING_STATUSES.includes(p.status),
    review: (p) => !p.is_excluded && p.status === 'ocr_done',
    attention: (p) => !p.is_excluded && (Boolean(p.error) || p.n_flags > 0 || Boolean(p.sequence_issue)),
    reviewed: (p) => !p.is_excluded && REVIEWED_STATUSES.includes(p.status),
  };
  const EASTERN_DIGITS = /[٠-٩۰-۹]/g;

  function readLocal(key, fallback) {
    try {
      const v = localStorage.getItem(key);
      return v === null ? fallback : v;
    } catch (e) {
      return fallback;
    }
  }
  function writeLocal(key, value) {
    try { localStorage.setItem(key, String(value)); } catch (e) { /* private mode: the default is fine */ }
  }
  function readSession(key) {
    try { return sessionStorage.getItem(key); } catch (e) { return null; }
  }
  function writeSession(key, value) {
    try { sessionStorage.setItem(key, String(value)); } catch (e) { /* fine */ }
  }
  // Contiguous runs of page numbers, each at most `max` long: [[from, to], ...].
  function batchRanges(numbers, max = SHEET_BATCH) {
    const sorted = [...new Set(numbers)].map(Number).filter((n) => n > 0).sort((a, b) => a - b);
    const ranges = [];
    sorted.forEach((n) => {
      const last = ranges[ranges.length - 1];
      if (last && n === last[1] + 1 && n - last[0] < max) last[1] = n;
      else ranges.push([n, n]);
    });
    return ranges;
  }
  const sheetLinesText = (lines) => {
    const body = [];
    const notes = [];
    (lines || []).forEach((line) => {
      const text = (line.tokens || []).map((tok) => (typeof tok === 'string' ? tok : tok.t)).join(' ');
      (line.region_kind === 'footnote' ? notes : body).push(text);
    });
    return notes.length ? `${body.join('\n')}\n\n${notes.join('\n')}` : body.join('\n');
  };
  // A page number typed by hand: Western or Eastern digits, anything else ignored.
  function parsePageNumber(raw) {
    const digits = String(raw || '')
      .replace(EASTERN_DIGITS, (d) => { const c = d.charCodeAt(0); return String(c >= 0x06f0 ? c - 0x06f0 : c - 0x0660); })
      .replace(/\D/g, '');
    return digits ? parseInt(digits, 10) : NaN;
  }
  const norm = (v) => (v === undefined || v === null || v === false ? '' : v === true ? '1' : String(v));
  const byNumberAsc = (a, b) => a.number - b.number;
  const toast = (message) => { if (window.Nassakh && window.Nassakh.toast) window.Nassakh.toast(message); };
  const setHidden = (el, hidden) => { if (el) el.hidden = Boolean(hidden); };
  const setText = (el, text) => { if (el && el.textContent !== String(text)) el.textContent = String(text); };
  const q = (root, sel) => (root && typeof root.querySelector === 'function' ? root.querySelector(sel) : null);
  const decode = () => window.NassakhDecode || null;
  const reduced = () => Boolean(decode() && decode().reducedMotion);

  // The top bar (base.html header_actions, outside the component root) reads the dashboard through this store.
  if (typeof Alpine.store === 'function') Alpine.store('book', { dash: null });

  Alpine.data('bookDashboard', (cfg = {}) => {
    // Non-reactive plumbing lives in the closure: the page records, shells, handles, observers, timers.
    const pages = new Map(); // page id (string) -> merged tile record
    const sheets = new Map(); // page id -> api:book_sheets item (only for sheets that came near the viewport)
    const shells = new Map(); // page id -> article.page-sheet
    const tiles = new Map(); // page id -> div.page-tile
    const handles = new Map(); // page id -> NassakhDecode.sheet handle
    const mounted = new Set(); // page ids whose sheet body is in the DOM
    const byNumber = new Map(); // page number -> page id
    const pending = new Set(); // page numbers waiting for a sheets request
    let bookLineH = 0; // the book's typical printed line height in px (api:book_sheets), shared by every sheet (D30)
    let flushTimer = null;
    let nearObserver = null;
    let farObserver = null;
    let tileObserver = null;
    let stackEl = null;
    let gridEl = null;
    let shellTpl = null;
    let tileTpl = null;
    let bodyTpl = null;
    let focusedId = null;
    let lastFollowAt = 0;
    let posTimer = null;
    let posHideTimer = null;
    let scrolling = false;
    let scrollRaf = 0;
    let resizeTimer = null;
    let doneTimer = null;
    let pulseTimer = null;
    const bookUrl = String(cfg.bookUrl || '');
    const urls = Object.assign(
      { page: `${bookUrl}pages/__n__/`, review: `${bookUrl}review/__n__/`, rerun: `${bookUrl}pages/__n__/rerun/`, exclude: `${bookUrl}pages/__n__/exclude/` },
      cfg.urls || {},
    );
    const statusLabels = Object.assign({}, STATUS_LABELS, cfg.statusLabels || {});
    const statusDots = Object.assign({}, STATUS_DOTS, cfg.statusDots || {});
    const fill = (template, n) => String(template || '').replace('__n__', String(n));
    const id = (p) => String(p.id);

    return {
    progressUrl: cfg.progressUrl,
    sheetsUrl: cfg.sheetsUrl || '',
    reviewNextUrl: cfg.reviewNextUrl || '',
    bookTextUrl: cfg.bookTextUrl || '',
    canEdit: Boolean(cfg.canEdit),
    active: Boolean(cfg.active),
    total: cfg.total || 0,
    percent: cfg.percent || 0,
    flags: cfg.flags || 0,
    status: cfg.status || '',
    statusLabel: cfg.statusLabel || '',
    dot: cfg.dot || 'dot-neutral',
    barState: cfg.barState || '',
    errorHeadline: cfg.errorHeadline || '',
    errorDetail: cfg.errorDetail || '',
    byStatus: cfg.byStatus || {},
    review: cfg.review || null, // {reviewed, total, unresolved_total, next_review_url} from the progress poll
    stageMap: Object.fromEntries((cfg.stages || []).map((s) => [s.key, s.statuses])),
    view: readLocal(VIEW_KEY, 'sheets') === 'grid' ? 'grid' : 'sheets',
    filter: 'all',
    follow: readLocal(FOLLOW_KEY, '0') === '1',
    counts: { all: 0, processing: 0, review: 0, attention: 0, reviewed: 0 },
    attention: [],
    nPages: 0,
    filteredOut: false,
    liveMessage: '', // one polite announcement per poll
    pollState: 'ok', // ok | error | auth
    timer: null,
    failures: 0,
    stopped: false,
    copying: false,
    sheetsFailed: false,
    pos: { visible: false, n: 0 },
    doneToast: { visible: false, count: 0, url: '' },
    lastJump: null,

    init() {
      (cfg.pages || []).forEach((raw) => { const p = this.completePage(raw, null); pages.set(id(p), p); byNumber.set(p.number, id(p)); });
      const savedFilter = readLocal(FILTER_KEY + (cfg.bookId || ''), 'all');
      this.filter = FILTERS[savedFilter] ? savedFilter : 'all';
      this.recount();
      this.bindDom();
      if (typeof Alpine.store === 'function' && Alpine.store('book')) Alpine.store('book').dash = this;
      if (this.active) this.schedule(POLL_INTERVAL);
      if (this.$watch) this.$watch('view', () => this.onViewChange());
    },
    destroy() {
      clearTimeout(this.timer);
      clearTimeout(flushTimer);
      clearTimeout(doneTimer);
      handles.forEach((h) => h.destroy());
      handles.clear();
      if (nearObserver) { nearObserver.disconnect(); farObserver.disconnect(); }
      if (tileObserver) tileObserver.disconnect();
      if (typeof Alpine.store === 'function' && Alpine.store('book')) Alpine.store('book').dash = null;
    },

    // ------------------------------------------------------------ page records
    // A poll entry (full tile at first paint, compact afterwards) merged over the previous record.
    completePage(raw, before) {
      const p = Object.assign({}, before || {}, raw);
      const n = p.number;
      p.id = raw.id;
      p.status_label = raw.status_label || statusLabels[p.status] || (before && before.status === p.status ? before.status_label : '') || p.status || '';
      p.dot = raw.dot || statusDots[p.status] || 'dot-neutral';
      p.url = p.url || fill(urls.page, n);
      p.review_url = p.review_url || fill(urls.review, n);
      p.rerun_url = p.rerun_url || fill(urls.rerun, n);
      p.exclude_url = p.exclude_url || fill(urls.exclude, n);
      if (!(p.n_flags > 0)) p.flag_labels = [];
      else if (!Array.isArray(p.flag_labels)) p.flag_labels = before && Array.isArray(before.flag_labels) ? before.flag_labels : [];
      if (!p.error) { p.error_headline = ''; p.retry_stage = ''; p.retry_label = ''; }
      else {
        p.error_headline = raw.error_headline || p.error_headline || '';
        p.retry_stage = raw.retry_stage || p.retry_stage || '';
        p.retry_label = raw.retry_label || p.retry_label || '';
      }
      p.sequence_issue = p.sequence_issue || '';
      p.n_unresolved = Number(p.n_unresolved) || 0;
      p.n_flags = Number(p.n_flags) || 0;
      return p;
    },
    page(pid) {
      return pages.get(String(pid)) || null;
    },
    tile(pid) {
      return pages.get(String(pid)) || {};
    },
    sheet(pid) {
      return sheets.get(String(pid)) || null;
    },
    // The sheet payload merged over the tile: what a NassakhDecode.sheet handle sees.
    sheetFor(pid) {
      const p = pages.get(String(pid)) || {};
      const s = sheets.get(String(pid));
      if (!s) return p;
      return Object.assign({}, p, s, { error: s.error || p.error || false, error_headline: s.error || p.error_headline || '', book_line_h_px: bookLineH });
    },
    isMounted(pid) {
      return mounted.has(String(pid));
    },

    // ------------------------------------------------------------ the poll (compact, §12.2)
    schedule(ms) {
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.poll(), ms);
    },
    pollNow() {
      clearTimeout(this.timer);
      this.poll();
    },
    async poll() {
      if (this.stopped) return;
      if (typeof document !== 'undefined' && document.hidden) { // do not hammer the server from a background tab
        this.schedule(POLL_INTERVAL);
        return;
      }
      try {
        const sep = this.progressUrl.includes('?') ? '&' : '?';
        const res = await fetch(`${this.progressUrl}${sep}compact=1`, {
          headers: { Accept: 'application/json' },
          credentials: 'same-origin',
          cache: 'no-store',
        });
        if (res.status === 401 || res.status === 403) { // session ended: stop for good
          this.stopped = true;
          this.pollState = 'auth';
          this.stopEffects();
          return;
        }
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        this.apply(await res.json());
        this.failures = 0;
        this.pollState = 'ok';
      } catch (err) {
        this.failures += 1;
        if (this.failures >= FAILURES_BEFORE_NOTICE) this.pollState = 'error';
      }
      if (this.active) this.schedule(Math.min(POLL_INTERVAL * (1 + this.failures), POLL_MAX_INTERVAL));
    },
    apply(d) {
      const wasActive = this.active;
      this.total = d.total;
      this.percent = d.percent;
      this.flags = d.flags;
      this.status = d.status;
      this.statusLabel = d.status_label;
      this.dot = d.dot;
      this.barState = d.bar_state || '';
      this.byStatus = d.by_status || {};
      if (d.error_headline !== undefined) this.errorHeadline = d.error_headline || '';
      if (d.error_detail !== undefined) this.errorDetail = d.error_detail || '';
      if (d.review) this.review = d.review;
      this.active = Boolean(d.active);
      const changed = [];
      const added = [];
      (d.pages || []).forEach((raw) => {
        const before = pages.get(String(raw.id));
        const p = this.completePage(raw, before);
        pages.set(id(p), p);
        if (!before) { byNumber.set(p.number, id(p)); added.push(p); return; }
        const diff = CHANGE_KEYS.some((k) => k in raw && norm(before[k]) !== norm(raw[k]));
        if (diff) changed.push({ before, after: p });
      });
      added.sort(byNumberAsc).forEach((p) => this.addPage(p));
      changed.forEach(({ after }) => { after.stale = true; this.patchPage(after); });
      if (added.length || changed.length) this.recount();
      if (changed.length) {
        const last = changed[changed.length - 1].after;
        this.liveMessage = `الصفحة ${last.number}: ${last.status_label}`;
        changed.forEach(({ after }) => { if (mounted.has(id(after)) || this.tileNear(id(after))) this.queueSheet(after.number); });
      }
      if (wasActive && !this.active) this.onProcessingEnd();
      else if (this.active && this.follow && changed.length) this.followChanged(changed);
    },
    // The first poll with active false after an active one: effects stop, chrome follows, one toast, no reload.
    onProcessingEnd() {
      this.stopEffects();
      this.doneToast = { visible: true, count: this.nPages, url: this.nextReviewUrl };
      clearTimeout(doneTimer);
      doneTimer = setTimeout(() => { this.doneToast.visible = false; }, DONE_TOAST_MS);
    },
    stopEffects() {
      handles.forEach((h) => h.update({ active: false }));
    },
    addPage(p) {
      this.patchPage(p);
      if (shellTpl && stackEl && !shells.has(id(p))) {
        const el = q(shellTpl.content ? shellTpl.content.cloneNode(true) : null, '.page-sheet');
        if (el) {
          shells.set(id(p), el);
          this.patchSheet(el, p);
          stackEl.insertBefore(el, this.nextShell(p.number, shells));
          this.observeSheet(el);
        }
      }
      if (tileTpl && gridEl && !tiles.has(id(p))) {
        const el = q(tileTpl.content ? tileTpl.content.cloneNode(true) : null, '.page-tile');
        if (el) {
          tiles.set(id(p), el);
          this.patchTile(el, p);
          gridEl.insertBefore(el, this.nextShell(p.number, tiles));
          if (tileObserver) tileObserver.observe(el);
        }
      }
    },
    nextShell(number, map) {
      let best = null;
      let bestN = Infinity;
      map.forEach((el, pid) => {
        const p = pages.get(pid);
        if (p && p.number > number && p.number < bestN) { bestN = p.number; best = el; }
      });
      return best;
    },
    patchPage(p) {
      const pid = id(p);
      this.patchSheet(shells.get(pid), p);
      this.patchTile(tiles.get(pid), p);
      const h = handles.get(pid);
      if (h) {
        // the head is patched now; the text pane waits for its refetched data (a stale payload never
        // decides the mode), except for the active flag which stops or resumes the effects at once
        if (p.stale) h.update({ active: this.active });
        else h.update({ page: this.sheetFor(pid), active: this.active });
      }
    },
    recount() {
      const all = [...pages.values()];
      const c = { all: all.length, processing: 0, review: 0, attention: 0, reviewed: 0 };
      all.forEach((p) => {
        if (FILTERS.processing(p)) c.processing += 1;
        if (FILTERS.review(p)) c.review += 1;
        if (FILTERS.attention(p)) c.attention += 1;
        if (FILTERS.reviewed(p)) c.reviewed += 1;
      });
      this.counts = c;
      this.nPages = all.length;
      this.attention = all.filter(FILTERS.attention).sort(byNumberAsc).map((p) => ({
        id: p.id,
        number: p.number,
        url: p.url,
        error: Boolean(p.error),
        error_headline: p.error_headline || '',
        retry_stage: p.retry_stage || '',
        retry_label: p.retry_label || '',
        rerun_url: p.rerun_url,
        sequence_issue: p.sequence_issue || '',
        flag_labels: p.flag_labels || [],
      }));
      this.filteredOut = this.nPages > 0 && this.filter !== 'all' && c[this.filter] === 0;
    },

    // ------------------------------------------------------------ derived chrome state
    count(key) {
      const statuses = this.stageMap[key] || [key];
      return statuses.reduce((n, s) => n + (this.byStatus[s] || 0), 0);
    },
    get done() {
      return this.count('ocr_done');
    },
    get anyOcrDone() {
      return this.done > 0;
    },
    get statusText() {
      if (this.active) return `قيد المعالجة · ${this.done} من ${this.total} صفحة`;
      return this.statusLabel;
    },
    get barClass() {
      return { 'is-success': this.percent >= 100 && this.status !== 'error', 'is-danger': this.status === 'error', 'is-warning': this.status === 'needs_guides' };
    },
    // The progress payload carries `review` (its `next_review_url` is null when no page waits);
    // until it does, the summary is derived from the tiles and the template's guarded review:next URL.
    get reviewSummary() {
      if (this.review) return { ...this.review, next_review_url: this.review.next_review_url || '' };
      const included = [...pages.values()].filter((p) => !p.is_excluded);
      const reviewed = included.filter((p) => REVIEWED_STATUSES.includes(p.status)).length;
      return {
        reviewed,
        total: included.length,
        unresolved_total: included.reduce((n, p) => n + (p.n_unresolved || 0), 0),
        next_review_url: this.anyOcrDone && reviewed < included.length ? this.reviewNextUrl : '',
      };
    },
    get nextReviewUrl() {
      return this.reviewSummary.next_review_url || '';
    },
    get allReviewed() {
      const s = this.reviewSummary;
      return s.total > 0 && s.reviewed >= s.total;
    },
    // Exactly one primary button per state (§2.1.5), chosen from the poll without a reload.
    get primary() {
      if (this.canEdit && (this.status === 'uploaded' || this.status === 'error')) return 'start';
      if (this.canEdit && this.status === 'needs_guides' && cfg.guidesUrl) return 'guides';
      if (this.nextReviewUrl) return 'review';
      if (this.allReviewed && this.bookTextUrl) return 'copy';
      return '';
    },
    stagePercent(p) {
      if (p.error) return 100;
      return STAGE_PERCENT[p.status] || 0;
    },

    // ------------------------------------------------------------ views, filters, follow
    setView(view) {
      this.view = view === 'grid' ? 'grid' : 'sheets';
      writeLocal(VIEW_KEY, this.view);
    },
    onViewChange() {
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => this.updateSheetHeight());
    },
    setFilter(name) {
      this.filter = FILTERS[name] ? name : 'all';
      writeLocal(FILTER_KEY + (cfg.bookId || ''), this.filter);
      this.applyFilter();
      this.filteredOut = this.nPages > 0 && this.filter !== 'all' && this.counts[this.filter] === 0;
    },
    matches(p) {
      return FILTERS[this.filter](p);
    },
    applyFilter() {
      shells.forEach((el, pid) => setHidden(el, !this.matches(pages.get(pid))));
      tiles.forEach((el, pid) => setHidden(el, !this.matches(pages.get(pid))));
    },
    toggleFollow() {
      this.follow = !this.follow;
      writeLocal(FOLLOW_KEY, this.follow ? '1' : '0');
    },
    userScrolled() {
      if (!this.follow) return;
      this.follow = false;
      writeLocal(FOLLOW_KEY, '0');
      toast('أُوقف التتبّع');
    },
    // The highest-numbered page that just entered provisional text or finished OCR (§8.3), or null.
    followTarget(changed) {
      let best = null;
      changed.forEach(({ before, after }) => {
        const entered = (before.text_state !== 'provisional' && after.text_state === 'provisional')
          || (before.status !== 'ocr_done' && after.status === 'ocr_done');
        if (entered && !after.is_excluded && (best === null || after.number > best)) best = after.number;
      });
      return best;
    },
    followChanged(changed) {
      if (this.view !== 'sheets') return;
      const n = this.followTarget(changed);
      if (n === null) return;
      const now = Date.now();
      if (now - lastFollowAt < FOLLOW_MIN_MS) return;
      lastFollowAt = now;
      this.goTo(n, false);
    },

    // ------------------------------------------------------------ jump, keyboard, position
    jumpTarget(value) {
      const n = parsePageNumber(value);
      if (!Number.isFinite(n) || !byNumber.has(n)) return null;
      return n;
    },
    jump(value) {
      const n = this.jumpTarget(value);
      this.lastJump = n;
      if (n === null) { toast('لا صفحة بهذا الرقم'); return null; }
      this.goTo(n, true);
      return n;
    },
    goTo(n, focus) {
      const pid = byNumber.get(n);
      const el = this.view === 'grid' ? tiles.get(pid) : shells.get(pid);
      if (!el || typeof el.scrollIntoView !== 'function') return;
      if (el.hidden) this.setFilter('all');
      el.scrollIntoView({ block: 'start', behavior: reduced() ? 'auto' : 'smooth' });
      el.classList.add('is-pulse');
      clearTimeout(pulseTimer);
      pulseTimer = setTimeout(() => el.classList.remove('is-pulse'), PULSE_MS);
      if (focus) {
        const target = q(el, '.sheet-title') || q(el, '.page-tile-link');
        if (target && target.focus) target.focus({ preventScroll: true });
      }
    },
    focusJump() {
      const field = this.$refs && this.$refs.jump;
      if (field && field.focus) { field.focus(); if (field.select) field.select(); }
    },
    // Keyboard map (§8.8), RTL-aware; never fires inside a field. Pure enough for the tests: returns the action.
    keyAction(e, inField) {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return null;
      const k = e.key;
      const code = e.code || '';
      if (k === 'Escape') return inField ? 'blur' : 'clearFilter';
      if (inField) return null;
      if (code === 'KeyG' || k === 'g' || k === 'G') return 'jump';
      if (code === 'KeyN' || k === 'n' || k === 'N') return 'nextReview';
      if (code === 'KeyC' || k === 'c' || k === 'C') return 'copy';
      if (k === '1') return 'sheets';
      if (k === '2') return 'grid';
      if (k === 'ArrowLeft') return 'nextSheet';
      if (k === 'ArrowRight') return 'prevSheet';
      if (k === 'ArrowDown') return 'hotDown';
      if (k === 'ArrowUp') return 'hotUp';
      return null;
    },
    onKey(e) {
      const t = e.target;
      const inField = Boolean(t && (['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName) || t.isContentEditable));
      const action = this.keyAction(e, inField);
      if (!action) return;
      if (action === 'blur') { if (t && t.blur) t.blur(); return; }
      if (action === 'clearFilter') { if (this.filter !== 'all') this.setFilter('all'); return; }
      if (action === 'jump') { e.preventDefault(); this.focusJump(); return; }
      if (action === 'nextReview') { if (this.nextReviewUrl) window.location.assign(this.nextReviewUrl); return; }
      if (action === 'sheets') { this.setView('sheets'); return; }
      if (action === 'grid') { this.setView('grid'); return; }
      if (action === 'nextSheet' || action === 'prevSheet') { e.preventDefault(); this.stepSheet(action === 'nextSheet' ? 1 : -1); return; }
      if (action === 'hotDown' || action === 'hotUp') { if (focusedId !== null && this.moveHot(action === 'hotDown' ? 1 : -1)) e.preventDefault(); return; }
      if (action === 'copy' && focusedId !== null) this.copySheet(focusedId);
    },
    visibleNumbers() {
      return [...byNumber.keys()].sort((a, b) => a - b).filter((n) => {
        const el = this.view === 'grid' ? tiles.get(byNumber.get(n)) : shells.get(byNumber.get(n));
        return el && !el.hidden;
      });
    },
    stepSheet(dir) {
      const numbers = this.visibleNumbers();
      if (!numbers.length) return;
      const current = focusedId !== null ? (pages.get(focusedId) || {}).number : this.currentNumber();
      let idx = numbers.indexOf(current);
      if (idx === -1) idx = dir > 0 ? -1 : numbers.length;
      const next = numbers[Math.max(0, Math.min(numbers.length - 1, idx + dir))];
      if (next !== undefined) this.goTo(next, true);
    },
    moveHot(dir) {
      const h = handles.get(focusedId);
      if (!h || !h.lines) return false;
      const i = Math.max(0, Math.min(h.lines - 1, (h.hotIndex < 0 ? (dir > 0 ? -1 : h.lines) : h.hotIndex) + dir));
      h.hot(i);
      const line = h.lineEl(i);
      if (line && line.focus && line.getAttribute && line.getAttribute('tabindex') !== null) line.focus({ preventScroll: true });
      return true;
    },
    currentNumber() {
      if (!stackEl || typeof window === 'undefined') return 0;
      const numbers = this.visibleNumbers();
      if (!numbers.length) return 0;
      const top = 52 + 44 + 8; // topbar + toolbar
      let lo = 0;
      let hi = numbers.length - 1;
      while (lo < hi) { // first sheet whose bottom edge is below the toolbar
        const mid = (lo + hi) >> 1;
        const el = shells.get(byNumber.get(numbers[mid]));
        const rect = el.getBoundingClientRect();
        if (rect.bottom < top) lo = mid + 1;
        else hi = mid;
      }
      return numbers[lo];
    },
    onScroll() {
      if (!scrolling) {
        scrolling = true;
        clearTimeout(posTimer);
        posTimer = setTimeout(() => {
          if (scrolling && this.view === 'sheets' && this.nPages >= 20) this.pos = { visible: true, n: this.currentNumber() };
        }, POS_SHOW_MS);
      } else if (this.pos.visible && !scrollRaf && typeof requestAnimationFrame === 'function') {
        scrollRaf = requestAnimationFrame(() => { scrollRaf = 0; this.pos.n = this.currentNumber(); });
      }
      clearTimeout(posHideTimer);
      posHideTimer = setTimeout(() => {
        scrolling = false;
        this.pos.visible = false;
        writeSession(SCROLL_KEY + (cfg.bookId || ''), Math.round(window.scrollY || 0));
      }, POS_HIDE_MS);
    },
    restorePosition() {
      const m = /^#sheet-(\d+)$/.exec(window.location.hash || '');
      if (m) { requestAnimationFrame(() => this.jump(m[1])); return; }
      const y = Number(readSession(SCROLL_KEY + (cfg.bookId || '')));
      if (y > 0) requestAnimationFrame(() => window.scrollTo(0, y));
    },

    // ------------------------------------------------------------ DOM binding: shells, observers, listeners
    bindDom() {
      const root = this.$el || (typeof document !== 'undefined' && document.querySelector ? document.querySelector('[data-book-dashboard]') : null);
      if (!root || typeof root.querySelector !== 'function') return;
      stackEl = root.querySelector('[data-sheet-stack]');
      gridEl = root.querySelector('[data-page-grid]');
      shellTpl = document.getElementById('sheet-shell');
      tileTpl = document.getElementById('tile-shell');
      bodyTpl = document.getElementById('sheet-body');
      if (stackEl) stackEl.querySelectorAll('.page-sheet').forEach((el) => { if (el.dataset.pageId) shells.set(el.dataset.pageId, el); });
      if (gridEl) gridEl.querySelectorAll('.page-tile').forEach((el) => { if (el.dataset.pageId) tiles.set(el.dataset.pageId, el); });
      this.ensureObservers();
      shells.forEach((el) => this.observeSheet(el));
      if (tileObserver) tiles.forEach((el) => tileObserver.observe(el));
      if (stackEl) {
        stackEl.addEventListener('mouseover', (e) => this.onLineOver(e));
        stackEl.addEventListener('mouseout', (e) => this.onLineOut(e));
        stackEl.addEventListener('focusin', (e) => { const art = e.target && e.target.closest ? e.target.closest('.page-sheet') : null; focusedId = art ? art.dataset.pageId : null; });
        stackEl.addEventListener('focusout', (e) => { const to = e.relatedTarget; if (!to || !to.closest || !to.closest('.page-sheet')) focusedId = null; });
        stackEl.addEventListener('click', (e) => this.onStackClick(e));
      }
      this.applyFilter();
      this.updateSheetHeight();
      window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => this.updateSheetHeight(), 120); });
      window.addEventListener('scroll', () => this.onScroll(), { passive: true });
      ['wheel', 'touchmove'].forEach((ev) => window.addEventListener(ev, () => this.userScrolled(), { passive: true }));
      window.addEventListener('keydown', (e) => { if ([' ', 'PageDown', 'PageUp', 'Home', 'End', 'ArrowDown', 'ArrowUp'].includes(e.key) && !(e.target && ['INPUT', 'TEXTAREA'].includes(e.target.tagName))) this.userScrolled(); });
      this.restorePosition();
    },
    // `--sheet-h` (§3): the intrinsic size of an unmounted shell, so 800 shells cost nothing and the scrollbar is stable.
    updateSheetHeight() {
      if (!stackEl || !stackEl.style) return;
      const stackW = stackEl.clientWidth || 0;
      if (!stackW) return;
      const wide = (window.innerWidth || 1200) >= 900;
      const paneW0 = Math.min(stackW, 1180);
      const paneW = wide ? (paneW0 - 16) / 2 : paneW0;
      const h = paneW / this.medianAspect + 36 + 8;
      stackEl.style.setProperty('--sheet-h', `${Math.round(h)}px`);
    },
    get medianAspect() {
      const ratios = [];
      pages.forEach((p) => {
        const s = sheets.get(id(p));
        const w = (s && s.width) || p.width;
        const hgt = (s && s.height) || p.height;
        if (w > 0 && hgt > 0) ratios.push(w / hgt);
      });
      ratios.sort((a, b) => a - b);
      return ratios.length ? ratios[Math.floor(ratios.length / 2)] : 0.7;
    },
    aspectOf(p) {
      const s = sheets.get(id(p));
      const w = (s && s.width) || p.width;
      const h = (s && s.height) || p.height;
      if (w > 0 && h > 0) return `${w} / ${h}`;
      return `${this.medianAspect.toFixed(4)} / 1`;
    },
    ensureObservers() {
      if (nearObserver || typeof IntersectionObserver !== 'function') return;
      nearObserver = new IntersectionObserver((entries) => {
        entries.forEach((e) => { if (e.isIntersecting) this.mountSheet(e.target); });
      }, { rootMargin: NEAR_MARGIN });
      farObserver = new IntersectionObserver((entries) => {
        entries.forEach((e) => { if (!e.isIntersecting) this.unmountSheet(e.target); });
      }, { rootMargin: FAR_MARGIN });
      tileObserver = new IntersectionObserver((entries) => {
        entries.forEach((e) => {
          e.target.classList.toggle('is-near', e.isIntersecting);
          const p = pages.get(e.target.dataset.pageId);
          if (e.isIntersecting && p && p.stale) this.queueSheet(p.number);
        });
      }, { rootMargin: TILE_MARGIN });
    },
    observeSheet(el) {
      if (nearObserver) { nearObserver.observe(el); farObserver.observe(el); }
      else this.mountSheet(el); // no observer: mount everything (old engines)
    },
    tileNear(pid) {
      const el = tiles.get(String(pid));
      return Boolean(el && el.classList && el.classList.contains('is-near'));
    },
    mountSheet(el) {
      const pid = el && el.dataset ? String(el.dataset.pageId) : '';
      if (!pid || mounted.has(pid)) return;
      mounted.add(pid);
      const p = pages.get(pid) || {};
      const body = q(el, '.sheet-body');
      if (body && bodyTpl && bodyTpl.content) {
        body.appendChild(bodyTpl.content.cloneNode(true));
        this.wireBody(el, p);
        const D = decode();
        const fac = q(body, '.sheet-fac');
        if (D && fac && typeof D.sheet === 'function') {
          const h = D.sheet(fac, { scan: q(body, '.sheet-lines'), figure: q(body, '.sheet-scan') });
          handles.set(pid, h);
          h.update({ page: sheets.has(pid) && !p.stale ? this.sheetFor(pid) : p, active: this.active });
        }
      }
      if (el.classList) el.classList.add('is-near');
      if (!sheets.has(pid) || p.stale) this.queueSheet(p.number);
    },
    unmountSheet(el) {
      const pid = el && el.dataset ? String(el.dataset.pageId) : '';
      if (!pid || !mounted.has(pid)) return;
      mounted.delete(pid);
      const h = handles.get(pid);
      if (h) h.destroy();
      handles.delete(pid);
      const body = q(el, '.sheet-body');
      if (body) body.textContent = '';
      if (el.classList) el.classList.remove('is-near');
      if (focusedId === pid) focusedId = null;
    },
    handle(pid) {
      return handles.get(String(pid)) || null;
    },
    // Images and the designed-state forms of a mounted body.
    wireBody(el, p) {
      const body = q(el, '.sheet-body');
      if (!body) return;
      const s = sheets.get(id(p));
      const scanImg = q(body, '.sheet-img-scan');
      const cleanImg = q(body, '.sheet-img-clean');
      const scanSrc = (s && (s.scan_thumb_url || s.scan_url)) || p.scan_thumb_url || '';
      const cleanSrc = (s && s.display_url) || '';
      if (scanImg) {
        if (scanSrc && scanImg.getAttribute('src') !== scanSrc) scanImg.setAttribute('src', scanSrc);
        setHidden(scanImg, !scanSrc);
      }
      if (cleanImg) {
        if (cleanSrc && cleanImg.getAttribute('src') !== cleanSrc) {
          cleanImg.classList.remove('is-ready');
          cleanImg.setAttribute('src', cleanSrc);
          cleanImg.setAttribute('alt', `الصفحة ${p.number} بعد المعالجة`);
          cleanImg.onload = () => cleanImg.classList.add('is-ready');
          if (cleanImg.complete && cleanImg.naturalWidth) cleanImg.classList.add('is-ready');
        }
        setHidden(cleanImg, !cleanSrc);
      }
      const restore = q(body, '.fac-restore');
      if (restore) restore.setAttribute('action', p.exclude_url || fill(urls.exclude, p.number));
      const retry = q(body, '.fac-retry');
      if (retry) {
        retry.setAttribute('action', p.rerun_url || fill(urls.rerun, p.number));
        const stage = q(retry, 'input[name="stage"]');
        if (stage) stage.value = p.retry_stage || '';
        setText(q(retry, '.fac-retry-label'), p.retry_label || '');
        setHidden(retry, !(p.error && p.retry_stage));
      }
      setText(q(body, '.fac-error-text'), p.error_headline || (p.error && typeof p.error === 'string' ? p.error : '') || 'فشلت معالجة هذه الصفحة.');
    },
    // The sheet head, patched from a page record (no Alpine bindings inside the shell).
    patchSheet(el, p) {
      if (!el || typeof el.querySelector !== 'function') return;
      el.id = `sheet-${p.number}`;
      el.dataset.pageId = id(p);
      el.dataset.number = String(p.number);
      el.dataset.status = p.status || '';
      el.dataset.text = p.text_state || '';
      el.classList.toggle('is-excluded', Boolean(p.is_excluded));
      el.classList.toggle('is-error', Boolean(p.error));
      const ar = this.aspectOf(p);
      if (el.style.getPropertyValue('--sheet-ar') !== ar) el.style.setProperty('--sheet-ar', ar);
      const title = q(el, '.sheet-title');
      if (title) { title.setAttribute('href', p.url || '#'); setText(q(title, 'bdi'), p.number); }
      const dot = q(el, '.sheet-head .dot');
      if (dot) dot.className = `dot ${p.dot || 'dot-neutral'}`;
      setText(q(el, '.sheet-status'), p.status_label || '');
      setHidden(q(el, '.bk-provisional'), p.text_state !== 'provisional');
      const printed = q(el, '.sheet-printed');
      if (printed) { setHidden(printed, !p.printed_number); setText(q(printed, 'bdi'), p.printed_number || ''); }
      const unresolved = q(el, '.sheet-unresolved');
      if (unresolved) { setHidden(unresolved, p.is_reviewed || !(p.n_unresolved > 0)); setText(q(unresolved, 'bdi'), p.n_unresolved || 0); }
      setHidden(q(el, '.bk-reviewed'), !p.is_reviewed);
      setHidden(q(el, '.sheet-excluded'), !p.is_excluded);
      const flags = q(el, '.sheet-flags');
      if (flags) {
        flags.textContent = '';
        (p.flag_labels || []).forEach((label) => { const b = document.createElement('span'); b.className = 'badge badge-warning'; b.textContent = label; flags.appendChild(b); });
        if (p.sequence_issue) { const b = document.createElement('span'); b.className = 'badge badge-warning num'; b.textContent = p.sequence_issue; flags.appendChild(b); }
      }
      const review = q(el, '.sheet-review');
      if (review) { review.setAttribute('href', p.review_url || '#'); setHidden(review, !(p.text_state === 'final' && !p.is_excluded)); }
      const copy = q(el, '.sheet-copy');
      if (copy) { setHidden(copy, !this.canCopy(id(p))); copy.setAttribute('aria-label', `نسخ نص الصفحة ${p.number}`); copy.onclick = () => this.copySheet(id(p)); }
      const retry = q(el, '.sheet-retry');
      if (retry) {
        retry.setAttribute('action', p.rerun_url || '');
        const stage = q(retry, 'input[name="stage"]');
        if (stage) stage.value = p.retry_stage || '';
        setText(q(retry, '.sheet-retry-label'), p.retry_label || '');
        setHidden(retry, !(p.error && p.retry_stage));
      }
      setHidden(el, !this.matches(p));
      if (mounted.has(id(p))) this.wireBody(el, p);
    },
    patchTile(el, p) {
      if (!el || typeof el.querySelector !== 'function') return;
      el.dataset.pageId = id(p);
      el.dataset.number = String(p.number);
      el.dataset.status = p.status || '';
      el.classList.toggle('is-excluded', Boolean(p.is_excluded));
      el.classList.toggle('is-error', Boolean(p.error));
      const link = q(el, '.page-tile-link');
      if (link) {
        link.setAttribute('href', p.url || '#');
        link.setAttribute('title', `الصفحة ${p.number} — ${p.status_label || ''}`);
        link.setAttribute('aria-label', `الصفحة ${p.number} — ${p.status_label || ''}`);
      }
      const s = sheets.get(id(p));
      const scanSrc = (s && s.scan_thumb_url) || p.scan_thumb_url || '';
      const cleanSrc = (s && s.thumb_url) || p.thumb_url || '';
      const scanImg = q(el, '.page-tile-scan');
      const cleanImg = q(el, '.page-tile-clean');
      if (scanImg) { if (scanSrc && scanImg.getAttribute('src') !== scanSrc) scanImg.setAttribute('src', scanSrc); setHidden(scanImg, !scanSrc); }
      if (cleanImg) {
        if (cleanSrc && cleanImg.getAttribute('src') !== cleanSrc) {
          cleanImg.classList.remove('is-ready');
          cleanImg.setAttribute('src', cleanSrc);
          cleanImg.onload = () => cleanImg.classList.add('is-ready');
          if (cleanImg.complete && cleanImg.naturalWidth) cleanImg.classList.add('is-ready');
        }
        setHidden(cleanImg, !cleanSrc);
      }
      setHidden(q(el, '.page-tile-placeholder'), Boolean(scanSrc || cleanSrc));
      const flag = q(el, '.page-tile-flag');
      if (flag) { setHidden(flag, !(p.n_flags > 0)); flag.setAttribute('title', (p.flag_labels || []).join('، ')); setText(q(flag, '.tile-nflags'), p.n_flags || 0); }
      setHidden(q(el, '.page-tile-excluded'), !p.is_excluded);
      setHidden(q(el, '.tile-mark-check'), !p.is_reviewed);
      const count = q(el, '.tile-mark-count');
      if (count) { setHidden(count, p.is_reviewed || !(p.n_unresolved > 0)); setText(count, p.n_unresolved || 0); count.setAttribute('title', `${p.n_unresolved || 0} كلمة غير مؤكَّدة`); }
      const stage = q(el, '.tile-stage');
      if (stage) {
        stage.classList.toggle('is-done', DONE_STATUSES.includes(p.status) || Boolean(p.is_excluded));
        stage.classList.toggle('is-error', Boolean(p.error));
        const fillEl = q(stage, '.tile-stage-fill');
        if (fillEl) fillEl.style.width = `${this.stagePercent(p)}%`;
      }
      setText(q(el, '.tile-number'), p.number);
      const dot = q(el, '.page-tile-footer .dot');
      if (dot) dot.className = `dot ${p.dot || 'dot-neutral'}`;
      const toggle = q(el, '.page-tile-toggle');
      if (toggle) {
        toggle.setAttribute('action', p.exclude_url || fill(urls.exclude, p.number));
        const btn = q(toggle, '.tile-toggle');
        if (btn) {
          btn.setAttribute('title', p.is_excluded ? 'إعادة الصفحة إلى الكتاب' : 'استثناء الصفحة من الكتاب');
          btn.setAttribute('aria-label', p.is_excluded ? `إعادة الصفحة ${p.number} إلى الكتاب` : `استثناء الصفحة ${p.number} من الكتاب`);
        }
        setHidden(q(toggle, '.tile-icon-exclude'), Boolean(p.is_excluded));
        setHidden(q(toggle, '.tile-icon-restore'), !p.is_excluded);
      }
      setHidden(el, !this.matches(p));
    },

    // ------------------------------------------------------------ hover / focus / click linking (§3)
    onLineOver(e) {
      const line = e.target && e.target.closest ? e.target.closest('[data-line]') : null;
      if (!line) return;
      const art = line.closest('.page-sheet');
      const h = art ? handles.get(art.dataset.pageId) : null;
      if (h) h.hot(Number(line.getAttribute('data-line')));
    },
    onLineOut(e) {
      const line = e.target && e.target.closest ? e.target.closest('[data-line]') : null;
      if (!line) return;
      const to = e.relatedTarget;
      if (to && to.closest && to.closest('[data-line]') === line) return;
      const art = line.closest('.page-sheet');
      const h = art ? handles.get(art.dataset.pageId) : null;
      if (h) h.hot(-1);
    },
    onStackClick(e) {
      if (!e.target || !e.target.closest || e.target.closest('a, button, form, input')) return;
      const line = e.target.closest('[data-line]');
      if (!line) return;
      const art = line.closest('.page-sheet');
      const p = art ? pages.get(art.dataset.pageId) : null;
      if (p && art.dataset.text === 'final' && p.review_url && !p.is_excluded) window.location.assign(p.review_url);
    },

    // ------------------------------------------------------------ sheets fetch queue (batched ≤ 40)
    queueSheet(number) {
      if (!this.sheetsUrl || !(number > 0)) return;
      pending.add(Number(number));
      clearTimeout(flushTimer);
      flushTimer = setTimeout(() => this.flushSheets(), SHEET_FLUSH_MS);
    },
    refetchSheet(number) {
      this.queueSheet(number);
    },
    batchRanges,
    async flushSheets() {
      if (!this.sheetsUrl || !pending.size) return;
      const ranges = batchRanges([...pending]);
      pending.clear();
      await Promise.all(ranges.map(([from, to]) => this.fetchSheets(from, to)));
    },
    async fetchSheets(from, to) {
      const sep = this.sheetsUrl.includes('?') ? '&' : '?';
      try {
        const res = await fetch(`${this.sheetsUrl}${sep}from=${from}&to=${to}`, {
          headers: { Accept: 'application/json' },
          credentials: 'same-origin',
          cache: 'no-store',
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        this.applySheets(await res.json());
        this.sheetsFailed = false;
      } catch (e) {
        this.sheetsFailed = true;
      }
    },
    applySheets(data) {
      const items = Array.isArray(data) ? data : (data && (data.pages || data.sheets || data.results)) || [];
      if (data && Number(data.book_line_h_px) > 0) bookLineH = Number(data.book_line_h_px);
      items.forEach((s) => {
        if (!s || s.id == null) return;
        const pid = String(s.id);
        sheets.set(pid, s);
        const p = pages.get(pid);
        if (p) {
          p.stale = false;
          if (s.width > 0) p.width = s.width;
          if (s.height > 0) p.height = s.height;
          this.patchSheet(shells.get(pid), p);
          this.patchTile(tiles.get(pid), p);
        }
        const h = handles.get(pid);
        if (h) h.update({ page: this.sheetFor(pid), active: this.active });
      });
    },
    lineBoxes(pid) {
      const s = sheets.get(String(pid));
      return s && Array.isArray(s.line_boxes) ? s.line_boxes : [];
    },

    // ------------------------------------------------------------ copy
    sheetText(pid) {
      const s = sheets.get(String(pid));
      if (!s) return '';
      if (s.text_state === 'final') return sheetLinesText(s.lines) || s.final_text || '';
      return '';
    },
    canCopy(pid) {
      const p = pages.get(String(pid));
      return Boolean(p) && !p.is_excluded && this.sheetText(pid) !== '';
    },
    copySheet(pid) {
      const text = this.sheetText(pid);
      if (!text) return Promise.resolve(false);
      return window.Nassakh.copyText(text);
    },
    // The request starts inside the click so the clipboard write keeps the user gesture.
    async copyBook() {
      const url = this.bookTextUrl;
      if (!url || this.copying) return;
      this.copying = true;
      const text = fetch(url, { headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store' })
        .then((res) => {
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          return res.json();
        })
        .then((data) => (data && data.text) || '');
      try {
        await window.Nassakh.copyText(text);
      } finally {
        this.copying = false;
      }
    },
    };
  });

  const readJson = (id) => {
    const el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  };

  // ---------------------------------------------------------------- page detail viewer
  const TAB_LABELS = { original: 'الأصل', gray: 'المعالَجة', bw: 'أبيض وأسود' };
  const TAB_KEYS = { 1: 'original', 2: 'gray', 3: 'bw' };

  Alpine.data('pageDetail', (cfg = {}) => ({
    images: cfg.images || {},
    regions: cfg.regions || [],
    size: cfg.size || null, // [width, height] of the gray image (region coordinate space)
    prevUrl: cfg.prevUrl || null,
    nextUrl: cfg.nextUrl || null,
    tab: 'original',
    showRegions: true,
    state: readJson('page-state') || {}, // /api/pages/<id>/status/ payload, kept live by the text panel poll
    stageLabels: readJson('page-stage-labels') || {},
    live: false, // true once a status poll has arrived (the server-rendered flags step aside)

    init() {
      const wanted = cfg.initialTab || 'gray';
      this.tab = this.has(wanted) ? wanted : (this.has('original') ? 'original' : wanted);
    },
    // Designed failure: `error` = Arabic headline, `error_detail` = technical detail.
    get errorLines() {
      return String(this.state.error || '').split('\n');
    },
    get errorHeadline() {
      return this.errorLines[0].trim() || 'فشلت معالجة هذه الصفحة.';
    },
    get errorDetail() {
      // The status API sends the detail in `error_detail`; older payloads carried it after line 1.
      if (this.state.error_detail) return String(this.state.error_detail).trim();
      return this.errorLines.slice(1).join('\n').trim();
    },
    get retryStage() {
      const stage = this.state.error_from || '';
      return this.stageLabels[stage] ? stage : '';
    },
    get errorStageLabel() {
      return this.stageLabels[this.state.error_from] || this.state.error_from || '';
    },
    // The text panel re-broadcasts each status poll, so the state card never contradicts it.
    onPageState(detail) {
      if (!detail || String(detail.id) !== String(cfg.pageId)) return;
      const before = this.state.status;
      this.state = { ...this.state, ...detail };
      this.live = true;
      const hadGray = this.has('gray');
      if (detail.images) this.onPageUpdated({ pageId: cfg.pageId, images: detail.images });
      if (before === 'uploaded' && detail.status !== 'uploaded' && detail.status !== 'error' && !hadGray) {
        // Preprocessing just finished: reload once so the gray-image size and regions arrive too.
        window.location.reload();
      }
    },
    has(key) {
      return Boolean(this.images[key]);
    },
    select(key) {
      if (this.has(key)) this.tab = key;
    },
    get src() {
      return this.images[this.tab] || null;
    },
    get alt() {
      return `صورة الصفحة — ${TAB_LABELS[this.tab] || ''}`;
    },
    get overlayVisible() {
      return this.showRegions && this.tab !== 'original' && Boolean(this.size) && this.regions.length > 0;
    },
    boxStyle(r) {
      if (!this.size || !r.bbox || r.bbox.length !== 4) return 'display:none';
      const [W, H] = this.size;
      const [x0, y0, x1, y1] = r.bbox;
      const pct = (v, total) => `${Math.max(0, Math.min(100, (100 * v) / total))}%`;
      // Physical `left`/`top`: image pixels do not flip with the writing direction.
      return `left:${pct(x0, W)};top:${pct(y0, H)};width:${pct(x1 - x0, W)};height:${pct(y1 - y0, H)}`;
    },
    // The preprocess panel (processing.js) dispatches this after a re-run: new derived images,
    // a new gray-image size (the region coordinate space) and the re-derived regions.
    onPageUpdated(detail) {
      if (!detail || String(detail.pageId) !== String(cfg.pageId)) return;
      const images = detail.images || {};
      if (images.display || images.gray) this.images = { ...this.images, gray: images.display || images.gray };
      if (images.bw) this.images = { ...this.images, bw: images.bw };
      if (detail.output && detail.output.width && detail.output.height) {
        this.size = [detail.output.width, detail.output.height];
      }
      if (Array.isArray(detail.regions)) this.regions = detail.regions;
      if (this.tab === 'original' && this.has('gray')) this.tab = 'gray';
    },
    // Keyboard follows the RTL reading direction: → goes back to the previous page, ← forward.
    onKey(e) {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      const t = e.target;
      if (t && (['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName) || t.isContentEditable)) return;
      if (e.key === 'ArrowRight' && this.prevUrl) {
        e.preventDefault();
        window.location.assign(this.prevUrl);
      } else if (e.key === 'ArrowLeft' && this.nextUrl) {
        e.preventDefault();
        window.location.assign(this.nextUrl);
      } else if (TAB_KEYS[e.key]) {
        this.select(TAB_KEYS[e.key]);
      }
    },
  }));
});
