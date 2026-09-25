"""Page previews (PHASE5_SPEC §2, D44): one row per render of the book or of one chapter, cached by hash."""

from __future__ import annotations

from django.db import models

from books.models import Book


class PreviewRender(models.Model):
    """A render of the book (or of one chapter) into a PDF and page images.

    `content_hash` identifies what was rendered (the document or the chapter, the stylesheet, the font
    files, the renderer version, the chapter's first page): a request for a hash that is already done
    reuses it. `chapters` is `[{id, title, first, last}]` (page numbers as printed); `folder` the media
    path of `page-0001.webp`, `page-0001-2x.webp`, … and the PDF; `first_page` the printed number of the
    first page (a chapter rendered alone starts where it starts in the last full render).
    """

    class Scope(models.TextChoices):
        BOOK = "book", "الكتاب"
        CHAPTER = "chapter", "فصل"

    class Status(models.TextChoices):
        QUEUED = "queued", "في الانتظار"
        RUNNING = "running", "قيد الإخراج"
        DONE = "done", "اكتمل"
        ERROR = "error", "خطأ"
        CANCELLED = "cancelled", "أُلغي"

    book = models.ForeignKey(
        Book, verbose_name="الكتاب", on_delete=models.CASCADE, related_name="preview_renders"
    )
    scope = models.CharField("النطاق", max_length=10, choices=Scope.choices, default=Scope.BOOK)
    chapter_id = models.CharField("الفصل", max_length=64, blank=True)
    content_hash = models.CharField("بصمة المحتوى", max_length=64, db_index=True)
    status = models.CharField("الحالة", max_length=10, choices=Status.choices, default=Status.QUEUED)
    page_count = models.PositiveIntegerField("عدد الصفحات", default=0)
    first_page = models.PositiveIntegerField("رقم أول صفحة", default=1)
    chapters = models.JSONField("الفصول", default=list, blank=True)
    folder = models.CharField("المجلد", max_length=255, blank=True)
    passes = models.PositiveSmallIntegerField("مرات الإخراج", default=0)
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
        ]

    def __str__(self) -> str:
        where = f"chapter {self.chapter_id}" if self.scope == self.Scope.CHAPTER else "book"
        return f"preview {self.pk} of book {self.book_id} {where} ({self.status})"

    @property
    def is_active(self) -> bool:
        """True while the render waits in the queue or runs."""
        return self.status in (self.Status.QUEUED, self.Status.RUNNING)
