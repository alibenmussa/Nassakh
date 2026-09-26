"""Publishing routes.

`urlpatterns` are mounted at /books/ (reverse as `publishing:<name>`): the export page and the download of
an export's file (PHASE6_SPEC §6.5). `api_urlpatterns` at /api/ in the shared `api` namespace: the page
preview and the live pages (the pages that show them are the editor app's `editor:layout`), and exports.
"""

from django.urls import path

from . import api, views

app_name = "publishing"

urlpatterns = [
    path("<int:book_id>/export/", views.export_page, name="export"),
    path("<int:book_id>/exports/<int:export_id>/download/", views.export_download, name="export_download"),
]

api_urlpatterns = [
    path("books/<int:book_id>/preview/", api.preview, name="preview"),
    path("books/<int:book_id>/preview/layout/", api.preview_layout, name="preview_layout"),
    path("books/<int:book_id>/chapters/<str:chapter_id>/relayout/", api.relayout_chapter, name="relayout"),
    path("books/<int:book_id>/relayout/<int:render_id>/", api.relayout_status, name="relayout_status"),
    path("books/<int:book_id>/exports/", api.book_exports, name="exports"),
    path("books/<int:book_id>/exports/<int:export_id>/", api.book_export, name="export"),
    path("books/<int:book_id>/exports/<int:export_id>/cancel/", api.book_export_cancel, name="export_cancel"),
]
