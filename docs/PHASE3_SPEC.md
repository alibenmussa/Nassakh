# Phase 3 specification — processing theatre and review screen

Single source of truth for the agents building Phase 3. Read first: `docs/PHASE2_SPEC.md` (conventions §0,
models §4), `docs/DECISIONS.md` (D1–D26), `DESIGN.md`, `docs/RUNBOOK.md`. When this file and earlier specs
disagree, this file wins. **Focus of this phase: UI/UX, motion and the "wow" of watching a scan turn into text.**

## 0. Conventions (unchanged from Phase 2)

Django 6.1, function-based views, thin views, logic in `services.py` (typed, docstrings), DRF `@api_view` in
`api.py`, Celery tasks in `tasks.py`. Templates + Alpine.js + Tailwind v4, no CDN, no SPA. Arabic RTL UI,
logical CSS, Western digits in the UI, never strip diacritics from stored text. Tests on SQLite
(`nassakh/settings_test.py`, Celery eager). Do not commit. Never edit `docs/PLAN.md`, `docs/DECISIONS.md`,
`docs/PHASE2_SPEC.md`, `docs/PHASE3_SPEC.md`, `playground/**`.

Shared scaffolding already exists (do not recreate): `static/src/js/decode.js`, `static/src/js/review.js`,
`static/src/components/theatre.css`, `static/src/components/review.css` (all empty, imported/loaded by
`static/src/app.css` and `templates/base.html`: `decode.js` loads after `ui.js` and before `books.js`, `review.js`
after `ocr.js`), and a placeholder `templates/review/review.html`.

## 1. Motion policy (owner decision D24)

DESIGN.md §8 still governs the application chrome (120–200 ms, opacity/transform, ease-out, no bounce).
**Exception, explicitly requested by the owner:** content that is being processed may animate continuously —
the decode effect on provisional text, the scan sweep on pages being cleaned, the line-reading shimmer while OCR
runs. Rules for the exception: it lives only on content in a processing state, it stops the moment that state
ends, only elements in (or near) the viewport animate, one shared `requestAnimationFrame` loop drives all
effects (throttled to ~24 fps), and `prefers-reduced-motion: reduce` replaces every effect with a static state
(provisional text shown plainly in `text-text-3`, no sweep, no shimmer). Resolution moments (final text arriving,
a word resolved, a page approved) are short, purposeful transitions (≤ 600 ms).

## 2. The decode effect ("generation") — `static/src/js/decode.js` (owner decision D23)

Tesseract's text is inaccurate; shown plainly it gives a bad first impression. While a page's `text_state` is
`provisional`, its text must look like it is being generated:

- The text is rendered word by word. Each word oscillates between **noise** and **Tesseract's letters**: a word
  is partly or fully replaced by glyphs drawn from a noise set (`@ # $ % ^ & * ! ? § ¤ ~ + =` mixed with Arabic
  letters `ا ب ت ث ج ح خ د ذ ر ز س ش ص ض ط ظ ع غ ف ق ك ل م ن ه و ي`), then drifts back towards its Tesseract
  letters, back and forth, with a per-word phase offset so the paragraph shimmers rather than blinks. It never
  settles fully on the Tesseract text while the page is still provisional (at most ~60 % of a word's letters are
  "real" at the peak), so nobody mistakes it for the result.
- **Arabic shaping:** never wrap single Arabic letters in their own elements (that breaks letter joining). Mutate
  the whole word's string inside one text node per word; the remaining real letters still join.
- When the page's final text arrives, run a **resolve wave** in reading direction (right to left, then line by
  line): each word stops scrambling and lands on its final text, with a brief highlight fade (≤ 400 ms per word,
  staggered so a page resolves in ~1–1.5 s). Uncertain final words land with their amber underline.
- API (plain script, no Alpine dependency):
  ```js
  window.NassakhDecode = {
    attach(el, { text, mode }),   // mode: 'noise' (processing, no text yet) | 'provisional' (Tesseract text)
    update(el, { text }),         // new provisional text for an attached element
    resolve(el, { lines, onDone }), // lines = final lines [{tokens:[{t, conf, res}]}]; plays the wave, then renders final
    detach(el),                   // stop and clean up
    reducedMotion: bool,
  }
  ```
  One shared rAF loop; elements outside the viewport (IntersectionObserver) are paused. Used by the stacked view
  (§3) and by the page-detail text panel (`templates/ocr/_text_panel.html`, replacing the plain faded text).
- `mode: 'noise'`: a page with no text yet (preprocessing / layout) shows a few lines of pure noise at low
  opacity sized to the page's detected line count, so the sheet is never an empty box.

## 3. Book view modes on the dashboard (owner decision D25)

The pages section of `templates/books/detail.html` gets a segmented control **«صفحات | شبكة»** (persisted in
`localStorage`, default «صفحات»):

- **صفحات (default): stacked sheets like a PDF viewer.** Pages one under another, each a "sheet": the scan on the
  start side, a paper-like text column on the end side (stacked vertically below 900 px). While a page is being
  processed the scan shows a soft **scan sweep** (a light band moving down) until it is cleaned, then crossfades
  (existing 200 ms) from the scan preview to the cleaned image; while OCR runs, the detected line boxes light up
  one after another in reading order (**line-reading shimmer**). The text column shows the decode effect
  (§2) and resolves into the final lines when the page finishes. A small header per sheet: page number, status
  dot + label, printed number (if any), uncertain-word count, actions «مراجعة» (link to the review screen) and
  «نسخ». Sheets are placeholders with the right aspect ratio until they come near the viewport
  (IntersectionObserver, rootMargin ≈ 1500 px); only near-viewport sheets fetch their data and animate, so an
  800-page book stays smooth. Use `content-visibility: auto` on sheets.
- **شبكة: small processing cards, no text.** The existing tiles, animated: scan sweep while cleaning, crossfade
  to the cleaned thumbnail, a thin progress ring or bar per card for its stage, reviewed check, uncertain-word
  badge. No Tesseract text in this mode.
- Dashboard review summary (new): «مُراجَعة X من Y صفحة», total uncertain words, and a primary action
  «الصفحة التالية للمراجعة» (link `review:next`) once any page is `ocr_done`.

## 4. Review screen (Phase 3 core) — `templates/review/review.html`, `static/src/js/review.js`

URL `/books/<book_id>/review/<number>/` (name `review:page`); `/books/<book_id>/review/next/` (name
`review:next`) redirects to the first page with status `ocr_done` that is not reviewed (after the current
`?after=<number>` if given), or back to the dashboard with a toast «لا صفحات بانتظار المراجعة».

Layout: top bar with book title, «صفحة N من M» (isolated with `<bdi>`), resolved-words progress
(«حُسمت X من Y كلمة» with an animated counter and a 4 px bar), save status chip, and the primary action
«اعتماد الصفحة». Main area split: **scan** on the start side, **lines** on the end side (a toggle swaps sides,
persisted in `localStorage`). A thin **filmstrip** of page thumbnails at the bottom (review state per page:
reviewed check / uncertain count), lazily loaded, current page centred.

Interactions (all must work with mouse and keyboard; RTL-correct):
- **Linking both ways.** Hover/focus a word → its box glows on the scan (soft accent glow, 150 ms) and the scan
  pans smoothly to keep it in view; the current line gets a faint band across the scan. Hover the scan → the
  word under the cursor (bbox hit-test) is highlighted in the lines column. Boxes are in gray-image pixel space
  (`image.width/height`); scale to the displayed image size.
- **Uncertain words.** The Phase 2 readings popover becomes actionable: options «النموذج الأول / النموذج الثاني /
  Tesseract» (only the ones present), each with its key hint 1/2/3, plus a small input «تصحيح» for typing.
  Choosing resolves the word: the amber underline morphs into a brief check and fades (≤ 400 ms), the counter
  ticks, focus moves to the next unresolved word.
- **Keyboard.** `Tab` / `Shift+Tab` next / previous unresolved word (scan follows), `1`/`2`/`3` choose a reading,
  `Enter` accept the current reading (choice `primary`), type to start a correction (opens the input prefilled),
  `Esc` close, `E` edit the whole line, `Alt+Enter` insert a line below, `Ctrl/Cmd+Z` undo, `A` approve page,
  `N` next page needing review, `ArrowLeft` next page / `ArrowRight` previous page (RTL reading direction),
  `+`/`-`/`0` zoom the scan. A «?» button shows the shortcut sheet (a small modal).
- **Line editing.** Double-click a line or `E`: an inline plain-text editor over the line (same font), `Enter`
  saves, `Esc` cancels. «إدراج سطر» below the current line (for text a model skipped). Delete a manual or
  garbage line from a line menu, with an undo toast «حُذف السطر · تراجع».
- **Autosave + undo.** Every action is sent at once; the chip shows «يحفظ…» → «محفوظ» (quiet), or
  «تعذّر الحفظ · إعادة المحاولة». Undo calls the server (§5) and re-renders from the returned payload.
- **Approve.** «اعتماد الصفحة» (`A`): if unresolved words remain, a small dialog «بقيت N كلمة غير محسومة. اعتماد
  الصفحة رغم ذلك؟» with «اعتماد» / «متابعة المراجعة». On success: a short stamp animation (check mark scales in,
  400 ms), then the next page needing review slides in from the reading direction (next page from the left in
  RTL), no full reload flash (fetch the next payload and swap; update the URL with `history.replaceState`).
- **Scan viewer.** Fit-to-width by default, zoom with wheel/pinch and buttons, drag to pan, tabs «المعالَجة /
  الأصل» (display vs scan image).
- **Designed states.** Loading skeleton, a page not yet OCR'd (show the decode effect and «النص قيد التعرّف»),
  a page in error (Arabic headline, retry link to the dashboard stage), read-only mode when `can_edit` is false.

`review:page` view context: `book`, `page`, `prev_url`, `next_url`, and `config` rendered with
`{{ config|json_script:"review-config" }}` where `config` = `review_payload(page, user)`:

```json
{
  "page": {"id": 7, "number": 3, "book_id": 1, "status": "ocr_done", "status_label": "…", "is_reviewed": false,
           "text_state": "final", "printed_number": "21", "error": "", "error_from": ""},
  "book": {"id": 1, "title": "…", "total_pages": 120, "reviewed_pages": 14, "unresolved_total": 380},
  "image": {"display_url": "/media/…/display.webp", "scan_url": "/media/…/original.png", "width": 1106, "height": 1634},
  "regions": [{"id": 3, "kind": "body", "label": "المتن", "bbox": [0, 0, 1106, 1270]}],
  "lines": [{"id": 51, "order": 0, "region_id": 3, "region_kind": "body", "bbox": [x0, y0, x1, y1],
             "text": "…", "ocr_text": "…", "is_manual": false, "is_reviewed": false, "n_low": 2,
             "tokens": [{"t": "…", "alt": "…", "tess": "…", "conf": "low", "digit": false, "bbox": [..], "res": null}]}],
  "counts": {"low_total": 14, "unresolved": 9, "resolved": 5},
  "labels": {"primary": "Qari v0.3", "secondary": "Qari v0.2"},
  "nav": {"prev_url": "…", "next_url": "…", "next_review_url": "/books/1/review/next/?after=3", "dashboard_url": "…"},
  "urls": {"payload": "/api/pages/7/review/", "resolve": "/api/lines/__id__/resolve/", "edit": "/api/lines/__id__/edit/",
           "delete": "/api/lines/__id__/delete/", "insert": "/api/pages/7/lines/", "undo": "/api/pages/7/undo/",
           "approve": "/api/pages/7/approve/", "reopen": "/api/pages/7/reopen/", "filmstrip": "/api/books/1/filmstrip/"},
  "can_edit": true
}
```
Token `res`: `null` (unresolved when `conf == "low"`), `"primary" | "secondary" | "tess" | "typed" | "chooser"`.
A token is **unresolved** when `conf == "low"` and `res is null`. `Line.n_low` = unresolved count of the line.

## 5. Backend (new app `review` + small changes elsewhere)

Models:
- `review.LineRevision`: `page` FK(CASCADE, related_name `revisions`), `line` FK(ocr.Line, null, SET_NULL),
  `action` (choices `resolve` «حسم كلمة», `edit` «تعديل سطر», `insert` «إدراج سطر», `delete` «حذف سطر»,
  `approve` «اعتماد», `reopen` «إعادة فتح»), `before` JSON(null) / `after` JSON(null) (line snapshots:
  order, region_id, bbox, text, tokens, is_manual, is_reviewed, n_low; for approve/reopen the page status fields),
  `user` FK(null), `created_at`, `undone` Bool(False). Ordering `-created_at, -id`.
- `ocr.Line.is_manual` BooleanField(False) (inserted by a reviewer), migration in `ocr`.
- `books.Page.n_unresolved` PositiveIntegerField(0), migration in `books`; kept up to date by `finalize_page`
  and every review service (sum of `Line.n_low`).

Services (`review/services.py`, all `@transaction.atomic`, each records a `LineRevision`, recomputes the line's
`n_low`, the page's `n_unresolved` and rebuilds `page.final_text` from the lines — body, blank line, footnotes;
Western digits per D6; printed page number never re-enters the text):
- `review_payload(page, user) -> dict` (§4 shape).
- `resolve_token(line, index, choice, text=None, user=None) -> Line` — choice `primary` keeps `t`; `secondary`
  sets `t = alt`; `tess` sets `t = tess`; `typed` sets `t = text` (stripped, non-empty, single word or a short
  phrase). Sets `res`. Raises `ReviewError` (Arabic message) on a bad index/choice/missing alternative.
- `edit_line(line, text, user=None) -> Line` — re-tokenise on whitespace, align the new tokens to the old ones
  (`ocr.alignment.align_tokens`): unchanged tokens keep their dict (bbox, conf, res); changed/new tokens become
  `{"t", "alt": null, "tess": null, "conf": "high", "digit": is_digit_token, "bbox": old bbox on a 1:1 replace else
  null, "res": "typed"}`. `ocr_text` never changes. Empty text is rejected (use delete).
- `insert_line(page, after_line_id | None, text, user=None) -> Line` — `is_manual=True`, region of the line
  above (or the first body region), tokens typed, orders shifted.
- `delete_line(line, user=None) -> int` — returns the deleted id; orders compacted.
- `undo_last(page, user=None) -> dict` — reverts the newest non-undone revision of the page (restores snapshots,
  re-inserts a deleted line with its old order, reverts approve/reopen), marks it undone, returns
  `review_payload`. Raises `ReviewError` «لا شيء للتراجع عنه» when none.
- `approve_page(page, user, force=False) -> dict` — if `n_unresolved > 0` and not force → `ReviewBlocked`
  (carries the count, API answers 409). Else status `reviewed`, `reviewed_by/at`, all lines `is_reviewed`,
  `book.refresh_status()`; returns `{"status", "next_review_url"}`.
- `reopen_page(page, user) -> None` — back to `ocr_done`, lines `is_reviewed=False`.
- `next_page_to_review(book, after_number=None) -> Page | None`.
- `book_review_summary(book) -> dict` — `{"reviewed", "total", "unresolved_total", "next_review_url"}`.
- `filmstrip(book) -> list[dict]` — per non-excluded page `{number, thumb_url, is_reviewed, n_unresolved,
  status, url}` (cheap: one query).

API (`review/api.py`, all in the `api` namespace through `review/urls.py` `api_urlpatterns`, wired into
`nassakh/urls.py`; GETs need login, POSTs need a reviewer role — `proofreader`, `editor` or `admin` — via a new
`core.permissions.CanReview`; CSRF as in Phase 2):
`GET pages/<id>/review/` (`review_payload`) · `POST lines/<id>/resolve/` `{index, choice, text?}` →
`{line, counts, page}` · `POST lines/<id>/edit/` `{text}` → `{line, counts}` · `POST lines/<id>/delete/` →
`{deleted_id, counts}` · `POST pages/<id>/lines/` `{after, text}` → `{line, lines, counts}` (lines = all ids/orders)
· `POST pages/<id>/undo/` → review payload · `POST pages/<id>/approve/` `{force}` → 200 `{status,
next_review_url}` or 409 `{unresolved, message}` · `POST pages/<id>/reopen/` · `GET books/<id>/filmstrip/`.
`counts` = `{"line_n_low", "page_unresolved", "page_low_total", "book_unresolved_total"}`. Errors: 400 with
`{"message": Arabic}`.

Views (`review/views.py`, `review/urls.py` app_name `review`, mounted at `books/`): `review_page`
(`<int:book_id>/review/<int:number>/`, login required) and `review_next` (`<int:book_id>/review/next/`).

Books/dashboard additions (`books/services.py`, `books/api.py`):
- `page_tile` gains `n_unresolved`, `is_reviewed`, `review_url`; `book_progress` gains `review` =
  `book_review_summary(book)`.
- New `GET /api/books/<id>/sheets/?from=<n>&to=<n>` (name `api:book_sheets`, max 40 pages per call) for the
  stacked view: per page `{id, number, status, status_label, text_state, provisional_text, lines: [{order,
  region_kind, tokens: [{t, conf, res}]}], display_url, scan_url, thumb_url, width, height, line_boxes (as
  0..1 ratios of width/height), n_unresolved, is_reviewed, printed_number, url, review_url}`.

Word-chooser hook (owner decision D26): `ocr/chooser.py` with
`choose_word(token: dict, context: dict) -> str | None` returning `None` (placeholder for a future small
classifier), called from `ocr.services.finalize_page` for every low-confidence token that has candidates
(`t`, `alt`, `tess`); when it returns a string that is one of the candidates, set `t` to it and `res =
"chooser"` but keep `conf = "low"` (still reviewable). Guarded by `settings.NASSAKH["WORD_CHOOSER"]`
(default `"none"`). No UI.

## 6. Ownership for the build agents

| Agent | Owns (may edit) |
|---|---|
| backend (Opus) | every `*.py` file, new app `review/` (python), migrations, `nassakh/urls.py`, `docs/RUNBOOK.md`, tests |
| theatre UI (Fable) | `static/src/js/decode.js`, `static/src/js/books.js`, `static/src/js/ocr.js`, `static/src/components/theatre.css`, `books.css`, `ocr.css`, `templates/books/**`, `templates/ocr/**`, a frontend test file `core/test_theatre.py` |
| review UI (Fable) | `static/src/js/review.js`, `static/src/components/review.css`, `templates/review/**`, a frontend test file `core/test_review_ui.py` |
| integrator (Opus) | anything, to join the parts |

Frontend agents build against the payload shapes above (use fixture JSON in their tests; the backend is built in
parallel). Both may call `window.Nassakh.copyText`, `Alpine.store('toast')` and the CSRF helper from `ui.js`
(read it; do not edit `ui.js` — ask the integrator in `open_issues` if something is missing). Rebuild CSS with
`npm run build:css`; check JS with `node --check`.
