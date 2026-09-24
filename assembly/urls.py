"""Assembly routes.

`urlpatterns` are mounted at /books/ (reverse as `assembly:<name>`); `api_urlpatterns` at /api/ in
the shared `api` namespace (reverse as `api:<name>`, names unique across apps).
"""

from django.urls import path

from . import api, views

app_name = "assembly"

urlpatterns = [
    path("<int:book_id>/manuscript/", views.manuscript, name="manuscript"),
    path("<int:book_id>/manuscript/document/", views.document, name="document"),
]

api_urlpatterns = [
    path("books/<int:book_id>/assemble/", api.book_assemble, name="book_assemble"),
    path("books/<int:book_id>/manuscript/", api.manuscript, name="manuscript"),
    path("books/<int:book_id>/manuscript/state/", api.manuscript_state, name="manuscript_state"),
    path("books/<int:book_id>/manuscript/seams/", api.manuscript_seam, name="manuscript_seam"),
    path("books/<int:book_id>/manuscript/roles/", api.manuscript_roles, name="manuscript_roles"),
    path(
        "books/<int:book_id>/manuscript/suggestions/", api.manuscript_suggestion, name="manuscript_suggestion"
    ),
]
