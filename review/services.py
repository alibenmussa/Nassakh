"""Review services: resolve uncertain words, edit / insert / delete lines, undo, approve (PHASE3 §5).

Every mutating service runs in one transaction on a locked page row, records a `LineRevision`
(snapshots before / after, which is what `undo_last` restores), recomputes the line's `n_low`
(unresolved tokens: `conf == "low"` and no `res`), the page's `n_unresolved` and rebuilds
`page.final_text` from the lines: body-like regions in order, a blank line, then the footnotes,
Western digits (D6). The printed page number is never part of the lines, so it never re-enters
the text. Tokens and texts keep their diacritics exactly as stored or typed.

Token keys: `t` (current reading), `alt` (secondary model), `tess` (Tesseract), `conf`, `digit`,
`bbox` (gray-image pixels), `res` (`None | primary | secondary | tess | typed | chooser`) and,
once a resolution changed `t`, `orig` (the primary model's reading, so «النموذج الأول» can be
chosen again after another reading).
"""

from __future__ import annotations

from datetime import datetime

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import F, Q, Sum
from django.urls import reverse
from django.utils import timezone

from books.models import Book, Page
from core.arabic import is_digit_token, to_western_digits
from core.decorators import ROLE_EDITOR, ROLE_PROOFREADER, has_role
from ocr.alignment import align_tokens
from ocr.models import Line
from ocr.services import ENGINE_LABELS, count_unresolved, engine_names, join_region_texts
from processing.models import Region

from .models import LineRevision

CHOICES: tuple[str, ...] = ("primary", "secondary", "tess", "typed")
REVIEWABLE_STATUSES: frozenset[str] = frozenset({Page.Status.OCR_DONE, Page.Status.REVIEWED})
REVIEWED_STATUSES: frozenset[str] = frozenset({Page.Status.REVIEWED, Page.Status.ASSEMBLED})
DEFAULT_REGION_KIND = Region.Kind.BODY  # lines without a region (text-layer pages) read as body
MAX_TYPED_WORDS = 6
MAX_TYPED_CHARS = 120
MAX_LINE_CHARS = 2000
TOKEN_KEYS: tuple[str, ...] = ("t", "alt", "tess", "conf", "digit", "bbox", "res")


class ReviewError(Exception):
    """A review action that cannot be applied; the message is Arabic and shown to the reviewer."""


class ReviewBlocked(ReviewError):
    """Approval refused because uncertain words remain (the API answers 409)."""

    def __init__(self, unresolved: int):
        self.unresolved = unresolved
        super().__init__(f"بقيت {unresolved} كلمة غير محسومة. اعتماد الصفحة رغم ذلك؟")


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


def typed_token(word: str, bbox: list | None = None) -> dict:
    """A token written by the reviewer: high confidence, `res = "typed"`, no alternatives."""
    return {
        "t": word,
        "alt": None,
        "tess": None,
        "conf": "high",
        "digit": is_digit_token(word),
        "bbox": bbox,
        "res": "typed",
    }


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
    """JSON snapshot of a line, as stored in `LineRevision.before` / `after`."""
    return {
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
    }


def _record(page: Page, action: str, line: Line | None, before, after, user) -> LineRevision:
    """Store one revision of the page."""
    return LineRevision.objects.create(
        page=page, line=line, action=action, before=before, after=after, user=_user_or_none(user)
    )


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


def refresh_page_text(page: Page) -> None:
    """Rebuild `final_text` (body, blank line, footnotes; Western digits) and `n_unresolved`."""
    lines = list(_page_lines(page))
    page.final_text = to_western_digits(
        join_region_texts([(_region_kind(line), line.text) for line in lines])
    )
    page.n_unresolved = sum(line.n_low for line in lines)
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


def line_item(line: Line) -> dict:
    """One line of the review payload (tokens with every key present)."""
    return {
        "id": line.pk,
        "order": line.order,
        "region_id": line.region_id,
        "region_kind": _region_kind(line),
        "bbox": line.bbox,
        "text": line.text,
        "ocr_text": line.ocr_text,
        "is_manual": line.is_manual,
        "is_reviewed": line.is_reviewed,
        "n_low": line.n_low,
        "tokens": [normalize_token(token) for token in line.tokens or []],
    }


def page_item(page: Page) -> dict:
    """The `page` block of the review payload."""
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
        "error": error[0] if error and page.status == Page.Status.ERROR else "",
        "error_from": page.error_from if page.status == Page.Status.ERROR else "",
    }


def _token_counts(tokens_per_line) -> tuple[int, int]:
    """`(low_total, unresolved)` over lists of tokens."""
    low = unresolved = 0
    for tokens in tokens_per_line:
        for token in tokens or []:
            if token.get("conf") == "low":
                low += 1
                if not token.get("res"):
                    unresolved += 1
    return low, unresolved


def book_unresolved_total(book_id: int) -> int:
    """Unresolved words over the non-excluded pages of a book."""
    total = Page.objects.filter(book_id=book_id, is_excluded=False).aggregate(n=Sum("n_unresolved"))["n"]
    return int(total or 0)


def mutation_counts(page: Page, line: Line | None = None) -> dict:
    """`counts` of the mutation responses: the line's and page's unresolved words and the book total."""
    low, unresolved = _token_counts(page.lines.values_list("tokens", flat=True))
    return {
        "line_n_low": line.n_low if line is not None else 0,
        "page_unresolved": unresolved,
        "page_low_total": low,
        "book_unresolved_total": book_unresolved_total(page.book_id),
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


def review_payload(page: Page, user) -> dict:
    """Everything the review screen needs for one page (PHASE3_SPEC §4 shape)."""
    book = page.book
    summary = book_review_summary(book)
    pre = _preprocess_of(page)
    lines = list(_page_lines(page))
    low, unresolved = _token_counts(line.tokens for line in lines)
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
        "counts": {"low_total": low, "unresolved": unresolved, "resolved": low - unresolved},
        "labels": {
            "primary": ENGINE_LABELS.get(primary, primary),
            "secondary": ENGINE_LABELS.get(secondary, secondary),
        },
        "nav": {
            "prev_url": review_url(previous) if previous else None,
            "next_url": review_url(following) if following else None,
            "next_review_url": next_review_url(book.pk, page.number),
            "dashboard_url": reverse("books:detail", args=[book.pk]),
        },
        "urls": {
            "payload": reverse("api:page_review", args=[page.pk]),
            "resolve": _id_template("line_resolve", "line_id"),
            "edit": _id_template("line_edit", "line_id"),
            "delete": _id_template("line_delete", "line_id"),
            "insert": reverse("api:page_lines", args=[page.pk]),
            "undo": reverse("api:page_undo", args=[page.pk]),
            "approve": reverse("api:page_approve", args=[page.pk]),
            "reopen": reverse("api:page_reopen", args=[page.pk]),
            "filmstrip": reverse("api:book_filmstrip", args=[book.pk]),
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
    """Per non-excluded page `{id, number, thumb_url, is_reviewed, n_unresolved, status, url}` (one query)."""
    pages = (
        book.pages.filter(is_excluded=False)
        .select_related("preprocess")
        .only("id", "number", "book_id", "status", "n_unresolved", "scan_thumbnail", "preprocess__thumbnail")
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
def resolve_token(line: Line, index, choice: str, text: str | None = None, user=None) -> Line:
    """Resolve token `index` of `line` with a reading: `primary`, `secondary`, `tess` or `typed`.

    `primary` keeps the primary model's reading (restores it when another reading was chosen
    before), `secondary` takes `alt`, `tess` takes Tesseract's word, `typed` takes `text`. Sets
    `res` to the choice (the token's `conf` is kept). Raises `ReviewError` (Arabic) on a bad
    index / choice or a missing alternative.
    """
    page = _lock_page(line.page)
    _check_editable(page)
    line = _line_of(page, line.pk)
    if line is None:
        raise ReviewError("السطر غير موجود في هذه الصفحة.")
    tokens = [normalize_token(token) for token in line.tokens or []]
    i = _as_index(index, len(tokens))
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
    _record(page, LineRevision.Action.RESOLVE, line, before, line_snapshot(line), user)
    refresh_page_text(page)
    return line


def retokenize(old_tokens: list[dict], text: str) -> list[dict]:
    """Tokens for a line edited to `text`, aligned to its old tokens (`ocr.alignment.align_tokens`).

    A token whose text is unchanged keeps its whole dict (bbox, conf, res, alternatives); a changed
    token paired one-to-one with an old token becomes a typed token with the old bbox; an added
    token becomes a typed token without a box.
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
            out[j] = typed_token(words[j], old[i]["bbox"] if i is not None else None)
    return [token if token is not None else typed_token(words[j]) for j, token in enumerate(out)]


@transaction.atomic
def edit_line(line: Line, text: str, user=None) -> Line:
    """Replace the text of a whole line; unchanged words keep their boxes and resolutions.

    Words are separated by whitespace (collapsed to single spaces). `ocr_text` never changes.
    Empty text is refused (delete the line instead); an unchanged text is a no-op (no revision).
    """
    page = _lock_page(line.page)
    _check_editable(page)
    line = _line_of(page, line.pk)
    if line is None:
        raise ReviewError("السطر غير موجود في هذه الصفحة.")
    words = _clean_words(text)
    if not words:
        raise ReviewError("السطر فارغ؛ لحذفه استخدم «حذف السطر».")
    if len(" ".join(words)) > MAX_LINE_CHARS:
        raise ReviewError("السطر أطول من المسموح.")
    if [normalize_token(token)["t"] for token in line.tokens or []] == words:
        return line
    before = line_snapshot(line)
    _set_tokens(line, retokenize(line.tokens, " ".join(words)))
    line.updated_by = _user_or_none(user)
    line.save(update_fields=["tokens", "text", "n_low", "updated_by", "updated_at"])
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
        is_reviewed=page.status == Page.Status.REVIEWED,
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
def delete_line(line: Line, user=None) -> int:
    """Delete a line (typically a manual or garbage one); the lines below move up. Returns its id."""
    page = _lock_page(line.page)
    _check_editable(page)
    line = _line_of(page, line.pk)
    if line is None:
        raise ReviewError("السطر غير موجود في هذه الصفحة.")
    line_id = line.pk
    _record(page, LineRevision.Action.DELETE, None, line_snapshot(line), None, user)
    line.delete()
    _compact_orders(page)
    refresh_page_text(page)
    return line_id


# ====================================================================== undo


def _restore_content(page: Page, revision: LineRevision) -> None:
    """Undo a resolve / edit: the line's text, tokens and box go back to the `before` snapshot."""
    snap = revision.before or {}
    line = _line_of(page, revision.line_id or snap.get("id"))
    if line is None:
        raise ReviewError("تعذّر التراجع: السطر لم يعد موجودًا.")
    line.bbox = snap.get("bbox")
    _set_tokens(line, [normalize_token(token) for token in snap.get("tokens") or []])
    line.text = snap.get("text", line.text)
    line.save(update_fields=["bbox", "tokens", "text", "n_low", "updated_at"])


def _undo_insert(page: Page, revision: LineRevision) -> None:
    """Undo an insert: the inserted line goes away and the lines below move back up."""
    line = _line_of(page, revision.line_id or (revision.after or {}).get("id"))
    if line is not None:
        line.delete()
    _compact_orders(page)


def _undo_delete(page: Page, revision: LineRevision) -> None:
    """Undo a delete: the line comes back with its old id, order, region and tokens."""
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
    )
    if line_id is not None and not Line.objects.filter(pk=line_id).exists():
        line.pk = line_id
    _set_tokens(line, [normalize_token(token) for token in snap.get("tokens") or []])
    line.text = snap.get("text", line.text)
    line.save(force_insert=True)
    _compact_orders(page)
    if line_id is not None:
        # Older revisions of this line lost their link when it was deleted; point them at it again.
        page.revisions.filter(line__isnull=True).filter(Q(before__id=line_id) | Q(after__id=line_id)).exclude(
            action__in=[LineRevision.Action.APPROVE, LineRevision.Action.REOPEN]
        ).update(line=line)


@transaction.atomic
def undo_last(page: Page, user=None) -> dict:
    """Revert the newest revision of the page that is not undone yet and mark it undone.

    Resolve / edit restore the line's previous text and tokens, an insert is removed, a deleted
    line comes back at its old position, approve / reopen restore the page's review status and
    the lines' reviewed flags. Returns the new `review_payload`. Raises `ReviewError`
    «لا شيء للتراجع عنه» when nothing is left to undo.
    """
    page = _lock_page(page)
    revision = page.revisions.filter(undone=False).order_by("-created_at", "-id").first()
    if revision is None:
        raise ReviewError("لا شيء للتراجع عنه.")
    _check_editable(page)
    action = revision.action
    if action in (LineRevision.Action.RESOLVE, LineRevision.Action.EDIT):
        _restore_content(page, revision)
    elif action == LineRevision.Action.INSERT:
        _undo_insert(page, revision)
    elif action == LineRevision.Action.DELETE:
        _undo_delete(page, revision)
    else:  # approve / reopen
        _restore_page_state(page, revision.before or {})
    revision.undone = True
    revision.save(update_fields=["undone"])
    refresh_page_text(page)
    if action in (LineRevision.Action.APPROVE, LineRevision.Action.REOPEN):
        _refresh_book(page)
    page.refresh_from_db()
    return review_payload(page, user)


# ====================================================================== approve / reopen


def _next_after(page: Page) -> dict:
    """Where to go after `page` was approved: the next page's review / payload URLs (None when none)."""
    book = Book.objects.get(pk=page.book_id)
    following = next_page_to_review(book, after_number=page.number)
    return {
        # Always a URL: the next page's review screen, or `review:next`, which redirects to the
        # dashboard with «لا صفحات بانتظار المراجعة» when nothing is left.
        "next_review_url": review_url(following) if following else next_review_url(book.pk, page.number),
        "next_payload_url": reverse("api:page_review", args=[following.pk]) if following else None,
        "next_number": following.number if following else None,
        "dashboard_url": reverse("books:detail", args=[book.pk]),
    }


@transaction.atomic
def approve_page(page: Page, user, force: bool = False) -> dict:
    """Mark a page reviewed. Returns `{"status", "next_review_url", "next_payload_url", ...}`.

    With unresolved words left and `force` false, raises `ReviewBlocked` (API 409). Every line is
    marked reviewed (so a new OCR pass keeps them), `reviewed_by` / `reviewed_at` are set and the
    book status is re-derived. Approving an approved page changes nothing.
    """
    page = _lock_page(page)
    if page.status == Page.Status.REVIEWED:
        return {"status": page.status, **_next_after(page)}
    if page.status != Page.Status.OCR_DONE or page.text_state != Page.TextState.FINAL:
        raise ReviewError("الصفحة ليست جاهزة للاعتماد بعد.")
    unresolved = sum(page.lines.values_list("n_low", flat=True))
    if unresolved and not force:
        raise ReviewBlocked(unresolved)
    before = _page_state(page)
    page.status = Page.Status.REVIEWED
    page.reviewed_by = _user_or_none(user)
    page.reviewed_at = timezone.now()
    page.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    page.lines.update(is_reviewed=True)
    _record(page, LineRevision.Action.APPROVE, None, before, _page_state(page), user)
    refresh_page_text(page)
    _refresh_book(page)
    return {"status": page.status, **_next_after(page)}


@transaction.atomic
def reopen_page(page: Page, user) -> None:
    """Take an approved page back to `ocr_done` (lines unreviewed) so it can be reviewed again."""
    page = _lock_page(page)
    if page.status != Page.Status.REVIEWED:
        raise ReviewError("الصفحة غير معتمدة؛ لا حاجة لإعادة فتحها.")
    before = _page_state(page)
    page.status = Page.Status.OCR_DONE
    page.reviewed_by = None
    page.reviewed_at = None
    page.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    page.lines.update(is_reviewed=False)
    _record(page, LineRevision.Action.REOPEN, None, before, _page_state(page), user)
    _refresh_book(page)
