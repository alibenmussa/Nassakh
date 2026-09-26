"""Readiness (PHASE6_SPEC §7, D59): what is left to do before exporting, as warnings with links. It never
blocks an export.

Every row is a ready-made `{code, level, message, action?: {label, url}}` object (level `warn`, `info` or
`success`); the export page renders them without knowing any codes. Counts are Arabic phrases with
Western digits (`assembly.render.ar_count`).

**About the book** (`book_readiness`, the same for every format):

- `uncertain_words` (warn): the uncertain words left in the text, and how many of them are numbers Kraken
  read (`editor.uncertain.counts`) → «غير المؤكَّدة» (`?tab=uncertain`);
- `review_drift` (warn): pages whose text changed in review after the manuscript was built
  (`editor.services.review_drift`) → «الكتاب»;
- `assembly_running` (warn): an assembly of the book is queued or running;
- `book_details` (info): no author for the title page and the file's properties; a copyright page with
  neither publisher nor year → «بيانات الكتاب» (`?tab=format`);
- `no_headings` (info): a contents page but no chapter headings;
- `clear` (success): none of the above.

**Helpers the exporters use for their notes** (`Exporter.notes`): `missing_font_rows` (a face of the
stylesheet not installed: Amiri stands in), `page_checks_row` (the live layout's page checks, 6b),
`layout_is_current` (the live pages are those of the current text and setup: Word's contents numbers
come from them), `uncertain_counts` (cached on the book instance, so the page payload counts once) and
`layout_url` (the book page on a panel tab).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse
from django.utils import timezone

from assembly.render import ar_count
from books.models import Book

WARN = "warn"
INFO = "info"
SUCCESS = "success"

WORDS = ("كلمة واحدة", "كلمتان", "كلمات", "كلمة")
NUMBERS = ("رقم واحد", "رقمان", "أرقام", "رقمًا")
PAGES_OF = ("صفحة واحدة", "صفحتين", "صفحات", "صفحة")  # after «نص»: the genitive
CHECKS = ("ملاحظة واحدة", "ملاحظتان", "ملاحظات", "ملاحظة")

ASSEMBLY_RUNNING = "يجري تجميع الكتاب الآن؛ يُخرَج النص كما هو عند بدء الإخراج."
NO_AUTHOR = "بيانات الكتاب بلا مؤلف؛ يُكتب في صفحة العنوان وخصائص الملف."
NO_IMPRINT = "صفحة الحقوق بلا ناشر ولا سنة."
NO_HEADINGS = "لا عناوين فصول في الكتاب؛ ستخلو المحتويات."
CLEAR = "لا ملاحظات؛ الكتاب جاهز للإخراج."
MISSING_FONT = "الخط «{name}» غير مثبّت على هذا الجهاز؛ يُستعمل {fallback} بدلًا منه، كما في المعاينة."

UNCERTAIN_LABEL = "غير المؤكَّدة"
BOOK_LABEL = "الكتاب"
DETAILS_LABEL = "بيانات الكتاب"
FONTS_LABEL = "الخطوط"


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

    model = book_model(document, setup, title=book.title, author=book.author)
    if not model.front.author.strip():
        rows.append(row("book_details", INFO, NO_AUTHOR, action(DETAILS_LABEL, book.pk, "format")))
    if setup.copyright_page and not (setup.detail("publisher") or setup.detail("year")):
        rows.append(row("book_details", INFO, NO_IMPRINT, action(DETAILS_LABEL, book.pk, "format")))
    if setup.contents and not model.contents():
        rows.append(row("no_headings", INFO, NO_HEADINGS))

    if not rows:
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
