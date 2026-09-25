"""Editor routes (PHASE5_SPEC §3).

`urlpatterns` are mounted at /books/ (reverse as `editor:<name>`): the chapter editor and the layout
page (stylesheet + page preview). `api_urlpatterns` at /api/ in the shared `api` namespace.
"""

from django.urls import path

from . import api, views

app_name = "editor"

urlpatterns = [
    path("<int:book_id>/editor/", views.edit, name="edit"),
    path("<int:book_id>/layout/", views.layout, name="layout"),
]

api_urlpatterns = [
    path("books/<int:book_id>/chapters/", api.chapters, name="chapters"),
    path("books/<int:book_id>/chapters/<str:chapter_id>/", api.chapter, name="chapter"),
    path(
        "books/<int:book_id>/chapters/<str:chapter_id>/reassemble/",
        api.chapter_reassemble,
        name="chapter_reassemble",
    ),
    path("books/<int:book_id>/find-replace/", api.find_replace, name="find_replace"),
    path("books/<int:book_id>/convert-digits/", api.convert_digits, name="convert_digits"),
    path("books/<int:book_id>/snapshots/", api.snapshots, name="snapshots"),
    path(
        "books/<int:book_id>/snapshots/<int:snapshot_id>/restore/",
        api.snapshot_restore,
        name="snapshot_restore",
    ),
    path("books/<int:book_id>/stylesheet/", api.stylesheet, name="stylesheet"),
]
