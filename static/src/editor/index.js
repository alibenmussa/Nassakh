// The book page's editor bundle (PHASE5_SPEC §9.2, D47): TipTap on the Phase 4 schema, behind small objects the
// Alpine glue (static/src/js/book/*.js) drives, so the glue runs under Node with a stub in their place.
//   window.NassakhEditor.createBlock(element, options) → one paragraph opened in place on its page (below)
//   window.NassakhEditor.createNote(element, options)  → the footnote's small editor (text, line breaks, B / I)
//   window.NassakhEditor.{toEditor, fromEditor, splitNode, mergeNodes, replaceBlock, findPlain, …}  pure helpers
//
// createBlock(element, { node, offset, numberOf, onChange, onSelection, onFocus, onBlur, onBoundary(kind, info),
//                        onFootnote({id, dom}), onUncertain({from, to, start, end, text, dom}) })
//   `node` is one block of the chapter as saved (a paragraph, a heading…); the editor holds exactly that block
//   in the page's face, size, measure and line height (the glue sets them on `element`). What the block cannot
//   answer alone is a boundary for the page: `split` (Enter: `{node, from, to}`), `mergeBackward` (Backspace at
//   the start), `mergeForward` (Delete at the end), `up` / `down` (the arrows past the first or last line:
//   `{x}`), `prev` / `next` (the reading-direction arrow at an edge), `escape`, `undo`, `redo`, `save`,
//   `separator` (⌘⌥6), `paste` (several paragraphs: `{node, from, to, blocks}`). The editor has no history of
//   its own: undo is the chapter's (the page keeps every version of the chapter it saved). D99: `pageBreak`
//   (⌘↩: `{node, from, to}`); `textAttrs()` / `setTextAttrs(patch)` / `setAlign(value)` the text options.
//   Offsets are plain-text offsets of the block (a footnote call one position, UTF-16), as in the page layout.
import { Editor, getMarkRange } from '@tiptap/core';
import { DOMSerializer, Fragment } from '@tiptap/pm/model';
import { TextSelection } from '@tiptap/pm/state';

import * as convert from './convert.js';
import { NUMBERS_META, blockExtensions, findKey, noteExtensions, offsetOfPos, posOfOffset } from './schema.js';

export const VERSION = '2';

const BLOCK_START = 1; // the one block of the document begins at 0; its text at 1

function styleOfNode(node) {
  if (!node) return 'paragraph';
  if (node.type.name === 'heading') return node.attrs.level === 2 ? 'heading2' : 'heading1';
  if (node.type.name === 'title') return 'title';
  if (node.type.name === 'separator') return 'separator';
  return convert.PARAGRAPH_STYLES.includes(node.attrs.style) ? node.attrs.style : 'paragraph';
}

function withoutUncertain(marks, schema) {
  const type = schema.marks.uncertain;
  return (marks || []).filter((m) => m.type !== type);
}

export function createBlock(element, options = {}) {
  const opts = options || {};
  const listeners = { change: opts.onChange, selection: opts.onSelection, focus: opts.onFocus, blur: opts.onBlur };
  const fire = (name, payload) => { const fn = listeners[name]; if (typeof fn === 'function') { try { fn(payload); } catch (_) { /* never breaks typing */ } } };
  let editor = null;
  let ready = false;
  let numbers = typeof opts.numberOf === 'function' ? opts.numberOf : null;
  const block = () => editor.state.doc.child(0);
  // the block's own direction (D99: a left-to-right paragraph inside the right-to-left page)
  const rtl = () => block().attrs.dir !== 'ltr' && (editor.view.dom && editor.view.dom.getAttribute('dir')) !== 'ltr';
  const boundary = (kind, info) => (typeof opts.onBoundary === 'function' ? opts.onBoundary(kind, info || {}) !== false : false);

  const api = {
    get editor() { return editor; },
    get view() { return editor.view; },
    get dom() { return editor.view.dom; },
    get isFocused() { return Boolean(editor && editor.isFocused); },
  };

  // ---------------------------------------------------------- selection in plain offsets
  const offsetAt = (pos) => offsetOfPos(block(), pos, BLOCK_START);
  const posAt = (offset) => Math.min(posOfOffset(block(), offset, BLOCK_START), editor.state.doc.content.size - 1);
  const range = () => {
    const { from, to } = editor.state.selection;
    return { from: offsetAt(from), to: offsetAt(to) };
  };
  const coords = () => {
    try { return editor.view.coordsAtPos(editor.state.selection.head); } catch (_) { return null; }
  };

  function handle(name) {
    const { selection } = editor.state;
    const length = convert.plainText(api.getNode()).length;
    const r = range();
    const c = coords();
    const x = c ? (c.left + c.right) / 2 : 0;
    switch (name) {
      case 'Enter': return boundary('split', { node: api.getNode(), from: r.from, to: r.to });
      case 'Backspace': return selection.empty && r.from === 0 ? boundary('mergeBackward', {}) : false;
      case 'Delete': return selection.empty && r.from === length ? boundary('mergeForward', {}) : false;
      case 'ArrowUp': return selection.empty && editor.view.endOfTextblock('up') ? boundary('up', { x }) : false;
      case 'ArrowDown': return selection.empty && editor.view.endOfTextblock('down') ? boundary('down', { x }) : false;
      case 'ArrowRight':
      case 'ArrowLeft': {
        if (!selection.empty) return false;
        const back = (name === 'ArrowRight') === rtl(); // RTL: the right arrow goes back in the text
        if (back && r.from === 0) return boundary('prev', { x });
        if (!back && r.from === length) return boundary('next', { x });
        return false;
      }
      case 'Escape': return boundary('escape', {});
      case 'undo': return boundary('undo', {});
      case 'redo': return boundary('redo', {});
      case 'save': return boundary('save', {});
      case 'separator': return boundary('separator', {});
      case 'footnote': { const note = api.insertFootnote(); if (note && opts.onFootnote) opts.onFootnote(note); return true; }
      // D99: ⌘↩ a page break at the caret (the page cuts the block there); ⌘⇧L / E / R / J the alignment by side
      case 'pageBreak': return boundary('pageBreak', { node: api.getNode(), from: r.from, to: r.to });
      case 'alignLeft': api.setAlign(rtl() ? 'end' : 'start'); return true;
      case 'alignRight': api.setAlign(rtl() ? 'start' : 'end'); return true;
      case 'alignCenter': api.setAlign('center'); return true;
      case 'alignJustify': api.setAlign('justify'); return true;
      default: return false;
    }
  }

  // Several paragraphs pasted: the page makes blocks of them; one paragraph goes in as usual.
  function handlePaste(_view, _event, slice) {
    const blocks = [];
    slice.content.forEach((node) => { if (node.isTextblock) blocks.push(node); });
    if (blocks.length < 2) return false;
    const json = blocks.map((node) => convert.fromEditor({ type: 'doc', content: [node.toJSON()] })[0]).filter(Boolean);
    const r = range();
    return boundary('paste', { node: api.getNode(), from: r.from, to: r.to, blocks: json });
  }

  function build(node, offset) {
    ready = false;
    editor = new Editor({
      element,
      extensions: blockExtensions({ handle, numberOf: (id) => (numbers ? numbers(id) : '') }),
      content: { type: 'doc', content: convert.toEditor([node]).content.slice(0, 1) },
      editable: opts.editable !== false,
      injectCSS: false,
      editorProps: {
        attributes: { class: 'ed-doc ed-block', dir: 'rtl', lang: 'ar', spellcheck: 'false', role: 'textbox', 'aria-multiline': 'true', 'aria-label': 'الفقرة' },
        handlePaste,
        handleClickOn(view, _pos, clicked, nodePos) {
          if (clicked.type.name !== 'footnote') return false;
          if (opts.onFootnote) opts.onFootnote({ id: clicked.attrs.id, dom: view.nodeDOM(nodePos) });
          return true;
        },
        handleClick(view, pos, event) {
          const target = event && event.target;
          const markEl = target && typeof target.closest === 'function' ? target.closest('mark.ed-uncertain') : null;
          if (!markEl || !opts.onUncertain) return false;
          const found = api.uncertainAt(pos);
          if (found) opts.onUncertain({ ...found, dom: markEl });
          return false;
        },
      },
      onUpdate: ({ transaction }) => { if (ready && !transaction.getMeta('preventUpdate')) fire('change'); },
      onSelectionUpdate: () => { if (ready) fire('selection'); },
      onFocus: () => fire('focus'),
      onBlur: () => fire('blur'),
    });
    ready = true;
    api.setOffset(offset || 0);
  }

  Object.assign(api, {
    // ---------------------------------------------------------- content
    // The block as the server saves it (null attrs dropped).
    getNode() { return convert.fromEditor(editor.getJSON())[0] || null; },
    // Another block (or the same one again after an undo): the editor is rebuilt in the same element.
    setNode(node, offset) {
      const focused = api.isFocused;
      if (editor) editor.destroy();
      build(node, offset);
      if (focused) api.focus();
      return api;
    },
    style() { return styleOfNode(block()); },
    text() { return convert.plainText(api.getNode()); },
    offset() { return range().to; },
    range,
    setOffset(offset, to) {
      const doc = editor.state.doc;
      const a = posAt(offset);
      const b = to === undefined ? a : posAt(to);
      editor.view.dispatch(editor.state.tr.setSelection(TextSelection.create(doc, a, b)).setMeta('preventUpdate', true));
      return api;
    },
    focus() { editor.commands.focus(null, { scrollIntoView: false }); return api; },
    blur() { editor.commands.blur(); return api; },
    destroy() { ready = false; if (editor) editor.destroy(); editor = null; },

    // ---------------------------------------------------------- geometry (viewport pixels)
    // The top of the line box holding `offset`, from the editor's top edge (the page anchors the paragraph
    // there). The caret's own rect starts below its line box by the half-leading (about 3 pt at 13 pt on a
    // 22 pt line), so the line index is taken from the caret's middle and turned back into a line-box top.
    lineTop(offset) {
      try {
        const dom = editor.view.dom;
        const top = dom.getBoundingClientRect().top;
        const c = editor.view.coordsAtPos(posAt(offset));
        const lh = typeof getComputedStyle === 'function' ? parseFloat(getComputedStyle(dom).lineHeight) || 0 : 0;
        if (!(lh > 0)) return c.top - top;
        return Math.max(0, Math.floor(((c.top + c.bottom) / 2 - top) / lh)) * lh;
      } catch (_) { return 0; }
    },
    caretRect() { return coords(); },
    // The caret on the first or the last line, as near `x` as the line allows (↑ / ↓ from a neighbour).
    placeAtX(x, which) {
      const box = editor.view.dom.getBoundingClientRect();
      const first = editor.view.coordsAtPos(BLOCK_START);
      const last = editor.view.coordsAtPos(editor.state.doc.content.size - 1);
      const y = which === 'last' ? (last.top + last.bottom) / 2 : (first.top + first.bottom) / 2;
      const hit = editor.view.posAtCoords({ left: Math.min(Math.max(x, box.left + 1), box.right - 1), top: y });
      if (hit) editor.view.dispatch(editor.state.tr.setSelection(TextSelection.near(editor.state.doc.resolve(hit.pos))).setMeta('preventUpdate', true));
      return api;
    },

    // ---------------------------------------------------------- marks and styles
    toggleBold() { return editor.chain().focus().toggleMark('bold').run(); },
    toggleItalic() { return editor.chain().focus().toggleMark('italic').run(); },
    isBold() { return editor.isActive('bold'); },
    isItalic() { return editor.isActive('italic'); },
    setStyle(key) { return editor.chain().focus().setBlockStyle(key).run(); },
    // «ابدأ صفحة جديدة» / «مع التالية» on the block (null clears)
    setAttrs(patch) {
      const node = block();
      const attrs = { ...node.attrs };
      Object.entries(patch || {}).forEach(([k, v]) => { attrs[k] = v === undefined ? null : v; });
      editor.view.dispatch(editor.state.tr.setNodeMarkup(0, undefined, attrs, node.marks));
      return api;
    },
    // D99: the block's text options (every name, null when the style's own) and setting them (null, the
    // default value or an unknown one clears an option); a heading or a paragraph only
    textAttrs() { return convert.textAttrsOf(block().attrs); },
    setTextAttrs(patch) {
      const node = block();
      if (node.type.name !== 'paragraph' && node.type.name !== 'heading') return false;
      const clean = {};
      Object.entries(patch || {}).forEach(([k, v]) => {
        if (!convert.TEXT_ATTRS.includes(k)) return;
        const value = convert.textAttr(k, v);
        const plain = value === null || (k === 'indent' && value === 0) || ((k === 'spaceBefore' || k === 'spaceAfter') && value === 0) || (k === 'firstLine' && value === true) || (k === 'dir' && value === 'rtl');
        clean[k] = plain ? null : value;
      });
      api.setAttrs(clean);
      return true;
    },
    // the alignment chosen again clears it (the style's own), as Word's buttons toggle
    setAlign(value) { return api.setTextAttrs({ align: block().attrs.align === value ? null : value }); },
    // the printed numbers of the calls changed (a new layout of the page)
    setNumbers(numberOf) {
      numbers = typeof numberOf === 'function' ? numberOf : null;
      editor.view.dispatch(editor.state.tr.setMeta(NUMBERS_META, true).setMeta('preventUpdate', true).setMeta('addToHistory', false));
      return api;
    },
    togglePageMarks(on) { if (editor.view.dom.classList) editor.view.dom.classList.toggle('hide-marks', !on); return api; },

    // ---------------------------------------------------------- footnotes
    insertFootnote() {
      const { selection } = editor.state;
      const node = block();
      const id = convert.newId('ne');
      const note = editor.schema.nodes.footnote.create({
        id,
        number: null,
        marker: '',
        sourcePage: Array.isArray(node.attrs.sourcePages) && node.attrs.sourcePages.length ? node.attrs.sourcePages[0] : null,
        sourceLineIds: [],
        orphan: false,
      });
      if (!selection.$from.parent.isTextblock) return null;
      const tr = editor.state.tr.replaceSelectionWith(note, false);
      const pos = tr.selection.from - note.nodeSize;
      tr.setSelection(TextSelection.create(tr.doc, pos + note.nodeSize));
      editor.view.dispatch(tr);
      return { id, dom: editor.view.nodeDOM(pos) };
    },
    noteAt(id) {
      let found = null;
      editor.state.doc.descendants((node, pos) => {
        if (found) return false;
        if (node.type.name === 'footnote') { if (node.attrs.id === id) found = { node, pos }; return false; }
        return true;
      });
      if (!found) return null;
      return { id, content: found.node.content.toJSON() || [], text: found.node.textContent, sourcePage: found.node.attrs.sourcePage, orphan: Boolean(found.node.attrs.orphan), dom: editor.view.nodeDOM(found.pos) };
    },
    setNoteContent(id, content) {
      let found = null;
      editor.state.doc.descendants((node, pos) => {
        if (found) return false;
        if (node.type.name === 'footnote') { if (node.attrs.id === id) found = { node, pos }; return false; }
        return true;
      });
      if (!found) return false;
      const fragment = Fragment.fromJSON(editor.schema, Array.isArray(content) ? content : []);
      if (found.node.content.eq(fragment)) return false;
      editor.view.dispatch(editor.state.tr.replaceWith(found.pos + 1, found.pos + 1 + found.node.content.size, fragment));
      return true;
    },
    deleteNote(id) {
      let found = null;
      editor.state.doc.descendants((node, pos) => {
        if (found) return false;
        if (node.type.name === 'footnote') { if (node.attrs.id === id) found = { node, pos }; return false; }
        return true;
      });
      if (!found) return false;
      const tr = editor.state.tr.delete(found.pos, found.pos + found.node.nodeSize);
      tr.setSelection(TextSelection.near(tr.doc.resolve(Math.min(found.pos, tr.doc.content.size - 1))));
      editor.view.dispatch(tr);
      return true;
    },

    // ---------------------------------------------------------- uncertain words
    uncertainAt(pos) {
      const type = editor.schema.marks.uncertain;
      const doc = editor.state.doc;
      const r = getMarkRange(doc.resolve(Math.max(0, Math.min(pos, doc.content.size))), type);
      if (!r) return null;
      return { from: r.from, to: r.to, start: offsetAt(r.from), end: offsetAt(r.to), text: doc.textBetween(r.from, r.to) };
    },
    acceptUncertain(from, to) { editor.view.dispatch(editor.state.tr.removeMark(from, to, editor.schema.marks.uncertain)); return true; },
    replaceRange(from, to, text) {
      const marks = withoutUncertain(editor.state.doc.resolve(from).marks(), editor.schema);
      const tr = text ? editor.state.tr.replaceWith(from, to, editor.schema.text(text, marks)) : editor.state.tr.delete(from, to);
      editor.view.dispatch(tr);
      return true;
    },
    // the same by plain offsets (find & replace in the open paragraph)
    replaceOffsets(start, end, text) { return api.replaceRange(posAt(start), posAt(end), text); },

    // ---------------------------------------------------------- find highlights (plain offsets)
    setFind(ranges) {
      const list = (Array.isArray(ranges) ? ranges : []).map((r) => ({ from: posAt(r.start), to: posAt(r.end), block: null, note: null }));
      const current = (Array.isArray(ranges) ? ranges : []).findIndex((r) => r.current);
      editor.view.dispatch(editor.state.tr.setMeta(findKey, { ranges: list, current }).setMeta('preventUpdate', true));
      return api;
    },
    // the block's markup as the browser lays it out (a static copy for a page not laid out again yet)
    html() {
      const serializer = DOMSerializer.fromSchema(editor.schema);
      const host = element.ownerDocument.createElement('div');
      host.appendChild(serializer.serializeFragment(editor.state.doc.content));
      return host.innerHTML;
    },
  });

  build(opts.node, opts.offset);
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

export { posOfOffset, offsetOfPos };
export const {
  STYLES, STYLE_LABELS, PARAGRAPH_STYLES, DEFAULT_FIND, toEditor, fromEditor, newId, fold, foldQuery, segmentMatches, findMatches,
  wordCount, blockText, formatCount, pageRange, stepIndex, escapeHtml, styleOf,
  OBJECT, BREAK, inlineText, plainText, normalizeContent, sliceContent, splitNode, mergeNodes, insertBlocks, locate, flatBlocks,
  replaceBlock, setBlockAttrs, findNote, noteOwner, noteIds, pageMarks, findPlain, replacePlain, unmarkPlain, editContent,
  replaceInBlock, nodeHtml,
  // D99: the text options, empty lines, page breaks and blank pages
  ALIGNS, SIZES, SPACES, INDENT_MAX, TEXT_ATTRS, MAX_EMPTY_RUN, textAttr, textAttrsOf, setTextAttrs, textAttrsHtml,
  isEmptyBlock, isBlankPage, blankPage, emptyRunAt, pageBreakAt,
} = convert;
