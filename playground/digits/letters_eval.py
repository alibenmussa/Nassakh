"""Which rule lets Kraken turn Qari's letter back into the digit it stands for, safely? (round 3)

On the labelled lone and bracketed letters (letters_truth.json), for the areas the pass could read:
    own     the letter's own word box
    tight   no box, but its boxed neighbours hold only it (and punctuation) between them
Kraken reads each area with three horizontal margins (0.3 = the numbers pass's, 0.15, 0.05 of the
height), and each reading is judged by the rule "Kraken read exactly one number where Qari wrote the
letter, and agrees with Qari on the rest of the area" (brackets, punctuation), with or without first
dropping Kraken's characters that lie outside the area (the margin's bits of the neighbours).

    .venv/bin/python playground/digits/letters_eval.py request
    .venv-kraken/bin/python -P ocr/engines/kraken_runner.py < playground/digits/letters_eval_request.json \
        > playground/digits/letters_eval_result.json
    .venv/bin/python playground/digits/letters_eval.py score
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")

import django  # noqa: E402

django.setup()

from ocr import numbers as nb  # noqa: E402
from ocr.models import Line  # noqa: E402

MARGINS = (None, 0.15, 0.05)
LETTER = re.compile(r"[اهع]ـ?")
DIGIT = re.compile(r"[0-9٠-٩۰-۹]")
# Kraken's digits as the page's: by value, but its Persian six ۶ is the page's ٤ (the same shape)
KRAKEN_DIGITS = str.maketrans("0123456789۰۱۲۳۴۵۶۷۸۹", "٠١٢٣٤٥٦٧٨٩٠١٢٣٤٥٤٧٨٩")
WORDISH = re.compile(r"[ء-ي0-9٠-٩۰-۹A-Za-z]")


def area(tokens: list[dict], i: int, line_bbox: list[int]):
    """`(mode, bbox, Qari's text of the area)` for token `i`, or None when it has no usable area."""
    token = tokens[i]
    if token.get("bbox"):
        return "own", [int(v) for v in token["bbox"]], str(token["t"])
    lx0, ly0, lx1, ly1 = (int(v) for v in line_bbox)
    r = next((j for j in range(i - 1, -1, -1) if tokens[j].get("bbox")), None)
    left = next((j for j in range(i + 1, len(tokens)) if tokens[j].get("bbox")), None)
    inside = range((r + 1) if r is not None else 0, left if left is not None else len(tokens))
    if any(WORDISH.search(str(tokens[j].get("t") or "")) for j in inside if j != i):
        return None  # the gap holds words or numbers too: no telling where the letter is
    x1 = int(tokens[r]["bbox"][0]) if r is not None else lx1
    x0 = int(tokens[left]["bbox"][2]) if left is not None else lx0
    if x1 - x0 < 3:
        return None
    return "tight", [x0, ly0, x1, ly1], " ".join(str(tokens[j].get("t") or "") for j in inside)


def load():
    truth = json.loads((HERE / "letters_truth.json").read_text())
    found = {f["key"]: f for f in json.loads((HERE / "letters.json").read_text())}
    out = []
    for key, lab in truth.items():
        f = found[key]
        if f["kind"] == "date":
            continue
        line = Line.objects.select_related("page__preprocess").get(
            page__book_id=f["book"], page__number=f["page"], order=f["line"])
        got = area(line.tokens, f["first"], line.bbox)
        out.append({"key": key, "truth": lab, "f": f, "area": got, "image": line.page.preprocess.gray_image.path,
                    "tokens": line.tokens, "line_bbox": line.bbox})
    return out


def trimmed(chars: list, bbox: list[int]) -> list:
    tol = 0.15 * (bbox[3] - bbox[1])
    keep = []
    for row in chars:
        if row[1] is None:
            keep.append(row)
            continue
        mid = (row[1] + row[2]) / 2
        if bbox[0] - tol <= mid <= bbox[2] + tol:
            keep.append(row)
    return keep


def verdict(chars: list, qari_area: str, letter_token: str):
    """Kraken's number for the letter, or None: one digit run, and the rest agrees with Qari's area."""
    text = "".join(str(row[0]) for row in chars).replace(" ", "")
    runs = re.findall(r"[0-9٠-٩۰-۹]+", text)
    if len(runs) != 1:
        return None
    rest = DIGIT.sub("", text)
    qari_rest = qari_area.replace(letter_token, LETTER.sub("", letter_token), 1).replace(" ", "")
    return runs[0].translate(KRAKEN_DIGITS) if rest == qari_rest else None


MARKS = re.compile(r"[\u064B-\u0652\u0670ـ\s]")


def plain(text: str) -> str:
    return MARKS.sub("", text.translate(KRAKEN_DIGITS))


def anchored(line_text: str, tokens: list[dict], i: int, n: int = 2):
    """Kraken's number for letter token `i` from its reading of the whole line: the digits between
    Qari's neighbours (their last / first `n` letters and the letter token's own punctuation), when
    they are found exactly once."""
    token = str(tokens[i]["t"])
    m = LETTER.search(token)
    if not m:
        return None
    before, after = plain(token[: m.start()]), plain(token[m.end():])
    prev = plain(str(tokens[i - 1]["t"])) if i > 0 else ""
    nxt = plain(str(tokens[i + 1]["t"])) if i + 1 < len(tokens) else ""
    left = ("^" if i == 0 else re.escape(prev[-n:])) + re.escape(before)
    right = re.escape(after) + (re.escape(nxt[:n]) if nxt else "$")
    found = re.findall(left + "([٠-٩]+)" + right, plain(line_text))
    return found[0] if len(found) == 1 else None


ALEFS = str.maketrans("أإآٱى", "اااای")


def skeleton(text: str) -> str:
    """Letters and digits only (digits as the page's, alef forms and ى folded, no marks or tatweel)."""
    text = text.translate(KRAKEN_DIGITS).translate(ALEFS)
    return re.sub(r"[^ء-ي٠-٩A-Za-z]|ـ", "", text)


def anchored2(line_text: str, tokens: list[dict], i: int, n: int = 2):
    """As `anchored`, on skeletons: the neighbours' letters around the digits, punctuation ignored."""
    prev = next((skeleton(str(tokens[j]["t"])) for j in range(i - 1, -1, -1) if skeleton(str(tokens[j]["t"]))), None)
    nxt = next((skeleton(str(tokens[j]["t"])) for j in range(i + 1, len(tokens)) if skeleton(str(tokens[j]["t"]))), None)
    left = re.escape(prev[-n:]) if prev else "^"
    right = re.escape(nxt[:n]) if nxt else "$"
    found = re.findall(left + "([٠-٩]+)" + right, skeleton(line_text))
    return found[0] if len(found) == 1 else None


if sys.argv[1] == "request":
    pages: dict[str, list] = {}
    rows = load()
    for c in rows:
        pages.setdefault(c["image"], []).append({"id": f"{c['key']}|line", "bbox": c["line_bbox"]})
        if not c["area"]:
            continue
        for m in MARGINS:
            line = {"id": f"{c['key']}|{m}", "bbox": c["area"][1]}
            if m is not None:
                line["margin_x"] = m
            pages.setdefault(c["image"], []).append(line)
    request = {"model": str(ROOT / "models" / "kraken" / "all_arabic_scripts.mlmodel"),
               "pages": [{"image": image, "lines": lines} for image, lines in pages.items()]}
    (HERE / "letters_eval_request.json").write_text(json.dumps(request, ensure_ascii=False))
    modes = Counter(c["area"][0] if c["area"] else "none" for c in rows)
    print(len(rows), "labelled letters", dict(modes))
else:
    read = {r["id"]: r for r in json.loads((HERE / "letters_eval_result.json").read_text())["lines"]}
    rows = load()
    for trim in (False, True):
        for m in MARGINS:
            tally = Counter()
            for c in rows:
                kind = c["truth"]["is"]
                if not c["area"]:
                    tally[f"{kind}:no-area"] += 1
                    continue
                mode, bbox, qari_area = c["area"]
                chars = read[f"{c['key']}|{m}"]["chars"]
                if trim:
                    chars = trimmed(chars, bbox)
                got = verdict(chars, qari_area, c["f"]["qari"])
                if got is None:
                    tally[f"{kind}:kept"] += 1
                elif kind == "digit":
                    want = c["truth"].get("value")
                    tally["digit:changed"] += 1
                    tally["digit:exact"] += bool(want) and got == want
                else:
                    tally[f"{kind}:CHANGED"] += 1
            print(f"trim={trim!s:5} margin_x={m!s:4}", dict(sorted(tally.items())))
    for combo in ("anchor2", "area+anchor2", "final"):
        tally = Counter()
        for c in rows:
            kind = c["truth"]["is"]
            line = read[f"{c['key']}|line"]["text"]
            by_area = verdict(read[f"{c['key']}|0.15"]["chars"], c["area"][2], c["f"]["qari"]) if c["area"] else None
            if by_area == "٠" and "ه" in c["f"]["qari"]:
                by_area = "٥"  # a circle: Qari's ه, Kraken's zero, the page's ٥
            a1 = anchored(line, c["tokens"], c["f"]["first"])
            a2 = anchored2(line, c["tokens"], c["f"]["first"], 1 if "n=1" in combo else 2)
            got = {"anchor": a1, "anchor2": a2, "anchor2 n=1": a2, "area+anchor2": by_area or a2,
                   "anchor2+area": a2 or by_area, "final": None,
                   "agree": (a2 if (by_area is None or a2 is None or a2 == by_area) else None) and (a2 or by_area)}[combo]
            if combo == "final":
                own = read.get(f"{c['key']}|0.15") if c["area"] and c["area"][0] == "own" else None
                if own and skeleton(own["text"]) == skeleton(c["f"]["qari"]) and "".join(own["text"].split()):
                    got = None  # the letter's own box, read alone, shows Qari's letter: a letter
                else:
                    got = a2 or by_area
                if got != c["truth"].get("value"):
                    print(f"    final {c['key']:>16} {kind:6} got {got!r:6} truth {c['truth'].get('value')!r}")
            if combo == "agree":
                got = a2 or by_area if (a2 is None or by_area is None or a2 == by_area) else None
                if c["area"] and by_area is None and a2 is not None and c["area"][0] == "own":
                    got = None  # the letter's own box says: a letter
            if got is None:
                tally[f"{kind}:kept"] += 1
            elif kind == "digit":
                want = c["truth"].get("value")
                tally["digit:changed"] += 1
                tally["digit:exact"] += bool(want) and got == want
                tally["digit:known"] += bool(want)
            else:
                tally[f"{kind}:CHANGED"] += 1
        print(f"{combo:12}", dict(sorted(tally.items())))
    for c in rows:
        line = read[f"{c['key']}|line"]["text"]
        print(f"  {c['key']:>16} {c['truth']['is']:6} anchor {anchored2(line, c['tokens'], c['f']['first'])!r:6} "
              f"truth {c['truth'].get('value', ''):4} | {line.strip()[:70]}")
    sys.exit()
    # the readings, for the best-looking variant
    m = 0.15
    for c in rows:
        if c["area"]:
            r = read[f"{c['key']}|{m}"]
            got = verdict(trimmed(r["chars"], c["area"][1]), c["area"][2], c["f"]["qari"])
            print(f"  {c['key']:>16} {c['truth']['is']:6} {c['area'][0]:5} Qari {c['area'][2]!r:10} "
                  f"Kraken {r['text'].strip()!r:18} -> {got!r:8} truth {c['truth'].get('value', '')}")
