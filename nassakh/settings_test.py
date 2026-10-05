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

# Removed when the test process exits (the TemporaryDirectory finaliser runs at interpreter exit).
_MEDIA_DIR = tempfile.TemporaryDirectory(prefix="nassakh-test-media-")
MEDIA_ROOT = _MEDIA_DIR.name

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Keep test output readable: only warnings and above.
LOGGING["root"]["level"] = "WARNING"  # noqa: F405

# Editor saves do not render previews in the background during tests (eager Celery would run them
# inline); the publishing tests call the renders, or turn this on, where they check them.
# Kraken's word boxes (D92) are off too: the tests that want them turn them on with a fake Kraken. The MCP's
# public URL is empty so a developer's .env (a tunnel, a domain) never changes what the tests see.
NASSAKH = {
    **NASSAKH,  # noqa: F405
    "PREVIEW_AUTORENDER": False,
    "NUMBERS_PASS": False,
    "KRAKEN_BOXES": False,
    "MCP_PUBLIC_URL": "",
}
