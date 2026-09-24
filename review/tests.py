"""Tests of the review app: services (resolve, edit, insert, delete, undo, approve, reopen), the
read models (payload, filmstrip, summary, next page), the JSON API and the two views."""

from __future__ import annotations

import json
import re

from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse

import numpy as np
import pytest

from books.models import Book, Page
from core.storage import save_array
from ocr.models import Line
from processing.models import Preprocess, Region
from review import services
from review.models import LineRevision

W, H = 100, 200


# ---------------------------------------------------------------- fixtures and helpers


def tok(t, conf="high", alt=None, tess=None, bbox=None, digit=False, **extra) -> dict:
    return {"t": t, "alt": alt, "tess": tess, "conf": conf, "digit": digit, "bbox": bbox, **extra}


def make_line(page, order, region, tokens) -> Line:
    text = " ".join(t["t"] for t in tokens)
    return Line.objects.create(
        page=page,
        order=order,
        region=region,
        bbox=[0, order * 20, W, order * 20 + 20],
        text=text,
        ocr_text=text,
        tokens=tokens,
        n_low=sum(1 for t in tokens if t["conf"] == "low"),
    )


def make_page(book, number, status=Page.Status.OCR_DONE, with_lines=True) -> Page:
    page = Page.objects.create(
        book=book,
        number=number,
        source_index=number - 1,
        status=status,
        text_state=Page.TextState.FINAL if status in services.REVIEWABLE_STATUSES else Page.TextState.NONE,
        width=W * 2,
        height=H * 2,
    )
    if not with_lines:
        return page
    body = Region.objects.create(page=page, kind="body", bbox=[0, 0, W, 150], order=0)
    foot = Region.objects.create(page=page, kind="footnote", bbox=[0, 160, W, H], order=1)
    make_line(
        page,
        0,
        body,
        [
            tok("قال", bbox=[80, 0, 100, 20]),
            tok("الكتب", "low", alt="الكتاب", tess="الكتاث", bbox=[40, 0, 80, 20]),
            tok("١٩٦٦", "low", bbox=[0, 0, 40, 20], digit=True),
        ],
    )
    make_line(
        page,
        1,
        body,
        [
            tok("وهذا", bbox=[70, 20, 100, 40]),
            tok("سطرٌ", bbox=[40, 20, 70, 40]),
            tok("ثان", "low", alt="ثانٍ", bbox=[0, 20, 40, 40]),
        ],
    )
    make_line(page, 2, foot, [tok("(١)", "low", bbox=[80, 160, 100, 180], digit=True), tok("حاشية")])
    services.refresh_page_text(page)
    return page


@pytest.fixture
def book(db):
    return Book.objects.create(title="كتاب المراجعة", status=Book.Status.READY_FOR_REVIEW)


@pytest.fixture
def page(book):
    page = make_page(book, 1)
    pre = Preprocess.objects.create(page=page, output_width=W, output_height=H)
    save_array(pre.gray_image, np.full((H, W), 230, dtype=np.uint8), "gray.png")
    save_array(pre.display_image, np.full((H, W), 230, dtype=np.uint8), "display.webp")
    save_array(pre.thumbnail, np.full((20, 10), 230, dtype=np.uint8), "thumb.webp")
    pre.save()
    return page


def role_user(name: str, role: str | None) -> User:
    user = User.objects.create_user(name, password="pass-1234")
    if role:
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


@pytest.fixture
def reviewer(db):
    return role_user("reader", "proofreader")


def lines_of(page) -> list[Line]:
    return list(page.lines.order_by("order", "id"))


def reload(page) -> Page:
    page.refresh_from_db()
    return page


# ---------------------------------------------------------------- final text and counts


def test_refresh_page_text_puts_footnotes_after_a_blank_line_with_western_digits(page):
    assert page.final_text == "قال الكتب 1966\nوهذا سطرٌ ثان\n\n(1) حاشية"
    assert page.n_unresolved == 4
    # the lines keep the raw digits
    assert lines_of(page)[0].text == "قال الكتب ١٩٦٦"


def test_printed_number_never_reenters_the_text(page):
    page.printed_number = "21"
    page.save()
    number = Region.objects.create(page=page, kind="page_number", bbox=[0, 190, W, H], order=2)
    Line.objects.create(page=page, order=3, region=number, text="٢١", ocr_text="٢١", tokens=[tok("٢١")])
    services.refresh_page_text(page)
    assert "21" not in reload(page).final_text


# ---------------------------------------------------------------- resolve


@pytest.mark.parametrize(
    ("choice", "text", "expected"),
    [
        ("primary", None, "الكتب"),
        ("secondary", None, "الكتاب"),
        ("tess", None, "الكتاث"),
        ("typed", " الكِتَاب ", "الكِتَاب"),
    ],
)
def test_resolve_token_applies_each_choice(page, reviewer, choice, text, expected):
    line = lines_of(page)[0]
    line = services.resolve_token(line, 1, choice, text, reviewer)
    token = line.tokens[1]
    assert token["t"] == expected and token["res"] == choice and token["conf"] == "low"
    assert token["bbox"] == [40, 0, 80, 20]
    assert line.n_low == 1 and line.text == f"قال {expected} ١٩٦٦"
    page = reload(page)
    assert page.n_unresolved == 3
    assert page.final_text.startswith(f"قال {expected} 1966\n")  # diacritics kept, digits Western
    revision = page.revisions.get()
    assert revision.action == "resolve" and revision.line_id == line.pk and revision.user == reviewer
    assert revision.before["tokens"][1]["t"] == "الكتب" and revision.after["tokens"][1]["res"] == choice


def test_resolve_primary_after_another_reading_restores_the_primary_word(page):
    line = lines_of(page)[0]
    line = services.resolve_token(line, 1, "secondary")
    assert line.tokens[1]["orig"] == "الكتب"
    line = services.resolve_token(line, 1, "primary")
    assert line.tokens[1]["t"] == "الكتب" and line.tokens[1]["res"] == "primary"


def test_resolve_accepts_a_digit_token_with_primary(page):
    line = services.resolve_token(lines_of(page)[0], "2", "primary")
    assert line.tokens[2]["res"] == "primary" and line.n_low == 1


@pytest.mark.parametrize(
    ("index", "choice", "text", "message"),
    [
        (9, "primary", None, "رقم الكلمة غير صالح."),
        ("x", "primary", None, "رقم الكلمة غير صالح."),
        (True, "primary", None, "رقم الكلمة غير صالح."),
        (1, "best", None, "اختيار غير معروف."),
        (2, "secondary", None, "لا توجد قراءة للنموذج الثاني لهذه الكلمة."),
        (2, "tess", None, "لا توجد قراءة Tesseract لهذه الكلمة."),
        (1, "typed", "   ", "اكتب التصحيح أولًا."),
        (1, "typed", "كلمة " * 7, None),
    ],
)
def test_resolve_token_rejects_bad_input_in_arabic(page, index, choice, text, message):
    with pytest.raises(services.ReviewError) as exc:
        services.resolve_token(lines_of(page)[0], index, choice, text)
    if message:
        assert str(exc.value) == message
    assert not page.revisions.exists() and reload(page).n_unresolved == 4


def test_review_actions_are_refused_before_the_final_text(book):
    page = make_page(book, 2)
    page.status = Page.Status.LAYOUT_DONE
    page.text_state = Page.TextState.PROVISIONAL
    page.save()
    with pytest.raises(services.ReviewError):
        services.resolve_token(lines_of(page)[0], 1, "primary")
    with pytest.raises(services.ReviewError):
        services.insert_line(page, None, "نص")


# ---------------------------------------------------------------- edit


def test_edit_line_keeps_unchanged_tokens_and_types_the_changed_ones(page, reviewer):
    line = lines_of(page)[0]
    line = services.resolve_token(line, 2, "primary")
    line = services.edit_line(line, "قال  الكِتاب ١٩٦٦ جديدة", reviewer)
    tokens = line.tokens
    assert [t["t"] for t in tokens] == ["قال", "الكِتاب", "١٩٦٦", "جديدة"]
    assert tokens[0] == tok("قال", bbox=[80, 0, 100, 20], res=None)  # unchanged dict kept whole
    assert (
        tokens[2]["res"] == "primary" and tokens[2]["conf"] == "low" and tokens[2]["bbox"] == [0, 0, 40, 20]
    )
    assert tokens[1] == {
        "t": "الكِتاب",
        "alt": None,
        "tess": None,
        "conf": "high",
        "digit": False,
        "bbox": [40, 0, 80, 20],  # 1:1 replacement keeps the old box
        "res": "typed",
    }
    assert tokens[3]["bbox"] is None and tokens[3]["res"] == "typed"
    assert line.text == "قال الكِتاب ١٩٦٦ جديدة" and line.ocr_text == "قال الكتب ١٩٦٦"
    assert line.n_low == 0
    page = reload(page)
    assert page.n_unresolved == 2 and page.final_text.startswith("قال الكِتاب 1966 جديدة\n")
    assert page.revisions.filter(action="edit").count() == 1


def test_edit_line_digit_tokens_are_marked_and_empty_text_is_refused(page):
    line = services.edit_line(lines_of(page)[1], "وهذا سطرٌ 12")
    assert line.tokens[2]["digit"] is True and line.tokens[2]["conf"] == "high"
    with pytest.raises(services.ReviewError, match="حذف السطر"):
        services.edit_line(line, "  ")


def test_edit_line_with_the_same_text_records_nothing(page):
    line = lines_of(page)[1]
    services.edit_line(line, " وهذا   سطرٌ ثان ")
    assert not page.revisions.exists()


# ---------------------------------------------------------------- insert and delete


def test_insert_line_below_a_line_shifts_the_orders(page, reviewer):
    first, second, foot = lines_of(page)
    line = services.insert_line(page, first.pk, "جملة سقطت من النموذج", reviewer)
    assert line.order == 1 and line.is_manual and line.region_id == first.region_id
    assert line.bbox is None and line.ocr_text == "" and line.updated_by == reviewer
    assert all(t["res"] == "typed" and t["conf"] == "high" for t in line.tokens)
    assert [ln.pk for ln in lines_of(page)] == [first.pk, line.pk, second.pk, foot.pk]
    assert [ln.order for ln in lines_of(page)] == [0, 1, 2, 3]
    assert reload(page).final_text == "قال الكتب 1966\nجملة سقطت من النموذج\nوهذا سطرٌ ثان\n\n(1) حاشية"


def test_insert_line_at_the_top_uses_the_first_body_region(page):
    line = services.insert_line(page, None, "عنوان")
    body = page.regions.get(kind="body")
    assert line.order == 0 and line.region_id == body.pk
    assert [ln.order for ln in lines_of(page)] == [0, 1, 2, 3]


def test_insert_line_below_a_footnote_goes_to_the_footnotes(page):
    foot = lines_of(page)[2]
    services.insert_line(page, foot.pk, "(٢) حاشية ثانية")
    assert reload(page).final_text.endswith("(1) حاشية\n(2) حاشية ثانية")


def test_insert_line_rejects_empty_text_and_foreign_lines(page, book):
    other = make_page(book, 2)
    with pytest.raises(services.ReviewError):
        services.insert_line(page, None, " ")
    with pytest.raises(services.ReviewError, match="غير موجود"):
        services.insert_line(page, lines_of(other)[0].pk, "نص")


def test_delete_line_compacts_the_orders_and_rebuilds_the_text(page, reviewer):
    first, second, foot = lines_of(page)
    deleted = services.delete_line(second, reviewer)
    assert deleted == second.pk and not Line.objects.filter(pk=second.pk).exists()
    assert [(ln.pk, ln.order) for ln in lines_of(page)] == [(first.pk, 0), (foot.pk, 1)]
    page = reload(page)
    assert page.final_text == "قال الكتب 1966\n\n(1) حاشية" and page.n_unresolved == 3
    revision = page.revisions.get()
    assert revision.action == "delete" and revision.line is None and revision.before["id"] == second.pk


# ---------------------------------------------------------------- undo


def test_undo_resolve_restores_the_word(page, reviewer):
    line = services.resolve_token(lines_of(page)[0], 1, "secondary")
    payload = services.undo_last(page, reviewer)
    line.refresh_from_db()
    assert line.tokens[1]["t"] == "الكتب" and line.tokens[1]["res"] is None and line.n_low == 2
    assert reload(page).n_unresolved == 4 and page.final_text.startswith("قال الكتب 1966")
    assert page.revisions.get().undone
    assert payload["counts"] == {"low_total": 4, "unresolved": 4, "resolved": 0}


def test_undo_edit_restores_text_and_tokens(page):
    line = lines_of(page)[1]
    original = [services.normalize_token(t) for t in line.tokens]
    services.edit_line(line, "سطر آخر تمامًا")
    services.undo_last(page)
    line.refresh_from_db()
    assert line.tokens == original and line.text == "وهذا سطرٌ ثان" and line.n_low == 1


def test_undo_insert_removes_the_line(page):
    first = lines_of(page)[0]
    inserted = services.insert_line(page, first.pk, "زائد")
    services.undo_last(page)
    assert not Line.objects.filter(pk=inserted.pk).exists()
    assert [ln.order for ln in lines_of(page)] == [0, 1, 2]
    assert "زائد" not in reload(page).final_text


def test_undo_delete_brings_the_line_back_at_its_place(page):
    first, second, foot = lines_of(page)
    services.resolve_token(second, 2, "secondary")  # a revision linked to the line before it is deleted
    services.delete_line(second)
    services.undo_last(page)
    restored = Line.objects.get(pk=second.pk)
    assert [(ln.pk, ln.order) for ln in lines_of(page)] == [(first.pk, 0), (second.pk, 1), (foot.pk, 2)]
    assert restored.text == "وهذا سطرٌ ثانٍ" and restored.region_id == second.region_id
    assert restored.tokens[2]["res"] == "secondary" and restored.bbox == second.bbox
    # the earlier resolve can be undone next: it is linked to the line again
    assert page.revisions.get(action="resolve").line_id == second.pk
    services.undo_last(page)
    restored.refresh_from_db()
    assert restored.tokens[2]["t"] == "ثان" and restored.tokens[2]["res"] is None
    assert reload(page).n_unresolved == 4


def test_undo_goes_back_one_revision_at_a_time_and_then_refuses(page):
    first = lines_of(page)[0]
    services.resolve_token(first, 1, "secondary")
    services.resolve_token(first, 2, "primary")
    services.undo_last(page)
    first.refresh_from_db()
    assert first.tokens[2]["res"] is None and first.tokens[1]["res"] == "secondary"
    services.undo_last(page)
    first.refresh_from_db()
    assert first.tokens[1]["res"] is None
    with pytest.raises(services.ReviewError, match="لا شيء للتراجع عنه"):
        services.undo_last(page)


def test_undo_approve_and_reopen_restore_the_review_state(page, reviewer, book):
    services.approve_page(page, reviewer, force=True)
    services.undo_last(page, reviewer)
    page = reload(page)
    assert page.status == Page.Status.OCR_DONE and page.reviewed_by is None and page.reviewed_at is None
    assert not page.lines.filter(is_reviewed=True).exists()
    book.refresh_from_db()
    assert book.status == Book.Status.READY_FOR_REVIEW

    services.approve_page(page, reviewer, force=True)
    stamp = reload(page).reviewed_at
    services.reopen_page(page, reviewer)
    assert reload(page).status == Page.Status.OCR_DONE
    services.undo_last(page, reviewer)
    page = reload(page)
    assert page.status == Page.Status.REVIEWED and page.reviewed_by == reviewer and page.reviewed_at == stamp
    assert page.lines.filter(is_reviewed=True).count() == 3


def test_a_new_ocr_pass_makes_older_revisions_final(page):
    from ocr import services as ocr_services

    services.resolve_token(lines_of(page)[0], 1, "secondary")
    ocr_services.finalize_page(page)  # no runs: the unreviewed lines are rebuilt (empty here)
    assert page.revisions.filter(undone=False).count() == 0
    with pytest.raises(services.ReviewError, match="لا شيء للتراجع عنه"):
        services.undo_last(page)


# ---------------------------------------------------------------- approve and reopen


def test_approve_is_blocked_while_words_are_unresolved(page, reviewer):
    with pytest.raises(services.ReviewBlocked) as exc:
        services.approve_page(page, reviewer)
    assert exc.value.unresolved == 4 and "بقيت 4 كلمة غير محسومة" in str(exc.value)
    assert reload(page).status == Page.Status.OCR_DONE and not page.revisions.exists()


def test_approve_forced_marks_the_page_and_lines_reviewed(page, reviewer, book):
    nxt = make_page(book, 2)
    result = services.approve_page(page, reviewer, force=True)
    page = reload(page)
    assert result["status"] == "reviewed"
    assert result["next_review_url"] == reverse("review:page", args=[book.pk, 2])
    assert result["next_payload_url"] == reverse("api:page_review", args=[nxt.pk])
    assert page.status == Page.Status.REVIEWED and page.reviewed_by == reviewer and page.reviewed_at
    assert page.lines.filter(is_reviewed=False).count() == 0
    assert page.n_unresolved == 4  # forced: the words stay unresolved
    book.refresh_from_db()
    assert book.status == Book.Status.REVIEWING
    assert page.revisions.get().action == "approve"
    # approving again changes nothing
    assert services.approve_page(page, reviewer)["status"] == "reviewed"
    assert page.revisions.count() == 1


def test_approve_after_resolving_everything_needs_no_force(page):
    for line in lines_of(page):
        for i, t in enumerate(line.tokens):
            if t["conf"] == "low":
                line = services.resolve_token(line, i, "primary")
    assert reload(page).n_unresolved == 0
    result = services.approve_page(page, None)
    assert result["status"] == "reviewed" and result["next_payload_url"] is None
    assert result["next_review_url"] == reverse("review:next", args=[page.book_id]) + "?after=1"


def test_reopen_needs_an_approved_page(page, reviewer):
    with pytest.raises(services.ReviewError):
        services.reopen_page(page, reviewer)
    services.approve_page(page, reviewer, force=True)
    services.reopen_page(page, reviewer)
    page = reload(page)
    assert page.status == Page.Status.OCR_DONE and not page.lines.filter(is_reviewed=True).exists()
    assert page.revisions.first().action == "reopen"


def test_insert_on_an_approved_page_is_marked_reviewed(page, reviewer):
    services.approve_page(page, reviewer, force=True)
    line = services.insert_line(page, None, "إضافة")
    assert line.is_reviewed


# ---------------------------------------------------------------- read models


def test_next_page_to_review_follows_after_and_wraps(book):
    p1, p2, p3 = (make_page(book, n, with_lines=False) for n in (1, 2, 3))
    make_page(book, 4, status=Page.Status.REVIEWED, with_lines=False)
    Page.objects.filter(pk=p2.pk).update(is_excluded=True)
    assert services.next_page_to_review(book) == p1
    assert services.next_page_to_review(book, after_number=1) == p3
    assert services.next_page_to_review(book, after_number=3) == p1
    Page.objects.filter(pk__in=[p1.pk, p3.pk]).update(status=Page.Status.REVIEWED)
    assert services.next_page_to_review(book, after_number=3) is None


def test_book_review_summary_counts_pages_and_words(page, book):
    make_page(book, 2, status=Page.Status.REVIEWED)
    summary = services.book_review_summary(book)
    assert summary == {
        "reviewed": 1,
        "total": 2,
        "pending": 1,
        "unresolved_total": 8,
        "next_review_url": reverse("review:next", args=[book.pk]),
    }
    Page.objects.filter(pk=page.pk).update(status=Page.Status.LAYOUT_DONE)
    assert services.book_review_summary(book)["next_review_url"] is None


def test_filmstrip_lists_non_excluded_pages_in_one_query(page, book, django_assert_num_queries):
    make_page(book, 2, status=Page.Status.REVIEWED, with_lines=False)
    excluded = make_page(book, 3, with_lines=False)
    Page.objects.filter(pk=excluded.pk).update(is_excluded=True)
    with django_assert_num_queries(1):
        strip = services.filmstrip(book)
    assert [item["number"] for item in strip] == [1, 2]
    assert strip[0]["thumb_url"].endswith("thumb.webp") and strip[0]["n_unresolved"] == 4
    assert strip[1]["is_reviewed"] and strip[1]["thumb_url"] is None
    assert strip[0]["url"] == reverse("review:page", args=[book.pk, 1])
    assert set(strip[0]) == {"id", "number", "thumb_url", "is_reviewed", "n_unresolved", "status", "url"}


def test_review_payload_shape(page, reviewer, book):
    make_page(book, 2)
    payload = services.review_payload(page, reviewer)
    assert set(payload) == {
        "page",
        "book",
        "image",
        "regions",
        "lines",
        "counts",
        "labels",
        "nav",
        "urls",
        "can_edit",
    }
    assert payload["page"]["number"] == 1 and payload["page"]["is_reviewed"] is False
    assert payload["book"] == {
        "id": book.pk,
        "title": "كتاب المراجعة",
        "total_pages": 2,
        "reviewed_pages": 0,
        "unresolved_total": 8,
    }
    assert payload["image"]["width"] == W and payload["image"]["height"] == H
    assert payload["image"]["display_url"].endswith("display.webp")
    assert [r["kind"] for r in payload["regions"]] == ["body", "footnote"]
    line = payload["lines"][0]
    assert line["region_kind"] == "body" and line["is_manual"] is False and line["n_low"] == 2
    assert set(line["tokens"][0]) >= {"t", "alt", "tess", "conf", "digit", "bbox", "res"}
    assert payload["lines"][2]["region_kind"] == "footnote"
    assert payload["counts"] == {"low_total": 4, "unresolved": 4, "resolved": 0}
    assert payload["labels"] == {"primary": "Qari v0.3", "secondary": "Qari v0.2"}
    assert payload["nav"]["prev_url"] is None
    assert payload["nav"]["next_url"] == reverse("review:page", args=[book.pk, 2])
    assert payload["nav"]["next_review_url"] == f"/books/{book.pk}/review/next/?after=1"
    assert payload["urls"]["resolve"] == "/api/lines/__id__/resolve/"
    assert payload["urls"]["insert"] == f"/api/pages/{page.pk}/lines/"
    assert payload["urls"]["filmstrip"] == f"/api/books/{book.pk}/filmstrip/"
    assert payload["can_edit"] is True
    assert services.review_payload(page, role_user("guest", None))["can_edit"] is False


# ---------------------------------------------------------------- API


def post(client, name, obj_id, data=None, **kwargs):
    kwarg = "line_id" if name.startswith("line_") else "page_id"
    url = reverse(f"api:{name}", kwargs={kwarg: obj_id})
    return client.post(url, json.dumps(data or {}), content_type="application/json", **kwargs)


@pytest.fixture
def reviewer_client(client, reviewer):
    client.force_login(reviewer)
    return client


def test_api_refuses_anonymous_users(client, page, book):
    line = lines_of(page)[0]
    assert client.get(reverse("api:page_review", args=[page.pk])).status_code == 403
    assert client.get(reverse("api:book_filmstrip", args=[book.pk])).status_code == 403
    assert post(client, "line_resolve", line.pk, {"index": 1, "choice": "primary"}).status_code == 403
    assert post(client, "page_approve", page.pk, {"force": True}).status_code == 403


def test_api_posts_need_a_reviewer_role(client, page):
    client.force_login(role_user("guest", None))
    assert client.get(reverse("api:page_review", args=[page.pk])).status_code == 200
    response = post(client, "line_resolve", lines_of(page)[0].pk, {"index": 1, "choice": "primary"})
    assert response.status_code == 403 and "مراجع" in response.json()["detail"]
    for role in ("editor", "admin"):
        client.force_login(role_user(f"u-{role}", role))
        response = post(client, "line_resolve", lines_of(page)[0].pk, {"index": 1, "choice": "primary"})
        assert response.status_code == 200


def test_api_resolve_edit_delete_insert(reviewer_client, page):
    first, second, foot = lines_of(page)
    response = post(reviewer_client, "line_resolve", first.pk, {"index": 1, "choice": "secondary"})
    assert response.status_code == 200
    body = response.json()
    assert body["line"]["tokens"][1]["t"] == "الكتاب" and body["line"]["tokens"][1]["res"] == "secondary"
    assert body["counts"] == {
        "line_n_low": 1,
        "page_unresolved": 3,
        "page_low_total": 4,
        "book_unresolved_total": 3,
    }
    assert body["page"]["n_unresolved"] == 3 and body["page"]["status"] == "ocr_done"

    response = post(reviewer_client, "line_edit", second.pk, {"text": "وهذا سطرٌ ثانٍ"})
    assert response.status_code == 200 and response.json()["counts"]["line_n_low"] == 0

    response = post(reviewer_client, "page_lines", page.pk, {"after": first.pk, "text": "سطر مُدرج"})
    assert response.status_code == 201
    body = response.json()
    assert body["line"]["is_manual"] is True and body["line"]["order"] == 1
    assert [row["order"] for row in body["lines"]] == [0, 1, 2, 3]
    assert body["counts"]["page_unresolved"] == 2

    response = post(reviewer_client, "line_delete", body["line"]["id"])
    assert response.status_code == 200
    assert response.json() == {
        "deleted_id": body["line"]["id"],
        # the edit typed «ثانٍ» (high confidence): 3 low words are left on the page
        "counts": {"line_n_low": 0, "page_unresolved": 2, "page_low_total": 3, "book_unresolved_total": 2},
    }


def test_api_errors_are_400_with_an_arabic_message(reviewer_client, page):
    line = lines_of(page)[0]
    response = post(reviewer_client, "line_resolve", line.pk, {"index": 2, "choice": "secondary"})
    assert response.status_code == 400
    assert response.json() == {"message": "لا توجد قراءة للنموذج الثاني لهذه الكلمة."}
    assert post(reviewer_client, "line_edit", line.pk, {"text": ""}).status_code == 400
    assert post(reviewer_client, "page_lines", page.pk, {"after": 99999, "text": "x"}).status_code == 400
    response = post(reviewer_client, "page_undo", page.pk)
    assert response.status_code == 400 and response.json()["message"] == "لا شيء للتراجع عنه."
    assert post(reviewer_client, "page_reopen", page.pk).status_code == 400
    assert post(reviewer_client, "line_resolve", 99999, {"index": 0, "choice": "primary"}).status_code == 404


def test_api_approve_409_then_forced_then_undo_and_reopen(reviewer_client, page):
    response = post(reviewer_client, "page_approve", page.pk, {})
    assert response.status_code == 409
    assert response.json()["unresolved"] == 4 and "4" in response.json()["message"]
    response = post(reviewer_client, "page_approve", page.pk, {"force": True})
    assert response.status_code == 200 and response.json()["status"] == "reviewed"
    assert "next_review_url" in response.json()
    response = post(reviewer_client, "page_reopen", page.pk)
    assert response.status_code == 200 and response.json()["page"]["status"] == "ocr_done"
    response = post(reviewer_client, "page_undo", page.pk)
    assert response.status_code == 200 and response.json()["page"]["status"] == "reviewed"


def test_api_payload_and_filmstrip(reviewer_client, page, book):
    response = reviewer_client.get(reverse("api:page_review", args=[page.pk]))
    assert response.status_code == 200 and response.json()["page"]["id"] == page.pk
    response = reviewer_client.get(reverse("api:book_filmstrip", args=[book.pk]))
    assert response.status_code == 200
    assert response.json()["book_id"] == book.pk and response.json()["pages"][0]["number"] == 1


def test_api_enforces_csrf(page, reviewer):
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(reviewer)
    line = lines_of(page)[0]
    data = {"index": 1, "choice": "primary"}
    assert post(strict, "line_resolve", line.pk, data).status_code == 403
    html = strict.get(reverse("books:detail", args=[page.book_id])).content.decode()
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', html).group(1)
    assert post(strict, "line_resolve", line.pk, data, HTTP_X_CSRFTOKEN=token).status_code == 200


# ---------------------------------------------------------------- views


def test_review_page_renders_the_config_json(reviewer_client, page, book):
    response = reviewer_client.get(reverse("review:page", args=[book.pk, 1]))
    assert response.status_code == 200
    assert response.context["book"] == book and response.context["page"] == page
    assert response.context["prev_url"] is None and response.context["next_url"] is None
    html = response.content.decode()
    match = re.search(r'<script id="review-config" type="application/json">(.*?)</script>', html, re.S)
    config = json.loads(match.group(1))
    assert config["page"]["id"] == page.pk and config["can_edit"] is True
    assert config == json.loads(json.dumps(response.context["config"]))


def test_review_page_needs_a_login_and_an_existing_page(client, page, book, reviewer):
    response = client.get(reverse("review:page", args=[book.pk, 1]))
    assert response.status_code == 302 and "/accounts/" in response["Location"]
    client.force_login(reviewer)
    assert client.get(reverse("review:page", args=[book.pk, 99])).status_code == 404


def test_review_next_redirects_to_the_next_page_or_the_dashboard(reviewer_client, page, book):
    make_page(book, 2, status=Page.Status.REVIEWED, with_lines=False)
    make_page(book, 3, with_lines=False)
    url = reverse("review:next", args=[book.pk])
    response = reviewer_client.get(url)
    assert response.status_code == 302 and response["Location"] == reverse("review:page", args=[book.pk, 1])
    response = reviewer_client.get(f"{url}?after=1")
    assert response["Location"] == reverse("review:page", args=[book.pk, 3])
    response = reviewer_client.get(f"{url}?after=abc")
    assert response["Location"] == reverse("review:page", args=[book.pk, 1])
    Page.objects.filter(book=book).update(status=Page.Status.REVIEWED)
    response = reviewer_client.get(url, follow=True)
    assert response.redirect_chain[-1][0] == reverse("books:detail", args=[book.pk])
    assert "لا صفحات بانتظار المراجعة" in [str(m) for m in response.context["messages"]]


def test_line_revision_admin_and_labels(db):
    assert LineRevision.Action.RESOLVE.label == "حسم كلمة"
    from django.apps import apps

    assert apps.get_app_config("review").verbose_name == "المراجعة"


# ---------------------------------------------------------------- merge / delete one word (D31)


def split_name_line(page) -> Line:
    """A body line where the models split «هيرودوت» in two, plus a stray letter the OCR added."""
    body = page.regions.get(kind="body")
    return make_line(
        page,
        3,
        body,
        [
            tok("هير", bbox=[60, 60, 100, 80]),
            tok("ودوت", "low", alt="ودت", bbox=[30, 62, 58, 81]),
            tok("ب", "low", bbox=[24, 70, 28, 78]),
            tok("قال", bbox=[0, 60, 22, 80]),
        ],
    )


def test_merge_tokens_joins_a_split_word_under_one_box_and_undo_splits_it_again(page, reviewer):
    line = split_name_line(page)
    services.refresh_page_text(page)
    unresolved = reload(page).n_unresolved
    line = services.merge_tokens(line, 0, reviewer)
    assert [t["t"] for t in line.tokens] == ["هيرودوت", "ب", "قال"]
    merged = line.tokens[0]
    assert merged["bbox"] == [30, 60, 100, 81] and merged["conf"] == "high" and merged["res"] == "typed"
    assert merged["alt"] is None and merged["tess"] is None
    assert line.text == "هيرودوت ب قال" and line.n_low == 1  # the uncertain half is resolved by the merge
    page = reload(page)
    assert page.n_unresolved == unresolved - 1 and "هيرودوت" in page.final_text
    assert page.revisions.first().action == LineRevision.Action.MERGE
    services.undo_last(page, reviewer)
    line.refresh_from_db()
    assert [t["t"] for t in line.tokens] == ["هير", "ودوت", "ب", "قال"] and line.n_low == 2
    assert line.tokens[1]["alt"] == "ودت" and reload(page).n_unresolved == unresolved


def test_merge_tokens_needs_a_following_word_on_the_line(page):
    line = split_name_line(page)
    with pytest.raises(services.ReviewError, match="لا توجد كلمة بعدها في هذا السطر للدمج."):
        services.merge_tokens(line, 3)
    with pytest.raises(services.ReviewError, match="رقم الكلمة غير صالح."):
        services.merge_tokens(line, 9)


def test_delete_token_removes_a_stray_letter_and_undo_restores_it(page, reviewer):
    line = split_name_line(page)
    result = services.delete_token(line, 2, reviewer)
    line = result["line"]
    assert result["deleted_line_id"] is None and [t["t"] for t in line.tokens] == ["هير", "ودوت", "قال"]
    assert line.n_low == 1 and page.revisions.first().action == LineRevision.Action.DROP_WORD
    assert " ب " not in reload(page).final_text
    services.undo_last(page, reviewer)
    line.refresh_from_db()
    assert [t["t"] for t in line.tokens] == ["هير", "ودوت", "ب", "قال"]
    assert line.tokens[2]["bbox"] == [24, 70, 28, 78] and line.n_low == 2


def test_delete_token_of_a_lines_only_word_deletes_the_line_and_undo_brings_it_back(page):
    lone = make_line(page, 3, page.regions.get(kind="body"), [tok("٧", "low", digit=True)])
    result = services.delete_token(lone, 0)
    assert result == {"line": None, "deleted_line_id": lone.pk}
    assert not Line.objects.filter(pk=lone.pk).exists()
    assert page.revisions.first().action == LineRevision.Action.DELETE
    services.undo_last(page)
    assert Line.objects.get(pk=lone.pk).tokens[0]["t"] == "٧"


def test_api_merge_and_delete_word(reviewer_client, page):
    from django.test import Client

    line = split_name_line(page)
    services.refresh_page_text(page)
    assert post(Client(), "line_merge", line.pk, {"index": 0}).status_code == 403  # anonymous
    response = post(reviewer_client, "line_merge", line.pk, {"index": 0})
    assert response.status_code == 200
    body = response.json()
    assert [t["t"] for t in body["line"]["tokens"]] == ["هيرودوت", "ب", "قال"] and body["counts"][
        "line_n_low"
    ] == 1
    response = post(reviewer_client, "line_delete_word", line.pk, {"index": 1})
    assert response.status_code == 200 and [t["t"] for t in response.json()["line"]["tokens"]] == [
        "هيرودوت",
        "قال",
    ]
    response = post(reviewer_client, "line_merge", line.pk, {"index": 1})
    assert (
        response.status_code == 400 and response.json()["message"] == "لا توجد كلمة بعدها في هذا السطر للدمج."
    )
    lone = make_line(page, 4, page.regions.get(kind="body"), [tok("٧", "low", digit=True)])
    response = post(reviewer_client, "line_delete_word", lone.pk, {"index": 0})
    assert response.status_code == 200 and response.json()["deleted_id"] == lone.pk
    urls = services.review_payload(reload(page), None)["urls"]
    assert (
        urls["merge"] == "/api/lines/__id__/merge/"
        and urls["delete_word"] == "/api/lines/__id__/delete-word/"
    )
