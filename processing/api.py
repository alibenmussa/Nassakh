"""JSON endpoints of the processing app (DRF function views, session auth).

- POST /api/pages/<id>/preprocess/  manual parameters → re-run preprocessing → new image URLs
- POST /api/pages/<id>/guides/      per-page guide override → regions re-derived
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from books.models import Page
from core.decorators import ROLE_EDITOR, has_role
from processing import services

FORBIDDEN = {"detail": "هذا الإجراء يتطلب صلاحية محرّر."}


def _editor_or_403(request: Request) -> Response | None:
    if not has_role(request.user, ROLE_EDITOR):
        return Response(FORBIDDEN, status=status.HTTP_403_FORBIDDEN)
    return None


@api_view(["POST"])
def page_preprocess(request: Request, page_id: int) -> Response:
    """Re-run preprocessing with manual parameters (or `reset: true` for the automatic values).

    Runs synchronously for ordinary page sizes and returns the new state (image URLs carry a
    version query so the browser reloads them); very large originals are queued instead (202).
    """
    denied = _editor_or_403(request)
    if denied:
        return denied
    page = get_object_or_404(Page.objects.select_related("book"), pk=page_id)
    data = request.data if isinstance(request.data, dict) else {}
    try:
        manual = None if data.get("reset") else services.clean_manual_params(data)
    except ValidationError as exc:
        return Response({"errors": list(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)

    if not services.preprocess_is_quick(page):
        from processing import tasks

        result = tasks.preprocess_page.delay(page.pk, manual or {})
        return Response(
            {
                "queued": True,
                "task_id": result.id,
                "detail": "الصورة كبيرة؛ أُرسلت المعالجة إلى العامل الخلفي.",
            },
            status=status.HTTP_202_ACCEPTED,
        )
    try:
        pre = services.preprocess_page(page, manual=manual)
    except services.ProcessingError as exc:
        return Response({"errors": [str(exc)]}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
    if page.regions.exists():
        services.derive_regions(page)
    payload = services.preprocess_payload(page, pre)
    payload["queued"] = False
    payload["regions"] = services.region_items(list(page.regions.all()))
    return Response(payload)


@api_view(["POST"])
def page_guides_override(request: Request, page_id: int) -> Response:
    """Set (or reset) the page's guide override and re-derive its regions."""
    denied = _editor_or_403(request)
    if denied:
        return denied
    page = get_object_or_404(Page.objects.select_related("book"), pk=page_id)
    data = request.data if isinstance(request.data, dict) else {}
    try:
        regions, enqueued = services.set_page_guides_override(page, data)
    except ValidationError as exc:
        return Response({"errors": list(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)
    except services.ProcessingError as exc:
        return Response({"errors": [str(exc)]}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
    return Response(
        {
            "page_id": page.pk,
            "status": page.status,
            "status_label": page.get_status_display(),
            "guides": services.effective_guides(page),
            "override": page.guides_override,
            "regions": services.region_items(regions),
            "ocr_enqueued": enqueued,
        }
    )
