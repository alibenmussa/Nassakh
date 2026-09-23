"""Repair the text layer of born-digital PDFs (sample 3) and write GT drafts.

Word-generated PDFs map the lam-alef ligature glyph to two code points in the
wrong order, so "الإسلام" is extracted as "اإلسالم". Text alone cannot fix
this ("ال" is also the definite article), but glyph geometry can: in
PyMuPDF's rawdict the alef that belongs to the ligature has a *zero-width*
box sitting exactly on the lam's right edge.

Per born-digital page this script:
  1. rebuilds each visual line from rawdict segments that share a baseline,
     sorted right-to-left (Word splits bold/italic runs into separate lines),
  2. swaps every zero-width (alef, lam) pair back to lam-alef,
  3. suppresses MuPDF's synthesised spaces (TEXT_INHIBIT_SPACES) and moves spaces and
     punctuation whose pen position contradicts their stream position (Word puts a trailing
     comma or a justification space at the line end but mid-stream),
  4. applies NFKC and writes gt/drafts/<page_id>.txt plus a repair report.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

import pymupdf

import config
from common import load_manifest, normalize_ws

ALEFS = {"ا": "لا", "أ": "لأ", "إ": "لإ", "آ": "لآ"}


def _w(c: dict) -> float:
    return c["bbox"][2] - c["bbox"][0]


def _touches(c1: dict, c2: dict, tol: float = 0.6) -> bool:
    """Do two glyph boxes share a vertical edge (either direction)?"""
    return abs(c1["bbox"][0] - c2["bbox"][2]) <= tol or abs(c2["bbox"][0] - c1["bbox"][2]) <= tol


def _is_neutral(ch: str) -> bool:
    return ch.isspace() or unicodedata.category(ch).startswith("P")


def fix_misplaced(chars: list[dict]) -> list[dict]:
    """Move spaces/punctuation whose pen position contradicts their stream position.

    Word emits a trailing comma or a justification space at the visual end of
    the line but in the middle (or start) of the character stream. Such glyphs
    have an origin far outside the span of their stream neighbours; they are
    removed and re-inserted where their x position fits (right-to-left).
    """
    solids = [c for c in chars if not _is_neutral(c["c"])]
    if len(solids) < 2:
        return chars
    widths = sorted(_w(c) for c in solids if _w(c) > 0)
    tol = max(2.0, 0.5 * (widths[len(widths) // 2] if widths else 4.0))
    keep, misplaced = [], []
    for i, c in enumerate(chars):
        if not _is_neutral(c["c"]):
            keep.append(c)
            continue
        prev = next((chars[j] for j in range(i - 1, -1, -1) if not _is_neutral(chars[j]["c"])), None)
        nxt = next((chars[j] for j in range(i + 1, len(chars)) if not _is_neutral(chars[j]["c"])), None)
        x = c["origin"][0]
        if prev and nxt:
            lo = min(prev["origin"][0], nxt["origin"][0]) - tol
            hi = max(prev["bbox"][2], nxt["bbox"][2]) + tol
            ok = lo <= x <= hi
        else:
            anchor = prev or nxt
            ok = abs(x - anchor["origin"][0]) <= 4 * tol
        (keep if ok else misplaced).append(c)
    for c in misplaced:
        idx = len(keep)
        for j, k in enumerate(keep):
            if not _is_neutral(k["c"]) and k["origin"][0] < c["origin"][0] - 0.5:
                idx = j
                break
        keep.insert(idx, c)
    return keep


def repair_chars(chars: list[dict]) -> tuple[str, int, int]:
    out, i, fixes, dropped = [], 0, 0, 0  # dropped kept for the report (always 0 now)
    while i < len(chars):
        c = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else None
        # zero-width alef followed by lam at the same x -> reversed ligature
        if c["c"] in ALEFS and _w(c) < 0.05 and nxt and nxt["c"] == "ل" and _touches(c, nxt):
            out.append(ALEFS[c["c"]])
            fixes += 1
            i += 2
            continue
        # lam followed by zero-width alef (other emission order)
        if c["c"] == "ل" and nxt and nxt["c"] in ALEFS and _w(nxt) < 0.05 and _touches(c, nxt):
            out.append(ALEFS[nxt["c"]])
            fixes += 1
            i += 2
            continue
        out.append(c["c"])
        i += 1
    return "".join(out), fixes, dropped


def visual_lines(block: dict) -> list[list[dict]]:
    """Group rawdict line segments by baseline and order them right-to-left."""
    by_baseline: dict[int, list[dict]] = defaultdict(list)
    for seg in block["lines"]:
        chars = [ch for span in seg["spans"] for ch in span["chars"]]
        if not chars:
            continue
        y = round(chars[0]["origin"][1] / 2.0)  # 2pt tolerance
        solid = [c for c in chars if not c["c"].isspace()] or chars
        by_baseline[y].append({"chars": chars, "solid": solid, "x1": max(c["bbox"][2] for c in solid)})
    lines = []
    for y in sorted(by_baseline):
        segs = sorted(by_baseline[y], key=lambda s: -s["x1"])
        merged: list[dict] = []
        last_solid = None
        for s in segs:
            if last_solid is not None and not _touches(last_solid, s["solid"][0], tol=1.5):
                # separate text objects that do not touch: keep a word boundary
                merged.append({"c": " ", "bbox": (0, 0, 0, 0), "origin": (0, 0)})
            merged.extend(s["chars"])
            last_solid = s["solid"][-1]
        lines.append(merged)
    return lines


def repair_page(page: pymupdf.Page) -> tuple[str, dict]:
    raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_LIGATURES | pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_INHIBIT_SPACES)
    blocks_out, fixes, dropped, n_chars = [], 0, 0, 0
    for block in raw["blocks"]:
        if block.get("type") != 0:
            continue
        lines_out = []
        for chars in visual_lines(block):
            n_chars += len(chars)
            text, f, d = repair_chars(fix_misplaced(chars))
            fixes += f
            dropped += d
            if text.strip():
                lines_out.append(text)
        if lines_out:
            blocks_out.append("\n".join(lines_out))
    text = normalize_ws(unicodedata.normalize("NFKC", "\n\n".join(blocks_out)))
    text = re.sub(r" +([،.:؛!؟,;])", r"\1", text)  # no space before punctuation
    return text, {"chars": n_chars, "lam_alef_fixed": fixes, "spaces_dropped": dropped,
                  "suspicious_left": text.count("اإل") + text.count("األ")}


def main() -> None:
    drafts = config.GT / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    report = []
    for entry in load_manifest(config.MANIFEST):
        if not entry.get("born_digital"):
            continue
        doc = pymupdf.open(config.INPUT / entry["pdf_file"])
        text, s = repair_page(doc[entry["pdf_page"] - 1])
        out = drafts / f"{entry['id']}.txt"
        out.write_text(text + "\n", encoding="utf-8")
        line = (f"{entry['id']}: {s['chars']} chars, {s['lam_alef_fixed']} lam-alef repaired, "
                f"{s['spaces_dropped']} spurious spaces dropped, 'لا' occurs {text.count('لا')}x, "
                f"suspicious leftovers {s['suspicious_left']} -> {out.relative_to(config.POC)}")
        print(line)
        report.append(line)
        doc.close()
    (drafts / "s3_repair_report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\nDrafts are the ground-truth starting point for born-digital pages. Review, then promote with make_gt_drafts.py --promote.")


if __name__ == "__main__":
    main()
