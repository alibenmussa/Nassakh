"""Word boxes from Kraken (D92): a region's printed lines read by Kraken, shaped as Tesseract's lines.

The line builder (`ocr.alignment.build_lines`) gives each model word the line and the box of the reader's
word it matches. That reader was Tesseract, which reads vowelled print poorly (book 31: 76 % of the model
words took the box of their own word, 54 % in the body); Kraken, the reader of the numbers (D50) and the
calls (D83), reads it far better. So, for a region whose text is the models', Kraken reads the region's
printed lines once and its words stand in for Tesseract's in the geometry (Tesseract still gives the
readings, `alignment.with_readings`): `region_crops` finds the lines, `read_boxes` reads them in one call
of the runner, and `kraken_line` turns each answer into a Tesseract-shaped line.

1. The lines (`region_crops`): the page's line bands (`Preprocess.line_boxes`, the letters' dense core rows)
   in the region, each read across its columns in the rows of the Tesseract lines fitted to it; and the
   Tesseract lines that cover no band and lie apart from every band — short last lines of paragraphs the
   band detector misses (book 29 p. 23: 12 bands for 16 lines) — read on their own. A band far taller than
   the others holding two Tesseract lines is those lines.
2. The words (`split_words`): Kraken's characters split at spaces and at punctuation, as Tesseract's words
   mostly are («عنها:» → «عنها» «:»); a number keeps the brackets around it and a tight joiner inside it
   («(١)», «٢٢-٣٠٨»), as the numbers pass reads it whole (D50). A word's confidence is its characters' mean
   × 100. A line of lone letters is an ornament's specks and no line.
3. The boxes (`snap`): Kraken places a character at the peaks of its network, narrower than the glyph
   (0.76–0.84 of the ink, drifting 20–40 px near a line's ends). Each boundary between two words moves to
   the widest blank run of ink columns within `SNAP_WINDOW` line heights of Kraken's space; the line's first
   and last words reach the ink that runs on from them (not a page's rule beyond); every box is then trimmed
   to its ink, its rows to the ink components of its own line (`word_rows`).

Pure functions first; `read_boxes` does the one engine call.
"""

from __future__ import annotations

import time
import unicodedata
from dataclasses import dataclass

import numpy as np

from .alignment import INK_LEVEL, _blank_runs, _valid_bands, fit_lines, ink_columns, line_pitch, page_bands

PASS = "boxes"  # `OcrRun.params["pass"]` of Kraken's reading of a region's lines (the numbers pass has none)
SHAPE = 1  # version of the shaping below, stored on the run: a run shaped otherwise is shaped again
REACH = 0.5  # a line's rows reach at most this many pitches beyond its core (as `alignment.BAND_REACH`)
TALL_BAND = 1.8  # a band this many times the region's median band height holding two Tesseract lines is two
APART = 0.6  # a Tesseract line without a band is a missed printed line this many pitches from every band
CORE_SHARE = 0.5  # rows with at least this share of the densest row's ink are a line's core
SNAP_WINDOW = 0.2  # a boundary is looked for this many line heights either side of Kraken's space
SNAP_DISTANCE = 0.15  # a blank run counts this much less per pixel away from the middle of Kraken's space
EDGE_REACH = (
    1.0  # a line's first / last word reaches the ink running on from it, this many line heights at most
)
EDGE_GAP = 0.15  # ... across blanks narrower than this many line heights (a page's rule lies further away)
MIN_TEXT = 2  # a Tesseract line with no band is read when a word of it has this many letters or digits
OPENERS = frozenset("([{")
CLOSERS = frozenset(")]}")
JOINERS = frozenset("-–/.,٫٬:")  # one of these between two digit runs may be inside the number («٢٢-٣٠٨»)
JOIN_GAP = 0.08  # ... when the blank on either side is under this many line heights (spaced «٢٢ ـ ٣٠٨»: two)


@dataclass(frozen=True)
class Crop:
    """A printed line to read: its columns `x0..x1` (inside the region), its core rows `y0..y1`, the rows
    `lo..hi` its words may use (halfway to its neighbours), whether it is a Tesseract line the band detector
    missed (`line`), and the rows Kraken reads, `top..bottom` (its Tesseract lines' rows; `lo..hi` when
    None)."""

    x0: int
    y0: int
    x1: int
    y1: int
    lo: int
    hi: int
    line: bool = False
    top: int | None = None
    bottom: int | None = None

    @property
    def rows(self) -> tuple[int, int]:
        """The rows Kraken reads and the words' columns are found in."""
        return (self.lo if self.top is None else self.top, self.hi if self.bottom is None else self.bottom)

    @property
    def box(self) -> list[int]:
        """What Kraken reads (the runner adds its margin): the line's columns and `rows`."""
        return [self.x0, self.rows[0], self.x1, self.rows[1]]

    def as_list(self) -> list:
        return [self.x0, self.y0, self.x1, self.y1, self.lo, self.hi, self.line, self.top, self.bottom]


# ---------------------------------------------------------------- 1. the lines


def ink_core(gray: np.ndarray | None, box: list[int]) -> tuple[int, int]:
    """The core rows of the ink in `box` (rows with at least `CORE_SHARE` of the densest row's ink), the
    box's own rows when there is no image or no ink."""
    x0, y0, x1, y1 = (int(v) for v in box)
    if gray is None:
        return y0, y1
    h, w = gray.shape[:2]
    a, b = max(0, y0), min(h, y1)
    part = gray[a:b, max(0, x0) : min(w, x1)]
    if not part.size:
        return y0, y1
    rows = (part < INK_LEVEL).sum(axis=1)
    if not rows.max():
        return y0, y1
    dense = np.flatnonzero(rows >= CORE_SHARE * rows.max())
    return a + int(dense[0]), a + int(dense[-1]) + 1


def _texty(line: dict) -> bool:
    return any(
        sum(1 for ch in str(w.get("text") or "") if ch.isalnum()) >= MIN_TEXT for w in line.get("words") or []
    )


def region_crops(
    bands: list[dict] | None, tess_lines: list[dict] | None, bbox: list[int], gray: np.ndarray | None
) -> list[Crop]:
    """The printed lines of the region `bbox` to read, top to bottom (module docstring, 1).

    `bands` are the page's `Preprocess.line_boxes`, `tess_lines` the region's Tesseract lines (gray-image
    pixels). A band belongs to the region when its middle row lies in it and its columns overlap it; it is
    read in its columns and in the rows of the Tesseract lines fitted to it (`alignment.fit_lines`), or of
    its own reach without them. A Tesseract line that covers no band is a line the detector missed only when
    its ink lies `APART` pitches or more from every band (else it is a raised call or the marks above a thin
    band); two such lines whose ink cores share rows (one printed line in two pieces) are read as one.
    """
    rx0, ry0, rx1, ry1 = (int(v) for v in bbox)
    valid = _valid_bands(bands)
    inside = [
        b for b in valid if ry0 <= (b["y0"] + b["y1"]) / 2 < ry1 and min(b["x1"], rx1) > max(b["x0"], rx0)
    ]
    lines = [ln for ln in tess_lines or [] if ln.get("bbox") and _texty(ln)]
    heights = sorted(b["y1"] - b["y0"] for b in inside)
    median_h = heights[len(heights) // 2] if heights else 0
    # the region's pitch, or the page's when that is shorter (a region's missed bands make its own too long)
    pitches = [p for p in (line_pitch(inside), line_pitch(valid)) if p]
    apart = APART * (min(pitches) if pitches else 3.0 * max(1, median_h))
    cores: list[dict] = []
    used: set[int] = set()  # the Tesseract lines that stand for a band already
    for b in inside:
        held = [k for k, ln in enumerate(lines) if b["y0"] <= (ln["bbox"][1] + ln["bbox"][3]) / 2 <= b["y1"]]
        if median_h and b["y1"] - b["y0"] > TALL_BAND * median_h and len(held) >= 2:
            for k in held:  # two printed lines the detector took for one
                x0, top, x1, bottom = lines[k]["bbox"]
                y0, y1 = ink_core(gray, [x0, max(b["y0"], top), x1, min(b["y1"], bottom)])
                cores.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "line": True})
                used.add(k)
            continue
        cores.append({**b, "line": False})
    rows: dict[tuple[int, int], tuple[int, int]] = {}  # a band's core → the rows of its Tesseract lines
    missed: list[dict] = []
    for k, fitted in enumerate(fit_lines(lines, page_bands(valid))):
        if k in used:
            continue
        box = [int(v) for v in fitted["bbox"]]
        if fitted.get("core") is not None:
            key = (int(fitted["core"][0]), int(fitted["core"][1]))
            top, bottom = rows.get(key, (box[1], box[3]))
            rows[key] = (min(top, box[1]), max(bottom, box[3]))
            continue
        y0, y1 = ink_core(gray, box)
        mid = (y0 + y1) / 2
        if any(abs(mid - (c["y0"] + c["y1"]) / 2) < apart for c in cores):
            continue  # on a band's printed line: a raised call, the marks above a thin core
        near = next((m for m in missed if min(m["y1"], y1) > max(m["y0"], y0)), None)
        if near is None:
            missed.append({"x0": box[0], "y0": y0, "x1": box[2], "y1": y1, "line": True})
        else:  # the same missed line in two pieces
            near.update(x0=min(near["x0"], box[0]), y0=min(near["y0"], y0))
            near.update(x1=max(near["x1"], box[2]), y1=max(near["y1"], y1))
    cores = sorted(cores + missed, key=lambda c: (c["y0"], c["y1"]))
    pitch = line_pitch(cores) or line_pitch(valid) or 3.0 * max(1, median_h)
    out: list[Crop] = []
    for n, c in enumerate(cores):
        lo, hi = c["y0"] - REACH * pitch, c["y1"] + REACH * pitch
        if n:
            lo = max(lo, (cores[n - 1]["y1"] + c["y0"]) / 2)
        if n + 1 < len(cores):
            hi = min(hi, (c["y1"] + cores[n + 1]["y0"]) / 2)
        lo, hi = max(0, min(int(lo), int(c["y0"]))), max(int(round(hi)), int(c["y1"]))
        top = bottom = None
        fitted_rows = None if c["line"] else rows.get((int(c["y0"]), int(c["y1"])))
        if fitted_rows is not None:  # the rows Tesseract's lines took there, inside the line's reach
            top = max(lo, min(int(c["y0"]), fitted_rows[0]))
            bottom = min(hi, max(int(c["y1"]), fitted_rows[1]))
        x0, x1 = max(rx0, int(c["x0"])), min(rx1, int(c["x1"]))
        if x1 > x0 and hi > lo:
            out.append(Crop(x0, int(c["y0"]), x1, int(c["y1"]), lo, hi, bool(c["line"]), top, bottom))
    return out


# ---------------------------------------------------------------- 2. the words


def _class(ch: str) -> str:
    """'S' a space, 'D' a digit, 'L' a letter, a mark or the tatweel, 'P' anything else."""
    if ch.isspace():
        return "S"
    category = unicodedata.category(ch)
    if category == "Nd":
        return "D"
    if ch.isalpha() or category.startswith("M"):
        return "L"
    return "P"


def split_words(chars: list[list]) -> list[tuple[list[list], bool]]:
    """Kraken's characters (`[char, x0, x1, conf]`, reading order) as word pieces: split at spaces, and at
    punctuation within them (letters and digits stay together); a number keeps the brackets just around it
    («(١)»). Each piece is its characters' rows and whether it may continue the number before it: a joiner
    between two digit runs and the digits after it («٢٢-٣٠٨», «١٢/٣»), one number when the ink between them
    is tight (`kraken_line`), two when Kraken dropped the spaces around a dash («٢٢ ـ ٣٠٨»)."""
    words: list[tuple[list[list], bool]] = []
    for group in _space_split(chars):
        segments: list[tuple[str, list[list]]] = []  # ('T' letters/digits | 'P' punctuation, rows)
        for row in group:
            kind = "P" if _class(str(row[0])) == "P" else "T"
            if segments and segments[-1][0] == kind:
                segments[-1][1].append(row)
            else:
                segments.append((kind, [row]))
        words.extend(_number_pieces(segments))
    return words


def _space_split(chars: list[list]) -> list[list[list]]:
    groups: list[list[list]] = []
    current: list[list] = []
    for row in chars or []:
        if _class(str(row[0])) == "S":
            if current:
                groups.append(current)
            current = []
        else:
            current.append(row)
    if current:
        groups.append(current)
    return groups


def _has_digit(rows: list[list]) -> bool:
    return any(_class(str(row[0])) == "D" for row in rows)


def _number_pieces(segments: list[tuple[str, list[list]]]) -> list[tuple[list[list], bool]]:
    """The pieces of one space-delimited group (`split_words`): its segments, each number with the brackets
    just around it, and a joiner between two digit runs marked, with the digits after it, as the number's."""
    kinds = [kind for kind, _ in segments]
    rows = [list(r) for _, r in segments]
    out: list[tuple[list[list], bool]] = []
    k = 0
    while k < len(rows):
        if kinds[k] != "T" or not _has_digit(rows[k]):
            out.append((rows[k], False))
            k += 1
            continue
        word = rows[k]
        glued = bool(out) and out[-1][1] is True and kinds[k - 1] == "P"  # the digits after a joiner
        if not glued and out and _punctuation(out[-1][0]):  # «(» just before it
            before = out[-1][0]
            opening = 0
            while opening < len(before) and str(before[-1 - opening][0]) in OPENERS:
                opening += 1
            if opening:
                word = before[len(before) - opening :] + word
                del before[len(before) - opening :]
                if not before:
                    out.pop()
        after = rows[k + 1] if k + 1 < len(rows) and kinds[k + 1] == "P" else None
        joiner = (
            after is not None
            and len(after) == 1
            and str(after[0][0]) in JOINERS
            and k + 2 < len(rows)
            and _has_digit(rows[k + 2])
        )
        if after is not None and not joiner:  # «)» just after it
            closing = 0
            while closing < len(after) and str(after[closing][0]) in CLOSERS:
                closing += 1
            word = word + after[:closing]
            rows[k + 1] = after[closing:]
        out.append((word, glued))
        if joiner:
            out.append((after, True))
            k += 2
            continue
        k += 1
    return [(w, glued) for w, glued in out if w]


def _punctuation(rows: list[list]) -> bool:
    return all(_class(str(row[0])) == "P" for row in rows)


def word_text(rows: list[list]) -> str:
    return "".join(str(row[0]) for row in rows)


def word_conf(rows: list[list]) -> float:
    """The mean confidence of a word's characters, as a percentage (Tesseract's scale)."""
    confs = [float(row[3]) for row in rows if len(row) > 3 and row[3] is not None]
    return round(100.0 * sum(confs) / len(confs), 1) if confs else 0.0


def _stray(rows: list[list]) -> bool:
    """A word of one letter and no digit: all Kraken reads of an ornament's specks («چ», «ر ن»)."""
    letters = sum(1 for row in rows if _class(str(row[0])) == "L")
    return not _has_digit(rows) and letters <= 1 and not _punctuation(rows)


# ---------------------------------------------------------------- 3. the boxes


def snap(spans: list[tuple[float, float]], gray: np.ndarray, crop: Crop) -> list[tuple[int, int]]:
    """Columns `(x0, x1)` of each word of one line from Kraken's raw spans (module docstring, 3).

    `spans` are the words' raw extents in any order; the answer keeps that order. Boundaries are set between
    neighbours on the page (right to left), in the ink of the rows read (`crop.rows`).
    """
    n = len(spans)
    if not n:
        return []
    height = max(1, crop.rows[1] - crop.rows[0])
    lo = max(0, int(min(crop.x0, min(s[0] for s in spans)) - EDGE_REACH * height))
    hi = int(max(crop.x1, max(s[1] for s in spans)) + EDGE_REACH * height)
    profile = ink_columns(gray, crop.rows, lo, hi)
    order = sorted(range(n), key=lambda i: -(spans[i][0] + spans[i][1]))  # right to left
    edges = {i: [float(spans[i][0]), float(spans[i][1])] for i in range(n)}
    reach = max(4, int(SNAP_WINDOW * height))
    for right, left in zip(order, order[1:], strict=False):
        r, e = edges[right], edges[left]
        gap_a, gap_b = min(e[1], r[0]), max(e[1], r[0])
        a = int(max(gap_a - reach, (e[0] + e[1]) / 2)) - lo
        b = int(min(gap_b + reach, (r[0] + r[1]) / 2)) - lo
        a, b = max(0, a), min(len(profile), b)
        runs = [(s + a, t + a) for s, t in _blank_runs(profile[a:b])] if b > a else []
        mid = (e[1] + r[0]) / 2 - lo
        if runs:
            s, t = max(runs, key=lambda q: (q[1] - q[0]) - SNAP_DISTANCE * abs((q[0] + q[1]) / 2 - mid))
            e[1], r[0] = float(lo + s), float(lo + t)
        elif e[1] > r[0]:  # overlapping and no blank between: part them in the middle
            e[1] = r[0] = lo + mid
    if order:  # the line's first and last words reach the ink that runs on from them, not a rule beyond
        first, last = edges[order[0]], edges[order[-1]]
        limit, gap = int(EDGE_REACH * height), max(2, int(EDGE_GAP * height))
        first[1] = max(first[1], float(lo + _run_on(profile, int(first[1]) - lo, 1, limit, gap)))
        last[0] = min(last[0], float(lo + _run_on(profile, int(last[0]) - lo, -1, limit, gap)))
    out: list[tuple[int, int]] = []
    for i in range(n):
        x0, x1 = int(round(edges[i][0])), int(round(edges[i][1]))
        a, b = max(0, x0 - lo), max(0, min(len(profile), x1 - lo))
        ink = np.flatnonzero(profile[a:b] > 0) if b > a else np.array([], dtype=int)
        if ink.size:
            out.append((lo + a + int(ink[0]), lo + a + int(ink[-1]) + 1))
        else:
            out.append((min(x0, x1), max(x0, x1, min(x0, x1) + 1)))
    return out


def _run_on(profile: np.ndarray, start: int, step: int, limit: int, gap: int) -> int:
    """From column `start`, the far edge of the ink that runs on in direction `step` (1: right, −1: left)
    across blank runs shorter than `gap`, at most `limit` columns away; `start` itself when there is none."""
    reached, blank = start, 0
    for k in range(1, limit + 1):
        x = start + step * k
        if x < 0 or x >= len(profile):
            break
        if profile[x] > 0:
            reached, blank = x + (1 if step > 0 else 0), 0
        else:
            blank += 1
            if blank >= gap:
                break
    return reached


def word_rows(gray: np.ndarray, crop: Crop, x0: int, x1: int) -> tuple[int, int]:
    """The rows of a word's own ink in columns `x0..x1`: the ink components that touch the line's core rows
    (its letters) and those lying wholly inside its rows `crop.lo..crop.hi` (its marks); a neighbour's
    ascender or descender reaching into those rows is left out. The line's rows when the word shows no ink."""
    import cv2  # the ink's components (OpenCV, as the preparation stage)

    h, w = gray.shape[:2]
    pad = crop.hi - crop.lo
    top, bottom = max(0, crop.lo - pad), min(h, crop.hi + pad)
    part = gray[top:bottom, max(0, x0) : min(w, x1)] < INK_LEVEL
    if not part.size or not part.any():
        return crop.lo, crop.hi
    n, _labels, stats, _ = cv2.connectedComponentsWithStats(part.astype(np.uint8), connectivity=8)
    core0, core1 = crop.y0 - top, crop.y1 - top
    lo, hi = crop.lo - top, crop.hi - top
    tops, bottoms = [], []
    for k in range(1, n):
        a = int(stats[k, cv2.CC_STAT_TOP])
        b = a + int(stats[k, cv2.CC_STAT_HEIGHT])
        if (a < core1 and b > core0) or (a >= lo and b <= hi):
            tops.append(a)
            bottoms.append(b)
    if not tops:
        return crop.lo, crop.hi
    return max(crop.lo, top + min(tops)), min(crop.hi, top + max(bottoms))


def kraken_line(chars: list[list], crop: Crop, gray: np.ndarray) -> dict | None:
    """One printed line as Tesseract gives it: `{"bbox", "words": [{"text", "bbox", "conf"}]}` in reading
    order, boxes in gray-image pixels. The pieces of a number join when the blank between them is under
    `JOIN_GAP` line heights (`split_words`). None when Kraken placed no word, or only lone letters (an
    ornament's specks)."""
    pieces = [(rows, glued) for rows, glued in split_words(chars) if any(row[1] is not None for row in rows)]
    if not pieces or all(_stray(rows) for rows, _ in pieces):
        return None
    spans = [
        (
            min(float(r[1]) for r in rows if r[1] is not None),
            max(float(r[2]) for r in rows if r[2] is not None),
        )
        for rows, _ in pieces
    ]
    snapped = snap(spans, gray, crop)
    words: list[tuple[list[list], int, int]] = []
    tight = JOIN_GAP * max(1, crop.rows[1] - crop.rows[0])
    number = False  # the last word is a number that a joiner or the digits after one may continue
    for (rows, glued), (x0, x1) in zip(pieces, snapped, strict=True):
        if glued and number and words:
            prev_rows, a, b = words[-1]
            if max(a, x0) - min(b, x1) <= tight:  # the blank between the two (none when they overlap)
                words[-1] = (prev_rows + rows, min(a, x0), max(b, x1))
                continue
        words.append((rows, x0, x1))
        number = _has_digit(rows)
    out = []
    for rows, x0, x1 in words:
        y0, y1 = word_rows(gray, crop, x0, x1)
        out.append({"text": word_text(rows), "bbox": [x0, y0, x1, y1], "conf": word_conf(rows)})
    boxes = [w["bbox"] for w in out]
    bbox = [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]
    return {"bbox": bbox, "words": out}


# ---------------------------------------------------------------- the reading


def read_boxes(
    engine, image_path: str, gray: np.ndarray, crops: dict[object, list[Crop]]
) -> tuple[dict[object, list[dict]], dict[object, list[dict]], float]:
    """Kraken reads every crop of every region in one call (`engine.read`); for each region key, its lines
    (`kraken_line`, top to bottom, lines without words left out), Kraken's answer rows per crop, and the
    seconds the call took. Raises what the engine raises (`KrakenError`)."""
    requests = [
        {"id": f"{n}:{k}", "bbox": crop.box}
        for n, key in enumerate(crops)
        for k, crop in enumerate(crops[key])
    ]
    if not requests:
        return {key: [] for key in crops}, {key: [] for key in crops}, 0.0
    started = time.monotonic()
    answer = engine.read([{"image": str(image_path), "lines": requests}])
    seconds = float(answer.get("elapsed") or round(time.monotonic() - started, 3))
    by_id = {str(row.get("id")): row for row in answer.get("lines") or []}
    lines: dict[object, list[dict]] = {}
    raw: dict[object, list[dict]] = {}
    for n, key in enumerate(crops):
        lines[key], raw[key] = [], []
        for k, crop in enumerate(crops[key]):
            row = by_id.get(f"{n}:{k}") or {}
            chars = row.get("chars") or []
            raw[key].append({"text": str(row.get("text") or ""), "chars": chars})
            line = kraken_line(chars, crop, gray)
            if line is not None:
                lines[key].append(line)
    return lines, raw, seconds


def shape_lines(raw: list[dict], crops: list[Crop], gray: np.ndarray) -> list[dict]:
    """A region's lines from Kraken's stored answer rows (`read_boxes`' second value) and their crops."""
    out = []
    for row, crop in zip(raw, crops, strict=False):
        line = kraken_line(row.get("chars") or [], crop, gray)
        if line is not None:
            out.append(line)
    return out
