"""Collect random number crops from the books in the dev database (digits experiment, step 1).

One book per distinct source PDF. Candidates are the OCR tokens that hold a number (`digit` flag or a
digit in the primary reading) and carry a word box; a seeded random sample per book is cropped from
the page's gray image with some padding. Writes:

    crops/<id>.png          the crop (gray, as the OCR saw the page)
    manifest.json           [{id, book, title, page, line, token, bbox, crop, t, alt, tess}]
    sheets/sheet-NN.png     numbered contact sheets, to write the true digits into truth.json

Run from the project root:  .venv/bin/python playground/digits/collect.py
"""

from __future__ import annotations

import json
import os
import random
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
from ocr.models import Line  # noqa: E402

BOOKS = (9, 13, 15, 16, 17, 18, 19, 20, 21)  # one per source PDF (the newest upload of each)
PER_BOOK = 24
SEED = 7
DIGIT = re.compile(r"[0-9٠-٩۰-۹]")
PAD_X = 0.35  # of the box height, on each side
PAD_Y = 0.30


def candidates(book: Book) -> list[dict]:
    out = []
    lines = (
        Line.objects.filter(page__book=book)
        .select_related("page", "page__preprocess")
        .order_by("page__number", "order")
    )
    for line in lines:
        pre = getattr(line.page, "preprocess", None)
        if pre is None or not pre.gray_image:
            continue
        for i, tok in enumerate(line.tokens or []):
            box = tok.get("bbox")
            if not box or not (tok.get("digit") or DIGIT.search(tok.get("t") or "")):
                continue
            out.append(
                {
                    "book": book.pk,
                    "title": book.title,
                    "page": line.page.number,
                    "line": line.pk,
                    "token": i,
                    "bbox": list(box),
                    "image": pre.gray_image.path,
                    "t": tok.get("t"),
                    "alt": tok.get("alt"),
                    "tess": tok.get("tess"),
                }
            )
    return out


def crop(item: dict) -> Image.Image:
    x0, y0, x1, y1 = item["bbox"]
    h = max(8, y1 - y0)
    with Image.open(item["image"]) as img:
        img = img.convert("L")
        box = (
            max(0, int(x0 - PAD_X * h)),
            max(0, int(y0 - PAD_Y * h)),
            min(img.width, int(x1 + PAD_X * h)),
            min(img.height, int(y1 + PAD_Y * h)),
        )
        item["crop"] = list(box)
        return img.crop(box)


def sheets(items: list[dict], per_sheet: int = 16) -> None:
    folder = HERE / "sheets"
    folder.mkdir(exist_ok=True)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    for s in range(0, len(items), per_sheet):
        chunk = items[s : s + per_sheet]
        rows = []
        for n, item in enumerate(chunk, start=s + 1):
            im = Image.open(HERE / "crops" / f"{item['id']}.png")
            scale = 90 / max(1, im.height)
            im = im.resize((max(1, int(im.width * scale)), 90))
            rows.append((n, im))
        width = max(im.width for _, im in rows) + 110
        sheet = Image.new("L", (width, sum(im.height + 14 for _, im in rows) + 10), 255)
        draw = ImageDraw.Draw(sheet)
        y = 10
        for n, im in rows:
            draw.text((8, y + 30), f"{n:03d}", fill=0, font=font)
            sheet.paste(im, (100, y))
            draw.line((0, y + im.height + 6, width, y + im.height + 6), fill=200)
            y += im.height + 14
        sheet.save(folder / f"sheet-{s // per_sheet + 1:02d}.png")


def extra(books: dict[int, int], seed: int = SEED + 1) -> None:
    """Append `n` more random crops per book (not taken before) to the manifest, numbered after it."""
    manifest = json.loads((HERE / "manifest.json").read_text())
    taken = {(m["line"], m["token"]) for m in manifest}
    rng = random.Random(seed)
    added: list[dict] = []
    for book_id, n in books.items():
        book = Book.objects.get(pk=book_id)
        found = [c for c in candidates(book) if (c["line"], c["token"]) not in taken]
        chosen = found if len(found) <= n else rng.sample(found, n)
        chosen.sort(key=lambda c: (c["page"], c["line"], c["token"]))
        print(f"book {book_id}: {len(found)} left, {len(chosen)} more taken")
        added.extend(chosen)
    start = len(manifest) + 1
    for n, item in enumerate(added, start=start):
        item["n"] = n
        item["id"] = f"{n:03d}-b{item['book']}-p{item['page']}"
        crop(item).save(HERE / "crops" / f"{item['id']}.png")
    everything = manifest + [{k: v for k, v in item.items() if k != "image"} for item in added]
    (HERE / "manifest.json").write_text(json.dumps(everything, ensure_ascii=False, indent=1))
    # sheets for the new crops only, numbered on
    for item in added:
        item.setdefault("image", None)
    sheets_from(added, first_sheet=(start - 1) // 16 + 1)
    print(f"{len(added)} crops added ({start}–{start + len(added) - 1})")


def sheets_from(items: list[dict], first_sheet: int) -> None:
    folder = HERE / "sheets"
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    for s in range(0, len(items), 16):
        chunk = items[s : s + 16]
        rows = []
        for item in chunk:
            im = Image.open(HERE / "crops" / f"{item['id']}.png")
            scale = 90 / max(1, im.height)
            rows.append((item["n"], im.resize((max(1, int(im.width * scale)), 90))))
        width = max(im.width for _, im in rows) + 110
        sheet = Image.new("L", (width, sum(im.height + 14 for _, im in rows) + 10), 255)
        draw = ImageDraw.Draw(sheet)
        y = 10
        for n, im in rows:
            draw.text((8, y + 30), f"{n:03d}", fill=0, font=font)
            sheet.paste(im, (100, y))
            draw.line((0, y + im.height + 6, width, y + im.height + 6), fill=200)
            y += im.height + 14
        sheet.save(folder / f"extra-{first_sheet + s // 16:02d}.png")


def main() -> None:
    if "--extra" in sys.argv:  # e.g. --extra 19:30 20:30
        pairs = sys.argv[sys.argv.index("--extra") + 1 :]
        extra({int(a): int(b) for a, b in (pair.split(":") for pair in pairs)})
        return
    rng = random.Random(SEED)
    (HERE / "crops").mkdir(exist_ok=True)
    picked: list[dict] = []
    for book_id in BOOKS:
        book = Book.objects.filter(pk=book_id).first()
        if book is None:
            continue
        found = candidates(book)
        chosen = found if len(found) <= PER_BOOK else rng.sample(found, PER_BOOK)
        chosen.sort(key=lambda c: (c["page"], c["line"], c["token"]))
        print(f"book {book_id} «{book.title}»: {len(found)} candidates, {len(chosen)} taken")
        picked.extend(chosen)
    for n, item in enumerate(picked, start=1):
        item["n"] = n
        item["id"] = f"{n:03d}-b{item['book']}-p{item['page']}"
        crop(item).save(HERE / "crops" / f"{item['id']}.png")
    manifest = [{k: v for k, v in item.items() if k != "image"} for item in picked]
    (HERE / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    sheets(picked)
    print(f"{len(picked)} crops → {HERE / 'crops'}; sheets → {HERE / 'sheets'}")


if __name__ == "__main__":
    main()
