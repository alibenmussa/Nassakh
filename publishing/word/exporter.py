"""`DocxExporter` (PHASE6_SPEC §5.1, §6.3, §7): the Word format of the export pipeline.

`export(job, progress)` runs prepare → (layout) → write → check: the book model of the exported text
(with the editorial marks when comments are asked for), the page plan for the contents field (the live
layout when it is current for the exported text, else one layout-only render), the uncertain words'
readings (one query), the pure build, then the schema and integrity checks — an invalid file fails the
export (`InvalidExport`, «الملف الناتج غير سليم»). `form` and `notes` feed the export page.
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
    kashida_choices,
    note,
)
from .styles import widow_control
from .writer import DocMeta, PagePlan, build_docx

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


def live_plan(job: ExportJob) -> PagePlan | None:
    """The live layout as a plan when it is current for the exported text: every chapter's laid-out
    version equals the exported document's, the chapter set is the same, and the page setup's hash
    matches (the manuscript version alone is not enough, §5.9)."""
    from publishing.preview import setup_hash
    from publishing.relayout import live_of, live_pages

    live = live_of(job.book_id)
    if live is None or live.setup_hash != setup_hash(job.setup):
        return None
    laid_out = {
        str(item.get("id")): item.get("version")
        for item in live.chapters or []
        if isinstance(item, dict) and item.get("id")
    }
    if laid_out != dict(job.chapter_versions):
        return None
    pages = live_pages(job.book_id)
    if not pages:
        return None
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


def page_plan(job: ExportJob, progress: Progress) -> PagePlan:
    """The preview's pages for the contents numbers: the live layout when current, else a render."""
    plan = live_plan(job)
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
    """The known differences of a Word file that follow from the setup and the faces alone."""
    plan = FacePlan(fonts)
    rows: list[dict] = []
    if plan.embedded_faces():
        rows.append(note("font_embedded"))
    for face in plan.not_embedded():
        rows.append(note("font_not_embedded", name=face.name))
    for missing in fonts.missing:
        row = note("font_missing", name=missing.get("name") or missing.get("key") or "")
        if book_id is not None:
            row["action"] = {"label": "الخطوط", "url": f"/books/{book_id}/layout/?tab=format"}
        rows.append(row)
    if has_contents:
        rows.append(note("toc_update"))
    if not layout_current:
        rows.append(note("layout_first"))
    if setup.chapter_opening == "recto" and setup.page_number != "none":
        rows.append(note("blank_versos"))
    if setup.print_source_pages:
        rows.append(note("source_pages"))
    _on, approximate = widow_control(setup)
    if approximate:
        rows.append(note("widows_approx", n=f"{setup.widows}/{setup.orphans}"))
    return rows


def _has_contents(setup: PageSetup, document: dict | None) -> bool:
    """True when the book prints a contents page: the setup asks for one and there is a heading."""
    from editor import document as doc

    if not setup.contents or not document:
        return False
    return any(chapter.kind == "chapter" for chapter in doc.chapters_of(document))


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
        """The known differences of this book's Word file (§7's table)."""
        from editor.models import Manuscript
        from publishing.readiness import layout_is_current

        manuscript = Manuscript.objects.filter(book_id=book.pk).only("document", "version").first()
        document = manuscript.document if manuscript is not None else None
        fonts = F.resolve(setup.body_font, setup.latin_font, setup.heading_font)
        current = layout_is_current(book, setup, manuscript.version if manuscript is not None else None)
        return static_notes(
            setup,
            fonts,
            has_contents=_has_contents(setup, document),
            layout_current=current,
            book_id=book.pk,
        )

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
        plan = page_plan(job, progress)
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
        result = build_docx(book, fonts, options, plan=plan, readings=readings, meta=meta)
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
            has_contents=book.front.contents and bool(book.contents()),
            layout_current=plan.source == "live",
            book_id=job.book_id,
        )
        warnings = [row for row in warnings if row["code"] != "font_embedded"] + list(result.warnings)
        stats = dict(result.stats)
        stats["validated"] = validate
        log_lines = list(result.log)
        log_lines.append(f"plan {plan.source}: {plan.page_count} pages, {len(plan.headings)} headings")
        return ExportResult(data=result.data, page_count=None, warnings=warnings, stats=stats, log=log_lines)


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


__all__ = ["DOCX_OPTIONS", "Book", "DocxExporter", "build_book", "page_plan", "readings_of", "static_notes"]
