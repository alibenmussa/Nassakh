// The book page's edit mode (PHASE5_SPEC §9.2, D47): edit on the page itself.
//   - «معاينة | تحرير», E, a double-click in preview: edit mode keeps the page exactly where it is
//   - a click on a line maps the point to (block, offset) (caretPositionFromPoint on the line's text + the
//     run's range) and opens that paragraph in place: a one-block TipTap editor (static/dist/editor.js
//     createBlock) in the page's face, size, measure, line height and indent, anchored so the clicked line
//     stays under the pointer; the paragraph's own lines are hidden (its continuation on the next page too),
//     the lines below it move with its growth
//   - Enter / Backspace at the start / Delete at the end / the arrows past the first or last line cross into
//     the neighbouring blocks (a chapter's edge: the next chapter); Esc closes the paragraph, then edit mode
//   - after a 500 ms pause the paragraph is written into the chapter's nodes (never mutated: each version is
//     kept for the chapter's undo), the chapter is saved with its version (409: the conflict banner), the
//     save asks for the re-layout and its pages are spliced in place — the open paragraph stays open at the
//     caret, re-anchored on the line the engine put it on
//   - a block edited but not laid out again yet is drawn by the browser in the page's faces in its place
//     (a patch), so the page never shows the old text beside the new
//   - style picker, B / I, footnotes (one overlay: the note editor or a word's readings), «الفقرة» flags
(function () {
  'use strict';

  const root = typeof window !== 'undefined' ? window : globalThis;
  const NS = (root.NassakhBook = root.NassakhBook || {});
  NS.parts = NS.parts || {};

  const RELAYOUT_MS = 500;
  const WAIT_S = 2; // the re-layout's long poll
  const FOLLOW_TRIES = 40;
  const HISTORY = 200;
  const MARKS_KEY = 'nassakh.book.marks';
  const POP_EDGE = 8;
  const POP_GAP = 6;
  const POP_SIZE = { note: [380, 132], word: [280, 168] };
  const SAVE_TEXT = { dirty: 'غير محفوظ', saving: 'يُحفظ…', saved: 'محفوظ', error: 'تعذّر الحفظ · إعادة المحاولة', conflict: 'تغيّر في نافذة أخرى' };

  const jsonOf = (node) => JSON.stringify(node);

  // Placement of the overlay (= manuscript placeAgainst, in viewport coordinates): below the anchor, flipped
  // above when it would leave the visible area and there is more room above, its start edge on the anchor's
  // start edge (RTL: the right), never across a side edge.
  function placeAgainst(a, view, size, rtl) {
    const w = Math.min(size[0], Math.max(120, view.right - view.left));
    const h = size[1];
    let top = a.bottom + POP_GAP;
    let above = false;
    if (top + h > view.bottom) {
      const roomAbove = a.top - POP_GAP - view.top;
      const roomBelow = view.bottom - top;
      if (roomAbove >= h || roomAbove > roomBelow) { top = Math.max(view.top, a.top - POP_GAP - h); above = true; } else top = Math.max(view.top, view.bottom - h);
    }
    let left;
    if (rtl) {
      let right = Math.min(a.right, view.right);
      if (right - w < view.left) right = Math.min(view.right, view.left + w);
      left = right - w;
    } else {
      left = Math.max(a.left, view.left);
      if (left + w > view.right) left = Math.max(view.left, view.right - w);
    }
    return { style: `top:${Math.round(top)}px;left:${Math.round(left)}px`, above };
  }

  // The flow of a page with its patches (pure): `regions` = [{key, oldTop, oldBottom, top (open: the anchor),
  // height, open}] in points. Returns each region's screen top and, for every line, how far it moves (`dy`)
  // and whether it now falls past the body (`over`). Lines of patched blocks are hidden by the caller.
  function flow(page, regions, bottom) {
    const list = regions.slice().sort((a, b) => a.oldTop - b.oldTop);
    let shift = 0;
    list.forEach((r) => {
      r.screenTop = r.open && Number.isFinite(r.anchorTop) ? r.anchorTop + shift : r.oldTop + shift;
      r.after = r.screenTop + r.height - r.oldBottom;
      shift = r.after;
    });
    const lines = ((page && page.lines) || []).map((l) => {
      if (l.kind === 'note' || l.kind === 'source') return { dy: 0, over: false };
      let dy = 0;
      list.forEach((r) => { if (r.oldBottom <= l.y + 0.5) dy = r.after; });
      return { dy, over: dy > 0 && l.y + l.h + dy > bottom + 0.5 };
    });
    return { regions: list, lines };
  }
  NS.editFlow = { flow, placeAgainst };

  NS.parts.edit = function edit(ctx) {
    const U = NS.util;
    const G = NS.geo;
    const cfg = ctx.cfg;
    const urls = ctx.urls;
    const T = ctx.timers;
    const B = () => U.bundle();
    const relayoutMs = Number(cfg.relayoutMs) || RELAYOUT_MS;
    ctx.nodes = [];
    ctx.saved = null; // the nodes as last saved
    ctx.laid = []; // the nodes as the live layout shows them
    ctx.hist = new Map();
    ctx.patches = new Map(); // block id → {el, json, side}
    ctx.ed = null;
    ctx.openGen = 0;
    let saving = null; // the PUT in flight (a promise)
    let again = false;
    let followGen = 0;
    let noteEd = null;
    let justOpened = false;
    let plainCache = null;
    let reflowQueued = false;
    const opened = () => { justOpened = true; setTimeout(() => { justOpened = false; }, 0); };

    return {
      mode: cfg.mode === 'edit' && cfg.canEdit ? 'edit' : 'preview',
      editChapterId: null,
      chapter: null,
      version: '',
      open: null, // {block, n, style}
      selected: null, // {block, kind: 'separator'} a block chosen without text (a separator)
      blockStyle: 'paragraph',
      bold: false,
      italic: false,
      canUndo: false,
      canRedo: false,
      editSave: { state: '', message: '' },
      textDirty: false,
      conflict: { open: false, version: '', content: null },
      pageMarks: U.readLocal(MARKS_KEY, '1') !== '0',
      styleMenu: false,
      pop: { kind: null, style: '', above: false },
      note: { id: '', number: '', sourcePage: null, orphan: false },
      word: { from: 0, to: 0, text: '', typed: '', readings: [], item: null },
      styles: [],
      flags: { breakBefore: false, keepWithNext: false },

      _init_edit() {
        this.styles = (B() && B().STYLES) || [];
      },
      _destroy_edit() {
        this.dropEditor();
        if (noteEd) { noteEd.destroy(); noteEd = null; }
      },
      // After the first paint: a page opened in edit mode loads the chapter under the eyes.
      start() {
        if (this.mode === 'edit') this.enterEdit();
        this.pollNow();
      },

      // ------------------------------------------------------------ modes
      get editing() { return this.mode === 'edit'; },
      addressQuery() { return this.mode === 'edit' ? '?mode=edit' : ''; },
      setMode(mode) {
        const next = mode === 'edit' ? 'edit' : 'preview';
        if (next === this.mode) return false;
        if (next === 'edit' && !this.canEdit) { U.toast('التحرير لمحرّر الكتاب؛ يمكنك تصفّح الصفحات.'); return false; }
        if (next === 'edit') return this.enterEdit(true);
        this.leaveEdit();
        return true;
      },
      toggleMode() { return this.setMode(this.mode === 'edit' ? 'preview' : 'edit'); },
      enterEdit(fresh) {
        if (!this.canEdit) { this.mode = 'preview'; return false; }
        if (!B() || typeof B().createBlock !== 'function') { this.mode = 'preview'; U.toast('لم يُحمَّل المحرّر: ملف static/dist/editor.js غير موجود أو تعطّل.'); return false; }
        this.mode = 'edit';
        // the chapter under the eyes first: the panel's tab (the source of its first block) waits for it
        if (this.focusChapter) this.loadChapter(this.focusChapter).then(() => this.paint());
        if (fresh && typeof this.afterMode === 'function') this.afterMode('edit');
        this.remember();
        this.paint();
        return true;
      },
      leaveEdit() {
        this.closeBlock({ commit: true });
        this.selected = null;
        this.mode = 'preview';
        if (typeof this.afterMode === 'function') this.afterMode('preview');
        this.remember();
        this.paint();
      },
      remember() {
        if (typeof history === 'undefined' || !history.replaceState || typeof window === 'undefined' || !window.location) return;
        try { history.replaceState(null, '', `${window.location.pathname}${this.addressQuery()}${this.current ? `#page-${this.current}` : ''}`); } catch (_) { /* sandboxed */ }
      },

      // ------------------------------------------------------------ the chapter's nodes
      // The chapter's nodes for editing (the one being edited is saved first); one request per chapter at a time.
      async loadChapter(cid, opts = {}) {
        if (!cid || !urls.chapter) return false;
        if (this.editChapterId === cid && !opts.force) return true;
        if (ctx.loading && ctx.loading.cid === cid && !opts.force) return ctx.loading.promise;
        const promise = (async () => {
          if (this.editChapterId && this.editChapterId !== cid) {
            await this.closeBlock({ commit: true });
            await this.saveNow();
            if (this.editDirty) { U.toast('تعذّر الحفظ؛ بقيت في هذا الفصل'); return false; }
          }
          const r = await U.api(U.fill(urls.chapter, cid));
          if (!r.ok || !r.data) { if (!opts.quiet) U.toast(r.message); return false; }
          this.applyChapter(r.data, opts);
          return true;
        })();
        ctx.loading = { cid, promise };
        try { return await promise; } finally { if (ctx.loading && ctx.loading.promise === promise) ctx.loading = null; }
      },
      applyChapter(data, opts = {}) {
        this.chapter = {
          id: data.id, number: data.number, kind: data.kind, title: data.title, prev: data.prev, next: data.next, count: data.count,
          sourcePages: data.source_pages, pages: data.pages, drift: Boolean(data.drift), warnings: Array.isArray(data.warnings) ? data.warnings : [],
        };
        this.editChapterId = data.id;
        this.version = data.version || '';
        const nodes = data.content && Array.isArray(data.content.content) ? data.content.content : Array.isArray(data.content) ? data.content : [];
        ctx.nodes = nodes;
        ctx.saved = nodes;
        ctx.laid = nodes;
        plainCache = null;
        if (opts.force || !ctx.hist.has(data.id)) ctx.hist.set(data.id, { past: [], future: [] });
        this.textDirty = false;
        this.editSave = { state: '', message: '' };
        this.conflict = { open: false, version: '', content: null };
        this.dropPatches();
        this.historyFlags();
      },
      nodes() { return ctx.nodes; },
      // The plain text of a block (or a note) as the pages show it: from the nodes the layout was made of.
      plainOf(id) {
        if (!id || !ctx.laid || !ctx.laid.length || !B()) return undefined;
        if (!plainCache) {
          plainCache = new Map();
          B().flatBlocks(ctx.laid).forEach(({ id: bid, node }) => {
            if (bid) plainCache.set(bid, B().plainText(node));
            (node.content || []).forEach((item) => { if (item && item.type === 'footnote' && item.attrs && item.attrs.id) plainCache.set(item.attrs.id, B().inlineText(item.content)); });
          });
        }
        return plainCache.get(id);
      },
      marksOf(id) {
        if (!B() || !ctx.laid.length) return [];
        const at = B().locate(ctx.laid, id);
        return at ? B().pageMarks(at.node) : [];
      },
      // The chapter holding `block` (a note id too), loaded: the page's chapter first, then its neighbours
      // (a page where one section ends and the next begins).
      async chapterFor(block, n) {
        const has = () => Boolean(B() && (B().locate(ctx.nodes, block) || B().findNote(ctx.nodes, block)));
        if (this.editChapterId && has()) return this.editChapterId;
        const page = n ? ctx.pages.get(n) : null;
        const first = (page && page.chapter) || this.focusChapter;
        const i = this.chapters.findIndex((c) => c.id === first);
        const candidates = [first, i >= 0 && this.chapters[i + 1] ? this.chapters[i + 1].id : null, i > 0 ? this.chapters[i - 1].id : null].filter(Boolean);
        for (const cid of candidates) {
          if (!(await this.loadChapter(cid, { quiet: true }))) return null;
          if (has()) return cid;
        }
        return null;
      },
      // What changed since the layout on screen: `{changed, removed, added: [{id, after}]}` (block ids).
      editDiff() {
        const out = { changed: new Set(), removed: new Set(), added: [] };
        if (!B() || ctx.nodes === ctx.laid) return out;
        const laid = new Map(B().flatBlocks(ctx.laid).map((b) => [b.id, jsonOf(b.node)]));
        const now = B().flatBlocks(ctx.nodes);
        const ids = new Set(now.map((b) => b.id));
        let prev = null;
        now.forEach((b) => {
          if (!laid.has(b.id)) out.added.push({ id: b.id, after: prev });
          else if (laid.get(b.id) !== jsonOf(b.node)) out.changed.add(b.id);
          prev = b.id;
        });
        laid.forEach((_json, id) => { if (!ids.has(id)) out.removed.add(id); });
        return out;
      },

      // ------------------------------------------------------------ a click on the page
      onSheetClick(e, side) {
        if (U.closest(e.target, '.lp-patch.is-open, .ed-pop')) return false;
        const n = this.shownNumbers[side];
        const page = n ? ctx.pages.get(n) : null;
        if (!page) return false;
        const lineEl = U.closest(e.target, '.lp-line');
        const line = lineEl ? page.lines[Number(lineEl.dataset.i)] : null;
        if (this.mode !== 'edit') {
          if (line && line.target) return this.goToBlock(line.target, line);
          if (line && typeof this.pointAt === 'function') this.pointAt(line.block, n);
          return false;
        }
        if (!this.canEdit) return false;
        const patch = U.closest(e.target, '.lp-patch');
        if (patch && patch.dataset.block) return this.openBlock(patch.dataset.block, this.patchOffset(e, patch), { n });
        if (!line) { this.closeBlock({ commit: true }); this.selected = null; this.paint(); return false; }
        const run = U.closest(e.target, '.lp-run');
        if (run && run.classList && run.classList.contains('is-sup') && run.dataset.note) return this.openNote(run.dataset.note, { n, anchor: run });
        if (line.kind === 'note') return this.openNote(line.block, { n, anchor: lineEl });
        if (/^(front-|toc-|copyright)/.test(String(line.block || '')) || ['title', 'contents', 'contents-title', 'copyright'].includes(line.kind)) {
          if (typeof this.showBookDetails === 'function') this.showBookDetails();
          return false;
        }
        if (line.kind === 'source') return false;
        if (line.kind === 'separator') return this.selectBlock(line.block, n);
        return this.openBlock(line.block, this.pointOffset(e, lineEl, line), { n });
      },
      // A double-click in preview: edit mode at the clicked point.
      async onSheetDblClick(e, side) {
        if (this.mode === 'edit' || !this.canEdit) return false;
        const n = this.shownNumbers[side];
        const page = n ? ctx.pages.get(n) : null;
        const lineEl = U.closest(e.target, '.lp-line');
        const line = page && lineEl ? page.lines[Number(lineEl.dataset.i)] : null;
        const offset = line ? this.pointOffset(e, lineEl, line) : 0;
        try { if (typeof window.getSelection === 'function') window.getSelection().removeAllRanges(); } catch (_) { /* fine */ }
        if (!this.enterEdit(true)) return false;
        if (!line || line.kind === 'note' || line.kind === 'separator' || line.kind === 'source' || /^(front-|toc-|copyright)/.test(String(line.block || ''))) return true;
        return this.openBlock(line.block, offset, { n });
      },
      // The plain offset under the pointer: the text position the browser finds there, mapped through the run's
      // range; a point beside the text takes the line's nearer end.
      pointOffset(e, lineEl, line) {
        const hit = this.caretFromPoint(e.clientX, e.clientY);
        if (hit && hit.node && typeof lineEl.contains === 'function' && lineEl.contains(hit.node)) {
          const runEl = U.closest(hit.node.nodeType === 3 ? hit.node.parentElement : hit.node, '.lp-run');
          if (runEl) return G.hitOffset(line, Number(runEl.dataset.r), this.textIndex(runEl, hit.node, hit.offset), this.plainOf(line.block));
        }
        const r = U.rect(lineEl);
        if (!r.width) return line.start;
        const rtl = line.dir !== 'ltr';
        const share = Math.min(1, Math.max(0, rtl ? (r.right - e.clientX) / r.width : (e.clientX - r.left) / r.width));
        return Math.round(line.start + share * (line.end - line.start));
      },
      caretFromPoint(x, y) {
        if (!U.hasDOM) return null;
        try {
          if (typeof document.caretPositionFromPoint === 'function') {
            const p = document.caretPositionFromPoint(x, y);
            return p ? { node: p.offsetNode, offset: p.offset } : null;
          }
          if (typeof document.caretRangeFromPoint === 'function') {
            const r = document.caretRangeFromPoint(x, y);
            return r ? { node: r.startContainer, offset: r.startOffset } : null;
          }
        } catch (_) { /* outside the document */ }
        return null;
      },
      // The index of (node, offset) in the text of `el` (its text nodes in order: a run may hold marks).
      textIndex(el, node, offset) {
        let index = 0;
        let found = null;
        const walk = (n) => {
          if (found !== null) return;
          if (n === node) { found = index + (n.nodeType === 3 ? offset : 0); return; }
          if (n.nodeType === 3) { index += n.data.length; return; }
          (n.childNodes || []).forEach((c) => walk(c));
        };
        walk(el);
        return found === null ? index : found;
      },
      // A click in a patch (a block drawn by the browser): the offset counted in its markup (a call is one).
      patchOffset(e, patch) {
        const hit = this.caretFromPoint(e.clientX, e.clientY);
        if (!hit || !patch.contains(hit.node)) return 0;
        let index = 0;
        let found = null;
        const walk = (n) => {
          if (found !== null) return;
          if (n === hit.node) { found = index + (n.nodeType === 3 ? hit.offset : 0); return; }
          if (n.nodeType === 3) { index += n.data.length; return; }
          if (n.nodeName === 'SUP' || n.nodeName === 'BR' || (n.classList && n.classList.contains('ed-pb'))) { index += 1; return; }
          (n.childNodes || []).forEach((c) => walk(c));
        };
        walk(patch);
        return found === null ? index : found;
      },

      // ------------------------------------------------------------ opening a paragraph in place
      async openBlock(block, offset, at = {}) {
        if (!this.canEdit || !B() || !block) return false;
        const gen = (ctx.openGen += 1);
        if (ctx.ed && ctx.openId === block) { ctx.ed.setOffset(offset); ctx.ed.focus(); return true; }
        await this.closeBlock({ commit: true });
        const cid = await this.chapterFor(block, at.n || this.current);
        if (!cid || gen !== ctx.openGen) { if (!cid) U.toast('لم يُعثر على الفقرة في فصول هذه الصفحة'); return false; }
        const found = B().locate(ctx.nodes, block);
        if (!found) return false;
        if (found.node.type === 'separator') return this.selectBlock(block, at.n);
        let n = at.n && this.sideOf(at.n) ? at.n : 0;
        if (!n) {
          const c = G.caretLine(ctx.pages, block, offset);
          if (c) { if (!this.sideOf(c.n)) this.showPage(c.n, { instant: true, keepOpen: true }); n = c.n; }
          else n = this.current;
        }
        const side = this.sideOf(n) || 'right';
        const host = ctx.dom.sheets && ctx.dom.sheets[side] ? ctx.dom.sheets[side].host : null;
        if (!host) return false;
        const box = document.createElement('div');
        box.className = 'lp-patch is-open';
        box.dataset.block = block;
        host.appendChild(box);
        ctx.box = box;
        ctx.openId = block;
        ctx.anchor = { n, side, offset: Number(offset) || 0, after: at.after || null, top: null };
        this.selected = null;
        ctx.ed = B().createBlock(box, {
          node: found.node,
          offset: Number(offset) || 0,
          numberOf: (id) => this.noteNumber(id),
          onChange: () => this.onBlockChange(),
          onSelection: () => this.onBlockSelection(),
          onBoundary: (kind, info) => this.onBoundary(kind, info),
          onFootnote: (note) => this.openNote(note.id, { anchor: note.dom, fromEditor: true }),
          onUncertain: (w) => this.openWord(w),
        });
        ctx.base = jsonOf(ctx.ed.getNode());
        if (ctx.ed.togglePageMarks) ctx.ed.togglePageMarks(this.pageMarks);
        this.open = { block, n, style: ctx.ed.style() };
        this.styleBox();
        this.onBlockSelection();
        this.paint();
        this.anchorOpen(true);
        ctx.ed.focus();
        if (typeof this.pointAt === 'function') this.pointAt(block, n);
        return true;
      },
      // The open box's face, size, measure, leading and indent: the block's own lines on the page when it has
      // them (the engine's numbers), else the stylesheet's.
      boxLook(node, page) {
        const s = this.sheet || {};
        const body = Number(s.body_size_pt) || 13;
        const leading = Number(s.line_height) || 1.7;
        const scale = s.heading_scale || {};
        const lines = page ? (page.lines || []).filter((l) => l.block === (node.attrs && node.attrs.id) && l.kind !== 'note') : [];
        const lead = lines.length ? (lines[0].runs || []).find((r) => !r.sup && String(r.text || '').trim()) : null;
        const m = G.measure(page || { margins: {}, width_pt: 0 });
        let cls = 'is-body';
        let fs = body;
        let lh = body * leading;
        let inset = 0;
        let indent = (Number(s.indent_em) || 0) * body;
        if (node.type === 'heading') {
          const h2 = node.attrs && node.attrs.level === 2;
          cls = h2 ? 'is-h2' : 'is-h1';
          fs = body * (Number(h2 ? scale.h2 : scale.h1) || (h2 ? 1.25 : 1.6));
          lh = fs * (h2 ? 1.4 : 1.35);
          indent = 0;
        } else if (node.type === 'title') {
          cls = 'is-title';
          fs = body * (Number(scale.h1) || 1.6) * 1.4;
          lh = fs * 1.4;
          indent = 0;
        } else {
          const style = node.attrs && node.attrs.style;
          if (style === 'quote') { cls = 'is-quote'; inset = 2 * body; indent = 0; }
          else if (style === 'verse' || style === 'center') { cls = `is-${style}`; indent = 0; }
          else if (!lines.length || lines[0].style === 'body' || lines[0].kind === 'body') cls = 'is-body';
        }
        if (lead && Number(lead.size_pt)) fs = Number(lead.size_pt);
        if (lines.length && Number(lines[0].h)) lh = Number(lines[0].h);
        return { cls, x: m.x + inset, w: Math.max(40, m.w - 2 * inset), fs, lh, indent };
      },
      styleBox(el, node, page) {
        const box = el || ctx.box;
        if (!box) return;
        const target = node || (ctx.ed ? ctx.ed.getNode() : null);
        const pg = page || (ctx.anchor ? ctx.pages.get(ctx.anchor.n) : null);
        if (!target) return;
        const look = this.boxLook(target, pg);
        box.className = `lp-patch ${look.cls}${box === ctx.box ? ' is-open' : ''}`;
        const set = (k, v) => { if (box.style && box.style.setProperty) box.style.setProperty(k, G.num(v)); };
        set('--x', look.x);
        set('--w', look.w);
        set('--fs', look.fs);
        set('--lh', look.lh);
        set('--indent', look.indent);
      },
      // Where the open paragraph sits: the line of the caret (in the layout) under the same line of the editor.
      anchorOpen(first) {
        if (!ctx.ed || !ctx.anchor) return;
        const a = ctx.anchor;
        const page = ctx.pages.get(a.n);
        const scale = this.scale(a.side);
        if (!page) return;
        const offset = first ? a.offset : ctx.ed.offset();
        const i = G.lineAt(page, ctx.openId, offset);
        if (i >= 0) {
          const line = page.lines[i];
          const inner = scale ? ctx.ed.lineTop(offset) / scale : 0;
          a.top = line.y - inner;
          a.after = null;
        } else if (a.top === null) {
          const f = G.flowAfter(page, a.after);
          a.top = f.top;
        }
        this.layoutPatches();
      },

      // ------------------------------------------------------------ patches: blocks the browser draws for now
      dropPatches() {
        ctx.patches.forEach((p) => { if (p.el && p.el.remove) p.el.remove(); });
        ctx.patches.clear();
      },
      // Called after every paint: the static patches of the edited blocks, the open box, the lines moved, the
      // find matches inside the open paragraph (its lines are hidden: the editor draws them).
      afterPaint() {
        this.layoutPatches();
        this.syncOpenFind();
      },
      syncOpenFind() {
        if (!ctx.ed || typeof ctx.ed.setFind !== 'function') { ctx.findKey = ''; return; }
        const f = this.find || { results: [], index: -1 };
        const ranges = (f.results || []).map((m, i) => ({ m, i })).filter(({ m }) => m.block === ctx.openId && !m.note)
          .map(({ m, i }) => ({ start: m.start, end: m.end, current: i === f.index }));
        const key = `${ctx.openId}|${JSON.stringify(ranges)}`;
        if (key === ctx.findKey) return;
        ctx.findKey = key;
        try { ctx.ed.setFind(ranges); } catch (_) { /* the editor went away */ }
      },
      scheduleReflow() {
        if (reflowQueued) return;
        reflowQueued = true;
        U.frame(() => { reflowQueued = false; this.layoutPatches(); });
      },
      layoutPatches() {
        const sheets = ctx.dom.sheets || {};
        const diff = this.mode === 'edit' || ctx.nodes !== ctx.laid ? this.editDiff() : { changed: new Set(), removed: new Set(), added: [] };
        const keep = new Set();
        const s = this.shownNumbers;
        ['right', 'left'].forEach((side) => {
          const host = sheets[side];
          const n = s[side];
          const page = n ? ctx.pages.get(n) : null;
          if (!host || !host.host || !page) return;
          const scale = this.scale(side) || 1;
          const regions = [];
          const measure = (el) => (el && el.offsetHeight ? el.offsetHeight / scale : 0);
          const bottomOf = (id) => {
            const r = regions.find((x) => x.key === id);
            if (r) return r.oldBottom;
            const f = id ? G.flowAround(page, id) : null;
            return f && f.lines.length ? f.bottom : null;
          };
          // blocks edited and closed: drawn from their nodes where they start on this page
          diff.changed.forEach((id) => {
            if (id === ctx.openId) return;
            const f = G.flowAround(page, id);
            if (!f.lines.length || !page.lines[f.lines[0]].first) return;
            const el = this.patchEl(id, side);
            if (!el) return;
            keep.add(id);
            regions.push({ key: id, el, oldTop: f.top, oldBottom: f.bottom, height: measure(el) });
          });
          // blocks removed: their room closes
          diff.removed.forEach((id) => {
            const f = G.flowAround(page, id);
            if (f.lines.length) regions.push({ key: id, el: null, oldTop: f.top, oldBottom: f.bottom, height: 0 });
          });
          // new blocks: under the block before them
          diff.added.forEach(({ id, after }) => {
            if (id === ctx.openId) return;
            const base = bottomOf(after);
            if (base === null) return;
            const el = this.patchEl(id, side);
            if (!el) return;
            keep.add(id);
            regions.push({ key: id, el, oldTop: base + 0.01, oldBottom: base + 0.01, height: measure(el) });
          });
          // the open paragraph: on its lines, anchored at the caret's line; a new one under the block before it
          if (ctx.ed && ctx.box && ctx.anchor && ctx.anchor.n === n) {
            if (ctx.box.parentNode !== host.host) this.moveBox(side);
            const f = G.flowAround(page, ctx.openId);
            if (f.lines.length) {
              regions.push({ key: ctx.openId, el: ctx.box, open: true, oldTop: f.top, oldBottom: f.bottom, anchorTop: ctx.anchor.top === null ? f.top : ctx.anchor.top, height: measure(ctx.box) });
            } else {
              const before = ctx.anchor.after || (diff.added.find((a) => a.id === ctx.openId) || {}).after;
              const base = bottomOf(before);
              const top = base !== null ? base + 0.01 : ctx.anchor.top !== null ? ctx.anchor.top : G.flowAfter(page, null).top;
              regions.push({ key: ctx.openId, el: ctx.box, oldTop: top, oldBottom: top, height: measure(ctx.box) });
            }
          }
          const clipTop = this.clipTop(page);
          if (host.host.style && host.host.style.setProperty) {
            host.host.style.setProperty('--clip', G.num(clipTop));
            // and the foot of the body: an open paragraph that runs on to the next page never covers the
            // footnotes or the page number (its lines past the body are the next page's, as in the PDF)
            host.host.style.setProperty('--clip-b', G.num(Math.max(0, (Number(page.height_pt) || 0) - G.bodyBottom(page))));
          }
          const out = flow(page, regions, G.bodyBottom(page));
          out.regions.forEach((r) => { if (r.el && r.el.style && r.el.style.setProperty) r.el.style.setProperty('--top', G.num(r.screenTop - clipTop)); });
          const lines = host.lines && host.lines.children ? Array.from(host.lines.children) : [];
          lines.forEach((el) => {
            if (!el.classList || !el.classList.contains('lp-line')) return;
            const info = out.lines[Number(el.dataset.i)];
            if (!info) return;
            if (el.style && el.style.setProperty) el.style.setProperty('--dy', G.num(info.dy));
            el.classList.toggle('is-moved', info.dy !== 0);
            el.classList.toggle('is-over', info.over);
          });
        });
        ctx.patches.forEach((p, id) => { if (!keep.has(id)) { if (p.el && p.el.remove) p.el.remove(); ctx.patches.delete(id); } });
      },
      // The top of the drawable body (the running header stays visible above it).
      clipTop(page) {
        const m = (page && page.margins) || {};
        const top = Number(m.top) || 0;
        const header = page && page.header ? page.header.y + page.header.h : 0;
        return Math.max(header, top - 4);
      },
      patchEl(id, side) {
        const host = ctx.dom.sheets && ctx.dom.sheets[side] ? ctx.dom.sheets[side].host : null;
        const at = B() ? B().locate(ctx.nodes, id) : null;
        if (!host || !at) return null;
        const json = jsonOf(at.node);
        let p = ctx.patches.get(id);
        if (!p || !p.el || p.side !== side) {
          if (p && p.el && p.el.remove) p.el.remove();
          const el = document.createElement('div');
          el.dataset.block = id;
          host.appendChild(el);
          p = { el, json: '', side };
          ctx.patches.set(id, p);
        }
        if (p.json !== json) {
          p.el.innerHTML = B().nodeHtml(at.node, { numberOf: (nid) => this.noteNumber(nid) });
          p.json = json;
          this.styleBox(p.el, at.node, ctx.pages.get(this.shownNumbers[side]));
          p.el.classList.add('is-static');
        }
        return p.el;
      },
      moveBox(side) {
        const host = ctx.dom.sheets && ctx.dom.sheets[side] ? ctx.dom.sheets[side].host : null;
        if (!host || !ctx.box) return;
        const focused = ctx.ed && ctx.ed.isFocused;
        const offset = ctx.ed ? ctx.ed.offset() : 0;
        host.appendChild(ctx.box);
        if (ctx.anchor) ctx.anchor.side = side;
        if (focused && ctx.ed) { ctx.ed.setOffset(offset); ctx.ed.focus(); }
      },

      // What each page draws besides its lines: the open, edited and removed blocks hidden (their patches
      // stand in), the decorations of the side panel, the scan page marks in edit mode.
      paintOpts(n) {
        const hidden = new Set();
        if (ctx.openId) hidden.add(ctx.openId);
        const diff = ctx.nodes !== ctx.laid ? this.editDiff() : null;
        if (diff) { diff.changed.forEach((id) => hidden.add(id)); diff.removed.forEach((id) => hidden.add(id)); }
        const decos = typeof this.decorationsFor === 'function' ? this.decorationsFor(n) : new Map();
        return {
          plain: (block) => this.plainOf(block),
          hidden,
          decos: (block) => decos.get(block) || null,
          marks: this.mode === 'edit' && this.pageMarks ? (block) => this.marksOf(block) : null,
          selected: this.selected ? this.selected.block : null,
          flash: this.flash && this.flash.n === n ? this.flash : null,
        };
      },
      // A separator (or another block without text) chosen: outlined; Delete removes it, the style picker
      // turns it into text.
      async selectBlock(block, n) {
        await this.closeBlock({ commit: true });
        const cid = await this.chapterFor(block, n || this.current);
        if (!cid) return false;
        this.selected = { block, n: n || this.current, kind: 'separator' };
        this.blockStyle = 'separator';
        this.paint();
        return true;
      },

      // ------------------------------------------------------------ typing, the pause, commit
      onBlockChange() {
        if (!ctx.ed) return;
        this.textDirty = true;
        if (this.editSave.state !== 'saving') this.editSave = { state: 'dirty', message: '' };
        clearTimeout(T.pause);
        T.pause = setTimeout(() => this.pause(), relayoutMs);
        const store = typeof Alpine !== 'undefined' && typeof Alpine.store === 'function' ? Alpine.store('bookToast') : null;
        if (store && store.visible && store.local) store.hide();
        const style = ctx.ed.style();
        if (this.open && style !== this.open.style) { this.open = Object.assign({}, this.open, { style }); this.styleBox(); }
        this.onBlockSelection();
        if (this.pop.kind === 'note' && noteEd) {
          const note = ctx.ed.noteAt(this.note.id);
          if (!note) this.closePop(); else noteEd.setContent(note.content);
        }
        this.scheduleReflow();
      },
      onBlockSelection() {
        if (!ctx.ed) return;
        this.blockStyle = ctx.ed.style();
        this.bold = ctx.ed.isBold();
        this.italic = ctx.ed.isItalic();
        const node = ctx.ed.getNode() || {};
        const a = node.attrs || {};
        this.flags = { breakBefore: a.breakBefore === true, keepWithNext: a.keepWithNext === true };
      },
      pause() {
        clearTimeout(T.pause);
        if (this.commitOpen()) return this.saveChapter();
        return Promise.resolve(true);
      },
      // The open paragraph into the chapter's nodes (a new version of them; the old one kept for the undo).
      commitOpen() {
        if (!ctx.ed || !ctx.openId) return false;
        const node = ctx.ed.getNode();
        if (!node) return false;
        const json = jsonOf(node);
        if (json === ctx.base) return false;
        this.pushHistory();
        ctx.nodes = B().replaceBlock(ctx.nodes, ctx.openId, [node]);
        ctx.base = json;
        this.textDirty = true;
        return true;
      },
      // A structural edit: the nodes replaced at once (split, merge, a separator, a flag), then saved.
      change(nodes) {
        this.pushHistory();
        ctx.nodes = nodes;
        this.textDirty = true;
        this.editSave = { state: 'dirty', message: '' };
        this.saveChapter();
      },

      // ------------------------------------------------------------ the chapter's undo
      caretRef() { return ctx.ed ? { block: ctx.openId, offset: ctx.ed.offset() } : null; },
      pushHistory() {
        const h = ctx.hist.get(this.editChapterId);
        if (!h) return;
        h.past.push({ nodes: ctx.nodes, caret: this.caretRef() });
        if (h.past.length > HISTORY) h.past.shift();
        h.future = [];
        this.historyFlags();
      },
      historyFlags() {
        const h = ctx.hist.get(this.editChapterId);
        this.canUndo = Boolean(h && h.past.length);
        this.canRedo = Boolean(h && h.future.length);
      },
      undo() { return this.travel(-1); },
      redo() { return this.travel(1); },
      travel(dir) {
        if (!this.canEdit || !this.editChapterId) return false;
        clearTimeout(T.pause);
        this.commitOpen();
        const h = ctx.hist.get(this.editChapterId);
        const from = dir < 0 ? h.past : h.future;
        const to = dir < 0 ? h.future : h.past;
        if (!from.length) { U.toast(dir < 0 ? 'لا شيء للتراجع عنه' : 'لا شيء لإعادته'); return false; }
        const entry = from.pop();
        to.push({ nodes: ctx.nodes, caret: this.caretRef() });
        ctx.nodes = entry.nodes;
        this.historyFlags();
        if (ctx.ed && ctx.openId) {
          const at = B().locate(ctx.nodes, ctx.openId);
          if (at && at.node.type !== 'separator') {
            const caret = entry.caret && entry.caret.block === ctx.openId ? entry.caret.offset : ctx.ed.offset();
            ctx.ed.setNode(at.node, Math.min(caret, B().plainText(at.node).length));
            ctx.base = jsonOf(ctx.ed.getNode());
            this.onBlockSelection();
            this.styleBox();
          } else this.closeBlock({ commit: false });
        }
        this.textDirty = true;
        this.editSave = { state: 'dirty', message: '' };
        this.saveChapter();
        this.paint();
        return true;
      },

      // ------------------------------------------------------------ the paragraph's edges
      onBoundary(kind, info = {}) {
        if (!ctx.ed) return false;
        switch (kind) {
          case 'split': this.splitOpen(info); return true;
          case 'mergeBackward': this.mergeOpen(-1); return true;
          case 'mergeForward': this.mergeOpen(1); return true;
          case 'up': this.moveTo(-1, info.x, 'last'); return true;
          case 'down': this.moveTo(1, info.x, 'first'); return true;
          case 'prev': this.moveTo(-1, null, 'end'); return true;
          case 'next': this.moveTo(1, null, 'start'); return true;
          case 'escape': this.closeBlock({ commit: true }); this.focusStage(); return true;
          case 'undo': this.undo(); return true;
          case 'redo': this.redo(); return true;
          case 'save': this.saveNow(); return true;
          case 'separator': this.addSeparator(); return true;
          case 'paste': this.pasteBlocks(info); return true;
          default: return false;
        }
      },
      neighbourOf(id, dir) {
        const flat = B().flatBlocks(ctx.nodes);
        const i = flat.findIndex((b) => b.id === id);
        if (i < 0) return null;
        return flat[i + dir] || null;
      },
      // Enter: the paragraph is cut at the caret; the second half opens under the first.
      splitOpen(info) {
        const id = ctx.openId;
        const node = info.node || ctx.ed.getNode();
        const from = Number(info.from) || 0;
        const to = Number(info.to) || from;
        const base = to > from ? Object.assign({}, node, { content: B().replacePlain(node.content || [], from, to, '') }) : node;
        const [a, b] = B().splitNode(base, from);
        const anchor = ctx.anchor ? Object.assign({}, ctx.anchor) : null;
        this.pushHistory();
        ctx.nodes = B().replaceBlock(ctx.nodes, id, [a, b]);
        this.textDirty = true;
        this.dropEditor();
        this.saveChapter();
        return this.openBlock(b.attrs.id, 0, { n: anchor ? anchor.n : this.current, after: id });
      },
      // Backspace at the start (dir −1) joins the paragraph to the one before; Delete at the end (dir +1) the
      // next one to it. A separator next to it is removed instead; a chapter's edge stays.
      mergeOpen(dir) {
        const id = ctx.openId;
        const other = this.neighbourOf(id, dir);
        if (!other) { U.toast(dir < 0 ? 'هذه أول فقرة في الفصل' : 'هذه آخر فقرة في الفصل'); return false; }
        const current = ctx.ed.getNode();
        if (other.node.type === 'separator') {
          this.commitOpen();
          this.change(B().replaceBlock(ctx.nodes, other.id, []));
          this.paint();
          return true;
        }
        const [first, second] = dir < 0 ? [other.node, current] : [current, other.node];
        const merged = B().mergeNodes(first, second);
        if (!merged) return false;
        this.pushHistory();
        let nodes = B().replaceBlock(ctx.nodes, dir < 0 ? id : other.id, []);
        nodes = B().replaceBlock(nodes, dir < 0 ? other.id : id, [merged.node]);
        ctx.nodes = nodes;
        this.textDirty = true;
        if (dir > 0) {
          const caret = ctx.ed.offset();
          ctx.ed.setNode(merged.node, caret);
          ctx.base = jsonOf(ctx.ed.getNode());
          this.saveChapter();
          this.paint();
          return true;
        }
        const n = ctx.anchor ? ctx.anchor.n : this.current;
        this.dropEditor();
        this.saveChapter();
        const c = G.caretLine(ctx.pages, other.id, merged.offset);
        return this.openBlock(other.id, merged.offset, { n: c && this.sideOf(c.n) ? c.n : n });
      },
      // ↑ / ↓ past the first or last line, → / ← at an edge: the neighbouring block (the next chapter's at the
      // chapter's edge), the caret on its nearer line under the same x.
      async moveTo(dir, x, where) {
        const id = ctx.openId;
        let target = this.neighbourOf(id, dir);
        while (target && target.node.type === 'separator') target = this.neighbourOf(target.id, dir);
        if (!target) {
          const cid = this.chapter ? (dir < 0 ? this.chapter.prev : this.chapter.next) : null;
          if (!cid) { U.toast(dir < 0 ? 'هذا أول الكتاب' : 'هذا آخر الكتاب'); return false; }
          await this.closeBlock({ commit: true });
          if (!(await this.loadChapter(cid))) return false;
          const flat = B().flatBlocks(ctx.nodes).filter((b) => b.node.type !== 'separator');
          target = dir < 0 ? flat[flat.length - 1] : flat[0];
          if (!target) return false;
        }
        const end = B().plainText(target.node).length;
        const offset = where === 'last' || where === 'end' ? end : 0;
        const c = G.caretLine(ctx.pages, target.id, offset);
        const n = c ? c.n : ctx.anchor ? ctx.anchor.n : this.current;
        if (c && !this.sideOf(c.n)) this.showPage(c.n, { instant: true, keepOpen: true });
        const ok = await this.openBlock(target.id, offset, { n, after: c ? null : dir > 0 ? id : null });
        if (ok && x !== null && x !== undefined && ctx.ed && ctx.ed.placeAtX) ctx.ed.placeAtX(x, where === 'last' ? 'last' : 'first');
        return ok;
      },
      addSeparator() {
        const id = ctx.openId;
        this.commitOpen();
        const node = ctx.ed.getNode();
        const a = (node && node.attrs) || {};
        const sep = { type: 'separator', attrs: { id: B().newId('e'), sourcePages: a.sourcePages || [], sourceLineIds: a.sourceLineIds || [], reviewed: a.reviewed !== false } };
        this.change(B().replaceBlock(ctx.nodes, id, [node, sep]));
        this.paint();
        return true;
      },
      pasteBlocks(info) {
        const id = ctx.openId;
        const result = B().insertBlocks(info.node || ctx.ed.getNode(), info.from, info.to, info.blocks);
        const n = ctx.anchor ? ctx.anchor.n : this.current;
        this.pushHistory();
        ctx.nodes = B().replaceBlock(ctx.nodes, id, result.blocks);
        this.textDirty = true;
        this.dropEditor();
        this.saveChapter();
        const target = result.blocks[result.caret.index];
        return this.openBlock(target.attrs.id, result.caret.offset, { n, after: result.caret.index ? result.blocks[result.caret.index - 1].attrs.id : null });
      },
      dropEditor() {
        clearTimeout(T.pause);
        this.closePop();
        if (ctx.ed) { try { ctx.ed.destroy(); } catch (_) { /* gone */ } }
        if (ctx.box && ctx.box.remove) ctx.box.remove();
        ctx.ed = null;
        ctx.box = null;
        ctx.findKey = '';
        ctx.openId = null;
        ctx.anchor = null;
        this.open = null;
      },
      // Close the open paragraph (its text written into the nodes and saved first).
      async closeBlock(opts = {}) {
        if (!ctx.ed) return false;
        if (opts.commit !== false && this.commitOpen()) this.saveChapter();
        this.dropEditor();
        this.paint();
        return true;
      },
      beforeTurn(_target, opts) {
        if (!opts || !opts.keepOpen) { if (ctx.ed) this.closeBlock({ commit: true }); this.selected = null; }
      },

      // ------------------------------------------------------------ saving and the re-layout
      get savePill() {
        const own = this.editSave.state;
        if (own === 'conflict') return { state: 'conflict', text: SAVE_TEXT.conflict };
        if (own === 'error') return { state: 'error', text: this.editSave.message && this.editSave.message.length < 40 ? this.editSave.message : SAVE_TEXT.error };
        if (own === 'saving' || this.sheetSave.state === 'saving') return { state: 'saving', text: SAVE_TEXT.saving };
        if (own === 'dirty') return { state: 'dirty', text: SAVE_TEXT.dirty };
        if (this.sheetSave.state) return { state: this.sheetSave.state, text: this.sheetSave.message };
        if (own === 'saved') return { state: 'saved', text: SAVE_TEXT.saved };
        return { state: '', text: '' };
      },
      retrySave() {
        if (this.editSave.state === 'error') return this.saveNow();
        if (this.sheetSave.state === 'error') return this.flushSheet();
        return false;
      },
      saveNow() {
        clearTimeout(T.pause);
        this.commitOpen();
        return this.saveChapter();
      },
      // One PUT at a time; a change made while it is on the wire is saved after it.
      async saveChapter() {
        if (!this.canEdit || !this.editChapterId || !urls.chapter) return false;
        if (saving) { again = true; return saving; }
        if (ctx.nodes === ctx.saved) { this.textDirty = false; return true; }
        saving = this.putChapter();
        let ok = false;
        try { ok = await saving; } finally { saving = null; }
        if (again) { again = false; return this.saveChapter(); }
        return ok;
      },
      async putChapter() {
        const cid = this.editChapterId;
        const nodes = ctx.nodes;
        if (!this.version) { this.editSave = { state: 'error', message: 'أعد تحميل الفصل قبل الحفظ.' }; return false; }
        this.editSave = { state: 'saving', message: '' };
        const r = await U.api(U.fill(urls.chapter, cid), { method: 'PUT', body: { content: { type: 'doc', content: nodes }, version: this.version } });
        if (this.editChapterId !== cid) return false;
        if (r.status === 409 && r.data) {
          this.editSave = { state: 'conflict', message: r.message };
          this.conflict = { open: true, version: r.data.version || '', content: r.data.content || null };
          this.liveMessage = r.message;
          return false;
        }
        if (!r.ok || !r.data) {
          this.editSave = { state: 'error', message: r.message };
          this.liveMessage = r.message;
          return false;
        }
        const data = r.data;
        ctx.saved = nodes;
        this.textDirty = ctx.nodes !== nodes || Boolean(ctx.ed && jsonOf(ctx.ed.getNode()) !== ctx.base);
        this.editSave = { state: this.textDirty ? 'dirty' : 'saved', message: '' };
        if (data.reload) { await this.reloadAfterSplit(data); return true; }
        this.version = data.version || this.version;
        if (Array.isArray(data.chapters) && data.chapters[0] && this.chapter) {
          this.chapter.title = data.chapters[0].title;
          const row = this.chapters.find((c) => c.id === cid);
          if (row) row.title = data.chapters[0].title;
          const sum = this.summaryOf(cid);
          if (sum) sum.version = data.chapters[0].version;
        }
        if (data.changed && typeof this.afterSave === 'function') this.afterSave(cid);
        this.followRelayout(data.relayout, nodes);
        this.pollNow();
        return true;
      },
      // A new level-1 heading split the chapter (or a deleted one merged it): the chapter list and the chapter
      // again, the open paragraph kept where it went.
      async reloadAfterSplit(data) {
        const open = ctx.openId;
        const caret = ctx.ed ? ctx.ed.offset() : 0;
        this.dropEditor();
        const list = await U.api(urls.chapters);
        if (list.ok && Array.isArray(list.data)) {
          this.chapters = list.data.map((c) => ({ id: c.id, number: c.number, kind: c.kind, title: c.title }));
          this.summaries = list.data;
        }
        const formed = Array.isArray(data.chapters) ? data.chapters.map((c) => c.id) : [];
        this.editChapterId = null;
        const target = data.id || formed[0];
        if (!(await this.loadChapter(target, { force: true }))) return false;
        if (open && !B().locate(ctx.nodes, open)) {
          const other = formed.find((c) => c !== target);
          if (other) await this.loadChapter(other, { force: true });
        }
        U.toast(formed.length > 1 ? 'انقسم الفصل؛ حُدّثت قائمة الفصول' : 'اندمج الفصل؛ حُدّثت قائمة الفصول');
        this.requestRelayout(this.editChapterId);
        if (open && B().locate(ctx.nodes, open)) this.openBlock(open, caret, { n: ctx.anchor ? ctx.anchor.n : this.current });
        return true;
      },
      // POST a re-layout of a chapter (after a server-side change, a page setup change) and follow it.
      async requestRelayout(cid) {
        if (!urls.relayout || !cid) return false;
        const r = await U.api(U.fill(urls.relayout, cid), { method: 'POST', body: {} });
        if (!r.ok || !r.data) return false;
        this.followRelayout(r.data, cid === this.editChapterId ? ctx.nodes : null);
        return true;
      },
      // Poll a re-layout until it is done (the long poll waits up to 2 s per request), then splice its pages.
      // `nodes`: what was saved (the layout will show it). A superseded one is dropped: a newer save follows.
      async followRelayout(payload, nodes) {
        if (!payload) return false;
        const gen = (followGen += 1);
        let p = payload;
        this.relayout = { state: 'running', id: p.id || null, error: '' };
        for (let i = 0; i < FOLLOW_TRIES; i += 1) {
          if (p.status === 'superseded' || (p.status === 'cancelled' && p.error === 'superseded')) { if (gen === followGen) this.relayout = { state: '', id: null, error: '' }; return false; }
          if (p.status === 'error') {
            if (gen === followGen) this.relayout = { state: 'error', id: p.id || null, error: p.error || '' };
            return false;
          }
          const done = p.status === 'done';
          const needPages = done && !(p.result && (p.result.unchanged || p.result.full)) && !(p.pages && p.pages.length) && p.url;
          if (done && !needPages) break;
          if (!p.url) { if (gen === followGen) this.relayout = { state: '', id: null, error: '' }; return false; }
          const r = await U.api(`${p.url}${p.url.includes('?') ? '&' : '?'}${needPages ? '' : `wait=${WAIT_S}`}`);
          if (gen !== followGen && !(r.ok && r.data && r.data.status === 'done')) return false;
          if (!r.ok || !r.data) { this.relayout = { state: 'error', id: p.id || null, error: r.message }; return false; }
          p = r.data;
        }
        if (p.status !== 'done') { if (gen === followGen) this.relayout = { state: '', id: null, error: '' }; return false; }
        ctx.nextLaid = nodes;
        const info = this.applyRelayoutPayload(p);
        if (info && info.refetch) {
          // a whole-book layout (or another revision): the patches stay until the new pages are drawn, so the
          // page never shows the text from before the edit in the meantime
          const fetched = await info.pending;
          if (!fetched && ctx.nextLaid === nodes) ctx.nextLaid = null;
          this.afterRelayout(info);
        } else if (info && info.unchanged) this.afterRelayout(info);
        if (gen === followGen) this.relayout = { state: '', id: null, error: '' };
        return true;
      },
      // The layout on screen now shows `ctx.nextLaid`: patches of those blocks go, the open paragraph is
      // anchored again on the line the engine put its caret on (the page turns if the caret moved there).
      afterRelayout() {
        if (ctx.nextLaid) { ctx.laid = ctx.nextLaid; ctx.nextLaid = null; plainCache = null; }
        if (ctx.ed && ctx.openId) {
          const caret = ctx.ed.offset();
          const c = G.caretLine(ctx.pages, ctx.openId, caret);
          if (c) {
            if (!this.sideOf(c.n)) this.showPage(c.n, { instant: true, keepOpen: true });
            const side = this.sideOf(c.n) || 'right';
            ctx.anchor = Object.assign(ctx.anchor || {}, { n: c.n, side, after: null });
            if (ctx.box && ctx.dom.sheets && ctx.dom.sheets[side] && ctx.box.parentNode !== ctx.dom.sheets[side].host) this.moveBox(side);
            const page = ctx.pages.get(c.n);
            const scale = this.scale(side);
            const line = page.lines[c.i];
            if (line && scale) ctx.anchor.top = line.y - ctx.ed.lineTop(caret) / scale;
            if (ctx.ed.setNumbers) ctx.ed.setNumbers((id) => this.noteNumber(id));
            this.styleBox();
            this.open = Object.assign({}, this.open || {}, { n: c.n });
          }
        }
        this.paint();
      },
      afterRevision() {
        // a new revision of the whole layout (the book render adopted): what it shows is the saved text
        if (ctx.saved && ctx.nodes === ctx.saved) { ctx.laid = ctx.nodes; plainCache = null; }
        if (ctx.ed && ctx.openId) U.frame(() => this.afterRelayout());
      },
      // The conflict (409): take the other window's text, or keep this one (the server's copy is kept first).
      reloadConflict() {
        if (!this.conflict.open) return false;
        const { content, version } = this.conflict;
        this.conflict = { open: false, version: '', content: null };
        this.dropEditor();
        if (!content) return this.loadChapter(this.editChapterId, { force: true });
        this.applyChapter({ ...this.chapter, source_pages: this.chapter.sourcePages, id: this.editChapterId, version, content }, { force: true });
        this.requestRelayout(this.editChapterId);
        U.toast('أُعيد تحميل الفصل من الخادم');
        this.paint();
        return true;
      },
      async keepMine() {
        if (!this.conflict.open) return false;
        const version = this.conflict.version;
        const s = await U.api(urls.snapshots, { method: 'POST', body: { label: `نص نافذة أخرى قبل استبداله · «${this.chapter ? this.chapter.title : ''}»` } });
        if (!s.ok) { U.toast(s.message); return false; }
        this.conflict = { open: false, version: '', content: null };
        this.version = version;
        ctx.saved = null;
        return this.saveChapter();
      },
      guardUnload(e) {
        if (typeof this.flushSheet === 'function' && Object.keys(this.dirty || {}).length) this.flushSheet({ keepalive: true });
        const pending = this.editDirty || Boolean(saving);
        if (!pending) return false;
        if (e && typeof e.preventDefault === 'function') e.preventDefault();
        if (e) e.returnValue = '';
        return true;
      },
      get editDirty() {
        return ctx.nodes !== ctx.saved && Boolean(this.editChapterId) || Boolean(ctx.ed && jsonOf(ctx.ed.getNode()) !== ctx.base);
      },

      // ------------------------------------------------------------ toolbar: styles, marks, flags, marks
      get styleLabel() {
        const key = this.blockStyle;
        const found = this.styles.find((s) => s.key === key);
        return found ? found.label : 'فقرة';
      },
      setStyle(key) {
        this.styleMenu = false;
        if (!this.canEdit) return false;
        if (key === 'footnote') return this.insertFootnote();
        if (ctx.ed) {
          if (key === 'separator') return this.addSeparator();
          const ok = ctx.ed.setStyle(key);
          this.onBlockSelection();
          this.styleBox();
          return ok;
        }
        if (this.selected && key !== 'separator') {
          // a separator becomes an empty paragraph of that style, opened
          const id = this.selected.block;
          const at = B().locate(ctx.nodes, id);
          if (!at) return false;
          const para = { type: key === 'heading1' || key === 'heading2' ? 'heading' : 'paragraph', attrs: Object.assign({}, at.node.attrs), content: [] };
          if (para.type === 'heading') para.attrs.level = key === 'heading2' ? 2 : 1;
          else if (['quote', 'verse', 'center'].includes(key)) para.attrs.style = key;
          const n = this.selected.n;
          this.selected = null;
          this.change(B().replaceBlock(ctx.nodes, id, [para]));
          return this.openBlock(id, 0, { n });
        }
        U.toast('ضع المؤشّر في فقرة أولًا');
        return false;
      },
      toggleBold() { if (ctx.ed && this.canEdit) { ctx.ed.toggleBold(); this.onBlockSelection(); } },
      toggleItalic() { if (ctx.ed && this.canEdit) { ctx.ed.toggleItalic(); this.onBlockSelection(); } },
      togglePageMarks() {
        this.pageMarks = !this.pageMarks;
        U.writeLocal(MARKS_KEY, this.pageMarks ? '1' : '0');
        if (ctx.ed && ctx.ed.togglePageMarks) ctx.ed.togglePageMarks(this.pageMarks);
        this.paint();
      },
      // «ابدأ صفحة جديدة» / «مع التالية» on the open (or chosen) block
      toggleFlag(name) {
        if (!this.canEdit || !['breakBefore', 'keepWithNext'].includes(name)) return false;
        const on = !this.flags[name];
        if (ctx.ed) {
          ctx.ed.setAttrs({ [name]: on || null });
          this.onBlockSelection();
          this.pause();
          return true;
        }
        // a separator chosen, or the paragraph «الفقرة» shows (the last one opened in this chapter)
        const pointed = this.pointed && B() && B().locate(ctx.nodes, this.pointed) ? this.pointed : null;
        const id = (this.selected && this.selected.block) || pointed;
        if (!id) { U.toast('ضع المؤشّر في فقرة أولًا'); this.flags = Object.assign({}, this.flags); return false; }
        if (!this.selected) {
          const a = (B().locate(ctx.nodes, id).node.attrs) || {};
          this.flags = { breakBefore: a.breakBefore === true, keepWithNext: a.keepWithNext === true };
        }
        const now = !this.flags[name];
        this.flags = Object.assign({}, this.flags, { [name]: now });
        this.change(B().setBlockAttrs(ctx.nodes, id, { [name]: now || null }));
        return true;
      },
      deleteSelected() {
        if (!this.selected || !this.canEdit) return false;
        const id = this.selected.block;
        this.selected = null;
        this.change(B().replaceBlock(ctx.nodes, id, []));
        this.paint();
        this.undoToast('حُذف الفاصل', () => this.undo(), true);
        return true;
      },
      // the block the side panel's «الفقرة» and «الأصل» describe
      get currentBlock() {
        void this.open; void this.selected; void this.flags; void this.pointedNode;
        const id = ctx.openId || (this.selected && this.selected.block) || this.pointed || null;
        if (!id || !B()) return null;
        const at = B().locate(ctx.nodes, id);
        if (at) return { id, node: at.node };
        // a block clicked in preview, its chapter not loaded for editing (panel.js lookupPointed)
        const p = this.pointedNode;
        return p && p.key === id ? { id: p.id, node: p.node } : null;
      },
      undoToast(message, undo, local) {
        const store = typeof Alpine !== 'undefined' && typeof Alpine.store === 'function' ? Alpine.store('bookToast') : null;
        if (store) store.show(message, undo, 8000, Boolean(local)); else U.toast(message);
      },

      // ------------------------------------------------------------ footnotes
      // The number the page prints for a note (its marker at the foot, «(3)»), else ''.
      noteNumber(id) {
        const seen = [this.shownNumbers.right, this.shownNumbers.left, ...ctx.pages.keys()];
        for (const n of seen) {
          const page = n ? ctx.pages.get(n) : null;
          if (!page) continue;
          for (const line of page.lines || []) {
            for (const run of line.runs || []) {
              if (run.note === id && (run.sup || line.kind === 'note')) {
                const m = /(\d+)/.exec(String(run.text || ''));
                if (m) return m[1];
              }
            }
          }
        }
        return '';
      },
      insertFootnote() {
        if (!ctx.ed || !this.canEdit) { U.toast('ضع المؤشّر داخل فقرة أولًا'); return false; }
        const note = ctx.ed.insertFootnote();
        if (!note) { U.toast('ضع المؤشّر داخل فقرة أولًا'); return false; }
        this.openNote(note.id, { anchor: note.dom, fromEditor: true });
        return true;
      },
      // The note's small editor, opening its paragraph first (the call is edited through it).
      async openNote(noteId, opts = {}) {
        if (!noteId || !this.canEdit || !B()) return false;
        if (!opts.fromEditor) {
          const cid = await this.chapterFor(noteId, opts.n || this.current);
          if (!cid) return false;
          const found = B().findNote(ctx.nodes, noteId);
          if (!found) return false;
          if (ctx.openId !== found.block) {
            const at = B().locate(ctx.nodes, found.block);
            let offset = 0;
            let pos = 0;
            (at.node.content || []).forEach((item) => { if (item.type === 'footnote' && item.attrs && item.attrs.id === noteId) offset = pos + 1; pos += item.type === 'text' ? String(item.text || '').length : 1; });
            const c = G.caretLine(ctx.pages, found.block, offset);
            const ok = await this.openBlock(found.block, offset, { n: c && this.sideOf(c.n) ? c.n : opts.n });
            if (!ok) return false;
          }
        }
        if (!ctx.ed) return false;
        const note = ctx.ed.noteAt(noteId);
        if (!note) return false;
        this.closePop();
        this.styleMenu = false; // one overlay at a time
        this.note = { id: noteId, number: this.noteNumber(noteId), sourcePage: note.sourcePage, orphan: note.orphan };
        this.pop = { kind: 'note', style: this.pop.style, above: false };
        opened();
        const anchor = opts.anchor && opts.anchor.isConnected !== false ? opts.anchor : note.dom;
        this.placePop(anchor);
        const mount = () => {
          const host = this.$refs && this.$refs.noteHost;
          if (!host || !B().createNote) return;
          if (noteEd) { noteEd.destroy(); noteEd = null; host.textContent = ''; }
          noteEd = B().createNote(host, {
            content: note.content,
            onUpdate: (content) => { if (ctx.ed && ctx.ed.setNoteContent(this.note.id, content)) this.onBlockChange(); },
            onSubmit: () => this.closePop(true),
          });
          this.placePop(anchor);
          noteEd.focus('end');
        };
        if (this.$nextTick) this.$nextTick(mount); else mount();
        this.liveMessage = `الحاشية ${this.note.number || ''}`.trim();
        return true;
      },
      noteBold() { if (noteEd) noteEd.toggleBold(); },
      noteItalic() { if (noteEd) noteEd.toggleItalic(); },
      deleteNote() {
        if (!ctx.ed || this.pop.kind !== 'note') return false;
        const id = this.note.id;
        this.closePop();
        const ok = ctx.ed.deleteNote(id);
        if (ok) { this.onBlockChange(); this.undoToast('حُذفت الحاشية', () => this.undo(), true); }
        return ok;
      },

      // ------------------------------------------------------------ an uncertain word in the open paragraph
      openWord(w) {
        if (!w || !ctx.ed) return false;
        this.closePop();
        this.styleMenu = false;
        const item = typeof this.uncertainAt === 'function' ? this.uncertainAt(ctx.openId, null, w.start, w.end) : null;
        const readings = item && Array.isArray(item.readings) ? item.readings.map((r) => ({ value: r.text, label: r.label })) : [];
        this.word = { from: w.from, to: w.to, text: w.text, typed: w.text, readings, item };
        this.pop = { kind: 'word', style: this.pop.style, above: false };
        opened();
        this.placePop(w.dom);
        if (this.$nextTick) this.$nextTick(() => { this.placePop(w.dom); const f = this.$refs && this.$refs.wordTyped; if (f) { U.focus(f); if (f.select) f.select(); } });
        return true;
      },
      chooseReading(value) {
        if (!ctx.ed || this.pop.kind !== 'word') return false;
        const { from, to, text, item } = this.word;
        this.closePop(true);
        if (value === text) ctx.ed.acceptUncertain(from, to); else ctx.ed.replaceRange(from, to, value);
        if (item && typeof this.dropUncertain === 'function') this.dropUncertain(item);
        this.onBlockChange();
        this.liveMessage = value === text ? 'قُبلت الكلمة' : `صُحّحت إلى ${value}`;
        return true;
      },
      acceptWord() { return this.chooseReading(this.word.text); },
      submitTyped() {
        const typed = String(this.word.typed || '').trim();
        return typed ? this.chooseReading(typed) : false;
      },

      // ------------------------------------------------------------ the one overlay
      placePop(anchor) {
        const node = this.$refs && this.$refs.pop;
        const guess = POP_SIZE[this.pop.kind] || POP_SIZE.note;
        const size = [node && node.offsetWidth ? node.offsetWidth : guess[0], node && node.offsetHeight ? node.offsetHeight : guess[1]];
        const W = (typeof window !== 'undefined' && window.innerWidth) || 1200;
        const H = (typeof window !== 'undefined' && window.innerHeight) || 800;
        const stage = U.rect(this.$refs && this.$refs.stage);
        const view = { top: Math.max(stage.top || 0, 0) + POP_EDGE, bottom: Math.min(stage.bottom || H, H) - POP_EDGE, left: Math.max(stage.left || 0, 0) + POP_EDGE, right: Math.min(stage.right || W, W) - POP_EDGE };
        const placed = placeAgainst(U.rect(anchor), view, size, true);
        this.pop = Object.assign({}, this.pop, placed);
        return true;
      },
      closePop(refocus) {
        const kind = this.pop.kind;
        if (!kind) return null;
        if (kind === 'note' && noteEd) { noteEd.destroy(); noteEd = null; const host = this.$refs && this.$refs.noteHost; if (host) host.textContent = ''; }
        this.pop = { kind: null, style: this.pop.style, above: false };
        if (refocus && ctx.ed) ctx.ed.focus();
        return kind;
      },
      onPopOutside(e) {
        if (!this.pop.kind || justOpened) return false;
        if (U.closest(e && e.target, '.ed-fn, mark.ed-uncertain, .lp-run.is-sup, .k-note')) return false;
        this.closePop();
        return true;
      },
      noteEditor() { return noteEd; },
      blockEditor() { return ctx.ed; },
    };
  };
})();
