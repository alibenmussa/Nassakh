"""Login and logout as function-based views around `django.contrib.auth`; the organisation's page (D98):
its fonts (upload, rename, remove, restore, delete) and its format templates (from a book, rename, update,
delete, apply to a book after a preview of the changes); the organisation's font files. The page is the
organisation of the user's membership; a user without one gets 403 «لا تنتمي إلى مؤسسة بعد…»; a superuser
works in the first organisation or the one chosen with the switcher (`organization_switch`, D102)."""

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST

from accounts import fonts as org_fonts
from accounts import styles
from accounts.forms import LoginForm
from accounts.models import FONT_STYLE_LABELS, FONT_STYLES, Organization, OrganizationFont, StyleTemplate
from accounts.services import (
    NOT_A_MEMBER,
    ORGANIZATION_SESSION_KEY,
    can_edit_books,
    current_organization,
    is_member,
    is_org_admin,
    safe_next_url,
)

FONT_TYPES = {"ttf": "font/ttf", "otf": "font/otf"}
ADMINS_ONLY = "هذا الإجراء لمدير المؤسسة."
EDITORS_ONLY = "هذا الإجراء يتطلب صلاحية محرّر في المؤسسة."


@sensitive_post_parameters("password")
@csrf_protect
@never_cache
def login(request: HttpRequest) -> HttpResponse:
    """Show the login card; on success redirect to `?next=` (when local) or LOGIN_REDIRECT_URL."""
    next_url = safe_next_url(request)
    if request.user.is_authenticated:
        return redirect(next_url or settings.LOGIN_REDIRECT_URL)
    if request.method == "POST":
        form = LoginForm(request, data=request.POST)
        if form.is_valid():
            auth_login(request, form.get_user())
            return redirect(next_url or settings.LOGIN_REDIRECT_URL)
    else:
        form = LoginForm(request)
    return render(request, "accounts/login.html", {"form": form, "next": next_url})


@require_POST
def logout(request: HttpRequest) -> HttpResponse:
    """End the session (POST only) and return to the login page."""
    auth_logout(request)
    return redirect(settings.LOGOUT_REDIRECT_URL)


# ====================================================================== the organisation's page


def _organization(request: HttpRequest) -> Organization:
    organization = current_organization(request)
    if organization is None:
        raise PermissionDenied(NOT_A_MEMBER)
    return organization


def _admin(request: HttpRequest) -> Organization:
    organization = _organization(request)
    if not is_org_admin(request.user, organization):
        raise PermissionDenied(ADMINS_ONLY)
    return organization


def _editor(request: HttpRequest) -> Organization:
    organization = _organization(request)
    if not can_edit_books(request.user, organization):
        raise PermissionDenied(EDITORS_ONLY)
    return organization


def _page_url(anchor: str = "") -> str:
    return reverse("accounts:organization") + (f"#{anchor}" if anchor else "")


def _book_of(organization: Organization, value):
    from books.models import Book

    try:
        book_id = int(value)
    except (TypeError, ValueError):
        return None
    return Book.objects.filter(pk=book_id, organization=organization).first()


def _font_rows(organization: Organization) -> tuple[list[dict], list[dict]]:
    """The faces of the page: `(active, removed)`, each `{font, styles, books, used, templates}`."""
    active: list[dict] = []
    removed: list[dict] = []
    for font in OrganizationFont.objects.filter(organization=organization).order_by("name", "id"):
        books = list(org_fonts.books_using(font)[:6])
        used = org_fonts.books_using(font).count() if len(books) == 6 else len(books)
        row = {
            "font": font,
            "styles": [FONT_STYLE_LABELS[style] for style in FONT_STYLES if font.file_of(style)],
            "books": books[:5],
            "more": max(0, used - 5),
            "used": used,
            "used_phrase": org_fonts.books_phrase(used) if used else "",
            "templates": [template.name for template in org_fonts.templates_using(font)],
            "licence_line": next((line for line in (font.licence or "").splitlines() if line.strip()), ""),
        }
        (removed if font.removed else active).append(row)
    return active, removed


@login_required
@require_GET
def organization(request: HttpRequest) -> HttpResponse:
    """The organisation's page: its fonts and its format templates (see the module docstring)."""
    from books.models import Book
    from publishing.fonts import org_sample_css

    organization = _organization(request)
    active, removed = _font_rows(organization)
    templates = [
        {"template": template, "summary": " · ".join(styles.summary(template))}
        for template in StyleTemplate.objects.filter(organization=organization)
        .select_related("source_book", "updated_by")
        .order_by("name", "id")
    ]
    books = list(Book.objects.filter(organization=organization).order_by("title", "id").only("id", "title"))
    # a superuser works in any organisation (D102): the switcher lists them all when there are several
    switchable = list(Organization.objects.order_by("name", "id")) if request.user.is_superuser else []
    return render(
        request,
        "accounts/organization.html",
        {
            "organization": organization,
            "organizations": switchable if len(switchable) > 1 else [],
            "is_org_admin": is_org_admin(request.user, organization),
            "can_edit_books": can_edit_books(request.user, organization),
            "fonts": active,
            "removed_fonts": removed,
            "templates": templates,
            "books": books,
            "sample_css": org_sample_css(organization),
            "max_mb": org_fonts.max_bytes() // (1024 * 1024),
            "accept": ",".join(org_fonts.EXTENSIONS),
            "members": organization.memberships.count(),
        },
    )


@login_required
@require_POST
def organization_rename(request: HttpRequest) -> HttpResponse:
    """Rename the organisation (its admins)."""
    organization = _admin(request)
    name = " ".join(str(request.POST.get("name") or "").split())
    if not name or len(name) > 200:
        messages.error(request, "اكتب اسمًا للمؤسسة (200 حرف على الأكثر).")
    else:
        organization.name = name
        organization.save(update_fields=["name"])
        messages.success(request, "حُفظ اسم المؤسسة.")
    return redirect(_page_url())


@login_required
@require_POST
def organization_switch(request: HttpRequest) -> HttpResponse:
    """A superuser works in another organisation (D102): its page, its fonts and templates, and the
    organisation of the books they add, for the rest of the session. Superusers see every book anyway."""
    if not request.user.is_superuser:
        raise PermissionDenied(ADMINS_ONLY)
    raw = str(request.POST.get("organization") or "")
    organization = get_object_or_404(Organization, pk=int(raw) if raw.isascii() and raw.isdigit() else 0)
    request.session[ORGANIZATION_SESSION_KEY] = organization.pk
    messages.success(request, f"تعمل الآن في «{organization.name}».")
    return redirect(_page_url())


# ---------------------------------------------------------------- fonts


@login_required
@require_POST
def font_upload(request: HttpRequest) -> HttpResponse:
    """Add font files (the organisation's admins): one face per family, see `accounts.fonts.add_fonts`."""
    organization = _admin(request)
    try:
        added = org_fonts.add_fonts(
            organization,
            request.FILES.getlist("files"),
            user=request.user,
            name=request.POST.get("name", ""),
            licence=request.POST.get("licence", ""),
            confirmed=request.POST.get("confirm") in ("on", "1", "true"),
        )
    except org_fonts.FontRefused as exc:
        for message in exc.messages:
            messages.error(request, message)
        return redirect(_page_url("fonts"))
    names = "، ".join(f"«{font.name}»" for font in added)
    messages.success(request, f"أُضيف إلى خطوط المؤسسة: {names}. يظهر في قائمة الخطوط في «التنسيق».")
    return redirect(_page_url("fonts"))


def _font(organization: Organization, font_id: int) -> OrganizationFont:
    return get_object_or_404(OrganizationFont, pk=font_id, organization=organization)


@login_required
@require_POST
def font_edit(request: HttpRequest, font_id: int) -> HttpResponse:
    """Rename a face and change its licence notice (the organisation's admins)."""
    font = _font(_admin(request), font_id)
    try:
        org_fonts.rename_font(font, request.POST.get("name", ""), request.POST.get("licence"))
    except org_fonts.FontRefused as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"حُفظ الخط «{font.name}».")
    return redirect(_page_url("fonts"))


@login_required
@require_POST
def font_remove(request: HttpRequest, font_id: int) -> HttpResponse:
    """Take a face off the menus (soft): its books fall back to Amiri with a notice."""
    font = _font(_admin(request), font_id)
    used = org_fonts.books_using(font).count()
    org_fonts.remove_font(font, request.user)
    tail = (
        f" الكتب التي تستعمله ({org_fonts.books_phrase(used)}) تُرتَّب الآن بخط أميري حتى يُختار لها خط آخر."
        if used
        else ""
    )
    messages.success(request, f"حُذف الخط «{font.name}» من خطوط المؤسسة.{tail}")
    return redirect(_page_url("fonts"))


@login_required
@require_POST
def font_restore(request: HttpRequest, font_id: int) -> HttpResponse:
    """Bring a removed face back."""
    font = _font(_admin(request), font_id)
    org_fonts.restore_font(font)
    messages.success(request, f"أُعيد الخط «{font.name}» إلى خطوط المؤسسة.")
    return redirect(_page_url("fonts"))


@login_required
@require_POST
def font_delete(request: HttpRequest, font_id: int) -> HttpResponse:
    """Delete a removed face no book uses, with its files."""
    font = _font(_admin(request), font_id)
    name = font.name
    try:
        org_fonts.delete_font(font)
    except org_fonts.FontRefused as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"حُذف الخط «{name}» وملفاته نهائيًا.")
    return redirect(_page_url("fonts"))


@login_required
@require_GET
def font_file(request: HttpRequest, font_id: int, filename: str) -> HttpResponse:
    """A file of an organisation's face, to its members only (the live pages and the font menus load it;
    `/media/` never serves `orgs/`). The address names the file by its content: cached for good."""
    font = OrganizationFont.objects.filter(pk=font_id).select_related("organization").first()
    if font is None or not is_member(request.user, font.organization):
        raise Http404("font not found")
    names = {font.file_of(style).name.rsplit("/", 1)[-1]: font.file_of(style) for style in font.styles()}
    field_file = names.get(filename)
    if field_file is None:
        raise Http404("font not found")
    try:
        handle = default_storage.open(field_file.name, "rb")
    except (FileNotFoundError, OSError):
        raise Http404("font not found") from None
    extension = filename.rsplit(".", 1)[-1].lower()
    response = FileResponse(handle, content_type=FONT_TYPES.get(extension, "application/octet-stream"))
    response["Cache-Control"] = "private, max-age=31536000, immutable"
    response["X-Content-Type-Options"] = "nosniff"
    return response


# ---------------------------------------------------------------- templates


@login_required
@require_POST
def template_create(request: HttpRequest) -> HttpResponse:
    """A new template from a book's current stylesheet (the organisation's editors)."""
    organization = _editor(request)
    book = _book_of(organization, request.POST.get("book"))
    if book is None:
        messages.error(request, "اختر كتابًا من كتب المؤسسة يؤخذ منه التنسيق.")
        return redirect(_page_url("templates"))
    try:
        template = styles.create_template(
            organization, book, request.POST.get("name"), request.POST.get("description", ""), request.user
        )
    except styles.TemplateError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"حُفظ القالب «{template.name}» من تنسيق «{book.title}».")
    return redirect(_page_url("templates"))


def _template(organization: Organization, template_id: int) -> StyleTemplate:
    return get_object_or_404(StyleTemplate, pk=template_id, organization=organization)


@login_required
@require_POST
def template_edit(request: HttpRequest, template_id: int) -> HttpResponse:
    """Rename a template and change its description (the organisation's admins)."""
    template = _template(_admin(request), template_id)
    try:
        styles.rename_template(template, request.POST.get("name"), request.POST.get("description", ""))
    except styles.TemplateError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"حُفظ القالب «{template.name}».")
    return redirect(_page_url("templates"))


@login_required
@require_POST
def template_update(request: HttpRequest, template_id: int) -> HttpResponse:
    """Take a book's current stylesheet into a template (the organisation's admins)."""
    organization = _admin(request)
    template = _template(organization, template_id)
    book = _book_of(organization, request.POST.get("book"))
    if book is None:
        messages.error(request, "اختر كتابًا من كتب المؤسسة يؤخذ منه التنسيق.")
        return redirect(_page_url("templates"))
    styles.update_from_book(template, book, request.user)
    messages.success(request, f"حُدّث القالب «{template.name}» من تنسيق «{book.title}».")
    return redirect(_page_url("templates"))


@login_required
@require_POST
def template_delete(request: HttpRequest, template_id: int) -> HttpResponse:
    """Delete a template (the organisation's admins); the books keep their stylesheets."""
    template = _template(_admin(request), template_id)
    name = template.name
    styles.delete_template(template)
    messages.success(request, f"حُذف القالب «{name}». الكتب التي طُبّق عليها تبقى على تنسيقها.")
    return redirect(_page_url("templates"))


@login_required
def template_apply(request: HttpRequest, template_id: int) -> HttpResponse:
    """GET: the changes a template brings to a book (`?book=`), field by field; POST: apply them (the
    organisation's editors), then open the book's «التنسيق»."""
    from books.models import Book
    from editor.services import StyleSheetError
    from publishing import engine

    organization = _editor(request)
    template = _template(organization, template_id)
    if request.method == "POST":
        book = _book_of(organization, request.POST.get("book"))
        if book is None:
            raise Http404("book not found")
        try:
            result = styles.apply_template(book, template, request.user)
        except (styles.TemplateError, StyleSheetError) as exc:
            messages.error(request, str(exc))
            return redirect(reverse("accounts:template_apply", args=[template.pk]) + f"?book={book.pk}")
        if result["changed"]:
            engine.request_preview(book, "book")
        for item in result["skipped"]:
            messages.warning(request, f"لم يُطبَّق {item['label']} «{item['name']}»: {item['message']}")
        messages.success(request, f"طُبّق القالب «{template.name}» على «{book.title}».")
        return redirect(reverse("editor:layout", args=[book.pk]) + "?tab=format")
    if request.method != "GET":
        return HttpResponse(status=405)
    books = list(Book.objects.filter(organization=organization).order_by("title", "id").only("id", "title"))
    book = _book_of(organization, request.GET.get("book"))
    preview = styles.template_changes(book, template) if book is not None else None
    return render(
        request,
        "accounts/template_apply.html",
        {
            "organization": organization,
            "template": template,
            "summary": " · ".join(styles.summary(template)),
            "books": books,
            "book": book,
            "preview": preview,
        },
    )
