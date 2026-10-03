"""The organisation (D98): the institution or publisher a book belongs to, its members, its fonts and its
format templates.

Roles inside the app stay Django groups (admin / editor / proofreader, `core.decorators`); the organisation
adds who owns a book and the assets its books share. A user belongs to one organisation (`Membership`, one
row per user); an organisation admin (`Membership.Role.ADMIN`, a superuser, or a member in the global `admin`
group: `accounts.services.is_org_admin`) adds and removes its fonts and manages its templates.

- `OrganizationFont` (D98): a face uploaded by the organisation, up to four files (regular, bold, italic,
  bold italic) stored normalised (TrueType or CFF OpenType, never WOFF) at
  `orgs/<org>/fonts/<sha256>.<ttf|otf>` — files are immutable and never served by `/media/` (only through
  `accounts:font_file`, to the organisation's members). Its key in a stylesheet is `org-<pk>`
  (`publishing.fonts`). Removal is soft (`removed_at`): the books that use it fall back to Amiri with a
  notice, and it can be restored; a removed face no book uses can be deleted for good.
- `StyleTemplate` (D98): a named set of the stylesheet's values (`editor.services.STYLESHEET_FIELDS` without
  the book's own details and cover), taken from a book and applied to others through the stylesheet's own
  validation (`accounts.styles`).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

FONT_STYLES: tuple[str, ...] = ("regular", "bold", "italic", "bold_italic")
FONT_STYLE_LABELS: dict[str, str] = {
    "regular": "عادي",
    "bold": "عريض",
    "italic": "مائل",
    "bold_italic": "عريض مائل",
}


class Organization(models.Model):
    """An institution or publisher: owns books, fonts and format templates."""

    name = models.CharField("الاسم", max_length=200)
    created_at = models.DateTimeField("أُنشئت في", auto_now_add=True)

    class Meta:
        verbose_name = "مؤسسة"
        verbose_name_plural = "المؤسسات"
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return self.name


class Membership(models.Model):
    """A user's place in an organisation (one organisation per user)."""

    class Role(models.TextChoices):
        ADMIN = "admin", "مدير المؤسسة"
        MEMBER = "member", "عضو"

    organization = models.ForeignKey(
        Organization, verbose_name="المؤسسة", on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, verbose_name="المستخدم", on_delete=models.CASCADE, related_name="membership"
    )
    role = models.CharField("الدور", max_length=10, choices=Role.choices, default=Role.MEMBER)
    created_at = models.DateTimeField("أُضيف في", auto_now_add=True)

    class Meta:
        verbose_name = "عضوية"
        verbose_name_plural = "العضويات"

    def __str__(self) -> str:
        return f"{self.user} in {self.organization} ({self.role})"


def org_font_path(instance: OrganizationFont, filename: str) -> str:
    """`orgs/<org>/fonts/<name>`: the upload service names the file by its content (`<sha256>.<ext>`)."""
    return f"orgs/{instance.organization_id}/fonts/{filename}"


class OrganizationFont(models.Model):
    """A face of an organisation (D98), the counterpart of a `publishing.fonts.FontSpec`.

    `family` is the family name the regular file gives (name ID 1: what Word and the reading systems match);
    `name` the display name in the font menus (the family unless typed). `files` holds each stored file's
    facts: `{style: {sha256, size, format (ttf | otf), source_name, weight, italic, fs_type, outlines (glyf |
    cff), glyphs}}`. `latin` is true when the face has Latin letters and digits, `arabic` when it has the
    Arabic letters. `licence` is the notice shown with the face (typed, else the files' licence and
    copyright records); `licence_confirmed_by` the admin who confirmed the organisation may embed it.
    """

    organization = models.ForeignKey(
        Organization, verbose_name="المؤسسة", on_delete=models.CASCADE, related_name="fonts"
    )
    name = models.CharField("الاسم", max_length=120)
    family = models.CharField("العائلة في الملف", max_length=200)
    regular = models.FileField("الملف العادي", upload_to=org_font_path, max_length=255)
    bold = models.FileField("الملف العريض", upload_to=org_font_path, max_length=255, blank=True)
    italic = models.FileField("الملف المائل", upload_to=org_font_path, max_length=255, blank=True)
    bold_italic = models.FileField("الملف العريض المائل", upload_to=org_font_path, max_length=255, blank=True)
    files = models.JSONField("بيانات الملفات", default=dict, blank=True)
    latin = models.BooleanField("فيه حروف لاتينية وأرقام", default=False)
    arabic = models.BooleanField("فيه حروف عربية", default=True)
    licence = models.TextField("الترخيص", blank=True)
    licence_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="أقرّ بالترخيص",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="رفعه",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField("رُفع في", auto_now_add=True)
    updated_at = models.DateTimeField("عُدّل في", auto_now=True)
    removed_at = models.DateTimeField("حُذف في", null=True, blank=True)
    removed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="حذفه",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = "خط مؤسسة"
        verbose_name_plural = "خطوط المؤسسات"
        ordering = ["name", "id"]
        indexes = [models.Index(fields=["organization", "removed_at"], name="org_font_active")]

    def __str__(self) -> str:
        return f"{self.name} ({self.key})"

    @property
    def key(self) -> str:
        """The face's key in a stylesheet (`org-<pk>`, `publishing.fonts.ORG_PREFIX`)."""
        return f"org-{self.pk}"

    @property
    def removed(self) -> bool:
        return self.removed_at is not None

    def file_of(self, style: str):
        """The `FieldFile` of a style (`regular`, `bold`, `italic`, `bold_italic`), falsy when absent."""
        return getattr(self, style) if style in FONT_STYLES else None

    def styles(self) -> list[str]:
        """The styles it has files for, in `FONT_STYLES` order."""
        return [style for style in FONT_STYLES if self.file_of(style)]


class StyleTemplate(models.Model):
    """A format template of an organisation (D98): the stylesheet's values under a name.

    `values` are `editor.services.stylesheet_values` without `updated_at`, the book details and the cover
    (`accounts.styles.template_values`); `source_book` the book they were last taken from.
    """

    organization = models.ForeignKey(
        Organization, verbose_name="المؤسسة", on_delete=models.CASCADE, related_name="style_templates"
    )
    name = models.CharField("الاسم", max_length=120)
    description = models.CharField("الوصف", max_length=300, blank=True)
    values = models.JSONField("القيم", default=dict, blank=True)
    source_book = models.ForeignKey(
        "books.Book",
        verbose_name="أُخذ من الكتاب",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="أنشأه",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="حدّثه",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField("أُنشئ في", auto_now_add=True)
    updated_at = models.DateTimeField("حُدّث في", auto_now=True)

    class Meta:
        verbose_name = "قالب تنسيق"
        verbose_name_plural = "قوالب التنسيق"
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="style_template_unique_name"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.organization_id})"
