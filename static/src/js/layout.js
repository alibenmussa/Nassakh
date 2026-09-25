// Layout page (Phase 5, docs/PHASE5_SPEC.md §5): the book's stylesheet beside its real pages.
//   bookLayout(config) – the stylesheet panel (every change → one debounced PUT → the renders queue → polling
//                        until the pages swap in place, the current page kept), the page viewer (the dashboard
//                        viewer's motion and input, D33: turns, keys, wheel, touch swipe, jump, filmstrip; a spread
//                        of two facing pages with the recto on the left; fit to the height, to the width or the
//                        trim size at 96 dpi), the footprint (page counts per chapter with the delta since the
//                        last change) and the rendering states (skeleton, updating, stale, error, missing fonts).
//   layoutBar          – the top-bar controls (base.html header_actions, outside the root) reading
//                        Alpine.store('layout').view.
// Config (editor.views.layout): editor.services.page_config(...) plus `initial` = {stylesheet (the api:stylesheet
// payload), preview (the api:preview payload of the book, never queued by the page itself), chapterPreview (the
// requested chapter's)}. The first GET at init queues a render when nothing is cached.
// Pages: the sequence shown is the newest finished book render; while the book is stale and the chapter under
// the reader's eyes has a fresh render of its own (D44: the chapter renders first, in seconds), that chapter's
// pages stand in for its range. A swap keeps the page by its printed number.
// The viewer copies the dashboard viewer's durations and thresholds one to one instead of sharing a module, so
// books.js keeps running alone under its two test harnesses. Western digits everywhere.
(function () {
  'use strict';

  const hasDOM = typeof window !== 'undefined' && typeof document !== 'undefined'
    && typeof document.createElement === 'function' && typeof document.querySelector === 'function';
  const POLL_MS = 1000; // while a render is queued or running
  const POLL_HIDDEN_MS = 2500; // a background tab polls slower
  const POLL_MAX_MS = 15000;
  const FAILURES_BEFORE_NOTICE = 3;
  const SAVE_DEBOUNCE_MS = 400; // stepper clicks coalesce into one PUT
  const SAVED_PILL_MS = 2000;
  const TURN_OUT_MS = 200; // = books.js (D33): a turn is 200 ms out + a two-frame entrance
  const WHEEL_TURN_X = 50;
  const WHEEL_TURN_Y = 90;
  const WHEEL_IDLE_MS = 260;
  const SWIPE_PX = 50;
  const FILM_EDGE_PX = 6;
  const SPREAD_MIN_PX = 640; // narrower stages show one page even in spread mode
  const SPREAD_GAP_PX = 12;
  const ACTIVE = ['queued', 'running'];
  const FIT_MODES = ['height', 'width', 'actual'];
  const FIT_KEY = 'nassakh.layout.fit';
  const SPREAD_KEY = 'nassakh.layout.spread';
  const PANEL_KEY = 'nassakh.layout.panel';
  const PAGE_KEY = 'nassakh.layout.page.'; // the page shown, per book (session)
  const STEPS = {
    width_mm: 1, height_mm: 1, top_mm: 1, bottom_mm: 1, inner_mm: 1, outer_mm: 1, bleed_mm: 0.5,
    body_size_pt: 0.5, line_height: 0.05, indent_em: 0.25, footnote_size_pt: 0.5,
    'heading_scale.h1': 0.05, 'heading_scale.h2': 0.05,
  };
  const MARGIN_OF = { top_mm: 'top', bottom_mm: 'bottom', inner_mm: 'inner', outer_mm: 'outer' };
  const FONT_FIELDS = { body: 'body_font', latin: 'latin_font', heading: 'heading_font' };
  const SAMPLES = { body: 'نسّاخ يُخرج الكتاب صفحةً صفحة', latin: 'Nassakh, 1234 pages', heading: 'الفصل الأول' };
  const PAGE_FORMS = ['صفحة واحدة', 'صفحتان', 'صفحات', 'صفحة'];
  const EASTERN_DIGITS = /[٠-٩۰-۹]/g;
  const SAVE_ERROR = 'تعذّر الحفظ · إعادة المحاولة';
  const ROLE_ERROR = 'هذا الإجراء يتطلب صلاحية محرّر.';
  const RENDER_ERROR = 'تعذّر إخراج صفحات المعاينة.';

  // ---------------------------------------------------------------- helpers
  const arCount = (n, forms) => {
    n = Number(n) || 0;
    if (n === 1) return forms[0];
    if (n === 2) return forms[1];
    const units = n % 100;
    return `${n} ${units >= 3 && units <= 10 ? forms[2] : forms[3]}`;
  };
  const westernDigits = (raw) => String(raw || '').replace(EASTERN_DIGITS, (d) => { const c = d.charCodeAt(0); return String(c >= 0x06f0 ? c - 0x06f0 : c - 0x0660); });
  // A number typed by hand: Western or Eastern digits, an Arabic decimal separator, NaN for anything else.
  const parseNumber = (raw) => {
    const text = westernDigits(raw).replace(/[٫,]/g, '.').replace(/[^\d.\-]/g, '').trim();
    return text ? parseFloat(text) : NaN;
  };
  const parsePageNumber = (raw) => {
    const digits = westernDigits(raw).replace(/\D/g, '');
    return digits ? parseInt(digits, 10) : NaN;
  };
  const round2 = (v) => Math.round(Number(v) * 100) / 100;
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const csrfToken = () => {
    const meta = hasDOM ? document.querySelector('meta[name="csrf-token"]') : null;
    return (meta && meta.content) || '';
  };
  const reduced = () => {
    try { return Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); } catch (_) { return false; }
  };
  const readLocal = (key, fallback) => { try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch (_) { return fallback; } };
  const writeLocal = (key, value) => { try { localStorage.setItem(key, String(value)); } catch (_) { /* private mode */ } };
  const readSession = (key) => { try { return sessionStorage.getItem(key); } catch (_) { return null; } };
  const writeSession = (key, value) => { try { sessionStorage.setItem(key, String(value)); } catch (_) { /* fine */ } };
  const toast = (message) => { if (window.Nassakh && window.Nassakh.toast) window.Nassakh.toast(message); };
  const q = (root, sel) => (root && typeof root.querySelector === 'function' ? root.querySelector(sel) : null);
  const setText = (el, text) => { if (el && el.textContent !== String(text)) el.textContent = String(text); };
  const frame = (fn) => { if (typeof requestAnimationFrame === 'function') requestAnimationFrame(fn); else setTimeout(fn, 16); };
  const getPath = (obj, path) => String(path).split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
  const setPath = (obj, path, value) => {
    const keys = String(path).split('.');
    let o = obj;
    keys.slice(0, -1).forEach((k) => { if (o[k] == null || typeof o[k] !== 'object') o[k] = {}; o = o[k]; });
    o[keys[keys.length - 1]] = value;
  };
  const copyPreview = (p) => (p ? Object.assign({}, p, { pages: (p.pages || []).slice(), chapters: (p.chapters || []).map((c) => Object.assign({}, c)) }) : null);
  const isActive = (p) => Boolean(p && (ACTIVE.includes(p.status) || p.rendering));
  const FONT_MENU_H = 300; // the face menu's height before it is measured (five faces with a sample line each)
  // A menu button inside the scrolling side panel: the menu opens above the button when the panel has no room
  // below it (the panel's overflow would clip it) and there is room above. Pure: rects of the button and panel.
  function menuAbove(button, panel, height) {
    const h = height || FONT_MENU_H;
    if (!button || !panel) return false;
    return button.bottom + h > panel.bottom && button.top - panel.top >= h;
  }

  document.addEventListener('alpine:init', () => {
  if (typeof Alpine.store === 'function') Alpine.store('layout', { view: null });

  Alpine.data('layoutBar', () => ({
    get v() { return typeof Alpine.store === 'function' && Alpine.store('layout') ? Alpine.store('layout').view : null; },
  }));

  Alpine.data('bookLayout', (cfg = {}) => {
    // Non-reactive plumbing lives in the closure: timers, the turn in flight, the input accumulators, the thumbs.
    const initial = cfg.initial || {};
    const urls = cfg.urls || {};
    const thumbs = []; // index → {el, img, num}: static thumbs patched by hand (no per-thumb Alpine bindings)
    const requested = new Set(); // hashes whose render this page asked for on its own (stale, nothing running)
    let filmEl = null;
    let canvasEl = null;
    let pollTimer = null;
    let saveTimer = null;
    let savedTimer = null;
    let turnTimer = null;
    let wheelTimer = null;
    let pendingTarget = -1;
    let pendingFocus = false;
    let wheelAcc = 0;
    let wheelLock = false;
    let swipeStart = null;
    let pendingN = 0; // a page asked for before the pages arrived (the address, the requested chapter)
    let pendingFlush = false;

    return {
    bookId: cfg.bookId,
    title: cfg.title || '',
    canEdit: Boolean(cfg.canEdit),
    urls,
    chapters: cfg.chapters || [], // [{id, number, kind, title}] in the manuscript's order
    chapterId: cfg.requestedChapter || null, // the chapter whose own render is tracked (opened from the editor, or the last change's)
    // the stylesheet and its options (api:stylesheet)
    sheet: {},
    saved: false,
    trims: [],
    fonts: [],
    latinFonts: [],
    choices: {},
    limits: {},
    missingFonts: [],
    errors: {}, // field → message from a 400
    dirty: {}, // dotted path → value waiting for the next PUT
    save: { state: '', message: '' }, // the top-bar pill (= rv-save): saving | saved | invalid | error
    saving: false,
    focusMargin: '', // the margin stepper with the focus: the diagram highlights it
    // the previews (api:preview)
    book: null,
    chapter: null,
    pages: [], // the sequence shown: [{n, url, url2x, chapter, scope}]
    ranges: [], // [{id, title, first, last, fresh}] of the sequence
    delta: 0, // page count change of the newest book render against the one before
    chapterDeltas: {},
    trimShown: null, // [width_mm, height_mm] of the pages on screen: the trim of the newest fresh book render
    // the viewer
    cursor: -1, // index into `pages`
    turning: '', // '' | out-next | out-prev | in-next | in-prev
    spread: readLocal(SPREAD_KEY, '0') === '1',
    fit: FIT_MODES.includes(readLocal(FIT_KEY, 'height')) ? readLocal(FIT_KEY, 'height') : 'height',
    stageWide: true,
    panel: readLocal(PANEL_KEY, 'style') === 'pages' ? 'pages' : 'style',
    loaded: {}, // page image url → true once drawn (a page seen before never shimmers again)
    liveMessage: '',
    pollState: 'ok', // ok | error | auth
    failures: 0,
    stopped: false,
    retrying: false,

    init() {
      this.applyStylesheet(initial.stylesheet || null);
      if (initial.preview) this.applyPreview('book', initial.preview, { quiet: true });
      if (initial.chapterPreview && this.chapterId) this.applyPreview('chapter', initial.chapterPreview, { quiet: true });
      this.bindDom();
      if (typeof Alpine.store === 'function' && Alpine.store('layout')) Alpine.store('layout').view = this;
      this.restorePosition();
      this.pollNow(); // the first GET queues a render when nothing is cached
    },
    destroy() {
      this.stopped = true;
      clearTimeout(pollTimer);
      clearTimeout(saveTimer);
      clearTimeout(savedTimer);
      clearTimeout(turnTimer);
      clearTimeout(wheelTimer);
      if (typeof Alpine.store === 'function' && Alpine.store('layout')) Alpine.store('layout').view = null;
    },

    // ------------------------------------------------------------ the stylesheet panel
    applyStylesheet(payload) {
      if (!payload || !payload.stylesheet) return;
      const values = JSON.parse(JSON.stringify(payload.stylesheet));
      values.heading_scale = Object.assign({ h1: 1.6, h2: 1.25 }, values.heading_scale || {});
      values.front_matter = Object.assign({ title_page: true, contents: true }, values.front_matter || {});
      this.sheet = values;
      this.saved = Boolean(payload.saved);
      if (payload.trims) this.trims = payload.trims;
      if (payload.fonts) this.fonts = payload.fonts;
      if (payload.latin_fonts) this.latinFonts = payload.latin_fonts;
      if (payload.choices) this.choices = payload.choices;
      if (payload.limits) this.limits = payload.limits;
      if (payload.missing_fonts) this.missingFonts = payload.missing_fonts;
    },
    value(path) {
      return getPath(this.sheet, path);
    },
    // «20», «1.7», «12.5»: at most two decimals, Western digits, nothing for a missing value.
    fmt(v) {
      const n = Number(v);
      return Number.isFinite(n) ? String(round2(n)) : '';
    },
    limit(path) {
      const pair = this.limits[path];
      return Array.isArray(pair) && pair.length === 2 ? pair : [-Infinity, Infinity];
    },
    stepOf(path) {
      return STEPS[path] || 1;
    },
    step(path, dir) {
      const now = Number(this.value(path));
      const base = Number.isFinite(now) ? now : this.limit(path)[0];
      return this.setField(path, base + dir * this.stepOf(path));
    },
    // A change from a control: numbers are parsed (Eastern digits welcome) and clamped to the field's limits;
    // the value shows at once, the PUT follows after a short quiet.
    setField(path, raw) {
      let value = raw;
      if (path in STEPS) {
        value = typeof raw === 'number' ? raw : parseNumber(raw);
        if (!Number.isFinite(value)) { this.sheet = Object.assign({}, this.sheet); return false; } // re-render the old value
        const [lo, hi] = this.limit(path);
        value = round2(clamp(value, lo, hi));
      }
      const next = JSON.parse(JSON.stringify(this.sheet));
      setPath(next, path, value);
      this.sheet = next;
      this.dirty[path] = value;
      const field = path.split('.')[0];
      if (this.errors[field]) { const errors = Object.assign({}, this.errors); delete errors[field]; this.errors = errors; }
      this.scheduleSave();
      return true;
    },
    setTrim(key) {
      const preset = this.trims.find((t) => t.key === key);
      if (!preset) return false;
      const next = JSON.parse(JSON.stringify(this.sheet));
      next.trim = key;
      if (preset.width_mm && preset.height_mm) { next.width_mm = preset.width_mm; next.height_mm = preset.height_mm; }
      this.sheet = next;
      this.dirty.trim = key;
      if (key === 'custom') { this.dirty.width_mm = next.width_mm; this.dirty.height_mm = next.height_mm; }
      this.scheduleSave();
      return true;
    },
    focusField(path) {
      this.focusMargin = MARGIN_OF[path] || '';
    },
    scheduleSave() {
      if (!this.canEdit) return;
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => this.flush(), SAVE_DEBOUNCE_MS);
    },
    // The waiting changes as one nested body ({heading_scale: {h1}}), emptied.
    takeDirty() {
      const body = {};
      Object.keys(this.dirty).forEach((path) => setPath(body, path, this.dirty[path]));
      this.dirty = {};
      return body;
    },
    mergeBack(body) {
      const walk = (obj, prefix) => Object.keys(obj).forEach((k) => {
        const v = obj[k];
        const path = prefix ? `${prefix}.${k}` : k;
        if (v && typeof v === 'object' && !Array.isArray(v)) walk(v, path);
        else if (!(path in this.dirty) && k !== 'chapter') this.dirty[path] = v;
      });
      walk(body, '');
    },
    // PUT the waiting changes with the chapter under the reader's eyes (it renders first, D44); the server's
    // values (rounded, validated) replace the local ones and its preview payload starts the polling.
    // `keepalive`: the page is leaving (E, «فتح المحرّر», the dashboard link) and the request must outlive it.
    async flush(opts = {}) {
      clearTimeout(saveTimer);
      if (!this.canEdit || !urls.stylesheet) return false;
      if (this.saving) { pendingFlush = true; return false; }
      const body = this.takeDirty();
      if (!Object.keys(body).length) return false;
      const focus = this.focusChapter;
      if (focus) body.chapter = focus;
      this.saving = true;
      this.save = { state: 'saving', message: 'يُحفظ…' };
      let res = null;
      let data = null;
      try {
        res = await fetch(urls.stylesheet, {
          method: 'PUT',
          credentials: 'same-origin',
          cache: 'no-store',
          keepalive: Boolean(opts.keepalive),
          headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
          body: JSON.stringify(body),
        });
        try { data = await res.json(); } catch (_) { data = null; }
      } catch (_) {
        res = null;
      }
      this.saving = false;
      if (res && res.ok && data) {
        // the server's values win, except a field refused earlier and not sent now (it keeps the typed
        // value and its message until it is corrected) and the changes made while the PUT was on the wire
        // (they are on their way in the next one)
        const refused = Object.keys(this.errors).filter((field) => !(field in body));
        const kept = {};
        refused.forEach((field) => { kept[field] = this.value(field); });
        const waiting = Object.assign({}, this.dirty);
        this.applyStylesheet(data);
        if (refused.length || Object.keys(waiting).length) {
          const next = JSON.parse(JSON.stringify(this.sheet));
          refused.forEach((field) => setPath(next, field, kept[field]));
          Object.keys(waiting).forEach((path) => setPath(next, path, waiting[path]));
          this.sheet = next;
        }
        const errors = {};
        refused.forEach((field) => { errors[field] = this.errors[field]; });
        this.errors = errors;
        if (focus) this.chapterId = focus;
        if (data.preview) this.applyPreview('book', data.preview);
        if (refused.length) {
          this.save = { state: 'invalid', message: 'لم يُحفظ · صحّح القيم' };
        } else {
          this.save = { state: 'saved', message: 'حُفظ' };
          clearTimeout(savedTimer);
          savedTimer = setTimeout(() => { if (this.save.state === 'saved') this.save = { state: '', message: '' }; }, SAVED_PILL_MS);
        }
        this.pollNow();
      } else if (res && res.status === 400) {
        this.errors = (data && data.errors) || {};
        this.save = { state: 'invalid', message: (data && data.detail) || 'لم يُحفظ · صحّح القيم' };
        // the valid fields of the same PUT are sent again without the refused ones
        Object.keys(this.errors).forEach((field) => { delete body[field]; });
        delete body.chapter;
        if (Object.keys(body).length) { this.mergeBack(body); this.scheduleSave(); }
      } else if (res && (res.status === 401 || res.status === 403)) {
        this.save = { state: 'error', message: res.status === 403 ? ROLE_ERROR : 'انتهت الجلسة · سجّل الدخول' };
        this.mergeBack(body);
      } else {
        this.save = { state: 'error', message: SAVE_ERROR };
        this.mergeBack(body); // «إعادة المحاولة» sends them again
      }
      if (pendingFlush) { pendingFlush = false; this.flush(); }
      return Boolean(res && res.ok);
    },
    // Leaving the page (a link, the E key, the tab closing) with changes still waiting for the debounce or a
    // failed save: they go now, in a request the browser keeps alive after the page is gone.
    onUnload() {
      if (!this.canEdit) return false;
      const waiting = Object.keys(this.dirty).length > 0;
      if (!waiting) return false;
      if (this.saving) { pendingFlush = true; return false; } // the running PUT sends them next (same tab only)
      this.flush({ keepalive: true });
      return true;
    },
    get savePill() {
      return { state: this.save.state, text: this.save.message };
    },
    // ---- fonts
    font(key) {
      return this.fonts.find((f) => f.key === key) || null;
    },
    fontChoices(role) {
      return role === 'latin' ? this.fonts.filter((f) => f.latin) : this.fonts;
    },
    fontLabel(key) {
      const f = this.font(key);
      return f ? f.label : key || '';
    },
    fontInstalled(key) {
      const f = this.font(key);
      return Boolean(f && f.installed);
    },
    // The face itself, from the Mac's fonts (Amiri is vendored); a missing face shows in the UI font.
    faceStyle(key) {
      const f = this.font(key);
      const family = f ? f.family : key;
      return family ? `font-family: "${String(family).replace(/"/g, '')}", var(--font-sans)` : '';
    },
    sample(role) {
      return SAMPLES[role] || SAMPLES.body;
    },
    fontField(role) {
      return FONT_FIELDS[role] || 'body_font';
    },
    menuAbove,
    // Whether a face menu opening from `button` should open upwards (the panel would clip it below).
    fontMenuAbove(button) {
      const panel = button && typeof button.closest === 'function' ? button.closest('.lo-side') : null;
      const rect = (el) => (el && typeof el.getBoundingClientRect === 'function' ? el.getBoundingClientRect() : null);
      return menuAbove(rect(button), rect(panel), FONT_MENU_H);
    },
    get missingFontText() {
      if (!this.missingFonts.length) return '';
      const names = [...new Set(this.missingFonts.map((m) => m.name))];
      const fallback = this.missingFonts[0].fallback || 'Amiri';
      return `${names.join('، ')} غير مثبّت على هذا الجهاز؛ تُخرَج الصفحات بخط ${fallback} بدلًا منه.`;
    },
    // ---- the page diagram (margins as fractions of the trim)
    get trimLabel() {
      const preset = this.trims.find((t) => t.key === this.sheet.trim);
      if (preset && preset.width_mm) return preset.label;
      return `${this.fmt(this.sheet.width_mm)}×${this.fmt(this.sheet.height_mm)} مم`;
    },
    // the trim of the pages on screen (the sheet's until a render of it exists)
    get pageRatio() {
      const [w, h] = this.trimShown || [Number(this.sheet.width_mm) || 170, Number(this.sheet.height_mm) || 240];
      return w / h;
    },
    get diagramStyle() {
      const w = Number(this.sheet.width_mm) || 170;
      const h = Number(this.sheet.height_mm) || 240;
      const f = (v, total) => `${clamp((Number(v) || 0) / total, 0, 0.45).toFixed(4)}`;
      return `--lo-ar: ${(w / h).toFixed(4)}; --lo-t: ${f(this.sheet.top_mm, h)}; --lo-b: ${f(this.sheet.bottom_mm, h)}; --lo-i: ${f(this.sheet.inner_mm, w)}; --lo-o: ${f(this.sheet.outer_mm, w)}`;
    },
    // «17×24 سم · النص 130×198 مم»: the type area, so a margin change reads as a number too
    get textAreaText() {
      const w = (Number(this.sheet.width_mm) || 0) - (Number(this.sheet.inner_mm) || 0) - (Number(this.sheet.outer_mm) || 0);
      const h = (Number(this.sheet.height_mm) || 0) - (Number(this.sheet.top_mm) || 0) - (Number(this.sheet.bottom_mm) || 0);
      return w > 0 && h > 0 ? `${this.fmt(w)}×${this.fmt(h)} مم` : '';
    },
    trimGlyphStyle(t) {
      const w = Number(t.width_mm) || 170;
      const h = Number(t.height_mm) || 240;
      return `aspect-ratio: ${w} / ${h}`;
    },
    choiceLabel(field, value) {
      const item = (this.choices[field] || []).find((c) => c.value === value);
      return item ? item.label : value;
    },

    // ------------------------------------------------------------ previews and polling
    get previewUrl() {
      return urls.preview || '';
    },
    scopeUrl(scope, chapterId) {
      const sep = this.previewUrl.includes('?') ? '&' : '?';
      return scope === 'chapter'
        ? `${this.previewUrl}${sep}scope=chapter&chapter=${encodeURIComponent(chapterId)}`
        : `${this.previewUrl}${sep}scope=book`;
    },
    schedule(ms) {
      clearTimeout(pollTimer);
      pollTimer = setTimeout(() => this.poll(), ms);
    },
    pollNow() {
      clearTimeout(pollTimer);
      return this.poll();
    },
    // The tab is back: a render in progress (or a poll in trouble) is looked at now, not at the slow tick.
    onVisible() {
      if (hasDOM && document.hidden) return false;
      if (this.stopped || !(this.active || this.failures)) return false;
      this.pollNow();
      return true;
    },
    get active() {
      return isActive(this.book) || (Boolean(this.chapterId) && isActive(this.chapter));
    },
    async fetchPreview(scope, chapterId) {
      const res = await fetch(this.scopeUrl(scope, chapterId), { headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store' });
      if (res.status === 401 || res.status === 403) { this.stopped = true; this.pollState = 'auth'; return null; }
      if (res.status === 404 && scope === 'chapter') { this.chapterId = null; this.chapter = null; return null; } // the chapter is gone
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    },
    async poll() {
      if (this.stopped || !this.previewUrl) return;
      if (hasDOM && document.hidden) { this.schedule(POLL_HIDDEN_MS); return; } // do not hammer the server from a background tab
      try {
        const book = await this.fetchPreview('book');
        if (book) this.applyPreview('book', book);
        if (this.chapterId) {
          const chapter = await this.fetchPreview('chapter', this.chapterId);
          if (chapter) this.applyPreview('chapter', chapter);
        }
        if (this.stopped) return;
        this.failures = 0;
        this.pollState = 'ok';
      } catch (_) {
        this.failures += 1;
        if (this.failures >= FAILURES_BEFORE_NOTICE) this.pollState = 'error';
      }
      if (this.active || this.failures) this.schedule(Math.min(POLL_MS * (1 + this.failures), POLL_MAX_MS));
    },
    // A payload of a scope: remembered, the sequence rebuilt (the current page kept), the polling kept going
    // while a render is on; a stale content with nothing running is asked for once.
    applyPreview(scope, payload, opts = {}) {
      const slot = scope === 'chapter' ? 'chapter' : 'book';
      const before = this[slot];
      const p = copyPreview(payload);
      this[slot] = p;
      if (slot === 'book') this.noteRender(before, p);
      // the sheets keep the shape of the pages drawn in them: a trim change reshapes them with the render
      if (p && !p.stale && p.pages && p.pages.length) this.trimShown = [Number(this.sheet.width_mm) || 170, Number(this.sheet.height_mm) || 240];
      this.rebuild();
      if (!opts.quiet) this.maybeRequest(scope, p);
    },
    noteRender(before, p) {
      if (!p || !p.render_id || !before || !before.render_id || before.render_id === p.render_id) return;
      if (before.page_count) this.delta = (p.page_count || 0) - before.page_count;
      const old = new Map((before.chapters || []).map((c) => [c.id, c.last - c.first + 1]));
      const deltas = {};
      (p.chapters || []).forEach((c) => { if (old.has(c.id)) { const d = c.last - c.first + 1 - old.get(c.id); if (d) deltas[c.id] = d; } });
      this.chapterDeltas = deltas;
    },
    maybeRequest(scope, p) {
      if (!p || !this.canEdit) return;
      if (p.stale && !p.rendering && (p.status === 'none' || p.status === 'error') && p.hash && !requested.has(p.hash)) {
        requested.add(p.hash);
        this.requestRender(scope);
      }
    },
    async requestRender(scope) {
      if (!this.canEdit || !this.previewUrl) return false;
      const body = scope === 'chapter' ? { scope: 'chapter', chapter: this.chapterId } : { scope: 'book' };
      try {
        const res = await fetch(this.previewUrl, {
          method: 'POST',
          credentials: 'same-origin',
          cache: 'no-store',
          headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
          body: JSON.stringify(body),
        });
        if (!res.ok) return false;
        const data = await res.json();
        if (data && data.scope) this.applyPreview(data.scope, data, { quiet: true });
        if (this.active) this.schedule(POLL_MS);
        return true;
      } catch (_) {
        return false;
      }
    },
    // «إعادة المحاولة»: the book (and the tracked chapter) rendered again after an error.
    async retry() {
      if (this.retrying) return false;
      this.retrying = true;
      try {
        const ok = await this.requestRender('book');
        if (this.chapterId && this.chapter && this.chapter.status === 'error') await this.requestRender('chapter');
        if (!ok) toast('تعذّر طلب المعاينة. حاول مرة أخرى.');
        return ok;
      } finally {
        this.retrying = false;
      }
    },
    // The sequence: the book's pages; the tracked chapter's own pages stand in for its range while the book
    // is stale and the chapter is fresh (or when no book render exists yet).
    rebuild() {
      const book = this.book;
      const ch = this.chapter;
      const wasN = this.cursor >= 0 && this.pages[this.cursor] ? this.pages[this.cursor] : null;
      let pages = book && book.pages ? book.pages.map((p) => Object.assign({}, p, { scope: 'book' })) : [];
      let ranges = book && book.chapters ? book.chapters.map((c) => Object.assign({}, c, { fresh: false })) : [];
      const chapterFresh = Boolean(ch && ch.pages && ch.pages.length && ch.render_id && !ch.stale && this.chapterId && ch.chapter === this.chapterId);
      if (chapterFresh && (!pages.length || book.stale)) {
        const own = ch.pages.map((p) => Object.assign({}, p, { scope: 'chapter', chapter: ch.chapter }));
        const range = ranges.find((c) => c.id === ch.chapter);
        if (!pages.length) {
          pages = own;
          ranges = (ch.chapters || []).map((c) => Object.assign({}, c, { fresh: true }));
        } else if (range) {
          const start = pages.findIndex((p) => p.n === range.first);
          const end = pages.findIndex((p) => p.n === range.last);
          if (start >= 0 && end >= start) {
            pages.splice(start, end - start + 1, ...own);
            range.first = ch.first_page;
            range.last = ch.first_page + own.length - 1;
            range.fresh = true;
          }
        }
      }
      this.pages = pages;
      this.ranges = ranges;
      this.syncFilm();
      // the current page survives a swap by its printed number (the same chapter's copy first); the first
      // pages to arrive land on the page asked for (the address, the requested chapter), else the first
      let idx = -1;
      if (wasN) {
        idx = pages.findIndex((p) => p.n === wasN.n && p.chapter === wasN.chapter);
        if (idx < 0) idx = pages.findIndex((p) => p.n === wasN.n);
        if (idx < 0 && pages.length) idx = Math.min(this.cursor, pages.length - 1);
        this.cursor = idx >= 0 && this.spreadOn ? this.canonical(idx) : idx;
        // the page kept by its number: nothing else moves; kept by position only (the number is gone), the
        // address, the session and the counter follow the number now on screen
        if (this.cursor >= 0 && this.current !== wasN.n) this.land(this.cursor);
        else { this.markThumbs(); if (this.cursor >= 0) this.prefetch(this.cursor); }
        return;
      }
      if (!pages.length) { this.cursor = -1; return; }
      idx = pendingN ? pages.findIndex((p) => p.n === pendingN) : -1;
      if (idx < 0 && this.chapterId) { const r = ranges.find((c) => c.id === this.chapterId); if (r) idx = pages.findIndex((p) => p.n === r.first); }
      if (idx < 0) idx = 0;
      pendingN = 0;
      this.land(this.spreadOn ? this.canonical(idx) : idx);
    },

    // ------------------------------------------------------------ derived state
    get pageCount() {
      return this.pages.length;
    },
    get current() {
      const p = this.pages[this.cursor];
      return p ? p.n : 0;
    },
    get rendering() {
      return this.active;
    },
    get stale() {
      return Boolean(this.book && this.book.stale) && !this.active;
    },
    get errorText() {
      const failed = [this.book, this.chapter].find((p) => p && p.status === 'error');
      return failed ? failed.error || RENDER_ERROR : '';
    },
    // skeleton (nothing cached, a render on its way) | error (nothing cached, the render failed) | pages
    get phase() {
      if (this.pages.length) return 'pages';
      if (this.book && this.book.status === 'error') return 'error';
      return 'skeleton';
    },
    // the toolbar's quiet pill: updating, stale, or the failure with the retry
    get renderPill() {
      if (this.active) return { state: 'saving', text: 'تُحدَّث المعاينة…', action: '' };
      if (this.errorText) { // with nothing cached the stage carries the failure itself
        return this.pages.length ? { state: 'error', text: 'تعذّر تحديث المعاينة · إعادة المحاولة', action: 'retry' } : { state: '', text: '', action: '' };
      }
      if (this.stale) return { state: 'warn', text: 'المعاينة أقدم من النص', action: this.canEdit ? 'retry' : '' };
      return { state: '', text: '', action: '' };
    },
    rangeAt(n) {
      return this.ranges.find((c) => c.first <= n && n <= c.last) || null;
    },
    chapterTitle(id) {
      const c = this.chapters.find((x) => x.id === id);
      return c ? c.title : '';
    },
    // the chapter under the reader's eyes: it renders first after a change and the editor opens on it
    get focusChapter() {
      const p = this.pages[this.cursor];
      if (p && p.chapter) return p.chapter;
      const r = p ? this.rangeAt(p.n) : null;
      return r ? r.id : this.chapterId || (this.chapters[0] ? this.chapters[0].id : null);
    },
    get counterChapter() {
      const p = this.pages[this.cursor];
      const r = p ? this.rangeAt(p.n) : null;
      if (!r) return null;
      return { id: r.id, title: this.chapterTitle(r.id) || r.title || '', first: r.first, last: r.last, fresh: Boolean(r.fresh) };
    },
    get editorHref() {
      const base = urls.editor || '';
      const focus = this.focusChapter;
      return base && focus ? `${base}${base.includes('?') ? '&' : '?'}chapter=${encodeURIComponent(focus)}` : base;
    },
    get pdfUrl() {
      return (this.book && this.book.pdf_url) || '';
    },
    // the footprint: «412 صفحة» with the delta, «يُحسب…» while the book render runs, one row per chapter
    get footprint() {
      const book = this.book;
      const count = book && book.page_count ? book.page_count : 0;
      const rows = this.chapters.map((c) => {
        const r = this.ranges.find((x) => x.id === c.id);
        return {
          id: c.id,
          number: c.number,
          kind: c.kind,
          title: c.title,
          first: r ? r.first : null,
          last: r ? r.last : null,
          pages: r ? r.last - r.first + 1 : null,
          delta: this.chapterDeltas[c.id] || 0,
          fresh: Boolean(r && r.fresh),
          current: this.focusChapter === c.id,
        };
      });
      return {
        count,
        text: count ? arCount(count, PAGE_FORMS) : '',
        computing: isActive(book),
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
    // In an Arabic book the odd (recto) page sits on the left, the even (verso) on the right: a spread is
    // (even n, odd n + 1). The canonical index of a spread is its right page's when it has one.
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
    get rightPage() {
      return this.pages[this.shown.right] || null;
    },
    get leftPage() {
      return this.pages[this.shown.left] || null;
    },
    get shownNumbers() {
      return [this.rightPage, this.leftPage].filter(Boolean).map((p) => p.n).sort((a, b) => a - b);
    },
    // «صفحة 37 من 412», «الصفحتان 36–37 من 412»
    get counterText() {
      const nums = this.shownNumbers;
      if (!nums.length) return '';
      return nums.length === 2 ? `الصفحتان ${nums[0]}–${nums[1]} من ${this.pageCount}` : `صفحة ${nums[0]} من ${this.pageCount}`;
    },
    srcset(p) {
      return p ? (p.url2x ? `${p.url} 1x, ${p.url2x} 2x` : p.url) : '';
    },
    get sheetStyle() {
      const [w, h] = this.trimShown || [Number(this.sheet.width_mm) || 170, Number(this.sheet.height_mm) || 240];
      return `--lo-ar: ${(w / h).toFixed(4)}; --lo-w: ${w}; --lo-gap: ${SPREAD_GAP_PX}px`;
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
      void this.cursor; void this.pages.length; void this.spreadOn; // reactive dependencies of the buttons
      return this.neighbour(dir) !== null;
    },
    turn(dir) {
      const idx = this.neighbour(dir);
      if (idx === null) return false;
      return this.showIndex(idx, { dir });
    },
    showPage(n, opts = {}) {
      let idx = this.pages.findIndex((p) => p.n === Number(n));
      if (idx < 0) { if (!this.pages.length) { pendingN = Number(n) || 0; } return false; }
      return this.showIndex(idx, opts);
    },
    // Show the page at `idx`. With a page on screen it turns: the sheet slides out in the reading direction,
    // the new one slides in (review-screen motion); pressing again mid-turn only moves the target.
    showIndex(idx, opts = {}) {
      if (idx < 0 || idx >= this.pages.length) return false;
      const target = this.spreadOn ? this.canonical(idx) : idx;
      if (target === this.cursor && !this.turning) { if (opts.focus) this.focusStage(); return true; }
      const base = this.turning ? pendingTarget : this.cursor;
      const dir = opts.dir || (base >= 0 && target < base ? -1 : 1);
      pendingTarget = target;
      pendingFocus = pendingFocus || Boolean(opts.focus);
      if (this.cursor < 0 || opts.instant || reduced()) {
        clearTimeout(turnTimer);
        this.turning = '';
        this.land(target);
        if (pendingFocus) { this.focusStage(); pendingFocus = false; }
        return true;
      }
      if (this.turning) return true; // the running turn lands on the latest target
      this.turning = dir > 0 ? 'out-next' : 'out-prev';
      clearTimeout(turnTimer);
      turnTimer = setTimeout(() => this.landTurn(dir), TURN_OUT_MS);
      return true;
    },
    landTurn(dir) {
      this.land(pendingTarget);
      this.turning = dir > 0 ? 'in-next' : 'in-prev';
      frame(() => frame(() => {
        this.turning = '';
        if (pendingFocus) { this.focusStage(); pendingFocus = false; }
        if (pendingTarget >= 0 && pendingTarget !== this.cursor) this.showIndex(pendingTarget); // pressed again during the entrance
      }));
    },
    land(idx) {
      this.cursor = idx;
      this.markThumbs();
      this.prefetch(idx);
      const n = this.current;
      if (n) {
        writeSession(PAGE_KEY + (this.bookId || ''), String(n));
        if (typeof history !== 'undefined' && history.replaceState && typeof window !== 'undefined' && window.location) {
          try { history.replaceState(null, '', `${window.location.pathname}${window.location.search}#page-${n}`); } catch (_) { /* sandboxed */ }
        }
        this.liveMessage = this.counterText;
      }
      this.centerFilm();
    },
    get turnClass() {
      return this.turning ? `is-${this.turning}` : '';
    },
    focusStage() {
      const stage = this.$refs && this.$refs.stage;
      if (stage && stage.focus) stage.focus({ preventScroll: true });
    },
    // The neighbours' images are fetched ahead, so the next turn lands on a drawn page.
    prefetch(idx) {
      if (typeof Image !== 'function') return;
      const reach = this.spreadOn ? 3 : 1;
      for (let d = -reach; d <= reach; d += 1) {
        const p = this.pages[idx + d];
        if (p && d !== 0 && p.url) { const img = new Image(); img.src = p.url; }
      }
    },
    // The sheet shimmers until its image is drawn (or fails: a broken image is shown as such, never a shimmer).
    onLoad(side) {
      const p = side === 'left' ? this.leftPage : this.rightPage;
      if (p && p.url && !this.loaded[p.url]) this.loaded = Object.assign({}, this.loaded, { [p.url]: true });
    },
    isLoading(p) {
      return Boolean(p && p.url && !this.loaded[p.url]);
    },
    setSpread(on) {
      this.spread = Boolean(on);
      writeLocal(SPREAD_KEY, this.spread ? '1' : '0');
      if (this.cursor >= 0 && this.spreadOn) this.cursor = this.canonical(this.cursor);
      this.markThumbs();
      this.centerFilm();
    },
    toggleSpread() {
      this.setSpread(!this.spread);
    },
    setFit(mode) {
      this.fit = FIT_MODES.includes(mode) ? mode : 'height';
      writeLocal(FIT_KEY, this.fit);
    },
    setPanel(name) {
      this.panel = name === 'pages' ? 'pages' : 'style';
      writeLocal(PANEL_KEY, this.panel);
      if (this.panel === 'pages') this.centerFilm();
    },
    onResize() {
      const width = canvasEl && canvasEl.clientWidth ? canvasEl.clientWidth : 0;
      const wide = width ? width >= SPREAD_MIN_PX : true;
      if (wide !== this.stageWide) { this.stageWide = wide; if (this.cursor >= 0 && this.spreadOn) this.cursor = this.canonical(this.cursor); this.markThumbs(); }
    },
    goToChapter(id) {
      const r = this.ranges.find((c) => c.id === id);
      if (!r) { toast('لم تُخرَج صفحات هذا الفصل بعد'); return false; }
      return this.showPage(r.first, { manual: true });
    },
    // ---- jump, keyboard, position
    jumpTarget(value) {
      const n = parsePageNumber(value);
      if (!Number.isFinite(n) || !this.pages.some((p) => p.n === n)) return null;
      return n;
    },
    jump(value) {
      const n = this.jumpTarget(value);
      if (n === null) { toast('لا صفحة بهذا الرقم'); return null; }
      this.showPage(n, { focus: true, manual: true });
      return n;
    },
    focusJump() {
      const field = this.$refs && this.$refs.jump;
      if (field && field.focus) { field.focus(); if (field.select) field.select(); }
    },
    // Keyboard map, RTL-aware; never fires inside a field. Pure: returns the action.
    keyAction(e, inField) {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return null;
      const k = e.key;
      const code = e.code || '';
      if (k === 'Escape') return inField ? 'blur' : null;
      if (inField) return null;
      if (code === 'KeyG' || k === 'g' || k === 'G') return 'jump';
      if (code === 'KeyS' || k === 's' || k === 'S') return 'spread';
      if (code === 'KeyE' || k === 'e' || k === 'E') return 'editor';
      if (k === '1') return 'fitHeight';
      if (k === '2') return 'fitWidth';
      if (k === '3') return 'fitActual';
      if (k === 'ArrowLeft' || k === 'PageDown') return 'next'; // RTL: the next page is on the left
      if (k === 'ArrowRight' || k === 'PageUp') return 'prev';
      if (k === 'Home') return 'first';
      if (k === 'End') return 'last';
      return null;
    },
    onKey(e) {
      const t = e.target;
      const inField = Boolean(t && (['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName) || t.isContentEditable));
      const action = this.keyAction(e, inField);
      if (!action) return;
      if (action === 'blur') { if (t && t.blur) t.blur(); return; }
      if (action === 'jump') { e.preventDefault(); this.focusJump(); return; }
      if (action === 'spread') { this.toggleSpread(); return; }
      if (action === 'editor') { if (this.editorHref && typeof window !== 'undefined' && window.location) window.location.assign(this.editorHref); return; }
      if (action === 'fitHeight') { this.setFit('height'); return; }
      if (action === 'fitWidth') { this.setFit('width'); return; }
      if (action === 'fitActual') { this.setFit('actual'); return; }
      if (action === 'next' || action === 'prev') { e.preventDefault(); this.turn(action === 'next' ? 1 : -1); return; }
      if (action === 'first' || action === 'last') {
        if (!this.pages.length) return;
        e.preventDefault();
        this.showIndex(action === 'first' ? 0 : this.pages.length - 1, { manual: true });
      }
    },
    // The viewer opens on the page in the address (#page-N), else the one shown last in this session, else
    // the requested chapter's first page, else the first page.
    restorePosition() {
      const hash = typeof window !== 'undefined' && window.location ? window.location.hash || '' : '';
      const m = /^#page-(\d+)$/.exec(hash);
      const saved = Number(readSession(PAGE_KEY + (this.bookId || '')));
      const n = m ? Number(m[1]) : cfg.requestedChapter ? 0 : saved;
      if (n > 0) {
        if (!this.showPage(n, { instant: true }) && m && this.pages.length) toast('لا صفحة بهذا الرقم');
      } else if (this.cursor >= 0) {
        this.land(this.cursor);
      }
    },
    // ---- a trackpad or wheel gesture turns one page (RTL: a swipe to the right and scrolling down go
    // forward); the gesture must pause before the next one counts, so inertia never flips two pages
    onStageWheel(e) {
      if (!e || e.ctrlKey || e.metaKey) return;
      if (this.fit !== 'height') return; // a scrolling stage scrolls
      const horizontal = Math.abs(e.deltaX) > Math.abs(e.deltaY);
      const delta = horizontal ? -e.deltaX : e.deltaY;
      if (e.cancelable && e.preventDefault) e.preventDefault();
      clearTimeout(wheelTimer);
      wheelTimer = setTimeout(() => { wheelAcc = 0; wheelLock = false; }, WHEEL_IDLE_MS);
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
      if (Math.abs(dx) >= SWIPE_PX && Math.abs(dx) > 1.5 * Math.abs(dy)) this.turn(dx > 0 ? 1 : -1); // RTL: finger to the right → next
    },
    swipeCancel() {
      swipeStart = null;
    },

    // ------------------------------------------------------------ the filmstrip (static thumbs, = books.js)
    bindDom() {
      const root = this.$el || (hasDOM ? document.querySelector('[data-layout]') : null);
      if (!root || typeof root.querySelector !== 'function') return;
      filmEl = root.querySelector('[data-film-track]');
      canvasEl = root.querySelector('[data-canvas]');
      // the page the server painted is drawn already: its load event fired before Alpine listened
      const painted = root.querySelector('[data-sheet="right"] img');
      if (painted && painted.complete && painted.naturalWidth && painted.getAttribute('src')) this.loaded[painted.getAttribute('src')] = true;
      if (filmEl) {
        // thumbs rendered by the server are adopted in order
        Array.from(filmEl.children || []).forEach((el) => {
          if (el && el.dataset && el.dataset.index !== undefined) thumbs[Number(el.dataset.index)] = { el, img: q(el, 'img'), num: q(el, '.lo-thumb-num') };
        });
        filmEl.addEventListener('click', (e) => {
          const thumb = e.target && e.target.closest ? e.target.closest('.lo-thumb') : null;
          if (thumb) this.showIndex(Number(thumb.dataset.index), { manual: true });
        });
      }
      this.onResize();
      this.syncFilm();
    },
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
    // One thumb per page of the sequence, reused by position: the image and number are patched, extras dropped.
    syncFilm() {
      if (!filmEl || !hasDOM) return;
      this.pages.forEach((p, i) => {
        let t = thumbs[i];
        if (!t) { t = this.makeThumb(); thumbs[i] = t; filmEl.appendChild(t.el); }
        t.el.dataset.index = String(i);
        t.el.dataset.number = String(p.n);
        t.el.setAttribute('title', `صفحة ${p.n}`);
        t.el.setAttribute('aria-label', `صفحة ${p.n}`);
        if (t.el.style && t.el.style.setProperty) t.el.style.setProperty('--thumb-ar', this.pageRatio.toFixed(4));
        if (t.img && t.img.getAttribute('src') !== p.url) t.img.setAttribute('src', p.url);
        setText(t.num, p.n);
        t.el.classList.toggle('is-fresh', p.scope === 'chapter');
      });
      while (thumbs.length > this.pages.length) { const t = thumbs.pop(); if (t && t.el && t.el.remove) t.el.remove(); }
      this.markThumbs();
    },
    markThumbs() {
      const shown = this.shown;
      thumbs.forEach((t, i) => {
        if (!t || !t.el || !t.el.classList) return;
        const on = i === shown.right || i === shown.left;
        t.el.classList.toggle('is-current', on);
        if (on) t.el.setAttribute('aria-current', 'page');
        else if (t.el.removeAttribute) t.el.removeAttribute('aria-current');
      });
    },
    // The current thumbnail stays in view: when it is not fully visible the strip (or the side panel it sits
    // in) scrolls it to the middle. Only the strip scrolls, never the page.
    centerFilm() {
      const film = filmEl || (this.$refs && this.$refs.film);
      if (!film || typeof film.querySelector !== 'function' || this.panel !== 'pages') return;
      const run = () => {
        const item = film.querySelector(`[data-index="${this.cursor}"]`);
        const own = film.scrollHeight > film.clientHeight + 1 || film.scrollWidth > film.clientWidth + 1;
        const scroller = own || typeof film.closest !== 'function' ? film : film.closest('.lo-side') || film;
        if (!item || !item.getBoundingClientRect || !scroller.getBoundingClientRect || !scroller.scrollBy) return;
        const f = scroller.getBoundingClientRect();
        const r = item.getBoundingClientRect();
        if (!f.height || !r.height) return;
        const outY = r.top < f.top + FILM_EDGE_PX || r.bottom > f.bottom - FILM_EDGE_PX;
        const outX = r.left < f.left + FILM_EDGE_PX || r.right > f.right - FILM_EDGE_PX;
        if (!outY && !outX) return;
        scroller.scrollBy({
          top: outY ? r.top + r.height / 2 - (f.top + f.height / 2) : 0,
          left: outX ? r.left + r.width / 2 - (f.left + f.width / 2) : 0,
          behavior: reduced() ? 'auto' : 'smooth',
        });
      };
      if (this.$nextTick) this.$nextTick(run); else run();
    },
    };
  });
  });
})();
