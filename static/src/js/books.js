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
  // Two views (D25): «صفحات» stacks lazily mounted sheets (scan + generating text), «شبكة» shows the
  // animated processing cards. Both are driven by the same progress poll; continuous effects come from
  // window.NassakhDecode (one rAF loop, paused off-screen, off under prefers-reduced-motion).
  const VIEW_KEY = 'nassakh.bookView';
  const SHEET_BATCH = 40; // api:book_sheets serves at most 40 pages per call
  const NEAR_MARGIN = '1500px'; // a sheet this close to the viewport mounts and fetches its data
  const FAR_MARGIN = '4000px'; // and unmounts again once it is this far away (800-page books stay light)
  const SHEET_FLUSH_MS = 60;
  const SWEEP_MS = 2400; // one pass of the scan sweep
  const SHIMMER_LINE_MS = 170; // one detected line lights up after another at this pace
  const STAGE_PERCENT = { uploaded: 12, preprocessed: 46, layout_done: 72, ocr_done: 100, reviewed: 100, assembled: 100 };
  const DONE_STATUSES = ['ocr_done', 'reviewed', 'assembled'];
  const REVIEWED_STATUSES = ['reviewed', 'assembled'];

  function readView() {
    try {
      return localStorage.getItem(VIEW_KEY) === 'grid' ? 'grid' : 'sheets';
    } catch (e) {
      return 'sheets';
    }
  }
  function storeView(view) {
    try { localStorage.setItem(VIEW_KEY, view); } catch (e) { /* private mode: the default is fine */ }
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
  // The scan sweep: a band of light moving down the scan (theatre.css .scan-sweep uses --sweep 0..1).
  function sweepEffect(t, el) {
    el.style.setProperty('--sweep', ((t % SWEEP_MS) / SWEEP_MS).toFixed(3));
  }
  // The line-reading shimmer: children of `.sheet-lines` light up in reading order with a short trail.
  function shimmerEffect(t, el) {
    const layer = el.querySelector('.sheet-lines');
    const boxes = layer ? layer.children : [];
    const n = boxes.length;
    if (!n) return;
    const idx = Math.floor(t / SHIMMER_LINE_MS) % n;
    if (el._litIndex === idx) return;
    el._litIndex = idx;
    for (let i = 0; i < n; i += 1) {
      const d = (idx - i + n) % n;
      const cls = boxes[i].classList;
      cls.toggle('is-lit', d === 0);
      cls.toggle('is-lit-2', d === 1);
      cls.toggle('is-lit-3', d === 2);
    }
  }
  const sheetLinesText = (lines) => {
    const body = [];
    const notes = [];
    (lines || []).forEach((line) => {
      const text = (line.tokens || []).map((tok) => tok.t).join(' ');
      (line.region_kind === 'footnote' ? notes : body).push(text);
    });
    return notes.length ? `${body.join('\n')}\n\n${notes.join('\n')}` : body.join('\n');
  };

  Alpine.data('bookDashboard', (cfg = {}) => {
    // Non-reactive plumbing lives in the closure: observers, the fetch queue and timers.
    let nearObserver = null;
    let farObserver = null;
    const pending = new Set(); // page numbers waiting for a sheets request
    let flushTimer = null;

    return {
    progressUrl: cfg.progressUrl,
    sheetsUrl: cfg.sheetsUrl || '',
    reviewNextUrl: cfg.reviewNextUrl || '',
    active: Boolean(cfg.active),
    wasActive: Boolean(cfg.active),
    total: cfg.total || 0,
    percent: cfg.percent || 0,
    flags: cfg.flags || 0,
    status: cfg.status || '',
    statusLabel: cfg.statusLabel || '',
    dot: cfg.dot || 'dot-neutral',
    byStatus: cfg.byStatus || {},
    review: cfg.review || null, // {reviewed, total, unresolved_total, next_review_url} from the progress poll
    stageMap: Object.fromEntries((cfg.stages || []).map((s) => [s.key, s.statuses])),
    pages: {},
    sheets: {}, // page id -> api:book_sheets payload (only for sheets that came near the viewport)
    near: {}, // page id -> true while its sheet body is mounted
    initialIds: new Set((cfg.pages || []).map((p) => String(p.id))),
    view: readView(),
    liveMessage: '', // one polite announcement for the latest page state change
    timer: null,
    failures: 0,
    stopped: false,
    fetchFailed: false, // shown after a few consecutive failed polls
    authLost: false,
    copying: false,
    sheetsFailed: false,

    init() {
      (cfg.pages || []).forEach((p) => { this.pages[p.id] = p; });
      if (this.active) this.schedule(POLL_INTERVAL);
      this.$watch && this.$watch('view', (view) => { if (view !== 'sheets') this.resetSheets(); });
    },
    destroy() {
      clearTimeout(this.timer);
      clearTimeout(flushTimer);
      this.resetSheets();
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
      if (d.review) this.review = d.review;
      const changed = [];
      (d.pages || []).forEach((p) => {
        const before = this.pages[p.id];
        if (before && (before.status !== p.status || before.text_state !== p.text_state)) changed.push(p);
        this.pages[p.id] = p;
      });
      this.active = Boolean(d.active);
      if (changed.length) {
        const last = changed[changed.length - 1];
        this.liveMessage = `الصفحة ${last.number}: ${last.status_label}`;
        // a visible sheet whose state moved on refetches just itself (new image, new text, new lines)
        changed.filter((p) => this.near[p.id]).forEach((p) => this.refetchSheet(p.number));
      }
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
        .filter((p) => !p.is_excluded && (p.error || p.n_flags > 0 || p.sequence_issue))
        .sort((a, b) => a.number - b.number);
    },

    // ------------------------------------------------------------ review summary (§3)
    get anyOcrDone() {
      return this.done > 0;
    },
    // The progress payload carries `review` (its `next_review_url` is null when no page waits);
    // until it does, the summary is derived from the tiles and the template's guarded review:next URL.
    get reviewSummary() {
      if (this.review) {
        return { ...this.review, next_review_url: this.review.next_review_url || '' };
      }
      const included = Object.values(this.pages).filter((p) => !p.is_excluded);
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

    // ------------------------------------------------------------ view toggle
    setView(view) {
      this.view = view === 'grid' ? 'grid' : 'sheets';
      storeView(this.view);
    },

    // ------------------------------------------------------------ per-page state helpers
    isCleaning(p) {
      return this.active && p.status === 'uploaded' && !p.is_excluded && !p.error;
    },
    // Line boxes exist from preprocessing on; the shimmer runs from there until the text is final.
    isReading(p) {
      return this.active && (p.status === 'preprocessed' || p.status === 'layout_done') && !p.is_excluded && !p.error;
    },
    isDone(p) {
      return DONE_STATUSES.includes(p.status);
    },
    stagePercent(p) {
      if (p.error) return 100;
      return STAGE_PERCENT[p.status] || 0;
    },
    stageStyle(p) {
      return `width:${this.stagePercent(p)}%`;
    },

    // ------------------------------------------------------------ stacked sheets (§3)
    ensureObservers() {
      if (nearObserver || typeof IntersectionObserver !== 'function') return;
      nearObserver = new IntersectionObserver((entries) => {
        entries.forEach((e) => {
          if (!e.isIntersecting) return;
          const id = e.target.dataset.pageId;
          if (!this.near[id]) this.near[id] = true;
          this.wantSheet(id);
        });
      }, { rootMargin: NEAR_MARGIN });
      farObserver = new IntersectionObserver((entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) return;
          const id = e.target.dataset.pageId;
          if (this.near[id]) this.near[id] = false;
        });
      }, { rootMargin: FAR_MARGIN });
    },
    observeSheet(el, id) {
      el.dataset.pageId = String(id);
      this.ensureObservers();
      if (nearObserver) { nearObserver.observe(el); farObserver.observe(el); }
      else { this.near[id] = true; this.wantSheet(id); } // no observer: mount everything (tests, old engines)
    },
    resetSheets() {
      if (nearObserver) { nearObserver.disconnect(); farObserver.disconnect(); }
      nearObserver = null;
      farObserver = null;
      this.near = {};
    },
    sheet(id) {
      return this.sheets[id] || null;
    },
    wantSheet(id) {
      const p = this.pages[id];
      if (!p || this.sheets[id] || !this.sheetsUrl) return;
      this.queueSheet(p.number);
    },
    refetchSheet(number) {
      this.queueSheet(number);
    },
    queueSheet(number) {
      pending.add(Number(number));
      clearTimeout(flushTimer);
      flushTimer = setTimeout(() => this.flushSheets(), SHEET_FLUSH_MS);
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
      items.forEach((s) => { if (s && s.id != null) this.sheets[s.id] = s; });
    },
    // Aspect ratio of a page's scan for the placeholder: its own size, else the median of the known ones.
    aspectOf(p) {
      const s = this.sheets[p.id];
      const w = (s && s.width) || p.width;
      const h = (s && s.height) || p.height;
      if (w > 0 && h > 0) return `${w} / ${h}`;
      return this.defaultAspect;
    },
    get defaultAspect() {
      const ratios = Object.values(this.sheets)
        .filter((s) => s.width > 0 && s.height > 0)
        .map((s) => s.width / s.height)
        .sort((a, b) => a - b);
      if (!ratios.length) return '7 / 10';
      return `${ratios[Math.floor(ratios.length / 2)].toFixed(4)} / 1`;
    },
    lineBoxes(id) {
      const s = this.sheets[id];
      return s && Array.isArray(s.line_boxes) ? s.line_boxes : [];
    },
    // line_boxes are 0..1 ratios of the image; physical left/top because image pixels do not flip in RTL
    boxStyle(b) {
      if (!Array.isArray(b) || b.length !== 4) return 'display:none';
      const pct = (v) => `${(100 * Math.max(0, Math.min(1, v))).toFixed(2)}%`;
      return `left:${pct(b[0])};top:${pct(b[1])};width:${pct(b[2] - b[0])};height:${pct(b[3] - b[1])}`;
    },
    sheetText(id) {
      const s = this.sheets[id];
      if (!s) return '';
      if (s.text_state === 'final') return sheetLinesText(s.lines) || s.final_text || '';
      return '';
    },
    canCopy(p) {
      return this.sheetText(p.id) !== '';
    },
    copySheet(id) {
      return window.Nassakh.copyText(this.sheetText(id));
    },
    // Scan effects, bound with x-effect: sweep while cleaning, shimmer while reading, nothing otherwise.
    // `shimmer` false (grid tiles): tiles have no line boxes, so only the sweep runs there.
    syncScanFx(el, p, mounted = true, shimmer = true) {
      const D = window.NassakhDecode;
      if (!D) return;
      this.lineBoxes(p.id); // tracked: re-run when the boxes arrive
      if (mounted && this.isCleaning(p)) {
        el.classList.add('scan-sweep');
        if (el._fx !== 'sweep') { el._fx = 'sweep'; D.effect(el, sweepEffect); }
      } else if (mounted && shimmer && this.isReading(p)) {
        el.classList.remove('scan-sweep');
        if (el._fx !== 'shimmer') { el._fx = 'shimmer'; el._litIndex = -1; D.effect(el, shimmerEffect); }
      } else if (el._fx) {
        el._fx = '';
        el.classList.remove('scan-sweep');
        D.detach(el);
        Array.from(el.querySelectorAll('.sheet-line.is-lit, .sheet-line.is-lit-2, .sheet-line.is-lit-3'))
          .forEach((box) => box.classList.remove('is-lit', 'is-lit-2', 'is-lit-3'));
      }
    },
    // The text column, bound with x-effect: noise → scrambled Tesseract text → resolve wave → final lines.
    syncSheetText(el, p) {
      const D = window.NassakhDecode;
      if (!D) return;
      const s = this.sheets[p.id];
      const state = s ? s.text_state : p.text_state;
      const stillWorking = this.active && !p.error && !this.isDone(p);
      if (state === 'final' && s && Array.isArray(s.lines) && s.lines.length) {
        if (el._mode && el._mode !== 'final' && el._mode !== 'static') D.resolve(el, { lines: s.lines });
        else if (el._mode !== 'final') D.render(el, { lines: s.lines });
        el._mode = 'final';
        return;
      }
      if (state === 'provisional' && s && s.provisional_text) {
        const mode = stillWorking ? 'provisional' : 'static';
        if (el._mode === mode) D.update(el, { text: s.provisional_text, static: mode === 'static' });
        else D.attach(el, { text: s.provisional_text, mode: 'provisional', static: mode === 'static' });
        el._mode = mode;
        return;
      }
      if (stillWorking || !s) {
        const n = s ? (Array.isArray(s.line_boxes) && s.line_boxes.length) || s.n_lines || 10 : 10;
        if (el._mode !== 'noise' || el._noiseLines !== n) { D.attach(el, { mode: 'noise', lines: n }); el._noiseLines = n; }
        el._mode = 'noise';
        return;
      }
      if (el._mode) { D.detach(el); el.textContent = ''; el._mode = ''; }
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
