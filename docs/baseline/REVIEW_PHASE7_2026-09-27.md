# Phase 7 quick review (2026-09-27, the owner's request before the hand test)

Three Opus reviewers, one per sub-phase (7a `7daf355`, 7b `b117597`, 7c `db80efe`), read the code at HEAD
and reported only findings they proved (a failing scratch test or a traced code path). Two Opus fixers then
fixed all 16, each with a regression test that failed before the fix.

| # | Sub-phase | Severity | Finding | Fix |
|---|---|---|---|---|
| 1 | 7c | high | A paragraph running over two drift pages was filed under the first page only: taking (or settling) the other page moved the base over it, and review's change never reached the book | an item is listed under every page it touches and applies when any of them is taken; no page of an item left out moves its baseline or base |
| 2 | 7c | high | Plan item ids were positional (`i1…iN`): after a 409 re-plan the owner's choices landed on other paragraphs (a deleted paragraph came back) | stable ids from the item's identity (`merge.item_id`); unticked pages kept across the re-plan |
| 3 | 7c | medium | «خذ ما جاء من المراجعة في هذه الصفحة» set `theirs` on merged items, dropping the owner's own edits in those paragraphs | merged items get `merged` |
| 4 | 7c | medium | The book-side replace of «تصحيح في كل الكتاب» ignored the sheet's options (whole word off), and the wrong extra replacements were settled as agreeing with review | the three options travel in `find_url`, the prefill and the book page's find |
| 5 | 7c | medium | Undoing the first edit (a snapshot of the assembled text) left an edited book with no base, so review's changes defaulted to «نصّي» | a snapshot of an unedited text records the text itself as its base |
| 6 | 7c | low | Review's fix-everywhere POST omitted the sheet's options, so forms listed with «كلمة كاملة» off were skipped as "changed" | `applyFix` sends them |
| 7 | 7c | low | A review undo after an apply read as «إعادة المعالجة» | an undone revision whose line changed after the page was read counts as review (undoing an insert or a delete still reads as processing: it leaves no line to date) |
| 8 | 7b | medium | `rebuild_lines` checked for review work once, before the slow rescue: a resolve made meanwhile was wiped, an approval reverted | the page is re-read under a lock after the rescue and checked again; the approval is read from the database |
| 9 | 7b | medium | A suggestion (gap) was re-anchored by a stale `after_t` after the word before it was resolved, so its words landed elsewhere | resolve, fix everywhere and the numbers pass keep the anchor with its word |
| 10 | 7b | medium | A lone comma from Qari v0.2 counted as its reading of a whole word: a false «النموذجان مختلفان» and a reading that deletes the word | a mark is no reading of a word; the vote never puts a mark in place of a word |
| 11 | 7b | low | A suggestion on a deleted line kept «نص قد يكون ناقصًا» in «قبل الإخراج» for good | the row counts only suggestions on a line, as review does |
| 12 | 7b | low | Where Qari v0.3 looped under v0.2's text, v0.3's clean prefix became the "second reading" and review named the models the wrong way round | that region counts as one reader |
| 13 | 7a | medium | «إزالة الضبط العام» could never be applied: its preview ignored the reset and said nothing changes | the preview previews the reset |
| 14 | 7a | medium | On a started book, «التلقائي» then a drag saved only the reset and lost the drag | the reset clears, then the drag applies |
| 15 | 7a | low | Esc with the band menu open dropped the page's unsaved move and left the menu open | Esc closes the menu first |
| 16 | 7a | low | Undoing an exclusion in an all-failed book left it in error | the status is re-derived |

**The flag measurements after fix 10** (`rebuild_lines --report`, read-only): books 23 + 25: 143 flags, 62.2 % useful,
61.4 % caught (7b's gate: 144 / 61.8 % / 61.4 %); the 14 books: 518 / 54.2 % / 73.6 % (519 / 54.1 % / 73.6 %). One useless
flag is gone («(دولفين» on 25/4); the others that lost a mark-only reading stay flagged for another reason.

**Left open**
- A page `rebuild_lines` refuses after the rescue is listed under "failed" rather than "skipped" (nothing is written).
- 11 `ocr_done` regions on the dev database (books 15, 19, 20, 22) still carry the wrongly labelled looped prefix from
  7b until a `rebuild_lines` write (which waits for the owner).
- The «نص قد يكون ناقصًا» row links to `review:next`, which cannot reach an approved page (after a forced approval
  with open suggestions); the spec names that link.
- Undoing an insert or a delete in review after an apply still reads as «إعادة المعالجة».
