from django.contrib import admin

from .models import Book, Page


@admin.register(Book)
class BookAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "author",
        "original_year",
        "status",
        "source_page_count",
        "pages_per_sheet",
        "updated_at",
    )
    list_filter = ("status", "pages_per_sheet", "has_text_layer")
    search_fields = ("title", "author")
    readonly_fields = ("created_at", "updated_at", "source_page_count", "has_text_layer")
    raw_id_fields = ("created_by",)
    date_hierarchy = "created_at"


@admin.register(Page)
class PageAdmin(admin.ModelAdmin):
    list_display = (
        "book",
        "number",
        "source_index",
        "source_half",
        "status",
        "text_state",
        "is_excluded",
        "error_from",
    )
    list_filter = ("status", "text_state", "is_excluded", "source_half")
    search_fields = ("book__title",)
    raw_id_fields = ("book", "reviewed_by")
    readonly_fields = ("width", "height", "dpi", "task_id")
    list_select_related = ("book",)
