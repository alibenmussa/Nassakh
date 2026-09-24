from django.contrib import admin

from .models import Manuscript, ManuscriptSnapshot


@admin.register(Manuscript)
class ManuscriptAdmin(admin.ModelAdmin):
    list_display = ("book", "version", "origin", "run", "updated_by", "updated_at")
    list_filter = ("origin",)
    search_fields = ("book__title",)
    raw_id_fields = ("book", "run", "updated_by")
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("book", "run", "updated_by")


@admin.register(ManuscriptSnapshot)
class ManuscriptSnapshotAdmin(admin.ModelAdmin):
    list_display = ("manuscript", "version", "reason", "label", "created_by", "created_at")
    list_filter = ("reason",)
    search_fields = ("manuscript__book__title", "label")
    raw_id_fields = ("manuscript", "created_by")
    readonly_fields = ("created_at",)
    list_select_related = ("manuscript", "created_by")
    date_hierarchy = "created_at"
