"""Django settings for Nassakh.

Everything environment-specific is read from `.env` (django-environ). Nassakh-specific
knobs live under the single `NASSAKH` dict so services can read `settings.NASSAKH[...]`.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

# ---------------------------------------------------------------- core

SECRET_KEY = env("SECRET_KEY", default="django-insecure-dev-only-change-me")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# Behind a TLS proxy (production: Caddy, docs/DEPLOY.md). Everything here is off unless the environment turns
# it on, so the dev server on http keeps working.
# BEHIND_PROXY: the proxy ends TLS and says so in `X-Forwarded-Proto` (only the proxy can reach this server),
# so `request.is_secure()` is true and the links and redirects Django makes are https.
if env.bool("BEHIND_PROXY", default=False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Caddy hands the Host header on unchanged; turn this on only behind a proxy that rewrites it.
USE_X_FORWARDED_HOST = env.bool("USE_X_FORWARDED_HOST", default=False)
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=False)
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=False)
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=False)
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]  # the container health check speaks plain http
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=False)
SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=False)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "core",
    "accounts",
    "books",
    "processing",
    "ocr",
    "review",
    "assembly",
    "editor",
    "publishing",
    "research",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "nassakh.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.nav",
            ],
        },
    },
]

WSGI_APPLICATION = "nassakh.wsgi.application"

# ---------------------------------------------------------------- database

DATABASES = {"default": env.db("DATABASE_URL", default="postgres://localhost:5432/nassakh")}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------- auth

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "books:list"
LOGOUT_REDIRECT_URL = "accounts:login"

# D106: the login is the username (accounts made before) or the email (a sign-up's login).
AUTHENTICATION_BACKENDS = ["accounts.backends.EmailBackend"]  # the email is the login (D106)
# Sign-up (D106): pages a new account is given (0: none) and for how many days; days the confirmation link
# stays valid; the site's address for the links in emails (empty: the address of the request).
SIGNUP_PAGE_QUOTA = env.int("SIGNUP_PAGE_QUOTA", default=0)
SIGNUP_QUOTA_DAYS = env.int("SIGNUP_QUOTA_DAYS", default=30)
EMAIL_CONFIRM_DAYS = env.int("EMAIL_CONFIRM_DAYS", default=3)
SITE_URL = env("SITE_URL", default="")

# ---------------------------------------------------------------- i18n

LANGUAGE_CODE = "ar"
TIME_ZONE = env("TIME_ZONE", default="Africa/Tripoli")
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------- static & media

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
# A relative MEDIA_ROOT is resolved against the repository root; an absolute one is used as is.
MEDIA_ROOT = str(Path(BASE_DIR, env("MEDIA_ROOT", default="media")).resolve())

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# ---------------------------------------------------------------- email

# D106: the sign-up's confirmation email. EMAIL_BACKEND: the console in development (`runserver` prints the
# message), `django.core.mail.backends.smtp.EmailBackend` in production with the EMAIL_* values (Django 6.1's
# MAILERS: the old EMAIL_* settings must not be defined as settings next to it).
MAILERS = {
    "default": {
        "BACKEND": env("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend"),
        "OPTIONS": {
            "host": env("EMAIL_HOST", default="localhost"),
            "port": env.int("EMAIL_PORT", default=587),
            "username": env("EMAIL_HOST_USER", default=""),
            "password": env("EMAIL_HOST_PASSWORD", default=""),
            "use_tls": env.bool("EMAIL_USE_TLS", default=True),
            "use_ssl": env.bool("EMAIL_USE_SSL", default=False),
            "timeout": env.int("EMAIL_TIMEOUT", default=20),
        }
        if env("EMAIL_BACKEND", default="").endswith("smtp.EmailBackend")
        else {},
    },
}
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="نسّاخ <no-reply@localhost>")

# ---------------------------------------------------------------- celery

CELERY_BROKER_URL = env("REDIS_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = CELERY_BROKER_URL
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_ROUTES = {
    "ocr.tasks.ocr_page_full": {"queue": "gpu"},
    "ocr.tasks.warm_up_engines": {"queue": "gpu"},
    # D47: the fast re-layout of a chapter has its own queue (the default worker consumes default,layout)
    "publishing.tasks.relayout_chapter": {"queue": "layout"},
    "publishing.tasks.render_layout_images": {"queue": "default", "priority": 9},
    # Phase 6 (D58): exports have their own queue (the CPU worker consumes default,layout,export)
    "publishing.tasks.run_export": {"queue": "export"},
}
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = 3600
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TASK_EAGER_PROPAGATES = CELERY_TASK_ALWAYS_EAGER
CELERY_TIMEZONE = TIME_ZONE
CELERY_RESULT_EXPIRES = 24 * 3600

# ---------------------------------------------------------------- nassakh

NASSAKH = {
    "OCR_BACKEND": env("OCR_BACKEND", default="torch"),  # torch | mlx | runpod
    "OCR_DEVICE": env("OCR_DEVICE", default="auto"),  # auto | mps | cpu
    "OCR_MODELS_DIR": Path(BASE_DIR, env("OCR_MODELS_DIR", default="playground/poc/models")).resolve(),
    "OCR_PRIMARY": env("OCR_PRIMARY", default="qari_v03"),
    "OCR_SECONDARY": env("OCR_SECONDARY", default="qari_v02"),
    "OCR_FAST": "tesseract",
    "TESSERACT_LANGS": env("TESSERACT_LANGS", default="ara+eng"),
    # The PDF's own text layer as a book's text (born-digital books): off (owner review 2026-10-03, item 20),
    # the embedded Arabic text is often garbled. Off: the option is hidden at book creation and no book uses
    # it, whatever its `use_text_layer`; the text always comes from OCR. The code stays for a later return.
    "TEXT_LAYER": env.bool("TEXT_LAYER", default=False),
    # One pipeline run per page at a time (`books.runs`): hours after which a claim no longer blocks a new
    # run (a run whose queue messages were lost).
    "RUN_CLAIM_HOURS": env.int("RUN_CLAIM_HOURS", default=12),
    "RENDER_DPI": 300,
    "FOOTNOTE_UPSCALE": 2,
    "MAX_PIXELS": 2048 * 28 * 28,
    "MIN_PIXELS": 256 * 28 * 28,
    "MAX_NEW_TOKENS": {"page": 3000, "body": 2500, "footnote": 1000, "other": 600},
    # Qari on Runpod Serverless (OCR_BACKEND=runpod, docs/baseline/RUNPOD_SPEC.md): the API key and the endpoint of
    # nassakh-qari-worker (or RUNPOD_ENDPOINT_URL for another address: the worker's local server); seconds one
    # request may take with a cold start, seconds /runsync waits before the job is polled, attempts for a
    # transient failure, seconds a job may run on the GPU, and the price of a GPU second for the cost
    # estimate of the API log in Django admin (0: not shown).
    "RUNPOD_API_KEY": env("RUNPOD_API_KEY", default=""),
    "RUNPOD_ENDPOINT_ID": env("RUNPOD_ENDPOINT_ID", default=""),
    "RUNPOD_ENDPOINT_URL": env("RUNPOD_ENDPOINT_URL", default=""),
    "RUNPOD_BASE_URL": env("RUNPOD_BASE_URL", default="https://api.runpod.ai/v2"),
    "RUNPOD_TIMEOUT_S": env.int("RUNPOD_TIMEOUT_S", default=600),
    # D104: a job waiting for a GPU (none free) may wait this long; then the page goes back to the queue,
    # RUNPOD_REQUEUE_S later, at most RUNPOD_REQUEUE_TIMES times, before it is an error
    "RUNPOD_QUEUE_WAIT_S": env.int("RUNPOD_QUEUE_WAIT_S", default=1800),
    "RUNPOD_REQUEUE_S": env.int("RUNPOD_REQUEUE_S", default=300),
    "RUNPOD_REQUEUE_TIMES": env.int("RUNPOD_REQUEUE_TIMES", default=6),
    "RUNPOD_SYNC_WAIT_S": env.int("RUNPOD_SYNC_WAIT_S", default=90),
    "RUNPOD_RETRIES": env.int("RUNPOD_RETRIES", default=4),
    "RUNPOD_EXECUTION_TIMEOUT_S": env.int("RUNPOD_EXECUTION_TIMEOUT_S", default=300),
    "RUNPOD_PRICE_PER_S": env.float("RUNPOD_PRICE_PER_S", default=0.0),
    # Word chooser for uncertain words during finalisation (D26): "vote" (D71) puts the reading Tesseract
    # backs into the text and leaves the word open; "none" disables the hook.
    "WORD_CHOOSER": env("WORD_CHOOSER", default="vote"),
    # Book faces not vendored (D45) are looked up in these folders (publishing.fonts).
    "FONT_DIRS": env.list(
        "FONT_DIRS",
        default=[
            "~/Library/Fonts",
            "/Library/Fonts",
            "/System/Library/Fonts/Supplemental",
            "/System/Library/Fonts",
        ],
    ),
    # D98: the name of the first organisation (created by the migration or the first book; renamed on its
    # page) and the largest font file an organisation may upload (MB).
    "ORGANIZATION_NAME": env("ORGANIZATION_NAME", default="المؤسسة"),
    "ORG_FONT_MAX_MB": env.int("ORG_FONT_MAX_MB", default=20),
    # Render the edited chapter and the book in the background after editor saves (D44, debounced).
    "PREVIEW_AUTORENDER": env.bool("PREVIEW_AUTORENDER", default=True),
    # The numbers pass (D50): Kraken, in its own environment (`make kraken`), reads the Arabic-Indic
    # numbers of each finalised page of a book that prints them.
    "NUMBERS_PASS": env.bool("NUMBERS_PASS", default=True),
    "CALLS_PASS": env.bool("CALLS_PASS", default=True),
    # D92: the models' words take their boxes from Kraken's reading of each region's printed lines (Tesseract
    # still gives the reference text, the provisional text and the third reading); off: Tesseract's boxes.
    "KRAKEN_BOXES": env.bool("KRAKEN_BOXES", default=True),
    "KRAKEN_PYTHON": Path(BASE_DIR, env("KRAKEN_PYTHON", default=".venv-kraken/bin/python")),
    "KRAKEN_MODEL": Path(BASE_DIR, env("KRAKEN_MODEL", default="models/kraken/all_arabic_scripts.mlmodel")),
    "KRAKEN_TIMEOUT_S": env.int("KRAKEN_TIMEOUT_S", default=120),
    # Exports (PHASE6_SPEC §6.4, D58): the export task's soft time limit (the hard one is 120 s more), the
    # finished files kept per book and format, and whether the check step validates a Word file against
    # the ECMA-376 schemas (the integrity checks always run).
    "EXPORT_SOFT_LIMIT_S": env.int("EXPORT_SOFT_LIMIT_S", default=1800),
    "EXPORTS_KEPT": env.int("EXPORTS_KEPT", default=5),
    "EXPORT_VALIDATE": env.bool("EXPORT_VALIDATE", default=True),
    # D107: days a page clip's signed link stays valid (an AI client shows it without a session).
    "CLIP_LINK_DAYS": env.int("CLIP_LINK_DAYS", default=7),
    # D108: the MCP server (`manage.py mcp_serve`): the interface and port it listens on, the /mcp URL clients
    # connect to (the snippets of «البحث والتحقق»; empty: http://localhost:<port>/mcp) and the calls an access
    # key may make a minute. Its clip and review links start with SITE_URL.
    "MCP_HOST": env("MCP_HOST", default="127.0.0.1"),
    "MCP_PORT": env.int("MCP_PORT", default=8001),
    "MCP_PUBLIC_URL": env("MCP_PUBLIC_URL", default=""),
    "MCP_RATE_LIMIT": env.int("MCP_RATE_LIMIT", default=60),
}

# ---------------------------------------------------------------- rest framework

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
}

# ---------------------------------------------------------------- logging

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "console": {"format": "{asctime} {levelname:<8} {name} {message}", "style": "{"},
    },
    "handlers": {
        # stdout: `docker compose logs` reads it (gunicorn and Celery write their own lines to stderr)
        "console": {"class": "logging.StreamHandler", "formatter": "console", "stream": "ext://sys.stdout"},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {
        "django": {"level": "WARNING", "propagate": True},
        "django.request": {"level": "WARNING", "propagate": True},
        "celery": {"level": "INFO", "propagate": True},
        # Project apps log at INFO; the root handler prints them.
        **{
            name: {"level": "INFO", "propagate": True}
            for name in (
                "core",
                "accounts",
                "books",
                "processing",
                "ocr",
                "review",
                "assembly",
                "editor",
                "publishing",
                "research",
            )
        },
        # WeasyPrint logs every layout step at INFO; its warnings (unsupported CSS) still show.
        "weasyprint": {"level": "WARNING", "propagate": True},
        "fontTools": {"level": "WARNING", "propagate": True},
    },
}
