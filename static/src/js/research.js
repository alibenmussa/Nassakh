// «البحث والتحقق» (D107, D108; templates/research/research.html, research/api.py). Small components, one per tab:
//   researchTabs()     – the three tabs; the address's #search / #verify / #connect opens one
//   researchSearch()   – the query, kind and book → hits (POST api:research_search), «المزيد», a hit's clip,
//                        «نسخ الإحالة», the page in review
//   researchVerify()   – a quotation, its book and attribution → the status card, the diff word by word, the
//                        clip and the citation (POST api:research_verify)
//   researchKeys()     – access keys for an MCP client: make one (shown once), copy the ready snippets
//                        (Claude Code, Claude Desktop, any Streamable HTTP client), revoke one
// Each reads the page's config (`#research-config`: urls, mcp_url, books, rate_limit, clip_days). The helpers
// at the bottom are pure (window.NassakhResearch) so they can be tested without a browser.
(function () {
  'use strict';

  const root = typeof window !== 'undefined' ? window : globalThis;
  const hasDOM = typeof document !== 'undefined';
  const TABS = ['search', 'verify', 'connect'];
  const PAGE = 20;

  const config = () => {
    if (!hasDOM) return {};
    const el = document.getElementById('research-config');
    try { return el ? JSON.parse(el.textContent) : {}; } catch (_) { return {}; }
  };
  const csrfToken = () => {
    const meta = hasDOM ? document.querySelector('meta[name="csrf-token"]') : null;
    return (meta && meta.content) || '';
  };
  const toast = (message) => { if (root.Nassakh && root.Nassakh.toast) root.Nassakh.toast(message); };

  // fetch wrapper: never throws; `{ ok, status, data, message }` with an Arabic message on failure
  async function api(url, opts = {}) {
    const method = opts.method || 'GET';
    const init = { method, credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } };
    if (method !== 'GET') {
      init.headers['Content-Type'] = 'application/json';
      init.headers['X-CSRFToken'] = csrfToken();
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
    let message = data && typeof data === 'object' && (data.detail || data.message);
    if (!message) {
      if (response.status === 401 || response.status === 403) message = 'انتهت الجلسة. سجّل الدخول من جديد.';
      else message = 'تعذّر تنفيذ الطلب. حاول مرة أخرى.';
    }
    return { ok: response.ok, status: response.status, data, message };
  }

  // Copy text as it is (indentation kept: the config snippets are JSON); «تم النسخ» or «تعذّر النسخ».
  async function copyRaw(text) {
    let ok = false;
    try {
      if (navigator.clipboard && root.isSecureContext) {
        await navigator.clipboard.writeText(text);
        ok = true;
      }
    } catch (_) { ok = false; }
    if (!ok && hasDOM) {
      const area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
      area.remove();
    }
    toast(ok ? 'تم النسخ' : 'تعذّر النسخ');
    return ok;
  }

  // ================================================================ pure helpers

  const KIND_LABEL = { body: 'المتن', notes: 'الحاشية' };
  const MODE_LABEL = { phrase: 'عبارة مطابقة', all_words: 'كل الكلمات في الصفحة', fuzzy: 'تقريبي' };
  const REVIEW_LABEL = { reviewed: 'مراجَع', partly_reviewed: 'مراجَع جزئيًا', unreviewed: 'غير مراجَع بعد' };
  const STATUS = {
    exact: { tone: 'success', icon: 'i-check-circle', label: 'مطابق' },
    differs: { tone: 'danger', icon: 'i-alert', label: 'مختلف' },
    needs_image_check: { tone: 'warning', icon: 'i-image', label: 'يحتاج مطابقة مع الصورة' },
    not_found: { tone: 'neutral', icon: 'i-search', label: 'لم يوجد' },
    too_short: { tone: 'neutral', icon: 'i-info', label: 'قصير جدًا' },
  };
  const CHANGE_LABEL = { replaced: 'كلمة مختلفة', missing: 'ناقص من النص', added: 'زائد في النص', moved: 'في غير موضعه' };

  // «ص 45» (the printed number) or «صفحة المسح 16»; «ص 45–46» over a page break
  function pageLabel(page, end) {
    if (!page) return '';
    if (page.printed) {
      const tail = end && end.printed && end.number !== page.number ? `–${end.printed}` : '';
      return `ص ${page.printed}${tail}`;
    }
    return `صفحة المسح ${page.number}`;
  }

  // a date in Arabic with Western digits (D6)
  function dateLabel(iso) {
    if (!iso) return '';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '';
    try {
      return new Intl.DateTimeFormat('ar-u-nu-latn', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
    } catch (_) {
      return date.toISOString().slice(0, 16).replace('T', ' ');
    }
  }

  // The matched words of a hit as spans: a doubtful reading underlined in amber
  function matchWords(hit) {
    return (hit && Array.isArray(hit.words) ? hit.words : []).map((w, n) => ({ key: n, text: w.text, doubtful: w.state === 'doubtful' }));
  }

  // The verify diff as display pieces: [{key, kind, text, quote, note}] in the book's order
  //   equal → kind 'same'; replaced → 'replaced' (the book's text and the quotation's); missing → 'missing';
  //   added → 'added'; moved → 'moved'. `image`: the reading there is doubtful (amber, not red).
  function diffPieces(diff) {
    return (Array.isArray(diff) ? diff : []).map((seg, n) => {
      const base = { key: n, image: Boolean(seg.needs_image) };
      if (seg.op === 'equal') return { ...base, kind: 'same', text: seg.source };
      if (seg.op === 'replaced') return { ...base, kind: 'replaced', text: seg.source, quote: seg.quote };
      if (seg.op === 'missing') return { ...base, kind: 'missing', text: seg.source };
      if (seg.op === 'added') return { ...base, kind: 'added', text: seg.quote };
      return { ...base, kind: 'moved', text: seg.source || seg.quote };
    });
  }

  // The three ready configurations of an MCP client (the key filled in when there is one)
  function snippets(url, key) {
    const secret = key || 'nsk_…';
    return {
      code: `claude mcp add --transport http nassakh ${url} --header "Authorization: Bearer ${secret}"`,
      desktop: JSON.stringify({
        mcpServers: {
          nassakh: {
            command: 'npx',
            args: ['-y', 'mcp-remote', url, '--header', 'Authorization:${NASSAKH_AUTH}'],
            env: { NASSAKH_AUTH: `Bearer ${secret}` },
          },
        },
      }, null, 2),
      http: JSON.stringify({
        mcpServers: {
          nassakh: { type: 'http', url, headers: { Authorization: `Bearer ${secret}` } },
        },
      }, null, 2),
    };
  }

  // ================================================================ components

  function researchTabs() {
    return {
      tab: 'search',
      init() {
        const hash = hasDOM ? String(root.location.hash || '').replace('#', '') : '';
        if (TABS.includes(hash)) this.tab = hash;
      },
      open(name) {
        if (!TABS.includes(name)) return;
        this.tab = name;
        if (hasDOM && root.history && root.history.replaceState) root.history.replaceState(null, '', `#${name}`);
      },
      // arrow keys move between the tabs (in reading order: right to left)
      key(event) {
        const at = TABS.indexOf(this.tab);
        const step = { ArrowLeft: 1, ArrowRight: -1 }[event.key];
        if (step === undefined) return;
        event.preventDefault();
        const next = TABS[(at + step + TABS.length) % TABS.length];
        this.open(next);
        if (hasDOM) { const el = document.getElementById(`rs-tab-${next}`); if (el) el.focus(); }
      },
    };
  }

  function researchSearch() {
    const cfg = config();
    return {
      books: cfg.books || [],
      query: '',
      kind: 'all',
      book: '',
      busy: false,
      error: '',
      result: null,
      hits: [],
      openClip: null,
      kindLabel: KIND_LABEL,
      modeLabel: MODE_LABEL,
      reviewLabel: REVIEW_LABEL,
      pageLabel,
      matchWords,
      get more() { return Boolean(this.result && this.hits.length < this.result.total); },
      async run(append = false) {
        const query = this.query.trim();
        if (!query || this.busy) return;
        this.busy = true;
        this.error = '';
        const body = { query, kind: this.kind, limit: PAGE, offset: append ? this.hits.length : 0 };
        if (this.book) body.book_ids = [Number(this.book)];
        const r = await api(cfg.urls.search, { method: 'POST', body });
        this.busy = false;
        if (!r.ok || !r.data) { this.error = r.message; return; }
        this.result = r.data;
        this.hits = append ? this.hits.concat(r.data.hits) : r.data.hits;
        if (!append) this.openClip = null;
      },
      toggleClip(hit) { this.openClip = this.openClip === hit.passage_id ? null : hit.passage_id; },
      cite(hit) { return copyRaw(hit.citation); },
    };
  }

  function researchVerify() {
    const cfg = config();
    return {
      books: cfg.books || [],
      quote: '',
      book: '',
      attributed: 'unknown',
      busy: false,
      error: '',
      result: null,
      status: STATUS,
      kindLabel: KIND_LABEL,
      reviewLabel: REVIEW_LABEL,
      changeLabel: CHANGE_LABEL,
      pageLabel,
      get pieces() { return diffPieces(this.result && this.result.diff); },
      get tone() { return this.result ? (STATUS[this.result.status] || STATUS.not_found).tone : 'neutral'; },
      get found() { return Boolean(this.result && this.result.passage_id); },
      async run() {
        const quote = this.quote.trim();
        if (!quote || this.busy) return;
        this.busy = true;
        this.error = '';
        const body = { quote, attributed_to: this.attributed };
        if (this.book) body.book_id = Number(this.book);
        const r = await api(cfg.urls.verify, { method: 'POST', body });
        this.busy = false;
        if (!r.ok || !r.data) { this.error = r.message; return; }
        this.result = r.data;
        if (hasDOM) {
          requestAnimationFrame(() => {
            const card = document.getElementById('rs-verdict');
            if (card && card.focus) card.focus({ preventScroll: false });
          });
        }
      },
      clear() { this.quote = ''; this.result = null; this.error = ''; },
      cite() { return this.result && this.result.citation ? copyRaw(this.result.citation) : false; },
    };
  }

  function researchKeys() {
    const cfg = config();
    return {
      url: cfg.mcp_url || '',
      rateLimit: cfg.rate_limit || 60,
      keys: [],
      loaded: false,
      name: '',
      busy: false,
      error: '',
      fresh: null, // {key, secret}: the key just made, shown once
      client: 'code',
      asking: null,
      dateLabel,
      get snippet() { return snippets(this.url, this.fresh && this.fresh.secret)[this.client]; },
      get active() { return this.keys.filter((k) => k.active); },
      async init() { await this.load(); },
      async load() {
        const r = await api(cfg.urls.keys);
        if (!r.ok || !r.data) { this.error = r.message; return; }
        this.keys = r.data.keys || [];
        if (r.data.mcp_url) this.url = r.data.mcp_url;
        this.loaded = true;
      },
      async create() {
        const name = this.name.trim();
        if (!name || this.busy) return;
        this.busy = true;
        this.error = '';
        const r = await api(cfg.urls.keys, { method: 'POST', body: { name } });
        this.busy = false;
        if (!r.ok || !r.data) { this.error = r.message; return; }
        this.fresh = { key: r.data.key, secret: r.data.secret };
        this.name = '';
        this.keys = [r.data.key].concat(this.keys);
      },
      forget() { this.fresh = null; },
      ask(key) { this.asking = key; },
      async revoke() {
        const key = this.asking;
        if (!key || this.busy) return;
        this.busy = true;
        const r = await api(String(cfg.urls.revoke).replace('__id__', String(key.id)), { method: 'POST' });
        this.busy = false;
        this.asking = null;
        if (!r.ok || !r.data) { toast(r.message); return; }
        this.keys = this.keys.map((k) => (k.id === key.id ? r.data.key : k));
        if (this.fresh && this.fresh.key.id === key.id) this.fresh = null;
        toast('أُلغي المفتاح');
      },
      copy(text) { return copyRaw(text); },
    };
  }

  root.NassakhResearch = { pageLabel, dateLabel, matchWords, diffPieces, snippets, STATUS };

  if (hasDOM) {
    document.addEventListener('alpine:init', () => {
      Alpine.data('researchTabs', researchTabs);
      Alpine.data('researchSearch', researchSearch);
      Alpine.data('researchVerify', researchVerify);
      Alpine.data('researchKeys', researchKeys);
    });
  }
})();
