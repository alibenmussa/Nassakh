"""Function-based views of the books app: list, new book, dashboard, page detail and actions.

Views parse the request, call a service and render or redirect. Reading screens need a login;
actions that change a book need the `editor` role (admins and superusers always pass).
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from books import services
from books.forms import BookForm
from books.models import Book, Page
from core.decorators import role_required


@login_required
def book_list(request: HttpRequest) -> HttpResponse:
    """Books table with status, progress and last update; empty state explains how to start."""
    return render(request, "books/list.html", {"rows": services.books_overview()})


@role_required("editor")
def book_create(request: HttpRequest) -> HttpResponse:
    """New-book form; on success the PDF is stored and inspected, then the dashboard opens."""
    if request.method == "POST":
        form = BookForm(request.POST, request.FILES)
        if form.is_valid():
            data = {key: value for key, value in form.cleaned_data.items() if key != "source_pdf"}
            book = services.create_book(data, form.cleaned_data["source_pdf"], request.user)
            if book.status == Book.Status.ERROR:
                messages.error(request, (book.error_message or "").splitlines()[0])
            else:
                messages.success(
                    request,
                    f"أُنشئ الكتاب «{book.title}» ({book.source_page_count} صفحة في الملف). "
                    "اضغط «بدء المعالجة» لاستخراج الصفحات.",
                )
            return redirect("books:detail", book.pk)
    else:
        form = BookForm()
    return render(request, "books/form.html", {"form": form})


@login_required
def book_detail(request: HttpRequest, book_id: int) -> HttpResponse:
    """Book dashboard: header, actions, stage progress, attention list and the pages grid."""
    book = get_object_or_404(Book, pk=book_id)
    return render(request, "books/detail.html", services.book_dashboard(book))


@login_required
def page_detail(request: HttpRequest, book_id: int, number: int) -> HttpResponse:
    """One page: image tabs with region overlay, preprocessing and text panels, runs, re-run menu."""
    page = get_object_or_404(Page.objects.select_related("book"), book_id=book_id, number=number)
    return render(request, "books/page_detail.html", services.page_detail_context(page))


@role_required("editor")
@require_POST
def toggle_exclude(request: HttpRequest, book_id: int, number: int) -> HttpResponse:
    """Exclude a page from the book or bring it back, then return to where the user was."""
    page = get_object_or_404(Page.objects.select_related("book"), book_id=book_id, number=number)
    page = services.toggle_exclude(page)
    if page.is_excluded:
        messages.success(request, f"استُثنيت الصفحة {page.number} من الكتاب.")
    else:
        messages.success(request, f"أُعيدت الصفحة {page.number} إلى الكتاب.")
    return _redirect_back(request, book_id)


@role_required("editor")
@require_POST
def start(request: HttpRequest, book_id: int) -> HttpResponse:
    """Start the pipeline (ingest → preprocess → guides → layout → OCR) for a book."""
    book = get_object_or_404(Book, pk=book_id)
    try:
        services.start_processing(book)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "بدأت المعالجة. تُحدَّث هذه الصفحة تلقائيًا أثناء العمل.")
    return redirect("books:detail", book.pk)


@role_required("editor")
@require_POST
def rerun(request: HttpRequest, book_id: int, number: int | None = None) -> HttpResponse:
    """Re-run the pipeline from `?stage=` for the whole book, or for one page when `number` is given."""
    book = get_object_or_404(Book, pk=book_id)
    stage = request.POST.get("stage") or request.GET.get("stage") or ""
    if stage not in services.STAGES:
        messages.error(request, "اختر مرحلة صحيحة لإعادة التشغيل.")
        return _redirect_back(request, book_id, number)

    label = services.STAGE_LABELS[stage]
    if number is None:
        from books.tasks import rerun_book_from

        rerun_book_from.delay(book.pk, stage)
        messages.success(request, f"أُعيد تشغيل الكتاب من مرحلة «{label}».")
        return redirect("books:detail", book.pk)

    page = get_object_or_404(Page, book=book, number=number)
    try:
        services.run_stage(page, stage)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"أُعيد تشغيل الصفحة {page.number} من مرحلة «{label}».")
    return redirect("books:page_detail", book.pk, page.number)


def _redirect_back(request: HttpRequest, book_id: int, number: int | None = None) -> HttpResponse:
    """Redirect to a same-site `next` when given, else to the page detail or the dashboard."""
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    if number is not None:
        return redirect("books:page_detail", book_id, number)
    return redirect("books:detail", book_id)
