"""JSON endpoints of the processing app (DRF function views, session auth, editors only).

- POST /api/pages/<id>/preprocess/  manual parameters → re-run preprocessing → new image URLs
- POST /api/pages/<id>/guides/      per-page guide override → regions re-derived
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.request import Request
from rest_framework.response import Response

from books.models import Page
from core.permissions import IsEditor
from processing import services


@api_view(["POST"])
@permission_classes([IsEditor])
def page_preprocess(request: Request, page_id: int) -> Response:
    """Re-run preprocessing with manual parameters (or `reset: true` for the automatic values).

    Runs synchronously for ordinary page sizes and returns the new state (image URLs carry a
    version query so the browser reloads them); very large originals are queued instead (202).
    """
    page = get_object_or_404(Page.objects.select_related("book"), pk=page_id)
    data = request.data if isinstance(request.data, dict) else {}
    try:
        manual = None if data.get("reset") else services.clean_manual_params(data)
        payload, queued = services.rerun_preprocess(page, manual)
    except ValidationError as exc:
        return Response({"errors": list(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)
    except services.ProcessingError as exc:
        return Response({"errors": [str(exc)]}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
    payload["queued"] = queued
    return Response(payload, status=status.HTTP_202_ACCEPTED if queued else status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsEditor])
def page_guides_override(request: Request, page_id: int) -> Response:
    """Set (or reset) the page's guide override and re-derive its regions."""
    page = get_object_or_404(Page.objects.select_related("book"), pk=page_id)
    data = request.data if isinstance(request.data, dict) else {}
    try:
        regions, enqueued = services.set_page_guides_override(page, data)
    except ValidationError as exc:
        return Response({"errors": list(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)
    except (services.ProcessingError, ValueError) as exc:  # ValueError: refused by books.run_stage
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
