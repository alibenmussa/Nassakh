"""Assembly runs: one row per «convert to book», with the options used, warnings, stats and staleness keys."""

from __future__ import annotations

from django.conf import settings as django_settings
from django.db import models

from books.models import Book


class AssemblyRun(models.Model):
    """One run of the assembly pipeline over a book (PHASE4_SPEC §1, §3).

    `stage` is the step being run (`assembly.pipeline.STAGES`); `settings` the options used;
    `warnings` the list of §2.9 dicts; `stats` the §2.10 counts; `included` maps each included
    page id (as a string) to `{"number", "reviewed", "sig"}`, which `manuscript_state` compares with
    the pages as they are now to tell whether the manuscript is out of date.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "في الانتظار"
        RUNNING = "running", "قيد التجميع"
        DONE = "done", "اكتمل"
        ERROR = "error", "خطأ"

    class Stage(models.TextChoices):
        COLLECT = "collect", "جمع الأسطر"
        PARAGRAPHS = "paragraphs", "بناء الفقرات"
        SEAMS = "seams", "وصل الفقرات عبر الصفحات"
        FOOTNOTES = "footnotes", "ربط الحواشي"
        HEADINGS = "headings", "بناء العناوين"
        TYPOGRAPHY = "typography", "ضبط علامات الترقيم والأرقام"
        SAVE = "save", "حفظ المخطوطة"

    book = models.ForeignKey(
        Book, verbose_name="الكتاب", on_delete=models.CASCADE, related_name="assembly_runs"
    )
    status = models.CharField("الحالة", max_length=10, choices=Status.choices, default=Status.QUEUED)
    stage = models.CharField("المرحلة", max_length=20, choices=Stage.choices, blank=True)
    settings = models.JSONField("الخيارات", default=dict, blank=True)
    warnings = models.JSONField("الملاحظات", default=list, blank=True)
    stats = models.JSONField("الإحصاءات", default=dict, blank=True)
    included = models.JSONField("الصفحات المُضمَّنة", default=dict, blank=True)
    duration_ms = models.PositiveIntegerField("المدة (مللي ثانية)", default=0)
    error = models.TextField("الخطأ", blank=True)
    task_id = models.CharField("معرّف المهمة", max_length=64, blank=True)
    created_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        verbose_name="بدأه",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField("بدأ في", auto_now_add=True)
    finished_at = models.DateTimeField("انتهى في", null=True, blank=True)

    class Meta:
        verbose_name = "تشغيل تجميع"
        verbose_name_plural = "تشغيلات التجميع"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["book", "-created_at"], name="assembly_run_book_latest")]

    def __str__(self) -> str:
        return f"assembly {self.pk} of book {self.book_id} ({self.status})"

    @property
    def is_active(self) -> bool:
        """True while the run waits in the queue or runs."""
        return self.status in (self.Status.QUEUED, self.Status.RUNNING)
