// «البحث والتحقق» (D107, D108; templates/research/research.html, research/api.py). One small component a part:
//   researchTabs()     – البحث / التحقق من نص / ربط مساعد; the address's #search / #verify / #connect opens one
//   researchSearch()   – the query, kind and book → hits (POST api:research_search), «المزيد», a hit's clip,
//                        «نسخ الإحالة», the page in review
//   researchVerify()   – a quotation, its book and attribution → the status card, the book's passage word by
//                        word, the clip and the citation (POST api:research_verify); «جرّب مثالًا» fills a
//                        sentence of the account's own text (GET api:research_example) and checks it
//   researchConnect()  – connect an assistant: pick the client, make a key for it (shown once; the clients that
//                        take only a URL get the secret URL `/mcp/k/<key>`), copy exactly what it needs, the
//                        connections with «إلغاء»
// Each reads the page's config (`#research-config`: urls, mcp_url, public_local, books, rate_limit, clip_days).
// The helpers at the bottom are pure (window.NassakhResearch) so they can be tested without a browser.
(function () {
  'use strict';

  const root = typeof window !== 'undefined' ? window : globalThis;
  const hasDOM = typeof document !== 'undefined';
  const TABS = ['search', 'verify', 'connect'];
  const PAGE = 20;
  const PLACEHOLDER = 'nsk_…'; // where the key goes until one is made

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
  const MODE_LABEL = { phrase: 'عبارة', all_words: 'الكلمات متفرقة في الصفحة', fuzzy: 'تقريبي' };
  const REVIEW_LABEL = { reviewed: 'مراجَع', partly_reviewed: 'مراجَع جزئيًا', unreviewed: 'غير مراجَع بعد' };
  // the five verdicts: a tone (the dot and the label's colour) and a short label; the sentence is the server's
  const STATUS = {
    exact: { tone: 'success', label: 'مطابق' },
    differs: { tone: 'danger', label: 'مختلف' },
    needs_image_check: { tone: 'warning', label: 'يحتاج مطابقة مع الصورة' },
    not_found: { tone: 'neutral', label: 'لم يوجد' },
    too_short: { tone: 'neutral', label: 'قصير' },
  };
  const CHANGE_LABEL = { replaced: 'كلمة مختلفة', missing: 'ناقص من النص', added: 'زائد في النص', moved: 'في غير موضعه' };
  const RESULTS = ['نتيجة واحدة', 'نتيجتان', 'نتائج', 'نتيجة'];
  const PLACES = ['موضع واحد', 'موضعان', 'مواضع', 'موضعًا'];

  // Arabic counting with Western digits (D6): forms = [one, two, 3–10, 11+]
  function countLabel(count, forms) {
    const n = Number(count) || 0;
    if (n === 1) return forms[0];
    if (n === 2) return forms[1];
    if (n >= 3 && n <= 10) return `${n} ${forms[2]}`;
    return `${n} ${forms[3]}`;
  }

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

  // The verify diff as display pieces: [{key, kind, text, quote, image}] in the book's order
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

  // ---------------------------------------------------------------- connecting an assistant
  // The clients the page walks through. `urlOnly`: the client takes a URL and nothing else (Claude's and
  // ChatGPT's custom connectors), so it gets the secret URL; the others take the plain URL and the header.
  // `keyName`: the connection's name as proposed.
  const CLIENTS = [
    { id: 'claude', name: 'Claude', sub: 'claude.ai وتطبيق سطح المكتب', urlOnly: true, keyName: 'Claude' },
    { id: 'chatgpt', name: 'ChatGPT', sub: 'وضع المطوّر', urlOnly: true, keyName: 'ChatGPT' },
    { id: 'code', name: 'Claude Code', sub: 'الطرفية', urlOnly: false, keyName: 'Claude Code' },
    { id: 'cursor', name: 'Cursor', sub: 'mcp.json', urlOnly: false, keyName: 'Cursor' },
    { id: 'vscode', name: 'VS Code', sub: '.vscode/mcp.json', urlOnly: false, keyName: 'VS Code' },
    { id: 'other', name: 'عميل آخر', sub: 'عنوان وترويسة', urlOnly: false, keyName: '' },
  ];
  const SECRET_NOTE = 'الرابط يحمل مفتاحك: احفظه كما تحفظ كلمة المرور، وألغِه من «الاتصالات» إن تسرّب.';
  const LOCAL_NOTE = 'العنوان الحالي محلي؛ يعمل هذا الخيار بعد نشر الموقع على عنوان عام (https).';
  const FREE_PLAN_NOTE = 'غير متاح في الخطة المجانية.';
  // The steps after «أنشئ مفتاحًا» (the page's first step), one client at a time. `paste`: which of
  // `pastes()` the step copies. Menu names as the clients print them, verified 2026-10-04 (Claude's connector
  // docs, OpenAI's developer-mode guide, Claude Code's and VS Code's MCP docs, Cursor's mcp.json docs).
  const STEPS = {
    claude: [
      { html: 'في Claude: الإعدادات (Settings) ← الموصِّلات (Connectors) ← أضف موصِّلًا مخصَّصًا (Add custom connector).' },
      { html: 'الاسم: نسّاخ. الرابط (Remote MCP server URL):', paste: 'secret', note: SECRET_NOTE },
      { html: 'أضِف (Add). في المحادثة: زر + ← الموصِّلات (Connectors) ← فعِّل «نسّاخ».' },
    ],
    chatgpt: [
      {
        html: 'فعِّل وضع المطوّر (Developer mode): الإعدادات (Settings) ← الأمان وتسجيل الدخول (Security and login). في حسابات العمل: Settings ← Apps & Connectors ← Advanced settings.',
        note: FREE_PLAN_NOTE,
      },
      { html: 'الموصِّلات (Connectors) ← إنشاء (Create / +): الاسم نسّاخ، ووصف قصير، ورابط الخادم (MCP server URL):', paste: 'secret', note: SECRET_NOTE },
      { html: 'المصادقة (Authentication): بلا (No authentication)، ثم إنشاء (Create). في المحادثة: + ← وضع المطوّر (Developer mode) ← نسّاخ.' },
    ],
    code: [
      { html: 'نفّذ في الطرفية مرة واحدة:', paste: 'code' },
      { html: 'للتأكد اكتب <code dir="ltr">/mcp</code> في Claude Code؛ يظهر <code dir="ltr">nassakh</code> متصلًا.' },
    ],
    cursor: [
      { html: 'أضِف إلى <code dir="ltr">~/.cursor/mcp.json</code> (لكل المشاريع) أو <code dir="ltr">.cursor/mcp.json</code> (لهذا المشروع):', paste: 'cursor' },
      { html: 'يظهر <code dir="ltr">nassakh</code> في إعدادات Cursor ← MCP؛ فعِّله إن كان متوقفًا.' },
    ],
    vscode: [
      { html: 'أضِف إلى <code dir="ltr">.vscode/mcp.json</code> في المشروع (أو ملف المستخدم من الأمر <code dir="ltr">MCP: Open User Configuration</code>):', paste: 'vscode' },
      { html: 'ابدأه بزر Start فوق الخادم في الملف، أو من لوحة الأوامر: <code dir="ltr">MCP: List Servers</code>.' },
    ],
    other: [
      { html: 'العنوان (Streamable HTTP):', paste: 'url' },
      { html: 'الترويسة مع كل طلب:', paste: 'header' },
      { html: 'أو بصيغة <code dir="ltr">mcpServers</code> التي تقرؤها أكثر الأدوات:', paste: 'http' },
    ],
  };

  // Exactly what each step pastes, the key filled in when there is one (`PLACEHOLDER` until then)
  function pastes(url, key) {
    const secret = key || PLACEHOLDER;
    const headers = { Authorization: `Bearer ${secret}` };
    const json = (value) => JSON.stringify(value, null, 2);
    return {
      url,
      secret: `${url}/k/${secret}`,
      header: `Authorization: Bearer ${secret}`,
      code: `claude mcp add --transport http nassakh ${url} --header "Authorization: Bearer ${secret}"`,
      http: json({ mcpServers: { nassakh: { type: 'http', url, headers } } }),
      cursor: json({ mcpServers: { nassakh: { url, headers } } }),
      vscode: json({ servers: { nassakh: { type: 'http', url, headers } } }),
    };
  }

  // A client's steps: `[{key, html, paste, note}]`; a URL-only client whose public URL is local is told so
  function steps(client, publicLocal) {
    const info = CLIENTS.find((c) => c.id === client) || CLIENTS[0];
    const list = (STEPS[info.id] || []).map((step, n) => ({ key: `${info.id}-${n}`, paste: '', note: '', ...step }));
    if (info.urlOnly && publicLocal) {
      const target = list.find((step) => step.paste === 'secret');
      if (target) target.note = [target.note, LOCAL_NOTE].filter(Boolean).join(' ');
    }
    return list;
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
      reviewLabel: REVIEW_LABEL,
      pageLabel,
      matchWords,
      get more() { return Boolean(this.result && this.hits.length < this.result.total); },
      // «نتيجتان لـ«…»»; with nothing found, one line on what to try
      get countText() {
        if (!this.result) return '';
        const q = String(this.result.query || '').trim();
        if (!this.result.total) {
          const wider = this.result.kind !== 'all' || Boolean(this.book) ? ' جرّب كلمات أقل، أو ابحث في كل الكتب والأنواع.' : ' جرّب كلمات أقل.';
          return `لا نتائج لـ«${q}».${wider}`;
        }
        return `${countLabel(this.result.total, RESULTS)} لـ«${q}»`;
      },
      // a quiet badge on a hit that is not the phrase itself
      modeText(hit) {
        if (hit.mode === 'fuzzy') return `${MODE_LABEL.fuzzy} ${Math.round(hit.score)}٪`;
        return MODE_LABEL[hit.mode] || '';
      },
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
      // «الكتاب · ص 45 · المتن · مراجَع · تطابق الكلمات 92٪»
      get metaText() {
        if (!this.found) return '';
        const r = this.result;
        const parts = [r.book && r.book.title, pageLabel(r.page, r.end_page), KIND_LABEL[r.kind], REVIEW_LABEL[r.review_state]];
        if (r.status !== 'exact') parts.push(`تطابق الكلمات ${Math.round(r.ratio * 100)}٪`);
        return parts.filter(Boolean).join(' · ');
      },
      get changesTitle() {
        const n = this.result && this.result.changes ? this.result.changes.length : 0;
        return n ? `الاختلافات: ${countLabel(n, PLACES)}` : '';
      },
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
      // «جرّب مثالًا»: a sentence of the account's own text, checked at once
      async example() {
        if (this.busy || !cfg.urls || !cfg.urls.example) return;
        this.busy = true;
        this.error = '';
        const r = await api(cfg.urls.example);
        this.busy = false;
        if (!r.ok || !r.data || !r.data.quote) { toast('لا نص كافٍ للمثال بعد.'); return; }
        this.quote = r.data.quote;
        this.book = '';
        this.attributed = 'unknown';
        await this.run();
      },
      clear() { this.quote = ''; this.result = null; this.error = ''; },
      cite() { return this.result && this.result.citation ? copyRaw(this.result.citation) : false; },
    };
  }

  function researchConnect() {
    const cfg = config();
    return {
      url: cfg.mcp_url || '',
      publicLocal: Boolean(cfg.public_local),
      clients: CLIENTS,
      client: 'claude',
      keys: [],
      loaded: false,
      name: CLIENTS[0].keyName,
      nameTouched: false, // the user typed a name: picking another client keeps it
      busy: false,
      error: '',
      fresh: null, // {key, secret, secretUrl}: the key just made, shown once
      asking: null,
      dateLabel,
      get current() { return CLIENTS.find((c) => c.id === this.client) || CLIENTS[0]; },
      get secret() { return this.fresh ? this.fresh.secret : null; },
      get pastes() {
        const out = pastes(this.url, this.secret);
        if (this.fresh && this.fresh.secretUrl) out.secret = this.fresh.secretUrl;
        return out;
      },
      get steps() { return steps(this.client, this.publicLocal); },
      get active() { return this.keys.filter((k) => k.active); },
      async init() { await this.load(); },
      pick(id) {
        if (!CLIENTS.some((c) => c.id === id)) return;
        this.client = id;
        if (!this.nameTouched) this.name = this.current.keyName;
      },
      async load() {
        const r = await api(cfg.urls.keys);
        if (!r.ok || !r.data) { this.error = r.message; return; }
        this.keys = r.data.keys || [];
        if (r.data.mcp_url) this.url = r.data.mcp_url;
        if (typeof r.data.public_local === 'boolean') this.publicLocal = r.data.public_local;
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
        this.fresh = { key: r.data.key, secret: r.data.secret, secretUrl: r.data.secret_url || '' };
        if (r.data.mcp_url) this.url = r.data.mcp_url;
        this.keys = [r.data.key].concat(this.keys);
        this.nameTouched = false;
        this.name = this.current.keyName;
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

  root.NassakhResearch = { pageLabel, dateLabel, matchWords, diffPieces, countLabel, pastes, steps, CLIENTS, STATUS };

  if (hasDOM) {
    document.addEventListener('alpine:init', () => {
      Alpine.data('researchTabs', researchTabs);
      Alpine.data('researchSearch', researchSearch);
      Alpine.data('researchVerify', researchVerify);
      Alpine.data('researchConnect', researchConnect);
    });
  }
})();
