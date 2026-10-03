"""Users and groups are managed through django.contrib.auth's own admin. The organisations (D98): their
members (and who administers each), and read-mostly lists of their fonts and format templates (the
organisation's page, `accounts:organization`, is where fonts are uploaded and templates made)."""

from django.contrib import admin

from .models import Membership, Organization, OrganizationFont, StyleTemplate


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 1
    autocomplete_fields = ("user",)
    fields = ("user", "role", "created_at")
    readonly_fields = ("created_at",)


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
