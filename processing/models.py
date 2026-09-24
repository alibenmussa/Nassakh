"""Preprocessing results, book-level layout guides and the regions derived from them.

All coordinates (`line_boxes`, `footnote_rule_y`, `footnote_block_y`, `page_number_box`, `Region.bbox`)
are in gray-image pixel space.
"""

from __future__ import annotations

from django.db import models

from books.models import Book, Page
from core.storage import page_derived_path


class Preprocess(models.Model):
    """Deskew / crop / clean outputs for one page, plus the parameters that produced them."""

    page = models.OneToOneField(
        Page, verbose_name="الصفحة", on_delete=models.CASCADE, related_name="preprocess"
    )

    angle = models.FloatField("زاوية التدوير", default=0)
    skew_confidence = models.FloatField("ثقة تقدير الانحراف", default=0)
    border_crop = models.JSONField("قص الحواف الداكنة", default=list, blank=True)
    crop_box = models.JSONField("صندوق القص", default=list, blank=True)
    bg_kernel = models.IntegerField("نواة تسوية الخلفية", default=0)
    nlm_h = models.IntegerField("قوة إزالة التشويش", default=6)
    sauvola_window = models.IntegerField("نافذة Sauvola", default=41)
    sauvola_k = models.FloatField("معامل Sauvola", default=0.2)
    is_manual = models.BooleanField("معدّلة يدويًا", default=False)
    auto_params = models.JSONField("القيم التلقائية", default=dict, blank=True)
    manual_params = models.JSONField("القيم اليدوية", default=dict, blank=True)

    gray_image = models.FileField("الصورة الرمادية", upload_to=page_derived_path, blank=True)
    bw_image = models.FileField("الصورة بالأبيض والأسود", upload_to=page_derived_path, blank=True)
    display_image = models.FileField("صورة العرض", upload_to=page_derived_path, blank=True)
    thumbnail = models.FileField("الصورة المصغّرة", upload_to=page_derived_path, blank=True)

    output_width = models.PositiveIntegerField("عرض الناتج", default=0)
    output_height = models.PositiveIntegerField("ارتفاع الناتج", default=0)
    line_boxes = models.JSONField("صناديق الأسطر", default=list, blank=True)
    median_line_height = models.FloatField("ارتفاع السطر الوسيط", default=0)
    n_lines = models.PositiveIntegerField("عدد الأسطر", default=0)
    footnote_rule_y = models.IntegerField("موضع خط الحاشية", null=True, blank=True)
    footnote_block_y = models.IntegerField("بداية كتلة الحاشية", null=True, blank=True)
    page_number_box = models.JSONField("موضع رقم الصفحة المكتشف", null=True, blank=True)
    edge_strips_removed = models.JSONField("أشرطة الحافة المُزالة", default=list, blank=True)

    created_at = models.DateTimeField("أُنشئت في", auto_now_add=True)
    updated_at = models.DateTimeField("عُدّلت في", auto_now=True)

    class Meta:
        verbose_name = "معالجة أولية"
        verbose_name_plural = "المعالجات الأولية"

    def __str__(self) -> str:
        return f"preprocess {self.page_id}"


class LayoutGuides(models.Model):
    """Book-level guide lines (ratios of the gray image height).

    Footnotes and page numbers are detected per page; the book's footnote line and page-number
    zone are a manual fallback, applied only when `source` is `manual` and nothing was detected on
    the page. The running-header cut is always manual.
    """

    class PageNumberZone(models.TextChoices):
        NONE = "none", "بدون"
        TOP = "top", "أعلى الصفحة"
        BOTTOM = "bottom", "أسفل الصفحة"

    class Source(models.TextChoices):
        AUTO = "auto", "تلقائي"
        MANUAL = "manual", "يدوي"

    book = models.OneToOneField(Book, verbose_name="الكتاب", on_delete=models.CASCADE, related_name="guides")
    header_cut = models.FloatField("حدّ الترويسة", null=True, blank=True)
    footnote_line = models.FloatField("خط الحاشية", null=True, blank=True)
    page_number_zone = models.CharField(
        "موضع رقم الصفحة", max_length=8, choices=PageNumberZone.choices, default=PageNumberZone.BOTTOM
    )
    page_number_height = models.FloatField("ارتفاع منطقة رقم الصفحة", default=0.06)
    reference_page = models.ForeignKey(
        Page,
        verbose_name="الصفحة المرجعية",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    source = models.CharField("المصدر", max_length=8, choices=Source.choices, default=Source.AUTO)
    updated_at = models.DateTimeField("عُدّلت في", auto_now=True)

    class Meta:
        verbose_name = "أدلة التخطيط"
        verbose_name_plural = "أدلة التخطيط"

    def __str__(self) -> str:
        return f"guides for book {self.book_id}"


class Region(models.Model):
    """A rectangular part of a page with a semantic kind, OCR'd separately."""

    class Kind(models.TextChoices):
        BODY = "body", "متن"
        HEADING = "heading", "عنوان"
        RUNNING_HEADER = "running_header", "ترويسة"
        PAGE_NUMBER = "page_number", "رقم الصفحة"
        FOOTNOTE = "footnote", "حاشية"
        POETRY = "poetry", "شعر"
        OTHER = "other", "أخرى"

    class Source(models.TextChoices):
        GUIDES = "guides", "من الأدلة"
        AUTO = "auto", "تلقائي"
        MANUAL = "manual", "يدوي"

    page = models.ForeignKey(Page, verbose_name="الصفحة", on_delete=models.CASCADE, related_name="regions")
    kind = models.CharField("النوع", max_length=20, choices=Kind.choices)
    bbox = models.JSONField("الإحداثيات", default=list)
    order = models.PositiveIntegerField("الترتيب", default=0)
    source = models.CharField("المصدر", max_length=8, choices=Source.choices, default=Source.GUIDES)

    class Meta:
        verbose_name = "منطقة"
        verbose_name_plural = "المناطق"
        ordering = ["page", "order"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} #{self.order} on page {self.page_id}"
