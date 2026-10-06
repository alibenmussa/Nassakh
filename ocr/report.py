"""`rebuild_lines --report` (docs/baseline/PHASE7_SPEC.md §2.4, §4.6): flag policy v2 measured on reviewed pages.

Read-only. For every approved page (`reviewed`, `assembled`) the tokens the reviewer first saw are rebuilt
from the review history (`first_seen`): the `before` of each line's first live `LineRevision`, untouched
lines as stored, deleted lines from their delete revisions; inserted lines (`is_manual`, or first seen as
an insert) hold no OCR token and are left out. Each first-seen token is matched to the approved line by
`difflib.SequenceMatcher(autojunk=False)` over the token texts: a `replace` or `delete` marks it changed.

The three readings of each token are re-derived from the stored runs (`page_rows`):
- Qari v0.2's: the region's primary and secondary texts are aligned with the punctuation split of
  `flags.second_readings` (`align_tokens`, the lenient key); the first-seen tokens are matched to the
  region's primary tokens through a `SequenceMatcher` over the flat primary sequence (a Kraken token
  keeps Qari's reading in `qari.t` for that match);
- Tesseract's: the token's stored `tess` (its word where it differs), and the confidence of the Tesseract
  word whose box overlaps the token's most (IoU above 0.3; the word must read `tess`, or overlap above
  0.5);
- the region's second reading is the one `select_reading` gives without the looped prefix
  (`partial=False`, as the measurements of §2.4 were made), and again with it (the reading 7b writes).

A flag is *useful* when the reviewer changed the token; an error is *missed* when the reviewer changed a
sure token; a token flagged before 7b that the reviewer left untouched and unresolved (a forced
approval) is *unknown* and excluded from every policy. The policies (`POLICIES`): `today` (the stored
`conf`), `v2` (`flags.classify` without single-reader flags: the gate numbers of §8.3), `v2+single`
(the adopted rule, conf ≥ 85 and another dotless skeleton, on regions one model read), `v2+loose` (the
looser variant of §2.4: any Arabic Tesseract word at conf ≥ 70 that differs), and `v2 as written` (the
looped prefix as a second reading, and the adopted single rule).

`suggestions` counts, over every read page, the groups of words only the second model read that 7b puts
in the text and the suggestions (`TextGap`) it offers (`ocr.services.build_region`).
"""

from __future__ import annotations

import difflib
from collections import Counter
from dataclasses import dataclass, field

from books.models import Page

from . import flags
from .models import OcrRun
from .services import (
    _collect_region_texts,
    _latest_runs,
    _page_geometry,
    _targets,
    build_region,
    engine_names,
    select_reading,
)

APPROVED = (Page.Status.REVIEWED, Page.Status.ASSEMBLED)
READ = (Page.Status.OCR_DONE, Page.Status.REVIEWED, Page.Status.ASSEMBLED)
LINE_ACTIONS = ("resolve", "edit", "merge", "drop_word", "role", "insert", "delete")
MATCH_WINDOW = 80  # primary tokens looked at past a line's own length when matching it to its region
MIN_IOU = 0.3  # a Tesseract word gives a token its confidence above this overlap ...
TEXT_FREE_IOU = 0.5  # ... reading the token's `tess`, or above this one whatever it reads

POLICIES: tuple[str, ...] = ("today", "v2", "v2+single", "v2+loose", "v2 as written")


# ---------------------------------------------------------------- the tokens the reviewer first saw


def first_seen(page: Page) -> list[tuple[int | None, list[dict] | None, list[dict] | None, object]]:
    """`[(line id, first-seen tokens | None for an inserted line, approved tokens | None for a deleted
    line, the Line | None)]` of a page, in reading order (deleted lines last)."""
    from review.models import LineRevision  # the review history (other app: lazy import)

    revisions = list(
        LineRevision.objects.filter(page=page, undone=False, action__in=LINE_ACTIONS).order_by(
            "created_at", "id"
        )
    )
    before_of: dict[int, dict | None] = {}
    for revision in revisions:
        snap_id = next(
            (
                snap["id"]
                for snap in (revision.before, revision.after)
                if isinstance(snap, dict) and snap.get("id")
            ),
            None,
        )
        line_id = revision.line_id or snap_id
        if line_id is None or line_id in before_of:
            continue
        before_of[line_id] = revision.before if revision.action != "insert" else None
    out: list = []
    seen: set[int] = set()
    for line in page.lines.select_related("region").order_by("order", "id"):
        seen.add(line.pk)
        if line.pk in before_of:
            before = before_of[line.pk]
            first = None if before is None else list(before.get("tokens") or [])
        else:
            first = None if line.is_manual else list(line.tokens or [])
        out.append((line.pk, first, list(line.tokens or []), line))
    for revision in revisions:
        if revision.action == "delete" and isinstance(revision.before, dict):
            line_id = revision.before.get("id")
            if line_id in seen:
                continue
            seen.add(line_id)
            before = before_of.get(line_id) or revision.before
            out.append((line_id, list(before.get("tokens") or []), None, None))
    return out


# ---------------------------------------------------------------- the readings of each token


@dataclass
class RegionReading:
    """One region's primary tokens and, per token, Qari v0.2's reading and whether a second model read
    it, without (`sec`, `two`) and with the looped prefix (`sec_written`, `two_written`)."""

    kind: str
    primary: list[str]
    sec: list[str | None]
    two: list[bool | None]
    sec_written: list[str | None]
    two_written: list[bool | None]
    words: list[dict] = field(default_factory=list)  # Tesseract's words with a box


def _two_readers(readings: list[str | None], has_second: bool, one_model: bool, partial: bool) -> list:
    """Per token: True (two models read it), False (one model), None (no model reading to compare)."""
    n = len(readings)
    if not has_second:
        return [False if one_model else None] * n
    cutoff = flags.last_read(readings) if partial else n - 1
    return [i <= cutoff for i in range(n)]


def region_readings(page: Page) -> list[RegionReading]:
    """The readings of every OCR region of a page from its latest runs (`select_reading`)."""
    primary_name, secondary_name, fast = engine_names()
    pre = page.preprocess
    out: list[RegionReading] = []
    for target in _targets(page, (pre.output_height, pre.output_width), ocr_only=True):
        runs = _latest_runs(page, target)
        tess = runs.get(fast)
        args = (runs.get(primary_name), runs.get(secondary_name), tess)
        gate, written = select_reading(*args, partial=False), select_reading(*args, partial=True)
        primary = gate.text.split()
        one_model = not gate.fallback
        sec = flags.second_readings(primary, (gate.alt or "").split())
        sec_written = flags.second_readings(primary, (written.alt or "").split())
        words = []
        if tess is not None and tess.status == OcrRun.Status.OK:
            for line in tess.params.get("lines") or []:
                words.extend(word for word in line.get("words") or [] if word.get("bbox"))
        out.append(
            RegionReading(
                kind=target.kind,
                primary=primary,
                sec=sec,
                two=_two_readers(sec, bool(gate.alt), one_model and not gate.alt, False),
                sec_written=sec_written,
                two_written=_two_readers(
                    sec_written, bool(written.alt), one_model and not written.alt, written.alt_partial
                ),
                words=words,
            )
        )
    return out


def iou(a: list, b: list) -> float:
    """Intersection over union of two boxes `[x0, y0, x1, y1]`."""
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def tess_conf(token: dict, words: list[dict]) -> float | None:
    """The confidence of the Tesseract word that overlaps the token's box most (`MIN_IOU`), None when none."""
    box, word_text = token.get("bbox"), token.get("tess")
    if not box:
        return None
    best, conf = 0.0, None
    for word in words:
        overlap = iou(box, word["bbox"])
        if overlap > best and (word_text is None or word.get("text") == word_text or overlap > TEXT_FREE_IOU):
            best, conf = overlap, word.get("conf")
    return float(conf) if conf is not None and best > MIN_IOU else None


@dataclass
class Row:
    """One first-seen token of an approved page and what became of it."""

    book: int
    page: int
    t: str
    conf: str | None
    src: str | None
    tess: str | None
    tconf: float | None
    boxed: bool
    sec: str | None
    two: bool | None
    sec_written: str | None
    two_written: bool | None
    res: str | None
    changed: bool

    @property
    def unknown(self) -> bool:
        """Flagged before 7b, untouched and unresolved: a forced approval, no evidence either way."""
        return self.conf == "low" and not self.res and not self.changed


def page_rows(page: Page) -> list[Row]:
    """Every first-seen token of an approved page with its readings and outcome (see the module)."""
    regions = region_readings(page)
    flat_p: list[str] = []
    flat: list[tuple[str | None, bool | None, str | None, bool | None]] = []
    words: list[dict] = []
    for region in regions:
        flat_p.extend(region.primary)
        flat.extend(zip(region.sec, region.two, region.sec_written, region.two_written, strict=True))
        words.extend(region.words)
    pointer = 0
    rows: list[Row] = []
    for _line_id, first, final, line in first_seen(page):
        if first is None:
            continue
        seen = [str(t.get("t") or "") for t in first]
        approved = [str(t.get("t") or "") for t in final or []]
        keys = [str((t.get("qari") or {}).get("t") or t.get("t") or "") for t in first]
        lo, hi = (
            (pointer, min(len(flat_p), pointer + len(keys) + MATCH_WINDOW))
            if line is not None
            else (0, len(flat_p))
        )
        reading: dict[int, tuple] = {}
        last = None
        for tag, i0, _i1, j0, j1 in difflib.SequenceMatcher(
            None, flat_p[lo:hi], keys, autojunk=False
        ).get_opcodes():
            if tag == "equal":
                for k in range(j1 - j0):
                    reading[j0 + k] = flat[lo + i0 + k]
                    last = lo + i0 + k
        if line is not None and last is not None:
            pointer = last + 1
        changed = [False] * len(seen)
        resolution: list[str | None] = [None] * len(seen)
        for tag, i0, i1, j0, _j1 in difflib.SequenceMatcher(
            None, seen, approved, autojunk=False
        ).get_opcodes():
            if tag == "equal":
                for k in range(i1 - i0):
                    resolution[i0 + k] = (final[j0 + k] or {}).get("res")
            elif tag in ("replace", "delete"):
                for k in range(i0, i1):
                    changed[k] = True
        for k, token in enumerate(first):
            sec, two, sec_written, two_written = reading.get(k, (None, None, None, None))
            rows.append(
                Row(
                    book=page.book_id,
                    page=page.number,
                    t=seen[k],
                    conf=token.get("conf"),
                    src=token.get("src"),
                    tess=token.get("tess"),
                    tconf=tess_conf(token, words),
                    boxed=bool(token.get("bbox")),
                    sec=sec,
                    two=two if k in reading else None,
                    sec_written=sec_written,
                    two_written=two_written if k in reading else None,
                    res=None if changed[k] else resolution[k],
                    changed=changed[k],
                )
            )
    return rows


# ---------------------------------------------------------------- the policies


def flagged(row: Row, policy: str) -> bool:
    """Whether `policy` flags the token of `row`. A number Kraken read is always flagged: the numbers
    pass leaves it low (D50), whatever the policy said before it."""
    today = row.conf == "low"
    if policy == "today":
        return today
    if row.src == "kraken":
        return True
    if policy == "v2 as written":
        return bool(
            flags.classify(
                row.t, row.sec_written, row.tess, row.tconf, row.boxed, row.two_written, today=today
            )
        )
    # the gate numbers have no single-reader flags: a one-reader token has nothing to compare
    two = row.two if policy == "v2+single" else (True if row.two else None)
    if flags.classify(row.t, row.sec, row.tess, row.tconf, row.boxed, two, today=today):
        return True
    if policy == "v2+loose" and row.two is not True and plain_word(row.t):
        return flags.loose_single_flag(row.t, row.tess, row.tconf)
    return False


def plain_word(text: str) -> bool:
    """A word the reading rules decide (not a number, punctuation, a lone letter or another script)."""
    return not (
        flags.has_digit(text)
        or flags.is_punctuation(text)
        or flags.is_lone_letter(text)
        or flags.foreign_chars(text)
    )


@dataclass
class Score:
    """One policy's outcome on a set of tokens."""

    flags: int = 0
    useful: int = 0
    missed: int = 0
    unknown: int = 0
    zero_pages_with_errors: int = 0

    @property
    def errors(self) -> int:
        return self.useful + self.missed

    @property
    def precision(self) -> float:
        return self.useful / self.flags if self.flags else 0.0

    @property
    def recall(self) -> float:
        return self.useful / self.errors if self.errors else 0.0


def score(rows: list[Row], policy: str) -> Score:
    """`policy` on `rows` (unknown tokens excluded; see the module)."""
    result = Score()
    page_flags: Counter = Counter()
    page_errors: Counter = Counter()
    for row in rows:
        if row.unknown:
            result.unknown += 1
            continue
        hit = flagged(row, policy)
        key = (row.book, row.page)
        page_flags[key] += hit
        page_errors[key] += row.changed
        if hit:
            result.flags += 1
            result.useful += row.changed
        elif row.changed:
            result.missed += 1
    result.zero_pages_with_errors = sum(
        1 for key in set(page_flags) | set(page_errors) if not page_flags[key] and page_errors[key]
    )
    return result


def book_rows(book_id: int) -> tuple[list[Row], int]:
    """The rows of every approved page of a book, and the number of pages that could not be read."""
    rows: list[Row] = []
    failed = 0
    for page in (
        Page.objects.filter(book_id=book_id, status__in=APPROVED)
        .select_related("preprocess")
        .order_by("number")
    ):
        try:
            rows.extend(page_rows(page))
        except Exception:  # noqa: BLE001 — a page without its stored inputs is counted, not fatal
            failed += 1
    return rows, failed


# ---------------------------------------------------------------- the suggestions 7b would make


@dataclass
class Suggestions:
    """Groups merged into the text and suggestions offered on a book's read pages."""

    pages: int = 0
    groups: int = 0
    gaps: int = 0
    approved_groups: int = 0
    approved_gaps: int = 0
    items: list[tuple[int, str, float, str]] = field(
        default_factory=list
    )  # (page number, kind, support, text)


def book_suggestions(book_id: int) -> Suggestions:
    """What `build_region` gives on every read, non-excluded page of a book (nothing is written)."""
    out = Suggestions()
    pages = Page.objects.filter(book_id=book_id, status__in=READ, is_excluded=False).order_by("number")
    for page in pages:
        try:
            region_texts = _collect_region_texts(page)
            bands, gray = _page_geometry(page) if any(rt.tess_lines for rt in region_texts) else ([], None)
        except Exception:  # noqa: BLE001 — a page without its stored inputs is skipped
            continue
        out.pages += 1
        next_group = 1
        for rt in region_texts:
            build = build_region(rt, bands, gray, next_group)
            next_group += len(build.groups)
            approved = page.status in APPROVED
            out.groups += len(build.groups)
            out.gaps += len(build.gaps)
            if approved:
                out.approved_groups += len(build.groups)
                out.approved_gaps += len(build.gaps)
            out.items.extend((page.number, "group", run.support, run.text) for run in build.groups.values())
            out.items.extend((page.number, "gap", run.support, run.text) for _at, run in build.gaps)
    return out
