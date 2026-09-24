"""The manuscript of a book (ProseMirror JSON, PHASE4_SPEC §1) and its snapshots.

Phase 4 writes it from assembly; Phase 5 adds the editor that saves it (`origin = editor`).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from books.models import Book


class Manuscript(models.Model):
    """The current document of a book; `version` grows by one on every save."""

    class Origin(models.TextChoices):
        ASSEMBLY = "assembly", "التجميع"
        EDITOR = "editor", "المحرّر"

    book = models.OneToOneField(
        Book, verbose_name="الكتاب", on_delete=models.CASCADE, related_name="manuscript"
    )
    document = models.JSONField("المستند", default=dict, blank=True)
    version = models.PositiveIntegerField("الإصدار", default=0)
    origin = models.CharField("المصدر", max_length=10, choices=Origin.choices, default=Origin.ASSEMBLY)
    run = models.ForeignKey(
        "assembly.AssemblyRun",
        verbose_name="تشغيل التجميع",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="manuscripts",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="عدّله",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField("أُنشئت في", auto_now_add=True)
    updated_at = models.DateTimeField("عُدّلت في", auto_now=True)

    class Meta:
        verbose_name = "مخطوطة"
        verbose_name_plural = "المخطوطات"

    def __str__(self) -> str:
        return f"manuscript of book {self.book_id} v{self.version}"


class ManuscriptSnapshot(models.Model):
    """A saved copy of a manuscript document (before a re-assembly, or taken by hand)."""

    class Reason(models.TextChoices):
        REASSEMBLY = "reassembly", "قبل إعادة التجميع"
        MANUAL = "manual", "يدوية"

    manuscript = models.ForeignKey(
        Manuscript, verbose_name="المخطوطة", on_delete=models.CASCADE, related_name="snapshots"
    )
    document = models.JSONField("المستند", default=dict, blank=True)
    version = models.PositiveIntegerField("الإصدار", default=0)
    label = models.CharField("الوصف", max_length=200, blank=True)
    reason = models.CharField("السبب", max_length=12, choices=Reason.choices, default=Reason.MANUAL)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="أنشأها",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField("أُنشئت في", auto_now_add=True)

    class Meta:
        verbose_name = "نسخة محفوظة من المخطوطة"
        verbose_name_plural = "النسخ المحفوظة من المخطوطات"
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"snapshot v{self.version} of manuscript {self.manuscript_id}"
