// The Phase 4 document ↔ the editor's JSON, and the pure helpers the book page shares with its Alpine glue
// (PHASE5_SPEC §9.2, PHASE4_SPEC §2.8). No TipTap, no DOM: Node runs this file as it is (core/test_layout_ui.py).
//
//   toEditor(content)          the chapter's nodes (a `doc` or a list) → the editor's document JSON: blockquotes
//                              become quote paragraphs, `horizontalRule` a separator, heading levels fold to 1–2,
//                              empty text nodes and unknown nodes / marks go; every block keeps id, sourcePages,
//                              sourceLineIds, reviewed, suggestedRole and the D47 flags breakBefore / keepWithNext;
//                              a title with no text takes its `text` attr
//   fromEditor(json)           the editor's document JSON → the nodes the server saves (null attrs dropped)
//   newId(prefix)              a fresh block (`e…`) or note (`ne…`) id, never one the server would give
//   fold / foldQuery / segmentMatches / findMatches
//                              find & replace matching, the same rules as editor/document.py: diacritics ignored
//                              unless `matchTashkeel`, alef forms folded when `foldAlef`, `wholeWord`
//   wordCount(nodes)           = editor.document.word_count
//   blockText(node)            a block's text without its notes
//   formatCount / pageRange    «2 184», «31–58» (Western digits; the template wraps them in <bdi>)
//   STYLES / styleOf           the style picker's entries and the style key of a block
//
// D47, the live pages: a block's **plain text** is `editor.document.inline_text` (text as it is, a hard break
// "\n", a footnote call or a scan page mark one U+FFFC) and every offset below is into it, in UTF-16 units —
// the unit of the page layout's `start` / `end` and of the uncertain words:
//   plainText / inlineText     the plain text of a block (or of a list of inline nodes)
//   sliceContent / splitNode / mergeNodes / insertBlocks
//                              the one-block editor's boundaries: Enter splits, Backspace at the start merges,
//                              a paste of several paragraphs becomes several blocks
//   locate / flatBlocks / replaceBlock / setBlockAttrs / noteOwner / findNote
//                              the chapter's nodes, addressed by block id (a blockquote's paragraphs too), edited
//                              without mutation (the per-chapter undo keeps every version it was given)
//   findPlain / replacePlain / unmarkPlain / replaceInBlock
//                              find & replace and the uncertain words by plain offsets
//   pageMarks / noteIds / nodeHtml
//                              scan page marks and notes of a block, a block's static markup (a block edited
//                              and not laid out again yet is drawn by the browser, in the page's faces)

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
    // D47 page-break flags: kept as they are (true / false), absent stays absent (null is dropped on save)
    breakBefore: typeof attrs.breakBefore === 'boolean' ? attrs.breakBefore : null,
    keepWithNext: typeof attrs.keepWithNext === 'boolean' ? attrs.keepWithNext : null,
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

// ---------------------------------------------------------------- markup
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const escapeHtml = (s) => str(s).replace(/[&<>"']/g, (c) => ESC[c]);

// ================================================================ D47: plain text, offsets and block edits
export const OBJECT = '￼'; // a footnote call or a scan page mark (= editor.document.OBJECT)
export const BREAK = '\n';

const itemLength = (item) => (isObj(item) && item.type === 'text' ? str(item.text).length : isObj(item) && (item.type === 'hardBreak' || item.type === 'footnote' || item.type === 'pageBreak') ? 1 : 0);
const marksKey = (item) => JSON.stringify(isObj(item) && Array.isArray(item.marks) ? item.marks.map((m) => (isObj(m) ? m.type : m)).sort() : []);
const withText = (item, text) => {
  const out = { type: 'text', text };
  if (Array.isArray(item.marks) && item.marks.length) out.marks = item.marks.map((m) => (isObj(m) ? { ...m } : { type: m }));
  return out;
};

// = editor.document.inline_text
export function inlineText(content) {
  let out = '';
  (Array.isArray(content) ? content : []).forEach((item) => {
    if (!isObj(item)) return;
    if (item.type === 'text') out += str(item.text);
    else if (item.type === 'hardBreak') out += BREAK;
    else if (item.type === 'footnote' || item.type === 'pageBreak') out += OBJECT;
  });
  return out;
}
export const plainText = (node) => inlineText(isObj(node) ? node.content : null);

// Empty text nodes dropped, neighbours with the same marks joined (the server's own normal form).
export function normalizeContent(content) {
  const out = [];
  (Array.isArray(content) ? content : []).forEach((item) => {
    if (!isObj(item)) return;
    if (item.type === 'text') {
      if (!str(item.text)) return;
      const last = out[out.length - 1];
      if (last && last.type === 'text' && marksKey(last) === marksKey(item)) { out[out.length - 1] = withText(last, str(last.text) + str(item.text)); return; }
    }
    out.push(item);
  });
  return out;
}

// The inline nodes between two plain offsets (a text node cut at the edges keeps its marks; an object is taken
// when its one position lies inside).
export function sliceContent(content, start, end) {
  const out = [];
  let pos = 0;
  (Array.isArray(content) ? content : []).forEach((item) => {
    const len = itemLength(item);
    if (!len) return;
    const a = pos;
    const b = pos + len;
    pos = b;
    if (b <= start || a >= end) return;
    if (item.type === 'text') out.push(withText(item, str(item.text).slice(Math.max(0, start - a), Math.min(len, end - a))));
    else out.push(item);
  });
  return normalizeContent(out);
}

const FLAGS = ['breakBefore', 'keepWithNext'];
// A copy of a block's attrs for the block that follows it (a split, a new paragraph): the source attrs stay,
// the id is new, the page break stays with the first half and «مع التالية» moves to the second.
function followingAttrs(node, type) {
  const a = { ...attrsOf(node) };
  delete a.breakBefore;
  delete a.level;
  delete a.text;
  delete a.author;
  a.id = newId('e');
  if (type === 'paragraph' && node.type !== 'paragraph') delete a.style;
  return a;
}

// Enter at `offset`: `[before, after]`. A heading (or the book title) split at its end is followed by a plain
// paragraph; split inside, both halves keep the block's kind.
export function splitNode(node, offset) {
  const text = plainText(node);
  const at = Math.max(0, Math.min(Number(offset) || 0, text.length));
  const content = isObj(node) && Array.isArray(node.content) ? node.content : [];
  const beforeAttrs = { ...attrsOf(node) };
  const keep = beforeAttrs.keepWithNext;
  delete beforeAttrs.keepWithNext;
  const before = { ...node, attrs: beforeAttrs, content: sliceContent(content, 0, at) };
  const heading = node.type === 'heading' || node.type === 'title';
  const type = heading && at >= text.length ? 'paragraph' : node.type === 'title' ? 'paragraph' : node.type;
  const afterAttrs = followingAttrs(node, type);
  if (type === 'heading') afterAttrs.level = attrsOf(node).level;
  if (typeof keep === 'boolean') afterAttrs.keepWithNext = keep;
  const after = { type, attrs: afterAttrs, content: sliceContent(content, at, text.length) };
  return [before, after];
}

// The union of two blocks' source marks (numbers, sorted, each once); an attribute neither block has stays absent.
function unionMarks(x, y) {
  const values = [x, y].filter(Array.isArray);
  if (!values.length) return undefined;
  const seen = new Set();
  values.flat().forEach((v) => { const n = Number(v); if (Number.isFinite(n)) seen.add(n); });
  return [...seen].sort((p, q) => p - q);
}

// Backspace at the start of `b`: `{node, offset}` — `a` with `b`'s text after it and the caret at the seam;
// null when `a` has no text to join (a separator: the caller removes it instead). The merged block keeps both
// blocks' source marks (D70): `sourcePages` and `sourceLineIds` are the union of the two (sorted, unique), and
// it is `reviewed` only when both were, so «الأصل» still shows both pages and the page-by-page merge (D78)
// finds every line.
export function mergeNodes(a, b) {
  if (!isObj(a) || !isObj(b) || a.type === 'separator') return null;
  const offset = plainText(a).length;
  const attrs = { ...attrsOf(a) };
  const other = attrsOf(b);
  if (other.keepWithNext === true) attrs.keepWithNext = true;
  ['sourcePages', 'sourceLineIds'].forEach((key) => {
    const marks = unionMarks(attrs[key], other[key]);
    if (marks !== undefined) attrs[key] = marks;
  });
  if (attrs.reviewed === false || other.reviewed === false) attrs.reviewed = false;
  const node = { ...a, attrs, content: normalizeContent([...(a.content || []), ...(b.content || [])]) };
  return { node, offset };
}

// A paste of several paragraphs between `from` and `to` of `node`: the first pasted paragraph joins the text
// before the selection, the last one the text after it. `{blocks, caret: {index, offset}}` (the caret after
// the pasted text).
export function insertBlocks(node, from, to, pasted) {
  const text = plainText(node);
  const a = Math.max(0, Math.min(Number(from) || 0, text.length));
  const b = Math.max(a, Math.min(Number(to) || 0, text.length));
  const content = Array.isArray(node.content) ? node.content : [];
  const head = sliceContent(content, 0, a);
  const tail = sliceContent(content, b, text.length);
  const items = (Array.isArray(pasted) ? pasted : []).filter(isObj);
  if (!items.length) return { blocks: [{ ...node, content: normalizeContent([...head, ...tail]) }], caret: { index: 0, offset: a } };
  const blocks = items.map((p, i) => {
    if (i === 0) return { ...node, content: normalizeContent([...head, ...(p.content || [])]) };
    const type = p.type === 'heading' ? 'heading' : 'paragraph';
    const attrs = followingAttrs(node, type);
    if (type === 'heading') attrs.level = attrsOf(p).level === 2 ? 2 : 1;
    const style = attrsOf(p).style;
    if (type === 'paragraph') { if (PARAGRAPH_STYLES.includes(style)) attrs.style = style; else delete attrs.style; }
    return { type, attrs, content: normalizeContent(p.content || []) };
  });
  const last = blocks.length - 1;
  const offset = plainText(blocks[last]).length;
  blocks[last] = { ...blocks[last], content: normalizeContent([...(blocks[last].content || []), ...tail]) };
  return { blocks, caret: { index: last, offset } };
}

// ---------------------------------------------------------------- the chapter's nodes, by block id
const idOf = (node) => (isObj(node) && isObj(node.attrs) && typeof node.attrs.id === 'string' ? node.attrs.id : '');

// `{index, child, node}` of the block `id` (`child` ≥ 0 for a paragraph inside a blockquote), else null.
export function locate(nodes, id) {
  const list = contentOf(nodes);
  for (let i = 0; i < list.length; i += 1) {
    const n = list[i];
    if (!isObj(n)) continue;
    if (n.type === 'blockquote' && Array.isArray(n.content)) {
      for (let j = 0; j < n.content.length; j += 1) if (idOf(n.content[j]) === id) return { index: i, child: j, node: n.content[j] };
    }
    if (idOf(n) === id && id) return { index: i, child: -1, node: n };
  }
  return null;
}

// Every block in reading order (a blockquote's paragraphs in its place): `[{id, node, index, child}]`.
export function flatBlocks(nodes) {
  const out = [];
  contentOf(nodes).forEach((n, i) => {
    if (!isObj(n)) return;
    if (n.type === 'blockquote') (n.content || []).forEach((c, j) => { if (isObj(c)) out.push({ id: idOf(c), node: c, index: i, child: j }); });
    else out.push({ id: idOf(n), node: n, index: i, child: -1 });
  });
  return out;
}

const quoteLike = (node) => isObj(node) && node.type === 'paragraph' && (attrsOf(node).style === 'quote' || attrsOf(node).style === 'verse');
// A block written back into a blockquote: its implied «quote» style is not spelled out.
function intoQuote(node) {
  if (attrsOf(node).style !== 'quote') return node;
  const attrs = { ...attrsOf(node) };
  delete attrs.style;
  return { ...node, attrs };
}

// A new list of nodes with the block `id` replaced by `blocks` (none: removed). Inside a blockquote the quote
// and verse paragraphs stay in it; any other block lifts out and cuts the blockquote in two (the second part
// without the id); an emptied blockquote goes.
export function replaceBlock(nodes, id, blocks) {
  const list = contentOf(nodes);
  const at = locate(list, id);
  const fresh = (Array.isArray(blocks) ? blocks : [blocks]).filter(isObj);
  if (!at) return list.slice();
  if (at.child < 0) return [...list.slice(0, at.index), ...fresh, ...list.slice(at.index + 1)];
  const bq = list[at.index];
  const kids = bq.content || [];
  const out = [];
  let run = kids.slice(0, at.child);
  let first = true;
  const close = () => {
    if (!run.length) return;
    if (first) out.push({ ...bq, content: run });
    else {
      const attrs = { ...attrsOf(bq) };
      delete attrs.id;
      const copy = { ...bq, content: run };
      if (Object.keys(attrs).length) copy.attrs = attrs;
      else delete copy.attrs;
      out.push(copy);
    }
    first = false;
    run = [];
  };
  fresh.forEach((b) => {
    if (quoteLike(b)) { run.push(intoQuote(b)); return; }
    close();
    out.push(b);
  });
  run = run.concat(kids.slice(at.child + 1));
  close();
  return [...list.slice(0, at.index), ...out, ...list.slice(at.index + 1)];
}

// The block with some attrs changed (`null` removes one): `«ابدأ صفحة جديدة»`, `«مع التالية»`.
export function setBlockAttrs(nodes, id, patch) {
  const at = locate(nodes, id);
  if (!at) return contentOf(nodes).slice();
  const attrs = { ...attrsOf(at.node) };
  Object.entries(patch || {}).forEach(([k, v]) => { if (v === null || v === undefined) delete attrs[k]; else attrs[k] = v; });
  return replaceBlock(nodes, id, [{ ...at.node, attrs }]);
}

// The footnote `noteId` and the block holding its call: `{block, note}` or null.
export function findNote(nodes, noteId) {
  for (const { id, node } of flatBlocks(nodes)) {
    const note = (node.content || []).find((item) => isObj(item) && item.type === 'footnote' && idOf(item) === noteId);
    if (note) return { block: id, note };
  }
  return null;
}
export const noteOwner = (nodes, noteId) => { const f = findNote(nodes, noteId); return f ? f.block : null; };
export const noteIds = (node) => (isObj(node) && Array.isArray(node.content) ? node.content.filter((i) => isObj(i) && i.type === 'footnote').map(idOf) : []);

// The scan page marks of a block: `[{offset, page, printed}]`.
export function pageMarks(node) {
  const out = [];
  let pos = 0;
  (isObj(node) && Array.isArray(node.content) ? node.content : []).forEach((item) => {
    if (isObj(item) && item.type === 'pageBreak') out.push({ offset: pos, page: attrsOf(item).page, printed: str(attrsOf(item).printed) });
    pos += itemLength(item);
  });
  return out;
}

// ---------------------------------------------------------------- find & replace by plain offsets
// Every match in the chapter's text and notes: `{block, note, start, end}` (plain offsets of the block's or the
// note's text; the same matches as `findMatches`, which counts in joined text nodes like the server).
export function findPlain(nodes, query, options) {
  const folded = foldQuery(query, options);
  const out = [];
  if (!folded.trim()) return out;
  for (const [block, note, content] of containers(nodes)) {
    let pos = 0;
    let run = null;
    const flush = () => {
      if (!run) return;
      segmentMatches(run.text, folded, options).forEach(([a, b]) => out.push({ block: idOf(block), note: note ? idOf(note) || null : null, start: run.start + a, end: run.start + b }));
      run = null;
    };
    content.forEach((item) => {
      if (isObj(item) && item.type === 'text') {
        if (!run) run = { start: pos, text: '' };
        run.text += str(item.text);
      } else flush();
      pos += itemLength(item);
    });
    flush();
  }
  return out;
}

// `content` with `start`…`end` replaced by `text` (the marks of the first character replaced, less `drop`).
export function replacePlain(content, start, end, text, drop = 'uncertain') {
  const list = Array.isArray(content) ? content : [];
  const total = inlineText(list).length;
  const a = Math.max(0, Math.min(start, total));
  const b = Math.max(a, Math.min(end, total));
  let marks = [];
  let pos = 0;
  list.forEach((item) => {
    const len = itemLength(item);
    if (item.type === 'text' && pos <= a && a < pos + len && !marks.length) marks = (item.marks || []).filter((m) => (isObj(m) ? m.type : m) !== drop);
    pos += len;
  });
  const middle = text ? [{ type: 'text', text: String(text), ...(marks.length ? { marks } : {}) }] : [];
  return normalizeContent([...sliceContent(list, 0, a), ...middle, ...sliceContent(list, b, total)]);
}

// `content` with `mark` taken off `start`…`end` (accepting an uncertain word).
export function unmarkPlain(content, start, end, mark = 'uncertain') {
  const out = [];
  let pos = 0;
  (Array.isArray(content) ? content : []).forEach((item) => {
    const len = itemLength(item);
    const a = pos;
    pos += len;
    if (!isObj(item) || item.type !== 'text' || a + len <= start || a >= end) { out.push(item); return; }
    const text = str(item.text);
    const cut = [Math.max(0, start - a), Math.min(len, end - a)];
    if (cut[0] > 0) out.push(withText(item, text.slice(0, cut[0])));
    const middle = withText(item, text.slice(cut[0], cut[1]));
    if (middle.marks) { middle.marks = middle.marks.filter((m) => m.type !== mark); if (!middle.marks.length) delete middle.marks; }
    out.push(middle);
    if (cut[1] < len) out.push(withText(item, text.slice(cut[1])));
  });
  return normalizeContent(out);
}

// A block (or its note `noteId`) with `fn(content)` applied to the right inline content.
export function editContent(node, noteId, fn) {
  if (!isObj(node)) return node;
  if (!noteId) return { ...node, content: fn(node.content || []) };
  return { ...node, content: (node.content || []).map((item) => (isObj(item) && item.type === 'footnote' && idOf(item) === noteId ? { ...item, content: fn(item.content || []) } : item)) };
}
export const replaceInBlock = (node, noteId, start, end, text) => editContent(node, noteId, (c) => replacePlain(c, start, end, text));

// ---------------------------------------------------------------- a block's static markup
// A block drawn by the browser in the page's faces until the engine lays it out again (the same markup and
// classes as the one-block editor, so the two look the same).
export function nodeHtml(node, opts = {}) {
  if (!isObj(node)) return '';
  const numberOf = typeof opts.numberOf === 'function' ? opts.numberOf : () => '';
  let seq = 0;
  const inline = (items) => (Array.isArray(items) ? items : []).map((item) => {
    if (!isObj(item)) return '';
    if (item.type === 'text') {
      let html = escapeHtml(item.text);
      const types = (item.marks || []).map((m) => (isObj(m) ? m.type : m));
      if (types.includes('uncertain')) html = `<mark class="ed-uncertain">${html}</mark>`;
      if (types.includes('italic')) html = `<i>${html}</i>`;
      if (types.includes('bold')) html = `<b>${html}</b>`;
      return html;
    }
    if (item.type === 'hardBreak') return '<br>';
    if (item.type === 'pageBreak') return `<span class="ed-pb" data-page="${escapeHtml(attrsOf(item).page == null ? '' : attrsOf(item).page)}"></span>`;
    if (item.type === 'footnote') { seq += 1; return `<sup class="ed-fn" data-seq="${escapeHtml(numberOf(idOf(item)) || seq)}"></sup>`; }
    return '';
  }).join('');
  const style = attrsOf(node).style;
  if (node.type === 'heading') return `<h2 class="ed-h${attrsOf(node).level === 2 ? 2 : 1}">${inline(node.content)}</h2>`;
  if (node.type === 'separator') return '<div class="ed-sep"></div>';
  if (node.type === 'title') return `<p class="ed-book-title">${inline(node.content)}</p>`;
  return `<p class="ed-p${PARAGRAPH_STYLES.includes(style) ? ` is-${style}` : ''}">${inline(node.content)}</p>`;
}
