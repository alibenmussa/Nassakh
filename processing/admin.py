from django.contrib import admin

from .models import LayoutGuides, Preprocess, Region


@admin.register(Preprocess)
class PreprocessAdmin(admin.ModelAdmin):
    list_display = (
        "page",
        "angle",
        "skew_confidence",
        "n_lines",
        "footnote_rule_y",
        "is_manual",
        "updated_at",
    )
    list_filter = ("is_manual",)
    raw_id_fields = ("page",)
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("page", "page__book")


@admin.register(LayoutGuides)
class LayoutGuidesAdmin(admin.ModelAdmin):
    list_display = ("book", "header_cut", "footnote_line", "page_number_zone", "source", "updated_at")
    list_filter = ("source", "page_number_zone")
    raw_id_fields = ("book", "reference_page")


@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    list_display = ("page", "order", "kind", "source", "bbox")
    list_filter = ("kind", "source")
    raw_id_fields = ("page",)
    list_select_related = ("page",)
