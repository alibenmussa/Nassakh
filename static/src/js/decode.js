// The processing theatre's motion engine (Phase 3, decisions D23/D24 as amended by D28 — see
// docs/DASHBOARD_SPEC.md §4–§5): the dwell-and-veil effect on provisional text, the text layout that
// mirrors a page's printed lines, the sheet handle of the dashboard, plus a tiny registry for other
// continuous effects.
//
//   window.NassakhDecode.attach(el, { text, mode, lines, static })  mode 'provisional' | 'noise'
//   window.NassakhDecode.update(el, { text, static })               new provisional text
//   window.NassakhDecode.resolve(el, { lines, onDone })             right-to-left wave onto the final lines
//   window.NassakhDecode.render(el, { lines })                      final lines at once (no wave)
//   window.NassakhDecode.effect(el, fn)                             fn(t, el) on every tick while near the viewport
//   window.NassakhDecode.detach(el)                                 stop and forget (DOM is left as is)
//   window.NassakhDecode.sheet(host, { scan, figure })              a dashboard sheet: text pane + scan overlay
//   window.NassakhDecode.layout(page)                               pure geometry of a page's text (§4)
//   window.NassakhDecode.measure                                    text → width at 100 px (injectable)
//   window.NassakhDecode.reducedMotion                              bool, honoured by every call
//
// One shared requestAnimationFrame loop throttled to ~24 fps drives everything; an IntersectionObserver
// pauses elements away from the viewport; elements removed from the document are dropped automatically.
// Words are mutated as whole strings inside one text node each (never per-letter elements), so Arabic
// letters keep joining. Veiled letters are Arabic letters of the same skeleton family; no ASCII symbols,
// no length changes, the first letter of a word is never veiled (D28). Plain script, no dependencies.
(function () {
  'use strict';

  // ---------------------------------------------------------------- parameters (DASHBOARD_SPEC §5.6)
  const FPS = 24;
  const FRAME_MS = 1000 / FPS;
  const PERIOD_MS = [3500, 6500]; // one word cycle: dwell on the exact string, then a short veil
  const DWELL_SHARE = 0.78;
  const VEIL_MAX = 40; // veiled words per sheet (or per attached element) at any moment
  const VEIL_RETRY_MS = 400;
  const LINE_MS = 420; // the reading cursor advances one line at this pace
  const CYCLE_REST_MS = 1200;
  const LOCK_MS = LINE_MS + 1200; // a line the cursor passed stays exact this long
  const WAVE_PER_WORD_MS = 35;
  const WAVE_MIN_MS = 600;
  const WAVE_MAX_MS = 1400;
  const DONE_DELAY_MS = 300;
  const LEAVE_MS = 300; // the provisional layer fades under the final one
  const SWEEP_MS = 2400;
  const FAC_K = 0.78; // font size = K × line ink height (IBM Plex Sans Arabic ascender→descender ≈ 1.28 em)
  const LINE_H = 1.24; // line box height in ink heights
  const PAD = 0.12; // vertical block padding in line heights (Tesseract boxes clip diacritics)
  const FEW = 3; // a block with ≤ 3 lines is centred, not spread
  const TITLE_FS = 0.016; // title page without geometry: font size as a ratio of H (≈ 14 px on a 900 px tall pane)
  const STAGGER_CAP = 24;
  const SKELETON_WIDTHS = [100, 100, 92, 100, 84, 100, 96, 100, 88, 100, 100, 72, 100, 48];
  const DEFAULT_BODY = [0.12, 0.10, 0.88, 0.90];
  // §4.7 one type size per text group, paragraph edges shared, lines fitted by word spacing (D30)
  const SNAP_TOL = 0.03; // line edges within 3 % of the text width share the paragraph edge…
  const SNAP_TOL_START = 0.012; // …but the start edge (right) snaps only within 1.2 %, so a paragraph indent survives
  const FIT_Q = 0.1; // the group's size lets 90 % of its full lines fit their measure at natural spacing…
  const FIT_FLOOR = 0.75; // …but never drops below 75 % of the size the printed line height gives
  const FIT_OUTLIER = 0.8; // a line needing < 80 % of the group's median size (merged lines, extra words) does not set it
  const WS_SHRINK = 0.5; // a line still too long tightens each word space by up to half a space…
  const SX_MIN = 0.9; // …then condenses horizontally down to 90 %; beyond that it is clipped with a fade
  const BOOK_TOL = 0.2; // a page whose line height is within ±20 % of the book's takes the book's size
  const NOISE_LINE_WORDS = [3, 6];
  const NOISE_WORD_LEN = [2, 7];
  const FONT = '"IBM Plex Sans Arabic"';
  const PROCESSING = ['uploaded', 'preprocessed', 'layout_done'];

  // ---------------------------------------------------------------- alphabet (D28)
  const FAMILIES = ['بتثني', 'جحخ', 'دذ', 'رز', 'سش', 'صض', 'طظ', 'عغ', 'فق', 'هة', 'وؤ', 'اأإآ', 'كل'];
  const FALLBACK = Array.from('بنت');
  const FAMILY_OF = {};
  FAMILIES.forEach((fam) => Array.from(fam).forEach((ch) => { FAMILY_OF[ch] = Array.from(fam); }));
  const LETTERS = Array.from('ابتثجحخدذرزسشصضطظعغفقكلمنهوي');
  const BASE_LETTER = /[ء-غف-يٮ-ٯٱ-ۓۺ-ۼ]/;

  const clock = () => (typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now());
  const rand = Math.random;
  const pick = (arr) => arr[(rand() * arr.length) | 0];
  const between = (lo, hi) => lo + Math.floor(rand() * (hi - lo + 1));
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const fix = (v) => Number(v).toFixed(4);
  const isBase = (ch) => BASE_LETTER.test(ch);

  function altLetter(ch) {
    const pool = (FAMILY_OF[ch] || FALLBACK).filter((c) => c !== ch);
    return pool.length ? pick(pool) : pick(FALLBACK.filter((c) => c !== ch));
  }

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
      if (el.isConnected === false) { // unmounted by the host: forget it
        registry.delete(el);
        unwatch(el);
        return;
      }
      if (!state.active) return;
      if (state.sheet) { tickSheet(state, t); return; }
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
      state.words.forEach((w) => tickWord(w, t, state.budget));
    });
  }

  // ---------------------------------------------------------------- words: dwell, veil, return (§5.2)
  function makeWord(node, text, mode) {
    const chars = Array.from(text);
    const veilable = [];
    let first = true;
    chars.forEach((ch, i) => {
      if (!isBase(ch)) return;
      if (first) { first = false; return; } // the first letter of a word is never veiled
      veilable.push(i);
    });
    const nBase = veilable.length + (first ? 0 : 1);
    const period = between(PERIOD_MS[0], PERIOD_MS[1]);
    return {
      node,
      real: text,
      chars,
      veilable,
      nVeil: clamp(Math.round(nBase * 0.25), 1, 3),
      period,
      veil: period * (1 - DWELL_SHARE),
      nextAt: 0,
      midAt: 0,
      midDone: false,
      veiled: false,
      lockUntil: 0,
      shown: text,
      noise: mode === 'noise',
      landed: false,
      landAt: 0,
      final: null,
      line: 0,
    };
  }

  function veilString(w) {
    const idx = w.veilable.slice();
    for (let i = idx.length - 1; i > 0; i -= 1) { // partial Fisher–Yates: which letters drift this time
      const j = (rand() * (i + 1)) | 0;
      const tmp = idx[i]; idx[i] = idx[j]; idx[j] = tmp;
    }
    const out = w.chars.slice();
    idx.slice(0, w.nVeil).forEach((i) => { out[i] = altLetter(w.chars[i]); });
    return out.join('');
  }

  function show(w, text) {
    if (w.shown === text) return;
    w.shown = text;
    w.node.nodeValue = text;
  }

  function spanOf(w) {
    const span = w.node.parentNode;
    return span && span.classList ? span : null;
  }

  function veil(w, t, budget) {
    w.veiled = true;
    budget.n += 1;
    w.midDone = false;
    w.midAt = t + w.veil / 2;
    w.nextAt = t + w.veil;
    show(w, veilString(w));
    const span = spanOf(w);
    if (span) span.classList.add('is-veiled');
  }

  function unveil(w, budget) {
    if (!w.veiled) return;
    w.veiled = false;
    budget.n = Math.max(0, budget.n - 1);
    show(w, w.real);
    const span = spanOf(w);
    if (span) span.classList.remove('is-veiled');
  }

  // Exact string now, and no drift before `until` (the reading cursor passed over this word).
  function lockExact(w, until, budget) {
    unveil(w, budget);
    w.lockUntil = until;
    if (w.nextAt < until) w.nextAt = until;
  }

  function phaseWord(w, t) {
    w.nextAt = t + rand() * w.period;
  }

  function tickWord(w, t, budget) {
    if (w.landed) return;
    if (w.veiled) {
      if (t >= w.nextAt) {
        unveil(w, budget);
        w.nextAt = t + w.period - w.veil;
      } else if (!w.midDone && t >= w.midAt) {
        w.midDone = true;
        show(w, veilString(w));
      }
      return;
    }
    if (t < w.nextAt || t < w.lockUntil || !w.veilable.length) return;
    if (budget.n >= VEIL_MAX) { w.nextAt = t + VEIL_RETRY_MS; return; }
    veil(w, t, budget);
  }

  function isLow(tok) {
    return Boolean(tok) && tok.conf === 'low' && !tok.res;
  }

  function landWord(w, budget) {
    if (w.veiled && budget) unveil(w, budget);
    w.veiled = false;
    w.landed = true;
    show(w, w.final ? w.final.t : w.real);
    const span = spanOf(w);
    if (span) {
      span.classList.remove('is-veiled');
      span.classList.add('is-landed');
      if (isLow(w.final)) span.classList.add('tok-low');
    }
  }

  function nReal(w) {
    if (w.noise) return 0;
    const shown = Array.from(w.shown);
    let n = 0;
    w.chars.forEach((ch, i) => { if (shown[i] === ch) n += 1; });
    return n;
  }

  // ---------------------------------------------------------------- DOM helpers
  function el(tag, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    return node;
  }

  function textNode(text) {
    return document.createTextNode(text);
  }

  function clear(node) {
    node.textContent = '';
  }

  function removeNode(node) {
    if (!node) return;
    if (typeof node.remove === 'function') node.remove();
    else if (node.parentNode && node.parentNode.removeChild) node.parentNode.removeChild(node);
  }

  function setProps(node, props) {
    Object.keys(props).forEach((k) => node.style.setProperty(k, String(props[k])));
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
        if (ti > 0) row.appendChild(textNode(' '));
        const span = el('span', 'tok decode-w');
        const node = textNode(tok.t);
        span.appendChild(node);
        row.appendChild(span);
        const w = makeWord(node, tok.t, state.mode);
        w.final = tok;
        w.line = li;
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

  // Pseudo-words from Arabic letters only (no text yet): the sheet is never an empty box.
  function noiseLines(count) {
    const n = Math.max(1, Math.min(60, Number(count) || 6));
    const lines = [];
    for (let i = 0; i < n; i += 1) {
      const words = [];
      const nWords = between(NOISE_LINE_WORDS[0], NOISE_LINE_WORDS[1]);
      for (let k = 0; k < nWords; k += 1) {
        let s = '';
        const len = between(NOISE_WORD_LEN[0], NOISE_WORD_LEN[1]);
        for (let c = 0; c < len; c += 1) s += pick(LETTERS);
        words.push({ t: s });
      }
      lines.push({ tokens: words });
    }
    return lines;
  }

  function renderStatic(state, lines) {
    const words = buildLines(state, lines);
    words.forEach((w) => { show(w, w.final.t); w.landed = true; });
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
    // drifting text means nothing to a screen reader; the host announces the state in an aria-live region
    state.el.setAttribute('aria-hidden', phase === 'resolved' || phase === 'static' ? 'false' : 'true');
  }

  function getOrCreate(target) {
    let state = registry.get(target);
    if (!state) {
      state = { el: target, mode: 'provisional', words: [], budget: { n: 0 }, active: false, visible: true, resolving: false, effect: null };
      registry.set(target, state);
      watch(state);
    }
    return state;
  }

  // ---------------------------------------------------------------- public API: attached elements
  function attach(target, options) {
    const opts = options || {};
    const state = getOrCreate(target);
    state.mode = opts.mode === 'noise' ? 'noise' : 'provisional';
    state.effect = null;
    state.resolving = false;
    state.text = opts.text || '';
    state.nLines = opts.lines;
    state.isStatic = Boolean(opts.static);
    state.budget = { n: 0 };
    const lines = state.mode === 'noise' ? noiseLines(opts.lines) : textToLines(opts.text);
    if (api.reducedMotion || opts.static) {
      // static state: Tesseract's text plainly (faded by the host's CSS), or nothing for pure noise
      state.words = state.mode === 'noise' ? (clear(target), []) : renderStatic(state, lines);
      state.active = false;
      setClasses(state, 'static');
      return;
    }
    const t0 = clock();
    state.words = buildLines(state, lines);
    state.words.forEach((w) => phaseWord(w, t0));
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

  function normaliseLines(lines) {
    return (Array.isArray(lines) ? lines : [])
      .map((line) => ({
        region_kind: line.region_kind || line.kind || '',
        tokens: (line.tokens || []).map((tok) => (typeof tok === 'string' ? { t: tok } : tok)).filter((tok) => tok && tok.t),
      }))
      .filter((line) => line.tokens.length);
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

  function waveMs(n) {
    return Math.max(WAVE_MIN_MS, Math.min(WAVE_MAX_MS, n * WAVE_PER_WORD_MS));
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
    state.budget = { n: 0 };
    state.words = buildLines(state, lines);
    const n = state.words.length;
    const wave = waveMs(n);
    state.words.forEach((w, i) => {
      w.landAt = t0 + (n > 1 ? (i / (n - 1)) * wave : 0);
      if (w.veilable.length) show(w, veilString(w)); // not yet landed: a veiled reading, gray
    });
    state.doneAt = t0 + wave + DONE_DELAY_MS;
    state.resolving = true;
    state.active = true;
    setClasses(state, 'resolving');
    wake();
  }

  function stepResolve(state, t) {
    state.words.forEach((w) => { if (!w.landed && t >= w.landAt) landWord(w, state.budget); });
    if (t >= state.doneAt) finishResolve(state, t, false);
  }

  function finishResolve(state, t, instant) {
    if (instant) state.words.forEach((w) => { if (!w.landed) landWord(w, state.budget); });
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
      real: w.real,
      shown: w.shown,
      nReal: nReal(w),
      landed: w.landed,
      low: isLow(w.final),
      veiled: w.veiled,
      line: w.line,
    }));
  }

  // ---------------------------------------------------------------- text measurement (§4.6)
  let canvasCtx = null;
  function browserMeasure(text) {
    if (canvasCtx === null) {
      try {
        const canvas = document.createElement('canvas');
        canvasCtx = canvas && typeof canvas.getContext === 'function' ? canvas.getContext('2d') : false;
        if (canvasCtx) canvasCtx.font = `100px ${FONT}`;
      } catch (e) {
        canvasCtx = false;
      }
    }
    if (!canvasCtx) return Array.from(String(text || '')).length * 52; // no canvas: ≈ 0.52 em per letter
    return canvasCtx.measureText(String(text || '')).width;
  }

  // ---------------------------------------------------------------- layout (§4): pure, testable
  function ratioBox(b) {
    if (!Array.isArray(b) || b.length !== 4) return null;
    const v = b.map(Number);
    if (v.some((x) => !Number.isFinite(x))) return null;
    return [clamp(v[0], 0, 1), clamp(v[1], 0, 1), clamp(v[2], 0, 1), clamp(v[3], 0, 1)];
  }

  function median(values) {
    const v = values.filter((x) => Number.isFinite(x) && x > 0).sort((a, b) => a - b);
    if (!v.length) return 0;
    return v.length % 2 ? v[(v.length - 1) / 2] : (v[v.length / 2 - 1] + v[v.length / 2]) / 2;
  }

  function mid(values) { // median without dropping zeros (edges may sit at 0)
    const v = values.filter((x) => Number.isFinite(x)).sort((a, b) => a - b);
    if (!v.length) return 0;
    return v.length % 2 ? v[(v.length - 1) / 2] : (v[v.length / 2 - 1] + v[v.length / 2]) / 2;
  }

  function quantile(values, q) {
    const v = values.filter((x) => Number.isFinite(x) && x > 0).sort((a, b) => a - b);
    if (!v.length) return 0;
    const pos = (v.length - 1) * q;
    const lo = Math.floor(pos);
    const hi = Math.ceil(pos);
    return v[lo] + (v[hi] - v[lo]) * (pos - lo);
  }

  // The edge most lines share: the densest ±tol window of `values` (ties go to the outer side), as the
  // median of that window; null when fewer than two lines share an edge.
  function dominantEdge(values, tol, outerIsMax) {
    if (values.length < 2) return null;
    let best = null;
    values.forEach((c) => {
      const near = values.filter((v) => Math.abs(v - c) <= tol);
      if (!best || near.length > best.near.length || (near.length === best.near.length && (outerIsMax ? c > best.c : c < best.c))) {
        best = { c, near };
      }
    });
    return best.near.length >= 2 ? mid(best.near) : null;
  }

  // Snap a measured line to the paragraph edges (L = end edge on the left, R = start edge on the right,
  // RTL) and classify it: full (reaches the measure: justified), indent (reaches the measure but starts
  // inset: a paragraph's first line, flush left with its printed indent), center (inset on both sides
  // about equally: a heading), short (ends early: a paragraph's last line).
  function snapLine(r, L, R, tol, tolStart) {
    const atEnd = Math.abs(r.lx0 - L) <= tol;
    const atStart = Math.abs(r.lx1 - R) <= tolStart;
    if (atEnd) r.lx0 = L;
    if (atStart) r.lx1 = R;
    // reaches the measure: justified; one inset at its start (right) is a paragraph's first line (D31)
    if (atEnd || r.lx0 < L) { r.shape = !atStart && r.lx1 < R ? 'indent' : 'full'; return; }
    if (!atStart && r.lx1 < R) {
      const inEnd = r.lx0 - L;
      const inStart = R - r.lx1;
      if (Math.abs(inEnd - inStart) <= Math.max(tol, 0.25 * Math.max(inEnd, inStart))) { r.shape = 'center'; return; }
    }
    r.shape = 'short';
  }

  function union(boxes) {
    return [
      Math.min(...boxes.map((b) => b[0])),
      Math.min(...boxes.map((b) => b[1])),
      Math.max(...boxes.map((b) => b[2])),
      Math.max(...boxes.map((b) => b[3])),
    ];
  }

  function tokenOf(tok) {
    if (typeof tok === 'string') return { t: tok };
    return tok && tok.t ? tok : null;
  }

  // The page's text lines `T`: final lines, else Tesseract's provisional lines, else the provisional text.
  function textLinesOf(page) {
    if (page.text_state === 'final') {
      if (!Array.isArray(page.lines)) return null; // data not here yet
      return page.lines
        .map((line) => {
          const tokens = (line.tokens || []).map(tokenOf).filter(Boolean);
          return { kind: line.region_kind || line.kind || 'body', tokens, words: tokens.map((tk) => tk.t), box: ratioBox(line.bbox) };
        })
        .filter((line) => line.words.length);
    }
    if (page.text_state === 'provisional') {
      if (Array.isArray(page.provisional_lines) && page.provisional_lines.length) {
        return page.provisional_lines
          .map((line) => {
            const words = (line.words || []).map(String).filter(Boolean);
            return { kind: line.region_kind || 'body', tokens: words.map((t) => ({ t })), words, box: ratioBox(line.bbox) };
          })
          .filter((line) => line.words.length);
      }
      return String(page.provisional_text || '')
        .split('\n')
        .map((line) => line.split(/\s+/).filter(Boolean))
        .filter((words) => words.length)
        .map((words) => ({ kind: 'body', tokens: words.map((t) => ({ t })), words, box: null }));
    }
    return [];
  }

  function layout(page) {
    const p = page || {};
    const W = Number(p.width) || 0;
    const H = Number(p.height) || 0;
    const ar = W > 0 && H > 0 ? W / H : 0.7;
    const HW = 1 / ar;
    const T = textLinesOf(p);
    const boxes = (Array.isArray(p.line_boxes) ? p.line_boxes : []).map(ratioBox).filter(Boolean).sort((a, b) => a[1] - b[1]);
    const fy = typeof p.footnote_y === 'number' && Number.isFinite(p.footnote_y) ? p.footnote_y : null;
    const mlh = Number(p.median_line_h) || 0;
    // same type across the book: this page's measured line height is replaced by the book's typical one
    // when they agree within BOOK_TOL (measurement noise); a page with clearly different type keeps its own
    const bookLh = Number(p.book_line_h_px) || 0;
    let bookNorm = 1;
    if (bookLh > 0 && mlh > 0 && H > 0) {
      const q = bookLh / (mlh * H);
      if (Math.abs(q - 1) <= BOOK_TOL) bookNorm = q;
    }
    const regions = (Array.isArray(p.regions) ? p.regions : []).map((r) => ({ kind: r.kind || 'body', box: ratioBox(r.bbox) })).filter((r) => r.box);
    let mode;
    if (T === null) mode = 'skeleton';
    else if (T.length) mode = p.text_state === 'final' ? 'final' : 'provisional';
    else mode = p.text_state === 'final' ? 'empty' : 'skeleton';
    const text = T || [];
    const bodyT = text.filter((l) => l.kind !== 'footnote');
    const noteT = text.filter((l) => l.kind === 'footnote');
    const bodyB = fy === null ? boxes : boxes.filter((b) => b[1] < fy);
    const noteB = fy === null ? [] : boxes.filter((b) => b[1] >= fy);
    const measure = typeof api.measure === 'function' ? api.measure : browserMeasure;
    const spaceW = Math.max(1, Number(measure(' ')) || 1);
    let index = 0;

    function group(kind, textLines, detected, bodyFs) {
      let tl = textLines;
      const skeleton = mode === 'skeleton';
      // text lines without geometry borrow the detected boxes when the counts match (older payloads)
      if (tl.length && detected.length === tl.length && !tl.some((l) => l.box)) {
        tl = tl.map((l, k) => ({ ...l, box: detected[k] }));
      }
      const count = skeleton ? (detected.length || (kind === 'body' ? SKELETON_WIDTHS.length : 0)) : tl.length;
      if (!count) return null;
      const withBox = tl.filter((l) => l.box);
      let block = null;
      let hg = 0;
      let K = FAC_K;
      let source;
      if (!skeleton && withBox.length && withBox.length >= 0.6 * tl.length) {
        block = union(withBox.map((l) => l.box));
        hg = median(withBox.map((l) => l.box[3] - l.box[1]));
        source = 'text';
      } else if (detected.length) {
        block = union(detected);
        hg = median(detected.map((b) => b[3] - b[1]));
        source = 'boxes';
      } else {
        const rb = regions.filter((r) => (kind === 'footnote' ? r.kind === 'footnote' : r.kind !== 'footnote')).map((r) => r.box);
        if (rb.length) { block = union(rb); source = 'regions'; }
        else {
          block = kind === 'body' ? [DEFAULT_BODY[0], DEFAULT_BODY[1], DEFAULT_BODY[2], fy === null ? DEFAULT_BODY[3] : fy] : [0.12, (fy === null ? 0.8 : fy) + 0.012, 0.88, 0.94];
          source = 'default';
        }
        if (mlh > 0) hg = kind === 'body' ? mlh : 0.85 * mlh;
      }
      if (hg > 0) hg *= bookNorm;
      else if (bookLh > 0 && H > 0) hg = (kind === 'body' ? 1 : 0.85) * (bookLh / H);
      if (!(hg > 0)) { // pitch-derived: the block height shared by the lines
        hg = 0.62 * ((block[3] - block[1]) / Math.max(1, count));
        K = 1;
      }
      const few = count <= FEW;
      const titlePage = !skeleton && few && !withBox.length && !detected.length;
      let fs = K * hg * HW;
      if (titlePage) fs = TITLE_FS * HW; // a title page
      if (kind === 'footnote' && bodyFs > 0) fs = Math.min(fs, 0.9 * bodyFs);
      let lh = LINE_H * hg * HW;
      const pad = PAD * hg;
      block = [block[0], clamp(block[1] - pad, 0, 1), block[2], clamp(block[3] + pad, 0, 1)];

      // §4.7 lines: measured extents, snapped to the edges the paragraph shares
      const rows = [];
      for (let k = 0; k < count; k += 1) {
        const l = skeleton ? null : tl[k];
        const box = skeleton ? (detected[k] || null) : l.box;
        rows.push({ l, box, lx0: box ? box[0] : block[0], lx1: box ? box[2] : block[2], shape: 'full', w100: 0, spaces: 0 });
      }
      const boxed = rows.filter((r) => r.box);
      let edgeL = block[0];
      let edgeR = block[2];
      if (boxed.length) {
        const bx0 = Math.min(...boxed.map((r) => r.lx0));
        const bx1 = Math.max(...boxed.map((r) => r.lx1));
        const tol = SNAP_TOL * Math.max(bx1 - bx0, 0.05);
        const tolStart = SNAP_TOL_START * Math.max(bx1 - bx0, 0.05);
        const domL = dominantEdge(boxed.map((r) => r.lx0), tol, false);
        const domR = dominantEdge(boxed.map((r) => r.lx1), tolStart, true);
        edgeL = domL === null ? bx0 : domL;
        edgeR = domR === null ? bx1 : domR;
        boxed.forEach((r) => snapLine(r, edgeL, edgeR, tol, tolStart));
      }

      // one size for the group: lowered only as far as its full lines need to fit their measure
      if (!skeleton) {
        rows.forEach((r) => {
          if (!r.l) return;
          r.w100 = Math.max(1, Number(measure(r.l.words.join(' '))) || 1);
          r.spaces = Math.max(0, r.l.words.length - 1);
        });
        if (!titlePage) {
          const caps = rows.filter((r) => r.l && (r.shape === 'full' || r.shape === 'indent')).map((r) => ((r.lx1 - r.lx0) * 100) / r.w100);
          const typical = mid(caps);
          // lines far longer than their neighbours do not shrink the page: they are tightened instead
          const fitFs = quantile(caps.filter((c) => c >= FIT_OUTLIER * typical), FIT_Q);
          if (fitFs > 0 && fitFs < fs) fs = Math.max(fitFs, FIT_FLOOR * fs);
        }
      }
      const blockH = (block[3] - block[1]) * HW;
      if (blockH > 0 && count * lh > blockH) { // shrink to fit: never overflow, never scroll
        const f = blockH / (count * lh);
        fs *= f;
        lh *= f;
      }

      // each line at that size: justify / start / center, or tightened word spaces, then a slight condense
      const space = (spaceW * fs) / 100;
      const lines = rows.map((r, k) => {
        let fit = 'justify';
        let ws = 0;
        let sx = 1;
        let ratio = 0;
        if (r.l) {
          const textW = (r.w100 * fs) / 100;
          if (r.shape === 'short' && textW > r.lx1 - r.lx0) r.lx0 = Math.max(edgeL, r.lx1 - textW); // a last line may run on to the measure
          if (r.shape === 'indent' && textW > r.lx1 - r.lx0) r.lx1 = Math.min(edgeR, r.lx0 + textW); // a long first line uses its indent
          if (r.shape === 'center' && textW > r.lx1 - r.lx0) {
            const c = (r.lx0 + r.lx1) / 2;
            const w = Math.min(textW, edgeR - edgeL);
            r.lx0 = clamp(c - w / 2, edgeL, edgeR - w);
            r.lx1 = r.lx0 + w;
          }
          const boxW = r.lx1 - r.lx0;
          ratio = textW > 0 ? boxW / textW : 1;
          if (ratio >= 1) {
            if (r.shape === 'short') fit = 'start';
            else if (r.shape === 'center') fit = 'center';
            else if (r.shape === 'indent') fit = ratio >= 1.4 ? 'end' : 'justify'; // flush left, the indent stays visible
            else fit = ratio >= 1.4 ? 'start' : 'justify';
          } else {
            const over = textW - boxW;
            const per = r.spaces > 0 ? Math.min(over / r.spaces, WS_SHRINK * space) : 0;
            ws = -per;
            const rest = textW - per * r.spaces;
            sx = rest > boxW ? boxW / rest : 1;
            fit = 'tight';
            if (sx < SX_MIN) { sx = SX_MIN; fit = 'over'; }
          }
        }
        const line = {
          i: index,
          kind,
          words: r.l ? r.l.words : [],
          tokens: r.l ? r.l.tokens : null,
          box: r.box,
          lx0: r.lx0,
          lx1: r.lx1,
          shape: r.shape,
          r: Math.round(ratio * 1000) / 1000,
          fit,
          ws: Math.round(ws * 1e6) / 1e6,
          sx: Math.round(sx * 1000) / 1000,
          bar: skeleton && !detected.length ? SKELETON_WIDTHS[k % SKELETON_WIDTHS.length] : 100,
        };
        index += 1;
        return line;
      });
      return { kind, block, fsCw: fs, lhCw: lh, few, source, lines };
    }

    const groups = [];
    const body = group('body', bodyT, bodyB, 0);
    if (body) groups.push(body);
    const note = group('footnote', noteT, noteB, body ? body.fsCw : 0);
    if (note) groups.push(note);
    return { ar, groups, rule: fy, mode };
  }

  // ---------------------------------------------------------------- sheet handle (§3, §5, §13.1)
  function boxCss(b) {
    const pct = (v) => `${(100 * v).toFixed(2)}%`;
    return `left:${pct(b[0])};top:${pct(b[1])};width:${pct(b[2] - b[0])};height:${pct(b[3] - b[1])}`;
  }

  function isProcessing(page) {
    return PROCESSING.includes(page.status) && !page.is_excluded && !page.error;
  }

  function decideMode(page, bookActive) {
    if (page.is_excluded) return 'excluded';
    if (page.error) return 'error';
    const lay = textLinesOf(page);
    if (page.text_state === 'final') {
      if (lay === null) return 'skeleton';
      return lay.length ? 'final' : 'empty';
    }
    if (page.text_state === 'provisional' && lay && lay.length) {
      return bookActive && isProcessing(page) ? 'provisional' : 'static';
    }
    return 'skeleton';
  }

  function pageSig(page) {
    const lines = Array.isArray(page.lines) ? page.lines.length : -1;
    const prov = Array.isArray(page.provisional_lines) && page.provisional_lines.length
      ? page.provisional_lines.map((l) => (l.words || []).join(' ')).join('\n')
      : String(page.provisional_text || '');
    const boxes = Array.isArray(page.line_boxes) ? page.line_boxes.length : 0;
    return [page.status, page.text_state, lines, prov.length, prov.slice(0, 64), boxes, page.footnote_y, page.width, page.height].join('|');
  }

  function buildLayer(lay, phase) {
    const layer = el('div', 'fac-layer');
    layer.setAttribute('data-phase', phase);
    layer.setAttribute('aria-hidden', phase === 'final' ? 'false' : 'true');
    const words = [];
    const lineEls = [];
    const blocks = [];
    let bodyX0 = DEFAULT_BODY[0];
    lay.groups.forEach((g) => {
      const block = el('div', 'fac-block');
      block.setAttribute('data-region', g.kind);
      if (g.kind === 'body') bodyX0 = g.block[0];
      setProps(block, { '--x0': fix(g.block[0]), '--y0': fix(g.block[1]), '--x1': fix(g.block[2]), '--y1': fix(g.block[3]), '--fs': fix(g.fsCw), '--lh': fix(g.lhCw) });
      if (g.few) block.setAttribute('data-few', '');
      g.lines.forEach((ln) => {
        const line = el('div', 'fac-line');
        line.setAttribute('data-line', String(ln.i));
        line.style.setProperty('--i', String(Math.min(ln.i, STAGGER_CAP)));
        if (ln.box) {
          setProps(line, { '--lx0': fix(ln.lx0), '--lx1': fix(ln.lx1) });
          line.setAttribute('data-boxed', '');
        }
        if (phase === 'skeleton') {
          const bar = el('span', 'fac-bar');
          bar.style.setProperty('--w', `${ln.bar}%`);
          line.appendChild(bar);
        } else {
          line.setAttribute('data-fit', ln.fit);
          if (ln.ws) line.style.setProperty('--ws', fix(ln.ws));
          if (ln.sx !== 1) line.style.setProperty('--sx', String(ln.sx));
          if (ln.fit === 'over') line.setAttribute('title', ln.words.join(' '));
          if (phase === 'final') line.setAttribute('tabindex', '-1');
          const text = el('span', 'fac-text');
          ln.words.forEach((word, ti) => {
            if (ti > 0) text.appendChild(textNode(' '));
            const span = el('span', 'tok');
            const node = textNode(word);
            span.appendChild(node);
            text.appendChild(span);
            const w = makeWord(node, word, 'provisional');
            w.final = ln.tokens ? ln.tokens[ti] : null;
            w.line = ln.i;
            words.push(w);
          });
          line.appendChild(text);
        }
        block.appendChild(line);
        lineEls[ln.i] = line;
      });
      layer.appendChild(block);
      blocks.push(block);
    });
    if (lay.rule !== null && lay.rule !== undefined) {
      const rule = el('hr', 'fac-rule');
      rule.style.setProperty('--x0', fix(bodyX0));
      rule.style.top = `${(100 * lay.rule).toFixed(2)}%`;
      layer.appendChild(rule);
    }
    return { layer, words, lineEls, blocks };
  }

  function buildScan(s, lay) {
    s.scanBoxes = [];
    if (!s.scan) return;
    clear(s.scan);
    lay.groups.forEach((g) => g.lines.forEach((ln) => {
      if (!ln.box) return;
      const box = el('span', 'sheet-line');
      box.setAttribute('data-line', String(ln.i));
      box.setAttribute('data-n', String(ln.i + 1));
      box.style.cssText = boxCss(ln.box);
      s.scan.appendChild(box);
      s.scanBoxes[ln.i] = box;
    }));
  }

  function dropLayers(s) {
    Object.keys(s.layers).forEach((k) => removeNode(s.layers[k]));
    s.layers = {};
    if (s.leaving) { removeNode(s.leaving); s.leaving = null; }
  }

  function toggleAt(list, i, cls, on) {
    const node = list[i];
    if (node) node.classList.toggle(cls, on);
  }

  function clearLit(s) {
    s.lit.forEach((i) => { toggleAt(s.scanBoxes, i, 'is-lit', false); toggleAt(s.scanBoxes, i, 'is-lit-2', false); toggleAt(s.lineEls, i, 'is-lit', false); });
    s.lit = [];
  }

  // The reading cursor lands on line k: band on the scan, sheen on the text, exact words underneath it.
  function setCursor(s, k, t) {
    const prev = s.cursor;
    clearLit(s);
    s.cursor = k;
    if (k < 0) return;
    toggleAt(s.scanBoxes, k, 'is-lit', true);
    toggleAt(s.lineEls, k, 'is-lit', true);
    s.lit.push(k);
    if (prev >= 0 && prev !== k) { toggleAt(s.scanBoxes, prev, 'is-lit-2', true); s.lit.push(prev); }
    if (s.mode === 'provisional') {
      const until = t + LOCK_MS;
      s.words.forEach((w) => { if (w.line === k) lockExact(w, until, s.budget); });
    }
  }

  function startCursor(s, t) {
    s.cursorOn = true;
    s.cursor = -1;
    s.resting = false;
    s.cursorAt = t;
  }

  function stopCursor(s) {
    s.cursorOn = false;
    clearLit(s);
    s.cursor = -1;
  }

  function tickCursor(s, t) {
    if (t < s.cursorAt) return;
    const n = s.L.length;
    if (!n) return;
    if (s.resting) {
      s.resting = false;
      setCursor(s, 0, t);
      s.cursorAt = t + LINE_MS;
      return;
    }
    const next = s.cursor + 1;
    if (next >= n) {
      setCursor(s, -1, t);
      s.resting = true;
      s.cursorAt = t + CYCLE_REST_MS;
    } else {
      setCursor(s, next, t);
      s.cursorAt = t + LINE_MS;
    }
  }

  function startSweep(s, t) {
    s.sweeping = true;
    s.sweepT0 = t;
    s.sweepStopAt = 0;
    s.figure.classList.add('scan-sweep');
  }

  function stopSweep(s) {
    s.sweeping = false;
    s.sweepStopAt = 0;
    s.figure.classList.remove('scan-sweep');
  }

  function syncSweep(s, t) {
    const page = s.page;
    const want = !api.reducedMotion && Boolean(s.figure) && s.bookActive && page.status === 'uploaded' && !page.is_excluded && !page.error;
    if (want && !s.sweeping) startSweep(s, t);
    else if (!want && s.sweeping && !s.sweepStopAt) {
      const elapsed = (t - s.sweepT0) % SWEEP_MS;
      s.sweepStopAt = t + (SWEEP_MS - elapsed); // finish the current pass, never snap mid-pass
    }
  }

  function startWave(s, built, t) {
    const n = built.words.length;
    const wave = waveMs(n);
    built.words.forEach((w, i) => { w.landAt = t + (n > 1 ? (i / (n - 1)) * wave : 0); });
    s.doneAt = t + wave + DONE_DELAY_MS;
    s.waveLine = -1;
    s.resolving = true;
  }

  function stepWave(s, t) {
    let front = -1;
    s.words.forEach((w) => {
      if (!w.landed && t >= w.landAt) { landWord(w, null); front = w.line; }
    });
    if (front >= 0 && front !== s.waveLine) {
      clearLit(s);
      toggleAt(s.scanBoxes, front, 'is-lit', true);
      s.lit.push(front);
      s.waveLine = front;
    }
    if (t >= s.doneAt) finishWave(s, false);
  }

  function finishWave(s, instant) {
    if (instant) s.words.forEach((w) => { if (!w.landed) landWord(w, null); });
    s.resolving = false;
    clearLit(s);
    if (s.layers.final) {
      s.layers.final.classList.add('is-resolved');
      s.layers.final.setAttribute('aria-hidden', 'false');
    }
    if (s.leaving) { removeNode(s.leaving); s.leaving = null; }
  }

  function mountLayer(s, layer, before) {
    if (before && before.parentNode === s.el && typeof s.el.insertBefore === 'function') s.el.insertBefore(layer, before);
    else s.el.appendChild(layer);
  }

  function sameShape(a, b) {
    if (!a || !b || a.groups.length !== b.groups.length) return false;
    return a.groups.every((g, i) => g.kind === b.groups[i].kind && g.lines.length === b.groups[i].lines.length);
  }

  // Skeleton bars glide to new positions when the geometry changes shape-compatibly (CSS transitions).
  function renderSkeleton(s, lay) {
    if (s.layers.skeleton && sameShape(s.layout, lay)) {
      lay.groups.forEach((g, gi) => {
        const block = s.skeletonBlocks[gi];
        setProps(block, { '--x0': fix(g.block[0]), '--y0': fix(g.block[1]), '--x1': fix(g.block[2]), '--y1': fix(g.block[3]), '--fs': fix(g.fsCw), '--lh': fix(g.lhCw) });
        g.lines.forEach((ln) => {
          const line = s.lineEls[ln.i];
          if (!line) return;
          if (ln.box) { setProps(line, { '--lx0': fix(ln.lx0), '--lx1': fix(ln.lx1) }); line.setAttribute('data-boxed', ''); }
        });
      });
      return;
    }
    const built = buildLayer(lay, 'skeleton');
    dropLayers(s);
    mountLayer(s, built.layer);
    s.layers.skeleton = built.layer;
    s.skeletonBlocks = built.blocks;
    s.lineEls = built.lineEls;
    s.words = [];
  }

  function renderSheet(s, mode, t) {
    const page = s.page;
    const prev = s.mode;
    s.mode = mode;
    s.el.setAttribute('data-mode', mode);
    if (s.el.dataset) s.el.dataset.mode = mode;
    stopCursor(s);
    s.resolving = false;
    s.hotIndex = -1;
    if (mode === 'excluded' || mode === 'error' || mode === 'empty') {
      dropLayers(s);
      buildScan(s, { groups: [] });
      s.layout = null;
      s.L = [];
      s.words = [];
      s.lineEls = [];
      return;
    }
    const lay = layout(page);
    buildScan(s, lay);
    s.L = [];
    lay.groups.forEach((g) => g.lines.forEach((ln) => s.L.push(ln)));
    if (mode === 'skeleton') {
      renderSkeleton(s, lay);
      s.layout = lay;
      if (!api.reducedMotion && s.bookActive && isProcessing(page)) startCursor(s, t);
      return;
    }
    s.layout = lay;
    if (mode === 'provisional' || mode === 'static') {
      const built = buildLayer(lay, 'provisional');
      dropLayers(s);
      mountLayer(s, built.layer);
      s.layers.provisional = built.layer;
      s.words = built.words;
      s.lineEls = built.lineEls;
      s.budget = { n: 0 };
      if (mode === 'static' || api.reducedMotion) {
        built.layer.classList.add('is-static');
        s.mode = 'static';
        s.el.setAttribute('data-mode', 'static');
        if (s.el.dataset) s.el.dataset.mode = 'static';
      } else {
        built.words.forEach((w) => phaseWord(w, t));
        startCursor(s, t);
      }
      return;
    }
    // final
    const built = buildLayer(lay, 'final');
    const wave = !api.reducedMotion && (prev === 'provisional' || prev === 'skeleton') && built.words.length > 0;
    if (wave) {
      const old = s.layers.provisional || s.layers.skeleton || null;
      if (s.leaving) { removeNode(s.leaving); s.leaving = null; }
      s.layers = {};
      if (old) {
        old.classList.add('is-leaving');
        s.leaving = old;
        s.leaveAt = t + LEAVE_MS;
        mountLayer(s, built.layer, old); // beneath the leaving provisional layer
      } else {
        mountLayer(s, built.layer);
      }
      startWave(s, built, t);
    } else {
      dropLayers(s);
      mountLayer(s, built.layer);
      built.layer.classList.add('is-resolved');
      built.words.forEach((w) => {
        w.landed = true;
        const span = spanOf(w);
        if (span && isLow(w.final)) span.classList.add('tok-low');
      });
    }
    s.layers.final = built.layer;
    s.words = built.words;
    s.lineEls = built.lineEls;
  }

  function tickSheet(s, t) {
    if (s.sweeping) {
      if (!s.visible) { if (s.sweepStopAt) stopSweep(s); }
      else {
        s.figure.style.setProperty('--sweep', (((t - s.sweepT0) % SWEEP_MS) / SWEEP_MS).toFixed(3));
        if (s.sweepStopAt && t >= s.sweepStopAt) stopSweep(s);
      }
    }
    if (s.leaving && t >= s.leaveAt) { removeNode(s.leaving); s.leaving = null; }
    if (s.resolving) {
      if (!s.visible) finishWave(s, true);
      else stepWave(s, t);
    } else if (s.visible) {
      if (s.cursorOn) tickCursor(s, t);
      if (s.mode === 'provisional') s.words.forEach((w) => tickWord(w, t, s.budget));
    }
    s.active = s.sweeping || Boolean(s.leaving) || s.resolving || s.cursorOn || s.mode === 'provisional';
  }

  function fontLoaded() {
    try {
      return !(typeof document !== 'undefined' && document.fonts && typeof document.fonts.check === 'function') || document.fonts.check(`16px ${FONT}`);
    } catch (e) {
      return true;
    }
  }

  function sheet(host, options) {
    const opts = options || {};
    const s = {
      el: host,
      sheet: true,
      scan: opts.scan || null,
      figure: opts.figure || null,
      active: false,
      visible: true,
      mode: '',
      page: {},
      bookActive: false,
      sig: '',
      layout: null,
      layers: {},
      skeletonBlocks: [],
      L: [],
      words: [],
      lineEls: [],
      scanBoxes: [],
      budget: { n: 0 },
      lit: [],
      cursor: -1,
      cursorOn: false,
      cursorAt: 0,
      resting: false,
      resolving: false,
      waveLine: -1,
      doneAt: 0,
      hotIndex: -1,
      sweeping: false,
      sweepT0: 0,
      sweepStopAt: 0,
      leaving: null,
      leaveAt: 0,
      fontOk: fontLoaded(),
    };
    registry.set(host, s);
    watch(s);
    if (!s.fontOk) {
      try {
        document.fonts.load(`16px ${FONT}`).then(() => {
          if (s.fontOk || !registry.has(host)) return;
          s.fontOk = true;
          s.sig = ''; // the width fit was measured with a fallback font: lay out once more
          handle.update({});
        }, () => { s.fontOk = true; });
      } catch (e) { s.fontOk = true; }
    }
    const handle = {
      update(o) {
        const opt = o || {};
        const t = clock();
        if (opt.page) s.page = opt.page;
        if (opt.active !== undefined) s.bookActive = Boolean(opt.active);
        const page = s.page || {};
        const mode = decideMode(page, s.bookActive);
        const sig = pageSig(page);
        syncSweep(s, t);
        const dataChanged = sig !== s.sig;
        s.sig = sig;
        if (mode !== s.mode || dataChanged) {
          const wasStatic = s.mode === 'static' && mode === 'provisional' && !dataChanged;
          if (wasStatic && s.layers.provisional) { // the book resumed: wake the same layer
            s.layers.provisional.classList.remove('is-static');
            s.mode = 'provisional';
            s.el.setAttribute('data-mode', 'provisional');
            if (s.el.dataset) s.el.dataset.mode = 'provisional';
            s.words.forEach((w) => phaseWord(w, t));
            startCursor(s, t);
          } else if (s.mode === 'provisional' && mode === 'static' && !dataChanged && s.layers.provisional) {
            // the book stopped: freeze on Tesseract's words, no rebuild
            stopCursor(s);
            s.words.forEach((w) => unveil(w, s.budget));
            s.layers.provisional.classList.add('is-static');
            s.mode = 'static';
            s.el.setAttribute('data-mode', 'static');
            if (s.el.dataset) s.el.dataset.mode = 'static';
          } else if (s.mode === 'skeleton' && mode === 'skeleton' && !dataChanged) {
            if (s.cursorOn && !(s.bookActive && isProcessing(page))) stopCursor(s);
            else if (!s.cursorOn && !api.reducedMotion && s.bookActive && isProcessing(page)) startCursor(s, t);
          } else {
            renderSheet(s, mode, t);
          }
        }
        s.active = s.sweeping || Boolean(s.leaving) || s.resolving || s.cursorOn || s.mode === 'provisional';
        if (s.active) wake();
        return s.mode;
      },
      lit(i) {
        if (!s.cursorOn && i >= 0) { s.cursorOn = true; s.cursorAt = Infinity; }
        setCursor(s, i, clock());
        if (i < 0) stopCursor(s);
      },
      hot(i) {
        if (s.hotIndex >= 0) { toggleAt(s.scanBoxes, s.hotIndex, 'is-hot', false); toggleAt(s.lineEls, s.hotIndex, 'is-hot', false); }
        s.hotIndex = i >= 0 && i < s.L.length ? i : -1;
        if (s.hotIndex >= 0) { toggleAt(s.scanBoxes, s.hotIndex, 'is-hot', true); toggleAt(s.lineEls, s.hotIndex, 'is-hot', true); }
        return s.hotIndex;
      },
      destroy() {
        registry.delete(host);
        unwatch(host);
      },
      get mode() { return s.mode; },
      get lines() { return s.L.length; },
      get hotIndex() { return s.hotIndex; },
      get cursor() { return s.cursor; },
      get layout() { return s.layout; },
      lineEl(i) { return s.lineEls[i] || null; },
      scanBox(i) { return s.scanBoxes[i] || null; },
    };
    return handle;
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
    sheet,
    layout,
    measure: browserMeasure,
    reducedMotion: prefersReducedMotion(),
    get size() { return registry.size; },
    params: { FPS, PERIOD_MS, DWELL_SHARE, VEIL_MAX, LINE_MS, CYCLE_REST_MS, LOCK_MS, WAVE_PER_WORD_MS, WAVE_MIN_MS, WAVE_MAX_MS, DONE_DELAY_MS, SWEEP_MS, FAC_K },
  };

  window.NassakhDecode = api;
})();
