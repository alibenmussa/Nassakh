"""Arabic text helpers ported from the Phase 1 PoC (`playground/poc/common.py`).

Normalisation here is for *comparison* only (metrics, alignment, sanity checks). Stored text
keeps its diacritics; the only stored conversion is Arabic-Indic → Western digits in
`Page.final_text` (D6).
"""

from __future__ import annotations

import html
import re
import unicodedata

# Harakat, tanween, shadda, sukun, superscript alef, Quranic marks, tatweel.
_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۜ۟-۪ۤۧۨ-ۭـ]")
_ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_DIGIT = re.compile(r"[0-9٠-٩۰-۹]")
_DIGIT_ONLY = re.compile(r"^[0-9٠-٩۰-۹]+(?:[.,،/:\-][0-9٠-٩۰-۹]+)*$")
_TOKEN_EDGE = re.compile(r"^[\s\W_]+|[\s\W_]+$", re.UNICODE)
_ARABIC_LETTER = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")

LEVELS = ("raw", "no_tashkeel", "lenient")


def to_western_digits(text: str) -> str:
    """Replace Arabic-Indic and Persian digits with 0-9."""
    return text.translate(_ARABIC_INDIC)


def strip_tashkeel(text: str) -> str:
    """Remove diacritics and tatweel. Used for comparison only, never for storage."""
    return _DIACRITICS.sub("", text)


def fold_letters(text: str) -> str:
    """Fold alef/ya/ta-marbuta/hamza variants for a lenient comparison."""
    text = re.sub("[أإآٱ]", "ا", text)
    return text.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")


def normalize_ws(text: str) -> str:
    """Collapse whitespace but keep line breaks (one per line, stripped)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t ‏‎‪-‮]+", " ", text)
    lines = [ln.strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def strip_markup(text: str) -> str:
    """Turn HTML/Markdown (Qari v0.3 output) into plain lines."""
    t = re.sub(r"<\s*br\s*/?>", "\n", text, flags=re.I)
    t = re.sub(
        r"</\s*(p|div|h[1-6]|li|tr|table|thead|tbody|section|article|header|footer|"
        r"blockquote|ul|ol|pre|figcaption)\s*>",
        "\n",
        t,
        flags=re.I,
    )
    t = re.sub(r"</\s*t[dh]\s*>", " ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)  # markdown headings
    t = re.sub(r"^\s*[-:| ]{3,}\s*$", "", t, flags=re.M)  # markdown table rules
    t = re.sub(r"^\s*[-*•]\s+", "", t, flags=re.M)  # bullets
    t = re.sub(r"(\*\*|__|`{1,3})", "", t)  # bold / code marks
    t = t.replace("|", " ")
    return normalize_ws(t)


def _cut_repeats(
    text: str, key: str, min_unit: int, max_unit: int, min_repeats: int, keep: int
) -> tuple[str, bool]:
    """Cut trailing repeats found on `key` (same length as `text`), keeping `keep` copies."""
    truncated = False
    while True:
        found = False
        for unit in range(min_unit, min(max_unit, len(key) // min_repeats) + 1):
            tail = key[-unit:]
            if all(key[-(k + 1) * unit : -k * unit or None] == tail for k in range(1, min_repeats)):
                copies = 1
                while len(key) >= (copies + 1) * unit and key[-(copies + 1) * unit : -copies * unit] == tail:
                    copies += 1
                drop = max(0, copies - keep) * unit
                if drop:
                    text, key = text[:-drop], key[:-drop]
                    truncated = True
                found = drop > 0
                break
        if not found:
            return text.rstrip(), truncated


def truncate_repetition(
    text: str, hit_cap: bool = False, min_unit: int = 8, max_unit: int = 400
) -> tuple[str, bool]:
    """Cut a degenerate repeating tail (model loop). Returns (text, was_truncated).

    Pass 1 (always): a unit of 8..400 chars repeated verbatim 3+ times at the end
    is reduced to one copy. Pass 2 (only when the run hit the token cap): the same
    with digits masked, so incrementing loops like "(131) … (132) …" are caught;
    the first five copies are kept because short footnote lists look alike.
    """
    text, t1 = _cut_repeats(text, text, min_unit, max_unit, 3, 1)
    t2 = False
    if hit_cap:
        key = _DIGIT.sub("0", text)
        text, t2 = _cut_repeats(text, key, min_unit, max_unit, 4, 5)
    return text, t1 or t2


def parse_output(raw: str, hit_cap: bool = False) -> tuple[str, bool]:
    """Plain text from a raw model output: loop-truncated, markup stripped. Returns (text, looped)."""
    text, looped = truncate_repetition(raw, hit_cap=hit_cap)
    return strip_markup(text), looped


def normalize(text: str, level: str) -> str:
    """Normalise text for metrics. level: raw | no_tashkeel | lenient.

    raw          NFKC + whitespace only (diacritic errors count)
    no_tashkeel  also drops diacritics and tatweel
    lenient      also folds letter variants, converts digits, drops punctuation
    """
    t = unicodedata.normalize("NFKC", text)
    t = normalize_ws(t).replace("\n", " ")
    if level == "raw":
        return re.sub(r" +", " ", t).strip()
    t = strip_tashkeel(t)
    if level == "no_tashkeel":
        return re.sub(r" +", " ", t).strip()
    if level != "lenient":
        raise ValueError(f"unknown normalisation level: {level}")
    t = to_western_digits(fold_letters(t))
    t = _NON_WORD.sub(" ", t).lower()
    return re.sub(r" +", " ", t).strip()


def is_digit_token(tok: str) -> bool:
    """True when a token is a number (Western, Arabic-Indic or Persian digits), ignoring edge punctuation.

    "1966", "(٣)", "١٢/٣" and "[۲]" are digit tokens; "ص٣" and "" are not. Digits are always
    low-confidence in the review screen (D17).
    """
    core = _TOKEN_EDGE.sub("", tok or "")
    return bool(core) and bool(_DIGIT_ONLY.match(core))


def arabic_ratio(text: str) -> float:
    """Share of alphabetic characters that belong to the Arabic script (0.0 when there are no letters)."""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    arabic = sum(1 for ch in letters if _ARABIC_LETTER.match(ch))
    return arabic / len(letters)
