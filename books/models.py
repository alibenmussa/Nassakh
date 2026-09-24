"""Book and Page: the unit of upload and the unit of processing."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Count

from core.storage import book_source_path, page_original_path


class Book(models.Model):
    """A scanned (or born-digital) PDF and the ingest options that turn it into pages."""

    class Status(models.TextChoices):
        UPLOADED = "uploaded", "مرفوع"
        PROCESSING = "processing", "قيد المعالجة"
        NEEDS_GUIDES = "needs_guides", "بانتظار ضبط الأدلة"
        OCR = "ocr", "قيد التعرّف على النص"
        READY_FOR_REVIEW = "ready_for_review", "جاهز للمراجعة"
        REVIEWING = "reviewing", "قيد المراجعة"
        ASSEMBLED = "assembled", "مُجمَّع"
        ERROR = "error", "خطأ"

    class PagesPerSheet(models.IntegerChoices):
        ONE = 1, "صفحة واحدة"
        TWO = 2, "صفحتان"

    class DigitStyle(models.TextChoices):
        WESTERN = "western", "أرقام غربية (0-9)"
        ARABIC_INDIC = "arabic_indic", "أرقام عربية هندية (٠-٩)"

    title = models.CharField("العنوان", max_length=300)
    author = models.CharField("المؤلف", max_length=300, blank=True)
    original_year = models.PositiveIntegerField("سنة النشر الأصلية", null=True, blank=True)
    notes = models.TextField("ملاحظات", blank=True)
    source_pdf = models.FileField("ملف PDF", upload_to=book_source_path)
    has_text_layer = models.BooleanField("يحوي طبقة نصية", null=True)
    source_page_count = models.PositiveIntegerField("عدد صفحات الملف", default=0)

    skip_first = models.PositiveSmallIntegerField("تجاوز الصفحات الأولى", default=0)
    skip_last = models.PositiveSmallIntegerField("تجاوز الصفحات الأخيرة", default=0)
    pages_per_sheet = models.PositiveSmallIntegerField(
        "صفحات في كل ورقة", default=PagesPerSheet.ONE, choices=PagesPerSheet.choices
    )
    split_ratio = models.FloatField("موضع القص", default=0.5)
    use_text_layer = models.BooleanField("استخدام الطبقة النصية", default=True)
    digit_style = models.CharField(
        "نمط الأرقام", max_length=20, default=DigitStyle.WESTERN, choices=DigitStyle.choices
    )

    status = models.CharField("الحالة", max_length=20, choices=Status.choices, default=Status.UPLOADED)
    error_message = models.TextField("رسالة الخطأ", blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="أنشأه",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="books",
    )
    created_at = models.DateTimeField("أُنشئ في", auto_now_add=True)
    updated_at = models.DateTimeField("عُدّل في", auto_now=True)

    class Meta:
        verbose_name = "كتاب"
        verbose_name_plural = "الكتب"
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return self.title

    # ------------------------------------------------------------ derived state

    @property
    def page_count(self) -> int:
        """Number of pages that are part of the book (excluded pages do not count)."""
        return self.pages.filter(is_excluded=False).count()

    def progress(self) -> dict[str, int]:
        """Count of non-excluded pages per `Page.Status` value (every status present, 0 when empty)."""
        counts = {status: 0 for status in Page.Status.values}
        rows = self.pages.filter(is_excluded=False).values("status").annotate(n=Count("id"))
        for row in rows:
            counts[row["status"]] = row["n"]
        return counts

    def refresh_status(self, save: bool = True) -> str:
        """Derive the book status from its pages and store it. Returns the (possibly unchanged) status.

        `error` (ingest failure) and an empty book are left alone. `needs_guides` is kept while
        every page is still waiting after preprocessing; it clears as soon as a page moves on.
        Pages in error keep the book from being complete but do not make the book itself `error`.
        """
        counts = self.progress()
        total = sum(counts.values())
        if total == 0 or self.status == self.Status.ERROR:
            return self.status

        ps = Page.Status
        errors = counts[ps.ERROR]
        n_done = counts[ps.OCR_DONE] + counts[ps.REVIEWED] + counts[ps.ASSEMBLED]

        if errors == total:
            return self.status
        if errors == 0 and n_done == total:
            if counts[ps.ASSEMBLED] == total:
                new_status = self.Status.ASSEMBLED
            elif counts[ps.REVIEWED] + counts[ps.ASSEMBLED] > 0:
                new_status = self.Status.REVIEWING
            else:
                new_status = self.Status.READY_FOR_REVIEW
        elif counts[ps.LAYOUT_DONE] or n_done:
            new_status = self.Status.OCR
        elif counts[ps.UPLOADED]:
            new_status = self.Status.PROCESSING
        elif self.status == self.Status.NEEDS_GUIDES:
            new_status = self.Status.NEEDS_GUIDES
        else:
            new_status = self.Status.PROCESSING

        if new_status != self.status:
            self.status = new_status
            if save:
                self.save(update_fields=["status", "updated_at"])
        return self.status


class Page(models.Model):
    """One book page: an original image plus everything the pipeline derives from it."""

    class Status(models.TextChoices):
        UPLOADED = "uploaded", "مرفوعة"
        PREPROCESSED = "preprocessed", "مُعالَجة"
        LAYOUT_DONE = "layout_done", "تم التخطيط"
        OCR_DONE = "ocr_done", "تم التعرّف"
        REVIEWED = "reviewed", "مُراجَعة"
        ASSEMBLED = "assembled", "مُجمَّعة"
        ERROR = "error", "خطأ"
        EXCLUDED = "excluded", "مستثناة"

    class SourceHalf(models.TextChoices):
        FULL = "full", "كاملة"
        RIGHT = "right", "النصف الأيمن"
        LEFT = "left", "النصف الأيسر"

    class TextState(models.TextChoices):
        NONE = "none", "لا نص"
        PROVISIONAL = "provisional", "نص مبدئي"
        FINAL = "final", "نص نهائي"

    book = models.ForeignKey(Book, verbose_name="الكتاب", on_delete=models.CASCADE, related_name="pages")
    number = models.PositiveIntegerField("رقم الصفحة")
    source_index = models.PositiveIntegerField("رقم الصفحة في الملف")
    source_half = models.CharField(
        "جزء الورقة", max_length=8, choices=SourceHalf.choices, default=SourceHalf.FULL
    )
    split_ratio_override = models.FloatField("موضع القص لهذه الصفحة", null=True, blank=True)
    original_image = models.FileField("الصورة الأصلية", upload_to=page_original_path, blank=True)
    width = models.PositiveIntegerField("العرض", default=0)
    height = models.PositiveIntegerField("الارتفاع", default=0)
    dpi = models.FloatField("الدقة", default=0)

    status = models.CharField("الحالة", max_length=20, choices=Status.choices, default=Status.UPLOADED)
    error_from = models.CharField("مرحلة الخطأ", max_length=40, blank=True)
    error_message = models.TextField("رسالة الخطأ", blank=True)
    task_id = models.CharField("معرّف المهمة", max_length=64, blank=True)
    attention_flags = models.JSONField("إشارات الانتباه", default=list, blank=True)
    is_excluded = models.BooleanField("مستثناة", default=False)

    text_layer_text = models.TextField("نص الطبقة النصية", blank=True)
    guides_override = models.JSONField("أدلة خاصة بالصفحة", null=True, blank=True)

    provisional_text = models.TextField("النص المبدئي", blank=True)
    final_text = models.TextField("النص النهائي", blank=True)
    text_state = models.CharField(
        "حالة النص", max_length=20, choices=TextState.choices, default=TextState.NONE
    )

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="راجعها",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_pages",
    )
    reviewed_at = models.DateTimeField("رُوجعت في", null=True, blank=True)

    class Meta:
        verbose_name = "صفحة"
        verbose_name_plural = "الصفحات"
        unique_together = [("book", "number")]
        ordering = ["book", "number"]

    def __str__(self) -> str:
        return f"{self.book_id}/{self.number}"

    # ------------------------------------------------------------ errors

    def set_error(self, stage: str, message: str) -> None:
        """Mark the page as failed in `stage` with an (Arabic, actionable) message and save."""
        self.status = self.Status.ERROR
        self.error_from = stage
        self.error_message = message
        self.save(update_fields=["status", "error_from", "error_message"])

    def clear_error(self) -> None:
        """Drop the error; a page that was in `error` falls back to the last stage it completed."""
        self.error_from = ""
        self.error_message = ""
        if self.status == self.Status.ERROR:
            self.status = self.Status.EXCLUDED if self.is_excluded else self._completed_status()
        self.save(update_fields=["status", "error_from", "error_message"])

    def _completed_status(self) -> str:
        """Status implied by the artefacts that exist: text → ocr_done, regions → layout_done, ..."""
        if self.text_state == self.TextState.FINAL:
            return self.Status.OCR_DONE
        if self.regions.exists():
            return self.Status.LAYOUT_DONE
        if Page.objects.filter(pk=self.pk, preprocess__isnull=False).exists():
            return self.Status.PREPROCESSED
        return self.Status.UPLOADED
