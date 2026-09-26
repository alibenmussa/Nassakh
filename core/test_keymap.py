"""One keymap by physical key (PHASE7_SPEC §3.15, D69), under Node.

`static/src/js/keys.js` (`window.NassakhKeys`) on its own, then every screen's `keyAction` fed the same
keys from the Latin and the Arabic layout: the dashboard (`books.js`, the «التخطيط» mode too), review (page
mode and word mode), the manuscript and the book page (preview and edit). §3.15's table is held here as data:
a bare letter or digit key has one meaning across screens, the Arabic layout gives the meaning of the Latin
one, and an IME composition is never a shortcut. Also the shell (`templates/base.html`): keys.js loaded after
ui.js, and the shared toast's «تراجع» form for a message that carries `undo:`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from django.contrib.auth.models import User
from django.contrib.messages.storage.base import Message
from django.template.loader import render_to_string
from django.test import RequestFactory

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "src" / "js"
NODE = shutil.which("node")

# The Arabic (101) layout: what each key types there. Letters by `KeyboardEvent.code`, digits in both
# Arabic-Indic scripts, the question mark and the brackets by their place.
ARABIC = {
    "a": "ش", "b": "لا", "c": "ؤ", "d": "ي", "e": "ث", "f": "ب", "g": "ل", "h": "ا", "i": "ه", "j": "ت",
    "k": "ن", "l": "م", "m": "ة", "n": "ى", "o": "خ", "p": "ح", "q": "ض", "r": "ق", "s": "س", "t": "ف",
    "u": "ع", "v": "ر", "w": "ص", "x": "ء", "y": "غ", "z": "ئ",
}  # fmt: skip
LETTERS = "abcdefghijklmnopqrstuvwxyz"
DIGITS = "0123456789"

# §3.15's table with the cells 7c fills (§5.8): review's G (the pager's jump field), O (the processed image or
# the original: the source), V (the sides swapped: the view), C (the page's text copied), the dashboard's «?».
# Key → {screen: meaning}; a key or screen not listed has no meaning there.
TABLE: dict[str, dict[str, str]] = {
    "g": {"dashboard": "jump", "review": "jump", "manuscript": "jump", "book": "jump"},
    "n": {"dashboard": "nextReview", "review": "nextReview"},
    "a": {"review": "approve"},
    "e": {"review": "edit", "book": "edit"},
    "o": {"review": "source", "manuscript": "source", "book": "source"},
    "s": {"manuscript": "marks", "book": "marks"},
    "v": {"dashboard": "view", "review": "view", "book": "view"},
    "c": {"dashboard": "copy", "review": "copy"},
    "j": {"manuscript": "nextBlock"},
    "k": {"manuscript": "prevBlock"},
    "]": {"manuscript": "nextWarning"},
    "[": {"manuscript": "prevWarning"},
    "?": {"dashboard": "sheet", "review": "sheet", "manuscript": "sheet", "book": "sheet"},
    "+": {"review": "zoomIn", "book": "zoomIn"},
    "-": {"review": "zoomOut", "book": "zoomOut"},
    "0": {"review": "fit", "book": "fit"},
    **{d: {"review": "reading"} for d in "123456789"},
}

HARNESS = r"""
const [, , jsDir, keysJson] = process.argv;
const fs = require('fs');
const reg = {}; const inits = [];
globalThis.window = globalThis;
globalThis.document = { hidden: false, addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); }, removeEventListener: () => {},
  getElementById: () => null, querySelector: () => null, querySelectorAll: () => [], createElement: () => ({ style: {} }) };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: () => null };
globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.sessionStorage = { getItem: () => null, setItem: () => {} };
globalThis.setTimeout = () => 1; globalThis.clearTimeout = () => {};
for (const f of ['ui.js', 'keys.js', 'books.js', 'review.js', 'manuscript.js', 'book/geometry.js']) eval(fs.readFileSync(`${jsDir}/${f}`, 'utf8'));
inits.forEach((fn) => fn());
const K = window.NassakhKeys;
const out = {};

// ---- keys.js on its own
out.letter = [K.letter({ key: 'ش', code: 'KeyA' }), K.letter({ key: 'a', code: 'KeyA' }), K.letter({ key: 'A' }), K.letter({ key: 'ش' }),
  K.letter({ key: 'A', code: 'KeyA', shiftKey: true }), K.letter({ key: 'A', code: 'KeyA', shiftKey: true }, 'shift'),
  K.letter({ key: 'z', code: 'KeyZ', metaKey: true }), K.letter({ key: 'z', code: 'KeyZ', metaKey: true }, 'mod'), K.letter({ key: 'z', code: 'KeyZ', ctrlKey: true }, 'mod'),
  K.letter({ key: 'å', code: 'KeyA', altKey: true }), K.letter({ key: 'å', code: 'KeyA', altKey: true }, 'any'), K.letter({ key: '1', code: 'Digit1' }),
  K.letter({ key: 'ش', code: 'KeyA', isComposing: true }), K.letter({ key: 'Process', code: 'KeyA', keyCode: 229 })];
out.digit = [K.digit({ key: '٢', code: 'Digit2' }), K.digit({ key: '۲', code: 'Digit2' }), K.digit({ key: '2', code: 'Digit2' }), K.digit({ key: '&', code: 'Digit1' }),
  K.digit({ key: '!', code: 'Digit1', shiftKey: true }), K.digit({ key: '7', code: 'Numpad7' }), K.digit({ key: 'Home', code: 'Numpad7' }), K.digit({ key: '٩' }),
  K.digit({ key: '1', code: 'Digit1', metaKey: true }), K.digit({ key: '1', code: 'Digit1', altKey: true }), K.digit({ key: 'a', code: 'KeyA' }), K.digit({ key: '٠', code: 'Digit0', isComposing: true })];
out.is = [K.is({ key: '?', code: 'Slash', shiftKey: true }, '?'), K.is({ key: '؟', code: 'Slash', shiftKey: true }, '?'), K.is({ key: 'ظ', code: 'Slash', shiftKey: true }, '?'),
  K.is({ key: '/', code: 'Slash' }, '?'), K.is({ key: 'ج', code: 'BracketLeft' }, '['), K.is({ key: '[' }, '['), K.is({ key: '{', code: 'BracketLeft', shiftKey: true }, '['),
  K.is({ key: 'د', code: 'BracketRight' }, ']'), K.is({ key: '[', code: 'BracketLeft', metaKey: true }, '['), K.is({ key: '?', isComposing: true }, '?')];
out.zoom = [K.plus({ key: '+', code: 'Equal', shiftKey: true }), K.plus({ key: '=', code: 'Equal' }), K.plus({ key: '+', code: 'NumpadAdd' }), K.plus({ key: '+', code: 'Equal', metaKey: true }),
  K.minus({ key: '-', code: 'Minus' }), K.minus({ key: '_', code: 'Minus', shiftKey: true }), K.minus({ key: '-', code: 'NumpadSubtract' }), K.minus({ key: '−' }), K.minus({ key: 'x', code: 'KeyX' })];
out.printable = [K.printable({ key: 'ش', code: 'KeyA' }), K.printable({ key: 'لا', code: 'KeyB' }), K.printable({ key: '؟', code: 'Slash', shiftKey: true }), K.printable({ key: 'ً', code: 'KeyQ', shiftKey: true }),
  K.printable({ key: 'Enter' }), K.printable({ key: 'ArrowLeft' }), K.printable({ key: 'F1' }), K.printable({ key: 'Dead' }), K.printable({ key: 'a', metaKey: true }),
  K.printable({ key: 'å', altKey: true }), K.printable({ key: 'ش', isComposing: true }), K.printable({ key: 'A', shiftKey: true })];

// ---- every screen's map, fed the Latin and the Arabic key
const dash = reg.bookDashboard({ bookId: 1, canEdit: true, status: 'ocr', active: false });
const guides = reg.bookDashboard({ bookId: 1, canEdit: true, status: 'needs_guides', active: false, guidesMode: true, layoutStage: true, startAction: 'startOcr' });
const R = window.NassakhReview.keyAction; const M = window.NassakhManuscript.keyAction; const B = window.NassakhBook.geo.keyAction;
const readings = ['1', '2', '3', '4', '5', '6', '7', '8', '9'];
const page = { inField: false, inPop: false, inFlow: true, focused: true, open: false, optionKeys: readings };
const SCREENS = {
  dashboard: [(e) => dash.keyAction(e, false), (e) => guides.keyAction(e, false)],
  review: [(e) => R(e, page), (e) => R(e, { ...page, focused: false, optionKeys: [] })],
  manuscript: [(e) => M(e, { inField: false, inMenu: false, hasBlock: true })],
  book: [(e) => B(e, { mode: 'preview' }), (e) => B(e, { mode: 'edit' })],
};
const MEANING = {
  dashboard: { jump: 'jump', nextReview: 'nextReview', copy: 'copy', toggleView: 'view', sheets: 'view', grid: 'view', sheet: 'sheet' },
  review: { approve: 'approve', edit: 'edit', nextReview: 'nextReview', sheet: 'sheet', zoomIn: 'zoomIn', zoomOut: 'zoomOut', zoomReset: 'fit', jump: 'jump', toggleScan: 'source', swap: 'view', copy: 'copy' },
  manuscript: { jump: 'jump', seams: 'marks', source: 'source', next: 'nextBlock', prev: 'prevBlock', nextWarning: 'nextWarning', prevWarning: 'prevWarning', sheet: 'sheet' },
  book: { jump: 'jump', mode: 'edit', source: 'source', marks: 'marks', spread: 'view', fitIn: 'zoomIn', fitOut: 'zoomOut', fitHeight: 'fit', sheet: 'sheet' },
};
const meaning = (screen, action) => {
  if (!action) return null;
  if (screen === 'review' && /^choose[1-9]$/.test(action)) return 'reading';
  return MEANING[screen][action] || `${screen}:${action}`; // an action the table does not know fails the comparison
};
const keys = JSON.parse(keysJson); // name → {latin: [events], arabic: [events]}
out.map = {};
out.layouts = {};
for (const [name, forms] of Object.entries(keys)) {
  out.map[name] = {};
  for (const [screen, maps] of Object.entries(SCREENS)) {
    const seen = {};
    for (const [layout, events] of Object.entries(forms)) {
      seen[layout] = [...new Set(events.flatMap((ev) => maps.map((fn) => meaning(screen, fn(ev)))).filter(Boolean))].sort();
    }
    out.map[name][screen] = seen.latin;
    if (JSON.stringify(seen.latin) !== JSON.stringify(seen.arabic)) out.layouts[`${name}@${screen}`] = seen;
  }
}
// ---- a composition is never a shortcut, on any screen
const composing = [{ key: 'ش', code: 'KeyA', isComposing: true }, { key: 'Process', code: 'KeyV', keyCode: 229 }, { key: '٢', code: 'Digit2', isComposing: true }, { key: '؟', code: 'Slash', shiftKey: true, isComposing: true }];
out.composing = Object.fromEntries(Object.entries(SCREENS).map(([screen, maps]) => [screen, composing.flatMap((ev) => maps.map((fn) => fn(ev))).filter(Boolean)]));
// ---- review's two modes
const word = { ...page, open: true, optionKeys: ['1', '2'] };
out.review = {
  shinOpen: R({ key: 'ش', code: 'KeyA' }, word), shinClosed: R({ key: 'ش', code: 'KeyA' }, page),
  cmdEnterWord: R({ key: 'Enter', code: 'Enter', metaKey: true }, word), cmdEnterInCorrection: R({ key: 'Enter', metaKey: true }, { ...word, inField: true, inPop: true }),
  cmdEnterInLineEditor: R({ key: 'Enter', metaKey: true }, { ...page, inField: true }),
  digitBeyond: R({ key: '٣', code: 'Digit3' }, word), digitReading: R({ key: '٢', code: 'Digit2' }, word), digitBeyondPage: R({ key: '٣', code: 'Digit3' }, { ...page, optionKeys: ['1', '2'] }),
  questionOpen: R({ key: '؟', code: 'Slash', shiftKey: true }, word), minusOpen: R({ key: '-', code: 'Minus' }, word), zeroOpen: R({ key: '0', code: 'Digit0' }, word),
  spaceClosed: R({ key: ' ', code: 'Space' }, page), spaceOpen: R({ key: ' ', code: 'Space' }, word), spaceUnfocused: R({ key: ' ', code: 'Space' }, { ...page, focused: false }),
  otherLetterClosed: R({ key: 'ق', code: 'KeyR' }, page), home: R({ key: 'Home' }, page), end: R({ key: 'End' }, word), pageDown: R({ key: 'PageDown' }, page),
  shiftA: R({ key: 'A', code: 'KeyA', shiftKey: true }, page), altA: R({ key: 'å', code: 'KeyA', altKey: true }, word),
  tabOpen: R({ key: 'Tab' }, word), enterOpen: R({ key: 'Enter' }, word), escOpen: R({ key: 'Escape' }, word), mergeOpen: R({ key: 'ArrowLeft', altKey: true }, word),
  undo: R({ key: 'z', code: 'KeyZ', metaKey: true }, page), undoArabic: R({ key: 'ئ', code: 'KeyZ', metaKey: true }, page),
  // §5.8: the page's letters stay quiet in word mode (they type), and ⌥F there is «تصحيح في كل الكتاب»
  gOpen: R({ key: 'ل', code: 'KeyG' }, word), cOpen: R({ key: 'ؤ', code: 'KeyC' }, word),
  altF: R({ key: 'ƒ', code: 'KeyF', altKey: true }, word), altFArabic: R({ key: 'ب', code: 'KeyF', altKey: true }, word),
  altFClosed: R({ key: 'ƒ', code: 'KeyF', altKey: true }, page), altFGap: R({ key: 'ƒ', code: 'KeyF', altKey: true }, { ...word, gap: true }),
};
console.log(JSON.stringify(out));
"""  # noqa: E501


def _event_forms() -> dict:
    """Every key of the table's rows, as the Latin and the Arabic layout send it."""
    forms: dict[str, dict[str, list[dict]]] = {}
    for letter in LETTERS:
        code = f"Key{letter.upper()}"
        forms[letter] = {
            "latin": [{"key": letter, "code": code}],
            "arabic": [{"key": ARABIC[letter], "code": code}],
        }
    for d in DIGITS:
        arabic = chr(0x0660 + int(d))
        persian = chr(0x06F0 + int(d))
        forms[d] = {
            "latin": [{"key": d, "code": f"Digit{d}"}, {"key": d, "code": f"Numpad{d}"}],
            "arabic": [{"key": arabic, "code": f"Digit{d}"}, {"key": persian, "code": f"Digit{d}"}],
        }
    forms["?"] = {
        "latin": [{"key": "?", "code": "Slash", "shiftKey": True}],
        "arabic": [{"key": "؟", "code": "Slash", "shiftKey": True}],
    }
    forms["["] = {
        "latin": [{"key": "[", "code": "BracketLeft"}],
        "arabic": [{"key": "ج", "code": "BracketLeft"}],
    }
    forms["]"] = {
        "latin": [{"key": "]", "code": "BracketRight"}],
        "arabic": [{"key": "د", "code": "BracketRight"}],
    }
    forms["+"] = {
        "latin": [{"key": "+", "code": "Equal", "shiftKey": True}, {"key": "=", "code": "Equal"}],
        "arabic": [{"key": "+", "code": "NumpadAdd"}],
    }
    forms["-"] = {
        "latin": [{"key": "-", "code": "Minus"}],
        "arabic": [{"key": "-", "code": "NumpadSubtract"}],
    }
    return forms


@pytest.fixture(scope="module")
def node_out(tmp_path_factory) -> dict:
    if NODE is None:
        pytest.skip("node is not installed")
    folder = tmp_path_factory.mktemp("keymap")
    harness = folder / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    run = subprocess.run(
        [NODE, str(harness), str(JS), json.dumps(_event_forms(), ensure_ascii=False)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr[-4000:]
    return json.loads(run.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------- keys.js


def test_keys_module_reads_the_physical_key(node_out):
    assert node_out["letter"] == [
        "a",  # «ش» on the A key is A
        "a",
        "a",  # no code (a synthetic event): the Latin character
        None,  # no code and not Latin: nothing
        None,  # ⇧A is not A …
        "a",  # … unless the map asks for ⇧
        None,
        "z",  # ⌘Z when the map allows ⌘ / Ctrl
        "z",
        None,  # ⌥A types «å» on the Mac
        "a",
        None,  # a digit key is not a letter
        None,  # composing
        None,
    ]
    assert node_out["digit"] == [2, 2, 2, 1, None, 7, None, 9, None, None, None, None]
    assert node_out["is"] == [True, True, True, False, True, True, False, True, False, False]
    assert node_out["zoom"] == [True, True, True, False, True, True, True, True, False]
    assert node_out["printable"] == ["ش", "لا", "؟", "ً", "", "", "", "", "", "", "", "A"]


# ---------------------------------------------------------------- one meaning per physical key


def test_every_bare_key_has_one_meaning_across_screens(node_out):
    """A bare letter or digit key means one thing on every screen that uses it, in all its modes."""
    clashes = {}
    for key, screens in node_out["map"].items():
        meanings = {m for found in screens.values() for m in found}
        if len(meanings) > 1:
            clashes[key] = screens
    assert clashes == {}, f"a key with two meanings: {clashes}"


def test_the_arabic_layout_gives_the_meaning_of_the_latin_one(node_out):
    assert node_out["layouts"] == {}, node_out["layouts"]


def test_the_screens_follow_the_table(node_out):
    found = {
        key: {screen: m[0] for screen, m in screens.items() if m}
        for key, screens in node_out["map"].items()
        if any(screens.values())
    }
    assert found == TABLE


def test_a_composition_is_never_a_shortcut(node_out):
    assert node_out["composing"] == {"dashboard": [], "review": [], "manuscript": [], "book": []}


def test_review_word_mode_and_page_mode(node_out):
    """D69: with the word menu open letters and digits edit the word; closed, the letters are commands."""
    assert node_out["review"] == {
        "shinOpen": "type",  # a proofreader can start «شيء» with ش
        "shinClosed": "approve",
        "cmdEnterWord": "approve",  # ⌘↵ approves from anywhere …
        "cmdEnterInCorrection": "approve",  # … the word menu's correction field too
        "cmdEnterInLineEditor": None,  # the line editor keeps its own keys
        "digitBeyond": "type",  # a digit beyond the readings types it
        "digitReading": "choose2",
        "digitBeyondPage": None,
        "questionOpen": "type",  # «؟» and «-» are part of a correction
        "minusOpen": "type",
        "zeroOpen": "type",
        "spaceClosed": "openWord",
        "spaceOpen": None,
        "spaceUnfocused": None,
        "otherLetterClosed": None,  # no stray correction with the menu closed
        "home": "firstPage",
        "end": "lastPage",
        "pageDown": "nextPage",
        "shiftA": None,
        "altA": None,
        "tabOpen": "next",
        "enterOpen": "accept",
        "escOpen": "close",
        "mergeOpen": "mergeNext",
        "undo": "undo",
        "undoArabic": "undo",
        "gOpen": "type",
        "cOpen": "type",
        "altF": "fixEverywhere",
        "altFArabic": "fixEverywhere",
        "altFClosed": None,
        "altFGap": None,
    }


# ---------------------------------------------------------------- the shell


def _shell(messages=()) -> str:
    request = RequestFactory().get("/books/1/?view=grid")
    request.user = User.objects.get_or_create(username="editor")[0]
    return render_to_string("base.html", {"messages": list(messages)}, request=request)


@pytest.mark.django_db
def test_keys_js_is_loaded_after_ui_js_and_before_the_screens():
    body = _shell()
    scripts = re.findall(r'<script src="/static/src/js/([\w/]+\.js)"></script>', body)
    assert scripts[:3] == ["ui.js", "keys.js", "decode.js"]
    assert all(
        scripts.index("keys.js") < scripts.index(name) for name in ("books.js", "review.js", "manuscript.js")
    )


@pytest.mark.django_db
def test_the_toast_offers_undo_for_a_message_that_carries_it():
    """PHASE7_SPEC §3.6: «تراجع» is a small POST form back to the same place; it never submits by itself."""
    undo = Message(25, "استُثنيت الصفحة 3 من الكتاب.", extra_tags="undo:/books/1/pages/3/exclude/")
    plain = Message(25, "بدأ استخراج الصفحات.")
    body = _shell([undo, plain])
    forms = re.findall(
        r'<form method="post" action="([^"]*)" class="contents" data-toast-undo>(.*?)</form>', body, re.S
    )
    assert len(forms) == 1
    action, inner = forms[0]
    assert action == "/books/1/pages/3/exclude/"
    assert 'name="csrfmiddlewaretoken"' in inner
    assert '<input type="hidden" name="next" value="/books/1/?view=grid">' in inner
    assert '<button type="submit" class="btn">تراجع</button>' in inner
    assert "submit()" not in body  # nothing submits it but the click
    assert "استُثنيت الصفحة 3 من الكتاب." in body and "بدأ استخراج الصفحات." in body
    # an off-site URL is never offered
    assert "data-toast-undo" not in _shell([Message(25, "x", extra_tags="undo://evil.example/")])
