// The books home (templates/books/list.html, books/shelf.py): search, filter, sort and view of the shelf, all in
// the page (the server renders every book; each card's data-* carry what is searched and sorted).
//   window.NassakhLibrary.searchKey(text)   Arabic text as the search compares it: no marks or tatweel, one alef,
//                                           ي for ى, ه for ة, و/ي for ؤ/ئ, Western digits, lower-case Latin
//   window.NassakhLibrary.arrange(items, s) the shelf for a state {q, stage, sort}: {order, visible}
//   Alpine.data('bookShelf', {view, sort})  the page: «/» focuses the search, Esc clears it; a filter chip shows
//                                           the books at one step; the view (shelf / list) and the sort are kept
//                                           per browser in a cookie (`nassakh_shelf=<view>:<sort>`), which the
//                                           server reads to draw the page that way at once (books/shelf.py
//                                           `prefs`); one «…» menu open at a time, closed by a click outside or Esc.
// The cards are moved in the DOM (not reordered with CSS `order`), so the keyboard follows the order shown; they
// hold no Alpine of their own, the «…» menus are <details>. Pure helpers first: the Node tests load this file.
(function () {
  'use strict';

  const root = typeof window !== 'undefined' ? window : globalThis;
  // harakat, superscript alef, Quranic annotation marks, tatweel
  const MARKS = /[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06dc\u06df-\u06e8\u06ea-\u06ed\u0640]/g;
  const DIGITS = /[\u0660-\u0669\u06f0-\u06f9]/g;

  function searchKey(text) {
    return String(text || '')
      .replace(MARKS, '')
      .replace(/[\u0622\u0623\u0625\u0671]/g, '\u0627') // آ أ إ ٱ → ا
      .replace(/\u0649/g, '\u064a') // ى → ي
      .replace(/\u0629/g, '\u0647') // ة → ه
      .replace(/\u0624/g, '\u0648') // ؤ → و
      .replace(/\u0626/g, '\u064a') // ئ → ي
      .replace(DIGITS, (d) => String(d.charCodeAt(0) >= 0x06f0 ? d.charCodeAt(0) - 0x06f0 : d.charCodeAt(0) - 0x0660))
      .toLowerCase()
      .replace(/\s+/g, ' ')
      .trim();
  }

  // Every word of the query appears in the key (in any order).
  function matches(key, query) {
    const words = searchKey(query).split(' ').filter(Boolean);
    return words.every((word) => key.includes(word));
  }

  const SORTS = {
    activity: (a, b) => b.activity - a.activity,
    progress: (a, b) => b.rank - a.rank || b.activity - a.activity,
    title: (a, b) => a.title.localeCompare(b.title, 'ar'),
    added: (a, b) => b.created - a.created,
  };

  // items: [{key, title, stage, attention, activity, created, rank}]; stage 'all', a step key, 'complete' or
  // 'attention' (any step needing it).
  function atStage(item, stage) {
    if (!stage || stage === 'all') return true;
    return stage === 'attention' ? item.attention : item.stage === stage;
  }

  function arrange(items, state) {
    const order = items.slice().sort(SORTS[state.sort] || SORTS.activity);
    const visible = new Set(order.filter((item) => atStage(item, state.stage) && (!state.q || matches(item.key, state.q))));
    return { order, visible };
  }

  const VIEWS = ['grid', 'list'];
  const PREFS_COOKIE = 'nassakh_shelf';
  function savePrefs(view, sort) {
    try {
      document.cookie = `${PREFS_COOKIE}=${view}:${sort}; path=/; max-age=31536000; SameSite=Lax`;
    } catch (_) { /* cookies blocked: not kept */ }
  }

  root.NassakhLibrary = { searchKey, matches, arrange, SORTS };

  if (typeof document === 'undefined' || typeof document.addEventListener !== 'function') return;
  document.addEventListener('alpine:init', () => {
    Alpine.data('bookShelf', (cfg = {}) => ({
      q: '',
      stage: 'all',
      sort: cfg.sort in SORTS ? cfg.sort : 'activity',
      view: VIEWS.includes(cfg.view) ? cfg.view : 'grid',
      shown: 0,
      total: 0,
      items: [],

      init() {
        this.items = Array.from(this.$refs.shelf.children).map((el) => ({
          el,
          key: searchKey(`${el.dataset.title || ''} ${el.dataset.author || ''}`),
          title: el.dataset.title || '',
          stage: el.dataset.stage,
          attention: el.dataset.attention === '1',
          activity: Number(el.dataset.activity) || 0,
          created: Number(el.dataset.created) || 0,
          rank: Number(el.dataset.rank) || 0,
        }));
        this.total = this.items.length;
        this.$watch('q', () => this.apply());
        this.$watch('stage', () => this.apply());
        this.$watch('sort', () => { savePrefs(this.view, this.sort); this.apply(); });
        this.$watch('view', () => savePrefs(this.view, this.sort));
        // one «…» open at a time
        this.$refs.shelf.addEventListener('toggle', (e) => {
          if (e.target.open) this.closeMenus(null, e.target);
        }, true);
        this.apply();
      },

      apply() {
        const { order, visible } = arrange(this.items, this);
        const shelf = this.$refs.shelf;
        const current = Array.from(shelf.children);
        const moved = order.some((item, i) => current[i] !== item.el);
        order.forEach((item) => {
          item.el.hidden = !visible.has(item);
          if (moved) shelf.appendChild(item.el);
        });
        this.shown = visible.size;
      },

      reset() {
        this.q = '';
        this.stage = 'all';
        this.$nextTick(() => this.$refs.search && this.$refs.search.focus());
      },

      // A click outside an open «…» closes it (`event` null: close all but `keep`).
      closeMenus(event, keep) {
        this.$root.querySelectorAll('details.lb-more[open]').forEach((menu) => {
          if (menu === keep || (event && menu.contains(event.target))) return;
          menu.open = false;
        });
      },

      onKey(e) {
        const keys = root.NassakhKeys;
        const typing = e.target && (e.target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName));
        if (e.key === 'Escape') {
          const open = this.$root.querySelector('details.lb-more[open]');
          if (open) {
            open.open = false;
            open.querySelector('summary').focus();
          }
          return;
        }
        if (!typing && keys && keys.is(e, '/')) {
          e.preventDefault();
          this.$refs.search.focus();
          this.$refs.search.select();
        }
      },
    }));
  });
})();
