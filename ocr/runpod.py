"""Qari on Runpod Serverless (OCR_BACKEND=runpod): the client of the nassakh-qari-worker endpoint.

The worker (its own repository, alibenmussa/nassakh-qari-worker) runs Qari v0.3 and v0.2 on a Runpod GPU;
everything else stays here. One request carries one crop, resized exactly as the local engines resize it,
and a task per model (request schema 1, docs/RUNPOD_SPEC.md §2). `run_full_ocr` asks both models in one
request (`registry.together` → `QariRunpodEngine.read_together` → `prefetch`) and each engine's `recognize`
takes its own answer (`take`); a call outside such a pair asks for one model alone (`read_one`).

A request is POST /runsync?wait=…, which brings the job back when it ends within RUNPOD_SYNC_WAIT_S. A job
still queued or running then (a cold start: a worker starting and loading the models) is polled on /status
until it ends, and cancelled once RUNPOD_TIMEOUT_S has passed. Transport errors, 429 and 5xx are retried with
backoff (Retry-After honoured) up to RUNPOD_RETRIES attempts; 400, 401, 403, 404 and 413 are not. Each job
carries a policy: `executionTimeout` (RUNPOD_EXECUTION_TIMEOUT_S on the GPU) and a `ttl` just past the
client's own deadline, so a job nobody waits for any more is not read later at a cost.

Every request is a `RemoteCall` row, the API log in Django admin, written when the request leaves and
completed when it ends; a failed write is logged and never stops a reading.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.utils import timezone

import httpx
import numpy as np

from core.images import prepare_image, to_png_bytes

log = logging.getLogger(__name__)

SCHEMA = 1
DEFAULT_BASE_URL = "https://api.runpod.ai/v2"
PENDING: frozenset[str] = frozenset({"IN_QUEUE", "IN_PROGRESS"})
RETRY_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
MAX_PAYLOAD_BYTES = 19 * 1024 * 1024  # /runsync takes 20 MB
CONNECT_TIMEOUT_S = 10.0
REQUEST_TIMEOUT_S = 30.0  # every call but /runsync, which waits up to its `wait` and this much more
POLL_FIRST_S = 0.5
POLL_GROWTH = 1.5
POLL_MAX_S = 5.0
BACKOFF_MAX_S = 30.0
TTL_MARGIN_S = 60


class RemoteError(RuntimeError):
    """A request to the Runpod endpoint failed. The message is Arabic (it reaches the page and the API log),
    with the technical cause on its next line."""


class RemoteConfigError(RemoteError):
    """OCR_BACKEND=runpod without an endpoint or an API key."""


class RemoteJobFailed(RemoteError):
    """The worker failed the job, Runpod cancelled or timed it out, or its answer is not schema 1."""


class RemoteTimeout(RemoteError):
    """No answer within RUNPOD_TIMEOUT_S; the job was cancelled."""


# ---------------------------------------------------------------- settings


@dataclass(frozen=True)
class Config:
    """The endpoint and the limits of every request, from `settings.NASSAKH` (read at call time)."""

    api_key: str
    endpoint_url: str
    timeout_s: float
    sync_wait_s: float
    retries: int
    execution_timeout_s: int

    @classmethod
    def from_settings(cls) -> Config:
        cfg = settings.NASSAKH
        url = str(cfg.get("RUNPOD_ENDPOINT_URL") or "").strip().rstrip("/")
        endpoint_id = str(cfg.get("RUNPOD_ENDPOINT_ID") or "").strip()
        if not url and endpoint_id:
            url = f"{str(cfg.get('RUNPOD_BASE_URL') or DEFAULT_BASE_URL).strip().rstrip('/')}/{endpoint_id}"
        return cls(
            api_key=str(cfg.get("RUNPOD_API_KEY") or "").strip(),
            endpoint_url=url,
            timeout_s=max(10.0, float(cfg.get("RUNPOD_TIMEOUT_S") or 600)),
            sync_wait_s=min(300.0, max(1.0, float(cfg.get("RUNPOD_SYNC_WAIT_S") or 90))),
            retries=max(1, int(cfg.get("RUNPOD_RETRIES") or 4)),
            execution_timeout_s=max(5, int(cfg.get("RUNPOD_EXECUTION_TIMEOUT_S") or 300)),
        )

    @property
    def problem(self) -> str:
        """Why the backend cannot be used ('' when it can)."""
        missing = [
            name
            for name, value in (("RUNPOD_ENDPOINT_ID", self.endpoint_url), ("RUNPOD_API_KEY", self.api_key))
            if not value
        ]
        if missing:
            return (
                f"لم يُضبط {' ولا '.join(missing)} في ملف .env، وهو مطلوب مع OCR_BACKEND=runpod؛ "
                "انظر docs/RUNBOOK.md (القسم 18)."
            )
        return ""


# ---------------------------------------------------------------- answers


@dataclass
class TaskAnswer:
    """One model's answer in a request: its reading, or the error the worker gave for it. `extra` becomes the
    OcrRun's params through `services._fill_run` (`model_revision` goes to its own column)."""

    engine: str
    ok: bool
    text: str = ""
    output_tokens: int | None = None
    finish: str = ""
    duration_s: float = 0.0
    error: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Answer:
    """A finished request: one `TaskAnswer` per model asked, the worker's report and Runpod's timing."""

    tasks: dict[str, TaskAnswer]
    worker: dict
    remote: dict


# ---------------------------------------------------------------- the image


def encode_image(path: str | Path, min_pixels: int, max_pixels: int) -> dict:
    """The crop as schema 1 carries it: resized as the local engines resize it (`prepare_image`) and sent as
    a lossless PNG, one channel when it is gray (R = G = B: the worker's RGB is then the local engines')."""
    img, original, resized = prepare_image(path, max_pixels, min_pixels)
    pixels = np.asarray(img)
    if (
        pixels.ndim == 3
        and (pixels[..., 0] == pixels[..., 1]).all()
        and (pixels[..., 1] == pixels[..., 2]).all()
    ):
        img = img.convert("L")
    data = to_png_bytes(img)
    return {
        "format": "png",
        "data_b64": base64.b64encode(data).decode("ascii"),
        "width": int(resized[0]),
        "height": int(resized[1]),
        "resized": True,
        "original_size": [int(original[0]), int(original[1])],
    }


def _worker_trace(trace: dict | None) -> dict:
    """What the worker's log may know of a request: the book, page, region and variant."""
    keys = ("book", "page", "region", "variant", "source")
    return {k: trace[k] for k in keys if trace and k in trace and isinstance(trace[k], (str, int, float))}


# ---------------------------------------------------------------- the API log


class CallLog:
    """The `RemoteCall` row of one request. Logging never breaks a reading: a failed write is logged and the
    request goes on without its row."""

    def __init__(self, row) -> None:
        self.row = row

    @classmethod
    def start(cls, operation: str, engines: Sequence[str], trace: dict | None, endpoint: str, request: dict):
        from .models import RemoteCall

        trace = trace or {}
        row = RemoteCall(
            operation=operation,
            engines="+".join(engines)[:80],
            page_id=trace.get("page_id"),
            region_kind=str(trace.get("region") or "")[:20],
            variant=str(trace.get("variant") or "")[:20],
            endpoint=endpoint[:200],
            request=request,
        )
        try:
            row.save()
        except Exception:  # noqa: BLE001 - a log that cannot be written never stops a reading
            log.warning("runpod: the API log row could not be written", exc_info=True)
            row.pk = None
        return cls(row)

    @property
    def pk(self) -> int | None:
        return self.row.pk

    def count(self, attempt: bool = False, poll: bool = False, http_status: int | None = None) -> None:
        if attempt:
            self.row.attempts += 1
        if poll:
            self.row.polls += 1
        if http_status is not None:
            self.row.http_status = http_status

    def finish(
        self, status: str, total_ms: int, job: dict | None = None, error: str = "", response=None
    ) -> None:
        from .models import RemoteCall

        row = self.row
        job = job or {}
        worker = (job.get("output") or {}).get("worker") if isinstance(job.get("output"), dict) else None
        worker = worker if isinstance(worker, dict) else {}
        row.status = status
        row.finished_at = timezone.now()
        row.total_ms = max(0, int(total_ms))
        row.job_id = str(job.get("id") or row.job_id or "")[:80]
        row.job_status = str(job.get("status") or row.job_status or "")[:20]
        row.queue_ms = _int(job.get("delayTime"))
        row.execution_ms = _int(job.get("executionTime"))
        row.worker_id = str(job.get("workerId") or "")[:80]
        row.gpu = str(worker.get("gpu") or "")[:80]
        row.worker_version = str(worker.get("version") or "")[:20]
        row.cold_start = worker.get("cold_start") if isinstance(worker.get("cold_start"), bool) else None
        row.response = response if response is not None else {}
        row.error = error[:4000]
        if row.pk is None:
            return
        fields = [f.name for f in RemoteCall._meta.concrete_fields if f.name not in ("id", "created_at")]
        try:
            row.save(
                update_fields=[f for f in fields if f not in ("operation", "engines", "page", "request")]
            )
        except Exception:  # noqa: BLE001 - see `start`
            log.warning("runpod: the API log row %s could not be completed", row.pk, exc_info=True)


def _int(value) -> int | None:
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _response_summary(output: dict) -> dict:
    """The worker's answer as the API log keeps it: everything but the texts."""
    results = []
    for item in output.get("results") or []:
        if not isinstance(item, dict):
            continue
        kept = {k: v for k, v in item.items() if k != "text"}
        if "text" in item:
            kept["chars"] = len(item.get("text") or "")
        results.append(kept)
    return {"schema": output.get("schema"), "worker": output.get("worker") or {}, "results": results}


# ---------------------------------------------------------------- the client


def _http_message(response: httpx.Response, path: str, attempts: int) -> str:
    code = response.status_code
    body = (response.text or "").strip().replace("\n", " ")[:300]
    if code in (401, 403):
        headline = f"رفض Runpod مفتاح الـ API (HTTP {code})؛ تحقّق من RUNPOD_API_KEY."
    elif code == 404 and path.startswith("/status"):
        headline = "انتهت صلاحية المهمة في Runpod قبل أن تُقرأ نتيجتها (HTTP 404)."
    elif code == 404:
        headline = "لم يجد Runpod نقطة النهاية (HTTP 404)؛ تحقّق من RUNPOD_ENDPOINT_ID."
    elif code == 413:
        headline = "الصورة أكبر مما يقبله Runpod في طلب واحد (HTTP 413)."
    elif code == 429:
        headline = f"Runpod مشغول بطلبات كثيرة (HTTP 429) بعد {attempts} محاولات."
    elif code >= 500:
        headline = f"خطأ في خادم Runpod (HTTP {code}) بعد {attempts} محاولات."
    else:
        headline = f"رفض Runpod الطلب (HTTP {code})."
    return f"{headline}\n{path}: {body}" if body else headline


def _job_message(job: dict) -> str:
    status = str(job.get("status") or "?")
    detail = job.get("error")
    if isinstance(detail, str):
        try:  # Runpod's SDK reports a handler's exception as JSON (error_type, error_message, traceback)
            parsed = json.loads(detail)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("error_message"):
            detail = str(parsed["error_message"])
    detail = str(detail or "").strip()[:1000]
    headline = {
        "FAILED": "فشلت المهمة في عامل Runpod.",
        "CANCELLED": "أُلغيت المهمة في Runpod.",
        "TIMED_OUT": "تجاوزت المهمة مهلة التنفيذ في Runpod (RUNPOD_EXECUTION_TIMEOUT_S).",
    }.get(status, f"انتهت المهمة في Runpod بالحالة {status}.")
    return f"{headline}\n{detail}" if detail else headline


class RunpodClient:
    """HTTP client of one endpoint, shared by the gpu worker's threads (httpx.Client is thread-safe)."""

    def __init__(
        self,
        config: Config,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._sleep = sleep
        self._clock = clock
        self._http = httpx.Client(
            base_url=config.endpoint_url,
            headers={"Authorization": f"Bearer {config.api_key}", "Accept": "application/json"},
            timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------ operations

    def read(
        self,
        image_path: str | Path,
        tasks: Sequence[tuple[str, int]],
        *,
        prompt: str,
        min_pixels: int,
        max_pixels: int,
        trace: dict | None = None,
    ) -> Answer:
        """Read one crop with every (engine, max_new_tokens) of `tasks` in one job. Raises `RemoteError`; a
        model the worker could not run is an answer with `ok=False`, not an exception."""
        image = encode_image(image_path, min_pixels, max_pixels)
        payload = {
            "schema": SCHEMA,
            "image": image,
            "prompt": prompt,
            "min_pixels": int(min_pixels),
            "max_pixels": int(max_pixels),
            "tasks": [{"engine": engine, "max_new_tokens": int(cap)} for engine, cap in tasks],
            "trace": _worker_trace(trace),
        }
        summary = {
            **{k: v for k, v in payload.items() if k != "image"},
            "image": {k: v for k, v in image.items() if k != "data_b64"}
            | {"b64_bytes": len(image["data_b64"])},
        }
        summary.pop("prompt", None)
        return self._job("read", payload, summary, [engine for engine, _ in tasks], trace)

    def ping(self, trace: dict | None = None) -> Answer:
        """Start a worker (when none is warm) and report it; reads nothing."""
        payload = {"schema": SCHEMA, "ping": True, "trace": _worker_trace(trace)}
        return self._job("ping", payload, dict(payload), [], trace)

    def health(self) -> dict:
        """The endpoint's workers and jobs (`GET /health`); not logged."""
        return self._request("GET", "/health", deadline=self._clock() + REQUEST_TIMEOUT_S)

    # ------------------------------------------------------------ one job

    def _job(
        self, operation: str, payload: dict, summary: dict, engines: list[str], trace: dict | None
    ) -> Answer:
        call = CallLog.start(operation, engines, trace, self.config.endpoint_url, summary)
        started = self._clock()
        job: dict = {}
        try:
            job = self._run(payload, call)
            return self._answer(job, engines, call, int((self._clock() - started) * 1000))
        except Exception as exc:  # the row says how the request ended, whatever ended it
            from .models import RemoteCall

            if isinstance(exc, RemoteTimeout):
                status = RemoteCall.Status.TIMEOUT
            elif isinstance(exc, RemoteJobFailed):
                status = RemoteCall.Status.FAILED
            else:
                status = RemoteCall.Status.ERROR
            output = job.get("output") if isinstance(job.get("output"), dict) else None
            call.finish(
                status,
                int((self._clock() - started) * 1000),
                job,
                error=str(exc) if isinstance(exc, RemoteError) else f"{type(exc).__name__}: {exc}",
                response=_response_summary(output) if output else None,
            )
            raise

    def _run(self, payload: dict, call: CallLog) -> dict:
        """The finished job (any terminal status): /runsync, then /status while it is queued or running."""
        cfg = self.config
        deadline = self._clock() + cfg.timeout_s
        body = {
            "input": payload,
            "policy": {
                "executionTimeout": cfg.execution_timeout_s * 1000,
                "ttl": int((cfg.timeout_s + TTL_MARGIN_S) * 1000),
            },
        }
        content = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if len(content) > MAX_PAYLOAD_BYTES:
            size_mb = len(content) // (1024 * 1024)
            raise RemoteError(f"الطلب أكبر مما يقبله Runpod ({size_mb} م.ب، والحد 20 م.ب)؛ صغّر المنطقة.")
        wait_s = max(1.0, min(cfg.sync_wait_s, deadline - self._clock()))
        job = self._request(
            "POST",
            "/runsync",
            content=content,
            params={"wait": int(wait_s * 1000)},
            read_timeout=wait_s + REQUEST_TIMEOUT_S,
            deadline=deadline,
            call=call,
            submit=True,
        )
        job_id = str(job.get("id") or "")
        if job_id:
            call.row.job_id = job_id[:80]
        interval = POLL_FIRST_S
        while str(job.get("status") or "") in PENDING:
            if not job_id:
                raise RemoteError(f"ردّ Runpod بمهمة بلا رقم وهي {job.get('status')}.")
            if self._clock() + interval >= deadline:
                self._cancel(job_id)
                raise RemoteTimeout(
                    f"لم يردّ Runpod خلال {int(cfg.timeout_s)} ثانية (RUNPOD_TIMEOUT_S)؛ أُلغيت المهمة.\n"
                    f"job {job_id}: {job.get('status')}"
                )
            self._sleep(interval)
            interval = min(interval * POLL_GROWTH, POLL_MAX_S)
            job = self._request("GET", f"/status/{job_id}", deadline=deadline, call=call, poll=True)
        return job

    def _answer(self, job: dict, engines: list[str], call: CallLog, total_ms: int) -> Answer:
        from .models import RemoteCall

        status = str(job.get("status") or "")
        if status != "COMPLETED":
            raise RemoteJobFailed(_job_message(job))
        output = job.get("output")
        if not isinstance(output, dict) or output.get("schema") != SCHEMA:
            found = output.get("schema") if isinstance(output, dict) else type(output).__name__
            raise RemoteJobFailed(
                f"ردّ عامل Runpod بصيغة غير معروفة (schema {found}، والمنتظر {SCHEMA})؛ حدِّث العامل أو نسّاخ."
            )
        worker = output.get("worker") if isinstance(output.get("worker"), dict) else {}
        results = {r.get("engine"): r for r in output.get("results") or [] if isinstance(r, dict)}
        remote = {
            "call": call.pk,
            "job": str(job.get("id") or ""),
            "queue_ms": _int(job.get("delayTime")),
            "execution_ms": _int(job.get("executionTime")),
            "total_ms": total_ms,
            "gpu": str(worker.get("gpu") or ""),
            "worker": str(worker.get("version") or ""),
            "cold_start": worker.get("cold_start"),
        }
        tasks: dict[str, TaskAnswer] = {}
        for engine in engines:
            item = results.get(engine)
            if item is None:
                tasks[engine] = TaskAnswer(engine, ok=False, error="لم يردّ عامل Runpod على هذا النموذج.")
            elif item.get("status") != "ok":
                tasks[engine] = TaskAnswer(engine, ok=False, error=str(item.get("error") or "خطأ غير معروف"))
            else:
                tasks[engine] = TaskAnswer(
                    engine,
                    ok=True,
                    text=str(item.get("text") or ""),
                    output_tokens=_int(item.get("output_tokens")),
                    finish=str(item.get("finish") or "unknown"),
                    duration_s=float(item.get("duration_s") or 0.0),
                    extra={
                        "device": str(worker.get("device") or ""),
                        "dtype": str(worker.get("dtype") or ""),
                        "prompt_tokens": item.get("prompt_tokens"),
                        "image_size": item.get("image_size"),
                        "resized_to": item.get("resized_to"),
                        "model_revision": str(item.get("model_revision") or ""),
                        "weights": str(item.get("weights") or ""),
                        "remote": dict(remote),
                    },
                )
        failed = [answer for answer in tasks.values() if not answer.ok]
        if not failed:
            outcome, error = RemoteCall.Status.OK, ""
        elif len(failed) < len(tasks):
            outcome, error = RemoteCall.Status.PARTIAL, "\n".join(f"{a.engine}: {a.error}" for a in failed)
        else:
            outcome, error = RemoteCall.Status.FAILED, "\n".join(f"{a.engine}: {a.error}" for a in failed)
        call.finish(outcome, total_ms, job, error=error, response=_response_summary(output))
        return Answer(tasks=tasks, worker=worker, remote=remote)

    # ------------------------------------------------------------ HTTP

    def _request(
        self,
        method: str,
        path: str,
        *,
        deadline: float,
        call: CallLog | None = None,
        content: bytes | None = None,
        params: dict | None = None,
        read_timeout: float = REQUEST_TIMEOUT_S,
        submit: bool = False,
        poll: bool = False,
    ) -> dict:
        """One HTTP exchange, retried on transport errors, 429 and 5xx while attempts and time are left."""
        headers = {"Content-Type": "application/json"} if content is not None else None
        timeout = httpx.Timeout(read_timeout, connect=CONNECT_TIMEOUT_S)
        attempt = 0
        while True:
            attempt += 1
            if call is not None:
                call.count(attempt=submit, poll=poll)
            try:
                response = self._http.request(
                    method, path, content=content, params=params, headers=headers, timeout=timeout
                )
            except httpx.TransportError as exc:
                if attempt >= self.config.retries or self._clock() >= deadline:
                    raise RemoteError(
                        f"تعذّر الوصول إلى Runpod بعد {attempt} محاولات.\n{type(exc).__name__}: {exc}"
                    ) from exc
                self._sleep(self._backoff(attempt, None, deadline))
                continue
            if call is not None:
                call.count(http_status=response.status_code)
            if (
                response.status_code in RETRY_STATUSES
                and attempt < self.config.retries
                and self._clock() < deadline
            ):
                self._sleep(self._backoff(attempt, response, deadline))
                continue
            if response.status_code >= 400:
                raise RemoteError(_http_message(response, path, attempt))
            try:
                data = response.json()
            except ValueError as exc:
                raise RemoteError(
                    f"ردّ Runpod ليس JSON (HTTP {response.status_code}).\n{(response.text or '')[:300]}"
                ) from exc
            if not isinstance(data, dict):
                raise RemoteError(f"ردّ Runpod ليس كائن JSON (HTTP {response.status_code}).")
            return data

    def _backoff(self, attempt: int, response: httpx.Response | None, deadline: float) -> float:
        """Seconds before the next attempt: Retry-After when Runpod sends one, else 1, 2, 4… up to 30."""
        delay = min(BACKOFF_MAX_S, 2.0 ** (attempt - 1))
        if response is not None:
            try:
                delay = min(BACKOFF_MAX_S, max(0.0, float(response.headers.get("Retry-After", ""))))
            except ValueError:
                pass
        return max(0.0, min(delay, deadline - self._clock()))

    def _cancel(self, job_id: str) -> None:
        """Ask Runpod to drop a job nobody waits for any more (best effort)."""
        try:
            self._http.post(
                f"/cancel/{job_id}", timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
            )
        except httpx.HTTPError:
            log.warning("runpod: job %s could not be cancelled", job_id, exc_info=True)


# ---------------------------------------------------------------- the shared client


_lock = threading.Lock()
_shared: RunpodClient | None = None
_override: RunpodClient | None = None


def client() -> RunpodClient:
    """The client of the configured endpoint (built once per process, again when the settings change)."""
    global _shared
    if _override is not None:
        return _override
    config = Config.from_settings()
    if config.problem:
        raise RemoteConfigError(config.problem)
    with _lock:
        if _shared is None or _shared.config != config:
            if _shared is not None:
                _shared.close()
            _shared = RunpodClient(config)
        return _shared


@contextmanager
def use_client(replacement: RunpodClient) -> Iterator[RunpodClient]:
    """Serve `replacement` from `client()` inside the block (tests, management commands)."""
    global _override
    previous = _override
    _override = replacement
    try:
        yield replacement
    finally:
        _override = previous


# ---------------------------------------------------------------- one request for both models

# The answers `prefetch` received, per thread (the gpu worker reads several pages at once), until each
# engine's `recognize` takes its own.
_answers = threading.local()


def _store() -> dict:
    store = getattr(_answers, "store", None)
    if store is None:
        store = _answers.store = {}
    return store


def _key(engine: str, source: str | Path, max_new_tokens: int) -> tuple[str, str, int]:
    return engine, str(source), int(max_new_tokens)


def prefetch(
    source: str | Path,
    engines: Sequence[str],
    max_new_tokens: int,
    *,
    prompt: str,
    min_pixels: int,
    max_pixels: int,
    trace: dict | None = None,
) -> None:
    """Read `source` with every engine of `engines` in one request and keep each answer for its `recognize`.
    Never raises: a failed request is kept as each engine's error, so each run records it."""
    store = _store()
    store.clear()
    try:
        answer = client().read(
            source,
            [(engine, max_new_tokens) for engine in engines],
            prompt=prompt,
            min_pixels=min_pixels,
            max_pixels=max_pixels,
            trace=trace,
        )
    except Exception as exc:  # noqa: BLE001 - recorded on each engine's run by `run_engine`
        message = str(exc) if isinstance(exc, RemoteError) else f"{type(exc).__name__}: {exc}"
        for engine in engines:
            store[_key(engine, source, max_new_tokens)] = RemoteError(message)
        return
    for engine in engines:
        store[_key(engine, source, max_new_tokens)] = answer.tasks[engine]


def take(engine: str, source: str | Path, max_new_tokens: int) -> TaskAnswer | None:
    """`engine`'s answer from the last `prefetch` of this thread (raising that request's error), or None."""
    item = _store().pop(_key(engine, source, max_new_tokens), None)
    if isinstance(item, BaseException):
        raise item
    return item


def forget() -> None:
    """Drop the answers of this thread nobody took (the end of `registry.together`)."""
    _store().clear()


def read_one(
    source: str | Path,
    engine: str,
    max_new_tokens: int,
    *,
    prompt: str,
    min_pixels: int,
    max_pixels: int,
    trace: dict | None = None,
) -> TaskAnswer:
    """One model alone (a call outside `registry.together`)."""
    answer = client().read(
        source,
        [(engine, max_new_tokens)],
        prompt=prompt,
        min_pixels=min_pixels,
        max_pixels=max_pixels,
        trace=trace,
    )
    return answer.tasks[engine]
