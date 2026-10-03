"""OCR services: fast provisional text, full dual-model OCR, selection and line building.

Flow per page (spec §6-§7):

    run_fast_ocr    Tesseract on every region (B&W crops) → OcrRuns with word boxes → provisional_text.
                    Printed lines Tesseract dropped are read again one by one (`rescue_lines`).
                    Born-digital books with `use_text_layer`: the repaired text layer becomes the
                    final text at once (Tesseract still runs for geometry) and the page is finalised.
    run_full_ocr    primary and secondary Qari on the OCR-able regions (gray crops, footnotes at 2x,
                    running header / page number skipped) → OcrRuns → finalize_page.
    finalize_page   per region, picks the text from the latest runs (primary → secondary → Tesseract
                    fallback with the `ocr_fallback` flag, D16), anchors the tokens to the printed lines
                    Kraken read (their words' boxes, D92; Tesseract's lines without Kraken, D12), stores
                    Line rows, `final_text` (Western digits, D6), `ocr_done` (`compose_page` builds the
                    same result in memory without saving it).

All coordinates stored on runs and lines are in gray-image pixel space.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models.fields.files import FieldFile

import numpy as np
from PIL import Image

from books.models import Book, Page
from core.arabic import normalize, normalize_ws, parse_output, to_western_digits
from core.images import crop, load_gray, to_png_bytes
from processing.models import Preprocess, Region

from . import boxes, chooser, flags
from .alignment import build_lines, merged_lines, word_f1
from .engines import registry
from .engines.base import OcrEngine, OcrResult
from .engines.pdf_text import PdfPageRef
from .engines.qari import max_new_tokens_for
from .models import Line, OcrRun, TextGap

log = logging.getLogger(__name__)

SKIPPED_KINDS: frozenset[str] = frozenset({Region.Kind.RUNNING_HEADER, Region.Kind.PAGE_NUMBER})
FOOTNOTE_KINDS: frozenset[str] = frozenset({Region.Kind.FOOTNOTE})
PAGE_KIND = "page"
PAGE_SCOPE = "page"
REGION_SCOPE = "region"
FLAG_FALLBACK = "ocr_fallback"
FLAG_ALIGNMENT = "alignment_poor"
FLAG_MERGED = "lines_merged"
FLAG_SINGLE = "single_reader"  # D73: a region read by one model (or Tesseract alone)
FLAG_MISSING = "missing_text"  # D72: words only the second model read (a group, a suggestion)
FLAG_UNREADABLE = "no_readable_text"  # D89: no text is left: Tesseract's reading of a photo or ornament
# D89: Tesseract's text of a region both models failed on is no text when its words are unsure (mean
# confidence), half Latin letters (an Arabic book's photo read as English), or letters of an ornament.
TESS_UNREADABLE_CONF = 50.0
TESS_UNREADABLE_LATIN = 0.5
ORNAMENT_LETTERS = frozenset("هاودةءأ")  # dots and flourishes read as letters («ههه ها هد واه»)
ORNAMENT_SHARE = 0.9
ORNAMENT_MIN_LETTERS = 20
_LATIN_LETTER = re.compile(r"[A-Za-z]")
# D90: a region both models failed on is read again in pieces of a few printed lines before Tesseract's text
# stands for it (a loop comes with length: book 31's full pages of vowelled hadith and commentary).
PIECE_LINES = 4  # printed lines (`Preprocess.line_boxes`) per piece
PIECE_MIN_BANDS = 5  # a region of fewer lines is not cut
PIECE_MIN_CONF = 40.0  # under this mean Tesseract confidence the region is a picture (photos: 22–36)
# How a page was read (`Page.reading["readers"]`, D73), weakest last.
READERS_TWO = "two"
READERS_ONE = "one"
READERS_TESSERACT = "tesseract"
_READERS_RANK = {READERS_TWO: 0, READERS_ONE: 1, READERS_TESSERACT: 2}
TEXT_LAYER_SOURCE = "pdf_text"

# A page-number line: only digits (Western, Arabic-Indic, Persian), dashes, dots, brackets, spaces.
_PN_CHARS = r"\s0-9٠-٩۰-۹\-‐‑‒–—―ـ.·•…()\[\]{}﴾﴿<>«»"
_PAGE_NUMBER_LINE = re.compile(rf"^[{_PN_CHARS}]*[0-9٠-٩۰-۹][{_PN_CHARS}]*$")
_PAGE_NUMBER_MARKS = re.compile(rf"^[{_PN_CHARS}]+$")  # a line of those characters (with or without a digit)
PN_EDGE_MARKS = 2  # lines of marks alone between a page-number line and the page's edge dropped with it (D92)
_DIGITS = re.compile(r"[0-9٠-٩۰-۹]+")
_ARABIC_LETTER = re.compile(r"[\u0621-\u063A\u0641-\u064A\u0671-\u06D3]")
PRINTED_NUMBER_MAX = 20
# A page-number region whose readings hold no digit and at least this many Arabic letters (in a model
# reading) holds words, a short last line of text, not a number (see `reads_as_words`).
MIN_WORD_LETTERS = 2

# Sanity thresholds (D16). A small absolute slack on the upper bound keeps tiny regions (a two-word
# heading read as five words) from failing on the ratio check alone.
MIN_WORD_RATIO = 0.2
MAX_WORD_RATIO = 1.8
WORD_SLACK = 3
MIN_WORD_F1 = 0.45
# Decision (Phase 2 review, extends D16): Tesseract is only a reliable yardstick on regions with some
# text. Below MIN_REF_WORDS_FOR_OVERLAP reference words (two-line footnotes of small type read from
# the 1x B&W crop) exact-token F1 is dominated by Tesseract's noise, so only a generous length check
# remains (runaway output: more than SHORT_REF_MAX_RATIO x the reference + SHORT_REF_SLACK words). An
# empty reference proves nothing: it is inconclusive (`no_reference` fails) unless the output is at
# most NO_REF_MAX_WORDS words; `select_text` still accepts the primary when the two models agree
# (word F1 >= MODELS_AGREE_F1), which is D16's own confidence signal. The loop check always applies.
MIN_REF_WORDS_FOR_OVERLAP = 15
SHORT_REF_MAX_RATIO = 3
SHORT_REF_SLACK = 10
NO_REF_MAX_WORDS = 3
MODELS_AGREE_F1 = 0.6
# Failure reasons that only mean "differs from Tesseract" (not a broken run): agreement can override.
COMPARISON_REASONS: frozenset[str] = frozenset({"no_reference", "too_short", "too_long", "low_overlap"})
# Below this share of tokens anchored to a Tesseract word the page gets the `alignment_poor` flag.
MIN_ANCHOR_RATIO = 0.5

ENGINE_LABELS: dict[str, str] = {
    "qari_v03": "Qari v0.3",
    "qari_v02": "Qari v0.2",
    "tesseract": "Tesseract",
    "kraken": "Kraken",
    "pdf_text": "طبقة النص (PDF)",
    "fake": "محرّك تجريبي",
}
VARIANT_LABELS: dict[str, str] = {
    "gray": "رمادية",
    "bw": "أبيض وأسود",
    "gray_2x": "رمادية ×2",
    "pdf": "ملف PDF",
    "ingest": "نص الاستيراد",
}


class OcrError(Exception):
    """Raised with an Arabic, actionable message when a page cannot be OCR'd (stored on the page)."""


@dataclass
class Target:
    """What one engine call looks at: a region, or the whole page when the page has no regions."""

    region: Region | None
    bbox: list[int]

    @property
    def kind(self) -> str:
        return self.region.kind if self.region is not None else PAGE_KIND

    @property
    def scope(self) -> str:
        return REGION_SCOPE if self.region is not None else PAGE_SCOPE

    @property
    def origin(self) -> tuple[int, int]:
        return int(self.bbox[0]), int(self.bbox[1])


@dataclass
class RegionText:
    """The text chosen for one target and what it is built from (`alt_partial`: `alt_text` is the clean
    start of the other model's looped run, D73)."""

    target: Target
    text: str
    alt_text: str | None
    tess_lines: list[dict]
    fallback: bool
    reason: str
    source: str
    alt_partial: bool = False
    unreadable: str = ""  # D89: why Tesseract's text of a failed region was dropped ('' when it was not)
    # D92: Kraken's lines of the region, which give the words their boxes (None: Tesseract's do)
    box_lines: list[dict] | None = None


@dataclass
class Selection:
    """What `select_reading` chose for one region (`select_text` gives the first five as a tuple)."""

    text: str
    alt: str | None
    fallback: bool
    reason: str
    source: str
    alt_partial: bool = False
    unreadable: str = ""


# ---------------------------------------------------------------- settings and inputs


def nassakh() -> dict:
    """`settings.NASSAKH`, read at call time so `override_settings` works in tests."""
    return settings.NASSAKH


def engine_names() -> tuple[str, str, str]:
    """(primary, secondary, fast) engine names from settings."""
    cfg = nassakh()
    return str(cfg["OCR_PRIMARY"]), str(cfg["OCR_SECONDARY"]), str(cfg.get("OCR_FAST", "tesseract"))


def uses_text_layer(page: Page) -> bool:
    """True for pages of born-digital books whose owner kept `use_text_layer` on, while the text layer is
    enabled (`NASSAKH["TEXT_LAYER"]`, off by default: item 20); off, every page is read by OCR."""
    from books.services import book_uses_text_layer  # the books app owns the rule (lazy: no import cycle)

    return book_uses_text_layer(page.book)


def _preprocess_of(page: Page) -> Preprocess:
    """The page's Preprocess row; OcrError (Arabic) when the page was never preprocessed."""
    try:
        return page.preprocess
    except ObjectDoesNotExist:
        raise OcrError("لم تُجهَّز هذه الصفحة بعد؛ شغّل «تجهيز الصفحات» أولًا.") from None


def _load_field_image(field: FieldFile, label: str) -> np.ndarray:
    """Read a stored image field as a 2-D uint8 array, with an Arabic error when it is missing."""
    if not field or not field.name:
        raise OcrError(f"الصورة {label} غير متوفرة؛ أعد تجهيز الصفحة.")
    try:
        with field.open("rb") as fh:
            return load_gray(fh)
    except FileNotFoundError:
        raise OcrError(f"ملف الصورة {label} مفقود من التخزين؛ أعد تجهيز الصفحة.") from None


def _targets(page: Page, shape: tuple[int, ...], ocr_only: bool) -> list[Target]:
    """Regions in order (OCR-able ones only when `ocr_only`), or one page-level target without regions."""
    h, w = int(shape[0]), int(shape[1])
    regions = list(page.regions.order_by("order", "id"))
    if not regions:
        return [Target(None, [0, 0, w, h])]
    if ocr_only:
        regions = [r for r in regions if r.kind not in SKIPPED_KINDS]
    return [Target(r, [int(round(float(v))) for v in r.bbox]) for r in regions]


def _page_shape(page: Page) -> tuple[int, int]:
    """(height, width) of the gray image from the Preprocess row (0, 0 when unknown)."""
    try:
        pre = page.preprocess
    except ObjectDoesNotExist:
        return 0, 0
    return int(pre.output_height), int(pre.output_width)


# ---------------------------------------------------------------- images


PAGE_NUMBER_UPSCALE = 4  # Lanczos factor for the page-number crop before Tesseract
PAGE_NUMBER_PAD = 8  # pixels of white kept around the digits (gray-image space)
PAGE_NUMBER_PSM = 7  # Tesseract "single text line"


def _pad_bbox(bbox: list[int], pad: int, shape: tuple[int, ...]) -> list[int]:
    """Grow `bbox` by `pad` pixels on every side, clamped to an image of `shape` (h, w, ...)."""
    h, w = int(shape[0]), int(shape[1])
    x0, y0, x1, y1 = (int(v) for v in bbox)
    return [max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)]


def _crop_image(array: np.ndarray, bbox: list[int], upscale: int = 1) -> np.ndarray:
    """Crop `bbox` out of `array`; upscale with Lanczos when `upscale > 1` (footnotes, D11)."""
    part = crop(array, bbox)
    if part.size == 0:
        raise OcrError("منطقة فارغة خارج حدود الصورة؛ راجع تخطيط هذه الصفحة.")
    if upscale > 1:
        img = Image.fromarray(part)
        img = img.resize((img.width * upscale, img.height * upscale), Image.LANCZOS)
        part = np.asarray(img, dtype=np.uint8)
    return part


def _save_temp(array: np.ndarray, directory: Path, stem: str) -> Path:
    """Write `array` as `<directory>/<stem>.png` for an engine that reads files; returns the path."""
    path = directory / f"{stem}.png"
    path.write_bytes(to_png_bytes(array))
    return path


def _offset_lines(lines: list[dict], dx: int, dy: int, scale: float = 1.0) -> list[dict]:
    """Move Tesseract boxes from crop space into gray-image space (undoing an upscale if any)."""

    def shift(bbox: list[int] | None) -> list[int] | None:
        if not bbox:
            return bbox
        x0, y0, x1, y1 = bbox
        return [
            int(round(x0 / scale + dx)),
            int(round(y0 / scale + dy)),
            int(round(x1 / scale + dx)),
            int(round(y1 / scale + dy)),
        ]

    out = []
    for line in lines:
        words = [{**w, "bbox": shift(w.get("bbox"))} for w in line.get("words", [])]
        out.append({**line, "bbox": shift(line.get("bbox")), "words": words})
    return out


def _json_safe(value: object) -> object:
    """Round-trip through JSON so numpy scalars, Paths and the like become plain JSON types."""
    return json.loads(json.dumps(value, default=str))


# ---------------------------------------------------------------- engine calls


def run_engine(
    page: Page,
    engine_name: str,
    target: Target,
    source: Path | PdfPageRef,
    input_variant: str,
    max_new_tokens: int | None = None,
    scale: float = 1.0,
    hints: dict | None = None,
) -> OcrRun:
    """Call `engine_name` on `source` (image path or PdfPageRef) and store the call as an OcrRun.

    Failures are stored too (`status=error`) so the page keeps a trace of every attempt. Tesseract
    word/line boxes from `result.extra["lines"]` are moved into gray-image coordinates.
    """
    engine = registry.get_engine(engine_name)
    params: dict = {"scope": target.scope, "kind": target.kind}
    if max_new_tokens:
        params["max_new_tokens"] = max_new_tokens
    if hints:
        params["hints"] = dict(hints)
    generation_params = getattr(engine, "generation_params", None)
    if callable(generation_params):
        params.update(generation_params(max_new_tokens or getattr(engine, "default_max_new_tokens", 0)))
    run = OcrRun(
        page=page,
        region=target.region,
        engine_name=engine_name[:40],
        model_id=(engine.model_id or "")[:200],
        model_revision=(engine.model_revision or "")[:64],
        backend=(engine.backend or "")[:20],
        prompt=engine.prompt or "",
        input_variant=input_variant[:20],
        params=params,
    )
    try:
        result = engine.recognize(source, max_new_tokens, hints=hints)
    except Exception as exc:  # noqa: BLE001 - recorded on the run, the page decides what to do
        log.exception("%s failed on page %s (%s)", engine_name, page.pk, target.kind)
        run.status = OcrRun.Status.ERROR
        run.error = f"{type(exc).__name__}: {exc}"[:2000]
        run.save()
        return run
    _fill_run(run, result, engine, target.origin, scale)
    run.save()
    return run


def _read_models(
    page: Page,
    names: Sequence[str],
    target: Target,
    source: Path,
    input_variant: str,
    max_new_tokens: int,
    scale: float = 1.0,
) -> list[OcrRun]:
    """Every engine of `names` reads the same crop: one OcrRun per engine, in the order of `names`. Engines
    that read together (both Qari models behind one Runpod endpoint) answer from one request
    (`registry.together`); local engines read one after the other as before."""
    trace = {
        "book": page.book_id,
        "page": page.number,
        "page_id": page.pk,
        "region": target.kind,
        "variant": input_variant,
    }
    with registry.together(names, source, max_new_tokens, trace):
        return [
            run_engine(page, name, target, source, input_variant, max_new_tokens, scale) for name in names
        ]


def _fill_run(
    run: OcrRun, result: OcrResult, engine: OcrEngine, origin: tuple[int, int], scale: float
) -> None:
    """Copy an engine result onto `run`: parsed text, loop detection, timing and boxes (gray space)."""
    hit_cap = result.finish == "length"
    if engine.kind == "vlm":
        parsed, looped = parse_output(result.text or "", hit_cap=hit_cap)
        looped = looped or result.looped or hit_cap
    else:
        parsed, looped = normalize_ws(result.text or ""), bool(result.looped)
    run.raw_output = result.text or ""
    run.parsed_text = parsed
    run.duration_ms = max(0, int(round(result.duration_s * 1000)))
    run.output_tokens = result.output_tokens
    run.finish = (result.finish or "")[:20]
    run.looped = looped
    extra = dict(result.extra or {})
    # a remote engine (Runpod) learns the revision of the weights it read with from its answer
    revision = extra.pop("model_revision", None)
    if revision:
        run.model_revision = str(revision)[:64]
    lines = extra.pop("lines", None)
    if lines is not None:
        run.params["lines"] = _offset_lines(lines, origin[0], origin[1], scale)
    run.params.update(_json_safe(extra))


def _run_passes(run: OcrRun, reference: str) -> tuple[bool, str]:
    """`sanity_check` of a stored run against `reference`; a failed engine call is `error`."""
    if run.status != OcrRun.Status.OK:
        return False, "error"
    return sanity_check(run.parsed_text, reference, run.looped)


def _record_check(run: OcrRun, reference: str) -> tuple[bool, str]:
    """`_run_passes` and store its verdict on the run (`params["sanity"]`, shown in the runs list)."""
    ok, reason = _run_passes(run, reference)
    run.params["sanity"] = {"ok": ok, "reason": reason}
    run.save(update_fields=["params"])
    return ok, reason


# ---------------------------------------------------------------- sanity check (D16)


def sanity_check(text: str, reference: str, looped: bool = False) -> tuple[bool, str]:
    """Does a model output look like a reading of the same region as Tesseract's `reference`?

    Fails when the run looped or is empty. With a reference of at least
    `MIN_REF_WORDS_FOR_OVERLAP` words it fails when it has fewer than 20 % or more than 180 % of
    the reference's words (plus a slack of a few words), or when the order-insensitive word F1
    with the reference is below 0.45 (lenient normalisation). A shorter reference only guards
    against runaway output (`short_reference` when it passes); an empty one is inconclusive
    (`no_reference` passes only for at most `NO_REF_MAX_WORDS` words). Returns `(ok, reason)` with
    reason one of `ok`, `short_reference`, `no_reference`, `loop`, `empty`, `too_short`,
    `too_long`, `low_overlap`.
    """
    if looped:
        return False, "loop"
    words = normalize(text or "", "lenient").split()
    if not words:
        return False, "empty"
    ref = normalize(reference or "", "lenient").split()
    if not ref:
        return len(words) <= NO_REF_MAX_WORDS, "no_reference"
    if len(ref) < MIN_REF_WORDS_FOR_OVERLAP:
        if len(words) > SHORT_REF_MAX_RATIO * len(ref) + SHORT_REF_SLACK:
            return False, "too_long"
        return True, "short_reference"
    if len(words) < MIN_WORD_RATIO * len(ref):
        return False, "too_short"
    if len(words) > MAX_WORD_RATIO * len(ref) + WORD_SLACK:
        return False, "too_long"
    if word_f1(words, ref) < MIN_WORD_F1:
        return False, "low_overlap"
    return True, "ok"


# ---------------------------------------------------------------- line rescue (Tesseract stage)

# Tesseract's page segmentation sometimes drops a whole printed line, most often the last line of a
# paragraph; the primary words of that line were then appended to the line above. After a region's
# Tesseract run, every detected line band (`Preprocess.line_boxes`, the letters' core) that no
# Tesseract line covers, and every inked band the detector missed in a gap between Tesseract lines
# (a short last line), is read again alone as a single line and added to the run as a `rescued` line.
RESCUE_PSM = 7  # Tesseract "single text line"
RESCUE_PAD = 20  # white pixels around a rescue crop
RESCUE_COVER = 0.5  # a band is read when a Tesseract line covers at least this share of its height
RESCUE_MIN_BAND = 0.5  # bands thinner than this share of the median detected band are slivers
RESCUE_MIN_GAP = 0.5  # gaps thinner than this share of the median line height hold no line
RESCUE_MIN_PEAK = 3  # an inked band in a gap needs a row this many band heights long
RESCUE_INK_FLOOR = 0.08  # rows with more ink than this share of the band's densest row belong to it
RESCUE_MARGIN = 0.2  # share of the line's inked height kept above and below it
RESCUE_MAX_HEIGHT = 1.5  # a crop is at most this many median line heights tall
# Tesseract's single-line mode returns nothing for many lines of these large scans and reads them well
# once the core band is about this tall (measured on 80 lines of books 12, 13, 15).
RESCUE_CORE_PX = 8
RESCUE_MIN_SCALE = 0.3
RESCUE_MIN_CONF = 50  # a word counts as read at this confidence with two letters or digits
RESCUE_MIN_READ = 0.4  # share of the words that must count as read, else the band is noise
RESCUE_MAX_BANDS = 20  # per page: keeps a page where Tesseract failed wholesale cheap
LINE_TO_BAND = 4.0  # median Tesseract line height / median detected band height, when unknown
_RULE_WORD = re.compile(r"^[|¦]+$")  # a vertical rule read as a word


def _is_rescued(line: dict) -> bool:
    return bool(line.get("rescued"))


def _line_height(lines: list[dict]) -> float:
    """Median height of the Tesseract lines with two words or more (0 without any)."""
    heights = [
        int(line["bbox"][3]) - int(line["bbox"][1])
        for line in lines
        if line.get("bbox") and len(line.get("words") or []) >= 2
    ]
    return float(np.median(heights)) if heights else 0.0


def _in_region(band: dict, bbox: list[int]) -> bool:
    """True when the band's vertical centre lies in `bbox` and the two overlap horizontally."""
    cy = (band["y0"] + band["y1"]) / 2
    return bbox[1] <= cy < bbox[3] and min(band["x1"], bbox[2]) > max(band["x0"], bbox[0])


def _covered(band: dict, lines: list[dict]) -> bool:
    """True when a Tesseract line covers at least `RESCUE_COVER` of the band's height."""
    need = RESCUE_COVER * max(1, band["y1"] - band["y0"])
    for line in lines:
        box = line.get("bbox")
        if box and min(band["y1"], box[3]) - max(band["y0"], box[1]) >= need:
            return True
    return False


def _ink_profile(bw: np.ndarray, bbox: list[int]) -> np.ndarray:
    """Dark pixels per row of `bw` between the columns of `bbox` (rows of the whole image)."""
    return (bw[:, int(bbox[0]) : int(bbox[2])] < 128).sum(axis=1)


def _row_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """`[start, end)` of every run of True in a 1-D mask."""
    padded = np.concatenate([[False], mask.astype(bool), [False]])
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2], strict=True)]


def _gaps(lines: list[dict], bbox: list[int], min_gap: float) -> list[tuple[int, int]]:
    """Vertical gaps of at least `min_gap` pixels between the Tesseract lines of a region (and its edges)."""
    spans = sorted((int(line["bbox"][1]), int(line["bbox"][3])) for line in lines if line.get("bbox"))
    out: list[tuple[int, int]] = []
    edge = int(bbox[1])
    for top, bottom in spans:
        if top - edge >= min_gap:
            out.append((edge, top))
        edge = max(edge, bottom)
    if int(bbox[3]) - edge >= min_gap:
        out.append((edge, int(bbox[3])))
    return out


def _gap_bands(
    profile: np.ndarray, gaps: list[tuple[int, int]], band_h: float, bbox: list[int]
) -> list[dict]:
    """Inked bands the projection detector missed (a short line has too little ink for its threshold).

    In each gap, rows darker than the gap's own background (vertical rules, specks) form runs; a run
    at least `RESCUE_MIN_BAND` band heights tall whose densest row is at least `RESCUE_MIN_PEAK` band
    heights long is a line, and its rows of at least half that density are its core band.
    """
    out: list[dict] = []
    for g0, g1 in gaps:
        seg = profile[g0:g1]
        if seg.size == 0:
            continue
        floor = 2 * float(np.median(seg)) + 2
        for a, b in _row_runs(seg > floor):
            peak = float(seg[a:b].max())
            if b - a < RESCUE_MIN_BAND * band_h or peak < RESCUE_MIN_PEAK * band_h:
                continue
            core = np.flatnonzero(seg[a:b] >= peak / 2)
            out.append(
                {
                    "x0": int(bbox[0]),
                    "y0": g0 + a + int(core[0]),
                    "x1": int(bbox[2]),
                    "y1": g0 + a + int(core[-1]) + 1,
                }
            )
    return out


def rescue_candidates(
    bands: list[dict], lines: list[dict], bbox: list[int], profile: np.ndarray, band_h: float, line_h: float
) -> list[tuple[dict, tuple[int, int]]]:
    """Printed lines of a region that none of its Tesseract `lines` covers: `(core band, crop rows)`.

    Detected bands (`Preprocess.line_boxes`) in the region count unless they are slivers. The rows
    they will be read from (`_crop_rows`) are then treated like Tesseract lines, and every gap left
    is searched for inked bands the detector missed (`_gap_bands`), so a short last line right
    after a lost full line is found too. Sorted top to bottom.
    """
    found: list[tuple[dict, tuple[int, int]]] = []
    spans = list(lines)

    def claim(band: dict) -> None:
        rows = _crop_rows(profile, band, spans, bbox, line_h)
        if rows[1] - rows[0] >= RESCUE_MIN_BAND * band_h:
            found.append((band, rows))
            spans.append({"bbox": [int(bbox[0]), rows[0], int(bbox[2]), rows[1]]})

    detected = [
        b
        for b in bands
        if _in_region(b, bbox) and b["y1"] - b["y0"] >= RESCUE_MIN_BAND * band_h and not _covered(b, lines)
    ]
    for band in sorted(detected, key=lambda b: b["y0"]):
        claim(band)
    for band in _gap_bands(profile, _gaps(spans, bbox, RESCUE_MIN_GAP * line_h), band_h, bbox):
        claim(band)
    return sorted(found, key=lambda c: c[0]["y0"])


def _crop_rows(
    profile: np.ndarray, band: dict, lines: list[dict], bbox: list[int], line_h: float
) -> tuple[int, int]:
    """Rows `[y0, y1)` of the printed line around a core band, between the neighbouring Tesseract lines.

    The line is the band grown while the rows keep some ink (`RESCUE_INK_FLOOR` of its densest row),
    plus a margin; it never reaches into a Tesseract line above or below and is at most
    `RESCUE_MAX_HEIGHT` median lines tall. Tesseract's single-line mode needs this tight crop: with a
    band of white above or below the text it often returns nothing.
    """
    cy = (band["y0"] + band["y1"]) / 2
    lo, hi = int(bbox[1]), int(bbox[3])
    for line in lines:
        box = line.get("bbox")
        if not box:
            continue
        if (box[1] + box[3]) / 2 < cy:
            lo = max(lo, int(box[3]))
        else:
            hi = min(hi, int(box[1]))
    y0, y1 = max(lo, int(band["y0"])), min(hi, int(band["y1"]))
    if y1 <= y0:
        return y0, y0
    floor = max(2.0, RESCUE_INK_FLOOR * float(profile[y0:y1].max()))
    while y0 > lo and profile[y0 - 1] > floor:
        y0 -= 1
    while y1 < hi and profile[y1] > floor:
        y1 += 1
    margin = max(2, int(round(RESCUE_MARGIN * (y1 - y0))))
    y0, y1 = max(lo, y0 - margin), min(hi, y1 + margin)
    if line_h and y1 - y0 > RESCUE_MAX_HEIGHT * line_h:
        half = RESCUE_MAX_HEIGHT * line_h / 2
        y0, y1 = max(y0, int(cy - half)), min(y1, int(cy + half))
    return y0, y1


def reads_as_text(words: list[dict]) -> bool:
    """Does a single-line reading hold text rather than specks read as "ee" or "TT"?

    A word counts as read with two letters or digits at `RESCUE_MIN_CONF` confidence or more. At least
    `RESCUE_MIN_READ` of the words must count, and either one of them is Arabic or two of them
    count (a lone Latin word is how Tesseract reads a smudge).
    """
    read = [
        str(w.get("text") or "")
        for w in words
        if float(w.get("conf", -1)) >= RESCUE_MIN_CONF
        and sum(1 for ch in str(w.get("text") or "") if ch.isalnum()) >= 2
    ]
    if not read or len(read) < RESCUE_MIN_READ * len(words):
        return False
    return len(read) >= 2 or bool(_ARABIC_LETTER.search(read[0]))


def _read_band(
    engine: OcrEngine,
    bw: np.ndarray,
    rows: tuple[int, int],
    band: dict,
    bbox: list[int],
    tmpdir: Path,
    stem: str,
) -> dict | None:
    """Read rows `rows` of the region `bbox` as one line (psm 7); the line in gray-image space, or None.

    The crop is padded with white and scaled so the core band is about `RESCUE_CORE_PX` tall. Words
    that are only a vertical rule are dropped; a reading that is not text (`reads_as_text`) gives None.
    """
    y0, y1 = rows
    x0, x1 = int(bbox[0]), int(bbox[2])
    part = np.pad(bw[y0:y1, x0:x1], RESCUE_PAD, mode="constant", constant_values=255)
    scale = min(1.0, max(RESCUE_MIN_SCALE, RESCUE_CORE_PX / max(1, band["y1"] - band["y0"])))
    if scale < 1.0:
        img = Image.fromarray(part)
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
        part = np.asarray(img, dtype=np.uint8)
    path = _save_temp(part, tmpdir, stem)
    result = engine.recognize(path, None, hints={"psm": RESCUE_PSM})
    found = _offset_lines((result.extra or {}).get("lines") or [], x0 - RESCUE_PAD, y0 - RESCUE_PAD, scale)
    words = []
    for line in found:
        for word in line.get("words") or []:
            text = str(word.get("text") or "").strip()
            box = word.get("bbox")
            if not text or not box or _RULE_WORD.match(text):
                continue
            box = [max(x0, box[0]), max(y0, box[1]), min(x1, box[2]), min(y1, box[3])]
            words.append({**word, "text": text, "bbox": box})
    if not words or not reads_as_text(words):
        return None
    line_box = [
        min(w["bbox"][0] for w in words),
        min(w["bbox"][1] for w in words),
        max(w["bbox"][2] for w in words),
        max(w["bbox"][3] for w in words),
    ]
    return {"bbox": line_box, "words": words, "rescued": True}


def _insert_line(lines: list[dict], line: dict) -> list[dict]:
    """`lines` with `line` inserted before the first line whose centre lies below its centre."""
    cy = (line["bbox"][1] + line["bbox"][3]) / 2
    for k, other in enumerate(lines):
        box = other.get("bbox")
        if box and (box[1] + box[3]) / 2 > cy:
            return [*lines[:k], line, *lines[k:]]
    return [*lines, line]


def lines_text(lines: list[dict]) -> str:
    """A Tesseract run's parsed text from its lines: one text line per line (as `parse_image_to_data`)."""
    return normalize_ws(
        "\n".join(" ".join(str(w.get("text") or "") for w in line.get("words") or []) for line in lines)
    )


def rescue_lines(
    pre: Preprocess,
    bw: np.ndarray,
    runs: list[tuple[Target, OcrRun]],
    engine_name: str,
    tmpdir: Path,
    save: bool = True,
) -> int:
    """Read the printed lines Tesseract dropped from OCR-able regions and add them to their runs.

    For every successful run of a region that is not a running header or page number, the lines a
    previous rescue added are dropped, then each candidate of `rescue_candidates` is cropped to its
    rows, read as a single line (`_read_band`) and, when it reads as text, inserted into
    `params["lines"]` in reading position with `"rescued": true`; `parsed_text` is rebuilt from the
    lines when they changed and `params["rescue"]` records what was tried. At most
    `RESCUE_MAX_BANDS` bands are read per call. Saves the runs unless `save` is False (dry runs).
    Returns the number of lines added.
    """
    usable = [
        (target, run)
        for target, run in runs
        if run.status == OcrRun.Status.OK and target.kind not in SKIPPED_KINDS
    ]
    if not usable:
        return 0
    bands = [b for b in (pre.line_boxes or []) if all(k in b for k in ("x0", "y0", "x1", "y1"))]
    band_h = (
        float(np.median([b["y1"] - b["y0"] for b in bands])) if bands else float(pre.median_line_height or 0)
    )
    base = {
        id(run): [ln for ln in (run.params.get("lines") or []) if not _is_rescued(ln)] for _, run in usable
    }
    line_h = _line_height([ln for lines in base.values() for ln in lines]) or LINE_TO_BAND * band_h
    engine: OcrEngine | None = None
    budget = RESCUE_MAX_BANDS
    added = 0
    for n, (target, run) in enumerate(usable):
        before = list(run.params.get("lines") or [])
        lines = base[id(run)]
        tried = 0
        new_lines = list(lines)
        if band_h and line_h:
            profile = _ink_profile(bw, target.bbox)
            candidates = rescue_candidates(bands, lines, target.bbox, profile, band_h, line_h)
            for c, (band, rows) in enumerate(candidates):
                if budget <= 0:
                    log.info("page %s: line rescue stopped after %d bands", pre.page_id, RESCUE_MAX_BANDS)
                    break
                budget -= 1
                tried += 1
                try:
                    engine = engine or registry.get_engine(engine_name)
                    line = _read_band(engine, bw, rows, band, target.bbox, tmpdir, f"rescue-{n}-{c}")
                except Exception:  # noqa: BLE001 - the rescue is a bonus: never fail the stage for it
                    log.exception("page %s: line rescue failed on rows %s", pre.page_id, rows)
                    continue
                if line is not None:
                    new_lines = _insert_line(new_lines, line)
                    added += 1
        run.params["lines"] = new_lines
        run.params["rescue"] = {"tried": tried, "added": sum(1 for ln in new_lines if _is_rescued(ln))}
        fields = ["params"]
        if new_lines != before:
            run.parsed_text = lines_text(new_lines)
            fields.append("parsed_text")
        if save:
            run.save(update_fields=fields)
    if added:
        log.info("page %s: %d line(s) Tesseract dropped were rescued", pre.page_id, added)
    return added


# ---------------------------------------------------------------- fast OCR (default queue)

TESSERACT_HEADLINE = "تعذّر تشغيل Tesseract على هذه الصفحة؛ تحقّق من تثبيت tesseract وحزمة اللغة العربية."
TESSERACT_HINT = "brew install tesseract tesseract-lang"


def _load_fast_engine(name: str) -> None:
    """Load the fast engine now so a missing install fails with the actionable Arabic headline."""
    try:
        registry.get_engine(name)
    except Exception as exc:  # noqa: BLE001 - configuration problem: stop with an Arabic message
        raise OcrError(f"{TESSERACT_HEADLINE}\n{TESSERACT_HINT}\n{type(exc).__name__}: {exc}") from exc


def run_fast_ocr(page: Page) -> None:
    """Tesseract on every region of the B&W image (page-level without regions); provisional text.

    Every region gets an OcrRun with word/line boxes in `params["lines"]`; printed lines Tesseract
    dropped are then read one by one and added to those lines (`rescue_lines`). `provisional_text` joins
    the body-like regions in order, then a blank line, then the footnotes; running header and page
    number are omitted, and a leading / trailing line that is only a page number is dropped
    (`strip_page_number_lines`). The page-number region's digits (else the dropped line's) are
    stored in `printed_number`. A page that already has its final text (a re-run of this stage
    alone) only gets new runs and a new provisional text: its text state and its voted
    `printed_number` stay as they are (the full pass owns them).
    Born-digital pages with `use_text_layer` are finalised right away from the repaired text layer.
    """
    _, _, fast = engine_names()
    pre = _preprocess_of(page)
    bw = _load_field_image(pre.bw_image, "بالأبيض والأسود")
    _load_fast_engine(fast)
    targets = _targets(page, bw.shape, ocr_only=False)
    done: list[tuple[Target, OcrRun]] = []
    failures: list[str] = []
    is_final = page.text_state == Page.TextState.FINAL
    with tempfile.TemporaryDirectory(prefix="nassakh-ocr-fast-") as tmp:
        tmpdir = Path(tmp)
        for i, target in enumerate(targets):
            if target.kind == Region.Kind.PAGE_NUMBER:
                # A printed page number is a few digits ~30 px tall: Tesseract needs the crop padded,
                # enlarged and read as a single line to return anything at all.
                target = Target(target.region, _pad_bbox(target.bbox, PAGE_NUMBER_PAD, bw.shape))
                crop_img = _crop_image(bw, target.bbox, upscale=PAGE_NUMBER_UPSCALE)
                path = _save_temp(crop_img, tmpdir, f"bw-{i}-{target.kind}")
                run = run_engine(
                    page,
                    fast,
                    target,
                    path,
                    f"bw_{PAGE_NUMBER_UPSCALE}x",
                    scale=PAGE_NUMBER_UPSCALE,
                    hints={"psm": PAGE_NUMBER_PSM},
                )
            else:
                path = _save_temp(_crop_image(bw, target.bbox), tmpdir, f"bw-{i}-{target.kind}")
                run = run_engine(page, fast, target, path, "bw")
            if run.status != OcrRun.Status.OK:
                failures.append(run.error)
                continue
            done.append((target, run))
        if failures and len(failures) == len(targets):
            raise OcrError(f"{TESSERACT_HEADLINE}\n{failures[0]}")
        rescue_lines(pre, bw, done, fast, tmpdir)
    printed = ""
    texts: list[tuple[str, str]] = []
    for target, run in done:
        if target.kind == Region.Kind.PAGE_NUMBER:
            printed = printed or printed_number_of(run.parsed_text)
        elif target.kind not in SKIPPED_KINDS and not tesseract_unreadable(run, run.parsed_text):
            texts.append((target.kind, run.parsed_text))  # a photo's letters are no provisional text (D89)

    has_region = any(target.kind == Region.Kind.PAGE_NUMBER for target in targets)
    provisional, stripped = strip_page_number_lines(
        join_region_texts(texts), has_region=has_region, known=printed
    )
    page.provisional_text = provisional
    fields = ["provisional_text"]
    if not is_final:  # the full pass owns the number and the text state once it has run
        page.printed_number = printed or stripped
        page.text_state = Page.TextState.PROVISIONAL
        fields += ["printed_number", "text_state"]
    page.save(update_fields=fields)

    if uses_text_layer(page):
        if _run_text_layer(page) is not None:
            finalize_page(page)


def _run_text_layer(page: Page) -> OcrRun | None:
    """Store a `pdf_text` run for a born-digital page (falls back to the ingest text layer).

    Returns None when both sources are empty.
    """
    book = page.book
    h, w = _page_shape(page)
    target = Target(None, [0, 0, w, h])
    run: OcrRun | None = None
    with tempfile.TemporaryDirectory(prefix="nassakh-ocr-pdf-") as tmp:
        pdf_path = _local_pdf_path(book, Path(tmp))
        if pdf_path is not None:
            ref = PdfPageRef(
                pdf_path=str(pdf_path),
                index=int(page.source_index),
                half=page.source_half,
                split_ratio=float(page.split_ratio_override or book.split_ratio or 0.5),
            )
            run = run_engine(page, "pdf_text", target, ref, "pdf")
    if run is not None and run.status == OcrRun.Status.OK and run.parsed_text.strip():
        page.provisional_text, _number = strip_page_number_lines(run.parsed_text)
        page.save(update_fields=["provisional_text"])
        return run
    fallback_text = normalize_ws(page.text_layer_text or "")
    if not fallback_text:
        return None
    run = OcrRun.objects.create(
        page=page,
        region=None,
        engine_name="pdf_text",
        model_id="pymupdf:get_text",
        backend="pdf",
        input_variant="ingest",
        raw_output=page.text_layer_text,
        parsed_text=fallback_text,
        params={"scope": PAGE_SCOPE, "kind": PAGE_KIND, "source": "text_layer_text"},
        finish="n/a",
    )
    page.provisional_text, _number = strip_page_number_lines(fallback_text)
    page.save(update_fields=["provisional_text"])
    return run


def _local_pdf_path(book, tmpdir: Path) -> Path | None:
    """A filesystem path to the book's PDF (copied to `tmpdir` for non-local storages)."""
    field = book.source_pdf
    if not field or not field.name:
        return None
    try:
        return Path(field.path)
    except (NotImplementedError, AttributeError):
        pass
    try:
        target = tmpdir / "source.pdf"
        with field.open("rb") as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        return target
    except FileNotFoundError:
        return None


def _latest_text_layer_run(page: Page) -> OcrRun | None:
    """Newest successful, non-empty page-level `pdf_text` run of the page, if any."""
    return (
        page.ocr_runs.filter(engine_name="pdf_text", region__isnull=True, status=OcrRun.Status.OK)
        .exclude(parsed_text="")
        .order_by("-created_at", "-id")
        .first()
    )


# ---------------------------------------------------------------- full OCR (gpu queue)


def run_full_ocr(page: Page) -> None:
    """Primary and secondary Qari on every OCR-able region, Tesseract as the reference; then finalise.

    Body, heading, poetry and other regions are sent as gray crops; footnotes are upscaled 2x
    (`gray_2x`); running header and page number are skipped. The primary output is checked against
    Tesseract with `sanity_check`; the secondary runs in every case (as the candidate when the
    primary failed, as the source of alternatives when it passed). `finalize_page` then selects.
    Born-digital pages that already have a text-layer run are only finalised. The page-number
    region is read first (`_vote_page_number`); when every reading of it is words, not digits, it
    was a short last line of text: its detected box is dropped and the regions re-derived
    (`processing.services.drop_detected_page_number`) so the models read those words with the body.
    """
    primary, secondary, fast = engine_names()
    if uses_text_layer(page) and _latest_text_layer_run(page) is not None:
        finalize_page(page)
        return

    pre = _preprocess_of(page)
    gray = _load_field_image(pre.gray_image, "الرمادية")
    upscale = int(nassakh().get("FOOTNOTE_UPSCALE", 2) or 1)
    for name in (primary, secondary):
        try:
            registry.get_engine(name)
        except Exception as exc:  # noqa: BLE001 - configuration problem: stop with an Arabic message
            raise OcrError(
                f"تعذّر تحميل محرّك التعرّف «{name}»؛ تحقّق من إعدادات OCR_MODELS_DIR وOCR_BACKEND.\n"
                f"{type(exc).__name__}: {exc}"
            ) from exc

    bw: np.ndarray | None = None
    model_errors: list[str] = []
    n_model_runs = 0
    with tempfile.TemporaryDirectory(prefix="nassakh-ocr-full-") as tmp:
        tmpdir = Path(tmp)
        number = _vote_page_number(page, gray, tmpdir, (primary, secondary))
        if number is not None and number[1] and not _has_review_work(page):
            from processing.services import drop_detected_page_number  # other app: lazy import

            if drop_detected_page_number(page):
                number = None  # the region is gone: its words are read with the body below
        targets = _targets(page, gray.shape, ocr_only=True)
        for i, target in enumerate(targets):
            tess = _latest_runs(page, target).get(fast)
            if tess is None or tess.status != OcrRun.Status.OK:
                if bw is None:
                    bw = _load_field_image(pre.bw_image, "بالأبيض والأسود")
                    _load_fast_engine(fast)
                bw_path = _save_temp(_crop_image(bw, target.bbox), tmpdir, f"bw-{i}-{target.kind}")
                tess = run_engine(page, fast, target, bw_path, "bw")
                rescue_lines(pre, bw, [(target, tess)], fast, tmpdir)
            reference = tess.parsed_text if tess.status == OcrRun.Status.OK else ""

            scale = upscale if target.kind in FOOTNOTE_KINDS and upscale > 1 else 1
            variant = f"gray_{scale}x" if scale > 1 else "gray"
            image_path = _save_temp(
                _crop_image(gray, target.bbox, scale), tmpdir, f"{variant}-{i}-{target.kind}"
            )
            cap = max_new_tokens_for(target.kind)

            primary_run, secondary_run = _read_models(
                page, (primary, secondary), target, image_path, variant, cap, scale
            )
            ok, reason = _record_check(primary_run, reference)
            if not ok:
                log.info("page %s %s: primary %s failed sanity (%s)", page.pk, target.kind, primary, reason)
            _record_check(secondary_run, reference)
            n_model_runs += 2
            both_read = primary_run.status == OcrRun.Status.OK and secondary_run.status == OcrRun.Status.OK
            if both_read and scale > 1 and select_reading(primary_run, secondary_run, tess).fallback:
                # a wide, short strip at 2× (book 38 p. 5: two footnote lines, 3030 × 400 px) makes both
                # models repeat its first line; at 1× they read it: read again without the upscale
                scale, variant = 1, "gray"
                log.info("page %s %s: both models failed at 2×; read again at 1×", page.pk, target.kind)
                image_path = _save_temp(
                    _crop_image(gray, target.bbox), tmpdir, f"{variant}-{i}-{target.kind}"
                )
                primary_run, secondary_run = _read_models(
                    page, (primary, secondary), target, image_path, variant, cap, scale
                )
                _record_check(primary_run, reference)
                _record_check(secondary_run, reference)
                n_model_runs += 2
            if select_reading(primary_run, secondary_run, tess).fallback and _worth_pieces(tess, reference):
                pieces = piece_boxes(pre.line_boxes, target.bbox)
                if pieces:
                    log.info(
                        "page %s %s: both models failed; read again in %d pieces",
                        page.pk,
                        target.kind,
                        len(pieces),
                    )
                    models = (primary, secondary)
                    _read_in_pieces(
                        page, target, models, gray, pieces, scale, variant, cap, reference, tmpdir, i
                    )
                    n_model_runs += len(models) * len(pieces)
            model_errors += [r.error for r in (primary_run, secondary_run) if r.status == OcrRun.Status.ERROR]
    if n_model_runs and len(model_errors) == n_model_runs:
        # Every model call crashed (out of memory, broken weights...): this is an engine failure,
        # not a page to finalise from Tesseract; the page goes to `error` and can be retried.
        raise OcrError(
            "تعذّر تشغيل نماذج التعرّف على هذه الصفحة؛ راجع سجل عامل GPU ثم أعد تشغيل المرحلة.\n"
            f"{model_errors[0]}"
        )
    if number is not None and number[0] != page.printed_number:
        page.printed_number = number[0]
        page.save(update_fields=["printed_number"])
    finalize_page(page)


def _worth_pieces(tess: OcrRun | None, reference: str) -> bool:
    """Whether a region both models failed on may be text the models read in smaller pieces (D90): Tesseract
    saw words there, not a picture — no Latin, ornament or letterless reading, and a confidence of at least
    `PIECE_MIN_CONF` (vowelled print reads at 44–53, photos at 22–36)."""
    if not (reference or "").strip():
        return False
    why = tesseract_unreadable(tess, reference)
    if why == "low confidence":
        return (tesseract_confidence(tess) or 0.0) >= PIECE_MIN_CONF
    return not why


def piece_boxes(bands: list[dict] | None, bbox: list[int]) -> list[list[int]]:
    """The region `bbox` cut into pieces of `PIECE_LINES` printed lines (`bands`, the page's detected lines),
    each cut halfway between two lines; none for a region of fewer than `PIECE_MIN_BANDS` lines (D90)."""
    x0, y0, x1, y1 = (int(v) for v in bbox)
    inside = sorted(
        (b for b in bands or [] if y0 <= (float(b["y0"]) + float(b["y1"])) / 2 <= y1),
        key=lambda b: float(b["y0"]),
    )
    if len(inside) < PIECE_MIN_BANDS:
        return []
    groups = [inside[k : k + PIECE_LINES] for k in range(0, len(inside), PIECE_LINES)]
    if len(groups) > 2 and len(groups[-1]) == 1:  # a lone last line goes with the piece above
        last = groups.pop()
        groups[-1] = groups[-1] + last
    if len(groups) < 2:
        return []
    out: list[list[int]] = []
    top = y0
    for k, group in enumerate(groups):
        if k + 1 < len(groups):
            bottom = int((float(group[-1]["y1"]) + float(groups[k + 1][0]["y0"])) / 2)
        else:
            bottom = y1
        out.append([x0, top, x1, bottom])
        top = bottom
    return out


def _read_in_pieces(
    page: Page,
    target: Target,
    names: Sequence[str],
    gray: np.ndarray,
    pieces: list[list[int]],
    scale: int,
    variant: str,
    cap: int,
    reference: str,
    tmpdir: Path,
    index: int,
) -> list[OcrRun]:
    """Read `target` again piece by piece (D90) with every engine of `names` (on Runpod both models read a
    piece in one request) and store each engine's reading as one run of the region (`_join_pieces`); being the
    latest of its engine, it is the one `select_reading` sees. Returns those runs, in the order of `names`."""
    parts: list[list[OcrRun]] = [[] for _ in names]
    for k, box in enumerate(pieces):
        piece = Target(target.region, box)
        path = _save_temp(_crop_image(gray, box, scale), tmpdir, f"{variant}-{index}-{target.kind}~p{k}")
        runs = _read_models(page, names, piece, path, f"{variant}_p{k}", cap, scale)
        for engine_parts, run in zip(parts, runs, strict=True):
            engine_parts.append(run)
    return [_join_pieces(page, target, engine_parts, variant, cap, reference) for engine_parts in parts]


def _join_pieces(
    page: Page, target: Target, parts: list[OcrRun], variant: str, cap: int, reference: str
) -> OcrRun:
    """One engine's pieces as one run of the region (`input_variant` «…_pieces», the pieces' runs listed in
    `params["pieces"]`), checked against the region's Tesseract text like any run (D90)."""
    first = parts[0]
    failed = next((r for r in parts if r.status != OcrRun.Status.OK), None)
    # a piece looping on dots or a flourish (a separator «. . . .», «* * *») holds no words: it is empty
    blank = {r.pk for r in parts if r.looped and not _ARABIC_LETTER.search(r.parsed_text or "")}
    run = OcrRun(
        page=page,
        region=target.region,
        engine_name=first.engine_name,
        model_id=first.model_id,
        model_revision=first.model_revision,
        backend=first.backend,
        prompt=first.prompt,
        input_variant=f"{variant}_pieces"[:20],
        params={
            "scope": target.scope,
            "kind": target.kind,
            "max_new_tokens": cap,
            "pieces": [r.pk for r in parts],
        },
        raw_output="\n".join(r.raw_output for r in parts),
        parsed_text="\n".join(r.parsed_text for r in parts if r.parsed_text and r.pk not in blank),
        looped=any(r.looped for r in parts if r.pk not in blank),
        duration_ms=sum(r.duration_ms or 0 for r in parts),
        output_tokens=sum(r.output_tokens or 0 for r in parts),
        finish="pieces",
    )
    if failed is not None:
        run.status, run.error = OcrRun.Status.ERROR, failed.error
    run.save()
    _record_check(run, reference)
    return run


PAGE_NUMBER_VLM_TOKENS = 16


def _vote_page_number(
    page: Page, gray: np.ndarray, tmpdir: Path, engines: tuple[str, ...]
) -> tuple[str, bool] | None:
    """Read the page-number region with the given models: `(agreed number, reads as words)`.

    None when the page has no page-number region. Tesseract misreads isolated Arabic-Indic digits
    ("٦" as "+", "٣١" as "اف") and the models misread them too now and then, so the number is
    decided by vote: the digits from each model call plus Tesseract's reading of the region (its
    latest run, not `printed_number`, which may hold an earlier vote). A value carried by at least
    two voters is agreed; with no agreement the number is '' because an unknown number is better
    than a wrong one (the dashboard's sequence check would otherwise raise false alarms). The
    region reads as words when no reading holds a digit and a model read at least two Arabic
    letters: a paragraph's short last line that the layout took for the page number.
    """
    region = page.regions.filter(kind=Region.Kind.PAGE_NUMBER).order_by("order").first()
    if region is None:
        return None
    target = Target(region, _pad_bbox(region.bbox, PAGE_NUMBER_PAD, gray.shape))
    path = _save_temp(
        _crop_image(gray, target.bbox, upscale=PAGE_NUMBER_UPSCALE), tmpdir, f"gray-number-{target.kind}"
    )
    _, _, fast = engine_names()
    tess = _latest_runs(page, Target(region, list(region.bbox))).get(fast)
    readings = [tess.parsed_text] if tess is not None and tess.status == OcrRun.Status.OK else []
    votes: list[str] = [printed_number_of(readings[0])] if readings else []
    model_texts: list[str] = []
    runs = _read_models(
        page,
        engines,
        target,
        path,
        f"gray_{PAGE_NUMBER_UPSCALE}x",
        PAGE_NUMBER_VLM_TOKENS,
        scale=PAGE_NUMBER_UPSCALE,
    )
    for run in runs:
        if run.status == OcrRun.Status.OK:
            model_texts.append(run.parsed_text)
            votes.append(printed_number_of(run.parsed_text))
    votes = [v for v in votes if v]
    agreed = next((v for v in votes if votes.count(v) >= 2), "")
    no_digit = not any(_DIGITS.search(text or "") for text in readings + model_texts)
    return agreed, no_digit and any(reads_as_words(text) for text in model_texts)


# ---------------------------------------------------------------- selection and finalisation


def _latest_runs(page: Page, target: Target) -> dict[str, OcrRun]:
    """Most recent run per engine name for `target` (page-level runs are those with `scope=page`)."""
    if target.region is not None:
        qs = page.ocr_runs.filter(region=target.region)
    else:
        qs = page.ocr_runs.filter(region__isnull=True, params__scope=PAGE_SCOPE)
    latest: dict[str, OcrRun] = {}
    for run in qs.order_by("-created_at", "-id"):
        latest.setdefault(run.engine_name, run)
    return latest


def looped_prefix_of(run: OcrRun | None) -> str:
    """The clean start of a model run that looped (`flags.looped_prefix`), '' for any other run."""
    if run is None or run.status != OcrRun.Status.OK or not run.looped:
        return ""
    return flags.looped_prefix(run.raw_output or run.parsed_text or "", hit_cap=run.finish == "length")


def tesseract_confidence(run: OcrRun | None) -> float | None:
    """The mean confidence of the words of a Tesseract run (None without words)."""
    confs = [
        float(w.get("conf") or 0)
        for line in ((run.params or {}).get("lines") or [] if run is not None else [])
        for w in line.get("words") or []
        if str(w.get("text") or "").strip()
    ]
    return sum(confs) / len(confs) if confs else None


def tesseract_unreadable(run: OcrRun | None, text: str) -> str:
    """Why Tesseract's `text` of a region is no text at all (D89), '' when it may be read: no letter in it,
    its words' mean confidence under `TESS_UNREADABLE_CONF` (photos: 22–34, print: 63–87 on books 29 and
    31), half its letters or more Latin (an Arabic page's picture read as English), or nine in ten of its
    letters from an ornament's few («ههه ها هد واه»: a dotted border read at confidence 83)."""
    arabic = _ARABIC_LETTER.findall(text or "")
    latin = _LATIN_LETTER.findall(text or "")
    if not arabic and not latin:
        return "no letters"
    mean = tesseract_confidence(run)
    if mean is not None and mean < TESS_UNREADABLE_CONF:
        return "low confidence"
    if len(latin) >= TESS_UNREADABLE_LATIN * (len(arabic) + len(latin)):
        return "latin"
    if len(arabic) >= ORNAMENT_MIN_LETTERS and sum(
        c in ORNAMENT_LETTERS for c in arabic
    ) >= ORNAMENT_SHARE * len(arabic):
        return "ornament"
    return ""


def select_reading(
    primary: OcrRun | None, secondary: OcrRun | None, tesseract: OcrRun | None, partial: bool = True
) -> Selection:
    """Pick the text of one region from its latest runs (D16), and its second reading (D73).

    The primary when it passes the sanity check (alternatives from the secondary when that passes too),
    else the secondary when it passes, else the primary when both only differ from Tesseract
    (`COMPARISON_REASONS`) but agree with each other (`models_agree`), else Tesseract's text with
    `fallback`. When the secondary looped under the primary's text, its clean start
    (`looped_prefix_of`) is the second reading, flagged `alt_partial` (with `partial`; without, the rule
    before 7b: no second reading). The secondary's text never gets the looped primary's start: review
    names `t` the primary and `alt` the secondary on every page, so it would name the two the wrong way
    round; that text is one model's reading.
    """
    reference = (
        tesseract.parsed_text if tesseract is not None and tesseract.status == OcrRun.Status.OK else ""
    )

    # a unit a model wrote twice in a row is read once (`flags.strip_repeat`); the run keeps its raw text
    texts = {
        id(run): (flags.strip_repeat(run.parsed_text, reference)[0] if run is not None else "")
        for run in (primary, secondary)
    }

    def passes(run: OcrRun | None) -> tuple[bool, str]:
        if run is None:
            return False, "missing"
        if run.status != OcrRun.Status.OK:
            return False, "error"
        return sanity_check(texts[id(run)], reference, run.looped)

    def prefix(run: OcrRun | None, reason: str) -> str:
        return looped_prefix_of(run) if partial and reason == "loop" else ""

    p_ok, p_reason = passes(primary)
    s_ok, s_reason = passes(secondary)
    p_text, s_text = texts[id(primary)], texts[id(secondary)]
    if p_ok:
        if s_ok:
            return Selection(p_text, s_text, False, p_reason, primary.engine_name)
        alt = prefix(secondary, s_reason)
        return Selection(p_text, alt or None, False, p_reason, primary.engine_name, bool(alt))
    if s_ok:
        return Selection(s_text, None, False, f"primary:{p_reason}", secondary.engine_name)
    if (
        p_reason in COMPARISON_REASONS
        and s_reason in COMPARISON_REASONS
        and word_f1(normalize(p_text, "lenient").split(), normalize(s_text, "lenient").split())
        >= MODELS_AGREE_F1
    ):
        # Both differ from Tesseract only, and agree with each other: trust the models (D16).
        return Selection(p_text, s_text, False, "models_agree", primary.engine_name)
    source = tesseract.engine_name if tesseract is not None else ""
    reason = f"primary:{p_reason} secondary:{s_reason}"
    why = tesseract_unreadable(tesseract, reference) if reference.strip() else ""
    if why:  # a photo, an ornament: Tesseract's letters would only be noise (D89)
        return Selection("", None, True, f"{reason} unreadable:{why}", source, unreadable=why)
    return Selection(reference, None, True, reason, source)


def select_text(
    primary: OcrRun | None, secondary: OcrRun | None, tesseract: OcrRun | None
) -> tuple[str, str | None, bool, str, str]:
    """`select_reading` as `(text, alt_text, fallback, reason, source)`; `alt_text` may be a looped
    prefix (`select_reading(...).alt_partial` tells)."""
    chosen = select_reading(primary, secondary, tesseract)
    return chosen.text, chosen.alt, chosen.fallback, chosen.reason, chosen.source


def _collect_region_texts(page: Page, tesseract: dict[int | None, OcrRun] | None = None) -> list[RegionText]:
    """Selected text of every OCR-able target (or the text-layer page) with its Tesseract lines.

    `tesseract` maps a region id (None for the page-level target) to the Tesseract run to use in
    place of the latest stored one (a dry run's rescued runs).
    """
    primary, secondary, fast = engine_names()
    h, w = _page_shape(page)
    override = tesseract or {}

    def tesseract_of(target: Target, runs: dict[str, OcrRun]) -> OcrRun | None:
        key = target.region.pk if target.region is not None else None
        return override[key] if key in override else runs.get(fast)

    if uses_text_layer(page):
        pdf_run = _latest_text_layer_run(page)
        if pdf_run is not None:
            tess_lines: list[dict] = []
            for target in _targets(page, (h, w), ocr_only=False):
                tess = tesseract_of(target, _latest_runs(page, target))
                if tess is not None and tess.status == OcrRun.Status.OK:
                    tess_lines.extend(tess.params.get("lines") or [])
            page_target = Target(None, [0, 0, w, h])
            return [
                RegionText(
                    page_target, pdf_run.parsed_text, None, tess_lines, False, "text_layer", TEXT_LAYER_SOURCE
                )
            ]

    out: list[RegionText] = []
    for target in _targets(page, (h, w), ocr_only=True):
        runs = _latest_runs(page, target)
        tess = tesseract_of(target, runs)
        chosen = select_reading(runs.get(primary), runs.get(secondary), tess)
        lines = (
            list(tess.params.get("lines") or [])
            if tess is not None and tess.status == OcrRun.Status.OK
            else []
        )
        out.append(
            RegionText(
                target,
                chosen.text,
                chosen.alt,
                lines,
                chosen.fallback,
                chosen.reason,
                chosen.source,
                chosen.alt_partial,
                chosen.unreadable,
            )
        )
    return out


def join_region_texts(items: list[tuple[str, str]]) -> str:
    """Join `(kind, text)` pairs: body-like kinds in order, then a blank line, then the footnotes."""
    main = [t.strip() for k, t in items if k not in FOOTNOTE_KINDS and k not in SKIPPED_KINDS and t.strip()]
    foot = [t.strip() for k, t in items if k in FOOTNOTE_KINDS and t.strip()]
    parts = []
    if main:
        parts.append("\n".join(main))
    if foot:
        parts.append("\n".join(foot))
    return "\n\n".join(parts)


def page_number_digits(line: str) -> str | None:
    """Western digits of `line` when it is only a page number («— ٢٢ —», «(20)», «٢٠»), else None."""
    text = (line or "").strip()
    if not text or not _PAGE_NUMBER_LINE.match(text):
        return None
    digits = to_western_digits("".join(_DIGITS.findall(text)))
    return digits[:PRINTED_NUMBER_MAX] or None


def _edge_run(lines: list[str], filled: list[int]) -> list[int]:
    """The non-empty lines `filled` from one edge of the page up to the first that holds a digit or a letter:
    the lines of marks alone before it (at most `PN_EDGE_MARKS`) and that line."""
    run: list[int] = []
    for i in filled[: PN_EDGE_MARKS + 1]:
        run.append(i)
        text = lines[i].strip()
        if _DIGITS.search(text) or not _PAGE_NUMBER_MARKS.match(text):
            break
    return run


def page_number_edges(lines: list[str], has_region: bool = False, known: str = "") -> tuple[set[int], str]:
    """Indices of the first / last non-empty lines that are only a page number, and the number.

    `digits` is the Western number of a dropped line (the last one when both are numbers), ''
    when none. A page with a single line is never emptied. Nothing between the first and the last
    non-empty line is looked at, but for the lines of marks alone between them and the page's edge,
    dropped with them (`_edge_run`: an ornament's specks around a page number that Kraken's word boxes
    read as a line of their own, «١٩٥» then «.», D92). With `has_region` (the lines come from region
    crops and the page has a page-number region, which already cut the number out) an edge line that is
    only digits is real text, a wrapped reference «٣٤» or a section marker «(١٢)», and is kept unless its
    digits equal `known`, the number read in that region.
    """
    filled = [i for i, line in enumerate(lines) if line.strip()]
    if len(filled) < 2:
        return set(), ""
    head, tail = _edge_run(lines, filled), _edge_run(lines, filled[::-1])
    if len(set(head) | set(tail)) >= len(filled):  # the marks would take the page: the edge lines alone
        head, tail = [filled[0]], [filled[-1]]
    first = page_number_digits(lines[head[-1]])
    last = page_number_digits(lines[tail[-1]])
    if has_region:
        first = first if known and first == known else None
        last = last if known and last == known else None
    drop = (set(head) if first else set()) | (set(tail) if last else set())
    return drop, last or first or ""


def strip_page_number_lines(text: str, has_region: bool = False, known: str = "") -> tuple[str, str]:
    """`text` without a leading / trailing page-number line, and that number ('' when none).

    The safety net for page numbers that reached the text (page order comes from the scan order;
    the printed number is kept as metadata only). Only the first and last non-empty lines are
    candidates; the body is left exactly as it is. `has_region` / `known`: see `page_number_edges`.
    """
    lines = (text or "").split("\n")
    drop, digits = page_number_edges(lines, has_region=has_region, known=known)
    if not drop:
        return text, ""
    kept = "\n".join(line for i, line in enumerate(lines) if i not in drop)
    return re.sub(r"\n{3,}", "\n\n", kept).strip("\n"), digits


def printed_number_of(text: str) -> str:
    """Western digits of a page-number region's OCR text ('' when it holds no number)."""
    digits = page_number_digits(" ".join((text or "").split()))
    return digits or ""


def reads_as_words(text: str) -> bool:
    """True when a page-number region's OCR text is words: no digit and at least two Arabic letters."""
    value = text or ""
    return not _DIGITS.search(value) and len(_ARABIC_LETTER.findall(value)) >= MIN_WORD_LETTERS


def _set_flags(flags: list, updates: dict[str, bool]) -> list:
    """`flags` with every key of `updates` removed, then re-added (at the end) where it is True."""
    out = [f for f in (flags or []) if f not in updates]
    out.extend(flag for flag, on in updates.items() if on)
    return out


def line_kind(role: str | None, region_kind: str | None) -> str:
    """A line's effective kind in the book (D74): `"footnote"` or `"body"`.

    The `footnote` role makes any line a note; the `body` role follows the region (a line of a
    footnote region is a note). Every other role (`heading`, `subheading`, `verse`, and `main`, which
    pulls a footnote-region line back into the body) and every other region give `"body"`. Used by the
    assembly loader, `review.services.refresh_page_text`, review's `line_item` and `api:book_sheets`.
    """
    if role == Line.Role.FOOTNOTE:
        return "footnote"
    if (role or Line.Role.BODY) == Line.Role.BODY and region_kind in FOOTNOTE_KINDS:
        return "footnote"
    return "body"


def readers_of(reading: dict | None) -> str:
    """How a page was read (`Page.reading["readers"]`, D73): `two`, `one`, `tesseract`, or '' (not read
    yet, or read before 7b). The dashboard tile and review's filmstrip carry it (the half-disc)."""
    value = reading.get("readers") if isinstance(reading, dict) else None
    return value if value in _READERS_RANK else ""


def is_unresolved(token: dict) -> bool:
    """A token still waiting for the reviewer: low confidence and no resolution (`res`) yet."""
    return token.get("conf") == "low" and not token.get("res")


def group_of(token: dict) -> int | None:
    """The insertion group of a word only the second model read (`ins`, D72), None for any other."""
    group = token.get("ins")
    return group if isinstance(group, int) and not isinstance(group, bool) else None


def count_unresolved(tokens: list[dict]) -> int:
    """Open items of a line (`Line.n_low`): its unresolved words, one per insertion group (D72)."""
    items = open_items([tokens])
    return items.words + items.groups


@dataclass(frozen=True)
class OpenItems:
    """What still waits for the reviewer on a page (D72, D73): unresolved words outside groups, open
    insertion groups (a group over two lines counts once) and open suggestions (`TextGap`)."""

    words: int = 0
    groups: int = 0
    gaps: int = 0

    @property
    def total(self) -> int:
        return self.words + self.groups + self.gaps


def open_items(token_lists, open_gaps: int = 0) -> OpenItems:
    """`OpenItems` of a page from its lines' tokens and its number of open gaps (pure)."""
    words, groups = 0, set()
    for tokens in token_lists:
        for token in tokens or []:
            if not is_unresolved(token):
                continue
            group = group_of(token)
            if group is None:
                words += 1
            else:
                groups.add(group)
    return OpenItems(words=words, groups=len(groups), gaps=int(open_gaps))


def page_open_items(page: Page) -> OpenItems:
    """`OpenItems` of a saved page: `Page.n_unresolved` is its `total` (refresh_page_text, finalize_page
    and approve_page use it). Only gaps still attached to a line count (a deleted line's gaps wait for
    the line's undo)."""
    gaps = TextGap.objects.filter(page=page, status=TextGap.Status.OPEN, line__isnull=False).count()
    return open_items(page.lines.values_list("tokens", flat=True), gaps)


@dataclass
class ComposedPage:
    """A page's lines and final text built in memory from its runs (`compose_page`), not saved yet.

    `lines` are unsaved `Line` objects; `merged` holds the orders of the lines that look like two
    printed lines in one (`alignment.merged_lines`); `printed` is the number of a dropped
    page-number line ('' when none) and `has_region` whether a page-number region owns the number.
    `gaps` are the suggestions (D72) as `{order, index, after_t, text, support}` (`order`: their
    line's), `groups` the number of groups of added words, `reading` what `Page.reading` gets (D73).
    """

    region_texts: list[RegionText]
    lines: list[Line]
    final_text: str
    fallback: bool
    poor: bool
    merged: list[int]
    printed: str
    has_region: bool
    total_tokens: int
    gaps: list[dict] = field(default_factory=list)
    groups: int = 0
    reading: dict = field(default_factory=dict)
    unreadable: bool = False  # D89: a region's Tesseract text was dropped as no text


def _page_geometry(page: Page) -> tuple[list[dict], np.ndarray | None]:
    """The page's detected line bands (`Preprocess.line_boxes`) and gray image, which fit Tesseract's
    word boxes to the printed lines (`alignment.fit_lines`); `[]` / None for what is missing."""
    try:
        pre = page.preprocess
    except ObjectDoesNotExist:
        return [], None
    try:
        gray = _load_field_image(pre.gray_image, "الرمادية")
    except (OcrError, OSError, ValueError):
        log.warning("page %s: no gray image to fit the word boxes to", page.pk)
        gray = None
    return list(pre.line_boxes or []), gray


def one_model(rt: RegionText) -> bool:
    """A region one model read: not Tesseract's text (fallback), not the text layer, no second reading."""
    return not rt.fallback and rt.source != TEXT_LAYER_SOURCE and not rt.alt_text


@dataclass
class RegionBuild:
    """One region's built lines (`alignment.build_lines`), its groups of added words (`{group: run}`),
    its suggestions (`[(index of the built token the run follows, −1 before the first; run)]`) and how
    it was read (`two` / `one` / `tesseract`, D73)."""

    built: list[dict]
    groups: dict[int, flags.Run]
    gaps: list[tuple[int, flags.Run]]
    readers: str


def build_region(
    rt: RegionText, bands: list[dict] | None = None, gray: np.ndarray | None = None, next_group: int = 1
) -> RegionBuild:
    """Build one region's lines with flag policy v2 (D71) and the words only the second model read (D72).

    The runs of Qari v0.2 words without a Qari v0.3 counterpart that pass the filters
    (`flags.secondary_only_runs`) are measured against Tesseract around them (`flags.run_support`, on
    the lines built without them on Tesseract's own lines, whichever reader gives the boxes, D92, so the
    text does not depend on the boxes): a run Tesseract supports (`flags.SUPPORT_MIN`) is merged into the
    text and the lines are built again with its words marked (`inserted`; groups numbered from
    `next_group`); any other run becomes a suggestion anchored after the token it follows. A region
    is read by `tesseract` (fallback), by `one` model (no second reading, or a looped prefix that stops
    before its last token) or by `two`.
    """
    single = one_model(rt)
    built = build_lines(
        rt.text,
        rt.alt_text,
        rt.tess_lines,
        bands,
        gray,
        single=single,
        partial=rt.alt_partial,
        box_lines=rt.box_lines,
    )
    primary = rt.text.split()
    secondary = (rt.alt_text or "").split()
    runs: list[flags.Run] = []
    if secondary and primary and not rt.fallback:
        footnote = rt.target.kind in FOOTNOTE_KINDS
        runs = [run for run in flags.secondary_only_runs(primary, secondary, footnote) if not run.drop]
    supported: list[flags.Run] = []
    unsupported: list[flags.Run] = []
    around = built  # the lines Tesseract's support is looked for on: its own (D92: not Kraken's)
    if runs and rt.tess_lines and rt.box_lines is not None:
        around = build_lines(
            rt.text, rt.alt_text, rt.tess_lines, bands, gray, single=single, partial=rt.alt_partial
        )
    for run in runs:
        run.support = round(flags.run_support(run, around, rt.tess_lines), 2) if rt.tess_lines else 0.0
        (supported if run.support >= flags.SUPPORT_MIN else unsupported).append(run)
    groups: dict[int, flags.Run] = {}
    position = list(range(len(primary)))  # primary token index → its index among the built tokens
    if supported:
        merged, inserted = flags.merge_runs(primary, supported)
        groups = {next_group + n: run for n, run in enumerate(supported)}
        position = [i for i in range(len(merged)) if i not in inserted]
        built = build_lines(
            " ".join(merged),
            rt.alt_text,
            rt.tess_lines,
            bands,
            gray,
            single=single,
            partial=rt.alt_partial,
            inserted={i: next_group + n for i, n in inserted.items()},
            box_lines=rt.box_lines,
        )
    gaps = [(position[run.at - 1] if run.at > 0 else -1, run) for run in unsupported]
    if rt.fallback:
        readers = READERS_TESSERACT
    elif single:
        readers = READERS_ONE
    elif rt.alt_partial and flags.last_read(flags.second_readings(primary, secondary)) < len(primary) - 1:
        readers = READERS_ONE
    else:
        readers = READERS_TWO
    return RegionBuild(built, groups, gaps, readers)


# ---------------------------------------------------------------- word boxes from Kraken (D92)


def kraken_boxes_on() -> bool:
    """Whether the words take Kraken's boxes (`NASSAKH["KRAKEN_BOXES"]`, env `KRAKEN_BOXES`, D92)."""
    return bool(nassakh().get("KRAKEN_BOXES", True))


def wants_boxes(rt: RegionText) -> bool:
    """A region whose words take Kraken's boxes: the models' text (not Tesseract's fallback, not the text
    layer), with the setting on and no lines attached yet."""
    return (
        kraken_boxes_on()
        and rt.box_lines is None
        and not rt.fallback
        and rt.source != TEXT_LAYER_SOURCE
        and bool(rt.text.strip())
    )


def _region_key(target: Target) -> int | None:
    return target.region.pk if target.region is not None else None


def _box_runs(page: Page) -> dict[int | None, OcrRun]:
    """The newest successful Kraken `boxes` run of each region of the page (None: the page-level target)."""
    out: dict[int | None, OcrRun] = {}
    for run in page.ocr_runs.filter(status=OcrRun.Status.OK).order_by("-created_at", "-id"):
        params = run.params or {}
        if params.get("pass") != boxes.PASS:
            continue
        if params.get("scope") == REGION_SCOPE and run.region_id is not None:
            out.setdefault(run.region_id, run)
        elif params.get("scope") == PAGE_SCOPE and run.region_id is None:
            out.setdefault(None, run)
    return out


def _crop_list(crops: list[boxes.Crop]) -> list[list]:
    return [c.as_list() for c in crops]


def _stored_lines(
    run: OcrRun, crops: list[boxes.Crop], gray: np.ndarray, tess_lines: list[dict]
) -> list[dict] | None:
    """A stored `boxes` run's lines when it read `crops` (shaped again from its answer when the shaping
    changed since), None when it read other lines."""
    params = run.params or {}
    if params.get("crops") != _crop_list(crops):
        return None
    if params.get("shape") == boxes.SHAPE:
        return list(params.get("lines") or [])
    try:
        raw = json.loads(run.raw_output or "[]")
    except json.JSONDecodeError:
        return None
    return boxes.region_lines(raw, crops, gray, tess_lines)


def _gray_path(pre: Preprocess, gray: np.ndarray, tmpdir: Path) -> str:
    """A file Kraken's runner can open for the gray image (a copy for a storage without paths)."""
    try:
        return pre.gray_image.path
    except (NotImplementedError, AttributeError, ValueError):
        return str(_save_temp(gray, tmpdir, "gray"))


def attach_box_lines(
    page: Page, region_texts: list[RegionText], bands: list[dict], gray: np.ndarray, save: bool = False
) -> int:
    """Give each of `region_texts` Kraken's lines (`RegionText.box_lines`, D92); returns how many got them.

    A region's printed lines are `boxes.region_crops` (its line bands and the Tesseract lines no band holds;
    Tesseract's own words stay on a short line where it read a number Kraken missed, `boxes.number_lines`).
    Its stored `boxes` run serves when it read the same lines; the others are read in one Kraken call for the
    page (`boxes.read_boxes`) and, with `save`, stored as one run per region (engine `kraken`, variant `gray`,
    `params`: scope and kind as any run of the region, `"pass": "boxes"`, the lines read (`crops`), the shaped
    `lines`, `shape`; `raw_output`: Kraken's characters). A region Kraken cannot read (not set up, failed, no
    word read) keeps Tesseract's boxes; nothing here stops a page.
    """
    stored = _box_runs(page)
    to_read: dict[int, tuple[RegionText, list[boxes.Crop]]] = {}
    attached = 0
    for n, rt in enumerate(region_texts):
        crops = boxes.region_crops(bands, rt.tess_lines, rt.target.bbox, gray)
        if not crops:
            continue
        run = stored.get(_region_key(rt.target))
        lines = _stored_lines(run, crops, gray, rt.tess_lines) if run is not None else None
        if lines is None:
            to_read[n] = (rt, crops)
        elif lines:
            rt.box_lines = lines
            attached += 1
    if not to_read:
        return attached
    try:
        engine = registry.get_engine("kraken")
        with tempfile.TemporaryDirectory(prefix="nassakh-boxes-") as tmp:
            path = _gray_path(page.preprocess, gray, Path(tmp))
            crops = {n: c for n, (_, c) in to_read.items()}
            tess = {n: rt.tess_lines for n, (rt, _) in to_read.items()}
            read, raw, seconds = boxes.read_boxes(engine, path, gray, crops, tess)
    except Exception as exc:  # noqa: BLE001 - the boxes are a bonus: Tesseract's stand
        log.warning("page %s: Kraken could not read the word boxes (%s); Tesseract's are used", page.pk, exc)
        return attached
    total = sum(len(crops) for _, crops in to_read.values()) or 1
    runs: list[OcrRun] = []
    for n, (rt, crops) in to_read.items():
        if read[n]:
            rt.box_lines = read[n]
            attached += 1
        if save:
            runs.append(
                OcrRun(
                    page=page,
                    region=rt.target.region,
                    engine_name=engine.name[:40],
                    model_id=(engine.model_id or "")[:200],
                    model_revision=(engine.model_revision or "")[:64],
                    backend=(engine.backend or "")[:20],
                    input_variant="gray",
                    raw_output=json.dumps(raw[n], ensure_ascii=False),
                    parsed_text="\n".join(row["text"] for row in raw[n] if row["text"]),
                    params={
                        "scope": rt.target.scope,
                        "kind": rt.target.kind,
                        "pass": boxes.PASS,
                        "shape": boxes.SHAPE,
                        "crops": _crop_list(crops),
                        "lines": read[n],
                    },
                    duration_ms=int(round(1000 * seconds * len(crops) / total)),
                    finish="n/a",
                )
            )
    if runs:
        OcrRun.objects.bulk_create(runs)
    return attached


def compose_page(
    page: Page, region_texts: list[RegionText] | None = None, save_boxes: bool = False
) -> ComposedPage:
    """Build the lines and the final text of a page from its runs without touching the database.

    `region_texts` defaults to `_collect_region_texts(page)` (the latest runs); a dry run passes
    its own. The words take their boxes from Kraken's reading of each model region's printed lines
    (`attach_box_lines`, D92: its stored run when it read the same lines, else read now and stored only
    with `save_boxes`), else from Tesseract's; either reader's boxes are fitted to the page's printed
    lines (its line bands; its gray image splits the ink read as one word, `_page_geometry` and
    `alignment.build_lines`).
    Each region is built with flag policy v2 and the words only the second model read (`build_region`:
    groups in the text, suggestions in `gaps`); tokens then go through the word-chooser hook
    (`ocr.chooser`, D26: the vote by default). A first or last line of the page that is only a page
    number is dropped (see `finalize_page`), with any suggestion anchored on it. A region whose tokens
    are anchored well enough (`MIN_ANCHOR_RATIO`) reports its lines that still look merged in
    `merged`; with poor anchoring the line split itself is a guess. A year followed by its value in
    words is checked against them (`flags.year_check`, §4.8). `reading` holds the weakest region's
    readers (D73), whether a looped prefix served as a second reading, and the numbers of groups and
    gaps.
    """
    if region_texts is None:
        region_texts = _collect_region_texts(page)
    new_lines: list[Line] = []
    main_parts: list[str] = []
    foot_parts: list[str] = []
    merged: list[int] = []
    gaps: list[dict] = []
    total_tokens = anchored = 0
    has_geometry = False
    order = 0
    wanted = [rt for rt in region_texts if wants_boxes(rt)]
    needs = wanted or any(rt.tess_lines for rt in region_texts)
    bands, gray = _page_geometry(page) if needs else ([], None)
    if wanted and gray is not None:
        attach_box_lines(page, wanted, bands, gray, save=save_boxes)
    builds: list[RegionBuild] = []
    next_group = 1
    for rt in region_texts:
        builds.append(build_region(rt, bands, gray, next_group))
        next_group += len(builds[-1].groups)
    built_per_region = [build.built for build in builds]
    # Safety net: a first / last line of the page that is only a page number is dropped from the
    # lines and the text; its number is kept as metadata (`printed_number`). When the text comes
    # from region crops of a page with a page-number region, that region already holds the number.
    has_region = (
        all(rt.target.region is not None for rt in region_texts)
        and page.regions.filter(kind=Region.Kind.PAGE_NUMBER).exists()
    )
    flat = [(r, k) for r, built in enumerate(built_per_region) for k in range(len(built))]
    drop, printed = page_number_edges(
        [built_per_region[r][k]["text"] for r, k in flat], has_region=has_region, known=page.printed_number
    )
    dropped = {flat[i] for i in drop}
    for r, (rt, build) in enumerate(zip(region_texts, builds, strict=True)):
        built = build.built
        geometry = bool(rt.tess_lines or rt.box_lines)
        has_geometry = has_geometry or geometry
        n_tokens = sum(len(b["tokens"]) for b in built)
        well_anchored = geometry and n_tokens > 0
        well_anchored = well_anchored and sum(b["n_anchored"] for b in built) >= MIN_ANCHOR_RATIO * n_tokens
        suspect = set(merged_lines(built)) if well_anchored else set()
        place = {i: (k, x) for k, b in enumerate(built) for x, i in enumerate(b["indices"])}
        orders: dict[int, int] = {}
        texts = []
        first_line = len(new_lines)
        for k, b in enumerate(built):
            if (r, k) in dropped:
                continue
            tokens = b["tokens"]
            chooser.apply_chooser(
                tokens, {"page_id": page.pk, "region_kind": rt.target.kind, "line_index": order}
            )
            text = " ".join(token["t"] for token in tokens)
            new_lines.append(
                Line(
                    page=page,
                    order=order,
                    region=rt.target.region,
                    bbox=b["bbox"],
                    text=text,
                    ocr_text=b["text"],
                    tokens=tokens,
                    confidence=b["confidence"],
                    n_low=count_unresolved(tokens),
                )
            )
            orders[k] = order
            if k in suspect:
                merged.append(order)
            order += 1
            total_tokens += len(tokens)
            anchored += b["n_anchored"]
            texts.append(text)
        region_lines = new_lines[first_line:]
        if flags.year_check([line.tokens for line in region_lines]):  # years against their words (§4.8)
            for line in region_lines:
                line.n_low = count_unresolved(line.tokens)
        for after, run in build.gaps:
            k, x = place[after] if after >= 0 else (0, -1)
            if k not in orders:
                continue  # its line was a page-number line, dropped
            gaps.append(
                {
                    "order": orders[k],
                    "index": x,
                    "after_t": built[k]["tokens"][x]["t"] if x >= 0 else "",
                    "text": run.text,
                    "support": run.support,
                }
            )
        (foot_parts if rt.target.kind in FOOTNOTE_KINDS else main_parts).append("\n".join(texts))
    final_text = join_region_texts(
        [(PAGE_KIND, "\n".join(p for p in main_parts if p))] + [(Region.Kind.FOOTNOTE, p) for p in foot_parts]
    )
    groups = sum(len(build.groups) for build in builds)
    readers = max(
        (build.readers for rt, build in zip(region_texts, builds, strict=True) if rt.text.strip()),
        key=_READERS_RANK.__getitem__,
        default=READERS_TWO,
    )
    return ComposedPage(
        region_texts=region_texts,
        lines=new_lines,
        final_text=final_text,
        fallback=any(rt.fallback and not rt.unreadable and rt.text.strip() for rt in region_texts),
        unreadable=any(rt.unreadable for rt in region_texts),
        poor=has_geometry and total_tokens >= 10 and anchored / total_tokens < MIN_ANCHOR_RATIO,
        merged=merged,
        printed=printed,
        has_region=has_region,
        total_tokens=total_tokens,
        gaps=gaps,
        groups=groups,
        reading={
            "readers": readers,
            "partial": any(rt.alt_partial for rt in region_texts),
            "groups": groups,
            "gaps": len(gaps),
        },
    )


def finalize_page(page: Page) -> ComposedPage | None:
    """Build the Line rows and the final text of a page from its stored runs; mark it `ocr_done`.

    Tokens carry flag policy v2's reasons (D71) and go through the word-chooser hook (`ocr.chooser`,
    D26: the vote); `Line.n_low` counts a line's open items and `Page.n_unresolved` the page's
    (`page_open_items`: open words, one per open group, open gaps). The page's suggestions (`TextGap`,
    D72) are replaced with its lines, and `Page.reading` records how it was read (D73): the
    `single_reader` flag marks a page one model (or Tesseract alone) read, `missing_text` one with
    groups or gaps. Review revisions recorded before this pass can no longer be undone (their lines
    are replaced).

    A page with review work (reviewed lines or an approval stamp; approved pages are refused by
    `books.services.run_stage`, this guards a pass that was queued before) keeps its lines exactly
    as they are: see `_keep_reviewed_lines`. Otherwise every line is replaced by those of
    `compose_page`. `final_text` gets Western digits (D6) while `Line.ocr_text` and the tokens keep
    the raw OCR output. Sets or clears the `ocr_fallback`, `alignment_poor` and `lines_merged`
    flags, then refreshes the book status (`ready_for_review` once every non-excluded page is
    done). A first or last line of the page that is only a page number is dropped (no Line row, not
    in the text): without a page-number region its digits go to `printed_number`; with one, only a
    line repeating the voted number is dropped and the vote is never overwritten. Returns what
    `compose_page` built (None for a page whose reviewed lines were kept). New lines then go to the
    numbers pass (`ocr.numbers.schedule`, D50).
    """
    if _has_review_work(page):
        _keep_reviewed_lines(page)
        return None
    composed = compose_page(page, save_boxes=True)
    new_lines = composed.lines

    from review.models import LineRevision  # review history of the page (other app: lazy import)

    with transaction.atomic():
        page.gaps.all().delete()
        page.lines.all().delete()
        Line.objects.bulk_create(new_lines)
        _save_gaps(page, composed.gaps)
        LineRevision.objects.filter(page=page, undone=False).update(undone=True)
        page.final_text = to_western_digits(composed.final_text)
        page.text_state = Page.TextState.FINAL
        page.reading = composed.reading
        page.attention_flags = _set_flags(
            page.attention_flags,
            {
                FLAG_FALLBACK: composed.fallback,
                FLAG_ALIGNMENT: composed.poor,
                FLAG_MERGED: bool(composed.merged),
                FLAG_SINGLE: composed.reading.get("readers") != READERS_TWO,
                FLAG_MISSING: bool(composed.groups or composed.gaps)
                or (composed.unreadable and bool(composed.final_text.strip())),
                FLAG_UNREADABLE: composed.unreadable and not composed.final_text.strip(),
            },
        )
        page.n_unresolved = page_open_items(page).total
        fields = ["final_text", "text_state", "reading", "attention_flags", "n_unresolved"]
        if composed.printed and not composed.has_region:
            page.printed_number = composed.printed
            fields.append("printed_number")
        if not page.is_excluded:
            page.status = Page.Status.OCR_DONE
            page.error_from = ""
            page.error_message = ""
            fields += ["status", "error_from", "error_message"]
        page.save(update_fields=fields)
    _refresh_book_status(page)
    from . import numbers  # the numbers pass reads the new lines' Arabic-Indic numbers (D50)

    numbers.schedule(page)
    log.info(
        "page %s finalised: %d lines, %d tokens (%d low), fallback=%s, merged=%s, sources=%s",
        page.pk,
        len(new_lines),
        composed.total_tokens,
        sum(line.n_low for line in new_lines),
        composed.fallback,
        composed.merged,
        sorted({rt.source for rt in composed.region_texts if rt.source}),
    )
    return composed


def _save_gaps(page: Page, gaps: list[dict]) -> list[TextGap]:
    """Store a composed page's suggestions (`ComposedPage.gaps`) on its lines, just saved (by order)."""
    if not gaps:
        return []
    line_ids = dict(page.lines.values_list("order", "pk"))
    rows = [
        TextGap(
            page=page,
            line_id=line_ids.get(gap["order"]),
            index=gap["index"],
            after_t=str(gap["after_t"] or "")[:200],
            text=gap["text"],
            support=float(gap["support"] or 0.0),
        )
        for gap in gaps
        if gap["order"] in line_ids
    ]
    return TextGap.objects.bulk_create(rows)


def _has_review_work(page: Page) -> bool:
    """True for a page that was approved or has reviewed lines: a new OCR pass must not replace them."""
    return page.reviewed_at is not None or page.lines.filter(is_reviewed=True).exists()


def _keep_reviewed_lines(page: Page) -> None:
    """Finalise a page that holds review work without touching its lines.

    New OCR lines cannot be matched to reviewed ones (review renumbers, deletes and inserts lines),
    so the lines stay exactly as they are, with their own order and region. The final text and
    `n_unresolved` are rebuilt from them as the review screen does, the text becomes final, and the
    page is `reviewed` again when it carries an approval stamp (else `ocr_done`; the stamp is read from
    the database: `page` may predate a long pass). The new runs stay on the page for the runs list;
    flags and review history are left alone.
    """
    from review.services import refresh_page_text  # the review app owns the text of reviewed lines

    with transaction.atomic():
        refresh_page_text(page)
        page.text_state = Page.TextState.FINAL
        fields = ["text_state"]
        if not page.is_excluded:
            page.reviewed_at = Page.objects.filter(pk=page.pk).values_list("reviewed_at", flat=True).first()
            approved = page.reviewed_at is not None
            page.status = Page.Status.REVIEWED if approved else Page.Status.OCR_DONE
            page.error_from = ""
            page.error_message = ""
            fields += ["status", "error_from", "error_message"]
        page.save(update_fields=fields)
    _refresh_book_status(page)
    log.info("page %s holds review work: its %d lines were kept", page.pk, page.lines.count())


def _refresh_book_status(page: Page) -> None:
    """Re-read the book (the instance on `page` may predate a long model run) and re-derive its status."""
    book = Book.objects.filter(pk=page.book_id).first()
    if book is not None:
        book.refresh_status()


# ---------------------------------------------------------------- rebuilding lines from stored runs

TRAILING_MIN = 3  # a line ending with this many words without a box is counted in reports


def trailing_unanchored(tokens: list[dict]) -> int:
    """Number of words at the end of a line that have no box."""
    count = 0
    for token in reversed(tokens or []):
        if token.get("bbox"):
            break
        count += 1
    return count


@dataclass
class LineStats:
    """Counts reported by `rebuild_page_lines` for one version of a page's lines."""

    lines: int
    trailing: int  # lines ending with TRAILING_MIN or more words without a box
    merged: int | None = None  # lines that look merged (`alignment.merged_lines`); unknown for stored lines

    @classmethod
    def of(cls, token_lists: list[list[dict]], merged: int | None = None) -> LineStats:
        trailing = sum(1 for tokens in token_lists if trailing_unanchored(tokens) >= TRAILING_MIN)
        return cls(lines=len(token_lists), trailing=trailing, merged=merged)


@dataclass
class RebuildResult:
    """What `rebuild_page_lines` did to one page."""

    before: LineStats
    after: LineStats
    rescued: int
    lines: list[Line]  # the new lines (saved when the rebuild was saved)
    saved: bool


def rebuild_skip_reason(page: Page) -> str:
    """Why `rebuild_page_lines` must leave `page` alone, or '' when it may rebuild it.

    Only pages whose lines are untouched OCR output qualify: status `ocr_done`, not excluded, not
    approved, no review revision at all (`review.LineRevision`, undone ones included), no reviewed
    and no manually inserted line.
    """
    from review.models import LineRevision  # review history of the page (other app: lazy import)

    if page.is_excluded:
        return "excluded"
    if page.status != Page.Status.OCR_DONE:
        return f"status {page.status}"
    if page.reviewed_at is not None:
        return "approved"
    if LineRevision.objects.filter(page=page).exists():
        return "has review revisions"
    if page.lines.filter(is_manual=True).exists():
        return "has manual lines"
    if page.lines.filter(is_reviewed=True).exists():
        return "has reviewed lines"
    return ""


def rebuild_page_lines(page: Page, save: bool = True) -> RebuildResult:
    """Re-run the line rescue on a page's stored Tesseract runs and rebuild its lines from its stored runs.

    No model is called: Tesseract only reads the rescue bands (`rescue_lines`), Kraken the printed lines
    whose stored `boxes` run read other lines or none (D92, before the page is locked), and the lines are
    built from the runs already stored (`compose_page`). With `save` the runs are saved and the page is
    finalised again (`finalize_page`); without it nothing is written. Raises `OcrError` for a page
    that `rebuild_skip_reason` refuses (callers check it first to list those pages), and when it
    refuses it once the rescue has read (a resolve or an approval made meanwhile): the check runs again
    on the page row locked as review locks it, and nothing is written.
    """
    reason = rebuild_skip_reason(page)
    if reason:
        raise OcrError(f"لا يمكن إعادة بناء أسطر هذه الصفحة ({reason}).")
    pre = _preprocess_of(page)
    bw = _load_field_image(pre.bw_image, "بالأبيض والأسود")
    _, _, fast = engine_names()
    before = LineStats.of([line.tokens for line in page.lines.order_by("order", "id")])
    pairs: list[tuple[Target, OcrRun]] = []
    for target in _targets(page, bw.shape, ocr_only=True):
        tess = _latest_runs(page, target).get(fast)
        if tess is not None and tess.status == OcrRun.Status.OK:
            pairs.append((target, tess))
    override = {target.region.pk if target.region is not None else None: run for target, run in pairs}
    if save:
        with tempfile.TemporaryDirectory(prefix="nassakh-rebuild-") as tmp:
            rescued = rescue_lines(pre, bw, pairs, fast, Path(tmp), save=False)
        texts = [rt for rt in _collect_region_texts(page, override) if wants_boxes(rt)]
        if texts:  # Kraken reads before the page is locked; `finalize_page` finds its runs (D92)
            bands, gray = _page_geometry(page)
            if gray is not None:
                attach_box_lines(page, texts, bands, gray, save=True)
        with transaction.atomic():
            # the rescue took seconds: review may have resolved or approved meanwhile (it locks this row too)
            fresh = Page.objects.select_for_update(of=("self",)).get(pk=page.pk)
            reason = rebuild_skip_reason(fresh)
            if reason:
                raise OcrError(f"لا يمكن إعادة بناء أسطر هذه الصفحة ({reason}).")
            for _target, run in pairs:  # the runs as the rescue left them (unchanged ones as they were)
                run.save(update_fields=["params", "parsed_text"])
            composed = finalize_page(fresh)
    else:
        with tempfile.TemporaryDirectory(prefix="nassakh-rebuild-") as tmp:
            rescued = rescue_lines(pre, bw, pairs, fast, Path(tmp), save=False)
        composed = compose_page(page, _collect_region_texts(page, override))
    lines = composed.lines if composed is not None else []
    after = LineStats.of([line.tokens for line in lines], len(composed.merged) if composed else None)
    return RebuildResult(before=before, after=after, rescued=rescued, lines=lines, saved=save)


# ---------------------------------------------------------------- payloads for the API / panel


def _error_headline(message: str | None) -> str:
    """First line of an error message: the Arabic headline (the rest is technical detail)."""
    lines = (message or "").splitlines()
    return lines[0] if lines else ""


def page_text_payload(page: Page) -> dict:
    """State and lines of a page for `/api/pages/<id>/text/` and the text panel."""
    lines = [
        {
            "id": line.pk,
            "order": line.order,
            "region_id": line.region_id,
            "bbox": line.bbox,
            "text": line.text,
            "tokens": line.tokens,
            "confidence": line.confidence,
            "n_low": line.n_low,
            "is_reviewed": line.is_reviewed,
        }
        for line in page.lines.order_by("order", "id")
    ]
    from books.services import ACTIVE_BOOK_STATUSES, ACTIVE_PAGE_STATUSES  # the books app owns "active"

    return {
        "page_id": page.pk,
        "status": page.status,
        "active": page.status in ACTIVE_PAGE_STATUSES and page.book.status in ACTIVE_BOOK_STATUSES,
        "text_state": page.text_state,
        "provisional_text": page.provisional_text,
        "final_text": page.final_text,
        "flags": list(page.attention_flags or []),
        "error": _error_headline(page.error_message),
        "error_detail": "\n".join((page.error_message or "").splitlines()[1:]).strip(),
        "lines": lines,
        "n_low": sum(line["n_low"] for line in lines),
    }


def page_runs_payload(page: Page, limit: int = 60) -> list[dict]:
    """Engine runs of a page, newest first, for `/api/pages/<id>/runs/` and the runs list."""
    runs = page.ocr_runs.select_related("region").order_by("-created_at", "-id")[:limit]
    out = []
    for run in runs:
        region = run.region
        sanity = (run.params or {}).get("sanity") or {}
        out.append(
            {
                "id": run.pk,
                "engine": run.engine_name,
                "engine_label": ENGINE_LABELS.get(run.engine_name, run.engine_name),
                "backend": run.backend,
                "variant": run.input_variant,
                "variant_label": VARIANT_LABELS.get(run.input_variant, run.input_variant),
                "region_id": run.region_id,
                "region_kind": region.kind if region else "",
                "region_label": region.get_kind_display() if region else "الصفحة كاملة",
                "duration_ms": run.duration_ms,
                "seconds": round(run.duration_ms / 1000, 1),
                "looped": run.looped,
                "status": run.status,
                "error": (run.error or "").splitlines()[0][:200] if run.error else "",
                "finish": run.finish,
                "output_tokens": run.output_tokens,
                "check": sanity.get("reason", ""),
                "check_ok": sanity.get("ok"),
                "created_at": run.created_at.isoformat() if run.created_at else None,
            }
        )
    return out
