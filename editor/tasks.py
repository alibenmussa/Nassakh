"""Editor task (default queue): `reassemble_chapter` runs one queued chapter re-assembly (D41).

The task calls `editor.services.run_chapter_reassembly`, which records every failure on the run (Arabic
headline, manuscript untouched), so the task itself never raises for a pipeline error.
"""

from __future__ import annotations

from celery import shared_task

from . import services


@shared_task
def reassemble_chapter(run_id: int) -> int:
    """Re-assemble the chapter of run `run_id` (idempotent: a run that is not queued any more is skipped)."""
    services.run_chapter_reassembly(run_id)
    return run_id
