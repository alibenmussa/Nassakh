"""The search index (D107): one `PageText` row per page and kind, built from the page's `ocr.Line` rows.

The source is the review layer — the lines as they were checked against the page image — not the manuscript
(edited for publishing). A line's kind is `ocr.services.line_kind`'s: body, verse and heading lines are the
author's text (`body`), footnote lines the editor's notes (`notes`). Lines of a running-header or page-number
region are no text (OCR reads none; a stray one is skipped), and neither is a footnote call mark (the call
pass's `call` tokens; `research.normalize` drops bracketed marks).

**Word states** (`token_state`), from the token flags review keeps (`review.services` module docstring):

- `reviewed`: a reviewer settled the word — `res` is one of `primary`, `secondary`, `tess`, `typed`, `sug`
  (a resolution, an edit, «تصحيح في كل الكتاب», a kept group) — or its line is reviewed (the page was
  approved) and the word is not an open one;
- `doubtful`: OCR flagged the word (`conf: "low"`, flag policy v2's reasons in `why`) and no reviewer
  settled it — `res` empty (the vote's `pick: "vote"` reading included) or `chooser` (a word the chooser
  hook put in the text, «uncertain for the eye»). It stays doubtful on a page approved with open words
  (`approve_page(force=True)`): nobody settled it;
- `unreviewed`: anything else — a confident reading (both models and Tesseract agree) nobody checked yet,
  or a year the number in words settled (`res: "words"`).

**Freshness.** `reindex_page` runs wherever a page's lines are written: `ocr.services.finalize_page` (new
lines), `review.services.refresh_page_text` (every review action: resolve, edit, insert, delete, merge, role,
groups, suggestions, undo, approve; the numbers and call passes; «تصحيح في كل الكتاب») and
`review.services.reopen_page` — through `page_changed`, which never lets an indexing error break the
action. Each row keeps the `stamp` of the lines it was built from (their count, how many are reviewed, the
latest `updated_at`); `refresh_stale` compares it with the lines' stamps in one query and rebuilds what
changed behind the hooks' back, so a search answers from the current text. `manage.py research_reindex`
rebuilds books (or everything).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from django.db import transaction
from django.db.models import Count, Max, Q

from books.models import Page
from ocr.chooser import RES_CHOOSER
from ocr.models import Line, TextGap
from ocr.services import line_kind

from .models import PageText
from .normalize import token_words

log = logging.getLogger(__name__)

# Bumped when the normal form or the words' layout changes: every stored stamp is then stale.
INDEX_VERSION = 1

REVIEWED = "reviewed"
DOUBTFUL = "doubtful"
UNREVIEWED = "unreviewed"
STATES: tuple[str, ...] = (REVIEWED, DOUBTFUL, UNREVIEWED)
# `res` values a reviewer sets (review.services.CHOICES); `chooser` and `words` are automatic.
HUMAN_RESOLUTIONS: frozenset[str] = frozenset({"primary", "secondary", "tess", "typed", "sug"})
# Regions whose lines are not text of the book (OCR reads neither; kept out should a line exist).
NOT_TEXT_REGIONS: frozenset[str] = frozenset({"running_header", "page_number"})

BODY = PageText.Kind.BODY
NOTES = PageText.Kind.NOTES
KINDS: tuple[str, ...] = (BODY, NOTES)


def token_state(token: dict, line_reviewed: bool) -> str:
    """A token's review state: `reviewed`, `doubtful` or `unreviewed` (the module docstring)."""
    res = token.get("res")
    if res in HUMAN_RESOLUTIONS:
        return REVIEWED
    if token.get("conf") == "low" and (not res or res == RES_CHOOSER):
        return DOUBTFUL
    return REVIEWED if line_reviewed else UNREVIEWED


def kind_of(line: Line) -> str | None:
    """The index kind of a line (`body` | `notes`), None for a line that is no text of the book."""
    region_kind = line.region.kind if line.region_id and line.region is not None else None
    if region_kind in NOT_TEXT_REGIONS:
        return None
    return NOTES if line_kind(line.role, region_kind) == "footnote" else BODY


def stamp_of(count: int, reviewed: int, last) -> str:
    """The stamp of a page's lines: their count, how many are reviewed and the latest save (µs)."""
    micros = int(last.timestamp() * 1_000_000) if last is not None else 0
    return f"v{INDEX_VERSION}:{count}:{reviewed}:{micros}"


def page_words(lines: Iterable[Line], gaps: Iterable[tuple[int, int]] = ()) -> dict[str, list[dict]]:
    """The words of a page's lines by kind: `{body: [...], notes: [...]}` (`PageText.words`).

    `gaps` are the open suggestions' places `(line id, token index)`: the word they follow (index −1: the
    line's first word) gets `gap: true`.
    """
    after = set(gaps)
    out: dict[str, list[dict]] = {kind: [] for kind in KINDS}
    for line in lines:
        kind = kind_of(line)
        if kind is None:
            continue
        for i, token in enumerate(line.tokens or []):
            if not isinstance(token, dict) or token.get("call"):
                continue
            state = token_state(token, line.is_reviewed)
            parts = token_words(str(token.get("t") or ""))
            for n, part in enumerate(parts):
                word = {"line": line.pk, "i": i, "text": part.raw, "norm": part.norm, "state": state}
                last = n == len(parts) - 1
                if (last and (line.pk, i) in after) or (i == 0 and n == 0 and (line.pk, -1) in after):
                    word["gap"] = True
                out[kind].append(word)
    return out


def _page_id(page: Page | int) -> int:
    return page.pk if isinstance(page, Page) else int(page)


def reindex_page(page: Page | int) -> int:
    """Rebuild the `PageText` rows of one page from its lines; returns the number of words indexed. A page
    without lines keeps no row."""
    page_id = _page_id(page)
    book_id = Page.objects.filter(pk=page_id).values_list("book_id", flat=True).first()
    if book_id is None:
        return 0
    lines = list(Line.objects.filter(page_id=page_id).select_related("region").order_by("order", "id"))
    if not lines:
        PageText.objects.filter(page_id=page_id).delete()
        return 0
    gaps = TextGap.objects.filter(page_id=page_id, status=TextGap.Status.OPEN, line__isnull=False)
    words = page_words(lines, gaps.values_list("line_id", "index"))
    stamp = stamp_of(
        len(lines), sum(1 for line in lines if line.is_reviewed), max(line.updated_at for line in lines)
    )
    with transaction.atomic():
        for kind in KINDS:
            PageText.objects.update_or_create(
                page_id=page_id,
                kind=kind,
                defaults={
                    "book_id": book_id,
                    "words": words[kind],
                    "norm": " ".join(word["norm"] for word in words[kind]),
                    "stamp": stamp,
                },
            )
    return sum(len(items) for items in words.values())


def page_changed(page: Page | int) -> None:
    """The hook the line-writing services call: rebuild the page's rows, in a savepoint, and log (never
    raise) a failure — the action that wrote the lines must not fail because of the index;
    `refresh_stale` repairs the row on the next search."""
    try:
        with transaction.atomic():
            reindex_page(page)
    except Exception:  # noqa: BLE001 - the index must never break a review action
        log.exception("research index: page %s could not be reindexed", _page_id(page))


def line_stamps(book_ids: Iterable[int]) -> dict[int, str]:
    """The current stamp of every page with lines in the given books, in one query."""
    rows = (
        Line.objects.filter(page__book_id__in=list(book_ids))
        .values("page_id")
        .annotate(n=Count("id"), r=Count("id", filter=Q(is_reviewed=True)), last=Max("updated_at"))
    )
    return {row["page_id"]: stamp_of(row["n"], row["r"], row["last"]) for row in rows}


def refresh_stale(book_ids: Iterable[int]) -> int:
    """Rebuild the rows of the given books whose stamp no longer matches their lines (and drop the rows of
    pages left without lines); returns the number of pages rebuilt. Two queries when nothing changed."""
    ids = list(book_ids)
    if not ids:
        return 0
    current = line_stamps(ids)
    stored = dict(PageText.objects.filter(book_id__in=ids, kind=BODY).values_list("page_id", "stamp"))
    gone = [page_id for page_id in stored if page_id not in current]
    if gone:
        PageText.objects.filter(page_id__in=gone).delete()
    stale = [page_id for page_id, stamp in current.items() if stored.get(page_id) != stamp]
    for page_id in stale:
        reindex_page(page_id)
    if stale:
        log.info("research index: %d stale page(s) rebuilt in book(s) %s", len(stale), ids)
    return len(stale)


def reindex_book(book_id: int) -> int:
    """Rebuild every page of a book (pages without lines lose their rows); returns the pages indexed."""
    page_ids = list(Page.objects.filter(book_id=book_id).values_list("pk", flat=True))
    with_lines = set(Line.objects.filter(page_id__in=page_ids).values_list("page_id", flat=True).distinct())
    PageText.objects.filter(book_id=book_id).exclude(page_id__in=with_lines).delete()
    for page_id in sorted(with_lines):
        reindex_page(page_id)
    return len(with_lines)
