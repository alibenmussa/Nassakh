"""The PDF exports (PHASE6_SPEC §9, D61): `print_pdf` «PDF للطباعة» and `screen_pdf` «PDF للشاشة».

Both are a fresh final render of the exported manuscript version through the book page's engine and
passes (`publishing.engine`, `RenderJob.output`), so their pages are the preview's exactly: the same
markup, CSS and footnote passes, with only rules that never move a line added.

- **Print** (`PrintPdfExporter`, options `bleed_mm` ∈ {0, 3, 5} and `crop_marks`, both off by default
  until the printer says otherwise, Q7): the TrimBox is the trim; the MediaBox grows with the bleed (and
  a 6 mm slug for the crop marks, so they sit outside the bleed); the BleedBox is the trim plus the bleed
  exactly; black is pure K; `Trapped /False`.
- **Screen** (`ScreenPdfExporter`, no options): RGB, MediaBox = TrimBox, the outline pane open, and each
  footnote call a link to its note (the call's element sits in a link; the lines do not move).
- **Both**: a clean outline (chapters at level 1, sections at level 2, «المحتويات» at level 1, nothing
  for the title page, no note calls in the labels), clickable contents entries, right-to-left reading
  with the document title in the viewer, subset fonts, and the metadata (title, author, subject — the
  subtitle —, creator «نسّاخ», `/Lang (ar)`, the export's time).

**Steps:** prepare → layout → footnotes → (relax) → write (the engine's passes, `on_pass`) → check.
Between the write and the check, `pdf_text.fix_text_layer` reverses the ToUnicode entries of the Arabic
ligatures (PHASE7_SPEC §4.7, owner question 1): Chrome, Firefox and MuPDF copy «لا» right; the check reads
the file as it ships.

**The check** reads the file back with PyMuPDF (`audit_pdf`): its page count and boxes (a file that does
not match is an `InvalidExport`), and the font of every span — a character set in a face that is not a
book face (`nk-body`, `nk-heading`, `nk-latin`, or any name record of a resolved face's files: fontconfig's
fallback for characters no book face has) becomes the `foreign_fonts` warning. The fonts are listed once
from the file's objects (never page by page). Font names are compared Unicode-aware: WeasyPrint may name
an embedded face after an Arabic name record (Lotus: «خط-لوتس-الجديد»), which PyMuPDF hands back as UTF-8
read as Latin-1 and cut at 31 bytes in a span (`repair_name`, `span_fonts`). The page count and chapter
ranges are compared with the book page (`reference_layout`: a finished preview render of the same job,
else the live layout when it shows the exported text — every chapter the file prints, at its version —
with the same setup); a difference is `layout_mismatch`.

**Notes** (`notes`, before exporting): the live layout's page checks (`page_checks`, → «الفصول») and each
face that is not installed (`missing_font`, Amiri stands in as in the preview).
"""

from __future__ import annotations

import codecs
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from assembly.render import ar_count

from . import fonts as F
from .exporters import (
    ExportCancelled,
    ExportJob,
    ExportResult,
    InvalidExport,
    OptionSpec,
    Progress,
)
from .model import PageSetup
from .pdf_text import fix_text_layer
from .readiness import CHECKS, WARN, action, missing_font_rows, page_checks_row, row

log = logging.getLogger(__name__)

MEDIA_TYPE_PDF = "application/pdf"
MM_PT = 72 / 25.4
BOX_TOLERANCE_PT = 0.05
BOOK_FAMILIES: tuple[str, ...] = ("nk-body", "nk-heading", "nk-latin")
PAGES_LISTED = 8

# (value, short label, full label, hint): the print form's bleed choices (Q7: 0 until the printer asks)
BLEEDS: tuple[tuple[int, str, str, str], ...] = (
    (0, "بلا", "بلا نزف", "الصفحة بمقاس القطع تمامًا، كما في المعاينة."),
    (3, "3 مم", "نزف 3 مم", "تتسع الصفحة 3 مم من كل جهة حول القطع، وهو ما تطلبه أغلب المطابع."),
    (5, "5 مم", "نزف 5 مم", "تتسع الصفحة 5 مم من كل جهة حول القطع."),
)
BLEED_LABEL = "النزف"
CROP_MARKS_LABEL = "علامات القص"
CROP_MARKS_HINT = "خطوط دقيقة خارج الصفحة تُري المطبعة حدود القطع."

PRINT_OPTIONS: tuple[OptionSpec, ...] = (
    OptionSpec(
        "bleed_mm",
        "choice",
        0,
        choices=tuple(value for value, *_rest in BLEEDS),
        text={value: title for value, _short, title, _hint in BLEEDS if value},
    ),
    OptionSpec("crop_marks", "bool", False, text={True: CROP_MARKS_LABEL}),
)

FOREIGN_FONT = "حروف {where} طُبعت بخط «{name}»، وهو ليس من خطوط الكتاب."
PAGE_COUNT_MISMATCH = "عدد صفحات الملف {file} وصفحات الكتاب {book}."
CHAPTER_MISMATCH = "يبدأ «{title}» في الملف في الصفحة {file} وفي الكتاب في الصفحة {book}."
CHAPTER_MISSING = "لا يظهر «{title}» في الملف كما يظهر في الكتاب."


# ====================================================================== the font audit


def repair_name(name: str) -> str:
    """A font name as the face calls itself: PyMuPDF hands names that are UTF-8 bytes back as Latin-1
    (`Ø®Ø·-Ù\\x84Ù\\x88ØªØ³` → `خط-لوتس`); when the Latin-1 bytes decode as UTF-8 (an incomplete last
    character dropped: a span's font name is cut at 31 bytes) the decoded name, else the name unchanged
    (a real Latin-1 name such as `Café`, or one already decoded)."""
    try:
        raw = name.encode("latin-1")
    except UnicodeEncodeError:
        return name
    if raw.isascii():
        return name
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(raw, final=False)
    except UnicodeDecodeError:
        return name
    return text if any(ord(char) > 0x7F for char in text) else name


def _norm(name: str) -> str:
    """A font name for comparing, in any script: NFKC, case folded, letters and digits only
    (`Times-New-Roman,` → `timesnewroman`, `خط-لوتس-الجديد` → `خطلوتسالجديد`)."""
    folded = unicodedata.normalize("NFKC", repair_name(name)).casefold()
    return "".join(char for char in folded if char.isalnum())


def base_name(name: str) -> str:
    """A PDF font name without its subset prefix (`ABCDEF+nk-body` → `nk-body`)."""
    head, plus, rest = name.partition("+")
    return rest if plus and len(head) == 6 and head.isascii() and head.isupper() else name


def display_name(name: str) -> str:
    """A font name as the warning shows it (`PMUTZQ+MS-Mincho` → `MS Mincho`)."""
    text = base_name(repair_name(name)).replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", text).strip(" ,")


FACE_NAME_IDS = (1, 4, 6, 16)  # family, full, PostScript, typographic family


@lru_cache(maxsize=64)
def _file_names(path: str, size: int, mtime: int) -> tuple[str, ...]:
    """Every name record of `FACE_NAME_IDS` in a font file, on every platform and in every language
    (Lotus's Windows names are Arabic only: «خط لوتس الجديد»)."""
    from fontTools.ttLib import TTFont

    names: list[str] = []
    try:
        with TTFont(path, lazy=True, fontNumber=0) as font:
            for record in font["name"].names:
                if record.nameID not in FACE_NAME_IDS:
                    continue
                try:
                    value = record.toUnicode()
                except (UnicodeDecodeError, LookupError):
                    continue
                if value.strip() and value not in names:
                    names.append(value)
    except Exception:  # noqa: BLE001 - an unreadable table: the registry's names stand in
        return ()
    return tuple(names)


def face_file_names(path: Path) -> tuple[str, ...]:
    """`_file_names` of a face file (cached by path, size and mtime)."""
    try:
        stat = path.stat()
    except OSError:
        return ()
    return _file_names(str(path), stat.st_size, int(stat.st_mtime))


def book_face_names(fonts: F.ResolvedFonts) -> tuple[str, ...]:
    """The normalised names a book face may carry in the PDF: the roles' families, each resolved face's
    name and family, and every name record (family, full, PostScript, typographic family; all platforms
    and languages) of its files."""
    names = {_norm(family) for family in BOOK_FAMILIES}
    for face in (fonts.body, fonts.latin, fonts.heading):
        names.add(_norm(face.name))
        names.add(_norm(face.family))
        for path in (face.files.regular, face.files.bold, face.files.italic, face.files.bold_italic):
            if path is not None:
                names.update(_norm(local) for local in F.local_names(path))
                names.update(_norm(record) for record in face_file_names(path))
    return tuple(sorted(name for name in names if name))


def is_book_face(name: str, allowed: tuple[str, ...]) -> bool:
    """True when a PDF font (a span's font name) is one of the book's faces (`nk-heading-Bold` too)."""
    normal = _norm(base_name(repair_name(name)))
    return bool(normal) and any(normal.startswith(prefix) for prefix in allowed)


def document_fonts(document) -> list[dict]:
    """The fonts of a PDF, listed once from its objects (`/Type /Font`; a composite font's descendant
    left out): `[{name, type, subset}]` with the name repaired (`repair_name`), in object order. Page by
    page (`get_page_fonts(full=True)`) the listing grows with the pages times their resources."""
    out: list[dict] = []
    seen: set[str] = set()
    for xref in range(1, document.xref_length()):
        try:
            if document.xref_get_key(xref, "Type") != ("name", "/Font"):
                continue
            _kind, subtype = document.xref_get_key(xref, "Subtype")
            kind, base = document.xref_get_key(xref, "BaseFont")
            if kind != "name":
                kind, base = document.xref_get_key(xref, "Name")  # a Type 3 font has no BaseFont
        except Exception:  # noqa: BLE001 - a broken object is not a font of the pages
            continue
        subtype = subtype.lstrip("/")
        if subtype in ("CIDFontType0", "CIDFontType2"):
            continue  # the descendant of a Type 0 font listed on its own
        name = repair_name(base.lstrip("/")) if kind == "name" else f"{subtype}-{xref}"
        if name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "type": subtype, "subset": base_name(name) != name})
    return out


def span_fonts(name: str, fonts: list[str]) -> list[str]:
    """The document's fonts (whole names, no subset prefix) a span's font name stands for: itself, or —
    a span name is cut at 31 bytes, the subset prefix counted — every longer name it begins."""
    name = base_name(repair_name(name))
    if not name or name in fonts:
        return [name]
    return [font for font in fonts if font.startswith(name)] or [name]


@dataclass
class PdfAudit:
    """What the file holds: its page count, the raw boxes of its first page (points), the embedded fonts
    (`{name, type, subset}`), the Type 3 and not-subset fonts, and the foreign fonts with their pages
    (printed numbers) and characters."""

    page_count: int
    boxes: dict[str, list[float]] = field(default_factory=dict)
    fonts: list[dict] = field(default_factory=list)
    type3: list[str] = field(default_factory=list)
    not_subset: list[str] = field(default_factory=list)
    foreign: dict[str, dict] = field(default_factory=dict)  # display name → {pages: [...], chars: "…"}
    characters: int = 0


def _box(document, xref: int, key: str) -> list[float]:
    kind, value = document.xref_get_key(xref, key)
    if kind != "array":
        return []
    return [float(item) for item in value.strip("[]").split()]


def audit_pdf(data: bytes, fonts: F.ResolvedFonts, first_page: int = 1) -> PdfAudit:
    """Read the PDF back (PyMuPDF): see `PdfAudit`. Raises `InvalidExport` when it does not open."""
    import pymupdf

    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - an unreadable file is an invalid export
        raise InvalidExport(f"the PDF does not open: {exc}") from exc
    allowed = book_face_names(fonts)
    with document:
        audit = PdfAudit(page_count=document.page_count)
        if document.page_count:
            xref = document[0].xref
            audit.boxes = {key: _box(document, xref, key) for key in ("MediaBox", "TrimBox", "BleedBox")}
        audit.fonts = document_fonts(document)
        for item in audit.fonts:
            if item["type"] == "Type3":
                audit.type3.append(item["name"])
            if not item["subset"]:
                audit.not_subset.append(item["name"])
        whole = [base_name(item["name"]) for item in audit.fonts]
        verdicts: dict[str, str | None] = {}  # a span's font name → None (a book face) or the foreign name
        for index, page in enumerate(document):
            number = index + first_page
            for block in page.get_text("dict", flags=0)["blocks"]:
                for line in block.get("lines", ()):
                    for span in line.get("spans", ()):
                        text = "".join(char for char in span.get("text", "") if not char.isspace())
                        if not text:
                            continue
                        audit.characters += len(text)
                        name = str(span.get("font") or "")
                        if name not in verdicts:
                            candidates = span_fonts(name, whole)
                            book = any(is_book_face(candidate, allowed) for candidate in candidates)
                            verdicts[name] = None if book else display_name(candidates[0])
                        foreign = verdicts[name]
                        if foreign is None:
                            continue
                        found = audit.foreign.setdefault(foreign, {"pages": [], "chars": ""})
                        if number not in found["pages"]:
                            found["pages"].append(number)
                        found["chars"] += "".join(char for char in text if char not in found["chars"])
    return audit


def pages_phrase(pages: list[int]) -> str:
    """«في الصفحة 47» / «في الصفحات 47، 81» (at most `PAGES_LISTED`, then «وغيرها»)."""
    pages = sorted(pages)
    if len(pages) == 1:
        return f"في الصفحة {pages[0]}"
    shown = "، ".join(str(page) for page in pages[:PAGES_LISTED])
    more = " وغيرها" if len(pages) > PAGES_LISTED else ""
    return f"في الصفحات {shown}{more}"


def foreign_font_rows(audit: PdfAudit) -> list[dict]:
    """The `foreign_fonts` warnings (one per foreign face, its pages)."""
    return [
        row("foreign_fonts", WARN, FOREIGN_FONT.format(where=pages_phrase(found["pages"]), name=name))
        for name, found in sorted(audit.foreign.items(), key=lambda item: min(item[1]["pages"]))
    ]


def check_boxes(audit: PdfAudit, setup: PageSetup, output) -> list[str]:
    """The file's boxes against the trim and the bleed asked for (empty: right)."""
    width, height = setup.width_mm * MM_PT, setup.height_mm * MM_PT
    bleed = output.bleed_mm * MM_PT if output.is_print else 0.0
    page = output.page_bleed_mm * MM_PT
    wanted = {
        "TrimBox": [0.0, 0.0, width, height],
        "BleedBox": [-bleed, -bleed, width + bleed, height + bleed],
        "MediaBox": [-page, -page, width + page, height + page],
    }
    errors = []
    for key, box in wanted.items():
        found = audit.boxes.get(key) or []
        if len(found) != 4 or any(abs(a - b) > BOX_TOLERANCE_PT for a, b in zip(found, box, strict=True)):
            errors.append(f"{key} is {found}, expected {[round(value, 3) for value in box]}")
    return errors


# ====================================================================== the book page's layout


@dataclass(frozen=True)
class Reference:
    """The book page's pages an export is compared with: where they come from (`preview`: a finished
    render of the same job; `live`: the live layout), their count, chapter ranges and page checks."""

    source: str
    page_count: int
    chapters: list = field(default_factory=list)
    checks: list = field(default_factory=list)


def printed_chapters(job: ExportJob) -> dict[str, str]:
    """`{chapter id: version}` of the chapters the job prints: those with blocks (a chapter left with
    nothing to print has no page, in the file as in the book page), versioned as the engine does."""
    from editor import document as doc

    from .model import book_model

    model = book_model(job.document, job.setup, title=job.title, author=job.author)
    printable = {chapter.id for chapter in model.chapters if chapter.blocks}
    return {
        chapter.id: doc.chapter_version(chapter.nodes(job.document))
        for chapter in doc.chapters_of(job.document)
        if chapter.id in printable
    }


def reference_layout(job: ExportJob, printed: dict[str, str] | None = None) -> Reference | None:
    """The book page's pages for this job: a finished preview render of the same job hash, else the live
    layout when it shows the exported text — the chapters the file prints (`printed`, `{id: version}`:
    the export's own chapter ranges; `printed_chapters` when not given), each at its version — with the
    same page setup; None when neither exists. An empty chapter never makes the live layout look old."""
    from .engine import RenderJob
    from .models import PreviewRender
    from .preview import job_hash, setup_hash
    from .readiness import layout_checks
    from .relayout import live_of

    digest = job_hash(
        RenderJob(document=job.document, stylesheet=job.setup, title=job.title, author=job.author)
    )
    render = (
        PreviewRender.objects.filter(
            book_id=job.book_id,
            scope=PreviewRender.Scope.BOOK,
            kind=PreviewRender.Kind.PAGES,
            content_hash=digest,
            status=PreviewRender.Status.DONE,
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if render is not None:
        return Reference("preview", render.page_count, list(render.chapters or []), list(render.checks or []))
    live = live_of(job.book_id)
    if live is None or live.setup_hash != setup_hash(job.setup):
        return None
    versions = {
        str(item.get("id")): item.get("version")
        for item in live.chapters or []
        if isinstance(item, dict) and item.get("id")
    }
    if versions != dict(printed if printed is not None else printed_chapters(job)):
        return None
    return Reference("live", live.page_count, list(live.chapters or []), layout_checks(job.book_id))


def checks_row(book_id: int, checks: list) -> dict | None:
    """«3 ملاحظات على الصفحات» (warn, → «الفصول») for the page checks of the pages the file has, a missing
    face left out (reported on its own); None without any."""
    items = [item for item in checks if isinstance(item, dict) and item.get("code") != "missing_font"]
    if not items:
        return None
    message = f"{ar_count(len(items), CHECKS)} على الصفحات"
    return row("page_checks", WARN, message, action("الفصول", book_id, "chapters"))


def layout_mismatch(page_count: int, chapters: list[dict], reference: Reference | None) -> dict | None:
    """The `layout_mismatch` warning when the file's pages differ from the book page's (None: the same)."""
    if reference is None:
        return None
    if page_count != reference.page_count:
        message = PAGE_COUNT_MISMATCH.format(file=page_count, book=reference.page_count)
        return row("layout_mismatch", WARN, message)
    found = {item.get("id"): item for item in chapters}
    for item in reference.chapters:
        if not isinstance(item, dict):
            continue
        mine = found.get(item.get("id"))
        title = item.get("title") or ""
        if mine is None:
            return row("layout_mismatch", WARN, CHAPTER_MISSING.format(title=title))
        if (mine.get("first"), mine.get("last")) != (item.get("first"), item.get("last")):
            message = CHAPTER_MISMATCH.format(title=title, file=mine.get("first"), book=item.get("first"))
            return row("layout_mismatch", WARN, message)
    return None


# ====================================================================== the exporters


class PdfExporter:
    """What the print and the screen PDF share (see the module docstring); `kind` picks the output."""

    kind = "screen"
    format = "screen_pdf"
    label = "PDF للشاشة"
    extension = ".pdf"
    media_type = MEDIA_TYPE_PDF
    options: tuple[OptionSpec, ...] = ()

    @property
    def version(self) -> str:
        """The renderer's version (`nk-print-5/weasyprint-70.0`): a newer one marks older files stale."""
        from .engine import get_engine

        return get_engine().version

    def form(self, book, values: dict) -> dict:
        """The page's form block (none for the screen PDF)."""
        return {}

    def notes(self, book, setup: PageSetup) -> list[dict]:
        """The live layout's page checks and the faces not installed (Amiri stands in)."""
        rows: list[dict] = []
        checks = page_checks_row(book.pk)
        if checks is not None:
            rows.append(checks)
        rows += missing_font_rows(book.pk, setup, code="missing_font")
        return rows

    def output(self, job: ExportJob):
        """The `PdfOutput` of this export."""
        from .engine import PdfMetadata, PdfOutput

        metadata = PdfMetadata(subject=job.setup.detail("subtitle"), created=job.created)
        return PdfOutput(self.kind, metadata=metadata)

    def export(self, job: ExportJob, progress: Progress) -> ExportResult:
        """prepare → layout → footnotes → (relax) → write → check (see the module docstring)."""
        from .engine import RenderJob, get_engine

        progress("prepare")
        setup = job.setup
        fonts = F.resolve(setup.body_font, setup.latin_font, setup.heading_font)
        output = self.output(job)
        render_job = RenderJob(
            document=job.document,
            stylesheet=setup,
            title=job.title,
            author=job.author,
            scope="book",
            output=output,
        )
        if progress.cancelled():
            raise ExportCancelled
        rendered = get_engine().render(render_job, progress.cancelled, on_pass=progress)
        if progress.cancelled():
            raise ExportCancelled
        text_layer = fix_text_layer(rendered.pdf)  # Arabic ligatures copy in logical order (§4.7)
        progress("check")
        audit = audit_pdf(text_layer.data, fonts)
        errors = check_boxes(audit, setup, output)
        if audit.page_count != rendered.page_count:
            errors.append(f"the file has {audit.page_count} pages, the render {rendered.page_count}")
        if errors:
            raise InvalidExport("\n".join(errors))
        printed = {str(item["id"]): item.get("version") for item in rendered.chapters}
        reference = reference_layout(job, printed)
        warnings: list[dict] = []
        checks = checks_row(job.book_id, reference.checks) if reference is not None else None
        if checks is not None:  # the book page's checks, when its pages are the file's
            warnings.append(checks)
        warnings += missing_font_rows(job.book_id, setup, code="missing_font")
        warnings += foreign_font_rows(audit)
        mismatch = layout_mismatch(rendered.page_count, rendered.chapters, reference)
        if mismatch is not None:
            warnings.append(mismatch)
        stats = {
            "kind": output.kind,
            "passes": rendered.passes,
            "render_ms": rendered.duration_ms,
            "chapters": [
                {"id": item["id"], "first": item["first"], "last": item["last"]} for item in rendered.chapters
            ],
            "fonts": sorted(base_name(item["name"]) for item in audit.fonts),
            "foreign_fonts": {name: found["pages"] for name, found in audit.foreign.items()},
            "reference": reference.source if reference is not None else None,
            "bleed_mm": output.bleed_mm if output.is_print else 0,
            "crop_marks": bool(output.crop_marks) if output.is_print else False,
            "text_layer": text_layer.entries,
        }
        lines = [
            f"{self.format}: {rendered.page_count} pages, {rendered.passes} passes,"
            f" {rendered.duration_ms} ms",
            f"fonts: {', '.join(stats['fonts'])}",
            f"text layer: {text_layer.entries} Arabic ligature entries reversed"
            f" in {text_layer.changed_cmaps} of {text_layer.cmaps} ToUnicode maps",
        ]
        for name, found in audit.foreign.items():
            lines.append(f"foreign font {name} on pages {found['pages']}: {found['chars'][:40]!r}")
        if audit.type3 or audit.not_subset:
            lines.append(f"type 3 fonts {audit.type3}; not subset {audit.not_subset}")
        if reference is None:
            lines.append("no finished preview or current live layout to compare the pages with")
        return ExportResult(
            data=text_layer.data, page_count=rendered.page_count, warnings=warnings, stats=stats, log=lines
        )


class ScreenPdfExporter(PdfExporter):
    """«PDF للشاشة»: RGB, no bleed, the outline pane open, note links (no options)."""

    kind = "screen"
    format = "screen_pdf"
    label = "PDF للشاشة"


class PrintPdfExporter(PdfExporter):
    """«PDF للطباعة»: the trim as the TrimBox, bleed and crop marks as asked, pure black."""

    kind = "print"
    format = "print_pdf"
    label = "PDF للطباعة"
    options = PRINT_OPTIONS

    def form(self, book, values: dict) -> dict:
        """The page's print block: the bleed (a segmented choice with a hint) and the crop marks."""
        bleed = values.get("bleed_mm", 0)
        return {
            "bleed_mm": {
                "value": bleed if bleed in (value for value, *_rest in BLEEDS) else 0,
                "label": BLEED_LABEL,
                "choices": [
                    {"value": value, "label": short, "title": title, "hint": hint}
                    for value, short, title, hint in BLEEDS
                ],
            },
            "crop_marks": {
                "value": bool(values.get("crop_marks")),
                "label": CROP_MARKS_LABEL,
                "hint": CROP_MARKS_HINT,
            },
        }

    def output(self, job: ExportJob):
        from .engine import PdfMetadata, PdfOutput

        metadata = PdfMetadata(subject=job.setup.detail("subtitle"), created=job.created)
        options = job.options or {}
        return PdfOutput(
            "print",
            bleed_mm=float(options.get("bleed_mm") or 0),
            crop_marks=bool(options.get("crop_marks")),
            metadata=metadata,
        )


__all__ = [
    "PRINT_OPTIONS",
    "PdfAudit",
    "PdfExporter",
    "PrintPdfExporter",
    "Reference",
    "ScreenPdfExporter",
    "audit_pdf",
    "foreign_font_rows",
    "layout_mismatch",
    "reference_layout",
]
