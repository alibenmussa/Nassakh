"""A footnote region's note markers set from Kraken's reading of its lines (D103).

The models read the «(٣)» that starts a footnote as part of a sequence, not from the ink: when they drop one
(«في ج (الرياح) والمثبت» for «(٣) في ج …») they number the notes after it one lower («(3)» for (٤), «(4)»
for (٥)), and a marker they did write may land at the end of the line above when the words take their lines
(«… ٨/٨٨٥ . (3)», books 34–36). The linker then joins two notes, the page wants the wrong calls, and the call
pass refuses or renumbers a call it read right. Kraken reads every printed line of the region for the word
boxes (D92, `ocr.boxes`) and keeps the markers where they are printed, with an odd digit misread («(8)»
for ٥).

A line starts a note when Kraken read a bracketed number (or empty brackets) at its start, before its first
word; a marker only the models wrote starts one when the run fits it better (a «(2)،» that opens a carried
line is a reference, book 33 p. 6). The starts take one consecutive run of numbers: the run most readings
agree with (Kraken's digit +2, a disagreeing one −1, the models' +1), kept only when at least half of
Kraken's digits agree with it. A missing marker is put in (the models' marker at the end of the line above
moves down), a different one replaced, and a copy of it at the end of the line above dropped. A page of
lettered notes («(أ)», «(ب)») is left as it is. The edits are made on the built lines' tokens (the text's
alignment would move a marker put into the text back up). A marker set here is low and says it came from
Kraken's reading; the models' reading stays under `qari`.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass

_PERSIAN = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "٠١٢٣٤٥٦٧٨٩")
_WESTERN = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_ARABIC_INDIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
# Kraken's reading of a marker at the start of its line: «(٣)», «[3]», «()» (the digit not read).
KRAKEN_MARKER = re.compile(r"^\s*[\(\[]\s*([0-9٠-٩۰-۹]{0,3})\s*[\)\]]")
# What the models write for a marker, one token: «(٣)», «٣)», «(3)-», «٣» … or a bracketed letter they read
# for the digit («(ه)» for ٥, «(أ)» for ١).
MODEL_MARKER = re.compile(
    r"^[\(\[]?\s*([0-9٠-٩۰-۹]{1,3})\s*[\)\]]?[-–—ـ.:،]?$"
    r"|^[\(\[]\s*([ء-ي”“\"'’‘]?)\s*[\)\]]$"
    r"|^[\(\[]\s*([0-9٠-٩۰-۹]{1,3})[ء-ي]\s*[\)\]]$"  # a letter read into it: «(٢م)» for (٣), book 29 p. 104
)
TRAILING = re.compile(r"^[\(\[]\s*([0-9٠-٩۰-۹]{1,3})\s*[\)\]]$")
DIGIT_LOOKALIKES = (
    "أاإآه”“\"'’‘"  # letters the models read for a marker's digit; any other one is a lettered note
)
STRAY = "أاإآء.،:ـ-•*"  # a mark the models read before the marker («أ (١) ويعني», D87)
CONTINUED = "="  # Kraken's first word on a note carried from the page before (D87)
LINE_OVERLAP = 0.5  # a built line and a Kraken line are the same printed line: this share of the lower height
FIRST_WORD_OVERLAP = 0.5  # the marker's ink inside the first word's box by more: the word holds it
MIN_AGREEMENT = 0.5  # share of Kraken's digits the run must agree with
MAX_OPTIONAL = 6  # model-only starts tried in and out of the run (more: all in)
MAX_NUMBER = 200
INSERTED = -1000  # the `indices` entry of a marker token put in here: no place in the models' text


@dataclass
class _Start:
    line: int
    token: int | None  # the models' marker token in the line (None: they wrote none there)
    model: int | None  # its number (None: a letter or empty brackets)
    kraken: int | None  # Kraken's digit (None: none read, or brackets without one)
    seen: bool  # Kraken read a marker at the line's start
    bbox: list[int] | None  # Kraken's marker ink
    reading: str = ""  # Kraken's reading of it


def _digits(text: str) -> int | None:
    digits = text.translate(_PERSIAN).translate(_WESTERN)
    return int(digits) if digits.isdigit() and 0 < int(digits) <= MAX_NUMBER else None


def _text(token: dict) -> str:
    return str(token.get("t") or "").strip()


def _overlap_y(a: list, b: list) -> float:
    inter = min(float(a[3]), float(b[3])) - max(float(a[1]), float(b[1]))
    low = min(float(a[3]) - float(a[1]), float(b[3]) - float(b[1]))
    return max(0.0, inter) / low if low > 0 else 0.0


def kraken_line(box: list | None, box_lines: list[dict]) -> dict | None:
    """Kraken's line on the same printed line as `box`."""
    if not box:
        return None
    best, best_share = None, LINE_OVERLAP
    for line in box_lines:
        if not line.get("bbox"):
            continue
        share = _overlap_y(box, line["bbox"])
        if share >= best_share:
            best, best_share = line, share
    return best


def kraken_marker(words: list[dict]) -> tuple[str, list[int] | None] | None:
    """Kraken's marker at its line's start (its text, its box): the first words while they spell it."""
    texts = [str(w.get("text") or "") for w in words]
    match = KRAKEN_MARKER.match(" ".join(texts))
    if not match:
        return None
    want = len(re.sub(r"\s", "", match.group(0)))
    got, boxes = 0, []
    for word, text in zip(words, texts, strict=True):
        got += len(re.sub(r"\s", "", text))
        if word.get("bbox"):
            boxes.append(word["bbox"])
        if got >= want:
            break
    if got > want + 1:  # glued to the word after it: that box is the word's, not the marker's
        boxes = []
    box = (
        [
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        ]
        if boxes
        else None
    )
    return match.group(0).strip(), box


def model_marker(tokens: list[dict]) -> tuple[int | None, int | None, bool]:
    """`(token index, number, lettered)` of the models' marker at the line's start (a stray mark before it
    allowed); the number is None for a bracketed letter or empty brackets; `lettered` for a letter that is not
    one read for a digit («(ب)»)."""
    for k, token in enumerate(tokens[:2]):
        text = _text(token)
        if k == 0 and text in STRAY and len(tokens) > 1:
            continue
        match = MODEL_MARKER.match(text)
        if not match:
            return None, None, False
        if match.group(1) or match.group(3):
            return k, _digits(match.group(1) or match.group(3)), False
        letter = match.group(2) or ""
        return k, None, bool(letter) and letter not in DIGIT_LOOKALIKES
    return None, None, False


def _before_first_word(marker_box: list | None, tokens: list[dict], skip: int | None) -> bool:
    """The marker's ink lies before (right of) the line's first boxed word, not inside it."""
    first = next((t.get("bbox") for k, t in enumerate(tokens) if t.get("bbox") and k != skip), None)
    if marker_box is None or first is None:
        return True
    width = float(marker_box[2]) - float(marker_box[0])
    inside = min(float(marker_box[2]), float(first[2])) - max(float(marker_box[0]), float(first[0]))
    if width > 0 and max(0.0, inside) / width > FIRST_WORD_OVERLAP:
        return False
    return float(marker_box[0]) >= float(first[0])


def _starts(built: list[dict], box_lines: list[dict]) -> list[_Start] | None:
    """The lines that may start a note, in order (None: a page of lettered notes)."""
    out: list[_Start] = []
    lettered = 0
    for k, line in enumerate(built):
        tokens = line.get("tokens") or []
        if not tokens:
            continue
        token, model, letter = model_marker(tokens)
        lettered += letter
        kl = kraken_line(line.get("bbox"), box_lines)
        words = list(kl.get("words") or []) if kl else []
        found = kraken_marker(words)
        seen, digit, box, reading = False, None, None, ""
        if found and _before_first_word(found[1], tokens, token):
            seen, box, reading = True, found[1], found[0]
            digit = _digits(KRAKEN_MARKER.match(found[0]).group(1))
        if token is None and not seen:
            continue
        if not seen and words and str(words[0].get("text") or "").strip().startswith(CONTINUED):
            continue  # «= (١/٥٤ …)»: a note carried from the page before, whatever the models wrote
        out.append(_Start(k, token, model, digit, seen, box, reading))
    return None if lettered >= 2 else out


def _score(run: list[_Start], base: int) -> int:
    score = 0
    for j, start in enumerate(run):
        if start.kraken is not None:
            score += 2 if start.kraken == base + j else -1
        if start.model is not None and start.model == base + j:
            score += 1
    return score


def _best_run(starts: list[_Start]) -> tuple[list[_Start], int] | None:
    """The starts that make the run and its first number (None: Kraken's digits do not agree with any)."""
    optional = [k for k, s in enumerate(starts) if not s.seen]
    choices = (
        itertools.product((False, True), repeat=len(optional))
        if len(optional) <= MAX_OPTIONAL
        else [(True,) * len(optional)]
    )
    best: tuple[int, int, list[_Start], int] | None = None  # (score, -included, run, base)
    for choice in choices:
        keep = {k for k, on in zip(optional, choice, strict=True) if on}
        run = [s for k, s in enumerate(starts) if s.seen or k in keep]
        bases = {
            v - j for j, s in enumerate(run) for v in (s.kraken, s.model) if v is not None and v - j >= 1
        }
        for base in bases:
            key = (_score(run, base), -len(keep), run, base)
            if best is None or key[:2] > best[:2] or (key[:2] == best[:2] and base < best[3]):
                best = key
    if best is None:
        return None
    _score_, _keep, run, base = best
    read = [s for s in run if s.kraken is not None]
    agree = sum(1 for j, s in enumerate(run) if s.kraken is not None and s.kraken == base + j)
    if read and agree < MIN_AGREEMENT * len(read):
        return None
    if not read and not any(s.model is not None for s in run):
        return None
    return run, base


def _marker_token(text: str, bbox: list[int] | None, qari: str) -> dict:
    return {
        "t": text,
        "alt": None,  # the vote never puts the models' marker back (`ocr.chooser`)
        "tess": None,
        "conf": "low",
        "digit": True,
        "bbox": [int(v) for v in bbox] if bbox else None,
        "src": "kraken",
        "marker": True,
        "why": [],
        "qari": {"t": qari, "alt": None, "tess": None},
    }


def _refresh(line: dict) -> None:
    tokens = line.get("tokens") or []
    line["text"] = " ".join(_text(t) for t in tokens if _text(t))
    line["n_low"] = sum(1 for token in tokens if token.get("conf") == "low")
    line["confidence"] = round(1.0 - line["n_low"] / len(tokens), 3) if tokens else 1.0
    boxes = [t["bbox"] for t in tokens if t.get("bbox")]
    if boxes and line.get("bbox"):
        x0, y0, x1, y1 = line["bbox"]
        line["bbox"] = [min(x0, *(b[0] for b in boxes)), y0, max(x1, *(b[2] for b in boxes)), y1]


def fix_markers(built: list[dict], box_lines: list[dict] | None) -> int:
    """Give a footnote region's lines their markers (`built`: the region's lines from `build_lines`, in order,
    changed in place; `box_lines`: Kraken's lines of the region). Returns the number of markers set."""
    if not box_lines or not built:
        return 0
    starts = _starts(built, box_lines)
    if not starts or not any(s.seen for s in starts):
        return 0
    found = _best_run(starts)
    if found is None:
        return 0
    run, base = found
    # the digits the book prints, as Kraken read them on the markers (the models write Western ones too)
    read = "".join(s.reading for s in run)
    if re.search(r"[٠-٩۰-۹]", read):
        western = False
    elif re.search(r"[0-9]", read):
        western = True
    else:
        western = any(
            re.search(r"[0-9]", _text(built[s.line]["tokens"][s.token])) for s in run if s.token is not None
        )
    changed = 0
    for j, start in enumerate(run):
        number = base + j
        text = "(" + (str(number) if western else str(number).translate(_ARABIC_INDIC)) + ")"
        line = built[start.line]
        tokens, indices = line["tokens"], line.setdefault("indices", [])
        above = built[start.line - 1] if start.line > 0 else None
        last = _text(above["tokens"][-1]) if above and len(above.get("tokens") or []) > 1 else ""
        copy = TRAILING.match(last)
        moved, moved_index = None, INSERTED
        if copy:
            value = _digits(copy.group(1))
            previous = run[j - 1].model if j > 0 and run[j - 1].line == start.line - 1 else None
            if value is not None and value in {number, start.model, (previous + 1) if previous else None}:
                # the marker the models wrote, left at the end of the line above (or a copy of it there)
                moved = above["tokens"].pop()
                if above.get("indices"):
                    moved_index = above["indices"].pop()
                _refresh(above)
                changed += 1 if start.token is not None else 0
        if start.token is None:
            qari = _text(moved) if moved else ""
            tokens.insert(0, _marker_token(text, start.bbox or (moved or {}).get("bbox"), qari))
            indices.insert(0, moved_index)
            changed += 1
        elif _text(tokens[start.token]) != text:
            old = tokens[start.token]
            tokens[start.token] = _marker_token(text, start.bbox or old.get("bbox"), _text(old))
            changed += 1
        else:
            tokens[start.token]["marker"] = True  # checked against the run: the numbers pass leaves it
            continue
        _refresh(line)
    return changed
