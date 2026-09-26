"""The numbers pass's letters and dates (D51) on the labelled pages, as the pass runs them, not saved.

For every page holding a labelled letter (letters_truth.json), the pass's own code plans the areas
(`page_letters`), Kraken reads them (the real engine) and `read_letters` changes copies of the lines;
each labelled letter is then scored: a number read (exact when its value is known), a real letter left
alone, and the three digit-less dates.

    .venv/bin/python playground/digits/letters_dryrun.py
"""

from __future__ import annotations

import copy
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")

import django  # noqa: E402

django.setup()

from books.models import Page  # noqa: E402
from ocr import numbers as nb  # noqa: E402
from ocr.engines.kraken import KrakenEngine  # noqa: E402

truth = json.loads((HERE / "letters_truth.json").read_text())
found = {f["key"]: f for f in json.loads((HERE / "letters.json").read_text())}
pages = sorted({(found[k]["book"], found[k]["page"]) for k in truth})
engine = KrakenEngine()
tally, seconds = Counter(), 0.0
for book, number in pages:
    page = Page.objects.select_related("preprocess").get(book_id=book, number=number)
    lines = nb._unreviewed_lines(page)
    refs = {}  # the letter's token dict: `apply_letter` changes it in place, dates only replace their own
    for line in lines:
        line.tokens = copy.deepcopy(line.tokens)
        line.before = copy.deepcopy(line.tokens)
        for key, f in found.items():
            if (f["book"], f["page"], f["line"]) == (book, number, line.order) and key in truth:
                refs[key] = line.tokens[f["first"]]
    plans = nb.page_letters(page, lines)
    if not plans:
        continue
    requests = [r for plan in plans for r in plan.requests()]
    answer = engine.read([{"image": page.preprocess.gray_image.path, "lines": requests}])
    seconds += answer["elapsed"]
    by_id = {row["id"]: row for row in answer["lines"]}
    for plan in plans:
        nb.read_letters(plan, by_id)
        line = plan.line
        for key, lab in truth.items():
            f = found[key]
            if (f["book"], f["page"], f["line"]) != (book, number, line.order):
                continue
            kind = lab["is"]
            if kind == "date":
                got = next((t["t"] for t in line.tokens if t.get("qari", {}).get("t") == f["qari"]), None)
                ok = got == lab["value"]
                tally[f"date:{'exact' if ok else 'missed'}"] += 1
                print(f"  date {key}: {f['qari']!r} -> {got!r} (page {lab['value']})")
                continue
            if kind == "date-part":
                continue
            before = line.before[f["first"]]
            token = refs[key]
            changed = token.get("src") == "kraken" and token.get("alt") == before["t"]
            if kind == "digit":
                tally["digit:read" if changed else "digit:missed"] += 1
                if changed and lab.get("value"):
                    good = token["t"].strip("()[]«»“”\".،:؛") == lab["value"]
                    tally["digit:exact" if good else "digit:WRONG"] += 1
                    if not good:
                        print(f"  wrong {key}: {before['t']!r} -> {token['t']!r} (page {lab['value']})")
            else:
                tally[f"{kind}:{'CHANGED' if changed else 'kept'}"] += 1
                if changed:
                    print(f"  changed {kind} {key}: {before['t']!r} -> {token['t']!r}")
print(dict(sorted(tally.items())), f"Kraken {seconds:.1f} s on {len(pages)} pages")
