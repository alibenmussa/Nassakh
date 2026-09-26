# Phase 7 design — «التخطيط» first, then «المعالجة»

Status: design for the owner's approval, 2026-09-26. The next decision number was D64. Every `file:line` citation was read on `main` at 02f108a. The baseline `books/tests.py` and `processing/tests.py` pass on SQLite. A copy of this document is at `/private/tmp/claude-501/-Users-alibenmussa-PycharmProjects-me-Nassakh/5e011468-4f0f-4097-b8c6-1a1084b7f4be/scratchpad/phase7/PHASE7_LAYOUT_DESIGN.md`. No project files were changed.

## 0. The request and the answer

**Owner (2026-09-26).** Today «بدء المعالجة» does two things; split them:
- «التخطيط»: get the pages, prepare them to gray and b&w, detect the regions.
- «المعالجة»: التفريغ, i.e. OCR and everything after it.

After upload the user takes a quick look at the regions. Detection is usually right, so one click goes on. He fixes a page, or applies a guide to all pages, only when needed.

**Clarifications:**
- Build on what exists: book guides, per-page overrides, `needs_guides`.
- The dashboard shows the layout only in the first step. Once «بدء المعالجة» is clicked, it is exactly today's dashboard.

**Names:** the first stage is «التخطيط» and its action is «استخراج الصفحات». Then «بدء المعالجة» starts the stage «المعالجة».

**The answer.**
1. The new-book form's button becomes «استخراج الصفحات». It creates the book and runs today's ingest and preprocessing: split, deskew, crop, clean, gray and b&w, and detection of the footnote rule, the smaller-type block and the page number.
2. The pipeline then stops where the old `needs_guides` pause was: every page is `preprocessed` and the book shows «تم التخطيط».
3. The dashboard is in its «التخطيط» mode. It has the same viewer, filmstrip, grid, filters and keys. Each page shows the prepared image with its regions drawn (running head, text block, footnotes, page number) and the two guide lines. The owner can move a line on one page or set it for all pages. The few pages worth a look are marked.
4. «بدء المعالجة» records the start on the book (`Book.ocr_started_at`) and runs today's `_enqueue_layout`: layout → Tesseract → Qari → numbers pass.
5. From then on the dashboard and every code path are today's. That one field selects them.

Clicks from upload to text stay two: today «إنشاء الكتاب» + «بدء المعالجة», now «استخراج الصفحات» + «بدء المعالجة». The second click now comes after a look at the layout.

A book nobody touches is read exactly as today. At «بدء المعالجة» its regions are derived by the same function, from the same detection, with inert guides.

## 1. Decisions (proposed)

- **D64 — Two stages, an explicit pause, one field.**
  - «التخطيط» = ingest + preprocessing (with detection). It stops with the book in `needs_guides` («تم التخطيط») and every page `preprocessed`.
  - «المعالجة» = regions derived, Tesseract, Qari, finalisation, numbers pass and everything after. It runs only after «بدء المعالجة», which is recorded as `Book.ocr_started_at`.
  - The book status is still derived from the pages (`refresh_status`), plus this field.
  - When the field is set, every function takes exactly today's path.
  - The old rule "the book never waits for guides" (`books/tasks.py:68–70`) is reversed: waiting is the normal, cheap first step.
- **D65 — Names (owner, 2026-09-26).**
  - Stage «التخطيط», action «استخراج الصفحات», then «بدء المعالجة» and the stage «المعالجة».
  - Book status labels change; the stored values stay:
    - `processing` → «قيد التخطيط»
    - `needs_guides` → «تم التخطيط» (was «بانتظار ضبط الأدلة»)
    - `ocr` → «قيد المعالجة» (was «قيد التعرّف على النص»)
  - Page status `preprocessed` → «مُجهَّزة» (was «مُعالَجة», which would now read as "OCR'd").
  - Re-run stage `preprocess` → «تجهيز الصفحات» (was «المعالجة الأولية»).
  - No user-facing text says «الأدلة» any more (§11).
  - These code names stay: `guides`, `needs_guides`, `LayoutGuides`, `guides_override`, `apply_guides`, `Region.Source.GUIDES`, the stage keys and the status values. They are stored in rows and used by URLs, JS and tests; the labels carry the new words.
- **D66 — Pages wait prepared; the regions shown are computed.**
  - No `Region` rows exist during «التخطيط».
  - The dashboard draws each page's regions with `page_region_specs` (`processing/services.py:607`). That is the same function `layout_page` runs at «بدء المعالجة», with the same detection and guides, so what the owner sees is what OCR reads.
  - Guide edits during «التخطيط» only save the guides: nothing is derived and nothing is queued for OCR.
- **D67 — The upload extracts.**
  - «استخراج الصفحات» on the form creates the book and calls `start_processing` in the same request.
  - A book still `uploaded` (queueing failed, or made by a script) shows the same button on its dashboard.
  - The success message states the page range, so a wrong skip value is seen at once.
- **D68 — One place for the guides: the dashboard's «التخطيط» mode.**
  - The guides screen folds into it: `/books/<id>/guides/`, `templates/processing/guides.html`, and `guidesEditor` in `static/src/js/processing.js:30–90`. Its URL redirects to the dashboard.
  - In «المعالجة», the ⋯ item «التخطيط» (was «ضبط الأدلة», `templates/books/detail.html:117–123`) opens the same mode with `?view=guides`. There a change re-reads the changed pages, as `apply_guides` does today.
- **D69 — Editing is the existing guides, made direct.**
  - On a page: move, add or remove its running-head cut and footnote start, or switch its page number off. This writes `Page.guides_override`, now merged key by key, autosaved, with undo.
  - For all pages: the book's `LayoutGuides` from the side panel. That is the running-head cut, the footnote line where none was detected, and the page-number zone where none was detected. Each change is previewed on every page before «تطبيق على كل الصفحات», and can be undone.
  - The first manual book change starts from inert values, so there is no hidden bottom page-number zone.
  - No free rectangles in 7a.
- **D70 — Pages that deserve a look.**
  - Live checks, with nothing stored:
    - a separator line the detector refused
    - footnotes found only from smaller type, or only from the book's line
    - a line cut by a guide
    - text hidden in a running-head or page-number band
    - no lines
    - large skew
  - Separator lines refused as too thick are stored at preprocessing (`Preprocess.rule_candidates`) and offered as «الحاشية تبدأ هنا».
  - Measured on the owner's books (Appendix A): book 25's six footnoted pages are caught, and there is no false suggestion on 173 other pages.
- **D71 — Safety that comes with the split.**
  - OCR tasks do nothing before «بدء المعالجة», so a stale queued chain cannot read a book that is being checked.
  - A queueing failure leaves the book as it was.
  - A page with review work keeps its regions when guides change. Today its footnote lines lose their region, because `Line.region` is `SET_NULL`.
  - The all-pages-failed message keeps matching rows written with the old text.

## 2. What exists and is reused

| Fact | Where |
|---|---|
| Detection happens in preprocessing: line boxes, the footnote rule (strict thickness ≤ 0.3 × line height), the smaller-type block, the page-number box | `processing/pipeline.py:340–378` (rule; thickness 361, 369), `418–445` (block), `576–619` (page number), `790–803` (in `run_pipeline`) |
| Regions are pure geometry from that detection and the guides: full-width bands for running head, body and footnote, plus the page-number box | `processing/services.py:481–543` (`guide_regions`), `557–604` (`page_layout`), `607–616` (`page_region_specs`) |
| Resolution order per page. Footnote: override → detected rule → block → book line (manual guides only). Page number: override → detected → book zone (manual only). Running-head cut: book guides or override | `processing/services.py:557–604` |
| The book guides row (`header_cut`, `footnote_line`, `page_number_zone`, `page_number_height`, `source` auto/manual) and the per-page override JSON (same keys; a present key wins, even null) | `processing/models.py:60–100`, `books/models.py:206`, `processing/services.py:402–409` |
| Applying book guides or a page override re-derives the regions and re-OCRs the changed pages | `processing/services.py:693–734`, `737–755`; API `processing/api.py:42–64` |
| The guides screen: reference page, two draggable lines, zone, stats, "use proposal" | `processing/views.py:15–33`, `templates/processing/guides.html`, `static/src/js/processing.js:30–90`, `static/src/components/processing.css:3–92` |
| `refresh_status` keeps `needs_guides` while pages wait at `preprocessed`, but no code sets it any more | `books/models.py:100–152` (branches 123–128, 142–143), `books/tasks.py:62–83` |
| `start_processing` refuses `needs_guides` as "an earlier version"; `toggle_exclude` has a waiting branch; `_bar_state` paints it amber | `books/services.py:456–457`, `637–642`, `657–658` |
| The chain: ingest → chord(preprocess ×N) → `after_preprocess` → `_enqueue_layout` → per page layout → ocr_fast → ocr_full | `books/tasks.py:25–96`, `books/services.py:472–484` |
| OCR leaves `running_header` and `page_number` out of the text; footnotes are read at 2× and joined after the body | `ocr/services.py:52–53`, `186–194`, `738–809`, `886–963` |
| Assembly: footnote regions give footnote lines, skipped kinds are dropped, everything else is body | `assembly/services.py:166–237` |
| Only `finalize_page` schedules the numbers pass | `ocr/services.py:1374–1376`, `ocr/numbers.py:793–806` |
| Region colours (one hue per kind) and the overlay used on the page detail | `static/src/components/books.css:147–174`, `templates/books/page_detail.html:57–68` |

**Kinds that matter:**
- `body` is read by Qari and Tesseract; it is the text.
- `footnote` is read at 2×, gives footnote lines, and assembly links them.
- `running_header` is read by Tesseract only in the fast pass and never reaches the text.
- `page_number` is voted into `printed_number` and never reaches the text.
- `heading`, `poetry` and `other` exist in `Region.Kind`, but derivation never makes them; they are read like body.

The «التخطيط» mode shows the four derived kinds. A top band is never read into the text, so the running-head band also serves to "ignore the top" (a stamp, a library mark).

## 3. The pipeline split

### 3.1 Where the pause is, and why there

The pause is after preprocessing and before `layout_page`, which is the point `needs_guides` always meant.

Pausing after layout (regions saved, pages `layout_done`) was considered and rejected. It would need region rows rewritten on every guide drag, OCR enqueue guards in every guide path, and a change to the ingest chord's shape.

Pausing before layout keeps the chord, `after_preprocess` and `_enqueue_layout` as they are, and a guide change during the check is only a saved value.

### 3.2 The gate and the book status

`Book.ocr_started_at = DateTimeField("بدء المعالجة", null=True, blank=True)`: null means «التخطيط», set means «المعالجة».

It is set once by «بدء المعالجة» and never cleared. After the start, re-runs behave as today, including re-runs from preprocessing, which go on through OCR.

`Book.refresh_status` (`books/models.py:100–152`) gets one branch in front of today's code:

```python
if self.ocr_started_at is None:                  # «التخطيط» (D64): nothing is read before «بدء المعالجة»
    if counts[ps.UPLOADED]:
        new_status = self.Status.PROCESSING       # «قيد التخطيط»: pages still being prepared
    elif counts[ps.PREPROCESSED] or counts[ps.LAYOUT_DONE]:
        new_status = self.Status.NEEDS_GUIDES     # «تم التخطيط»: waits for «بدء المعالجة»
    else:
        new_status = self._settled(counts, total) # every page failed → error; nothing else can happen here
else:
    ...today's lines 118–145, unchanged (the legacy NEEDS_GUIDES branch stays, unreachable)...
```

`_settled` is today's `pending == 0` block (lines 129–137), extracted into a method.

The early return for a non-derivable error compares against `ALL_PAGES_FAILED_MESSAGES`, which holds both the new text and the old one. Rows written with the old text keep re-deriving (D71).

```
            «استخراج الصفحات»                      «بدء المعالجة»
uploaded ──────────────────► processing ──────► needs_guides ─────────────► ocr ─► ready_for_review ─► reviewing ─► assembled
 «مرفوع»                    «قيد التخطيط»       «تم التخطيط»                «قيد المعالجة»   (today)
                              │  ▲                 │  ▲
                              │  └── re-prepare ◄──┘  │  (page or book «تجهيز الصفحات», re-included page)
                              └── every page failed ─► error («إعادة استخراج الصفحات»)
ocr_started_at: null ─────────────────────────────────┘ set ──────────────────────────────────────────────►
```

`ACTIVE_BOOK_STATUSES` is unchanged (`processing`, `ocr`). The dashboard polls while pages are prepared and stops at «تم التخطيط».

### 3.3 Chains per stage

| Starting stage | «التخطيط» (`ocr_started_at` null) | «المعالجة» (set) — today |
|---|---|---|
| ingest (`start_processing`) | ingest → chord(preprocess ×N) → `after_preprocess(continue_ocr=False)` → pause | ingest → chord → `after_preprocess(continue_ocr=True)` → `_enqueue_layout` |
| `preprocess` (page or book) | `preprocess_page` only; the task refreshes the book | preprocess → layout → ocr_fast → ocr_full |
| `layout`, `ocr`, `ocr_fast`, `ocr_full` | refused: «لم تبدأ المعالجة بعد؛ اضغط «بدء المعالجة» أولًا.» | today |
| «بدء المعالجة» | claim → `start_ocr_task` → `_enqueue_layout` (layout → ocr_fast → ocr_full for each prepared page) | refused («بدأت المعالجة بالفعل.») |

`_stage_signatures(page_id, stage, layout_stage=False)`: with `layout_stage`, the only entry is `"preprocess": [preprocess_page]`. Otherwise today's dictionary (`books/services.py:477–483`) is used unchanged.

### 3.4 «استخراج الصفحات» — `start_processing` (`books/services.py:448–469`)

The allowed states stay the same: `uploaded` and `error`. Changes:
- The `needs_guides` refusal now says «اكتمل التخطيط. اضغط «بدء المعالجة»، أو أعد تجهيز الصفحات من القائمة.»
- `ingest_book_task.delay` is wrapped. If the broker refuses, the status and message go back, and a ValueError says «تعذّر إرسال العمل إلى العامل الخلفي. تأكّد من تشغيل Redis والعامل ثم أعد المحاولة.» Today the book would stay `processing` forever.
- Restarting a book whose «المعالجة» had started still goes through OCR, exactly as today, because its chord gets `continue_ocr=True`.

`book_create` (`books/views.py:27–46`) calls `start_processing` right after `create_book`, unless the book is in error.
- The message becomes «أُنشئ الكتاب «…»، ويُستخرج الآن 7 صفحات من 555 صفحة في الملف.»
- The count comes from `selected_indices` × `pages_per_sheet`; Arabic counting follows `assembly.render.ar_count`.
- If queueing fails, the book stays `uploaded` with the error message, and the dashboard offers «استخراج الصفحات».

### 3.5 «بدء المعالجة» — new `books.services.start_ocr(book)`

```python
def start_ocr(book: Book) -> None:
    """«بدء المعالجة» (D64): record the start and send every prepared page into «المعالجة».

    Only from «تم التخطيط» (`needs_guides`, `ocr_started_at` unset). One conditional UPDATE claims it (status → `ocr`,
    `ocr_started_at` → now), so a double submit or a second tab starts nothing twice; `start_ocr_task` then runs
    `_enqueue_layout`. When the task cannot be queued the claim is undone and ValueError explains it.
    """
```

- **Claim.** `Book.objects.filter(pk=…, status=NEEDS_GUIDES, ocr_started_at__isnull=True).update(ocr_started_at=now, status=OCR, error_message="", updated_at=now)`. If 0 rows change, the answer is «بدأت المعالجة بالفعل.» or «لم يكتمل التخطيط بعد؛ انتظر حتى تُجهَّز كل الصفحات.»
- **New task `books.tasks.start_ocr_task(book_id)`** (default queue):
  1. It lists pages that are already `layout_done` (legacy only).
  2. It calls `_enqueue_layout(book)` unchanged, which takes the `preprocessed` pages.
  3. It calls `run_stage(page, "ocr")` for the pages listed in step 1.
  
  The two sets are disjoint, so no page is enqueued twice. It is a task, like `rerun_book_from`, because an 800-page book means 800 chains.
- «بدء المعالجة» is available only once every page is prepared. While «قيد التخطيط», the button is shown disabled with the title «يُتاح بعد اكتمال تجهيز الصفحات». This keeps the start exact without locks; see open question 3.

### 3.6 While pages are prepared

- **`processing.tasks.preprocess_page`** (`processing/tasks.py:40–71`). After a success or a recorded failure, if the book's `ocr_started_at` is null and its status is not `uploaded`, the task re-reads the book and calls `refresh_status()`.
  - Page re-runs in «التخطيط» have no later task to move the book on, so this is needed.
  - The ingest chord gets a live status for free.
  - If two tasks finish together, the one that reads last sees both commits; a refresh that changes nothing never writes. The chord callback refreshes once more at the end.
- **`after_preprocess(results, book_id, continue_ocr=None)`** (`books/tasks.py:62–83`).
  - It proposes guides when there are none (display only, as today).
  - Then it calls `_enqueue_layout(book)` when `continue_ocr` is true; otherwise it calls `refresh_status()`, which moves the book to «تم التخطيط».
  - `None` comes from chords queued before this change; in that case it decides by `ocr_started_at`.
  - `ingest_book_task` (`books/tasks.py:57–58`) passes `continue_ocr=book.ocr_started_at is not None`.

### 3.7 Re-runs

- **In «التخطيط»:**
  - The book ⋯ menu offers «إعادة تجهيز الصفحات». This is `rerun_book(book, "preprocess")` with unchanged code: status `processing`, pages back to `uploaded`, preprocess-only chains.
  - A page offers «تجهيز الصفحة».
  - `validate_rerun` and `run_stage` refuse the other stages with the message in §3.3.
  - The menus list only `preprocess`: `book_dashboard` and `page_detail_context` filter `rerun_stages`.
  - The page detail's preprocess panel (a synchronous re-run, `processing/services.py:217–246`) works as today. There are no regions yet, so nothing is re-derived.
- **In «المعالجة»:** today's behaviour, all five stages, for the book and for a page.

### 3.8 Excluded pages

`toggle_exclude` (`books/services.py:616–647`): the waiting branch (lines 637–642) is keyed on `ocr_started_at is None` instead of `status == NEEDS_GUIDES`.
- A re-included page that is already prepared joins the waiting pages.
- A re-included page that is not prepared yet is only prepared; its task refreshes the book.
- Once «المعالجة» has started, today's behaviour applies.

Excluded pages never count and never carry a doubt. In the «التخطيط» mode they show as today's excluded sheet: grayscale, «مستثناة», and «إعادة الصفحة إلى الكتاب».

### 3.9 Guide changes in each stage

| Service | «التخطيط» | «المعالجة» |
|---|---|---|
| `apply_guides` (form POST, full) and new `apply_book_guides` (partial, §7) | save the guides; nothing derived, nothing queued | today: re-derive unapproved pages, re-OCR the changed ones |
| `set_page_guides_override` (replace) and the new merge mode | save the override | today: re-derive, re-OCR if changed |
| `_derive_regions` | not called | keeps the regions of a page with review work (D71) |

### 3.10 OCR and layout tasks and the gate

In `ocr/tasks.py:32–68`, `_run_stage` returns early, with a log line, when `page.book.ocr_started_at` is null. `processing.tasks.layout_page` does the same.

This blocks nothing legitimate. After the migration every book with OCR history has the field set, and new books reach OCR only through `start_ocr`.

### 3.11 Unchanged

These are untouched:
- the numbers pass, scheduled by `finalize_page`, which only runs in «المعالجة»
- Tesseract and Qari selection, and line building
- review, assembly, the manuscript, the book page and exports
- the `ACTIVE_*` sets, the compact poll payload and the Celery queues

Born-digital books take the same pause. Their text comes from the PDF layer whatever the regions are (`ocr/services.py:807–809`), so the banner says so (open question 2).

### 3.12 Existing books (data migration, §6)

| Status before | `ocr_started_at` after | Dashboard | Primary |
|---|---|---|---|
| `uploaded` | null | «التخطيط» mode, empty state | «استخراج الصفحات» |
| `processing`, no OCR run, no page past `preprocessed` (preparing under the old flow) | null | «التخطيط» mode; the queued chord's callback (new code) pauses | disabled «بدء المعالجة», then enabled |
| `processing` / `error` with OCR runs or a page past `preprocessed` (re-run or failure under the old flow) | set | today's | today's |
| `needs_guides` (legacy, pages `preprocessed`) | null | «التخطيط» mode, «تم التخطيط» | «بدء المعالجة» |
| `ocr`, `ready_for_review`, `reviewing`, `assembled` | set | today's, unchanged | today's |
| `error` from ingest (no runs) | null | «التخطيط» mode, error banner | «إعادة استخراج الصفحات» |

The owner's database today has 25 books, all `ready_for_review`, `reviewing` or `assembled`. All of them get the field set, so their dashboards stay exactly as they are.

## 4. The «التخطيط» mode of the dashboard

### 4.1 When it shows

- It always shows while `ocr_started_at` is null.
- In «المعالجة» it shows only through the ⋯ item «التخطيط» (`/books/<id>/?view=guides`).
- The server chooses the mode (`book_dashboard(book, view)`), so today's markup is rendered untouched when the mode is off.
- «بدء المعالجة» is a form POST; its redirect loads today's dashboard.
- If a poll reports that «المعالجة» started elsewhere (another tab), a banner offers «بدأت المعالجة · تحديث الصفحة».

### 4.2 Viewer (default view, «تم التخطيط», RTL: start = right)

```
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ الحوليات الليبية            ‹ كل الكتب   ● تم التخطيط  ▰▰▰▰▰▰▰▰   [ ▶ بدء المعالجة ]   ⋯      │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ [صفحات|شبكة] (الكل 7)(تستحق نظرة 6)(بضبط خاص 0)   [رمادية|أبيض وأسود]  صفحة 1 من 7  [إلى صفحة… G] │
├─────────────────────────────────────────────────────────────────┬────────────────────────────┤
│ ⓘ جُهّزت الصفحات واكتُشفت مناطقها. ألقِ نظرة واضبط ما يلزم، ثم    │ التخطيط                     │
│   اضغط «بدء المعالجة».  ستّ صفحات تستحق نظرة · عرضها               │ ● جُهّزت 7 من 7 صفحات        │
│                                                                 │ ▲ 6 صفحات تستحق نظرة         │
│ صفحة 1  [خط فاصل لم يُعتمد]                       التلقائي  استثناء│ ─────────────────────────── │
│         ┌─────────────────────────────────────────┐    [+ ترويسة]│ لكل الصفحات                  │
│   →     │┌متن────────────────────────────────────┐│        ←     │ ☐ ترويسة أعلى كل الصفحات      │
│ السابقة ││ ─── ──── ─────── ─── ──────── ────     ││     التالية  │ ☐ حاشية حيث لم تُكتشف          │
│         ││ ─── ──── ─────── ─── ──────── ────     ││              │ رقم الصفحة حيث لم يُكتشف:      │
│         ││ ...                                    ││              │   [بدون|أعلى|أسفل]             │
│         ││        ┄┄┄┄┄┄┄┄┄┄ خط فاصل؟ [الحاشية تبدأ هنا]│         │ اكتُشف خط حاشية في 0 من 7…    │
│         ││ (1) ─── ──── ─────── ─── ─────         ││              │ خطوط فاصلة مقترحة في 5 صفحات  │
│         ││ (2) ─── ──── ────                      ││              │   · اعتمادها كلها              │
│         │└────────────────────────────────────────┘│              │ ─────────────────────────── │
│         │               ┌رقم الصفحة┐                │              │ ▭ متن ▭ حاشية ▭ ترويسة        │
│         │               └──────────┘  [+ حاشية]     │              │ ▭ رقم الصفحة  ┄ خط مقترح      │
│         └─────────────────────────────────────────┘              │ ─────────────────────────── │
│                                                                 │ الصفحات 7                    │
│                                                                 │ [1▲][2 ][3▲][4▲][5▲][6▲][7▲] │
└─────────────────────────────────────────────────────────────────┴────────────────────────────┘
```

The viewer shows one page at a time, fitted to the stage height (review's default fit), with no text pane. The image takes the stage width it needs and is centred. Turns, the filmstrip, jump, filters, `#sheet-N` and the session memory work as in D33.

### 4.3 Grid view

```
┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐
│▒متن▒▒│ │▒▒▒▒▒▒│ │▒▒▒▒▒▒│ │▒▒▒▒▒▒│   bands drawn on the prepared thumbnail
│▒▒▒▒▒▒│ │▒▒▒▒▒▒│ │▒▒▒▒▒▒│ │▒▒▒▒▒▒│   (▒ text block, ▓ footnotes, ▪ page number,
│▓حاشية│ │▒▒▒▒▒▒│ │▓▓▓▓▓▓│ │▒▒▒▒▒▒│    amber dot = worth a look)
│  ▪  ●│ │  ▪   │ │  ▪  ●│ │  ▪   │
└──1───┘ └──2───┘ └──3───┘ └──4───┘
```

An outlier stands out at a glance. A plain click opens the viewer on that page, as today.

### 4.4 What differs from today's dashboard (only in this mode)

| Part | «التخطيط» mode |
|---|---|
| Status chip | «قيد التخطيط · X من Y صفحة» (X = prepared) while preparing; «تم التخطيط» after |
| Progress bar | the prepared share; green at «تم التخطيط» (`_bar_state`: `needs_guides` → success; the dot is success too) |
| Primary | `uploaded` «استخراج الصفحات» · `processing` «بدء المعالجة» disabled · `needs_guides` «بدء المعالجة» · `error` «إعادة استخراج الصفحات» · in «المعالجة» (`?view=guides`) «العودة إلى الصفحات» |
| ⋯ menu | «إعادة تجهيز الصفحات» and «كل الكتب» only (no copy, manuscript, export, convert or OCR re-runs) |
| Toolbar | «صفحات · شبكة» as today; chips «الكل · تستحق نظرة · بضبط خاص» (their own saved filter); image toggle «رمادية · أبيض وأسود» (`nassakh.guidesImage`); no follow toggle |
| Banner | the sentence of §4.2, replacing the `needs_guides` banner (`detail.html:203–206`); for born-digital books, also «للكتاب طبقة نصية يُؤخذ منها نصّه، فلا تغيّر المناطق النص.» |
| Sheet head | the title, doubt badges (amber), «بضبط خاص»; actions «التلقائي» (when the page has an override) and «استثناء» / «إعادة إلى الكتاب»; the page status label only when the page is not prepared («مرفوعة» with the sweep, «خطأ» with the retry) |
| Sheet body | `<template id="sheet-guides">`: the prepared image, bands, guide lines, grips and suggestion (§5); no decode handle |
| Side panel | the «التخطيط» summary, «لكل الصفحات», suggestions, the legend, and the filmstrip (thumbs with bands and an amber dot); today's counters, review, manuscript, book and attention blocks are not rendered |
| End of preparation | toast «اكتمل التخطيط · 7 صفحات», with no action, since the primary is now enabled |

### 4.5 Pages that deserve a look (D70)

`processing.services.layout_doubts` computes these per page from `Preprocess`, the effective guides and the bands. Nothing is stored. The chip «تستحق نظرة» keeps pages that have a doubt or are in error.

| Code | Rule | Label | How it goes away |
|---|---|---|---|
| `rule_suggested` | a refused separator line exists (`rule_candidates`) and the page has no footnote region | «خط فاصل لم يُعتمد» | «الحاشية تبدأ هنا» or «ليس خطًّا فاصلًا» (either one sets the page's `footnote_line`) |
| `footnote_from_type` | the footnote start comes from the smaller-type block | «حاشية من حجم الخط» | any explicit footnote choice on the page |
| `footnote_from_book` | the footnote start comes from the book's fallback line | «حاشية من الضبط العام» | same |
| `line_cut` | the running-head cut or the footnote start lies inside a detected line box (2 px margin) | «خط يقطع سطرًا» | move the line (snapping avoids this) |
| `text_hidden` | a line box at least 50 % of the text-block width lies at least 60 % inside a running-head or page-number band | «سطر من المتن خارج المتن» | move or remove the band |
| `no_lines` | `n_lines == 0` | «لا أسطر في الصفحة» + «استثناء الصفحة» | exclude the page, or leave it |
| `large_skew` | `abs(angle) > 3°` (`pipeline.LARGE_SKEW_DEG`) | «ميلان كبير» | the page detail's rotation |

On the owner's 217 included pages these rules give the following. The candidates were computed by a script, since the field is new.
- 5 × `rule_suggested` and 1 × `footnote_from_type`, all in book 25, all six real.
- 1 × `no_lines` in book 19. The owner had already excluded 12 other pages without lines, 11 of them in book 19.
- Nothing else.

Untouched books show 0–1 pages to look at.

### 4.6 Region drawing

- Bands use the page detail's vocabulary: `.region-box` + `.region-<kind>`.
  - Hues: body 215, footnote 140, running head 45, page number 26.
  - 1.5 px outline, 8 % fill, a label chip at the top start corner.
- Image coordinates are physical: a `dir="ltr"` layer with `left/top` percentages, as in `pageDetail.boxStyle`.
- Guide lines are 2 px in their band's hue, with the guides screen's pill handle («ترويسة 6.2%», `.guide-handle`).
- The suggestion is a dashed 1.5 px line in the footnote hue, with its chip.
- The legend is in the side panel.
- Under `prefers-reduced-motion`, the 150 ms band transitions are off.

## 5. Editing the guides (D69)

### 5.1 On one page

- **Move.**
  - The running-head cut and the footnote start are draggable lines (`role="slider"`, `aria-orientation="vertical"`). The two adjacent bands follow live.
  - Dragging snaps to the middle of the gap between detected lines, and to the suggested line, within max(6 screen px, 0.8 × median line height). Alt / Option drags freely.
  - On release, the value is saved for this page (`guides_override`, merge), with the toast «حُفظ لهذه الصفحة · تراجع».
- **Add.**
  - A page without a running head shows the grip «+ ترويسة» at its top edge. A page without footnotes shows «+ حاشية» at its bottom edge.
  - Drag a grip in, or click it. The running-head line lands in the gap after the first detected line. The footnote line lands on the suggestion if there is one, otherwise in the gap nearest 80 % of the height.
- **Remove and other actions.** Each band's chip opens a small menu:
  - «إزالة من هذه الصفحة» sets the key to null, meaning "no running head / no footnotes here".
  - «تطبيق على كل الصفحات…» loads this page's value into the side panel's draft (§5.2). This is offered for the running head and the footnote line only.
  - The page-number box's chip offers «ليس رقم صفحة», which sets `page_number_zone: "none"` for this page.
- **Suggestion.** «الحاشية تبدأ هنا» sets `footnote_line` to the suggested line; «ليس خطًّا فاصلًا» sets it to null.
- **Back to automatic.** «التلقائي» in the sheet head clears the page's override, with the undo toast «عادت الصفحة إلى التلقائي · تراجع».
- **In «المعالجة»** (`?view=guides`) a page change re-reads the page, so a drag does not autosave.
  - The line turns dashed, and a bar under the page offers «حفظ وإعادة التعرّف على الصفحة · إلغاء».
  - Approved pages and pages with review work are shown locked: «معتمدة: لا تتغيّر» / «فيها تصحيحات مراجعة».

### 5.2 For all pages (side panel «لكل الصفحات»)

The panel holds the guides screen's three controls, in compact form:
- «ترويسة أعلى كل الصفحات»: a checkbox, «% من الأعلى», and «من هذه الصفحة».
- «حاشية حيث لم تُكتشف»: a checkbox, a percentage, and «الموضع الوسيط 80.1%» from `guides_stats`.
- «رقم الصفحة حيث لم يُكتشف»: بدون · أعلى · أسفل, and «% ارتفاع المنطقة».

It also shows the detection line («اكتُشف خط حاشية في 44 من 81 صفحة، وحاشية بخط أصغر في 1، ورقم صفحة في 72.») and «إزالة الضبط العام», which returns the book to automatic guides.

Any change is a draft. The stage shows it dashed on the page on screen, the filmstrip and grid draw the draft's bands, and a preview box appears:

```
┌ معاينة ──────────────────────────────────────────────┐
│ يغيّر هذا 118 صفحة.                                   │
│ ▲ في 3 صفحات تغطّي منطقةٌ سطرًا من المتن: 12 · 45 · 77  │   (page numbers are links: the viewer turns there)
│ ☐ صفحتان بضبط خاص تبقيان كما هما · تطبيقه عليهما أيضًا  │
│ (in «المعالجة»: سيُعاد التعرّف على نص 118 صفحة.         │
│  4 صفحات معتمدة أو فيها تصحيحات مراجعة لا تتغيّر.)      │
│ [ تطبيق على كل الصفحات ]   [ إلغاء ]                   │
└──────────────────────────────────────────────────────┘
```

- «تطبيق على كل الصفحات» saves, with the toast «طُبّق على 118 صفحة · تراجع». «إلغاء» or Esc drops the draft.
- While a draft exists, per-page editing on the stage is paused.
- Applying a value that came from a page's chip also drops that key from that page's override, since it now equals the book value.
- A page's own override for the same key still wins, as today. If the box is ticked, those keys are removed from those pages' overrides, and undo restores them.

**Inert start.** The first manual change to a book whose guides are `auto` starts from `{header_cut: None, footnote_line: None, page_number_zone: "none", page_number_height: 0.06}`, then applies only the changed keys.

This fixes two traps in today's guides form, which posts all four keys:
- Setting only a running head also switched on a bottom page-number zone on every page without a detected number, because `guidesEditor` defaults the zone to `bottom` (`processing.js:36`).
- `propose_guides` puts the median rule in `footnote_line` (`processing/services.py:369`), and that value would become live the same way.

### 5.3 Accept all suggestions

«خطوط فاصلة مقترحة في 5 صفحات · اعتمادها كلها» sets `footnote_line` on those pages in one call, with the toast «اعتُمدت الخطوط المقترحة في 5 صفحات · تراجع». Undo restores each page's previous override.

### 5.4 Undo

- Every change returns its inverse. The toast's «تراجع» posts it; the button is bound to a boolean `hasAction`, following the 2a0bca2 pattern, and never runs by itself.
- ⌘Z / Ctrl+Z does the same while the mode is open and no field has focus. It undoes the last change only.
- In «التخطيط» nothing irreversible happens (no OCR), so undo is held on the client and the server stays stateless.

### 5.5 Keyboard (physical keys via `e.code`, so the Arabic layout works — UX blocker 3)

| Keys | Action |
|---|---|
| ← / PageDown, → / PageUp, Home / End, G, 1, 2 | as today (turns follow RTL) |
| B | gray ⇄ b&w |
| H / F | focus the running-head / footnote line of this page, or add it |
| on a focused line: ↑ / ↓ | move 0.2 % (with Shift: to the previous / next gap between lines) |
| on a focused line: Enter / Delete / Esc | save now / remove from this page / undo the unsaved move |
| Esc (no field focused) | drop the draft; otherwise clear the filter |
| ⌘Z / Ctrl+Z | undo the last change |

### 5.6 Accessibility

- Lines are sliders with `aria-valuemin/max/now` and `aria-valuetext` («حدّ الترويسة عند 6.2%»).
- Grips, chips, menu items and the suggestion are labelled buttons («خيارات منطقة الحاشية»).
- The image has the alt text «الصفحة 12 بعد التجهيز».
- An `sr-only` list names the page's regions in reading order and updates with them.
- Colour never carries meaning alone: every band has its label, and the legend names the colours.
- The live region announces saves, undo and the preview summary.
- Each line has a 20 px tall hit area (`.guide-line::before`), and focus returns to the line after a save.
- Target sizes follow DESIGN §10.

### 5.7 Not in 7a

- free rectangles and "ignore" zones inside the page
- a bottom band forced on every page
- two columns
- changing a band's kind by hand (with guides, each band's kind follows from its position)

## 6. Data model and migrations

- `books.Book.ocr_started_at = DateTimeField("بدء المعالجة", null=True, blank=True)`.
- `books.Book.status` / `books.Page.status` choices: labels only (§1, D65).
- Verbose names and labels:
  - `Page.guides_override`: «تخطيط خاص بالصفحة»
  - `processing.LayoutGuides`: «التخطيط العام»
  - `Region.Source.GUIDES`: «تلقائي»
- `processing.Preprocess.rule_candidates = JSONField("خطوط فاصلة مقترحة", default=list, blank=True)`: `[{"y", "x0", "x1", "thickness", "fill"}]` in gray pixels, filled only when no rule was accepted.
- **`books/migrations/0007_book_ocr_started_at.py`**: AddField, the AlterFields, and a RunPython. The RunPython sets `ocr_started_at = updated_at` for books that:
  - have status `ocr`, `ready_for_review`, `reviewing` or `assembled`, or
  - have a page in `layout_done`, `ocr_done`, `reviewed` or `assembled`, or
  - have any `OcrRun` (so the migration depends on `ocr/0003`).
  
  Reverse: no-op.
- **`processing/migrations/0004_preprocess_rule_candidates.py`**: AddField + the label AlterFields.
- Optional management command `detect_rule_candidates [book …]`. It fills `rule_candidates` from the stored gray images and writes only that field, so existing books show suggestions in `?view=guides`.

## 7. Services — new and changed functions

`books/services.py`

| Function | Change |
|---|---|
| `start_processing` (448–469) | new messages; a broker failure reverts (§3.4) |
| **`start_ocr(book)`** | new (§3.5) |
| `_stage_signatures` (472–484) | `layout_stage` flag (§3.3) |
| `run_stage` (536–560) | in «التخطيط» only `preprocess`; the gate is read fresh (`Book.objects.filter(pk=…).values_list("ocr_started_at")`) |
| `validate_rerun` (570–581) | same rule |
| `rerun_book` (589–613) | none (inherits the rule) |
| `toggle_exclude` (616–647) | waiting branch keyed on the gate (§3.8) |
| `_bar_state` (653–661) | `needs_guides` → success |
| `_progress_payload` (711–724) | + `layout_stage`; `percent` = the prepared share in «التخطيط» |
| `book_dashboard(book, view=None)` (973–1050) | + `guides_mode`, `start_action` (extract / re-extract / restart), the config below; `rerun_stages` filtered |
| `page_regions` (1072–1074), `page_detail_context` (1102–1144) | computed bands when the page has no rows; stages filtered |
| `book_sheets(book, first, last, guides=False)` (1268–1313), `_sheet` | with `guides`, each item carries the `guides` block (below), and lines and fast runs are not loaded |
| `STAGE_LABELS` (63–69) | `preprocess` → «تجهيز الصفحات» |

`books/models.py`: the `refresh_status` branch (§3.2), `_settled`, `ALL_PAGES_FAILED_MESSAGES`, and a `Book.in_layout_stage` property (`ocr_started_at is None`).

`processing/services.py`

| Function | Change |
|---|---|
| **`resolve_layout(book_values, manual_book, override, pre) -> PageLayout`** | pure, extracted from `page_layout` (569–604); `page_layout` wraps it with the same results and one query instead of two per page |
| **`page_bands(pre, book_values, manual_book, override) -> list[Spec]`** | `guide_regions(resolve_layout(…))` |
| **`layout_doubts(pre, layout, override, specs) -> list[str]`** | §4.5; labels in `LAYOUT_DOUBT_LABELS` |
| **`page_guides_payload(page, pre, book_values, manual_book, rows=None) -> dict`** | the `guides` block: bands (the rows once derived, else computed), lines, override, suggestion, doubts, image URLs, `locked` |
| **`book_guides_state(book, first=None, last=None) -> dict`** | the compact entry per page, plus book values, stats (`guides_summary`) and counts; ≤ 4 queries whatever the page count |
| **`preview_book_guides(book, changes, reset_overrides=()) -> dict`** | writes nothing; per page the new bands, `changed`, `cut`, `kept_override`, `locked`; plus a summary |
| **`apply_book_guides(book, changes, reset_overrides=(), user=None) -> dict`** | partial, inert start (§5.2); in «التخطيط» it saves; in «المعالجة» it runs today's `apply_guides` loop (713–726); returns the changed pages and an `undo` payload |
| **`set_page_guides(page, set_, unset=(), reset=False) -> dict`** | merge semantics with the same validation (`clean_guides(partial=True)`); the stage rule of §3.9; returns the payload and an `undo` payload |
| **`set_many_page_guides(book, items) -> dict`** | bulk version (accept all, undo) |
| **`guides_summary(book) -> dict`** | the stats part of `guides_context` (808–820), which is removed with the screen |
| `apply_guides` (693–734), `set_page_guides_override` (737–755) | in «التخطيط»: save only |
| `_derive_regions` (619–656) | a page with review work (`ocr.services._has_review_work`) keeps its regions, `changed=False` |

`processing/pipeline.py`:
- The component pass of `detect_footnote_rule` (355–378) moves into `_rule_components(…)`.
- `detect_footnote_rule` keeps its exact acceptance, so all current tests are unchanged.
- New **`footnote_rule_candidates(gray, lines, med_h, extra_rows)`** returns components that pass every test except thickness, with `0.3 × med_h < thickness ≤ 0.5 × med_h` (`NEAR_RULE_THICKNESS = 0.5`) and fill ≥ 0.6 (`NEAR_RULE_MIN_FILL`), widest first.
- `run_pipeline` calls it only when no rule was accepted and stores the result in `PreprocessResult.rule_candidates`. `preprocess_page` saves it through `model_fields()`.

## 8. Tasks and Celery

| Task | Queue | Change |
|---|---|---|
| `books.tasks.ingest_book_task` | default | passes `continue_ocr` to the chord callback |
| `books.tasks.after_preprocess` | default | keeps its name, since queued chords call it; pauses or continues (§3.6) |
| **`books.tasks.start_ocr_task`** | default | new (§3.5) |
| `books.tasks.rerun_book_from` | default | none |
| `processing.tasks.preprocess_page` | default | refreshes the book in «التخطيط» |
| `processing.tasks.layout_page` | default | returns early in «التخطيط» |
| `ocr.tasks.ocr_page_fast` / `ocr_page_full` | default / gpu | return early in «التخطيط» |
| `ocr.tasks.read_numbers` | default | none |

There is no routing change (`nassakh/settings.py:123–131`). The `layout` queue is the book page's re-layout (D47) and has nothing to do with «التخطيط»; nothing new goes there.

## 9. Endpoints (FBVs)

| Route | Name | Who | What |
|---|---|---|---|
| POST `/books/<id>/start/` | `books:start` | editor | «استخراج الصفحات» (`start_processing`), messages updated |
| POST `/books/<id>/start-ocr/` | `books:start_ocr` | editor | «بدء المعالجة» (`start_ocr`) → redirect to the dashboard |
| GET `/books/<id>/?view=guides` | `books:detail` | login | the «التخطيط» mode during «المعالجة» |
| GET `/books/<id>/guides/` | `processing:guides` | editor | redirects to the dashboard (`?view=guides` once «المعالجة» started; `#sheet-N` for `?page=N`); POST kept (full form, stage-aware) |
| GET `/api/books/<id>/sheets/?from&to&guides=1` | `api:book_sheets` | login | + the `guides` block per page |
| GET `/api/books/<id>/guides/[?from&to]` | `api:book_guides` | login | `book_guides_state` (thumbs, grid, chip counts) |
| POST `/api/books/<id>/guides/` | `api:book_guides` | editor | `apply_book_guides`; body `{set: {…}, reset_overrides: [keys]}`, or an `undo` payload (`{set, source: "auto", restore_overrides: {page_id: override}}`) |
| POST `/api/books/<id>/guides/preview/` | `api:book_guides_preview` | editor | `preview_book_guides` |
| POST `/api/books/<id>/guides/pages/` | `api:book_page_guides` | editor | `set_many_page_guides`; body `{pages: [{id, set?: {…}, override?: {…} \| null}]}` |
| POST `/api/pages/<id>/guides/` | `api:page_guides_override` | editor | today's replace body, or `{merge: true, set: {…}, unset: [keys], reset: false}` |

Error messages are Arabic: the `clean_guides` messages, 409 with «بدأت المعالجة بالفعل.», and 422 for locked pages.

The `guides` block of a sheet item. All values are ratios of the prepared image, to 4 decimals:

```json
"guides": {
  "bands": [{"kind": "running_header", "bbox": [0, 0, 1, 0.062], "source": "book"},
            {"kind": "body", "bbox": [0, 0.062, 1, 0.781], "source": "auto"},
            {"kind": "footnote", "bbox": [0, 0.781, 1, 0.965], "source": "rule"},
            {"kind": "page_number", "bbox": [0.46, 0.972, 0.53, 0.99], "source": "detected"}],
  "lines": {"header": {"y": 0.062, "source": "book"}, "footnote": {"y": 0.781, "source": "rule"}},
  "override": {"footnote_line": 0.781}, "suggestion": {"y": 0.7648, "x0": 0.638, "x1": 0.976},
  "doubts": [{"code": "rule_suggested", "label": "خط فاصل لم يُعتمد"}],
  "gray_url": "…/display.webp", "bw_url": "…/bw.png", "derived": false, "locked": ""
}
```

The compact entry of `api:book_guides` is `{"id", "n", "b": [[kind, y0, y1, x0, x1], …], "d": n_doubts, "o": has_override, "s": suggestion_y | null, "l": "" | "approved" | "review"}`, about 70 B per page.

Config additions in `book_dashboard`: `guidesMode`, `layoutStage`, `guidesUrls` (state, apply, preview, pages, page template `__id__`, startOcr, back), `guides` (book values + source), `guidesStats`, `textLayer`.

## 10. Templates, JavaScript, CSS

**Templates**
- `templates/books/detail.html`:
  - `{% if guides_mode %}` blocks for the top-bar primary and ⋯ items, the toolbar chips and image toggle, the banner, and the side panel (`{% include "books/_guides_side.html" %}`).
  - `<template id="sheet-guides">{% include "books/_sheet_guides.html" %}</template>`.
  - Outside the mode, the file renders today's markup unchanged.
  - The ⋯ item «ضبط الأدلة» becomes «التخطيط» (a link to `?view=guides`); the `guides` primary and the `needs_guides` banner are removed.
- `_page_sheet.html`: hidden `.sheet-doubts` and `.sheet-guides-actions`, shown only in the mode.
- `_page_tile.html` and the thumb shell: an empty `.gd-bands` overlay and an `.is-doubt` mark.
- `templates/books/form.html`: the button «استخراج الصفحات», with the help text «تُجهَّز الصفحات (قصّ وتقويم وتنظيف) وتُكتشف مناطقها، ثم تنتظر «بدء المعالجة».»
- `templates/processing/guides.html`: removed.

**`static/src/js/books.js` (`bookDashboard`)**
- `guidesMode` comes from the config.
- Guides FILTERS.
- `primary` returns `extract` / `startOcr` / `back` in the mode, and today's order otherwise (`books.js:514–528`).
- `mountSheet` clones `#sheet-guides` and creates no decode handle.
- `patchGuidesBody` and `patchBands` (thumbs, tiles).
- The guides state map: `api:book_guides` once on load, then for the pages the poll reports as changed, batched like the sheets queue.
- `applyGuides(items)` / `previewGuides(items | null)` for the component.
- Dispatches `nassakh:page-shown` on `swapTo`.
- Key map additions (§5.5).

**`static/src/js/processing.js`: `bookGuides`**, the one new Alpine component. It sits on the `.bk-layout` wrapper in the mode and replaces `guidesEditor`.
- State: `book` (current values), `draft`, `preview`, `busy`, `error`, `last` (undo payload), `drag`.
- It owns the side panel section, the preview box, the page's drag / key / chip handling (delegated listeners on the sheet stack, direct style writes while dragging), the API calls and the toasts.
- It reads the dashboard through `Alpine.store('book').dash`.
- Pure helpers on `window.NassakhGuides` for the Node tests: `snapY(y, lineBoxes, tolerance)`, `adjacentBands(bands, guide, y)`, `mergeBody(set, unset)`, `inverse(change)`.
- `preprocessPanel`'s notice no longer mentions OCR in «التخطيط».

**CSS**
- `static/src/components/processing.css` keeps `.guide-line` / `.guide-handle` and adds `gd-` classes: page figure, band chip, grip, suggestion, menu, preview box, legend, thumb/tile overlays.
- `theatre.css`: `.bk-dashboard.is-guides .bk-viewer .gd-page { width: min(calc(100cqh * var(--ar-n, .7)), 100cqw) }`.
- Rebuild with `npm run build:css`.

## 11. Copy and names

| Where | Arabic |
|---|---|
| Stage names | «التخطيط» · «المعالجة» |
| Actions | «استخراج الصفحات» · «إعادة استخراج الصفحات» · «بدء المعالجة» · «إعادة بدء المعالجة» (today) · «العودة إلى الصفحات» |
| Book statuses | «مرفوع» · «قيد التخطيط» · «تم التخطيط» · «قيد المعالجة» · (the rest unchanged) |
| Page status | `preprocessed` «مُجهَّزة» |
| Re-run labels | «تجهيز الصفحات» (preprocess), «التخطيط» (layout); OCR labels unchanged |
| Chip while preparing | «قيد التخطيط · 40 من 120 صفحة» |
| Empty states | «تُستخرج الصفحات الآن» / «تظهر الصفحات هنا واحدةً واحدة، ثم تُجهَّز وتُرسم مناطقها.»; for `uploaded`: «لم تُستخرج الصفحات بعد» / «اضغط «استخراج الصفحات» لتجهيز الصفحات واكتشاف مناطقها.» |
| Banner | «جُهّزت الصفحات واكتُشفت مناطقها. ألقِ نظرة واضبط ما يلزم، ثم اضغط «بدء المعالجة».» + «ستّ صفحات تستحق نظرة · عرضها» |
| Chips | «الكل» · «تستحق نظرة» · «بضبط خاص» · image «رمادية» · «أبيض وأسود» |
| Side panel | «التخطيط» · «جُهّزت 7 من 7 صفحات» · «لكل الصفحات» · «ترويسة أعلى كل الصفحات» · «حاشية حيث لم تُكتشف» · «رقم الصفحة حيث لم يُكتشف» · «بدون · أعلى · أسفل» · «% من الأعلى» · «% ارتفاع المنطقة» · «من هذه الصفحة» · «الموضع الوسيط» · «إزالة الضبط العام» · «خطوط فاصلة مقترحة في 5 صفحات · اعتمادها كلها» · legend «متن · حاشية · ترويسة · رقم الصفحة · خط مقترح» |
| Page | bands «متن» «حاشية» «ترويسة» «رقم الصفحة»; grips «+ ترويسة» «+ حاشية»; «خط فاصل؟» «الحاشية تبدأ هنا» «ليس خطًّا فاصلًا»; menu «إزالة من هذه الصفحة» «تطبيق على كل الصفحات…» «ليس رقم صفحة»; head «التلقائي» «استثناء» «بضبط خاص»; locks «معتمدة: لا تتغيّر» «فيها تصحيحات مراجعة»; «حفظ وإعادة التعرّف على الصفحة» |
| Preview | «معاينة» · «يغيّر هذا 118 صفحة.» · «تُضاف منطقة حاشية إلى 40 صفحة لم تُكتشف فيها حاشية.» · «في 3 صفحات يقطع خطٌّ سطرًا أو تغطّي منطقةٌ سطرًا من المتن: …» · «صفحتان بضبط خاص تبقيان كما هما» · «تطبيقه عليهما أيضًا» · «سيُعاد التعرّف على نص 118 صفحة.» · «تطبيق على كل الصفحات» · «إلغاء» |
| Toasts | «حُفظ لهذه الصفحة · تراجع» · «طُبّق على 118 صفحة · تراجع» · «اعتُمدت الخطوط المقترحة في 5 صفحات · تراجع» · «عادت الصفحة إلى التلقائي · تراجع» · «اكتمل التخطيط · 7 صفحات» |
| Doubts | §4.5 |
| Errors / messages | «لم تبدأ المعالجة بعد؛ اضغط «بدء المعالجة» أولًا.» · «لم يكتمل التخطيط بعد؛ انتظر حتى تُجهَّز كل الصفحات.» · «بدأت المعالجة بالفعل.» · the broker message (§3.4) · «بدأ استخراج الصفحات. تُحدَّث هذه الصفحة تلقائيًا.» · «بدأت المعالجة. تُحدَّث هذه الصفحة تلقائيًا أثناء العمل.» |
| «الأدلة» wording replaced | `processing/views.py:29` → «طُبّق التخطيط على N صفحة…»; `processing/tasks.py:95` → «…راجع تخطيطها ثم أعد تشغيل المرحلة.»; `ocr/services.py:225` → «…راجع تخطيط هذه الصفحة.»; `books/tasks.py:19–21` → «…ثم اضغط «إعادة استخراج الصفحات».»; `books/models.py:13–16` → «…أو ابدأ من جديد من زر الإعادة أعلى الصفحة.» (the old text is kept for matching) |

Names kept in code (§1, D65): `LayoutGuides`, `Book.guides`, `Page.guides_override`, `needs_guides`, `processing`, `ocr`, the `guides` URL and API names, `Region.Source.GUIDES`, and the stage keys.

## 12. Tests

**`books/tests.py`**
- `refresh_status` in «التخطيط»:
  - uploaded pages → `processing`; all prepared → `needs_guides`; all failed → `error` (`ALL_PAGES_FAILED`)
  - excluded pages are ignored
  - an error with the old text still re-derives
  - with the field set, today's `core/tests.py:326–370` assertions hold (they move behind `ocr_started_at`)
- `ingest_book_task` passes `continue_ocr`. `after_preprocess`:
  - pauses: no `run_stage`, book `needs_guides`, proposal made
  - continues with `True` (today's test)
  - decides by the field when called without the kwarg
- `start_ocr`:
  - claims once; a second call and a `processing` book are refused
  - sets status `ocr` and calls `start_ocr_task.delay` once
  - reverts on a broker failure
- `start_ocr_task` sends prepared pages from `layout` and legacy `layout_done` pages from `ocr`, never both.
- `run_stage`, `validate_rerun` and `rerun_book` in «التخطيط»: preprocess-only chain, other stages refused. Today's chain test runs with the field set.
- `toggle_exclude` in «التخطيط»: an unprepared page → preprocess task only; a prepared page → joins; nothing queued for OCR.
- `start_processing`:
  - works from `uploaded` and `error`, is refused for `needs_guides`, and reverts on a broker failure
  - the `book_create` view extracts at once and shows the range, and the book stays `uploaded` when queueing fails
- Dashboard:
  - the mode's config and markup: primary candidates, chips, side panel, the `sheet-guides` template
  - a started book renders no mode markup (today's template output)
  - `?view=guides` works for a started book
  - `rerun_stages` is filtered
- Progress:
  - the prepared-share percent, and `bar_state` success for `needs_guides`
  - the compact poll payload is unchanged: `test_progress_compact_tiles_keys_size_and_book_error` still passes with the same query count
- Sheets with `guides=1`: computed bands before derivation and rows after, lines as ratios, doubts, suggestion, `locked`; ≤ 5 queries.
- Migration: the RunPython on books in every status (the table in §3.12).
- `smoke_pipeline` runs `start_ocr` after `start_processing`; a new `--layout-only` flag stops at the pause. Its test passes.

**`processing/tests.py`**
- Pipeline:
  - a synthetic page with a 7 px rule (line height 21 px): `detect_footnote_rule` returns None and `footnote_rule_candidates` finds it
  - a row of closed text (fill < 0.6) is not a candidate
  - `run_pipeline` stores candidates only when no rule was accepted
  - every existing rule, block and page-number test is unchanged
- `resolve_layout` equals `page_layout` on the existing fixtures; `page_bands` equals `page_region_specs`.
- `layout_doubts`: one test per code, plus suppression by an explicit override.
- Guide services:
  - `apply_guides` and `set_page_guides_override` in «التخطيط» only save: pages stay `preprocessed`, no `run_stage`
  - `apply_book_guides` starts inert: auto → manual with only `header_cut` gives zone `none` and no footnote line
  - merge mode: set, unset and reset keep the other keys
  - `set_many_page_guides` and its undo
  - `preview_book_guides`: changed pages, kept overrides, `cut`, locked pages in «المعالجة», the re-OCR count, with a query count independent of the page count
- In «المعالجة»:
  - `apply_book_guides` re-derives and re-OCRs only the changed, unlocked pages
  - `_derive_regions` keeps the regions of a page with review work, and its footnote lines keep their region
- Tasks and views:
  - `preprocess_page` refreshes the book in «التخطيط»; `layout_page` returns early
  - the guides URL redirects
  - the APIs check the editor role and CSRF and return Arabic validation messages

**`ocr/tests.py`**: `ocr_page_fast` and `ocr_page_full` do nothing before «بدء المعالجة»; the page and its runs are unchanged.

**`core/test_theatre.py`** (Node harness)
- `primary` per state in the mode (`uploaded`, `processing`, `needs_guides`, `error`, `?view=guides`). Today's cases are unchanged except `needs_guides`, which was `guides`.
- Guides filters, and counts from `api:book_guides` data.
- `keyAction` for B / H / F, and `e.code` with an Arabic `key`.
- `patchGuidesBody` geometry: bands, lines, suggestion, physical left/top.
- `NassakhGuides.snapY`: gap middles, suggestion, Alt off, tolerance.
- The component's draft → preview → apply / cancel flow, the undo payloads, and a toast action that never runs by itself.

**`core/test_frontend.py`**: the form's button, the redirect, and the compiled CSS carrying the `gd-` classes.

**`core/tests.py`**: `status_dot("needs_guides") == "dot-success"` and the new labels.

## 13. Rollout and migration safety

1. Stop both workers, `git pull`, run `make migrate` (this sets `ocr_started_at` for the 25 existing books), run `npm run build:css`, then start the workers and the web server.
2. Messages still in Redis are safe:
   - Queued chords call `after_preprocess(results, book_id)` and decide by the field, which is set for every book with OCR history.
   - Queued OCR chains run for books with the field set and are skipped for the others.
3. Rolling back the code leaves an unused column. A book paused at `needs_guides` then behaves as the old code expects: its `needs_guides` branch keeps it, and the old «ضبط الأدلة» applies the guides and moves it on.
4. The owner tests on:
   - a short PDF; book 25's range is the best case, with six missed footnote rules
   - an untouched book, which should need one click and give the same text as before

## 14. Risks

- **A pause for books that need nothing.** Mitigated: extraction starts with the upload, the pause costs one click, and «بدء المعالجة» is enabled the moment the last page is prepared.
- **Old words with new meanings.** «قيد المعالجة» now means OCR, and «مُعالَجة» becomes «مُجهَّزة». Screenshots and the RUNBOOK need updating; `docs/RUNBOOK.md` describes «بدء المعالجة» as the single start.
- **Book fallback lines are blunt.** A book footnote line applies to every page without a detected footnote, including pages that have none. The preview counts them and each page can switch the line off. The semantics are unchanged; they are just visible now.
- **Guide ratios after re-preparation.** A crop change shifts the content under a stored ratio. In «التخطيط», `line_cut` shows it; in «المعالجة», it goes unnoticed, as today.
- **Re-reading in «المعالجة».** Changes made from `?view=guides` cost GPU time; the preview says how many pages.
- **Pages with review work no longer follow guide changes** (D71). This is deliberate: it keeps their footnote attribution. The preview lists them as locked.
- **The suggestions are measured on only 180 pages of 14 books** (Appendix A). A thick underline in the lower part of a page could be offered as a separator. It is only a suggestion and is never applied by itself.
- **bw.png is full size.** The b&w toggle loads it for the page on screen only.

## 15. Open questions for the owner

1. **Thick separator lines.** Keep them as suggestions (7a), or accept them automatically as footnote starts? Measured: 6 of 6 correct on book 25, no false one on 173 other pages.
2. **Born-digital books.** Keep the same pause (recommended: one flow), or skip it, since their text comes from the PDF?
3. **«بدء المعالجة» before every page is prepared.** In 7a it is disabled until then, which is exact and simple. For 800-page books, should a click during preparation be remembered and start as soon as the last page is ready?
4. **The guides screen goes away**, and its URL opens the dashboard's «التخطيط» mode. Agreed?

## Appendix A — measurements (dev database, 2026-09-26)

- **Missed rules, book 25** (الحوليات الليبية, 7 pages).
  - No rule was accepted on any page.
  - The separator lines are 6.5–7.1 px thick against a 6.3 px limit (0.3 × a 21 px line), with fill 0.65–0.88. Example, page 1: y 2389, 732 px wide, 7.1 px thick, fill 0.79.
  - They are on pages 1, 3, 4, 5, 6 and 7; page 2 has no footnote.
  - Page 6's footnotes were found from smaller type only.
  - On the other five pages the whole footnote text was read as body.
- **False suggestions.** With thickness ≤ 0.5 × line height and fill ≥ 0.6, there were none on books 1, 13, 14, 15, 16, 18, 19, 20, 21, 22, 23, 24 and 26 (173 pages). Book 20 (a bibliography) had thick components, but they were closed text with fill 0.22–0.56.
- **Doubt counts** on the 217 included pages: see §4.5. Line boxes alone cannot find rules: thin projection bands are often the core of short text lines (checked on books 19, 23 and 26).
- **Current state.**
  - 25 books, all `ready_for_review`, `reviewing` or `assembled`.
  - Regions are only `body`, `footnote` and `page_number`, all from `guides`.
  - No page override is in use.
  - One book has manual guides: book 5, footnote line at 85.6 %, which cuts no line.
