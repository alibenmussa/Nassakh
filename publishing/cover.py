"""The cover (D80, COVER_SPEC §3): a page of its own, drawn alone and put in front of the interior by each
output.

The cover never enters the book's layout: it is its own small WeasyPrint document — one page of the trim, no
margins — so page numbers, sides, chapter openings, the footprint («N صفحة»), the contents' numbers and the
live layout stay as they are. `publishing.model.CoverSpec` (`Book.front.cover`, set when the cover can be
drawn) says what it shows:

- **`info`** «من بيانات الكتاب»: the title (heading face, bold, `center_pt`), the subtitle and the author
  (body face, 0.55 × `center_pt`) in a block centred in the page; the imprint «الناشر، المدينة، السنة» (as
  the title page writes it, body face, `bottom_pt`) at the bottom.
- **`text`** «نص مخصّص»: the owner's centre text (heading face, bold; its lines kept, each centred) and
  bottom text (body face).
- **`image`** «صورة»: the picture placed by its fit (`image_box`, computed here from its pixel size, so no
  renderer's `object-fit` is relied on): `fill` covers the page (the overflow cropped, centred), `width` /
  `height` show the whole width / height, centred; the background colour shows where the image does not
  reach. No text.

The bottom block sits `bottom_mm` from the trim edge (default: the page's bottom margin + 8 mm); both blocks
keep the larger side margin on each side. The faces are the preview's `@font-face` roles
(`publishing.fonts.font_face_css`), so Arabic shapes as on the pages. For the print PDF (`bleed_mm`,
`slug_mm`), the page grows by the bleed and the crop marks' slug (`@page { bleed }`, as the interior's) and
the cover's background and image run into the bleed (never into the slug, where the finisher draws the
marks).

**Outputs** (each calls this module): `render_cover` (the PDF) and `cover_raster` (PNG, JPEG or WebP of
page 1); `export_raster` is the rule the Word file and the EPUB share (JPEG q90 when the cover shows a
picture, PNG otherwise); `prepend_cover` puts the cover in front of an interior PDF (PyMuPDF `insert_pdf`:
the outline, the links and the named destinations point at page objects, so they move with their pages)
and writes `/PageLabels` — the cover «غلاف» (a PDF text string, UTF-16BE with its BOM), the interior from
1 — so a viewer's page numbers are the printed ones.

**The book page's cover** (`cover_payload`, `api:cover`, and the answer of every stylesheet save): rendered in
the request when missing (one page, well under a second; the page's own first paint only reads the cache,
`render=False`), cached by `cover_hash` (the cover's settings and image sha, the trim and margins,
the texts it prints, the faces' files and the renderer's version) under `media/books/<id>/cover/<hash>/`
(`cover.pdf`, `cover.webp` 1100 px tall like the page sheets, `cover-2x.webp`), the newest `KEEP` kept.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from dataclasses import dataclass
from html import escape
from pathlib import Path

from . import fonts as F
from .model import COVER_BOTTOM_GAP_MM, COVER_SUB_SCALE, Book, CoverSpec, PageSetup, book_model

log = logging.getLogger(__name__)

COVER_VERSION = "nk-cover-1"  # part of the cover's hash: bump it when the markup or the CSS change
PAGE_LABEL = "غلاف"  # the cover's label in a PDF viewer (`/PageLabels`)
PREVIEW_HEIGHT_PX = 1100  # the book page's sheet, as `publishing.preview.PAGE_HEIGHT_PX`
WEBP_QUALITY = 85
JPEG_QUALITY = 90
WORD_DPI = 300
EPUB_HEIGHT_PX = 1600
KEEP = 3  # cover renders kept per book
GOOD_DPI = 300  # what print asks of the cover's picture
LOW_DPI = 285  # below this (5 % short of 300) readiness says so (`cover_resolution`)
MM_PT = 72 / 25.4
LINE_HEIGHT_CENTER = 1.35
LINE_HEIGHT_TEXT = 1.5
# PyMuPDF's save of a combined file: as `pdf_text.SAVE_OPTIONS` (the file's /ID kept, the same bytes for the
# same inputs, object streams and deflate, the objects left unreferenced dropped)
SAVE_OPTIONS = {"garbage": 1, "deflate": 1, "use_objstms": 1, "no_new_id": 1}


# ====================================================================== geometry


@dataclass(frozen=True)
class Box:
    """A box in millimetres from the top-left corner of the area it is placed in."""

    left: float
    top: float
    width: float
    height: float


def bottom_distance(cover: CoverSpec, setup: PageSetup) -> float:
    """The bottom block's distance from the trim edge: `bottom_mm`, else the bottom margin + 8 mm."""
    return float(cover.bottom_mm) if cover.bottom_mm is not None else setup.bottom_mm + COVER_BOTTOM_GAP_MM


def side_padding(setup: PageSetup) -> float:
    """The text blocks' distance from each side edge: the larger of the two side margins (the blocks stay
    centred on the page whichever side the binding is)."""
    return max(setup.inner_mm, setup.outer_mm)


def image_box(cover: CoverSpec, width_mm: float, height_mm: float) -> Box | None:
    """Where the cover's image goes in an area of `width_mm` × `height_mm` (the trim, or the trim with its
    bleed): `fill` scales it to cover the area and centres it (the overflow is cut), `width` to the area's
    width, centred vertically, `height` to its height, centred horizontally. None without an image."""
    image = cover.image
    if image is None or image.width <= 0 or image.height <= 0:
        return None
    if cover.fit == "width":
        scale = width_mm / image.width
    elif cover.fit == "height":
        scale = height_mm / image.height
    else:
        scale = max(width_mm / image.width, height_mm / image.height)
    width, height = image.width * scale, image.height * scale
    return Box((width_mm - width) / 2, (height_mm - height) / 2, width, height)


def effective_dpi(cover: CoverSpec, width_mm: float, height_mm: float) -> float | None:
    """The image's pixels per inch as it is fitted on the trim (None without an image)."""
    box = image_box(cover, width_mm, height_mm)
    if box is None or cover.image is None:
        return None
    return cover.image.width / (box.width / 25.4)


# ====================================================================== the markup


def _mm(value: float) -> str:
    return f"{round(float(value), 3):g}mm"


def _pt(value: float) -> str:
    return f"{round(float(value), 2):g}pt"


def _text(value: str) -> str:
    return escape(value, quote=False)


def _lines_html(text: str) -> str:
    """A text's lines, escaped, one `<br/>` between two (an empty line keeps its height)."""
    return "<br/>".join(_text(line) if line else "&#160;" for line in text.split("\n"))


def imprint_line(book: Book) -> str:
    """«الناشر، المدينة، السنة» as the title page writes it."""
    front = book.front
    return "، ".join(value for value in (front.publisher, front.city, front.year) if value)


def cover_blocks(book: Book) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The centre block's and the bottom block's paragraphs, `(class, inner HTML)` each, for the book's
    cover (both empty for an image cover)."""
    cover = book.front.cover
    if cover is None or cover.mode == "image":
        return [], []
    if cover.mode == "info":
        front = book.front
        center = [("nk-cover-title", _text(front.title))]
        if front.subtitle:
            center.append(("nk-cover-sub", _text(front.subtitle)))
        if front.author:
            center.append(("nk-cover-author", _text(front.author)))
        place = imprint_line(book)
        return center, ([("nk-cover-foot", _text(place))] if place else [])
    center = [("nk-cover-title", _lines_html(cover.center))] if cover.center else []
    bottom = [("nk-cover-foot", _lines_html(cover.bottom))] if cover.bottom else []
    return center, bottom


def cover_css(book: Book, fonts: F.ResolvedFonts, *, bleed_mm: float = 0.0, slug_mm: float = 0.0) -> str:
    """The cover page's CSS (see the module docstring); `bleed_mm` and `slug_mm` for the print PDF."""
    setup, cover = book.setup, book.front.cover
    if cover is None:
        raise ValueError("the book has no cover to draw")
    bleed = max(float(bleed_mm), 0.0)
    page_bleed = bleed + max(float(slug_mm), 0.0)
    width, height = setup.width_mm, setup.height_mm
    side = side_padding(setup)
    rules = [
        F.font_face_css(fonts),
        f"@page {{ size: {_mm(width)} {_mm(height)}; margin: 0;"
        + (f" bleed: {_mm(page_bleed)};" if page_bleed > 0 else "")
        + " }",
        "html, body { margin: 0; padding: 0; }",
        # the trim with its bleed; the text blocks are placed from the trim
        f".nk-cover {{ position: absolute; left: -{_mm(bleed)}; top: -{_mm(bleed)};"
        f" width: {_mm(width + 2 * bleed)}; height: {_mm(height + 2 * bleed)}; overflow: hidden;"
        f" background: {cover.background}; color: {cover.color}; }}",
        f".nk-cover-center, .nk-cover-bottom {{ position: absolute; left: {_mm(bleed + side)};"
        f" width: {_mm(width - 2 * side)}; text-align: center; text-align-last: center; }}",
        # centred in the page: its middle on the trim's middle
        f".nk-cover-center {{ top: {_mm(bleed + height / 2)}; transform: translateY(-50%); }}",
        f".nk-cover-bottom {{ bottom: {_mm(bleed + bottom_distance(cover, setup))}; }}",
        ".nk-cover p { margin: 0; text-indent: 0; }",
        f".nk-cover-title {{ font-family: {F.families('heading')}; font-weight: bold;"
        f" font-size: {_pt(cover.center_pt)}; line-height: {LINE_HEIGHT_CENTER}; }}",
        f".nk-cover-sub, .nk-cover-author {{ font-family: {F.families()};"
        f" font-size: {_pt(cover.center_pt * COVER_SUB_SCALE)}; line-height: {LINE_HEIGHT_TEXT}; }}",
        ".nk-cover p.nk-cover-sub { margin-top: 0.6em; } .nk-cover p.nk-cover-author { margin-top: 1.6em; }",
        f".nk-cover-foot {{ font-family: {F.families()}; font-size: {_pt(cover.bottom_pt)};"
        f" line-height: {LINE_HEIGHT_TEXT}; }}",
    ]
    box = image_box(cover, width + 2 * bleed, height + 2 * bleed)
    if box is not None:
        rules.append(
            f".nk-cover-img {{ position: absolute; display: block; left: {_mm(box.left)};"
            f" top: {_mm(box.top)}; width: {_mm(box.width)}; height: {_mm(box.height)}; }}"
        )
    return "\n".join(rules)


def cover_html(
    book: Book, setup: PageSetup | None = None, *, bleed_mm: float = 0.0, slug_mm: float = 0.0
) -> str:
    """The cover page's markup (the CSS is `cover_css`): `div.nk-cover` with the image (`img.nk-cover-img`)
    or the two text blocks (`div.nk-cover-center`, `div.nk-cover-bottom`). `setup` defaults to the book's
    (`bleed_mm` and `slug_mm` do not change the markup, only the CSS)."""
    cover = book.front.cover
    if cover is None:
        raise ValueError("the book has no cover to draw")
    parts = ['<div class="nk-cover">']
    if cover.mode == "image" and cover.image is not None:
        source = escape(Path(cover.image.path).as_uri(), quote=True)
        parts.append(f'<img class="nk-cover-img" src="{source}" alt=""/>')
    center, bottom = cover_blocks(book)
    for name, lines in (("nk-cover-center", center), ("nk-cover-bottom", bottom)):
        if lines:
            inner = "".join(f'<p class="{cls}">{html}</p>' for cls, html in lines)
            parts.append(f'<div class="{name}">{inner}</div>')
    parts.append("</div>")
    title = _text(book.front.title)
    return (
        '<!DOCTYPE html>\n<html lang="ar" dir="rtl"><head><meta charset="utf-8"/>'
        f"<title>{title}</title></head><body>{''.join(parts)}</body></html>"
    )


# ====================================================================== rendering


def render_cover(
    book: Book,
    fonts: F.ResolvedFonts | None = None,
    *,
    bleed_mm: float = 0.0,
    slug_mm: float = 0.0,
    finisher=None,
) -> bytes:
    """The cover as a one-page PDF (WeasyPrint; `fonts` resolved from the setup when not given). `finisher`
    goes to `write_pdf` (the export's: boxes, crop marks, `publishing.pdf.finisher_for`)."""
    from weasyprint import CSS, HTML
    from weasyprint.text.fonts import FontConfiguration

    setup = book.setup
    if fonts is None:
        fonts = F.resolve(setup.body_font, setup.latin_font, setup.heading_font)
    font_config = FontConfiguration()
    css = CSS(string=cover_css(book, fonts, bleed_mm=bleed_mm, slug_mm=slug_mm), font_config=font_config)
    document = HTML(string=cover_html(book)).render(stylesheets=[css], font_config=font_config)
    return document.write_pdf(finisher=finisher)


def cover_raster(
    pdf: bytes, *, dpi: float | None = None, height: int | None = None, kind: str = "png"
) -> tuple[bytes, str]:
    """Page 1 of `pdf` as an RGB image — at `dpi`, or `height` pixels tall — in `kind` `png`, `jpeg`
    (q90) or `webp` (q85): `(bytes, media type)`. Only the trim is drawn (a print cover's bleed and slug
    are left out)."""
    import pymupdf

    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        page = document[0]
        clip = page.trimbox if not page.trimbox.is_empty else page.rect
        if height:  # the width rounded, as the page sheets: 17×24 at 1100 px tall is 779 px wide
            size = (round(clip.width * height / clip.height), int(height))
        else:
            size = (round(clip.width * (dpi or 72) / 72), round(clip.height * (dpi or 72) / 72))
        matrix = pymupdf.Matrix(size[0] / clip.width, size[1] / clip.height)
        pix = page.get_pixmap(matrix=matrix, colorspace=pymupdf.csRGB, alpha=False, clip=clip)
        if kind == "png":
            return pix.tobytes("png"), "image/png"
        from PIL import Image

        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    out = _encode(image, kind)
    return out, "image/jpeg" if kind == "jpeg" else "image/webp"


def _encode(image, kind: str) -> bytes:
    import io

    out = io.BytesIO()
    if kind == "jpeg":
        image.save(out, "JPEG", quality=JPEG_QUALITY, subsampling=0, optimize=True)
    else:
        image.save(out, "WEBP", quality=WEBP_QUALITY, method=4)
    return out.getvalue()


def export_raster(
    book: Book, fonts: F.ResolvedFonts | None = None, *, dpi: float | None = None, height: int | None = None
) -> tuple[bytes, str]:
    """The cover rasterised for the Word file (`dpi` 300) or the EPUB (`height` 1600 px), from the same
    render as the PDFs: JPEG q90 when it shows a picture, PNG otherwise."""
    cover = book.front.cover
    if cover is None:
        raise ValueError("the book has no cover to draw")
    return cover_raster(
        render_cover(book, fonts), dpi=dpi, height=height, kind="jpeg" if cover.photo else "png"
    )


def pdf_text_string(value: str) -> str:
    """A PDF text string in hex: UTF-16BE with its byte order mark (`<FEFF…>`), how a PDF writes text that
    is not PDFDocEncoding (the page label «غلاف»)."""
    return "<" + ("﻿" + value).encode("utf-16-be").hex().upper() + ">"


def page_labels(first: str = PAGE_LABEL) -> str:
    """`/PageLabels` for a file whose page 1 is the cover: the cover labelled `first`, the pages after it
    numbered from 1."""
    return f"<</Nums[0<</P{pdf_text_string(first)}>>1<</S/D/St 1>>]>>"


def text_string(raw: str) -> str:
    """A PDF text string as PyMuPDF hands it back from an object (`<FEFF…>` hex or `(…)` literal) decoded as
    PDF 32000 §7.9.2.2 says: UTF-16BE after its byte order mark, UTF-8 after its BOM (PDF 2.0), else
    PDFDocEncoding (Latin-1 for the characters a label uses)."""
    raw = raw.strip()
    if raw.startswith("<") and raw.endswith(">"):
        digits = "".join(raw[1:-1].split())
        data = bytes.fromhex(digits + ("0" if len(digits) % 2 else ""))
    else:
        body = raw[1:-1] if raw.startswith("(") and raw.endswith(")") else raw
        data = _literal_bytes(body)
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    return data.decode("latin-1")


OCTAL = "01234567"
_ESCAPES = {"n": b"\n", "r": b"\r", "t": b"\t", "b": b"\b", "f": b"\f", "(": b"(", ")": b")", "\\": b"\\"}


def _literal_bytes(body: str) -> bytes:
    """The bytes of a literal string's body (its escapes and octal codes resolved)."""
    out = bytearray()
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out += char.encode("latin-1", errors="replace")
            index += 1
            continue
        following = body[index + 1 : index + 2]
        if following in _ESCAPES:
            out += _ESCAPES[following]
            index += 2
        elif following and following in OCTAL:
            digits = following
            while (
                len(digits) < 3 and (body[index + 1 + len(digits) : index + 2 + len(digits)] or "x") in OCTAL
            ):
                digits += body[index + 1 + len(digits)]
            out.append(int(digits, 8) & 0xFF)
            index += 1 + len(digits)
        else:  # a line continuation, or a backslash before any other character
            index += 2 if following in ("\n", "\r") else 1
    return bytes(out)


def _roman(number: int) -> str:
    out = ""
    for value, letters in (
        (1000, "m"),
        (900, "cm"),
        (500, "d"),
        (400, "cd"),
        (100, "c"),
        (90, "xc"),
        (50, "l"),
        (40, "xl"),
        (10, "x"),
        (9, "ix"),
        (5, "v"),
        (4, "iv"),
        (1, "i"),
    ):
        while number >= value:
            out += letters
            number -= value
    return out


def _numeral(style: str, number: int) -> str:
    if style == "D":
        return str(number)
    if style in ("r", "R"):
        return _roman(number) if style == "r" else _roman(number).upper()
    if style in ("a", "A"):
        letter = chr(ord("a") + (number - 1) % 26) * ((number - 1) // 26 + 1)
        return letter if style == "a" else letter.upper()
    return ""


def page_labels_of(data: bytes, pages: int | None = None) -> list[str]:
    """The labels a viewer shows for the first `pages` pages of a PDF (all when None): the `/PageLabels`
    ranges (PyMuPDF's `get_page_labels`) with each prefix decoded as a PDF text string (`text_string`;
    PyMuPDF's own `Page.get_label` hands a UTF-16 prefix back as its raw hex). Pages before the first
    range, or a file without labels, get their page numbers."""
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as document:
        count = document.page_count if pages is None else min(pages, document.page_count)
        rules = sorted(document.get_page_labels(), key=lambda rule: rule.get("startpage", 0))
    out = []
    for index in range(count):
        rule = next((item for item in reversed(rules) if item.get("startpage", 0) <= index), None)
        if rule is None:
            out.append(str(index + 1))
            continue
        number = int(rule.get("firstpagenum") or 1) + index - int(rule.get("startpage") or 0)
        prefix = text_string(rule.get("prefix") or "()") if rule.get("prefix") else ""
        out.append(prefix + _numeral(str(rule.get("style") or ""), number))
    return out


def prepend_cover(interior: bytes, cover: bytes) -> bytes:
    """`cover`'s page in front of `interior`'s pages, with the page labels (see the module docstring). The
    interior's catalog (outline, viewer preferences, language, page mode) and document information stay."""
    import pymupdf

    with (
        pymupdf.open(stream=interior, filetype="pdf") as document,
        pymupdf.open(stream=cover, filetype="pdf") as front,
    ):
        document.insert_pdf(front, start_at=0, links=False, annots=False)
        document.xref_set_key(document.pdf_catalog(), "PageLabels", page_labels())
        return document.tobytes(**SAVE_OPTIONS)


# ====================================================================== the book page's cover (api:cover)


def cover_book(document, setup: PageSetup, title: str = "", author: str = "") -> Book:
    """The book model the cover needs — the front matter only: the document's leading title node (never its
    chapters) with the setup and the book's own title and author."""
    from editor import document as doc

    content = doc.content_of(document)
    return book_model(
        {"type": "doc", "content": content[: doc.preamble_end(content)]}, setup, title=title, author=author
    )


def cover_hash(book: Book, fonts: F.ResolvedFonts) -> str:
    """24 hex digits identifying a cover render: its settings and image sha, the trim and the margins it
    reads, the texts it prints, the faces (keys and files) and the renderer."""
    import weasyprint

    cover, setup, front = book.front.cover, book.setup, book.front
    if cover is None:
        raise ValueError("the book has no cover to draw")
    texts = None
    if cover.mode == "info":
        texts = [front.title, front.subtitle, front.author, front.publisher, front.city, front.year]
    payload = {
        "version": COVER_VERSION,
        "engine": weasyprint.__version__,
        "cover": cover.fingerprint(),
        "page": [setup.width_mm, setup.height_mm, setup.bottom_mm, setup.inner_mm, setup.outer_mm],
        "texts": texts,
        "faces": [setup.body_font, setup.latin_font, setup.heading_font],
        "fonts": fonts.fingerprint(),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def cover_folder(book_id: int, digest: str) -> str:
    """The storage folder of a cover render: `books/<id>/cover/<hash>`."""
    return f"books/{book_id}/cover/{digest}"


COVER_FILES = ("cover.webp", "cover-2x.webp", "cover.pdf")  # the PDF is written last: its presence means done


def _write(path: Path, data: bytes) -> None:
    """`data` at `path`, replaced whole (a temporary file of its own, then renamed: two requests rendering
    the same cover at once never mix their bytes)."""
    import tempfile

    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        handle.write(data)
    os.replace(handle.name, path)


def write_cover_files(book: Book, fonts: F.ResolvedFonts, folder: Path) -> None:
    """Render the cover and write `cover.webp` (1×, `PREVIEW_HEIGHT_PX` tall), `cover-2x.webp` and
    `cover.pdf` into `folder` (each file replaced whole, the PDF last)."""
    pdf = render_cover(book, fonts)
    folder.mkdir(parents=True, exist_ok=True)
    for name, height in (("cover.webp", PREVIEW_HEIGHT_PX), ("cover-2x.webp", PREVIEW_HEIGHT_PX * 2)):
        data, _media = cover_raster(pdf, height=height, kind="webp")
        _write(folder / name, data)
    _write(folder / "cover.pdf", pdf)


def prune_covers(root: Path, keep: str) -> None:
    """Keep the newest `KEEP` cover renders of a book (and always `keep`, the one just shown)."""
    try:
        folders = [path for path in root.iterdir() if path.is_dir()]
    except OSError:
        return
    folders.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    others = [path for path in folders if path.name != keep]
    for path in others[KEEP - 1 :]:
        shutil.rmtree(path, ignore_errors=True)


EMPTY_PAYLOAD = {"hash": None, "image_1x": None, "image_2x": None, "width": None, "height": None}


def cover_payload(book, *, render: bool = True) -> dict:
    """`api:cover`: `{mode, hash, image_1x, image_2x, width, height}` of the book's cover (a `books.Book`),
    rendered now when this hash has no files yet; the images and sizes are null for mode `none` and for an
    image cover without its image. `width` / `height` are the 1× image's pixels. `render=False` never
    renders (the book page's first paint: a cached render, else `pending: true` and no images)."""
    from django.conf import settings
    from django.core.files.storage import default_storage

    from PIL import Image

    from editor.models import Manuscript

    from .model import page_setup
    from .preview import stylesheet_for

    setup = page_setup(stylesheet_for(book))
    if setup.cover is None or not setup.cover.ready:
        return {"mode": setup.cover.mode if setup.cover is not None else "none", **EMPTY_PAYLOAD}
    head = Manuscript.objects.filter(book_id=book.pk).values_list("document__content__0", flat=True).first()
    model = cover_book({"content": [head] if isinstance(head, dict) else []}, setup, book.title, book.author)
    fonts = F.resolve(setup.body_font, setup.latin_font, setup.heading_font)
    digest = cover_hash(model, fonts)
    name = cover_folder(book.pk, digest)
    folder = Path(settings.MEDIA_ROOT) / name
    if not all((folder / file).is_file() for file in COVER_FILES):
        if not render:
            return {"mode": setup.cover.mode, **EMPTY_PAYLOAD, "hash": digest, "pending": True}
        write_cover_files(model, fonts, folder)
        prune_covers(folder.parent, digest)
    else:
        os.utime(folder)  # the newest shown stays among the kept
    with Image.open(folder / "cover.webp") as image:
        width, height = image.size
    return {
        "mode": setup.cover.mode,
        "hash": digest,
        "image_1x": default_storage.url(f"{name}/cover.webp"),
        "image_2x": default_storage.url(f"{name}/cover-2x.webp"),
        "width": width,
        "height": height,
    }


__all__ = [
    "PAGE_LABEL",
    "Box",
    "cover_blocks",
    "cover_book",
    "cover_css",
    "cover_hash",
    "cover_html",
    "cover_payload",
    "cover_raster",
    "effective_dpi",
    "export_raster",
    "image_box",
    "page_labels",
    "page_labels_of",
    "prepend_cover",
    "render_cover",
]
