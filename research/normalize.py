"""The Arabic normal form of search and quotation checking (D107, CHALLENGE_SPEC §2), with a map back to the
words as written.

A text is cut into **words**: its whitespace-separated tokens, each split again where punctuation or a
bracket sits inside it («قال:حدثنا» is two words). A word keeps its raw form (`Word.raw`: letters and
diacritics as written, punctuation and brackets dropped) beside its normal form (`Word.norm`):

- presentation forms read as their letters («ﻻ» → «لا»; a ligature that stands for several words, «ﷺ», is
  kept as it is so one word stays one word);
- tashkeel (U+064B–U+065F, U+0670), tatweel and every other mark are dropped, and so is anything that is not
  a letter or a digit (punctuation, brackets, symbols, joiners);
- أ إ آ ٱ → ا, ى → ي, ة → ه, ؤ → و, ئ → ي (and the Persian ی → ي, ک → ك);
- Arabic-Indic and Persian digits → Western.

A word whose normal form is empty (a dash, a lone bracket) is no word. A footnote mark is apparatus, not
text: a token that is a bracketed number of one or two digits («(١)», «[٢]», «٣)») is skipped, and such a
mark glued to a word («الشيخ(١)») is cut off it; plain numbers («سنة 1966») and long bracketed ones (a
hadith's number, «(١٣٤٢٨)») stay. The index (`research.index`) and the quotations go through the same
functions, so a match on normal forms maps back to the words, their lines and their tokens.

The diacritics are kept for the diff: `vowel_key` is a word's letters and marks in canonical order, and
`diacritics_differ` tells two words that read the same and are both vowelled, differently.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

__all__ = [
    "Word",
    "diacritics_differ",
    "has_diacritics",
    "is_marker",
    "normalize_text",
    "normalize_word",
    "token_words",
    "vowel_key",
    "words_of",
]

TATWEEL = "\u0640"
_TASHKEEL = re.compile("[\u064b-\u065f\u0670]")
# the vowels proper (no maddah or hamza mark: those are spelling, folded by the normal form)
_VOWELS = re.compile("[\u064b-\u0652\u0656-\u065f\u0670]")
_FOLD = str.maketrans(
    {
        "\u0623": "\u0627",  # أ → ا
        "\u0625": "\u0627",  # إ → ا
        "\u0622": "\u0627",  # آ → ا
        "\u0671": "\u0627",  # ٱ → ا
        "\u0649": "\u064a",  # ى → ي
        "\u0629": "\u0647",  # ة → ه
        "\u0624": "\u0648",  # ؤ → و
        "\u0626": "\u064a",  # ئ → ي
        "\u06cc": "\u064a",  # ی → ي
        "\u06a9": "\u0643",  # ک → ك
        **{chr(0x0660 + n): str(n) for n in range(10)},
        **{chr(0x06F0 + n): str(n) for n in range(10)},
    }
)
_DIGITS = "0-9\u0660-\u0669\u06f0-\u06f9"
_OPEN = r"\(\[\{﴿<«"
_CLOSE = r"\)\]\}﴾>»"
# a footnote mark standing alone: «(١)», «[2]», «(٣», «٣)», with the punctuation that may follow it
_MARKER = re.compile(
    rf"^(?:[{_OPEN}]\s*[{_DIGITS}]{{1,2}}\s*[{_CLOSE}]?|[{_DIGITS}]{{1,2}}\s*[{_CLOSE}])[.,،؛:]*$"
)
# the same mark glued to a word
_GLUED_MARKER = re.compile(rf"[{_OPEN}]\s*[{_DIGITS}]{{1,2}}\s*[{_CLOSE}]")
# presentation forms (A and B): read as their letters through NFKC
_PRESENTATION = re.compile("[\ufb50-\ufdff\ufe70-\ufeff]")


@dataclass(frozen=True)
class Word:
    """One word of a text: as written (`raw`: letters and diacritics, no punctuation) and its normal form."""

    raw: str
    norm: str


def _compat(text: str) -> str:
    """Presentation forms as their letters, except a ligature that NFKC turns into several words."""

    def one(match: re.Match) -> str:
        char = match.group(0)
        folded = unicodedata.normalize("NFKC", char)
        return char if " " in folded else folded

    return _PRESENTATION.sub(one, text)


def _is_word_char(char: str) -> bool:
    """Letters, digits and marks belong to a word (tatweel and joiners too); anything else separates words."""
    return char == TATWEEL or unicodedata.category(char)[0] in "LNM" or unicodedata.category(char) == "Cf"


def normalize_word(raw: str) -> str:
    """The normal form of one word (see the module docstring); '' when nothing of a word is left."""
    text = _compat(raw).translate(_FOLD)
    return "".join(ch for ch in text if ch != TATWEEL and unicodedata.category(ch)[0] in "LN")


def is_marker(token: str) -> bool:
    """A footnote mark standing alone («(١)», «[2]», «٣)»): apparatus, never a word of the text."""
    return bool(_MARKER.match(token.strip()))


def token_words(token: str) -> list[Word]:
    """The words of one whitespace-separated token: none for a footnote mark, else its pieces between
    punctuation (a glued mark cut off), each with its normal form; pieces without one are dropped."""
    if is_marker(token):
        return []
    text = _GLUED_MARKER.sub(" ", _compat(token))
    words: list[Word] = []
    piece: list[str] = []
    for char in text + " ":
        if _is_word_char(char):
            piece.append(char)
            continue
        if piece:
            raw = "".join(piece)
            norm = normalize_word(raw)
            if norm:
                words.append(Word(raw=raw, norm=norm))
            piece = []
    return words


def words_of(text: str) -> list[Word]:
    """Every word of `text` in order (`token_words` of each whitespace-separated token)."""
    return [word for token in str(text or "").split() for word in token_words(token)]


def normalize_text(text: str) -> str:
    """The normal forms of the words of `text` joined by single spaces."""
    return " ".join(word.norm for word in words_of(text))


def has_diacritics(raw: str) -> bool:
    """True when the word carries a haraka, a tanween, a shadda, a sukun or a superscript alef."""
    return bool(_VOWELS.search(raw or ""))


def vowel_key(raw: str) -> str:
    """A word's letters (folded as the normal form folds them) and its tashkeel, marks in canonical order:
    two spellings of the same vowelled word give the same key."""
    text = unicodedata.normalize("NFC", _compat(raw or "").replace(TATWEEL, ""))
    out = []
    for char in text:
        if _VOWELS.match(char):
            out.append(char)
        elif unicodedata.category(char)[0] in "LN":
            out.append(char.translate(_FOLD))
    return unicodedata.normalize("NFC", "".join(out))


def diacritics_differ(quoted: str, source: str) -> bool:
    """True when two words that read the same are both vowelled and their vowels differ (a quotation
    without vowels, or a source without them, is not a difference)."""
    if not (has_diacritics(quoted) and has_diacritics(source)):
        return False
    return vowel_key(quoted) != vowel_key(source)
