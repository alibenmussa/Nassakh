"""Function-based views of the processing app: the guides screen."""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from books.models import Book, Page
from core.decorators import ROLE_EDITOR, role_required
from processing import services
from processing.models import LayoutGuides, Preprocess


def _percent(ratio: float | None) -> str:
    """Ratio → percentage string with one decimal and Western digits ('' for None)."""
    return "" if ratio is None else f"{ratio * 100:.1f}"


def _reference_page(book: Book, guides: LayoutGuides | None, requested: str | None) -> Page | None:
    """The page shown on the guides screen: `?page=<number>`, else the stored reference, else the first."""
    candidates = book.pages.filter(is_excluded=False, preprocess__isnull=False).select_related("preprocess")
    if requested and requested.isdigit():
        page = candidates.filter(number=int(requested)).first()
        if page is not None:
            return page
    if guides is not None and guides.reference_page_id:
        page = candidates.filter(pk=guides.reference_page_id).first()
        if page is not None:
            return page
    return candidates.order_by("number").first()


@role_required(ROLE_EDITOR)
def guides(request: HttpRequest, book_id: int) -> HttpResponse:
    """Show the reference page with draggable guide lines (GET) or apply the posted guides (POST)."""
    book = get_object_or_404(Book, pk=book_id)

    if request.method == "POST":
        try:
            guides_obj = services.apply_guides(book, request.POST, request.user)
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
            return redirect(request.get_full_path())
        n_pages = book.pages.filter(is_excluded=False, preprocess__isnull=False).count()
        messages.success(
            request, f"طُبّقت الأدلة على {n_pages} صفحة. يُعاد التعرّف على النص للصفحات التي تغيّرت مناطقها."
        )
        return redirect("books:detail", book.pk)

    guides_obj = LayoutGuides.objects.filter(book=book).first()
    stats = services.guides_stats(book)
    reference = _reference_page(book, guides_obj, request.GET.get("page"))
    values = services.book_guides_dict(book)

    pre: Preprocess | None = reference.preprocess if reference is not None else None
    detected_rule = None
    if pre is not None and pre.footnote_rule_y is not None and pre.output_height:
        detected_rule = round(pre.footnote_rule_y / pre.output_height, 4)

    pages = list(
        Preprocess.objects.filter(page__book=book, page__is_excluded=False, output_height__gt=0)
        .select_related("page")
        .order_by("page__number")
        .values_list("page__number", "footnote_rule_y")
    )
    config = {
        "header_cut": values["header_cut"],
        "footnote_line": values["footnote_line"],
        "page_number_zone": values["page_number_zone"],
        "page_number_height": values["page_number_height"],
        "proposal": stats.footnote_line,
        "detected_rule": detected_rule,
        "line_boxes": pre.line_boxes if pre is not None else [],
        "output": {"width": pre.output_width, "height": pre.output_height} if pre is not None else None,
    }
    context = {
        "book": book,
        "guides": guides_obj,
        "config": config,
        "stats": stats,
        "proposal_percent": _percent(stats.footnote_line),
        "reference": reference,
        "reference_image": pre.display_image.url if pre is not None and pre.display_image else "",
        "pages": [{"number": n, "has_rule": rule is not None} for n, rule in pages],
        "zones": LayoutGuides.PageNumberZone.choices,
    }
    return render(request, "processing/guides.html", context)
