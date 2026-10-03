"""«الغلاف» on the book page (COVER_SPEC §4, D80): the first section of «التنسيق», the cover sheet before
page 1 and the filmstrip's first thumb.

- the template: the section first, with the spec's copy (the modes, the drop zone, «اختيار صورة…», the size
  note, the fits, the texts, the colours, the sizes), the cover sheet in the stage (no sheet x-show hides binds
  its style: a stylesheet change showed the cover beside the pages), the script tag
- the source CSS: the sheet, the thumb, the drop zone, the palette, the pickers, reduced motion
- `static/src/js/book/cover.js` under Node: the pure helpers (a colour normalised, the print pixels, the dpi
  of an image as fitted, what is refused before any request)
- the `bookPage` component under Node (the harness of core/test_layout_ui.py) against the contract fixtures of
  editor/fixtures/cover/: every stylesheet PUT body the section makes equals the fixture's request, in the
  fixture's order; the answers are the fixture's; the cover sheet and its turns (alone, not a page: the
  count, the cursor and the footprint unchanged), the thumb, the upload with progress and its refusals, the
  colours, the sizes, the address («#cover»), a proofreader."""

from __future__ import annotations

import json
import re
import subprocess
from html.parser import HTMLParser

import pytest

from core.test_layout_ui import (
    COMPONENT_HARNESS,
    NODE,
    ROOT,
    _between,
    _book,
    _component_fixture,
    _logged,
    _node_tmp,
    _page,
    _run_node,
    _user,
)

pytestmark = pytest.mark.django_db

CONTRACT = ROOT / "editor" / "fixtures" / "cover"
CSS_SRC = ROOT / "static" / "src" / "components" / "layout.css"
COVER_JS = ROOT / "static" / "src" / "js" / "book" / "cover.js"
LTR = "⁦"
PDI = "⁩"


@pytest.fixture
def editor(db):
    return _user("editor", "editor")


@pytest.fixture
def proofreader(db):
    return _user("reader", "proofreader")


def _contract() -> dict:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8")) for path in sorted(CONTRACT.glob("*.json"))
    }


def _label_body(label: str):
    """The body written in a request fixture's label («PUT … {front_matter: {cover: {mode: 'info'}}} → 200»)
    as data; None for a label whose body is not literal («<601 characters>»)."""
    match = re.search(r"(\{.*\}) → (\d{3})", label)
    if not match or "<" in match.group(1):
        return None
    text = re.sub(r"(\w+):", r'"\1":', match.group(1)).replace("'", '"')
    try:
        return json.loads(text), int(match.group(2))
    except ValueError:
        return None


def _normalised(value):
    """A fixture body as the section sends it: numbers as numbers (the fixture posts «'32'» to show the server
    takes it), colours in lower case (the pickers normalise them)."""
    if isinstance(value, dict):
        return {k: _normalised(v) for k, v in value.items()}
    if isinstance(value, str):
        if re.fullmatch(r"-?\d+(\.\d+)?", value):
            return float(value) if "." in value else int(value)
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
            return value.lower()
    return value


def _requests() -> list[dict]:
    """The contract's stylesheet requests with a literal body: `[{label, body, status, answer}]`."""
    out = []
    for label, answer in _contract()["stylesheet_requests.json"].items():
        if not label.startswith("PUT"):
            continue
        parsed = _label_body(label)
        if parsed:
            out.append({"label": label, "body": parsed[0], "status": parsed[1], "answer": answer})
    return out


# ---------------------------------------------------------------- the template


def test_cover_section_and_sheet_in_the_template(editor):
    body, config = _page(_logged(editor), _book())
    assert config["urls"]["cover"] and config["urls"]["bookImages"]
    assert "src/js/book/cover.js" in body
    side = _between(body, '<aside class="bk-side lo-side bp-side"', "</aside>")
    # the first accordion of «التنسيق», its head naming the mode
    assert re.findall(r'data-section="(\w+)"', side)[0] == "cover"
    section = _between(side, 'data-section="cover"', 'data-section="trim"')
    assert (
        '<span class="bp-acc-title">الغلاف</span><span class="lo-head-meta" x-text="coverModeLabel">'
        in section
    )
    assert 'x-show="sections.cover"' in section and "data-cover-section" in section
    # the mode as a segmented control (the labels are the payload's, cover.js's MODES until it comes)
    assert (
        'class="segmented lo-seg bp-cover-modes" role="group" aria-label="الغلاف" data-cover-modes' in section
    )
    assert 'x-for="m in coverModes"' in section and "setCoverMode(m.key)" in section
    for needle in (
        # the image: the drop zone is a button, the file input, the progress, the chosen image, the size note,
        # the fit
        "اسحب صورة الغلاف إلى هنا أو اخترها",
        '<span class="bp-drop-cta" x-show="coverUpload.state !== \'uploading\'">اختيار صورة…</span>',
        '<button type="button" class="bp-drop"',
        'accept="image/jpeg,image/png,image/webp"',
        '@drop.prevent="onCoverDrop($event)"',
        "bp-drop-progress",
        "تغيير الصورة…",
        ">إزالة الصورة</button>",
        'x-text="coverSizeNote"',
        'x-for="f in coverFits"',
        "setCoverFit(f.key)",
        # the texts
        ">نص الوسط</span>",
        ">نص الأسفل</span>",
        "setField('front_matter.cover.center', $el.value)",
        "setField('front_matter.cover.bottom', $el.value)",
        # the note of «من بيانات الكتاب», its link
        "يُؤخذ العنوان والعنوان الفرعي والمؤلف من <button",
        "revealSection('details')",
        "«بيانات الكتاب»</button>، والناشر والمدينة والسنة في الأسفل.",
        # the colours: the palette, the two pickers
        ">الألوان</span>",
        'class="bp-palette" role="radiogroup" aria-label="ألوان الغلاف"',
        "setCoverPreset(p.key)",
        ">لون الخلفية</span>",
        ">لون النص</span>",
        'type="color"',
        "setCoverColor('background', $el.value)",
        "setCoverColor('color', $el.value)",
        # the sizes
        'data-field="front_matter.cover.center_pt"',
        'data-field="front_matter.cover.bottom_pt"',
        "حجم نص الوسط",
        "حجم نص الأسفل",
    ):
        assert needle in section, needle
    # the errors under their keys
    for key in ("mode", "image", "fit", "center", "bottom", "background", "color", "center_pt"):
        assert f"errors['front_matter.cover.{key}']" in section, key
    # the cover sheet in the stage, before the right sheet, a button opening the section
    stage = _between(body, 'class="lo-sheet lo-sheet-cover"', 'data-sheet="right"')
    assert 'data-sheet="cover"' in stage and "x-show=\"phase === 'pages' && onCover\"" in stage
    assert 'class="lp-page lp-cover"' in stage and 'role="button" tabindex="0"' in stage
    assert '@click="onCoverSheet()"' in stage and '@keydown.enter.prevent="onCoverSheet()"' in stage
    assert 'x-text="coverHint" data-cover-hint' in stage and ':srcset="coverSrcset' in stage
    # the draft between a change and its render (the texts, the picture in its fit), the render fading in over
    # it, the thin bar while one is on its way
    assert ':style="coverDraftStyle" aria-hidden="true" data-cover-draft' in stage
    assert 'x-for="(p, i) in coverDraft.center"' in stage and 'x-for="(p, i) in coverDraft.bottom"' in stage
    assert ':data-fit="coverDraft.image.fit"' in stage and ":class=\"{ 'is-shown': coverFresh }\"" in stage
    assert 'x-show="coverBusy" aria-hidden="true" data-cover-busy' in stage
    assert 'x-show="phase === \'pages\' && !onCover" x-cloak data-sheet="right"' in body


class _Tags(HTMLParser):
    """Every start tag of a piece of HTML: `[(tag, {attr: value})]`."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_no_sheet_of_the_stage_binds_its_style_where_x_show_hides_it(editor):
    """The owner's report (2026-10-03): in a spread, a margin or the body size changed in «التنسيق» left the
    cover sheet beside the pages (page 3) until a reload. Cause: the cover figure carried both
    `x-show="… && onCover"` and `:style="coverSheetStyle"`; Alpine binds a string style by setting the whole
    `style` attribute, so every stylesheet change (coverSheetStyle reads the sheet) wiped the `display: none`
    x-show had set, and x-show, its value unchanged, never hid it again. The colours and the width sit on the
    cover's page inside the figure now, and no element of the stage that x-show toggles binds its style."""
    body, _config = _page(_logged(editor), _book())
    stage = _between(body, '<div class="lo-stage"', "</section>")
    parser = _Tags()
    parser.feed(stage)
    shown = [(tag, attrs) for tag, attrs in parser.tags if "x-show" in attrs]
    assert len(shown) >= 4  # the cover, the right and the left sheets, the cover's parts, the error state
    for tag, attrs in shown:
        assert not {":style", "x-bind:style"} & set(attrs), (tag, attrs)
    cover = next(attrs for tag, attrs in parser.tags if attrs.get("data-sheet") == "cover")
    assert cover["x-show"] == "phase === 'pages' && onCover" and ":style" not in cover
    page = next(attrs for tag, attrs in parser.tags if "lp-cover" in (attrs.get("class") or "").split())
    assert page[":style"] == "coverSheetStyle" and "x-show" not in page


def test_cover_section_reads_only_for_a_proofreader(proofreader):
    body, _config = _page(_logged(proofreader), _book())
    section = _between(body, 'data-section="cover"', 'data-section="trim"')
    assert re.search(r'<fieldset class="lo-section bp-cover" id="bp-sec-cover"[^>]*\sdisabled>', section)
    assert 'class="bp-acc-head"' in section and "disabled" not in _between(
        section, "bp-acc-head", "</button>"
    )


# ---------------------------------------------------------------- the CSS


def test_cover_css_rules_in_the_source():
    css = CSS_SRC.read_text(encoding="utf-8")
    for rule in (
        ".lp-page.lp-cover { display: grid; place-items: center; background: var(--cover-bg, #fff);",
        ".lp-cover:focus-visible { outline: 2px solid var(--color-accent); outline-offset: -3px; }",
        ".lp-cover-img { position: absolute; inset: 0; display: block; width: 100%; height: 100%;",
        ".lp-cover-img.is-shown { opacity: 1; }",
        ".lp-cover-draft { position: absolute; inset: 0; overflow: hidden; color: var(--cover-fg, #1b1b1b);",
        ".lp-cover-center { top: 50%; transform: translateY(-50%); }",
        ".lp-cover-bottom { bottom: var(--cd-bottom, 12%); }",
        ".lp-cover-title { font-weight: 700; font-size: calc(var(--cd-title, 28) * var(--u));",
        ".lp-cover-busy { position: absolute; inset: auto 0 0; height: 3px;",
        ".lo-thumb-cover .lo-thumb-img { background: var(--cover-bg, #fff); }",
        ".bp-cover-modes { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }",
        "border: 1.5px dashed var(--color-border-strong);",
        ".bp-drop:hover, .bp-drop.is-over { border-color: var(--color-accent);",
        ".bp-drop:focus-visible { outline: 2px solid var(--color-accent); outline-offset: 2px; }",
        ".bp-drop-progress > span {",
        '.bp-cover-image[data-fit="fill"] .bp-cover-thumb img { inset: 0; width: 100%; height: 100%;',
        "object-fit: cover; }",
        '.bp-cover-image[data-fit="width"] .bp-cover-thumb img {',
        '.bp-cover-image[data-fit="height"] .bp-cover-thumb img {',
        ".bp-palette { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr));",
        ".bp-swatch-cover { position: relative; display: block; width: 26px; aspect-ratio: 5 / 7;",
        ".bp-swatch-cover::before {",
        ".bp-swatch-cover::after {",
        '.bp-swatch[aria-checked="true"] .bp-swatch-cover {',
        ".bp-swatch:focus-visible { outline: 2px solid var(--color-accent);",
        '.bp-color-box input[type="color"] {',
        ".bp-color-box:focus-within { box-shadow: var(--ring); }",
        ".bp-cover-center textarea { text-align: center; }",
    ):
        assert rule in css, rule
    reduced = css[css.rindex("@media (prefers-reduced-motion: reduce)") :]
    assert (
        ".lp-cover-busy { animation: none; }" in reduced and ".lp-cover-img { transition: none; }" in reduced
    )
    assert ".bp-drop, .bp-drop-progress > span, .bp-swatch, .bp-swatch-cover, .bp-color-box {" in reduced


def test_cover_css_compiles(tmp_path):
    if NODE is None or not (ROOT / "node_modules" / "@tailwindcss" / "cli").is_dir():
        pytest.skip("node_modules not installed")
    out = tmp_path / "app.css"
    build = subprocess.run(
        ["npx", "tailwindcss", "-i", "static/src/app.css", "-o", str(out), "--minify"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert build.returncode == 0, build.stderr
    css = out.read_text(encoding="utf-8")
    for selector in (".lp-cover{", ".bp-drop{", ".bp-swatch-cover{", ".bp-cover-modes{", ".lo-thumb-cover"):
        assert selector in css, selector


# ---------------------------------------------------------------- cover.js's pure helpers under Node

HELPERS_HARNESS = r"""
import { readFileSync } from 'node:fs';
const [, , root] = process.argv;
globalThis.window = globalThis;
(0, eval)(readFileSync(`${root}/static/src/js/book/cover.js`, 'utf8'));
const C = globalThis.NassakhBook.cover;
const out = {
  hex: ['#1B2433', 'abc', '#ABC', ' #f4efe4 ', 'blue', '#12345', '', null].map((v) => C.normaliseHex(v)),
  pixels: [[170, 240], [148, 210], [210, 297], [120, 170], [0, 0]].map(([w, h]) => C.printPixels(w, h)),
  dpi: [
    C.effectiveDpi({ width: 1004, height: 1417 }, 170, 240, 'fill'),
    C.effectiveDpi({ width: 1200, height: 800 }, 170, 240, 'width'),
    C.effectiveDpi({ width: 1200, height: 800 }, 170, 240, 'height'),
    C.effectiveDpi({ width: 1200, height: 800 }, 170, 240, 'fill'),
    C.effectiveDpi({ width: 2400, height: 3400 }, 170, 240, 'fill'),
    C.effectiveDpi(null, 170, 240, 'fill'),
  ],
  problems: [
    C.imageProblem({ name: 'a.jpg', type: 'image/jpeg', size: 10 }),
    C.imageProblem({ name: 'a.webp', type: '', size: 10 }),
    C.imageProblem({ name: 'notes.txt', type: 'text/plain', size: 10 }),
    C.imageProblem({ name: 'scan.gif', type: 'image/gif', size: 10 }),
    C.imageProblem({ name: 'big.jpg', type: 'image/jpeg', size: 30 * 1024 * 1024 + 1 }),
    C.imageProblem({ name: 'big.png', type: 'image/png', size: 5 * 1024 * 1024 + 1 }, { types: ['image/png'], max_bytes: 5 * 1024 * 1024, max_mb: 5 }),
    C.imageProblem({ name: 'a.jpg', type: 'image/jpeg', size: 10 }, { types: ['image/png'] }),
    C.imageProblem(null),
  ],
  messages: [C.uploadMessage(413, null), C.uploadMessage(422, { detail: 'من الخادم' }), C.uploadMessage(0, null), C.uploadMessage(500, null), C.uploadMessage(403, null)],
  presets: [C.presetOf(C.PRESETS, '#1d2433', '#f3efe6'), C.presetOf(C.PRESETS, '#1d2433', '#ffffff'), C.PRESETS.map((p) => p.key)],
  modes: C.MODES.map((m) => [m.key, m.label]),
  fits: C.FITS.map((f) => [f.key, f.label]),
};
console.log(JSON.stringify(out));
"""  # noqa: E501


def test_cover_helpers_under_node(tmp_path):
    folder = _node_tmp(tmp_path)
    out = _run_node(folder, "helpers.mjs", HELPERS_HARNESS, str(ROOT))
    assert out["hex"] == ["#1b2433", "#aabbcc", "#aabbcc", "#f4efe4", "", "", "", ""]
    # = editor.services.print_pixels: round(mm / 25.4 × 300)
    assert out["pixels"] == [
        {"width": 2008, "height": 2835},
        {"width": 1748, "height": 2480},
        {"width": 2480, "height": 3508},
        {"width": 1417, "height": 2008},
        {"width": 0, "height": 0},
    ]
    # image 801 (1004 × 1417) on 17×24 is the contract's «150 dpi as fitted»; a landscape image by each fit
    assert out["dpi"] == [150, 179, 85, 85, 359, 150 * 0]
    contract = _contract()["upload.json"]
    wrong_type = next(v for k, v in contract.items() if "scan.gif" in k)["detail"]
    too_large = next(v for k, v in contract.items() if "31 MB" in k)["detail"]
    assert out["problems"] == [
        "",
        "",
        wrong_type,
        wrong_type,
        too_large,
        too_large.replace("30", "5"),
        wrong_type,
        wrong_type,
    ]
    assert out["messages"][0] == too_large and out["messages"][1] == "من الخادم"
    assert out["messages"][2].startswith("انقطع الاتصال") and out["messages"][3].startswith("تعذّر رفع")
    assert out["messages"][4] == "هذا الإجراء يتطلب صلاحية محرّر."
    assert out["presets"] == ["navy", "", ["white", "cream", "gray", "navy", "green", "burgundy"]]
    choices = next(v for k, v in _contract()["stylesheet.json"].items() if "the choices" in k)
    assert out["modes"] == [[m["value"], m["label"]] for m in choices["modes"]]
    assert out["fits"] == [[f["value"], f["label"]] for f in choices["fits"]]
    assert [dict(p) for p in choices["presets"]] == [
        {"key": k, "label": lb, "background": bg, "color": fg}
        for k, lb, bg, fg in (
            ("white", "أبيض", "#ffffff", "#1b1b1b"),
            ("cream", "كريمي", "#f4efe4", "#2a2419"),
            ("gray", "رمادي", "#e9e9ec", "#1f1f24"),
            ("navy", "كحلي", "#1d2433", "#f3efe6"),
            ("green", "أخضر داكن", "#1f3b2d", "#f1ead8"),
            ("burgundy", "عنابي", "#4a1d24", "#f4e9d8"),
        )
    ]


# ---------------------------------------------------------------- the component against the contract

COVER_SCENARIO = r"""
const contract = JSON.parse(readFileSync(process.argv[4], 'utf8'));
const pick = (obj, label) => Object.entries(obj).find(([k]) => k.includes(label))[1];
const SHEET = contract['stylesheet.json']; const API = contract['cover_api.json']; const UP = contract['upload.json'];
const REQS = contract.requests; // [{label, body, status, answer}] — the literal PUT bodies of stylesheet_requests.json
const sortKeys = (v) => (v && typeof v === 'object' && !Array.isArray(v) ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, sortKeys(v[k])])) : v);
const canon = (v) => JSON.stringify(sortKeys(v));
const out = {};
// the stylesheet payload of the contract's book 80 on the fixture's sheet: the settings, the limits, the choices
const choices = pick(SHEET, 'the choices');
const frontMatter = pick(SHEET, 'no cover key yet');
let stored = clone(frontMatter.cover);
let imageInfo = null;
const sheetPayload = () => ({ ...fixture.sheetPayload, saved: true, stylesheet: { ...fixture.sheetPayload.stylesheet, front_matter: { ...clone(frontMatter), cover: clone(stored) } }, limits: { ...fixture.sheetPayload.limits, ...pick(SHEET, 'limits (keys added)') }, cover: { ...clone(choices), image: imageInfo } });
let coverAnswer = pick(API, 'mode none');
const bodies = [];
const baseFetch = globalThis.fetch;
globalThis.fetch = async (url, init = {}) => {
  const method = init.method || 'GET';
  if (url === '/api/books/80/cover/') { calls.push([method, url, null]); return reply(200, clone(coverAnswer)); }
  if (url === '/api/books/1/stylesheet/' && method === 'PUT') {
    const body = JSON.parse(init.body);
    calls.push([method, url, body]);
    bodies.push(body);
    const cover = body.front_matter && body.front_matter.cover;
    if (cover && cover.mode === 'poster') return reply(400, pick(contract['stylesheet_requests.json'], 'nothing saved'));
    // the contract's answer for this body (the fixture posts «'32'» and upper-case colours: the same body)
    const same = (a, b) => canon(a) === canon(b);
    const loose = (v) => JSON.parse(JSON.stringify(v, (k, x) => (typeof x === 'string' && /^-?\d+(\.\d+)?$/.test(x) ? Number(x) : typeof x === 'string' && /^#[0-9A-Fa-f]{6}$/.test(x) ? x.toLowerCase() : x)));
    const found = REQS.find((r) => r.status === 200 && same(loose(r.body), loose(body)));
    if (found) { stored = clone(found.answer['stylesheet.front_matter.cover']); imageInfo = found.answer['cover.image']; }
    else if (cover) stored = { ...stored, ...cover };
    return reply(200, sheetPayload());
  }
  return baseFetch(url, init);
};
// the upload: XMLHttpRequest with progress, answered by hand
const xhrs = [];
globalThis.XMLHttpRequest = class { constructor() { this.headers = {}; this.upload = {}; this.status = 0; this.responseText = ''; xhrs.push(this); } open(m, u) { this.method = m; this.url = u; } setRequestHeader(k, v) { this.headers[k] = v; } send(form) { this.form = form; } respond(status, data) { this.status = status; this.responseText = JSON.stringify(data); this.onload(); } };
globalThis.FormData = class { constructor() { this.entries = []; } append(k, v, name) { this.entries.push([k, v, name === undefined ? null : name]); } };
const puts = () => calls.filter((c) => c[0] === 'PUT').map((c) => c[2]);
const coverGets = () => gets().filter((u) => u === '/api/books/80/cover/').length;
(async () => {
  const urls = { ...fixture.config.urls, cover: '/api/books/80/cover/', bookImages: '/api/books/80/images/' };
  // ---- a book without a cover key: «بلا غلاف»; nothing asked, the thumb there but hidden, no turn back from page 1
  calls.length = 0;
  const v = mk({ urls }, { stylesheet: sheetPayload() }); await settle();
  const coverThumb = (c) => (c || v)._dom.film.children.find((t) => t.dataset.cover !== undefined);
  const hidden = (c) => coverThumb(c).attrs.hidden !== undefined;
  v.showIndex(0, { instant: true }); // page 1: without a cover nothing is before it
  out.none = { hasCover: v.hasCover, mode: v.coverMode, label: v.coverModeLabel, thumbHidden: hidden(), thumbFirst: v._dom.film.children[0] === coverThumb(), thumbLabel: coverThumb().childNodes[1].textContent, canBack: v.canTurn(-1), current: v.current, coverGets: coverGets(),
    modes: v.coverModes.map((m) => [m.key, m.label]), fits: v.coverFits.map((f) => [f.key, f.label]), presets: v.coverPresets.map((p) => p.key), preset: v.coverPreset, sizeNote: v.coverSizeNote, values: v.coverValues, open: v.sections.cover, limit: v.limit('front_matter.cover.center_pt') };
  // ---- «من بيانات الكتاب»: the stage turns to the cover at once (its colours, a hint), the PUT is the contract's
  // (no re-layout: the cover is no page), api:cover follows the save
  calls.length = 0; replaced.length = 0;
  coverAnswer = pick(API, 'mode info');
  v.setCoverMode('info');
  const beforeTurn = { turning: v.turning, onCover: v.onCover, hasCover: v.hasCover, label: v.coverModeLabel };
  await fire(200); await settle();
  const onCover = { onCover: v.onCover, cursor: v.cursor, current: v.current, counter: v.counterText, numbers: v.shownNumbers, shown: v.shown, rightLines: v._dom.sheets.right.lines.innerHTML, address: replaced.slice(-1)[0], thumbCurrent: coverThumb().classList.contains('is-current'), thumbHidden: hidden(), hint: v.coverHint, img: v.coverImageUrl, style: v.coverSheetStyle, pageCount: v.pageCount, footprint: v.footprint.text, live: v.liveMessage, canBack: v.canTurn(-1), canNext: v.canTurn(1), pages: v.pages.length, focus: v.focusChapter,
    busy: v.coverBusy, fresh: v.coverFresh, draft: v.coverDraft, draftStyle: v.coverDraftStyle };
  await fire(400); await settle();
  out.info = { beforeTurn, onCover, puts: puts(), relayoutPosts: calls.filter((c) => c[0] === 'POST' && c[1].includes('relayout')).length, coverGets: coverGets(), img: v.coverImageUrl, srcset: v.coverSrcset, thumbSrc: coverThumb().childNodes[0].childNodes[0].getAttribute('src'), thumbBlank: coverThumb().classList.contains('is-blank'), pill: v.sheetSave.state, mode: v.coverValues.mode, hint: v.coverHint, state: v.coverState };
  // ---- the turns: forward lands page 1 (the cursor waited there), back returns to the cover, Home is page 1;
  // a spread keeps the cover alone on the right
  v.turn(1); await fire(200); await settle();
  const fwd = { onCover: v.onCover, current: v.current, counter: v.counterText, thumbCurrent: coverThumb().classList.contains('is-current'), address: replaced.slice(-1)[0], numbers: v.shownNumbers };
  v.turn(-1); await fire(200); await settle();
  const back = { onCover: v.onCover, counter: v.counterText, turnedFrom: v.turning };
  v.showIndex(0, { manual: true }); await fire(200); await settle();
  const home = { onCover: v.onCover, current: v.current };
  v.setSpread(true);
  v.showCover({ instant: true });
  const spread = { onCover: v.onCover, shown: v.shown, right: v.rightPage, left: v.leftPage, numbers: v.shownNumbers, canBack: v.canTurn(-1), canNext: v.canTurn(1), counter: v.counterText };
  v.turn(1); await fire(200); await settle();
  const spreadFwd = { onCover: v.onCover, current: v.current, counter: v.counterText, shown: v.shown };
  v.turn(-1); await fire(200); await settle();
  const spreadBack = { onCover: v.onCover, cursor: v.cursor };
  v.setSpread(false);
  v.showIndex(3, { instant: true });
  out.turns = { fwd, back, home, spread, spreadFwd, spreadBack, farCanBack: v.canTurn(-1), farNeighbour: v.neighbour(-1) };
  // ---- «صورة»: the empty drop zone, the size note; a text file and a file too large refused before any request;
  // the upload with its progress; the image id saves with the mode in one PUT; the render follows
  drop(); calls.length = 0;
  v.setCoverMode('image');
  const turnedBack = { onCover: v.onCover, turning: v.turning };
  await fire(200); await settle();
  const empty = { onCover: v.onCover, info: v.coverImageInfo, hint: v.coverHint, note: v.coverSizeNote, upload: v.coverUpload, style: v.coverSheetStyle };
  await v.uploadCoverImage({ name: 'notes.txt', type: 'text/plain', size: 100 });
  const badType = { error: v.coverUpload.error, xhrs: xhrs.length };
  await v.uploadCoverImage({ name: 'big.jpg', type: 'image/jpeg', size: 31 * 1024 * 1024 });
  const tooBig = { error: v.coverUpload.error, xhrs: xhrs.length };
  const p = v.uploadCoverImage({ name: 'غلاف.jpg', type: 'image/jpeg', size: 240000 });
  const xhr = xhrs[xhrs.length - 1];
  xhr.upload.onprogress({ lengthComputable: true, loaded: 120000, total: 240000 });
  const mid = { state: v.coverUpload.state, percent: v.coverUpload.percent, text: v.coverUploadText, url: xhr.url, method: xhr.method, csrf: xhr.headers['X-CSRFToken'], accept: xhr.headers.Accept, form: xhr.form.entries.map((e) => [e[0], e[1] && e[1].name ? e[1].name : e[1], e[2]]), again: await v.uploadCoverImage({ name: 'b.jpg', type: 'image/jpeg', size: 10 }) };
  xhr.respond(201, pick(UP, '→ 201 (turned upright)'));
  await p; await settle();
  const uploaded = { upload: v.coverUpload, image: v.coverImage, info: v.coverImageInfo, note: v.coverImageNote, live: v.liveMessage, hint: v.coverHint, waiting: pending(400).length, draft: v.coverDraft };
  coverAnswer = pick(API, 'image 801, fit fill');
  await fire(400); await settle();
  out.image = { turnedBack, empty, badType, tooBig, mid, uploaded, puts: puts(), img: v.coverImageUrl, infoAfter: v.coverImageInfo, payloadImage: v.coverChoices.image && v.coverChoices.image.id, coverGets: coverGets() };
  // a 413 from the server: its words, the image chosen stays
  const p2 = v.uploadCoverImage({ name: 'huge.jpg', type: 'image/jpeg', size: 1000 });
  xhrs[xhrs.length - 1].respond(413, pick(UP, '→ 413'));
  await p2; await settle();
  out.refused = { error: v.coverUpload.error, state: v.coverUpload.state, image: v.coverImageInfo && v.coverImageInfo.id };
  // ---- the fit: one PUT of the fit alone; the note follows the fit
  calls.length = 0;
  v.setCoverFit('width'); await fire(400); await settle();
  out.fit = { puts: puts(), fit: v.coverValues.fit, note: v.coverImageNote, bad: v.setCoverFit('stretch') };
  // ---- «نص مخصّص»: the texts type letter by letter (one PUT after the long quiet), lines kept as the server keeps them
  calls.length = 0;
  v.setCoverMode('text');
  v.setField('front_matter.cover.center', '  كتاب   الأمالي \r\n\r\nلأبي علي القالي\n\n');
  v.setField('front_matter.cover.bottom', 'طرابلس ٢٠٢٦');
  const textWait = { at400: pending(400).length, at900: pending(900).length };
  await fire(900); await settle();
  out.text = { textWait, puts: puts(), center: v.coverValues.center, bottom: v.coverValues.bottom, sizes: v.coverMode === 'text' };
  // ---- the colours: a preset alone on the wire, both colours at once here; a hand-picked colour → custom; the
  // two colours of a preset → that preset; a bad colour refused here with its message, nothing sent
  calls.length = 0;
  v.setCoverPreset('navy');
  const navyNow = { bg: v.coverValues.background, fg: v.coverValues.color, checked: v.coverPreset, style: v.coverSheetStyle, dirty: Object.keys(v.dirty) };
  await fire(400); await settle();
  const navySaved = { puts: puts(), stored: v.coverValues.preset, checked: v.coverPreset };
  calls.length = 0;
  v.setCoverColor('background', '#123456'); v.setField('front_matter.cover.center_pt', '32'); v.setField('front_matter.cover.bottom_pt', 12.5); v.setField('front_matter.cover.bottom_mm', 40);
  const customNow = { preset: v.coverValues.preset, checked: v.coverPreset, centerPt: v.coverValues.center_pt, bg: v.coverValues.background };
  await fire(400); await settle();
  const customSaved = { puts: puts(), stored: v.coverValues.preset, bg: v.coverValues.background, bottomMm: v.coverValues.bottom_mm };
  calls.length = 0;
  v.setCoverColor('background', '#F4EFE4'); v.setCoverColor('color', '2A2419'); v.setField('front_matter.cover.bottom_mm', null);
  const creamNow = { preset: v.coverValues.preset, checked: v.coverPreset };
  await fire(400); await settle();
  const creamSaved = { puts: puts(), stored: v.coverValues.preset, bottomMm: v.coverValues.bottom_mm };
  calls.length = 0;
  const bad = v.setCoverColor('color', 'blue');
  const badColour = { returned: bad, error: v.errors['front_matter.cover.color'], pending: pending(400).length, dirty: Object.keys(v.dirty), preview: v.previewCoverColor('color', '#ABCDEF'), previewed: v.coverValues.color, stillDirty: Object.keys(v.dirty), badPreset: v.setCoverPreset('pink') };
  v.setLocal('front_matter.cover.color', '#2a2419');
  drop();
  out.colours = { navyNow, navySaved, customNow, customSaved, creamNow, creamSaved, badColour };
  // ---- the sizes: the stepper's step and the limits; a value clears its own field's error
  v.errors = { 'front_matter.cover.center_pt': 'الحجم بين 8 و96 نقطة.' };
  v.setField('front_matter.cover.center_pt', 32);
  v.step('front_matter.cover.center_pt', 1);
  const stepped = v.coverValues.center_pt;
  v.setField('front_matter.cover.center_pt', 500);
  const clamped = v.coverValues.center_pt;
  v.setField('front_matter.cover.bottom_pt', 3);
  out.sizes = { stepped, clamped, bottomClamped: v.coverValues.bottom_pt, limit: v.limit('front_matter.cover.center_pt'), errorsCleared: Object.keys(v.errors) };
  drop(); v.dirty = {};
  // ---- the image removed (its file kept) with the mode in one PUT; then «بلا غلاف»: the stage leaves the cover
  // at once, the thumb hides, the settings stay for when it is turned on again
  calls.length = 0;
  v.setCoverMode('image'); v.removeCoverImage();
  await fire(400); await settle();
  const removed = { puts: puts(), info: v.coverImageInfo, hint: v.coverHint, upload: v.coverUpload };
  calls.length = 0;
  v.showCover({ instant: true });
  const wasOn = v.onCover;
  v.setCoverMode('none'); await settle();
  const offNow = { wasOn, onCover: v.onCover, current: v.current, hasCover: v.hasCover, thumbHidden: hidden(), canBack: v.canTurn(-1), label: v.coverModeLabel };
  coverAnswer = pick(API, 'mode none');
  await fire(400); await settle();
  out.off = { removed, offNow, puts: puts(), kept: [v.coverValues.fit, v.coverValues.center], coverGets: coverGets(), img: v.coverImageUrl };
  // ---- every PUT the section made, in the contract's order
  out.bodies = bodies.slice();
  // ---- a refused body: the errors under their keys, the pill
  calls.length = 0;
  v.setField('front_matter.cover.mode', 'poster');
  await fire(400); await settle();
  out.invalid = { keys: Object.keys(v.errors), mode: v.errors['front_matter.cover.mode'], colour: v.errors['front_matter.cover.background'], pill: v.sheetSave, shown: v.coverMode };
  v.errors = {}; v.dirty = {}; drop();
  // ---- the sheet or the thumb opens the section; «بيانات الكتاب» from the note
  v.setTab('pages'); v.sections = { ...v.sections, cover: false, details: false };
  v.openCoverSection();
  const opened = { tab: v.tab, cover: v.sections.cover, stored: local['nassakh.book.sections'] };
  v.revealSection('details');
  out.section = { opened, details: v.sections.details };
  // ---- «#cover» (and the session) restore the cover on a book that has one; the thumb's click opens it and its section
  drop(); calls.length = 0;
  globalThis.location.hash = '#cover';
  coverAnswer = pick(API, 'mode text');
  stored = { ...clone(frontMatter.cover), mode: 'text', center: 'كتاب' };
  const w = mk({ urls }, { stylesheet: sheetPayload() }); await settle();
  out.restored = { onCover: w.onCover, counter: w.counterText, current: w.current, cursor: w.cursor, session: session['nassakh.book.page.1'], img: w.coverImageUrl, coverGets: coverGets(), thumbSrc: coverThumb(w).childNodes[0].childNodes[0].getAttribute('src'), thumbHidden: hidden(w) };
  globalThis.location.hash = '';
  w.turn(1); await fire(200); await settle();
  w.setTab('chapters');
  w._dom.film.listeners.click({ target: coverThumb(w) });
  await fire(200); await settle();
  out.thumbClick = { onCover: w.onCover, tab: w.tab, section: w.sections.cover, counter: w.counterText };
  // a #cover link opened in this tab
  w.showIndex(2, { instant: true });
  globalThis.location.hash = '#cover';
  w.onHashChange(); await fire(200); await settle();
  out.hashChange = { onCover: w.onCover };
  globalThis.location.hash = '';
  // ---- a proofreader: the section reads (the cover shows), nothing saves or uploads
  drop(); calls.length = 0;
  const r = mk({ urls, canEdit: false }, { stylesheet: sheetPayload() }); await settle();
  const before = xhrs.length;
  const refusedUpload = await r.uploadCoverImage({ name: 'a.jpg', type: 'image/jpeg', size: 10 });
  r.setCoverMode('info'); await fire(400); await settle();
  // the requested chapter wins over the session's «cover» (as it does over a page); the cover still shows on a turn
  out.reader = { hasCover: r.hasCover, onCover: r.onCover, shows: r.showCover({ instant: true }) && r.onCover, upload: refusedUpload, xhrs: xhrs.length - before, puts: puts().length, mode: r.coverMode, pick: r.pickCoverImage(), remove: r.removeCoverImage() };
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


def test_cover_component_under_node_against_the_contract(tmp_path):
    folder = _node_tmp(tmp_path)
    fixture = _component_fixture()
    (folder / "fixture.json").write_text(json.dumps(fixture, ensure_ascii=False))
    contract = _contract()
    requests = _requests()
    contract["requests"] = requests
    (folder / "contract.json").write_text(json.dumps(contract, ensure_ascii=False))
    base = COMPONENT_HARNESS.split("\nconst out = {};\n")[0]
    out = _run_node(
        folder,
        "cover.mjs",
        base + COVER_SCENARIO,
        str(ROOT),
        str(folder / "fixture.json"),
        str(folder / "contract.json"),
    )
    choices = next(v for k, v in contract["stylesheet.json"].items() if "the choices" in k)
    api = contract["cover_api.json"]
    info_render = next(v for k, v in api.items() if "mode info" in k)
    image_render = next(v for k, v in api.items() if "image 801, fit fill" in k)
    text_render = next(v for k, v in api.items() if "mode text" in k)
    upload = next(v for k, v in contract["upload.json"].items() if "→ 201 (turned upright)" in k)

    # --- every stylesheet PUT the section made equals the contract's request, in the contract's order (the
    # fixture posts «'32'» and upper-case colours to show the server takes them; the section sends numbers and
    # lower-case colours)
    expected = [_normalised(r["body"]) for r in requests if r["status"] == 200]
    assert len(expected) == 9
    assert out["bodies"] == expected
    assert out["bodies"][0] == {"front_matter": {"cover": {"mode": "info"}}}
    assert out["bodies"][1] == {"front_matter": {"cover": {"mode": "image", "image": 801}}}
    assert out["bodies"][4] == {"front_matter": {"cover": {"preset": "navy"}}}
    assert out["bodies"][8] == {"front_matter": {"cover": {"mode": "none"}}}

    # --- a book without a cover key: «بلا غلاف», nothing asked of the server, the thumb first but hidden,
    # no turn back from page 1; the choices are the payload's, the note the trim's pixels
    none = out["none"]
    assert none["hasCover"] is False and none["mode"] == "none" and none["label"] == "بلا غلاف"
    assert none["thumbHidden"] is True and none["thumbFirst"] is True and none["thumbLabel"] == "الغلاف"
    assert none["canBack"] is False and none["current"] == 1 and none["coverGets"] == 0
    assert none["modes"] == [[m["value"], m["label"]] for m in choices["modes"]]
    assert none["fits"] == [[f["value"], f["label"]] for f in choices["fits"]]
    assert none["presets"] == [p["key"] for p in choices["presets"]] and none["preset"] == "white"
    assert none["sizeNote"] == f"للطباعة: 300 نقطة في البوصة، أي {LTR}2008 × 2835{PDI} بكسل على هذا القطع"
    assert none["sizeNote"].replace(LTR, "").replace(PDI, "") == choices["print_size"]["note"]
    assert none["values"]["bottom_mm"] == choices["bottom_mm_auto"] and none["values"]["preset"] == "white"
    assert none["open"] is True and none["limit"] == [8, 96]

    # --- «من بيانات الكتاب»: the stage turns to the cover at once (before any save), with its colours and a
    # hint; the sequence, the count, the footprint and the cursor stay; one PUT of the mode alone, no
    # re-layout; api:cover fetched once after the save, its render on the sheet and the thumb
    info = out["info"]
    assert info["beforeTurn"] == {
        "turning": "out-prev",
        "onCover": False,
        "hasCover": True,
        "label": "من بيانات الكتاب",
    }
    on = info["onCover"]
    assert on["onCover"] is True and on["cursor"] == 0 and on["current"] == 0 and on["counter"] == "الغلاف"
    assert on["numbers"] == {"right": 0, "left": 0} and on["shown"] == {"right": -1, "left": -1}
    assert on["rightLines"] == "" and on["address"].endswith("#cover")
    assert on["thumbCurrent"] is True and on["thumbHidden"] is False
    # no «يُحضَّر» hint on a blank sheet: the draft draws the cover (the book's details, in the render's places:
    # the title, the subtitle and the author in the middle, the imprint at the foot), the thin bar says the
    # render is on its way
    assert on["hint"] == "" and on["img"] == "" and on["busy"] is True and on["fresh"] is False
    assert on["draft"] == {
        "center": [
            {"kind": "title", "text": "الأمالي"},
            {"kind": "sub", "text": "مجالس في الأدب"},
            {"kind": "author", "text": "أبو علي القالي"},
        ],
        "bottom": [{"kind": "foot", "text": "دار المدار، طرابلس، 2026"}],
        "image": None,
    }
    assert on["style"] == "--cover-bg: #ffffff; --cover-fg: #1b1b1b; --pw: 481.89"
    assert (
        on["draftStyle"]
        == "--cd-side: 12.941%; --cd-bottom: 12.500%; --cd-title: 28; --cd-sub: 15.40; --cd-foot: 13"
    )
    assert (
        on["pageCount"] == 5 and on["footprint"] == "5 صفحات" and on["pages"] == 5 and on["live"] == "الغلاف"
    )
    assert on["canBack"] is False and on["canNext"] is True and on["focus"] == "h10"
    assert info["puts"] == [{"front_matter": {"cover": {"mode": "info"}}}] and info["relayoutPosts"] == 0
    assert (
        info["coverGets"] == 1
        and info["img"] == info_render["image_1x"]
        and info["thumbSrc"] == info_render["image_1x"]
    )
    assert info["srcset"] == f"{info_render['image_1x']} 1x, {info_render['image_2x']} 2x"
    assert (
        info["thumbBlank"] is False
        and info["pill"] == "saved"
        and info["mode"] == "info"
        and info["state"] == ""
    )

    # --- the turns: forward lands page 1 (the cursor waited there), back returns to the cover, Home is
    # page 1; a spread keeps the cover alone on the right; from page 4 no turn reaches it in one step
    turns = out["turns"]
    assert (
        turns["fwd"]["onCover"] is False
        and turns["fwd"]["current"] == 1
        and turns["fwd"]["counter"] == "صفحة 1 من 5"
    )
    assert turns["fwd"]["thumbCurrent"] is False and turns["fwd"]["address"].endswith("#page-1")
    assert turns["fwd"]["numbers"] == {"right": 1, "left": 0}
    assert turns["back"] == {"onCover": True, "counter": "الغلاف", "turnedFrom": ""}
    assert turns["home"] == {"onCover": False, "current": 1}
    spread = turns["spread"]
    assert spread["onCover"] is True and spread["shown"] == {"right": -1, "left": -1}
    assert spread["right"] is None and spread["left"] is None and spread["numbers"] == {"right": 0, "left": 0}
    assert spread["canBack"] is False and spread["canNext"] is True and spread["counter"] == "الغلاف"
    assert turns["spreadFwd"]["onCover"] is False and turns["spreadFwd"]["current"] == 1
    assert turns["spreadFwd"]["shown"] == {"right": -1, "left": 0}  # page 1 alone on the left of its spread
    assert turns["spreadBack"] == {"onCover": True, "cursor": 0}
    assert turns["farCanBack"] is True and turns["farNeighbour"] == 2

    # --- «صورة»: the stage comes back to the cover (a cover change shows the cover); the empty state (no
    # image, its hint, the size note); a text file and a file too large refused with api:book_images's words
    # before any request; the upload posts multipart {file, purpose: cover} with the CSRF token and shows its
    # progress; a second upload meanwhile is refused; the answer's image shows at once with its size and dpi,
    # and its id saves with the mode in one PUT (the contract's); the render follows
    image = out["image"]
    assert image["turnedBack"] == {"onCover": False, "turning": "out-prev"}
    empty = image["empty"]
    assert empty["onCover"] is True and empty["info"] is None and empty["hint"] == "لم تُرفع صورة الغلاف بعد"
    assert empty["note"] == none["sizeNote"] and empty["upload"] == {"state": "", "percent": 0, "error": ""}
    wrong_type = next(v for k, v in contract["upload.json"].items() if "scan.gif" in k)["detail"]
    too_large = next(v for k, v in contract["upload.json"].items() if "31 MB" in k)["detail"]
    assert image["badType"] == {"error": wrong_type, "xhrs": 0}
    assert image["tooBig"] == {"error": too_large, "xhrs": 0}
    mid = image["mid"]
    assert mid["state"] == "uploading" and mid["percent"] == 50 and mid["text"] == "يُرفع… 50 %"
    assert mid["url"] == "/api/books/80/images/" and mid["method"] == "POST" and mid["csrf"] == "tok"
    assert mid["accept"] == "application/json" and mid["again"] is False
    assert mid["form"] == [["file", "غلاف.jpg", "غلاف.jpg"], ["purpose", "cover", None]]
    up = image["uploaded"]
    assert up["upload"] == {"state": "", "percent": 0, "error": ""} and up["live"] == "رُفعت صورة الغلاف"
    assert up["image"] == {
        "id": 801,
        "url": upload["url"],
        "thumb_url": upload["thumb_url"],
        "width": 1004,
        "height": 1417,
        "format": "jpeg",
        "name": "غلاف.jpg",
    }
    assert up["info"] == up["image"] and up["hint"] == "" and up["waiting"] == 1
    # the draft shows the picture uploaded (its thumb) in its fit at once, before the save and the render
    assert up["draft"] == {"center": [], "bottom": [], "image": {"src": upload["thumb_url"], "fit": "fill"}}
    assert up["note"] == f"الصورة {LTR}1004 × 1417{PDI} بكسل · نحو 150 نقطة في البوصة على هذا القطع"
    assert image["puts"] == [{"front_matter": {"cover": {"mode": "image", "image": 801}}}]
    assert image["img"] == image_render["image_1x"] and image["coverGets"] == 1
    assert image["infoAfter"] == up["image"] and image["payloadImage"] == 801
    # a 413 from the server: its words; the image chosen stays
    assert out["refused"] == {"error": too_large, "state": "", "image": 801}
    # the fit alone on the wire; the note follows the fit (a portrait image's width on 17×24: 150 dpi too)
    fit = out["fit"]
    assert fit["puts"] == [{"front_matter": {"cover": {"fit": "width"}}}] and fit["fit"] == "width"
    assert fit["note"].endswith("نحو 150 نقطة في البوصة على هذا القطع") and fit["bad"] is False

    # --- «نص مخصّص»: the texts wait the long quiet (one PUT with the mode), the server's lines come back
    text = out["text"]
    assert text["textWait"] == {"at400": 0, "at900": 1}
    assert text["puts"] == [
        {
            "front_matter": {
                "cover": {
                    "mode": "text",
                    "center": "  كتاب   الأمالي \r\n\r\nلأبي علي القالي\n\n",
                    "bottom": "طرابلس ٢٠٢٦",
                }
            }
        }
    ]
    assert text["center"] == "كتاب الأمالي\n\nلأبي علي القالي" and text["bottom"] == "طرابلس ٢٠٢٦"
    assert text["sizes"] is True

    # --- the colours: a preset alone on the wire, both colours at once on the sheet; a hand-picked colour is
    # `custom` here and there; the two colours of a preset (typed in any case, with or without «#») are that
    # preset; a bad colour is refused here with the server's words and sends nothing; the well's preview
    # changes nothing on the wire
    colours = out["colours"]
    assert colours["navyNow"] == {
        "bg": "#1d2433",
        "fg": "#f3efe6",
        "checked": "navy",
        "style": "--cover-bg: #1d2433; --cover-fg: #f3efe6; --pw: 481.89",
        "dirty": ["front_matter.cover.preset"],
    }
    assert colours["navySaved"] == {
        "puts": [{"front_matter": {"cover": {"preset": "navy"}}}],
        "stored": "navy",
        "checked": "navy",
    }
    assert colours["customNow"] == {"preset": "custom", "checked": "", "centerPt": 32, "bg": "#123456"}
    assert colours["customSaved"]["puts"] == [
        {
            "front_matter": {
                "cover": {"background": "#123456", "center_pt": 32, "bottom_pt": 12.5, "bottom_mm": 40}
            }
        }
    ]
    assert colours["customSaved"]["stored"] == "custom" and colours["customSaved"]["bottomMm"] == 40
    assert colours["creamNow"] == {"preset": "cream", "checked": "cream"}
    assert colours["creamSaved"]["puts"] == [
        {"front_matter": {"cover": {"background": "#f4efe4", "color": "#2a2419", "bottom_mm": None}}}
    ]
    assert colours["creamSaved"]["stored"] == "cream" and colours["creamSaved"]["bottomMm"] == 30
    bad = colours["badColour"]
    assert bad["returned"] is False and bad["error"] == "اللون غير صالح؛ يُكتب مثل #1d2433."
    assert bad["pending"] == 0 and bad["dirty"] == [] and bad["preview"] is True
    assert bad["previewed"] == "#abcdef" and bad["stillDirty"] == [] and bad["badPreset"] is False

    # --- the sizes: ↑ steps one point, the limits clamp (the payload's), a value clears its field's error
    assert out["sizes"] == {
        "stepped": 33,
        "clamped": 96,
        "bottomClamped": 8,
        "limit": [8, 96],
        "errorsCleared": [],
    }

    # --- the image removed with the mode in one PUT (the file kept, the drop zone back); «بلا غلاف» leaves
    # the cover at once, hides the thumb, saves the mode alone and keeps the other settings
    off = out["off"]
    assert off["removed"]["puts"] == [{"front_matter": {"cover": {"mode": "image", "image": None}}}]
    assert off["removed"]["info"] is None and off["removed"]["hint"] == "لم تُرفع صورة الغلاف بعد"
    assert off["offNow"] == {
        "wasOn": True,
        "onCover": False,
        "current": 1,
        "hasCover": False,
        "thumbHidden": True,
        "canBack": False,
        "label": "بلا غلاف",
    }
    assert off["puts"] == [{"front_matter": {"cover": {"mode": "none"}}}]
    assert off["kept"] == ["width", "كتاب الأمالي\n\nلأبي علي القالي"] and off["img"] == ""

    # --- a refused body: every error under its key, the pill says so; the unknown mode shows as none
    invalid = out["invalid"]
    errors = next(v for k, v in contract["stylesheet_requests.json"].items() if "nothing saved" in k)[
        "errors"
    ]
    assert invalid["keys"] == list(errors) and invalid["mode"] == errors["front_matter.cover.mode"]
    assert invalid["colour"] == errors["front_matter.cover.background"]
    assert (
        invalid["pill"] == {"state": "invalid", "message": "نوع الغلاف غير معروف."}
        and invalid["shown"] == "none"
    )

    # --- the sheet or the thumb opens «التنسيق» on the cover section (remembered open); the note's link opens
    # «بيانات الكتاب»
    assert out["section"]["opened"] == {"tab": "format", "cover": True, "stored": "cover"} or (
        out["section"]["opened"]["tab"] == "format" and out["section"]["opened"]["cover"] is True
    )
    assert out["section"]["details"] is True

    # --- «#cover» restores the cover on a book that has one (the render fetched once); the thumb's click
    # opens the cover and its section; a #cover link opened in this tab
    restored = out["restored"]
    assert restored["onCover"] is True and restored["counter"] == "الغلاف" and restored["current"] == 0
    assert restored["cursor"] == 0 and restored["session"] == "cover"
    assert restored["img"] == text_render["image_1x"] and restored["coverGets"] == 1
    assert restored["thumbSrc"] == text_render["image_1x"] and restored["thumbHidden"] is False
    assert out["thumbClick"] == {"onCover": True, "tab": "format", "section": True, "counter": "الغلاف"}
    assert out["hashChange"] == {"onCover": True}

    # --- a proofreader: the cover shows (the requested chapter first, as for a page), nothing saves, uploads
    # or opens the picker
    assert out["reader"] == {
        "hasCover": True,
        "onCover": False,
        "shows": True,
        "upload": False,
        "xhrs": 0,
        "puts": 0,
        "mode": "info",
        "pick": False,
        "remove": False,
    }
