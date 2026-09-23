"""OCR engine wrappers used by run_ocr.py.

Three backends behind one tiny interface (`load()`, `recognize(path)`, `unload()`):
  TorchQwenEngine   Qwen2-VL checkpoints through transformers on MPS (or CPU)
  MlxQwenEngine     the same checkpoints converted for Apple MLX (mlx-vlm)
  TesseractEngine   CPU baseline

This is a sketch of the `OcrEngine` interface the Django `ocr` app will get.
"""
from __future__ import annotations

import gc
import inspect
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import config
from common import prepare_image

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


@dataclass
class OcrResult:
    text: str
    duration_s: float
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    finish: str = "stop"            # stop | length | unknown
    image_size: tuple[int, int] | None = None
    resized_to: tuple[int, int] | None = None
    extra: dict = field(default_factory=dict)


class TorchQwenEngine:
    backend = "torch"

    def __init__(self, model_dir: Path, prompt: str, gen: dict, device: str | None = None):
        import torch

        self.model_dir = Path(model_dir)
        self.prompt = prompt
        self.gen = gen
        self.device = device or ("mps" if torch.backends.mps.is_available() else "cpu")
        self.model = None
        self.processor = None

    def load(self) -> None:
        import torch
        from transformers import AutoProcessor, Qwen2VLForConditionalGeneration

        t0 = time.time()
        dtype = getattr(torch, self.gen["dtype"])
        self.model = Qwen2VLForConditionalGeneration.from_pretrained(self.model_dir, dtype=dtype).to(self.device).eval()
        self.processor = AutoProcessor.from_pretrained(self.model_dir)
        self._constrain_processor()
        print(f"  [torch] loaded {self.model_dir.name} on {self.device} in {time.time() - t0:.0f}s", flush=True)

    def adopt(self, model, processor) -> None:
        """Use an already constructed model (for the self-test)."""
        self.model, self.processor = model.eval(), processor
        self._constrain_processor()

    def _constrain_processor(self) -> None:
        ip = getattr(self.processor, "image_processor", None)
        for attr in ("min_pixels", "max_pixels"):
            if ip is not None and hasattr(ip, attr):
                setattr(ip, attr, self.gen[attr])
        if ip is not None and isinstance(getattr(ip, "size", None), dict):
            ip.size = {**ip.size, "shortest_edge": self.gen["min_pixels"], "longest_edge": self.gen["max_pixels"]}

    def recognize(self, image_path: Path) -> OcrResult:
        import torch

        img, orig, new = prepare_image(image_path, self.gen["max_pixels"], self.gen["min_pixels"])
        messages = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": self.prompt}]}]
        chat = self.processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self.processor(text=[chat], images=[img], return_tensors="pt").to(self.device)
        n_prompt = int(inputs["input_ids"].shape[1])
        t0 = time.time()
        with torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=self.gen["max_new_tokens"], do_sample=False)
        duration = time.time() - t0
        gen_ids = out[0, n_prompt:]
        text = self.processor.batch_decode([gen_ids], skip_special_tokens=True)[0]
        finish = "length" if gen_ids.shape[0] >= self.gen["max_new_tokens"] else "stop"
        if self.device == "mps":
            torch.mps.empty_cache()
        return OcrResult(text=text, duration_s=duration, prompt_tokens=n_prompt, output_tokens=int(gen_ids.shape[0]),
                         finish=finish, image_size=orig, resized_to=new, extra={"device": self.device, "dtype": self.gen["dtype"]})

    def unload(self) -> None:
        import torch

        self.model = None
        self.processor = None
        gc.collect()
        if self.device == "mps":
            torch.mps.empty_cache()


class MlxQwenEngine:
    backend = "mlx"

    def __init__(self, model_dir: Path, prompt: str, gen: dict):
        self.model_dir = Path(model_dir)
        self.prompt = prompt
        self.gen = gen
        self.model = None
        self.processor = None
        self.config = None

    def load(self) -> None:
        from mlx_vlm import load
        from mlx_vlm.utils import load_config

        t0 = time.time()
        self.model, self.processor = load(str(self.model_dir))
        self.config = load_config(str(self.model_dir))
        print(f"  [mlx] loaded {self.model_dir.name} in {time.time() - t0:.0f}s", flush=True)

    def recognize(self, image_path: Path) -> OcrResult:
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        img, orig, new = prepare_image(image_path, self.gen["max_pixels"], self.gen["min_pixels"])
        config.TMP.mkdir(exist_ok=True)
        tmp = config.TMP / f"mlx_{uuid.uuid4().hex}.png"
        img.save(tmp)
        try:
            formatted = apply_chat_template(self.processor, self.config, self.prompt, num_images=1)
            wanted = {"max_tokens": self.gen["max_new_tokens"], "temperature": 0.0, "verbose": False}
            sig = inspect.signature(generate).parameters
            accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.values())
            kwargs = {k: v for k, v in wanted.items() if k in sig or accepts_kwargs}
            t0 = time.time()
            out = generate(self.model, self.processor, formatted, image=[str(tmp)], **kwargs)
            duration = time.time() - t0
        finally:
            tmp.unlink(missing_ok=True)
        text = getattr(out, "text", out)
        n_out = getattr(out, "generation_tokens", None)
        n_prompt = getattr(out, "prompt_tokens", None)
        finish = "unknown" if n_out is None else ("length" if n_out >= self.gen["max_new_tokens"] else "stop")
        extra = {k: getattr(out, k) for k in ("generation_tps", "prompt_tps", "peak_memory") if hasattr(out, k)}
        return OcrResult(text=str(text), duration_s=duration, prompt_tokens=n_prompt, output_tokens=n_out,
                         finish=finish, image_size=orig, resized_to=new, extra=extra)

    def unload(self) -> None:
        self.model = self.processor = self.config = None
        gc.collect()


class TesseractEngine:
    backend = "cpu"

    def __init__(self, lang: str = "ara", psm: int = 4):
        self.lang, self.psm = lang, psm

    def load(self) -> None:
        import pytesseract

        langs = pytesseract.get_languages(config="")
        if self.lang not in langs:
            raise SystemExit(f"tesseract language '{self.lang}' missing (have {langs}). Run: brew install tesseract-lang")
        print(f"  [tesseract] {pytesseract.get_tesseract_version()} lang={self.lang} psm={self.psm}", flush=True)

    def recognize(self, image_path: Path) -> OcrResult:
        import pytesseract
        from PIL import Image

        img = Image.open(image_path)
        t0 = time.time()
        text = pytesseract.image_to_string(img, lang=self.lang, config=f"--oem 1 --psm {self.psm}")
        return OcrResult(text=text, duration_s=time.time() - t0, image_size=img.size, resized_to=img.size,
                         extra={"psm": self.psm, "oem": 1})

    def unload(self) -> None:
        pass


def build_engine(key: str, backend: str):
    """Instantiate the engine `key` for `backend` (torch | mlx | cpu)."""
    spec = config.ENGINES[key]
    if spec["kind"] == "tesseract":
        return TesseractEngine(spec["lang"], spec["psm"])
    if backend == "mlx":
        d = spec["mlx"]
        if not (d / ".complete").exists():
            raise SystemExit(f"{d} not prepared. Run: python prepare_models.py --engines {key} --mlx")
        return MlxQwenEngine(d, spec["prompt"], config.GEN)
    d = spec["local"]
    if not (d / "config.json").exists():
        raise SystemExit(f"{d} not prepared. Run: python prepare_models.py --engines {key}")
    return TorchQwenEngine(d, spec["prompt"], config.GEN)
