// The book page's side panel and everything around the pages (PHASE5_SPEC §9.2, D47):
//   - one panel with an icon tab bar: الفصول (the footprint, the chapters, the page checks), الصفحات (the
//     filmstrip), بحث (find & replace over the chapter's nodes; matches with their pages), التنسيق (style.js),
//     الفقرة (the open block's style and page-break flags, its source pages), الأصل (the scan of the block with
//     its line bands, the pager, «عرض الأصل», the review link — as the editor page had it), غير المؤكَّدة (the
//     uncertain words by chapter and page; readings, a typed correction, accept, in place). Preview opens
//     التنسيق, edit الأصل; the owner's choice is remembered per mode.
//   - dialogs: the snapshots, the digit conversion, the shortcut sheet; the review drift and its re-assembly
//   - the keyboard maps of both modes (geometry.js keyAction); nothing fires inside a field
(function () {
  'use strict';

  const root = typeof window !== 'undefined' ? window : globalThis;
  const NS = (root.NassakhBook = root.NassakhBook || {});
  NS.parts = NS.parts || {};

  const TABS = ['chapters', 'pages', 'find', 'format', 'block', 'source', 'uncertain'];
  const TAB_KEY = 'nassakh.book.tab.';
  const DEFAULT_TAB = { preview: 'format', edit: 'source' };
  const FIND_MS = 200;
  const SOURCE_MS = 250;
  const FLASH_MS = 1400;
  const POLL_MS = 1000;
  const POLL_MAX_MS = 5000;
  const UNCERTAIN_REFRESH_MS = 1500;
  const MATCHES = ['مطابقة واحدة', 'مطابقتان', 'مطابقات', 'مطابقة'];
  const DIGITS = ['رقم واحد', 'رقمان', 'أرقام', 'رقمًا'];
  const PAGES = ['صفحة واحدة', 'صفحتان', 'صفحات', 'صفحة'];
  const MINUTES = ['دقيقة', 'دقيقتين', 'دقائق', 'دقيقة'];
  const HOURS = ['ساعة', 'ساعتين', 'ساعات', 'ساعة'];
  const DAYS = ['يوم', 'يومين', 'أيام', 'يومًا'];
  const CHECKS = { missing_font: 'خط غير مثبّت', footnote_overflow: 'حاشية أطول من صفحتها', almost_empty_page: 'صفحة شبه فارغة', heading_at_foot: 'عنوان في أسفل الصفحة' };

  function relativeTime(iso, now) {
    const G = NS.geo;
    const t = Date.parse(iso || '');
    if (!Number.isFinite(t)) return '';
    const seconds = Math.max(0, Math.round(((now || Date.now()) - t) / 1000));
    if (seconds < 45) return 'قبل لحظات';
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `قبل ${G.arCount(Math.max(1, minutes), MINUTES)}`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `قبل ${G.arCount(hours, HOURS)}`;
    const days = Math.round(hours / 24);
    if (days < 30) return `قبل ${G.arCount(days, DAYS)}`;
    return `في ${String(iso).slice(0, 10)}`;
  }
  NS.relativeTime = relativeTime;

  NS.parts.panel = function panel(ctx) {
    const U = NS.util;
    const G = NS.geo;
    const cfg = ctx.cfg;
    const urls = ctx.urls;
    const T = ctx.timers;
    const B = () => U.bundle();
    const sheetCache = new Map(); // scan page number → api:book_sheets item
    const chapterCache = new Map(); // chapter id → {version, nodes} (find in a chapter not being edited)
    let findGen = 0;
    let sourceGen = 0;
    let lastTrigger = null;
    let pollFailures = 0;
    let listHost = null;
    let listKey = '';
    const tabFor = (mode) => { const t = U.readLocal(TAB_KEY + mode, DEFAULT_TAB[mode]); return TABS.includes(t) ? t : DEFAULT_TAB[mode]; };

    return {
      tab: tabFor(cfg.mode === 'edit' && cfg.canEdit ? 'edit' : 'preview'),
      pointed: null, // the block last clicked in preview (the source tab follows it)
      pointedNode: null, // {key, id, node}: that block from its chapter's nodes when no chapter is being edited («الفقرة»)
      flash: null, // {n, block, i}: a line pointed at from a list, lit for a moment
      // find & replace
      find: { query: '', replacement: '', matchTashkeel: false, foldAlef: true, wholeWord: false, scope: 'chapter', chapter: null, results: [], index: -1, total: 0, groups: [], busy: false, loading: false },
      // the uncertain words
      uncertain: { loaded: false, loading: false, items: [], count: Number(cfg.uncertainCount) || 0, busy: '', error: '', open: null },
      // the source («الأصل»)
      source: { blockId: null, pages: [], index: 0, lines: [], sheet: null, loading: false, error: '' },
      drawerOpen: false,
      // dialogs
      snapshots: { open: false, list: [], busy: false, label: '', loading: false, error: '' },
      digits: { open: false, style: 'western', scope: 'chapter', busy: false },
      sheetOpen: false,
      // the review drift (D41)
      dismissedDrift: [],
      reassembly: { running: false, runId: null, error: '' },

      _init_panel() {
        if (this.uncertain.count && this.tab === 'uncertain') this.loadUncertain();
      },

      // ------------------------------------------------------------ tabs
      get tabs() {
        void this.uncertain.count; void this.find.total; void this.checks.length;
        return [
          { key: 'chapters', label: 'الفصول', icon: 'i-list', badge: this.checks.length ? String(this.checks.length) : '', warn: Boolean(this.checks.length) },
          { key: 'pages', label: 'الصفحات', icon: 'i-pages', badge: '' },
          { key: 'find', label: 'بحث', icon: 'i-search', badge: this.find.total ? String(this.find.total) : '' },
          { key: 'format', label: 'التنسيق', icon: 'i-sliders', badge: '' },
          { key: 'block', label: 'الفقرة', icon: 'i-pilcrow', badge: '' },
          { key: 'source', label: 'الأصل', icon: 'i-image', badge: '' },
          { key: 'uncertain', label: 'غير المؤكَّدة', icon: 'i-uncertain', badge: this.uncertain.count ? String(this.uncertain.count) : '', warn: Boolean(this.uncertain.count) },
        ];
      },
      setTab(key, opts = {}) {
        if (!TABS.includes(key)) return false;
        this.tab = key;
        if (!opts.quiet) U.writeLocal(TAB_KEY + this.mode, key);
        if (key === 'pages') this.centerFilm();
        if (key === 'uncertain' && !this.uncertain.loaded) this.loadUncertain();
        if (key === 'source') this.followSource(true);
        if (key === 'block' && this.pointed && !this.currentBlock) this.lookupPointed(this.pointed, this.current);
        if (key === 'chapters') this.renderChapters();
        if (key === 'find' && this.$nextTick) this.$nextTick(() => { const f = this.$refs && this.$refs.findQuery; if (f) { U.focus(f); if (f.select) f.select(); } });
        return true;
      },
      // ←/→ on the tab bar move between the tabs (RTL: the next tab is on the left)
      stepTab(dir) {
        const i = TABS.indexOf(this.tab);
        const next = TABS[(i + dir + TABS.length) % TABS.length];
        this.setTab(next);
        const focus = () => U.focus(U.q(ctx.dom.root || (U.hasDOM ? document.querySelector('[data-book]') : null), `[data-tab="${next}"]`));
        if (this.$nextTick) this.$nextTick(focus); else focus();
        return next;
      },
      // the mode changed: the panel shows that mode's tab (the owner's last choice in it)
      afterMode(mode) {
        this.setTab(tabFor(mode), { quiet: true });
      },
      showBookDetails() {
        this.setTab('format', { quiet: true });
        this.openSection('details');
        U.toast('صفحتا العنوان والحقوق تُكتبان من «بيانات الكتاب»');
      },
      // a line clicked in preview: the source tab follows its block
      pointAt(block, n) {
        if (!block || /^(front-|toc-|copyright)/.test(block)) return;
        this.pointed = block;
        if (this.tab === 'source') this.followSource();
        if (this.tab === 'block') return this.lookupPointed(block, n);
        return null;
      },
      // The block clicked in preview, from its chapter's nodes (the page's chapter, then its neighbours): «الفقرة»
      // describes it without edit mode (a proofreader too). A later click wins.
      async lookupPointed(block, n) {
        if (!B()) return null;
        if (B().locate(ctx.nodes, block) || B().findNote(ctx.nodes, block)) { this.pointedNode = null; return null; }
        const found = await this.blockFromChapters(block, n);
        if (!found || this.pointed !== block) return null;
        this.pointedNode = { key: block, id: (found.at.node.attrs && found.at.node.attrs.id) || block, node: found.at.node };
        return this.pointedNode;
      },
      // A block (or a note: its paragraph) from the chapters of page `n`: the page's chapter, then the next
      // and the previous one (a page where one section ends and the next begins) → {nodes, at} or null.
      async blockFromChapters(block, n) {
        const page = ctx.pages.get(n || this.current);
        const first = (page && page.chapter) || this.focusChapter;
        const i = this.chapters.findIndex((c) => c.id === first);
        const candidates = [first, i >= 0 && this.chapters[i + 1] ? this.chapters[i + 1].id : null, i > 0 ? this.chapters[i - 1].id : null].filter(Boolean);
        for (const cid of candidates) {
          const nodes = await this.nodesOf(cid);
          if (!nodes) continue;
          let at = B().locate(nodes, block);
          if (!at) { const note = B().findNote(nodes, block); if (note) at = B().locate(nodes, note.block); }
          if (at) return { nodes, at };
        }
        return null;
      },
      afterLand(n) {
        if (this.tab === 'chapters') this.renderChapters();
        if (this.tab === 'source' && !ctx.openId) this.followSource();
        void n;
      },
      afterSave() {
        if (this.uncertain.loaded) { clearTimeout(T.uncertain); T.uncertain = setTimeout(() => this.loadUncertain(), UNCERTAIN_REFRESH_MS); }
        if (this.find.query.trim() && this.find.scope === 'chapter') this.scheduleFind();
      },

      // ------------------------------------------------------------ الفصول: the chapters, the checks
      chapterRows() {
        return this.footprint.rows;
      },
      renderChapters() {
        // the component root saved by bindStage: `this.$el` is whichever element triggered the call (the tab button,
        // the list's own x-effect), so a lookup inside it finds nothing
        if (!listHost || !listHost.isConnected) listHost = U.q(ctx.dom.root || (U.hasDOM ? document.querySelector('[data-book]') : null), '[data-chapter-list]');
        if (listHost && listHost.childElementCount === 0) listKey = null; // emptied (re-rendered): paint again
        if (!listHost) return;
        const rows = this.chapterRows();
        const key = JSON.stringify(rows.map((r) => [r.id, r.first, r.last, r.delta, r.drift, r.current, r.title]));
        if (key === listKey) return;
        listKey = key;
        listHost.innerHTML = G.chapterRowsHtml(rows);
        const current = U.q(listHost, '.bp-ch.is-current');
        if (current && typeof current.scrollIntoView === 'function') current.scrollIntoView({ block: 'nearest' });
      },
      onChapterListClick(e) {
        const row = U.closest(e.target, '[data-cid]');
        if (!row) return;
        e.preventDefault();
        this.goToChapter(row.getAttribute('data-cid'));
      },
      get checkRows() {
        return this.checks.map((c, i) => ({ key: `${c.code}-${c.page}-${i}`, page: c.page, block: c.block, label: CHECKS[c.code] || c.code, message: c.message || '' }));
      },
      goToCheck(c) {
        if (!c || !c.page) return false;
        this.showPage(c.page, { manual: true });
        if (c.block) this.lightBlock(c.page, c.block);
        return true;
      },
      // a block (a heading from the contents page) to its page
      // (`line`: the contents line clicked; its printed page number when the heading's page is not held yet)
      goToBlock(block, line) {
        const c = G.caretLine(ctx.pages, block, 0);
        if (c) { this.showPage(c.n, { manual: true }); this.lightBlock(c.n, block, c.i); return true; }
        const r = this.ranges.find((x) => x.id === block);
        if (r) return this.showPage(r.first, { manual: true });
        const runs = (line && line.runs) || [];
        const printed = runs.length ? /^\d+$/.exec(String(runs[runs.length - 1].text || '').trim()) : null;
        if (printed) return this.showPage(Number(printed[0]), { manual: true });
        return false;
      },
      lightBlock(n, block, i) {
        this.flash = { n, block, i };
        this.paint();
        clearTimeout(T.flash);
        T.flash = setTimeout(() => { this.flash = null; this.paint(); }, FLASH_MS);
      },

      // ------------------------------------------------------------ decorations drawn on the pages
      // block → [{start, end, cls}] for page n: the find matches (the current one outlined), the uncertain words
      // (edit mode), the word picked in the uncertain tab.
      decorationsFor(n) {
        const out = new Map();
        const add = (block, start, end, cls) => { if (!block || end <= start) return; if (!out.has(block)) out.set(block, []); out.get(block).push({ start, end, cls }); };
        this.find.results.forEach((m, i) => { if (m.page === n || m.page === null) add(m.note || m.block, m.start, m.end, i === this.find.index ? 'lp-match is-current' : 'lp-match'); });
        if (this.mode === 'edit') this.uncertain.items.forEach((w) => add(w.note || w.block, w.start, w.end, 'lp-uncertain'));
        const picked = this.uncertain.open;
        if (picked) add(picked.note || picked.block, picked.start, picked.end, 'lp-picked');
        return out;
      },

      // ------------------------------------------------------------ بحث: find & replace
      findOptions() { return { matchTashkeel: this.find.matchTashkeel, foldAlef: this.find.foldAlef, wholeWord: this.find.wholeWord }; },
      get findCountText() {
        const f = this.find;
        if (!f.query.trim()) return '';
        if (f.loading) return 'يُبحث…';
        if (f.scope === 'book') return f.total ? `${f.total} في الكتاب` : 'لا مطابقات في الكتاب';
        if (!f.total) return 'لا مطابقات';
        return f.index >= 0 ? `${f.index + 1} من ${f.total}` : G.arCount(f.total, MATCHES);
      },
      onFindInput() { this.scheduleFind(); },
      scheduleFind() {
        clearTimeout(T.find);
        T.find = setTimeout(() => this.runFind(), FIND_MS);
      },
      setFindScope(scope) {
        this.find.scope = scope === 'book' ? 'book' : 'chapter';
        this.find.chapter = null;
        this.runFind();
      },
      // The nodes of a chapter: the edited chapter's own, else fetched (kept by version).
      async nodesOf(cid) {
        if (!cid) return null;
        if (ctx.loading && ctx.loading.cid === cid) await ctx.loading.promise;
        if (cid === this.editChapterId) return ctx.nodes;
        const cached = chapterCache.get(cid);
        const sum = this.summaryOf(cid);
        if (cached && (!sum || !sum.version || cached.version === sum.version)) return cached.nodes;
        const r = await U.api(U.fill(urls.chapter, cid));
        if (!r.ok || !r.data) return null;
        const nodes = r.data.content && Array.isArray(r.data.content.content) ? r.data.content.content : [];
        chapterCache.set(cid, { version: r.data.version, nodes });
        return nodes;
      },
      async runFind() {
        clearTimeout(T.find);
        const gen = (findGen += 1);
        const f = this.find;
        const query = f.query;
        if (!query.trim()) { Object.assign(f, { results: [], total: 0, index: -1, groups: [], loading: false }); this.paint(); return; }
        f.loading = true;
        if (f.scope === 'book') {
          const r = await U.api(urls.findReplace, { method: 'POST', body: { query, replacement: '', replace: false, match_tashkeel: f.matchTashkeel, fold_alef: f.foldAlef, whole_word: f.wholeWord } });
          if (gen !== findGen) return;
          f.loading = false;
          if (!r.ok || !r.data) { U.toast(r.message); return; }
          const groups = new Map();
          (r.data.matches || []).forEach((m) => { if (!groups.has(m.chapter)) groups.set(m.chapter, 0); groups.set(m.chapter, groups.get(m.chapter) + 1); });
          f.groups = [...groups.entries()].map(([id, count]) => { const range = this.ranges.find((c) => c.id === id); return { id, count, title: this.chapterTitle(id), first: range ? range.first : null, last: range ? range.last : null }; });
          f.total = Number(r.data.total) || 0;
          f.results = [];
          f.index = -1;
          this.paint();
          return;
        }
        const cid = f.chapter || this.focusChapter;
        const nodes = await this.nodesOf(cid);
        if (gen !== findGen) return;
        f.loading = false;
        if (!nodes || !B()) { f.results = []; f.total = 0; return; }
        const matches = B().findPlain(nodes, query, this.findOptions());
        // their pages: the chapter's pages fetched once (a chapter of 30 pages is one request)
        const range = this.ranges.find((c) => c.id === cid);
        if (range && [...Array(range.last - range.first + 1).keys()].some((k) => !ctx.pages.has(range.first + k))) {
          await this.fetchLayout(range.first, Math.min(range.last, range.first + 119));
          if (gen !== findGen) return;
        }
        const texts = new Map();
        const textOf = (id) => {
          if (!texts.has(id)) {
            const found = B().locate(nodes, id);
            let text = found ? B().plainText(found.node) : '';
            if (!found) { const note = B().findNote(nodes, id); text = note ? B().inlineText(note.note.content) : ''; }
            texts.set(id, text);
          }
          return texts.get(id);
        };
        f.results = matches.map((m) => {
          const at = G.caretLine(ctx.pages, m.note || m.block, m.start);
          return Object.assign({}, m, { chapter: cid, page: at ? at.n : null, line: at ? at.i : null, snippet: G.snippet(textOf(m.note || m.block), m.start, m.end) });
        });
        f.total = f.results.length;
        const current = this.current;
        const next = f.results.findIndex((m) => m.page !== null && m.page >= current);
        f.index = f.results.length ? (next >= 0 ? next : 0) : -1;
        this.paint();
      },
      findStep(dir) {
        const f = this.find;
        if (f.scope === 'book' || !f.total) return null;
        f.index = ((f.index < 0 ? (dir > 0 ? -1 : 0) : f.index) + dir + f.total) % f.total;
        return this.goToMatch(f.index);
      },
      goToMatch(i) {
        const m = this.find.results[i];
        if (!m) return null;
        this.find.index = i;
        if (m.page) {
          if (!this.sideOf(m.page)) this.showPage(m.page, { manual: true });
          this.lightBlock(m.page, m.note || m.block, m.line);
        } else this.paint();
        return m;
      },
      openFindGroup(g) {
        this.find.scope = 'chapter';
        this.find.chapter = g.id;
        if (g.first) this.showPage(g.first, { manual: true });
        return this.runFind();
      },
      onFindKey(e) {
        if (e.key === 'Enter') { e.preventDefault(); this.findStep(e.shiftKey ? -1 : 1); }
      },
      // «استبدال»: the current match, in the chapter's nodes (or in the open paragraph), saved and laid out.
      async replaceOne() {
        const f = this.find;
        const m = f.results[f.index];
        if (!m || !this.canEdit || f.scope === 'book' || !B()) return false;
        if (m.chapter !== this.editChapterId && !(await this.loadChapter(m.chapter))) return false;
        if (ctx.openId === m.block && ctx.ed && !m.note) {
          ctx.ed.replaceOffsets(m.start, m.end, f.replacement);
          this.onBlockChange();
          this.pause();
        } else {
          if (ctx.openId === m.block) await this.closeBlock({ commit: true });
          const at = B().locate(ctx.nodes, m.block);
          if (!at) return false;
          this.change(B().replaceBlock(ctx.nodes, m.block, [B().replaceInBlock(at.node, m.note, m.start, m.end, f.replacement)]));
          this.paint();
        }
        this.liveMessage = 'استُبدلت مطابقة واحدة';
        await this.runFind();
        return true;
      },
      // «استبدال الكل»: through the server (a snapshot first: the undo toast restores it), then laid out.
      async replaceAll() {
        const f = this.find;
        if (!this.canEdit || !f.query.trim() || f.busy) return 0;
        await this.closeBlock({ commit: true });
        await this.saveNow();
        if (this.editDirty) { U.toast('تعذّر الحفظ قبل الاستبدال'); return 0; }
        const cid = f.scope === 'book' ? null : f.chapter || this.focusChapter;
        const version = cid && cid === this.editChapterId ? this.version : cid ? (this.summaryOf(cid) || {}).version : null;
        f.busy = true;
        const r = await U.api(urls.findReplace, { method: 'POST', body: { chapter: cid, query: f.query, replacement: f.replacement, replace: true, version: version || undefined, match_tashkeel: f.matchTashkeel, fold_alef: f.foldAlef, whole_word: f.wholeWord } });
        f.busy = false;
        if (!r.ok || !r.data) { U.toast(r.message); return 0; }
        const n = Number(r.data.replaced) || 0;
        if (!n) { U.toast('لا مطابقات'); return 0; }
        chapterCache.clear();
        if (this.editChapterId) await this.loadChapter(this.editChapterId, { force: true, quiet: true });
        if (r.data.relayout) this.followRelayout(r.data.relayout, this.editChapterId === cid ? ctx.nodes : null);
        else if (this.focusChapter) this.requestRelayout(this.focusChapter);
        this.pollNow();
        const where = cid ? '' : ' في الكتاب';
        this.liveMessage = `استُبدلت ${G.arCount(n, MATCHES)}${where}`;
        const snapshot = r.data.snapshot;
        this.undoToast(`استُبدلت ${G.arCount(n, MATCHES)}${where}`, () => this.restoreSnapshot(snapshot, { quiet: true }));
        await this.runFind();
        return n;
      },

      // ------------------------------------------------------------ غير المؤكَّدة
      async loadUncertain() {
        if (!urls.uncertain) return false;
        this.uncertain.loading = true;
        const r = await U.api(urls.uncertain);
        this.uncertain.loading = false;
        if (!r.ok || !r.data) { this.uncertain.error = r.message; return false; }
        this.uncertain.items = Array.isArray(r.data.items) ? r.data.items : [];
        this.uncertain.count = Number(r.data.count) || this.uncertain.items.length;
        this.uncertain.loaded = true;
        this.uncertain.error = '';
        if (this.mode === 'edit') this.paint();
        return true;
      },
      get uncertainGroups() {
        const titles = Object.fromEntries(this.chapters.map((c) => [c.id, c.title]));
        return G.groupUncertain(this.uncertain.items, titles);
      },
      uncertainAt(block, note, start, end) {
        return this.uncertain.items.find((w) => w.block === block && (w.note || null) === (note || null) && w.start === start && w.end === end) || null;
      },
      dropUncertain(item) {
        const i = this.uncertain.items.indexOf(item);
        if (i >= 0) this.uncertain.items.splice(i, 1);
        this.uncertain.count = Math.max(0, this.uncertain.count - 1);
      },
      // A word picked: its page shown, the word lit on it.
      // In edit mode the word's paragraph opens with the word selected (a body word; a note's word is lit).
      async pickUncertain(item) {
        if (!item) return false;
        this.uncertain.open = item;
        // the page it is on now (the list's page is the one of the layout it was loaded with)
        const at = G.caretLine(ctx.pages, item.note || item.block, item.start);
        const page = (at ? at.n : null) || item.page || null;
        if (page && !this.sideOf(page)) this.showPage(page, { manual: true });
        else this.paint();
        if (!page) {
          const r = this.ranges.find((c) => c.id === item.chapter);
          if (r && !this.sideOf(r.first)) this.showPage(r.first, { manual: true });
        }
        if (this.mode === 'edit' && this.canEdit && !item.note && page) {
          const ok = await this.openBlock(item.block, item.start, { n: page });
          if (!ok || !ctx.ed || this.uncertain.open !== item) return ok;
          const text = typeof ctx.ed.text === 'function' ? ctx.ed.text() : null;
          if (text === null || text.slice(item.start, item.end) === item.word) ctx.ed.setOffset(item.start, item.end);
        }
        return true;
      },
      isPicked(item) { const o = this.uncertain.open; return Boolean(o && item && o.block === item.block && o.start === item.start && (o.note || null) === (item.note || null)); },
      // accept / choose a reading / type: the manuscript edited on the server (version-checked), laid out again
      async resolveUncertain(item, action, extra = {}) {
        if (!this.canEdit || !item || this.uncertain.busy) return false;
        const url = { accept: urls.uncertainAccept, choose: urls.uncertainChoose, type: urls.uncertainType }[action];
        if (!url) return false;
        if (action === 'type' && !String(extra.text || '').trim()) return false;
        // the edits of that chapter go first (the word's offsets are the saved text's)
        if (item.chapter === this.editChapterId) {
          await this.closeBlock({ commit: true });
          await this.saveNow();
          if (this.editDirty) { U.toast('تعذّر الحفظ قبل التصحيح'); return false; }
        }
        let version = item.chapter === this.editChapterId ? this.version : (this.summaryOf(item.chapter) || {}).version;
        if (!version) { const r0 = await U.api(U.fill(urls.chapter, item.chapter)); version = r0.ok && r0.data ? r0.data.version : ''; }
        this.uncertain.busy = `${item.block}:${item.start}`;
        const body = Object.assign({ chapter: item.chapter, block: item.block, note: item.note, start: item.start, end: item.end, word: item.word, version }, extra);
        let r = await U.api(url, { method: 'POST', body });
        if (r.status === 409 && r.data && r.data.version && r.data.version !== version) {
          body.version = r.data.version; // the chapter moved on (a save elsewhere): once more with its version
          r = await U.api(url, { method: 'POST', body });
        }
        this.uncertain.busy = '';
        if (!r.ok || !r.data) {
          U.toast(r.status === 409 ? 'تغيّر النص حول هذه الكلمة؛ حُدّثت القائمة.' : r.message);
          this.loadUncertain();
          return false;
        }
        const data = r.data;
        const sum = this.summaryOf(item.chapter);
        if (sum && data.version) sum.version = data.version;
        this.dropUncertain(item);
        this.uncertain.count = Number(data.remaining) >= 0 ? Number(data.remaining) : this.uncertain.count;
        if (this.uncertain.open === item) this.uncertain.open = Object.assign({}, item, { start: data.start, end: data.end, word: data.word });
        if (item.chapter === this.editChapterId) await this.loadChapter(item.chapter, { force: true, quiet: true });
        chapterCache.delete(item.chapter);
        this.followRelayout(data.relayout, item.chapter === this.editChapterId ? ctx.nodes : null);
        this.liveMessage = action === 'accept' ? 'قُبلت الكلمة' : `صُحّحت إلى ${data.word}`;
        clearTimeout(T.uncertain);
        T.uncertain = setTimeout(() => this.loadUncertain(), UNCERTAIN_REFRESH_MS);
        return true;
      },

      // ------------------------------------------------------------ الأصل: the source of the block
      get sourcePage() { return this.source.pages[this.source.index] || 0; },
      get sourceAspect() {
        const s = this.source.sheet;
        return s && s.width > 0 && s.height > 0 ? (s.width / s.height).toFixed(4) : '0.7';
      },
      reviewUrl(n) { return n ? U.fill(urls.review, n) : ''; },
      // The block the pane shows: the open one, the one clicked in preview, else the first on the page shown.
      async followSource(now) {
        clearTimeout(T.source);
        const gen = (sourceGen += 1);
        const run = async () => {
          let id = ctx.openId || (this.selected && this.selected.block) || this.pointed;
          if (!id) { const page = ctx.pages.get(this.current); id = page ? (G.blocksOn(page).find((b) => b && !/^(front-|toc-|copyright)/.test(b)) || null) : null; }
          if (!id || !B()) { this.source = { blockId: null, pages: [], index: 0, lines: [], sheet: null, loading: false, error: '' }; return; }
          let at = B().locate(ctx.nodes, id);
          if (!at) { const note = B().findNote(ctx.nodes, id); if (note) at = B().locate(ctx.nodes, note.block); }
          if (!at) {
            const found = await this.blockFromChapters(id, this.current);
            if (gen !== sourceGen) return; // a newer block asked for meanwhile
            at = found ? found.at : null;
          }
          if (!at) return;
          const attrs = at.node.attrs || {};
          const pages = [...new Set((attrs.sourcePages || []).filter((n) => Number.isInteger(n)))].sort((a, b) => a - b);
          const lines = (attrs.sourceLineIds || []).slice();
          if (!pages.length) { this.source = { blockId: id, pages: [], index: 0, lines: [], sheet: null, loading: false, error: '' }; return; }
          const keep = this.source.blockId === id ? Math.min(this.source.index, pages.length - 1) : 0;
          const same = this.source.sheet && this.source.sheet.number === pages[keep] ? this.source.sheet : null;
          this.source = { blockId: id, pages, index: keep, lines, sheet: same, loading: !same, error: '' };
          if (!same) this.loadSource(pages[keep]);
        };
        if (now) return run();
        T.source = setTimeout(run, SOURCE_MS);
        return null;
      },
      async fetchSheet(n, force) {
        if (!n) return null;
        if (!force && sheetCache.has(n)) return sheetCache.get(n);
        const sep = String(urls.sheets || '').includes('?') ? '&' : '?';
        const r = await U.api(`${urls.sheets}${sep}from=${n}&to=${n}`);
        const item = r.ok && r.data ? (Array.isArray(r.data) ? r.data : r.data.pages || []).find((p) => p && p.number === n) : null;
        if (item) sheetCache.set(n, item);
        else this.source.error = r.ok ? 'لا بيانات لهذه الصفحة.' : `تعذّر تحميل صورة الصفحة. ${r.message}`;
        return item || null;
      },
      async loadSource(n, force) {
        const item = await this.fetchSheet(n, force);
        if (this.sourcePage !== n) return false;
        this.source.loading = false;
        this.source.sheet = item;
        if (item) this.source.error = '';
        return Boolean(item);
      },
      sourceStep(dir) {
        const i = this.source.index + dir;
        if (i < 0 || i >= this.source.pages.length) return false;
        this.source.index = i;
        this.source.loading = true;
        this.source.sheet = null;
        return this.loadSource(this.source.pages[i]);
      },
      sourceBoxes() {
        const s = this.source.sheet;
        if (!s || !Array.isArray(s.lines)) return [];
        const own = new Set(this.source.lines);
        return s.lines.filter((l) => Array.isArray(l.bbox) && l.bbox.length === 4 && own.has(l.id)).map((l) => ({ id: l.id, bbox: l.bbox }));
      },
      boxStyle(b) {
        const pct = (v) => `${(100 * Math.min(1, Math.max(0, Number(v) || 0))).toFixed(2)}%`;
        return `left:${pct(b[0])};top:${pct(b[1])};width:${pct(b[2] - b[0])};height:${pct(b[3] - b[1])}`;
      },
      async openDrawer() {
        await this.followSource(true);
        if (!this.source.pages.length) { U.toast('ضع المؤشّر في فقرة لها أصل'); return false; }
        this.closePop();
        this.drawerOpen = true;
        this.liveMessage = `الأصل: صفحة ${this.sourcePage}`;
        if (this.$nextTick) this.$nextTick(() => U.focus(this.$refs && this.$refs.drawerClose));
        return true;
      },
      closeDrawer() {
        if (!this.drawerOpen) return;
        this.drawerOpen = false;
        if (ctx.ed) ctx.ed.focus();
      },
      toggleDrawer() { if (this.drawerOpen) this.closeDrawer(); else this.openDrawer(); },
      // «الفقرة»: the block's source pages, as review links
      get blockInfo() {
        const b = this.currentBlock;
        if (!b) return null;
        const a = b.node.attrs || {};
        const pages = [...new Set((a.sourcePages || []).filter((n) => Number.isInteger(n)))].sort((x, y) => x - y);
        const style = this.open ? this.blockStyle : B() ? B().styleOf(b.node) : 'paragraph';
        const found = this.styles.find((s) => s.key === style);
        return {
          id: b.id,
          style,
          styleLabel: found ? found.label : 'فقرة',
          breakBefore: this.open ? this.flags.breakBefore : a.breakBefore === true,
          keepWithNext: this.open ? this.flags.keepWithNext : a.keepWithNext === true,
          pages,
          reviewed: a.reviewed !== false,
          separator: b.node.type === 'separator',
        };
      },

      // ------------------------------------------------------------ dialogs: snapshots, digits, shortcuts
      // a menu item hides with its menu: focus goes back to the button that opened the menu
      rememberTrigger() {
        const el = U.hasDOM ? document.activeElement : null;
        const menu = el ? U.closest(el, '[role="menu"]') : null;
        const opener = menu && menu.parentElement ? menu.parentElement.querySelector('[aria-haspopup="menu"]') : null;
        lastTrigger = opener || el;
      },
      restoreTrigger() { const el = lastTrigger; lastTrigger = null; if (el && el.isConnected !== false) U.focus(el); },
      async openSnapshots() {
        this.closePop();
        this.rememberTrigger();
        this.snapshots = Object.assign({}, this.snapshots, { open: true, loading: true, error: '', label: '' });
        if (this.$nextTick) this.$nextTick(() => U.focus((this.$refs && this.$refs.snapshotLabel) || (this.$refs && this.$refs.snapshotsClose)));
        const r = await U.api(urls.snapshots);
        this.snapshots.loading = false;
        if (!r.ok || !Array.isArray(r.data)) { this.snapshots.error = r.message; return false; }
        this.snapshots.list = r.data;
        return true;
      },
      closeSnapshots() { if (!this.snapshots.open) return; this.snapshots.open = false; this.restoreTrigger(); },
      async createSnapshot() {
        if (!this.canEdit || this.snapshots.busy) return false;
        await this.saveNow();
        if (this.editDirty) { U.toast('تعذّر الحفظ قبل أخذ النسخة'); return false; }
        this.snapshots.busy = true;
        const r = await U.api(urls.snapshots, { method: 'POST', body: { label: this.snapshots.label } });
        this.snapshots.busy = false;
        if (!r.ok || !r.data) { this.snapshots.error = r.message; return false; }
        this.snapshots.list = [r.data, ...this.snapshots.list.map((s) => Object.assign({}, s, { current: false }))];
        this.snapshots.label = '';
        this.snapshots.error = '';
        U.toast('حُفظت النسخة');
        return true;
      },
      async restoreSnapshot(id, opts = {}) {
        if (!this.canEdit || !id) return false;
        await this.closeBlock({ commit: true });
        await this.saveNow();
        if (this.editDirty) { U.toast('تعذّر الحفظ قبل الاستعادة'); return false; }
        this.snapshots.busy = true;
        const r = await U.api(U.fill(urls.restore, id), { method: 'POST', body: {} });
        this.snapshots.busy = false;
        if (!r.ok || !r.data) { U.toast(r.message); return false; }
        this.snapshots.open = false;
        chapterCache.clear();
        await this.afterServerEdit();
        if (!opts.quiet) this.undoToast('استُعيدت النسخة', () => this.restoreSnapshot(r.data.snapshot, { quiet: true }));
        else U.toast('أُعيد النص كما كان');
        return true;
      },
      // A server-side edit of the manuscript (a restore, a book-wide conversion): the chapter again, the pages
      // under the reader's eyes laid out again now, the whole book in the background.
      async afterServerEdit() {
        if (this.editChapterId) await this.loadChapter(this.editChapterId, { force: true, quiet: true });
        const list = await U.api(urls.chapters);
        if (list.ok && Array.isArray(list.data)) {
          this.chapters = list.data.map((c) => ({ id: c.id, number: c.number, kind: c.kind, title: c.title }));
          this.summaries = list.data;
        }
        if (this.focusChapter) this.requestRelayout(this.focusChapter);
        if (this.uncertain.loaded) this.loadUncertain();
        this.pollNow();
      },
      openDigits() {
        this.closePop();
        this.rememberTrigger();
        this.digits.open = true;
        if (this.$nextTick) this.$nextTick(() => U.focus(this.$refs && this.$refs.digitsFirst));
      },
      closeDigits() { if (!this.digits.open) return; this.digits.open = false; this.restoreTrigger(); },
      async convertDigits() {
        if (!this.canEdit || this.digits.busy) return false;
        await this.closeBlock({ commit: true });
        await this.saveNow();
        if (this.editDirty) { U.toast('تعذّر الحفظ قبل التحويل'); return false; }
        this.digits.busy = true;
        const cid = this.digits.scope === 'book' ? null : this.focusChapter;
        const r = await U.api(urls.convertDigits, { method: 'POST', body: { chapter: cid, style: this.digits.style } });
        this.digits.busy = false;
        this.closeDigits();
        if (!r.ok || !r.data) { U.toast(r.message); return false; }
        const n = Number(r.data.changed) || 0;
        if (!n) { U.toast('لا أرقام تُحوَّل'); return true; }
        chapterCache.clear();
        if (this.editChapterId) await this.loadChapter(this.editChapterId, { force: true, quiet: true });
        if (r.data.relayout) this.followRelayout(r.data.relayout, cid === this.editChapterId ? ctx.nodes : null);
        else await this.afterServerEdit();
        this.undoToast(`حُوّل ${G.arCount(n, DIGITS)}`, () => this.restoreSnapshot(r.data.snapshot, { quiet: true }));
        return true;
      },
      openSheet() { this.closePop(); this.rememberTrigger(); this.sheetOpen = true; if (this.$nextTick) this.$nextTick(() => U.focus(this.$refs && this.$refs.sheetClose)); },
      closeSheet() { if (!this.sheetOpen) return; this.sheetOpen = false; this.restoreTrigger(); },
      relativeTime(iso) { return relativeTime(iso, Date.now()); },
      moveIn(rootEl, dir) {
        const items = U.qa(rootEl, '.menu-item, .rv-opt, .rv-act').filter((el) => !el.disabled && !(typeof el.hasAttribute === 'function' && el.hasAttribute('disabled')));
        if (!items.length) return false;
        const active = U.hasDOM ? document.activeElement : null;
        const i = items.indexOf(active);
        const next = i === -1 ? (dir > 0 ? 0 : items.length - 1) : (i + dir + items.length) % items.length;
        U.focus(items[next]);
        return true;
      },
      trapTab(e, rootEl) {
        const items = U.qa(rootEl, 'a[href], button:not([disabled]), input, [tabindex="0"]').filter((el) => Boolean(el) && !el.hidden && (typeof el.getClientRects !== 'function' || el.getClientRects().length > 0));
        if (!items.length) return;
        const first = items[0];
        const last = items[items.length - 1];
        const active = U.hasDOM ? document.activeElement : null;
        if (e.shiftKey && active === first) { e.preventDefault(); U.focus(last); } else if (!e.shiftKey && active === last) { e.preventDefault(); U.focus(first); }
      },

      // ------------------------------------------------------------ the review drift (D41) and its re-assembly
      get driftChapter() {
        const cid = this.focusChapter;
        if (!cid || this.dismissedDrift.includes(cid)) return null;
        const s = this.summaryOf(cid);
        const listed = ((cfg.drift && cfg.drift.chapters) || []).includes(cid);
        if (!(s && s.drift) && !listed) return null;
        const range = s && s.source_pages;
        const pages = ((cfg.drift && cfg.drift.pages) || []).filter((n) => !range || (n >= range.first && n <= (range.last || range.first)));
        return { id: cid, title: this.chapterTitle(cid), pages };
      },
      get driftText() {
        const d = this.driftChapter;
        if (!d) return '';
        return d.pages.length ? `تغيّر نص ${G.arCount(d.pages.length, PAGES)} من هذا الفصل في المراجعة بعد التحرير:` : 'تغيّر نص هذا الفصل في المراجعة بعد التحرير.';
      },
      dismissDrift() {
        const d = this.driftChapter;
        if (d) this.dismissedDrift = [...this.dismissedDrift, d.id];
        this.renderChapters();
      },
      async reassembleChapter() {
        const cid = this.focusChapter;
        if (!this.canEdit || !cid || this.reassembly.running) return false;
        await this.closeBlock({ commit: true });
        await this.saveNow();
        if (this.editDirty) { U.toast('تعذّر الحفظ قبل إعادة التجميع'); return false; }
        const r = await U.api(U.fill(urls.reassemble, cid), { method: 'POST', body: {} });
        if (!r.ok || !r.data) { U.toast(r.message); return false; }
        this.reassembly = { running: true, runId: r.data.run_id, error: '', chapter: cid };
        this.liveMessage = 'تُعاد قراءة الفصل من صفحات المراجعة';
        pollFailures = 0;
        clearTimeout(T.reassembly);
        T.reassembly = setTimeout(() => this.pollReassembly(), POLL_MS);
        return true;
      },
      async pollReassembly() {
        if (!this.reassembly.running) return;
        const r = await U.api(urls.manuscriptState);
        if (!r.ok || !r.data) {
          pollFailures += 1;
          clearTimeout(T.reassembly);
          T.reassembly = setTimeout(() => this.pollReassembly(), Math.min(POLL_MS * (1 + pollFailures), POLL_MAX_MS));
          return;
        }
        const run = r.data.run;
        const mine = run && run.id === this.reassembly.runId;
        if (r.data.active || (mine && (run.status === 'queued' || run.status === 'running'))) {
          clearTimeout(T.reassembly);
          T.reassembly = setTimeout(() => this.pollReassembly(), POLL_MS);
          return;
        }
        const cid = this.reassembly.chapter;
        this.reassembly = { running: false, runId: null, error: mine && run.status === 'error' ? run.error || 'تعذّرت إعادة تجميع الفصل.' : '' };
        if (this.reassembly.error) { U.toast(this.reassembly.error); return; }
        const s = this.summaryOf(cid);
        if (s) s.drift = false;
        if (cfg.drift && Array.isArray(cfg.drift.chapters)) cfg.drift.chapters = cfg.drift.chapters.filter((c) => c !== cid);
        chapterCache.delete(cid);
        await this.afterServerEdit();
        U.toast('أُعيد تجميع الفصل من المراجعة؛ النص السابق محفوظ نسخةً');
      },

      // ------------------------------------------------------------ layers and keys
      // A menu open outside the component (the top bar's «⋯», the app's icon rail): its own Esc closes it, so
      // the page's Esc must not also close the paragraph or leave edit mode under it.
      outsideLayer() {
        if (!U.hasDOM) return null;
        const menu = document.querySelector('[data-book-menu]');
        if (menu && menu.style && menu.style.display !== 'none' && typeof menu.getClientRects === 'function' && menu.getClientRects().length) return 'menu';
        const shell = document.querySelector('.app-shell.is-rail-open');
        return shell ? 'rail' : null;
      },
      topLayer() {
        if (this.sheetOpen) return 'sheet';
        if (this.snapshots.open) return 'snapshots';
        if (this.digits.open) return 'digits';
        if (this.styleMenu) return 'styleMenu';
        if (this.pop.kind) return this.pop.kind;
        if (this.drawerOpen) return 'drawer';
        return null;
      },
      // Esc: the top-most layer, then the open paragraph, then edit mode (DESIGN.md §10).
      escape() {
        const outside = this.outsideLayer();
        if (outside) return outside;
        const layer = this.topLayer();
        if (layer === 'sheet') this.closeSheet();
        else if (layer === 'snapshots') this.closeSnapshots();
        else if (layer === 'digits') this.closeDigits();
        else if (layer === 'styleMenu') this.styleMenu = false;
        else if (layer === 'note' || layer === 'word') this.closePop(true);
        else if (layer === 'drawer') this.closeDrawer();
        else if (ctx.ed) { this.closeBlock({ commit: true }); this.focusStage(); return 'block'; }
        else if (this.selected) { this.selected = null; this.paint(); return 'selected'; }
        else if (this.mode === 'edit') { this.setMode('preview'); return 'mode'; }
        return layer;
      },
      onKey(e) {
        const t = e.target;
        const inBlock = Boolean(U.closest(t, '.lp-patch.is-open'));
        const inField = !inBlock && Boolean(t && (['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName) || t.isContentEditable || U.closest(t, '.ed-note-editor')));
        const action = G.keyAction(e, { mode: this.mode, inField, inBlock });
        if (!action) return;
        if (action === 'blur') { if (t && t.blur) t.blur(); return; }
        if (action === 'escape') { if (e.defaultPrevented) return; const done = this.escape(); if (done && done !== 'menu' && done !== 'rail') e.preventDefault(); return; }
        if (e.defaultPrevented) return; // the open paragraph's editor took it
        // a dialog is open (the snapshots, the digits, the shortcut sheet): the page's keys wait behind it
        if (['sheet', 'snapshots', 'digits'].includes(this.topLayer())) return;
        switch (action) {
          case 'save': e.preventDefault(); if (this.canEdit) { this.saveNow(); this.flushSheet(); } return;
          case 'find': e.preventDefault(); this.setTab('find', { quiet: true }); return;
          case 'mode': e.preventDefault(); this.toggleMode(); return;
          case 'sheet': e.preventDefault(); this.openSheet(); return;
          case 'source': e.preventDefault(); this.toggleDrawer(); return;
          case 'jump': e.preventDefault(); this.focusJump(); return;
          case 'spread': this.toggleSpread(); return;
          case 'fitHeight': this.setFit('height'); return;
          case 'fitWidth': this.setFit('width'); return;
          case 'fitActual': this.setFit('actual'); return;
          case 'next': case 'prev': e.preventDefault(); this.turn(action === 'next' ? 1 : -1); return;
          case 'first': case 'last': if (this.pages.length) { e.preventDefault(); this.showIndex(action === 'first' ? 0 : this.pages.length - 1, { manual: true }); } return;
          case 'undo': e.preventDefault(); this.undo(); return;
          case 'redo': e.preventDefault(); this.redo(); return;
          case 'bold': e.preventDefault(); this.toggleBold(); return;
          case 'italic': e.preventDefault(); this.toggleItalic(); return;
          case 'footnote': e.preventDefault(); this.insertFootnote(); return;
          case 'prevChapter': case 'nextChapter': e.preventDefault(); this.stepChapter(action === 'prevChapter' ? -1 : 1); return;
          case 'deleteSelected': if (this.selected) { e.preventDefault(); this.deleteSelected(); } return;
          default:
            if (Object.values(G.STYLE_KEYS).includes(action)) { e.preventDefault(); this.setStyle(action); }
        }
      },
      // ⌘[ / ⌘]: the chapter before or after the one under the eyes, from its first page
      stepChapter(dir) {
        const i = this.chapters.findIndex((c) => c.id === this.focusChapter);
        const next = this.chapters[i + dir];
        if (!next) { U.toast(dir < 0 ? 'هذا أول فصل' : 'هذا آخر فصل'); return false; }
        this.closeBlock({ commit: true });
        return this.goToChapter(next.id);
      },
    };
  };
})();
