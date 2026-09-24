"""Review history: one `LineRevision` per review action, which powers server-side undo (D27)."""

from __future__ import annotations

from django.conf import settings
from django.db import models

from books.models import Page
from ocr.models import Line


class LineRevision(models.Model):
    """One review action on a page, with snapshots of the state before and after it.

    For line actions `before` / `after` are line snapshots (`id`, `order`, `region_id`, `bbox`,
    `text`, `ocr_text`, `tokens`, `confidence`, `is_manual`, `is_reviewed`, `n_low`); `before` is
    null for an insert and `after` is null for a delete. For approve / reopen they hold the page's
    status fields and the lines' `is_reviewed` values. `undone` marks a revision reverted by undo
    (or made obsolete by a new OCR pass).
    """

    class Action(models.TextChoices):
        RESOLVE = "resolve", "حسم كلمة"
        EDIT = "edit", "تعديل سطر"
        INSERT = "insert", "إدراج سطر"
        DELETE = "delete", "حذف سطر"
        APPROVE = "approve", "اعتماد"
        REOPEN = "reopen", "إعادة فتح"

    page = models.ForeignKey(Page, verbose_name="الصفحة", on_delete=models.CASCADE, related_name="revisions")
    line = models.ForeignKey(
        Line,
        verbose_name="السطر",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="revisions",
    )
    action = models.CharField("الإجراء", max_length=10, choices=Action.choices)
    before = models.JSONField("قبل", null=True, blank=True)
    after = models.JSONField("بعد", null=True, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="المستخدم",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField("في", auto_now_add=True)
    undone = models.BooleanField("تُراجع عنه", default=False)

    class Meta:
        verbose_name = "تعديل مراجعة"
        verbose_name_plural = "سجل المراجعة"
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.action} on page {self.page_id}"
