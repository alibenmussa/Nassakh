"""Processing routes.

`urlpatterns` are mounted at /books/ (so `guides` lives at /books/<id>/guides/; reverse as
`processing:guides`). `api_urlpatterns` are mounted at /api/ in the shared `api` namespace
(reverse as `api:page_preprocess`, `api:page_guides_override`).
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
]
