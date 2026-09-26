"""The exporter interface (PHASE6_SPEC §6.3, D58): what the export pipeline (`publishing.exports`) asks of
a format's writer, the formats and progress steps of the shared contract (§3.1), and the registry.

- **Formats** (`FORMATS`): `docx` «Word», `print_pdf` «PDF للطباعة», `screen_pdf` «PDF للشاشة», `epub`
  «EPUB», all declared from the start. A format is *available* only once its exporter resolves.
- **An exporter** (`Exporter`) has a `format`, `label`, `extension`, `media_type`, a `version` (recorded
  on every export: a newer one marks older files «تحدّث نسّاخ بعد هذا الإخراج»), its `options`
  (`OptionSpec`s: validation only, the pipeline normalises a request through them), and three methods:
  `form(book, values)` (the export page's form block: labels, hints, counts), `notes(book, setup)` (the
  known differences for this book, `{code, level, message, action?}` rows) and `export(job, progress)`
  (the file: `ExportResult`). `export` reads nothing the job does not carry, except what it lays out
  for itself; it raises `ExportCancelled` when `progress.cancelled()` says so (the engine's
  `RenderCancelled` is treated alike) and `InvalidExport` when the file fails its own check.
- **The registry.** `EXPORTERS` maps each format to the dotted paths where its exporter lives, so the
  Word, PDF and EPUB streams never edit this file: `get_exporter(format)` imports the module on first
  use (a missing module means "not available"; a broken one is logged once and counts as missing) and
  takes the class (instantiated without arguments) or instance found there. A module may instead call
  `register(exporter)` when it is imported; `register` also serves tests (`registered(FakeExporter())`).
- **`FakeExporter`** writes fixed bytes, reports its steps and honours cancelling: the pipeline and UI
  tests use it.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from .model import PageSetup
from .models import Export

log = logging.getLogger(__name__)


# ====================================================================== the contract (§3.1)


@dataclass(frozen=True)
class FormatInfo:
    """A format of the contract: its key, Arabic label, file extension and media type."""

    key: str
    label: str
    extension: str
    media_type: str


FORMATS: dict[str, FormatInfo] = {
    Export.Format.DOCX.value: FormatInfo(
        "docx",
        Export.Format.DOCX.label,
        ".docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    Export.Format.PRINT_PDF.value: FormatInfo(
        "print_pdf", Export.Format.PRINT_PDF.label, ".pdf", "application/pdf"
    ),
    Export.Format.SCREEN_PDF.value: FormatInfo(
        "screen_pdf", Export.Format.SCREEN_PDF.label, ".pdf", "application/pdf"
    ),
    Export.Format.EPUB.value: FormatInfo("epub", Export.Format.EPUB.label, ".epub", "application/epub+zip"),
}

# The progress steps and their labels. Word: prepare → (layout) → write → check → done. PDF: prepare →
# layout → footnotes → (relax) → write → check → done.
STEPS: dict[str, str] = {
    "queued": "في الانتظار",
    "prepare": "تحضير النص",
    "layout": "ترتيب الصفحات",
    "footnotes": "ترقيم الحواشي",
    "relax": "ضبط الحواشي مع أسطرها",
    "write": "كتابة الملف",
    "check": "فحص الملف",
    "done": "اكتمل",
}

# Where each format's exporter lives (the first path that resolves wins).
EXPORTERS: dict[str, tuple[str, ...]] = {
    "docx": ("publishing.word.DocxExporter", "publishing.word.exporter.DocxExporter"),
    "print_pdf": ("publishing.pdf_export.PrintPdfExporter",),
    "screen_pdf": ("publishing.pdf_export.ScreenPdfExporter",),
    "epub": ("publishing.epub.EpubExporter",),
}


class ExportCancelled(Exception):  # noqa: N818 - a signal, not an error
    """The export was cancelled while it ran (`progress.cancelled()`): the row stays cancelled, no file."""


class InvalidExport(Exception):
    """The written file failed its check (schema, integrity): the export fails with `INVALID_FILE`, and
    `str(exc)` (the technical details) goes to the row's error line and the server log."""


@dataclass(frozen=True)
class ExportJob:
    """What an exporter works from, read once when the task starts (an edit made during the export never
    gets into the file): the manuscript `document` and its version, the page `setup`, the book's title,
    author and digit style, each chapter's version, the normalised `options` and the row's creation
    time. `export_id` is None for an export without a row (`manage.py export_book`)."""

    export_id: int | None
    book_id: int
    format: str
    document: dict
    setup: PageSetup
    title: str
    author: str
    digit_style: str
    chapter_versions: dict[str, str]
    options: dict
    created: datetime
    manuscript_version: int | None = None


@dataclass
class ExportResult:
    """The file and what the build found: `page_count` (null for Word), `warnings` (`{code, level,
    message}`: the notes that applied to the file and the build's own), `stats` and technical `log`
    lines."""

    data: bytes
    page_count: int | None = None
    warnings: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    log: list[str] = field(default_factory=list)


@runtime_checkable
class Progress(Protocol):
    """How an exporter reports: `progress(step, done, total)` (a `STEPS` key; `done`/`total` when the
    step can count) and `progress.cancelled()` (checked between steps and passes; cheap to call often)."""

    def __call__(self, step: str, done: int | None = None, total: int | None = None) -> None: ...

    def cancelled(self) -> bool: ...


class NullProgress:
    """A `Progress` that records nothing and is never cancelled (`manage.py export_book`)."""

    def __init__(self, on_step: Callable[[str], None] | None = None):
        self.steps: list[str] = []
        self._on_step = on_step

    def __call__(self, step: str, done: int | None = None, total: int | None = None) -> None:
        if not self.steps or self.steps[-1] != step:
            self.steps.append(step)
            if self._on_step is not None:
                self._on_step(step)

    def cancelled(self) -> bool:
        return False


# ====================================================================== options


BAD_CHOICE = "قيمة غير معروفة لهذا الخيار."
BAD_BOOL = "يُنتظر «نعم» أو «لا» لهذا الخيار."
BAD_NUMBER = "يُنتظر رقم بين {low} و{high}."


class OptionsError(ValueError):
    """Options that do not pass their specs: `errors` is `{key: Arabic message}`."""

    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{key}: {message}" for key, message in errors.items()))
        self.errors = errors


def _number_text(value: float) -> str:
    return f"{value:g}"


@dataclass(frozen=True)
class OptionSpec:
    """One option of a format, for validation: `kind` is `choice`, `bool`, `int` or `float`; `choices`
    the allowed values of a choice; `bounds` `(low, high)` of a number. `text` maps a value to the words
    the history shows for it (`options_text`, «كشيدة خفيفة · تعليقات»); a value missing there shows
    nothing."""

    key: str
    kind: str
    default: Any
    choices: tuple = ()
    bounds: tuple[float, float] | None = None
    text: Mapping[Any, str] = field(default_factory=dict)

    def parse(self, value: Any) -> Any:
        """The value normalised (None: the default); raises `ValueError` with an Arabic message."""
        if value is None:
            return self.default
        if self.kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, int) and value in (0, 1):
                return bool(value)
            if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", "on", "off"):
                return value.strip().lower() in ("true", "1", "on")
            raise ValueError(BAD_BOOL)
        if self.kind == "choice":
            for choice in self.choices:
                if value == choice and type(value) is not bool:
                    return choice
                if isinstance(value, str) and not isinstance(choice, str) and value.strip() == str(choice):
                    return choice
            raise ValueError(BAD_CHOICE)
        if self.kind in ("int", "float"):
            if isinstance(value, bool):
                raise ValueError(self._range_message())
            try:
                number = float(value.strip()) if isinstance(value, str) else float(value)
            except (TypeError, ValueError):
                raise ValueError(self._range_message()) from None
            if self.kind == "int":
                if number != int(number):
                    raise ValueError(self._range_message())
                number = int(number)
            if self.bounds is not None and not (self.bounds[0] <= number <= self.bounds[1]):
                raise ValueError(self._range_message())
            return number
        raise ValueError(BAD_CHOICE)

    def _range_message(self) -> str:
        low, high = self.bounds if self.bounds is not None else (0, 0)
        return BAD_NUMBER.format(low=_number_text(low), high=_number_text(high))


def defaults(specs: Sequence[OptionSpec]) -> dict:
    """Every option at its default."""
    return {spec.key: spec.default for spec in specs}


def parse_options(specs: Sequence[OptionSpec], data: Any) -> dict:
    """`data` normalised through `specs`: every key present (defaults filled in), unknown keys dropped.
    Raises `OptionsError` with the per-key messages."""
    data = data if isinstance(data, dict) else {}
    out: dict = {}
    errors: dict[str, str] = {}
    for spec in specs:
        try:
            out[spec.key] = spec.parse(data.get(spec.key))
        except ValueError as exc:
            errors[spec.key] = str(exc)
    if errors:
        raise OptionsError(errors)
    return out


def options_text(exporter: Exporter | None, options: dict) -> str:
    """The options in words for the history («كشيدة خفيفة · تعليقات»): the exporter's `describe(options)`
    when it has one, else each spec's `text` for its value."""
    if exporter is None:
        return ""
    describe = getattr(exporter, "describe", None)
    if callable(describe):
        return str(describe(options) or "")
    words = []
    for spec in exporter.options:
        word = spec.text.get(options.get(spec.key, spec.default), "") if spec.text else ""
        if word:
            words.append(word)
    return " · ".join(words)


# ====================================================================== the exporter protocol


@runtime_checkable
class Exporter(Protocol):
    """A format's writer (see the module docstring)."""

    format: str
    label: str
    extension: str
    media_type: str
    options: tuple[OptionSpec, ...]
    version: str

    def form(self, book, values: dict) -> dict: ...

    def notes(self, book, setup: PageSetup) -> list[dict]: ...

    def export(self, job: ExportJob, progress: Progress) -> ExportResult: ...


# ====================================================================== the registry

_registry: dict[str, Exporter] = {}
_broken: set[str] = set()  # dotted paths whose import failed (logged once)


def register(exporter):
    """Register an exporter (an instance, or a class instantiated without arguments) under its `format`;
    returns what it was given, so it also works as a class decorator. A later registration replaces an
    earlier one."""
    instance = exporter() if isinstance(exporter, type) else exporter
    key = getattr(instance, "format", None)
    if key not in FORMATS:
        raise ValueError(f"unknown export format {key!r}")
    _registry[key] = instance
    return exporter


def unregister(format: str) -> Exporter | None:
    """Remove a format's exporter (tests); returns it."""
    return _registry.pop(format, None)


@contextmanager
def registered(exporter) -> Iterator[Exporter]:
    """Register `exporter` for the block, then put back what was there (tests)."""
    instance = exporter() if isinstance(exporter, type) else exporter
    key = instance.format
    previous = _registry.get(key)
    register(instance)
    try:
        yield instance
    finally:
        if previous is None:
            _registry.pop(key, None)
        else:
            _registry[key] = previous


def _load(format: str) -> Exporter | None:
    for path in EXPORTERS.get(format, ()):
        module_name, _dot, attribute = path.rpartition(".")
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            if not (module_name == missing or module_name.startswith(missing + ".")) and path not in _broken:
                _broken.add(path)
                log.warning("exporter %s needs a missing module (%s)", path, missing)
            continue
        except Exception:  # noqa: BLE001 - a module being written: the format is not available
            if path not in _broken:
                _broken.add(path)
                log.exception("exporter %s could not be imported", path)
            continue
        if format in _registry:  # the module registered itself on import
            return _registry[format]
        try:
            found = getattr(module, attribute, None)  # (a package may import its exporter lazily)
        except Exception:  # noqa: BLE001 - an exporter module being written: not available
            if path not in _broken:
                _broken.add(path)
                log.exception("exporter %s could not be imported", path)
            continue
        if found is None:
            continue
        try:
            register(found)
        except Exception:  # noqa: BLE001 - a wrong exporter counts as missing
            if path not in _broken:
                _broken.add(path)
                log.exception("exporter %s could not be registered", path)
            continue
        return _registry.get(format)
    return None


def get_exporter(format: str) -> Exporter | None:
    """The exporter of a format (None: unknown, or not available yet)."""
    if format not in FORMATS:
        return None
    found = _registry.get(format)
    return found if found is not None else _load(format)


get = get_exporter


def available_formats() -> list[str]:
    """The formats whose exporter resolves, in the contract's order."""
    return [key for key in FORMATS if get_exporter(key) is not None]


def is_available(format: str) -> bool:
    """True when the format's exporter resolves."""
    return get_exporter(format) is not None


# ====================================================================== the fake (tests)

FAKE_KASHIDA: tuple[tuple[str, str, str, str], ...] = (
    ("none", "بلا", "بلا كشيدة", "تُضبط الأسطر بالمسافات وحدها، كما في المعاينة."),
    ("low", "خفيفة", "كشيدة خفيفة", "الأسطر كما في المعاينة تقريبًا."),
    ("medium", "متوسطة", "كشيدة متوسطة", "يغيّر Word فواصل الأسطر فيطول الكتاب عن المعاينة."),
    ("high", "قوية", "كشيدة قوية", "يغيّر Word فواصل الأسطر فيطول الكتاب كثيرًا عن المعاينة."),
)
FAKE_COMMENTS_LABEL = "تعليقات على الكلمات غير المؤكَّدة"
FAKE_COMMENTS_HINT = (
    "تعليق Word لكل كلمة بقراءاتها وصفحتها الأصلية، للمدقّق. نسخة للمراجعة لا للمطبعة: يطبع Word"
    " التعليقات في الهامش ما لم تُخفِها."
)
FAKE_DOCX_OPTIONS: tuple[OptionSpec, ...] = (
    OptionSpec(
        "kashida",
        "choice",
        "low",
        choices=tuple(key for key, *_rest in FAKE_KASHIDA),
        text={key: title for key, _short, title, _hint in FAKE_KASHIDA},
    ),
    OptionSpec("comments", "bool", False, text={True: "تعليقات"}),
)
FAKE_PRINT_OPTIONS: tuple[OptionSpec, ...] = (
    OptionSpec("bleed_mm", "choice", 0, choices=(0, 3, 5), text={3: "نزف 3 مم", 5: "نزف 5 مم"}),
    OptionSpec("crop_marks", "bool", False, text={True: "علامات القص"}),
)


class FakeExporter:
    """A test exporter: `data` as the file, `steps` reported in order (each followed by `on_step(step,
    job, progress)` when given: a test edits the book, cancels, …), `error` raised after the steps;
    `ExportCancelled` as soon as `progress.cancelled()` says so. `calls` keeps every job it was given.
    The docx fake has the Word options (kashida, comments) and a form block of the §3.2 shape."""

    def __init__(
        self,
        format: str = "docx",
        *,
        data: bytes = b"PK fake export",
        steps: Sequence[str] = ("prepare", "write", "check"),
        error: BaseException | None = None,
        on_step: Callable[[str, ExportJob, Progress], None] | None = None,
        page_count: int | None = None,
        warnings: Sequence[dict] = (),
        notes: Sequence[dict] = (),
        version: str = "fake-1",
        options: tuple[OptionSpec, ...] | None = None,
    ):
        info = FORMATS[format]
        self.format = info.key
        self.label = info.label
        self.extension = info.extension
        self.media_type = info.media_type
        self.version = version
        if options is None:
            options = {"docx": FAKE_DOCX_OPTIONS, "print_pdf": FAKE_PRINT_OPTIONS}.get(format, ())
        self.options = options
        self.data = data
        self.steps = tuple(steps)
        self.error = error
        self.on_step = on_step
        self.page_count = page_count
        self.warnings = [dict(item) for item in warnings]
        self._notes = [dict(item) for item in notes]
        self.calls: list[ExportJob] = []

    def form(self, book, values: dict) -> dict:
        if self.format != "docx":
            return {key: {"value": value} for key, value in values.items()}
        from .readiness import uncertain_counts

        available = uncertain_counts(book).total
        return {
            "kashida": {
                "value": values.get("kashida", "low"),
                "choices": [
                    {"value": key, "label": short, "title": title, "hint": hint}
                    for key, short, title, hint in FAKE_KASHIDA
                ],
            },
            "comments": {
                "value": bool(values.get("comments")) and available > 0,
                "available": available,
                "label": FAKE_COMMENTS_LABEL,
                "hint": FAKE_COMMENTS_HINT if available else "لا كلمات غير مؤكَّدة في الكتاب.",
            },
        }

    def notes(self, book, setup: PageSetup) -> list[dict]:
        return [dict(item) for item in self._notes]

    def export(self, job: ExportJob, progress: Progress) -> ExportResult:
        self.calls.append(job)
        for step in self.steps:
            if progress.cancelled():
                raise ExportCancelled
            progress(step)
            if self.on_step is not None:
                self.on_step(step, job, progress)
            if progress.cancelled():
                raise ExportCancelled
        if self.error is not None:
            raise self.error
        return ExportResult(
            data=self.data,
            page_count=self.page_count,
            warnings=[dict(item) for item in self.warnings],
            stats={"fake": True, "bytes": len(self.data)},
            log=[f"fake {self.format} export of book {job.book_id}"],
        )
