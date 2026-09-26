"""Page previews (PHASE5_SPEC §2, D44): one row per render of the book or of one chapter, cached by hash;
the book's live layout (D47): the pages the book page draws now; exports (PHASE6_SPEC §6.1, D58): one row
per exported file, the history of the export page."""

from __future__ import annotations

from pathlib import PurePosixPath

from django.conf import settings
from django.db import models

from books.models import Book


class PreviewRender(models.Model):
    """A render of the book (or of one chapter) into a PDF and page images.

    `content_hash` identifies what was rendered (the document or the chapter, the stylesheet, the font
    files, the renderer version, the chapter's first page): a request for a hash that is already done
    reuses it. `chapters` is `[{id, title, first, last}]` (page numbers as printed); `folder` the media
    path of `page-0001.webp`, `page-0001-2x.webp`, … and the PDF; `first_page` the printed number of the
    first page (a chapter rendered alone starts where it starts in the last full render).

    D47: every render also writes `layout.json` (the pages' layout, `publishing.layout`) and its `checks`.
    `kind` `layout` is a fast re-layout of a chapter (`publishing.relayout`): layout only, its page images
    follow in a low-priority task (`images`); `result` holds what it changed in the live layout (the
    replaced page range, the delta, the blank pages…); `version` is the manuscript version rendered.
    """

    class Kind(models.TextChoices):
        PAGES = "pages", "صفحات"
        LAYOUT = "layout", "إعادة ترتيب"

    class Scope(models.TextChoices):
        BOOK = "book", "الكتاب"
        CHAPTER = "chapter", "فصل"

    # Pagination is «ترتيب الصفحات» (D77): a running render reads «قيد الترتيب» (an Export: «قيد الإخراج»).
    class Status(models.TextChoices):
        QUEUED = "queued", "في الانتظار"
        RUNNING = "running", "قيد الترتيب"
        DONE = "done", "اكتمل"
        ERROR = "error", "خطأ"
        CANCELLED = "cancelled", "أُلغي"

    book = models.ForeignKey(
        Book, verbose_name="الكتاب", on_delete=models.CASCADE, related_name="preview_renders"
    )
    scope = models.CharField("النطاق", max_length=10, choices=Scope.choices, default=Scope.BOOK)
    kind = models.CharField("النوع", max_length=10, choices=Kind.choices, default=Kind.PAGES)
    chapter_id = models.CharField("الفصل", max_length=64, blank=True)
    content_hash = models.CharField("بصمة المحتوى", max_length=64, db_index=True)
    status = models.CharField("الحالة", max_length=10, choices=Status.choices, default=Status.QUEUED)
    page_count = models.PositiveIntegerField("عدد الصفحات", default=0)
    first_page = models.PositiveIntegerField("رقم أول صفحة", default=1)
    chapters = models.JSONField("الفصول", default=list, blank=True)
    folder = models.CharField("المجلد", max_length=255, blank=True)
    passes = models.PositiveSmallIntegerField("مرات الإخراج", default=0)
    images = models.BooleanField("صور الصفحات جاهزة", default=True)
    checks = models.JSONField("ملاحظات الصفحات", default=list, blank=True)
    result = models.JSONField("نتيجة إعادة الترتيب", default=dict, blank=True)
    version = models.PositiveIntegerField("إصدار المخطوطة", default=0)
    duration_ms = models.PositiveIntegerField("المدة (مللي ثانية)", default=0)
    error = models.TextField("الخطأ", blank=True)
    task_id = models.CharField("معرّف المهمة", max_length=64, blank=True)
    created_at = models.DateTimeField("أُنشئ في", auto_now_add=True)
    finished_at = models.DateTimeField("انتهى في", null=True, blank=True)

    class Meta:
        verbose_name = "معاينة صفحات"
        verbose_name_plural = "معاينات الصفحات"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["book", "scope", "chapter_id", "-created_at"], name="preview_scope_latest"),
            models.Index(fields=["book", "kind", "chapter_id", "-created_at"], name="preview_kind_latest"),
        ]

    def __str__(self) -> str:
        where = f"chapter {self.chapter_id}" if self.scope == self.Scope.CHAPTER else "book"
        return f"preview {self.pk} of book {self.book_id} {where} ({self.status})"

    @property
    def is_active(self) -> bool:
        """True while the render waits in the queue or runs."""
        return self.status in (self.Status.QUEUED, self.Status.RUNNING)


class LiveLayout(models.Model):
    """The book's current page layout (D47): what the book page draws and the next re-layout builds on.

    `path` is the media path of the pages' JSON (`publishing.layout` pages plus each page's image
    reference): a finished book render's `layout.json`, or a revision written by a chapter re-layout
    (`books/<id>/layout/live-<revision>.json`, one file per revision, never rewritten). `revision` grows
    on every change; `chapters` is `[{id, title, first, last, version}]` (printed pages, the chapter
    version laid out); `manuscript_version` the newest manuscript version in it; `setup_hash` the page
    setup it was laid out with (a render with another setup replaces it).
    """

    book = models.OneToOneField(
        Book, verbose_name="الكتاب", on_delete=models.CASCADE, related_name="live_layout"
    )
    revision = models.PositiveIntegerField("المراجعة", default=0)
    path = models.CharField("الملف", max_length=255, blank=True)
    base = models.ForeignKey(
        PreviewRender,
        verbose_name="إخراج الكتاب الأساس",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    page_count = models.PositiveIntegerField("عدد الصفحات", default=0)
    chapters = models.JSONField("الفصول", default=list, blank=True)
    manuscript_version = models.PositiveIntegerField("إصدار المخطوطة", default=0)
    setup_hash = models.CharField("بصمة التنسيق", max_length=64, blank=True)
    updated_at = models.DateTimeField("حُدّث في", auto_now=True)

    class Meta:
        verbose_name = "ترتيب صفحات الكتاب"
        verbose_name_plural = "ترتيبات صفحات الكتب"

    def __str__(self) -> str:
        return f"live layout of book {self.book_id} r{self.revision} ({self.page_count} pages)"


def export_path(instance: Export, filename: str) -> str:
    """Media path of an export's file: `books/<id>/exports/<pk>.<ext>` (the row exists before its file;
    the Arabic file name is `filename`, used only for the download)."""
    extension = PurePosixPath(filename).suffix.lower() or ".bin"
    return f"books/{instance.book_id}/exports/{instance.pk}{extension}"


class Export(models.Model):
    """One export of the book into a file (D58): Word, PDF for print or for screen, EPUB.

    `options` are the normalised options (every default filled in); `inputs` what the task read when it
    started (`{stylesheet, title, author, digit_style, chapters: {id: version}}`), with the manuscript
    version it read (`manuscript_version`), the hash of the page setup and faces (`stylesheet_hash`,
    without the engine version) and the renderer's version (`renderer`) — what tells a file made from an
    older text, stylesheet or renderer (`publishing.exports.stale_reasons`). `progress` is `{step, done,
    total}`; `warnings` `[{code, level, message}]`; `error` the Arabic headline, then the technical
    `Type: message` line; `log` the exporter's technical lines. `page_count` is null for Word (Word lays
    out its own pages). `updated_at` is set explicitly on every queryset `update()`.

    At most one export per book and format is queued or running (a partial unique constraint).
    """

    class Format(models.TextChoices):
        DOCX = "docx", "Word"
        PRINT_PDF = "print_pdf", "PDF للطباعة"
        SCREEN_PDF = "screen_pdf", "PDF للشاشة"
        EPUB = "epub", "EPUB"

    class Status(models.TextChoices):
        QUEUED = "queued", "في الانتظار"
        RUNNING = "running", "قيد الإخراج"
        DONE = "done", "اكتمل"
        ERROR = "error", "خطأ"
        CANCELLED = "cancelled", "أُلغي"

    ACTIVE = (Status.QUEUED, Status.RUNNING)
    FINAL = (Status.DONE, Status.ERROR, Status.CANCELLED)

    book = models.ForeignKey(Book, verbose_name="الكتاب", on_delete=models.CASCADE, related_name="exports")
    format = models.CharField("الصيغة", max_length=12, choices=Format.choices)
    status = models.CharField("الحالة", max_length=10, choices=Status.choices, default=Status.QUEUED)
    options = models.JSONField("الخيارات", default=dict, blank=True)
    inputs = models.JSONField("المدخلات", default=dict, blank=True)
    manuscript_version = models.PositiveIntegerField("إصدار المخطوطة", null=True, blank=True)
    stylesheet_hash = models.CharField("بصمة التنسيق", max_length=24, blank=True)
    renderer = models.CharField("المُخرِج", max_length=64, blank=True)
    file = models.FileField("الملف", upload_to=export_path, max_length=255, blank=True)
    filename = models.CharField("اسم الملف", max_length=255, blank=True)
    size_bytes = models.BigIntegerField("الحجم (بايت)", default=0)
    page_count = models.PositiveIntegerField("عدد الصفحات", null=True, blank=True)
    stats = models.JSONField("الإحصاءات", default=dict, blank=True)
    progress = models.JSONField("التقدّم", default=dict, blank=True)
    warnings = models.JSONField("الملاحظات", default=list, blank=True)
    log = models.TextField("السجل", blank=True)
    error = models.TextField("الخطأ", blank=True)
    duration_ms = models.IntegerField("المدة (مللي ثانية)", default=0)
    task_id = models.CharField("معرّف المهمة", max_length=64, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="أخرجه",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField("أُنشئ في", auto_now_add=True)
    started_at = models.DateTimeField("بدأ في", null=True, blank=True)
    finished_at = models.DateTimeField("انتهى في", null=True, blank=True)
    updated_at = models.DateTimeField("حُدّث في", auto_now=True)

    class Meta:
        verbose_name = "إخراج"
        verbose_name_plural = "الإخراجات"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["book", "format", "-created_at"], name="export_book_format_latest")]
        constraints = [
            models.UniqueConstraint(
                fields=["book", "format"],
                condition=models.Q(status__in=["queued", "running"]),
                name="export_one_active_per_format",
            )
        ]

    def __str__(self) -> str:
        return f"export {self.pk} of book {self.book_id} as {self.format} ({self.status})"

    @property
    def is_active(self) -> bool:
        """True while the export waits in the queue or runs."""
        return self.status in self.ACTIVE
