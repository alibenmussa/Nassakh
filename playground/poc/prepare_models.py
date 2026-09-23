"""Step 3: download the models and prepare local weights.

Qari v0.2.2.1 and the KITAB checkpoint are LoRA *adapters* whose declared base
is a 4-bit bitsandbytes model that cannot run on a Mac. We therefore:
  1. download the full-precision base (Qwen/Qwen2-VL-2B-Instruct),
  2. download the adapter,
  3. merge the adapter into the base weights ourselves (W += scale * B @ A),
     matching module names by their canonical suffix so it works across
     transformers versions,
  4. save the merged model under models/<name>.
Qari v0.3 ships full weights and is only linked into models/.

With --mlx each prepared model is also converted for Apple MLX (mlx-vlm),
without quantisation, to protect OCR accuracy.

Everything is resumable: finished steps leave a `.complete` marker and are
skipped on the next run.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import config
from common import write_json_atomic

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

WRAPPERS = ("base_model", "model", "language_model", "modules_to_save", "default")


def canonical(name: str) -> str:
    """Strip wrapper prefixes so adapter and base parameter names can be compared."""
    parts = name.split(".")
    while parts and parts[0] in WRAPPERS:
        parts.pop(0)
    return ".".join(parts)


def snapshot(repo_id: str) -> Path:
    from huggingface_hub import snapshot_download

    print(f"  downloading/checking {repo_id} ...", flush=True)
    return Path(snapshot_download(repo_id))


def revision_of(snapshot_dir: Path) -> str:
    return snapshot_dir.name  # the snapshot folder name is the commit sha


def is_complete(d: Path) -> bool:
    return (d / ".complete").exists()


def mark_complete(d: Path, info: dict) -> None:
    write_json_atomic(d / "nassakh_info.json", info)
    (d / ".complete").write_text("ok\n")


def merge_adapter(base_dir: Path, adapter_dir: Path, out_dir: Path, dtype_name: str) -> dict:
    import torch
    from safetensors.torch import load_file
    from transformers import AutoProcessor, Qwen2VLForConditionalGeneration

    cfg = json.loads((adapter_dir / "adapter_config.json").read_text())
    if cfg.get("use_dora"):
        raise SystemExit("DoRA adapters are not supported by this merge script")
    r, alpha = cfg["r"], cfg["lora_alpha"]
    scaling = alpha / math.sqrt(r) if cfg.get("use_rslora") else alpha / r

    print(f"  loading base {base_dir} in {dtype_name} on CPU ...", flush=True)
    dtype = getattr(torch, dtype_name)
    model = Qwen2VLForConditionalGeneration.from_pretrained(base_dir, dtype=dtype)
    params = dict(model.named_parameters())
    by_canonical: dict[str, str] = {}
    for name in params:
        by_canonical.setdefault(canonical(name), name)

    sd = load_file(str(adapter_dir / "adapter_model.safetensors"))
    pairs: dict[str, dict] = {}
    direct: dict[str, torch.Tensor] = {}
    for key, tensor in sd.items():
        if ".lora_A." in key:
            pairs.setdefault(key.split(".lora_A.")[0], {})["A"] = tensor
        elif ".lora_B." in key:
            pairs.setdefault(key.split(".lora_B.")[0], {})["B"] = tensor
        else:
            direct[key] = tensor

    merged, missing, shape_err = 0, [], []
    with torch.no_grad():
        for module, ab in pairs.items():
            if "A" not in ab or "B" not in ab:
                missing.append(module)
                continue
            target = by_canonical.get(canonical(module) + ".weight")
            if target is None:
                missing.append(module)
                continue
            w = params[target]
            delta = (ab["B"].float() @ ab["A"].float()) * scaling
            if delta.shape != w.shape:
                shape_err.append(f"{module}: {tuple(delta.shape)} vs {tuple(w.shape)}")
                continue
            w.add_(delta.to(w.dtype))
            merged += 1
        for key, tensor in direct.items():
            target = by_canonical.get(canonical(key))
            if target is None or params[target].shape != tensor.shape:
                missing.append(key)
                continue
            params[target].copy_(tensor.to(params[target].dtype))
            merged += 1

    print(f"  merged {merged}/{len(pairs) + len(direct)} tensors (scale={scaling})")
    if missing or shape_err:
        for m in missing[:10]:
            print("   unmatched:", m)
        for s in shape_err[:10]:
            print("   shape mismatch:", s)
        raise SystemExit("adapter could not be fully merged; see messages above")

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"  saving merged model to {out_dir} ...", flush=True)
    model.save_pretrained(out_dir, safe_serialization=True)
    AutoProcessor.from_pretrained(base_dir).save_pretrained(out_dir)
    write_legacy_config(out_dir, base_dir, dtype_name)
    return {"merged_tensors": merged, "scaling": scaling, "r": r, "lora_alpha": alpha}


def write_legacy_config(model_dir: Path, base_dir: Path, dtype_name: str) -> bool:
    """Rewrite config.json in the flat legacy Qwen2-VL layout.

    transformers 5 saves a nested config (text_config / rope_parameters) that
    mlx-vlm's Qwen2-VL loader cannot read; transformers itself still reads the
    legacy layout (the v0.3 repo uses it). Returns True when a rewrite happened.
    """
    cfg_path = model_dir / "config.json"
    cfg = json.loads(cfg_path.read_text())
    if "text_config" not in cfg:
        return False
    legacy = json.loads((base_dir / "config.json").read_text())
    legacy["torch_dtype"] = dtype_name
    legacy["architectures"] = cfg.get("architectures", legacy.get("architectures"))
    legacy.pop("_name_or_path", None)
    cfg_path.write_text(json.dumps(legacy, indent=2, ensure_ascii=False) + "\n")
    return True


def stage_snapshot(snapshot_dir: Path, dst: Path) -> None:
    """Materialise a Hugging Face snapshot as a normal writable folder.

    Small files (config, tokenizer, processor) are copied without the cache's
    read-only mode bits; the multi-GB safetensors files are symlinked. mlx-vlm's
    converter copies the tokenizer into the output folder and then rewrites it,
    which fails on read-only cache files (Permission denied).
    """
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    dst.mkdir(parents=True, exist_ok=True)
    for f in snapshot_dir.iterdir():
        if f.name.startswith("."):
            continue
        target = dst / f.name
        if target.is_symlink() or target.exists():
            target.unlink()
        real = f.resolve()
        if f.suffix == ".safetensors":
            target.symlink_to(real)
        else:
            shutil.copyfile(real, target)
            os.chmod(target, 0o644)


def convert_mlx(src: Path, dst: Path) -> None:
    if is_complete(dst):
        print(f"  mlx: {dst} exists, skip")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)
    cmd = [sys.executable, "-m", "mlx_vlm", "convert", "--hf-path", str(src), "--mlx-path", str(dst)]
    print("  " + " ".join(cmd), flush=True)
    res = subprocess.run(cmd)
    if res.returncode != 0:
        raise SystemExit("mlx conversion failed. Try: python -m mlx_vlm.convert --help")
    mark_complete(dst, {"source": str(src), "quantized": False})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engines", default=",".join(config.QWEN_ENGINES), help="comma-separated engine keys")
    ap.add_argument("--mlx", action="store_true", help="also convert to MLX format")
    ap.add_argument("--dtype", default=config.GEN["dtype"], help="bfloat16 | float16")
    args = ap.parse_args()

    config.MODELS.mkdir(exist_ok=True)
    for key in args.engines.split(","):
        spec = config.ENGINES[key]
        if spec["kind"] != "qwen2vl":
            continue
        local: Path = spec["local"]
        print(f"\n== {key} ({spec['hf_id']})")
        if is_complete(local):
            print(f"  {local} ready, skip")
            if spec["adapter"] and write_legacy_config(local, snapshot(spec["base"]), args.dtype):
                print("  rewrote config.json in the legacy layout (mlx-vlm compatibility)")
        elif spec["adapter"]:
            base_dir = snapshot(spec["base"])
            adapter_dir = snapshot(spec["hf_id"])
            info = merge_adapter(base_dir, adapter_dir, local, args.dtype)
            info.update({"kind": "merged_lora", "base": spec["base"], "base_revision": revision_of(base_dir),
                         "adapter": spec["hf_id"], "adapter_revision": revision_of(adapter_dir), "dtype": args.dtype})
            mark_complete(local, info)
        else:
            snap = snapshot(spec["hf_id"])
            stage_snapshot(snap, local)
            mark_complete(local, {"kind": "full", "repo": spec["hf_id"], "revision": revision_of(snap)})
            print(f"  staged {local} (small files copied, weights linked to {snap})")
        if args.mlx:
            convert_mlx(local, spec["mlx"])
    print("\nmodels ready.")


if __name__ == "__main__":
    main()
