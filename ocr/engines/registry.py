"""Engine registry: one loaded instance per engine name per process.

The GPU worker runs with `--pool=solo`, so the cache keeps the Qari models resident across tasks; with
OCR_BACKEND=runpod the models live on a Runpod GPU and the gpu worker is a threads pool of HTTP clients
(`ocr.runpod`). `together` lets engines that answer several models in one request read a crop once.
Tests inject fakes with `override({...})` so no real model is ever loaded there.
"""

from __future__ import annotations

import logging
import shutil
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings

from .base import OcrEngine

log = logging.getLogger(__name__)

ENGINE_NAMES: tuple[str, ...] = ("qari_v03", "qari_v02", "tesseract", "pdf_text", "kraken", "fake")
VLM_NAMES: tuple[str, ...] = ("qari_v03", "qari_v02")

_cache: dict[str, OcrEngine] = {}
_override: dict[str, OcrEngine] | None = None
_lock = threading.RLock()


def build_engine(name: str) -> OcrEngine:
    """Instantiate (without loading) the engine `name`; the backend comes from settings."""
    if name in VLM_NAMES:
        backend = str(settings.NASSAKH.get("OCR_BACKEND", "torch")).lower()
        if backend == "mlx":
            from .qari_mlx import QariMlxEngine

            return QariMlxEngine(name)
        if backend == "runpod":
            from .qari_runpod import QariRunpodEngine

            return QariRunpodEngine(name)
        from .qari_torch import QariTorchEngine

        return QariTorchEngine(name)
    if name == "tesseract":
        from .tesseract import TesseractEngine

        return TesseractEngine()
    if name == "pdf_text":
        from .pdf_text import PdfTextEngine

        return PdfTextEngine()
    if name == "kraken":
        from .kraken import KrakenEngine

        return KrakenEngine()
    if name == "fake":
        from .fake import FakeEngine

        return FakeEngine()
    raise KeyError(f"unknown OCR engine {name!r}; known: {', '.join(ENGINE_NAMES)}")


def get_engine(name: str) -> OcrEngine:
    """Return the loaded engine `name`, building and loading it on first use in this process."""
    with _lock:
        if _override is not None:
            try:
                return _override[name]
            except KeyError:
                raise KeyError(f"engine {name!r} not provided by the active override") from None
        engine = _cache.get(name)
        if engine is None:
            engine = build_engine(name)
            engine.load()
            _cache[name] = engine
        elif not engine.is_loaded:
            engine.load()
        return engine


def loaded_engines() -> list[str]:
    """Names of the engines currently held in the cache."""
    with _lock:
        return sorted(_cache)


def available_engines() -> list[str]:
    """Engine names whose prerequisites exist on this machine (weights prepared, binary installed)."""
    names: list[str] = []
    for name in ENGINE_NAMES:
        if name in VLM_NAMES:
            try:
                engine = build_engine(name)
            except Exception:  # noqa: BLE001 - backend import failed: not available
                continue
            if engine.is_prepared():
                names.append(name)
        elif name == "tesseract":
            if shutil.which("tesseract"):
                names.append(name)
        elif name == "kraken":
            if build_engine(name).is_prepared():
                names.append(name)
        else:
            names.append(name)
    return names


@contextmanager
def together(
    names: Sequence[str], source: str | Path, max_new_tokens: int | None = None, trace: dict | None = None
) -> Iterator[None]:
    """`names` are about to read the same `source`, one `recognize` after the other.

    Engines that answer several models in one request (Qari on Runpod: they share a `batch_key`) are asked
    once here, through the first of them (`read_together`), and each `recognize` in the block takes its own
    answer from that request; what nobody took is dropped at the end (`forget_together`). `trace` says what is
    read (book, page, region, variant) for engines that log their requests. Other engines are untouched, so
    local and fake engines read exactly as before.
    """
    engines = [get_engine(name) for name in names]
    groups: dict[str, list[str]] = {}
    leads: dict[str, OcrEngine] = {}
    for name, engine in zip(names, engines, strict=True):
        key = getattr(engine, "batch_key", None)
        if key and name not in groups.get(key, []):
            groups.setdefault(key, []).append(name)
            leads.setdefault(key, engine)
    for key, members in groups.items():
        leads[key].read_together(source, members, max_new_tokens, dict(trace or {}))
    try:
        yield
    finally:
        for lead in leads.values():
            lead.forget_together()


def unload_all() -> None:
    """Unload and forget every cached engine (frees the GPU memory)."""
    with _lock:
        for name, engine in list(_cache.items()):
            try:
                engine.unload()
            except Exception:  # noqa: BLE001 - keep unloading the others
                log.exception("unloading %s failed", name)
        _cache.clear()


@contextmanager
def override(engines: dict[str, OcrEngine]) -> Iterator[dict[str, OcrEngine]]:
    """Serve only `engines` from `get_engine` inside the block (tests). Unknown names raise KeyError."""
    global _override
    with _lock:
        previous = _override
        _override = dict(engines)
    try:
        yield _override
    finally:
        with _lock:
            _override = previous
