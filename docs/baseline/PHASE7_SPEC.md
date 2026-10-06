# Phase 7 specification: «التخطيط» first, then «المعالجة»; a safe round trip; trust in the text

Draft for the owner's approval, 2026-09-26. Decisions D64–D79 below are proposed; nothing is built before the owner's
"go". Phase 7 comes in four sub-phases (7a–7d); each is built only after the owner's hand test of the one before.

Read first: `docs/baseline/UX_TEST_2026-09-26.md` (the evidence), `docs/DECISIONS.md` (D4, D17, D22, D26, D33–D39, D41, D47,
D49–D51, D63), `docs/baseline/PHASE6_SPEC.md` §7 (readiness), `docs/baseline/DASHBOARD_SPEC.md`, `DESIGN.md`, and the approved visual
reference: the review screen (`templates/review/review.html`, `static/src/components/review.css`,
`static/src/js/review.js`). Every `file:line` below was read on `main` at 02f108a. The Phase 6 review has since
committed dbd3f28, which touched only publishing files, `export.js` and the export templates. Its word-box fixes, still
in progress, move lines in `ocr/alignment.py` and `ocr/numbers.py`, so for those two files the agents find the code by
the snippet quoted here, not by the line number.

Every measurement was taken read-only on the dev database: 25 books (13 `ready_for_review`, 6 `reviewing`,
6 `assembled`), 230 stored pages of which 217 are in their books, and 9 manuscripts, all edited on the book page. The
suite collected 895 tests at 02f108a; the Phase 6 review adds its own.

**The owner's request (2026-09-26, authoritative).** "Separate «التخطيط» and «المعالجة». The book dashboard shows the
layout only in the first step; once you click «المعالجة» the same book dashboard is as it is today."
- The first stage is «التخطيط», and its action is «استخراج الصفحات». Then «بدء المعالجة» starts the stage «المعالجة»
  (OCR).
- «ضبط الأدلة» is dropped as a user-facing term.
- Detection usually works, so the default path is one glance and one click. A page's regions are edited, or a guide
  (the top, say) applied to all pages, only when needed.
- The book dashboard is reused. "Do it without breaking the logic."

**What Phase 7 is.**
- **7a** answers that request. It also fixes the three things the UX test found unsafe or unusable: a review fix
  after the book exists silently replaces a chapter, review's shortcuts fail with the Arabic keyboard layout, and a
  page with no flags reads as proofread.
- **7b** makes the flags honest (every flag has a reason, three readers decide) and gives footnotes and verse a line
  role.
- **7c** adds the stage bar, lets review lead back to where it was opened, and lets review changes enter an edited
  book paragraph by paragraph.
- **7d** follows 7c's hand test: the line audit, verse laid out as صدر and عجز, narrow screens and contrast.

**Being done now in the Phase 6 review.** Phase 7 builds on these and does not plan them again:
- the readiness rows `pages_unreviewed`, `pages_missing` and `stray_notes`, and the new `clear` rule, with the
  round-trip designer's wording (`publishing/readiness.py`; `stray_notes` opens the book page on the chapter,
  `?chapter=`, until 7c adds `?block=`);
- the Word builder's bidi direction runs (UAX #9 per paragraph) and its page-number fixes;
- fixes to the export pipeline, the PDF and EPUB exporters and the export page;
- fixes to the D63 word boxes: page numbers leaking into the text, numbers pulled to the next line, and weak boxes
  against the numbers pass.

The review also found the PDF text-layer trade-off, which is owner question 1 (§12). Its fix is built in 7b, or in the
review itself if the owner answers before the review closes.

## 0. Decisions (D64–D79)

- **D64: two stages with a pause, and a flag whose default is today's path.**
  - «التخطيط» is extraction (ingest) and preparation: split, deskew, crop, clean, gray and b&w, and the detection of
    the lines, the footnote rule, the smaller-type block and the page number. It stops where `needs_guides` always
    meant to stop. Every page is then `preprocessed`, the book reads «تم التخطيط», and no `Region` rows exist yet.
  - «المعالجة» is the regions, Tesseract, Qari, finalisation, the numbers pass and everything after. It starts only at
    «بدء المعالجة».
  - The switch is a new field, `Book.awaits_ocr_start` (default False). `create_book` sets it; «بدء المعالجة» clears it;
    nothing else writes it. When it is False, every function takes exactly today's path.
  - Reason: the owner's request, made "without breaking the logic". With a default of False, three groups keep
    today's path with no data migration: the 25 existing books; the test fixtures, which create books directly
    (`ocr/tests.py:54`, `processing/tests.py:430`, `books/tests.py:335`); and the ~110 test lines that call
    `run_stage`, `rerun_book`, `after_preprocess` or the OCR tasks. The alternative, `ocr_started_at = null` meaning
    «التخطيط», would have paused every one of them.
  - This amends the Phase 2 rule "the book never waits for guides" (`books/tasks.py:68–70`). Waiting is now the
    normal, cheap first step of a new book.
- **D65: the names of the stages, statuses and re-run steps.**
  - Stage «التخطيط» (action «استخراج الصفحات»), then «بدء المعالجة» and the stage «المعالجة».
  - Book statuses: `processing` «قيد التخطيط», `needs_guides` «تم التخطيط», `ocr` «قيد المعالجة».
  - Page statuses: `preprocessed` «مُجهَّزة», `layout_done` «بانتظار التعرّف».
  - Re-run steps: `preprocess` «تجهيز الصفحات», `layout` «تحديد المناطق». The region source `guides` becomes
    «من التخطيط».
  - No user-facing text says «الأدلة» or «ضبط الأدلة» any more.
  - Stored values and code names stay (`needs_guides`, `guides`, `LayoutGuides`, `guides_override`, the stage keys), so
    rows, URLs and tests keep working.
  - Reason: the owner's names, and one meaning per label. Today «تم التخطيط» is already the page status
    `layout_done` (`books/models.py:168`), and «التخطيط» is also the name of a re-run step inside the OCR chain
    (`books/services.py:65`).
- **D66: the upload extracts at once, and the kept page range is shown before anything is queued.**
  - The form's button is «استخراج الصفحات». It creates the book and starts extraction in the same request.
  - Under the skip fields, the form shows which PDF pages will be extracted. The browser reads the page count from
    the file when it can (26 of 40 of the owner's PDFs); otherwise the form states the rule in words.
  - The success message, and the «التخطيط» side panel next to «حذف الكتاب», give the exact range.
  - Reason: the UX test. A 555-page file with skip values 186 / 362 keeps 7 pages, and a typo would queue hundreds.
    There is also no delete today (`books/urls.py`), so a wrong upload could only be removed by the admin.
- **D67: the «التخطيط» mode of the dashboard.**
  - While a book awaits «بدء المعالجة», its dashboard opens on the grid. Every page shows its prepared image with its
    bands (running head, text block, footnotes, page number), and an amber mark flags the pages worth a look.
  - A click opens the viewer to fix that page: move, add or remove its running-head cut and its footnote start,
    «ليس رقم صفحة», «التلقائي», «استثناء».
  - The side panel sets a running head, or a footnote line, for all pages, with a preview and undo.
  - The bands come from the same function that `layout_page` runs at «بدء المعالجة», so what the owner sees is what
    OCR reads. Edits during «التخطيط» only save values.
  - After the start, the dashboard is exactly today's. The same mode opens from «⋯» «التخطيط» (`?view=guides`); there
    each change is saved explicitly and re-reads its page.
  - The guides screen goes, and its URL redirects.
  - Reason: the owner's words ("the dashboard shows the layout only in the first step"; «ضبط الأدلة» is dropped), with
    the critic's cuts: no suggestions to accept, no gray/b&w toggle, no book-wide page-number zone, and no draft bands
    on every thumbnail.
- **D68: thick separator rules are accepted at detection, strict first.**
  - `detect_footnote_rule` accepts a strict rule when there is one: mean thickness ≤ max(3, 0.3 × line height), as
    today.
  - Only when there is none, it takes the widest component with thickness ≤ 0.5 × line height and fill ≥ 0.6.
  - Measured on all 230 stored pages: book 25's pages 1 and 3–7 gain their rule (6.5–7.1 px against a 6.0 px limit),
    and nothing else changes.
  - A single "widest wins" test over both kinds would move book 16 page 2's rule from y 1967 to y 1839.
  - Reason: this is the one change to the text 7a makes, and it applies where nobody decides. The owner said
    detection usually works, so there are no suggestions to accept.
- **D69: one keymap, matched by the physical key.**
  - Letters are matched by `KeyboardEvent.code`. Digits are accepted in any script (0–9, ١–٩, ۱–۹). IME composition
    is ignored. Each bare key has one meaning across screens.
  - Review has a word mode and a page mode. While the word menu is open, letters and digits edit the word. While it
    is closed, A, E, N and the other letters are commands. ⌘↵ approves the page from anywhere.
  - Moved keys:
    - on the dashboard, the view moves from 1 / 2 to V;
    - on the book page, the spread moves from S to V, and the fits from 1 / 2 / 3 to + − 0;
    - S means «فواصل الصفحات الأصلية» everywhere.
  - Reason: UX blocker 3. The Arabic layout types ش, ث and ى where A, E and N are, and with a word selected an Arabic
    letter starts a correction. Owner question 4.
- **D70: no silent chapter replacement; approval is not drift; drift is live; a merge keeps both source marks.**
  - Rebuilding a chapter from review over an edited text needs `replace_edited` and a confirmation that names what is
    lost, as D49 already requires for the whole book.
  - Approving a page whose lines did not change is no longer announced as «تغيّر نص … بعد التحرير». The manuscript's
    amber mark follows the page's live status.
  - The book page refreshes its drift on focus, on visibility, on a return from the back-forward cache, and on a
    message from the review screen.
  - Merging two paragraphs with Backspace on the book page keeps both paragraphs' `sourcePages` and `sourceLineIds`.
  - Reason: UX blocker 1, where a chapter rebuild dropped the owner's heading (8 → 6 pages). Book 26 shows 5
    approval-only pages. The page-by-page merge (D78) needs complete source marks.
- **D71: flag policy v2. Every flag has a reason, punctuation is never flagged, and three readers decide.**
  - A Western number that all three readers read alike is sure (amends D17).
  - Where the two models disagree and Tesseract backs Qari v0.2, v0.2's reading goes into the text but the word stays
    open. This activates D26 and amends it: the vote leaves the word unresolved.
  - Foreign letters and stray symbols are flagged.
  - Reason: measured on the UX books 23 + 25, flags go from 275 to 144 and precision from 33 % to 62 %, while the
    catch rate goes from 63 % to 61 % (65 % with the single-reader flags of D73). The right reading is already in the
    text for 76 % of the disagreements, against 40 % today. Owner question 2.
- **D72: words only the second model read are no longer thrown away.**
  - A run of such words that Tesseract supports (≥ 70 % of its words) enters the text as one marked group. The
    reviewer keeps it or drops it in one step.
  - An unsupported run becomes a suggestion between two words. It is never text.
  - No new model calls.
  - Reason: the dropped line of book 23 page 1 (UX blocker 4). On 217 pages there are 104 suggestions. On reviewed
    pages, 8 of about 10 were real, and the reviewers had missed 6 of those 8.
- **D73: honest page state.**
  - A page read by one model says so («قراءة واحدة», «نص Tesseract وحده») and gets flags against Tesseract.
  - The clean start of a looped reading counts as a second reading.
  - A page with no flags reads «لا علامات», never «حُسمت 0 من 0». This part lands in 7a.
  - Approved pages leave the attention list.
  - Reason: page 23/8 had 14 real errors and 0 flags because Qari v0.2 looped. Proofreaders read "0 uncertain" as
    "proofread".
- **D74: footnote and verse are line roles, and a note without a marker continues the previous page's note only when
  it should.**
  - Review's line menu, and a Shift-click range, offer «حاشية» and «شعر». The manuscript's paragraph menu offers «شعر»
    and «حاشية للعلامة (n)».
  - A verse line is never glued to its neighbour.
  - Reason: book 25 found 1 of its 8 notes (on 6 footnoted pages), and no footnote role exists today
    (`review/services.py:786–787` refuses headings on footnote lines). Verse hemistichs were merged into the wrong
    paragraphs (book 23 page 5).
- **D75: readiness names doubtful text.**
  - Two rows join the Phase 6 review's: `missing_text` (suggested words nobody decided) and `single_reader` (pages in
    the book that one model read and nobody has reviewed yet).
  - Reason: «قبل الإخراج» must not read «جاهز» over text nobody checked.
- **D76: a stage bar, and navigation that leads to the work.**
  - A stage bar in the top bar of every book screen: «التخطيط · المعالجة · المراجعة · المخطوطة · الكتاب · الإخراج».
  - The icon rail on book screens.
  - Review knows where it was opened from and returns there, and names the next step after the last page.
  - Thumbnails open review.
  - Addresses keep their tab and block.
  - Reason: UX "Navigation between stages" (all major findings).
- **D77: one term per concept.**
  - «تجميع المخطوطة» replaces «تحويل إلى كتاب».
  - «الكتاب» means only the typeset book; «لوحة الكتاب» is retired.
  - Pagination is «ترتيب الصفحات», so «الإخراج» means files only.
  - Counts always use the Arabic count helper.
  - Reason: the UX test's naming findings. Owner question 5.
- **D78: review changes enter an edited book page by page.**
  - The manuscript keeps the assembled text its edits started from (its base).
  - A word-level three-way merge of the base, the book page's text and the fresh assembly takes only the paragraphs of
    the changed pages. The owner sees and applies them in «تغييرات المراجعة», with a snapshot and undo.
  - Books edited before 7c have no base, so each paragraph that differs is the owner's choice, and it starts on
    «نصّي».
  - Reason: UX blocker 1 and top item 1. Replayed on the two incidents, the merge takes exactly the one changed
    paragraph and keeps every book-page edit.
- **D79: «تصحيح في كل الكتاب».**
  - One correction applied to every occurrence in review, with a preview and a batch undo.
  - For an edited book, it hands over to the book page's find & replace, pre-filled.
  - There is no learning and no propagation.
  - Reason: «السعودي» for «المسعودي» occurs 160 times on 51 pages of «كتابي» (117 flagged one by one, 43 not flagged
    at all).
- **D80 and later: 7d, recorded as built.**

## 1. Scope

| Step | What is in it | When | Risk |
|---|---|---|---|
| 7a | The owner's request: the split with the D64 flag, re-runs, exclusions and task gates; the «التخطيط» mode (§3.12) and `?view=guides` for books already started, with an explicit save; the old guides screen redirects; thick rules at detection (D68); the upload's «استخراج الصفحات», the kept range and «حذف الكتاب»; the labels of D65. Also: one keymap by physical key, including the dashboard's V (D69); round-trip safety (D70); «لا علامات» (D73's first part); a confirmation for book-wide re-runs; undo for «استثناء الصفحة» | after "go" | medium, contained by the flag's default. The only text change is book 25's footnotes, and only when a book is prepared again |
| 7b | Trust, mostly backend and checked by replaying stored data: flag policy v2 with reasons and the vote (D71); second-model-only words as groups or suggestions (D72); single-reader flags, a minimal `Page.reading`, a pill and banner in review, and the attention list clearing on approval (D73); footnote and verse line roles with a line range, the manuscript's «حاشية للعلامة (n)», and the continuation guard (D74); the readiness rows (D75); `rebuild_lines --report` before any write; the PDF text-layer fix (Q1). Optional, last: years against the number in words | after the owner's test of 7a | medium: words enter the text before review. Measured on stored runs first |
| 7c | Navigation and the full round trip: the stage bar, the icon rail, review's origin and detour, the end-of-review panel, the N rule, tiles that open review, `?tab=` and `?block=`, the names of D77 (D76, D77); the merge with a stored base, the word-level three-way merge, the «تغييرات المراجعة» tab, `choose` for legacy books, approval-only changes absorbed (D78); fix everywhere with a batch undo (D79) | after 7b's test | highest: it touches the edited text. Contained by the plan-then-apply step, the snapshot and the byte-identity gate |
| 7d | After 7c's hand test: the line audit and re-reading of looped regions (GPU); verse detection with one بيت per paragraph first, then flex halves in HTML and PDF; review stacking below 880 px, the book toolbar's overflow, labelled side tabs, contrast with split colour tokens | after 7c's test | GPU cost and layout |

**Not in Phase 7:**
- **Out of scope for now.** Phone layouts (a phone top bar, a bottom action bar). Tables (PLAN "Later").
- **Cut from the designs as over-engineering.**
  - Remembered corrections, learning from repeats, and propagation to unreviewed pages.
  - The text-assisted footnote zone. If it ever returns, it must be a detection source after the smaller-type block
    in `resolve_layout`, never written into `guides_override`, and it must skip pages whose override has
    `footnote_line`.
  - Word verse tables; EPUB verse beyond the centred lines it has today.
  - The agreement-percentage pill.
  - Rebuilding a legacy base from `LineRevision.before`, and the 80 % text-containment rescue.
  - The 30 s and 60 s polls.
  - The rule suggestions, the gray/b&w toggle, the keys B / H / F and ⌘Z in «التخطيط».
  - Draft bands on every thumbnail, and the page-number zone for all pages.
  - The doubts that never fired on the 217 pages (`footnote_from_book`, `large_skew`).
  - Free rectangles, "ignore" zones, two columns, and changing a band's kind by hand.
- **Needs its own decision later.** The Kraken + Qari v0.2 confirmation key; a vowelled-books option; starting
  «بدء المعالجة» early on 800-page books; simplifying the page detail.
- **Against an owner decision.**
  - A time estimate on «قيد المعالجة». D22 rejected time-remaining estimates, so this plan does not add one, even
    though the critic listed it as missing. The re-run confirmation does state the model time before a costly action
    (§3.6); that is a cost, not a progress estimate.
  - The round-trip design's "D80" («تضمين الصفحات غير المُراجَعة» off by default), unless the owner answers question 3
    the other way.

## 2. Evidence

### 2.1 The UX test and what answers each finding

| Finding (`docs/baseline/UX_TEST_2026-09-26.md`) | Answered by |
|---|---|
| Blocker 1: a review fix after the book exists wipes book-page work; the banner appears only after a reload | 7a D70 (confirmation, `replace_edited`, live drift, source marks); 7c D78 (page by page) |
| Blocker 2: restore and undo undid themselves | fixed on 2026-09-26 (2a0bca2). Every new toast action in Phase 7 binds a boolean `hasAction` |
| Blocker 3: shortcuts fail with the Arabic layout; an Arabic letter starts a correction | 7a D69 |
| Blocker 4: a dropped line with no flag; «0» uncertain pages with a dozen errors; 70 % of book 25's flags are punctuation or digits; dates wrong and unflagged | 7a «لا علامات»; 7b D71–D73, D75; the optional year check |
| No stage bar; review links only to «لوحة الكتاب»; nothing says what comes next; export only in «⋯» | 7c D76 |
| «تحويل إلى كتاب», «الكتاب» twice, «يُخرَج» against «الإخراج» | 7c D77 |
| Grid thumbnails open the technical page | 7c D76 (`primary_url`) |
| `?tab=` dropped from the address | 7c D76 |
| «تضمين الصفحات غير المُراجَعة» checked by default | owner question 3 (recommended: keep D35 and show the count on the button, 7c) |
| «قبل الإخراج» ignores unreviewed pages and stray «(1) …» notes | the Phase 6 review (done); 7b adds `missing_text` and `single_reader` |
| The upload form never shows the kept pages; processing needs a second click | 7a D66 (the range; extraction on upload; «بدء المعالجة» after one look) |
| Two measures in one place, no time estimate | 7a: in «التخطيط» the chip and the bar measure the same thing (prepared pages); 7c folds the rest into the stage bar. No time estimate (D22) |
| Footnote detection weak; no footnote role | 7a D68; 7b D74 |
| Verse glued into the wrong paragraphs; «شعر» only centres | 7b D74 (the role keeps verse lines apart); 7d (بيت layout) |
| Page detail is an engineer's screen; re-runs without confirmation | 7a (the confirmation); 7c renames it «تفاصيل المعالجة» and keeps it secondary |
| Side tabs icons only; narrow widths; contrast | 7d |
| Screen PDF text layer garbled with Traditional Arabic | owner question 1; fix in 7b |
| Minor: English file picker; «8 صفحة»; the view shared by all books; the attention list never clears; after A, N skips one; one key, several meanings; «استثناء الصفحة» has no undo | 7a (file picker, keys, exclusion undo); 7b (attention list); 7c (counts, per-book view, the N rule) |

### 2.2 Layout and the pipeline (dev database, 2026-09-26)

- **Book 25 (الحوليات الليبية, 7 pages).**
  - No rule was accepted on any page. The separator lines are 6.5–7.1 px thick, against a limit of
    `max(3, 0.3 × med_h)` = 6.0 px (`processing/pipeline.py:361`). Here `med_h` is the core band of the detected lines,
    19.5–23 px and mostly 20.
  - Their fill (area ÷ box) is 0.65–0.88. Page 1's rule, for example: y 2389, 732 px wide, 7.1 px thick, fill 0.79.
  - Rules are on pages 1, 3, 4, 5, 6 and 7; page 2 has no footnote.
  - Page 6's footnotes were found from smaller type only. On the other five pages the whole footnote text was read as
    body. Book 25 found 1 of its 8 notes (the UX test counted 1 of 6 footnoted pages).
- **Replay of D68 (by the writer, `rule_check.py`).** The strict-first rule was run on every stored page's `gray.png`
  with its stored line boxes and line height.

  | Page | Before | After | Thickness | Fill | Width | `med_h` |
  |---|---|---|---|---|---|---|
  | 25/1 | – | 2389 | 7.1 | 0.79 | 732 | 20 |
  | 25/3 | – | 2683 | 7.1 | 0.88 | 713 | 23 |
  | 25/4 | – | 2866 | 6.9 | 0.69 | 730 | 20 |
  | 25/5 | – | 2138 | 6.8 | 0.68 | 706 | 19.5 |
  | 25/6 | – | 2850 | 6.5 | 0.65 | 729 | 20 |
  | 25/7 | – | 2760 | 7.0 | 0.87 | 745 | 20 |

  - No other page of the 230 changes. Book 20's thick components are closed text rows with fill ≤ 0.56, and stay
    refused.
  - Replacing strict-first with one "widest wins" test over both kinds moves book 16 page 2 from y 1967 to 1839. That
    page is a table page with 3 px rules and one 4.0 px rule. Hence strict first.
- **State of the books.**
  - Regions are only `body`, `footnote` and `page_number`, all from `guides`. No page override is in use.
  - One book has manual guides: book 5, with its footnote line at 85.6 %, which cuts no line.
  - Today's `page_region_specs` equals the stored regions on 220 of the 230 pages. The other 10 belong to the
    smoke-test books 1, 3 and 4 (3 stored regions against 1–2 computed): Phase 2 code derived them before detection
    became per-page. They are harmless, and the 7a gate lists them instead of failing on them.
- **Pages worth a look** (the kept doubts of §3.9, recomputed by the writer on the 217 included pages from stored
  data): 4 pages today, 3 after the rule change.
  - 25/6 `footnote_from_type` disappears once its thick rule is accepted.
  - 16/2 and 16/3 `line_cut`: a born-digital table page, where the detected rule runs through a detected table row.
    These are worth the look.
  - 19/62 `no_lines`. The owner had already excluded 12 other pages without lines, 11 of them in book 19.
- **Timings.**
  - UX test: the first page is ready 40 s – 2 min after «بدء المعالجة»; a whole 8-page book takes 4–6 min, with three
    books running at once.
  - Mean model-call durations over all stored runs on this Mac: Qari v0.3 8.9 s and Qari v0.2 6.6 s per region,
    Tesseract 0.5 s, Kraken 2.6 s per page.

### 2.3 The round trip (dev database, and the prototypes)

- **The two incidents, replayed with a page-scoped three-way merge.** The base was rebuilt from the review history,
  for the prototype only.
  - Book 26 (run 41, snapshot 39, page 3): one paragraph is taken («بزعَ» → «بزغَ»). The other six paragraphs of the
    page are identical. The heading the owner made on page 1 and a page-1 edit are untouched. The whole-chapter rebuild
    had dropped that heading (8 → 6 pages).
  - Book 23 (run 37, snapshot 34, page 8): one paragraph is taken («فقيها، فاضلا، زاهدا» → «فقيهاً، فاضلاً، زاهداً»).
    The owner's inserted heading «دولة بني الأغلب في طرابلس» and the edits on pages 1 and 5 (one with a footnote) are
    untouched.
  - Word tokens must be compared, not node JSON: TipTap splits text nodes and adds null attributes, so every block of
    book 26 looked "changed" by JSON.
- **Today's drift is mostly not text** (recomputed by the writer):
  - book 26: pages 4–8, all approval-only (the lines did not change);
  - book 19 («كتابي»): 70 pages of content drift and no review revision after the run. The numbers pass rewrote the
    lines (D50–D51);
  - the other 7 edited books: no drift.
- **Book-page edits already blur the source marks.**
  - A split copies the source lines to both halves (`static/src/editor/convert.js:446–461`); book 13 has 21 lines
    claimed twice.
  - A merge keeps only the first block's lines (`convert.js:465–472`). 13 lines of book 13 and 7 of book 25 are
    claimed by no block.
- **Size of a manuscript.** Book 19's document is 868 KB as Django sends it (450 KB of text). `manuscript_of(lock=True)`
  loads the whole row on every 1.5 s autosave, so a stored base must be deferred on the hot paths.
- **The PDF page count in the browser.** The probe reads the first and last 2 MB of the file and takes the largest
  `/Count` of a `/Type /Pages` dictionary. On the 40 Arabic PDFs in `~/Documents/Books`: 26 exact, 14 not found
  (compressed object streams), none wrong.
- **Contrast.** `--color-text-3` #8a8a93 measures 3.42 : 1 on white, 3.20 on `--bg-subtle` and 3.03 on `--bg-muted`.
  #6b6b73 measures 5.28, 4.93 and 4.68 (7d).

### 2.4 Trust (stored runs and review outcomes, no model calls)

**Method.**
- For every approved page, the lines the reviewer first saw are rebuilt: the `before` of each line's first live
  `LineRevision`, untouched lines as stored, deleted lines from the delete revisions, and inserted lines marked
  `is_manual`.
- They are aligned word by word with the approved lines.
- A flag is *useful* when the reviewer changed the token. An error is *missed* when the reviewer changed a sure token.
- A flagged token left untouched on a force-approved page is *unknown* and excluded (170 tokens, mostly book 26).

| Measure | Today | Policy v2 (D71) |
|---|---|---|
| UX books 23 + 25 (4,526 words) | 275 flags, 33 % useful, 63 % of errors caught | 144 flags, 62 % useful, 61 % caught (65 % with the single-reader flags) |
| 14 reviewed books (14,063 words) | 675 flags, 43 % useful, 75 % caught | 518 flags, 54 % useful, 74 % caught |
| Punctuation | 95 flags, 89 of them correct marks; comma against full stop: 0 errors in 48 | never flagged |
| Western numbers | all 184 flagged | 70 are sure (all three readers agree), and none of them was wrong |
| The reading in the text where the models disagree (177 cases) | always Qari v0.3 (right 40 %) | the one Tesseract backs (right 76 %). Tesseract with v0.2: v0.2 won 88 of 116. Tesseract with v0.3: v0.3 won 46 of 61 |
| A word Qari v0.2 lacks | flagged | Tesseract read it: 27 correct, 1 changed (so sure). Tesseract did not: 29 correct, 21 changed (so flagged) |
| Tesseract as a flagger where both models read | – | errors 16 times in 752 (2 %); with a different dotless skeleton at conf ≥ 70, 9 in 215 (4 %). Not a flag |
| Differences hidden by the lenient comparison (tanween, hamza, ة/ه, shadda) | not flagged | not flagged: the approved text kept v0.3's form in 303 of 310 |

- **One reader, no flags.**
  - `select_text` keeps an alternative only when the second model passes its sanity check (`ocr/services.py:1051`),
    so a looped v0.2 removes every flag from its region.
  - Regions read by one model: 21 + 4 (fallback) of 185 body regions, 16 + 6 of 81 footnote regions; in «كتابي», 13
    of 81 pages.
  - Page 23/8 has 290 words, 14 errors and 0 flags. The clean start of the looped run had already read «يحيى»,
    «فقيهاً», «فاضلاً», «زاهداً» and «يقرىء» correctly. Looped prefixes cover 0–100 % of their region, median about
    45 %.
- **Dropped text.** The main cause is homoeoteleuton: the model jumps from one repeated phrase to the next.
  - 23/1 dropped «تعالى بطرابلس ونشأ بها واخذ عن جماعة من الفضلاء وكان رحمه الله». v0.2 had the whole line.
  - 19/61 dropped 11 words between two occurrences of «على أهل كل قرية ما».
  - 19/87 dropped «وفيها مغاص اللؤلؤ المعروف بالخاركي،»; 19/52 dropped a whole line.
  - Words that only the second model read, on reviewed pages, after the filters: 23/1 (the line, and «الاجابة»); 25/6
    («LAMPEDOUSE», «D’ANFREVILLE», «(1) انظر :»); 25/7 («DE POINTS», «فقد»); 26/1 («وهُما»). Six of those eight are
    still missing from the approved text.
  - Printed bands that no line covers: 69 on 46 of 180 pages. 33 lie beside a merged line, 23 are likely dropped lines
    and 13 are page tops (7d).
- **Foreign scripts in stored tokens.**
  - Cyrillic ×7, including «مилادية» and «بزنзор», both stored as sure words.
  - CJK ×6 («وال德拉ية», «الأ石油化工»).
  - «※», «^» for «٧» (×6), and «© ¢ € ¥ ≡».
- **Years (book 23).** Years are printed in digits and again in words («سنة ( ٢٤٢ ) اثنتين واربعين ومايتين»). The
  words give the right value for 10 of the 14 wrong years and confirm 4 correct ones.
- **Repeated misreadings.** «السعودي» appears 160 times on 51 pages of «كتابي», against 103 «المسعودي».

### 2.5 Structure

- **Footnotes.**
  - Book 25: once the rules are accepted (D68), 7 of its 8 notes are found. Six are linked correctly; one wrongly
    continues the previous page's note, which the continuation guard (D74) fixes.
  - Book 23 page 5 has one footnote line in smaller type after a 184 px gap, with no rule. `BLOCK_MIN_LINES = 2`
    (`pipeline.py:58`) refuses it. With the text zone cut, the owner fixes it in «التخطيط» (drag «+ حاشية») or with
    the «حاشية» role (7b).
  - Line-start markers alone are unsafe: book 26 has lists «١ – …», and book 19 has numbered sub-headings
    «(١) بلاد فارس».
- **Verse, book 23 page 5.**
  - The page is staggered: a right-aligned صدر (x 565–1217 of 1279), then a left-aligned عجز (x 97–743). Every عجز ends
    in «ر».
  - `breaks_between` (`assembly/pipeline.py:900`) never breaks between an عجز and the next صدر, so the text reads
    «الى الهياج؛ ونار الحرب تستعر وفي يدي صارم، افري الرؤوس به».
  - Detection by staggering and rhyme finds 6 bayts (rhyme «ر» 100 %) and no false verse on 16 books.
  - WeasyPrint 70: `display:grid` puts the صدر on the left in RTL (wrong); flex with `space-between` and 46 % halves is
    right (7d).

### 2.6 Checked against the code: what the designs would have broken

| Found by the critic, verified | How this plan avoids it |
|---|---|
| A gate default of "not started" pauses every book not made by `create_book`: the fixtures and ~110 test lines | D64: `awaits_ocr_start` defaults to False; no data migration, and no books → ocr migration dependency |
| Freezing regions inside `_derive_regions` also freezes `rerun_preprocess` (`processing/services.py:217–246`) and layout re-runs, leaving regions in the old image's pixels | pages with review work are skipped only where guides change: `apply_guides`, `apply_book_guides`, `set_page_guides` (§3.10) |
| Rule candidates merged into "widest wins" move book 16 page 2's rule | strict first (D68) |
| «بدء المعالجة» with no prepared page: `refresh_status` returns early (`books/models.py:113`), the book goes to `ocr`, nothing is queued, and the poll runs forever | `start_ocr` refuses with «لا صفحات جاهزة للمعالجة» (§3.5) |
| Merge plans stored as `AssemblyRun` rows would show «يجري التجميع» in readiness and block chapter rebuilds (`editor/services.py:616`) | plans live in their own table; an apply writes one `done` run, as `_save_chapter_run` does (§5.6) |
| A legacy `insert` can duplicate text, because merges made before the D70 fix kept only the first block's lines (13 lines lost in book 13, 7 in book 25) | with no base, a fresh block without a partner on a page that is not `added` becomes `choose` on «نصّي» (§5.6) |
| Copying the current document into the base on restore drops review changes silently | restore sets base = the snapshot's base, or null for older snapshots (§5.6) |
| Approval-only drift "absorbed at the next merge" never flips D36 with no merge, and keeps D35's amber mark (`assembly/render.py:282`, `manuscript.css:207`) | 7a stops announcing it and draws the mark from live page status; 7c absorbs it at the next apply |
| The vote could touch `digit` tokens and D51's lone «ا/ه/ع» candidates | the vote skips them; `ocr/test_numbers.py` must give the same tokens with the vote on |
| `rebuild_lines` on the 9 edited books (none has a base) floods the round trip with `choose` items (77 unreviewed pages in «كتابي») | 7b rebuilds only books without an edited manuscript, unless `--include-edited` |
| `--color-text-3` also colours the approved provisional gray and shimmer (D28/D29, `theatre.css:16, 23, 555–560`) | 7d splits the token: `--color-provisional` keeps #8a8a93 |
| The round-trip design's "D80" (unreviewed pages off by default) silently reverses D35 | owner question 3 |

Checked and fine: the book statuses (13, 6, 6); `_enqueue_layout` and the `run_stage` reset paths; an excluded page in
«التخطيط»; re-deriving regions inside `run_full_ocr`; `approve_page` using `QuerySet.update`, so approval leaves the
signature alone; and `Line.region` being `SET_NULL`, so the footnote attribution loss on a guide change is real.

**Where this plan departs from the critic, and why** (everything else follows the critic):
- **No time estimate on «قيد المعالجة».** The critic lists it as missing, but D22 is the owner's decision against
  time-remaining estimates. The re-run confirmation states the model time instead (§3.6).
- **Fix everywhere moves an edited book's baseline only after the book-side replace** (§5.7), not on the fix alone.
  Moving it on the fix would lose the fix for good if the owner never replaced the word in the book text.
- **Pages worth a look.** Recomputed from stored data, there are 3 pages after D68, not the 1 that the layout design
  counted: book 16's table pages 2 and 3 have a rule through a detected row (§2.2).
- **The 7a replay gate** compares against the 220 pages where today's derivation already equals the stored regions.
  10 pages of the smoke-test books differ before 7a (§8.2).
- **The text-assisted footnote zone.** The critic's §1.3 defers it to 7d "if kept", while its §3 puts it out of
  Phase 7. This plan follows §3, with §1.3's constraints recorded for a later return (§1).

The designs' line references were rechecked. The alignment rule moved after D63. At 02f108a the secondary alignment
is at `ocr/alignment.py:762–773`, where `if i is None: continue` drops the second model's extra words, and the
confidence rule `low = digit or disagree[i]` is at `:1088`. In the review's work in progress they sit at about 775 and
1137. The Tab toast of review is at `review.js:362`.

## 3. 7a: «التخطيط» first, then «المعالجة»

### 3.1 What the owner sees

```
new-book form ─«استخراج الصفحات»─► dashboard in «التخطيط» (grid) ─ one glance ─«بدء المعالجة»─► today's dashboard
                                   pages appear, bands drawn,          (fix a page or all pages
                                   «قيد التخطيط · 3 من 7 صفحة»          only when needed)
```

- The clicks from upload to text stay two. Today they are «إنشاء الكتاب» + «بدء المعالجة»; now they are
  «استخراج الصفحات» + «بدء المعالجة». The second click now comes after a look at the layout.
- A book nobody touches is read exactly as today: at «بدء المعالجة» its regions are derived by the same function, from
  the same detection, with inert guides.

### 3.2 The pause flag and the book status (D64)

`books.Book.awaits_ocr_start = models.BooleanField("بانتظار «بدء المعالجة»", default=False)`.
- `create_book` (`books/services.py:119`) sets it to True. The upload form and the smoke command both go through
  `create_book`; fixtures and scripts that call `Book.objects.create` do not, and keep today's path.
- The claim in `start_ocr` sets it to False (§3.5). No other code writes it.

`Book.refresh_status` (`books/models.py:100–152`) gets one branch in front of today's code:

```python
if self.awaits_ocr_start:                         # «التخطيط» (D64): nothing is read before «بدء المعالجة»
    if counts[ps.UPLOADED]:
        new_status = self.Status.PROCESSING       # «قيد التخطيط»: pages are still being prepared
    elif counts[ps.PREPROCESSED] or counts[ps.LAYOUT_DONE]:
        new_status = self.Status.NEEDS_GUIDES     # «تم التخطيط»: waits for «بدء المعالجة»
    else:
        new_status = self._settled(counts, total) # every page failed → error (ALL_PAGES_FAILED_LAYOUT)
else:
    ...today's lines 118–145, unchanged...
```

- `_settled` is today's `pending == 0` block (`:129–137`), extracted into a method.
- **Error messages.** In «التخطيط» the all-pages-failed message is a new constant, `ALL_PAGES_FAILED_LAYOUT`
  (text in §3.14). Books in «المعالجة» keep today's `ALL_PAGES_FAILED`.
- **The early return.** The early return for an ingest error (`:115`) compares against
  `ALL_PAGES_FAILED_MESSAGES = (ALL_PAGES_FAILED, ALL_PAGES_FAILED_LAYOUT)`, so both errors are re-derived.
- **Lifecycle.**

```
            «استخراج الصفحات»                          «بدء المعالجة»
uploaded ─────────────────► processing ─────────► needs_guides ──────────► ocr ─► ready_for_review ─► reviewing ─► assembled
 «مرفوع»                    «قيد التخطيط»          «تم التخطيط»            «قيد المعالجة»   (today)
                              │  ▲                    │  ▲
                              │  └── «إعادة تجهيز ◄───┘  │   (a page or the book prepared again, a page re-included)
                              │        الصفحات»           │
                              └── every page failed ─► error («إعادة استخراج الصفحات»)
awaits_ocr_start: True ─────────────────────────────────┘ False ─────────────────────────────────────────►
```

`ACTIVE_BOOK_STATUSES` is unchanged (`processing`, `ocr`). The dashboard polls while pages are prepared and stops at
«تم التخطيط».

**Concurrent refreshes.** In «التخطيط» each `preprocess_page` task refreshes the book (§3.3). Two tasks that finish
together could otherwise write a stale status. So the task's refresh runs inside `transaction.atomic()` on
`Book.objects.select_for_update().get(pk=…)`: the second refresh counts after the first one has committed.

### 3.3 Chains, tasks and the gate

| Starting point | While the book awaits the start | After «بدء المعالجة», and every book with the flag False (today) |
|---|---|---|
| ingest (`start_processing`) | ingest → chord(preprocess ×N) → `after_preprocess(continue_ocr=False)` → pause | ingest → chord → `after_preprocess(continue_ocr=True)` → `_enqueue_layout` |
| `preprocess` (page or book) | `preprocess_page` only; the task refreshes the book | preprocess → layout → ocr_fast → ocr_full |
| `layout`, `ocr`, `ocr_fast`, `ocr_full` | refused: «لم تبدأ المعالجة بعد؛ اضغط «بدء المعالجة» أولًا.» | today |
| «بدء المعالجة» | claim → `start_ocr_task` → `_enqueue_layout` | refused: «بدأت المعالجة بالفعل.» |

- **`_stage_signatures(page_id, stage, layout_stage=False)`** (`books/services.py:472–484`). With `layout_stage`, the
  only entry is `"preprocess": [preprocess_page]`. Otherwise it keeps today's dictionary.
- **`ingest_book_task`** (`books/tasks.py:57–58`) calls
  `chord(header)(after_preprocess.s(book_id, continue_ocr=not book.awaits_ocr_start))`. The value is fixed at ingest,
  so a click on «بدء المعالجة» that lands before the chord callback runs cannot enqueue the pages twice.
- **`after_preprocess(results, book_id, continue_ocr=None)`** (`books/tasks.py:62–83`).
  1. It proposes guides when there are none, for display only, as today.
  2. If `continue_ocr is None` (a chord queued before this change), it decides by the live flag.
  3. If `continue_ocr` is true, it calls `_enqueue_layout(book)`.
  4. Otherwise it re-reads the book. If the flag is already False (the owner started meanwhile), it returns without
     touching the status; else it calls `refresh_status()`, and the book becomes «تم التخطيط».
- **`processing.tasks.preprocess_page`** (`processing/tasks.py:40–71`). After a success or a recorded failure, if the
  book awaits the start and is not `uploaded`, it refreshes the book under the row lock of §3.2. Page re-runs in
  «التخطيط» have no later task to move the book on, and during ingest this gives the chip its live count.
- **The gate.** `processing.tasks.layout_page` (`:74–96`) and `ocr.tasks._run_stage` (`ocr/tasks.py:32–68`) return
  early, with a log line, when `page.book.awaits_ocr_start`. They already load the page with its book, fresh. This
  blocks nothing legitimate: books with OCR history all have the flag False, and new books reach OCR only through
  `start_ocr`.
- **Routing.** No routing change (`nassakh/settings.py:123–131`). The `layout` Celery queue is the book page's
  re-layout (D47) and has nothing to do with «التخطيط».

### 3.4 «استخراج الصفحات»: the upload and `start_processing`

- **`book_create`** (`books/views.py:27–46`) calls `start_processing` right after `create_book`, unless the book is in
  error.
  - The success message gives the exact range, from `kept_range` (below).
  - If queueing fails, the book stays `uploaded` with the error message, and its dashboard offers
    «استخراج الصفحات».
- **`start_processing`** (`books/services.py:448–469`) keeps its allowed states (`uploaded`, `error`).
  - The `needs_guides` refusal gets the text «اكتمل التخطيط؛ اضغط «بدء المعالجة»، أو أعد تجهيز الصفحات من القائمة «⋯».».
  - `ingest_book_task.delay` is wrapped. If the broker refuses, the status and the error message go back, and a
    ValueError says «تعذّر إرسال العمل إلى العامل الخلفي. تأكّد من تشغيل Redis والعامل ثم أعد المحاولة.». Today the
    book would stay `processing` forever.
  - Restarting from `error` a book whose «المعالجة» had started still goes through OCR, as today, because its flag is
    False.
- **`books.services.kept_range(book, source_pages=None) -> dict`** gives the PDF pages kept, 1-based and inclusive:
  `{source_pages, first, last, sheets, pages, text}`.
  - `sheets` = last − first + 1, and `pages` = sheets × `pages_per_sheet`. `text` is the panel's line (§3.14).
  - The count is `Book.source_page_count`, which `inspect_pdf` already stores, or the form's `pdf_page_count`
    (`books/forms.py:120–161`).
  - Counts in words go through `assembly.render.ar_count`.

### 3.5 «بدء المعالجة»: `books.services.start_ocr(book)`

```python
def start_ocr(book: Book) -> None:
    """«بدء المعالجة» (D64): send every prepared page into «المعالجة».

    Only from «تم التخطيط» (`needs_guides` with `awaits_ocr_start`). Refused with an Arabic ValueError when no
    page is ready. One conditional UPDATE claims the start (flag → False, status → `ocr`), so a double submit
    or a second tab starts nothing twice; `start_ocr_task` then runs `_enqueue_layout`. When the task cannot
    be queued the claim is undone.
    """
```

1. **Ready pages.** If no non-excluded page is `preprocessed`, it raises «لا صفحات جاهزة للمعالجة؛ أعد صفحةً إلى
   الكتاب أو أعد تجهيز الصفحات.».
2. **Claim.** `Book.objects.filter(pk=…, awaits_ocr_start=True, status=NEEDS_GUIDES).update(awaits_ocr_start=False,
   status=OCR, error_message="", updated_at=now)`. When 0 rows change, it re-reads the book:
   - the flag is already False: «بدأت المعالجة بالفعل.»;
   - the book is still `processing`: «لم يكتمل التخطيط بعد؛ انتظر حتى تُجهَّز كل الصفحات.»;
   - anything else: the ready-pages message.
3. **Task.** `books.tasks.start_ocr_task.delay(book.pk)` goes to the default queue. The task is `_enqueue_layout(book)`
   unchanged. It is a task because an 800-page book means 800 chains. If queueing raises, the claim is reverted
   (flag True, status `needs_guides`) and the broker message of §3.4 is raised.
4. **Only when prepared.** «بدء المعالجة» is offered only once every page is prepared. While the book is «قيد التخطيط»
   the button is shown disabled, with the title «يُتاح بعد اكتمال تجهيز الصفحات». This is decided without asking
   (§12): it keeps the start exact without locks.
5. **Routes.**
   - POST `/books/<id>/start-ocr/` (`books:start_ocr`, editor) redirects to the dashboard. A success adds «بدأت
     المعالجة. تُحدَّث هذه الصفحة تلقائيًا أثناء العمل.». A refusal shows its message as an error.
   - The redirect loads today's dashboard.

### 3.6 Re-runs, exclusions, deletion, the numbers pass

**Re-runs in «التخطيط».**
- `run_stage` (`books/services.py:536–560`) and `validate_rerun` (`:570–581`) allow only `preprocess`. They read the
  flag fresh (`Book.objects.filter(pk=…).values_list("awaits_ocr_start", flat=True)`) and refuse the other stages
  with the message of §3.3.
- `rerun_book(book, "preprocess")` is unchanged. It sets the status `processing`, puts the pages back to `uploaded`
  and enqueues preprocess-only chains.
- The book's «⋯» offers «إعادة تجهيز الصفحات», and a page offers «تجهيز الصفحة». `book_dashboard` and
  `page_detail_context` filter `rerun_stages` to `preprocess`.
- The page detail's preprocess panel is a synchronous re-run (`processing/services.py:217–246`) and works as today.
  With no regions yet, nothing is re-derived. When the book awaits the start, the API view refreshes the book status
  after the re-run.

**Re-runs in «المعالجة».** Today's five stages, for the book and for a page.

**Confirmation for book-wide re-runs** (a missing piece). Each «⋯» re-run item opens a dialog before posting (copy in
§3.14). The numbers come from `books.services.rerun_estimate(book, stage) -> {pages, kept, minutes}`:
- `pages`: the non-excluded, unapproved pages, which is what `rerun_book` would run.
- `kept`: `approved_page_count`.
- `minutes`: only in «المعالجة», and only for the stages whose chain ends with the models (`preprocess`, `layout`,
  `ocr`, `ocr_full`; not `ocr_fast`, which is Tesseract alone). It is ⌈pages × s ÷ 60⌉, where s is the median, over
  the book's pages, of the page's Qari time. A page's Qari time is the duration of the newest Qari v0.3 run plus the
  newest Qari v0.2 run of each of its regions. This takes one query over `OcrRun` (reduced in Python), and s falls
  back to 20 s when the book has no Qari runs.

`book_dashboard` sends the estimate of each offered stage in its config, so the dialog opens without a request.

**Exclusions.**
- `toggle_exclude` (`books/services.py:616–647`) keys its waiting branch (`:637–642`) on `book.awaits_ocr_start`
  instead of `status == NEEDS_GUIDES`:
  - a re-included page that is already prepared joins the waiting pages (the book is refreshed);
  - a page that is not prepared yet gets `preprocess_page.delay` only.
- In «المعالجة», today's behaviour applies.
- **Undo** (a minor finding). The view (`books/views.py:63–73`) adds the message with
  `extra_tags="undo:<the same toggle URL>"`. The shared toast in `templates/base.html` renders a small POST form
  «تراجع» when a message carries `undo:`. It reads the URL through a new template filter, `undo_action`, in
  `core/templatetags/nassakh.py`, and adds `next` so the user comes back to the same place. The form never submits by
  itself.

**Deletion («حذف الكتاب»).**
- `books.services.delete_book(book) -> str` deletes the book in one transaction. Every related table cascades
  (`Book` → pages, guides, runs, lines, revisions, manuscript, snapshots, stylesheet, renders, exports). After commit
  it removes `MEDIA_ROOT/books/<id>/` with `shutil.rmtree(ignore_errors=True)`: every file of a book lives under that
  folder (pages, previews, the live layout, exports). It returns the title.
- POST `/books/<id>/delete/` (`books:delete`, editor) redirects to the books list with «حُذف الكتاب «{title}».».
- Tasks still running for the book end quietly, since they load their page or book and return when it is gone. A task
  that was mid-write can leave a stray file; that is harmless.
- The action is offered in «⋯» for every book, and in the «التخطيط» side panel.

**The numbers pass.** Unchanged. Only `finalize_page` schedules it (`ocr/services.py:1374–1376`), and that runs only
in «المعالجة», so it needs no gate.

### 3.7 Books already past each stage; rollout and rollback

| Book | `awaits_ocr_start` | Dashboard | Behaviour |
|---|---|---|---|
| The 25 existing books (all `ready_for_review`, `reviewing` or `assembled`) | False | today's | today's; «⋯» «التخطيط» opens `?view=guides` |
| A book created by the upload form after 7a | True until «بدء المعالجة» | the «التخطيط» mode | the pause |
| Fixtures and scripts that call `Book.objects.create` | False | today's | today's |
| `manage.py smoke_pipeline` (uses `create_book`) | True | – | calls `start_ocr` after the pause; `--layout-only` stops at the pause and prints each page's bands and doubts |

- **Deploying.** Stop both workers, `git pull`, `make migrate`, `npm run build`, then start the workers and the web
  server. Messages already in Redis are safe:
  - queued chords call `after_preprocess(results, book_id)` and decide by the flag, which is False for every existing
    book;
  - queued OCR chains run for books with the flag False.
- **Rolling back the code** leaves an unused column. A book paused at `needs_guides` then behaves as the old code
  expects: its legacy `needs_guides` branch keeps the status, and the old «ضبط الأدلة» applies the guides and moves the
  book on. The old `apply_guides` enqueues OCR for every page whose regions changed, and a paused page has no regions
  yet, so every one of them changes and goes on to OCR.

### 3.8 The footnote rule (D68)

This is one change inside `detect_footnote_rule` (`processing/pipeline.py:340–378`). The components pass every test
of today (width ≥ 12 % of the text block, in the lower 60 % of the page, height ≤ max(12, 0.06 h), text lines above
and below, bottom above 0.97 h). They are then split by mean thickness `t = area / cw`:
- **strict**: `t ≤ max(3.0, 0.3 × med_h)`, as today (`:361`, `:369`);
- **thick**: `max(3.0, 0.3 × med_h) < t ≤ NEAR_RULE_THICKNESS × med_h`, with `NEAR_RULE_THICKNESS = 0.5`, and fill
  `area / (cw × ch) ≥ RULE_MIN_FILL`, with `RULE_MIN_FILL = 0.6`.

The widest strict component wins. Only when there is none does the widest thick one win. Nothing else changes: there
is no new field, and every existing rule, block and page-number test stays as it is.

- Existing books do not change until a page is prepared again. Book 25 improves when it is uploaded or prepared again.
- The page detail shows the rule as today.

### 3.9 Regions computed before they exist, and the pages worth a look

`processing/services.py` gains pure functions, and `page_layout` (`:557–604`) becomes a thin wrapper:

| Function | What it does |
|---|---|
| `resolve_layout(book_values, manual_book, override, pre) -> PageLayout` | extracted from `page_layout` without changing its results; `page_layout(page, pre)` loads the inputs (one query instead of two per page) and calls it |
| `page_bands(pre, book_values, manual_book, override) -> list[Spec]` | `guide_regions(resolve_layout(…))`, the same specs as `page_region_specs` |
| `layout_doubts(pre, layout, override, specs) -> list[str]` | the doubts below; labels in `LAYOUT_DOUBT_LABELS` |
| `page_guides_payload(page, pre, book_values, manual_book, rows=None) -> dict` | the `guides` block of a sheet item (§3.11): the bands are the `Region` rows once derived, else computed |
| `book_guides_state(book, first=None, last=None) -> dict` | the compact entry of every page, plus the book values, the stats and the counts; ≤ 4 queries whatever the page count |
| `guides_summary(book) -> dict` | the detection line; replaces the stats part of `guides_context` (`:789–820`), which goes with the screen |

In «التخطيط» no `Region` rows exist. The dashboard, the page detail (`page_regions`, `books/services.py:1072`) and the
API draw computed bands. `layout_page` later writes exactly those bands, because `_derive_regions` calls
`page_region_specs`, which calls `page_bands`.

**Pages worth a look.** These are live checks; nothing is stored. The chip «تستحق نظرة» keeps the pages with a doubt,
and pages in error.

| Code | Rule | Label | Goes away |
|---|---|---|---|
| `footnote_from_type` | the footnote start comes from the smaller-type block | «حاشية من حجم الخط» | any explicit footnote choice on the page (the override has `footnote_line`) |
| `line_cut` | the running-head cut or the footnote start lies inside a detected line box, 2 px in from its edges | «خط يقطع سطرًا» | move the line (snapping avoids this) |
| `text_hidden` | a line box at least 50 % of the text-block width lies at least 60 % (by height) inside a running-head or page-number band | «سطر من المتن خارج المتن» | move or remove the band |
| `no_lines` | `Preprocess.n_lines == 0` | «لا أسطر في الصفحة» | exclude the page, or leave it |

On the 217 included pages this gives 3 pages after D68 (§2.2): 23 of the 25 books show none, and no book shows more
than 2.

### 3.10 Guide edits: the services and the stage rule

| Service | While the book awaits the start | In «المعالجة» |
|---|---|---|
| `apply_book_guides(book, changes, reset_overrides=(), user=None) -> dict` (new; partial) | saves the book guides; nothing derived, nothing queued | re-derives every unapproved page without review work, re-OCRs those whose regions changed (today's `apply_guides` loop, `:713–726`) |
| `set_page_guides(page, set_, unset=(), reset=False) -> dict` (new; merge semantics, validated by `clean_guides(partial=True)`) | saves the override | refuses an approved page (today) and a page with review work (422); else re-derives and re-OCRs when changed |
| `set_page_guides_override` (replace, `:737–755`) | saves only | today, plus the review-work refusal |
| `preview_book_guides(book, changes, reset_overrides=()) -> dict` (new) | writes nothing | writes nothing; also counts the pages to re-read and the locked ones |
| `apply_guides` (full form, `:693–734`) | not called: the form that posted it goes with the screen | the loop that `apply_book_guides` runs; it now also skips pages with review work |
| `_derive_regions` (`:619–656`) | never called | **unchanged**: it also serves `rerun_preprocess` and layout re-runs, whose regions must follow the new image (§2.6) |

- **Review work** is `ocr.services._has_review_work` (`ocr/services.py:1390`): an approval stamp or a reviewed line.
  Such pages keep their regions when guides change, because `Line.region` is `SET_NULL` and their footnote lines would
  otherwise lose their region.
- **Inert start.** The first manual change to a book whose guides are `auto` starts from `{header_cut: None,
  footnote_line: None, page_number_zone: "none", page_number_height: 0.06}`, then applies only the changed keys. This
  fixes two traps of today's form, which posts all four keys:
  - `guidesEditor` defaults the zone to `bottom` (`static/src/js/processing.js:36`), so a running head alone switched
    on a bottom page-number zone on every page without a detected number;
  - `propose_guides` stores the median rule in `footnote_line` (`processing/services.py:369`), and that value would
    have become live.
- **Applying from a page.** A value applied from a page's chip also drops that key from that page's override, since
  it now equals the book value.
- **Overrides win.** A page's own override for the same key still wins. With «تطبيقه عليهما أيضًا» ticked, those keys
  are removed from those pages' overrides, and undo restores them.
- **Undo payloads.** `apply_book_guides` and `set_page_guides` return their inverse (`undo`): the previous book values
  and source, and the previous override of every page they changed. Posting it back restores the state exactly.
  - In «التخطيط» the client holds the payload and the server stays stateless, because nothing irreversible happens.
  - In «المعالجة» there is no undo: every change re-reads pages, and the explicit save is the safety.

### 3.11 The contract for the «التخطيط» mode (fixed before the UI agent starts)

Agent A (§9) writes these shapes as JSON fixtures in `books/fixtures/guides/` first. Agent B builds against them, and
the tests check the real payloads against the fixtures.

**Names.**

| Item | Values |
|---|---|
| Band kinds (full / compact) | `running_header` / `h`, `body` / `b`, `footnote` / `f`, `page_number` / `p` |
| Band `source` | body `auto`; running head `book` \| `override`; footnote `rule` \| `block` \| `book` \| `override`; page number `detected` \| `book` \| `override` |
| Doubt codes | `footnote_from_type`, `line_cut`, `text_hidden`, `no_lines` |
| `locked` | `""`, `"approved"`, `"review"` (only in «المعالجة») |
| `startAction` | `extract` (uploaded), `reextract` (error), `startOcr` (needs_guides), `startOcrDisabled` (processing), `back` (`?view=guides` on a started book) |
| Template filter | `undo_action` (a message's `undo:` URL) |

**Config additions in `book_dashboard(book, view=None)`.** `book_detail` passes `request.GET.get("view")`.

```json
{"guidesMode": true, "layoutStage": true, "startAction": "startOcr",
 "guides": {"source": "auto", "header_cut": null, "footnote_line": null, "page_number_zone": "none", "page_number_height": 0.06},
 "guidesStats": {"pages": 7, "rule": 6, "block": 0, "number": 7, "median_rule": 0.8012,
                 "text": "اكتُشف خط حاشية في 6 من 7 صفحات، ورقم صفحة في 7."},
 "keptRange": {"source_pages": 555, "first": 187, "last": 193, "sheets": 7, "pages": 7, "text": "الصفحات 187–193 من 555"},
 "textLayer": false,
 "rerun": {"preprocess": {"pages": 7, "kept": 0, "minutes": null}},
 "guidesUrls": {"state": "/api/books/25/guides/", "apply": "/api/books/25/guides/", "preview": "/api/books/25/guides/preview/",
                "page": "/api/pages/__id__/guides/", "startOcr": "/books/25/start-ocr/", "extract": "/books/25/start/",
                "delete": "/books/25/delete/", "back": "/books/25/"}}
```

`guidesMode` is `awaits_ocr_start or view == "guides"`. Outside the mode, the config and the markup are exactly
today's.

**The `guides` block of a sheet item** (`GET api:book_sheets?from&to&guides=1`). Every value is a ratio of the
prepared image, to 4 decimals. With `guides=1` the lines and fast runs are not loaded (≤ 5 queries).

```json
"guides": {
  "bands": [{"kind": "running_header", "bbox": [0, 0, 1, 0.062], "source": "book"},
            {"kind": "body", "bbox": [0, 0.062, 1, 0.781], "source": "auto"},
            {"kind": "footnote", "bbox": [0, 0.781, 1, 0.965], "source": "rule"},
            {"kind": "page_number", "bbox": [0.46, 0.972, 0.53, 0.99], "source": "detected"}],
  "lines": {"header": {"y": 0.062, "source": "book"}, "footnote": {"y": 0.781, "source": "rule"}},
  "rows": [[0.071, 0.083], [0.101, 0.113]],
  "override": {"footnote_line": 0.781},
  "doubts": [{"code": "line_cut", "label": "خط يقطع سطرًا"}],
  "image_url": "/media/books/25/pages/0001/display.webp?v=1727337600",
  "derived": false,
  "locked": ""
}
```

`rows` holds the detected line boxes' `[y0, y1]`, for snapping and for drawing a cut.

**`GET api:book_guides` (`/api/books/<id>/guides/[?from&to]`)** feeds the grid, the filmstrip and the chip counts.
Each page's entry is about 60 B:

```json
{"book": {"source": "auto", "header_cut": null, "footnote_line": null, "awaits_ocr_start": true},
 "stats": {"...": "as guidesStats"}, "counts": {"all": 7, "doubt": 1, "override": 0, "error": 0},
 "pages": [{"id": 812, "n": 1, "b": [["h", 0, 0.062], ["b", 0.062, 0.781], ["f", 0.781, 0.965], ["p", 0.972, 0.99, 0.46, 0.53]],
            "d": 1, "o": false, "l": "", "s": "preprocessed", "x": false}]}
```

In each page entry, `d` counts the doubts, `o` means the page has an override, `l` is `locked`, `s` the page status
and `x` whether it is excluded.

**Writes.**

| Route | Name | Who | Body → response |
|---|---|---|---|
| POST `/api/books/<id>/guides/` | `api:book_guides` | editor | `{set: {header_cut?, footnote_line?}, reset_overrides: [keys], stage: "layout" \| "ocr"}`, or `{undo: {...}}` → 200 `{changed: [page numbers], reocr: n, undo: {...} \| null, book: {...}, pages: [compact entries]}`. 409 `{detail: «بدأت المعالجة في نافذة أخرى؛ حدّث الصفحة.», started: true}` when the stage the client sent is no longer the book's |
| POST `/api/books/<id>/guides/preview/` | `api:book_guides_preview` | editor | the same body without `undo` → `{changed: n, pages: [n…], cut: [n…], kept_overrides: [n…], locked: [n…], reocr: n, minutes: n \| null}` |
| POST `/api/pages/<id>/guides/` | `api:page_guides_override` (existing) | editor | today's replace body; or `{merge: true, set: {...}, unset: [keys], reset: false, stage}`; or `{replace: {...} \| null, stage}` (undo) → today's response plus `guides` (the block above) and `undo`. The same 409 as above when `stage` is no longer the book's |
| POST `/books/<id>/start/` | `books:start` (existing) | editor | «استخراج الصفحات»; success message «بدأ استخراج الصفحات. تُحدَّث هذه الصفحة تلقائيًا.» when the book awaits the start, else today's |
| POST `/books/<id>/start-ocr/` | `books:start_ocr` | editor | «بدء المعالجة» (§3.5) |
| POST `/books/<id>/delete/` | `books:delete` | editor | «حذف الكتاب» (§3.6) |
| GET `/books/<id>/?view=guides` | `books:detail` | login | the mode on a started book |
| GET `/books/<id>/guides/` | `processing:guides` | editor | redirects to the dashboard, with `?view=guides` once «المعالجة» has started and `#sheet-N` for `?page=N`. The POST branch and `templates/processing/guides.html` go |

Errors are in Arabic:
- 400 `{errors: [...]}` with the `clean_guides` messages;
- 422 for a locked page: «الصفحة معتمدة؛ أعد فتحها من شاشة المراجعة أولًا.» / «في هذه الصفحة تصحيحات مراجعة، فلا تتغيّر مناطقها.»;
- 409 as above.

### 3.12 The «التخطيط» mode (UI, D67)

(Mockups are right to left: the right edge is the start side. The side panel sits on the end side, the left, as
D34 put it.)

**When it shows.** Always while the book awaits the start. On a started book, only through «⋯» «التخطيط»
(`?view=guides`). The server chooses the mode, so today's markup is rendered untouched when the mode is off.

**States.**

| Book | Chip | Bar | Primary | Banner | «⋯» |
|---|---|---|---|---|---|
| `uploaded` | «مرفوع» | hidden | «استخراج الصفحات» | – (empty state) | «حذف الكتاب…», «كل الكتب» |
| `processing` | «قيد التخطيط · 3 من 7 صفحة» (prepared) | the prepared share, accent | «بدء المعالجة» disabled, title «يُتاح بعد اكتمال تجهيز الصفحات» | – | «حذف الكتاب…», «كل الكتب» |
| `needs_guides` | «تم التخطيط» (warning dot: your turn) | hidden | «بدء المعالجة» | the look banner | «إعادة تجهيز الصفحات…», «حذف الكتاب…», «كل الكتب» |
| `error` | «خطأ» | hidden | «إعادة استخراج الصفحات» | today's error banner | «إعادة تجهيز الصفحات…» (only when pages exist), «حذف الكتاب…», «كل الكتب» |
| started, `?view=guides` | today's | today's | «العودة إلى الصفحات» | the re-read banner | today's, with «التخطيط» and «حذف الكتاب…» |

- **Payload flags.** `_progress_payload` (`books/services.py:711–724`) gains `layout_stage` and `waiting` (the book
  awaits the start and is `needs_guides`). In «التخطيط», `percent` is the prepared share.
- **Status colour.** `status_dot("needs_guides")` stays `dot-warning`: attention, the owner acts next.
- **Books list.** It shows «تم التخطيط» with that dot, and in the progress cell «بانتظار «بدء المعالجة»» instead of a
  bar. A green bar at 100 % would read as finished.
- **End of preparation.** The toast «اكتمل التخطيط · 7 صفحات» appears, with no action: the primary is enabled.
- **Started elsewhere.** If a guides request answers 409 `started`, a banner offers «بدأت المعالجة في نافذة أخرى ·
  تحديث الصفحة».

**The grid (the mode opens here).** It shows bands on every prepared thumbnail and an amber dot on the pages worth a
look.

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ ⋯   [ ▶ بدء المعالجة ]   ● تم التخطيط   ‹ كل الكتب                                     الحوليات الليبية │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ [إلى صفحة… G]                           (بضبط خاص 0) (تستحق نظرة 1) (الكل 7)          [صفحات | شبكة]   │
├────────────────────────────┬─────────────────────────────────────────────────────────────────────────────┤
│                  التخطيط   │ ⓘ جُهّزت الصفحات واكتُشفت مناطقها. ألقِ نظرة واضبط ما يلزم، ثم اضغط          │
│    جُهّزت 7 من 7 صفحات ●   │   «بدء المعالجة».                          صفحة واحدة تستحق نظرة · عرضها    │
│ صفحة واحدة تستحق نظرة ▲    │                                                                             │
│ الصفحات 187–193 من 555     │  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐              │
│              حذف الكتاب…   │  │▒▒▒▒▒▒│ │▒▒▒▒▒▒│ │▒▒▒▒▒▒│ │▒▒▒▒▒▒│ │▒▒▒▒▒▒│ │▒▒▒▒▒▒│ │▒▒متن▒│              │
│ ────────────────────────── │  │▒▒▒▒▒▒│ │▒▒▒▒▒▒│ │▒▒▒▒▒▒│ │▒▒▒▒▒▒│ │▒▒▒▒▒▒│ │▒▒▒▒▒▒│ │▒▒▒▒▒▒│              │
│               لكل الصفحات  │  │▓▓▓▓▓▓│ │▓▓▓▓▓▓│ │▓▓▓▓▓▓│ │▓▓▓▓▓▓│ │▓▓▓▓▓▓│ │▒▒▒▒▒▒│ │حاشية▓│              │
│ ترويسة أعلى كل الصفحات ☐   │  │●  ▪  │ │  ▪   │ │  ▪   │ │  ▪   │ │  ▪   │ │  ▪   │ │  ▪   │              │
│  حاشية حيث لم تُكتشف ☐     │  └──7───┘ └──6───┘ └──5───┘ └──4───┘ └──3───┘ └──2───┘ └──1───┘              │
│ اكتُشف خط حاشية في 6 من 7   │                                                                             │
│ صفحات، ورقم صفحة في 7.     │                                                                             │
│ ────────────────────────── │                                                                             │
│ ▭ متن ▭ حاشية ▭ ترويسة     │                                                                             │
│ ▭ رقم الصفحة ● تستحق نظرة   │                                                                             │
└────────────────────────────┴─────────────────────────────────────────────────────────────────────────────┘
```

- The doubt on page 7 is illustrative: after D68, book 25 itself shows none.
- A plain click on a tile opens the viewer on that page. The mode always opens on the grid (it is not remembered in
  `nassakh.bookView`), except that a `#sheet-N` address opens the viewer on page N.
- Excluded pages show today's excluded tile (grayscale, «مستثناة», «إعادة الصفحة إلى الكتاب»). They never count and
  never carry a doubt.

**The viewer: fixing one page.** The page is fitted to the stage height, as in review, with no text pane.

```
┌────────────────────────────┬─────────────────────────────────────────────────────────────────────────────┐
│ (side panel as above;      │ استثناء   التلقائي        [بضبط خاص]  [خط يقطع سطرًا]               صفحة 4  │
│  in the viewer it ends     │                 ┌─────────────────────────────────────────┐                  │
│  with the filmstrip)       │    ←            │ [+ ترويسة]                               │          →       │
│                            │  التالية        │ ┌──────────────────────────────── متن ▾┐ │       السابقة    │
│ ────────────────────────── │                 │ │ ──── ─────── ──── ──────── ───── ─── │ │                  │
│ الصفحات 7                  │                 │ │ ──── ─────── ──── ──────── ───── ─── │ │                  │
│ [7][6][5][4●][3][2][1]     │                 │ ├━━━━━━━━━━━━━━━━━━━━━━━━━ حاشية 80.2% ┤ │  ← a draggable   │
│                            │                 │ │ (1) ─── ──────── ───── ──── ───      │ │    line (slider) │
│                            │                 │ └───────────────────────────── حاشية ▾┘ │                  │
│                            │                 │              ┌رقم الصفحة ▾┐              │                  │
│                            │                 └─────────────────────────────────────────┘                  │
└────────────────────────────┴─────────────────────────────────────────────────────────────────────────────┘
```

- **Bands.** They use the page detail's vocabulary: `.region-box` + `.region-<kind>`. The hues are body 215, footnote
  140, running head 45 and page number 26, with a 1.5 px outline, an 8 % fill and a label chip at the top start
  corner. Image coordinates are physical: a `dir="ltr"` layer with `left` / `top` percentages, as in
  `pageDetail.boxStyle`.
- **Guide lines.** They are 2 px lines in their band's hue, each with a pill handle («ترويسة 6.2%», «حاشية 80.2%»,
  `.guide-handle`) and a hit area 20 px tall.
- **Move.** Dragging the running-head cut or the footnote start moves the two adjacent bands live.
  - It snaps to the middle of the gap between detected lines within max(6 screen px, 0.8 × the median line height);
    Alt / Option drags freely.
  - On release (in «التخطيط») the value is saved for this page (`set_page_guides`, merge), with the toast «حُفظ لهذه
    الصفحة · تراجع».
- **Add.** A page without a running head shows the grip «+ ترويسة» at its top edge; a page without footnotes shows
  «+ حاشية» at its bottom edge. A click or a drag adds the line:
  - the running-head line lands in the gap after the first detected line;
  - the footnote line lands in the gap nearest 80 % of the height.
- **Band menu.** Each band's chip opens a small menu:
  - «إزالة من هذه الصفحة» sets the key to null (no running head, or no footnotes, here);
  - «تطبيق على كل الصفحات…» loads this page's value into the side panel's draft (running head and footnote only);
  - the page-number chip offers «ليس رقم صفحة» (`page_number_zone: "none"` for this page).
- **Head actions.** «التلقائي» appears when the page has an override and clears it («عادت الصفحة إلى التلقائي ·
  تراجع»). «استثناء» or «إعادة إلى الكتاب» posts the toggle (§3.6). The page's status label shows only when the page is
  not prepared: «مرفوعة» with today's sweep, or «خطأ» with «تجهيز الصفحة».
- **Born-digital books.** The banner adds «للكتاب طبقة نصية يُؤخذ منها نصّه؛ المناطق هنا لا تغيّر النص.»
  (`ocr/services.py:807–809`).

**All pages (the side panel's «لكل الصفحات»).**
- **Controls.**
  - «ترويسة أعلى كل الصفحات»: a checkbox, «% من الأعلى» and «من هذه الصفحة», which takes the cut of the page on screen.
  - «حاشية حيث لم تُكتشف»: a checkbox, a percentage, and «الموضع الوسيط 80.1%» when `guides_stats` has a median rule.
  - The detection line, and «إزالة الضبط العام» (only when the book guides are manual), which returns the book to its
    automatic guides.
- **Draft.** Any change is a draft. Only the page on screen draws it, dashed; the grid does not (a cut). While a draft
  exists, editing on the page is paused. The preview box appears under the controls:

```
┌ معاينة ─────────────────────────────────────────────────────┐
│                                         يغيّر هذا 118 صفحة. │
│ ▲ في 3 صفحات يقطع خطٌّ سطرًا أو تغطّي منطقةٌ سطرًا من المتن:   │
│   12 · 45 · 77                     (links: the viewer turns) │
│ ☐ صفحتان بضبط خاص تبقيان كما هما · تطبيقه عليهما أيضًا          │
│ (in «المعالجة»: سيُعاد التعرّف على نص 118 صفحة؛ نحو 40 دقيقة.  │
│  4 صفحات معتمدة أو فيها تصحيحات مراجعة لا تتغيّر.)             │
│                     [ إلغاء ]   [ تطبيق على كل الصفحات ]     │
└─────────────────────────────────────────────────────────────┘
```

- **Apply.** «تطبيق على كل الصفحات» saves, with the toast «طُبّق على 118 صفحة · تراجع» in «التخطيط», or «طُبّق على 118
  صفحة؛ يُعاد التعرّف على نصّها.» in «المعالجة». «إلغاء» or Esc drops the draft.

**Undo.** Every change in «التخطيط» returns its inverse. The toast's «تراجع» posts it. The button is bound to a
boolean `hasAction`, following 2a0bca2, and never runs by itself. There is no ⌘Z: the dashboard's `keyAction` returns
null on ⌘ and Ctrl (`books.js:756`), and the toast is enough.

**Keys in the mode.**

| Key | Action |
|---|---|
| ← / PageDown, → / PageUp, Home / End, G | as today (turns follow RTL) |
| V | grid ⇄ viewer (D69) |
| ↑ / ↓ on a focused guide line | move it 0.2 % (with ⇧: to the previous / next gap between lines) |
| Enter on a focused guide line | save now (only in «المعالجة»; in «التخطيط» a release saves) |
| Delete on a focused guide line | remove it from this page |
| Esc | undo an unsaved move; else drop the draft; else clear the filter |

**Accessibility.**
- Lines are sliders (`role="slider"`, `aria-orientation="vertical"`, `aria-valuemin/max/now`), with
  `aria-valuetext` «حدّ الترويسة عند 6.2%» or «بداية الحاشية عند 80.2%».
- Grips, chips and menu items are labelled buttons («خيارات منطقة الحاشية»).
- The image has the alt text «الصفحة 12 بعد التجهيز».
- An `sr-only` list names the page's regions in reading order («مناطق الصفحة: ترويسة، متن، حاشية، رقم الصفحة»).
- Colour never carries meaning alone: every band has its label, and the legend names the colours.
- The live region announces saves, undo and the preview summary. Focus returns to the line after a save. Under
  `prefers-reduced-motion` the band transitions (150 ms) are off.

**Started books (`?view=guides`).**
- The banner reads «تغيير المناطق هنا يُعيد التعرّف على نص الصفحة. الصفحات المعتمدة وما فيه تصحيحات مراجعة لا تتغيّر.».
- A drag does not save. The line turns dashed, and a bar under the page offers «حفظ وإعادة التعرّف على الصفحة» ·
  «إلغاء».
- Locked pages show «معتمدة: لا تتغيّر» or «فيها تصحيحات مراجعة: لا تتغيّر», with no handles.

**JavaScript and CSS (agent B).**
- **`bookDashboard`** (`static/src/js/books.js`):
  - `guidesMode` and `layoutStage` from the config;
  - the guides filters (`nassakh.guidesFilter.<book>`), and `primary` returning the `startAction` states in the mode
    (today's order otherwise, `books.js:514–528`);
  - `mountSheet` clones `#sheet-guides` and creates no decode handle;
  - `patchGuidesBody` and `patchBands` for thumbs and tiles;
  - the guides state map: `api:book_guides` once on load, then only for the pages the poll reports as changed,
    batched like the sheets queue;
  - `nassakh:page-shown` dispatched on `swapTo`;
  - V through `NassakhKeys` (§3.15);
  - a small `arCount`, following `assembly.render.ar_count`, on `window.NassakhBooks`.
- **`bookGuides`** (`static/src/js/processing.js`) is the one new Alpine component. It replaces `guidesEditor`
  (`:30–90`), which goes with the screen.
  - It sits on `.bk-layout` in the mode and reads the dashboard through `Alpine.store('book').dash`.
  - State: `book`, `draft`, `preview`, `busy`, `error`, `last` (the undo payload), `drag`.
  - It owns the side panel's section, the preview box, the drag, key and chip handling (delegated listeners on the
    sheet stack, direct style writes while dragging), the API calls and the toasts.
  - Pure helpers for the Node tests live on `window.NassakhGuides`: `snapY(y, rows, tolerance)`,
    `adjacentBands(bands, guide, y)`, `mergeBody(set, unset)`, `inverse(change)`.
- **`preprocessPanel`'s notice** (`processing.js:168`) no longer mentions OCR in «التخطيط» (§3.14).
- **Templates.**
  - `templates/books/detail.html`: `{% if guides_mode %}` blocks for the top-bar primary and the «⋯» items, the
    toolbar chips, the banner and the side panel (`{% include "books/_guides_side.html" %}`); a
    `<template id="sheet-guides">{% include "books/_sheet_guides.html" %}</template>`; outside the mode, today's
    markup unchanged.
  - The «⋯» item «ضبط الأدلة» becomes «التخطيط» (a link to `?view=guides`). The `guides` primary and the
    `needs_guides` banner go (`detail.html:41–47, 117–123, 203–206`).
  - `_page_sheet.html` gains a hidden `.sheet-doubts` and `.sheet-guides-actions`. `_page_tile.html` and the thumb
    shell gain an empty `.gd-bands` overlay and an `.is-doubt` mark.
- **CSS.**
  - `processing.css` keeps `.guide-line` / `.guide-handle` and adds the `gd-` classes (page figure, band chip, grip,
    menu, preview box, legend, thumb and tile overlays).
  - `theatre.css`: `.bk-dashboard.is-guides .bk-viewer .gd-page { width: min(calc(100cqh * var(--ar-n, .7)), 100cqw) }`.

### 3.13 The upload form, the dialogs, the books list

**The form** (`templates/books/form.html`, `bookForm` in `books.js:10–22`).

```
┌ الملف ────────────────────────────────────────────── يُحفظ الملف الأصلي كما هو ولا يُعدَّل ┐
│                                      الحوليات الليبية.pdf   [ اختيار ملف PDF ]            │
└───────────────────────────────────────────────────────────────────────────────────────────┘
┌ خيارات الاستخراج ─────────────────────────────── يمكن استثناء صفحات بعينها لاحقًا ┐
│        تجاوز الصفحات الأخيرة [ 362 ]              تجاوز الصفحات الأولى [ 186 ]          │
│                              الملف 555 صفحة · تُستخرج الصفحات 187–193 (7 صفحات).       │
│  …                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────────┘
  تُستخرج الصفحات فور الرفع وتُكتشف مناطقها، ثم تنتظر «بدء المعالجة».   إلغاء   [ استخراج الصفحات ]
```

- **File picker.** The native «Choose File» is replaced by a label button «اختيار ملف PDF» with the file's name, or
  «لم يُختر ملف».
- **The range probe.** When a PDF is chosen, `bookForm` reads `file.slice` of the first and last 2 MB and decodes them
  as Latin-1. It finds every `<<…>>` dictionary that holds `/Type /Pages` (not `/Page`), reads its `/Count n`, and
  takes the largest n. The line under the skip fields updates as the values change (copy in §3.14). The server check
  stays authoritative (`books/forms.py:151–161`).
- **The button.** It reads «استخراج الصفحات» and keeps today's submitting state «يُرفع الملف…».

**Dialogs** use the `rv-modal` look of «نسخة محفوظة». Focus goes to the safe button, and Esc cancels.
- **Re-run the book from a stage** («⋯»): the numbers of `rerun_estimate`.
- **Delete the book:** it names what is lost.
- **Rebuild a chapter from review** (the book page, §3.16).

### 3.14 Labels and copy (every new or changed string of 7a)

**Stored choices (labels only; migrations in §7).**

| Where | Now | New |
|---|---|---|
| `Book.Status.PROCESSING` | «قيد المعالجة» | «قيد التخطيط» |
| `Book.Status.NEEDS_GUIDES` | «بانتظار ضبط الأدلة» | «تم التخطيط» |
| `Book.Status.OCR` | «قيد التعرّف على النص» | «قيد المعالجة» |
| `Page.Status.PREPROCESSED` | «مُعالَجة» | «مُجهَّزة» |
| `Page.Status.LAYOUT_DONE` | «تم التخطيط» | «بانتظار التعرّف» |
| `Page.guides_override` (verbose) | «أدلة خاصة بالصفحة» | «تخطيط خاص بالصفحة» |
| `LayoutGuides` (verbose) | «أدلة التخطيط» | «التخطيط العام» |
| `Region.Source.GUIDES` | «من الأدلة» | «من التخطيط» |
| `Book.awaits_ocr_start` (verbose) | – | «بانتظار «بدء المعالجة»» |
| `STAGE_LABELS["preprocess"]` | «المعالجة الأولية» | «تجهيز الصفحات» |
| `STAGE_LABELS["layout"]` | «التخطيط» | «تحديد المناطق» |

The dashboard's counters stop hard-coding «مُعالَجة» and «تم التخطيط» (`books/services.py:989–990`) and read the
`Page.Status` labels. `books.js:58` (`STATUS_LABELS`) follows.

**Messages.**

| Where | Arabic |
|---|---|
| `ALL_PAGES_FAILED_LAYOUT` | «تعذّر تجهيز كل صفحات الكتاب. افتح إحدى الصفحات لمعرفة السبب، أو أعد المحاولة من الزر أعلى الصفحة.» |
| `INGEST_ERROR` (`books/tasks.py:19–21`) | «تعذّر استخراج الصفحات من الملف. تأكد أن الملف PDF سليم وغير محمي، ثم أعد المحاولة من الزر أعلى الصفحة.» |
| `start_processing` on `needs_guides` | «اكتمل التخطيط؛ اضغط «بدء المعالجة»، أو أعد تجهيز الصفحات من القائمة «⋯».» |
| broker failure (extraction and start) | «تعذّر إرسال العمل إلى العامل الخلفي. تأكّد من تشغيل Redis والعامل ثم أعد المحاولة.» |
| gate (`run_stage`, `validate_rerun`) | «لم تبدأ المعالجة بعد؛ اضغط «بدء المعالجة» أولًا.» |
| `start_ocr` refusals | «بدأت المعالجة بالفعل.» · «لم يكتمل التخطيط بعد؛ انتظر حتى تُجهَّز كل الصفحات.» · «لا صفحات جاهزة للمعالجة؛ أعد صفحةً إلى الكتاب أو أعد تجهيز الصفحات.» |
| `start_ocr` success | «بدأت المعالجة. تُحدَّث هذه الصفحة تلقائيًا أثناء العمل.» |
| extraction started (the start view, a book in «التخطيط») | «بدأ استخراج الصفحات. تُحدَّث هذه الصفحة تلقائيًا.» |
| `book_create` success | one page per sheet: «أُنشئ الكتاب «{title}»، وتُستخرج الآن الصفحات 187–193 من 555 صفحة في الملف (7 صفحات).»; no skip: «أُنشئ الكتاب «{title}»، وتُستخرج الآن صفحات الملف كلها (8 صفحات).»; two per sheet: «أُنشئ الكتاب «{title}»، وتُستخرج الآن الصفحات 3–50 من 52 صفحة في الملف (96 صفحة في الكتاب).» |
| delete | «حُذف الكتاب «{title}».» |
| exclusion toasts | «استُثنيت الصفحة 12 من الكتاب. · تراجع» · «أُعيدت الصفحة 12 إلى الكتاب. · تراجع» |
| `_derive_regions` (`processing/services.py:628`) | «لم تُجهَّز الصفحة بعد؛ شغّل «تجهيز الصفحات» قبل تحديد مناطقها.» |
| `processing/tasks.py:69` | «فشل تجهيز الصفحة. افحص الصورة الأصلية ثم أعد تشغيل المرحلة من صفحة التفاصيل.» |
| `processing/tasks.py:88` | «تعذّر الوصول إلى قاعدة البيانات أو الملفات أثناء تحديد المناطق. أعد التشغيل.» |
| `processing/tasks.py:95` | «فشل تحديد مناطق الصفحة. راجع تخطيطها ثم أعد تشغيل المرحلة.» |
| `ocr/services.py:172` | «لم تُجهَّز هذه الصفحة بعد؛ شغّل «تجهيز الصفحات» أولًا.» |
| `ocr/services.py:178, 183` | «…؛ أعد تجهيز الصفحة.» (replacing «أعد تشغيل المعالجة الأولية») |
| `ocr/services.py:225` | «منطقة فارغة خارج حدود الصورة؛ راجع تخطيط هذه الصفحة.» |
| `processing.js:168` notice | in «التخطيط»: «جُهّزت الصفحة من جديد.»; in «المعالجة»: «جُهّزت الصفحة من جديد. أعد تشغيل التعرّف على النص من قائمة إعادة التشغيل إن أردت تحديث النص.» |
| `_preprocess_panel.html` | title «تجهيز الصفحة»; help «لم تُجهَّز هذه الصفحة بعد. شغّل التجهيز لاستقامة الصفحة وقصّها وتنظيفها.» |
| `page_detail.html:47–48, 71` | tab «المُجهَّزة» (was «المعالَجة»); title «تتوافر بعد تجهيز الصفحة»; «لا صورة لهذا العرض بعد؛ تُنشأ صور الصفحة بعد تجهيزها.» |
| `books/list.html:19` empty text | «ارفع ملف PDF لكتاب مصوّر أو رقمي، وحدّد الصفحات التي تُتجاوز وعدد الصفحات في كل ورقة. يستخرج نسّاخ الصفحات ويكتشف مناطقها، ثم تضغط «بدء المعالجة» ليتعرّف على نصّها.» |

**The dashboard in the mode.**

| Where | Arabic |
|---|---|
| Primary | «استخراج الصفحات» · «إعادة استخراج الصفحات» · «بدء المعالجة» (disabled title «يُتاح بعد اكتمال تجهيز الصفحات») · «العودة إلى الصفحات» |
| «⋯» | «إعادة تجهيز الصفحات…» · «حذف الكتاب…» · «كل الكتب»; on started books the item «التخطيط» |
| Chip | «قيد التخطيط · 40 من 120 صفحة» · «تم التخطيط» |
| Books list, progress cell | «بانتظار «بدء المعالجة»» |
| Empty states | `uploaded`: «لم تُستخرج الصفحات بعد» / «اضغط «استخراج الصفحات»؛ تُستخرج الصفحات 187–193 من الملف وتُجهَّز وتُرسم مناطقها.»; preparing with no page yet: «تُستخرج الصفحات الآن» / «تظهر الصفحات هنا واحدةً واحدة، ثم تُجهَّز وتُرسم مناطقها.» |
| Banner | «جُهّزت الصفحات واكتُشفت مناطقها. ألقِ نظرة واضبط ما يلزم، ثم اضغط «بدء المعالجة».» + «صفحة واحدة تستحق نظرة · عرضها» / «صفحتان تستحقان نظرة · عرضهما» / «3 صفحات تستحق نظرة · عرضها» (hidden at 0); born-digital: «للكتاب طبقة نصية يُؤخذ منها نصّه؛ المناطق هنا لا تغيّر النص.»; started books: «تغيير المناطق هنا يُعيد التعرّف على نص الصفحة. الصفحات المعتمدة وما فيه تصحيحات مراجعة لا تتغيّر.»; another tab: «بدأت المعالجة في نافذة أخرى · تحديث الصفحة» |
| Chips | «الكل» · «تستحق نظرة» · «بضبط خاص» |
| Side panel | «التخطيط» · «جُهّزت 7 من 7 صفحات» · «صفحة واحدة تستحق نظرة» · «الصفحات 187–193 من 555» (two per sheet: «الصفحات 3–50 من 52، وفي كل منها صفحتان») · «حذف الكتاب…» · «لكل الصفحات» · «ترويسة أعلى كل الصفحات» · «% من الأعلى» · «من هذه الصفحة» · «حاشية حيث لم تُكتشف» · «الموضع الوسيط 80.1%» · «اكتُشف خط حاشية في 44 من 81 صفحة، وحاشية بخط أصغر في 1، ورقم صفحة في 72.» (parts at 0 are left out) · «إزالة الضبط العام» · legend «متن · حاشية · ترويسة · رقم الصفحة · تستحق نظرة» · «الصفحات» |
| Page | bands «متن» «حاشية» «ترويسة» «رقم الصفحة»; handles «ترويسة 6.2%» «حاشية 80.2%»; grips «+ ترويسة» «+ حاشية»; band menu «إزالة من هذه الصفحة» «تطبيق على كل الصفحات…» «ليس رقم صفحة»; head «التلقائي» «استثناء» «إعادة إلى الكتاب» «بضبط خاص»; locks «معتمدة: لا تتغيّر» «فيها تصحيحات مراجعة: لا تتغيّر»; save bar «حفظ وإعادة التعرّف على الصفحة» «إلغاء» |
| Doubts | «حاشية من حجم الخط» · «خط يقطع سطرًا» · «سطر من المتن خارج المتن» · «لا أسطر في الصفحة» (+ «استثناء الصفحة») |
| Preview | «معاينة» · «يغيّر هذا 118 صفحة.» · «في 3 صفحات يقطع خطٌّ سطرًا أو تغطّي منطقةٌ سطرًا من المتن: 12 · 45 · 77» · «صفحتان بضبط خاص تبقيان كما هما» · «تطبيقه عليهما أيضًا» · «سيُعاد التعرّف على نص 118 صفحة؛ نحو 40 دقيقة.» · «4 صفحات معتمدة أو فيها تصحيحات مراجعة لا تتغيّر.» · «تطبيق على كل الصفحات» · «إلغاء» |
| Toasts | «حُفظ لهذه الصفحة · تراجع» · «عادت الصفحة إلى التلقائي · تراجع» · «طُبّق على 118 صفحة · تراجع» · «طُبّق على 118 صفحة؛ يُعاد التعرّف على نصّها.» · «يُعاد التعرّف على نص الصفحة 12.» · «اكتمل التخطيط · 7 صفحات» |
| Accessibility | «حدّ الترويسة عند 6.2%» · «بداية الحاشية عند 80.2%» · «خيارات منطقة الحاشية» · «الصفحة 12 بعد التجهيز» · «مناطق الصفحة: ترويسة، متن، حاشية، رقم الصفحة» |

**Form and dialogs.**

| Where | Arabic |
|---|---|
| Form button and help | «استخراج الصفحات»; under the buttons: «تُستخرج الصفحات فور الرفع وتُكتشف مناطقها، ثم تنتظر «بدء المعالجة».» |
| File field | «اختيار ملف PDF» · «لم يُختر ملف» |
| Range line | count known: «الملف 555 صفحة · تُستخرج الصفحات 187–193 (7 صفحات).»; no skip: «الملف 8 صفحات · تُستخرج كلها.»; two per sheet: «الملف 52 صفحة · تُستخرج الصفحات 3–50، وفي كل منها صفحتان (96 صفحة في الكتاب).»; count unknown: «يُعرف عدد صفحات الملف بعد رفعه؛ تُستخرج الصفحات بعد أول 186 وقبل آخر 362.» / «تُستخرج كل صفحات الملف.»; nothing left (field error): «لا تبقى صفحات: الملف 555 صفحة والتجاوز 186 + 369.» |
| Re-run dialog | title «إعادة تشغيل الكتاب من «{label}»؟»; «تُعاد 81 صفحة من هذه المرحلة، وتبقى 12 صفحة معتمدة كما هي.» (the second part only when kept > 0); for model stages «يُعاد التعرّف عليها بالنماذج: نحو 27 دقيقة على هذا الجهاز.»; for `preprocess` «يُحتفظ بما ضُبط يدويًا لكل صفحة من تدوير وقصّ.»; buttons «إلغاء» · «إعادة التشغيل» |
| Delete dialog | title «حذف الكتاب «{title}»؟»; «يُحذف الكتاب وملفه وكل صفحاته ونصوصها ومخطوطته وملفات إخراجه، ولا يمكن التراجع عن ذلك.»; when there is work: «فيه 12 صفحة مُراجَعة ومخطوطة محرَّرة.»; buttons «إلغاء» · «حذف الكتاب» (`btn-danger`) |

### 3.15 One keymap by physical key (D69)

**`static/src/js/keys.js`** (new) exposes `window.NassakhKeys`. It is loaded after `ui.js` with one line in
`templates/base.html`, and every screen's `keyAction` uses it:
- `letter(e)` gives `'a'…'z'` from `e.code` (`KeyA`…), else a Latin `e.key`. Letters never match with ⇧, ⌘, Ctrl or ⌥
  unless a map asks for them.
- `digit(e)` gives 0–9 from `Digit*` or `Numpad*` without ⇧, or from `e.key` in 0–9, ٠–٩ or ۰–۹.
- `is(e, '?')` matches `?`, `؟`, or ⇧ with `Slash`. `plus(e)` / `minus(e)` match `+ =` / `- _` and `Equal`, `Minus`,
  `NumpadAdd`, `NumpadSubtract`. Brackets are matched by code.
- `composing(e)` is `e.isComposing || e.keyCode === 229`. Every map returns null for it.

**Review's two modes, decided by the word menu (`pop.open`).**
- **Menu open (word mode).**
  - 1–9 in any script choose reading n; a digit beyond the readings types it.
  - Enter accepts; Tab and ⇧Tab move; ⌥← and ⌥→ merge; ⌫ deletes the word; Esc closes the menu, and focus stays on
    the word; ⌘↵ approves the page.
  - Any other printable character, Latin or Arabic, starts the correction with that character, including ؟ and −. A
    proofreader can start «شيء» with ش.
- **Menu closed (page mode).**
  - The page is driven by physical key: A E N ? + − 0, ← →, Home and End (new).
  - Space (new) opens the focused word's menu, and digits still choose a reading for a focused word.
  - Other letters do nothing, so there is no stray correction.
- Today `review.js:91–120` matches `ev.key`: with a word focused, any single character becomes a correction (`:118`),
  and on the Latin layout `a` approves even with a word open (`:116`).

**The table: one meaning per physical key** (cells marked 7c land with the navigation work, §5.8).

| Key | Meaning | Dashboard | «التخطيط» | Review | Manuscript | Book page |
|---|---|---|---|---|---|---|
| ← → PageDown PageUp | next / previous page (RTL: ← is next) | ✓ | ✓ | ✓ | – | ✓ |
| Home End | first / last page | ✓ | ✓ | new | – | ✓ |
| G | go to a page | ✓ | ✓ | 7c (the pager becomes a field) | ✓ | ✓ |
| N | the next page waiting for review | ✓ | – | ✓ page mode, by code | – | – |
| A | approve the page | – | – | ✓ page mode, by code | – | – |
| E | edit (review: the line; book page: the mode) | – | – | ✓ by code | – | ✓ |
| O | the original scan | – | – | 7c («المعالَجة / الأصل») | ✓ | ✓ |
| S | the scan page marks «فواصل الصفحات الأصلية» | – | – | – | ✓ (seams) | **moved here** (was the spread; `togglePageMarks`) |
| V | the view arrangement | **moved here** (was 1 / 2) | ✓ grid / viewer | 7c (swap the sides) | – | **moved here** (one page / spread) |
| C | copy the page's text | ✓ | – | 7c | – | – |
| J K | next / previous block | – | – | – | ✓ | – |
| ] [ | next / previous warning | – | – | – | ✓ by code | – |
| + − 0 | zoom in / out / fit | – | – | ✓ | – | **moved here** (was 1 / 2 / 3: 0 fits the height, + steps up to the width then 100 %, − steps back) |
| 1–9 | choose reading n | – (freed) | – | ✓ word mode, any script | – | – |
| Space | open the focused word's menu | – | – | new | – | – |
| Enter | accept | – | save a line (started books) | ✓ | – | ✓ word menu |
| ⌘↵ | approve the page, also from the word menu | – | – | new | – | – |
| ⌥F | «تصحيح في كل الكتاب» | – | – | 7c, word mode | – | – |
| ? | the shortcut sheet | 7c | 7c | ✓ | ✓ | ✓ |
| Esc | close the top layer | ✓ | ✓ | ✓ | ✓ | ✓ |

Beyond the table:
- ⌘Z / ⇧⌘Z (review, book page), the book page's ⌘ keys and review's Tab / ⌥← ⌥→ / ⌫ are unchanged, except for
  matching by code.
- The dashboard's `keyAction` (`books.js:755–776`) swaps its `k === '1'` / `'2'` for V.
- The manuscript's `?`, `[` and `]` (`manuscript.js:161–166`) match through `NassakhKeys`.
- The book page (`book/geometry.js:378–420`) already matches by code; S → `togglePageMarks`, V → spread, and + − 0 →
  the fits.

**Sheets and hints.**
- Review's sheet (`review.html:353–382`) is regrouped into «في قائمة الكلمة» and «في الصفحة».
- The legend (`review.html:300–303`) reads «Tab للانتقال بينها، 1–3 لاختيار قراءة، أو اكتب التصحيح مباشرة؛ ⌘↵ لاعتماد
  الصفحة.».
- The toast at `review.js:604` keeps «حُسمت كل الكلمات · اعتمد الصفحة بـ A».
- The approve button's title becomes «اعتماد الصفحة (A، أو ⌘↵ من قائمة الكلمة)».
- The book page's sheet (`templates/editor/_book_overlays.html:161–184`) and toolbar titles
  (`_book_toolbar.html:18–25`) read «صفحة واحدة أو صفحتان (V)», «ملء الارتفاع (0) · تكبير (+) · تصغير (−)» and
  «فواصل الصفحات الأصلية (S)».
- Every sheet shows the letter keys as Latin capitals, with the line «تعمل الاختصارات بلوحة المفاتيح العربية أيضًا:
  المفتاح نفسه في مكانه.».

### 3.16 Round-trip safety (D70) and «لا علامات»

1. **No silent chapter replacement.**
   - `editor.services.reassemble_chapter(book, chapter_id, user, replace_edited=False)` (`editor/services.py:600–648`)
     raises `EditorEdited` when the manuscript is edited and the flag is false. The API (`editor/api.py:108`) answers
     409 `{detail: «حُرِّر نص هذا الفصل في «الكتاب»؛ إعادة بنائه من المراجعة تستبدله كله. أكّد الاستبدال أولًا.»,
     edited: true}`.
   - An unedited manuscript needs no flag, because re-assembly loses nothing there.
   - The book page's «⋯» item (`templates/editor/layout.html:62–65`) and the drift banner's button (`:123–126`) are
     renamed «إعادة بناء الفصل من المراجعة…». They open this dialog:

   ```
   ┌──────────────────────────────────────────────────────────────┐
   │                    إعادة بناء الفصل «الفصل الثالث» من المراجعة؟ │
   │ يُستبدل نص الفصل كله بنص صفحاته في المراجعة، فتضيع تعديلاته في   │
   │ الكتاب: العناوين والحواشي والكلمات. تبقى منه نسخة في «نسخة محفوظة». │
   │                               [ استبدال الفصل ]   [ إلغاء ]     │
   └──────────────────────────────────────────────────────────────┘
   ```

   «استبدال الفصل» (`btn-danger`) posts `{replace_edited: true}`, and «إلغاء» holds the focus. The banner keeps
   «الاحتفاظ بالنص».
2. **Approval is not drift.**
   - `assembly.services.drift_pages(included, rows, options)` is `stale_pages` (`assembly/services.py:699–726`) minus
     the pages whose only change is the reviewed flag: eligible, present in `included`, same signature.
   - `editor.services.review_drift` (`:569–594`) and `editor_state.drift_pages` (`:1093–1106`) use it. The readiness
     row `review_drift` follows through `review_drift`.
   - `stale_pages` itself is unchanged for an unedited manuscript: its re-assembly is lossless and is what flips D36.
   - On the dev database, book 26's drift goes from pages 4–8 to none, and book 19 keeps its 70 pages.
3. **The manuscript's amber mark from live status.**
   - `assembly/render.py` `_block_html` (`:276–300`) decides `data-reviewed` from the live status of the block's
     source pages: a set of reviewed page numbers is carried in `_Ctx`, loaded in one query. It falls back to the
     block's attribute when the block has no source pages.
   - So a page approved after assembly loses its amber rule at once.
4. **Live drift on the book page.**
   - New `GET /api/books/<id>/drift/` (`api:review_drift`, login) answers `{edited, pages: [n…], chapters: [ids]}` in
     three queries.
   - The book page keeps `drift` as live state instead of `cfg.drift` (`panel.js:716–725`, `stage.js:480`). It
     refreshes it at most once every 2 s:
     - when the tab becomes visible (`stage.js:281`, now always, not only while a render is active);
     - on `pageshow` with `persisted`;
     - on window focus;
     - on a `BroadcastChannel('nassakh')` message `{type: "review", book, page}` for this book.
   - Review posts that message after every successful change: resolve, edit, insert, delete, merge, drop word, role,
     approve, reopen and undo.
   - The manuscript view listens to the same triggers and re-fetches `api:manuscript_state` once.
   - Where `BroadcastChannel` is missing, focus and visibility cover it.
5. **A merge keeps both source marks.** `mergeNodes` (`static/src/editor/convert.js:465–472`) unions `sourcePages`
   and `sourceLineIds` (sorted, unique), and sets `reviewed` to false when either block has it false.
6. **«لا علامات».**
   - When a page has no uncertain words, review's counter (`review.html:27–28`) reads «لا علامات» instead of
     «حُسمت 0 من 0 كلمة», with the title «لا كلمات غير مؤكَّدة في هذه الصفحة؛ اقرأ الأسطر مع الصورة ثم اعتمدها.».
   - The Tab toast (`review.js:362`) reads «لا علامات في هذه الصفحة؛ اقرأ الأسطر مع الصورة ثم اعتمدها.».

## 4. 7b: trust in the text, footnotes and verse as roles

### 4.1 Flag policy v2 (D71): `ocr/flags.py` (new, pure), used by `alignment.build_lines`

**Readings.** `second_readings(primary, secondary) -> list[str | None]` gives Qari v0.2's reading of each v0.3 token.
It replaces the whitespace alignment at `ocr/alignment.py:762–773`.
- Punctuation runs are split off both sides (`[^\wً-ٰٟـ]+`), words are compared leniently, and marks only against
  marks. The pieces are glued back in the primary token's shape, so «القرآن.» equals «القرآن .».
- `alt` stays "the second model's reading when it differs leniently", so the popover and `editor/uncertain.py` keep
  working.

**`classify(p, s, tess, tconf, boxed, two_readers) -> list[why]`**. An empty list means sure. The first match wins:

| Case | Result |
|---|---|
| a letter from Greek, Cyrillic, Hebrew, kana, CJK or Hangul (`[Ͱ-ϿЀ-ӿ֐-׿぀-ヿ一-鿿가-힯]`), one of `※ ★ ∩ ∧ ∨ ≡ → © ¢ € ¥ ^`, or Arabic and Latin letters mixed in one token | `script` |
| punctuation only | sure |
| a Western-digit number with v0.3 = v0.2 = Tesseract (Tesseract: its word when it differs, else the primary's own when boxed) | sure |
| any other number | `number` (Kraken's reading follows, D50–D51) |
| a lone letter that may be a digit (D51) | as today |
| one-reader region | `single` when Tesseract's word is Arabic, conf ≥ 85 and its dotless skeleton differs; else sure |
| v0.2 has no counterpart | sure when Tesseract read the same word (the token is boxed and `tess` is null); else `alone` |
| v0.3 ≠ v0.2 (lenient) | `disagree` |

- The dotless skeleton strips tashkeel and non-letters, then maps ب ت ث ن ي ى ئ → ٮ, ج خ → ح, ذ → د, ز → ر, ش → س, ض → ص,
  ظ → ط, غ → ع, ف ق → ڡ, ة → ه, ؤ → و, أ إ آ ٱ → ا, and drops ء and ـ.
- **The single-reader measurements.** The 65 % catch rate quoted in §2.4 comes from a looser variant: any Arabic
  Tesseract word at conf ≥ 70 that differs. The rule adopted above (conf ≥ 85 and a different dotless skeleton) is
  stricter; on the reviewed single-read tokens it gave 6 flags, 3 of them real. `rebuild_lines --report` prints v2
  without single-reader flags (the gate numbers), v2 with the adopted rule, and v2 with the looser variant, so the
  owner can see the difference on his books.
- `conf = "low"` when `why` is non-empty. The token stores `why` only then.
- **The vote** (the D26 hook, `ocr/chooser.py`; `NASSAKH["WORD_CHOOSER"]` defaults to `"vote"`).
  - When `lenient(tess) == lenient(alt) != lenient(t)`, it sets `t = alt`, `orig = old t`, `pick = "vote"`, and leaves
    `res` null: the word stays open. `apply_chooser` (`:44–70`) no longer sets `res` for the vote.
  - It never touches `digit` tokens, Kraken tokens (`src == "kraken"`) or D51's lone «ا/ه/ع» candidates.
- Tesseract's confidence is kept as `tc`, taken from the matched word.
- Token keys added (JSON; `normalize_token` keeps unknown keys, `review/services.py:94–102`): `why`, `pick`, `tc`,
  `ins` (§4.2) and `sug` (§4.8).
- **Popover** (`static/src/js/review.js`). The reading keys stay stable (1 = v0.3, 2 = v0.2, 3 = Tesseract). The
  reading now in the text is marked «● في النص», and Enter confirms it (`accept()`, `:563–573`, confirms the current
  option, not `primary`). A reason line sits under the title:

| `why` | Arabic |
|---|---|
| `disagree` | «النموذجان مختلفان.» · with the vote: «النموذجان مختلفان؛ Tesseract يوافق Qari v0.2، فقراءته في النص.» · with Tesseract on v0.3's side: «النموذجان مختلفان؛ Tesseract يوافق Qari v0.3.» |
| `alone` | «لم يقرأ Qari v0.2 هذه الكلمة، ولم يؤكّدها Tesseract.» |
| `number` | «رقم: قابِله بالصورة.» |
| `script` | «في الكلمة حروف ليست عربية («и»، «л»).» · «في الكلمة رمز غريب («※»).» |
| `single` | «قرأ هذه المنطقةَ نموذجٌ واحد، ويقرأ Tesseract هنا «يحيى».» |
| `missing` | «كلمات أضافتها القراءة الثانية: قرأها Qari v0.2 وTesseract ولم يقرأها Qari v0.3.» |

```
┌───────────────────────────────────────────────┐
│                          اختر القراءة الصحيحة  │
│  النموذجان مختلفان؛ Tesseract يوافق Qari v0.2،  │
│                            فقراءته في النص.    │
│  Qari v0.3            يجي   1                  │
│  Qari v0.2   ● في النص  يحيى   2               │
│  Tesseract           يحبى   3                  │
│ ─────────────────────────────────────────────  │
│  حفظ  [                            …تصحيح]     │
│  ›                              إجراءات أخرى   │
└───────────────────────────────────────────────┘
```

### 4.2 Words only the second model read (D72)

1. **Runs.** In `compose_page` (`ocr/services.py:1235`), before `build_lines`, `flags.secondary_only_runs` finds the
   runs of v0.2 pieces with no v0.3 counterpart (after the punctuation split). Each run is anchored after the primary
   token it follows.
2. **Filters.** A run is dropped when:
   - every word holds digits (the numbers pass handles it);
   - ≥ 60 % of its words appear within ±12 primary words (a duplicate);
   - it is one word that is a prefix or suffix of an adjacent primary token (a split word, «هير» from «هيرودوت»);
   - it sits at anchor 0 of a body region with ≤ 4 words (a running head).
3. **Support.** It is the share of the run's words that find a Tesseract word (fuzzy ratio ≥ 75, lenient) among the
   Tesseract words of the anchor's built line ±1, after removing the words the primary already accounts for.
   - **Support ≥ 0.7.** The run is merged into the region's text. `build_lines(..., inserted={index: group})` marks
     its tokens `why: ["missing"]`, `ins: <group>`, low; Tesseract's boxes place them. On 23/1 this rebuilds line 11
     as «تعالى بطرابلس … رحمه الله» and line 12 as «تعالى من كبار …».
   - **Otherwise** it becomes an `ocr.TextGap(kind="words")` after the token it follows.
4. **`ocr.TextGap`** (7b keeps only `kind="words"`; 7d adds `line` and `split`) has these fields:
   - `page` (FK, CASCADE, `related_name="gaps"`) and `line` (FK, SET_NULL);
   - `index` (−1 = before the first token) and `after_t` (the anchor's text, for re-anchoring);
   - `kind`, `text`, `source="secondary"`, `support` (float);
   - `status` ∈ {`open`, `inserted`, `dismissed`}, `decided_by`, `decided_at`, `created_at`.

   Gaps are replaced with the lines by `finalize_page` and kept as they are on a page with review work
   (`ocr/services.py:1340–1342`).
5. **Review services** (`review/services.py`). Each one locks the page, records its revisions, and is undone by
   `undo_last` through a shared `LineRevision.batch`:
   - `resolve_insertion(page, group, keep)`: keep sets `res = "secondary"` on the group's tokens (on any line); drop
     removes them.
   - `accept_gap(gap, text=None)` inserts typed tokens after `index`, re-anchored by `after_t`. The gap becomes
     `inserted`, the revision's `after` holds `{"gap": id}`, and undo reopens it.
   - `dismiss_gap(gap)`: new action `gap` «نص مقترح»; undo reopens it.
   - `edit_line`, `merge_tokens` and `delete_token` shift a line's open gaps with the same alignment as `retokenize`
     (`:570–587`).
6. **Counts.** `count_unresolved` (`:1195`) counts one item per insertion group. `Page.n_unresolved` is the open tokens
   plus the open gaps (a helper, `page_open_items`, used by `refresh_page_text`, `finalize_page` and `approve_page`).
   `Line.n_low` stays tokens only.
7. **Keys (word mode).**
   - Tab visits words, groups and gaps in reading order.
   - Enter keeps the focused group or inserts the focused gap. ⌫ dismisses a gap.
   - On a group, ⌫ deletes one word as usual; the group's popover carries «حذف الكلمات (12)» (critic 1.4).

```
┌────────────────────────────────────────────┐   ┌────────────────────────────────────────┐
│                 كلمات أضافتها القراءة الثانية │   │                   قد تكون هنا كلمات ناقصة │
│ قرأها Qari v0.2 وTesseract ولم يقرأها       │   │      يقرأ النموذج الثاني هنا: «الاجابة» │
│                               Qari v0.3.   │   │  إدراج  [                  الاجابة ]   │
│                   إبقاء الكلمات (12)  ↵     │   │                          تجاهل  ⌫      │
│                        حذف الكلمات (12)     │   └────────────────────────────────────────┘
└────────────────────────────────────────────┘
lines column: a dotted underline marks a group, a thin bar ▏ a possible gap
```

### 4.3 Honest page state (D73)

- **The looped prefix** (no model call). When the other model looped, `select_text` (`ocr/services.py:1029–1067`)
  returns a partial `alt_text`: its clean parsed text minus the last repeated unit, flagged `alt_partial`. Only the v0.3
  tokens up to the last aligned index count as two-reader; the rest are single-read. The re-read of the uncovered rest
  is a GPU call, so it is 7d.
- **Single-reader flags.** These follow §4.1's `single` rule.
- **`books.Page.reading`** (JSON, default `{}`) is written by `finalize_page`:
  `{"readers": "two" | "one" | "tesseract", "partial": false, "groups": 1, "gaps": 2}`.
  - `readers` is the weakest region's reading: `tesseract` when a region fell back to Tesseract's text (today's
    `ocr_fallback` flag), `one` when a region had one model reading beyond the looped prefix, else `two`.
- **Review header.**
  - A pill: «قراءة واحدة» (warning) or «نص Tesseract وحده» (danger). Nothing for two readers; the agreement pill is
    cut.
  - For `one`, a banner at the top of the lines column: «قرأ هذه الصفحةَ نموذجٌ واحد، فالعلامات فيها أقل من الحقيقة.
    قابِل كل سطر بالصورة.».
  - For `tesseract`: «تعذّرت قراءة هذه الصفحة بالنموذجين، ونصّها من Tesseract وحده. قابِل كل سطر بالصورة.».
- **Thumbnails** (the dashboard's tiles and filmstrip, review's filmstrip). A small half-disc marks one reader. The
  count shows the open items. The check shows only after approval (today).
- **Attention flags** (`core/templatetags/nassakh.py:34–42`): `single_reader` «قراءة واحدة», `missing_text` «نص قد يكون
  ناقصًا».
- **The attention list clears on approval.** `attention_pages` (`books/services.py:919`) leaves out approved pages
  unless they are in error or have a page-number sequence issue.
- **The approve dialog** counts groups and gaps: «بقيت 3 علامات: كلمتان غير محسومتين وكلمات مقترحة لم تُحسم. اعتماد
  الصفحة رغم ذلك؟», with the note «لا يدخل الكتابَ نصٌّ مقترح لم يُحسم.».

```
┌──────────────────────────────── النص ──────────────────────────────────────────────┐
│             [قراءة واحدة]   [3 علامات]   الرقم المطبوع: 67   نسخ                      │
│ ▲ قرأ هذه الصفحةَ نموذجٌ واحد، فالعلامات فيها أقل من الحقيقة. قابِل كل سطر بالصورة.     │
│ 9   قطب الاقطاب وكنز الطلاب الشيخ عبد الله الشعاب . ولد رحمه الله                      │
│ 10  ┈تعالى بطرابلس ونشأ بها واخذ عن جياعة من الفضاء وكان رحمه الله┈                    │
│ 15  معروف يقصد للزيارة والدعوات فه مشهورة ▏رحمه الله تعالى                              │
└────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.4 Footnote and verse roles; the continuation guard (D74)

- **Roles.** `ocr.Line.Role` gains `footnote` «حاشية», `verse` «شعر» and `main` «محتوى». `main` pulls a line that the
  layout put below the footnote line back into the body. `max_length=12` is enough, and there is no data migration.
- **`ocr.services.line_kind(role, region_kind)`**:
  - it returns `"footnote"` when the role is `footnote`, or when the role is `body` and the region is a footnote;
  - it returns `"body"` for every other role (`heading`, `subheading`, `verse`, `main`) and every other region.
  - It is used by the assembly loader (`assembly/services.py:221–230`), by `refresh_page_text`
    (`review/services.py:226–228`), by `line_item` (new field `kind`, read by `showGroup` and `is-footnote`,
    `review.js:798–809`), and by `api:book_sheets`.
- **`set_line_role`** (`review/services.py:770`) takes the effective choice. The refusal at `:786–787` goes.
  - On a footnote-region line, «حاشية» stores `body` and «محتوى» stores `main`; headings and verse are allowed.
  - On a body-region line, «حاشية» stores `footnote`.
- **`set_roles(page, line_ids, role)`** handles a ⇧-click range in review: one revision per line, sharing a batch id.
- **Review's line menu:** «نوع السطر»: «محتوى» · «عنوان رئيسي» · «عنوان فرعي» · «شعر» · «حاشية». With a range selected
  it reads «نوع الأسطر المحدَّدة (6)».
- **The manuscript's paragraph menu** (`static/src/js/manuscript.js:966–977`, `templates/assembly/manuscript.html:215–237`)
  gains «شعر» and «حاشية». When the block starts with a marker whose call exists on its page, the label reads
  «حاشية للعلامة (1)». The choice goes through `set_block_roles` as today; the manuscript's structure tools rest on an
  edited book (D49).
- **Verse in assembly (7b).** A `verse` line is never joined with another line. Each one becomes its own
  `paragraph style=verse`, so hemistichs no longer glue into the wrong paragraphs. Pairing them into bayts is 7d.
- **The continuation guard.** `page_notes` (`assembly/pipeline.py:1180–1218`): a note line without a marker at the top
  of a page's footnotes continues the previous page's note only if that note does not end with terminal punctuation
  and this page's body has no unmatched call. Otherwise it is a note of this page.
  - `link_footnotes` (`:1301`) gives the k-th marker-less note the k-th unmatched call on its page, with the warning
    `note_marker_missing` «حاشية بلا علامة رُبطت بالعلامة (1)؛ تحقّق منها.».
- **The manuscript's stray note.** A warning `stray_note` covers a body paragraph that starts with a marker, lies at
  the end of its page's text, and whose page has an unmatched call with that number. Text: «فقرة في الصفحة 3 تبدأ
  بعلامة حاشية «(1)» ولم تُربط.»; actions «جعلها حاشية» · «انتقال». The Phase 6 review's readiness row keeps its own
  measured rule. The unmatched-call condition only chooses the label «حاشية للعلامة (n)» (critic 1.6).

### 4.5 Readiness rows (D75, `publishing/readiness.py`, after the Phase 6 review's rows)

| Code | Rule | Level | Message | Action |
|---|---|---|---|---|
| `missing_text` | open `TextGap` rows on pages in the book | warn | «نص قد يكون ناقصًا لم يُحسم في 4 صفحات: 3، 7، 12، 30.» | «المراجعة» → `review:next` |
| `single_reader` | pages in the book with `reading.readers` ≠ `two` that are still `ocr_done` | info | «قُرئت 13 صفحة من الكتاب بنموذج واحد ولم تُراجَع بعد.» | «المراجعة» |

`clear` stays the Phase 6 review's rule: it is shown only without warn or info rows.

### 4.6 Existing books: `rebuild_lines --report` first

- **`manage.py rebuild_lines --report [book …]`** (`ocr/management/commands/rebuild_lines.py`, D39) writes nothing.
  - For every approved page, it rebuilds in memory the tokens the reviewer first saw, with the §2.4 method.
  - It computes today's flags and v2's from the stored runs, and prints per book and in total: the flags, the useful
    share, the catch rate, the suggestions (groups and gaps), and the pages with zero flags but errors.
  - How the tokens are matched, so the numbers can be reproduced:
    - a first-seen token is matched to the approved line by `difflib.SequenceMatcher(autojunk=False)` over the line's
      token texts; a `replace` or `delete` marks it changed;
    - its Qari v0.2 counterpart comes from aligning the region's primary and secondary texts with the punctuation split
      of §4.1 (`align_tokens` with the lenient key);
    - Tesseract's word and confidence come from the Tesseract word whose box overlaps the token's box most, with an
      IoU above 0.3;
    - a Kraken token (`src == "kraken"`) keeps Qari's reading in `qari.t` for the alignment.
  - It must reproduce §2.4's numbers (the 7b gate).
- **The write** recomputes reasons, votes, groups, gaps and the looped-prefix readings from stored runs, then
  reschedules the numbers pass, as D39 does.
  - It skips pages with review work.
  - It skips books with an edited manuscript unless `--include-edited` is given: all 9 manuscripts are edited, and
    none has a base until 7c (§2.6).

### 4.7 The PDF text layer (owner question 1)

- `publishing/pdf_text.py` (new) post-processes the print and screen PDFs, the same rule for both, before the check
  step of `PdfExporter`.
- In every font's `/ToUnicode` CMap (PyMuPDF: `xref_stream` / `update_stream`), it reverses the code points of each
  `bfchar` / `bfrange` entry that maps one glyph to two or more characters, **when every character is Arabic script**
  (U+0600–06FF, 0750–077F, 08A0–08FF, FB50–FDFF, FE70–FEFF, with their combining marks).
- Latin ligatures («fi», «fl») keep their order.
- Measured in the Phase 6 review on book 23: Chrome goes from 66 % to 99 % of words copied right, Preview and Safari
  from 99 % to 66 %.
- The test reads the screen PDF back with PyMuPDF, whose MuPDF engine reverses lam-alef as Chrome does. With the fix
  it must match the manuscript's words on ≥ 99 %.

### 4.8 Optional, last: years against the number in words

- `flags.number_words` parses Arabic cardinals with their old spellings (مايتين، ماية، ثلثماية، الف، الفين، عشرة …),
  joined by «و». It allows «م / هـ / ه» and brackets between the number and the words, and words that continue on the
  next line.
- `flags.year_check(line_tokens)` runs at finalize and again at the end of the numbers pass, after `ocr/numbers.py`
  saves a line (`:764` at 02f108a), because Kraken rewrites the digits.
  - Agreement sets `res = "words"`.
  - A mismatch sets `sug = {t: "٢٤٣", src: "words", label: "من الحروف"}` and adds `year` to `why`. The popover shows it
    as an extra reading «٢٤٣ · من الحروف», with the reason «السنة مكتوبة بعدها بالحروف: «ثلاث واربعين ومايتين» =
    ٢٤٣.», and `CHOICES` (`review/services.py:40`) gains `sug`.
- Built only if 7b's other parts are done and green.

## 5. 7c: navigation, the stage bar, the full merge, fix everywhere

### 5.1 The stage bar (D76)

**Where.**
- `templates/base.html:102–108` gains `{% block stagebar %}` between the `h1` and `.topbar-actions`.
- On book screens the `h1` holds only the title (`max-width: 26ch`, truncated), and it hides below 900 px.
- The partial is `templates/partials/_stage_bar.html`, driven by the small Alpine component `stageBar`
  (`static/src/js/stages.js`).
- A row of its own under the top bar is rejected: D34 pushed chrome out of the page's height.

**Steps.** `books.services.book_stages(book, current) -> list[dict]` is built from `book_progress`
(`books/services.py:672–708`) and the newest export of each format, in ≤ 8 queries. Each step is
`{key, label, url, state, detail, hint, current}`, and `state` ∈ `todo`, `active`, `done`, `stale`, `attention`,
`blocked`.

| Key · label | URL | done | active | stale | attention | blocked | detail |
|---|---|---|---|---|---|---|---|
| `pages` · التخطيط | the dashboard while the book awaits the start; `?view=guides` after | the start was clicked, or every page is prepared (at the pause) | extracting or preparing | – | pages in error before the start | – | «تم التخطيط · 7 صفحات» / «قيد التخطيط: 3 من 7» / «لم تُستخرج الصفحات بعد» |
| `ocr` · المعالجة | the dashboard | every page OCR'd or later | OCR running | – | pages in error | – | «عولجت 8 صفحات» / «قيد المعالجة: 3 من 8» / «بانتظار «بدء المعالجة»» (todo) |
| `review` · المراجعة | `review:next`, else page 1 | every page reviewed | some reviewed | – | – | no page OCR'd yet | «رُوجعت 6 من 8 صفحات» / «تبدأ المراجعة حين تنتهي معالجة أول صفحة» |
| `manuscript` · المخطوطة | the manuscript view | fresh, or edited («النص يُحرَّر الآن في «الكتاب»») | assembling | stale and not edited («تغيّر نص 4 صفحات بعد التجميع») | the run failed | – | «جُمعت: 38 فصلًا» / «لم تُجمَع المخطوطة بعد» |
| `book` · الكتاب | the book page | a layout exists | – | edited with content drift («تغيّر نص صفحتين في المراجعة بعد تحرير الكتاب») | – | no manuscript («يُفتح الكتاب بعد تجميع المخطوطة») | «17×24 سم · 84 صفحة» |
| `export` · الإخراج | the export page | a current file | an export running | the newest file is older than the text («تغيّر النص بعد آخر ملف») | the last export failed | no manuscript | «Word وPDF · قبل ساعتين» / «لم يُخرَج ملف بعد» |

- `current` is the screen shown; the dashboard passes `pages` in the «التخطيط» mode, else `ocr`. The mode's chip and
  today's status chip and bar fold into the current step.
- **Look** (DESIGN §2, §6).
  - Steps are 28 px high, radius 6, 12.5 px / 500 in `--text-2`. The current step has the `--active` fill, `--text`
    and weight 600.
  - Done is a 12 px ✓ in `--success`. Active is a static 7 px accent dot (chrome never loops, D24). Stale is a warning
    dot with the label in `--warning-text`. Attention is a danger dot, todo a hollow ring, and blocked `--text-3` with
    no link.
  - Separators are a 12 px `i-chevron-end` (`icon-mirror`).
- **Accessibility.** The bar is a `<nav aria-label="مراحل الكتاب"><ol>`. Each state is also written out, visually
  hidden: «(مكتملة)», «(جارية)», «(أقدم من النص)», «(تحتاج انتباهًا)», «(لم تبدأ)», «(غير متاحة بعد)». The current
  step has `aria-current="page"`, and the hint is its `title`.
- **Collapse by its own width** (`container-type: inline-size`):
  - ≥ 560 px: labels and details;
  - 360–560 px: labels, with a detail only on the current step;
  - < 360 px: one button, «المراجعة 6/8 ▾», opening a `menu-popover` of the six steps.
- **Live.** `GET api:book_stages` refreshes the bar:
  - on the window event `nassakh:stages`, which screens dispatch after an approval, a finished assembly, an apply and
    a finished export;
  - on the `nassakh` channel, on visibility and on `pageshow`.
  - There is no poll.

```
review at 1440 px (the rail on):
│ [اعتماد الصفحة] ? محفوظ ▬▬ حُسمت 5 من 9 ‹ صفحة 3 من 8 ›  الإخراج ‹ الكتاب ‹ المخطوطة ‹ [●المراجعة 6/8] ‹ ✓المعالجة ‹ ✓التخطيط   أيسر الشروح… │
the dashboard at 1180 px (compact):
│ ⋯ [الصفحة التالية للمراجعة]   الإخراج ‹ الكتاب ‹ المخطوطة ‹ المراجعة ‹ [●المعالجة 3/8] ‹ ✓التخطيط   المنهل العذب… │
```

### 5.2 The rail; the retired back links

- The book page's rail rules (`static/src/components/layout.css:11–36`, `.app-shell:has(.bp-screen)`) become
  `.app-shell:has([data-rail])`.
- `data-rail` goes on the roots of the dashboard, review, the manuscript view, the book page, the export page and the
  page detail. That gives back 176 px, the room review needs at 900–1180 px.
- **The bar retires:**
  - «لوحة الكتاب» in review (`review.html:15`), the manuscript (`manuscript.html:21`), the book page's «⋯»
    (`layout.html:85–88`) and the export page (`export.html:25–28, 45–48`);
  - the dashboard's «كل الكتب» ghost (`detail.html:14–17`); the rail's «الكتب» and «⋯» keep it.

### 5.3 Review's origin, the detour, the end of review, the N rule

- **Origin.**
  - Links from the book page carry `from=book&at=<its page>`: `reviewUrl(n)` (`panel.js:502`) and the changes tab.
  - The manuscript view's «فتح في المراجعة» carries `from=manuscript&block=<id>`; the export page carries
    `from=export`.
  - `review_page` (`review/views.py:16–27`) validates `from` against an enum, `at` as an int and `block` against
    `[hpn][0-9]+`.
  - `review_payload` (`review/services.py:373`) gains `nav.back = {label, url}`: «الكتاب» → `layout#page-<at>`,
    «المخطوطة» → `manuscript#block-<id>`, «الإخراج» → export.
  - The top bar's first link shows «‹ الكتاب». Page swaps inside review keep the parameters (`review.js:1165`).
- **Detour** (`from=book`). After «اعتماد الصفحة» the next page does not slide in (`celebrate`, `review.js:1071`). The
  lines pane shows «اعتُمدت الصفحة 12.», then «العودة إلى الكتاب» (primary) and «الصفحة التالية للمراجعة».
- **The end-of-review panel.**
  - It appears when approval finds no next page, or N finds none, replacing the toast «لا صفحات بانتظار المراجعة».
  - Focus moves to its primary, and the live region says «رُوجعت كل الصفحات».
  - The next step comes from `book_stages` as `review_payload.next_step`, and in `approve_page`'s answer when no page
    is next.

```
┌──────────────────────────────────────────────┐
│                          رُوجعت كل الصفحات ✓  │
│                   8 صفحات · حُسمت 41 كلمة     │
│               الخطوة التالية: تجميع المخطوطة  │
│   تُجمَع الصفحات في نص واحد متّصل: فصول وفقرات  │
│   وحواشٍ، تتحقّق من بنيته قبل الكتاب.          │
│      [ تجميع المخطوطة… ]    البقاء في المراجعة │
└──────────────────────────────────────────────┘
```

| State | Next step | Button | Link |
|---|---|---|---|
| no manuscript | «تجميع المخطوطة» | «تجميع المخطوطة…» | the manuscript view with `?convert=1` (opens the popover) |
| stale, not edited | «إعادة التجميع» | «إعادة التجميع» | the manuscript view |
| edited, with drift | «أخذ التغييرات إلى الكتاب» («غيّرت المراجعة نص صفحتين من الكتاب المحرَّر؛ تُؤخذ فقراتهما وحدها.») | «عرض التغييرات في الكتاب» | `layout?tab=changes` |
| fresh | «الكتاب» («المخطوطة محدَّثة؛ نسّق الكتاب وحرّره على صفحاته.») | «فتح الكتاب» | the book page |
| pages still processing | «لا صفحات بانتظار المراجعة الآن؛ ما زالت 3 صفحات قيد المعالجة.» | «العودة إلى المعالجة» | the dashboard |

- **The N rule.** A page brought in by approval's auto-advance is marked `arrived`. N on an `arrived` page that still
  needs review and has not been touched shows «هذه هي الصفحة التالية للمراجعة» and does not skip.

### 5.4 Tiles lead to review; addresses keep the view

- **Tiles and sheets.**
  - `page_tile` (`books/services.py:826`) gains `primary_url`: review when the page's text is final and the page is not
    excluded, else the page detail.
  - The tile link (`_page_tile.html:8`) and the sheet title (`_page_sheet.html:8`) point to it, so a new tab or a
    modifier-click reaches review. D33's plain click that opens the viewer stays (`books.js:1120–1131`).
  - A hover action «مراجعة» (always visible on touch) opens review. «تفاصيل المعالجة» (`i-sliders`, editors only) opens
    the page detail, and also sits in the sheet header and in review's «⋯». *Superseded by D84 (2026-09-29): the page
    screen is removed; `primary_url` falls back to the page's sheet at `/books/<id>/guides/#sheet-<n>`.*
- **Per-book memory.** The view and filter choice are remembered per book (`VIEW_KEY + bookId`, `books.js:28`).
- **Addresses.**
  - `addressQuery()` (`book/edit.js:151`) builds `?mode=edit&tab=<tab>`, and `setTab` rewrites the address the same way
    (`replaceState`). `?chapter=` is dropped once the page has landed.
  - `page_config` (`editor/services.py:1174`) accepts `block`; the stage lands on the block's page and lights it
    (`goToBlock`, `panel.js:214–223`).
  - `PANEL_TABS` (`:1153`) gains `changes`.
  - The readiness row `stray_notes` switches from `?chapter=` to `?block=`.
- **The book page's «تحويل إلى حاشية للعلامة (n)»** (D74 for edited books, where the manuscript's tools rest).
  - `editor.document.paragraph_to_footnote(nodes, block_id)` finds the call («(n)», «[n]», a superscript or a glued
    digit) in the preceding blocks of the same source page, then of the chapter. It replaces the call with a footnote
    node that holds the paragraph's text without its marker, and removes the paragraph; chapter undo applies.
  - With no call it answers «لم تُعثر على العلامة (1) في نص الصفحة 3؛ ضع المؤشّر موضعها ثم اختر «حاشية» (⌘⇧F).».
- **The dashboard** listens on the `nassakh` channel and on focus, and refreshes its progress once, so its «الكتاب»
  drift line stays live.

### 5.5 One term per concept (D77)

| Concept | Term | Its verbs and buttons | Never |
|---|---|---|---|
| Extracting and preparing pages, detecting regions | «التخطيط» | «استخراج الصفحات» | «ضبط الأدلة» |
| OCR | «المعالجة» | «بدء المعالجة» | – |
| Proofreading | «المراجعة» | «اعتماد الصفحة»، «الصفحة التالية للمراجعة» | – |
| The assembled text | «المخطوطة» | «تجميع المخطوطة»، «إعادة التجميع» | «تحويل إلى كتاب» |
| The typeset book page | «الكتاب» | «فتح الكتاب» | the project |
| WeasyPrint pagination | «ترتيب الصفحات» | «تُرتَّب الصفحات…» | «يُخرَج»، «إخراج» |
| Review changes entering an edited book | «تغييرات المراجعة» | «عرض التغييرات»، «أخذ التغييرات» | «إعادة تجميع الفصل» as the default |
| Files | «الإخراج» | «إخراج Word»… | pagination |
| The project | its title; the list is «الكتب» | – | «الكتاب»، «لوحة الكتاب» |

**Renames.**
- «تحويل إلى كتاب» → «تجميع المخطوطة» (`templates/books/detail.html:64, 112, 266`, `templates/assembly/manuscript.html:39,
  164`, `_convert_popover.html:9`).
- `editor/services.py:41` → «لم تُجمَع مخطوطة هذا الكتاب بعد؛ اجمعها أولًا.».
- `layout.html:101` and `export.html:59` → «اجمع مخطوطة الكتاب أولًا…».
- The «لوحة الكتاب» links (`review.html:15, 152`; `manuscript.html:21, 75, 167`; `layout.html:87`; `export.html:47`;
  `page_detail.html:26`; `books/form.html:21, 42, 93`) are removed where the stage bar replaces them, or become
  «المعالجة».
- `_book_side.html:5` `aria-label` → «أدوات الكتاب».
- `stage.js:464` → «تُرتَّب صفحات الكتاب…»; `layout.html:180` → «لم تُرتَّب الصفحات…»; `layout.html:70` → «بعد أول
  ترتيب»; `books.js:584` → «لم تُرتَّب صفحاته بعد»; `style.js:256` → «تُرتَّب الصفحات بخط …»; `stage.js:342` →
  «تعذّر طلب ترتيب الصفحات…».
- `PreviewRender.Status.RUNNING` (`publishing/models.py:40`) → «قيد الترتيب» (`Export` keeps «قيد الإخراج»).
- The done toast (`detail.html:334`) goes through `arCount`.

**The convert popover** (owner question 3, recommended answer). D35 stays: unreviewed pages are included by default
and marked amber. When the box is checked and pages are unreviewed, the button reads «تجميع مع 5 صفحات غير مُراجَعة».
Unchecked, the help reads «تُترك 5 صفحات لم تُراجَع بعد، وتُضاف حين تُراجَع.».

A template test asserts that the retired strings are absent from every template and every script under
`static/src/js`.

### 5.6 The page-by-page merge (D78)

**Model.**
- **`editor.Manuscript.base`** (JSON, null, «أصل التحرير») holds the assembled document the current text descends
  from.
  - One helper writes it, `_mark_edited(manuscript)`, and every editor write calls it first: `save_chapter`
    (`editor/services.py:243–298`, which writes inline at `:277–282`) and `_write` (`:301–306`: find & replace,
    digits, `editor/uncertain.py`). When `origin == assembly`, it copies the document into `base` before the change.
  - A whole-book assembly clears it (`assembly/services.py:_save`, origin back to `assembly`).
  - Each apply moves it page by page (`splice_base`).
  - **Restore** (`:372–410`) sets `base` to the snapshot's `base`, or to null for older snapshots. It never copies the
    current document (§2.6).
- **`editor.ManuscriptSnapshot.base`** (JSON, null). `_take` copies the manuscript's base.
- **Hot paths** (`manuscript_of(lock=True)` in autosave, and `manuscript_state`) use `defer("base")`.
- **`AssemblyRun.included[pk]` gains `"at"`**, the ISO time the page was read. Legacy entries fall back to the run's
  `finished_at`.
- **`editor.ChangesPlan`**: `book` (FK, CASCADE), `manuscript_version`, `status` (queued / running / done / error, the
  `AssemblyRun` labels), `pages` (JSON), `plan` (JSON), `error`, `task_id`, `created_by`, `created_at`, `finished_at`.
  - At most one plan per book is queued or running (a partial unique constraint, as `Export` has). The newest 5 per
    book are kept.
  - Plans never live on `AssemblyRun` (§2.6).

**Drift, classified.** `editor.services.review_drift` returns:

```json
{"edited": true, "pages": [12, 13, 91], "reasons": {"12": "review", "13": "processing", "91": "added"},
 "approvals": [4, 5, 6, 7, 8], "chapters": {"h812": [12, 13], "h950": [91]}}
```

- `review`: the page has a line-changing `LineRevision` created after its `at`, undone ones included (critic 1.5).
- `processing`: the signature changed with no such revision.
- `added` / `removed`: eligibility changed.
- `approvals` is never announced.
- `api:review_drift` returns the same object; `stale_pages` gains a sibling, `stale_reasons(included, rows, options)`.

**The merge** (`editor/merge.py`, pure, no ORM). The inputs are M (the manuscript), B (the stored base, or None), F
(`pipeline.assemble` on the current lines) and S (the pages to take).
1. **Select.** For each of M, B and F, take the top-level body blocks whose `sourcePages` meet S, or whose footnotes'
   `sourcePage` is in S. `lines_of(block)` is the block's `sourceLineIds` plus its notes' and its blockquote children's.
2. **Cluster.** Union-find over the selected blocks of the three documents joins blocks that share a source line. This
   joins the halves of a split paragraph to the one fresh paragraph. A block the owner typed (no source lines) is never
   clustered and stays where it is. There is no 80 % rescue (cut).
3. **Tokens.** Each block list becomes tokens, compared by key:
   - `("¶", type, level, style)` at each block start;
   - `("w", text, marks)` per run of non-space characters with one set of marks;
   - `(" ",)` for a collapsed space, `("fn", canonical note content)`, `("pb", page)` and `("br",)`.

   Volatile attributes never enter a key: `id`, `reviewed`, `suggestedRole`, `sourcePages`, `sourceLineIds`, a note's
   `number` / `marker` / `orphan` / `sourcePage`, and every null or empty attribute. Each token keeps a reference to its
   node, so a rebuild restores the real footnote node and marks.
4. **Decide** each cluster (T = the tokens of m, b, f):

   | Case | Kind | Default | Chip |
   |---|---|---|---|
   | T(m) = T(f) | none | – | – |
   | base known, T(m) = T(b) | `take` | theirs | «من المراجعة» |
   | base known, T(f) = T(b) | none (mine keeps its edits) | – | – |
   | base known, both changed, diff3 clean | `merged` | merged | «مع تعديلك» |
   | base known, both changed, diff3 conflicts | `conflict` | mine | «تعارض» |
   | no base, T(m) ≠ T(f) | `choose` | mine | «للمقارنة» |
   | m empty, f new: the base is known and holds no partner either, or the page is `added` | `insert` | theirs | «فقرة جديدة» |
   | m empty, f new, no base, the page is not `added` | `choose` | mine | «للمقارنة» (§2.6) |
   | m empty, b = f (the owner deleted it) | none | – | – |
   | m empty, b ≠ f | `conflict` | mine | «تعارض» |
   | f empty, T(m) = T(b) | `remove` | theirs | «تُحذف» |
   | f empty, T(m) ≠ T(b) | `conflict` | mine | «تعارض» |

   **diff3 over tokens.**
   - Anchors are the base tokens that `difflib` (`autojunk=False`) matches in both mine and theirs.
   - Between anchors it takes theirs where mine equals base, mine where theirs equals base, and either where both made
     the same change. Otherwise the chunk is a conflict.
   - A conflict whose chunks are two footnote tokens of the same note is merged recursively on the note's content.
   - `¶` tokens carry structure, so a split, a heading the owner made and a review role change all merge like words.
     Both sides changing the same `¶` is a conflict.
5. **Rebuild** only the items that change. An untouched block is never re-serialised, so chapter versions do not move
   without an edit.
   - A result block takes its attributes from the mine block that produced its `¶`, else from the fresh block.
   - A 1:1 `take` keeps mine's `id`, `style`, `breakBefore` and `keepWithNext` unless theirs changed the block type.
   - `sourceLineIds` and `sourcePages` are the union of the blocks the tokens came from, and `reviewed` comes from
     theirs.
6. **Apply** (`apply(document, plan, choices)`).
   - Each item's chosen nodes replace its first M block, and its other M blocks are dropped.
   - An `insert` goes after its anchor, the M block holding the lines of the nearest preceding clustered F block. It
     falls back to before the successor's block, then to the end of the chapter covering its page.
   - Approval-only pages flip `reviewed` on their blocks. That is not an item.
   - Then `doc.repair_ids` runs, and the chapters are formed again as `save_chapter` forms them.
7. **Base.** `splice_base(base or fresh, fresh, applied_pages)` replaces the base's blocks that touch the applied pages
   with the fresh ones. With no base, the new base is `fresh`.

**Services, task, API** (`editor/services.py`, typed, with docstrings).
- **`plan_review_changes(book, user, pages=None) -> ChangesPlan`**
  - It is refused with 400 before a manuscript exists, and with 409 `{reassemble: true}` when the text is not edited
    (a plain re-assembly loses nothing).
  - It is idempotent while a plan is queued or running.
  - It enqueues `editor.tasks.plan_review_changes` on the default queue.
- **The task.**
  - It runs `load_book` and `pipeline.assemble` in memory, picks the base (stored, else none) and runs `merge.plan`.
  - It stores `{manuscript_version, base: "stored" | "none", pages, items, counts}`, the planned pages' signatures
    (`sig`, `reviewed`, `at`), and the fresh warnings and seams for those pages.
  - Failures are recorded on the plan with an Arabic headline; the manuscript is untouched.
- **`apply_review_changes(book, plan_id, choices, pages, user) -> dict`** is synchronous and atomic, locking the book
  and the manuscript:
  1. Check that the plan is fresh; otherwise raise `PlanStale` (409).
  2. Take an `edit` snapshot «قبل أخذ تغييرات المراجعة · ص 12، 13», with its base.
  3. Run `merge.apply` and write the document (version + 1).
  4. Run `splice_base`.
  5. Write one `done` `AssemblyRun` with `settings.scope = "changes"`, as `_save_chapter_run` (`:759–849`) does:
     `included` is the manuscript run's entries with the applied and approval-only pages replaced by the planned
     signatures; warnings and seams are merged; `stats.applied = {at, taken, merged, kept, pages}`;
     `manuscript.run` points at it.
  6. Apply D36: reviewed pages still at the planned signature become `assembled`.
  7. `_schedule` the focus chapter's relayout (D47).
  8. Return `{version, snapshot, chapters, reload, relayout, applied}`.
- **«الاحتفاظ بنصّي في الكل»** is the apply with every choice `mine`. It moves the baseline and the base without
  writing the document, so there is no version bump and no 409 in an open editor.
- **A dev command**, `manage.py review_changes <book> [--pages 3,5] [--dry-run]`, prints the plan.

| Method | URL (name) | Who | Body → response |
|---|---|---|---|
| GET | `/api/books/<id>/drift/` (`api:review_drift`, from 7a) | login | `{edited, pages, reasons, approvals, chapters}` |
| GET | `/api/books/<id>/review-changes/` (`api:review_changes`) | login | `{drift, plan, stale}` of the newest plan |
| POST | `/api/books/<id>/review-changes/` | editor | `{pages?: [n…]}` → 202 `{plan_id, status}` |
| POST | `/api/books/<id>/review-changes/<plan_id>/apply/` (`api:review_changes_apply`) | editor | `{choices: {itemId: "theirs" \| "mine" \| "merged"}, pages: [n…]}` → 200; 409 `{detail, stale: true}` |

A plan item, as the UI reads it:

```json
{"id": "i7c2e91a04b3f", "kind": "merged", "default": "merged", "page": 12, "pages": [12, 13], "chapter": "h812",
 "chapter_title": "الفصل الثالث", "block": "p815",
 "diff": [["eq", "… وكان"], ["del", "فقيها"], ["ins", "فقيهاً"], ["eq", "، فاضلاً …"]], "conflicts": [], "base": "stored"}
```

The id is stable across plans (review 2026-09-27: a digest of the item's kind, source lines, book blocks and
words, `merge.item_id`), so a choice survives a 409 re-plan; an item is listed under every page it touches.
`diff` compares mine with the default result by word, with 5 words of context. A conflict carries `{mine, theirs}`
snippets. The result nodes stay server-side; the client never sends document JSON.

**UI (the book page).**
- **The banner** (`layout.html:113–129`) reads the live drift and is book-wide:
  - review: «تغيّر نص صفحتين في المراجعة بعد تحرير الكتاب: 12، 13.»;
  - processing: «أعادت المعالجة قراءة 70 صفحة بعد تحرير الكتاب: 4، 12، 13…» (at most 8 numbers);
  - added: «رُوجعت 3 صفحات لم تدخل الكتاب بعد: 91، 92، 93.»;
  - mixed: «تغيّر نص 5 صفحات بعد تحرير الكتاب: 3 في المراجعة و2 أعادت المعالجة قراءتها.».

  Its buttons are «عرض التغييرات» and «لاحقًا». «لاحقًا» hides the banner until the drift changes (`sessionStorage`, per
  book and per drift signature). For an unedited stale manuscript, the banner reads «تغيّر نص 3 صفحات في المراجعة بعد
  التجميع؛ لم يُحرَّر الكتاب بعد، فإعادة التجميع لا تُضيّع شيئًا.» with «إعادة التجميع».
- **The «⋯» item** reads «تغييرات المراجعة…» with a count.
- **The tab «تغييرات المراجعة»** (key `changes`) appears only with content drift or an open plan, with a badge.
  - Opening it first closes and saves the open paragraph (`panel.js:739–741`), then posts a plan and polls
    `api:review_changes` every 700 ms until it is ready.
  - Rows are per page, with a checkbox (whether the page is taken now), «ص N», the chapter title and a reason chip:
    «المراجعة», «إعادة المعالجة», «صفحة جديدة» or «أُخرجت من الكتاب».
  - Items have a status dot with its word, and a diff snippet: `<del>` struck through in `--color-danger-text` on
    `--color-danger-bg`, `<ins>` underlined on a soft green.
  - Conflicts and `choose` items get the segmented control «نصّي | المراجعة» (default «نصّي»). The `choose` help reads
    «لا يُعرف ما عدّلتَه هنا قبل هذا الإصدار من نسّاخ؛ قارن واختر.», and a deleted-by-owner conflict reads «حذفتَ هذه
    الفقرة من الكتاب، وغيّرتها المراجعة.».
  - Per page, the row's «⋯» offers «خذ ما جاء من المراجعة في هذه الصفحة» and «أبقِ نصّي في هذه الصفحة».
  - A click turns to the page where the item's block sits and lights it (`lightBlock`, `panel.js:224–229`).
  - Loading shows two skeleton rows and «يُقارَن نص الصفحات بنص الكتاب…». With no text difference it shows «لا فرق في
    النص؛ المراجعة والكتاب متّفقان في هذه الصفحات.» and «تم» (the baseline moves; the document is not written).
  - **Apply.** «أخذ التغييرات (3)» reads «تُؤخذ…» while it runs, then:
    - `afterServerEdit()` (`panel.js:656–666`) runs;
    - the changed blocks flash `--accent-soft` once (600 ms, none under reduced motion);
    - the toast reads «أُخذت تغييرات صفحتين من المراجعة · تراجع»; undo restores the snapshot, its base and its run;
    - keep-all reads «بقي نصّك كما هو؛ لن تعود هذه الصفحات إلى التغييرات · تراجع».
  - **409** gives «تغيّر النص منذ المقارنة؛ أُعيدت المقارنة.», a new plan, and the choices kept for the item ids that
    remain. **Failure** gives «تعذّرت المقارنة؛ بقي الكتاب كما هو.» with «إعادة المحاولة».
  - The rebuild of 7a stays as the tab's secondary «إعادة بناء الفصل من المراجعة…», with its dialog.

```
┌────────────────────────────────────┐
│ ٣              تغييرات المراجعة     │
│ تُؤخذ فقرات هذه الصفحات وحدها، ويبقى│
│ كل ما سواها كما حرّرته. تُحفظ نسخة   │
│ قبل الأخذ.                           │
├────────────────────────────────────┤
│ ☑   الفصل الثالث · ص 12 · المراجعة   │
│     ● من المراجعة                    │
│     «… وكان ~~فقيها~~ فقيهاً، فاضلاً …»│
│ ☑   ص 13 · إعادة المعالجة            │
│     ▲ تعارض                          │
│     عدّلتَ هذه الكلمات في الكتاب،    │
│     وغيّرتها المراجعة أيضًا.         │
│     نصّك: «… سنة 1966 …»            │
│     المراجعة: «… سنة 1965 …»        │
│     [ المراجعة | نصّي ]              │
├────────────────────────────────────┤
│        [ أخذ التغييرات (3) ]         │
│         الاحتفاظ بنصّي في الكل        │
│  إعادة بناء الفصل من المراجعة…        │
└────────────────────────────────────┘
```

**Other entry points.**
- The dashboard's «الكتاب» block drift line (`detail.html:273`) links «عرض التغييرات» → `layout?tab=changes`.
- The readiness row `review_drift` links «تغييرات المراجعة» → `?tab=changes`.
- The manuscript's edited banner adds «غيّرت المراجعة نص 3 صفحات بعد التحرير؛ تُؤخذ من «الكتاب».».
- The convert popover's edited warning adds «أو خذ تغييرات المراجعة وحدها من «الكتاب».».
- Review, on a page of an edited book, shows one quiet line: «يُحرَّر هذا الكتاب في «الكتاب»؛ تصله تغييرات هذه
  الصفحة من «تغييرات المراجعة».».

### 5.7 Fix everywhere (D79; no learning)

- **`review/corrections.py` (new).**
  - `find_occurrences(book, form, options)` scans the token JSON of non-excluded pages: a `tokens::text` prefilter,
    then Python. It matches whole tokens by their core (edge punctuation aside), with the book page's `FindOptions`
    (tashkeel, alef, whole word, `editor/services.py:412–417`).
  - It returns per page `{number, status, approved, lines: [{line_id, v, index, word, before, after, bbox, res, conf,
    head}]}`, plus `total` and `pages`. `head` marks a line that assembly drops as a running head.
- **`fix_everywhere(book, from_, to, picks, user)`** runs one transaction per page.
  - The token must still read `from`; otherwise it is skipped and reported.
  - It sets `t = to` (edge punctuation kept), keeps `orig`, sets `res = "typed"`, and writes one
    `LineRevision(action="fix", batch=uuid)` per line.
  - Approved pages stay approved; an `assembled` page goes back to `reviewed` (D36).
  - It returns `{batch, applied, skipped, pages, edited, find_url}`.
- **`undo_fix(book, batch)`** reverts each line of the batch whose newest revision is the batch's, and reports the
  others.
- **An edited book.**
  - The review toast adds «وفي نص الكتاب: استبدال «السعودي» بـ«المسعودي»…». Its link is `find_url` =
    `layout?tab=find&q=<from>&r=<to>&fix=<batch>`, and it opens the book page's find & replace pre-filled.
  - After the owner's «استبدال الكل» there, the book page posts a plan for the batch's pages. Pages whose plan has no
    items are settled at once (their baseline moves); the rest stay in «تغييرات المراجعة».
  - The baseline therefore moves only where the book text now agrees with review. Moving it on the fix alone would
    drop the fix for good if the owner skipped the book-side replace.
- **Entry points (review).**
  - The word menu's «إجراءات أخرى» offers «تصحيح في كل الكتاب…» (⌥F in word mode).
  - After a correction whose word has other occurrences, the response carries `elsewhere: {from, to, count, pages}`,
    shown as a chip under the undo toast: «159 موضعًا آخر بالشكل نفسه في الكتاب · تصحيحها…».
- **API** (`review/api.py`):
  - GET `books/<id>/occurrences/?q=&match_tashkeel=&fold_alef=&whole_word=`;
  - POST `books/<id>/fix-everywhere/ {from, to, picks: [{line_id, index, t}]}`;
  - POST `books/<id>/fix-everywhere/<batch>/undo/`.

```
┌──────────────────────────────────────────────────────────────┐
│ ✕                                        تصحيح في كل الكتاب  │
│      [ المسعودي              ]  ←  «السعودي»                  │
│ ☑ توحيد صور الألف   ☑ كلمة كاملة   ☐ مطابقة التشكيل            │
│ 160 موضعًا في 51 صفحة · المحدَّد 158                          │
│ ─────────────────────────────────────────────────────────── │
│ ص 8                                                           │
│ ☑ [مسح] …وقد ذكر «السعودي» في كتابه مروج الذهب…               │
│ ص 11 · مُعتمَدة                                               │
│ ☐ [مسح] …«السعودي»… · أكّدها المراجع كما هي                    │
│ ─────────────────────────────────────────────────────────── │
│ [ إلغاء ]                           [ تصحيح 158 موضعًا ]       │
└──────────────────────────────────────────────────────────────┘
toast: «صُحّح 158 موضعًا في 51 صفحة · تراجع» (+ «وتُرك موضعان تغيّرا منذ فتح القائمة»)
```

- **Sheet keys:** ↑ ↓ move, Space toggles, Enter applies.
- **Default ticks:** forms a reviewer confirmed as they are (`res` primary or typed) start unticked.

### 5.8 Keys that land in 7c

- **In review:**
  - G focuses the pager, which becomes a jump field;
  - O toggles «المعالَجة / الأصل» (the payload gains the original image's URL);
  - V swaps the sides;
  - C copies the page's text;
  - ⌥F opens «تصحيح في كل الكتاب» (word mode).
- **On the dashboard:** a «?» sheet, also reachable from «⋯» «اختصارات لوحة المفاتيح».
- The `core/test_keymap.py` table fills these cells.

## 6. 7d (briefly; designed in detail after 7c's hand test, decisions D81+; D80 is the cover)

- **The line audit** (`ocr/audit.py`), in the GPU task before a page is finalised.
  - At most 4 suspect bands per page are read alone by the primary model:
    - `uncovered`: a printed band at least 0.6 × the median band height tall and 25 % of the measure wide, outside
      the head and page-number regions, with no line box over its centre;
    - `underfull`: a line ≥ 75 % of the measure wide holding ≤ 0.5 × the region's word density, and at least 3 words
      fewer;
    - `unread`: a Tesseract line with ≥ 4 words of ≥ 2 letters at conf ≥ 60, of which < 35 % were matched.
  - The crop is the band's rows (`_crop_rows`, `ocr/services.py:536`), gray, 2× when the core band is under 12 px,
    capped at 80 tokens, and stored as an `OcrRun` with `params.scope = "line"`, so `rebuild_lines` can reuse it.
  - A reading is kept only when it did not loop, holds at least one word of ≥ 2 letters, and has at most twice the
    words the band's width holds.
  - Classification: ≥ 60 % of its words, in order, inside a neighbouring line (±2) gives `TextGap(kind="split")`;
    found elsewhere on the page gives nothing; otherwise `TextGap(kind="line")` with the band's box (an `underfull`
    suspect gives `kind="words"` inside that line).
  - These are suggestions, never text. A new review action `split` «فصل سطر» moves tokens `[index:]` to a new line just
    below; the word menu gains «بدء سطر جديد من هذه الكلمة».
  - About 0.4 extra short calls per page; measured, 23 likely dropped lines on 180 pages (including 19/52 and 26/2).
- **Re-reading a looped region's uncovered rest.** The crop runs from the top of the first Tesseract line past the
  covered part to the region's bottom, read by the model that looped (variant `gray_part`, `params.scope = "part"`),
  at most one call per region. The prefix and the part together become the second reading.
- **Verse.**
  - **Line classes** (ratios of the measure M): `R` right-aligned (R − x1 < 0.04 M, width ≤ 0.62 M); `L` left-aligned
    (x0 − L < 0.04 M, width ≤ 0.62 M); `C` centred and narrow; `S` wide with a central gap ≥ 0.04 M between boxed tokens,
    centred within 0.35–0.65 of M. Two lines on one row (y overlap ≥ 50 %) make one `S`, the right one being the صدر.
  - **Pairs:** `S` is one بيت; `R` then `L` is one بيت; `C C` is a pair in order; the `verse` role without geometry
    pairs in order; a leftover is a single hemistich.
  - **Accepted** with ≥ 2 bayts and ≥ ⅔ sharing the rhyme letter (the last Arabic letter of the عجز, with diacritics, a
    final alif or ى, calls and punctuation stripped, and ة/ه folded), or by the role. Qari v0.3's per-بيت markup breaks
    ties. Chips «شعر · 6 أبيات» and «ليس شعرًا» (a dismissal kept in `Book.assembly_settings`).
  - **Document:** one بيت is one `paragraph style=verse` whose `hardBreak` separates صدر and عجز; verse never joins
    across a page seam.
  - **HTML and PDF:** the runs between line breaks become `<span class="nk-hemi">`, with
    `.nk-verse{display:flex;justify-content:space-between;flex-wrap:wrap;text-indent:0;margin:0}`,
    `.nk-verse>br{display:none}`, `.nk-hemi{flex:0 0 46%;text-align:justify;text-align-last:justify}` and
    `.nk-hemi:only-child{flex-basis:100%;text-align:center;text-align-last:center}`. The WeasyPrint spike gave two line
    boxes on one row, صدر on the right. Word and EPUB keep today's centred verse; there are no Word tables.
  - **Book page tools:** «ضمّ الفقرة التالية شطرًا ثانيًا», «فصل الشطرين», and the hint «بيت بشطر واحد: ⇧Enter بين
    الشطرين».
- **Narrow screens and legibility.**
  - Review becomes a container and stacks below 880 px.
  - The book toolbar puts its extras into «⋯» below 640 px, and the side tabs are labelled and wrap to two rows.
  - Contrast: `--color-text-3` → #6b6b73 for hints and meta, a new `--color-provisional: #8a8a93` keeps D28 and D29,
    and `--color-text-disabled` covers disabled controls.
  - No text under 11.5 px except `kbd`, badges and the filmstrip numbers.
- **The owner is asked in 7d** whether verse and the audit are applied automatically (decided without asking, §12).

## 7. Data model and migrations (each file has one owner)

| Sub-phase | Migration | Owner | Contents |
|---|---|---|---|
| 7a | `books/migrations/0007_book_awaits_ocr_start.py` | A | AddField `Book.awaits_ocr_start` (default False); AlterField of `Book.status` and `Page.status` (labels); AlterField of `Page.guides_override` (verbose name). No RunPython and no cross-app dependency |
| 7a | `processing/migrations/0004_labels.py` | A | AlterField `Region.source` (label); AlterModelOptions `LayoutGuides` (verbose names) |
| 7b | `ocr/migrations/0004_roles_textgap.py` | D | AlterField `Line.role` (choices + `footnote`, `verse`, `main`); CreateModel `TextGap` |
| 7b | `review/migrations/0004_revision_batch_gap.py` | D | AddField `LineRevision.batch` (UUID, null, indexed); AlterField `action` (+ `gap` «نص مقترح») |
| 7b | `books/migrations/0008_page_reading.py` | D | AddField `Page.reading` (JSON, default `{}`) |
| 7c | `editor/migrations/0005_base_changes_plan.py` | F | AddField `Manuscript.base`, `ManuscriptSnapshot.base` (JSON, null); CreateModel `ChangesPlan` with its constraint |
| 7c | `review/migrations/0005_revision_fix.py` | F | AlterField `LineRevision.action` (+ `fix` «تصحيح في الكتاب») |
| 7c | `publishing/migrations/0004_preview_running_label.py` | F | AlterField `PreviewRender.status` (label «قيد الترتيب»); after the Phase 6 review has merged |
| 7d | as built | – | `TextGap.kind` + `line`, `split`; `LineRevision.action` + `split` |

JSON-only additions with no migration:
- token keys `why`, `pick`, `tc`, `ins`, `sug`;
- `AssemblyRun.included[pk].at`;
- `AssemblyRun.settings.scope = "changes"`.

## 8. Tests and gates

All tests use pytest with `nassakh.settings_test` (eager Celery, SQLite, a throw-away media folder), run as
`.venv/bin/python -m pytest <paths> -q`. The Node harness tests run as today.

### 8.1 The gates of every sub-phase

| Gate | Pass condition |
|---|---|
| Suite | the whole suite green (895, plus the Phase 6 review's, plus the new ones); `manage.py check`; `manage.py makemigrations --check` |
| Dev copy | `pg_dump` (postgresql@17, port 5433) of `nassakh` into `nassakh_p7check`; `migrate` it; then, as the owner's user with the test client and `DATABASE_URL` pointing at the copy, GET every screen of all 25 books: the dashboard, `?view=guides`, page 1's detail, review of page 1, the manuscript, the book page and the export page. Every one answers 200. Nobody writes to the dev database itself |
| Build | `npm run build` (the integrator); `static/dist/app.css` carries the new classes |

### 8.2 7a

- **Extra gates.**
  1. All 25 books stay on today's path. The flag is False for all of them, and each dashboard renders no mode markup
     (the rendered HTML equals the pre-7a render after normalising the renamed labels).
  2. `resolve_layout` ≡ `page_layout` on every stored page (230), as a refactor identity.
  3. **Detection replay.** The new `detect_footnote_rule`, run on every stored page's gray image and lines, changes the
     rule only on book 25's pages 1 and 3–7 (§2.2). The bands computed from the replay equal the stored regions on the
     same 220 pages where today's `page_region_specs` already equals them. The other 10 pages (the smoke-test books 1,
     3 and 4) were derived by Phase 2 code and differ before 7a; they are listed, not failed.
  4. The smoke command with and without `--layout-only` (a sample of `playground/poc/input/`).
  5. Node tests with Arabic-layout key events (`core/test_keymap.py`).
  6. Drift on the dev copy: book 26 → none (was 4–8), book 19 → the same 70 pages.
- **`books/tests.py`.**
  - `refresh_status` in «التخطيط»: uploaded pages give `processing`; all prepared give `needs_guides`; all failed give
    `error` with `ALL_PAGES_FAILED_LAYOUT`; excluded pages are ignored; the old texts still re-derive; with the flag
    False, today's assertions (`core/tests.py:326–370`) hold.
  - `create_book` sets the flag and `Book.objects.create` does not.
  - `ingest_book_task` passes `continue_ocr`. `after_preprocess` pauses (no `run_stage`, `needs_guides`, a proposal
    made), continues with True, decides by the flag without the kwarg, and leaves a book the owner already started
    alone.
  - `start_ocr` claims once. A second call, a `processing` book and zero ready pages are refused. It sets `ocr` and
    clears the flag, calls `start_ocr_task.delay` once, and reverts on a broker failure.
  - `start_ocr_task` enqueues layout chains for the prepared pages.
  - `run_stage`, `validate_rerun` and `rerun_book` in «التخطيط» give a preprocess-only chain and refuse the other
    stages; today's chain test runs with the flag False.
  - `toggle_exclude` in «التخطيط»: an unprepared page gets the preprocess task only, a prepared page joins, nothing is
    queued for OCR, and the message carries `undo:`.
  - `start_processing` works from `uploaded` and `error`, refuses `needs_guides` with the new message, and reverts on
    a broker failure.
  - `book_create` extracts at once and shows the range for one page per sheet, two per sheet and no skip; the book
    stays `uploaded` when queueing fails.
  - `kept_range`, `rerun_estimate` (the median and the fallback), and `delete_book` (rows cascade, the folder is
    removed after commit, editor role, redirect).
  - The dashboard: the mode's config and markup (the `startAction` candidates, chips, side panel, the `sheet-guides`
    template); a started book renders no mode markup; `?view=guides`; `rerun_stages` filtered.
  - Progress: the prepared-share percent and `waiting`. The compact poll payload is unchanged:
    `test_progress_compact_tiles_keys_size_and_book_error` passes with the same query count.
  - Sheets with `guides=1`: computed bands before derivation and rows after; ratios; doubts; `locked`; ≤ 5 queries.
    `api:book_guides` stays at ≤ 4 queries whatever the page count, and the payloads equal the §3.11 fixtures.
  - `smoke_pipeline`; the labels (Book and Page statuses, `STAGE_LABELS`, the dashboard counters).
- **`processing/tests.py`.**
  - A synthetic page with a 7 px bar at line height 20 and fill ≥ 0.6 is accepted.
  - A closed text row with fill < 0.6 is refused.
  - A page with a 3 px strict rule and a wider thick bar keeps the strict one (the book 16 shape).
  - Every existing rule, block and page-number test is unchanged.
  - `resolve_layout` ≡ `page_layout` and `page_bands` ≡ `page_region_specs` on the fixtures.
  - `layout_doubts`: one test per code, plus suppression by an override.
  - Guide services in «التخطيط» only save (pages stay `preprocessed`, no `run_stage`). The inert start: auto → manual
    with only `header_cut` gives zone `none` and no footnote line. The merge keeps the other keys. The preview reports
    changed, cut, kept and locked pages and the re-read count, with a query count independent of the page count. The
    undo payloads round-trip.
  - In «المعالجة», only changed pages that are unapproved and have no review work are re-derived and re-read.
    `set_page_guides` gives 422 for review work. `rerun_preprocess` still re-derives a page with review work (§2.6).
  - `preprocess_page` refreshes the book under the lock, and `layout_page` returns early.
  - The guides URL redirects. The APIs check the editor role and CSRF and answer in Arabic.
- **`ocr/tests.py`.** `ocr_page_fast` and `ocr_page_full` do nothing while the book awaits the start; the page and its
  runs are unchanged.
- **`core/tests.py`.** `status_dot("needs_guides") == "dot-warning"`; the new labels.
- **`core/test_theatre.py`** (B, Node).
  - `primary` per state in the mode (`uploaded`, `processing`, `needs_guides`, `error`, `?view=guides`). Today's cases
    are unchanged except `needs_guides`, which was `guides` (`:745–746`), and the key `'2'`, which becomes V (`:778`).
  - The guides filters, and counts from `api:book_guides` data.
  - `keyAction` V with `{key: 'ر', code: 'KeyV'}`.
  - `patchGuidesBody` geometry (bands, lines, physical left and top).
  - `NassakhGuides.snapY` (gap middles, Alt off, tolerance).
  - The draft → preview → apply / cancel flow, and the undo payloads.
  - A toast action never runs by itself.
  - The mode opens on the grid, and `#sheet-N` opens the viewer.
  - The form's range probe on byte fixtures: a found count, a missing one, and two `/Count` values.
- **`core/test_frontend.py`** (B). The button «استخراج الصفحات» and «اختيار ملف PDF»; the compiled CSS carries the
  `gd-` classes; `templates/processing/guides.html` is gone.
- **C's tests.**
  - **`core/test_keymap.py`** (new, Node) feeds every screen's `keyAction` Latin and Arabic-layout events
    (`{key: 'ش', code: 'KeyA'}`, `{key: '٢', code: 'Digit2'}`, `{key: '؟', code: 'Slash', shiftKey: true}`,
    `{key: 'ج', code: 'BracketLeft'}`); composing events return null.
    - It holds §3.15's table as data and fails when a bare letter or digit key maps to two different meanings across
      screens.
    - Review's modes: ش with the menu open gives `type`, closed gives `approve`; ⌘↵ in word mode gives `approve`; a
      digit beyond the readings types.
  - **Updated tests:** `core/test_review_ui.py:326–336` and `core/test_layout_ui.py:986, 2378` (S and 1–3 on the book
    page).
  - **`core/test_layout_ui.py`:** the rebuild dialog posts the flag; the live refresh on `pageshow`, visibility, focus
    and a channel message; `mergeNodes` unions the marks.
  - **`core/test_review_ui.py`:** «لا علامات»; a channel message after each change.
  - **`editor/tests.py`:** `reassemble_chapter` over an edited text without the flag gives 409, and with it 202; an
    unedited manuscript gives 202 without the flag; drift leaves out approval-only pages (the book 26 shape);
    `api:review_drift` in three queries.
  - **`assembly/tests.py`:** `drift_pages` against `stale_pages`; the D35 mark from live status.

### 8.3 7b

- **Extra gates.**
  1. `rebuild_lines --report` reproduces §2.4 before any write: books 23 + 25 give 144 flags at 62 % useful, 61 %
     caught; the 14 reviewed books give 518 at 54 %, 74 % caught.
  2. Pages with review work are byte-identical after `rebuild_lines` on the dev copy.
  3. `ocr/test_numbers.py` is unchanged and gives the same tokens with the vote on.
  4. `rebuild_lines` refuses edited books without `--include-edited`.
  5. The PDF text layer: PyMuPDF reads book 23's screen PDF with ≥ 99 % of words right (if Q1 is "reverse").
- **`ocr/test_flags.py`** (new, pure).
  - The punctuation split («القرآن.» against «القرآن .»: the «.» is not flagged); «،» against «.» is not flagged.
  - A Western number is sure only with three equal readings; an Arabic-Indic number stays low.
  - «مилادية», «وال德拉ية», «※» and «^» are flagged `script`; «°» and «%» are not.
  - The vote sets t = alt, keeps orig, leaves res null, sets `pick = "vote"` and keeps the key order; it never touches
    digit, Kraken or lone-letter tokens.
  - A word v0.2 lacks is sure with Tesseract and `alone` without it.
  - Single-reader flags only at conf ≥ 85 with a different skeleton; the dotless skeleton helper.
  - The secondary-only runs and each filter: the real «هير ودوت», «لها», a head at anchor 0, a duplicate.
  - The support threshold: the real 23/1 texts rebuild lines 11 and 12, or give a gap.
- **`ocr/tests.py`.**
  - `select_text`'s partial alternative from a looped run.
  - `compose_page` and `finalize_page` write `reading`, gaps and groups; the counts include groups and gaps.
  - `rebuild_lines` recomputes them without models (fake engines).
- **`review/tests.py`.**
  - `accept_gap` and `dismiss_gap`, with undo reopening; insertion keep and drop across two lines, with undo.
  - Roles: footnote, verse and main on both region kinds; the old refusal is gone; `set_roles` on a range.
  - `approve_page` counts open gaps.
- **`assembly/tests.py`.**
  - `line_kind` in the loader; footnote-role lines become notes; a verse-role line is never joined.
  - The continuation guard, and the positional link with `note_marker_missing`; the `stray_note` warning.
- **`books/tests.py`.** Approved pages leave the attention list; the tile's `readers`.
- **`publishing` tests.** The two readiness rows; `pdf_text`: an Arabic multi-character entry is reversed, a Latin
  «fi» is kept, and a single-character entry is untouched.
- **UI** (Node: `core/test_review_ui.py`, `core/test_manuscript_ui.py`, `core/test_theatre.py`).
  - The reason lines; Enter confirms the current reading; groups and gaps in the Tab order; ⌫ and Enter on them.
  - The pill and the banner; the tile's half-disc; the manuscript's «حاشية للعلامة (n)» label.

### 8.4 7c

- **Extra gates.**
  1. Plan-then-apply with every choice on «نصّي» leaves all 9 manuscripts byte-identical (on the dev copy).
  2. The incidents 23/8 and 26/3 each give exactly one item, a `take`. They are run as fixtures: the base, mine and
     fresh documents reduced to the page's blocks, built once from the dev database and committed as JSON. The
     fixture's base is made by reverting, in memory, the page's review revisions after the manuscript's run (edit,
     resolve, merge, drop_word and role restore `before`; insert removes the line; delete restores `before`), then
     assembling. This recipe is a fixture builder, not product code.
  3. The stage bar in ≤ 8 queries.
- **`editor/test_merge.py`** (new, pure).
  - The tokens (marks, notes, page marks, hard breaks, collapsed spaces, null attributes ignored).
  - diff3: clean merges, the same change on both sides, conflicts, recursion into a note.
  - Every row of the decision table.
  - Splits (two halves, one fresh paragraph), typed blocks kept, a heading made on the book page kept while a word
    elsewhere on the page is taken, a role change that makes a heading and splits a chapter, an owner-inserted footnote
    kept.
  - Stable ids for 1:1 items, and untouched blocks byte-identical.
  - Idempotence: after an apply, a new plan has no items.
  - `splice_base`.
- **`editor/tests.py`.**
  - The base is written once by every write path, never rewritten by a later edit, reset by a whole-book run, and on
    restore set from the snapshot (null for older ones).
  - The drift reasons, including undone revisions counted as `review`.
  - The plan task end to end (eager).
  - Apply: snapshot, version, one `done` run, drift cleared, D36, relayout.
  - Keep-all writes no document.
  - 409 for a stale version, and for a page reviewed again after the plan.
  - Plans are never an `AssemblyRun` (readiness shows no «يجري التجميع» during a plan).
  - A reader gets 403. Query counts do not grow with the page count.
- **`review/tests.py`.**
  - `nav.back` for each `from` (an unknown value is ignored); `next_step` in the payload and in approve's answer.
  - `find_occurrences` context and head marking.
  - `fix_everywhere`: a stale token is skipped, an approved page stays approved, assembled goes to reviewed; batch
    undo, including a partial one; the edited book's `find_url` and settling.
- **`books/tests.py`.** `book_stages` for each state of each step; `primary_url`.
- **UI** (Node).
  - `core/test_stage_bar.py` (new): six steps, `aria-current`, the hidden state words, a blocked step with no link, the
    collapse classes, the refresh on events.
  - `core/test_review_ui.py`: the back link, the detour, every variant of the end panel, the N rule, the fix sheet.
  - `core/test_layout_ui.py`: the banner's wording per reason; the tab (post, poll, render, choices, apply, toast,
    chapter reload); `?tab=` survives a landing and a tab switch; `?block=`; «تحويل إلى حاشية».
  - `core/test_theatre.py`: the tile's `href`, the «مراجعة» and «تفاصيل المعالجة» actions, the per-book view.
  - The template test for the retired strings.

### 8.5 7d

Designed after 7c's hand test. The audit is tested with fake engines, verse on the 23/5 shapes, and the WeasyPrint
export gives two line boxes on one row.

## 9. Build plan

- **How it runs.** The lead orchestrates each sub-phase as one Workflow. Agents are few and efficient: "few efficient
  can do the job … save my usage on Fable".
  - **Fable** builds only the critical UI piece, the «التخطيط» mode (7a B).
  - **Opus** builds everything else.
  - One Opus review runs after the owner's hand test of each sub-phase, not during the build.
- **What every agent receives.**
  - The spec sections it owns, the contract (§3.11 in 7a), its file list and this plan's rules.
  - The owner's style: function-based views, thin views, typed services with docstrings, small Alpine components,
    Arabic UI text, English code, settings from env.
  - The rules: `.venv/bin/python` only, never `uv run`; no writes to the dev database (checks run on the
    `nassakh_p7check` copy); no Chrome MCP.
- **What every agent returns:** its files, its test counts and its open issues.
- **Rules.**
  - No two agents edit the same file in the same sub-phase.
  - The integrator alone edits `static/dist/app.css` and `static/dist/editor.js` (`npm run build` once per sub-phase)
    and the docs.
  - An agent that needs a change in another agent's file reports it, and does not make it.

**7a**

| Stage | Agent | Model | Owns | Needs | Ends with |
|---|---|---|---|---|---|
| 0 | Lead | – | copies the Phase 7 evidence from the scratchpad into `playground/phase7/`: the three designs and the critic's report, the probes (`measure.py`, `features.py`, `evaluate.py`, `policy.py`, `s1_*.py`, `dropped.py`, `verse*.py`, `numwords.py`), `rule_check.py`, the merge prototypes (`merge_proto*.py`, `merge_lib.py`) and the incident data. The scratchpad does not survive a reboot. Records D64–D70 in `docs/DECISIONS.md` | "go" | the evidence in the repo |
| 1a | A "split" | Opus | `books/{models,services,tasks,views,urls,api}.py`, `books/migrations/0007`, `books/management/commands/smoke_pipeline.py`, `books/fixtures/guides/*.json`, `processing/{pipeline,services,tasks,api,views,urls,models}.py`, `processing/migrations/0004`, `ocr/tasks.py`, `ocr/services.py` (messages only), `core/templatetags/nassakh.py` (`undo_action`), `nassakh/urls.py`; `books/tests.py`, `processing/tests.py`, `core/tests.py`, the gate cases in `ocr/tests.py` | 0 | **first** the §3.11 fixtures (the contract), then the backend; its suites green; payloads equal to the fixtures |
| 1b | C "keys + safety" | Opus | `static/src/js/keys.js` (new), `templates/base.html` (the script line and the toast's undo form), `static/src/js/review.js`, `manuscript.js`, `book/*.js`, `static/src/editor/convert.js`, `templates/{review,editor,assembly}/*`, `editor/{services,api,urls}.py`, `assembly/services.py` (`drift_pages`), `assembly/render.py` (the D35 mark); `core/test_keymap.py` (new), `core/test_review_ui.py`, `core/test_layout_ui.py`, `core/test_manuscript_ui.py`, `editor/tests.py`, `assembly/tests.py` | 0 | **first** `keys.js` with its tests (B uses it for V); then D70 and «لا علامات» |
| 2 | B "the «التخطيط» mode" | **Fable** | `templates/books/*` (`detail`, `form`, `list`, `page_detail`, `_page_sheet`, `_page_tile`, new `_guides_side.html` and `_sheet_guides.html`), `templates/processing/_preprocess_panel.html` (wording); deletes `templates/processing/guides.html`; `static/src/js/books.js`, `static/src/js/processing.js`, `static/src/components/{theatre,processing,books}.css`; `core/test_theatre.py`, `core/test_frontend.py` | 1a's fixtures, 1b's `keys.js` | its suites green against the fixtures, then against A's real payloads |
| 3 | Lead (integrator) | – | the build, `docs/RUNBOOK.md` (the two stages, «بدء المعالجة», the new labels, deleting a book), `docs/baseline/PLAN.md` (a Phase 7 line) | 1a, 1b, 2 | §8.1 and §8.2's gates; the hand-off to the owner with run instructions (restart both workers) |
| 4 | Owner | – | §10, 7a | 3 | "OK", or findings |
| 5 | Opus "review" | Opus | none (it reports) | 4 | findings; one Opus fix-up agent if needed |

1a and 1b start together. B starts once A's fixtures and C's `keys.js` exist, which is early in their runs, and works
in parallel with the rest of them.

**7b**

| Stage | Agent | Model | Owns | Needs | Ends with |
|---|---|---|---|---|---|
| 1 | D "trust backend" | Opus | `ocr/*` (`flags.py` new, `alignment.py`, `chooser.py`, `services.py`, `numbers.py`, `models.py`, migration 0004, `management/commands/rebuild_lines.py`), `review/{services,api,urls,models}.py` + migration 0004, `assembly/{pipeline,services}.py`, `books/models.py` + migration 0008, `books/services.py` (`attention_pages` and the tile's `readers` only), `publishing/readiness.py`, `publishing/pdf_text.py` (new) + one call in `publishing/pdf_export.py`, `core/templatetags/nassakh.py` (flag labels), `nassakh/settings.py` (the `WORD_CHOOSER` default); `ocr/test_flags.py` (new), `ocr/tests.py`, `review/tests.py`, `assembly/tests.py`, `books/tests.py`, `publishing/test_exports.py`, `publishing/test_pdf_export.py` | "go" for 7b | **first** `rebuild_lines --report` reproducing §2.4 (gate 1), then the write paths; the payload fixtures for E |
| 2 | E "trust UI" | Opus | `static/src/js/review.js`, `templates/review/*`, `static/src/components/review.css`, `static/src/js/manuscript.js`, `templates/assembly/*`, `static/src/components/manuscript.css`, `static/src/js/books.js` (the tile and thumb mark only), `templates/books/_page_tile.html` and the thumb shell in `detail.html`; `core/test_review_ui.py`, `core/test_manuscript_ui.py`, `core/test_theatre.py` | D's fixtures | its suites green |
| 3 | Lead | – | the build, docs (D71–D75), the gates; `rebuild_lines` on the dev copy, then on the dev database for books without an edited manuscript once the owner agrees | 1, 2 | the hand-off |
| 4–5 | Owner, then an Opus review | | | | |

**7c**

| Stage | Agent | Model | Owns | Needs | Ends with |
|---|---|---|---|---|---|
| 1 | F "round-trip backend" | Opus | `editor/*` (`merge.py` new, models + migration 0005, `services.py`, `api.py`, `urls.py`, `tasks.py`, `document.py` `paragraph_to_footnote`, the `review_changes` command), `assembly/services.py` (`included.at`, `stale_reasons`), `review/{services,views,api,urls,models}.py`, `review/corrections.py` (new), review migration 0005, `books/services.py` (`book_stages`, `primary_url`), `books/api.py` + `books/urls.py` (`api:book_stages`), `publishing/models.py` + migration 0004, `publishing/readiness.py` (the `?block=` and «تغييرات المراجعة» links); `editor/test_merge.py` (new), `editor/tests.py`, `review/tests.py`, `books/tests.py`, the incident fixtures | "go" for 7c | **first** the merge's pure core, its tests and the byte-identity check on the dev copy (gate 1); then the services and the API |
| 2 | G "navigation UI" | Opus | `templates/base.html`, `templates/partials/_stage_bar.html` (new), `static/src/js/stages.js` (new), `static/src/components/layout.css` (+ the stage bar's rules), `static/src/js/review.js`, `templates/review/*`, `static/src/js/book/*.js`, `templates/editor/*` (`_book_side.html`, `layout.html`), `static/src/js/books.js`, `templates/books/*`, `static/src/js/manuscript.js`, `templates/assembly/*`, `templates/publishing/export.html`, `static/src/js/export.js`; `core/test_stage_bar.py` (new), `core/test_review_ui.py`, `core/test_layout_ui.py`, `core/test_theatre.py`, `core/test_manuscript_ui.py`, `core/test_export_ui.py` | F's payload fixtures | two passes by the same agent: first navigation (stage bar, rail, review's origin and end panel, the N rule, tiles, the renames, the keys of §5.8); then the book page's changes tab, `?tab=` / `?block=`, «تحويل إلى حاشية», and review's fix-everywhere sheet |
| 3 | Lead | – | the build, docs (D76–D79), the gates | 1, 2 | the hand-off |
| 4–5 | Owner, then an Opus review | | | | |

**7d.** One or two Opus agents, backend (`ocr/audit.py`, assembly verse) and UI (verse tools, narrow screens,
contrast), designed after 7c's test. Decisions D81+ (D80 is the cover, docs/baseline/COVER_SPEC.md).

## 10. Acceptance: what the owner tests at the end of each sub-phase

**7a**
- [ ] **Upload.** Upload «الحوليات الليبية» (the 555-page PDF) with skip 186 / 362. Before submitting, the form reads
      «الملف 555 صفحة · تُستخرج الصفحات 187–193 (7 صفحات).», or states the rule in words if the file's count cannot be
      read. «استخراج الصفحات» creates the book, and the message repeats the exact range.
- [ ] **Preparation.** The dashboard opens in «التخطيط» on the grid. Pages appear one by one («قيد التخطيط · 3 من 7
      صفحة»). «بدء المعالجة» is disabled until the last page is prepared, then «اكتمل التخطيط · 7 صفحات» appears.
- [ ] **One glance.** Every thumbnail shows its bands. Pages 1 and 3–7 show a footnote band, because the thick rules
      are now accepted; page 2 shows none.
- [ ] **Fix a page.** A click on a tile opens the viewer on it. Dragging the footnote line snaps it to a gap and shows
      «حُفظ لهذه الصفحة · تراجع»; «تراجع» restores it; «التلقائي» resets the page; «+ ترويسة» adds a running head.
- [ ] **All pages.** Tick «ترويسة أعلى كل الصفحات» and set the percentage. The preview says how many pages change and
      which lines would be cut. «تطبيق على كل الصفحات» applies it, and undo works.
- [ ] **Start.** «بدء المعالجة» leads to today's dashboard. A book that nobody touched in «التخطيط» gets the same text
      as the old flow, for one click more.
- [ ] **Exclusion.** Excluding a page shows a toast whose «تراجع» brings it back.
- [ ] **Delete.** With a wrong skip value, «حذف الكتاب…» in the side panel removes the book after one confirmation.
- [ ] **A started book.** «⋯» «التخطيط» opens the grid with bands. A change asks «حفظ وإعادة التعرّف على الصفحة», and
      approved pages are locked. The old link `/books/<id>/guides/` lands on the dashboard.
- [ ] **Re-runs.** A book-wide re-run from «⋯» asks first, with the page count, the approved pages kept and the model
      time.
- [ ] **Arabic keyboard layout.** In review, ش with the word menu open starts a correction, and with it closed
      approves the page; ⌘↵ approves from the menu. On the dashboard V switches the view. On the book page V toggles
      the spread, + − 0 set the fit, and S shows the page marks.
- [ ] **Round trip.** Edit a heading on the book page, then change a word in review. Back on the book page (the same
      tab, no reload) the banner appears. «إعادة بناء الفصل من المراجعة…» asks first and names what is lost.
- [ ] **Book 26.** Approving pages no longer shows «تغيّر نص … بعد التحرير».
- [ ] **Source marks.** Merge two paragraphs on the book page with Backspace; both source pages still show in «الأصل».
- [ ] **No flags.** Review of a page with no flags reads «لا علامات».
- [ ] **Throughout.** RTL, Western digits, Arabic copy, keyboard reach, reduced motion.

**7b**
- [ ] **Clean flags.** On a new upload, or after `rebuild_lines` on a book without an edited manuscript: punctuation is
      never flagged; a year all three readers read alike is not flagged; a word with Cyrillic or CJK letters is flagged
      with «حروف ليست عربية».
- [ ] **The vote.** Where the models disagree and Tesseract backs Qari v0.2, v0.2's reading is in the text, marked
      «● في النص», and still open; Enter confirms it.
- [ ] **A dropped line.** On book 23's page 1 range, the dropped line comes back as a dotted group «كلمات أضافتها
      القراءة الثانية». Enter keeps it, and «حذف الكلمات (12)» drops it.
- [ ] **One reader.** A page read by one model shows «قراءة واحدة» and the banner, and its thumbnail the half-disc.
- [ ] **Roles.** Mark a line «حاشية», and a range with ⇧-click: the manuscript puts them in the footnotes. «محتوى»
      pulls a footnote line back. «شعر» keeps a verse line on its own.
- [ ] **Attention list.** It no longer lists approved pages.
- [ ] **Readiness.** «قبل الإخراج» lists «نص قد يكون ناقصًا…» and «قُرئت … بنموذج واحد» where they apply.
- [ ] **PDF copy.** Copying from the screen PDF in Chrome gives the right lam-alef (if Q1 is "reverse").
- [ ] **The report.** `rebuild_lines --report` prints the numbers of §2.4.

**7c**
- [ ] **The stage bar.** Every book screen shows it in the top bar. Each step links to its screen, and the states
      update after an approval, an assembly or an export without a reload.
- [ ] **Review's origin.** Review opened from the book page shows «‹ الكتاب». Approval stays on the page and offers
      «العودة إلى الكتاب». After the last page, the end panel names the next step. N after A does not skip.
- [ ] **Tiles.** A grid tile opens review in a new tab or with a modifier-click; «تفاصيل المعالجة» opens the technical
      page.
- [ ] **The round trip.** Edit a heading and a word on the book page, then change another word of the same page in
      review. «تغييرات المراجعة» lists exactly that paragraph as «من المراجعة». «أخذ التغييرات» takes it and the
      heading stays; «تراجع» restores; a conflict shows «نصّي | المراجعة». Legacy books show «للمقارنة» items that
      start on «نصّي».
- [ ] **Fix everywhere.** «تصحيح في كل الكتاب» for «السعودي» → «المسعودي» in «كتابي»: the sheet lists the occurrences
      with crops; applying fixes them and «تراجع» reverts. On the edited book, find & replace opens pre-filled, and
      afterwards the pages settle.
- [ ] **Names.** «تجميع المخطوطة» and «ترتيب الصفحات» appear, and «لوحة الكتاب» appears nowhere.
- [ ] **Links.** A copied link keeps its tab (`?tab=`), and `?block=` opens on the paragraph.
- [ ] **Throughout.** RTL, Western digits, Arabic copy, keyboard reach, reduced motion; the rail on every book screen.

**7d:** the list is written with its design.

## 11. Risks

| # | Risk | Handling |
|---|---|---|
| R1 | A pause for books that need nothing | extraction starts with the upload; «بدء المعالجة» is enabled the moment the last page is prepared; one click |
| R2 | A book stuck in «التخطيط» (a lost chord callback, the broker down) | every preprocess task refreshes the book under a lock; extraction and start revert on a broker failure; «إعادة استخراج الصفحات» from error |
| R3 | A race at «بدء المعالجة» (the chord callback after the click) | `continue_ocr` is fixed at ingest; the callback leaves a started book alone; the claim is one conditional UPDATE |
| R4 | Old words with new meanings («قيد المعالجة» now means OCR; «مُعالَجة» becomes «مُجهَّزة») | the RUNBOOK and screenshots are updated by the integrator; the labels are tested |
| R5 | A thick underline taken for a rule on a new book (in the lower 60 %, text above and below, fill ≥ 0.6) | strict first; none on 230 pages; the grid shows it at a glance; «إزالة من هذه الصفحة» |
| R6 | Book fallback lines are blunt: a book footnote line applies to every page without a detected footnote | the preview counts the pages; per-page «إزالة من هذه الصفحة» |
| R7 | Pages with review work no longer follow book guide changes in «المعالجة» | deliberate (their footnote attribution would be lost, `Line.region` SET_NULL); the preview lists them as locked |
| R8 | Moved keys retrain the owner's fingers (S, 1 / 2 / 3 on the book page, 1 / 2 on the dashboard) | the sheets and titles show the new keys; owner question 4 |
| R9 | The vote puts v0.2's reading into the text before review | it stays open, marked and counted; measured right in 76 % of cases; owner question 2 |
| R10 | Groups add words to the text before review | only with Tesseract support ≥ 0.7; marked; dropped in one step; unsupported runs stay suggestions; «لا يدخل الكتابَ نصٌّ مقترح لم يُحسم» |
| R11 | Mixed flag semantics until `rebuild_lines` runs; rebuilding edited books floods the round trip | the report first; edited books skipped until 7c |
| R12 | The merge (7c) touches the edited text | only on apply; a snapshot first; untouched blocks byte-identical; «نصّي» by default for legacy items and conflicts; the byte-identity gate on all 9 manuscripts |
| R13 | The base doubles the manuscript's size and its hot-path load | `defer("base")` on autosave and state; snapshot pruning unchanged |
| R14 | The PDF copy direction: fixing Chrome costs Preview and Safari | the owner's choice (Q1); only Arabic entries are reversed; Acrobat checked once |
| R15 | The Phase 7 evidence lives only in the scratchpad | stage 0 copies it into `playground/phase7/`; this spec carries every number an implementer needs |
| R16 | Fable budget | only the «التخطيط» mode runs on Fable; the contract fixtures let it build without rework |
| R17 | Deleting a book is irreversible | a dialog that names what is lost; no bulk delete; rows and folder gone |
| R18 | A dense top bar in 7c (title, stage bar, actions) | collapse rules; the title hides below 900 px; the owner judges it in 7c's hand test |
| R19 | Test churn from Arabic labels | the changed assertions are listed (§8); the retired strings are enforced by a test |

## 12. Questions for the owner, and what was decided without asking

**Answered 2026-09-26:** "start with Phase 7. skip 8-bit test. do the full plan." All five as recommended; 7a, 7b
and 7c are built in one run (gates between sub-phases, the owner's hand test at the end); 7d follows.


1. **Copy direction of the PDF text (asked by the Phase 6 review).** WeasyPrint writes one ToUnicode entry per
   ligature, in logical order.
   - As the PDFs are now, Apple's engine (Preview and Safari, probably iPhone and iPad) copies right, while Chrome,
     Firefox, poppler and MuPDF reverse lam-alef.
   - Reversing the multi-character entries takes Chrome from 66 % to 99 % of words right on book 23, and Preview from
     99 % to 66 %.
   - **Recommended: reverse, with one rule for both PDFs, and only for Arabic entries.** Most readers use Chrome's
     (PDFium), Firefox's (pdf.js) or MuPDF-based viewers. Check Acrobat once. On the Mac, test copying in Chrome, not
     in Preview.
2. **Flag policy v2.**
   - Punctuation is never flagged.
   - A Western number that all three readers read alike is sure (amends D17).
   - When Tesseract backs Qari v0.2 against v0.3, v0.2's reading goes into the text but stays open (activates D26).
   - Foreign letters are flagged.
   - Measured: flags 275 → 144, precision 33 % → 62 %, catch rate 63 % → 61 % (65 % with the single-reader flags). The
     right reading is already in the text for 76 % of the disagreements, against 40 % today.
   - **Recommended: yes.**
3. **Unreviewed pages in «تجميع المخطوطة».** Keep D35 (included by default and marked amber; readiness now lists
   them), or leave them out by default, as the round-trip design proposed?
   - **Recommended: keep D35, and show the count on the button** («تجميع مع 5 صفحات غير مُراجَعة»). Off by default
     would build nothing on a fresh book.
4. **Moved keys.**
   - On the book page: the spread moves from S to V, and the fits from 1 / 2 / 3 to + − 0.
   - S means «فواصل الصفحات الأصلية» everywhere.
   - On the dashboard: the view moves from 1 / 2 to V.
   - In review: letters type into an open word menu, A approves only when the menu is closed, and ⌘↵ approves from
     anywhere.
   - **Recommended: yes.**
5. **Names.**
   - «تجميع المخطوطة» for «تحويل إلى كتاب»; «ترتيب الصفحات» for pagination.
   - «الكتاب» only for the typeset book, and «لوحة الكتاب» retired.
   - `layout_done` → «بانتظار التعرّف»; the re-run step → «تحديد المناطق».
   - **Recommended: yes.**

**Decided without asking** (they follow the owner's stated rules; each can be reversed with one change):
- **Thick rules are accepted automatically** (D68). Detection usually works, and there are no suggestions to accept.
- **Born-digital books take the same pause.** One flow for every book; the banner says the text comes from the text
  layer.
- **«بدء المعالجة» is disabled until every page is prepared.** It keeps the start exact without locks. A click
  remembered during preparation, for 800-page books, is not in Phase 7.
- **The guides screen goes** (he dropped «ضبط الأدلة»); its URL redirects to the dashboard.
- **Old edited books start every differing paragraph on «نصّي»** (7c): nothing changes until he picks, with per-page
  «خذ ما جاء من المراجعة في هذه الصفحة».
- **The stage bar sits inside the top bar (D34), and the icon rail is on every book screen.** He sees both in 7c's
  hand test.
- **Verse and the line audit** are asked in 7d.
- **Also decided by this plan:**
  - No time estimate on «قيد المعالجة», because D22 rejected one. The re-run confirmation states the model time as a
    cost before a costly action.
  - The needs_guides dot stays amber («your turn»), and the books list reads «بانتظار «بدء المعالجة»» instead of a full
    green bar.
  - In «المعالجة», guide changes have an explicit save and no undo, since each one re-reads pages.
  - The verse role in 7b keeps verse lines apart without pairing them; 7d pairs them into bayts.
  - The book page's «تحويل إلى حاشية للعلامة (n)» and review's G / O / V / C keys land in 7c, with the agents that own
    those files.
  - Fix everywhere moves an edited book's baseline only where the book text agrees with review after the pre-filled
    find & replace.
