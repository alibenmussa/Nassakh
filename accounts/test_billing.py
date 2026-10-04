"""D106: the page quota (`accounts.billing`) and its screens.

The math: live grants (started, unexpired, unrevoked), pages taken from the grant that expires first, holds,
consume once per (page, run key), debt when nothing is left and its repayment from the next grant, unlimited
accounts and books without one, `expire_quota`, the ledger summing to the balance. The screens: «الفوترة» for
superusers only (404 for anyone else), a grant from a plan or by hand, revoke, «غير محدود», the plans; the
members' «الرصيد» on the organisation's page and in the sidebar; Django admin read-only.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

import pytest

from accounts import billing
from accounts.models import Organization, Plan, QuotaEntry, QuotaGrant, QuotaHold
from accounts.testing import member, owned
from books.models import Book, Page

pytestmark = pytest.mark.django_db


@pytest.fixture
def account() -> Organization:
    from accounts.services import default_organization

    default_organization()  # the first organisation is the owner's (unlimited); this one is a customer's
    return Organization.objects.create(name="دار المخطوطات", unlimited=False)


@pytest.fixture
def book(account) -> Book:
    book = owned(Book.objects.create(title="كتاب", status=Book.Status.OCR), account)
    for n in range(1, 6):
        Page.objects.create(book=book, number=n, source_index=n - 1, status=Page.Status.OCR_DONE)
    return book


def page_of(book: Book, n: int = 1) -> Page:
    return Page.objects.select_related("book").get(book=book, number=n)


def ledger_sum(account) -> int:
    return int(QuotaEntry.objects.filter(organization=account).aggregate(s=Sum("pages"))["s"] or 0)


# ====================================================================== the math


def test_balance_counts_only_live_grants(account):
    now = timezone.now()
    billing.grant(account, 10)
    billing.grant(account, 7, starts_at=now + timedelta(days=2))  # not started
    old = billing.grant(account, 5, starts_at=now - timedelta(days=40), days=30)  # expired 10 days ago
    gone = billing.revoke(billing.grant(account, 3))
    assert billing.balance(account) == 10 and billing.available(account) == 10
    assert old.state() == "expired" and gone.state() == "revoked"
    # the future grant counts once it starts
    assert billing.balance(account, now + timedelta(days=3)) == 17


def test_pages_come_from_the_grant_that_expires_first(account, book):
    now = timezone.now()
    never = billing.grant(account, 10)
    late = billing.grant(account, 10, expires_at=now + timedelta(days=30))
    soon = billing.grant(account, 2, days=10)
    for n in (1, 2, 3, 4):
        billing.consume(page_of(book, n), "run-a")
    for row in (never, late, soon):
        row.refresh_from_db()
    assert (soon.remaining, late.remaining, never.remaining) == (0, 8, 10)
    assert billing.balance(account) == 18
    entries = QuotaEntry.objects.filter(kind=QuotaEntry.Kind.CONSUME).order_by("id")
    assert [e.grant_id for e in entries] == [soon.pk, soon.pk, late.pk, late.pk]
    assert ledger_sum(account) == billing.balance(account)


def test_consume_is_once_per_page_and_run(account, book):
    item = billing.grant(account, 5)
    page = page_of(book)
    first = billing.consume(page, "run-1")
    again = billing.consume(page, "run-1")  # a retried or requeued task
    assert first.pk == again.pk
    item.refresh_from_db()
    assert item.remaining == 4
    billing.consume(page, "run-2")  # the page read again: charged again
    item.refresh_from_db()
    assert item.remaining == 3
    assert QuotaEntry.objects.filter(kind=QuotaEntry.Kind.CONSUME, page=page).count() == 2


def test_holds_lower_the_available_pages_and_consume_releases_them(account, book):
    billing.grant(account, 10)
    pages = list(book.pages.order_by("number"))
    placed = billing.hold(book, pages[:3], "run-x")
    assert len(placed) == 3 and billing.held(account) == 3
    assert billing.available(account) == 7 and billing.balance(account) == 10
    assert billing.hold(book, pages[:3], "run-y") == []  # a page keeps its hold
    billing.consume(page_of(book, 1), "run-x")
    billing.release(pages[1])
    assert billing.held(account) == 1 and billing.available(account) == 8
    assert billing.release_pages([p.pk for p in pages]) == 1
    assert billing.held(account) == 0


def test_hold_checks_the_available_pages_for_a_member_only(account, book):
    billing.grant(account, 2)
    pages = list(book.pages.order_by("number"))
    editor = User.objects.create_user("e1")
    with pytest.raises(billing.QuotaExceeded) as caught:
        billing.hold(book, pages[:3], "run", user=editor)
    assert str(caught.value) == "تقرأ هذه المعالجة 3 صفحات ورصيدك المتاح صفحتان."
    assert not QuotaHold.objects.exists()
    root = User.objects.create_superuser("root", "root@example.org", "x")
    assert len(billing.hold(book, pages[:3], "run", user=root)) == 3  # a superuser is not checked
    assert billing.available(account) == -1  # the account is still the one charged
    assert len(billing.hold(book, pages[3:], "task")) == 2  # a task (no user) is not checked either


def test_consume_never_fails_the_debt_is_paid_by_the_next_grant(account, book):
    entry = billing.consume(page_of(book, 1), "r")
    billing.consume(page_of(book, 2), "r")
    assert entry.grant is None and entry.note == billing.NO_GRANT_NOTE
    assert billing.debt(account) == 2 and billing.balance(account) == -2 and billing.available(account) == -2
    item = billing.grant(account, 10)
    assert item.remaining == 8 and billing.debt(account) == 0 and billing.balance(account) == 8
    assert ledger_sum(account) == billing.balance(account)
    adjusts = QuotaEntry.objects.filter(kind=QuotaEntry.Kind.ADJUST).order_by("id")
    assert [(e.pages, e.grant_id) for e in adjusts] == [(-2, item.pk), (2, None)]


def test_expiry_counts_on_read_and_expire_quota_records_it(account, book):
    now = timezone.now()
    item = billing.grant(account, 6, starts_at=now - timedelta(days=5), expires_at=now + timedelta(days=1))
    billing.consume(page_of(book), "r")
    later = now + timedelta(days=2)
    assert billing.balance(account) == 5 and billing.balance(account, later) == 0
    assert billing.expire(later) == 1
    item.refresh_from_db()
    assert item.remaining == 0
    row = QuotaEntry.objects.get(kind=QuotaEntry.Kind.EXPIRE)
    assert (row.pages, row.grant_id, row.created_at) == (-5, item.pk, item.expires_at)
    assert billing.expire(later) == 0  # recorded once
    assert ledger_sum(account) == billing.balance(account, later) == 0
    out = StringIO()
    call_command("expire_quota", stdout=out)
    assert "expired grants recorded: 0" in out.getvalue()


def test_revoke_writes_the_remaining_pages_off(account):
    item = billing.grant(account, 9, reference="INV-7")
    billing.revoke(item)
    billing.revoke(item)  # once
    item.refresh_from_db()
    assert item.revoked_at is not None and item.remaining == 0
    assert list(QuotaEntry.objects.filter(kind=QuotaEntry.Kind.REVOKE).values_list("pages", flat=True)) == [
        -9
    ]
    assert billing.balance(account) == 0 == ledger_sum(account)


def test_unlimited_accounts_and_books_without_one_are_never_charged(account, book):
    account.unlimited = True
    account.save()
    page = page_of(book)
    assert billing.hold(book, [page], "r", user=User.objects.create_user("x")) == []
    assert billing.consume(page, "r") is None
    assert billing.allowance(account, User.objects.create_user("y")) is None
    Book.objects.filter(pk=book.pk).update(organization=None)
    orphan = page_of(book, 2)
    assert billing.consume(orphan, "r") is None and billing.hold(orphan.book, [orphan], "r") == []
    assert not QuotaEntry.objects.exists() and not QuotaHold.objects.exists()


def test_allowance_is_none_for_a_superuser(account):
    billing.grant(account, 4)
    assert billing.allowance(account, User.objects.create_user("m")) == 4
    assert billing.allowance(account, User.objects.create_superuser("s", "s@example.org", "x")) is None
    assert billing.allowance(None) is None


def test_grant_from_a_plan_and_refusals(account):
    plan = Plan.objects.create(name="باقة", pages=1500, validity_days=365, price=Decimal("49.00"))
    item = billing.grant_plan(account, plan, reference="R-1")
    assert (item.pages, item.remaining, item.amount, item.currency, item.kind) == (
        1500,
        1500,
        Decimal("49.00"),
        "USD",
        QuotaGrant.Kind.PURCHASE,
    )
    assert abs((item.expires_at - item.starts_at) - timedelta(days=365)) < timedelta(seconds=1)
    with pytest.raises(ValueError, match="أكبر من صفر"):
        billing.grant(account, 0)
    with pytest.raises(ValueError, match="قبل تاريخ البدء"):
        billing.grant(account, 5, expires_at=timezone.now() - timedelta(days=1))


def test_summaries_give_the_numbers_of_every_account(account, book):
    other = Organization.objects.create(name="فرد", kind=Organization.Kind.INDIVIDUAL)
    billing.grant(account, 10, days=5)
    billing.grant(account, 20, days=60)
    billing.consume(page_of(book), "r")
    billing.hold(book, [page_of(book, 2)], "r2")
    numbers = billing.summaries([account, other])
    s = numbers[account.pk]
    assert (s.balance, s.held, s.available, s.used_this_month, s.expiring) == (29, 1, 28, 1, 9)
    assert s.expiring_soon and not s.unlimited
    assert numbers[other.pk].available == 0 and numbers[other.pk].next_expiry is None
    usage = billing.usage_by_book(account)
    assert usage == [{"book_id": book.pk, "title": "كتاب", "pages": 1, "last": usage[0]["last"]}]


def test_pages_phrase_uses_the_arabic_count_forms():
    assert [billing.pages_phrase(n) for n in (0, 1, 2, 5, 11, 300, 103)] == [
        "0 صفحة",
        "صفحة واحدة",
        "صفحتان",
        "5 صفحات",
        "11 صفحة",
        "300 صفحة",
        "103 صفحات",
    ]


def test_the_plans_are_seeded():
    assert list(Plan.objects.order_by("pages").values_list("pages", "price", "validity_days")) == [
        (1500, Decimal("49.00"), 365),
        (5000, Decimal("149.00"), 365),
        (15000, Decimal("349.00"), 365),
    ]


def test_the_first_organisation_is_unlimited():
    from accounts.services import default_organization

    assert default_organization().unlimited is True


# ====================================================================== «الفوترة» (superusers only)


@pytest.fixture
def root(db) -> User:
    return User.objects.create_superuser("root", "root@example.org", "pass-1234")


def _member(username: str, organization, role: str = "member", group: str = "editor") -> User:
    user = User.objects.create_user(username, password="pass-1234")
    user.groups.add(Group.objects.get_or_create(name=group)[0])
    return member(user, organization, role)


def test_billing_pages_answer_404_to_anyone_but_a_superuser(client, account):
    item = billing.grant(account, 3)
    plan = Plan.objects.first()
    gets = [
        reverse("accounts:billing"),
        reverse("accounts:billing_plans"),
        reverse("accounts:billing_account", args=[account.pk]),
    ]
    posts = [
        reverse("accounts:billing_grant_add", args=[account.pk]),
        reverse("accounts:billing_grant_revoke", args=[item.pk]),
        reverse("accounts:billing_unlimited", args=[account.pk]),
        reverse("accounts:billing_plan_edit", args=[plan.pk]),
        reverse("accounts:billing_plans"),
    ]
    for url in gets:
        response = client.get(url)
        assert response.status_code == 302 and response["Location"].startswith("/accounts/login/")
    # the account's own admin, and a member of the global admin group, are not superusers
    for user in (_member("boss", account, "admin"), _member("global", account, group="admin")):
        client.force_login(user)
        for url in gets:
            assert client.get(url).status_code == 404, url
        for url in posts:
            assert client.post(url, {"pages": "100", "unlimited": "1", "name": "x"}).status_code == 404, url
    item.refresh_from_db()
    account.refresh_from_db()
    assert item.revoked_at is None and not account.unlimited and billing.balance(account) == 3


def test_the_billing_list_shows_every_account(client, root, account):
    billing.grant(account, 120, days=7)
    client.force_login(root)
    body = client.get(reverse("accounts:billing")).content.decode()
    assert "دار المخطوطات" in body and reverse("accounts:billing_account", args=[account.pk]) in body
    assert ">120<" in body and "قريب" in body


def test_a_superuser_adds_a_grant_from_a_plan_or_by_hand(client, root, account):
    client.force_login(root)
    plan = Plan.objects.get(pages=1500)
    url = reverse("accounts:billing_grant_add", args=[account.pk])
    response = client.post(url, {"plan": plan.pk, "kind": "purchase", "reference": "INV-1"})
    assert response.status_code == 302 and response["Location"].endswith("#grants")
    item = QuotaGrant.objects.get(organization=account, plan=plan)
    assert (item.pages, item.amount, item.currency, item.reference, item.created_by) == (
        1500,
        Decimal("49.00"),
        "USD",
        "INV-1",
        root,
    )
    response = client.post(
        url,
        {
            "kind": "bonus",
            "pages": "40",
            "expires_on": (timezone.localdate() + timedelta(days=9)).isoformat(),
        },
    )
    assert response.status_code == 302
    bonus = QuotaGrant.objects.get(organization=account, kind="bonus")
    assert timezone.localtime(bonus.expires_at).date() == timezone.localdate() + timedelta(days=9)
    assert billing.balance(account) == 1540
    # nothing to give: the page again with the reason
    response = client.post(url, {"kind": "bonus"})
    assert response.status_code == 400 and "اكتب عدد الصفحات أو اختر باقة." in response.content.decode()
    response = client.post(url, {"kind": "bonus", "pages": "5", "days": "3", "expires_on": "2030-01-01"})
    assert response.status_code == 400 and "لا كليهما" in response.content.decode()


def test_a_superuser_revokes_a_grant_and_sets_unlimited(client, root, account):
    item = billing.grant(account, 12)
    client.force_login(root)
    client.post(reverse("accounts:billing_grant_revoke", args=[item.pk]))
    assert billing.balance(account) == 0
    client.post(reverse("accounts:billing_unlimited", args=[account.pk]), {"unlimited": "1"})
    account.refresh_from_db()
    assert account.unlimited
    client.post(reverse("accounts:billing_unlimited", args=[account.pk]), {"unlimited": "0"})
    account.refresh_from_db()
    assert not account.unlimited
    body = client.get(reverse("accounts:billing_account", args=[account.pk])).content.decode()
    assert "إلغاء" in body and 'data-state="revoked"' in body and "السجل" in body


def test_a_superuser_manages_the_plans(client, root):
    client.force_login(root)
    response = client.post(
        reverse("accounts:billing_plans"),
        {
            "name": "باقة تجريبية",
            "pages": "100",
            "validity_days": "30",
            "price": "5",
            "currency": "usd",
            "active": "on",
        },
    )
    assert response.status_code == 302
    plan = Plan.objects.get(name="باقة تجريبية")
    assert (plan.pages, plan.currency, plan.active) == (100, "USD", True)
    client.post(
        reverse("accounts:billing_plan_edit", args=[plan.pk]),
        {"name": "باقة تجريبية", "pages": "120", "validity_days": "30", "price": "6", "currency": "USD"},
    )
    plan.refresh_from_db()
    assert (plan.pages, plan.active) == (120, False)
    assert "باقة تجريبية" in client.get(reverse("accounts:billing_plans")).content.decode()


def test_the_admin_shows_the_billing_rows_read_only(client, root, account):
    billing.grant(account, 3)
    client.force_login(root)
    for model in ("quotagrant", "quotaentry", "quotahold", "plan", "signup"):
        assert client.get(reverse(f"admin:accounts_{model}_changelist")).status_code == 200
        assert client.get(reverse(f"admin:accounts_{model}_add")).status_code == 403
    item = QuotaGrant.objects.get()
    assert (
        client.post(reverse("admin:accounts_quotagrant_change", args=[item.pk]), {"pages": "999"}).status_code
        == 403
    )


# ====================================================================== the members' view


def test_the_sidebar_shows_the_balance_and_a_near_expiry(client, account):
    billing.grant(account, 120, days=5)
    client.force_login(_member("m1", account))
    body = client.get(reverse("books:list")).content.decode()
    assert "الرصيد: 120 صفحة" in body and "data-nav-quota" in body
    assert "ينتهي منها 120 صفحة في" in body
    assert reverse("accounts:billing") not in body  # «الفوترة» is the superusers'


def test_the_sidebar_says_unlimited_and_shows_billing_to_a_superuser(client, root):
    from accounts.services import default_organization

    default_organization()
    client.force_login(root)
    body = client.get(reverse("books:list")).content.decode()
    assert "الرصيد: غير محدود" in body and reverse("accounts:billing") in body and "الفوترة" in body


def test_the_organisation_page_lists_the_live_grants_and_the_usage(client, account, book):
    billing.grant(account, 50, reference="secret-ref")
    billing.consume(page_of(book), "r")
    client.force_login(_member("m2", account))
    body = client.get(reverse("accounts:organization")).content.decode()
    assert 'id="quota"' in body and "data-quota-grants" in body and "data-usage" in body
    assert ">49<" in body and "كتاب" in body
    assert "secret-ref" not in body  # read-only, without the superuser's details
    assert reverse("accounts:billing_grant_add", args=[account.pk]) not in body


# ====================================================================== the demo account


def test_make_demo_account_moves_the_books_with_the_faces_only_they_use(account):
    from accounts.models import Membership, OrganizationFont, StyleTemplate
    from editor.models import StyleSheet

    first, second, stays = (
        owned(Book.objects.create(title=title), account) for title in ("الأول", "الثاني", "الباقي")
    )
    own = OrganizationFont.objects.create(
        organization=account, name="خاص", family="Own", regular="orgs/x/own.ttf"
    )
    shared = OrganizationFont.objects.create(
        organization=account, name="مشترك", family="Shared", regular="orgs/x/s.ttf"
    )
    StyleSheet.objects.create(book=first, body_font=own.key, heading_font=shared.key)
    StyleSheet.objects.create(book=stays, body_font=shared.key)
    template = StyleTemplate.objects.create(organization=account, name="قالب", source_book=second)
    page = Page.objects.create(book=first, number=1, source_index=0)
    billing.hold(first, [page], "r")
    out = StringIO()
    call_command(
        "make_demo_account",
        "--email",
        "Demo@Example.org",
        "--password",
        "demo-pass-2026",
        "--books",
        str(first.pk),
        str(second.pk),
        stdout=out,
    )
    demo = Organization.objects.get(name="حساب التجربة")
    assert demo.unlimited and demo.kind == Organization.Kind.ORGANIZATION
    user = User.objects.get(email="demo@example.org")
    assert user.username == "demo@example.org" and user.is_active and user.check_password("demo-pass-2026")
    assert (
        Membership.objects.get(user=user).organization == demo and user.groups.filter(name="editor").exists()
    )
    assert set(Book.objects.filter(organization=demo).values_list("title", flat=True)) == {"الأول", "الثاني"}
    own.refresh_from_db()
    shared.refresh_from_db()
    template.refresh_from_db()
    assert (own.organization, shared.organization, template.organization) == (demo, account, demo)
    assert QuotaHold.objects.get().organization == demo
    assert "font «مشترك» stays" in out.getvalue()
    # once more: nothing new
    call_command(
        "make_demo_account",
        "--email",
        "demo@example.org",
        "--password",
        "demo-pass-2026",
        "--books",
        str(first.pk),
        stdout=StringIO(),
    )
    assert (
        Organization.objects.filter(name="حساب التجربة").count() == 1
        and User.objects.filter(email="demo@example.org").count() == 1
    )


def test_the_account_page_shows_the_usage_and_the_ledger(client, root, account, book):
    billing.grant(account, 10, reference="INV-9")
    billing.consume(page_of(book), "r")
    client.force_login(root)
    body = client.get(reverse("accounts:billing_account", args=[account.pk])).content.decode()
    assert reverse("books:detail", args=[book.pk]) in body and "INV-9" in body
    assert 'data-entry="consume"' in body and 'data-entry="grant"' in body and "data-grant-form" in body
    assert "الباقة الأساسية" in body  # the plans fill the form
