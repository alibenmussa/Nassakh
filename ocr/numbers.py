"""The numbers pass (D50): Kraken reads the Arabic-Indic numbers of a page after Qari.

Every reader we measured misreads Arabic-Indic digits; Kraken with the OpenITI printed Arabic-script
model read 92 % of 116 real numbers exactly (Qari 43 %, playground/digits/REPORT.md). So in books that
print Arabic-Indic digits, a number's reading comes from Kraken and it is the only reading offered in
review: the reviewer confirms it or types the true number. Books printed with Western digits keep
Qari's (it reads those at 90 %, Kraken at 71 %).

The pass, for one finalised page (`read_page_numbers`):
1. `printed_style(book)`: does the book print Arabic-Indic digits? Decided from what Qari already
   wrote for the book's numbers (Qari v0.2 writes Arabic-Indic digits for 90–100 % of them in such
   books, 0–12 % in Western ones; v0.3 33–82 % against 0 %).
2. `number_areas`: where each number token sits: its word box (in its line's rows, D92:
   `in_line_rows`), else the gap between its nearest neighbours that have one (right to left: the word
   before it is on its right). Numbers without a box that share a gap share one area. A box the
   alignment marked weak (`bq: "weak"`) counts as none, here and in steps 5-6 (`word_box`).
3. Kraken reads each area as one line (`ocr.engines.kraken`, its own process); `assign` gives its
   digit runs to the area's numbers in order when the counts agree (closing up stray spaces inside a
   number if that makes them agree). A token read in its own word box whose counts do not agree takes
   Kraken's reading of the whole box (`box_text`: Qari then held a fragment of the box, e.g. «١،» for
   «(ص٢٣-٢٤).»); a gap, which may hold other words, keeps Qari's reading then. A number whose weak box
   was left out is read in that box too (`page_weak_boxes`) and takes its one number when its gap gave
   it none (a weak box is mostly wide: «٢٢» for «٢٠١٢»; no whole-box reading, it may be a
   neighbour's).
4. `apply_reading`: the token's digits become Kraken's (in Arabic-Indic digits; what surrounds them,
   brackets, «هـ», «م», stays Qari's), Qari's readings are kept under `qari` for the record and dropped
   from the offered readings (`alt`, `tess` → None), `src` = "kraken"; the token stays low-confidence
   (D17) until the reviewer confirms it. Reviewed lines and resolved tokens are never touched.

Numbers Qari wrote as letters (D51): Qari writes some Arabic-Indic digits as the letter they look
like (١ → «ا», ٥ → «ه», ٤ → «ع»: «(ج ا، ص…)», «(ه) الخزر», a footnote mark «ا») or drops a date's
digits altogether («(٨٤٧–٨٦١م)» → «(هـ – م)»), so the word holds no digit and step 2 never sees it.
5. `is_letter_digit` / `digitless_dates` find them; Kraken reads their whole line, and each one's own
   area when it has one to itself (a box, or a gap shared with punctuation only; a letter's with less
   of its neighbours).
6. A letter takes the number Kraken read between Qari's neighbours in the line (`anchored_number`,
   the neighbours' letters around it; no box needed), else the one number in its own area when the
   rest of the area agrees with Qari (`box_number`); never when Kraken read Qari's letter itself in
   its own area (`shows_letter`: a real letter, «(هـ)» of a lettered list, the hijri «هـ»). Qari's
   letter stays offered as the second reading (`apply_letter`). A digit-less date becomes the one
   bracketed date Kraken read in its own area, else between the same neighbours in the line
   (`date_reading`); one token, Qari's reading offered second (`replace_date`).
On 77 labelled letters of six books (playground/digits/REPORT.md, round 3): 45 of 56 numbers read,
40 of 41 of known value exact, none of 13 real letters changed; the three digit-less dates exact.

A line's open suggestions (`ocr.TextGap`, D72) follow its words (`token_moves`): a date's tokens become
one, and a rewritten word is what a suggestion after it now comes after.

Pure functions up to `token_moves`; the service (`read_page_numbers`) does the I/O.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .alignment import WEAK

log = logging.getLogger(__name__)

DIGIT_CHARS = "0-9٠-٩۰-۹"
_RUN = re.compile(f"[{DIGIT_CHARS}]+")
_DIGIT = re.compile(f"[{DIGIT_CHARS}]")
_EASTERN = re.compile("[٠-٩]")
_WESTERN = re.compile("[0-9]")
# Kraken's digits as the page's (Arabic-Indic): by value, but Kraken's Persian six «۶» is the page's
# «٤» (the two share a shape; it wrote «٧٥۶» for «٧٥٤» and «ص۲۶٠» for «ص٢٤٠», playground/digits round 3)
KRAKEN_DIGITS = str.maketrans("0123456789۰۱۲۳۴۵۶۷۸۹", "٠١٢٣٤٥٦٧٨٩٠١٢٣٤٥٤٧٨٩")

ARABIC_INDIC = "arabic_indic"
WESTERN = "western"
STYLE_MIN_DIGITS = 8  # fewer digits seen than this: the book's style is not known yet
STYLE_SECONDARY_SHARE = 0.5  # the secondary model (v0.2) writes this share of them Arabic-Indic, or ...
STYLE_PRIMARY_SHARE = 0.2  # ... the primary (v0.3) this share


# ====================================================================== 1. the book's printed digits


def digit_counts(tokens) -> tuple[int, int, int, int]:
    """`(primary Arabic-Indic, primary Western, secondary Arabic-Indic, secondary Western)` digits
    written for the number tokens among `tokens`."""
    pe = pw = se = sw = 0
    for token in tokens:
        primary = str(token.get("t") or "")
        if not (token.get("digit") or _DIGIT.search(primary)):
            continue
        pe += len(_EASTERN.findall(primary))
        pw += len(_WESTERN.findall(primary))
        secondary = str(token.get("alt") or "")
        se += len(_EASTERN.findall(secondary))
        sw += len(_WESTERN.findall(secondary))
    return pe, pw, se, sw


def style_of(counts: tuple[int, int, int, int]) -> str:
    """`arabic_indic`, `western`, or '' when too few digits were seen to tell."""
    pe, pw, se, sw = counts
    if pe + pw < STYLE_MIN_DIGITS:
        return ""
    if se + sw and se / (se + sw) >= STYLE_SECONDARY_SHARE:
        return ARABIC_INDIC
    if pe / (pe + pw) >= STYLE_PRIMARY_SHARE:
        return ARABIC_INDIC
    return WESTERN


# ====================================================================== 2. where the numbers are


@dataclass
class Area:
    """A stretch of one line to read with Kraken: `[x0, y0, x1, y1]` in page pixels, and the indexes of
    the number tokens it holds (in reading order)."""

    bbox: list[int]
    tokens: list[int] = field(default_factory=list)


def is_number(token: dict) -> bool:
    return bool(token.get("digit")) or bool(_DIGIT.search(str(token.get("t") or "")))


def word_box(token: dict) -> list | None:
    """A token's word box, or None without one or when the alignment was unsure of it (`bq: "weak"`,
    `ocr.alignment.weak_boxes`): such a box is no area to read and no edge of a gap."""
    box = token.get("bbox")
    return box if box and token.get("bq") != WEAK else None


def in_line_rows(box: list, line_bbox: list[int]) -> list[int]:
    """A word box's columns in its line's rows at least. Kraken's word boxes (D92) are trimmed to the word's
    own ink, and Kraken misreads digits cut that close (book 29 p. 15: «٢٧» read «٢٤», p. 1: «١٩٧٠» read
    «١٦٧٠»; in the line's rows, as Tesseract's boxes fitted to the printed line were, both right)."""
    x0, y0, x1, y1 = (int(v) for v in box)
    return [x0, min(y0, int(line_bbox[1])), x1, max(y1, int(line_bbox[3]))]


def number_areas(tokens: list[dict], line_bbox: list[int]) -> list[Area]:
    """The areas of a line's number tokens (reading order, right to left on the page).

    A token with a word box is read in that box's columns and its line's rows (`in_line_rows`; the runner
    adds a margin of 30 % of the height: the set-up that read 92 % of the labelled numbers, on Tesseract's
    boxes fitted to the printed line). One without is read in the gap between its nearest
    neighbours that have a box (the word before it is on its right: the gap's right edge is that
    word's left edge), or the line's edge, at the line's height; tokens of one gap share it. A weak
    box counts as none (`word_box`).
    """
    lx0, ly0, lx1, ly1 = (int(v) for v in line_bbox)
    areas: list[Area] = []
    gaps: dict[tuple[int, int], Area] = {}
    for i, token in enumerate(tokens):
        if not is_number(token):
            continue
        box = word_box(token)
        if box:
            areas.append(Area(in_line_rows(box, line_bbox), [i]))
            continue
        right = next((int(word_box(tokens[j])[0]) for j in range(i - 1, -1, -1) if word_box(tokens[j])), lx1)
        left = next(
            (int(word_box(tokens[j])[2]) for j in range(i + 1, len(tokens)) if word_box(tokens[j])), lx0
        )
        if right - left < 3:
            continue
        key = (left, right)
        if key not in gaps:
            gaps[key] = Area([left, ly0, right, ly1])
            areas.append(gaps[key])
        gaps[key].tokens.append(i)
    return areas


# ====================================================================== 3. Kraken's digits for them


def digit_runs(chars: list) -> list[list]:
    """Kraken's digit runs of an area in reading order: lists of its `[char, x0, x1, conf]` rows; a
    space (or any other character) ends a run."""
    runs: list[list] = []
    current: list = []
    for row in chars:
        if _DIGIT.fullmatch(str(row[0])):
            current.append(row)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _joined(chars: list) -> list[list]:
    """Digit runs with the stray spaces inside a number closed up («٢٠ ٠ ٠» → «٢٠٠٠»)."""
    runs: list[list] = []
    current: list = []
    for row in chars:
        char = str(row[0])
        if _DIGIT.fullmatch(char):
            current.append(row)
        elif char.isspace() and current:
            continue  # a space inside a number: the number goes on
        else:
            if current:
                runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _by_position(runs: list[list]) -> list[list]:
    """The runs right to left on the page (reading order), when Kraken placed every character."""
    if any(row[1] is None or row[2] is None for run in runs for row in run):
        return runs
    return sorted(runs, key=lambda run: -sum(row[1] + row[2] for row in run) / len(run))


def _likeness(qari: list[str], numbers: list[str]) -> int:
    """The digits Qari's numbers share with `numbers`, place by place from the units (Qari drops
    leading digits more than the last ones)."""
    score = 0
    for ours, theirs in zip(qari, numbers, strict=False):
        ours, theirs = ours.translate(KRAKEN_DIGITS), theirs.translate(KRAKEN_DIGITS)
        score += sum(a == b for a, b in zip(reversed(ours), reversed(theirs), strict=False))
    return score


def _ordered(runs: list[list], qari: list[str]) -> list[str]:
    """Kraken's numbers of an area in reading order. Kraken gives them in its logical order, which is
    wrong when it dropped the spaces between numbers set apart by a comma or a slash («٥٢ ، ٥٣» read
    «٥٣،٥٢»: BiDi then keeps them left to right); on the page they are right to left. Neither is
    always right (a date «٨/٣٨» set without spaces is left to right on the page too), so the page's
    order is taken when it agrees better with Qari's digits."""
    numbers = ["".join(str(row[0]) for row in run) for run in runs]
    placed = ["".join(str(row[0]) for row in run) for run in _by_position(runs)]
    if placed != numbers and _likeness(qari, placed) > _likeness(qari, numbers):
        return placed
    return numbers


def assign(area_tokens: list[dict], chars: list) -> list[list[str]] | None:
    """Kraken's numbers for each token of an area, in order (each token gets as many numbers as it has
    digit runs in Qari's reading), or None when the counts cannot be matched."""
    wanted = [len(_RUN.findall(str(token.get("t") or ""))) or 1 for token in area_tokens]
    qari = [n for token in area_tokens for n in (_RUN.findall(str(token.get("t") or "")) or [""])]
    for runs in (digit_runs(chars), _joined(chars)):
        if len(runs) != sum(wanted):
            continue
        numbers = _ordered(runs, qari)
        out, k = [], 0
        for n in wanted:
            out.append(numbers[k : k + n])
            k += n
        return out
    return None


def box_text(chars: list) -> str:
    """Kraken's reading of a whole word box (spaces collapsed), '' when it holds no digit."""
    text = " ".join("".join(str(row[0]) for row in chars).split())
    return text if _DIGIT.search(text) else ""


# ====================================================================== 4. the token


def apply_reading(token: dict, numbers: list[str] | None = None, whole: str = "") -> bool:
    """Put Kraken's `numbers` into `token` (or its reading of the token's whole box, `whole`; see the
    module docstring); False when nothing changed or the token must not change (resolved by the
    reviewer, or already read)."""
    if token.get("res") or token.get("src") == "kraken" or not (numbers or whole):
        return False
    text = str(token.get("t") or "")
    runs = list(_RUN.finditer(text))
    arabic = [number.translate(KRAKEN_DIGITS) for number in numbers or []]
    if whole:
        new = whole.translate(KRAKEN_DIGITS)
    elif runs and len(runs) == len(arabic):
        parts, last = [], 0
        for match, number in zip(runs, arabic, strict=True):
            parts.append(text[last : match.start()])
            parts.append(number)
            last = match.end()
        parts.append(text[last:])
        new = "".join(parts)
    elif not runs and len(arabic) == 1:
        new = arabic[0]  # a number Qari wrote as something else in a box of its own
    else:
        return False
    token["qari"] = {"t": token.get("t"), "alt": token.get("alt"), "tess": token.get("tess")}
    token["t"] = new
    token["alt"] = None
    token["tess"] = None
    token["src"] = "kraken"
    token["digit"] = True
    token["conf"] = "low"  # D17: a number is confirmed by the reviewer
    return True


# ============================================================ 5.–6. numbers Qari wrote as letters (D51)

# a letter that looks like a digit, alone, with brackets or punctuation: «ا» (١), «ه»/«هـ» (٥), «ع» (٤)
LETTER_TOKEN = re.compile(r"^([(\[«“\"]?)([اهع]ـ?)([)\]»”\"]?[.،:؛]?)$")
LETTER_MARGIN = 0.15  # a letter's own area is read with this margin left and right (of its height)
ANCHOR_LETTERS = 2  # the neighbours' letters that must surround the number in Kraken's line
DATE_MAX_TOKENS = 8
DATE_MAX_CHARS = 24
DATE_MARK_LETTERS = 2  # a digit-less date holds only marks this short («ع», «ه», «هـ», «م»), no words
# alef forms → ا; «ى» and Kraken's Persian «ی» → ي; its Persian «ک» → ك
_ALEFS = str.maketrans(
    "\u0623\u0625\u0622\u0671\u0649\u06cc\u06a9", "\u0627\u0627\u0627\u0627\u064a\u064a\u0643"
)
_NOT_SKELETON = re.compile(r"[^ء-ي٠-٩A-Za-z]|ـ")
_MARKS = re.compile(r"[\u064B-\u0655\u0670ـ\s]")
_WORDISH = re.compile(f"[ء-يA-Za-z{DIGIT_CHARS}]")
_DASH = re.compile(r"[–—\-/]")
_DATE_END = re.compile(r"(?:م|هـ|ه)\s*\)[.،:؛]?$")
_BRACKETED = re.compile(r"\([^()]*\)")
_BRACKET = re.compile(r"[()]")
_DATE_SHAPE = re.compile(r"^\(([٠-٩]+(?:هـ|ه|م)?)([–—\-/])([٠-٩]+(?:هـ|ه|م)?)\)$")


def skeleton(text) -> str:
    """Letters and digits only, to compare two readings: digits as the page's, alef forms, «ى» and
    Kraken's Persian letters folded; no punctuation, spaces, marks or tatweel."""
    return _NOT_SKELETON.sub("", str(text or "").translate(KRAKEN_DIGITS).translate(_ALEFS))


def _plain(text) -> str:
    """A reading without spaces, marks or tatweel, digits as the page's."""
    return _MARKS.sub("", str(text or "")).translate(KRAKEN_DIGITS)


def is_letter_digit(token: dict) -> bool:
    """A lone letter Qari may have written for a digit (not a number, not resolved, not read yet)."""
    if token.get("res") or token.get("src") == "kraken" or is_number(token):
        return False
    return bool(LETTER_TOKEN.match(str(token.get("t") or "")))


def is_era_sign(tokens: list[dict], i: int) -> bool:
    """«ه» / «هـ» right after a word holding digits is the hijri era sign of that year («سنة ٢٩ هـ»), never a
    digit Qari wrote as a letter. Kraken reads that sign as «٥» or «٨» in its own area, so `shows_letter` does
    not catch it; taken for a digit, the year gained a digit and the sign became one (book 29, 2026-09-28:
    «٢٩ هـ» → «٢٩٩ ٨», «٤٥ هـ» → «٥ ٤٥»). A letter after anything else («(ه) الخزر», «سنة ه») stays a
    candidate."""
    match = LETTER_TOKEN.match(str(tokens[i].get("t") or ""))
    if not match or not match.group(2).startswith("ه") or i == 0:
        return False
    return bool(_DIGIT.search(str(tokens[i - 1].get("t") or "")))


def digitless_dates(tokens: list[dict]) -> list[tuple[int, int]]:
    """`(first, last)` of each bracketed date Qari wrote without its digits, «(هـ – م)», «(ع ه – ه م)»:
    a bracket group of at most `DATE_MAX_TOKENS` tokens with a dash or a slash, ending in «م» or «هـ»,
    holding no digit and no word, only marks of at most `DATE_MARK_LETTERS` letters (never real text:
    «(كتابه – شرحه)» is not one); one «(» only; none of its tokens resolved."""
    out: list[tuple[int, int]] = []
    i = 0
    while i < len(tokens):
        if str(tokens[i].get("t") or "").startswith("("):
            for j in range(i, min(len(tokens), i + DATE_MAX_TOKENS)):
                closing = str(tokens[j].get("t") or "")
                if ")" not in (closing[1:] if j == i else closing):
                    continue
                group = tokens[i : j + 1]
                text = " ".join(str(t.get("t") or "") for t in group)
                if (
                    len(text) <= DATE_MAX_CHARS
                    and "(" not in text[1:]
                    and not _DIGIT.search(text)
                    and _DASH.search(text)
                    and _DATE_END.search(text)
                    and all(len(skeleton(piece)) <= DATE_MARK_LETTERS for piece in text.split())
                    and not any(t.get("res") or t.get("src") == "kraken" for t in group)
                ):
                    out.append((i, j))
                    i = j  # past the date; a group refused is searched again from its next token
                break
        i += 1
    return out


def letter_area(tokens: list[dict], i: int, line_bbox: list[int]) -> tuple[list[int], str, bool] | None:
    """Letter token `i`'s own area: `(bbox, Qari's reading of the area without the letter, is its
    box)`. Its word box (in its line's rows, `in_line_rows`); else the gap between its boxed neighbours
    when nothing but punctuation shares it (a gap holding words or numbers too says nothing of where the
    letter is: None). Weak boxes count as none (`word_box`)."""
    token = tokens[i]
    rest = LETTER_TOKEN.sub(r"\1\3", str(token.get("t") or ""))
    if word_box(token):
        return in_line_rows(word_box(token), line_bbox), rest, True
    lx0, ly0, lx1, ly1 = (int(v) for v in line_bbox)
    right = next((j for j in range(i - 1, -1, -1) if word_box(tokens[j])), None)
    left = next((j for j in range(i + 1, len(tokens)) if word_box(tokens[j])), None)
    inside = range(right + 1 if right is not None else 0, left if left is not None else len(tokens))
    if any(_WORDISH.search(str(tokens[j].get("t") or "")) for j in inside if j != i):
        return None
    x1 = int(word_box(tokens[right])[0]) if right is not None else lx1
    x0 = int(word_box(tokens[left])[2]) if left is not None else lx0
    if x1 - x0 < 3:
        return None
    text = "".join(rest if j == i else str(tokens[j].get("t") or "") for j in inside)
    return [x0, ly0, x1, ly1], text, False


def _neighbour(tokens: list[dict], start: int, step: int) -> str:
    """The skeleton of the nearest token from `start` on (by `step`) that has letters or digits."""
    j = start
    while 0 <= j < len(tokens):
        found = skeleton(tokens[j].get("t"))
        if found:
            return found
        j += step
    return ""


def anchored_number(line_text: str, tokens: list[dict], i: int, n: int = ANCHOR_LETTERS) -> str:
    """Kraken's number for letter token `i` out of its reading of the whole line: the digits found
    exactly once between the neighbours' letters (the last `n` of the word before, the first `n` of the
    word after, as Qari read them; the line's start or end when there is none); '' when not found."""
    before, after = _neighbour(tokens, i - 1, -1), _neighbour(tokens, i + 1, 1)
    # lookarounds: the neighbours' letters are not used up, so every place that fits is counted
    left = f"(?<={re.escape(before[-n:])})" if before else "^"
    right = f"(?={re.escape(after[:n])})" if after else "$"
    found = re.findall(left + "([٠-٩]+)" + right, skeleton(line_text))
    return found[0] if len(found) == 1 else ""


def box_number(chars: list, rest: str) -> str:
    """Kraken's number for a letter out of its reading of the letter's own area: the one digit run
    there, when the rest of Kraken's reading is what Qari read in the area besides the letter; ''."""
    text = _plain("".join(str(row[0]) for row in chars))
    runs = _RUN.findall(text)
    if len(runs) != 1 or _DIGIT.sub("", text) != _plain(rest):
        return ""
    return runs[0]


def shows_letter(chars: list, token_text: str) -> bool:
    """Kraken read Qari's letter itself in the letter's own box: a real letter."""
    read = skeleton("".join(str(row[0]) for row in chars))
    return bool(read) and read == skeleton(token_text)


def apply_letter(token: dict, number: str) -> bool:
    """The letter becomes Kraken's `number` (Qari's brackets and punctuation stay); Qari's reading stays
    offered as the second one, `alt` (it may be a real letter); the token is Kraken's and low (D17).
    A lone zero where Qari saw «ه» is «٥» (both are a circle; Kraken wrote «0» for a bold «٥»)."""
    match = LETTER_TOKEN.match(str(token.get("t") or ""))
    if not match or not number or not is_letter_digit(token):
        return False
    number = number.translate(KRAKEN_DIGITS)
    if number == "٠" and match.group(2).startswith("ه"):
        number = "٥"
    token["qari"] = {"t": token.get("t"), "alt": token.get("alt"), "tess": token.get("tess")}
    token["t"] = match.group(1) + number + match.group(3)
    token["alt"] = match.group(0)
    token["tess"] = None
    token["src"] = "kraken"
    token["digit"] = True
    token["conf"] = "low"
    return True


def date_area(tokens: list[dict], first: int, last: int, line_bbox: list[int]) -> list[int] | None:
    """Where the date `tokens[first:last + 1]` is: its tokens' boxes (in the line's rows, `in_line_rows`),
    else the gap between its nearest boxed neighbours (the word before it is on its right), at the line's
    height, when no number and no bracket shares that gap (one could be another date, read in the date's
    place: None). Words may: Kraken reads no bracketed date out of them. Weak boxes count as none
    (`word_box`)."""
    lx0, ly0, lx1, ly1 = (int(v) for v in line_bbox)
    boxes = [word_box(t) for t in tokens[first : last + 1] if word_box(t)]
    if len(boxes) == last - first + 1:
        return in_line_rows(
            [
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            ],
            line_bbox,
        )
    boxed = [bool(word_box(t)) for t in tokens]
    right = first if boxed[first] else next((j for j in range(first - 1, -1, -1) if boxed[j]), None)
    left = last if boxed[last] else next((j for j in range(last + 1, len(tokens)) if boxed[j]), None)
    start = 0 if right is None else (right if right == first else right + 1)
    end = len(tokens) if left is None else (left + 1 if left == last else left)
    others = (tokens[j] for j in range(start, end) if not first <= j <= last)
    if any(is_number(t) or _BRACKET.search(str(t.get("t") or "")) for t in others):
        return None
    x1 = lx1 if right is None else int(word_box(tokens[right])[2 if right == first else 0])
    x0 = lx0 if left is None else int(word_box(tokens[left])[0 if left == last else 2])
    return [x0, ly0, x1, ly1] if x1 - x0 >= 3 else None


def _dated(shape, tokens: list[dict], first: int, last: int) -> str:
    """Kraken's date (`_DATE_SHAPE` match) written with Qari's dash; the hijri mark as printed, «هـ»
    (the readings were compared without tatweel)."""
    dash = _DASH.search(" ".join(str(t.get("t") or "") for t in tokens[first : last + 1]))
    start, sep, end = (re.sub("ه$", "هـ", part) for part in shape.groups())
    return f"({start}{dash.group(0) if dash else sep}{end})"


def date_reading(
    line_text: str, tokens: list[dict], first: int, last: int, area_text: str = "", n: int = ANCHOR_LETTERS
) -> str:
    """Kraken's reading of the digit-less date `tokens[first:last + 1]`: the one bracketed date
    («(digits[م|هـ]–digits[م|هـ])») in its reading of the date's own area (`area_text`), else the one
    in its reading of the whole line with Qari's neighbours on either side (the line's start or end
    when there is none); written with Qari's dash; '' when not found."""
    if area_text:
        found = [
            shape for m in _BRACKETED.finditer(_plain(area_text)) if (shape := _DATE_SHAPE.match(m.group(0)))
        ]
        if len(found) == 1:
            return _dated(found[0], tokens, first, last)
    before, after = _neighbour(tokens, first - 1, -1), _neighbour(tokens, last + 1, 1)
    plain = _plain(line_text)
    found = []
    for match in _BRACKETED.finditer(plain):
        shape = _DATE_SHAPE.match(match.group(0))
        if not shape:
            continue
        head, tail = skeleton(plain[: match.start()]), skeleton(plain[match.end() :])
        if (head.endswith(before[-n:]) if before else not head) and (
            tail.startswith(after[:n]) if after else not tail
        ):
            found.append(shape)
    return _dated(found[0], tokens, first, last) if len(found) == 1 else ""


def replace_date(tokens: list[dict], first: int, last: int, reading: str) -> dict:
    """The date's tokens become one token holding Kraken's `reading` (with what Qari read before its
    «(» and after its «)»); Qari's reading of the date is kept under `qari` and stays offered as the
    second reading, as a letter's does; low (D17); its box is the union of its tokens' boxes when they
    all have one (weak when one of them was). Returns it."""
    group = tokens[first : last + 1]
    text = " ".join(str(t.get("t") or "") for t in group)
    head, tail = text[: text.index("(")], text[text.rindex(")") + 1 :]
    boxes = [t["bbox"] for t in group if t.get("bbox")]
    bbox = None
    if len(boxes) == len(group):
        bbox = [
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        ]
    token = {
        "t": head + reading + tail,
        "alt": text,
        "tess": None,
        "conf": "low",
        "digit": True,
        "bbox": bbox,
        "res": None,
        "src": "kraken",
        "qari": {"t": text, "alt": None, "tess": None},
    }
    if bbox and any(t.get("bq") == WEAK for t in group):
        token["bq"] = WEAK
    tokens[first : last + 1] = [token]
    return token


def token_moves(before: list[int], tokens: list[dict], dates=()) -> dict[int, int]:
    """Old → new indices of a line's tokens after the pass (`before`: the `id()` of each token as read,
    `dates`: the line's digit-less dates, `(first, last)` as read).

    Tokens change in place, and `replace_date` puts one new token where a date's tokens were, right
    after the last token kept before them: those tokens all go to it, so a suggestion after the date
    stays after it (`review.services._shift_gaps` takes the mapping)."""
    now = {id(token): j for j, token in enumerate(tokens)}
    first_of = {i: first for first, last in dates for i in range(first, last + 1)}
    moves: dict[int, int] = {}
    at = -1  # the new index of the last token placed
    for i, key in enumerate(before):
        if key in now:
            at = now[key]
        elif i not in first_of:
            continue  # not the pass's doing: a token gone
        elif first_of[i] == i:
            at += 1  # a joined date: its one token
        moves[i] = at
    return moves


# ====================================================================== the pass (I/O)

STYLE_SAMPLE_DIGITS = 200  # the book's style is decided from its first numbers, this many digits
PAGE_STYLE_MIN_DIGITS = 3  # a page alone decides when the book has not shown enough numbers yet


def _as_read_by_qari(token: dict) -> dict:
    """A token as Qari read it (a number Kraken read keeps Qari's readings under `qari`)."""
    return {**token, **(token.get("qari") or {})} if token.get("src") == "kraken" else token


def _add(total: list[int], counts: tuple[int, int, int, int]) -> None:
    for k, value in enumerate(counts):
        total[k] += value


def book_style(book) -> str:
    """The digits a book prints, from Qari's readings of its first numbers (`style_of`)."""
    from ocr.models import Line

    total = [0, 0, 0, 0]
    rows = (
        Line.objects.filter(page__book=book)
        .order_by("page__number", "order")
        .values_list("tokens", flat=True)
    )
    for tokens in rows.iterator(chunk_size=500):
        _add(total, digit_counts([_as_read_by_qari(t) for t in tokens or []]))
        if total[0] + total[1] >= STYLE_SAMPLE_DIGITS:
            break
    return style_of((total[0], total[1], total[2], total[3]))


def page_style(page) -> str:
    """The style of one page's own numbers (for a book that has not shown enough of them yet)."""
    total = [0, 0, 0, 0]
    for tokens in page.lines.values_list("tokens", flat=True):
        _add(total, digit_counts([_as_read_by_qari(t) for t in tokens or []]))
    if total[0] + total[1] < PAGE_STYLE_MIN_DIGITS:
        return ""
    se, sw = total[2], total[3]
    if se + sw and se / (se + sw) >= STYLE_SECONDARY_SHARE:
        return ARABIC_INDIC
    return ARABIC_INDIC if total[0] / (total[0] + total[1]) >= STYLE_PRIMARY_SHARE else WESTERN


@dataclass
class PageNumbers:
    """What the pass did on a page: Kraken's areas, the numbers it gave (`applied`, letters and dates
    among them)."""

    style: str = ""
    areas: int = 0
    applied: int = 0
    letters: int = 0
    dates: int = 0
    printed: str = ""  # the printed page number Kraken read in the page-number region ('' when none)
    seconds: float = 0.0
    skipped: str = ""

    def as_dict(self) -> dict:
        keys = ("style", "areas", "applied", "letters", "dates", "printed", "seconds", "skipped")
        return {k: getattr(self, k) for k in keys}


def _unreviewed_lines(page) -> list:
    return [line for line in page.lines.filter(is_reviewed=False).order_by("order") if line.bbox]


def page_areas(page, lines: list | None = None) -> tuple[list[dict], list[tuple]]:
    """The number areas to read on a page's unreviewed lines: runner requests and `(line, area)` pairs."""
    requests: list[dict] = []
    index: list[tuple] = []
    for line in _unreviewed_lines(page) if lines is None else lines:
        tokens = line.tokens or []
        for k, area in enumerate(number_areas(tokens, line.bbox)):
            if all(tokens[i].get("res") or tokens[i].get("src") == "kraken" for i in area.tokens):
                continue  # every number of the area was already read or resolved
            requests.append({"id": f"{line.pk}:{k}", "bbox": area.bbox})
            index.append((line, area))
    return requests, index


def page_weak_boxes(page, lines: list | None = None) -> tuple[list[dict], list[tuple]]:
    """The number tokens of a page's unreviewed lines with a weak box (step 3): runner requests for
    those boxes and `(line, token index)` pairs."""
    requests: list[dict] = []
    index: list[tuple] = []
    for line in _unreviewed_lines(page) if lines is None else lines:
        for i, token in enumerate(line.tokens or []):
            if not (token.get("bbox") and token.get("bq") == WEAK and is_number(token)):
                continue
            if token.get("res") or token.get("src") == "kraken":
                continue
            requests.append({"id": f"{line.pk}:weak{i}", "bbox": in_line_rows(token["bbox"], line.bbox)})
            index.append((line, i))
    return requests, index


def read_weak_box(token: dict, chars: list) -> bool:
    """Kraken's reading of a number's own weak box into it, when its gap gave it nothing and the box
    holds as many numbers as the token (`assign`); False otherwise."""
    numbers = assign([token], chars)
    return bool(numbers) and apply_reading(token, numbers[0])


@dataclass
class LineLetters:
    """A line's letters Qari may have written for digits and its digit-less dates (D51), with the
    letters' own areas (`letter_area`, by token index)."""

    line: object
    letters: list[int]
    dates: list[tuple[int, int]]
    areas: dict[int, tuple[list[int], str, bool]]
    date_areas: dict[int, list[int]] = field(default_factory=dict)  # by the date's first token

    def requests(self) -> list[dict]:
        """The runner's areas: the whole line, each letter's own area with less of its neighbours, and
        each date's own area."""
        out = [{"id": f"{self.line.pk}:line", "bbox": [int(v) for v in self.line.bbox]}]
        for i, (bbox, _rest, _box) in self.areas.items():
            out.append({"id": f"{self.line.pk}:letter{i}", "bbox": bbox, "margin_x": LETTER_MARGIN})
        for first, bbox in self.date_areas.items():
            out.append({"id": f"{self.line.pk}:date{first}", "bbox": bbox})
        return out


def page_letters(page, lines: list | None = None) -> list[LineLetters]:
    """The lines of a page with letters or digit-less dates to read (D51)."""
    out: list[LineLetters] = []
    for line in _unreviewed_lines(page) if lines is None else lines:
        tokens = line.tokens or []
        dates = digitless_dates(tokens)
        in_dates = {i for first, last in dates for i in range(first, last + 1)}
        letters = [
            i
            for i, token in enumerate(tokens)
            if i not in in_dates and is_letter_digit(token) and not is_era_sign(tokens, i)
        ]
        if not (letters or dates):
            continue
        areas = {i: area for i in letters if (area := letter_area(tokens, i, line.bbox))}
        date_areas = {
            first: bbox for first, last in dates if (bbox := date_area(tokens, first, last, line.bbox))
        }
        out.append(LineLetters(line, letters, dates, areas, date_areas))
    return out


def read_letters(plan: LineLetters, by_id: dict) -> tuple[int, int]:
    """Kraken's numbers into a line's letters and digit-less dates (module docstring, 6), in place:
    `(letters, dates)` changed."""
    row = by_id.get(f"{plan.line.pk}:line") or {}
    text = str(row.get("text") or "")
    tokens = plan.line.tokens
    letters = dates = 0
    for i in plan.letters:
        own = (by_id.get(f"{plan.line.pk}:letter{i}") or {}).get("chars") or []
        area = plan.areas.get(i)
        if area and shows_letter(own, str(tokens[i].get("t") or "")):
            continue  # its own area, read alone, shows Qari's letter: a letter
        number = anchored_number(text, tokens, i) if text else ""
        if not number and area and own:
            number = box_number(own, area[1])
        letters += apply_letter(tokens[i], number)
    for first, last in sorted(plan.dates, reverse=True):  # right to left: earlier indexes stay put
        own = str((by_id.get(f"{plan.line.pk}:date{first}") or {}).get("text") or "")
        reading = date_reading(text, tokens, first, last, own)
        if reading:
            replace_date(tokens, first, last, reading)
            dates += 1
    return letters, dates


def check_years(lines: list) -> int:
    """`ocr.flags.year_check` on a page's lines (in memory), region by region, after Kraken rewrote
    their digits (§4.8): a year agreeing with its value in words is settled, a mismatch gets the words'
    reading as a suggestion. Returns the number of tokens changed."""
    from . import flags

    changed = 0
    run: list = []
    for line in [*lines, None]:
        if run and (line is None or line.region_id != run[-1].region_id):
            changed += flags.year_check([item.tokens or [] for item in run])
            run = []
        if line is not None:
            run.append(line)
    return changed


PRINTED_ID = "printed"
PRINTED_PAD = 8  # pixels of paper kept around the page-number region, as the OCR crops it


def printed_number_area(bbox, width: int, height: int, pad: int = PRINTED_PAD) -> list[int]:
    """The page-number region's box with `pad` pixels of paper around it, inside the image."""
    x0, y0, x1, y1 = (int(v) for v in bbox)
    return [max(0, x0 - pad), max(0, y0 - pad), min(int(width), x1 + pad), min(int(height), y1 + pad)]


def printed_number_request(page, pre) -> dict | None:
    """The runner request for the page's page-number region (None without one). Tesseract and both
    models misread an isolated Arabic-Indic number (`ocr.services.read_page_number` votes; on a printed
    hijri history the vote agreed on 21 of 147 pages, several of them wrong), so in an Arabic-Indic book
    Kraken reads the region too and its number, when it reads as one, is the page's (full-book test,
    2026-09-28)."""
    from processing.models import Region

    region = page.regions.filter(kind=Region.Kind.PAGE_NUMBER).order_by("order").first()
    if region is None or not region.bbox:
        return None
    return {"id": PRINTED_ID, "bbox": printed_number_area(region.bbox, pre.output_width, pre.output_height)}


def printed_number_from(row: dict | None) -> str:
    """The Western page number in Kraken's reading of the page-number region ('' unless the reading is
    only a page number, «— ٢١ —», «٢١»)."""
    from ocr.services import page_number_digits

    text = " ".join(str((row or {}).get("text") or "").split()).translate(KRAKEN_DIGITS)
    return page_number_digits(text) or ""


def read_page_numbers(page, engine=None, style: str | None = None) -> PageNumbers:
    """The numbers pass on one finalised page (module docstring). `style`: the book's, when the caller
    knows it (a whole book at once); `engine`: a Kraken engine (tests pass a fake). A line the reviewer
    changed while Kraken read is left as they made it."""
    import json

    from django.db import transaction

    from books.models import Page
    from ocr.models import Line, OcrRun
    from ocr.services import count_unresolved
    from review.services import _shift_gaps, refresh_page_text  # the review app owns a page's lines

    from .engines import registry

    result = PageNumbers()
    if page.reviewed_at is not None:
        result.skipped = "approved"
        return result
    result.style = style if style is not None else (book_style(page.book) or page_style(page))
    if result.style != ARABIC_INDIC:
        result.skipped = "not arabic-indic"
        return result
    pre = getattr(page, "preprocess", None)
    if pre is None or not pre.gray_image:
        result.skipped = "no image"
        return result
    lines = _unreviewed_lines(page)
    as_read = {line.pk: [id(token) for token in line.tokens or []] for line in lines}  # `token_moves`
    requests, index = page_areas(page, lines)
    weak, weak_index = page_weak_boxes(page, lines)
    plans = page_letters(page, lines)
    printed = printed_number_request(page, pre)
    requests_all = requests + weak + [request for plan in plans for request in plan.requests()]
    if printed:
        requests_all.append(printed)
    result.areas = len(requests_all)
    if not requests_all:
        return result
    read_at = {line.pk: line.updated_at for line in lines}
    engine = engine or registry.get_engine("kraken")
    answer = engine.read([{"image": pre.gray_image.path, "lines": requests_all}])
    result.seconds = float(answer.get("elapsed") or answer.get("seconds") or 0.0)
    by_id = {row.get("id"): row for row in answer.get("lines") or []}
    changed: dict[int, object] = {}
    counts: dict[int, list[int]] = {}  # line → [numbers, letters, dates] Kraken gave it

    def gave(line, numbers: int = 0, letters: int = 0, dates: int = 0) -> None:
        if numbers or letters or dates:
            changed[line.pk] = line
            count = counts.setdefault(line.pk, [0, 0, 0])
            count[0] += numbers
            count[1] += letters
            count[2] += dates

    for request, (line, area) in zip(requests, index, strict=True):
        row = by_id.get(request["id"])
        if not row:
            continue
        tokens = line.tokens
        numbers = assign([tokens[i] for i in area.tokens], row.get("chars") or [])
        own_box = len(area.tokens) == 1 and bool(word_box(tokens[area.tokens[0]]))
        if numbers is None and own_box:
            whole = box_text(row.get("chars") or [])
            gave(line, bool(whole) and apply_reading(tokens[area.tokens[0]], whole=whole))
            continue
        if numbers is None:
            continue
        gave(
            line, sum(apply_reading(tokens[i], found) for i, found in zip(area.tokens, numbers, strict=True))
        )
    for request, (line, i) in zip(weak, weak_index, strict=True):
        row = by_id.get(request["id"])
        if row:
            gave(line, read_weak_box(line.tokens[i], row.get("chars") or []))
    for plan in plans:
        letters, dates = read_letters(plan, by_id)
        gave(plan.line, letters=letters, dates=dates)
    printed_number = printed_number_from(by_id.get(PRINTED_ID)) if printed else ""
    with transaction.atomic():
        locked = Page.objects.select_for_update(of=("self",)).get(pk=page.pk)  # as review does
        if printed_number and locked.reviewed_at is None and printed_number != locked.printed_number:
            Page.objects.filter(pk=page.pk).update(printed_number=printed_number)
            page.printed_number = printed_number
            result.printed = printed_number
        now = dict(
            Line.objects.filter(pk__in=list(changed), is_reviewed=False).values_list("pk", "updated_at")
        )
        kept = [
            line for pk, line in changed.items() if locked.reviewed_at is None and now.get(pk) == read_at[pk]
        ]
        if kept:
            check_years(lines)  # §4.8: Kraken rewrote digits; only the kept lines are saved below
        dates_of = {plan.line.pk: plan.dates for plan in plans}
        for line in kept:
            line.text = " ".join(token["t"] for token in line.tokens)
            line.n_low = count_unresolved(line.tokens)
            line.save(update_fields=["tokens", "text", "n_low", "updated_at"])
            moves = token_moves(as_read[line.pk], line.tokens, dates_of.get(line.pk, ()))
            _shift_gaps(line, moves, line.tokens)  # its suggestions follow the words (D72)
            numbers, letters, dates = counts[line.pk]
            result.applied += numbers + letters + dates
            result.letters += letters
            result.dates += dates
        if kept:
            refresh_page_text(page)
        params = {"areas": result.areas, "applied": result.applied, "style": result.style}
        if result.letters or result.dates:
            params.update(letters=result.letters, dates=result.dates)
        if result.printed:
            params["printed"] = result.printed
        if len(kept) < len(changed):
            params["left_to_reviewer"] = len(changed) - len(kept)  # changed or removed while Kraken read
        OcrRun.objects.create(
            page=page,
            engine_name=engine.name,
            model_id=engine.model_id,
            model_revision=engine.model_revision,
            backend=engine.backend,
            input_variant="gray",
            raw_output=json.dumps(answer.get("lines") or [], ensure_ascii=False),
            parsed_text="\n".join(str(row.get("text") or "") for row in answer.get("lines") or []),
            params=params,
            duration_ms=int(result.seconds * 1000),
            finish="n/a",
        )
    log.info("page %s: Kraken gave %d readings (%d areas read)", page.pk, result.applied, result.areas)
    return result


def schedule(page) -> None:
    """Queue the numbers pass of a finalised page (after the commit), when it is on and Kraken is set up."""
    from django.conf import settings
    from django.db import transaction

    if not settings.NASSAKH.get("NUMBERS_PASS", True):
        return
    from .engines.kraken import KrakenEngine

    if not KrakenEngine().is_prepared():
        return
    from books import runs

    from . import tasks

    run = runs.active_token(page.pk)  # the run finalising the page: a newer run makes the pass skip
    transaction.on_commit(lambda: tasks.read_numbers.delay(page.pk, run=run))
