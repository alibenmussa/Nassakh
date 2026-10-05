/* The landing page's few behaviours (templates/core/landing.html; styles in components/landing.css).
   Plain script, no library: the header's hairline once scrolled; the reveal-on-scroll fallback (`is-in`) where the
   browser has no CSS scroll-driven animations; the sticky stepper of the six stages; the quotation check's tabs
   (role=tablist, arrow keys) with a gentle auto-cycle until the visitor touches it; the MCP tools that highlight
   in turn with their call card. Nothing here is needed to read the page: with JS off everything is visible
   (landing.css keys the hidden states on `html.ld-js`). `html.ld-static` (screenshots) and prefers-reduced-motion
   switch the auto-cycles off. */
(function () {
  'use strict';
  const html = document.documentElement;
  const isStatic = html.classList.contains('ld-static');
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const scrollDriven = Boolean(window.CSS && CSS.supports && CSS.supports('animation-timeline: view()'));
  const hasIO = 'IntersectionObserver' in window;

  /** Run `fn` once, when `el` first comes into view (at once without IntersectionObserver). */
  function whenVisible(el, fn) {
    if (!hasIO) return fn();
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { io.disconnect(); fn(); }
    }, { threshold: 0.2 });
    io.observe(el);
  }

  // the header: a hairline once the page has scrolled
  const header = document.querySelector('[data-ld-header]');
  if (header) {
    const onScroll = () => header.classList.toggle('is-scrolled', window.scrollY > 8);
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
  }

  // reveal on scroll, where the browser cannot drive the animation from the scroll itself
  if (!scrollDriven && !isStatic) {
    const all = document.querySelectorAll('.ld-r');
    if (hasIO) {
      const io = new IntersectionObserver((entries) => {
        for (const e of entries) if (e.isIntersecting) { e.target.classList.add('is-in'); io.unobserve(e.target); }
      }, { rootMargin: '0px 0px -8% 0px', threshold: 0.04 });
      all.forEach((el) => io.observe(el));
    } else {
      all.forEach((el) => el.classList.add('is-in'));
    }
  }

  // the six stages: the panel crossing the middle band of the viewport names the active step
  const stages = document.querySelector('[data-ld-stages]');
  if (stages && hasIO) {
    const steps = Array.from(stages.querySelectorAll('.ld-stepper li'));
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        const n = Number(e.target.dataset.stage);
        steps.forEach((li, i) => { li.classList.toggle('is-active', i + 1 === n); li.classList.toggle('is-done', i + 1 < n); });
      }
    }, { rootMargin: '-38% 0px -47% 0px', threshold: 0 });
    stages.querySelectorAll('.ld-stage').forEach((panel) => io.observe(panel));
  }

  // the quotation check: four answers as tabs; they cycle on their own until the visitor touches them
  const check = document.querySelector('[data-ld-check]');
  if (check) {
    const tabs = Array.from(check.querySelectorAll('[role="tab"]'));
    const panels = Array.from(check.querySelectorAll('[role="tabpanel"]'));
    let timer = 0;
    let current = Math.max(0, tabs.findIndex((t) => t.getAttribute('aria-selected') === 'true'));
    const select = (index, focus) => {
      current = index;
      tabs.forEach((t, i) => {
        const on = i === index;
        t.classList.toggle('is-selected', on);
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.tabIndex = on ? 0 : -1;
      });
      const id = tabs[index].getAttribute('aria-controls');
      panels.forEach((p) => p.classList.toggle('is-current', p.id === id));
      if (focus) tabs[index].focus();
    };
    const stop = () => { if (timer) { window.clearInterval(timer); timer = 0; } };
    const rtl = html.dir === 'rtl';
    tabs.forEach((tab, i) => {
      tab.addEventListener('click', () => { stop(); select(i); });
      tab.addEventListener('keydown', (e) => {
        let next = -1;
        if (e.key === 'ArrowRight') next = (i + (rtl ? -1 : 1) + tabs.length) % tabs.length;
        else if (e.key === 'ArrowLeft') next = (i + (rtl ? 1 : -1) + tabs.length) % tabs.length;
        else if (e.key === 'Home') next = 0;
        else if (e.key === 'End') next = tabs.length - 1;
        if (next < 0) return;
        e.preventDefault();
        stop();
        select(next, true);
      });
    });
    check.addEventListener('pointerdown', stop);
    check.addEventListener('focusin', stop);
    if (!isStatic && !reduced) {
      whenVisible(check, () => { timer = window.setInterval(() => select((current + 1) % tabs.length), 4000); });
    }
  }

  // the MCP tools: each lights up in turn with its call and result; a click picks one and stops the turn
  const mcp = document.querySelector('[data-ld-mcp]');
  if (mcp) {
    const tools = Array.from(mcp.querySelectorAll('.ld-tool'));
    const frames = Array.from(mcp.querySelectorAll('.ld-call-frame'));
    let timer = 0;
    let current = Math.max(0, tools.findIndex((li) => li.classList.contains('is-active')));
    const show = (index) => {
      current = index;
      tools.forEach((li, i) => {
        li.classList.toggle('is-active', i === index);
        li.querySelector('button').setAttribute('aria-pressed', i === index ? 'true' : 'false');
      });
      frames.forEach((f, i) => f.classList.toggle('is-current', i === index));
    };
    const stop = () => { if (timer) { window.clearInterval(timer); timer = 0; } };
    tools.forEach((li, i) => li.querySelector('button').addEventListener('click', () => { stop(); show(i); }));
    mcp.addEventListener('focusin', stop);
    if (!isStatic && !reduced) {
      whenVisible(mcp, () => { timer = window.setInterval(() => show((current + 1) % tools.length), 3400); });
    }
  }
})();
