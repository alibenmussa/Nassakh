"""JSON endpoints of the organisation's templates for the book page («التنسيق» → «قوالب المؤسسة», D98).

- GET  /api/books/<id>/templates/               → `{organization, templates: [{id, name, description, summary,
                                                  source, updated_at, changes, skipped, same}], can_save,
                                                  can_manage, manage_url}` (what each template changes in
                                                  this book, `accounts.styles`)
- POST /api/books/<id>/templates/               `{name, description?}` → 201 the template, from the book's
                                                  current stylesheet (editors of the book's organisation)
- POST /api/books/<id>/templates/<tid>/update/  → the template, taken again from this book (its admins)
- POST /api/books/<id>/templates/<tid>/apply/   `{chapter?}` → the stylesheet's answer, as its PUT gives it
                                                  (`stylesheet_payload` + `preview` + `cover_render`) +
                                                  `applied: {id, name, changed, skipped}`

Refusals answer `{"detail": <Arabic>}`: 400 bad input, 403 not the book's organisation or not allowed, 404.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response

from books.models import Book

from . import styles
from .models import StyleTemplate
from .services import book_organization, can_edit_books, is_member, is_org_admin

NOT_YOURS = "هذا الكتاب لا ينتمي إلى مؤسستك."
EDITORS_ONLY = "هذا الإجراء يتطلب صلاحية محرّر في المؤسسة."
ADMINS_ONLY = "هذا الإجراء لمدير المؤسسة."


def _data(request: Request) -> dict:
    return request.data if isinstance(request.data, dict) else {}


def _book(request: Request, book_id: int):
    book = Book.objects.filter(pk=book_id).select_related("organization").first()
    if book is None:
        raise NotFound("الكتاب غير موجود.")
    organization = book_organization(book)
    if organization is not None and not is_member(request.user, organization):
        raise PermissionDenied(NOT_YOURS)
    return book, organization


def _template_payload(book, template: StyleTemplate) -> dict:
    changes = styles.template_changes(book, template)
    source = template.source_book
    return {
        "id": template.pk,
        "name": template.name,
        "description": template.description,
        "summary": styles.summary(template),
        "source": {"id": source.pk, "title": source.title} if source is not None else None,
        "updated_at": template.updated_at.isoformat() if template.updated_at else None,
        **changes,
    }


@api_view(["GET", "POST"])
def book_templates(request: Request, book_id: int) -> Response:
    """The organisation's templates with what each changes in this book (GET); a new one from it (POST)."""
    from django.urls import reverse

    book, organization = _book(request, book_id)
    if request.method == "POST":
        if organization is None or not can_edit_books(request.user, organization):
            raise PermissionDenied(EDITORS_ONLY)
        data = _data(request)
        try:
            template = styles.create_template(
                organization, book, data.get("name"), data.get("description", ""), request.user
            )
        except styles.TemplateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_template_payload(book, template), status=status.HTTP_201_CREATED)
    rows = (
        StyleTemplate.objects.filter(organization=organization)
        .select_related("source_book")
        .order_by("name", "id")
        if organization is not None
        else []
    )
    return Response(
        {
            "organization": {"id": organization.pk, "name": organization.name} if organization else None,
            "templates": [_template_payload(book, template) for template in rows],
            "can_save": organization is not None and can_edit_books(request.user, organization),
            "can_manage": organization is not None and is_org_admin(request.user, organization),
            "manage_url": reverse("accounts:organization") + "#templates",
        }
    )


def _template(organization, template_id: int) -> StyleTemplate:
    template = (
        StyleTemplate.objects.filter(pk=template_id, organization=organization).first()
        if organization
        else None
    )
    if template is None:
        raise NotFound("القالب غير موجود.")
    return template


@api_view(["POST"])
def book_template_update(request: Request, book_id: int, template_id: int) -> Response:
    """Take this book's current stylesheet into the template (the organisation's admins)."""
    book, organization = _book(request, book_id)
    template = _template(organization, template_id)
    if not is_org_admin(request.user, organization):
        raise PermissionDenied(ADMINS_ONLY)
    styles.update_from_book(template, book, request.user)
    return Response(_template_payload(book, template))


@api_view(["POST"])
def book_template_apply(request: Request, book_id: int, template_id: int) -> Response:
    """Apply a template to the book through the stylesheet's validation and save, queue the renders, and
    answer as the stylesheet's PUT does (the book page takes the answer the same way)."""
    from editor.services import StyleSheetError, cover_after_save, stylesheet_payload
    from publishing import engine
    from publishing.preview import PreviewNotFound

    book, organization = _book(request, book_id)
    template = _template(organization, template_id)
    if not can_edit_books(request.user, organization):
        raise PermissionDenied(EDITORS_ONLY)
    try:
        result = styles.apply_template(book, template, request.user)
    except styles.TemplateError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except StyleSheetError as exc:
        return Response({"detail": str(exc), "errors": exc.errors}, status=status.HTTP_400_BAD_REQUEST)
    data = _data(request)
    chapter_id = str(data.get("chapter") or "") or None
    engine.request_preview(book, "book")
    if chapter_id:
        engine.request_preview(book, "chapter", chapter_id)
    payload = stylesheet_payload(book)
    try:
        payload["preview"] = engine.preview_payload(book, "book", enqueue=False)
    except PreviewNotFound:
        payload["preview"] = None
    payload["cover_render"] = cover_after_save(book, {})  # a template carries no cover: its render follows
    payload["applied"] = {
        "id": template.pk,
        "name": template.name,
        "changed": result["changed"],
        "skipped": result["skipped"],
    }
    return Response(payload)
