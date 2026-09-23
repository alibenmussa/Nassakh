"""Step 2: preprocess extracted pages.

For every page in pages/manifest.json:
  1. remove dark scan borders
  2. flatten the paper background (yellowing, uneven lighting)
  3. mild non-local-means denoise
  4. deskew (projection-profile variance on a cleaned ink mask, coarse then fine)
  5. crop to the text block with a constant margin
  6. write gray/<id>.png (OCR input), bw/<id>.png (Sauvola, display),
     gray_2x/<id>.png (low-res pages only), overlays/<id>.png (line boxes),
     regions/<id>_body.png + <id>_foot.png (2x) when a footnote rule is found
  7. detect text lines and a footnote separator rule
  8. save all parameters to pages/<id>.prep.json

Parameters, not just images, are saved so the same pipeline can later be
re-run with manual overrides (this mirrors the future `Preprocess` model).
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import cv2
import numpy as np
from skimage.filters import threshold_sauvola

import config
from common import load_manifest, write_json_atomic


# ---------------------------------------------------------------- helpers

def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous True runs of a 1-D boolean mask as (start, end_exclusive)."""
    idx = np.where(mask)[0]
    if idx.size == 0:
        return []
    out = []
    for r in np.split(idx, np.where(np.diff(idx) > 1)[0] + 1):
        out.append((int(r[0]), int(r[-1]) + 1))
    return out


def remove_dark_borders(gray: np.ndarray, max_frac: float = 0.15) -> tuple[np.ndarray, list[int]]:
    """Trim rows/columns at the edges that are mostly black (scanner bed, tape)."""
    h, w = gray.shape
    dark_rows = (gray < 70).mean(axis=1) > 0.5
    dark_cols = (gray < 70).mean(axis=0) > 0.5

    def trim(flags: np.ndarray, limit: int) -> tuple[int, int]:
        a = 0
        while a < limit and flags[a]:
            a += 1
        b = flags.size
        while b > flags.size - limit and flags[b - 1]:
            b -= 1
        return a, b

    y0, y1 = trim(dark_rows, int(max_frac * h))
    x0, x1 = trim(dark_cols, int(max_frac * w))
    # a dark strip that is not exactly at the edge (sample 4): look for the last
    # mostly-dark column within the limit and cut there too
    for i in range(x0, int(max_frac * w)):
        if dark_cols[i]:
            x0 = i + 1
    for i in range(x1 - 1, w - int(max_frac * w), -1):
        if dark_cols[i]:
            x1 = i
    return gray[y0:y1, x0:x1], [int(x0), int(y0), int(x1), int(y1)]


def estimate_skew(ink: np.ndarray, max_angle: float) -> tuple[float, float]:
    """Return (angle_deg, confidence) from a cleaned ink mask (255 = ink).

    Rotating by +angle (CCW) straightens the lines. The score is the variance
    of the horizontal projection profile; confidence compares the best score
    with the median over all tried angles (flat curve = unreliable).
    """
    h, w = ink.shape
    scale = min(1.0, 1200 / max(h, w))
    small = cv2.resize(ink, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    mask = (small > 64).astype(np.float32)
    sh, sw = mask.shape
    center = (sw / 2, sh / 2)

    def score(angle: float) -> float:
        m = cv2.getRotationMatrix2D(center, angle, 1.0)
        rot = cv2.warpAffine(mask, m, (sw, sh), flags=cv2.INTER_NEAREST, borderValue=0)
        return float(rot.sum(axis=1).var())

    coarse = np.arange(-max_angle, max_angle + 1e-6, 0.5)
    cs = [score(a) for a in coarse]
    best = float(coarse[int(np.argmax(cs))])
    fine = np.arange(best - 0.6, best + 0.6 + 1e-6, 0.1)
    fs = [score(a) for a in fine]
    best = float(fine[int(np.argmax(fs))])
    med = float(np.median(np.array(cs)))
    conf = (max(fs) - med) / (med + 1e-6)
    return round(best, 2), round(conf, 3)


def rotate(gray: np.ndarray, angle: float) -> np.ndarray:
    if abs(angle) < 0.05:
        return gray
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=255)


def flatten_background(gray: np.ndarray) -> tuple[np.ndarray, int]:
    """Divide by an estimated paper background so paper becomes ~white."""
    k = (max(15, gray.shape[1] // 30)) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    bg = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel)
    bg = cv2.GaussianBlur(bg, (0, 0), k / 3)
    norm = cv2.divide(gray.astype(np.float32), np.maximum(bg.astype(np.float32), 1.0), scale=255.0)
    norm = np.clip(norm, 0, 255)
    lo = float(np.percentile(norm, 1.0))
    norm = np.clip((norm - lo) * (255.0 / max(255.0 - lo, 1.0)), 0, 255)
    return norm.astype(np.uint8), k


def binarize_clean(gray: np.ndarray, min_area: int, max_area_frac: float = 0.05) -> np.ndarray:
    """Otsu ink mask with tiny specks and huge blobs (borders, staples) removed."""
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    areas = stats[:, cv2.CC_STAT_AREA]
    max_area = max_area_frac * ink.size
    keep = np.zeros(n, dtype=bool)
    keep[1:] = (areas[1:] >= min_area) & (areas[1:] <= max_area)
    return np.where(keep[labels], 255, 0).astype(np.uint8)


def content_bbox(ink: np.ndarray, margin_frac: float) -> list[int]:
    h, w = ink.shape
    rows = (ink > 0).sum(axis=1)
    cols = (ink > 0).sum(axis=0)
    ys = np.where(rows > max(3, 0.002 * w))[0]
    xs = np.where(cols > max(3, 0.002 * h))[0]
    if ys.size == 0 or xs.size == 0:
        return [0, 0, w, h]
    m = int(margin_frac * w)
    return [max(0, int(xs[0]) - m), max(0, int(ys[0]) - m), min(w, int(xs[-1]) + 1 + m), min(h, int(ys[-1]) + 1 + m)]


def detect_lines(ink: np.ndarray) -> tuple[list[dict], float]:
    """Text-line boxes from the horizontal projection profile. Returns (lines, median_height)."""
    h, w = ink.shape
    prof = (ink > 0).sum(axis=1).astype(np.float32)
    k = max(3, h // 400) | 1
    prof_s = np.convolve(prof, np.ones(k) / k, mode="same")
    if prof_s.max() <= 0:
        return [], 0.0
    p8 = np.clip(prof_s / prof_s.max() * 255, 0, 255).astype(np.uint8).reshape(-1, 1)
    otsu, _ = cv2.threshold(p8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    thr = min(otsu / 255.0 * prof_s.max(), 0.3 * prof_s.max())
    runs = _runs(prof_s > thr)
    if not runs:
        return [], 0.0
    heights = np.array([b - a for a, b in runs])
    med = float(np.median(heights[heights >= np.percentile(heights, 50)])) if heights.size else 0.0
    # merge slivers (diacritics, dots) into the neighbouring line when the gap is small
    merged: list[list[int]] = []
    for a, b in runs:
        if merged and (a - merged[-1][1]) < 0.3 * med and ((b - a) < 0.5 * med or (merged[-1][1] - merged[-1][0]) < 0.5 * med):
            merged[-1][1] = b
        else:
            merged.append([a, b])
    lines = []
    for a, b in merged:
        if (b - a) < 0.3 * med:
            continue
        cols = np.where((ink[a:b] > 0).sum(axis=0) > 0)[0]
        if cols.size == 0:
            continue
        lines.append({"y0": a, "y1": b, "x0": int(cols[0]), "x1": int(cols[-1]) + 1})
    return lines, med


def _longest_run(row: np.ndarray) -> int:
    idx = np.where(row)[0]
    if idx.size == 0:
        return 0
    breaks = np.where(np.diff(idx) > 1)[0]
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [idx.size - 1]))
    return int((idx[ends] - idx[starts] + 1).max())


def detect_footnote_rule(gray: np.ndarray, lines: list[dict], med_h: float) -> int | None:
    """y of a footnote separator rule, or None.

    Connected components of the raw Otsu ink mask (not the speck-cleaned one)
    after a horizontal closing that bridges dotted rules. A rule is a component
    in the lower 60% of the page that is at least 18% of the width wide and
    thin: its mean stroke thickness (area / width) is a few pixels, while a
    closed text line is ~0.7 x the line height thick. Tilted rules pass because
    thickness is measured per column, not by bounding box. Text lines must
    exist both above and below. The widest candidate wins.
    """
    h, w = gray.shape
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kx = max(15, int(0.02 * w)) | 1
    closed = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (kx, 1)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
    max_thickness = max(3.0, 0.25 * med_h) if med_h else 3.0
    best = None
    for i in range(1, n):
        x, y, cw, ch, area = (int(v) for v in stats[i])
        if cw < 0.18 * w or y < 0.4 * h or ch > max(12, 0.06 * h):
            continue
        if area / cw > max_thickness:
            continue
        y0, y1 = y, y + ch
        below = [ln for ln in lines if ln["y0"] >= y1 - 2]
        above = [ln for ln in lines if ln["y1"] <= y0 + 2]
        if not (below and above) or y1 >= 0.97 * h:
            continue
        if best is None or cw > best[1]:
            best = ((y0 + y1) // 2, cw)
    return None if best is None else int(best[0])


def overlay(gray: np.ndarray, lines: list[dict], rule_y: int | None, out: Path) -> None:
    scale = 1000 / gray.shape[1]
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(small, cv2.COLOR_GRAY2BGR)
    for i, ln in enumerate(lines):
        p0 = (int(ln["x0"] * scale), int(ln["y0"] * scale))
        p1 = (int(ln["x1"] * scale), int(ln["y1"] * scale))
        cv2.rectangle(rgb, p0, p1, (0, 170, 0), 1)
        cv2.putText(rgb, str(i + 1), (p1[0] + 4, p1[1]), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 120, 0), 1, cv2.LINE_AA)
    if rule_y is not None:
        y = int(rule_y * scale)
        cv2.line(rgb, (0, y), (rgb.shape[1], y), (0, 0, 255), 2)
    cv2.imwrite(str(out), rgb)


# ---------------------------------------------------------------- pipeline

def preprocess_page(entry: dict, prep: dict) -> dict:
    pid = entry["id"]
    gray0 = cv2.imread(str(config.POC / entry["path"]), cv2.IMREAD_GRAYSCALE)
    if gray0 is None:
        raise SystemExit(f"cannot read {entry['path']}")

    gray1, border_crop = remove_dark_borders(gray0)
    gray2, bg_kernel = flatten_background(gray1)
    gray3 = cv2.fastNlMeansDenoising(gray2, None, h=prep["nlm_h"], templateWindowSize=7, searchWindowSize=21)

    min_area = max(4, int(gray3.size * 1e-5))
    angle, skew_conf = estimate_skew(binarize_clean(gray3, min_area=min_area), prep["max_skew_deg"])
    gray4 = rotate(gray3, angle)

    ink_clean = binarize_clean(gray4, min_area=min_area)
    crop = content_bbox(ink_clean, prep["crop_margin_frac"])
    x0, y0, x1, y1 = crop
    gray_c = gray4[y0:y1, x0:x1]
    ink_c = ink_clean[y0:y1, x0:x1]

    lines, med_h = detect_lines(ink_c)
    rule_y = detect_footnote_rule(gray_c, lines, med_h)

    ws = int(np.clip(2 * med_h + 1, 25, 75)) | 1 if med_h else 41
    thr = threshold_sauvola(gray_c, window_size=ws, k=prep["sauvola_k"])
    bw = np.where(gray_c > thr, 255, 0).astype(np.uint8)

    cv2.imwrite(str(config.GRAY / f"{pid}.png"), gray_c)
    cv2.imwrite(str(config.BW / f"{pid}.png"), bw)
    upscaled = False
    if entry.get("low_res"):
        f = prep["upscale_factor"]
        up = cv2.resize(gray_c, None, fx=f, fy=f, interpolation=cv2.INTER_LANCZOS4)
        cv2.imwrite(str(config.GRAY_2X / f"{pid}.png"), up)
        upscaled = True
    overlay(gray_c, lines, rule_y, config.OVERLAYS / f"{pid}.png")
    if rule_y is not None:
        pad = max(2, int(0.15 * med_h)) if med_h else 3
        body = gray_c[: max(1, rule_y - pad)]
        foot = gray_c[min(gray_c.shape[0] - 1, rule_y + pad):]
        cv2.imwrite(str(config.REGIONS / f"{pid}_body.png"), body)
        foot2 = cv2.resize(foot, None, fx=2, fy=2, interpolation=cv2.INTER_LANCZOS4)
        cv2.imwrite(str(config.REGIONS / f"{pid}_foot.png"), foot2)
    else:
        for suffix in ("_body", "_foot"):
            (config.REGIONS / f"{pid}{suffix}.png").unlink(missing_ok=True)

    flags = []
    if abs(angle) > 3.0:
        flags.append("large_skew")
    if skew_conf < 0.10:
        flags.append("deskew_low_confidence")
    if not lines:
        flags.append("no_lines_detected")

    params = {
        "id": pid,
        "input": entry["path"],
        "input_size": [int(gray0.shape[1]), int(gray0.shape[0])],
        "border_crop": border_crop,
        "angle": angle,
        "skew_confidence": skew_conf,
        "bg_kernel": bg_kernel,
        "nlm_h": prep["nlm_h"],
        "crop_box": crop,
        "output_size": [int(gray_c.shape[1]), int(gray_c.shape[0])],
        "sauvola": {"window": ws, "k": prep["sauvola_k"]},
        "median_line_height": round(med_h, 1),
        "n_lines": len(lines),
        "line_boxes": lines,
        "footnote_rule_y": rule_y,
        "upscaled_2x": upscaled,
        "flags": flags,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    write_json_atomic(config.PAGES / f"{pid}.prep.json", params)
    return params


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pages", help="comma-separated page ids (default: all in manifest)")
    ap.add_argument("--force", action="store_true", help="redo pages that already have a prep.json")
    args = ap.parse_args()

    for d in (config.GRAY, config.BW, config.GRAY_2X, config.OVERLAYS, config.REGIONS):
        d.mkdir(exist_ok=True)
    pages = load_manifest(config.MANIFEST)
    if args.pages:
        wanted = set(args.pages.split(","))
        pages = [p for p in pages if p["id"] in wanted]

    for entry in pages:
        out = config.PAGES / f"{entry['id']}.prep.json"
        if out.exists() and not args.force:
            print(f"{entry['id']:12s} skip (exists)")
            continue
        p = preprocess_page(entry, config.PREP)
        print(f"{p['id']:12s} angle={p['angle']:+5.2f} conf={p['skew_confidence']:.2f} "
              f"crop={p['crop_box']} lines={p['n_lines']:2d} rule_y={p['footnote_rule_y']} "
              f"flags={','.join(p['flags']) or '-'}")
    print(f"\ndone. Check overlays/ visually (green = lines, red = footnote rule).")


if __name__ == "__main__":
    main()
