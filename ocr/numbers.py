"""The numbers pass (D50): Kraken reads the Arabic-Indic numbers of a page after Qari.

Every reader we measured misreads Arabic-Indic digits; Kraken with the OpenITI printed Arabic-script
model read 92 % of 116 real numbers exactly (Qari 43 %, playground/digits/REPORT.md). So in books that
print Arabic-Indic digits, a number's reading comes from Kraken and it is the only reading offered in
review: the reviewer confirms it or types the true number. Books printed with Western digits keep
Qari's (it reads those at 90 %, Kraken at 71 %).

The pass, for one finalised page (`read_page_numbers`):
1. `printed_style(book)`: does the book print Arabic-Indic digits? Decided from what Qari already
   wrote for the book's numbers (Qari v0.2 writes Arabic-Indic digits for 90–100 % of them in such
   books, 0–12 % in Western ones; v0.3 33–82 % against 0 %).
2. `number_areas`: where each number token sits: its word box, else the gap between its nearest
   neighbours that have one (right to left: the word before it is on its right). Numbers without a
   box that share a gap share one area.
3. Kraken reads each area as one line (`ocr.engines.kraken`, its own process); `assign` gives its
   digit runs to the area's numbers in order when the counts agree (closing up stray spaces inside a
   number if that makes them agree). A token read in its own word box whose counts do not agree takes
   Kraken's reading of the whole box (`box_text`: Qari then held a fragment of the box, e.g. «١،» for
   «(ص٢٣-٢٤).»); a gap, which may hold other words, keeps Qari's reading then.
4. `apply_reading`: the token's digits become Kraken's (in Arabic-Indic digits; what surrounds them,
   brackets, «هـ», «م», stays Qari's), Qari's readings are kept under `qari` for the record and dropped
   from the offered readings (`alt`, `tess` → None), `src` = "kraken"; the token stays low-confidence
   (D17) until the reviewer confirms it. Reviewed lines and resolved tokens are never touched.

Pure functions up to `apply_reading`; the service (`read_page_numbers`) does the I/O.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

DIGIT_CHARS = "0-9٠-٩۰-۹"
_RUN = re.compile(f"[{DIGIT_CHARS}]+")
_DIGIT = re.compile(f"[{DIGIT_CHARS}]")
_EASTERN = re.compile("[٠-٩]")
_WESTERN = re.compile("[0-9]")
TO_ARABIC_INDIC = str.maketrans("0123456789۰۱۲۳۴۵۶۷۸۹", "٠١٢٣٤٥٦٧٨٩" * 2)

ARABIC_INDIC = "arabic_indic"
WESTERN = "western"
STYLE_MIN_DIGITS = 8  # fewer digits seen than this: the book's style is not known yet
STYLE_SECONDARY_SHARE = 0.5  # the secondary model (v0.2) writes this share of them Arabic-Indic, or ...
STYLE_PRIMARY_SHARE = 0.2  # ... the primary (v0.3) this share


# ====================================================================== 1. the book's printed digits


def digit_counts(tokens) -> tuple[int, int, int, int]:
    """`(primary Arabic-Indic, primary Western, secondary Arabic-Indic, secondary Western)` digits
    written for the number tokens among `tokens`."""
    pe = pw = se = sw = 0
    for token in tokens:
        primary = str(token.get("t") or "")
        if not (token.get("digit") or _DIGIT.search(primary)):
            continue
        pe += len(_EASTERN.findall(primary))
        pw += len(_WESTERN.findall(primary))
        secondary = str(token.get("alt") or "")
        se += len(_EASTERN.findall(secondary))
        sw += len(_WESTERN.findall(secondary))
    return pe, pw, se, sw


def style_of(counts: tuple[int, int, int, int]) -> str:
    """`arabic_indic`, `western`, or '' when too few digits were seen to tell."""
    pe, pw, se, sw = counts
    if pe + pw < STYLE_MIN_DIGITS:
        return ""
    if se + sw and se / (se + sw) >= STYLE_SECONDARY_SHARE:
        return ARABIC_INDIC
    if pe / (pe + pw) >= STYLE_PRIMARY_SHARE:
        return ARABIC_INDIC
    return WESTERN


# ====================================================================== 2. where the numbers are


@dataclass
class Area:
    """A stretch of one line to read with Kraken: `[x0, y0, x1, y1]` in page pixels, and the indexes of
    the number tokens it holds (in reading order)."""

    bbox: list[int]
    tokens: list[int] = field(default_factory=list)


def is_number(token: dict) -> bool:
    return bool(token.get("digit")) or bool(_DIGIT.search(str(token.get("t") or "")))


def number_areas(tokens: list[dict], line_bbox: list[int]) -> list[Area]:
    """The areas of a line's number tokens (reading order, right to left on the page).

    A token with a word box is read in that box (the runner adds a margin of 30 % of its height: the
    set-up that read 92 % of the labelled numbers). One without is read in the gap between its nearest
    neighbours that have a box (the word before it is on its right: the gap's right edge is that
    word's left edge), or the line's edge, at the line's height; tokens of one gap share it.
    """
    lx0, ly0, lx1, ly1 = (int(v) for v in line_bbox)
    areas: list[Area] = []
    gaps: dict[tuple[int, int], Area] = {}
    for i, token in enumerate(tokens):
        if not is_number(token):
            continue
        box = token.get("bbox")
        if box:
            areas.append(Area([int(v) for v in box], [i]))
            continue
        right = next((int(tokens[j]["bbox"][0]) for j in range(i - 1, -1, -1) if tokens[j].get("bbox")), lx1)
        left = next(
            (int(tokens[j]["bbox"][2]) for j in range(i + 1, len(tokens)) if tokens[j].get("bbox")), lx0
        )
        if right - left < 3:
            continue
        key = (left, right)
        if key not in gaps:
            gaps[key] = Area([left, ly0, right, ly1])
            areas.append(gaps[key])
        gaps[key].tokens.append(i)
    return areas


# ====================================================================== 3. Kraken's digits for them


def digit_runs(chars: list) -> list[list]:
    """Kraken's digit runs of an area in reading order: lists of its `[char, x0, x1, conf]` rows; a
    space (or any other character) ends a run."""
    runs: list[list] = []
    current: list = []
    for row in chars:
        if _DIGIT.fullmatch(str(row[0])):
            current.append(row)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _joined(chars: list) -> list[list]:
    """Digit runs with the stray spaces inside a number closed up («٢٠ ٠ ٠» → «٢٠٠٠»)."""
    runs: list[list] = []
    current: list = []
    for row in chars:
        char = str(row[0])
        if _DIGIT.fullmatch(char):
            current.append(row)
        elif char.isspace() and current:
            continue  # a space inside a number: the number goes on
        else:
            if current:
                runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def assign(area_tokens: list[dict], chars: list) -> list[list[str]] | None:
    """Kraken's numbers for each token of an area, in order (each token gets as many numbers as it has
    digit runs in Qari's reading), or None when the counts cannot be matched."""
    wanted = [len(_RUN.findall(str(token.get("t") or ""))) or 1 for token in area_tokens]
    for runs in (digit_runs(chars), _joined(chars)):
        numbers = ["".join(str(row[0]) for row in run) for run in runs]
        if len(numbers) != sum(wanted):
            continue
        out, k = [], 0
        for n in wanted:
            out.append(numbers[k : k + n])
            k += n
        return out
    return None


def box_text(chars: list) -> str:
    """Kraken's reading of a whole word box (spaces collapsed), '' when it holds no digit."""
    text = " ".join("".join(str(row[0]) for row in chars).split())
    return text if _DIGIT.search(text) else ""


# ====================================================================== 4. the token


def apply_reading(token: dict, numbers: list[str] | None = None, whole: str = "") -> bool:
    """Put Kraken's `numbers` into `token` (or its reading of the token's whole box, `whole`; see the
    module docstring); False when nothing changed or the token must not change (resolved by the
    reviewer, or already read)."""
    if token.get("res") or token.get("src") == "kraken" or not (numbers or whole):
        return False
    text = str(token.get("t") or "")
    runs = list(_RUN.finditer(text))
    arabic = [number.translate(TO_ARABIC_INDIC) for number in numbers or []]
    if whole:
        new = whole.translate(TO_ARABIC_INDIC)
    elif runs and len(runs) == len(arabic):
        parts, last = [], 0
        for match, number in zip(runs, arabic, strict=True):
            parts.append(text[last : match.start()])
            parts.append(number)
            last = match.end()
        parts.append(text[last:])
        new = "".join(parts)
    elif not runs and len(arabic) == 1:
        new = arabic[0]  # a number Qari wrote as something else in a box of its own
    else:
        return False
    token["qari"] = {"t": token.get("t"), "alt": token.get("alt"), "tess": token.get("tess")}
    token["t"] = new
    token["alt"] = None
    token["tess"] = None
    token["src"] = "kraken"
    token["digit"] = True
    token["conf"] = "low"  # D17: a number is confirmed by the reviewer
    return True


# ====================================================================== the pass (I/O)

STYLE_SAMPLE_DIGITS = 200  # the book's style is decided from its first numbers, this many digits
PAGE_STYLE_MIN_DIGITS = 3  # a page alone decides when the book has not shown enough numbers yet


def _as_read_by_qari(token: dict) -> dict:
    """A token as Qari read it (a number Kraken read keeps Qari's readings under `qari`)."""
    return {**token, **(token.get("qari") or {})} if token.get("src") == "kraken" else token


def _add(total: list[int], counts: tuple[int, int, int, int]) -> None:
    for k, value in enumerate(counts):
        total[k] += value


def book_style(book) -> str:
    """The digits a book prints, from Qari's readings of its first numbers (`style_of`)."""
    from ocr.models import Line

    total = [0, 0, 0, 0]
    rows = (
        Line.objects.filter(page__book=book)
        .order_by("page__number", "order")
        .values_list("tokens", flat=True)
    )
    for tokens in rows.iterator(chunk_size=500):
        _add(total, digit_counts([_as_read_by_qari(t) for t in tokens or []]))
        if total[0] + total[1] >= STYLE_SAMPLE_DIGITS:
            break
    return style_of((total[0], total[1], total[2], total[3]))


def page_style(page) -> str:
    """The style of one page's own numbers (for a book that has not shown enough of them yet)."""
    total = [0, 0, 0, 0]
    for tokens in page.lines.values_list("tokens", flat=True):
        _add(total, digit_counts([_as_read_by_qari(t) for t in tokens or []]))
    if total[0] + total[1] < PAGE_STYLE_MIN_DIGITS:
        return ""
    se, sw = total[2], total[3]
    if se + sw and se / (se + sw) >= STYLE_SECONDARY_SHARE:
        return ARABIC_INDIC
    return ARABIC_INDIC if total[0] / (total[0] + total[1]) >= STYLE_PRIMARY_SHARE else WESTERN


@dataclass
class PageNumbers:
    """What the pass did on a page."""

    style: str = ""
    areas: int = 0
    applied: int = 0
    seconds: float = 0.0
    skipped: str = ""

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("style", "areas", "applied", "seconds", "skipped")}


def page_areas(page) -> tuple[list[dict], list[tuple]]:
    """The areas to read on a page's unreviewed lines: runner requests and `(line, area)` pairs."""
    requests: list[dict] = []
    index: list[tuple] = []
    for line in page.lines.filter(is_reviewed=False).order_by("order"):
        if not line.bbox:
            continue
        tokens = line.tokens or []
        for k, area in enumerate(number_areas(tokens, line.bbox)):
            if all(tokens[i].get("res") or tokens[i].get("src") == "kraken" for i in area.tokens):
                continue  # every number of the area was already read or resolved
            requests.append({"id": f"{line.pk}:{k}", "bbox": area.bbox})
            index.append((line, area))
    return requests, index


def read_page_numbers(page, engine=None, style: str | None = None) -> PageNumbers:
    """The numbers pass on one finalised page (module docstring). `style`: the book's, when the caller
    knows it (a whole book at once); `engine`: a Kraken engine (tests pass a fake)."""
    import json

    from django.db import transaction

    from ocr.models import OcrRun
    from ocr.services import count_unresolved
    from review.services import refresh_page_text  # the review app owns the text of a page's lines

    from .engines import registry

    result = PageNumbers()
    if page.reviewed_at is not None:
        result.skipped = "approved"
        return result
    result.style = style if style is not None else (book_style(page.book) or page_style(page))
    if result.style != ARABIC_INDIC:
        result.skipped = "not arabic-indic"
        return result
    pre = getattr(page, "preprocess", None)
    if pre is None or not pre.gray_image:
        result.skipped = "no image"
        return result
    requests, index = page_areas(page)
    result.areas = len(requests)
    if not requests:
        return result
    engine = engine or registry.get_engine("kraken")
    answer = engine.read([{"image": pre.gray_image.path, "lines": requests}])
    result.seconds = float(answer.get("elapsed") or answer.get("seconds") or 0.0)
    by_id = {row.get("id"): row for row in answer.get("lines") or []}
    changed: dict[int, object] = {}
    for request, (line, area) in zip(requests, index, strict=True):
        row = by_id.get(request["id"])
        if not row:
            continue
        tokens = line.tokens
        numbers = assign([tokens[i] for i in area.tokens], row.get("chars") or [])
        own_box = len(area.tokens) == 1 and bool(tokens[area.tokens[0]].get("bbox"))
        if numbers is None and own_box:
            whole = box_text(row.get("chars") or [])
            if whole and apply_reading(tokens[area.tokens[0]], whole=whole):
                result.applied += 1
                changed[line.pk] = line
            continue
        if numbers is None:
            continue
        for i, found in zip(area.tokens, numbers, strict=True):
            if apply_reading(tokens[i], found):
                result.applied += 1
                changed[line.pk] = line
    with transaction.atomic():
        for line in changed.values():
            line.text = " ".join(token["t"] for token in line.tokens)
            line.n_low = count_unresolved(line.tokens)
            line.save(update_fields=["tokens", "text", "n_low", "updated_at"])
        if changed:
            refresh_page_text(page)
        OcrRun.objects.create(
            page=page,
            engine_name=engine.name,
            model_id=engine.model_id,
            model_revision=engine.model_revision,
            backend=engine.backend,
            input_variant="gray",
            raw_output=json.dumps(answer.get("lines") or [], ensure_ascii=False),
            parsed_text="\n".join(str(row.get("text") or "") for row in answer.get("lines") or []),
            params={"areas": result.areas, "applied": result.applied, "style": result.style},
            duration_ms=int(result.seconds * 1000),
            finish="n/a",
        )
    log.info("page %s: Kraken read %d of %d number areas", page.pk, result.applied, result.areas)
    return result


def schedule(page) -> None:
    """Queue the numbers pass of a finalised page (after the commit), when it is on and Kraken is set up."""
    from django.conf import settings
    from django.db import transaction

    if not settings.NASSAKH.get("NUMBERS_PASS", True):
        return
    from .engines.kraken import KrakenEngine

    if not KrakenEngine().is_prepared():
        return
    from . import tasks

    transaction.on_commit(lambda: tasks.read_numbers.delay(page.pk))
