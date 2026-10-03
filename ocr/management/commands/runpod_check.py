"""`manage.py runpod_check [--no-ping] [--book ID --page N]`: is the Runpod endpoint of the Qari models ready?

Prints the endpoint Nassakh would call, its health (workers idle and running, jobs queued; the worker's local
server has none, which is only a warning), then sends a ping:
a job that reads nothing, so it waits for a worker to start and load both models when none is warm (a cold
start), and reports the GPU, the library versions and where each model's weights came from (baked into the
image, Runpod's cached model, a download) at which revision. With --book and --page it also reads that page's
first region with both models, as `run_full_ocr` would, and prints the timing (the page is not changed).
Each request is a row of the API log («سجل طلبات Runpod» in Django admin).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from books.models import Page
from ocr import runpod, services
from ocr.engines.prompts import PROMPT_QARI

from .runpod_compare import model_crops


def _s(ms) -> str:
    return "?" if ms is None else f"{ms / 1000:.1f} s"


class Command(BaseCommand):
    help = "Check the Runpod endpoint of the Qari models: health, a ping, and optionally one real read."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--no-ping", action="store_true", help="only the health, no job")
        parser.add_argument("--book", type=int, help="read a region of this book (id) …")
        parser.add_argument("--page", type=int, help="… on this page (number)")

    def handle(self, *args, **options) -> None:
        try:
            client = runpod.client()
        except runpod.RemoteConfigError as exc:
            raise CommandError(str(exc)) from exc
        key = client.config.api_key
        self.stdout.write(
            f"endpoint  {client.config.endpoint_url} (key …{key[-4:] if len(key) > 4 else '?'})"
        )
        try:
            health = client.health()
        except runpod.RemoteError as exc:
            if options["no_ping"]:
                raise CommandError(f"health: {exc}") from exc
            # the worker's local server (`python handler.py --rp_serve_api`) has no /health: the ping decides
            first = str(exc).splitlines()[0]
            self.stdout.write(self.style.WARNING(f"health    not available ({first}); the ping follows"))
        else:
            workers, jobs = health.get("workers") or {}, health.get("jobs") or {}
            self.stdout.write(
                f"health    workers idle {workers.get('idle', '?')}, running {workers.get('running', '?')} · "
                f"jobs in queue {jobs.get('inQueue', '?')}, in progress {jobs.get('inProgress', '?')}, "
                f"completed {jobs.get('completed', '?')}, failed {jobs.get('failed', '?')}"
            )
        if options["no_ping"]:
            return
        try:
            ping = client.ping(trace={"source": "runpod_check"})
        except runpod.RemoteError as exc:
            raise CommandError(f"ping: {exc}") from exc
        worker, remote = ping.worker, ping.remote
        start = "cold start" if worker.get("cold_start") else "warm worker"
        self.stdout.write(
            f"ping      {start}: {_s(remote.get('total_ms'))} (queue and start {_s(remote.get('queue_ms'))}) "
            f"on {worker.get('gpu') or worker.get('device') or '?'} · worker {worker.get('version', '?')} · "
            f"torch {worker.get('torch', '?')} · CUDA {worker.get('cuda', '?')} · "
            f"transformers {worker.get('transformers', '?')} · models loaded in {worker.get('load_s', '?')} s"
        )
        for engine, info in (worker.get("weights") or {}).items():
            if info.get("error"):
                self.stdout.write(self.style.ERROR(f"          {engine}: {info['error']}"))
            else:
                origin, source, revision = info.get("origin"), info.get("source"), info.get("revision")
                self.stdout.write(f"          {engine}: {origin} · {source} @ {revision}")
        if options.get("book") is not None:
            self._read(client, options["book"], options.get("page") or 1)

    def _read(self, client: runpod.RunpodClient, book_id: int, number: int) -> None:
        page = Page.objects.filter(book_id=book_id, number=number).select_related("book").first()
        if page is None:
            raise CommandError(f"No page {number} in book {book_id}.")
        primary, secondary, _ = services.engine_names()
        cfg = settings.NASSAKH
        with tempfile.TemporaryDirectory(prefix="nassakh-runpod-check-") as tmp:
            try:
                crop = next(model_crops(page, Path(tmp)))
            except services.OcrError as exc:
                raise CommandError(str(exc)) from exc
            except StopIteration:
                raise CommandError(f"Page {number} has no region the models read.") from None
            try:
                answer = client.read(
                    crop.path,
                    [(name, crop.cap) for name in dict.fromkeys((primary, secondary))],
                    prompt=PROMPT_QARI,
                    min_pixels=int(cfg["MIN_PIXELS"]),
                    max_pixels=int(cfg["MAX_PIXELS"]),
                    trace={
                        "book": book_id,
                        "page": number,
                        "page_id": page.pk,
                        "region": crop.target.kind,
                        "variant": crop.variant,
                        "source": "runpod_check",
                    },
                )
            except runpod.RemoteError as exc:
                raise CommandError(f"read: {exc}") from exc
        parts = []
        for name, task in answer.tasks.items():
            if task.ok:
                parts.append(f"{name} {task.output_tokens} tokens in {task.duration_s:.1f} s ({task.finish})")
            else:
                parts.append(f"{name} failed: {task.error.splitlines()[0]}")
        remote = answer.remote
        self.stdout.write(
            f"read      p{number} {crop.target.kind}: {' · '.join(parts)} · "
            f"request {_s(remote.get('total_ms'))}, GPU {_s(remote.get('execution_ms'))}"
        )
        for name, task in answer.tasks.items():
            if task.ok:
                self.stdout.write(f"          {name}: {task.text[:160]}")
