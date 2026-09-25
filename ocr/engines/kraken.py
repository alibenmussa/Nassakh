"""Kraken, the reader of Arabic-Indic numbers (D50), through its own process.

Kraken 6 cannot share the project's environment (it pins torch <= 2.9, the project runs 2.14), so
`read` sends the page images and areas to `kraken_runner.py` running with Kraken's Python
(`NASSAKH["KRAKEN_PYTHON"]`, set up by `make kraken`) and reads its JSON answer. One call per page:
starting the process and loading the 16 MB model take about a second, each area a few milliseconds.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from django.conf import settings

from .base import OcrEngine, OcrResult

log = logging.getLogger(__name__)

RUNNER = Path(__file__).with_name("kraken_runner.py")
# Zenodo 7050270: «Printed Arabic-Script Base Model Trained on the OpenITI Corpus» (2022, CC0)
MODEL_REVISION = "openiti-2022"


class KrakenError(RuntimeError):
    """Kraken could not read the areas (not set up, crashed, or answered nonsense)."""


class KrakenEngine(OcrEngine):
    """Reads short areas of a page (numbers) with Kraken; `recognize` is not used for whole pages."""

    name = "kraken"
    backend = "cpu"
    kind = "classic"

    def __init__(
        self, python: str | Path | None = None, model: str | Path | None = None, timeout: int | None = None
    ):
        cfg = settings.NASSAKH
        self.python = Path(python or cfg["KRAKEN_PYTHON"])
        self.model = Path(model or cfg["KRAKEN_MODEL"])
        self.timeout = int(timeout or cfg.get("KRAKEN_TIMEOUT_S", 120))

    def is_prepared(self) -> bool:
        return self.python.exists() and self.model.exists()

    def load(self) -> None:
        if not self.is_prepared():
            missing = self.python if not self.python.exists() else self.model
            raise KrakenError(f"Kraken is not set up: {missing} is missing; run `make kraken`.")

    @property
    def model_id(self) -> str:
        return f"kraken:{self.model.name}"

    @property
    def model_revision(self) -> str:
        return MODEL_REVISION

    def read(self, pages: list[dict]) -> dict:
        """`kraken_runner` on `pages` (`[{"image", "lines": [{"id", "bbox"}]}]`): its answer, with
        `lines` `[{id, text, chars: [[char, x0, x1, conf], …]}]`. Raises `KrakenError`."""
        self.load()
        request = json.dumps({"model": str(self.model), "pages": pages}, ensure_ascii=False)
        started = time.monotonic()
        try:
            done = subprocess.run(  # noqa: S603 - our own script, with the configured interpreter
                [str(self.python), "-P", str(RUNNER)],  # -P: not this folder first on the path
                input=request,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise KrakenError(f"Kraken took longer than {self.timeout} s") from exc
        except OSError as exc:
            raise KrakenError(f"Kraken could not start: {exc}") from exc
        try:
            answer = json.loads(done.stdout or "{}")
        except json.JSONDecodeError as exc:
            tail = (done.stderr or done.stdout or "").strip()[-500:]
            raise KrakenError(f"Kraken answered no JSON (exit {done.returncode}): {tail}") from exc
        if not answer.get("ok"):
            raise KrakenError(str(answer.get("error") or f"Kraken failed (exit {done.returncode})"))
        answer["elapsed"] = round(time.monotonic() - started, 3)
        return answer

    def recognize(self, image_path, max_new_tokens=None, hints=None) -> OcrResult:  # noqa: ARG002
        raise NotImplementedError("Kraken reads number areas through `read`, not whole images")
