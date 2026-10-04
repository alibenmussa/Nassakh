"""Search and quotation checking (D107) and the MCP server's access (D108).

- `PageText`: the searchable text of one page and kind (the author's text, `body`, or the editor's notes,
  `notes`), built from the page's reviewed lines by `research.index`.
- `AccessKey`: a key an AI assistant connects to the MCP server with (`research.keys`); only its hash is kept.
- `ToolCall`: one MCP tool call (the log the account page and the evaluation read, and the rate limit).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from books.models import Book, Page


class PageText(models.Model):
    """The words of one page and kind, as the review layer holds them (`research.index`).

    `words`: `[{line, i, text, norm, state}]` in reading order — the `ocr.Line` id, the token's index in
    the line, the word as written (diacritics kept, punctuation dropped), its normal form
    (`research.normalize`) and its review state (`reviewed` | `doubtful` | `unreviewed`, see
    `research.index.token_state`); `gap: true` on a word an open suggestion (`ocr.TextGap`) follows.
    `norm` is the words' normal forms joined by single spaces (a match on it maps back to the words);
    `stamp` the state of the page's lines it was built from (`research.index.line_stamps`): a search
    rebuilds a row whose stamp no longer matches before it answers.
    """

    class Kind(models.TextChoices):
        BODY = "body", "المتن"
        NOTES = "notes", "الحواشي"

    book = models.ForeignKey(Book, verbose_name="الكتاب", on_delete=models.CASCADE, related_name="+")
    page = models.ForeignKey(Page, verbose_name="الصفحة", on_delete=models.CASCADE, related_name="texts")
    kind = models.CharField("النوع", max_length=5, choices=Kind.choices)
    words = models.JSONField("الكلمات", default=list, blank=True)
    norm = models.TextField("النص المطبَّع", blank=True)
    stamp = models.CharField("بصمة الأسطر", max_length=80, blank=True)
    updated_at = models.DateTimeField("حُدّث في", auto_now=True)

    class Meta:
        verbose_name = "نص صفحة للبحث"
        verbose_name_plural = "نصوص الصفحات للبحث"
        constraints = [models.UniqueConstraint(fields=["page", "kind"], name="research_pagetext_page_kind")]
        indexes = [models.Index(fields=["book", "kind"], name="research_pagetext_book_kind")]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} of page {self.page_id}"


class AccessKey(models.Model):
    """A key an AI assistant connects to the MCP server with (D108): `nsk_` + 32 random bytes (urlsafe),
    shown once when it is made; only its SHA-256 (`key_hash`) and its first characters (`prefix`, to tell
    keys apart) are kept. The key acts as its user: the tools see that user's books (`books.access`). A
    revoked key (`revoked_at`) answers 401."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="المستخدم",
        on_delete=models.CASCADE,
        related_name="access_keys",
    )
    name = models.CharField("الاسم", max_length=80)
    prefix = models.CharField("بداية المفتاح", max_length=16)
    key_hash = models.CharField("بصمة المفتاح", max_length=64, unique=True)
    created_at = models.DateTimeField("أُنشئ في", auto_now_add=True)
    last_used_at = models.DateTimeField("آخر استعمال", null=True, blank=True)
    revoked_at = models.DateTimeField("أُلغي في", null=True, blank=True)

    class Meta:
        verbose_name = "مفتاح وصول"
        verbose_name_plural = "مفاتيح الوصول"
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.prefix}…)"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class ToolCall(models.Model):
    """One call of an MCP tool (D108): which key, which tool, how long it took and how it ended. The rate
    limit counts a key's calls of the last minute here."""

    class Status(models.TextChoices):
        OK = "ok", "ناجح"
        ERROR = "error", "خطأ"
        LIMITED = "limited", "تجاوز الحد"

    key = models.ForeignKey(
        AccessKey, verbose_name="المفتاح", null=True, on_delete=models.SET_NULL, related_name="calls"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="المستخدم",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    tool = models.CharField("الأداة", max_length=40)
    status = models.CharField("الحالة", max_length=8, choices=Status.choices, default=Status.OK)
    ms = models.PositiveIntegerField("المدة (مللي ثانية)", default=0)
    detail = models.CharField("التفاصيل", max_length=200, blank=True)
    created_at = models.DateTimeField("في", auto_now_add=True)

    class Meta:
        verbose_name = "استدعاء أداة"
        verbose_name_plural = "سجل استدعاءات الأدوات"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["key", "created_at"], name="research_toolcall_key_time")]

    def __str__(self) -> str:
        return f"{self.tool} · {self.status}"
