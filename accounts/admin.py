"""Users and groups are managed through django.contrib.auth's own admin, the user's page carrying its
organisation (D102: a user without a membership sees no book, so a new user is given one here). The
organisations (D98): their members (and who administers each), and read-mostly lists of their fonts and
format templates (the organisation's page, `accounts:organization`, is where fonts are uploaded and templates
made). D106: the sign-ups and the page quota's rows (plans, grants, holds, the ledger), read-only («الفوترة»
changes them)."""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import (
    Membership,
    Organization,
    OrganizationFont,
    Plan,
    QuotaEntry,
    QuotaGrant,
    QuotaHold,
    SignUp,
    StyleTemplate,
)

User = get_user_model()


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 1
    autocomplete_fields = ("user",)
    fields = ("user", "role", "created_at")
    readonly_fields = ("created_at",)


class UserMembershipInline(admin.StackedInline):
    """The user's organisation on the user's page (one per user): without one the user sees no book."""

    model = Membership
    extra = 1
    max_num = 1
    can_delete = True
    fields = ("organization", "role")
    verbose_name = "المؤسسة"
    verbose_name_plural = "المؤسسة (بلا مؤسسة لا يرى المستخدم أي كتاب)"


admin.site.unregister(User)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """django.contrib.auth's user admin with the user's organisation (D102)."""

    inlines = (*BaseUserAdmin.inlines, UserMembershipInline)
    list_display = (*BaseUserAdmin.list_display, "organization")
    list_filter = (*BaseUserAdmin.list_filter, "membership__organization")
    list_select_related = ("membership__organization",)

    @admin.display(description="المؤسسة", ordering="membership__organization__name")
    def organization(self, user) -> str:
        try:
            return user.membership.organization.name
        except Membership.DoesNotExist:
            return "—"


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "org_type", "country", "unlimited", "created_at")
    list_filter = ("kind", "org_type", "unlimited")
    search_fields = ("name", "country")
    inlines = (MembershipInline,)


@admin.register(OrganizationFont)
class OrganizationFontAdmin(admin.ModelAdmin):
    list_display = ("name", "family", "organization", "latin", "arabic", "removed_at", "created_at")
    list_filter = ("organization", "latin", "arabic")
    search_fields = ("name", "family")
    readonly_fields = (
        "family",
        "regular",
        "bold",
        "italic",
        "bold_italic",
        "files",
        "uploaded_by",
        "licence_confirmed_by",
        "created_at",
        "updated_at",
        "removed_by",
    )


@admin.register(StyleTemplate)
class StyleTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "source_book", "updated_at")
    list_filter = ("organization",)
    search_fields = ("name", "description")
    readonly_fields = ("values", "source_book", "created_by", "updated_by", "created_at", "updated_at")


# ====================================================================== D106: sign-ups and the page quota
# Read-only: every change goes through `accounts.billing` (the «الفوترة» screens, superusers).


class ReadOnlyAdmin(admin.ModelAdmin):
    """A list and a page to look at, nothing to add, change or delete."""

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(SignUp)
class SignUpAdmin(ReadOnlyAdmin):
    """Deletable: deleting a user (a spam sign-up) deletes its row. The billing rows below are not: the ledger
    is append-only, so an account with billing rows is not deleted from here."""

    list_display = ("user", "created_at", "confirmed_at")
    list_filter = ("confirmed_at",)
    search_fields = ("user__username", "user__email")

    def has_delete_permission(self, request, obj=None) -> bool:
        return bool(request.user.is_superuser)


@admin.register(Plan)
class PlanAdmin(ReadOnlyAdmin):
    list_display = ("name", "pages", "validity_days", "price", "currency", "active")
    list_filter = ("active",)


@admin.register(QuotaGrant)
class QuotaGrantAdmin(ReadOnlyAdmin):
    list_display = (
        "organization",
        "kind",
        "pages",
        "remaining",
        "starts_at",
        "expires_at",
        "revoked_at",
        "reference",
    )
    list_filter = ("kind", "organization")
    search_fields = ("organization__name", "reference", "note")


@admin.register(QuotaHold)
class QuotaHoldAdmin(ReadOnlyAdmin):
    list_display = ("organization", "page", "run_key", "created_at")
    list_filter = ("organization",)


@admin.register(QuotaEntry)
class QuotaEntryAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "organization", "kind", "pages", "grant", "book", "page", "run_key", "note")
    list_filter = ("kind", "organization")
    search_fields = ("organization__name", "run_key", "note")
    list_select_related = ("organization", "grant", "book", "page")
