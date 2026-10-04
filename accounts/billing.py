"""The page quota (D106, docs/CHALLENGE_SPEC.md §1): a page is one page the models read (the cost on Runpod).

Every change goes through this module, under `select_for_update` on the organisation (the account):

- `grant` / `grant_plan`: pages given to an account (a `QuotaGrant` and its `grant` entry); `revoke` one.
- `hold(book, pages, run_key, user)`: one `QuotaHold` per page queued for model reading. With a `user` who is
  not a superuser (a request) the account's available pages are checked first: more pages than available raise
  `QuotaExceeded` (Arabic, with the numbers) and nothing is held. Without a user (a task, a command) the holds
  are placed unchecked. A page that already has a hold keeps it (the run that queued it).
- `consume(page, run_key)`: the models read the page (`ocr_page_full` ended with it in `ocr_done`): one
  `consume` entry, taken from the live grant that expires first (never-expiring last), and the page's hold
  goes. Unique per (page, run key): a retried or requeued task never charges twice. It never fails: with no
  live grant left the page is read on debt (an entry without a grant), the available pages go below zero,
  and the next grant pays the debt first.
- `release(page)`: the page's hold goes (an error, an exclusion, a run that died).
- `balance` = the remaining pages of the live grants (started, unexpired, not revoked) − the debt;
  `available` = balance − holds. Expiry is computed on read; `expire` (`manage.py expire_quota`, daily)
  writes the `expire` entries for the record.

The account charged is the book's organisation. Unlimited accounts and books without an organisation are never
limited nor charged (no hold, no entry). Superusers skip the checks; the book's account is still charged.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import F, Max, Q, Sum
from django.utils import timezone

from accounts.models import Organization, Plan, QuotaEntry, QuotaGrant, QuotaHold

log = logging.getLogger(__name__)

PAGE_FORMS: tuple[str, str, str, str] = ("صفحة واحدة", "صفحتان", "صفحات", "صفحة")
PAGE_FORMS_OF: tuple[str, str, str, str] = (
    "صفحة واحدة",
    "صفحتين",
    "صفحات",
    "صفحة",
)  # an object: «تقرأ صفحتين»
EXPIRY_NOTICE_DAYS = 14  # the sidebar names the nearest expiry when it is closer than this
UPLOAD_REFUSED = "هذا الكتاب نحو {pages} ورصيدك المتاح {available}."
RUN_REFUSED = "تقرأ هذه المعالجة {pages} ورصيدك المتاح {available}."
DEBT_NOTE = "سداد صفحات قُرئت بلا رصيد"
NO_GRANT_NOTE = "قُرئت بلا رصيد"

__all__ = [
    "QuotaExceeded",
    "Summary",
    "allowance",
    "available",
    "balance",
    "charged_account",
    "consume",
    "debt",
    "expire",
    "grant",
    "grant_plan",
    "held",
    "hold",
    "ledger",
    "live_grants",
    "live_q",
    "nav_summary",
    "pages_phrase",
    "release",
    "release_pages",
    "release_run",
    "revoke",
    "summaries",
    "summary",
    "upload_pages",
    "usage_by_book",
]


class QuotaExceeded(ValueError):
    """A request asks for more pages than the account has available (the message says both numbers)."""

    def __init__(self, pages: int, available: int, template: str = RUN_REFUSED):
        self.pages, self.available = int(pages), int(available)
        super().__init__(
            template.format(
                pages=pages_phrase(pages, PAGE_FORMS_OF), available=pages_phrase(max(0, available))
            )
        )


def pages_phrase(n: int, forms: tuple[str, str, str, str] = PAGE_FORMS) -> str:
    """`n` pages in Arabic: «صفحة واحدة», «صفحتان» (`PAGE_FORMS_OF`: «صفحتين»), «5 صفحات», «300 صفحة»,
    «0 صفحة» (Western digits)."""
    n = int(n)
    if n == 1:
        return forms[0]
    if n == 2:
        return forms[1]
    return f"{n} {forms[2] if 3 <= abs(n) % 100 <= 10 else forms[3]}"


# ====================================================================== reading


def live_q(now: datetime | None = None) -> Q:
    """Grants that count: started, not expired, not revoked."""
    now = now or timezone.now()
    return Q(revoked_at__isnull=True, starts_at__lte=now) & (
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    )


def live_grants(organization: Organization, now: datetime | None = None):
    """The account's live grants, the one that expires first first (never-expiring last): the order pages are
    taken from."""
    return QuotaGrant.objects.filter(live_q(now), organization=organization).order_by(
        F("expires_at").asc(nulls_last=True), "starts_at", "id"
    )


def debt(organization: Organization) -> int:
    """Pages the account read with no live grant and has not paid back yet (≥ 0)."""
    total = QuotaEntry.objects.filter(organization=organization, grant__isnull=True).aggregate(
        s=Sum("pages")
    )["s"]
    return max(0, -int(total or 0))


def balance(organization: Organization, now: datetime | None = None) -> int:
    """Remaining pages of the live grants, less the debt (below zero while in debt)."""
    remaining = live_grants(organization, now).aggregate(s=Sum("remaining"))["s"] or 0
    return int(remaining) - debt(organization)


def held(organization: Organization) -> int:
    """Pages queued for model reading and not read yet."""
    return QuotaHold.objects.filter(organization=organization).count()


def available(organization: Organization, now: datetime | None = None) -> int:
    """Balance − holds: what a new run may still read."""
    return balance(organization, now) - held(organization)


def charged_account(book) -> Organization | None:
    """The account a page of `book` is charged to: the book's organisation, None when the book has none or
    its organisation is unlimited (never charged)."""
    if book is None or not getattr(book, "organization_id", None):
        return None
    organization = book.organization
    return None if organization.unlimited else organization


def allowance(organization: Organization | None, user=None) -> int | None:
    """The pages a request of `user` may still queue for `organization`: None when nothing limits it (no
    organisation, an unlimited one, a superuser), else the available pages."""
    if organization is None or organization.unlimited or getattr(user, "is_superuser", False):
        return None
    return available(organization)


def upload_pages(pdf_pages: int, skip_first: int, skip_last: int, pages_per_sheet: int) -> int:
    """The pages a new book will read: (PDF pages − skipped first − skipped last) × pages per sheet."""
    sheets = max(0, int(pdf_pages or 0) - int(skip_first or 0) - int(skip_last or 0))
    return sheets * max(1, int(pages_per_sheet or 1))


@dataclass(frozen=True)
class Summary:
    """An account's numbers for the screens and the sidebar."""

    unlimited: bool
    balance: int
    held: int
    available: int
    debt: int
    next_expiry: datetime | None  # the nearest expiry of a live grant with pages left
    expiring: int  # its pages
    used_this_month: int

    @property
    def expiring_soon(self) -> bool:
        if self.next_expiry is None:
            return False
        return self.next_expiry - timezone.now() < timedelta(days=EXPIRY_NOTICE_DAYS)


def _month_start(now: datetime) -> datetime:
    return timezone.localtime(now).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def summaries(organizations: Iterable[Organization], now: datetime | None = None) -> dict[int, Summary]:
    """`{organization id: Summary}` in five queries whatever the number of accounts (the «الفوترة» list)."""
    now = now or timezone.now()
    orgs = {org.pk: org for org in organizations}
    if not orgs:
        return {}
    ids = list(orgs)
    remaining: dict[int, int] = {}
    nearest: dict[int, tuple[datetime, int]] = {}
    rows = QuotaGrant.objects.filter(live_q(now), organization_id__in=ids, remaining__gt=0).values_list(
        "organization_id", "remaining", "expires_at"
    )
    for org_id, left, expires in rows:
        remaining[org_id] = remaining.get(org_id, 0) + left
        if expires is not None:
            best = nearest.get(org_id)
            if best is None or expires < best[0]:
                nearest[org_id] = (expires, left)
            elif expires == best[0]:
                nearest[org_id] = (expires, best[1] + left)
    debts = dict(
        QuotaEntry.objects.filter(organization_id__in=ids, grant__isnull=True)
        .values_list("organization_id")
        .annotate(s=Sum("pages"))
        .values_list("organization_id", "s")
    )
    holds: dict[int, int] = {}
    for org_id in QuotaHold.objects.filter(organization_id__in=ids).values_list("organization_id", flat=True):
        holds[org_id] = holds.get(org_id, 0) + 1
    used = dict(
        QuotaEntry.objects.filter(
            organization_id__in=ids, kind=QuotaEntry.Kind.CONSUME, created_at__gte=_month_start(now)
        )
        .values_list("organization_id")
        .annotate(s=Sum("pages"))
        .values_list("organization_id", "s")
    )
    out: dict[int, Summary] = {}
    for org_id, org in orgs.items():
        owed = max(0, -int(debts.get(org_id) or 0))
        bal = remaining.get(org_id, 0) - owed
        hold_count = holds.get(org_id, 0)
        expiry = nearest.get(org_id)
        out[org_id] = Summary(
            unlimited=bool(org.unlimited),
            balance=bal,
            held=hold_count,
            available=bal - hold_count,
            debt=owed,
            next_expiry=expiry[0] if expiry else None,
            expiring=expiry[1] if expiry else 0,
            used_this_month=-int(used.get(org_id) or 0),
        )
    return out


def summary(organization: Organization, now: datetime | None = None) -> Summary:
    """One account's numbers (`summaries`)."""
    return summaries([organization], now)[organization.pk]


def nav_summary(organization: Organization) -> Summary:
    """The sidebar's «الرصيد»: `summary`, with no query for an unlimited account (only «غير محدود»)."""
    if organization.unlimited:
        return Summary(True, 0, 0, 0, 0, None, 0, 0)
    return summary(organization)


def usage_by_book(organization: Organization) -> list[dict]:
    """Pages read per book, most first: `[{book_id, title, pages, last}]` (a deleted book: id None)."""
    rows = (
        QuotaEntry.objects.filter(organization=organization, kind=QuotaEntry.Kind.CONSUME)
        .values("book_id", "book__title")
        .annotate(s=Sum("pages"), last=Max("created_at"))
        .order_by("s", "book__title")
    )
    return [
        {
            "book_id": row["book_id"],
            "title": row["book__title"] or "",
            "pages": -int(row["s"] or 0),
            "last": row["last"],
        }
        for row in rows
    ]


def ledger(organization: Organization, limit: int = 200):
    """The account's latest ledger entries, newest first."""
    return QuotaEntry.objects.filter(organization=organization).select_related(
        "grant", "book", "page", "user"
    )[:limit]


# ====================================================================== changing


def _lock(organization_id: int) -> Organization:
    """Lock the account's row for the rest of the transaction (every change is serialised per account)."""
    return Organization.objects.select_for_update().get(pk=organization_id)


def _settle_debt(organization: Organization, now: datetime) -> int:
    """Pay the account's debt from its live grants, the one that expires first first (under the lock): each
    payment is a pair of `adjust` entries, −n on the grant and +n on the debt. Returns the pages paid."""
    owed = debt(organization)
    paid = 0
    if owed <= 0:
        return 0
    for item in live_grants(organization, now).filter(remaining__gt=0):
        take = min(item.remaining, owed - paid)
        QuotaGrant.objects.filter(pk=item.pk).update(remaining=F("remaining") - take)
        QuotaEntry.objects.create(
            organization=organization, kind=QuotaEntry.Kind.ADJUST, pages=-take, grant=item, note=DEBT_NOTE
        )
        QuotaEntry.objects.create(
            organization=organization, kind=QuotaEntry.Kind.ADJUST, pages=take, note=DEBT_NOTE
        )
        paid += take
        if paid >= owed:
            break
    return paid


def grant(
    organization: Organization,
    pages: int,
    *,
    kind: str = QuotaGrant.Kind.PURCHASE,
    user=None,
    plan: Plan | None = None,
    starts_at: datetime | None = None,
    expires_at: datetime | None = None,
    days: int | None = None,
    amount: Decimal | None = None,
    currency: str = "",
    reference: str = "",
    note: str = "",
) -> QuotaGrant:
    """Give `pages` to the account from `starts_at` (now) until `expires_at`, or for `days` from the start
    (neither: never expires). Writes the grant entry; a debt is paid from the live grants at once. Raises
    ValueError (Arabic) for no pages or an expiry before the start."""
    pages = int(pages or 0)
    if pages <= 0:
        raise ValueError("اكتب عدد صفحات أكبر من صفر.")
    now = timezone.now()
    starts_at = starts_at or now
    if expires_at is None and days:
        expires_at = starts_at + timedelta(days=int(days))
    if expires_at is not None and expires_at <= starts_at:
        raise ValueError("تاريخ الانتهاء قبل تاريخ البدء.")
    label = QuotaGrant.Kind(kind).label if kind in QuotaGrant.Kind.values else str(kind)
    with transaction.atomic():
        _lock(organization.pk)
        item = QuotaGrant.objects.create(
            organization=organization,
            kind=kind,
            plan=plan,
            pages=pages,
            remaining=pages,
            starts_at=starts_at,
            expires_at=expires_at,
            amount=amount,
            currency=(currency or "").upper()[:3],
            reference=(reference or "")[:120],
            note=note or "",
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )
        QuotaEntry.objects.create(
            organization=organization,
            kind=QuotaEntry.Kind.GRANT,
            pages=pages,
            grant=item,
            user=item.created_by,
            note=f"{label} · {plan.name}" if plan is not None else label,
        )
        _settle_debt(organization, now)
    item.refresh_from_db(fields=["remaining"])
    return item


def grant_plan(
    organization: Organization,
    plan: Plan,
    *,
    user=None,
    starts_at: datetime | None = None,
    reference: str = "",
    note: str = "",
) -> QuotaGrant:
    """A purchase of `plan`: its pages for its validity from the start, at its price."""
    return grant(
        organization,
        plan.pages,
        kind=QuotaGrant.Kind.PURCHASE,
        user=user,
        plan=plan,
        starts_at=starts_at,
        days=plan.validity_days or None,
        amount=plan.price,
        currency=plan.currency,
        reference=reference,
        note=note,
    )


def revoke(item: QuotaGrant, user=None) -> QuotaGrant:
    """Revoke a grant: it counts for nothing from now; its remaining pages leave the ledger (`revoke`)."""
    with transaction.atomic():
        _lock(item.organization_id)
        item = QuotaGrant.objects.select_for_update().get(pk=item.pk)
        if item.revoked_at is not None:
            return item
        left = item.remaining
        item.revoked_at = timezone.now()
        item.revoked_by = user if getattr(user, "is_authenticated", False) else None
        item.remaining = 0
        item.save(update_fields=["revoked_at", "revoked_by", "remaining"])
        QuotaEntry.objects.create(
            organization_id=item.organization_id,
            kind=QuotaEntry.Kind.REVOKE,
            pages=-left,
            grant=item,
            user=item.revoked_by,
        )
    return item


def _page_ids(pages: Iterable) -> list[int]:
    seen: dict[int, None] = {}
    for page in pages:
        seen.setdefault(int(getattr(page, "pk", page)), None)
    return list(seen)


def hold(book, pages: Iterable, run_key: str = "", user=None) -> list[int]:
    """Hold `pages` (pages or ids) of `book` for a run (`run_key`) that queues them for model reading; returns
    the ids of the holds placed (a page that has one keeps it). With a `user` who is not a superuser the
    account's available pages are checked first: `QuotaExceeded` when more pages are asked than available,
    and nothing is held. Nothing for a book that is not charged (`charged_account`)."""
    organization = charged_account(book)
    ids = _page_ids(pages)
    if organization is None or not ids:
        return []
    check = user is not None and not getattr(user, "is_superuser", False)
    if not check and QuotaHold.objects.filter(page_id__in=ids).count() == len(ids):
        return []  # every page is held already (a book run's own holds, placed by its request)
    with transaction.atomic():
        _lock(organization.pk)
        existing = set(QuotaHold.objects.filter(page_id__in=ids).values_list("page_id", flat=True))
        new = [pk for pk in ids if pk not in existing]
        if check:
            left = available(organization)
            if len(new) > left:
                raise QuotaExceeded(len(ids), left)
        QuotaHold.objects.bulk_create(
            [QuotaHold(organization=organization, page_id=pk, run_key=(run_key or "")[:64]) for pk in new]
        )
    return new


def release(page, run_key: str | None = None) -> int:
    """The page's hold goes (any run's; only `run_key`'s when given). Returns how many went."""
    rows = QuotaHold.objects.filter(page_id=int(getattr(page, "pk", page)))
    if run_key is not None:
        rows = rows.filter(run_key=run_key)
    first = rows.values_list("organization_id", flat=True).first()
    if first is None:
        return 0
    with transaction.atomic():
        _lock(first)
        return rows.delete()[0]


def release_run(run_key: str) -> int:
    """Release the holds a run placed (a book re-run that could not be queued gives them back)."""
    if not run_key:
        return 0
    rows = QuotaHold.objects.filter(run_key=run_key[:64])
    org_ids = sorted(set(rows.values_list("organization_id", flat=True)))
    if not org_ids:
        return 0
    with transaction.atomic():
        for org_id in org_ids:
            _lock(org_id)
        return rows.delete()[0]


def release_pages(page_ids: Iterable[int]) -> int:
    """Release the holds of these pages (a run that could not be queued gives back the holds it placed)."""
    ids = _page_ids(page_ids)
    rows = QuotaHold.objects.filter(page_id__in=ids)
    org_ids = sorted(set(rows.values_list("organization_id", flat=True)))
    if not org_ids:
        return 0
    with transaction.atomic():
        for org_id in org_ids:
            _lock(org_id)
        return rows.delete()[0]


def consume(page, run_key: str, user=None) -> QuotaEntry | None:
    """The models read `page` (its run `run_key`): charge one page to its book's account and release its hold.
    Idempotent per (page, run key): the entry already written is returned. Never fails: with no live grant the
    page is read on debt. None for a book that is not charged (its hold, if any, is released)."""
    book = page.book
    organization = charged_account(book)
    if organization is None:
        release(page)
        return None
    run_key = (run_key or "")[:64]
    now = timezone.now()
    with transaction.atomic():
        _lock(organization.pk)
        done = QuotaEntry.objects.filter(kind=QuotaEntry.Kind.CONSUME, page=page, run_key=run_key).first()
        if done is None:
            _settle_debt(organization, now)
            source = live_grants(organization, now).filter(remaining__gt=0).first()
            entry = QuotaEntry(
                organization=organization,
                kind=QuotaEntry.Kind.CONSUME,
                pages=-1,
                grant=source,
                book=book,
                page=page,
                run_key=run_key,
                user=user if getattr(user, "is_authenticated", False) else None,
                note="" if source is not None else NO_GRANT_NOTE,
            )
            try:
                with transaction.atomic():
                    entry.save()
            except IntegrityError:  # its twin got there first
                done = QuotaEntry.objects.get(kind=QuotaEntry.Kind.CONSUME, page=page, run_key=run_key)
            else:
                if source is not None:
                    QuotaGrant.objects.filter(pk=source.pk).update(remaining=F("remaining") - 1)
                done = entry
        QuotaHold.objects.filter(page_id=page.pk).delete()
    return done


def expire(now: datetime | None = None) -> int:
    """Record the grants that expired with pages left: an `expire` entry each (dated at its expiry) and
    `remaining` 0. Expiry already counts on read; this writes it down. Returns the grants recorded."""
    now = now or timezone.now()
    count = 0
    due = QuotaGrant.objects.filter(revoked_at__isnull=True, expires_at__lte=now, remaining__gt=0)
    for item_id, org_id in due.order_by("organization_id", "id").values_list("pk", "organization_id"):
        with transaction.atomic():
            _lock(org_id)
            item = QuotaGrant.objects.get(pk=item_id)
            if item.remaining <= 0 or item.revoked_at is not None:
                continue
            left = item.remaining
            item.remaining = 0
            item.save(update_fields=["remaining"])
            QuotaEntry.objects.create(
                organization_id=org_id,
                kind=QuotaEntry.Kind.EXPIRE,
                pages=-left,
                grant=item,
                created_at=item.expires_at or now,
            )
            count += 1
    return count
