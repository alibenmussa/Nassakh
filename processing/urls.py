"""Processing routes.

`urlpatterns` are mounted at /books/ (`processing:guides` at /books/<id>/guides/ only redirects to
the dashboard now). `api_urlpatterns` are mounted at /api/ in the shared `api` namespace (reverse as
`api:page_preprocess`, `api:page_guides_override`, `api:book_guides`, `api:book_guides_preview`).
"""

from django.urls import path

from . import api, views

app_name = "processing"

urlpatterns = [
    path("<int:book_id>/guides/", views.guides, name="guides"),
]

api_urlpatterns = [
    path("pages/<int:page_id>/preprocess/", api.page_preprocess, name="page_preprocess"),
    path("pages/<int:page_id>/guides/", api.page_guides_override, name="page_guides_override"),
    path("books/<int:book_id>/guides/", api.book_guides, name="book_guides"),
    path("books/<int:book_id>/guides/preview/", api.book_guides_preview, name="book_guides_preview"),
]
