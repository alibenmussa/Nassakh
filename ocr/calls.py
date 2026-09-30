"""The call pass (D83): the small raised «(١)» of a footnote call, read from the ink by Kraken.

The models drop or misread the raised call mark of a footnote (book 29: 63 of 114 notes orphaned; the
linker repairs the misread ones, D82, and this pass finds the rest). For one finalised page of a book
printed with Arabic-Indic digits (`read_page_calls`, after the numbers pass, D50):

1. `wanted_numbers`: the numbers the page's footnote markers call for and its body does not yet hold
   in brackets (a first note line without a marker wants 1). Nothing wanted, nothing read.
2. Candidates on the body lines: a boxed token the models wrote for a call («(ا)», «(”)», «()»,
   «(١١)»: `token_candidates`, its word box the crop), and clusters of small ink components no word
   box covers, sitting high in the line (`ink_candidates`: `call_clusters`, the union box the crop),
   placed after the boxed word on their right (`place`; a gap holding an unplaced word gives none).
3. Kraken reads each crop at every scale of `SCALES` (`kraken_runner`); a reading of one or two digits,
   in brackets or not, whose value is wanted is accepted (`accept`), the best-formed reading of a crop
   and then of a number winning (`best_reading`, `reading_score`).
4. The token is written back («(N)», Kraken's, low, `call: true`; `apply_token`, `insert_call`), the
   line's text and suggestions follow (`ocr.numbers.token_moves`), one `OcrRun` records the page.

`docs/NOTE_CALLS_SPEC.md` has the design; pure functions first, the service does the I/O.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import numpy as np

from .numbers import KRAKEN_DIGITS, token_moves

log = logging.getLogger(__name__)

INK_LEVEL = 128  # gray values below this are ink (as `ocr.alignment.INK_LEVEL`)
SCALES = (1, 2)  # each crop is read as it is and upscaled by 2: the two readings differ on half the calls
# (book 29: 27 of 50 candidates), each right where the other is not; the best-formed reading wins
MIN_HEIGHT = 0.9  # a call-like component is this many median line heights tall (hamzas are shorter) ...
MAX_HEIGHT = 1.6  # ... at most this many ...
MAX_WIDTH = 1.0  # ... and at most this wide
JOIN_GAP = 0.8  # components closer than this (× the line height) form one cluster
MIN_PARTS, MAX_PARTS = 2, 4  # a cluster's components: «(», «١», «)»; a bracket may touch the digit
MIN_CLUSTER, MAX_CLUSTER = 1.0, 3.5  # a cluster's width in line heights («(١)» 2.3, «(١١)» 3.2)
INSIDE = 3  # px: a component is a word's only this far inside its box (clipped boxes split a call)
REACH = 5.0  # line heights searched left of the line box (an unboxed call at the line's end lies outside it)
CORE_SHARE = 0.5  # rows with at least this share of the densest row's ink are the line's core
MARK_WIDTH = 1.0  # ink narrower than this (× the line height) beside a cluster is a mark («،»), not a word
MIN_AREA = 12  # px: smaller components are noise
CALL_MAX = 99
# D87: a call's two brackets, sized by the body's line pitch (the core band's height does not travel between
# prints: book 29's calls are 1.3× it, book 31's 2.1×; in pitches both are 0.24–0.30). A bracket is tall and
# thin, raised above the line's core bottom; the pair faces each other at one height, a digit or two apart.
BRACKET_MIN_H, BRACKET_MAX_H = 0.14, 0.40  # a bracket's height, in line pitches
BRACKET_MAX_WH = 0.6  # its width / height
PAIR_TOP = 0.35  # the two tops within this share of the height
PAIR_HEIGHTS = 0.7  # the shorter at least this share of the taller
PAIR_GAP = (0.1, 3.2)  # the gap between the two, in bracket heights («()» … «(١٢)»)
JOIN_HEIGHTS = 0.6  # a part joins a group when at least this share of its tallest (a damma beside it: 0.58)
OLD_CLUSTERS = True  # D83's clusters as a second source: nothing more on books 29, 32–35 once `bracket_calls`
# runs, but 8 more calls linked on the vowelled book 31 (orphans 65 → 57 with `ESTIMATE`)
ESTIMATE = True  # place a call among unboxed words by the run's letters spread over its width
RAISED = 0.15  # a bracket's bottom at most this share of its height below the line's core bottom
CLOSERS = frozenset({"»", ")", "]", "﴾"})  # a call follows these …
PUNCTUATION = frozenset({".", "،", "؛", ":", "!", "؟", ",", ";", "..", "..."})  # … and precedes these

DIGITS = "0-9٠-٩۰-۹"
_MARKER = re.compile(rf"^\s*[\(\[]?\s*([{DIGITS}]{{1,3}}|ا(?=[\s\)\]]))\s*[\)\]]?\s*[-–—ـ.:،]?(?:\s+|$)")
_PRESENT = re.compile(rf"[\(\[]\s*([{DIGITS}]{{1,3}})\s*[\)\]]")
_TOKEN = re.compile(
    rf"^(?:[\(\[]\s*(?:[اأإآ”“\"'’‘]|[{DIGITS}]{{1,2}})?\s*[\)\]]|[\(\[]\s*[اأإآ”“\"'’‘]{{1,2}})[.،:؛]?$"
)  # «(ا)», «()», «(١١)»; with the closing bracket lost «(”» (book 31, D87)
_ACCEPT = re.compile(rf"^\s*([\(\)\[\]]?)\s*([{DIGITS}]{{1,2}})\s*([\(\)\[\]]?)\s*[.،:؛]?\s*$")
_DIGIT_RUN = re.compile(f"[{DIGITS}]")
_GLYPHS = re.compile(r"^[\(\)\[\]”“\"'’‘اأإآ\s.،]+$")
_QUOTES = "”“\"'’‘«»"
_STROKES = "\\(\\)\\[\\]" + _QUOTES
# What the models write for a raised call (D87): strokes around at most two digits or an alef («"٢"», «(”»,
# «"ا"», «‘‘»), or digits with closing quotes («٤»», «٢٢»»); never bare digits, never digits before a bracket
# («٢٣]» ends a verse number).
_READING = rf"(?:[{_STROKES}]+(?:[{DIGITS}]{{1,2}}|[اأإآ])?[{_STROKES}]*|[{DIGITS}]{{1,2}}[{_QUOTES}]+)"
_CALL_READING = re.compile(
    rf"^(?P<word>\S*?[\u0621-\u064a][\u064b-\u0652\u0670ـ]*)?(?P<closers>»*)(?P<reading>{_READING})"
    rf"(?P<punct>[.،؛:!؟,;]*)$"
)
_QUOTED = re.compile(rf"^[{_QUOTES}]*[{DIGITS}]{{1,2}}[{_QUOTES}]+$")  # «"٣"», «"٢٢»», «٤»»
_OPENERS = {")": "(", "]": "[", "»": "«", "”": "“"}
_TO_WESTERN = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def western(digits: str) -> str:
    """A digit run's value in Western digits (Kraken's Persian forms folded, `KRAKEN_DIGITS`)."""
    return str(int(digits.translate(KRAKEN_DIGITS).translate(_TO_WESTERN)))


def line_marker(text: str) -> str | None:
    """The number (Western) that starts a footnote line, «(١) …» «١ –» «ا …» (a lone alef is 1), or None."""
    match = _MARKER.match(text or "")
    if not match:
        return None
    marker = match.group(1)
    return "1" if marker == "ا" else western(marker)


def note_numbers(note_texts: list[str]) -> list[str | None]:
    """The numbers of a page's notes in order, as the linker numbers them (`assembly.pipeline.page_notes`:
    lookalike markers «(أ)» by sequence, a bare number inside a note of a bracketed page is text; D85);
    None for a note without a marker."""
    from assembly.pipeline import LineIn, PageIn, page_notes

    lines = [
        LineIn(id=k + 1, order=k, kind="footnote", role="body", text=text, box=None, uncertain=[])
        for k, text in enumerate(note_texts)
        if (text or "").strip()
    ]
    page = PageIn(id=0, number=0, printed="", status="ocr_done", reviewed=False, lines=lines)
    notes, _carry = page_notes(page, None)
    return [note.key if note.key is None or note.key.isdigit() else None for note in notes]


def wanted_numbers(body_texts: list[str], note_texts: list[str]) -> list[str]:
    """The call numbers the page's notes want and its body lacks: its notes' numbers (`note_numbers`; a
    first note without a marker wants 1) minus the bracketed numbers of `body_texts`."""
    markers = note_numbers(note_texts)
    wanted = [m for m in markers if m is not None]
    if markers and markers[0] is None and "1" not in wanted:
        wanted.insert(0, "1")
    present = {western(m.group(1)) for text in body_texts for m in _PRESENT.finditer(text or "")}
    seen: set[str] = set()
    out = []
    for number in wanted:
        if number not in present and number not in seen:
            seen.add(number)
            out.append(number)
    return out


# ====================================================================== candidates


@dataclass
class Candidate:
    """One crop to read: `line` (the Line), where its result goes (`index`: a misread token to replace;
    else `after`, the token index the new token follows, −1 at the line's start, and `replace`, the
    lookalike tokens of its gap it stands for), `bbox` in page pixels, `source` `token` or `ink`."""

    line: object
    bbox: list[int]
    source: str
    index: int | None = None
    after: int = -1
    replace: tuple[int, int] = (0, 0)
    reading: str = ""
    number: str = ""
    status: str = ""
    readings: dict = field(default_factory=dict)  # scale → Kraken's text
    score: int = 0  # how well-formed the accepted reading is (`best_reading`)
    estimated: bool = False  # placed among unboxed words by `estimate_place`
    read: str = ""  # the number its reading gave, before the page's numbering (`number_calls`)

    @property
    def id(self) -> str:
        where = self.index if self.index is not None else self.after
        return f"{self.line.pk}:{self.source}{where}:{self.bbox[0]}"

    def as_dict(self) -> dict:
        return {
            "line": getattr(self.line, "order", None),
            "bbox": self.bbox,
            "source": self.source,
            "reading": self.reading,
            "readings": {str(k): v for k, v in self.readings.items()},
            "number": self.number,
            "status": self.status,
            "estimated": self.estimated,
        }


def is_misread_token(token: dict, wanted: list[str], markers: set[str]) -> bool:
    """A boxed token to read again: a bracket holding a lookalike glyph, nothing, or one or two digits
    that are no note's number («(١١)»; a correct «(٢)» is left alone); not resolved, not Kraken's call."""
    if token.get("res") or token.get("call") or not token.get("bbox"):
        return False
    text = str(token.get("t") or "")
    if not _TOKEN.match(text):
        return False
    digits = re.search(f"[{DIGITS}]+", text)
    return digits is None or western(digits.group(0)) not in markers


def token_candidates(line, wanted: list[str], markers: set[str]) -> list[Candidate]:
    """The misread call tokens of a line, each its own crop."""
    out = []
    for i, token in enumerate(line.tokens or []):
        if is_misread_token(token, wanted, markers):
            out.append(Candidate(line, [int(v) for v in token["bbox"]], "token", index=i))
    return out


def components(gray: np.ndarray, bbox: list[int]) -> list[tuple[int, int, int, int]]:
    """The ink components inside `bbox` of the gray page: `(x0, y0, x1, y1)` boxes in page pixels."""
    import cv2

    x0, y0, x1, y1 = (int(v) for v in bbox)
    crop = gray[max(0, y0) : max(0, y1), max(0, x0) : max(0, x1)]
    if crop.size == 0:
        return []
    ink = (crop < INK_LEVEL).astype(np.uint8)
    n, _labels, stats, _centroids = cv2.connectedComponentsWithStats(ink, connectivity=8)
    out = []
    for k in range(1, n):
        cx, cy, cw, ch, area = (int(v) for v in stats[k])
        if area >= MIN_AREA:
            out.append((x0 + cx, y0 + cy, x0 + cx + cw, y0 + cy + ch))
    return out


def search_box(line_bbox: list[int], line_height: float) -> list[int]:
    """The line box widened to its left by `REACH` line heights: a call after the line's last boxed word
    lies outside the box the word boxes make."""
    x0, y0, x1, y1 = (int(v) for v in line_bbox)
    return [max(0, int(x0 - REACH * max(1.0, float(line_height)))), y0, x1, y1]


def core_band(gray: np.ndarray, line_bbox: list[int]) -> tuple[float, float]:
    """The top and bottom of the line's core rows: the longest run of rows holding at least `CORE_SHARE`
    of the densest row's ink (the band of the baseline strokes; the line box's centre when it holds none)."""
    x0, y0, x1, y1 = (int(v) for v in line_bbox)
    crop = gray[max(0, y0) : max(0, y1), max(0, x0) : max(0, x1)]
    if crop.size == 0:
        return (y0 + y1) / 2, (y0 + y1) / 2
    rows = (crop < INK_LEVEL).sum(axis=1)
    if not rows.max():
        return (y0 + y1) / 2, (y0 + y1) / 2
    dense = rows >= CORE_SHARE * rows.max()
    best, start = (0, 0), None
    for i, on in enumerate([*dense.tolist(), False]):
        if on and start is None:
            start = i
        elif not on and start is not None:
            if i - start > best[1] - best[0]:
                best = (start, i)
            start = None
    return float(y0 + best[0]), float(y0 + best[1])


def core_centre(gray: np.ndarray, line_bbox: list[int]) -> float:
    """The centre of the line's core rows (`core_band`)."""
    top, bottom = core_band(gray, line_bbox)
    return (top + bottom) / 2


def line_pitch(lines: list, fallback: float) -> float:
    """The median distance between the centres of consecutive body lines, top down (D87: what a call's
    size is measured by); `fallback` on a page of one line."""
    centres = sorted((float(ln.bbox[1]) + float(ln.bbox[3])) / 2 for ln in lines if ln.bbox)
    gaps = [b - a for a, b in zip(centres, centres[1:], strict=False) if b > a]
    return float(np.median(gaps)) if gaps else float(fallback)


def _covered(comp: tuple[int, int, int, int], boxes: list[list]) -> bool:
    """Inside a word box by more than `INSIDE` on both sides: a box clipped at its neighbour may end on
    the call's ink (book 29 p. 214: «(٢)» split between two boxes)."""
    cx0, _cy0, cx1, _cy1 = comp
    return any(int(b[0]) + INSIDE <= cx0 and cx1 <= int(b[2]) - INSIDE for b in boxes)


def call_clusters(
    comps: list[tuple[int, int, int, int]], boxes: list[list], centre: float, line_height: float
) -> list[list[int]]:
    """The union boxes of the clusters of call-like components (module docstring, 2), right to left.

    Call-like: not inside a word box (`boxes`: sure ones; a weak box may have swallowed the call,
    book 29 p. 22), `MIN_HEIGHT`–`MAX_HEIGHT` line heights
    tall, at most `MAX_WIDTH` wide, its bottom at or above the band's `centre`. Neighbours closer
    than `JOIN_GAP` cluster; a cluster of `MIN_PARTS`–`MAX_PARTS` components, `MIN_CLUSTER`–
    `MAX_CLUSTER` line heights wide, is a candidate.
    """
    lh = max(1.0, float(line_height))
    parts = []
    for comp in comps:
        x0, y0, x1, y1 = comp
        h, w = y1 - y0, x1 - x0
        if not MIN_HEIGHT * lh <= h <= MAX_HEIGHT * lh or w > MAX_WIDTH * lh:
            continue
        if y1 > centre or _covered(comp, boxes):
            continue
        parts.append(comp)
    parts.sort(key=lambda c: -c[2])  # right to left: reading order
    clusters: list[list[tuple[int, int, int, int]]] = []
    for comp in parts:
        if clusters and clusters[-1][-1][0] - comp[2] <= JOIN_GAP * lh:
            clusters[-1].append(comp)
        else:
            clusters.append([comp])
    out = []
    for cluster in clusters:
        box = [
            min(c[0] for c in cluster),
            min(c[1] for c in cluster),
            max(c[2] for c in cluster),
            max(c[3] for c in cluster),
        ]
        if MIN_PARTS <= len(cluster) <= MAX_PARTS and MIN_CLUSTER * lh <= box[2] - box[0] <= MAX_CLUSTER * lh:
            out.append(box)
    return out


def _call_part(comp: tuple[int, int, int, int], core_top: float, pitch: float) -> bool:
    """A component a bracketed call is made of (D87): `BRACKET_MIN_H`–`BRACKET_MAX_H` line pitches tall and
    raised, its bottom above the line's core top (a letter ends in the core; book 29: 7–15 px above it,
    book 31: 16–25 px)."""
    h = comp[3] - comp[1]
    return BRACKET_MIN_H * pitch <= h <= BRACKET_MAX_H * pitch and comp[3] <= core_top + RAISED * h


def _is_bracket(comp: tuple[int, int, int, int]) -> bool:
    return comp[2] - comp[0] <= BRACKET_MAX_WH * (comp[3] - comp[1])


def bracket_calls(
    comps: list[tuple[int, int, int, int]], boxes: list[list], core_top: float, pitch: float
) -> list[list[int]]:
    """The union boxes of a line's bracketed calls «(١)» from the ink (D87), right to left.

    Raised call-sized components (`_call_part`), inside a word's box or not (`boxes` is unused, kept for
    the callers), join while they are less than `JOIN_GAP` × their height apart at one height; a group of
    2–5 whose two outer parts are
    brackets (tall and thin, `_is_bracket`) of one height and top (`PAIR_HEIGHTS`, `PAIR_TOP`), `PAIR_GAP`
    heights apart inside, is a call. Sized by the line pitch, not by the core band (module constants).
    """
    if pitch <= 0:
        return []
    # a word's box does not hide them: Tesseract boxes «الشافعي(٢)» as one word when the models drop the call
    # (book 34); a raised pair of like brackets is no letter's ink
    parts = sorted((c for c in comps if _call_part(c, core_top, pitch)), key=lambda c: c[0])
    groups: list[list[tuple[int, int, int, int]]] = []
    for comp in parts:
        if groups:
            last = groups[-1][-1]
            tall = max(c[3] - c[1] for c in groups[-1])
            h = max(tall, comp[3] - comp[1])
            if (
                comp[0] - last[2] <= JOIN_GAP * h
                and abs(comp[1] - last[1]) <= PAIR_TOP * h * 2
                and min(tall, comp[3] - comp[1]) >= JOIN_HEIGHTS * h  # a vowel mark beside the call stays out
            ):
                groups[-1].append(comp)
                continue
        groups.append([comp])
    out = []
    for group in groups:
        while group and not _is_bracket(group[0]):  # a neighbour's stroke at an end is not the call's
            group = group[1:]
        while group and not _is_bracket(group[-1]):
            group = group[:-1]
        if not 2 <= len(group) <= 5:
            continue
        a, b = group[0], group[-1]
        ha, hb = a[3] - a[1], b[3] - b[1]
        h = max(ha, hb)
        if not (_is_bracket(a) and _is_bracket(b)) or min(ha, hb) < PAIR_HEIGHTS * h:
            continue
        if abs(a[1] - b[1]) > PAIR_TOP * h or not PAIR_GAP[0] * h <= b[0] - a[2] <= PAIR_GAP[1] * h:
            continue
        out.append([a[0], min(c[1] for c in group), b[2], max(c[3] for c in group)])
    return sorted(out, key=lambda box: -box[2])


def _overlap_x(a, b) -> float:
    """The horizontal overlap of two boxes, as a share of the narrower."""
    inter = min(float(a[2]), float(b[2])) - max(float(a[0]), float(b[0]))
    narrow = min(float(a[2]) - float(a[0]), float(b[2]) - float(b[0]))
    return max(0.0, inter) / narrow if narrow > 0 else 0.0


def hides_no_call(token: dict) -> bool:
    """A token whose box may cover a call's ink without being a word: the models' reading of the call
    («(”», «‘‘», «(ا)», «(١١)»), a bracket, a quote stroke or a mark (D87)."""
    text = str(token.get("t") or "").strip()
    return bool(text) and (is_reading_token(token) or text in CLOSERS or text in PUNCTUATION)


def is_glyph_token(token: dict) -> bool:
    """A token made of brackets, quote strokes or alefs only: the models' reading of a call."""
    return bool(_GLYPHS.match(str(token.get("t") or "")))


def call_reading(text: str) -> tuple[str, str, str, str] | None:
    """`(word, closers, reading, punctuation)` when `text` ends with what the models write for a call
    (`_READING`: «المخالفة"٢"،», «الخبيثَ»٤»», «به"٢٢».», «الأصول»"ا".», «المنع””؛»), glued to a word or
    alone (`word` ''). The word's own closers stay its: a closer whose opener is in the word («(يؤذيهما)»,
    «"الموطأ"») or a leading «»» («الخبيثَ»٤»»: the quote ends there, «٤»» is the call). None when only
    closers are left."""
    match = _CALL_READING.match(text or "")
    if not match:
        return None
    word, closers = match.group("word") or "", match.group("closers")
    reading, punct = match.group("reading"), match.group("punct")
    while reading:
        char = reading[0]
        if char in _OPENERS:
            opener = _OPENERS[char]
            own = word.count(opener) > word.count(char) + closers.count(char)
        elif char in "\"'":
            own = (word.count(char) + closers.count(char)) % 2 == 1  # it closes the quote the word opened
        else:
            own = False
        if not own:
            break
        closers, reading = closers + char, reading[1:]
    if not reading.strip(")]»"):
        return None
    return word, closers, reading, punct


def is_reading(text: str) -> bool:
    """What the models write for a call, standing alone (D87): brackets, quote strokes or alefs («(”»,
    «‘‘», «"ا"»), a bracketed number or lookalike («(١١)», «(ا)»), a number in quotes («"٣"», «٤»»). Not
    a mark or a closer alone."""
    text = (text or "").strip()
    if not text or text in PUNCTUATION or text in CLOSERS:
        return False
    return bool(_GLYPHS.match(text) or _TOKEN.match(text) or _QUOTED.match(text))


def split_reading(text: str) -> tuple[str, str, str] | None:
    """`(closers, reading, punctuation)` of a token that is a call's reading with the text's closers before
    it or its marks after it («"٣"،», «“”؛», «»٤»»), else None (`is_reading` for the reading)."""
    parts = call_reading(text)
    if not parts or parts[0]:
        return None
    _word, closers, reading, punct = parts
    return (closers, reading, punct) if is_reading(reading) else None


def is_reading_token(token: dict) -> bool:
    """A token that is the models' reading of a call, alone or with its closers and marks glued."""
    text = _text(token)
    return is_reading(text) or split_reading(text) is not None


def _centre_x(box) -> float:
    return (float(box[0]) + float(box[2])) / 2


def word_before(tokens: list[dict], bbox: list[int]) -> int:
    """The index of the last boxed token whose box centre lies right of the cluster's: the word before
    it in reading order (−1 at the line's start)."""
    centre = _centre_x(bbox)
    after = -1
    for i, token in enumerate(tokens):
        box = token.get("bbox")
        if box and _centre_x(box) > centre:
            after = i
    return after


def place(
    tokens: list[dict],
    bbox: list[int],
    others: list[tuple[int, int, int, int]] = (),
    line_height: float = 20.0,
) -> tuple[int, tuple[int, int]] | None:
    """Where a cluster at `bbox` goes: `(after, (start, end))`, the token it follows and the run of
    unboxed tokens it replaces.

    The word before it (reading order, right to left) is the last boxed token whose box centre lies
    right of the cluster's (−1 at the line's start). The unboxed tokens after that word: lookalike
    glyph tokens are replaced; real words are placed by the gap's other ink (`others`, the line's
    components that are not the cluster's; a side narrower than `MARK_WIDTH` is a mark, «(١) ،»): all
    of it right of the cluster, the call follows the run; all of it left, the call precedes it; words
    on both sides, the gap is not understood (None); no words on either side, the cluster is the run's
    own ink (a small year, book 29 p. 100), as it may be when the run holds a number (None).
    """
    after = word_before(tokens, bbox)
    start = after + 1
    end = start
    while end < len(tokens) and not tokens[end].get("bbox"):
        end += 1
    if (
        end > start
        and (call_reading(_text(tokens[start])) or ("",))[0]
        and all(is_gap_token(t) for t in tokens[start + 1 : end])
    ):
        return start, (start + 1, end)  # the call follows the unboxed word it was glued to (`insert_call`)
    if all(is_gap_token(t) for t in tokens[start:end]):
        return after, (start, end)  # the call stands in the gap, its marks around it (`split_run`, D87)
    right_edge = float(tokens[after]["bbox"][0]) if after >= 0 else float("inf")
    left_edge = float(tokens[end]["bbox"][2]) if end < len(tokens) else float("-inf")
    inside = [
        c
        for c in others
        if c[2] > left_edge and c[0] < right_edge and not (c[0] >= bbox[0] and c[2] <= bbox[2])
    ]
    words = MARK_WIDTH * max(1.0, float(line_height))
    on_right = sum(c[2] - c[0] for c in inside if _centre_x(c) > bbox[2]) >= words
    on_left = sum(c[2] - c[0] for c in inside if _centre_x(c) < bbox[0]) >= words
    if on_right and on_left:
        return None
    if not on_right and not on_left:
        return None  # the run's words have no ink but the cluster's: the cluster is them (a year's digits)
    if any(_DIGIT_RUN.search(str(t.get("t") or "")) for t in tokens[start:end]):
        return None  # a number in the run: the cluster may be its digits
    if on_right:  # the words come first, the call ends the gap: it replaces the run's trailing glyphs
        tail = end
        while tail > start and is_glyph_token(tokens[tail - 1]):
            tail -= 1
        return tail - 1, (tail, end)
    head = start  # the call opens the gap: it replaces the run's leading glyphs
    while head < end and is_glyph_token(tokens[head]):
        head += 1
    return after, (start, head)


def estimate_place(
    tokens: list[dict], bbox: list[int], others: list[tuple[int, int, int, int]]
) -> tuple[int, tuple[int, int]] | None:
    """Where a call goes among unboxed words (D87: a vowelled line keeps few word boxes, book 31): the run
    of unboxed tokens between the boxed word on its right and the one on its left (or the line's ink ends)
    has its letters spread evenly over that width; the call follows the word whose end falls nearest the
    call's right edge, in place of the lookalike tokens right after it. An estimate: the token is low
    anyway, and the place counts for the note's order more than for the word."""
    after = word_before(tokens, bbox)
    start = after + 1
    end = start
    while end < len(tokens) and not tokens[end].get("bbox"):
        end += 1
    run = tokens[start:end]
    if not run:
        return None
    readings = [start + k for k, t in enumerate(run) if is_reading_token(t)]
    if len(readings) == 1:  # the models' one reading of a call in the run («(١١)», «""»): it is this call
        return readings[0] - 1, (readings[0], readings[0] + 1)
    right = float(tokens[after]["bbox"][0]) if after >= 0 else max((float(c[2]) for c in others), default=0.0)
    left = (
        float(tokens[end]["bbox"][2])
        if end < len(tokens)
        else min((float(c[0]) for c in others), default=0.0)
    )
    weights = [len(re.sub(r"[^\u0621-\u064a0-9٠-٩]", "", _text(t))) + 1 for t in run]
    if right <= left or not sum(weights):
        return None
    target = (right - float(bbox[2])) / (right - left) * sum(weights)
    best, acc, best_diff = 0, 0.0, None
    for k, weight in enumerate(weights):
        acc += weight
        if best_diff is None or abs(acc - target) < best_diff:
            best, best_diff = k, abs(acc - target)
    i = start + best
    j = i + 1
    while j < end and is_glyph_token(tokens[j]) and _text(tokens[j]) not in PUNCTUATION:
        j += 1
    return i, (i + 1, j)


def ink_candidates(line, gray: np.ndarray, line_height: float, pitch: float = 0.0) -> list[Candidate]:
    """The call-shaped ink of a line, as candidates placed among its tokens: bracketed calls sized by the
    line `pitch` (`bracket_calls`, D87), then D83's clusters where none overlaps. The models' reading of
    the call, boxed on its ink («(”», «‘‘»), hides nothing and is replaced; a cluster whose word before is
    itself a misread call token («(١١)» boxed on the wrong ink, book 29 p. 33) replaces it too."""
    from .alignment import WEAK

    tokens = line.tokens or []
    boxes = [t["bbox"] for t in tokens if t.get("bbox") and t.get("bq") != WEAK and not hides_no_call(t)]
    top, bottom = core_band(gray, line.bbox)
    centre = (top + bottom) / 2
    comps = components(gray, search_box(line.bbox, line_height))
    found = bracket_calls(comps, boxes, top, pitch)
    for box in call_clusters(comps, boxes, centre, line_height) if OLD_CLUSTERS else []:
        if not any(_overlap_x(box, other) > 0.3 for other in found):
            found.append(box)
    found.sort(key=lambda box: -box[2])
    out = []
    for box in found:
        hit = next(
            (
                i
                for i, t in enumerate(tokens)
                if t.get("bbox")
                and not (t.get("res") or t.get("call"))
                and is_reading_token(t)
                and _overlap_x(t["bbox"], box) >= 0.4
            ),
            None,
        )
        if hit is not None:  # the models' reading of the call, boxed on its ink: it is replaced (D87)
            out.append(Candidate(line, box, "ink", index=hit))
            continue
        after = word_before(tokens, box)
        before = tokens[after] if after >= 0 else None
        if before is not None and is_reading_token(before):  # «(١١)», «"» boxed off the call's ink (book 34)
            if not (before.get("res") or before.get("call")):
                out.append(Candidate(line, box, "ink", index=after))
            continue
        placed = place(tokens, box, comps, line_height)
        estimated = False
        if placed is None and ESTIMATE:
            placed = estimate_place(tokens, box, comps)
            estimated = placed is not None
        if placed is None:
            out.append(Candidate(line, box, "ink", status="unplaced words in the gap"))
            continue
        after, replace = placed
        out.append(Candidate(line, box, "ink", after=after, replace=replace, estimated=estimated))
    return out


# ====================================================================== Kraken's reading


def accept(
    reading: str, wanted: list[str], taken: set[str], markers: set[str] = frozenset()
) -> tuple[str, str]:
    """`(number, status)` for Kraken's `reading`: the Western number when it is one or two digits,
    bracketed or not, of a wanted number not `taken` yet; else '' and why. A reading «n1» whose n is
    wanted and which is no note's number (`markers`) is n: the «(» stroke of this print reads as a one
    (D82's rule, the same on Kraken: «۱١)» for «(١)», book 29 p. 33)."""
    match = _ACCEPT.match(reading or "")
    if not match:
        return "", "not a call"
    number = western(match.group(2))
    if number not in wanted and len(number) == 2 and number[1] == "1" and number[0] in wanted:
        if number not in markers:
            number = number[0]
    if number not in wanted:
        return "", f"«{number}» not wanted"
    if number in taken:
        return "", f"«{number}» duplicate"
    return number, "accepted"


def reading_score(reading: str, number: str) -> int:
    """How well-formed an accepted reading is: one point, plus one per bracket, less one when its digits
    were not the number itself («١١» read for 1)."""
    match = _ACCEPT.match(reading or "")
    if not match:
        return 0
    score = 1 + bool(match.group(1)) + bool(match.group(3))
    return score - (western(match.group(2)) != number)


def best_reading(
    readings: dict[int, str], wanted: list[str], markers: set[str] = frozenset()
) -> tuple[str, str, str, int]:
    """`(reading, number, status, score)`: the best-formed acceptable reading among the scales'
    (`accept`, `reading_score`), else the last scale's reading with its reason and no number."""
    best: tuple[int, str, str] | None = None
    last = ("", "not a call")
    for scale in sorted(readings):
        text = readings[scale]
        number, status = accept(text, wanted, set(), markers)
        last = (text, status)
        if number:
            score = reading_score(text, number)
            if best is None or score > best[0]:
                best = (score, text, number)
    if best is None:
        return last[0], "", last[1], 0
    return best[1], best[2], "accepted", best[0]


def call_token(number: str, bbox: list[int] | None, qari: str, style: str = "arabic_indic") -> dict:
    """A call token from the ink: «(N)» in the book's digits (Arabic-Indic unless `style` is western),
    low (D17), a number, flagged `call` for review; what the models wrote stays under `qari`."""
    digits = number if style == "western" else number.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
    text = "(" + digits + ")"
    return {
        "t": text,
        "alt": qari or None,
        "tess": None,
        "conf": "low",
        "digit": True,
        "bbox": [int(v) for v in bbox] if bbox else None,
        "res": None,
        "src": "kraken",
        "call": True,
        "qari": {"t": qari, "alt": None, "tess": None},
    }


def apply_token(
    tokens: list[dict], index: int, number: str, bbox: list[int] | None = None, style: str = "arabic_indic"
) -> None:
    """The misread token `index` becomes the call «(N)» in its own box (or in `bbox`, the ink's)."""
    old = tokens[index]
    parts = split_reading(_text(old))
    closers, qari, punct = parts if parts else ("", str(old.get("t") or ""), "")
    new = call_token(number, bbox or old.get("bbox"), qari, style)
    if old.get("bq") and not bbox:
        new["bq"] = old["bq"]
    tokens[index : index + 1] = [
        *([{**old, "t": closers, "bbox": None}] if closers else []),
        new,
        *([{**old, "t": punct, "bbox": None}] if punct else []),
    ]


def _text(token: dict) -> str:
    return str(token.get("t") or "").strip()


def is_gap_token(token: dict) -> bool:
    """A token the call's gap may hold (D87): the models' reading of the call, a closer, punctuation, or
    such a reading glued to a mark («“”؛»)."""
    text = _text(token)
    return is_reading_token(token) or text in CLOSERS or text in PUNCTUATION


def split_run(run: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """The tokens of a call's gap as `(closers, glyphs, punctuation)` (D87): the closers the call follows
    («»», «]», a lone «)»: «فله الجنة »(١) .», «فيه )(٢) :»), the models' readings of the call it replaces
    (quote strokes, alefs, «(” », a «( )» pair), the punctuation it precedes («.», «،», «:»)."""
    closers, glyphs, punctuation = [], [], []
    for k, token in enumerate(run):
        text = _text(token)
        parts = split_reading(text)
        if parts and (parts[0] or parts[2]):  # «“”؛», «"٣"،», «»٤»»: the reading with its closers or marks
            if parts[0]:
                closers.append({**token, "t": parts[0], "bbox": None})
            glyphs.append({**token, "t": parts[1]})
            if parts[2]:
                punctuation.append({**token, "t": parts[2], "bbox": None})
            continue
        pair = (text == ")" and k and _text(run[k - 1]) == "(") or (
            text == "(" and k + 1 < len(run) and _text(run[k + 1]) == ")"
        )
        if text in PUNCTUATION:
            punctuation.append(token)
        elif (text in CLOSERS or text == ")") and not pair:
            closers.append(token)
        else:
            glyphs.append(token)
    return closers, glyphs, punctuation


def insert_call(
    tokens: list[dict],
    after: int,
    replace: tuple[int, int],
    number: str,
    bbox: list[int],
    style: str = "arabic_indic",
) -> None:
    """A new call token «(N)» after token `after`, in the gap `replace`: after its closers, in place of its
    lookalike tokens, before its punctuation (`split_run`)."""
    start, end = replace
    closers, glyphs, punctuation = split_run(tokens[start:end])
    qari = " ".join(str(t.get("t") or "") for t in glyphs)
    glued = call_reading(_text(tokens[after])) if 0 <= after < len(tokens) else None
    if glued and glued[0]:  # the word before carries the call's reading glued to it: the call takes it
        word, own, reading, mark = glued  # («المخالفة"٢"،» → «المخالفة (٢) ،»; «الخبيثَ»٤»» keeps its «»»)
        tokens[after] = {**tokens[after], "t": word + own}
        qari = (reading + " " + qari).strip()
        if mark:
            punctuation = [{**tokens[after], "t": mark, "bbox": None}, *punctuation]
    tokens[start:end] = [*closers, call_token(number, bbox, qari, style), *punctuation]


# ====================================================================== the pass (I/O)


@dataclass
class PageCalls:
    """What the pass did on a page."""

    wanted: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    accepted: int = 0
    applied: int = 0
    seconds: float = 0.0
    skipped: str = ""

    @property
    def rejected(self) -> int:
        return sum(1 for c in self.candidates if c.status != "accepted")

    def as_dict(self) -> dict:
        return {
            "wanted": list(self.wanted),
            "candidates": len(self.candidates),
            "read": sum(1 for c in self.candidates if c.reading),
            "accepted": self.accepted,
            "rejected": self.rejected,
            "applied": self.applied,
            "seconds": self.seconds,
            "skipped": self.skipped,
        }


def _position(line_order: int, bbox) -> tuple[int, float]:
    """A place in reading order: the line, then right to left."""
    return (int(line_order), -_centre_x(bbox))


def present_calls(body: list, markers: set[str]) -> list[tuple[tuple[int, float], int, list | None]]:
    """The calls already in the body's text, `(position, number, box)` in reading order: bracketed numbers
    of the page's notes. An unboxed one takes the place of the boxed word before it."""
    out = []
    for line in body:
        last = [float(line.bbox[2]) + 1, 0, float(line.bbox[2]) + 1, 0]  # the line's right edge
        for token in line.tokens or []:
            box = token.get("bbox")
            match = _PRESENT.fullmatch(_text(token))
            if match and western(match.group(1)) in markers:
                out.append((_position(line.order, box or last), int(western(match.group(1))), box))
            if box:
                last = box
    return sorted(out, key=lambda item: item[0])


def page_plan(
    page, lines: list, gray: np.ndarray, line_height: float
) -> tuple[list[str], set[str], list[Candidate], list]:
    """The wanted numbers of a page, its note numbers, its candidates in reading order and the calls
    already in its text (`present_calls`; `lines`: its unreviewed lines with a box, in order, each with
    its effective `kind`). An ink candidate on a present call's ink is that call, not a candidate."""
    from ocr.services import SKIPPED_KINDS, line_kind

    kinds = {ln.pk: (ln.region.kind if ln.region_id else None) for ln in lines}
    lines = [ln for ln in lines if kinds[ln.pk] not in SKIPPED_KINDS]  # no running header, no page number
    body = [ln for ln in lines if line_kind(ln.role, kinds[ln.pk]) == "body"]
    notes = [ln for ln in lines if ln not in body]
    wanted = wanted_numbers([ln.text for ln in body], [ln.text for ln in notes])
    if not wanted:
        return wanted, set(), [], []
    markers = {m for m in note_numbers([ln.text for ln in notes]) if m}
    anchors = present_calls(body, markers)
    pitch = line_pitch(body, 4.5 * float(line_height or 20.0))
    out: list[Candidate] = []
    for line in body:
        found = [
            c
            for c in ink_candidates(line, gray, line_height, pitch)
            if not any(
                box and _position(line.order, c.bbox)[0] == pos[0] and _overlap_x(box, c.bbox) >= 0.4
                for pos, _n, box in anchors
            )
        ]
        replaced = {c.index for c in found if c.index is not None}
        found += [c for c in token_candidates(line, wanted, markers) if c.index not in replaced]
        found.sort(key=lambda c: -c.bbox[2])
        out.extend(found)
    return wanted, markers, out, [(pos, number) for pos, number, _box in anchors]


def _evidence(cand: Candidate, number: int, markers: set[str]) -> int:
    """How well Kraken's readings of `cand` say it is call `number`: the best `reading_score` among the
    scales' readings accepted as that number, 0 when none is (a candidate built without readings counts
    the number it was given, `read`, at its `score`)."""
    key = str(number)
    if cand.readings:
        best = 0
        for text in cand.readings.values():
            got, _status = accept(text, [key], set(), markers)
            if got == key:
                best = max(best, reading_score(text, got))
        return best
    return max(cand.score, 1) if cand.read == key else 0


def _aligned(inside: list[Candidate], numbers: list[int], markers: set[str]) -> list[tuple[Candidate, int]]:
    """The readings that agree with the page's order: the pairs (candidate, number), both in reading
    order, with the most evidence (`_evidence`), found by dynamic programming. A reading that another
    one of the page contradicts («4» then «4», «3» before «2») is left out."""
    rows, cols = len(inside), len(numbers)
    best = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(1, rows + 1):
        for j in range(1, cols + 1):
            gain = _evidence(inside[i - 1], numbers[j - 1], markers)
            best[i][j] = max(best[i - 1][j], best[i][j - 1], best[i - 1][j - 1] + gain if gain else 0)
    pairs = []
    i, j = rows, cols
    while i and j:
        gain = _evidence(inside[i - 1], numbers[j - 1], markers)
        if gain and best[i][j] == best[i - 1][j - 1] + gain:
            pairs.append((inside[i - 1], numbers[j - 1]))
            i, j = i - 1, j - 1
        elif best[i][j] == best[i - 1][j]:
            i -= 1
        else:
            j -= 1
    return pairs[::-1]


def _numbered(cand: Candidate) -> bool:
    return cand.source == "ink" and not cand.status.startswith("unplaced")


def number_calls(
    cands: list[Candidate],
    wanted: list[str],
    anchors: list[tuple[tuple, int]],
    markers: set[str] = frozenset(),
) -> int:
    """Number the page's candidates (D87): count and order first, Kraken's readings second.

    The calls already in the text (`anchors`) fix their numbers at their places. Between two of them (or
    a page end), when the call shapes (ink candidates, placed) are as many as the numbers still wanted
    there, they take those numbers in reading order whatever Kraken read: Kraken misreads a small raised
    digit often («(٢)» read «۶» and «(٣)» read «٢» on book 35 p. 2), while one note per call is the
    printer's rule. Otherwise the readings that agree with each other and with the order keep their
    numbers (`_aligned`) and, between those, the shapes and numbers left pair by order when they are as
    many. A token candidate (no ink shape) takes a number only from its reading. Returns how many
    candidates were numbered."""
    for cand in cands:
        cand.read, cand.number = cand.number, ""
    places = {id(c): _position(c.line.order, c.bbox) for c in cands}
    fixed = sorted(((pos, int(n)) for pos, n in anchors), key=lambda f: f[0])
    taken = {n for _p, n in fixed}
    left = [int(n) for n in wanted if n.isdigit() and int(n) not in taken]
    ordered = sorted(cands, key=lambda c: places[id(c)])
    bounds = [((-1, float("-inf")), 0), *fixed, ((10**9, float("inf")), 10**9)]
    count = 0

    def by_order(inside: list[Candidate], numbers: list[int]) -> int:
        shapes = [c for c in inside if _numbered(c) and not c.number]
        if not numbers or len(numbers) != len(shapes):
            return 0
        for number, cand in zip(numbers, shapes, strict=True):
            agrees = _evidence(cand, number, markers) > 0
            cand.number, cand.status = str(number), "accepted" if agrees else "accepted by order"
        return len(numbers)

    for (low_place, low), (high_place, high) in zip(bounds, bounds[1:], strict=False):
        numbers = [n for n in left if low < n < high]
        inside = [c for c in ordered if low_place < places[id(c)] < high_place]
        if not numbers or not inside:
            continue
        done = by_order(inside, numbers)
        if done:
            count += done
            continue
        pairs = _aligned(inside, numbers, markers)
        for cand, number in pairs:
            cand.number, cand.status = str(number), "accepted"
        count += len(pairs)
        stops = [(low_place, low), *((places[id(c)], n) for c, n in pairs), (high_place, high)]
        for (a_place, a), (b_place, b) in zip(stops, stops[1:], strict=False):
            count += by_order(
                [c for c in inside if a_place < places[id(c)] < b_place], [n for n in numbers if a < n < b]
            )
    for cand in cands:  # why a read candidate kept no number
        if not cand.number and cand.status == "accepted":
            cand.status = "out of order"
    return count


def _save_sample(gray: np.ndarray, cand: Candidate, page, sample_dir) -> None:
    from pathlib import Path

    from PIL import Image

    x0, y0, x1, y1 = cand.bbox
    pad = max(6, (y1 - y0) // 2)
    crop = gray[max(0, y0 - pad) : y1 + pad, max(0, x0 - pad) : x1 + pad]
    folder = Path(sample_dir)
    folder.mkdir(parents=True, exist_ok=True)
    Image.fromarray(crop).save(folder / f"p{page.number}_L{cand.line.order}_{cand.number}.png")


def read_page_calls(
    page, engine=None, style: str | None = None, dry_run: bool = False, sample_dir=None
) -> PageCalls:
    """The call pass on one finalised page (module docstring). `style`: the book's digits when the
    caller knows it; `dry_run`: read and report, write nothing; `sample_dir`: save the crop of every
    accepted call as a PNG. A line the reviewer changed while Kraken read is left as they made it."""
    import json

    from django.db import transaction

    from PIL import Image

    from books.models import Page
    from ocr.models import Line, OcrRun
    from ocr.services import count_unresolved
    from review.services import _shift_gaps, refresh_page_text

    from . import numbers
    from .engines import registry

    result = PageCalls()
    if page.reviewed_at is not None:
        result.skipped = "approved"
        return result
    style = style if style is not None else (numbers.book_style(page.book) or numbers.page_style(page))
    style = numbers.WESTERN if style == numbers.WESTERN else numbers.ARABIC_INDIC  # D87: both are read
    pre = getattr(page, "preprocess", None)
    if pre is None or not pre.gray_image:
        result.skipped = "no image"
        return result
    lines = [
        line
        for line in page.lines.filter(is_reviewed=False).select_related("region").order_by("order")
        if line.bbox
    ]
    gray = np.asarray(Image.open(pre.gray_image.path).convert("L"))
    line_height = float(pre.median_line_height or 0) or _median_height(lines)
    result.wanted, markers, result.candidates, anchors = page_plan(page, lines, gray, line_height)
    to_read = [c for c in result.candidates if not c.status]
    if not to_read:
        return result
    read_at = {line.pk: line.updated_at for line in lines}
    as_read = {line.pk: [id(token) for token in line.tokens or []] for line in lines}
    by_id: dict = {}
    if style == numbers.ARABIC_INDIC:  # Kraken's digits model reads Arabic-Indic digits only
        engine = engine or registry.get_engine("kraken")
        requests = [{"id": f"{c.id}@{k}", "bbox": c.bbox, "scale": k} for c in to_read for k in SCALES]
        answer = engine.read([{"image": pre.gray_image.path, "lines": requests}])
        result.seconds = float(answer.get("elapsed") or answer.get("seconds") or 0.0)
        by_id = {row.get("id"): row for row in answer.get("lines") or []}
    for cand in to_read:
        cand.readings = (
            {k: str((by_id.get(f"{cand.id}@{k}") or {}).get("text") or "") for k in SCALES} if by_id else {}
        )
        cand.reading, cand.number, cand.status, cand.score = best_reading(
            cand.readings, result.wanted, markers
        )
    number_calls(to_read, result.wanted, anchors, markers)
    result.accepted = sum(1 for c in to_read if c.number)
    if sample_dir:
        for cand in to_read:
            if cand.number:
                _save_sample(gray, cand, page, sample_dir)
    if dry_run or not result.accepted:
        return result
    changed: dict[int, object] = {}
    for cand in sorted(  # last token first on each line: an edit never moves the tokens still to edit
        (c for c in to_read if c.number),
        key=lambda c: (c.line.order, c.index if c.index is not None else c.after + 0.5, -c.bbox[2]),
        reverse=True,
    ):
        tokens = cand.line.tokens
        if cand.index is not None:
            apply_token(tokens, cand.index, cand.number, cand.bbox if cand.source == "ink" else None, style)
        else:
            insert_call(tokens, cand.after, cand.replace, cand.number, cand.bbox, style)
        changed[cand.line.pk] = cand.line
    with transaction.atomic():
        locked = Page.objects.select_for_update(of=("self",)).get(pk=page.pk)
        now = dict(
            Line.objects.filter(pk__in=list(changed), is_reviewed=False).values_list("pk", "updated_at")
        )
        kept = [
            line for pk, line in changed.items() if locked.reviewed_at is None and now.get(pk) == read_at[pk]
        ]
        for line in kept:
            line.text = " ".join(token["t"] for token in line.tokens)
            line.n_low = count_unresolved(line.tokens)
            line.save(update_fields=["tokens", "text", "n_low", "updated_at"])
            _shift_gaps(line, token_moves(as_read[line.pk], line.tokens), line.tokens)
            result.applied += sum(1 for c in to_read if c.number and c.line.pk == line.pk)
        if kept:
            refresh_page_text(page)
        params = {"pass": "calls", **result.as_dict()}
        params.pop("seconds", None)
        params.pop("skipped", None)
        if len(kept) < len(changed):
            params["left_to_reviewer"] = len(changed) - len(kept)
        OcrRun.objects.create(  # a western book's pass reads no digit: the run is the ink's alone (D87)
            page=page,
            engine_name=engine.name if engine else "ink",
            model_id=engine.model_id if engine else "",
            model_revision=engine.model_revision if engine else "",
            backend=engine.backend if engine else "",
            input_variant=("gray_" + "+".join(f"{k}x" for k in SCALES)) if engine else "gray",
            raw_output=json.dumps([c.as_dict() for c in result.candidates], ensure_ascii=False),
            parsed_text="\n".join(c.reading for c in to_read),
            params=params,
            duration_ms=int(result.seconds * 1000),
            finish="n/a",
        )
    log.info(
        "page %s: the call pass wrote %d calls (%d candidates)",
        page.pk,
        result.applied,
        len(result.candidates),
    )
    return result


def undo_calls(tokens: list[dict]) -> tuple[list[dict], dict[int, int]]:
    """A line's tokens without the calls a pass wrote (`call` tokens the reviewer has not resolved), and the
    old → new index of each token kept: an inserted call is dropped, a replacing one gives back what the
    models wrote (`qari`) in its box — a reading once glued to its word comes back as a token of its own,
    which the pass reads as the same call."""
    out: list[dict] = []
    moves: dict[int, int] = {}
    for i, token in enumerate(tokens):
        if token.get("call") and not token.get("res"):
            qari = str((token.get("qari") or {}).get("t") or "").strip()
            if not qari:
                continue
            token = {
                "t": qari,
                "alt": None,
                "tess": None,
                "conf": "low",
                "digit": False,
                "bbox": token.get("bbox"),
            }
        moves[i] = len(out)
        out.append(token)
    return out, moves


def undo_page_calls(page) -> int:
    """Undo the calls an earlier pass wrote on a page (`undo_calls`), so the pass reads the models' text again
    (`read_calls --redo`, D87). Never on an approved page or a reviewed line; the lines' suggestions follow
    their words. Returns how many call tokens were undone."""
    from django.db import transaction

    from books.models import Page
    from ocr.models import Line
    from ocr.services import count_unresolved
    from review.services import _shift_gaps, refresh_page_text

    undone = 0
    with transaction.atomic():
        locked = Page.objects.select_for_update(of=("self",)).get(pk=page.pk)
        if locked.reviewed_at is not None:
            return 0
        for line in Line.objects.filter(page=page, is_reviewed=False):
            tokens = line.tokens or []
            clean, moves = undo_calls(tokens)
            if len(moves) == len(tokens) and all(clean[j] is tokens[i] for i, j in moves.items()):
                continue
            undone += sum(1 for t in tokens if t.get("call") and not t.get("res"))
            line.tokens = clean
            line.text = " ".join(token["t"] for token in clean)
            line.n_low = count_unresolved(clean)
            line.save(update_fields=["tokens", "text", "n_low", "updated_at"])
            _shift_gaps(line, moves, clean)
        if undone:
            refresh_page_text(page)
    return undone


def _median_height(lines: list) -> float:
    heights = sorted(int(line.bbox[3]) - int(line.bbox[1]) for line in lines if line.bbox)
    return float(heights[len(heights) // 2]) if heights else 20.0
