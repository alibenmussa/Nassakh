# Phase 7 design: trust in the text and the book's structure (D80–D91)

**Note for the writer first.** The round-trip draft in the scratchpad (`roundtrip_design.md`) also uses D80–D84, for defaults, readiness, page range, narrow screens and legibility. I kept D80–D91 as instructed. One of the two sets needs renumbering when they are merged.

Two parts line up with the other drafts:
- **Rule candidates.** The layout draft (`PHASE7_LAYOUT_DESIGN.md`, D70) reached the same fix for the footnote rule on its own: thickness ≤ 0.5 × core height when fill ≥ 0.6. It stores these as `Preprocess.rule_candidates` and offers them as suggestions in «التخطيط». §3.7 below reuses that field and adds only an automatic use at «المعالجة».
- **Keys and drift.** New keys follow the round-trip D74 keymap: physical keys, a word mode and a page mode. The book-wide fix gives the round-trip D72 drift a `mirrored` flag, so its fixes are not announced as drift.

All probes are read-only. They are in `scratchpad/phase7/` (`NOTES.txt` lists each script). No project file was modified.

---

## 0. Summary

Nassakh has three readers on every region: Qari v0.3, Qari v0.2 and Tesseract. Its confidence rule looks at only two of them, and it reads punctuation as disagreement:
- `low = digit or disagree[i]` (`ocr/alignment.py:1026`);
- a word counts as a disagreement when the second model has no counterpart for it (`:753-754`), after a whitespace split. So «القرآن .» against «القرآن.» flags the «.».
- Words that only the second model read are thrown away (`:750-752`, `if i is None: continue`). That is where the dropped lines go.
- When a model loops, the region has one reader and no word flags at all (`ocr/services.py:1050-1052`).
- A printed line that every reader skipped leaves no trace except an empty band.

The design:
1. **One rule, three readers.** A word all readers see alike is sure. Where the two models disagree, the reading Tesseract backs is put in the text, and the word stays open. A word only one model read is flagged, or offered for insertion. A printed band nobody read is read again alone. Punctuation is never flagged.
2. **Honest page state.** «لم تُراجَع» stays until approval. Every page shows how it was read: two readings, one, or Tesseract only.
3. **A book-wide fix** with a preview and a batch undo, plus remembered corrections.
4. **Structure.** Footnote and verse become line roles. The footnote rule fix is shared with the layout draft, and a text-assisted footnote zone is found before the models run. Each بيت is one verse paragraph whose hard break splits صدر from عجز. It prints as two justified halves (a WeasyPrint spike confirms flex does this).

### Measured effect (stored data, no model runs)

| Problem | Today | With this design | Source |
|---|---|---|---|
| Flags, UX books 23 + 25 (4,526 words) | 275 flags, 33 % correct, catch 63 % of errors | 144 flags, 62 % correct, 61 % caught; 65 % with the single-reader fallback | `evaluate.py`, `policy.py` |
| Flags, 14 reviewed books (14,063 words) | 675 flags, 43 % correct, 75 % caught | 518 flags, 54 % correct, 74 % caught | same |
| Punctuation flags | 95 flags, 6 real errors | never flagged | same |
| Western numbers (184 flagged) | all flagged | 70 sure (all three readers agree), 0 errors among them | same |
| Which reading goes in the text when the models disagree | always Qari v0.3 (right 40 %) | the reading Tesseract backs (right 76 %) | 177 disagreements |
| Page 23/8: 14 real errors, 0 flags | «0» | «قراءة واحدة» + re-read of the missing half; Tesseract fallback flags 23 words (5 real) | `features.py` |
| Words or lines only the 2nd model read | discarded | 104 suggestions on 217 pages; on reviewed pages 8 of ~10 were real, and reviewers had missed 6 of those 8 | `s1_filtered.py`, crops |
| Printed text bands with no line at all | invisible | 69 bands on 46 of 180 pages: 33 beside a merged line, 23 likely dropped, 13 heads | `dropped.py`, crops |
| Years printed in digits and words (book 23) | 14 flagged, no suggestion | 10 wrong years get the right value first; 4 correct ones confirmed | `numwords.py` |
| «السعودي» for «المسعودي» in «كتابي» | 160 on 51 pages; 117 flagged one by one, 43 unflagged | one book-wide fix | DB count |
| Footnotes, book 25 (8 notes) | 1 | 7 (6 linked correctly; 1 wrongly continued, fixed by the guard in §3.7) | `fn_assemble.py` |
| Footnotes, book 23 | 0 of 1 | 1 of 1 | same |
| Verse, book 23 p5 | 5 wrong paragraphs, each gluing one verse's عجز to the next verse's صدر | 6 bayts, rhyme «ر» 100 %; no false verse on 16 books | `verse.py` |

---

## 1. Evidence

### 1.1 Method

For each approved page (reviewed or assembled), I rebuilt the lines the reviewer first saw:
- the `before` of the first live `LineRevision` of each line;
- untouched lines as stored;
- deleted lines from the delete revisions;
- inserted lines (`is_manual`).

Each token was then aligned word by word with the approved lines.
- **Useful flag:** a low-confidence token the reviewer changed.
- **Missed error:** a changed high-confidence token.
- **Unknown:** a low token left unresolved on a force-approved page. These 170 tokens, mostly book 26, are excluded.

"Changed" also counts a few corrections of the print itself: 23/7 «ضرابلس» and 23/1 «جهاد» are printed that way.

### 1.2 Flags today (books 23, 25, 26; `measure.py`)

| Flag reason | Useful / total | Precision |
|---|---|---|
| second model reads differently (`alt`) | 69 / 143 | 48 % |
| second model has no counterpart (`alt` null) | 8 / 135 | 6 %, mostly punctuation from tokenising «word .» against «word.» |
| digits (D17) | 11 / 68 | 16 % |
| Kraken numbers | 5 / 32 | 16 % |

Across the 14 books:
- **Punctuation.** 95 flags, 89 of them correct marks. Where the two models give different marks («،» against «.»), 0 of 48 were errors.
- **Tesseract as a vote.** Where the models disagree and Tesseract agrees with v0.2, v0.2 won in 88 of 116 cases (76 %) and v0.3 in 25. Where Tesseract agrees with v0.3, v0.3 won in 46 of 61 (75 %).
- **Tesseract alone.** On words both Qari models agree on, Tesseract's one-letter differences were errors 16 times in 752 (2 %). Filtered by the dotless skeleton at conf ≥ 70: 9 in 215 (4 %). Tesseract does not earn flags where both models are present.
- **A word the second model lacks:**
  - with Tesseract agreeing: 27 correct, 1 changed. Unflag.
  - with no Tesseract word: 29 correct, 21 changed. Keep the flag.
- **Differences hidden by lenient normalisation** (tanween, hamza, ة/ه, a moved shadda): 310 cases, and the approved text kept v0.3's form in 303 (98 %). Not flagged. See Q4.

### 1.3 One reader, zero flags

`select_text` keeps an alternative only when the second model passes the sanity check (`ocr/services.py:1050-1052`). A looped v0.2 therefore removes every word flag from the region.
- **Share of regions:** 21 + 4 (fallback) of 185 body regions, and 16 + 6 of 81 footnote regions. In «كتابي», 13 of 81 pages.
- **Page 23/8** is the report's «0 uncertain, a dozen errors» page:
  - v0.2 looped on «قـــــ»;
  - 290 words, 14 errors, 0 flags;
  - the clean prefix of the looped run already read «يحيى», «فقيهاً», «فاضلاً», «زاهداً» and «يقرىء» correctly.
- **Coverage of looped prefixes:** 0 to 100 % of their region, median about 45 %.

### 1.4 Dropped text

**Homoeoteleuton is the main cause: the model jumps from one repeated phrase to the next.**
- 23/1 dropped «تعالى بطرابلس ونشأ بها واخذ عن جماعة من الفضلاء وكان رحمه الله». The lines around it end with «رحمه الله» and begin with «تعالى».
  - v0.2 had the whole line. Tesseract line 11 had it too, but only 2 of its 12 words were matched, so it was stored as a line reading «تعالى من».
- 19/61 dropped 11 words between two occurrences of «على أهل كل قرية ما».
- 19/87 dropped «وفيها مغاص اللؤلؤ المعروف بالخاركي،».
- 19/52 dropped a whole line («… فلما وردت مراكب الروس …»).
- 26/2 lost a line in every reading: «فالمفرد نحو: زيد قائم، …». Its Quran quotation «﴿ اللَّهُ الصَّمَدُ ﴾» is also missing.
- 13/2 lost its chapter heading («دراسة حول مدينة برقة»).
- 23/5 lost a hemistich; the reviewer typed it back.

**Words only the second model read (after the filters in §3.3), on reviewed pages:**
- 23/1: the whole line, and «الاجابة»;
- 25/6: «LAMPEDOUSE», «D’ANFREVILLE», and the footnote's «(1) انظر :»;
- 25/7: «DE POINTS» and «فقد»;
- 26/1: «وهُما».

Six of these eight are still missing from the approved text. The filters remove false runs such as «هير» (split from «هيرودوت») and «لها».

**Printed bands that no line covers:** 69 on 180 pages. Beside a merged line: 33 (25/1 and 25/3: Tesseract lost the line and Qari's words went to the line above). Likely dropped: 23. Page tops: 13.

### 1.5 Numbers and dates

**Book 23 prints years twice, «سنة ( ٢٤٢ ) اثنتين واربعين ومايتين».**
- v0.3 wrote «( 23 م )», «(25)» and «27» for three-digit years.
- Kraken misread «٢٤٩» as «٢٤٦».
- The years spelled out in words give the right value in 10 of the 14 wrong years and confirm 4 correct ones.

**Western digits.** When v0.3, v0.2 and Tesseract read the same digits, 70 of 70 were right.

**Kraken.** When Kraken and v0.2 agree digit for digit, 17 of 18 were right. The sample is too small to change D50; see Q3.

### 1.6 Repeated misreadings

- 230 corrected word pairs. 35 of the wrong forms occur two or more times in their book, covering 66 of 261 corrected tokens.
- Book 23: «التوكل» ×4, «الوائق», «المباس», «المعضد», «وفيا», «المالككي».
- Book 19: «السعودي» 160 times on 51 pages, against 103 «المسعودي».
- Guards are needed: «من» → «ليالي» (a misaligned word) must never propagate.

### 1.7 Foreign scripts

In stored tokens:
- Cyrillic ×7, including «مилادية» and «بزنзор», both stored as sure words;
- CJK ×6: «وال德拉ية», «الأ石油化工»;
- «※» (a Quranic verse marker), «^» for «٧» ×6, and «© ¢ € ¥ ≡».

### 1.8 Footnotes

**Book 25.** Every footnoted page has a rule 6.5–7.1 px thick. The limit is `max(3, 0.3 × med_h)` = 6.0, since `med_h` is the 20 px core band (`processing/pipeline.py:361`). All six rules were refused.
- The layout draft measured the same: ≤ 0.5 × core with fill ≥ 0.6 → 6 of 6.
- Book 20's thick blocks are closed text lines with fill ≤ 0.49, and stay refused.

**Book 23 p5.** One footnote line in smaller type after a 184 px gap, with no rule. `BLOCK_MIN_LINES = 2` (`:58`) refuses it. Tesseract read its marker mirrored, as «)1(».

**Line-start markers alone are unsafe.** Book 26 has lists «١ – …» and book 19 has numbered sub-headings «(١) بلاد فارس».

**No footnote role exists.** `set_line_role` refuses headings on footnote lines (`review/services.py:786-787`). The role menus offer only محتوى / عنوان رئيسي / عنوان فرعي (`static/src/js/review.js:41-45`, `static/src/js/manuscript.js:69-71`).

### 1.9 Verse

**What the page looks like.** Book 23 p5 is staggered: a right-aligned صدر (x 565–1217 of 1279), then a left-aligned عجز (x 97–743). Every عجز ends in «ر».

**Why the pairs merge.** `breaks_between` (`assembly/pipeline.py:900-927`) never breaks between an عجز and the next صدر: the صدر starts further right, so it does not count as indented. The result is «الى الهياج؛ ونار الحرب تستعر وفي يدي صارم، افري الرؤوس به».

**Qari v0.3's markup.** Its raw output put each بيت in its own `<h2>`. That markup is thrown away today.

**What the model supports.**
- The editor accepts `paragraph.style = "verse"` and `hardBreak` (`editor/document.py:14-21, 62`, `static/src/editor/schema.js:155`).
- The book model turns a hard break into `LineBreak` (`publishing/model.py:118-120`).
- Every renderer only centres a verse: `publishing/css.py:199`, Word `NkVerse` centred (`publishing/word/styles.py:122`), EPUB centred with `<br/>` (`publishing/epub.py:161`).

**WeasyPrint 70 spike** (`verse_spike*.py`):
- `display:grid` puts the صدر on the **left** in RTL, which is wrong.
- `display:flex; justify-content:space-between` with 46 % halves puts the صدر right and the عجز left on the same row. Each half is a separate line box, which is what the live book page needs.

---

## 2. Decisions (proposed)

**D80 — Every flag has a reason; punctuation is never flagged.**
- A low-confidence token stores `why`: `disagree` · `alone` · `missing` · `number` · `year` · `script` · `corrected` · `single`.
- The word popover shows the reason in Arabic.
- The two models are compared with punctuation split off both sides, so «القرآن.» equals «القرآن .».
- Punctuation-only tokens are sure (94 % of their flags were noise).
- A word the second model lacks is sure when Tesseract read it, and `alone` when Tesseract did not.
- Tesseract's own disagreement is not a flag in two-reader regions (2–4 % precision).

**D81 — Numbers.**
- A Western-digit number that all three readers read identically is sure (70 of 70).
- Arabic-Indic numbers stay Kraken's reading, and low (D50).
- A number followed, or preceded, by its value in words («( ٢٤٣ ) ثلاث واربعين ومايتين») is checked against the words:
  - agreement resolves it (`res: "words"`);
  - a mismatch keeps it open, with the words' value as the first reading (`sug`).

**D82 — The majority reading goes in the text (activates D26, amends it).**
- When Tesseract agrees with one model against the other, that model's reading is `t` (`orig` keeps v0.3's) and the token stays **unresolved**.
- The popover keeps its order and keys (1 = v0.3, 2 = v0.2, 3 = Tesseract) and marks the reading now in the text.
- Enter confirms the reading now in the text. Today Enter restores `orig`.
- Measured: the right reading is in the text for 76 % of disagreements, against 40 % today.

**D83 — Foreign letters and stray symbols are flagged at OCR time (`why: "script"`).**
- Letters from non-Arabic, non-Latin scripts; Arabic and Latin mixed in one token; and symbols outside an allow-list (so «※» and «^» are flagged).
- A clean second reading is put first by the vote.

**D84 — Words only the second model read: presence goes by majority too.**
- **Supported run.** A run with Tesseract support (≥ 70 % of its words among Tesseract's unmatched words around the gap) is inserted before the lines are built. Its tokens have `why: "missing"` and share a group `ins`. The reviewer keeps or drops the group in one step.
- **Unsupported run.** It becomes an `ocr.TextGap` suggestion (not in the text): an amber bar between two words.
- **Filters:** numbers only, duplicates within ±12 words, one word split from its neighbour, short runs at a region start (a running head).
- Open groups and gaps count as open items, as uncertain words do. Tab visits them, the approval dialog lists them, and they are undoable.

**D85 — A loop no longer makes a page single-read.**
- The looped run's clean prefix is the second reading for the words it covers.
- The uncovered rest of the region is read again as a smaller crop by the model that looped (one call, GPU task).
- What is still single-read is flagged against Tesseract: Arabic word, confidence ≥ 85, different dotless skeleton (6 flags, 3 real on the reviewed single-read tokens). The page carries `single_reader`.

**D86 — The line audit.** Before a page is finalised, in the GPU task, at most 4 suspect bands are read alone by the primary model:
- a printed text band no line covers;
- a wide line holding under half the words its width holds;
- a Tesseract line with ≥ 4 readable words that the models barely matched.

The reading becomes one of two suggestions, never inserted text:
- a missing-line `TextGap`;
- a split suggestion, when the words sit in a neighbouring merged line. This adds a `split_line` review action.

**D87 — Honest page state.**
- Review status is «لم تُراجَع» until approval, whatever the counts.
- A zero count reads «لا علامات» with a hint to read the page against the scan.
- `Page.reading` stores readers, agreement between the two models and open suggestions. It is shown as a pill in review, a mark on thumbnails, a banner on single-read pages, and readiness rows.

**D88 — «تصحيح في كل الكتاب».**
- From a correction in review, the word menu, the manuscript and the book page.
- A sheet lists every occurrence with its context and a scan crop. Each one can be unticked; forms a reviewer confirmed are unticked by default.
- The fix is one batch of revisions with a batch undo. It is mirrored into an edited manuscript: the matching words in the blocks sourced from the ticked lines; blocks that also hold an unticked occurrence are skipped and listed.
- The revisions carry `mirrored`, so D72 drift ignores them.
- **Remembered corrections** (`review.Correction`) come from a batch fix, or from the same correction made twice on different pages. Guards: ≥ 3 letters; never confirmed as correct elsewhere in the book. Other unreviewed occurrences get `why: "corrected"` with the fix as the first reading, including pages read later.

**D89 — Footnote detection.**
- The rule test is `thickness ≤ 0.3·core` **or** (`≤ 0.5·core` and fill ≥ 0.6). This is the layout draft's candidate, used automatically at «المعالجة» when nobody decided in «التخطيط».
- A **text-assisted footnote zone** is scored from Tesseract's lines before the models read (§3.7). If found, the page's footnote start is set (`guides_override`, `footnote_by: "text"`) and its regions re-derived, so the notes are read as their own region at 2× (D11).
- A marker-less first note does not continue the previous page's note when this page's body has an unmatched call and the previous note ended a sentence. It is linked to that call instead, with a warning.

**D90 — Footnote and verse are line roles.**
- `ocr.Line.Role` gains `footnote` «حاشية», `verse` «شعر», and `main`. `main` is shown as «محتوى»: it pulls a line the layout put below the footnote line back into the body.
- One function gives a line's effective kind. Assembly, the page text, review grouping and the dashboard all use it.
- Review's line menu offers the five roles, and the same role can be applied to a selected range of lines.
- The manuscript's paragraph menu offers «شعر» and «حاشية للعلامة (n)».
- The book page (edited text) offers «تحويل إلى حاشية للعلامة (n)», which finds the call and moves the paragraph into a footnote there.

**D91 — Verse.**
- **Detection** in assembly: runs of narrow lines that are staggered (R/L) or side by side (a central gap), or have the `verse` role, confirmed by rhyme (≥ 2 bayts, ≥ ⅔ share the rhyme letter). v0.3's per-بيت markup counts as supporting evidence. Found automatically (chip «شعر · N أبيات», «ليس شعرًا»), or by role.
- **Document:** one بيت = one `paragraph` with `style: "verse"` whose hard break separates صدر and عجز; a run of such paragraphs is a poem. Verse never joins across a page break.
- **Print:** flex halves, each justified. Word: a borderless two-cell table row per بيت (calibration pass, D62). EPUB: flex, with centred lines as the fallback.
- **Book page tools:** ⇧Enter splits the hemistichs; «ضمّ الفقرة التالية شطرًا ثانيًا» and «فصل الشطرين».

---

## 3. Detailed design

### 3.1 Flag policy v2 (`ocr/flags.py`, new, pure; used by `ocr/alignment.py:build_lines`)

**Readings.** `second_readings(primary, secondary) -> list[str | None]` gives v0.2's reading of each v0.3 token:
- punctuation is split off both sides, words compared leniently, marks only against marks;
- the pieces are re-glued in the primary token's shape;
- this replaces the whitespace alignment at `alignment.py:748-757`.

`alt` stays "the second model's reading when it differs leniently", so the popover and `editor/uncertain.py` still work.

**`classify(p, s, tess, tconf, two_readers) -> list[why]`** (empty list = sure), first match wins:

| Case | Result |
|---|---|
| foreign letters, mixed scripts, or a symbol outside the allow-list (`° % ‰ ٪ + = × ÷ ± § • * $ £ ( ) [ ] « » " ' - – — / ـ …` and Arabic punctuation) | `script` |
| punctuation only | sure |
| number, Western, v0.3 = v0.2 = Tesseract (digits) | sure |
| number, otherwise | `number` (Kraken's reading later, D50–D51) |
| lone letter that may be a digit (D51) | as today |
| one-reader region | `single` if Tesseract's word is Arabic, conf ≥ 85 and its dotless skeleton differs, else sure |
| v0.2 has no counterpart | sure if Tesseract read the same word (box, `tess` null); else `alone` |
| v0.3 ≠ v0.2 (lenient) | `disagree` |

Rules around the table:
- `conf = "low" if why else "high"`; the token stores `why` only when it is non-empty.
- **Vote** (`chooser.choose_word`, D26 hook; `WORD_CHOOSER` defaults to `"vote"`): when `lenient(tess) == lenient(alt) != lenient(t)`, set `t = alt`, `orig = old t`, `pick = "vote"`, and leave `res` null.
  - `apply_chooser` (`ocr/chooser.py:44-70`) no longer sets `res` for the vote.
  - `review.js accept()` (`:563-573`) confirms the option marked current, not `primary`.
- **Tesseract confidence** is kept on the token as `tc`, the matched word's conf, taken from the run's words when the box is taken (`alignment.py:783-789`).

Token keys added (JSON; `normalize_token` keeps unknown keys, `review/services.py:94-102`): `why`, `sug {t, src, label}`, `ins` (group id), `pick`, `tc`.

### 3.2 Numbers and dates

- **Three-reader rule** (§3.1).
- **Year check:** `flags.year_check(line_tokens)` runs:
  - at finalize, for Western numbers;
  - at the end of the numbers pass, after `ocr/numbers.py` saves a line (`:764`), because Kraken rewrites the digits.
- **Parser** (`flags.number_words`): Arabic cardinals with old spellings (مايتين، ماية، ثلثماية، الف، الفين، عشرة …), joined by «و», allowing «م / هـ / ه» and brackets between the number and the words, and words on the next line.
  - Agreement → `res = "words"`, the token is resolved.
  - Mismatch → `sug = {t: "٢٤٣", src: "words", label: "من الحروف"}`, `why += ["year"]`.
- **Popover:** a suggestion is an extra reading row «٢٤٣ · من الحروف». The server adds choice `sug` to `CHOICES` (`review/services.py:40`).

### 3.3 Missing text: insertions, gaps, the line audit, split

**Secondary-only runs** (`flags.secondary_only_runs`), computed in `compose_page` (`ocr/services.py:1235`) before `build_lines`:
1. Runs of v0.2 pieces with no v0.3 counterpart, after the punctuation split.
2. Filters:
   - every word holds digits → the numbers pass handles it;
   - ≥ 60 % of the run's words appear within ±12 primary words → duplicate;
   - one word that is a prefix or suffix of an adjacent primary token → a split word («هير»);
   - anchor 0 of a body region with ≤ 4 words → a head.
3. Support: pre-align v0.3 with Tesseract's words in reading order; collect the Tesseract words left unmatched between the matches around the gap; support = the share of the run's words found among them (fuzzy ≥ 75).
   - **Support ≥ 0.7:** merge the run into the region's text. `build_lines(..., inserted={index: group})` marks those tokens `why: ["missing"]`, `ins: g`, low. Tesseract's boxes place them. On 23/1 this rebuilds line 11 as «تعالى بطرابلس … رحمه الله» and line 12 as «تعالى من كبار …».
   - **Otherwise:** a `TextGap(kind="words")` after the token the run follows (line, index).

**Line audit** (`ocr/audit.py`, new). In `run_full_ocr`, after the region runs: compose in memory, then look for suspects:
- `uncovered`: band height ≥ 0.6 × median band, width ≥ 25 % of the measure, outside head and page-number regions, and no line box over its centre;
- `underfull`: line width ≥ 75 % of the measure and words ≤ 0.5 × the region's median density × width, with at least 3 fewer words;
- `unread`: a Tesseract line with ≥ 4 words of ≥ 2 letters at conf ≥ 60, under 35 % of them matched.

At most `MAX_BANDS = 4` per page, widest first.
- **Reading:** crop the band rows (`_crop_rows`, `ocr/services.py:536`), gray, 2× when the core band is under 12 px. Primary model, cap 80 tokens. Stored as an `OcrRun` with `params.scope = "line"`, `kind = "audit"`, so `rebuild_lines` can reuse it without a model.
- **Sanity:** not looped; ≥ 1 word of ≥ 2 letters; ≤ 2× the words the band's width holds.
- **Classification:**
  - ≥ 60 % of its words, in order, inside a neighbour line (±2) → `TextGap(kind="split", line, index)`, the band's box;
  - found elsewhere on the page → nothing;
  - else → `TextGap(kind="line", after_line, text, bbox=band)`; an `underfull` suspect gives `kind="words"` inside that line instead.

**Model** `ocr.TextGap`:
- page FK (CASCADE, `gaps`);
- line FK (SET_NULL; for `kind=line`, the line it follows, null = page top);
- `index` (int; −1 = before the first token) and `after_t` (the anchor's text, to re-anchor);
- `kind` ∈ {words, line, split}, `text`, `bbox`;
- `source` ∈ {secondary, audit}, `support` (float), `evidence` JSON;
- `status` ∈ {open, inserted, dismissed}, `decided_by`, `decided_at`, `created_at`.

It is replaced with the lines by `finalize_page`, and kept as it is when the page has review work (`ocr/services.py:1340-1342`).

**Review services** (`review/services.py`; each locks the page, records a revision and is undone by `undo_last`):
- `resolve_insertion(page, group, keep)`: keep → `res = "secondary"` on the group's tokens (on any line); drop → remove them. Action `edit` per line, sharing a batch id.
- `accept_gap(gap, text=None)`:
  - words → insert typed tokens after `index`, re-anchored by `after_t`;
  - line → `insert_line(…, bbox=gap.bbox)`;
  - split → `split_line`.

  Gap status becomes `inserted`; the revision's `after` holds `{"gap": id}` and undo reopens the gap.
- `dismiss_gap(gap)`: new action `gap` «نص مقترح»; undo reopens.
- `split_line(line, index, bbox=None)`: tokens `[index:]` move to a new line just below, keeping their boxes; new action `split` «فصل سطر». Undo merges back from the snapshots. Also offered in the word menu's «إجراءات أخرى» as «بدء سطر جديد من هذه الكلمة» (the merged-line flag had no fix).
- **Re-anchoring:** `edit_line`, `merge_tokens` and `delete_token` shift a line's open gaps with the same alignment as `retokenize` (`:570-587`).

**Counts.**
- `count_unresolved` (`ocr/services.py:1195`) counts one item per insertion group.
- `Page.n_unresolved` = open tokens + open gaps, through a helper `page_open_items`, used by `refresh_page_text` (`:223-230`), `finalize_page` and `approve_page` (`:925`).
- `Line.n_low` stays tokens only.

### 3.4 Single-reader regions

- **`select_text`** (`ocr/services.py:1029-1067`) returns a partial `alt_text` when the other model looped: its clean parsed text minus the last repeated unit, flagged `alt_partial`. Only v0.3 tokens up to the last aligned index count as two-reader.
- **Re-read** in `run_full_ocr`, after the loop over regions: for such a region, crop from the top of the first Tesseract line past the covered part to the region's bottom, and run the looped model on it (variant `gray_part`, stored as a run with `params.scope = "part"`). `_collect_region_texts` concatenates prefix and part as the second reading. At most one extra call per region.
- What remains single-read gets `why: ["single"]` flags (§3.1) and the page flag `single_reader`.

### 3.5 Honest page state

**`books.Page.reading`** (JSON, default `{}`), written by `finalize_page`:

```json
{"readers": 2, "agreement": 0.96, "partial": false,
 "regions": [{"kind": "body", "readers": 2, "source": "qari_v03", "reason": "ok"}],
 "open": {"words": 5, "groups": 1, "gaps": 2}, "audit": {"bands": 2, "read": 2},
 "footnotes": "rule" | "zone" | "none"}
```

**Where it shows:**
- **Review header** (`templates/review/review.html` bar): a pill «قراءتان · اتفاق 96٪», «قراءة واحدة» (warning) or «نص Tesseract وحده». The counter reads «لا علامات» when there are none (never «حُسمت 0 من 0»).
- **Single-read pages:** a banner at the top of the lines column.
- **Tab with nothing open:** «لا علامات في هذه الصفحة؛ اقرأ الأسطر مع الصورة ثم اعتمدها.» (replaces `review.js:397`).
- **Thumbnails** (filmstrip, dashboard tiles): a small half-disc for one reader. The count shows open items. «مُراجَعة» appears only after approval.
- **Attention flags** (`core/templatetags/nassakh.py:34-42`):
  - `single_reader` «قراءة واحدة»;
  - `missing_text` «نص قد يكون ناقصًا»;
  - `footnotes_by_text` «حواشٍ حُدّدت من النص».
- **Readiness** (`publishing/readiness.py:119`):
  - `missing_text` (warn): «نص قد يكون ناقصًا لم يُحسم في 4 صفحات» → review;
  - `stray_notes` (warn): §3.7;
  - `single_reader` (info): «قُرئت 13 صفحة بنموذج واحد».
- **Approve dialog:** «بقيت 3 علامات: كلمتان غير محسومتين وسطر قد يكون ناقصًا. اعتماد الصفحة رغم ذلك؟» with the note «لا يدخل الكتابَ نصٌّ مقترح لم يُحسم.»

### 3.6 «تصحيح في كل الكتاب» and remembered corrections (`review/corrections.py`, new)

**`find_occurrences(book, form, options)`:**
- scans the token JSON of non-excluded pages (a `tokens::text` prefilter, then Python);
- matches whole tokens by their core, edge punctuation aside, with the book page's `FindOptions` (tashkeel, alef, whole word: `editor/services.py:412-417`);
- returns per page `{number, status, approved, lines: [{line_id, v, index, word, before, after, bbox, res, conf, head}]}`, plus `total` and `pages`;
- `head` = a line assembly drops as a running head.

**`fix_everywhere(book, from, to, picks, remember, user)`:** one transaction per page.
- The token must still read `from`, else it is skipped and reported.
- `t = to` with its edge punctuation kept, `orig` kept, `res = "typed"`.
- One `LineRevision(action="fix", batch=uuid)` per line.
- Approved pages stay approved and their lines stay reviewed. An `assembled` page goes back to `reviewed` (D36).
- **Mirror** when `Manuscript.origin == "editor"`: in each block whose `sourceLineIds` hold a ticked line, replace whole-word `from` with `to` (`editor.document.replace_in_nodes`, restricted to those blocks).
  - A block that also holds an unticked occurrence is skipped and listed.
  - One edit snapshot, one relayout.
  - The page's revisions get `after.mirrored = true`, so D72 drift ignores them.
- `remember=True` → `Correction(book, from, to, source="batch")`.

**`undo_fix(book, batch)`** reverts each line of the batch whose newest revision is the batch's, and reports the others. The manuscript side comes back through its snapshot when nothing was written after it; otherwise the reverse replace, scoped the same way.

**Learning without a batch.** A 1:1 word correction (resolve typed, secondary or tess, or a 1:1 edit) is counted; the second time the same (from, to) happens on another page, a `Correction(source="repeat")` is created.
- Guards: `from` has ≥ 3 letters; no token of the book with `t == from` was confirmed as it is (`res` primary or typed).
- A background task `review.tasks.propagate_correction` marks matching tokens on unreviewed pages (page lock; skips lines saved since the task started, as D51 does): `why += ["corrected"]`, `sug = to`.
- `compose_page` applies active corrections to pages read later.

**Entry points:**
- **Review:** after a correction whose word has other occurrences, the response carries `elsewhere: {from, to, count, pages}`, shown as a chip under the undo toast. The word menu's «إجراءات أخرى» gets «تصحيح في كل الكتاب…» (⌥F in word mode).
- **Manuscript:** the block menu's word context.
- **Book page:** the «بحث» tab's result list gets «من المسح» with crops, and the same sheet.

### 3.7 Footnotes

**Detection.**
1. **Rule** (`processing/pipeline.py:340-378`): keep the layout draft's `rule_candidates`. At «بدء المعالجة», a page with no footnote decision and a candidate below the text gets it as its footnote start (`footnote_source = "candidate"`), unless «التخطيط» refused it («ليس خطًّا فاصلًا»). Book 25: 6 of 6.
2. **Text zone** (new `processing.services.footnote_zone_from_text`, run at the start of `run_full_ocr` before the models, beside `_vote_page_number` at `ocr/services.py:920`). Only for pages without a footnote region. Inputs: the body's Tesseract lines, the bands, rule candidates.
   - For k from the bottom line up, while in the lower half of the text block, score:
     - rule above: +3;
     - gap above ≥ max(1.5 × median gap, median gap + 0.8 × median height): +2 (≥ 1.25 × median gap: +1);
     - a marker at the line start, brackets in either direction (`^\s*[()[\]]\s*([0-9٠-٩]{1,2}|.{1,2})\s*[()[\]]`, for Tesseract's «)1(»): +1;
     - a matching call in the body above, not at a line start: +2;
     - zone lines ≤ 0.9 × the body's median height: +1;
     - a numbered list continuing from above: −3.
   - A zone of ≥ 3 lines needs a rule or smaller type. Accept at ≥ 4.
   - **Effect:** `guides_override["footnote_line"] = top/h`, `footnote_by = "text"`, `derive_regions(page)`, attention flag `footnotes_by_text`. Measured: 23/5 and 25/1, 3, 4, 5, 7 found; no false zone on 16 books.
3. **Assembly guard** (`page_notes`, `assembly/pipeline.py:1180-1218`): a marker-less first footnote line continues the previous page's note only if that note does not end with terminal punctuation **and** this page's body has no unmatched call. Otherwise it is a note of this page. `link_footnotes` gives the k-th marker-less note the k-th unmatched call on its page, with warning `note_marker_missing` «حاشية بلا علامة رُبطت بالعلامة (1)؛ تحقّق منها».

**Roles** (D90):
- `Line.Role` += `FOOTNOTE`, `VERSE`, `MAIN` (max_length 12 is enough; no data migration).
- `ocr.services.line_kind(role, region_kind)` → `"footnote"` if role = footnote, or role = body and region = footnote; else `"body"`.
- Used by the assembly loader (`assembly/services.py:221-230`), `refresh_page_text` (`review/services.py:226-228`), `line_item` (new field `kind`; `showGroup` and `is-footnote` read it, `review.js:798-809`) and `api:book_sheets`.
- `set_line_role` takes the effective choice and stores:
  - a footnote-region line: «حاشية» → `body`, «محتوى» → `main`, headings and verse allowed (they mean body);
  - a body-region line: «حاشية» → `footnote`.

  The refusal at `:786-787` goes.
- `set_roles(page, line_ids, role)` for a Shift-click range in review, one revision per line, sharing a batch id.

**Manuscript** (`static/src/js/manuscript.js:966-977`, `templates/assembly/manuscript.html:215-237`):
- the role list gains «شعر» and «حاشية»;
- when the block starts with a marker whose call exists on its page, the label reads «حاشية للعلامة (1)»;
- the call goes through `set_block_roles` as today;
- warning `stray_note`: a body paragraph that starts with a marker, lies at the end of its page's text, and whose page has an unmatched call with that number. Message «فقرة في الصفحة 3 تبدأ بعلامة حاشية «(1)» ولم تُربط»; actions «جعلها حاشية» and «انتقال». Readiness row `stray_notes`.

**Book page** (edited text), in the «الفقرة» panel and the style menu:
- «تحويل إلى حاشية للعلامة (n)» runs `editor.document.paragraph_to_footnote(nodes, block_id)`:
  - it finds the call «(n)», «[n]», a superscript, or a glued digit in the preceding blocks of the same source page, then the chapter;
  - replaces the call with a footnote node holding the paragraph's text without its marker;
  - removes the paragraph; chapter undo applies.
- With no call: «لم تُعثر على العلامة (1) في نص الصفحة 3؛ ضع المؤشّر موضعها ثم اختر «حاشية» (⌘⇧F).»

### 3.8 Verse

**Detection** (`assembly/pipeline.py`, new `verse_runs(lines, measure)`, before `split_paragraphs`: `:974`):
1. **Classify each body line** (ratios of the measure M):
   - `R`: right-aligned, R − x1 < 0.04M and width ≤ 0.62M;
   - `L`: left-aligned, x0 − L < 0.04M and width ≤ 0.62M;
   - `C`: centred and narrow;
   - `S`: wide with a central gap ≥ 0.04M between boxed tokens, centred within 0.35–0.65;
   - two lines on the same row (y overlap ≥ 50 %) → one `S`, the right one is the صدر.
2. **Pair into bayts within a run:** `S` → one بيت; `R` then `L` → one بيت; `C C` → a pair in order; role `verse` without geometry → pairs in order (a line without a box fits the alternation; 23/5 line 12); a leftover → a single hemistich.
3. **Accept:** ≥ 2 bayts and ≥ ⅔ share the rhyme letter, or the role is `verse`.
   - Rhyme letter = last Arabic letter of the عجز, with diacritics, a final alif/ى, calls and punctuation stripped, and ة/ه folded.
   - The v0.3 `<h2>`/`<p>` block boundaries (kept as `pb` on tokens from `raw_output`) break ties.
   - Auto runs carry `verse: "auto"` → chip «شعر · 6 أبيات» + «ليس شعرًا» (`dismissed_suggestions`).
4. **Blocks:** kind `verse`, id `p<first line id>`, content صدر + hard-break placeholder `BR` (``, a new `Rich` node) + عجز. `decide_seam` (`:1005-1030`) already splits non-paragraphs, so verse never joins. `find_candidates` sees verse blocks, so «والخبر (١)» links.

**Document:** `{"type":"paragraph","attrs":{"id":"p4970","style":"verse","verse":"auto",…},"content":[text,{"type":"hardBreak"},text]}`. The editor schema needs no change. Legacy verse paragraphs with several breaks give one row per pair.

**Rendering:**
- **HTML** (`publishing/html.py:167-187`): for style `verse`, the runs between `LineBreak`s become `<span class="nk-hemi">`; the `<br>` stays for the text layer and the fallback.
- **CSS** (`publishing/css.py:199`): `.nk-verse{display:flex;justify-content:space-between;flex-wrap:wrap;text-indent:0;margin:0}` `.nk-verse>br{display:none}` `.nk-hemi{flex:0 0 46%;text-align:justify;text-align-last:justify}` `.nk-hemi:only-child{flex-basis:100%;text-align:center;text-align-last:center}`, plus a run margin of 3 mm before the first and after the last بيت.
- **Layout export:** unchanged (spike: two line boxes on one row). The book page draws them justified (`geometry.js:192-199`).
- **In-place editor:** shows the paragraph as two centred lines until the relayout.
- **Word:** a run of verse paragraphs becomes one borderless table (`w:bidiVisual`), one row per بيت, cells 46 %/46 % with an 8 % gap as cell margins, cell paragraphs `NkVerse` justified (kashida per D55). Calibration in D62.
- **EPUB:** the same flex rule, under `@supports not (display:flex)` the current centred `<br/>`.

**Book page tools:**
- «ضمّ الفقرة التالية شطرًا ثانيًا» merges two verse paragraphs with a hard break;
- «فصل الشطرين» splits at the break;
- hint on a verse paragraph without a break: «بيت بشطر واحد: ⇧Enter بين الشطرين».

### 3.9 Existing books

- `manage.py rebuild_lines` (D39) already skips pages with review work. It now also recomputes reasons, votes, insertions, gaps and year checks from stored runs, then reschedules the numbers pass.
- `manage.py audit_lines <book>` queues the audit on the GPU queue for pages with suspects (idempotent; stored audit runs are reused).
- «كتابي»: 77 unreviewed pages are rebuilt; the few pages with review work keep their tokens.

---

## 4. Data model and API

**Migrations**
- `ocr.Line.role`: new choices `footnote`, `verse`, `main`.
- New `ocr.TextGap`.
- `books.Page.reading`: JSON.
- `review.LineRevision.batch`: UUID, null, indexed; actions += `gap` «نص مقترح», `split` «فصل سطر», `fix` «تصحيح في الكتاب».
- New `review.Correction`: book, from_form, to_form, source ∈ {batch, repeat}, count, batch, active, created_by, created_at; unique (book, from_form, to_form).
- `Preprocess.rule_candidates`: owned by the layout draft.
- `Book.assembly_settings.dismissed_suggestions` also holds dismissed verse runs.

**API** (review `api_urlpatterns`; POST needs a reviewer role; Arabic 400/409):
- `POST lines/<id>/split/ {index, t, v}` → `{lines, counts}`
- `POST pages/<id>/insertions/<group>/ {keep}` → `{lines, counts}`
- `POST gaps/<id>/accept/ {text?}` and `gaps/<id>/dismiss/` → `{line|lines, gap, counts}`
- `POST pages/<id>/roles/ {line_ids, role}` → `{lines}`
- `GET books/<id>/occurrences/?q=&match_tashkeel=&fold_alef=&whole_word=` → occurrences
- `POST books/<id>/fix-everywhere/ {from, to, picks:[{line_id, index, t}], remember}` → `{batch, applied, skipped, manuscript:{mirrored, skipped_blocks}, counts}`
- `POST books/<id>/fix-everywhere/<batch>/undo/` → `{reverted, skipped}`
- `GET books/<id>/corrections/` and `POST corrections/<id>/forget/`

The review payload's lines gain `kind` and `gaps: [...]`; the page gains `reading`; `counts` gains `gaps` and `groups`; the resolve responses gain `elsewhere`.

---

## 5. UI (RTL; review screen vocabulary: amber for doubt, dashed for unsure geometry, hairline chrome)

**Lines column.** Line numbers sit on the start side (right). A solid amber underline marks an uncertain word, a dotted one an inserted group, a thin bar (▏) possibly missing words, and a dashed row a possibly missing line.

```
┌──────────────────────────────── النص ──────────────────────────────────┐
│  نسخ   الرقم المطبوع: 67   [3 علامات]   [قراءتان · اتفاق 96٪]            │
│                                                                        │
│  قطب الاقطاب وكنز الطلاب الشيخ عبد الله الشعاب . ولد رحمه الله        9 │
│  تعالى ┈بطرابلس ونشأ بها واخذ عن جياعة من الفضاء وكان رحمه الله┈     10 │
│  تعالى من كبار الصوفية واحد الزهاد الورعين وعباد الله المتقين مثغلا    11 │
│  معروف يقصد للزيارة والدعوات فه مشهورة ▏رحمه الله تعالى              15 │
│  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄ سطر قد يكون ناقصًا ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄ │
│  وفي شهر جهاد الآخر من سنة ( ٢٤٥ ) خمس واربعين ومايتين           17 │
└────────────────────────────────────────────────────────────────────────┘
```

**Word popover with its reason.** Keys stay stable, the current reading is marked, and Enter confirms it.

```
┌─────────────────────────────────────────────┐
│ اختر القراءة الصحيحة                          │
│ النموذجان مختلفان؛ Tesseract يوافق الثاني       │
│ 1  يجي            Qari v0.3                  │
│ 2  يحيى   ● في النص   Qari v0.2               │
│ 3  يحبى           Tesseract                  │
│ ───────────────────────────────────────────  │
│ [تصحيح…                          ]  حفظ       │
│ إجراءات أخرى                              ‹  │
└─────────────────────────────────────────────┘
  year:   «السنة مكتوبة بعدها بالحروف: «ثلاث واربعين ومايتين» = ٢٤٣» · 1 ٢٤٣ من الحروف
  script: «في الكلمة حروف ليست عربية («и»، «л»)» · 2 ميلادية Qari v0.2
  corrected: «صُحّحت «السعودي» إلى «المسعودي» في الصفحة 8»
```

**Insertion group, gap and split popovers:**

```
┌──────────────────────────────────────────┐   ┌─────────────────────────────────────┐
│ كلمات أضافتها القراءة الثانية               │   │ قد تكون هنا كلمات ناقصة               │
│ قرأها Qari v0.2 وTesseract ولم يقرأها       │   │ يقرأ النموذج الثاني هنا: «الاجابة»     │
│ Qari v0.3.                                │   │ [ الاجابة                 ]  إدراج    │
│ ↵  إبقاء الكلمات (12)                      │   │ ⌫  تجاهل                              │
│ ⌫  حذفها                                  │   └─────────────────────────────────────┘
└──────────────────────────────────────────┘
split: «هذه الكلمات سطر مطبوع وحدها» · ↵ فصلها في سطر · ⌫ تجاهل
line:  «سطر مطبوع لم يقرأه أحد النماذج؛ قُرئ وحده:» + editable text · ↵ إدراج السطر · ⌫ تجاهل
```

**Single-read banner** (warning style):
«قرأ هذه الصفحةَ نموذجٌ واحد، فالعلامات فيها أقل من الحقيقة. قابِل كل سطر بالصورة.»

**Line menu:**

```
│ السطر 17                         │
│ تعديل السطر                   E  │
│ إدراج سطر بعده                    │
│ ──────────────                  │
│ نوع السطر                         │
│ ✓ محتوى                           │
│   عنوان رئيسي · عنوان فرعي        │
│   شعر                              │
│   حاشية                            │
│ ──────────────                  │
│ حذف السطر                         │
```

With a Shift-click range selected, the label reads «نوع الأسطر المحدَّدة (6)».

**Fix-everywhere sheet** (side sheet, keyboard: ↑↓, Space toggles, Enter applies):

```
┌──────────────────────────────────────────────────────────────┐
│ ✕                                        تصحيح في كل الكتاب  │
│      [ المسعودي              ]  ←  «السعودي»                  │
│ ☑ توحيد صور الألف   ☑ كلمة كاملة   ☐ مطابقة التشكيل            │
│ 160 موضعًا في 51 صفحة · المحدَّد 158    ☑ تذكّر هذا التصحيح    │
│ ─────────────────────────────────────────────────────────── │
│ ص 8                                                           │
│ ☑ [مسح] …وقد ذكر «السعودي» في كتابه مروج الذهب…               │
│ ص 11 · مُعتمَدة                                               │
│ ☐ [مسح] …«السعودي»… · أكّدها المراجع كما هي                    │
│ ─────────────────────────────────────────────────────────── │
│ [ إلغاء ]                           [ تصحيح 158 موضعًا ]       │
└──────────────────────────────────────────────────────────────┘
toast: «صُحّح 158 موضعًا في 51 صفحة · تراجع» (+ «وتُرك موضعان تغيّرا منذ فتح القائمة»)
edited book: «وفي نص الكتاب: 150 فقرة؛ 3 تحتاج تصحيحًا يدويًا» (links)
after one correction: «159 موضعًا آخر بالشكل نفسه في الكتاب · تصحيحها…»
```

**Manuscript paragraph menu, and verse in the manuscript and book:**

```
┌───────────────────────────┐        ثم ملك ( لبده ) بعدها وقال:
│ ص 3                        │   الى الهياج؛ ونار الحرب تستعر       لله دري! اذا اعدو على فرسي
│ عرض الأصل               O │   في حده الموت، لا يبقي! ولا يذر     وفي يدي صارم، افري الرؤوس به
│ نوع الفقرة                  │   ─ chip: «شعر · 6 أبيات» · «ليس شعرًا»
│ ✓ محتوى                    │
│   عنوان رئيسي · عنوان فرعي │
│   شعر                       │
│   حاشية للعلامة (1)         │
│ فتح في المراجعة             │
└───────────────────────────┘
```

**Keys** (D74 keymap):
- word mode: Enter keeps or inserts the focused group or gap; ⌫ drops or dismisses; ⌥F opens «تصحيح في كل الكتاب»;
- Tab visits words, groups and gaps in reading order;
- nothing new in page mode.

---

## 6. Tests

**`ocr/test_flags.py`** (new, pure):
- punctuation split: «القرآن.» against «القرآن .»; the «.» is not flagged;
- «،» against «.» not flagged;
- Western number sure only with three equal readings;
- Arabic-Indic stays low;
- «مилادية», «وال德拉ية», «※» and «^» flagged `script`; «°» and «%» not;
- vote: t=alt, orig kept, res null, `pick` = vote, key order unchanged;
- a word v0.2 lacks: sure with Tesseract agreeing, `alone` without;
- secondary-only runs and each filter (the real «هير ودوت», «لها», a head at anchor 0, a duplicate);
- support threshold → inserted group (the real 23/1 texts rebuild line 11 and line 12) or a gap;
- number words: «اثنتين واربعين ومايتين» = 242, «ثلثماية», words across a line break, «( 23 م ) ثلاث واربعين ومايتين» → `sug` ٢٤٣, «( ٢٦٨ ) ثمان وستين ومايتين» → `res` words;
- dotless skeleton helper;
- single-read flags only at conf ≥ 85 with a different skeleton.

**`ocr/tests.py`:**
- `select_text` partial alternative from a looped run;
- `compose_page` and `finalize_page` write `reading`, gaps and groups; counts with groups and gaps;
- `rebuild_lines` recomputes them without models (fake engines);
- the audit (fake engine): uncovered band → a line gap, merged neighbour → a split, a duplicate → nothing, looped or too long → dropped;
- the footnote text zone: synthetic 23/5 (gap, «)1(», smaller) → zone; the lists «١ –» and «(١) بلاد فارس» with body after → none;
- `run_full_ocr` sets the override and derives a footnote region before the models (fake engines).

**`processing/tests.py`:** a solid 7 px bar with a 20 px core is accepted with fill ≥ 0.6; a closed text row with fill 0.3 is refused (shared with the layout draft).

**`review/tests.py`:**
- `accept_gap` and `dismiss_gap` for all kinds, with undo reopening;
- insertion keep and drop across two lines, with undo;
- `split_line` and undo;
- roles: footnote, verse and main on both region kinds; the old refusal is gone; `set_roles` on a range;
- `find_occurrences` context and head marking;
- `fix_everywhere`: a stale token skipped; an approved page stays approved; assembled goes to reviewed; batch undo, including partial;
- mirror into an edited manuscript, skipping blocks with an unticked occurrence; `mirrored` flag set;
- learning: twice on two pages makes a Correction; a two-letter word or a form confirmed elsewhere does not;
- propagation skips lines saved since it started;
- `approve_page` refuses with open gaps (409 message counts them).

**`assembly/tests.py`:**
- `line_kind` from roles in the loader;
- footnote-role lines become notes;
- continuation guard and positional link (`note_marker_missing`);
- `stray_note` warning;
- verse: staggered with rhyme → bayts; side by side → split at the gap; role verse by order; a line with no box in a run; the glossary layout without rhyme → no verse; never joins at a seam; a call at the end of a بيت links; `BR` survives the typography (D37); dismissal.

**`editor/tests.py`:** `paragraph_to_footnote` (call found in the same page; no call → error; ids repaired); join and split of a بيت.

**`publishing` tests:**
- a verse block renders two hemistich spans;
- the WeasyPrint layout export gives two lines with one y and the right order and offsets;
- Word writes a verse table (python-docx reads it back);
- EPUB has the flex rule and the fallback.

**UI** (Node harness, `core/test_review_ui.py`, `test_manuscript_ui.py`, `test_layout_ui.py`):
- reason lines; Enter confirms the current reading;
- groups and gaps in the Tab order; ⌫ and Enter on them;
- the pill and banner; «لا علامات»;
- the fix-everywhere sheet (ticking, apply, undo, skipped report);
- the manuscript's «حاشية للعلامة (n)» label;
- the book page's convert-to-footnote and بيت tools.

---

## 7. Risks, and what must not break

**Risks**
- **Words in the text before review.** Supported insertions and vote readings enter the text before review. A forced approval keeps them.
  - Mitigation: only the majority of three readers decides; each is marked and counted; the approval dialog names them; an unsupported suggestion never enters the text.
- **Short crops.** A single-line crop can make Qari hallucinate or loop. Mitigation: cap 80 tokens, a width-based sanity check, suggestion only, stored runs.
- **Footnote zone false positives** on numbered lists and headings. Mitigation: strict scoring measured on 16 books; the role «محتوى» (main) reverts a line; the text zone is visible in «التخطيط» as `footnote_by: text`.
- **Verse rendering.** Grid is wrong in RTL (measured), so flex only. Word tables and EPUB readers vary: calibrate in D62, keep the fallback. The in-place editor shows two centred lines until the relayout.
- **Fix-everywhere on an edited book.** Replacement only in blocks sourced from ticked lines, where the exact form is still present; skipped blocks are listed; snapshot first; drift flag.
- **Cost:**
  - the audit: about 0.4 bands per page, each a short call;
  - the re-read: one call on about 14 % of body regions;
  - occurrence scans over 800 pages: a text prefilter.
- **Mixed semantics.** A book mixes old and new flags until `rebuild_lines` runs.

**Must not break**
- D17, D50 and D51 number handling: Kraken's reading, Qari's letter as the second reading.
- D26 hook, amended: the vote leaves words unresolved.
- D27 approval gate, which now includes gaps.
- D31 and D32 word and line menus.
- D36: assembled goes back to reviewed.
- D39: rescue and `rebuild_lines` skip reasons.
- D41 and D49: the edited manuscript is the truth; whole-book runs are still refused.
- D46 and D48: footnote numbering and call placement.
- D63: weak boxes.
- The `LineRevision` undo stack and snapshot fields.
- Tokens without the new keys.
- The TipTap schema, which is unchanged.
- The layout export `Aligner` offsets for verse.
- The existing tests: 326 ran green in `ocr/tests.py`, `ocr/test_boxes.py`, `review/tests.py` and `assembly/tests.py`.

---

## 8. Build order

1. **Flag policy, numbers, script, vote and reasons** (`ocr/flags.py`, alignment, chooser, popover copy, `rebuild_lines`). The biggest noise cut; nothing new in the UI beyond the reason line.
2. **Page reading state and wording**, and the single-read re-read.
3. **Missing text:** `TextGap`, insertions, audit, split, review UI.
4. **Footnotes:** rule candidates from the layout draft, text zone, roles, assembly guard, manuscript and book tools, readiness.
5. **Verse:** detection, document, rendering, spike follow-ups for Word and EPUB, tools.
6. **Fix-everywhere and remembered corrections** (needs the D70–D72 merge and drift pieces for the mirror flag).

Ownership follows the Phase 4–6 pattern. Backend: `ocr`, `review`, `assembly`, `processing`. UI: `review.js`/`html`, `manuscript`, the book page. Publishing: verse render, Word, EPUB.

---

## 9. Open questions for the owner

1. **Verse applied automatically?** Apply the verse structure when layout and rhyme agree (≥ 2 bayts, with a «ليس شعرًا» dismissal), or only suggest «شعر؟» as headings do? Recommendation: apply. Today's output is wrong either way.
2. **Majority reading in the text.** When Tesseract agrees with Qari v0.2 against v0.3, may v0.2's reading be the text before review? It stays marked and must still be confirmed. Measured: right 76 % against 22 %.
3. **Kraken plus v0.2.** Kraken numbers that Qari v0.2 reads digit for digit: keep them low (D50), or add a one-key «تأكيد الأرقام المتّفق عليها في الصفحة»? 17 of 18 were right on reviewed pages; too few to make them sure.
4. **Vowelled books.** Should they compare diacritics between the models, as a per-book option off by default? About +2 flags per 100 words; the reviewers kept v0.3's form in 98 % of the 310 cases.
5. **Punctuation.** Never flag punctuation? 89 of 95 punctuation flags were correct marks, and 0 of 48 comma/period disagreements were errors.
6. **Line audit.** Always on? It costs about 0.4 extra short model calls per page and found 23 likely dropped lines on 180 pages, including 19/52 and 26/2.
