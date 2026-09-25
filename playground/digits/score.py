"""Digits experiment, step 3: score every reader against truth.json.

For each crop the numbers are the digit runs of the reading (Arabic-Indic, Persian and Western digits
all count, compared as Western). Per reader:
    crop exact   the crop's numbers are exactly the true ones (as a multiset: order across numbers
                 is not scored; a crop with one number, most of them, is simply right or wrong)
    numbers      share of the true numbers found exactly
    nothing      crops where the reader returned no digit at all
    reversed     true numbers read with their digits in reverse order (a direction slip)
Crops marked skip in truth.json (duplicate scans, bad crops, unsure) are left out.

Writes results/scores.json and prints the tables. Run: .venv/bin/python playground/digits/score.py
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
RUN = re.compile(r"[0-9٠-٩۰-۹]+")


def numbers_of(text: str | None) -> list[str]:
    return [run.translate(DIGITS) for run in RUN.findall(text or "")]


def score(
    readings: dict[str, str], truth: dict, manifest: dict, script: str | None = None, book: int | None = None
) -> dict:
    n = exact = nothing = found = total = reversed_ = 0
    for key, label in truth.items():
        if "skip" in label or (script and label["script"] != script):
            continue
        if book is not None and manifest[key]["book"] != book:
            continue
        want = label["numbers"]
        got = numbers_of(readings.get(key))
        n += 1
        total += len(want)
        if not got:
            nothing += 1
        if Counter(got) == Counter(want):
            exact += 1
        left = Counter(got)
        for w in want:
            if left[w] > 0:
                left[w] -= 1
                found += 1
            elif w[::-1] != w and w[::-1] in got:
                reversed_ += 1
    if not n:
        return {}
    return {
        "crops": n,
        "exact": round(100 * exact / n, 1),
        "numbers": round(100 * found / total, 1),
        "nothing": round(100 * nothing / n, 1),
        "reversed": reversed_,
    }


def load_readers() -> dict[str, dict[str, str]]:
    readers: dict[str, dict[str, str]] = {}
    manifest = json.loads((HERE / "manifest.json").read_text())
    readers["stored: Qari v0.3 (page)"] = {m["id"][:3]: m.get("t") or "" for m in manifest}
    readers["stored: Qari v0.2 (page)"] = {m["id"][:3]: m.get("alt") or m.get("t") or "" for m in manifest}
    for path in sorted((HERE / "results").glob("*.json")):
        if path.name == "scores.json":
            continue
        for variant, table in json.loads(path.read_text()).items():
            readers[f"{path.stem}: {variant}"] = table
    return readers


def main() -> None:
    truth = json.loads((HERE / "truth.json").read_text())
    manifest = {m["id"][:3]: m for m in json.loads((HERE / "manifest.json").read_text())}
    readers = load_readers()
    books = sorted({manifest[k]["book"] for k, v in truth.items() if "skip" not in v})
    out: dict = {}
    head = f"{'reader':38} {'crops':>5} {'exact':>6} {'numbers':>8} {'nothing':>8} {'reversed':>8}"
    for script, title in (("A", "Arabic-Indic digits ٠١٢٣٤٥٦٧٨٩"), ("W", "Western digits 0123456789")):
        print(f"\n{title}\n{head}")
        for name, readings in readers.items():
            s = score(readings, truth, manifest, script)
            out.setdefault(name, {})[script] = s
            # a whole line holds other numbers too: only "numbers found" is meaningful there
            exact = "     –" if "_line_" in name else f"{s['exact']:5}%"
            print(f"{name:38} {s['crops']:5} {exact} {s['numbers']:7}% {s['nothing']:7}% {s['reversed']:8}")
    print("\nArabic-Indic, crop exact per book (line readers: numbers found)")
    print(f"{'reader':38} " + " ".join(f"{'b' + str(b):>6}" for b in books))
    for name, readings in readers.items():
        cells = []
        for b in books:
            s = score(readings, truth, manifest, "A", b)
            value = (s["numbers"] if "_line_" in name else s["exact"]) if s else None  # lines: numbers found
            cells.append(f"{str(value) + '%' if s else '-':>6}")
            if s:
                out[name].setdefault("books", {})[b] = s
        print(f"{name:38} " + " ".join(cells))
    (HERE / "results" / "scores.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
