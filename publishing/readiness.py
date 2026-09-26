"""Readiness (PHASE6_SPEC §7, D59): what is left to do before exporting, as warnings with links. It never
blocks an export.

Every row is a ready-made `{code, level, message, action?: {label, url}}` object (level `warn`, `info` or
`success`); the export page renders them without knowing any codes. Counts are Arabic phrases with
Western digits (`assembly.render.ar_count`).

**About the book** (`book_readiness`, the same for every format):

- `uncertain_words` (warn): the uncertain words left in the text, and how many of them are numbers Kraken
  read (`editor.uncertain.counts`) → «غير المؤكَّدة» (`?tab=uncertain`);
- `pages_unreviewed` (warn): the book's pages (those the manuscript's assembly run included,
  `run.included`) that are still `ocr_done` now: their text is the models' reading → «المراجعة»
  (`review:next`);
- `pages_missing` (warn): pages of the book (not excluded) that are not in it — still processing, in
  error, or left out as unreviewed → «المراجعة», or «المعالجة» (the dashboard) when none of them can be
  reviewed yet;
- `stray_notes` (warn): body paragraphs that look like footnotes left in the text: a paragraph of one
  source page whose text starts with a note marker («(n)», «[n]» with n from 1 to 15, or «*») followed
  by text, at most 80 words, in the run of such paragraphs that ends its page (a run that is not the
  whole page) → «عرض» (the book page on the first one's chapter, `?chapter=`: the book page does not
  take a block yet);
- `missing_text` (warn, D75): pages of the book (those its assembly run included; every page not
  excluded before the first run) with an open suggestion of words a model may have skipped (an open
  `ocr.TextGap`, D72) → «المراجعة»;
- `review_drift` (warn): pages whose text changed in review after the manuscript was built
  (`editor.services.review_drift`) → «الكتاب»;
- `assembly_running` (warn): an assembly of the book is queued or running;
- `single_reader` (info, D75): pages of the book that one model read (`Page.reading.readers` is `one`,
  or `tesseract` when the page's text is Tesseract's alone; a page read before 7b, `{}`, is not counted)
  and that nobody has reviewed yet (`ocr_done`) → «المراجعة»;
- `book_details` (info): no author for the title page and the file's properties; a copyright page with
  neither publisher nor year → «بيانات الكتاب» (`?tab=format`);
- `no_headings` (info): a contents page but no chapter headings;
- `clear` (success): none of the above — every page of the book reviewed and nothing to note.

**Helpers the exporters use for their notes** (`Exporter.notes`): `missing_font_rows` (a face of the
stylesheet not installed: Amiri stands in), `page_checks_row` (the live layout's page checks, 6b),
`layout_is_current` (the live pages are those of the current text and setup: Word's contents numbers
come from them), `uncertain_counts` (cached on the book instance, so the page payload counts once) and
`layout_url` (the book page on a panel tab).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone

from assembly.render import ar_count
from books.models import Book

WARN = "warn"
INFO = "info"
SUCCESS = "success"

WORDS = ("كلمة واحدة", "كلمتان", "كلمات", "كلمة")
NUMBERS = ("رقم واحد", "رقمان", "أرقام", "رقمًا")
PAGES = ("صفحة واحدة", "صفحتان", "صفحات", "صفحة")
PAGES_OF = ("صفحة واحدة", "صفحتين", "صفحات", "صفحة")  # after «نص»: the genitive
PARAGRAPHS = ("فقرة واحدة", "فقرتان", "فقرات", "فقرة")
CHECKS = ("ملاحظة واحدة", "ملاحظتان", "ملاحظات", "ملاحظة")

ASSEMBLY_RUNNING = "يجري تجميع الكتاب الآن؛ يُخرَج النص كما هو عند بدء الإخراج."
NO_AUTHOR = "بيانات الكتاب بلا مؤلف؛ يُكتب في صفحة العنوان وخصائص الملف."
NO_IMPRINT = "صفحة الحقوق بلا ناشر ولا سنة."
NO_HEADINGS = "لا عناوين فصول في الكتاب؛ ستخلو المحتويات."
CLEAR = "كل الصفحات مُراجَعة ولا ملاحظات؛ الكتاب جاهز للإخراج."
MISSING_FONT = "الخط «{name}» غير مثبّت على هذا الجهاز؛ يُستعمل {fallback} بدلًا منه، كما في المعاينة."

UNCERTAIN_LABEL = "غير المؤكَّدة"
REVIEW_LABEL = "المراجعة"
PROCESSING_LABEL = "المعالجة"
SHOW_LABEL = "عرض"
BOOK_LABEL = "الكتاب"
DETAILS_LABEL = "بيانات الكتاب"
FONTS_LABEL = "الخطوط"

NUMBERS_LISTED = 8  # page numbers a message lists before «…»
NOTE_MARKER_MAX = 15  # «(n)» / «[n]» up to this n reads as a note marker
NOTE_WORDS_MAX = 80  # a stray note is at most this long
# a note marker at a paragraph's start: «(n)», «[n]» (Western or Arabic-Indic digits) or «*»
_RE_NOTE_MARKER = re.compile(r"(?:\(\s*(\d{1,2})\s*\)|\[\s*(\d{1,2})\s*\]|(\*))")
_LEADING = " \t\n\u200e\u200f\u061c\ufeff"  # spaces and direction marks before the marker


def row(code: str, level: str, message: str, action: dict | None = None) -> dict:
    """One readiness (or note) row; `action` is `{label, url}`."""
    out = {"code": code, "level": level, "message": message}
    if action is not None:
        out["action"] = action
    return out


def layout_url(book_id: int, tab: str | None = None) -> str:
    """The book page, on a panel tab (`?tab=uncertain`, `format`, `chapters`…) when given."""
    url = reverse("editor:layout", args=[book_id])
    return f"{url}?tab={tab}" if tab else url


def action(label: str, book_id: int, tab: str | None = None) -> dict:
    """A row's link to the book page (on a panel tab)."""
    return {"label": label, "url": layout_url(book_id, tab)}


# ====================================================================== counts


@dataclass(frozen=True)
class UncertainCounts:
    """The uncertain words left in the text, and how many of them are numbers Kraken read."""

    total: int
    numbers: int


def uncertain_counts(book: Book, document: dict | None = None) -> UncertainCounts:
    """`UncertainCounts` of the book's manuscript (`document` when the caller has it; zero without a
    manuscript). Cached on the `book` instance, so the page payload's readiness and the Word form share
    one count (one query for the readings' OCR lines)."""
    cached = getattr(book, "_nk_uncertain_counts", None)
    if cached is not None:
        return cached
    from editor import uncertain

    if document is None:
        from editor.models import Manuscript

        document = Manuscript.objects.filter(book_id=book.pk).values_list("document", flat=True).first()
    found = uncertain.counts(book, document) if document else {"words": 0, "numbers": 0}
    counts = UncertainCounts(total=found["words"], numbers=found["numbers"])
    book._nk_uncertain_counts = counts
    return counts


def uncertain_message(counts: UncertainCounts) -> str:
    """«بقيت 12 كلمة غير مؤكَّدة في النص، منها 3 أرقام.»"""
    words = ar_count(counts.total, WORDS)
    adjective = "غير مؤكَّدتين" if counts.total == 2 else "غير مؤكَّدة"
    numbers = f"، منها {ar_count(counts.numbers, NUMBERS)}" if counts.numbers else ""
    return f"بقيت {words} {adjective} في النص{numbers}."


# ====================================================================== the pages of the book


def numbers_text(numbers: list[int]) -> str:
    """«3، 4، 7.»: the first `NUMBERS_LISTED` numbers, ending «…» when there are more."""
    shown = "، ".join(str(number) for number in numbers[:NUMBERS_LISTED])
    return shown + ("…" if len(numbers) > NUMBERS_LISTED else ".")


def unreviewed_message(pages: list[int]) -> str:
    """«77 صفحة في الكتاب لم تُراجَع بعد؛ نصّها كما قرأته النماذج: 3، 4، 7…»"""
    if len(pages) == 2:
        return f"صفحتان في الكتاب لم تُراجَعا بعد؛ نصّهما كما قرأته النماذج: {numbers_text(pages)}"
    return (
        f"{ar_count(len(pages), PAGES)} في الكتاب لم تُراجَع بعد؛ نصّها كما قرأته النماذج: {numbers_text(pages)}"
    )


def missing_message(pages: list[int]) -> str:
    """«صفحتان لم تدخلا الكتاب بعد: 91، 92.»"""
    if len(pages) == 2:
        return f"صفحتان لم تدخلا الكتاب بعد: {numbers_text(pages)}"
    return f"{ar_count(len(pages), PAGES)} لم تدخل الكتاب بعد: {numbers_text(pages)}"


def missing_text_message(pages: list[int]) -> str:
    """«نص قد يكون ناقصًا لم يُحسم في 4 صفحات: 3، 7، 12، 30.» (D75)"""
    return f"نص قد يكون ناقصًا لم يُحسم في {ar_count(len(pages), PAGES_OF)}: {numbers_text(pages)}"


def single_reader_message(count: int) -> str:
    """«قُرئت 13 صفحة من الكتاب بنموذج واحد ولم تُراجَع بعد.» (D75)"""
    if count == 2:
        return "قُرئت صفحتان من الكتاب بنموذج واحد ولم تُراجَعا بعد."
    return f"قُرئت {ar_count(count, PAGES)} من الكتاب بنموذج واحد ولم تُراجَع بعد."


def stray_notes_message(count: int, marker: str, pages: list[int]) -> str:
    """«3 فقرات في أواخر صفحاتها تبدأ بعلامة حاشية مثل «(1)» ولم تُربَط حاشيةً: ص 5، 12، 30.»"""
    listed = numbers_text(pages)
    if count == 1:
        return f"فقرة واحدة في آخر صفحتها تبدأ بعلامة حاشية مثل «{marker}» ولم تُربَط حاشيةً: ص {listed}"
    if count == 2:
        return f"فقرتان في أواخر صفحاتهما تبدآن بعلامة حاشية مثل «{marker}» ولم تُربَطا حاشيةً: ص {listed}"
    return (
        f"{ar_count(count, PARAGRAPHS)} في أواخر صفحاتها تبدأ بعلامة حاشية مثل «{marker}» ولم تُربَط"
        f" حاشيةً: ص {listed}"
    )


@dataclass(frozen=True)
class BookPages:
    """Where the pages stand against the book: the numbers of the pages in it (the manuscript's run
    included them) that are still unreviewed (`ocr_done`), of the pages not in it, and whether none of
    those can be reviewed now (they are still in the pipeline or in error)."""

    unreviewed: list[int]
    missing: list[int]
    missing_in_pipeline: bool


@dataclass(frozen=True)
class TrustPages:
    """The pages whose text nobody has checked yet (D75): the numbers of the pages with an open
    suggestion of missing words, and of the unreviewed pages that one model read."""

    missing_text: list[int]
    single_reader: list[int]


SINGLE_READERS: frozenset[str] = frozenset({"one", "tesseract"})  # `Page.reading.readers` (D73)


def trust_pages(book: Book, manuscript) -> TrustPages:
    """`TrustPages` of the book (two queries): the pages its manuscript's run included (every page not
    excluded when there is no run) with an open `ocr.TextGap`, and those still `ocr_done` whose
    `reading.readers` is one model or Tesseract alone."""
    from assembly.pipeline import UNREVIEWED_STATUS
    from books.models import Page
    from ocr.models import TextGap

    run = getattr(manuscript, "run", None)
    pages = Page.objects.filter(book_id=book.pk, is_excluded=False).exclude(status=Page.Status.EXCLUDED)
    if run is not None:
        pages = pages.filter(pk__in=[int(key) for key in (run.included or {}) if str(key).isdigit()])
    gaps = (
        TextGap.objects.filter(page__in=pages, status=TextGap.Status.OPEN)
        .order_by("page__number")
        .values_list("page__number", flat=True)
        .distinct()
    )
    single = [
        number
        for number, reading in pages.filter(status=UNREVIEWED_STATUS)
        .order_by("number")
        .values_list("number", "reading")
        if isinstance(reading, dict) and reading.get("readers") in SINGLE_READERS
    ]
    return TrustPages(missing_text=list(dict.fromkeys(gaps)), single_reader=single)


def book_pages(book: Book, manuscript) -> BookPages | None:
    """`BookPages` of the book (one query); None when the manuscript has no assembly run to tell which
    pages it holds."""
    from assembly.pipeline import UNREVIEWED_STATUS
    from books.models import Page

    run = getattr(manuscript, "run", None)
    if run is None:
        return None
    included = {int(key) for key in (run.included or {}) if str(key).isdigit()}
    rows = (
        Page.objects.filter(book_id=book.pk, is_excluded=False)
        .exclude(status=Page.Status.EXCLUDED)
        .order_by("number")
        .values_list("id", "number", "status")
    )
    unreviewed: list[int] = []
    missing: list[tuple[int, str]] = []
    for pk, number, status in rows:
        if pk not in included:
            missing.append((number, status))
        elif status == UNREVIEWED_STATUS:
            unreviewed.append(number)
    reviewable = {UNREVIEWED_STATUS, Page.Status.REVIEWED, Page.Status.ASSEMBLED}
    return BookPages(
        unreviewed=unreviewed,
        missing=[number for number, _status in missing],
        missing_in_pipeline=bool(missing) and not any(status in reviewable for _n, status in missing),
    )


def note_marker(text: str) -> str | None:
    """The note marker a paragraph's text starts with, as shown (Western digits): «(1)», «[2]» (n from
    1 to `NOTE_MARKER_MAX`) or «*», when some text follows it; None otherwise («(22) …», «* * *», a lone
    «(5)» — a page number — are not notes)."""
    text = str(text or "").lstrip(_LEADING)
    found = _RE_NOTE_MARKER.match(text)
    if found is None or not any(char.isalpha() for char in text[found.end() :]):
        return None
    if found.group(3):
        return "*"
    digits = found.group(1) or found.group(2)
    number = int(digits)  # int() reads Arabic-Indic digits too
    if not 1 <= number <= NOTE_MARKER_MAX:
        return None
    return f"({number})" if found.group(1) else f"[{number}]"


@dataclass(frozen=True)
class StrayNote:
    """A body paragraph that looks like a footnote left in the text: its block id, source page, marker
    and index among the document's top-level nodes."""

    block: str
    page: int
    marker: str
    index: int


def _note_paragraph(node, page: int) -> str | None:
    """The marker of a body paragraph of `page` alone that starts with a note marker (else None)."""
    from editor import document as doc

    if not isinstance(node, dict) or node.get("type") != doc.PARAGRAPH:
        return None
    if doc.attrs_of(node).get("style") in doc.PARAGRAPH_STYLES:
        return None
    if set(doc.source_pages(node)) != {page}:
        return None
    return note_marker(doc.plain_text(node))


def stray_notes(document) -> list[StrayNote]:
    """The body paragraphs that look like footnotes left in the text (see the module docstring), in the
    document's order: for every source page, the run of note-marked paragraphs that ends it (when the
    run is not the whole page), those of at most `NOTE_WORDS_MAX` words."""
    from editor import document as doc

    content = doc.content_of(document)
    by_page: dict[int, list[int]] = {}
    for index in range(doc.preamble_end(content), len(content)):
        for page in sorted(set(doc.source_pages(content[index]))):
            by_page.setdefault(page, []).append(index)
    found: list[StrayNote] = []
    for page, indexes in by_page.items():
        run: list[tuple[int, str]] = []
        for index in reversed(indexes):
            marker = _note_paragraph(content[index], page)
            if marker is None:
                break
            run.append((index, marker))
        if not run or len(run) == len(indexes):
            continue
        for index, marker in run:
            node = content[index]
            if len(doc.plain_text(node).split()) <= NOTE_WORDS_MAX:
                found.append(StrayNote(doc.node_id(node), page, marker, index))
    return sorted(found, key=lambda note: note.index)


def chapter_url(book_id: int, document, index: int) -> str:
    """The book page on the chapter holding the document's top-level node `index` (`?chapter=`)."""
    from editor import document as doc

    for chapter in doc.chapters_of(document):
        if chapter.start <= index < chapter.end:
            return f"{layout_url(book_id)}?{urlencode({'chapter': chapter.id})}"
    return layout_url(book_id)


# ====================================================================== the book


def book_readiness(book: Book, *, manuscript=None, setup=None) -> list[dict]:
    """The «قبل الإخراج» rows of a book (see the module docstring); `manuscript` and `setup` (the page
    setup) when the caller has them. Without a manuscript: no rows (the page shows its empty state)."""
    from assembly.models import AssemblyRun
    from assembly.services import ABANDONED_AFTER
    from editor.models import Manuscript
    from editor.services import review_drift

    from .model import book_model, page_setup
    from .preview import stylesheet_for

    if manuscript is None:
        manuscript = Manuscript.objects.filter(book_id=book.pk).select_related("run").first()
    if manuscript is None:
        return []
    setup = setup if setup is not None else page_setup(stylesheet_for(book))
    document = manuscript.document or {}
    rows: list[dict] = []

    counts = uncertain_counts(book, document)
    if counts.total:
        rows.append(
            row(
                "uncertain_words",
                WARN,
                uncertain_message(counts),
                action(UNCERTAIN_LABEL, book.pk, "uncertain"),
            )
        )

    state = book_pages(book, manuscript)
    review = {"label": REVIEW_LABEL, "url": reverse("review:next", args=[book.pk])}
    if state is not None and state.unreviewed:
        rows.append(row("pages_unreviewed", WARN, unreviewed_message(state.unreviewed), review))
    if state is not None and state.missing:
        fix = review
        if state.missing_in_pipeline:
            fix = {"label": PROCESSING_LABEL, "url": reverse("books:detail", args=[book.pk])}
        rows.append(row("pages_missing", WARN, missing_message(state.missing), fix))
    strays = stray_notes(document)
    if strays:
        message = stray_notes_message(len(strays), strays[0].marker, sorted({note.page for note in strays}))
        show = {"label": SHOW_LABEL, "url": chapter_url(book.pk, document, strays[0].index)}
        rows.append(row("stray_notes", WARN, message, show))
    trust = trust_pages(book, manuscript)
    if trust.missing_text:
        rows.append(row("missing_text", WARN, missing_text_message(trust.missing_text), review))

    drift = review_drift(book, manuscript)
    if drift["pages"]:
        pages = ar_count(len(drift["pages"]), PAGES_OF)
        rows.append(
            row(
                "review_drift", WARN, f"تغيّر نص {pages} في المراجعة بعد التحرير.", action(BOOK_LABEL, book.pk)
            )
        )

    running = AssemblyRun.objects.filter(
        book_id=book.pk,
        status__in=(AssemblyRun.Status.QUEUED, AssemblyRun.Status.RUNNING),
        created_at__gte=timezone.now() - ABANDONED_AFTER,
    ).exists()
    if running:
        rows.append(row("assembly_running", WARN, ASSEMBLY_RUNNING))

    if trust.single_reader:
        rows.append(row("single_reader", INFO, single_reader_message(len(trust.single_reader)), review))
    model = book_model(document, setup, title=book.title, author=book.author)
    if not model.front.author.strip():
        rows.append(row("book_details", INFO, NO_AUTHOR, action(DETAILS_LABEL, book.pk, "format")))
    if setup.copyright_page and not (setup.detail("publisher") or setup.detail("year")):
        rows.append(row("book_details", INFO, NO_IMPRINT, action(DETAILS_LABEL, book.pk, "format")))
    if setup.contents and not model.contents():
        rows.append(row("no_headings", INFO, NO_HEADINGS))

    if not rows:  # never beside a warning (nor a note: «ولا ملاحظات»)
        rows.append(row("clear", SUCCESS, CLEAR))
    return rows


# ====================================================================== helpers for the exporters' notes


def missing_font_rows(book_id: int, setup, code: str = "font_missing") -> list[dict]:
    """A warn row for each face of the stylesheet that is not installed on this Mac (Amiri stands in, as
    in the preview), with the link «الخطوط» (`?tab=format`)."""
    from .fonts import resolve

    fonts = resolve(setup.body_font, setup.latin_font, setup.heading_font)
    out: list[dict] = []
    seen: set[str] = set()
    for item in fonts.missing:
        name = str(item.get("name") or item.get("key") or "")
        if name in seen:
            continue
        seen.add(name)
        used = str(item.get("fallback") or "Amiri")
        fallback = "أميري" if used == "Amiri" else f"«{used}»"
        message = MISSING_FONT.format(name=name, fallback=fallback)
        out.append(row(code, WARN, message, action(FONTS_LABEL, book_id, "format")))
    return out


def layout_checks(book_id: int) -> list[dict]:
    """The page checks of the book's live layout (`publishing.layout.page_checks`; empty before one)."""
    from .relayout import live_of, read_json

    live = live_of(book_id)
    if live is None:
        return []
    data = read_json(live.path) or {}
    return [item for item in data.get("checks") or [] if isinstance(item, dict)]


def page_checks_row(book_id: int, code: str = "page_checks") -> dict | None:
    """«3 ملاحظات على الصفحات» (warn, → `?tab=chapters`) for the live layout's page checks other than a
    missing face (reported on its own), None without any."""
    checks = [item for item in layout_checks(book_id) if item.get("code") != "missing_font"]
    if not checks:
        return None
    return row(
        code, WARN, f"{ar_count(len(checks), CHECKS)} على الصفحات", action("الفصول", book_id, "chapters")
    )


def layout_is_current(book: Book, setup=None, manuscript_version: int | None = None) -> bool:
    """True when the book's live layout shows the current text (its manuscript version) with the current
    page setup and engine: its page numbers are the preview's (Word's contents field is filled from them)."""
    from editor.models import Manuscript

    from .model import page_setup
    from .preview import setup_hash, stylesheet_for
    from .relayout import live_of

    live = live_of(book.pk)
    if live is None:
        return False
    if manuscript_version is None:
        manuscript_version = (
            Manuscript.objects.filter(book_id=book.pk).values_list("version", flat=True).first() or 0
        )
    setup = setup if setup is not None else page_setup(stylesheet_for(book))
    return live.manuscript_version >= manuscript_version and live.setup_hash == setup_hash(setup)
