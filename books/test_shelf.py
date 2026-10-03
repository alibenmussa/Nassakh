"""The books home (books/shelf.py, templates/books/list.html and its _shelf_* partials, library.js): the
shelf's steps are the stage bar's, in a constant number of queries; the book to resume; the filters; the
covers; the view and sort cookie; the page's markup and permissions; the page's search and sort under Node."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

import pytest

from accounts.testing import member
from books import services, shelf
from books.models import Book, Page

pytestmark = pytest.mark.django_db

JS = Path(__file__).resolve().parent.parent / "static" / "src" / "js"
PS = Page.Status
STEP_KEYS = ("key", "label", "url", "state", "detail", "hint", "count")


def _user(name: str, group: str) -> User:
    user = User.objects.create_user(name, password="pass-1234")
    user.groups.add(Group.objects.get_or_create(name=group)[0])
    return member(user)  # the books' organisation (D102)


@pytest.fixture
def editor_client(client):
    client.force_login(_user("editor", "editor"))
    return client


def _book(
    title: str, statuses=(), status: str = Book.Status.READY_FOR_REVIEW, **kwargs
) -> tuple[Book, list[Page]]:
    book = Book.objects.create(title=title, status=status, **kwargs)
    pages = []
    for number, page_status in enumerate(statuses, start=1):
        extra = {}
        if isinstance(page_status, tuple):  # (status, error_from)
            page_status, extra = page_status[0], {"error_from": page_status[1]}
        pages.append(
            Page.objects.create(
                book=book, number=number, source_index=number - 1, status=page_status, **extra
            )
        )
    return book, pages


def _assembled(
    book: Book, pages: list[Page], *, version: int = 2, exported: bool = True, laid_out: bool = True
):
    """A manuscript built from every page as it is now (so nothing is stale), its stylesheet, a laid-out book
    and a Word file of this version."""
    from assembly.models import AssemblyRun
    from assembly.services import page_signatures
    from editor.models import Manuscript, StyleSheet
    from publishing.models import Export, LiveLayout

    signatures = page_signatures([page.pk for page in pages])
    included = {
        str(page.pk): {
            "number": page.number,
            "reviewed": page.status in (PS.REVIEWED, PS.ASSEMBLED),
            "sig": sig,
        }
        for page, sig in ((page, signatures[page.pk]) for page in pages)
    }
    now = timezone.now()
    run = AssemblyRun.objects.create(
        book=book, status=AssemblyRun.Status.DONE, included=included, stats={"chapters": 3}, finished_at=now
    )
    Manuscript.objects.create(book=book, version=version, run=run, document={"type": "doc", "content": []})
    StyleSheet.objects.get_or_create(book=book)
    if laid_out:
        LiveLayout.objects.create(book=book, revision=1, page_count=120)
    if exported:
        Export.objects.create(
            book=book, format="docx", status=Export.Status.DONE, manuscript_version=version, finished_at=now
        )


def _steps(steps: list[dict]) -> list[tuple]:
    return [tuple(step[key] for key in STEP_KEYS) for step in steps]


def _shelf_states() -> dict[str, Book]:
    """One book per state the shelf must read as the stage bar does."""
    books = {
        "extracting": _book("يستخرج", status=Book.Status.PROCESSING)[0],
        "preparing": _book("يخطط", [PS.UPLOADED, PS.PREPROCESSED], status=Book.Status.PROCESSING)[0],
        "awaiting": _book(
            "ينتظر", [PS.PREPROCESSED, PS.LAYOUT_DONE], status=Book.Status.NEEDS_GUIDES, awaits_ocr_start=True
        )[0],
        "layout_error": _book("تعذر تجهيزها", [PS.OCR_DONE, (PS.ERROR, "preprocess")])[0],
        "reading": _book("يقرأ", [PS.OCR_DONE, PS.LAYOUT_DONE, PS.LAYOUT_DONE], status=Book.Status.OCR)[0],
        "ocr_error": _book("تعذرت قراءتها", [PS.OCR_DONE, (PS.ERROR, "ocr_full")])[0],
        "to_review": _book("للمراجعة", [PS.OCR_DONE, PS.OCR_DONE, PS.REVIEWED], status=Book.Status.REVIEWING)[
            0
        ],
    }
    done, pages = _book("مكتمل", [PS.REVIEWED, PS.REVIEWED, PS.ASSEMBLED], status=Book.Status.REVIEWING)
    _assembled(done, pages)
    books["complete"] = done
    unlaid, pages = _book("مجموع", [PS.REVIEWED, PS.REVIEWED], status=Book.Status.REVIEWING)
    _assembled(unlaid, pages, exported=False, laid_out=False)
    books["assembled"] = unlaid
    return books


# ---------------------------------------------------------------- the service


def test_the_shelf_reads_every_step_as_the_stage_bar_does():
    books = _shelf_states()
    rows = {row["book"].pk: row for row in shelf.books_shelf()["rows"]}
    for name, book in books.items():
        assert _steps(rows[book.pk]["steps"]) == _steps(services.book_stages(book)), name
    # and with one book (no book to resume, so nothing but the shelf's own queries)
    Book.objects.exclude(pk=books["reading"].pk).delete()
    (row,) = shelf.books_shelf()["rows"]
    assert _steps(row["steps"]) == _steps(services.book_stages(books["reading"]))


def test_the_step_a_book_is_at_its_progress_and_its_continue_link():
    books = _shelf_states()
    rows = {row["book"].pk: row for row in shelf.books_shelf()["rows"]}
    at = {name: rows[book.pk]["stage_key"] for name, book in books.items()}
    assert at == {
        "extracting": "pages",
        "preparing": "pages",
        "awaiting": "ocr",
        "layout_error": "pages",
        "reading": "ocr",
        "ocr_error": "ocr",
        "to_review": "review",
        "complete": "complete",
        "assembled": "book",
    }
    reading = rows[books["reading"].pk]
    assert reading["next_label"] == "المعالجة" and reading["next_text"] == "1/3"
    assert [step["percent"] for step in reading["steps"]][:3] == [100, 33, 0]
    assert reading["next_url"] == reverse("books:detail", args=[books["reading"].pk])
    to_review = rows[books["to_review"].pk]
    assert to_review["next_url"] == reverse("review:next", args=[books["to_review"].pk])
    assert to_review["pending_page"] == 1 and to_review["rank"] == 233
    awaiting = rows[books["awaiting"].pk]
    assert awaiting["next_text"] == "بانتظار «بدء المعالجة»"
    complete = rows[books["complete"].pk]
    assert complete["current"] is None and complete["next_label"] == "مكتمل" and complete["rank"] == 600
    assert complete["next_url"] == reverse("publishing:export", args=[books["complete"].pk])
    assert rows[books["layout_error"].pk]["attention"] and rows[books["ocr_error"].pk]["attention"]
    assert not rows[books["to_review"].pk]["attention"]


def test_the_shelf_costs_the_same_queries_for_two_books_or_twenty():
    _shelf_states()

    def count() -> int:
        with CaptureQueriesContext(connection) as ctx:
            shelf.books_shelf()
        return len(ctx.captured_queries)

    with patch.object(shelf, "_resume", lambda rows: None):  # the shelf's own queries
        few = count()
        for i in range(4):
            book, pages = _book(f"كتاب {i}", [PS.REVIEWED, PS.OCR_DONE, (PS.ERROR, "ocr_full")])
            if i % 2:
                _assembled(book, pages)
        assert count() == few <= 10
    assert count() <= few + 9  # and the book to resume's exact stage bar (`StageFacts`) and its page


def test_the_book_to_resume_is_the_newest_activity_with_a_step_left():
    books = _shelf_states()
    Book.objects.filter(pk=books["complete"].pk).update(updated_at=timezone.now() + timedelta(hours=2))
    Page.objects.filter(book=books["to_review"]).update(reviewed_at=timezone.now() + timedelta(hours=1))
    data = shelf.books_shelf()
    assert data["rows"][0]["book"].pk == books["complete"].pk  # newest activity first
    assert data["rows"][1]["book"].pk == books["to_review"].pk  # a review counts as activity
    resume = data["resume"]
    assert resume["book"].pk == books["to_review"].pk  # the complete book has nothing left
    assert resume["action"] == "متابعة المراجعة" and resume["current"]["hint"] == "الصفحة التالية للمراجعة: 1"
    assert resume["next_url"] == reverse("review:next", args=[books["to_review"].pk])
    # one book: the shelf shows it already
    Book.objects.exclude(pk=books["to_review"].pk).delete()
    assert shelf.books_shelf()["resume"] is None


def test_the_filters_count_the_books_at_each_step():
    _shelf_states()
    data = shelf.books_shelf()
    chips = {chip["key"]: (chip["label"], chip["count"]) for chip in data["filters"]}
    assert list(chips) == ["all", "pages", "ocr", "review", "book", "complete", "attention"]
    assert (
        chips["all"] == ("الكل", 9) and chips["pages"] == ("التخطيط", 3) and chips["ocr"] == ("المعالجة", 3)
    )
    assert chips["complete"] == ("مكتملة", 1) and chips["attention"] == ("تحتاج انتباهًا", 2)
    assert data["summary"]["books_label"] == "9 كتب" and data["summary"]["pages_label"] == "19 صفحة"


def test_the_sorts_and_the_cookie():
    books = _shelf_states()
    titles = [row["book"].title for row in shelf.books_shelf("title")["rows"]]
    assert titles == sorted(titles)
    added = [row["book"].pk for row in shelf.books_shelf("added")["rows"]]
    assert added == sorted(added, reverse=True)
    progress = [row["rank"] for row in shelf.books_shelf("progress")["rows"]]
    assert progress == sorted(progress, reverse=True) and progress[0] == 600
    assert shelf.books_shelf("nonsense")["rows"][0]["book"].pk == shelf.books_shelf()["rows"][0]["book"].pk
    assert books
    assert shelf.prefs("list:title") == ("list", "title")
    assert shelf.prefs("list") == ("list", "activity") and shelf.prefs("tiles:evil") == ("grid", "activity")
    assert shelf.prefs(None) == ("grid", "activity")


def test_a_cover_is_the_rendered_one_or_typographic_in_the_books_colours():
    from editor.models import StyleSheet

    plain, _ = _book("بلا تنسيق")
    styled, _ = _book("بتنسيق")
    sheet = StyleSheet.objects.create(book=styled)
    sheet.front_matter = {
        **sheet.front_matter,
        "cover": {"mode": "info", "background": "#1d2433", "color": "#f3efe6"},
    }
    sheet.save()
    folder = Path(settings.MEDIA_ROOT) / "books" / str(styled.pk) / "cover" / "abc123"
    folder.mkdir(parents=True)
    (folder / "cover.webp").write_bytes(b"webp")
    rows = {row["book"].pk: row for row in shelf.books_shelf()["rows"]}
    assert rows[plain.pk]["cover"]["url"] is None
    assert rows[plain.pk]["cover"]["cloth"] in {"#f4efe4", "#1d2433", "#1f3b2d", "#4a1d24", "#e9e9ec"}
    assert rows[styled.pk]["cover"] == {
        "url": None,
        "cloth": "#1d2433",
        "ink": "#f3efe6",
    }  # no PDF: unfinished
    (folder / "cover.pdf").write_bytes(b"%PDF")
    rows = {row["book"].pk: row for row in shelf.books_shelf()["rows"]}
    assert rows[styled.pk]["cover"]["url"] == f"{settings.MEDIA_URL}books/{styled.pk}/cover/abc123/cover.webp"
    sheet.front_matter = {**sheet.front_matter, "cover": {"mode": "none"}}
    sheet.save()
    rows = {row["book"].pk: row for row in shelf.books_shelf()["rows"]}
    assert rows[styled.pk]["cover"]["url"] is None  # no cover chosen: the render left on disk is not shown


def test_a_placeholder_author_is_left_out():
    _book("أ", author="-", original_year=1950)
    _book("ب", author="الزاوي", original_year=1970)
    bylines = {row["book"].title: row["byline"] for row in shelf.books_shelf()["rows"]}
    assert bylines == {"أ": "1950", "ب": "الزاوي · 1970"}


# ---------------------------------------------------------------- the page


def test_the_books_home_shows_the_book_to_resume_and_the_shelf(editor_client):
    books = _shelf_states()
    body = editor_client.get(reverse("books:list")).content.decode()
    assert 'class="lb-resume"' in body and "تابع من حيث توقفت" in body
    assert body.count("btn btn-primary") == 1  # «متابعة …» is the primary; «كتاب جديد» steps back
    assert reverse("books:create") in body
    assert body.count('<li class="lb-card') == 9 and body.count('class="lb-seg ') == 9 * 6 + 6
    for book in books.values():
        assert reverse("books:detail", args=[book.pk]) in body
    # «…» reaches every step that is open; a blocked step is no link
    assert reverse("assembly:manuscript", args=[books["complete"].pk]) in body
    assert reverse("publishing:export", args=[books["complete"].pk]) in body
    assert 'aria-disabled="true"' in body
    # filters, search, sort, views; the view and sort of the cookie drawn at once
    assert 'data-stage="review"' in body and 'aria-label="عرض الكتب حسب مرحلتها"' in body
    assert (
        'x-model="q"' in body
        and 'id="lb-sort"' in body
        and "bookShelf({ view: 'grid', sort: 'activity' })" in body
    )
    assert 'class="lb-shelf"' in body and "library.js" in body
    editor_client.cookies[shelf.PREFS_COOKIE] = "list:title"
    body = editor_client.get(reverse("books:list")).content.decode()
    assert 'class="lb-shelf is-list"' in body and '<option value="title" selected>' in body


def test_a_running_step_sweeps_and_a_waiting_book_says_what_it_waits_for(editor_client):
    _book("يقرأ", [PS.OCR_DONE, PS.LAYOUT_DONE], status=Book.Status.OCR)
    _book("ينتظر", [PS.PREPROCESSED], status=Book.Status.NEEDS_GUIDES, awaits_ocr_start=True)
    body = editor_client.get(reverse("books:list")).content.decode()
    assert "lb-seg is-active is-at is-running" in body
    assert "بانتظار «بدء المعالجة»" in body and "dot-warning" in body and "تم التخطيط" in body


def test_a_proofreader_sees_the_shelf_without_the_new_book_button(client):
    _book("أ", [PS.OCR_DONE])
    _book("ب", [PS.REVIEWED])
    client.force_login(_user("reader", "proofreader"))
    body = client.get(reverse("books:list")).content.decode()
    assert 'class="lb-shelf"' in body and reverse("books:create") not in body


def test_the_empty_home_explains_the_six_steps(editor_client, client):
    body = editor_client.get(reverse("books:list")).content.decode()
    assert "لا كتب بعد" in body and "بدء المعالجة" in body and body.count("btn btn-primary") == 1
    for label in services.STAGE_NAMES.values():
        assert f"<b>{label}</b>" in body
    assert "library.js" in body  # harmless: no shelf, no component
    client.force_login(_user("reader", "proofreader"))
    body = client.get(reverse("books:list")).content.decode()
    assert reverse("books:create") not in body and "يضيف الكتبَ المحرّرون والمديرون" in body


# ---------------------------------------------------------------- library.js under Node

HARNESS = r"""
const inits = []; const reg = {};
globalThis.window = globalThis;
globalThis.document = { addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); } };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; } };
const fs = require('fs');
for (const f of process.argv.slice(2)) eval(fs.readFileSync(f, 'utf8'));
inits.forEach((fn) => fn());
const L = NassakhLibrary;
const items = [
  { key: L.searchKey('تاريخ ليبيا العام محمد بن مسعود'), title: 'تاريخ ليبيا العام', stage: 'review', attention: false, activity: 30, created: 1, rank: 209 },
  { key: L.searchKey('إيصال السالك الولاتي'), title: 'إيصال السالك', stage: 'review', attention: true, activity: 10, created: 3, rank: 200 },
  { key: L.searchKey('المدخل إلى علم السيرة'), title: 'المدخل إلى علم السيرة', stage: 'ocr', attention: false, activity: 20, created: 2, rank: 140 },
];
const pick = (s) => { const { order, visible } = L.arrange(items, s); return order.filter((i) => visible.has(i)).map((i) => i.title); };
const shelf = reg.bookShelf({ view: 'list', sort: 'title' });
const fallback = reg.bookShelf({ view: 'wall', sort: 'evil' });
console.log(JSON.stringify({
  key: L.searchKey('إيصالُ السّالك ـ ٣٤ أدبى ومؤلَّفة'),
  hamza: L.matches(L.searchKey('إيصال السالك'), 'ايصال'),
  words: L.matches(items[0].key, 'مسعود تاريخ'),
  activity: pick({ q: '', stage: 'all', sort: 'activity' }),
  title: pick({ q: '', stage: 'all', sort: 'title' }),
  added: pick({ q: '', stage: 'all', sort: 'added' }),
  progress: pick({ q: '', stage: 'all', sort: 'progress' }),
  review: pick({ q: '', stage: 'review', sort: 'activity' }),
  attention: pick({ q: '', stage: 'attention', sort: 'activity' }),
  search: pick({ q: 'السالك', stage: 'all', sort: 'activity' }),
  none: pick({ q: 'غير موجود', stage: 'all', sort: 'activity' }),
  init: [shelf.view, shelf.sort, fallback.view, fallback.sort],
}));
"""  # noqa: E501


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_search_filters_and_sorts_under_node(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    run = subprocess.run(
        ["node", str(harness), str(JS / "keys.js"), str(JS / "library.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["key"] == "ايصال السالك 34 ادبي ومولفه"  # marks, tatweel, hamza seats, ى, ة, digits
    assert out["hamza"] and out["words"]
    assert out["activity"] == ["تاريخ ليبيا العام", "المدخل إلى علم السيرة", "إيصال السالك"]
    # the Arabic collation: إ is an alef, so «المدخل» (ا ل) comes before «إيصال» (ا ي)
    assert out["title"] == ["المدخل إلى علم السيرة", "إيصال السالك", "تاريخ ليبيا العام"]
    assert out["added"] == ["إيصال السالك", "المدخل إلى علم السيرة", "تاريخ ليبيا العام"]
    assert out["progress"] == ["تاريخ ليبيا العام", "إيصال السالك", "المدخل إلى علم السيرة"]
    assert out["review"] == ["تاريخ ليبيا العام", "إيصال السالك"]
    assert out["attention"] == ["إيصال السالك"] and out["search"] == ["إيصال السالك"] and out["none"] == []
    assert out["init"] == ["list", "title", "grid", "activity"]
