"""Step 1: extract the selected PDF pages as grayscale PNGs.

- Scanned pages are rendered at their *native* DPI (the resolution of the
  embedded scan), so no pixels are invented and PDF rotation is honoured.
- Born-digital pages are rendered at `render_dpi` (default 300).
- Sheets holding two book pages are split at the detected gutter into a right
  page (first, RTL) and a left page. An overlay shows where the cut was made.

Writes pages/<page_id>.png, overlays/<page_id>_split.png and pages/manifest.json.
Re-running skips pages that already exist unless --force is given.
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import cv2
import numpy as np
import pymupdf

import config
from common import write_json_atomic


def native_dpi(page: pymupdf.Page, img_w: int, img_h: int) -> float:
    """DPI at which rendering reproduces the embedded scan pixel for pixel."""
    rect = page.rect  # already rotated as displayed
    long_in, short_in = sorted([rect.width, rect.height], reverse=True)
    long_px, short_px = sorted([img_w, img_h], reverse=True)
    return max(long_px / (long_in / 72.0), short_px / (short_in / 72.0))


def largest_embedded_image(doc: pymupdf.Document, page: pymupdf.Page):
    best = None
    for info in page.get_images(full=True):
        xref = info[0]
        w, h = info[2], info[3]
        if best is None or w * h > best[1] * best[2]:
            best = (xref, w, h)
    return best


def render_gray(page: pymupdf.Page, dpi: float) -> np.ndarray:
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()


def find_gutter(gray: np.ndarray) -> tuple[int, float]:
    """Return (x, confidence) of the gutter between two book pages on a sheet.

    Looks for the widest low-ink vertical band in the central 30% of the sheet,
    bridging narrow dark spikes (fold shadow, staples).
    """
    h, w = gray.shape
    x0, x1 = int(0.35 * w), int(0.65 * w)
    band = gray[int(0.08 * h): int(0.92 * h), x0:x1]
    ink = (band < 128).mean(axis=0)
    k = max(5, w // 200)
    ink_s = np.convolve(ink, np.ones(k) / k, mode="same")
    low = ink_s < 0.02
    # bridge gaps shorter than 1.5% of the width (staples / fold line)
    gap_max = max(3, int(0.015 * w))
    idx = np.where(~low)[0]
    if idx.size:
        runs = np.split(idx, np.where(np.diff(idx) > 1)[0] + 1)
        for r in runs:
            if r.size <= gap_max and r[0] > 0 and r[-1] < low.size - 1:
                low[r[0]: r[-1] + 1] = True
    best_len, best_center = 0, None
    lows = np.where(low)[0]
    if lows.size:
        for r in np.split(lows, np.where(np.diff(lows) > 1)[0] + 1):
            if r.size > best_len:
                best_len, best_center = r.size, int((r[0] + r[-1]) / 2)
    if best_center is None or best_len < 0.02 * w:
        return w // 2, 0.0
    return x0 + best_center, min(1.0, best_len / (0.15 * w))


def split_overlay(gray: np.ndarray, x: int, out: Path) -> None:
    scale = 1000 / gray.shape[1]
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(small, cv2.COLOR_GRAY2BGR)
    cv2.line(rgb, (int(x * scale), 0), (int(x * scale), rgb.shape[0]), (0, 0, 255), 2)
    cv2.imwrite(str(out), rgb)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samples", default=",".join(config.SAMPLES), help="comma-separated sample keys")
    ap.add_argument("--force", action="store_true", help="re-extract even if the PNG exists")
    args = ap.parse_args()

    config.PAGES.mkdir(exist_ok=True)
    config.OVERLAYS.mkdir(exist_ok=True)
    manifest: list[dict] = []

    for key in args.samples.split(","):
        spec = config.SAMPLES[key]
        pdf_path = config.INPUT / spec["file"]
        if not pdf_path.exists():
            raise SystemExit(f"missing {pdf_path}")
        doc = pymupdf.open(pdf_path)
        for pdf_page in spec["pages"]:
            page = doc[pdf_page - 1]
            emb = None if spec["born_digital"] else largest_embedded_image(doc, page)
            if emb is None:
                dpi = float(spec.get("render_dpi", 300))
                source = "render"
            else:
                dpi = native_dpi(page, emb[1], emb[2])
                source = "native"
            base_id = f"{key}_p{pdf_page:03d}"
            targets = [(base_id, "full")] if spec["pages_per_sheet"] == 1 else [(base_id + "R", "right"), (base_id + "L", "left")]
            existing = [config.PAGES / f"{pid}.png" for pid, _ in targets]
            if all(p.exists() for p in existing) and not args.force:
                gray = None
            else:
                gray = render_gray(page, dpi)

            split_x, split_conf = None, None
            if spec["pages_per_sheet"] == 2:
                if gray is None:
                    prev = next((m for m in _previous_manifest() if m["id"] == targets[0][0]), None)
                    split_x = prev["split_x"] if prev else None
                    split_conf = prev["split_conf"] if prev else None
                else:
                    split_x, split_conf = find_gutter(gray)
                    split_overlay(gray, split_x, config.OVERLAYS / f"{base_id}_split.png")

            for pid, half in targets:
                out = config.PAGES / f"{pid}.png"
                if gray is not None:
                    if half == "full":
                        img = gray
                    elif half == "right":
                        img = gray[:, split_x:]
                    else:
                        img = gray[:, :split_x]
                    cv2.imwrite(str(out), img)
                    h, w = img.shape
                else:
                    h, w = cv2.imread(str(out), cv2.IMREAD_GRAYSCALE).shape
                manifest.append({
                    "id": pid,
                    "sample": key,
                    "pdf_file": spec["file"],
                    "pdf_page": pdf_page,
                    "half": half,
                    "path": str(out.relative_to(config.POC)),
                    "width": int(w),
                    "height": int(h),
                    "dpi": round(dpi, 1),
                    "source": source,
                    "born_digital": spec["born_digital"],
                    "low_res": spec["low_res"],
                    "split_x": split_x,
                    "split_conf": None if split_conf is None else round(float(split_conf), 2),
                })
                print(f"{pid:12s} {w}x{h}px  dpi={dpi:6.1f} {source:6s} half={half}"
                      + (f" split_x={split_x} conf={split_conf:.2f}" if split_x is not None else ""))
        doc.close()

    write_json_atomic(config.MANIFEST, {"created_at": dt.datetime.now().isoformat(timespec="seconds"), "pages": manifest})
    print(f"\n{len(manifest)} pages -> {config.MANIFEST}")


def _previous_manifest() -> list[dict]:
    if config.MANIFEST.exists():
        import json
        return json.loads(config.MANIFEST.read_text())["pages"]
    return []


if __name__ == "__main__":
    main()
