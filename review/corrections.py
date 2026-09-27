"""«تصحيح في كل الكتاب» (D79, PHASE7_SPEC §5.7): one correction applied to every occurrence of a word form in
review, with a preview and a batch undo. No learning and no propagation: nothing is corrected that the
reviewer did not tick.

- `find_occurrences(book, form, options)` lists every token of the book's reviewable pages whose core (the
  token without its edge punctuation) matches `form` with the book page's find options (diacritics, alef
  forms, whole word: `editor.document.FindOptions`), with its context, its box and whether assembly drops its
  line (a running head). A regex over `Line.text` (diacritics allowed between the letters) finds the
  candidate lines; the tokens decide.
- `fix_everywhere(book, from_, to, picks, user)` sets each ticked token to the correction (its edge
  punctuation kept, `orig` kept, `res = "typed"`; an open suggestion after it reads the new text as its
  `after_t`), one transaction per page and one
  `LineRevision(action="fix")` per line, every line in one `batch`. A token that no longer reads what the
  sheet showed, or a page not open for review, is skipped and reported. Approved pages stay approved; an
  `assembled` page goes back to `reviewed` (D36).
- `undo_fix(book, batch, user)` reverts each line of the batch whose newest revision is the batch's, and
  reports the others (changed after the fix: their newer text stays).
- `elsewhere(book, from_, to, line_id, index)`: the other occurrences of a form just corrected (the chip
  «159 موضعًا آخر بالشكل نفسه في الكتاب · تصحيحها…»).

An edited book (D41) gets the fix in review only; `find_url` opens the book page's find & replace filled
in with the same correction and the same options, and the book page then plans the batch's pages
(`api:review_changes` with `{fix}`): the pages whose plan has no item left are settled at once, so the
baseline moves only where the book text agrees with review.

The contract of these payloads is editor/fixtures/contract/ (occurrences.json, fix_everywhere.json).
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode

from django.db import transaction
from django.urls import reverse

from assembly.render import ar_count
from books.models import Book, Page
from editor import document as doc
from ocr.models import Line, TextGap
from ocr.services import SKIPPED_KINDS

from .models import LineRevision
from .services import (
    REVIEWABLE_STATUSES,
    REVIEWED_STATUSES,
    ReviewError,
    _check_editable,
    _file_url,
    _line_of,
    _lock_page,
    _preprocess_of,
    _record,
    _restore_content,
    _set_tokens,
    _touch_page,
    _user_or_none,
    line_snapshot,
    line_version,
    normalize_token,
    refresh_page_text,
)

MAX_OCCURRENCES = 2000
MAX_PICKS = 5000
CONTEXT_WORDS = 5
PLACES = ("موضع واحد", "موضعان", "مواضع", "موضعًا")
PLACES_OF = ("موضع واحد", "موضعين", "مواضع", "موضعًا")  # after «في»
OTHER_PLACES = ("موضع واحد آخر", "موضعان آخران", "مواضع أخرى", "موضعًا آخر")
PAGES_OF = ("صفحة واحدة", "صفحتين", "صفحات", "صفحة")
NO_FORM = "اكتب الكلمة التي تريد تصحيحها."
ONE_WORD = "يُصحَّح في كل الكتاب كلمة واحدة في كل مرة."
NO_PICKS = "لم يُحدَّد موضع للتصحيح."
SAME_WORD = "التصحيح مطابق للكلمة نفسها."
NO_CORRECTION = "اكتب التصحيح أولًا."
TOO_MANY = "عدد المواضع أكبر من المسموح."
UNKNOWN_BATCH = "لا تصحيح بهذا المعرّف في هذا الكتاب."
MAX_WORD_CHARS = 120
CONFIRMED = frozenset({"primary", "typed"})  # a reviewer kept the form as it is: unticked by default


class CorrectionNotFound(ReviewError):
    """No such fix batch in this book (API 404)."""


def options_of(data) -> doc.FindOptions:
    """The sheet's options from a request (defaults: diacritics ignored, alef forms folded, whole words)."""
    from editor.services import _parse_bool

    data = data if hasattr(data, "get") else {}
    return doc.FindOptions(
        match_tashkeel=_parse_bool(data.get("match_tashkeel"), False),
        fold_alef=_parse_bool(data.get("fold_alef"), True),
        whole_word=_parse_bool(data.get("whole_word"), True),
    )


def _form(value) -> str:
    """A form to find or a correction: one word (edge punctuation allowed), else `ReviewError`."""
    words = str(value or "").split()
    if not words:
        raise ReviewError(NO_FORM)
    if len(words) > 1:
        raise ReviewError(ONE_WORD)
    if len(words[0]) > MAX_WORD_CHARS:
        raise ReviewError(ONE_WORD)
    return words[0]


def _is_core(char: str) -> bool:
    return char.isalnum() or unicodedata.combining(char) > 0 or char == "ـ"


def split_core(word: str) -> tuple[str, str, str]:
    """`(prefix, core, suffix)` of a token: its edge punctuation around the letters, digits and marks."""
    start, end = 0, len(word)
    while start < end and not _is_core(word[start]):
        start += 1
    while end > start and not _is_core(word[end - 1]):
        end -= 1
    return word[:start], word[start:end], word[end:]


@dataclass(frozen=True)
class Found:
    """Where the form sits in a token's core: `[start, end)` in the core's characters."""

    start: int
    end: int


def match(core: str, query: str, options: doc.FindOptions) -> Found | None:
    """The form in a token's core (`query` already folded): the whole core with `whole_word`, else the
    first occurrence in it; None when it is not there."""
    if not core:
        return None
    folded, index = doc.fold(core, options)
    if options.whole_word:
        if folded != query:
            return None
        return Found(0, len(core))
    pos = folded.find(query)
    if pos < 0:
        return None
    start = index[pos]
    stop = index[pos + len(query) - 1] + 1
    if not options.match_tashkeel:
        while stop < len(core) and core[stop] in doc._DROPPED:
            stop += 1
    return Found(start, stop)


def corrected(word: str, query: str, to: str, options: doc.FindOptions) -> str | None:
    """`word` with the form replaced by `to` (its edge punctuation kept); None when the form is not there."""
    prefix, core, suffix = split_core(word)
    found = match(core, query, options)
    if found is None:
        return None
    replacement = split_core(to)[1] or to
    return f"{prefix}{core[: found.start]}{replacement}{core[found.end :]}{suffix}"


_MARKS = "".join(sorted(doc._DROPPED))
_ALEFS = "اأإآٱٲٳ"


def text_pattern(query: str, options: doc.FindOptions) -> str:
    """A regex over `Line.text` that every line holding the form matches (diacritics and tatweel allowed
    between the letters, alef forms folded): the prefilter of `find_occurrences`."""
    letters = [char for char in query if char not in doc._DROPPED and not unicodedata.combining(char)]
    marks = f"[{re.escape(_MARKS)}]*"
    parts = []
    for char in letters:
        if options.fold_alef and char == "ا":
            parts.append(f"[{_ALEFS}]")
        else:
            parts.append(re.escape(char))
    return marks.join(parts)


def _heads(book: Book) -> set[int]:
    """Lines assembly leaves out of the text as running heads (`pipeline.drop_running_heads`)."""
    from assembly import pipeline
    from assembly.services import load_book

    options = pipeline.normalize_settings(book.assembly_settings)
    if not options.strip_running_heads:
        return set()
    loaded = load_book(book)
    included = pipeline.select_pages(loaded.pages, options).included
    kept, _warnings, _count = pipeline.drop_running_heads(included)
    before = {line.id for page in included for line in page.lines}
    after = {line.id for page in kept for line in page.lines}
    return before - after


def folded_form(form: str, options: doc.FindOptions) -> str:
    """The form as tokens are compared with it: its core (edge punctuation aside), folded."""
    return doc.fold_query(split_core(form)[1] or form, options)


def _candidates(book: Book, form: str, options: doc.FindOptions):
    query = folded_form(form, options)
    pattern = text_pattern(query, options)
    if not pattern:
        raise ReviewError(NO_FORM)
    rows = (
        Line.objects.filter(
            page__book_id=book.pk,
            page__is_excluded=False,
            page__status__in=REVIEWABLE_STATUSES,
            page__text_state=Page.TextState.FINAL,
            text__iregex=pattern,
        )
        .only("id", "page_id", "tokens")
        .order_by("page__number", "order", "id")
    )
    out = []
    for line in rows:
        for index, token in enumerate(line.tokens or []):
            if not isinstance(token, dict):
                continue
            word = str(token.get("t") or "")
            if match(split_core(word)[1], query, options) is not None:
                out.append((line.page_id, line.pk, index))
    return query, out


def find_occurrences(book: Book, form, options: doc.FindOptions) -> dict:
    """Every occurrence of `form` in the book's reviewable pages (see the module docstring):
    `{query, options, total, pages, picked, truncated, results: [{number, page_id, status, status_label,
    approved, image: {url, width, height}, lines: [{line_id, v, index, word, before, after, bbox, line_bbox,
    res, conf, head, pick}]}]}`. `head`: assembly drops the line (a running head or page number region, or
    a running head the pipeline finds); `pick`: the default tick (off for a form a reviewer confirmed as it
    is, `res` primary or typed, and for head lines). At most `MAX_OCCURRENCES` (`truncated`)."""
    form = _form(form)
    try:
        query, found = _candidates(book, form, options)
    except doc.DocumentError as exc:
        raise ReviewError(str(exc)) from exc
    total = len(found)
    found = found[:MAX_OCCURRENCES]
    page_ids = sorted({page_id for page_id, _line, _index in found})
    pages = {
        page.pk: page
        for page in Page.objects.filter(pk__in=page_ids)
        .select_related("preprocess")
        .only(
            "id",
            "number",
            "book_id",
            "status",
            "width",
            "height",
            "preprocess__display_image",
            "preprocess__gray_image",
            "preprocess__output_width",
            "preprocess__output_height",
        )
    }
    lines_by_page: dict[int, list[Line]] = {}
    for line in (
        Line.objects.filter(page_id__in=page_ids)
        .select_related("region")
        .only("id", "page_id", "order", "tokens", "bbox", "updated_at", "region__kind")
        .order_by("order", "id")
    ):
        lines_by_page.setdefault(line.page_id, []).append(line)
    heads = _heads(book) if found else set()
    wanted: dict[int, set[int]] = {}
    for _page, line_id, index in found:
        wanted.setdefault(line_id, set()).add(index)
    results = []
    picked = 0
    for page_id in sorted(page_ids, key=lambda pk: pages[pk].number):
        page = pages[page_id]
        words: list[tuple[int, int, str]] = []  # (line id, token index, text) in reading order
        for line in lines_by_page.get(page_id, []):
            for index, token in enumerate(line.tokens or []):
                if isinstance(token, dict) and token.get("t"):
                    words.append((line.pk, index, str(token["t"])))
        position = {(line_id, index): n for n, (line_id, index, _t) in enumerate(words)}
        items = []
        for line in lines_by_page.get(page_id, []):
            for index in sorted(wanted.get(line.pk, ())):
                token = normalize_token((line.tokens or [])[index])
                n = position.get((line.pk, index), 0)
                region = line.region.kind if line.region_id and line.region is not None else "body"
                head = region in SKIPPED_KINDS or line.pk in heads
                pick = not head and token.get("res") not in CONFIRMED
                picked += pick
                items.append(
                    {
                        "line_id": line.pk,
                        "v": line_version(line),
                        "index": index,
                        "word": token["t"],
                        "before": " ".join(t for _l, _i, t in words[max(0, n - CONTEXT_WORDS) : n]),
                        "after": " ".join(t for _l, _i, t in words[n + 1 : n + 1 + CONTEXT_WORDS]),
                        "bbox": token.get("bbox"),
                        "line_bbox": line.bbox,
                        "res": token.get("res"),
                        "conf": token.get("conf"),
                        "head": head,
                        "pick": pick,
                    }
                )
        pre = _preprocess_of(page)
        results.append(
            {
                "number": page.number,
                "page_id": page.pk,
                "status": page.status,
                "status_label": page.get_status_display(),
                "approved": page.status in REVIEWED_STATUSES,
                "image": {
                    "url": (_file_url(pre.display_image) or _file_url(pre.gray_image))
                    if pre is not None
                    else None,
                    "width": (pre.output_width if pre is not None else 0) or page.width,
                    "height": (pre.output_height if pre is not None else 0) or page.height,
                },
                "lines": items,
            }
        )
    return {
        "query": form,
        "options": {
            "match_tashkeel": options.match_tashkeel,
            "fold_alef": options.fold_alef,
            "whole_word": options.whole_word,
        },
        "total": total,
        "pages": len(results),
        "picked": picked,
        "truncated": total > MAX_OCCURRENCES,
        "results": results,
    }


def _picks(values) -> list[tuple[int, int, str]]:
    if not isinstance(values, list | tuple) or not values:
        raise ReviewError(NO_PICKS)
    if len(values) > MAX_PICKS:
        raise ReviewError(TOO_MANY)
    out = []
    for value in values:
        if not isinstance(value, dict):
            raise ReviewError(NO_PICKS)
        line_id, index = value.get("line_id"), value.get("index")
        if isinstance(line_id, bool) or isinstance(index, bool):
            raise ReviewError(NO_PICKS)
        try:
            out.append((int(str(line_id)), int(str(index)), str(value.get("t") or "")))
        except (TypeError, ValueError):
            raise ReviewError(NO_PICKS) from None
    return out


def _fix_message(applied: int, pages: int) -> str:
    return f"صُحّح {ar_count(applied, PLACES)} في {ar_count(pages, PAGES_OF)}"


def find_url(book: Book, from_: str, to: str, batch: str, options: doc.FindOptions | None = None) -> str:
    """The edited book's find & replace, filled in with the correction (D79) and the sheet's options
    (`match_tashkeel`, `fold_alef`, `whole_word` as 1 / 0): the book page's own defaults (whole word off)
    would also replace «السعودية» for «السعودي», and the batch's plan would take that for the owner's edit."""
    options = options or doc.FindOptions(whole_word=True)
    flag = {True: "1", False: "0"}
    query = urlencode(
        {
            "tab": "find",
            "q": from_,
            "r": to,
            "fix": batch,
            "match_tashkeel": flag[bool(options.match_tashkeel)],
            "fold_alef": flag[bool(options.fold_alef)],
            "whole_word": flag[bool(options.whole_word)],
        }
    )
    return f"{reverse('editor:layout', args=[book.pk])}?{query}"


def _follow_gaps(line: Line, changed: dict[int, str]) -> None:
    """The open suggestions (TextGap, D72) after a corrected token read its new text as `after_t`: review
    draws a gap by its `index`, and `gap_anchor` would otherwise take the stale `after_t` for a moved word
    and put the accepted words after another token that still reads it. The line's snapshot taken before
    the fix keeps the old `after_t`, so the batch's undo puts it back."""
    for gap in TextGap.objects.filter(line=line, status=TextGap.Status.OPEN, index__in=list(changed)):
        after_t = changed[gap.index][:200]
        if gap.after_t != after_t:
            gap.after_t = after_t
            gap.save(update_fields=["after_t"])


def fix_everywhere(book: Book, from_, to, picks, user=None, options: doc.FindOptions | None = None) -> dict:
    """Correct the ticked occurrences (see the module docstring). Returns `{batch, applied, skipped:
    [{line_id, index, page, reason: changed | page}], pages: [numbers changed], edited, find_url (an edited
    book), message}`."""
    from editor.models import Manuscript

    options = options or doc.FindOptions(whole_word=True)
    source = _form(from_)
    target = " ".join(str(to or "").split())
    if not target:
        raise ReviewError(NO_CORRECTION)
    if len(target) > MAX_WORD_CHARS or len(target.split()) > 1:
        raise ReviewError(ONE_WORD)
    if target == source:
        raise ReviewError(SAME_WORD)
    chosen = _picks(picks)
    try:
        query = folded_form(source, options)
    except doc.DocumentError as exc:
        raise ReviewError(str(exc)) from exc
    owners = dict(
        Line.objects.filter(
            pk__in={line_id for line_id, _i, _t in chosen}, page__book_id=book.pk
        ).values_list("id", "page_id")
    )
    numbers = dict(Page.objects.filter(pk__in=set(owners.values())).values_list("id", "number"))
    batch = uuid.uuid4()
    by_page: dict[int, dict[int, list[tuple[int, str]]]] = {}
    skipped: list[dict] = []
    for line_id, index, seen in chosen:
        page_id = owners.get(line_id)
        if page_id is None:
            skipped.append({"line_id": line_id, "index": index, "page": None, "reason": "changed"})
            continue
        by_page.setdefault(page_id, {}).setdefault(line_id, []).append((index, seen))
    applied = 0
    changed_pages: list[int] = []
    for page_id in sorted(by_page, key=lambda pk: numbers.get(pk, 0)):
        with transaction.atomic():
            page = _lock_page(Page(pk=page_id))
            try:
                _check_editable(page)
            except ReviewError:
                for line_id, wanted in by_page[page_id].items():
                    skipped.extend(
                        {"line_id": line_id, "index": i, "page": page.number, "reason": "page"}
                        for i, _s in wanted
                    )
                continue
            count = 0
            for line_id, wanted in sorted(by_page[page_id].items()):
                line = _line_of(page, line_id)
                tokens = [normalize_token(token) for token in (line.tokens or [])] if line is not None else []
                changed: dict[int, str] = {}  # token index → its corrected text
                for index, seen in sorted(wanted):
                    token = tokens[index] if 0 <= index < len(tokens) else None
                    new = corrected(token["t"], query, target, options) if token is not None else None
                    if token is None or token["t"] != seen or new is None:
                        skipped.append(
                            {"line_id": line_id, "index": index, "page": page.number, "reason": "changed"}
                        )
                        continue
                    if not changed:
                        before = line_snapshot(line)
                    token.setdefault("orig", token["t"])
                    token["t"] = new
                    token["res"] = "typed"
                    changed[index] = new
                    count += 1
                if changed:
                    _set_tokens(line, tokens)
                    line.updated_by = _user_or_none(user)
                    line.save(update_fields=["tokens", "text", "n_low", "updated_by", "updated_at"])
                    _follow_gaps(line, changed)
                    _record(page, LineRevision.Action.FIX, line, before, line_snapshot(line), user, batch)
            if count:
                refresh_page_text(page)
                applied += count
                changed_pages.append(page.number)
    edited = Manuscript.objects.filter(book_id=book.pk, origin=Manuscript.Origin.EDITOR).exists()
    return {
        "batch": str(batch),
        "applied": applied,
        "skipped": skipped,
        "pages": sorted(changed_pages),
        "edited": edited,
        "find_url": find_url(book, source, target, str(batch), options) if edited and applied else None,
        "message": _fix_message(applied, len(changed_pages)),
    }


def undo_fix(book: Book, batch, user=None) -> dict:
    """Revert a fix batch (see the module docstring): `{batch, reverted, kept: [{line_id, page}], pages,
    message}`. `CorrectionNotFound` for a batch this book never had."""
    try:
        key = uuid.UUID(str(batch))
    except (TypeError, ValueError):
        raise CorrectionNotFound(UNKNOWN_BATCH) from None
    revisions = LineRevision.objects.filter(page__book_id=book.pk, batch=key, action=LineRevision.Action.FIX)
    if not revisions.exists():
        raise CorrectionNotFound(UNKNOWN_BATCH)
    page_ids = sorted(set(revisions.filter(undone=False).values_list("page_id", flat=True)))
    reverted = 0
    kept: list[dict] = []
    pages: list[int] = []
    for page_id in page_ids:
        with transaction.atomic():
            page = _lock_page(Page(pk=page_id))
            count = 0
            for revision in page.revisions.filter(
                batch=key, action=LineRevision.Action.FIX, undone=False
            ).order_by("-created_at", "-id"):
                line_id = revision.line_id or (revision.before or {}).get("id")
                newest = (
                    LineRevision.objects.filter(page=page, undone=False, line_id=line_id)
                    .order_by("-created_at", "-id")
                    .first()
                )
                if newest is None or newest.pk != revision.pk:
                    kept.append({"line_id": line_id, "page": page.number})
                    continue
                _restore_content(page, revision)
                revision.undone = True
                revision.save(update_fields=["undone"])
                count += 1
            if count:
                _touch_page(page)
                refresh_page_text(page)
                reverted += count
                pages.append(page.number)
    message = f"تُراجع عن التصحيح في {ar_count(reverted, PLACES_OF)}"
    if kept:
        message += f"؛ وبقي على حاله ما تغيّر بعد التصحيح ({len(kept)})"
    return {"batch": str(key), "reverted": reverted, "kept": kept, "pages": sorted(pages), "message": message}


def elsewhere(book: Book, before: str, after: str, line_id: int, index: int) -> dict | None:
    """The other occurrences of a form just corrected (`before` → `after`, token `index` of `line_id`
    excepted): `{from, to, count, pages, text}`, or None when there are none (or the correction was not a
    change of one word's core)."""
    source, target = split_core(str(before or ""))[1], split_core(str(after or ""))[1]
    if not source or not target or source == target:
        return None
    options = doc.FindOptions(whole_word=True)
    try:
        _query, found = _candidates(book, source, options)
    except (doc.DocumentError, ReviewError):
        return None
    others = [(page, line, i) for page, line, i in found if not (line == line_id and i == index)]
    if not others:
        return None
    count = len(others)
    return {
        "from": source,
        "to": target,
        "count": count,
        "pages": len({page for page, _line, _i in others}),
        "text": f"{ar_count(count, OTHER_PLACES)} بالشكل نفسه في الكتاب",
    }
