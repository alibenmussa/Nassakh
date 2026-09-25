"""Publishing routes: `api_urlpatterns` mounted at /api/ in the shared `api` namespace (the pages that
show the preview are the editor app's `editor:layout`)."""

from django.urls import path

from . import api

api_urlpatterns = [
    path("books/<int:book_id>/preview/", api.preview, name="preview"),
]
