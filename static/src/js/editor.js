// Chapter editor (Phase 5, docs/PHASE5_SPEC.md §4): a modern Word, simpler, in the Nassakh vocabulary.
//   bookEditor(config) – the page: one chapter at a time in the TipTap editor (static/dist/editor.js,
//                        window.NassakhEditor), autosave 1.5 s after the last change with the chapter's version
//                        (a 409 shows the conflict), the style picker, B / I, footnotes in a small popover,
//                        uncertain words with their readings, find & replace (live in the chapter, the whole
//                        book through the server with an undo toast), the source pane and drawer («الأصل»),
//                        the chapters panel, snapshots, digit conversion, chapter re-assembly after review
//                        drift (D41), the status line and the keyboard map
//   editorBar          – the top-bar controls (base.html header_actions, outside the root) reading
//                        Alpine.store('editor').view
// Config (editor.services.page_config): { bookId, title, author, exists, chapter, chapters, canEdit, stylesheet,
// faces, autosaveMs, urls }. The editor itself is one object (`NassakhEditor.create`); under Node the tests hand
// this component a stub in its place and drive everything else.
// One overlay (`pop`, the .ed-pop element) serves the footnote editor and the word readings: at most one open,
// placed against the visible column like the review popover (below the anchor, flipped above near the bottom,
// never across an edge), closed by a click outside, Esc, scrolling it away or opening another.
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
  const ZOOM_KEY = 'nassakh.editor.zoom';
  const MARKS_KEY = 'nassakh.editor.marks';
  const ZOOMS = [90, 100, 120];
  const AUTOSAVE_MS = 1500; // §4: 1.5 s after the last change (config.autosaveMs)
  const FIND_MS = 150; // the count follows the typed query
  const WORDS_MS = 300;
  const SOURCE_MS = 250; // the source pane follows the caret
  const PAGES_REFRESH_MS = 25000; // after the book render that follows a save (D44: chapter 3 s, book 20 s)
  const POLL_MS = 1000; // a chapter re-assembly run
  const POLL_MAX_MS = 5000;
  const POP_EDGE = 8; // = review / manuscript POP_EDGE
  const POP_GAP = 6;
  const POP_SIZE = { note: [380, 132], word: [280, 168] }; // [w, h] before the first measure
  const TOPBAR_H = 52;
  const TOOLBAR_H = 44;
  const PX_PER_MM = 96 / 25.4;
  const PX_PER_PT = 96 / 72;
  // = publishing/fonts.py: Latin letters are left to the Latin face; a face without Latin glyphs leaves the digits too
  const ARABIC_RANGES = 'U+0000-0040, U+005B-0060, U+007B-00BF, U+00D7, U+00F7, U+0600-06FF, U+0750-077F, U+08A0-08FF, U+2000-206F, U+FB50-FDFF, U+FE70-FEFF';
  const ARABIC_RANGES_NO_DIGITS = ARABIC_RANGES.replace('U+0000-0040', 'U+0000-002F, U+003A-0040');
  const AMIRI = { regular: '/static/fonts/amiri/Amiri-Regular.ttf', bold: '/static/fonts/amiri/Amiri-Bold.ttf' };
  const PAGES = ['صفحة واحدة', 'صفحتان', 'صفحات', 'صفحة'];
  const WORDS = ['كلمة واحدة', 'كلمتان', 'كلمات', 'كلمة'];
  const MATCHES = ['مطابقة واحدة', 'مطابقتان', 'مطابقات', 'مطابقة'];
  const DIGITS = ['رقم واحد', 'رقمان', 'أرقام', 'رقمًا'];
  const STYLE_KEYS = { Digit1: 'heading1', Digit2: 'heading2', Digit0: 'paragraph', Digit3: 'quote', Digit4: 'verse', Digit5: 'center', Digit6: 'separator' };
  const READING_LABELS = { primary: 'الأساسي', secondary: 'الثانوي', tess: 'Tesseract' };

  // ---------------------------------------------------------------- pure helpers (exported for the tests)
  // «صفحة واحدة», «صفحتان», «5 صفحات», «214 صفحة» (= assembly.render.ar_count).
  function arCount(n, forms) {
    n = Number(n) || 0;
    if (n === 1) return forms[0];
    if (n === 2) return forms[1];
    const units = n % 100;
    return `${n} ${units >= 3 && units <= 10 ? forms[2] : forms[3]}`;
  }
  const MINUTES = ['دقيقة', 'دقيقتين', 'دقائق', 'دقيقة'];
  const HOURS = ['ساعة', 'ساعتين', 'ساعات', 'ساعة'];
  const DAYS = ['يوم', 'يومين', 'أيام', 'يومًا'];
  // «قبل لحظات», «قبل 5 دقائق», «قبل ساعتين», «قبل 3 أيام», then the date (= manuscript relativeTime).
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
  const formatCount = (n) => String(Math.max(0, Math.round(Number(n) || 0))).replace(/\B(?=(\d{3})+(?!\d))/g, '\u202f'); // = convert.formatCount
  const pageRange = (p) => (p && Number.isInteger(p.first) ? (Number.isInteger(p.last) && p.last !== p.first ? `${p.first}–${p.last}` : String(p.first)) : '');
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const fill = (template, n) => String(template || '').replace('__n__', String(n)).replace('__cid__', String(n)).replace('__sid__', String(n));
  const attr = (el, name) => (el && typeof el.getAttribute === 'function' ? el.getAttribute(name) : null) || '';
  const q = (root, sel) => (root && typeof root.querySelector === 'function' ? root.querySelector(sel) : null);
  const qa = (root, sel) => (root && typeof root.querySelectorAll === 'function' ? Array.from(root.querySelectorAll(sel)) : []);
  const closest = (el, sel) => (el && typeof el.closest === 'function' ? el.closest(sel) : null);
  const rect = (el) => (el && typeof el.getBoundingClientRect === 'function' ? el.getBoundingClientRect() : { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 });
  const focusEl = (el, opts) => { if (el && typeof el.focus === 'function') el.focus(opts || { preventScroll: true }); };
  const toast = (message) => { if (window.Nassakh && window.Nassakh.toast) window.Nassakh.toast(message); };
  const bundle = () => window.NassakhEditor || null;
  // Shown on screen (x-show'd items are display: none, not hidden): the tab ring of a dialog skips them.
  const visible = (el) => Boolean(el) && !el.hidden && (typeof el.getClientRects !== 'function' || el.getClientRects().length > 0);
  const stripTashkeel = (s) => String(s || '').replace(/[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06dc\u06df-\u06e4\u06e7\u06e8\u06ea-\u06ed\u0640]/g, '');

  // The sheet's measure and type from the stylesheet at a zoom (PHASE5_SPEC §4: 17×24 → about 640 px at 100 %,
  // Amiri 12 pt → 16 px), as CSS custom properties on the sheet.
  function sheetVars(sheet, zoom) {
    const s = sheet || {};
    const z = (Number(zoom) || 100) / 100;
    const mm = (v, d) => `${Math.round((Number(v) || d) * PX_PER_MM * z)}px`;
    const pt = (v, d) => `${((Number(v) || d) * PX_PER_PT * z).toFixed(2)}px`;
    const scale = s.heading_scale || {};
    return [
      `--ed-zoom:${z}`,
      `--ed-page-w:${mm(s.width_mm, 170)}`,
      `--ed-pad-top:${mm(s.top_mm, 20)}`,
      `--ed-pad-bottom:${mm(s.bottom_mm, 22)}`,
      `--ed-pad-start:${mm(s.inner_mm, 22)}`,
      `--ed-pad-end:${mm(s.outer_mm, 18)}`,
      `--ed-size:${pt(s.body_size_pt, 13)}`,
      `--ed-lh:${Number(s.line_height) || 1.7}`,
      `--ed-indent:${Number.isFinite(Number(s.indent_em)) ? Number(s.indent_em) : 1.5}em`,
      `--ed-h1:${Number(scale.h1) || 1.6}em`,
      `--ed-h2:${Number(scale.h2) || 1.25}em`,
      `--ed-fn-size:${pt(s.footnote_size_pt, 10)}`,
    ].join(';');
  }
  // @font-face rules for the sheet: the stylesheet's faces through local() (the vendored Amiri by URL), the Arabic
  // faces limited to the ranges the print CSS uses so Latin runs take the Latin face, as on the pages.
  function fontCss(faces) {
    const f = faces || {};
    const rules = [];
    const face = (family, role, ranges) => {
      const spec = f[role] || {};
      const name = String(spec.family || 'Amiri').replace(/["\\]/g, '');
      const extra = ranges ? ` unicode-range: ${ranges};` : '';
      if ((spec.key || 'amiri') === 'amiri') {
        rules.push(`@font-face { font-family: "${family}"; src: url("${AMIRI.regular}") format("truetype"); font-weight: 400; font-style: normal; font-display: swap;${extra} }`);
        rules.push(`@font-face { font-family: "${family}"; src: url("${AMIRI.bold}") format("truetype"); font-weight: 700; font-style: normal; font-display: swap;${extra} }`);
      } else {
        rules.push(`@font-face { font-family: "${family}"; src: local("${name}"); font-weight: 400; font-style: normal;${extra} }`);
        rules.push(`@font-face { font-family: "${family}"; src: local("${name} Bold"), local("${name}"); font-weight: 700; font-style: normal;${extra} }`);
      }
    };
    const ranges = (role) => ((f[role] || {}).key === 'lotus' ? ARABIC_RANGES_NO_DIGITS : ARABIC_RANGES);
    face('nk-ed-body', 'body', ranges('body'));
    face('nk-ed-heading', 'heading', ranges('heading'));
    face('nk-ed-latin', 'latin', null);
    return rules.join('\n');
  }

  // Placement of the overlay (pure, = manuscript placeAgainst): below the anchor, flipped above when it would
  // leave the visible column and there is more room above, its start edge on the anchor's start edge (RTL: the
  // right), never across a side edge. Offsets relative to the column (the positioned parent).
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

  // Keyboard map (§4), pure. ctx: inField (an input / textarea: the find fields, the snapshot name), inEditor
  // (the caret is in the sheet), inNote (in the footnote editor). The editor keeps its own keys (B / I, undo,
  // the styles, ⌘⇧F): when the event reached the window already handled, the caller drops it.
  function keyAction(ev, ctx) {
    const k = ev.key || '';
    const code = ev.code || '';
    const mod = Boolean(ev.metaKey || ev.ctrlKey);
    if (k === 'Escape') return 'escape';
    if (mod && ev.altKey) return STYLE_KEYS[code] || null;
    if (mod) {
      const low = k.toLowerCase();
      if (low === 's' || code === 'KeyS') return 'save';
      if ((low === 'f' || code === 'KeyF') && ev.shiftKey) return 'footnote';
      if (low === 'f' || code === 'KeyF') return 'find';
      if (k === '[' || code === 'BracketLeft') return 'prevChapter';
      if (k === ']' || code === 'BracketRight') return 'nextChapter';
      if ((low === 'z' || code === 'KeyZ') && ev.shiftKey) return 'redo';
      if (low === 'z' || code === 'KeyZ') return 'undo';
      if (low === 'b' || code === 'KeyB') return 'bold';
      if (low === 'i' || code === 'KeyI') return 'italic';
      if ((low === 'o' || code === 'KeyO') && ev.shiftKey) return 'source';
      return null;
    }
    if (ctx.inField || ctx.inEditor || ctx.inNote || ev.altKey) return null;
    if (k === '?' || k === '؟') return 'sheet'; // the Arabic layout's question mark too
    if (code === 'KeyO' || k === 'o' || k === 'O') return 'source';
    return null;
  }

  // The status line (§4): «الفصل 3 من 14 · 2 184 كلمة · ص 31–58 في الكتاب».
  function statusText(chapter, words, opts = {}) {
    if (!chapter) return '';
    const parts = [`الفصل ${chapter.number} من ${chapter.count}`, arCount(words, WORDS).replace(/^(\d+)/, (m) => formatCount(m))];
    const pages = pageRange(chapter.pages);
    if (pages) parts.push(`ص ${pages} في الكتاب${opts.pagesStale ? ' · تُحدَّث' : ''}`);
    if (opts.readOnly) parts.push('للقراءة فقط');
    return parts.join(' · ');
  }

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
    let message = data && typeof data === 'object' && (data.message || data.detail);
    if (!message) {
      if (response.status === 401) message = 'انتهت الجلسة. سجّل الدخول من جديد.';
      else if (response.status === 403) message = 'لا تملك صلاحية هذا الإجراء.';
      else message = 'تعذّر تنفيذ الطلب. حاول مرة أخرى.';
    }
    return { ok: response.ok, status: response.status, data, message };
  }

  window.NassakhEditorUI = Object.assign(window.NassakhEditorUI || {}, {
    keyAction, placeAgainst, sheetVars, fontCss, statusText, arCount, relativeTime, formatCount, pageRange, ZOOMS,
  });

  document.addEventListener('alpine:init', () => {
    if (typeof Alpine.store === 'function') Alpine.store('editor', { view: null });

    Alpine.data('editorBar', () => ({
      get v() { return this.$store.editor.view; },
    }));

    Alpine.data('bookEditor', (cfg = {}) => {
      // Non-reactive plumbing in the closure: the editor object, timers, caches.
      const urls = cfg.urls || {};
      let ed = null; // the NassakhEditor object
      let noteEd = null; // the footnote's small editor while its popover is open
      let saveTimer = null;
      let findTimer = null;
      let wordsTimer = null;
      let sourceTimer = null;
      let pagesTimer = null;
      let pollTimer = null;
      let bookFindTimer = null;
      let saving = false;
      let savePromise = null; // the PUT in flight: a chapter switch and the server-side actions wait for it
      let loadGen = 0;
      let bookFindGen = 0; // the book count in flight: an older answer never lands after a newer query
      // The overlay opened by this click (the toolbar's «حاشية», the style menu): the outside-click of the same
      // event is no close. Cleared by a macrotask: Alpine paints the overlay in a microtask, before the
      // document's click listener runs, so a microtask would clear it too early.
      let justOpened = false;
      let returnTo = null; // where the focus goes back to when the drawer or the overlay closes
      let lastTrigger = null; // the control that opened a dialog: the focus returns to it
      let lastCaretBlock = null;
      let listHost = null;
      const sheetCache = new Map(); // page number → api:book_sheets item
      const readingsCache = new Map(); // page id → api:page_review lines
      const dismissedDrift = new Set(); // chapters whose drift banner was dismissed («الاحتفاظ بالنص») this session
      let pollFailures = 0;
      const opened = () => { justOpened = true; setTimeout(() => { justOpened = false; }, 0); };

      return {
        bookId: cfg.bookId || 0,
        title: cfg.title || '',
        canEdit: Boolean(cfg.canEdit),
        exists: Boolean(cfg.exists),
        stylesheet: cfg.stylesheet || {},
        faces: cfg.faces || {},
        autosaveMs: Number(cfg.autosaveMs) || AUTOSAVE_MS,
        phase: 'loading', // loading | ready | error | empty
        errorHeadline: '',
        chapters: [],
        chapter: null, // {id, number, kind, title, prev, next, count, sourcePages, pages, drift, warnings}
        version: '',
        reloadPending: false, // after a split / merge: no save until the chapter is loaded again
        save: { state: 'idle', message: '' }, // idle | dirty | saving | saved | error | conflict
        dirty: false,
        conflict: { open: false, version: '', content: null },
        driftPages: [],
        style: 'paragraph',
        bold: false,
        italic: false,
        canUndo: false,
        canRedo: false,
        words: 0,
        pagesStale: false,
        zoom: ZOOMS.includes(Number(storage.get(ZOOM_KEY, '100'))) ? Number(storage.get(ZOOM_KEY, '100')) : 100,
        pageMarks: storage.get(MARKS_KEY, '1') !== '0',
        find: { open: false, query: '', replacement: '', matchTashkeel: false, foldAlef: true, wholeWord: false, scope: 'chapter', total: 0, index: -1, bookTotal: null, busy: false },
        findH: 0, // the find panel's measured height: the side panel's sticky top moves down by it
        styleMenu: false,
        sheetOpen: false,
        pop: { kind: null, anchorId: '', style: '', above: false }, // 'note' | 'word'
        note: { id: '', seq: 0, sourcePage: null, orphan: false },
        word: { from: 0, to: 0, text: '', typed: '', readings: [], loading: false },
        source: { blockId: null, pages: [], index: 0, lines: [], sheet: null, loading: false, error: '' },
        drawerOpen: false,
        snapshots: { open: false, list: [], busy: false, label: '', loading: false, error: '' },
        digits: { open: false, style: 'western', scope: 'chapter', busy: false },
        reassembly: { running: false, runId: null, error: '' },
        sideOpen: false,
        liveMessage: '',
        styles: (bundle() && bundle().STYLES) || [],

        init() {
          if (typeof Alpine.store === 'function' && Alpine.store('editor')) Alpine.store('editor').view = this;
          if (!this.exists) { this.phase = 'empty'; return; }
          if (!bundle()) {
            this.phase = 'error';
            this.errorHeadline = 'لم يُحمَّل المحرّر: ملف static/dist/editor.js غير موجود أو تعطّل.';
            return;
          }
          this.styles = bundle().STYLES || [];
          this.load(cfg.chapter, { first: true });
        },
        destroy() {
          [saveTimer, findTimer, wordsTimer, sourceTimer, pagesTimer, pollTimer, bookFindTimer].forEach((t) => clearTimeout(t));
          this.closeNote();
          if (ed) ed.destroy();
          ed = null;
          if (typeof Alpine.store === 'function' && Alpine.store('editor')) Alpine.store('editor').view = null;
        },

        // ------------------------------------------------------------ derived state
        get readOnly() { return !this.canEdit; },
        get hasPrev() { return Boolean(this.chapter && this.chapter.prev); },
        get hasNext() { return Boolean(this.chapter && this.chapter.next); },
        get chapterTitle() { return this.chapter ? this.chapter.title : ''; },
        get styleLabel() {
          const found = this.styles.find((s) => s.key === this.style);
          return found ? found.label : 'فقرة';
        },
        get pill() {
          if (!this.canEdit || this.phase !== 'ready') return { state: 'idle', text: '' };
          const s = this.save.state;
          if (s === 'dirty') return { state: 'dirty', text: 'غير محفوظ' };
          if (s === 'saving') return { state: 'saving', text: 'يُحفظ…' };
          if (s === 'saved') return { state: 'saved', text: 'محفوظ' };
          if (s === 'error') return { state: 'error', text: 'تعذّر الحفظ · إعادة المحاولة' };
          if (s === 'conflict') return { state: 'conflict', text: 'تغيّر في نافذة أخرى' };
          return { state: 'idle', text: '' };
        },
        get statusText() { return statusText(this.chapter, this.words, { pagesStale: this.pagesStale, readOnly: this.readOnly }); },
        get driftText() {
          const n = this.driftPages.length;
          return n ? `تغيّر نص ${arCount(n, PAGES)} في المراجعة بعد التحرير:` : 'تغيّر نص هذا الفصل في المراجعة بعد التحرير.';
        },
        get findCountText() {
          const f = this.find;
          if (!f.query.trim()) return '';
          if (f.scope === 'book') return f.bookTotal === null ? 'يُعدّ…' : f.bookTotal ? `${f.bookTotal} في الكتاب` : 'لا مطابقات في الكتاب';
          if (!f.total) return 'لا مطابقات';
          return `${f.index + 1} من ${f.total}`;
        },
        get sheetStyle() { return sheetVars(this.stylesheet, this.zoom); },
        get screenStyle() { return this.find.open && this.findH ? `--ed-find-h:${this.findH}px` : ''; },
        get fontCss() { return fontCss(this.faces); },
        get warnings() { return this.chapter && Array.isArray(this.chapter.warnings) ? this.chapter.warnings : []; },
        get sourcePage() { return this.source.pages[this.source.index] || 0; },
        get sourceAspect() {
          const s = this.source.sheet;
          return s && s.width > 0 && s.height > 0 ? (s.width / s.height).toFixed(4) : '0.7';
        },
        reviewUrl(n) { return n ? fill(urls.review, n) : ''; },
        get layoutUrl() { return urls.layout ? `${urls.layout}${this.chapter ? `?chapter=${encodeURIComponent(this.chapter.id)}` : ''}` : ''; },

        // ------------------------------------------------------------ loading chapters
        async load(cid, opts = {}) {
          loadGen += 1;
          const gen = loadGen;
          this.phase = opts.first ? 'loading' : this.phase;
          const [list, one] = await Promise.all([api(urls.chapters), api(fill(urls.chapter, cid))]);
          if (gen !== loadGen) return false;
          if (!list.ok || !Array.isArray(list.data)) { this.fail(list.message); return false; }
          this.chapters = list.data;
          if (!one.ok || !one.data) {
            // the requested chapter is gone (the split changed): the first one
            if (one.status === 404 && this.chapters.length && cid !== this.chapters[0].id) return this.load(this.chapters[0].id, opts);
            this.fail(one.message);
            return false;
          }
          this.applyChapter(one.data);
          this.phase = 'ready';
          this.renderList();
          if (this.$nextTick) this.$nextTick(() => this.mount(one.data.content));
          else this.mount(one.data.content);
          this.loadDrift();
          return true;
        },
        fail(message) {
          this.phase = 'error';
          this.errorHeadline = message || 'تعذّر تحميل الفصل.';
        },
        applyChapter(data) {
          this.chapter = {
            id: data.id, number: data.number, kind: data.kind, title: data.title, prev: data.prev, next: data.next, count: data.count,
            sourcePages: data.source_pages, pages: data.pages, drift: Boolean(data.drift), warnings: Array.isArray(data.warnings) ? data.warnings : [],
          };
          this.version = data.version || '';
          this.reloadPending = false;
          this.dirty = false;
          this.pagesStale = false;
          this.save = { state: 'idle', message: '' };
          this.conflict = { open: false, version: '', content: null };
          this.words = bundle() && bundle().wordCount ? bundle().wordCount(data.content) : 0;
          this.updateUrl(data.id);
          if (dismissedDrift.has(this.chapter.id)) this.chapter.drift = false;
          this.driftPages = this.chapter.drift ? this.driftPages.filter((n) => this.inChapter(n)) : [];
        },
        inChapter(n) {
          const s = this.chapter && this.chapter.sourcePages;
          return Boolean(s && Number.isInteger(s.first) && n >= s.first && n <= (Number.isInteger(s.last) ? s.last : s.first));
        },
        updateUrl(cid) {
          try {
            if (window.history && typeof window.history.replaceState === 'function') {
              const url = new URL(window.location.href);
              url.searchParams.set('chapter', cid);
              window.history.replaceState(null, '', url.toString());
            }
          } catch (_) { /* no history in tests */ }
        },
        // The editor mounts once; a new chapter replaces its document (a fresh history).
        mount(content) {
          const host = this.$refs && this.$refs.sheet;
          if (!host || !bundle()) return;
          if (ed) { ed.setContent(content); }
          else {
            ed = bundle().create(host, {
              content,
              editable: this.canEdit,
              onFootnote: (note) => this.openNote(note),
              onUncertain: (word) => this.openWord(word),
            });
            ed.on('update', () => this.onEditorUpdate());
            ed.on('selection', () => this.onEditorSelection());
            ed.on('focus', () => this.onEditorSelection());
          }
          if (ed.dom && ed.dom.classList) ed.dom.classList.toggle('hide-marks', !this.pageMarks);
          this.onEditorSelection();
          this.words = ed.wordCount();
          if (this.find.open && this.find.query) this.runFind();
        },
        editor() { return ed; },
        async loadDrift() {
          if (!urls.manuscriptState || !this.chapter || !this.chapter.drift) return;
          const r = await api(urls.manuscriptState);
          if (!r.ok || !r.data) return;
          const pages = Array.isArray(r.data.stale_pages) ? r.data.stale_pages : [];
          this.driftPages = pages.filter((n) => this.inChapter(n));
        },
        async openChapter(cid, opts = {}) {
          if (!cid || (this.chapter && cid === this.chapter.id)) return false;
          if (this.dirty || saving) {
            await this.saveNow();
            if (this.dirty || this.reloadPending) { toast('تعذّر الحفظ؛ بقيت في هذا الفصل'); return false; }
          }
          this.closePop();
          this.closeDrawer();
          loadGen += 1;
          const gen = loadGen;
          const r = await api(fill(urls.chapter, cid));
          if (gen !== loadGen) return false;
          if (!r.ok || !r.data) { toast(r.message); return false; }
          this.applyChapter(r.data);
          this.renderList();
          this.mount(r.data.content);
          this.liveMessage = `الفصل ${r.data.number}: ${r.data.title}`;
          if (opts.block && ed) ed.goToBlock(opts.block);
          if (!opts.block && ed && this.$nextTick) this.$nextTick(() => { ed.focus('start'); });
          if (typeof window.scrollTo === 'function') window.scrollTo({ top: 0, behavior: 'auto' });
          this.loadDrift();
          return true;
        },
        prevChapter() {
          if (!this.hasPrev) { toast('هذا أول فصل'); return Promise.resolve(false); }
          return this.openChapter(this.chapter.prev);
        },
        nextChapter() {
          if (!this.hasNext) { toast('هذا آخر فصل'); return Promise.resolve(false); }
          return this.openChapter(this.chapter.next);
        },
        async refreshChapters() {
          const r = await api(urls.chapters);
          if (!r.ok || !Array.isArray(r.data)) return false;
          this.chapters = r.data;
          const mine = this.chapters.find((c) => this.chapter && c.id === this.chapter.id);
          if (mine && this.chapter) { this.chapter.pages = mine.pages; this.chapter.drift = mine.drift && !dismissedDrift.has(this.chapter.id); }
          this.pagesStale = false;
          this.renderList();
          return true;
        },
        schedulePagesRefresh() {
          this.pagesStale = Boolean(this.chapter && this.chapter.pages);
          clearTimeout(pagesTimer);
          pagesTimer = setTimeout(() => this.refreshChapters(), PAGES_REFRESH_MS);
        },
        // The chapters panel: static rows (800 chapters cheaply), clicks delegated.
        renderList() {
          if (!listHost) listHost = q(this.$el || (hasDOM ? document.querySelector('[data-editor]') : null), '[data-ed-list]');
          if (!listHost || !bundle() || !bundle().chapterListHtml) return;
          listHost.innerHTML = bundle().chapterListHtml(this.chapters, this.chapter ? this.chapter.id : null);
          const current = q(listHost, '.ed-ch.is-current');
          if (current && typeof current.scrollIntoView === 'function') current.scrollIntoView({ block: 'nearest' });
        },
        onListClick(e) {
          const row = closest(e.target, '[data-cid]');
          if (!row) return;
          e.preventDefault();
          this.openChapter(attr(row, 'data-cid'));
        },

        // ------------------------------------------------------------ the editor's events
        onEditorUpdate() {
          if (!this.canEdit) return;
          this.dirty = true;
          if (this.save.state !== 'saving') this.save = { state: 'dirty', message: '' };
          this.scheduleSave();
          // an undo toast («استُبدلت…», «حُذفت الحاشية») undoes that change only: the next edit retires it
          const store = window.Alpine && typeof Alpine.store === 'function' ? Alpine.store('editorToast') : null;
          if (store && store.visible && store.local) store.hide();
          clearTimeout(wordsTimer);
          wordsTimer = setTimeout(() => { if (ed) this.words = ed.wordCount(); }, WORDS_MS);
          if (this.find.open && this.find.query.trim()) this.scheduleFind(true);
          if (this.pop.kind === 'note' && noteEd && ed) {
            const note = ed.noteAt(this.note.id);
            if (!note) this.closePop(); else noteEd.setContent(note.content);
          }
          this.onEditorSelection();
        },
        onEditorSelection() {
          if (!ed) return;
          this.style = ed.currentStyle();
          this.bold = ed.isBold();
          this.italic = ed.isItalic();
          this.canUndo = ed.canUndo();
          this.canRedo = ed.canRedo();
          const block = ed.caretBlock();
          const id = block ? block.id : null;
          if (id !== lastCaretBlock) {
            lastCaretBlock = id;
            clearTimeout(sourceTimer);
            sourceTimer = setTimeout(() => this.followCaret(block), SOURCE_MS);
          }
        },

        // ------------------------------------------------------------ autosave (§4): 1.5 s after the last change
        scheduleSave() {
          clearTimeout(saveTimer);
          saveTimer = setTimeout(() => this.saveChapter(), this.autosaveMs);
        },
        saveNow() {
          clearTimeout(saveTimer);
          return this.saveChapter();
        },
        // One PUT at a time: a caller arriving while one is in flight waits for it, then saves again if the
        // text changed meanwhile (a chapter switch or a server-side action never runs beside a pending save).
        async saveChapter() {
          if (!this.canEdit || !ed || !this.chapter) return false;
          while (saving && savePromise) await savePromise;
          if (!this.dirty) return true;
          if (this.reloadPending || !this.version) { this.save = { state: 'error', message: 'أعد تحميل الفصل قبل الحفظ.' }; return false; }
          saving = true;
          savePromise = this.putChapter();
          let result;
          try { result = await savePromise; } finally { saving = false; savePromise = null; }
          // a split or merge reloads the list and the chapter once the save is over (the reload switches
          // chapters, which waits for a pending save itself)
          if (result.reload) await this.reloadAfterSplit(result.reload);
          return result.ok;
        },
        // The PUT itself: `{ ok, reload? }`, applied only while the chapter it was made for is still open.
        async putChapter() {
          const cid = this.chapter.id;
          this.dirty = false;
          this.save = { state: 'saving', message: '' };
          const content = { type: 'doc', content: ed.getContent() };
          const r = await api(fill(urls.chapter, cid), { method: 'PUT', body: { content, version: this.version } });
          if (!this.chapter || this.chapter.id !== cid) return { ok: false }; // the chapter changed under it: nothing to apply
          if (r.status === 409 && r.data) {
            this.dirty = true;
            this.save = { state: 'conflict', message: r.message };
            this.conflict = { open: true, version: r.data.version || '', content: r.data.content || null };
            this.liveMessage = r.message;
            return { ok: false };
          }
          if (!r.ok || !r.data) {
            this.dirty = true;
            this.save = { state: 'error', message: r.message };
            this.liveMessage = r.message;
            return { ok: false };
          }
          const data = r.data;
          this.save = { state: this.dirty ? 'dirty' : 'saved', message: '' }; // typed meanwhile: not saved yet
          if (data.reload) {
            this.reloadPending = true;
            this.version = '';
            return { ok: true, reload: data };
          }
          this.version = data.version || this.version;
          if (data.changed) {
            const formed = Array.isArray(data.chapters) && data.chapters[0];
            if (formed && this.chapter) {
              this.chapter.title = formed.title;
              const row = this.chapters.find((c) => c.id === this.chapter.id);
              if (row) { row.title = formed.title; row.version = formed.version; row.words = this.words; }
              this.renderList();
            }
            this.schedulePagesRefresh();
          }
          if (this.dirty) this.scheduleSave();
          return { ok: true };
        },
        // A new level-1 heading split the chapter (or a deleted one merged it): the list and the chapter are
        // loaded again before any save; the caret's block is looked for in the chapters that formed.
        async reloadAfterSplit(data) {
          const block = ed && ed.caretBlock();
          const formed = Array.isArray(data.chapters) ? data.chapters.map((c) => c.id) : [];
          const target = data.id || formed[0] || (this.chapter && this.chapter.id);
          this.chapter = null;
          const ok = await this.load(target);
          if (!ok) return false;
          const ids = block && block.id ? [block.id] : [];
          if (ids.length && ed && !ed.goToBlock(ids[0])) {
            const other = formed.find((id) => id !== target);
            if (other) await this.openChapter(other, { block: ids[0] });
          }
          toast(formed.length > 1 ? 'انقسم الفصل؛ حُدّثت قائمة الفصول' : 'اندمج الفصل؛ حُدّثت قائمة الفصول');
          return true;
        },
        // The conflict (409): reload the other window's text, or keep this one (the server's copy is kept first).
        async reloadConflict() {
          if (!this.conflict.open) return false;
          const content = this.conflict.content;
          const version = this.conflict.version;
          this.conflict = { open: false, version: '', content: null };
          if (!content || !ed) return this.reloadCurrent(); // no text in the answer: the server's copy again
          ed.setContent(content);
          this.version = version;
          this.dirty = false;
          this.save = { state: 'saved', message: '' };
          this.words = ed ? ed.wordCount() : this.words;
          this.onEditorSelection();
          toast('أُعيد تحميل الفصل من الخادم');
          return true;
        },
        async keepMine() {
          if (!this.conflict.open) return false;
          const version = this.conflict.version;
          const s = await api(urls.snapshots, { method: 'POST', body: { label: `نص نافذة أخرى قبل استبداله · الفصل «${this.chapterTitle}»` } });
          if (!s.ok) { toast(s.message); return false; }
          this.conflict = { open: false, version: '', content: null };
          this.version = version;
          this.dirty = true;
          return this.saveNow();
        },
        // Leaving with unsaved changes warns (the browser's own sentence).
        guardUnload(e) {
          if (!this.dirty && !saving) return false;
          if (e && typeof e.preventDefault === 'function') e.preventDefault();
          if (e) e.returnValue = '';
          return true;
        },

        // ------------------------------------------------------------ toolbar: styles, marks, history, zoom, marks
        setStyle(key) {
          this.styleMenu = false;
          if (!ed || !this.canEdit) return false;
          if (key === 'footnote') return this.insertFootnote();
          const ok = ed.setStyle(key);
          this.onEditorSelection();
          return ok;
        },
        toggleBold() { if (ed && this.canEdit) { ed.toggleBold(); this.onEditorSelection(); } },
        toggleItalic() { if (ed && this.canEdit) { ed.toggleItalic(); this.onEditorSelection(); } },
        undo() { if (ed && this.canEdit) { ed.undo(); this.onEditorSelection(); } },
        redo() { if (ed && this.canEdit) { ed.redo(); this.onEditorSelection(); } },
        setZoom(z) {
          if (!ZOOMS.includes(Number(z))) return;
          this.zoom = Number(z);
          storage.set(ZOOM_KEY, String(this.zoom));
          if (this.pop.kind) this.placePop();
        },
        togglePageMarks() {
          this.pageMarks = !this.pageMarks;
          storage.set(MARKS_KEY, this.pageMarks ? '1' : '0');
          if (ed && ed.dom && ed.dom.classList) ed.dom.classList.toggle('hide-marks', !this.pageMarks);
        },
        focusEditor() { if (ed) ed.focus(); },

        // ------------------------------------------------------------ find & replace (§4)
        openFind() {
          this.closePop();
          this.find.open = true;
          if (this.$nextTick) this.$nextTick(() => { this.measureFind(); const f = this.$refs && this.$refs.findQuery; if (f) { focusEl(f); if (f.select) f.select(); } });
          if (this.find.query.trim()) this.runFind();
        },
        measureFind() {
          const panel = this.$refs && this.$refs.findPanel;
          this.findH = this.find.open && panel && panel.offsetHeight ? panel.offsetHeight : 0;
        },
        closeFind(refocus) {
          this.find.open = false;
          this.findH = 0;
          this.find.total = 0;
          this.find.index = -1;
          this.find.bookTotal = null;
          if (ed) ed.clearFind();
          if (refocus && ed) ed.focus();
        },
        toggleFind() { if (this.find.open) this.closeFind(true); else this.openFind(); },
        onFindInput() { this.scheduleFind(false); },
        setFindScope(scope) { this.find.scope = scope === 'book' ? 'book' : 'chapter'; this.runFind(); },
        findOptions() { return { matchTashkeel: this.find.matchTashkeel, foldAlef: this.find.foldAlef, wholeWord: this.find.wholeWord }; },
        scheduleFind(keepIndex) {
          clearTimeout(findTimer);
          findTimer = setTimeout(() => this.runFind(keepIndex), FIND_MS);
        },
        runFind(keepIndex) {
          clearTimeout(findTimer);
          if (!ed) return;
          const query = this.find.query;
          if (!query.trim()) { this.find.total = 0; this.find.index = -1; this.find.bookTotal = null; ed.clearFind(); return; }
          const r = ed.find(query, this.findOptions(), Boolean(keepIndex));
          this.find.total = r.total;
          this.find.index = r.index;
          if (this.find.scope === 'book') this.scheduleBookCount();
        },
        scheduleBookCount() {
          clearTimeout(bookFindTimer);
          bookFindGen += 1; // an answer to the older query or options is dropped
          this.find.bookTotal = null;
          bookFindTimer = setTimeout(() => this.countInBook(), FIND_MS * 2);
        },
        async countInBook() {
          const query = this.find.query;
          if (!query.trim() || !this.canEdit) return;
          const gen = bookFindGen;
          const r = await api(urls.findReplace, { method: 'POST', body: { query, replacement: '', replace: false, match_tashkeel: this.find.matchTashkeel, fold_alef: this.find.foldAlef, whole_word: this.find.wholeWord } });
          if (gen !== bookFindGen || this.find.query !== query || this.find.scope !== 'book') return;
          this.find.bookTotal = r.ok && r.data ? Number(r.data.total) || 0 : 0;
          if (!r.ok) toast(r.message);
        },
        // Enter: the next match (⇧Enter the previous); a match in a footnote opens the note's editor.
        findStep(dir) {
          if (!ed) return null;
          if (!this.find.total) { this.runFind(); if (!this.find.total) return null; }
          const m = ed.findNext(dir);
          if (!m) return null;
          this.find.index = m.index;
          if (m.note) {
            const note = ed.noteAt(m.note);
            if (note) this.openNote(note, { keepFocus: true });
          } else this.closePop();
          return m;
        },
        replaceOne() {
          if (!ed || !this.canEdit || !this.find.total) return false;
          const current = ed.findCurrent();
          if (!current) return false;
          const ok = ed.replaceCurrent(this.find.replacement);
          if (ok) { this.onEditorUpdate(); this.runFind(true); if (this.find.total) this.findStep(0); }
          return ok;
        },
        async replaceAll() {
          if (!ed || !this.canEdit || !this.find.query.trim()) return 0;
          if (this.find.scope === 'book') return this.replaceAllInBook();
          const n = ed.replaceAll(this.find.replacement);
          if (n) {
            this.onEditorUpdate();
            this.find.total = 0;
            this.find.index = -1;
            this.liveMessage = `استُبدلت ${arCount(n, MATCHES)}`;
            this.undoToast(`استُبدلت ${arCount(n, MATCHES)}`, () => this.undo(), true);
          } else toast('لا مطابقات');
          return n;
        },
        // The whole book goes through the server: the chapter is saved first, the edit snapshot is the undo.
        async replaceAllInBook() {
          if (this.dirty || saving) { await this.saveNow(); if (this.dirty) { toast('تعذّر الحفظ قبل الاستبدال'); return 0; } }
          this.find.busy = true;
          const r = await api(urls.findReplace, { method: 'POST', body: { query: this.find.query, replacement: this.find.replacement, replace: true, match_tashkeel: this.find.matchTashkeel, fold_alef: this.find.foldAlef, whole_word: this.find.wholeWord } });
          this.find.busy = false;
          if (!r.ok || !r.data) { toast(r.message); return 0; }
          const n = Number(r.data.replaced) || 0;
          if (!n) { toast('لا مطابقات في الكتاب'); return 0; }
          await this.reloadCurrent();
          this.find.bookTotal = 0;
          this.liveMessage = `استُبدلت ${arCount(n, MATCHES)} في الكتاب`;
          const snapshot = r.data.snapshot;
          this.undoToast(`استُبدلت ${arCount(n, MATCHES)} في الكتاب`, () => this.restoreSnapshot(snapshot, { quiet: true }));
          return n;
        },
        // The chapter again from the server (after a server-side change): the same chapter, or the first.
        async reloadCurrent() {
          const cid = this.chapter ? this.chapter.id : cfg.chapter;
          const r = await api(fill(urls.chapter, cid));
          if (!r.ok || !r.data) { if (r.status === 404) return this.load(this.chapters[0] ? this.chapters[0].id : cid); toast(r.message); return false; }
          this.applyChapter(r.data);
          this.mount(r.data.content);
          this.refreshChapters();
          return true;
        },
        // `local`: the undo is the editor's own history (replace all in the chapter, a deleted note) and dies
        // with the next edit; a server-side undo (a snapshot restore) outlives typing.
        undoToast(message, undo, local) {
          const store = window.Alpine && typeof Alpine.store === 'function' ? Alpine.store('editorToast') : null;
          if (store) store.show(message, undo, 8000, Boolean(local)); else toast(message);
        },

        // ------------------------------------------------------------ footnotes: insert, the popover editor
        insertFootnote() {
          if (!ed || !this.canEdit) return false;
          const note = ed.insertFootnote();
          if (!note) { toast('ضع المؤشّر داخل فقرة أولًا'); return false; }
          this.onEditorUpdate();
          this.openNote(note);
          return true;
        },
        openNote(note, opts = {}) {
          if (!ed || !note || !note.id) return false;
          const full = ed.noteAt(note.id);
          if (!full) return false;
          const same = this.pop.kind === 'note' && this.pop.anchorId === note.id;
          if (!same) this.closePop();
          this.note = { id: full.id, seq: full.seq, sourcePage: full.sourcePage, orphan: full.orphan };
          this.pop = { kind: 'note', anchorId: full.id, style: this.pop.style, above: false };
          opened();
          this.placePop();
          const mountNote = () => {
            const host = this.$refs && this.$refs.noteHost;
            if (!host || !bundle() || !bundle().createNote) return;
            if (noteEd) { noteEd.destroy(); noteEd = null; host.textContent = ''; }
            noteEd = bundle().createNote(host, {
              content: full.content,
              editable: this.canEdit,
              onUpdate: (content) => { if (ed && ed.setNoteContent(this.note.id, content)) this.onEditorUpdate(); },
              onSubmit: () => this.closePop(true), // Enter: done with the note, back to the text
            });
            this.placePop();
            if (!opts.keepFocus) noteEd.focus('end');
          };
          if (this.$nextTick) this.$nextTick(mountNote); else mountNote();
          this.liveMessage = `الحاشية ${full.seq}`;
          return true;
        },
        closeNote() {
          if (noteEd) { noteEd.destroy(); noteEd = null; }
          const host = this.$refs && this.$refs.noteHost;
          if (host) host.textContent = '';
        },
        noteEditor() { return noteEd; },
        noteBold() { if (noteEd) noteEd.toggleBold(); },
        noteItalic() { if (noteEd) noteEd.toggleItalic(); },
        deleteNote() {
          if (!ed || !this.canEdit || this.pop.kind !== 'note') return false;
          const id = this.note.id;
          this.closePop();
          const ok = ed.deleteNote(id);
          if (ok) { this.onEditorUpdate(); this.undoToast('حُذفت الحاشية', () => this.undo(), true); }
          return ok;
        },

        // ------------------------------------------------------------ uncertain words: the readings popover
        async openWord(word) {
          if (!ed || !word) return false;
          this.closePop();
          this.word = { from: word.from, to: word.to, text: word.text, typed: word.text, readings: [], loading: true };
          this.pop = { kind: 'word', anchorId: `${word.from}-${word.to}`, style: this.pop.style, above: false };
          opened();
          this.placePop();
          if (this.$nextTick) this.$nextTick(() => { this.placePop(); const f = this.$refs && this.$refs.wordTyped; if (f) { focusEl(f); if (f.select) f.select(); } });
          const block = ed.caretBlock();
          const readings = await this.loadReadings(block, word.text);
          if (this.pop.kind !== 'word' || this.word.from !== word.from) return true;
          this.word.readings = readings;
          this.word.loading = false;
          if (this.$nextTick) this.$nextTick(() => this.placePop());
          return true;
        },
        // The readings of the word from the review payload of its page: the primary and secondary models and
        // Tesseract (the review vocabulary); nothing when the page or the token cannot be found.
        async loadReadings(block, text) {
          if (!block || !block.sourcePages.length || !urls.chapters) return [];
          const page = block.sourcePages[0];
          const sheet = await this.fetchSheet(page);
          if (!sheet || !sheet.id) return [];
          let lines = readingsCache.get(sheet.id);
          if (!lines) {
            const root = String(urls.chapters).split('/books/')[0];
            const r = await api(`${root}/pages/${sheet.id}/review/`);
            if (!r.ok || !r.data || !Array.isArray(r.data.lines)) return [];
            lines = { lines: r.data.lines, labels: r.data.labels || {} };
            readingsCache.set(sheet.id, lines);
          }
          const own = new Set(block.sourceLineIds);
          const wanted = stripTashkeel(text).trim();
          const out = [];
          const seen = new Set();
          const push = (value, label) => { const v = String(value || '').trim(); if (v && !seen.has(v)) { seen.add(v); out.push({ value: v, label }); } };
          lines.lines.forEach((line) => {
            if (own.size && !own.has(line.id)) return;
            (line.tokens || []).forEach((tok) => {
              if (stripTashkeel(tok.t).trim() !== wanted && stripTashkeel(tok.alt).trim() !== wanted && stripTashkeel(tok.tess).trim() !== wanted) return;
              push(tok.t, lines.labels.primary || READING_LABELS.primary);
              push(tok.alt, lines.labels.secondary || READING_LABELS.secondary);
              push(tok.tess, READING_LABELS.tess);
            });
          });
          return out;
        },
        chooseReading(value) {
          if (!ed || this.pop.kind !== 'word') return false;
          const { from, to, text } = this.word;
          this.closePop(true);
          if (!this.canEdit) return false;
          if (value === text) ed.acceptUncertain(from, to); else ed.replaceRange(from, to, value);
          this.onEditorUpdate();
          this.liveMessage = value === text ? 'قُبلت الكلمة' : `صُحّحت إلى ${value}`;
          return true;
        },
        acceptWord() { return this.chooseReading(this.word.text); },
        submitTyped() {
          const typed = String(this.word.typed || '').trim();
          if (!typed) return false;
          return this.chooseReading(typed);
        },

        // ------------------------------------------------------------ the one overlay
        visibleArea() {
          const column = this.$refs && this.$refs.column;
          const c = rect(column);
          const W = (typeof window !== 'undefined' && window.innerWidth) || 1200;
          const H = (typeof window !== 'undefined' && window.innerHeight) || 800;
          const findH = this.find.open && this.$refs && this.$refs.findPanel ? (this.$refs.findPanel.offsetHeight || 44) : 0;
          return { top: TOPBAR_H + TOOLBAR_H + findH + POP_EDGE, bottom: H - POP_EDGE, left: Math.max(c.left, 0) + POP_EDGE, right: Math.min(c.right, W) - POP_EDGE };
        },
        anchorEl() {
          if (!ed) return null;
          if (this.pop.kind === 'note') { const note = ed.noteAt(this.pop.anchorId); return note ? note.dom : null; }
          if (this.pop.kind === 'word') {
            const dom = ed.dom;
            const hit = qa(dom, 'mark.ed-uncertain').find((el) => { try { const pos = ed.view.posAtDOM(el, 0); return pos >= this.word.from - 1 && pos <= this.word.to; } catch (_) { return false; } });
            return hit || null;
          }
          return null;
        },
        placePop() {
          const el = this.anchorEl();
          if (!el) return false;
          const node = this.$refs && this.$refs.pop;
          const guess = POP_SIZE[this.pop.kind] || POP_SIZE.note;
          const size = [node && node.offsetWidth ? node.offsetWidth : guess[0], node && node.offsetHeight ? node.offsetHeight : guess[1]];
          const placed = placeAgainst(rect(el), this.visibleArea(), rect(this.$refs && this.$refs.column), size, isRtl());
          this.pop = Object.assign({}, this.pop, placed);
          return true;
        },
        closePop(refocus) {
          const kind = this.pop.kind;
          if (!kind) return null;
          if (kind === 'note') this.closeNote();
          this.pop = { kind: null, anchorId: '', style: this.pop.style, above: false };
          if (refocus && ed) ed.focus();
          return kind;
        },
        // A click anywhere outside the overlay closes it. The click on a call or an uncertain word is never a
        // close: the popover opened on the mouseup before it (ProseMirror's click handling) and a click on the
        // same anchor goes through openNote / openWord again.
        onPopOutside(e) {
          if (!this.pop.kind || justOpened) return false;
          if (closest(e && e.target, '.ed-fn, mark.ed-uncertain')) return false;
          this.closePop();
          return true;
        },
        onScroll() {
          if (!this.pop.kind) return false;
          const el = this.anchorEl();
          const a = rect(el);
          const view = this.visibleArea();
          if (!el || a.bottom < view.top - POP_EDGE || a.top > view.bottom + POP_EDGE) { this.closePop(); return true; }
          return false;
        },
        onResize() { if (this.pop.kind) this.placePop(); if (this.find.open) this.measureFind(); },

        // ------------------------------------------------------------ the source («الأصل»): the pane and the drawer
        followCaret(block) {
          if (!block) return;
          const pages = block.sourcePages.slice().sort((a, b) => a - b);
          const unique = pages.filter((n, i) => pages.indexOf(n) === i);
          if (!unique.length) { this.source = { blockId: block.id, pages: [], index: 0, lines: [], sheet: null, loading: false, error: '' }; return; }
          const keepPage = this.source.blockId !== block.id ? 0 : clamp(this.source.index, 0, unique.length - 1);
          // the same scan as before (a neighbouring block on the page): only the line bands move, no reload
          const same = this.source.sheet && this.source.sheet.number === unique[keepPage] ? this.source.sheet : null;
          this.source = { blockId: block.id, pages: unique, index: keepPage, lines: block.sourceLineIds.slice(), sheet: same, loading: !same, error: '' };
          if (!same) this.loadSource(unique[keepPage]);
        },
        async fetchSheet(n, force) {
          if (!n) return null;
          if (!force && sheetCache.has(n)) return sheetCache.get(n);
          const sep = String(urls.sheets || '').includes('?') ? '&' : '?';
          const r = await api(`${urls.sheets}${sep}from=${n}&to=${n}`);
          const item = r.ok && r.data ? (Array.isArray(r.data) ? r.data : r.data.pages || []).find((p) => p && p.number === n) : null;
          if (item) sheetCache.set(n, item);
          else this.source.error = r.ok ? 'لا بيانات لهذه الصفحة.' : `تعذّر تحميل صورة الصفحة. ${r.message}`;
          return item || null;
        },
        async loadSource(n, force) {
          const item = await this.fetchSheet(n, force);
          if (this.sourcePage !== n) return false;
          this.source.loading = false;
          this.source.sheet = item;
          if (item) this.source.error = '';
          return Boolean(item);
        },
        sourceStep(dir) {
          const i = this.source.index + dir;
          if (i < 0 || i >= this.source.pages.length) return false;
          this.source.index = i;
          this.source.loading = true;
          this.source.sheet = null;
          return this.loadSource(this.source.pages[i]);
        },
        // The block's lines on the page (review band style), from the sheet's line boxes (ratios).
        sourceBoxes() {
          const s = this.source.sheet;
          if (!s || !Array.isArray(s.lines)) return [];
          const own = new Set(this.source.lines);
          return s.lines.filter((l) => Array.isArray(l.bbox) && l.bbox.length === 4 && own.has(l.id)).map((l) => ({ id: l.id, bbox: l.bbox }));
        },
        // Physical left/top: image pixels do not flip with the writing direction.
        boxStyle(b) {
          const pct = (v) => `${(100 * clamp(Number(v) || 0, 0, 1)).toFixed(2)}%`;
          return `left:${pct(b[0])};top:${pct(b[1])};width:${pct(b[2] - b[0])};height:${pct(b[3] - b[1])}`;
        },
        openDrawer() {
          if (!ed) return false;
          const block = ed.caretBlock();
          if (!block || !block.sourcePages.length) { toast('ضع المؤشّر في فقرة لها أصل'); return false; }
          this.closePop();
          if (this.source.blockId !== block.id) this.followCaret(block);
          returnTo = 'editor';
          this.drawerOpen = true;
          this.liveMessage = `الأصل: صفحة ${this.sourcePage}`;
          if (this.$nextTick) this.$nextTick(() => focusEl(this.$refs && this.$refs.drawerClose));
          return true;
        },
        closeDrawer() {
          if (!this.drawerOpen) return;
          this.drawerOpen = false;
          if (returnTo === 'editor' && ed) ed.focus();
          returnTo = null;
        },
        toggleDrawer() { if (this.drawerOpen) this.closeDrawer(); else this.openDrawer(); },

        // ------------------------------------------------------------ warnings («ملاحظات»)
        goToWarning(w) {
          if (!ed || !w) return false;
          if (w.blockId && ed.goToBlock(w.blockId)) { this.liveMessage = w.message || ''; return true; }
          toast('لم تُعثر على الفقرة في هذا الفصل');
          return false;
        },

        // ------------------------------------------------------------ snapshots
        // A dialog opens with the focus inside it and gives it back to the control that opened it.
        rememberTrigger() { lastTrigger = typeof document !== 'undefined' ? document.activeElement : null; },
        restoreTrigger() { const el = lastTrigger; lastTrigger = null; if (el && el.isConnected !== false) focusEl(el); },
        async openSnapshots() {
          this.closePop();
          this.rememberTrigger();
          this.snapshots = Object.assign({}, this.snapshots, { open: true, loading: true, error: '', label: '' });
          if (this.$nextTick) this.$nextTick(() => focusEl((this.$refs && this.$refs.snapshotLabel) || (this.$refs && this.$refs.snapshotsClose)));
          const r = await api(urls.snapshots);
          this.snapshots.loading = false;
          if (!r.ok || !Array.isArray(r.data)) { this.snapshots.error = r.message; return false; }
          this.snapshots.list = r.data;
          return true;
        },
        closeSnapshots() { if (!this.snapshots.open) return; this.snapshots.open = false; this.restoreTrigger(); },
        async createSnapshot() {
          if (!this.canEdit || this.snapshots.busy) return false;
          if (this.dirty || saving) { await this.saveNow(); if (this.dirty) { toast('تعذّر الحفظ قبل أخذ النسخة'); return false; } }
          this.snapshots.busy = true;
          const r = await api(urls.snapshots, { method: 'POST', body: { label: this.snapshots.label } });
          this.snapshots.busy = false;
          if (!r.ok || !r.data) { this.snapshots.error = r.message; return false; }
          this.snapshots.list = [r.data, ...this.snapshots.list.map((s) => Object.assign({}, s, { current: false }))];
          this.snapshots.label = '';
          this.snapshots.error = '';
          toast('حُفظت النسخة');
          return true;
        },
        async restoreSnapshot(id, opts = {}) {
          if (!this.canEdit || !id) return false;
          if (this.dirty || saving) { await this.saveNow(); if (this.dirty) { toast('تعذّر الحفظ قبل الاستعادة'); return false; } }
          this.snapshots.busy = true;
          const r = await api(fill(urls.restore, id), { method: 'POST' });
          this.snapshots.busy = false;
          if (!r.ok || !r.data) { toast(r.message); return false; }
          this.snapshots.open = false;
          this.closePop();
          await this.reloadCurrent();
          if (!opts.quiet) this.undoToast('استُعيدت النسخة', () => this.restoreSnapshot(r.data.snapshot, { quiet: true }));
          else toast('أُعيد النص كما كان');
          return true;
        },

        // ------------------------------------------------------------ digit conversion
        openDigits() {
          this.closePop();
          this.rememberTrigger();
          this.digits.open = true;
          if (this.$nextTick) this.$nextTick(() => focusEl(this.$refs && this.$refs.digitsFirst));
        },
        closeDigits() { if (!this.digits.open) return; this.digits.open = false; this.restoreTrigger(); },
        async convertDigits() {
          if (!this.canEdit || this.digits.busy) return false;
          if (this.dirty || saving) { await this.saveNow(); if (this.dirty) { toast('تعذّر الحفظ قبل التحويل'); return false; } }
          this.digits.busy = true;
          const body = { chapter: this.digits.scope === 'book' ? null : this.chapter.id, style: this.digits.style };
          const r = await api(urls.convertDigits, { method: 'POST', body });
          this.digits.busy = false;
          this.closeDigits();
          if (!r.ok || !r.data) { toast(r.message); return false; }
          const n = Number(r.data.changed) || 0;
          if (!n) { toast('لا أرقام تُحوَّل'); return true; }
          await this.reloadCurrent();
          this.undoToast(`حُوّل ${arCount(n, DIGITS)}`, () => this.restoreSnapshot(r.data.snapshot, { quiet: true }));
          return true;
        },

        // ------------------------------------------------------------ review drift: re-assemble this chapter (D41)
        async reassembleChapter() {
          if (!this.canEdit || !this.chapter || this.reassembly.running) return false;
          if (this.dirty || saving) { await this.saveNow(); if (this.dirty) { toast('تعذّر الحفظ قبل إعادة التجميع'); return false; } }
          const r = await api(fill(urls.reassemble, this.chapter.id), { method: 'POST' });
          if (!r.ok || !r.data) { toast(r.message); return false; }
          this.reassembly = { running: true, runId: r.data.run_id, error: '' };
          if (ed) ed.setEditable(false);
          this.liveMessage = 'تُعاد قراءة الفصل من صفحات المراجعة';
          pollFailures = 0;
          this.schedulePoll(POLL_MS);
          return true;
        },
        schedulePoll(ms) {
          clearTimeout(pollTimer);
          pollTimer = setTimeout(() => this.pollReassembly(), ms);
        },
        async pollReassembly() {
          if (!this.reassembly.running) return;
          const r = await api(urls.manuscriptState);
          if (!r.ok || !r.data) {
            pollFailures += 1;
            this.schedulePoll(Math.min(POLL_MS * (1 + pollFailures), POLL_MAX_MS));
            return;
          }
          const run = r.data.run;
          const mine = run && run.id === this.reassembly.runId;
          if (r.data.active || (mine && (run.status === 'queued' || run.status === 'running'))) { this.schedulePoll(POLL_MS); return; }
          this.reassembly = { running: false, runId: null, error: mine && run.status === 'error' ? run.error || 'تعذّرت إعادة تجميع الفصل.' : '' };
          if (ed) ed.setEditable(this.canEdit);
          if (this.reassembly.error) { toast(this.reassembly.error); return; }
          await this.reloadCurrent();
          this.driftPages = [];
          if (this.chapter) this.chapter.drift = false;
          toast('أُعيد تجميع الفصل من المراجعة؛ النص السابق محفوظ نسخةً');
        },
        // «الاحتفاظ بالنص»: the banner stays away for this chapter in this session (the list's refresh after a
        // save reads the drift from the server again).
        dismissDrift() { this.driftPages = []; if (this.chapter) { this.chapter.drift = false; dismissedDrift.add(this.chapter.id); } },

        // ------------------------------------------------------------ layers, keys
        openSheet() { this.closePop(); this.rememberTrigger(); this.sheetOpen = true; if (this.$nextTick) this.$nextTick(() => focusEl(this.$refs && this.$refs.sheetClose)); },
        closeSheet() { if (!this.sheetOpen) return; this.sheetOpen = false; this.restoreTrigger(); },
        // The layers, top-most first: the shortcut sheet, the snapshots, the digits, the conflict, the style menu,
        // the overlay, the drawer, the find panel.
        topLayer() {
          if (this.sheetOpen) return 'sheet';
          if (this.snapshots.open) return 'snapshots';
          if (this.digits.open) return 'digits';
          if (this.styleMenu) return 'styleMenu';
          if (this.pop.kind) return this.pop.kind;
          if (this.drawerOpen) return 'drawer';
          if (this.find.open) return 'find';
          return null;
        },
        // Esc closes the top-most layer only (DESIGN.md §10); with nothing open it leaves the sheet.
        closeTop() {
          const layer = this.topLayer();
          if (layer === 'sheet') this.closeSheet();
          else if (layer === 'snapshots') this.closeSnapshots();
          else if (layer === 'digits') this.closeDigits();
          else if (layer === 'styleMenu') this.styleMenu = false;
          else if (layer === 'note' || layer === 'word') this.closePop(true);
          else if (layer === 'drawer') this.closeDrawer();
          else if (layer === 'find') this.closeFind(true);
          return layer;
        },
        relativeTime(iso) { return relativeTime(iso, Date.now()); },
        // Arrows inside a menu move between its enabled items, wrapping (= manuscript movePop).
        moveIn(root, dir) {
          const items = qa(root, '.menu-item, .rv-opt, .rv-act').filter((el) => !el.disabled && !(typeof el.hasAttribute === 'function' && el.hasAttribute('disabled')));
          if (!items.length) return false;
          const active = typeof document !== 'undefined' ? document.activeElement : null;
          const i = items.indexOf(active);
          const next = i === -1 ? (dir > 0 ? 0 : items.length - 1) : (i + dir + items.length) % items.length;
          focusEl(items[next]);
          return true;
        },
        trapTab(e, root) {
          const items = qa(root, 'a[href], button:not([disabled]), input, [tabindex="0"]').filter(visible);
          if (!items.length) return;
          const first = items[0];
          const last = items[items.length - 1];
          const active = typeof document !== 'undefined' ? document.activeElement : null;
          if (e.shiftKey && active === first) { e.preventDefault(); focusEl(last); }
          else if (!e.shiftKey && active === last) { e.preventDefault(); focusEl(first); }
        },
        keyAction,
        onKey(e) {
          const t = e.target;
          const inField = Boolean(t && ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName));
          const inEditor = Boolean(closest(t, '.ed-doc'));
          const inNote = Boolean(closest(t, '.ed-note-editor'));
          const action = keyAction(e, { inField, inEditor, inNote });
          if (!action) return;
          if (action === 'escape') { if (this.closeTop()) e.preventDefault(); else if (inEditor && ed) ed.blur(); return; }
          if (e.defaultPrevented) return; // the editor's own keymap took it (B / I, undo, styles, ⌘⇧F)
          if (action === 'save') { e.preventDefault(); if (this.canEdit) this.saveNow(); return; }
          if (action === 'find') { e.preventDefault(); this.openFind(); return; }
          if (action === 'prevChapter' || action === 'nextChapter') { e.preventDefault(); if (action === 'prevChapter') this.prevChapter(); else this.nextChapter(); return; }
          if (action === 'sheet') { e.preventDefault(); this.openSheet(); return; }
          if (action === 'source') { e.preventDefault(); this.toggleDrawer(); return; }
          if (inField) return; // the find fields and the snapshot name keep every other key
          if (action === 'undo') { e.preventDefault(); this.undo(); return; }
          if (action === 'redo') { e.preventDefault(); this.redo(); return; }
          if (inNote) return; // the note editor keeps B / I
          if (action === 'bold') { e.preventDefault(); this.toggleBold(); return; }
          if (action === 'italic') { e.preventDefault(); this.toggleItalic(); return; }
          if (action === 'footnote') { e.preventDefault(); this.insertFootnote(); return; }
          if (STYLE_KEYS[e.code] === action) { e.preventDefault(); this.setStyle(action); }
        },
        onFindKey(e) {
          if (e.key === 'Enter') { e.preventDefault(); this.findStep(e.shiftKey ? -1 : 1); }
        },
      };
    });

    // The undo toast (DESIGN.md §6): one at a time, one action; a new one replaces the old. `local` marks a toast
    // whose undo is the editor's own history (the next edit retires it, or it would undo that edit instead).
    Alpine.store('editorToast', {
      message: '',
      visible: false,
      action: null,
      local: false,
      timer: null,
      show(message, action, ms = 8000, local = false) {
        clearTimeout(this.timer);
        this.message = message;
        this.action = typeof action === 'function' ? action : null;
        this.local = Boolean(local);
        this.visible = true;
        this.timer = setTimeout(() => { this.visible = false; this.action = null; }, ms);
      },
      run() {
        const fn = this.action;
        this.hide();
        if (fn) fn();
      },
      hide() {
        clearTimeout(this.timer);
        this.visible = false;
        this.action = null;
      },
    });
  });
})();
