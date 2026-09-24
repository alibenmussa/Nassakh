# Import the Celery app so shared tasks are registered when Django starts.
from .celery import app as celery_app

__all__ = ("celery_app",)
