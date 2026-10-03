// The organisation's format templates on the book page (D98): «التنسيق» → «قوالب المؤسسة»
// (templates/accounts/_book_templates.html).
//   bookTemplates(cfg) – the section's component: the organisation's templates with what each changes in this
//                        book (api:book_templates), one chosen to see its changes, «تطبيق القالب…» behind a
//                        confirmation (api:book_template_apply: the stylesheet's own save; the answer is taken
//                        as a stylesheet save's — the panel's values, the pages laid out again, the cover), and
//                        «حفظ التنسيق قالبًا…» (a new template from this book). The book page itself is
//                        Alpine.store('bookPage').view (static/src/js/book/page.js).
// cfg: {list, apply, update, manage} (apply/update with __tid__ for the template's id).
(function () {
  'use strict';

  const root = typeof window !== 'undefined' ? window : globalThis;
  const OPEN_KEY = 'nassakh.book.templates';
  const NAME_MAX = 120;

  const readOpen = () => { try { return root.localStorage.getItem(OPEN_KEY) === '1'; } catch (_) { return false; } };
  const writeOpen = (on) => { try { root.localStorage.setItem(OPEN_KEY, on ? '1' : '0'); } catch (_) { /* private mode */ } };
  const fill = (url, id) => String(url || '').replace('__tid__', String(id));

  async function call(url, options) {
    const NS = root.NassakhBook;
    if (NS && NS.util && typeof NS.util.api === 'function') return NS.util.api(url, options);
    return { ok: false, status: 0, data: null, message: 'تعذّر الاتصال بالخادم.' };
  }

  function bookTemplates(cfg = {}) {
    return {
      open: readOpen(),
      loading: false,
      loaded: false,
      error: '',
      list: [],
      canSave: false,
      canManage: false,
      selectedId: null,
      confirming: false,
      busy: false,
      saving: { open: false, name: '', description: '', error: '', busy: false },
      nameMax: NAME_MAX,

      init() { if (this.open) this.load(); },
      view() { return typeof Alpine !== 'undefined' && Alpine.store('bookPage') ? Alpine.store('bookPage').view : null; },
      toggle() {
        this.open = !this.open;
        writeOpen(this.open);
        if (this.open) this.load();
      },
      get headMeta() {
        if (!this.loaded) return '';
        return this.list.length ? `${this.list.length}` : 'لا قوالب';
      },
      get selected() { return this.list.find((t) => t.id === this.selectedId) || null; },
      summaryOf(t) { return (t && Array.isArray(t.summary) ? t.summary : []).join(' · '); },

      async load() {
        if (!cfg.list || this.loading) return false;
        this.loading = true;
        this.error = '';
        const r = await call(cfg.list);
        this.loading = false;
        if (!r.ok || !r.data) { this.error = r.message || 'تعذّر تحميل القوالب.'; return false; }
        this.list = Array.isArray(r.data.templates) ? r.data.templates : [];
        this.canSave = Boolean(r.data.can_save);
        this.canManage = Boolean(r.data.can_manage);
        this.loaded = true;
        if (this.selectedId !== null && !this.selected) this.selectedId = null;
        return true;
      },
      select(id) { this.selectedId = this.selectedId === id ? null : id; },

      askApply() { if (this.selected && !this.selected.same && this.canSave) this.confirming = true; },
      closeApply() { this.confirming = false; },
      async apply() {
        const t = this.selected;
        const v = this.view();
        if (!t || this.busy) return false;
        this.busy = true;
        if (v && typeof v.flushSheet === 'function') await v.flushSheet(); // the panel's waiting changes first
        const r = await call(fill(cfg.apply, t.id), { method: 'POST', body: { chapter: v && v.focusChapter ? v.focusChapter : null } });
        this.busy = false;
        this.confirming = false;
        if (!r.ok || !r.data) { this.error = r.message || 'تعذّر تطبيق القالب.'; return false; }
        if (v) {
          // the answer of a stylesheet save (style.js flushSheet): the values, the pages laid out again, the cover
          if (typeof v.applyStylesheet === 'function') v.applyStylesheet(r.data);
          v.errors = {};
          if (r.data.preview && typeof v.applyPreview === 'function') v.applyPreview(r.data.preview, { quiet: true });
          if (v.focusChapter && typeof v.requestRelayout === 'function') v.requestRelayout(v.focusChapter);
          if (typeof v.pollNow === 'function') v.pollNow();
          if (typeof v.afterSheetSaved === 'function') v.afterSheetSaved({}, r.data);
        }
        const applied = r.data.applied || {};
        const skipped = Array.isArray(applied.skipped) ? applied.skipped : [];
        const tail = skipped.length ? ` (لم يُطبَّق: ${skipped.map((s) => s.label).join('، ')})` : '';
        if (root.Nassakh && typeof root.Nassakh.toast === 'function') root.Nassakh.toast(`طُبّق القالب «${applied.name || t.name}»${tail}.`);
        await this.load();
        return true;
      },

      openSave() {
        this.saving = { open: true, name: '', description: '', error: '', busy: false };
        if (this.$nextTick) this.$nextTick(() => { if (this.$refs && this.$refs.templateName) this.$refs.templateName.focus(); });
      },
      closeSave() { this.saving = Object.assign({}, this.saving, { open: false, error: '' }); },
      async save() {
        const name = String(this.saving.name || '').trim();
        if (!name) { this.saving.error = 'اكتب اسمًا للقالب.'; return false; }
        const v = this.view();
        if (v && typeof v.flushSheet === 'function') await v.flushSheet(); // the template takes the saved values
        this.saving.busy = true;
        const r = await call(cfg.list, { method: 'POST', body: { name, description: this.saving.description || '' } });
        this.saving.busy = false;
        if (!r.ok || !r.data) { this.saving.error = r.message || 'تعذّر حفظ القالب.'; return false; }
        this.saving = { open: false, name: '', description: '', error: '', busy: false };
        if (root.Nassakh && typeof root.Nassakh.toast === 'function') root.Nassakh.toast(`حُفظ تنسيق الكتاب قالبًا: «${r.data.name}».`);
        await this.load();
        this.selectedId = r.data.id;
        return true;
      },
      // an admin takes this book's format into the chosen template
      async updateFromBook() {
        const t = this.selected;
        if (!t || !this.canManage || this.busy || !cfg.update) return false;
        const v = this.view();
        if (v && typeof v.flushSheet === 'function') await v.flushSheet();
        this.busy = true;
        const r = await call(fill(cfg.update, t.id), { method: 'POST', body: {} });
        this.busy = false;
        if (!r.ok) { this.error = r.message || 'تعذّر تحديث القالب.'; return false; }
        if (root.Nassakh && typeof root.Nassakh.toast === 'function') root.Nassakh.toast(`حُدّث القالب «${t.name}» من تنسيق هذا الكتاب.`);
        await this.load();
        return true;
      },
    };
  }

  function register() {
    if (typeof Alpine === 'undefined') return;
    Alpine.data('bookTemplates', bookTemplates);
  }

  root.NassakhOrg = { bookTemplates, register };
  if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') document.addEventListener('alpine:init', register);
})();
