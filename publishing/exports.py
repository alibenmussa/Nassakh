"""Exports (PHASE6_SPEC §6.2, D58): the book as a file, one `publishing.Export` row per file.

**Starting.** `request_export(book, format, options, user)` checks the format is available and the book
has a manuscript, normalises the options through the exporter's `OptionSpec`s, closes an abandoned
active row, refuses a second active export of the same book and format (`ExportConflict`, 409 with that
row) and queues a new row; after the transaction the task `publishing.tasks.run_export` is sent to the
`export` queue (an enqueue failure leaves the row in `error` with `ENQUEUE_ERROR`).

**Running.** `run_export(export_id, task_id)` is the task's job and never raises for an export failure:

1. *claim* the row (a missing or final row is left as it is; a row running under another task id is
   left to it; the same task id — an `acks_late` redelivery — restarts; an abandoned row, see
   `abandoned`, is closed as `ABANDONED`);
2. *read the inputs once* (`read_inputs`): the manuscript and its version, the stylesheet, the chapter
   versions, the title, author and digit style, recorded in `inputs` (an edit made during the export
   never gets into the file, and the row then reads stale «تغيّر النص بعد هذا الإخراج»);
3. *export* through `publishing.exporters.get_exporter(format).export(job, Reporter(id))`:
   `ExportCancelled` / `RenderCancelled` leave the row cancelled with no file; `SoftTimeLimitExceeded`
   is `TIMEOUT`; `InvalidExport` is `INVALID_FILE`; any other exception is logged and the row gets
   `EXPORT_ERROR` plus the technical `Type: message` line;
4. *write the file* at `books/<id>/exports/<pk>.<ext>`, then mark the row done only if it still runs
   (a cancel during the write deletes the file, and so does any failure between the write and the
   row's update: a file is never left without its row);
5. *prune*: the newest `EXPORTS_KEPT` (5) done rows and `FAILED_KEPT` (3) failed or cancelled rows per
   book and format are kept; older rows go together with their files (a row whose file cannot be
   deleted stays, and the next prune tries again); a file of `books/<id>/exports/` that no row records
   (a worker killed between the write and the update) is swept away.

**Abandoned exports** (`abandoned`): a queued row older than the soft limit plus 5 minutes (its task was
lost before it started); a running row whose heartbeat stopped — `updated_at`, which every progress
report and at least one write a minute (`Reporter.cancelled`) move, older than 10 minutes: a dead worker
— or that started longer than the soft limit plus 5 minutes ago.

**Progress.** `Reporter(export_id)` writes `progress` at most every 0.5 s (always when the step changes)
on a row that still runs; an update that touches no row means the export was cancelled, and
`cancelled()` also re-reads the status at most once a second (and moves `updated_at` once a minute).

**Reading.** `export_payload(row, user)` is a row of the API (§3.2), `page_payload(book, user)` the export
page's object (a bounded number of queries), `stale_reasons(row, current)` why a finished file is older
than the book (`text`, `format`, `renderer`), `wait_for(row, seconds, since)` the long poll. The file is
named after the title it prints (`printed_title`: «بيانات الكتاب», else the manuscript's title, else the
book's).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from books.models import Book

from .exporters import (
    FORMATS,
    STEPS,
    ExportCancelled,
    ExportJob,
    ExportResult,
    InvalidExport,
    OptionsError,
    defaults,
    get_exporter,
    options_text,
    parse_options,
)
from .models import Export, export_path

log = logging.getLogger(__name__)

EXPORT_ERROR = "تعذّر إخراج الملف. أعد المحاولة، وإن تكرّر الخطأ فراجع سجل الخادم."
ENQUEUE_ERROR = "تعذّر إرسال الإخراج إلى طابور المهام؛ تحقّق من تشغيل Redis وعامل المهام ثم أعد المحاولة."
ABANDONED = "توقّف الإخراج قبل أن يكتمل؛ أعد المحاولة."
CANCELLED_BY_USER = "أُلغي الإخراج."
TIMEOUT = "استغرق الإخراج أطول من المسموح."
INVALID_FILE = "الملف الناتج غير سليم فلم يُسلَّم؛ التفاصيل في سجل الخادم."
NO_MANUSCRIPT = "لا توجد مخطوطة بعد."
UNKNOWN_FORMAT = "صيغة الإخراج غير معروفة."
NOT_AVAILABLE = "هذه الصيغة غير متاحة بعد."
BAD_OPTIONS = "خيارات الإخراج غير صالحة."
BUSY = "هذا الملف يُخرَج الآن؛ انتظر انتهاءه أو ألغِه."
FINISHED = "انتهى هذا الإخراج."
NOT_READY = "الملف غير جاهز."
WAITING_HINT = "لم يبدأ الإخراج بعد؛ تأكّد من تشغيل عامل المهام بعد آخر تحديث (make worker)."

STALE_TEXTS: dict[str, str] = {
    "text": "تغيّر النص بعد هذا الإخراج",
    "format": "تغيّر التنسيق بعد هذا الإخراج",
    "renderer": "تحدّث نسّاخ بعد هذا الإخراج",
}
# the file name after the title, per format (§3.1); Word with comments adds «- مع التعليقات»
FILE_SUFFIXES: dict[str, str] = {"docx": "", "print_pdf": " - للطباعة", "screen_pdf": "", "epub": ""}
COMMENTS_SUFFIX = " - مع التعليقات"
TITLE_MAX = 100

FAILED_KEPT = 3
ITEMS_LISTED = 30
WAITING_AFTER = timedelta(seconds=60)
ABANDONED_MARGIN = timedelta(minutes=5)
HEARTBEAT_LOST = timedelta(minutes=10)  # a running row not updated for this long has lost its worker
HEARTBEAT_EVERY_S = 60.0  # `Reporter.cancelled` moves `updated_at` at least this often
REPORT_EVERY_S = 0.5
CHECK_EVERY_S = 1.0
MAX_WAIT_S = 5.0
WAIT_STEP_S = 0.25  # the long poll reads the status and `updated_at` this often (the row once, at the end)
ERROR_MAX = 4000

ACTIVE = (Export.Status.QUEUED, Export.Status.RUNNING)


def _setting(name: str, default):
    return settings.NASSAKH.get(name, default)


def soft_limit_s() -> int:
    """The export task's soft time limit (`NASSAKH["EXPORT_SOFT_LIMIT_S"]`, 1800 s)."""
    return int(_setting("EXPORT_SOFT_LIMIT_S", 1800))


def exports_kept() -> int:
    """Finished files kept per book and format (`NASSAKH["EXPORTS_KEPT"]`, 5)."""
    return max(1, int(_setting("EXPORTS_KEPT", 5)))


# ====================================================================== errors


class ExportError(Exception):
    """A refused request: `detail` (Arabic), the HTTP `status`, per-key `errors` and the `row` concerned."""

    status = 400

    def __init__(self, detail: str, *, errors: dict | None = None, row: Export | None = None):
        super().__init__(detail)
        self.detail = detail
        self.errors = errors or {}
        self.row = row


class ExportInvalid(ExportError):
    """400: an unknown or unavailable format, or bad options (`errors`)."""


class ExportNotFound(ExportError):
    """404: no manuscript yet, or no such export."""

    status = 404


class ExportConflict(ExportError):
    """409: an export of this book and format is queued or running (`row`)."""

    status = 409


class ExportFinished(ExportError):
    """409: cancelling an export that is already done or failed (`row`)."""

    status = 409


# ====================================================================== names, hashes, states


_UNSAFE = re.compile(r'[/\\:*?"<>|‎‏‪-‮⁦-⁩]')
_SPACES = re.compile(r"\s+")


def clean_title(title: str, book_id: int) -> str:
    """The book title as a file name: control characters, `/ \\ : * ? " < > |` and the bidi controls
    (U+200E/F, U+202A–202E, U+2066–2069) removed, spaces collapsed, dots and spaces trimmed at the ends,
    at most 100 code points; `كتاب-<id>` when nothing is left."""
    text = "".join(" " if unicodedata.category(char) == "Cc" else char for char in str(title or ""))
    text = _UNSAFE.sub("", text)
    text = _SPACES.sub(" ", text).strip(" .")
    text = text[:TITLE_MAX].strip(" .")
    return text or f"كتاب-{book_id}"


def file_name(title: str, format: str, options: dict | None, book_id: int) -> str:
    """The file name of an export: `{title}.docx`, `{title} - مع التعليقات.docx`, `{title} - للطباعة.pdf`,
    `{title}.pdf` (screen) or `{title}.epub`."""
    info = FORMATS[format]
    suffix = FILE_SUFFIXES.get(format, "")
    if format == "docx" and (options or {}).get("comments"):
        suffix += COMMENTS_SUFFIX
    return f"{clean_title(title, book_id)}{suffix}{info.extension}"


def printed_title(document, setup, fallback: str) -> str:
    """The title the file prints, in the book model's order (`publishing.model.book_model`): the book
    details' title («بيانات الكتاب»), else the manuscript's title node (its text, else its `text` attr),
    else `fallback` (the book's own title). Of `document` only the leading title node is read."""
    from editor import document as doc

    detail = setup.detail("title") if setup is not None else ""
    if detail:
        return detail
    content = doc.content_of(document)
    for node in content[: doc.preamble_end(content)]:
        text = doc.plain_text(node) or str(doc.attrs_of(node).get("text") or "").strip()
        return text or fallback
    return fallback


def export_filename(row: Export) -> str:
    """The download name of an export: the one recorded when it ran (the printed title's), else one
    from the book's title."""
    if row.filename:
        return row.filename
    title = (row.inputs or {}).get("title") or row.book.title
    return file_name(title, row.format, row.options, row.book_id)


def stylesheet_hash(setup) -> str:
    """24 hex digits of the page setup and its faces' files, without the engine version (a WeasyPrint
    update does not show Word files as «تغيّر التنسيق»)."""
    from .fonts import resolve

    fonts = resolve(setup.body_font, setup.latin_font, setup.heading_font)
    payload = {"setup": setup.as_dict(), "fonts": fonts.fingerprint()}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def abandoned(row: Export, now: datetime | None = None) -> bool:
    """True for an export whose task is lost (see the module docstring): a queued row created longer
    than the soft limit plus 5 minutes ago; a running row whose heartbeat (`updated_at`) is older than
    `HEARTBEAT_LOST` (a dead worker: `acks_late` without `reject_on_worker_lost` never runs it again),
    or that started longer than the soft limit plus 5 minutes ago. A healthy export that waited in the
    queue is judged from its start, not from its creation."""
    if row.status not in ACTIVE:
        return False
    now = now or timezone.now()
    limit = timedelta(seconds=soft_limit_s()) + ABANDONED_MARGIN
    if row.status == Export.Status.QUEUED:
        return row.created_at is not None and now - row.created_at > limit
    beat = row.updated_at or row.started_at or row.created_at
    if beat is not None and now - beat > HEARTBEAT_LOST:
        return True
    started = row.started_at or row.created_at
    return started is not None and now - started > limit


def _headline(error: str) -> tuple[str, str]:
    lines = (error or "").split("\n", 1)
    return lines[0], (lines[1] if len(lines) > 1 else "")


def _error_text(headline: str, exc: BaseException | None) -> str:
    if exc is None:
        return headline
    return f"{headline}\n{type(exc).__name__}: {exc}"[:ERROR_MAX]


@dataclass(frozen=True)
class Current:
    """What a finished export is compared with (`stale_reasons`): the manuscript version now, the page
    setup's hash now, each available format's renderer version, and the preview's page count."""

    manuscript_version: int | None
    stylesheet_hash: str
    renderers: dict[str, str]
    pages_hint: int | None = None


def current_state(book: Book, *, manuscript_version: int | None = None, setup=None) -> Current:
    """`Current` of a book now (two queries when nothing is given: the manuscript version, the live
    layout; the stylesheet when `setup` is not given)."""
    from editor.models import Manuscript

    from .model import page_setup
    from .preview import stylesheet_for
    from .relayout import live_of

    if manuscript_version is None:
        manuscript_version = (
            Manuscript.objects.filter(book_id=book.pk).values_list("version", flat=True).first()
        )
    setup = setup if setup is not None else page_setup(stylesheet_for(book))
    renderers = {}
    for key in FORMATS:
        exporter = get_exporter(key)
        if exporter is not None:
            renderers[key] = exporter.version
    live = live_of(book.pk)
    return Current(
        manuscript_version=manuscript_version,
        stylesheet_hash=stylesheet_hash(setup),
        renderers=renderers,
        pages_hint=live.page_count if live is not None else None,
    )


def stale_reasons(row: Export, current: Current) -> list[str]:
    """Why a finished export is older than the book: `text` (another manuscript version), `format`
    (another page setup or face files) and `renderer` (another exporter version). Only done rows."""
    if row.status != Export.Status.DONE:
        return []
    out = []
    if (
        row.manuscript_version is not None
        and current.manuscript_version is not None
        and row.manuscript_version != current.manuscript_version
    ):
        out.append("text")
    if row.stylesheet_hash and row.stylesheet_hash != current.stylesheet_hash:
        out.append("format")
    renderer = current.renderers.get(row.format)
    if row.renderer and renderer and row.renderer != renderer:
        out.append("renderer")
    return out


# ====================================================================== starting


def _user(user):
    return user if getattr(user, "is_authenticated", False) else None


def request_export(book: Book, format: str, options: dict | None, user=None) -> Export:
    """Queue an export of the book (see the module docstring). Raises `ExportInvalid` (400),
    `ExportNotFound` (404) or `ExportConflict` (409, with the active row)."""
    from editor.models import Manuscript

    from .model import page_setup
    from .preview import stylesheet_for

    if format not in FORMATS:
        raise ExportInvalid(UNKNOWN_FORMAT)
    exporter = get_exporter(format)
    if exporter is None:
        raise ExportInvalid(NOT_AVAILABLE)
    try:
        with transaction.atomic():
            # one request per book at a time; FOR NO KEY UPDATE, so the deferred foreign-key check of a
            # concurrent insert (FOR KEY SHARE on the book) never deadlocks with this lock (PostgreSQL)
            Book.objects.select_for_update(no_key=True).filter(pk=book.pk).first()
            head = (
                Manuscript.objects.filter(book_id=book.pk).values_list("id", "document__content__0").first()
            )  # the manuscript's first node only (its title node names the file), not the whole document
            if head is None:
                raise ExportNotFound(NO_MANUSCRIPT)
            try:
                values = parse_options(exporter.options, options)
            except OptionsError as exc:
                raise ExportInvalid(BAD_OPTIONS, errors=exc.errors) from None
            now = timezone.now()
            for active in Export.objects.select_for_update().filter(
                book_id=book.pk, format=format, status__in=ACTIVE
            ):
                if abandoned(active, now):
                    _close_abandoned(active, now)
                else:
                    raise ExportConflict(BUSY, row=active)
            first = head[1] if isinstance(head[1], dict) else None
            setup = page_setup(stylesheet_for(book))
            title = printed_title({"content": [first] if first else []}, setup, book.title)
            row = Export.objects.create(
                book=book,
                format=format,
                status=Export.Status.QUEUED,
                options=values,
                renderer=exporter.version[:64],
                filename=file_name(title, format, values, book.pk),
                progress={"step": "queued", "done": None, "total": None},
                created_by=_user(user),
            )
    except IntegrityError:  # another request won the race (the partial unique constraint)
        active = Export.objects.filter(book_id=book.pk, format=format, status__in=ACTIVE).first()
        raise ExportConflict(BUSY, row=active) from None
    _enqueue(row)
    row.refresh_from_db()
    return row


def _close_abandoned(row: Export, now: datetime) -> None:
    Export.objects.filter(pk=row.pk, status__in=ACTIVE).update(
        status=Export.Status.ERROR, error=ABANDONED, finished_at=now, updated_at=now
    )


def _enqueue(row: Export) -> None:
    """Send the row's export to the `export` queue and remember the task id (`preview._enqueue`)."""
    from . import tasks

    try:
        result = tasks.run_export.delay(row.pk)
    except Exception as exc:  # noqa: BLE001 - reported on the row
        log.exception("export %s of book %s could not be enqueued", row.pk, row.book_id)
        now = timezone.now()
        Export.objects.filter(pk=row.pk, status=Export.Status.QUEUED).update(
            status=Export.Status.ERROR, error=_error_text(ENQUEUE_ERROR, exc), finished_at=now, updated_at=now
        )
        return
    task_id = str(getattr(result, "id", "") or "")
    if task_id:
        Export.objects.filter(pk=row.pk, task_id="").update(task_id=task_id[:64])


# ====================================================================== running


class Reporter:
    """The `Progress` of a running export (see the module docstring)."""

    def __init__(
        self,
        export_id: int,
        *,
        every_s: float = REPORT_EVERY_S,
        check_every_s: float = CHECK_EVERY_S,
        heartbeat_s: float = HEARTBEAT_EVERY_S,
    ):
        self.export_id = export_id
        self.every_s = every_s
        self.check_every_s = check_every_s
        self.heartbeat_s = heartbeat_s
        self._step: str | None = None
        self._written = time.monotonic()  # the claim has just written the row
        self._checked = 0.0
        self._cancelled = False

    def __call__(self, step: str, done: int | None = None, total: int | None = None) -> None:
        if self._cancelled:
            return
        now = time.monotonic()
        if step == self._step and now - self._written < self.every_s:
            return
        self._step, self._written, self._checked = step, now, now
        updated = Export.objects.filter(pk=self.export_id, status=Export.Status.RUNNING).update(
            progress={"step": step, "done": done, "total": total}, updated_at=timezone.now()
        )
        if not updated:
            self._cancelled = True

    def cancelled(self) -> bool:
        """True once the row no longer runs (cancelled); re-reads the status at most once a second, and
        once `heartbeat_s` passed without a write moves `updated_at` instead (the heartbeat `abandoned`
        reads: a long step between two reports never looks like a dead worker)."""
        if self._cancelled:
            return True
        now = time.monotonic()
        if now - self._written >= self.heartbeat_s:
            self._written = self._checked = now
            beat = Export.objects.filter(pk=self.export_id, status=Export.Status.RUNNING).update(
                updated_at=timezone.now()
            )
            self._cancelled = not beat
        elif now - self._checked >= self.check_every_s:
            self._checked = now
            status = Export.objects.filter(pk=self.export_id).values_list("status", flat=True).first()
            self._cancelled = status != Export.Status.RUNNING
        return self._cancelled


def read_inputs(
    book: Book, format: str, options: dict, *, export_id: int | None = None, created: datetime | None = None
) -> tuple[ExportJob, dict, str]:
    """The export's job, read once from the book now: `(job, inputs, stylesheet_hash)` — `inputs` is
    what the row records (`{stylesheet, title, author, digit_style, chapters: {id: version}}`, the
    stylesheet's values without `updated_at`). Raises `ExportNotFound` without a manuscript."""
    from editor import document as doc
    from editor.models import Manuscript
    from editor.services import stylesheet_values

    from .model import page_setup
    from .preview import stylesheet_for

    manuscript = Manuscript.objects.filter(book_id=book.pk).only("id", "document", "version").first()
    if manuscript is None:
        raise ExportNotFound(NO_MANUSCRIPT)
    document = manuscript.document or {}
    sheet = stylesheet_for(book)
    setup = page_setup(sheet)
    chapters = {
        chapter.id: doc.chapter_version(chapter.nodes(document)) for chapter in doc.chapters_of(document)
    }
    values = stylesheet_values(sheet)
    values.pop("updated_at", None)
    inputs = {
        "stylesheet": values,
        "title": book.title,
        "author": book.author,
        "digit_style": book.digit_style,
        "chapters": chapters,
    }
    job = ExportJob(
        export_id=export_id,
        book_id=book.pk,
        format=format,
        document=document,
        setup=setup,
        title=book.title,
        author=book.author,
        digit_style=book.digit_style,
        chapter_versions=chapters,
        options=dict(options),
        created=created or timezone.now(),
        manuscript_version=manuscript.version,
    )
    return job, inputs, stylesheet_hash(setup)


def _claim(export_id: int, task_id: str) -> tuple[Export | None, bool]:
    """`(row, should_run)`: see step 1 of the module docstring."""
    with transaction.atomic():
        row = Export.objects.select_for_update().filter(pk=export_id).first()
        if row is None or row.status not in ACTIVE:
            return row, False
        if row.status == Export.Status.RUNNING and row.task_id and task_id and row.task_id != task_id:
            return row, False
        now = timezone.now()
        if abandoned(row, now):
            _close_abandoned(row, now)
            return row, False
        Export.objects.filter(pk=row.pk).update(
            status=Export.Status.RUNNING,
            task_id=(task_id or row.task_id)[:64],
            started_at=now,
            finished_at=None,
            progress={"step": "prepare", "done": None, "total": None},
            error="",
            updated_at=now,
        )
    row.refresh_from_db()
    return row, True


def _fail(row: Export, headline: str, exc: BaseException | None, started: float) -> None:
    now = timezone.now()
    Export.objects.filter(pk=row.pk, status=Export.Status.RUNNING).update(
        status=Export.Status.ERROR,
        error=_error_text(headline, exc),
        duration_ms=int((time.monotonic() - started) * 1000),
        finished_at=now,
        updated_at=now,
    )


def _cancelled(row: Export, started: float) -> None:
    now = timezone.now()
    Export.objects.filter(pk=row.pk, status=Export.Status.RUNNING).update(
        status=Export.Status.CANCELLED,
        error=CANCELLED_BY_USER,
        duration_ms=int((time.monotonic() - started) * 1000),
        finished_at=now,
        updated_at=now,
    )


def _warnings(items) -> list[dict]:
    out = []
    for item in items or []:
        if isinstance(item, dict) and item.get("message"):
            out.append(
                {
                    "code": str(item.get("code") or ""),
                    "level": "warn" if item.get("level") == "warn" else "info",
                    "message": str(item["message"]),
                }
            )
    return out


def _finish(row: Export, result: ExportResult, started: float) -> None:
    """Write the file, then mark the row done if it still runs (else the file goes). Whatever interrupts
    the two (a timeout, the database) takes the written file with it, unless the row already records it:
    a file never stays behind without its row."""
    data = bytes(result.data or b"")
    path = export_path(row, row.filename or f"export{FORMATS[row.format].extension}")
    if default_storage.exists(path):  # a redelivered task writes again
        default_storage.delete(path)
    saved = None
    try:
        saved = default_storage.save(path, ContentFile(data))
        now = timezone.now()
        finished = Export.objects.filter(pk=row.pk, status=Export.Status.RUNNING).update(
            status=Export.Status.DONE,
            file=saved,
            size_bytes=len(data),
            page_count=result.page_count,
            warnings=_warnings(result.warnings),
            stats=dict(result.stats or {}),
            log="\n".join(str(line) for line in result.log or []),
            progress={"step": "done", "done": None, "total": None},
            error="",
            duration_ms=int((time.monotonic() - started) * 1000),
            finished_at=now,
            updated_at=now,
        )
    except BaseException:
        _discard_unrecorded(row.pk, saved or path)
        raise
    if not finished:  # cancelled during the write
        _discard(saved)


def _discard(name: str) -> bool:
    """Delete a file of the storage; False (logged) when it could not be deleted."""
    try:
        default_storage.delete(name)
    except Exception:  # noqa: BLE001 - reported; the sweep of the next prune tries again
        log.warning("could not delete the export file %s", name, exc_info=True)
        return False
    return True


def _discard_unrecorded(export_id: int, name: str) -> None:
    """Delete the file an interrupted `_finish` wrote, unless the row records it as done (the update
    went through before the interruption). With the database away the file stays for the sweep."""
    try:
        recorded = Export.objects.filter(pk=export_id, status=Export.Status.DONE, file=name).exists()
        if not recorded and default_storage.exists(name):
            _discard(name)
    except Exception:  # noqa: BLE001 - never hides the interruption; `sweep` removes the file later
        log.warning("could not tell whether export %s recorded %s", export_id, name, exc_info=True)


def run_export(export_id: int, task_id: str = "") -> Export | None:
    """The task's job (see the module docstring); returns the row as it ends (None: no such row)."""
    from celery.exceptions import SoftTimeLimitExceeded

    from .pdf import RenderCancelled

    row, work = _claim(export_id, task_id)
    if row is None or not work:
        return row
    started = time.monotonic()
    try:
        exporter = get_exporter(row.format)
        if exporter is None:
            _fail(row, NOT_AVAILABLE, None, started)
            return _reload(row)
        job, inputs, digest = read_inputs(
            row.book,
            row.format,
            row.options or defaults(exporter.options),
            export_id=row.pk,
            created=row.created_at,
        )
        title = printed_title(job.document, job.setup, job.title)  # the name of the title the file prints
        row.inputs, row.filename = inputs, file_name(title, row.format, row.options, row.book_id)
        Export.objects.filter(pk=row.pk).update(
            inputs=inputs,
            manuscript_version=job.manuscript_version,
            stylesheet_hash=digest,
            renderer=exporter.version[:64],
            filename=row.filename,
            updated_at=timezone.now(),
        )
        reporter = Reporter(row.pk)
        result = exporter.export(job, reporter)
        if reporter.cancelled():
            raise ExportCancelled
        _finish(row, result, started)
    except (ExportCancelled, RenderCancelled):
        _cancelled(row, started)
    except ExportNotFound as exc:
        _fail(row, exc.detail, None, started)
    except SoftTimeLimitExceeded as exc:
        _fail(row, TIMEOUT, exc, started)
    except InvalidExport as exc:
        log.error("export %s of book %s wrote an invalid file: %s", row.pk, row.book_id, exc)
        _fail(row, INVALID_FILE, exc, started)
    except Exception as exc:  # noqa: BLE001 - reported on the row (designed failure, D22)
        log.exception("export %s of book %s failed", row.pk, row.book_id)
        _fail(row, EXPORT_ERROR, exc, started)
    prune(row.book_id, row.format)
    return _reload(row)


def _reload(row: Export) -> Export | None:
    return Export.objects.filter(pk=row.pk).first()


def export_now(
    book: Book, format: str, options: dict | None = None, progress=None
) -> tuple[ExportJob, ExportResult]:
    """Export the book now, in this process, with no row (`manage.py export_book`): the job read from the
    current manuscript and the exporter's result. Raises `ExportInvalid` / `ExportNotFound`, and whatever
    the exporter raises."""
    from .exporters import NullProgress

    if format not in FORMATS:
        raise ExportInvalid(UNKNOWN_FORMAT)
    exporter = get_exporter(format)
    if exporter is None:
        raise ExportInvalid(NOT_AVAILABLE)
    try:
        values = parse_options(exporter.options, options)
    except OptionsError as exc:
        raise ExportInvalid(BAD_OPTIONS, errors=exc.errors) from None
    job, _inputs, _digest = read_inputs(book, format, values)
    return job, exporter.export(job, progress if progress is not None else NullProgress())


# ====================================================================== cancelling, pruning


def cancel_export(row: Export) -> Export:
    """Cancel a queued or running export (its task is revoked; a running exporter stops at its next
    check). Cancelling a cancelled row does nothing; a done or failed row raises `ExportFinished`."""
    from .preview import revoke

    with transaction.atomic():
        row = Export.objects.select_for_update().get(pk=row.pk)
        if row.status == Export.Status.CANCELLED:
            return row
        if row.status not in ACTIVE:
            raise ExportFinished(FINISHED, row=row)
        now = timezone.now()
        Export.objects.filter(pk=row.pk).update(
            status=Export.Status.CANCELLED, error=CANCELLED_BY_USER, finished_at=now, updated_at=now
        )
    revoke(row.task_id)
    prune(row.book_id, row.format)
    row.refresh_from_db()
    return row


def _delete_file(row: Export) -> bool:
    """Delete an export's file; False when it could not be deleted (its row is kept for the next prune)."""
    return _discard(row.file.name) if row.file else True


def prune(book_id: int, format: str) -> int:
    """Keep the newest `EXPORTS_KEPT` done rows and `FAILED_KEPT` failed or cancelled rows of a book and
    format; delete the older ones with their files — a row whose file cannot be deleted stays (the next
    prune tries again) — then `sweep` the book's export folder. Returns the rows deleted."""
    rows = list(
        Export.objects.filter(book_id=book_id, format=format)
        .exclude(status__in=ACTIVE)
        .order_by("-created_at", "-id")
        .only("id", "status", "file")
    )
    done = [row for row in rows if row.status == Export.Status.DONE]
    failed = [row for row in rows if row.status != Export.Status.DONE]
    drop = done[exports_kept() :] + failed[FAILED_KEPT:]
    gone = [row.pk for row in drop if _delete_file(row)]
    if gone:
        Export.objects.filter(pk__in=gone).delete()
    sweep(book_id)
    return len(gone)


_RE_EXPORT_ID = re.compile(r"(\d+)")


def sweep(book_id: int) -> int:
    """Delete the files of `books/<id>/exports/` that no row records (a worker killed between the write and
    the row's update). The folder is listed before the rows are read, and a file of a queued or running
    row (`<pk>.<ext>`, being written) is left alone. Returns the files deleted."""
    folder = f"books/{book_id}/exports"
    try:
        _dirs, names = default_storage.listdir(folder)
    except (FileNotFoundError, NotImplementedError):
        return 0
    except OSError:
        log.warning("could not list %s", folder, exc_info=True)
        return 0
    if not names:
        return 0
    recorded: set[str] = set()
    active: set[int] = set()
    for pk, status, name in Export.objects.filter(book_id=book_id).values_list("pk", "status", "file"):
        if name:
            recorded.add(str(name))
        if status in ACTIVE:
            active.add(pk)
    deleted = 0
    for name in names:
        path = f"{folder}/{name}"
        found = _RE_EXPORT_ID.match(name)
        if path in recorded or (found and int(found.group(1)) in active):
            continue
        if _discard(path):
            deleted += 1
            log.info("swept the unrecorded export file %s", path)
    return deleted


# ====================================================================== payloads (§3.2)


def iso(value: datetime | None) -> str | None:
    """A time for the API: local time with its offset, full precision (the long poll compares it)."""
    return timezone.localtime(value).isoformat() if value is not None else None


def size_text(size: int) -> str:
    """«318 KB», «1.2 MB» ('' for nothing)."""
    if not size or size <= 0:
        return ""
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024))} KB"
    megabytes = size / (1024 * 1024)
    return f"{megabytes:.1f} MB" if megabytes < 10 else f"{round(megabytes)} MB"


def _user_name(user) -> str:
    if user is None:
        return ""
    return (user.get_full_name() or "").strip() or user.get_username()


def _progress(row: Export, status: str) -> dict:
    progress = row.progress if isinstance(row.progress, dict) else {}
    if status == Export.Status.DONE:
        step = "done"
    elif status == Export.Status.QUEUED:
        step = "queued"
    else:
        step = str(progress.get("step") or ("prepare" if status == Export.Status.RUNNING else "queued"))
    done, total = progress.get("done"), progress.get("total")
    percent = None
    if (
        status == Export.Status.RUNNING
        and isinstance(done, int | float)
        and isinstance(total, int | float)
        and total > 0
    ):
        percent = max(0, min(100, round(100 * done / total)))
    return {"step": step, "label": STEPS.get(step, ""), "done": done, "total": total, "percent": percent}


def _download_url(row: Export) -> str | None:
    if row.status != Export.Status.DONE or not row.file:
        return None
    return reverse("publishing:export_download", args=[row.book_id, row.pk])


def export_payload(
    row: Export, user=None, *, current: Current | None = None, can_edit: bool | None = None, now=None
) -> dict:
    """A row of the API (§3.2). `current` (`current_state`) and `can_edit` (whether the technical error
    line is sent) are computed when not given."""
    from core.decorators import ROLE_EDITOR, has_role

    now = now or timezone.now()
    if can_edit is None:
        can_edit = has_role(user, ROLE_EDITOR)
    status, error, detail = row.status, "", ""
    if abandoned(row, now):
        status, error = Export.Status.ERROR, ABANDONED
    elif status == Export.Status.ERROR:
        error, detail = _headline(row.error)
    if current is None:
        current = current_state(row.book)
    reasons = stale_reasons(row, current) if current is not None and status == Export.Status.DONE else []
    exporter = get_exporter(row.format)
    info = FORMATS.get(row.format)
    options = row.options if isinstance(row.options, dict) else {}
    waiting = (
        status == Export.Status.QUEUED and row.created_at is not None and now - row.created_at > WAITING_AFTER
    )
    return {
        "id": row.pk,
        "format": row.format,
        "format_label": info.label if info is not None else row.format,
        "status": status,
        "status_label": Export.Status(status).label,
        "options": options,
        "options_text": options_text(exporter, options),
        "manuscript_version": row.manuscript_version,
        "stale": bool(reasons),
        "stale_text": STALE_TEXTS[reasons[0]] if reasons else "",
        "progress": _progress(row, status),
        "waiting_hint": WAITING_HINT if waiting else "",
        "page_count": row.page_count,
        "pages_hint": current.pages_hint if current is not None else None,
        "size_bytes": row.size_bytes if status == Export.Status.DONE else 0,
        "size_text": size_text(row.size_bytes) if status == Export.Status.DONE else "",
        "duration_ms": row.duration_ms,
        "warnings": list(row.warnings or []) if status == Export.Status.DONE else [],
        "error": error,
        "error_detail": detail if can_edit else "",
        "filename": row.filename,
        "download_url": _download_url(row) if status == Export.Status.DONE else None,
        "created_by": _user_name(row.created_by),
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
        "finished_at": iso(row.finished_at),
    }


def _slot(url: str, marker: str = "__eid__") -> str:
    """`/api/books/19/exports/0/cancel/` → `/api/books/19/exports/__eid__/cancel/`."""
    head, _sep, tail = url.rpartition("/0/")
    return f"{head}/{marker}/{tail}"


def _layout_block(book: Book, manuscript, sheet, setup, document: dict) -> dict:
    from editor import document as doc
    from editor.models import TRIM_PRESETS

    from .fonts import FONTS
    from .models import PreviewRender
    from .preview import ABANDONED_AFTER, setup_hash
    from .relayout import live_of

    live = live_of(book.pk)
    rendering = PreviewRender.objects.filter(
        book_id=book.pk,
        scope=PreviewRender.Scope.BOOK,
        status__in=(PreviewRender.Status.QUEUED, PreviewRender.Status.RUNNING),
        created_at__gte=timezone.now() - ABANDONED_AFTER,
    ).exists()
    if live is None:
        state = "rendering" if rendering else "none"
    elif live.manuscript_version < manuscript.version or live.setup_hash != setup_hash(setup):
        state = "rendering" if rendering else "stale"
    else:
        state = "current"
    trim = (
        TRIM_PRESETS[sheet.trim][0]
        if sheet.trim in TRIM_PRESETS
        else f"{sheet.width_mm:g}×{sheet.height_mm:g} مم"
    )
    size = float(setup.body_size_pt)
    face = FONTS.get(setup.body_font)
    return {
        "state": state,
        "trim_label": trim,
        "page_count": live.page_count if live is not None else None,
        "chapters": len(doc.chapters_of(document)),
        "body_font": face.name if face is not None else setup.body_font,
        "body_size_pt": int(size) if size.is_integer() else size,
    }


def _format_block(book, key: str, rows: list[Export], setup, payload) -> dict:
    info = FORMATS[key]
    exporter = get_exporter(key)
    own = [row for row in rows if row.format == key]
    now = timezone.now()
    active = next((row for row in own if row.status in ACTIVE and not abandoned(row, now)), None)
    latest = next(
        (
            row
            for row in own
            if row.status in (Export.Status.DONE, Export.Status.ERROR) or abandoned(row, now)
        ),
        None,
    )
    form: dict = {}
    notes: list[dict] = []
    if exporter is not None:
        values = defaults(exporter.options)
        if own:
            try:
                values = parse_options(exporter.options, own[0].options)
            except OptionsError:
                pass
        try:
            form = exporter.form(book, values)
            notes = list(exporter.notes(book, setup))
        except Exception:  # noqa: BLE001 - a broken exporter must not take the page down
            log.exception("the %s exporter's form or notes failed for book %s", key, book.pk)
    return {
        "key": key,
        "label": info.label,
        "extension": info.extension,
        "available": exporter is not None,
        "form": form,
        "notes": notes,
        "active": payload(active) if active is not None else None,
        "latest": payload(latest) if latest is not None else None,
    }


def page_payload(book: Book, user=None) -> dict:
    """The export page's object (§3.2; `api:exports` GET and the page's `config`), in a bounded number of
    queries whatever the history's length. The export rows are read last, after the readiness (the slow
    part): a payload's rows are as fresh as the page can have them (the page never takes a row back to an
    older state it already saw either, `export.js`)."""
    from core.decorators import ROLE_EDITOR, has_role
    from editor.models import Manuscript

    from .model import page_setup
    from .preview import stylesheet_for
    from .readiness import book_readiness

    manuscript = Manuscript.objects.filter(book_id=book.pk).select_related("run").defer("base").first()
    can_edit = has_role(user, ROLE_EDITOR)
    urls = {
        "create": reverse("api:exports", args=[book.pk]),
        "row": _slot(reverse("api:export", args=[book.pk, 0])),
        "cancel": _slot(reverse("api:export_cancel", args=[book.pk, 0])),
    }
    if manuscript is None:
        current = Current(None, "", {}, None)
        layout = None
        readiness: list[dict] = []
        setup = page_setup(stylesheet_for(book))
    else:
        sheet = stylesheet_for(book)
        setup = page_setup(sheet)
        document = manuscript.document or {}
        layout = _layout_block(book, manuscript, sheet, setup, document)
        current = Current(
            manuscript_version=manuscript.version,
            stylesheet_hash=stylesheet_hash(setup),
            renderers={
                key: exporter.version for key in FORMATS if (exporter := get_exporter(key)) is not None
            },
            pages_hint=layout["page_count"],
        )
        readiness = book_readiness(book, manuscript=manuscript, setup=setup)
    rows = list(
        Export.objects.filter(book_id=book.pk).select_related("created_by").order_by("-created_at", "-id")
    )
    now = timezone.now()

    def payload(row: Export) -> dict:
        return export_payload(row, user, current=current, can_edit=can_edit, now=now)

    formats = [_format_block(book, key, rows, setup, payload) for key in FORMATS]
    return {
        "book": {
            "id": book.pk,
            "title": book.title,
            "has_manuscript": manuscript is not None,
            "layout": layout,
        },
        "can_edit": can_edit,
        "readiness": readiness,
        "formats": formats,
        "items": [payload(row) for row in rows[:ITEMS_LISTED]],
        "urls": urls,
    }


def wait_for(row: Export, seconds: float, since: datetime | None = None) -> Export:
    """The row once it changed after `since` (its `updated_at`) or reached a final status, or after
    `seconds` (at most 5: the API's long poll). Every `WAIT_STEP_S` only the status and the times are
    read; the whole row (with its book and author) is read once at the end, and only when it moved."""
    deadline = time.monotonic() + max(0.0, min(float(seconds), MAX_WAIT_S))
    moved = False
    while True:
        final = row.status not in ACTIVE or abandoned(row)
        changed = since is not None and row.updated_at is not None and row.updated_at > since
        left = deadline - time.monotonic()
        if final or changed or left <= 0:
            break
        time.sleep(min(WAIT_STEP_S, left))
        found = Export.objects.filter(pk=row.pk).values_list("status", "updated_at", "started_at").first()
        if found is None:  # deleted meanwhile: the next poll answers 404
            break
        if found != (row.status, row.updated_at, row.started_at):
            row.status, row.updated_at, row.started_at = found
            moved = True
    if not moved:
        return row
    fresh = Export.objects.filter(pk=row.pk).select_related("book", "created_by").first()
    return fresh if fresh is not None else row
