"""Celery application. Queues: `default` (CPU work) and `gpu` (OCR models, one solo worker)."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
# On macOS with Python 3.13 the prefork pool *spawns* its children (multiprocessing's default start
# method), so a child never inherits the worker's task optimisations and every task fails with
# "ValueError: not enough values to unpack (expected 3, got 0)" in celery.app.trace.fast_trace_task.
# With this variable set Celery redoes that setup inside the child (its documented workaround for
# the same situation on Windows). Harmless for the solo pool of the gpu worker.
os.environ.setdefault("FORKED_BY_MULTIPROCESSING", "1")

app = Celery("nassakh")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
