"""Numbers Qari wrote as letters (digits experiment, round 3).

Qari sometimes writes an Arabic-Indic number as the letters it looks like (٥ → ه, ٤ → ع) or drops it,
so the numbers pass (D50), which only reads tokens holding a digit, never sees it:

    «(٧٥٤–٧٧٥م)» → «(ع ه – ه م)»        «(٨٤٧–٨٦١م)» → «(هـ – م)»        «(٥) الخزر» → «(ه) الخزر»

Two kinds of candidates in the books that print Arabic-Indic digits:
    letter  a bracketed single letter, «(ه)», «(ع)», «(د)»: a list item, lettered (أ ب ج د هـ) or
            numbered (١ ٢ ٣ ٤ ٥); only the scan tells which
    date    a bracketed group with a dash that ends in «م» or «هـ» and holds no digit, «(هـ – م)»:
            never real text
    lone    a lone «ا», «ه», «هـ» or «ع» outside brackets: «ا» is no Arabic word (a digit ١, a
            footnote mark?), «هـ» is the hijri mark after a year, or a ٥ Qari split off a number
Kraken reads each candidate's area (its box, or the gap between its boxed neighbours) as the numbers
pass would; a lone letter next to a number is read again with that number («|wide»). Then the crops
go on a sheet with Qari's and Kraken's readings, to label by eye.

    .venv/bin/python playground/digits/letters.py find      # letters_request.json + letters.json
    .venv-kraken/bin/python -P ocr/engines/kraken_runner.py < playground/digits/letters_request.json \
        > playground/digits/letters_result.json
    .venv/bin/python playground/digits/letters.py sheet     # sheets/letters-NN.png
    .venv/bin/python playground/digits/letters.py score     # against letters_truth.json
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")

import django  # noqa: E402

django.setup()

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from books.models import Book  # noqa: E402
from ocr import numbers as nb  # noqa: E402
from ocr.models import Line  # noqa: E402

DIGIT = re.compile(r"[0-9٠-٩۰-۹]")
LETTER = re.compile(r"^\(\s*[ء-ي]ـ?\s*\)[.،:]?$")
DATE_END = re.compile(r"(م|هـ|ه)\s*\)[.،:]?$")
DASH = re.compile(r"[–—\-/]")
LONE = re.compile(r"^[اهع]ـ?[.،]?$")
MAX_GROUP = 8
BOOKS = (9, 13, 17, 18, 19, 20)  # the books printed with Arabic-Indic digits, one per source PDF


def groups(tokens: list[dict]):
    """`(first, last)` token indexes of each bracket group of a line: a token opening «(» up to the
    first token that closes it, within `MAX_GROUP` tokens."""
    i = 0
    while i < len(tokens):
        text = str(tokens[i].get("t") or "")
        if text.startswith("("):
            for j in range(i, min(len(tokens), i + MAX_GROUP)):
                closing = str(tokens[j].get("t") or "")
                if ")" in (closing[1:] if j == i else closing):
                    yield i, j
                    i = j
                    break
        i += 1


def kind_of(tokens: list[dict], first: int, last: int) -> str:
    text = " ".join(str(t.get("t") or "") for t in tokens[first : last + 1])
    if DIGIT.search(text):
        return ""
    if LETTER.match(text):
        return "letter"
    if DASH.search(text) and DATE_END.search(text) and len(text) <= 24:
        return "date"
    return ""


def area_of(tokens: list[dict], first: int, last: int, line_bbox: list[int]) -> list[int]:
    """The group's box, or the gap between its nearest boxed neighbours (right to left)."""
    lx0, ly0, lx1, ly1 = (int(v) for v in line_bbox)
    inside = [t["bbox"] for t in tokens[first : last + 1] if t.get("bbox")]
    if len(inside) == last - first + 1:
        return [min(b[0] for b in inside), min(b[1] for b in inside),
                max(b[2] for b in inside), max(b[3] for b in inside)]
    right = tokens[first]["bbox"][2] if tokens[first].get("bbox") else next(
        (int(tokens[j]["bbox"][0]) for j in range(first - 1, -1, -1) if tokens[j].get("bbox")), lx1)
    left = tokens[last]["bbox"][0] if tokens[last].get("bbox") else next(
        (int(tokens[j]["bbox"][2]) for j in range(last + 1, len(tokens)) if tokens[j].get("bbox")), lx0)
    return [int(left), ly0, int(right), ly1]


def lone_of(tokens: list[dict]):
    """`(index, neighbour or None)` of each lone letter of a line; the neighbour: a number token right
    before or after it."""
    for i, token in enumerate(tokens):
        if LONE.match(str(token.get("t") or "")):
            near = next((j for j in (i - 1, i + 1) if 0 <= j < len(tokens) and nb.is_number(tokens[j])), None)
            yield i, near


def find() -> None:
    found, pages = [], {}
    for book in Book.objects.filter(pk__in=BOOKS).order_by("pk"):
        if nb.book_style(book) != nb.ARABIC_INDIC:
            continue
        lines = (Line.objects.filter(page__book=book).select_related("page", "page__preprocess")
                 .order_by("page__number", "order"))
        for line in lines:
            pre = getattr(line.page, "preprocess", None)
            if not line.bbox or pre is None or not pre.gray_image:
                continue
            tokens = line.tokens or []
            for first, last in groups(tokens):
                kind = kind_of(tokens, first, last)
                if not kind:
                    continue
                bbox = area_of(tokens, first, last, line.bbox)
                key = f"b{book.pk}p{line.page.number}l{line.order}t{first}"
                found.append({
                    "key": key, "kind": kind, "book": book.pk, "page": line.page.number,
                    "line": line.order, "first": first, "last": last, "bbox": bbox,
                    "boxed": all(t.get("bbox") for t in tokens[first : last + 1]),
                    "qari": " ".join(str(t.get("t") or "") for t in tokens[first : last + 1]),
                    "tess": " ".join(str(t.get("tess") or "") for t in tokens[first : last + 1]).strip(),
                    "before": str(tokens[first - 1].get("t")) if first else "",
                    "line_text": line.text, "image": pre.gray_image.path,
                })
                pages.setdefault(pre.gray_image.path, []).append({"id": key, "bbox": bbox})
            for i, near in lone_of(tokens):
                key = f"b{book.pk}p{line.page.number}l{line.order}t{i}"
                bbox = area_of(tokens, i, i, line.bbox)
                wide = area_of(tokens, min(i, near), max(i, near), line.bbox) if near is not None else None
                found.append({
                    "key": key, "kind": "lone", "book": book.pk, "page": line.page.number,
                    "line": line.order, "first": i, "last": i, "bbox": bbox, "wide": wide,
                    "boxed": bool(tokens[i].get("bbox")),
                    "qari": str(tokens[i].get("t")),
                    "near": str(tokens[near].get("t")) if near is not None else "",
                    "tess": str(tokens[i].get("tess") or ""),
                    "before": " ".join(str(t.get("t")) for t in tokens[max(0, i - 2) : i]),
                    "after": " ".join(str(t.get("t")) for t in tokens[i + 1 : i + 3]),
                    "line_text": line.text, "image": pre.gray_image.path,
                })
                pages.setdefault(pre.gray_image.path, []).append({"id": key, "bbox": bbox})
                if wide:
                    pages[pre.gray_image.path].append({"id": key + "|wide", "bbox": wide})
    request = {"model": str(ROOT / "models" / "kraken" / "all_arabic_scripts.mlmodel"),
               "pages": [{"image": image, "lines": lines} for image, lines in pages.items()]}
    (HERE / "letters.json").write_text(json.dumps(found, ensure_ascii=False, indent=1))
    (HERE / "letters_request.json").write_text(json.dumps(request, ensure_ascii=False))
    kinds = {k: sum(1 for f in found if f["kind"] == k) for k in ("letter", "date", "lone")}
    print(len(found), "candidates", kinds)


def results() -> dict:
    path = HERE / "letters_result.json"
    return {r["id"]: r for r in json.loads(path.read_text())["lines"]} if path.exists() else {}


def sheet() -> None:
    found = json.loads((HERE / "letters.json").read_text())
    read = results()
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 34)
    rows_per_sheet, row_h, width = 12, 110, 900
    (HERE / "sheets").mkdir(exist_ok=True)
    for s in range(0, len(found), rows_per_sheet):
        chunk = found[s : s + rows_per_sheet]
        out = Image.new("RGB", (width, row_h * len(chunk)), "white")
        draw = ImageDraw.Draw(out)
        for r, f in enumerate(chunk):
            y = r * row_h
            with Image.open(f["image"]) as img:
                x0, y0, x1, y1 = f.get("wide") or f["bbox"]
                pad = int(0.6 * (y1 - y0))
                crop = img.crop((max(0, x0 - pad), max(0, y0 - pad // 2),
                                 min(img.width, x1 + pad), min(img.height, y1 + pad // 2))).convert("RGB")
            scale = min(1.6, (row_h - 10) / crop.height, 560 / crop.width)
            crop = crop.resize((max(1, int(crop.width * scale)), max(1, int(crop.height * scale))))
            out.paste(crop, (width - crop.width - 10, y + 5))
            draw.text((10, y + 30), f"{s + r + 1}", fill="black", font=font)
            draw.line((0, y + row_h - 1, width, y + row_h - 1), fill="#ddd")
        out.save(HERE / "sheets" / f"letters-{s // rows_per_sheet + 1:02d}.png")
    for n, f in enumerate(found, 1):  # the readings, to read next to the sheets
        kraken = (read.get(f["key"]) or {}).get("text", "?").strip()
        wide = (read.get(f["key"] + "|wide") or {}).get("text", "").strip() if f.get("wide") else ""
        qari = f"{f.get('before', '')} [{f['qari']}] {f.get('after', '')}" if f["kind"] == "lone" else f["qari"]
        print(f"{n:3} {f['key']:>16} {f['kind']:6} {'box' if f['boxed'] else 'gap'} | {qari} || K: {kraken}"
              + (f" || wide: {wide}" if wide else ""))


def score() -> None:
    found = json.loads((HERE / "letters.json").read_text())
    read = results()
    truth = json.loads((HERE / "letters_truth.json").read_text())
    tally: dict[str, dict[str, int]] = {}
    for f in found:
        want = truth.get(f["key"])
        if want is None:
            continue
        got = " ".join(((read.get(f["key"]) or {}).get("text") or "").split())
        is_number = bool(DIGIT.search(want))
        bucket = f"{f['kind']}:{'number' if is_number else 'letter'}"
        t = tally.setdefault(bucket, {"n": 0, "exact": 0, "says_number": 0})
        t["n"] += 1
        t["exact"] += got.replace(" ", "") == want.replace(" ", "")
        t["says_number"] += bool(DIGIT.search(got))
        if got.replace(" ", "") != want.replace(" ", ""):
            print(f"  {f['key']:>18} [{bucket}] truth {want!r:14} Kraken {got!r:14} Qari {f['qari']!r}")
    print(json.dumps(tally, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    {"find": find, "sheet": sheet, "score": score}[sys.argv[1]]()
