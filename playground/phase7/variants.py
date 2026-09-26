"""Book-internal variant rule: a word read once whose one-letter variant the book prints often.

Vocabulary per book: the original tokens of approved pages (features.jsonl) + the tokens of the other
pages as stored. Evaluated on the approved pages' tokens not already flagged (conf high).
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
import django  # noqa: E402

django.setup()
from books.models import Page  # noqa: E402
from ocr.alignment import norm_token  # noqa: E402

CONFUSABLE = [set("عمغ"), set("غفق"), set("فق"), set("حجخ"), set("بتثنيى"), set("رز"), set("دذ"), set("صض"),
              set("طظ"), set("سش"), set("هة"), set("وز"), set("لك"), set("ءئؤ")]


def confusable(a: str, b: str) -> bool:
    return any(a in grp and b in grp for grp in CONFUSABLE)


def edits1(word: str, vocab: dict[str, int], only_confusable: bool):
    out = []
    n = len(word)
    for v, c in vocab.items():
        m = len(v)
        if abs(m - n) > 2 or v == word:
            continue
        if m == n:
            diff = [(x, y) for x, y in zip(word, v) if x != y]
            if len(diff) == 1 and (not only_confusable or confusable(*diff[0])):
                out.append((v, c))
        elif m == n + 1 and not only_confusable:
            for i in range(m):
                if v[:i] + v[i + 1:] == word:
                    out.append((v, c)); break
        elif m == n - 1 and not only_confusable:
            for i in range(n):
                if word[:i] + word[i + 1:] == v:
                    out.append((v, c)); break
        elif m == n + 1 and only_confusable:
            # «بينها» → «بينهما»: an «م» the models drop before a final alef
            for i in range(m):
                if v[:i] + v[i + 1:] == word and v[i] in "م":
                    out.append((v, c)); break
    return out


rows = [json.loads(l) for l in open(sys.argv[1])]
books = sorted({r["book"] for r in rows})
approved_pages = {(r["book"], r["page"]) for r in rows}
vocab: dict[int, Counter] = defaultdict(Counter)
for r in rows:
    k = norm_token(r["t"])
    if k:
        vocab[r["book"]][k] += 1
for page in Page.objects.filter(book_id__in=books, is_excluded=False):
    if (page.book_id, page.number) in approved_pages:
        continue
    for line in page.lines.all():
        for tok in line.tokens or []:
            k = norm_token(tok.get("t") or "")
            if k:
                vocab[page.book_id][k] += 1

for only in (False, True):
    for ratio in (2, 3):
        tp = fp = 0
        ex_tp, ex_fp = [], []
        for r in rows:
            if r["conf"] == "low" or r["cat"] != "word":
                continue
            k = norm_token(r["t"])
            if len(k) < 3:
                continue
            c = vocab[r["book"]][k]
            cands = [(v, n) for v, n in edits1(k, vocab[r["book"]], only) if n >= max(2, ratio * c)]
            if not cands:
                continue
            if r["changed"]:
                tp += 1
                ex_tp.append((r["book"], r["page"], r["t"], r["new"], cands[:3]))
            else:
                fp += 1
                ex_fp.append((r["book"], r["page"], r["t"], cands[:3]))
        missed = sum(1 for r in rows if r["conf"] != "low" and r["changed"] and r["cat"] == "word")
        print(f"only_confusable={only} ratio={ratio}: flags {tp+fp} useful {tp} prec {tp/max(1,tp+fp):.0%} (of {missed} missed word errors)")
        if only and ratio == 2:
            for e in ex_tp[:40]:
                print("   TP", e)
            for e in ex_fp[:40]:
                print("   FP", e)
