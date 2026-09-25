// The Phase 4 document ↔ the editor's JSON, and the pure helpers the chapter editor shares with its Alpine
// glue (PHASE5_SPEC §4, PHASE4_SPEC §2.8). No TipTap, no DOM: Node runs this file as it is (core/test_editor_ui.py).
//
//   toEditor(content)          the chapter's nodes (a `doc` or a list) → the editor's document JSON: blockquotes
//                              become quote paragraphs, `horizontalRule` a separator, heading levels fold to 1–2,
//                              empty text nodes and unknown nodes / marks go; every block keeps id, sourcePages,
//                              sourceLineIds, reviewed, suggestedRole; a title with no text takes its `text` attr
//   fromEditor(json)           the editor's document JSON → the nodes the server saves (null attrs dropped)
//   newId(prefix)              a fresh block (`e…`) or note (`ne…`) id, never one the server would give
//   fold / foldQuery / segmentMatches / findMatches
//                              find & replace matching, the same rules as editor/document.py: diacritics ignored
//                              unless `matchTashkeel`, alef forms folded when `foldAlef`, `wholeWord`
//   wordCount(nodes)           = editor.document.word_count
//   blockText(node)            a block's text without its notes (the chapters panel, the status line)
//   chapterListHtml(...)       the chapters panel rows (static markup, clicks delegated)
//   formatCount / pageRange    «2 184», «31–58» (Western digits; the template wraps them in <bdi>)
//   STYLES / styleOf           the style picker's entries and the style key of an editor block

export const STYLES = [
  { key: 'title', label: 'عنوان الكتاب', keys: '' },
  { key: 'heading1', label: 'عنوان فصل', keys: '⌘⌥1' },
  { key: 'heading2', label: 'عنوان فرعي', keys: '⌘⌥2' },
  { key: 'paragraph', label: 'فقرة', keys: '⌘⌥0' },
  { key: 'quote', label: 'اقتباس', keys: '⌘⌥3' },
  { key: 'verse', label: 'شعر', keys: '⌘⌥4' },
  { key: 'center', label: 'ملاحظة وسط', keys: '⌘⌥5' },
  { key: 'separator', label: 'فاصل', keys: '⌘⌥6' },
  { key: 'footnote', label: 'حاشية', keys: '⌘⇧F' },
];
export const STYLE_LABELS = Object.fromEntries(STYLES.map((s) => [s.key, s.label]));
export const MARKS = ['bold', 'italic', 'uncertain'];
export const PARAGRAPH_STYLES = ['quote', 'verse', 'center'];
const BLOCKS = new Set(['title', 'heading', 'paragraph', 'separator', 'horizontalRule', 'blockquote']);

// ---------------------------------------------------------------- small helpers
const isObj = (v) => Boolean(v) && typeof v === 'object' && !Array.isArray(v);
const attrsOf = (node) => (isObj(node) && isObj(node.attrs) ? node.attrs : {});
const intList = (v) => (Array.isArray(v) ? v.filter((n) => Number.isInteger(n)) : []);
const str = (v) => (typeof v === 'string' ? v : v == null ? '' : String(v));

export function contentOf(content) {
  if (Array.isArray(content)) return content;
  if (isObj(content) && Array.isArray(content.content)) return content.content;
  return [];
}

let idCounter = 0;
// `e` + a letter + base36 time + counter: never `e<digits>` (the server's own repair ids) and unique in a session.
export function newId(prefix) {
  idCounter += 1;
  return `${prefix || 'e'}k${Date.now().toString(36)}${idCounter.toString(36)}`;
}

function marksOf(node) {
  const seen = new Set();
  const out = [];
  (Array.isArray(node.marks) ? node.marks : []).forEach((m) => {
    const type = isObj(m) ? m.type : m;
    if (MARKS.includes(type) && !seen.has(type)) { seen.add(type); out.push({ type }); }
  });
  return out;
}

// ---------------------------------------------------------------- document → editor
function sourceAttrs(attrs) {
  return {
    id: typeof attrs.id === 'string' && attrs.id ? attrs.id : null,
    sourcePages: intList(attrs.sourcePages),
    sourceLineIds: intList(attrs.sourceLineIds),
    reviewed: attrs.reviewed !== false,
    suggestedRole: typeof attrs.suggestedRole === 'string' ? attrs.suggestedRole : null,
  };
}

function inlineToEditor(items, inNote) {
  const out = [];
  (Array.isArray(items) ? items : []).forEach((item) => {
    if (!isObj(item)) return;
    const kind = item.type;
    if (kind === 'text') {
      const text = str(item.text);
      if (!text) return;
      const node = { type: 'text', text };
      const marks = marksOf(item);
      if (marks.length) node.marks = marks;
      out.push(node);
    } else if (kind === 'hardBreak') {
      out.push({ type: 'hardBreak' });
    } else if (kind === 'pageBreak' && !inNote) {
      const a = attrsOf(item);
      out.push({ type: 'pageBreak', attrs: { page: Number.isInteger(a.page) ? a.page : null, printed: str(a.printed) } });
    } else if (kind === 'footnote' && !inNote) {
      const a = attrsOf(item);
      out.push({
        type: 'footnote',
        attrs: {
          id: typeof a.id === 'string' && a.id ? a.id : null,
          number: Number.isInteger(a.number) ? a.number : null,
          marker: str(a.marker),
          sourcePage: Number.isInteger(a.sourcePage) ? a.sourcePage : null,
          sourceLineIds: intList(a.sourceLineIds),
          orphan: a.orphan === true,
        },
        content: inlineToEditor(item.content, true),
      });
    }
  });
  return out;
}

function paragraphToEditor(node, style) {
  const a = attrsOf(node);
  const chosen = style || (PARAGRAPH_STYLES.includes(a.style) ? a.style : null);
  return { type: 'paragraph', attrs: { ...sourceAttrs(a), style: chosen }, content: inlineToEditor(node.content, false) };
}

function blockToEditor(node) {
  if (!isObj(node) || !BLOCKS.has(node.type)) return [];
  const a = attrsOf(node);
  switch (node.type) {
    case 'blockquote':
      return (Array.isArray(node.content) ? node.content : [])
        .filter((child) => isObj(child) && child.type === 'paragraph')
        .map((child) => paragraphToEditor(child, attrsOf(child).style === 'verse' ? 'verse' : 'quote'));
    case 'separator':
    case 'horizontalRule':
      return [{ type: 'separator', attrs: sourceAttrs(a) }];
    case 'heading': {
      const raw = a.level;
      const level = raw == null || raw === 1 || raw === '1' ? 1 : 2;
      return [{ type: 'heading', attrs: { ...sourceAttrs(a), level }, content: inlineToEditor(node.content, false) }];
    }
    case 'title': {
      let content = inlineToEditor(node.content, false);
      if (!content.length && str(a.text).trim()) content = [{ type: 'text', text: str(a.text).trim() }];
      return [{ type: 'title', attrs: { ...sourceAttrs(a), text: str(a.text), author: str(a.author) }, content }];
    }
    default:
      return [paragraphToEditor(node, null)];
  }
}

export function toEditor(content) {
  const out = [];
  contentOf(content).forEach((node) => out.push(...blockToEditor(node)));
  if (!out.length) out.push({ type: 'paragraph', attrs: { ...sourceAttrs({}), style: null }, content: [] });
  return { type: 'doc', content: out };
}

// ---------------------------------------------------------------- editor → document
function cleanAttrs(attrs) {
  const out = {};
  Object.entries(isObj(attrs) ? attrs : {}).forEach(([key, value]) => {
    if (value === null || value === undefined) return;
    out[key] = value;
  });
  return out;
}

function inlineFromEditor(items) {
  const out = [];
  (Array.isArray(items) ? items : []).forEach((item) => {
    if (!isObj(item)) return;
    if (item.type === 'text') {
      if (!str(item.text)) return;
      const node = { type: 'text', text: item.text };
      const marks = marksOf(item);
      if (marks.length) node.marks = marks;
      out.push(node);
      return;
    }
    const node = { type: item.type };
    const attrs = cleanAttrs(item.attrs);
    if (Object.keys(attrs).length) node.attrs = attrs;
    if (item.type === 'footnote') node.content = inlineFromEditor(item.content);
    out.push(node);
  });
  return out;
}

export function fromEditor(json) {
  return contentOf(json)
    .filter((node) => isObj(node) && typeof node.type === 'string')
    .map((node) => {
      const out = { type: node.type };
      const attrs = cleanAttrs(node.attrs);
      if (Object.keys(attrs).length) out.attrs = attrs;
      if (node.type !== 'separator') out.content = inlineFromEditor(node.content);
      return out;
    });
}

// ---------------------------------------------------------------- styles
export function styleOf(node) {
  if (!isObj(node)) return 'paragraph';
  if (node.type === 'heading') return attrsOf(node).level === 2 ? 'heading2' : 'heading1';
  if (node.type === 'title') return 'title';
  if (node.type === 'separator') return 'separator';
  if (node.type === 'footnote') return 'footnote';
  const style = attrsOf(node).style;
  return PARAGRAPH_STYLES.includes(style) ? style : 'paragraph';
}

// ---------------------------------------------------------------- text and counts
export function blockText(node) {
  const parts = [];
  const walk = (item) => {
    if (!isObj(item)) return;
    if (item.type === 'text') parts.push(str(item.text));
    else if (item.type === 'hardBreak') parts.push(' ');
    else if (item.type === 'footnote') return;
    (Array.isArray(item.content) ? item.content : []).forEach(walk);
  };
  walk(node);
  return parts.join('').replace(/\s+/g, ' ').trim();
}

// Every list of inline nodes: `[block, null, content]` per block, `[block, note, content]` per footnote in it.
export function* containers(nodes) {
  for (const node of contentOf(nodes)) {
    if (!isObj(node)) continue;
    const blocks = node.type === 'blockquote' ? (node.content || []).filter(isObj) : [node];
    for (const block of blocks) {
      if (!Array.isArray(block.content)) continue;
      yield [block, null, block.content];
      for (const item of block.content) {
        if (isObj(item) && item.type === 'footnote' && Array.isArray(item.content)) yield [block, item, item.content];
      }
    }
  }
}

export function wordCount(nodes) {
  let total = 0;
  for (const [, , content] of containers(nodes)) {
    const text = content.map((n) => (isObj(n) && n.type === 'text' ? str(n.text) : ' ')).join('');
    total += text.split(/\s+/).filter(Boolean).length;
  }
  return total;
}

export function formatCount(n) {
  const digits = String(Math.max(0, Math.round(Number(n) || 0)));
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
}

export function pageRange(pages) {
  if (!pages || !Number.isInteger(pages.first)) return '';
  return Number.isInteger(pages.last) && pages.last !== pages.first ? `${pages.first}–${pages.last}` : String(pages.first);
}

// «كلمة واحدة», «كلمتان», «5 كلمات», «214 كلمة» (= assembly.render.ar_count); the number in <bdi class="num">.
const WORD_FORMS = ['كلمة واحدة', 'كلمتان', 'كلمات', 'كلمة'];
export function wordsHtml(n) {
  n = Number(n) || 0;
  if (n === 1) return WORD_FORMS[0];
  if (n === 2) return WORD_FORMS[1];
  const units = n % 100;
  return `<bdi class="num">${formatCount(n)}</bdi> ${units >= 3 && units <= 10 ? WORD_FORMS[2] : WORD_FORMS[3]}`;
}

// The next find index when cycling: `dir` > 0 forward, < 0 backward, 0 stays (after a replacement the same
// index already points at the next match).
export function stepIndex(current, dir, length) {
  if (!length) return -1;
  const step = dir > 0 ? 1 : dir < 0 ? -1 : 0;
  const base = current < 0 ? (step < 0 ? 0 : -1) : current;
  return ((base + step) % length + length) % length;
}

// ---------------------------------------------------------------- find & replace (= editor/document.py)
// Diacritics, Quranic marks and tatweel: the characters core.arabic.strip_tashkeel removes.
const DROPPED = /[ؐ-ًؚ-ٰٟۖ-ۜ۟-۪ۤۧۨ-ۭـ]/;
const ALEF_FORMS = { 'أ': 'ا', 'إ': 'ا', 'آ': 'ا', 'ٱ': 'ا', 'ٲ': 'ا', 'ٳ': 'ا' };
const WORD_CHAR = /[\p{L}\p{N}]/u;
export const DEFAULT_FIND = { matchTashkeel: false, foldAlef: true, wholeWord: false };

export function fold(text, options) {
  const opts = { ...DEFAULT_FIND, ...(options || {}) };
  const out = [];
  const index = [];
  const chars = Array.from(str(text));
  let pos = 0;
  chars.forEach((raw) => {
    const at = pos;
    pos += raw.length;
    let char = raw;
    if (!opts.matchTashkeel && DROPPED.test(char)) return;
    if (opts.foldAlef && ALEF_FORMS[char]) char = ALEF_FORMS[char];
    const low = char.toLowerCase();
    const kept = low.length === char.length ? low : char;
    for (let i = 0; i < kept.length; i += 1) { out.push(kept[i]); index.push(at + i); }
  });
  return { text: out.join(''), index };
}

// The query as compared ('' when nothing is left to search for).
export function foldQuery(query, options) {
  const folded = fold(query, options).text;
  return folded.trim() ? folded : '';
}

// `[start, stop]` of the matches of the folded `query` in `text` (indexes into `text`).
export function segmentMatches(text, foldedQuery, options) {
  const opts = { ...DEFAULT_FIND, ...(options || {}) };
  if (!foldedQuery || !foldedQuery.trim()) return [];
  const folded = fold(text, opts);
  const hay = folded.text;
  if (!hay.includes(foldedQuery)) return [];
  const out = [];
  let pos = hay.indexOf(foldedQuery);
  while (pos >= 0) {
    const end = pos + foldedQuery.length;
    if (opts.wholeWord && ((pos > 0 && WORD_CHAR.test(hay[pos - 1])) || (end < hay.length && WORD_CHAR.test(hay[end])))) {
      pos = hay.indexOf(foldedQuery, pos + 1);
      continue;
    }
    const start = folded.index[pos];
    let stop = folded.index[end - 1] + 1;
    if (!opts.matchTashkeel) while (stop < text.length && DROPPED.test(text[stop])) stop += 1;
    out.push([start, stop]);
    pos = hay.indexOf(foldedQuery, end);
  }
  return out;
}

// Runs of consecutive text nodes: `{ offset, members }` (offset in the container text).
export function segments(content) {
  const runs = [];
  let offset = 0;
  let current = null;
  content.forEach((node, i) => {
    if (isObj(node) && node.type === 'text') {
      if (!current) { current = { offset, members: [] }; runs.push(current); }
      current.members.push(i);
      offset += str(node.text).length;
    } else current = null;
  });
  return runs;
}

// Every match in the chapter's text and notes, in document order: `{block, note, index, length}` (the same
// answer as the server's `find_in_nodes`; a match never spans a footnote, a page mark or a line break).
export function findMatches(nodes, query, options) {
  const folded = foldQuery(query, options);
  const out = [];
  if (!folded.trim()) return out;
  for (const [block, note, content] of containers(nodes)) {
    segments(content).forEach(({ offset, members }) => {
      const text = members.map((i) => str(content[i].text)).join('');
      segmentMatches(text, folded, options).forEach(([start, stop]) => {
        out.push({ block: str(attrsOf(block).id), note: note ? str(attrsOf(note).id) || null : null, index: offset + start, length: stop - start });
      });
    });
  }
  return out;
}

// ---------------------------------------------------------------- the chapters panel (static rows)
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const escapeHtml = (s) => str(s).replace(/[&<>"']/g, (c) => ESC[c]);

export function chapterListHtml(chapters, currentId) {
  const rows = (Array.isArray(chapters) ? chapters : []).map((c) => {
    const current = c.id === currentId;
    const pages = pageRange(c.pages);
    const meta = [wordsHtml(c.words)];
    if (pages) meta.push(`ص <bdi class="num">${escapeHtml(pages)}</bdi>`);
    const drift = c.drift ? '<span class="ed-ch-drift">تغيّر في المراجعة</span>' : '';
    const kind = c.kind === 'front' ? ' is-front' : c.kind === 'section' ? ' is-section' : '';
    return `<button type="button" class="ed-ch${current ? ' is-current' : ''}${kind}" data-cid="${escapeHtml(c.id)}"${current ? ' aria-current="true"' : ''} title="${escapeHtml(c.title)}">`
      + `<bdi class="ed-ch-n num">${escapeHtml(c.number)}</bdi>`
      + `<span class="ed-ch-body"><span class="ed-ch-title">${escapeHtml(c.title)}</span><span class="ed-ch-meta">${meta.join(' · ')}</span>${drift}</span>`
      + '</button>';
  });
  return rows.join('');
}
