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

MAILERS = {
    "default": {"BACKEND": "django.core.mail.backends.console.EmailBackend"},
}

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
    "OCR_BACKEND": env("OCR_BACKEND", default="torch"),  # torch | mlx
    "OCR_DEVICE": env("OCR_DEVICE", default="auto"),  # auto | mps | cpu
    "OCR_MODELS_DIR": Path(BASE_DIR, env("OCR_MODELS_DIR", default="playground/poc/models")).resolve(),
    "OCR_PRIMARY": env("OCR_PRIMARY", default="qari_v03"),
    "OCR_SECONDARY": env("OCR_SECONDARY", default="qari_v02"),
    "OCR_FAST": "tesseract",
    "TESSERACT_LANGS": env("TESSERACT_LANGS", default="ara+eng"),
    "RENDER_DPI": 300,
    "FOOTNOTE_UPSCALE": 2,
    "MAX_PIXELS": 2048 * 28 * 28,
    "MIN_PIXELS": 256 * 28 * 28,
    "MAX_NEW_TOKENS": {"page": 3000, "body": 2500, "footnote": 1000, "other": 600},
    # Word chooser for uncertain words during finalisation (D26): "none" disables the hook.
    "WORD_CHOOSER": env("WORD_CHOOSER", default="none"),
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
    # Render the edited chapter and the book in the background after editor saves (D44, debounced).
    "PREVIEW_AUTORENDER": env.bool("PREVIEW_AUTORENDER", default=True),
    # The numbers pass (D50): Kraken, in its own environment (`make kraken`), reads the Arabic-Indic
    # numbers of each finalised page of a book that prints them.
    "NUMBERS_PASS": env.bool("NUMBERS_PASS", default=True),
    "KRAKEN_PYTHON": Path(BASE_DIR, env("KRAKEN_PYTHON", default=".venv-kraken/bin/python")),
    "KRAKEN_MODEL": Path(BASE_DIR, env("KRAKEN_MODEL", default="models/kraken/all_arabic_scripts.mlmodel")),
    "KRAKEN_TIMEOUT_S": env.int("KRAKEN_TIMEOUT_S", default=120),
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
        "console": {"class": "logging.StreamHandler", "formatter": "console"},
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
            )
        },
        # WeasyPrint logs every layout step at INFO; its warnings (unsupported CSS) still show.
        "weasyprint": {"level": "WARNING", "propagate": True},
        "fontTools": {"level": "WARNING", "propagate": True},
    },
}
