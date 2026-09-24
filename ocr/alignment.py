"""Token alignment and line building (D12, D16, D17).

Qari returns the page as running text without reliable line breaks; Tesseract returns words with
boxes grouped into visual lines. `align_tokens` pairs two token sequences after lenient
normalisation; `build_lines` anchors the chosen engine's tokens to Tesseract's words to give each
token a line and a box, and attaches the other engine's reading as `alt` where it differs.
Words between two anchors are placed on the Tesseract words left unmatched between them
(`_place_run`); `merged_lines` finds lines that still look like two printed lines in one.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter

from rapidfuzz import fuzz

from core.arabic import is_digit_token, normalize

from .engines.tesseract import reading_order

Pair = tuple[int | None, int | None]

# Minimum rapidfuzz ratio (0-100) for two differently normalised tokens to count as the same word.
FUZZY_MIN_RATIO = 60

# Unmatched Tesseract words count as evidence for the primary words around them only when they hold
# some text: at least this many letters / digits at the start or end of an anchored line, and this many
# on a Tesseract line with no anchor at all (smaller lines are specks read as "A" or "|"), which must
# also be at least LINE_MIN_HEIGHT_RATIO of the median line height unless it was rescued.
EDGE_MIN_CHARS = 2
LINE_MIN_CHARS = 3
LINE_MIN_HEIGHT_RATIO = 0.5
# A line looks like two printed lines in one (`merged_lines`) when it holds at least MERGED_MIN_UNSEEN
# words (two letters or more: numbers and one-letter abbreviations are misread too often) that no
# Tesseract word accounts for, or at least one such word and more than MERGED_WORDS_RATIO x the words
# its width holds at the region's median density (and at least MERGED_MIN_EXTRA words more); the
# density needs at least MERGED_MIN_LINES lines of MERGED_DENSITY_MIN_WORDS words or more.
MERGED_MIN_UNSEEN = 3
MERGED_WORDS_RATIO = 1.6
MERGED_MIN_EXTRA = 4
MERGED_MIN_LINES = 4
MERGED_DENSITY_MIN_WORDS = 3
# ... and only on a line Tesseract read: at least this share of its words matched a primary word (a
# line Tesseract read as garbage has "unseen" words that are simply its own).
MERGED_MIN_READ = 0.5
# Punctuation that opens (and so belongs with the word after it).
_OPENERS = frozenset("«([{“‹")
# Punctuation that ends what comes before it: at the start of a run it stays with the anchor before.
_ENDERS = frozenset(".،؛:؟!,;?…)]")
# A short token in brackets: a footnote reference or a number («(٧٢)», which Tesseract reads "(VY)").
_BRACKETED = re.compile(r"^[(\[][^\s()\[\]]{1,4}[)\]][.,،؛:]*$")
_BIDI_CONTROLS = re.compile("[\u200e\u200f\u202a-\u202e\u2066-\u2069]")  # Tesseract adds RLMs


def _bracketed(token: str) -> bool:
    """True for a short token in brackets («(٧٢)», «(VY).»), bidi control marks aside."""
    return bool(_BRACKETED.match(_BIDI_CONTROLS.sub("", str(token or ""))))


def norm_token(token: str) -> str:
    """Lenient normalisation of one token (no diacritics, folded letters, Western digits, no punctuation)."""
    return normalize(token, "lenient")


def word_f1(a: str | list[str], b: str | list[str]) -> float:
    """Order-insensitive word overlap (multiset F1) of two texts or token lists, lenient normalisation."""
    ta = a.split() if isinstance(a, str) else list(a)
    tb = b.split() if isinstance(b, str) else list(b)
    ca = Counter(norm_token(t) for t in ta if norm_token(t))
    cb = Counter(norm_token(t) for t in tb if norm_token(t))
    common = sum((ca & cb).values())
    total = sum(ca.values()) + sum(cb.values())
    return (2 * common / total) if total else 0.0


def align_tokens(a: list[str], b: list[str], min_ratio: int = FUZZY_MIN_RATIO) -> list[Pair]:
    """Pair the tokens of `a` with those of `b`, in order; `(i, j)` pairs where either side may be None.

    Exact matches (after lenient normalisation) come from `difflib.SequenceMatcher`; inside replaced
    blocks tokens are paired by a monotone best-similarity alignment (rapidfuzz ratio ≥ `min_ratio`).
    A one-for-one replacement is always paired (it is the typical misread word).
    """
    na = [norm_token(t) for t in a]
    nb = [norm_token(t) for t in b]
    pairs: list[Pair] = []
    matcher = difflib.SequenceMatcher(None, na, nb, autojunk=False)
    for tag, i0, i1, j0, j1 in matcher.get_opcodes():
        if tag == "equal":
            pairs.extend(zip(range(i0, i1), range(j0, j1), strict=True))
        elif tag == "delete":
            pairs.extend((i, None) for i in range(i0, i1))
        elif tag == "insert":
            pairs.extend((None, j) for j in range(j0, j1))
        else:
            pairs.extend(_pair_block(na, nb, i0, i1, j0, j1, min_ratio))
    return pairs


def _pair_block(
    na: list[str], nb: list[str], i0: int, i1: int, j0: int, j1: int, min_ratio: int
) -> list[Pair]:
    """Monotone pairing of a replaced block by summed similarity (small DP; blocks are short)."""
    n, m = i1 - i0, j1 - j0
    if n == 1 and m == 1:
        return [(i0, j0)]
    sim = [[int(round(fuzz.ratio(na[i0 + x], nb[j0 + y]))) for y in range(m)] for x in range(n)]
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for x in range(1, n + 1):
        for y in range(1, m + 1):
            best = max(dp[x - 1][y], dp[x][y - 1])
            s = sim[x - 1][y - 1]
            if s >= min_ratio:
                best = max(best, dp[x - 1][y - 1] + s)
            dp[x][y] = best
    out: list[Pair] = []
    x, y = n, m
    while x > 0 or y > 0:
        if (
            x > 0
            and y > 0
            and sim[x - 1][y - 1] >= min_ratio
            and dp[x][y] == dp[x - 1][y - 1] + sim[x - 1][y - 1]
        ):
            out.append((i0 + x - 1, j0 + y - 1))
            x, y = x - 1, y - 1
        elif x > 0 and (y == 0 or dp[x][y] == dp[x - 1][y]):
            out.append((i0 + x - 1, None))
            x -= 1
        else:
            out.append((None, j0 + y - 1))
            y -= 1
    out.reverse()
    return out


def is_word(token: str) -> bool:
    """A token with at least two letters (numbers, abbreviations like «ه» and punctuation are not)."""
    return sum(1 for ch in str(token or "") if ch.isalpha()) >= 2


def _chars(texts: list[str]) -> int:
    """Number of letters and digits in `texts` (punctuation, marks and diacritics do not count)."""
    return sum(1 for text in texts for ch in str(text or "") if ch.isalnum())


def _height(bbox: list | None) -> int:
    return int(bbox[3]) - int(bbox[1]) if bbox else 0


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    return float(ordered[mid]) if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _evidence(
    words: list[tuple[int, dict]],
    lines: list[dict],
    start: int,
    stop: int,
    edges: tuple[int | None, int | None],
    median_h: float,
) -> list[tuple[int, list[int]]]:
    """The unmatched Tesseract words `words[start:stop]` with letters or digits, grouped by line.

    `edges` are the lines of the anchors around the run (None at the start / end of the region): their
    words are the unmatched tail / head of an anchored line; every other line has no anchor at all and
    only counts between two anchors, or when it was rescued (before the first or after the last anchor
    such a line is as likely a header or a speck as the home of the words). Groups with too little
    text are noise and left out; punctuation-only words never count (Tesseract splits and drops
    punctuation freely).
    """
    bounded = None not in edges
    grouped: dict[int, list[int]] = {}
    for j in range(start, stop):
        if _chars([str(words[j][1].get("text") or "")]):
            grouped.setdefault(words[j][0], []).append(j)
    slots: list[tuple[int, list[int]]] = []
    for k in sorted(grouped):
        texts = [str(words[j][1].get("text") or "") for j in grouped[k]]
        if k in edges:
            keep = _chars(texts) >= EDGE_MIN_CHARS
        else:
            line = lines[k]
            rescued = bool(line.get("rescued"))
            tall = rescued or not median_h or _height(line.get("bbox")) >= LINE_MIN_HEIGHT_RATIO * median_h
            keep = (bounded or rescued) and tall and _chars(texts) >= LINE_MIN_CHARS
        if keep:
            slots.append((k, grouped[k]))
    return slots


def _without_bracketed(words: list[tuple[int, dict]], slots: list[tuple[int, list[int]]]) -> list:
    """`slots` without the bracketed Tesseract words (a reference «(VY)» the primary text left out).

    Used for a run that holds no bracketed word itself: the tail «(VY).» of the line before is then
    no home for the word that starts the next line. Kept as they are when nothing else is left.
    """
    kept = [(k, [j for j in idx if not _bracketed(words[j][1].get("text"))]) for k, idx in slots]
    kept = [(k, idx) for k, idx in kept if idx]
    return kept if kept else slots


def _units(tokens: list[str]) -> list[list[int]]:
    """Group a run's tokens into words: a punctuation-only token rides with a neighbouring word.

    Openers («, ( ...) join the next word, anything else the previous one (the next one at the start
    of the run). Returns the token indices of each unit, `[]` when the run is only punctuation.
    """
    units: list[list[int]] = []
    pending: list[int] = []
    for x, tok in enumerate(tokens):
        if _chars([tok]):
            units.append([*pending, x])
            pending = []
        elif units and not pending and tok[:1] not in _OPENERS:
            units[-1].append(x)
        else:
            pending.append(x)
    if pending and units:
        units[-1].extend(pending)
    return units


def _proportional(n: int, counts: list[int]) -> list[int]:
    """Split `n` items over slots proportionally to `counts` (largest remainder; ties go to the first)."""
    total = sum(counts)
    quotas = [n * c / total for c in counts]
    alloc = [int(q) for q in quotas]
    order = sorted(range(len(counts)), key=lambda x: (-(quotas[x] - alloc[x]), x))
    for x in order[: n - sum(alloc)]:
        alloc[x] += 1
    return alloc


def _place_run(n: int, slots: list[tuple[int, list[int]]], edges: tuple[int | None, int | None]) -> list:
    """Line, Tesseract word (or None) and `seen` flag for each of the `n` words (`_units`) of a run.

    `slots` are the unmatched Tesseract words between the run's anchors (`_evidence`), `edges` the
    lines of those anchors. Without evidence the run stays on the previous anchored line (the next
    one at the start of a region). With at most as many words as the evidence, the run is split over
    the slots in proportion to their word counts. With more, each slot takes as many words as it has
    and the rest, which Tesseract never saw, go to the lines without an anchor (a rescued line) or
    else stay on the previous anchored line. A slot that gets exactly its word count takes the words'
    boxes one to one; any other gets no boxes (the line box already says where the words are).
    """
    before, after = edges
    home = before if before is not None else after
    m = sum(len(idx) for _, idx in slots)
    if m == 0:
        return [(home, None, False)] * n
    if n <= m:
        alloc = _proportional(n, [len(idx) for _, idx in slots])
    else:
        alloc = [len(idx) for _, idx in slots]
        inner = [x for x, (k, _) in enumerate(slots) if k not in edges]
        if inner:
            for x, extra in zip(inner, _proportional(n - m, [len(slots[x][1]) for x in inner]), strict=True):
                alloc[x] += extra
    excess = n - sum(alloc)
    out: list[tuple[int | None, int | None, bool]] = []
    placed_excess = False
    for (k, idx), a in zip(slots, alloc, strict=True):
        if not placed_excess and k != before:
            out.extend([(home, None, False)] * excess)
            placed_excess = True
        if a == len(idx):
            out.extend((k, j, True) for j in idx)
        else:
            out.extend((k, None, t < len(idx)) for t in range(a))
    if not placed_excess:
        out.extend([(home, None, False)] * excess)
    return out


def build_lines(primary_text: str, secondary_text: str | None, tesseract_lines: list[dict]) -> list[dict]:
    """Split the chosen text into visual lines with per-token confidence.

    Each primary token is anchored to the Tesseract word it aligns with (Tesseract's words taken in
    `reading_order`) and inherits that word's line and box. A run of unmatched tokens inside one
    line stays there; a run between the last anchor of a line and the first anchor of a later line
    (or before the first / after the last anchor) is placed on the Tesseract words left unmatched
    between them by `_place_run`: the garbage words that start the next line, a rescued line or any
    line with no anchored word take their share, and without such words the run stays on the
    previous anchored line (leading ones take the first anchored line). Without Tesseract lines the
    primary text's own line breaks are used. `alt` is the secondary engine's token when its
    normalised form differs; `conf` is `low` when an alt differs, when the secondary has no
    counterpart for the token, or when the token is a number (D17).

    Returns `[{order, bbox, text, tokens, n_low, n_anchored, n_unseen, tess_words, tess_matched,
    rescued, confidence}]` with tokens `{"t", "alt", "conf", "digit", "bbox", "tess"}` (`tess` =
    Tesseract's word when it differs). `n_unseen` counts the words (`is_word`) no Tesseract word
    accounts for; `tess_words` / `tess_matched` are the words of the Tesseract line and how many of
    them a primary word matched; `rescued` marks a line that only the line rescue found.
    """
    p_tokens = primary_text.split()
    if not p_tokens:
        return []
    n = len(p_tokens)

    alts: list[str | None] = [None] * n
    disagree: list[bool] = [False] * n
    if secondary_text and secondary_text.split():
        s_tokens = secondary_text.split()
        for i, j in align_tokens(p_tokens, s_tokens):
            if i is None:
                continue
            if j is None:
                disagree[i] = True
            elif norm_token(p_tokens[i]) != norm_token(s_tokens[j]):
                alts[i] = s_tokens[j]
                disagree[i] = True

    lines_in = list(tesseract_lines or [])
    line_of: list[int | None] = [None] * n
    bbox_of: list[list | None] = [None] * n
    tess_of: list[str | None] = [None] * n  # Tesseract's reading when it differs from the primary
    unseen: list[bool] = [False] * n
    word_of: list[int | None] = [None] * n
    read: dict[int | None, tuple[int, int]] = {k: (0, 0) for k in range(len(lines_in))}

    # Runs stored before `reading_order` existed may hold lines laid out left to right: repair them here.
    words = [(k, w) for k, line in enumerate(lines_in) for w in reading_order(list(line.get("words", [])))]

    def take(i: int, j: int) -> None:
        line_of[i] = words[j][0]
        bbox_of[i] = words[j][1].get("bbox")
        word = str(words[j][1].get("text") or "")
        if word and norm_token(word) != norm_token(p_tokens[i]):
            tess_of[i] = word

    if words:
        for i, j in align_tokens(p_tokens, [w["text"] for _, w in words]):
            if i is not None and j is not None:
                word_of[i] = j
                take(i, j)
    if any(j is not None for j in word_of):
        median_h = _median([_height(ln.get("bbox")) for ln in lines_in if len(ln.get("words", [])) >= 2])
        i = 0
        while i < n:
            if word_of[i] is not None:
                i += 1
                continue
            s = i
            while i < n and word_of[i] is None:
                i += 1
            ja = word_of[s - 1] if s > 0 else None
            jb = word_of[i] if i < n else None
            edges = (words[ja][0] if ja is not None else None, words[jb][0] if jb is not None else None)
            if edges[0] is not None and edges[0] == edges[1]:
                for r in range(s, i):  # inside one line: the line is certain, the box is not
                    line_of[r] = edges[0]
                continue
            start = ja + 1 if ja is not None else 0
            stop = jb if jb is not None else len(words)
            if edges[0] is not None:  # «التاريخ ⟨. تقع⟩ فزان»: the full stop ends the line before
                while s < i and p_tokens[s] and set(p_tokens[s]) <= _ENDERS:
                    line_of[s] = edges[0]
                    s += 1
            units = _units(p_tokens[s:i])
            if not units:  # punctuation only: it ends the previous line (or opens the first)
                for r in range(s, i):
                    line_of[r] = edges[0] if edges[0] is not None else edges[1]
                continue
            slots = _evidence(words, lines_in, start, stop, edges, median_h)
            if not any(_bracketed(p_tokens[s + x]) for unit in units for x in unit):
                slots = _without_bracketed(words, slots)
            for unit, (k, j, seen) in zip(units, _place_run(len(units), slots, edges), strict=True):
                for r in (s + x for x in unit):
                    line_of[r] = k
                    if _chars([p_tokens[r]]):
                        unseen[r] = not seen
                        if j is not None:
                            take(r, j)
        line_bboxes = {k: line.get("bbox") for k, line in enumerate(lines_in)}
        rescued = {k for k, line in enumerate(lines_in) if line.get("rescued")}
        matched = {j for j in word_of if j is not None}
        for j, (k, w) in enumerate(words):
            if is_word(str(w.get("text") or "")):
                read[k] = (read[k][0] + 1, read[k][1] + (j in matched))
    else:
        # No geometry, or nothing matched it: the text's own line breaks are the best we have.
        idx = 0
        for k, raw_line in enumerate(primary_text.split("\n")):
            for _ in raw_line.split():
                line_of[idx] = k
                idx += 1
        line_bboxes = {}
        rescued = set()

    grouped: dict[int, list[int]] = {}
    for i in range(n):
        grouped.setdefault(line_of[i], []).append(i)

    lines: list[dict] = []
    for order, key in enumerate(sorted(grouped)):
        indices = grouped[key]
        tokens = []
        for i in indices:
            tok = p_tokens[i]
            digit = is_digit_token(tok)
            low = digit or disagree[i]
            tokens.append(
                {
                    "t": tok,
                    "alt": alts[i],
                    "conf": "low" if low else "high",
                    "digit": digit,
                    "bbox": bbox_of[i],
                    "tess": tess_of[i],
                }
            )
        n_low = sum(1 for t in tokens if t["conf"] == "low")
        anchored = sum(1 for i in indices if bbox_of[i] is not None)
        bbox = line_bboxes.get(key)
        if bbox is None and anchored:
            boxes = [bbox_of[i] for i in indices if bbox_of[i] is not None]
            bbox = [
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            ]
        lines.append(
            {
                "order": order,
                "bbox": bbox,
                "text": " ".join(t["t"] for t in tokens),
                "tokens": tokens,
                "n_low": n_low,
                "n_anchored": anchored,
                "n_unseen": sum(1 for i in indices if unseen[i] and is_word(p_tokens[i])),
                "tess_words": read.get(key, (0, 0))[0],
                "tess_matched": read.get(key, (0, 0))[1],
                "rescued": key in rescued,
                "confidence": round(1.0 - n_low / len(tokens), 3) if tokens else 1.0,
            }
        )
    return lines


def merged_lines(lines: list[dict]) -> list[int]:
    """Indices of the lines of one region (`build_lines` output) that look like two printed lines in one.

    A line qualifies when it holds `MERGED_MIN_UNSEEN` or more words no Tesseract word accounts
    for (a printed line Tesseract missed and the rescue could not read), or when it holds such a
    word and more than `MERGED_WORDS_RATIO` times, and `MERGED_MIN_EXTRA` more than, the words its
    width holds at the median words-per-pixel of the region's lines (a width-based measure, so pages
    that mix long and short lines, headings or lists do not trip it; a long line every word of
    which Tesseract saw is long in print too). Either way Tesseract must have read the line itself
    (`MERGED_MIN_READ` of its words matched), else its unseen words are just its own, misread.
    """
    counts = [sum(1 for t in line.get("tokens") or [] if is_word(t.get("t"))) for line in lines]
    widths = [max(0, int(b[2]) - int(b[0])) if (b := line.get("bbox")) else 0 for line in lines]
    dense = [c / w for c, w in zip(counts, widths, strict=True) if w and c >= MERGED_DENSITY_MIN_WORDS]
    density = _median(dense) if len(dense) >= MERGED_MIN_LINES else 0.0
    out = []
    for x, line in enumerate(lines):
        expected = density * widths[x]
        long = bool(expected) and counts[x] > MERGED_WORDS_RATIO * expected
        long = long and counts[x] - expected >= MERGED_MIN_EXTRA
        unseen = int(line.get("n_unseen") or 0)
        was_read = int(line.get("tess_matched") or 0) >= MERGED_MIN_READ * max(
            1, int(line.get("tess_words") or 0)
        )
        if was_read and (unseen >= MERGED_MIN_UNSEEN or (long and unseen > 0)):
            out.append(x)
    return out
