// Alpine components for the ocr app: the text panel of the page detail screen.
// These scripts run before the deferred Alpine bundle: register components inside
// document.addEventListener('alpine:init', () => { Alpine.data('name', () => ({ ... })) });

document.addEventListener('alpine:init', () => {
  // Page statuses during which the pipeline is still working on the page.
  const ACTIVE_STATUSES = ['uploaded', 'preprocessed', 'layout_done'];
  const POLL_MS = 2000;
  const FIRST_TEXT_POLL_MS = 1000;
  const MAX_POLL_MS = 15000;
  const FAILURES_BEFORE_NOTICE = 3;
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
   * textPanel({ statusUrl, textUrl, runsUrl, pageId })
   * Initial data comes from the <script type="application/json"> that the partial embeds
   * (same shape as the /text/ endpoint plus `runs`). Polling is a self-rescheduling timeout, so a
   * request never starts before the previous one finished; it stops for good on 401/403.
   * Every status poll is re-broadcast as `nassakh:page-state` so the page's state card stays in step.
   * Provisional text is shown through the decode effect (window.NassakhDecode, D23): while the page is
   * still working the words oscillate between noise and Tesseract's letters; when the final lines arrive a
   * right-to-left wave lands them, and only then does the final-lines markup take over (`resolving`).
   */
  Alpine.data('textPanel', (config) => ({
    status: '',
    textState: 'none',
    provisional: '',
    finalText: '',
    flags: [],
    error: '',
    errorFrom: '',
    stageLabels: {},
    lines: [],
    runs: [],
    nLow: 0,
    active: null, // server-side `active` flag when known; falls back to ACTIVE_STATUSES
    polling: false,
    fetchFailed: false,
    authLost: false,
    failures: 0,
    timer: null,
    ticks: 0,
    resolving: false, // the resolve wave is playing: keep showing the decode host until it lands
    decodeMode: '', // what the decode host currently shows: '' | 'noise' | 'provisional' | 'static'

    init() {
      const script = this.$root.querySelector('script[type="application/json"]:not([id])');
      if (script) {
        try {
          this.apply(JSON.parse(script.textContent));
        } catch (e) {
          // Malformed payload: the first poll fills the panel instead.
        }
      }
      const stages = document.getElementById('text-panel-stages');
      if (stages) {
        try { this.stageLabels = JSON.parse(stages.textContent) || {}; } catch (e) { this.stageLabels = {}; }
      }
      if (config.errorFrom) this.errorFrom = config.errorFrom;
      if (this.isActive) this.start();
      if (this.$nextTick) this.$nextTick(() => this.afterUpdate());
    },

    destroy() {
      this.stop();
    },

    get isActive() {
      if (typeof this.active === 'boolean') return this.active;
      return ACTIVE_STATUSES.includes(this.status);
    },

    // What the text stage shows: the final markup waits for the resolve wave to land.
    get view() {
      return this.resolving ? 'provisional' : this.textState;
    },
    get showDecode() {
      return this.view === 'provisional' || (this.view === 'none' && this.isActive);
    },
    // Polite announcement for screen readers (the scrambled text itself is aria-hidden).
    get liveText() {
      if (this.view === 'final') return this.lines.length ? 'وصل النص النهائي.' : '';
      if (this.view === 'provisional') return this.isActive ? 'النص قيد التعرّف.' : 'نص Tesseract المبدئي.';
      return this.isActive ? 'قيد المعالجة، لا نص بعد.' : '';
    },

    // Called after every state change: keep the decode host in step, or play the resolve wave.
    afterUpdate() {
      const el = this.$refs && this.$refs.decode;
      const D = window.NassakhDecode;
      if (!el || !D || this.resolving) return;
      if (this.textState === 'final') {
        if (this.decodeMode && this.decodeMode !== 'static' && this.lines.length) {
          this.playResolve(el, D);
        } else {
          this.clearDecode(el, D);
        }
        return;
      }
      if (this.textState === 'provisional' && this.provisional) {
        const mode = this.isActive ? 'provisional' : 'static';
        if (this.decodeMode === mode) D.update(el, { text: this.provisional, static: mode === 'static' });
        else D.attach(el, { text: this.provisional, mode: 'provisional', static: mode === 'static' });
        this.decodeMode = mode;
        return;
      }
      if (this.textState === 'none' && this.isActive) {
        if (this.decodeMode !== 'noise') D.attach(el, { mode: 'noise', lines: 8 });
        this.decodeMode = 'noise';
        return;
      }
      this.clearDecode(el, D);
    },
    playResolve(el, D) {
      this.resolving = true;
      const lines = this.lines.map((line) => ({ region_kind: line.region_kind || '', tokens: line.tokens || [] }));
      D.resolve(el, {
        lines,
        onDone: () => {
          this.resolving = false;
          this.decodeMode = '';
          // the final-lines markup has taken over (200 ms crossfade); free the host afterwards
          setTimeout(() => { if (!this.decodeMode) { D.detach(el); el.textContent = ''; } }, 400);
        },
      });
    },
    clearDecode(el, D) {
      if (!this.decodeMode) return;
      D.detach(el);
      el.textContent = '';
      this.decodeMode = '';
    },

    // Designed failure: the first line is the Arabic headline, the rest is technical detail.
    get errorHeadline() {
      return this.error.split('\n')[0].trim();
    },
    get errorDetail() {
      return this.error.split('\n').slice(1).join('\n').trim();
    },
    get errorStageLabel() {
      return (this.errorFrom && this.stageLabels[this.errorFrom]) || '';
    },

    // Final text when there is one, else the provisional Tesseract text.
    get copyable() {
      if (this.textState === 'final') return this.finalText || this.lines.map((l) => l.text || '').join('\n');
      return this.textState === 'provisional' ? this.provisional : '';
    },
    copy() {
      return window.Nassakh.copyText(this.copyable);
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
      if (data.error !== undefined) {
        // The API sends the Arabic headline in `error` and the technical detail in `error_detail`.
        const detail = data.error_detail ? String(data.error_detail).trim() : '';
        this.error = [this.errorText(data.error), detail].filter(Boolean).join('\n');
      }
      if (data.error_from !== undefined) this.errorFrom = data.error_from || '';
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
      if (this.polling) return;
      this.polling = true;
      this.schedule();
    },

    stop() {
      clearTimeout(this.timer);
      this.timer = null;
      this.polling = false;
    },

    // Faster while waiting for the first (provisional) text; back off after failures.
    schedule() {
      clearTimeout(this.timer);
      if (!this.polling) return;
      const base = this.textState === 'none' ? FIRST_TEXT_POLL_MS : POLL_MS;
      const delay = Math.min(base * (1 + this.failures), MAX_POLL_MS);
      this.timer = setTimeout(() => this.poll(), delay);
    },

    // null when the session ended (polling is stopped); throws on other HTTP errors.
    async getJson(url) {
      const response = await fetch(url, {
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
        cache: 'no-store',
      });
      if (response.status === 401 || response.status === 403) {
        this.stop();
        this.authLost = true;
        this.fetchFailed = true;
        return null;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    },

    async poll() {
      if (!this.polling) return;
      if (document.hidden) {
        this.schedule();
        return;
      }
      this.ticks += 1;
      try {
        const data = await this.getJson(config.statusUrl);
        if (!data) return;
        const changed = data.status !== this.status || (data.text_state || 'none') !== this.textState;
        const statusFields = {
          status: data.status,
          active: data.active,
          text_state: data.text_state,
          provisional_text: data.provisional_text,
          final_text: data.final_text,
          flags: data.flags,
          error: data.error,
          error_detail: data.error_detail,
          error_from: data.error_from,
        };
        if (changed || !data.active) {
          // Load the new lines first so the final text settles in one crossfade.
          const [text, runs] = await Promise.allSettled([this.getJson(config.textUrl), this.getJson(config.runsUrl)]);
          this.apply(statusFields);
          if (text.status === 'fulfilled') this.apply(text.value);
          if (runs.status === 'fulfilled' && runs.value) this.apply({ runs: runs.value.runs });
        } else {
          this.apply(statusFields);
          if (this.ticks % RUNS_EVERY === 0) await this.refreshRuns();
        }
        this.afterUpdate();
        window.dispatchEvent(new CustomEvent('nassakh:page-state', { detail: data }));
        this.failures = 0;
        this.fetchFailed = false;
        if (!this.isActive) this.stop();
      } catch (e) {
        this.failures += 1;
        this.fetchFailed = this.failures >= FAILURES_BEFORE_NOTICE;
      }
      this.schedule();
    },

    async refreshRuns() {
      try {
        const data = await this.getJson(config.runsUrl);
        if (data) this.apply({ runs: data.runs });
      } catch (e) {
        // transient; the next poll retries
      }
    },

    hover: { tok: null, style: '' },
    hideTimer: null,

    showTok(ev, tok) {
      clearTimeout(this.hideTimer);
      const host = this.$root.querySelector('.text-stage');
      const box = ev.currentTarget.getBoundingClientRect();
      const ref = host ? host.getBoundingClientRect() : { top: 0, right: box.right };
      // anchored under the word, aligned to its right edge (RTL start)
      const top = Math.round(box.bottom - ref.top + 6);
      const right = Math.max(0, Math.round(ref.right - box.right));
      this.hover = { tok, style: `top:${top}px; right:${right}px;` };
    },

    keepTok() {
      clearTimeout(this.hideTimer);
    },

    hideTok(now = false) {
      clearTimeout(this.hideTimer);
      if (now) { this.hover = { tok: null, style: '' }; return; }
      this.hideTimer = setTimeout(() => { this.hover = { tok: null, style: '' }; }, 120);
    },

    tokenOptions(tok) {
      if (!tok) return [];
      const rows = [{ label: config.primaryLabel || 'النموذج الأول', value: tok.orig || tok.t }];
      if (tok.alt) rows.push({ label: config.secondaryLabel || 'النموذج الثاني', value: tok.alt });
      else if (!tok.digit) rows.push({ label: config.secondaryLabel || 'النموذج الثاني', value: '— لا مقابل' });
      if (tok.tess) rows.push({ label: 'Tesseract', value: tok.tess });
      return rows;
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
