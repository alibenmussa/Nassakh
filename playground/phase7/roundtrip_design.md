# Phase 7 design: the round trip, navigation, the Arabic keyboard, names and defaults, narrow screens

This part covers the UX test's findings (docs/UX_TEST_2026-09-26.md) on:
- the round trip between review and an edited book (blocker 1);
- the Arabic keyboard (blocker 3);
- navigation between stages;
- names and defaults, and readiness;
- narrow widths and legibility.

It builds on D35–D63 and adds D70–D84. The other Phase 7 designers own the processing split (التخطيط → regions check →
المعالجة, with the owner's decision of today: «التخطيط» with its button «استخراج الصفحات», then «المعالجة» with «بدء
المعالجة»; «ضبط الأدلة» is dropped), the layout-review step (D64+), OCR trust and structure (footnote role, verse). §6
lists what this design needs from them.

**Evidence gathered for this design** (read-only on the dev database; scripts and notes in the Phase 7 scratchpad):
- **The two incidents, replayed.**
  - The base (the assembly the edits started from) was rebuilt from the review history. A page-scoped three-way merge
    then takes exactly the review's change and keeps every book-page edit.
  - Book 26 (the UX tester, page 3): 1 paragraph is taken («بزعَ» → «بزغَ»); the other 6 paragraphs of that page are
    identical. The chapter heading the owner had made on page 1, and a page-1 paragraph edit, are untouched. The
    whole-chapter rebuild had dropped that heading (8 → 6 pages).
  - Book 23 (page 8): 1 paragraph is taken («فقيها، فاضلا، زاهدا» → «فقيهاً، فاضلاً، زاهداً»). The owner's inserted
    chapter heading «دولة بني الأغلب في طرابلس» and the edits on pages 1 and 5 (one with a footnote) are untouched.
- **Today's drift is mostly not text.**
  - Book 26 reports drift on pages 4–8, but those pages were only approved: no text changed. The banner would say
    «تغيّر نص 5 صفحات» with nothing to take.
  - Book 19 («كتابي») reports 70 pages, none with a review revision. Its lines were rewritten by a machine pass (the
    D50/D51 numbers pass).
- **Book-page edits already blur the source marks.**
  - A split copies the source lines to both halves: book 13 has 21 lines claimed twice.
  - A merge keeps only the first block's lines (`static/src/editor/convert.js:465-472`): 13 lines of book 13 and 7 of
    book 25 are claimed by no block.
- **The PDF page count can often be read in the browser.** From the first and last 2 MB of the file, a regex on the
  root `/Type /Pages … /Count N`, tested on 40 Arabic PDFs in `~/Documents/Books`: 26 exact, 14 not found (compressed
  object streams), none wrong.
- **Stray footnote paragraphs.** The manuscripts hold 26 body paragraphs that start with «(n)» (24 of them numbered
  lists in book 19). A page-end rule flags 3: the two real strays (book 23 p. 5 «(١) قال مصححه…», book 25 p. 1) and one
  list item «(22) …». Adding the limit "marker ≤ 15" removes that false one.
- **Contrast.** `--color-text-3` #8a8a93 measures 3.42 : 1 on white, 3.20 on `--bg-subtle` and 3.03 on `--bg-muted`.
  #6b6b73 measures 5.28, 4.93 and 4.68.

---

## 0. Decisions (proposed)

- **D70: A safe round trip: review changes enter an edited book page by page, never as an unannounced chapter
  replacement.**
  - Once the text has been edited on the book page (D41), a review or processing change reaches the book through a
    plan the owner sees first: «تغييرات المراجعة».
  - The plan holds only the paragraphs of the changed pages. Each one is resolved by a three-way word merge: base,
    the book page's text, and the fresh assembly.
  - A snapshot is taken first, and undo works.
  - The old whole-chapter rebuild stays as a secondary action with the manuscript's warning. The server refuses it
    without `replace_edited`, as D49 already does for a whole-book run.
- **D71: The manuscript keeps its base.**
  - `Manuscript.base` holds the assembled document the edits started from. It is written at the first book-page write
    and moved page by page by every merge. Snapshots keep theirs.
  - Manuscripts edited before D71 have no stored base, so their first merge gets one:
    - from the review history (`LineRevision.before`) when every line that changed can be reverted;
    - otherwise the paragraph is the owner's choice, defaulting to «نصّي». Nothing is taken silently.
  - After the first merge the base exists, because the fresh text of the unchanged pages is their base.
- **D72: Drift is classified and live.**
  - Content drift has four causes: `review` (a review revision), `processing` (lines rewritten by a machine pass),
    `added` and `removed` (eligibility).
  - Approval-only drift is not drift. It is absorbed at the next merge and never announced.
  - The book page, the dashboard and the stage bar refresh the drift:
    - on focus, visibility and a bfcache return;
    - on a same-origin `BroadcastChannel` message from the review screen;
    - on a 30 s poll while the page is visible.
- **D73: A merge on the book page keeps both paragraphs' source marks.** `mergeNodes` unions `sourcePages` and
  `sourceLineIds`. The merge (D70) needs this; legacy documents are rescued by a text-containment rule.
- **D74: One keymap across screens, matched by the physical key.**
  - Keys are read from `KeyboardEvent.code`; digits are accepted in any script (١–٩, ۱–۹); IME composition is
    ignored.
  - In review, an open word menu takes letters (a correction) and a closed one takes commands. ⌘↵ approves from
    anywhere.
  - Moved keys:
    - dashboard 1/2 → V;
    - book page S (spread) → V;
    - book page 1/2/3 (fit) → + − 0;
    - S means «فواصل الصفحات الأصلية» everywhere.
- **D75: A stage bar on every book screen:** «التخطيط · المعالجة · المراجعة · المخطوطة · الكتاب · الإخراج».
  - It sits in the top bar, costing no height.
  - Each step shows one state: done, current, running, out of date, attention, not started or not yet available.
    Every step is a link unless it is not yet available.
  - It collapses by its own width, down to a stage menu on phones.
- **D76: Book screens fold the app sidebar into the icon rail** (as the book page does today): dashboard, review,
  manuscript, book and export. The book list and the upload form keep the full sidebar.
- **D77: Review knows where it was opened from.**
  - `?from=book|manuscript|export&at=…` shows «‹ الكتاب» and returns there.
  - A detour from the book page does not auto-advance after approval.
  - After the last page, an end-of-review panel names the next step.
  - N never skips a page that approval has just brought in.
- **D78: Thumbnails and addresses lead to the work.**
  - A dashboard tile or sheet links to review. The page's technical detail becomes a secondary «تفاصيل المعالجة».
    D33's plain click that opens the viewer stays.
  - The book page writes `?tab=` (and `mode`) with `#page-N`, so a copied link keeps its tab. `?block=` opens on a
    block.
- **D79: One term per concept.**
  - «تجميع المخطوطة» replaces «تحويل إلى كتاب».
  - «الكتاب» means only the typeset book; the project is called by its title, and «لوحة الكتاب» is retired.
  - Pagination is «ترتيب الصفحات» («تُرتَّب الصفحات…»), so «إخراج» means files only.
  - Counts always go through `ar_count`.
- **D80: «تضمين الصفحات غير المُراجَعة» is off by default and never remembered.**
  - The popover always opens unchecked, and a checked box warns and relabels the button.
  - Follow-up runs (seams, roles, suggestions, «إعادة التجميع») inherit the current manuscript's choice.
  - Staleness is measured against the run's own settings.
- **D81: Readiness names what is not yet a book.**
  - «قبل الإخراج» lists the pages in the book not reviewed yet, the pages missing from the book, and body paragraphs
    that look like stray footnotes.
  - «جاهز» only when every page is reviewed and nothing warns.
- **D82: The kept page range is visible before anything is queued.**
  - The upload form shows «تُستخرج الصفحات 187–193 (7 صفحات)» when the browser can read the count, otherwise the
    rule in words.
  - `books.services.kept_range` gives the exact range to the «التخطيط» step's first screen (other designer).
- **D83: Narrow screens.**
  - Review stacks below an 880 px container, and below 560 px gets a phone top bar with a bottom action bar holding
    «اعتماد الصفحة».
  - The book page toolbar moves its extras into «⋯» below 640 px.
  - The side panel's tabs are labelled and wrap to two rows.
- **D84: Legibility.**
  - `--color-text-3` becomes #6b6b73 (≥ 4.68 : 1 on every surface); the old grey is kept only for disabled
    controls.
  - No text under 11.5 px except `kbd`, badges and the filmstrip numbers.

---

## 1. A safe round trip between review and an edited book (D70–D73)

### 1.1 What happens today

**The book page.**
- «⋯» → «إعادة تجميع الفصل من المراجعة» (`templates/editor/layout.html:62-65`) and the drift banner's button
  (`layout.html:123-126`) call `reassembleChapter()` (`static/src/js/book/panel.js:736-750`) with no confirmation.
- That queues `editor.services.reassemble_chapter` (`editor/services.py:600-648`). Its task runs the whole pipeline
  and splices the fresh blocks over the chapter (`_save_chapter_run`, `editor/services.py:759-849`).
- The blocks are chosen by source line (`pick_chapter_nodes`, `:657-710`), so the book page's headings, footnotes,
  styles and word fixes in that chapter are replaced. A typed heading is re-added only when it has no source line
  (`:700-709`). That is why book 26's heading, made from a paragraph, was lost.

**The drift banner.**
- It reads `cfg.drift` and the chapter summaries once, when the page loads (`panel.js:716-725`, `stage.js:480`,
  `editor/services.py:1210-1220`).
- The page's poll asks only `api:preview`, and only while a render runs (`stage.js:290-305`); `onVisible` polls only
  when active (`stage.js:281-286`). So the banner appears only after a reload.

**The manuscript's regroup, by contrast.**
- The convert popover warns «حُرِّر نص الكتاب في صفحة الكتاب…» and its button reads «استبدال النص المحرَّر»
  (`templates/assembly/_convert_popover.html:15`, `books.js:599`, `manuscript.js:426`).
- The server refuses a whole-book run over an edited text without `replace_edited` (`assembly/services.py:394-397`).

**Drift itself.**
- It is `stale_pages` (`assembly/services.py:699-726`): an eligibility change, a changed reviewed flag, or a changed
  content signature (line count and the latest `updated_at`, `:108-128`).
- So approving a page counts as "text changed". `approve_page` updates lines with `QuerySet.update`, which leaves
  `updated_at` alone, but it flips the reviewed flag (`review/services.py:912-935`).

### 1.2 Model

**`editor.Manuscript.base`**: a JSON field, null by default, labelled «أصل التحرير».
- It holds the assembled document (pipeline output shape) that the current text descends from.
- It is written by one helper, `_mark_edited(manuscript)`. Every editor write calls it first: `save_chapter`
  (`editor/services.py:243-298`, which today writes inline at `:277-282`), `_write` (`:301-306`, used by
  `restore`, `find_replace`, `convert_digits` and `editor/uncertain.py:423`). When `origin == assembly`, it copies
  the document into `base` before changing it.
- It is cleared by a whole-book assembly (`assembly/services.py:_save`, `:634-639`: origin back to `assembly`).
- It is moved page by page by each merge (§1.4, step 7).

**`editor.ManuscriptSnapshot.base`**: JSON, null. `_take` copies the manuscript's base.
- `restore` puts it back.
- For a snapshot with `reason == reassembly`, the snapshot's own document is its base (it was unedited when taken).
- Older snapshots leave the base null (legacy mode).

**`assembly.AssemblyRun.plan`**: JSON, null. It holds the merge plan of a run with `settings.scope == "changes"`
(§1.5).
- `is_chapter_run` (`assembly/services.py:318-320`) becomes `is_partial_run` (scope `chapter` or `changes`), so a
  full run is still queued beside one.
- `manuscript_state` excludes `changes` runs from its `latest` run (`:763-768`), so a plan never shows «قيد التجميع»
  on the dashboard or the manuscript view.

**`AssemblyRun.included[pk]` gains `"at"`**: the ISO time the page was read. Drift classification and base
reconstruction use it; legacy entries fall back to the run's `finished_at`.

**Drift classes**: `editor.services.review_drift` (`editor/services.py:569-594`) returns:

```json
{"edited": true,
 "pages": [12, 13, 91],                                            // content drift only (readiness, banner, stage bar)
 "reasons": {"12": "review", "13": "processing", "91": "added"},   // review | processing | added | removed
 "approvals": [4, 5, 6, 7, 8],                                     // reviewed flag only, same signature: not announced
 "chapters": {"h812": [12, 13], "h950": [91]}}
```
- `stale_pages` gets a sibling, `stale_reasons(included, rows, options)`.
- `review` means the page has a revision (not undone) created after its `included.at`. One grouped query over
  `LineRevision` for the changed pages.
- `processing` means the signature changed with no revision after the baseline.
- `editor_state.drift_pages` (`editor/services.py:1093-1106`) and the readiness row
  (`publishing/readiness.py:149-156`) read the new `pages`, so approvals never count as drift.

### 1.3 The merge (`editor/merge.py`, pure functions, no ORM)

**Inputs:**
- `document` (M): the manuscript;
- `base` (B): the stored base, the reconstructed one, or None;
- `fresh` (F): the `pipeline.assemble` document for the current lines;
- `pages` (S): the page numbers to take.

**Step 1: select.**
- For M, B and F, select the top-level body blocks whose `source_pages` meet S, or whose footnotes' `sourcePage` is
  in S.
- `lines_of(block)` is the block's `sourceLineIds` plus its notes' and its blockquote children's.

**Step 2: cluster.**
- Union-find over the selected blocks of the three documents, joining blocks that share a source line. This joins
  the halves of a paragraph the owner split (the split copies its lines, `convert.js:446-461`) to the one fresh
  paragraph.
- A block the owner typed (no source lines) is never clustered and stays where it is.
- Rescue for pre-D73 merges: a B or F block with no M partner joins the M block on the same pages that contains at
  least 80 % of its words in order (difflib on word lists).

**Step 3: tokens.**
- A block list becomes tokens, compared by key:
  - `("¶", type, level, style)` at each block start;
  - `("w", text, marks)` per run of non-space characters of one mark set;
  - `(" ",)` for a collapsed space;
  - `("fn", canonical note content)`;
  - `("pb", page)`;
  - `("br",)`.
- Volatile attributes never enter a key: `id`, `reviewed`, `suggestedRole`, `sourcePages`, `sourceLineIds`, a note's
  `number` / `marker` / `orphan` / `sourcePage`, and every null or empty attribute.
- Comparing node JSON is wrong: TipTap splits text nodes and adds null attributes. The prototype showed every block of
  book 26 "changed" that way, while the word tokens were equal.
- Each token keeps a reference to its source node, so a rebuild restores the real footnote node and marks.

**Step 4: decide each cluster** (m, b, f are its blocks in each document):

| Case | Kind | Default | Chip in the UI |
|---|---|---|---|
| T(m) = T(f) | none | – | – |
| base known, T(m) = T(b) | `take` | theirs | «من المراجعة» |
| base known, T(f) = T(b) | none (mine keeps its edits) | – | – |
| base known, both changed, diff3 clean | `merged` | merged | «مع تعديلك» |
| base known, both changed, diff3 conflicts | `conflict` | mine | «تعارض» |
| base unknown, T(m) ≠ T(f) | `choose` | mine | «للمقارنة» |
| m empty, f new (no b) | `insert` | theirs | «فقرة جديدة» |
| m empty, b = f (the owner deleted it) | none | – | – |
| m empty, b ≠ f (the owner deleted it, review changed it) | `conflict` | mine | «تعارض» |
| f empty, T(m) = T(b) (lines gone: page excluded, lines deleted) | `remove` | theirs | «تُحذف» |
| f empty, T(m) ≠ T(b) | `conflict` | mine | «تعارض» |

**diff3** over tokens, as prototyped:
- Anchors are the base tokens that difflib (`autojunk=False`) matches in both mine and theirs.
- Between anchors: take theirs where mine equals base, mine where theirs equals base, either one when both made the
  same change; otherwise it is a conflict.
- A conflict whose chunks are two footnote tokens of the same note is merged recursively on the note's own content.
- `¶` tokens carry structure, so the owner's split, a heading the owner made (`¶ heading` against `¶ paragraph`), and
  a review role change all merge like words. Both sides changing the same `¶` is a conflict.

**Step 5: rebuild** (only items that change: an untouched block is never re-serialised, so chapter versions do not
move without an edit).
- The result tokens are cut into blocks at `¶`. A result block takes its attributes from the mine block that produced
  its `¶`, else from the fresh block.
- A 1:1 `take` keeps mine's `id`, `style`, `breakBefore` and `keepWithNext` unless theirs changed the block type.
- A new block's `sourceLineIds` and `sourcePages` are the union of the lines of the blocks its tokens came from.
- `reviewed` comes from theirs.

**Step 6: placement and apply.**
- `apply(document, plan, choices)` walks M. Each item's chosen nodes replace its first M block; the item's other M
  blocks are dropped.
- An `insert` goes after its anchor: the M block that holds the lines of the nearest preceding clustered F block.
  Failing that, before the successor's; failing that, at the end of the chapter covering its page.
- Approval-only pages flip `reviewed` on their blocks. This changes no text and is not an item.
- Then `doc.repair_ids` runs against the rest of the document, and the chapters are formed again as `save_chapter`
  forms them (a new level-1 heading splits a chapter).

**Step 7: base.**
- `splice_base(base or fresh, fresh, applied_pages)` replaces the base's blocks touching the applied pages with the
  fresh ones (both are pure pipeline output in page order).
- With no base (legacy), the new base is `fresh`. For pages that did not drift, fresh is the old base, give or take
  pipeline code changes. After a code change, later merges only show "both changed" more often; they never lose text.

**Base reconstruction (legacy tier, D71).** For a drift page P with baseline time `t`:
- Every line of P with `updated_at > t` must be covered by a review revision after `t`.
- Revert those revisions in memory, newest first:
  - edit, resolve, merge, drop_word and role restore `before`;
  - insert removes the line;
  - delete re-adds `before`;
  - approve and reopen only set the page's reviewed flag.
- The reverted line count must equal the one in `included[P].sig`.
- If any check fails, P has no base, and its differing items become `choose`.
- Run the pipeline on the book with P's reverted lines in place (the neighbours as they are), and take the blocks
  touching P.
- Book 19's machine rewrites fail this check, as they must. Books 23 and 26 pass.

### 1.4 Services, task, API

`editor/services.py` (typed, with docstrings; views stay thin):
- `plan_review_changes(book, user, pages: list[int] | None = None) -> AssemblyRun`
  - Refused with 400 before a manuscript exists, and with 409 when the text is not edited: the answer then carries
    `{reassemble: true}`, because a plain re-assembly loses nothing.
  - Idempotent while a plan is queued or running.
  - Otherwise it creates `AssemblyRun(scope="changes", pages=…)` and enqueues `editor.tasks.plan_review_changes`.
- `run_review_changes_plan(run_id)`, the task body:
  - `load_book`, then `pipeline.assemble` in memory;
  - base: stored, else reconstructed, else none;
  - `merge.plan(...)`.
  - It stores in `run.plan`:
    - `{manuscript_version, base: "stored"|"rebuilt"|"none", pages, items, counts}`;
    - the signatures of the pages read (`sig`, `reviewed`, `at`);
    - the fresh warnings and seams for those pages.
  - Status `done`, stage `plan`. Any failure is recorded on the run with an Arabic headline, and the manuscript is
    untouched.
- `review_changes(book, run_id=None) -> dict`: `{drift, run, plan, stale}`. `stale` is true when the manuscript
  version or a planned page's signature moved since the plan.
- `apply_review_changes(book, run_id, choices, pages, user) -> dict`, synchronous and atomic, locking the book and the
  manuscript:
  1. Check the plan is fresh; otherwise raise `PlanStale` (409).
  2. Take an `edit` snapshot «قبل أخذ تغييرات المراجعة · ص 12، 13» (with its base).
  3. `merge.apply`, then write the document (version + 1, origin stays `editor`).
  4. Move the base with `splice_base` over the applied pages.
  5. `run.included` = the manuscript run's entries, with the applied and approval-only pages replaced by the planned
     signatures. Warnings and seams are merged as `_save_chapter_run` merges them (`:786-831`).
     `manuscript.run = run`, and `run.stats.applied = {at, taken, merged, kept, pages}`.
  6. Apply D36: reviewed pages whose signature is still the planned one become `assembled`.
  7. `_schedule(book, focus_chapter, version)`, so the focus chapter is laid out again now and the book debounced
     (D47).
  8. Return `{version, snapshot, chapters: [{id, version, title}], reload, relayout, applied}`.
- «الاحتفاظ بنصّي في الكل» is `apply` with every choice set to `mine`. It moves the baseline and the base, but does
  not write the document (no version bump, no 409 in an open editor).
- `reassemble_chapter(book, chapter_id, user, replace_edited=False)` raises `EditorEdited` (409
  `{detail, edited: true}`) without the flag. The flag comes only from the confirmation dialog (§1.5).
- `merge.py` also serves a dev command, `manage.py review_changes <book> [--pages 3,5] [--dry-run]`. It prints the
  plan, for the owner's smoke test.

| Method | URL (name) | Who | Body → response |
|---|---|---|---|
| GET | `/api/books/<id>/review-changes/` (`api:review_changes`) | login | `?drift=1` → `{drift}` (four queries: the page polls this); else `{drift, run, plan, stale}` of the newest plan |
| POST | `/api/books/<id>/review-changes/` | editor | `{pages?: [n…]}` → 202 `{run_id, status}` |
| POST | `/api/books/<id>/review-changes/<run_id>/apply/` (`api:review_changes_apply`) | editor | `{choices: {itemId: "theirs"\|"mine"\|"merged"}, pages: [n…]}` → 200 as above; 409 `{detail, stale: true}` |
| POST | `/api/books/<id>/chapters/<cid>/reassemble/` (existing) | editor | now needs `{replace_edited: true}`; without it → 409 |

Plan items, as the UI reads them:

```json
{"id": "i3", "kind": "merged", "default": "merged", "page": 12, "pages": [12, 13], "chapter": "h812",
 "chapter_title": "الفصل الثالث", "block": "p815",
 "diff": [["eq", "… وكان"], ["del", "فقيها"], ["ins", "فقيهاً"], ["eq", "، فاضلاً …"]],
 "conflicts": [], "base": "stored"}
```
- `diff` compares mine with the default result by word, with 5 words of context around each change.
- A conflict carries `{mine, theirs}` snippets.
- The result nodes stay server-side in `run.plan`. The client never sends document JSON for a merge.

### 1.5 UI (the book page first; the review screen's look)

**The banner** (`layout.html:113-129`, reworded). It reads the live `drift` state (§1.6) and is book-wide. When the
drift is in one chapter, the chapter's title is added.

```
┌────────────────────────────────────────────────────────────────────────────────────────────┐
│ [ لاحقًا ]  [ ⟲ عرض التغييرات ]    تغيّر نص صفحتين في المراجعة بعد تحرير الكتاب: 12، 13.  ⓘ │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```
(The right edge is the start side, as in PHASE6_SPEC §8.3.)
- The page numbers are review links (§3.3, with `from=book`). The wording follows the reasons:
  - review: «تغيّر نص صفحتين في المراجعة بعد تحرير الكتاب: 12، 13.»
  - processing: «أعادت المعالجة قراءة 70 صفحة بعد تحرير الكتاب: 4، 12، 13…» (at most 8 numbers, then «…»)
  - added: «رُوجعت 3 صفحات لم تدخل الكتاب بعد: 91، 92، 93.»
  - mixed: «تغيّر نص 5 صفحات بعد تحرير الكتاب: 3 في المراجعة و2 أعادت المعالجة قراءتها.»
- «لاحقًا» hides the banner until the drift changes, remembered in `sessionStorage` per book and drift signature.
- An unedited manuscript that is stale shows the same banner with «تغيّر نص 3 صفحات في المراجعة بعد التجميع؛ لم
  يُحرَّر الكتاب بعد، فإعادة التجميع لا تُضيّع شيئًا.» and one button, «إعادة التجميع», which is a plain
  `start_assembly`.
- «⋯» (`layout.html:62-65`): «تغييرات المراجعة…» with a count, replacing «إعادة تجميع الفصل من المراجعة».

**The tab «تغييرات المراجعة»** (key `changes`) in the side panel (`_book_side.html`).
- Its model is «غير المؤكَّدة»: a list of review-born items, clicked to turn the stage to the page and light the
  block. It is not modal, so the page stays in view.
- It appears only while there is content drift or an open plan, with a badge.
- Opening it (the banner, «⋯», `?tab=changes`) first closes and saves the open paragraph, as `reassembleChapter` does
  today (`panel.js:739-741`). Then it posts the plan and polls `api:review_changes` every 700 ms until the plan is
  ready.

```
┌────────────────────────────────────┐
│ ٣              تغييرات المراجعة     │
│ تُؤخذ فقرات هذه الصفحات وحدها، ويبقى│
│ كل ما سواها كما حرّرته. تُحفظ نسخة   │
│ قبل الأخذ.                           │
├────────────────────────────────────┤
│  الفصل الثالث · ص 12            ☑   │
│  ● من المراجعة                       │
│  «… وكان ~~فقيها~~ فقيهاً، فاضلاً …» │
│  ● مع تعديلك                         │
│  «… القَمَرُ ~~بزعَ~~ بزغَ – النجمُ …»│
│  ص 13 · إعادة المعالجة          ☑   │
│  ▲ تعارض                             │
│  عدّلتَ هذه الكلمات في الكتاب،       │
│  وغيّرتها المراجعة أيضًا.            │
│  نصّك: «… سنة 1966 …»               │
│  المراجعة: «… سنة 1965 …»           │
│  [ المراجعة | نصّي ]                 │
├────────────────────────────────────┤
│         [ أخذ التغييرات (3) ]        │
│          الاحتفاظ بنصّي في الكل       │
│  إعادة بناء الفصل من المراجعة…        │
└────────────────────────────────────┘
```
- **Rows.** Each page row has a checkbox (whether this page is taken now), `ص N`, the chapter title as meta, and a
  reason chip: «المراجعة», «إعادة المعالجة», «صفحة جديدة» or «أُخرجت من الكتاب».
- **Items.** Items have a status dot with its word (never colour alone) and a diff snippet: `<del>` struck through
  in `--color-danger-text` on `--color-danger-bg`, `<ins>` underlined on a soft green.
- **Choices.** Conflicts and `choose` items get the segmented control «نصّي | المراجعة» (default «نصّي»).
  - A `choose` item's help reads «لا يُعرف ما عدّلتَه هنا قبل هذا الإصدار من نسّاخ؛ قارن واختر.»
  - A deleted-by-owner conflict reads «حذفتَ هذه الفقرة من الكتاب، وغيّرتها المراجعة.»
- **Bulk actions.** Per page: «خذ ما جاء من المراجعة في هذه الصفحة» and «أبقِ نصّي في هذه الصفحة» (links in the page
  row's «⋯»), needed for a legacy book like «كتابي» with 70 processing pages.
- **Click.** A click on an item turns to the page where the item's block now sits (`G.caretLine`) and lights it
  (`lightBlock`, `panel.js:224-229`). An `insert` lights its anchor.
- **Loading.** Two skeleton rows and «يُقارَن نص الصفحات بنص الكتاب…».
- **No text difference** (the approvals of book 26): «لا فرق في النص؛ المراجعة والكتاب متّفقان في هذه الصفحات.» with
  «تم». This moves the baseline without writing the document.
- **Apply.** The button disables and reads «تُؤخذ…». Then:
  - `afterServerEdit()` (`panel.js:656-666`): the chapter reloads, the list refreshes, the relayout is followed;
  - the changed blocks flash `--accent-soft` once (600 ms, none under reduced motion);
  - the tab empties;
  - the undo toast reads «أُخذت تغييرات صفحتين من المراجعة · تراجع», and undo restores the snapshot.
  - For keep-all: «بقي نصّك كما هو؛ لن تعود هذه الصفحات إلى التغييرات · تراجع».
- **409.** The toast «تغيّر النص منذ المقارنة؛ أُعيدت المقارنة.», a new plan, and the owner's choices kept for the
  item ids that remain.
- **Failure.** «تعذّرت المقارنة؛ بقي الكتاب كما هو.» with «إعادة المحاولة».
- **Rebuild.** «إعادة بناء الفصل من المراجعة…» opens a confirmation dialog (`rv-modal`, the look of «نسخة محفوظة»):
  - title: «إعادة بناء الفصل «{title}» من المراجعة؟»
  - text: «يُستبدل نص الفصل كله بنص صفحاته في المراجعة، فتضيع تعديلاته في الكتاب: العناوين والحواشي والكلمات. تبقى
    منه نسخة في «نسخة محفوظة».»
  - buttons: «إلغاء» and «استبدال الفصل» (`btn-danger`, posts `replace_edited: true`).

**Other entry points.**
- The dashboard's «الكتاب» block: its drift line (`detail.html:273`, `books.js:575-578`) gains «عرض التغييرات» →
  `layout?tab=changes`.
- The export readiness row `review_drift` now links «تغييرات المراجعة» → `?tab=changes`
  (`publishing/readiness.py:149-156`).
- The manuscript view's edited banner adds «غيّرت المراجعة نص 3 صفحات بعد التحرير؛ تُؤخذ من «الكتاب».» with a link.
- The convert popover's edited warning (`_convert_popover.html:15`) adds «أو خذ تغييرات المراجعة وحدها من «الكتاب».»
  as a link.
- Review, on a page of an edited book, shows one quiet line in the lines toolbar: «يُحرَّر هذا الكتاب في «الكتاب»؛
  تصله تغييرات هذه الصفحة من «تغييرات المراجعة».»

### 1.6 Live drift (D72)

- The book page keeps `drift` as state; it no longer reads `cfg.drift` alone. It is refreshed with
  `GET api:review_changes?drift=1`, throttled to one call per 2 s:
  - when the tab becomes visible (`stage.js:281`, now always, not only while active);
  - on `pageshow` with `persisted` (a bfcache return; the export page already listens, `export.html`);
  - on `window` focus;
  - on a `BroadcastChannel('nassakh')` message `{type: "review", book, page}`;
  - every 30 s while the page is visible and the text is edited.
- Review posts that message after every successful change: resolve, edit, insert, delete, merge, drop word, role,
  approve, reopen, undo.
- The dashboard (its poll stops once processing ends, `books.js:342`) and the manuscript view listen to the same
  channel and refresh once.
- `BroadcastChannel` works on this Mac's Safari and Chrome. Where it is missing, focus, visibility and the poll cover
  the gap.

### 1.7 Tests

**`editor/test_merge.py`** (pure):
- the tokens: marks, notes, page marks, hard breaks, collapsed spaces, null attributes ignored;
- diff3: clean merges, the same change on both sides, conflicts, recursion into a note;
- every row of the decision table;
- splits (two halves, one fresh paragraph); merges (union and rescue); typed blocks kept;
- a heading made on the book page kept while a word elsewhere on the page is taken;
- a review role change that makes a heading and splits a chapter;
- an uncertain mark dropped by review coming in;
- a footnote the owner inserted kept;
- ids stable for 1:1 items;
- untouched blocks byte-identical (chapter versions unchanged);
- idempotence: after applying, a new plan has no items;
- `splice_base`;
- two fixtures built from the incidents (book 26 page 3, book 23 page 8), reduced to their blocks.

**`editor/tests.py`:**
- the base written once by every write path (`save_chapter`, find & replace, digits, uncertain, restore), never
  rewritten by a later edit, reset by a whole-book run, restored from a snapshot, taken from a `reassembly` snapshot's
  own document;
- drift reasons (`review`, `processing`, `added`, `removed`, approval-only not listed);
- the plan task end to end (eager);
- apply: snapshot, version, drift cleared, D36, relayout scheduled;
- keep-all writes no document;
- 409 for a stale version and for a page reviewed again after the plan;
- the reconstruction tier, and its refusal when a line changed with no revision;
- `reassemble` without `replace_edited` → 409;
- roles (a reader gets 403), and query counts that do not grow with the page count.

**`core/test_layout_ui.py`** (Node harness):
- the banner's wording for each reason;
- the tab: post, poll, render, choices, apply, toast, chapter reload;
- the rebuild dialog posts the flag;
- live refresh on `pageshow`, visibility and a channel message;
- «⋯» shows the count.

**Existing tests that change on purpose:** `editor/tests.py:565-994` (drift and chapter re-assembly, now behind the
flag) and the layout UI assertions on «إعادة تجميع الفصل من المراجعة».

### 1.8 Risks and what must not break

- **Round-trip exactness.** Only items that change are rebuilt, and a test holds that untouched blocks keep their
  exact JSON. A dev check runs `plan` (dry) on every manuscript in the dev database and asserts that a
  plan-then-apply with all choices `mine` leaves the document byte-identical.
- **D47 live layout.**
  - Every apply schedules the focus chapter's relayout and the debounced book render.
  - The open paragraph is closed and saved before a plan and before an apply.
  - Pages swap in place as they do after a restore.
- **Chapter versions (409).** Apply changes the versions of the chapters it touches. The page reloads the edited
  chapter, and a stale tab gets the usual conflict banner.
- **Snapshots and undo.** An `edit` snapshot is taken before apply (pruned with the others, 20 kept). Undo restores
  the snapshot, its base and its run, so the drift comes back.
- **D41, D49, D36.** Nothing flows in without the owner's apply. The whole-book refusal stays. `assembled` flips only
  for pages that are unchanged since the plan.
- **Plan runs never read as an assembly.** They are excluded from `manuscript_state.latest` and from `_queue_run`'s
  idempotence.
- **Size.** The base doubles the manuscript's JSON (book 19: 889 KB plus about 889 KB) and each snapshot's. That is
  acceptable for one local user; snapshot pruning is unchanged.
- **Pipeline code changes** between the base and a merge can only produce extra "both changed" items, never a silent
  loss, because conflicts default to «نصّي».

---

## 2. The Arabic keyboard and one keymap (D74)

### 2.1 Today

- **Review** matches `ev.key` only (`static/src/js/review.js:91-120`).
  - On the Arabic layout, A, E and N type ش, ث and ى; ? types ؟; 1–3 type ١–٣.
  - With a word focused, any single character becomes a correction (`:118`), so ش starts one instead of approving.
    With the Latin layout, a approves even with a word open (`:116` comes before `:118`).
- **The dashboard** matches G, N and C by code, but 1 and 2 by `key` (`static/src/js/books.js:755-773`).
- **The manuscript view** matches `?`, `[` and `]` by `key` (`static/src/js/manuscript.js:161-166`). The Arabic layout
  types ؟, ج and د there.
- **The book page** already matches by code and accepts ١–٣ (`static/src/js/book/geometry.js:378-420`).
- **The same key means different things on different screens:**
  - 1/2/3: the dashboard's view, review's readings, the book page's fit;
  - S: the manuscript's seams, the book page's spread;
  - 0: review's zoom reset;
  - E: review's line, the book page's mode.

### 2.2 Rules

**One small module, `static/src/js/keys.js`** (`window.NassakhKeys`, loaded after `ui.js`), used by every screen's
`keyAction`:
- `letter(e)` → `'a'…'z'` from `e.code` (`KeyA`…), else a Latin `e.key`. Letters never match with Shift, ⌘, Ctrl or
  ⌥ unless a map asks for them.
- `digit(e)` → 0–9 from `Digit*` or `Numpad*` without Shift, or from `e.key` in 0–9, ٠–٩ or ۰–۹.
- `is(e, '?')` → `?`, `؟`, or Shift with `Slash`. `plus` / `minus` accept `+ =` / `- _` and `Equal` / `Minus` /
  `NumpadAdd` / `NumpadSubtract`. Brackets are matched by code.
- `composing(e)` → `e.isComposing || e.keyCode === 229`. Every map returns null for it.

**Review has two modes, decided by the word menu (`pop.open`).**
- **The menu is open (word mode): keys edit the word.**
  - 1–9 in any script choose reading n; a digit beyond the readings types it.
  - Enter accepts; Tab and ⇧Tab move; ⌥← and ⌥→ merge; ⌫ deletes; Esc closes the menu (focus stays on the word).
  - ⌘↵ approves the page.
  - Any other printable character starts the correction with that character, Latin or Arabic, including ؟ and −.
    A proofreader can start «شيء» with ش.
- **The menu is closed (page mode): keys drive the page**, by physical key: A E N G O V C ? + − 0, ← →, Home and End.
  - Space opens the focused word's menu.
  - Digits still choose a reading for a focused word.
  - Letters do nothing, so there is no stray correction.
- The two modes follow the tester's case: ش with the menu open is a correction, and ش with it closed is «اعتماد
  الصفحة». Both work on any layout.

### 2.3 The keymap: one meaning per physical key

| Key | Meaning | Dashboard | Review | Manuscript | Book page | Change |
|---|---|---|---|---|---|---|
| ← → PageDown PageUp | next / previous page (RTL) | ✓ | ✓ | – | ✓ | – |
| Home End | first / last page | ✓ | **new** | – | ✓ | – |
| G | go to a page (focus the jump field) | ✓ | **new** (the pager count becomes a field) | ✓ | ✓ | – |
| N | the next page waiting for review | ✓ | ✓ page mode | – | – | by code |
| A | approve the page | – | ✓ page mode | – | – | by code |
| E | edit (review: the line; book page: edit mode) | – | ✓ | – | ✓ | by code |
| O | the original scan | – | **new**: «المعالَجة / الأصل» | ✓ drawer | ✓ drawer, now in preview too | – |
| S | the scan page marks («فواصل الصفحات الأصلية») | – | – | ✓ seams | **moved here** (was spread) | book page |
| V | the view arrangement | **moved here** (was 1 / 2) | **new**: swap sides | – | **moved here**: one page / spread | dashboard, book page |
| C | copy the page's text | ✓ | **new** | – | – | – |
| J K ↓ ↑ | next / previous block | ↑ ↓ lines | – | ✓ | – | – |
| ] [ | next / previous warning | – | – | ✓ by code | – | by code |
| + − 0 | zoom in / out / fit | – | ✓ | – | **moved here** (was 1 / 2 / 3: fit width, 100 %, fit height) | book page |
| 1–9 | choose reading n | – (freed) | ✓ | – | **new**: in the word menu of «غير المؤكَّدة» | – |
| Enter | accept the word | – | ✓ | (activates) | ✓ word menu | – |
| Tab ⇧Tab | next / previous uncertain word | – | ✓ | – | – | – |
| Space | open the focused word's menu | – | **new** | (menus) | – | – |
| ? | the shortcut sheet | **new** | ✓ | ✓ | ✓ | – |
| Esc | close the top layer | ✓ | ✓ | ✓ | ✓ | – |
| ⌘Z ⇧⌘Z | undo / redo | – | ✓ | – | ✓ | – |
| ⌘↵ | approve the page, from the word menu too | – | **new** | – | – | – |
| ⌘S ⌘F ⌘B ⌘I ⌘⇧F ⌘⌥digits ⌘[ ⌘] | book page editing | – | – | – | ✓ unchanged | – |

**Sheets and hints.**
- **Review's sheet** (`templates/review/review.html:353-382`) is regrouped:
  - «في قائمة الكلمة» (1–9, Enter, Tab, «اكتب التصحيح مباشرة», ⌥← ⌥→, ⌫, Esc, ⌘↵)
  - «في الصفحة» (A, E, N, G, O, V, C, + − 0, ← →, Space, ?)
- **The legend** (`review.html:300-303`) reads «Tab للانتقال بينها، 1–3 لاختيار قراءة، أو اكتب التصحيح مباشرة؛ ⌘↵
  لاعتماد الصفحة.» and the toast at `review.js:604` reads «حُسمت كل الكلمات · اعتمد الصفحة بـ A».
- **The approve button's title** is «اعتماد الصفحة (A، أو ⌘↵ من قائمة الكلمة)».
- **The dashboard** gets a sheet: «?» and «⋯» → «اختصارات لوحة المفاتيح».
- **The book page's sheet** (`templates/editor/_book_overlays.html:161-184`) and the toolbar titles
  (`_book_toolbar.html:18-25`) follow the moves: «صفحة واحدة أو صفحتان (V)» and «ملء الارتفاع (0) · تكبير (+) ·
  تصغير (−)».
- **Every sheet** shows the letter keys as their Latin caps, with a line that says «تعمل الاختصارات بلوحة المفاتيح
  العربية أيضًا: المفتاح نفسه في مكانه.»

### 2.4 Tests

**`core/test_keymap.py`** (new, Node).
- For every screen's `keyAction`, it feeds Latin events and Arabic-layout events (`{key: 'ش', code: 'KeyA'}`,
  `{key: '٢', code: 'Digit2'}`, `{key: '؟', code: 'Slash', shiftKey: true}`, `{key: 'ج', code: 'BracketLeft'}`), and
  checks that composing events return null.
- It holds the table above as data and fails when a bare physical key maps to actions of two different meanings
  across screens.
- **Review's modes:** ش with the menu open gives `type`, closed gives `approve`; ⌘↵ in word mode gives `approve`; a
  digit beyond the readings types.

**Updated tests:**
- `core/test_review_ui.py:326-336` (letters with focus, approve by code);
- `core/test_theatre.py:778` (the key `'2'` becomes `V`);
- `core/test_layout_ui.py:986` and `:2378` (S, 1, 2 and 3 on the book page).

---

## 3. Navigation (D75–D78)

### 3.1 The stage bar (D75)

**Where.**
- `templates/base.html:102-108` gains `{% block stagebar %}` between the `h1` and `.topbar-actions`.
- On book screens the `h1` holds only the book title (`max-width: 26ch`, truncated). The current stage is the bar's
  `aria-current` step, and `<title>` keeps «المراجعة · صفحة 3 · {title} · نسّاخ».
- A thin row under the top bar was rejected: the owner has pushed chrome out of the page's height before (D34).
- The partial is `templates/partials/_stage_bar.html`, rendered from `stages`. The small Alpine component `stageBar`
  lives in `static/src/js/stages.js`.

**The steps and their states** (`books.services.book_stages(book, current) -> list[dict]`):
- It is built from `book_progress` (`books/services.py:672-708`, already the dashboard's) plus the newest export of
  each format (one query), about 8 queries in all.
- Each step is `{key, label, url, state, detail, hint, current}`, where `state` is one of `todo`, `active`, `done`,
  `stale`, `attention` or `blocked`.

| Key · label | URL | done | active | stale | attention | blocked | detail and hint |
|---|---|---|---|---|---|---|---|
| `layout` · التخطيط | dashboard | every page at layout or later | extracting | – | pages in error | – | «استُخرجت 8 صفحات وحُدّدت مناطقها» / «تُستخرج الصفحات: 3 من 8» / «لم تُستخرج الصفحات بعد» |
| `processing` · المعالجة | dashboard | every page OCR'd or later | OCR running (detail «3/8») | – | pages in error | – | «عولجت 8 صفحات» / «قيد المعالجة: 3 من 8 صفحات» / «لم تبدأ المعالجة» |
| `review` · المراجعة | `review:next`, else page 1 | all reviewed | some reviewed (detail «6/8») | – | – | no page OCR'd yet | «رُوجعت 6 من 8 صفحات» / «تبدأ المراجعة حين تنتهي معالجة أول صفحة» |
| `manuscript` · المخطوطة | the manuscript view | fresh, or edited («النص يُحرَّر الآن في «الكتاب»») | assembling | stale and not edited: «تغيّر نص 4 صفحات بعد التجميع» | the run failed | – | «جُمعت: 38 فصلًا» / «لم تُجمَع المخطوطة بعد» |
| `book` · الكتاب | the book page | a layout exists | – | edited with content drift: «تغيّر نص صفحتين في المراجعة بعد تحرير الكتاب» | – | no manuscript: «يُفتح الكتاب بعد تجميع المخطوطة» | «17×24 سم · 84 صفحة» |
| `export` · الإخراج | the export page | a current file | an export running | the newest file is older than the text: «تغيّر النص بعد آخر ملف» | the last export failed | no manuscript | «Word وPDF · قبل ساعتين» / «لم يُخرَج ملف بعد» |

- `current` is the screen being shown: the dashboard passes `layout` or `processing`, following the other designer's
  processing states. A stale state sits on the step where it is fixed, once. Every step but `blocked` is a link.
- **Look** (DESIGN.md §2, §6):
  - steps are 28 px high, radius 6, 12.5 px/500 in `--text-2`;
  - the current step has the `--active` fill, `--text` and weight 600;
  - done is a 12 px ✓ in `--success`; active is a 7 px accent dot (static: chrome never loops, D24); stale is a 7 px
    warning dot with the label in `--warning-text`; attention is a danger dot; todo is a hollow ring; blocked is
    `--text-3` and not a link;
  - separators are a 12 px `i-chevron-end` (`icon-mirror`, pointing forward in RTL) in `--border-strong`.
- **Accessibility.** The bar is `<nav aria-label="مراحل الكتاب"><ol>`. Each step's state is also written out, visually
  hidden: «(مكتملة)», «(جارية)», «(أقدم من النص)», «(تحتاج انتباهًا)», «(لم تبدأ)», «(غير متاحة بعد)». The current
  step has `aria-current="page"`, and the `hint` is its `title`.
- **Collapse** by the bar's own width (`container-type: inline-size`):
  - ≥ 560 px: labels and details;
  - 360–560 px: labels, with a detail only on the current step;
  - below 360 px: one button, «المراجعة 6/8 ▾», opening a menu (`menu-popover`) of the six steps with their states.
  - The `h1` shrinks first, and hides below 900 px.
- **Live.** `stageBar` refreshes from `GET api:book_stages` on the window event `nassakh:stages`, which screens
  dispatch after approve, an assembly done, an apply, an export done. It also refreshes on the channel of §1.6, on
  visibility and `pageshow`, and every 60 s while the page is visible.

(Mockups: the right edge is the start side.)

Review at 1440 px, with the rail:
```
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ [اعتماد الصفحة] ? محفوظ ▬▬ حُسمت 5 من 9 ‹ صفحة 3 من 8 ›  الإخراج ‹ الكتاب ‹ المخطوطة ‹ [●المراجعة 6/8] ‹ ✓المعالجة ‹ ✓التخطيط  أيسر الشروح… │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```
The dashboard at 1180 px (compact: only the current step carries its detail):
```
│ ⋯ [الصفحة التالية للمراجعة]   الإخراج ‹ الكتاب ‹ المخطوطة ‹ المراجعة ‹ [●المعالجة 3/8] ‹ ✓التخطيط   المنهل العذب… │
```
A phone:
```
│ ⋯  › 3/8 ‹            [المراجعة 6/8 ▾]  ☰ │
```

**What the bar retires.**
- The top bars' back links: «لوحة الكتاب» in review (`review.html:15`), the manuscript (`manuscript.html:21`), the
  book page's «⋯» (`layout.html:85-88`) and the export page (`export.html:25-28, 45-48`).
- The dashboard's «كل الكتب» ghost (`detail.html:14-17`): the rail's «الكتب» and «⋯» keep it.
- The dashboard's own status text and bar in the top bar can then fold into the «المعالجة» step. That belongs to the
  processing designer (§6).

### 3.2 The rail on book screens (D76)

- The book page's rail rules (`static/src/components/layout.css:11-36`, `.app-shell:has(.bp-screen)`) become
  `.app-shell:has([data-rail])`.
- `data-rail` goes on the roots of the dashboard, review, the manuscript view, the book page, the export page and the
  page detail.
- That gives back 176 px, which is the room review needs at 900–1180 px (§5.1).

### 3.3 Review knows where it came from; the end of review (D77)

**The origin.**
- Links from the book page carry `from=book&at=<its page>`:
  - `reviewUrl(n)` (`panel.js:502`), which serves the banner's page links, «الأصل» and the drawer;
  - the changes tab.
- The manuscript view's «فتح في المراجعة» carries `from=manuscript&block=<id>`; the export page carries
  `from=export`.
- `review_page` (`review/views.py:15-27`) validates both: `from` against an enum, `at` as an int, `block` against
  `[hpn][0-9]+`. `review_payload` (`review/services.py:373-436`) gains `nav.back = {label, url}`:
  - «الكتاب» → `layout#page-<at>`;
  - «المخطوطة» → `manuscript#block-<id>`;
  - «الإخراج» → export.
- The top bar's first link shows «‹ الكتاب». In-review page swaps keep the parameters (the destination URLs of
  `swapTo`, `review.js:1165`).

**Detour mode** (`from=book`).
- After «اعتماد الصفحة» the next page does not slide in (`celebrate`, `review.js:1071-1082`). The lines pane shows
  «اعتُمدت الصفحة 12.», then «العودة إلى الكتاب» (primary) and «الصفحة التالية للمراجعة».
- When the book is edited, a successful change also shows the quiet line of §1.5.

**The end-of-review panel.** When approval finds no next page, or N finds none, the lines pane shows a card instead of
the toast «لا صفحات بانتظار المراجعة». Focus moves to its primary, and the live region says «رُوجعت كل الصفحات».

```
┌──────────────────────────────────────────────┐
│                  رُوجعت كل الصفحات ✓           │
│               8 صفحات · حُسمت 41 كلمة            │
│                                                │
│               الخطوة التالية: تجميع المخطوطة   │
│   تُجمَع الصفحات في نص واحد متّصل: فصول وفقرات   │
│   وحواشٍ، تتحقّق من بنيته قبل الكتاب.           │
│                                                │
│     البقاء في المراجعة     [ تجميع المخطوطة… ]    │
└──────────────────────────────────────────────┘
```

The next step comes from `book_stages`, as `review_payload.next_step`, and also in `approve_page`'s answer when no page
is next:

| State | Next step | Button | Link |
|---|---|---|---|
| no manuscript | «تجميع المخطوطة» | «تجميع المخطوطة…» | the manuscript view with `?convert=1`, which opens the popover |
| manuscript stale, not edited | «إعادة التجميع» | «إعادة التجميع» | the manuscript view |
| edited, with drift | «أخذ التغييرات إلى الكتاب» («غيّرت المراجعة نص صفحتين من الكتاب المحرَّر؛ تُؤخذ فقراتهما وحدها.») | «عرض التغييرات في الكتاب» | `layout?tab=changes` |
| fresh | «الكتاب» («المخطوطة محدَّثة؛ نسّق الكتاب وحرّره على صفحاته.») | «فتح الكتاب» | the book page |
| pages still processing | «لا صفحات بانتظار المراجعة الآن؛ ما زالت 3 صفحات قيد المعالجة.» | «العودة إلى المعالجة» | the dashboard |

**The N rule** (the minor finding "after A, N skips one"). A page brought in by approval's auto-advance is marked
`arrived`. N on an `arrived` page that still needs review and has not been touched shows the toast «هذه هي الصفحة
التالية للمراجعة» and does not skip.

### 3.4 Dashboard thumbnails lead to review (D78)

- `page_tile` gains `primary_url`: the review screen when the page's text is final and not excluded, else the page
  detail (`books/services.py:826-866`).
- Links:
  - the tile link (`templates/books/_page_tile.html:8`) and the sheet title «صفحة N» (`_page_sheet.html:8`) point to
    `primary_url`, so a new tab or a modifier-click reaches review;
  - D33's plain click on a tile, which opens the viewer (`books.js:1120-1131`), stays.
- On the tile:
  - a hover action «مراجعة» (always visible on touch) goes to review;
  - «تفاصيل المعالجة» (`i-sliders`, editors only) goes to the page detail. It also sits in the sheet header, next to
    «مراجعة», and in review's «⋯».
- The attention list keeps linking errors to the page detail, where their fix is.
- The view and filter choice becomes per book (`VIEW_KEY + bookId`, `books.js:28`), the minor finding "the grid/list
  choice is shared by all books".

### 3.5 Addresses keep the view (D78)

- `addressQuery()` (`static/src/js/book/edit.js:151`, used by `stage.js:619-621` and `edit.js:180-183`) builds
  `?mode=edit&tab=<tab>`. `setTab` rewrites the address the same way (`replaceState`, with no history entry).
- `?chapter=` is dropped once the page lands, because the hash says where it is.
- `page_config` (`editor/services.py:1174-1243`) accepts `block`, and the stage lands on the block's page and lights
  it (`goToBlock`, `panel.js:214-223`).
- `PANEL_TABS` (`editor/services.py:1153`) gains `changes`.

### 3.6 Tests

- **`books/tests.py`:** `book_stages` for each state of each step; `primary_url`.
- **`review/tests.py`:** `nav.back` for each `from` (an unknown value is ignored, `at` must be an int); `next_step` in
  the payload and in approve's answer.
- **`core/test_stage_bar.py`:** six steps, `aria-current`, the hidden state words, a blocked step with no link, the
  collapse classes, and refresh on the events.
- **`core/test_review_ui.py`:** the back link; detour approval; each variant of the end panel; the N rule; a channel
  message after a change.
- **`core/test_theatre.py`:** the tile's `href`, the «مراجعة» and «تفاصيل المعالجة» actions, the per-book view.
- **`core/test_layout_ui.py`:** `?tab=` survives a landing and a tab switch; `?block=`.

---

## 4. Names and defaults (D79–D82)

### 4.1 One term per concept (D79)

**The glossary:**

| Concept | Term | Its verbs and buttons | Never |
|---|---|---|---|
| Preparing pages, guides and regions (owner, today) | «التخطيط» | «استخراج الصفحات» | «ضبط الأدلة» |
| OCR | «المعالجة» | «بدء المعالجة» | – |
| Proofreading | «المراجعة» | «اعتماد الصفحة»، «الصفحة التالية للمراجعة» | – |
| The assembled text (the structure check) | «المخطوطة» | «تجميع المخطوطة»، «إعادة التجميع» | «تحويل إلى كتاب» |
| The typeset book page | «الكتاب» | «فتح الكتاب» | the project |
| WeasyPrint pagination | «ترتيب الصفحات» | «تُرتَّب الصفحات…» | «يُخرَج»، «إخراج» |
| Review changes entering an edited book | «تغييرات المراجعة» | «عرض التغييرات»، «أخذ التغييرات» | «إعادة تجميع الفصل» as the default |
| Files | «الإخراج» | «إخراج Word»… | pagination |
| The project | its title; the list «الكتب» | – | «الكتاب»، «لوحة الكتاب» |

**Renames:**

| Now | Where | New |
|---|---|---|
| «تحويل إلى كتاب» / «تحويل إلى كتاب…» | `templates/books/detail.html:64, 112, 266`; `templates/assembly/manuscript.html:39, 164`; `_convert_popover.html:9` | «تجميع المخطوطة» / «تجميع المخطوطة…» |
| «لم يُجمَّع هذا الكتاب بعد؛ حوّله إلى كتاب أولًا.» | `editor/services.py:41` | «لم تُجمَع مخطوطة هذا الكتاب بعد؛ اجمعها أولًا.» |
| «حوّل الكتاب إلى مخطوطة أولًا…» | `layout.html:101`, `export.html:59` | «اجمع مخطوطة الكتاب أولًا…» |
| «لوحة الكتاب» (links) | `review.html:15, 152`; `manuscript.html:21, 75, 167`; `layout.html:87`; `export.html:47`; `page_detail.html:26`; `books/form.html:21, 42, 93` | removed (the stage bar), or «المعالجة» where a link stays («فتح المعالجة لإعادة المرحلة») |
| side panel `aria-label="لوحة الكتاب"` | `_book_side.html:5` | «أدوات الكتاب» |
| «يُخرَج الكتاب…» | `static/src/js/book/stage.js:464` | «تُرتَّب صفحات الكتاب…» |
| «لم تُخرَج صفحات…» | `layout.html:180` | «لم تُرتَّب الصفحات…» |
| «بعد أول إخراج» | `layout.html:70` | «بعد أول ترتيب» |
| «لم تُخرَج صفحاته بعد» | `books.js:584` | «لم تُرتَّب صفحاته بعد» |
| «تُخرَج الصفحات بخط …» | `static/src/js/book/style.js:256` | «تُرتَّب الصفحات بخط …» |
| «تعذّر طلب الإخراج…» (the book render) | `stage.js:342` | «تعذّر طلب ترتيب الصفحات…» |
| `PreviewRender.Status.RUNNING` «قيد الإخراج» | `publishing/models.py:40` | «قيد الترتيب» (`Export` keeps its own, `:155`) |
| «إعادة تجميع الفصل من المراجعة» (default) | `layout.html:64, 125` | «تغييرات المراجعة…» (§1.5) |
| «اكتملت المعالجة · 8 صفحة» | `detail.html:334` | through `arCount`: «… · 8 صفحات» |
| the native «Choose File» | `books/form.html` | a label button «اختيار ملف PDF» with the file's name, or «لم يُختر ملف» |

**Tests:** template tests assert that the retired strings are absent from every template and script under
`static/src/js`.

### 4.2 «تضمين الصفحات غير المُراجَعة» off by default (D80)

**Today:**
- `Settings.include_unreviewed = True` (`assembly/pipeline.py:150`).
- The popovers open with `true` (`books.js:213, 596, 614`) and store it per book, because `start_assembly` merges the
  cleaned options (`assembly/services.py:290-310, 408-417`).
- So one click builds from raw OCR, as happened with book 24.

**Proposed:**
- The default becomes `False`. `start_assembly` no longer writes `include_unreviewed` into `Book.assembly_settings`;
  it goes into the run's `settings` only.
- A request without the key (the one-click «إعادة التجميع», seams, roles, suggestions) inherits the current
  manuscript run's value, else `False`.
- `stale_pages` and `review_drift` are measured with `normalize_settings(run.settings)`, the run's own, not the
  book's (`assembly/services.py:821`, `editor/services.py:586`). A manuscript built with unreviewed pages then never
  shows them as "removed". This is what must not break.
- **The popover** (`_convert_options.html:15-18`):
  - The box is unchecked when it opens, and always sent explicitly.
  - Unchecked with unreviewed pages, its help reads «تُترك 5 صفحات لم تُراجَع بعد، وتُضاف حين تُراجَع.»
  - Checked, the row turns amber with «يدخل الكتابَ نصُّ 5 صفحات لم يراجعه أحد؛ تبقى معلَّمة حتى تُراجَع.», and the
    button reads «تجميع مع 5 صفحات غير مُراجَعة».
- **Tests** (`assembly/tests.py`): the default; nothing stored; inheritance; staleness against the run's settings; the
  popover's state in the UI tests.

### 4.3 Readiness (D81, `publishing/readiness.py:119-176`)

**New rows** (warn), in this order after `uncertain_words`:
- **`pages_unreviewed`.** The book's pages in `run.included` that are `ocr_done` now: «77 صفحة في الكتاب لم تُراجَع
  بعد؛ نصّها كما قرأته النماذج: 3، 4، 7…» → «المراجعة» (`review:next`).
- **`pages_missing`.** Non-excluded pages not in the book (still processing, in error, or left out as unreviewed):
  «صفحتان لم تدخلا الكتاب بعد: 91، 92.» → «المراجعة», or «المعالجة» when they are still in the pipeline.
- **`stray_notes`.** A body paragraph qualifies when:
  - its text starts with a note marker (`(n)`, `[n]`, n ≤ 15, or `*`);
  - it lies in the trailing run of such paragraphs that ends its only source page (a run that is not the whole page);
  - it is at most 80 words.
  - Message: «3 فقرات في أواخر صفحاتها تبدأ بعلامة حاشية مثل «(1)» ولم تُربَط حاشيةً: ص 5، 12، 30.» → «عرض» → the book
    page with `?block=<first>`.
  - On the dev database this finds the two real strays and none of book 19's 23 list items.
  - Turning such a paragraph into a note belongs to the structure designer's footnote role (§6).

**`clear`** becomes «كل الصفحات مُراجَعة ولا ملاحظات؛ الكتاب جاهز للإخراج.». It is shown only without warn rows, so a
book with unreviewed pages can no longer read «جاهز».

**Tests** (`publishing/test_exports.py`): each row; the stray fixtures (a page-end «(١) قال مصححه…» flagged; a
mid-page «(1) … (2) …» list and «(22) …» not flagged); `clear` only when all pages are reviewed.

### 4.4 The kept page range (D82)

- **The server.** `books.services.kept_range(book) -> {source_pages, first, last, count}` (1-based, inclusive), from
  `source_page_count`, `skip_first` and `skip_last`.
- **The upload form** (`templates/books/form.html:44-47`, `bookForm` in `books.js:10-22`). When a PDF is chosen, it
  reads `file.slice` of the first and last 2 MB, decodes them as Latin-1, and takes the largest `/Count` of a
  `/Type /Pages` dictionary. Under the skip fields, live:
  - count known: «الملف 555 صفحة · تُستخرج الصفحات 187–193 (7 صفحات).»;
  - count unknown: «يُعرف عدد صفحات الملف بعد رفعه؛ تُستخرج الصفحات بعد أول 186 وقبل آخر 362.»;
  - nothing left: the field error «لا تبقى صفحات: الملف 555 صفحة والتجاوز 186 + 369.»
- The server check stays authoritative (`books/forms.py:151-161`).
- **The «التخطيط» step's first screen** (the other designer's, before «استخراج الصفحات») shows the exact range with
  the skip values editable, and ideally thumbnails of the first and last kept pages. It replaces today's sentence
  (`detail.html:218`).
- **The stage bar's hint** for «التخطيط» reads «7 من 555 صفحة».
- **Tests:** `kept_range`; the form's probe under Node, on byte fixtures (a found count, a missing one, two `/Count`
  values).

---

## 5. Narrow screens and legibility (D83–D84)

### 5.1 Review

**What exists today.**
- The breakpoints are viewport media queries at 1180 and 900 px (`static/src/components/review.css:527-541`).
- With the 232 px sidebar, a window of 900–1000 px leaves each pane about 330–380 px. That is about 220–270 px of text
  measure after the 72 px gutter and the padding: the tester's "200 px".
- At 420 px the top bar's controls do not fit: the approve button is cut off.

**Proposed.**
- The rail (§3.2) gives back 176 px.
- The screen becomes a container (`.review-screen { container: review / inline-size }`), and the rules follow its own
  width:
  - **≥ 880 px:** side by side at 1:1, as today (a fit-height scan). The text measure is at least about 360 px.
  - **< 880 px:** stacked, as the 900 px rule does today: the scan at 56 vh, then the lines.
  - **< 560 px (a phone):**
    - the top bar shows ☰, the stage menu, the compact pager «3/8» and «⋯»;
    - «⋯» holds the shortcuts, «نسخ نص الصفحة», «إدراج سطر», «تفاصيل المعالجة» and «إعادة فتح»;
    - «اعتماد الصفحة» moves to a sticky bottom action bar (48 px, 44 px targets), with «حُسمت 5 من 9 · محفوظ»;
    - the filmstrip hides; «⋯» → «الصفحات» opens it as a sheet;
    - the line gutter is 44 px and the padding 12 px.

```
┌───────────────────────────────────────┐
│ ⋯   › 3/8 ‹          [المراجعة 6/8 ▾] ☰ │
├───────────────────────────────────────┤
│              (scan, 56vh)              │
├───────────────────────────────────────┤
│   1  قال الأمير في سنة 1966            │
│   2  وَفِي الكتاب حكاية                 │
├───────────────────────────────────────┤
│ [ اعتماد الصفحة ]      حُسمت 5 من 9 · محفوظ │
└───────────────────────────────────────┘
```

### 5.2 The book page toolbar below 640 px

- The toolbar becomes a container (`.lo-toolbar`). Today it only wraps below 900 px (`layout.css:665-676`) and
  overlaps at 420 px.
- **Preview:** the mode control, one fit button that cycles (its current name in `title`, keys + − 0), the counter
  «37/412», and «⋯» with «الانتقال إلى صفحة…» (G) and the three fits. The spread is off below 640 px anyway
  (`stage.js:28`).
- **Edit:** the mode control, undo and redo, the style picker (its label cut to 8ch), B and I, «⋯» with «حاشية» and
  «فواصل الصفحات الأصلية», then «تم».

```
│ [⋯]  37/412   [⤢ الارتفاع]   [ تحرير | معاينة ] │
```

### 5.3 Labelled side tabs

- Today `.bp-tab-label` shows only on the active tab, and even that hides below 290 px (`layout.css:331-360`,
  `_book_side.html:6-14`).
- **Proposed:**
  - every tab shows its 16 px icon and a 12 px label;
  - the bar is `display: flex; flex-wrap: wrap; gap: 2px` with `flex: 1 1 auto` tabs, so it wraps to two rows at
    272–336 px;
  - badges sit inline after the label; the arrow keys (`stepTab`) are unchanged;
  - «تغييرات المراجعة» is the eighth tab, and appears only with drift (§1.5).

```
│ [¶ الفقرة] [▣ الأصل] [〰 غير المؤكَّدة 12] [⟲ تغييرات المراجعة 3] │
│ [≡ الفصول] [▤ الصفحات] [⌕ بحث] [⚙ التنسيق]                     │
```

### 5.4 Contrast and sizes (D84)

- **Colours.**
  - `--color-text-3` goes from #8a8a93 to #6b6b73 (`static/src/app.css:38`). It measures 5.28 on white, 4.93 on
    `--bg-subtle` and 4.68 on `--bg-muted`, where #8a8a93 measured 3.42, 3.20 and 3.03.
  - `.meta` (`:204`), `.help` (`:381`), placeholders (`:152`) and the 5 template uses of `text-text-3` follow.
  - A new `--color-text-disabled: #8a8a93` is used only for disabled controls (WCAG exempts them), for example
    `.input:disabled` (`:396`).
- **Sizes.** Today's CSS has 6 rules at 10 px, 4 at 10.5 px and 12 at 11 px. They move to 11.5 px, except `.kbd`,
  `.badge` and the filmstrip thumbnail numbers (an allowlist with its reasons).
- **A CSS test** reads the tokens from `app.css`, computes the contrast of `--color-text-3` on the three surfaces
  (≥ 4.5), and fails on a `font-size` below 11.5 px outside the allowlist in the built `static/dist/app.css`.

### 5.5 Tests

- The rendered templates carry the container classes and the phone action bar.
- Under Node, the pieces that depend on the width: the stage bar's collapse class from its measured width, the
  toolbar's overflow.
- The owner's pass at 1440, 1180, 1024, 900, 800, 560 and 420 px (in the acceptance list; no Chrome for the agents).

---

## 6. Interfaces with the other Phase 7 designs

- **Processing split** (owner: «التخطيط» with «استخراج الصفحات», then «المعالجة» with «بدء المعالجة»):
  - `book_stages` needs, from their page and book statuses, what "done", "running" and "attention" mean for
    `layout` and `processing`, and which of the two the dashboard is showing (`current`).
  - Their pre-extraction screen uses `kept_range` (§4.4).
  - Their dashboard can fold its status text and bar (`detail.html:20-26`) into the stage bar's «المعالجة» detail,
    which removes the finding "two measures in one place".
- **Layout review** (D64+): if it adds a regions check as a step, it is one more row in `book_stages`, and the bar's
  collapse rules already allow seven steps.
- **OCR trust and structure** (footnote role, verse):
  - readiness's `stray_notes` links to the block; their role or «حاشية» action is what fixes it;
  - their review changes (a role, a dropped line) reach an edited book through §1, as `review` drift.
- **Everyone:** new keys go into the §2.3 table, which `core/test_keymap.py` enforces.

## 7. Suggested build order (each step reviewed and tested before the next)

1. **The groundwork:**
   - D73 (`mergeNodes` unions the source marks);
   - the `base`, `plan` and `included.at` fields and their migrations;
   - `_mark_edited` on every write path;
   - drift reasons (§1.2);
   - the refused `reassemble` without the flag.
2. **`editor/merge.py`** with its pure tests (§1.3), then the reconstruction tier.
3. **Services, task and API** (§1.4), and the dev command.
4. **The book page:** the banner, the changes tab, the rebuild dialog, live drift (§1.5–§1.6); the channel posts from
   review.
5. **`keys.js`** and the four keymaps, the sheets, `core/test_keymap.py` (§2).
6. **`book_stages`,** the stage bar, the rail (§3.1–§3.2).
7. **Review's origin,** the detour, the end panel, the N rule; the tiles; the addresses (§3.3–§3.5).
8. **Vocabulary,** D80, readiness, the kept range (§4).
9. **Narrow screens and legibility** (§5).

Ownership:
- **Backend:** `editor/` (merge, services, api, tasks, models), `assembly/services.py` (settings, staleness),
  `publishing/readiness.py`, `books/services.py` (`book_stages`, `kept_range`, `primary_url`), `review/`
  (payload, views).
- **UI:** the templates, `static/src/js` (book/*, review, books, manuscript, stages, keys), the CSS and the Node tests.

## 8. Open questions for the owner

1. **Old edited books** («كتابي» today: 70 pages rewritten by the numbers pass, with no history to tell your edits
   apart). Should each differing paragraph start on «نصّي», so nothing changes until you pick? This is proposed, with
   per-page «خذ ما جاء من المراجعة». Or should it start on «المراجعة»?
2. **Moved keys on the book page:** spread S → V; fit 1/2/3 → + − 0; S means «فواصل الصفحات الأصلية» as in the
   manuscript. And on the dashboard, 1/2 → V. Agreed?
3. **The stage bar inside the top bar** (no height cost; it narrows to «المراجعة 6/8 ▾»), not a row of its own under
   it (always full labels, 30 px less page)?
4. **Retire «لوحة الكتاب».** The dashboard is named by its stage («التخطيط» / «المعالجة») and reached from the bar;
   the project is called by its title. Agreed?
5. **The icon rail on every book screen** (dashboard, review, manuscript, book, export): it gives review its room at
   900–1180 px. Agreed?
