"""Review routes.

`urlpatterns` are mounted at /books/ (reverse as `review:<name>`); `api_urlpatterns` are mounted
at /api/ in the shared `api` namespace (reverse as `api:<name>`, names unique across apps).
"""

from django.urls import path

from . import api, views

app_name = "review"

urlpatterns = [
    path("<int:book_id>/review/next/", views.review_next, name="next"),
    path("<int:book_id>/review/<int:number>/", views.review_page, name="page"),
]

api_urlpatterns = [
    path("pages/<int:page_id>/review/", api.page_review, name="page_review"),
    path("pages/<int:page_id>/lines/", api.page_lines, name="page_lines"),
    path("pages/<int:page_id>/undo/", api.page_undo, name="page_undo"),
    path("pages/<int:page_id>/approve/", api.page_approve, name="page_approve"),
    path("pages/<int:page_id>/reopen/", api.page_reopen, name="page_reopen"),
    path("pages/<int:page_id>/roles/", api.page_roles, name="page_roles"),
    path("pages/<int:page_id>/insertions/<int:group>/", api.page_insertion, name="page_insertion"),
    path("gaps/<int:gap_id>/accept/", api.gap_accept, name="gap_accept"),
    path("gaps/<int:gap_id>/dismiss/", api.gap_dismiss, name="gap_dismiss"),
    path("lines/<int:line_id>/resolve/", api.line_resolve, name="line_resolve"),
    path("lines/<int:line_id>/edit/", api.line_edit, name="line_edit"),
    path("lines/<int:line_id>/delete/", api.line_delete, name="line_delete"),
    path("lines/<int:line_id>/merge/", api.line_merge, name="line_merge"),
    path("lines/<int:line_id>/role/", api.line_role, name="line_role"),
    path("lines/<int:line_id>/delete-word/", api.line_delete_word, name="line_delete_word"),
    path("books/<int:book_id>/filmstrip/", api.book_filmstrip, name="book_filmstrip"),
]
