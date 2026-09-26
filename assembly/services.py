"""Assembly services (PHASE4_SPEC §3): load a book's lines, run the pipeline, save the manuscript and
tell whether it is out of date.

Views and API functions stay thin and call into here; they only enqueue (`start_assembly` and the
override services), the work runs in `assembly.tasks.assemble_book` → `run_assembly`. The pipeline
itself is `assembly.pipeline` (pure functions); `load_book` is its only loader and reads a book in
two queries whatever its page count.

Options and overrides live in `Book.assembly_settings` (D38) and are re-applied by every run.
After a successful run the included pages that were `reviewed` become `assembled` (D36) and the book
render of the new text is asked for (D49). Once the text is edited on the book page (D41) a whole-book
run replaces the edits, so it is refused unless the request confirms it (`replace_edited`, D49).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from django.contrib.auth.models import AnonymousUser
from django.db import transaction
from django.db.models import Count, Max
from django.urls import reverse
from django.utils import timezone

from books.models import Book, Page
from editor.models import Manuscript, ManuscriptSnapshot
from ocr.models import Line
from ocr.services import SKIPPED_KINDS, is_unresolved, line_kind
from processing.models import Region

from . import pipeline
from .models import AssemblyRun
from .pipeline import BookMeta, LineIn, PageIn, Settings, normalize_settings

log = logging.getLogger(__name__)

ACTIVE_STATUSES: tuple[str, ...] = (AssemblyRun.Status.QUEUED, AssemblyRun.Status.RUNNING)
ABANDONED_AFTER = timedelta(minutes=30)  # a queued/running run older than this is taken as lost
SNAPSHOTS_KEPT = 10  # re-assembly snapshots kept per manuscript (manual ones are never pruned)
MAX_ROLE_LINES = 500
# The paragraph menu's «نوع الفقرة» (D74), in its order: the effective choices `set_block_roles` takes and
# the review service stores per line (`review.services.set_line_role`: «حاشية» on a footnote-region line
# stores `body`, «محتوى» there stores `main`). A paragraph that starts with a marker whose call is open on
# its page (`noteFor`) reads the footnote choice as `FOOTNOTE_FOR_LABEL`.
BLOCK_ROLES: tuple[tuple[str, str], ...] = (
    ("body", "محتوى"),
    ("heading", "عنوان رئيسي"),
    ("subheading", "عنوان فرعي"),
    ("verse", "شعر"),
    ("footnote", "حاشية"),
)
FOOTNOTE_FOR_LABEL = "حاشية للعلامة ({n})"
OPTION_KEYS: tuple[str, ...] = ("footnote_numbering", "include_unreviewed", "strip_tatweel")
EMPTY_SIGNATURE = "0:"

# Arabic step labels of the manuscript view (§4.2), in pipeline order.
STAGE_LABELS: dict[str, str] = {
    "collect": "جمع الأسطر",
    "paragraphs": "بناء الفقرات",
    "seams": "وصل الفقرات عبر الصفحات",
    "footnotes": "ربط الحواشي",
    "headings": "بناء العناوين",
    "typography": "ضبط علامات الترقيم والأرقام",
    "save": "حفظ المخطوطة",
}

RUN_ERROR = (
    "تعذّر تجميع المخطوطة؛ بقيت المخطوطة السابقة كما هي. أعد المحاولة، وإن تكرّر الخطأ فراجع سجل الخادم."
)
ABANDONED_ERROR = "توقّف التجميع قبل أن يكتمل؛ أعد المحاولة."
ENQUEUE_ERROR = "تعذّر إرسال التجميع إلى طابور المهام؛ تحقّق من تشغيل Redis وعامل Celery ثم أعد المحاولة."
EDITED_ERROR = (
    "حُرِّر نص هذا الكتاب في صفحة الكتاب، وإعادة تجميعه تستبدل النص المحرَّر بنص الصفحات. أكّد الاستبدال أولًا."
)


class AssemblyError(ValueError):
    """A refused assembly request; the message is Arabic (API 400)."""


class AssemblyNotFound(AssemblyError):
    """A line or page that is not part of the book (API 404)."""


class AssemblyEdited(AssemblyError):
    """A whole-book run asked for on a text edited on the book page, without `replace_edited` (API 409)."""


def _user_or_none(user):
    """The user to store on a row: None for anonymous / missing users."""
    if user is None or isinstance(user, AnonymousUser) or not getattr(user, "is_authenticated", False):
        return None
    return user


# ====================================================================== loader


@dataclass
class LoadedBook:
    """The pipeline's input for a book, plus a content signature per page (for staleness)."""

    pages: list[PageIn]
    signatures: dict[int, str]


def _stamp(value: datetime | None) -> str:
    """A timestamp in one fixed form (UTC, microseconds) so loader and aggregate compare equal."""
    if value is None:
        return ""
    if timezone.is_naive(value):
        value = timezone.make_aware(value, UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")


def page_signature(count: int, last_updated: datetime | None) -> str:
    """Content signature of a page: its number of lines and their latest `updated_at`.

    Any review action changes it: edits and roles save a line (`updated_at`), inserts and deletes
    change the count, undo restores or re-saves lines.
    """
    return f"{count}:{_stamp(last_updated)}" if count else EMPTY_SIGNATURE


def page_signatures(page_ids) -> dict[int, str]:
    """`page_signature` of each page, in one grouped query."""
    ids = list(page_ids)
    if not ids:
        return {}
    rows = (
        Line.objects.filter(page_id__in=ids).values("page_id").annotate(n=Count("id"), last=Max("updated_at"))
    )
    out = dict.fromkeys(ids, EMPTY_SIGNATURE)
    for row in rows:
        out[row["page_id"]] = page_signature(row["n"], row["last"])
    return out


def _ratio_box(bbox, width: int, height: int) -> tuple[float, float, float, float] | None:
    """A `[x0, y0, x1, y1]` gray-image box as ratios of the page (None when unusable)."""
    if not width or not height or not isinstance(bbox, list | tuple) or len(bbox) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return (round(x0 / width, 5), round(y0 / height, 5), round(x1 / width, 5), round(y1 / height, 5))


def line_words(tokens: list | None, text: str) -> tuple[str, list[int]]:
    """The line's text as words joined by single spaces, and the indexes of the unresolved words.

    Tokens are the source (a typed token may hold a short phrase: each of its words is indexed);
    a line without tokens uses its text and has no uncertain words.
    """
    if not tokens:
        return " ".join((text or "").split()), []
    words: list[str] = []
    uncertain: list[int] = []
    for token in tokens:
        if not isinstance(token, dict):
            continue
        parts = str(token.get("t") or "").split()
        if not parts:
            continue
        if is_unresolved(token):
            uncertain.extend(range(len(words), len(words) + len(parts)))
        words.extend(parts)
    return " ".join(words), uncertain


def load_book(book: Book) -> LoadedBook:
    """Read a book for the pipeline in two queries, whatever its page count.

    Pages: every non-excluded page in book order with its preprocess output size. Lines: every line
    of those pages in reading order with its region kind; running headers and page numbers are left
    out (`ocr.services.SKIPPED_KINDS`). A line's kind is its effective kind (`ocr.services.line_kind`,
    D74): a line of a footnote region, or one with the `footnote` role, is a `footnote` line; every
    other line (a `main` role pulls a footnote-region line back, a heading or verse role too; a line
    without a region) is `body`. Its role is the one the pipeline reads (`pipeline_role`). Boxes become
    ratios of the preprocess output size (the gray image the boxes are measured on), falling back to
    the page's size.
    """
    pages = list(
        Page.objects.filter(book_id=book.pk, is_excluded=False)
        .select_related("preprocess")
        .only(
            "id",
            "number",
            "printed_number",
            "status",
            "width",
            "height",
            "preprocess__output_width",
            "preprocess__output_height",
        )
        .order_by("number")
    )
    rows = (
        Line.objects.filter(page__book_id=book.pk, page__is_excluded=False)
        .select_related("region")
        .only("id", "page_id", "order", "role", "text", "bbox", "tokens", "updated_at", "region__kind")
        .order_by("page_id", "order", "id")
    )
    sizes: dict[int, tuple[int, int]] = {}
    items: dict[int, PageIn] = {}
    for page in pages:
        pre = getattr(page, "preprocess", None) if _has_preprocess(page) else None
        width = (pre.output_width if pre is not None else 0) or page.width
        height = (pre.output_height if pre is not None else 0) or page.height
        sizes[page.pk] = (width, height)
        items[page.pk] = PageIn(
            id=page.pk,
            number=page.number,
            printed=page.printed_number or "",
            status=page.status,
            reviewed=page.status in pipeline.REVIEWED_STATUSES,
        )
    counts: dict[int, int] = {}
    latest: dict[int, datetime] = {}
    for line in rows:
        page_in = items.get(line.page_id)
        if page_in is None:
            continue
        counts[line.page_id] = counts.get(line.page_id, 0) + 1
        if line.updated_at is not None and (
            line.page_id not in latest or line.updated_at > latest[line.page_id]
        ):
            latest[line.page_id] = line.updated_at
        kind = line.region.kind if line.region_id and line.region is not None else Region.Kind.BODY
        if kind in SKIPPED_KINDS:
            continue
        text, uncertain = line_words(line.tokens, line.text)
        width, height = sizes[line.page_id]
        page_in.lines.append(
            LineIn(
                id=line.pk,
                order=line.order,
                kind=pipeline.FOOTNOTE if line_kind(line.role, kind) == "footnote" else pipeline.BODY,
                role=pipeline_role(line.role),
                text=text,
                box=_ratio_box(line.bbox, width, height),
                uncertain=uncertain,
            )
        )
    signatures = {pk: page_signature(counts.get(pk, 0), latest.get(pk)) for pk in items}
    return LoadedBook(pages=list(items.values()), signatures=signatures)


def pipeline_role(role: str | None) -> str:
    """A stored line role as the pipeline reads it (`pipeline.LINE_ROLES`): headings and verse as they
    are; `body`, `main` and `footnote` are `body` (the line's kind carries the note, `line_kind`)."""
    return role if role in pipeline.LINE_ROLES else pipeline.ROLE_BODY


def _has_preprocess(page: Page) -> bool:
    """True when the page's (select_related) preprocess row exists."""
    try:
        return page.preprocess is not None
    except Page.preprocess.RelatedObjectDoesNotExist:
        return False


def book_meta(book: Book, run: AssemblyRun | None = None) -> BookMeta:
    """What the document needs from the book (and the run, when there is one)."""
    return BookMeta(
        id=book.pk,
        title=book.title,
        author=book.author,
        digit_style=book.digit_style if book.digit_style in pipeline.DIGIT_STYLES else "western",
        run_id=run.pk if run is not None else None,
        assembled_at=None,
    )


def preview(book: Book) -> pipeline.Result:
    """Run the pipeline in memory on the book as it is now; writes nothing (`assemble --dry-run`)."""
    loaded = load_book(book)
    return pipeline.assemble(loaded.pages, normalize_settings(book.assembly_settings), book_meta(book))


# ====================================================================== starting runs


def _parse_bool(value) -> bool | None:
    """A posted boolean, None when it is not one."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
        "0",
        "false",
        "no",
        "off",
    ):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return None


def clean_options(options) -> dict:
    """The assembly options of a request (`footnote_numbering`, `include_unreviewed`, `strip_tatweel`,
    `strip_running_heads`).

    Missing keys are left out; unknown keys are ignored; a bad value raises `AssemblyError`.
    """
    data = options if isinstance(options, dict) else {}
    out: dict = {}
    numbering = data.get("footnote_numbering")
    if numbering not in (None, ""):
        if numbering not in pipeline.NUMBERING_MODES:
            raise AssemblyError("طريقة ترقيم الحواشي غير معروفة.")
        out["footnote_numbering"] = numbering
    for key in ("include_unreviewed", "strip_tatweel", "strip_running_heads"):
        if data.get(key) is None:
            continue
        value = _parse_bool(data[key])
        if value is None:
            raise AssemblyError("قيمة غير صالحة لأحد خيارات التجميع.")
        out[key] = value
    return out


def _abandoned(run: AssemblyRun) -> bool:
    """True for a queued/running run that has been waiting far longer than any assembly takes."""
    return run.created_at is not None and timezone.now() - run.created_at > ABANDONED_AFTER


def is_chapter_run(run: AssemblyRun) -> bool:
    """True for a run that re-assembles one chapter of an edited manuscript (settings `scope: chapter`)."""
    return isinstance(run.settings, dict) and run.settings.get("scope") == "chapter"


def _queue_run(book: Book, user, mutate=None, changed: bool = False) -> tuple[AssemblyRun, bool]:
    """Apply `mutate(settings)` to the book's assembly settings and find or create the run to report.

    Returns `(run, created)`. A queued run is returned as is (it reads the settings when it
    starts); a running one too, unless this request changed something (settings, or line roles
    when `changed`), in which case a follow-up run is queued. Runs lost for longer than
    `ABANDONED_AFTER` are closed with an error.
    """
    with transaction.atomic():
        locked = Book.objects.select_for_update().get(pk=book.pk)
        before = dict(locked.assembly_settings or {})
        settings = dict(before)
        if mutate is not None:
            mutate(settings)
        if settings != before:
            locked.assembly_settings = settings
            locked.save(update_fields=["assembly_settings"])
            changed = True
        book.assembly_settings = settings
        active = list(AssemblyRun.objects.filter(book_id=book.pk, status__in=ACTIVE_STATUSES).order_by("-id"))
        lost = [run.pk for run in active if _abandoned(run)]
        if lost:
            AssemblyRun.objects.filter(pk__in=lost).update(
                status=AssemblyRun.Status.ERROR, error=ABANDONED_ERROR, finished_at=timezone.now()
            )
        # A chapter re-assembly (D41, `editor.services.reassemble_chapter`) is not this book's
        # assembly: a full run is queued beside it; whichever saves last, the full run's text stays
        # (a chapter run that finds a newer run on the manuscript changes nothing).
        live = [run for run in active if run.pk not in lost and not is_chapter_run(run)]
        queued = next((run for run in live if run.status == AssemblyRun.Status.QUEUED), None)
        if queued is not None:
            return queued, False
        if live and not changed:
            return live[0], False
        run = AssemblyRun.objects.create(
            book_id=book.pk,
            status=AssemblyRun.Status.QUEUED,
            settings=normalize_settings(settings).as_dict(),
            created_by=_user_or_none(user),
        )
    return run, True


def _enqueue(run: AssemblyRun) -> None:
    """Send the run to the default queue (after its row is committed) and remember the task id.

    When the broker cannot be reached the run is closed with an error at once: left queued, it
    would be returned by every new request (idempotent start) until it counted as abandoned.
    """
    from .tasks import assemble_book

    try:
        result = assemble_book.delay(run.pk)
    except Exception as exc:  # noqa: BLE001 - reported on the run (broker down, misconfigured queue)
        log.exception("assembly run %s of book %s could not be enqueued", run.pk, run.book_id)
        AssemblyRun.objects.filter(pk=run.pk, status=AssemblyRun.Status.QUEUED).update(
            status=AssemblyRun.Status.ERROR,
            error=f"{ENQUEUE_ERROR}\n{type(exc).__name__}: {exc}"[:4000],
            finished_at=timezone.now(),
        )
        return
    task_id = getattr(result, "id", "") or ""
    if task_id:
        AssemblyRun.objects.filter(pk=run.pk, task_id="").update(task_id=task_id[:64])


def is_edited(book: Book) -> bool:
    """Whether the book's text was saved from the book page since the last whole-book run (D41)."""
    return Manuscript.objects.filter(book_id=book.pk, origin=Manuscript.Origin.EDITOR).exists()


def check_edited(book: Book, replace_edited: bool = False) -> None:
    """Refuse a whole-book run over an edited text unless the request confirms it (D49)."""
    if not replace_edited and is_edited(book):
        raise AssemblyEdited(EDITED_ERROR)


def _start(book: Book, user, mutate=None, changed: bool = False) -> AssemblyRun:
    run, created = _queue_run(book, user, mutate, changed)
    if created:
        _enqueue(run)
    run.refresh_from_db()
    return run


def start_assembly(book: Book, user, options=None, replace_edited: bool = False) -> AssemblyRun:
    """Merge `options` into `book.assembly_settings` and start a run (or return the active one).

    Idempotent: while a run is queued or running it is returned instead of a new one (a running
    run is followed by a new one only when the options changed). The run is enqueued on the default
    queue; raises `AssemblyError` for a bad option value, `AssemblyEdited` over an edited text.
    """
    changes = clean_options(options)
    check_edited(book, replace_edited)
    return _start(book, user, lambda settings: settings.update(changes))


def set_seam_override(book: Book, user, page, mode: str, replace_edited: bool = False) -> AssemblyRun:
    """Force a join or a split at the boundary before page `page` (`mode="auto"` removes the override).

    Stored in `assembly_settings.seams` and re-applied by every run; starts a run.
    """
    try:
        number = int(str(page).strip())
    except (TypeError, ValueError):
        raise AssemblyError("رقم الصفحة غير صالح.") from None
    if isinstance(page, bool) or number < 1:
        raise AssemblyError("رقم الصفحة غير صالح.")
    if mode not in (*pipeline.SEAM_MODES, "auto"):
        raise AssemblyError("نوع الفاصل غير معروف؛ المتاح: وصل أو فصل أو تلقائي.")
    if not book.pages.filter(number=number, is_excluded=False).exists():
        raise AssemblyNotFound("الصفحة غير موجودة في هذا الكتاب.")
    check_edited(book, replace_edited)

    def mutate(settings: dict) -> None:
        stored = settings.get("seams")
        seams = {str(k): v for k, v in stored.items()} if isinstance(stored, dict) else {}
        if mode == "auto":
            seams.pop(str(number), None)
        else:
            seams[str(number)] = mode
        settings["seams"] = seams

    return _start(book, user, mutate)


def dismiss_suggestion(
    book: Book, user, block_id: str, action: str = "dismiss", replace_edited: bool = False
) -> AssemblyRun:
    """Dismiss the heading suggestion of paragraph `block_id` for good (`dismissed_suggestions`).

    Starts a run.
    """
    if action != "dismiss":
        raise AssemblyError("إجراء غير معروف.")
    if not isinstance(block_id, str) or not re.fullmatch(r"p[0-9]+", block_id):
        raise AssemblyError("معرّف الفقرة غير صالح.")
    check_edited(book, replace_edited)

    def mutate(settings: dict) -> None:
        stored = settings.get("dismissed_suggestions")
        dismissed = [value for value in stored if isinstance(value, str)] if isinstance(stored, list) else []
        if block_id not in dismissed:
            dismissed.append(block_id)
        settings["dismissed_suggestions"] = dismissed

    return _start(book, user, mutate)


def _line_ids(values) -> list[int]:
    """Validated list of line ids (ints or digit strings)."""
    if not isinstance(values, list | tuple) or not values:
        raise AssemblyError("حدّد أسطر الفقرة أولًا.")
    if len(values) > MAX_ROLE_LINES:
        raise AssemblyError("عدد الأسطر أكبر من المسموح.")
    out: list[int] = []
    for value in values:
        if isinstance(value, bool) or not str(value).strip().isdigit():
            raise AssemblyError("معرّف السطر غير صالح.")
        out.append(int(str(value).strip()))
    return list(dict.fromkeys(out))


def set_block_roles(book: Book, user, line_ids, role: str, replace_edited: bool = False) -> AssemblyRun:
    """Set the role of a block's lines through the review service (one revision per changed line), then
    start a run (D38: a heading is a line fact, recorded and undoable like any review action).

    `role` is one of the paragraph menu's choices (`BLOCK_ROLES`, D74): «محتوى», the two headings,
    «شعر» (each line a verse paragraph of its own) and «حاشية» (the lines become a note; with a marker
    whose call is open on the page, the next run links it there). It goes to the review service page by
    page (`review.services.set_roles`: the effective choice, stored per line by its region, one batch per
    page, so review's undo reverts a page's share in one step). The structure tools rest on an edited
    book (D49): over a text edited on the book page the request must confirm the replacement
    (`replace_edited`).

    All or nothing: a line of another book → `AssemblyNotFound`; a line the review service refuses
    (a page not editable) → `AssemblyError` and no line changes.
    """
    from review import services as review_services  # other app: lazy import

    if role not in dict(BLOCK_ROLES):
        raise AssemblyError("نوع السطر غير معروف.")
    ids = _line_ids(line_ids)
    lines = list(Line.objects.filter(pk__in=ids).select_related("page"))
    if len(lines) != len(ids) or any(line.page.book_id != book.pk for line in lines):
        raise AssemblyNotFound("السطر غير موجود في هذا الكتاب.")
    check_edited(book, replace_edited)  # before any line changes
    by_page: dict[int, tuple[Page, list[int]]] = {}
    for line in sorted(lines, key=lambda item: (item.page.number, item.order, item.pk)):
        by_page.setdefault(line.page_id, (line.page, []))[1].append(line.pk)
    try:
        with transaction.atomic():
            for page, page_line_ids in by_page.values():
                review_services.set_roles(page, page_line_ids, role, user)
    except review_services.ReviewError as exc:
        raise AssemblyError(str(exc)) from exc
    return _start(book, user, changed=True)


# ====================================================================== running


def _set_stage(run: AssemblyRun, stage: str) -> None:
    """Record the step being run (committed at once, so the view's polling sees it)."""
    run.stage = stage
    AssemblyRun.objects.filter(pk=run.pk).update(stage=stage)


def run_assembly(run_id: int) -> AssemblyRun | None:
    """Run a queued assembly: load, pipeline, then save everything in one transaction.

    The save snapshots the previous document (reason `reassembly`, keeping the newest
    `SNAPSHOTS_KEPT`), writes the manuscript (`origin="assembly"`, version + 1), moves the included
    `reviewed` pages whose lines did not change during the run to `assembled` and re-derives the
    book status. A run older than the one that saved the manuscript does not overwrite it. Any
    exception → `status=error` with an Arabic headline; the old manuscript stays. A run that is not
    queued any more (a duplicate delivery) is left alone. Returns the run (None when it is gone).
    """
    run = AssemblyRun.objects.select_related("book").filter(pk=run_id).first()
    if run is None or run.status != AssemblyRun.Status.QUEUED:
        return run
    started = time.monotonic()
    # Claim the run in one conditional update: of two deliveries of the task (acks_late, a retry)
    # only one moves it from queued to running; the other leaves it alone.
    claimed = AssemblyRun.objects.filter(pk=run.pk, status=AssemblyRun.Status.QUEUED).update(
        status=AssemblyRun.Status.RUNNING, stage="collect"
    )
    if not claimed:
        run.refresh_from_db()
        return run
    run.status = AssemblyRun.Status.RUNNING
    run.stage = "collect"
    try:
        book = Book.objects.get(pk=run.book_id)
        options = normalize_settings(book.assembly_settings)
        loaded = load_book(book)
        result = pipeline.assemble(
            loaded.pages, options, book_meta(book, run), lambda key: _set_stage(run, key)
        )
        _set_stage(run, "save")
        _save(run, book, options, loaded, result, started)
        _render_pages(run, book)
    except Exception as exc:  # noqa: BLE001 - reported on the run, the old manuscript stays
        log.exception("assembly run %s of book %s failed", run.pk, run.book_id)
        AssemblyRun.objects.filter(pk=run.pk).update(
            status=AssemblyRun.Status.ERROR,
            error=f"{RUN_ERROR}\n{type(exc).__name__}: {exc}"[:4000],
            finished_at=timezone.now(),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    run.refresh_from_db()
    return run


def _render_pages(run: AssemblyRun, book: Book) -> None:
    """The book render of the text this run wrote, asked for at once (D49): the book page opens on the
    new pages (or on their render), and a render left over from the old text is cancelled. Nothing when a
    newer run had already saved; a failure to ask never fails the run."""
    from publishing import engine  # other app: lazy import

    if not Manuscript.objects.filter(book_id=book.pk, run_id=run.pk).exists():
        return
    try:
        engine.render_after_assembly(book)
    except Exception:  # noqa: BLE001 - the run is saved; the book page asks again when it opens
        log.warning("could not ask for the pages of book %s after run %s", book.pk, run.pk, exc_info=True)


def _save(
    run: AssemblyRun,
    book: Book,
    options: Settings,
    loaded: LoadedBook,
    result: pipeline.Result,
    started: float,
) -> None:
    """The save step of `run_assembly` (one transaction)."""
    included_ids = set(result.pages)
    pages = [page for page in loaded.pages if page.id in included_ids]
    included = {
        str(page.id): {
            "number": page.number,
            "reviewed": page.reviewed,
            "sig": loaded.signatures.get(page.id, EMPTY_SIGNATURE),
        }
        for page in pages
    }
    with transaction.atomic():
        Book.objects.select_for_update().filter(pk=book.pk).first()  # one save per book at a time
        manuscript = Manuscript.objects.select_for_update().filter(book_id=book.pk).first()
        finished = timezone.now()
        superseded = manuscript is not None and manuscript.run_id is not None and manuscript.run_id > run.pk
        if not superseded:
            document = result.document
            document["attrs"]["assembledAt"] = finished.isoformat()
            if manuscript is None:
                Manuscript.objects.create(
                    book_id=book.pk,
                    document=document,
                    version=1,
                    origin=Manuscript.Origin.ASSEMBLY,
                    run=run,
                    updated_by=run.created_by,
                )
            else:
                edited = manuscript.origin == Manuscript.Origin.EDITOR
                ManuscriptSnapshot.objects.create(
                    manuscript=manuscript,
                    document=manuscript.document,
                    version=manuscript.version,
                    label=(
                        f"النص المحرَّر قبل إعادة التجميع · الإصدار {manuscript.version}"
                        if edited
                        else f"قبل إعادة التجميع · الإصدار {manuscript.version}"
                    ),
                    # an edited text (D41) is kept for good: re-assembly snapshots are pruned
                    reason=ManuscriptSnapshot.Reason.MANUAL
                    if edited
                    else ManuscriptSnapshot.Reason.REASSEMBLY,
                    created_by=run.created_by,
                )
                prune_snapshots(manuscript)
                manuscript.document = document
                manuscript.version += 1
                manuscript.origin = Manuscript.Origin.ASSEMBLY
                manuscript.run = run
                manuscript.updated_by = run.created_by
                manuscript.save()
            reviewed = [page.id for page in pages if page.status == Page.Status.REVIEWED]
            now = page_signatures(reviewed)
            unchanged = [pk for pk in reviewed if now.get(pk) == loaded.signatures.get(pk)]
            if unchanged:
                Page.objects.filter(pk__in=unchanged, status=Page.Status.REVIEWED).update(
                    status=Page.Status.ASSEMBLED
                )
            Book.objects.get(pk=book.pk).refresh_status()
        run.status = AssemblyRun.Status.DONE
        run.stage = "save"
        run.settings = options.as_dict()
        run.warnings = result.warnings
        run.stats = result.stats
        run.included = included
        run.error = ""
        run.finished_at = finished
        run.duration_ms = int((time.monotonic() - started) * 1000)
        run.save(
            update_fields=[
                "status",
                "stage",
                "settings",
                "warnings",
                "stats",
                "included",
                "error",
                "finished_at",
                "duration_ms",
            ]
        )


def prune_snapshots(manuscript: Manuscript, keep: int = SNAPSHOTS_KEPT) -> int:
    """Delete re-assembly snapshots beyond the newest `keep`; returns how many went."""
    old = list(
        manuscript.snapshots.filter(reason=ManuscriptSnapshot.Reason.REASSEMBLY)
        .order_by("-created_at", "-id")
        .values_list("id", flat=True)[keep:]
    )
    if old:
        ManuscriptSnapshot.objects.filter(pk__in=old).delete()
    return len(old)


# ====================================================================== read models


def eligible_statuses(options: Settings) -> frozenset[str]:
    """Page statuses a run would include with these options."""
    if options.include_unreviewed:
        return pipeline.REVIEWED_STATUSES | {pipeline.UNREVIEWED_STATUS}
    return pipeline.REVIEWED_STATUSES


def page_rows(book: Book) -> list[tuple[int, int, str]]:
    """`(id, number, status)` of the book's non-excluded pages (one query)."""
    return list(book.pages.filter(is_excluded=False).values_list("id", "number", "status"))


def page_changes(included: dict, rows, options: Settings) -> tuple[list[int], list[int]]:
    """`(stale, drift)`: the numbers of the pages changed since the run that built the manuscript, two ways.

    - **stale**: the text or the status changed. A page is stale when it became eligible or stopped being
      eligible, when its reviewed flag changed, or when its content signature differs (a line edited,
      inserted, deleted or re-roled after the run read it). For an unedited manuscript a re-assembly is
      lossless and is what brings an approval into the text (D36), so an approval counts here.
    - **drift** (D70): the same minus the pages whose *only* change is the reviewed flag (eligible before and
      now, same signature). Approving a page whose lines did not change is not "text changed in review
      after the edit"; the manuscript's amber mark follows the page's live status instead (`render`).

    One query for the signatures (of every page the run read that is still eligible).
    """
    allowed = eligible_statuses(options)
    eligible = {pk: (number, status) for pk, number, status in rows if status in allowed}
    known: dict[int, dict] = {}
    for key, info in (included or {}).items():
        if str(key).isdigit() and isinstance(info, dict):
            known[int(key)] = info
    stale: set[int] = set()
    drift: set[int] = set()
    common: list[int] = []
    for pk, (number, status) in eligible.items():
        info = known.get(pk)
        if info is None:
            stale.add(number)
            drift.add(number)
            continue
        if bool(info.get("reviewed")) != (status in pipeline.REVIEWED_STATUSES):
            stale.add(number)
        common.append(pk)
    for pk, info in known.items():
        if pk not in eligible and isinstance(info.get("number"), int):
            stale.add(info["number"])
            drift.add(info["number"])
    for pk, signature in page_signatures(common).items():
        if signature != known[pk].get("sig"):
            stale.add(eligible[pk][0])
            drift.add(eligible[pk][0])
    return sorted(stale), sorted(drift)


def stale_pages(included: dict, rows, options: Settings) -> list[int]:
    """Numbers of the pages whose text or status changed since the run that built the manuscript
    (`page_changes`' first list): what re-assembly would bring in. One query for the signatures."""
    return page_changes(included, rows, options)[0]


def drift_pages(included: dict, rows, options: Settings) -> list[int]:
    """`stale_pages` minus the approval-only pages (D70, `page_changes`' second list): the review drift of
    an edited manuscript. One query for the signatures."""
    return page_changes(included, rows, options)[1]


def _run_info(run: AssemblyRun | None) -> dict | None:
    if run is None:
        return None
    error = (run.error or "").splitlines()
    return {
        "id": run.pk,
        "status": run.status,
        "stage": run.stage,
        "error": error[0] if error and run.status == AssemblyRun.Status.ERROR else "",
    }


def assembly_options(options: Settings) -> dict:
    """The options of the convert popover as stored for the book."""
    return {
        "footnote_numbering": options.footnote_numbering,
        "include_unreviewed": options.include_unreviewed,
        "strip_tatweel": options.strip_tatweel,
        "strip_running_heads": options.strip_running_heads,
    }


def manuscript_state(book: Book, rows: list[tuple[int, int, str]] | None = None) -> dict:
    """The compact manuscript state of the dashboard and the manuscript view (§3).

    `{exists, version, assembled_at, run: {id, status, stage, error} | None, stale, stale_pages,
    drift_pages, warnings_count, stats}` (`drift_pages`: the stale pages minus the approval-only ones, D70,
    `page_changes`) plus `active` (a run is queued or running), `options` (the stored
    assembly options), `unreviewed_pages` (pages OCR'd and not reviewed yet) and `edited` (the text
    was saved from the book page since the last whole-book run: D41, D49). `rows` are the
    book's `(id, number, status)` page rows when the caller already has them. A book never assembled
    costs one query (two without `rows`); an assembled one two more (the manuscript with its run, the
    page signatures). The document itself is never loaded here.
    """
    options = normalize_settings(book.assembly_settings)
    latest = (
        AssemblyRun.objects.filter(book_id=book.pk)
        .only("id", "book_id", "status", "stage", "error", "created_at")
        .order_by("-created_at", "-id")
        .first()
    )
    state = {
        "exists": False,
        "version": 0,
        "assembled_at": None,
        "run": _run_info(latest),
        "active": latest is not None and latest.is_active,
        "stale": False,
        "stale_pages": [],
        "drift_pages": [],
        "warnings_count": 0,
        "stats": {},
        "options": assembly_options(options),
        "unreviewed_pages": 0,
        "edited": False,
    }
    if latest is None and rows is None:
        # Manuscripts are only written by runs: never assembled, so only the pending count is needed.
        state["unreviewed_pages"] = book.pages.filter(is_excluded=False, status=Page.Status.OCR_DONE).count()
        return state
    rows = rows if rows is not None else page_rows(book)
    state["unreviewed_pages"] = sum(1 for _pk, _number, status in rows if status == Page.Status.OCR_DONE)
    if latest is None:
        return state
    manuscript = (
        Manuscript.objects.filter(book_id=book.pk)
        .select_related("run")
        .only(
            "id",
            "book_id",
            "version",
            "origin",
            "updated_at",
            "run",
            "run__finished_at",
            "run__included",
            "run__warnings",
            "run__stats",
        )
        .first()
    )
    if manuscript is None:
        return state
    run = manuscript.run
    assembled = run.finished_at if run is not None and run.finished_at else manuscript.updated_at
    state.update(
        exists=True,
        version=manuscript.version,
        assembled_at=assembled.isoformat() if assembled else None,
        warnings_count=len(run.warnings or []) if run is not None else 0,
        stats=dict(run.stats or {}) if run is not None else {},
        edited=manuscript.origin == Manuscript.Origin.EDITOR,
    )
    if run is not None:
        pages, drift = page_changes(run.included, rows, options)
        state.update(stale=bool(pages), stale_pages=pages, drift_pages=drift)
    return state


def manuscript_urls(book: Book) -> dict:
    """URLs of the manuscript for the dashboard and the manuscript view."""
    return {
        "assemble": reverse("api:book_assemble", args=[book.pk]),
        "state": reverse("api:manuscript_state", args=[book.pk]),
        "data": reverse("api:manuscript", args=[book.pk]),
        "seams": reverse("api:manuscript_seam", args=[book.pk]),
        "roles": reverse("api:manuscript_roles", args=[book.pk]),
        "suggestions": reverse("api:manuscript_suggestion", args=[book.pk]),
        "page": reverse("assembly:manuscript", args=[book.pk]),
        "book": reverse("editor:layout", args=[book.pk]),
        "document": reverse("assembly:document", args=[book.pk]),
    }


def run_payload(run: AssemblyRun) -> dict:
    """The 202 answer of the endpoints that start a run."""
    return {
        "run_id": run.pk,
        "status": run.status,
        "stage": run.stage,
        "manuscript_url": reverse("assembly:manuscript", args=[run.book_id]),
        "state_url": reverse("api:manuscript_state", args=[run.book_id]),
    }


def live_reviewed(book: Book) -> dict[int, bool]:
    """Page number → whether the page is reviewed now, for the book's non-excluded pages (one query): the
    manuscript's amber mark follows it (D70), so a page approved after assembly loses the mark at once."""
    rows = book.pages.filter(is_excluded=False).values_list("number", "status")
    return {number: status in pipeline.REVIEWED_STATUSES for number, status in rows}


def manuscript_payload(book: Book) -> dict | None:
    """`{document, warnings, stats, seams, version, reviewed}` of the book's manuscript (None before the first
    run). `reviewed` is `live_reviewed` (page number → reviewed now) for the amber mark. Two queries."""
    manuscript = Manuscript.objects.filter(book_id=book.pk).select_related("run").first()
    if manuscript is None:
        return None
    document = manuscript.document or {}
    run = manuscript.run
    return {
        "document": document,
        "warnings": list(run.warnings or []) if run is not None else [],
        "stats": dict(run.stats or {}) if run is not None else {},
        "seams": list((document.get("attrs") or {}).get("seams") or []),
        "version": manuscript.version,
        "reviewed": live_reviewed(book),
    }
