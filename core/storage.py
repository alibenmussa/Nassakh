"""Media paths and a helper to write images into FileFields.

Layout under MEDIA_ROOT:
    books/{book_id}/source.pdf
    books/{book_id}/pages/{number:04d}/original.png
    books/{book_id}/pages/{number:04d}/{gray.png | bw.png | display.webp | thumb.webp}

`Book.source_pdf` and `Page.original_image` are written once at ingest and never overwritten.
Derived images (Preprocess fields) are replaced in place when a stage is re-run.
"""

from __future__ import annotations

from django.core.files.base import ContentFile
from django.db.models.fields.files import FieldFile

from core.images import encode_image

DERIVED_FILENAMES = ("gray.png", "bw.png", "display.webp", "thumb.webp")


def _require_pk(obj, what: str) -> int:
    pk = getattr(obj, "pk", None)
    if pk is None:
        raise ValueError(f"{what} must be saved (have a primary key) before a file is attached to it")
    return pk


def book_source_path(book, filename: str) -> str:
    """`upload_to` for `Book.source_pdf`: books/{book.id}/source.pdf (the upload name is ignored)."""
    return f"books/{_require_pk(book, 'Book')}/source.pdf"


def page_original_path(page, filename: str) -> str:
    """`upload_to` for `Page.original_image`: books/{book_id}/pages/{number:04d}/original.png."""
    return f"books/{page.book_id}/pages/{page.number:04d}/original.png"


def page_scan_thumb_path(page, filename: str) -> str:
    """`upload_to` for `Page.scan_thumbnail`: books/{book_id}/pages/{number:04d}/scan_thumb.webp."""
    return f"books/{page.book_id}/pages/{page.number:04d}/scan_thumb.webp"


def page_derived_path(preprocess, filename: str) -> str:
    """`upload_to` for Preprocess images: books/{book_id}/pages/{number:04d}/{filename}.

    `filename` is one of gray.png, bw.png, display.webp, thumb.webp.
    """
    page = preprocess.page
    return f"books/{page.book_id}/pages/{page.number:04d}/{filename}"


def save_array(field: FieldFile, array_or_pil, filename: str, quality: int = 82) -> str:
    """Encode a numpy/PIL image and store it in `field` under the path its `upload_to` gives.

    An existing file at that path is replaced so the path stays stable across re-runs; the
    model instance is *not* saved (call `instance.save()` afterwards). Returns the stored name.
    """
    data = encode_image(array_or_pil, filename, quality=quality)
    instance = field.instance
    target = field.field.generate_filename(instance, filename)
    storage = field.storage
    if field.name and field.name != target and storage.exists(field.name):
        storage.delete(field.name)
    if storage.exists(target):
        storage.delete(target)
    field.save(filename, ContentFile(data), save=False)
    return field.name
