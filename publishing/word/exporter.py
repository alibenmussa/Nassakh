"""`DocxExporter` (PHASE6_SPEC §5.1, §6.3, §7): the Word format of the export pipeline.

`export(job, progress)` runs prepare → (layout) → write → check: the book model of the exported text
(with the editorial marks when comments are asked for), the page plan for the contents field — only
when the file prints one: the live layout when it is current for the exported text (the chapters that
print, at their versions, with the same page setup), else one layout-only render —, the uncertain
words' readings (one query), the cover's picture when the book has a cover (D80: the preview's cover
rendered alone and rasterised at 300 dpi, `publishing.cover.export_raster`), the pure build, then the
schema and integrity checks — an invalid file fails the export (`InvalidExport`, «الملف الناتج غير سليم»).
`form` and `notes` feed the export page.
"""

from __future__ import annotations

import logging
from datetime import UTC

from django.conf import settings

from publishing import fonts as F
from publishing.exporters import (
    ExportCancelled,
    ExportJob,
    ExportResult,
    InvalidExport,
    OptionSpec,
    Progress,
)
from publishing.model import Book, PageSetup, book_model

from . import schema
from .faces import FacePlan
from .ooxml import MEDIA_TYPE_DOCX
from .options import (
    COMMENTS_HINT,
    COMMENTS_LABEL,
    COMMENTS_NONE_HINT,
    KASHIDA,
    KASHIDA_DEFAULT,
    KASHIDA_KEYS,
    KASHIDA_LABEL,
    WORD_VERSION,
    WordOptions,
    blank_verso_shows,
    fallback_name,
    kashida_choices,
    note,
)
from .styles import widow_control
from .writer import CoverPicture, DocMeta, PagePlan, build_docx, prints_contents

log = logging.getLogger(__name__)

DOCX_OPTIONS: tuple[OptionSpec, ...] = (
    OptionSpec(
        "kashida",
        "choice",
        KASHIDA_DEFAULT,
        choices=KASHIDA_KEYS,
        text={key: KASHIDA[key]["title"] for key in KASHIDA_KEYS},
    ),
    OptionSpec("comments", "bool", False, text={True: "تعليقات"}),
)


# ====================================================================== the page plan (§5.9)


def printed_versions(job: ExportJob, book: Book | None = None) -> dict[str, str]:
    """The exported document's chapter versions for the chapters that print: a chapter with no block
    to print (an empty front chapter) gets no pages, so a layout's chapter list never has it. `book` is
    the job's book model (built when not given)."""
    if book is None:
        book = book_model(job.document, job.setup, title=job.title, author=job.author)
    printed = {chapter.id for chapter in book.chapters if chapter.blocks}
    return {key: value for key, value in job.chapter_versions.items() if key in printed}


def current_live(job: ExportJob, book: Book | None = None) -> tuple[object, list[dict]] | None:
    """The live layout row and its pages when they are current for the exported text: every printed
    chapter's laid-out version equals the exported document's, the chapter set is the same, and the
    page setup's hash matches (the manuscript version alone is not enough, §5.9); None otherwise. The
    row is read once."""
    from publishing.preview import setup_hash
    from publishing.relayout import live_of, read_json

    live = live_of(job.book_id)
    if live is None or live.setup_hash != setup_hash(job.setup):
        return None
    laid_out = {
        str(item.get("id")): item.get("version")
        for item in live.chapters or []
        if isinstance(item, dict) and item.get("id")
    }
    if laid_out != printed_versions(job, book):
        return None
    pages = list((read_json(live.path) or {}).get("pages") or [])
    if not pages:
        return None
    return live, pages


def live_plan(job: ExportJob, book: Book | None = None) -> PagePlan | None:
    """The live layout as a plan when it is current for the exported text (`current_live`)."""
    found = current_live(job, book)
    if found is None:
        return None
    live, pages = found
    return PagePlan.from_pages(pages, "live", live.chapters)


def render_plan(job: ExportJob, progress: Progress) -> PagePlan:
    """One layout-only render of the exported document (the `layout` step)."""
    from publishing.engine import RenderJob, get_engine

    progress("layout")
    render_job = RenderJob(
        document=job.document,
        stylesheet=job.setup,
        title=job.title,
        author=job.author,
        scope="book",
        pdf=False,
    )
    rendered = get_engine().render(render_job, progress.cancelled)
    return PagePlan.from_pages(rendered.layout or [], "render", rendered.chapters)


def page_plan(job: ExportJob, progress: Progress, book: Book | None = None) -> PagePlan:
    """The preview's pages for the contents numbers: the live layout when current, else a render."""
    plan = live_plan(job, book)
    return plan if plan is not None else render_plan(job, progress)


# ====================================================================== the uncertain words (§5.11)


def readings_of(job: ExportJob) -> dict[tuple[str, str | None], list]:
    """`(block id, note id) → [uncertain.Word…]` of the exported document, each with its readings and
    scan page (one query for the OCR lines, `attach_readings`)."""
    from books.models import Book as BookRow
    from editor import uncertain

    words = uncertain.words_of(job.document)
    if not words:
        return {}
    book = BookRow.objects.filter(pk=job.book_id).first()
    uncertain.attach_readings(words, uncertain.normalizer(book) if book is not None else None)
    grouped: dict[tuple[str, str | None], list] = {}
    for word in words:
        grouped.setdefault((word.block, word.note), []).append(word)
    return grouped


# ====================================================================== notes (§7)


def static_notes(
    setup: PageSetup,
    fonts: F.ResolvedFonts,
    *,
    has_contents: bool,
    layout_current: bool,
    book_id: int | None = None,
) -> list[dict]:
    """The known differences of a Word file that follow from the setup and the faces alone
    (`has_contents`: the file prints the contents field; `layout_current`: its page numbers come from
    the live layout, so no layout runs first)."""
    plan = FacePlan(fonts)
    rows: list[dict] = []
    embedded = plan.embedded_faces()
    if any(not F.is_org_key(face.key) for face in embedded):
        rows.append(note("font_embedded"))
    for face in embedded:  # D98: an organisation's face its licence lets Word carry
        if F.is_org_key(face.key):
            rows.append(note("font_embedded_org", name=face.name))
    for face in plan.not_embedded():
        reason = F.word_refusal(face)
        if reason:
            rows.append(note("font_not_embedded_org", name=face.name, reason=reason))
        else:
            rows.append(note("font_not_embedded", name=face.name))
    for missing in fonts.missing:
        row = note(
            "font_removed" if missing.get("removed") else "font_missing",
            name=missing.get("name") or missing.get("key") or "",
            fallback=fallback_name(missing.get("fallback")),
        )
        if book_id is not None:
            row["action"] = {"label": "الخطوط", "url": f"/books/{book_id}/layout/?tab=format"}
        rows.append(row)
    if has_contents:
        rows.append(note("toc_update"))
        if not layout_current:  # the layout runs only for the contents' page numbers
            rows.append(note("layout_first"))
    shown = blank_verso_shows(setup)
    if shown:
        rows.append(note("blank_versos", what=shown))
    if setup.print_source_pages:
        rows.append(note("source_pages"))
    _on, approximate = widow_control(setup)
    if approximate:
        rows.append(note("widows_approx", n=f"{setup.widows}/{setup.orphans}"))
    return rows


def _prints_contents(row, setup: PageSetup, document: dict | None) -> bool:
    """True when the book's Word file prints a contents field (`writer.prints_contents` on the book
    model: the setup asks for one and there is a chapter or section title)."""
    if not setup.contents or not document:
        return False
    model = book_model(document, setup, title=row.title, author=row.author)
    return prints_contents(model)


# ====================================================================== the exporter


class DocxExporter:
    """The Word format (see the module docstring)."""

    format = "docx"
    label = "Word"
    extension = ".docx"
    media_type = MEDIA_TYPE_DOCX
    options = DOCX_OPTIONS
    version = WORD_VERSION

    def describe(self, options: dict) -> str:
        """«كشيدة خفيفة · تعليقات» for the history."""
        try:
            return WordOptions.parse(options).text()
        except ValueError:
            return ""

    def form(self, book, values: dict) -> dict:
        """The page's Word form block (§3.2): the kashida choice and the comments checkbox with the
        number of uncertain words it would comment."""
        from publishing.readiness import uncertain_counts

        available = uncertain_counts(book).total
        return {
            "kashida": {
                "value": values.get("kashida", KASHIDA_DEFAULT),
                "label": KASHIDA_LABEL,
                "choices": kashida_choices(),
            },
            "comments": {
                "value": bool(values.get("comments")) and available > 0,
                "available": available,
                "label": COMMENTS_LABEL,
                "hint": COMMENTS_HINT if available else COMMENTS_NONE_HINT,
            },
        }

    def notes(self, book, setup: PageSetup) -> list[dict]:
        """The known differences of this book's Word file (§7's table), and a cover picture short of
        300 dpi (`cover_resolution`, info)."""
        from editor.models import Manuscript
        from publishing.readiness import INFO, cover_resolution_row, layout_is_current

        manuscript = Manuscript.objects.filter(book_id=book.pk).only("document", "version").first()
        document = manuscript.document if manuscript is not None else None
        fonts = F.resolve(setup.body_font, setup.latin_font, setup.heading_font)
        current = layout_is_current(book, setup, manuscript.version if manuscript is not None else None)
        rows = static_notes(
            setup,
            fonts,
            has_contents=_prints_contents(book, setup, document),
            layout_current=current,
            book_id=book.pk,
        )
        resolution = cover_resolution_row(book.pk, setup, INFO)
        return rows + ([resolution] if resolution is not None else [])

    def export(self, job: ExportJob, progress: Progress) -> ExportResult:
        """prepare → (layout) → write → check (see the module docstring)."""
        options = WordOptions.parse(job.options)
        progress("prepare")
        book = book_model(
            job.document, job.setup, title=job.title, author=job.author, editorial=options.comments
        )
        setup = book.setup
        fonts = F.resolve(setup.body_font, setup.latin_font, setup.heading_font)
        readings = readings_of(job) if options.comments else None
        if progress.cancelled():
            raise ExportCancelled
        contents = prints_contents(book)
        plan = page_plan(job, progress, book) if contents else None  # only the contents need page numbers
        if progress.cancelled():
            raise ExportCancelled
        progress("write")
        created = job.created
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        meta = DocMeta(
            now=created.astimezone(UTC),
            book_id=job.book_id,
            manuscript_version=job.manuscript_version,
            export_id=job.export_id,
        )
        cover = cover_picture(book, fonts)
        if progress.cancelled():
            raise ExportCancelled
        result = build_docx(book, fonts, options, plan=plan, readings=readings, meta=meta, cover=cover)
        if progress.cancelled():
            raise ExportCancelled
        progress("check")
        validate = bool(settings.NASSAKH.get("EXPORT_VALIDATE", True))
        errors = schema.check(result.data, schema=validate)
        if errors:
            raise InvalidExport("\n".join(errors[:30]))
        warnings = static_notes(
            setup,
            fonts,
            has_contents=contents,
            layout_current=plan is None or plan.source == "live",
            book_id=job.book_id,
        )
        embedded = ("font_embedded", "font_embedded_org")
        warnings = [row for row in warnings if row["code"] not in embedded] + list(result.warnings)
        stats = dict(result.stats)
        stats["validated"] = validate
        log_lines = list(result.log)
        if plan is not None:
            log_lines.append(f"plan {plan.source}: {plan.page_count} pages, {len(plan.headings)} headings")
        else:
            log_lines.append("no contents field: no page plan")
        return ExportResult(data=result.data, page_count=None, warnings=warnings, stats=stats, log=log_lines)


def cover_picture(book: Book, fonts: F.ResolvedFonts) -> CoverPicture | None:
    """The book's cover as the Word file's first page (D80): the preview's cover rendered alone and
    rasterised at 300 dpi (JPEG q90 for a picture, PNG otherwise); None when the book has no cover."""
    from publishing.cover import WORD_DPI, export_raster

    if book.front.cover is None:
        return None
    data, media_type = export_raster(book, fonts, dpi=WORD_DPI)
    return CoverPicture(data, media_type, book.setup.width_mm, book.setup.height_mm)


def build_book(job: ExportJob, options: WordOptions, *, chapter_ids=None, readings=None, **kwargs):
    """The pure build of a job's book (the harness: one chapter alone with `chapter_ids`, another
    compatibility mode…): `kwargs` go to `build_docx`."""
    book = book_model(
        job.document,
        job.setup,
        title=job.title,
        author=job.author,
        chapter_ids=chapter_ids,
        editorial=options.comments,
    )
    fonts = F.resolve(book.setup.body_font, book.setup.latin_font, book.setup.heading_font)
    created = job.created if job.created.tzinfo else job.created.replace(tzinfo=UTC)
    meta = DocMeta(
        now=created.astimezone(UTC), book_id=job.book_id, manuscript_version=job.manuscript_version
    )
    return build_docx(book, fonts, options, meta=meta, readings=readings, **kwargs)


__all__ = [
    "DOCX_OPTIONS",
    "Book",
    "DocxExporter",
    "build_book",
    "cover_picture",
    "current_live",
    "page_plan",
    "printed_versions",
    "readings_of",
    "static_notes",
]
