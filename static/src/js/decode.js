// The processing theatre's motion engine (Phase 3, decisions D23/D24): the "decode" effect on
// provisional text, plus a tiny registry for other continuous effects (scan sweep, line shimmer).
//
//   window.NassakhDecode.attach(el, { text, mode, lines, static })  mode 'provisional' | 'noise'
//   window.NassakhDecode.update(el, { text })                       new provisional text
//   window.NassakhDecode.resolve(el, { lines, onDone })             right-to-left wave onto the final lines
//   window.NassakhDecode.render(el, { lines })                      final lines at once (no wave)
//   window.NassakhDecode.effect(el, fn)                             fn(t, el) on every tick while near the viewport
//   window.NassakhDecode.detach(el)                                 stop and forget (DOM is left as is)
//   window.NassakhDecode.reducedMotion                              bool, honoured by every call
//
// One shared requestAnimationFrame loop throttled to ~24 fps drives everything; an IntersectionObserver
// pauses elements away from the viewport; elements removed from the document are dropped automatically.
// Words are mutated as whole strings inside one text node each (never per-letter elements), so the
// remaining real Arabic letters keep joining. Plain script, no Alpine dependency, no other dependencies.
(function () {
  'use strict';

  const FPS = 24;
  const FRAME_MS = 1000 / FPS;
  const CAP = 0.6; // at the peak at most 60 % of a word's letters are Tesseract's; it never looks final
  const FLOOR = 0.08;
  const NOISE = Array.from('@#$%^&*!?§¤~+=' + 'ابتثجحخدذرزسشصضطظعغفقكلمنهوي');
  const SYMBOLS = Array.from('@#$%^&*!?§¤~+=');
  const WAVE_PER_WORD_MS = 35;
  const WAVE_MIN_MS = 600;
  const WAVE_MAX_MS = 1400;
  const DONE_DELAY_MS = 300; // the last word's highlight has faded most of the way before onDone
  const NOISE_LINE_WORDS = [3, 6];
  const NOISE_WORD_LEN = [2, 7];

  const clock = () => (typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now());
  const rand = Math.random;
  const pick = (arr) => arr[(rand() * arr.length) | 0];
  const between = (lo, hi) => lo + Math.floor(rand() * (hi - lo + 1));

  const registry = new Map(); // element -> state
  let observer = null;
  let running = false;
  let lastTick = 0;

  function prefersReducedMotion() {
    try {
      return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
    } catch (e) {
      return false;
    }
  }

  // ---------------------------------------------------------------- shared loop and visibility
  function ensureObserver() {
    if (observer !== null || typeof IntersectionObserver !== 'function') return;
    observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        const state = registry.get(entry.target);
        if (state) state.visible = entry.isIntersecting;
      });
    }, { rootMargin: '240px' });
  }

  function watch(state) {
    ensureObserver();
    if (observer) observer.observe(state.el);
  }

  function unwatch(el) {
    if (observer) observer.unobserve(el);
  }

  function schedule() {
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(frame);
    else setTimeout(() => frame(clock()), FRAME_MS);
  }

  function frame(t) {
    if (t - lastTick >= FRAME_MS - 1) {
      lastTick = t;
      step(t);
    }
    if (anyActive()) schedule();
    else running = false;
  }

  function wake() {
    if (running) return;
    running = true;
    schedule();
  }

  function anyActive() {
    for (const state of registry.values()) if (state.active) return true;
    return false;
  }

  // One tick of every active effect. Public (as `step`) so hosts and tests can drive it by hand.
  function step(t) {
    registry.forEach((state, el) => {
      if (el.isConnected === false) { // unmounted by the host (e.g. an Alpine x-if): forget it
        registry.delete(el);
        unwatch(el);
        return;
      }
      if (!state.active) return;
      if (state.effect) {
        if (state.visible) state.effect(t, el);
        return;
      }
      if (state.resolving) {
        if (!state.visible) { finishResolve(state, t, true); return; }
        stepResolve(state, t);
        return;
      }
      if (!state.visible) return;
      state.tick += 1;
      state.words.forEach((w, i) => {
        if (((state.tick + i) & 1) === 0) renderWord(w, t);
      });
    });
  }

  // ---------------------------------------------------------------- words
  function makeWord(node, text, mode) {
    const real = Array.from(text);
    const rank = real.map((_, i) => i);
    for (let i = rank.length - 1; i > 0; i -= 1) { // Fisher–Yates: the order letters lock in
      const j = (rand() * (i + 1)) | 0;
      const tmp = rank[i]; rank[i] = rank[j]; rank[j] = tmp;
    }
    return {
      node,
      real,
      rank,
      shown: real.map(() => pick(NOISE)),
      extra: '',
      phase: rand() * Math.PI * 2,
      speed: 2.0 + rand() * 1.4, // one full breath every ~2–3 s
      noise: mode === 'noise',
      nReal: 0,
      landed: false,
      landAt: 0,
      final: null,
    };
  }

  function noiseGlyph(real) {
    const g = pick(NOISE);
    return g === real ? pick(SYMBOLS) : g;
  }

  function renderWord(w, t) {
    const len = w.real.length;
    const breath = 0.5 + 0.5 * Math.sin((t / 1000) * w.speed + w.phase);
    const ratio = FLOOR + (CAP - FLOOR) * breath;
    const nReal = w.noise ? 0 : Math.min(Math.floor(ratio * len), Math.floor(CAP * len));
    for (let i = 0; i < len; i += 1) {
      if (w.rank[i] < nReal) w.shown[i] = w.real[i];
      else if (w.shown[i] === w.real[i] || rand() < 0.45) w.shown[i] = noiseGlyph(w.real[i]);
    }
    if (rand() < 0.06) w.extra = w.extra ? '' : pick(NOISE); // length breathes by one glyph, never more
    w.nReal = nReal;
    w.node.nodeValue = w.shown.join('') + w.extra;
  }

  function landWord(w) {
    w.landed = true;
    w.extra = '';
    w.node.nodeValue = w.final.t;
    w.nReal = w.real.length;
    const span = w.node.parentNode;
    if (span && span.classList) {
      span.classList.add('is-landed');
      if (isLow(w.final)) span.classList.add('tok-low');
    }
  }

  function isLow(tok) {
    return tok && tok.conf === 'low' && !tok.res;
  }

  // ---------------------------------------------------------------- DOM building
  function el(tag, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    return node;
  }

  function clear(node) {
    node.textContent = '';
  }

  // Lines of words → one `.decode-line` per line, one `.tok` span (one text node) per word, real spaces
  // between words so selection copies words. Returns the word states in reading order.
  function buildLines(state, lines) {
    clear(state.el);
    const words = [];
    lines.forEach((line, li) => {
      const row = el('div', 'decode-line');
      if (line.region_kind) row.setAttribute('data-region', line.region_kind);
      if (line.region_kind === 'footnote' && (li === 0 || lines[li - 1].region_kind !== 'footnote')) {
        row.classList.add('is-first-footnote');
      }
      line.tokens.forEach((tok, ti) => {
        if (ti > 0) row.appendChild(document.createTextNode(' '));
        const span = el('span', 'tok decode-w');
        const node = document.createTextNode('');
        span.appendChild(node);
        row.appendChild(span);
        const w = makeWord(node, tok.t, state.mode);
        w.final = tok;
        words.push(w);
      });
      state.el.appendChild(row);
    });
    return words;
  }

  function textToLines(text) {
    return String(text || '')
      .split('\n')
      .map((line) => line.split(/\s+/).filter(Boolean))
      .filter((words) => words.length)
      .map((words) => ({ tokens: words.map((t) => ({ t })) }));
  }

  function noiseLines(count) {
    const n = Math.max(1, Math.min(60, Number(count) || 6));
    const lines = [];
    for (let i = 0; i < n; i += 1) {
      const words = [];
      const nWords = between(NOISE_LINE_WORDS[0], NOISE_LINE_WORDS[1]);
      for (let k = 0; k < nWords; k += 1) {
        let s = '';
        const len = between(NOISE_WORD_LEN[0], NOISE_WORD_LEN[1]);
        for (let c = 0; c < len; c += 1) s += pick(NOISE);
        words.push({ t: s });
      }
      lines.push({ tokens: words });
    }
    return lines;
  }

  function renderStatic(state, lines) {
    const words = buildLines(state, lines);
    words.forEach((w) => { w.node.nodeValue = w.final.t; w.nReal = w.real.length; w.landed = true; });
    return words;
  }

  function setClasses(state, phase) {
    const cls = state.el.classList;
    cls.add('decode');
    cls.toggle('is-decoding', phase === 'decoding');
    cls.toggle('is-static', phase === 'static');
    cls.toggle('is-resolving', phase === 'resolving');
    cls.toggle('is-resolved', phase === 'resolved');
    cls.toggle('is-noise', state.mode === 'noise');
    // scrambled text means nothing to a screen reader; the host announces the state in an aria-live region
    state.el.setAttribute('aria-hidden', phase === 'resolved' || phase === 'static' ? 'false' : 'true');
  }

  function getOrCreate(target) {
    let state = registry.get(target);
    if (!state) {
      state = { el: target, mode: 'provisional', words: [], active: false, visible: true, resolving: false, tick: 0, effect: null };
      registry.set(target, state);
      watch(state);
    }
    return state;
  }

  // ---------------------------------------------------------------- public API
  function attach(target, options) {
    const opts = options || {};
    const state = getOrCreate(target);
    state.mode = opts.mode === 'noise' ? 'noise' : 'provisional';
    state.effect = null;
    state.resolving = false;
    state.text = opts.text || '';
    state.nLines = opts.lines;
    state.isStatic = Boolean(opts.static);
    const lines = state.mode === 'noise' ? noiseLines(opts.lines) : textToLines(opts.text);
    if (api.reducedMotion || opts.static) {
      // static state: Tesseract's text plainly (faded by the host's CSS), or nothing for pure noise
      state.words = state.mode === 'noise' ? (clear(target), []) : renderStatic(state, lines);
      state.active = false;
      setClasses(state, 'static');
      return;
    }
    state.words = buildLines(state, lines);
    state.words.forEach((w) => renderWord(w, clock()));
    state.active = true;
    setClasses(state, 'decoding');
    wake();
  }

  function update(target, options) {
    const state = registry.get(target);
    const text = (options && options.text) || '';
    if (!state) { attach(target, { text, mode: 'provisional' }); return; }
    if (state.mode === 'provisional' && state.text === text && Boolean(options && options.static) === state.isStatic) return;
    attach(target, { text, mode: 'provisional', static: Boolean(options && options.static) });
  }

  function render(target, options) {
    const state = getOrCreate(target);
    const lines = (options && options.lines) || [];
    state.mode = 'provisional';
    state.effect = null;
    state.resolving = false;
    state.active = false;
    state.words = renderStatic(state, normaliseLines(lines));
    state.words.forEach((w) => { if (isLow(w.final)) w.node.parentNode.classList.add('tok-low'); });
    setClasses(state, 'resolved');
  }

  function normaliseLines(lines) {
    return (Array.isArray(lines) ? lines : [])
      .map((line) => ({
        region_kind: line.region_kind || line.kind || '',
        tokens: (line.tokens || []).map((tok) => (typeof tok === 'string' ? { t: tok } : tok)).filter((tok) => tok && tok.t),
      }))
      .filter((line) => line.tokens.length);
  }

  function resolve(target, options) {
    const opts = options || {};
    const state = getOrCreate(target);
    const lines = normaliseLines(opts.lines);
    state.onDone = typeof opts.onDone === 'function' ? opts.onDone : null;
    state.effect = null;
    if (api.reducedMotion || !lines.length) {
      render(target, { lines });
      const done = state.onDone; state.onDone = null;
      if (done) done();
      return;
    }
    const t0 = clock();
    state.mode = 'provisional';
    state.words = buildLines(state, lines);
    const n = state.words.length;
    const wave = Math.max(WAVE_MIN_MS, Math.min(WAVE_MAX_MS, n * WAVE_PER_WORD_MS));
    state.words.forEach((w, i) => {
      w.landAt = t0 + (n > 1 ? (i / (n - 1)) * wave : 0);
      renderWord(w, t0);
    });
    state.doneAt = t0 + wave + DONE_DELAY_MS;
    state.resolving = true;
    state.active = true;
    setClasses(state, 'resolving');
    wake();
  }

  function stepResolve(state, t) {
    state.words.forEach((w) => {
      if (w.landed) return;
      if (t >= w.landAt) landWord(w);
      else renderWord(w, t);
    });
    if (t >= state.doneAt) finishResolve(state, t, false);
  }

  function finishResolve(state, t, instant) {
    if (instant) state.words.forEach((w) => { if (!w.landed) landWord(w); });
    state.resolving = false;
    state.active = false;
    setClasses(state, 'resolved');
    const done = state.onDone; state.onDone = null;
    if (done) done();
  }

  function effect(target, fn) {
    if (api.reducedMotion || typeof fn !== 'function') return;
    const state = getOrCreate(target);
    state.effect = fn;
    state.words = [];
    state.resolving = false;
    state.active = true;
    wake();
  }

  function detach(target) {
    const state = registry.get(target);
    if (!state) return;
    registry.delete(target);
    unwatch(target);
    if (target.classList) target.classList.remove('is-decoding', 'is-resolving');
  }

  // For tests and diagnostics: what each word shows right now against its real letters.
  function inspect(target) {
    const state = registry.get(target);
    if (!state) return [];
    return state.words.map((w) => ({
      real: w.real.join(''),
      shown: w.node.nodeValue,
      nReal: w.nReal,
      landed: w.landed,
      low: isLow(w.final),
    }));
  }

  const api = {
    attach,
    update,
    resolve,
    render,
    effect,
    detach,
    step,
    inspect,
    reducedMotion: prefersReducedMotion(),
    get size() { return registry.size; },
  };

  window.NassakhDecode = api;
})();
