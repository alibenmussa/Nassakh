from django.contrib import admin

from .models import AccessKey, PageText, ToolCall


@admin.register(PageText)
class PageTextAdmin(admin.ModelAdmin):
    list_display = ("page", "kind", "book", "stamp", "updated_at")
    list_filter = ("kind",)
    search_fields = ("book__title",)
    raw_id_fields = ("book", "page")
    readonly_fields = ("book", "page", "kind", "words", "norm", "stamp", "updated_at")
    list_select_related = ("book", "page")


@admin.register(AccessKey)
class AccessKeyAdmin(admin.ModelAdmin):
    list_display = ("name", "prefix", "user", "created_at", "last_used_at", "revoked_at")
    list_filter = ("revoked_at",)
    search_fields = ("name", "prefix", "user__username", "user__email")
    raw_id_fields = ("user",)
    readonly_fields = ("prefix", "key_hash", "created_at", "last_used_at")
    list_select_related = ("user",)


@admin.register(ToolCall)
class ToolCallAdmin(admin.ModelAdmin):
    list_display = ("created_at", "tool", "status", "ms", "key", "user", "detail")
    list_filter = ("tool", "status")
    raw_id_fields = ("key", "user")
    readonly_fields = ("key", "user", "tool", "status", "ms", "detail", "created_at")
    list_select_related = ("key", "user")
    date_hierarchy = "created_at"
