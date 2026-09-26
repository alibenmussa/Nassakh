// The book page's pure geometry (PHASE5_SPEC §9.2, D47): the live pages drawn from the WeasyPrint layout, and
// every calculation the page makes on it. No DOM, no Alpine: Node runs this file as it is (core/test_layout_ui.py).
//
//   window.NassakhBook.geo.pageHtml(page, opts)     a laid-out page as positioned text: each line absolutely placed
//                                                  at its box (points, scaled by the page's width through
//                                                  container units), `width = w`, justified when the engine
//                                                  justified it, runs with their face, weight, italic and
//                                                  superscript footnote calls; the footnote rule, the running
//                                                  header and the page number anchored by their `align`
//   runMap / hitOffset / lineAt / caretLine         a point in a line's text ↔ the block's plain offset
//   flowAround(page, open)                          the lines of an open paragraph hidden, the lines below it
//   applyRelayout(pages, result, fresh, geometry)   a chapter's new pages spliced into the ones held: later pages
//                                                  renumbered by the delta, sides swapped on an odd delta
//   shiftPage(page, delta, geometry)                = publishing.layout.shift_page
//   around / shown                                  the pages worth fetching, the pages worth drawing
//   keyAction(event, ctx)                           the keyboard maps of both modes
//   groupUncertain, snippet, chapterRowsHtml, arCount  the side panel's lists
// Units: every layout number is in points from the page's top-left corner; the page element carries `--pw`
// (its width in points) and the CSS turns points into pixels with `100cqw / --pw`, so a page redraws at any
// size without a new layout. Offsets are plain-text offsets of a block (UTF-16, a footnote call one position).
(function () {
  'use strict';

  const root = typeof window !== 'undefined' ? window : globalThis;
  const NS = (root.NassakhBook = root.NassakhBook || {});

  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ESC[c]);
  const num = (v) => { const n = Math.round((Number(v) || 0) * 100) / 100; return String(n); };
  const FACES = { 'nk-body': 'f-b', 'nk-heading': 'f-h', 'nk-latin': 'f-l' };
  const CENTRED = new Set(['title', 'author', 'subtitle', 'credit', 'imprint', 'heading', 'verse', 'center', 'separator', 'contents-title', 'copyright']);
  const FRONT = /^(front-|toc-|copyright)/;
  const WS = /\s/;
  const LEADER_DOTS = '.'.repeat(240);

  // ---------------------------------------------------------------- Arabic counts (= assembly.render.ar_count)
  function arCount(n, forms) {
    n = Number(n) || 0;
    if (n === 1) return forms[0];
    if (n === 2) return forms[1];
    const units = n % 100;
    return `${n} ${units >= 3 && units <= 10 ? forms[2] : forms[3]}`;
  }

  // ---------------------------------------------------------------- run text ↔ plain offsets
  // The plain offset of every position of a run's text (length + 1 entries). The engine's text differs from the
  // block's plain text only by white space (collapsed, trimmed at line ends) and by the page marks it does not
  // print; without the plain text a position is the run's start plus the index, capped at its end.
  function runMap(text, plain, start, end) {
    const t = String(text || '');
    const out = new Array(t.length + 1);
    const s = Number(start) || 0;
    const e = Math.max(s, Number(end) || s);
    if (typeof plain !== 'string' || e <= s) {
      for (let k = 0; k <= t.length; k += 1) out[k] = Math.min(s + k, e);
      return out;
    }
    const skip = (c) => WS.test(c) || c === '\uFFFC' || c === '\u0000'; // white space, unprinted page marks
    let p = s;
    for (let k = 0; k < t.length; k += 1) {
      out[k] = Math.min(p, e);
      const ch = t[k];
      if (WS.test(ch)) {
        while (p < e && plain[p] !== undefined && skip(plain[p])) p += 1; // a collapsed run of spaces
        continue;
      }
      let q = p;
      while (q < e && plain[q] !== ch && skip(plain[q])) q += 1;
      p = q < e && plain[q] === ch ? q + 1 : Math.min(p + 1, e); // a character the source lacks (a hyphen) counts one
      out[k] = Math.min(q < e && plain[q] === ch ? q : out[k], e);
    }
    out[t.length] = e;
    return out;
  }

  // The plain offset of a click: run `r` of the line, character `k` of its text (a footnote call: after it).
  function hitOffset(line, r, k, plain) {
    const runs = (line && line.runs) || [];
    const run = runs[r];
    if (!run) return line ? Number(line.start) || 0 : 0;
    if (run.sup || run.leader || run.end <= run.start) return run.end > run.start ? run.end : Number(run.start) || 0;
    const map = runMap(run.text, plain, run.start, run.end);
    return map[Math.max(0, Math.min(Number(k) || 0, map.length - 1))];
  }

  const bodyKinds = (line) => line && line.kind !== 'note' && line.kind !== 'source';
  // The index of the line of `block` on `page` holding `offset` (a line's range includes its end: the caret at
  // the end of a line sits on it), -1 when none.
  function lineAt(page, block, offset) {
    const lines = (page && page.lines) || [];
    let best = -1;
    for (let i = 0; i < lines.length; i += 1) {
      const l = lines[i];
      if (l.block !== block || l.kind === 'source') continue;
      if (offset >= l.start && offset < l.end) return i;
      if (offset >= l.start && offset <= l.end) best = i;
      else if (best < 0 && offset < l.start) return i; // before its first line here: the block's top
    }
    return best;
  }

  // The page and line of `block` at `offset` among the pages held (a Map n → page): `{n, i}` or null.
  function caretLine(pages, block, offset) {
    let fallback = null;
    const ordered = [...pages.keys()].sort((a, b) => a - b);
    for (const n of ordered) {
      const page = pages.get(n);
      const lines = (page && page.lines) || [];
      for (let i = 0; i < lines.length; i += 1) {
        const l = lines[i];
        if (l.block !== block || l.kind === 'source') continue;
        if (offset >= l.start && offset < l.end) return { n, i };
        if (offset >= l.start && offset <= l.end) fallback = { n, i };
        else if (!fallback && offset < l.start) fallback = { n, i };
      }
    }
    return fallback;
  }

  // The first line of every block on the page, in reading order (the body first, then the notes).
  function blocksOn(page) {
    const seen = [];
    ((page && page.lines) || []).forEach((l) => { if (l.block && !seen.includes(l.block)) seen.push(l.block); });
    return seen;
  }

  // Where the body ends: the footnote rule, else the bottom margin.
  function bodyBottom(page) {
    if (!page) return 0;
    if (page.footnote_rule) return Number(page.footnote_rule.y) || 0;
    const m = page.margins || {};
    return (Number(page.height_pt) || 0) - (Number(m.bottom) || 0);
  }

  // The text area of a page (the measure the engine set the body in): `{x, w}` in points.
  function measure(page) {
    const m = (page && page.margins) || {};
    const x = Number(m.left) || 0;
    return { x, w: Math.max(0, (Number(page && page.width_pt) || 0) - x - (Number(m.right) || 0)) };
  }

  // ---------------------------------------------------------------- decorations (find, uncertain, a picked word)
  // A run's text cut at the decorations of its block (`[{start, end, cls}]` in plain offsets) and with the
  // markers inserted at their offsets (`[{offset, html}]`, empty inline elements: no width in the line):
  // `[{text, cls, before}]`.
  function runPieces(run, plain, decos, inserts) {
    const text = String(run.text || '');
    const ranged = run.end > run.start && !run.sup && !run.leader;
    const withDecos = Boolean(ranged && decos && decos.length);
    const own = ranged ? (inserts || []).filter((m) => m.offset >= run.start && m.offset < run.end) : [];
    if (!withDecos && !own.length) return [{ text, cls: '', before: '' }];
    const map = runMap(text, plain, run.start, run.end);
    const at = new Map();
    own.forEach((m) => {
      let k = map.findIndex((v) => v >= m.offset);
      if (k < 0) k = text.length;
      at.set(k, (at.get(k) || '') + m.html);
    });
    const out = [];
    for (let k = 0; k <= text.length; k += 1) {
      const before = at.get(k) || '';
      if (k === text.length) { if (before) out.push({ text: '', cls: '', before }); break; }
      const where = map[k];
      const cls = withDecos ? [...new Set(decos.filter((d) => where >= d.start && where < d.end).map((d) => d.cls))].join(' ') : '';
      const last = out[out.length - 1];
      if (last && last.cls === cls && !before) last.text += text[k];
      else out.push({ text: text[k], cls, before });
    }
    return out.length ? out : [{ text, cls: '', before: '' }];
  }

  // ---------------------------------------------------------------- one page as markup
  function runHtml(run, r, plain, decos, inserts) {
    const face = FACES[run.font] || '';
    const cls = ['lp-run'];
    if (face) cls.push(face);
    if (Number(run.weight) >= 600) cls.push('is-b');
    if (run.italic) cls.push('is-i');
    if (run.sup) cls.push('is-sup');
    const style = [`--fs:${num(run.size_pt)}`];
    if (!face && run.font) style.push(`font-family:"${String(run.font).replace(/["\\<>]/g, '')}",serif`);
    if (run.leader) {
      return `<span class="lp-leader${face ? ` ${face}` : ''}" data-r="${r}" style="--fs:${num(run.size_pt)};--lw:${num(run.w)}" aria-hidden="true">${LEADER_DOTS}</span>`;
    }
    const note = run.note ? ` data-note="${esc(run.note)}"` : '';
    const range = ` data-s="${Number(run.start) || 0}" data-e="${Number(run.end) || 0}"`;
    const inner = runPieces(run, plain, decos, inserts).map((p) => p.before + (p.cls ? `<mark class="${esc(p.cls)}">${esc(p.text)}</mark>` : esc(p.text))).join('');
    return `<span class="${cls.join(' ')}" data-r="${r}"${range}${note} style="${style.join(';')}">${inner}</span>`;
  }

  // The class list of a line: its kind (`k-body`, `k-note`…), justified (`is-j`) or centred (`is-c`).
  function lineClass(line) {
    const cls = ['lp-line', `k-${String(line.kind || 'other').replace(/[^\w-]/g, '')}`];
    if (line.justify) cls.push('is-j');
    else if (CENTRED.has(line.kind)) cls.push('is-c');
    if (line.target) cls.push('is-link');
    if (FRONT.test(String(line.block || ''))) cls.push('is-front');
    return cls.join(' ');
  }

  // The first run with text sets the line's own face and size (the strut the engine measured the line with).
  const leadRun = (line) => (line.runs || []).find((r) => !r.sup && !r.leader && String(r.text || '').trim()) || (line.runs || [])[0] || {};

  function lineHtml(line, i, o) {
    const lead = leadRun(line);
    const face = FACES[lead.font] || 'f-b';
    const cls = [lineClass(line), face];
    if (o.hidden && o.hidden.has(line.block)) cls.push('is-hidden');
    if (o.below && o.below(line, i)) cls.push('is-below');
    if (o.selected && line.block === o.selected) cls.push('is-selected');
    if (o.flash && line.block === o.flash.block && (o.flash.i === undefined || o.flash.i === i)) cls.push('is-flash');
    const style = `--x:${num(line.x)};--y:${num(line.y)};--w:${num(line.w)};--h:${num(line.h)};--fs:${num(lead.size_pt || 13)}`;
    const plain = o.plain ? o.plain(line.block) : undefined;
    const decos = o.decos ? o.decos(line.block) : null;
    const own = decos && decos.length ? decos.filter((d) => d.end > line.start && d.start < line.end) : null;
    const inserts = o.marks ? (o.marks(line.block) || []).filter((m) => m.offset >= line.start && m.offset < line.end).map((m) => ({ offset: m.offset, html: `<span class="lp-pb" data-page="${esc(m.page)}"></span>` })) : null;
    const runs = (line.runs || []).map((run, r) => runHtml(run, r, plain, own, inserts)).join('');
    const target = line.target ? ` data-target="${esc(line.target)}"` : '';
    // a scan page mark on this line: its «ص N» label sits in the margin beside the line, like the original page
    // numbers of a scholarly edition (above the mark it would land on the previous line's letters)
    const pb = inserts && inserts.length ? ` data-pb="${esc(inserts.map((m) => m.html.match(/data-page="([^"]*)"/)[1]).join('، '))}"` : '';
    if (pb) cls.push('has-pb');
    return `<div class="${cls.join(' ')}" data-i="${i}" data-b="${esc(line.block || '')}"${target}${pb} dir="${line.dir === 'ltr' ? 'ltr' : 'rtl'}" style="${style}">${runs}</div>`;
  }

  // A header or a page number: its box, the text kept at the side it is anchored to (`align`).
  function furnitureHtml(item, cls) {
    if (!item || !item.text) return '';
    const face = FACES[item.font] || 'f-b';
    const align = ['left', 'right', 'center'].includes(item.align) ? item.align : 'center';
    const style = `--x:${num(item.x)};--y:${num(item.y)};--w:${num(item.w)};--h:${num(item.h)};--fs:${num(item.size_pt || 10)}`;
    const weight = Number(item.weight) >= 600 ? ' is-b' : '';
    const italic = item.italic ? ' is-i' : '';
    return `<div class="${cls} ${face}${weight}${italic}" data-align="${align}" style="${style}"><span>${esc(item.text)}</span></div>`;
  }

  // The whole page. opts: {plain(block) → text, decos(block) → [{start, end, cls}], marks(block) → [{offset, page}],
  // hidden: Set of blocks not drawn (an open paragraph, its continuation), below(line, i) → true for the lines
  // that move with an open paragraph's growth, selected: a block outlined, flash: {block, i} a line pointed at}.
  function pageHtml(page, opts) {
    if (!page) return '';
    const o = opts || {};
    const out = [];
    (page.lines || []).forEach((line, i) => out.push(lineHtml(line, i, o)));
    const rule = page.footnote_rule;
    if (rule) out.push(`<div class="lp-rule" style="--x:${num(rule.x)};--y:${num(rule.y)};--w:${num(rule.w)}"></div>`);
    out.push(furnitureHtml(page.header, 'lp-header'));
    out.push(furnitureHtml(page.number, 'lp-number'));
    return out.join('');
  }

  // The style of the page element: its size in points (the sheet keeps the aspect).
  const pageStyle = (page) => `--pw:${num(page && page.width_pt)};--ph:${num(page && page.height_pt)}`;

  // ---------------------------------------------------------------- an open paragraph on its page
  // The lines an open paragraph `block` covers on `page` and the ones that move with it: `{lines, top, bottom,
  // below(line, i), belowFrom}` — `top` / `bottom` its extent here (points); every body line starting at or
  // under its last line's bottom moves by the paragraph's growth (the notes and the page furniture stay).
  function flowAround(page, block) {
    const lines = ((page && page.lines) || []).map((l, i) => ({ l, i })).filter(({ l }) => l.block === block && bodyKinds(l));
    if (!lines.length) return { lines: [], top: null, bottom: null, below: () => false, belowFrom: null };
    const top = Math.min(...lines.map(({ l }) => l.y));
    const bottom = Math.max(...lines.map(({ l }) => l.y + l.h));
    const last = Math.max(...lines.map(({ i }) => i));
    const below = (line, i) => bodyKinds(line) && line.block !== block && i > last && line.y >= bottom - 0.5;
    return { lines: lines.map(({ i }) => i), top, bottom, below, belowFrom: bottom };
  }

  // A block without lines yet (a new paragraph): it opens under the block before it on the page (or at the
  // page's top), the lines after that block moving down.
  function flowAfter(page, before) {
    const f = before ? flowAround(page, before) : null;
    if (f && f.bottom !== null) return { top: f.bottom, below: (line, i) => bodyKinds(line) && line.block !== before && i > Math.max(...f.lines) && line.y >= f.bottom - 0.5, belowFrom: f.bottom };
    const m = (page && page.margins) || {};
    const top = Number(m.top) || 0;
    return { top, below: (line) => bodyKinds(line), belowFrom: top };
  }

  // ---------------------------------------------------------------- page numbers and sides (= publishing.layout)
  const moved = (item, dx) => (item ? Object.assign({}, item, { x: Math.round((Number(item.x) + dx) * 100) / 100 }) : item);

  // A copy of `page` numbered `n + delta`; on an odd delta it swaps sides: the lines, the header and the footnote
  // rule move by `side_shift_pt` (right → left; the opposite move is the negative), an outer page number goes
  // to the other corner, a centred one moves with the text.
  function shiftPage(page, delta, geometry) {
    if (!delta || !page) return page;
    const g = geometry || {};
    const out = Object.assign({}, page, { n: page.n + delta });
    let number = page.number ? Object.assign({}, page.number) : null;
    if (number && /^\d+$/.test(String(number.text || ''))) number.text = String(page.n + delta);
    if (delta % 2) {
      const right = page.side === 'right';
      const shift = Number(g.side_shift_pt) || 0;
      const dx = right ? shift : -shift;
      out.side = right ? 'left' : 'right';
      out.lines = (page.lines || []).map((line) => Object.assign({}, line, { x: Math.round((line.x + dx) * 100) / 100 }));
      out.header = moved(page.header, dx);
      out.footnote_rule = moved(page.footnote_rule, dx);
      const m = page.margins || {};
      out.margins = Object.assign({}, m, { left: m.right, right: m.left });
      if (number) {
        if (g.page_number === 'bottom_outer' || g.page_number === 'top_outer') {
          number = Object.assign(number, {
            x: Math.round(((Number(page.width_pt) || 0) - number.x - number.w) * 100) / 100,
            align: { left: 'right', right: 'left' }[number.align] || number.align,
          });
        } else number = moved(number, dx);
      }
    }
    out.number = number;
    return out;
  }

  // A re-layout's pages spliced into the pages held (a Map n → page, with `pageCount`, `revision`):
  //   `{refetch: true}` when the result is not a splice of the held revision (a whole-book layout, another
  //   revision) — the caller fetches the visible range again; else `{pages, pageCount, revision, moved(n) →
  //   the old page n's new number (null: replaced), from, to, count, delta}`. Pages before `from` stay, `from`…
  //   `to` are replaced by the fresh ones, every later page moves by `delta` (sides swap on an odd delta).
  //   Contents pages are dropped (their page numbers may have moved): they are fetched again when shown.
  function applyRelayout(held, result, fresh, geometry) {
    const r = result || {};
    if (r.unchanged) return { unchanged: true };
    const before = r.revision ? r.revision.before : null;
    if (r.full || before === null || before === undefined || before !== held.revision) return { refetch: true };
    const from = Number(r.from);
    const to = Number(r.to);
    const delta = Number(r.delta) || 0;
    const shiftedFrom = r.shifted_from === null || r.shifted_from === undefined ? to + 1 : Number(r.shifted_from);
    const geo = Object.assign({}, geometry || {}, r.side_shift_pt !== undefined ? { side_shift_pt: r.side_shift_pt } : {});
    const pages = new Map();
    held.pages.forEach((page, n) => {
      if (n < from) pages.set(n, page);
      else if (n >= shiftedFrom) pages.set(n + delta, shiftPage(page, delta, geo));
    });
    (fresh || []).forEach((page) => { if (page && Number.isFinite(page.n)) pages.set(page.n, page); });
    if (delta) pages.forEach((page, n) => { if ((page.lines || []).some((l) => l.kind === 'contents')) pages.delete(n); });
    const movedN = (n) => (n < from ? n : n >= shiftedFrom ? n + delta : null);
    return {
      pages,
      pageCount: Number(r.page_count) || held.pageCount,
      revision: r.revision ? r.revision.after : held.revision,
      chapters: Array.isArray(r.chapters) ? r.chapters : null,
      checks: Array.isArray(r.checks) ? r.checks : null,
      moved: movedN,
      from,
      to,
      count: Number(r.count) || (fresh || []).length,
      delta,
      flip: Boolean(r.flip),
    };
  }

  // ---------------------------------------------------------------- which pages
  // The page numbers worth having around page `n` (fetched ahead so a turn lands on a drawn page).
  function around(n, first, last, spread) {
    const back = spread ? 3 : 2;
    const ahead = spread ? 7 : 5;
    const lo = Math.max(first, n - back);
    const hi = Math.min(last, n + ahead);
    return lo <= hi ? [lo, hi] : null;
  }

  // Virtualisation: the pages in the DOM are the ones shown (one, or the two of a spread); everything else is
  // data. `numbers` of the sequence, `cursor` its index. Returns `{right, left}` page numbers (0 = none).
  function shown(numbers, cursor, spread) {
    const n = numbers[cursor];
    if (!n) return { right: 0, left: 0 };
    if (!spread) return { right: n, left: 0 };
    // an Arabic spread: the even (verso) page on the right, the odd (recto) on the left
    if (n % 2 === 0) return { right: n, left: numbers[cursor + 1] === n + 1 ? n + 1 : 0 };
    return { right: numbers[cursor - 1] === n - 1 ? n - 1 : 0, left: n };
  }

  // ---------------------------------------------------------------- keyboard (PHASE5_SPEC §9.2, PHASE7_SPEC §3.15)
  // ⌘⌥ + a digit key sets a block style; by code, so the digit row counts whatever it types.
  const STYLE_KEYS = { Digit1: 'heading1', Digit2: 'heading2', Digit0: 'paragraph', Digit3: 'quote', Digit4: 'verse', Digit5: 'center', Digit6: 'separator' };
  const FIT_ORDER = ['height', 'width', 'actual'];
  // The fit a step leads to (D69): + from the height to the width, then 100 %; − back; 0 is the height.
  function stepFit(fit, dir) {
    const i = Math.max(0, FIT_ORDER.indexOf(fit));
    return FIT_ORDER[Math.min(FIT_ORDER.length - 1, Math.max(0, i + dir))];
  }
  // ctx: {mode: 'preview'|'edit', inField (an input, a select, the note editor), inBlock (the open paragraph)}.
  // Returns the action; nothing fires inside a field except Esc (blur) and ⌘S / ⌘F. Keys go through
  // `NassakhKeys` (keys.js, D69): letters by their place (the Arabic layout's «ث» on E is E), digits in any
  // script, nothing while an IME composes. V is the spread, S the scan page marks («فواصل الصفحات الأصلية»),
  // + − 0 the fits.
  function keyAction(e, ctx) {
    const K = root.NassakhKeys;
    if (K.composing(e)) return null;
    const k = e.key || '';
    const code = e.code || '';
    const c = ctx || {};
    if (k === 'Escape') return c.inField ? 'blur' : 'escape';
    if (K.mod(e) && e.altKey) return c.mode === 'edit' && !c.inField ? STYLE_KEYS[code] || null : null;
    if (K.mod(e)) {
      const letter = K.letter(e, 'mod shift');
      if (letter === 's') return 'save';
      if (letter === 'f' && e.shiftKey) return c.mode === 'edit' && !c.inField ? 'footnote' : null;
      if (letter === 'f') return 'find';
      if (c.inField) return null;
      if (c.mode !== 'edit') return null;
      if (code === 'BracketLeft' || (!code && k === '[')) return 'prevChapter';
      if (code === 'BracketRight' || (!code && k === ']')) return 'nextChapter';
      if (letter === 'z' && e.shiftKey) return 'redo';
      if (letter === 'z') return 'undo';
      if (letter === 'y') return 'redo';
      if (letter === 'b') return 'bold';
      if (letter === 'i') return 'italic';
      return null;
    }
    if (c.inField || c.inBlock || e.altKey) return null;
    if (K.is(e, '?')) return 'sheet';
    const letter = K.letter(e);
    if (letter === 'e') return 'mode';
    if (k === 'ArrowLeft' || k === 'PageDown') return 'next'; // RTL: the next page is on the left
    if (k === 'ArrowRight' || k === 'PageUp') return 'prev';
    if (k === 'Home') return 'first';
    if (k === 'End') return 'last';
    if (letter === 's') return 'marks';
    if (c.mode === 'edit') {
      if (letter === 'o') return 'source';
      if (k === 'Delete' || k === 'Backspace') return 'deleteSelected';
      return null;
    }
    if (letter === 'g') return 'jump';
    if (letter === 'v') return 'spread';
    if (K.plus(e)) return 'fitIn';
    if (K.minus(e)) return 'fitOut';
    if (K.digit(e) === 0) return 'fitHeight';
    return null;
  }

  // ---------------------------------------------------------------- the side panel's lists
  // «… قد حطم كيانها وشنت أهلها …»: the words around a match, cut at word edges.
  function snippet(text, start, end, room = 34) {
    const t = String(text || '').replace(/[\uFFFC\u0000]/g, ' ').replace(/\n/g, ' ');
    let a = Math.max(0, start - room);
    let b = Math.min(t.length, end + room);
    if (a > 0) { const sp = t.indexOf(' ', a); if (sp >= 0 && sp < start) a = sp + 1; }
    if (b < t.length) { const sp = t.lastIndexOf(' ', b); if (sp > end) b = sp; }
    return { before: (a > 0 ? '… ' : '') + t.slice(a, start), match: t.slice(start, end), after: t.slice(end, b) + (b < t.length ? ' …' : '') };
  }

  // The uncertain words grouped by chapter, then by page: `[{chapter, title, count, pages: [{page, items}]}]`
  // (a word with no page yet sits in a group of its own, `page: null`).
  function groupUncertain(items, titles) {
    const out = [];
    const byChapter = new Map();
    (items || []).forEach((item, index) => {
      const key = item.chapter || '';
      let g = byChapter.get(key);
      if (!g) { g = { chapter: key, title: (titles && titles[key]) || '', count: 0, pages: [], _pages: new Map() }; byChapter.set(key, g); out.push(g); }
      g.count += 1;
      const pk = item.page === null || item.page === undefined ? 'none' : String(item.page);
      let p = g._pages.get(pk);
      if (!p) { p = { page: item.page === undefined ? null : item.page, items: [] }; g._pages.set(pk, p); g.pages.push(p); }
      p.items.push(Object.assign({ index }, item));
    });
    out.forEach((g) => { delete g._pages; g.pages.sort((a, b) => (a.page === null) - (b.page === null) || (a.page || 0) - (b.page || 0)); });
    return out;
  }

  // The chapters list as static rows (800 chapters stay cheap): the title, the pages, the drift badge.
  // rows: [{id, number, kind, title, first, last, pages, delta, drift, current, checks}].
  function chapterRowsHtml(rows) {
    return (rows || []).map((c) => {
      const pages = c.first !== null && c.first !== undefined ? `<span class="bp-ch-range">ص <bdi>${esc(c.first === c.last ? c.first : `${c.first}–${c.last}`)}</bdi></span>` : '<span class="bp-ch-range is-none">—</span>';
      const count = c.pages ? `<bdi class="bp-ch-count">${esc(c.pages)}</bdi>` : '';
      const delta = c.delta ? `<bdi class="lo-delta">${c.delta > 0 ? '+' : '−'}${Math.abs(c.delta)}</bdi>` : '';
      const drift = c.drift ? '<span class="bp-ch-drift">تغيّر في المراجعة</span>' : '';
      const kind = c.kind === 'front' ? ' is-front' : c.kind === 'section' ? ' is-section' : '';
      return `<button type="button" class="bp-ch${c.current ? ' is-current' : ''}${kind}" data-cid="${esc(c.id)}"${c.current ? ' aria-current="true"' : ''} title="${esc(c.title)}">`
        + `<span class="bp-ch-title">${esc(c.title)}</span>`
        + `<span class="bp-ch-meta num">${count}${delta}${pages}</span>${drift}</button>`;
    }).join('');
  }

  NS.geo = {
    esc, num, arCount, runMap, hitOffset, lineAt, caretLine, blocksOn, bodyBottom, measure, runPieces, pageHtml, pageStyle,
    lineHtml, flowAround, flowAfter, shiftPage, applyRelayout, around, shown, keyAction, stepFit, snippet, groupUncertain,
    chapterRowsHtml, STYLE_KEYS,
  };
})();
