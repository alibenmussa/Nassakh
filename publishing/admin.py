from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import Export, LiveLayout, PreviewRender


@admin.register(PreviewRender)
class PreviewRenderAdmin(admin.ModelAdmin):
    list_display = (
        "book",
        "scope",
        "kind",
        "chapter_id",
        "status",
        "page_count",
        "duration_ms",
        "created_at",
    )
    list_filter = ("scope", "kind", "status")
    search_fields = ("book__title", "chapter_id", "content_hash")
    raw_id_fields = ("book",)
    readonly_fields = ("created_at", "finished_at", "duration_ms", "task_id", "content_hash", "folder")
    list_select_related = ("book",)
    date_hierarchy = "created_at"


@admin.register(LiveLayout)
class LiveLayoutAdmin(admin.ModelAdmin):
    list_display = ("book", "revision", "page_count", "manuscript_version", "updated_at")
    raw_id_fields = ("book", "base")
    readonly_fields = (
        "revision",
        "path",
        "page_count",
        "chapters",
        "manuscript_version",
        "setup_hash",
        "updated_at",
    )
    list_select_related = ("book",)


@admin.register(Export)
class ExportAdmin(admin.ModelAdmin):
    """The export history, read only (D58: rows are made and pruned by `publishing.exports`)."""

    list_display = (
        "book",
        "format",
        "status",
        "filename",
        "size_bytes",
        "manuscript_version",
        "duration_ms",
        "created_by",
        "created_at",
        "download",
    )
    list_filter = ("format", "status")
    search_fields = ("book__title", "filename", "task_id")
    list_select_related = ("book", "created_by")
    date_hierarchy = "created_at"

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields] + ["download"]

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False  # no delete in v1 (D58); retention removes old rows together with their files

    @admin.display(description="تنزيل")
    def download(self, obj: Export) -> str:
        if obj.status != Export.Status.DONE or not obj.file:
            return "—"
        url = reverse("publishing:export_download", args=[obj.book_id, obj.pk])
        return format_html('<a href="{}">{}</a>', url, obj.filename or "تنزيل")
