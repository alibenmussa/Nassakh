from django.contrib import admin

from .models import ChangesPlan, Manuscript, ManuscriptSnapshot, StyleSheet


@admin.register(Manuscript)
class ManuscriptAdmin(admin.ModelAdmin):
    list_display = ("book", "version", "origin", "run", "updated_by", "updated_at")
    list_filter = ("origin",)
    search_fields = ("book__title",)
    raw_id_fields = ("book", "run", "updated_by")
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("book", "run", "updated_by")
    exclude = ("base",)  # D78: written by the editor only (as large as the document)


@admin.register(ManuscriptSnapshot)
class ManuscriptSnapshotAdmin(admin.ModelAdmin):
    list_display = ("manuscript", "version", "reason", "label", "created_by", "created_at")
    list_filter = ("reason",)
    search_fields = ("manuscript__book__title", "label")
    raw_id_fields = ("manuscript", "created_by")
    readonly_fields = ("created_at",)
    list_select_related = ("manuscript", "created_by")
    date_hierarchy = "created_at"
    exclude = ("base",)


@admin.register(ChangesPlan)
class ChangesPlanAdmin(admin.ModelAdmin):
    list_display = ("book", "status", "manuscript_version", "created_by", "created_at", "finished_at")
    list_filter = ("status",)
    search_fields = ("book__title",)
    raw_id_fields = ("book", "created_by")
    readonly_fields = ("created_at", "finished_at")
    exclude = ("results",)  # server-side result nodes and the fresh document
    list_select_related = ("book", "created_by")


@admin.register(StyleSheet)
class StyleSheetAdmin(admin.ModelAdmin):
    list_display = ("book", "trim", "width_mm", "height_mm", "body_font", "body_size_pt", "updated_at")
    list_filter = ("trim", "body_font", "running_header", "page_number", "chapter_opening")
    search_fields = ("book__title",)
    raw_id_fields = ("book",)
    readonly_fields = ("updated_at",)
    list_select_related = ("book",)
