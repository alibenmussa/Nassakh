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

    class Role(models.TextChoices):
        """What the line is in the book, set by a reviewer (D32, D74). `body` follows the line's region
        (a line of a footnote region is a note); `footnote` makes a body-region line a note, `main` pulls
        a footnote-region line back into the body, `verse` keeps the line on its own in assembly.
        `ocr.services.line_kind` gives the effective kind; the review menu offers the effective choice."""

        BODY = "body", "محتوى"
        HEADING = "heading", "عنوان رئيسي"
        SUBHEADING = "subheading", "عنوان فرعي"
        VERSE = "verse", "شعر"
        FOOTNOTE = "footnote", "حاشية"
        MAIN = "main", "محتوى"

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
    # Inserted by a reviewer (Phase 3) for text a model skipped; never produced by OCR.
    is_manual = models.BooleanField("أُدرج يدويًا", default=False)
    role = models.CharField("نوع السطر", max_length=12, choices=Role.choices, default=Role.BODY)
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


class TextGap(models.Model):
    """Text a model may have skipped, offered to the reviewer and never part of the text (D72).

    7b writes only `kind="words"`: a run of Qari v0.2 words with no Qari v0.3 counterpart that
    Tesseract does not support (`ocr.flags.secondary_only_runs`). It sits after the token `index` of
    `line` (−1 = before its first token); `after_t` keeps that token's text so the review services can
    re-anchor it after an edit. `finalize_page` replaces a page's gaps with its lines; a page with
    review work keeps them. `accept_gap` inserts the words (`inserted`), `dismiss_gap` drops the offer.
    """

    class Kind(models.TextChoices):
        WORDS = "words", "كلمات"

    class Status(models.TextChoices):
        OPEN = "open", "مفتوح"
        INSERTED = "inserted", "أُدرج"
        DISMISSED = "dismissed", "تُجوهل"

    page = models.ForeignKey(Page, verbose_name="الصفحة", on_delete=models.CASCADE, related_name="gaps")
    line = models.ForeignKey(
        Line, verbose_name="السطر", null=True, blank=True, on_delete=models.SET_NULL, related_name="gaps"
    )
    index = models.IntegerField("بعد الكلمة", default=-1)
    after_t = models.CharField("نص الكلمة السابقة", max_length=200, blank=True)
    kind = models.CharField("النوع", max_length=10, choices=Kind.choices, default=Kind.WORDS)
    text = models.TextField("النص المقترح")
    source = models.CharField("المصدر", max_length=20, default="secondary")
    support = models.FloatField("تأييد Tesseract", default=0.0)
    status = models.CharField("الحالة", max_length=10, choices=Status.choices, default=Status.OPEN)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="حسمه",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    decided_at = models.DateTimeField("حُسم في", null=True, blank=True)
    created_at = models.DateTimeField("أُنشئ في", auto_now_add=True)

    class Meta:
        verbose_name = "نص مقترح"
        verbose_name_plural = "نصوص مقترحة"
        ordering = ["page", "line__order", "index", "id"]

    def __str__(self) -> str:
        return f"gap after {self.index} on line {self.line_id} of page {self.page_id}"


class RemoteCall(models.Model):
    """One request to the Runpod endpoint that reads with the Qari models (OCR_BACKEND=runpod,
    docs/RUNPOD_SPEC.md): the API log in Django admin.

    What was asked (never the image), how long Runpod kept it in its queue and on the GPU, what came back
    (never the text) and why it failed. `ocr.runpod` writes the row when the request leaves and completes it
    when the request ends, so a request still waiting for a cold start is on the list while it waits.
    `queue_ms` and `execution_ms` are Runpod's own `delayTime` and `executionTime`; `total_ms` is the time
    Nassakh waited, retries and polls included.
    """

    class Operation(models.TextChoices):
        READ = "read", "قراءة"
        PING = "ping", "فحص"

    class Status(models.TextChoices):
        RUNNING = "running", "قيد التنفيذ"
        OK = "ok", "اكتمل"
        PARTIAL = "partial", "اكتمل جزئيًا"  # a model failed inside a completed job
        FAILED = "failed", "فشل"  # the worker failed the job, or every model in it
        TIMEOUT = "timeout", "انتهت المهلة"  # no answer within RUNPOD_TIMEOUT_S: the job was cancelled
        ERROR = "error", "تعذّر الاتصال"  # Runpod refused the request or could not be reached

    created_at = models.DateTimeField("أُرسل في", auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField("انتهى في", null=True, blank=True)
    operation = models.CharField("العملية", max_length=8, choices=Operation.choices, default=Operation.READ)
    status = models.CharField(
        "الحالة", max_length=10, choices=Status.choices, default=Status.RUNNING, db_index=True
    )
    engines = models.CharField("النماذج", max_length=80, blank=True)
    page = models.ForeignKey(
        Page,
        verbose_name="الصفحة",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="remote_calls",
    )
    region_kind = models.CharField("المنطقة", max_length=20, blank=True)
    variant = models.CharField("نوع الصورة", max_length=20, blank=True)
    endpoint = models.CharField("نقطة النهاية", max_length=200, blank=True)
    job_id = models.CharField("رقم المهمة", max_length=80, blank=True, db_index=True)
    job_status = models.CharField("حالة المهمة في Runpod", max_length=20, blank=True)
    http_status = models.PositiveSmallIntegerField("آخر رمز HTTP", null=True, blank=True)
    attempts = models.PositiveSmallIntegerField("محاولات الإرسال", default=0)
    polls = models.PositiveSmallIntegerField("مرات الاستعلام", default=0)
    queue_ms = models.PositiveIntegerField("الانتظار وبدء العامل (مللي ثانية)", null=True, blank=True)
    execution_ms = models.PositiveIntegerField("التنفيذ على GPU (مللي ثانية)", null=True, blank=True)
    total_ms = models.PositiveIntegerField("الزمن الكلي (مللي ثانية)", null=True, blank=True)
    gpu = models.CharField("GPU", max_length=80, blank=True)
    worker_id = models.CharField("العامل", max_length=80, blank=True)
    worker_version = models.CharField("إصدار العامل", max_length=20, blank=True)
    cold_start = models.BooleanField("بدء بارد", null=True, blank=True)
    request = models.JSONField("الطلب", default=dict, blank=True)
    response = models.JSONField("الرد", default=dict, blank=True)
    error = models.TextField("الخطأ", blank=True)

    class Meta:
        verbose_name = "طلب Runpod"
        verbose_name_plural = "سجل طلبات Runpod"
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.get_operation_display()} {self.engines} · {self.get_status_display()}"
