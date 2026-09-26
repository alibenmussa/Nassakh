"""Books routes.

`urlpatterns` are mounted at /books/ (reverse as `books:<name>`); `api_urlpatterns` are mounted
at /api/ together with the other apps' API routes (reverse as `api:<name>`, names unique across
apps). `rerun` answers at book level (`/books/<id>/rerun/`) and at page level
(`/books/<id>/pages/<n>/rerun/`) under the same name; `reverse` picks the pattern by its arguments.
"""

from django.urls import path

from . import api, views

app_name = "books"

urlpatterns = [
    path("", views.book_list, name="list"),
    path("new/", views.book_create, name="create"),
    path("<int:book_id>/", views.book_detail, name="detail"),
    path("<int:book_id>/start/", views.start, name="start"),
    path("<int:book_id>/start-ocr/", views.start_ocr, name="start_ocr"),
    path("<int:book_id>/delete/", views.delete, name="delete"),
    path("<int:book_id>/rerun/", views.rerun, name="rerun"),
    path("<int:book_id>/pages/<int:number>/", views.page_detail, name="page_detail"),
    path("<int:book_id>/pages/<int:number>/exclude/", views.toggle_exclude, name="toggle_exclude"),
    path("<int:book_id>/pages/<int:number>/rerun/", views.rerun, name="rerun"),
]

api_urlpatterns = [
    path("books/<int:book_id>/progress/", api.book_progress, name="book_progress"),
    path("books/<int:book_id>/text/", api.book_text, name="book_text"),
    path("books/<int:book_id>/sheets/", api.book_sheets, name="book_sheets"),
    path("books/<int:book_id>/stages/", api.book_stages, name="book_stages"),
    path("pages/<int:page_id>/status/", api.page_status, name="page_status"),
]
