from django.contrib import admin

from .models import AssemblyRun


@admin.register(AssemblyRun)
class AssemblyRunAdmin(admin.ModelAdmin):
    list_display = ("book", "status", "stage", "duration_ms", "created_by", "created_at", "finished_at")
    list_filter = ("status", "stage")
    search_fields = ("book__title",)
    raw_id_fields = ("book", "created_by")
    readonly_fields = ("created_at", "finished_at", "duration_ms", "task_id")
    list_select_related = ("book", "created_by")
    date_hierarchy = "created_at"
