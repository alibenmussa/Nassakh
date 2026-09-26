"""Token alignment and line building (D12, D16, D17).

Qari returns the page as running text without reliable line breaks; Tesseract returns words with
boxes grouped into visual lines. `align_tokens` pairs two token sequences after lenient
normalisation; `build_lines` anchors the chosen engine's tokens to Tesseract's words to give each
token a line and a box, and attaches the other engine's reading as `alt` where it differs.
Words between two anchors are placed on the Tesseract words left unmatched between them
(`_place_run`); `merged_lines` finds lines that still look like two printed lines in one.

Word boxes (audit of book 22, 2026-09: 250 of 1,462 boxes were wrong). Before the matching,
Tesseract's boxes are fitted to the page (`fit_lines`): each is kept inside the rows of its printed
line (the preprocess stage's line bands) and ends where the word on its right starts
(`clip_overruns`). Punctuation and numbers are never paired with a word, and with a Tesseract mark
or number only inside the gap between the words matched around them, where the gap's other tokens
still fit (`pair_marks`, `build_lines`: a date that ends a line keeps it). The words between two
anchors of one line take the unused Tesseract words there, by position, or pieces of their ink
(`split_at_ink`). Boxes that still look wrong are marked `bq: "weak"` (`weak_boxes`).
"""

from __future__ import annotations

import bisect
import difflib
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from rapidfuzz import fuzz

from core.arabic import is_digit_token, normalize, strip_tashkeel

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
_CLOSERS = frozenset(")]")  # a closing bracket after a line's last word ends that line
# A short token in brackets: a footnote reference or a number («(٧٢)», which Tesseract reads "(VY)").
_BRACKETED = re.compile(r"^[(\[][^\s()\[\]]{1,4}[)\]][.,،؛:]*$")
_BIDI_CONTROLS = re.compile("[\u200e\u200f\u202a-\u202e\u2066-\u2069]")  # Tesseract adds RLMs
_LATIN = re.compile(r"[A-Za-z]")
# «ج» (volume), «ص» (page), «ط» (edition) before their number, and any digit
_ABBREVIATION = re.compile(r"^[(\[«]?[جصط]$")
_DIGITS = re.compile(r"[0-9٠-٩۰-۹]")
# A lone letter (or two) that looks like a digit, bare or with brackets / punctuation: «آ», «(ه)», «اا».
_LETTER_DIGIT = re.compile(r"^[(\[«]?[اأإآهع]{1,2}ـ?[)\]»]?[.،:؛]?$")
_ARABIC = re.compile(r"[\u0621-\u064A\u066E-\u06D3\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFC]")
# Punctuation compared across the two readings: the marks Tesseract writes for one another folded
# (it reads the Arabic comma as «»» or «‘», brackets mirrored, any dash as any other).
_PUNCT_FOLD = str.maketrans(
    {
        **dict.fromkeys("،,‘’'«»\"“”٬", ","),
        **dict.fromkeys(".…·٫", "."),
        **dict.fromkeys("؛;", ";"),
        **dict.fromkeys("؟?", "?"),
        **dict.fromkeys("()", "("),
        **dict.fromkeys("[]", "["),
        **dict.fromkeys("-–—ـ‐−", "-"),
    }
)

# ---- word boxes (see the module docstring)
INK_LEVEL = 128  # gray values below this are ink (the preprocessed gray image has white paper)
OVERLAP_TOLERANCE = 2  # px: two boxes overlapping by at most this much are side by side
CLIP_MIN_KEEP = 0.25  # a clip that would keep less than this share of either box is not made
# A Tesseract line covers a band (`Preprocess.line_boxes`: the dense core of a printed line) when it
# holds at least BAND_COVER of the band's height. The rows of a printed line run halfway to the
# bands above and below, and at most BAND_REACH line pitches beyond its core. A word on a line that
# covers two bands belongs to the one it covers most, or to the band most of the line's words sit
# on when it covers both to within BAND_TIE.
BAND_COVER = 0.5
BAND_REACH = 0.5
BAND_TIE = 0.1
# A gap between two anchors of one line with a different number of unused Tesseract words than words
# is split along the ink of those words: at the widest blank columns (of the core band's rows), when
# the narrowest of them is at least SPLIT_MIN_GAP core heights wide and SPLIT_CLEAN_RATIO times the
# widest blank run left, and every piece is within SPLIT_WIDTH_TOLERANCE times its share by letters.
SPLIT_MIN_GAP = 0.2
SPLIT_CLEAN_RATIO = 1.5
SPLIT_WIDTH_TOLERANCE = 1.8
SPLIT_PUNCT_WEIGHT = 0.5  # a punctuation token riding with a word counts as this many letters
# A box is weak (`bq: "weak"`) when it overlaps a neighbouring word's box by more than WEAK_OVERLAP of
# the narrower one, is taller than WEAK_TALL line pitches, or is wider than WEAK_WIDE times the width
# its letters take at the line's median letter width (plus WEAK_WIDE_SLACK letters, for short
# words); the letter width needs WEAK_MIN_SAMPLES boxed words on the line.
WEAK = "weak"
WEAK_OVERLAP = 0.3
WEAK_TALL = 1.35
WEAK_WIDE = 1.6
WEAK_WIDE_SLACK = 1
WEAK_MIN_SAMPLES = 4
# A region's first / last Tesseract line of one word of at most this many characters, on a printed line
# of its own, is where the region's leading / trailing number may go (a page number, «١٨» read "\A").
LONE_MAX_CHARS = 5


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


def align_tokens(
    a: list[str],
    b: list[str],
    min_ratio: int = FUZZY_MIN_RATIO,
    key: Callable[[str], str] = norm_token,
) -> list[Pair]:
    """Pair the tokens of `a` with those of `b`, in order; `(i, j)` pairs where either side may be None.

    Exact matches (after lenient normalisation, or `key`) come from `difflib.SequenceMatcher`; inside
    replaced blocks tokens are paired by a monotone best-similarity alignment (rapidfuzz ratio ≥
    `min_ratio`). A one-for-one replacement is always paired (it is the typical misread word).
    """
    na = [key(t) for t in a]
    nb = [key(t) for t in b]
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


def _count(tokens: list[str]) -> int:
    """How many of `tokens` hold a letter or a digit (a tatweel alone, «1 ـ كتاب», is a dash)."""
    return sum(1 for token in tokens if any(ch.isalnum() and ch != "ـ" for ch in str(token or "")))


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


def _by_position(words: list[tuple[int, dict]], idx: list[int]) -> list[int]:
    """Words `idx` of one line right to left on the page when a Latin-looking one is among them.

    Tesseract lays a run of Latin words out left to right; when the primary words there are Arabic,
    those are Arabic words it misread ("ERE BAC" for «الفتح العربي») and their order is the page's.
    """
    if not any(_latin_looking(str(words[j][1].get("text") or "")) for j in idx):
        return idx
    if any(not words[j][1].get("bbox") for j in idx):
        return idx
    return sorted(idx, key=lambda j: -_centre(words[j][1]["bbox"]))


def _units(tokens: list[str], abbreviations: bool = True) -> list[list[int]]:
    """Group a run's tokens into words: a punctuation-only token rides with a neighbouring word.

    Openers («, ( ...) join the next word, anything else the previous one (the next one at the start
    of the run). With `abbreviations`, the abbreviation of a volume, page or edition and the number
    after it are one word as printed («(ج ١، ص ١٧٩).» for «(ج١، ص١٧٩).», which Tesseract reads as two
    words). Returns the token indices of each unit, `[]` when the run is only punctuation.
    """
    units: list[list[int]] = []
    pending: list[int] = []
    for x, tok in enumerate(tokens):
        if _chars([tok]):
            joined = abbreviations and units and not pending and _ABBREVIATION.match(tokens[units[-1][-1]])
            if joined and _DIGITS.search(tok):
                units[-1].append(x)
            else:
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


# ---------------------------------------------------------------- word boxes: fitting Tesseract's boxes


@dataclass(frozen=True)
class Band:
    """A printed line: its core rows `y0..y1` (a `Preprocess.line_boxes` band, columns `x0..x1`) and the
    rows `lo..hi` its words may use."""

    x0: int
    y0: int
    x1: int
    y1: int
    lo: int
    hi: int


def _centre(box: list) -> float:
    return (box[0] + box[2]) / 2


def _box(word: dict) -> list[int] | None:
    box = word.get("bbox")
    return [int(v) for v in box] if box and len(box) == 4 else None


def line_pitch(bands: list[dict]) -> float:
    """Median distance between the centres of consecutive printed-line bands (0 with fewer than two)."""
    centres = sorted((b["y0"] + b["y1"]) / 2 for b in bands)
    return _median([b - a for a, b in zip(centres, centres[1:], strict=False) if b > a])


def _valid_bands(bands: list[dict] | None) -> list[dict]:
    """The detected bands that have their four sides as numbers and some height (others are ignored)."""
    out = []
    for b in bands or []:
        try:
            sides = [float(b[k]) for k in ("x0", "y0", "x1", "y1")]
        except (KeyError, TypeError, ValueError):
            continue
        if all(v == v for v in sides) and sides[3] > sides[1]:  # v == v: no NaN
            out.append(b)
    return out


def page_bands(bands: list[dict] | None) -> list[Band]:
    """The printed lines of a page from its detected bands (`Preprocess.line_boxes`), top to bottom.

    A line's rows run halfway to the band above and below it, and at most `BAND_REACH` pitches beyond
    its own band (a band the detector missed is no neighbour). Empty without a pitch (fewer than two
    bands): nothing then says how far a line reaches.
    """
    ordered = sorted(_valid_bands(bands), key=lambda b: b["y0"])
    pitch = line_pitch(ordered)
    if not pitch:
        return []
    reach = BAND_REACH * pitch
    out = []
    for n, b in enumerate(ordered):
        lo, hi = b["y0"] - reach, b["y1"] + reach
        if n:
            lo = max(lo, (ordered[n - 1]["y1"] + b["y0"]) / 2)
        if n + 1 < len(ordered):
            hi = min(hi, (b["y1"] + ordered[n + 1]["y0"]) / 2)
        out.append(Band(int(b["x0"]), int(b["y0"]), int(b["x1"]), int(b["y1"]), int(lo), int(round(hi))))
    return out


def _core_rows(box: list, band: Band) -> int:
    """How many of the band's core rows `box` covers."""
    return max(0, min(box[3], band.y1) - max(box[1], band.y0))


def _core_share(box: list, band: Band) -> float:
    """Share of the band's core rows that `box` covers."""
    return _core_rows(box, band) / max(1, band.y1 - band.y0)


def covered_bands(box: list | None, bands: list[Band]) -> list[Band]:
    """The printed lines a Tesseract line box covers (`BAND_COVER` of a band's core, overlapping columns)."""
    if not box:
        return []
    return [b for b in bands if _core_share(box, b) >= BAND_COVER and min(box[2], b.x1) > max(box[0], b.x0)]


def _clamp(box: list, band: Band) -> list:
    """`box` inside the rows of `band` (unchanged when nothing of it would be left)."""
    y0, y1 = max(box[1], band.lo), min(box[3], band.hi)
    return [box[0], y0, box[2], y1] if y1 > y0 else box


def ink_columns(gray: np.ndarray, rows: tuple[int, int], x0: int, x1: int) -> np.ndarray:
    """Ink pixels (`INK_LEVEL`) per column of `gray` in rows `[rows[0], rows[1])`, columns `[x0, x1)`."""
    h, w = gray.shape[:2]
    y0, y1 = max(0, int(rows[0])), min(h, int(rows[1]))
    x0, x1 = max(0, int(x0)), min(w, int(x1))
    if y1 <= y0 or x1 <= x0:
        return np.zeros(max(0, x1 - x0), dtype=np.int64)
    return (gray[y0:y1, x0:x1] < INK_LEVEL).sum(axis=0)


def _blank_runs(profile: np.ndarray) -> list[tuple[int, int]]:
    """`[start, end)` of every run of blank (zero) columns in a profile."""
    padded = np.concatenate([[False], profile == 0, [False]])
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2], strict=True)]


def clip_overruns(words: list[dict]) -> list[dict]:
    """Copies of one line's words with each box that runs into its right-hand neighbour clipped.

    Tesseract's word boxes often reach too far right, over the word read before them (the page reads
    right to left), sometimes over several; their left edges are right. A word's right-hand neighbour
    is the word whose box starts next to the right of its own left edge; a box that runs into it ends
    where the neighbour starts. (Parting the two boxes at the widest blank of the gray image's
    columns in the overlap, or at its middle, cut into the neighbour instead: on book 22 that gave
    12 and 20 fewer right word boxes.) Only words with letters or digits take part; a clip that
    would leave less than `CLIP_MIN_KEEP` of a box (two boxes of one word) is not made.
    """
    boxes = [_box(w) for w in words]
    texty = [x for x, w in enumerate(words) if boxes[x] and _chars([str(w.get("text") or "")])]
    order = sorted(texty, key=lambda x: boxes[x][0])
    for pos, a in enumerate(order):
        right = next((y for y in order[pos + 1 :] if boxes[y][0] > boxes[a][0]), None)
        if right is None:
            continue
        box_a, start = boxes[a], boxes[right][0]
        if box_a[2] - start > OVERLAP_TOLERANCE and start - box_a[0] >= CLIP_MIN_KEEP * (box_a[2] - box_a[0]):
            box_a[2] = start
    return [{**w, "bbox": boxes[x]} if boxes[x] else w for x, w in enumerate(words)]


def fit_lines(lines: list[dict], bands: list[Band]) -> list[dict]:
    """Copies of a region's Tesseract lines with their boxes fitted to the printed lines (`page_bands`).

    Each word box is kept inside the rows of its printed line: the band its line covers
    (`covered_bands`). A line that covers two bands (flagged `two_bands`) is one printed line with
    its box grown over the next: its main band is the one its words cover most (rows of the band's
    core: a thin band is no line's main one), else the one no other line covers alone; a word goes
    to the other band only when it covers that one clearly more (`BAND_TIE`). The line box keeps to
    the main band, whose core rows the line keeps as `core`.
    Then boxes that run into their right-hand neighbour are clipped (`clip_overruns`). A line that
    covers no band keeps its rows (`core` None).
    """
    covers = [covered_bands(_box(line), bands) for line in lines]
    owned = Counter(band for found in covers if len(found) == 1 for band in found)
    out = []
    for line, covered in zip(lines, covers, strict=True):
        words = [dict(w) for w in line.get("words") or []]
        new = {**line, "words": words}
        box = _box(line)
        rows = None
        if covered:
            main = max(covered, key=lambda b: (sum(_core_rows(_box(w) or box, b) for w in words), -owned[b]))
            for word in words:
                wbox = _box(word)
                if not wbox:
                    continue
                band = main
                if len(covered) > 1:
                    best = max(covered, key=lambda b: _core_share(wbox, b))
                    if _core_share(wbox, best) - _core_share(wbox, main) > BAND_TIE:
                        band = best
                word["bbox"] = _clamp(wbox, band)
            new["bbox"] = _clamp(box, main)
            rows = (main.y0, main.y1)
            if len(covered) > 1:
                new["two_bands"] = True
        new["words"] = clip_overruns(words)
        new["core"] = list(rows) if rows else None
        out.append(new)
    return out


def split_at_ink(
    gray: np.ndarray, rows: tuple[int, int], x0: int, x1: int, weights: list[float]
) -> list[tuple[int, int]] | None:
    """Columns `(x0, x1)` of each of `len(weights)` words written right to left in columns `[x0, x1)`.

    The ink of the core rows `rows` is cut at its widest blank runs, only when the split is clean: the
    narrowest run used is at least `SPLIT_MIN_GAP` core heights wide and `SPLIT_CLEAN_RATIO` times the
    widest run left (Arabic words have blanks inside them too), and each piece's share of the width is
    within `SPLIT_WIDTH_TOLERANCE` of its word's share of the letters (`weights`). None otherwise.
    Pieces are trimmed to their ink; the first is the rightmost.
    """
    n = len(weights)
    profile = ink_columns(gray, rows, x0, x1)
    inked = np.flatnonzero(profile > 0)
    if not n or not inked.size:
        return None
    a, b = int(inked[0]), int(inked[-1]) + 1
    if n == 1:
        return [(x0 + a, x0 + b)]
    runs = [(s + a, e + a) for s, e in _blank_runs(profile[a:b])]
    if len(runs) < n - 1:
        return None
    ranked = sorted(runs, key=lambda r: -(r[1] - r[0]))
    cuts, rest = sorted(ranked[: n - 1]), ranked[n - 1 :]
    narrowest = min(e - s for s, e in cuts)
    if narrowest < max(1.0, SPLIT_MIN_GAP * (rows[1] - rows[0])):
        return None
    if rest and narrowest < SPLIT_CLEAN_RATIO * (rest[0][1] - rest[0][0]):
        return None
    edges = [a, *[x for cut in cuts for x in cut], b]
    pieces = [(edges[2 * k], edges[2 * k + 1]) for k in range(n)][::-1]  # right to left
    inked_width = sum(e - s for s, e in pieces)
    total = sum(weights)
    for (s, e), weight in zip(pieces, weights, strict=True):
        ratio = ((e - s) / inked_width) / (weight / total) if weight and inked_width else 0.0
        if not 1 / SPLIT_WIDTH_TOLERANCE <= ratio <= SPLIT_WIDTH_TOLERANCE:
            return None
    return [(x0 + s, x0 + e) for s, e in pieces]


# ---------------------------------------------------------------- punctuation and numbers


def is_special(token: str) -> str:
    """'punct' for punctuation, 'number' for a number («١٩٦٦», «(٣)»), '' for a word (bidi marks aside).

    Punctuation and numbers are never paired with a word (`build_lines`): every mark normalises to
    nothing (so any two marks compared equal) and Tesseract rarely reads Arabic-Indic digits as digits.
    """
    text = _BIDI_CONTROLS.sub("", str(token or ""))
    if not _chars([text]):
        return "punct"
    return "number" if is_digit_token(text) else ""


def _special_word(text: str) -> str:
    """`is_special` for a Tesseract word, where a short bracketed word is a number («(VY)» for «(٧٢)»)."""
    kind = is_special(text)
    return kind or ("number" if _bracketed(text) else "")


def _mark_kind(token: str, tesseract: bool = False) -> str:
    """`_special_word`, else 'letter' for a lone letter that may be a digit Qari wrote as a letter
    («آ» for «١», «ه» for «٥», D51) or, for a Tesseract word (`tesseract`), any word of one letter or
    digit ("e"); '' otherwise."""
    kind = _special_word(token)
    if kind:
        return kind
    if tesseract:
        return "letter" if _chars([token]) <= 1 else ""
    return "letter" if _LETTER_DIGIT.match(strip_tashkeel(_BIDI_CONTROLS.sub("", str(token or "")))) else ""


def pair_marks(
    a: list[str],
    b: list[str],
    allowed: Callable[[int, int], bool] | None = None,
) -> list[tuple[int, int]]:
    """Monotone one-to-one pairs `(x, y)` of Qari's marks and numbers `a` with Tesseract's `b`.

    The most pairs win, those whose readings agree (`_special_key`) counting most, then those of one
    kind (`_mark_kind`) and those sharing a mark («(٢)» and "(')"): Tesseract reads «(٢)» as "(')",
    «•» as "©", «١» as "e". Only pairs `allowed(x, y)` are made (all when None). Of two equal
    choices the earlier Tesseract word wins. A small DP: a gap between two matched words holds a few
    marks at most.
    """
    if not a or not b:
        return []

    def score(x: int, y: int) -> int | None:
        if allowed is not None and not allowed(x, y):
            return None
        if _special_key(a[x]) == _special_key(b[y]):
            return 5
        shared = bool(_marks(a[x]) & _marks(b[y]))
        return (3 if _mark_kind(a[x]) == _mark_kind(b[y], tesseract=True) else 2) + shared

    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for x in range(1, n + 1):
        for y in range(1, m + 1):
            s = score(x - 1, y - 1)
            dp[x][y] = max(dp[x - 1][y], dp[x][y - 1], 0 if s is None else dp[x - 1][y - 1] + s)
    out: list[tuple[int, int]] = []
    x, y = n, m
    while x and y:
        if dp[x][y] == dp[x][y - 1]:
            y -= 1
        elif dp[x][y] == dp[x - 1][y]:
            x -= 1
        else:
            out.append((x - 1, y - 1))
            x, y = x - 1, y - 1
    return out[::-1]


def _marks(token: str) -> set[str]:
    """The punctuation marks of a token (`_punct_key`: variants folded)."""
    return {ch for ch in _punct_key(token) if not ch.isalnum()}


def _structure_key(token: str) -> str:
    """`norm_token`, and one key for every mark (which that turns to nothing), apart from words:
    Tesseract reads a mark as another («.» as "-»), which must not throw the two readings out of step."""
    return norm_token(token) or "\x00"


def _mark_class_key(token: str) -> str:
    """`norm_token`, and for a mark its folded marks («،» equals "," but not ":»): a run of marks then
    cannot take the words' matches (`build_lines` aligns a gap again with it)."""
    return norm_token(token) or "\x00" + _punct_key(token)


def _special_key(token: str) -> str:
    """A punctuation token or number for comparison: a number's digits, a mark's `_punct_key`."""
    return norm_token(token) or _punct_key(token)


def _punct_key(token: str) -> str:
    """A punctuation token for comparison: its marks, with the variants Tesseract writes folded."""
    return _BIDI_CONTROLS.sub("", str(token or "")).translate(_PUNCT_FOLD)


def _latin_looking(text: str) -> bool:
    return bool(_LATIN.search(text)) and not _ARABIC.search(text)


# ---------------------------------------------------------------- weak boxes


def _width_units(token: str) -> float:
    """How wide a token prints, in letters: its letters and digits, and half a letter per mark."""
    text = _BIDI_CONTROLS.sub("", str(token or ""))
    marks = sum(1 for ch in text if not ch.isalnum() and not unicodedata.combining(ch))
    return _chars([text]) + SPLIT_PUNCT_WEIGHT * marks


def letter_width(tokens: list[dict]) -> float:
    """Median width of one letter (`_width_units`) over the boxed words and numbers of `tokens`, 0 below
    `WEAK_MIN_SAMPLES` of them."""
    widths = [
        (t["bbox"][2] - t["bbox"][0]) / _width_units(t["t"])
        for t in tokens
        if t.get("bbox") and _chars([str(t.get("t") or "")]) and is_special(t.get("t")) != "punct"
    ]
    return _median(widths) if len(widths) >= WEAK_MIN_SAMPLES else 0.0


def weak_boxes(tokens: list[dict], pitch: float = 0.0) -> list[bool]:
    """Which boxes of one line's tokens (reading order) look wrong, by their shape alone.

    A word's box is weak when it overlaps the box of the word before or after it (punctuation aside) by
    more than `WEAK_OVERLAP` of the narrower of the two (both are then), when it is taller than
    `WEAK_TALL` line pitches (`pitch`, 0: unknown), or when it is wider than `WEAK_WIDE` times the width
    of its letters (`_width_units`, plus `WEAK_WIDE_SLACK`) at the line's `letter_width` (a heading's
    type is larger; a line with too few boxed words has none).
    """
    weak = [False] * len(tokens)
    boxed = [x for x, t in enumerate(tokens) if t.get("bbox") and is_special(t.get("t")) != "punct"]
    per_letter = letter_width(tokens)
    for x in boxed:
        box = tokens[x]["bbox"]
        if pitch and box[3] - box[1] > WEAK_TALL * pitch:
            weak[x] = True
        units = _width_units(tokens[x].get("t"))
        if per_letter and box[2] - box[0] > WEAK_WIDE * (units + WEAK_WIDE_SLACK) * per_letter:
            weak[x] = True
    for x, y in zip(boxed, boxed[1:], strict=False):
        a, b = tokens[x]["bbox"], tokens[y]["bbox"]
        overlap = min(a[2], b[2]) - max(a[0], b[0])
        if overlap > WEAK_OVERLAP * max(1, min(a[2] - a[0], b[2] - b[0])):
            weak[x] = weak[y] = True
    return weak


# ---------------------------------------------------------------- lines


def build_lines(
    primary_text: str,
    secondary_text: str | None,
    tesseract_lines: list[dict],
    bands: list[dict] | None = None,
    gray: np.ndarray | None = None,
    *,
    single: bool = False,
    partial: bool = False,
    inserted: dict[int, int] | None = None,
) -> list[dict]:
    """Split the chosen text into visual lines with per-token confidence.

    Tesseract's boxes are first fitted to the printed lines (`fit_lines`; `bands` are the page's
    `Preprocess.line_boxes`, `gray` its gray image, both optional). Each primary word is anchored to
    the Tesseract word it aligns with (Tesseract's words taken in `reading_order`) and inherits that
    word's line and box. Punctuation and numbers (`_special_word`) take part in that alignment only to
    keep the two sequences in step (every mark equals every other, `_structure_key`; the words left
    between two matched ones that read the same as a Tesseract word there are matched again, a mark
    then equal only to its kind, `_mark_class_key`) and keep their pair only in the gap between the
    words matched around them (`between`: on those words' lines, between their boxes; the region's
    trailing number on a page-number line of its own, `lone`) where the gap's other tokens still fit
    (`room`); the ones left are paired with the Tesseract marks and numbers of that gap (`pair_marks`,
    under the same rules), and those still without a box take the unused Tesseract words between
    their neighbours' boxes when there are as many. A run of unmatched tokens inside one line stays
    there and takes the unused Tesseract words between its anchors: one to one by position when there
    are as many as its words, else pieces of their ink (`split_at_ink`). A run between the last anchor
    of a line and the first anchor of a later line (or before the first / after the last anchor) is
    placed on the Tesseract words left unmatched between them by `_place_run` (Latin-looking ones
    taken by position when the run is Arabic: they are Arabic words Tesseract misread and laid out
    left to right): the garbage words that start the next line, a rescued line or any line with no
    anchored word take their share, and without such words the run stays on the previous anchored
    line (leading ones take the first anchored line). An abbreviation and its number («(ج ١،») share a
    Tesseract word, whose box is the number's (`holders`). Without Tesseract lines the primary
    text's own line breaks are used. A box that still looks wrong (`weak_boxes`) is marked
    `bq: "weak"`.

    Confidence is flag policy v2 (D71, `ocr.flags.classify`): `alt` is Qari v0.2's reading of the token
    (`flags.second_readings`: punctuation split off, glued back in the token's shape) when it differs
    leniently, and `why` lists the reasons a token is doubtful (`conf` is `low` exactly then). Without
    `secondary_text` the region had one model reading when `single` (a `single` flag where Tesseract's
    confident Arabic word there has another skeleton), else no model reading to compare (the text
    layer, Tesseract's own text: only `script` and `number`). With `partial` the secondary text is the
    clean start of a looped run (D73): the tokens after the last one it reads are one-reader. `inserted`
    maps token indices to the group of words only the second model read that were merged into the text
    (D72): those tokens are `why: ["missing"]`, `ins: <group>`, low. `tc` is the confidence of the
    Tesseract word a token took.

    Returns `[{order, bbox, text, tokens, n_low, n_anchored, n_unseen, tess_words, tess_matched,
    rescued, two_bands, confidence, indices}]` (`indices`: the line's tokens' places in the text)
    with tokens `{"t", "alt", "conf", "digit", "bbox", "tess"}`, `tc` when a Tesseract word was
    taken, `why` / `ins` when set and `"bq": "weak"` on a weak box (`tess` = Tesseract's word when it
    differs). `n_anchored` counts the tokens anchored to a Tesseract word by the alignment or a run's evidence
    (not those boxed by their place in a line, as before D63: the page's `alignment_poor` flag); `n_unseen`
    the words (`is_word`) no Tesseract word accounts for; `tess_words` / `tess_matched` are the words of the
    Tesseract line and how many of them a primary word matched; `rescued` marks a line that only the line
    rescue found, `two_bands` one whose Tesseract line covers two printed lines.
    """
    p_tokens = primary_text.split()
    if not p_tokens:
        return []
    n = len(p_tokens)

    from . import flags  # the policy (D71) imports this module's alignment

    s_tokens = (secondary_text or "").split()
    readings = flags.second_readings(p_tokens, s_tokens) if s_tokens else [None] * n
    alts: list[str | None] = [
        s if s is not None and flags.lenient(s) != flags.lenient(p) else None
        for p, s in zip(p_tokens, readings, strict=True)
    ]
    # the rule before 7b, kept for a lone letter that may be a digit (D51): the whitespace alignment
    legacy: list[bool] = [False] * n
    if s_tokens:
        for i, j in align_tokens(p_tokens, s_tokens):
            if i is not None and (j is None or norm_token(p_tokens[i]) != norm_token(s_tokens[j])):
                legacy[i] = True
    if s_tokens:
        cutoff = flags.last_read(readings) if partial else n - 1
        two: list[bool | None] = [i <= cutoff for i in range(n)]
    else:
        two = [False if single else None] * n
    inserted = dict(inserted or {})

    source = list(tesseract_lines or [])
    lines_in = fit_lines(source, page_bands(bands))
    line_of: list[int | None] = [None] * n
    bbox_of: list[list | None] = [None] * n
    tess_of: list[str | None] = [None] * n  # Tesseract's reading when it differs from the primary
    unseen: list[bool] = [False] * n
    word_of: list[int | None] = [None] * n
    took: list[bool] = [False] * n  # the token took a Tesseract word (its box and reading)
    tconf_of: list[float | None] = [None] * n  # that word's confidence
    taken: set[int] = set()
    anchored_by: set[int] = set()  # tokens anchored to a Tesseract word (`take`)
    read: dict[int | None, tuple[int, int]] = {k: (0, 0) for k in range(len(lines_in))}

    # Runs stored before `reading_order` existed may hold lines laid out left to right: repair them here
    # (from the boxes as Tesseract gave them; the fitted words keep their places in the line).
    words: list[tuple[int, dict]] = []
    for k, (line, fitted) in enumerate(zip(source, lines_in, strict=True)):
        raw = list(line.get("words", []))
        place = {id(w): x for x, w in enumerate(raw)}
        words.extend((k, fitted["words"][place[id(w)]]) for w in reading_order(raw))
    by_line: dict[int, list[int]] = {}
    for j, (k, _) in enumerate(words):
        by_line.setdefault(k, []).append(j)

    def text_of(j: int) -> str:
        return str(words[j][1].get("text") or "")

    def take(i: int, j: int, anchor: bool = True) -> None:
        """Token `i` takes Tesseract word `j` (its line and box); `anchor`: by the alignment or as a
        run's evidence (`n_anchored`), not by its place among a line's unused words."""
        line_of[i] = words[j][0]
        bbox_of[i] = words[j][1].get("bbox")
        taken.add(j)
        took[i] = True
        conf = words[j][1].get("conf")
        tconf_of[i] = float(conf) if isinstance(conf, int | float) else None
        if anchor:
            anchored_by.add(i)
        word = text_of(j)
        tess_of[i] = word if word and norm_token(word) != norm_token(p_tokens[i]) else None

    # A region's first / last Tesseract line that holds one short word and is a printed line of its own
    # (no other line covers its band): a page number («١٨» read "\A"), which the region's leading /
    # trailing number or letter may take (`between`) when the word is about as long (Tesseract drops a
    # digit at most).
    cores = Counter(tuple(line["core"]) for line in lines_in if line.get("core"))

    def lone(k: int, i: int | None) -> bool:
        idx = by_line.get(k) or []
        if len(idx) != 1 or len(_BIDI_CONTROLS.sub("", text_of(idx[0])).strip()) > LONE_MAX_CHARS:
            return False
        core = lines_in[k].get("core")
        if core and cores[tuple(core)] > 1:
            return False
        return i is None or _chars([text_of(idx[0])]) >= _chars([p_tokens[i]]) - 1

    def between(j: int, ja: int | None, jb: int | None, i: int | None = None) -> bool:
        """Is word `j` in the gap between the anchors `ja` and `jb`: on one of their lines (or on a line
        between them when there are both), and after `ja` / before `jb` on the page where it shares
        their line? Before the first / after the last anchor only the region's first / last line may
        be another one, when it is a page number on a line of its own (`lone`): a number at the end of
        a region is no number further down. Token `i`, when given, must stay on `ja`'s line when it
        closes what comes before it («الهجري ⟨)⟩ ⏎ وبرنيق», `ends_line`)."""
        k = words[j][0]
        if k not in {words[a][0] for a in (ja, jb) if a is not None} and (ja is None or jb is None):
            edges = ({max(by_line)} if jb is None else set()) | ({min(by_line)} if ja is None else set())
            if k not in edges or not lone(k, i):
                return False
        if i is not None and ja is not None and k != words[ja][0] and ends_line(i):
            return False
        box = words[j][1].get("bbox")
        for anchor, after in ((ja, True), (jb, False)):
            if not box or anchor is None or words[anchor][0] != words[j][0]:
                continue
            abox = words[anchor][1].get("bbox")
            if not abox or _latin_looking(text_of(anchor)):
                continue
            if (_centre(box) >= _centre(abox)) if after else (_centre(box) <= _centre(abox)):
                return False
        return True

    def inside(k: int, right: int | None, left: int | None) -> list[int]:
        """The words of line `k` between the words `right` and `left` of that line (either None: the
        line's edge), by their boxes (Tesseract may list a Latin-looking word out of place), by its
        order without them."""
        ends = [words[y][1].get("bbox") for y in (right, left) if y is not None]
        if not all(ends) or any(not words[y][1].get("bbox") for y in by_line[k]):
            return [y for y in by_line[k] if (right is None or y > right) and (left is None or y < left)]
        hi = _centre(words[right][1]["bbox"]) if right is not None else float("inf")
        lo = _centre(words[left][1]["bbox"]) if left is not None else float("-inf")
        return [y for y in by_line[k] if lo < _centre(words[y][1]["bbox"]) < hi]

    def span(ja: int | None, jb: int | None) -> list[str]:
        """The unused Tesseract words between `ja` and `jb` on the page (None: the region's edge)."""
        la = words[ja][0] if ja is not None else min(by_line)
        lb = words[jb][0] if jb is not None else max(by_line)
        if la == lb:
            found = inside(la, ja, jb)
        else:
            found = inside(la, ja, None) + inside(lb, None, jb)
            found += [y for k in by_line if la < k < lb for y in by_line[k]]
        return [text_of(y) for y in found if y not in taken]

    def room(i: int, gap: tuple[int, int], j: int, ja: int | None, jb: int | None) -> bool:
        """May token `i` of the gap `gap` (its first token and its end) between the anchors `ja` and
        `jb` take word `j`? The gap's other tokens must fit on the page around `j` (a gap within one
        line always does). On `ja`'s line the words and numbers before `i` go between `ja` and `j`: one
        more than the Tesseract words there at most (Tesseract joins «ص ٢٢١١).» as one; «عظيمًا» (ج٢٢،
        ص ١٥٢١).» gives «(ج”,» to «(ج٢٢،», not to the number after it). On any later line those after
        `i` (past the one closing its bracket) go between `j` and `jb`: no more than the Tesseract
        words there («٢٠٢١م. ⏎ ° يقول»: «°» is the footnote mark «٢», no home for the date that ends
        the line before); on a line between the two anchors' lines, those before `i` between `ja` and
        `j` too (a mark there does not split a run of words away from that line). A number of two
        digits or more that reads the same needs no room (Qari may have set it apart from its words)."""
        k = words[j][0]
        la = words[ja][0] if ja is not None else None
        lb = words[jb][0] if jb is not None else None
        same = _special_key(p_tokens[i]) == _special_key(text_of(j))
        if la == lb or (same and sum(ch.isdigit() for ch in _special_key(p_tokens[i])) >= 2):
            return True
        if k == la:
            return _count(p_tokens[gap[0] : i]) <= _count(span(ja, j)) + 1
        if _count(p_tokens[closing(i, gap[1]) + 1 : gap[1]]) > _count(span(j, jb)):
            return False
        if la is None or lb is None or k == lb:
            return True
        return _count(p_tokens[gap[0] : i]) <= _count(span(ja, j))

    def closing(i: int, end: int) -> int:
        """The token closing the bracket token `i` opens («(2 ⟨1)⟩»: Qari splits what Tesseract reads as
        one), `i` itself when it opens none or none closes it before `end`."""
        text = p_tokens[i]
        if text.count("(") + text.count("[") <= text.count(")") + text.count("]"):
            return i
        for r in range(i + 1, end):
            if ")" in p_tokens[r] or "]" in p_tokens[r]:
                return r
        return i

    def ends_line(i: int) -> bool:
        """Is token `i` a closing bracket (with its punctuation) after only such marks since the anchored
        token before? (Other marks may start a line: «… وإنما», a footnote mark Qari read as «؟».)"""
        r = i
        while r >= 0 and word_of[r] is None:
            if not p_tokens[r] or not set(p_tokens[r]) <= _ENDERS or not set(p_tokens[r]) & _CLOSERS:
                return False
            r -= 1
        return r >= 0 and r < i

    def fill(s: int, e: int, ja: int, jb: int) -> None:
        """Boxes for the unmatched tokens `s..e` between the anchors `ja` and `jb` of one line."""
        units = _units(p_tokens[s:e])
        box_a, box_b = words[ja][1].get("bbox"), words[jb][1].get("bbox")
        if not units:
            return
        if not any(_LATIN.search(p_tokens[r]) for r in range(s, e)) and box_a and box_b:
            left, right = _centre(box_b), _centre(box_a)
            idx = [
                j
                for j in by_line[words[ja][0]]
                if j not in taken
                and words[j][1].get("bbox")
                and _chars([text_of(j)])
                and left < _centre(words[j][1]["bbox"]) < right
            ]
            idx.sort(key=lambda j: -_centre(words[j][1]["bbox"]))
        else:
            idx = [j for j in range(ja + 1, jb) if j not in taken and _chars([text_of(j)])]
        if not idx:
            return
        if len(idx) == len(units):
            for unit, j in zip(units, idx, strict=True):
                for r in holders(s, unit):
                    take(r, j, anchor=False)
            return
        boxes = [words[j][1]["bbox"] for j in idx if words[j][1].get("bbox")]
        if gray is None or not boxes:
            return
        y0, y1 = min(b[1] for b in boxes), max(b[3] for b in boxes)
        core = lines_in[words[ja][0]].get("core") or (y0, y1)
        weights = [
            max(1, _chars([p_tokens[s + x] for x in unit]))
            + SPLIT_PUNCT_WEIGHT * sum(1 for x in unit if not _chars([p_tokens[s + x]]))
            for unit in units
        ]
        pieces = split_at_ink(gray, core, min(b[0] for b in boxes), max(b[2] for b in boxes), weights) or []
        latin = [
            bool(held) and all(_latin_looking(p_tokens[r]) for r in held)
            for held in (holders(s, u) for u in units)
        ]
        x = 0
        while x < len(pieces):  # the pieces run right to left; a run of Latin words reads left to right
            y = x
            while y < len(pieces) and latin[y]:
                y += 1
            pieces[x:y] = pieces[x:y][::-1]
            x = y + 1 if y == x else y
        for unit, (x0, x1) in zip(units, pieces, strict=False):
            for r in holders(s, unit):
                bbox_of[r] = [x0, y0, x1, y1]

    def holders(s: int, unit: list[int]) -> list[int]:
        """The tokens of unit `unit` (of a run starting at token `s`) that take its box: those with
        letters or digits, but not an abbreviation before its number («(ج ١،»: the box is the number's,
        which the numbers pass reads)."""
        texty = [s + x for x in unit if _chars([p_tokens[s + x]])]
        return [r for r in texty if r == texty[-1] or not _ABBREVIATION.match(p_tokens[r])]

    def place_marks() -> None:
        """Punctuation and numbers still without a box take the unused Tesseract words in the gap
        between their neighbours' boxes on their line (one to one by position, when as many)."""
        on_line: dict[int, list[int]] = {}
        for i in range(n):
            if line_of[i] is not None:
                on_line.setdefault(line_of[i], []).append(i)
        for k, indices in on_line.items():
            x = 0
            while x < len(indices):
                if bbox_of[indices[x]] is not None or not kinds[indices[x]]:
                    x += 1
                    continue
                y = x
                while y < len(indices) and bbox_of[indices[y]] is None and kinds[indices[y]]:
                    y += 1
                run, x = indices[x:y], y
                if run[0] == indices[0] or y == len(indices) or k not in by_line:
                    continue  # at an edge of the line nothing bounds the gap
                before, after = indices[indices.index(run[0]) - 1], indices[y]
                if before != run[0] - 1 or after != run[-1] + 1:
                    continue  # another line's tokens come between
                right, left = bbox_of[before], bbox_of[after]
                if right is None or left is None:
                    continue
                if _centre(right) <= _centre(left):
                    continue
                idx = [
                    j
                    for j in by_line[k]
                    if j not in taken
                    and words[j][1].get("bbox")
                    and _centre(left) < _centre(words[j][1]["bbox"]) < _centre(right)
                ]
                if len(idx) == len(run):
                    for i, j in zip(
                        run, sorted(idx, key=lambda j: -_centre(words[j][1]["bbox"])), strict=True
                    ):
                        take(i, j, anchor=False)

    kinds = [_special_word(t) for t in p_tokens]
    if words:
        w_kinds = [_special_word(text_of(j)) for j in range(len(words))]
        # Every token is aligned with every Tesseract word, every mark equal to every other
        # (`_structure_key`), and a word never takes a mark or a number: marks keep the two sequences in
        # step. The words left unmatched between two matched ones are aligned again there with a mark
        # equal only to a mark of its kind (`_mark_class_key`), and those that read the same are matched:
        # a run of marks matched to marks must not take their words' matches. A mark or number keeps its
        # pair only in the gap between the words matched around it (else «،» takes a «:» two lines away).
        texts = [text_of(j) for j in range(len(words))]
        pairs = [
            (a, b)
            for a, b in align_tokens(p_tokens, texts, key=_structure_key)
            if a is not None and b is not None
        ]
        for a, b in pairs:
            if not kinds[a] and not w_kinds[b]:
                word_of[a] = b
                take(a, b)
        bounds = [(-1, -1), *((i, word_of[i]) for i in range(n) if word_of[i] is not None), (n, len(words))]
        for (a0, b0), (a1, b1) in zip(bounds, bounds[1:], strict=False):
            if a1 - a0 < 2 or b1 - b0 < 2:
                continue
            for x, y in align_tokens(p_tokens[a0 + 1 : a1], texts[b0 + 1 : b1], key=_mark_class_key):
                if x is None or y is None:
                    continue
                a, b = a0 + 1 + x, b0 + 1 + y
                if not kinds[a] and not w_kinds[b] and norm_token(p_tokens[a]) == norm_token(texts[b]):
                    word_of[a] = b
                    take(a, b)
        matched_words = [i for i in range(n) if word_of[i] is not None]
        for a, b in pairs:
            if kinds[a] or w_kinds[b]:
                x = bisect.bisect_left(matched_words, a)
                ja = word_of[matched_words[x - 1]] if x else None
                jb = word_of[matched_words[x]] if x < len(matched_words) else None
                end = matched_words[x] if x < len(matched_words) else n
                if (
                    (ja is None or ja < b)
                    and (jb is None or b < jb)
                    and between(b, ja, jb, a)
                    and room(a, (matched_words[x - 1] + 1 if x else 0, end), b, ja, jb)
                ):
                    word_of[a] = b
                    take(a, b)
        # punctuation and numbers (and lone letters that may be digits, «آ» for «١»): only among the marks,
        # numbers and one-letter words in the gap between the words matched around them, where the gap's
        # other tokens leave room (`room`); a number that reads the same may be anywhere between those
        # words (on a line of its own)
        anchors = [i for i in range(n) if word_of[i] is not None]
        for before, after in zip([None, *anchors], [*anchors, None], strict=True):
            ja = word_of[before] if before is not None else None
            jb = word_of[after] if after is not None else None
            gap = (before + 1 if before is not None else 0, after if after is not None else n)
            toks = [i for i in range(*gap) if _mark_kind(p_tokens[i]) and word_of[i] is None]
            numbers = {_special_key(p_tokens[i]) for i in toks if kinds[i] == "number"}
            found = range(ja + 1 if ja is not None else 0, jb if jb is not None else len(words))
            cands = [
                j
                for j in found
                if _mark_kind(text_of(j), tesseract=True)
                and (between(j, ja, jb) or (w_kinds[j] == "number" and _special_key(text_of(j)) in numbers))
            ]
            for a, b in pair_marks(
                [p_tokens[i] for i in toks],
                [text_of(j) for j in cands],
                allowed=lambda x, y, toks=toks, cands=cands, gap=gap, ja=ja, jb=jb: room(
                    toks[x], gap, cands[y], ja, jb
                ),
            ):
                i, j = toks[a], cands[b]
                same = _special_key(p_tokens[i]) == _special_key(text_of(j))
                if between(j, ja, jb, i) or (same and not ends_line(i)):
                    word_of[i] = j
                    take(i, j)
    if any(j is not None for j in word_of):
        median_h = _median([_height(ln.get("bbox")) for ln in source if len(ln.get("words", [])) >= 2])
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
                for r in range(s, i):  # inside one line: the line is certain, the box comes from the gap
                    line_of[r] = edges[0]
                fill(s, i, ja, jb)
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
            slots = _evidence(words, source, start, stop, edges, median_h)
            slots = [(k, kept) for k, idx in slots if (kept := [j for j in idx if j not in taken])]
            if not any(_bracketed(p_tokens[s + x]) for unit in units for x in unit):
                slots = _without_bracketed(words, slots)
            if not any(_LATIN.search(p_tokens[r]) for r in range(s, i)):
                slots = [(k, _by_position(words, idx)) for k, idx in slots]
            placed = _place_run(len(units), slots, edges)
            plain = _units(p_tokens[s:i], abbreviations=False)
            if plain != units:  # an abbreviation and its number go together; no other word moves line
                by_word = _place_run(len(plain), slots, edges)
                paired = {
                    x
                    for unit in units
                    if len([y for y in unit if _chars([p_tokens[s + y]])]) > 1
                    for x in unit
                }
                was = {x: k for unit, (k, _, _) in zip(plain, by_word, strict=True) for x in unit}
                now = {x: k for unit, (k, _, _) in zip(units, placed, strict=True) for x in unit}
                if any(was[x] != now[x] for x in was if x not in paired):
                    units, placed = plain, by_word
            for unit, (k, j, seen) in zip(units, placed, strict=True):
                for r in (s + x for x in unit):
                    line_of[r] = k
                    if _chars([p_tokens[r]]):
                        unseen[r] = not seen
                if j is not None:
                    for r in holders(s, unit):
                        take(r, j)
        place_marks()
        line_bboxes = {k: line.get("bbox") for k, line in enumerate(lines_in)}
        rescued = {k for k, line in enumerate(lines_in) if line.get("rescued")}
        two_bands = {k for k, line in enumerate(lines_in) if line.get("two_bands")}
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
        two_bands = set()

    grouped: dict[int, list[int]] = {}
    for i in range(n):
        grouped.setdefault(line_of[i], []).append(i)

    built: list[tuple[int, list[int], list[dict]]] = []
    for key in sorted(grouped):
        indices = grouped[key]
        tokens = []
        for i in indices:
            tok = p_tokens[i]
            digit = is_digit_token(tok)
            if i in inserted:
                why = [flags.MISSING]
            else:
                why = flags.classify(
                    tok, readings[i], tess_of[i], tconf_of[i], took[i], two[i], today=legacy[i]
                )
            token = {
                "t": tok,
                "alt": alts[i],
                "conf": "low" if why else "high",
                "digit": digit,
                "bbox": bbox_of[i],
                "tess": tess_of[i],
            }
            if took[i] and tconf_of[i] is not None:
                token["tc"] = tconf_of[i]
            if why:
                token["why"] = why
            if i in inserted:
                token["ins"] = inserted[i]
            tokens.append(token)
        built.append((key, indices, tokens))
    pitch = line_pitch(_valid_bands(bands)) or line_pitch(
        [{"y0": b[1], "y1": b[3]} for line in source if (b := _box(line))]
    )

    lines: list[dict] = []
    for order, (key, indices, tokens) in enumerate(built):
        for token, weak in zip(tokens, weak_boxes(tokens, pitch), strict=True):
            if token["bbox"] and weak:
                token["bq"] = WEAK
        n_low = sum(1 for t in tokens if t["conf"] == "low")
        anchored = sum(1 for i in indices if i in anchored_by)
        bbox = line_bboxes.get(key)
        boxes = [bbox_of[i] for i in indices if bbox_of[i] is not None]
        if bbox is None and boxes:
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
                "two_bands": key in two_bands,
                "confidence": round(1.0 - n_low / len(tokens), 3) if tokens else 1.0,
                "indices": list(indices),
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
