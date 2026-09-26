"""Years written in digits and in words («سنة ( ٢٤٢ ) اثنتين واربعين ومايتين»): parse the words, compare."""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
import django  # noqa: E402

django.setup()
from core.arabic import normalize, to_western_digits  # noqa: E402

UNITS = {
    "واحد": 1, "واحده": 1, "احد": 1, "احدي": 1, "اثنين": 2, "اثنتين": 2, "اثنان": 2, "اثنتان": 2, "اثني": 2, "اثنتي": 2,
    "ثلاث": 3, "ثلاثه": 3, "ثلث": 3, "اربع": 4, "اربعه": 4, "خمس": 5, "خمسه": 5, "ست": 6, "سته": 6, "سبع": 7, "سبعه": 7,
    "ثمان": 8, "ثماني": 8, "ثمانيه": 8, "تسع": 9, "تسعه": 9,
}
TENS = {"عشر": 10, "عشره": 10, "عشرين": 20, "عشرون": 20, "ثلاثين": 30, "ثلثين": 30, "ثلاثون": 30, "اربعين": 40, "اربعون": 40,
        "خمسين": 50, "خمسون": 50, "ستين": 60, "ستون": 60, "سبعين": 70, "سبعون": 70, "ثمانين": 80, "ثمانون": 80, "تسعين": 90, "تسعون": 90}
HUNDREDS = {"ماءه": 100, "مايه": 100, "ميه": 100, "مائه": 100, "مئه": 100, "مايتين": 200, "مائتين": 200, "ماءتين": 200, "مايتان": 200, "مائتان": 200, "ميتين": 200}
THOUSANDS = {"الف": 1000, "الفين": 2000, "الفان": 2000}


def word_value(w: str) -> tuple[str, int] | None:
    w = normalize(w, "lenient")
    w = w.replace("ة", "ه").replace("ى", "ي")
    for prefix in ("", "و"):
        if prefix and not w.startswith(prefix):
            continue
        core = w[len(prefix):]
        if core in UNITS:
            return "u", UNITS[core]
        if core in TENS:
            return "t", TENS[core]
        if core in HUNDREDS:
            return "h", HUNDREDS[core]
        if core in THOUSANDS:
            return "k", THOUSANDS[core]
        for u, v in UNITS.items():  # ثلاثمائة / ثلثماية / اربعمائة ...
            for h in ("مايه", "مائه", "ماءه", "ميه", "مئه"):
                if core == u + h or core == u.rstrip("ه") + h:
                    return "h", v * 100
        for u, v in UNITS.items():  # ثلاثة آلاف
            if core in ("الاف", "آلاف"):
                return "K", 1000
    return None


def parse_number_words(words: list[str]) -> tuple[int, int] | None:
    """Value of the number words at the start of `words` and how many words they take (None if none)."""
    total = 0
    n = 0
    last = None
    for w in words:
        v = word_value(w)
        if v is None:
            break
        kind, value = v
        if kind == "t" and value == 10 and last == "u":
            total += 10  # ثلاث عشرة
        elif kind == "K":
            total = (total or 1) * 1000
        else:
            total += value
        last = kind
        n += 1
    return (total, n) if n else None


_DIGITS = re.compile(r"[0-9٠-٩۰-۹]+")


def check_line_numbers(tokens: list[str]) -> list[tuple[int, int, int]]:
    """[(token index, digits value, words value)] for numbers followed (after brackets) by number words."""
    out = []
    for i, t in enumerate(tokens):
        m = _DIGITS.search(t)
        if not m:
            continue
        j = i + 1
        while j < len(tokens) and not any(ch.isalnum() for ch in tokens[j]):
            j += 1
        parsed = parse_number_words(tokens[j:j + 6])
        if parsed:
            value, _ = parsed
            if value >= 3:
                out.append((i, int(to_western_digits(m.group())), value))
    return out


if __name__ == "__main__":
    from books.models import Page
    from collections import Counter
    c = Counter()
    for page in Page.objects.filter(is_excluded=False).order_by("book_id", "number"):
        toks = []
        for line in page.lines.order_by("order"):
            toks.extend(t.get("t") or "" for t in line.tokens or [])
        for i, d, v in check_line_numbers(toks):
            c[(page.book_id, d == v)] += 1
            if d != v and page.book_id in (23, 19, 21, 25):
                print(page.book_id, page.number, "digits", d, "words", v, "|", " ".join(toks[max(0, i - 2): i + 6]))
    print(sorted(c.items()))
