// Review screen (Phase 3, PHASE3_SPEC §4).
//   reviewScreen(config)  – the whole screen: scan viewer, lines column, popover, autosave, filmstrip
//   reviewBar             – the top-bar controls (rendered in base.html's header_actions, outside the
//                           screen's root) reading Alpine.store('review').bar and calling .act(name, arg)
// Config = review_payload (docs/PHASE3_SPEC.md §4), read from <script id="review-config">.
// Every action is optimistic: the UI changes at once, the POST follows, a failure rolls back and the
// save chip offers a retry. Undo, approve and page swaps re-render from the payload the server returns.
// Motion: chrome 120–200 ms; resolution moments ≤ 400 ms; prefers-reduced-motion drops glides and waves.
(function () {
  'use strict';

  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const hasDOM = typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function'
    && typeof document !== 'undefined' && typeof document.createElement === 'function';
  const csrfToken = () => {
    const meta = hasDOM ? document.querySelector('meta[name="csrf-token"]') : null;
    return (meta && meta.content) || '';
  };
  const reducedMotion = () => {
    try { return Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); } catch (_) { return false; }
  };
  const storage = {
    get(key, fallback) { try { const v = window.localStorage.getItem(key); return v == null ? fallback : v; } catch (_) { return fallback; } },
    set(key, value) { try { window.localStorage.setItem(key, value); } catch (_) { /* private mode */ } },
  };
  const SWAP_KEY = 'nassakh.review.swapped';
  const FIT_KEY = 'nassakh.review.fit'; // 'height' (default) | 'width'
  const ZOOM_MIN = 1;
  const ZOOM_MAX = 6;
  const SAVED_MS = 1600;
  const FLASH_MS = 700;
  const POLL_MS = 3000;
  const UNDO_TOAST_MS = 8000;
  // word popover placement (DESIGN.md: open below, flip above when needed, keep 8 px from the edges)
  const POP_EDGE = 8;
  const POP_GAP = 8;
  const POP_W = 272;
  const POP_H_GUESS = 220;
  const MORE_W = 248; // the «إجراءات أخرى» submenu
  const MORE_CLOSE_MS = 180; // hover intent: the submenu survives the gap between the two panels
  const TAP_SLOP = 6; // px a press may travel on the scan and still be a tap (a trackpad click wobbles)
  // «نوع السطر» (D32, D74): the effective choice; `set_line_role` maps it onto the stored role (on a footnote-
  // region line «حاشية» stores `body` and «محتوى» stores `main`), so the menu never shows a stored value.
  const ROLES = [
    { value: 'body', label: 'محتوى' },
    { value: 'heading', label: 'عنوان رئيسي' },
    { value: 'subheading', label: 'عنوان فرعي' },
    { value: 'verse', label: 'شعر' },
    { value: 'footnote', label: 'حاشية' },
  ];
  const MARKS = ['علامة واحدة', 'علامتان', 'علامات', 'علامة'];
  const PAGES = ['صفحة واحدة', 'صفحتان', 'صفحات', 'صفحة'];
  const PAGES_IN = ['صفحة واحدة', 'صفحتين', 'صفحات', 'صفحة']; // after «في»: «في صفحتين»
  const PLACES = ['موضع واحد', 'موضعان', 'مواضع', 'موضعًا'];
  const PLACES_OBJ = ['موضع واحد', 'موضعين', 'مواضع', 'موضعًا']; // as an object: «تصحيح موضعين»
  // D79 (§5.7): a word's core (edge punctuation aside), as the book is searched for it
  const core = (word) => String(word || '').replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, '');
  const CROP_H = 30; // the height of an occurrence's crop in the fix sheet (px)
  // The end of review (§5.3): the next step, as `next_step.state` names it. The server sends the sentences with the
  // numbers in them (`heading`, `summary`, `title`, `text`, `button`, `url`); these stand in for a field it leaves out.
  const NEXT_STEPS = {
    assemble: { step: 'تجميع المخطوطة', text: 'تُجمَع الصفحات في نص واحد متّصل: فصول وفقرات وحواشٍ، تتحقّق من بنيته قبل الكتاب.', button: 'تجميع المخطوطة…' },
    reassemble: { step: 'إعادة التجميع', text: 'تغيّر نص صفحات بعد التجميع، ولم يُحرَّر الكتاب بعد؛ فإعادة التجميع لا تُضيّع شيئًا.', button: 'إعادة التجميع' },
    changes: { step: 'أخذ التغييرات إلى الكتاب', text: 'غيّرت المراجعة نص صفحات من الكتاب المحرَّر؛ تُؤخذ فقراتها وحدها.', button: 'عرض التغييرات في الكتاب' },
    book: { step: 'الكتاب', text: 'المخطوطة محدَّثة؛ نسّق الكتاب وحرّره على صفحاته.', button: 'فتح الكتاب' },
    processing: { heading: 'رُوجعت كل الصفحات الجاهزة', step: 'المعالجة', text: 'لا صفحات بانتظار المراجعة الآن؛ ما زالت صفحات قيد المعالجة.', button: 'العودة إلى المعالجة' },
  };
  const OPEN_WORDS = ['كلمة غير محسومة', 'كلمتان غير محسومتين', 'كلمات غير محسومة', 'كلمة غير محسومة'];
  // D73: how a page was read, when it was not read by both models (`page.reading.readers`): the review header's
  // pill and banner, and the filmstrip's half-disc.
  const READERS = {
    one: {
      label: 'قراءة واحدة',
      tone: 'warning',
      banner: 'قرأ هذه الصفحةَ نموذجٌ واحد، فالعلامات فيها أقل من الحقيقة. قابِل كل سطر بالصورة.',
    },
    tesseract: {
      label: 'نص Tesseract وحده',
      tone: 'danger',
      banner: 'تعذّرت قراءة هذه الصفحة بالنموذجين، ونصّها من Tesseract وحده. قابِل كل سطر بالصورة.',
    },
  };
  const isDigits = (word) => /^[0-9٠-٩۰-۹]+$/.test(word);

  // «صفحة واحدة», «صفحتان», «5 صفحات», «214 صفحة» (= assembly.render.ar_count), Western digits.
  function arCount(n, forms) {
    const k = Number(n) || 0;
    if (k === 1) return forms[0];
    if (k === 2) return forms[1];
    const units = k % 100;
    return `${k} ${units >= 3 && units <= 10 ? forms[2] : forms[3]}`;
  }

  // The lenient comparison of core.arabic.normalize(…, 'lenient'): no tashkeel or tatweel, folded letters,
  // Western digits, no punctuation. Only used to say which reading Tesseract backs.
  const TASHKEEL = /[ؐ-ًؚ-ٰٟۖ-ۭـ]/g;
  function lenient(text) {
    let t = String(text || '');
    try { t = t.normalize('NFKC'); } catch (_) { /* old engine */ }
    t = t.replace(TASHKEEL, '').replace(/[أإآٱ]/g, 'ا').replace(/ى/g, 'ي').replace(/ة/g, 'ه').replace(/ؤ/g, 'و').replace(/ئ/g, 'ي');
    t = t.replace(/[٠-٩]/g, (d) => String(d.charCodeAt(0) - 0x0660)).replace(/[۰-۹]/g, (d) => String(d.charCodeAt(0) - 0x06f0));
    return t.replace(/[^\p{L}\p{N}]+/gu, ' ').toLowerCase().replace(/ +/g, ' ').trim();
  }

  // D71 `script`: the letters and symbols that made a token foreign (ocr/flags.py's classes), distinct, at most 3.
  const FOREIGN_LETTER = /[Ͱ-ϿЀ-ӿ֐-׿぀-ヿ一-鿿가-힯]/u;
  const STRANGE = new Set(['※', '★', '∩', '∧', '∨', '≡', '→', '©', '¢', '€', '¥', '^']);
  function foreignChars(text) {
    const chars = Array.from(String(text || ''));
    const arabic = chars.some((ch) => /[؀-ۿݐ-ݿࢠ-ࣿ]/.test(ch) && /\p{L}/u.test(ch));
    const letters = []; const symbols = [];
    chars.forEach((ch) => {
      const latinBeside = arabic && /[A-Za-zÀ-ɏ]/.test(ch); // Arabic and Latin letters in one token
      if ((FOREIGN_LETTER.test(ch) || latinBeside) && !letters.includes(ch)) letters.push(ch);
      else if (STRANGE.has(ch) && !symbols.includes(ch)) symbols.push(ch);
    });
    return { letters: letters.slice(0, 3), symbols: symbols.slice(0, 3) };
  }
  const quoted = (chars) => chars.map((ch) => `«${ch}»`).join('، ');

  // D74 `line_kind(role, region_kind)` (ocr.services): a footnote when the role says so, or when a body-role line
  // sits in a footnote region; every other role (heading, subheading, verse, main) is body text.
  function lineKind(role, regionKind) {
    if (role === 'footnote') return 'footnote';
    return (role || 'body') === 'body' && regionKind === 'footnote' ? 'footnote' : 'body';
  }
  // The role `set_line_role` stores for an effective choice (the optimistic step; the server's line follows).
  function storedRole(choice, regionKind) {
    if (regionKind === 'footnote') {
      if (choice === 'footnote') return 'body';
      if (choice === 'body') return 'main';
    }
    return choice;
  }
  // What the menu shows as chosen: headings and verse as they are, else the line's kind («حاشية» / «محتوى»).
  function effectiveRole(role, regionKind, kind) {
    if (role === 'heading' || role === 'subheading' || role === 'verse') return role;
    return (kind || lineKind(role, regionKind)) === 'footnote' ? 'footnote' : 'body';
  }

  // fetch wrapper: never throws; `{ ok, status, data, message }` with an Arabic message on failure.
  async function api(url, options) {
    const opts = options || {};
    const method = opts.method || 'GET';
    const headers = { Accept: 'application/json' };
    const init = { method, credentials: 'same-origin', headers, cache: 'no-store' };
    if (method !== 'GET') {
      headers['Content-Type'] = 'application/json';
      headers['X-CSRFToken'] = csrfToken();
      init.body = JSON.stringify(opts.body || {});
    }
    let response;
    try {
      response = await fetch(url, init);
    } catch (_) {
      return { ok: false, status: 0, data: null, message: 'انقطع الاتصال بالخادم. تحقّق من الشبكة ثم أعد المحاولة.' };
    }
    let data = null;
    try { data = await response.json(); } catch (_) { data = null; }
    let message = data && (data.message || data.detail);
    if (!message) {
      if (response.status === 401) message = 'انتهت الجلسة. سجّل الدخول من جديد.';
      else if (response.status === 403) message = 'لا تملك صلاحية المراجعة.';
      else message = 'تعذّر تنفيذ الطلب. حاول مرة أخرى.';
    }
    return { ok: response.ok, status: response.status, data, message };
  }

  // D70: every change saved here is announced to the book's other tabs on `BroadcastChannel('nassakh')` as
  // `{type: 'review', book, page}`: the book page refreshes its review drift, the manuscript its state. Where the
  // channel is missing, their focus and visibility checks cover it.
  const CHANNEL = 'nassakh';
  let channel = null;
  function announce(message) {
    try {
      if (!channel && typeof BroadcastChannel === 'function') {
        channel = new BroadcastChannel(CHANNEL);
        if (typeof channel.unref === 'function') channel.unref(); // Node (the tests): never hold the process open
      }
      if (channel) channel.postMessage(message);
    } catch (_) { /* no channel (an old browser, a sandbox) */ }
  }

  // D76: the stage bar (stages.js) hears that this book moved on (an approval, a fix everywhere)
  const stagesChanged = (book) => { try { if (window.NassakhStages) window.NassakhStages.changed(book); } catch (_) { /* no bar */ } };
  const fill = (template, id) => String(template || '').replace(/__(id|group)__/, String(id));
  const plainToken = (word) => ({ t: word, alt: null, tess: null, conf: 'high', digit: isDigits(word), bbox: null, res: 'typed' });
  const splitWords = (text) => String(text || '').replace(/\s+/g, ' ').trim().split(' ').filter(Boolean);
  const isBox = (b) => Array.isArray(b) && b.length === 4;
  const unionBox = (a, b) => {
    const boxes = [a, b].filter(isBox);
    if (!boxes.length) return null;
    return [Math.min(...boxes.map((x) => x[0])), Math.min(...boxes.map((x) => x[1])), Math.max(...boxes.map((x) => x[2])), Math.max(...boxes.map((x) => x[3]))];
  };

  // Keyboard map (PHASE3_SPEC §4, PHASE7_SPEC §3.15 D69), RTL: ArrowLeft = next page. Pure, so the tests can
  // exercise it. Keys are read through `NassakhKeys` (keys.js): letters by their place, so the Arabic layout's
  // «ش» on the A key is A; digits in any script; nothing while an IME composes.
  // ctx: inField (typing in an input), inPop (the target is inside the word menu), inFlow (Tab may be taken
  // over), focused (a word, a group of added words or a gap is focused and the page is editable), open (its
  // menu is open), optionKeys (digit hints of the focused word's readings), gap (the focus is a gap ▏), group
  // (the focused word belongs to an open group of added words, D72).
  // Two modes, decided by the word menu:
  //   word mode (the menu open on an editable word): 1–9 choose reading n, a digit beyond the readings and any
  //     other printable character (Latin or Arabic, ؟ and − too) start the correction with that character (on a
  //     gap: the words to insert; a group's menu has no correction);
  //   page mode (the menu closed): A approve, E edit the line, N the next page to review, G jump to a page, O
  //     the processed image or the original, V swap the sides, C copy the page's text, ? the sheet, + − 0 zoom,
  //     Space opens the focused word's menu, a digit still chooses a reading of a focused word; other letters do
  //     nothing, so no correction starts by accident. ⌥F in word mode: «تصحيح في كل الكتاب» (D79).
  // In both: ← → PageDown PageUp Home End turn pages, Enter accepts (the reading in the text, a group kept, a
  // gap's words inserted), Tab / ⇧Tab move through the words, groups and gaps, ⌥← / ⌥→ merge the focused word
  // with the next / previous one (D31, RTL: the next word is on the left), ⌫ deletes it (a gap: dismissed), ⌘Z
  // undoes, ⌘↵ approves the page from anywhere (the correction field of the word menu too).
  function keyAction(ev, ctx) {
    const K = window.NassakhKeys;
    if (K.composing(ev)) return null;
    const k = ev.key;
    const c = ctx || {};
    if (k === 'Escape') return 'close';
    if (k === 'Enter' && K.mod(ev) && !ev.altKey && !ev.shiftKey) return !c.inField || c.inPop ? 'approve' : null;
    if (c.inField) return null; // an input keeps its own keys, including the native ⌘Z of its draft
    if (K.mod(ev)) return K.letter(ev, 'mod') === 'z' ? 'undo' : null;
    if (k === 'Tab') return c.inFlow ? (ev.shiftKey ? 'prev' : 'next') : null;
    if (k === 'Enter') return ev.altKey ? 'insert' : (c.focused ? 'accept' : null);
    // ⌥F (§5.8, D79): the focused word everywhere in the book, from its menu (word mode)
    if (ev.altKey && c.focused && c.open && !c.gap && !c.group && K.letter(ev, 'alt') === 'f') return 'fixEverywhere';
    if (ev.altKey) {
      if (c.focused && !c.gap && k === 'ArrowLeft') return 'mergeNext';
      if (c.focused && !c.gap && k === 'ArrowRight') return 'mergePrev';
      return null;
    }
    if ((k === 'Backspace' || k === 'Delete') && c.focused) return c.gap ? 'dismissGap' : 'deleteWord';
    if (k === 'ArrowLeft' || k === 'PageDown') return 'nextPage';
    if (k === 'ArrowRight' || k === 'PageUp') return 'prevPage';
    if (k === 'Home') return 'firstPage';
    if (k === 'End') return 'lastPage';
    const n = K.digit(ev);
    const reading = n !== null && n >= 1 && c.focused && (c.optionKeys || []).includes(String(n));
    if (reading) return 'choose' + n;
    if (c.focused && c.open) {
      // word mode: what the key types starts the correction (a space never does); a group's menu has none
      const ch = K.printable(ev);
      return ch && ch.trim() && !c.group ? 'type' : null;
    }
    if ((k === ' ' || ev.code === 'Space') && !ev.shiftKey) return c.focused ? 'openWord' : null;
    if (K.is(ev, '?')) return 'sheet';
    if (K.plus(ev)) return 'zoomIn';
    if (K.minus(ev)) return 'zoomOut';
    if (n === 0) return 'zoomReset';
    const letter = K.letter(ev);
    if (letter === 'a') return 'approve';
    if (letter === 'e') return 'edit';
    if (letter === 'n') return 'nextReview';
    // §5.8: G the pager's jump field, O «المعالَجة / الأصل», V the sides swapped, C the page's text copied
    if (letter === 'g') return 'jump';
    if (letter === 'o') return 'toggleScan';
    if (letter === 'v') return 'swap';
    if (letter === 'c') return 'copy';
    return null;
  }

  window.NassakhReview = Object.assign(window.NassakhReview || {}, { keyAction, api, arCount, lenient, foreignChars, lineKind, storedRole, effectiveRole });

  document.addEventListener('alpine:init', () => {
    // Shared with the top bar: a plain snapshot (`bar`) and an action dispatcher (`act`).
    Alpine.store('review', { bar: null, act: null });

    Alpine.data('reviewBar', () => ({
      get b() { return this.$store.review.bar; },
      act(name, arg) { const fn = this.$store.review.act; if (fn) fn(name, arg); },
    }));

    Alpine.data('reviewScreen', (config) => ({
      // ---- payload
      page: {},
      book: {},
      image: {},
      regions: [],
      lines: [],
      counts: { low_total: 0, unresolved: 0, resolved: 0 },
      labels: {},
      nav: {},
      urls: {},
      canEdit: false,
      // ---- words
      focus: null,        // { lineId, index } focused word, or { lineId, index, gap } a focused gap ▏ (D72)
      hot: null,          // { lineId, index } hovered word, from either side
      hotLine: null,      // hovered line id
      flashing: {},       // "lineId-index" → true while the resolve animation plays
      pop: { open: false, style: '', typed: '', typing: false, above: false, more: false, moreSide: 'left', moreStyle: '' },
      roles: ROLES,
      // ---- lines
      edit: null,         // { lineId, text }
      insert: null,       // { afterId, text }
      menuFor: null,      // line id whose «…» menu is open
      range: { anchor: null, ids: [] }, // ⇧-click: the lines whose role changes together (D74)
      // ---- saving
      save: { state: 'idle', pending: 0, failed: [] }, // failed: [{ message, retry }] in order, until retried
      queue: Promise.resolve(),
      actionSeq: 0,       // counts requests; only the newest one may offer an undo toast
      gen: 0,             // page generation: a page swap bumps it so late responses of the old page are dropped
      undoToast: null,
      // ---- where review leads (D76, §5.3)
      after: null,        // the lines pane after an approval: {kind: 'detour', number, backUrl, hasNext} | {kind: 'end', …}
      arrived: false,     // this page came in by approval's auto-advance and nothing was saved on it yet (the N rule)
      jumping: false,     // G: the pager is a jump field
      nextStep: null,     // review_payload.next_step (approve's answer brings a fresher one)
      // «تصحيح في كل الكتاب» (D79, §5.7): the sheet, api:book_occurrences and api:fix_everywhere
      fix: { open: false, from: '', to: '', options: { fold_alef: true, whole_word: true, match_tashkeel: false }, loading: false, error: '', results: [], total: 0, pages: 0, truncated: false, picks: {}, cursor: 0, busy: false },
      liveMessage: '',
      // ---- page
      shown: 0,           // animated resolved counter
      dialog: { open: false, count: 0, words: 0, suggested: 0, gaps: 0 }, // suggested: open groups and gaps (D73)
      sheetOpen: false,
      stamp: false,
      approving: false,
      slide: '',          // '' | 'out' | 'in' | 'out-back' | 'in-back' (turning to an earlier page)
      loading: false,
      entering: false,
      pulsing: false,
      film: { items: [], state: 'idle' },
      // ---- scan viewer
      tab: 'display',
      swapped: false,
      zoom: { scale: 1, x: 0, y: 0 },
      fit: 'height',
      pane: { w: 0, h: 0 },
      gliding: false,
      drag: null,
      dragMoved: false,
      tapBox: null, // the scan box under the last pointerdown: its tap opens the word in the text
      pinch: null,
      pointers: null,
      reduced: false,
      decodeEl: null,
      provisional: [],    // a pending page's Tesseract lines (`provisional_lines`: words, box as ratios)
      readingLine: -1,    // the pending text's reading cursor (NassakhDecode onLine), mirrored on the scan
      readingLast: -1,    // the line it last stood on (the band fades out there between two passes)
      noiseRows: 10,      // rows of noise shown before Tesseract has read the page

      init() {
        this.reduced = reducedMotion();
        this.swapped = storage.get(SWAP_KEY, '0') === '1';
        this.fit = storage.get(FIT_KEY, 'height') === 'width' ? 'width' : 'height';
        this.apply(config);
        this.shown = this.counts.resolved;
        this.syncBar();
        if (!hasDOM) return;
        if (this.$watch) {
          ['counts', 'save', 'page', 'shown', 'loading', 'canEdit'].forEach((key) => this.$watch(key, () => this.syncBar()));
          // one toast at a time (DESIGN.md): a shared toast («تم النسخ», an error) takes the place of the undo offer
          this.$watch('$store.toast.visible', (visible) => { if (visible) this.dismissUndo(); });
        }
        this.tick(() => { this.measure(); this.observe(); this.enter(); this.mountDecode(); });
        setTimeout(() => this.loadFilm(), 60);
        if (this.isPending) this.schedulePoll();
      },

      destroy() {
        if (this.ro) this.ro.disconnect();
        clearTimeout(this.pollTimer);
        clearTimeout(this.saveTimer);
        clearTimeout(this.undoTimer);
        this.unmountDecode();
        try { const store = Alpine.store('review'); store.bar = null; store.act = null; } catch (_) { /* no store */ }
      },

      tick(fn) { if (hasDOM && this.$nextTick) this.$nextTick(fn); },

      // ------------------------------------------------------------ payload
      apply(data) {
        if (!data) return;
        if (data.page) this.page = data.page;
        if (data.book) this.book = data.book;
        if (data.image) this.image = data.image;
        if (data.regions) this.regions = data.regions;
        if (Array.isArray(data.provisional_lines)) this.provisional = data.provisional_lines;
        if (data.lines) this.lines = data.lines.slice().sort((a, b) => a.order - b.order);
        if (Array.isArray(data.gaps)) this.adoptGaps(data.gaps);
        if (data.labels) this.labels = data.labels;
        if (data.nav) this.nav = data.nav;
        if (data.urls) this.urls = data.urls;
        if (data.can_edit !== undefined) this.canEdit = Boolean(data.can_edit);
        if (data.next_step !== undefined) this.nextStep = data.next_step || null;
        if (data.counts) this.setCounts(data.counts); else this.recount();
        this.syncBar();
      },

      // `words`, `groups` and `gaps` split the open items (D73: the approve dialog names them); kept when given.
      setCounts(c) {
        const lowTotal = Number(c.low_total) || 0;
        const unresolved = Number(c.unresolved) || 0;
        const resolved = c.resolved != null ? Number(c.resolved) || 0 : Math.max(0, lowTotal - unresolved);
        this.counts = { low_total: lowTotal, unresolved, resolved };
        ['words', 'groups', 'gaps'].forEach((k) => { if (c[k] != null) this.counts[k] = Number(c[k]) || 0; });
        this.tickCounter();
        this.syncBar();
      },

      // Local recount from the lines (optimistic updates); the server's counts replace it afterwards. One item per
      // group of added words (D72, `count_unresolved`), one per gap; `n_low` stays the line's open words.
      recount() {
        const t = this.tally();
        this.lines.forEach((line) => { line.n_low = this.lineOpen(line); });
        this.setCounts({ low_total: t.low, unresolved: t.words + t.groups + t.gaps, words: t.words, groups: t.groups, gaps: t.gaps });
      },

      // A line's open items as `Line.n_low` counts them: its open words, one per open group on it, no gaps.
      lineOpen(line) {
        const groups = new Set();
        let open = 0;
        (line.tokens || []).forEach((tok) => {
          if (!this.isUnresolved(tok)) return;
          const g = this.groupOf(tok);
          if (g === null) open += 1; else groups.add(g);
        });
        return open + groups.size;
      },

      // The page's marks: `low` all of them (decided or not), then the open ones by kind.
      tally() {
        const groups = new Map(); // group → still open
        let low = 0; let words = 0; let gaps = 0;
        this.lines.forEach((line) => {
          (line.tokens || []).forEach((tok) => {
            if (tok.conf !== 'low') return;
            const g = this.groupOf(tok);
            if (g !== null) { groups.set(g, groups.get(g) || tok.res == null); return; }
            low += 1;
            if (tok.res == null) words += 1;
          });
          (line.gaps || []).forEach((gap) => { low += 1; if (this.gapOpen(gap)) gaps += 1; });
        });
        let open = 0;
        groups.forEach((isOpen) => { low += 1; if (isOpen) open += 1; });
        return { low, words, groups: open, gaps };
      },

      // API `counts` = { line_n_low, page_unresolved, page_low_total, book_unresolved_total }.
      applyApiCounts(c, line) {
        if (!c) return;
        if (line && c.line_n_low != null) line.n_low = c.line_n_low;
        this.setCounts({ low_total: c.page_low_total, unresolved: c.page_unresolved, words: c.page_words, groups: c.page_groups, gaps: c.page_gaps });
        if (c.book_unresolved_total != null) this.book.unresolved_total = c.book_unresolved_total;
      },

      // The line's server version (`v` in its payload) as it is now; sent with whole-line actions.
      versionOf(line) {
        const current = line ? this.lineById(line.id) : null;
        const v = (current && current.v) || (line && line.v);
        return v === undefined || v === null ? undefined : v;
      },
      // A line from an answer. Its gaps come with it when the answer carries them (`line.gaps`), else the line
      // keeps the ones it has (shifted by the optimistic step; `adoptAnswerGaps` may bring the page's list).
      replaceLine(line) {
        const i = this.lines.findIndex((l) => l.id === line.id);
        if (!Array.isArray(line.gaps)) line.gaps = i >= 0 ? this.lines[i].gaps || [] : [];
        if (i >= 0) this.lines.splice(i, 1, line); else { this.lines.push(line); this.lines.sort((a, b) => a.order - b.order); }
      },

      syncBar() {
        try {
          const store = Alpine.store('review');
          if (!store) return;
          store.bar = {
            number: this.page.number,
            total: this.book.total_pages,
            shown: this.shown,
            resolved: this.counts.resolved,
            lowTotal: this.counts.low_total,
            unresolved: this.counts.unresolved,
            pct: this.pct,
            save: this.save.state,
            failed: this.save.failed.length,
            reviewed: this.isReviewed,
            canEdit: this.canEdit,
            canApprove: this.editable && !this.isReviewed,
            pending: this.isPending,
            error: this.isError,
            statusLabel: this.page.status_label || '',
            dashboardUrl: this.nav.dashboard_url || '',
            back: this.back,
            jumping: this.jumping,
            hasPrev: Boolean(this.nav.prev_url),
            hasNext: Boolean(this.nav.next_url),
            loading: this.loading,
          };
          store.act = (name, arg) => { if (typeof this[name] === 'function') this[name](arg); };
        } catch (_) { /* no Alpine store (tests) */ }
      },

      // ------------------------------------------------------------ derived
      // D76 (§5.3): where review was opened from (`nav.back` = {label, url}, from `?from=`), else nothing
      get back() {
        const b = this.nav && this.nav.back;
        return b && b.url ? { label: b.label || 'الكتاب', url: b.url, from: b.from || '' } : null;
      },
      // opened from the book page (`nav.detour`, `from=book`): approval stays on the page and offers the way back
      get fromBook() {
        if (this.nav && this.nav.detour !== undefined) return Boolean(this.nav.detour);
        const b = this.nav && this.nav.back;
        return Boolean(b && b.from === 'book');
      },
      // the origin as a query string (`nav.origin.query`, «from=book&at=12»): client-built review URLs carry it, so a
      // page swap keeps the way back ('' when review was opened with no origin)
      get originQuery() { const o = this.nav && this.nav.origin; return (o && o.query) || ''; },
      // …and as the approve body's fields (its next_review_url then carries it)
      get originBody() {
        const o = (this.nav && this.nav.origin) || {};
        const out = {};
        ['from', 'at', 'block'].forEach((k) => { if (o[k] !== undefined && o[k] !== null && o[k] !== '') out[k] = o[k]; });
        return out;
      },
      withOrigin(url) {
        const q = this.originQuery;
        if (!url || !q || /[?&]from=/.test(url)) return url;
        const [path, hash] = String(url).split('#');
        return `${path}${path.includes('?') ? '&' : '?'}${q}${hash !== undefined ? `#${hash}` : ''}`;
      },
      get isError() { return this.page.status === 'error'; },
      get isPending() { return !this.isError && this.page.text_state !== 'final'; },
      get ready() { return !this.isError && !this.isPending; },
      get isReviewed() { return this.page.status === 'reviewed' || this.page.status === 'assembled' || Boolean(this.page.is_reviewed); },
      get editable() { return this.canEdit && this.ready && !this.loading && !this.isReviewed; },
      get pct() { return this.counts.low_total ? Math.round((100 * this.counts.resolved) / this.counts.low_total) : 100; },
      get currentLineId() {
        if (this.edit) return this.edit.lineId;
        if (this.focus) return this.focus.lineId;
        if (this.hot) return this.hot.lineId;
        return this.hotLine;
      },
      get currentLine() { return this.lineById(this.currentLineId); },
      get focused() {
        if (!this.focus || this.focus.gap != null) return null;
        const line = this.lineById(this.focus.lineId);
        return (line && line.tokens[this.focus.index]) || null;
      },
      // The focused gap ▏ (D72), still open; null otherwise.
      get focusedGap() {
        if (!this.focus || this.focus.gap == null) return null;
        const gap = this.gapById(this.focus.gap);
        return gap && this.gapOpen(gap) ? gap : null;
      },
      // What the popover shows: a gap's offer, an open group's keep / drop, or the word's readings.
      get popKind() {
        if (this.focusedGap) return 'gap';
        const tok = this.focused;
        return tok && this.inOpenGroup(tok) ? 'group' : 'word';
      },
      // D73: how the page was read (`page.reading.readers`); pages read before 7b count as two readers.
      get readers() {
        const r = this.page.reading && this.page.reading.readers;
        return r === 'one' || r === 'tesseract' ? r : 'two';
      },
      // The pill and the banner (nothing for two readers: the agreement pill is cut, §4.3).
      get readersInfo() { return READERS[this.readers] || null; },
      get hasGroups() { return this.lines.some((line) => (line.tokens || []).some((tok) => this.inOpenGroup(tok))); },
      get hasGaps() { return this.lines.some((line) => (line.gaps || []).some((gap) => this.gapOpen(gap))); },
      get hasWordMarks() { return this.lines.some((line) => (line.tokens || []).some((tok) => tok.conf === 'low' && this.groupOf(tok) === null)); },
      // «3 علامات» in the lines column's head (Arabic count forms, Western digits)
      get marksLabel() { return arCount(this.counts.unresolved, MARKS); },
      get errorHeadline() { return String(this.page.error || 'تعطّلت معالجة هذه الصفحة').split('\n')[0].trim(); },
      get zoomLabel() { return Math.round(this.zoom.scale * 100) + '%'; },
      get hasScan() { return Boolean(this.image.scan_url); },
      get thumbAspect() {
        const ratio = this.image.width && this.image.height ? this.image.width / this.image.height : 0.68;
        return `aspect-ratio: ${Math.round(ratio * 1000) / 1000};`;
      },

      lineById(id) { return id == null ? null : this.lines.find((l) => l.id === id) || null; },
      same(a, b) {
        if (!a || !b || a.lineId !== b.lineId) return false;
        if (a.gap != null || b.gap != null) return a.gap === b.gap;
        return a.index === b.index;
      },
      isUnresolved(tok) { return Boolean(tok) && tok.conf === 'low' && tok.res == null; },
      tokId(lineId, i) { return `rv-tok-${lineId}-${i}`; },
      gapElId(id) { return `rv-gap-${id}`; },
      refElId(ref) { return ref.gap != null ? this.gapElId(ref.gap) : this.tokId(ref.lineId, ref.index); },

      // ------------------------------------------------------------ D72: groups of added words, gaps
      // A token of a group carries `ins` (the group); the group is open while its words are unresolved.
      groupOf(tok) { return tok && tok.ins != null && tok.ins !== '' ? String(tok.ins) : null; },
      inOpenGroup(tok) { return this.groupOf(tok) !== null && this.isUnresolved(tok); },
      // Every word of a group in reading order (a group may run over two lines).
      groupRefs(group) {
        const out = [];
        this.lines.forEach((line) => (line.tokens || []).forEach((tok, i) => {
          if (this.groupOf(tok) === group) out.push({ lineId: line.id, index: i });
        }));
        return out;
      },
      get focusedGroup() {
        const tok = this.focused;
        return tok && this.inOpenGroup(tok) ? this.groupOf(tok) : null;
      },
      get groupSize() { const g = this.focusedGroup; return g === null ? 0 : this.groupRefs(g).length; },
      // The words between two tokens of one open group are joined by the group's dotted underline too.
      joinsGroup(line, i) {
        const a = line.tokens[i];
        const b = line.tokens[i + 1];
        const g = a && this.inOpenGroup(a) ? this.groupOf(a) : null;
        return g !== null && Boolean(b) && this.inOpenGroup(b) && this.groupOf(b) === g;
      },

      gapOpen(gap) { return Boolean(gap) && (gap.status == null || gap.status === 'open'); },
      gapById(id) {
        for (const line of this.lines) {
          const gap = (line.gaps || []).find((g) => g.id === id);
          if (gap) return gap;
        }
        return null;
      },
      lineOfGap(id) { return this.lines.find((line) => (line.gaps || []).some((g) => g.id === id)) || null; },
      // The open gaps of `line` that sit after token `i` (−1: before its first token).
      gapsAt(line, i) { return (line.gaps || []).filter((gap) => this.gapOpen(gap) && Number(gap.index) === i); },
      // A payload's page-level gaps (`{line_id, …}`), placed on their lines; the list is the page's, so a line
      // it leaves out has none.
      adoptGaps(gaps) {
        const byLine = new Map();
        gaps.forEach((gap) => {
          const id = gap.line_id != null ? gap.line_id : gap.line;
          if (!byLine.has(id)) byLine.set(id, []);
          byLine.get(id).push(gap);
        });
        this.lines.forEach((line) => { line.gaps = byLine.get(line.id) || []; });
      },
      // A mutation's answer may carry the page's open gaps (`gaps`): they replace the local ones.
      adoptAnswerGaps(data) { if (data && Array.isArray(data.gaps)) this.adoptGaps(data.gaps); },
      gapsSnapshot(line) { return (line.gaps || []).map((gap) => Object.assign({}, gap)); },
      // The optimistic step of a line change: the gaps after token `from` move by `delta` words (never before
      // the line's start); the server's answer then gives their true places (`retokenize`'s alignment).
      shiftGaps(line, from, delta) {
        (line.gaps || []).forEach((gap) => {
          if (Number(gap.index) >= from) gap.index = Math.max(-1, Number(gap.index) + delta);
        });
      },

      // ------------------------------------------------------------ words: navigation
      // The Tab stops in reading order (D72): open words, one per open group (its first word) and open gaps.
      unresolvedRefs() {
        const out = [];
        const seen = new Set();
        this.lines.forEach((line, li) => {
          const base = li * 10000;
          this.gapsAt(line, -1).forEach((gap) => out.push({ lineId: line.id, index: -1, gap: gap.id, pos: base - 0.5 }));
          (line.tokens || []).forEach((tok, i) => {
            if (this.isUnresolved(tok)) {
              const g = this.groupOf(tok);
              if (g === null) out.push({ lineId: line.id, index: i, pos: base + i });
              else if (!seen.has(g)) { seen.add(g); out.push({ lineId: line.id, index: i, group: g, pos: base + i }); }
            }
            this.gapsAt(line, i).forEach((gap) => out.push({ lineId: line.id, index: i, gap: gap.id, pos: base + i + 0.5 }));
          });
        });
        return out;
      },

      // A ref's place in reading order. A word of an open group stands for the whole group: at its last word
      // going forward (dir 1) and at its first going back, so Tab and ⇧Tab leave the group as one stop.
      posOf(ref, dir) {
        const li = this.lines.findIndex((l) => l.id === ref.lineId);
        if (li < 0) return -1;
        if (ref.gap != null) return li * 10000 + ref.index + 0.5;
        const line = this.lines[li];
        const tok = line.tokens && line.tokens[ref.index];
        if (tok && this.inOpenGroup(tok)) {
          const refs = this.groupRefs(this.groupOf(tok));
          const edge = dir < 0 ? refs[0] : refs[refs.length - 1];
          return this.lines.findIndex((l) => l.id === edge.lineId) * 10000 + edge.index;
        }
        return li * 10000 + ref.index;
      },

      // Next (dir 1) / previous (dir -1) stop in reading order, wrapping around. `from` is a ref, or `{ at }`: a
      // place in reading order taken before a change removed its word (a group dropped with its line).
      nextUnresolved(dir, from) {
        const refs = this.unresolvedRefs();
        if (!refs.length) return null;
        const start = from === undefined ? this.focus : from;
        if (!start) return dir > 0 ? refs[0] : refs[refs.length - 1];
        const cur = start.at != null ? start.at : this.posOf(start, dir);
        if (dir > 0) return refs.find((r) => r.pos > cur) || refs[0];
        for (let i = refs.length - 1; i >= 0; i -= 1) if (refs[i].pos < cur) return refs[i];
        return refs[refs.length - 1];
      },

      move(dir) {
        const ref = this.nextUnresolved(dir);
        if (!ref) {
          this.toast(this.counts.low_total ? 'حُسمت كل الكلمات في هذه الصفحة' : 'لا علامات في هذه الصفحة؛ اقرأ الأسطر مع الصورة ثم اعتمدها.');
          return false;
        }
        this.focusWord(ref, { open: true });
        return true;
      },

      // Focus a word or a gap (`ref.gap`) and open its menu (`open: false` keeps it closed).
      focusWord(ref, options) {
        const opts = options || {};
        this.focus = ref.gap != null ? { lineId: ref.lineId, index: ref.index, gap: ref.gap } : { lineId: ref.lineId, index: ref.index };
        this.menuFor = null;
        this.pop.typed = '';
        this.pop.typing = false;
        const tok = this.focused;
        const gap = this.focusedGap;
        // any word opens the popover on an editable page (merge / delete, D31); read-only: uncertain words only
        this.pop.open = opts.open !== false && (Boolean(gap) || (Boolean(tok) && (tok.conf === 'low' || this.editable)));
        this.pop.more = false;
        // D32: correction is the main action; a click on a confident word opens it prefilled and selected
        const prefill = this.pop.open && opts.fromClick && tok && tok.conf !== 'low' && this.editable;
        if (prefill) { this.pop.typing = true; this.pop.typed = tok.t; }
        if (gap) this.pop.typed = String(gap.text || ''); // the words a gap would insert, ready to edit
        if (!hasDOM) return;
        this.tick(() => {
          const el = document.getElementById(this.refElId(this.focus || ref));
          if (el) {
            if (document.activeElement !== el) el.focus({ preventScroll: true });
            el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
            this.placePop(el);
          }
          if (prefill && this.$refs.typed) { this.$refs.typed.focus({ preventScroll: true }); this.$refs.typed.select(); }
          const box = tok ? tok.bbox : this.gapBox(gap);
          if (opts.pan !== false && box) this.panTo(box);
        });
      },

      // A gap has no box of its own: the scan shows the word it follows (or the one after it).
      gapBox(gap) {
        const line = gap ? this.lineOfGap(gap.id) : null;
        if (!line) return null;
        const tokens = line.tokens || [];
        const near = tokens[Math.max(0, Number(gap.index))] || tokens[Number(gap.index) + 1];
        return (near && near.bbox) || line.bbox || null;
      },

      onGapClick(line, gap) {
        this.range = { anchor: line.id, ids: [] };
        this.focusWord({ lineId: line.id, index: Number(gap.index), gap: gap.id }, { open: true });
      },

      onTokFocus(line, i) {
        if (this.same(this.focus, { lineId: line.id, index: i })) return;
        const tok = line.tokens[i];
        if (tok && tok.conf === 'low') this.focusWord({ lineId: line.id, index: i }, { open: true });
      },

      onTokClick(line, i, ev) {
        if (ev && ev.shiftKey) { this.extendRange(line); return; } // ⇧-click: a range of lines (D74)
        this.range = { anchor: line.id, ids: [] };
        const tok = line.tokens[i];
        if (tok && (tok.conf === 'low' || this.editable)) this.focusWord({ lineId: line.id, index: i }, { open: true, fromClick: true });
        else { this.focus = null; this.pop.open = false; this.hotLine = line.id; }
      },

      // ------------------------------------------------------------ lines: a ⇧-click range (D74)
      // A click on a line sets the range's anchor; ⇧-click selects every line from the anchor to it, and the
      // line menu then sets the role of all of them («نوع الأسطر المحدَّدة (6)»). Esc or a plain click clears it.
      onLineClick(line, ev) {
        if (ev && ev.shiftKey) { this.extendRange(line); return; }
        this.range = { anchor: line.id, ids: [] };
      },
      // ⇧ + mousedown would extend the browser's text selection: the range replaces it.
      onLineMouseDown(ev) { if (ev && ev.shiftKey && ev.preventDefault) ev.preventDefault(); },
      extendRange(line) {
        const anchorId = this.lineById(this.range.anchor) ? this.range.anchor : (this.currentLineId != null ? this.currentLineId : line.id);
        const a = this.lines.findIndex((l) => l.id === anchorId);
        const b = this.lines.findIndex((l) => l.id === line.id);
        if (b < 0) return;
        const [from, to] = a < 0 ? [b, b] : [Math.min(a, b), Math.max(a, b)];
        this.closePop();
        this.focus = null;
        this.menuFor = null;
        this.range = { anchor: anchorId, ids: this.lines.slice(from, to + 1).map((l) => l.id) };
      },
      clearRange() { this.range = { anchor: this.range.anchor, ids: [] }; },
      inRange(line) { return Boolean(line) && this.range.ids.length > 1 && this.range.ids.includes(line.id); },
      // The lines the menu of `line` acts on: the range when the line is in it, else the line alone.
      menuLines(line) { return this.inRange(line) ? this.range.ids.map((id) => this.lineById(id)).filter(Boolean) : [line]; },
      roleMenuLabel(line) {
        const n = this.menuLines(line).length;
        return n > 1 ? `نوع الأسطر المحدَّدة (${n})` : 'نوع السطر';
      },

      // A box on the image opens its word's menu in the text exactly as a click on the word there does (owner
      // review 2026-10-03: one menu, one behaviour): on an editable page every word, a confident one with its
      // text in the correction field, selected; on a read-only page the uncertain words' readings. A word that
      // opens no menu is still focused and scrolled into view. A second tap on the word whose menu is open
      // closes it. The image stays where it is.
      onBoxClick(line, i, ev) {
        if (this.dragMoved) return;
        if (ev && ev.shiftKey) { this.extendRange(line); return; }
        this.range = { anchor: line.id, ids: [] };
        const tok = line.tokens[i];
        if (!tok) return;
        const ref = { lineId: line.id, index: i };
        if (this.pop.open && this.same(this.focus, ref)) { this.closePop(); return; }
        const open = tok.conf === 'low' || this.editable; // = onTokClick
        this.focusWord(ref, { open, fromClick: true, pan: false });
      },

      // Every tap on the image comes here (the boxes have no click handler of their own). The scan captures the
      // pointer on pointerdown (to drag), so the browser may deliver the click to the scan rather than to the box
      // under it: the box pressed is remembered on pointerdown. A drag's release is no tap. A tap on a box is
      // stopped (the popover's click-outside would close what it just opened); a tap beside the boxes goes on
      // and closes an open menu.
      onScanClick(ev) {
        const tap = this.tapBox;
        this.tapBox = null;
        if (!tap || this.dragMoved) return;
        const line = this.lines.find((l) => String(l.id) === tap.line);
        if (!line) return;
        if (ev && ev.stopPropagation) ev.stopPropagation();
        this.onBoxClick(line, tap.index, ev);
      },

      // The part of the lines column the reader can see (the scroller clipped to the window), minus the margin.
      visibleArea(node) {
        const scroller = node && node.closest ? node.closest('.rv-lines-scroll') : null;
        const r = scroller && scroller.getBoundingClientRect ? scroller.getBoundingClientRect() : null;
        const W = window.innerWidth || 0;
        const H = window.innerHeight || 0;
        return {
          top: Math.max(r ? r.top : 0, 0) + POP_EDGE,
          bottom: Math.min(r ? r.bottom : H, H) - POP_EDGE,
          left: Math.max(r ? r.left : 0, 0) + POP_EDGE,
          right: Math.min(r ? r.right : W, W) - POP_EDGE,
        };
      },

      // Open below the word, flip above when it would leave the visible column and there is more room
      // above; the popover's right edge sits on the word's right edge (RTL) and never crosses a side edge.
      placePop(el) {
        const host = this.$refs && this.$refs.stage;
        if (!el || !host || !el.getBoundingClientRect) return;
        const tok = el.getBoundingClientRect();
        const ref = host.getBoundingClientRect();
        const view = this.visibleArea(host);
        const node = this.$refs.pop;
        const w = node && node.offsetWidth ? node.offsetWidth : POP_W;
        const h = node && node.offsetHeight ? node.offsetHeight : POP_H_GUESS;
        let top = tok.bottom + POP_GAP;
        let above = false;
        if (top + h > view.bottom) {
          const roomAbove = tok.top - POP_GAP - view.top;
          const roomBelow = view.bottom - top;
          if (roomAbove >= h || roomAbove > roomBelow) { top = Math.max(view.top, tok.top - POP_GAP - h); above = true; }
          else top = Math.max(view.top, view.bottom - h);
        }
        let right = Math.min(tok.right, view.right);
        if (right - w < view.left) right = Math.min(view.right, view.left + w);
        this.pop.above = above;
        this.pop.style = `top:${Math.round(top - ref.top)}px; right:${Math.round(ref.right - right)}px;`;
      },

      repositionPop() {
        if (!hasDOM || !this.pop.open || !this.focus) return;
        this.placePop(document.getElementById(this.refElId(this.focus)));
        if (this.pop.more) this.placeMore();
      },

      // A click anywhere outside the popover closes it (clicks on words and scan boxes stop propagation and
      // open their own popover instead); the release of a scan drag does not count.
      onPopOutside() {
        if (!this.pop.open) return;
        if (this.dragMoved) { this.dragMoved = false; return; }
        this.closePop();
        this.focus = null;
      },

      closePop() { this.pop.open = false; this.pop.typing = false; this.pop.typed = ''; this.pop.more = false; },

      // ---- «إجراءات أخرى»: a second-level menu (hover, click or ArrowLeft) with merge and delete, kept
      // away from the correction so a destructive action is never one mis-click away (D32).
      openMore(focusFirst) {
        clearTimeout(this.moreTimer);
        if (!this.pop.more) this.pop.more = true;
        this.tick(() => {
          this.placeMore();
          if (focusFirst && this.$refs.more) {
            const first = this.$refs.more.querySelector('button:not([disabled])');
            if (first) first.focus();
          }
        });
      },

      closeMoreSoon() {
        clearTimeout(this.moreTimer);
        this.moreTimer = setTimeout(() => { this.pop.more = false; }, MORE_CLOSE_MS);
      },

      closeMore(focusRow) {
        clearTimeout(this.moreTimer);
        this.pop.more = false;
        const row = this.$refs && this.$refs.moreRow;
        if (focusRow && row && row.focus) row.focus();
      },

      toggleMore() { if (this.pop.more) this.closeMore(); else this.openMore(); },

      // Beside the popover on the side with room (RTL: the left first), level with its row and kept inside
      // the visible column; folded into the popover when neither side has room (a narrow column).
      placeMore() {
        const pop = this.$refs && this.$refs.pop;
        const row = this.$refs && this.$refs.moreRow;
        if (!pop || !row || !pop.getBoundingClientRect) return;
        const fly = this.$refs.more;
        const p = pop.getBoundingClientRect();
        const view = this.visibleArea(pop);
        const w = fly && fly.offsetWidth ? fly.offsetWidth : MORE_W;
        const h = fly && fly.offsetHeight ? fly.offsetHeight : 132;
        const roomLeft = p.left - view.left;
        const roomRight = view.right - p.right;
        let side = 'left';
        if (roomLeft < w + 6) side = roomRight >= w + 6 ? 'right' : 'below';
        let top = (row.offsetTop || 0) - 6;
        if (p.top + top + h > view.bottom) top -= p.top + top + h - view.bottom;
        if (p.top + top < view.top) top = view.top - p.top;
        this.pop.moreSide = side;
        this.pop.moreStyle = side === 'below' ? '' : `top:${Math.round(top)}px;`;
      },

      moveInMore(ev, dir) {
        const fly = this.$refs && this.$refs.more;
        const items = fly ? Array.from(fly.querySelectorAll('button:not([disabled])')) : [];
        if (!items.length) return;
        const i = items.indexOf(ev && ev.target);
        const next = items[(i + dir + items.length) % items.length];
        if (next) next.focus();
      },

      // ------------------------------------------------------------ words: readings
      options(token) {
        const tok = token === undefined ? this.focused : token;
        if (!tok || tok.conf !== 'low') return [];
        const rows = [];
        // `orig` (sent once a resolution changed `t`) keeps the primary model's reading, so all three
        // readings stay offered after a word was resolved; `current` marks the one now in the text.
        const primary = tok.orig || tok.t;
        // a number Kraken read (D50) is its one reading: confirm it, or type the true number; where Qari
        // wrote a letter for it (D51), Qari's letter is the second reading (it may be a real letter)
        const kraken = tok.src === 'kraken';
        // a word only the second model read (D72) has no reading of the first: its one reading is the second's
        const added = this.groupOf(tok) !== null;
        const label = kraken ? 'Kraken' : added ? this.secondaryLabel : this.labels.primary || 'النموذج الأول';
        const second = kraken ? this.labels.primary || 'النموذج الأول' : this.secondaryLabel;
        rows.push({ choice: 'primary', value: primary, label, current: primary === tok.t });
        if (tok.alt && tok.alt !== primary) rows.push({ choice: 'secondary', value: tok.alt, label: second, current: tok.alt === tok.t });
        if (tok.tess && tok.tess !== primary && tok.tess !== tok.alt) rows.push({ choice: 'tess', value: tok.tess, label: 'Tesseract', current: tok.tess === tok.t });
        // §4.8: a year read again from the number in words, offered as one more reading («من الحروف»)
        const sug = tok.sug && tok.sug.t ? tok.sug : null;
        if (sug && !rows.some((row) => row.value === sug.t)) rows.push({ choice: 'sug', value: sug.t, label: sug.label || 'من الحروف', current: sug.t === tok.t });
        // Owner review 2026-10-03: the word in the text comes first, so «1» (like Enter) accepts it and a digit
        // never picks another reading by mistake; the other readings follow in the models' order. A correction
        // typed earlier is no model's reading: it stands first as its own row.
        const at = rows.findIndex((row) => row.current);
        if (at > 0) rows.unshift(rows.splice(at, 1)[0]);
        else if (at < 0 && tok.t) rows.unshift({ choice: 'typed', value: tok.t, label: 'تصحيح مكتوب', current: true });
        rows.forEach((row, i) => { row.key = String(i + 1); });
        return rows;
      },

      // A reading picked from the menu, by its digit or a click. The row of a typed correction confirms the word
      // as it stands: an open word saves it, a decided one needs nothing and the menu moves on, as Enter does.
      pickOption(row) {
        if (!row) return Promise.resolve(false);
        if (row.choice !== 'typed') return this.choose(row.choice);
        if (this.isUnresolved(this.focused)) return this.choose('typed', row.value);
        this.advance(this.focus);
        return Promise.resolve(false);
      },

      get primaryLabel() { return this.labels.primary || 'Qari v0.3'; },
      get secondaryLabel() { return this.labels.secondary || 'Qari v0.2'; },

      // D71: why the focused word is marked, in one line under the popover's title ('' when it is not marked).
      // `why` is the flag policy's list (first match wins, `year` may follow); tokens stored before 7b have none,
      // and a number among them keeps the number's line.
      reasonOf(token) {
        const tok = token === undefined ? this.focused : token;
        if (!tok || tok.conf !== 'low') return '';
        const why = Array.isArray(tok.why) ? tok.why : [];
        const P = this.primaryLabel;
        const S = this.secondaryLabel;
        const primary = tok.orig || tok.t;
        if (why.includes('year') && tok.sug && tok.sug.words) return `السنة مكتوبة بعدها بالحروف: «${tok.sug.words}» = ${tok.sug.t}.`;
        switch (why[0]) {
          case 'disagree':
            if (tok.pick === 'vote') return `النموذجان مختلفان؛ Tesseract يوافق ${S}، فقراءته في النص.`;
            if (this.tessBacksPrimary(tok)) return `النموذجان مختلفان؛ Tesseract يوافق ${P}.`;
            return 'النموذجان مختلفان.';
          case 'alone': return `لم يقرأ ${S} هذه الكلمة، ولم يؤكّدها Tesseract.`;
          case 'number': return 'رقم: قابِله بالصورة.';
          case 'script': {
            const found = foreignChars(`${primary} ${tok.t}`);
            if (found.letters.length) return `في الكلمة حروف ليست عربية (${quoted(found.letters)}).`;
            if (found.symbols.length) return `في الكلمة رمز غريب (${quoted(found.symbols)}).`;
            return 'في الكلمة حروف ليست عربية.';
          }
          case 'single':
            return tok.tess ? `قرأ هذه المنطقةَ نموذجٌ واحد، ويقرأ Tesseract هنا «${tok.tess}».` : 'قرأ هذه المنطقةَ نموذجٌ واحد.';
          case 'missing': return `كلمات أضافتها القراءة الثانية: قرأها ${S} وTesseract ولم يقرأها ${P}.`;
          default:
            if (tok.call) return 'علامة حاشية قرأها Kraken من الصورة: قابِلها بالصورة.';
            // D103: a note's number set from Kraken's reading of its line and the page's run of numbers
            if (tok.marker && tok.src === 'kraken') return 'رقم الحاشية من قراءة Kraken للسطر وتسلسل حواشي الصفحة: قابِله بالصورة.';
            return !why.length && tok.digit ? 'رقم: قابِله بالصورة.' : '';
        }
      },
      get reason() { return this.reasonOf(); },
      // The group menu's line (its title says «كلمات أضافتها القراءة الثانية»).
      get groupReason() { return `قرأها ${this.secondaryLabel} وTesseract ولم يقرأها ${this.primaryLabel}.`; },
      // The popover's title and its reason line, per kind (§4.1, §4.2).
      get popTitle() {
        if (this.popKind === 'gap') return 'قد تكون هنا كلمات ناقصة';
        if (this.popKind === 'group') return 'كلمات أضافتها القراءة الثانية';
        const tok = this.focused;
        if (tok && tok.conf === 'low') return this.canEdit ? 'اختر القراءة الصحيحة' : 'قراءات هذه الكلمة';
        return 'تصحيح الكلمة';
      },
      get popReason() {
        if (this.popKind === 'gap') return `يقرأ النموذج الثاني هنا: «${this.gapOffer}»`;
        if (this.popKind === 'group') return this.groupReason;
        return this.reason;
      },

      // Without the vote, Tesseract is on the first model's side when its word is the first model's (it is kept on
      // the token only where it differs, so a boxed token without one agrees) and not the second's.
      tessBacksPrimary(tok) {
        const primary = lenient(tok.orig || tok.t);
        if (tok.tess == null || tok.tess === '') return Boolean(tok.bbox);
        const tess = lenient(tok.tess);
        return tess === primary && tess !== lenient(tok.alt);
      },

      chooseNth(n) { return this.pickOption(this.options()[n - 1]); },

      // Close the popover and move on to the next unresolved word; false when none is left (focus cleared).
      advance(ref) {
        this.closePop();
        const next = this.nextUnresolved(1, ref);
        if (next) this.focusWord(next, { open: true }); else this.focus = null;
        return Boolean(next);
      },

      // Enter: confirm what is in the text (D71: the option marked «● في النص», so a word the vote gave Qari
      // v0.2's reading keeps it), keep a group of added words, or insert a gap's words (D72). A word the chooser
      // picked (D26) records the reviewer's confirmation of that reading; a word the reviewer already resolved,
      // or a confident one, needs no request: the popover closes and focus moves on.
      accept() {
        if (!this.editable) return Promise.resolve(false);
        if (this.focusedGap) return this.acceptGap();
        const tok = this.focused;
        if (!tok) return Promise.resolve(false);
        if (this.inOpenGroup(tok)) return this.keepGroup();
        if (this.isUnresolved(tok) || tok.res === 'chooser') {
          const cur = this.options().find((o) => o.current);
          return cur ? this.pickOption(cur) : this.choose('primary');
        }
        this.advance(this.focus);
        return Promise.resolve(false);
      },

      // Resolve the focused word. Optimistic: the word lands, the counter ticks, focus moves on; the
      // POST follows and a failure restores the word and the counts.
      choose(choice, text) {
        const ref = this.focus;
        const line = this.lineById(ref && ref.lineId);
        const tok = line && line.tokens[ref.index];
        if (!tok || !this.editable) return Promise.resolve(false);
        let value = tok.orig || tok.t;
        if (choice === 'secondary') value = tok.alt;
        else if (choice === 'tess') value = tok.tess;
        else if (choice === 'sug') value = tok.sug && tok.sug.t;
        else if (choice === 'typed') value = String(text || '').replace(/\s+/g, ' ').trim();
        if (!value) return Promise.resolve(false);
        if (choice === 'typed' && value === tok.t && !this.isUnresolved(tok)) { this.closePop(); return Promise.resolve(false); }
        // the reading already in the text, chosen again: a confirmation, nothing to record
        if (choice !== 'typed' && choice === tok.res && value === tok.t) { this.advance(ref); return Promise.resolve(false); }

        const before = Object.assign({}, tok);
        const beforeCounts = Object.assign({}, this.counts);
        const beforeNLow = line.n_low;
        const wasOpen = this.isUnresolved(tok);
        if (value !== tok.t && !tok.orig) tok.orig = tok.t;
        tok.t = value;
        tok.res = choice;
        if (wasOpen) {
          line.n_low = Math.max(0, (line.n_low || 0) - 1);
          const c = this.counts;
          const split = c.words != null ? { words: Math.max(0, c.words - 1), groups: c.groups, gaps: c.gaps } : {};
          this.setCounts(Object.assign({ low_total: c.low_total, unresolved: c.unresolved - 1, resolved: c.resolved + 1 }, split));
        }
        this.flash(ref);
        if (!this.advance(ref) && wasOpen && this.counts.unresolved === 0) this.toast('حُسمت كل الكلمات · اعتمد الصفحة بـ A');

        const body = { index: ref.index, choice, t: before.t }; // t: the word as seen, so a stale tab gets a 409
        if (choice === 'typed') body.text = value;
        return this.request(() => api(fill(this.urls.resolve, line.id), { method: 'POST', body }), {
          apply: (data, seq) => {
            if (data.line) this.replaceLine(data.line);
            this.applyApiCounts(data.counts, this.lineById(line.id));
            if (data.page) Object.assign(this.page, data.page);
            this.offerElsewhere(data.elsewhere, seq);
          },
          rollback: () => {
            const l = this.lineById(line.id);
            if (l && l.tokens[ref.index]) { const t = l.tokens[ref.index]; if (!('orig' in before)) delete t.orig; Object.assign(t, before); l.n_low = beforeNLow; }
            this.setCounts(beforeCounts);
          },
          retry: () => { this.focus = { lineId: ref.lineId, index: ref.index }; return this.choose(choice, text); },
        });
      },

      // ------------------------------------------------------------ words: merge / delete (D31)
      // What the popover may offer for the focused word: merge with the next / previous word on its line.
      wordActions() {
        const ref = this.focus;
        const line = this.lineById(ref && ref.lineId);
        const n = line ? (line.tokens || []).length : 0;
        if (!line || !this.editable || !n) return { next: false, prev: false, del: false };
        return { next: ref.index + 1 < n, prev: ref.index > 0, del: true };
      },

      // The word a merge would produce («هير» + «ودوت» → «هيرودوت»); '' when there is nothing to merge with.
      mergePreview(dir) {
        const ref = this.focus;
        const line = this.lineById(ref && ref.lineId);
        const tokens = line ? line.tokens || [] : [];
        const k = dir < 0 ? (ref ? ref.index - 1 : -1) : (ref ? ref.index : -1);
        if (k < 0 || k + 1 >= tokens.length) return '';
        return tokens[k].t + tokens[k + 1].t;
      },

      // Join the focused word with the next (dir 1, reading order: to its left) or previous (dir -1) word.
      // Optimistic: the merged word lands at once with the union of both boxes; a failure restores both.
      mergeWord(dir) {
        const ref = this.focus;
        const line = this.lineById(ref && ref.lineId);
        if (!line || !this.editable) return Promise.resolve(false);
        const tokens = line.tokens || [];
        const k = dir < 0 ? ref.index - 1 : ref.index;
        if (k < 0 || k + 1 >= tokens.length) return Promise.resolve(false);
        const before = tokens.map((t) => Object.assign({}, t));
        const beforeText = line.text;
        const beforeGaps = this.gapsSnapshot(line);
        const beforeCounts = Object.assign({}, this.counts);
        const merged = plainToken(tokens[k].t + tokens[k + 1].t);
        merged.bbox = unionBox(tokens[k].bbox, tokens[k + 1].bbox);
        tokens.splice(k, 2, merged);
        line.text = tokens.map((t) => t.t).join(' ');
        this.shiftGaps(line, k + 1, -1);
        this.recount();
        this.closePop();
        const at = { lineId: line.id, index: k };
        this.focusWord(at, { open: false });
        this.flash(at);
        const seen = { index: k, t: before[k].t, t_next: before[k + 1].t };
        return this.request(() => api(fill(this.urls.merge, line.id), { method: 'POST', body: seen }), {
          apply: (data) => {
            if (data.line) this.replaceLine(data.line);
            this.adoptAnswerGaps(data);
            this.applyApiCounts(data.counts, this.lineById(line.id));
          },
          rollback: () => {
            const l = this.lineById(line.id);
            if (l) { l.tokens = before; l.text = beforeText; l.gaps = beforeGaps; }
            this.setCounts(beforeCounts);
            this.recount();
          },
          retry: () => { this.focus = { lineId: ref.lineId, index: ref.index }; return this.mergeWord(dir); },
        });
      },

      // Remove the focused word (a stray letter or number); the line's only word removes the line.
      deleteWord() {
        const ref = this.focus;
        const line = this.lineById(ref && ref.lineId);
        if (!line || !this.editable) return Promise.resolve(false);
        const tokens = line.tokens || [];
        if (!tokens[ref.index]) return Promise.resolve(false);
        if (tokens.length === 1) { this.closePop(); this.focus = null; return this.removeLine(line); }
        const before = tokens.map((t) => Object.assign({}, t));
        const beforeText = line.text;
        const beforeGaps = this.gapsSnapshot(line);
        const beforeCounts = Object.assign({}, this.counts);
        tokens.splice(ref.index, 1);
        line.text = tokens.map((t) => t.t).join(' ');
        this.shiftGaps(line, ref.index, -1);
        this.recount();
        this.closePop();
        this.focusWord({ lineId: line.id, index: Math.min(ref.index, tokens.length - 1) }, { open: false });
        const seen = { index: ref.index, t: before[ref.index].t };
        return this.request(() => api(fill(this.urls.delete_word, line.id), { method: 'POST', body: seen }), {
          apply: (data, seq) => {
            if (data.line) this.replaceLine(data.line);
            this.adoptAnswerGaps(data);
            this.applyApiCounts(data.counts, this.lineById(line.id));
            this.showUndoToast('حُذفت الكلمة', seq);
          },
          rollback: () => {
            const l = this.lineById(line.id);
            if (l) { l.tokens = before; l.text = beforeText; l.gaps = beforeGaps; }
            this.setCounts(beforeCounts);
            this.recount();
          },
          retry: () => { this.focus = { lineId: ref.lineId, index: ref.index }; return this.deleteWord(); },
        });
      },

      // ------------------------------------------------------------ D72: a group kept or dropped, a gap decided
      // Keep (Enter, «إبقاء الكلمات (12)») or drop («حذف الكلمات (12)») every word of the focused group, on
      // whichever lines it runs over (`resolve_insertion`, one batch: one ⌘Z undoes it). Optimistic: kept words
      // lose the dotted underline at once, dropped ones leave the text; a failure restores the lines.
      resolveGroup(keep) {
        const group = this.focusedGroup;
        if (group === null || !this.editable) return Promise.resolve(false);
        const refs = this.groupRefs(group);
        const ids = Array.from(new Set(refs.map((r) => r.lineId)));
        const before = ids.map((id) => JSON.parse(JSON.stringify(this.lineById(id))));
        const beforeCounts = Object.assign({}, this.counts);
        const first = refs[0];
        const n = refs.length;
        const before0 = { at: this.posOf(first, -1) - 0.75 }; // just before the group: where a drop leaves off
        ids.forEach((id) => {
          const line = this.lineById(id);
          if (keep) {
            line.tokens.forEach((tok) => { if (this.groupOf(tok) === group && tok.res == null) tok.res = 'secondary'; });
            return;
          }
          // a gap after old word i now follows the last kept word at or before it (−1: the line's start)
          const place = [];
          let last = -1;
          line.tokens.forEach((tok, i) => { if (this.groupOf(tok) !== group) last += 1; place[i] = last; });
          (line.gaps || []).forEach((gap) => { const g = Number(gap.index); if (g >= 0) gap.index = place[g]; });
          line.tokens = line.tokens.filter((tok) => this.groupOf(tok) !== group);
          line.text = line.tokens.map((t) => t.t).join(' ');
        });
        // a line that was all added words goes with them (the answer brings it back if the server keeps it)
        if (!keep) this.lines = this.lines.filter((l) => !ids.includes(l.id) || (l.tokens || []).length > 0);
        this.recount();
        if (keep) refs.forEach((r) => this.flash(r));
        if (!this.advance(keep ? refs[n - 1] : before0) && this.counts.unresolved === 0) this.toast('حُسمت كل الكلمات · اعتمد الصفحة بـ A');
        return this.request(() => api(fill(this.urls.insertion, group), { method: 'POST', body: { keep: Boolean(keep) } }), {
          apply: (data, seq) => {
            // {lines: the changed lines, deleted_ids: lines the drop emptied, order: the page's lines after it}
            const gone = new Set(data.deleted_ids || []);
            if (gone.size) this.lines = this.lines.filter((l) => !gone.has(l.id));
            (data.lines || []).forEach((line) => this.replaceLine(line));
            if (Array.isArray(data.order)) this.reorder(data.order);
            this.adoptAnswerGaps(data);
            this.applyApiCounts(data.counts);
            if (data.page) Object.assign(this.page, data.page);
            if (!keep) this.showUndoToast(n === 1 ? 'حُذفت الكلمة المضافة' : 'حُذفت الكلمات المضافة', seq);
          },
          rollback: () => {
            before.forEach((line) => this.replaceLine(line));
            this.setCounts(beforeCounts);
            this.recount();
          },
          retry: () => { this.focus = { lineId: first.lineId, index: first.index }; return this.resolveGroup(keep); },
        });
      },
      keepGroup() { return this.resolveGroup(true); },
      dropGroup() { return this.resolveGroup(false); },

      // Insert the focused gap's words (Enter, «إدراج»), as offered or as typed in its field (`accept_gap`); the
      // gap closes, and ⌘Z reopens it. The words land after the gap's word, typed (sure) like a correction.
      acceptGap(text) {
        const gap = this.focusedGap;
        const line = gap ? this.lineOfGap(gap.id) : null;
        if (!gap || !line || !this.editable) return Promise.resolve(false);
        const typed = splitWords(text === undefined ? (this.pop.typed || gap.text) : text).join(' ');
        if (!typed) return Promise.resolve(false);
        const before = JSON.parse(JSON.stringify(line));
        const beforeCounts = Object.assign({}, this.counts);
        const words = splitWords(typed);
        const at = Math.max(-1, Number(gap.index));
        gap.status = 'inserted';
        this.shiftGaps(line, at + 1, words.length);
        line.tokens.splice(at + 1, 0, ...words.map(plainToken));
        line.text = line.tokens.map((t) => t.t).join(' ');
        this.recount();
        const landed = { lineId: line.id, index: at + words.length };
        words.forEach((_, k) => this.flash({ lineId: line.id, index: at + 1 + k }));
        if (!this.advance(landed) && this.counts.unresolved === 0) this.toast('حُسمت كل الكلمات · اعتمد الصفحة بـ A');
        // the offered words go as they are (`{}`); words the reviewer changed travel as `text`
        const offered = splitWords(gap.text).join(' ');
        const body = typed === offered ? {} : { text: typed };
        return this.request(() => api(fill(this.urls.gap_accept, gap.id), { method: 'POST', body }), {
          apply: (data) => {
            if (data.line) this.replaceLine(data.line);
            this.adoptAnswerGaps(data);
            this.applyApiCounts(data.counts, this.lineById(line.id));
            if (data.page) Object.assign(this.page, data.page);
          },
          rollback: () => { this.replaceLine(before); this.setCounts(beforeCounts); },
          retry: () => { this.focus = { lineId: line.id, index: at, gap: gap.id }; return this.acceptGap(typed); },
        });
      },

      // ⌫ on a gap, «تجاهل»: the offer is dropped (`dismiss_gap`, «نص مقترح» in the history); ⌘Z or the toast
      // brings it back. Nothing in the text changes.
      dismissGap() {
        const gap = this.focusedGap;
        const line = gap ? this.lineOfGap(gap.id) : null;
        if (!gap || !line || !this.editable) return Promise.resolve(false);
        const beforeStatus = gap.status || 'open';
        const beforeCounts = Object.assign({}, this.counts);
        gap.status = 'dismissed';
        this.recount();
        const ref = { lineId: line.id, index: Number(gap.index), gap: gap.id };
        if (!this.advance(ref) && this.counts.unresolved === 0) this.toast('حُسمت كل الكلمات · اعتمد الصفحة بـ A');
        return this.request(() => api(fill(this.urls.gap_dismiss, gap.id), { method: 'POST', body: {} }), {
          apply: (data, seq) => {
            if (data.line) this.replaceLine(data.line);
            this.adoptAnswerGaps(data);
            this.applyApiCounts(data.counts, data.line ? this.lineById(data.line.id) : null);
            if (data.page) Object.assign(this.page, data.page);
            this.showUndoToast('تُجوهل النص المقترح', seq);
          },
          rollback: () => { const g = this.gapById(ref.gap); if (g) g.status = beforeStatus; this.setCounts(beforeCounts); },
          retry: () => { this.focus = ref; return this.dismissGap(); },
        });
      },

      // What the gap offers, in the popover's line («يقرأ النموذج الثاني هنا: «الاجابة»»).
      get gapOffer() { const gap = this.focusedGap; return gap ? String(gap.text || '').trim() : ''; },
      gapTitle(gap) { return gap ? `قد تكون هنا كلمات ناقصة: «${String(gap.text || '').trim()}»` : ''; },
      groupCountLabel(verb) { return `${verb} (${this.groupSize})`; },

      startTyping(ch) {
        if (this.focusedGap && this.editable) {
          // on a gap the field holds the words to insert: a key starts them afresh (Esc brings the offer back)
          this.pop.open = true;
          this.pop.typing = true;
          this.pop.typed = ch || '';
          this.tick(() => {
            const input = this.$refs.gapTyped;
            if (input) { input.focus(); try { input.setSelectionRange(input.value.length, input.value.length); } catch (_) { /* fine */ } }
          });
          return;
        }
        if (!this.focused || !this.editable) return;
        this.pop.open = true;
        this.pop.typing = true;
        this.pop.typed = ch || '';
        this.tick(() => {
          const input = this.$refs.typed;
          if (input) { input.focus(); try { input.setSelectionRange(input.value.length, input.value.length); } catch (_) { /* fine */ } }
        });
      },

      submitTyped() {
        const value = this.pop.typed.trim();
        if (!value) return Promise.resolve(false);
        return this.choose('typed', value);
      },

      // Tab in the correction input: a draft that changes the word (or confirms an unresolved one) is saved,
      // and choose() moves on; an untouched prefill just moves to the next / previous unresolved word.
      onTypedTab(shift) {
        const value = this.pop.typed.trim();
        const tok = this.focused;
        if (value && tok && (value !== tok.t || this.isUnresolved(tok))) return this.submitTyped();
        return this.move(shift ? -1 : 1);
      },

      flash(ref) {
        const key = `${ref.lineId}-${ref.index}`;
        this.flashing = Object.assign({}, this.flashing, { [key]: true });
        setTimeout(() => { const next = Object.assign({}, this.flashing); delete next[key]; this.flashing = next; }, FLASH_MS);
      },

      // An open group's words carry a dotted underline instead of the amber one (D72); the whole group lights up
      // while one of its words is focused.
      tokClass(line, tok, i) {
        const ref = { lineId: line.id, index: i };
        const low = tok.conf === 'low';
        const group = this.inOpenGroup(tok);
        const focusGroup = this.focusedGroup;
        return {
          'is-low': low,
          'is-open': low && tok.res == null && !group,
          'is-group': group,
          'is-group-focus': group && focusGroup !== null && this.groupOf(tok) === focusGroup,
          'is-resolved': low && tok.res != null,
          'is-focus': this.same(this.focus, ref),
          'is-hot': this.same(this.hot, ref),
          'is-flash': Boolean(this.flashing[`${line.id}-${i}`]),
          'is-digit': Boolean(tok.digit),
        };
      },

      // The space after word i: part of the group's underline when both neighbours are in the same open group.
      sepClass(line, i) {
        if (!this.joinsGroup(line, i)) return {};
        const g = this.groupOf(line.tokens[i]);
        return { 'rv-sep': true, 'is-group': true, 'is-group-focus': g === this.focusedGroup };
      },

      gapClass(gap) {
        return { 'is-focus': Boolean(this.focus && this.focus.gap === gap.id), 'is-hot': Boolean(this.hot && this.hot.gap === gap.id) };
      },

      boxClass(line, tok, i) {
        const ref = { lineId: line.id, index: i };
        return {
          'is-open': this.isUnresolved(tok) && !this.inOpenGroup(tok),
          'is-group': this.inOpenGroup(tok),
          'is-hot': this.same(this.hot, ref) || this.same(this.focus, ref),
          'is-weak': tok.bq === 'weak', // the alignment is unsure of this box: drawn dashed
        };
      },

      // D32, D74: what the line is in the book: «محتوى» · «عنوان رئيسي» · «عنوان فرعي» · «شعر» · «حاشية». The
      // request carries the effective choice (`set_line_role` maps it onto the stored role: on a footnote-region
      // line «حاشية» is `body` and «محتوى» is `main`); undo restores it.
      lineRole(line) { return line ? effectiveRole(line.role, line.region_kind, line.kind) : 'body'; },
      lineKindOf(line) { return line ? line.kind || lineKind(line.role, line.region_kind) : 'body'; },
      setRole(line, choice) {
        this.menuFor = null;
        if (!line || !this.editable || this.lineRole(line) === choice) return Promise.resolve(false);
        const before = { role: line.role || 'body', kind: line.kind };
        line.role = storedRole(choice, line.region_kind);
        line.kind = lineKind(line.role, line.region_kind);
        return this.request(() => api(fill(this.urls.role, line.id), { method: 'POST', body: { role: choice, v: this.versionOf(line) } }), {
          apply: (data) => { if (data.line) this.replaceLine(data.line); },
          rollback: () => { const l = this.lineById(line.id); if (l) { l.role = before.role; l.kind = before.kind; } },
          retry: () => this.setRole(this.lineById(line.id), choice),
        });
      },

      // The line menu's role for its lines: one line (`setRole`, with its version) or a ⇧-click range
      // (`set_roles`, one revision per line in one batch, so one ⌘Z undoes it). The lines already of that kind
      // are left out of the request; none left, nothing is sent.
      setMenuRole(line, choice) {
        const lines = this.menuLines(line);
        if (lines.length <= 1) return this.setRole(line, choice);
        this.menuFor = null;
        const changing = lines.filter((l) => this.lineRole(l) !== choice);
        if (!this.editable || !changing.length) return Promise.resolve(false);
        const before = changing.map((l) => ({ id: l.id, role: l.role || 'body', kind: l.kind }));
        changing.forEach((l) => { l.role = storedRole(choice, l.region_kind); l.kind = lineKind(l.role, l.region_kind); });
        const body = { line_ids: changing.map((l) => l.id), role: choice };
        return this.request(() => api(this.urls.roles, { method: 'POST', body }), {
          apply: (data) => {
            (data.lines || []).forEach((l) => this.replaceLine(l));
            this.clearRange();
          },
          rollback: () => before.forEach((b) => { const l = this.lineById(b.id); if (l) { l.role = b.role; l.kind = b.kind; } }),
          retry: () => this.setMenuRole(this.lineById(line.id), choice),
        });
      },

      // The menu's check: the role of the menu's lines when they share one, else none.
      menuRoleChecked(line, value) {
        const lines = this.menuLines(line);
        return lines.length > 0 && lines.every((l) => this.lineRole(l) === value);
      },

      // The quiet tag after the line: headings and verse by name, a body-region line made a note «حاشية», and a
      // footnote-region line pulled into the body «محتوى» (`main`).
      roleLabel(line) {
        if (!line) return '';
        const role = line.role || 'body';
        if (role === 'main') return 'محتوى';
        const found = ROLES.find((r) => r.value === role);
        return found && found.value !== 'body' ? found.label : '';
      },

      lineClass(line) {
        return {
          'is-heading': line.role === 'heading',
          'is-subheading': line.role === 'subheading',
          'is-verse': line.role === 'verse',
          'is-current': this.currentLineId === line.id,
          'is-reviewed': Boolean(line.is_reviewed),
          'is-manual': Boolean(line.is_manual),
          'is-editing': Boolean(this.edit && this.edit.lineId === line.id),
          'is-footnote': this.lineKindOf(line) === 'footnote',
          'is-selected': this.inRange(line),
        };
      },

      // «الحواشي» over the first note line of a run (the line's kind, D74: a role can make or unmake a note).
      showGroup(line, li) {
        const prev = li > 0 ? this.lines[li - 1] : null;
        return this.lineKindOf(line) === 'footnote' && (!prev || this.lineKindOf(prev) !== 'footnote');
      },

      // ------------------------------------------------------------ lines: edit / insert / delete
      startEdit(target) {
        const line = target || this.currentLine;
        if (!line || !this.editable) return;
        this.closePop();
        this.insert = null;
        this.menuFor = null;
        this.edit = { lineId: line.id, text: line.text || (line.tokens || []).map((t) => t.t).join(' ') };
        this.tick(() => this.focusEditor());
      },

      cancelEdit() { this.edit = null; this.insert = null; },

      // Only one line editor exists at a time; it is found by class because the three templates that render
      // one (edit, insert after a line, insert first) would otherwise fight over a single shared x-ref.
      focusEditor() {
        const root = this.$root && this.$root.querySelector ? this.$root : (hasDOM ? document : null);
        const el = root && root.querySelector ? root.querySelector('textarea.rv-editor') : null;
        if (!el) return;
        this.autosize(el);
        el.focus();
        try { el.setSelectionRange(el.value.length, el.value.length); } catch (_) { /* fine */ }
      },

      autosize(el) {
        if (!el || !el.style) return;
        el.style.height = 'auto';
        el.style.height = `${el.scrollHeight}px`;
      },

      saveEdit() {
        const draft = this.edit;
        if (!draft) return Promise.resolve(false);
        const line = this.lineById(draft.lineId);
        const text = splitWords(draft.text).join(' ');
        this.edit = null;
        if (!line) return Promise.resolve(false);
        if (!text) { this.toast('السطر فارغ؛ احذفه من قائمة السطر بدلًا من ذلك'); return Promise.resolve(false); }
        if (text === (line.text || '')) return Promise.resolve(false);

        const before = JSON.parse(JSON.stringify(line));
        const beforeCounts = Object.assign({}, this.counts);
        const words = splitWords(text);
        line.text = text;
        line.tokens = words.map((w, i) => (before.tokens[i] && before.tokens[i].t === w ? before.tokens[i] : plainToken(w)));
        this.recount();
        this.hotLine = line.id;

        return this.request(() => api(fill(this.urls.edit, line.id), { method: 'POST', body: { text, v: this.versionOf(line) } }), {
          apply: (data, seq) => {
            if (data.line) this.replaceLine(data.line);
            this.applyApiCounts(data.counts, this.lineById(line.id));
            this.offerElsewhere(data.elsewhere, seq);
          },
          rollback: () => {
            const i = this.lines.findIndex((l) => l.id === line.id);
            if (i >= 0) this.lines.splice(i, 1, before);
            this.setCounts(beforeCounts);
          },
          retry: () => { this.edit = { lineId: line.id, text }; return this.saveEdit(); },
        });
      },

      startInsert(target) {
        if (!this.editable) return;
        const anchor = target || this.currentLine || this.lines[this.lines.length - 1] || null;
        this.closePop();
        this.edit = null;
        this.menuFor = null;
        this.insert = { afterId: anchor ? anchor.id : null, text: '' };
        this.tick(() => this.focusEditor());
      },

      saveInsert() {
        const draft = this.insert;
        if (!draft) return Promise.resolve(false);
        const text = splitWords(draft.text).join(' ');
        this.insert = null;
        if (!text) return Promise.resolve(false);
        const idx = this.lines.findIndex((l) => l.id === draft.afterId);
        const anchor = idx >= 0 ? this.lines[idx] : null;
        const temp = {
          id: `tmp-${Date.now()}`, order: anchor ? anchor.order + 1 : 0,
          region_id: anchor ? anchor.region_id : null, region_kind: anchor ? anchor.region_kind : 'body',
          bbox: null, text, ocr_text: '', is_manual: true, is_reviewed: false, n_low: 0,
          tokens: splitWords(text).map(plainToken),
        };
        this.lines.splice(idx + 1, 0, temp);
        this.renumber();

        return this.request(() => api(this.urls.insert, { method: 'POST', body: { after: draft.afterId, text } }), {
          apply: (data) => {
            const i = this.lines.findIndex((l) => l.id === temp.id);
            if (data.line) { if (i >= 0) this.lines.splice(i, 1, data.line); else this.lines.push(data.line); }
            else if (i >= 0) this.lines.splice(i, 1);
            if (Array.isArray(data.lines)) this.reorder(data.lines);
            this.applyApiCounts(data.counts);
          },
          rollback: () => {
            const i = this.lines.findIndex((l) => l.id === temp.id);
            if (i >= 0) this.lines.splice(i, 1);
            this.renumber();
          },
          retry: () => { this.insert = { afterId: draft.afterId, text }; return this.saveInsert(); },
        });
      },

      renumber() { this.lines.forEach((line, i) => { line.order = i; }); },

      reorder(list) {
        const orders = new Map(list.map((x) => [x.id, x.order]));
        this.lines.forEach((line) => { if (orders.has(line.id)) line.order = orders.get(line.id); });
        this.lines.sort((a, b) => a.order - b.order);
      },

      removeLine(line) {
        if (!line || !this.editable) return Promise.resolve(false);
        const i = this.lines.findIndex((l) => l.id === line.id);
        if (i < 0) return Promise.resolve(false);
        const snapshot = this.lines[i];
        const beforeCounts = Object.assign({}, this.counts);
        this.menuFor = null;
        this.lines.splice(i, 1);
        this.renumber();
        this.recount();
        if (this.focus && this.focus.lineId === line.id) { this.focus = null; this.closePop(); }
        if (this.hotLine === line.id) this.hotLine = null;

        return this.request(() => api(fill(this.urls.delete, line.id), { method: 'POST', body: { v: this.versionOf(line) } }), {
          apply: (data, seq) => { this.applyApiCounts(data.counts); this.showUndoToast('حُذف السطر', seq); },
          rollback: () => {
            this.lines.splice(Math.min(i, this.lines.length), 0, snapshot);
            this.renumber();
            this.setCounts(beforeCounts);
          },
          retry: () => this.removeLine(snapshot),
        });
      },

      // Offer «تراجع» for the action that just landed. `seq` is that request's number: when a later action has
      // been sent since, the newest revision is no longer this one and the offer would undo the wrong thing.
      // `extra`: {elsewhere} (the chip «159 موضعًا آخر… · تصحيحها…»), {batch, detail, link} (a fix everywhere: its
      // «تراجع» undoes the batch, `link` the edited book's pre-filled find & replace)
      showUndoToast(message, seq, extra) {
        if (seq !== undefined && seq !== this.actionSeq) return;
        try { const shared = Alpine.store('toast'); if (shared && shared.hide) shared.hide(); } catch (_) { /* no store */ }
        clearTimeout(this.undoTimer);
        this.undoToast = Object.assign({ message }, extra || {});
        this.undoTimer = setTimeout(() => { this.undoToast = null; }, UNDO_TOAST_MS);
      },
      // the toast's «تراجع»: a fix everywhere undoes its batch, anything else the page's newest revision
      undoFromToast() {
        const t = this.undoToast;
        if (t && t.batch) return this.undoFix(t.batch);
        return this.undo();
      },

      dismissUndo() {
        clearTimeout(this.undoTimer);
        if (this.undoToast) this.undoToast = null;
      },

      // Server-side undo (LineRevision): the whole payload comes back and the screen re-renders from it.
      undo() {
        if (!this.canEdit || this.loading) return Promise.resolve(false);
        this.dismissUndo();
        this.edit = null;
        this.insert = null;
        this.closePop();
        return this.request(() => api(this.urls.undo, { method: 'POST' }), {
          apply: (payload) => { this.focus = null; this.apply(payload); this.pulse(); },
          soft: true,
        });
      },

      // ------------------------------------------------------------ saving
      // Runs `send` after the previous request (actions stay in order). `apply(data)` on success,
      // `rollback()` + save chip error with `retry` on failure; `onFail(res)` may claim a failure
      // (returns true), `soft` failures only toast (nothing to roll back).
      request(send, handlers) {
        const h = handlers || {};
        const gen = this.gen;
        const seq = ++this.actionSeq;
        const where = { type: 'review', book: this.book.id || this.page.book_id || null, page: this.page.number };
        this.dismissUndo(); // a newer action makes the undo offer stale
        this.arrived = false; // the page is touched: N moves on from it again
        this.save.pending += 1;
        this.save.state = 'saving';
        this.syncBar();
        const run = this.queue.then(() => send()).then((res) => {
          this.save.pending = Math.max(0, this.save.pending - 1);
          if (res.ok) announce(where); // saved on the server, whichever page is on screen now
          if (gen !== this.gen) {
            // the page this belonged to was swapped away meanwhile: nothing here to apply or roll back
            if (!res.ok) this.toast(res.message);
            if (!this.save.pending) this.settle('idle');
            return null;
          }
          if (res.ok) {
            if (h.apply) h.apply(res.data || {}, seq);
            if (!this.save.pending) this.settle('saved');
            return res.data || {};
          }
          if (h.onFail && h.onFail(res)) { if (!this.save.pending) this.settle('idle'); return null; }
          if (res.status === 409 && res.data && res.data.line) {
            // the line changed elsewhere (another tab): show it as it is now instead of offering a retry
            if (h.rollback) h.rollback();
            this.replaceLine(res.data.line);
            this.recount();
            if (!this.save.pending) this.settle('idle');
            this.toast(res.message || 'تغيّر هذا السطر في نافذة أخرى، فعُرض كما هو الآن.');
            return null;
          }
          if (h.rollback) h.rollback();
          if (h.soft) { this.settle('idle'); this.toast(res.message); return null; }
          if (h.retry) this.save.failed.push({ message: res.message, retry: h.retry });
          this.settle('error');
          this.toast(res.message);
          return null;
        });
        this.queue = run.catch(() => {});
        return run;
      },

      // The chip never says «محفوظ» while an earlier action is still unsaved: the failures outrank a later success.
      settle(state) {
        this.save.state = state !== 'saving' && this.save.failed.length ? 'error' : state;
        this.syncBar();
        clearTimeout(this.saveTimer);
        if (this.save.state === 'saved') {
          this.saveTimer = setTimeout(() => { if (this.save.state === 'saved') { this.save.state = 'idle'; this.syncBar(); } }, SAVED_MS);
        }
      },

      // Replay every failed action in its original order; one that fails again rejoins the list.
      retrySave() {
        const failed = this.save.failed.slice();
        this.save.failed = [];
        this.settle('idle');
        failed.forEach((f) => f.retry());
      },

      // ------------------------------------------------------------ approve / reopen / pages
      approve(force) {
        if (!this.canEdit || !this.ready || this.isReviewed || this.approving || this.loading) return Promise.resolve(false);
        if (!force && this.counts.unresolved > 0) {
          this.openDialog(this.counts.unresolved);
          return Promise.resolve(false);
        }
        this.clearRange();
        this.dialog.open = false;
        this.approving = true;
        this.closePop();
        return this.request(() => api(this.urls.approve, { method: 'POST', body: Object.assign({ force: Boolean(force) }, this.originBody) }), {
          apply: (data) => {
            this.approving = false;
            this.page.status = data.status || 'reviewed';
            this.page.is_reviewed = true;
            this.page.status_label = 'مُراجَعة';
            this.book.reviewed_pages = (Number(this.book.reviewed_pages) || 0) + 1;
            this.markFilm({ is_reviewed: true, status: this.page.status });
            if (data.next_step !== undefined) this.nextStep = data.next_step || null;
            this.syncBar();
            stagesChanged(this.book.id || this.page.book_id); // the stage bar: «المراجعة» counted one more page
            this.celebrate(data);
          },
          onFail: (res) => {
            this.approving = false;
            if (res.status === 409) {
              this.openDialog((res.data && res.data.unresolved) || this.counts.unresolved, res.data);
              return true;
            }
            return false;
          },
        });
      },

      // D73: the dialog counts the open words and the open suggestions (groups of added words and gaps). The
      // split comes from the server's 409 when it gives one (`words`, `groups`, `gaps`), else from the page.
      openDialog(count, detail) {
        const total = Number(count) || 0;
        const d = detail || {};
        const t = this.tally();
        const given = d.words != null || d.groups != null || d.gaps != null;
        const gaps = given ? Number(d.gaps) || 0 : Math.min(total, t.gaps);
        const suggested = given ? (Number(d.groups) || 0) + gaps : Math.min(total, t.groups + t.gaps);
        const words = given && d.words != null ? Number(d.words) || 0 : Math.max(0, total - suggested);
        this.dialog = { open: true, count: total, words, suggested, gaps };
        this.tick(() => { const el = this.$refs.dialogCancel; if (el) el.focus(); });
      },

      // = review.services.blocked_message: open words only «بقيت 4 كلمة غير محسومة. اعتماد الصفحة رغم ذلك؟» (review's
      // sentence as it always read); with suggestions «بقيت 3 علامات: كلمتان غير محسومتين وكلمات مقترحة لم تُحسم.
      // اعتماد الصفحة رغم ذلك؟» (Arabic count forms, Western digits).
      get dialogTitle() {
        const d = this.dialog;
        const ask = 'اعتماد الصفحة رغم ذلك؟';
        if (!d.suggested) return `بقيت ${d.count} كلمة غير محسومة. ${ask}`;
        const parts = [];
        if (d.words) parts.push(arCount(d.words, OPEN_WORDS));
        parts.push('كلمات مقترحة لم تُحسم');
        return `بقيت ${arCount(d.count, MARKS)}: ${parts.join(' و')}. ${ask}`;
      },

      // Stamp (400 ms), then the next page needing review slides in from the reading direction (marked `arrived`,
      // the N rule). Opened from the book page, the page stays and the pane offers the way back (the detour, §5.3);
      // with no page left to review the end panel names the next step.
      celebrate(data) {
        const d = data || {};
        this.stamp = true;
        const number = this.page.number;
        const after = () => {
          this.stamp = false;
          // the server knows best: a next step means no page is left (the filmstrip may be older)
          if (d.next_step) { this.showEnd(d.next_step); return; }
          const item = d.next_payload_url ? null : this.nextFromFilm();
          const payloadUrl = d.next_payload_url || this.payloadUrlFor(item && item.id);
          if (!payloadUrl) { this.showEnd(d.next_step); return; }
          if (this.fromBook && this.back) { this.showDetour(number, true); return; }
          this.swapTo(payloadUrl, d.next_review_url, this.turnDir(item), { arrived: true });
        };
        if (this.reduced || !hasDOM) after(); else setTimeout(after, 800);
      },

      // ---- after an approval (D76, §5.3): the detour and the end panel, in the lines pane
      showDetour(number, hasNext) {
        this.after = { kind: 'detour', number, backUrl: this.back ? this.back.url : this.nav.dashboard_url, hasNext: Boolean(hasNext) };
        this.liveMessage = `اعتُمدت الصفحة ${number}.`;
        this.focusAfter();
      },
      // `step` = next_step (approve's answer, else the payload's); the words come from NEXT_STEPS when the server
      // sends only its key.
      showEnd(step) {
        this.after = this.endPanel(step === undefined ? this.nextStep : step);
        this.liveMessage = this.after.heading;
        this.focusAfter();
      },
      // next_step = {state, heading, summary, title, text, button, url, stay} (the contract's words, with the numbers);
      // NEXT_STEPS stands in for a field it leaves out. The ✓ shows once this page is reviewed (N on the last page
      // still waiting reads «هذه آخر صفحة بانتظار المراجعة» without it).
      endPanel(raw) {
        const s = raw || {};
        const given = s.state || s.key;
        const key = NEXT_STEPS[given] ? given : (s.url ? 'book' : 'processing');
        const words = NEXT_STEPS[key];
        const total = Number(this.book.total_pages) || 0;
        const summary = s.summary !== undefined ? String(s.summary || '') : total ? arCount(total, PAGES) : '';
        return {
          kind: 'end',
          key,
          done: this.isReviewed,
          heading: String(s.heading || words.heading || 'رُوجعت كل الصفحات'),
          summary,
          step: String(s.title || words.step || ''),
          text: String(s.text || words.text || ''),
          button: String(s.button || words.button || ''),
          url: String(s.url || (key === 'processing' ? this.nav.dashboard_url || '' : '')),
          stay: String(s.stay || 'البقاء في المراجعة'),
        };
      },
      focusAfter() {
        this.closePop();
        this.focus = null;
        if (!hasDOM) return;
        this.tick(() => {
          const el = (this.$refs && this.$refs.afterPrimary) || document.querySelector('[data-after] .btn-primary');
          if (el && el.focus) el.focus({ preventScroll: true });
        });
      },
      leaveAfter() { this.after = null; this.liveMessage = ''; },


      reopen() {
        if (!this.canEdit || !this.isReviewed || this.loading) return Promise.resolve(false);
        return this.request(() => api(this.urls.reopen, { method: 'POST' }), {
          apply: (data) => {
            // The API answers the full review payload; the local patch is the fallback.
            if (data && data.page) this.apply(data);
            else {
              this.page.status = 'ocr_done';
              this.page.is_reviewed = false;
              this.page.status_label = 'تم التعرّف';
              this.book.reviewed_pages = Math.max(0, (Number(this.book.reviewed_pages) || 0) - 1);
            }
            this.markFilm({ is_reviewed: false, status: 'ocr_done' });
            this.syncBar();
          },
        });
      },

      markFilm(patch) {
        const item = this.film.items.find((p) => p.number === this.page.number);
        if (item) Object.assign(item, patch, { n_unresolved: this.counts.unresolved });
      },

      // The next page still to review: the first one after this page, else the first open page of the book.
      nextFromFilm() {
        const items = this.film.items || [];
        const open = (p) => p.id && p.status === 'ocr_done' && !p.is_reviewed && p.number !== this.page.number;
        return items.find((p) => open(p) && p.number > this.page.number) || items.find(open) || null;
      },

      // Reading direction of a turn to `item`: 1 forward (also when unknown), -1 back to an earlier page.
      turnDir(item) { return item && item.number < this.page.number ? -1 : 1; },

      // another page's payload, asked with the origin (its nav then leads back to the same place)
      payloadUrlFor(id) {
        if (!id || !this.urls.payload) return null;
        return this.withOrigin(String(this.urls.payload).replace(/\/pages\/\d+\//, `/pages/${id}/`));
      },

      // The address of page `number`, with this address's query (`?from=book&at=12` survives a page swap, §5.3).
      pageUrlFor(number) {
        const item = this.film.items.find((p) => p.number === number);
        if (item && item.url) return this.filmHref(item);
        if (!hasDOM) return null;
        return window.location.pathname.replace(/\/\d+\/?$/, `/${number}/`) + window.location.search;
      },
      filmHref(p) { return this.withOrigin((p && p.url) || ''); },

      // Fetch another page's payload and swap it in without a reload (old page leaves toward the end
      // side, the new one arrives from the start of the reading direction); the URL follows.
      // `dir`: 1 turns forward (the default), -1 back to an earlier page (the motion is mirrored).
      async swapTo(payloadUrl, fallbackUrl, dir, opts) {
        if (!payloadUrl) return false;
        const arrived = Boolean(opts && opts.arrived);
        const back = dir < 0;
        this.loading = true;
        this.slide = this.reduced || !hasDOM ? '' : (back ? 'out-back' : 'out');
        clearTimeout(this.pollTimer);
        // every queued action lands on the page it belongs to before the next page is fetched
        await this.queue.catch(() => {});
        const res = await api(payloadUrl);
        if (!res.ok || !res.data || !res.data.page) {
          this.loading = false;
          this.slide = '';
          if (fallbackUrl && hasDOM) { window.location.assign(fallbackUrl); return false; }
          this.toast(res.message);
          return false;
        }
        const land = () => {
          this.gen += 1; // responses still in flight for the old page are dropped when they arrive
          this.focus = null; this.hot = null; this.hotLine = null; this.closePop();
          this.edit = null; this.insert = null; this.menuFor = null; this.dialog.open = false; this.dismissUndo();
          this.range = { anchor: null, ids: [] };
          this.after = null;
          this.jumping = false;
          this.arrived = arrived;
          if (this.save.failed.length) {
            // their retries target lines of the page being left: say so instead of keeping a chip that lies
            this.save.failed = [];
            this.toast('لم تُحفظ بعض الإجراءات في الصفحة التي غادرتها');
          }
          if (this.save.state === 'error') this.settle(this.save.pending ? 'saving' : 'idle');
          this.unmountDecode();
          this.apply(res.data);
          this.shown = this.counts.resolved;
          this.zoomReset(false);
          if (hasDOM) {
            const dest = this.pageUrlFor(res.data.page.number) || fallbackUrl;
            try {
              if (dest && window.history && window.history.replaceState) window.history.replaceState(null, '', dest);
              document.title = `مراجعة · صفحة ${res.data.page.number} · ${this.book.title || ''} · نسّاخ`;
            } catch (_) { /* fine */ }
          }
          this.loading = false;
          this.slide = this.reduced || !hasDOM ? '' : (back ? 'in-back' : 'in');
          const settle = () => {
            this.slide = '';
            this.enter();
            this.centreFilm();
            this.mountDecode();
            if (this.isPending) this.schedulePoll();
          };
          if (hasDOM) requestAnimationFrame(() => requestAnimationFrame(settle)); else settle();
        };
        if (this.reduced || !hasDOM) land(); else setTimeout(land, 220);
        return true;
      },

      goTo(item) {
        if (!item || item.number === this.page.number) return;
        const payloadUrl = this.payloadUrlFor(item.id);
        if (payloadUrl) this.swapTo(payloadUrl, item.url, this.turnDir(item));
        else if (item.url && hasDOM) window.location.assign(item.url);
      },

      goNextPage() {
        const item = this.film.items.find((p) => p.number === this.page.number + 1);
        if (item) { this.goTo(item); return; }
        if (this.nav.next_url && hasDOM) window.location.assign(this.nav.next_url);
        else this.toast('هذه آخر صفحة');
      },

      goPrevPage() {
        const item = this.film.items.find((p) => p.number === this.page.number - 1);
        if (item) { this.goTo(item); return; }
        if (this.nav.prev_url && hasDOM) window.location.assign(this.nav.prev_url);
        else this.toast('هذه أول صفحة');
      },

      // Home / End: the book's first (dir -1) or last (dir 1) page, from the filmstrip.
      goEdgePage(dir) {
        const items = (this.film.items || []).filter((p) => p && p.number);
        if (!items.length) return false;
        const numbers = items.map((p) => p.number);
        const target = dir < 0 ? Math.min(...numbers) : Math.max(...numbers);
        if (target === this.page.number) { this.toast(dir < 0 ? 'هذه أول صفحة' : 'هذه آخر صفحة'); return false; }
        this.goTo(items.find((p) => p.number === target));
        return true;
      },

      // N: the next page waiting for review. The N rule (§5.3): on a page approval brought in, untouched and still
      // to review, N says so and stays (N right after A never skips a page). With none left, the end panel.
      goNextReview() {
        if (this.arrived && this.ready && !this.isReviewed && !this.loading) { this.toast('هذه هي الصفحة التالية للمراجعة'); return; }
        const item = this.nextFromFilm();
        const payloadUrl = this.payloadUrlFor(item && item.id);
        if (payloadUrl) { this.leaveAfter(); this.swapTo(payloadUrl, this.nav.next_review_url, this.turnDir(item)); return; }
        if (this.film.state === 'ready') {
          if (this.ready && !this.isReviewed) { this.toast('هذه آخر صفحة بانتظار المراجعة'); return; }
          this.showEnd();
          return;
        }
        if (this.nav.next_review_url && hasDOM) window.location.assign(this.nav.next_review_url);
        else this.toast('لا صفحات بانتظار المراجعة');
      },

      // ---- G: the pager becomes a jump field (a page number in any digits; Enter goes, Esc leaves)
      startJump() {
        this.jumping = true;
        this.syncBar();
        if (!hasDOM) return;
        this.tick(() => { const el = document.getElementById('rv-jump'); if (el && el.focus) { el.focus(); if (el.select) el.select(); } });
      },
      endJump() { if (!this.jumping) return; this.jumping = false; this.syncBar(); },
      jumpTo(value) {
        const digits = String(value || '').replace(/[٠-٩]/g, (d) => String(d.charCodeAt(0) - 0x0660)).replace(/[۰-۹]/g, (d) => String(d.charCodeAt(0) - 0x06f0)).replace(/\D/g, '');
        const n = digits ? parseInt(digits, 10) : NaN;
        this.endJump();
        const item = Number.isFinite(n) ? this.film.items.find((p) => p.number === n) : null;
        if (!item) { this.toast('لا صفحة بهذا الرقم'); return false; }
        if (n !== this.page.number) { this.leaveAfter(); this.goTo(item); }
        return true;
      },
      // O: «المعالَجة / الأصل»
      toggleScan() {
        if (this.tab === 'scan') { this.setTab('display'); return true; }
        if (!this.hasScan) { this.toast('لا صورة أصلية لهذه الصفحة'); return false; }
        this.setTab('scan');
        return true;
      },

      // «صفحة 3 · 3 علامات · قراءة واحدة»: the open items (words, groups and gaps, D73) and a single reader.
      thumbTitle(p) {
        const parts = [`صفحة ${p.number}`];
        if (p.is_reviewed) parts.push('مُراجَعة');
        else if (p.n_unresolved > 0) parts.push(arCount(p.n_unresolved, MARKS));
        const readers = READERS[p.readers];
        if (readers) parts.push(readers.label);
        return parts.join(' · ');
      },
      readersOf(p) { return p && READERS[p.readers] ? p.readers : ''; },

      // ------------------------------------------------------------ filmstrip
      async loadFilm() {
        if (!this.urls.filmstrip || this.film.state === 'loading') return;
        this.film.state = 'loading';
        const res = await api(this.urls.filmstrip);
        if (!res.ok) { this.film.state = 'error'; return; }
        const items = Array.isArray(res.data) ? res.data : (res.data && (res.data.pages || res.data.items)) || [];
        this.film = { items, state: 'ready' };
        this.tick(() => this.centreFilm());
      },

      centreFilm() {
        if (!hasDOM) return;
        const host = this.$refs && this.$refs.film;
        const el = host && host.querySelector('.is-current');
        if (el && el.scrollIntoView) el.scrollIntoView({ inline: 'center', block: 'nearest', behavior: this.reduced ? 'auto' : 'smooth' });
      },

      // ------------------------------------------------------------ pending / error states
      schedulePoll() {
        if (!hasDOM) return;
        clearTimeout(this.pollTimer);
        this.pollTimer = setTimeout(() => this.poll(), POLL_MS);
      },

      async poll() {
        if (!this.isPending || document.hidden) { if (this.isPending) this.schedulePoll(); return; }
        const gen = this.gen;
        const res = await api(this.urls.payload);
        if (gen !== this.gen) return; // another page landed meanwhile and schedules its own poll
        if (res.ok && res.data && res.data.page) {
          const p = res.data.page;
          if (p.text_state === 'final' || p.status === 'error') { this.landFinal(res.data); return; }
          this.page = p;
          this.takeProvisional(res.data.provisional_lines);
        }
        this.schedulePoll();
      },

      // A poll of a pending page: Tesseract's lines when they arrive (the noise gives way to them) or change.
      takeProvisional(lines) {
        if (!Array.isArray(lines)) return;
        const sig = (list) => list.map((line) => (line.words || []).join(' ')).join('\n');
        if (sig(lines) === sig(this.provisional)) return;
        this.provisional = lines;
        if (!this.decodeEl || !hasDOM || !window.NassakhDecode) return;
        try { window.NassakhDecode.attach(this.decodeEl, this.decodeOptions()); } catch (_) { /* the old text stays */ }
      },

      // The pending text (owner review 2026-10-03): Tesseract's lines once it has read the page, else lines of
      // noise; a reading cursor walks them, and `readingLine` mirrors it as the band over the scan.
      decodeOptions() {
        const onLine = (k) => { this.readingLine = k; if (k >= 0) this.readingLast = k; };
        if (this.provisional.length) return { mode: 'provisional', lines: this.provisional, cursor: true, onLine };
        this.noiseRows = this.regions.length ? 14 : 10;
        return { mode: 'noise', lines: this.noiseRows, cursor: true, onLine };
      },

      // The band over the scan while the page is pending: on the line the text's cursor is on, at Tesseract's box;
      // over noise (nothing read yet) it steps down the page's text regions, one row of the noise at a time. Between
      // two passes it fades out where it was.
      get readingStyle() {
        const k = this.readingLast;
        if (!this.isPending || !(k >= 0)) return 'opacity:0;';
        const shown = this.readingLine >= 0 ? 1 : 0;
        let top;
        let height;
        if (this.provisional.length) {
          const box = this.provisional[k] && this.provisional[k].bbox;
          if (!isBox(box)) return 'opacity:0;';
          const h = box[3] - box[1];
          top = box[1] - 0.3 * h;
          height = 1.6 * h;
        } else {
          const W = this.image.width || 0;
          const H = this.image.height || 0;
          const text = this.regions.filter((r) => isBox(r.bbox) && r.kind !== 'running_header' && r.kind !== 'page_number');
          const area = W && H && text.length ? text.map((r) => r.bbox).reduce((a, b) => unionBox(a, b)) : null;
          const y0 = area ? area[1] / H : 0.08;
          const y1 = area ? area[3] / H : 0.92;
          const pitch = (y1 - y0) / Math.max(1, this.noiseRows || 10);
          top = y0 + k * pitch + 0.1 * pitch;
          height = 0.8 * pitch;
        }
        return `top:${(100 * top).toFixed(3)}%; height:${(100 * height).toFixed(3)}%; opacity:${shown};`;
      },

      // The models finished while we watched: the noise resolves in a right-to-left wave, then the
      // real lines take over (staggered in).
      landFinal(data) {
        const el = this.decodeEl;
        const gen = this.gen;
        const finish = () => {
          if (gen !== this.gen) return; // the wave outlived a page swap
          this.unmountDecode();
          this.apply(data);
          this.shown = this.counts.resolved;
          this.tick(() => this.enter());
        };
        const decode = hasDOM && window.NassakhDecode;
        if (el && decode && data.page.text_state === 'final' && !this.reduced && Array.isArray(data.lines) && data.lines.length) {
          try {
            decode.resolve(el, {
              lines: data.lines.map((l) => ({ region_kind: l.region_kind, tokens: (l.tokens || []).map((t) => ({ t: t.t, conf: t.conf, res: t.res })) })),
              onDone: finish,
            });
            return;
          } catch (_) { /* fall through */ }
        }
        finish();
      },

      mountDecode() {
        if (!hasDOM || !window.NassakhDecode) return;
        const el = this.$refs && this.$refs.noise;
        if (!el || !this.isPending || this.decodeEl === el) return;
        this.decodeEl = el;
        try { window.NassakhDecode.attach(el, this.decodeOptions()); } catch (_) { this.decodeEl = null; }
      },

      unmountDecode() {
        if (this.decodeEl && hasDOM && window.NassakhDecode) { try { window.NassakhDecode.detach(this.decodeEl); } catch (_) { /* fine */ } }
        this.decodeEl = null;
        this.readingLine = -1;
        this.readingLast = -1;
      },

      // ------------------------------------------------------------ scan viewer
      measure() {
        const el = this.$refs && this.$refs.scan;
        if (!el) return;
        this.pane = { w: el.clientWidth, h: el.clientHeight };
        this.clampPan();
      },

      observe() {
        if (!hasDOM || typeof window.ResizeObserver !== 'function') return;
        const el = this.$refs && this.$refs.scan;
        if (!el) return;
        this.ro = new ResizeObserver(() => this.measure());
        this.ro.observe(el);
      },

      // Base (scale 1) size of the page sheet. Fit height: the whole page height fits the pane; fit width: the page
      // width fits the pane. Word boxes and the line band are % of the sheet, so they follow any size unchanged.
      get sheetW() {
        const W = this.image.width;
        const H = this.image.height;
        if (this.fit === 'height' && W && H && this.pane.h > 0) return (this.pane.h * W) / H;
        return this.pane.w;
      },
      get sheetH() { return this.image.width ? (this.sheetW * this.image.height) / this.image.width : this.pane.h; },
      get sheetStyle() {
        const w = this.sheetW > 0 ? `width:${Math.round(this.sheetW)}px; ` : '';
        return `${w}transform: translate(${Math.round(this.zoom.x)}px, ${Math.round(this.zoom.y)}px) scale(${this.zoom.scale});`;
      },
      get fitLabel() { return this.fit === 'height' ? 'ملاءمة الارتفاع' : 'ملاءمة العرض'; },

      setFit(mode) {
        const next = mode === 'width' ? 'width' : 'height';
        if (next === this.fit) { this.zoomReset(); return; }
        this.fit = next;
        storage.set(FIT_KEY, next);
        this.zoomReset();
        // keep the focused word in view in the new layout
        const tok = this.focused;
        if (tok && tok.bbox) this.tick(() => this.panTo(tok.bbox));
      },

      clampPan() {
        const z = this.zoom;
        const w = this.sheetW * z.scale;
        const h = this.sheetH * z.scale;
        z.x = w <= this.pane.w ? (this.pane.w - w) / 2 : clamp(z.x, this.pane.w - w, 0);
        z.y = h <= this.pane.h ? (this.fit === 'height' ? (this.pane.h - h) / 2 : 0) : clamp(z.y, this.pane.h - h, 0);
      },

      zoomBy(factor, cx, cy) {
        const z = this.zoom;
        const scale = clamp(z.scale * factor, ZOOM_MIN, ZOOM_MAX);
        const px = cx == null ? this.pane.w / 2 : cx;
        const py = cy == null ? this.pane.h / 2 : cy;
        const k = scale / z.scale;
        z.x = px - (px - z.x) * k;
        z.y = py - (py - z.y) * k;
        z.scale = scale;
        this.clampPan();
      },

      zoomIn() { this.glide(); this.zoomBy(1.25); },
      zoomOut() { this.glide(); this.zoomBy(0.8); },
      zoomReset(animate) {
        if (animate !== false) this.glide();
        this.zoom = { scale: 1, x: 0, y: 0 };
        this.clampPan();
      },

      // Pinch / ctrl+wheel zooms around the cursor; a plain wheel pans (the sheet is taller than the pane).
      onWheel(ev) {
        const el = this.$refs && this.$refs.scan;
        const r = el && el.getBoundingClientRect ? el.getBoundingClientRect() : { left: 0, top: 0 };
        if (ev.ctrlKey || ev.metaKey) {
          this.zoomBy(Math.exp(-ev.deltaY * 0.01), ev.clientX - r.left, ev.clientY - r.top);
        } else {
          this.zoom.y -= ev.deltaY;
          this.zoom.x -= ev.deltaX;
          this.clampPan();
        }
      },

      pointerDist() {
        const pts = Array.from(this.pointers.values());
        if (pts.length < 2) return 1;
        return Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
      },

      onPointerDown(ev) {
        if (ev.pointerType === 'mouse' && ev.button !== 0) return;
        if (!this.pointers) this.pointers = new Map();
        this.pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
        this.dragMoved = false;
        if (this.pointers.size === 2) {
          this.pinch = { dist: this.pointerDist(), scale: this.zoom.scale };
          this.drag = null;
          return;
        }
        this.drag = { x: ev.clientX, y: ev.clientY, ox: this.zoom.x, oy: this.zoom.y, moved: false };
        const hit = ev.target && ev.target.closest ? ev.target.closest('.rv-box') : null;
        this.tapBox = hit && hit.dataset ? { line: String(hit.dataset.line), index: Number(hit.dataset.i) } : null;
        const el = ev.currentTarget;
        if (el && el.setPointerCapture) { try { el.setPointerCapture(ev.pointerId); } catch (_) { /* fine */ } }
      },

      onPointerMove(ev) {
        if (this.pointers && this.pointers.has(ev.pointerId)) this.pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
        if (this.pinch && this.pointers && this.pointers.size === 2) {
          const el = this.$refs && this.$refs.scan;
          const r = el && el.getBoundingClientRect ? el.getBoundingClientRect() : { left: 0, top: 0 };
          const pts = Array.from(this.pointers.values());
          const cx = (pts[0].x + pts[1].x) / 2 - r.left;
          const cy = (pts[0].y + pts[1].y) / 2 - r.top;
          const target = (this.pinch.scale * this.pointerDist()) / this.pinch.dist;
          this.zoomBy(target / this.zoom.scale, cx, cy);
          this.dragMoved = true;
          return;
        }
        if (!this.drag) return;
        const dx = ev.clientX - this.drag.x;
        const dy = ev.clientY - this.drag.y;
        this.zoom.x = this.drag.ox + dx;
        this.zoom.y = this.drag.oy + dy;
        this.clampPan();
        // A drag is a press that travelled past the slop and moved the page. A click on a trackpad wobbles a few
        // pixels, and a page that fits the pane cannot move at all: both stay taps (owner review 2026-10-03: a
        // wobbly click on the image neither opened its word nor closed the open menu).
        const panned = Math.abs(this.zoom.x - this.drag.ox) + Math.abs(this.zoom.y - this.drag.oy) >= 1;
        if (!this.drag.moved && panned && Math.hypot(dx, dy) > TAP_SLOP) { this.drag.moved = true; this.dragMoved = true; }
      },

      onPointerUp(ev) {
        if (this.pointers) this.pointers.delete(ev.pointerId);
        if (!this.pointers || this.pointers.size < 2) this.pinch = null;
        this.drag = null;
        // The drag's own click (dispatched right after this release) reads `dragMoved`; nothing later may. A flag
        // left set swallowed the next click outside an open word menu, wherever it was.
        if (this.dragMoved) setTimeout(() => { this.dragMoved = false; }, 0);
      },

      // Keep the focused word in view: glide the sheet so the word sits mid-pane when it is near an edge.
      panTo(bbox) {
        if (!bbox || !this.pane.w || !this.image.width) return;
        const z = this.zoom;
        const k = (this.sheetW / this.image.width) * z.scale; // display px per image px (sheet, not pane)
        const cx = ((bbox[0] + bbox[2]) / 2) * k + z.x;
        const cy = ((bbox[1] + bbox[3]) / 2) * k + z.y;
        const my = Math.min(96, this.pane.h * 0.22);
        const mx = 48;
        let dx = 0;
        let dy = 0;
        if (cy < my || cy > this.pane.h - my) dy = this.pane.h / 2 - cy;
        if (cx < mx || cx > this.pane.w - mx) dx = this.pane.w / 2 - cx;
        if (!dx && !dy) return;
        this.glide();
        z.x += dx;
        z.y += dy;
        this.clampPan();
      },

      glide() {
        if (this.reduced || !hasDOM) return;
        this.gliding = true;
        clearTimeout(this.glideTimer);
        this.glideTimer = setTimeout(() => { this.gliding = false; }, 300);
      },

      boxStyle(bbox) {
        const W = this.image.width || 1;
        const H = this.image.height || 1;
        const pad = Math.max(1, (bbox[3] - bbox[1]) * 0.12);
        return `left:${(100 * (bbox[0] - pad)) / W}%; top:${(100 * (bbox[1] - pad)) / H}%; width:${(100 * (bbox[2] - bbox[0] + 2 * pad)) / W}%; height:${(100 * (bbox[3] - bbox[1] + 2 * pad)) / H}%;`;
      },

      get bandStyle() {
        if (this.isPending) return this.readingStyle;
        const line = this.currentLine;
        if (!line || !line.bbox) return 'opacity:0;';
        const H = this.image.height || 1;
        const pad = (line.bbox[3] - line.bbox[1]) * 0.3;
        return `top:${(100 * (line.bbox[1] - pad)) / H}%; height:${(100 * (line.bbox[3] - line.bbox[1] + 2 * pad)) / H}%; opacity:1;`;
      },

      setTab(tab) { if (tab === 'scan' && !this.hasScan) return; this.tab = tab; },

      toggleSwap() {
        this.swapped = !this.swapped;
        storage.set(SWAP_KEY, this.swapped ? '1' : '0');
        this.tick(() => this.measure());
      },

      // ------------------------------------------------------------ entrances and small effects
      enter() {
        if (this.reduced || !hasDOM) return;
        this.entering = true;
        clearTimeout(this.enterTimer);
        this.enterTimer = setTimeout(() => { this.entering = false; }, 900);
      },

      pulse() {
        if (this.reduced || !hasDOM) return;
        this.pulsing = true;
        clearTimeout(this.pulseTimer);
        this.pulseTimer = setTimeout(() => { this.pulsing = false; }, 500);
      },

      tickCounter() {
        const target = this.counts.resolved;
        if (this.reduced || !hasDOM) { this.shown = target; this.syncBar(); return; }
        if (this.counterRaf) cancelAnimationFrame(this.counterRaf);
        const from = this.shown;
        if (from === target) return;
        const start = performance.now();
        const duration = 360;
        const step = (now) => {
          const p = Math.min(1, (now - start) / duration);
          const eased = 1 - Math.pow(1 - p, 3);
          this.shown = Math.round(from + (target - from) * eased);
          this.syncBar();
          if (p < 1) this.counterRaf = requestAnimationFrame(step); else this.counterRaf = null;
        };
        this.counterRaf = requestAnimationFrame(step);
      },

      // The shared toast (one at a time, DESIGN.md): it takes the place of an undo offer still showing.
      toast(message) {
        if (!message) return;
        this.dismissUndo();
        if (window.Nassakh && window.Nassakh.toast) window.Nassakh.toast(message);
      },

      // Clean text of the page for the clipboard: body, blank line, footnotes.
      pageText() {
        const body = []; const notes = [];
        this.lines.forEach((line) => {
          const text = line.text || (line.tokens || []).map((t) => t.t).join(' ');
          (this.lineKindOf(line) === 'footnote' ? notes : body).push(text);
        });
        return [body.join('\n'), notes.join('\n')].filter(Boolean).join('\n\n');
      },

      copyPage() {
        if (window.Nassakh && window.Nassakh.copyText) return window.Nassakh.copyText(this.pageText());
        return false;
      },

      // ------------------------------------------------------------ «تصحيح في كل الكتاب» (D79, §5.7; no learning)
      // One correction over every occurrence in the book: the sheet lists them with their crops (api:book_occurrences),
      // a click or Space ticks them (the forms a reviewer confirmed as they are, and running heads, start unticked),
      // Enter or the button corrects the ticked ones in one batch (api:fix_everywhere), and the toast's «تراجع» undoes
      // the batch. On an edited book the toast leads to the book page's find & replace, pre-filled.
      bookUrl(path) { return `/api/books/${this.book.id || this.page.book_id}/${path}`; },
      get fixUrls() {
        const u = this.urls || {};
        return {
          occurrences: u.occurrences || this.bookUrl('occurrences/'),
          apply: u.fix_everywhere || this.bookUrl('fix-everywhere/'),
          undo: u.fix_everywhere_undo || this.bookUrl('fix-everywhere/__batch__/undo/'),
        };
      },
      // ⌥F or «إجراءات أخرى»: the focused word; its correction when one is typed (or it was corrected already)
      openFix(from, to) {
        if (!this.canEdit) return false;
        const tok = this.focused;
        let a = from;
        let b = to;
        if (a === undefined) {
          if (!tok) return false;
          const typed = this.pop.typing ? this.pop.typed.trim() : '';
          a = tok.orig && tok.res === 'typed' && !typed ? tok.orig : tok.t;
          b = typed || (tok.orig && tok.res === 'typed' ? tok.t : '');
        }
        this.closePop();
        this.fixReturn = hasDOM ? document.activeElement : null;
        this.fix = Object.assign({}, this.fix, { open: true, from: core(a), to: core(b), error: '', results: [], total: 0, pages: 0, picks: {}, cursor: 0, busy: false });
        this.tick(() => { const el = this.$refs && this.$refs.fixTo; if (el && el.focus) { el.focus(); if (el.select) el.select(); } });
        return this.loadOccurrences();
      },
      closeFix() {
        if (!this.fix.open) return;
        this.fix.open = false;
        const el = this.fixReturn;
        this.fixReturn = null;
        if (el && el.focus) this.tick(() => el.focus());
      },
      // `?q=&fold_alef=&whole_word=&match_tashkeel=` (the book page's FindOptions)
      occurrencesUrl() {
        const f = this.fix;
        const flag = (v) => (v ? '1' : '0');
        const url = this.fixUrls.occurrences;
        return `${url}${url.includes('?') ? '&' : '?'}q=${encodeURIComponent(f.from)}&fold_alef=${flag(f.options.fold_alef)}&whole_word=${flag(f.options.whole_word)}&match_tashkeel=${flag(f.options.match_tashkeel)}`;
      },
      async loadOccurrences() {
        const f = this.fix;
        if (!f.from) { f.error = 'اكتب الكلمة التي تريد تصحيحها.'; return false; }
        const gen = (this.fixGen = (this.fixGen || 0) + 1);
        f.loading = true;
        f.error = '';
        const res = await api(this.occurrencesUrl());
        if (gen !== this.fixGen) return false; // options changed meanwhile
        f.loading = false;
        if (!res.ok || !res.data) { f.error = res.message; f.results = []; f.total = 0; f.pages = 0; return false; }
        const d = res.data;
        f.results = Array.isArray(d.results) ? d.results : [];
        f.total = Number(d.total) || 0;
        f.pages = Number(d.pages) || f.results.length;
        f.truncated = Boolean(d.truncated);
        const picks = {};
        f.results.forEach((p) => (p.lines || []).forEach((o) => { picks[this.fixKey(o)] = o.pick !== false; }));
        f.picks = picks;
        f.cursor = 0;
        return true;
      },
      setFixOption(name) { this.fix.options[name] = !this.fix.options[name]; return this.loadOccurrences(); },
      fixKey(o) { return `${o.line_id}:${o.index}`; },
      picked(o) { return Boolean(this.fix.picks[this.fixKey(o)]); },
      togglePick(o) { const k = this.fixKey(o); this.fix.picks = Object.assign({}, this.fix.picks, { [k]: !this.fix.picks[k] }); },
      // every occurrence in book order, each with its page (the keyboard's cursor walks them)
      get fixRows() {
        const rows = [];
        this.fix.results.forEach((p) => (p.lines || []).forEach((o) => rows.push(Object.assign({ number: p.number, image: p.image, approved: p.approved }, o))));
        return rows;
      },
      fixRowIndex(o) { return this.fixRows.findIndex((r) => this.fixKey(r) === this.fixKey(o)); },
      get fixPicked() { return this.fixRows.filter((o) => this.picked(o)).length; },
      // «160 موضعًا في 51 صفحة · المحدَّد 158»
      get fixCountText() {
        const f = this.fix;
        if (f.loading) return 'يُبحث في الكتاب…';
        if (!f.total) return f.from ? `لا مواضع لـ«${f.from}» في الكتاب` : '';
        return `${arCount(f.total, PLACES)} في ${arCount(f.pages, PAGES_IN)} · المحدَّد ${this.fixPicked}`;
      },
      get fixButton() { const n = this.fixPicked; return n ? `تصحيح ${arCount(n, PLACES_OBJ)}` : 'تصحيح'; },
      // why a row starts unticked: a running head, or a form a reviewer confirmed as it is
      fixNote(o) {
        if (o.head) return 'ترويسة لا تدخل الكتاب';
        if (o.res === 'primary' || o.res === 'typed') return 'أكّدها المراجع كما هي';
        return '';
      },
      // the word on its line, cut from the review image: the crop is CROP_H px high around the word
      fixCrop(o, p) {
        const img = o.image || (p && p.image) || {};
        const line = Array.isArray(o.line_bbox) && o.line_bbox.length === 4 ? o.line_bbox : o.bbox;
        const word = Array.isArray(o.bbox) && o.bbox.length === 4 ? o.bbox : null;
        if (!img.url || !line || !word || !img.width) return null;
        const h = Math.max(1, line[3] - line[1]);
        const pad = h * 2.5;
        const x0 = Math.max(line[0], word[0] - pad);
        const x1 = Math.min(line[2], word[2] + pad);
        const k = CROP_H / h;
        const r = (v) => Math.round(v * 10) / 10;
        return {
          box: `width:${r((x1 - x0) * k)}px;height:${CROP_H}px;background-image:url("${String(img.url).replace(/"/g, '%22')}");background-size:${r(img.width * k)}px auto;background-position:${r(-x0 * k)}px ${r(-line[1] * k)}px`,
          word: `left:${r((word[0] - x0) * k)}px;width:${r((word[2] - word[0]) * k)}px`,
        };
      },
      cropBox(o, p) { const c = this.fixCrop(o, p); return c ? c.box : ''; },
      cropWord(o, p) { const c = this.fixCrop(o, p); return c ? c.word : ''; },
      // ↑ ↓ move, Space ticks, Enter corrects; Enter in the correction field corrects too
      onFixKey(ev) {
        const k = ev.key;
        const tag = String((ev.target && ev.target.tagName) || '').toLowerCase();
        const inText = tag === 'input' && ev.target.type !== 'checkbox';
        if (k === 'Escape') { ev.preventDefault(); this.closeFix(); return; }
        if (k === 'Enter' && !ev.shiftKey) {
          if (tag === 'button' || tag === 'a') return; // «إلغاء» and the button keep their own Enter
          ev.preventDefault();
          this.applyFix();
          return;
        }
        if (inText) return;
        const rows = this.fixRows;
        if (!rows.length) return;
        if (k === 'ArrowDown' || k === 'ArrowUp') {
          ev.preventDefault();
          this.fix.cursor = clamp(this.fix.cursor + (k === 'ArrowDown' ? 1 : -1), 0, rows.length - 1);
          this.tick(() => { const el = hasDOM && document.getElementById(`rv-fix-row-${this.fix.cursor}`); if (el && el.focus) el.focus(); });
        } else if (k === ' ' && tag !== 'button' && tag !== 'input' && tag !== 'label') {
          // a focused checkbox toggles itself; Space elsewhere in the list ticks the row under the cursor
          ev.preventDefault();
          this.togglePick(rows[this.fix.cursor]);
        }
      },
      async applyFix() {
        const f = this.fix;
        const to = f.to.replace(/\s+/g, ' ').trim();
        if (f.busy || f.loading) return false;
        if (!to) { f.error = 'اكتب التصحيح أولًا.'; return false; }
        if (to === f.from) { f.error = 'التصحيح مطابق للكلمة نفسها.'; return false; }
        const picks = this.fixRows.filter((o) => this.picked(o)).map((o) => ({ line_id: o.line_id, index: o.index, t: o.word }));
        if (!picks.length) { f.error = 'لم يُحدَّد موضع للتصحيح.'; return false; }
        f.busy = true;
        f.error = '';
        // the options the list was made with: the server's default is whole words, so a form listed with «كلمة كاملة»
        // off («والسعودي») would be skipped as changed since the list opened
        const { match_tashkeel, fold_alef, whole_word } = f.options;
        const res = await api(this.fixUrls.apply, { method: 'POST', body: { from: f.from, to, picks, match_tashkeel, fold_alef, whole_word } });
        f.busy = false;
        if (!res.ok || !res.data) { f.error = res.message; return false; }
        const d = res.data;
        this.closeFix();
        this.afterFix(d.pages);
        const skipped = Array.isArray(d.skipped) ? d.skipped.length : 0;
        const link = d.edited && d.find_url ? { label: `وفي نص الكتاب: استبدال «${f.from}» بـ«${to}»…`, url: d.find_url } : null;
        this.showUndoToast(d.message || `صُحّح ${arCount(d.applied, PLACES)}`, undefined, { batch: d.batch, detail: skipped ? this.skippedText(skipped) : '', link });
        return d;
      },
      // «وتُرك موضعان تغيّرا منذ فتح القائمة»
      skippedText(n) {
        if (n === 1) return 'وتُرك موضع واحد تغيّر منذ فتح القائمة';
        if (n === 2) return 'وتُرك موضعان تغيّرا منذ فتح القائمة';
        return `وتُرك ${arCount(n, PLACES)} تغيّرت منذ فتح القائمة`;
      },
      async undoFix(batch) {
        if (!batch || !this.canEdit) return false;
        this.dismissUndo();
        const res = await api(fill(this.fixUrls.undo.replace('__batch__', '__id__'), batch), { method: 'POST' });
        if (!res.ok || !res.data) { this.toast(res.message); return false; }
        this.afterFix(res.data.pages);
        this.toast(res.data.message || 'أُعيدت المواضع كما كانت');
        return res.data;
      },
      // A batch touched other pages (and maybe this one): this page again, the filmstrip, the stage bar, other tabs.
      async afterFix(pages) {
        const book = this.book.id || this.page.book_id;
        announce({ type: 'review', book, pages: Array.isArray(pages) ? pages : [] });
        stagesChanged(book);
        this.film.state = 'idle';
        this.loadFilm();
        if (!Array.isArray(pages) || pages.includes(this.page.number)) {
          const gen = this.gen;
          const res = await api(this.urls.payload);
          if (gen === this.gen && res.ok && res.data && res.data.page) { this.focus = null; this.apply(res.data); this.pulse(); }
        }
      },
      // After a correction: the chip «159 موضعًا آخر بالشكل نفسه في الكتاب · تصحيحها…» under the undo toast.
      offerElsewhere(elsewhere, seq) {
        if (!elsewhere || !(Number(elsewhere.count) > 0) || !this.canEdit) return;
        const text = elsewhere.text || `${arCount(elsewhere.count, ['موضع آخر', 'موضعان آخران', 'مواضع أخرى', 'موضعًا آخر'])} بالشكل نفسه في الكتاب`;
        this.showUndoToast('صُحّحت الكلمة', seq, { elsewhere: { from: elsewhere.from, to: elsewhere.to, text } });
      },
      openElsewhere() {
        const e = this.undoToast && this.undoToast.elsewhere;
        if (!e) return false;
        this.dismissUndo();
        return this.openFix(e.from, e.to);
      },

      // ------------------------------------------------------------ modals
      openSheet() {
        this.sheetReturn = hasDOM ? document.activeElement : null;
        this.sheetOpen = true;
        this.tick(() => { const el = this.$refs.sheetClose; if (el) el.focus(); });
      },

      closeSheet() {
        this.sheetOpen = false;
        const el = this.sheetReturn;
        this.sheetReturn = null;
        if (el && el.focus) this.tick(() => el.focus());
      },

      // Focus trap for the small modals: Tab cycles inside the dialog.
      trapTab(ev, root) {
        if (!root || !root.querySelectorAll) return;
        const items = Array.from(root.querySelectorAll('button, [href], input, textarea, select, [tabindex]:not([tabindex="-1"])'))
          .filter((el) => !el.disabled && el.offsetParent !== null);
        if (!items.length) { ev.preventDefault(); return; }
        const first = items[0];
        const last = items[items.length - 1];
        if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
        else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
      },

      // Esc closes the top-most layer only.
      closeTop() {
        if (this.menuFor != null) { this.menuFor = null; return; }
        if (this.after && !this.pop.open) { this.leaveAfter(); return; } // «البقاء في المراجعة»
        if (this.pop.more) { this.closeMore(true); return; }
        if (this.pop.typing) {
          // readings above the input: Esc returns to them; a correction-only popover (a confident word) just
          // closes; a gap's field gets its offer back
          const gap = this.focusedGap;
          if (gap) { this.pop.typing = false; this.pop.typed = String(gap.text || ''); } else if (this.options().length) { this.pop.typing = false; this.pop.typed = ''; } else this.closePop();
          this.refocusWord();
          return;
        }
        // the menu closes and the focus stays on its word (page mode: A, E, N are commands again)
        if (this.pop.open) { this.closePop(); this.refocusWord(); return; }
        if (this.edit || this.insert) { this.cancelEdit(); return; }
        if (this.range.ids.length) { this.clearRange(); return; }
        if (this.undoToast) { this.dismissUndo(); return; }
        if (this.focus) { this.focus = null; return; }
      },

      refocusWord() {
        this.tick(() => { const el = this.focus && document.getElementById(this.refElId(this.focus)); if (el) el.focus(); });
      },

      // ------------------------------------------------------------ keyboard
      onKey(ev) {
        if (!ev || ev.defaultPrevented) return;
        const target = ev.target || {};
        const tag = String(target.tagName || '').toLowerCase();
        const inField = tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable === true;
        if (this.fix.open) return; // the fix sheet takes its own keys (onFixKey)
        if (this.sheetOpen) { if (ev.key === 'Escape') { ev.preventDefault(); this.closeSheet(); } return; }
        if (this.dialog.open) { if (ev.key === 'Escape') { ev.preventDefault(); this.dialog.open = false; } return; }
        // Buttons and links keep their own keys (Enter / Space activate them, nothing deletes a word from
        // them), and inside the word popover the arrows belong to its menus, not to page navigation.
        // (⌘↵ approves from anywhere, a button included)
        const k = ev.key;
        const chord = ev.metaKey || ev.ctrlKey;
        if ((tag === 'button' || tag === 'a') && ((k === 'Enter' && !chord) || k === ' ' || k === 'Backspace' || k === 'Delete')) return;
        const inPop = Boolean(target.closest && target.closest('.rv-pop'));
        if (inPop && !inField && (k === 'ArrowLeft' || k === 'ArrowRight' || k === 'ArrowUp' || k === 'ArrowDown')) return;
        const outside = Boolean(target.closest && target.closest('.rv-bar, .rv-film, .rv-toolbar, .sidebar, .topbar'));
        const inFlow = !inField && !outside && tag !== 'button' && tag !== 'a';
        const gap = Boolean(this.focusedGap);
        const group = this.focusedGroup !== null;
        const focused = (Boolean(this.focused) || gap) && this.editable;
        const open = focused && this.pop.open;
        // a group's menu keeps or drops its words: no readings to choose, no correction to start
        const optionKeys = gap || group ? [] : this.options().map((o) => o.key);
        const action = keyAction(ev, { inField, inPop, inFlow, focused, open, optionKeys, gap, group });
        if (action) this.runAction(action, ev);
      },

      // ⌘↵ / A: approve the page. From the word menu's correction a changed draft is saved first (the queue keeps
      // the order, and the approval then counts it); an untouched prefill is dropped with the menu.
      approveFromKeys() {
        if (this.pop.open && this.pop.typing) {
          const value = this.pop.typed.trim();
          const tok = this.focused;
          if (value && this.focusedGap && this.editable) this.acceptGap(value); // words typed into a gap's field
          else if (value && tok && this.editable && (value !== tok.t || this.isUnresolved(tok))) this.submitTyped();
        }
        this.closePop();
        return this.approve(false);
      },

      // Space on a focused word or gap: its menu, as a click opens it.
      openWord() {
        if (!this.focus || !(this.focused || this.focusedGap)) return false;
        this.focusWord(this.focus, { open: true, pan: false });
        return true;
      },

      runAction(action, ev) {
        const stop = () => { if (ev && ev.preventDefault) ev.preventDefault(); };
        switch (action) {
          case 'undo': stop(); this.undo(); break;
          case 'close': this.closeTop(); break;
          case 'next': stop(); this.move(1); break;
          case 'prev': stop(); this.move(-1); break;
          case 'insert': stop(); this.startInsert(); break;
          case 'accept': stop(); this.accept(); break;
          case 'type': stop(); this.startTyping(ev.key); break;
          case 'openWord': stop(); this.openWord(); break;
          case 'edit': stop(); this.startEdit(); break;
          case 'mergeNext': stop(); this.mergeWord(1); break;
          case 'mergePrev': stop(); this.mergeWord(-1); break;
          case 'deleteWord': stop(); this.deleteWord(); break;
          case 'dismissGap': stop(); this.dismissGap(); break;
          case 'approve': stop(); this.approveFromKeys(); break;
          case 'nextReview': stop(); this.goNextReview(); break;
          case 'nextPage': stop(); this.goNextPage(); break;
          case 'prevPage': stop(); this.goPrevPage(); break;
          case 'firstPage': stop(); this.goEdgePage(-1); break;
          case 'lastPage': stop(); this.goEdgePage(1); break;
          case 'zoomIn': stop(); this.zoomIn(); break;
          case 'zoomOut': stop(); this.zoomOut(); break;
          case 'zoomReset': stop(); this.zoomReset(); break;
          case 'sheet': stop(); this.openSheet(); break;
          case 'jump': stop(); this.startJump(); break;
          case 'toggleScan': stop(); this.toggleScan(); break;
          case 'swap': stop(); this.toggleSwap(); break;
          case 'copy': stop(); this.copyPage(); break;
          case 'fixEverywhere': stop(); this.openFix(); break;
          default: {
            const reading = /^choose([1-9])$/.exec(action);
            if (reading) { stop(); this.chooseNth(Number(reading[1])); }
            break;
          }
        }
      },
    }));
  });
})();
