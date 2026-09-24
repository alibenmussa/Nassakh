from django.contrib import admin

from .models import Line, OcrRun


@admin.register(OcrRun)
class OcrRunAdmin(admin.ModelAdmin):
    list_display = (
        "page",
        "region",
        "engine_name",
        "backend",
        "input_variant",
        "duration_ms",
        "looped",
        "status",
        "created_at",
    )
    list_filter = ("engine_name", "backend", "input_variant", "status", "looped")
    search_fields = ("page__book__title", "model_id")
    raw_id_fields = ("page", "region")
    readonly_fields = ("created_at",)
    list_select_related = ("page", "region")
    date_hierarchy = "created_at"


@admin.register(Line)
class LineAdmin(admin.ModelAdmin):
    list_display = ("page", "order", "region", "confidence", "n_low", "is_reviewed", "updated_at")
    list_filter = ("is_reviewed",)
    search_fields = ("text", "ocr_text")
    raw_id_fields = ("page", "region", "updated_by")
    list_select_related = ("page", "region")
