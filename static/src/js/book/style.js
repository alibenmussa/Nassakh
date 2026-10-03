// The book page's stylesheet («التنسيق», PHASE5_SPEC §5, §9.2): the accordions الغلاف (D80, cover.js)، القطع،
// الهوامش، الخطوط، النص، الصفحة، بيانات الكتاب. Every change shows at once and saves after a short quiet (one
// PUT of the waiting fields); the server's values win. A saved change lays the book out again (the page setup
// changed: the re-layout of the chapter under the reader's eyes lays out the whole book, in a second or so for
// most books) and the book render follows in the background. A change of the cover alone lays nothing out
// again (the cover is its own page, outside the interior): only its render is fetched again.
(function () {
  'use strict';

  const root = typeof window !== 'undefined' ? window : globalThis;
  const NS = (root.NassakhBook = root.NassakhBook || {});
  NS.parts = NS.parts || {};

  const SAVE_DEBOUNCE_MS = 400; // stepper clicks coalesce into one PUT
  const TEXT_DEBOUNCE_MS = 900; // a book detail typed letter by letter
  const SAVED_PILL_MS = 2000;
  const STEPS = {
    width_mm: 1, height_mm: 1, top_mm: 1, bottom_mm: 1, inner_mm: 1, outer_mm: 1, bleed_mm: 0.5,
    body_size_pt: 0.5, line_height: 0.05, indent_em: 0.25, footnote_size_pt: 0.5,
    'heading_scale.h1': 0.05, 'heading_scale.h2': 0.05, widows: 1, orphans: 1,
    'front_matter.cover.center_pt': 1, 'front_matter.cover.bottom_pt': 0.5, 'front_matter.cover.bottom_mm': 1,
  };
  const INTEGERS = new Set(['widows', 'orphans']);
  const NULLABLE = new Set(['front_matter.cover.bottom_mm']); // null: the automatic distance (the bottom margin + 8 mm)
  // the cover's limits (COVER_SPEC §2) until the stylesheet payload names them
  const FALLBACK_LIMITS = { 'front_matter.cover.center_pt': [8, 96], 'front_matter.cover.bottom_pt': [8, 96], 'front_matter.cover.bottom_mm': [0, 80] };
  const MARGIN_OF = { top_mm: 'top', bottom_mm: 'bottom', inner_mm: 'inner', outer_mm: 'outer' };
  const FONT_FIELDS = { body: 'body_font', latin: 'latin_font', heading: 'heading_font' };
  const SAMPLES = { body: 'نسّاخ يُخرج الكتاب صفحةً صفحة', latin: 'Nassakh, 1234 pages', heading: 'الفصل الأول' };
  const FONT_MENU_H = 300;
  const SECTIONS = ['cover', 'trim', 'margins', 'fonts', 'text', 'page', 'details'];
  const OPEN_KEY = 'nassakh.book.sections';
  const COVER_PREFIX = 'front_matter.cover.';
  const COVER_TEXTS = new Set(['front_matter.cover.center', 'front_matter.cover.bottom']); // typed letter by letter
  // a path whose error the server names in full (`front_matter.fields.title`, `front_matter.cover.center`)
  const ownError = (path) => path.startsWith('front_matter.fields.') || path.startsWith(COVER_PREFIX);
  // a save body that touches the cover alone: nothing in the interior changed
  const coverOnly = (body) => {
    const keys = Object.keys(body || {});
    if (keys.length !== 1 || keys[0] !== 'front_matter') return false;
    const front = Object.keys(body.front_matter || {});
    return front.length === 1 && front[0] === 'cover';
  };
  // the book details (StyleSheet.front_matter.fields), in the order of the title and copyright pages
  const DETAILS = [
    { key: 'title', label: 'العنوان' },
    { key: 'subtitle', label: 'العنوان الفرعي' },
    { key: 'author', label: 'المؤلف' },
    { key: 'editor', label: 'التحقيق' },
    { key: 'translator', label: 'الترجمة' },
    { key: 'publisher', label: 'الناشر' },
    { key: 'city', label: 'المدينة' },
    { key: 'year', label: 'السنة' },
    { key: 'edition', label: 'الطبعة' },
    { key: 'isbn', label: 'ردمك (ISBN)', ltr: true },
    { key: 'rights', label: 'الحقوق', long: true },
  ];

  const round2 = (v) => Math.round(Number(v) * 100) / 100;
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const getPath = (obj, path) => String(path).split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
  const setPath = (obj, path, value) => {
    const keys = String(path).split('.');
    let o = obj;
    keys.slice(0, -1).forEach((k) => { if (o[k] == null || typeof o[k] !== 'object') o[k] = {}; o = o[k]; });
    o[keys[keys.length - 1]] = value;
  };
  function menuAbove(button, panel, height) {
    const h = height || FONT_MENU_H;
    if (!button || !panel) return false;
    return button.bottom + h > panel.bottom && button.top - panel.top >= h;
  }

  NS.parts.style = function style(ctx) {
    const U = NS.util;
    const urls = ctx.urls;
    const T = ctx.timers;
    let pendingFlush = false;
    const openSaved = String(U.readLocal(OPEN_KEY, 'cover,trim,margins')).split(',').filter((s) => SECTIONS.includes(s));

    return {
      sheet: {},
      saved: false,
      trims: [],
      fonts: [],
      latinFonts: [],
      choices: {},
      limits: {},
      missingFonts: [],
      fieldDefaults: {},
      details: DETAILS,
      errors: {},
      dirty: {},
      sheetSave: { state: '', message: '' },
      sheetSaving: false,
      focusMargin: '',
      sections: Object.fromEntries(SECTIONS.map((s) => [s, openSaved.includes(s)])),

      _init_style() {
        this.applyStylesheet(ctx.initial.stylesheet || null);
      },

      // ------------------------------------------------------------ accordions
      toggleSection(key) {
        this.sections = Object.assign({}, this.sections, { [key]: !this.sections[key] });
        U.writeLocal(OPEN_KEY, SECTIONS.filter((s) => this.sections[s]).join(','));
      },
      openSection(key) {
        if (!this.sections[key]) this.toggleSection(key);
      },
      // a section opened and brought into view (the cover sheet clicked, «بيانات الكتاب» from the cover's note)
      revealSection(key) {
        this.openSection(key);
        const run = () => {
          const el = U.q(ctx.dom.root || (U.hasDOM ? document.querySelector('[data-book]') : null), `[data-section="${key}"]`);
          if (el && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'start', behavior: U.reduced() ? 'auto' : 'smooth' });
        };
        if (this.$nextTick) this.$nextTick(run); else run();
        return true;
      },

      // ------------------------------------------------------------ values
      applyStylesheet(payload) {
        if (!payload || !payload.stylesheet) return;
        const values = JSON.parse(JSON.stringify(payload.stylesheet));
        values.heading_scale = Object.assign({ h1: 1.6, h2: 1.25 }, values.heading_scale || {});
        values.front_matter = Object.assign({ title_page: true, contents: true, copyright_page: false }, values.front_matter || {});
        values.front_matter.fields = Object.assign({}, values.front_matter.fields || {});
        // D80: a stylesheet without a cover key is «بلا غلاف» (NassakhBook.cover.DEFAULTS, cover.js)
        const coverDefaults = (NS.cover && NS.cover.DEFAULTS) || { mode: 'none' };
        values.front_matter.cover = Object.assign({}, coverDefaults, values.front_matter.cover || {});
        this.sheet = values;
        this.saved = Boolean(payload.saved);
        if (payload.trims) this.trims = payload.trims;
        if (payload.fonts) this.fonts = payload.fonts;
        if (payload.latin_fonts) this.latinFonts = payload.latin_fonts;
        if (payload.choices) this.choices = payload.choices;
        if (payload.limits) this.limits = payload.limits;
        if (payload.missing_fonts) this.missingFonts = payload.missing_fonts;
        if (payload.field_defaults) this.fieldDefaults = payload.field_defaults;
        // D80: the cover's choices (modes, fits, presets, limits, the upload's limits, the print sizes, the image)
        if (payload.cover && typeof payload.cover === 'object' && !Array.isArray(payload.cover)) this.coverChoices = payload.cover;
      },
      value(path) { return getPath(this.sheet, path); },
      fmt(v) {
        const n = Number(v);
        return Number.isFinite(n) ? String(round2(n)) : '';
      },
      limit(path) {
        const pair = this.limits[path] || FALLBACK_LIMITS[path];
        return Array.isArray(pair) && pair.length === 2 ? pair : [-Infinity, Infinity];
      },
      // a value shown at once and not saved (a colour well while it is dragged); the change event saves it
      setLocal(path, value) {
        const next = JSON.parse(JSON.stringify(this.sheet));
        setPath(next, path, value);
        this.sheet = next;
        return true;
      },
      step(path, dir) {
        const now = Number(this.value(path));
        const base = Number.isFinite(now) ? now : this.limit(path)[0];
        return this.setField(path, base + dir * (STEPS[path] || 1));
      },
      // A change from a control: numbers are parsed (Eastern digits welcome) and clamped to the field's limits;
      // the value shows at once, the PUT follows after a short quiet.
      setField(path, raw) {
        let value = raw;
        if (path in STEPS && !(raw === null && NULLABLE.has(path))) {
          const text = U.westernDigits(raw).replace(/[٫,]/g, '.').replace(/[^\d.-]/g, '').trim();
          value = typeof raw === 'number' ? raw : text ? parseFloat(text) : NaN;
          if (!Number.isFinite(value)) { this.sheet = Object.assign({}, this.sheet); return false; }
          const [lo, hi] = this.limit(path);
          value = clamp(value, lo, hi);
          value = INTEGERS.has(path) ? Math.round(value) : round2(value);
        }
        const next = JSON.parse(JSON.stringify(this.sheet));
        setPath(next, path, value);
        this.sheet = next;
        this.dirty[path] = value;
        const field = ownError(path) ? path : path.split('.')[0];
        if (this.errors[field]) { const errors = Object.assign({}, this.errors); delete errors[field]; this.errors = errors; }
        this.scheduleSheetSave(path.startsWith('front_matter.fields.') || COVER_TEXTS.has(path) ? TEXT_DEBOUNCE_MS : SAVE_DEBOUNCE_MS);
        if (path.startsWith(COVER_PREFIX) && typeof this.onCoverField === 'function') this.onCoverField(path, value);
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
        this.scheduleSheetSave(SAVE_DEBOUNCE_MS);
        return true;
      },
      focusField(path) { this.focusMargin = MARGIN_OF[path] || ''; },
      detailValue(key) { return String(getPath(this.sheet, `front_matter.fields.${key}`) || ''); },
      detailPlaceholder(key) { return this.fieldDefaults[key] ? String(this.fieldDefaults[key]) : ''; },
      scheduleSheetSave(ms) {
        if (!this.canEdit) return;
        clearTimeout(T.sheetSave);
        T.sheetSave = setTimeout(() => this.flushSheet(), ms || SAVE_DEBOUNCE_MS);
      },
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
          else if (!(path in this.dirty)) this.dirty[path] = v;
        });
        walk(body, '');
      },
      // PUT the waiting changes; the server's values (rounded, validated) replace the local ones; the pages
      // follow through a re-layout (the page setup changed) and the book render.
      async flushSheet(opts = {}) {
        clearTimeout(T.sheetSave);
        if (!this.canEdit || !urls.stylesheet) return false;
        if (this.sheetSaving) { pendingFlush = true; return false; }
        const body = this.takeDirty();
        if (!Object.keys(body).length) return false;
        this.sheetSaving = true;
        this.sheetSave = { state: 'saving', message: 'يُحفظ…' };
        const r = await U.api(urls.stylesheet, { method: 'PUT', body, keepalive: Boolean(opts.keepalive) });
        this.sheetSaving = false;
        if (r.ok && r.data) {
          const refused = Object.keys(this.errors).filter((field) => getPath(body, field) === undefined);
          const kept = {};
          refused.forEach((field) => { kept[field] = this.value(field); });
          const waiting = Object.assign({}, this.dirty);
          this.applyStylesheet(r.data);
          if (refused.length || Object.keys(waiting).length) {
            const next = JSON.parse(JSON.stringify(this.sheet));
            refused.forEach((field) => setPath(next, field, kept[field]));
            Object.keys(waiting).forEach((path) => setPath(next, path, waiting[path]));
            this.sheet = next;
          }
          const errors = {};
          refused.forEach((field) => { errors[field] = this.errors[field]; });
          this.errors = errors;
          if (r.data.preview) this.applyPreview(r.data.preview, { quiet: true });
          this.sheetSave = refused.length ? { state: 'invalid', message: 'لم يُحفظ · صحّح القيم' } : { state: 'saved', message: 'حُفظ' };
          clearTimeout(T.sheetSaved);
          T.sheetSaved = setTimeout(() => { if (this.sheetSave.state === 'saved') this.sheetSave = { state: '', message: '' }; }, SAVED_PILL_MS);
          // the page setup changed: the chapter under the eyes is laid out again (the whole book, as a layout);
          // the cover alone changes no page of the interior
          if (!coverOnly(body) && this.focusChapter && typeof this.requestRelayout === 'function') this.requestRelayout(this.focusChapter);
          this.pollNow();
          if (typeof this.afterSheetSaved === 'function') this.afterSheetSaved(body, r.data);
        } else if (r.status === 400) {
          this.errors = (r.data && r.data.errors) || {};
          this.sheetSave = { state: 'invalid', message: (r.data && r.data.detail) || 'لم يُحفظ · صحّح القيم' };
          Object.keys(this.errors).forEach((field) => {
            const keys = field.split('.');
            let o = body;
            keys.slice(0, -1).forEach((k) => { o = o && o[k]; });
            if (o) delete o[keys[keys.length - 1]];
          });
          if (Object.keys(body).length) { this.mergeBack(body); this.scheduleSheetSave(SAVE_DEBOUNCE_MS); }
        } else {
          this.sheetSave = { state: 'error', message: r.status === 403 ? 'هذا الإجراء يتطلب صلاحية محرّر.' : r.status === 401 ? 'انتهت الجلسة · سجّل الدخول' : 'تعذّر الحفظ · إعادة المحاولة' };
          this.mergeBack(body);
        }
        if (pendingFlush) { pendingFlush = false; this.flushSheet(); }
        return r.ok;
      },

      // ------------------------------------------------------------ faces
      font(key) { return this.fonts.find((f) => f.key === key) || null; },
      fontChoices(role) { return role === 'latin' ? this.fonts.filter((f) => f.latin) : this.fonts; },
      fontLabel(key) { const f = this.font(key); return f ? f.label : key || ''; },
      fontInstalled(key) { const f = this.font(key); return Boolean(f && f.installed); },
      faceStyle(key) {
        const f = this.font(key);
        const family = f ? f.family : key;
        return family ? `font-family: "${String(family).replace(/"/g, '')}", var(--font-sans)` : '';
      },
      sample(role) { return SAMPLES[role] || SAMPLES.body; },
      fontField(role) { return FONT_FIELDS[role] || 'body_font'; },
      menuAbove,
      fontMenuAbove(button) {
        const panel = U.closest(button, '.bp-tab-body');
        return menuAbove(U.rect(button), panel ? U.rect(panel) : null, FONT_MENU_H);
      },
      get missingFontText() {
        if (!this.missingFonts.length) return '';
        const names = [...new Set(this.missingFonts.map((m) => m.name))];
        const fallback = this.missingFonts[0].fallback || 'Amiri';
        return `${names.join('، ')} غير مثبّت على هذا الجهاز؛ تُرتَّب الصفحات بخط ${fallback} بدلًا منه.`;
      },

      // ------------------------------------------------------------ the page diagram and labels
      get trimLabel() {
        const preset = this.trims.find((t) => t.key === this.sheet.trim);
        if (preset && preset.width_mm) return preset.label;
        // isolated left to right (U+2066 … U+2069): in an Arabic line «120×180» would read «180×120»
        return `\u2066${this.fmt(this.sheet.width_mm)}×${this.fmt(this.sheet.height_mm)}\u2069 مم`;
      },
      get diagramStyle() {
        const w = Number(this.sheet.width_mm) || 170;
        const h = Number(this.sheet.height_mm) || 240;
        const f = (v, total) => `${clamp((Number(v) || 0) / total, 0, 0.45).toFixed(4)}`;
        return `--lo-ar: ${(w / h).toFixed(4)}; --lo-t: ${f(this.sheet.top_mm, h)}; --lo-b: ${f(this.sheet.bottom_mm, h)}; --lo-i: ${f(this.sheet.inner_mm, w)}; --lo-o: ${f(this.sheet.outer_mm, w)}`;
      },
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
    };
  };
})();
