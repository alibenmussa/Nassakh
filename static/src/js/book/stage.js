// The book page's stage (PHASE5_SPEC §9.2, D47): the dashboard viewer drawing live pages.
//   - the viewer: one page or a spread (recto on the left), turns (200 ms out, an entrance from the other side),
//     keys, wheel, touch swipe, jump, fit to the height, to the width or the trim at 96 dpi (= books.js, D33)
//   - live pages: each shown page is drawn from its layout as positioned text (geometry.js pageHtml); only the
//     pages shown are in the DOM, the layouts around them are fetched in windows from `api:preview_layout`
//   - the live layout's revision: a re-layout's pages are spliced in place (later pages renumbered, sides
//     swapped on an odd delta), a newer revision (the book render adopted) refetches the pages shown
//   - the footprint, the page checks, the render pill, the polling of `api:preview`, the filmstrip thumbs
// Hooks the other parts answer: paintOpts(n) (what a page draws besides its lines), afterPaint(), afterLand(),
// afterRelayout(info).
(function () {
  'use strict';

  const root = typeof window !== 'undefined' ? window : globalThis;
  const NS = (root.NassakhBook = root.NassakhBook || {});
  NS.parts = NS.parts || {};

  const POLL_MS = 1000; // while a render or a re-layout is on
  const POLL_HIDDEN_MS = 2500;
  const POLL_MAX_MS = 15000;
  const FAILURES_BEFORE_NOTICE = 3;
  const TURN_OUT_MS = 200; // = books.js (D33): 200 ms out + a two-frame entrance
  const WHEEL_TURN_X = 50;
  const WHEEL_TURN_Y = 90;
  const WHEEL_IDLE_MS = 260;
  const SWIPE_PX = 50;
  const FILM_EDGE_PX = 6;
  const SPREAD_MIN_PX = 640;
  const SPREAD_GAP_PX = 12;
  const THUMBS_LATER_MS = 2500; // the images of re-laid-out pages follow in a low-priority task
  const THUMB_TRIES = 4;
  const ACTIVE = ['queued', 'running'];
  const FIT_MODES = ['height', 'width', 'actual'];
  const FIT_KEY = 'nassakh.book.fit';
  const SPREAD_KEY = 'nassakh.book.spread';
  const PAGE_KEY = 'nassakh.book.page.'; // the page shown, per book (session)
  const PAGE_FORMS = ['صفحة واحدة', 'صفحتان', 'صفحات', 'صفحة'];
  const RENDER_ERROR = 'تعذّر إخراج صفحات الكتاب.';
  const SKELETON = '<div class="lp-skeleton" aria-hidden="true"><span class="lp-sk-title"></span><span style="width:96%"></span><span style="width:92%"></span><span style="width:88%"></span><span style="width:61%"></span><span style="width:90%"></span><span style="width:95%"></span><span style="width:84%"></span><span style="width:93%"></span><span style="width:40%"></span></div>';

  const isActive = (p) => Boolean(p && (ACTIVE.includes(p.status) || p.rendering));

  NS.parts.stage = function stage(ctx) {
    const U = NS.util;
    const G = NS.geo;
    const cfg = ctx.cfg;
    const urls = ctx.urls;
    const T = ctx.timers;
    const thumbs = []; // index → {el, img, num}: static thumbs patched by hand
    const inflight = new Set(); // "from-to" of layout fetches on the wire
    let pendingTarget = -1;
    let pendingFocus = false;
    let wheelAcc = 0;
    let wheelLock = false;
    let swipeStart = null;
    let pendingN = 0;
    let fetchGen = 0;
    let requestedStale = false;
    let thumbTries = 0;

    return {
      bookId: cfg.bookId,
      title: cfg.title || '',
      canEdit: Boolean(cfg.canEdit),
      urls,
      chapters: cfg.chapters || [], // [{id, number, kind, title}] in the manuscript's order
      summaries: cfg.chapterSummaries || [], // [{id, version, words, pages, source_pages, drift}]
      // the live layout
      revision: null,
      geometry: {},
      pageCount: 0,
      firstPage: 1,
      ranges: [], // [{id, title, first, last, version}]
      checks: [],
      fontCss: cfg.fontCss || '',
      stale: false, // the live layout is older than the manuscript
      // the viewer
      pages: [], // the sequence: [{n, chapter}]
      cursor: -1,
      turning: '',
      spread: U.readLocal(SPREAD_KEY, '0') === '1',
      fit: FIT_MODES.includes(U.readLocal(FIT_KEY, 'height')) ? U.readLocal(FIT_KEY, 'height') : 'height',
      stageWide: true,
      painted: 0, // bumps after every paint (the template's derived state follows it)
      liveMessage: '',
      // the book render (api:preview) and the re-layouts
      book: null,
      relayout: { state: '', id: null, error: '' }, // '' | running | error
      delta: 0,
      chapterDeltas: {},
      pollState: 'ok', // ok | error | auth
      failures: 0,
      stopped: false,
      retrying: false,

      _init_stage() {
        const initial = ctx.initial;
        if (initial.preview) this.applyPreview(initial.preview, { quiet: true });
        if (initial.layout) this.seedLayout(initial.layout, { reset: true });
        this.bindStage();
        this.restorePosition();
      },
      _destroy_stage() {
        this.stopped = true;
      },

      // ------------------------------------------------------------ the live layout, fetched in windows
      seedLayout(payload, opts = {}) {
        if (!payload) return false;
        if (opts.reset || (payload.revision !== null && payload.revision !== undefined && payload.revision !== this.revision)) ctx.pages.clear();
        if (payload.revision !== null && payload.revision !== undefined) this.revision = payload.revision;
        if (payload.geometry) this.geometry = payload.geometry;
        if (payload.font_css) this.fontCss = payload.font_css;
        this.pageCount = Number(payload.page_count) || 0;
        this.firstPage = Number(payload.first_page) || 1;
        if (Array.isArray(payload.chapters)) this.ranges = payload.chapters.map((c) => Object.assign({}, c));
        if (Array.isArray(payload.checks)) this.checks = payload.checks.slice();
        this.stale = Boolean(payload.stale);
        (payload.pages || []).forEach((page) => {
          ctx.pages.set(page.n, page);
          if (page.url) ctx.thumbs.set(page.n, { url: page.url, url2x: page.url2x });
        });
        this.rebuildSequence();
        return true;
      },
      rebuildSequence() {
        const out = [];
        for (let i = 0; i < this.pageCount; i += 1) {
          const n = this.firstPage + i;
          const r = this.rangeAt(n);
          out.push({ n, chapter: r ? r.id : null });
        }
        const wasN = this.current;
        this.pages = out;
        if (wasN) {
          const idx = out.findIndex((p) => p.n === wasN);
          this.cursor = idx >= 0 ? idx : Math.min(this.cursor, out.length - 1);
        } else if (out.length && this.cursor < 0) {
          this.cursor = -1;
        }
        this.syncFilm();
      },
      get lastPage() { return this.firstPage + this.pageCount - 1; },
      layoutUrl(from, to) {
        const base = urls.pageLayout || '';
        const sep = base.includes('?') ? '&' : '?';
        return `${base}${sep}from=${from}&to=${to}`;
      },
      // The pages around `n` that are not held yet, in one request; a newer revision in the answer resets the
      // cache (the pages shown are drawn again from it).
      async ensureAround(n) {
        if (!urls.pageLayout || !n || this.revision === null) return false;
        const span = G.around(n, this.firstPage, this.lastPage, this.spreadOn);
        if (!span) return false;
        let [lo, hi] = span;
        while (lo <= hi && ctx.pages.has(lo)) lo += 1;
        while (hi >= lo && ctx.pages.has(hi)) hi -= 1;
        if (lo > hi) return false;
        return this.fetchLayout(lo, hi);
      },
      async fetchLayout(from, to, opts = {}) {
        const key = `${from}-${to}-${opts.reset ? 'r' : ''}`;
        if (inflight.has(key)) return false;
        inflight.add(key);
        const gen = opts.reset ? (fetchGen += 1) : fetchGen;
        const r = await U.api(this.layoutUrl(from, to));
        inflight.delete(key);
        if (!r.ok || !r.data) return false;
        if (gen !== fetchGen) return false; // a reset after this request started: its answer is older
        const data = r.data;
        if (opts.reset || data.revision !== this.revision) {
          // an answer older than the pages held (a re-layout was spliced in while it was on the wire) never
          // replaces them: the page would show the text from before the edit
          if (this.revision !== null && data.revision !== null && data.revision !== undefined && data.revision < this.revision) return false;
          // a window fetched ahead found a newer revision (the book render was adopted): seeding the cache with
          // that window alone would leave the pages shown as skeletons; they are fetched again from it instead,
          // the old ones drawn meanwhile
          if (!opts.reset && this.revision !== null) { this.refetchShown(); return false; }
          this.seedLayout(data, { reset: true });
          if (typeof this.afterRevision === 'function') this.afterRevision(data);
        } else {
          (data.pages || []).forEach((page) => {
            ctx.pages.set(page.n, page);
            if (page.url) ctx.thumbs.set(page.n, { url: page.url, url2x: page.url2x });
          });
          this.pageCount = Number(data.page_count) || this.pageCount;
          if (Array.isArray(data.chapters)) this.ranges = data.chapters.map((c) => Object.assign({}, c));
          if (Array.isArray(data.checks)) this.checks = data.checks.slice();
          if (this.pages.length !== this.pageCount) this.rebuildSequence();
          this.syncFilm();
        }
        if (this.cursor < 0 && this.pages.length) this.landFirst();
        this.paint();
        return true;
      },
      // The pages shown again from the server (another revision, a whole-book layout, a page setup change).
      refetchShown() {
        const n = this.current || pendingN || this.firstPage;
        const span = G.around(n, this.firstPage, Math.max(this.lastPage, n + 1), this.spreadOn) || [n, n];
        return this.fetchLayout(span[0], span[1], { reset: true });
      },

      // ------------------------------------------------------------ drawing the pages shown
      bindStage() {
        const el = this.$el || (U.hasDOM ? document.querySelector('[data-book]') : null);
        if (!el || typeof el.querySelector !== 'function') return;
        ctx.dom.root = el;
        ctx.dom.canvas = el.querySelector('[data-canvas]');
        ctx.dom.film = el.querySelector('[data-film-track]');
        ctx.dom.sheets = {};
        ['right', 'left'].forEach((side) => {
          const sheet = el.querySelector(`[data-sheet="${side}"]`);
          if (!sheet) return;
          ctx.dom.sheets[side] = { sheet, page: U.q(sheet, '.lp-page'), lines: U.q(sheet, '.lp-lines'), host: U.q(sheet, '.lp-edit') };
        });
        if (ctx.dom.film && typeof ctx.dom.film.addEventListener === 'function') {
          ctx.dom.film.addEventListener('click', (e) => {
            const thumb = U.closest(e.target, '.lo-thumb');
            if (thumb) this.showIndex(Number(thumb.dataset.index), { manual: true });
          });
        }
        this.onResize();
        this.syncFilm();
      },
      // The page numbers on the two sheets now (0 = none).
      get shownNumbers() {
        void this.cursor; void this.spreadOn; void this.pages.length;
        return G.shown(this.pages.map((p) => p.n), this.cursor, this.spreadOn);
      },
      sideOf(n) {
        const s = this.shownNumbers;
        return s.right === n ? 'right' : s.left === n ? 'left' : '';
      },
      pageOf(n) { return ctx.pages.get(n) || null; },
      // Every sheet shown draws its page: its lines (and what the other parts add: find highlights, an open
      // paragraph's lines hidden, the uncertain words in edit mode…), or a skeleton while its layout is on the
      // wire. The editor host of a sheet is never touched here.
      paint() {
        const sheets = ctx.dom.sheets || {};
        const s = this.shownNumbers;
        ['right', 'left'].forEach((side) => {
          const host = sheets[side];
          if (!host || !host.lines) return;
          const n = s[side];
          const page = n ? ctx.pages.get(n) : null;
          if (host.page && host.page.dataset) host.page.dataset.n = n ? String(n) : '';
          if (!n) { host.lines.innerHTML = ''; return; }
          if (!page) { host.lines.innerHTML = SKELETON; if (host.page && host.page.classList) host.page.classList.add('is-loading'); return; }
          if (host.page && host.page.classList) {
            host.page.classList.remove('is-loading');
            host.page.classList.toggle('is-blank', Boolean(page.blank));
          }
          if (host.page && host.page.style && typeof host.page.style.setProperty === 'function') {
            host.page.style.setProperty('--pw', G.num(page.width_pt));
            host.page.style.setProperty('--ph', G.num(page.height_pt));
          }
          const opts = typeof this.paintOpts === 'function' ? this.paintOpts(n, side) : {};
          host.lines.innerHTML = G.pageHtml(page, opts);
        });
        this.painted += 1;
        if (typeof this.afterPaint === 'function') this.afterPaint();
      },
      // The size of one point on screen (pixels), from the sheet's width.
      scale(side) {
        const host = (ctx.dom.sheets || {})[side || 'right'];
        const n = this.shownNumbers[side || 'right'];
        const page = n ? ctx.pages.get(n) : null;
        const width = host && host.page ? U.rect(host.page).width : 0;
        return page && width ? width / page.width_pt : 1;
      },

      // ------------------------------------------------------------ the book render and its polling
      schedule(ms) {
        clearTimeout(T.poll);
        T.poll = setTimeout(() => this.poll(), ms);
      },
      pollNow() {
        clearTimeout(T.poll);
        return this.poll();
      },
      onVisible() {
        if (U.hasDOM && document.hidden) return false;
        if (this.stopped || !(this.active || this.failures)) return false;
        this.pollNow();
        return true;
      },
      get active() {
        return isActive(this.book) || this.relayout.state === 'running';
      },
      async poll() {
        if (this.stopped || !urls.preview) return;
        if (U.hasDOM && document.hidden) { this.schedule(POLL_HIDDEN_MS); return; }
        const r = await U.api(`${urls.preview}${urls.preview.includes('?') ? '&' : '?'}scope=book`);
        if (r.status === 401 || r.status === 403) { this.stopped = true; this.pollState = 'auth'; return; }
        if (!r.ok || !r.data) {
          this.failures += 1;
          if (this.failures >= FAILURES_BEFORE_NOTICE) this.pollState = 'error';
        } else {
          this.failures = 0;
          this.pollState = 'ok';
          this.applyPreview(r.data);
        }
        if (this.stopped) return;
        if (this.active || this.failures || this.stale) this.schedule(Math.min(POLL_MS * (1 + this.failures) * (this.active ? 1 : 3), POLL_MAX_MS));
      },
      // The book render's state; the live layout's summary rides along: a newer revision (the render adopted)
      // brings the pages shown again, a first one starts the stage.
      applyPreview(payload, opts = {}) {
        if (!payload) return;
        this.book = Object.assign({}, payload, { pages: (payload.pages || []).slice() });
        (payload.pages || []).forEach((p) => { if (p.url && !ctx.thumbs.has(p.n)) ctx.thumbs.set(p.n, { url: p.url, url2x: p.url2x }); });
        const live = payload.layout;
        if (live) {
          this.stale = Boolean(live.stale);
          const newer = this.revision === null || (live.revision !== null && live.revision > this.revision);
          if (newer && this.relayout.state !== 'running' && !opts.quiet) {
            this.revision = this.revision === null ? live.revision : this.revision;
            this.pageCount = Number(live.page_count) || this.pageCount;
            if (Array.isArray(live.chapters)) this.ranges = live.chapters.map((c) => Object.assign({}, c));
            this.refetchShown();
          }
          this.maybeRelayoutStale();
        }
        this.syncFilm();
      },
      // A live layout older than the text with nothing running: the chapter under the reader's eyes is laid
      // out again once (the edits were made elsewhere, or before this page opened).
      maybeRelayoutStale() {
        if (!this.canEdit || !this.stale || requestedStale || this.active || !urls.relayout) return false;
        const cid = this.focusChapter;
        if (!cid) return false;
        requestedStale = true;
        if (typeof this.requestRelayout === 'function') this.requestRelayout(cid);
        return true;
      },
      async retry() {
        if (this.retrying || !urls.preview) return false;
        this.retrying = true;
        try {
          const r = await U.api(urls.preview, { method: 'POST', body: { scope: 'book' } });
          if (r.ok && r.data) this.applyPreview(r.data);
          if (!r.ok) U.toast('تعذّر طلب الإخراج. حاول مرة أخرى.');
          if (this.relayout.state === 'error' && typeof this.requestRelayout === 'function' && this.focusChapter) this.requestRelayout(this.focusChapter);
          this.schedule(POLL_MS);
          return r.ok;
        } finally {
          this.retrying = false;
        }
      },

      // ------------------------------------------------------------ a re-layout's result, spliced in place
      // `payload` = the re-layout API's answer (done). The pages held are renumbered by the delta, sides swapped
      // on an odd delta; the page under the reader's eyes stays (by its content); the footprint follows.
      applyRelayoutPayload(payload) {
        const result = (payload && payload.result) || {};
        if (result.unchanged) return { unchanged: true };
        const held = { pages: ctx.pages, pageCount: this.pageCount, revision: this.revision };
        const out = G.applyRelayout(held, result, payload.pages || [], this.geometry);
        if (out.refetch) {
          // `pending`: the pages shown, fetched again (the caller waits for them before it drops its patches)
          return { refetch: true, pending: this.refetchShown() };
        }
        const wasN = this.current;
        ctx.pages = out.pages;
        // the thumbs move with their pages; the new pages' images come later
        const moved = new Map();
        ctx.thumbs.forEach((t, n) => { const m = out.moved(n); if (m !== null) moved.set(m, t); });
        (payload.pages || []).forEach((p) => { if (p.url) moved.set(p.n, { url: p.url, url2x: p.url2x }); else moved.delete(p.n); });
        ctx.thumbs = moved;
        this.revision = out.revision;
        this.pageCount = out.pageCount;
        if (out.chapters) {
          const before = new Map(this.ranges.map((c) => [c.id, c.last - c.first + 1]));
          const deltas = {};
          out.chapters.forEach((c) => { if (before.has(c.id)) { const d = c.last - c.first + 1 - before.get(c.id); if (d) deltas[c.id] = d; } });
          this.chapterDeltas = Object.assign({}, this.chapterDeltas, deltas);
          this.ranges = out.chapters.map((c) => Object.assign({}, c));
        }
        if (out.checks) {
          // the checks of the pages replaced (the old `from`…`to`) go, the ones after them move with their pages;
          // the answer brings the new pages' checks and the book-wide ones (no page)
          const lo = out.from;
          const hi = out.to;
          this.checks = this.checks.filter((c) => (c.page === null || c.page === undefined ? false : c.page < lo || c.page > hi))
            .map((c) => (c.page > hi ? Object.assign({}, c, { page: c.page + out.delta }) : c)).concat(out.checks);
        }
        if (out.delta) this.delta = out.delta;
        this.rebuildSequence();
        // the reader stays on the same page: a page after the chapter moves with it, one inside stays at its number
        let target = wasN ? out.moved(wasN) : 0;
        if (target === null) target = Math.min(wasN, out.from + out.count - 1);
        if (target) {
          const idx = this.pages.findIndex((p) => p.n === target);
          if (idx >= 0) this.cursor = this.spreadOn ? this.canonical(idx) : idx;
        }
        this.markThumbs();
        this.laterThumbs();
        const info = Object.assign({}, out, { result });
        if (typeof this.afterRelayout === 'function') this.afterRelayout(info);
        this.paint();
        this.ensureAround(this.current);
        return info;
      },
      // the images of the pages a re-layout made (their filmstrip thumbs) once the image task is done
      laterThumbs() {
        thumbTries = 0;
        clearTimeout(T.thumbs);
        T.thumbs = setTimeout(() => this.refreshThumbs(), THUMBS_LATER_MS);
      },
      async refreshThumbs() {
        const missing = this.pages.map((p) => p.n).filter((n) => !ctx.thumbs.has(n) && ctx.pages.has(n));
        if (!missing.length || !urls.pageLayout) return false;
        thumbTries += 1;
        const r = await U.api(this.layoutUrl(Math.min(...missing), Math.min(Math.max(...missing), Math.min(...missing) + 119)));
        if (r.ok && r.data && r.data.revision === this.revision) {
          (r.data.pages || []).forEach((p) => { if (p.url) ctx.thumbs.set(p.n, { url: p.url, url2x: p.url2x }); });
          this.syncFilm();
        }
        if (thumbTries < THUMB_TRIES && this.pages.some((p) => !ctx.thumbs.has(p.n) && ctx.pages.has(p.n))) {
          clearTimeout(T.thumbs);
          T.thumbs = setTimeout(() => this.refreshThumbs(), THUMBS_LATER_MS * thumbTries);
        }
        return true;
      },

      // ------------------------------------------------------------ derived state
      get current() {
        const p = this.pages[this.cursor];
        return p ? p.n : 0;
      },
      rangeAt(n) {
        return this.ranges.find((c) => c.first <= n && n <= c.last) || null;
      },
      chapterTitle(id) {
        const c = this.chapters.find((x) => x.id === id);
        return c ? c.title : '';
      },
      // the chapter under the reader's eyes (the shown page's; the open paragraph's in edit mode)
      get focusChapter() {
        if (this.editChapterId && (this.open || this.selected)) return this.editChapterId;
        const page = ctx.pages.get(this.current);
        if (page && page.chapter) return page.chapter;
        const r = this.rangeAt(this.current);
        return r ? r.id : cfg.chapter || (this.chapters[0] ? this.chapters[0].id : null);
      },
      get counterChapter() {
        const r = this.rangeAt(this.current);
        if (!r) return null;
        return { id: r.id, title: this.chapterTitle(r.id) || r.title || '', first: r.first, last: r.last };
      },
      get phase() {
        void this.painted;
        if (this.pages.length) return 'pages';
        if (this.book && this.book.status === 'error' && this.revision === null) return 'error';
        return 'skeleton';
      },
      get errorText() {
        if (this.relayout.state === 'error') return this.relayout.error || 'تعذّرت إعادة ترتيب صفحات الفصل.';
        return this.book && this.book.status === 'error' ? this.book.error || RENDER_ERROR : '';
      },
      // the quiet pill: re-laying out, the book render, a failure with the retry, stale
      get renderPill() {
        if (this.relayout.state === 'running') return { state: 'saving', text: 'تُرتَّب الصفحات…', action: '' };
        if (isActive(this.book)) return { state: 'saving', text: 'يُخرَج الكتاب…', action: '' };
        if (this.errorText) return this.pages.length ? { state: 'error', text: 'تعذّر تحديث الصفحات · إعادة المحاولة', action: 'retry' } : { state: '', text: '', action: '' };
        if (this.stale && this.pages.length) return { state: 'warn', text: 'الصفحات أقدم من النص', action: this.canEdit ? 'retry' : '' };
        return { state: '', text: '', action: '' };
      },
      get pdfUrl() {
        return (this.book && this.book.pdf_url) || '';
      },
      summaryOf(id) {
        return this.summaries.find((s) => s.id === id) || null;
      },
      // the footprint: «412 صفحة» with the delta, «يُحسب…» while the book render runs, one row per chapter
      get footprint() {
        void this.painted;
        const count = this.pageCount;
        const focus = this.focusChapter;
        const driftIds = new Set((cfg.drift && cfg.drift.chapters) || []);
        const dismissed = new Set(this.dismissedDrift || []);
        // one pass over each list (800 chapters stay cheap on every paint)
        const rangeOf = new Map(this.ranges.map((x) => [x.id, x]));
        const sumOf = new Map(this.summaries.map((x) => [x.id, x]));
        const rows = this.chapters.map((c) => {
          const r = rangeOf.get(c.id);
          const s = sumOf.get(c.id);
          return {
            id: c.id,
            number: c.number,
            kind: c.kind,
            title: c.title,
            first: r ? r.first : null,
            last: r ? r.last : null,
            pages: r ? r.last - r.first + 1 : null,
            delta: this.chapterDeltas[c.id] || 0,
            drift: Boolean((s && s.drift) || driftIds.has(c.id)) && !dismissed.has(c.id),
            current: focus === c.id,
          };
        });
        return {
          count,
          text: count ? G.arCount(count, PAGE_FORMS) : '',
          computing: isActive(this.book) || this.relayout.state === 'running',
          delta: this.delta,
          deltaText: this.delta ? (this.delta > 0 ? `+${this.delta}` : `−${-this.delta}`) : '',
          rows,
        };
      },
      deltaText(d) {
        return d ? (d > 0 ? `+${d}` : `−${-d}`) : '';
      },

      // ------------------------------------------------------------ the viewer (= books.js D33)
      get spreadOn() {
        return this.spread && this.stageWide;
      },
      canonical(idx) {
        const p = this.pages[idx];
        if (!p) return idx;
        if (p.n % 2 === 1 && idx > 0 && this.pages[idx - 1].n === p.n - 1) return idx - 1;
        return idx;
      },
      spreadOf(idx) {
        const p = this.pages[idx];
        if (!p) return { right: -1, left: -1 };
        if (p.n % 2 === 0) return { right: idx, left: idx + 1 < this.pages.length && this.pages[idx + 1].n === p.n + 1 ? idx + 1 : -1 };
        return { right: idx > 0 && this.pages[idx - 1].n === p.n - 1 ? idx - 1 : -1, left: idx };
      },
      get shown() {
        if (this.cursor < 0) return { right: -1, left: -1 };
        if (!this.spreadOn) return { right: this.cursor, left: -1 };
        return this.spreadOf(this.cursor);
      },
      get rightPage() { return this.pages[this.shown.right] || null; },
      get leftPage() { return this.pages[this.shown.left] || null; },
      // «صفحة 37 من 412», «الصفحتان 36–37 من 412»
      get counterText() {
        const nums = [this.rightPage, this.leftPage].filter(Boolean).map((p) => p.n).sort((a, b) => a - b);
        if (!nums.length) return '';
        // the range isolated left to right (U+2066 … U+2069): in an Arabic line «36–37» would read «37–36»
        return nums.length === 2 ? `الصفحتان \u2066${nums[0]}–${nums[1]}\u2069 من ${this.pageCount}` : `صفحة ${nums[0]} من ${this.pageCount}`;
      },
      // the sheet's shape: the trim of the pages on screen (the layout's, else the stylesheet's)
      get sheetStyle() {
        const g = this.geometry || {};
        const s = this.sheet || {};
        const w = Number(g.width_pt) || (Number(s.width_mm) || 170) * 72 / 25.4;
        const h = Number(g.height_pt) || (Number(s.height_mm) || 240) * 72 / 25.4;
        return `--lo-ar: ${(w / h).toFixed(4)}; --lo-w: ${(w * 25.4 / 72).toFixed(2)}; --lo-gap: ${SPREAD_GAP_PX}px`;
      },
      get pageRatio() {
        const g = this.geometry || {};
        return Number(g.width_pt) && Number(g.height_pt) ? g.width_pt / g.height_pt : 170 / 240;
      },
      neighbour(dir, from) {
        const base = from !== undefined ? from : this.turning ? pendingTarget : this.cursor;
        const n = this.pages.length;
        if (!n) return null;
        if (base < 0) return dir > 0 ? 0 : (this.spreadOn ? this.canonical(n - 1) : n - 1);
        if (!this.spreadOn) { const next = base + dir; return next >= 0 && next < n ? next : null; }
        const s = this.spreadOf(base);
        const next = dir > 0 ? Math.max(s.right, s.left) + 1 : Math.min(...[s.right, s.left].filter((i) => i >= 0)) - 1;
        return next >= 0 && next < n ? this.canonical(next) : null;
      },
      canTurn(dir) {
        void this.cursor; void this.pages.length; void this.spreadOn;
        return this.neighbour(dir) !== null;
      },
      turn(dir) {
        const idx = this.neighbour(dir);
        if (idx === null) return false;
        return this.showIndex(idx, { dir });
      },
      showPage(n, opts = {}) {
        const idx = this.pages.findIndex((p) => p.n === Number(n));
        if (idx < 0) { if (!this.pages.length) pendingN = Number(n) || 0; return false; }
        return this.showIndex(idx, opts);
      },
      // Show the page at `idx`: the sheet slides out in the reading direction, the new one slides in; pressing
      // again mid-turn only moves the target. `instant` (and reduced motion) lands at once.
      showIndex(idx, opts = {}) {
        if (idx < 0 || idx >= this.pages.length) return false;
        const target = this.spreadOn ? this.canonical(idx) : idx;
        if (target === this.cursor && !this.turning) { if (opts.focus) this.focusStage(); return true; }
        if (typeof this.beforeTurn === 'function') this.beforeTurn(target, opts);
        const base = this.turning ? pendingTarget : this.cursor;
        const dir = opts.dir || (base >= 0 && target < base ? -1 : 1);
        pendingTarget = target;
        pendingFocus = pendingFocus || Boolean(opts.focus);
        if (this.cursor < 0 || opts.instant || U.reduced()) {
          clearTimeout(T.turn);
          this.turning = '';
          this.land(target);
          if (pendingFocus) { this.focusStage(); pendingFocus = false; }
          return true;
        }
        if (this.turning) return true;
        this.turning = dir > 0 ? 'out-next' : 'out-prev';
        clearTimeout(T.turn);
        T.turn = setTimeout(() => this.landTurn(dir), TURN_OUT_MS);
        return true;
      },
      landTurn(dir) {
        this.land(pendingTarget);
        this.turning = dir > 0 ? 'in-next' : 'in-prev';
        U.frame(() => U.frame(() => {
          this.turning = '';
          if (pendingFocus) { this.focusStage(); pendingFocus = false; }
          if (pendingTarget >= 0 && pendingTarget !== this.cursor) this.showIndex(pendingTarget);
        }));
      },
      land(idx) {
        this.cursor = idx;
        this.markThumbs();
        const n = this.current;
        if (n) {
          U.writeSession(PAGE_KEY + (this.bookId || ''), String(n));
          if (typeof history !== 'undefined' && history.replaceState && typeof window !== 'undefined' && window.location) {
            try { history.replaceState(null, '', `${window.location.pathname}${this.addressQuery ? this.addressQuery() : window.location.search}#page-${n}`); } catch (_) { /* sandboxed */ }
          }
          this.liveMessage = this.counterText;
          this.ensureAround(n);
        }
        this.paint();
        this.centerFilm();
        if (typeof this.afterLand === 'function') this.afterLand(n);
      },
      landFirst() {
        let idx = pendingN ? this.pages.findIndex((p) => p.n === pendingN) : -1;
        if (idx < 0 && cfg.requestedChapter) { const r = this.ranges.find((c) => c.id === cfg.requestedChapter); if (r) idx = this.pages.findIndex((p) => p.n === r.first); }
        if (idx < 0) idx = 0;
        pendingN = 0;
        this.land(this.spreadOn ? this.canonical(idx) : idx);
      },
      get turnClass() {
        return this.turning ? `is-${this.turning}` : '';
      },
      focusStage() {
        const stage = this.$refs && this.$refs.stage;
        if (stage && stage.focus) stage.focus({ preventScroll: true });
      },
      setSpread(on) {
        this.spread = Boolean(on);
        U.writeLocal(SPREAD_KEY, this.spread ? '1' : '0');
        if (this.cursor >= 0 && this.spreadOn) this.cursor = this.canonical(this.cursor);
        this.markThumbs();
        this.centerFilm();
        this.ensureAround(this.current);
        this.paint();
      },
      toggleSpread() { this.setSpread(!this.spread); },
      setFit(mode) {
        this.fit = FIT_MODES.includes(mode) ? mode : 'height';
        U.writeLocal(FIT_KEY, this.fit);
        if (typeof this.afterPaint === 'function') U.frame(() => this.afterPaint());
      },
      onResize() {
        const width = ctx.dom.canvas && ctx.dom.canvas.clientWidth ? ctx.dom.canvas.clientWidth : 0;
        const wide = width ? width >= SPREAD_MIN_PX : true;
        if (wide !== this.stageWide) {
          this.stageWide = wide;
          if (this.cursor >= 0 && this.spreadOn) this.cursor = this.canonical(this.cursor);
          this.markThumbs();
          this.paint();
        } else if (typeof this.afterPaint === 'function') this.afterPaint();
      },
      goToChapter(id) {
        const r = this.ranges.find((c) => c.id === id);
        if (!r) { U.toast('لم تُرتَّب صفحات هذا الفصل بعد'); return false; }
        return this.showPage(r.first, { manual: true });
      },
      jumpTarget(value) {
        const digits = U.westernDigits(value).replace(/\D/g, '');
        const n = digits ? parseInt(digits, 10) : NaN;
        if (!Number.isFinite(n) || !this.pages.some((p) => p.n === n)) return null;
        return n;
      },
      jump(value) {
        const n = this.jumpTarget(value);
        if (n === null) { U.toast('لا صفحة بهذا الرقم'); return null; }
        this.showPage(n, { focus: true, manual: true });
        return n;
      },
      focusJump() {
        const field = this.$refs && this.$refs.jump;
        if (field && field.focus) { field.focus(); if (field.select) field.select(); }
      },
      // The page in the address (#page-N), else the one shown last in this session, else the requested
      // chapter's first page, else the first page.
      restorePosition() {
        const hash = typeof window !== 'undefined' && window.location ? window.location.hash || '' : '';
        const m = /^#page-(\d+)$/.exec(hash);
        const saved = Number(U.readSession(PAGE_KEY + (this.bookId || '')));
        const n = m ? Number(m[1]) : cfg.requestedChapter ? 0 : saved;
        if (!this.pages.length) { pendingN = n > 0 ? n : 0; return; }
        if (n > 0 && this.showPage(n, { instant: true })) return;
        if (n > 0 && m) U.toast('لا صفحة بهذا الرقم');
        this.landFirst();
      },
      // a #page-N link opened in this tab (the address the page itself writes goes through replaceState,
      // which fires no hashchange)
      onHashChange() {
        const m = /^#page-(\d+)$/.exec(typeof window !== 'undefined' && window.location ? window.location.hash || '' : '');
        if (!m || Number(m[1]) === this.current) return;
        if (!this.showPage(Number(m[1]), { manual: true }) && this.pages.length) U.toast('لا صفحة بهذا الرقم');
      },
      // a trackpad or wheel gesture turns one page (RTL: a swipe to the right and scrolling down go forward);
      // the gesture must pause before the next one counts, so inertia never flips two pages
      onStageWheel(e) {
        if (!e || e.ctrlKey || e.metaKey) return;
        if (this.fit !== 'height') return;
        if (U.closest(e.target, '.lp-edit, .ed-pop')) return;
        const horizontal = Math.abs(e.deltaX) > Math.abs(e.deltaY);
        const delta = horizontal ? -e.deltaX : e.deltaY;
        if (e.cancelable && e.preventDefault) e.preventDefault();
        clearTimeout(T.wheel);
        T.wheel = setTimeout(() => { wheelAcc = 0; wheelLock = false; }, WHEEL_IDLE_MS);
        if (wheelLock) return;
        wheelAcc += delta;
        if (Math.abs(wheelAcc) >= (horizontal ? WHEEL_TURN_X : WHEEL_TURN_Y)) {
          const dir = wheelAcc > 0 ? 1 : -1;
          wheelAcc = 0;
          wheelLock = true;
          this.turn(dir);
        }
      },
      onStagePointerDown(e) {
        if (!e || e.pointerType === 'mouse') return;
        swipeStart = { x: e.clientX, y: e.clientY };
      },
      onStagePointerUp(e) {
        if (!swipeStart || !e || e.pointerType === 'mouse') return;
        const dx = e.clientX - swipeStart.x;
        const dy = e.clientY - swipeStart.y;
        swipeStart = null;
        if (Math.abs(dx) >= SWIPE_PX && Math.abs(dx) > 1.5 * Math.abs(dy)) this.turn(dx > 0 ? 1 : -1);
      },
      swipeCancel() { swipeStart = null; },

      // ------------------------------------------------------------ the filmstrip (static thumbs, = books.js)
      makeThumb() {
        const el = document.createElement('button');
        el.type = 'button';
        el.className = 'lo-thumb';
        const box = document.createElement('span');
        box.className = 'lo-thumb-img';
        const img = document.createElement('img');
        img.setAttribute('loading', 'lazy');
        img.setAttribute('decoding', 'async');
        img.setAttribute('alt', '');
        box.appendChild(img);
        const num = document.createElement('span');
        num.className = 'lo-thumb-num num';
        el.appendChild(box);
        el.appendChild(num);
        return { el, img, num };
      },
      syncFilm() {
        const film = ctx.dom.film;
        if (!film || !U.hasDOM) return;
        this.pages.forEach((p, i) => {
          let t = thumbs[i];
          if (!t) { t = this.makeThumb(); thumbs[i] = t; film.appendChild(t.el); }
          t.el.dataset.index = String(i);
          t.el.dataset.number = String(p.n);
          t.el.setAttribute('title', `صفحة ${p.n}`);
          t.el.setAttribute('aria-label', `صفحة ${p.n}`);
          if (t.el.style && t.el.style.setProperty) t.el.style.setProperty('--thumb-ar', this.pageRatio.toFixed(4));
          const thumb = ctx.thumbs.get(p.n);
          const url = thumb && thumb.url ? thumb.url : '';
          if (t.img && t.img.getAttribute('src') !== url) { if (url) t.img.setAttribute('src', url); else if (t.img.removeAttribute) t.img.removeAttribute('src'); }
          t.el.classList.toggle('is-pending', !url);
          if (t.num && t.num.textContent !== String(p.n)) t.num.textContent = String(p.n);
        });
        while (thumbs.length > this.pages.length) { const t = thumbs.pop(); if (t && t.el && t.el.remove) t.el.remove(); }
        this.markThumbs();
      },
      markThumbs() {
        const s = this.shown;
        thumbs.forEach((t, i) => {
          if (!t || !t.el || !t.el.classList) return;
          const on = i === s.right || i === s.left;
          t.el.classList.toggle('is-current', on);
          if (on) t.el.setAttribute('aria-current', 'page');
          else if (t.el.removeAttribute) t.el.removeAttribute('aria-current');
        });
      },
      centerFilm() {
        const film = ctx.dom.film;
        if (!film || typeof film.querySelector !== 'function' || this.tab !== 'pages') return;
        const run = () => {
          const item = film.querySelector(`[data-index="${this.cursor}"]`);
          const scroller = U.closest(film, '.bp-tab-body') || film;
          if (!item || !item.getBoundingClientRect || !scroller.getBoundingClientRect || !scroller.scrollBy) return;
          const f = scroller.getBoundingClientRect();
          const r = item.getBoundingClientRect();
          if (!f.height || !r.height) return;
          const out = r.top < f.top + FILM_EDGE_PX || r.bottom > f.bottom - FILM_EDGE_PX;
          if (!out) return;
          scroller.scrollBy({ top: r.top + r.height / 2 - (f.top + f.height / 2), behavior: U.reduced() ? 'auto' : 'smooth' });
        };
        if (this.$nextTick) this.$nextTick(run); else run();
      },
    };
  };
})();
