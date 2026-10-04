"""Page clips (D107): a crop of a page image around some lines, those lines highlighted, as WebP, behind a
signed link an AI client can show without a session.

- `clip_url(page_id, line_ids, words)`: `research:clip` with a token signed by `django.core.signing`
  (salt `research.clip`, timestamped; `read_token` refuses it after `NASSAKH["CLIP_LINK_DAYS"]` days or when
  `SECRET_KEY` changes). The token carries the page, its lines and the words to underline (`[line, token]`,
  the doubtful readings a check points at). Only the services mint tokens, for pages the user may see
  (`books.access`): holding the link is the permission, for those lines of that page and until it expires.
- `clip_bytes(payload)`: the image. The page's prepared gray image (`Preprocess.gray_image`, the space the
  line boxes are in; the display image scaled when it is missing) is cut to the width of the page's text and
  to the lines with a line and a half of context above and below; the lines get the highlight colour
  (multiplied, so the ink stays black), the words an amber underline; the result is at most `MAX_WIDTH`
  pixels wide. Cached on disk under the book's media folder (`books/<id>/clips/<sha256>.webp`), the key
  covering the page image and the lines' boxes, so an edited line gets a fresh clip.
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core import signing
from django.urls import reverse

from PIL import Image, ImageChops, ImageDraw

from books.models import Page
from ocr.models import Line
from processing.models import Preprocess

SALT = "research.clip"
MAX_WIDTH = 1100
WEBP_QUALITY = 80
HIGHLIGHT = (255, 236, 150)  # DESIGN.md --highlight, a shade deeper so it shows on a gray scan
UNDERLINE = (217, 119, 6)  # --warning
CONTEXT_LINES = 1.5
MAX_LINES = 60


class ClipError(Exception):
    """A clip that cannot be made (the page, its lines or its image are gone)."""


def link_days() -> int:
    return int(settings.NASSAKH.get("CLIP_LINK_DAYS", 7))


def make_token(page_id: int, line_ids, words=()) -> str:
    """The signed token of a clip (page, lines, words to underline)."""
    payload = {
        "p": int(page_id),
        "l": sorted({int(pk) for pk in line_ids})[:MAX_LINES],
        "w": sorted({(int(line), int(i)) for line, i in words}),
    }
    return signing.dumps(payload, salt=SALT, compress=True)


def read_token(token: str) -> dict:
    """The payload of a token; raises `signing.BadSignature` (`SignatureExpired` after `link_days()`)."""
    payload = signing.loads(token, salt=SALT, max_age=timedelta(days=link_days()))
    if not isinstance(payload, dict) or not isinstance(payload.get("p"), int) or not payload.get("l"):
        raise signing.BadSignature("not a clip")
    return payload


def clip_url(page_id: int, line_ids, words=(), base_url: str = "") -> str:
    return f"{base_url.rstrip('/')}{reverse('research:clip', args=[make_token(page_id, line_ids, words)])}"


def _image(pre: Preprocess | None) -> tuple[Image.Image, float]:
    """The page image in the line boxes' space and the scale from that space to it (1 for the gray one)."""
    if pre is not None and pre.gray_image:
        with pre.gray_image.open("rb") as handle:
            image = Image.open(handle)
            image.load()
        return image.convert("L"), 1.0
    if pre is not None and pre.display_image and pre.output_width:
        with pre.display_image.open("rb") as handle:
            image = Image.open(handle)
            image.load()
        return image.convert("L"), image.width / float(pre.output_width)
    raise ClipError("no page image")


def _box(value) -> tuple[int, int, int, int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 4:
        x0, y0, x1, y1 = (int(round(float(v))) for v in value)
        if x1 > x0 and y1 > y0:
            return x0, y0, x1, y1
    return None


def _image_version(pre: Preprocess | None) -> str:
    """The page image's name and last change: a page prepared again (same file name) gets fresh clips."""
    if pre is None:
        return ""
    field = pre.gray_image if pre.gray_image else pre.display_image
    if not field:
        return ""
    try:
        changed = field.storage.get_modified_time(field.name).timestamp()
    except (OSError, NotImplementedError):
        changed = 0
    return f"{field.name}@{changed}"


def _cache_path(page: Page, image_name: str, lines: list[Line], words: list) -> Path:
    key = json.dumps(
        [page.pk, image_name, [(line.pk, line.bbox) for line in lines], words, MAX_WIDTH, HIGHLIGHT],
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:40]
    return Path(settings.MEDIA_ROOT) / "books" / str(page.book_id) / "clips" / f"{digest}.webp"


def clip_bytes(payload: dict) -> bytes:
    """The WebP clip a token's payload describes (from the cache when it was made before)."""
    page = Page.objects.filter(pk=payload["p"]).first()
    if page is None:
        raise ClipError("no page")
    lines = list(Line.objects.filter(page=page, pk__in=payload["l"]).order_by("order", "id"))
    if not lines:
        raise ClipError("no lines")
    pre = Preprocess.objects.filter(page=page).first()
    image_name = _image_version(pre)
    words = [list(pair) for pair in payload.get("w") or []]
    path = _cache_path(page, image_name, lines, words)
    if path.exists():
        return path.read_bytes()
    data = render(page, pre, lines, words)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_bytes(data)
    temp.replace(path)
    return data


def render(page: Page, pre: Preprocess | None, lines: list[Line], words: list) -> bytes:
    """Cut, highlight and encode the clip (see the module docstring)."""
    image, scale = _image(pre)
    boxes = [box for line in lines if (box := _box(line.bbox))]
    marked = set(map(tuple, words))
    word_boxes = []
    for line in lines:
        for i, token in enumerate(line.tokens or []):
            if (line.pk, i) in marked and isinstance(token, dict) and (box := _box(token.get("bbox"))):
                word_boxes.append(box)
    if not boxes:
        boxes = word_boxes
    if not boxes:
        raise ClipError("the lines have no boxes")
    # the width of the page's text: every line of the page with a box
    text_boxes = [
        box for box in (_box(b) for b in Line.objects.filter(page=page).values_list("bbox", flat=True)) if box
    ]
    heights = sorted(box[3] - box[1] for box in boxes)
    pad = int(heights[len(heights) // 2] * CONTEXT_LINES)
    width, height = (int(image.width / scale), int(image.height / scale))
    x0 = min(box[0] for box in text_boxes or boxes) - 12
    x1 = max(box[2] for box in text_boxes or boxes) + 12
    y0 = min(box[1] for box in boxes) - pad
    y1 = max(box[3] for box in boxes) + pad
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(width, x1), min(height, y1)

    def scaled(box):
        return tuple(int(round(v * scale)) for v in box)

    crop = image.crop(scaled((x0, y0, x1, y1))).convert("RGB")
    tint = Image.new("RGB", crop.size, (255, 255, 255))
    draw = ImageDraw.Draw(tint)
    for box in boxes:
        bx0, by0, bx1, by1 = scaled((box[0] - x0 - 4, box[1] - y0 - 2, box[2] - x0 + 4, box[3] - y0 + 2))
        draw.rectangle((bx0, by0, bx1, by1), fill=HIGHLIGHT)
    crop = ImageChops.multiply(crop, tint)
    if word_boxes:
        pen = ImageDraw.Draw(crop)
        stroke = max(2, int(round(3 * scale)))
        for box in word_boxes:
            bx0, _by0, bx1, by1 = scaled((box[0] - x0, box[1] - y0, box[2] - x0, box[3] - y0))
            pen.rectangle((bx0, by1 + 1, bx1, by1 + 1 + stroke), fill=UNDERLINE)
    if crop.width > MAX_WIDTH:
        crop = crop.resize((MAX_WIDTH, max(1, round(crop.height * MAX_WIDTH / crop.width))), Image.LANCZOS)
    out = io.BytesIO()
    crop.save(out, format="WEBP", quality=WEBP_QUALITY, method=4)
    return out.getvalue()
