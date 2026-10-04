"""D106: the page quota at the doors of model reading.

The upload refuses a book whose pages to read exceed the account's available pages (nothing saved);
«بدء المعالجة», the book's «إعادة المعالجة» and a page's re-run check again and hold one page each; the OCR
task charges a page that ends in `ocr_done` once per run and releases the hold on any other end. Superusers
skip the checks (the book's account is still charged); unlimited accounts are never limited nor charged.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from celery.exceptions import Retry
from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

import pytest

from accounts import billing
from accounts.models import Organization, QuotaEntry, QuotaHold
from accounts.services import default_organization
from accounts.testing import member, owned
from books import runs, services, tasks
from books.models import Book, Page
from books.tests import make_text_pdf
from ocr import services as ocr_services
from ocr import tasks as ocr_tasks

pytestmark = pytest.mark.django_db
PS = Page.Status


@pytest.fixture
def account() -> Organization:
    default_organization()  # the owner's (unlimited) comes first, as in a real database
    return Organization.objects.create(name="دار نشر", unlimited=False)


@pytest.fixture
def editor(account) -> User:
    user = User.objects.create_user("editor", password="pass-1234")
    user.groups.add(Group.objects.get_or_create(name="editor")[0])
    return member(user, account)


@pytest.fixture
def root(db) -> User:
    return User.objects.create_superuser("root", "root@example.org", "pass-1234")


def make_book(account, statuses, *, awaits=False, status=Book.Status.OCR) -> Book:
    book = owned(Book.objects.create(title="كتاب", status=status, awaits_ocr_start=awaits), account)
    for n, page_status in enumerate(statuses, start=1):
        Page.objects.create(book=book, number=n, source_index=n - 1, status=page_status)
    return Book.objects.select_related("organization").get(pk=book.pk)


def upload(client, pages: int = 10, **extra):
    data = {
        "title": "كتاب جديد",
        "author": "",
        "original_year": "",
        "notes": "",
        "skip_first": "0",
        "skip_last": "0",
        "pages_per_sheet": "1",
        "split_ratio": "0.5",
        "source_pdf": SimpleUploadedFile(
            "book.pdf", make_text_pdf(pages, lines=1), content_type="application/pdf"
        ),
        **extra,
    }
    with patch("books.tasks.ingest_book_task.delay"):
        return client.post(reverse("books:create"), data)


# ====================================================================== the upload


def test_the_form_shows_the_available_pages(client, account, editor):
    billing.grant(account, 7)
    client.force_login(editor)
    body = client.get(reverse("books:create")).content.decode()
    assert "رصيدك المتاح: 7 صفحات." in body and "available: 7" in body and "data-quota-line" in body


def test_an_upload_beyond_the_available_pages_is_refused_and_nothing_is_saved(client, account, editor):
    billing.grant(account, 5)
    client.force_login(editor)
    response = upload(client, pages=10)
    assert response.status_code == 200
    assert "هذا الكتاب نحو 10 صفحات ورصيدك المتاح 5 صفحات." in response.content.decode()
    assert not Book.objects.filter(title="كتاب جديد").exists()
    # (pages − skipped first − skipped last) × pages per sheet: (10 − 2 − 1) × 2 = 14
    billing.grant(account, 8)
    response = upload(client, pages=10, skip_first="2", skip_last="1", pages_per_sheet="2")
    assert "هذا الكتاب نحو 14 صفحة ورصيدك المتاح 13 صفحة." in response.content.decode()
    assert not Book.objects.filter(title="كتاب جديد").exists()
    response = upload(client, pages=10, skip_first="4", skip_last="1", pages_per_sheet="2")
    assert (
        response.status_code == 302 and Book.objects.filter(title="كتاب جديد", organization=account).exists()
    )


def test_a_superuser_and_an_unlimited_account_upload_without_a_check(client, account, root, editor):
    client.force_login(root)
    client.post(reverse("accounts:organization_switch"), {"organization": account.pk})
    body = client.get(reverse("books:create")).content.decode()
    assert "رصيدك المتاح" not in body and "available: null" in body
    assert upload(client, pages=3).status_code == 302
    account.unlimited = True
    account.save()
    client.force_login(editor)
    assert upload(client, pages=3).status_code == 302
    assert Book.objects.filter(title="كتاب جديد", organization=account).count() == 2


# ====================================================================== «بدء المعالجة»


def test_start_is_refused_with_the_numbers_and_starts_nothing(client, account, editor):
    billing.grant(account, 2)
    book = make_book(account, [PS.PREPROCESSED] * 3, awaits=True, status=Book.Status.NEEDS_GUIDES)
    client.force_login(editor)
    with patch("books.tasks.start_ocr_task.delay") as delay:
        client.post(reverse("books:start_ocr", args=[book.pk]))
    delay.assert_not_called()
    book.refresh_from_db()
    assert (book.awaits_ocr_start, book.status) == (True, Book.Status.NEEDS_GUIDES)
    assert not QuotaHold.objects.exists()
    body = client.get(reverse("books:detail", args=[book.pk])).content.decode()
    assert "تقرأ هذه المعالجة 3 صفحات ورصيدك المتاح صفحتان." in body


def test_start_holds_a_page_each_and_a_double_click_holds_nothing_more(account, editor):
    billing.grant(account, 3)
    book = make_book(
        account, [PS.PREPROCESSED] * 3 + [PS.ERROR], awaits=True, status=Book.Status.NEEDS_GUIDES
    )
    with patch("books.tasks.start_ocr_task.delay") as delay:
        services.start_ocr(book, user=editor)
        with pytest.raises(ValueError, match="بدأت المعالجة بالفعل"):
            services.start_ocr(Book.objects.get(pk=book.pk), user=editor)
    delay.assert_called_once_with(book.pk)
    assert set(QuotaHold.objects.values_list("run_key", flat=True)) == {services.QUOTA_START_KEY}
    assert billing.held(account) == 3 and billing.available(account) == 0


def test_start_gives_its_holds_back_when_the_broker_refuses(account, editor):
    billing.grant(account, 5)
    book = make_book(account, [PS.PREPROCESSED] * 2, awaits=True, status=Book.Status.NEEDS_GUIDES)
    with (
        patch("books.tasks.start_ocr_task.delay", side_effect=ConnectionError("redis down")),
        pytest.raises(ValueError, match="تعذّر إرسال العمل"),
    ):
        services.start_ocr(book, user=editor)
    assert not QuotaHold.objects.exists()


def test_a_superuser_starts_without_a_check_and_the_account_is_charged(account, root):
    book = make_book(account, [PS.PREPROCESSED] * 2, awaits=True, status=Book.Status.NEEDS_GUIDES)
    with patch("books.tasks.start_ocr_task.delay"):
        services.start_ocr(book, user=root)
    assert billing.held(account) == 2 and billing.available(account) == -2


def test_the_start_task_keeps_the_holds_for_the_runs(account, editor):
    billing.grant(account, 2)
    book = make_book(account, [PS.PREPROCESSED] * 2, awaits=True, status=Book.Status.NEEDS_GUIDES)
    with patch("books.tasks.start_ocr_task.delay"):
        services.start_ocr(book, user=editor)
    with patch("books.services.chain") as chain:  # the chains are not run here
        tasks.start_ocr_task(book.pk)
    assert chain.call_count == 2 and billing.held(account) == 2


# ====================================================================== «إعادة المعالجة»


def test_a_book_rerun_is_refused_with_the_numbers_and_claims_nothing(client, account, editor):
    billing.grant(account, 1)
    book = make_book(account, [PS.OCR_DONE, PS.OCR_DONE, PS.REVIEWED])
    client.force_login(editor)
    with patch("books.tasks.rerun_book_from.delay") as delay:
        client.post(reverse("books:rerun", args=[book.pk]), {"stage": "ocr"})
    delay.assert_not_called()
    assert not book.pages.exclude(run_token="").exists() and not QuotaHold.objects.exists()
    body = client.get(reverse("books:detail", args=[book.pk])).content.decode()
    assert "تقرأ هذه المعالجة صفحتين ورصيدك المتاح صفحة واحدة." in body


def test_a_book_rerun_holds_its_pages_under_its_token(account, editor):
    billing.grant(account, 5)
    book = make_book(account, [PS.OCR_DONE, PS.OCR_DONE, PS.REVIEWED])
    with patch("books.tasks.rerun_book_from.delay") as delay:
        assert services.queue_book_rerun(book, "ocr", user=editor) == 2
    token = delay.call_args.kwargs["run"]
    assert set(QuotaHold.objects.values_list("run_key", flat=True)) == {token}
    assert billing.available(account) == 3
    # the task re-runs the pages under the same token: no hold more
    with patch("books.services.chain"):
        assert services.rerun_book(book, "ocr", run=token) == 2
    assert billing.held(account) == 2


def test_a_book_rerun_that_cannot_be_queued_gives_its_holds_back(account, editor):
    billing.grant(account, 5)
    book = make_book(account, [PS.OCR_DONE])
    with (
        patch("books.tasks.rerun_book_from.delay", side_effect=ConnectionError("redis down")),
        pytest.raises(ValueError, match="تعذّر إرسال العمل"),
    ):
        services.queue_book_rerun(book, "ocr", user=editor)
    assert not QuotaHold.objects.exists() and not book.pages.exclude(run_token="").exists()


def test_reruns_the_models_do_not_read_are_free(account, editor):
    book = make_book(account, [PS.OCR_DONE, PS.OCR_DONE])
    with patch("books.tasks.rerun_book_from.delay"):
        services.queue_book_rerun(book, "ocr_fast", user=editor)  # Tesseract alone: no check, no hold
    assert not QuotaHold.objects.exists()
    waiting = make_book(account, [PS.PREPROCESSED], awaits=True, status=Book.Status.NEEDS_GUIDES)
    with patch("books.tasks.rerun_book_from.delay"):
        services.queue_book_rerun(waiting, "preprocess", user=editor)  # «إعادة التخطيط» in «التخطيط»
    assert not QuotaHold.objects.exists()


def test_a_page_rerun_is_refused_with_the_numbers_and_keeps_the_page(client, account, editor):
    book = make_book(account, [PS.OCR_DONE])
    page = book.pages.get()
    page.set_error("ocr_full", "فشل سابق")
    client.force_login(editor)
    with patch("books.services.chain") as chain:
        response = client.post(reverse("books:rerun", args=[book.pk, 1]), {"stage": "ocr_full"})
    chain.assert_not_called()
    assert response.status_code == 302
    page.refresh_from_db()
    assert (page.status, page.run_token, page.error_message) == (PS.ERROR, "", "فشل سابق")
    assert not QuotaHold.objects.exists()
    with pytest.raises(billing.QuotaExceeded, match="تقرأ هذه المعالجة صفحة واحدة ورصيدك المتاح 0 صفحة."):
        services.run_stage(page, "ocr_full", user=editor)


# ====================================================================== the OCR task: charge or release


def _read_ok(page: Page) -> None:
    Page.objects.filter(pk=page.pk).update(status=PS.OCR_DONE)


def _held_run(account, book: Book) -> tuple[Page, str]:
    page = book.pages.get(number=1)
    token = runs.claim_page(page.pk)
    billing.hold(book, [page], token)
    return Page.objects.select_related("book").get(pk=page.pk), token


def test_a_page_read_is_charged_once_per_run_and_its_hold_released(account):
    item = billing.grant(account, 10)
    book = make_book(account, [PS.LAYOUT_DONE])
    page, token = _held_run(account, book)
    with patch.object(ocr_services, "run_full_ocr", side_effect=_read_ok):
        ocr_tasks.ocr_page_full.apply(args=[page.pk], kwargs={"run": token, "last": True}).get()
    entry = QuotaEntry.objects.get(kind=QuotaEntry.Kind.CONSUME)
    assert (entry.page_id, entry.book_id, entry.run_key, entry.pages, entry.grant_id) == (
        page.pk,
        book.pk,
        token,
        -1,
        item.pk,
    )
    assert not QuotaHold.objects.exists() and billing.balance(account) == 9
    # the same run again (a message delivered twice): the run no longer holds the page, nothing is charged
    with patch.object(ocr_services, "run_full_ocr", side_effect=_read_ok):
        ocr_tasks.ocr_page_full.apply(args=[page.pk], kwargs={"run": token, "last": True}).get()
    ocr_tasks._settle_quota(page.pk, token)  # and if it settled again, the (page, run) entry is the one
    assert (
        QuotaEntry.objects.filter(kind=QuotaEntry.Kind.CONSUME).count() == 1 and billing.balance(account) == 9
    )


def test_a_task_without_a_run_is_charged_under_its_task_id(account):
    billing.grant(account, 10)
    book = make_book(account, [PS.LAYOUT_DONE])
    page = book.pages.get()
    with patch.object(ocr_services, "run_full_ocr", side_effect=_read_ok):
        result = ocr_tasks.ocr_page_full.apply(args=[page.pk])
    assert QuotaEntry.objects.get(kind=QuotaEntry.Kind.CONSUME).run_key == result.id


def test_a_page_that_fails_releases_its_hold_at_no_cost(account):
    billing.grant(account, 10)
    book = make_book(account, [PS.LAYOUT_DONE])
    page, token = _held_run(account, book)
    with patch.object(ocr_services, "run_full_ocr", side_effect=ValueError("bad tensor")):
        ocr_tasks.ocr_page_full.apply(args=[page.pk], kwargs={"run": token, "last": True}).get()
    page.refresh_from_db()
    assert page.status == PS.ERROR
    assert not QuotaHold.objects.exists() and not QuotaEntry.objects.filter(kind="consume").exists()
    assert billing.available(account) == 10


def test_an_excluded_page_releases_its_hold(account):
    billing.grant(account, 10)
    book = make_book(account, [PS.LAYOUT_DONE, PS.LAYOUT_DONE])
    page, token = _held_run(account, book)
    Page.objects.filter(pk=page.pk).update(is_excluded=True)
    with patch.object(ocr_services, "run_full_ocr") as run:
        ocr_tasks.ocr_page_full.apply(args=[page.pk], kwargs={"run": token, "last": True}).get()
    run.assert_not_called()
    assert not QuotaHold.objects.exists() and not QuotaEntry.objects.filter(kind="consume").exists()
    # «استثناء» itself releases the hold of a page queued for reading
    other = book.pages.get(number=2)
    billing.hold(book, [other], "r")
    services.toggle_exclude(other)
    assert not QuotaHold.objects.exists()


def test_a_requeued_task_keeps_its_hold_until_it_ends(account):
    billing.grant(account, 10)
    book = make_book(account, [PS.LAYOUT_DONE])
    page, token = _held_run(account, book)
    with patch.object(ocr_services, "run_full_ocr", side_effect=OSError("disk gone")), pytest.raises(Retry):
        ocr_tasks.ocr_page_full.apply(args=[page.pk], kwargs={"run": token, "last": True})
    assert billing.held(account) == 1 and not QuotaEntry.objects.filter(kind="consume").exists()


def test_a_run_that_died_releases_its_hold(account):
    billing.grant(account, 10)
    book = make_book(account, [PS.LAYOUT_DONE])
    page, token = _held_run(account, book)
    request = SimpleNamespace(task="ocr.tasks.ocr_page_full")
    tasks.page_run_failed(request, TimeoutError("hard limit"), None, page_id=page.pk, run=token)
    assert not QuotaHold.objects.exists()


def test_a_page_rerun_end_to_end_charges_the_account(account, editor):
    billing.grant(account, 3)
    book = make_book(account, [PS.OCR_DONE])
    page = book.pages.get()
    with (
        patch.object(ocr_services, "run_fast_ocr"),
        patch.object(ocr_services, "run_full_ocr", side_effect=_read_ok),
    ):
        services.run_stage(page, "ocr", user=editor)  # eager: Tesseract, then the models
    assert billing.balance(account) == 2 and not QuotaHold.objects.exists()
    assert QuotaEntry.objects.filter(kind="consume", page=page).count() == 1


def test_an_unlimited_account_is_never_held_nor_charged(account, editor):
    account.unlimited = True
    account.save()
    book = make_book(account, [PS.OCR_DONE])
    page = book.pages.get()
    with (
        patch.object(ocr_services, "run_fast_ocr"),
        patch.object(ocr_services, "run_full_ocr", side_effect=_read_ok),
    ):
        services.run_stage(page, "ocr", user=editor)
    assert not QuotaHold.objects.exists() and not QuotaEntry.objects.exists()


# ====================================================================== the form's line in books.js

QUOTA_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
globalThis.window = globalThis;
globalThis.document = { addEventListener() {} };
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
const q = window.NassakhBooks.quotaLine;
console.log(JSON.stringify([
  q({ available: null, count: 10 }),
  q({ available: 7 }),
  q({ available: 20, count: 12, skipFirst: 1, skipLast: 1 }),
  q({ available: 13, count: 10, skipFirst: 2, skipLast: 1, perSheet: 2 }),
  q({ available: -3, count: 2 }),
]));
"""


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="node is not installed")
def test_the_form_counts_the_pages_in_the_browser(tmp_path):
    import json
    import subprocess
    from pathlib import Path

    harness = tmp_path / "harness.js"
    harness.write_text(QUOTA_HARNESS, encoding="utf-8")
    books_js = Path(__file__).resolve().parent.parent / "static" / "src" / "js" / "books.js"
    run = subprocess.run(["node", str(harness), str(books_js)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out == [
        {"text": "", "error": False},
        {"text": "رصيدك المتاح: 7 صفحات.", "error": False},
        {"text": "رصيدك المتاح: 20 صفحة · يقرأ هذا الكتاب نحو 10 صفحات.", "error": False},
        {"text": "هذا الكتاب نحو 14 صفحة ورصيدك المتاح 13 صفحة.", "error": True},
        {"text": "هذا الكتاب نحو صفحتين ورصيدك المتاح 0 صفحة.", "error": True},
    ]
