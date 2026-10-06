"""`manage.py prepare_models`: download the two Qari models from Hugging Face, ready for a local backend.

What the local engines (`ocr.engines.qari_torch`, `qari_mlx`) read, under OCR_MODELS_DIR (`ocr.engines.qari`):

    qari-v0.3/          NAMAA-Space/Qari-OCR-v0.3-VL-2B-Instruct, full weights (4.4 GB), used as they are
    qari-v0.2-merged/   NAMAA-Space/Qari-OCR-0.2.2.1-VL-2B-Instruct is a LoRA adapter (0.12 GB) whose declared
                        base is a 4-bit bitsandbytes model that runs on CUDA only; it is merged here onto the
                        full-precision base Qwen/Qwen2-VL-2B-Instruct (4.4 GB): W += scale * B @ A
    mlx/qari-v0.3/      with --mlx (OCR_BACKEND=mlx, Apple Silicon): the two folders above converted
    mlx/qari-v0.2/      by mlx-vlm, without quantisation (4.4 GB each)

Every download goes to the Hugging Face cache (HF_HOME, default ~/.cache/huggingface); v0.3's weights are then
hard-linked into its folder (copied when the cache is on another disk). The cache may be deleted once the
models are ready. Every step is resumable: a finished folder holds a `.complete` marker and is skipped on the
next run. The same procedure as playground/poc/prepare_models.py (the PoC), without the PoC.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ocr.engines.qari import QARI_MODELS

BASE_MODEL = "Qwen/Qwen2-VL-2B-Instruct"
ADAPTERS = {"qari_v02"}  # the engines whose Hugging Face repository is a LoRA adapter on BASE_MODEL
# the weights, configs and tokenizer; not README, training_args.bin or other leftovers
PATTERNS = ["*.json", "*.safetensors", "*.txt", "*.jinja"]
WRAPPERS = ("base_model", "model", "language_model", "modules_to_save", "default")


def canonical(name: str) -> str:
    """A parameter name without its wrapper prefixes, so adapter and base names can be compared."""
    parts = name.split(".")
    while parts and parts[0] in WRAPPERS:
        parts.pop(0)
    return ".".join(parts)


def is_complete(folder: Path) -> bool:
    return (folder / ".complete").exists()


def mark_complete(folder: Path, info: dict) -> None:
    (folder / "nassakh_info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n")
    (folder / ".complete").write_text("ok\n")


class Command(BaseCommand):
    help = "Download Qari v0.3 and v0.2 from Hugging Face into OCR_MODELS_DIR (--mlx: also convert for MLX)."
    requires_system_checks: list = []

    def add_arguments(self, parser) -> None:
        parser.add_argument("--engines", default="qari_v03,qari_v02", help="comma separated engine names")
        parser.add_argument(
            "--mlx", action="store_true", help="also convert for Apple MLX (the default when OCR_BACKEND=mlx)"
        )
        parser.add_argument("--dtype", default="bfloat16", choices=("bfloat16", "float16"))

    def handle(self, *args, **options) -> None:
        root = Path(settings.NASSAKH["OCR_MODELS_DIR"])
        mlx = options["mlx"] or str(settings.NASSAKH["OCR_BACKEND"]).lower() == "mlx"
        engines = [e.strip() for e in options["engines"].split(",") if e.strip()]
        unknown = [e for e in engines if e not in QARI_MODELS]
        if unknown:
            raise CommandError(f"unknown engine(s) {', '.join(unknown)}; known: {', '.join(QARI_MODELS)}")
        root.mkdir(parents=True, exist_ok=True)
        self.stdout.write(f"models folder: {root}")
        started = time.monotonic()
        for name in engines:
            spec = QARI_MODELS[name]
            local = root / spec["torch_dir"]
            self.stdout.write(f"\n== {name} ({spec['hf_id']})")
            if is_complete(local):
                self.stdout.write(f"  {local} is ready, skip")
            elif name in ADAPTERS:
                self._merge(spec["hf_id"], local, options["dtype"])
            else:
                self._download(spec["hf_id"], local)
            if mlx:
                self._convert_mlx(local, root / "mlx" / spec["mlx_dir"])
        self.stdout.write(f"\nmodels ready in {time.monotonic() - started:.0f} s")

    # ---------------------------------------------------------------------------------------------- steps

    def _snapshot(self, repo_id: str) -> Path:
        """The repository's files in the Hugging Face cache (downloaded once; a second run only checks)."""
        from huggingface_hub import snapshot_download

        self.stdout.write(f"  downloading {repo_id} to the Hugging Face cache ...")
        self.stdout.flush()
        return Path(snapshot_download(repo_id, allow_patterns=PATTERNS))

    def _download(self, repo_id: str, local: Path) -> None:
        """A model with full weights: downloaded to the cache, then its files placed in the folder.

        The weights are hard-linked when the cache is on the same disk (no second copy), else copied; the
        small files are copied without the cache's read-only mode (mlx-vlm's converter rewrites them).
        """
        snapshot = self._snapshot(repo_id)
        local.mkdir(parents=True, exist_ok=True)
        for source in snapshot.iterdir():
            if source.name.startswith(".") or not source.is_file():
                continue
            target = local / source.name
            if target.is_symlink() or target.exists():
                target.unlink()
            real = source.resolve()
            if source.suffix == ".safetensors":
                try:
                    os.link(real, target)
                    continue
                except OSError:
                    pass  # another disk: copy
            shutil.copyfile(real, target)
            os.chmod(target, 0o644)
        mark_complete(local, {"kind": "full", "repo": repo_id, "revision": snapshot.name})
        self.stdout.write(f"  {local} ready")

    def _merge(self, adapter_id: str, local: Path, dtype_name: str) -> None:
        """A LoRA adapter: merged onto the full-precision base on the CPU and saved as a whole model."""
        import torch
        from safetensors.torch import load_file
        from transformers import AutoProcessor, Qwen2VLForConditionalGeneration

        base_dir = self._snapshot(BASE_MODEL)
        adapter_dir = self._snapshot(adapter_id)
        cfg = json.loads((adapter_dir / "adapter_config.json").read_text())
        if cfg.get("use_dora"):
            raise CommandError("DoRA adapters are not supported")
        r, alpha = cfg["r"], cfg["lora_alpha"]
        scaling = alpha / math.sqrt(r) if cfg.get("use_rslora") else alpha / r

        self.stdout.write(f"  loading the base in {dtype_name} on the CPU and merging ...")
        self.stdout.flush()
        model = Qwen2VLForConditionalGeneration.from_pretrained(base_dir, dtype=getattr(torch, dtype_name))
        params = dict(model.named_parameters())
        by_canonical: dict[str, str] = {}
        for pname in params:
            by_canonical.setdefault(canonical(pname), pname)

        state = load_file(str(adapter_dir / "adapter_model.safetensors"))
        pairs: dict[str, dict] = {}
        direct: dict = {}
        for key, tensor in state.items():
            if ".lora_A." in key:
                pairs.setdefault(key.split(".lora_A.")[0], {})["A"] = tensor
            elif ".lora_B." in key:
                pairs.setdefault(key.split(".lora_B.")[0], {})["B"] = tensor
            else:
                direct[key] = tensor

        merged, problems = 0, []
        with torch.no_grad():
            for module, ab in pairs.items():
                target = by_canonical.get(canonical(module) + ".weight")
                if "A" not in ab or "B" not in ab or target is None:
                    problems.append(f"unmatched {module}")
                    continue
                weight = params[target]
                delta = (ab["B"].float() @ ab["A"].float()) * scaling
                if delta.shape != weight.shape:
                    problems.append(f"shape {module}: {tuple(delta.shape)} vs {tuple(weight.shape)}")
                    continue
                weight.add_(delta.to(weight.dtype))
                merged += 1
            for key, tensor in direct.items():
                target = by_canonical.get(canonical(key))
                if target is None or params[target].shape != tensor.shape:
                    problems.append(f"unmatched {key}")
                    continue
                params[target].copy_(tensor.to(params[target].dtype))
                merged += 1
        if problems:
            raise CommandError("the adapter could not be fully merged: " + "; ".join(problems[:10]))
        self.stdout.write(f"  merged {merged} tensors (scale {scaling})")

        local.mkdir(parents=True, exist_ok=True)
        model.save_pretrained(local, safe_serialization=True)
        AutoProcessor.from_pretrained(base_dir).save_pretrained(local)
        self._legacy_config(local, base_dir, dtype_name)
        mark_complete(
            local,
            {
                "kind": "merged_lora",
                "base": BASE_MODEL,
                "base_revision": base_dir.name,
                "adapter": adapter_id,
                "adapter_revision": adapter_dir.name,
                "dtype": dtype_name,
                "merged_tensors": merged,
                "scaling": scaling,
            },
        )
        self.stdout.write(f"  {local} ready")

    def _legacy_config(self, model_dir: Path, base_dir: Path, dtype_name: str) -> None:
        """config.json in the flat Qwen2-VL layout: transformers 5 saves a nested one mlx-vlm cannot read."""
        cfg_path = model_dir / "config.json"
        cfg = json.loads(cfg_path.read_text())
        if "text_config" not in cfg:
            return
        legacy = json.loads((base_dir / "config.json").read_text())
        legacy["torch_dtype"] = dtype_name
        legacy["architectures"] = cfg.get("architectures", legacy.get("architectures"))
        legacy.pop("_name_or_path", None)
        cfg_path.write_text(json.dumps(legacy, indent=2, ensure_ascii=False) + "\n")

    def _convert_mlx(self, source: Path, target: Path) -> None:
        if is_complete(target):
            self.stdout.write(f"  mlx: {target} is ready, skip")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        cmd = [sys.executable, "-m", "mlx_vlm", "convert", "--hf-path", str(source)]
        cmd += ["--mlx-path", str(target)]
        self.stdout.write("  " + " ".join(cmd))
        self.stdout.flush()
        if subprocess.run(cmd, env={**os.environ, "TOKENIZERS_PARALLELISM": "false"}).returncode != 0:
            raise CommandError("the MLX conversion failed (is the `mlx` extra installed?)")
        mark_complete(target, {"source": str(source), "quantized": False})
        self.stdout.write(f"  {target} ready")
