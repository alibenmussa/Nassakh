"""JSON endpoints of the ocr app (DRF function views, session auth). A page of another organisation's book
answers 404 (`books.access`, D102)."""

from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from books.access import get_page_or_404
from books.models import Page

from . import services


@api_view(["GET"])
def page_text(request: Request, page_id: int) -> Response:
    """GET /api/pages/<id>/text/ — text state, provisional/final text and the lines with tokens."""
    page = get_page_or_404(request.user, pk=page_id)
    return Response(services.page_text_payload(page))


@api_view(["GET"])
def page_runs(request: Request, page_id: int) -> Response:
    """GET /api/pages/<id>/runs/ — engine runs of the page, newest first."""
    page = get_page_or_404(request.user, Page.objects.all(), pk=page_id)
    return Response({"page_id": page.pk, "runs": services.page_runs_payload(page)})
