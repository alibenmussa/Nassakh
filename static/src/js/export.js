// The export page «الإخراج» (PHASE6_SPEC §8, D59): the readiness card, one card per format, the history card.
//   exportPage(config) – config is the §3.2 payload (`publishing.exports.page_payload`, the same object as
//                        GET `api:exports`):
//     - every format the payload lists is drawn from its `form`: a field with `choices` is a segmented control
//       (its chosen choice's hint under it), a boolean one a checkbox (its count, disabled at 0, its help); the
//       labels, hints and counts come from the payload, never from here (only a missing field label falls back)
//     - each format has one state row (never exported, done, stale, queued, running, error); a cancelled
//       export brings the previous file back
//     - start → 202 the new row (409: the export already running, shown with «إلغاء»); the row is long-polled
//       (`api:export?wait=4&since=<updated_at>`, given up after 14 s: a hung connection is a failed poll) until
//       it is final, then the page payload is fetched once (a refresh asked for while one is on the wire runs
//       once more after it); failures back off 2 → 10 s and only a poll that succeeds clears the back-off and
//       the pill; nothing is polled while the tab is hidden or when nothing runs; leaving the page never stops
//       an export
//     - answers can arrive out of order (a poll read before a cancel, a payload read before an export ended):
//       the page keeps the newest version of every row it saw (`updated_at`), and a row it saw final never
//       comes back as running
//     - cancel; the live region and the focus of §8.3 (after a start, a 409 and a cancel); the menu's ↑/↓ and Esc
//   exportBar          – the top bar (base.html header_actions, outside the root): the poll pills and the menu,
//                        reading Alpine.store('exportPage').view
// Pure helpers are on window.NassakhExport for the tests. Western digits everywhere; the relative times and the
// Arabic counts are the manuscript page's (NassakhManuscript.relativeTime / arCount).
(function () {
  'use strict';

  const root = typeof window !== 'undefined' ? window : globalThis;
  const hasDOM = typeof document !== 'undefined' && typeof document.querySelector === 'function';
  const ACTIVE = ['queued', 'running'];
  const WAIT_S = 4; // the long poll's wait (the API allows up to 5)
  const POLL_TIMEOUT_MS = (WAIT_S + 10) * 1000; // a long poll with no answer by then is a failed poll
  const MIN_GAP_MS = 400; // between two polls of one row, whatever the server answered
  const RETRY_MS = 2000; // a failed poll waits 2, 4, 6, 8, then 10 s (= stage.js's back-off, capped at 10 s)
  const RETRY_MAX_MS = 10000;
  const FAILURES_BEFORE_NOTICE = 3;
  const TICK_MS = 30000; // «قبل 5 دقائق» refreshes
  const REFRESH_AFTER_HIDDEN_MS = 10000; // back on the tab after a while: the payload once (a fix made elsewhere)
  const PAGES = ['صفحة واحدة', 'صفحتان', 'صفحات', 'صفحة'];
  const CHAPTERS = ['فصل واحد', 'فصلان', 'فصول', 'فصلًا'];
  const POINTS = ['نقطة واحدة', 'نقطتان', 'نقاط', 'نقطة'];
  // A field whose payload carries no label of its own (the Word kashida of §3.2).
  const FIELD_LABELS = { kashida: 'الكشيدة' };
  const LEVEL_DOT = { warn: 'dot-warning', info: 'dot-neutral', success: 'dot-success' };
  const LEVEL_ICON = { warn: 'i-alert', info: 'i-info', success: 'i-check-circle' };
  // What the page draws for a format key (the payload carries its label and extension, not its look): a generic
  // stroke icon of base.html's sprite and the one-line purpose under its name.
  const FORMAT_ICON = { docx: 'i-doc', print_pdf: 'i-printer', screen_pdf: 'i-monitor', epub: 'i-book-open' };
  const FORMAT_PURPOSE = { docx: 'للتحرير والمراجعة', print_pdf: 'للمطبعة', screen_pdf: 'للقراءة على الشاشة', epub: 'للقارئات الإلكترونية' };
  const NOTES = ['ملاحظة واحدة', 'ملاحظتان', 'ملاحظات', 'ملاحظة'];
  const FILES = ['ملف واحد', 'ملفان', 'ملفات', 'ملفًا'];

  // ---------------------------------------------------------------- pure helpers (exported for the tests)
  const M = () => root.NassakhManuscript || {};
  const count = (n, forms) => (M().arCount ? M().arCount(n, forms) : `${n} ${forms[3]}`);
  const ago = (iso, now) => (M().relativeTime ? M().relativeTime(iso, now) : String(iso || '').slice(0, 10));
  const fill = (template, id) => String(template || '').replace('__eid__', String(id));
  const isActive = (row) => Boolean(row && ACTIVE.includes(row.status));
  const backoff = (failures) => Math.min(RETRY_MS * Math.max(1, failures), RETRY_MAX_MS);
  const madeAt = (row) => (row ? row.finished_at || row.created_at || '' : '');
  // «2026-09-26 10:02» (the `title` of a relative time)
  const absTime = (iso) => String(iso || '').slice(0, 16).replace('T', ' ');
  const levelDot = (level) => LEVEL_DOT[level] || 'dot-neutral';
  const levelIcon = (level) => LEVEL_ICON[level] || 'i-info';
  const formatIcon = (key) => FORMAT_ICON[key] || 'i-doc';
  const formatPurpose = (key) => FORMAT_PURPOSE[key] || '';

  // «17×24 سم · 85 صفحة · 6 فصول · Simplified Arabic 13 نقطة»; it ends «لم تُرتَّب الصفحات بعد» with no layout
  // and «يُحسب…» while one renders.
  function metaLine(layout) {
    if (!layout) return '';
    const counted = layout.state !== 'none' && layout.state !== 'rendering' && Number(layout.page_count) > 0;
    const parts = [layout.trim_label];
    if (counted) parts.push(count(layout.page_count, PAGES));
    if (layout.chapters) parts.push(count(layout.chapters, CHAPTERS));
    if (layout.body_font) parts.push(`${layout.body_font} ${count(layout.body_size_pt, POINTS)}`);
    if (layout.state === 'rendering') parts.push('يُحسب…');
    else if (!counted) parts.push('لم تُرتَّب الصفحات بعد');
    return parts.filter(Boolean).join(' · ');
  }

  // The fields of a format's form: `choice` (a segmented control), `bool` (a checkbox) or `value` (kept and sent,
  // not drawn: a form block without choices or labels).
  function fieldsOf(f) {
    const form = (f && f.form) || {};
    return Object.keys(form).map((key) => {
      const fd = form[key] || {};
      const choices = Array.isArray(fd.choices) ? fd.choices : [];
      const kind = choices.length ? 'choice' : (typeof fd.value === 'boolean' ? 'bool' : 'value');
      const available = typeof fd.available === 'number' ? fd.available : null;
      return { key, kind, label: fd.label || FIELD_LABELS[key] || '', choices, hint: fd.hint || '', available, disabled: available === 0 };
    });
  }
  // The values a format's form starts from: the payload's (the last options used, or the defaults).
  function initialValues(f) {
    const values = {};
    fieldsOf(f).forEach((fd) => {
      const raw = f.form[fd.key] ? f.form[fd.key].value : undefined;
      values[fd.key] = fd.kind === 'bool' ? Boolean(raw) && !fd.disabled : raw;
    });
    return values;
  }

  // After the file name: «318 KB», «قبل ساعتين» (a PDF adds its exact page count), «تغيّر النص بعد هذا الإخراج» when stale.
  function fileParts(row, now) {
    return [row.page_count ? count(row.page_count, PAGES) : '', row.size_text, ago(madeAt(row), now), row.stale_text].filter(Boolean);
  }
  const stepText = (row) => `${(row.progress && row.progress.label) || row.status_label || ''}…`;

  // The state row of a format (§8.3): {kind, row, dot, name, parts, text, title, percent, hint}. `name` is the file
  // name (truncated with an ellipsis), `parts` the rest (each drawn in its own <bdi>, «318 KB» stays one run),
  // `text` the parts joined; `dot` the dot's classes ('' draws none).
  function stateOf(f, now) {
    const s = stateBase(f, now);
    s.text = s.parts.join(' · ');
    return s;
  }
  function stateBase(f, now) {
    const active = f && f.active;
    const base = { kind: 'never', row: null, dot: '', name: '', parts: [], text: '', title: '', percent: null, hint: '' };
    if (isActive(active)) {
      const queued = active.status === 'queued';
      const p = active.progress || {};
      return Object.assign(base, {
        kind: active.status,
        row: active,
        dot: 'dot-accent is-live',
        parts: [queued ? 'في الانتظار…' : stepText(active)],
        percent: !queued && typeof p.percent === 'number' ? p.percent : null,
        hint: active.waiting_hint || '',
      });
    }
    const last = f && f.latest;
    if (!last) return base;
    if (last.status === 'error') {
      return Object.assign(base, { kind: 'error', row: last, dot: 'dot-danger', parts: [last.error ? `تعذّر الإخراج: ${last.error}` : 'تعذّر الإخراج'], title: absTime(madeAt(last)) });
    }
    return Object.assign(base, {
      kind: last.stale ? 'stale' : 'done',
      row: last,
      dot: last.stale ? 'dot-warning' : 'dot-success',
      name: last.filename || '',
      parts: fileParts(last, now),
      title: absTime(madeAt(last)),
    });
  }
  // The warnings shown under a finished file: its `warn` ones.
  function fileWarnings(row) {
    return row && Array.isArray(row.warnings) ? row.warnings.filter((w) => w && w.level === 'warn') : [];
  }
  // The format's notes, without those its shown file already carries (the same row is not said twice).
  function notesOf(f, now) {
    const s = stateOf(f, now);
    const shown = s.kind === 'done' || s.kind === 'stale' ? fileWarnings(s.row).map((w) => w.code) : [];
    return ((f && f.notes) || []).filter((n) => n && !shown.includes(n.code));
  }

  // The history (§8.3 «السجل»): line 1 as parts (each in its own <bdi>: «Word», «كتابي.docx», «318 KB»), line 2
  // after the relative time, the dot.
  function historyParts(row) {
    const head = row.format_label || row.format;
    if (row.status === 'done') return [head, row.filename, row.page_count ? count(row.page_count, PAGES) : '', row.size_text].filter(Boolean);
    if (isActive(row)) return [head, 'قيد الإخراج', (row.progress && row.progress.label) || row.status_label];
    if (row.status === 'error') return [head, 'تعذّر الإخراج'];
    return [head, row.status_label || 'أُلغي'];
  }
  const historyTitle = (row) => historyParts(row).join(' · ');
  function historyRest(row) {
    return [row.created_by, row.options_text, row.status === 'done' ? row.stale_text : ''].filter(Boolean).map((part) => ` · ${part}`).join('');
  }
  function historyDot(row) {
    if (row.status === 'done') return row.stale ? 'dot-warning' : 'dot-success';
    if (isActive(row)) return 'dot-accent is-live';
    return row.status === 'error' ? 'dot-danger' : 'dot-neutral';
  }

  // A row into the history: replaced in place, or the newest first (at most 30, as the server lists them).
  function upsert(items, row) {
    const i = items.findIndex((item) => item.id === row.id);
    if (i >= 0) items.splice(i, 1, row);
    else { items.unshift(row); if (items.length > 30) items.length = 30; }
    return items;
  }

  // True when `row` is an older version of the export `had` is (a stale answer). An export only moves on
  // (queued → running → done / error / cancelled): a row seen running never comes back as queued, one seen
  // final never comes back as queued or running; within one stage an older `updated_at` never replaces a
  // newer one.
  const stamp = (row) => Date.parse((row && row.updated_at) || '');
  const stage = (row) => (row.status === 'queued' ? 0 : row.status === 'running' ? 1 : 2);
  function staler(row, had) {
    if (!row || !had || had.id !== row.id) return false;
    if (stage(row) !== stage(had)) return stage(row) < stage(had);
    const a = stamp(row);
    const b = stamp(had);
    return Number.isFinite(a) && Number.isFinite(b) && a < b;
  }

  // ↑/↓ inside a menu: the next enabled item (wrapping).
  function moveIn(menu, dir) {
    if (!menu || typeof menu.querySelectorAll !== 'function') return null;
    const items = Array.from(menu.querySelectorAll('.menu-item')).filter((el) => !el.disabled && el.getAttribute('aria-disabled') !== 'true');
    if (!items.length) return null;
    const at = items.indexOf(hasDOM ? document.activeElement : null);
    const next = items[(at + dir + items.length) % items.length] || items[0];
    if (next && typeof next.focus === 'function') next.focus();
    return next;
  }

  const hidden = () => Boolean(hasDOM && document.hidden);
  const isRtl = () => { try { return !(document.documentElement && document.documentElement.dir === 'ltr'); } catch (_) { return true; } };
  const csrfToken = () => {
    const meta = hasDOM ? document.querySelector('meta[name="csrf-token"]') : null;
    return (meta && meta.content) || '';
  };
  const store = () => (typeof Alpine !== 'undefined' && typeof Alpine.store === 'function' ? Alpine.store('exportPage') : null);

  // fetch wrapper: never throws; `{ ok, status, data, message }` with an Arabic message on failure. With
  // `timeout` (ms) the request is given up after that long (a hung connection answers as a lost one).
  async function api(url, opts = {}) {
    const method = opts.method || 'GET';
    const init = { method, credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } };
    if (method !== 'GET') {
      init.headers['Content-Type'] = 'application/json';
      init.headers['X-CSRFToken'] = csrfToken();
      init.body = JSON.stringify(opts.body || {});
    }
    let timer = null;
    if (opts.timeout && typeof AbortController === 'function') {
      const controller = new AbortController();
      init.signal = controller.signal;
      timer = setTimeout(() => controller.abort(), opts.timeout);
    }
    const settle = () => { if (timer !== null) { clearTimeout(timer); timer = null; } };
    let response;
    try {
      response = await fetch(url, init);
    } catch (_) {
      settle();
      return { ok: false, status: 0, data: null, message: 'انقطع الاتصال بالخادم. تحقّق من الشبكة ثم أعد المحاولة.' };
    }
    let data = null;
    try { data = await response.json(); } catch (_) { data = null; }
    settle();
    let message = data && typeof data === 'object' && (data.detail || data.message);
    if (!message) {
      if (response.status === 401) message = 'انتهت الجلسة. سجّل الدخول من جديد.';
      else if (response.status === 403) message = 'لا تملك صلاحية هذا الإجراء.';
      else message = 'تعذّر تنفيذ الطلب. حاول مرة أخرى.';
    }
    return { ok: response.ok, status: response.status, data, message };
  }

  // ---------------------------------------------------------------- the component
  function exportPage(cfg = {}) {
    const formats = (cfg.formats || []).map((f) => Object.assign({}, f));
    const values = {};
    const errors = {};
    const busy = {};
    formats.forEach((f) => { values[f.key] = initialValues(f); errors[f.key] = null; busy[f.key] = ''; });
    const timers = {}; // format → the next poll of its active row
    const inflight = {}; // format → a poll on the wire
    // export id → the newest version of its row this page has seen (answers may arrive out of order)
    const seen = {};
    const remember = (row) => { if (row && row.id != null && !staler(row, seen[row.id])) seen[row.id] = row; };
    formats.forEach((f) => { remember(f.latest); remember(f.active); });
    (cfg.items || []).forEach(remember);
    let tick = null;
    let hiddenAt = 0;

    return {
      book: cfg.book || {},
      canEdit: Boolean(cfg.can_edit),
      readiness: cfg.readiness || [],
      formats,
      items: (cfg.items || []).slice(),
      urls: cfg.urls || {},
      values,
      errors, // format → {detail, fields: {option: message}} of the last request, or null
      busy, // format → 'start' | 'cancel' | ''
      fresh: {}, // format → the id of a file finished while this page was open (its dot blooms in once)
      live: '',
      pollState: 'ok', // ok | error | auth (the top bar's bk-poll pills)
      failures: 0,
      refreshing: false,
      refreshAgain: false, // a refresh was asked for while one was on the wire: it runs once more
      stopped: false,
      now: Date.now(),

      init() {
        const s = store();
        if (s) s.view = this;
        this.formats.forEach((f) => { if (isActive(f.active)) this.schedule(f.key, 0); });
        tick = setInterval(() => { this.now = Date.now(); }, TICK_MS);
      },
      destroy() {
        Object.keys(timers).forEach((key) => clearTimeout(timers[key]));
        clearInterval(tick);
        const s = store();
        if (s && s.view === this) s.view = null;
      },

      // ------------------------------------------------------------ reading the payload
      fmt(key) { return this.formats.find((f) => f.key === key) || null; },
      get metaText() { return metaLine(this.book.layout); },
      get warnCount() { return this.readiness.filter((r) => r.level === 'warn').length; },
      get availableCount() { return this.formats.filter((f) => f.available).length; },
      // true while this is the only available format (the 6a payload); every card's «إخراج …» is drawn primary
      // now, so the template no longer reads it
      isPrimary(f) { return Boolean(f.available) && this.availableCount === 1; },
      levelDot,
      levelIcon,
      formatIcon,
      formatPurpose,
      // the readiness head: the rows that are notes (every level but the all-clear one) as «3 ملاحظات», and its
      // tone: warn while a warning is left, info with notes only, success when the book is clear
      get noteCount() { return this.readiness.filter((r) => r.level !== 'success').length; },
      get noteText() { return this.noteCount ? count(this.noteCount, NOTES) : ''; },
      get readyLevel() { return this.warnCount ? 'warn' : this.noteCount ? 'info' : 'success'; },
      // a clear book: the all-clear row's message («كل الصفحات مُراجَعة ولا ملاحظات؛ …») is the head's line, no rows
      get clearText() {
        if (this.noteCount) return '';
        const clear = this.readiness.find((r) => r.level === 'success');
        return clear ? clear.message : '';
      },
      get itemsText() { return this.items.length ? count(this.items.length, FILES) : ''; },
      // the card's foot: the running strip stands in for «إخراج …» while an export runs; the actions row shows
      // when there is a button to press or a file to download
      isRunning(f) { return isActive(f && f.active); },
      hasFile(s) { return Boolean(s && (s.kind === 'done' || s.kind === 'stale') && s.row && s.row.download_url); },
      hasActions(f) { return (this.canEdit && !this.isRunning(f)) || this.hasFile(this.stateOf(f)); },
      // the head's meta after the extension (which sits in its own LTR <bdi>): « · غير متاحة بعد», « · لم يُخرَج بعد»
      headNote(f) {
        if (!f.available) return ' · غير متاحة بعد';
        return isActive(f.active) || f.latest ? '' : ' · لم يُخرَج بعد';
      },
      fieldsOf,
      stateOf(f) { return stateOf(f, this.now); },
      stateKey(s) { return `${s.kind}:${s.row ? s.row.id : ''}`; },
      // «إلغاء»'s key: the export it cancels, so queued → running keeps the same button (and its focus)
      cancelKey(s) { return s.kind === 'queued' || s.kind === 'running' ? `active:${s.row.id}` : this.stateKey(s); },
      notesOf(f) { return notesOf(f, this.now); },
      fileWarnings(s) { return s && (s.kind === 'done' || s.kind === 'stale') ? fileWarnings(s.row) : []; },
      isFresh(f, s) { return Boolean(s && s.row && s.kind === 'done' && this.fresh[f.key] === s.row.id); },
      goLabel(f) { return `إخراج ${f.label}`; },
      canStart(f) { return Boolean(this.canEdit && f.available && !isActive(f.active) && !this.busy[f.key] && !this.stopped); },
      errorOf(f) { const e = this.errors[f.key]; return (e && e.detail) || ''; },
      fieldError(f, fd) { const e = this.errors[f.key]; return (e && e.fields && e.fields[fd.key]) || ''; },
      historyParts,
      historyTitle,
      historyDot,
      historyRest,
      historyWhen(row) { return ago(madeAt(row), this.now); },
      absTime(row) { return absTime(madeAt(row)); },

      // ------------------------------------------------------------ the form
      // (not `valueOf`: Alpine's scope would find Object.prototype's first)
      fieldValue(f, fd) { return (this.values[f.key] || {})[fd.key]; },
      setValue(f, fd, value) {
        if (!this.values[f.key]) this.values[f.key] = {};
        this.values[f.key][fd.key] = value;
        const e = this.errors[f.key];
        if (e && e.fields && e.fields[fd.key]) this.errors[f.key] = null;
      },
      picked(f, fd, c) { return this.fieldValue(f, fd) === c.value; },
      pick(f, fd, c) { this.setValue(f, fd, c.value); },
      // one Tab stop per control: the chosen segment (the first when none is)
      segTab(f, fd, i) {
        const at = fd.choices.findIndex((c) => this.picked(f, fd, c));
        return i === (at < 0 ? 0 : at) ? 0 : -1;
      },
      choiceHint(f, fd) {
        const c = fd.choices.find((choice) => choice.value === this.fieldValue(f, fd));
        return (c && c.hint) || fd.hint || '';
      },
      // ←/→ move the choice (RTL: the next choice is on the left), Home/End the first/last; Space and Enter are
      // the buttons' own click.
      onSegKey(ev, f, fd, i) {
        const n = fd.choices.length;
        if (!n) return false;
        const forward = isRtl() ? 'ArrowLeft' : 'ArrowRight';
        const back = isRtl() ? 'ArrowRight' : 'ArrowLeft';
        let next = -1;
        if (ev.key === forward) next = (i + 1) % n;
        else if (ev.key === back) next = (i - 1 + n) % n;
        else if (ev.key === 'Home') next = 0;
        else if (ev.key === 'End') next = n - 1;
        if (next < 0) return false;
        if (ev.preventDefault) ev.preventDefault();
        this.pick(f, fd, fd.choices[next]);
        const group = ev.target && ev.target.parentElement;
        const buttons = group && group.querySelectorAll ? group.querySelectorAll('button') : [];
        if (buttons[next] && typeof buttons[next].focus === 'function') buttons[next].focus();
        return true;
      },
      optionsOf(key) { return Object.assign({}, this.values[key] || {}); },

      // ------------------------------------------------------------ start, cancel
      async start(key) {
        const f = this.fmt(key);
        if (!f || !this.canStart(f)) return false;
        this.busy[key] = 'start';
        this.errors[key] = null;
        const r = await api(this.urls.create, { method: 'POST', body: { format: key, options: this.optionsOf(key) } });
        this.busy[key] = '';
        if (r.ok && r.data && r.data.id) {
          this.applyRow(r.data);
          this.focusIn(`[data-ex-status="${key}"]`);
          if (!isActive(r.data)) { this.finished(f, r.data); return false; } // e.g. the queue was unreachable
          this.say(`بدأ إخراج ${f.label}`);
          this.schedule(key, 0);
          return true;
        }
        if (r.status === 409 && r.data && r.data.active) {
          // already running (another tab, a double click): that export is shown, with «إلغاء»; the focus goes
          // to its status (the pressed «إخراج …» gives way to the running strip)
          this.errors[key] = { detail: r.message, fields: {} };
          this.applyRow(r.data.active);
          this.focusIn(`[data-ex-status="${key}"]`);
          this.schedule(key, 0);
          return false;
        }
        if (r.status === 401) { this.pollState = 'auth'; this.stopped = true; }
        // the card's alert (role="alert") reads the message out: not the live region too
        this.errors[key] = { detail: r.message, fields: (r.data && r.data.errors) || {} };
        return false;
      },
      async cancel(key) {
        const f = this.fmt(key);
        const row = f && f.active;
        if (!row || !isActive(row) || !this.canEdit || this.busy[key]) return false;
        this.busy[key] = 'cancel';
        const r = await api(fill(this.urls.cancel, row.id), { method: 'POST', body: {} });
        this.busy[key] = '';
        // the focused «إلغاء» goes with the running strip: the focus moves to «إخراج …» (else the status)
        const refocus = () => this.focusIn(`[data-ex-go="${key}"]`, `[data-ex-status="${key}"]`);
        if (r.ok && r.data) { this.errors[key] = null; this.applyRow(r.data); refocus(); return true; }
        if (r.status === 409 && r.data && r.data.row) {
          // it finished meanwhile: the finished row is shown, and why nothing was cancelled
          this.applyRow(r.data.row);
          this.errors[key] = { detail: r.message, fields: {} };
          refocus();
          return false;
        }
        this.errors[key] = { detail: r.message, fields: {} };
        return false;
      },

      // ------------------------------------------------------------ rows and the long poll
      // A row from the server (POST, a poll, a cancel): the format's active or latest row, and the history. An
      // answer older than what the page already shows of that export is left out (false).
      applyRow(row) {
        const f = row && this.fmt(row.format);
        if (!f || staler(row, seen[row.id])) return false;
        seen[row.id] = row;
        const was = f.active;
        if (isActive(row)) {
          if (!isActive(was) || was.id <= row.id) f.active = row; // one export of a format runs at a time
        } else {
          if (was && was.id === row.id) f.active = null;
          if ((row.status === 'done' || row.status === 'error') && (!f.latest || f.latest.id <= row.id)) f.latest = row;
        }
        upsert(this.items, row);
        if (was && was.id === row.id && !isActive(row)) this.finished(f, row);
        return true;
      },
      // the newest version of a row the page has: `row`, unless the page already saw a newer one
      newest(row) {
        if (!row || row.id == null) return row;
        if (staler(row, seen[row.id])) return seen[row.id];
        seen[row.id] = row;
        return row;
      },
      finished(f, row) {
        clearTimeout(timers[f.key]);
        this.errors[f.key] = null; // a 409 about this export is over with it
        if (row.status === 'done') {
          this.fresh = Object.assign({}, this.fresh, { [f.key]: row.id });
          this.say([`اكتمل ملف ${f.label}`, row.size_text].filter(Boolean).join(' · '));
          // focus moves to «تنزيل» only when it was inside this format's block
          const block = this.q(`[data-ex-format="${f.key}"]`);
          const inside = block && hasDOM && typeof block.contains === 'function' && block.contains(document.activeElement);
          if (inside) this.focusIn(`[data-ex-download="${f.key}"]`);
        } else if (row.status === 'error') {
          this.say(`تعذّر إخراج ${f.label}`);
        } else {
          this.say(`أُلغي إخراج ${f.label}`);
        }
        this.refresh();
      },
      schedule(key, ms) {
        clearTimeout(timers[key]);
        timers[key] = setTimeout(() => this.poll(key), Math.max(0, ms || 0));
      },
      async poll(key) {
        clearTimeout(timers[key]);
        const f = this.fmt(key);
        const row = f && f.active;
        if (this.stopped || !isActive(row) || inflight[key]) return false;
        if (hidden()) return false; // onVisible() resumes it
        inflight[key] = true;
        const started = Date.now();
        const url = `${fill(this.urls.row, row.id)}?wait=${WAIT_S}&since=${encodeURIComponent(row.updated_at || '')}`;
        const r = await api(url, { timeout: POLL_TIMEOUT_MS });
        inflight[key] = false;
        if (r.status === 401 || r.status === 403) { this.stopped = true; this.pollState = 'auth'; return false; }
        if (r.status === 404) {
          // the row is gone: the page is read again; the server answered, so the trouble is over
          this.failures = 0;
          this.pollState = 'ok';
          if (f.active && f.active.id === row.id) f.active = null;
          this.refresh();
          return false;
        }
        if (!r.ok || !r.data) {
          this.failures += 1;
          if (this.failures >= FAILURES_BEFORE_NOTICE) this.pollState = 'error';
          this.schedule(key, backoff(this.failures));
          return false;
        }
        this.failures = 0;
        this.pollState = 'ok';
        this.applyRow(r.data); // (a stale answer changes nothing: the poll goes on from the newer row)
        const now = f.active;
        if (isActive(now)) this.schedule(key, now.id === row.id ? MIN_GAP_MS - (Date.now() - started) : 0);
        return true;
      },
      // the bk-poll pill's click: every running export at once. The pill (and the back-off) stay until a poll
      // succeeds.
      pollNow() {
        return Promise.all(this.formats.filter((f) => isActive(f.active)).map((f) => this.poll(f.key)));
      },
      // The page payload once (after an export ended, back on the tab, a page restored from the cache): the
      // book, the readiness, each format's form, notes and rows, the history. The options being chosen stay. A
      // refresh asked for while one is on the wire is not dropped: it runs once more when that one answers (the
      // first may have been read before the second export ended).
      async refresh() {
        if (!this.urls.create || this.stopped) return false;
        if (this.refreshing) { this.refreshAgain = true; return false; }
        this.refreshing = true;
        let ok = false;
        try {
          do {
            this.refreshAgain = false;
            const r = await api(this.urls.create);
            if (r.status === 401 || r.status === 403) { this.stopped = true; this.pollState = 'auth'; break; }
            if (r.ok && r.data) { this.adopt(r.data); ok = true; }
          } while (this.refreshAgain && !this.stopped);
        } finally {
          this.refreshing = false;
        }
        return ok;
      },
      adopt(data) {
        if (data.book) this.book = data.book;
        if (Array.isArray(data.readiness)) this.readiness = data.readiness;
        // the history as the server lists it, each row at its newest version this page has seen
        const items = Array.isArray(data.items) ? data.items.map((item) => this.newest(item)) : this.items;
        (data.formats || []).forEach((next) => {
          const f = this.fmt(next.key);
          if (!f) return;
          f.available = next.available;
          f.form = next.form || {};
          f.notes = next.notes || [];
          const values = initialValues(f);
          fieldsOf(f).forEach((fd) => {
            const mine = this.values[f.key] || (this.values[f.key] = {});
            if (!(fd.key in mine) || (fd.kind === 'bool' && fd.disabled)) mine[fd.key] = values[fd.key];
          });
          const latest = this.newest(next.latest);
          if (latest && !isActive(latest) && (!f.latest || f.latest.id <= latest.id)) f.latest = latest;
          // an export this page saw end is never taken back as running (a payload read before it ended)
          const active = next.active && !staler(next.active, seen[next.active.id]) ? next.active : null;
          const polling = isActive(f.active);
          if (isActive(active) && (!polling || f.active.id <= active.id)) {
            seen[active.id] = active;
            f.active = active;
            if (!polling) this.schedule(f.key, 0);
          }
          // an export started after this payload was read stays in the history
          if (isActive(f.active) && !items.some((item) => item.id === f.active.id)) upsert(items, f.active);
        });
        this.items = items;
      },
      onVisible() {
        if (hidden()) { hiddenAt = Date.now(); return false; }
        const away = hiddenAt ? Date.now() - hiddenAt : 0;
        hiddenAt = 0;
        this.formats.forEach((f) => { if (isActive(f.active)) this.poll(f.key); });
        if (away > REFRESH_AFTER_HIDDEN_MS) this.refresh();
        return true;
      },
      onPageShow(ev) {
        if (ev && ev.persisted) { this.refresh(); this.formats.forEach((f) => { if (isActive(f.active)) this.poll(f.key); }); }
      },

      // ------------------------------------------------------------ the live region, focus
      say(message) { this.live = message; },
      q(sel) {
        const host = this.$root && typeof this.$root.querySelector === 'function' ? this.$root : (hasDOM ? document : null);
        return host ? host.querySelector(sel) : null;
      },
      // the focus to the first of `selectors` that can take it (there, enabled, shown), after Alpine's update
      focusIn(...selectors) {
        const run = () => {
          for (const sel of selectors) {
            const el = this.q(sel);
            const shown = !el || typeof el.getClientRects !== 'function' || el.getClientRects().length > 0;
            if (el && typeof el.focus === 'function' && !el.disabled && shown) { el.focus({ preventScroll: false }); return; }
          }
        };
        if (typeof this.$nextTick === 'function') this.$nextTick(run); else run();
      },
    };
  }

  root.NassakhExport = Object.assign(root.NassakhExport || {}, {
    metaLine, fieldsOf, initialValues, stateOf, notesOf, fileWarnings, historyParts, historyTitle, historyRest, historyDot,
    upsert, staler, backoff, fill, absTime, moveIn, levelIcon, formatIcon, formatPurpose, exportPage,
  });

  document.addEventListener('alpine:init', () => {
    if (typeof Alpine.store === 'function') Alpine.store('exportPage', { view: null });
    Alpine.data('exportPage', exportPage);
    Alpine.data('exportBar', () => ({
      open: false,
      get v() { const s = store(); return s ? s.view : null; },
      moveIn,
    }));
  });
})();
