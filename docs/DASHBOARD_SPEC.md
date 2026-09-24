# Dashboard specification — the book page as a mirror of its scans

Single source of truth for rebuilding `/books/<id>/` (`templates/books/detail.html`). It refines
`docs/PHASE3_SPEC.md` §1–§3; where the two disagree, this file wins. Read first: `DESIGN.md`,
`docs/PHASE3_SPEC.md` (§1 motion policy, §2 decode effect, §3 views, §5 payloads), `docs/DECISIONS.md`
(D22–D27), and the approved visual reference: `templates/review/review.html`,
`static/src/components/review.css`, `static/src/js/review.js`.

Owner feedback that drives this spec (after testing Phase 3 in the browser):

- The review screen — its theming, style and animation — is what the owner likes. The dashboard is not.
- The generating text must be a centred flex column with `space-between`, matching its page; the font size
  must come from the page's lines; text justified.
- Far fewer noise glyphs; the text must visibly return to Tesseract's words and drift away again.
- Muted gray with an opacity fade; highlighting through a shimmer.
- Make the page perfect during processing and easy to use.

Standing rules: Arabic RTL UI, Western digits, no reading app (viewing verifies the machine's work), no
time-remaining estimates, no persona copy, one primary button per view, DESIGN.md tokens and hairline calm
for the chrome, PHASE3_SPEC §1 motion exception for content being processed, smooth at 800 pages.

## 0. Decision to record before building — D28 (amends D23)

D23 says provisional words "never settle fully on the Tesseract text (at most ~60 % real letters)". The owner
now asks for far fewer glyphs and a visible return to Tesseract's exact words. **D28:** a provisional word may
show 100 % of Tesseract's letters; it is veiled briefly and returns. "Never mistaken for the result" is carried
by colour (`--color-text-3`), the opacity fade, the reading shimmer, the sheet badge «نص مبدئي», and the
absence of the «مراجعة» action on provisional sheets. No ASCII symbols anywhere; noise letters are Arabic
letters of the same skeleton family. `core/test_theatre.py` assertions `capOk` and `settled == 0` are replaced
(§15). The integrator records D28 in `docs/DECISIONS.md` (this spec must not edit it).

## 1. Goals

1. **Mirror.** Each sheet shows the scan and a text pane of exactly the same box; every text line sits where
   its printed line is (block from the detected geometry, lines distributed inside it), sized from the
   page's line height, justified. Hovering a line on either side lights the same line on the other.
2. **Calm generation.** Gray provisional text that breathes in opacity, returns to Tesseract's words as a
   reading cursor passes over it, drifts by one to three same-shape letters afterwards; one shimmer per
   sheet, synchronised with the lit line box on the scan; a right-to-left wave lands the final text.
3. **Review look.** Toolbars, bands, pills, marks, skeletons and easing are the review screen's, one to one.
4. **Navigable at 800 pages.** Sticky toolbar with view toggle, jump, filters; attention next to the page;
   no reload at the end of processing; the poll and the shells stay cheap.

## 2. Layout

Top to bottom, inside the normal page shell (`base.html` topbar 52 px + `.main` with page padding 32/16).

### 2.1 Top bar (`block title` + `block header_actions`)

`block title` = the book title (truncate). `block header_actions` = `<div class="bk-bar">` modelled on
`.rv-bar-inner` (flex, gap 10, min-width 0), in this order:

1. `<a class="btn btn-ghost btn-sm">` «كل الكتب» (existing markup and icon).
2. **Status chip** `.bk-status` = the `.rv-save` pill (24 px, `--radius-pill`, 12 px, `--color-text-3`,
   6 px dot): dot `dot-accent` pulsing `bk-pulse` 1.2 s while `active`, text
   «قيد المعالجة · <bdi>X</bdi> من <bdi>Y</bdi> صفحة» (X = pages `ocr_done|reviewed|assembled`);
   when not active the book's `status_label` with its `dot`. Fixed `min-width: 14ch`, `tabular-nums`.
3. **Progress** `.bk-progress`: the `.rv-progress-bar` (96 px, 4 px, fill 320 ms
   `cubic-bezier(.2,.7,.2,1)`), `is-success` at 100 %, `is-danger` on book error, `is-warning` on
   `needs_guides`. `aria-label="نسبة إتمام المعالجة"`. Hidden below 1180 px.
4. **Poll health** `.bk-poll` = `.rv-save`-style pill, hidden while fine; `is-error`
   «تعذّر التحديث · إعادة المحاولة» (click = poll now); after 401/403 «انتهت الجلسة · تسجيل الدخول»
   (a link to the login page). This replaces the `fetchFailed` banner.
5. **The one primary button** (36 px, `--color-text` fill), chosen client-side from the poll (no reload):
   - `can_start` and role admin/editor → «بدء المعالجة» (`error` → «إعادة بدء المعالجة»), the existing form;
   - `status == needs_guides` and role admin/editor → «ضبط الأدلة» (link);
   - `review.next_review_url` → «الصفحة التالية للمراجعة» (`data-review-next`);
   - every non-excluded page reviewed → «نسخ نص الكتاب» (the copy button, primary);
   - otherwise no primary (the book is processing and nothing is reviewable).
   All candidates are rendered server-side (role-gated) and toggled with `x-show`, so no reload is needed.
6. **«⋯» menu** (`btn-icon`, `aria-haspopup="menu"`, popover 240 px, `--shadow-pop`): «نسخ نص الكتاب»
   (with the existing `title` hints, disabled while copying), separator, «ضبط الأدلة» (admin/editor),
   separator, label «إعادة التشغيل من مرحلة» with the five rerun forms (admin/editor), separator,
   «كل الكتب». An item that is currently the primary is hidden from the menu.

### 2.2 Sticky toolbar `.bk-toolbar`

`position: sticky; top: var(--topbar-height); z-index: 10; height: 44px; margin-inline: -32px;
padding: 0 32px` (16 px below 900 px), `background: color-mix(in srgb, var(--color-bg) 92%, transparent);
backdrop-filter: blur(12px); border-bottom: 1px solid var(--color-border)`, groups `.bk-toolbar-start`
/ `.bk-toolbar-end` as `.rv-toolbar-start/-end`. Shown only when the book has pages.

- Start: the existing segmented «صفحات | شبكة» (`data-view-toggle`, persisted `nassakh.bookView`).
- Middle: **filter chips** (DESIGN §6 filter chip: 26 px pill, 1 px `--color-border-strong`, selected =
  `--color-accent-soft` + `--color-accent-text` borderless, `aria-pressed`), each with a live count
  (`<bdi class="num">`): «الكل» · «قيد المعالجة» (status `uploaded|preprocessed|layout_done`, not excluded)
  · «بانتظار المراجعة» (`ocr_done`) · «تحتاج انتباهًا» (`error`, `n_flags > 0` or `sequence_issue`) ·
  «مُراجَعة» (`reviewed|assembled`). A chip whose count is 0 is disabled (45 % opacity) except «الكل».
  Persisted `nassakh.bookFilter.<book_id>`; default «الكل». Excluded pages match only «الكل».
- End: **jump field** (search-input style: 34 px, `--color-bg-muted`, no border, width 112 px,
  `inputmode="numeric"`, `dir="ltr"`, placeholder «إلى صفحة…», `aria-label="الانتقال إلى صفحة"`,
  trailing `<kbd class="kbd">G</kbd>`), then the **follow toggle** `.bk-follow` (`btn-icon` with label
  «تتبّع المعالجة», `aria-pressed`, visible only while `active`; §8).
- Below 900 px the toolbar wraps to two 40 px rows; chips scroll horizontally
  (`overflow-x: auto; scrollbar-width: none`).

### 2.3 Banners

Unchanged components, 8 px under the toolbar: book `error` (headline + folded technical detail +
the retry primary lives in the top bar), `needs_guides` (existing copy), `sheetsFailed`
(«تعذّر تحميل بيانات بعض الصفحات. تُعاد المحاولة عند تحديث الحالة التالي.»). Bound with `x-show` to the
poll's `status`, `error_headline`, `error_detail` (§12.2) so they appear and disappear without a reload.

### 2.4 Summary strip `.bk-summary` (replaces the two cards)

One row, `font-size: 12.5px`, `padding: 10px 0`, `border-block: 1px solid var(--color-border)`,
`display: flex; flex-wrap: wrap; gap: 8px 20px; align-items: center`:

- the five stage counters as `.status` items: `<span class="dot {dot}">` + label + `<bdi class="num">`
  count: «مرفوعة N» · «مُعالَجة N» · «تم التخطيط N» · «تم التعرّف N» · «خطأ N» (counts from `by_status`,
  the `stages` config; a counter with 0 is `--color-text-3`);
- the review summary (`x-show="anyOcrDone"`): «مُراجَعة <bdi>X</bdi> من <bdi>Y</bdi> صفحة» and
  «<bdi>N</bdi> كلمة غير مؤكَّدة» (when N > 0), or «رُوجعت كل الصفحات» when X ≥ Y > 0;
- at the end: `<details class="bk-attention">` with `<summary>` «تحتاج انتباهًا <bdi>n</bdi>»
  (`badge badge-warning num`), open list = the current attention rows (page link, error headline with the
  inline retry form, sequence issue, flag badges). Hidden when n = 0.

### 2.5 Pages

- **Empty state** (nothing ingested): unchanged.
- **«صفحات»**: `.sheet-stack` (`max-width: 1180px; margin-inline: auto; display: flex;
  flex-direction: column; gap: 32px`). One `article.page-sheet` shell per page, server-rendered for the
  first paint, cloned from `<template id="sheet-shell">` for pages ingested later (§13).
- **«شبكة»**: `.page-grid` with the existing tiles, restyled (§7).
- **Position chip** `.bk-pos`: toast styling (dark `#1c1c1f`, white, radius 10, 12.5 px, tabular), fixed
  bottom 20 px centre, «صفحة <bdi>N</bdi> من <bdi>M</bdi>», appears 150 ms after a scroll starts, hides
  1.2 s after it stops; click focuses the jump field. Only in «صفحات» view with ≥ 20 pages.

### 2.6 Responsive

≥ 1180: as above. 900–1180: the summary strip wraps; the progress bar hides; the jump field keeps its
width. < 900: the sheet body stacks (scan above the text pane, both full width, same aspect box), the
toolbar wraps, page padding 16. < 560: the position chip hides; grid min 104 px.

## 3. The sheet and its correspondence

```html
<article class="page-sheet" id="sheet-12" data-page-id="…" data-number="12" data-status="layout_done"
         data-text="provisional" style="--sheet-ar: 1106 / 1634">
  <header class="sheet-head">
    <a class="sheet-title num" href="…">صفحة <bdi>12</bdi></a>
    <span class="status"><span class="dot dot-accent"></span><span class="sheet-status">تم التخطيط</span></span>
    <span class="badge bk-provisional" hidden>نص مبدئي</span>
    <span class="meta num sheet-printed" hidden>الرقم المطبوع <bdi>21</bdi></span>
    <span class="badge badge-warning num sheet-unresolved" hidden><bdi>3</bdi> غير مؤكَّدة</span>
    <span class="bk-reviewed" hidden><span class="bk-mark"><svg class="icon"><use href="#i-check"/></svg></span>مُراجَعة</span>
    <span class="badge badge-dark sheet-excluded" hidden>مستثناة</span>
    <span class="sheet-flags"></span>                 <!-- amber badges: flag labels, sequence issue -->
    <span class="sheet-actions">
      <a class="btn btn-ghost btn-sm sheet-review" hidden>مراجعة</a>
      <button class="btn btn-ghost btn-sm sheet-copy" hidden>نسخ</button>
      <form class="sheet-retry" hidden>…<button class="btn btn-sm">إعادة <span class="sheet-retry-label"></span></button></form>
    </span>
  </header>
  <div class="sheet-body">                            <!-- empty until near; filled from <template id="sheet-body"> -->
    <figure class="sheet-scan"> <img class="sheet-img sheet-img-scan"> <img class="sheet-img sheet-img-clean">
      <div class="sheet-lines" dir="ltr"><span class="sheet-band"></span><span class="sheet-line" data-line="0">…</div>
    </figure>
    <div class="sheet-fac" dir="rtl">
      <div class="fac-layer" data-phase="provisional">
        <div class="fac-block" data-region="body" style="--x0:.12;--y0:.10;--x1:.88;--y1:.78;--lh:…;--fs:…">
          <div class="fac-line" data-line="0" data-fit="justify" style="--lx0:.12;--lx1:.88;--fit:1">
            <span class="fac-text"><span class="tok">…</span> <span class="tok">…</span></span>
          </div>
        </div>
        <hr class="fac-rule" style="top: 78.4%">
        <div class="fac-block" data-region="footnote">…</div>
      </div>
    </div>
  </div>
</article>
```

**Sizes.** `.sheet-body { display: grid; grid-template-columns: minmax(0,1fr) minmax(0,1fr); gap: 16px }`.
`.sheet-scan` and `.sheet-fac` both have `aspect-ratio: var(--sheet-ar)` (`width / height` from the sheet
payload, else the tile's `width/height`, else the book's median ratio, else `7 / 10`), so they are always
the same box and 1 % of one is the same number of pixels as 1 % of the other. `.sheet-fac` is
`position: relative; container-type: inline-size; overflow: hidden; background: var(--color-surface);
border: 1px solid var(--color-border); border-radius: var(--radius-sm)` (white like `.rv-lines-pane`; the
cream `--paper` tint is removed). `.sheet-scan` keeps `--shadow-md` + inset hairline on `--color-bg-muted`
and the 200 ms scan→clean crossfade. The text pane never scrolls: the layout algorithm fits the text into the
box (§4). `.page-sheet { content-visibility: auto; contain-intrinsic-size: auto var(--sheet-h);
scroll-margin-top: calc(var(--topbar-height) + 56px) }` where books.js sets `--sheet-h` on the stack once
per resize: `paneW = min(stackW, 1180); paneW = width ≥ 900 ? (paneW − 16) / 2 : paneW;
sheetH = paneW / ar_median + 36 (head) + 8 (gap)`.

**One index space.** The sheet handle (§13) builds both overlays from a single array `L` of layout lines,
so `.sheet-line[data-line=i]` on the scan and `.fac-line[data-line=i]` in the text pane always mean the
same printed line. `L` comes from, in order of preference: final `lines[].bbox` (text final), Tesseract
`provisional_lines[].bbox` (text provisional), detected `line_boxes` (no text yet). Lines without a bbox get
no scan box (nothing to link) but keep their index. Scan boxes use physical `left/top/width/height`
percentages (image pixels never mirror; the overlay is `dir="ltr"`).

**Scan overlay styles** (review vocabulary): `.sheet-line { position: absolute; border-radius: 2px;
opacity: 0; pointer-events: auto; transition: opacity 150ms ease, background-color 150ms ease }` (idle boxes
are invisible). `.sheet-line.is-lit { opacity: 1; background: var(--color-accent-soft); box-shadow: inset 0 1px
0 rgba(37,99,235,.16), inset 0 -1px 0 rgba(37,99,235,.16) }` (the `.rv-band` look), `.is-lit-2 { opacity: .5 }`
(one-step trail). `.sheet-line.is-hot { opacity: 1; background: rgba(37,99,235,.12); box-shadow: 0 0 0 1.5px
rgba(37,99,235,.7), 0 0 0 6px rgba(37,99,235,.14) }` (the `.rv-box.is-hot` glow) plus a line-number badge:
`.sheet-line.is-hot::before { content: attr(data-n); position: absolute; right: 100%; margin-right: 6px;
top: 50%; transform: translateY(-50%); font: 10.5px/15px var(--font-sans); padding: 0 4px; border-radius: 3px;
background: rgba(20,20,24,.74); color: #fff }` (`data-n` = i + 1, Western digits). While a page is being
cleaned the faint detected boxes are not shown (skeleton mode draws bars on the text side only); from
`preprocessed` on, boxes exist but stay at opacity 0 until lit or hot.

**Hover / focus linking.** One delegated `mouseover`/`mouseout` (and `focusin`/`focusout`) listener on the
stack: `closest('[data-line]')` inside a `.page-sheet` → toggle `is-hot` on
`article.querySelectorAll('[data-line="i"]')` directly (no Alpine reactivity). `.fac-line.is-hot
{ background: var(--color-hover); border-radius: 3px }` (150 ms, `.rv-line.is-current`). Final lines are
`tabindex="-1"` and reachable with ↑/↓ once a sheet is focused (§8). Click on a final line (either side,
`cursor: pointer` only when `data-text="final"`) opens `review_url` (the review page of that sheet; a
per-line deep link is deferred, §16).

**Footnotes.** `.fac-rule` is a hairline (`position: absolute; left: calc(var(--x0) * 100%);
width: 32%; border-top: 1px solid var(--color-border-strong)`) at `top: footnote_y * 100%`, drawn when
`footnote_y` is known, mirroring the printed rule. The footnote block's lines are `color: var(--color-text-2)`.

**Excluded / error sheets.** Excluded: whole article `opacity: .5`, images `grayscale(1)`, the text pane
shows a centred `meta` «مستثناة» + (admin/editor) `btn-sm` «إعادة الصفحة إلى الكتاب» (the existing exclude
form); no effects. Error: scan `box-shadow: 0 0 0 2px var(--color-danger), var(--shadow-md)`, the text
pane shows a compact empty state (danger `icon-lg`, the Arabic `error_headline` 13 px `--color-danger-text`,
one `btn-sm` «إعادة <retry_label>» posting the existing rerun form with `next` = the dashboard).

## 4. Text layout algorithm

Runs once per data arrival for a mounted sheet (pure function `NassakhDecode.layout(page)`, testable
without a DOM), after `document.fonts.load('16px "IBM Plex Sans Arabic"')`; re-run once on `fonts.ready` if
the first run happened before the font loaded. All geometry is emitted as CSS custom properties in
container units, so window resizes need no JS.

### 4.1 Inputs (per page, from `api:book_sheets`)

- `W, H` image size (gray-image px) → `ar = W / H`.
- Text lines `T`: final `lines[]` (`tokens`, `region_kind`, `bbox` ratios | null) or `provisional_lines[]`
  (`words`, `region_kind`, `bbox` ratios | null); `[]` when no text yet.
- Detected `line_boxes[]` (ratios, sorted by `y0`), `n_lines`, `median_line_h` (ratio of H, 0 = unknown),
  `footnote_y` (ratio | null), `regions[]` (`kind`, ratio bbox; `[]` before layout).

### 4.2 Groups

Two groups, `body` and `footnote`. A text line belongs to `footnote` iff `region_kind == "footnote"`; a
detected box belongs to `footnote` iff `footnote_y != null && box.y0 >= footnote_y`. Heading, poetry and
other kinds join `body` (they are placed by their own bbox where it exists).

### 4.3 Block box per group `B_g = [X0, Y0, X1, Y1]` (ratios)

1. From the group's text lines with a bbox, when ≥ 60 % of them have one:
   `X0 = min x0, Y0 = min y0, X1 = max x1, Y1 = max y1`.
2. Else from the group's detected boxes (same min/max), when there is at least one.
3. Else from `regions[]`: the union of the group's region bboxes (`footnote` regions for `footnote`; every
   other non-skipped kind for `body`).
4. Else defaults: body `[0.12, 0.10, 0.88, footnote_y ?? 0.90]`, footnote `[0.12, footnote_y + 0.012, 0.88, 0.94]`.

Pad the block vertically by `0.12 · h_g` on both sides (Tesseract boxes clip diacritics), clamped to `[0, 1]`.

### 4.4 Median line height `h_g` and font size

- `h_g = median(y1 − y0)` over the group's text-line bboxes, else over its detected boxes, else
  `median_line_h` (for `body`; footnote = `0.85 · median_line_h`), else pitch-derived (below).
- **Font size as a fraction of the container width** (`1cqw` = 1 % of the pane width, and because the pane
  keeps the page's aspect ratio, a ratio of H maps to `ratio · H/W` of the width):
  `fs_cw = K · h_g · (H / W)`, `K = 0.78` (`--fac-k`; the ink of a running Arabic line spans ascender to
  descender ≈ 1.28 em of IBM Plex Sans Arabic). CSS:
  `.fac-block { font-size: clamp(9px, calc(var(--fs) * 100cqw), 28px) }`. Footnote `fs_cw` is capped at
  `0.9 · fs_cw(body)`.
- Pitch-derived (no `h_g` at all): with `n` = number of lines and `blockH = Y1 − Y0`, `p = blockH / n`,
  `h_g = 0.62 · p`, `K = 1` (0.62 = 0.78 × the usual ink/pitch ratio 0.8).
- **Line box height** `lh_cw = 1.24 · h_g · (H / W)` → `.fac-line { height: calc(var(--lh) * 100cqw) }`.
- **Shrink to fit:** if `n · lh_cw > blockH · (H/W)` (the lines cannot fit their block), multiply `fs_cw`
  and `lh_cw` by `blockH · (H/W) / (n · lh_cw)`; never overflow the block, never scroll.

### 4.5 Vertical distribution (the owner's flex column)

```css
.fac-layer { position: absolute; inset: 0; }
.fac-block { position: absolute; left: calc(var(--x0) * 100%); top: calc(var(--y0) * 100%);
             width: calc((var(--x1) - var(--x0)) * 100%); height: calc((var(--y1) - var(--y0)) * 100%);
             display: flex; flex-direction: column; justify-content: space-between; align-items: stretch; }
.fac-block[data-few] { justify-content: center; gap: calc(0.6 * var(--lh) * 100cqw); }   /* n ≤ 3 */
.fac-line { position: relative; flex: 0 0 auto; height: calc(var(--lh) * 100cqw);
            display: flex; align-items: center; min-width: 0; }
```

Lines are laid out top to bottom in `T` order inside their block; `space-between` puts the first line at the
block's top and the last at its bottom, which for a regularly set page lands every line on its printed
position (the block box was taken from the boxes themselves). Headings with larger gaps may sit a few pixels
off — accepted; correspondence is by index, not by pixel. A block with ≤ 3 lines is centred with a gap
(`data-few`), not flung to the corners.

### 4.6 Horizontal extents and fit per line

- When the line has its own bbox: `.fac-line { margin-left: calc((var(--lx0) - var(--x0)) * 100cqw); width:
  calc((var(--lx1) - var(--lx0)) * 100cqw) }` — physical `margin-left`, the block is `dir="rtl"` but boxes
  are image space (a right-indented last line lands on the right, as printed). Without a bbox the line
  spans the block (`width: 100%`).
- **Width fit** (once per data arrival, one shared offscreen canvas, `ctx.font = '100px "IBM Plex Sans
  Arabic"'`): `w100 = ctx.measureText(lineText).width` (px at 100 px). With the line's width as a fraction of
  the container `bw_cw = lx1 − lx0` (or `X1 − X0`) and the group's `fs_cw`, the scale-invariant ratio is
  `r = bw_cw · 100 / (w100 · fs_cw)` (box width over the natural text width at the block's font size).
  Rules → `data-fit` and `--fit`:
  - `r ≥ 1.4` → `start` (a short line: last line of a paragraph, a title), or `center` when the line has a
    bbox and `|((lx0 + lx1) / 2) − ((X0 + X1) / 2)| < 0.04` and `(lx1 − lx0) < 0.7 · (X1 − X0)` (centred heading);
  - `1.0 ≤ r < 1.4` → `justify`;
  - `0.85 ≤ r < 1.0` → `justify`, `--fit: r`;
  - `r < 0.85` → `over`, `--fit: 0.85` (the surplus runs into a 14 px end fade; `title` holds the line).
- CSS:

```css
.fac-text { display: block; width: 100%; direction: rtl; white-space: nowrap; overflow: hidden;
            font-size: calc(var(--fit, 1) * 1em); line-height: 1.2; text-align: justify; text-align-last: start;
            text-justify: inter-word; overflow-wrap: normal; }
[data-fit="justify"] .fac-text, [data-fit="over"] .fac-text { text-align-last: justify; }
[data-fit="center"] .fac-text { text-align-last: center; }
[data-fit="over"] .fac-text { mask-image: linear-gradient(to right, transparent, #000 14px); }
.tok { unicode-bidi: isolate; }
```

A single-line block is its own last line, so `text-align-last` is what actually justifies (inter-word
spacing only; kashida is not attempted). No `letter-spacing` (it breaks Arabic joins visually); tokens are
whole words in one text node each, real spaces between them (selection copies words).

### 4.7 Special cases

- 0 text lines and `text_state == final` → centred `meta` «لا نص في هذه الصفحة.».
- No boxes and no text → skeleton of 14 bars in the default body block (§5.1).
- 1–3 text lines without bboxes (a title page) → `data-few` centred group at `fs_cw = 1.6 · (H/W)`
  (≈ 14 px on a 900 px tall pane).
- A title page whose boxes are known → normal path (big type comes from big boxes, capped at 28 px).
- Text lines that exceed detected boxes by > 30 % (Tesseract split lines): the text count wins (it only
  tightens the pitch).
- `regions[]` present but no boxes (born-digital pages): block from the regions, pitch-derived font.

## 5. Generation effect (decode.js, D23 as amended by D28)

All parameters are constants at the top of `decode.js` and named here. Effects run only for mounted sheets
near the viewport, only while the page is in a processing state and the book is `active`; they stop the tick
the state ends. One shared rAF loop at 24 fps; DOM writes only on state changes.

### 5.1 Skeleton (no text yet: `uploaded`, `preprocessed`, `layout_done` with `text_state == none`)

One `.fac-bar` per layout line (detected boxes; else 14 bars in the default block with widths
100/100/92/100/84/100/96/100/88/100/100/72/100/48 %): `height: 55%` of the line box, vertically centred,
`border-radius: 3px`, the review skeleton gradient `linear-gradient(90deg, var(--color-bg-muted) 25%,
var(--color-bg-subtle) 50%, var(--color-bg-muted) 75%); background-size: 200% 100%; animation: bk-shimmer
1.4s linear infinite; animation-delay: calc(var(--i) * 40ms)`. `bk-shimmer` duplicates `rv-shimmer`
(200 % → −200 %) in theatre.css so the dashboard never references `rv-` names. Paused
(`animation-play-state: paused`) unless the sheet `.is-near`. When boxes arrive the bars move to the box
positions with `transition: top 260ms, width 260ms cubic-bezier(.2,.7,.2,1)` (same element count where
possible; extra bars fade in, surplus bars fade out, 200 ms).

### 5.2 Provisional text: dwell, veil, return

Words are Tesseract's (`provisional_lines[].words`, else `provisional_text` split by lines and whitespace).

- **Alphabet.** A veiled letter is replaced by a letter of the same skeleton family, never itself:
  `{ب ت ث ن ي}`, `{ج ح خ}`, `{د ذ}`, `{ر ز}`, `{س ش}`, `{ص ض}`, `{ط ظ}`, `{ع غ}`, `{ف ق}`, `{ه ة}`, `{و ؤ}`,
  `{ا أ إ آ}`, `{ك ل}` → any other base letter draws from `{ب ن ت}`. Never the first letter of a word,
  never a combining mark (U+064B–U+0652, U+0670), never digits or Latin. No ASCII symbols. No length
  changes (the current `extra` glyph is removed). The whole word stays one text node (joins preserved).
- **Cycle per word.** Period `P ~ U(3500, 6500) ms`, random phase. `DWELL = 0.78 · P` shows Tesseract's
  exact string; `VEIL = 0.22 · P` (≈ 0.8–1.4 s) shows the veiled string with `nVeil =
  clamp(round(len · 0.25), 1, 3)` letters (len counted in base letters). Veiled positions are drawn at veil
  start and re-drawn once at its midpoint. Writes per word ≈ 3 per cycle (≈ 0.6 / s).
- **Budget.** At most `VEIL_MAX = 40` veiled words per sheet at any moment; a word due to veil while the
  sheet is at the cap keeps dwelling and re-phases (`nextAt += 400 ms`). Steady state ≈ 20 % of the words
  are veiled, every word visibly returns to Tesseract's reading and drifts away again.
- **Colour and fade.** `.fac-layer[data-phase="provisional"] { color: var(--color-text-3) }`,
  `.tok { opacity: .85; transition: opacity 400ms ease }`, `.tok.is-veiled { opacity: .45;
  transition-duration: 500ms }` — the veil enters and leaves as an opacity fade, never a blink. On first
  arrival lines fade in: `.fac-line { animation: bk-line-in 320ms ease-out both; animation-delay:
  calc(var(--i) * 16ms) }` (`bk-line-in` = `rv-line-in`: opacity 0 → 1, translateY 6 px → 0; `--i` capped at 24).
- **Reading cursor and shimmer (the highlight).** Per sheet, index `k` advances every `LINE_MS = 420 ms`
  over the layout lines top to bottom, then rests `CYCLE_REST = 1200 ms` and starts again while the page
  stays provisional (or has only skeleton bars: then the cursor lights scan boxes and bars). On each step:
  scan box `k` ← `.is-lit`, `k − 1` ← `.is-lit-2`; text line `k` ← `.is-lit`. `.fac-line.is-lit .fac-text
  { color: transparent; background: linear-gradient(to left, var(--color-text-3) 0 40%, var(--ink-shine) 50%,
  var(--color-text-3) 60% 100%) 0 0 / 250% 100% no-repeat; -webkit-background-clip: text;
  background-clip: text; animation: bk-sheen 700ms cubic-bezier(.2,.7,.2,1) 1 both }` with
  `@keyframes bk-sheen { from { background-position-x: 0% } to { background-position-x: 100% } }` — an ink
  band crosses the line right to left, the reading direction; `.fac-line.is-lit .tok { opacity: 1 }`.
  Gray only: no accent on the text during processing; the scan band is the review's accent-soft band.
- **Synchronised return.** When the cursor reaches line `k`, every word on it is set to its exact string at
  once and locked clean for `LOCK_MS = LINE_MS + 1200`; drift resumes only after the band has moved on.
  The shimmer therefore always passes over Tesseract's real words: this is the visible "back to Tesseract,
  then away".
- **Head.** Status label from the page; badge «نص مبدئي» (`.bk-provisional`, `badge`, `--color-text-3`)
  while `text_state == provisional`.

### 5.3 Resolve into final (≤ 1.5 s, D24)

On `text_state: final` for a mounted sheet that was provisional or skeleton: (1) remove the cursor and lit
classes; (2) build the final `.fac-layer[data-phase="final"]` beneath the provisional layer with its own
geometry (§4 from `lines[].bbox`), every `.tok` starting `color: var(--color-text-3); opacity: .55`, and
fade the provisional layer to opacity 0 over 300 ms, then remove it; (3) wave: words ordered (line, token)
land at `t0 + i / (n − 1) · wave`, `wave = clamp(n · WAVE_PER_WORD_MS 35, 600, 1400)`; landing = `.is-landed
{ color: var(--color-text); opacity: 1; transition: color 300ms ease-out, opacity 300ms ease-out;
animation: decode-land 400ms ease-out both }` (the existing accent-soft wash: the one accent moment on the
text); the scan box of the landing line gets `.is-lit` and follows the wave front; (4) unresolved
low-confidence tokens (`conf == "low" && !res`) land with `.tok-low` (1.5 px `--color-warning` underline);
(5) at `doneAt = t0 + wave + 300` the layer is `is-resolved`, `aria-hidden="false"`, all motion off, scan
boxes back to opacity 0 (hover only). A final sheet that mounts later renders statically (no wave).

### 5.4 Static fallback

Book inactive with the page still provisional (stopped, `needs_guides`, error elsewhere): plain
Tesseract text in its geometry, `--color-text-3` at opacity .7, no veil, no cursor; badge «نص مبدئي».
`prefers-reduced-motion`: the same static rendering for provisional, static bars for skeleton, final
rendered at once, no sweep.

### 5.5 Scan sweep (softened)

While `uploaded` and active: `.scan-sweep::after` band `height: 14%`, gradient white 0 → .4 → 0, reading edge
`1px solid rgba(37,99,235,.25)`, one pass `SWEEP_MS = 2400`, driven by `--sweep` as today. The sweep
finishes its current pass when the cleaned image arrives (never snaps mid-pass, ≤ 2.4 s), then the
200 ms crossfade plays.

### 5.6 Parameter table

| Name | Value | Where |
|---|---|---|
| `FPS` | 24 | decode.js loop |
| `P` (word cycle) | U(3500, 6500) ms, dwell 78 % / veil 22 % | §5.2 |
| `nVeil` | clamp(round(len × .25), 1, 3) | §5.2 |
| `VEIL_MAX` | 40 words per sheet | §5.2 |
| `LINE_MS` / `SHEEN_MS` / `CYCLE_REST` / `LOCK_MS` | 420 / 700 / 1200 / 1620 ms | §5.2 |
| `WAVE_PER_WORD_MS`, `WAVE_MIN_MS`, `WAVE_MAX_MS`, `DONE_DELAY_MS` | 35, 600, 1400, 300 | §5.3 |
| `SWEEP_MS` | 2400 | §5.5 |
| `--fac-k` | .78 | §4.4 |
| veil opacity / dwell opacity / static opacity | .45 / .85 / .7 | §5.2, §5.4 |
| skeleton shimmer | 1.4 s linear, 40 ms stagger | §5.1 |

## 6. Processing states

Per sheet: **scan | text pane | head**. `active` = book status `processing|ocr`.

- **uploaded, active** — scan thumb (`saturate(.85)`) with the softened sweep | 14 distributed skeleton
  bars shimmering | dot neutral «مرفوعة».
- **preprocessed** — cleaned image crossfades in (200 ms); boxes exist at opacity 0 | bars glide to the
  detected line positions (260 ms), `.fac-rule` appears when `footnote_y` is known | «مُعالَجة». If the book
  is `needs_guides`: bars static, no cursor, head gets a ghost «ضبط الأدلة» (admin/editor).
- **layout_done, text none (fast OCR running)** — cursor runs over the boxes (scan band) and the bars |
  «تم التخطيط».
- **layout_done, text provisional (models running)** — cursor + shimmer on both panes | dwell-and-veil text
  at its Tesseract line positions, justified | «تم التخطيط» + badge «نص مبدئي».
- **ocr_done** — cursor stops, provisional layer fades, right-to-left wave lands the final lines in
  `--color-text`; uncertain words amber-underlined; hover linking on | head dot success «تم التعرّف», badge
  «<bdi>N</bdi> غير مؤكَّدة» (warning) or nothing when N = 0, ghost «مراجعة» and «نسخ» fade in (120 ms).
- **reviewed / assembled** — final text; head shows `.bk-reviewed` (20 px success circle `.bk-mark` with
  `bk-stamp-in` 300 ms when it changes live) «مُراجَعة»; «مراجعة» stays (the review page can reopen);
  no effects.
- **error** — scan danger ring; text pane = designed failure (§3); head dot danger, the retry form in the
  head (admin/editor). Matches the «تحتاج انتباهًا» filter.
- **excluded** — §3; matches only «الكل».
- **provisional, book inactive** — static muted text, badge «نص مبدئي», no motion.
- **final with zero lines** — «لا نص في هذه الصفحة.» centred.
- **attention flags / sequence issue (any status)** — amber badges in `.sheet-flags`; the page matches
  «تحتاج انتباهًا».

Book level (top bar + strip): `uploaded` → empty state, primary «بدء المعالجة»; `processing|ocr` → pulsing
status chip, live bar and counters, follow toggle available; `needs_guides` → banner, primary «ضبط الأدلة»;
`error` → danger banner with headline and folded detail, primary «إعادة بدء المعالجة»;
`ready_for_review|reviewing` → strip shows the review summary, primary «الصفحة التالية للمراجعة»;
everything reviewed → «رُوجعت كل الصفحات», primary «نسخ نص الكتاب».

**End of processing (no reload).** The first poll with `active: false` after an active one: every mounted
sheet drops its effects (provisional sheets go static, §5.4), the status chip stops pulsing and shows the
new label, the primary swaps, banners follow `status`, the follow toggle hides, and one toast
«اكتملت المعالجة · <bdi>X</bdi> صفحة» with the single action «ابدأ المراجعة» (opens `next_review_url`; no
action when none). Scroll position and mounted sheets are untouched. Polling stops.

**Live transitions** come from the poll's changed set (status, `text_state`, `n_unresolved`,
`is_reviewed`, `n_flags`, `sequence_issue`, `error`): the head of that page is patched, and if the sheet is
mounted it refetches itself through `api:book_sheets` (batched ≤ 40, existing queue) and plays only the
transition for its new state. The `aria-live` line announces «الصفحة N: <status_label>» once per poll.

## 7. Grid view («شبكة»)

`_page_tile.html` keeps its structure; the palette goes muted: the 3 px stage bar fill is
`rgba(24,24,27,.35)` (accent only for the 200 ms it reaches 100 %, then fades out), the sweep is the softened
one, `preprocessed|layout_done` show a 1 px reading line (`rgba(24,24,27,.18)`, travelling top → bottom every
2.4 s, the tile's `--sweep`), `ocr_done` crossfades to the cleaned thumb and shows the amber count mark
(`.bk-mark-count` = `.rv-thumb-mark.is-count` values, 16 px, 2 px `--color-bg` ring), `reviewed` the green
check (`.bk-mark-check` = `.rv-thumb-mark.is-check`), `error` the danger ring, excluded grayscale .45 with the
«مستثناة» badge. Footer: number (12.5/500) + status dot only; the label lives in `title` / `aria-label`.
Tile effects run only while `.is-near` (a coarse observer on the grid, rootMargin 600 px). Filters apply to
tiles too (`hidden`). Density: 132 px min (104 px below 560 px).

## 8. Ease of use

1. **Jump to page.** Enter (or blur with a number) in the jump field scrolls to `#sheet-N`
   (`scrollIntoView({ block: 'start', behavior: 'smooth' })`; `scroll-margin-top` clears the sticky
   toolbar), pulses the sheet (`bk-pulse-bg` 500 ms) and focuses its title link. Out of range → toast
   «لا صفحة بهذا الرقم». `G` focuses the field from anywhere. `#sheet-N` in the URL on load jumps there.
   In «شبكة» the jump scrolls to the tile.
2. **Filters** (§2.2) set `hidden` on non-matching shells/tiles in one loop; the empty result shows the
   review-style line «لا صفحات تطابق هذا المرشّح»; counts update from every poll without layout shifts
   (`tabular-nums`, chips have `min-width`).
3. **Follow processing** («تتبّع المعالجة», default off, persisted `nassakh.bookFollow`): when on, after a poll the stack scrolls smoothly (≤ 600 ms, at most once per 2 s)
   to the highest-numbered page that just entered `provisional` or `ocr_done`; any user wheel, touch or
   keyboard scroll turns it off with a quiet toast «أُوقف التتبّع». Only in «صفحات» view while `active`.
4. **Attention where the page is.** Flags, sequence issue and the error headline sit in the sheet head with
   the inline «إعادة <stage>» retry; the strip's `<details>` and the «تحتاج انتباهًا» chip give the overview.
5. **Hover linking** both ways with the line-number badge on the scan; click a final line → the review page.
6. **Review entry** is one obvious path: the primary «الصفحة التالية للمراجعة», the strip's summary, the
   ghost «مراجعة» on every final sheet. `N` opens `next_review_url`.
7. **Copy.** Per sheet «نسخ» (final only; `C` while a sheet is focused) and «نسخ نص الكتاب» in the menu (or
   as the primary when all is reviewed), both through `window.Nassakh.copyText` and the «تم النسخ» toast.
8. **Keyboard** (only when no field is focused, RTL-aware, mirrors the review screen): `G` jump field,
   `N` next page to review, `1` / `2` switch «صفحات» / «شبكة», `ArrowLeft` next sheet / `ArrowRight` previous
   sheet (scroll + focus title), `↑` / `↓` move the hot line inside the focused sheet (band follows on the
   scan), `Enter` on a focused title opens the page, `C` copies the focused sheet, `Esc` clears the filter.
   No shortcut sheet on the dashboard (the review has one; here `title` hints carry the keys).
9. **Position chip** while scrolling (§2.5); **scroll memory** per book in `sessionStorage`
   (`nassakh.bookScroll.<id>`) so returning from a review page lands on the same sheet.
10. **Quiet chrome.** Rerun and exclude are in menus (top-bar «⋯», the tile's hover toggle); successful polls
    are silent; the live region announces once per poll; all copy is factual counts.

## 9. Theming

Carry the review vocabulary one to one; new classes are prefixed `bk-`/`fac-`, defined in `theatre.css`
(`@layer components`). The dashboard never references `rv-` classes (review.css is outside the frontend
agent's files), so the handful of shared values are duplicated verbatim and marked `/* = rv-… */` for a later
unification:

- Tokens added on `.bk-dashboard` (the component root): `--fac-k: .78; --bk-toolbar-h: 44px;
  --bk-sheet-gap: 32px; --ink-shine: #3f3f46; --ink-wash: rgba(24,24,27,.06)`.
- Surfaces: text pane `--color-surface` + 1 px `--color-border` + `--radius-sm`; scan `--color-bg-muted`
  + `--shadow-md` + inset hairline; toolbar and strip `--color-bg` with hairlines only (no cards); badges on
  images and the position chip `rgba(20,20,24,.74)` / toast `#1c1c1f`.
- Typography: IBM Plex Sans Arabic; text-pane size from the page (9–28 px, `line-height: 1.2`); chrome at
  DESIGN sizes (section title 14/600, meta 12 `--color-text-3`, status 12.5, sheet title 13/600, chips 12.5/500);
  `tabular-nums` on every count; `<bdi>` around numbers inside Arabic strings.
- Colour meaning only: accent = in progress / hot / lit band, success = reviewed, warning = uncertain and
  attention, danger = error, neutral = pending; status always dot + label. The text pane is gray during
  processing; the accent appears on the text only in `decode-land`.
- Motion: chrome 120 ms (hover, opacity), 150 ms (hot states), 200 ms (crossfades, chips, banners), 260 ms
  `cubic-bezier(.2,.7,.2,1)` for glides (bars, position chip, jump), 320 ms progress fill, resolution ≤ 400 ms
  (`decode-land`, `bk-stamp-in` 300 ms); loops only on processing content (D24): `bk-shimmer` 1.4 s,
  sweep 2.4 s, cursor 420 ms/line, sheen 700 ms, status-dot `bk-pulse` 1.2 s.
- Keyframes in theatre.css: `bk-shimmer` (= `rv-shimmer`), `bk-pulse` (= `rv-pulse`), `bk-line-in`
  (= `rv-line-in`), `bk-pulse-bg` (= `rv-pulse-bg`), `bk-stamp-in` (= `rv-stamp-in`), `bk-sheen`,
  `decode-land` (existing).
- Classes reused from app.css as they are: `btn*`, `btn-icon*`, `segmented`, `badge*`, `status`/`dot*`,
  `progress*`, `menu*`, `banner*`, `toast`, `kbd`, `meta`, `num`.
- Spacing: 8 px inside groups, 16 px between scan and text pane, 32 px between sheets, 24 px between
  toolbar, strip and stack. No new colours.

## 10. Reduced motion

`prefers-reduced-motion: reduce` (and `NassakhDecode.reducedMotion`): no sweep (`display: none`), static
skeleton bars (no shimmer), provisional text static in `--color-text-3` at opacity .7 (no veil, no cursor,
no sheen), final rendered at once (no wave, no `decode-land`), no line entrance stagger, no position-chip
transition, jump uses `behavior: 'auto'`, no `bk-pulse`. Hover/hot states keep their 150 ms fades (app.css
already zeroes durations globally under the media query).

## 11. Performance budget (800 pages)

- **Shells are static.** The sheet head is server-rendered markup (no per-shell Alpine bindings except one
  `x-init="observeSheet($el)"`); books.js patches only the pages in the poll's changed set
  (`patchSheet(el, page)` sets text, `hidden`, classes, `data-*`). Pages ingested after load are cloned from
  `<template id="sheet-shell">` and patched the same way. Target: a poll `apply()` for 800 pages < 8 ms on an
  M-series Mac (today ~12 k Alpine effects re-evaluate per poll).
- **Bodies mount near the viewport** (existing near/far observers, 1500 / 4000 px) from
  `<template id="sheet-body">`; a mounted sheet is one `NassakhDecode.sheet()` handle. ≤ 6 mounted sheets,
  ≤ 400 `.tok` spans each → ≤ 2.4 k live spans.
- **`content-visibility: auto` + `contain-intrinsic-size: auto var(--sheet-h)`** with `--sheet-h` from the
  measured pane width (§3), so 800 shells cost nothing off-screen and the scrollbar does not jump.
- **Geometry is CSS.** All placement uses `cqw` and percentages; resizes need no JS; `measureText` runs
  once per line per data arrival (~0.05 ms per line) on one shared canvas, outside the loop.
- **The loop** writes only on state changes: per word one comparison per tick (`t >= w.nextAt`), ≈ 0.6
  string writes per word per second, ≤ 40 veiled words per sheet; the cursor toggles ≤ 4 classes per
  420 ms per sheet; veil, lit, land and hot are class toggles animated by CSS (opacity, background,
  transform) on the compositor. Budget: ≤ 1 ms per frame for 6 mounted sheets; no layout reads in the loop.
- **Skeleton and tile loops** are CSS animations with `animation-play-state: paused` unless `.is-near`.
- **Poll** uses the compact payload (§12.2): ~120 KB for 800 pages instead of ~500 KB, no URL reversal
  server-side; changed mounted sheets refetch through `api:book_sheets` in batches of ≤ 40.
- **Images** `loading="lazy" decoding="async"`; grid uses thumbnails only; the display image only for
  mounted sheets.
- **Filters** toggle `hidden` in one loop (< 2 ms for 800); hover linking is delegated (two listeners on
  the stack).

## 12. Data contract (backend agent)

### 12.1 `GET /api/books/<id>/sheets/?from=&to=` (`books.services.book_sheets`)

Per page, in addition to the current keys (`id, number, status, status_label, dot, text_state,
provisional_text, lines, display_url, scan_url, thumb_url, scan_thumb_url, width, height, line_boxes,
n_lines, n_unresolved, is_reviewed, is_excluded, printed_number, error, url, review_url`):

| Key | Type | Definition |
|---|---|---|
| `footnote_y` | float ∈ [0,1] \| null | `(pre.footnote_rule_y ?? pre.footnote_block_y) / height`, 4 decimals; null when neither exists or before preprocessing |
| `median_line_h` | float | `pre.median_line_height / height`, 4 decimals; `0` when unknown |
| `regions` | list | `[{"kind": str, "bbox": [x0, y0, x1, y1]}]` for the page's `Region` rows whose kind is not `running_header` / `page_number`, ordered by `order`, bbox as ratios of `width`/`height` (4 decimals); `[]` before layout |
| `provisional_lines` | list | `[{"region_kind": str, "bbox": [x0, y0, x1, y1] \| null, "words": [str, …]}]`, see below; `[]` when `text_state == none` |
| `lines[].id` | int | `Line.pk` |
| `lines[].bbox` | list \| null | `Line.bbox` (gray px) as ratios, 4 decimals; null when the line is unanchored |

`provisional_lines` rules: take the latest `OcrRun` with `status == OK` and `engine_name == fast`
(`ocr.services.engine_names()[2]`) per target — one per `Region` whose kind is not in
`ocr.services.SKIPPED_KINDS`, or the page-level run (`region is null`, `params.scope == "page"`) when the
page has no regions; walk targets in region `order` with non-footnote kinds first and `footnote` last
(the order of `join_region_texts`); for each run emit one entry per `params["lines"]` item with
`words = [w["text"] …]` and `bbox` from the line's `bbox` (already gray-image px) as ratios; drop a first or
last line of the whole list whose text is only a page number (`ocr.services.page_number_edges` on the joined
texts), exactly as `provisional_text` does. When no run has `lines` (text-layer books, old runs) but
`provisional_text` is non-empty, derive entries from its non-blank lines with `bbox: null` and
`region_kind: "body"`. One query for the runs of the whole range (`page_id__in`, `engine_name`, `status`),
grouped in Python; the function stays at ≤ 5 queries.

### 12.2 `GET /api/books/<id>/progress/` (`books.api.book_progress`, `books.services`)

- Always add `error_headline` (str, `_headline(book.error_message)` when `status == error`, else `""`) and
  `error_detail` (str, `_detail(...)` likewise).
- `?compact=1`: `pages` entries carry only `id, number, status, text_state, n_unresolved, n_flags,
  sequence_issue, is_excluded, is_reviewed, error, printed_number`, plus `flag_labels` only when
  `n_flags > 0` and `error_headline, retry_stage, retry_label` only when `error` is true. No URLs, labels,
  dots, thumbnails or sizes (the client keeps those from the first paint and from `api:book_sheets`).
  Implemented as `page_tiles(book, compact=True)` → `page_tile(page, issue, compact=True)`; the same
  `_TILE_DEFERRED` query. Target ≤ 150 B per page.
- `book_dashboard` config adds `statusLabels: {status: label}` and `statusDots: {status: dot}` for every
  `Page.Status`, and `urls: {"page": "/books/<id>/pages/__n__/", "review": "/books/<id>/review/__n__/",
  "rerun": "/books/<id>/pages/__n__/rerun/", "exclude": "/books/<id>/pages/__n__/exclude/"}` (built with
  `reverse(..., args=[book.pk, 0])` and `0` replaced by `__n__`), so the client builds the URLs of pages
  ingested after load. `config.pages` stays the full first-paint tiles.

Not in scope (needs a model field or another app): a timestamp delta (`Page` has no `updated_at`),
per-line review deep links (`review.js`).

## 13. JavaScript contract

### 13.1 `window.NassakhDecode` — backward compatible surface, new behaviour, one new entry point

Unchanged signatures, used by the page-detail text panel (`ocr.js`) and the review pending state
(`review.js`): `attach(el, {text, mode, lines, static})`, `update(el, {text, static})`,
`resolve(el, {lines, onDone})`, `render(el, {lines})`, `effect(el, fn)`, `detach(el)`, `step(t)`,
`inspect(el)`, `reducedMotion`, `size`. Behaviour changes inside them (listed so the text panel is
reviewed, not surprised): provisional mode is the dwell-and-veil cycle of §5.2 (Arabic-only same-family
letters, no length changes, words return to the exact string); noise mode builds its pseudo-words from
Arabic letters only; `inspect()` keeps `real, shown, nReal, landed, low` and adds `veiled: bool`. The
resolve wave and `render` are unchanged. `.decode` CSS in theatre.css (used by the text panel and the
review pending state) keeps its selectors.

New:

```js
const h = NassakhDecode.sheet(host, { scan })     // host = .sheet-fac, scan = .sheet-lines (both mounted)
h.update({ page, active })                          // page = api:book_sheets item (or the tile before it arrives)
                                                    // decides the mode: skeleton | provisional | final | static | empty | error | excluded
h.lit(i)                                            // set the cursor index by hand (tests); -1 clears
h.hot(i)                                            // set the hot line by hand (tests / keyboard); -1 clears
h.destroy()                                         // stop, forget, leave the DOM as is
NassakhDecode.layout(page) -> { ar, groups: [{ kind, block: [X0,Y0,X1,Y1], fsCw, lhCw, few, lines: [{ i, kind, words, box, lx0, lx1, r, fit, scale }] }], rule: footnote_y|null, mode }
NassakhDecode.measure = (text) => width_at_100px     // injectable (tests stub it; the browser uses the canvas)
```

`layout()` is pure (§4) and returns the numbers `sheet()` writes as custom properties; it is what
`core/test_theatre.py` exercises under Node.

### 13.2 `books.js` (Alpine `bookDashboard`)

Owns: the poll (compact mode, backoff, health pill, no reload), the changed-set diff and `patchSheet` /
`patchTile`, the primary-button and banner state, filters, the jump field, the follow toggle, the position
chip, the keyboard map, `--sheet-h`, the near/far observers, mounting bodies from `<template id="sheet-body">`
and creating `NassakhDecode.sheet` handles, the sheets fetch queue (unchanged), copy actions, the live
region. It no longer uses `x-effect` per sheet or `x-for` over pages in the sheets view; the grid keeps a
light `x-for` only for pages ingested after load (or the same clone-and-patch approach — the agent's choice,
but the sheets view must be static).

## 14. File ownership

| Agent | Owns (may edit) |
|---|---|
| frontend | `templates/books/**` (`detail.html`, `_page_sheet.html`, `_page_tile.html`, new `_sheet_body.html` include is allowed), `static/src/js/books.js`, `static/src/js/decode.js`, `static/src/components/theatre.css`, `static/src/components/books.css`, `core/test_theatre.py` |
| backend | `books/services.py`, `books/api.py`, `books/tests.py`, `ocr/services.py` (only if a helper is genuinely shared) |

Neither edits `base.html`, `ui.js`, `app.css`, `review.*`, `ocr.js`, `_text_panel.html`, `docs/DECISIONS.md`.
The frontend agent builds against §12 with fixture JSON until the backend lands, and must degrade when the
new keys are absent (`provisional_lines` missing → derive from `provisional_text`, bbox null; `regions`
missing → defaults; `lines[].bbox` missing → distributed). Rebuild CSS with `npm run build:css`; check JS
with `node --check`.

## 15. Tests

`core/test_theatre.py` (frontend): replace the D23 assertions `capOk`/`settled == 0` with D28 ones — every
shown string is either the exact word or differs in 1–3 base letters from the same family, never the first
letter, never a digit, never a combining mark, no ASCII, same length; over 20 s every word was exact at some
tick and veiled at some tick; the lit line's words are exact while lit; ≤ 40 veiled words per sheet. Add:
`layout()` on fixtures (regular body page with boxes → block from boxes, `fs_cw = .78 · h · H/W`, all
lines `justify`/`start` by `r`; page without boxes → defaults and pitch font; title page → `few`; footnotes
→ second group under `footnote_y`; final lines with `bbox: null` → distributed); the sheet handle under the
Node stub (skeleton bars count, provisional layer classes, resolve wave order, `is-hot` on both sides for
the same index); the dashboard (compact poll merge, changed set, filters hide/show counts, jump parsing,
primary-button choice per state, no reload at the end, follow target = highest changed page); compiled CSS
(`.sheet-fac{…container-type:inline-size…}`, `.fac-block{…justify-content:space-between…}`,
`.fac-text{…text-align:justify…}`, `@keyframes bk-sheen`, the reduced-motion block). Keep the existing text
panel (`ocr.js`) assertions passing: `tpAttached`, `tpResolving`, `tpDone`, `tpStatic`.

`books/tests.py` (backend): `provisional_lines` from a fast run with `params["lines"]` (ratios, region
order, footnotes last, page-number edge line dropped, `[]` when `text_state == none`, derived from
`provisional_text` without runs); `lines[].id`/`bbox` ratios and null; `footnote_y` (rule, block, null),
`median_line_h`, `regions` without skipped kinds; `?compact=1` keys and size; `error_headline`/`error_detail`;
query counts (sheets ≤ 5, progress compact ≤ 4); `book_dashboard` config `statusLabels`, `statusDots`, `urls`.

## 16. Deferred (not in this build)

Per-line deep link from a sheet into the review screen (`review.js` must honour `?line=<order>`); a
timestamp delta poll (`Page.updated_at` + migration); the bottom minimap scrubber (a second bar competes
with the toolbar and the position chip; revisit when the owner asks for it); the sheet-size control ص/م/ك;
moving `rv-` keyframes and marks into a shared file; a shortcut sheet on the dashboard.

## 17. Acceptance checklist

Layout and chrome
- [ ] Top bar: «كل الكتب», status chip (pulsing only while active), progress bar, poll pill, exactly one
      primary button per state (§2.1), «⋯» menu with copy / guides / rerun / all books.
- [ ] Sticky toolbar under the topbar: view toggle, five filter chips with live counts, jump field with `G`,
      follow toggle (active only); wraps below 900 px.
- [ ] Summary strip replaces the two cards: five counters, review summary, `<details>` attention list.
- [ ] Banners follow the poll (`status`, `error_headline`, `error_detail`) without a reload.
- [ ] Position chip while scrolling; jump lands below the toolbar; `#sheet-N` on load works.

Sheet and correspondence
- [ ] Scan and text pane are the same aspect box; the text pane is white with a hairline, never scrolls.
- [ ] The text is a flex column with `space-between` inside a block taken from the page's boxes; font size
      from the median line height (`--fac-k`), `clamp(9px, …, 28px)`; lines justified; short lines start-
      aligned; centred headings centred; footnotes under the printed rule in `--color-text-2`.
- [ ] Hovering a line on either side lights the same index on the other with the line number badge;
      clicking a final line opens the review page.
- [ ] Skeleton bars sit at the detected line positions; 14 default bars before any geometry.

Generation
- [ ] No ASCII glyphs anywhere; veiled letters are same-family Arabic letters; word length never changes;
      the first letter is never veiled.
- [ ] Every word returns to Tesseract's exact string (≥ 78 % of its cycle) and drifts again; ≤ 40 veiled
      words per sheet; veil enters and leaves as an opacity fade (.85 ↔ .45).
- [ ] One reading cursor per sheet: scan band (accent-soft) and text sheen (gray ink, right to left) on the
      same line; the lit line's words are exact and full strength.
- [ ] Final text lands with a right-to-left wave (600–1400 ms), `decode-land` wash, amber underline on
      unresolved words; nothing moves afterwards.
- [ ] Effects run only on near sheets while the page is processing and the book is active; a provisional
      sheet of an inactive book is static gray with «نص مبدئي».
- [ ] Reduced motion: static everywhere, final at once.

States and grid
- [ ] Every state of §6 renders as specified (uploaded, preprocessed, layout_done ± text, ocr_done,
      reviewed, error with inline retry, excluded with restore, final with no lines).
- [ ] End of processing: no reload, effects stop, primary swaps, toast «اكتملت المعالجة · X صفحة» with
      «ابدأ المراجعة».
- [ ] Grid tiles use the muted palette, marks and footer of §7; effects paused off-screen.

Ease of use
- [ ] Filters hide/show shells and tiles with counts, persisted; empty message shown.
- [ ] Follow toggle scrolls to the latest progressed page, at most once per poll, off on user scroll.
- [ ] Keyboard map of §8 works and never fires inside inputs.
- [ ] Per-sheet «نسخ» and book copy work with the «تم النسخ» toast.

Performance
- [ ] 800-page book: static shells, poll `apply()` < 8 ms, compact payload ≤ 150 B per page, ≤ 6 mounted
      sheets, ≤ 1 ms per frame in the loop, no layout reads in the loop, no scrollbar jumps.
- [ ] `node --check` passes; `npm run build:css` succeeds; `core/test_theatre.py` and `books/tests.py` pass.

Backend
- [ ] `api:book_sheets` carries `footnote_y`, `median_line_h`, `regions`, `provisional_lines`, `lines[].id`,
      `lines[].bbox` exactly as §12.1, within 5 queries.
- [ ] `api:book_progress` carries `error_headline`/`error_detail` and honours `?compact=1` (§12.2);
      `book_dashboard` config carries `statusLabels`, `statusDots`, `urls`.
- [ ] The page-detail text panel (`templates/ocr/_text_panel.html` + `ocr.js`) still works with the new
      decode behaviour (provisional → wave → final, static on error).
