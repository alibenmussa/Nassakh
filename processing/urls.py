"""Processing routes.

`api_urlpatterns` are mounted at /api/ in the shared `api` namespace (reverse as
`api:page_guides_override`, `api:book_guides`, `api:book_guides_preview`). The app has no HTML routes:
the «التخطيط» mode lives at `books:guides` (D84).
"""

from django.urls import path

from . import api

app_name = "processing"

api_urlpatterns = [
    path("pages/<int:page_id>/guides/", api.page_guides_override, name="page_guides_override"),
    path("books/<int:book_id>/guides/", api.book_guides, name="book_guides"),
    path("books/<int:book_id>/guides/preview/", api.book_guides_preview, name="book_guides_preview"),
]
