# Phase 7 plan — critic's report (verbatim summary for the writer)

L = LAYOUT_DESIGN_FINAL.md, R = roundtrip_design.md, T = TRUST_DESIGN.md. Checked against main at 02f108a and the dev
DB (25 books, 9 manuscripts, all 9 edited; the suite collects 895 tests).

## 1. Contradictions and overlaps
1.1 Decision numbers collide (L D64–D71, R D70–D84, T D80–D91; T cites stale numbers). D62 is reserved (Word
calibration), D63 is used. Number in build order from D64, merging small ones. Proposed map:
D64 two stages + a pause flag whose default is today's path (L D64, D66, D71); D65 names of stages, statuses, re-run
steps (L D65 + 1.9); D66 the upload extracts, the kept range is shown (L D67, R D82); D67 the «التخطيط» mode (L D68–D70);
D68 thick separator rules accepted at detection, strict first (L D70, T D89.1); D69 one keymap by physical key (R D74);
D70 no silent chapter replacement, approval-only is not drift, live drift, merges keep both source marks (R D70 refusal,
D72 part, D73); D71–D75 7b: flag policy (T D80–D83), second-model-only words (T D84), honest page state (T D85, D87),
footnote/verse roles + continuation guard (T D89.3, D90), readiness rows (R D81 minus the Phase 6 row, plus T §3.5);
D76–D79 7c: stage bar and navigation (R D75–D78), one term per concept (R D79), page-by-page merge (R D70–D72), fix
everywhere (T D88 without learning); D80+ 7d as built.
1.2 The footnote rule is designed twice. L stores refused rules as Preprocess.rule_candidates (suggestions); T applies
them automatically at «بدء المعالجة» — that breaks L's promise that what «التخطيط» shows is what OCR reads. Line height
in the DB is 19.5–23 px, mostly 20, so the limit is 6.0 (T right, not L's 6.3). Fix: ONE change inside
detect_footnote_rule (processing/pipeline.py:340-378), strict first: accept a strict rule if any; only when none, take
the widest component with 0.3·med_h < area/cw ≤ 0.5·med_h and fill area/(cw·ch) ≥ 0.6. Measured on all 223 included
pages of the 25 books: changes book 25 pages 1 and 3–7 and nothing else (book 20's thick text rows have fill ≤ 0.56).
Strict first matters: book 16 page 2 (a table page with 3 px rules and one 4.0 px rule) — a merged "widest wins" test
moves its rule from y 1967 to y 1839. Then drop rule_candidates, the suggestion line, «الحاشية تبدأ هنا / ليس خطًّا
فاصلًا», «اعتمادها كلها», detect_rule_candidates and the rule_suggested doubt.
1.3 T's text-assisted footnote zone (D89.2) runs in run_full_ocr after the owner's check and writes guides_override
(+ footnote_by): it re-adds footnotes where the owner chose «إزالة من هذه الصفحة», shows as «بضبط خاص», and
«التلقائي» wipes it; its only measured gain beyond 1.2 is one page (23/5). Fix: defer to 7d (if kept: a detection
source after the smaller-type block in resolve_layout, never in guides_override, skip pages with "footnote_line" in
the override).
1.4 Keymap: V everywhere for the view (also in «التخطيط»; L kept 1/2). Drop L's B/H/F and ⌘Z (the dashboard's
keyAction returns null on meta/ctrl, books.js:756; the toast's «تراجع» is enough). T's ⌫ on an inserted group would
delete 12 words: ⌫ always deletes one word; the group's popover carries «حذف الكلمات (12)»; ⌫ = dismiss on gaps is
fine. T's ⌥F goes into R's table and core/test_keymap.py.
1.5 Drift: a mirrored page (signature changed, no counted revision) would be announced as «أعادت المعالجة قراءة…»;
the mirror moves the baseline for those pages (the included[P] signature/at and the base splice) — no `mirrored` flag.
Undone revisions: count every revision after `at`, including undone ones, as `review`.
1.6 Readiness: the Phase 6 review is building pages_unreviewed, pages_missing, stray_notes and the `clear` rule now —
remove them from R D81 (Phase 7 only extends). Two stray_notes rules exist; use R's measured rule (2 true, 0 false);
T's unmatched-call condition only picks the label «حاشية للعلامة (n)». publishing/readiness.py gets one owner.
1.7 Upload: drop R's «التخطيط» first screen before «استخراج الصفحات» (L extracts at once). The form shows R's live
range line (client-side /Count probe, exact on 26 of 40 PDFs, else the rule in words); the success message gives the
exact range (the server knows the count, books/forms.py:120-161); the «التخطيط» side panel repeats «الصفحات 187–193 من
555» next to «حذف الكتاب».
1.8 Stage bar vs «التخطيط»: R's `layout` step "done when every page is at layout or later" reads "not started" at
«تم التخطيط» (pages are `preprocessed`); its link goes to the dashboard which after the start shows today's view; the
key `layout` already means four things (this step, the re-run stage inside «المعالجة», the book page URL/template, the
Celery queue). Fix: keys `pages` («التخطيط») and `ocr` («المعالجة»); `pages` done once OCR has started or every page
is prepared, shows «تم التخطيط · 7 صفحات» at the pause; links to the dashboard while paused, to ?view=guides after the
start; 7a keeps L's chip; 7c folds it into the current step.
1.9 Status names: Page.Status.LAYOUT_DONE is already «تم التخطيط» (books/models.py:168) and L gives needs_guides the
same label; STAGE_LABELS["layout"] is «التخطيط» (books/services.py:65) for a re-run step inside «المعالجة»;
book_dashboard hard-codes «مُعالَجة»/«تم التخطيط» (books/services.py:989-990); L relabels Region.Source.GUIDES as
«تلقائي», AUTO's label (processing/models.py:116-117). Fix: layout_done → «بانتظار التعرّف»; re-run `layout` →
«تحديد المناطق»; the dashboard list reads Page.Status labels; GUIDES → «من التخطيط».
1.10 Data model: books migrations 0007 in 7a (L), 0008 in 7b (T Page.reading), one owner each; R relabels
PreviewRender.Status (publishing migration) in 7c after the Phase 6 review merges; R's Manuscript.base on a hot row
(manuscript_of(lock=True) loads the whole row every 1.5 s autosave — 868 KB + 868 KB for book 19): defer("base") on hot
paths.

## 2. What would break existing logic (verified)
2.1 L D64's gate default (ocr_started_at = null means «التخطيط») makes every book not made by create_book a paused
book; fixtures create books with status=OCR (ocr/tests.py:54, processing/tests.py:430, books/tests.py:335) and ~110
test lines call run_stage / rerun_book / after_preprocess / OCR tasks that the gate would refuse; L also needs a
three-condition RunPython and a books→ocr migration dependency. FIX: invert the default —
Book.awaits_ocr_start = BooleanField(default=False), set only by create_book, cleared by «بدء المعالجة». Old books and
fixtures keep today's path, no data migration; start_ocr_task reduces to _enqueue_layout; keep L's continue_ocr,
fixed at ingest time (stops a double enqueue if «بدء المعالجة» is clicked before the chord callback runs).
2.2 L D71 inside _derive_regions also serves rerun_preprocess (processing/services.py:217-246, allowed on unapproved
pages with review work) and layout re-runs; freezing regions there leaves them in the old image's pixels. Fix: skip
pages with review work only where guides change (apply_guides, set_page_guides_override, the new guide services).
2.3 Rule merge order: see 1.2 (book 16 p2).
2.4 start_ocr with no prepared page (the owner excludes the last page → refresh_status returns early, total 0,
books/models.py:113 → the book goes to `ocr`, nothing is queued, polls forever): refuse with «لا صفحات جاهزة للمعالجة».
2.5 R's plans stored as AssemblyRun rows would trigger readiness's «يجري التجميع» (publishing/readiness.py:158) and the
chapter-rebuild refusal (editor/services.py:616): keep plans off AssemblyRun; at apply write one `done` run as
_save_chapter_run does.
2.6 R's legacy `insert` can duplicate text (pre-D73 merges kept only the first block's line ids: 13 lines lost in
book 13, 7 in book 25): with no base, a fresh block with no partner on a page that is not `added` becomes `choose`,
default «نصّي».
2.7 Restore and the base: restore goes through _write (editor/services.py:372-410); if it copies the current document
into base, review changes between the snapshot and now drop silently: restore sets base = snapshot.base (or null for
older snapshots).
2.8 Approval-only drift "absorbed at the next merge": with no content drift there is no merge, D36 `assembled` never
flips, the manuscript keeps D35's amber mark on now-approved pages (assembly/render.py:282, manuscript.css:207): derive
the mark from live page status; in 7a stop announcing approval-only drift (book 26 shows 5 such pages; today the only
action is the destructive chapter rebuild).
2.9 T's majority vote must never touch `digit` tokens or D51's single-letter number candidates (lone «ا/ه/ع»); test
that ocr/test_numbers.py gives the same tokens with the vote on.
2.10 T's rebuild_lines on edited books: all 9 manuscripts are edited, none has a base; rebuilding their unreviewed pages
(77 in «كتابي») floods the round trip with `choose` items: only on books without an edited manuscript until 7c's base.
2.11 R D84 contrast: --color-text-3 also colours the owner-approved provisional decode gray and shimmer (D28/D29;
theatre.css:16,23,555-560): add --color-provisional: #8a8a93, darken only hints and meta text.
2.12 R D80 silently reverses owner decision D35 (default off builds nothing on a fresh book): owner question.
Checked fine: book statuses (13 ready for review, 6 reviewing, 6 assembled; no page overrides; manual guides only on
book 5); _enqueue_layout and run_stage reset paths; L's excluded-page branch; re-deriving regions inside run_full_ocr;
approve_page uses QuerySet.update (approval leaves the signature alone); Line.region SET_NULL (L's footnote-attribution
bug is real).

## 3. Scope and order
7a — the owner's request + blockers 1 (safety part) and 3 (medium risk, contained by the flag default; the only text
change is book 25's footnotes): the «التخطيط» / «المعالجة» split with the §2.1 flag, re-runs, exclusions and task
gates; the «التخطيط» mode trimmed as in §4, plus ?view=guides with an explicit save for books already started (the old
guides screen redirects); thick rules at detection (1.2); upload: «استخراج الصفحات», the range line and message,
«حذف الكتاب»; the labels of D65 and 1.9; one keymap by physical key incl. the dashboard's V; round-trip safety (the
chapter rebuild needs replace_edited + a confirmation dialog; approval-only changes not announced; live drift on focus,
visibility, pageshow, BroadcastChannel; mergeNodes unions the source marks, D73); «لا علامات» on pages with zero flags.
7b — trust (blocker 4) and footnote structure (mostly backend, checked by replaying stored data): flag policy v2 with
reasons and the majority vote; second-model-only words become inserted groups or gaps (no new model calls);
single-reader flags, a minimal Page.reading, a pill/banner in review; footnote and verse line roles (incl. a line range
and the manuscript's «حاشية للعلامة (n)») + the continuation guard; readiness rows (1.6); the attention list clears on
approval; rebuild_lines dry run first (2.10); the PDF text-layer fix (Q1); optional last: years vs the number in words.
7c — navigation and the full round trip (highest risk): stage bar, icon rail, review's origin and detour, the
end-of-review panel, the N rule, tiles open review, ?tab=/?block=, the names of D79; the merge (stored base, word-level
three-way merge, the «تغييرات المراجعة» tab; legacy books get `choose`; approval-only changes absorbed); fix everywhere
with batch undo (for an edited book it opens the book page's find & replace pre-filled and moves the baseline).
7d — after 7c's hand test: line audit and re-reading of looped regions (GPU); verse (detection and one-بيت-per-paragraph
first, then flex halves in HTML/PDF, then Word/EPUB after D62); review stacking below 880 px, book-toolbar overflow,
labelled side tabs, contrast with split tokens.
Not in Phase 7: phone layouts; remembered corrections and propagation; the text-assisted footnote zone; Word verse
tables; the Kraken + Qari v0.2 key and a vowelled-books option; starting «بدء المعالجة» early on 800-page books;
simplifying the page detail.

## 4. Over-engineering and missing pieces
Cut — L: the rule suggestions (1.2); the gray/b&w toggle; B/H/F/⌘Z; draft bands on every thumbnail (keep the count and
the list of cut lines); the page-number zone control for all pages (keep «ليس رقم صفحة» per page); doubts that never
fired on 217 pages (footnote_from_book, large_skew). R: rebuilding the base from LineRevision.before (the 9 edited
manuscripts are test data; `choose` is enough); the 80 % rescue once 2.6 holds; the 30 s and 60 s polls; phone bars;
D80. T: the learning / repeat / propagation machinery; the text zone; Word/EPUB verse; the agreement-% pill (only
«قراءة واحدة» and «نص Tesseract وحده» carry information).
Missing: «حذف الكتاب» (none today, books/urls.py; with extraction on upload a mistyped skip value leaves a 555-page book
only the admin can remove); confirmation for book-wide re-runs in «⋯» (page count, approved pages kept, GPU time);
undo for «استثناء الصفحة»; the attention list clearing after approval (T adds three flags to it); a time estimate on
«قيد المعالجة» from measured run durations; open the «التخطيط» mode on the grid (one glance = every page's bands with
the doubt marks; the viewer is for fixing a page); the books list: «تم التخطيط» at 100 % green reads finished — show it
as waiting («بانتظار «بدء المعالجة»»).

## 5. Build plan
Fable only for the «التخطيط» mode UI; everything else Opus; one Opus review after the owner's hand test of each
sub-phase. File ownership:
7a A split (Opus): books/{models,services,tasks,views,urls,api}.py + 0007, smoke command; processing/{pipeline,
services,tasks,api,views,urls,models}.py; ocr/tasks.py; core/templatetags/nassakh.py; nassakh/urls.py; Python tests in
books/processing/core (+ gate cases in ocr).
7a B mode UI (Fable): templates/books/* (delete processing/guides.html), static/src/js/{books,processing}.js,
{theatre,processing,books}.css; core/test_theatre.py, test_frontend.py.
7a C keys + safety (Opus): static/src/js/keys.js (new; one line in base.html), review.js, manuscript.js, book/*.js,
static/src/editor/convert.js, templates/{review,editor,assembly}/*, editor/{services,api}.py, the stale_pages option in
assembly/services.py; core/test_{keymap,review_ui,layout_ui,manuscript_ui}.py, editor/assembly tests.
7b D trust backend (Opus): ocr/* (+ flags.py, migration), review/* services/api/models + migration,
assembly/{pipeline,services}.py, books/models.py + 0008, readiness.py.
7b E trust UI (Opus): review and manuscript JS/templates/CSS, the tile mark in books.js.
7c F round-trip backend (Opus): editor/* (+ merge.py, migration), assembly/services.py, review/services.py payload,
books/services.py (book_stages, primary_url), publishing label migration.
7c G navigation UI (Opus): base.html, _stage_bar.html, stages.js, layout.css, review.js, book/panel.js + _book_side.html,
books.js/detail.html, renames.
All: the integrator owns the tracked static/dist/app.css and editor.js (npm run build once per sub-phase), docs, the
final run. Order: A publishes L §9's JSON contract before B starts, B builds against stubs; C lands keys.js first so B
can use it for V.
Gates every sub-phase: the full suite (895 + new), manage.py check, makemigrations --check; migrate a copy of the dev
DB, then GET every screen of all 25 books → 200. Extra: 7a all 25 books stay on today's path; resolve_layout ==
page_layout on every stored page except book 25 pp. 1, 3–7; the smoke command with and without --layout-only; Node
tests with Arabic-layout key events. 7b rebuild_lines --report reproduces T's numbers before writing (books 23 + 25:
144 flags at 62 %; the 14 reviewed books: 518 at 54 %); pages with review work byte-identical; ocr/test_numbers.py
unchanged. 7c plan-then-apply with every choice on «نصّي» leaves all 9 manuscripts byte-identical; incidents 23/8 and
26/3 each give one `take`; the stage bar ≤ 8 queries.

## 6. Questions for the owner (the few that need him)
1. PDF copy direction (Phase 6 review): WeasyPrint writes one ToUnicode entry per ligature in logical order. As is,
Apple's engine (Preview, Safari, probably iPhone/iPad) copies right; Chrome, Firefox, poppler, MuPDF reverse lam-alef.
Reversing multi-character entries: Chrome 66 → 99 % of words right on book 23, Preview 99 → 66 %. Recommend
reversing, one rule for both PDFs (most readers are on Chrome/PDFium-, pdf.js- or MuPDF-based viewers; check Acrobat
once; on his Mac, test copying in Chrome, not Preview).
2. Flag policy v2: punctuation never flagged; a Western number all three readers read alike is sure (amends D17); when
Tesseract backs Qari v0.2 against v0.3, v0.2's reading goes into the text but stays open (activates D26); foreign
letters flagged. Measured: flags 275 → 144, precision 33 % → 62 %, catch rate 63 % → 61 % (65 % with single-reader
flags); the right reading already in the text for 76 % of disagreements against 40 % today. Recommend yes.
3. Unreviewed pages in «تجميع المخطوطة»: keep D35 (included by default, marked amber; readiness now lists them) or off
by default (R D80)? Recommend keeping D35 and showing the count on the button (off-by-default builds nothing on a fresh
book).
4. Moved keys: book page spread S → V, fit 1/2/3 → + − 0; S = «فواصل الصفحات الأصلية» everywhere; the dashboard view
1/2 → V; in review letters type into an open word menu, A approves only when it is closed, ⌘↵ approves from anywhere.
Recommend yes.
5. Names: «تجميع المخطوطة» for «تحويل إلى كتاب»; «ترتيب الصفحات» for pagination; «الكتاب» only for the typeset book
(retire «لوحة الكتاب»); layout_done → «بانتظار التعرّف»; the re-run step → «تحديد المناطق». Recommend yes.
Decided without asking (they follow his stated rules): thick rules accepted automatically; born-digital books take the
same pause; «بدء المعالجة» disabled until every page is prepared; the guides screen goes (he dropped «ضبط الأدلة»); old
edited books start every differing paragraph on «نصّي»; the stage bar inside the top bar (D34) and the icon rail on book
screens (he sees both in 7c's hand test); verse and the line audit asked in 7d.
