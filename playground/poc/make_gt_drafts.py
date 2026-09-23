"""Create ground-truth drafts from the best available OCR run, and promote corrected drafts.

  python make_gt_drafts.py            # write gt/drafts/<page_id>.txt for pages without one
  python make_gt_drafts.py --promote s1_p006 s4_p003   # copy corrected drafts to gt/<page_id>.txt
  python make_gt_drafts.py --promote all

Drafts are never overwritten (manual corrections live there). The evaluation
only uses files in gt/, so nothing is measured before a draft is promoted.
"""
from __future__ import annotations

import argparse
import shutil

import config
from common import load_manifest, read_json

PREFERENCE = ["qari_v02", "qari_v03", "qari_kitab", "tesseract"]


def best_run_text(page_id: str) -> tuple[str, str] | None:
    for eng in PREFERENCE:
        for variant in ("gray", "gray_2x", "bw"):
            for backend in ("torch", "mlx", "cpu"):
                p = config.RUNS / f"{page_id}__{eng}__{variant}__{backend}.json"
                if p.exists():
                    rec = read_json(p)
                    if rec.get("status") == "ok" and rec.get("parsed_text", "").strip():
                        return rec["parsed_text"], p.name
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--promote", nargs="*", help="page ids to copy from gt/drafts/ to gt/ ('all' for every draft)")
    args = ap.parse_args()
    drafts = config.GT / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)

    if args.promote is not None:
        ids = args.promote
        if ids == ["all"] or not ids:
            ids = [p.stem for p in drafts.glob("*.txt") if p.stem != "s3_repair_report"]
        for pid in ids:
            src = drafts / f"{pid}.txt"
            if not src.exists():
                print(f"{pid}: no draft")
                continue
            shutil.copyfile(src, config.GT / f"{pid}.txt")
            print(f"{pid}: promoted -> gt/{pid}.txt")
        return

    for entry in load_manifest(config.MANIFEST):
        out = drafts / f"{entry['id']}.txt"
        if out.exists():
            print(f"{entry['id']:12s} draft exists, kept")
            continue
        found = best_run_text(entry["id"])
        if found is None:
            print(f"{entry['id']:12s} no OCR run yet")
            continue
        text, source = found
        out.write_text(text + "\n", encoding="utf-8")
        print(f"{entry['id']:12s} draft from {source}")


if __name__ == "__main__":
    main()
