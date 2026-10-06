# Run Nassakh on your own computer

This guide gives two ways to run Nassakh. Both read pages with the same two OCR models, Qari v0.3 and Qari v0.2.

| | Path A: Mac, native | Path B: Docker, CPU only |
|---|---|---|
| Computer | Mac with Apple Silicon (M1 or newer) | Linux, Windows or Intel Mac with Docker |
| OCR backend | MLX on the Apple GPU (`OCR_BACKEND=mlx`) | PyTorch on the CPU (`OCR_BACKEND=torch`) |
| Memory (RAM) | 16 GB or more (tested with 24 GB) | 16 GB or more (12 GB for Docker), or 8 GB with one model |
| Free disk | about 25 GB | about 20 GB |
| Download | 9 GB of models, about 1.5 GB of Python packages | 9 GB of models, about 2 GB for the images |
| Time for one page | about 20 s (the first page up to 2 minutes) | about 6 to 8 minutes on 2 CPU cores (**estimate**, Path B step 9) |
| Status | **verified** end to end on 2026-10-06 | image build, model download and one page read **verified**; the full stack **not verified** |

The production server uses a third backend, `runpod`. It needs a private GPU worker. Do not use it here.

Both paths use a sample book: `samples/alwaraqat-sample.pdf`. It has 2 pages of «الورقات» by الجويني. See `samples/README.md`.

---

## Path A: Mac with Apple Silicon (MLX)

Verified on 2026-10-06 with a fresh clone on an M5 Pro Mac (24 GB, macOS 26), Python 3.13, PostgreSQL 17, Redis 7, Tesseract 5.5:

- Steps 2, 3, 4 and 6 were run as written.
- Steps 5, 7 and 8 had small differences. Step 5: a scratch database name and `manage.py migrate` instead of `make db`. Step 7: the commands that `make worker` and `make gpu-worker` print with `make -n`, and the web server on port 8010. Step 8: `createsuperuser --noinput` instead of `make superuser`.
- The sample went through the web site (sign-in, upload, «بدء المعالجة») with the two workers of step 7. Both pages had their text after 65 s.
- The sample also went through `smoke_pipeline` (step 10).
- The tools of step 1 were already on the test Mac. Their install was not run again.
- The models were already in the Hugging Face cache. Thus the download time on the Mac was not measured (see Path B, step 4).

### 1. Install the tools

Install [Homebrew](https://brew.sh) first. Then run:

```sh
brew install uv redis tesseract tesseract-lang postgresql@17
brew services start postgresql@17
brew services start redis
export PATH="$(brew --prefix postgresql@17)/bin:$PATH"
```

- `uv` installs Python 3.13 for you. You do not need another Python.
- `tesseract-lang` gives Tesseract its Arabic model. Check with `tesseract --list-langs`. The list must show `ara` and `eng`.
- Put the `export PATH=...` line in `~/.zshrc`. Then `createdb` and `psql` work in a new terminal.
- Node is not necessary. The built CSS and JavaScript are in the repository (`static/dist`).

### 2. Get the code and the Python packages

```sh
git clone <repository URL> nassakh
cd nassakh
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r pyproject.toml --extra dev --extra mlx
```

The packages take about 1.4 GB on disk. The install took 58 s with a warm `uv` cache. The first install downloads about 1.5 GB.

### 3. Install Kraken

Kraken reads the Arabic-Indic numbers and gives the word boxes. It has its own Python 3.11 environment.

```sh
make kraken
```

This makes `.venv-kraken/` (about 830 MB) and downloads `models/kraken/all_arabic_scripts.mlmodel` (16 MB, CC0). It took 23 s.

### 4. Write the settings file

```sh
cp .env.example .env
```

Open `.env` in an editor. Change these lines:

```ini
SECRET_KEY=<a long random string>
DATABASE_URL=postgres://localhost:5432/nassakh
OCR_BACKEND=mlx
OCR_MODELS_DIR=models/qari
```

- Make a random string with `openssl rand -hex 32`.
- Use port `5432` for a new Homebrew PostgreSQL. Use another port only if your server listens on it.
- Keep the other lines as they are.

### 5. Make the database

```sh
make db
```

This creates the database `nassakh` and applies the migrations. The migrations took 37 s.

### 6. Download and prepare the models

```sh
.venv/bin/python manage.py prepare_models --mlx
```

This command does these steps:

1. It downloads Qari v0.3 (`NAMAA-Space/Qari-OCR-v0.3-VL-2B-Instruct`, 4.4 GB, Apache 2.0).
2. It downloads Qari v0.2 (`NAMAA-Space/Qari-OCR-0.2.2.1-VL-2B-Instruct`, a LoRA adapter, 0.12 GB, Apache 2.0).
3. It downloads the base model of v0.2 (`Qwen/Qwen2-VL-2B-Instruct`, 4.4 GB, Apache 2.0).
4. It merges the v0.2 adapter into the base model, on the CPU.
5. It converts both models for MLX, without quantisation.

| Item | Size |
|---|---|
| Download in total | 9.0 GB |
| Hugging Face cache (`~/.cache/huggingface`) | 8.9 GB |
| `models/qari/qari-v0.2-merged` | 4.4 GB |
| `models/qari/mlx/` (two models) | 8.8 GB |
| `models/qari/qari-v0.3` | 0 GB extra (a hard link to the cache) |

The download time depends on your connection. 9 GB takes about 12 minutes at 100 Mbit/s. The merge and the two conversions took 41 s on the test Mac. The converted models were identical in configuration and size to the owner's. The command can run again: it skips the finished steps.

After the command, you can delete the base model from the cache to free 4.4 GB:

```sh
rm -rf ~/.cache/huggingface/hub/models--Qwen--Qwen2-VL-2B-Instruct
```

### 7. Start Nassakh

Open three terminals in the `nassakh` folder. Run one command in each terminal:

```sh
make web          # the site on http://127.0.0.1:8000
make worker       # ingest, layout, Tesseract, Kraken, exports
make gpu-worker   # the two Qari models, loaded once (about 9 GB of memory)
```

A fourth terminal is optional: `make mcp` starts the MCP server on http://127.0.0.1:8001/mcp.

### 8. Make the first user

```sh
make superuser
```

- At `Username`, type your email address.
- At `Email address`, type the same email address.
- The email is the login (decision D106).

Open http://127.0.0.1:8000 and sign in with the email and the password.

### 9. Pages for your account

A superuser has no page limit. You can read books at once.

For other accounts:

1. Sign in as the superuser.
2. Open «الفوترة» (`/accounts/billing/`).
3. Open the account.
4. Give it pages, or set «رصيد غير محدود» (unlimited).

To give every new sign-up some pages, set `SIGNUP_PAGE_QUOTA=100` in `.env`. Sign-up sends a confirmation email. In development the email is printed in the `make web` terminal.

### 10. Read the sample book

In the browser:

1. On the books page, click «كتاب جديد».
2. Choose `samples/alwaraqat-sample.pdf`. Keep one page per sheet.
3. Click «استخراج الصفحات». Wait for «تم التخطيط».
4. Click «بدء المعالجة».
5. Wait until the book shows «جاهز للمراجعة». Open a page to see the text.

From the command line, without the workers:

```sh
CELERY_TASK_ALWAYS_EAGER=true .venv/bin/python manage.py smoke_pipeline samples/alwaraqat-sample.pdf
```

This command runs every stage in one process. It prints one line for each page. The result of the test run:

```text
book 1: needs_guides after «التخطيط» in 5 s
book 1: ready_for_review in 132 s
page  status        text         lines  runs  chars  flags / error
   1  ocr_done      final           15    12    740  single_reader
   2  ocr_done      final           18     7    851  missing_text
```

In this run the two pages took 127 s after «بدء المعالجة». Page 2 took 21 s. Page 1 took 105 s, because the first Kraken run starts its environment (55 s). Each model read the body of a page in 5 to 9 s. The models loaded in about 2 s.

The text of both pages was correct, with a few small errors. The flags (`single_reader`, `missing_text`) mark lines for the reviewer. They are not failures.

---

## Path B: any computer with Docker (CPU)

Status on 2026-10-06:

- **Verified** on a Linux x86_64 server (4 vCPU): the image build (step 3); `prepare_models` (step 4) and one page read with Qari v0.3 (step 9), each in a `docker run` container limited to 2 CPUs and 7 GB.
- **Verified**: `docker compose -f docker-compose.local.yml config` accepts the file.
- **Verified** outside Docker: the `web` command of the file (`runserver --insecure` with `DEBUG=false`) serves the CSS and the pages.
- **Not verified**: steps 5 to 8, the whole stack with `docker compose up` (no Docker on the test Mac; the server runs production).

### 1. Install Docker

- Windows and Mac: install Docker Desktop. In Settings, Resources, set Memory to 12 GB or more.
- Linux: install Docker Engine and the Compose plugin (`docker compose version` must work).

### 2. Get the code and write the settings file

```sh
git clone <repository URL> nassakh
cd nassakh
cp deploy/.env.local.example deploy/.env.local
```

Open `deploy/.env.local`. Set `SECRET_KEY` to a long random string. The containers do not start with `CHANGE_ME`.

### 3. Build the image

```sh
docker compose -f docker-compose.local.yml build
```

The build adds CPU-only PyTorch and transformers to the production image (build argument `OCR_STACK=torch`). The image is 4.6 GB. With cached base layers, the build took about 1 minute on the test server. A first build downloads more and takes longer (**not measured**).

### 4. Download and prepare the models

```sh
docker compose -f docker-compose.local.yml run --rm --no-deps gpu-worker python manage.py prepare_models
```

This downloads 9.0 GB and merges Qari v0.2 into its base model. The models go into the Docker volume `models` (12.4 GB). On the test server (a fast data-center link) it took 87 s. On a home connection the download takes longer: 9 GB takes about 12 minutes at 100 Mbit/s.

### 5. Start Nassakh

```sh
docker compose -f docker-compose.local.yml up -d
docker compose -f docker-compose.local.yml ps
```

Wait until `web` shows `healthy`. The first start runs the migrations.

### 6. Make the first user

```sh
docker compose -f docker-compose.local.yml exec web python manage.py createsuperuser
```

Type your email address at `Username` and at `Email address`.

### 7. Open the site

Open http://localhost:8000. Sign in with the email and the password. The MCP server is at http://localhost:8001/mcp.

Pages for accounts: do the same as Path A, step 9. Emails are printed in the log: `docker compose -f docker-compose.local.yml logs web`.

### 8. Read the sample book

Do the same as Path A, step 10, in the browser. The file is `samples/alwaraqat-sample.pdf` in your clone.

### 9. Time and memory on the CPU

Measured on the test server, in a container limited to 2 CPUs and 7 GB:

| Step | Result |
|---|---|
| Load Qari v0.3 (bfloat16) | 7.4 s |
| Read one full page of the sample with Qari v0.3 (453 tokens) | 169 s and 166 s (2.7 tokens per second) |
| Memory of the container while it reads (`docker stats`) | 5.4 GiB |
| Peak resident memory of the process (includes the memory-mapped weights file) | 7.2 GiB |
| A hard limit of 7 GiB, no swap | no out-of-memory stop |

The pipeline reads each region with two models, then runs Tesseract and Kraken. Thus one page takes about two such reads, plus the page number and the footnotes: about 6 to 8 minutes on 2 CPU cores (**estimate**). More cores make it faster (**not measured**).

On a Mac with Apple Silicon, use Path A. PyTorch on the Apple CPU was very slow in a test: 22 s for the page number alone.

Memory:

- One model fits in 7 GB (**verified**, table above).
- Both models (the default): the `gpu-worker` needs about 10 GB (**estimate**). Give Docker 12 GB or more.
- To keep one model in memory, set `OCR_SECONDARY=qari_v03` in `deploy/.env.local`. Each region is then read twice with the same model, so the time does not go down. Give Docker 8 GB or more (**not verified**).

Restart after a change to `deploy/.env.local`:

```sh
docker compose -f docker-compose.local.yml up -d --force-recreate
```

### 10. Stop and remove

```sh
docker compose -f docker-compose.local.yml down        # stops; keeps the books and the models
docker compose -f docker-compose.local.yml down -v     # also deletes the database, the books and the models
```

---

## Troubleshooting

| Problem | Cause and fix |
|---|---|
| A page shows «تعذّر تحميل محرّك التعرّف» or the log says `... is not prepared` | The models are missing. Run step 6 (Path A) or step 4 (Path B). Check `OCR_MODELS_DIR` in `.env`. |
| `NotSupportedError: PostgreSQL 15 or later is required` | Django 6.1 needs PostgreSQL 15 or newer. Install `postgresql@17`. Point `DATABASE_URL` at it. |
| `createdb: command not found` | `postgresql@17` is keg-only. Run the `export PATH=...` line of Path A, step 1. |
| `connection refused` on port 5432 | Start the server: `brew services start postgresql@17`. Check with `pg_isready -p 5432`. |
| `tesseract language(s) missing: ['ara']` | Run `brew install tesseract-lang`. |
| `No module named 'mlx_vlm'` | Install the `mlx` extra (Path A, step 2). MLX works only on Apple Silicon. On an Intel Mac, use Path B. |
| The containers stop with `SECRET_KEY is empty or still a placeholder` | Set `SECRET_KEY` in `deploy/.env.local`. |
| The `gpu-worker` container restarts, exit code 137 | It ran out of memory. Give Docker more memory, or set `OCR_SECONDARY=qari_v03` (Path B, step 9). |
| The first page is slow | The worker loads the models on the first page. The next pages are faster. |
| Every login fails the CSRF check | Open the site as `http://localhost:8000` or `http://127.0.0.1:8000`. For another address, add it to `CSRF_TRUSTED_ORIGINS`. |
| An upload is refused for pages | The account has no pages. See Path A, step 9. |

More details: `docs/RUNBOOK.md` (development on the Mac) and `docs/DEPLOY.md` (the production server).
