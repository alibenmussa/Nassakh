"""Check the numbers pass's areas on the labelled numbers (project env builds the request / scores).

    .venv/bin/python playground/digits/eval_areas.py request      # writes areas_request.json
    playground/digits/.venv-kraken/bin/python ocr/engines/kraken_runner.py < areas_request.json > areas_result.json
    .venv/bin/python playground/digits/eval_areas.py score
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from ocr import numbers as nb  # noqa: E402

truth = json.loads((HERE / "truth.json").read_text())
rows = {r["key"]: r for r in json.loads((HERE / "lines.json").read_text())}
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def areas_for(key: str, path: str):
    r = rows[key]
    tokens = [dict(t) for t in r["tokens"]]
    if path == "B":
        tokens[r["token"]]["bbox"] = None
    for area in nb.number_areas(tokens, r["line_bbox"]):
        if r["token"] in area.tokens:
            return tokens, area
    return tokens, None


if sys.argv[1] == "request":
    pages = defaultdict(list)
    for key, lab in truth.items():
        if "skip" in lab:
            continue
        for path in ("A", "B"):
            _tokens, area = areas_for(key, path)
            if area:
                pages[rows[key]["image"]].append({"id": f"{key}|{path}", "bbox": area.bbox})
    request = {"model": str(HERE / "models" / "all_arabic_scripts.mlmodel"),
               "pages": [{"image": img, "lines": lines} for img, lines in pages.items()]}
    (HERE / "areas_request.json").write_text(json.dumps(request))
    print(sum(len(p["lines"]) for p in request["pages"]), "areas")
else:
    result = {line["id"]: line for line in json.loads((HERE / "areas_result.json").read_text())["lines"]}
    for script in ("A", "W"):
        for path in ("A", "B"):
            n = ok = skipped = 0
            for key, lab in truth.items():
                if "skip" in lab or lab["script"] != script:
                    continue
                n += 1
                tokens, area = areas_for(key, path)
                read = result.get(f"{key}|{path}")
                if not area or not read:
                    skipped += 1
                    continue
                got = nb.assign([tokens[i] for i in area.tokens], read["chars"])
                if got is None and path == "A":
                    whole = nb.box_text(read["chars"])
                    got = [nb._RUN.findall(whole)] if whole else None
                if got is None:
                    skipped += 1
                    continue
                mine = got[area.tokens.index(rows[key]["token"])] if path == "B" or len(got) > 1 else got[0]
                ok += Counter(x.translate(DIGITS) for x in mine) == Counter(lab["numbers"])
            label = "own word box" if path == "A" else "gap (no box)"
            print(f"{'Arabic-Indic' if script == 'A' else 'Western':12} {label:13}: right {ok}/{n} = {100*ok/n:.1f}%  (left to Qari: {skipped})")
