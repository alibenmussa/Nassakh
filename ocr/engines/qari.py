"""Shared pieces of the two Qari backends: model directories, metadata, generation settings."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from django.conf import settings

from .base import OcrEngine
from .prompts import PROMPT_QARI

log = logging.getLogger(__name__)

# Engine name → (torch model directory, MLX model directory, Hugging Face id).
QARI_MODELS: dict[str, dict[str, str]] = {
    "qari_v03": {
        "torch_dir": "qari-v0.3",
        "mlx_dir": "qari-v0.3",
        "hf_id": "NAMAA-Space/Qari-OCR-v0.3-VL-2B-Instruct",
    },
    "qari_v02": {
        "torch_dir": "qari-v0.2-merged",
        "mlx_dir": "qari-v0.2",
        "hf_id": "NAMAA-Space/Qari-OCR-0.2.2.1-VL-2B-Instruct",
    },
}


def nassakh_settings() -> dict:
    """The `settings.NASSAKH` dict (read at call time so tests can override it)."""
    return settings.NASSAKH


def model_dir_for(name: str, backend: str) -> Path:
    """Directory of the prepared weights for engine `name` on `backend` (`torch` | `mlx`)."""
    spec = QARI_MODELS[name]
    root = Path(nassakh_settings()["OCR_MODELS_DIR"])
    if backend == "mlx":
        return root / "mlx" / spec["mlx_dir"]
    return root / spec["torch_dir"]


def read_model_info(model_dir: Path) -> dict:
    """`nassakh_info.json` written by `prepare_models.py` next to the weights (empty when missing)."""
    for candidate in (
        model_dir / "nassakh_info.json",
        model_dir.parent / f"{model_dir.name}.nassakh_info.json",
    ):
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("unreadable model info %s", candidate)
    return {}


def resolve_device(setting: str | None = None) -> str:
    """`mps` when available else `cpu`; an explicit `mps`/`cpu` setting is returned as is."""
    wanted = (setting or nassakh_settings().get("OCR_DEVICE") or "auto").lower()
    if wanted in ("mps", "cpu"):
        return wanted
    try:
        import torch

        return "mps" if torch.backends.mps.is_available() else "cpu"
    except Exception:  # noqa: BLE001 - torch missing or broken: fall back to CPU
        return "cpu"


def max_new_tokens_for(kind: str) -> int:
    """Token cap per region kind (`page`, `body`, `footnote`, others → `other`)."""
    caps = nassakh_settings()["MAX_NEW_TOKENS"]
    return int(caps.get(kind, caps.get("other", 600)))


class QariEngine(OcrEngine):
    """Common state of a Qari engine: paths, pixel budget, metadata. Subclasses do the inference."""

    kind = "vlm"
    prompt = PROMPT_QARI

    def __init__(self, name: str, model_dir: Path | None = None) -> None:
        if name not in QARI_MODELS:
            raise ValueError(f"unknown Qari engine {name!r}")
        cfg = nassakh_settings()
        self.name = name
        self.model_dir = Path(model_dir) if model_dir else model_dir_for(name, self.backend)
        self.max_pixels = int(cfg["MAX_PIXELS"])
        self.min_pixels = int(cfg["MIN_PIXELS"])
        self.default_max_new_tokens = max_new_tokens_for("page")
        self._info = read_model_info(self.model_dir)

    @property
    def model_id(self) -> str:
        return QARI_MODELS[self.name]["hf_id"]

    @property
    def model_revision(self) -> str:
        info = self._info
        return str(info.get("revision") or info.get("adapter_revision") or "")[:64]

    def generation_params(self, max_new_tokens: int) -> dict:
        """Parameters recorded on the OcrRun (identical across backends)."""
        return {
            "max_new_tokens": max_new_tokens,
            "max_pixels": self.max_pixels,
            "min_pixels": self.min_pixels,
            "repetition_penalty": 1.0,
            "model_dir": str(self.model_dir),
        }

    def is_prepared(self) -> bool:
        """True when the weights directory looks complete."""
        return (self.model_dir / "config.json").exists()
