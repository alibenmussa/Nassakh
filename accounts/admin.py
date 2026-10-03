"""Users and groups are managed through django.contrib.auth's own admin, the user's page carrying its
organisation (D102: a user without a membership sees no book, so a new user is given one here). The
organisations (D98): their members (and who administers each), and read-mostly lists of their fonts and
format templates (the organisation's page, `accounts:organization`, is where fonts are uploaded and templates
made)."""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import Membership, Organization, OrganizationFont, StyleTemplate

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
    list_display = ("name", "created_at")
    search_fields = ("name",)
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
