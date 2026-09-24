// Shared UI helpers used by every screen:
//   Alpine.store('toast')   – one quiet toast at a time: $store.toast.show('تم النسخ')
//   window.Nassakh.plainText(text)  – clipboard-ready text: Western digits, normalised whitespace, no markup
//   window.Nassakh.copyText(text or Promise<text>) – copy plain text, show «تم النسخ» (true on success)
// Diacritics and other letters are never touched; only digits and whitespace are normalised.
(function () {
  'use strict';

  const TOAST_MS = 3000;
  const EASTERN_DIGITS = /[٠-٩۰-۹]/g;

  function plainText(value) {
    return String(value || '')
      .replace(EASTERN_DIGITS, (d) => {
        const code = d.charCodeAt(0);
        return String(code >= 0x06f0 ? code - 0x06f0 : code - 0x0660);
      })
      .replace(/\r\n?/g, '\n')
      .replace(/[^\S\n]+/g, ' ') // tabs, no-break and other spaces → one real space (ZWNJ/ZWJ are kept)
      .split('\n')
      .map((line) => line.trim())
      .join('\n')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }

  function legacyCopy(text) {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
    area.remove();
    return ok;
  }

  function toast(message) {
    if (window.Alpine && Alpine.store('toast')) Alpine.store('toast').show(message);
  }

  // `value` may be a string or a Promise of one (text fetched after the click). A promise goes
  // through ClipboardItem when available so Safari still sees the click as the user gesture.
  async function copyText(value) {
    let ok = false;
    let empty = false;
    try {
      if (value && typeof value.then === 'function' && window.ClipboardItem && navigator.clipboard && navigator.clipboard.write) {
        const blob = Promise.resolve(value).then((raw) => {
          const text = plainText(raw);
          if (!text) { empty = true; throw new Error('empty'); }
          return new Blob([text], { type: 'text/plain' });
        });
        await navigator.clipboard.write([new ClipboardItem({ 'text/plain': blob })]);
        ok = true;
      } else {
        const text = plainText(await value);
        if (!text) {
          empty = true;
        } else if (navigator.clipboard && window.isSecureContext) {
          try {
            await navigator.clipboard.writeText(text);
            ok = true;
          } catch (_) {
            ok = legacyCopy(text);
          }
        } else {
          ok = legacyCopy(text);
        }
      }
    } catch (_) {
      ok = false;
    }
    toast(ok ? 'تم النسخ' : (empty ? 'لا نص لنسخه بعد' : 'تعذّر النسخ'));
    return ok;
  }

  window.Nassakh = Object.assign(window.Nassakh || {}, { plainText, copyText, toast });

  document.addEventListener('alpine:init', () => {
    Alpine.store('toast', {
      message: '',
      visible: false,
      timer: null,
      show(message, ms = TOAST_MS) {
        clearTimeout(this.timer);
        this.message = message;
        this.visible = true;
        this.timer = setTimeout(() => { this.visible = false; }, ms);
      },
      hide() {
        clearTimeout(this.timer);
        this.visible = false;
      },
    });
  });
})();
