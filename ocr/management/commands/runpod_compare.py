"""The Runpod endpoint against the readings stored for a book (docs/RUNPOD_SPEC.md §8, the gate before
OCR_BACKEND=runpod becomes the default):

    manage.py runpod_compare --book ID [--pages 1-5,9] [--max-regions N] [--price-per-second P] [--json FILE]

Every region the models read on the chosen pages is cropped exactly as `run_full_ocr` crops it (gray,
footnotes at 2x, the same token cap) and read again on Runpod by the engines that have a stored run of that
region and crop; the remote text is parsed as `_fill_run` parses it and compared with the stored text, which
a local backend (MLX or PyTorch) wrote. Per engine: identical readings, the character difference (edit
distance over the stored text's length), loops on either side. Then Runpod's timing: GPU seconds per request
and per page, the queue and cold starts, and the cost per page and per 800 pages at the price of a GPU second.

Nothing of the book changes; each request is a row of the API log («سجل طلبات Runpod»). The comparison is
not an accuracy measure against a ground truth: it says how far Runpod reads from what the local backend
read.
"""

from __future__ import annotations

import json
import statistics
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from rapidfuzz.distance import Levenshtein

from books.models import Book, Page
from core.arabic import parse_output
from ocr import runpod, services
from ocr.engines.prompts import PROMPT_QARI
from ocr.engines.qari import max_new_tokens_for
from ocr.models import OcrRun

DIFF_LISTED = 0.02  # regions whose difference is above 2 % are listed one by one


@dataclass
class Crop:
    """A region as `run_full_ocr` gives it to the models."""

    target: services.Target
    path: Path
    variant: str
    cap: int


def model_crops(page: Page, tmpdir: Path) -> Iterator[Crop]:
    """The crops `run_full_ocr` sends the models for `page` (running heads and page numbers are not read)."""
    pre = services._preprocess_of(page)
    gray = services._load_field_image(pre.gray_image, "الرمادية")
    upscale = int(services.nassakh().get("FOOTNOTE_UPSCALE", 2) or 1)
    for i, target in enumerate(services._targets(page, gray.shape, ocr_only=True)):
        scale = upscale if target.kind in services.FOOTNOTE_KINDS and upscale > 1 else 1
        variant = f"gray_{scale}x" if scale > 1 else "gray"
        path = services._save_temp(
            services._crop_image(gray, target.bbox, scale), tmpdir, f"{variant}-{i}-{target.kind}"
        )
        yield Crop(target, path, variant, max_new_tokens_for(target.kind))


def stored_run(page: Page, crop: Crop, engine: str) -> OcrRun | None:
    """The latest successful run of `engine` on this region and crop."""
    runs = page.ocr_runs.filter(engine_name=engine, input_variant=crop.variant, status=OcrRun.Status.OK)
    if crop.target.region is not None:
        runs = runs.filter(region=crop.target.region)
    else:
        runs = runs.filter(region__isnull=True, params__scope=services.PAGE_SCOPE)
    return runs.order_by("-created_at", "-id").first()


def parse_pages(spec: str | None) -> set[int] | None:
    """`1-5,9` → {1, 2, 3, 4, 5, 9}; None for every page."""
    if not spec:
        return None
    numbers: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        first, _, last = part.partition("-")
        try:
            low, high = int(first), int(last or first)
        except ValueError as exc:
            raise CommandError(f"--pages: {part!r} is not a page number or a range") from exc
        numbers.update(range(min(low, high), max(low, high) + 1))
    return numbers


def difference(stored: str, remote: str) -> float:
    """Characters to change to go from the stored reading to Runpod's, over the stored reading's length."""
    if stored == remote:
        return 0.0
    return Levenshtein.distance(stored, remote) / max(1, len(stored))


class Command(BaseCommand):
    help = "Read a book's regions again on Runpod and compare with the stored readings (writes nothing)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--book", type=int, required=True, help="the book (id)")
        parser.add_argument("--pages", help="page numbers, e.g. 1-5,9 (default: every page read)")
        parser.add_argument(
            "--max-regions", type=int, default=0, help="stop after this many regions (0: all)"
        )
        parser.add_argument(
            "--price-per-second",
            type=float,
            default=None,
            help="price of a GPU second (default: RUNPOD_PRICE_PER_S; 24 GB GPUs cost 0.00019)",
        )
        parser.add_argument("--json", help="write every comparison, with both texts, to this file")

    def handle(self, *args, **options) -> None:
        book = Book.objects.filter(pk=options["book"]).first()
        if book is None:
            raise CommandError(f"No book {options['book']}.")
        try:
            client = runpod.client()
        except runpod.RemoteConfigError as exc:
            raise CommandError(str(exc)) from exc
        primary, secondary, _ = services.engine_names()
        engines = list(dict.fromkeys((primary, secondary)))
        cfg = settings.NASSAKH
        min_pixels, max_pixels = int(cfg["MIN_PIXELS"]), int(cfg["MAX_PIXELS"])
        wanted = parse_pages(options.get("pages"))
        pages = book.pages.filter(text_state=Page.TextState.FINAL, is_excluded=False).order_by("number")
        if wanted is not None:
            pages = pages.filter(number__in=wanted)
        price = options["price_per_second"]
        if price is None:
            price = float(cfg.get("RUNPOD_PRICE_PER_S") or 0)
        limit = int(options["max_regions"] or 0)

        self.stdout.write(f"book {book.pk} «{book.title}» on {client.config.endpoint_url}")
        rows: list[dict] = []
        requests: list[dict] = []
        pages_read: set[int] = set()
        with tempfile.TemporaryDirectory(prefix="nassakh-runpod-compare-") as tmp:
            for page in pages:
                for crop in model_crops(page, Path(tmp)):
                    if limit and len(requests) >= limit:
                        break
                    stored = {name: stored_run(page, crop, name) for name in engines}
                    asked = [name for name in engines if stored[name] is not None]
                    if not asked:
                        continue
                    trace = {
                        "book": book.pk,
                        "page": page.number,
                        "page_id": page.pk,
                        "region": crop.target.kind,
                        "variant": crop.variant,
                        "source": "runpod_compare",
                    }
                    caps = {
                        name: int((stored[name].params or {}).get("max_new_tokens") or crop.cap)
                        for name in asked
                    }
                    try:
                        answer = client.read(
                            crop.path,
                            [(name, caps[name]) for name in asked],
                            prompt=PROMPT_QARI,
                            min_pixels=min_pixels,
                            max_pixels=max_pixels,
                            trace=trace,
                        )
                    except runpod.RemoteError as exc:
                        self.stdout.write(self.style.ERROR(f"p{page.number} {crop.target.kind}: {exc}"))
                        continue
                    pages_read.add(page.pk)
                    requests.append(answer.remote)
                    for name in asked:
                        rows.append(self._compare(page, crop, stored[name], answer.tasks[name], caps[name]))
                if limit and len(requests) >= limit:
                    break
        if not rows:
            raise CommandError("No region of these pages has a stored reading to compare with.")
        self._report(rows, requests, len(pages_read), price)
        if options.get("json"):
            Path(options["json"]).write_text(
                json.dumps({"rows": rows, "requests": requests}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.stdout.write(f"details in {options['json']}")

    def _compare(self, page: Page, crop: Crop, stored: OcrRun, task: runpod.TaskAnswer, cap: int) -> dict:
        row = {
            "page": page.number,
            "region": crop.target.kind,
            "variant": crop.variant,
            "engine": stored.engine_name,
            "local_backend": stored.backend,
            "local_tokens": stored.output_tokens,
            "local_looped": stored.looped,
            "ok": task.ok,
        }
        if not task.ok:
            return {**row, "error": task.error}
        parsed, looped = parse_output(task.text, hit_cap=task.finish == "length")
        looped = looped or task.finish == "length"
        return {
            **row,
            "remote_tokens": task.output_tokens,
            "remote_looped": looped,
            "remote_gpu_s": task.duration_s,
            "identical": parsed == stored.parsed_text,
            "difference": round(difference(stored.parsed_text, parsed), 4),
            "local_text": stored.parsed_text,
            "remote_text": parsed,
            "cap": cap,
        }

    def _report(self, rows: list[dict], requests: list[dict], pages: int, price: float) -> None:
        self.stdout.write("")
        self.stdout.write(
            "engine     regions  identical  difference (median / mean / max)  loops local → runpod"
        )
        for engine in dict.fromkeys(row["engine"] for row in rows):
            done = [r for r in rows if r["engine"] == engine and r["ok"]]
            failed = sum(1 for r in rows if r["engine"] == engine and not r["ok"])
            if not done:
                self.stdout.write(f"{engine:<10} {failed} failed")
                continue
            diffs = [r["difference"] for r in done]
            same = sum(1 for r in done if r["identical"])
            self.stdout.write(
                f"{engine:<10} {len(done):>7}  {same / len(done):>8.0%}  "
                f"{statistics.median(diffs):>7.2%} / {statistics.fmean(diffs):.2%} / {max(diffs):.2%}"
                f"{'':>12}{sum(r['local_looped'] for r in done)} → {sum(r['remote_looped'] for r in done)}"
                + (f"   ({failed} failed)" if failed else "")
            )
        listed = sorted(
            (r for r in rows if r["ok"] and r["difference"] > DIFF_LISTED), key=lambda r: -r["difference"]
        )
        if listed:
            self.stdout.write("")
            self.stdout.write(f"regions differing by more than {DIFF_LISTED:.0%}:")
            for r in listed[:30]:
                self.stdout.write(
                    f"  p{r['page']} {r['region']} {r['engine']}: {r['difference']:.1%} "
                    f"(local {r['local_backend']} {r['local_tokens']} tokens, runpod {r['remote_tokens']})"
                )
        execution = [r["execution_ms"] for r in requests if r.get("execution_ms") is not None]
        queue = [r["queue_ms"] for r in requests if r.get("queue_ms") is not None]
        total = [r["total_ms"] for r in requests if r.get("total_ms") is not None]
        gpu_s = sum(execution) / 1000
        self.stdout.write("")
        self.stdout.write(
            f"requests {len(requests)} on {pages} pages · GPU {gpu_s:.1f} s "
            f"({gpu_s / max(1, len(requests)):.1f} s a request, {gpu_s / max(1, pages):.1f} s a page)"
        )
        if queue:
            cold = sum(1 for r in requests if r.get("cold_start"))
            median_queue, max_queue = statistics.median(queue) / 1000, max(queue) / 1000
            self.stdout.write(
                f"queue and start: median {median_queue:.1f} s, max {max_queue:.1f} s; "
                f"{cold} cold start(s) · a request takes {statistics.median(total) / 1000:.1f} s (median)"
            )
        gpus = sorted({r.get("gpu") for r in requests if r.get("gpu")})
        if gpus:
            self.stdout.write(f"GPU: {', '.join(gpus)}")
        if price and pages:
            per_page = gpu_s / pages * price
            self.stdout.write(
                f"cost at {price} $/s: {per_page:.5f} $ a page, {per_page * 800:.2f} $ for 800 pages "
                "(GPU time of the jobs; Runpod also bills a worker's start and its idle timeout)"
            )
