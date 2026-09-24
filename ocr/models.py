"""OCR runs (one per engine × region) and the reviewed lines built from them."""

from __future__ import annotations

from django.conf import settings
from django.db import models

from books.models import Page
from processing.models import Region


class OcrRun(models.Model):
    """One engine invocation on a page or region, with the raw and parsed output."""

    class Status(models.TextChoices):
        OK = "ok", "ناجح"
        ERROR = "error", "خطأ"

    page = models.ForeignKey(Page, verbose_name="الصفحة", on_delete=models.CASCADE, related_name="ocr_runs")
    region = models.ForeignKey(
        Region,
        verbose_name="المنطقة",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ocr_runs",
    )
    engine_name = models.CharField("المحرّك", max_length=40)
    model_id = models.CharField("معرّف النموذج", max_length=200, blank=True)
    model_revision = models.CharField("إصدار النموذج", max_length=64, blank=True)
    backend = models.CharField("المنصّة", max_length=20, blank=True)
    prompt = models.TextField("التعليمة", blank=True)
    input_variant = models.CharField("نوع الصورة المُدخلة", max_length=20, blank=True)  # gray | bw | gray_2x
    raw_output = models.TextField("الناتج الخام", blank=True)
    parsed_text = models.TextField("النص المستخلص", blank=True)
    params = models.JSONField("المعاملات", default=dict, blank=True)
    duration_ms = models.PositiveIntegerField("المدة (مللي ثانية)", default=0)
    output_tokens = models.PositiveIntegerField("عدد الرموز الناتجة", null=True, blank=True)
    finish = models.CharField("سبب التوقف", max_length=20, blank=True)
    looped = models.BooleanField("تكرار مفرط", default=False)
    status = models.CharField("الحالة", max_length=8, choices=Status.choices, default=Status.OK)
    error = models.TextField("الخطأ", blank=True)
    created_at = models.DateTimeField("أُنشئت في", auto_now_add=True)

    class Meta:
        verbose_name = "تشغيل OCR"
        verbose_name_plural = "تشغيلات OCR"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.engine_name} on page {self.page_id}"


class Line(models.Model):
    """A visual line of a page with its tokens, alternatives and review state."""

    page = models.ForeignKey(Page, verbose_name="الصفحة", on_delete=models.CASCADE, related_name="lines")
    order = models.PositiveIntegerField("الترتيب")
    region = models.ForeignKey(
        Region, verbose_name="المنطقة", null=True, blank=True, on_delete=models.SET_NULL, related_name="lines"
    )
    bbox = models.JSONField("الإحداثيات", null=True, blank=True)
    text = models.TextField("النص", blank=True)
    ocr_text = models.TextField("نص OCR الأصلي", blank=True)
    tokens = models.JSONField("الكلمات", default=list, blank=True)
    confidence = models.FloatField("الثقة", default=1.0)
    n_low = models.PositiveIntegerField("عدد الكلمات منخفضة الثقة", default=0)
    is_reviewed = models.BooleanField("مُراجَع", default=False)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="عدّله",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    updated_at = models.DateTimeField("عُدّل في", auto_now=True)

    class Meta:
        verbose_name = "سطر"
        verbose_name_plural = "الأسطر"
        ordering = ["page", "order"]

    def __str__(self) -> str:
        return f"line {self.order} of page {self.page_id}"
