"""OCR routes.

`urlpatterns` are mounted at /ocr/ (reverse as `ocr:<name>`; no HTML routes in Phase 2).
`api_urlpatterns` are mounted at /api/ in the shared `api` namespace (reverse as `api:<name>`,
names unique across apps).
"""

from django.urls import path

from . import api

app_name = "ocr"

urlpatterns: list = []

api_urlpatterns = [
    path("pages/<int:page_id>/text/", api.page_text, name="page_text"),
    path("pages/<int:page_id>/runs/", api.page_runs, name="page_runs"),
]
