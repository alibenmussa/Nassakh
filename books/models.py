"""Book and Page: the unit of upload and the unit of processing."""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import models
from django.db.models import Count

from core.storage import book_source_path, page_original_path, page_scan_thumb_path

# Book error set by `refresh_status` when every page failed (unlike an ingest error it is re-derived).
ALL_PAGES_FAILED = (
    "تعذّرت معالجة كل صفحات الكتاب. افتح إحدى الصفحات لمعرفة السبب ثم أعد تشغيل مرحلتها، "
    "أو اضغط «بدء المعالجة» للبدء من جديد."
)


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

        An empty book and an ingest failure (`error` with any other message than
        `ALL_PAGES_FAILED`) are left alone. `needs_guides` is kept while the guides are still the
        automatic proposal and a page waits at `preprocessed` (only applying the guides moves those
        pages on). Once no page has pipeline work left (`uploaded`, `preprocessed`, `layout_done`)
        the book settles: `ready_for_review`/`reviewing`/`assembled` when at least one page is done
        (pages in error show in the attention list), `error` with `ALL_PAGES_FAILED` when every page
        failed. It never stays `processing`/`ocr` with nothing left to run.
        """
        counts = self.progress()
        total = sum(counts.values())
        if total == 0:
            return self.status
        if self.status == self.Status.ERROR and self.error_message != ALL_PAGES_FAILED:
            return self.status

        ps = Page.Status
        errors = counts[ps.ERROR]
        n_done = counts[ps.OCR_DONE] + counts[ps.REVIEWED] + counts[ps.ASSEMBLED]
        pending = counts[ps.UPLOADED] + counts[ps.PREPROCESSED] + counts[ps.LAYOUT_DONE]

        if (
            self.status == self.Status.NEEDS_GUIDES
            and counts[ps.PREPROCESSED]
            and not self._has_manual_guides()
        ):
            new_status = self.Status.NEEDS_GUIDES
        elif pending == 0:
            if errors == total:
                new_status = self.Status.ERROR
            elif counts[ps.ASSEMBLED] == total:
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
            self.error_message = ALL_PAGES_FAILED if new_status == self.Status.ERROR else ""
            if save:
                self.save(update_fields=["status", "error_message", "updated_at"])
        return self.status

    def _has_manual_guides(self) -> bool:
        """True when the owner set the layout guides by hand (the guides screen was applied)."""
        try:
            return self.guides.source == "manual"  # processing.models.LayoutGuides.Source.MANUAL
        except ObjectDoesNotExist:
            return False


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
    scan_thumbnail = models.FileField("مصغّرة المسح", upload_to=page_scan_thumb_path, blank=True)
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
    # The number printed on the page (Western digits), read by OCR; metadata only, never in the text.
    printed_number = models.CharField("الرقم المطبوع", max_length=20, blank=True)

    provisional_text = models.TextField("النص المبدئي", blank=True)
    final_text = models.TextField("النص النهائي", blank=True)
    text_state = models.CharField(
        "حالة النص", max_length=20, choices=TextState.choices, default=TextState.NONE
    )

    # Uncertain words not yet resolved (sum of `ocr.Line.n_low`), kept by finalize_page and review.
    n_unresolved = models.PositiveIntegerField("كلمات غير محسومة", default=0)

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
        """Status implied by the artefacts that exist: approval → reviewed, text → ocr_done, regions → ...

        `reviewed_at` is the approval marker: `approve_page` sets it and the paths that take a page
        out of `reviewed` (reopen, undo of the approval) clear it; stage re-runs refuse approved pages.
        """
        if self.text_state == self.TextState.FINAL:
            return self.Status.REVIEWED if self.reviewed_at is not None else self.Status.OCR_DONE
        if self.regions.exists():
            return self.Status.LAYOUT_DONE
        if Page.objects.filter(pk=self.pk, preprocess__isnull=False).exists():
            return self.Status.PREPROCESSED
        return self.Status.UPLOADED
