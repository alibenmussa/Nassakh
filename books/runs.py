"""One pipeline run per page at a time: the run claim (owner review 2026-10-03, item 29).

Every chain `books.services.run_stage` queues for a page (preprocess → layout → ocr_fast → ocr_full, or a
part of it) first claims the page: `Page.run_token` gets a fresh token and `Page.run_claimed_at` the time,
in one conditional UPDATE that only succeeds while no other run holds the page. A second click on a
re-run, a second tab or a second book re-run therefore queues nothing: the caller gets
`PAGE_RUN_ACTIVE_ERROR` / `book_run_active_message`. The token travels with every task of the chain
(`run=`); a task whose token is no longer the page's skips the page (a superseded run never works twice),
and the chain's last task (`last=True`) gives the claim back. A chain that dies outside its tasks' own
error handling (the hard time limit, a lost worker) is released by its errback
(`books.tasks.page_run_failed`).

A claim older than `NASSAKH["RUN_CLAIM_HOURS"]` (default 12) no longer blocks: a run whose messages were
lost (a purged queue) must not lock its page forever. Its tasks, if they ever arrive, still skip the page
once a newer run holds it. `manage.py release_page_runs` releases claims by hand.

Tasks queued without a token (the ingest chord, `processing.services.rerun_preprocess`, messages queued
before this change) run as before.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Q, QuerySet
from django.utils import timezone

from books.models import Page

log = logging.getLogger(__name__)

DEFAULT_CLAIM_HOURS = 12

PAGE_RUN_ACTIVE_ERROR = "تجري معالجة هذه الصفحة الآن؛ انتظر حتى تنتهي ثم أعد المحاولة."
BOOK_RUN_ACTIVE_ERROR = "تجري معالجة {count} من هذا الكتاب الآن؛ انتظر حتى تنتهي ثم أعد المحاولة."


def claim_hours() -> float:
    """Hours after which a claim no longer blocks (`NASSAKH["RUN_CLAIM_HOURS"]`, read at call time)."""
    try:
        hours = float(settings.NASSAKH.get("RUN_CLAIM_HOURS", DEFAULT_CLAIM_HOURS))
    except (TypeError, ValueError):
        hours = DEFAULT_CLAIM_HOURS
    return hours if hours > 0 else DEFAULT_CLAIM_HOURS


def _stale_before(now: datetime | None = None) -> datetime:
    return (now or timezone.now()) - timedelta(hours=claim_hours())


def active_q(now: datetime | None = None) -> Q:
    """Pages a run holds: a token, claimed within `claim_hours`."""
    return ~Q(run_token="") & Q(run_claimed_at__gte=_stale_before(now))


def free_q(now: datetime | None = None) -> Q:
    """Pages no live run holds (no token, or a claim past `claim_hours`)."""
    return Q(run_token="") | Q(run_claimed_at__isnull=True) | Q(run_claimed_at__lt=_stale_before(now))


def new_token() -> str:
    return uuid.uuid4().hex


def claim_page(page_id: int) -> str:
    """Claim one page for a new run; returns the token, or '' when another run holds the page."""
    token, now = new_token(), timezone.now()
    claimed = Page.objects.filter(free_q(now), pk=page_id).update(run_token=token, run_claimed_at=now)
    return token if claimed else ""


def claim_pages(pages: QuerySet[Page]) -> tuple[str, int]:
    """Claim every free page of `pages` with one token (a book re-run); returns `(token, count)`."""
    token, now = new_token(), timezone.now()
    count = pages.filter(free_q(now)).update(run_token=token, run_claimed_at=now)
    return token, count


def release(page_id: int, token: str) -> bool:
    """Give back the claim of the run `token`; False when the page holds no claim of that run."""
    if not token:
        return False
    return bool(Page.objects.filter(pk=page_id, run_token=token).update(run_token="", run_claimed_at=None))


def release_book(book_id: int, token: str) -> int:
    """Give back every claim the run `token` holds in the book (a book re-run that could not be queued)."""
    if not token:
        return 0
    return Page.objects.filter(book_id=book_id, run_token=token).update(run_token="", run_claimed_at=None)


def is_current(page_id: int, token: str) -> bool:
    """Whether a task of the run `token` may work on the page: always for a task queued without a token,
    else only while the page still holds that run's claim (a superseded or released run skips)."""
    if not token:
        return True
    return Page.objects.filter(pk=page_id, run_token=token).exists()


def active_token(page_id: int) -> str:
    """The token of the live run holding the page, '' when none."""
    return Page.objects.filter(active_q(), pk=page_id).values_list("run_token", flat=True).first() or ""


def is_active(page_id: int) -> bool:
    return bool(active_token(page_id))


def active_count(pages: QuerySet[Page]) -> int:
    """How many of `pages` a live run holds."""
    return pages.filter(active_q()).count()


def book_run_active_message(count: int) -> str:
    """«تجري معالجة 3 صفحات من هذا الكتاب الآن؛ …» (Arabic count forms)."""
    from assembly.render import ar_count  # other app: lazy import (the shared Arabic count forms)

    return BOOK_RUN_ACTIVE_ERROR.format(count=ar_count(count, ("صفحة واحدة", "صفحتين", "صفحات", "صفحة")))
