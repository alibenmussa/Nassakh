// Alpine components for the books screens: new-book form, dashboard, page detail viewer.
// This file runs before the deferred Alpine bundle, so components are registered on `alpine:init`.
// Digits shown to the user are always Western (no locale-aware number formatting here).

// Pure helpers of the books screens, shared with processing.js and the Node tests (window.NassakhBooks).
(function () {
  'use strict';

  // «صفحة واحدة», «صفحتان», «5 صفحات», «214 صفحة», «103 صفحات» (= assembly.render.ar_count)
  const arCount = (n, forms) => {
    n = Number(n) || 0;
    if (n === 1) return forms[0];
    if (n === 2) return forms[1];
    const units = n % 100;
    return `${n} ${units >= 3 && units <= 10 ? forms[2] : forms[3]}`;
  };
  const PAGE_FORMS = ['صفحة واحدة', 'صفحتان', 'صفحات', 'صفحة'];
  // The dual after a preposition or as an object: «على صفحتين», «يغيّر هذا صفحتين».
  const PAGE_FORMS_GEN = ['صفحة واحدة', 'صفحتين', 'صفحات', 'صفحة'];

  // The dictionary (`<< … >>`) around offset `at` of a PDF's text, or null.
  function enclosingDict(text, at) {
    let depth = 0;
    let start = -1;
    for (let i = at - 1; i > 0; i -= 1) {
      if (text[i] === '>' && text[i - 1] === '>') { depth += 1; i -= 1; } else if (text[i] === '<' && text[i - 1] === '<') {
        if (depth === 0) { start = i - 1; break; }
        depth -= 1;
        i -= 1;
      }
    }
    if (start < 0) return null;
    depth = 0;
    for (let i = at; i < text.length - 1; i += 1) {
      if (text[i] === '<' && text[i + 1] === '<') { depth += 1; i += 1; } else if (text[i] === '>' && text[i + 1] === '>') {
        if (depth === 0) return text.slice(start, i + 2);
        depth -= 1;
        i += 1;
      }
    }
    return null;
  }
  // D66: the page count a PDF declares, read from the Latin-1 text of its first and last bytes: the largest
  // `/Count n` of a `/Type /Pages` dictionary (never `/Page`). Null when none is readable (compressed object
  // streams); the server's check stays authoritative (books/forms.py).
  function pdfPageCount(text) {
    const re = /\/Type\s*\/Pages(?![A-Za-z0-9])/g;
    let best = null;
    let m;
    while ((m = re.exec(String(text || '')))) {
      const dict = enclosingDict(text, m.index);
      const c = dict ? /\/Count\s+(\d+)/.exec(dict) : null;
      const n = c ? parseInt(c[1], 10) : 0;
      if (n > 0 && (best === null || n > best)) best = n;
    }
    return best;
  }
  // The form's line under the skip fields (PHASE7 §3.13, §3.14): which PDF pages are extracted. `count` is the
  // file's page count when the browser could read it, else null (the rule in words). `error` marks the case
  // where nothing is left.
  function rangeLine({ count = null, skipFirst = 0, skipLast = 0, perSheet = 1 } = {}) {
    const first = Math.max(0, parseInt(skipFirst, 10) || 0);
    const last = Math.max(0, parseInt(skipLast, 10) || 0);
    const two = Number(perSheet) === 2;
    if (!(Number(count) > 0)) {
      if (!first && !last) return { text: 'تُستخرج كل صفحات الملف.', error: false };
      const rule = [first ? `بعد أول ${first}` : '', last ? `قبل آخر ${last}` : ''].filter(Boolean).join(' و');
      return { text: `يُعرف عدد صفحات الملف بعد رفعه؛ تُستخرج الصفحات ${rule}.`, error: false };
    }
    const file = `الملف ${arCount(count, PAGE_FORMS)}`;
    if (first + last >= count) return { text: `لا تبقى صفحات: ${file} والتجاوز ${first} + ${last}.`, error: true };
    const from = first + 1;
    const to = count - last;
    const sheets = to - from + 1;
    const pagesNote = `(${arCount(sheets * 2, PAGE_FORMS)} في الكتاب)`;
    if (!first && !last) return { text: two ? `${file} · تُستخرج كلها، وفي كل منها صفحتان ${pagesNote}.` : `${file} · تُستخرج كلها.`, error: false };
    const span = from === to ? `الصفحة ${from}` : `الصفحات ${from}–${to}`;
    if (two) return { text: `${file} · تُستخرج ${span}، وفي كل منها صفحتان ${pagesNote}.`, error: false };
    return { text: `${file} · تُستخرج ${span}${from === to ? '' : ` (${arCount(sheets, PAGE_FORMS)})`}.`, error: false };
  }
  // A guides block's full bands as the compact `b` of api:book_guides: [kind, y0, y1] (+ x0, x1 for the page
  // number), kinds h / b / f / p.
  const KIND_CODES = { running_header: 'h', body: 'b', footnote: 'f', page_number: 'p' };
  const r4 = (v) => Math.round(Number(v) * 10000) / 10000;
  function compactBands(bands) {
    return (bands || []).filter((b) => b && KIND_CODES[b.kind] && Array.isArray(b.bbox)).map((b) => {
      const [x0, y0, x1, y1] = b.bbox.map(r4);
      return b.kind === 'page_number' ? ['p', y0, y1, x0, x1] : [KIND_CODES[b.kind], y0, y1];
    });
  }

  // «تجميع المخطوطة» (D77, PHASE7 §5.5, owner question 3): the convert popover's button, shared by the dashboard and
  // the manuscript view (`convert` = {label, edited, unreviewed, options}). D35 stays: unreviewed pages are in by
  // default, and the button says how many («تجميع مع 5 صفحات غير مُراجَعة»); a re-assembly and the replacement of
  // an edited text keep their own words.
  const CONVERT = 'تجميع المخطوطة';
  function convertLabel(convert) {
    const c = convert || {};
    const n = Number(c.unreviewed) || 0;
    if (c.label !== CONVERT || !n || !(c.options && c.options.include_unreviewed)) return c.label || CONVERT;
    return `تجميع مع ${arCount(n, PAGE_FORMS_GEN)} ${n === 2 ? 'غير مُراجَعتين' : 'غير مُراجَعة'}`;
  }
  // Unchecked: «تُترك 5 صفحات لم تُراجَع بعد، وتُضاف حين تُراجَع.» ('' with nothing left out)
  function convertLeftOut(convert) {
    const c = convert || {};
    const n = Number(c.unreviewed) || 0;
    if (!n || (c.options && c.options.include_unreviewed)) return '';
    if (n === 2) return 'تُترك صفحتان لم تُراجَعا بعد، وتُضافان حين تُراجَعان.';
    return `تُترك ${arCount(n, PAGE_FORMS)} لم تُراجَع بعد، وتُضاف حين تُراجَع.`;
  }

  window.NassakhBooks = Object.assign(window.NassakhBooks || {}, { arCount, PAGE_FORMS, PAGE_FORMS_GEN, pdfPageCount, rangeLine, compactBands, KIND_CODES, CONVERT, convertLabel, convertLeftOut });
})();

document.addEventListener('alpine:init', () => {
  const POLL_INTERVAL = 2000;
  const POLL_MAX_INTERVAL = 15000;
  const FAILURES_BEFORE_NOTICE = 3;

  // ---------------------------------------------------------------- new book form
  // «استخراج الصفحات» (D66): the file picker names the chosen file, and the line under the skip fields says which
  // PDF pages will be extracted before anything is queued. The page count is read in the browser from the first
  // and last 2 MB of the file; the server check stays authoritative.
  const PROBE_BYTES = 2 * 1024 * 1024;
  Alpine.data('bookForm', (cfg = {}) => ({
    pagesPerSheet: Number(cfg.pagesPerSheet) || 1,
    splitRatio: Number(cfg.splitRatio) || 0.5,
    submitting: false,
    fileName: '',
    sourcePages: null, // the page count the chosen PDF declares, null when unknown
    skipFirst: Number(cfg.skipFirst) || 0,
    skipLast: Number(cfg.skipLast) || 0,
    probeId: 0,

    // The skip fields are plain Django widgets: their values are read on input (no x-model on them).
    readSkips(root) {
      const field = (name) => (root && root.querySelector ? root.querySelector(`[name="${name}"]`) : null);
      const a = field('skip_first');
      const b = field('skip_last');
      if (a) this.skipFirst = Math.max(0, parseInt(a.value, 10) || 0);
      if (b) this.skipLast = Math.max(0, parseInt(b.value, 10) || 0);
    },
    get range() {
      return window.NassakhBooks.rangeLine({ count: this.sourcePages, skipFirst: this.skipFirst, skipLast: this.skipLast, perSheet: this.pagesPerSheet });
    },
    async onFile(event) {
      const file = event && event.target && event.target.files ? event.target.files[0] : null;
      this.fileName = file ? file.name : '';
      this.sourcePages = null;
      if (!file) return;
      const id = (this.probeId += 1);
      const count = await this.probe(file);
      if (id === this.probeId) this.sourcePages = count;
    },
    // Latin-1 text of the file's first and last 2 MB → the declared page count (NassakhBooks.pdfPageCount).
    async probe(file) {
      try {
        const decoder = new TextDecoder('latin1');
        const read = async (blob) => decoder.decode(await blob.arrayBuffer());
        const head = await read(file.slice(0, PROBE_BYTES));
        const tail = file.size > PROBE_BYTES ? await read(file.slice(Math.max(PROBE_BYTES, file.size - PROBE_BYTES))) : '';
        const counts = [window.NassakhBooks.pdfPageCount(head), window.NassakhBooks.pdfPageCount(tail)].filter((n) => n > 0);
        return counts.length ? Math.max(...counts) : null;
      } catch (e) {
        return null; // unreadable here: the form states the rule in words
      }
    },

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
  const PAGE_KEY = 'nassakh.bookPage.'; // the page the viewer shows, per book (session)
  // D33 page viewer: a turn is 200 ms out + a two-frame entrance (= review screen); input thresholds
  const TURN_OUT_MS = 200;
  const WHEEL_TURN_X = 50; // px of horizontal trackpad travel for one page
  const WHEEL_TURN_Y = 90; // px of vertical wheel travel for one page
  const WHEEL_IDLE_MS = 260; // a wheel gesture ends after this pause (inertia never turns two pages)
  const SWIPE_PX = 50; // touch swipe distance for one page
  const FILM_REFRESH_MS = 4000; // new thumbnails during processing: at most one filmstrip request per 4 s
  const FILM_EDGE_PX = 6; // a thumbnail closer than this to the strip's edge counts as out of view
  const SHEET_BATCH = 40; // api:book_sheets serves at most 40 pages per call
  const NEAR_MARGIN = '1500px'; // a sheet this close to the viewport mounts and fetches its data
  const FAR_MARGIN = '4000px'; // and unmounts again once it is this far away (800-page books stay light)
  const TILE_MARGIN = '600px'; // grid tiles animate only this close to the viewport
  const SHEET_FLUSH_MS = 60;
  const SHEET_RETRY_MS = 2000; // a failed sheets request is retried after 2 s, 4 s, 8 s … up to 15 s
  const SHEET_RETRY_MAX_MS = 15000;
  const FOLLOW_MIN_MS = 2000;
  const SCROLL_SAVE_MS = 1200; // the grid saves its scroll this long after it stops
  const DONE_TOAST_MS = 8000;
  const PULSE_MS = 500;
  const LIVE_MS = 2000; // D76: a refresh the channel or the focus asks for, at most once every 2 s
  const STAGE_PERCENT = { uploaded: 12, preprocessed: 46, layout_done: 72, ocr_done: 100, reviewed: 100, assembled: 100 };
  const DONE_STATUSES = ['ocr_done', 'reviewed', 'assembled'];
  const REVIEWED_STATUSES = ['reviewed', 'assembled'];
  const PROCESSING_STATUSES = ['uploaded', 'preprocessed', 'layout_done'];
  const CHANGE_KEYS = ['status', 'text_state', 'n_unresolved', 'is_reviewed', 'n_flags', 'sequence_issue', 'error', 'is_excluded', 'printed_number', 'readers'];
  // D73: the half-disc of a page one model read (`readers`: 'two' | 'one' | 'tesseract' | ''), on the tile and the thumb
  const READER_TITLES = { one: 'قراءة واحدة', tesseract: 'نص Tesseract وحده' };
  // Fallbacks until the backend config carries statusLabels / statusDots / urls (§12.2).
  const STATUS_LABELS = { uploaded: 'مرفوعة', preprocessed: 'مُجهَّزة', layout_done: 'بانتظار التعرّف', ocr_done: 'تم التعرّف', reviewed: 'مُراجَعة', assembled: 'مُجمَّعة', error: 'خطأ', excluded: 'مستثناة' };
  const STATUS_DOTS = { uploaded: 'dot-neutral', preprocessed: 'dot-accent', layout_done: 'dot-accent', ocr_done: 'dot-success', reviewed: 'dot-success', assembled: 'dot-success', error: 'dot-danger', excluded: 'dot-neutral' };
  const FILTERS = {
    all: () => true,
    processing: (p) => !p.is_excluded && PROCESSING_STATUSES.includes(p.status),
    review: (p) => !p.is_excluded && p.status === 'ocr_done',
    attention: (p) => !p.is_excluded && (Boolean(p.error) || p.n_flags > 0 || Boolean(p.sequence_issue)),
    reviewed: (p) => !p.is_excluded && REVIEWED_STATUSES.includes(p.status),
  };
  const EASTERN_DIGITS = /[٠-٩۰-۹]/g;
  // «التخطيط» mode (D67, PHASE7 §3.12)
  const GUIDES_FILTER_KEY = 'nassakh.guidesFilter.';
  const GUIDES_BATCH = 400; // pages per api:book_guides range request (about 60 B each)
  const PREPARED_STATUSES = ['preprocessed', 'layout_done', 'ocr_done', 'reviewed', 'assembled'];
  const KIND_OF = { h: 'running_header', b: 'body', f: 'footnote', p: 'page_number' };
  const BAND_LABELS = { running_header: 'ترويسة', body: 'متن', footnote: 'حاشية', page_number: 'رقم الصفحة' };
  const BAND_ORDER = { running_header: 0, body: 1, footnote: 2, page_number: 3 };
  const LINE_KINDS = { header: 'running_header', footnote: 'footnote' };
  const MINUTE_FORMS = ['دقيقة واحدة', 'دقيقتين', 'دقائق', 'دقيقة']; // after «نحو»: «نحو دقيقتين»
  const pct1 = (y) => (Math.round(Number(y) * 1000) / 10).toFixed(1);
  // Letters by physical key (D69) through static/src/js/keys.js, with a fallback of its own rule.
  const keyLetter = (e) => {
    const K = window.NassakhKeys;
    if (K && typeof K.letter === 'function') return K.letter(e) || '';
    const m = /^Key([A-Z])$/.exec(e.code || '');
    if (m) return m[1].toLowerCase();
    return /^[a-zA-Z]$/.test(e.key || '') ? e.key.toLowerCase() : '';
  };
  const keyComposing = (e) => {
    const K = window.NassakhKeys;
    if (K && typeof K.composing === 'function') return Boolean(K.composing(e));
    return Boolean(e.isComposing || e.keyCode === 229);
  };

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
  // «صفحة واحدة», «صفحتان», «5 صفحات», «214 صفحة» (= NassakhManuscript.arCount / assembly.render.ar_count)
  const { arCount, PAGE_FORMS, PAGE_FORMS_GEN, compactBands } = window.NassakhBooks;
  const NOTE_FORMS = ['ملاحظة واحدة', 'ملاحظتان', 'ملاحظات', 'ملاحظة'];
  const csrfToken = () => {
    const meta = typeof document !== 'undefined' && document.querySelector ? document.querySelector('meta[name="csrf-token"]') : null;
    return (meta && meta.content) || '';
  };
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
    const thumbs = new Map(); // page id -> button.bk-thumb (the filmstrip, static like the shells)
    const handles = new Map(); // page id -> NassakhDecode.sheet handle
    const mounted = new Set(); // page ids whose sheet body is in the DOM
    const byNumber = new Map(); // page number -> page id
    const pending = new Set(); // page numbers waiting for a sheets request
    const inflight = new Set(); // page numbers with a sheets request on the wire
    const failed = new Set(); // page numbers whose last sheets request failed (retried with a backoff)
    let bookLineH = 0; // the book's typical printed line height in px (api:book_sheets), shared by every sheet (D30)
    let turnTimer = null; // D33 viewer: the running turn and the page it will land on
    let pendingTarget = 0;
    let pendingFocus = false;
    let wheelAcc = 0;
    let wheelLock = false;
    let wheelTimer = null;
    let swipeStart = null;
    let filmTimer = null;
    let flushTimer = null;
    let sheetRetryTimer = null;
    let sheetRetryMs = 0;
    let nearObserver = null;
    let farObserver = null;
    let tileObserver = null;
    let stackEl = null;
    let gridEl = null;
    let filmEl = null;
    let shellTpl = null;
    let tileTpl = null;
    let bodyTpl = null;
    let thumbTpl = null;
    let focusedId = null;
    let lastFollowAt = 0;
    let scrollSaveTimer = null;
    let resizeTimer = null;
    let doneTimer = null;
    let pulseTimer = null;
    let liveChannel = null; // D76: review's saves in another tab
    let liveFocus = null;
    let liveAt = 0;
    let liveTimer = null;
    const bookUrl = String(cfg.bookUrl || '');
    const urls = Object.assign(
      { page: `${bookUrl}pages/__n__/`, review: `${bookUrl}review/__n__/`, rerun: `${bookUrl}pages/__n__/rerun/`, exclude: `${bookUrl}pages/__n__/exclude/` },
      cfg.urls || {},
    );
    const statusLabels = Object.assign({}, STATUS_LABELS, cfg.statusLabels || {});
    const statusDots = Object.assign({}, STATUS_DOTS, cfg.statusDots || {});
    const fill = (template, n) => String(template || '').replace('__n__', String(n));
    const id = (p) => String(p.id);
    // «التخطيط» mode (D67): chosen by the server (`guidesMode`); outside it every path below is today's.
    const guidesMode = Boolean(cfg.guidesMode);
    const guidesUrls = cfg.guidesUrls || {};
    const guideEntries = new Map(); // page id -> compact api:book_guides entry {id, n, b, d, o, l, s, x}
    const guidesPending = new Set(); // page numbers waiting for a guides-state request
    let guidesTimer = null;
    let guidesTpl = null; // <template id="sheet-guides">: the body of a mounted sheet in the mode
    let guidesHooks = null; // the bookGuides component (processing.js): Esc layers, the view of a page on screen
    const entryOf = (p) => (p ? guideEntries.get(id(p)) : null) || null;
    // The mode's chips (§3.12): every page, the pages worth a look (a doubt or an error), the pages with
    // their own guides. Excluded pages never count and never carry a doubt.
    const GUIDE_FILTERS = {
      all: () => true,
      doubt: (p) => !p.is_excluded && (Boolean(p.error) || p.status === 'error' || Number((entryOf(p) || {}).d) > 0),
      override: (p) => !p.is_excluded && Boolean((entryOf(p) || {}).o),
    };
    const filters = guidesMode ? GUIDE_FILTERS : FILTERS;
    const filterKey = (guidesMode ? GUIDES_FILTER_KEY : FILTER_KEY) + (cfg.bookId || '');
    const hashPage = () => {
      const hash = typeof window !== 'undefined' && window.location ? window.location.hash || '' : '';
      const m = /^#sheet-(\d+)$/.exec(hash);
      return m ? Number(m[1]) : 0;
    };

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
    manuscript: cfg.manuscript || null, // assembly.services.manuscript_state (compact), refreshed by the poll
    manuscriptUrls: cfg.manuscriptUrls || {},
    editor: cfg.editor || null, // Phase 5: {edited, version, drift_pages} (D41), refreshed by the poll
    layout: cfg.layout || null, // Phase 5: {trim, trim_label, page_count, rendering, rendered_at}, refreshed by the poll
    editorUrls: cfg.editorUrls || {}, // editor.services.editor_urls: layout (the book page), chapters, …
    convert: { open: false, busy: false, error: '', label: 'تجميع المخطوطة', edited: false, options: { footnote_numbering: 'page', include_unreviewed: true, strip_tatweel: true, strip_running_heads: true }, unreviewed: 0 },
    stageMap: Object.fromEntries((cfg.stages || []).map((s) => [s.key, s.statuses])),
    // the mode opens on the grid (never remembered), except that a #sheet-N address opens the viewer (§3.12); outside
    // it the view is remembered per book (D76, §5.4), a book never opened keeping the last one chosen anywhere
    view: guidesMode ? (hashPage() ? 'sheets' : 'grid') : readLocal(VIEW_KEY + (cfg.bookId || ''), readLocal(VIEW_KEY, 'sheets')) === 'grid' ? 'grid' : 'sheets',
    sheetOpen: false, // the shortcut sheet (§5.8)
    filter: 'all',
    follow: readLocal(FOLLOW_KEY, '0') === '1',
    counts: guidesMode ? { all: 0, doubt: 0, override: 0 } : { all: 0, processing: 0, review: 0, attention: 0, reviewed: 0 },
    // ---- «التخطيط» mode (D67): the server's config; `layoutStage` = the book awaits «بدء المعالجة»
    guidesMode,
    layoutStage: Boolean(cfg.layoutStage),
    startAction: cfg.startAction || '',
    waiting: Boolean(cfg.layoutStage) && cfg.status === 'needs_guides',
    guidesBook: cfg.guides || null, // {source, header_cut, footnote_line, …}: the book's guides
    guidesStats: cfg.guidesStats || null, // {pages, rule, block, number, median_rule, text}
    keptRange: cfg.keptRange || null, // books.services.kept_range: {source_pages, first, last, sheets, pages, text}
    textLayer: Boolean(cfg.textLayer),
    guidesUrls,
    startedElsewhere: false, // a guides request answered 409 `started`, or the poll says «المعالجة» began
    rerun: cfg.rerun || {}, // books.services.rerun_estimate per offered stage: {pages, kept, minutes}
    dialog: { kind: '', stage: '', label: '' }, // kind '' | rerun | delete (rv-modal look, §3.13)
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
    doneToast: { visible: false, count: 0, url: '' },
    lastJump: null,
    filmstripUrl: cfg.filmstripUrl || '',
    current: 0, // D33: the page number the viewer shows
    turning: '', // '' | out-next | out-prev | in-next | in-prev

    init() {
      (cfg.pages || []).forEach((raw) => { const p = this.completePage(raw, null); pages.set(id(p), p); byNumber.set(p.number, id(p)); });
      const savedFilter = readLocal(filterKey, 'all');
      this.filter = filters[savedFilter] ? savedFilter : 'all';
      this.recount();
      this.bindDom();
      if (typeof Alpine.store === 'function' && Alpine.store('book')) Alpine.store('book').dash = this;
      if (this.active) this.schedule(POLL_INTERVAL);
      if (this.$watch) this.$watch('view', () => this.onViewChange());
      this.syncStage();
      this.bindLive();
      if (guidesMode) this.fetchGuides(); // the whole book once; then only the pages the poll reports as changed
    },
    destroy() {
      clearTimeout(this.timer);
      clearTimeout(guidesTimer);
      clearTimeout(flushTimer);
      clearTimeout(sheetRetryTimer);
      clearTimeout(doneTimer);
      clearTimeout(turnTimer);
      clearTimeout(filmTimer);
      clearTimeout(wheelTimer);
      clearTimeout(scrollSaveTimer);
      handles.forEach((h) => h.destroy());
      handles.clear();
      if (nearObserver) { nearObserver.disconnect(); farObserver.disconnect(); }
      if (tileObserver) tileObserver.disconnect();
      if (typeof Alpine.store === 'function' && Alpine.store('book')) Alpine.store('book').dash = null;
      if (liveChannel) { try { liveChannel.close(); } catch (e) { /* closed */ } liveChannel = null; }
      if (liveFocus && typeof window !== 'undefined' && window.removeEventListener) window.removeEventListener('focus', liveFocus);
    },

    // ------------------------------------------------------------ D76 (§5.1, §5.4): the stage bar, kept live
    // The status folds into the stage bar's current step («التخطيط» in the mode, else «المعالجة»): while pages are
    // prepared or read the step runs with the poll's count and bar; an error needs attention; otherwise the
    // server's step shows. A finished run asks the bar for the server's steps (`NassakhStages.changed`).
    syncStage() {
      let store = null;
      try { store = typeof Alpine.store === 'function' ? Alpine.store('stages') : null; } catch (e) { store = null; }
      if (store && typeof store.set === 'function') store.set(guidesMode ? 'pages' : 'ocr', this.stagePatch);
    },
    get stagePatch() {
      const total = this.total || this.nPages;
      if (this.status === 'error') return { state: 'attention', detail: this.errorHeadline || this.statusLabel || 'حدث خطأ أثناء المعالجة.' };
      if (!this.active || !total) return null;
      if (this.layoutStage) { const n = this.preparedCount; return { state: 'active', detail: `قيد التخطيط: ${n} من ${total}`, count: `${n}/${total}`, percent: this.percent }; }
      if (guidesMode) return null; // a started book in the mode: its «التخطيط» is done, the bar says so
      const n = this.done;
      return { state: 'active', detail: `قيد المعالجة: ${n} من ${total}`, count: `${n}/${total}`, percent: this.percent };
    },
    // Review saves in another tab (BroadcastChannel 'nassakh') and the window's focus refresh the progress once,
    // so the «الكتاب» block's drift line stays live without a poll (a running book polls anyway).
    bindLive() {
      if (typeof window === 'undefined') return;
      try {
        if (typeof BroadcastChannel === 'function') {
          liveChannel = new BroadcastChannel('nassakh');
          if (typeof liveChannel.unref === 'function') liveChannel.unref(); // Node (the tests)
          liveChannel.onmessage = (e) => { const m = e && e.data; if (m && Number(m.book) === Number(cfg.bookId)) this.refreshLive(); };
        }
      } catch (e) { liveChannel = null; }
      liveFocus = () => this.refreshLive();
      if (window.addEventListener) window.addEventListener('focus', liveFocus);
    },
    refreshLive() {
      if (this.active || this.stopped || !this.progressUrl) return false; // the poll runs already (or stopped for good)
      const wait = liveAt + LIVE_MS - Date.now();
      if (wait > 0) {
        if (!liveTimer) liveTimer = setTimeout(() => { liveTimer = null; this.refreshLive(); }, wait);
        return false;
      }
      liveAt = Date.now();
      this.pollNow();
      return true;
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
      // D76 (§5.4): where the tile and the sheet title lead: review once the text is final, else the page detail
      p.primary_url = raw.primary_url || (p.text_state === 'final' && !p.is_excluded ? p.review_url : p.url);
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
      // the full tile always names its readers; the compact poll only 'one' / 'tesseract', so a page it leaves
      // them out of was read by both models (or not read yet)
      p.readers = READER_TITLES[raw.readers] ? raw.readers : '';
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
      if (d.manuscript) this.manuscript = d.manuscript;
      if (d.editor) this.editor = d.editor;
      if (d.layout) this.layout = d.layout;
      this.active = Boolean(d.active);
      if (guidesMode) {
        if ('waiting' in d) this.waiting = Boolean(d.waiting);
        // «بدء المعالجة» pressed in another window: the pause is over, this page is out of date
        if (this.layoutStage && d.layout_stage === false) this.startedElsewhere = true;
      }
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
      if (!this.current && added.length) { const first = this.visibleNumbers()[0]; if (first) this.showPage(first, { instant: true }); }
      // a page that left `uploaded` has a thumbnail now; the compact poll carries none
      if (changed.some(({ before, after }) => before.status === 'uploaded' && after.status !== 'uploaded' && !after.thumb_url)) this.refreshFilmSoon();
      if (changed.length) {
        const last = changed[changed.length - 1].after;
        this.liveMessage = `الصفحة ${last.number}: ${last.status_label}`;
        // refetched now: the pages on screen or near it, and the viewer's next turns (its neighbours and the
        // follow target), so a turn lands on laid-out text instead of a skeleton (§11)
        const ahead = new Set([this.neighbour(1), this.neighbour(-1), this.active && this.follow ? this.followTarget(changed) : null]);
        changed.forEach(({ after }) => { if (mounted.has(id(after)) || this.tileNear(id(after)) || ahead.has(after.number)) this.queueSheet(after.number); });
      }
      // the mode's bands and doubts follow the pages that changed (a page prepared, failed or re-included)
      if (guidesMode) this.queueGuides([...added, ...changed.map((c) => c.after)].filter((p) => p.status !== 'uploaded').map((p) => p.number));
      this.syncStage();
      if (wasActive && !this.active) this.onProcessingEnd();
      else if (this.active && this.follow && changed.length) this.followChanged(changed);
      this.ensureSheet(this.current); // safety net: the page on screen never stays without its data
    },
    // The first poll with active false after an active one: effects stop, chrome follows, one toast, no reload.
    onProcessingEnd() {
      this.stopEffects();
      if (window.NassakhStages) window.NassakhStages.changed(cfg.bookId); // the bar: the step is done now
      this.refreshFilmSoon(0);
      if (guidesMode) {
        this.fetchGuides(); // the stats line and every page's bands, once the pages are prepared
        // only a finished preparation is announced: not a failure, not `?view=guides` on a started book
        if (!this.layoutStage || this.status !== 'needs_guides') return;
      }
      // «اكتمل التخطيط · 7 صفحات» with no action (the primary is enabled now), else today's toast
      this.doneToast = { visible: true, count: guidesMode ? this.preparedCount : this.nPages, url: guidesMode ? '' : this.nextReviewUrl };
      clearTimeout(doneTimer);
      doneTimer = setTimeout(() => { this.doneToast.visible = false; }, DONE_TOAST_MS);
    },
    stopEffects() {
      handles.forEach((h) => h.update({ active: false }));
      thumbs.forEach((el) => setHidden(q(el, '.bk-thumb-mark.is-live'), true)); // no page is live any more
    },
    addPage(p) {
      this.patchPage(p);
      this.addThumb(p, this.nextShell(p.number, thumbs));
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
      this.patchThumb(thumbs.get(pid), p);
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
      if (guidesMode) {
        this.counts = { all: all.length, doubt: all.filter(GUIDE_FILTERS.doubt).length, override: all.filter(GUIDE_FILTERS.override).length };
        this.nPages = all.length;
        this.filteredOut = this.nPages > 0 && this.filter !== 'all' && this.counts[this.filter] === 0;
        return;
      }
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
      if (this.active && this.layoutStage) return `قيد التخطيط · ${this.preparedCount} من ${this.total} صفحة`;
      if (this.active) return `قيد المعالجة · ${this.done} من ${this.total} صفحة`;
      return this.statusLabel;
    },
    get barClass() {
      if (this.layoutStage) return { 'is-danger': this.status === 'error' }; // the prepared share, accent (§3.12)
      return { 'is-success': this.percent >= 100 && this.status !== 'error', 'is-danger': this.status === 'error', 'is-warning': this.status === 'needs_guides' };
    },
    // ------------------------------------------------------------ «التخطيط» mode: the chrome (§3.12, §3.14)
    // pages prepared so far (non-excluded; every status past `uploaded` except an error)
    get preparedCount() {
      return PREPARED_STATUSES.reduce((n, s) => n + (this.byStatus[s] || 0), 0);
    },
    // the bar shows only while the pages are prepared; on a started book it is today's
    get showBar() {
      return this.nPages > 0 && (!this.layoutStage || this.status === 'processing');
    },
    // the side panel: «جُهّزت 7 من 7 صفحات»
    get preparedLine() {
      const total = this.total || this.nPages;
      const units = total % 100;
      return `جُهّزت ${this.preparedCount} من ${total} ${total >= 3 && units >= 3 && units <= 10 ? 'صفحات' : 'صفحة'}`;
    },
    // «صفحة واحدة تستحق نظرة», «صفحتان تستحقان نظرة», «3 صفحات تستحق نظرة» ('' at 0)
    get doubtLine() {
      const n = this.counts.doubt || 0;
      if (!n) return '';
      return n === 2 ? 'صفحتان تستحقان نظرة' : `${arCount(n, PAGE_FORMS)} تستحق نظرة`;
    },
    get doubtShow() {
      return (this.counts.doubt || 0) === 2 ? 'عرضهما' : 'عرضها';
    },
    // the chip «تستحق نظرة» on, the first such page on screen in the viewer
    showDoubts() {
      this.setFilter('doubt');
      if (this.view === 'sheets') { const first = this.visibleNumbers()[0]; if (first) this.showPage(first, { instant: true, manual: true }); }
    },
    // the end toasts: «اكتمل التخطيط · 7 صفحات» (the mode), «اكتملت المعالجة · 8 صفحات» (D77: the count helper)
    get doneText() {
      return `${guidesMode ? 'اكتمل التخطيط' : 'اكتملت المعالجة'} · ${arCount(this.doneToast.count, PAGE_FORMS)}`;
    },
    get emptyTitle() {
      return this.status === 'processing' ? 'تُستخرج الصفحات الآن' : 'لم تُستخرج الصفحات بعد';
    },
    get emptyText() {
      if (this.status === 'processing') return 'تظهر الصفحات هنا واحدةً واحدة، ثم تُجهَّز وتُرسم مناطقها.';
      const k = this.keptRange || {};
      const action = this.status === 'error' ? 'إعادة استخراج الصفحات' : 'استخراج الصفحات';
      const span = k.first && k.last ? (k.first === k.last ? `الصفحة ${k.first}` : `الصفحات ${k.first}–${k.last}`) : 'صفحات الملف';
      return `اضغط «${action}»؛ تُستخرج ${span} من الملف وتُجهَّز وتُرسم مناطقها.`;
    },
    // ---- dialogs (§3.13, rv-modal look): re-run the book from a stage, delete the book
    openRerun(stage, label) {
      this.dialog = { kind: 'rerun', stage: String(stage || ''), label: String(label || stage || '') };
      this.focusDialog();
    },
    openDelete() {
      this.dialog = { kind: 'delete', stage: '', label: '' };
      this.focusDialog();
    },
    closeDialog() {
      this.dialog = { kind: '', stage: '', label: '' };
    },
    // ---- the shortcut sheet (§5.8): «?», or «⋯» «اختصارات لوحة المفاتيح»; the focus returns where it was
    openSheet() {
      this.sheetReturn = typeof document !== 'undefined' ? document.activeElement : null;
      this.sheetOpen = true;
      if (this.$nextTick) this.$nextTick(() => { const el = this.$refs && this.$refs.sheetClose; if (el && el.focus) el.focus(); });
    },
    closeSheet() {
      if (!this.sheetOpen) return;
      this.sheetOpen = false;
      const el = this.sheetReturn;
      this.sheetReturn = null;
      if (el && el.focus && this.$nextTick) this.$nextTick(() => el.focus());
    },
    // Tab stays inside the open dialog (= review.js trapTab).
    trapTab(ev, root) {
      if (!root || !root.querySelectorAll) return;
      const items = Array.from(root.querySelectorAll('button, [href], input, textarea, select, [tabindex]:not([tabindex="-1"])'))
        .filter((el) => !el.disabled && el.type !== 'hidden' && el.offsetParent !== null);
      if (!items.length) { ev.preventDefault(); return; }
      const first = items[0];
      const last = items[items.length - 1];
      if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); } else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
    },
    // focus goes to the safe button («إلغاء») once the dialog is shown
    focusDialog() {
      const run = () => { const safe = this.$refs && this.$refs[this.dialog.kind === 'delete' ? 'deleteSafe' : 'rerunSafe']; if (safe && safe.focus) safe.focus(); };
      if (this.$nextTick) this.$nextTick(run);
    },
    get rerunTitle() {
      return `إعادة تشغيل الكتاب من «${this.dialog.label}»؟`;
    },
    // «تُعاد 81 صفحة من هذه المرحلة، وتبقى 12 صفحة معتمدة كما هي.» (the second part only when pages are kept)
    get rerunText() {
      const est = this.rerun[this.dialog.stage] || {};
      const n = Number(est.pages);
      const kept = Number(est.kept) || 0;
      const head = Number.isFinite(n) && est.pages !== undefined && est.pages !== null ? `تُعاد ${arCount(n, PAGE_FORMS)} من هذه المرحلة` : 'تُعاد صفحات الكتاب غير المعتمدة من هذه المرحلة';
      if (!kept) return `${head}.`;
      const keptText = kept === 1 ? 'صفحة واحدة معتمدة كما هي' : kept === 2 ? 'صفحتان معتمدتان كما هما' : `${arCount(kept, PAGE_FORMS)} معتمدة كما هي`;
      return `${head}، وتبقى ${keptText}.`;
    },
    // the cost before a costly action (not a progress estimate, D22): the models' time, or what preprocessing keeps
    get rerunNote() {
      const est = this.rerun[this.dialog.stage] || {};
      if (this.dialog.stage === 'preprocess') return 'يُحتفظ بما ضُبط يدويًا لكل صفحة من تدوير وقصّ.';
      if (Number(est.minutes) > 0) return `يُعاد التعرّف عليها بالنماذج: نحو ${arCount(Math.ceil(Number(est.minutes)), MINUTE_FORMS)} على هذا الجهاز.`;
      return '';
    },
    // «فيه 12 صفحة مُراجَعة ومخطوطة محرَّرة.» ('' for a book with no work in it)
    get deleteWork() {
      const reviewed = Number((this.review || {}).reviewed) || 0;
      const m = this.manuscript || {};
      const parts = [];
      if (reviewed) parts.push(reviewed === 1 ? 'صفحة واحدة مُراجَعة' : reviewed === 2 ? 'صفحتان مُراجَعتان' : `${arCount(reviewed, PAGE_FORMS)} مُراجَعة`);
      if (this.edited) parts.push('مخطوطة محرَّرة');
      else if (m.exists) parts.push('مخطوطة');
      return parts.length ? `فيه ${parts.join(' و')}.` : '';
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
    // Exactly one primary button per state (§2.1.5, PHASE4 §4.1, PHASE5 §3), chosen from the poll without a
    // reload: once every page is reviewed, convert (or re-assemble a stale / failed manuscript) for editors;
    // once the manuscript is fresh, editors open the book page (D47: its pages, edited in place; after the
    // first edit the review drift is resolved per chapter there, D41), everyone else the manuscript; else copy
    // the book's text.
    get primary() {
      if (guidesMode) return this.guidesPrimary;
      if (this.canEdit && (this.status === 'uploaded' || this.status === 'error')) return 'start';
      if (this.nextReviewUrl) return 'review';
      if (this.allReviewed) {
        const m = this.manuscript || {};
        if (m.active) return this.manuscriptUrl ? 'manuscript' : '';
        if (this.canEdit && m.exists && !this.manuscriptFailed && (!m.stale || this.edited) && this.layoutUrl) return 'book';
        if (m.exists && !m.stale && !this.manuscriptFailed) return this.manuscriptUrl ? 'manuscript' : 'copy';
        if (this.canEdit) return m.exists ? 'reassemble' : 'convert';
        if (m.exists && this.manuscriptUrl) return 'manuscript';
        if (this.bookTextUrl) return 'copy';
      }
      return '';
    },
    // The mode's primary (§3.12 `startAction`), from the live status: «استخراج الصفحات» (uploaded), «إعادة
    // استخراج الصفحات» (error), «بدء المعالجة» disabled while the pages are prepared and enabled at «تم التخطيط»;
    // on a started book (`?view=guides`) «العودة إلى الصفحات».
    get guidesPrimary() {
      if (!this.layoutStage) return 'back';
      if (!this.canEdit) return '';
      if (this.status === 'uploaded') return 'extract';
      if (this.status === 'error') return 'reextract';
      if (this.status === 'processing') return 'startOcrDisabled';
      if (this.status === 'needs_guides') return 'startOcr';
      return this.startAction || '';
    },
    // ------------------------------------------------------------ the manuscript (Phase 4, §4.1)
    get hasManuscript() {
      const m = this.manuscript || {};
      return Boolean(m.exists || m.active);
    },
    get manuscriptFailed() {
      const m = this.manuscript || {};
      return Boolean(m.run && m.run.status === 'error') && !m.active;
    },
    get manuscriptUrl() {
      return this.manuscriptUrls.page || '';
    },
    get manuscriptActive() {
      return Boolean((this.manuscript || {}).active);
    },
    // the side panel's state line: «مُجمَّعة · 214 صفحة · 3 ملاحظات», «تغيّر نص 4 صفحات بعد التجميع», «قيد التجميع»;
    // after the first editor save the manuscript is the source of truth: «مُحرَّرة …» and the drift line (D41)
    get manuscriptLine() {
      const m = this.manuscript || {};
      if (m.active) return 'قيد التجميع';
      if (this.manuscriptFailed) return 'فشل التجميع';
      if (m.exists && m.stale && !this.edited) return `تغيّر نص ${arCount((m.stale_pages || []).length, PAGE_FORMS)} بعد التجميع`;
      if (m.exists) {
        const pages = (m.stats && m.stats.pages_included) || 0;
        const notes = m.warnings_count || 0;
        return `${this.edited ? 'مُحرَّرة' : 'مُجمَّعة'} · ${arCount(pages, PAGE_FORMS)} · ${notes ? arCount(notes, NOTE_FORMS) : 'بلا ملاحظات'}`;
      }
      return 'لم تُجمَّع بعد';
    },
    get manuscriptDot() {
      const m = this.manuscript || {};
      if (m.active) return 'dot-accent';
      if (this.manuscriptFailed) return 'dot-danger';
      if (m.exists && m.stale && !this.edited) return 'dot-warning';
      if (m.exists) return 'dot-success';
      return 'dot-neutral';
    },
    // ------------------------------------------------------------ the book page and the book's form (Phase 5, D47)
    get edited() {
      return Boolean(this.editor && this.editor.edited);
    },
    // the book page (`/books/<id>/layout/`): the one place to preview and to edit the book
    get layoutUrl() {
      return this.editorUrls.layout || '';
    },
    // D41: «تغيّر نص 4 صفحات في المراجعة بعد التحرير» once review corrections stop flowing into the manuscript
    get driftLine() {
      const pages = this.edited && this.editor.drift_pages ? this.editor.drift_pages.length : 0;
      return pages ? `تغيّر نص ${arCount(pages, PAGE_FORMS)} في المراجعة بعد التحرير` : '';
    },
    // D78 (§5.6): «عرض التغييرات» takes the changed paragraphs on the book page, in «تغييرات المراجعة»
    get changesUrl() {
      return this.layoutUrl ? `${this.layoutUrl}${this.layoutUrl.includes('?') ? '&' : '?'}tab=changes` : '';
    },
    // the «الكتاب» block: «17×24 سم · 412 صفحة», «17×24 سم · يُحسب…», «17×24 سم · لم تُرتَّب صفحاته بعد»
    get bookLine() {
      const l = this.layout || {};
      const trim = l.trim_label || '';
      const count = Number(l.page_count) || 0;
      const state = l.rendering && !count ? 'يُحسب…' : count ? arCount(count, PAGE_FORMS) : 'لم تُرتَّب صفحاته بعد';
      return trim ? `${trim} · ${state}` : state;
    },
    get bookDot() {
      const l = this.layout || {};
      if (l.rendering) return 'dot-accent';
      return Number(l.page_count) > 0 ? 'dot-success' : 'dot-neutral';
    },
    // the convert popover (assembly/_convert_popover.html): the options remembered per book (D38)
    // Over a text edited on the book page (D49) the popover says what a run replaces; its button confirms it.
    openConvert() {
      const m = this.manuscript || {};
      this.convert.options = Object.assign({ footnote_numbering: 'page', include_unreviewed: true, strip_tatweel: true, strip_running_heads: true }, m.options || {});
      this.convert.unreviewed = Number(m.unreviewed_pages) || 0;
      this.convert.edited = Boolean(m.exists && (m.edited || this.edited));
      this.convert.label = this.convert.edited ? 'استبدال النص المحرَّر' : m.exists ? 'إعادة التجميع' : 'تجميع المخطوطة';
      this.convert.error = '';
      this.convert.open = true;
    },
    closeConvert() {
      this.convert.open = false;
    },
    async submitConvert() {
      const ok = await this.startAssembly(Object.assign({}, this.convert.options, this.convert.edited ? { replace_edited: true } : {}));
      if (ok) this.convert.open = false;
      return ok;
    },
    reassemble() {
      const m = this.manuscript || {};
      if (m.edited || this.edited) { this.openConvert(); return Promise.resolve(false); }
      return this.startAssembly(Object.assign({ footnote_numbering: 'page', include_unreviewed: true, strip_tatweel: true, strip_running_heads: true }, m.options || {}));
    },
    // POST assemble, then the manuscript view shows the assembly as it runs (§4.1).
    async startAssembly(options) {
      const url = this.manuscriptUrls.assemble;
      if (!url || this.convert.busy) return false;
      this.convert.busy = true;
      this.convert.error = '';
      let data = null;
      let ok = false;
      let message = '';
      try {
        const res = await fetch(url, {
          method: 'POST',
          credentials: 'same-origin',
          cache: 'no-store',
          headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
          body: JSON.stringify(options || {}),
        });
        try { data = await res.json(); } catch (e) { data = null; }
        ok = res.ok;
        if (!ok) message = (data && (data.detail || data.message)) || (res.status === 403 ? 'هذا الإجراء يتطلب صلاحية محرّر.' : 'تعذّر بدء التجميع. حاول مرة أخرى.');
      } catch (e) {
        message = 'انقطع الاتصال بالخادم. تحقّق من الشبكة ثم أعد المحاولة.';
      }
      this.convert.busy = false;
      if (!ok && data && data.edited) { // edited meanwhile (another window): the popover asks first
        this.manuscript = Object.assign({}, this.manuscript || {}, { edited: true });
        this.openConvert();
        return false;
      }
      if (!ok) { this.convert.error = message; toast(message); return false; }
      this.manuscript = Object.assign({}, this.manuscript || {}, { active: true, run: { id: data && data.run_id, status: data && data.status, stage: data && data.stage, error: '' } });
      const target = (data && data.manuscript_url) || this.manuscriptUrl;
      if (target && typeof window !== 'undefined' && window.location && window.location.assign) window.location.assign(target);
      return true;
    },
    stagePercent(p) {
      if (p.error) return 100;
      return STAGE_PERCENT[p.status] || 0;
    },

    // ------------------------------------------------------------ views, filters, follow
    setView(view) {
      this.view = view === 'grid' ? 'grid' : 'sheets';
      if (!guidesMode) { writeLocal(VIEW_KEY + (cfg.bookId || ''), this.view); writeLocal(VIEW_KEY, this.view); } // the mode always opens on the grid
    },
    toggleView() {
      this.setView(this.view === 'grid' ? 'sheets' : 'grid');
    },
    onViewChange() {
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => this.updateSheetHeight());
      if (this.view !== 'sheets') return;
      const nums = this.visibleNumbers();
      if (!this.current && nums.length) this.showPage(nums[0], { instant: true });
      else this.centerFilm();
    },
    setFilter(name) {
      this.filter = filters[name] ? name : 'all';
      writeLocal(filterKey, this.filter);
      this.applyFilter();
      this.filteredOut = this.nPages > 0 && this.filter !== 'all' && this.counts[this.filter] === 0;
      // the viewer moves to the first page the filter keeps when the shown one is filtered out
      const shown = this.current ? pages.get(byNumber.get(this.current)) : null;
      if (shown && !this.matches(shown)) { const first = this.visibleNumbers()[0]; if (first) this.showPage(first, { instant: true }); }
    },
    matches(p) {
      return filters[this.filter](p);
    },
    isShown(pid) {
      return Boolean(this.current) && byNumber.get(this.current) === String(pid);
    },
    applyFilter() {
      shells.forEach((el, pid) => setHidden(el, !this.matches(pages.get(pid)) && !this.isShown(pid)));
      tiles.forEach((el, pid) => setHidden(el, !this.matches(pages.get(pid))));
      thumbs.forEach((el, pid) => setHidden(el, !this.matches(pages.get(pid))));
    },
    // the filmstrip's header: how many pages the filter keeps
    get filmCount() {
      return this.counts[this.filter] || 0;
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
      if (this.view !== 'sheets') return; // in the viewer, following turns to the page that advanced
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
      if (this.view === 'sheets') { this.showPage(n, { focus, manual: Boolean(focus) }); return; }
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
    // Letters by physical key (D69, NassakhKeys): G, V (the view, was 1 / 2), and outside the mode N and C; «?» the
    // shortcut sheet (§5.8).
    keyAction(e, inField) {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || keyComposing(e)) return null;
      const k = e.key;
      if (k === 'Escape') return inField ? 'blur' : 'clearFilter';
      if (inField) return null;
      const K = window.NassakhKeys;
      if (K && typeof K.is === 'function' ? K.is(e, '?') : k === '?' || k === '؟') return 'sheet';
      const letter = keyLetter(e);
      if (letter === 'g') return 'jump';
      if (letter === 'v') return 'toggleView';
      if (letter === 'n' && !guidesMode) return 'nextReview';
      if (letter === 'c' && !guidesMode) return 'copy';
      if (k === 'ArrowLeft' || k === 'PageDown') return 'nextSheet'; // RTL: the next page is on the left
      if (k === 'ArrowRight' || k === 'PageUp') return 'prevSheet';
      if (k === 'Home') return 'firstSheet';
      if (k === 'End') return 'lastSheet';
      if (k === 'ArrowDown') return 'hotDown';
      if (k === 'ArrowUp') return 'hotUp';
      return null;
    },
    onKey(e) {
      if (this.dialog.kind) { // an open dialog takes the keys: Esc cancels it
        if (e.key === 'Escape') { e.preventDefault(); this.closeDialog(); }
        return;
      }
      if (this.sheetOpen) { if (e.key === 'Escape') { e.preventDefault(); this.closeSheet(); } return; }
      const t = e.target;
      const inField = Boolean(t && (['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName) || t.isContentEditable));
      const action = this.keyAction(e, inField);
      if (!action) return;
      if (action === 'blur') { if (t && t.blur) t.blur(); return; }
      // Esc in the mode: an unsaved move first, then the draft, then the filter (§3.12)
      if (action === 'clearFilter') {
        if (guidesHooks && guidesHooks.escape && guidesHooks.escape()) { e.preventDefault(); return; }
        if (this.filter !== 'all') this.setFilter('all');
        return;
      }
      if (action === 'jump') { e.preventDefault(); this.focusJump(); return; }
      if (action === 'sheet') { e.preventDefault(); this.openSheet(); return; }
      if (action === 'nextReview') { if (this.nextReviewUrl) window.location.assign(this.nextReviewUrl); return; }
      if (action === 'toggleView') { this.toggleView(); return; }
      if (action === 'nextSheet' || action === 'prevSheet') { e.preventDefault(); this.stepSheet(action === 'nextSheet' ? 1 : -1); return; }
      if (action === 'firstSheet' || action === 'lastSheet') {
        if (this.view !== 'sheets') return;
        const nums = this.visibleNumbers();
        const n = action === 'firstSheet' ? nums[0] : nums[nums.length - 1];
        if (n) { e.preventDefault(); this.showPage(n, { manual: true }); }
        return;
      }
      // the viewer shows one page: C and ↑/↓ act on it, whether or not a sheet holds the focus
      const target = this.view === 'sheets' && this.current ? byNumber.get(this.current) : focusedId;
      if (action === 'hotDown' || action === 'hotUp') { if (target != null && this.moveHot(action === 'hotDown' ? 1 : -1, target)) e.preventDefault(); return; }
      if (action === 'copy' && target != null) this.copySheet(target);
    },
    visibleNumbers() {
      const all = [...byNumber.keys()].sort((a, b) => a - b);
      if (this.view === 'grid') return all.filter((n) => { const el = tiles.get(byNumber.get(n)); return el && !el.hidden; });
      return all.filter((n) => this.matches(pages.get(byNumber.get(n)) || {})); // the viewer's sequence follows the filter
    },
    stepSheet(dir) {
      if (this.view === 'sheets') { this.turn(dir); return; }
      const numbers = this.visibleNumbers();
      if (!numbers.length) return;
      const current = focusedId !== null ? (pages.get(focusedId) || {}).number : this.currentNumber();
      let idx = numbers.indexOf(current);
      if (idx === -1) idx = dir > 0 ? -1 : numbers.length;
      const next = numbers[Math.max(0, Math.min(numbers.length - 1, idx + dir))];
      if (next !== undefined) this.goTo(next, true);
    },
    moveHot(dir, pid = focusedId) {
      const h = handles.get(pid);
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
      if (this.view !== 'grid') return; // the viewer does not scroll (D33)
      clearTimeout(scrollSaveTimer);
      scrollSaveTimer = setTimeout(() => writeSession(SCROLL_KEY + (cfg.bookId || ''), Math.round(window.scrollY || 0)), SCROLL_SAVE_MS);
    },
    // The viewer opens on the page in the address (#sheet-N), else the one shown last in this session, else
    // the first page the filter keeps; the grid restores its scroll.
    restorePosition() {
      const hash = typeof window !== 'undefined' && window.location ? window.location.hash || '' : '';
      const m = /^#sheet-(\d+)$/.exec(hash);
      const saved = Number(readSession(PAGE_KEY + (cfg.bookId || '')));
      const nums = this.visibleNumbers();
      let n = m ? Number(m[1]) : saved;
      if (!byNumber.has(n)) { if (m) toast('لا صفحة بهذا الرقم'); n = nums[0]; }
      if (n) this.showPage(n, { instant: true });
      if (this.view === 'grid') {
        const y = Number(readSession(SCROLL_KEY + (cfg.bookId || '')));
        if (y > 0 && typeof requestAnimationFrame === 'function') requestAnimationFrame(() => window.scrollTo(0, y));
      }
    },

    // ------------------------------------------------------------ the page viewer (D33)
    get turnClass() {
      return this.turning ? `is-${this.turning}` : '';
    },
    // Show page `n`. With a page already on screen it turns: the old one slides out in the reading direction,
    // the new one mounts and slides in (review-screen motion); pressing again mid-turn only moves the target.
    showPage(n, opts = {}) {
      const pid = byNumber.get(n);
      const el = pid ? shells.get(pid) : null;
      if (!el) return false;
      if (opts.manual) this.userScrolled(); // a page the reader chose stops the follow mode
      if (this.current === n && !this.turning && el.classList && el.classList.contains('is-current')) {
        if (opts.focus) this.focusTitle(el);
        return true;
      }
      const base = this.turning ? pendingTarget : this.current;
      const dir = opts.dir || (base && n < base ? -1 : 1);
      pendingTarget = n;
      pendingFocus = pendingFocus || Boolean(opts.focus);
      if (!this.current || opts.instant || reduced() || this.view !== 'sheets') {
        clearTimeout(turnTimer);
        this.turning = '';
        this.swapTo(n);
        if (pendingFocus) { this.focusTitle(el); pendingFocus = false; }
        return true;
      }
      if (this.turning) return true; // the running turn lands on the latest target
      this.turning = dir > 0 ? 'out-next' : 'out-prev';
      clearTimeout(turnTimer);
      turnTimer = setTimeout(() => this.landTurn(dir), TURN_OUT_MS);
      return true;
    },
    landTurn(dir) {
      const n = pendingTarget;
      this.swapTo(n);
      this.turning = dir > 0 ? 'in-next' : 'in-prev';
      const frame = typeof requestAnimationFrame === 'function' ? requestAnimationFrame : (fn) => setTimeout(fn, 16);
      frame(() => frame(() => {
        this.turning = '';
        if (pendingFocus) { this.focusTitle(shells.get(byNumber.get(n))); pendingFocus = false; }
        if (pendingTarget && pendingTarget !== this.current) this.showPage(pendingTarget); // pressed again during the entrance
      }));
    },
    swapTo(n) {
      const prev = this.current ? shells.get(byNumber.get(this.current)) : null;
      const pid = byNumber.get(n);
      const el = pid ? shells.get(pid) : null;
      if (!el) return;
      if (prev && prev !== el && prev.classList) {
        prev.classList.remove('is-current');
        const before = pages.get(byNumber.get(this.current));
        if (before && !this.matches(before)) setHidden(prev, true);
      }
      const p = pages.get(pid) || {};
      if (el.style && el.style.setProperty) el.style.setProperty('--ar-n', String(this.aspectNumber(p)));
      setHidden(el, false);
      if (el.classList) el.classList.add('is-current');
      if (this.current) this.markThumb(thumbs.get(byNumber.get(this.current)), false);
      this.markThumb(thumbs.get(pid), true);
      this.current = n;
      focusedId = pid; // C copies and ↑/↓ move through the lines of the page on screen
      this.mountSheet(el);
      this.ensureSheet(n); // a mounted page whose data never arrived (or went stale) asks again
      this.prefetchAround(n);
      writeSession(PAGE_KEY + (cfg.bookId || ''), String(n));
      if (typeof history !== 'undefined' && history.replaceState && typeof window !== 'undefined' && window.location) {
        try { history.replaceState(null, '', `${window.location.pathname}${window.location.search}#sheet-${n}`); } catch (e) { /* sandboxed */ }
      }
      this.centerFilm();
      // the «التخطيط» side panel follows the page on screen («من هذه الصفحة», the draft drawn on it)
      if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function' && typeof CustomEvent === 'function') {
        window.dispatchEvent(new CustomEvent('nassakh:page-shown', { detail: { id: pid, number: n } }));
      }
    },
    // The neighbours' data is fetched ahead, so the next turn lands on a laid-out page.
    prefetchAround(n) {
      const nums = this.visibleNumbers();
      const i = nums.indexOf(n);
      [nums[i - 1], nums[i + 1]].forEach((m) => {
        const q2 = m ? pages.get(byNumber.get(m)) : null;
        if (q2 && (!sheets.has(id(q2)) || q2.stale)) this.queueSheet(m);
      });
    },
    // The page `dir` steps away in the viewer's sequence (the filter's pages), also when the shown page no
    // longer matches the filter; null at either end.
    neighbour(dir, from) {
      const nums = this.visibleNumbers();
      const base = from || (this.turning ? pendingTarget : this.current);
      if (!nums.length) return null;
      if (!base) return dir > 0 ? nums[0] : nums[nums.length - 1];
      if (dir > 0) { const next = nums.find((m) => m > base); return next === undefined ? null : next; }
      for (let i = nums.length - 1; i >= 0; i -= 1) if (nums[i] < base) return nums[i];
      return null;
    },
    canTurn(dir) {
      // reactive dependencies of the turn buttons: `counts` is replaced whenever a poll moves a page into or
      // out of a filter, which changes the sequence without changing `nPages`
      void this.current; void this.filter; void this.nPages; void this.counts;
      return this.neighbour(dir) !== null;
    },
    turn(dir) {
      const n = this.neighbour(dir);
      if (n === null) return false;
      return this.showPage(n, { dir, manual: true });
    },
    focusTitle(el) {
      const target = q(el, '.sheet-title');
      if (target && target.focus) target.focus({ preventScroll: true });
    },
    aspectNumber(p) {
      const s = sheets.get(id(p));
      const w = (s && s.width) || p.width;
      const h = (s && s.height) || p.height;
      return w > 0 && h > 0 ? Math.round((w / h) * 10000) / 10000 : Math.round(this.medianAspect * 10000) / 10000;
    },
    // A trackpad or wheel gesture turns one page (RTL: a swipe to the right and scrolling down go forward);
    // the gesture must pause before the next one counts, so inertia never flips two pages.
    onStageWheel(e) {
      if (this.view !== 'sheets' || !e || e.ctrlKey || e.metaKey) return;
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
    // ---- filmstrip (= review screen): one static thumb per page, cloned from <template id="thumb-shell">
    // and patched from the poll like the shells and the tiles (no per-thumb Alpine bindings, §11): marks
    // from the poll, thumbnails from the tiles, the sheets and api:book_filmstrip; the filter hides thumbs
    // and swapTo marks the current one
    addThumb(p, before) {
      if (!thumbTpl || !filmEl || thumbs.has(id(p))) return null;
      const el = q(thumbTpl.content ? thumbTpl.content.cloneNode(true) : null, '.bk-thumb');
      if (!el) return null;
      thumbs.set(id(p), el);
      this.patchThumb(el, p);
      filmEl.insertBefore(el, before || null);
      return el;
    },
    patchThumb(el, p) {
      if (!el || typeof el.querySelector !== 'function') return;
      el.dataset.number = String(p.number);
      const reader = READER_TITLES[p.readers];
      el.setAttribute('title', `صفحة ${p.number} — ${p.status_label || ''}${reader ? ` · ${reader}` : ''}`);
      const s = sheets.get(id(p));
      const w = (s && s.width) || p.width;
      const h = (s && s.height) || p.height;
      const frame = q(el, '.bk-thumb-img');
      if (frame && w > 0 && h > 0) { const ar = `${w} / ${h}`; if (frame.style.getPropertyValue('--thumb-ar') !== ar) frame.style.setProperty('--thumb-ar', ar); }
      const src = p.thumb_url || (s && s.thumb_url) || p.scan_thumb_url || '';
      const img = q(el, 'img');
      if (img) { if (src && img.getAttribute('src') !== src) img.setAttribute('src', src); setHidden(img, !src); }
      const reviewed = Boolean(p.is_reviewed);
      const unresolved = reviewed ? 0 : p.n_unresolved || 0;
      el.classList.toggle('is-pending', !reviewed && !DONE_STATUSES.includes(p.status));
      el.classList.toggle('is-excluded', Boolean(p.is_excluded));
      el.classList.toggle('is-error', Boolean(p.error));
      setText(q(el, '.bk-thumb-num'), p.number);
      setHidden(q(el, '.bk-thumb-mark.is-check'), !reviewed);
      const count = q(el, '.bk-thumb-mark.is-count');
      if (count) { setHidden(count, !(unresolved > 0)); setText(count, unresolved); }
      setHidden(q(el, '.bk-thumb-mark.is-live'), !(this.active && PROCESSING_STATUSES.includes(p.status) && !p.error && !p.is_excluded));
      this.patchReader(q(el, '.mark-reader'), p);
      if (guidesMode) this.patchThumbGuides(el, p);
      setHidden(el, !this.matches(p));
    },
    markThumb(el, current) {
      if (!el || !el.classList) return;
      el.classList.toggle('is-current', current);
      if (current) el.setAttribute('aria-current', 'page');
      else if (el.removeAttribute) el.removeAttribute('aria-current');
    },
    refreshFilmSoon(ms = FILM_REFRESH_MS) {
      if (!this.filmstripUrl) return;
      clearTimeout(filmTimer);
      filmTimer = setTimeout(() => this.loadFilm(), ms);
    },
    async loadFilm() {
      if (!this.filmstripUrl) return;
      try {
        const res = await fetch(this.filmstripUrl, { headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        (Array.isArray(data) ? data : (data && data.pages) || []).forEach((item) => {
          const p = item && item.id != null ? pages.get(String(item.id)) : null;
          if (p && item.thumb_url && item.thumb_url !== p.thumb_url) {
            p.thumb_url = item.thumb_url;
            this.patchThumb(thumbs.get(id(p)), p);
            if (guidesMode) this.patchTile(tiles.get(id(p)), p); // the mode's grid draws bands on the prepared image
          }
        });
      } catch (e) {
        // thumbnails only: the next refresh retries
      }
    },
    // The current thumbnail stays in view: when it is not fully visible the strip scrolls it to the middle
    // (vertical in the side panel, horizontal on a narrow screen). Only the strip scrolls, never the page.
    centerFilm() {
      const film = filmEl || (this.$refs && this.$refs.film);
      if (!film || typeof film.querySelector !== 'function') return;
      const run = () => {
        const item = film.querySelector(`[data-number="${this.current}"]`);
        // the strip scrolls itself when it overflows (horizontal on a narrow screen), else the side panel does
        const own = film.scrollHeight > film.clientHeight + 1 || film.scrollWidth > film.clientWidth + 1;
        const scroller = own || typeof film.closest !== 'function' ? film : film.closest('.bk-side') || film;
        if (!item || !item.getBoundingClientRect || !scroller.getBoundingClientRect || !scroller.scrollBy) return;
        const f = scroller.getBoundingClientRect();
        const r = item.getBoundingClientRect();
        if (!f.height || !r.height) return; // hidden (grid view)
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

    // ------------------------------------------------------------ DOM binding: shells, observers, listeners
    bindDom() {
      const root = this.$el || (typeof document !== 'undefined' && document.querySelector ? document.querySelector('[data-book-dashboard]') : null);
      if (!root || typeof root.querySelector !== 'function') return;
      stackEl = root.querySelector('[data-sheet-stack]');
      gridEl = root.querySelector('[data-page-grid]');
      filmEl = root.querySelector('[data-film-track]');
      shellTpl = document.getElementById('sheet-shell');
      tileTpl = document.getElementById('tile-shell');
      bodyTpl = document.getElementById('sheet-body');
      thumbTpl = document.getElementById('thumb-shell');
      guidesTpl = guidesMode ? document.getElementById('sheet-guides') : null;
      if (stackEl) stackEl.querySelectorAll('.page-sheet').forEach((el) => { if (el.dataset.pageId) shells.set(el.dataset.pageId, el); });
      if (gridEl) gridEl.querySelectorAll('.page-tile').forEach((el) => { if (el.dataset.pageId) tiles.set(el.dataset.pageId, el); });
      if (filmEl && thumbTpl) {
        [...pages.values()].sort(byNumberAsc).forEach((p) => this.addThumb(p, null)); // in order: appended
        filmEl.addEventListener('click', (e) => {
          const thumb = e.target && e.target.closest ? e.target.closest('.bk-thumb') : null;
          if (thumb) this.showPage(Number(thumb.dataset.number), { manual: true });
        });
      }
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
      if (gridEl) {
        gridEl.addEventListener('click', (e) => {
          const link = e.target && e.target.closest ? e.target.closest('.page-tile-link') : null;
          if (!link || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
          const tileEl = link.closest('.page-tile');
          const p = tileEl ? pages.get(tileEl.dataset.pageId) : null;
          if (!p) return;
          e.preventDefault();
          this.setView('sheets');
          this.showPage(p.number, { instant: true, manual: true });
        });
      }
      this.applyFilter();
      this.updateSheetHeight();
      window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => this.updateSheetHeight(), 120); });
      window.addEventListener('scroll', () => this.onScroll(), { passive: true });
      // the follow mode ends only with a page the reader chose (showPage manual, §8.3): scrolling the
      // filmstrip or the attention list and the hot-line keys are not page choices
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
      if (guidesMode) { // the prepared page with its bands (_sheet_guides.html); no decode handle
        if (body && guidesTpl && guidesTpl.content) { body.appendChild(guidesTpl.content.cloneNode(true)); this.patchGuidesBody(el, p); }
      } else if (body && bodyTpl && bodyTpl.content) {
        body.appendChild(bodyTpl.content.cloneNode(true));
        this.wireBody(el, p);
        const D = decode();
        const fac = q(body, '.sheet-fac');
        if (D && fac && typeof D.sheet === 'function') {
          const h = D.sheet(fac, { scan: q(body, '.sheet-lines'), figure: q(body, '.sheet-scan') });
          handles.set(pid, h);
          // a cached payload is rendered even when stale: a one-poll-old page can only lag, and the refetch
          // then plays the provisional → final wave instead of a skeleton flash before the text
          h.update({ page: sheets.has(pid) ? this.sheetFor(pid) : p, active: this.active });
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
          cleanImg.setAttribute('alt', `الصفحة ${p.number} بعد التجهيز`);
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
      if (title) { title.setAttribute('href', p.primary_url || p.url || '#'); setText(q(title, 'bdi'), p.number); }
      const detail = q(el, '.sheet-detail');
      if (detail) { detail.setAttribute('href', p.url || '#'); detail.setAttribute('aria-label', `تفاصيل المعالجة للصفحة ${p.number}`); }
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
      setHidden(el, !this.matches(p) && !this.isShown(id(p)));
      if (guidesMode) {
        this.patchSheetGuides(el, p);
        if (mounted.has(id(p))) this.patchGuidesBody(el, p);
      } else if (mounted.has(id(p))) this.wireBody(el, p);
    },
    // The half-disc (D73) of a tile or a thumb: shown for one reader, in the danger tint for Tesseract alone.
    patchReader(mark, p) {
      if (!mark) return;
      const title = READER_TITLES[p.readers] || '';
      setHidden(mark, !title);
      mark.classList.toggle('is-tesseract', p.readers === 'tesseract');
      if (title) mark.setAttribute('title', title); else mark.removeAttribute('title');
      if (title) setText(q(mark, '.sr-only'), title); // the tile's words for a screen reader (the thumb's is aria-hidden)
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
        link.setAttribute('href', p.primary_url || p.url || '#');
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
      const review = q(el, '.page-tile-review');
      if (review) { review.setAttribute('href', p.review_url || '#'); review.setAttribute('aria-label', `مراجعة الصفحة ${p.number}`); setHidden(review, !(p.text_state === 'final' && !p.is_excluded)); }
      const detail = q(el, '.page-tile-detail');
      if (detail) { detail.setAttribute('href', p.url || '#'); detail.setAttribute('aria-label', `تفاصيل المعالجة للصفحة ${p.number}`); }
      setHidden(q(el, '.tile-mark-check'), !p.is_reviewed);
      const count = q(el, '.tile-mark-count');
      if (count) { setHidden(count, p.is_reviewed || !(p.n_unresolved > 0)); setText(count, p.n_unresolved || 0); count.setAttribute('title', `علامات لم تُحسم: ${p.n_unresolved || 0}`); }
      this.patchReader(q(el, '.mark-reader'), p);
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
      if (guidesMode) this.patchTileGuides(el, p);
      setHidden(el, !this.matches(p));
    },

    // ------------------------------------------------------------ «التخطيط» mode: the guides state (D67, §3.11)
    // api:book_guides feeds the grid, the filmstrip and the chips: the whole book once on load (and when the
    // pages are prepared), then only ranges of the pages the poll reports as changed, batched like the sheets.
    fetchGuides(from, to) {
      const url = guidesUrls.state;
      if (!url || typeof fetch !== 'function') return Promise.resolve(false);
      const sep = url.includes('?') ? '&' : '?';
      return fetch(from ? `${url}${sep}from=${from}&to=${to}` : url, { headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store' })
        .then((res) => {
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          return res.json();
        })
        .then((data) => { this.applyGuidesState(data); return true; })
        .catch(() => {
          if (from) { for (let n = from; n <= to; n += 1) guidesPending.add(n); } // retried with the next change
          return false;
        });
    },
    queueGuides(numbers) {
      (numbers || []).forEach((n) => { if (Number(n) > 0) guidesPending.add(Number(n)); });
      if (!guidesPending.size) return;
      clearTimeout(guidesTimer);
      guidesTimer = setTimeout(() => this.flushGuides(), SHEET_FLUSH_MS);
    },
    flushGuides() {
      const ranges = batchRanges([...guidesPending], GUIDES_BATCH);
      guidesPending.clear();
      return Promise.all(ranges.map(([from, to]) => this.fetchGuides(from, to)));
    },
    applyGuidesState(data) {
      if (!data) return;
      if (data.book) {
        this.guidesBook = data.book;
        if (this.layoutStage && data.book.awaits_ocr_start === false) this.startedElsewhere = true;
      }
      if (data.stats) this.guidesStats = data.stats;
      this.applyGuideEntries(data.pages || []);
      if (guidesHooks && guidesHooks.onState) guidesHooks.onState(data);
    },
    // Compact entries (api:book_guides, and the answers of the guides writes). Tiles, thumbs and heads follow;
    // a mounted page whose bands, doubts or override differ from its loaded block is fetched again.
    applyGuideEntries(entries) {
      let any = false;
      (entries || []).forEach((e) => {
        if (!e || e.id == null) return;
        const pid = String(e.id);
        guideEntries.set(pid, e);
        const p = pages.get(pid);
        if (!p) return;
        any = true;
        // a loaded block that no longer matches is stale: fetched again now when mounted, else at its mount
        const s = sheets.get(pid);
        const differs = !s || !s.guides || this.entryKey(this.compactOf(s)) !== this.entryKey(e);
        if (differs && s) p.stale = true;
        if (differs && mounted.has(pid)) { p.stale = true; this.queueSheet(p.number); }
        this.patchTile(tiles.get(pid), p);
        this.patchThumb(thumbs.get(pid), p);
        this.patchSheet(shells.get(pid), p);
      });
      if (any) this.recount();
    },
    entryKey(e) {
      return e ? JSON.stringify([e.b || [], Number(e.d) || 0, Boolean(e.o), e.l || '']) : '';
    },
    // The compact entry of a sheet item's full `guides` block (the same shape as api:book_guides').
    compactOf(s) {
      const g = (s && s.guides) || {};
      const before = s ? guideEntries.get(String(s.id)) || {} : {};
      return Object.assign({}, before, {
        id: s.id,
        n: s.number != null ? s.number : before.n,
        b: compactBands(g.bands),
        d: (g.doubts || []).length,
        o: Boolean(g.override && Object.keys(g.override).length),
        l: g.locked || '',
        s: s.status || before.s,
        x: s.is_excluded !== undefined ? Boolean(s.is_excluded) : Boolean(before.x),
      });
    },
    entryFromGuides(pid, s) {
      guideEntries.set(String(pid), this.compactOf(s));
      const p = pages.get(String(pid));
      if (p) { this.patchTile(tiles.get(String(pid)), p); this.patchThumb(thumbs.get(String(pid)), p); }
    },
    // A page's fresh `guides` block after a write (processing.js bookGuides): stored, drawn, counted.
    setPageGuides(pid, block) {
      const key = String(pid);
      const p = pages.get(key);
      const s = sheets.get(key) || { id: Number(key), number: p ? p.number : null };
      s.guides = block;
      sheets.set(key, s);
      if (p) p.stale = false;
      this.entryFromGuides(key, s);
      if (p) this.patchSheet(shells.get(key), p);
      this.recount();
      if (guidesHooks && guidesHooks.onSheets) guidesHooks.onSheets();
    },
    guidesSheet(pid) {
      const s = sheets.get(String(pid));
      return s && s.guides ? s.guides : null;
    },
    guidesEntry(pid) {
      return guideEntries.get(String(pid)) || null;
    },
    pageIdOf(number) {
      return byNumber.get(Number(number)) || null;
    },
    // processing.js bookGuides registers {view, escape, onState}; the dashboard asks it before drawing a page
    registerGuides(hooks) {
      guidesHooks = hooks || null;
      shells.forEach((el, pid) => { if (mounted.has(pid)) this.patchGuidesBody(el, pages.get(pid) || {}); });
    },
    rerenderGuides(pid) {
      const key = String(pid);
      const el = shells.get(key);
      if (el && mounted.has(key)) this.patchGuidesBody(el, pages.get(key) || {});
    },
    // this address with the viewer on page n: where a form posted from the page comes back to
    hereUrl(n) {
      const loc = typeof window !== 'undefined' && window.location ? window.location : { pathname: '', search: '' };
      return `${loc.pathname || ''}${loc.search || ''}#sheet-${n}`;
    },
    // Bands on a thumb or a tile: one span per band, placed with physical percentages (compact entry `b`).
    patchBands(box, list) {
      if (!box || !box.childNodes) return;
      const bands = Array.isArray(list) ? list : [];
      while (box.children.length > bands.length) box.removeChild(box.children[box.children.length - 1]);
      bands.forEach((band, i) => {
        let span = box.children[i];
        if (!span) { span = document.createElement('span'); box.appendChild(span); }
        const kind = KIND_OF[band[0]] || 'body';
        if (span.className !== `gd-b region-${kind}`) span.className = `gd-b region-${kind}`;
        const wide = band.length < 5;
        this.place(span, wide ? 0 : band[3], band[1], wide ? 1 : band[4], band[2]);
      });
    },
    // Physical left / top / width / height in percent of the image (image pixels never flip with RTL).
    place(el, x0, y0, x1, y1) {
      if (!el || !el.style) return;
      const pc = (v) => `${Math.round(Math.max(0, Math.min(1, Number(v) || 0)) * 100000) / 1000}%`;
      el.style.left = pc(x0);
      el.style.top = pc(y0);
      el.style.width = pc(Number(x1) - Number(x0));
      el.style.height = pc(Number(y1) - Number(y0));
    },
    prepared(p) {
      return Boolean(p) && PREPARED_STATUSES.includes(p.status) && !p.error;
    },
    // A grid tile in the mode: the prepared thumbnail at its own ratio, its bands, the amber mark; a prepared
    // page is done in «التخطيط», so its stage bar fills and fades.
    patchTileGuides(el, p) {
      const e = entryOf(p);
      const thumb = q(el, '.page-tile-thumb');
      if (thumb && thumb.style && p.width > 0 && p.height > 0) {
        const ar = `${p.width} / ${p.height}`;
        if (thumb.style.getPropertyValue('--tile-ar') !== ar) thumb.style.setProperty('--tile-ar', ar);
      }
      const clean = (sheets.get(id(p)) || {}).thumb_url || p.thumb_url;
      this.patchBands(q(el, '.gd-bands'), e && clean && this.prepared(p) && !p.is_excluded ? e.b : []);
      const doubt = GUIDE_FILTERS.doubt(p);
      el.classList.toggle('is-doubt', doubt);
      setHidden(q(el, '.tile-mark-doubt'), !doubt);
      const stage = q(el, '.tile-stage');
      if (stage && this.layoutStage) {
        stage.classList.toggle('is-done', p.status !== 'uploaded' || Boolean(p.is_excluded));
        const fillEl = q(stage, '.tile-stage-fill');
        if (fillEl) fillEl.style.width = p.status === 'uploaded' && !p.error ? '12%' : '100%';
      }
    },
    patchThumbGuides(el, p) {
      const e = entryOf(p);
      const clean = p.thumb_url || (sheets.get(id(p)) || {}).thumb_url;
      this.patchBands(q(el, '.gd-bands'), e && clean && this.prepared(p) && !p.is_excluded ? e.b : []);
      setHidden(q(el, '.bk-thumb-mark.is-doubt'), !GUIDE_FILTERS.doubt(p));
      if (this.layoutStage) { // a prepared page is not pending in «التخطيط»; only pages still being prepared are live
        el.classList.toggle('is-pending', p.status === 'uploaded' && !p.error);
        setHidden(q(el, '.bk-thumb-mark.is-live'), !(this.active && p.status === 'uploaded' && !p.error && !p.is_excluded));
      }
    },
    // The sheet head in the mode (§3.12): the doubts, «بضبط خاص», «التلقائي», «تجهيز الصفحة» on an error, and
    // «استثناء» / «إعادة إلى الكتاب»; the forms come back to this page in the viewer.
    patchSheetGuides(el, p) {
      const e = entryOf(p) || {};
      const g = (sheets.get(id(p)) || {}).guides || null;
      const doubts = q(el, '.sheet-doubts');
      if (doubts) {
        doubts.textContent = '';
        const labels = p.is_excluded || !g ? [] : (g.doubts || []).map((d) => d.label).filter(Boolean);
        labels.forEach((label) => { const b = document.createElement('span'); b.className = 'badge badge-warning'; b.textContent = label; doubts.appendChild(b); });
        setHidden(doubts, !labels.length);
      }
      const own = Boolean(e.o) && !p.is_excluded;
      setHidden(q(el, '.gd-own'), !own);
      const locked = e.l || (g && g.locked) || '';
      setHidden(q(el, '.gd-auto'), !(this.canEdit && own && !locked && this.prepared(p)));
      const next = this.hereUrl(p.number);
      const ex = q(el, '.gd-exclude');
      if (ex) {
        ex.setAttribute('action', p.exclude_url || fill(urls.exclude, p.number));
        const nx = q(ex, 'input[name="next"]');
        if (nx) nx.value = next;
        setText(q(ex, '.gd-exclude-label'), p.is_excluded ? 'إعادة إلى الكتاب' : 'استثناء');
      }
      const prep = q(el, '.gd-prep');
      if (prep) {
        prep.setAttribute('action', p.rerun_url || fill(urls.rerun, p.number));
        const nx = q(prep, 'input[name="next"]');
        if (nx) nx.value = next;
        setHidden(prep, !(p.error || p.status === 'error') || p.is_excluded);
      }
    },
    // The mounted page of the viewer (_sheet_guides.html): the prepared image, its bands with their chips, the
    // two guide lines (sliders) or their grips, the lock, the book-wide draft (dashed) and the save bar of a
    // started book. `guidesHooks.view` lays a pending change and the draft over the page's block.
    patchGuidesBody(el, p) {
      const body = q(el, '.sheet-body');
      const fig = q(body, '.gd-page');
      if (!fig || !p || p.id == null) return;
      const pid = id(p);
      const s = sheets.get(pid) || {};
      const base = s.guides || null;
      const view = base ? (guidesHooks && guidesHooks.view ? guidesHooks.view(pid, base) : Object.assign({ dashed: {}, draft: null, paused: false, pending: false }, base)) : null;
      const prepared = this.prepared(p);
      const img = q(fig, '.gd-img');
      const src = prepared ? (base && base.image_url) || s.display_url || s.thumb_url || p.thumb_url || '' : s.scan_thumb_url || p.scan_thumb_url || '';
      if (img) {
        if (src && img.getAttribute('src') !== src) {
          img.onload = () => this.onGuidesImage(el, img);
          img.setAttribute('src', src);
        }
        img.setAttribute('alt', `الصفحة ${p.number} بعد التجهيز`);
        setHidden(img, !src);
      }
      if (fig.dataset) fig.dataset.pageId = pid;
      const show = Boolean(prepared && !p.is_excluded && view);
      const locked = show ? view.locked || '' : '';
      const editable = show && this.canEdit && !locked;
      fig.classList.toggle('is-sweep', Boolean(this.active && p.status === 'uploaded' && !p.is_excluded && !p.error));
      fig.classList.toggle('is-excluded', Boolean(p.is_excluded));
      fig.classList.toggle('is-error', Boolean(p.error));
      fig.classList.toggle('is-editable', editable);
      fig.classList.toggle('is-paused', Boolean(show && view.paused));
      fig.classList.toggle('is-pending', Boolean(show && view.pending));
      const bands = show ? [...(view.bands || [])].sort((a, b) => (a.bbox[1] - b.bbox[1]) || (BAND_ORDER[a.kind] - BAND_ORDER[b.kind])) : [];
      this.renderBands(q(fig, '.gd-bands-full'), bands, editable);
      this.renderLines(fig, show ? view : null, editable);
      const lock = q(fig, '.gd-lock');
      setHidden(lock, !locked);
      if (locked) setText(q(lock, '.gd-lock-text'), locked === 'approved' ? 'معتمدة: لا تتغيّر' : 'فيها تصحيحات مراجعة: لا تتغيّر');
      setText(q(fig, '.gd-sr'), bands.length ? `مناطق الصفحة: ${bands.map((b) => BAND_LABELS[b.kind] || b.kind).join('، ')}` : '');
      setHidden(q(body, '.gd-savebar'), !(show && view.pending && !this.layoutStage));
    },
    // The page's bands: `.region-box.region-<kind>` (the page detail's vocabulary), each with its chip; the chips of
    // the running head, the footnotes and the page number open the band menu (processing.js), the body's is a label.
    renderBands(box, bands, editable) {
      if (!box || !box.childNodes) return;
      while (box.children.length > bands.length) box.removeChild(box.children[box.children.length - 1]);
      bands.forEach((band, i) => {
        let div = box.children[i];
        if (!div) { div = document.createElement('div'); box.appendChild(div); }
        const kind = band.kind;
        const cls = `region-box gd-band region-${kind}`;
        if (div.className !== cls) div.className = cls;
        if (div.dataset) div.dataset.kind = kind;
        const [x0, y0, x1, y1] = band.bbox;
        this.place(div, x0, y0, x1, y1);
        const menu = kind !== 'body';
        let chip = div.children[0];
        if (!chip || chip.tagName.toLowerCase() !== (menu ? 'button' : 'span')) {
          if (chip) div.removeChild(chip);
          chip = document.createElement(menu ? 'button' : 'span');
          div.appendChild(chip);
        }
        chip.className = 'gd-chip';
        setText(chip, BAND_LABELS[kind] || kind);
        if (menu) {
          chip.setAttribute('type', 'button');
          chip.setAttribute('aria-haspopup', 'menu');
          chip.setAttribute('aria-expanded', 'false');
          chip.setAttribute('aria-label', `خيارات منطقة ${BAND_LABELS[kind] || kind}`);
          if (chip.dataset) chip.dataset.kind = kind;
          chip.disabled = !editable;
        }
      });
    },
    renderLines(fig, view, editable) {
      const active = Boolean(view) && editable && !view.paused;
      Object.keys(LINE_KINDS).forEach((kind) => {
        const line = q(fig, `.guide-line.is-${kind}`);
        const at = view && view.lines ? view.lines[kind] : null;
        const y = at && at.y !== null && at.y !== undefined ? Number(at.y) : null;
        const draft = view && view.draft && kind in view.draft ? view.draft[kind] : undefined;
        if (line) {
          setHidden(line, y === null);
          if (y !== null) {
            line.style.top = `${Math.round(y * 100000) / 1000}%`;
            line.classList.remove('is-moving');
            line.classList.toggle('is-dashed', Boolean(view.dashed && view.dashed[kind]));
            line.classList.toggle('is-draft-off', draft === null);
            line.classList.toggle('is-static', !active);
            line.setAttribute('tabindex', active ? '0' : '-1');
            line.setAttribute('aria-disabled', active ? 'false' : 'true');
            line.setAttribute('aria-valuenow', pct1(y));
            line.setAttribute('aria-valuetext', `${kind === 'header' ? 'حدّ الترويسة' : 'بداية الحاشية'} عند ${pct1(y)}%`);
            if (line.dataset) line.dataset.source = at.source || '';
            setText(q(line, '.guide-handle'), `${kind === 'header' ? 'ترويسة' : 'حاشية'} ${pct1(y)}%`);
          }
        }
        setHidden(q(fig, `.gd-grip.is-${kind}`), !(active && y === null));
        const ghost = q(fig, `.gd-draft.is-${kind}`);
        if (ghost) {
          const on = draft !== undefined && draft !== null;
          setHidden(ghost, !on);
          if (on) {
            ghost.style.top = `${Math.round(Number(draft) * 100000) / 1000}%`;
            setText(q(ghost, '.guide-handle'), `${kind === 'header' ? 'ترويسة' : 'حاشية'} ${pct1(draft)}%`);
          }
        }
      });
    },
    // The prepared image decides the page's box in the viewer (its own width / height).
    onGuidesImage(el, img) {
      const w = img && img.naturalWidth;
      const h = img && img.naturalHeight;
      if (!(w > 0 && h > 0) || !el || !el.style || !el.style.setProperty) return;
      el.style.setProperty('--ar-n', String(Math.round((w / h) * 10000) / 10000));
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
      ranges.forEach(([from, to]) => { for (let n = from; n <= to; n += 1) inflight.add(n); });
      await Promise.all(ranges.map(([from, to]) => this.fetchSheets(from, to)));
    },
    async fetchSheets(from, to) {
      const sep = this.sheetsUrl.includes('?') ? '&' : '?';
      try {
        const res = await fetch(`${this.sheetsUrl}${sep}from=${from}&to=${to}${guidesMode ? '&guides=1' : ''}`, {
          headers: { Accept: 'application/json' },
          credentials: 'same-origin',
          cache: 'no-store',
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        this.applySheets(await res.json());
        for (let n = from; n <= to; n += 1) failed.delete(n);
        if (!failed.size) { this.sheetsFailed = false; sheetRetryMs = 0; } // the banner stays while a range still waits
      } catch (e) {
        // the range goes back in the queue and is retried with a backoff, also when the book is inactive
        // (no poll re-queues it) — the page on screen never stays a skeleton after one failed request
        this.sheetsFailed = true;
        for (let n = from; n <= to; n += 1) { failed.add(n); pending.add(n); }
        this.retrySheetsSoon();
      } finally {
        for (let n = from; n <= to; n += 1) inflight.delete(n);
      }
    },
    retrySheetsSoon() {
      sheetRetryMs = sheetRetryMs ? Math.min(sheetRetryMs * 2, SHEET_RETRY_MAX_MS) : SHEET_RETRY_MS;
      clearTimeout(sheetRetryTimer);
      sheetRetryTimer = setTimeout(() => this.flushSheets(), sheetRetryMs);
    },
    // A mounted page whose data is missing or stale is queued again, unless a request for it is queued or
    // on the wire already.
    ensureSheet(n) {
      const p = n ? pages.get(byNumber.get(n)) : null;
      if (!p || !mounted.has(id(p))) return;
      if ((!sheets.has(id(p)) || p.stale) && !pending.has(n) && !inflight.has(n)) this.queueSheet(n);
    },
    applySheets(data) {
      const items = Array.isArray(data) ? data : (data && (data.pages || data.sheets || data.results)) || [];
      if (data && Number(data.book_line_h_px) > 0) bookLineH = Number(data.book_line_h_px);
      items.forEach((s) => {
        if (!s || s.id == null) return;
        const pid = String(s.id);
        sheets.set(pid, s);
        if (guidesMode && s.guides) this.entryFromGuides(pid, s); // the thumbs and tiles follow the full block
        const p = pages.get(pid);
        if (p) {
          p.stale = false;
          if (s.width > 0) p.width = s.width;
          if (s.height > 0) p.height = s.height;
          if (s.thumb_url && s.thumb_url !== p.thumb_url) p.thumb_url = s.thumb_url;
          this.patchSheet(shells.get(pid), p);
          this.patchTile(tiles.get(pid), p);
          this.patchThumb(thumbs.get(pid), p); // the thumbnail and the aspect ratio may arrive here
        }
        const h = handles.get(pid);
        if (h) h.update({ page: this.sheetFor(pid), active: this.active });
        if (pid === byNumber.get(this.current)) { const el = shells.get(pid); if (el && el.style && el.style.setProperty) el.style.setProperty('--ar-n', String(this.aspectNumber(pages.get(pid) || {}))); }
      });
      if (guidesMode && items.length) {
        this.recount();
        if (guidesHooks && guidesHooks.onSheets) guidesHooks.onSheets();
      }
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
  const TAB_LABELS = { original: 'الأصل', gray: 'المُجهَّزة', bw: 'أبيض وأسود' };
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
