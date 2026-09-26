"""Tests of the export pipeline (PHASE6_SPEC §6, §7, §11.2), with `FakeExporter`: the `Export` model and
its partial unique constraint, options, `request_export` (202 · 409 · 404 · 400, an abandoned row
replaced, an enqueue failure), `run_export` (the file's path, the inputs read once, claims and
redeliveries, an edit during the run), cancelling, errors, retention, file names, the API and the page,
the task's queue, the page payload's query count, readiness, `export_book`, and the real payloads
against the §3.2 fixtures (`publishing/fixtures/export/`)."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

import pytest

from assembly.models import AssemblyRun
from books.models import Book
from books.models import Page as ScanPage
from editor.models import Manuscript, StyleSheet
from publishing import exporters, exports, readiness, tasks
from publishing.exporters import (
    ExportCancelled,
    ExportResult,
    FakeExporter,
    InvalidExport,
    OptionsError,
    OptionSpec,
    parse_options,
)
from publishing.models import Export
from publishing.tests import document, heading, logged, long_book, para, role_user, text

FIXTURES = Path(__file__).parent / "fixtures" / "export"


# ====================================================================== fixtures


@pytest.fixture(autouse=True)
def clean_media():
    """No export files left over from another test (row ids repeat across tests)."""
    import shutil

    shutil.rmtree(Path(settings.MEDIA_ROOT) / "books", ignore_errors=True)


@pytest.fixture
def fake(monkeypatch):
    """A registry holding only the fake Word exporter (the real exporters of the other streams are not
    loaded, so these tests do not depend on them)."""
    monkeypatch.setattr(exporters, "_registry", {})
    monkeypatch.setattr(exporters, "_load", lambda format: None)
    exporter = FakeExporter("docx")
    exporters.register(exporter)
    return exporter


@pytest.fixture
def book(db):
    book = Book.objects.create(title="كتابي", author="المؤلف")
    run = AssemblyRun.objects.create(book=book, status="done")
    source = long_book(chapters=2, paragraphs=3)
    source["content"][0]["attrs"]["text"] = book.title  # the manuscript's title node, as assembly writes it
    Manuscript.objects.create(book=book, document=source, version=1, run=run)
    return book


@pytest.fixture
def editor(db):
    return role_user("editor-1", "editor")


@pytest.fixture
def reader(db):
    return role_user("reader-1", "proofreader")


def queued(book, monkeypatch, user=None, options=None, format="docx") -> Export:
    """A queued export (the task is not sent)."""
    monkeypatch.setattr(exports, "_enqueue", lambda row: None)
    return exports.request_export(book, format, options or {}, user)


def done_row(book, *, data=b"file", format="docx", **fields) -> Export:
    row = Export.objects.create(
        book=book, format=format, status=Export.Status.DONE, filename="كتابي.docx", **fields
    )
    row.file.save(f"x{exporters.FORMATS[format].extension}", ContentFile(data), save=False)
    row.size_bytes = len(data)
    row.save()
    return row


def edit_manuscript(book, value="نص جديد بعد الإخراج") -> None:
    manuscript = Manuscript.objects.get(book=book)
    manuscript.document["content"][2]["content"][0]["text"] = value
    manuscript.version += 1
    manuscript.save()


def post(client, url, data=None):
    return client.post(url, data=json.dumps(data or {}), content_type="application/json")


# ====================================================================== model


def test_one_active_export_per_book_and_format(book, db):
    Export.objects.create(book=book, format="docx", status="queued")
    Export.objects.create(book=book, format="print_pdf", status="running")  # another format: fine
    Export.objects.create(book=book, format="docx", status="done")  # finished rows: any number
    Export.objects.create(book=book, format="docx", status="error")
    other = Book.objects.create(title="كتاب آخر")
    Export.objects.create(book=other, format="docx", status="running")  # another book: fine
    with pytest.raises(IntegrityError), transaction.atomic():
        Export.objects.create(book=book, format="docx", status="running")
    assert Export.objects.filter(book=book).count() == 4


def test_the_file_path_and_the_admin_list(book, db, admin_client):
    row = done_row(book)
    assert row.file.name == f"books/{book.pk}/exports/{row.pk}.docx"
    assert admin_client.get(reverse("admin:publishing_export_changelist")).status_code == 200


# ====================================================================== options


def test_options_are_normalised_through_the_specs():
    specs = (
        OptionSpec("kashida", "choice", "low", choices=("none", "low", "medium", "high")),
        OptionSpec("comments", "bool", False),
        OptionSpec("bleed_mm", "choice", 0, choices=(0, 3, 5)),
        OptionSpec("size", "float", 1.0, bounds=(0.5, 2.0)),
        OptionSpec("count", "int", 2, bounds=(1, 9)),
    )
    assert parse_options(specs, None) == {
        "kashida": "low",
        "comments": False,
        "bleed_mm": 0,
        "size": 1.0,
        "count": 2,
    }
    got = parse_options(
        specs, {"kashida": "high", "comments": "true", "bleed_mm": "3", "size": 2, "count": "4", "x": 1}
    )
    assert got == {
        "kashida": "high",
        "comments": True,
        "bleed_mm": 3,
        "size": 2.0,
        "count": 4,
    }  # unknown key dropped
    with pytest.raises(OptionsError) as caught:
        parse_options(
            specs, {"kashida": "extreme", "comments": "maybe", "bleed_mm": 4, "size": 9, "count": 1.5}
        )
    assert set(caught.value.errors) == {"kashida", "comments", "bleed_mm", "size", "count"}
    assert caught.value.errors["size"] == "يُنتظر رقم بين 0.5 و2."
    with pytest.raises(OptionsError):
        parse_options(specs, {"bleed_mm": False})  # a bool is never a number choice


def test_options_text_and_the_last_options_used(book, editor, fake, monkeypatch):
    assert exporters.options_text(fake, {"kashida": "low", "comments": True}) == "كشيدة خفيفة · تعليقات"
    assert exporters.options_text(fake, {"kashida": "none", "comments": False}) == "بلا كشيدة"
    exports.request_export(book, "docx", {"kashida": "medium"}, editor)
    form = exports.page_payload(book, editor)["formats"][0]["form"]
    assert form["kashida"]["value"] == "medium"  # the last options used for this book and format


# ====================================================================== request_export


def test_an_export_runs_through_the_queue_into_its_file(book, editor, fake):
    client = logged(editor)
    response = post(
        client, reverse("api:exports", args=[book.pk]), {"format": "docx", "options": {"comments": True}}
    )
    assert response.status_code == 202
    row = Export.objects.get(pk=response.json()["id"])
    assert row.status == "done" and row.task_id  # eager Celery ran it at once
    assert row.file.name == f"books/{book.pk}/exports/{row.pk}.docx"
    assert default_storage.open(row.file.name).read() == fake.data
    assert row.filename == "كتابي - مع التعليقات.docx" and row.size_bytes == len(fake.data)
    assert row.options == {"kashida": "low", "comments": True} and row.created_by == editor
    assert row.manuscript_version == 1 and row.renderer == "fake-1" and row.stylesheet_hash
    assert set(row.inputs) == {"stylesheet", "title", "author", "digit_style", "chapters"}
    assert "updated_at" not in row.inputs["stylesheet"] and set(row.inputs["chapters"]) == {"h1", "h2"}
    assert row.started_at and row.finished_at and row.progress["step"] == "done"
    job = fake.calls[0]
    assert job.export_id == row.pk and job.book_id == book.pk and job.options == row.options
    assert job.chapter_versions == row.inputs["chapters"] and job.manuscript_version == 1


def test_a_second_request_while_one_is_active_gets_409_with_it(book, editor, fake, monkeypatch):
    first = queued(book, monkeypatch, editor)
    with pytest.raises(exports.ExportConflict) as caught:
        exports.request_export(book, "docx", {}, editor)
    assert caught.value.row.pk == first.pk
    response = post(logged(editor), reverse("api:exports", args=[book.pk]), {"format": "docx"})
    assert response.status_code == 409
    body = response.json()
    assert (
        body["detail"] == exports.BUSY
        and body["active"]["id"] == first.pk
        and body["active"]["status"] == "queued"
    )


def test_refusals_404_and_400(book, editor, fake, db):
    client = logged(editor)
    bare = Book.objects.create(title="بلا مخطوطة")
    response = post(client, reverse("api:exports", args=[bare.pk]), {"format": "docx"})
    assert response.status_code == 404 and response.json() == {"detail": exports.NO_MANUSCRIPT}
    response = post(client, reverse("api:exports", args=[book.pk]), {"format": "epub"})
    assert response.status_code == 400 and response.json()["detail"] == exports.NOT_AVAILABLE
    response = post(client, reverse("api:exports", args=[book.pk]), {"format": "rtf"})
    assert response.status_code == 400 and response.json()["detail"] == exports.UNKNOWN_FORMAT
    response = post(
        client, reverse("api:exports", args=[book.pk]), {"format": "docx", "options": {"kashida": "x"}}
    )
    assert response.status_code == 400
    assert response.json() == {"detail": exports.BAD_OPTIONS, "errors": {"kashida": exporters.BAD_CHOICE}}
    assert Export.objects.count() == 0


def test_an_abandoned_active_row_is_replaced(book, editor, fake, monkeypatch):
    old = queued(book, monkeypatch, editor)
    Export.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(seconds=1800 + 301))
    old.refresh_from_db()
    assert exports.export_payload(old, editor)["status"] == "error"  # shown as abandoned at once
    new = exports.request_export(book, "docx", {}, editor)
    old.refresh_from_db()
    assert old.status == "error" and old.error == exports.ABANDONED and new.pk != old.pk


def test_an_enqueue_failure_leaves_the_row_in_error(book, editor, fake, monkeypatch):
    def refuse(*args, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(tasks.run_export, "delay", refuse)
    row = exports.request_export(book, "docx", {}, editor)
    assert row.status == "error" and row.error.startswith(exports.ENQUEUE_ERROR)
    assert "ConnectionError: redis down" in row.error
    payload = exports.export_payload(row, editor)
    assert (
        payload["error"] == exports.ENQUEUE_ERROR and payload["error_detail"] == "ConnectionError: redis down"
    )


# ====================================================================== run_export


def test_a_final_row_is_left_as_it_is(book, fake):
    row = done_row(book)
    assert exports.run_export(row.pk, "task-1").status == "done" and fake.calls == []
    assert exports.run_export(987654, "task-1") is None


def test_a_row_running_under_another_task_is_left_and_a_redelivery_restarts(book, editor, fake, monkeypatch):
    row = queued(book, monkeypatch, editor)
    Export.objects.filter(pk=row.pk).update(status="running", task_id="task-a")
    assert exports.run_export(row.pk, "task-b").status == "running" and fake.calls == []
    again = exports.run_export(row.pk, "task-a")  # acks_late redelivery of the same task
    assert again.status == "done" and len(fake.calls) == 1


class DocumentExporter(FakeExporter):
    """Writes the job's document (what the file was made from) and edits the book during `write`."""

    def export(self, job, progress):
        progress("prepare")
        edit_manuscript(Book.objects.get(pk=job.book_id))
        progress("write")
        return ExportResult(data=json.dumps(job.document, ensure_ascii=False).encode())


def test_an_edit_during_the_run_does_not_get_into_the_file(book, editor, monkeypatch):
    monkeypatch.setattr(exporters, "_registry", {})
    monkeypatch.setattr(exporters, "_load", lambda format: None)
    exporters.register(DocumentExporter("docx"))
    row = exports.request_export(book, "docx", {}, editor)
    assert row.status == "done" and row.manuscript_version == 1
    written = default_storage.open(row.file.name).read().decode()
    assert "نص جديد بعد الإخراج" not in written and "هذا نص تجريبي" in written
    payload = exports.export_payload(row, editor)
    assert payload["stale"] is True and payload["stale_text"] == "تغيّر النص بعد هذا الإخراج"


def test_stale_reasons_for_the_format_and_the_renderer(book, editor, fake):
    row = exports.request_export(book, "docx", {}, editor)
    current = exports.current_state(book)
    assert exports.stale_reasons(row, current) == []
    StyleSheet.objects.create(book=book, body_size_pt=14)
    assert exports.stale_reasons(row, exports.current_state(book)) == ["format"]
    fake.version = "fake-2"
    assert exports.stale_reasons(row, exports.current_state(book)) == ["format", "renderer"]
    assert exports.export_payload(row, editor)["stale_text"] == "تغيّر التنسيق بعد هذا الإخراج"


def test_warnings_and_the_page_count_are_recorded(book, editor, monkeypatch):
    monkeypatch.setattr(exporters, "_registry", {})
    monkeypatch.setattr(exporters, "_load", lambda format: None)
    warning = {"code": "toc_update", "level": "warn", "message": "حدّث المحتويات."}
    exporters.register(FakeExporter("screen_pdf", page_count=12, warnings=[warning, {"code": "x"}]))
    row = exports.request_export(book, "screen_pdf", {}, editor)
    assert row.status == "done" and row.page_count == 12 and row.filename == "كتابي.pdf"
    assert row.warnings == [warning]  # a row without a message is dropped
    assert exports.export_payload(row, editor)["warnings"] == [warning]


# ====================================================================== cancel


def test_a_cancelled_queued_row_never_runs(book, editor, fake, monkeypatch):
    row = queued(book, monkeypatch, editor)
    revoked = []
    monkeypatch.setattr("publishing.preview.revoke", lambda task_id: revoked.append(task_id))
    Export.objects.filter(pk=row.pk).update(task_id="task-q")
    row = exports.cancel_export(row)
    assert row.status == "cancelled" and row.error == exports.CANCELLED_BY_USER and revoked == ["task-q"]
    assert exports.cancel_export(row).status == "cancelled"  # again: nothing to do
    assert exports.run_export(row.pk, "task-q").status == "cancelled" and fake.calls == []
    assert exports.export_payload(row, editor)["error"] == ""  # «أُلغي» is the status, not an error


def test_a_running_export_stops_when_cancelled_and_leaves_no_file(book, editor, monkeypatch):
    def cancel(step, job, progress):
        if step == "prepare":
            Export.objects.filter(pk=job.export_id).update(status="cancelled")

    monkeypatch.setattr(exporters, "_registry", {})
    monkeypatch.setattr(exporters, "_load", lambda format: None)
    fake = exporters.register(FakeExporter("docx", on_step=cancel))
    row = exports.request_export(book, "docx", {}, editor)
    assert row.status == "cancelled" and not row.file
    assert fake.calls and not default_storage.exists(f"books/{book.pk}/exports/{row.pk}.docx")


def test_a_cancel_during_the_write_deletes_the_file(book, editor, fake, monkeypatch):
    real_save = default_storage.save
    saved = []

    def save_then_cancel(name, content, **kwargs):
        path = real_save(name, content, **kwargs)
        saved.append(path)
        Export.objects.filter(file="", status="running").update(status="cancelled")
        return path

    monkeypatch.setattr(exports.default_storage, "save", save_then_cancel)
    row = exports.request_export(book, "docx", {}, editor)
    assert row.status == "cancelled" and saved and not default_storage.exists(saved[0])


def test_cancelling_a_finished_export_is_409(book, editor, fake):
    row = exports.request_export(book, "docx", {}, editor)
    response = post(logged(editor), reverse("api:export_cancel", args=[book.pk, row.pk]))
    assert response.status_code == 409 and response.json()["detail"] == exports.FINISHED
    assert response.json()["row"]["id"] == row.pk


def test_the_reporter_writes_at_most_every_half_second_and_sees_a_cancel(book, editor, fake, monkeypatch):
    row = queued(book, monkeypatch, editor)
    Export.objects.filter(pk=row.pk).update(status="running")
    reporter = exports.Reporter(row.pk)
    reporter("layout", 1, 4)
    reporter("layout", 2, 4)  # the same step within 0.5 s: not written
    row.refresh_from_db()
    assert row.progress == {"step": "layout", "done": 1, "total": 4}
    assert exports.export_payload(row, editor)["progress"]["percent"] == 25
    reporter("write")  # a new step: written at once
    row.refresh_from_db()
    assert row.progress["step"] == "write" and not reporter.cancelled()
    Export.objects.filter(pk=row.pk).update(status="cancelled")
    reporter("check")
    assert reporter.cancelled()


# ====================================================================== errors


@pytest.mark.parametrize(
    ("error", "headline", "line"),
    [
        (RuntimeError("boom"), exports.EXPORT_ERROR, "RuntimeError: boom"),
        (SoftTimeLimitExceeded(), exports.TIMEOUT, "SoftTimeLimitExceeded"),
        (
            InvalidExport("word/document.xml: bad jc"),
            exports.INVALID_FILE,
            "InvalidExport: word/document.xml: bad jc",
        ),
    ],
)
def test_failures_carry_the_arabic_headline_and_the_technical_line(
    book, editor, reader, monkeypatch, error, headline, line
):
    monkeypatch.setattr(exporters, "_registry", {})
    monkeypatch.setattr(exporters, "_load", lambda format: None)
    exporters.register(FakeExporter("docx", error=error))
    row = exports.request_export(book, "docx", {}, editor)
    assert (
        row.status == "error" and row.error.split("\n")[0] == headline and line in row.error and not row.file
    )
    mine = exports.export_payload(row, editor)
    assert mine["error"] == headline and line in mine["error_detail"] and mine["download_url"] is None
    assert exports.export_payload(row, reader)["error_detail"] == ""  # the technical line: editors only


def test_an_exporter_cancelling_itself_leaves_the_row_cancelled(book, editor, monkeypatch):
    monkeypatch.setattr(exporters, "_registry", {})
    monkeypatch.setattr(exporters, "_load", lambda format: None)
    exporters.register(FakeExporter("docx", error=ExportCancelled()))
    assert exports.request_export(book, "docx", {}, editor).status == "cancelled"


# ====================================================================== retention


def test_five_done_and_three_failed_rows_are_kept_per_format(book, db):
    done = [done_row(book, data=f"file {i}".encode()) for i in range(7)]
    failed = [Export.objects.create(book=book, format="docx", status="error") for _ in range(5)]
    other = done_row(book, format="screen_pdf")
    assert exports.prune(book.pk, "docx") == 4
    kept = set(Export.objects.filter(book=book).values_list("pk", flat=True))
    assert kept == {row.pk for row in done[2:]} | {row.pk for row in failed[2:]} | {other.pk}
    for row in done[:2]:
        assert not default_storage.exists(row.file.name)
    assert all(default_storage.exists(row.file.name) for row in done[2:])


# ====================================================================== file names


def test_file_names_clean_the_arabic_title():
    title = 'كتاب/الأمثال: "الجزء‫ الأول" <مختصر>?|*\\‏' + " طويل" * 60 + " ."
    name = exports.clean_title(title, 7)
    assert not any(char in name for char in '/\\:*?"<>|‫‏')
    assert name.startswith("كتابالأمثال الجزء الأول مختصر طويل") and len(name) <= 100
    assert not name.endswith((" ", "."))
    assert exports.clean_title(" ..‮.. ", 7) == "كتاب-7"
    assert exports.clean_title("كتاب\n\tالسطرين", 7) == "كتاب السطرين"
    assert exports.file_name("كتابي", "docx", {"comments": True}, 1) == "كتابي - مع التعليقات.docx"
    assert exports.file_name("كتابي", "docx", {"comments": False}, 1) == "كتابي.docx"
    assert exports.file_name("كتابي", "print_pdf", {}, 1) == "كتابي - للطباعة.pdf"
    assert exports.file_name("كتابي", "screen_pdf", {}, 1) == "كتابي.pdf"
    assert exports.file_name("كتابي", "epub", {}, 1) == "كتابي.epub"
    assert exports.size_text(325632) == "318 KB" and exports.size_text(1258291) == "1.2 MB"
    assert exports.size_text(0) == "" and exports.size_text(15 * 1024 * 1024) == "15 MB"


def test_the_download_sends_the_arabic_name(book, editor, reader, fake):
    row = exports.request_export(book, "docx", {"comments": True}, editor)
    response = logged(reader).get(reverse("publishing:export_download", args=[book.pk, row.pk]))
    assert response.status_code == 200 and b"".join(response.streaming_content) == fake.data
    disposition = response["Content-Disposition"]
    assert disposition.startswith("attachment;") and f"filename*=utf-8''{quote(row.filename)}" in disposition
    assert response["Cache-Control"] == "private, no-cache"
    assert response["Content-Type"].startswith("application/vnd.openxmlformats-officedocument")
    inline = logged(reader).get(reverse("publishing:export_download", args=[book.pk, row.pk]) + "?inline=1")
    assert inline["Content-Disposition"].startswith("inline;")


# ====================================================================== API and pages


def test_who_may_read_download_start_and_cancel(book, editor, reader, fake, monkeypatch, client):
    row = queued(book, monkeypatch, editor)
    exports_url = reverse("api:exports", args=[book.pk])
    assert client.get(exports_url).status_code == 403  # anonymous
    assert post(client, exports_url, {"format": "docx"}).status_code == 403
    download = reverse("publishing:export_download", args=[book.pk, row.pk])
    assert client.get(download).status_code == 302  # the login page
    assert client.get(reverse("publishing:export", args=[book.pk])).status_code == 302
    proofreader = logged(reader)
    assert proofreader.get(exports_url).status_code == 200
    assert proofreader.get(reverse("api:export", args=[book.pk, row.pk])).status_code == 200
    assert post(proofreader, exports_url, {"format": "docx"}).status_code == 403
    assert post(proofreader, reverse("api:export_cancel", args=[book.pk, row.pk])).status_code == 403
    assert proofreader.get(download).status_code == 404  # not done yet: «الملف غير جاهز.»
    response = post(logged(editor), reverse("api:export_cancel", args=[book.pk, row.pk]))
    assert response.status_code == 200 and response.json()["status"] == "cancelled"


def test_another_books_export_is_404(book, editor, fake):
    row = exports.request_export(book, "docx", {}, editor)
    other = Book.objects.create(title="كتاب آخر")
    client = logged(editor)
    assert client.get(reverse("api:export", args=[other.pk, row.pk])).status_code == 404
    assert client.get(reverse("publishing:export_download", args=[other.pk, row.pk])).status_code == 404
    assert post(client, reverse("api:export_cancel", args=[other.pk, row.pk])).status_code == 404
    default_storage.delete(row.file.name)  # the file is gone: 404 as well
    assert client.get(reverse("publishing:export_download", args=[book.pk, row.pk])).status_code == 404


def test_the_long_poll_is_capped_and_returns_early_on_a_change(book, editor, fake, monkeypatch):
    row = queued(book, monkeypatch, editor)
    monkeypatch.setattr(exports, "MAX_WAIT_S", 0.3)
    monkeypatch.setattr(exports, "WAIT_STEP_S", 0.02)
    client = logged(editor)
    url = reverse("api:export", args=[book.pk, row.pk])
    started = time.monotonic()
    response = client.get(url, {"wait": 60, "since": exports.iso(row.updated_at)})
    assert response.status_code == 200 and 0.25 <= time.monotonic() - started < 2  # capped
    earlier = exports.iso(row.updated_at - timedelta(seconds=1))
    started = time.monotonic()
    assert client.get(url, {"wait": 5, "since": earlier}).json()["id"] == row.pk
    assert time.monotonic() - started < 0.25  # changed since: at once
    from publishing.api import _since

    stamp = exports.iso(row.updated_at)
    assert _since(stamp.replace("+", " ")) == row.updated_at  # an unescaped `+` arrives as a space
    assert _since("not a time") is None and client.get(url, {"wait": "x"}).status_code == 200


def test_download_url_only_when_done(book, editor, fake, monkeypatch):
    row = queued(book, monkeypatch, editor)
    assert exports.export_payload(row, editor)["download_url"] is None
    Export.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(seconds=61))
    row.refresh_from_db()
    assert exports.export_payload(row, editor)["waiting_hint"] == exports.WAITING_HINT
    finished = exports.run_export(row.pk, "task-1")
    payload = exports.export_payload(finished, editor)
    assert payload["download_url"] == reverse("publishing:export_download", args=[book.pk, row.pk])
    assert payload["size_text"] and payload["waiting_hint"] == ""


def test_the_export_page_embeds_the_payload(book, editor, fake):
    response = logged(editor).get(reverse("publishing:export", args=[book.pk]))
    assert response.status_code == 200
    body = response.content.decode()
    assert 'id="export-config"' in body and "الإخراج · كتابي · نسّاخ" in body
    assert response.context["config"]["book"]["id"] == book.pk


def test_the_book_page_config_takes_a_panel_tab(book, editor):
    from editor.services import page_config

    assert page_config(book, editor, page="layout", tab="uncertain")["tab"] == "uncertain"
    assert page_config(book, editor, page="layout", tab="nope")["tab"] is None
    assert page_config(book, editor, page="layout")["tab"] is None


# ====================================================================== other


def test_the_export_task_has_its_own_queue():
    from nassakh.celery import app

    assert app.amqp.router.route({}, "publishing.tasks.run_export")["queue"].name == "export"
    assert tasks.run_export.soft_time_limit == settings.NASSAKH["EXPORT_SOFT_LIMIT_S"]
    assert tasks.run_export.time_limit == settings.NASSAKH["EXPORT_SOFT_LIMIT_S"] + 120
    root = Path(settings.BASE_DIR)
    for name in ("Makefile", "Procfile"):
        assert "worker -Q default,layout,export" in (root / name).read_text(), name


def test_the_page_payload_takes_a_bounded_number_of_queries(
    book, editor, fake, django_assert_max_num_queries
):
    for _ in range(3):
        exports.request_export(book, "docx", {}, editor)
    for status in ("error", "cancelled", "error"):
        Export.objects.create(book=book, format="docx", status=status, created_by=editor)
    for _ in range(4):
        done_row(book, format="screen_pdf", created_by=editor)
    fresh = Book.objects.get(pk=book.pk)
    with django_assert_max_num_queries(10):
        payload = exports.page_payload(fresh, editor)
    assert len(payload["items"]) == 10


def test_export_book_writes_the_file_without_a_row(book, fake, tmp_path, capsys):
    call_command("export_book", str(book.pk), "--format", "docx", "--comments", "--out", str(tmp_path))
    path = tmp_path / "كتابي - مع التعليقات.docx"
    assert path.read_bytes() == fake.data and Export.objects.count() == 0
    output = capsys.readouterr().out
    assert str(path.resolve()) in output and '"comments": true' in output
    call_command("export_book", str(book.pk), "--format", "docx", "--out", str(tmp_path / "x.docx"))
    assert (tmp_path / "x.docx").is_file()


# ====================================================================== readiness


def rows_of(book) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for item in readiness.book_readiness(Book.objects.get(pk=book.pk)):
        out.setdefault(item["code"], []).append(item)
    return out


def test_a_ready_book_reads_clear(book):
    assert rows_of(book) == {"clear": [{"code": "clear", "level": "success", "message": readiness.CLEAR}]}


def test_uncertain_words_and_kraken_numbers(book, db):
    page = ScanPage.objects.create(book=book, number=10, source_index=9)
    from ocr.models import Line

    tokens = [
        {"t": "كلمه", "orig": "كلمه", "conf": "low"},
        {"t": "12", "conf": "low", "src": "kraken"},
        {"t": "ثالثه", "conf": "low"},
    ]
    line = Line.objects.create(page=page, order=0, tokens=tokens, text="كلمه 12 ثالثه")
    source = document(
        heading("h1", "الفصل"),
        para(
            "p1",
            text("كلمه", "uncertain"),
            " ثم ",
            text("12", "uncertain"),
            " و",
            text("ثالثه", "uncertain"),
            sourceLineIds=[line.pk],
        ),
    )
    Manuscript.objects.filter(book=book).update(document=source)
    found = rows_of(book)["uncertain_words"][0]
    assert found["level"] == "warn" and found["message"] == "بقيت 3 كلمات غير مؤكَّدة في النص، منها رقم واحد."
    assert found["action"] == {"label": "غير المؤكَّدة", "url": f"/books/{book.pk}/layout/?tab=uncertain"}
    counts = readiness.UncertainCounts
    assert readiness.uncertain_message(counts(2, 0)) == "بقيت كلمتان غير مؤكَّدتين في النص."
    assert readiness.uncertain_message(counts(12, 3)) == "بقيت 12 كلمة غير مؤكَّدة في النص، منها 3 أرقام."
    assert readiness.uncertain_message(counts(1, 1)) == "بقيت كلمة واحدة غير مؤكَّدة في النص، منها رقم واحد."


def test_review_drift_and_a_running_assembly(book, monkeypatch):
    monkeypatch.setattr(
        "editor.services.review_drift",
        lambda book, manuscript=None: {"edited": True, "pages": [3, 4, 5, 9], "chapters": {}},
    )
    AssemblyRun.objects.create(book=book, status="running")
    found = rows_of(book)
    assert found["review_drift"][0]["message"] == "تغيّر نص 4 صفحات في المراجعة بعد التحرير."
    assert found["review_drift"][0]["action"] == {  # D78: the book page's «تغييرات المراجعة» tab
        "label": "تغييرات المراجعة",
        "url": f"/books/{book.pk}/layout/?tab=changes",
    }
    assert found["assembly_running"][0] == {
        "code": "assembly_running",
        "level": "warn",
        "message": readiness.ASSEMBLY_RUNNING,
    }
    assert "clear" not in found


def test_book_details_and_no_headings(book):
    Book.objects.filter(pk=book.pk).update(author="")
    StyleSheet.objects.create(book=book, front_matter={"copyright_page": True, "fields": {"isbn": "123"}})
    Manuscript.objects.filter(book=book).update(document=document(para("p1", "نص بلا عناوين."), author=""))
    found = rows_of(book)
    assert [item["message"] for item in found["book_details"]] == [readiness.NO_AUTHOR, readiness.NO_IMPRINT]
    assert found["book_details"][0]["action"]["url"] == f"/books/{book.pk}/layout/?tab=format"
    assert found["no_headings"][0]["level"] == "info"


def test_helpers_for_the_exporters_notes(book, tmp_path, settings):
    settings.NASSAKH = {**settings.NASSAKH, "FONT_DIRS": [str(tmp_path)]}
    from publishing.model import page_setup

    rows = readiness.missing_font_rows(book.pk, page_setup({"body_font": "traditional_arabic"}))
    first = rows[0]
    assert (
        first["code"] == "font_missing" and first["level"] == "warn" and first["action"]["label"] == "الخطوط"
    )
    assert first["message"] == (
        "الخط «Traditional Arabic» غير مثبّت على هذا الجهاز؛ يُستعمل أميري بدلًا منه، كما في المعاينة."
    )
    assert readiness.page_checks_row(book.pk) is None and readiness.layout_is_current(book) is False


# ====================================================================== the §3.2 fixtures

OPTIONAL = {"action"}


def fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _pick(models: list, item):
    if isinstance(item, dict):
        for key in ("code", "key", "status", "value"):
            if key in item:
                for model in models:
                    if isinstance(model, dict) and model.get(key) == item[key]:
                        return model
    return models[0]


def same_shape(real, model, path: str = "$") -> None:
    """`real` has the fixture's keys (exactly, but for the optional ones) and JSON types; null matches
    anything (the fixtures show one state)."""
    if model is None or real is None:
        return
    if isinstance(model, dict):
        assert isinstance(real, dict), path
        missing = set(model) - set(real) - OPTIONAL
        extra = set(real) - set(model) - OPTIONAL
        assert not missing and not extra, f"{path}: missing {sorted(missing)}, extra {sorted(extra)}"
        for key in set(model) & set(real):
            same_shape(real[key], model[key], f"{path}.{key}")
    elif isinstance(model, list):
        assert isinstance(real, list), path
        if model:
            for index, item in enumerate(real):
                same_shape(item, _pick(model, item), f"{path}[{index}]")
    elif isinstance(model, bool):
        assert isinstance(real, bool), path
    elif isinstance(model, int | float):
        assert isinstance(real, int | float) and not isinstance(real, bool), path
    else:
        assert isinstance(real, type(model)), path


def test_the_fixtures_follow_the_contract():
    rows = {
        name: fixture(f"row-{name}")
        for name in ("queued", "running", "done", "done-stale", "error", "cancelled")
    }
    assert {row["status"] for row in rows.values()} == set(Export.Status.values)
    for row in rows.values():
        same_shape(row, rows["done"])
        assert row["status_label"] == Export.Status(row["status"]).label
        assert row["progress"]["label"] == exporters.STEPS[row["progress"]["step"]]
    page = fixture("page")
    assert [item["key"] for item in page["formats"]] == list(exporters.FORMATS)
    assert all(item["label"] == exporters.FORMATS[item["key"]].label for item in page["formats"])
    same_shape(fixture("page-running"), page)


def test_the_real_payloads_match_the_fixtures(book, editor, fake, monkeypatch):
    for key in ("print_pdf", "screen_pdf", "epub"):  # every format available, as with the real exporters
        exporters.register(FakeExporter(key))
    AssemblyRun.objects.create(book=book, status="queued")
    model_row = fixture("row-done")
    finished = exports.request_export(book, "docx", {"comments": True}, editor)
    same_shape(exports.export_payload(finished, editor), model_row)
    edit_manuscript(book)
    stale = exports.export_payload(finished, editor)
    same_shape(stale, fixture("row-done-stale"))
    assert stale["stale"] and stale["stale_text"] == fixture("row-done-stale")["stale_text"]
    monkeypatch.setattr(exports, "_enqueue", lambda row: None)
    active = exports.request_export(book, "docx", {}, editor)
    same_shape(exports.export_payload(active, editor), fixture("row-queued"))
    Export.objects.filter(pk=active.pk).update(
        status="running", progress={"step": "write", "done": None, "total": None}
    )
    active.refresh_from_db()
    running = exports.export_payload(active, editor)
    same_shape(running, fixture("row-running"))
    assert running["progress"] == fixture("row-running")["progress"]
    response = post(logged(editor), reverse("api:exports", args=[book.pk]), {"format": "docx"})
    same_shape(response.json(), fixture("response-conflict"))
    page = logged(editor).get(reverse("api:exports", args=[book.pk])).json()
    same_shape(page, fixture("page-running"))
    assert page["urls"] == {
        "create": f"/api/books/{book.pk}/exports/",
        "row": f"/api/books/{book.pk}/exports/__eid__/",
        "cancel": f"/api/books/{book.pk}/exports/__eid__/cancel/",
    }
    assert (
        page["formats"][0]["active"]["id"] == active.pk and page["formats"][0]["latest"]["id"] == finished.pk
    )
    assert [item["available"] for item in page["formats"]] == [True, True, True, True]
    cancelled = exports.cancel_export(active)
    same_shape(exports.export_payload(cancelled, editor), fixture("row-cancelled"))
    Export.objects.create(
        book=book,
        format="docx",
        status="error",
        options={"kashida": "medium", "comments": False},
        error=f"{exports.INVALID_FILE}\nInvalidExport: x",
    )
    error = Export.objects.filter(book=book, status="error").first()
    same_shape(exports.export_payload(error, editor), fixture("row-error"))
    response = post(
        logged(editor),
        reverse("api:exports", args=[book.pk]),
        {"format": "docx", "options": {"kashida": "?"}},
    )
    same_shape(response.json(), fixture("response-bad-options"))
    assert response.json() == fixture("response-bad-options")
    empty = Book.objects.create(title="كتاب بلا مخطوطة")
    same_shape(exports.page_payload(empty, editor), fixture("page-empty"))
    response = post(logged(editor), reverse("api:exports", args=[empty.pk]), {"format": "docx"})
    assert response.json() == fixture("response-no-manuscript")
    response = post(logged(editor), reverse("api:export_cancel", args=[book.pk, finished.pk]))
    same_shape(response.json(), fixture("response-finished"))


# ====================================================================== the Phase 6 review (P1–P8)


def paged(book, statuses: dict[int, str], *, included=(), excluded=()) -> dict[int, ScanPage]:
    """Scan pages of `book` with these statuses; the manuscript's run includes the pages `included` (as
    they are now: no review drift)."""
    from assembly.services import EMPTY_SIGNATURE

    pages = {
        number: ScanPage.objects.create(
            book=book,
            number=number,
            source_index=number - 1,
            status=status,
            is_excluded=number in excluded,
        )
        for number, status in statuses.items()
    }
    run = Manuscript.objects.get(book=book).run
    run.included = {
        str(pages[n].pk): {"number": n, "reviewed": pages[n].status != "ocr_done", "sig": EMPTY_SIGNATURE}
        for n in included
    }
    run.save()
    return pages


def test_readiness_lists_the_unreviewed_and_the_missing_pages(book):
    """P1: the book's pages still as the models read them, and the pages left out of it."""
    statuses = {n: "reviewed" for n in range(1, 11)} | {3: "ocr_done", 4: "ocr_done", 7: "ocr_done"}
    statuses |= {11: "ocr_done", 12: "layout_done", 13: "error", 14: "ocr_done"}
    paged(book, statuses, included=range(1, 11), excluded=(14,))
    found = rows_of(book)
    codes = [item["code"] for item in readiness.book_readiness(Book.objects.get(pk=book.pk))]
    assert codes[:2] == ["pages_unreviewed", "pages_missing"] and "clear" not in found
    unreviewed = found["pages_unreviewed"][0]
    assert unreviewed == {
        "code": "pages_unreviewed",
        "level": "warn",
        "message": "3 صفحات في الكتاب لم تُراجَع بعد؛ نصّها كما قرأته النماذج: 3، 4، 7.",
        "action": {"label": "المراجعة", "url": f"/books/{book.pk}/review/next/"},
    }
    missing = found["pages_missing"][0]  # 11 left out unreviewed (reviewable now), 12 processing, 13 in error
    assert missing["message"] == "3 صفحات لم تدخل الكتاب بعد: 11، 12، 13."
    assert missing["action"] == {"label": "المراجعة", "url": f"/books/{book.pk}/review/next/"}
    ScanPage.objects.filter(book=book, number=11).update(is_excluded=True)  # only the pipeline's pages left
    missing = rows_of(book)["pages_missing"][0]
    assert missing["message"] == "صفحتان لم تدخلا الكتاب بعد: 12، 13."
    assert missing["action"] == {"label": "المعالجة", "url": f"/books/{book.pk}/"}


def test_the_page_messages_agree_with_their_numbers():
    assert (
        readiness.unreviewed_message([3]) == "صفحة واحدة في الكتاب لم تُراجَع بعد؛ نصّها كما قرأته النماذج: 3."
    )
    assert readiness.unreviewed_message([3, 4]) == (
        "صفحتان في الكتاب لم تُراجَعا بعد؛ نصّهما كما قرأته النماذج: 3، 4."
    )
    assert readiness.unreviewed_message(list(range(3, 80))) == (
        "77 صفحة في الكتاب لم تُراجَع بعد؛ نصّها كما قرأته النماذج: 3، 4، 5، 6، 7، 8، 9، 10…"
    )
    assert readiness.missing_message([91]) == "صفحة واحدة لم تدخل الكتاب بعد: 91."
    assert readiness.missing_message([91, 92]) == "صفحتان لم تدخلا الكتاب بعد: 91، 92."
    assert readiness.missing_message(list(range(80, 91))) == (
        "11 صفحة لم تدخل الكتاب بعد: 80، 81، 82، 83، 84، 85، 86، 87…"
    )
    assert readiness.stray_notes_message(3, "(1)", [5, 12, 30]) == (
        "3 فقرات في أواخر صفحاتها تبدأ بعلامة حاشية مثل «(1)» ولم تُربَط حاشيةً: ص 5، 12، 30."
    )
    assert readiness.stray_notes_message(1, "*", [9]) == (
        "فقرة واحدة في آخر صفحتها تبدأ بعلامة حاشية مثل «*» ولم تُربَط حاشيةً: ص 9."
    )
    assert readiness.stray_notes_message(2, "(1)", [5, 9]) == (
        "فقرتان في أواخر صفحاتهما تبدآن بعلامة حاشية مثل «(1)» ولم تُربَطا حاشيةً: ص 5، 9."
    )


def test_a_book_built_from_raw_ocr_is_never_ready(book):
    """P1 (the review's probe): every page included unreviewed, no uncertain mark left — not «جاهز»."""
    paged(book, {1: "ocr_done", 2: "ocr_done", 3: "ocr_done"}, included=(1, 2, 3))
    found = rows_of(book)
    assert "clear" not in found
    assert found["pages_unreviewed"][0]["message"] == (
        "3 صفحات في الكتاب لم تُراجَع بعد؛ نصّها كما قرأته النماذج: 1، 2، 3."
    )


def test_clear_only_when_every_page_of_the_book_is_reviewed(book):
    paged(book, {1: "reviewed", 2: "assembled", 3: "reviewed"}, included=(1, 2, 3))
    assert rows_of(book) == {"clear": [{"code": "clear", "level": "success", "message": readiness.CLEAR}]}
    assert readiness.CLEAR == "كل الصفحات مُراجَعة ولا ملاحظات؛ الكتاب جاهز للإخراج."
    ScanPage.objects.filter(book=book).delete()
    paged(book, {1: "reviewed", 2: "assembled", 3: "ocr_done"}, included=(1, 2, 3))
    assert set(rows_of(book)) == {"pages_unreviewed"}  # one raw page: no «جاهز»


# ---------------------------------------------------------------------- D75: doubtful text


def gap(page: ScanPage, status: str = "open") -> None:
    from ocr.models import TextGap

    TextGap.objects.create(page=page, text="تعالى بطرابلس ونشأ بها", status=status, support=0.4)


def test_missing_text_lists_the_pages_with_an_open_suggestion(book):
    """D75: open `TextGap` rows on pages in the book; decided ones, excluded pages and pages the run left
    out do not count."""
    pages = paged(book, {n: "reviewed" for n in range(1, 9)}, included=range(1, 8), excluded=(7,))
    for number in (3, 5, 5):
        gap(pages[number])
    gap(pages[2], "inserted")
    gap(pages[4], "dismissed")
    gap(pages[7])  # excluded
    gap(pages[8])  # not in the book (pages_missing speaks for it)
    found = rows_of(book)
    assert found["missing_text"] == [
        {
            "code": "missing_text",
            "level": "warn",
            "message": "نص قد يكون ناقصًا لم يُحسم في صفحتين: 3، 5.",
            "action": {"label": "المراجعة", "url": f"/books/{book.pk}/review/next/"},
        }
    ]
    assert "clear" not in found
    codes = [item["code"] for item in readiness.book_readiness(Book.objects.get(pk=book.pk))]
    assert codes[:2] == ["pages_missing", "missing_text"]  # after the Phase 6 review's rows


def test_single_reader_counts_the_unreviewed_pages_one_model_read(book):
    """D75: `reading.readers` ≠ two on pages still `ocr_done`; a page read before 7b (`{}`) and a reviewed
    page do not count; the row is info, and `clear` never sits beside it."""
    statuses = {1: "reviewed", 2: "ocr_done", 3: "ocr_done", 4: "ocr_done", 5: "reviewed", 6: "ocr_done"}
    pages = paged(book, statuses, included=range(1, 7))
    readings = {2: "one", 3: "tesseract", 4: "two", 5: "one"}
    for number, readers in readings.items():
        ScanPage.objects.filter(pk=pages[number].pk).update(reading={"readers": readers, "partial": False})
    found = rows_of(book)
    assert found["single_reader"] == [
        {
            "code": "single_reader",
            "level": "info",
            "message": "قُرئت صفحتان من الكتاب بنموذج واحد ولم تُراجَعا بعد.",
            "action": {"label": "المراجعة", "url": f"/books/{book.pk}/review/next/"},
        }
    ]
    ScanPage.objects.filter(book=book, status="ocr_done").update(status="reviewed")
    ScanPage.objects.filter(pk=pages[2].pk).update(status="ocr_done")
    assert rows_of(book) == {
        "pages_unreviewed": [
            {
                "code": "pages_unreviewed",
                "level": "warn",
                "message": "صفحة واحدة في الكتاب لم تُراجَع بعد؛ نصّها كما قرأته النماذج: 2.",
                "action": {"label": "المراجعة", "url": f"/books/{book.pk}/review/next/"},
            }
        ],
        "single_reader": [
            {
                "code": "single_reader",
                "level": "info",
                "message": "قُرئت صفحة واحدة من الكتاب بنموذج واحد ولم تُراجَع بعد.",
                "action": {"label": "المراجعة", "url": f"/books/{book.pk}/review/next/"},
            }
        ],
    }


def test_the_doubtful_text_messages_agree_with_their_numbers():
    assert readiness.missing_text_message([3]) == "نص قد يكون ناقصًا لم يُحسم في صفحة واحدة: 3."
    assert (
        readiness.missing_text_message([3, 7, 12, 30]) == "نص قد يكون ناقصًا لم يُحسم في 4 صفحات: 3، 7، 12، 30."
    )
    assert readiness.missing_text_message(list(range(1, 14))) == (
        "نص قد يكون ناقصًا لم يُحسم في 13 صفحة: 1، 2، 3، 4، 5، 6، 7، 8…"
    )
    assert readiness.single_reader_message(13) == "قُرئت 13 صفحة من الكتاب بنموذج واحد ولم تُراجَع بعد."
    assert readiness.single_reader_message(4) == "قُرئت 4 صفحات من الكتاب بنموذج واحد ولم تُراجَع بعد."
    assert readiness.single_reader_message(1) == "قُرئت صفحة واحدة من الكتاب بنموذج واحد ولم تُراجَع بعد."


def test_doubtful_text_without_a_run_counts_every_page_of_the_book(book):
    Manuscript.objects.filter(book=book).update(run=None)
    page = ScanPage.objects.create(
        book=book, number=1, source_index=0, status="ocr_done", reading={"readers": "one"}
    )
    gap(page)
    trust = readiness.trust_pages(Book.objects.get(pk=book.pk), Manuscript.objects.get(book=book))
    assert trust == readiness.TrustPages(missing_text=[1], single_reader=[1])


def notes_document() -> dict:
    """Pages 5–12: page-end note paragraphs (flagged: 5, 9), and what must not be flagged."""
    words = " ".join(["كلمة"] * 90)
    return document(
        heading("h1", "الفصل الأول", pages=(5,)),
        para("p1", "نص الصفحة الخامسة وهو طويل بما يكفي.", pages=(5,)),
        para("n1", "(١) قال مصححه: في الأصل «بياض».", pages=(5,)),  # page end, Arabic-Indic digit: flagged
        para("p2", "نص الصفحة السادسة يسبق قائمة.", pages=(6,)),
        para("l1", "(1) أولًا ما قيل في الباب.", pages=(6,)),  # a list in the middle of the page
        para("l2", "(2) ثانيًا ما قيل بعده.", pages=(6,)),
        para("p3", "ويكمل النص بعد القائمة.", pages=(6,)),
        para("p4", "نص الصفحة السابعة.", pages=(7,)),
        para("n2", "(22) رقم أكبر من أرقام الحواشي.", pages=(7,)),  # n > 15
        para("l3", "(1) فقرة أولى في صفحة كلها مرقّمة.", pages=(8,)),  # the whole page is the run
        para("l4", "(2) فقرة ثانية في الصفحة نفسها.", pages=(8,)),
        para("p5", "نص الصفحة التاسعة.", pages=(9,)),
        para("n3", "* تنبيه من الناشر على هذه الصفحة.", pages=(9,)),  # «*»: flagged
        para("p6", "نص الصفحة العاشرة.", pages=(10,)),
        para("n4", f"(3) {words}", pages=(10,)),  # more than 80 words
        para("p7", "نص الصفحة الحادية عشرة.", pages=(11,)),
        para("s1", "* * *", pages=(11,)),  # a separator typed as text
        para("p8", "نص الصفحة الثانية عشرة.", pages=(12,)),
        para("n5", "[2] حاشية تمتد إلى الصفحة التالية.", pages=(12, 13)),  # two source pages
        para("q1", "(4) اقتباس في آخر الصفحة.", pages=(13,), style="quote"),  # not a body paragraph
    )


def test_stray_notes_are_page_end_paragraphs_that_start_with_a_note_marker(book):
    Manuscript.objects.filter(book=book).update(document=notes_document())
    found = readiness.stray_notes(notes_document())
    assert [(note.block, note.page, note.marker) for note in found] == [("n1", 5, "(1)"), ("n3", 9, "*")]
    row = rows_of(book)["stray_notes"][0]
    assert row["level"] == "warn" and row["message"] == (
        "فقرتان في أواخر صفحاتهما تبدآن بعلامة حاشية مثل «(1)» ولم تُربَطا حاشيةً: ص 5، 9."
    )
    # the book page opens on the first one's block (`?block=`, 7c): its chapter, its page, lit
    assert row["action"] == {"label": "عرض", "url": f"/books/{book.pk}/layout/?block=n1"}


def test_note_markers():
    marker = readiness.note_marker
    assert (
        marker("(١) قال مصححه") == "(1)" and marker(" [3] حاشية") == "[3]" and marker("( 15 ) نص") == "(15)"
    )
    assert marker("* تنبيه") == "*" and marker("\u200f(2) نص") == "(2)"
    for plain in ("(22) نص", "(0) نص", "(1)", "* * *", "نص (1)", "[2) نص", "(٥)"):
        assert marker(plain) is None, plain


def test_a_running_export_is_judged_by_its_heartbeat_not_its_creation(book, editor, fake, monkeypatch):
    """P2: an export that waited 31 minutes in the queue and runs now is not abandoned (it was, from
    `created_at`), and the format stays busy."""
    monkeypatch.setattr(exports, "_enqueue", lambda row: None)
    row = exports.request_export(book, "docx", {}, editor)
    now = timezone.now()
    Export.objects.filter(pk=row.pk).update(
        created_at=now - timedelta(minutes=36),
        started_at=now - timedelta(minutes=5),
        status="running",
        task_id="t-1",
        progress={"step": "layout", "done": 3, "total": 4},
        updated_at=now - timedelta(seconds=1),
    )
    row.refresh_from_db()
    assert not exports.abandoned(row, now)
    assert exports.export_payload(row, editor)["status"] == "running"
    with pytest.raises(exports.ExportConflict):
        exports.request_export(book, "docx", {}, editor)
    # still reporting, but past the soft limit plus 5 minutes since it started: the worker's limit has hit it
    Export.objects.filter(pk=row.pk).update(started_at=now - timedelta(minutes=36))
    row.refresh_from_db()
    assert exports.abandoned(row, now)


def test_a_dead_worker_frees_the_format_after_ten_silent_minutes(book, editor, fake, monkeypatch):
    """P2: a worker killed right after the claim leaves a running row that never moves again."""
    monkeypatch.setattr(exports, "_enqueue", lambda row: None)
    row = exports.request_export(book, "docx", {}, editor)
    now = timezone.now()
    Export.objects.filter(pk=row.pk).update(
        started_at=now - timedelta(minutes=9),
        status="running",
        task_id="t-dead",
        updated_at=now - timedelta(minutes=9),
    )
    row.refresh_from_db()
    assert not exports.abandoned(row, now)  # nine silent minutes: still running
    Export.objects.filter(pk=row.pk).update(
        started_at=now - timedelta(minutes=11), updated_at=now - timedelta(minutes=11)
    )
    row.refresh_from_db()
    payload = exports.export_payload(row, editor)
    assert payload["status"] == "error" and payload["error"] == exports.ABANDONED
    second = exports.request_export(book, "docx", {}, editor)  # accepted: the dead one is closed
    row.refresh_from_db()
    assert row.status == "error" and row.error == exports.ABANDONED and second.status == "queued"


def test_the_reporter_beats_while_a_long_step_runs(book, editor, fake, monkeypatch):
    """P2: `cancelled()` moves `updated_at` once `heartbeat_s` passed without a report."""
    row = queued(book, monkeypatch, editor)
    before = timezone.now() - timedelta(minutes=5)
    Export.objects.filter(pk=row.pk).update(status="running", updated_at=before)
    reporter = exports.Reporter(row.pk, heartbeat_s=0.0)
    assert not reporter.cancelled()
    row.refresh_from_db()
    assert row.updated_at > before + timedelta(minutes=4)
    Export.objects.filter(pk=row.pk).update(status="cancelled")
    assert reporter.cancelled()  # the beat touched no running row: cancelled


def test_a_file_that_cannot_be_deleted_keeps_its_row_for_the_next_prune(book, monkeypatch):
    """P3: retention never leaves a file without its row."""
    rows = [done_row(book, data=f"file {i}".encode()) for i in range(6)]
    oldest = rows[0]
    path = oldest.file.name
    real_delete = default_storage.delete

    def refuse(name):
        if name == path:
            raise PermissionError(13, "Permission denied", name)
        return real_delete(name)

    monkeypatch.setattr(default_storage, "delete", refuse)
    assert exports.prune(book.pk, "docx") == 0
    assert Export.objects.filter(pk=oldest.pk).exists() and default_storage.exists(path)
    monkeypatch.setattr(default_storage, "delete", real_delete)
    assert exports.prune(book.pk, "docx") == 1  # the next prune tries again
    assert not Export.objects.filter(pk=oldest.pk).exists() and not default_storage.exists(path)


def test_an_interrupted_finish_takes_its_file_with_it(book, editor, fake, monkeypatch):
    """P3: a timeout right after the file is written (before the row records it) leaves no file."""
    real_save = default_storage.save
    saved = []

    def save_then_time_out(name, content, **kwargs):
        saved.append(real_save(name, content, **kwargs))
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(exports.default_storage, "save", save_then_time_out)
    row = exports.request_export(book, "docx", {}, editor)
    assert row.status == "error" and row.error.startswith(exports.TIMEOUT) and not row.file
    assert saved and not default_storage.exists(saved[0])


def test_the_sweep_removes_only_the_files_no_row_records(book, editor, fake, monkeypatch):
    """P3: a file left by a worker killed between the write and the update goes; a running export's file
    and the recorded ones stay."""
    kept = done_row(book)
    running = queued(book, monkeypatch, editor)
    Export.objects.filter(pk=running.pk).update(status="running")
    folder = f"books/{book.pk}/exports"
    orphan = default_storage.save(f"{folder}/9999.docx", ContentFile(b"lost"))
    writing = default_storage.save(f"{folder}/{running.pk}.docx", ContentFile(b"being written"))
    assert exports.sweep(book.pk) == 1
    assert not default_storage.exists(orphan)
    assert default_storage.exists(writing) and default_storage.exists(kept.file.name)
    Export.objects.filter(pk=running.pk).update(status="error")  # it failed: its file is no one's now
    exports.prune(book.pk, "docx")  # every prune sweeps
    assert not default_storage.exists(writing) and default_storage.exists(kept.file.name)
    assert exports.sweep(Book.objects.create(title="بلا ملفات").pk) == 0  # no folder: nothing to do


def test_the_file_is_named_after_the_title_it_prints(book, editor, fake, monkeypatch):
    """P4: «بيانات الكتاب» first, then the manuscript's title node, then the book's own title — the order
    of the title page and the file's properties."""
    from publishing.model import book_model, page_setup
    from publishing.preview import stylesheet_for

    Book.objects.filter(pk=book.pk).update(title="scan-0042")
    StyleSheet.objects.create(book=book, front_matter={"fields": {"title": "الأمالي", "author": "القالي"}})
    fresh = Book.objects.get(pk=book.pk)
    monkeypatch.setattr(exports, "_enqueue", lambda row: None)
    queued_row = exports.request_export(fresh, "docx", {"comments": True}, editor)
    assert queued_row.filename == "الأمالي - مع التعليقات.docx"  # named so from the start
    done = exports.run_export(queued_row.pk, "task-1")
    assert done.status == "done" and done.filename == "الأمالي - مع التعليقات.docx"
    response = logged(editor).get(reverse("publishing:export_download", args=[book.pk, done.pk]))
    assert f"filename*=utf-8''{quote(done.filename)}" in response["Content-Disposition"]
    # the same order as the book model, whatever is given
    source = Manuscript.objects.get(book=book).document
    untitled = document(para("p1", "نص"), title="")
    for sheet, doc, fallback in (
        ({"front_matter": {"fields": {"title": "الأمالي"}}}, source, "scan-0042"),
        ({}, source, "scan-0042"),
        ({}, untitled, "scan-0042"),
    ):
        setup = page_setup(sheet)
        printed = book_model(doc, setup, title=fallback).front.title
        assert exports.printed_title(doc, setup, fallback) == printed
    assert exports.printed_title(untitled, page_setup(stylesheet_for(fresh)), "scan-0042") == "الأمالي"


def test_export_book_names_the_file_after_the_printed_title(book, fake, tmp_path):
    StyleSheet.objects.create(book=book, front_matter={"fields": {"title": "الأمالي"}})
    call_command("export_book", str(book.pk), "--format", "docx", "--out", str(tmp_path))
    assert (tmp_path / "الأمالي.docx").read_bytes() == fake.data


def test_the_book_lock_does_not_conflict_with_a_foreign_key_check(book, editor, fake, monkeypatch):
    """P5: FOR NO KEY UPDATE (a deferred foreign-key check takes FOR KEY SHARE on the book at commit:
    with FOR UPDATE, the IntegrityError fallback deadlocks on PostgreSQL). SQLite takes no row lock."""
    from django.db.models import QuerySet

    asked: list[dict] = []
    original = QuerySet.select_for_update

    def record(self, *args, **kwargs):
        if self.model is Book:
            asked.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", record)
    queued(book, monkeypatch, editor)
    assert asked == [{"no_key": True}]


def test_the_long_poll_reads_the_status_only_and_the_row_once(book, editor, fake, monkeypatch):
    """P6: while waiting, only the status and the times are read (every 0.25 s); the row once, at the
    end, when it moved."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    row = queued(book, monkeypatch, editor)
    monkeypatch.setattr(exports, "MAX_WAIT_S", 1.0)
    assert exports.WAIT_STEP_S >= 0.25
    with CaptureQueriesContext(connection) as ctx:
        same = exports.wait_for(row, 5, row.updated_at)
    assert same is row and 3 <= len(ctx.captured_queries) <= 5
    assert all('"inputs"' not in query["sql"] for query in ctx.captured_queries)  # never the whole row
    Export.objects.filter(pk=row.pk).update(
        status="running", progress={"step": "write"}, updated_at=timezone.now()
    )
    with CaptureQueriesContext(connection) as ctx:
        moved = exports.wait_for(row, 5, row.updated_at)
    assert moved.status == "running" and moved.progress == {"step": "write"}
    assert len(ctx.captured_queries) == 2 and '"inputs"' in ctx.captured_queries[-1]["sql"]


def _real_registry(monkeypatch):
    monkeypatch.setattr(exporters, "_registry", {})
    monkeypatch.setattr(exporters, "_broken", set())


def test_the_real_exporters_answer_the_shape_of_the_fixtures(book, editor, monkeypatch):
    """P7: the forms and notes of the real Word, PDF and EPUB exporters (not the fake) have the §3.2
    fixtures' shape, and so have their finished rows."""
    _real_registry(monkeypatch)
    assert exporters.available_formats() == ["docx", "print_pdf", "screen_pdf", "epub"]
    model = fixture("page")
    payload = exports.page_payload(Book.objects.get(pk=book.pk), editor)
    same_shape(payload, model)
    for real, want in zip(payload["formats"], model["formats"], strict=True):
        assert real["key"] == want["key"] and real["available"] is True
        same_shape(real["form"], want["form"], f"formats[{real['key']}].form")
        assert set(real["form"]) == set(want["form"]), real["key"]  # every field, none missing
    assert (
        payload["formats"][0]["form"]["kashida"]["label"] == model["formats"][0]["form"]["kashida"]["label"]
    )
    for format in ("docx", "epub"):  # the quick ones: a real file each
        row = exports.request_export(book, format, {}, editor)
        assert row.status == "done", row.error
        want = fixture("row-done")  # a Word row: its options are Word's (an EPUB has none)
        want["options"] = want["options"] if format == "docx" else {}
        same_shape(exports.export_payload(row, editor), want)


def test_a_number_that_is_not_finite_is_a_clean_400(book, editor, monkeypatch):
    """P8: «inf» / «nan» for a number option is refused with the Arabic message (was an OverflowError:
    500)."""
    specs = (OptionSpec("count", "int", 2, bounds=(1, 9)), OptionSpec("size", "float", 1.0))
    for value in ("inf", "-inf", "nan", float("inf"), float("nan"), "1e400"):
        with pytest.raises(OptionsError) as caught:
            parse_options(specs, {"count": value})
        assert caught.value.errors == {"count": "يُنتظر رقم بين 1 و9."}
        with pytest.raises(OptionsError) as caught:
            parse_options(specs, {"size": value})
        assert caught.value.errors == {"size": exporters.BAD_NUMBER_ANY}
    assert parse_options(specs, {"size": "2.5"})["size"] == 2.5  # an unbounded number: any finite one
    monkeypatch.setattr(exporters, "_registry", {})
    monkeypatch.setattr(exporters, "_load", lambda format: None)
    exporters.register(FakeExporter("docx", options=specs))
    response = post(
        logged(editor),
        reverse("api:exports", args=[book.pk]),
        {"format": "docx", "options": {"count": "inf"}},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": exports.BAD_OPTIONS, "errors": {"count": "يُنتظر رقم بين 1 و9."}}


def test_the_page_rows_come_after_the_uncertain_words(book, monkeypatch):
    """P1: the three new warnings sit after `uncertain_words`, before the review drift."""
    monkeypatch.setattr(
        readiness, "uncertain_counts", lambda book, document=None: readiness.UncertainCounts(2, 0)
    )
    monkeypatch.setattr(
        "editor.services.review_drift",
        lambda book, manuscript=None: {"edited": True, "pages": [9], "chapters": {}},
    )
    Manuscript.objects.filter(book=book).update(document=notes_document())
    paged(book, {5: "ocr_done", 6: "reviewed", 7: "error"}, included=(5, 6))
    codes = [item["code"] for item in readiness.book_readiness(Book.objects.get(pk=book.pk))]
    assert codes[:5] == [
        "uncertain_words",
        "pages_unreviewed",
        "pages_missing",
        "stray_notes",
        "review_drift",
    ]
