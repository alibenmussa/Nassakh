import json

from django.conf import settings
from django.contrib import admin
from django.db.models import Avg, Count, Q, Sum
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from .models import Line, OcrRun, RemoteCall, TextGap


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
    list_display = ("page", "order", "region", "role", "confidence", "n_low", "is_reviewed", "updated_at")
    list_filter = ("is_reviewed", "role")
    search_fields = ("text", "ocr_text")
    raw_id_fields = ("page", "region", "updated_by")
    list_select_related = ("page", "region")


@admin.register(TextGap)
class TextGapAdmin(admin.ModelAdmin):
    list_display = ("page", "line", "index", "kind", "text", "support", "status", "decided_by", "created_at")
    list_filter = ("kind", "status")
    search_fields = ("text", "page__book__title")
    raw_id_fields = ("page", "line", "decided_by")
    readonly_fields = ("created_at",)
    list_select_related = ("page", "line", "decided_by")


# ---------------------------------------------------------------- the Runpod API log (OCR_BACKEND=runpod)

_STATUS_COLOURS = {
    RemoteCall.Status.RUNNING: "#2563eb",
    RemoteCall.Status.OK: "#16a34a",
    RemoteCall.Status.PARTIAL: "#d97706",
    RemoteCall.Status.FAILED: "#dc2626",
    RemoteCall.Status.TIMEOUT: "#dc2626",
    RemoteCall.Status.ERROR: "#dc2626",
}


def _seconds(ms: int | None) -> str:
    return "" if ms is None else f"{ms / 1000:.1f}"


def runpod_summary(queryset) -> dict:
    """What the requests of `queryset` (the filtered list) did and cost: counts per outcome, cold starts, the
    GPU seconds of their jobs and, with RUNPOD_PRICE_PER_S, an estimate of their price. Runpod also bills a
    worker's start and its idle timeout, so the estimate is a floor."""
    failed = [RemoteCall.Status.FAILED, RemoteCall.Status.TIMEOUT, RemoteCall.Status.ERROR]
    agg = queryset.aggregate(
        calls=Count("id"),
        ok=Count("id", filter=Q(status=RemoteCall.Status.OK)),
        partial=Count("id", filter=Q(status=RemoteCall.Status.PARTIAL)),
        failed=Count("id", filter=Q(status__in=failed)),
        running=Count("id", filter=Q(status=RemoteCall.Status.RUNNING)),
        cold=Count("id", filter=Q(cold_start=True)),
        pages=Count("page", distinct=True),
        execution_ms=Sum("execution_ms"),
        queue_ms=Sum("queue_ms"),
        mean_total_ms=Avg("total_ms"),
    )
    gpu_s = (agg["execution_ms"] or 0) / 1000
    price = float(settings.NASSAKH.get("RUNPOD_PRICE_PER_S") or 0)
    pages = agg["pages"] or 0
    return {
        **agg,
        "gpu_s": round(gpu_s, 1),
        "queue_s": round((agg["queue_ms"] or 0) / 1000, 1),
        "mean_total_s": round((agg["mean_total_ms"] or 0) / 1000, 1),
        "gpu_s_per_page": round(gpu_s / pages, 1) if pages else None,
        "price": price,
        "cost": round(gpu_s * price, 4) if price else None,
        "cost_per_page": round(gpu_s * price / pages, 5) if price and pages else None,
    }


@admin.register(RemoteCall)
class RemoteCallAdmin(admin.ModelAdmin):
    """The Runpod API log: every request to the Qari endpoint, read-only, newest first; the list's summary
    sums the filtered requests (`runpod_summary`)."""

    list_display = (
        "created_at",
        "status_badge",
        "operation",
        "engines",
        "page_link",
        "region_kind",
        "variant",
        "total_s",
        "queue_s",
        "execution_s",
        "gpu",
        "cold_start",
        "attempts",
    )
    list_filter = ("status", "operation", "cold_start", "gpu", "region_kind")
    search_fields = ("job_id", "worker_id", "error", "page__book__title")
    date_hierarchy = "created_at"
    list_select_related = ("page", "page__book")
    list_per_page = 50
    fieldsets = (
        (
            "الطلب",
            {
                "fields": (
                    "created_at",
                    "finished_at",
                    "operation",
                    "status_badge",
                    "engines",
                    "page_link",
                    "region_kind",
                    "variant",
                    "endpoint",
                )
            },
        ),
        (
            "Runpod",
            {
                "fields": (
                    "job_id",
                    "job_status",
                    "http_status",
                    "attempts",
                    "polls",
                    "worker_id",
                    "worker_version",
                    "gpu",
                    "cold_start",
                )
            },
        ),
        ("التوقيت (ثوانٍ)", {"fields": ("queue_s", "execution_s", "total_s")}),
        ("التفاصيل", {"fields": ("error", "request_json", "response_json")}),
    )
    readonly_fields = (
        "created_at",
        "finished_at",
        "operation",
        "status_badge",
        "engines",
        "page_link",
        "region_kind",
        "variant",
        "endpoint",
        "job_id",
        "job_status",
        "http_status",
        "attempts",
        "polls",
        "worker_id",
        "worker_version",
        "gpu",
        "cold_start",
        "queue_s",
        "execution_s",
        "total_s",
        "error",
        "request_json",
        "response_json",
    )

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    @admin.display(description="الحالة", ordering="status")
    def status_badge(self, obj: RemoteCall) -> str:
        label = obj.get_status_display()
        limit = float(settings.NASSAKH.get("RUNPOD_TIMEOUT_S") or 600) + 120
        if obj.status == RemoteCall.Status.RUNNING and obj.created_at:
            if (timezone.now() - obj.created_at).total_seconds() > limit:
                label = "لم يكتمل (توقّف العامل؟)"
        colour = _STATUS_COLOURS.get(obj.status, "#8a94a6")
        return format_html('<span style="color:{}">●</span> {}', colour, label)

    @admin.display(description="الصفحة", ordering="page")
    def page_link(self, obj: RemoteCall) -> str:
        if obj.page_id is None:
            return "—"
        url = reverse("admin:books_page_change", args=[obj.page_id])
        return format_html('<a href="{}">كتاب {} · ص {}</a>', url, obj.page.book_id, obj.page.number)

    @admin.display(description="الكلي", ordering="total_ms")
    def total_s(self, obj: RemoteCall) -> str:
        return _seconds(obj.total_ms)

    @admin.display(description="الانتظار", ordering="queue_ms")
    def queue_s(self, obj: RemoteCall) -> str:
        return _seconds(obj.queue_ms)

    @admin.display(description="GPU", ordering="execution_ms")
    def execution_s(self, obj: RemoteCall) -> str:
        return _seconds(obj.execution_ms)

    @admin.display(description="الطلب (بلا الصورة)")
    def request_json(self, obj: RemoteCall) -> str:
        return format_html(
            '<pre dir="ltr" style="white-space:pre-wrap;margin:0">{}</pre>',
            json.dumps(obj.request, ensure_ascii=False, indent=2),
        )

    @admin.display(description="الرد (بلا النصوص)")
    def response_json(self, obj: RemoteCall) -> str:
        return format_html(
            '<pre dir="ltr" style="white-space:pre-wrap;margin:0">{}</pre>',
            json.dumps(obj.response, ensure_ascii=False, indent=2),
        )

    def changelist_view(self, request, extra_context=None):
        response = super().changelist_view(request, extra_context)
        context = getattr(response, "context_data", None)
        if context and context.get("cl") is not None:
            context["runpod_summary"] = runpod_summary(context["cl"].queryset)
        return response
