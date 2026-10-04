"""Search and quotation checking over an account's books (D107; CHALLENGE_SPEC §2), the services behind the
«البحث والتحقق» page (`research.api`) and the MCP tools (`research.mcp_server`, D108).

Every function takes the user and starts from `books.access.books_for(user)`: a book of another organisation
is never read, by construction (a book id outside the account answers 404 like a missing one). Pure Python
over `research.PageText` (`research.index`), so the suite runs on SQLite; the candidate rows are found with
`norm LIKE '%word%'`, which a `pg_trgm` GIN index serves on PostgreSQL (migration 0002).

- `search`: the query's normal form; phrase first (the words in order, also across a page break), then every
  word on the page (the smallest window holding them), then fuzzy (rapidfuzz `partial_ratio` ≥ 85 on the
  page). Filters: books, kind (body | notes | all).
- `verify_quote`: under 3 words → `too_short`; candidate pages by their words, the best window of each by
  word alignment (`difflib.SequenceMatcher` on normal forms, located first with rapidfuzz) over the page
  with its neighbours (a quotation may run over a page break; a running head at the top of the next page is
  left out); word ratio < 0.6 → `not_found` («لم يوجد في كتب هذا الحساب», never «مختلق»). Otherwise every
  difference is listed (replaced, missing, added, moved) with the book's words and their state:
  `needs_image_check` when every difference falls on a doubtful reading (or, for a word the quotation adds,
  next to one, or where the OCR offered a skipped word), `differs` as soon as one falls on a reviewed or a
  confident reading, `exact` otherwise (`diacritics_differ` when only the vowels differ). Attribution: a
  match in the notes quoted as the author's → `note_not_author`; in the body quoted as the editor's →
  `body_not_editor`; `book` given and the passage found only in another book → `other_book`.
- `get_passage`, `cite`, `list_books`: a passage with its context (body and notes apart, boxes, doubtful
  words, clip), its citation from the book's details, the account's books.

`passage_id`: `<book>:<page>:<b|n>:<line>.<token>-<line>.<token>`, the end prefixed by `<page>:` when the
passage runs onto the next page: stable while the lines are (review renumbers no line id).
"""

from __future__ import annotations

import bisect
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from django.db.models import Count, Q, QuerySet
from django.http import Http404
from django.urls import reverse

from rapidfuzz import fuzz

from assembly.pipeline import head_key, heads_match
from assembly.render import ar_count
from books.access import BOOK_NOT_FOUND, books_for, get_book_or_404
from books.models import Book, Page
from ocr.models import Line

from . import clips, index, keys
from .models import PageText
from .normalize import Word, diacritics_differ, words_of
from .schemas import (
    BookInfo,
    BookRef,
    BooksResult,
    Change,
    Citation,
    CitationParts,
    DiacriticDifference,
    DiffSegment,
    Hit,
    PageRef,
    Passage,
    PassageLine,
    SearchResult,
    VerifyResult,
    WordState,
)

MIN_QUOTE_WORDS = 3
NOT_FOUND_RATIO = 0.6
FUZZY_MIN = 85.0
MAX_LIMIT = 50
CONTEXT_WORDS = 12
PREFILTER_WORDS = 6  # the longest distinct words of a query that find the candidate rows
VERIFY_CANDIDATES = 8
HEAD_MAX_WORDS = 6  # a running head is a short first line
PRINTED_WINDOW = 10  # pages on each side whose printed numbers vote for a page's
KIND_CHAR = {"body": "b", "notes": "n"}
CHAR_KIND = {value: key for key, value in KIND_CHAR.items()}
_PASSAGE = re.compile(r"^(\d+):(\d+):([bn]):(\d+)\.(\d+)-(?:(\d+):)?(\d+)\.(\d+)$")
KIND_LABEL = {"body": "المتن", "notes": "الحاشية"}

MESSAGES = {
    "exact": "النص مطابق لما في الكتاب.",
    "differs": "يختلف النص عن الكتاب في {n}.",
    "needs_image_check": "يحتاج مطابقة مع الصورة: الفرق في كلمات لم تُحسم قراءتها بعد.",
    "not_found": "لم يوجد في كتب هذا الحساب.",
    "too_short": "النص قصير جدًا للتحقق؛ اكتب ثلاث كلمات على الأقل.",
}
ATTRIBUTION_MESSAGES = {
    "note_not_author": "هذا من حاشية المحقق لا من متن المؤلف.",
    "body_not_editor": "هذا من متن المؤلف لا من حاشية المحقق.",
}
PLACES = ("موضع واحد", "موضعين", "مواضع", "موضعًا")
WORDS = ("كلمة واحدة", "كلمتين", "كلمات", "كلمة")
RESULTS = ("نتيجة واحدة", "نتيجتان", "نتائج", "نتيجة")


class PassageNotFound(Http404):
    """A passage id that is malformed or names words that are no longer there (API 404)."""


# ====================================================================== scope


def account_books(user, book_ids: Iterable[int] | None = None) -> QuerySet[Book]:
    """The user's books (`books.access.books_for`), narrowed to `book_ids` when given (ids outside the
    account are simply not there)."""
    books = books_for(user)
    if book_ids is not None:
        books = books.filter(pk__in=[int(pk) for pk in book_ids])
    return books


def _book_ids(user, book_ids: Iterable[int] | None = None) -> list[int]:
    return list(account_books(user, book_ids).values_list("pk", flat=True))


# ====================================================================== printed page numbers


def infer_printed(rows: Iterable[tuple[int, str]]) -> dict[int, tuple[str | None, str]]:
    """Each page's printed number from `(page number, printed number)` rows: `{number: (printed, source)}`.

    OCR reads a page number in a frame or an ornament badly, so no reading is taken alone. The pages around
    a page (`PRINTED_WINDOW` on each side) vote with the offset between their printed number and their
    place. The page's own reading stands (`page`) when at least two of them share its offset (it is part of a
    run of numbers that agree); otherwise the offset most of them share, held by two or more and by most,
    gives the number (`inferred`: a page without a reading, or a misread one); otherwise it is unknown
    (`none`, cited as «صفحة المسح N»).
    """
    rows = list(rows)
    known = {number: int(printed) for number, printed in rows if printed and str(printed).isdigit()}
    out: dict[int, tuple[str | None, str]] = {}
    for number, _printed in rows:
        offsets = Counter(
            known[other] - other
            for other in range(number - PRINTED_WINDOW, number + PRINTED_WINDOW + 1)
            if other != number and other in known
        )
        own = known.get(number)
        if own is not None and offsets[own - number] >= 2:
            out[number] = (str(own), "page")
            continue
        inferred = None
        if offsets:
            offset, support = offsets.most_common(1)[0]
            if support >= 2 and support * 2 > sum(offsets.values()) and number + offset > 0:
                inferred = number + offset
        out[number] = (str(inferred), "inferred") if inferred is not None else (None, "none")
    return out


class PrintedPages:
    """The printed numbers of the books a call touches, computed once per book (`infer_printed`)."""

    def __init__(self) -> None:
        self._books: dict[int, dict[int, tuple[str | None, str]]] = {}

    def of(self, book_id: int) -> dict[int, tuple[str | None, str]]:
        if book_id not in self._books:
            rows = Page.objects.filter(book_id=book_id, is_excluded=False).values_list(
                "number", "printed_number"
            )
            self._books[book_id] = infer_printed(rows)
        return self._books[book_id]

    def ref(self, book_id: int, number: int) -> PageRef:
        printed, source = self.of(book_id).get(number, (None, "none"))
        return PageRef(number=number, printed=printed, printed_source=source)  # type: ignore[arg-type]


# ====================================================================== book details and citations


@dataclass
class BookDetails:
    """What a citation needs from a book: the Book's own fields and the details the owner typed for the
    title page in «التنسيق» (`StyleSheet.front_matter["fields"]`, D47)."""

    book: Book
    author: str = ""
    title: str = ""
    editor: str = ""
    publisher: str = ""
    city: str = ""
    edition: str = ""
    year: str = ""
    volume: str = ""

    def ref(self) -> BookRef:
        return BookRef(id=self.book.pk, title=self.title, author=self.author)


def book_details(books: Iterable[Book]) -> dict[int, BookDetails]:
    """`BookDetails` of each book: author and title from «التنسيق»'s book details when typed there, else the
    Book's; the editor (المحقق), publisher, city and edition from those details; the year from the Book
    (`original_year`, the printed edition's) else the details; the volume from the Book. One query."""
    from editor.models import StyleSheet

    books = list(books)
    fields = {
        book_id: ((front or {}).get("fields") or {})
        for book_id, front in StyleSheet.objects.filter(book__in=books).values_list("book_id", "front_matter")
    }
    out = {}
    for book in books:
        typed = {key: str(value or "").strip() for key, value in (fields.get(book.pk) or {}).items()}
        out[book.pk] = BookDetails(
            book=book,
            author=typed.get("author") or book.author or "",
            title=typed.get("title") or book.title,
            editor=typed.get("editor", ""),
            publisher=typed.get("publisher", ""),
            city=typed.get("city", ""),
            edition=typed.get("edition", ""),
            year=str(book.original_year) if book.original_year else typed.get("year", ""),
            volume=str(getattr(book, "volume", "") or ""),
        )
    return out


def _page_label(start: PageRef, end: PageRef | None) -> str | None:
    """«45» or «45–46» from printed numbers; None when the start's is unknown."""
    if start.printed is None:
        return None
    if end is not None and end.number != start.number and end.printed is not None:
        return f"{start.printed}–{end.printed}"
    return start.printed


def citation_parts(
    details: BookDetails, kind: str, start: PageRef, end: PageRef | None = None
) -> CitationParts:
    return CitationParts(
        author=details.author,
        title=details.title,
        editor=details.editor,
        publisher=details.publisher,
        city=details.city,
        edition=details.edition,
        year=details.year,
        volume=details.volume,
        page=_page_label(start, end),
        scan_page=start.number,
        kind=kind,  # type: ignore[arg-type]
    )


def citation_text(parts: CitationParts) -> str:
    """«المؤلف، العنوان، تحقيق: المحقق، الناشر، الطبعة، السنة، ج V، ص N» (parts left out when empty; a note
    is «ص N (الحاشية)»; without a printed number, «صفحة المسح N»)."""
    edition = parts.edition
    if edition.isdigit():
        edition = f"ط {edition}"
    items = [
        parts.author,
        parts.title,
        f"تحقيق: {parts.editor}" if parts.editor else "",
        parts.publisher,
        edition,
        parts.year,
        f"ج {parts.volume}" if parts.volume else "",
    ]
    page = f"ص {parts.page}" if parts.page else f"صفحة المسح {parts.scan_page}"
    if parts.kind == "notes":
        page += " (الحاشية)"
    items.append(page)
    return "، ".join(item for item in items if item)


# ====================================================================== passages


@dataclass(frozen=True)
class PassageKey:
    """A parsed `passage_id`."""

    book_id: int
    page: int
    kind: str
    start: tuple[int, int]
    end_page: int
    end: tuple[int, int]

    def __str__(self) -> str:
        head = f"{self.book_id}:{self.page}:{KIND_CHAR[self.kind]}:{self.start[0]}.{self.start[1]}"
        tail = f"{self.end[0]}.{self.end[1]}"
        return f"{head}-{tail}" if self.end_page == self.page else f"{head}-{self.end_page}:{tail}"


def parse_passage_id(value: str) -> PassageKey:
    match = _PASSAGE.match(str(value or "").strip())
    if match is None:
        raise PassageNotFound("المقطع غير موجود.")
    book, page, kind, l1, i1, page2, l2, i2 = match.groups()
    return PassageKey(
        book_id=int(book),
        page=int(page),
        kind=CHAR_KIND[kind],
        start=(int(l1), int(i1)),
        end_page=int(page2) if page2 else int(page),
        end=(int(l2), int(i2)),
    )


@dataclass
class Span:
    """Words of one book and kind over one or more consecutive pages: each word dict carries its page
    (`p`: number, `pid`: id) next to the index's keys."""

    book_id: int
    kind: str
    words: list[dict] = field(default_factory=list)

    @property
    def norms(self) -> list[str]:
        return [word["norm"] for word in self.words]


def _with_page(words: Sequence[dict], page: Page) -> list[dict]:
    return [{**word, "p": page.number, "pid": page.pk} for word in words]


def passage_key(book_id: int, kind: str, words: Sequence[dict]) -> PassageKey:
    first, last = words[0], words[-1]
    return PassageKey(
        book_id=book_id,
        page=first["p"],
        kind=kind,
        start=(first["line"], first["i"]),
        end_page=last["p"],
        end=(last["line"], last["i"]),
    )


def review_state(words: Sequence[dict]) -> str:
    states = {word["state"] for word in words}
    if states == {index.REVIEWED}:
        return "reviewed"
    if index.REVIEWED in states:
        return "partly_reviewed"
    return "unreviewed"


def _base(base_url: str | None) -> str:
    """The absolute base of the links (the API passes the request's; the MCP server the site's)."""
    value = base_url if base_url is not None else keys.site_url()
    return str(value or "").rstrip("/")


def review_link(book_id: int, number: int, base_url: str | None = None) -> str:
    return _base(base_url) + reverse("review:page", args=[book_id, number])


def clip_links(
    words: Sequence[dict], emphasis: Iterable[tuple[int, int]] = (), base_url: str | None = None
) -> list[str]:
    """One signed clip link per page of `words`: the lines they sit on, highlighted; `emphasis` words
    `(line, token)` underlined (the doubtful readings a check points at)."""
    pages: dict[int, list[int]] = defaultdict(list)
    for word in words:
        if word["line"] not in pages[word["pid"]]:
            pages[word["pid"]].append(word["line"])
    marked = set(emphasis)
    out = []
    for page_id, line_ids in pages.items():
        page_words = [(line, i) for line, i in marked if line in line_ids]
        out.append(clips.clip_url(page_id, line_ids, page_words, base_url=_base(base_url)))
    return out


# ====================================================================== rows and sequences


def _rows(book_ids: Sequence[int], kinds: Sequence[str]) -> QuerySet[PageText]:
    return PageText.objects.filter(book_id__in=book_ids, kind__in=kinds, page__is_excluded=False).exclude(
        norm=""
    )


def _prefilter(rows: QuerySet[PageText], norms: Sequence[str]) -> QuerySet[PageText]:
    """Rows whose text contains one of the query's longest distinct words (`norm LIKE '%w%'`, served by
    the trigram index on PostgreSQL)."""
    distinct = sorted({w for w in norms if w}, key=len, reverse=True)
    if not distinct:
        return rows.none()
    condition = Q()
    for word in distinct[:PREFILTER_WORDS]:
        condition |= Q(norm__contains=word)
    return rows.filter(condition)


def _neighbour_rows(rows: Iterable[PageText]) -> dict[tuple[int, str, int], PageText]:
    """The rows of the pages next to `rows` (same book and kind, `PRINTED_WINDOW`-free: up to two pages on
    each side, so an excluded page in between is stepped over), keyed `(book, kind, page number)`."""
    wanted: dict[tuple[int, str], set[int]] = defaultdict(set)
    for row in rows:
        wanted[(row.book_id, row.kind)].update(row.page.number + d for d in (-2, -1, 1, 2))
    out: dict[tuple[int, str, int], PageText] = {}
    for (book_id, kind), numbers in wanted.items():
        for row in PageText.objects.filter(
            book_id=book_id, kind=kind, page__number__in=numbers, page__is_excluded=False
        ).select_related("page"):
            out[(book_id, kind, row.page.number)] = row
    return out


def _next_of(row: PageText, around: dict, step: int) -> PageText | None:
    for distance in (1, 2):
        found = around.get((row.book_id, row.kind, row.page.number + step * distance))
        if found is not None:
            return found
    return None


def _first_line(row: PageText | None) -> list[dict]:
    if row is None or not row.words:
        return []
    line = row.words[0]["line"]
    return [word for word in row.words if word["line"] == line]


def _is_running_head(row: PageText, others: Iterable[PageText | None]) -> bool:
    """The first line of `row` is a running head: short, and the first line of a page around it reads the
    same (`assembly.pipeline.heads_match`, D49)."""
    first = _first_line(row)
    if not first or len(first) > HEAD_MAX_WORDS or len(first) == len(row.words):
        return False
    key = head_key(" ".join(word["text"] for word in first))
    if not key:
        return False
    for other in others:
        other_first = _first_line(other)
        if other_first and heads_match(key, head_key(" ".join(word["text"] for word in other_first))):
            return True
    return False


def _join(row: PageText, around: dict) -> Span:
    """`row` with the pages before and after it (same book and kind): the span a quotation may run over.
    A running head at the top of a page that follows another is left out."""
    previous = _next_of(row, around, -1)
    following = _next_of(row, around, +1)
    span = Span(book_id=row.book_id, kind=row.kind)
    if previous is not None:
        span.words += _with_page(previous.words, previous.page)
    words = row.words
    if previous is not None and _is_running_head(row, [previous, following]):
        words = words[len(_first_line(row)) :]
    span.words += _with_page(words, row.page)
    if following is not None:
        words = following.words
        if _is_running_head(following, [row, previous]):
            words = words[len(_first_line(following)) :]
        span.words += _with_page(words, following.page)
    return span


# ====================================================================== search


def _phrase_starts(norms: Sequence[str], query: Sequence[str]) -> list[int]:
    n, k = len(norms), len(query)
    first = query[0]
    return [s for s in range(n - k + 1) if norms[s] == first and list(norms[s : s + k]) == list(query)]


def _smallest_window(norms: Sequence[str], query: Sequence[str]) -> tuple[int, int] | None:
    """The smallest `[start, end)` holding every distinct query word, None when one is missing."""
    need = Counter(set(query))
    if not set(need) <= set(norms):
        return None
    have: Counter = Counter()
    missing = len(need)
    best: tuple[int, int] | None = None
    start = 0
    for end, word in enumerate(norms):
        if word in need:
            have[word] += 1
            if have[word] == 1:
                missing -= 1
        while missing == 0:
            if best is None or end + 1 - start < best[1] - best[0]:
                best = (start, end + 1)
            gone = norms[start]
            if gone in need:
                have[gone] -= 1
                if have[gone] == 0:
                    missing += 1
            start += 1
    return best


def _char_window(norms: Sequence[str], a: int, b: int) -> tuple[int, int]:
    """Word range `[start, end)` covering the characters `[a, b)` of `" ".join(norms)`."""
    starts, offset = [], 0
    for word in norms:
        starts.append(offset)
        offset += len(word) + 1
    first = max(0, bisect.bisect_right(starts, a) - 1)
    last = max(first, bisect.bisect_left(starts, b) - 1)
    return first, last + 1


@dataclass
class _Found:
    mode: str
    score: float
    row: PageText
    start: int
    end: int  # exclusive, in the row's words; past its end when the match runs onto `next_row`
    next_row: PageText | None = None


def search(
    user,
    query: str,
    book_ids: Iterable[int] | None = None,
    kind: str = "all",
    limit: int = 20,
    offset: int = 0,
    base_url: str | None = None,
) -> SearchResult:
    """Search the account's books (see the module docstring); `limit` is capped at `MAX_LIMIT`."""
    limit = max(1, min(int(limit or 20), MAX_LIMIT))
    offset = max(0, int(offset or 0))
    kind = kind if kind in ("body", "notes", "all") else "all"
    words = words_of(query)
    norms = [word.norm for word in words]
    result = SearchResult(
        query=query,
        normalized=" ".join(norms),
        kind=kind,
        total=0,
        offset=offset,
        limit=limit,
        hits=[],
        message="",
    )
    if not norms:
        result.message = "اكتب كلمة للبحث."
        return result
    ids = _book_ids(user, book_ids)
    index.refresh_stale(ids)
    kinds = index.KINDS if kind == "all" else (kind,)
    rows = list(
        _prefilter(_rows(ids, kinds), norms)
        .select_related("page")
        .only("id", "book_id", "kind", "norm", "page__id", "page__number", "page__book_id")
        .order_by("book_id", "kind", "page__number")
    )
    found = _search_rows(rows, norms)
    found.sort(key=_hit_order)
    result.total = len(found)
    page_of = found[offset : offset + limit]
    result.hits = _hits(page_of, norms, base_url)
    result.message = _search_message(result)
    return result


MODES: tuple[str, ...] = ("phrase", "all_words", "fuzzy")


def _hit_order(found: _Found) -> tuple:
    """Phrases, then all-words windows, in book and page order; then fuzzy matches, best first."""
    score = -found.score if found.mode == "fuzzy" else 0.0
    row = found.row
    return (
        MODES.index(found.mode),
        score,
        row.book_id,
        index.KINDS.index(row.kind),
        row.page.number,
        found.start,
    )


def _search_rows(rows: list[PageText], query: list[str]) -> list[_Found]:
    """Phrase matches (also across a page break), all-words windows, then fuzzy matches."""
    found: list[_Found] = []
    seen_rows: set[int] = set()
    k = len(query)
    tails: list[tuple[PageText, int]] = []  # rows whose last words start the phrase
    heads: list[tuple[PageText, int]] = []  # rows whose first words end it
    for row in rows:
        norms = row.norm.split(" ")
        starts = _phrase_starts(norms, query)
        for start in starts:
            found.append(_Found("phrase", 100.0, row, start, start + k))
        if starts:
            seen_rows.add(row.pk)
        for cut in range(1, k):
            if norms[-cut:] == query[:cut]:
                tails.append((row, cut))
            if norms[:cut] == query[k - cut :]:
                heads.append((row, cut))
    found += _across_pages(tails, heads, query)
    seen_rows.update(f.row.pk for f in found)
    for row in rows:
        if row.pk in seen_rows:
            continue
        norms = row.norm.split(" ")
        window = _smallest_window(norms, query) if k > 1 else None
        if window is not None:
            found.append(_Found("all_words", 100.0, row, window[0], window[1]))
            continue
        if k < 2 and len(query[0]) < 4:
            continue  # one short word: a fuzzy match would be noise
        aligned = fuzz.partial_ratio_alignment(" ".join(query), row.norm, score_cutoff=FUZZY_MIN)
        if aligned is not None and aligned.score >= FUZZY_MIN:
            start, end = _char_window(norms, aligned.dest_start, aligned.dest_end)
            found.append(_Found("fuzzy", round(aligned.score, 1), row, start, end))
    return found


def _across_pages(
    tails: list[tuple[PageText, int]], heads: list[tuple[PageText, int]], query: list[str]
) -> list[_Found]:
    """Phrases that start at the end of one page and end at the top of the next (running head left out)."""
    if not tails and not heads:
        return []
    k = len(query)
    rows = {row.pk: row for row, _cut in tails + heads}
    around = _neighbour_rows(rows.values())
    out: list[_Found] = []
    seen: set[tuple[int, int]] = set()
    for row, cut in tails:
        following = _next_of(row, around, +1)
        if following is None:
            continue
        skip = len(_first_line(following)) if _is_running_head(following, [row]) else 0
        head = following.norm.split(" ")[skip : skip + k - cut]
        if head == query[cut:] and (row.pk, cut) not in seen:
            seen.add((row.pk, cut))
            n = len(row.norm.split(" "))
            out.append(_Found("phrase", 100.0, row, n - cut, n + (k - cut), following))
    for row, cut in heads:
        previous = _next_of(row, around, -1)
        if previous is None:
            continue
        need = k - cut
        tail = previous.norm.split(" ")[-need:]
        if tail == query[:need] and (previous.pk, need) not in seen:
            seen.add((previous.pk, need))
            n = len(previous.norm.split(" "))
            out.append(_Found("phrase", 100.0, previous, n - need, n + cut, row))
    return out


def _hits(found: list[_Found], query: list[str], base_url: str | None) -> list[Hit]:
    """The hits of one result page: the rows' words loaded only for them."""
    if not found:
        return []
    ids = {f.row.pk for f in found} | {f.next_row.pk for f in found if f.next_row is not None}
    full = {row.pk: row for row in PageText.objects.filter(pk__in=ids).select_related("page")}
    details = book_details(Book.objects.filter(pk__in={f.row.book_id for f in found}))
    printed = PrintedPages()
    hits = []
    for f in found:
        row = full[f.row.pk]
        words = _with_page(row.words, row.page)
        if f.next_row is not None:
            nxt = full[f.next_row.pk]
            following = nxt.words
            if _is_running_head(nxt, [row]):
                following = following[len(_first_line(nxt)) :]
            words += _with_page(following, nxt.page)
        matched = words[f.start : f.end]
        if not matched:
            continue
        before = words[max(0, f.start - CONTEXT_WORDS) : f.start]
        after = words[f.end : f.end + CONTEXT_WORDS]
        key = passage_key(row.book_id, row.kind, matched)
        info = details[row.book_id]
        start_ref = printed.ref(row.book_id, key.page)
        end_ref = printed.ref(row.book_id, key.end_page) if key.end_page != key.page else None
        doubtful = [(w["line"], w["i"]) for w in matched if w["state"] == index.DOUBTFUL]
        hits.append(
            Hit(
                passage_id=str(key),
                book=info.ref(),
                page=start_ref,
                end_page=end_ref,
                kind=row.kind,
                mode=f.mode,
                score=f.score,
                before=("… " if f.start > CONTEXT_WORDS else "") + " ".join(w["text"] for w in before),
                match=" ".join(w["text"] for w in matched),
                after=" ".join(w["text"] for w in after)
                + (" …" if f.end + CONTEXT_WORDS < len(words) else ""),
                words=[WordState(text=w["text"], state=w["state"]) for w in matched],
                doubtful=len(doubtful),
                review_state=review_state(matched),
                clip_url=clip_links(matched, doubtful, base_url)[0],
                review_url=review_link(row.book_id, key.page, base_url),
                citation=citation_text(citation_parts(info, row.kind, start_ref, end_ref)),
            )
        )
    return hits


def _search_message(result: SearchResult) -> str:
    if not result.total:
        return f"لا نتائج لـ«{result.query.strip()}» في كتب هذا الحساب."
    first = result.hits[0] if result.hits else None
    text = f"{ar_count(result.total, RESULTS)} لـ«{result.query.strip()}»."
    if first is not None and result.offset == 0:
        page = f"ص {first.page.printed}" if first.page.printed else f"صفحة المسح {first.page.number}"
        text += f" الأولى في «{first.book.title}»، {page} ({KIND_LABEL[first.kind]})."
    return text


# ====================================================================== quotation checking


@dataclass
class _Alignment:
    ratio: float
    span: Span
    start: int  # the passage in span.words: [start, end)
    end: int
    ops: list[tuple[str, int, int, int, int]]  # SequenceMatcher opcodes, j relative to `start`


def _codes(words: Sequence[str], vocabulary: dict[str, int]) -> str:
    """One private-use character per distinct word, so rapidfuzz aligns words, not letters."""
    return "".join(chr(0xF0000 + vocabulary.setdefault(word, len(vocabulary))) for word in words)


def align(query: Sequence[str], span: Span) -> _Alignment | None:
    """The best window of `span` for the normalised quotation `query`.

    rapidfuzz's `partial_ratio_alignment` over word codes locates it; `difflib.SequenceMatcher` aligns the
    quotation with that window widened by a third of its length on each side, and the window is then cut
    to the aligned words: source words before the first or after the last aligned word are not part of it
    (a leading or trailing replacement keeps as many source words as the quotation has there). The ratio is
    `2 × matched / (quotation + window)` on words.
    """
    source = span.norms
    if not source or not query:
        return None
    vocabulary: dict[str, int] = {}
    qs, ss = _codes(query, vocabulary), _codes(source, vocabulary)
    located = fuzz.partial_ratio_alignment(qs, ss)
    if located is None or located.score <= 0:
        return None
    lo, hi = (located.dest_start, located.dest_end) if len(qs) <= len(ss) else (0, len(ss))
    slack = max(3, len(query) // 3)
    lo, hi = max(0, lo - slack), min(len(source), hi + slack)
    ops = SequenceMatcher(None, list(query), source[lo:hi], autojunk=False).get_opcodes()
    while ops and ops[0][0] == "insert":
        ops.pop(0)
    while ops and ops[-1][0] == "insert":
        ops.pop()
    if not ops or not any(op[0] == "equal" for op in ops):
        return None
    tag, i1, i2, j1, j2 = ops[0]
    if tag == "replace" and j2 - j1 > i2 - i1:
        ops[0] = (tag, i1, i2, j2 - (i2 - i1), j2)
    tag, i1, i2, j1, j2 = ops[-1]
    if tag == "replace" and j2 - j1 > i2 - i1:
        ops[-1] = (tag, i1, i2, j1, j1 + (i2 - i1))
    first_j, last_j = ops[0][3], ops[-1][4]
    start, end = lo + first_j, lo + last_j
    matched = sum(op[2] - op[1] for op in ops if op[0] == "equal")
    ratio = 2 * matched / (len(query) + (end - start))
    shifted = [(tag, i1, i2, j1 - first_j, j2 - first_j) for tag, i1, i2, j1, j2 in ops]
    return _Alignment(ratio=ratio, span=span, start=start, end=end, ops=shifted)


def _candidates(user, norms: Sequence[str], book_id: int | None) -> list[PageText]:
    """The rows most likely to hold the quotation: by how many of its distinct words they contain, then (the
    first `VERIFY_CANDIDATES × 4` of them) by rapidfuzz's `partial_ratio` of the quotation on the page — a
    common isnad holds the same words on many pages; with `book_id`, that book's best rows are always among
    them."""
    ids = _book_ids(user)
    index.refresh_stale(ids)
    rows = list(_prefilter(_rows(ids, index.KINDS), norms).select_related("page"))
    wanted = set(norms)
    text = " ".join(norms)
    by_words = sorted(
        rows, key=lambda row: (-len(wanted & set(row.norm.split(" "))), row.book_id, row.page.number)
    )
    shortlist = by_words[: VERIFY_CANDIDATES * 4]
    overlap = {row.pk: len(wanted & set(row.norm.split(" "))) for row in shortlist}
    scored = sorted(shortlist, key=lambda row: (-overlap[row.pk], -fuzz.partial_ratio(text, row.norm)))
    chosen = scored[:VERIFY_CANDIDATES]
    if book_id is not None:
        own = [row for row in by_words if row.book_id == book_id and row not in chosen]
        chosen += own[: VERIFY_CANDIDATES // 2]
    return chosen


def _preferred_kind(attributed_to: str) -> str:
    return "notes" if attributed_to == "editor" else "body"


def _best(alignments: list[_Alignment], attributed_to: str) -> _Alignment | None:
    """The best alignment: the highest ratio; on a tie the kind the quotation is attributed to."""
    if not alignments:
        return None
    preferred = _preferred_kind(attributed_to)
    return max(alignments, key=lambda a: (round(a.ratio, 6), a.span.kind == preferred))


def verify_quote(
    user,
    quote: str,
    book_id: int | None = None,
    attributed_to: str = "unknown",
    base_url: str | None = None,
) -> VerifyResult:
    """Check a quotation against the account's books (see the module docstring)."""
    attributed_to = attributed_to if attributed_to in ("author", "editor", "unknown") else "unknown"
    words = words_of(quote)
    norms = [word.norm for word in words]
    result = VerifyResult(
        status="too_short",
        message=MESSAGES["too_short"],
        quote=quote,
        normalized=" ".join(norms),
        ratio=0.0,
        diacritics_differ=False,
    )
    if book_id is not None:
        get_book_or_404(user, book_id)  # another organisation's book: 404, as for a missing one
    if len(norms) < MIN_QUOTE_WORDS:
        return result
    rows = _candidates(user, norms, book_id)
    around = _neighbour_rows(rows)
    alignments = [found for row in rows if (found := align(norms, _join(row, around))) is not None]
    best = _best(alignments, attributed_to)
    attribution = "ok"
    if book_id is not None:
        in_book = _best([a for a in alignments if a.span.book_id == book_id], attributed_to)
        if in_book is not None and in_book.ratio >= NOT_FOUND_RATIO:
            best = in_book
        elif best is not None and best.ratio >= NOT_FOUND_RATIO:
            attribution = "other_book"
    if best is None or best.ratio < NOT_FOUND_RATIO:
        result.status = "not_found"
        result.message = MESSAGES["not_found"]
        result.ratio = round(best.ratio, 3) if best is not None else 0.0
        return result
    return _verdict(result, words, best, attributed_to, attribution, base_url)


def _states(words: Sequence[dict]) -> str | None:
    """The review state that sums up the book's words of a change: doubtful when all are, else the most
    trusted one present."""
    states = {word["state"] for word in words}
    if not states:
        return None
    if states == {index.DOUBTFUL}:
        return index.DOUBTFUL
    return index.REVIEWED if index.REVIEWED in states else index.UNREVIEWED


def _added_uncertain(words: Sequence[dict], j: int) -> bool:
    """Words the quotation adds before `words[j]` are uncertain when a word next to the place is a doubtful
    reading, or the OCR offered a skipped word there (an open suggestion: `gap` on the word it follows, or
    on the first word of a line when it comes before it)."""
    before = words[j - 1] if 0 < j <= len(words) else None
    after = words[j] if 0 <= j < len(words) else None
    if any(word is not None and word["state"] == index.DOUBTFUL for word in (before, after)):
        return True
    if before is not None and before.get("gap"):
        return True
    return bool(
        after is not None and after.get("gap") and (before is None or before["line"] != after["line"])
    )


def _verdict(
    result: VerifyResult, quote: list[Word], best: _Alignment, attributed_to: str, attribution: str, base_url
) -> VerifyResult:
    passage = best.span.words[best.start : best.end]
    changes: list[Change] = []
    diff: list[DiffSegment] = []
    diacritics: list[DiacriticDifference] = []
    emphasis: list[tuple[int, int]] = []
    added: list[tuple[int, int]] = []  # (change index, diff index) of words the quotation adds
    missing: list[tuple[int, int]] = []

    def text(words) -> str:
        return " ".join(word["text"] if isinstance(word, dict) else word.raw for word in words)

    for tag, i1, i2, j1, j2 in best.ops:
        src = passage[j1:j2]
        if tag == "equal":
            for q_word, s_word in zip(quote[i1:i2], src, strict=True):
                if diacritics_differ(q_word.raw, s_word["text"]):
                    diacritics.append(DiacriticDifference(quote=q_word.raw, source=s_word["text"]))
            diff.append(DiffSegment(op="equal", source=text(src)))
            continue
        if tag == "delete":
            uncertain = _added_uncertain(best.span.words, best.start + j1)
            added.append((len(changes), len(diff)))
            changes.append(Change(type="added", quote=text(quote[i1:i2]), source="", needs_image=uncertain))
            diff.append(DiffSegment(op="added", quote=text(quote[i1:i2]), needs_image=uncertain))
            continue
        state = _states(src)
        uncertain = state == index.DOUBTFUL
        if uncertain:
            emphasis += [(word["line"], word["i"]) for word in src]
        if tag == "insert":
            missing.append((len(changes), len(diff)))
            changes.append(
                Change(type="missing", quote="", source=text(src), source_state=state, needs_image=uncertain)
            )
            diff.append(DiffSegment(op="missing", source=text(src), state=state, needs_image=uncertain))
        else:
            changes.append(
                Change(
                    type="replaced",
                    quote=text(quote[i1:i2]),
                    source=text(src),
                    source_state=state,
                    needs_image=uncertain,
                )
            )
            diff.append(
                DiffSegment(
                    op="replaced",
                    source=text(src),
                    quote=text(quote[i1:i2]),
                    state=state,
                    needs_image=uncertain,
                )
            )
    changes = _moved(changes, diff, added, missing)
    result.ratio = round(best.ratio, 3)
    result.changes = changes
    result.diff = diff
    result.diacritics = diacritics
    if not changes:
        result.status = "exact"
        result.diacritics_differ = bool(diacritics)
        result.message = (
            f"الكلمات مطابقة لما في الكتاب، ويختلف الشكل في {ar_count(len(diacritics), WORDS)}."
            if diacritics
            else MESSAGES["exact"]
        )
    elif all(change.needs_image for change in changes):
        result.status = "needs_image_check"
        result.message = MESSAGES["needs_image_check"]
    else:
        result.status = "differs"
        result.message = MESSAGES["differs"].format(n=ar_count(len(changes), PLACES))
    kind = best.span.kind
    if attribution == "ok" and attributed_to == "author" and kind == "notes":
        attribution = "note_not_author"
    elif attribution == "ok" and attributed_to == "editor" and kind == "body":
        attribution = "body_not_editor"
    key = passage_key(best.span.book_id, kind, passage)
    details = book_details(Book.objects.filter(pk=best.span.book_id))[best.span.book_id]
    printed = PrintedPages()
    start_ref = printed.ref(key.book_id, key.page)
    end_ref = printed.ref(key.book_id, key.end_page) if key.end_page != key.page else None
    links = clip_links(passage, emphasis, base_url)
    result.attribution = attribution
    if attribution == "other_book":
        result.attribution_message = f"لم يوجد في الكتاب المحدد، ووُجد في «{details.title}»."
    else:
        result.attribution_message = ATTRIBUTION_MESSAGES.get(attribution, "")
    result.passage_id = str(key)
    result.book = details.ref()
    result.page = start_ref
    result.end_page = end_ref
    result.kind = kind
    result.source_text = text(passage)
    result.review_state = review_state(passage)
    result.clip_url = links[0] if links else None
    result.clip_urls = links
    result.review_url = review_link(key.book_id, key.page, base_url)
    result.citation = citation_text(citation_parts(details, kind, start_ref, end_ref))
    return result


def _moved(changes: list[Change], diff: list[DiffSegment], added, missing) -> list[Change]:
    """A word the quotation adds in one place and leaves out in another is the same word moved: the two
    changes become one `moved` (the diff marks both places)."""
    used: set[int] = set()
    out_moved: dict[int, Change] = {}
    for a_change, a_diff in added:
        for m_change, m_diff in missing:
            if m_change in used:
                continue
            quote_norm = " ".join(w.norm for w in words_of(changes[a_change].quote))
            source_norm = " ".join(w.norm for w in words_of(changes[m_change].source))
            if quote_norm and quote_norm == source_norm:
                used.update({a_change, m_change})
                missing_change = changes[m_change]
                out_moved[min(a_change, m_change)] = Change(
                    type="moved",
                    quote=changes[a_change].quote,
                    source=missing_change.source,
                    source_state=missing_change.source_state,
                    needs_image=missing_change.needs_image,
                )
                diff[a_diff].op = "moved"
                diff[m_diff].op = "moved"
                break
    return [out_moved.get(n, change) for n, change in enumerate(changes) if n not in used or n in out_moved]


# ====================================================================== passages, citations, books


def _passage_words(user, key: PassageKey) -> tuple[Book, Span, list[Page]]:
    """The words of a passage (and its book, its span over one or two pages, its pages)."""
    book = get_book_or_404(user, key.book_id)
    numbers = sorted({key.page, key.end_page})
    if numbers[-1] - numbers[0] > 2:
        raise PassageNotFound("المقطع غير موجود.")
    rows = {
        row.page.number: row
        for row in PageText.objects.filter(book=book, kind=key.kind, page__number__in=numbers).select_related(
            "page"
        )
    }
    if any(number not in rows for number in numbers):
        index.refresh_stale([book.pk])
        rows = {
            row.page.number: row
            for row in PageText.objects.filter(
                book=book, kind=key.kind, page__number__in=numbers
            ).select_related("page")
        }
    words: list[dict] = []
    for number in numbers:
        if number in rows:
            words += _with_page(rows[number].words, rows[number].page)
    start = next(
        (n for n, w in enumerate(words) if (w["line"], w["i"]) == key.start and w["p"] == key.page), None
    )
    end = None
    for n, w in enumerate(words):
        if (w["line"], w["i"]) == key.end and w["p"] == key.end_page:
            end = n
    if start is None or end is None or end < start:
        raise PassageNotFound("المقطع غير موجود؛ ربما تغيّرت أسطره بعد المراجعة. ابحث عنه من جديد.")
    pages = [rows[number].page for number in numbers if number in rows]
    return book, Span(book_id=book.pk, kind=key.kind, words=words[start : end + 1]), pages


def _line_items(
    lines: Sequence[Line], in_passage: set[int], doubtful: dict[int, list[str]]
) -> list[PassageLine]:
    return [
        PassageLine(
            id=line.pk,
            order=line.order,
            kind=index.kind_of(line) or "body",
            text=line.text,
            bbox=[int(v) for v in line.bbox] if isinstance(line.bbox, list) and len(line.bbox) == 4 else None,
            reviewed=line.is_reviewed,
            in_passage=line.pk in in_passage,
            doubtful_words=doubtful.get(line.pk, []),
        )
        for line in lines
    ]


def get_passage(
    user,
    passage_id: str | None = None,
    book_id: int | None = None,
    page: int | None = None,
    printed_page: str | None = None,
    context: int = 2,
    base_url: str | None = None,
) -> Passage:
    """A passage by id, or a whole page (`book_id` with `page`, its place in the scan, or `printed_page`):
    its words, its lines with `context` lines around (the body and the notes apart), its boxes, doubtful
    words, review state, clip links and citation."""
    context = max(0, min(int(context or 0), 20))
    if passage_id:
        key = parse_passage_id(passage_id)
        book, span, pages = _passage_words(user, key)
    else:
        if book_id is None:
            raise PassageNotFound("حدّد المقطع أو الكتاب والصفحة.")
        book = get_book_or_404(user, book_id)
        number = page
        if number is None and printed_page:
            wanted = str(printed_page).strip()
            numbers = [n for n, (printed, _s) in PrintedPages().of(book.pk).items() if printed == wanted]
            number = numbers[0] if numbers else None
        target = Page.objects.filter(book=book, number=number).first() if number is not None else None
        if target is None:
            raise Http404("الصفحة غير موجودة.")
        index.refresh_stale([book.pk])
        rows = {row.kind: row for row in PageText.objects.filter(page=target)}
        kind = "body" if rows.get("body") and rows["body"].words else "notes"
        words = _with_page(rows[kind].words, target) if rows.get(kind) else []
        if not words:
            raise PassageNotFound("لا نص في هذه الصفحة بعد.")
        key = passage_key(book.pk, kind, words)
        span, pages = Span(book_id=book.pk, kind=kind, words=words), [target]
        context = 10_000  # the whole page
    passage_lines = {word["line"] for word in span.words}
    page_ids = [p.pk for p in pages]
    lines = list(
        Line.objects.filter(page_id__in=page_ids)
        .select_related("region", "page")
        .order_by("page__number", "order", "id")
    )
    doubtful: dict[int, list[str]] = defaultdict(list)
    for word in span.words:
        if word["state"] == index.DOUBTFUL:
            doubtful[word["line"]].append(word["text"])
    same = [line for line in lines if index.kind_of(line) == key.kind]
    positions = [n for n, line in enumerate(same) if line.pk in passage_lines]
    lo = max(0, positions[0] - context) if positions else 0
    hi = min(len(same), positions[-1] + context + 1) if positions else 0
    window = same[lo:hi]
    other = [line for line in lines if index.kind_of(line) not in (None, key.kind)]
    body = window if key.kind == "body" else []
    notes = window if key.kind == "notes" else other
    details = book_details([book])[book.pk]
    printed = PrintedPages()
    start_ref = printed.ref(book.pk, key.page)
    end_ref = printed.ref(book.pk, key.end_page) if key.end_page != key.page else None
    emphasis = [(w["line"], w["i"]) for w in span.words if w["state"] == index.DOUBTFUL]
    links = clip_links(span.words, emphasis, base_url)
    first_page = pages[0]
    return Passage(
        passage_id=str(key),
        book=details.ref(),
        page=start_ref,
        end_page=end_ref,
        kind=key.kind,  # type: ignore[arg-type]
        text=" ".join(word["text"] for word in span.words),
        body=_line_items(body, passage_lines, doubtful),
        notes=_line_items(notes, passage_lines, doubtful),
        page_size=_page_size(first_page),
        doubtful_words=[word["text"] for word in span.words if word["state"] == index.DOUBTFUL],
        review_state=review_state(span.words),  # type: ignore[arg-type]
        clip_url=links[0],
        clip_urls=links,
        review_url=review_link(book.pk, key.page, base_url),
        citation=citation_text(citation_parts(details, key.kind, start_ref, end_ref)),
    )


def _page_size(page: Page) -> list[int]:
    """The size of the image the line boxes refer to: the prepared page (`Preprocess.output_*`), else the
    page's own."""
    from processing.models import Preprocess

    pre = Preprocess.objects.filter(page=page).values_list("output_width", "output_height").first()
    width, height = pre if pre is not None else (0, 0)
    return [int(width or page.width), int(height or page.height)]


def cite(user, passage_id: str) -> Citation:
    """The citation of a passage and its parts (`citation_text`)."""
    key = parse_passage_id(passage_id)
    book, _span, _pages = _passage_words(user, key)
    details = book_details([book])[book.pk]
    printed = PrintedPages()
    start_ref = printed.ref(book.pk, key.page)
    end_ref = printed.ref(book.pk, key.end_page) if key.end_page != key.page else None
    parts = citation_parts(details, key.kind, start_ref, end_ref)
    return Citation(passage_id=str(key), citation=citation_text(parts), parts=parts)


def list_books(user) -> BooksResult:
    """The account's books with their details, size, printed page range and how much of them is reviewed."""
    books = list(books_for(user).order_by("title", "pk"))
    ids = [book.pk for book in books]
    details = book_details(books)
    pages = dict(
        Page.objects.filter(book_id__in=ids, is_excluded=False)
        .values("book_id")
        .annotate(n=Count("id"))
        .values_list("book_id", "n")
    )
    indexed = dict(
        PageText.objects.filter(book_id__in=ids, kind=index.BODY)
        .values("book_id")
        .annotate(n=Count("id"))
        .values_list("book_id", "n")
    )
    lines = {
        row["page__book_id"]: (row["n"], row["r"])
        for row in Line.objects.filter(page__book_id__in=ids)
        .values("page__book_id")
        .annotate(n=Count("id"), r=Count("id", filter=Q(is_reviewed=True)))
    }
    printed = PrintedPages()
    out = []
    for book in books:
        numbers = [int(p) for p, _source in printed.of(book.pk).values() if p is not None and p.isdigit()]
        total, reviewed = lines.get(book.pk, (0, 0))
        info = details[book.pk]
        out.append(
            BookInfo(
                id=book.pk,
                title=info.title,
                author=info.author,
                editor=info.editor,
                publisher=info.publisher,
                edition=info.edition,
                year=info.year,
                volume=info.volume,
                pages=pages.get(book.pk, 0),
                indexed_pages=indexed.get(book.pk, 0),
                printed_range=f"{min(numbers)}–{max(numbers)}" if numbers else None,
                reviewed_share=round(reviewed / total, 3) if total else 0.0,
            )
        )
    message = (
        f"في هذا الحساب {ar_count(len(out), ('كتاب واحد', 'كتابان', 'كتب', 'كتابًا'))}."
        if out
        else "لا كتب في هذا الحساب بعد."
    )
    return BooksResult(books=out, message=message)


__all__ = [
    "BOOK_NOT_FOUND",
    "PassageNotFound",
    "account_books",
    "align",
    "book_details",
    "cite",
    "citation_text",
    "get_passage",
    "infer_printed",
    "list_books",
    "parse_passage_id",
    "search",
    "verify_quote",
]
