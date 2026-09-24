// Alpine components for the books screens: new-book form, dashboard polling, page detail viewer.
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

  // ---------------------------------------------------------------- book dashboard
  Alpine.data('bookDashboard', (cfg = {}) => ({
    progressUrl: cfg.progressUrl,
    active: Boolean(cfg.active),
    wasActive: Boolean(cfg.active),
    total: cfg.total || 0,
    percent: cfg.percent || 0,
    flags: cfg.flags || 0,
    status: cfg.status || '',
    statusLabel: cfg.statusLabel || '',
    dot: cfg.dot || 'dot-neutral',
    byStatus: cfg.byStatus || {},
    stageMap: Object.fromEntries((cfg.stages || []).map((s) => [s.key, s.statuses])),
    pages: {},
    initialIds: new Set((cfg.pages || []).map((p) => String(p.id))),
    timer: null,
    failures: 0,
    stopped: false,
    fetchFailed: false, // shown after a few consecutive failed polls
    authLost: false,
    copying: false,

    init() {
      (cfg.pages || []).forEach((p) => { this.pages[p.id] = p; });
      if (this.active) this.schedule(POLL_INTERVAL);
    },
    destroy() {
      clearTimeout(this.timer);
    },
    schedule(ms) {
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.poll(), ms);
    },
    async poll() {
      if (document.hidden) { // do not hammer the server from a background tab
        this.schedule(POLL_INTERVAL);
        return;
      }
      try {
        const res = await fetch(this.progressUrl, {
          headers: { Accept: 'application/json' },
          credentials: 'same-origin',
          cache: 'no-store',
        });
        if (res.status === 401 || res.status === 403) { // session ended: stop for good
          this.stopped = true;
          this.active = false;
          this.wasActive = false;
          this.authLost = true;
          this.fetchFailed = true;
          return;
        }
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        this.apply(await res.json());
        this.failures = 0;
        this.fetchFailed = false;
      } catch (err) {
        this.failures += 1;
        this.fetchFailed = this.failures >= FAILURES_BEFORE_NOTICE;
      }
      if (this.active) {
        this.schedule(Math.min(POLL_INTERVAL * (1 + this.failures), POLL_MAX_INTERVAL));
      } else if (this.wasActive) {
        // The run finished (or stopped for guides): reload once so actions and banners match.
        window.location.reload();
      }
    },
    apply(d) {
      this.total = d.total;
      this.percent = d.percent;
      this.flags = d.flags;
      this.status = d.status;
      this.statusLabel = d.status_label;
      this.dot = d.dot;
      this.byStatus = d.by_status || {};
      (d.pages || []).forEach((p) => { this.pages[p.id] = p; });
      this.active = Boolean(d.active);
    },

    tile(id) {
      return this.pages[id] || {};
    },
    // Pages that were not in the server-rendered first paint (ingested while the dashboard is open).
    get laterPages() {
      return Object.values(this.pages)
        .filter((p) => !this.initialIds.has(String(p.id)))
        .sort((a, b) => a.number - b.number);
    },
    get hasPages() {
      return Object.keys(this.pages).length > 0;
    },
    // Pages whose text is recognised (the plain "X من Y صفحة" progress).
    get done() {
      return this.count('ocr_done');
    },
    // The request starts inside the click so the clipboard write keeps the user gesture.
    async copyBook(url) {
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
    count(key) {
      const statuses = this.stageMap[key] || [key];
      return statuses.reduce((n, s) => n + (this.byStatus[s] || 0), 0);
    },
    barWidth(key) {
      const n = this.count(key);
      return `width:${this.total ? (100 * n) / this.total : 0}%`;
    },
    get attention() {
      return Object.values(this.pages)
        .filter((p) => !p.is_excluded && (p.error || p.n_flags > 0))
        .sort((a, b) => a.number - b.number);
    },
  }));

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
