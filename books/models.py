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
# The same, for a book in «التخطيط» (D64): every page failed its preparation.
ALL_PAGES_FAILED_LAYOUT = (
    "تعذّر تجهيز كل صفحات الكتاب. افتح إحدى الصفحات لمعرفة السبب، أو أعد المحاولة من الزر أعلى الصفحة."
)
# Error messages that `refresh_status` owns (re-derived); any other `error` is an ingest failure.
ALL_PAGES_FAILED_MESSAGES: tuple[str, ...] = (ALL_PAGES_FAILED, ALL_PAGES_FAILED_LAYOUT)


class Book(models.Model):
    """A scanned (or born-digital) PDF and the ingest options that turn it into pages."""

    class Status(models.TextChoices):
        UPLOADED = "uploaded", "مرفوع"
        PROCESSING = "processing", "قيد التخطيط"
        NEEDS_GUIDES = "needs_guides", "تم التخطيط"
        OCR = "ocr", "قيد المعالجة"
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
    # D107: the volume of a multi-volume work, for the citation of a page («ج 2، ص 45»); the editor, publisher
    # and edition come from the book details of «التنسيق» (`StyleSheet.front_matter["fields"]`).
    volume = models.CharField("الجزء", max_length=40, blank=True)
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
    # Off by default (owner review 2026-10-03, item 20): the embedded text of Arabic PDFs is often garbled,
    # so the text comes from OCR. Honoured only while `NASSAKH["TEXT_LAYER"]` is on (`uses_text_layer`).
    use_text_layer = models.BooleanField("استخدام الطبقة النصية", default=False)
    digit_style = models.CharField(
        "نمط الأرقام", max_length=20, default=DigitStyle.WESTERN, choices=DigitStyle.choices
    )
    # Assembly options and overrides (D38): footnote_numbering, include_unreviewed, strip_tatweel,
    # seams {"<page number>": "join" | "split"}, dismissed_suggestions [block ids]. Read through
    # `assembly.pipeline.normalize_settings`.
    assembly_settings = models.JSONField("إعدادات التجميع", default=dict, blank=True)

    status = models.CharField("الحالة", max_length=20, choices=Status.choices, default=Status.UPLOADED)
    error_message = models.TextField("رسالة الخطأ", blank=True)
    # The pause between «التخطيط» and «المعالجة» (D64): `create_book` sets it, `start_ocr` clears it,
    # nothing else writes it. False (the default: every older book and every fixture) is today's path.
    awaits_ocr_start = models.BooleanField("بانتظار «بدء المعالجة»", default=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="أنشأه",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="books",
    )
    # D98: the organisation the book belongs to (its fonts and format templates); `create_book` sets the
    # creator's, the migration gave every older book the first organisation.
    organization = models.ForeignKey(
        "accounts.Organization",
        verbose_name="المؤسسة",
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

        An empty book and an ingest failure (`error` with any other message than the
        `ALL_PAGES_FAILED_MESSAGES`) are left alone. A book in «التخطيط» (`awaits_ocr_start`, D64)
        is `processing` while a page is still `uploaded`, `needs_guides` («تم التخطيط») once the
        pages are prepared, and `error` with `ALL_PAGES_FAILED_LAYOUT` when every page failed: nothing
        is read before «بدء المعالجة». Otherwise (today's path) `needs_guides` is kept while the
        guides are still the automatic proposal and a page waits at `preprocessed` (a book an older
        version parked there); once no page has pipeline work left (`uploaded`, `preprocessed`,
        `layout_done`) the book settles (`_settled`). It never stays `processing`/`ocr` with nothing
        left to run.
        """
        counts = self.progress()
        total = sum(counts.values())
        if total == 0:
            return self.status
        if self.status == self.Status.ERROR and self.error_message not in ALL_PAGES_FAILED_MESSAGES:
            return self.status

        ps = Page.Status
        n_done = counts[ps.OCR_DONE] + counts[ps.REVIEWED] + counts[ps.ASSEMBLED]
        pending = counts[ps.UPLOADED] + counts[ps.PREPROCESSED] + counts[ps.LAYOUT_DONE]

        if self.awaits_ocr_start:  # «التخطيط» (D64): nothing is read before «بدء المعالجة»
            if counts[ps.UPLOADED]:
                new_status = self.Status.PROCESSING  # «قيد التخطيط»: pages are still being prepared
            elif counts[ps.PREPROCESSED] or counts[ps.LAYOUT_DONE]:
                new_status = self.Status.NEEDS_GUIDES  # «تم التخطيط»: waits for «بدء المعالجة»
            else:
                new_status = self._settled(counts, total)  # every page failed → error
        elif (
            self.status == self.Status.NEEDS_GUIDES
            and counts[ps.PREPROCESSED]
            and not self._has_manual_guides()
        ):
            new_status = self.Status.NEEDS_GUIDES
        elif pending == 0:
            new_status = self._settled(counts, total)
        elif counts[ps.LAYOUT_DONE] or n_done:
            new_status = self.Status.OCR
        elif counts[ps.UPLOADED]:
            new_status = self.Status.PROCESSING
        elif self.status == self.Status.NEEDS_GUIDES:
            new_status = self.Status.NEEDS_GUIDES
        else:
            new_status = self.Status.PROCESSING

        failed = ALL_PAGES_FAILED_LAYOUT if self.awaits_ocr_start else ALL_PAGES_FAILED
        message = failed if new_status == self.Status.ERROR else ""
        if new_status != self.status or (new_status == self.Status.ERROR and message != self.error_message):
            self.status = new_status
            self.error_message = message
            if save:
                self.save(update_fields=["status", "error_message", "updated_at"])
        return self.status

    def _settled(self, counts: dict[str, int], total: int) -> str:
        """Status of a book with no pipeline work left: `error` when every page failed, else `assembled`
        when every page is, `reviewing` once a page is approved, `ready_for_review` otherwise."""
        ps = Page.Status
        if counts[ps.ERROR] == total:
            return self.Status.ERROR
        if counts[ps.ASSEMBLED] == total:
            return self.Status.ASSEMBLED
        if counts[ps.REVIEWED] + counts[ps.ASSEMBLED] > 0:
            return self.Status.REVIEWING
        return self.Status.READY_FOR_REVIEW

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
        PREPROCESSED = "preprocessed", "مُجهَّزة"
        LAYOUT_DONE = "layout_done", "بانتظار التعرّف"
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
    # The pipeline run that holds the page (`books.runs`): one run per page at a time; '' when none.
    run_token = models.CharField("تشغيل المعالجة الجاري", max_length=32, blank=True, default="")
    run_claimed_at = models.DateTimeField("بدأ تشغيل المعالجة في", null=True, blank=True)
    attention_flags = models.JSONField("إشارات الانتباه", default=list, blank=True)
    is_excluded = models.BooleanField("مستثناة", default=False)

    text_layer_text = models.TextField("نص الطبقة النصية", blank=True)
    guides_override = models.JSONField("تخطيط خاص بالصفحة", null=True, blank=True)
    # The number printed on the page (Western digits), read by OCR; metadata only, never in the text.
    printed_number = models.CharField("الرقم المطبوع", max_length=20, blank=True)

    provisional_text = models.TextField("النص المبدئي", blank=True)
    final_text = models.TextField("النص النهائي", blank=True)
    text_state = models.CharField(
        "حالة النص", max_length=20, choices=TextState.choices, default=TextState.NONE
    )

    # Open review items (D73): unresolved words, one per insertion group, plus the open suggestions
    # (`ocr.TextGap`); kept by finalize_page and review through `ocr.services.page_open_items`.
    n_unresolved = models.PositiveIntegerField("كلمات غير محسومة", default=0)
    # How the page was read, written by finalize_page (D73): {"readers": "two" | "one" | "tesseract",
    # "partial": bool, "groups": int, "gaps": int}. `{}` for pages read before 7b.
    reading = models.JSONField("القراءة", default=dict, blank=True)

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
