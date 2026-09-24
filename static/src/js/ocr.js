// Alpine components for the ocr app: the text panel of the page detail screen.
// These scripts run before the deferred Alpine bundle: register components inside
// document.addEventListener('alpine:init', () => { Alpine.data('name', () => ({ ... })) });

document.addEventListener('alpine:init', () => {
  // Page statuses during which the pipeline is still working on the page.
  const ACTIVE_STATUSES = ['uploaded', 'preprocessed', 'layout_done'];
  const POLL_MS = 2000;
  // Reload the runs list every N status polls while active (engines finish one after another).
  const RUNS_EVERY = 3;

  const CHECK_LABELS = {
    loop: 'تكرار مفرط',
    empty: 'ناتج فارغ',
    too_short: 'أقصر من مرجع Tesseract',
    too_long: 'أطول من مرجع Tesseract',
    low_overlap: 'تطابق ضعيف مع Tesseract',
    error: 'خطأ',
    missing: 'لم يُشغَّل',
  };

  /**
   * textPanel({ statusUrl, textUrl, runsUrl })
   * Initial data comes from the <script type="application/json"> that the partial embeds
   * (same shape as the /text/ endpoint plus `runs`).
   */
  Alpine.data('textPanel', (config) => ({
    status: '',
    textState: 'none',
    provisional: '',
    finalText: '',
    flags: [],
    error: '',
    lines: [],
    runs: [],
    nLow: 0,
    active: null, // server-side `active` flag when known; falls back to ACTIVE_STATUSES
    polling: false,
    fetchFailed: false,
    timer: null,
    ticks: 0,

    init() {
      const script = this.$root.querySelector('script[type="application/json"]');
      if (script) {
        try {
          this.apply(JSON.parse(script.textContent));
        } catch (e) {
          // Malformed payload: the first poll fills the panel instead.
        }
      }
      if (this.isActive) this.start();
    },

    destroy() {
      this.stop();
    },

    get isActive() {
      if (typeof this.active === 'boolean') return this.active;
      return ACTIVE_STATUSES.includes(this.status);
    },

    // Merge a payload from the embedded data, /status/ or /text/ into the component state.
    apply(data) {
      if (!data) return;
      if (data.status !== undefined) this.status = data.status || '';
      if (data.active !== undefined) this.active = Boolean(data.active);
      if (data.text_state !== undefined) this.textState = data.text_state || 'none';
      if (data.provisional_text !== undefined) this.provisional = data.provisional_text || '';
      if (data.final_text !== undefined) this.finalText = data.final_text || '';
      if (data.flags !== undefined) this.flags = Array.isArray(data.flags) ? data.flags : [];
      if (data.error !== undefined) this.error = this.errorText(data.error);
      if (data.lines !== undefined) {
        this.lines = Array.isArray(data.lines) ? data.lines : [];
        this.nLow = this.lines.reduce((sum, line) => sum + (line.n_low || 0), 0);
      }
      if (data.runs !== undefined) this.runs = Array.isArray(data.runs) ? data.runs : [];
    },

    errorText(value) {
      if (!value) return '';
      if (typeof value === 'string') return value;
      return value.message || value.error_message || value.detail || '';
    },

    start() {
      if (this.timer) return;
      this.polling = true;
      this.timer = setInterval(() => this.poll(), POLL_MS);
    },

    stop() {
      if (this.timer) clearInterval(this.timer);
      this.timer = null;
      this.polling = false;
    },

    async getJson(url) {
      const response = await fetch(url, {
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
        cache: 'no-store',
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    },

    async poll() {
      if (document.hidden) return;
      this.ticks += 1;
      try {
        const data = await this.getJson(config.statusUrl);
        const before = `${this.status}/${this.textState}`;
        this.apply({
          status: data.status,
          active: data.active,
          text_state: data.text_state,
          provisional_text: data.provisional_text,
          final_text: data.final_text,
          flags: data.flags,
          error: data.error,
        });
        this.fetchFailed = false;
        const changed = before !== `${this.status}/${this.textState}`;
        if (changed || !this.isActive) {
          await this.refresh();
        } else if (this.ticks % RUNS_EVERY === 0) {
          await this.refreshRuns();
        }
        if (!this.isActive) this.stop();
      } catch (e) {
        this.fetchFailed = true;
      }
    },

    async refresh() {
      const [text, runs] = await Promise.allSettled([this.getJson(config.textUrl), this.getJson(config.runsUrl)]);
      if (text.status === 'fulfilled') this.apply(text.value);
      if (runs.status === 'fulfilled') this.apply({ runs: runs.value.runs });
    },

    async refreshRuns() {
      try {
        const data = await this.getJson(config.runsUrl);
        this.apply({ runs: data.runs });
      } catch (e) {
        // transient; the next poll retries
      }
    },

    tokenTitle(tok) {
      const parts = [];
      if (tok.alt) parts.push(`القراءة البديلة: ${tok.alt}`);
      else if (tok.conf === 'low' && !tok.digit) parts.push('لا مقابل لها في المحرّك الثاني');
      if (tok.digit) parts.push('رقم: تحقّق منه على الصورة');
      return parts.join(' · ');
    },

    checkLabel(reason) {
      return CHECK_LABELS[reason] || reason || '';
    },
  }));
});
