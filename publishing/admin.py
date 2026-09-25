from django.contrib import admin

from .models import PreviewRender


@admin.register(PreviewRender)
class PreviewRenderAdmin(admin.ModelAdmin):
    list_display = ("book", "scope", "chapter_id", "status", "page_count", "duration_ms", "created_at")
    list_filter = ("scope", "status")
    search_fields = ("book__title", "chapter_id", "content_hash")
    raw_id_fields = ("book",)
    readonly_fields = ("created_at", "finished_at", "duration_ms", "task_id", "content_hash", "folder")
    list_select_related = ("book",)
    date_hierarchy = "created_at"
