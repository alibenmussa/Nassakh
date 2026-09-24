"""Image helpers shared by the pipeline and the OCR engines.

Arrays are numpy `uint8`: 2-D for grayscale, H×W×3 RGB for colour. Encoding goes through PIL.
"""

from __future__ import annotations

import io
import math
import mimetypes
from pathlib import Path

import numpy as np
from PIL import Image

WEBP_QUALITY = 82
Bbox = tuple[int, int, int, int]


def smart_resize(height: int, width: int, factor: int, min_pixels: int, max_pixels: int) -> tuple[int, int]:
    """Qwen2-VL resize rule: dimensions multiple of `factor`, area within bounds. Returns (height, width)."""
    h_bar = max(factor, round(height / factor) * factor)
    w_bar = max(factor, round(width / factor) * factor)
    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = max(factor, math.floor(height / beta / factor) * factor)
        w_bar = max(factor, math.floor(width / beta / factor) * factor)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = math.ceil(height * beta / factor) * factor
        w_bar = math.ceil(width * beta / factor) * factor
    return h_bar, w_bar


def prepare_image(
    path: str | Path, max_pixels: int, min_pixels: int
) -> tuple[Image.Image, tuple[int, int], tuple[int, int]]:
    """Open an image as RGB and resize it the way Qwen2-VL would.

    Returns (PIL.Image, original_size, new_size) with sizes as (width, height). Doing the
    resize ourselves keeps the PyTorch and MLX backends on identical pixels.
    """
    img = Image.open(path).convert("RGB")
    w, h = img.size
    new_h, new_w = smart_resize(h, w, 28, min_pixels, max_pixels)
    if (new_w, new_h) != (w, h):
        img = img.resize((new_w, new_h), Image.LANCZOS)
    return img, (w, h), (new_w, new_h)


def to_pil(image: np.ndarray | Image.Image) -> Image.Image:
    """Wrap a numpy array (grayscale or RGB) as a PIL image; PIL images pass through."""
    if isinstance(image, Image.Image):
        return image
    arr = np.asarray(image)
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    if arr.ndim == 2:
        return Image.fromarray(arr, mode="L")
    if arr.ndim == 3 and arr.shape[2] == 3:
        return Image.fromarray(arr, mode="RGB")
    if arr.ndim == 3 and arr.shape[2] == 1:
        return Image.fromarray(arr[:, :, 0], mode="L")
    raise ValueError(f"unsupported image shape {arr.shape}")


def fit_width(image: np.ndarray | Image.Image, max_width: int) -> Image.Image:
    """PIL image no wider than `max_width` (downscaled with Lanczos when needed)."""
    img = to_pil(image)
    if img.width > max_width:
        img = img.resize((max_width, max(1, round(img.height * max_width / img.width))), Image.LANCZOS)
    return img


def to_webp_bytes(
    array: np.ndarray | Image.Image, max_width: int | None = None, quality: int = WEBP_QUALITY
) -> bytes:
    """Encode an image as WebP, downscaling to `max_width` pixels wide when it is larger."""
    img = to_pil(array)
    if max_width and img.width > max_width:
        new_h = max(1, round(img.height * max_width / img.width))
        img = img.resize((max_width, new_h), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=quality, method=4)
    return buf.getvalue()


def to_png_bytes(array: np.ndarray | Image.Image) -> bytes:
    """Encode an image as lossless PNG."""
    buf = io.BytesIO()
    to_pil(array).save(buf, format="PNG", optimize=False, compress_level=6)
    return buf.getvalue()


def encode_image(array: np.ndarray | Image.Image, filename: str, quality: int = WEBP_QUALITY) -> bytes:
    """Encode according to the file extension: `.webp` lossy, `.png` lossless, `.jpg/.jpeg` JPEG."""
    ext = Path(filename).suffix.lower()
    if ext == ".webp":
        return to_webp_bytes(array, quality=quality)
    if ext == ".png":
        return to_png_bytes(array)
    if ext in (".jpg", ".jpeg"):
        buf = io.BytesIO()
        to_pil(array).convert("RGB").save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
    raise ValueError(f"unsupported image extension: {filename}")


def crop(array: np.ndarray, bbox: Bbox | list) -> np.ndarray:
    """Return `array[y0:y1, x0:x1]` for a `[x0, y0, x1, y1]` box, clamped to the image and rounded to ints."""
    h, w = array.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in bbox)
    x0, x1 = sorted((max(0, min(w, x0)), max(0, min(w, x1))))
    y0, y1 = sorted((max(0, min(h, y0)), max(0, min(h, y1))))
    return array[y0:y1, x0:x1]


def load_gray(source) -> np.ndarray:
    """Read an image (path or file-like) as a 2-D uint8 grayscale array."""
    with Image.open(source) as img:
        return np.asarray(img.convert("L"), dtype=np.uint8).copy()


def guess_content_type(filename: str) -> str:
    """MIME type for a file name, defaulting to a generic binary type."""
    content_type, _ = mimetypes.guess_type(filename)
    return content_type or "application/octet-stream"
