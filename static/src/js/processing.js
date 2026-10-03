// Alpine components for the processing screens:
//   bookGuides       – the dashboard's «التخطيط» mode (D67, PHASE7_SPEC §3.12): the side panel «لكل الصفحات» with
//                      its preview and undo, and the page on screen (drag, keys and band menus on its guide lines)
//   preprocessPanel  – page detail: rotation / crop / Sauvola controls posting to /api/pages/<id>/preprocess/
// Pure helpers of the mode live on window.NassakhGuides for the Node tests. This file runs before the deferred
// Alpine bundle, so components are registered on `alpine:init`.
(function () {
  'use strict';

  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const round4 = (value) => Math.round(value * 10000) / 10000;
  const csrfToken = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || '';

  async function postJson(url, body) {
    const response = await fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRFToken': csrfToken() },
      body: JSON.stringify(body || {}),
    });
    let data = null;
    try { data = await response.json(); } catch (_) { data = null; }
    if (!response.ok) {
      const message = data && (Array.isArray(data.errors) ? data.errors.join(' ') : data.detail);
      const error = new Error(message || 'تعذّر تنفيذ الطلب. حاول مرة أخرى.');
      error.status = response.status;
      error.started = Boolean(data && data.started);
      throw error;
    }
    return data;
  }

  // ---------------------------------------------------------------- «التخطيط»: pure helpers (window.NassakhGuides)
  const KEYS = { header: 'header_cut', footnote: 'footnote_line' };
  const pct1 = (y) => (Math.round(Number(y) * 1000) / 10).toFixed(1);
  const EASTERN = /[٠-٩۰-۹]/g;
  // A percentage typed by hand (Western or Eastern digits, «,» or «٫» as the decimal mark) → a ratio, or null.
  function ratioOf(text) {
    const clean = String(text == null ? '' : text)
      .replace(EASTERN, (d) => { const c = d.charCodeAt(0); return String(c >= 0x06f0 ? c - 0x06f0 : c - 0x0660); })
      .replace(/[,٫]/g, '.')
      .replace(/[^0-9.]/g, '');
    const v = parseFloat(clean);
    return Number.isFinite(v) ? round4(v / 100) : null;
  }
  // The middles of the gaps between detected lines (`rows`: [y0, y1] ratios), top to bottom.
  function gapMiddles(rows) {
    const sorted = (rows || []).filter((r) => Array.isArray(r) && r.length >= 2).map((r) => [Number(r[0]), Number(r[1])]).sort((a, b) => a[0] - b[0]);
    const mids = [];
    let bottom = null;
    sorted.forEach(([y0, y1]) => {
      if (bottom !== null && y0 > bottom) mids.push(round4((bottom + y0) / 2));
      bottom = bottom === null ? y1 : Math.max(bottom, y1);
    });
    return mids;
  }
  // A guide line snaps to the middle of the gap between two detected lines within `tolerance` (a ratio of the
  // page height: max(6 screen px, 0.8 × the median line height)); `free` (⌥ / Option held) never snaps.
  function snapY(y, rows, tolerance, free) {
    const v = round4(clamp(Number(y), 0, 1));
    if (free) return v;
    let best = null;
    gapMiddles(rows).forEach((m) => {
      if (Math.abs(m - v) <= tolerance && (best === null || Math.abs(m - v) < Math.abs(best - v))) best = m;
    });
    return best === null ? v : best;
  }
  // ⇧↑ / ⇧↓: the previous / next gap between lines, from y.
  function stepGap(y, rows, dir) {
    const mids = gapMiddles(rows);
    if (dir > 0) { const next = mids.find((m) => m > y + 0.0005); return next === undefined ? y : next; }
    for (let i = mids.length - 1; i >= 0; i -= 1) if (mids[i] < y - 0.0005) return mids[i];
    return y;
  }
  // Where an added line lands (§3.12): the running head in the gap after the first detected line, the footnotes in
  // the gap nearest 80 % of the height.
  function defaultY(kind, rows) {
    const mids = gapMiddles(rows);
    if (kind === 'header') return mids.length ? mids[0] : 0.06;
    if (!mids.length) return 0.8;
    return mids.reduce((best, m) => (Math.abs(m - 0.8) < Math.abs(best - 0.8) ? m : best), mids[0]);
  }
  const BAND_RANK = { running_header: 0, body: 1, footnote: 2, page_number: 3 };
  const sortBands = (bands) => bands.sort((a, b) => (a.bbox[1] - b.bbox[1]) || (BAND_RANK[a.kind] - BAND_RANK[b.kind]));
  // The bands after moving (or adding, or with y = null removing) the running-head cut or the footnote start: the two
  // bands beside the line follow it. The full bands of a guides block ({kind, bbox: [x0, y0, x1, y1], source}).
  function adjacentBands(bands, guide, y) {
    const out = (bands || []).map((b) => Object.assign({}, b, { bbox: [...b.bbox] }));
    const find = (kind) => out.find((b) => b.kind === kind) || null;
    const body = find('body');
    const off = y === null || y === undefined;
    if (guide === 'header') {
      const head = find('running_header');
      if (off) {
        if (head) { out.splice(out.indexOf(head), 1); if (body) body.bbox[1] = head.bbox[1]; }
        return sortBands(out);
      }
      if (head) head.bbox[3] = y;
      else out.push({ kind: 'running_header', bbox: [0, 0, 1, y], source: 'override' });
      if (body) body.bbox[1] = y;
      return sortBands(out);
    }
    const foot = find('footnote');
    if (off) {
      if (foot) { out.splice(out.indexOf(foot), 1); if (body) body.bbox[3] = foot.bbox[3]; }
      return sortBands(out);
    }
    if (foot) foot.bbox[1] = y;
    else if (body) out.push({ kind: 'footnote', bbox: [0, y, 1, body.bbox[3]], source: 'override' });
    if (body) body.bbox[3] = y;
    return sortBands(out);
  }
  // «ليس رقم صفحة»: the page-number band goes, the band above it runs to the foot of the page.
  function withoutNumber(bands) {
    const out = (bands || []).map((b) => Object.assign({}, b, { bbox: [...b.bbox] }));
    const num = out.find((b) => b.kind === 'page_number');
    if (!num) return out;
    out.splice(out.indexOf(num), 1);
    const above = out.filter((b) => b.kind !== 'running_header' && Math.abs(b.bbox[3] - num.bbox[1]) < 0.0005);
    above.forEach((b) => { b.bbox[3] = 1; });
    return sortBands(out);
  }
  // The body of a per-page change (api:page_guides_override, merge semantics): the keys set, the keys unset (back
  // to the book and the detection), or the whole override reset.
  function mergeBody(set, unset, reset) {
    const clean = {};
    Object.keys(set || {}).forEach((k) => {
      const v = set[k];
      clean[k] = typeof v === 'number' ? round4(v) : v;
    });
    return { merge: true, set: clean, unset: (unset || []).filter((k) => !(k in clean)), reset: Boolean(reset) };
  }
  // The request that undoes a write (its answer's `undo`): a page's previous override, or the book's (§3.10). Null
  // when there is nothing to undo (every change in «المعالجة» re-reads pages and has an explicit save instead).
  function inverse(change) {
    const undo = change && change.undo;
    if (!undo) return null;
    if (Object.prototype.hasOwnProperty.call(undo, 'replace')) return { replace: undo.replace === undefined ? null : undo.replace };
    return { undo };
  }
  // A per-page change laid over a guides block (the page on screen of a started book, until it is saved).
  function applyChange(view, change) {
    const v = view;
    if (change.reset || (change.unset && change.unset.length)) { v.dashed.header = true; v.dashed.footnote = true; }
    Object.keys(change.set || {}).forEach((key) => {
      const y = change.set[key];
      if (key === 'page_number_zone') { if (y === 'none') v.bands = withoutNumber(v.bands); return; }
      const kind = key === 'header_cut' ? 'header' : key === 'footnote_line' ? 'footnote' : '';
      if (!kind) return;
      v.bands = adjacentBands(v.bands, kind, y);
      v.lines[kind] = y === null ? null : { y, source: 'override' };
      v.dashed[kind] = true;
    });
    return v;
  }
  // The book-wide draft on the page on screen (dashed): the running head wherever the page has no cut of its own; the
  // footnote line only where no footnote was detected (§3.12). `undefined` = unchanged, null = removed here.
  function draftLines(base, set) {
    const out = {};
    const own = (base && base.override) || {};
    const foot = base && base.lines ? base.lines.footnote : null;
    if (set && 'header_cut' in set && !('header_cut' in own)) out.header = set.header_cut;
    if (set && 'footnote_line' in set && !('footnote_line' in own) && !(foot && (foot.source === 'rule' || foot.source === 'block'))) out.footnote = set.footnote_line;
    return out;
  }
  // Arabic counts of pages (assembly.render.ar_count): nominative, or after a preposition / as an object.
  const books = () => window.NassakhBooks || null;
  const pagesNom = (n) => books().arCount(n, books().PAGE_FORMS);
  const pagesGen = (n) => books().arCount(n, books().PAGE_FORMS_GEN);

  window.NassakhGuides = { ratioOf, gapMiddles, snapY, stepGap, defaultY, adjacentBands, withoutNumber, mergeBody, inverse, applyChange, draftLines, pct1 };

  document.addEventListener('alpine:init', () => {
    // ------------------------------------------------------------ «التخطيط» mode (D67)
    // Sits on `.bk-layout` in the mode and reads the dashboard (books.js bookDashboard) through
    // Alpine.store('book').dash. The dashboard draws every page (bands, lines, grips) from its guides block; this
    // component lays the page's unsaved change and the side panel's draft over it (`view`), and owns the drag, the
    // keys and chips on the page, the preview box, the API calls and the toasts. In «التخطيط» each change is saved
    // at once and answers with its inverse, which the toast's «تراجع» posts; in «المعالجة» (`?view=guides`) a
    // change waits for «حفظ وإعادة التعرّف على الصفحة», and there is no undo.
    const TOAST_MS = 6000;
    const TOAST_UNDO_MS = 9000;
    const PREVIEW_MS = 250;
    const KEY_SAVE_MS = 700; // arrow-key moves in «التخطيط» are saved once the keys rest
    const NUDGE = 0.002;
    const MINUTE_FORMS = ['دقيقة واحدة', 'دقيقتين', 'دقائق', 'دقيقة']; // after «نحو»: «نحو دقيقتين»

    Alpine.data('bookGuides', () => {
      const pending = new Map(); // page id -> unsaved change of a started book: {set, unset, reset}
      let toastAction = null;
      let toastTimer = null;
      let previewTimer = null;
      let keyMove = null; // {pid, kind, y, timer}: an arrow-key move not saved yet («التخطيط»)
      let dragCtx = null; // the move in progress: its figure, rect, rows, tolerance and the view it started from
      let chain = Promise.resolve(); // page writes go out one after the other
      let fromPage = null; // the page a draft value came from (its override drops that key, §3.10)
      let onMove = null; // the window listeners of a move in progress
      let onUp = null;

      return {
        book: {},
        ctl: { header: { on: false, pct: '' }, footnote: { on: false, pct: '' } },
        draft: null, // {set: {...}} or {reset: true}: the side panel's change, previewed, not applied
        preview: null, // api:book_guides_preview for the draft
        keepOverrides: false, // «تطبيقه عليهما أيضًا»
        busy: false,
        error: '',
        last: null, // the undo request of the last change in «التخطيط»
        drag: null, // {pid, kind, y, moved, adding}
        toast: { visible: false, message: '', hasAction: false },
        live: '',
        pageId: '',
        menu: null, // {pid, kind}: the band menu open on the page
        rev: 0, // bumped when page blocks arrive (they live in the dashboard's plain maps)

        get dash() {
          const st = typeof Alpine.store === 'function' ? Alpine.store('book') : null;
          return st ? st.dash : null;
        },
        get stage() {
          return this.dash && this.dash.layoutStage ? 'layout' : 'ocr';
        },
        get isLayout() {
          return Boolean(this.dash && this.dash.layoutStage);
        },

        init() {
          const dash = this.dash;
          this.resetControls(dash ? dash.guidesBook : null);
          if (dash && dash.current) this.pageId = String(dash.pageIdOf(dash.current) || '');
          if (dash && typeof dash.registerGuides === 'function') {
            dash.registerGuides({ view: (pid, base) => this.pageView(pid, base), escape: () => this.escape(), onState: (data) => this.onState(data), onSheets: () => { this.rev += 1; } });
          }
          const stack = this.$el && typeof this.$el.querySelector === 'function' ? this.$el.querySelector('[data-sheet-stack]') : null;
          if (stack) this.bindStack(stack);
        },
        destroy() {
          clearTimeout(toastTimer);
          clearTimeout(previewTimer);
          if (keyMove) clearTimeout(keyMove.timer);
          this.endDrag();
        },

        // ---------------------------------------------------------- the book's guides and the side panel controls
        onState(data) {
          if (data && data.book && !this.draft) this.resetControls(data.book);
          else if (data && data.book) this.book = data.book;
        },
        resetControls(book) {
          const b = book || {};
          this.book = b;
          const manual = b.source === 'manual';
          this.ctl = {
            header: { on: b.header_cut !== null && b.header_cut !== undefined, pct: b.header_cut != null ? pct1(b.header_cut) : '' },
            footnote: { on: manual && b.footnote_line != null, pct: manual && b.footnote_line != null ? pct1(b.footnote_line) : '' },
          };
        },
        get medianRule() {
          const s = this.dash && this.dash.guidesStats;
          return s && s.median_rule !== null && s.median_rule !== undefined ? Number(s.median_rule) : null;
        },
        get medianLabel() {
          return this.medianRule === null ? '' : `الموضع الوسيط ${pct1(this.medianRule)}%`;
        },
        get statsText() {
          const s = this.dash && this.dash.guidesStats;
          return (s && s.text) || '';
        },
        get isManual() {
          return (this.book || {}).source === 'manual';
        },
        // The cut of the page on screen, for «من هذه الصفحة» (null when it has none).
        get pageCut() {
          void this.rev;
          const v = this.pageId && this.dash ? this.dash.guidesSheet(this.pageId) : null;
          return v && v.lines && v.lines.header ? Number(v.lines.header.y) : null;
        },
        fromThisPage() {
          if (this.pageCut === null) return;
          this.ctl.header = { on: true, pct: pct1(this.pageCut) };
          fromPage = this.pageId;
          this.onControl();
        },
        useMedian() {
          if (this.medianRule === null) return;
          this.ctl.footnote = { on: true, pct: pct1(this.medianRule) };
          this.onControl();
        },
        // The side panel's change against the book's guides, at the panel's 0.1 % resolution.
        changes() {
          const b = this.book || {};
          const out = {};
          const now = { header: b.header_cut != null ? Number(b.header_cut) : null, footnote: b.source === 'manual' && b.footnote_line != null ? Number(b.footnote_line) : null };
          Object.keys(KEYS).forEach((kind) => {
            const c = this.ctl[kind];
            const want = c.on ? ratioOf(c.pct) : null;
            if (c.on && want === null) return; // an empty or unreadable field changes nothing yet
            if ((now[kind] === null ? '' : pct1(now[kind])) !== (want === null ? '' : pct1(want))) out[KEYS[kind]] = want;
          });
          return out;
        },
        onControl() {
          ['header', 'footnote'].forEach((kind) => {
            const c = this.ctl[kind];
            if (c.on && String(c.pct).trim() === '') c.pct = pct1(kind === 'header' ? (this.pageCut !== null ? this.pageCut : 0.06) : (this.medianRule !== null ? this.medianRule : 0.8));
          });
          const set = this.changes();
          if (!Object.keys(set).length) { this.dropDraft(); return; }
          this.draft = { set };
          this.error = '';
          this.menu = null;
          this.schedulePreview();
          this.redraw();
        },
        // «إزالة الضبط العام»: the book back to its automatic guides, previewed like any other change
        resetBook() {
          this.draft = { reset: true };
          this.error = '';
          this.schedulePreview();
          this.redraw();
        },
        dropDraft() {
          const had = Boolean(this.draft);
          clearTimeout(previewTimer);
          this.draft = null;
          this.preview = null;
          this.keepOverrides = false;
          this.error = '';
          fromPage = null;
          this.resetControls(this.book);
          if (had) this.redraw();
          return had;
        },
        draftBody() {
          if (!this.draft) return null;
          if (this.draft.reset) return { reset: true, stage: this.stage };
          const set = this.draft.set;
          const body = { set, reset_overrides: this.keepOverrides ? Object.keys(set) : [], stage: this.stage };
          if (fromPage) body.from_page = Number(fromPage);
          return body;
        },
        schedulePreview() {
          clearTimeout(previewTimer);
          this.preview = null;
          previewTimer = setTimeout(() => this.requestPreview(), PREVIEW_MS);
        },
        async requestPreview() {
          const body = this.draftBody();
          const url = this.dash && this.dash.guidesUrls.preview;
          if (!body || !url) return null;
          const asked = JSON.stringify(body);
          try {
            const data = await postJson(url, body);
            if (JSON.stringify(this.draftBody()) !== asked) return null; // the draft moved on meanwhile
            this.preview = data;
            this.live = [this.previewChanged, this.previewCut, this.previewReocr].filter(Boolean).join(' ');
            if (data && data.header_at && Object.keys(data.header_at).length) this.redraw(); // the draft at this page's own cut
            return data;
          } catch (err) {
            this.failed(err, true); // a value the server refuses is said under the controls, not in a toast
            return null;
          }
        },
        // ---- the preview box (§3.12, §3.14)
        get previewChanged() {
          const n = this.preview ? Number(this.preview.changed) || 0 : 0;
          if (!this.preview) return '';
          return n ? `يغيّر هذا ${pagesGen(n)}.` : 'لا يغيّر هذا أي صفحة.';
        },
        get cutPages() {
          return (this.preview && this.preview.cut) || [];
        },
        get previewCut() {
          const n = this.cutPages.length;
          return n ? `في ${pagesGen(n)} يقطع خطٌّ سطرًا أو تغطّي منطقةٌ سطرًا من المتن:` : '';
        },
        get keptCount() {
          return this.preview ? ((this.preview.kept_overrides || []).length) : 0;
        },
        get keptText() {
          const n = this.keptCount;
          if (!n) return '';
          if (n === 1) return 'صفحة واحدة بضبط خاص تبقى كما هي';
          if (n === 2) return 'صفحتان بضبط خاص تبقيان كما هما';
          return `${pagesNom(n)} بضبط خاص تبقى كما هي`;
        },
        get keptApply() {
          return this.keptCount === 2 ? 'تطبيقه عليهما أيضًا' : 'تطبيقه عليها أيضًا';
        },
        get previewReocr() {
          if (!this.preview || this.isLayout) return '';
          const n = Number(this.preview.reocr) || 0;
          if (!n) return '';
          const minutes = Number(this.preview.minutes) > 0 ? `؛ نحو ${books().arCount(Math.ceil(Number(this.preview.minutes)), MINUTE_FORMS)}` : '';
          return `سيُعاد التعرّف على نص ${pagesGen(n)}${minutes}.`;
        },
        get previewLocked() {
          if (!this.preview || this.isLayout) return '';
          const n = (this.preview.locked || []).length;
          if (!n) return '';
          if (n === 1) return 'صفحة واحدة معتمدة أو فيها تصحيحات مراجعة لا تتغيّر.';
          if (n === 2) return 'صفحتان معتمدتان أو فيهما تصحيحات مراجعة لا تتغيّران.';
          return `${pagesNom(n)} معتمدة أو فيها تصحيحات مراجعة لا تتغيّر.`;
        },
        goToPage(n) {
          const dash = this.dash;
          if (!dash) return;
          dash.setView('sheets');
          dash.showPage(Number(n), { instant: true, manual: true });
        },
        // «تطبيق على كل الصفحات»
        async applyDraft() {
          const body = this.draftBody();
          const url = this.dash && this.dash.guidesUrls.apply;
          if (!body || !url || this.busy) return null;
          this.busy = true;
          this.error = '';
          try {
            const data = await postJson(url, body);
            this.onApplied(data, false);
            return data;
          } catch (err) {
            this.failed(err);
            return null;
          } finally {
            this.busy = false;
          }
        },
        onApplied(data, undone) {
          const dash = this.dash;
          const n = ((data && data.changed) || []).length;
          clearTimeout(previewTimer);
          this.draft = null;
          this.preview = null;
          this.keepOverrides = false;
          fromPage = null;
          if (dash) dash.applyGuidesState({ book: data.book, pages: data.pages || [] });
          this.resetControls(data.book || this.book);
          this.redraw();
          const undo = this.isLayout && !undone ? inverse(data) : null;
          this.last = undo;
          let message;
          if (undone) message = 'أُلغي التغيير.';
          else if (!n) message = 'لم تتغيّر أي صفحة.';
          else message = this.isLayout ? `طُبّق على ${pagesGen(n)}` : `طُبّق على ${pagesGen(n)}؛ يُعاد التعرّف على نصّها.`;
          this.showToast(message, undo ? () => this.undoBook(undo) : null);
        },
        async undoBook(undo) {
          const url = this.dash && this.dash.guidesUrls.apply;
          if (!url) return;
          try {
            const data = await postJson(url, Object.assign({}, undo, { stage: this.stage }));
            this.onApplied(data, true);
          } catch (err) {
            this.failed(err);
          }
        },

        // ---------------------------------------------------------- the page on screen
        onPageShown(detail) {
          const before = this.pageId;
          this.pageId = detail && detail.id != null ? String(detail.id) : '';
          this.menu = null;
          if (this.draft && this.dash && before !== this.pageId) { if (before) this.dash.rerenderGuides(before); if (this.pageId) this.dash.rerenderGuides(this.pageId); }
        },
        redraw() {
          if (this.dash && this.pageId) this.dash.rerenderGuides(this.pageId);
        },
        // The page as drawn: its block, with its unsaved change (a started book) and the side panel's draft.
        // (Names here never repeat the dashboard's: this component's scope sits inside the dashboard's.)
        pageView(pid, base) {
          const v = Object.assign({}, base, {
            bands: (base.bands || []).map((b) => Object.assign({}, b, { bbox: [...b.bbox] })),
            lines: { header: base.lines && base.lines.header ? Object.assign({}, base.lines.header) : null, footnote: base.lines && base.lines.footnote ? Object.assign({}, base.lines.footnote) : null },
            dashed: {},
            draft: null,
            paused: Boolean(this.draft),
            pending: false,
          });
          const change = pending.get(String(pid));
          if (change) { applyChange(v, change); v.pending = true; }
          if (this.draft && this.draft.set && String(pid) === this.pageId) v.draft = draftLines(base, this.fittedSet(pid, this.draft.set));
          return v;
        },
        // Owner 18: the book's running-head cut lands on each page from the page's own geometry; the preview names the
        // pages where that is not the plain value (`header_at`), and the draft is drawn there.
        fittedSet(pid, set) {
          const at = this.preview && this.preview.header_at ? this.preview.header_at[String(pid)] : undefined;
          if (at === undefined || !set || set.header_cut === null || set.header_cut === undefined) return set;
          return Object.assign({}, set, { header_cut: Number(at) });
        },
        // A change to one page: saved at once in «التخطيط», held for the explicit save in «المعالجة».
        change(pid, change, focusKind) {
          if (!pid) return null;
          this.menu = null;
          if (!this.isLayout) {
            const before = pending.get(String(pid)) || { set: {}, unset: [], reset: false };
            const merged = change.reset ? { set: {}, unset: [], reset: true } : { set: Object.assign({}, before.set, change.set || {}), unset: [...before.unset, ...(change.unset || [])], reset: before.reset };
            pending.set(String(pid), merged);
            this.dash.rerenderGuides(pid);
            this.focusLine(pid, focusKind);
            return null;
          }
          return this.savePage(pid, change, focusKind);
        },
        savePage(pid, change, focusKind) {
          const url = this.pageUrl(pid);
          if (!url) return Promise.resolve(null);
          const body = Object.assign(mergeBody(change.set, change.unset, change.reset), { stage: this.stage });
          const run = async () => {
            try {
              const data = await postJson(url, body);
              this.dash.setPageGuides(pid, data.guides);
              const n = this.numberOf(pid);
              if (this.isLayout) {
                const undo = inverse(data);
                this.last = undo;
                this.showToast(change.reset ? 'عادت الصفحة إلى التلقائي' : 'حُفظ لهذه الصفحة', undo ? () => this.undoPage(pid, undo) : null);
              } else {
                this.showToast(`يُعاد التعرّف على نص الصفحة ${n}.`, null);
              }
              this.focusLine(pid, focusKind);
              return data;
            } catch (err) {
              this.failed(err);
              if (this.dash) this.dash.rerenderGuides(pid); // the page goes back to what is stored
              return null;
            }
          };
          chain = chain.then(run, run);
          return chain;
        },
        async undoPage(pid, undo) {
          const url = this.pageUrl(pid);
          if (!url) return;
          try {
            const data = await postJson(url, Object.assign({}, undo, { stage: this.stage }));
            this.dash.setPageGuides(pid, data.guides);
            this.showToast('أُلغي التغيير.', null);
          } catch (err) {
            this.failed(err);
          }
        },
        // «حفظ وإعادة التعرّف على الصفحة» / «إلغاء» (a started book)
        savePending(pid) {
          const change = pending.get(String(pid));
          if (!change) return Promise.resolve(null);
          pending.delete(String(pid));
          return this.savePage(pid, change, null);
        },
        cancelPending(pid) {
          if (!pending.delete(String(pid))) return false;
          this.dash.rerenderGuides(pid);
          return true;
        },
        hasPending(pid) {
          return pending.has(String(pid));
        },
        pageUrl(pid) {
          const t = this.dash && this.dash.guidesUrls.page;
          return t ? String(t).replace('__id__', String(pid)) : '';
        },
        numberOf(pid) {
          const p = this.dash && this.dash.page ? this.dash.page(pid) : null;
          return p ? p.number : '';
        },
        focusLine(pid, kind) {
          if (!kind) return;
          const run = () => {
            const fig = this.figureOf(pid);
            const line = fig ? fig.querySelector(`.guide-line.is-${kind}`) : null;
            if (line && !line.hidden && line.focus) line.focus({ preventScroll: true });
          };
          if (this.$nextTick) this.$nextTick(run); else run();
        },
        figureOf(pid) {
          const root = this.$el && this.$el.querySelector ? this.$el : null;
          return root ? root.querySelector(`.gd-page[data-page-id="${pid}"]`) : null;
        },
        // The page's view as drawn now (its block with the unsaved change), for a move that starts on it.
        currentView(pid) {
          const base = this.dash ? this.dash.guidesSheet(pid) : null;
          return base ? this.pageView(pid, base) : null;
        },

        // ---------------------------------------------------------- the page's lines, grips, chips and menu
        bindStack(stack) {
          stack.addEventListener('pointerdown', (e) => this.onPointerDown(e));
          stack.addEventListener('click', (e) => this.onClick(e));
          stack.addEventListener('keydown', (e) => this.onLineKey(e));
        },
        editableFigure(el) {
          const fig = el && el.closest ? el.closest('.gd-page') : null;
          return fig && fig.classList.contains('is-editable') && !fig.classList.contains('is-paused') ? fig : null;
        },
        onPointerDown(e) {
          if (!e || (e.button !== undefined && e.button !== 0) || !e.target || !e.target.closest) return;
          const handle = e.target.closest('.guide-line, .gd-grip');
          const fig = this.editableFigure(handle);
          if (!handle || !fig || handle.classList.contains('is-static')) return;
          const kind = handle.classList.contains('is-header') ? 'header' : 'footnote';
          const pid = fig.dataset.pageId;
          const view = this.currentView(pid);
          if (!view) return;
          e.preventDefault();
          const rect = fig.getBoundingClientRect();
          const rows = view.rows || [];
          const heights = rows.map((r) => r[1] - r[0]).sort((a, b) => a - b);
          const median = heights.length ? heights[Math.floor(heights.length / 2)] : 0;
          dragCtx = { fig, rect, view, rows, tol: Math.max(rect.height > 0 ? 6 / rect.height : 0.004, 0.8 * median) };
          this.menu = null;
          this.drag = { pid, kind, y: view.lines[kind] ? Number(view.lines[kind].y) : null, start: e.clientY, moved: false, adding: handle.classList.contains('gd-grip') };
          if (handle.setPointerCapture && e.pointerId !== undefined) { try { handle.setPointerCapture(e.pointerId); } catch (_) { /* fine */ } }
          onMove = (ev) => this.onPointerMove(ev);
          onUp = (ev) => this.onPointerUp(ev);
          window.addEventListener('pointermove', onMove);
          window.addEventListener('pointerup', onUp);
          window.addEventListener('pointercancel', onUp);
        },
        // The line's allowed range: the running head within the top half and above the footnotes; the footnotes
        // below the running head and the upper fifth.
        clampFor(kind, y, view) {
          const other = view.lines[kind === 'header' ? 'footnote' : 'header'];
          const oy = other ? Number(other.y) : null;
          if (kind === 'header') return clamp(y, 0.005, Math.min(0.5, oy !== null ? oy - 0.01 : 1));
          return clamp(y, Math.max(0.2, oy !== null ? oy + 0.01 : 0), 0.995);
        },
        onPointerMove(e) {
          const d = this.drag;
          if (!d || !dragCtx) return;
          if (!d.moved && Math.abs(e.clientY - d.start) < 3) return;
          d.moved = true;
          const r = dragCtx.rect;
          const raw = r.height > 0 ? (e.clientY - r.top) / r.height : 0;
          const y = snapY(this.clampFor(d.kind, raw, dragCtx.view), dragCtx.rows, dragCtx.tol, Boolean(e.altKey));
          d.y = y;
          this.paintMove(dragCtx.fig, dragCtx.view, d.kind, y);
        },
        onPointerUp() {
          const d = this.drag;
          const ctx = dragCtx;
          this.endDrag();
          if (!d || !ctx) return;
          let y = d.y;
          if (!d.moved) {
            if (!d.adding) return; // a click on a line only focuses it
            y = defaultY(d.kind, ctx.rows);
          }
          if (y === null || y === undefined) return;
          this.change(d.pid, { set: { [KEYS[d.kind]]: round4(y) } }, d.kind);
        },
        endDrag() {
          if (onMove && typeof window.removeEventListener === 'function') window.removeEventListener('pointermove', onMove);
          if (onUp && typeof window.removeEventListener === 'function') { window.removeEventListener('pointerup', onUp); window.removeEventListener('pointercancel', onUp); }
          onMove = null;
          onUp = null;
          this.drag = null;
          dragCtx = null;
        },
        // Direct style writes while a line moves: the line, its pill, and the two bands beside it.
        paintMove(fig, view, kind, y) {
          const dash = this.dash;
          const line = fig.querySelector(`.guide-line.is-${kind}`);
          if (line) {
            line.hidden = false;
            line.style.top = `${Math.round(y * 100000) / 1000}%`;
            line.classList.add('is-moving');
            const handle = line.querySelector('.guide-handle');
            if (handle) handle.textContent = `${kind === 'header' ? 'ترويسة' : 'حاشية'} ${pct1(y)}%`;
            line.setAttribute('aria-valuenow', pct1(y));
            line.setAttribute('aria-valuetext', `${kind === 'header' ? 'حدّ الترويسة' : 'بداية الحاشية'} عند ${pct1(y)}%`);
          }
          const grip = fig.querySelector(`.gd-grip.is-${kind}`);
          if (grip) grip.hidden = true;
          if (dash && dash.renderBands) dash.renderBands(fig.querySelector('.gd-bands-full'), adjacentBands(view.bands, kind, y), true);
        },
        // ↑ / ↓ move a focused line by 0.2 % (⇧: to the previous / next gap), Enter saves (a started book), Delete
        // removes it from this page. Handled keys never reach the dashboard's map.
        onLineKey(e) {
          const line = e.target && e.target.closest ? e.target.closest('.guide-line') : null;
          const fig = this.editableFigure(line);
          if (!line || !fig || e.altKey || e.metaKey || e.ctrlKey) return;
          const kind = line.classList.contains('is-header') ? 'header' : 'footnote';
          const pid = fig.dataset.pageId;
          const k = e.key;
          if (k === 'ArrowUp' || k === 'ArrowDown') {
            e.preventDefault();
            const view = keyMove && keyMove.pid === pid && keyMove.kind === kind ? keyMove.view : this.currentView(pid);
            if (!view || !view.lines[kind]) return;
            const from = keyMove && keyMove.pid === pid && keyMove.kind === kind ? keyMove.y : Number(view.lines[kind].y);
            const dir = k === 'ArrowDown' ? 1 : -1;
            const y = round4(this.clampFor(kind, e.shiftKey ? stepGap(from, view.rows, dir) : from + dir * NUDGE, view));
            if (keyMove) clearTimeout(keyMove.timer);
            keyMove = { pid, kind, y, view, timer: null };
            this.paintMove(fig, view, kind, y);
            if (this.isLayout) keyMove.timer = setTimeout(() => this.flushKeyMove(), KEY_SAVE_MS);
            return;
          }
          if (k === 'Enter') {
            e.preventDefault();
            if (keyMove && keyMove.pid === pid) this.flushKeyMove();
            if (!this.isLayout) this.savePending(pid);
            return;
          }
          if (k === 'Delete' || k === 'Backspace') {
            e.preventDefault();
            if (keyMove) { clearTimeout(keyMove.timer); keyMove = null; }
            this.change(pid, { set: { [KEYS[kind]]: null } }, null);
          }
        },
        flushKeyMove() {
          const m = keyMove;
          keyMove = null;
          if (!m) return null;
          clearTimeout(m.timer);
          return this.change(m.pid, { set: { [KEYS[m.kind]]: m.y } }, m.kind);
        },
        onClick(e) {
          const t = e.target;
          if (!t || !t.closest) return;
          const chip = t.closest('button.gd-chip');
          if (chip) { e.preventDefault(); this.toggleMenu(chip); return; }
          const item = t.closest('.gd-menu [data-act]');
          if (item) { e.preventDefault(); this.menuAction(item.dataset.act); return; }
          const auto = t.closest('.gd-auto');
          if (auto) {
            const art = auto.closest('.page-sheet');
            if (art) { e.preventDefault(); this.change(art.dataset.pageId, { reset: true }, null); }
            return;
          }
          const act = t.closest('.gd-savebar [data-act]');
          if (act) {
            const art = act.closest('.page-sheet');
            if (!art) return;
            e.preventDefault();
            if (act.dataset.act === 'save') this.savePending(art.dataset.pageId);
            else this.cancelPending(art.dataset.pageId);
            return;
          }
          if (this.menu && !t.closest('.gd-menu')) this.closeMenu();
        },
        // The band menu (§3.12): «إزالة من هذه الصفحة» and «تطبيق على كل الصفحات…» for the running head and the
        // footnotes, «ليس رقم صفحة» for the page number.
        toggleMenu(chip) {
          const fig = this.editableFigure(chip);
          if (!fig || chip.disabled) return;
          const kind = chip.dataset.kind;
          const pid = fig.dataset.pageId;
          const open = this.menu && this.menu.pid === pid && this.menu.kind === kind;
          this.closeMenu();
          if (open) return;
          const menu = fig.querySelector('.gd-menu');
          if (!menu) return;
          this.menu = { pid, kind };
          const band = chip.closest('.gd-band');
          menu.querySelectorAll('[data-act]').forEach((el) => {
            const act = el.dataset.act;
            el.hidden = kind === 'page_number' ? act !== 'no-number' : act === 'no-number';
          });
          // under the band's chip; above it for a band in the lower part of the page (the page number)
          const top = band ? parseFloat(band.style.top) || 0 : 0;
          menu.classList.toggle('is-above', top > 60);
          menu.style.top = top > 60 ? '' : `${top}%`;
          menu.style.bottom = top > 60 ? `${Math.max(0, 100 - top)}%` : '';
          menu.hidden = false;
          chip.setAttribute('aria-expanded', 'true');
          const first = [...menu.querySelectorAll('[data-act]')].find((el) => !el.hidden);
          if (first && first.focus) first.focus({ preventScroll: true });
        },
        closeMenu() {
          if (!this.menu) return false;
          const fig = this.figureOf(this.menu.pid);
          const menu = fig ? fig.querySelector('.gd-menu') : null;
          if (menu) menu.hidden = true;
          const chip = fig ? fig.querySelector(`.gd-band[data-kind="${this.menu.kind}"] .gd-chip`) : null;
          if (chip) { chip.setAttribute('aria-expanded', 'false'); if (chip.focus) chip.focus({ preventScroll: true }); }
          this.menu = null;
          return true;
        },
        menuAction(act) {
          const m = this.menu;
          if (!m) return;
          this.closeMenu();
          const kind = m.kind === 'running_header' ? 'header' : m.kind === 'footnote' ? 'footnote' : '';
          if (act === 'no-number') { this.change(m.pid, { set: { page_number_zone: 'none' } }, null); return; }
          if (!kind) return;
          if (act === 'remove') { this.change(m.pid, { set: { [KEYS[kind]]: null } }, null); return; }
          if (act === 'all') { // loads this page's value into the side panel's draft
            const view = this.currentView(m.pid);
            const at = view && view.lines[kind];
            if (!at) return;
            this.ctl[kind] = { on: true, pct: pct1(at.y) };
            fromPage = m.pid;
            this.onControl();
          }
        },

        // ---------------------------------------------------------- Esc, errors, the toast
        // Esc in the mode, the top layer first (§3.12): a move in progress or not saved, the band menu, the page's
        // unsaved change (a started book), then the draft.
        escape() {
          if (this.drag) { const pid = this.drag.pid; this.endDrag(); this.dash.rerenderGuides(pid); return true; }
          if (keyMove) { const pid = keyMove.pid; clearTimeout(keyMove.timer); keyMove = null; this.dash.rerenderGuides(pid); return true; }
          if (this.closeMenu()) return true;
          if (this.pageId && this.cancelPending(this.pageId)) return true;
          return this.dropDraft();
        },
        failed(err, quiet) {
          if (err && err.status === 409 && err.started) { if (this.dash) this.dash.startedElsewhere = true; return; }
          this.error = (err && err.message) || 'تعذّر تنفيذ الطلب. حاول مرة أخرى.';
          if (!quiet) this.showToast(this.error, null);
        },
        // One toast at a time; «تراجع» is bound to the boolean `hasAction` and never runs by itself (2a0bca2).
        showToast(message, action) {
          clearTimeout(toastTimer);
          toastAction = typeof action === 'function' ? action : null;
          this.toast = { visible: true, message, hasAction: toastAction !== null };
          this.live = message;
          toastTimer = setTimeout(() => this.hideToast(), toastAction ? TOAST_UNDO_MS : TOAST_MS);
        },
        hideToast() {
          clearTimeout(toastTimer);
          toastAction = null;
          this.toast = { visible: false, message: this.toast.message, hasAction: false };
        },
        runToastAction() {
          const action = toastAction;
          this.hideToast();
          if (action) action();
        },
      };
    });

    // ------------------------------------------------------------ preprocess panel
    Alpine.data('preprocessPanel', (cfg) => ({
      state: cfg,
      angle: 0,
      crop: [0, 0, 0, 0],
      k: 0.2,
      window: 41,
      nlm: 6,
      busy: false,
      error: '',
      notice: '',

      init() { this.load(cfg); },

      load(s) {
        this.state = s;
        if (!s.has_preprocess) return;
        const p = s.params || {};
        this.angle = Number(p.angle) || 0;
        this.crop = Array.isArray(p.crop_box) && p.crop_box.length === 4
          ? p.crop_box.map(Number)
          : [0, 0, s.frame.width, s.frame.height];
        this.k = Number(p.sauvola_k) || 0.2;
        this.window = Number(p.sauvola_window) || 41;
        this.nlm = p.nlm_h == null ? 6 : Number(p.nlm_h);
      },

      get autoAngle() {
        const a = this.state.auto_params && this.state.auto_params.angle;
        return a == null ? null : Number(a);
      },
      get autoCropBox() {
        const box = this.state.auto_params && this.state.auto_params.crop_box;
        return Array.isArray(box) && box.length === 4 ? box : null;
      },

      fullCrop() { this.crop = [0, 0, this.state.frame.width, this.state.frame.height]; },
      autoCrop() { if (this.autoCropBox) this.crop = this.autoCropBox.map(Number); },

      // Only the values that differ from what the pipeline chose automatically are sent as overrides:
      // posting the stored auto crop/angle back would pin them as manual (the crop would no longer follow
      // a new angle, edge-strip removal and the deskew-confidence flag would be skipped).
      manualOverrides() {
        const auto = this.state.auto_params || {};
        const differs = (value, autoValue, eps) => autoValue == null || Math.abs(Number(value) - Number(autoValue)) > eps;
        const out = {};
        if (differs(this.angle, auto.angle, 0.05)) out.angle = this.angle;
        const box = this.autoCropBox;
        if (!box || this.crop.some((v, i) => Math.round(Number(v)) !== Math.round(Number(box[i])))) out.crop_box = this.crop;
        if (differs(this.k, auto.sauvola_k, 0.0005)) out.sauvola_k = this.k;
        if (differs(this.window, auto.sauvola_window, 0)) out.sauvola_window = this.window;
        if (differs(this.nlm, auto.nlm_h, 0)) out.nlm_h = this.nlm;
        return out;
      },
      rerun() {
        const overrides = this.manualOverrides();
        return this.post(Object.keys(overrides).length ? overrides : { reset: true });
      },
      reset() { return this.post({ reset: true }); },

      // In «التخطيط» (the book awaits «بدء المعالجة») no text is read yet, so the notice says nothing about OCR.
      get doneNotice() {
        return this.state.layout_stage || cfg.layoutStage
          ? 'جُهّزت الصفحة من جديد.'
          : 'جُهّزت الصفحة من جديد. أعد تشغيل التعرّف على النص من قائمة إعادة التشغيل إن أردت تحديث النص.';
      },

      async post(body) {
        this.busy = true;
        this.error = '';
        this.notice = '';
        try {
          const data = await postJson(this.state.api_url, body);
          if (data.queued) {
            this.notice = data.detail || 'أُرسل تجهيز الصفحة إلى العامل الخلفي؛ حدّث الصفحة بعد قليل.';
            return;
          }
          data.api_url = this.state.api_url;
          data.guides_url = this.state.guides_url;
          data.guides = this.state.guides;
          data.override = this.state.override;
          data.layout_stage = this.state.layout_stage;
          this.load(data);
          this.swapImages(data);
          this.notice = this.doneNotice;
        } catch (err) {
          this.error = err.message;
        } finally {
          this.busy = false;
        }
      },

      swapImages(data) {
        const images = data.images || {};
        document.querySelectorAll('[data-page-image]').forEach((el) => {
          const url = images[el.dataset.pageImage];
          if (url) el.src = url;
        });
        window.dispatchEvent(new CustomEvent('nassakh:page-updated', {
          detail: {
            pageId: data.page_id,
            images: images,
            output: data.output,
            regions: data.regions || [],
            status: data.status,
            statusLabel: data.status_label,
            flags: data.flags || [],
          },
        }));
      },
    }));
  });
})();
