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

D106 (docs/challenge/CHALLENGE_SPEC.md §1): an account is an organisation (`kind` organization) or a person
(`individual`: an organisation of one member, so D102's scoping holds unchanged); `SignUp` marks a user who
signed up on the site and confirms their email. The page quota: `Plan` (a package the superuser sells),
`QuotaGrant` (pages given to an account), `QuotaHold` (a page queued for model reading, not read yet) and
`QuotaEntry` (the append-only ledger). Every change goes through `accounts.billing`.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

FONT_STYLES: tuple[str, ...] = ("regular", "bold", "italic", "bold_italic")
FONT_STYLE_LABELS: dict[str, str] = {
    "regular": "عادي",
    "bold": "عريض",
    "italic": "مائل",
    "bold_italic": "عريض مائل",
}


class Organization(models.Model):
    """An account (D106): an institution or publisher, or a person (`individual`); owns books, fonts and
    format templates. `unlimited` accounts are never limited or charged (the ones that existed before
    D106); every other one reads pages from its quota (`accounts.billing`)."""

    class Kind(models.TextChoices):
        ORGANIZATION = "organization", "مؤسسة"
        INDIVIDUAL = "individual", "فرد"

    class OrgType(models.TextChoices):
        PUBLISHER = "publisher", "دار نشر"
        RESEARCH = "research", "مركز بحث"
        UNIVERSITY = "university", "جامعة"
        LIBRARY = "library", "مكتبة"
        OTHER = "other", "أخرى"

    name = models.CharField("الاسم", max_length=200)
    kind = models.CharField("نوع الحساب", max_length=20, choices=Kind.choices, default=Kind.ORGANIZATION)
    org_type = models.CharField("نوع المؤسسة", max_length=20, choices=OrgType.choices, blank=True)
    country = models.CharField("البلد", max_length=100, blank=True)
    website = models.URLField("الموقع", max_length=300, blank=True)
    unlimited = models.BooleanField("رصيد غير محدود", default=False)
    # Pages the account may read past its balance (a debt the next grant pays first); 0: none. Superuser-set.
    overdraft_pages = models.PositiveIntegerField("حد السحب على المكشوف (صفحات)", default=0)
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


# ====================================================================== sign-up (D106)


class SignUp(models.Model):
    """A user who signed up on the site (D106): inactive until they follow the link of the confirmation email
    (`confirmed_at`). Only such a user is told on the login page that the account waits for confirmation, may
    ask for the link again and is activated by it; a user an admin deactivated is not."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, verbose_name="المستخدم", on_delete=models.CASCADE, related_name="signup"
    )
    created_at = models.DateTimeField("سُجّل في", auto_now_add=True)
    confirmed_at = models.DateTimeField("أُكّد البريد في", null=True, blank=True)

    class Meta:
        verbose_name = "تسجيل"
        verbose_name_plural = "التسجيلات"

    def __str__(self) -> str:
        return f"{self.user} ({'confirmed' if self.confirmed_at else 'pending'})"


# ====================================================================== the page quota (D106)


class Plan(models.Model):
    """A package the superuser sells: `pages` valid `validity_days` from the grant's start, at `price`."""

    name = models.CharField("الاسم", max_length=120)
    pages = models.PositiveIntegerField("الصفحات")
    validity_days = models.PositiveIntegerField("الصلاحية (أيام)", default=365)
    price = models.DecimalField("السعر", max_digits=10, decimal_places=2, default=0)
    currency = models.CharField("العملة", max_length=3, default="USD")
    active = models.BooleanField("متاحة", default=True)
    note = models.TextField("ملاحظة", blank=True)
    created_at = models.DateTimeField("أُنشئت في", auto_now_add=True)

    class Meta:
        verbose_name = "باقة"
        verbose_name_plural = "الباقات"
        ordering = ["pages", "id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.pages})"


class QuotaGrant(models.Model):
    """Pages given to an account. `remaining` is what is left of it; it counts while it is live (started,
    not expired, not revoked: `accounts.billing.live_q`). An expired or revoked grant counts for nothing;
    `manage.py expire_quota` and `billing.revoke` record that in the ledger and set `remaining` to 0."""

    class Kind(models.TextChoices):
        SIGNUP = "signup", "رصيد التسجيل"
        PURCHASE = "purchase", "شراء"
        BONUS = "bonus", "هدية"
        TRIAL = "trial", "تجربة"
        ADJUSTMENT = "adjustment", "تسوية"

    organization = models.ForeignKey(
        Organization, verbose_name="الحساب", on_delete=models.CASCADE, related_name="quota_grants"
    )
    kind = models.CharField("النوع", max_length=20, choices=Kind.choices, default=Kind.PURCHASE)
    plan = models.ForeignKey(
        Plan, verbose_name="الباقة", null=True, blank=True, on_delete=models.SET_NULL, related_name="grants"
    )
    pages = models.PositiveIntegerField("الصفحات")
    remaining = models.PositiveIntegerField("المتبقي")
    starts_at = models.DateTimeField("يبدأ في", default=timezone.now)
    expires_at = models.DateTimeField("ينتهي في", null=True, blank=True)
    amount = models.DecimalField("المبلغ", max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField("العملة", max_length=3, blank=True)
    reference = models.CharField("المرجع", max_length=120, blank=True)
    note = models.TextField("ملاحظة", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="أضافه",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField("أُضيف في", auto_now_add=True)
    revoked_at = models.DateTimeField("أُلغي في", null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="ألغاه",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = "رصيد ممنوح"
        verbose_name_plural = "الأرصدة الممنوحة"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["organization", "expires_at"], name="quota_grant_org_expiry")]

    def __str__(self) -> str:
        return f"{self.pages} → {self.organization_id} ({self.kind})"

    def state(self, now=None) -> str:
        """`live` | `future` | `expired` | `revoked` (for the screens)."""
        now = now or timezone.now()
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at is not None and self.expires_at <= now:
            return "expired"
        if self.starts_at > now:
            return "future"
        return "live"


class QuotaHold(models.Model):
    """A page queued for model reading and not read yet: pages promised. One per page (one run per page at a
    time, `books.runs`); the run that reads it consumes it, any other end releases it (`accounts.billing`)."""

    organization = models.ForeignKey(
        Organization, verbose_name="الحساب", on_delete=models.CASCADE, related_name="quota_holds"
    )
    page = models.OneToOneField(
        "books.Page", verbose_name="الصفحة", on_delete=models.CASCADE, related_name="quota_hold"
    )
    run_key = models.CharField("التشغيل", max_length=64, blank=True)
    created_at = models.DateTimeField("حُجز في", auto_now_add=True)

    class Meta:
        verbose_name = "صفحة محجوزة"
        verbose_name_plural = "الصفحات المحجوزة"

    def __str__(self) -> str:
        return f"{self.page_id} ({self.run_key})"


class QuotaEntry(models.Model):
    """The ledger (append-only): every change of an account's pages, signed. `consume` rows are unique per
    (page, run key): a retried task never charges twice. Rows without a grant are the account's debt: pages
    read with no live grant (`consume`, −1) and their repayment from a later grant (`adjust`, +n, paired with
    the grant's own −n)."""

    class Kind(models.TextChoices):
        GRANT = "grant", "منح"
        CONSUME = "consume", "قراءة صفحة"
        EXPIRE = "expire", "انتهاء الصلاحية"
        REVOKE = "revoke", "إلغاء"
        ADJUST = "adjust", "تسوية"

    organization = models.ForeignKey(
        Organization, verbose_name="الحساب", on_delete=models.CASCADE, related_name="quota_entries"
    )
    kind = models.CharField("النوع", max_length=10, choices=Kind.choices)
    pages = models.IntegerField("الصفحات")
    grant = models.ForeignKey(
        QuotaGrant,
        verbose_name="الرصيد",
        null=True,
        blank=True,
        on_delete=models.RESTRICT,
        related_name="entries",
    )
    book = models.ForeignKey(
        "books.Book",
        verbose_name="الكتاب",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    page = models.ForeignKey(
        "books.Page",
        verbose_name="الصفحة",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    run_key = models.CharField("التشغيل", max_length=64, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="المستخدم",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    note = models.CharField("ملاحظة", max_length=300, blank=True)
    created_at = models.DateTimeField("في", default=timezone.now, editable=False, db_index=True)

    class Meta:
        verbose_name = "قيد رصيد"
        verbose_name_plural = "سجل الرصيد"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["organization", "kind", "created_at"], name="quota_entry_org_kind")]
        constraints = [
            models.UniqueConstraint(
                fields=["page", "run_key"], condition=Q(kind="consume"), name="quota_consume_once"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.pages:+d} ({self.organization_id})"
