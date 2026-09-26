# Phase 7 design notes (round trip, keyboard, navigation, names, narrow screens)
Read-only probes on the dev DB (2026-09-26), scripts in this folder:
- probe_drift.py / probe_reasons.py: drift classes. Book 26 drift [4-8] = approval only (no text change);
  book 19 drift = 70 pages, no review revision after run 32 (lines rewritten by a machine pass, the numbers pass).
  Claims: book 13 has 21 lines claimed twice (splits) and 13 fresh lines claimed by no block; book 25: 7 + 3 typed.
- merge_proto.py / merge_proto2.py + merge_lib.py: page-scoped three-way merge on the two incidents.
  Book 23 (run 37, snapshot 34, page 8): 1 item (take «فقيها، فاضلا، زاهدا» → «فقيهاً، فاضلاً، زاهداً»); the owner's
  3 book-page edits (inserted heading on p.1, edits on p.1 and p.5 with a footnote) untouched.
  Book 26 (run 41, snapshot 39, page 3): 1 item (take «بزعَ» → «بزغَ»), 6 paragraphs identical; the heading the owner made
  on p.1 and a p.1 paragraph edit untouched. Key detail: compare by word tokens, not node JSON (TipTap splits text
  nodes and adds null attrs).
- PDF page count from the first + last 2 MB (regex /Type /Pages … /Count N): 40 Arabic PDFs in ~/Documents/Books:
  26 exact, 14 not found (compressed object streams), 0 wrong.
- probe_stray.py / probe_stray2.py: marker-initial body paragraphs: 26 (24 in book 19, numbered lists); the page-end
  rule flags 3 (book 23 p.5 and book 25 p.1 true strays; book 19 p.58 «(22) …» false: add marker ≤ 15).
- Contrast: #8a8a93 = 3.42 / 3.20 / 3.03 on bg / subtle / muted; #6b6b73 = 5.28 / 4.93 / 4.68.
