"""Assembly task (default queue): `assemble_book` runs one queued `AssemblyRun`.

The task calls `assembly.services.run_assembly`, which records every failure on the run (Arabic
headline, old manuscript kept), so the task itself never raises for a pipeline error.
"""

from __future__ import annotations

from celery import shared_task

from . import services


@shared_task
def assemble_book(run_id: int) -> int:
    """Assemble the book of run `run_id` (idempotent: a run that is not queued any more is skipped)."""
    services.run_assembly(run_id)
    return run_id
