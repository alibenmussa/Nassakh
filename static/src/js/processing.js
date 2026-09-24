// Alpine components for the processing screens:
//   guidesEditor     – guides screen: two draggable horizontal lines + page-number zone on the reference page
//   preprocessPanel  – page detail: rotation / crop / Sauvola controls posting to /api/pages/<id>/preprocess/
// This file runs before the deferred Alpine bundle, so components are registered on `alpine:init`.
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
      throw new Error(message || 'تعذّر تنفيذ الطلب. حاول مرة أخرى.');
    }
    return data;
  }

  document.addEventListener('alpine:init', () => {
    // ------------------------------------------------------------ guides screen
    Alpine.data('guidesEditor', (cfg) => ({
      header: { enabled: cfg.header_cut != null, ratio: cfg.header_cut != null ? cfg.header_cut : 0.08 },
      footnote: {
        enabled: cfg.footnote_line != null,
        ratio: cfg.footnote_line != null ? cfg.footnote_line : (cfg.proposal != null ? cfg.proposal : (cfg.detected_rule != null ? cfg.detected_rule : 0.8)),
      },
      zone: cfg.page_number_zone || 'bottom',
      zoneHeight: cfg.page_number_height || 0.06,
      proposal: cfg.proposal != null ? cfg.proposal : null,
      detectedRule: cfg.detected_rule != null ? cfg.detected_rule : null,
      dragging: null,

      pct(ratio) { return (ratio * 100).toFixed(2) + '%'; },
      pctText(ratio) { return (ratio * 100).toFixed(1); },

      footnoteZoneStyle() {
        const reserved = this.zone === 'bottom' ? this.zoneHeight : 0;
        return { top: this.pct(this.footnote.ratio), height: this.pct(Math.max(0, 1 - reserved - this.footnote.ratio)) };
      },
      numberZoneStyle() {
        return this.zone === 'top'
          ? { top: 0, height: this.pct(this.zoneHeight) }
          : { bottom: 0, height: this.pct(this.zoneHeight) };
      },

      startDrag(which) { this.dragging = which; },
      onMove(event) {
        if (!this.dragging) return;
        const rect = this.$refs.stage.getBoundingClientRect();
        if (!rect.height) return;
        this.setRatio(this.dragging, (event.clientY - rect.top) / rect.height);
      },
      endDrag() { this.dragging = null; },

      setRatio(which, ratio) {
        const r = round4(clamp(ratio, 0, 1));
        if (which === 'header') {
          const max = this.footnote.enabled ? Math.min(0.5, this.footnote.ratio - 0.01) : 0.5;
          this.header.ratio = clamp(r, 0, max);
        } else {
          const min = this.header.enabled ? Math.max(0.2, this.header.ratio + 0.01) : 0.2;
          this.footnote.ratio = clamp(r, min, 1);
        }
      },
      setPercent(which, value) {
        const v = parseFloat(value);
        if (Number.isFinite(v)) this.setRatio(which, v / 100);
      },
      nudge(which, delta) {
        this.setRatio(which, (which === 'header' ? this.header.ratio : this.footnote.ratio) + delta);
      },
      setZoneHeight(value) {
        const v = parseFloat(value);
        if (Number.isFinite(v)) this.zoneHeight = clamp(round4(v / 100), 0.01, 0.25);
      },
      useProposal() {
        if (this.proposal === null) return;
        this.footnote.enabled = true;
        this.setRatio('footnote', this.proposal);
      },
    }));

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

      async post(body) {
        this.busy = true;
        this.error = '';
        this.notice = '';
        try {
          const data = await postJson(this.state.api_url, body);
          if (data.queued) {
            this.notice = data.detail || 'أُرسلت المعالجة إلى العامل الخلفي؛ حدّث الصفحة بعد قليل.';
            return;
          }
          data.api_url = this.state.api_url;
          data.guides_url = this.state.guides_url;
          data.guides = this.state.guides;
          data.override = this.state.override;
          this.load(data);
          this.swapImages(data);
          this.notice = 'تمت المعالجة. أعد تشغيل التعرّف على النص من قائمة إعادة التشغيل إن أردت تحديث النص.';
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
