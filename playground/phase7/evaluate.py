"""Offline evaluation of flag policies on features.jsonl (see features.py)."""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict

sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
import django  # noqa: E402

django.setup()
from rapidfuzz import fuzz  # noqa: E402

from core.arabic import normalize, strip_tashkeel, to_western_digits  # noqa: E402

PATH = sys.argv[1] if len(sys.argv) > 1 else "features.jsonl"
rows = [json.loads(l) for l in open(PATH)]
_AR = re.compile(r"[ء-يٮ-ۓ]")
_LAT = re.compile(r"[A-Za-z]")
_DIG = re.compile(r"[0-9٠-٩۰-۹]")
_AIDIG = re.compile(r"[٠-٩۰-۹]")
FOLD = str.maketrans({"،": ",", "؛": ";", "؟": "?", "٫": ".", "…": "."})


def lenient(t):
    return normalize(t or "", "lenient")


def marks(t):
    return "".join(ch for ch in unicodedata.normalize("NFKC", t or "") if not ch.isalnum() and not unicodedata.combining(ch) and ch not in "ـ ").translate(FOLD)


def arabic_word(t):
    return bool(t) and bool(_AR.search(t)) and not _LAT.search(t)


def unknown(r):
    """A flagged token the reviewer never looked at (forced approval): no evidence either way."""
    return r["conf"] == "low" and not r["res"] and not r["changed"]


def p0(r):
    return r["conf"] == "low"


def number_agrees(r):
    t = to_western_digits(re.sub(r"\D", "", to_western_digits(r["t"])))
    s = to_western_digits(re.sub(r"\D", "", to_western_digits(r["sec"] or "")))
    te = to_western_digits(re.sub(r"\D", "", to_western_digits(r["tess"] or r["t"])))
    return bool(t) and t == s and t == te


def p1(r, western_relax=True, word_nosec=True):
    """Punctuation-aware disagreement; numbers still low unless all three readers agree (Western)."""
    t, sec = r["t"], r["sec"]
    cat = r["cat"]
    if r["src"] == "kraken":
        return True
    if cat == "number" or cat == "mixed_digit":
        if western_relax and not _AIDIG.search(t) and number_agrees(r):
            return False
        return True
    if cat == "letterdigit":
        return True
    if cat == "punct":
        return sec is not None and marks(sec) != marks(t) and marks(sec) != ""
    if not r.get("has_sec"):
        return False  # one reader only: no disagreement to see (as today)
    if sec is None:
        return word_nosec
    return lenient(t) != lenient(sec)


def tess_disagrees(r, min_conf=None, min_ratio=0):
    tt = r["tess"]
    if not tt or not arabic_word(tt) or r["cat"] not in ("word",):
        return False
    if lenient(tt) == lenient(r["t"]):
        return False
    if min_conf is not None and (r["tconf"] is None or r["tconf"] < min_conf):
        return False
    if min_ratio and fuzz.ratio(lenient(tt), lenient(r["t"])) < min_ratio:
        return False
    return True


def hidden(r):
    """t and sec differ only in diacritics / letter folds (hidden by the lenient comparison)."""
    t, sec = r["t"], r["sec"]
    if not sec or r["cat"] != "word":
        return False
    a = re.sub(r"[^\wً-ٰٟ]", "", unicodedata.normalize("NFKC", t))
    b = re.sub(r"[^\wً-ٰٟ]", "", unicodedata.normalize("NFKC", sec))
    return a != b and lenient(t) == lenient(sec)


def score(name, policy, subset=None):
    tp = fp = fn = tn = unk = 0
    by_page_flags = Counter()
    by_page_err = Counter()
    for r in rows:
        if subset and not subset(r):
            continue
        f = policy(r)
        if unknown(r):
            unk += 1
            continue
        key = (r["book"], r["page"])
        by_page_flags[key] += f
        by_page_err[key] += r["changed"]
        if f and r["changed"]:
            tp += 1
        elif f:
            fp += 1
        elif r["changed"]:
            fn += 1
        else:
            tn += 1
    pages = set(by_page_err) | set(by_page_flags)
    zero_err = sum(1 for k in pages if by_page_flags[k] == 0 and by_page_err[k] > 0)
    zero = sum(1 for k in pages if by_page_flags[k] == 0)
    n = tp + fp + fn + tn
    print(f"{name:48s} flags {tp+fp:5d} ({100*(tp+fp)/max(1,n):4.1f}/100w) useful {tp:4d} prec {tp/max(1,tp+fp):5.1%} "
          f"recall {tp/max(1,tp+fn):5.1%} missed {fn:4d}  pages 0-flag {zero} (with errors {zero_err})  [unknown {unk}]")


if __name__ == "__main__":
    books = sorted({r["book"] for r in rows})
    ux = lambda r: r["book"] in (23, 25)  # noqa: E731
    owner = lambda r: r["book"] not in (23, 25, 26)  # noqa: E731
    for label, subset in (("ALL", None), ("UX 23+25", ux), ("owner books", owner)):
        print("====", label)
        score("P0 current", p0, subset)
        score("P1 punct-aware, numbers always", lambda r: p1(r, western_relax=False), subset)
        score("P1 punct-aware, Western numbers agreeing ok", p1, subset)
        score("P1 + words without secondary not flagged", lambda r: p1(r, word_nosec=False), subset)
        score("P2 P1 + tess disagrees (any)", lambda r: p1(r) or tess_disagrees(r), subset)
        for c in (30, 50, 70, 80, 90):
            score(f"P2 P1 + tess disagrees conf>={c}", lambda r, c=c: p1(r) or tess_disagrees(r, c), subset)
        score("P2b P1 + tess conf>=70 ratio>=60", lambda r: p1(r) or tess_disagrees(r, 70, 60), subset)
        score("P3 P2(70) + hidden diacritics/folds", lambda r: p1(r) or tess_disagrees(r, 70) or hidden(r), subset)
        score("P4 P1 + tess(70) only on single-reader", lambda r: p1(r) or (not r.get("has_sec") and tess_disagrees(r, 70)), subset)
        score("P4b P1 + tess(any) only on single-reader", lambda r: p1(r) or (not r.get("has_sec") and tess_disagrees(r)), subset)
