"""The interface every OCR engine implements and the result it returns."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class OcrResult:
    """Output of one `recognize` call.

    `finish` is `stop` (model ended on its own), `length` (hit the token cap), `unknown` (backend
    does not report it) or `n/a` (classic engines). `looped` is set by engines that detect a
    degenerate repetition themselves; the service also checks the parsed text. `extra` carries
    backend details (device, tokens/s) and, for Tesseract, the `lines` with word boxes.
    """

    text: str
    duration_s: float
    output_tokens: int | None = None
    finish: str = "stop"
    looped: bool = False
    extra: dict = field(default_factory=dict)


class OcrEngine:
    """Base class: `load()` once per process, `recognize()` per image, `unload()` to free memory.

    `kind` is `vlm` (vision-language model, needs the GPU worker), `classic` (Tesseract, CPU) or
    `pdf` (text layer, no image at all). `prompt` is stored on every run for VLMs; empty otherwise.
    """

    name: str = ""
    backend: str = ""
    kind: str = "classic"
    prompt: str = ""

    def load(self) -> None:
        """Load weights / check binaries. Idempotent; cheap for classic engines."""

    def recognize(
        self, image_path: str | Path, max_new_tokens: int | None = None, hints: dict | None = None
    ) -> OcrResult:
        """Recognise the text of one image file (or, for the pdf engine, one PDF page reference).

        `hints` are optional engine-specific knobs for one call (e.g. `{"psm": 7}` for Tesseract on a
        single-line crop); engines ignore hints they do not understand.
        """
        raise NotImplementedError

    def unload(self) -> None:
        """Release the model. `load()` may be called again afterwards."""

    @property
    def is_loaded(self) -> bool:
        """True once `load()` succeeded (classic engines are always loaded)."""
        return True

    @property
    def model_id(self) -> str:
        """Hugging Face id, `tesseract:<langs>` or another stable identifier of the model."""
        return ""

    @property
    def model_revision(self) -> str:
        """Commit / version of the model weights, when known."""
        return ""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name} [{self.backend}]>"
