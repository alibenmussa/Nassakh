"""Review services: resolve uncertain words, edit / insert / delete lines, undo, approve (PHASE3 §5).

Every mutating service runs in one transaction on a locked page row, records a `LineRevision`
(snapshots before / after, which is what `undo_last` restores), puts an `assembled` page back to
`reviewed` (D36: the manuscript is out of date until the next assembly), recomputes the line's `n_low`
(unresolved tokens: `conf == "low"` and no `res`), the page's `n_unresolved` and rebuilds
`page.final_text` from the lines: body-like regions in order, a blank line, then the footnotes,
Western digits (D6). The printed page number is never part of the lines, so it never re-enters
the text. Tokens and texts keep their diacritics exactly as stored or typed.

Token keys: `t` (current reading), `alt` (secondary model; for a number Kraken read, Qari's own
letter where it wrote one, D51), `tess` (Tesseract), `conf`, `digit`,
`bbox` (gray-image pixels), `res` (`None | primary | secondary | tess | typed | sug | chooser | words`)
and, once a resolution or the vote changed `t`, `orig` (the primary model's reading, so «النموذج الأول»
can be chosen again after another reading). Phase 7b (D71–D72) adds `why` (the flag's reasons), `pick`
(`"vote"`: Tesseract backed the second model, whose reading is in the text; the word stays open), `tc`
(Tesseract's confidence), `ins` (the insertion group of words only the second model read) and `sug`
(§4.8: a year read from the number in words). The contract of these payloads is
`review/fixtures/trust/` (index.json).

Suggestions (`ocr.TextGap`, D72) belong to a line and sit after its token `index`; the services that
change a line's tokens move its gaps with them (`_shift_gaps`), and each revision of such a line keeps
the gaps' places and statuses (`"gaps"` in its snapshots) so undo puts them back. One action over
several lines (a group kept or dropped, a range of roles) shares a `LineRevision.batch`: `undo_last`
reverts the batch at once.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import F, Q, Sum
from django.urls import reverse
from django.utils import timezone

from assembly.render import ar_count
from books.models import Book, Page
from core.arabic import is_digit_token, to_western_digits
from core.decorators import ROLE_EDITOR, ROLE_PROOFREADER, has_role
from ocr.alignment import WEAK, align_tokens
from ocr.models import Line, TextGap
from ocr.services import (
    ENGINE_LABELS,
    FOOTNOTE_KINDS,
    OpenItems,
    count_unresolved,
    engine_names,
    group_of,
    join_region_texts,
    line_kind,
    page_open_items,
    readers_of,
)
from processing.models import Region

from .models import LineRevision

CHOICES: tuple[str, ...] = ("primary", "secondary", "tess", "typed", "sug")
# «نوع السطر» (D32, D74): the effective choices the review menu offers; `stored_role` maps each onto
# the stored `Line.Role` by the line's region (`main` is stored only, shown as «محتوى»).
ROLE_CHOICES: tuple[str, ...] = (
    Line.Role.BODY,
    Line.Role.HEADING,
    Line.Role.SUBHEADING,
    Line.Role.VERSE,
    Line.Role.FOOTNOTE,
)
MAX_RANGE_LINES = 200  # lines of one «نوع الأسطر المحدَّدة» request
MARKS = ("علامة واحدة", "علامتان", "علامات", "علامة")
OPEN_WORDS = ("كلمة غير محسومة", "كلمتان غير محسومتين", "كلمات غير محسومة", "كلمة غير محسومة")
# `assembled` pages stay editable (D36): any change puts them back to `reviewed` (`_touch_page`).
REVIEWABLE_STATUSES: frozenset[str] = frozenset(
    {Page.Status.OCR_DONE, Page.Status.REVIEWED, Page.Status.ASSEMBLED}
)
REVIEWED_STATUSES: frozenset[str] = frozenset({Page.Status.REVIEWED, Page.Status.ASSEMBLED})
DEFAULT_REGION_KIND = Region.Kind.BODY  # lines without a region (text-layer pages) read as body
MAX_TYPED_WORDS = 6
MAX_TYPED_CHARS = 120
MAX_LINE_CHARS = 2000
# `src`: "kraken" for a number the numbers pass read (D50): its one reading, confirmed or typed; where
# Qari wrote a letter for it (D51) Qari's letter is its `alt`. `bq`: "weak" on a box the alignment is
# unsure of (`ocr.alignment.weak_boxes`; drawn dashed). 7b's keys (D71, D72, §4.8) are in the module
# docstring. `normalize_token` keeps every stored key.
TOKEN_KEYS: tuple[str, ...] = (
    "t",
    "alt",
    "tess",
    "conf",
    "digit",
    "bbox",
    "bq",
    "res",
    "src",
    "orig",
    "why",
    "pick",
    "tc",
    "ins",
    "sug",
)


# Review's origin (D76, PHASE7_SPEC §5.3): where the reviewer came from, and the link back there.
ORIGINS: dict[str, str] = {"book": "الكتاب", "manuscript": "المخطوطة", "export": "الإخراج"}
_RE_ORIGIN_BLOCK = re.compile(r"[hpn][0-9]+")
# Line-changing actions (a drift page with one after it was read changed in review: `assembly.services.
# stale_reasons`, D78); approve / reopen / gap change no line.
LINE_ACTIONS: tuple[str, ...] = ("resolve", "edit", "insert", "delete", "merge", "drop_word", "role", "fix")


class ReviewError(Exception):
    """A review action that cannot be applied; the message is Arabic and shown to the reviewer."""


class ReviewBlocked(ReviewError):
    """Approval refused because open items remain (the API answers 409).

    `unresolved` is their total; `items` splits it into words, groups and gaps (D72–D73). With open
    words only, the message is the one review always gave; with suggested words it names both
    («بقيت 3 علامات: كلمتان غير محسومتين وكلمات مقترحة لم تُحسم. اعتماد الصفحة رغم ذلك؟»).
    """

    def __init__(self, unresolved: int | OpenItems):
        items = unresolved if isinstance(unresolved, OpenItems) else OpenItems(words=int(unresolved))
        self.items = items
        self.unresolved = items.total
        super().__init__(blocked_message(items))


def blocked_message(items: OpenItems) -> str:
    """The approve dialog's question for `items` (Arabic counts, Western digits)."""
    suggested = items.groups + items.gaps
    if not suggested:
        return f"بقيت {items.words} كلمة غير محسومة. اعتماد الصفحة رغم ذلك؟"
    parts = [ar_count(items.words, OPEN_WORDS)] if items.words else []
    parts.append("كلمات مقترحة لم تُحسم")
    return f"بقيت {ar_count(items.total, MARKS)}: {' و'.join(parts)}. اعتماد الصفحة رغم ذلك؟"


class ReviewConflict(ReviewError):
    """The line changed since the client read it (another tab, a failed queued action); API 409.

    Carries the line as it is now, so the client can replace its copy.
    """

    def __init__(self, line: Line):
        self.line = line
        super().__init__("تغيّر هذا السطر في نافذة أخرى؛ أُعيد تحميله.")


# ====================================================================== small helpers


def _user_or_none(user):
    """The user to store on a row: None for anonymous / missing users."""
    if user is None or isinstance(user, AnonymousUser) or not getattr(user, "is_authenticated", False):
        return None
    return user


def can_review(user) -> bool:
    """True for proofreaders, editors, admins and superusers."""
    return has_role(user, ROLE_PROOFREADER, ROLE_EDITOR)


def normalize_token(raw: dict) -> dict:
    """A copy of a stored token with every payload key present (`res`/`alt`/`tess` default to None)."""
    token = dict(raw or {})
    token["t"] = str(token.get("t") or "")
    for key in ("alt", "tess", "bbox", "res"):
        token.setdefault(key, None)
    token.setdefault("conf", "high")
    token.setdefault("digit", is_digit_token(token["t"]))
    return token


def typed_token(word: str, bbox: list | None = None, bq: str | None = None) -> dict:
    """A token written by the reviewer: high confidence, `res = "typed"`, no alternatives. A box it
    keeps keeps its quality mark (`bq`: "weak" when the alignment was unsure of it)."""
    token = {
        "t": word,
        "alt": None,
        "tess": None,
        "conf": "high",
        "digit": is_digit_token(word),
        "bbox": bbox,
        "res": "typed",
    }
    if bbox and bq:
        token["bq"] = bq
    return token


def _clean_words(text: str | None) -> list[str]:
    """Whitespace-separated words of `text` (diacritics and punctuation untouched)."""
    return str(text or "").split()


def _region_kind(line: Line) -> str:
    """Kind of the line's region (`body` for lines without a region)."""
    region = line.region if line.region_id else None
    return region.kind if region is not None else DEFAULT_REGION_KIND


def _page_lines(page: Page):
    """The page's lines in reading order with their regions."""
    return page.lines.select_related("region").order_by("order", "id")


def _lock_page(page: Page) -> Page:
    """Re-read `page` with a row lock (serialises concurrent review actions on one page)."""
    return Page.objects.select_for_update(of=("self",)).select_related("book").get(pk=page.pk)


def _check_editable(page: Page) -> None:
    """Refuse review actions on a page whose final text is not there (or is being recomputed)."""
    if page.status not in REVIEWABLE_STATUSES or page.text_state != Page.TextState.FINAL:
        raise ReviewError("الصفحة ليست جاهزة للمراجعة بعد؛ انتظر حتى ينتهي التعرّف على نصها.")


def _line_of(page: Page, line_id: int | None) -> Line | None:
    """A line of `page` by id (None when missing)."""
    if line_id is None:
        return None
    return Line.objects.select_related("region").filter(page=page, pk=line_id).first()


def line_snapshot(line: Line) -> dict:
    """JSON snapshot of a line, as stored in `LineRevision.before` / `after`; with `"gaps"`
    (`gap_state`) when the line has suggestions, so undo puts them back in place."""
    snapshot = {
        "id": line.pk,
        "order": line.order,
        "region_id": line.region_id,
        "bbox": line.bbox,
        "text": line.text,
        "ocr_text": line.ocr_text,
        "tokens": line.tokens,
        "confidence": line.confidence,
        "is_manual": line.is_manual,
        "is_reviewed": line.is_reviewed,
        "n_low": line.n_low,
        "role": line.role,
    }
    gaps = gap_state(line) if line.pk else {}
    if gaps:
        snapshot["gaps"] = gaps
    return snapshot


def line_version(line: Line) -> str:
    """The line's version (`v` in the payload): its last save time, sent back by whole-line actions."""
    return line.updated_at.isoformat() if line.updated_at else ""


def _check_version(line: Line, version) -> None:
    """`ReviewConflict` when the client sent a version (`v`) and the line was saved since."""
    if version is not None and str(version) != line_version(line):
        raise ReviewConflict(line)


def _check_word(line: Line, tokens: list[dict], index: int, expected) -> None:
    """`ReviewConflict` when the client sent the word it saw at `index` (`t`) and it is not there now."""
    if expected is not None and (index >= len(tokens) or tokens[index]["t"] != str(expected)):
        raise ReviewConflict(line)


def _touch_page(page: Page) -> None:
    """A change to an `assembled` page puts it back to `reviewed` (D36): the manuscript no longer
    holds its current text, so it shows as out of date until the book is re-assembled."""
    if page.status == Page.Status.ASSEMBLED:
        page.status = Page.Status.REVIEWED
        page.save(update_fields=["status"])
        _refresh_book(page)


def _record(
    page: Page, action: str, line: Line | None, before, after, user, batch: uuid.UUID | None = None
) -> LineRevision:
    """Store one revision of the page (and take an assembled page back to `reviewed`, D36). Revisions
    of one action over several lines share `batch` (undone together)."""
    _touch_page(page)
    return LineRevision.objects.create(
        page=page,
        line=line,
        action=action,
        before=before,
        after=after,
        user=_user_or_none(user),
        batch=batch,
    )


# ---------------------------------------------------------------- suggestions (TextGap, D72) on a line


def gap_state(line: Line) -> dict:
    """The places and statuses of a line's suggestions, kept in revision snapshots (`"gaps"`) so
    undo puts them back: `{gap id: {index, after_t, status}}` (`{}` when the line has none)."""
    return {
        str(gap.pk): {"index": gap.index, "after_t": gap.after_t, "status": gap.status}
        for gap in TextGap.objects.filter(line=line)
    }


def _restore_gaps(page: Page, line: Line | None, state: dict | None) -> None:
    """Put a line's suggestions back as `state` (`gap_state`) holds them: their line, place and status
    (a gap reopened loses who decided it)."""
    for key, value in (state or {}).items():
        gap = TextGap.objects.filter(pk=int(key), page=page).first()
        if gap is None:
            continue
        gap.line = line if line is not None else gap.line
        gap.index = int(value.get("index", gap.index))
        gap.after_t = str(value.get("after_t", gap.after_t) or "")[:200]
        status = value.get("status") or gap.status
        if status != gap.status and status == TextGap.Status.OPEN:
            gap.decided_by = None
            gap.decided_at = None
        gap.status = status
        gap.save(update_fields=["line", "index", "after_t", "status", "decided_by", "decided_at"])


def _shift_gaps(line: Line, mapping: dict[int, int], tokens: list[dict]) -> None:
    """Move the open suggestions of `line` after a change of its tokens.

    `mapping` gives each old token index that survives its new index (`tokens` are the new tokens). A
    gap after old token k goes after the new place of the last surviving token at or before k, or
    before the first token (−1) when none survives; `after_t` follows.
    """
    kept = sorted(mapping)
    for gap in TextGap.objects.filter(line=line, status=TextGap.Status.OPEN):
        before = [k for k in kept if k <= gap.index]
        index = mapping[before[-1]] if before else -1
        after_t = str(tokens[index].get("t") or "") if 0 <= index < len(tokens) else ""
        if (index, after_t) != (gap.index, gap.after_t):
            gap.index, gap.after_t = index, after_t[:200]
            gap.save(update_fields=["index", "after_t"])


def _follow_words(line: Line) -> None:
    """The open suggestions of `line` after a word changed in place (a resolution, «تصحيح في كل الكتاب»):
    `after_t` follows its token's new text, so `gap_anchor` keeps them where review draws them."""
    tokens = line.tokens or []
    _shift_gaps(line, {k: k for k in range(len(tokens))}, tokens)


def _aligned(old: list[dict], new: list[dict]) -> dict[int, int]:
    """Old → new token indices of an edited line: the pairs `retokenize` aligns."""
    pairs = align_tokens([str(t.get("t") or "") for t in old], [str(t.get("t") or "") for t in new])
    return {i: j for i, j in pairs if i is not None and j is not None}


def _set_tokens(line: Line, tokens: list[dict]) -> None:
    """Set the tokens and derive the line's text and `n_low` from them."""
    line.tokens = tokens
    line.text = " ".join(token["t"] for token in tokens)
    line.n_low = count_unresolved(tokens)


def _compact_orders(page: Page) -> None:
    """Renumber the page's lines 0..n-1 in their current (order, id) sequence."""
    lines = list(page.lines.order_by("order", "id").only("id", "order"))
    changed = []
    for order, line in enumerate(lines):
        if line.order != order:
            line.order = order
            changed.append(line)
    if changed:
        Line.objects.bulk_update(changed, ["order"])


def text_kind(line: Line) -> str:
    """The kind a line's text joins the page text as: a footnote when `line_kind` says so (D74), else
    its region's kind (a footnote region's `main` line reads as body)."""
    region_kind = _region_kind(line)
    if line_kind(line.role, region_kind) == "footnote":
        return Region.Kind.FOOTNOTE
    return Region.Kind.BODY if region_kind in FOOTNOTE_KINDS else region_kind


def refresh_page_text(page: Page) -> None:
    """Rebuild `final_text` (body, blank line, footnotes by `line_kind`; Western digits) and
    `n_unresolved` (the open items: `ocr.services.page_open_items`)."""
    lines = list(_page_lines(page))
    page.final_text = to_western_digits(join_region_texts([(text_kind(line), line.text) for line in lines]))
    page.n_unresolved = page_open_items(page).total
    page.save(update_fields=["final_text", "n_unresolved"])


def _page_state(page: Page) -> dict:
    """Snapshot of the page's review status fields and each line's `is_reviewed` (approve / reopen)."""
    return {
        "status": page.status,
        "reviewed_by": page.reviewed_by_id,
        "reviewed_at": page.reviewed_at.isoformat() if page.reviewed_at else None,
        "lines": {str(pk): reviewed for pk, reviewed in page.lines.values_list("pk", "is_reviewed")},
    }


def _restore_page_state(page: Page, state: dict) -> None:
    """Put back the fields captured by `_page_state` (lines missing from the snapshot are left alone)."""
    page.status = state.get("status") or Page.Status.OCR_DONE
    reviewer = state.get("reviewed_by")
    page.reviewed_by_id = (
        reviewer if reviewer and get_user_model().objects.filter(pk=reviewer).exists() else None
    )
    stamp = state.get("reviewed_at")
    page.reviewed_at = datetime.fromisoformat(stamp) if stamp else None
    page.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    flags = state.get("lines") or {}
    for value in (True, False):
        ids = [int(pk) for pk, reviewed in flags.items() if bool(reviewed) is value]
        if ids:
            page.lines.filter(pk__in=ids).update(is_reviewed=value)


def _refresh_book(page: Page) -> None:
    """Re-derive the book status after a page's review status changed."""
    book = Book.objects.filter(pk=page.book_id).first()
    if book is not None:
        book.refresh_status()


# ====================================================================== read models


def _file_url(field) -> str | None:
    """URL of a stored file field, None when it is empty."""
    return field.url if field else None


def _preprocess_of(page: Page):
    """The page's `Preprocess` row, or None before preprocessing."""
    try:
        return page.preprocess
    except ObjectDoesNotExist:
        return None


def gap_item(gap: TextGap) -> dict:
    """One suggestion of a line in the review payload."""
    return {
        "id": gap.pk,
        "index": gap.index,
        "after_t": gap.after_t,
        "text": gap.text,
        "support": round(float(gap.support or 0.0), 2),
        "status": gap.status,
    }


def line_item(line: Line) -> dict:
    """One line of the review payload (tokens with every key present), with its effective `kind`
    (`ocr.services.line_kind`, D74) and its suggestions (`gaps`, D72; prefetched by `review_payload`)."""
    region_kind = _region_kind(line)
    gaps = sorted(line.gaps.all(), key=lambda gap: (gap.index, gap.pk))
    return {
        "id": line.pk,
        "order": line.order,
        "region_id": line.region_id,
        "region_kind": region_kind,
        "kind": line_kind(line.role, region_kind),
        "bbox": line.bbox,
        "text": line.text,
        "ocr_text": line.ocr_text,
        "is_manual": line.is_manual,
        "is_reviewed": line.is_reviewed,
        "n_low": line.n_low,
        "role": line.role,
        "v": line_version(line),
        "tokens": [normalize_token(token) for token in line.tokens or []],
        "gaps": [gap_item(gap) for gap in gaps],
    }


def page_item(page: Page) -> dict:
    """The `page` block of the review payload; `reading` is how the page was read (D73, `{}` before
    7b) and `n_unresolved` its open items."""
    error = (page.error_message or "").splitlines()
    return {
        "id": page.pk,
        "number": page.number,
        "book_id": page.book_id,
        "status": page.status,
        "status_label": page.get_status_display(),
        "is_reviewed": page.status in REVIEWED_STATUSES,
        "text_state": page.text_state,
        "printed_number": page.printed_number,
        "n_unresolved": page.n_unresolved,
        "reading": dict(page.reading or {}),
        "error": error[0] if error and page.status == Page.Status.ERROR else "",
        "error_from": page.error_from if page.status == Page.Status.ERROR else "",
    }


def page_counts(page: Page) -> dict:
    """The page's marks: `low_total` (low words outside groups, one per group, every suggestion of its
    lines, decided or not), `unresolved` (the open ones: words + groups + gaps, `page_open_items`),
    `resolved` and the open ones by kind."""
    token_lists = list(page.lines.values_list("tokens", flat=True))
    items = page_open_items(page)
    low, groups = 0, set()
    for tokens in token_lists:
        for token in tokens or []:
            if token.get("conf") != "low":
                continue
            group = group_of(token)
            if group is None:
                low += 1
            else:
                groups.add(group)
    low += len(groups) + TextGap.objects.filter(page=page, line__isnull=False).count()
    return {
        "low_total": low,
        "unresolved": items.total,
        "resolved": low - items.total,
        "words": items.words,
        "groups": items.groups,
        "gaps": items.gaps,
    }


def book_unresolved_total(book_id: int) -> int:
    """Unresolved words over the non-excluded pages of a book."""
    total = Page.objects.filter(book_id=book_id, is_excluded=False).aggregate(n=Sum("n_unresolved"))["n"]
    return int(total or 0)


def mutation_counts(page: Page, line: Line | None = None) -> dict:
    """`counts` of the mutation responses: the line's open items (`n_low`), the page's (`page_counts`)
    and the book total."""
    counts = page_counts(page)
    return {
        "line_n_low": line.n_low if line is not None else 0,
        "page_unresolved": counts["unresolved"],
        "page_low_total": counts["low_total"],
        "book_unresolved_total": book_unresolved_total(page.book_id),
        "page_words": counts["words"],
        "page_groups": counts["groups"],
        "page_gaps": counts["gaps"],
    }


def review_url(page: Page) -> str:
    """URL of the review screen of `page`."""
    return reverse("review:page", args=[page.book_id, page.number])


def next_review_url(book_id: int, after: int | None = None) -> str:
    """URL of `review:next` (optionally after page number `after`)."""
    url = reverse("review:next", args=[book_id])
    return f"{url}?after={after}" if after is not None else url


def _id_template(name: str, kwarg: str) -> str:
    """An API URL with `__id__` in place of the id (the client fills it in)."""
    return reverse(f"api:{name}", kwargs={kwarg: 0}).replace("/0/", "/__id__/", 1)


def review_neighbours(page: Page) -> tuple[Page | None, Page | None]:
    """Previous / next non-excluded pages of the book (None at the ends)."""
    siblings = Page.objects.filter(book_id=page.book_id, is_excluded=False).only("id", "number", "book_id")
    previous = siblings.filter(number__lt=page.number).order_by("-number").first()
    following = siblings.filter(number__gt=page.number).order_by("number").first()
    return previous, following


def parse_origin(source, at=None, block=None) -> dict | None:
    """Review's origin from `?from=&at=&block=` (D76): `{from, at, block, query}`, or None when `from` is not
    one of `ORIGINS`. `at` (the book page's page) must be a positive int and `block` a block id (`[hpn]n`);
    anything else is dropped. `query` is the origin as a query string (review URLs carry it)."""
    if not isinstance(source, str) or source not in ORIGINS:
        return None
    number = None
    if isinstance(at, int) and not isinstance(at, bool):
        number = at if at > 0 else None
    elif isinstance(at, str) and at.isascii() and at.isdigit():
        number = int(at) or None
    block_id = block if isinstance(block, str) and _RE_ORIGIN_BLOCK.fullmatch(block) else None
    params = {"from": source}
    if number is not None:
        params["at"] = number
    if block_id is not None:
        params["block"] = block_id
    return {"from": source, "at": number, "block": block_id, "query": urlencode(params)}


def with_origin(url: str | None, origin: dict | None) -> str | None:
    """`url` with the origin's query appended (unchanged without an origin)."""
    if not url or not origin:
        return url
    return f"{url}{'&' if '?' in url else '?'}{origin['query']}"


def origin_back(book_id: int, origin: dict | None) -> dict | None:
    """The top bar's link back (`nav.back`): «الكتاب» → the book page on its page (`#page-<at>`),
    «المخطوطة» → the manuscript on its block (`#block-<id>`), «الإخراج» → the export page."""
    if not origin:
        return None
    source = origin["from"]
    if source == "book":
        url = reverse("editor:layout", args=[book_id]) + (f"#page-{origin['at']}" if origin["at"] else "")
    elif source == "manuscript":
        url = reverse("assembly:manuscript", args=[book_id]) + (
            f"#block-{origin['block']}" if origin["block"] else ""
        )
    else:
        url = reverse("publishing:export", args=[book_id])
    return {"from": source, "label": ORIGINS[source], "url": url}


RESOLVED_WORDS = ("كلمة واحدة", "كلمتين", "كلمات", "كلمة")
NEXT_ASSEMBLE = "تُجمَع الصفحات في نص واحد متّصل: فصول وفقرات وحواشٍ، تتحقّق من بنيته قبل الكتاب."


def next_step(book: Book, current: Page | None = None, facts=None) -> dict | None:
    """The end-of-review panel (D76, §5.3): what comes after review, from the stage bar's facts
    (`books.services.StageFacts`); None while a page other than `current` waits for review (N or approval
    has somewhere to go). `{state, heading, summary, title, text, button, url, stay}` with `state` one of
    `processing` (pages still being read), `assemble` (no manuscript), `reassemble` (out of date, not
    edited), `changes` (edited, with content drift) and `book` (fresh). Nine queries at most."""
    from books.services import PAGES_NOUN, PAGES_OF, StageFacts  # other app: lazy import

    facts = facts if facts is not None else StageFacts(book)
    others = [n for n in facts.pending if current is None or n != current.number]
    if others:
        return None
    resolved = LineRevision.objects.filter(
        page__book_id=book.pk, action=LineRevision.Action.RESOLVE, undone=False
    ).count()
    summary = ar_count(facts.reviewed, PAGES_NOUN)
    if resolved:
        summary = f"{summary} · حُسمت {ar_count(resolved, RESOLVED_WORDS)}"
    if facts.reviewed == facts.total:
        heading = "رُوجعت كل الصفحات"
    elif current is not None and facts.pending == [current.number]:
        heading = "هذه آخر صفحة بانتظار المراجعة"
    else:
        heading = "رُوجعت كل الصفحات الجاهزة"
    state = facts.manuscript
    manuscript_url = reverse("assembly:manuscript", args=[book.pk])
    layout_url = reverse("editor:layout", args=[book.pk])
    drift = state.get("drift_pages") or []
    if facts.processing:
        step = (
            "processing",
            "المعالجة",
            f"لا صفحات بانتظار المراجعة الآن؛ ما زالت {ar_count(facts.processing, PAGES_NOUN)} قيد المعالجة.",
            "العودة إلى المعالجة",
            reverse("books:detail", args=[book.pk]),
        )
    elif not state.get("exists"):
        step = ("assemble", "تجميع المخطوطة", NEXT_ASSEMBLE, "تجميع المخطوطة…", f"{manuscript_url}?convert=1")
    elif state.get("edited") and drift:
        theirs = "فقراتهما" if len(drift) == 2 else "فقراتها"
        step = (
            "changes",
            "أخذ التغييرات إلى الكتاب",
            f"غيّرت المراجعة نص {ar_count(len(drift), PAGES_OF)} من الكتاب المحرَّر؛ تُؤخذ {theirs} وحدها.",
            "عرض التغييرات في الكتاب",
            f"{layout_url}?tab=changes",
        )
    elif not state.get("edited") and state.get("stale"):
        changed = len(drift) or len(state.get("stale_pages") or [])
        step = (
            "reassemble",
            "إعادة التجميع",
            f"تغيّر نص {ar_count(changed, PAGES_OF)} في المراجعة بعد التجميع؛ لم يُحرَّر الكتاب بعد، "
            "فإعادة التجميع لا تُضيّع شيئًا.",
            "إعادة التجميع",
            manuscript_url,
        )
    else:
        step = ("book", "الكتاب", "المخطوطة محدَّثة؛ نسّق الكتاب وحرّره على صفحاته.", "فتح الكتاب", layout_url)
    key, title, text, button, url = step
    return {
        "state": key,
        "heading": heading,
        "summary": summary,
        "title": title,
        "text": text,
        "button": button,
        "url": url,
        "stay": "البقاء في المراجعة",
    }


def review_payload(page: Page, user, origin: dict | None = None, facts=None) -> dict:
    """Everything the review screen needs for one page (PHASE3_SPEC §4 shape; 7b's additions:
    `review/fixtures/trust/index.json`; 7c's: `nav.back`, `nav.origin`, `nav.detour` for the origin
    (`parse_origin`) and `next_step`, editor/fixtures/contract/; `facts`, the stage bar's
    `books.services.StageFacts` when the caller has them); `book.edited`: the book's text is edited on the
    book page, so a page's changes reach it through «تغييرات المراجعة» (D78)."""
    from books.services import StageFacts  # other app: lazy import

    book = page.book
    facts = facts if facts is not None else StageFacts(book)
    summary = book_review_summary(book)
    pre = _preprocess_of(page)
    lines = list(_page_lines(page).prefetch_related("gaps"))
    primary, secondary, _fast = engine_names()
    previous, following = review_neighbours(page)
    width = (pre.output_width if pre is not None else 0) or page.width
    height = (pre.output_height if pre is not None else 0) or page.height
    display = None
    if pre is not None:
        display = _file_url(pre.display_image) or _file_url(pre.gray_image)
    return {
        "page": page_item(page),
        "book": {
            "id": book.pk,
            "title": book.title,
            "total_pages": Page.objects.filter(book_id=book.pk).count(),
            "reviewed_pages": summary["reviewed"],
            "unresolved_total": summary["unresolved_total"],
            "edited": bool(facts.manuscript.get("edited")),
        },
        "image": {
            "display_url": display,
            "scan_url": _file_url(page.original_image),
            "width": width,
            "height": height,
        },
        "regions": [
            {"id": region.pk, "kind": region.kind, "label": region.get_kind_display(), "bbox": region.bbox}
            for region in page.regions.order_by("order", "id")
        ],
        "lines": [line_item(line) for line in lines],
        "counts": page_counts(page),
        "labels": {
            "primary": ENGINE_LABELS.get(primary, primary),
            "secondary": ENGINE_LABELS.get(secondary, secondary),
        },
        "nav": {
            "prev_url": with_origin(review_url(previous), origin) if previous else None,
            "next_url": with_origin(review_url(following), origin) if following else None,
            "next_review_url": with_origin(next_review_url(book.pk, page.number), origin),
            "dashboard_url": reverse("books:detail", args=[book.pk]),
            "back": origin_back(book.pk, origin),
            "origin": origin,
            "detour": bool(origin and origin["from"] == "book"),
        },
        "next_step": next_step(book, page, facts),
        "urls": {
            "payload": reverse("api:page_review", args=[page.pk]),
            "resolve": _id_template("line_resolve", "line_id"),
            "edit": _id_template("line_edit", "line_id"),
            "delete": _id_template("line_delete", "line_id"),
            "merge": _id_template("line_merge", "line_id"),
            "delete_word": _id_template("line_delete_word", "line_id"),
            "role": _id_template("line_role", "line_id"),
            "insert": reverse("api:page_lines", args=[page.pk]),
            "undo": reverse("api:page_undo", args=[page.pk]),
            "approve": reverse("api:page_approve", args=[page.pk]),
            "reopen": reverse("api:page_reopen", args=[page.pk]),
            "filmstrip": reverse("api:book_filmstrip", args=[book.pk]),
            "insertion": reverse("api:page_insertion", args=[page.pk, 0]).replace("/0/", "/__group__/", 1),
            "gap_accept": _id_template("gap_accept", "gap_id"),
            "gap_dismiss": _id_template("gap_dismiss", "gap_id"),
            "roles": reverse("api:page_roles", args=[page.pk]),
            # «تصحيح في كل الكتاب» (D79): the sheet's list, the correction and its undo (`__batch__`)
            "occurrences": reverse("api:book_occurrences", args=[book.pk]),
            "fix_everywhere": reverse("api:fix_everywhere", args=[book.pk]),
            "fix_everywhere_undo": reverse("api:fix_everywhere_undo", args=[book.pk, "__batch__"]),
        },
        "can_edit": can_review(user)
        and page.status in REVIEWABLE_STATUSES
        and page.text_state == Page.TextState.FINAL,
    }


def next_page_to_review(book: Book, after_number: int | None = None) -> Page | None:
    """First non-excluded `ocr_done` page after `after_number`, wrapping to the start; None when none."""
    pending = book.pages.filter(is_excluded=False, status=Page.Status.OCR_DONE).order_by("number")
    if after_number is None:
        return pending.first()
    return pending.filter(number__gt=after_number).first() or pending.exclude(number=after_number).first()


def pending_page(book: Book, number: int | None) -> Page | None:
    """Page `number` of the book when it still waits for review (non-excluded, `ocr_done`), else None."""
    if number is None:
        return None
    return book.pages.filter(number=number, is_excluded=False, status=Page.Status.OCR_DONE).first()


def book_review_summary(book: Book) -> dict:
    """Dashboard review numbers: `reviewed`, `total`, `pending`, `unresolved_total`, `next_review_url`.

    `next_review_url` (`review:next`) is set once a page waits for review (`ocr_done`), else None.
    """
    rows = list(book.pages.filter(is_excluded=False).values_list("status", "n_unresolved"))
    pending = sum(1 for status, _ in rows if status == Page.Status.OCR_DONE)
    return {
        "reviewed": sum(1 for status, _ in rows if status in REVIEWED_STATUSES),
        "total": len(rows),
        "pending": pending,
        "unresolved_total": sum(n for _, n in rows),
        "next_review_url": next_review_url(book.pk) if pending else None,
    }


def filmstrip(book: Book) -> list[dict]:
    """Per non-excluded page `{id, number, thumb_url, is_reviewed, n_unresolved, status, url, readers}`
    (one query; `readers` is `Page.reading["readers"]`, '' before 7b, D73)."""
    pages = (
        book.pages.filter(is_excluded=False)
        .select_related("preprocess")
        .only(
            "id",
            "number",
            "book_id",
            "status",
            "n_unresolved",
            "reading",
            "scan_thumbnail",
            "preprocess__thumbnail",
        )
        .order_by("number")
    )
    out = []
    for page in pages:
        pre = _preprocess_of(page)
        thumb = (_file_url(pre.thumbnail) if pre is not None else None) or _file_url(page.scan_thumbnail)
        out.append(
            {
                "id": page.pk,
                "number": page.number,
                "thumb_url": thumb,
                "is_reviewed": page.status in REVIEWED_STATUSES,
                "n_unresolved": page.n_unresolved,
                "status": page.status,
                "url": review_url(page),
                "readers": readers_of(page.reading),
            }
        )
    return out


# ====================================================================== line actions


def _as_index(index, size: int) -> int:
    """Validated token index (ints and digit strings accepted)."""
    if isinstance(index, bool):
        raise ReviewError("رقم الكلمة غير صالح.")
    try:
        value = int(str(index).strip())
    except (TypeError, ValueError):
        raise ReviewError("رقم الكلمة غير صالح.") from None
    if not 0 <= value < size:
        raise ReviewError("رقم الكلمة غير صالح.")
    return value


def _clean_typed(text: str | None) -> str:
    """A typed correction: stripped, inner whitespace collapsed, one word or a short phrase."""
    words = _clean_words(text)
    if not words:
        raise ReviewError("اكتب التصحيح أولًا.")
    value = " ".join(words)
    if len(words) > MAX_TYPED_WORDS or len(value) > MAX_TYPED_CHARS:
        raise ReviewError("التصحيح طويل؛ لتعديل أكثر من كلمات قليلة عدّل السطر كاملًا (E).")
    return value


@transaction.atomic
def resolve_token(
    line: Line, index, choice: str, text: str | None = None, user=None, expected: str | None = None
) -> Line:
    """Resolve token `index` of `line` with a reading: `primary`, `secondary`, `tess`, `typed` or `sug`.

    `primary` keeps the primary model's reading (restores it when another reading was chosen
    before, the vote's included), `secondary` takes `alt` (with the vote, D71, the reading already
    in the text: Enter confirms it), `tess` takes Tesseract's word, `typed` takes `text`, `sug` the
    year read from the number in words (§4.8). Sets `res` to the choice (the token's `conf` is
    kept); a suggestion after the word follows its new reading (`_follow_words`). `expected` is the
    word the client saw at `index`; when it is not there any more,
    `ReviewConflict` (the indices moved). Raises `ReviewError` (Arabic) on a bad index / choice or a
    missing alternative.
    """
    page = _lock_page(line.page)
    _check_editable(page)
    line = _line_of(page, line.pk)
    if line is None:
        raise ReviewError("السطر غير موجود في هذه الصفحة.")
    tokens = [normalize_token(token) for token in line.tokens or []]
    i = _as_index(index, len(tokens))
    _check_word(line, tokens, i, expected)
    if choice not in CHOICES:
        raise ReviewError("اختيار غير معروف.")
    token = tokens[i]
    if choice == "primary":
        new = token.get("orig") or token["t"]
    elif choice == "secondary":
        if not token.get("alt"):
            raise ReviewError("لا توجد قراءة للنموذج الثاني لهذه الكلمة.")
        new = token["alt"]
    elif choice == "tess":
        if not token.get("tess"):
            raise ReviewError("لا توجد قراءة Tesseract لهذه الكلمة.")
        new = token["tess"]
    elif choice == "sug":
        suggestion = token.get("sug")
        if not isinstance(suggestion, dict) or not suggestion.get("t"):
            raise ReviewError("لا توجد قراءة مقترحة لهذه الكلمة.")
        new = str(suggestion["t"])
    else:
        new = _clean_typed(text)
    before = line_snapshot(line)
    if new != token["t"]:
        token.setdefault("orig", token["t"])
        token["t"] = new
    token["res"] = choice
    _set_tokens(line, tokens)
    line.updated_by = _user_or_none(user)
    line.save(update_fields=["tokens", "text", "n_low", "updated_by", "updated_at"])
    _follow_words(line)
    _record(page, LineRevision.Action.RESOLVE, line, before, line_snapshot(line), user)
    refresh_page_text(page)
    return line


def retokenize(old_tokens: list[dict], text: str) -> list[dict]:
    """Tokens for a line edited to `text`, aligned to its old tokens (`ocr.alignment.align_tokens`).

    A token whose text is unchanged keeps its whole dict (bbox, conf, res, alternatives); a changed
    token paired one-to-one with an old token becomes a typed token with the old bbox (and its weak
    mark, `bq`); an added token becomes a typed token without a box.
    """
    old = [normalize_token(token) for token in old_tokens or []]
    words = _clean_words(text)
    out: list[dict | None] = [None] * len(words)
    for i, j in align_tokens([token["t"] for token in old], words):
        if j is None:
            continue
        if i is not None and old[i]["t"] == words[j]:
            out[j] = old[i]
        else:
            out[j] = (
                typed_token(words[j], old[i]["bbox"], old[i].get("bq"))
                if i is not None
                else typed_token(words[j])
            )
    return [token if token is not None else typed_token(words[j]) for j, token in enumerate(out)]


@transaction.atomic
def edit_line(line: Line, text: str, user=None, version: str | None = None) -> Line:
    """Replace the text of a whole line; unchanged words keep their boxes and resolutions.

    Words are separated by whitespace (collapsed to single spaces). `ocr_text` never changes. The
    line's open suggestions follow the words they come after (`_shift_gaps`, the same alignment).
    Empty text is refused (delete the line instead); an unchanged text is a no-op (no revision).
    `version` is the line's `v` as the client read it; a line saved since is a `ReviewConflict`.
    """
    page = _lock_page(line.page)
    _check_editable(page)
    line = _line_of(page, line.pk)
    if line is None:
        raise ReviewError("السطر غير موجود في هذه الصفحة.")
    _check_version(line, version)
    words = _clean_words(text)
    if not words:
        raise ReviewError("السطر فارغ؛ لحذفه استخدم «حذف السطر».")
    if len(" ".join(words)) > MAX_LINE_CHARS:
        raise ReviewError("السطر أطول من المسموح.")
    if [normalize_token(token)["t"] for token in line.tokens or []] == words:
        return line
    before = line_snapshot(line)
    old_tokens = list(line.tokens or [])
    _set_tokens(line, retokenize(old_tokens, " ".join(words)))
    line.updated_by = _user_or_none(user)
    line.save(update_fields=["tokens", "text", "n_low", "updated_by", "updated_at"])
    _shift_gaps(line, _aligned(old_tokens, line.tokens), line.tokens)
    _record(page, LineRevision.Action.EDIT, line, before, line_snapshot(line), user)
    refresh_page_text(page)
    return line


@transaction.atomic
def insert_line(page: Page, after_line_id: int | None, text: str, user=None) -> Line:
    """Insert a reviewer-typed line below `after_line_id` (at the top when None).

    The new line takes the region of the line above (or the page's first body region), has no box,
    typed tokens and `is_manual=True`; the lines below move down by one. On an approved page the
    new line is marked reviewed like the others.
    """
    page = _lock_page(page)
    _check_editable(page)
    words = _clean_words(text)
    if not words:
        raise ReviewError("اكتب نص السطر أولًا.")
    if len(" ".join(words)) > MAX_LINE_CHARS:
        raise ReviewError("السطر أطول من المسموح.")
    if after_line_id in (None, ""):
        order = 0
        region = page.regions.filter(kind=Region.Kind.BODY).order_by("order", "id").first()
    else:
        try:
            above = _line_of(page, int(after_line_id))
        except (TypeError, ValueError):
            above = None
        if above is None:
            raise ReviewError("السطر المحدّد غير موجود في هذه الصفحة.")
        order = above.order + 1
        region = above.region
    page.lines.filter(order__gte=order).update(order=F("order") + 1)
    line = Line(
        page=page,
        order=order,
        region=region,
        bbox=None,
        ocr_text="",
        confidence=1.0,
        is_manual=True,
        is_reviewed=page.status in REVIEWED_STATUSES,
        updated_by=_user_or_none(user),
    )
    _set_tokens(line, [typed_token(word) for word in words])
    line.save()
    _compact_orders(page)
    line.refresh_from_db()
    _record(page, LineRevision.Action.INSERT, line, None, line_snapshot(line), user)
    refresh_page_text(page)
    return line


@transaction.atomic
def delete_line(line: Line, user=None, version: str | None = None) -> int:
    """Delete a line (typically a manual or garbage one); the lines below move up. Returns its id.

    `version`: as in `edit_line` (a line saved since the client read it is a `ReviewConflict`).
    """
    page = _lock_page(line.page)
    _check_editable(page)
    line = _line_of(page, line.pk)
    if line is None:
        raise ReviewError("السطر غير موجود في هذه الصفحة.")
    _check_version(line, version)
    line_id = line.pk
    _record(page, LineRevision.Action.DELETE, None, line_snapshot(line), None, user)
    line.delete()
    _compact_orders(page)
    refresh_page_text(page)
    return line_id


def _union_box(a: list | None, b: list | None) -> list | None:
    """Union of two word boxes (None when neither has one)."""
    boxes = [box for box in (a, b) if isinstance(box, list | tuple) and len(box) == 4]
    if not boxes:
        return None
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


@transaction.atomic
def merge_tokens(
    line: Line, index, user=None, expected: str | None = None, expected_next: str | None = None
) -> Line:
    """Join word `index` with the word after it (reading order) into one word, e.g. «هير» + «ودوت».

    For a name or place the models split in two (D31). The two readings are written together without
    a space; the merged word's box is the union of both boxes (weak when one of them was) and it
    counts as the reviewer's decision (typed, high confidence, no alternatives); a suggestion between
    the two follows the merged word. Undo restores both words. `expected` / `expected_next` are the
    two words the client saw (`ReviewConflict` when they moved). Raises `ReviewError` when there is
    no word after `index` on the line.
    """
    page = _lock_page(line.page)
    _check_editable(page)
    line = _line_of(page, line.pk)
    if line is None:
        raise ReviewError("السطر غير موجود في هذه الصفحة.")
    tokens = [normalize_token(token) for token in line.tokens or []]
    i = _as_index(index, len(tokens))
    _check_word(line, tokens, i, expected)
    _check_word(line, tokens, i + 1, expected_next)
    if i + 1 >= len(tokens):
        raise ReviewError("لا توجد كلمة بعدها في هذا السطر للدمج.")
    before = line_snapshot(line)
    first, second = tokens[i], tokens[i + 1]
    weak = any(t.get("bbox") and t.get("bq") == WEAK for t in (first, second))
    merged = typed_token(
        first["t"] + second["t"], _union_box(first.get("bbox"), second.get("bbox")), WEAK if weak else None
    )
    tokens[i : i + 2] = [merged]
    _set_tokens(line, tokens)
    line.updated_by = _user_or_none(user)
    line.save(update_fields=["tokens", "text", "n_low", "updated_by", "updated_at"])
    _shift_gaps(line, {k: k if k <= i else k - 1 for k in range(len(tokens) + 1)}, line.tokens)
    _record(page, LineRevision.Action.MERGE, line, before, line_snapshot(line), user)
    refresh_page_text(page)
    return line


@transaction.atomic
def delete_token(line: Line, index, user=None, expected: str | None = None) -> dict:
    """Remove one stray word (a lone letter or number the OCR added) from the line.

    Returns `{"line": Line | None, "deleted_line_id": int | None}`: removing the only word of a line
    deletes the line itself (recorded as a line delete, so undo brings the line back). Undo restores
    the word with its box and readings (and a suggestion after it to its place). `expected`: as in
    `resolve_token`.
    """
    page = _lock_page(line.page)
    _check_editable(page)
    line = _line_of(page, line.pk)
    if line is None:
        raise ReviewError("السطر غير موجود في هذه الصفحة.")
    tokens = [normalize_token(token) for token in line.tokens or []]
    i = _as_index(index, len(tokens))
    _check_word(line, tokens, i, expected)
    if len(tokens) == 1:
        line_id = line.pk
        _record(page, LineRevision.Action.DELETE, None, line_snapshot(line), None, user)
        line.delete()
        _compact_orders(page)
        refresh_page_text(page)
        return {"line": None, "deleted_line_id": line_id}
    before = line_snapshot(line)
    size = len(tokens)
    del tokens[i]
    _set_tokens(line, tokens)
    line.updated_by = _user_or_none(user)
    line.save(update_fields=["tokens", "text", "n_low", "updated_by", "updated_at"])
    _shift_gaps(line, {k: k if k < i else k - 1 for k in range(size) if k != i}, line.tokens)
    _record(page, LineRevision.Action.DROP_WORD, line, before, line_snapshot(line), user)
    refresh_page_text(page)
    return {"line": line, "deleted_line_id": None}


def stored_role(choice: str, region_kind: str | None) -> str:
    """The `Line.Role` stored for an effective choice of «نوع السطر» (D74) on a line of `region_kind`.

    On a footnote-region line «حاشية» (`footnote`) is its region's own kind and stores `body`, and
    «محتوى» (`body`) pulls it into the body (`main`); on any other line «حاشية» stores `footnote`.
    Headings and verse store themselves everywhere. Raises `ReviewError` for an unknown choice.
    """
    if choice not in ROLE_CHOICES:
        raise ReviewError("نوع السطر غير معروف.")
    if region_kind in FOOTNOTE_KINDS:
        if choice == Line.Role.FOOTNOTE:
            return Line.Role.BODY
        if choice == Line.Role.BODY:
            return Line.Role.MAIN
    return choice


def _apply_role(page: Page, line: Line, role: str, user, batch: uuid.UUID | None = None) -> bool:
    """Store `role` (a stored value) on `line` with its revision; False when it already had it."""
    if line.role == role:
        return False
    before = line_snapshot(line)
    line.role = role
    line.updated_by = _user_or_none(user)
    line.save(update_fields=["role", "updated_by", "updated_at"])
    _record(page, LineRevision.Action.ROLE, line, before, line_snapshot(line), user, batch)
    return True


@transaction.atomic
def set_line_role(line: Line, role: str, user=None, version: str | None = None) -> Line:
    """Mark what a line is (D32, D74): «محتوى», «عنوان رئيسي», «عنوان فرعي», «شعر» or «حاشية».

    `role` is the effective choice the menu shows (`ROLE_CHOICES`); `stored_role` maps it by the
    line's region, so a footnote-region line can become a heading, verse or body text («محتوى»), and
    a body-region line a note. Assembly reads the result through `ocr.services.line_kind`; the page
    text moves a note line among the footnotes. An unchanged role records nothing; undo restores the
    previous role. Raises `ReviewError` (Arabic) for an unknown role and `ReviewConflict` for a stale
    `version` (as in `edit_line`).
    """
    page = _lock_page(line.page)
    _check_editable(page)
    line = _line_of(page, line.pk)
    if line is None:
        raise ReviewError("السطر غير موجود في هذه الصفحة.")
    _check_version(line, version)
    if _apply_role(page, line, stored_role(role, _region_kind(line)), user):
        refresh_page_text(page)
    return line


@transaction.atomic
def set_roles(page: Page, line_ids, role: str, user=None) -> list[Line]:
    """«نوع الأسطر المحدَّدة»: the same effective choice on a ⇧-click range of lines (D74).

    `line_ids` are lines of `page` (at most `MAX_RANGE_LINES`); each line stores `stored_role` of the
    choice for its own region, with one revision per changed line sharing a batch, so `undo_last`
    reverts the range in one step. Returns the lines in reading order. Raises `ReviewError` for an
    empty or foreign id list or an unknown role.
    """
    page = _lock_page(page)
    _check_editable(page)
    if not isinstance(line_ids, list | tuple) or not line_ids:
        raise ReviewError("حدّد الأسطر أولًا.")
    try:
        parsed = [int(str(value).strip()) for value in line_ids if not isinstance(value, bool | dict | list)]
    except (TypeError, ValueError):
        raise ReviewError("الأسطر المحدّدة غير صالحة.") from None
    ids = list(dict.fromkeys(parsed))
    if len(parsed) != len(line_ids) or len(ids) > MAX_RANGE_LINES:
        raise ReviewError("الأسطر المحدّدة غير صالحة.")
    if role not in ROLE_CHOICES:
        raise ReviewError("نوع السطر غير معروف.")
    lines = list(_page_lines(page).filter(pk__in=ids))
    if len(lines) != len(ids):
        raise ReviewError("بعض الأسطر المحدّدة ليست في هذه الصفحة.")
    batch = uuid.uuid4()
    changed = False
    for line in lines:
        changed |= _apply_role(page, line, stored_role(role, _region_kind(line)), user, batch)
    if changed:
        refresh_page_text(page)
    return lines


# ====================================================================== words only the second model read


def _group_lines(page: Page, group: int) -> list[Line]:
    """The lines of `page` holding words of insertion group `group` (D72), in reading order."""
    return [
        line for line in _page_lines(page) if any(group_of(token) == group for token in line.tokens or [])
    ]


@transaction.atomic
def resolve_insertion(page: Page, group, keep: bool, user=None) -> dict:
    """Keep or drop a group of words only the second model read (D72), on every line it covers.

    Keep sets `res = "secondary"` on the group's open words (their text stays); drop removes the
    group's words, and a line left without words is deleted. One revision per line (`resolve`,
    `drop_word` or `delete`), all in one batch: `undo_last` brings the whole group back in one step.
    Suggestions on those lines follow the words around them. Returns `{"lines": [changed lines],
    "deleted_ids": [ids of the lines removed]}`. Raises `ReviewError` for an unknown group.
    """
    page = _lock_page(page)
    _check_editable(page)
    try:
        number = int(str(group).strip()) if not isinstance(group, bool) else None
    except (TypeError, ValueError):
        number = None
    lines = _group_lines(page, number) if number is not None else []
    if not lines:
        raise ReviewError("لم تعد هذه الكلمات المقترحة في الصفحة.")
    batch = uuid.uuid4()
    changed: list[Line] = []
    deleted: list[int] = []
    for line_id in [line.pk for line in lines]:
        line = _line_of(page, line_id)  # fresh: an earlier line of the group may have been deleted
        tokens = [normalize_token(token) for token in line.tokens or []]
        before = line_snapshot(line)
        if keep:
            touched = False
            for token in tokens:
                if group_of(token) == number and not token.get("res"):
                    token["res"] = "secondary"
                    touched = True
            if not touched:
                continue
            _set_tokens(line, tokens)
            line.updated_by = _user_or_none(user)
            line.save(update_fields=["tokens", "text", "n_low", "updated_by", "updated_at"])
            _record(page, LineRevision.Action.RESOLVE, line, before, line_snapshot(line), user, batch)
            changed.append(line)
            continue
        kept = [k for k, token in enumerate(tokens) if group_of(token) != number]
        if not kept:
            deleted.append(line.pk)
            _record(page, LineRevision.Action.DELETE, None, before, None, user, batch)
            line.delete()
            _compact_orders(page)  # as `delete_line`: the next snapshot holds the order undo needs
            continue
        _set_tokens(line, [tokens[k] for k in kept])
        line.updated_by = _user_or_none(user)
        line.save(update_fields=["tokens", "text", "n_low", "updated_by", "updated_at"])
        _shift_gaps(line, {k: n for n, k in enumerate(kept)}, line.tokens)
        _record(page, LineRevision.Action.DROP_WORD, line, before, line_snapshot(line), user, batch)
        changed.append(line)
    refresh_page_text(page)
    for line in changed:
        line.refresh_from_db()
    return {"lines": changed, "deleted_ids": deleted}


# ====================================================================== suggestions (TextGap, D72)


def _gap_of(page: Page, gap_id) -> TextGap:
    """An open suggestion of `page` that still has its line (`ReviewError` otherwise)."""
    gap = TextGap.objects.select_related("line").filter(pk=gap_id, page=page).first()
    if gap is None or gap.line_id is None:
        raise ReviewError("لم يعد هذا النص المقترح في الصفحة.")
    if gap.status != TextGap.Status.OPEN:
        raise ReviewError("حُسم هذا النص المقترح من قبل.")
    return gap


def gap_anchor(tokens: list[dict], index: int, after_t: str) -> int:
    """Where a suggestion goes now (−1: before the first token): after token `index` when it still
    reads `after_t`, else after the token reading `after_t` nearest to `index`, else `index` clamped to
    the line."""
    size = len(tokens)
    if index < 0 or not size:
        return -1
    if index < size and str(tokens[index].get("t") or "") == after_t:
        return index
    places = [k for k, token in enumerate(tokens) if after_t and str(token.get("t") or "") == after_t]
    if places:
        return min(places, key=lambda k: (abs(k - index), k))
    return min(index, size - 1)


def _decide_gap(gap: TextGap, status: str, user) -> None:
    gap.status = status
    gap.decided_by = _user_or_none(user)
    gap.decided_at = timezone.now()
    gap.save(update_fields=["status", "decided_by", "decided_at"])


@transaction.atomic
def accept_gap(gap: TextGap, text: str | None = None, user=None) -> tuple[Line, TextGap]:
    """Insert a suggestion's words into its line (D72): «إدراج».

    The words (the offered text, or `text` typed over it, whitespace collapsed) become typed tokens
    after the gap's token, re-anchored by `after_t` (`gap_anchor`); the line's other suggestions
    after that place move with the words. The gap becomes `inserted`. Recorded as an `edit` whose
    `after` holds `{"gap": id}`; undo removes the words and reopens the gap. Returns `(line, gap)`.
    """
    page = _lock_page(gap.page)
    _check_editable(page)
    gap = _gap_of(page, gap.pk)
    line = _line_of(page, gap.line_id)
    words = _clean_words(gap.text if text is None else text)
    if not words:
        raise ReviewError("اكتب النص المُدرَج أولًا.")
    tokens = [normalize_token(token) for token in line.tokens or []]
    if len(" ".join([token["t"] for token in tokens] + words)) > MAX_LINE_CHARS:
        raise ReviewError("السطر أطول من المسموح.")
    at = gap_anchor(tokens, gap.index, gap.after_t)
    before = line_snapshot(line)
    new = tokens[: at + 1] + [typed_token(word) for word in words] + tokens[at + 1 :]
    _set_tokens(line, new)
    line.updated_by = _user_or_none(user)
    line.save(update_fields=["tokens", "text", "n_low", "updated_by", "updated_at"])
    _decide_gap(gap, TextGap.Status.INSERTED, user)
    shift = {k: k if k <= at else k + len(words) for k in range(len(tokens))}
    _shift_gaps(line, shift, line.tokens)
    _record(page, LineRevision.Action.EDIT, line, before, {**line_snapshot(line), "gap": gap.pk}, user)
    refresh_page_text(page)
    return line, gap


@transaction.atomic
def dismiss_gap(gap: TextGap, user=None) -> tuple[Line, TextGap]:
    """Drop a suggestion (D72): «تجاهل». The text is unchanged; the revision (action `gap` «نص
    مقترح») holds the gap's status before and after, and undo reopens it. Returns `(line, gap)`."""
    page = _lock_page(gap.page)
    _check_editable(page)
    gap = _gap_of(page, gap.pk)
    line = _line_of(page, gap.line_id)
    _decide_gap(gap, TextGap.Status.DISMISSED, user)
    _record(
        page,
        LineRevision.Action.GAP,
        line,
        {"gap": gap.pk, "status": TextGap.Status.OPEN},
        {"gap": gap.pk, "status": TextGap.Status.DISMISSED},
        user,
    )
    refresh_page_text(page)
    return line, gap


# ====================================================================== undo


def _restore_content(page: Page, revision: LineRevision) -> None:
    """Undo a resolve / edit: the line's text, tokens, box, role and suggestions go back to the
    `before` snapshot (a suggestion the edit inserted is open again)."""
    snap = revision.before or {}
    line = _line_of(page, revision.line_id or snap.get("id"))
    if line is None:
        raise ReviewError("تعذّر التراجع: السطر لم يعد موجودًا.")
    line.bbox = snap.get("bbox")
    _set_tokens(line, [normalize_token(token) for token in snap.get("tokens") or []])
    line.text = snap.get("text", line.text)
    line.role = snap.get("role") or line.role
    line.save(update_fields=["bbox", "tokens", "text", "n_low", "role", "updated_at"])
    _restore_gaps(page, line, snap.get("gaps"))
    gap_id = (revision.after or {}).get("gap")
    if gap_id is not None:  # an accepted suggestion (`accept_gap`) is open again
        _restore_gaps(page, line, {str(gap_id): {"status": TextGap.Status.OPEN}})


def _undo_insert(page: Page, revision: LineRevision) -> None:
    """Undo an insert: the inserted line goes away and the lines below move back up."""
    line = _line_of(page, revision.line_id or (revision.after or {}).get("id"))
    if line is not None:
        line.delete()
    _compact_orders(page)


def _undo_delete(page: Page, revision: LineRevision) -> None:
    """Undo a delete: the line comes back with its old id, order, region, tokens and suggestions."""
    snap = revision.before or {}
    order = int(snap.get("order") or 0)
    region_id = snap.get("region_id")
    if region_id is not None and not Region.objects.filter(pk=region_id, page=page).exists():
        region_id = None
    page.lines.filter(order__gte=order).update(order=F("order") + 1)
    line_id = snap.get("id")
    line = Line(
        page=page,
        order=order,
        region_id=region_id,
        bbox=snap.get("bbox"),
        ocr_text=snap.get("ocr_text") or "",
        confidence=snap.get("confidence", 1.0),
        is_manual=bool(snap.get("is_manual")),
        is_reviewed=bool(snap.get("is_reviewed")),
        role=snap.get("role") or Line.Role.BODY,
    )
    if line_id is not None and not Line.objects.filter(pk=line_id).exists():
        line.pk = line_id
    _set_tokens(line, [normalize_token(token) for token in snap.get("tokens") or []])
    line.text = snap.get("text", line.text)
    line.save(force_insert=True)
    _restore_gaps(page, line, snap.get("gaps"))
    _compact_orders(page)
    if line_id is not None:
        # Older revisions of this line lost their link when it was deleted; point them at it again.
        page.revisions.filter(line__isnull=True).filter(Q(before__id=line_id) | Q(after__id=line_id)).exclude(
            action__in=[LineRevision.Action.APPROVE, LineRevision.Action.REOPEN]
        ).update(line=line)


def _undo_gap(page: Page, revision: LineRevision) -> None:
    """Undo a dismissed suggestion: it is open again, on the line it had."""
    snap = revision.before or {}
    gap_id = snap.get("gap")
    if gap_id is not None:
        _restore_gaps(page, None, {str(gap_id): {"status": snap.get("status") or TextGap.Status.OPEN}})


def _undo_one(page: Page, revision: LineRevision) -> None:
    """Revert one revision (see `undo_last`) and mark it undone."""
    action = revision.action
    if action in (
        LineRevision.Action.RESOLVE,
        LineRevision.Action.EDIT,
        LineRevision.Action.MERGE,
        LineRevision.Action.DROP_WORD,
        LineRevision.Action.ROLE,
        LineRevision.Action.FIX,
    ):
        _restore_content(page, revision)
    elif action == LineRevision.Action.INSERT:
        _undo_insert(page, revision)
    elif action == LineRevision.Action.DELETE:
        _undo_delete(page, revision)
    elif action == LineRevision.Action.GAP:
        _undo_gap(page, revision)
    else:  # approve / reopen
        _restore_page_state(page, revision.before or {})
    revision.undone = True
    revision.save(update_fields=["undone"])


@transaction.atomic
def undo_last(page: Page, user=None, origin: dict | None = None) -> dict:
    """Revert the newest action of the page that is not undone yet and mark its revisions undone.

    Resolve / edit restore the line's previous text and tokens, an insert is removed, a deleted
    line comes back at its old position, approve / reopen restore the page's review status and
    the lines' reviewed flags, a suggestion accepted or dismissed is open again. An action over
    several lines (a group kept or dropped, a range of roles: one `LineRevision.batch`) is undone at
    once, newest revision first. Returns the new `review_payload`. Raises `ReviewError`
    «لا شيء للتراجع عنه» when nothing is left to undo.
    """
    page = _lock_page(page)
    revision = page.revisions.filter(undone=False).order_by("-created_at", "-id").first()
    if revision is None:
        raise ReviewError("لا شيء للتراجع عنه.")
    _check_editable(page)
    action = revision.action
    if action not in (LineRevision.Action.APPROVE, LineRevision.Action.REOPEN):
        _touch_page(page)  # D36; approve / reopen restore their own status snapshot below
    if revision.batch is not None:
        batch = list(page.revisions.filter(undone=False, batch=revision.batch).order_by("-created_at", "-id"))
    else:
        batch = [revision]
    for item in batch:
        _undo_one(page, item)
    refresh_page_text(page)
    if action in (LineRevision.Action.APPROVE, LineRevision.Action.REOPEN):
        _refresh_book(page)
    page.refresh_from_db()
    return review_payload(page, user, origin)


# ====================================================================== approve / reopen


def _next_after(page: Page, origin: dict | None = None) -> dict:
    """Where to go after `page` was approved: the next page's review / payload URLs (None when none) and,
    when no page is next, the end-of-review panel (`next_step`, D76); the URLs carry the origin."""
    book = Book.objects.get(pk=page.book_id)
    following = next_page_to_review(book, after_number=page.number)
    return {
        # Always a URL: the next page's review screen, or `review:next`, which redirects to the
        # dashboard with «لا صفحات بانتظار المراجعة» when nothing is left.
        "next_review_url": with_origin(
            review_url(following) if following else next_review_url(book.pk, page.number), origin
        ),
        "next_payload_url": reverse("api:page_review", args=[following.pk]) if following else None,
        "next_number": following.number if following else None,
        "dashboard_url": reverse("books:detail", args=[book.pk]),
        "next_step": None if following else next_step(book, page),
    }


@transaction.atomic
def approve_page(page: Page, user, force: bool = False, origin: dict | None = None) -> dict:
    """Mark a page reviewed. Returns `{"status", "next_review_url", "next_payload_url", ..., "next_step"}`
    (`origin`, review's `parse_origin`, is carried by the URLs).

    With open items left (unresolved words, open groups of added words, open suggestions:
    `ocr.services.page_open_items`) and `force` false, raises `ReviewBlocked` (API 409) with their
    split. Every line is marked reviewed (so a new OCR pass keeps them), `reviewed_by` /
    `reviewed_at` are set and the book status is re-derived. A suggestion left open never enters the
    text. Approving an approved (`reviewed` or `assembled`) page changes nothing.
    """
    page = _lock_page(page)
    if page.status in REVIEWED_STATUSES:
        return {"status": page.status, **_next_after(page, origin)}
    if page.status != Page.Status.OCR_DONE or page.text_state != Page.TextState.FINAL:
        raise ReviewError("الصفحة ليست جاهزة للاعتماد بعد.")
    items = page_open_items(page)
    if items.total and not force:
        raise ReviewBlocked(items)
    before = _page_state(page)
    page.status = Page.Status.REVIEWED
    page.reviewed_by = _user_or_none(user)
    page.reviewed_at = timezone.now()
    page.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    page.lines.update(is_reviewed=True)
    _record(page, LineRevision.Action.APPROVE, None, before, _page_state(page), user)
    refresh_page_text(page)
    _refresh_book(page)
    return {"status": page.status, **_next_after(page, origin)}


@transaction.atomic
def reopen_page(page: Page, user) -> None:
    """Take an approved (`reviewed` or `assembled`) page back to `ocr_done` (lines unreviewed)."""
    page = _lock_page(page)
    if page.status not in REVIEWED_STATUSES:
        raise ReviewError("الصفحة غير معتمدة؛ لا حاجة لإعادة فتحها.")
    before = _page_state(page)
    page.status = Page.Status.OCR_DONE
    page.reviewed_by = None
    page.reviewed_at = None
    page.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    page.lines.update(is_reviewed=False)
    _record(page, LineRevision.Action.REOPEN, None, before, _page_state(page), user)
    _refresh_book(page)
