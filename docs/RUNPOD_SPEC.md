# Runpod spec: Qari on a Runpod Serverless GPU (`OCR_BACKEND=runpod`)

Status: built 2026-10-01 on branch `claude/tender-galileo-9wkpuq`, with the worker in its own repository,
`alibenmussa/nassakh-qari-worker` (branch `main`). **Not the default.** `OCR_BACKEND` stays `mlx` or `torch` until
the owner's measurements (§8) pass. How to run it is in `docs/RUNBOOK.md` §18.

The goal is the SaaS phase for the first one to three publishers. The two Qari models (about 9 GB of weights,
most of a page's time) move to a rented GPU that costs nothing while idle. Everything else stays where it is:
on the Mac now, on the app server later.

## 0. Decisions (to be recorded as D93 in `docs/DECISIONS.md`, after D92)

- **A third backend, nothing else moves.** `OCR_BACKEND=runpod` puts Qari v0.3 and v0.2 behind the Runpod endpoint
  of `nassakh-qari-worker` (`ocr/engines/qari_runpod.py`). Tesseract, Kraken, the alignment, D90's re-reading in
  pieces, the footnotes, review and assembly stay local. `torch` and `mlx` are unchanged.
- **The worker is its own repository.** It is a CUDA amd64 image with its own dependencies and release cycle.
  In Nassakh it is cloned at `workers/qari/`, which Nassakh's git ignores. The one thing both sides share is
  request schema 1 (§2); a change to it raises `schema` on both sides together.
- **The same pixels.** Nassakh resizes each crop with `core.images.prepare_image`, as the local engines do, and
  sends it already resized. The worker only decodes it, then reads as `qari_torch.py` reads: the same chat
  template and processor limits, and greedy decoding.
- **One request per region for both models** (`registry.together`, §4). This covers the region reads, the
  page-number vote and D90's pieces.
- **`/runsync`, then `/status`.** A job still queued or running when `/runsync` returns (a cold start) is polled.
  At the deadline it is cancelled. Transport errors, 429 and 5xx are retried with backoff.
- **Every request is logged.** It is a `RemoteCall` row, the API log in Django admin, with Runpod's timing and
  the worker's GPU, cold start and versions. The OcrRun gets the worker's `model_revision` and `params["remote"]`.
- **The gpu worker pool follows the backend.** It is a threads pool (`GPU_THREADS`, 4) with `runpod`, and the
  solo process otherwise (`make gpu-worker`).
- **The weights.**
  - Qari v0.3 is public: it is baked into the image.
  - Qari v0.2 is the owner's merged folder (D7). The owner uploads it to a **private** Hugging Face repository,
    and Runpod serves it as the endpoint's cached model (one cached model per endpoint).
  - The token lives in Runpod's settings, never in code.

## 1. Scope

Built:

- `ocr/runpod.py`: the client.
- `ocr/engines/qari_runpod.py`: the engine.
- `ocr/engines/registry.py`: the `runpod` branch and `together`.
- `ocr/services.py`: `_read_models`, and `_fill_run` taking the revision.
- `ocr/models.py`: `RemoteCall`, with migration `0005`.
- `ocr/admin.py` and `templates/admin/ocr/remotecall/change_list.html`: the API log.
- `manage.py runpod_check` and `runpod_compare`.
- Settings and `.env.example`; `httpx` as a dependency.
- `Makefile` and `Procfile`: the pool.
- `.gitignore`: `workers/qari/`.
- Tests: `ocr/test_runpod.py`.
- The worker repository.

Out of scope:

- Kraken on a server.
- vLLM (a later speed-up, once accuracy is settled).
- Batching several regions in one job.
- The app server's own deployment.

## 2. Request schema 1

Every job is `POST /runsync` with `{"input": …, "policy": {"executionTimeout", "ttl"}}`. Its `input`:

| Field | Meaning |
|---|---|
| `schema` | `1` |
| `image.format`, `image.data_b64` | a PNG, one channel for a gray crop (R = G = B, so its RGB is exactly the local engines') |
| `image.width`, `image.height` | its size, already resized to the Qwen2-VL rule |
| `image.resized` | `true`: the worker must not resize again |
| `image.original_size` | the crop's size before the resize (reported back as `image_size`, as the local engines do) |
| `prompt` | `PROMPT_QARI`, from Nassakh (D10) |
| `min_pixels`, `max_pixels` | `NASSAKH["MIN_PIXELS"]`, `NASSAKH["MAX_PIXELS"]` |
| `tasks` | `[{"engine": "qari_v03", "max_new_tokens": 2500}, {"engine": "qari_v02", …}]` |
| `trace` | `{"book", "page", "region", "variant"}`, for the worker's log only |

A ping, `{"schema": 1, "ping": true}`, reads nothing. It starts a worker and reports it.

The job's `output` is `{"schema": 1, "worker": {…}, "results": [one per task, in order]}`.

- **A result** is either:
  - `{"engine", "status": "ok", "text", "output_tokens", "finish", "duration_s", "prompt_tokens", "image_size", "resized_to", "model_revision", "weights"}`;
  - or `{"engine", "status": "error", "error"}`.
- **`finish: "length"`** means the reading hit the cap, usually a loop. `parse_output` and the loop check
  handle it as for the local engines.
- **`worker`** has:
  - `version`, `gpu`, `capability`, `device`, `dtype`;
  - `torch`, `cuda`, `transformers`;
  - `load_s`, `cold_start`;
  - `weights`: `{engine: {source, origin, revision}}`, where origin is `directory | baked | runpod_cache | download`.
- **An input outside the schema** raises in the worker, and Runpod marks the job `FAILED`.
- **A model that cannot read** answers its own task, so the other model's reading still arrives.

The worker's `worker/contract.py` is the reference implementation.

## 3. The client (`ocr/runpod.py`)

| Step | Behaviour |
|---|---|
| Submit | `POST {endpoint}/runsync?wait={RUNPOD_SYNC_WAIT_S × 1000}`, `Authorization: Bearer {RUNPOD_API_KEY}`; payload refused here above 19 MB (Runpod: 20 MB) |
| Policy | `executionTimeout` = `RUNPOD_EXECUTION_TIMEOUT_S` × 1000; `ttl` = (`RUNPOD_TIMEOUT_S` + 60) × 1000, so a job nobody waits for any more is not run later at a cost |
| Cold start | status `IN_QUEUE` / `IN_PROGRESS` → `GET /status/{id}` every 0.5 s, growing ×1.5 to 5 s |
| Deadline | `RUNPOD_TIMEOUT_S` from the submit: `POST /cancel/{id}`, `RemoteTimeout` |
| Retried | transport errors, 429, 500, 502, 503, 504: backoff 1, 2, 4… s (≤ 30), `Retry-After` honoured, up to `RUNPOD_RETRIES` attempts |
| Not retried | 400; 401 / 403 (names `RUNPOD_API_KEY`); 404 (names `RUNPOD_ENDPOINT_ID`, or an expired job on `/status`); 413 |
| Job outcome | `COMPLETED` → per-task answers; `FAILED` / `CANCELLED` / `TIMED_OUT` → `RemoteJobFailed` with the worker's own message; an answer that is not schema 1 → `RemoteJobFailed` |

- **Messages.** Every error message is Arabic first, technical detail on the next line. It reaches `OcrRun.error`,
  the page's error and the API log.
- **Shared client.** One `httpx.Client` serves the gpu worker's threads, and is rebuilt when the settings
  change.
- **Settings.** `RUNPOD_ENDPOINT_URL` replaces `RUNPOD_BASE_URL/RUNPOD_ENDPOINT_ID`, for the worker's local
  server.

| Setting | Default | Meaning |
|---|---|---|
| `RUNPOD_API_KEY` | — | Runpod console, Settings, API Keys |
| `RUNPOD_ENDPOINT_ID` | — | the endpoint's ID |
| `RUNPOD_ENDPOINT_URL` | — | another address (`http://localhost:8010`, the worker's local server) |
| `RUNPOD_BASE_URL` | `https://api.runpod.ai/v2` | |
| `RUNPOD_TIMEOUT_S` | 600 | one request, cold start included |
| `RUNPOD_SYNC_WAIT_S` | 90 | how long `/runsync` waits (1–300) |
| `RUNPOD_RETRIES` | 4 | attempts per HTTP exchange |
| `RUNPOD_EXECUTION_TIMEOUT_S` | 300 | a job's time on the GPU |
| `RUNPOD_PRICE_PER_S` | 0 | price of a GPU second, for the API log's estimate (24 GB GPUs: 0.00019) |

## 4. One request for both models

`services._read_models(page, names, target, path, variant, cap, scale)` runs the engines of `names` on one crop
inside `registry.together(names, path, cap, trace)`:

1. `together` finds the engines with a `batch_key` (the Runpod engines share `runpod:<endpoint>`).
2. It asks the first of them, `read_together` → `runpod.prefetch`, which sends one request for all of them.
   Each answer, or the request's error, is kept per thread.
3. `run_engine` then runs each engine as before. `QariRunpodEngine.recognize` takes its answer (`runpod.take`),
   or raises the kept error, which is recorded on its OcrRun.
4. At the end of the block, what nobody took is dropped.

Local and fake engines have no `batch_key`: they read one after the other exactly as before. The existing tests
pass unchanged.

Where it is used:

- `run_full_ocr`: each region's primary and secondary.
- `_vote_page_number`: the 4× crop with a 16-token cap.
- `_read_in_pieces` (D90): each piece read by both models, then each model's pieces joined into one run
  (`_join_pieces`).

A model read alone outside `together` asks for its model alone (`runpod.read_one`).

`_fill_run` takes `model_revision` out of a result's `extra` into the OcrRun's column. The rest of `extra` goes
to `params`:

- `device`, `dtype`, `prompt_tokens`, `image_size`, `resized_to`, `weights`;
- `remote`: `{call, job, queue_ms, execution_ms, total_ms, gpu, worker, cold_start}`.

The two runs of one request share `params["remote"]["call"]`, the API log row.

## 5. The API log (`ocr.RemoteCall`, Django admin «سجل طلبات Runpod»)

- **One row per request** (a read or a ping). It is written when the request leaves, so a cold start is
  visible while it waits, and completed when the request ends. A failed write is logged and never stops a
  reading.
- **Statuses:**
  - `running`;
  - `ok`;
  - `partial`: a model failed inside a completed job;
  - `failed`: the job failed, or every model in it did;
  - `timeout`;
  - `error`: refused or unreachable.

  A `running` row older than the timeout reads «لم يكتمل (توقّف العامل؟)».
- **Timing:**
  - `queue_ms` and `execution_ms` are Runpod's `delayTime` and `executionTime`;
  - `total_ms` is Nassakh's own wait, retries and polls included;
  - `attempts` and `polls` count the HTTP exchanges.
- **Request and response.** `request` keeps the request without the image (its base64 size instead). `response`
  keeps the answer without the texts (their length instead), which stay in the OcrRuns.
- **The admin page** is read-only, newest first, filterable by status, operation, cold start, GPU and region,
  and searchable by job, worker and error. Above the list, a summary of the filtered requests:
  - counts per outcome;
  - pages;
  - cold starts, with their total queue time;
  - GPU seconds, per page;
  - with `RUNPOD_PRICE_PER_S`, the estimated cost. This is a floor: Runpod also bills a worker's start and its
    idle timeout.

## 6. The gpu worker

`make gpu-worker` reads `OCR_BACKEND` from the shell or `.env`:

| Backend | Command |
|---|---|
| `mlx`, `torch` | `celery -A nassakh worker -Q gpu -P solo -c 1` (models resident, as before) |
| `runpod` | `celery -A nassakh worker -Q gpu -P threads -c $(GPU_THREADS)` (default 4) |

The `Procfile`'s `gpu-worker` runs `make gpu-worker`.

- **Throughput.** The threads are HTTP clients. Four pages read at once means up to four jobs on Runpod, which
  adds workers up to the endpoint's max workers.
- **Time limits.** Celery's hard time limit (`CELERY_TASK_TIME_LIMIT`) is not enforced in a threads pool. Each
  request is bounded by `RUNPOD_TIMEOUT_S` instead.

## 7. The worker (`alibenmussa/nassakh-qari-worker`, details in its README)

- **`handler.py`** loads both models when the worker starts. `worker.service.handle` serves each job. It never
  raises for a model problem; it answers per task.
- **`worker/images.py`** is a verbatim copy of `smart_resize`. It was checked identical to Nassakh's on
  200,000 random sizes.
- **`worker/qari.py`** reads as `qari_torch.py`. It checks that CUDA is available and that the GPU has bfloat16
  (Ampere or newer); otherwise every task says why.
- **`worker/weights.py`** looks for each model, in this order: a folder named by `QARI_*_SOURCE`, the baked
  folder (`/models/<engine>`), Runpod's cache (`/runpod-volume/huggingface-cache/hub`), then a download.
- **The image:**
  - `python:3.12-slim`;
  - torch 2.14.1 and torchvision 0.29.1 from PyPI. These are CUDA 13.0 wheels, so the endpoint's CUDA filter
    must be 13.0 and newer;
  - `transformers` and `pillow` pinned; both must match the Mac's;
  - Qari v0.3 baked in. About 9 GB, inside Runpod's GitHub build limits: a 30-minute Docker step, an 80 GB
    image.
- **Endpoint:**
  - queue-based;
  - GPU 24 GB first (L4, A5000, 3090), 16 GB second;
  - active workers 0, max 3;
  - idle timeout 60 s, execution timeout 300 s, FlashBoot on;
  - model: the private Qari v0.2 repository with a read-only token.
- **Tests:** 46 tests without PyTorch (a fake runner), and a GitHub workflow.

## 8. Before `OCR_BACKEND=runpod` becomes the default (the owner's measurements)

The local readings were written by MLX; the worker reads with PyTorch on CUDA, so the texts can differ. They
are measured, not assumed.

1. **Plumbing.** Run `manage.py runpod_check`. It reports the health and the ping: which GPU, where each model's
   weights came from, and at which revision. The revisions must be those of the Mac's
   `playground/poc/models/*/nassakh_info.json`.
2. **Accuracy.** Run `manage.py runpod_compare --book <id> --json <file>` on:
   - books made from the PoC samples (the 17 ground-truth pages, `playground/poc/input/`);
   - one full book (29 or 31).

   It reads every region again on Runpod and compares with the stored readings, which were validated against
   the ground truth in the PoC.

   Proposed gate, per engine:
   - the median difference is at most 0.5 %;
   - no more loops than locally;
   - every region above 5 % is checked against its scan, and Runpod is no worse there.
3. **Speed and cost.** Process one real book with `OCR_BACKEND=runpod` and `make gpu-worker` (threads). Then
   read:
   - pages per hour, on the dashboard;
   - GPU seconds and cost per page, and cold starts, in the API log's summary (`RUNPOD_PRICE_PER_S=0.00019` for
     24 GB GPUs);
   - against the Mac's ~20 s a page.

   `runpod_compare` prints the GPU seconds per page and the cost of 800 pages too. Plain PyTorch generation on
   an L4 may be slower per page than the M5. The gain is pages read in parallel, so it is the pages per hour
   that decide.
4. **Record the results** as a decision in `docs/DECISIONS.md`, then switch the default.

## 9. Tests (`ocr/test_runpod.py`, no network)

A fake endpoint (`httpx.MockTransport`) and a fake clock cover:

- **The request:** schema-1 request shape, headers, policy and wait; the crop sent with exactly the local
  engines' pixels.
- **Timing:** a cold start polled on `/status`; a timeout that cancels the job; retries with backoff and
  `Retry-After`; retries running out.
- **Failures:** 401, 403 and 404 not retried; a `FAILED` job's message; an answer in another schema; one model
  failing (`partial`) and both (`failed`); a log write that fails without stopping the reading.
- **Operations:** the ping and the health.
- **The engine's settings:** missing key or endpoint; `RUNPOD_ENDPOINT_URL`.
- **Pairing:** `together` pairs remote engines and leaves local ones alone; a lone `recognize`.
- **`run_full_ocr` on Runpod:** one request per region for both models, the footnote at 2×, the OcrRuns'
  backend, revision, timing and shared call. A refused request is recorded on both runs and the page error
  names `RUNPOD_API_KEY`.
- **D90 on Runpod:** pieces read together.
- **The admin page:** list, summary, detail, no add.
- **Commands:** `runpod_check`, and `runpod_compare` measuring a one-word difference.
- **The Makefile's pool**, through `make -n`.

## 10. Risks and open items

- **Numerical differences.** MLX against PyTorch on CUDA, and kernels, may change a few tokens. Measured by §8.
- **Version parity.** Worker transformers, Pillow and torch against the Mac's: the worker pins them, and its
  README says to keep them equal.
- **CUDA 13 wheels need recent drivers.** The endpoint's CUDA filter must say 13.0 and newer; otherwise the
  worker answers «CUDA is not available…» on every task.
- **A transport error during `/runsync`** can make the retry submit the job twice. It is rare, and only costs
  GPU seconds.
- **The threads pool** has no Celery hard time limit; each request is bounded by its own deadline.
- **Privacy.** Page crops leave the machine. This is fine for public-domain books, and should be stated in the
  publishers' terms.
- **Cold starts** cost the first page of a burst its load time. FlashBoot and the idle timeout trade that
  against idle seconds.
- **Runpod caches one model per endpoint.** Qari v0.3 is therefore baked into the image; if the image grows too
  big to build, both models can come from a network volume instead (`QARI_*_SOURCE` as folders).
