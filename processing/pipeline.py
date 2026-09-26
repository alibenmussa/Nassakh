"""Pure image functions of the preprocessing stage, ported from `playground/poc/preprocess.py`.

No Django here: every function takes and returns numpy arrays and plain Python values so the
stage can be unit-tested on synthetic pages. `run_pipeline` is the single entry point used by
`processing.services`; the order of the steps is fixed and matches the PoC:

    dark borders → background flattening → denoise → skew (on the cleaned ink mask) → rotate
    → edge strips (D18) → crop → text lines → page number → footnote rule / footnote block
    → Sauvola binarisation

Conventions: grayscale images are 2-D `uint8` arrays (255 = paper); ink masks are `uint8`
with 255 = ink. Boxes are `[x0, y0, x1, y1]` with exclusive upper bounds. `crop_box` and
`edge_strips_removed` are in the coordinates of the rotated (still uncropped) image; line boxes,
the footnote rule / block and the page-number box are in the coordinates of the cropped output
(`gray_image` pixel space).

Footnotes and page numbers are detected per page: a separator rule (solid, dotted, dashed or
short) or, without one, a block of smaller type at the bottom; a page number is a short, isolated
first or last line. Nothing here is applied from other pages of the book.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import cv2
import numpy as np
from skimage.filters import threshold_sauvola

Box = list[int]
LineBox = dict[str, int]

DEFAULT_NLM_H = 6
DEFAULT_SAUVOLA_K = 0.2
DEFAULT_MAX_SKEW_DEG = 5.0
DEFAULT_CROP_MARGIN_FRAC = 0.02

FLAG_LARGE_SKEW = "large_skew"
FLAG_LOW_CONFIDENCE = "deskew_low_confidence"
FLAG_NO_LINES = "no_lines_detected"
FLAG_EDGE_STRIP = "edge_strip_removed"
PREPROCESS_FLAGS: tuple[str, ...] = (FLAG_LARGE_SKEW, FLAG_LOW_CONFIDENCE, FLAG_NO_LINES, FLAG_EDGE_STRIP)

LARGE_SKEW_DEG = 3.0
LOW_SKEW_CONFIDENCE = 0.10

# `kind` of a removed edge strip: fragments of the facing page's text, or a scan-edge sliver.
STRIP_TEXT = "text"
STRIP_ARTIFACT = "artifact"
# A removed band counts as a scan-edge artifact only when its ink runs this share of the height.
ARTIFACT_MIN_HEIGHT_FRAC = 0.25

# Footnote rule: horizontal closing kernel and minimum width (of the text block).
RULE_CLOSE_FRAC = 0.03
RULE_MIN_WIDTH_FRAC = 0.12
# Thick rules (D68), taken only when no strict rule exists: mean thickness up to this share of the line
# height, and a component that fills at least this share of its box (a closed text row fills less).
NEAR_RULE_THICKNESS = 0.5
RULE_MIN_FILL = 0.6
# Footnote block without a rule: smaller type at the bottom of the page.
BLOCK_MAX_SIZE_RATIO = 0.8
BLOCK_MIN_LINES = 2
BLOCK_MIN_GAP_PITCH = 1.2
BLOCK_LOWER_FRAC = 0.45
# Page number: a short isolated first/last line near the top or bottom edge.
PN_MAX_WIDTH_FRAC = 0.15
PN_MAX_HEIGHT_RATIO = 1.4
PN_MIN_GAP_RATIO = 0.8
PN_TOP_FRAC = 0.12
PN_BOTTOM_FRAC = 0.15
# Candidate rows beyond the text are split where the ink leaves this many type sizes of gap.
CLUSTER_GAP_RATIO = 3.0


@dataclass(slots=True)
class PreprocessParams:
    """Knobs of one pipeline run. `None` means "detect automatically" for the overridable values.

    `angle`, `crop_box`, `sauvola_window`, `sauvola_k` and `nlm_h` are the manual overrides the
    UI can set (they correspond one to one to `Preprocess` fields); the rest are constants of the
    algorithm that the PoC settled on.
    """

    angle: float | None = None
    crop_box: Box | None = None
    sauvola_window: int | None = None
    sauvola_k: float | None = None
    nlm_h: int | None = None
    max_skew_deg: float = DEFAULT_MAX_SKEW_DEG
    crop_margin_frac: float = DEFAULT_CROP_MARGIN_FRAC
    remove_edge_strips: bool = True

    @property
    def is_manual(self) -> bool:
        """True when at least one overridable value was set by hand."""
        return any(
            v is not None
            for v in (self.angle, self.crop_box, self.sauvola_window, self.sauvola_k, self.nlm_h)
        )


@dataclass(slots=True)
class PreprocessResult:
    """Everything one pipeline run produced: the two output images and every stored parameter."""

    gray: np.ndarray
    bw: np.ndarray
    angle: float
    skew_confidence: float
    border_crop: Box
    crop_box: Box
    bg_kernel: int
    nlm_h: int
    sauvola_window: int
    sauvola_k: float
    output_width: int
    output_height: int
    line_boxes: list[LineBox]
    median_line_height: float
    n_lines: int
    footnote_rule_y: int | None
    footnote_block_y: int | None
    page_number_box: dict | None
    edge_strips_removed: list[dict]
    flags: list[str]
    auto_params: dict = field(default_factory=dict)

    def model_fields(self) -> dict:
        """The scalar/JSON values as `Preprocess` field names (images excluded)."""
        data = asdict(self)
        data.pop("gray")
        data.pop("bw")
        data.pop("flags")
        return data


# ---------------------------------------------------------------- helpers


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous True runs of a 1-D boolean mask as (start, end_exclusive)."""
    idx = np.where(mask)[0]
    if idx.size == 0:
        return []
    out = []
    for r in np.split(idx, np.where(np.diff(idx) > 1)[0] + 1):
        out.append((int(r[0]), int(r[-1]) + 1))
    return out


def _odd(n: int, minimum: int = 3) -> int:
    """Smallest odd integer ≥ max(n, minimum)."""
    n = max(int(n), minimum)
    return n if n % 2 else n + 1


def clamp_box(box, width: int, height: int) -> Box:
    """Round a `[x0, y0, x1, y1]` box to ints inside a width × height image (x0 < x1, y0 < y1)."""
    x0, y0, x1, y1 = (int(round(float(v))) for v in box)
    x0, x1 = sorted((min(max(x0, 0), width), min(max(x1, 0), width)))
    y0, y1 = sorted((min(max(y0, 0), height), min(max(y1, 0), height)))
    if x1 - x0 < 8 or y1 - y0 < 8:
        raise ValueError("crop box is empty")
    return [x0, y0, x1, y1]


# ---------------------------------------------------------------- steps (PoC port)


def remove_dark_borders(gray: np.ndarray, max_frac: float = 0.15) -> tuple[np.ndarray, Box]:
    """Trim rows/columns at the edges that are mostly black (scanner bed, tape).

    Returns the trimmed image and the kept box `[x0, y0, x1, y1]` in the input coordinates.
    """
    h, w = gray.shape
    dark_rows = (gray < 70).mean(axis=1) > 0.5
    dark_cols = (gray < 70).mean(axis=0) > 0.5

    def trim(flags: np.ndarray, limit: int) -> tuple[int, int]:
        a = 0
        while a < limit and flags[a]:
            a += 1
        b = flags.size
        while b > flags.size - limit and flags[b - 1]:
            b -= 1
        return a, b

    y0, y1 = trim(dark_rows, int(max_frac * h))
    x0, x1 = trim(dark_cols, int(max_frac * w))
    # a dark strip that is not exactly at the edge (sample 4): look for the last mostly-dark
    # column within the limit and cut there too
    for i in range(x0, int(max_frac * w)):
        if dark_cols[i]:
            x0 = i + 1
    for i in range(x1 - 1, w - int(max_frac * w), -1):
        if dark_cols[i]:
            x1 = i
    if x1 - x0 < 8 or y1 - y0 < 8:  # a nearly black image: keep it whole rather than nothing
        return gray, [0, 0, int(w), int(h)]
    return gray[y0:y1, x0:x1], [int(x0), int(y0), int(x1), int(y1)]


def flatten_background(gray: np.ndarray) -> tuple[np.ndarray, int]:
    """Divide by an estimated paper background so paper becomes ~white. Returns (image, kernel)."""
    k = (max(15, gray.shape[1] // 30)) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    bg = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel)
    bg = cv2.GaussianBlur(bg, (0, 0), k / 3)
    norm = cv2.divide(gray.astype(np.float32), np.maximum(bg.astype(np.float32), 1.0), scale=255.0)
    norm = np.clip(norm, 0, 255)
    lo = float(np.percentile(norm, 1.0))
    norm = np.clip((norm - lo) * (255.0 / max(255.0 - lo, 1.0)), 0, 255)
    return norm.astype(np.uint8), int(k)


def denoise(gray: np.ndarray, h: int) -> np.ndarray:
    """Mild non-local-means denoising; `h <= 0` disables it."""
    if h <= 0:
        return gray
    return cv2.fastNlMeansDenoising(gray, None, h=float(h), templateWindowSize=7, searchWindowSize=21)


def estimate_skew(ink: np.ndarray, max_angle: float = DEFAULT_MAX_SKEW_DEG) -> tuple[float, float]:
    """Return (angle_deg, confidence) from a cleaned ink mask (255 = ink).

    Rotating by +angle (counter-clockwise, `rotate()`) straightens the lines. The score is the
    variance of the horizontal projection profile; confidence compares the best score with the
    median over all coarse angles (a flat curve means the estimate is unreliable).
    """
    h, w = ink.shape
    scale = min(1.0, 1200 / max(h, w))
    small = cv2.resize(ink, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    mask = (small > 64).astype(np.float32)
    if mask.sum() == 0:
        return 0.0, 0.0
    sh, sw = mask.shape
    center = (sw / 2, sh / 2)

    def score(angle: float) -> float:
        m = cv2.getRotationMatrix2D(center, angle, 1.0)
        rot = cv2.warpAffine(mask, m, (sw, sh), flags=cv2.INTER_NEAREST, borderValue=0)
        return float(rot.sum(axis=1).var())

    coarse = np.arange(-max_angle, max_angle + 1e-6, 0.5)
    cs = [score(a) for a in coarse]
    best = float(coarse[int(np.argmax(cs))])
    fine = np.arange(best - 0.6, best + 0.6 + 1e-6, 0.1)
    fs = [score(a) for a in fine]
    best = float(fine[int(np.argmax(fs))])
    med = float(np.median(np.array(cs)))
    conf = (max(fs) - med) / (med + 1e-6)
    return round(best, 2), round(conf, 3)


def rotate(gray: np.ndarray, angle: float) -> np.ndarray:
    """Rotate counter-clockwise by `angle` degrees about the centre, padding with white."""
    if abs(angle) < 0.05:
        return gray
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(
        gray, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=255
    )


def binarize_clean(gray: np.ndarray, min_area: int, max_area_frac: float = 0.05) -> np.ndarray:
    """Otsu ink mask with tiny specks and huge blobs (borders, staples) removed."""
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    areas = stats[:, cv2.CC_STAT_AREA]
    max_area = max_area_frac * ink.size
    keep = np.zeros(n, dtype=bool)
    keep[1:] = (areas[1:] >= min_area) & (areas[1:] <= max_area)
    return np.where(keep[labels], 255, 0).astype(np.uint8)


def content_bbox(ink: np.ndarray, margin_frac: float = DEFAULT_CROP_MARGIN_FRAC) -> Box:
    """Bounding box of the ink plus a constant margin (a fraction of the width); whole image if empty."""
    h, w = ink.shape
    rows = (ink > 0).sum(axis=1)
    cols = (ink > 0).sum(axis=0)
    ys = np.where(rows > max(3, 0.002 * w))[0]
    xs = np.where(cols > max(3, 0.002 * h))[0]
    if ys.size == 0 or xs.size == 0:
        return [0, 0, int(w), int(h)]
    m = int(margin_frac * w)
    return [
        max(0, int(xs[0]) - m),
        max(0, int(ys[0]) - m),
        min(w, int(xs[-1]) + 1 + m),
        min(h, int(ys[-1]) + 1 + m),
    ]


def detect_lines(ink: np.ndarray) -> tuple[list[LineBox], float]:
    """Text-line boxes from the horizontal projection profile. Returns (lines, median_height).

    Slivers (diacritics, dots) are merged into the neighbouring line when the gap is small.
    """
    h, w = ink.shape
    if h == 0 or w == 0:
        return [], 0.0
    prof = (ink > 0).sum(axis=1).astype(np.float32)
    k = max(3, h // 400) | 1
    prof_s = np.convolve(prof, np.ones(k) / k, mode="same")
    if prof_s.max() <= 0:
        return [], 0.0
    p8 = np.clip(prof_s / prof_s.max() * 255, 0, 255).astype(np.uint8).reshape(-1, 1)
    otsu, _ = cv2.threshold(p8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    thr = min(otsu / 255.0 * prof_s.max(), 0.3 * prof_s.max())
    runs = _runs(prof_s > thr)
    if not runs:
        return [], 0.0
    heights = np.array([b - a for a, b in runs])
    med = float(np.median(heights[heights >= np.percentile(heights, 50)])) if heights.size else 0.0
    merged: list[list[int]] = []
    for a, b in runs:
        if (
            merged
            and (a - merged[-1][1]) < 0.3 * med
            and ((b - a) < 0.5 * med or (merged[-1][1] - merged[-1][0]) < 0.5 * med)
        ):
            merged[-1][1] = b
        else:
            merged.append([a, b])
    lines: list[LineBox] = []
    for a, b in merged:
        if (b - a) < 0.3 * med:
            continue
        cols = np.where((ink[a:b] > 0).sum(axis=0) > 0)[0]
        if cols.size == 0:
            continue
        lines.append({"x0": int(cols[0]), "y0": int(a), "x1": int(cols[-1]) + 1, "y1": int(b)})
    return lines, round(med, 1)


def _text_block_width(lines: list[LineBox], fallback: int) -> int:
    """Width of the text block spanned by `lines` (`fallback` when there are none)."""
    if not lines:
        return int(fallback)
    return int(max(ln["x1"] for ln in lines) - min(ln["x0"] for ln in lines)) or int(fallback)


def detect_footnote_rule(
    gray: np.ndarray, lines: list[LineBox], med_h: float, extra_rows: list[LineBox] | None = None
) -> int | None:
    """y of a footnote separator rule in `gray`, or None.

    Connected components of the raw Otsu ink mask (not the speck-cleaned one) after a horizontal
    closing of about 3% of the width, which bridges the gaps of dotted and dashed rules. A rule is
    a component in the lower 60% of the page that is at least 12% of the text-block width wide
    (short rules at the start of the line count), at most max(12, 6% of the height) tall, with
    text lines both above and below; `extra_rows` (short rows that `detect_lines` dropped, such as
    a single short footnote, without the page number) count for that check but not for the
    text-block width. The candidates are then split by mean stroke thickness t = area / width
    (a closed text line is ~0.7 × the line height thick):

    - strict: t ≤ max(3, 0.3 × `med_h`), as before D68;
    - thick (D68): max(3, 0.3 × `med_h`) < t ≤ `NEAR_RULE_THICKNESS` × `med_h`, with a fill
      (area / box) of at least `RULE_MIN_FILL`, so a closed text row (fill ≤ 0.56) stays refused.

    The widest strict candidate wins; only when there is none does the widest thick one win (a
    single "widest wins" over both kinds would move a table page's thin rule to a thicker bar).
    """
    h, w = gray.shape
    if h == 0 or w == 0:
        return None
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kx = max(15, int(RULE_CLOSE_FRAC * w)) | 1
    closed = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (kx, 1)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
    max_thickness = max(3.0, 0.3 * med_h) if med_h else 3.0
    max_thick = NEAR_RULE_THICKNESS * med_h if med_h else 0.0
    min_width = max(20, int(RULE_MIN_WIDTH_FRAC * _text_block_width(lines, w)))
    text = list(lines) + list(extra_rows or [])
    strict = None
    thick = None
    for i in range(1, n):
        x, y, cw, ch, area = (int(v) for v in stats[i])
        if cw < min_width or y < 0.4 * h or ch > max(12, 0.06 * h):
            continue
        thickness = area / cw
        if thickness <= max_thickness:
            kind = "strict"
        elif thickness <= max_thick and area / (cw * ch) >= RULE_MIN_FILL:
            kind = "thick"
        else:
            continue
        y0, y1 = y, y + ch
        below = [ln for ln in text if ln["y0"] >= y1 - 2]
        above = [ln for ln in text if ln["y1"] <= y0 + 2]
        if not (below and above) or y1 >= 0.97 * h:
            continue
        candidate = ((y0 + y1) // 2, cw)
        if kind == "strict":
            if strict is None or cw > strict[1]:
                strict = candidate
        elif thick is None or cw > thick[1]:
            thick = candidate
    best = strict if strict is not None else thick
    return None if best is None else int(best[0])


def measure_line_sizes(ink: np.ndarray, lines: list[LineBox]) -> list[LineBox]:
    """Copies of `lines` with a `size` key: the type size of each line, from its glyphs.

    The projection-profile boxes of `detect_lines` only cover the dense core of a line, whose
    height barely changes with the type size; the 75th percentile of the heights of the connected
    components whose centre lies nearest to a line (within half a line pitch) follows the type
    size, so smaller footnote type shows up. A line without components keeps its box height.
    """
    out = [dict(ln) for ln in lines]
    if not lines:
        return out
    centres = np.array([(ln["y0"] + ln["y1"]) / 2 for ln in lines], dtype=np.float32)
    if len(lines) > 1:
        pitch = float(np.median(np.diff(centres)))
    else:
        pitch = 3.0 * float(lines[0]["y1"] - lines[0]["y0"])
    n, _, stats, _ = cv2.connectedComponentsWithStats((ink > 0).astype(np.uint8), connectivity=8)
    heights: list[list[int]] = [[] for _ in lines]
    for i in range(1, n):
        cy = stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT] / 2
        j = int(np.argmin(np.abs(centres - cy)))
        if abs(centres[j] - cy) < 0.5 * pitch:
            heights[j].append(int(stats[i, cv2.CC_STAT_HEIGHT]))
    for ln, hs in zip(out, heights, strict=True):
        ln["size"] = int(round(float(np.percentile(hs, 75)))) if hs else int(ln["y1"] - ln["y0"])
    return out


def _line_size(ln: LineBox) -> float:
    return float(ln.get("size", ln["y1"] - ln["y0"]))


def median_line_size(lines: list[LineBox]) -> float:
    """Median `size` (type size) of `lines`, falling back to the box heights; 0 without lines."""
    return float(np.median([_line_size(ln) for ln in lines])) if lines else 0.0


def detect_footnote_block(lines: list[LineBox], median_h: float, height: int) -> int | None:
    """Top y of a block of smaller type at the bottom of the page (footnotes without a rule), or None.

    Scanning from the last line upwards, the block is the run of lines whose height (their
    `size` when `measure_line_sizes` set it, else the box height) is at most 0.8 × the body
    median `median_h`. It counts when it has at least two lines, is separated from the line above
    by a gap of at least 1.2 × the median line pitch and starts in the lower 45% of the page.
    Returns the block top minus a small pad (never above the previous line). The page number, if
    one was found, must already be removed from `lines`.
    """
    if len(lines) < 3 or not median_h or height <= 0:
        return None
    ordered = sorted(lines, key=lambda ln: ln["y0"])
    pitch = float(np.median(np.diff([ln["y0"] for ln in ordered])))
    limit = BLOCK_MAX_SIZE_RATIO * median_h
    i = len(ordered)
    while i > 0 and _line_size(ordered[i - 1]) <= limit:
        i -= 1
    block = ordered[i:]
    if len(block) < BLOCK_MIN_LINES or i == 0:
        return None
    top, above = block[0], ordered[i - 1]
    if top["y0"] - above["y1"] < BLOCK_MIN_GAP_PITCH * pitch:
        return None
    if top["y0"] < (1.0 - BLOCK_LOWER_FRAC) * height:
        return None
    pad = max(2, int(round(0.5 * median_h)))
    return int(max(above["y1"] + 1, top["y0"] - pad))


def text_rows(ink: np.ndarray, type_size: float) -> list[LineBox]:
    """Every horizontal band that holds glyph-sized ink (a low-threshold profile), merged over small gaps.

    Unlike `detect_lines` this keeps short, sparse rows such as a centred page number; it is only
    used to look for such rows beyond the detected lines. Components taller than three type sizes
    (page borders, fold lines, scan-edge slivers) are ignored so they do not join every row into
    one. A band is split where its ink leaves a horizontal gap of more than three type sizes, so a
    number in a corner is not joined to a margin note or a stamp at the other end of the row.
    Each row carries its `area` (ink pixels), `glyph_w`: the extent of the columns that hold a
    glyph-high stroke, which leaves out the dashes of «— ٨ —», and `glyphs`: how many
    digit-shaped components (at least 0.3 type size high, at most 1.5 type sizes wide) it
    holds, which a scan-edge smear or a stain does not.
    """
    h, w = ink.shape
    if h == 0 or w == 0 or not type_size:
        return []
    n, labels, stats, _ = cv2.connectedComponentsWithStats((ink > 0).astype(np.uint8), connectivity=8)
    keep = np.zeros(n, dtype=bool)
    keep[1:] = stats[1:, cv2.CC_STAT_HEIGHT] <= 3 * type_size
    mask = keep[labels]
    prof = mask.sum(axis=1)
    merge_gap = max(2.0, 0.25 * type_size)
    merged: list[list[int]] = []
    for a, b in _runs(prof >= 2):
        if merged and a - merged[-1][1] < merge_gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    stroke = max(2, int(round(0.25 * type_size)))
    centres = stats[:, cv2.CC_STAT_TOP] + stats[:, cv2.CC_STAT_HEIGHT] / 2
    glyph_like = (
        keep
        & (stats[:, cv2.CC_STAT_HEIGHT] >= 0.3 * type_size)
        & (stats[:, cv2.CC_STAT_WIDTH] <= 1.5 * type_size)
    )
    lefts = stats[:, cv2.CC_STAT_LEFT]
    split_gap = CLUSTER_GAP_RATIO * type_size
    rows: list[LineBox] = []
    for a, b in merged:
        band = mask[a:b]
        col_all = band.sum(axis=0)
        in_band = glyph_like & (centres >= a) & (centres < b)
        for c0, c1 in _clusters(np.where(col_all > 0)[0], split_gap):
            # the cluster's own vertical runs: ink elsewhere in the band (a tall mark) must not
            # stretch a small number up to the specks under the last line
            for r0, r1 in _clusters(np.where(band[:, c0:c1].any(axis=1))[0], merge_gap):
                sub = band[r0:r1]
                cols = np.where(sub.any(axis=0))[0]
                cols = cols[(cols >= c0) & (cols < c1)]
                if cols.size == 0:
                    continue
                x0, x1 = int(cols[0]), int(cols[-1]) + 1
                col = sub[:, x0:x1].sum(axis=0)
                tall = np.where(col >= stroke)[0]
                glyph_w = int(tall[-1] - tall[0] + 1) if tall.size else int(x1 - x0)
                ya, yb = a + r0, a + r1
                inside = in_band & (lefts >= x0) & (lefts < x1) & (centres >= ya) & (centres < yb)
                rows.append(
                    {
                        "x0": x0,
                        "y0": int(ya),
                        "x1": x1,
                        "y1": int(yb),
                        "area": int(col.sum()),
                        "glyph_w": glyph_w,
                        "glyphs": int(inside.sum()),
                    }
                )
    return rows


def _clusters(idx: np.ndarray, gap: float) -> list[tuple[int, int]]:
    """Runs `(start, end_exclusive)` of the sorted indices `idx`, split where they jump by more than `gap`."""
    if idx.size == 0:
        return []
    breaks = np.where(np.diff(idx) > gap)[0]
    starts = [int(idx[0])] + [int(idx[i + 1]) for i in breaks]
    ends = [int(idx[i]) + 1 for i in breaks] + [int(idx[-1]) + 1]
    return list(zip(starts, ends, strict=True))


def page_number_candidates(ink: np.ndarray, lines: list[LineBox], type_size: float) -> list[LineBox]:
    """`lines` plus the ink rows above the first and below the last detected line, in page order.

    `detect_lines` drops rows whose profile is weak, which is exactly what a short page number
    such as «— ٨ —» is; this adds them back for `detect_page_number` (the stored line boxes stay
    as they are). `type_size` is `median_line_size` of the page.
    """
    if not lines:
        return []
    return sorted([dict(ln) for ln in lines] + edge_rows(ink, lines, type_size), key=lambda ln: ln["y0"])


def edge_rows(ink: np.ndarray, lines: list[LineBox], type_size: float) -> list[LineBox]:
    """`text_rows` of the page strips above the first and below the last detected line.

    Each strip is scanned on its own, so ink that touches the outermost line (a stray dot, a
    handwritten mark) cannot join a page number to that line. Boxes are in page coordinates.
    """
    if not lines:
        return []
    first = min(ln["y0"] for ln in lines)
    last = max(ln["y1"] for ln in lines)
    rows = text_rows(ink[:first], type_size)
    for r in text_rows(ink[last:], type_size):
        rows.append({**r, "y0": r["y0"] + last, "y1": r["y1"] + last})
    return rows


def _is_page_number(ln: LineBox, block_w: int, median_h: float) -> bool:
    """Shape test of a page-number line: short, not taller than type, not a solid blob, not a speck."""
    lw, lh = ln["x1"] - ln["x0"], ln["y1"] - ln["y0"]
    if ln.get("glyph_w", lw) > PN_MAX_WIDTH_FRAC * block_w:
        return False
    if lh > PN_MAX_HEIGHT_RATIO * median_h or lh < 0.3 * median_h:
        return False
    if ln.get("glyphs", 1) < 1:  # a smear or a lone dash: no digit-shaped component
        return False
    area = ln.get("area")
    # a filled blob (a punched hole, a stain) is not a number; digits and dashes are sparse
    return area is None or area / max(1, lw * lh) < 0.6


def _is_text_line(ln: LineBox, block_w: int, median_h: float) -> bool:
    """A full text line: stops the walk from the page edge (nothing beyond it is a page number)."""
    return (ln["x1"] - ln["x0"]) >= 0.5 * block_w and (ln["y1"] - ln["y0"]) >= 0.5 * median_h


def detect_page_number(lines: list[LineBox], width: int, height: int, median_h: float) -> dict | None:
    """The printed page number: `{"bbox": [x0, y0, x1, y1], "position": "top"|"bottom"}` or None.

    Candidate is the last line (bottom, checked first) or the first line (top): short (at most 15%
    of the text-block width), not taller than 1.4 × `median_h`, separated from its neighbour
    towards the text by at least 0.8 × `median_h`, and inside the bottom 15% / top 12% of the
    page. Walking in from the page edge, specks and scan-edge smears (rows that are neither a
    page number nor a full text line) are skipped; the first full text line ends the search.
    """
    if not lines or not median_h or height <= 0:
        return None
    ordered = sorted(lines, key=lambda ln: ln["y0"])
    text_lines = [ln for ln in ordered if _is_text_line(ln, width, median_h)]
    block_w = _text_block_width(text_lines or ordered, width)
    gap_min = PN_MIN_GAP_RATIO * median_h

    def search(indices: list[int], position: str) -> dict | None:
        for k, i in enumerate(indices):
            ln = ordered[i]
            in_zone = (
                ln["y0"] >= (1.0 - PN_BOTTOM_FRAC) * height
                if position == "bottom"
                else ln["y1"] <= PN_TOP_FRAC * height
            )
            if not in_zone or _is_text_line(ln, block_w, median_h):
                return None
            if _is_page_number(ln, block_w, median_h):
                # the neighbour towards the text: the next row that does not share ln's band
                beyond = [
                    ordered[j]
                    for j in indices[k + 1 :]
                    if ordered[j]["y1"] <= ln["y0"] or ordered[j]["y0"] >= ln["y1"]
                ]
                if not beyond:
                    return None
                neighbour = beyond[0]
                gap = ln["y0"] - neighbour["y1"] if position == "bottom" else neighbour["y0"] - ln["y1"]
                if gap >= gap_min:
                    bbox = [int(ln["x0"]), int(ln["y0"]), int(ln["x1"]), int(ln["y1"])]
                    return {"bbox": bbox, "position": position}
        return None

    idx = list(range(len(ordered)))
    return search(idx[::-1], "bottom") or search(idx, "top")


def _is_short_text_row(row: LineBox, lines: list[LineBox], type_size: float) -> bool:
    """A row beyond the detected lines that is text (a short last line), not a number or a speck."""
    width = row["x1"] - row["x0"]
    height = row["y1"] - row["y0"]
    return (
        width > PN_MAX_WIDTH_FRAC * _text_block_width(lines, width)
        and row.get("glyphs", 0) >= 3
        and 0.5 * type_size <= height <= 3 * type_size
    )


def _overlaps(ln: LineBox, box: Box) -> bool:
    return ln["y0"] < box[3] and ln["y1"] > box[1] and ln["x0"] < box[2] and ln["x1"] > box[0]


def binarize_sauvola(gray: np.ndarray, window: int, k: float) -> np.ndarray:
    """Sauvola black-and-white image (255 = paper) for Tesseract and display."""
    thr = threshold_sauvola(gray, window_size=_odd(window), k=k)
    return np.where(gray > thr, 255, 0).astype(np.uint8)


def sauvola_window_for(median_line_height: float) -> int:
    """PoC rule: about two line heights, clamped to 25..75 and odd; 41 when no lines were found."""
    if not median_line_height:
        return 41
    return int(np.clip(2 * median_line_height + 1, 25, 75)) | 1


# ---------------------------------------------------------------- edge strips (D18)


def _median_word_gap(ink: np.ndarray, lines: list[LineBox], med_h: float) -> float:
    """Median horizontal gap between words inside the detected lines (0 when it cannot be measured).

    Gaps narrower than a fifth of the line height are letter gaps, not word gaps, and are ignored.
    """
    gaps: list[int] = []
    min_gap = max(2, int(0.2 * med_h)) if med_h else 2
    for ln in lines:
        band = ink[ln["y0"] : ln["y1"], ln["x0"] : ln["x1"]]
        if band.size == 0:
            continue
        empty = ~(band > 0).any(axis=0)
        for a, b in _runs(empty):
            if a > 0 and b < empty.size and (b - a) >= min_gap:
                gaps.append(b - a)
    return float(np.median(gaps)) if gaps else 0.0


def remove_edge_strips(
    ink: np.ndarray,
    gray: np.ndarray,
    max_band_frac: float = 0.12,
    gap_frac: float = 0.04,
    word_gap_factor: float = 3.0,
) -> tuple[np.ndarray, list[dict]]:
    """Whiten narrow ink bands at the left/right edge that belong to the facing page (D18).

    The vertical projection of the cleaned ink mask is split into bands separated by empty gaps
    of at least `gap_frac` of the width, or `word_gap_factor` × the median inter-word gap when
    that is smaller. Starting at each scan edge and walking inwards, bands narrower than
    `max_band_frac` of the width are strips and are painted white in `gray`; the first wide band
    (the main text block or a second column) stops the walk on that side, so nothing that lies
    between two text columns is ever removed.

    Returns a copy of `gray` and the removed strips as `{"side", "x0", "x1", "kind"}` in its
    coordinates, `kind` being `text` (facing-page fragments or any glyph-sized ink such as a
    page number in the outer margin: worth a look) or `artifact` (a tall continuous scan-edge
    sliver or fold line). `ink` is not modified; callers blank the same columns in their mask.
    """
    h, w = ink.shape
    out = gray.copy()
    col_ink = (ink > 0).sum(axis=0)
    inked = col_ink >= max(2, int(0.002 * h))
    if not inked.any():
        return out, []

    lines, med_h = detect_lines(ink)
    word_gap = _median_word_gap(ink, lines, med_h)
    gap_threshold = int(gap_frac * w)
    if word_gap > 0:
        by_words = max(word_gap_factor * word_gap, 0.75 * med_h)
        gap_threshold = int(min(gap_threshold, by_words))
    gap_threshold = max(gap_threshold, 4)

    bands: list[list[int]] = []
    for a, b in _runs(inked):
        if bands and a - bands[-1][1] < gap_threshold:
            bands[-1][1] = b
        else:
            bands.append([a, b])
    if len(bands) < 2:
        return out, []

    main = int(np.argmax([col_ink[a:b].sum() for a, b in bands]))
    max_band = max_band_frac * w
    strips: list[dict] = []
    # From the scan edge inwards on each side; the main band itself always stops the walk.
    for side, candidates in (("left", bands[:main]), ("right", list(reversed(bands[main + 1 :])))):
        for a, b in candidates:
            if b - a >= max_band:
                break  # a real column of text: nothing further in is an edge strip
            pad = 2
            x0, x1 = max(0, a - pad), min(w, b + pad)
            # Facing-page fragments alternate with the line gaps (many ink runs down the column)
            # and a stray page number is a short run; only a tall continuous run (a scan-edge
            # sliver or fold line) is an artifact. Everything else is flagged for a look.
            runs = _runs((ink[:, a:b] > 0).any(axis=1))
            tallest = max((r1 - r0 for r0, r1 in runs), default=0)
            kind = STRIP_ARTIFACT if len(runs) < 4 and tallest >= ARTIFACT_MIN_HEIGHT_FRAC * h else STRIP_TEXT
            out[:, x0:x1] = 255
            strips.append({"side": side, "x0": int(x0), "x1": int(x1), "kind": kind})
    strips.sort(key=lambda s: s["x0"])
    return out, strips


# ---------------------------------------------------------------- pipeline


def run_pipeline(gray: np.ndarray, params: PreprocessParams | None = None) -> PreprocessResult:
    """Run the whole preprocessing stage on a grayscale page.

    Detection always runs so that `auto_params` reflects what the pipeline would have chosen;
    manual values in `params` then replace the detected angle, crop box, Sauvola window/k and
    denoise strength. A manual crop box disables edge-strip removal (the user decides the crop);
    the strips are still detected so `auto_params` (`crop_box`, `edge_strips`) describe the
    automatic run, while `edge_strips_removed` lists only what was actually whitened.
    """
    p = params or PreprocessParams()
    if gray.ndim != 2:
        raise ValueError("run_pipeline expects a 2-D grayscale image")
    if gray.dtype != np.uint8:
        gray = np.clip(gray, 0, 255).astype(np.uint8)

    gray1, border_crop = remove_dark_borders(gray)
    gray2, bg_kernel = flatten_background(gray1)
    nlm_h = int(p.nlm_h) if p.nlm_h is not None else DEFAULT_NLM_H
    gray3 = denoise(gray2, nlm_h)

    min_area = max(4, int(gray3.size * 1e-5))
    auto_angle, skew_conf = estimate_skew(binarize_clean(gray3, min_area=min_area), p.max_skew_deg)
    angle = float(p.angle) if p.angle is not None else auto_angle
    gray4 = rotate(gray3, angle)
    frame_h, frame_w = gray4.shape

    ink = binarize_clean(gray4, min_area=min_area)
    auto_strips: list[dict] = []
    auto_ink = ink
    if p.remove_edge_strips:
        stripped, auto_strips = remove_edge_strips(ink, gray4)
        if auto_strips:
            auto_ink = ink.copy()
            for s in auto_strips:
                auto_ink[:, s["x0"] : s["x1"]] = 0
        if p.crop_box is None:
            gray4, ink = stripped, auto_ink
    strips = auto_strips if p.crop_box is None else []
    auto_crop = content_bbox(auto_ink, p.crop_margin_frac)
    crop_box = auto_crop
    if p.crop_box is not None:
        try:
            crop_box = clamp_box(p.crop_box, frame_w, frame_h)
        except ValueError:
            crop_box = auto_crop
    x0, y0, x1, y1 = crop_box
    gray_c = np.ascontiguousarray(gray4[y0:y1, x0:x1])
    ink_c = ink[y0:y1, x0:x1]

    lines, med_h = detect_lines(ink_c)
    sized = measure_line_sizes(ink_c, lines)
    type_size = median_line_size(sized)
    candidates = page_number_candidates(ink_c, lines, type_size)
    page_number = detect_page_number(candidates, gray_c.shape[1], gray_c.shape[0], type_size)
    pn_box = page_number["bbox"] if page_number else None
    short_text = [
        r
        for r in candidates
        if "glyphs" in r and _is_short_text_row(r, lines, type_size) and not (pn_box and _overlaps(r, pn_box))
    ]
    rule_y = detect_footnote_rule(gray_c, lines, med_h, extra_rows=short_text)
    body_lines = [ln for ln in sized if not (pn_box and _overlaps(ln, pn_box))]
    block_y = None if rule_y is not None else detect_footnote_block(body_lines, type_size, gray_c.shape[0])

    auto_window = sauvola_window_for(med_h)
    window = _odd(p.sauvola_window) if p.sauvola_window else auto_window
    k = float(p.sauvola_k) if p.sauvola_k is not None else DEFAULT_SAUVOLA_K
    bw = binarize_sauvola(gray_c, window, k)

    flags: list[str] = []
    if abs(angle) > LARGE_SKEW_DEG:
        flags.append(FLAG_LARGE_SKEW)
    if p.angle is None and skew_conf < LOW_SKEW_CONFIDENCE:
        flags.append(FLAG_LOW_CONFIDENCE)
    if not lines:
        flags.append(FLAG_NO_LINES)
    if any(s["kind"] == STRIP_TEXT for s in strips):
        flags.append(FLAG_EDGE_STRIP)

    auto_params = {
        "angle": auto_angle,
        "skew_confidence": skew_conf,
        "crop_box": [int(v) for v in auto_crop],
        "edge_strips": auto_strips,
        "sauvola_window": int(auto_window),
        "sauvola_k": DEFAULT_SAUVOLA_K,
        "nlm_h": DEFAULT_NLM_H,
        "border_crop": [int(v) for v in border_crop],
        "frame": [int(frame_w), int(frame_h)],
    }
    return PreprocessResult(
        gray=gray_c,
        bw=bw,
        angle=round(float(angle), 2),
        skew_confidence=float(skew_conf),
        border_crop=[int(v) for v in border_crop],
        crop_box=[int(v) for v in crop_box],
        bg_kernel=int(bg_kernel),
        nlm_h=nlm_h,
        sauvola_window=int(window),
        sauvola_k=k,
        output_width=int(gray_c.shape[1]),
        output_height=int(gray_c.shape[0]),
        line_boxes=lines,
        median_line_height=float(med_h),
        n_lines=len(lines),
        footnote_rule_y=rule_y,
        footnote_block_y=block_y,
        page_number_box=page_number,
        edge_strips_removed=strips,
        flags=flags,
        auto_params=auto_params,
    )
