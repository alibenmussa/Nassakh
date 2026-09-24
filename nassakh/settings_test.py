"""Settings for the test suite: SQLite in memory, Celery eager, throw-away media directory."""

import tempfile

from .settings import *  # noqa: F401,F403

SECRET_KEY = "test-secret-key"
DEBUG = False

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

MEDIA_ROOT = tempfile.mkdtemp(prefix="nassakh-test-media-")

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Keep test output readable: only warnings and above.
LOGGING["root"]["level"] = "WARNING"  # noqa: F405
