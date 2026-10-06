# Nassakh, one image for every service: web (gunicorn), the cpu and gpu Celery workers, the MCP server, and the
# one-off commands (migrate, collectstatic, createsuperuser, ...). docker-compose.yml chooses the command.
# docs/DEPLOY.md. Built on the server (x86_64): `docker compose build`.
#
# Stages:
#   frontend  Node: Tailwind CSS, the TipTap editor bundle, vendored Alpine.js and fonts (Node is build-time only)
#   pydeps    the main virtualenv (/opt/venv): pyproject's dependencies without the local OCR stack
#             (with --build-arg OCR_STACK=torch: CPU-only PyTorch too, for docker-compose.local.yml)
#   kraken    Kraken's own Python 3.11 environment (/opt/kraken, CPU-only torch) and its model (/opt/models)
#   app       python:3.13-slim + Tesseract + WeasyPrint's libraries, the two environments, the code, a non-root user

# ---------------------------------------------------------------------------------------------- frontend
FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

# ---------------------------------------------------------------------------------------------- pydeps
FROM python:3.13-slim-trixie AS pydeps
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_NO_CACHE=1
RUN pip install --no-cache-dir uv==0.9.27
WORKDIR /build
COPY pyproject.toml ./
COPY deploy/server_requirements.py deploy/constraints.txt ./deploy/
# torch, transformers and the rest of the local OCR stack are left out: OCR_BACKEND=runpod reads pages on a
# Runpod GPU and nothing imports them otherwise (deploy/server_requirements.py). Wheels only: no compiler here.
RUN uv venv /opt/venv \
 && python deploy/server_requirements.py > requirements.txt \
 && uv pip install --python /opt/venv/bin/python --only-binary :all: -r requirements.txt -c deploy/constraints.txt
# OCR_STACK=torch adds the local OCR stack for a machine without Runpod (docs/RUN_LOCALLY.md, Path B): CPU-only
# torch (PyPI's Linux wheels carry CUDA, several GB) and transformers, at the versions of the owner's .venv.
# The default `runpod` leaves the image as it was: production does not change.
ARG OCR_STACK=runpod
RUN if [ "$OCR_STACK" = "torch" ]; then \
        uv pip install --python /opt/venv/bin/python --index-url https://download.pytorch.org/whl/cpu \
            "torch==2.14.0" "torchvision==0.29.0" \
     && uv pip install --python /opt/venv/bin/python --only-binary :all: -c deploy/constraints.txt \
            "transformers==5.17.0" "accelerate==1.15.0" "safetensors==0.8.0" "huggingface_hub==1.32.0" \
     && /opt/venv/bin/python -c "import torch, transformers; assert torch.version.cuda is None, 'a CUDA build of torch'"; \
    elif [ "$OCR_STACK" != "runpod" ]; then echo "OCR_STACK must be runpod or torch, not $OCR_STACK" >&2; exit 1; \
    fi

# ---------------------------------------------------------------------------------------------- kraken
# Kraken 6 pins torch <= 2.9 and numpy 2.0, so it keeps its own environment on Python 3.11 (`make kraken`);
# the project calls it as a subprocess (ocr/engines/kraken.py). uv fetches the Python 3.11 build.
FROM python:3.13-slim-trixie AS kraken
ENV UV_LINK_MODE=copy UV_NO_CACHE=1 UV_PYTHON_INSTALL_DIR=/opt/uv-python UV_PYTHON_PREFERENCE=only-managed
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv==0.9.27
RUN uv venv /opt/kraken --python 3.11
# CPU-only torch first (PyPI's Linux wheels carry CUDA, several GB), then Kraken, which finds torch in place.
RUN uv pip install --python /opt/kraken/bin/python --index-url https://download.pytorch.org/whl/cpu \
        "torch==2.9.0" "torchvision==0.24.0" \
 && uv pip install --python /opt/kraken/bin/python "kraken>=6.0.3,<7" Pillow \
 && /opt/kraken/bin/python -c "import torch, kraken; assert torch.version.cuda is None, 'a CUDA build of torch'"
# The model: «Printed Arabic-Script Base Model Trained on the OpenITI Corpus» (Zenodo 7050270, 2022, CC0),
# the file `make kraken` downloads. Checked against its SHA-256.
ARG KRAKEN_MODEL_URL="https://zenodo.org/records/7050270/files/all_arabic_scripts.mlmodel?download=1"
ARG KRAKEN_MODEL_SHA256=1e74031608c37ab99ae4f9d5227aef7782de63fac50f5f2ef3256165db834f9f
RUN mkdir -p /opt/models/kraken \
 && curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors -o /opt/models/kraken/all_arabic_scripts.mlmodel "$KRAKEN_MODEL_URL" \
 && echo "$KRAKEN_MODEL_SHA256  /opt/models/kraken/all_arabic_scripts.mlmodel" | sha256sum -c -

# ---------------------------------------------------------------------------------------------- app
FROM python:3.13-slim-trixie AS app

# Tesseract with ara + eng (the same tessdata_fast models as the Mac), WeasyPrint's libraries (Pango, Cairo,
# GDK-Pixbuf, MIME types), fontconfig with one fallback face (Amiri is vendored in static/fonts), gosu to step
# down from root in the `init` service.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-ara tesseract-ocr-eng \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 libcairo2 libgdk-pixbuf-2.0-0 shared-mime-info \
        fontconfig fonts-dejavu-core libgomp1 gosu \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 nassakh \
 && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin nassakh

COPY --from=pydeps /opt/venv /opt/venv
COPY --from=kraken /opt/uv-python /opt/uv-python
COPY --from=kraken /opt/kraken /opt/kraken
COPY --from=kraken /opt/models /opt/models

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DJANGO_SETTINGS_MODULE=nassakh.settings \
    MEDIA_ROOT=/data/media \
    OCR_BACKEND=runpod \
    KRAKEN_PYTHON=/opt/kraken/bin/python \
    KRAKEN_MODEL=/opt/models/kraken/all_arabic_scripts.mlmodel

# The code belongs to root and is read-only for the app user. The writable places are volumes: MEDIA_ROOT,
# STATIC_ROOT (collectstatic) and /data/models (the local OCR models of docker-compose.local.yml); created here,
# owned by the app user, so a new volume takes that ownership (the `init` service fixes it for a volume made
# another way).
WORKDIR /app
COPY . /app
COPY --from=frontend /build/static /app/static
RUN mkdir -p /data/media /data/models /app/staticfiles \
 && chown nassakh:nassakh /data/media /data/models /app/staticfiles \
 && chmod 0755 /app/deploy/entrypoint.sh /app/deploy/init.sh \
 && (python -m compileall -q /app || true)

# A smoke test of the two things that cannot be told from a green build: Kraken loads its model, and the app
# starts without torch. (The model is read before any page: an empty page list is a complete request.)
RUN echo '{"model": "'"$KRAKEN_MODEL"'", "pages": []}' | "$KRAKEN_PYTHON" -P ocr/engines/kraken_runner.py \
 && SECRET_KEY=build-check python manage.py check

USER nassakh
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
CMD ["gunicorn", "nassakh.wsgi:application", "-c", "/app/deploy/gunicorn.conf.py"]
