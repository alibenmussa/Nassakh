"""A deterministic engine for tests: returns configurable text, never touches a model.

    engine = FakeEngine(text="سطر أول\nسطر ثانٍ", lines=[...])   # fixed answer
    engine = FakeEngine(responder=lambda path, cap: "..." )       # answer per call
    engine = FakeEngine(error=RuntimeError("boom"))               # every call fails

Every call is recorded in `engine.calls` (path, cap, image size) so tests can assert what the
service sent (e.g. that footnote crops were upscaled).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .base import OcrEngine, OcrResult


class FakeEngine(OcrEngine):
    """Configurable stand-in for any engine name; `kind` and `backend` can be changed per test."""

    name = "fake"
    backend = "fake"
    kind = "classic"

    def __init__(
        self,
        text: str = "نص تجريبي",
        lines: list[dict] | None = None,
        looped: bool = False,
        finish: str = "stop",
        output_tokens: int | None = None,
        duration_s: float = 0.01,
        error: Exception | None = None,
        responder: Callable[[Path, int | None], OcrResult | str] | None = None,
        name: str = "fake",
    ):
        self.name = name
        self.text = text
        self.lines = lines
        self.looped = looped
        self.finish = finish
        self.output_tokens = output_tokens
        self.duration_s = duration_s
        self.error = error
        self.responder = responder
        self.calls: list[dict] = []
        self.loaded = False
        self.unloaded = 0

    @property
    def is_loaded(self) -> bool:
        return self.loaded

    def load(self) -> None:
        self.loaded = True

    def unload(self) -> None:
        self.loaded = False
        self.unloaded += 1

    @property
    def model_id(self) -> str:
        return "fake"

    @property
    def model_revision(self) -> str:
        return "0"

    def set_text(self, text: str, lines: list[dict] | None = None) -> None:
        """Change the fixed answer (and optional Tesseract-style lines) for the next calls."""
        self.text = text
        self.lines = lines

    def recognize(self, image_path: str | Path, max_new_tokens: int | None = None) -> OcrResult:
        call = {"path": str(image_path), "max_new_tokens": max_new_tokens, "image_size": None}
        try:
            from PIL import Image

            with Image.open(image_path) as img:
                call["image_size"] = list(img.size)
        except Exception:  # noqa: BLE001 - not an image (pdf ref) or unreadable: fine for tests
            pass
        self.calls.append(call)
        if self.error is not None:
            raise self.error
        if self.responder is not None:
            out = self.responder(Path(str(image_path)), max_new_tokens)
            if isinstance(out, OcrResult):
                return out
            return OcrResult(text=str(out), duration_s=self.duration_s, finish=self.finish)
        extra = {"lines": [dict(line) for line in self.lines]} if self.lines is not None else {}
        return OcrResult(
            text=self.text,
            duration_s=self.duration_s,
            output_tokens=self.output_tokens,
            finish=self.finish,
            looped=self.looped,
            extra=extra,
        )
