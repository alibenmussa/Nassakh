"""Qari on Runpod (OCR_BACKEND=runpod): the client, the engine, one request for both models, the API log.

No network: a fake endpoint answers through `httpx.MockTransport`, and a fake clock makes the retries, the
polling of a cold start and the timeout instant.
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.urls import reverse

import httpx
import numpy as np
import pytest
from PIL import Image

from books.models import Book, Page
from core.images import prepare_image, to_png_bytes
from core.storage import save_array
from ocr import runpod, services
from ocr.engines import registry
from ocr.engines.fake import FakeEngine
from ocr.engines.prompts import PROMPT_QARI
from ocr.engines.qari_runpod import QariRunpodEngine
from ocr.models import OcrRun, RemoteCall
from ocr.tests import FOOT, PRIMARY_BODY, SECONDARY_BODY, H, W, add_regions, engines
from processing.models import Preprocess

ENDPOINT_URL = "https://api.runpod.ai/v2/ep123"


# ---------------------------------------------------------------- a fake endpoint and clock


class Clock:
    """`time.monotonic` and `time.sleep` for the client: sleeping moves the clock, instantly."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


def worker_output(tasks: list[dict], text=None, errors: dict | None = None, cold: bool = False) -> dict:
    """What nassakh-qari-worker answers for `tasks` (schema 1)."""
    results = []
    for task in tasks:
        engine = task["engine"]
        if errors and engine in errors:
            results.append({"engine": engine, "status": "error", "error": errors[engine]})
            continue
        results.append(
            {
                "engine": engine,
                "status": "ok",
                "text": text(engine) if callable(text) else (text or f"قراءة {engine}"),
                "output_tokens": 12,
                "finish": "stop",
                "duration_s": 1.5,
                "prompt_tokens": 900,
                "image_size": [120, 100],
                "resized_to": [532, 448],
                "model_revision": f"rev-{engine}",
                "weights": "baked",
            }
        )
    worker = {
        "version": "0.1.0",
        "gpu": "NVIDIA L4",
        "device": "cuda",
        "dtype": "bfloat16",
        "cold_start": cold,
        "weights": {"qari_v03": {"origin": "baked", "source": "a/b", "revision": "rev-qari_v03"}},
    }
    return {"schema": 1, "worker": worker, "results": results}


def completed(output: dict, job_id: str = "sync-1", delay: int = 420, execution: int = 6830) -> dict:
    return {
        "id": job_id,
        "status": "COMPLETED",
        "delayTime": delay,
        "executionTime": execution,
        "workerId": "w-1",
        "output": output,
    }


class Endpoint:
    """A Runpod endpoint as `httpx.MockTransport` serves it. Scripted replies are used first, in order: a dict
    is a 200 JSON body, an int a bare status code, a (code, headers) tuple a status with headers, an exception
    is raised (a transport error); with no script left, /runsync completes with `reader(input)`."""

    def __init__(self, runsync=None, status=None, reader=None) -> None:
        self.runsync = list(runsync or [])
        self.status = list(status or [])
        self.reader = reader or (lambda job_input: worker_output(job_input.get("tasks") or []))
        self.requests: list[httpx.Request] = []
        self.inputs: list[dict] = []

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/runsync"):
            body = json.loads(request.content)
            self.inputs.append(body)
            reply = self.runsync.pop(0) if self.runsync else completed(self.reader(body["input"]))
        elif "/status/" in path:
            reply = self.status.pop(0)
        elif "/cancel/" in path:
            reply = {"id": path.rsplit("/", 1)[1], "status": "CANCELLED"}
        elif path.endswith("/health"):
            reply = {
                "jobs": {"inQueue": 0, "inProgress": 0, "completed": 3, "failed": 0},
                "workers": {"idle": 1},
            }
        else:
            reply = 404
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": f"HTTP {reply}"})
        if isinstance(reply, tuple):
            code, headers = reply
            return httpx.Response(code, headers=headers, json={})
        return httpx.Response(200, json=reply)


def make_client(endpoint: Endpoint, clock: Clock | None = None, **limits) -> runpod.RunpodClient:
    config = runpod.Config(
        api_key="rp-test-key",
        endpoint_url=ENDPOINT_URL,
        timeout_s=limits.get("timeout_s", 600.0),
        sync_wait_s=limits.get("sync_wait_s", 90.0),
        retries=limits.get("retries", 4),
        execution_timeout_s=limits.get("execution_timeout_s", 300),
    )
    clock = clock or Clock()
    return runpod.RunpodClient(
        config, transport=httpx.MockTransport(endpoint), sleep=clock.sleep, clock=clock
    )


@pytest.fixture
def page(db):
    """The page of ocr/tests.py: a gray and a B&W image, regions added by `add_regions`."""
    book = Book.objects.create(title="كتاب", status=Book.Status.OCR)
    page = Page.objects.create(book=book, number=1, source_index=0, status=Page.Status.LAYOUT_DONE)
    pre = Preprocess.objects.create(page=page, output_width=W, output_height=H)
    save_array(pre.gray_image, np.full((H, W), 230, dtype=np.uint8), "gray.png")
    save_array(pre.bw_image, np.full((H, W), 255, dtype=np.uint8), "bw.png")
    pre.save()
    return page


@pytest.fixture
def runpod_settings(settings):
    settings.NASSAKH = {
        **settings.NASSAKH,
        "OCR_BACKEND": "runpod",
        "RUNPOD_API_KEY": "rp-test-key",
        "RUNPOD_ENDPOINT_ID": "ep123",
        "RUNPOD_ENDPOINT_URL": "",
    }
    return settings


@pytest.fixture
def crop(tmp_path) -> Path:
    """A gray crop with a gradient, as `run_full_ocr` writes them."""
    pixels = np.tile(np.linspace(30, 240, 120, dtype=np.uint8), (100, 1))
    path = tmp_path / "gray-0-body.png"
    path.write_bytes(to_png_bytes(pixels))
    return path


def read(client: runpod.RunpodClient, crop: Path, engines=("qari_v03", "qari_v02"), trace=None):
    return client.read(
        crop,
        [(engine, 2500) for engine in engines],
        prompt=PROMPT_QARI,
        min_pixels=settings.NASSAKH["MIN_PIXELS"],
        max_pixels=settings.NASSAKH["MAX_PIXELS"],
        trace=trace,
    )


# ---------------------------------------------------------------- the request


def test_the_crop_is_sent_as_the_local_engines_would_read_it(crop):
    """The image is resized here exactly as `prepare_image` does for the local engines, and a gray crop goes
    as one channel whose RGB is those very pixels."""
    sent = runpod.encode_image(crop, settings.NASSAKH["MIN_PIXELS"], settings.NASSAKH["MAX_PIXELS"])
    local, original, resized = prepare_image(
        crop, settings.NASSAKH["MAX_PIXELS"], settings.NASSAKH["MIN_PIXELS"]
    )
    image = Image.open(io.BytesIO(base64.b64decode(sent["data_b64"])))
    assert image.mode == "L" and image.size == resized
    assert image.convert("RGB").tobytes() == local.tobytes()
    assert sent["resized"] is True and sent["original_size"] == [120, 100] and sent["format"] == "png"
    assert (sent["width"], sent["height"]) == resized and resized[0] % 28 == 0 and resized[1] % 28 == 0


def test_a_read_sends_schema_1_and_logs_the_request(db, crop):
    endpoint = Endpoint()
    trace = {"book": 31, "page": 44, "page_id": None, "region": "body", "variant": "gray"}
    answer = read(make_client(endpoint), crop, trace=trace)

    request = endpoint.requests[0]
    assert request.method == "POST" and request.url.path == "/v2/ep123/runsync"
    assert request.url.params["wait"] == "90000"
    assert request.headers["authorization"] == "Bearer rp-test-key"
    body = endpoint.inputs[0]
    assert body["policy"] == {"executionTimeout": 300_000, "ttl": 660_000}
    job = body["input"]
    assert job["schema"] == 1 and job["prompt"] == PROMPT_QARI
    assert job["tasks"] == [
        {"engine": "qari_v03", "max_new_tokens": 2500},
        {"engine": "qari_v02", "max_new_tokens": 2500},
    ]
    assert job["min_pixels"] == settings.NASSAKH["MIN_PIXELS"] and job["image"]["resized"] is True
    assert job["trace"] == {"book": 31, "page": 44, "region": "body", "variant": "gray"}

    first = answer.tasks["qari_v03"]
    assert (
        first.ok and first.text == "قراءة qari_v03" and first.output_tokens == 12 and first.duration_s == 1.5
    )
    assert first.extra["model_revision"] == "rev-qari_v03" and first.extra["weights"] == "baked"
    row = RemoteCall.objects.get()
    assert first.extra["remote"]["call"] == row.pk
    assert row.status == RemoteCall.Status.OK and row.operation == RemoteCall.Operation.READ
    assert row.engines == "qari_v03+qari_v02" and row.job_id == "sync-1" and row.job_status == "COMPLETED"
    assert (row.queue_ms, row.execution_ms, row.http_status, row.attempts) == (420, 6830, 200, 1)
    assert row.gpu == "NVIDIA L4" and row.cold_start is False and row.region_kind == "body"
    assert row.finished_at is not None and row.total_ms is not None
    assert "data_b64" not in json.dumps(row.request) and row.request["image"]["b64_bytes"] > 0
    assert "prompt" not in row.request
    assert [r.get("chars") for r in row.response["results"]] == [len("قراءة qari_v03"), len("قراءة qari_v02")]
    assert "قراءة" not in json.dumps(row.response, ensure_ascii=False)  # the texts stay in the OcrRuns


def test_a_cold_start_is_polled_on_status_until_the_job_ends(db, crop):
    clock = Clock()
    done = completed(worker_output([{"engine": "qari_v03"}], cold=True), job_id="sync-9", delay=31_000)
    endpoint = Endpoint(
        runsync=[{"id": "sync-9", "status": "IN_QUEUE"}],
        status=[{"id": "sync-9", "status": "IN_PROGRESS"}, done],
    )
    answer = read(make_client(endpoint, clock), crop, engines=("qari_v03",))
    assert answer.tasks["qari_v03"].ok and answer.remote["cold_start"] is True
    assert endpoint.paths() == ["/v2/ep123/runsync", "/v2/ep123/status/sync-9", "/v2/ep123/status/sync-9"]
    assert clock.sleeps == [0.5, 0.75]
    row = RemoteCall.objects.get()
    assert row.polls == 2 and row.attempts == 1 and row.cold_start is True and row.queue_ms == 31_000


def test_a_job_that_outlives_the_timeout_is_cancelled(db, crop):
    clock = Clock()
    queued = {"id": "sync-7", "status": "IN_QUEUE"}
    endpoint = Endpoint(runsync=[queued], status=[queued] * 50)
    with pytest.raises(runpod.RemoteTimeout, match="RUNPOD_TIMEOUT_S"):
        read(make_client(endpoint, clock, timeout_s=10.0, sync_wait_s=5.0), crop)
    assert endpoint.paths()[-1] == "/v2/ep123/cancel/sync-7"
    assert endpoint.requests[0].url.params["wait"] == "5000"
    row = RemoteCall.objects.get()
    assert row.status == RemoteCall.Status.TIMEOUT and row.job_id == "sync-7" and "أُلغيت" in row.error


def test_transient_failures_are_retried_with_backoff_and_retry_after(db, crop):
    clock = Clock()
    refused = httpx.ConnectError("connection refused")
    endpoint = Endpoint(runsync=[503, (429, {"Retry-After": "7"}), refused])
    answer = read(make_client(endpoint, clock), crop)
    assert answer.tasks["qari_v02"].ok
    assert clock.sleeps == [1.0, 7.0, 4.0]
    row = RemoteCall.objects.get()
    assert row.status == RemoteCall.Status.OK and row.attempts == 4 and row.http_status == 200


def test_retries_run_out(db, crop):
    clock = Clock()
    endpoint = Endpoint(runsync=[503, 503, 503, 503])
    with pytest.raises(runpod.RemoteError, match="HTTP 503"):
        read(make_client(endpoint, clock), crop)
    assert len(endpoint.requests) == 4 and clock.sleeps == [1.0, 2.0, 4.0]
    row = RemoteCall.objects.get()
    assert row.status == RemoteCall.Status.ERROR and row.http_status == 503 and row.attempts == 4


@pytest.mark.parametrize(
    ("code", "setting"), [(401, "RUNPOD_API_KEY"), (403, "RUNPOD_API_KEY"), (404, "RUNPOD_ENDPOINT_ID")]
)
def test_an_error_retrying_cannot_mend_is_not_retried(db, crop, code, setting):
    clock = Clock()
    endpoint = Endpoint(runsync=[code])
    with pytest.raises(runpod.RemoteError, match=setting):
        read(make_client(endpoint, clock), crop)
    assert len(endpoint.requests) == 1 and clock.sleeps == []
    assert RemoteCall.objects.get().status == RemoteCall.Status.ERROR


def test_a_failed_job_carries_the_workers_message(db, crop):
    detail = json.dumps({"error_type": "InvalidRequest", "error_message": "unsupported schema 2"})
    endpoint = Endpoint(runsync=[{"id": "sync-3", "status": "FAILED", "error": detail, "executionTime": 12}])
    with pytest.raises(runpod.RemoteJobFailed, match="unsupported schema 2"):
        read(make_client(endpoint), crop)
    row = RemoteCall.objects.get()
    assert row.status == RemoteCall.Status.FAILED and row.job_status == "FAILED" and row.execution_ms == 12


def test_an_answer_in_another_schema_fails_the_request(db, crop):
    endpoint = Endpoint(runsync=[completed({"schema": 2, "results": []})])
    with pytest.raises(runpod.RemoteJobFailed, match="schema 2"):
        read(make_client(endpoint), crop)
    assert RemoteCall.objects.get().status == RemoteCall.Status.FAILED


def test_one_model_failing_inside_a_job_is_a_partial_answer(db, crop):
    endpoint = Endpoint(
        reader=lambda job: worker_output(
            job["tasks"], errors={"qari_v02": "RuntimeError: CUDA out of memory"}
        )
    )
    answer = read(make_client(endpoint), crop)
    assert answer.tasks["qari_v03"].ok
    assert not answer.tasks["qari_v02"].ok and "CUDA out of memory" in answer.tasks["qari_v02"].error
    row = RemoteCall.objects.get()
    assert row.status == RemoteCall.Status.PARTIAL and row.error.startswith("qari_v02: RuntimeError")

    every = Endpoint(
        reader=lambda job: worker_output(job["tasks"], errors={t["engine"]: "boom" for t in job["tasks"]})
    )
    read(make_client(every), crop)
    assert RemoteCall.objects.order_by("-id").first().status == RemoteCall.Status.FAILED


def test_the_api_log_never_stops_a_reading(db, crop, monkeypatch):
    def refuse(self, *args, **kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(RemoteCall, "save", refuse)
    answer = read(make_client(Endpoint()), crop)
    assert answer.tasks["qari_v03"].ok and answer.remote["call"] is None


def test_a_ping_and_the_health(db):
    client = make_client(Endpoint())
    ping = client.ping(trace={"source": "test"})
    assert ping.tasks == {} and ping.worker["gpu"] == "NVIDIA L4"
    row = RemoteCall.objects.get()
    assert row.operation == RemoteCall.Operation.PING and row.status == RemoteCall.Status.OK
    assert client.health()["workers"] == {"idle": 1}


# ---------------------------------------------------------------- settings, the engine and the registry


def test_the_runpod_backend_needs_an_endpoint_and_a_key(settings):
    settings.NASSAKH = {
        **settings.NASSAKH,
        "OCR_BACKEND": "runpod",
        "RUNPOD_API_KEY": "",
        "RUNPOD_ENDPOINT_ID": "",
    }
    engine = registry.build_engine("qari_v03")
    assert isinstance(engine, QariRunpodEngine) and engine.backend == "runpod"
    assert engine.is_prepared() is False
    with pytest.raises(runpod.RemoteConfigError, match="RUNPOD_ENDPOINT_ID ولا RUNPOD_API_KEY"):
        engine.load()
    settings.NASSAKH = {**settings.NASSAKH, "RUNPOD_API_KEY": "k", "RUNPOD_ENDPOINT_ID": "ep9"}
    engine.load()
    assert engine.is_loaded and engine.is_prepared() and engine.model_revision == ""
    params = engine.generation_params(2500)
    assert "model_dir" not in params and params["endpoint"] == "https://api.runpod.ai/v2/ep9"
    assert params["max_new_tokens"] == 2500 and engine.batch_key == "runpod:https://api.runpod.ai/v2/ep9"
    settings.NASSAKH = {**settings.NASSAKH, "RUNPOD_ENDPOINT_URL": "http://localhost:8010/"}
    assert runpod.Config.from_settings().endpoint_url == "http://localhost:8010"


def test_together_asks_engines_that_share_a_request_once_and_leaves_the_others_alone(tmp_path):
    class Pairable(FakeEngine):
        batch_key = "runpod:x"

        def __init__(self, name, log):
            super().__init__(name=name)
            self.log = log

        def read_together(self, source, names, max_new_tokens, trace):
            self.log.append(("read", self.name, tuple(names), max_new_tokens, trace))

        def forget_together(self):
            self.log.append(("forget", self.name))

    log: list = []
    fakes = {"a": Pairable("a", log), "b": Pairable("b", log), "local": FakeEngine(name="local")}
    with registry.override(fakes):
        with registry.together(["a", "b", "local"], tmp_path / "x.png", 99, {"page": 3}):
            assert log == [("read", "a", ("a", "b"), 99, {"page": 3})]
    assert log[-1] == ("forget", "a") and fakes["local"].calls == []


def test_a_recognize_outside_a_pair_asks_for_its_model_alone(db, runpod_settings, crop):
    endpoint = Endpoint()
    with runpod.use_client(make_client(endpoint)):
        result = QariRunpodEngine("qari_v02").recognize(crop, 1000)
    assert result.text == "قراءة qari_v02" and result.extra["model_revision"] == "rev-qari_v02"
    assert endpoint.inputs[0]["input"]["tasks"] == [{"engine": "qari_v02", "max_new_tokens": 1000}]


# ---------------------------------------------------------------- the pipeline on Runpod


def _remote_engines() -> dict:
    fakes = engines()
    fakes["qari_v03"] = QariRunpodEngine("qari_v03")
    fakes["qari_v02"] = QariRunpodEngine("qari_v02")
    return fakes


def _reader(job_input: dict) -> dict:
    """The worker reading Nassakh's test page: what each model reads in each region (a ping reads nothing)."""
    region = (job_input.get("trace") or {}).get("region")

    def text(engine: str) -> str:
        if region == "body":
            return PRIMARY_BODY if engine == "qari_v03" else SECONDARY_BODY
        if region == "footnote":
            return FOOT
        return "٨"

    return worker_output(job_input.get("tasks") or [], text=text)


def test_run_full_ocr_on_runpod_reads_each_region_with_both_models_in_one_request(page, runpod_settings):
    add_regions(page)
    endpoint = Endpoint(reader=_reader)
    with registry.override(_remote_engines()), runpod.use_client(make_client(endpoint)):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    page.refresh_from_db()

    assert page.status == Page.Status.OCR_DONE
    assert page.final_text == "قال الأمير في سنة 1966 إن الكتاب مفيد\nوهذا سطر ثانٍ من المتن\n\n(1) حاشية أولى"
    jobs = [body["input"] for body in endpoint.inputs]
    assert [job["trace"]["region"] for job in jobs] == ["page_number", "body", "footnote"]
    assert all([t["engine"] for t in job["tasks"]] == ["qari_v03", "qari_v02"] for job in jobs)
    assert [job["tasks"][0]["max_new_tokens"] for job in jobs] == [16, 2500, 1000]
    assert jobs[2]["image"]["original_size"] == [
        W * 2,
        60 * 2,
    ]  # the footnote at 2x, as the local engines get it

    body = page.ocr_runs.get(engine_name="qari_v03", region__kind="body")
    assert body.backend == "runpod" and body.model_revision == "rev-qari_v03" and body.prompt == PROMPT_QARI
    assert body.duration_ms == 1500 and body.output_tokens == 12 and body.params["sanity"]["ok"] is True
    assert body.params["endpoint"] == ENDPOINT_URL and "model_dir" not in body.params
    secondary = page.ocr_runs.get(engine_name="qari_v02", region__kind="body")
    assert body.params["remote"]["call"] == secondary.params["remote"]["call"]  # one request, two runs
    calls = RemoteCall.objects.order_by("id")
    assert [c.region_kind for c in calls] == ["page_number", "body", "footnote"]
    assert {c.status for c in calls} == {RemoteCall.Status.OK} and {c.page_id for c in calls} == {page.pk}


def test_a_refused_request_is_recorded_on_both_runs_and_the_page_says_why(page, runpod_settings):
    add_regions(page)
    endpoint = Endpoint(runsync=[401] * 10)
    with registry.override(_remote_engines()), runpod.use_client(make_client(endpoint)):
        services.run_fast_ocr(page)
        with pytest.raises(services.OcrError, match="RUNPOD_API_KEY"):
            services.run_full_ocr(page)
    runs = page.ocr_runs.filter(engine_name__in=["qari_v03", "qari_v02"], region__kind="body")
    assert runs.count() == 2 and {r.status for r in runs} == {OcrRun.Status.ERROR}
    assert all("RUNPOD_API_KEY" in r.error for r in runs)
    assert len(endpoint.requests) == 3  # one request per region read, both runs answered from it


def test_the_pieces_of_a_failed_region_are_read_together(page, runpod_settings, monkeypatch):
    """D90 on Runpod: both models read each piece in one request; each model's pieces are one run."""
    add_regions(page)
    pre = page.preprocess
    pre.line_boxes = [{"x0": 0, "y0": y, "x1": W, "y1": y + 6} for y in (24, 34, 46, 56)]
    pre.save()
    monkeypatch.setattr(services, "PIECE_MIN_BANDS", 4)
    monkeypatch.setattr(services, "PIECE_LINES", 2)
    first, second = "قال الأمير في سنة ١٩٦٦ إن الكتاب مفيد", "وهذا سطر ثانٍ من المتن"

    def reader(job_input: dict) -> dict:
        variant = job_input["trace"]["variant"]
        if job_input["trace"]["region"] == "body" and variant == "gray":
            out = worker_output(job_input["tasks"], text=PRIMARY_BODY)
            for result in out["results"]:
                result.update(output_tokens=2500, finish="length")  # both models loop on the whole region
            return out
        texts = {"gray_p0": first, "gray_p1": second}
        return worker_output(job_input["tasks"], text=texts.get(variant, FOOT))

    endpoint = Endpoint(reader=reader)
    with registry.override(_remote_engines()), runpod.use_client(make_client(endpoint)):
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    page.refresh_from_db()
    assert page.final_text.startswith("قال الأمير في سنة 1966 إن الكتاب مفيد\nوهذا سطر ثانٍ من المتن")
    variants = [body["input"]["trace"]["variant"] for body in endpoint.inputs]
    assert variants.count("gray_p0") == 1 and variants.count("gray_p1") == 1
    for engine in ("qari_v03", "qari_v02"):
        joined = page.ocr_runs.filter(engine_name=engine, region__kind="body").order_by("-id").first()
        assert joined.input_variant == "gray_pieces" and joined.parsed_text == first + "\n" + second


# ---------------------------------------------------------------- the API log in Django admin


def test_the_api_log_lists_and_sums_the_requests(admin_client, page, settings):
    settings.NASSAKH = {**settings.NASSAKH, "RUNPOD_PRICE_PER_S": 0.0002}
    common = {"engines": "qari_v03+qari_v02", "page": page, "region_kind": "body", "variant": "gray"}
    ok = RemoteCall.objects.create(
        **common,
        status="ok",
        execution_ms=6000,
        queue_ms=400,
        total_ms=7000,
        cold_start=True,
        gpu="NVIDIA L4",
    )
    RemoteCall.objects.create(**common, status="partial", execution_ms=4000, queue_ms=100, total_ms=5000)
    RemoteCall.objects.create(**common, status="error", error="رفض Runpod مفتاح الـ API (HTTP 401)")

    from ocr.admin import runpod_summary

    summary = runpod_summary(RemoteCall.objects.all())
    assert (summary["calls"], summary["ok"], summary["partial"], summary["failed"]) == (3, 1, 1, 1)
    assert summary["gpu_s"] == 10.0 and summary["pages"] == 1 and summary["gpu_s_per_page"] == 10.0
    assert summary["cost"] == 0.002 and summary["cold"] == 1

    listing = admin_client.get(reverse("admin:ocr_remotecall_changelist"))
    assert listing.status_code == 200
    html = listing.content.decode()
    assert "ملخّص الطلبات المعروضة" in html and "NVIDIA L4" in html and "0.002 $" in html
    detail = admin_client.get(reverse("admin:ocr_remotecall_change", args=[ok.pk]))
    assert detail.status_code == 200 and "NVIDIA L4" in detail.content.decode()
    assert admin_client.get(reverse("admin:ocr_remotecall_add")).status_code == 403


# ---------------------------------------------------------------- the commands and the gpu worker


def test_runpod_check_reports_the_worker_and_reads_a_region(page, runpod_settings, capsys):
    add_regions(page)
    with runpod.use_client(make_client(Endpoint(reader=_reader))):
        call_command("runpod_check", "--book", str(page.book_id), "--page", "1")
    out = capsys.readouterr().out
    assert "endpoint  https://api.runpod.ai/v2/ep123" in out and "workers idle 1" in out
    assert "on NVIDIA L4" in out and "qari_v03: baked" in out
    assert "read      p1 body: qari_v03 12 tokens" in out
    assert RemoteCall.objects.filter(operation="read", region_kind="body").count() == 1


def test_runpod_compare_measures_runpod_against_the_stored_readings(page, runpod_settings, capsys, tmp_path):
    add_regions(page)
    with registry.override(engines()):  # the local readings
        services.run_fast_ocr(page)
        services.run_full_ocr(page)
    page.refresh_from_db()

    def reader(job_input: dict) -> dict:  # Runpod reads the body one word differently
        out = _reader(job_input)
        if job_input["trace"]["region"] == "body":
            out["results"][0]["text"] = PRIMARY_BODY.replace("مفيد", "مفيدة")
        return out

    report = tmp_path / "compare.json"
    with runpod.use_client(make_client(Endpoint(reader=reader))):
        call_command(
            "runpod_compare", "--book", str(page.book_id), "--price-per-second", "0.0002", "--json", report
        )
    out = capsys.readouterr().out
    assert "qari_v03" in out and "qari_v02" in out and "GPU 13.7 s" in out and "for 800 pages" in out
    rows = json.loads(report.read_text())["rows"]
    body = next(r for r in rows if r["engine"] == "qari_v03" and r["region"] == "body")
    assert body["identical"] is False and 0 < body["difference"] < 0.05
    assert next(r for r in rows if r["engine"] == "qari_v02" and r["region"] == "body")["identical"] is True


def test_the_gpu_worker_pool_follows_the_backend():
    root = Path(settings.BASE_DIR)
    makefile = (root / "Makefile").read_text()
    assert "ifeq ($(OCR_BACKEND),runpod)" in makefile and "-P threads -c $(GPU_THREADS)" in makefile
    assert "GPU_POOL := -P solo -c 1" in makefile
    assert "gpu-worker: make gpu-worker" in (root / "Procfile").read_text()
    if shutil.which("make") is None:
        pytest.skip("make is not installed")

    def command(backend: str) -> str:
        run = subprocess.run(
            ["make", "-n", "gpu-worker", f"OCR_BACKEND={backend}"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        return run.stdout

    assert "-Q gpu -P threads -c 4" in command("runpod")
    assert "-Q gpu -P solo -c 1" in command("mlx")
