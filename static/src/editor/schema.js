// The TipTap schema of the book page's editors: the Phase 4 nodes one to one (PHASE4_SPEC §2.8, PHASE5_SPEC §9.2).
//   blocks   title, heading (levels 1–2), paragraph (style null | quote | verse | center), separator (atom)
//   inline   text, hardBreak, pageBreak (a scan page mark, atom), footnote (atom with its own inline content)
//   marks    bold, italic, uncertain
// Every block carries id, sourcePages, sourceLineIds, reviewed, suggestedRole and the D47 flags breakBefore /
// keepWithNext, and keeps them through edits (a split copies the source attrs, never the id). Plugins: fresh
// ids for new and duplicated blocks and notes, footnote numbers as a `data-seq` decoration, the caret block
// (`is-caret`), the find highlights, and the uncertain mark dropped from a word the owner retypes.
// `extensions` is a whole chapter's schema; `blockExtensions` the one-block editor's (D47: a paragraph opened in
// place on its page) — a document of exactly one block whose edges (Enter, Backspace at the start, the arrows
// past its first or last line, Esc, undo) are reported to the page through `onBoundary`.
import { Extension, Mark, Node, mergeAttributes } from '@tiptap/core';
import { Plugin, PluginKey } from '@tiptap/pm/state';
import { Decoration, DecorationSet } from '@tiptap/pm/view';

import { PARAGRAPH_STYLES, newId } from './convert.js';

const list = (value) => (Array.isArray(value) ? value.filter((n) => Number.isInteger(n)) : []);
const parseList = (raw) => String(raw || '').split(',').map((v) => parseInt(v, 10)).filter((n) => Number.isFinite(n));
export const srcLabel = (pages) => {
  const p = list(pages);
  if (!p.length) return '';
  const first = Math.min(...p);
  const last = Math.max(...p);
  return first === last ? String(first) : `${first}–${last}`;
};

// ---------------------------------------------------------------- shared block attributes
const sourceAttributes = () => ({
  id: {
    default: null,
    keepOnSplit: false,
    parseHTML: (el) => el.getAttribute('data-id') || null,
    renderHTML: (attrs) => (attrs.id ? { 'data-id': attrs.id } : {}),
  },
  sourcePages: {
    default: [],
    parseHTML: (el) => parseList(el.getAttribute('data-pages')),
    renderHTML: (attrs) => {
      const pages = list(attrs.sourcePages);
      return pages.length ? { 'data-pages': pages.join(','), 'data-src': srcLabel(pages) } : {};
    },
  },
  sourceLineIds: {
    default: [],
    parseHTML: (el) => parseList(el.getAttribute('data-lines')),
    renderHTML: (attrs) => (list(attrs.sourceLineIds).length ? { 'data-lines': list(attrs.sourceLineIds).join(',') } : {}),
  },
  reviewed: {
    default: true,
    parseHTML: (el) => el.getAttribute('data-reviewed') !== 'false',
    renderHTML: (attrs) => (attrs.reviewed === false ? { 'data-reviewed': 'false' } : {}),
  },
  suggestedRole: { default: null, rendered: false },
  // D47: «ابدأ صفحة جديدة» / «مع التالية» (true / false as saved; null: not set)
  breakBefore: { default: null, rendered: false },
  keepWithNext: { default: null, rendered: false },
});

// ---------------------------------------------------------------- nodes
export const Doc = Node.create({ name: 'doc', topNode: true, content: 'block+' });

export const Text = Node.create({ name: 'text', group: 'inline noteInline' });

export const Paragraph = Node.create({
  name: 'paragraph',
  group: 'block',
  content: 'inline*',
  addAttributes() {
    return {
      ...sourceAttributes(),
      style: {
        default: null,
        parseHTML: (el) => (PARAGRAPH_STYLES.includes(el.getAttribute('data-style')) ? el.getAttribute('data-style') : null),
        renderHTML: (attrs) => (attrs.style ? { 'data-style': attrs.style } : {}),
      },
    };
  },
  parseHTML() {
    return [{ tag: 'p' }];
  },
  renderHTML({ node, HTMLAttributes }) {
    return ['p', mergeAttributes(HTMLAttributes, { class: `ed-p${node.attrs.style ? ` is-${node.attrs.style}` : ''}` }), 0];
  },
});

export const Heading = Node.create({
  name: 'heading',
  group: 'block',
  content: 'inline*',
  defining: true,
  addAttributes() {
    return {
      ...sourceAttributes(),
      level: {
        default: 1,
        // the editor's own markup says its level (h2.ed-h1 is a chapter heading); pasted h1 is one, h2–h6 subheadings
        parseHTML: (el) => {
          const own = parseInt(el.getAttribute('data-level'), 10);
          if (own === 1 || own === 2) return own;
          return el.tagName.toUpperCase() === 'H1' ? 1 : 2;
        },
        renderHTML: () => ({}),
      },
    };
  },
  parseHTML() {
    return [1, 2, 3, 4, 5, 6].map((n) => ({ tag: `h${n}` }));
  },
  renderHTML({ node, HTMLAttributes }) {
    const level = node.attrs.level === 2 ? 2 : 1;
    return [`h${level + 1}`, mergeAttributes(HTMLAttributes, { class: `ed-h${level}`, 'data-level': String(level) }), 0];
  },
});

export const Title = Node.create({
  name: 'title',
  group: 'block',
  content: 'inline*',
  defining: true,
  addAttributes() {
    return {
      ...sourceAttributes(),
      text: { default: '', rendered: false },
      author: { default: '', rendered: false },
    };
  },
  parseHTML() {
    return [{ tag: 'p.ed-book-title', priority: 60 }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['p', mergeAttributes(HTMLAttributes, { class: 'ed-book-title' }), 0];
  },
});

export const Separator = Node.create({
  name: 'separator',
  group: 'block',
  atom: true,
  selectable: true,
  addAttributes() {
    return sourceAttributes();
  },
  parseHTML() {
    return [{ tag: 'hr' }, { tag: 'div.ed-sep' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['div', mergeAttributes(HTMLAttributes, { class: 'ed-sep', role: 'separator', contenteditable: 'false', title: 'فاصل' })];
  },
  renderText() {
    return '\n* * *\n';
  },
});

export const HardBreak = Node.create({
  name: 'hardBreak',
  group: 'inline noteInline',
  inline: true,
  selectable: false,
  linebreakReplacement: true,
  parseHTML() {
    return [{ tag: 'br' }];
  },
  renderHTML() {
    return ['br'];
  },
  renderText() {
    return '\n';
  },
  addKeyboardShortcuts() {
    const insert = () => this.editor.commands.first(({ commands }) => [
      () => commands.exitCode(),
      () => commands.insertContent({ type: this.name }),
    ]);
    return { 'Shift-Enter': insert, 'Mod-Enter': insert };
  },
});

export const PageBreak = Node.create({
  name: 'pageBreak',
  group: 'inline',
  inline: true,
  atom: true,
  selectable: true,
  addAttributes() {
    return {
      page: { default: null, parseHTML: (el) => parseInt(el.getAttribute('data-page'), 10) || null, renderHTML: (attrs) => ({ 'data-page': attrs.page == null ? '' : String(attrs.page) }) },
      printed: { default: '', parseHTML: (el) => el.getAttribute('data-printed') || '', renderHTML: (attrs) => (attrs.printed ? { 'data-printed': attrs.printed } : {}) },
    };
  },
  parseHTML() {
    return [{ tag: 'span.ed-pb' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['span', mergeAttributes(HTMLAttributes, { class: 'ed-pb', contenteditable: 'false', 'aria-hidden': 'true' })];
  },
  renderText() {
    return ' ';
  },
});

export const Footnote = Node.create({
  name: 'footnote',
  group: 'inline',
  inline: true,
  atom: true,
  selectable: true,
  content: 'noteInline*',
  addAttributes() {
    return {
      id: { default: null, parseHTML: (el) => el.getAttribute('data-id') || null, renderHTML: (attrs) => (attrs.id ? { 'data-id': attrs.id } : {}) },
      number: { default: null, parseHTML: (el) => parseInt(el.getAttribute('data-n'), 10) || null, renderHTML: (attrs) => (attrs.number == null ? {} : { 'data-n': String(attrs.number) }) },
      marker: { default: '', rendered: false },
      sourcePage: { default: null, parseHTML: (el) => parseInt(el.getAttribute('data-page'), 10) || null, renderHTML: (attrs) => (attrs.sourcePage == null ? {} : { 'data-page': String(attrs.sourcePage) }) },
      sourceLineIds: { default: [], parseHTML: (el) => parseList(el.getAttribute('data-lines')), renderHTML: (attrs) => (list(attrs.sourceLineIds).length ? { 'data-lines': list(attrs.sourceLineIds).join(',') } : {}) },
      orphan: { default: false, parseHTML: (el) => el.getAttribute('data-orphan') === 'true', renderHTML: (attrs) => (attrs.orphan ? { 'data-orphan': 'true' } : {}) },
    };
  },
  parseHTML() {
    return [{ tag: 'sup.ed-fn', contentElement: '.ed-fn-body' }];
  },
  // The call is the number (a `data-seq` decoration, sequential in the chapter); the note's text stays in the
  // document as a hidden span, so copying and pasting inside the editor keeps the notes.
  renderHTML({ HTMLAttributes }) {
    return ['sup', mergeAttributes(HTMLAttributes, { class: 'ed-fn', role: 'button', contenteditable: 'false', title: 'الحاشية: نقرة لتحريرها' }), ['span', { class: 'ed-fn-body', hidden: 'hidden' }, 0]];
  },
  renderText({ node }) {
    return `[${node.textContent}]`;
  },
});

// ---------------------------------------------------------------- marks
export const Bold = Mark.create({
  name: 'bold',
  parseHTML() {
    return [{ tag: 'strong' }, { tag: 'b', getAttrs: (el) => el.style.fontWeight !== 'normal' && null }, { style: 'font-weight', getAttrs: (v) => (/^(bold(er)?|[6-9]\d{2,})$/.test(v) ? null : false) }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['b', HTMLAttributes, 0];
  },
  addKeyboardShortcuts() {
    return { 'Mod-b': () => this.editor.commands.toggleMark(this.name), 'Mod-B': () => this.editor.commands.toggleMark(this.name) };
  },
});

export const Italic = Mark.create({
  name: 'italic',
  parseHTML() {
    return [{ tag: 'em' }, { tag: 'i', getAttrs: (el) => el.style.fontStyle !== 'normal' && null }, { style: 'font-style=italic' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['i', HTMLAttributes, 0];
  },
  addKeyboardShortcuts() {
    return { 'Mod-i': () => this.editor.commands.toggleMark(this.name), 'Mod-I': () => this.editor.commands.toggleMark(this.name) };
  },
});

export const Uncertain = Mark.create({
  name: 'uncertain',
  inclusive: false,
  parseHTML() {
    return [{ tag: 'mark.ed-uncertain' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['mark', mergeAttributes(HTMLAttributes, { class: 'ed-uncertain', title: 'كلمة غير محسومة: نقرة لاختيار قراءتها' }), 0];
  },
});

// ---------------------------------------------------------------- plugins
export const findKey = new PluginKey('ed-find');
export const caretKey = new PluginKey('ed-caret');
export const numbersKey = new PluginKey('ed-numbers');
export const LOAD_META = 'ed:load';

const BLOCK_NAMES = new Set(['paragraph', 'heading', 'title', 'separator']);

// Fresh ids for blocks and notes that have none or repeat one (a split, a paste, a new paragraph): the server
// would repair them too, but the panel, the source pane and the find matches address blocks by id right away.
export function uniqueIdsPlugin() {
  return new Plugin({
    key: new PluginKey('ed-ids'),
    appendTransaction(transactions, _old, state) {
      if (!transactions.some((tr) => tr.docChanged)) return null;
      const seen = new Set();
      const fixes = [];
      state.doc.descendants((node, pos) => {
        const isBlock = BLOCK_NAMES.has(node.type.name);
        const isNote = node.type.name === 'footnote';
        if (!isBlock && !isNote) return true;
        const id = node.attrs.id;
        if (typeof id === 'string' && id && !seen.has(id)) seen.add(id);
        else fixes.push([pos, node, isNote ? 'ne' : 'e']);
        return isNote ? false : true;
      });
      if (!fixes.length) return null;
      const tr = state.tr;
      fixes.forEach(([pos, node, prefix]) => {
        const id = newId(prefix);
        seen.add(id);
        tr.setNodeMarkup(pos, undefined, { ...node.attrs, id }, node.marks);
      });
      tr.setMeta('addToHistory', false);
      return tr;
    },
  });
}

function numberDecorations(doc) {
  const decos = [];
  let n = 0;
  doc.descendants((node, pos) => {
    if (node.type.name === 'footnote') {
      n += 1;
      decos.push(Decoration.node(pos, pos + node.nodeSize, { 'data-seq': String(n) }));
      return false;
    }
    return true;
  });
  return DecorationSet.create(doc, decos);
}

// Footnote calls show their order in the chapter (1, 2, 3…): the printed numbers come from the stylesheet.
export function footnoteNumbersPlugin() {
  return new Plugin({
    key: numbersKey,
    state: {
      init: (_config, state) => numberDecorations(state.doc),
      apply: (tr, decos) => (tr.docChanged ? numberDecorations(tr.doc) : decos),
    },
    props: { decorations(state) { return this.getState(state); } },
  });
}

// The top-level block holding the selection head (a selected separator counts too).
export function caretBlockPos(state) {
  const { selection } = state;
  const $head = selection.$head;
  if ($head.depth >= 1) return $head.before(1);
  if (selection.node && selection.node.isBlock) return selection.from;
  return null;
}

function caretDecorations(state) {
  const pos = caretBlockPos(state);
  if (pos === null) return DecorationSet.empty;
  const node = state.doc.nodeAt(pos);
  if (!node) return DecorationSet.empty;
  return DecorationSet.create(state.doc, [Decoration.node(pos, pos + node.nodeSize, { class: 'is-caret' })]);
}

// The block under the caret carries `is-caret`: its source badge shows in the margin (editor.css).
export function caretBlockPlugin() {
  return new Plugin({
    key: caretKey,
    state: {
      init: (_config, state) => caretDecorations(state),
      apply: (tr, decos, _old, state) => (tr.docChanged || tr.selectionSet ? caretDecorations(state) : decos),
    },
    props: { decorations(state) { return this.getState(state); } },
  });
}

// Find highlights: `{ ranges: [{from, to, block, note}], current }` set through the `ed:find` meta and mapped
// through later edits (the glue searches again once the edits settle).
export function findPlugin() {
  const build = (doc, ranges, current) => DecorationSet.create(doc, ranges.map((r, i) => Decoration.inline(r.from, r.to, { class: `ed-match${i === current ? ' is-current' : ''}` })));
  return new Plugin({
    key: findKey,
    state: {
      init: () => ({ ranges: [], current: -1, decos: DecorationSet.empty }),
      apply(tr, value) {
        const meta = tr.getMeta(findKey);
        if (meta) return { ranges: meta.ranges, current: meta.current, decos: build(tr.doc, meta.ranges, meta.current) };
        if (!tr.docChanged || !value.ranges.length) return value;
        const ranges = value.ranges.map((r) => ({ ...r, from: tr.mapping.map(r.from), to: tr.mapping.map(r.to, -1) })).filter((r) => r.to > r.from);
        const current = Math.min(value.current, ranges.length - 1);
        return { ranges, current, decos: build(tr.doc, ranges, current) };
      },
    },
    props: { decorations(state) { return this.getState(state).decos; } },
  });
}

// A word the owner retypes is theirs: the uncertain mark leaves the whole marked word around a small typed
// insertion (loads, pastes, undo and the find replacements are left alone).
export function resolveOnTypePlugin() {
  return new Plugin({
    key: new PluginKey('ed-resolve'),
    appendTransaction(transactions, _old, state) {
      const type = state.schema.marks.uncertain;
      if (!type) return null;
      const spans = [];
      transactions.forEach((tr) => {
        if (!tr.docChanged || tr.getMeta(LOAD_META) || tr.getMeta('history$') || tr.getMeta('addToHistory') === false || tr.getMeta('uiEvent')) return;
        tr.steps.forEach((step, i) => {
          step.getMap().forEach((_os, _oe, ns, ne) => {
            if (ne > ns && ne - ns <= 8) spans.push([tr.mapping.slice(i + 1).map(ns), tr.mapping.slice(i + 1).map(ne, -1)]);
          });
        });
      });
      if (!spans.length) return null;
      const tr = state.tr;
      let changed = false;
      spans.forEach(([from, to]) => {
        const $from = state.doc.resolve(Math.max(0, Math.min(from, state.doc.content.size)));
        if (!$from.parent.isTextblock && $from.parent.type.name !== 'footnote') return;
        const parentStart = $from.start();
        const parent = $from.parent;
        let a = from;
        let b = to;
        let hit = false;
        parent.forEach((child, offset) => {
          const cs = parentStart + offset;
          const ce = cs + child.nodeSize;
          if (!child.isText || !type.isInSet(child.marks)) return;
          if (ce > from && cs < to) { hit = true; a = Math.min(a, cs); b = Math.max(b, ce); }
        });
        if (hit) { tr.removeMark(a, b, type); changed = true; }
      });
      return changed ? tr : null;
    },
  });
}

// ---------------------------------------------------------------- the toolbar's keys (PHASE5_SPEC §4)
export const Shortcuts = Extension.create({
  name: 'edShortcuts',
  addOptions() {
    return { onFootnote: null };
  },
  addKeyboardShortcuts() {
    const set = (style) => () => this.editor.commands.setBlockStyle(style);
    return {
      'Mod-Alt-1': set('heading1'),
      'Mod-Alt-2': set('heading2'),
      'Mod-Alt-0': set('paragraph'),
      'Mod-Alt-3': set('quote'),
      'Mod-Alt-4': set('verse'),
      'Mod-Alt-5': set('center'),
      'Mod-Alt-6': set('separator'),
      'Mod-Shift-f': () => { if (this.options.onFootnote) this.options.onFootnote(); return true; },
      'Mod-Shift-F': () => { if (this.options.onFootnote) this.options.onFootnote(); return true; },
    };
  },
});

export const Plugins = Extension.create({
  name: 'edPlugins',
  addProseMirrorPlugins() {
    return [uniqueIdsPlugin(), footnoteNumbersPlugin(), caretBlockPlugin(), findPlugin(), resolveOnTypePlugin()];
  },
  addCommands() {
    return {
      // The style picker's choice for the block under the caret: a node type (with the paragraph style),
      // a separator inserted after the block (or in place of an empty one).
      setBlockStyle: (style) => ({ state, chain, commands }) => {
        if (style === 'separator') {
          const { $from } = state.selection;
          const block = $from.depth >= 1 ? $from.node(1) : null;
          const attrs = block ? { sourcePages: block.attrs.sourcePages, sourceLineIds: block.attrs.sourceLineIds, reviewed: block.attrs.reviewed } : {};
          if (block && block.isTextblock && block.content.size === 0) {
            return commands.insertContentAt({ from: $from.before(1), to: $from.after(1) }, { type: 'separator', attrs });
          }
          return commands.insertContentAt($from.after(1), [{ type: 'separator', attrs }, { type: 'paragraph', attrs }]);
        }
        if (style === 'heading1' || style === 'heading2') return chain().setNode('heading', { level: style === 'heading1' ? 1 : 2 }).run();
        if (style === 'title') return chain().setNode('title').run();
        if (style === 'paragraph') return chain().setNode('paragraph', { style: null }).run();
        if (PARAGRAPH_STYLES.includes(style)) return chain().setNode('paragraph', { style }).run();
        return false;
      },
    };
  },
});

export const NoteDoc = Node.create({ name: 'doc', topNode: true, content: 'noteInline*' });

// The footnote editor's keys: Enter finishes the note (the popover closes, the text keeps the caret); a line
// inside the note is ⇧Enter (HardBreak). Without this Enter would fall to the browser in a doc of inline nodes.
export const NoteKeys = Extension.create({
  name: 'noteKeys',
  addOptions() {
    return { onSubmit: null };
  },
  addKeyboardShortcuts() {
    return {
      Enter: () => { if (this.options.onSubmit) this.options.onSubmit(); return true; },
    };
  },
});

export const extensions = ({ onFootnote } = {}) => [
  Doc, Text, Paragraph, Heading, Title, Separator, HardBreak, PageBreak, Footnote, Bold, Italic, Uncertain, Plugins,
  Shortcuts.configure({ onFootnote }),
];

export const noteExtensions = ({ onSubmit } = {}) => [NoteDoc, Text, HardBreak, Bold, Italic, Uncertain, NoteKeys.configure({ onSubmit })];

// ---------------------------------------------------------------- the one-block editor (D47)
export const BlockDoc = Node.create({ name: 'doc', topNode: true, content: 'block' });
export const NUMBERS_META = 'ed:numbers';

// Footnote calls show the number printed on the page (`numberOf(id)`, from the page layout), else their order.
export function pageNumbersPlugin(numberOf) {
  const build = (doc) => {
    const decos = [];
    let n = 0;
    doc.descendants((node, pos) => {
      if (node.type.name === 'footnote') {
        n += 1;
        const printed = numberOf ? numberOf(node.attrs.id) : '';
        decos.push(Decoration.node(pos, pos + node.nodeSize, { 'data-seq': String(printed || n) }));
        return false;
      }
      return true;
    });
    return DecorationSet.create(doc, decos);
  };
  return new Plugin({
    key: numbersKey,
    state: {
      init: (_config, state) => build(state.doc),
      apply: (tr, decos) => (tr.docChanged || tr.getMeta(NUMBERS_META) ? build(tr.doc) : decos),
    },
    props: { decorations(state) { return this.getState(state); } },
  });
}

// Every key the block cannot answer alone goes to `handle(name, editor)` first (true: handled). The style keys
// of the toolbar stay here (a separator is a boundary: it adds a block after this one).
export const BlockKeys = Extension.create({
  name: 'edBlockKeys',
  priority: 1000,
  addOptions() {
    return { handle: null };
  },
  addKeyboardShortcuts() {
    const to = (name) => () => (this.options.handle ? Boolean(this.options.handle(name, this.editor)) : false);
    const set = (style) => () => this.editor.commands.setBlockStyle(style);
    return {
      Enter: to('Enter'),
      Backspace: to('Backspace'),
      Delete: to('Delete'),
      ArrowUp: to('ArrowUp'),
      ArrowDown: to('ArrowDown'),
      ArrowLeft: to('ArrowLeft'),
      ArrowRight: to('ArrowRight'),
      Escape: to('Escape'),
      'Mod-z': to('undo'),
      'Mod-Z': to('redo'),
      'Shift-Mod-z': to('redo'),
      'Mod-y': to('redo'),
      'Mod-s': to('save'),
      'Mod-Alt-1': set('heading1'),
      'Mod-Alt-2': set('heading2'),
      'Mod-Alt-0': set('paragraph'),
      'Mod-Alt-3': set('quote'),
      'Mod-Alt-4': set('verse'),
      'Mod-Alt-5': set('center'),
      'Mod-Alt-6': to('separator'),
      'Mod-Shift-f': to('footnote'),
      'Mod-Shift-F': to('footnote'),
    };
  },
});

export const BlockPlugins = Extension.create({
  name: 'edBlockPlugins',
  addOptions() {
    return { numberOf: null };
  },
  addProseMirrorPlugins() {
    return [uniqueIdsPlugin(), pageNumbersPlugin(this.options.numberOf), findPlugin(), resolveOnTypePlugin()];
  },
  addCommands() {
    return {
      setBlockStyle: (style) => ({ chain }) => {
        if (style === 'heading1' || style === 'heading2') return chain().setNode('heading', { level: style === 'heading1' ? 1 : 2 }).run();
        if (style === 'title') return chain().setNode('title').run();
        if (style === 'paragraph') return chain().setNode('paragraph', { style: null }).run();
        if (PARAGRAPH_STYLES.includes(style)) return chain().setNode('paragraph', { style }).run();
        return false;
      },
    };
  },
});

export const blockExtensions = ({ handle, numberOf } = {}) => [
  BlockDoc, Text, Paragraph, Heading, Title, Separator, HardBreak, PageBreak, Footnote, Bold, Italic, Uncertain,
  BlockPlugins.configure({ numberOf }), BlockKeys.configure({ handle }),
];

// ---------------------------------------------------------------- plain offsets ↔ positions (D47)
// A plain offset counts a footnote call as one position (= editor.document.inline_text); in the document the
// call is a node of its own size. `start` is the position where the textblock's content begins.
const plainLength = (child) => (child.isText ? child.text.length : 1);
export function posOfOffset(block, offset, start = 1) {
  let pos = start;
  let left = Math.max(0, Number(offset) || 0);
  for (let i = 0; i < block.childCount; i += 1) {
    const child = block.child(i);
    const len = plainLength(child);
    if (left < len || (left === len && child.isText)) return child.isText ? pos + left : pos + (left > 0 ? child.nodeSize : 0);
    left -= len;
    pos += child.nodeSize;
  }
  return pos;
}
export function offsetOfPos(block, pos, start = 1) {
  let at = start;
  let off = 0;
  for (let i = 0; i < block.childCount; i += 1) {
    const child = block.child(i);
    if (pos <= at) return off;
    const end = at + child.nodeSize;
    if (pos < end) return child.isText ? off + (pos - at) : off + 1;
    off += plainLength(child);
    at = end;
  }
  return off;
}
