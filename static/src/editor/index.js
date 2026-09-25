// The chapter editor bundle (PHASE5_SPEC §4): TipTap on the Phase 4 schema, behind one small object the
// Alpine glue (static/src/js/editor.js) drives, so the glue can be run under Node with a stub in its place.
//   window.NassakhEditor.create(element, options)  → the editor object below
//   window.NassakhEditor.createNote(element, opts) → the footnote's small editor (text, line breaks, B / I)
//   window.NassakhEditor.{toEditor, fromEditor, findMatches, wordCount, STYLES, …}  the pure helpers
// Options of `create`: { content (the chapter's nodes), editable, onUpdate, onSelection, onFocus, onBlur,
// onFootnote({id, pos, dom}) (a click on a call, or ⌘⇧F after the insert), onUncertain({from, to, text, dom}) }.
// The editor object: content in and out (`getContent` gives the nodes the server saves), the style of the block
// under the caret and `setStyle`, marks, undo / redo, footnotes (insert, read, write, delete), the caret block
// with its source pages and lines, find & replace (highlights, cycling, replace one / all), word count.
import { Editor, createDocument, getMarkRange } from '@tiptap/core';
import { Gapcursor, Placeholder, UndoRedo } from '@tiptap/extensions';
import { Fragment } from '@tiptap/pm/model';
import { NodeSelection, TextSelection } from '@tiptap/pm/state';

import * as convert from './convert.js';
import { LOAD_META, caretBlockPos, extensions, findKey, noteExtensions, srcLabel } from './schema.js';

export const VERSION = '1';

const BLOCKS = new Set(['paragraph', 'heading', 'title', 'separator']);

function styleOfNode(node) {
  if (!node) return 'paragraph';
  if (node.type.name === 'heading') return node.attrs.level === 2 ? 'heading2' : 'heading1';
  if (node.type.name === 'title') return 'title';
  if (node.type.name === 'separator') return 'separator';
  return convert.PARAGRAPH_STYLES.includes(node.attrs.style) ? node.attrs.style : 'paragraph';
}

function blockInfo(node, pos, index) {
  return {
    id: node.attrs.id || null,
    type: node.type.name,
    style: styleOfNode(node),
    sourcePages: Array.isArray(node.attrs.sourcePages) ? node.attrs.sourcePages.slice() : [],
    sourceLineIds: Array.isArray(node.attrs.sourceLineIds) ? node.attrs.sourceLineIds.slice() : [],
    reviewed: node.attrs.reviewed !== false,
    src: srcLabel(node.attrs.sourcePages),
    pos,
    index,
    size: node.nodeSize,
  };
}

function withoutUncertain(marks, schema) {
  const type = schema.marks.uncertain;
  return (marks || []).filter((m) => m.type !== type);
}

// The find ranges of a document: text runs of every block and note, matched like the server (convert.js).
function findRanges(doc, query, options) {
  const folded = convert.foldQuery(query, options);
  const out = [];
  if (!folded.trim()) return out;
  const scan = (container, start, blockId, noteId) => {
    let runStart = null;
    let text = '';
    const flush = () => {
      if (runStart === null) return;
      convert.segmentMatches(text, folded, options).forEach(([a, b]) => out.push({ from: runStart + a, to: runStart + b, block: blockId, note: noteId }));
      runStart = null;
      text = '';
    };
    container.forEach((child, offset) => {
      const pos = start + offset;
      if (child.isText) {
        if (runStart === null) runStart = pos;
        text += child.text;
        return;
      }
      flush();
      if (child.type.name === 'footnote') scan(child, pos + 1, blockId, child.attrs.id || null);
    });
    flush();
  };
  doc.forEach((block, offset) => {
    if (block.isTextblock) scan(block, offset + 1, block.attrs.id || null, null);
  });
  return out;
}

export function create(element, options = {}) {
  const opts = options || {};
  const listeners = { update: [], selection: [], focus: [], blur: [], transaction: [] };
  let editor = null;
  let ready = false;
  const fire = (name, payload) => listeners[name].forEach((fn) => { try { fn(payload); } catch (_) { /* a listener's error never breaks typing */ } });
  // The live views of the editor are getters on the object itself (Object.assign would copy their values once).
  const api = {
    get editor() { return editor; },
    get state() { return editor.state; },
    get view() { return editor.view; },
    get dom() { return editor.view.dom; },
    get isFocused() { return Boolean(editor && editor.isFocused); },
    get isEditable() { return Boolean(editor && editor.isEditable); },
  };

  function build(content) {
    editor = new Editor({
      element,
      extensions: [
        ...extensions({ onFootnote: () => { const note = api.insertFootnote(); if (note && opts.onFootnote) opts.onFootnote(note); } }),
        UndoRedo.configure({ depth: 400, newGroupDelay: 600 }),
        Placeholder.configure({ placeholder: 'ابدأ الكتابة…', showOnlyCurrent: false }),
        Gapcursor, // a caret before or after a separator at the chapter's ends (editor.css draws it)
      ],
      content: convert.toEditor(content),
      editable: opts.editable !== false,
      injectCSS: false,
      editorProps: {
        attributes: { class: 'ed-doc', dir: 'rtl', lang: 'ar', spellcheck: 'false', role: 'textbox', 'aria-multiline': 'true', 'aria-label': 'نص الفصل' },
        handleClickOn(view, _pos, node, nodePos) {
          if (node.type.name !== 'footnote') return false;
          if (opts.onFootnote) opts.onFootnote({ id: node.attrs.id, pos: nodePos, dom: view.nodeDOM(nodePos) });
          return true;
        },
        handleClick(view, pos, event) {
          const target = event && event.target;
          const markEl = target && typeof target.closest === 'function' ? target.closest('mark.ed-uncertain') : null;
          if (!markEl || !opts.onUncertain) return false;
          const range = api.uncertainAt(pos);
          if (range) opts.onUncertain({ ...range, dom: markEl });
          return false;
        },
      },
      onUpdate: ({ transaction }) => { if (ready && !transaction.getMeta(LOAD_META)) fire('update', { transaction }); },
      onSelectionUpdate: () => { if (ready) fire('selection'); },
      onTransaction: ({ transaction }) => { if (ready) fire('transaction', { transaction }); },
      onFocus: () => fire('focus'),
      onBlur: () => fire('blur'),
    });
    ready = true;
  }

  const state = () => editor.state;
  const schema = () => editor.schema;

  function findNote(id) {
    let found = null;
    state().doc.descendants((node, pos) => {
      if (found) return false;
      if (node.type.name === 'footnote') {
        if (node.attrs.id === id) found = { node, pos };
        return false;
      }
      return true;
    });
    return found;
  }

  function noteSeq(pos) {
    let n = 0;
    let seq = 0;
    state().doc.descendants((node, at) => {
      if (seq) return false;
      if (node.type.name === 'footnote') { n += 1; if (at === pos) seq = n; return false; }
      return true;
    });
    return seq;
  }

  function findBlock(id) {
    let found = null;
    state().doc.forEach((node, offset, index) => {
      if (!found && node.attrs.id === id) found = { node, pos: offset, index };
    });
    return found;
  }

  function current() {
    return findKey.getState(state()) || { ranges: [], current: -1 };
  }

  function setFind(ranges, index) {
    const tr = state().tr.setMeta(findKey, { ranges, current: index });
    editor.view.dispatch(tr);
  }

  function select(from, to) {
    const doc = state().doc;
    const tr = state().tr.setSelection(TextSelection.create(doc, Math.min(from, doc.content.size), Math.min(to, doc.content.size))).scrollIntoView();
    editor.view.dispatch(tr);
  }

  Object.assign(api, {
    on(name, fn) { if (listeners[name]) listeners[name].push(fn); return api; },

    // ---------------------------------------------------------- content
    getJSON() { return editor.getJSON(); },
    getContent() { return convert.fromEditor(editor.getJSON()); },
    // A new document: the editor is rebuilt so its history starts afresh (a chapter switch, a reload).
    setContent(content) {
      const editable = editor ? editor.isEditable : opts.editable !== false;
      ready = false;
      if (editor) editor.destroy(); // removes its own root from the element; the sheet's other children stay
      opts.editable = editable;
      build(content);
      return api;
    },
    // The same document again, in place (the caret and the history stay): loads use the `ed:load` meta.
    replaceContent(content) {
      const doc = createDocument(convert.toEditor(content), schema());
      const tr = state().tr.replaceWith(0, state().doc.content.size, doc.content).setMeta(LOAD_META, true).setMeta('preventUpdate', true);
      editor.view.dispatch(tr);
      return api;
    },
    setEditable(on) { editor.setEditable(Boolean(on), false); return api; },
    focus(where) { editor.commands.focus(where === undefined ? null : where, { scrollIntoView: false }); return api; },
    blur() { editor.commands.blur(); return api; },
    destroy() { ready = false; if (editor) editor.destroy(); editor = null; },

    // ---------------------------------------------------------- history, marks, styles
    undo() { return editor.chain().focus().undo().run(); },
    redo() { return editor.chain().focus().redo().run(); },
    canUndo() { return editor.can().undo(); },
    canRedo() { return editor.can().redo(); },
    toggleBold() { return editor.chain().focus().toggleMark('bold').run(); },
    toggleItalic() { return editor.chain().focus().toggleMark('italic').run(); },
    isBold() { return editor.isActive('bold'); },
    isItalic() { return editor.isActive('italic'); },
    currentStyle() {
      const pos = caretBlockPos(state());
      return styleOfNode(pos === null ? null : state().doc.nodeAt(pos));
    },
    setStyle(key) {
      if (key === 'footnote') return api.insertFootnote();
      return editor.chain().focus().setBlockStyle(key).run();
    },

    // ---------------------------------------------------------- blocks
    caretBlock() {
      const pos = caretBlockPos(state());
      if (pos === null) return null;
      const node = state().doc.nodeAt(pos);
      if (!node) return null;
      const index = state().doc.resolve(pos).index(0);
      return blockInfo(node, pos, index);
    },
    blocks() {
      const out = [];
      state().doc.forEach((node, offset, index) => { if (BLOCKS.has(node.type.name)) out.push(blockInfo(node, offset, index)); });
      return out;
    },
    blockDom(id) {
      const found = findBlock(id);
      return found ? editor.view.nodeDOM(found.pos) : null;
    },
    goToBlock(id) {
      const found = findBlock(id);
      if (!found) return false;
      const doc = state().doc;
      const sel = found.node.isTextblock ? TextSelection.near(doc.resolve(found.pos + 1)) : NodeSelection.create(doc, found.pos);
      editor.view.dispatch(state().tr.setSelection(sel).scrollIntoView());
      editor.commands.focus(undefined, { scrollIntoView: false });
      return true;
    },
    selectRange(from, to) { select(from, to); return api; },

    // ---------------------------------------------------------- footnotes
    insertFootnote() {
      const { selection } = state();
      const $from = selection.$from;
      if (!$from.parent.isTextblock) return null;
      const block = $from.depth >= 1 ? $from.node(1) : null;
      const id = convert.newId('ne');
      const node = schema().nodes.footnote.create({
        id,
        number: null,
        marker: '',
        sourcePage: block && block.attrs.sourcePages && block.attrs.sourcePages.length ? block.attrs.sourcePages[0] : null,
        sourceLineIds: [],
        orphan: false,
      });
      const tr = state().tr.replaceSelectionWith(node, false);
      const pos = tr.selection.from - node.nodeSize;
      tr.setSelection(TextSelection.create(tr.doc, pos + node.nodeSize)).scrollIntoView();
      editor.view.dispatch(tr);
      return { id, pos, dom: editor.view.nodeDOM(pos) };
    },
    noteAt(id) {
      const found = findNote(id);
      if (!found) return null;
      const { node, pos } = found;
      return {
        id,
        pos,
        seq: noteSeq(pos),
        number: node.attrs.number,
        sourcePage: node.attrs.sourcePage,
        sourceLineIds: Array.isArray(node.attrs.sourceLineIds) ? node.attrs.sourceLineIds.slice() : [],
        orphan: Boolean(node.attrs.orphan),
        content: node.content.toJSON() || [],
        text: node.textContent,
        dom: editor.view.nodeDOM(pos),
      };
    },
    notes() {
      const out = [];
      state().doc.descendants((node, pos) => {
        if (node.type.name === 'footnote') { out.push({ id: node.attrs.id, pos, text: node.textContent }); return false; }
        return true;
      });
      return out;
    },
    setNoteContent(id, content) {
      const found = findNote(id);
      if (!found) return false;
      const fragment = Fragment.fromJSON(schema(), Array.isArray(content) ? content : []);
      if (found.node.content.eq(fragment)) return false;
      const tr = state().tr.replaceWith(found.pos + 1, found.pos + 1 + found.node.content.size, fragment);
      editor.view.dispatch(tr);
      return true;
    },
    deleteNote(id) {
      const found = findNote(id);
      if (!found) return false;
      const tr = state().tr.delete(found.pos, found.pos + found.node.nodeSize);
      tr.setSelection(TextSelection.near(tr.doc.resolve(found.pos)));
      editor.view.dispatch(tr);
      return true;
    },

    // ---------------------------------------------------------- uncertain words
    uncertainAt(pos) {
      const type = schema().marks.uncertain;
      const doc = state().doc;
      const range = getMarkRange(doc.resolve(Math.max(0, Math.min(pos, doc.content.size))), type);
      if (!range) return null;
      return { from: range.from, to: range.to, text: doc.textBetween(range.from, range.to) };
    },
    acceptUncertain(from, to) {
      editor.view.dispatch(state().tr.removeMark(from, to, schema().marks.uncertain));
      return true;
    },
    replaceRange(from, to, text) {
      const doc = state().doc;
      const marks = withoutUncertain(doc.resolve(from).marks(), schema());
      const tr = text ? state().tr.replaceWith(from, to, schema().text(text, marks)) : state().tr.delete(from, to);
      editor.view.dispatch(tr);
      return true;
    },

    // ---------------------------------------------------------- find & replace
    find(query, options, keepIndex) {
      const ranges = findRanges(state().doc, query, options);
      let index = -1;
      if (ranges.length) {
        const before = current();
        const caret = state().selection.from;
        if (keepIndex && before.current >= 0 && before.current < ranges.length) index = before.current;
        else {
          index = ranges.findIndex((r) => r.from >= caret);
          if (index === -1) index = 0;
        }
      }
      setFind(ranges, index);
      return { total: ranges.length, index };
    },
    findCurrent() {
      const c = current();
      return c.current >= 0 ? c.ranges[c.current] : null;
    },
    // The next match (dir > 0), the previous (dir < 0) or the current one again (dir 0, after a replacement).
    findNext(dir) {
      const c = current();
      if (!c.ranges.length) return null;
      const index = convert.stepIndex(c.current, dir, c.ranges.length);
      setFind(c.ranges, index);
      const range = c.ranges[index];
      if (!range.note) select(range.from, range.to);
      else {
        const found = findNote(range.note);
        if (found) editor.view.dispatch(state().tr.setSelection(NodeSelection.create(state().doc, found.pos)).scrollIntoView());
      }
      return { ...range, index, total: c.ranges.length };
    },
    clearFind() { if (current().ranges.length) setFind([], -1); return api; },
    replaceCurrent(replacement) {
      const range = api.findCurrent();
      if (!range) return false;
      const marks = withoutUncertain(state().doc.resolve(range.from).marks(), schema());
      const text = String(replacement || '');
      const tr = text ? state().tr.replaceWith(range.from, range.to, schema().text(text, marks)) : state().tr.delete(range.from, range.to);
      editor.view.dispatch(tr);
      return true;
    },
    replaceAll(replacement) {
      const c = current();
      if (!c.ranges.length) return 0;
      const text = String(replacement || '');
      const tr = state().tr;
      c.ranges.slice().sort((a, b) => b.from - a.from).forEach((range) => {
        const marks = withoutUncertain(tr.doc.resolve(range.from).marks(), schema());
        if (text) tr.replaceWith(range.from, range.to, schema().text(text, marks));
        else tr.delete(range.from, range.to);
      });
      tr.setMeta(findKey, { ranges: [], current: -1 });
      editor.view.dispatch(tr);
      return c.ranges.length;
    },

    // ---------------------------------------------------------- counts
    wordCount() {
      let total = 0;
      const count = (text) => text.split(/\s+/).filter(Boolean).length;
      state().doc.forEach((block) => {
        if (!block.isTextblock) return;
        let text = '';
        block.forEach((child) => {
          if (child.isText) text += child.text;
          else { text += ' '; if (child.type.name === 'footnote') total += count(child.textBetween(0, child.content.size, ' ', ' ')); }
        });
        total += count(text);
      });
      return total;
    },
  });

  build(opts.content);
  return api;
}

// The footnote's editor: one line of text with breaks and B / I; `onUpdate` receives the note's content JSON,
// `onSubmit` fires on Enter (the glue closes the popover).
export function createNote(element, options = {}) {
  const opts = options || {};
  const editor = new Editor({
    element,
    extensions: noteExtensions({ onSubmit: () => { if (opts.onSubmit) opts.onSubmit(); } }),
    content: { type: 'doc', content: Array.isArray(opts.content) ? opts.content : [] },
    editable: opts.editable !== false,
    injectCSS: false,
    editorProps: { attributes: { class: 'ed-note-editor', dir: 'rtl', lang: 'ar', spellcheck: 'false', role: 'textbox', 'aria-label': 'نص الحاشية' } },
    onUpdate: ({ editor: ed }) => { if (opts.onUpdate) opts.onUpdate(ed.getJSON().content || []); },
  });
  return {
    get editor() { return editor; },
    getContent() { return editor.getJSON().content || []; },
    setContent(content) {
      const next = Array.isArray(content) ? content : [];
      if (JSON.stringify(editor.getJSON().content || []) === JSON.stringify(next)) return;
      editor.commands.setContent({ type: 'doc', content: next }, { emitUpdate: false });
    },
    focus(where) { editor.commands.focus(where === undefined ? 'end' : where, { scrollIntoView: false }); },
    selectRange(from, to) { editor.view.dispatch(editor.state.tr.setSelection(TextSelection.create(editor.state.doc, from, to))); },
    toggleBold() { return editor.chain().focus().toggleMark('bold').run(); },
    toggleItalic() { return editor.chain().focus().toggleMark('italic').run(); },
    isBold() { return editor.isActive('bold'); },
    isItalic() { return editor.isActive('italic'); },
    isEmpty() { return editor.state.doc.content.size === 0; },
    destroy() { editor.destroy(); },
  };
}

export const {
  STYLES, STYLE_LABELS, PARAGRAPH_STYLES, DEFAULT_FIND, toEditor, fromEditor, newId, fold, foldQuery, segmentMatches, findMatches,
  wordCount, blockText, formatCount, pageRange, wordsHtml, stepIndex, chapterListHtml, escapeHtml, styleOf,
} = convert;
