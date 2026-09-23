"""Step 5: evaluate all runs against the ground truth and write REPORT.md.

Metrics per run (when gt/<page_id>.txt exists):
  CER / WER at three normalisation levels: raw, no_tashkeel, lenient
Always reported (no GT needed): timing, output length, finish reason, grid
coverage, and OCR line count vs detected image lines.

Also writes runs_summary.csv for spreadsheets.
"""
from __future__ import annotations

import csv
import datetime as dt
import statistics as st
from collections import defaultdict
from pathlib import Path

import jiwer

import config
from common import LEVELS, load_manifest, normalize, parse_output, read_json


def load_runs() -> list[dict]:
    runs = []
    for p in sorted(config.RUNS.glob("*.json")):
        rec = read_json(p)
        rec["_file"] = p.name
        runs.append(rec)
    return runs


def load_gt() -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in config.GT.glob("*.txt")}


def load_prep() -> dict[str, dict]:
    return {p.name.split(".")[0]: read_json(p) for p in config.PAGES.glob("*.prep.json")}


def word_f1(ref: str, hyp: str) -> float:
    """Order-insensitive word overlap (multiset F1). Fair for tables and reordered blocks."""
    from collections import Counter

    r, h = Counter(ref.split()), Counter(hyp.split())
    common = sum((r & h).values())
    total = sum(r.values()) + sum(h.values())
    return (2 * common / total) if total else 0.0


def score(rec: dict, gt: str) -> dict:
    hyp_src, _ = parse_output(rec.get("raw_output", ""), hit_cap=rec.get("finish") == "length")
    out = {}
    for level in LEVELS:
        ref, hyp = normalize(gt, level), normalize(hyp_src, level)
        if not ref:
            continue
        hyp = hyp or "∅"
        out[f"cer_{level}"] = jiwer.cer(ref, hyp)
        out[f"wer_{level}"] = jiwer.wer(ref, hyp)
    out["word_f1_lenient"] = word_f1(normalize(gt, "lenient"), normalize(hyp_src, "lenient"))
    return out


def md_table(headers: list[str], rows: list[list]) -> str:
    def cell(v):
        if isinstance(v, float):
            return f"{v:.3f}" if abs(v) < 10 else f"{v:.1f}"
        return "" if v is None else str(v)
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(cell(v) for v in r) + " |")
    return "\n".join(lines)


def mean(xs):
    xs = [x for x in xs if x is not None]
    return st.mean(xs) if xs else None


def main() -> None:
    pages = load_manifest(config.MANIFEST)
    page_by_id = {p["id"]: p for p in pages}
    runs = load_runs()
    gt = load_gt()
    prep = load_prep()
    if not runs:
        raise SystemExit("no runs in runs/. Run run_ocr.py first.")

    rows = []
    for rec in runs:
        row = {"run_id": rec.get("run_id"), "page_id": rec.get("page_id"), "sample": rec.get("sample"),
               "engine": rec.get("engine"), "variant": rec.get("variant"), "backend": rec.get("backend"),
               "status": rec.get("status"), "duration_s": rec.get("duration_s"), "output_tokens": rec.get("output_tokens"),
               "finish": rec.get("finish"), "has_gt": rec.get("page_id") in gt}
        if rec.get("status") == "ok":
            parsed, looped = parse_output(rec.get("raw_output", ""), hit_cap=rec.get("finish") == "length")
            row["looped"] = looped or rec.get("finish") == "length"
            row["n_ocr_lines"] = len(parsed.splitlines())
            row["n_img_lines"] = prep.get(rec["page_id"], {}).get("n_lines")
            row["lines_match"] = (row["n_img_lines"] is not None and row["n_ocr_lines"] == row["n_img_lines"])
            row["chars"] = len(parsed)
            if rec["page_id"] in gt:
                row.update(score(rec, gt[rec["page_id"]]))
        rows.append(row)

    # ---------------------------------------------------------------- csv
    fields = ["run_id", "page_id", "sample", "engine", "variant", "backend", "status", "duration_s", "output_tokens",
              "finish", "looped", "chars", "n_ocr_lines", "n_img_lines", "lines_match", "has_gt", "word_f1_lenient"] + \
             [f"{m}_{lvl}" for lvl in LEVELS for m in ("cer", "wer")]
    with open(config.POC / "runs_summary.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    ok = [r for r in rows if r["status"] == "ok"]
    scored = [r for r in ok if r.get("cer_raw") is not None]

    def group(rows_, keys):
        g = defaultdict(list)
        for r in rows_:
            g[tuple(r[k] for k in keys)].append(r)
        return g

    md = [f"# Phase 1 OCR report", f"_generated {dt.datetime.now():%Y-%m-%d %H:%M}_", "",
          f"Runs: {len(rows)} total, {len(ok)} ok, {len(rows) - len(ok)} error. Pages with ground truth: "
          f"{len(gt)}/{len(pages)}. Scored runs: {len(scored)}.", ""]

    # 1. overall by engine x variant x backend
    md += ["## 1. Accuracy by engine, input variant and backend", "",
           "CER = character error rate (lower is better). `raw` counts diacritic errors; `no_tashkeel` ignores "
           "diacritics; `lenient` also folds alef/ya/ta-marbuta, digits and punctuation.", ""]
    rows1 = []
    for (eng, var, be), rs in sorted(group(ok, ["engine", "variant", "backend"]).items()):
        sc = [r for r in rs if r.get("cer_raw") is not None]
        rows1.append([eng, var, be, len(rs), len(sc), mean([r.get("cer_raw") for r in sc]),
                      mean([r.get("cer_no_tashkeel") for r in sc]), mean([r.get("cer_lenient") for r in sc]),
                      mean([r.get("wer_lenient") for r in sc]), mean([r.get("word_f1_lenient") for r in sc]),
                      mean([r["duration_s"] for r in rs]), sum(bool(r.get("looped")) for r in rs)])
    md.append(md_table(["engine", "variant", "backend", "runs", "scored", "CER raw", "CER no tashkeel", "CER lenient",
                        "WER lenient", "word F1", "mean s/page", "loops"], rows1))
    md += ["", "`word F1` ignores word order (fair for tables and reordered blocks); 1.0 = every word present."]

    # 2. per sample x engine (best variant by lenient CER)
    md += ["", "## 2. Accuracy per sample (best variant per engine, lenient CER)", ""]
    rows2 = []
    for (smp, eng), rs in sorted(group(scored, ["sample", "engine"]).items()):
        by_var = group(rs, ["variant"])
        best = min(by_var.items(), key=lambda kv: mean([r["cer_lenient"] for r in kv[1]]))
        rows2.append([smp, config.SAMPLES.get(smp, {}).get("note", ""), eng, best[0][0],
                      mean([r["cer_lenient"] for r in best[1]]), mean([r["cer_raw"] for r in best[1]]), len(best[1])])
    md.append(md_table(["sample", "what", "engine", "best variant", "CER lenient", "CER raw", "pages"], rows2))

    # 3. per page for the best overall engine/variant
    if scored:
        best_combo = min(group(scored, ["engine", "variant", "backend"]).items(),
                         key=lambda kv: mean([r["cer_lenient"] for r in kv[1]]))
        (beng, bvar, bbe), brs = best_combo
        md += ["", f"## 3. Per page for the best combination: {beng} / {bvar} / {bbe}", ""]
        rows3 = [[r["page_id"], r["cer_raw"], r["cer_no_tashkeel"], r["cer_lenient"], r["wer_lenient"],
                  r.get("word_f1_lenient"), r["duration_s"], "yes" if r.get("looped") else "no"]
                 for r in sorted(brs, key=lambda r: r["page_id"])]
        md.append(md_table(["page", "CER raw", "CER no tashkeel", "CER lenient", "WER lenient", "word F1", "s", "loop"], rows3))

    # 4. backend comparison (same engine/variant/page)
    pairs = []
    by_key = group(ok, ["page_id", "engine", "variant"])
    for key, rs in by_key.items():
        bes = {r["backend"]: r for r in rs}
        if "torch" in bes and "mlx" in bes:
            t, m = bes["torch"], bes["mlx"]
            pairs.append([key[0], key[1], key[2], t["duration_s"], m["duration_s"],
                          (t["duration_s"] / m["duration_s"]) if m["duration_s"] else None,
                          t.get("cer_lenient"), m.get("cer_lenient")])
    if pairs:
        md += ["", "## 4. Backend comparison: PyTorch MPS vs MLX (same page, engine, variant)", "",
               md_table(["page", "engine", "variant", "torch s", "mlx s", "speed-up", "CER torch", "CER mlx"], pairs),
               "", f"Mean speed-up: {mean([p[5] for p in pairs]):.2f}x; mean CER torch {mean([p[6] for p in pairs])}, "
               f"mlx {mean([p[7] for p in pairs])}"]

    # 5. upscaling for low-res pages
    ups = []
    for key, rs in group([r for r in scored if page_by_id.get(r["page_id"], {}).get("low_res")], ["page_id", "engine", "backend"]).items():
        vs = {r["variant"]: r for r in rs}
        if "gray" in vs and "gray_2x" in vs:
            ups.append([key[0], key[1], vs["gray"]["cer_lenient"], vs["gray_2x"]["cer_lenient"],
                        vs["gray_2x"]["cer_lenient"] - vs["gray"]["cer_lenient"]])
    if ups:
        md += ["", "## 5. Low-resolution pages: grayscale vs 2x upscaled (lenient CER, negative delta = upscaling helped)", "",
               md_table(["page", "engine", "CER gray", "CER gray_2x", "delta"], ups)]

    # 6. line alignment feasibility
    md += ["", "## 6. Line alignment: OCR line count vs detected image lines", "",
           "Qari v0.2 returns the page as one paragraph and v0.3's tags do not follow visual lines, so this "
           "table mostly documents that line linking in the review screen must come from image geometry, "
           "not from OCR line breaks.", ""]
    rows6 = []
    for (eng, var), rs in sorted(group(ok, ["engine", "variant"]).items()):
        with_lines = [r for r in rs if r.get("n_img_lines") is not None]
        rows6.append([eng, var, len(with_lines), sum(bool(r.get("lines_match")) for r in with_lines),
                      mean([abs(r["n_ocr_lines"] - r["n_img_lines"]) for r in with_lines])])
    md.append(md_table(["engine", "variant", "pages", "exact match", "mean |diff|"], rows6))

    # 7. coverage
    md += ["", "## 7. Grid coverage", ""]
    rows7 = []
    for (eng, be), rs in sorted(group(rows, ["engine", "backend"]).items()):
        rows7.append([eng, be, len(rs), sum(r["status"] == "ok" for r in rs), sum(r["status"] != "ok" for r in rs),
                      len({r["page_id"] for r in rs})])
    md.append(md_table(["engine", "backend", "runs", "ok", "errors", "pages"], rows7))
    errors = [r for r in rows if r["status"] != "ok"]
    if errors:
        md += ["", "Errors:", ""] + [f"- `{r['run_id']}`" for r in errors[:30]]

    notes = config.POC / "notes.md"
    md += ["", "## 8. Findings and decisions", ""]
    if notes.exists():
        md.append(notes.read_text(encoding="utf-8").strip())
    else:
        md.append("_Write qualitative findings in `notes.md`; they are inserted here on every `make report`._")
    md.append("")
    config.REPORT.write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {config.REPORT} and runs_summary.csv ({len(rows)} runs, {len(scored)} scored)")


if __name__ == "__main__":
    main()
