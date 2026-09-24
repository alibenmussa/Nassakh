from django.contrib import admin

from .models import LineRevision


@admin.register(LineRevision)
class LineRevisionAdmin(admin.ModelAdmin):
    list_display = ("page", "action", "line", "user", "created_at", "undone")
    list_filter = ("action", "undone")
    search_fields = ("page__book__title",)
    raw_id_fields = ("page", "line", "user")
    readonly_fields = ("created_at",)
    list_select_related = ("page", "line", "user")
    date_hierarchy = "created_at"
