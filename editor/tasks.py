"""Editor tasks (default queue): `reassemble_chapter` runs one queued chapter re-assembly (D41);
`plan_review_changes` runs one queued comparison of review changes with the edited book (D78).

The tasks call `editor.services.run_chapter_reassembly` / `run_changes_plan`, which record every failure on
their row (Arabic headline, manuscript untouched), so the tasks themselves never raise for a pipeline error.
"""

from __future__ import annotations

from celery import shared_task

from . import services


@shared_task
def reassemble_chapter(run_id: int) -> int:
    """Re-assemble the chapter of run `run_id` (idempotent: a run that is not queued any more is skipped)."""
    services.run_chapter_reassembly(run_id)
    return run_id


@shared_task
def plan_review_changes(plan_id: int) -> int:
    """Compare the review changes with the edited book for plan `plan_id` (idempotent: a plan that is not
    queued any more is skipped)."""
    services.run_changes_plan(plan_id)
    return plan_id
