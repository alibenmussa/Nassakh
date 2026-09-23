"""Step 4: run the OCR experiment grid. Resumable.

The grid = pages x engines x image variants x backend. Every finished run is
written immediately to runs/<run_id>.json, so you can stop at any time
(Ctrl+C, or --max-minutes) and continue later with the same command: runs
that already have a JSON are skipped.

Examples
  python run_ocr.py --dry-run                      # show the grid and what is done
  python run_ocr.py --quick                        # 6 pages, gray only, v0.2 + v0.3
  python run_ocr.py --max-minutes 120              # work for 2 hours, then stop cleanly
  python run_ocr.py --engines qari_v02 --backend mlx
  python run_ocr.py --self-test                    # tiny random model: checks the pipeline on MPS
"""
from __future__ import annotations

import argparse
import datetime as dt
import platform
import sys
import time
import traceback
from pathlib import Path

import config
from common import fmt_secs, load_manifest, parse_output, read_json, write_json_atomic


def run_id(page_id: str, engine: str, variant: str, backend: str) -> str:
    return f"{page_id}__{engine}__{variant}__{backend}"


def plan(pages: list[dict], engines: list[str], variants: list[str], backend: str) -> list[dict]:
    """Ordered list of runs: grouped by engine so each model loads once."""
    rp = config.GEN.get("repetition_penalty", 1.0)
    runs = []
    for eng in engines:
        spec = config.ENGINES[eng]
        be = "cpu" if spec["kind"] == "tesseract" else backend
        for page in pages:
            for var in variants:
                if var == "gray_2x" and not page.get("low_res"):
                    continue
                if var == "regions":
                    img = config.REGIONS / f"{page['id']}_body.png"
                    if not img.exists():
                        continue  # no footnote rule on this page
                else:
                    img = config.VARIANTS[var] / f"{page['id']}.png"
                label = var + (f"+rp{rp}" if rp != 1.0 and spec["kind"] == "qwen2vl" else "")
                runs.append({"run_id": run_id(page["id"], eng, label, be), "page_id": page["id"], "sample": page["sample"],
                             "engine": eng, "variant": label, "backend": be, "image": img})
    return runs


def recognize_run(engine, r: dict):
    """Run one grid cell; the regions variant OCRs body and footnotes separately and joins them."""
    if not r["variant"].startswith("regions"):
        res = engine.recognize(r["image"])
        return res, None
    parts = []
    texts = []
    for region in ("body", "foot"):
        img = config.REGIONS / f"{r['page_id']}_{region}.png"
        if not img.exists():
            continue
        res = engine.recognize(img, max_new_tokens=config.REGION_MAX_TOKENS[region])
        parts.append({"region": region, "image": str(img.relative_to(config.POC)), "text": res.text,
                      "duration_s": round(res.duration_s, 2), "output_tokens": res.output_tokens, "finish": res.finish,
                      "image_size": res.image_size, "resized_to": res.resized_to})
        texts.append(res.text)
    from engines import OcrResult
    joined = OcrResult(text="\n\n".join(texts), duration_s=sum(p["duration_s"] for p in parts),
                       prompt_tokens=None, output_tokens=sum((p["output_tokens"] or 0) for p in parts),
                       finish="length" if any(p["finish"] == "length" for p in parts) else "stop",
                       image_size=parts[0]["image_size"] if parts else None, resized_to=None, extra={})
    return joined, parts


def status_of(run: dict) -> str:
    p = config.RUNS / f"{run['run_id']}.json"
    if not p.exists():
        return "pending"
    return read_json(p).get("status", "error")


def model_info(engine: str, backend: str) -> dict:
    spec = config.ENGINES[engine]
    if spec["kind"] == "tesseract":
        return {"model_id": f"tesseract:{spec['lang']}", "model_path": None, "model_revision": None}
    d: Path = spec["mlx"] if backend == "mlx" else spec["local"]
    info_path = d / "nassakh_info.json"
    if not info_path.exists():
        info_path = d.parent / f"{d.name}.nassakh_info.json"
    info = read_json(info_path) if info_path.exists() else {}
    return {"model_id": spec["hf_id"], "model_path": str(d),
            "model_revision": info.get("revision") or info.get("adapter_revision"), "model_info": info}


def execute(runs: list[dict], max_minutes: float, dry_run: bool) -> None:
    from engines import build_engine

    todo = [r for r in runs if r["_status"] == "pending"]
    print(f"grid: {len(runs)} runs | done: {sum(r['_status'] == 'ok' for r in runs)} | "
          f"errors: {sum(r['_status'] == 'error' for r in runs)} | to do: {len(todo)}")
    if dry_run or not todo:
        for r in runs:
            print(f"  {r['_status']:8s} {r['run_id']}")
        return

    missing = [r for r in todo if not r["image"].exists()]
    if missing:
        print(f"{len(missing)} runs skipped: image missing (run preprocess.py first), e.g. {missing[0]['image']}")
        todo = [r for r in todo if r["image"].exists()]

    t_start = time.time()
    budget = max_minutes * 60 if max_minutes else None
    done_now = 0
    current_engine, engine = None, None
    try:
        for i, r in enumerate(todo, 1):
            if budget and (time.time() - t_start) > budget:
                print(f"\ntime budget reached after {done_now} runs. {len(todo) - i + 1} remain; rerun the same command to resume.")
                break
            key = (r["engine"], r["backend"])
            if key != current_engine:
                if engine is not None:
                    engine.unload()
                print(f"\n== loading {r['engine']} [{r['backend']}]", flush=True)
                engine = build_engine(r["engine"], r["backend"])
                engine.load()
                current_engine = key
            elapsed = time.time() - t_start
            eta = (elapsed / done_now * (len(todo) - i + 1)) if done_now else 0
            print(f"[{i}/{len(todo)}] {r['run_id']}  elapsed {fmt_secs(elapsed)}"
                  + (f"  eta ~{fmt_secs(eta)}" if eta else ""), flush=True)
            record = {**{k: v for k, v in r.items() if not k.startswith('_')}, "image": str(r["image"].relative_to(config.POC)),
                      **model_info(r["engine"], r["backend"]),
                      "prompt": config.ENGINES[r["engine"]].get("prompt"),
                      "params": dict(config.GEN) if config.ENGINES[r["engine"]]["kind"] == "qwen2vl" else {},
                      "host": f"{platform.system()} {platform.machine()} {platform.mac_ver()[0]}".strip(),
                      "created_at": dt.datetime.now().isoformat(timespec="seconds")}
            try:
                res, parts = recognize_run(engine, r)
                parsed, looped = parse_output(res.text, hit_cap=res.finish == "length")
                record.update({"status": "ok", "raw_output": res.text, "parsed_text": parsed,
                               "looped": looped or res.finish == "length",
                               "duration_s": round(res.duration_s, 2), "prompt_tokens": res.prompt_tokens,
                               "output_tokens": res.output_tokens, "finish": res.finish,
                               "image_size": res.image_size, "resized_to": res.resized_to, "extra": res.extra})
                if parts is not None:
                    record["parts"] = parts
                print(f"     {res.duration_s:6.1f}s  {res.output_tokens or '?'} tokens  finish={res.finish}"
                      f"{'  LOOP' if record['looped'] else ''}  {len(parsed)} chars", flush=True)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - record and continue with the next run
                record.update({"status": "error", "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()})
                print(f"     ERROR {record['error']}", flush=True)
            write_json_atomic(config.RUNS / f"{r['run_id']}.json", record)
            done_now += 1
    except KeyboardInterrupt:
        print(f"\ninterrupted. {done_now} runs saved this session; rerun the same command to resume.")
        sys.exit(130)
    finally:
        if engine is not None:
            engine.unload()
    print(f"\nsession: {done_now} runs in {fmt_secs(time.time() - t_start)}")


def self_test() -> None:
    """Build a tiny random Qwen2-VL with the real processor and run one page through it on MPS."""
    import torch
    from transformers import AutoProcessor, Qwen2VLConfig, Qwen2VLForConditionalGeneration

    from engines import TorchQwenEngine

    print("self-test: downloading processor files for", config.BASE_MODEL)
    processor = AutoProcessor.from_pretrained(config.BASE_MODEL)
    cfg = Qwen2VLConfig.from_pretrained(config.BASE_MODEL)
    tc = getattr(cfg, "text_config", cfg)
    tc.hidden_size, tc.intermediate_size, tc.num_hidden_layers = 256, 512, 2
    tc.num_attention_heads, tc.num_key_value_heads = 2, 2
    vc = cfg.vision_config
    vc.depth, vc.embed_dim, vc.num_heads, vc.hidden_size = 2, 64, 4, 256
    torch.manual_seed(0)
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    dtype = getattr(torch, config.GEN["dtype"])
    model = Qwen2VLForConditionalGeneration(cfg).to(dtype).to(device)
    gen = {**config.GEN, "max_new_tokens": 8}
    eng = TorchQwenEngine(Path("tiny-random"), config.PROMPT_QARI, gen, device=device)
    eng.adopt(model, processor)
    pages = load_manifest(config.MANIFEST)
    img = config.GRAY / f"{pages[0]['id']}.png"
    if not img.exists():
        img = config.POC / pages[0]["path"]
    res = eng.recognize(img)
    print(f"self-test OK on {device}: image {res.image_size} -> {res.resized_to}, prompt tokens {res.prompt_tokens}, "
          f"generated {res.output_tokens} tokens in {res.duration_s:.1f}s (random text: {res.text[:40]!r})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engines", default=",".join(config.DEFAULT_ENGINES))
    ap.add_argument("--variants", default=",".join(config.DEFAULT_VARIANTS) + ",gray_2x",
                    help="gray,bw,regions,gray_2x (regions: pages with a footnote rule; gray_2x: low-res pages)")
    ap.add_argument("--backend", default=config.DEFAULT_BACKEND, choices=["torch", "mlx"])
    ap.add_argument("--pages", help="comma-separated page ids")
    ap.add_argument("--quick", action="store_true", help="6 pages, gray only, qari_v02 + qari_v03")
    ap.add_argument("--max-minutes", type=float, default=0, help="stop cleanly after this many minutes (0 = no limit)")
    ap.add_argument("--dry-run", action="store_true", help="print the grid and its status, run nothing")
    ap.add_argument("--retry-errors", action="store_true", help="redo runs that ended in error")
    ap.add_argument("--force", action="store_true", help="redo everything selected")
    ap.add_argument("--max-pixels", type=int)
    ap.add_argument("--max-new-tokens", type=int)
    ap.add_argument("--repetition-penalty", type=float, help="e.g. 1.1; default off (1.0)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.max_pixels:
        config.GEN["max_pixels"] = args.max_pixels
    if args.max_new_tokens:
        config.GEN["max_new_tokens"] = args.max_new_tokens
    if args.repetition_penalty:
        config.GEN["repetition_penalty"] = args.repetition_penalty
    if args.self_test:
        self_test()
        return

    pages = load_manifest(config.MANIFEST)
    engines = args.engines.split(",")
    variants = args.variants.split(",")
    if args.quick:
        pages = [p for p in pages if p["id"] in config.QUICK_PAGES]
        engines = [e for e in ("qari_v02", "qari_v03") if e in engines] or ["qari_v02", "qari_v03"]
        variants = ["gray", "regions"]
    if args.pages:
        wanted = set(args.pages.split(","))
        pages = [p for p in pages if p["id"] in wanted]
    for e in engines:
        if e not in config.ENGINES:
            raise SystemExit(f"unknown engine {e}; choose from {list(config.ENGINES)}")

    runs = plan(pages, engines, variants, args.backend)
    for r in runs:
        st = status_of(r)
        if args.force or (args.retry_errors and st == "error"):
            st = "pending"
        r["_status"] = st
    config.RUNS.mkdir(exist_ok=True)
    execute(runs, args.max_minutes, args.dry_run)


if __name__ == "__main__":
    main()
