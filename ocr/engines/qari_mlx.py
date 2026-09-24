"""Qari (Qwen2-VL fine-tunes) through Apple MLX (`mlx-vlm`). Port of the PoC engine."""

from __future__ import annotations

import gc
import inspect
import logging
import tempfile
import time
import uuid
from pathlib import Path

from core.images import prepare_image

from .base import OcrResult
from .qari import QariEngine

log = logging.getLogger(__name__)


class QariMlxEngine(QariEngine):
    """MLX backend: the same converted checkpoints, typically 2-4x faster than MPS."""

    backend = "mlx"

    def __init__(self, name: str, model_dir: Path | None = None):
        super().__init__(name, model_dir)
        self.model = None
        self.processor = None
        self.config = None

    @property
    def is_loaded(self) -> bool:
        return self.model is not None

    def is_prepared(self) -> bool:
        return (self.model_dir / ".complete").exists() or super().is_prepared()

    def load(self) -> None:
        if self.model is not None:
            return
        if not self.is_prepared():
            raise FileNotFoundError(
                f"{self.model_dir} is not prepared; "
                f"run playground/poc/prepare_models.py --engines {self.name} --mlx"
            )
        from mlx_vlm import load
        from mlx_vlm.utils import load_config

        t0 = time.time()
        self.model, self.processor = load(str(self.model_dir))
        self.config = load_config(str(self.model_dir))
        log.info("loaded %s (mlx) in %.0fs", self.model_dir.name, time.time() - t0)

    def recognize(
        self, image_path: str | Path, max_new_tokens: int | None = None, hints: dict | None = None
    ) -> OcrResult:
        if self.model is None:
            self.load()
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        max_new_tokens = max_new_tokens or self.default_max_new_tokens
        img, orig, new = prepare_image(image_path, self.max_pixels, self.min_pixels)
        # mlx-vlm takes file paths: write the resized image so both backends see identical pixels.
        tmp = Path(tempfile.gettempdir()) / f"nassakh_mlx_{uuid.uuid4().hex}.png"
        img.save(tmp)
        try:
            formatted = apply_chat_template(self.processor, self.config, self.prompt, num_images=1)
            wanted = {"max_tokens": max_new_tokens, "temperature": 0.0, "verbose": False}
            params = inspect.signature(generate).parameters
            accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            kwargs = {k: v for k, v in wanted.items() if k in params or accepts_kwargs}
            t0 = time.time()
            out = generate(self.model, self.processor, formatted, image=[str(tmp)], **kwargs)
            duration = time.time() - t0
        finally:
            tmp.unlink(missing_ok=True)
        text = getattr(out, "text", out)
        n_out = getattr(out, "generation_tokens", None)
        finish = "unknown" if n_out is None else ("length" if n_out >= max_new_tokens else "stop")
        extra = {
            k: getattr(out, k) for k in ("generation_tps", "prompt_tps", "peak_memory") if hasattr(out, k)
        }
        extra.update(
            {
                "prompt_tokens": getattr(out, "prompt_tokens", None),
                "image_size": list(orig),
                "resized_to": list(new),
            }
        )
        return OcrResult(text=str(text), duration_s=duration, output_tokens=n_out, finish=finish, extra=extra)

    def unload(self) -> None:
        self.model = self.processor = self.config = None
        gc.collect()
