"""The manuscript of a book (ProseMirror JSON, PHASE4_SPEC §1), its snapshots and the book's stylesheet.

Phase 4 writes the manuscript from assembly; Phase 5 adds the editor that saves it one chapter at a time
(`origin = editor`, D40–D41) and the stylesheet that gives the book its physical form (PHASE5_SPEC §2).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from books.models import Book
from publishing.model import BOOK_FIELDS  # noqa: F401 - the book details of `front_matter["fields"]`

from .document import ChapterSlice, chapters_of


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

    def chapters(self) -> list[ChapterSlice]:
        """The document's chapters (D40): level-1 headings, or sections of about 30 source pages."""
        return chapters_of(self.document)


class ManuscriptSnapshot(models.Model):
    """A saved copy of a manuscript document (before a re-assembly, or taken by hand)."""

    class Reason(models.TextChoices):
        REASSEMBLY = "reassembly", "قبل إعادة التجميع"
        MANUAL = "manual", "يدوية"
        EDIT = (
            "edit",
            "قبل تعديل آلي",
        )  # before a chapter re-assembly (D41), a replace-all or a digit conversion

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


# ====================================================================== the book's stylesheet

# Trim presets (D45): key → (label, width mm, height mm). `custom` takes the stored width and height.
TRIM_PRESETS: dict[str, tuple[str, float, float]] = {
    "17x24": ("17×24 سم", 170.0, 240.0),
    "a4": ("A4", 210.0, 297.0),
    "a5": ("A5", 148.0, 210.0),
    "b5": ("B5", 176.0, 250.0),
    "14x21": ("14×21 سم", 140.0, 210.0),
    "12x17": ("12×17 سم", 120.0, 170.0),
}
CUSTOM_TRIM = "custom"
DEFAULT_TRIM = "17x24"


def default_heading_scale() -> dict:
    """Level-1 and level-2 heading sizes as multiples of the body size."""
    return {"h1": 1.6, "h2": 1.25}


def default_front_matter() -> dict:
    """Front matter of the book: a title page and a contents page; no copyright page until asked for; the
    book details (`BOOK_FIELDS`) empty (the title and the author then come from the Book)."""
    return {"title_page": True, "contents": True, "copyright_page": False, "fields": {}}


class StyleSheet(models.Model):
    """The physical form of a book (PHASE5_SPEC §2, D45): trim, margins, faces, type sizes and page furniture.

    One per book, created on the first change; a book without one uses the defaults below. Faces are
    keys of the font registry (`publishing.fonts.FONTS`); a face whose files are missing on this Mac falls
    back to Amiri when rendering. Margins are mirrored: `inner` is the binding side of every page.
    `widows` / `orphans` are the fewest lines of a paragraph left at the top / foot of a page; with
    `keep_headings` a heading never ends a page apart from the text after it (D47).
    """

    class Trim(models.TextChoices):
        T17X24 = "17x24", "17×24 سم"
        A4 = "a4", "A4"
        A5 = "a5", "A5"
        B5 = "b5", "B5"
        T14X21 = "14x21", "14×21 سم"
        T12X17 = "12x17", "12×17 سم"
        CUSTOM = "custom", "مقاس مخصّص"

    class RunningHeader(models.TextChoices):
        NONE = "none", "بلا ترويسة"
        BOOK = "book", "عنوان الكتاب"
        CHAPTER = "chapter", "عنوان الفصل"

    class PageNumber(models.TextChoices):
        NONE = "none", "بلا ترقيم"
        BOTTOM_CENTER = "bottom_center", "أسفل الصفحة في الوسط"
        BOTTOM_OUTER = "bottom_outer", "أسفل الصفحة في الطرف الخارجي"
        TOP_OUTER = "top_outer", "أعلى الصفحة في الطرف الخارجي"

    class ChapterOpening(models.TextChoices):
        ANY = "any", "أي صفحة"
        RECTO = "recto", "صفحة فردية"

    class FootnoteNumbering(models.TextChoices):
        PAGE = "page", "حسب الصفحة"
        CHAPTER = "chapter", "حسب الفصل"
        BOOK = "book", "حسب الكتاب"

    book = models.OneToOneField(
        Book, verbose_name="الكتاب", on_delete=models.CASCADE, related_name="stylesheet"
    )
    trim = models.CharField("القطع", max_length=10, choices=Trim.choices, default=Trim.T17X24)
    width_mm = models.FloatField("العرض (مم)", default=170.0)
    height_mm = models.FloatField("الارتفاع (مم)", default=240.0)
    top_mm = models.FloatField("الهامش العلوي (مم)", default=20.0)
    bottom_mm = models.FloatField("الهامش السفلي (مم)", default=22.0)
    inner_mm = models.FloatField("الهامش الداخلي (مم)", default=22.0)
    outer_mm = models.FloatField("الهامش الخارجي (مم)", default=18.0)
    bleed_mm = models.FloatField("زيادة القص (مم)", default=0.0)
    body_font = models.CharField("خط المتن", max_length=40, default="amiri")
    latin_font = models.CharField("الخط اللاتيني", max_length=40, default="times")
    heading_font = models.CharField("خط العناوين", max_length=40, default="amiri")
    body_size_pt = models.FloatField("حجم خط المتن (نقطة)", default=13.0)
    line_height = models.FloatField("تباعد الأسطر", default=1.7)
    indent_em = models.FloatField("إزاحة أول السطر (em)", default=1.5)
    heading_scale = models.JSONField("أحجام العناوين", default=default_heading_scale, blank=True)
    footnote_size_pt = models.FloatField("حجم خط الحواشي (نقطة)", default=10.0)
    footnote_numbering = models.CharField(
        "ترقيم الحواشي", max_length=10, choices=FootnoteNumbering.choices, default=FootnoteNumbering.PAGE
    )
    running_header = models.CharField(
        "الترويسة", max_length=10, choices=RunningHeader.choices, default=RunningHeader.CHAPTER
    )
    page_number = models.CharField(
        "رقم الصفحة", max_length=15, choices=PageNumber.choices, default=PageNumber.BOTTOM_CENTER
    )
    chapter_opening = models.CharField(
        "بداية الفصل", max_length=10, choices=ChapterOpening.choices, default=ChapterOpening.ANY
    )
    widows = models.PositiveSmallIntegerField("أقل عدد من الأسطر أعلى الصفحة", default=2)
    orphans = models.PositiveSmallIntegerField("أقل عدد من الأسطر أسفل الصفحة", default=2)
    keep_headings = models.BooleanField("العنوان مع السطر التالي", default=True)
    front_matter = models.JSONField("الصفحات التمهيدية", default=default_front_matter, blank=True)
    print_source_pages = models.BooleanField("أرقام الصفحات الأصلية في الهامش", default=False)
    updated_at = models.DateTimeField("عُدّل في", auto_now=True)

    class Meta:
        verbose_name = "تنسيق كتاب"
        verbose_name_plural = "تنسيقات الكتب"

    def __str__(self) -> str:
        return f"stylesheet of book {self.book_id} ({self.trim})"
