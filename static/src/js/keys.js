// One keymap by physical key (PHASE7_SPEC §3.15, D69): `window.NassakhKeys`, used by every screen's keyAction.
// A shortcut is the key's place, not the character it types: with the Arabic layout the A key types «ش», and it
// still means A. Digits are read in any script (0–9, ٠–٩, ۰–۹). A key pressed while an IME composes is never a
// shortcut (every map returns null for it).
//   composing(e)       true while an input method composes (`isComposing`, or the 229 keyCode of old engines)
//   letter(e, allow)   'a'…'z' from `e.code` (KeyA…KeyZ), else from a Latin `e.key`; null for anything else.
//                      A letter never matches with ⇧, ⌘, Ctrl or ⌥ unless `allow` names them: a space-separated
//                      list of 'shift', 'alt', 'mod' (⌘ or Ctrl), or 'any'
//   digit(e)           0–9 as a number: from `e.key` in any script, or from Digit*/Numpad* without ⇧; never with
//                      ⌘, Ctrl or ⌥; else null
//   is(e, ch)          '?' (?, ؟, or ⇧ with Slash); '[' and ']' by code (the Arabic layout types «ج» and «د»
//                      there); '/' by code without ⇧. Never with ⌘, Ctrl or ⌥
//   plus(e) / minus(e) the zoom keys: + = / - _ − by character, Equal / Minus / NumpadAdd / NumpadSubtract by code
//   mod(e)             ⌘ or Ctrl held
//   printable(e)       what the key types (a Latin or Arabic letter, a digit, a mark, the layout's «لا»), or ''
//                      (named keys, ⌘ / Ctrl / ⌥ chords, a composition)
// Pure: no DOM, so the Node tests load it as it is.
(function () {
  'use strict';

  const root = typeof window !== 'undefined' ? window : globalThis;
  const ARABIC_ZERO = 0x0660; // ٠
  const PERSIAN_ZERO = 0x06f0; // ۰ (Extended Arabic-Indic, the Persian and Urdu layouts)

  const composing = (e) => Boolean(e && (e.isComposing || e.keyCode === 229));
  const mod = (e) => Boolean(e && (e.metaKey || e.ctrlKey));

  // Whether the modifiers held are all ones `allow` names ('shift', 'alt', 'mod', 'any').
  function allowed(e, allow) {
    const names = String(allow || '').split(/\s+/);
    if (names.includes('any')) return true;
    if (e.shiftKey && !names.includes('shift')) return false;
    if (e.altKey && !names.includes('alt')) return false;
    if ((e.metaKey || e.ctrlKey) && !names.includes('mod')) return false;
    return true;
  }

  function letter(e, allow) {
    if (!e || composing(e) || !allowed(e, allow)) return null;
    const code = String(e.code || '');
    const byCode = /^Key([A-Z])$/.exec(code);
    if (byCode) return byCode[1].toLowerCase();
    // no code (a synthetic event, an old engine): the character, when it is a Latin letter
    const k = String(e.key || '');
    if (!code && /^[A-Za-z]$/.test(k)) return k.toLowerCase();
    return null;
  }

  // The value of one digit character in any script, else null.
  function digitOf(ch) {
    if (typeof ch !== 'string' || ch.length !== 1) return null;
    if (ch >= '0' && ch <= '9') return ch.charCodeAt(0) - 48;
    const c = ch.charCodeAt(0);
    if (c >= ARABIC_ZERO && c <= ARABIC_ZERO + 9) return c - ARABIC_ZERO;
    if (c >= PERSIAN_ZERO && c <= PERSIAN_ZERO + 9) return c - PERSIAN_ZERO;
    return null;
  }

  function digit(e) {
    if (!e || composing(e) || e.altKey || mod(e)) return null;
    const fromKey = digitOf(e.key);
    if (fromKey !== null) return fromKey;
    const byCode = /^(Digit|Numpad)([0-9])$/.exec(String(e.code || ''));
    // ⇧ with a digit key is a symbol (!, @ …); a numpad key without NumLock is a named key (End, ArrowDown …)
    if (!byCode || e.shiftKey) return null;
    if (byCode[1] === 'Numpad' && String(e.key || '').length > 1) return null;
    return Number(byCode[2]);
  }

  function is(e, ch) {
    if (!e || composing(e) || e.altKey || mod(e)) return false;
    const k = e.key;
    const code = String(e.code || '');
    switch (ch) {
      case '?': return k === '?' || k === '؟' || (code === 'Slash' && Boolean(e.shiftKey));
      case '[': return code ? code === 'BracketLeft' && !e.shiftKey : k === '[';
      case ']': return code ? code === 'BracketRight' && !e.shiftKey : k === ']';
      case '/': return code ? code === 'Slash' && !e.shiftKey : k === '/';
      default: return false;
    }
  }

  function plus(e) {
    if (!e || composing(e) || e.altKey || mod(e)) return false;
    return e.key === '+' || e.key === '=' || e.code === 'Equal' || e.code === 'NumpadAdd';
  }

  function minus(e) {
    if (!e || composing(e) || e.altKey || mod(e)) return false;
    return e.key === '-' || e.key === '_' || e.key === '−' || e.code === 'Minus' || e.code === 'NumpadSubtract';
  }

  function printable(e) {
    if (!e || composing(e) || e.altKey || mod(e)) return '';
    const k = typeof e.key === 'string' ? e.key : '';
    // what the key types: one character, a mark («ً»), or the two letters of the Arabic layout's «لا» key; a
    // named key (Enter, ArrowLeft, F1, Dead, Unidentified …) is a capitalised identifier and types nothing
    if (!k || /^[A-Z][A-Za-z0-9]+$/.test(k)) return '';
    return Array.from(k).length <= 2 ? k : '';
  }

  root.NassakhKeys = { composing, mod, letter, digit, digitOf, is, plus, minus, printable };
})();
