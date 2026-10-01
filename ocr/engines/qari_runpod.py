"""Qari through the Runpod endpoint of nassakh-qari-worker (OCR_BACKEND=runpod): no weights on this machine.

Both Qari engines share the endpoint: `run_full_ocr` reads a crop with both in one request
(`registry.together` → `read_together`) and each `recognize` takes its own answer; a call outside such a pair
asks the endpoint for its model alone. The answer carries the revision of the worker's weights and Runpod's
timing, which `services._fill_run` stores on the OcrRun (`model_revision`, `params["remote"]`); the prompt,
the pixel budget and the token caps are this side's (`QariEngine`), sent with every request.
"""

from __future__ import annotations

from pathlib import Path

from .. import runpod
from .base import OcrResult
from .qari import QariEngine


class QariRunpodEngine(QariEngine):
    """A Qari model read on a Runpod GPU; `load` only checks that the endpoint is configured."""

    backend = "runpod"

    def __init__(self, name: str, model_dir: Path | None = None) -> None:
        super().__init__(name, model_dir)
        self._loaded = False

    @property
    def model_revision(self) -> str:
        """Known from the worker's answer only (`_fill_run` takes it from the result's `extra`)."""
        return ""

    @property
    def batch_key(self) -> str:
        """Engines with the same key read a crop together in one request (`registry.together`)."""
        return f"runpod:{runpod.Config.from_settings().endpoint_url}"

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def is_prepared(self) -> bool:
        return not runpod.Config.from_settings().problem

    def load(self) -> None:
        problem = runpod.Config.from_settings().problem
        if problem:
            raise runpod.RemoteConfigError(problem)
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def generation_params(self, max_new_tokens: int) -> dict:
        params = super().generation_params(max_new_tokens)
        params.pop("model_dir", None)
        params["endpoint"] = runpod.Config.from_settings().endpoint_url
        return params

    def read_together(
        self, source: str | Path, names: list[str], max_new_tokens: int | None, trace: dict
    ) -> None:
        """Ask the endpoint once for every engine of `names` (this one included); never raises."""
        runpod.prefetch(
            source,
            names,
            max_new_tokens or self.default_max_new_tokens,
            prompt=self.prompt,
            min_pixels=self.min_pixels,
            max_pixels=self.max_pixels,
            trace=trace,
        )

    def forget_together(self) -> None:
        runpod.forget()

    def recognize(
        self, image_path: str | Path, max_new_tokens: int | None = None, hints: dict | None = None
    ) -> OcrResult:
        cap = max_new_tokens or self.default_max_new_tokens
        answer = runpod.take(self.name, image_path, cap)
        if answer is None:
            answer = runpod.read_one(
                image_path,
                self.name,
                cap,
                prompt=self.prompt,
                min_pixels=self.min_pixels,
                max_pixels=self.max_pixels,
            )
        if not answer.ok:
            raise runpod.RemoteError(answer.error)
        return OcrResult(
            text=answer.text,
            duration_s=answer.duration_s,
            output_tokens=answer.output_tokens,
            finish=answer.finish or "unknown",
            extra=dict(answer.extra),
        )
