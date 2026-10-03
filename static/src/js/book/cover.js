// The cover («الغلاف», COVER_SPEC §4, D80) on the book page: the first section of «التنسيق» and the sheet before
// page 1. The section's values live in the stylesheet (`front_matter.cover`: the mode, the image, the fit, the
// centre and bottom texts, the colours and their preset, the sizes) and save through style.js's setField like
// every format field; this part adds what the cover alone needs: the image upload (`api:book_images`, multipart,
// with progress), the colour presets as a small palette of covers, the print-size note for the trim, and the
// render the stage shows ({mode, hash, image_1x, image_2x, width, height}: embedded when cached, then the one
// every stylesheet save answers with — `cover_render` — else `api:cover`). Between a change and its render the
// sheet draws the cover itself from the values (the draft: texts, faces, sizes, the picture in its fit), so a
// change shows at once and the render fades in over it; a thin bar along the sheet's foot says one is on its
// way. The sheet itself (the turn to it, the thumb) is stage.js. Pure helpers are on `NassakhBook.cover`.
(function () {
  'use strict';

  const root = typeof window !== 'undefined' ? window : globalThis;
  const NS = (root.NassakhBook = root.NassakhBook || {});
  NS.parts = NS.parts || {};

  const PREFIX = 'front_matter.cover.';
  const MODES = [
    { key: 'none', label: 'بلا غلاف' },
    { key: 'info', label: 'من بيانات الكتاب' },
    { key: 'image', label: 'صورة' },
    { key: 'text', label: 'نص مخصّص' },
  ];
  const FITS = [
    { key: 'fill', label: 'ملء الصفحة' },
    { key: 'width', label: 'ملاءمة العرض' },
    { key: 'height', label: 'ملاءمة الارتفاع' },
  ];
  // the named presets (COVER_SPEC §1.5, = editor.models.COVER_PRESETS; the stylesheet payload's `cover.presets`
  // win when they are there): the background and the text; a pair set by hand is `custom`
  const PRESETS = [
    { key: 'white', label: 'أبيض', background: '#ffffff', color: '#1b1b1b' },
    { key: 'cream', label: 'كريمي', background: '#f4efe4', color: '#2a2419' },
    { key: 'gray', label: 'رمادي', background: '#e9e9ec', color: '#1f1f24' },
    { key: 'navy', label: 'كحلي', background: '#1d2433', color: '#f3efe6' },
    { key: 'green', label: 'أخضر داكن', background: '#1f3b2d', color: '#f1ead8' },
    { key: 'burgundy', label: 'عنابي', background: '#4a1d24', color: '#f4e9d8' },
  ];
  const CUSTOM = 'custom';
  const DEFAULTS = { mode: 'none', image: null, fit: 'fill', center: '', bottom: '', center_pt: 28, bottom_pt: 13, background: '#ffffff', color: '#1b1b1b', preset: 'white', bottom_mm: null };
  const IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp'];
  const IMAGE_EXT = /\.(jpe?g|png|webp)$/i;
  const MAX_BYTES = 30 * 1024 * 1024;
  const PRINT_DPI = 300;
  const MM_PER_INCH = 25.4;
  const PRELOAD_MS = 4000; // never wait longer on a stalled render
  const PT_PER_MM = 72 / 25.4;
  const SUB_SCALE = 0.55; // the subtitle and the author of an `info` cover (= publishing.model.COVER_SUB_SCALE)
  const HEX = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i;
  // isolated left to right (U+2066 … U+2069): in an Arabic line «2008 × 2835» would read «2835 × 2008»
  const ltr = (s) => `⁦${s}⁩`;
  // the words of api:book_images for what is refused before any request (= editor.images), and the rest
  const MESSAGES = {
    type: 'نوع الصورة غير مقبول؛ المقبول JPEG وPNG وWebP.',
    size: 'الصورة أكبر من 30 ميغابايت؛ اختر صورة أصغر.',
    hex: 'اللون غير صالح؛ يُكتب مثل #1d2433.',
    notImage: 'تعذّرت قراءة الملف صورةً؛ اختر صورة JPEG أو PNG أو WebP.',
    session: 'انتهت الجلسة. سجّل الدخول من جديد.',
    editor: 'هذا الإجراء يتطلب صلاحية محرّر.',
    network: 'انقطع الاتصال بالخادم. تحقّق من الشبكة ثم أعد المحاولة.',
    failed: 'تعذّر رفع الصورة. حاول مرة أخرى.',
  };

  // «#ABC», «abc», «#aabbcc» → «#aabbcc»; '' when it is no colour
  function normaliseHex(raw) {
    const m = HEX.exec(String(raw || '').trim());
    if (!m) return '';
    const hex = m[1].toLowerCase();
    return `#${hex.length === 3 ? hex.split('').map((c) => c + c).join('') : hex}`;
  }
  // the pixels a trim needs at 300 dpi: {width, height}
  function printPixels(widthMm, heightMm, dpi) {
    const px = (mm) => Math.round(((Number(mm) || 0) / MM_PER_INCH) * (dpi || PRINT_DPI));
    return { width: px(widthMm), height: px(heightMm) };
  }
  // the dpi of an image as fitted on the trim: `fill` the tighter side, `width` / `height` that side; 0 unknown
  function effectiveDpi(image, widthMm, heightMm, fit) {
    const w = Number(image && image.width) || 0;
    const h = Number(image && image.height) || 0;
    const tw = (Number(widthMm) || 0) / MM_PER_INCH;
    const th = (Number(heightMm) || 0) / MM_PER_INCH;
    if (!w || !h || !tw || !th) return 0;
    const byWidth = w / tw;
    const byHeight = h / th;
    return Math.round(fit === 'width' ? byWidth : fit === 'height' ? byHeight : Math.min(byWidth, byHeight));
  }
  // the preset a pair of colours is (its key), else '' (the server stores `custom`)
  function presetOf(presets, background, color) {
    const p = (presets || []).find((x) => x.background === background && x.color === color);
    return p ? p.key : '';
  }
  // why a file cannot be the cover's image, before any request ('' when it can); `limits` = the payload's
  // `cover.upload` ({types, max_bytes, max_mb}) when there is one
  function imageProblem(file, limits) {
    if (!file) return MESSAGES.type;
    const types = limits && Array.isArray(limits.types) && limits.types.length ? limits.types : IMAGE_TYPES;
    const maxBytes = Number(limits && limits.max_bytes) || MAX_BYTES;
    const type = String(file.type || '');
    const ok = type ? types.includes(type) : IMAGE_EXT.test(String(file.name || ''));
    if (!ok) return MESSAGES.type;
    if (Number(file.size) > maxBytes) return MESSAGES.size.replace('30', String(Number(limits && limits.max_mb) || 30));
    return '';
  }
  // the server's Arabic message when it sent one, else one for the status
  function uploadMessage(status, data) {
    const own = data && typeof data === 'object' && (data.detail || data.message);
    if (own) return String(own);
    if (status === 413) return MESSAGES.size;
    if (status === 422 || status === 400) return MESSAGES.notImage;
    if (status === 401) return MESSAGES.session;
    if (status === 403) return MESSAGES.editor;
    return status ? MESSAGES.failed : MESSAGES.network;
  }
  // `[{value, label}]` (the payload's choices) or `[{key, label}]` → `[{key, label}]`
  const keyed = (list, fallback) => (Array.isArray(list) && list.length ? list.map((c) => ({ key: c.key !== undefined ? c.key : c.value, label: c.label })) : fallback);
  // A multipart POST with progress (XMLHttpRequest; fetch where it is missing) → {ok, status, data, message}.
  function upload(url, form, onProgress, csrf) {
    return new Promise((resolve) => {
      const done = (status, text) => {
        let data = null;
        try { data = text ? JSON.parse(text) : null; } catch (_) { data = null; }
        resolve({ ok: status >= 200 && status < 300, status, data, message: uploadMessage(status, data) });
      };
      const failed = () => resolve({ ok: false, status: 0, data: null, message: MESSAGES.network });
      const XHR = root.XMLHttpRequest;
      if (typeof XHR !== 'function') {
        if (typeof root.fetch !== 'function') { failed(); return; }
        root.fetch(url, { method: 'POST', body: form, credentials: 'same-origin', headers: { Accept: 'application/json', 'X-CSRFToken': csrf || '' } })
          .then((r) => r.text().then((text) => done(r.status, text)))
          .catch(failed);
        return;
      }
      const xhr = new XHR();
      xhr.open('POST', url, true);
      xhr.setRequestHeader('Accept', 'application/json');
      if (csrf) xhr.setRequestHeader('X-CSRFToken', csrf);
      if (xhr.upload && typeof onProgress === 'function') {
        xhr.upload.onprogress = (e) => { if (e && e.lengthComputable && e.total) onProgress(e.loaded / e.total); };
      }
      xhr.onload = () => done(xhr.status, xhr.responseText);
      xhr.onerror = failed;
      xhr.onabort = failed;
      xhr.send(form);
    });
  }

  NS.cover = { PREFIX, MODES, FITS, PRESETS, CUSTOM, DEFAULTS, MESSAGES, normaliseHex, printPixels, effectiveDpi, presetOf, imageProblem, uploadMessage };

  NS.parts.cover = function cover(ctx) {
    const U = NS.util;
    const urls = ctx.urls;
    let coverGen = 0; // the newest api:cover request wins
    let dragging = false; // a colour well dragged, its change not made yet
    // page_config's `urls.cover` and `urls.bookImages`; the spec's addresses (§3) where they are not named yet
    const derived = (tail) => (urls.stylesheet ? String(urls.stylesheet).replace(/stylesheet\/?$/, tail) : '');
    const coverUrl = () => urls.cover || derived('cover/');
    const imagesUrl = () => urls.bookImages || urls.images || derived('images/');
    // the render decoded before it replaces the one shown (no blank frame between two covers)
    const preload = (src) => new Promise((resolve) => {
      if (!src || typeof root.Image !== 'function') { resolve(false); return; }
      let settled = false;
      const finish = (ok) => { if (!settled) { settled = true; resolve(ok); } };
      try {
        const im = new root.Image();
        im.onload = () => finish(true);
        im.onerror = () => finish(false);
        im.src = src;
      } catch (_) { finish(false); }
      setTimeout(() => finish(false), PRELOAD_MS);
    });

    return {
      cover: null, // api:cover's answer: the render the stage shows
      coverState: '', // '' | loading | error
      coverStale: false, // the values changed since that render: the sheet draws them itself until the next one
      coverImage: null, // the image uploaded in this session: {id, url, width, height, format, name}
      coverUpload: { state: '', percent: 0, error: '' }, // state: '' | uploading
      coverDrag: false, // a file held over the drop zone
      coverChoices: {}, // the stylesheet payload's `cover` block (style.js applyStylesheet): modes, fits, presets, limits, upload, print sizes, the image

      // the page embeds the cached render (editor.services.cached_cover): the sheet paints at once; a cover not
      // rendered yet is fetched (api:cover renders it); a book without one («بلا غلاف», most books) asks nothing
      _init_cover() {
        const first = ctx.initial && ctx.initial.cover;
        if (first && first.image_1x) this.cover = first;
        this.syncCoverThumb();
        this.resolvePendingCover();
        if (this.hasCover && !this.cover) this.refreshCover();
      },

      // ------------------------------------------------------------ the values (front_matter.cover, with the defaults)
      get coverValues() {
        const front = (this.sheet && this.sheet.front_matter) || {};
        const values = Object.assign({}, DEFAULTS, this.coverChoices.defaults || {}, front.cover || {});
        if (values.bottom_mm === null || values.bottom_mm === undefined) values.bottom_mm = Number(this.coverChoices.bottom_mm_auto) || (Number(this.sheet && this.sheet.bottom_mm) || 0) + 8;
        return values;
      },
      get coverModes() { return keyed(this.coverChoices.modes, MODES); },
      get coverFits() { return keyed(this.coverChoices.fits, FITS); },
      get coverMode() {
        const m = this.coverValues.mode;
        return this.coverModes.some((x) => x.key === m) ? m : 'none';
      },
      // the stage shows a cover sheet (the chosen mode, saved or not); with `none` nothing changes anywhere
      get hasCover() { return this.coverMode !== 'none'; },
      get coverModeLabel() { return (this.coverModes.find((m) => m.key === this.coverMode) || MODES[0]).label; },
      get coverPresets() {
        const list = this.coverChoices.presets;
        return Array.isArray(list) && list.length ? list.filter((p) => p.key !== CUSTOM) : PRESETS;
      },
      // the swatch checked: the preset the colours are now ('' for a pair set by hand: the server's `custom`)
      get coverPreset() {
        const v = this.coverValues;
        return presetOf(this.coverPresets, v.background, v.color);
      },
      // the sheet's colours, and its width in points (`--pw`: the draft's sizes are points, as on the pages)
      get coverSheetStyle() {
        const v = this.coverValues;
        const width = (Number(this.sheet && this.sheet.width_mm) || 170) * PT_PER_MM;
        return `--cover-bg: ${normaliseHex(v.background) || DEFAULTS.background}; --cover-fg: ${normaliseHex(v.color) || DEFAULTS.color}; --pw: ${width.toFixed(2)}`;
      },
      // «للطباعة: 300 نقطة في البوصة، أي 2008 × 2835 بكسل على هذا القطع»: the server's pixels for a trim preset
      // (`cover.print_sizes`), the formula for a custom trim
      get coverSizeNote() {
        const sizes = this.coverChoices.print_sizes || {};
        const known = this.sheet && sizes[this.sheet.trim];
        const px = Array.isArray(known) && known.length === 2 ? { width: known[0], height: known[1] } : printPixels(this.sheet && this.sheet.width_mm, this.sheet && this.sheet.height_mm);
        return `للطباعة: ${PRINT_DPI} نقطة في البوصة، أي ${ltr(`${px.width} × ${px.height}`)} بكسل على هذا القطع`;
      },
      // the image chosen: the one uploaded here, else the payload's (`cover.image`), else the render as its thumb;
      // {id, url, thumb_url, width, height, name}
      get coverImageInfo() {
        const v = this.coverValues;
        if (v.image === null || v.image === undefined || v.image === '') return null;
        if (this.coverImage && String(this.coverImage.id) === String(v.image)) return this.coverImage;
        const known = this.coverChoices.image;
        if (known && String(known.id) === String(v.image)) return { id: known.id, url: known.url, thumb_url: known.thumb_url || known.url, width: known.width, height: known.height, name: known.source_name || '' };
        return { id: v.image, url: this.coverImageUrl, thumb_url: this.coverImageUrl, width: null, height: null, name: '' };
      },
      // «الصورة 2400 × 3400 بكسل · نحو 359 نقطة في البوصة على هذا القطع» (the image as fitted)
      get coverImageNote() {
        const info = this.coverImageInfo;
        if (!info || !info.width || !info.height) return '';
        const dpi = effectiveDpi(info, this.sheet && this.sheet.width_mm, this.sheet && this.sheet.height_mm, this.coverValues.fit);
        return `الصورة ${ltr(`${info.width} × ${info.height}`)} بكسل · نحو ${dpi} نقطة في البوصة على هذا القطع`;
      },
      get coverUploadText() { return `يُرفع… ${this.coverUpload.percent} %`; },

      // ------------------------------------------------------------ the render (api:cover)
      get coverImageUrl() {
        const c = this.cover;
        return c && c.mode !== 'none' && c.image_1x ? c.image_1x : '';
      },
      get coverSrcset() {
        const c = this.cover;
        return c && c.image_1x && c.image_2x ? `${c.image_1x} 1x, ${c.image_2x} 2x` : '';
      },
      // the render shown is of the values as they are now (else the draft stands in)
      get coverFresh() { return Boolean(this.coverImageUrl) && !this.coverStale; },
      // something to draw: texts, or an image cover with its image
      get coverDrawable() { return this.hasCover && !(this.coverMode === 'image' && !this.coverImageInfo); },
      // a render on its way (a change saved, then drawn; or the first one): a thin bar along the sheet's foot
      get coverBusy() { return this.coverDrawable && this.coverState !== 'error' && (this.coverState === 'loading' || this.coverStale || !this.coverImageUrl); },
      // why the sheet shows no picture: no image chosen, or the render failed (a click on the sheet retries)
      get coverHint() {
        if (this.coverMode === 'image' && !this.coverImageInfo) return 'لم تُرفع صورة الغلاف بعد';
        if (this.coverState === 'error') return 'تعذّر تحضير الغلاف · انقر لإعادة المحاولة';
        return '';
      },
      // The cover drawn here from its values, at once (the owner's review, 2026-10-03: never a blank «يُحضَّر»
      // sheet): the texts in the render's places, faces and sizes (publishing/cover.py), the picture in its fit;
      // the render fades in over it when ready, and it stands in again from the next change on.
      get coverDraft() {
        const v = this.coverValues;
        const fields = (this.sheet && this.sheet.front_matter && this.sheet.front_matter.fields) || {};
        const defaults = this.fieldDefaults || {};
        const pick = (key) => String(fields[key] || defaults[key] || (key === 'title' ? this.title : '') || '').trim();
        const center = [];
        const bottom = [];
        if (v.mode === 'info') {
          [['title', 'title'], ['subtitle', 'sub'], ['author', 'author']].forEach(([key, kind]) => { if (pick(key)) center.push({ kind, text: pick(key) }); });
          const place = ['publisher', 'city', 'year'].map(pick).filter(Boolean).join('، ');
          if (place) bottom.push({ kind: 'foot', text: place });
        } else if (v.mode === 'text') {
          if (String(v.center || '').trim()) center.push({ kind: 'title', text: String(v.center) });
          if (String(v.bottom || '').trim()) bottom.push({ kind: 'foot', text: String(v.bottom) });
        }
        const info = v.mode === 'image' ? this.coverImageInfo : null;
        const src = info ? info.thumb_url || info.url || '' : '';
        return { center, bottom, image: src ? { src, fit: FITS.some((f) => f.key === v.fit) ? v.fit : 'fill' } : null };
      },
      // the draft's places and sizes (points: `--u` is one point on the sheet): the side padding is the larger
      // side margin, the bottom block `bottom_mm` from the edge, the subtitle and the author 0.55 of the title
      get coverDraftStyle() {
        const s = this.sheet || {};
        const v = this.coverValues;
        const w = Number(s.width_mm) || 170;
        const h = Number(s.height_mm) || 240;
        const side = Math.max(Number(s.inner_mm) || 0, Number(s.outer_mm) || 0);
        const title = Number(v.center_pt) || DEFAULTS.center_pt;
        return `--cd-side: ${((side / w) * 100).toFixed(3)}%; --cd-bottom: ${(((Number(v.bottom_mm) || 0) / h) * 100).toFixed(3)}%; --cd-title: ${title}; --cd-sub: ${(title * SUB_SCALE).toFixed(2)}; --cd-foot: ${Number(v.bottom_pt) || DEFAULTS.bottom_pt}`;
      },
      // `data`: an answer that carries the render (a stylesheet save's `cover_render`), else api:cover is asked
      // (it renders a cover not drawn yet). The newest answer wins; the picture is decoded before it shows.
      async refreshCover(data) {
        const gen = (coverGen += 1);
        let payload = data && typeof data === 'object' ? data : null;
        if (!payload) {
          const url = coverUrl();
          if (!url) return false;
          if (!this.cover || this.coverStale) this.coverState = 'loading';
          const r = await U.api(url);
          if (gen !== coverGen) return false;
          if (!r.ok || !r.data) { this.coverState = 'error'; this.syncCoverThumb(); return false; }
          payload = r.data;
        }
        if (payload.pending && !payload.image_1x && data) return this.refreshCover(); // not drawn yet: api:cover draws it
        if (payload.image_1x && !(this.cover && this.cover.hash === payload.hash)) await preload(payload.image_1x);
        if (gen !== coverGen) return false;
        this.cover = payload;
        this.coverState = '';
        // a change made after that save (waiting for the next one, or a colour well being dragged) keeps the draft
        this.coverStale = dragging || Object.keys(this.dirty || {}).some((path) => path.startsWith(PREFIX));
        this.syncCoverThumb();
        return true;
      },
      // the stylesheet was saved (style.js flushSheet): the render it answers with (`cover_render`), else asked
      // for (the trim, the faces, the book details and every cover field change it)
      afterSheetSaved(body, payload) {
        const touched = Boolean(body && body.front_matter && body.front_matter.cover);
        const render = payload && payload.cover_render;
        if (render && typeof render === 'object') this.refreshCover(render);
        else if (touched || this.hasCover) this.refreshCover();
        this.syncCoverThumb();
      },
      // a cover field changed here (style.js setField): the draft shows it at once; the stage turns to the cover;
      // gone, it leaves it
      onCoverField() {
        this.coverStale = true;
        if (this.coverState === 'error') this.coverState = '';
        this.syncCoverThumb();
        if (this.hasCover) {
          if (!this.onCover && typeof this.showCover === 'function') this.showCover();
        } else if (this.onCover && typeof this.showIndex === 'function') {
          this.showIndex(0, { instant: true });
        }
      },
      // a click on the sheet: a failed render is tried again; else the cover's section in «التنسيق»
      onCoverSheet() {
        if (this.coverState === 'error' && this.coverDrawable) { this.refreshCover(); return true; }
        return this.openCoverSection();
      },

      // ------------------------------------------------------------ the section's controls
      setCoverMode(key) {
        if (!this.coverModes.some((m) => m.key === key) || key === this.coverMode) return false;
        return this.setField(`${PREFIX}mode`, key);
      },
      setCoverFit(key) {
        if (!this.coverFits.some((f) => f.key === key)) return false;
        return this.setField(`${PREFIX}fit`, key);
      },
      // a preset: the two colours at once here, the preset alone on the wire (the server sets both from it)
      setCoverPreset(key) {
        const p = this.coverPresets.find((x) => x.key === key);
        if (!p || typeof this.setLocal !== 'function') return false;
        this.setLocal(`${PREFIX}background`, p.background);
        this.setLocal(`${PREFIX}color`, p.color);
        return this.setField(`${PREFIX}preset`, p.key);
      },
      // a colour typed or picked: normalised to «#rrggbb» and saved alone (the server names the preset the pair
      // is, else `custom`; the same is shown here at once)
      setCoverColor(key, raw) {
        if (key !== 'background' && key !== 'color') return false;
        const path = PREFIX + key;
        const hex = normaliseHex(raw);
        if (!hex) { this.errors = Object.assign({}, this.errors, { [path]: MESSAGES.hex }); return false; }
        dragging = false;
        this.setField(path, hex);
        const v = this.coverValues;
        if (typeof this.setLocal === 'function') this.setLocal(`${PREFIX}preset`, presetOf(this.coverPresets, v.background, v.color) || CUSTOM);
        return true;
      },
      // the colour well while it is dragged: the sheet follows at once, the save waits for the change event
      previewCoverColor(key, raw) {
        const hex = normaliseHex(raw);
        if (!hex || (key !== 'background' && key !== 'color') || typeof this.setLocal !== 'function') return false;
        this.coverStale = true; // the draft follows the well while the render waits for the change event
        dragging = true;
        return this.setLocal(PREFIX + key, hex);
      },

      // ------------------------------------------------------------ the image
      pickCoverImage() {
        if (!this.canEdit || this.coverUpload.state === 'uploading') return false;
        const input = this.$refs && this.$refs.coverFile;
        if (!input || typeof input.click !== 'function') return false;
        try { input.value = ''; } catch (_) { /* a file input's value is read-only in some browsers */ }
        input.click();
        return true;
      },
      onCoverFile(e) {
        const files = e && e.target && e.target.files;
        const file = files && files[0];
        if (file) this.uploadCoverImage(file);
      },
      onCoverDrop(e) {
        this.coverDrag = false;
        const dt = e && e.dataTransfer;
        const file = dt && dt.files && dt.files[0];
        if (file) this.uploadCoverImage(file);
      },
      // POST api:book_images (multipart `file`, `purpose: cover`), then the image id saves as every field does
      async uploadCoverImage(file) {
        if (!this.canEdit || !file) return false;
        const url = imagesUrl();
        if (!url || this.coverUpload.state === 'uploading') return false;
        const problem = imageProblem(file, this.coverChoices.upload);
        if (problem) { this.coverUpload = { state: '', percent: 0, error: problem }; return false; }
        const form = new root.FormData();
        form.append((this.coverChoices.upload && this.coverChoices.upload.field) || 'file', file, file.name);
        form.append('purpose', 'cover');
        this.coverUpload = { state: 'uploading', percent: 0, error: '' };
        const onProgress = (share) => { this.coverUpload = Object.assign({}, this.coverUpload, { percent: Math.max(0, Math.min(100, Math.round(share * 100))) }); };
        const r = await upload(url, form, onProgress, U.csrfToken());
        if (!r.ok || !r.data || r.data.id === undefined || r.data.id === null) {
          this.coverUpload = { state: '', percent: 0, error: r.message };
          return false;
        }
        const d = r.data;
        this.coverImage = { id: d.id, url: d.url, thumb_url: d.thumb_url || d.url, width: d.width, height: d.height, format: d.format, name: d.source_name || file.name || '' };
        this.coverUpload = { state: '', percent: 0, error: '' };
        this.setField(`${PREFIX}image`, r.data.id);
        this.liveMessage = 'رُفعت صورة الغلاف';
        return true;
      },
      removeCoverImage() {
        if (!this.canEdit) return false;
        this.coverImage = null;
        this.coverUpload = { state: '', percent: 0, error: '' };
        return this.setField(`${PREFIX}image`, null);
      },

      // ------------------------------------------------------------ the sheet or the thumb clicked: the section
      openCoverSection() {
        if (typeof this.setTab === 'function') this.setTab('format', { quiet: true });
        if (typeof this.revealSection === 'function') this.revealSection('cover');
        return true;
      },
    };
  };
})();
