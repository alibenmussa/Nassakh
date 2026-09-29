"""Function-based views of the books app: list, new book, dashboard, page detail and actions.

Views parse the request, call a service and render or redirect. Reading screens need a login;
actions that change a book need the `editor` role (admins and superusers always pass).
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
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
    """New-book form: «استخراج الصفحات» stores and inspects the PDF, starts the extraction at once and
    opens the dashboard in «التخطيط» (D66); the message gives the exact kept range. If the extraction
    cannot be queued the book stays `uploaded` and its dashboard offers «استخراج الصفحات»."""
    if request.method == "POST":
        form = BookForm(request.POST, request.FILES)
        if form.is_valid():
            data = {key: value for key, value in form.cleaned_data.items() if key != "source_pdf"}
            book = services.create_book(data, form.cleaned_data["source_pdf"], request.user)
            if book.status == Book.Status.ERROR:
                messages.error(request, (book.error_message or "").splitlines()[0])
                return redirect("books:detail", book.pk)
            try:
                services.start_processing(book)
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, services.extraction_message(book))
            return redirect("books:detail", book.pk)
    else:
        form = BookForm()
    return render(request, "books/form.html", {"form": form})


@login_required
def book_detail(request: HttpRequest, book_id: int) -> HttpResponse:
    """Book dashboard: header, actions, stage progress, attention list and the pages grid.

    It opens in the «التخطيط» mode while the book awaits «بدء المعالجة»; once «المعالجة» started that mode
    has its own address, `/books/<id>/guides/` (`book_guides`, D84); the old `?view=guides` goes there.
    """
    book = get_object_or_404(Book, pk=book_id)
    if request.GET.get("view") == services.GUIDES_VIEW:
        return redirect(services.guides_url(book), permanent=True)
    return render(request, "books/detail.html", services.book_dashboard(book))


@login_required
def book_guides(request: HttpRequest, book_id: int) -> HttpResponse:
    """The «التخطيط» mode of the dashboard at its own address (D84): every page as a sheet with its guides;
    `#sheet-<n>` opens at a page. Before «بدء المعالجة» the dashboard itself is this mode."""
    book = get_object_or_404(Book, pk=book_id)
    if book.awaits_ocr_start:
        return redirect("books:detail", book.pk)
    return render(request, "books/detail.html", services.book_dashboard(book, services.GUIDES_VIEW))


@role_required("editor")
@require_POST
def toggle_exclude(request: HttpRequest, book_id: int, number: int) -> HttpResponse:
    """Exclude a page from the book or bring it back, then return to where the user was."""
    page = get_object_or_404(Page.objects.select_related("book"), book_id=book_id, number=number)
    page = services.toggle_exclude(page)
    undo = "undo:" + reverse("books:toggle_exclude", args=[book_id, number])  # the toast's «تراجع»
    if page.is_excluded:
        messages.success(request, f"استُثنيت الصفحة {page.number} من الكتاب.", extra_tags=undo)
    else:
        messages.success(request, f"أُعيدت الصفحة {page.number} إلى الكتاب.", extra_tags=undo)
    return _redirect_back(request, book_id)


@role_required("editor")
@require_POST
def start(request: HttpRequest, book_id: int) -> HttpResponse:
    """«استخراج الصفحات» (and «إعادة استخراج الصفحات» from error): ingest → preprocess, then the pause
    for a book in «التخطيط»; a book whose «المعالجة» had started goes on through OCR, as today."""
    book = get_object_or_404(Book, pk=book_id)
    try:
        services.start_processing(book)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            "بدأ استخراج الصفحات. تُحدَّث هذه الصفحة تلقائيًا."
            if book.awaits_ocr_start
            else "بدأت المعالجة. تُحدَّث هذه الصفحة تلقائيًا أثناء العمل.",
        )
    return redirect("books:detail", book.pk)


@role_required("editor")
@require_POST
def start_ocr(request: HttpRequest, book_id: int) -> HttpResponse:
    """«بدء المعالجة» (D64): send the prepared pages into «المعالجة», then today's dashboard."""
    book = get_object_or_404(Book, pk=book_id)
    try:
        services.start_ocr(book)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "بدأت المعالجة. تُحدَّث هذه الصفحة تلقائيًا أثناء العمل.")
    return redirect("books:detail", book.pk)


@role_required("editor")
@require_POST
def delete(request: HttpRequest, book_id: int) -> HttpResponse:
    """«حذف الكتاب»: delete the book, its rows and its files (the dialog confirmed it), then the list."""
    book = get_object_or_404(Book, pk=book_id)
    title = services.delete_book(book)
    messages.success(request, f"حُذف الكتاب «{title}».")
    return redirect("books:list")


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
        try:
            services.validate_rerun(book, stage)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("books:detail", book.pk)

        from books.tasks import rerun_book_from

        kept = services.approved_page_count(book)
        rerun_book_from.delay(book.pk, stage)
        note = f" تُركت {kept} صفحة معتمدة كما هي." if kept else ""
        messages.success(request, f"أُعيد تشغيل الكتاب من مرحلة «{label}».{note}")
        return redirect("books:detail", book.pk)

    page = get_object_or_404(Page, book=book, number=number)
    try:
        services.run_stage(page, stage)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"أُعيد تشغيل الصفحة {page.number} من مرحلة «{label}».")
    return _redirect_back(request, book.pk, page.number)


def _redirect_back(request: HttpRequest, book_id: int, number: int | None = None) -> HttpResponse:
    """Redirect to a same-site `next` when given, else to the page's sheet or the dashboard."""
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    if number is not None:
        return redirect(services.sheet_url(book_id, number))
    return redirect("books:detail", book_id)
