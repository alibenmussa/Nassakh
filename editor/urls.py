"""Editor routes (PHASE5_SPEC §3).

`urlpatterns` are mounted at /books/ (reverse as `editor:<name>`): the book page (`editor:layout`, live
pages, preview and edit, D47) and the old editor address, which redirects to it. `api_urlpatterns` at
/api/ in the shared `api` namespace.
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
    path("books/<int:book_id>/drift/", api.review_drift, name="review_drift"),
    path("books/<int:book_id>/review-changes/", api.review_changes, name="review_changes"),
    path(
        "books/<int:book_id>/review-changes/<int:plan_id>/apply/",
        api.review_changes_apply,
        name="review_changes_apply",
    ),
    path("books/<int:book_id>/to-footnote/", api.to_footnote, name="to_footnote"),
    path("books/<int:book_id>/find-replace/", api.find_replace, name="find_replace"),
    path("books/<int:book_id>/convert-digits/", api.convert_digits, name="convert_digits"),
    path("books/<int:book_id>/snapshots/", api.snapshots, name="snapshots"),
    path(
        "books/<int:book_id>/snapshots/<int:snapshot_id>/restore/",
        api.snapshot_restore,
        name="snapshot_restore",
    ),
    path("books/<int:book_id>/stylesheet/", api.stylesheet, name="stylesheet"),
    path("books/<int:book_id>/images/", api.book_images, name="book_images"),
    path("books/<int:book_id>/cover/", api.cover, name="cover"),
    path("books/<int:book_id>/uncertain/", api.uncertain_words, name="uncertain"),
    path("books/<int:book_id>/uncertain/accept/", api.uncertain_accept, name="uncertain_accept"),
    path("books/<int:book_id>/uncertain/choose/", api.uncertain_choose, name="uncertain_choose"),
    path("books/<int:book_id>/uncertain/type/", api.uncertain_type, name="uncertain_type"),
]
