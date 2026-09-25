"""Publishing routes: `api_urlpatterns` mounted at /api/ in the shared `api` namespace (the pages that
show the preview are the editor app's `editor:layout`)."""

from django.urls import path

from . import api

api_urlpatterns = [
    path("books/<int:book_id>/preview/", api.preview, name="preview"),
    path("books/<int:book_id>/preview/layout/", api.preview_layout, name="preview_layout"),
    path("books/<int:book_id>/chapters/<str:chapter_id>/relayout/", api.relayout_chapter, name="relayout"),
    path("books/<int:book_id>/relayout/<int:render_id>/", api.relayout_status, name="relayout_status"),
]
