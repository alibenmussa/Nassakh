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

DIGITS = "0-9٠-٩۰-۹"
_MARKER = re.compile(rf"^\s*[\(\[]?\s*([{DIGITS}]{{1,3}}|ا(?=[\s\)\]]))\s*[\)\]]?\s*[-–—ـ.:،]?(?:\s+|$)")
_PRESENT = re.compile(rf"[\(\[]\s*([{DIGITS}]{{1,3}})\s*[\)\]]")
_TOKEN = re.compile(rf"^[\(\[]\s*(?:[اأإآ”“\"'’‘]|[{DIGITS}]{{1,2}})?\s*[\)\]][.،:؛]?$")
_ACCEPT = re.compile(rf"^\s*([\(\)\[\]]?)\s*([{DIGITS}]{{1,2}})\s*([\(\)\[\]]?)\s*[.،:؛]?\s*$")
_DIGIT_RUN = re.compile(f"[{DIGITS}]")
_GLYPHS = re.compile(r"^[\(\)\[\]”“\"'’‘اأإآ\s.،]+$")
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


def wanted_numbers(body_texts: list[str], note_texts: list[str]) -> list[str]:
    """The call numbers the page's notes want and its body lacks: the markers of `note_texts` (a
    first note line without a marker wants 1) minus the bracketed numbers of `body_texts`."""
    markers = [line_marker(text) for text in note_texts if (text or "").strip()]
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


def core_centre(gray: np.ndarray, line_bbox: list[int]) -> float:
    """The centre of the line's core rows: the longest run of rows holding at least `CORE_SHARE` of
    the densest row's ink (the x-height band; the line box's centre when it holds no ink)."""
    x0, y0, x1, y1 = (int(v) for v in line_bbox)
    crop = gray[max(0, y0) : max(0, y1), max(0, x0) : max(0, x1)]
    if crop.size == 0:
        return (y0 + y1) / 2
    rows = (crop < INK_LEVEL).sum(axis=1)
    if not rows.max():
        return (y0 + y1) / 2
    dense = rows >= CORE_SHARE * rows.max()
    best, start = (0, 0), None
    for i, on in enumerate([*dense.tolist(), False]):
        if on and start is None:
            start = i
        elif not on and start is not None:
            if i - start > best[1] - best[0]:
                best = (start, i)
            start = None
    return y0 + (best[0] + best[1]) / 2


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


def is_glyph_token(token: dict) -> bool:
    """A token made of brackets, quote strokes or alefs only: the models' reading of a call."""
    return bool(_GLYPHS.match(str(token.get("t") or "")))


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
    if all(is_glyph_token(t) for t in tokens[start:end]):
        return after, (start, end)
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


def ink_candidates(line, gray: np.ndarray, line_height: float) -> list[Candidate]:
    """The clusters of uncovered call-like ink on a line, as candidates placed among its tokens. A
    cluster whose word before is itself a misread call token («(١١)» boxed on the wrong ink, book 29
    p. 33) is that token's true ink and replaces it."""
    from .alignment import WEAK

    tokens = line.tokens or []
    boxes = [t["bbox"] for t in tokens if t.get("bbox") and t.get("bq") != WEAK]
    centre = core_centre(gray, line.bbox)
    comps = components(gray, search_box(line.bbox, line_height))
    out = []
    for box in call_clusters(comps, boxes, centre, line_height):
        after = word_before(tokens, box)
        before = tokens[after] if after >= 0 else None
        if before is not None and _TOKEN.match(str(before.get("t") or "")):
            if not (before.get("res") or before.get("call")):
                out.append(Candidate(line, box, "ink", index=after))
            continue
        placed = place(tokens, box, comps, line_height)
        if placed is None:
            out.append(Candidate(line, box, "ink", status="unplaced words in the gap"))
            continue
        after, replace = placed
        out.append(Candidate(line, box, "ink", after=after, replace=replace))
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


def call_token(number: str, bbox: list[int] | None, qari: str) -> dict:
    """A call token Kraken read: «(N)» in Arabic-Indic digits, low (D17), a number, flagged `call`
    for review; what the models wrote stays under `qari`."""
    text = "(" + number.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")) + ")"
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


def apply_token(tokens: list[dict], index: int, number: str, bbox: list[int] | None = None) -> None:
    """The misread token `index` becomes the call «(N)» in its own box (or in `bbox`, the ink's)."""
    old = tokens[index]
    new = call_token(number, bbox or old.get("bbox"), str(old.get("t") or ""))
    if old.get("bq") and not bbox:
        new["bq"] = old["bq"]
    tokens[index] = new


def insert_call(
    tokens: list[dict], after: int, replace: tuple[int, int], number: str, bbox: list[int]
) -> None:
    """A new call token «(N)» after token `after`, in place of the lookalike tokens `replace`."""
    start, end = replace
    qari = " ".join(str(t.get("t") or "") for t in tokens[start:end])
    tokens[start:end] = [call_token(number, bbox, qari)]


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


def page_plan(
    page, lines: list, gray: np.ndarray, line_height: float
) -> tuple[list[str], set[str], list[Candidate]]:
    """The wanted numbers of a page, its note markers and its candidates in reading order (`lines`:
    its unreviewed lines with a box, in order, each with its effective `kind`)."""
    from ocr.services import SKIPPED_KINDS, line_kind

    kinds = {ln.pk: (ln.region.kind if ln.region_id else None) for ln in lines}
    lines = [ln for ln in lines if kinds[ln.pk] not in SKIPPED_KINDS]  # no running header, no page number
    body = [ln for ln in lines if line_kind(ln.role, kinds[ln.pk]) == "body"]
    notes = [ln for ln in lines if ln not in body]
    wanted = wanted_numbers([ln.text for ln in body], [ln.text for ln in notes])
    if not wanted:
        return wanted, set(), []
    markers = {m for ln in notes if (m := line_marker(ln.text)) is not None}
    out: list[Candidate] = []
    for line in body:
        found = ink_candidates(line, gray, line_height)
        replaced = {c.index for c in found if c.index is not None}
        found += [c for c in token_candidates(line, wanted, markers) if c.index not in replaced]
        found.sort(key=lambda c: -c.bbox[2])
        out.extend(found)
    return wanted, markers, out


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
    if style != numbers.ARABIC_INDIC:
        result.skipped = "not arabic-indic"
        return result
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
    result.wanted, markers, result.candidates = page_plan(page, lines, gray, line_height)
    to_read = [c for c in result.candidates if not c.status]
    if not to_read:
        return result
    read_at = {line.pk: line.updated_at for line in lines}
    as_read = {line.pk: [id(token) for token in line.tokens or []] for line in lines}
    engine = engine or registry.get_engine("kraken")
    requests = [{"id": f"{c.id}@{k}", "bbox": c.bbox, "scale": k} for c in to_read for k in SCALES]
    answer = engine.read([{"image": pre.gray_image.path, "lines": requests}])
    result.seconds = float(answer.get("elapsed") or answer.get("seconds") or 0.0)
    by_id = {row.get("id"): row for row in answer.get("lines") or []}
    by_number: dict[str, list[Candidate]] = {}
    for cand in to_read:
        cand.readings = {k: str((by_id.get(f"{cand.id}@{k}") or {}).get("text") or "") for k in SCALES}
        cand.reading, cand.number, cand.status, cand.score = best_reading(
            cand.readings, result.wanted, markers
        )
        if cand.number:
            by_number.setdefault(cand.number, []).append(cand)
    for number, cands in by_number.items():  # a number is called once on a page: the best-formed reading
        keep = max(cands, key=lambda c: (c.score, -to_read.index(c)))
        for cand in cands:
            if cand is not keep:
                cand.number, cand.status = "", f"«{number}» duplicate"
        result.accepted += 1
        if sample_dir:
            _save_sample(gray, keep, page, sample_dir)
    if dry_run or not result.accepted:
        return result
    changed: dict[int, object] = {}
    for cand in sorted(
        (c for c in to_read if c.number), key=lambda c: (c.line.order, c.after, -c.bbox[2]), reverse=True
    ):
        tokens = cand.line.tokens
        if cand.index is not None:
            apply_token(tokens, cand.index, cand.number, cand.bbox if cand.source == "ink" else None)
        else:
            insert_call(tokens, cand.after, cand.replace, cand.number, cand.bbox)
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
        OcrRun.objects.create(
            page=page,
            engine_name=engine.name,
            model_id=engine.model_id,
            model_revision=engine.model_revision,
            backend=engine.backend,
            input_variant="gray_" + "+".join(f"{k}x" for k in SCALES),
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


def _median_height(lines: list) -> float:
    heights = sorted(int(line.bbox[3]) - int(line.bbox[1]) for line in lines if line.bbox)
    return float(heights[len(heights) // 2]) if heights else 20.0
