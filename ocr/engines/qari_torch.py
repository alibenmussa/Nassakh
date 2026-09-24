"""Qari (Qwen2-VL fine-tunes) through transformers on Apple MPS or CPU. Port of the PoC engine."""

from __future__ import annotations

import gc
import logging
import os
import time
from pathlib import Path

from core.images import prepare_image

from .base import OcrResult
from .qari import QariEngine, resolve_device

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

log = logging.getLogger(__name__)


class QariTorchEngine(QariEngine):
    """PyTorch backend: the reference implementation, bf16 on MPS (or CPU)."""

    backend = "torch"

    def __init__(
        self, name: str, model_dir: Path | None = None, device: str | None = None, dtype: str = "bfloat16"
    ):
        super().__init__(name, model_dir)
        self.device = resolve_device(device)
        self.dtype_name = dtype
        self.model = None
        self.processor = None

    @property
    def is_loaded(self) -> bool:
        return self.model is not None

    def load(self) -> None:
        if self.model is not None:
            return
        if not self.is_prepared():
            raise FileNotFoundError(
                f"{self.model_dir} is not prepared; "
                f"run playground/poc/prepare_models.py --engines {self.name}"
            )
        import torch
        from transformers import AutoProcessor, Qwen2VLForConditionalGeneration

        t0 = time.time()
        dtype = getattr(torch, self.dtype_name)
        model = Qwen2VLForConditionalGeneration.from_pretrained(self.model_dir, dtype=dtype)
        self.model = model.to(self.device).eval()
        self.processor = AutoProcessor.from_pretrained(self.model_dir)
        self._constrain_processor()
        log.info("loaded %s on %s in %.0fs", self.model_dir.name, self.device, time.time() - t0)

    def _constrain_processor(self) -> None:
        """Apply the pixel budget to the image processor (names differ across transformers versions)."""
        ip = getattr(self.processor, "image_processor", None)
        if ip is None:
            return
        for attr, value in (("min_pixels", self.min_pixels), ("max_pixels", self.max_pixels)):
            if hasattr(ip, attr):
                setattr(ip, attr, value)
        if isinstance(getattr(ip, "size", None), dict):
            ip.size = {**ip.size, "shortest_edge": self.min_pixels, "longest_edge": self.max_pixels}

    def recognize(
        self, image_path: str | Path, max_new_tokens: int | None = None, hints: dict | None = None
    ) -> OcrResult:
        if self.model is None:
            self.load()
        import torch

        max_new_tokens = max_new_tokens or self.default_max_new_tokens
        img, orig, new = prepare_image(image_path, self.max_pixels, self.min_pixels)
        messages = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": self.prompt}]}]
        chat = self.processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self.processor(text=[chat], images=[img], return_tensors="pt").to(self.device)
        n_prompt = int(inputs["input_ids"].shape[1])
        t0 = time.time()
        with torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
        duration = time.time() - t0
        gen_ids = out[0, n_prompt:]
        text = self.processor.batch_decode([gen_ids], skip_special_tokens=True)[0]
        n_out = int(gen_ids.shape[0])
        if self.device == "mps":
            torch.mps.empty_cache()
        return OcrResult(
            text=text,
            duration_s=duration,
            output_tokens=n_out,
            finish="length" if n_out >= max_new_tokens else "stop",
            extra={
                "device": self.device,
                "dtype": self.dtype_name,
                "prompt_tokens": n_prompt,
                "image_size": list(orig),
                "resized_to": list(new),
            },
        )

    def unload(self) -> None:
        self.model = None
        self.processor = None
        gc.collect()
        try:
            import torch

            if self.device == "mps":
                torch.mps.empty_cache()
        except Exception:  # noqa: BLE001 - nothing to free
            pass
