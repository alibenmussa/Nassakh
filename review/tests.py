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
    assert payload["counts"] == {
        "low_total": 4,
        "unresolved": 4,
        "resolved": 0,
        "words": 4,
        "groups": 0,
        "gaps": 0,
    }


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
    assert set(strip[0]) == {
        "id",
        "number",
        "thumb_url",
        "is_reviewed",
        "n_unresolved",
        "status",
        "url",
        "readers",
    }
    assert strip[0]["readers"] == ""  # read before 7b (D73)


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
        "next_step",
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
        "edited": False,
    }
    assert payload["image"]["width"] == W and payload["image"]["height"] == H
    assert payload["image"]["display_url"].endswith("display.webp")
    assert [r["kind"] for r in payload["regions"]] == ["body", "footnote"]
    line = payload["lines"][0]
    assert line["region_kind"] == "body" and line["is_manual"] is False and line["n_low"] == 2
    assert set(line["tokens"][0]) >= {"t", "alt", "tess", "conf", "digit", "bbox", "res"}
    assert payload["lines"][2]["region_kind"] == "footnote"
    assert payload["counts"] == {
        "low_total": 4,
        "unresolved": 4,
        "resolved": 0,
        "words": 4,
        "groups": 0,
        "gaps": 0,
    }
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
        "page_words": 3,
        "page_groups": 0,
        "page_gaps": 0,
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
        "counts": {
            "line_n_low": 0,
            "page_unresolved": 2,
            "page_low_total": 3,
            "book_unresolved_total": 2,
            "page_words": 2,
            "page_groups": 0,
            "page_gaps": 0,
        },
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


# ---------------------------------------------------------------- line role (D32)


def test_set_line_role_marks_headings_and_undo_restores_body(page, reviewer):
    first = lines_of(page)[0]
    assert first.role == Line.Role.BODY
    line = services.set_line_role(first, "heading", reviewer)
    assert line.role == "heading" and page.revisions.first().action == LineRevision.Action.ROLE
    line = services.set_line_role(line, "subheading", reviewer)
    assert line.role == "subheading"
    count = page.revisions.count()
    services.set_line_role(line, "subheading", reviewer)  # unchanged: nothing recorded
    assert page.revisions.count() == count
    services.undo_last(page, reviewer)
    line.refresh_from_db()
    assert line.role == "heading"
    services.undo_last(page, reviewer)
    line.refresh_from_db()
    assert line.role == "body" and line.text == first.text  # the words are untouched


def test_set_line_role_refuses_unknown_roles_and_allows_headings_on_footnote_lines(page):
    """D74: the refusal «سطر الحاشية لا يكون عنوانًا» is gone; `main` is stored, never chosen."""
    first, _second, foot = lines_of(page)
    for role in ("chapter", "main"):
        with pytest.raises(services.ReviewError, match="نوع السطر غير معروف."):
            services.set_line_role(first, role)
    assert services.set_line_role(foot, "heading").role == "heading"


def test_undo_of_a_deleted_heading_brings_its_role_back(page):
    line = services.set_line_role(lines_of(page)[0], "heading")
    services.delete_line(line)
    services.undo_last(page)
    assert Line.objects.get(pk=line.pk).role == "heading"


def test_api_line_role_and_payload(reviewer_client, page):
    from django.test import Client

    first = lines_of(page)[0]
    assert post(Client(), "line_role", first.pk, {"role": "heading"}).status_code == 403  # anonymous
    response = post(reviewer_client, "line_role", first.pk, {"role": "heading"})
    assert response.status_code == 200 and response.json()["line"]["role"] == "heading"
    response = post(reviewer_client, "line_role", first.pk, {"role": "nope"})
    assert response.status_code == 400 and response.json()["message"] == "نوع السطر غير معروف."
    payload = services.review_payload(reload(page), None)
    assert payload["lines"][0]["role"] == "heading" and payload["lines"][1]["role"] == "body"
    assert payload["urls"]["role"] == "/api/lines/__id__/role/"


# ---------------------------------------------------------------- stale lines (two tabs, failed actions)


def test_a_stale_word_index_is_refused_instead_of_changing_the_next_word(page):
    line = lines_of(page)[0]  # قال · الكتب · ١٩٦٦
    services.merge_tokens(line, 0, expected="قال", expected_next="الكتب")  # tab B merges the first two words
    # tab A still shows the old words: index 1 is now «١٩٦٦», not «الكتب»
    with pytest.raises(services.ReviewConflict) as exc:
        services.resolve_token(line, 1, "primary", expected="الكتب")
    assert exc.value.line.pk == line.pk and str(exc.value) == "تغيّر هذا السطر في نافذة أخرى؛ أُعيد تحميله."
    with pytest.raises(services.ReviewConflict):
        services.delete_token(line, 1, expected="الكتب")
    with pytest.raises(services.ReviewConflict):
        services.merge_tokens(line, 0, expected="قال", expected_next="الكتب")
    line.refresh_from_db()
    assert [t["t"] for t in line.tokens] == ["قالالكتب", "١٩٦٦"] and line.tokens[1].get("res") is None
    # the word the client saw is still there: the action applies
    line = services.resolve_token(line, 1, "primary", expected="١٩٦٦")
    assert line.tokens[1]["res"] == "primary"


def test_whole_line_actions_refuse_a_stale_version(page):
    line = lines_of(page)[1]
    seen = services.line_item(line)["v"]
    assert seen == line.updated_at.isoformat()
    services.resolve_token(line, 2, "secondary")  # changed elsewhere after the client read it
    with pytest.raises(services.ReviewConflict):
        services.edit_line(line, "نص قديم", version=seen)
    with pytest.raises(services.ReviewConflict):
        services.set_line_role(line, "heading", version=seen)
    with pytest.raises(services.ReviewConflict):
        services.delete_line(line, version=seen)
    line.refresh_from_db()
    assert line.text == "وهذا سطرٌ ثانٍ" and line.role == Line.Role.BODY
    fresh = services.line_item(line)["v"]
    line = services.edit_line(line, "وهذا سطرٌ ثانٍ جديد", version=fresh)
    assert line.text.endswith("جديد")


def test_api_answers_409_with_the_current_line_on_a_conflict(reviewer_client, page):
    line = lines_of(page)[0]
    response = post(reviewer_client, "line_resolve", line.pk, {"index": 1, "choice": "secondary", "t": "قال"})
    assert response.status_code == 409
    body = response.json()
    assert body["message"] == "تغيّر هذا السطر في نافذة أخرى؛ أُعيد تحميله."
    assert body["line"]["id"] == line.pk and [t["t"] for t in body["line"]["tokens"]] == [
        "قال",
        "الكتب",
        "١٩٦٦",
    ]
    seen = body["line"]["v"]
    response = post(reviewer_client, "line_edit", line.pk, {"text": "قال الكتاب ١٩٦٦", "v": seen})
    assert response.status_code == 200 and response.json()["line"]["v"] != seen
    assert post(reviewer_client, "line_edit", line.pk, {"text": "قال", "v": seen}).status_code == 409
    assert post(reviewer_client, "line_role", line.pk, {"role": "heading", "v": seen}).status_code == 409
    assert post(reviewer_client, "line_delete", line.pk, {"v": seen}).status_code == 409
    data = {"index": 0, "t": "قال", "t_next": "الكتب"}
    assert post(reviewer_client, "line_merge", line.pk, data).status_code == 409
    assert post(reviewer_client, "line_delete_word", line.pk, {"index": 0, "t": "الكتب"}).status_code == 409
    assert [t["t"] for t in lines_of(page)[0].tokens] == ["قال", "الكتاب", "١٩٦٦"]
    # without what the client saw, nothing is checked (older clients keep working)
    assert post(reviewer_client, "line_delete_word", line.pk, {"index": 0}).status_code == 200


@pytest.mark.parametrize("force", [[True], {}, {"force": True}])
def test_api_approve_with_a_non_scalar_force_is_refused_not_a_500(reviewer_client, page, force):
    response = post(reviewer_client, "page_approve", page.pk, {"force": force})
    assert response.status_code == 409 and response.json()["unresolved"] == 4
    assert reload(page).status == Page.Status.OCR_DONE


def test_review_next_on_the_last_pending_page_stays_on_it(reviewer_client, page, book):
    make_page(book, 2, status=Page.Status.REVIEWED, with_lines=False)
    url = f"{reverse('review:next', args=[book.pk])}?after=1"
    response = reviewer_client.get(url, follow=True)
    assert response.redirect_chain[-1][0] == reverse("review:page", args=[book.pk, 1])
    assert "هذه آخر صفحة بانتظار المراجعة" in [str(m) for m in response.context["messages"]]
    Page.objects.filter(pk=page.pk).update(status=Page.Status.REVIEWED)  # nothing waits any more
    response = reviewer_client.get(url, follow=True)
    assert response.redirect_chain[-1][0] == reverse("books:detail", args=[book.pk])
    assert "لا صفحات بانتظار المراجعة" in [str(m) for m in response.context["messages"]]


# ------------------------------------------------------------ 7b: the trust contract (fixtures for the UI)
#
# The pages below hold one token of each 7b shape (D71–D74, §4.8) as `ocr.alignment.build_lines`, the
# vote (`ocr.chooser`) and the numbers pass write them; `test_trust_payloads_equal_the_fixtures` runs
# every new read and write through the API and compares the answers with review/fixtures/trust/*.json
# (the contract of the review UI; `NASSAKH_WRITE_TRUST_FIXTURES=1` rewrites the files).

TRUST_DIR = __import__("pathlib").Path(__file__).parent / "fixtures" / "trust"
TRUST_BOOK, TRUST_PAGES = 30, (900, 901, 902, 903)
STAMP = "2026-09-26T15:20:00.000000+00:00"  # every line version (`v`) and ISO stamp, normalised


def ttok(t, why=None, bbox=None, tc=None, **extra) -> dict:
    """A token as `build_lines` stores it: `conf` low exactly when `why` is set."""
    token = {
        "t": t,
        "alt": extra.pop("alt", None),
        "conf": "low" if why else "high",
        "digit": extra.pop("digit", False),
        "bbox": bbox,
        "tess": extra.pop("tess", None),
    }
    if tc is not None:
        token["tc"] = tc
    if why:
        token["why"] = list(why)
    token.update(extra)
    return token


def _trust_line(page, pk, order, region, tokens, role=Line.Role.BODY) -> Line:
    text = " ".join(t["t"] for t in tokens)
    return Line.objects.create(
        pk=pk,
        page=page,
        order=order,
        region=region,
        bbox=[0, order * 20, W, order * 20 + 20],
        text=text,
        ocr_text=text,
        tokens=tokens,
        role=role,
        n_low=ocr_count(tokens),
    )


def ocr_count(tokens) -> int:
    from ocr.services import count_unresolved

    return count_unresolved(tokens)


def trust_book() -> Book:
    """Book 30 «كتاب الثقة»: page 900 (number 1) read by two models, with a vote, every reason, two
    groups of added words (one over two lines, one filling a line), two suggestions and a note; page
    901 read by one model (a looped prefix), page 902 Tesseract's text, page 903 read before 7b."""
    from ocr.models import TextGap

    Book.objects.filter(pk=TRUST_BOOK).delete()
    book = Book.objects.create(pk=TRUST_BOOK, title="كتاب الثقة", status=Book.Status.READY_FOR_REVIEW)

    def page_of(pk, number, reading, flags=()):
        page = Page.objects.create(
            pk=pk,
            book=book,
            number=number,
            source_index=number - 1,
            status=Page.Status.OCR_DONE,
            text_state=Page.TextState.FINAL,
            width=W * 2,
            height=H * 2,
            reading=reading,
            attention_flags=list(flags),
        )
        body = Region.objects.create(pk=pk * 10, page=page, kind="body", bbox=[0, 0, W, 150], order=0)
        foot = Region.objects.create(pk=pk * 10 + 1, page=page, kind="footnote", bbox=[0, 160, W, H], order=1)
        return page, body, foot

    two, body, foot = page_of(
        900,
        1,
        {"readers": "two", "partial": False, "groups": 2, "gaps": 2},
        ["missing_text"],
    )
    _trust_line(
        two,
        9100,
        0,
        body,
        [
            ttok("قال", bbox=[88, 0, 100, 20], tc=91.0),
            # the vote (D71): Tesseract backs Qari v0.2, whose reading is in the text; still open
            ttok(
                "يحيى",
                ["disagree"],
                [70, 0, 88, 20],
                88.0,
                alt="يحيى",
                tess="يحيى",
                orig="يجي",
                pick="vote",
            ),
            # the models differ and Tesseract reads as Qari v0.3 (tess null: the same word)
            ttok("فاضلا", ["disagree"], [50, 0, 70, 20], 93.0, alt="فاضل"),
            ttok("زاهدا", ["disagree"], [30, 0, 50, 20], 64.0, alt="راهدا", tess="زاهد"),
            ttok("١٢٥", ["number"], [15, 0, 30, 20], 71.0, digit=True, tess="١٢٠"),
            ttok("سنة", bbox=[0, 0, 15, 20], tc=90.0),
        ],
    )
    _trust_line(
        two,
        9101,
        1,
        body,
        [
            ttok("وكان", ["alone"], [85, 20, 100, 40], 58.0, tess="ركان"),
            ttok("مилادية", ["script"], [65, 20, 85, 40], 77.0, alt="ميلادية", tess="ميلادية"),
            ttok("※", ["script"], [60, 20, 65, 40]),
            ttok(
                "١٩٦٦",
                ["number"],
                [45, 20, 60, 40],
                digit=True,
                src="kraken",
                qari={"t": "١٩٦٠", "alt": "١٩٦٦", "tess": None},
            ),
            # words only the second model read, supported by Tesseract (D72): group 1, over two lines
            ttok("تعالى", ["missing"], [30, 20, 45, 40], 82.0, ins=1),
            ttok("بطرابلس", ["missing"], [10, 20, 30, 40], 79.0, ins=1),
        ],
    )
    _trust_line(
        two,
        9102,
        2,
        body,
        [
            ttok("ونشأ", ["missing"], [85, 40, 100, 60], 80.0, ins=1),
            ttok("بها", ["missing"], [75, 40, 85, 60], 90.0, ins=1),
            ttok("رحمه", bbox=[60, 40, 75, 60], tc=92.0),
            ttok("الله", bbox=[50, 40, 60, 60], tc=95.0),
            # §4.8: the year printed again in words
            ttok(
                "٢٤٢",
                ["number", "year"],
                [40, 40, 50, 60],
                digit=True,
                sug={"t": "٢٤٣", "src": "words", "label": "من الحروف", "words": "ثلاث واربعين ومايتين"},
            ),
            ttok("ثلاث", bbox=[30, 40, 40, 60], tc=90.0),
            ttok("واربعين", bbox=[15, 40, 30, 60], tc=88.0),
            ttok("ومايتين", bbox=[0, 40, 15, 60], tc=86.0),
        ],
    )
    _trust_line(
        two,
        9103,
        3,
        body,
        [
            ttok(word, ["missing"], [100 - 20 * (k + 1), 60, 100 - 20 * k, 80], 85.0, ins=2)
            for k, word in enumerate(["وفيها", "مغاص", "اللؤلؤ", "المعروف", "بالخاركي،"])
        ],
    )
    _trust_line(
        two,
        9104,
        4,
        foot,
        [
            ttok("(١)", ["number"], [90, 160, 100, 180], digit=True),
            ttok("انظر", bbox=[70, 160, 90, 180], tc=94.0),
            ttok(":", bbox=[66, 160, 70, 180], tc=90.0),
        ],
    )
    # suggestions Tesseract did not support (D72): after «الله» of line 9102, and before line 9100
    TextGap.objects.create(
        pk=9500, page=two, line_id=9102, index=3, after_t="الله", text="تعالى الاجابة", support=0.33
    )
    TextGap.objects.create(pk=9501, page=two, line_id=9100, index=-1, after_t="", text="وقد", support=0.0)

    one, body, _foot = page_of(
        901, 2, {"readers": "one", "partial": True, "groups": 0, "gaps": 0}, ["single_reader"]
    )
    _trust_line(
        one,
        9110,
        0,
        body,
        [
            ttok("ولد", bbox=[80, 0, 100, 20], tc=93.0),
            # a one-reader region (D73): Tesseract's confident word there has another skeleton
            ttok("يجي", ["single"], [60, 0, 80, 20], 90.0, tess="يحيى"),
            ttok("بطرابلس", bbox=[30, 0, 60, 20], tc=88.0),
        ],
    )
    tess, body, _foot = page_of(
        902,
        3,
        {"readers": "tesseract", "partial": False, "groups": 0, "gaps": 0},
        ["ocr_fallback", "single_reader"],
    )
    _trust_line(
        tess, 9120, 0, body, [ttok("نص", bbox=[80, 0, 100, 20]), ttok("احتياطي", bbox=[40, 0, 80, 20])]
    )
    old, body, _foot = page_of(903, 4, {})
    _trust_line(old, 9130, 0, body, [ttok("قديم", bbox=[80, 0, 100, 20])])
    for page in Page.objects.filter(book=book):
        services.refresh_page_text(page)
    return book


def _normal(value):
    """`value` with line versions and ISO stamps replaced by `STAMP` (they change on every run)."""
    if isinstance(value, dict):
        return {k: STAMP if k in ("v", "decided_at") and v else _normal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normal(v) for v in value]
    return value


def _call(client, method: str, url: str, body=None) -> dict:
    if method == "GET":
        response = client.get(url)
    else:
        response = client.post(url, json.dumps(body or {}), content_type="application/json")
    return {
        "request": {"method": method, "url": url, **({"body": body} if body is not None else {})},
        "status": response.status_code,
        "response": _normal(response.json()),
    }


def _gap_url(action: str, gap_id: int) -> str:
    return reverse(f"api:gap_{action}", args=[gap_id])


def _insertion_url(group) -> str:
    return reverse("api:page_insertion", args=[900, group])


def _line_url(name: str, line_id: int) -> str:
    return reverse(f"api:{name}", args=[line_id])


def trust_contract(client) -> dict[str, object]:
    """Every fixture file's content, from live answers (see `TRUST_DIR/index.json`)."""
    from books import services as books_services
    from ocr.models import TextGap

    out: dict[str, object] = {}
    review = reverse("api:page_review", args=[900])
    undo = reverse("api:page_undo", args=[900])

    trust_book()
    payload = _call(client, "GET", review)
    out["review_payload.json"] = payload
    lines = {line["id"]: line for line in payload["response"]["lines"]}
    tokens = {
        "sure": lines[9100]["tokens"][0],
        "vote": lines[9100]["tokens"][1],
        "disagree_v03": lines[9100]["tokens"][2],
        "disagree": lines[9100]["tokens"][3],
        "number": lines[9100]["tokens"][4],
        "alone": lines[9101]["tokens"][0],
        "script_letters": lines[9101]["tokens"][1],
        "script_symbol": lines[9101]["tokens"][2],
        "kraken": lines[9101]["tokens"][3],
        "missing": lines[9101]["tokens"][4],
        "year": lines[9102]["tokens"][4],
    }
    single = _call(client, "GET", reverse("api:page_review", args=[901]))["response"]
    tokens["single"] = single["lines"][0]["tokens"][1]

    readings = {}
    for pk, name in zip(TRUST_PAGES, ("two", "one", "tesseract", "before_7b"), strict=True):
        answer = _call(client, "GET", reverse("api:page_review", args=[pk]))["response"]
        readings[name] = {"page": answer["page"], "counts": answer["counts"]}
    out["reading.json"] = {
        "readings": readings,
        "header": {
            "two": {"pill": None, "banner": None},
            "one": {
                "pill": {"text": "قراءة واحدة", "level": "warning"},
                "banner": "قرأ هذه الصفحةَ نموذجٌ واحد، فالعلامات فيها أقل من الحقيقة. قابِل كل سطر بالصورة.",
            },
            "tesseract": {
                "pill": {"text": "نص Tesseract وحده", "level": "danger"},
                "banner": "تعذّرت قراءة هذه الصفحة بالنموذجين، ونصّها من Tesseract وحده. قابِل كل سطر بالصورة.",
            },
            "before_7b": {"pill": None, "banner": None},
        },
    }

    insertion = []
    insertion.append(
        {
            "name": "keep group 1 (lines 9101 and 9102)",
            **_call(client, "POST", _insertion_url(1), {"keep": True}),
        }
    )
    tokens["kept"] = insertion[-1]["response"]["lines"][0]["tokens"][4]
    insertion.append({"name": "undo the keep (one step)", **_call(client, "POST", undo)})
    insertion.append(
        {
            "name": "drop group 1 (lines 9101 and 9102)",
            **_call(client, "POST", _insertion_url(1), {"keep": False}),
        }
    )
    insertion.append(
        {
            "name": "drop group 2 (it fills line 9103: the line goes)",
            **_call(client, "POST", _insertion_url(2), {"keep": False}),
        }
    )
    insertion.append({"name": "undo the drop of group 2 (line 9103 is back)", **_call(client, "POST", undo)})
    out["insertion.json"] = insertion

    trust_book()
    gaps = []
    gaps.append(
        {"name": "accept gap 9500 with the offered words", **_call(client, "POST", _gap_url("accept", 9500))}
    )
    tokens["typed"] = gaps[-1]["response"]["line"]["tokens"][4]
    gaps.append({"name": "undo: the words go, gap 9500 is open again", **_call(client, "POST", undo)})
    gaps.append(
        {
            "name": "accept gap 9501 with typed words",
            **_call(client, "POST", _gap_url("accept", 9501), {"text": "  وقد   كان "}),
        }
    )
    gaps.append({"name": "dismiss gap 9500", **_call(client, "POST", _gap_url("dismiss", 9500))})
    gaps.append({"name": "undo: gap 9500 is open again", **_call(client, "POST", undo)})
    edited = "رحمه الله ٢٤٢ ثلاث واربعين ومايتين"
    gaps.append(
        {
            "name": "an edit of line 9102 re-anchors gap 9500 (after «الله»: index 1)",
            **_call(client, "POST", _line_url("line_edit", 9102), {"text": edited}),
        }
    )
    out["gaps.json"] = gaps

    trust_book()
    roles = []
    roles.append(
        {
            "name": "«حاشية» on a body-region line stores footnote",
            **_call(client, "POST", _line_url("line_role", 9100), {"role": "footnote"}),
        }
    )
    roles.append(
        {
            "name": "«محتوى» on a footnote-region line stores main",
            **_call(client, "POST", _line_url("line_role", 9104), {"role": "body"}),
        }
    )
    roles.append(
        {
            "name": "«حاشية» on that footnote-region line stores body again",
            **_call(client, "POST", _line_url("line_role", 9104), {"role": "footnote"}),
        }
    )
    roles.append(
        {
            "name": "«عنوان رئيسي» on a footnote-region line is allowed",
            **_call(client, "POST", _line_url("line_role", 9104), {"role": "heading"}),
        }
    )
    roles.append(
        {
            "name": "«شعر» on a body line",
            **_call(client, "POST", _line_url("line_role", 9102), {"role": "verse"}),
        }
    )
    roles.append(
        {
            "name": "a range: «حاشية» on lines 9101–9103",
            **_call(
                client,
                "POST",
                reverse("api:page_roles", args=[900]),
                {"line_ids": [9101, 9102, 9103], "role": "footnote"},
            ),
        }
    )
    roles.append({"name": "undo the range (one step)", **_call(client, "POST", undo)})
    out["roles.json"] = roles

    trust_book()
    approve = reverse("api:page_approve", args=[900])
    out["approve.json"] = [
        {"name": "open items left: 409", **_call(client, "POST", approve, {"force": False})},
        {"name": "forced approval", **_call(client, "POST", approve, {"force": True})},
    ]

    trust_book()
    TextGap.objects.filter(pk=9501).update(status=TextGap.Status.DISMISSED)
    errors = [
        {"name": "an unknown group", **_call(client, "POST", _insertion_url(7), {"keep": True})},
        {"name": "a decided gap", **_call(client, "POST", _gap_url("accept", 9501))},
        {"name": "an unknown gap", **_call(client, "POST", _gap_url("dismiss", 99999))},
        {
            "name": "typed words that are blank",
            **_call(client, "POST", _gap_url("accept", 9500), {"text": "  "}),
        },
        {"name": "an unknown role", **_call(client, "POST", _line_url("line_role", 9100), {"role": "main"})},
        {
            "name": "a range without lines",
            **_call(client, "POST", reverse("api:page_roles", args=[900]), {"line_ids": [], "role": "verse"}),
        },
        {
            "name": "a range with another page's line",
            **_call(
                client,
                "POST",
                reverse("api:page_roles", args=[900]),
                {"line_ids": [9100, 9110], "role": "verse"},
            ),
        },
    ]
    out["errors.json"] = errors

    trust_book()
    book = Book.objects.get(pk=TRUST_BOOK)
    one = Page.objects.get(pk=901)
    sheets = books_services.book_sheets(book, 1, 1)
    out["tile.json"] = {
        "page_tile (page 901, full)": _normal(books_services.page_tile(one)),
        "page_tile (page 901, compact)": _normal(books_services.page_tile(one, compact=True)),
        "page_tile (page 900, compact: no readers key for two)": _normal(
            books_services.page_tile(Page.objects.get(pk=900), compact=True)
        ),
        "filmstrip": _call(client, "GET", reverse("api:book_filmstrip", args=[TRUST_BOOK]))["response"],
        "book_sheets line (page 900, line 9104)": _normal(sheets["pages"][0]["lines"][4]),
        "attention flags": {
            code: label
            for code, label in (("single_reader", "قراءة واحدة"), ("missing_text", "نص قد يكون ناقصًا"))
        },
    }
    out["tokens.json"] = tokens
    return out


def test_trust_payloads_equal_the_fixtures(reviewer_client):
    import os

    contract = trust_contract(reviewer_client)
    if os.environ.get("NASSAKH_WRITE_TRUST_FIXTURES"):
        for name, content in contract.items():
            (TRUST_DIR / name).write_text(
                json.dumps(without_7c(content), ensure_ascii=False, indent=1) + "\n"
            )
    index = json.loads((TRUST_DIR / "index.json").read_text())
    assert set(index["files"]) == set(contract)
    for name, content in contract.items():
        assert json.loads((TRUST_DIR / name).read_text()) == without_7c(json.loads(json.dumps(content))), name


def without_7c(value):
    """A 7b answer without what 7c added (`next_step`, `elsewhere`, the tile's `primary_url`, `nav.back` /
    `origin` / `detour`; their contract is editor/fixtures/contract/), so the 7b contract keeps its shape."""
    if isinstance(value, dict):
        out = {
            k: without_7c(v) for k, v in value.items() if k not in ("next_step", "elsewhere", "primary_url")
        }
        if isinstance(out.get("nav"), dict):
            out["nav"] = {k: v for k, v in out["nav"].items() if k not in ("back", "origin", "detour")}
        if isinstance(out.get("book"), dict) and "unresolved_total" in out["book"]:
            out["book"] = {k: v for k, v in out["book"].items() if k != "edited"}
        if isinstance(out.get("urls"), dict) and "roles" in out["urls"]:
            fix = ("occurrences", "fix_everywhere", "fix_everywhere_undo")
            out["urls"] = {k: v for k, v in out["urls"].items() if k not in fix}
        return out
    if isinstance(value, list):
        return [without_7c(v) for v in value]
    return value


# ---------------------------------------------------------------- 7b: the services behind the contract


@pytest.fixture
def trust(db):
    trust_book()
    return Page.objects.get(pk=900)


def gap(pk):
    from ocr.models import TextGap

    return TextGap.objects.get(pk=pk)


def texts(page) -> list[str]:
    return [line.text for line in lines_of(page)]


def test_accept_gap_inserts_the_words_and_undo_takes_them_out_and_reopens_it(trust, reviewer):
    line, accepted = services.accept_gap(gap(9500), user=reviewer)
    assert accepted.status == "inserted" and accepted.decided_by == reviewer and accepted.decided_at
    assert line.text == "ونشأ بها رحمه الله تعالى الاجابة ٢٤٢ ثلاث واربعين ومايتين"
    assert [t["res"] for t in line.tokens[4:6]] == ["typed", "typed"]
    revision = trust.revisions.get()
    assert revision.action == "edit" and revision.after["gap"] == 9500
    assert reload(trust).n_unresolved == 13 and "تعالى الاجابة" in trust.final_text
    services.undo_last(trust, reviewer)
    reopened = gap(9500)
    assert (reopened.status, reopened.decided_by, reopened.decided_at) == ("open", None, None)
    assert Line.objects.get(pk=9102).text == "ونشأ بها رحمه الله ٢٤٢ ثلاث واربعين ومايتين"
    assert reload(trust).n_unresolved == 14


def test_accept_gap_takes_typed_words_and_re_anchors_by_the_word_it_follows(trust):
    line = Line.objects.get(pk=9102)
    tokens = [dict(t) for t in line.tokens]
    Line.objects.filter(pk=9102).update(tokens=[{"t": "بدء", "conf": "high"}, *tokens])  # indices moved
    line, _ = services.accept_gap(gap(9500), "  كما   قال ")
    words = line.text.split()
    assert words[words.index("الله") + 1 : words.index("الله") + 3] == ["كما", "قال"]
    assert services.gap_anchor([{"t": "أ"}, {"t": "ب"}], 5, "غائب") == 1
    assert services.gap_anchor([{"t": "أ"}], -1, "") == -1


def test_dismiss_gap_leaves_the_text_and_undo_reopens_it(trust, reviewer):
    before = reload(trust).final_text
    _, dismissed = services.dismiss_gap(gap(9500), reviewer)
    assert dismissed.status == "dismissed" and reload(trust).final_text == before
    assert trust.revisions.get().action == LineRevision.Action.GAP and reload(trust).n_unresolved == 13
    with pytest.raises(services.ReviewError, match="حُسم هذا النص المقترح"):
        services.dismiss_gap(gap(9500))
    services.undo_last(trust)
    assert gap(9500).status == "open" and reload(trust).n_unresolved == 14


def test_line_actions_move_the_gaps_with_their_words_and_undo_puts_them_back(trust):
    line = Line.objects.get(pk=9102)  # gap 9500 after «الله» (index 3)
    services.delete_token(line, 0)  # «ونشأ» goes
    assert (gap(9500).index, gap(9500).after_t) == (2, "الله")
    services.merge_tokens(Line.objects.get(pk=9102), 1)  # «رحمه» + «الله»
    assert (gap(9500).index, gap(9500).after_t) == (1, "رحمهالله")
    services.edit_line(Line.objects.get(pk=9102), "قال رحمهالله ٢٤٢")
    assert (gap(9500).index, gap(9500).after_t) == (1, "رحمهالله")
    for _ in range(3):
        services.undo_last(trust)
    assert (gap(9500).index, gap(9500).after_t) == (3, "الله")


def test_a_deleted_line_takes_its_gaps_out_of_the_counts_and_undo_brings_them_back(trust):
    services.delete_line(Line.objects.get(pk=9102))
    assert gap(9500).line_id is None and services.page_counts(reload(trust))["gaps"] == 1
    services.undo_last(trust)
    assert gap(9500).line_id == 9102 and gap(9500).status == "open"
    assert services.page_counts(reload(trust))["gaps"] == 2


def test_keeping_a_group_over_two_lines_and_undoing_it_in_one_step(trust, reviewer):
    result = services.resolve_insertion(trust, 1, True, reviewer)
    assert [line.pk for line in result["lines"]] == [9101, 9102] and result["deleted_ids"] == []
    group = [t for pk in (9101, 9102) for t in Line.objects.get(pk=pk).tokens if t.get("ins") == 1]
    assert len(group) == 4 and all(t["res"] == "secondary" for t in group)
    batch = {r.batch for r in trust.revisions.all()}
    assert len(batch) == 1 and None not in batch and trust.revisions.count() == 2
    services.undo_last(trust, reviewer)
    group = [t for pk in (9101, 9102) for t in Line.objects.get(pk=pk).tokens if t.get("ins") == 1]
    assert all(t.get("res") is None for t in group) and reload(trust).n_unresolved == 14


def test_dropping_groups_removes_their_words_and_a_line_they_fill(trust):
    services.resolve_insertion(trust, 1, False)
    assert texts(trust)[1:3] == ["وكان مилادية ※ ١٩٦٦", "رحمه الله ٢٤٢ ثلاث واربعين ومايتين"]
    result = services.resolve_insertion(trust, 2, False)
    assert result["deleted_ids"] == [9103] and [line.order for line in lines_of(trust)] == [0, 1, 2, 3]
    services.undo_last(trust)
    assert texts(trust)[3] == "وفيها مغاص اللؤلؤ المعروف بالخاركي،"
    services.undo_last(trust)
    assert texts(trust)[1].endswith("تعالى بطرابلس") and texts(trust)[2].startswith("ونشأ بها")
    with pytest.raises(services.ReviewError, match="لم تعد هذه الكلمات المقترحة"):
        services.resolve_insertion(trust, 9, True)


def test_roles_take_the_effective_choice_on_both_region_kinds(trust):
    body, foot = Line.objects.get(pk=9100), Line.objects.get(pk=9104)
    assert services.stored_role("footnote", "body") == "footnote"
    assert services.stored_role("footnote", "footnote") == "body"
    assert services.stored_role("body", "footnote") == "main"
    assert services.stored_role("verse", "footnote") == "verse"
    assert services.set_line_role(body, "footnote").role == "footnote"
    assert "قال يحيى" in reload(trust).final_text.split("\n\n")[1]  # the line is among the notes now
    assert services.set_line_role(foot, "body").role == "main"
    assert "(1) انظر" in reload(trust).final_text.split("\n\n")[0]  # pulled into the body
    assert services.set_line_role(foot, "footnote").role == "body"
    assert services.set_line_role(foot, "heading").role == "heading"  # no refusal any more (D74)
    assert services.set_line_role(Line.objects.get(pk=9102), "verse").role == "verse"


def test_set_roles_on_a_range_is_one_undo_step(trust, reviewer):
    lines = services.set_roles(trust, [9101, "9102", 9103, 9104], "footnote", reviewer)
    assert [line.role for line in lines] == ["footnote", "footnote", "footnote", "body"]  # 9104 unchanged
    assert trust.revisions.count() == 3 and len({r.batch for r in trust.revisions.all()}) == 1
    services.undo_last(trust, reviewer)
    assert [line.role for line in lines_of(trust)] == ["body"] * 5
    for bad in ([], [9101, "x"], [9101, 9110], "9101"):
        with pytest.raises(services.ReviewError):
            services.set_roles(trust, bad, "footnote")
    with pytest.raises(services.ReviewError, match="نوع السطر غير معروف"):
        services.set_roles(trust, [9101], "main")


def test_approve_counts_open_groups_and_gaps(trust, reviewer):
    with pytest.raises(services.ReviewBlocked) as exc:
        services.approve_page(trust, reviewer)
    assert (exc.value.items.words, exc.value.items.groups, exc.value.items.gaps) == (10, 2, 2)
    assert (
        str(exc.value) == "بقيت 14 علامة: 10 كلمات غير محسومة وكلمات مقترحة لم تُحسم. اعتماد الصفحة رغم ذلك؟"
    )
    items = services.OpenItems(words=2, groups=0, gaps=1)
    assert services.blocked_message(items) == (
        "بقيت 3 علامات: كلمتان غير محسومتين وكلمات مقترحة لم تُحسم. اعتماد الصفحة رغم ذلك؟"
    )
    assert (
        services.blocked_message(services.OpenItems(words=4))
        == "بقيت 4 كلمة غير محسومة. اعتماد الصفحة رغم ذلك؟"
    )
    # the words only the second model read, kept: only the suggestions are left
    services.resolve_insertion(trust, 1, True)
    services.resolve_insertion(trust, 2, True)
    for line in lines_of(trust):
        for index, token in enumerate(line.tokens):
            if token.get("conf") == "low" and not token.get("res"):
                line = services.resolve_token(line, index, "primary")
    with pytest.raises(services.ReviewBlocked) as exc:
        services.approve_page(trust, reviewer)
    assert exc.value.unresolved == 2 and str(exc.value).startswith("بقيت علامتان: كلمات مقترحة لم تُحسم")
    services.dismiss_gap(gap(9500))
    services.dismiss_gap(gap(9501))
    assert services.approve_page(trust, reviewer)["status"] == "reviewed"


def test_resolve_with_the_year_from_the_words(trust):
    line = services.resolve_token(Line.objects.get(pk=9102), 4, "sug")
    assert line.tokens[4]["t"] == "٢٤٣" and line.tokens[4]["res"] == "sug" and line.tokens[4]["orig"] == "٢٤٢"
    with pytest.raises(services.ReviewError, match="لا توجد قراءة مقترحة"):
        services.resolve_token(line, 0, "sug")


# ====================================================================== 7c: origin, next step, fix everywhere

from editor.tests import (  # noqa: E402 - the round trip's books (editor/fixtures/contract/)
    BATCH,
    check_contract,
    round_trip_book,
)
from review import corrections  # noqa: E402


def nav_contract(client) -> dict:
    out = {}
    for label, query in (
        ("GET /books/40/review/2/?from=book&at=12", "?from=book&at=12"),
        ("GET /books/40/review/2/?from=book", "?from=book"),
        ("GET /books/40/review/2/?from=manuscript&block=p40021", "?from=manuscript&block=p40021"),
        ("GET /books/40/review/2/?from=export", "?from=export"),
        ("GET /books/40/review/2/", ""),
        ("GET /books/40/review/2/?from=elsewhere&at=x&block=zz (ignored)", "?from=elsewhere&at=x&block=zz"),
    ):
        response = client.get(reverse("review:page", args=[40, 2]) + query)
        assert response.status_code == 200
        out[label] = response.context["config"]["nav"]
    api = client.get(reverse("api:page_review", args=[4002]) + "?from=book&at=12").json()
    out["GET /api/pages/4002/review/?from=book&at=12 (the payload API takes the same parameters)"] = api[
        "nav"
    ]
    return out


def next_step_contract(user, client) -> dict:
    """The end-of-review panel in each state (book 47, three pages; book 40 for the approvals)."""
    from assembly import services as assembly_services
    from editor import services as editor_services

    variants = {}
    lines = {
        1: [(47011, "الفصل الأول", "heading"), (47012, "نص الصفحة الأولى.", "body")],
        2: [(47021, "نص الصفحة الثانية.", "body")],
        3: [(47031, "نص الصفحة الثالثة.", "body")],
    }
    book = Book.objects.create(pk=47, title="كتاب الخطوة التالية", status=Book.Status.REVIEWING)
    for number, rows in lines.items():
        page = make_page(book, number, status=Page.Status.REVIEWED, with_lines=False)
        for order, (line_id, words, role) in enumerate(rows):
            Line.objects.create(
                pk=line_id,
                page=page,
                order=order,
                text=words,
                ocr_text=words,
                role=role,
                tokens=[tok(w) for w in words.split()],
            )
    three = Page.objects.get(book=book, number=3)
    Line.objects.filter(pk=47031).update(
        tokens=[tok("نص"), tok("الصفحة", conf="low"), tok("الثالثة.")], n_low=1
    )
    services.resolve_token(Line.objects.get(pk=47031), 1, "primary", user=user)
    variants["assemble"] = services.next_step(book)
    Page.objects.filter(pk=three.pk).update(status=Page.Status.OCR_DONE)
    variants["last_page (the current page is the only one left)"] = services.next_step(book, three)
    Page.objects.filter(pk=three.pk).update(status=Page.Status.REVIEWED)
    assembly_services.start_assembly(book, user)
    variants["book"] = services.next_step(book)
    services.edit_line(Line.objects.get(pk=47021), "نص الصفحة الثانية مصححًا.", user)
    services.edit_line(Line.objects.get(pk=47031), "نص الصفحة الثالثة مصححًا.", user)
    variants["reassemble"] = services.next_step(book)
    chapter = editor_services.chapter_document(book, "h47011")
    chapter["content"]["content"][0]["content"][0]["text"] = "الفصل الأول محرَّرًا"
    editor_services.save_chapter(book, "h47011", chapter["content"], chapter["version"], user)
    variants["changes"] = services.next_step(book)
    for number in (4, 5, 6):
        make_page(book, number, status=Page.Status.LAYOUT_DONE, with_lines=False)
    variants["processing"] = services.next_step(book)
    # book 40: approving a page with a next page, then the last one (the origin comes along)
    round_trip_book(user)
    Page.objects.filter(pk=4007).update(status=Page.Status.OCR_DONE)
    approve = {}
    response = client.post(
        reverse("api:page_approve", args=[4006]), {"force": False}, content_type="application/json"
    )
    approve["POST /api/pages/4006/approve/ {force: false} (a next page)"] = {
        "request": {"force": False},
        "status": response.status_code,
        "response": response.json(),
    }
    body = {"force": False, "from": "book", "at": 12}
    response = client.post(reverse("api:page_approve", args=[4007]), body, content_type="application/json")
    approve["POST /api/pages/4007/approve/ {force: false, from: book, at: 12} (the last page)"] = {
        "request": body,
        "status": response.status_code,
        "response": response.json(),
    }
    Page.objects.filter(pk=4007).update(status=Page.Status.OCR_DONE)
    Page.objects.filter(pk=4006).update(status=Page.Status.OCR_DONE)
    payload = services.review_payload(Page.objects.get(pk=4006), user)
    return {
        "variants": variants,
        "review_payload": {"next_step (another page waits for review)": payload["next_step"]},
        "approve": approve,
    }


MUROOJ = {
    1: [
        (42011, "السعودي رأس الصفحة", "running_header", [None, None, None]),
        (42012, "وقد ذكر المؤرخ «السعودي» في كتابه مروج الذهب", "body", [None, None, None, "low"]),
    ],
    2: [(42021, "قال السعودي، وهو ثقة", "body", [None, None])],
    3: [(42031, "السعودي في أخبار الزمان", "body", ["primary"])],
}


def murooj_book() -> Book:
    """Book 42 «مروج الذهب»: «السعودي» four times on three pages (a running head on page 1, approved; a form
    the reviewer confirmed as it is on page 3)."""
    book = Book.objects.create(pk=42, title="مروج الذهب", status=Book.Status.REVIEWING)
    for number, rows in MUROOJ.items():
        page = Page.objects.create(
            pk=4200 + number,
            book=book,
            number=number,
            source_index=number - 1,
            status=Page.Status.REVIEWED if number == 1 else Page.Status.OCR_DONE,
            text_state=Page.TextState.FINAL,
            width=1000,
            height=1400,
        )
        pre = Preprocess.objects.create(page=page, output_width=1000, output_height=1400)
        pre.display_image.name = f"books/42/pages/{number}/display.png"
        pre.save()
        regions = {
            kind: Region.objects.create(page=page, kind=kind, bbox=[0, 0, 1000, 1400], order=n)
            for n, kind in enumerate(("running_header", "body"))
        }
        for order, (line_id, words, kind, marks) in enumerate(rows):
            y = 40 if kind == "running_header" else 120 + order * 80
            tokens = []
            for index, word in enumerate(words.split()):
                token = tok(word, bbox=[120 + index * 150, y, 260 + index * 150, y + 40])
                if index < len(marks) and marks[index] == "low":
                    token["conf"] = "low"
                if index < len(marks) and marks[index] == "primary":
                    token.update(conf="low", res="primary")
                tokens.append(token)
            Line.objects.create(
                pk=line_id,
                page=page,
                order=order,
                region=regions[kind],
                bbox=[100, y, 900, y + 40],
                text=words,
                ocr_text=words,
                tokens=tokens,
                n_low=0,
            )
    return book


def fix_contract(user, client) -> dict:
    from editor.models import Manuscript

    book = murooj_book()
    occurrences = {}
    url = reverse("api:book_occurrences", args=[42])
    query = "?q=السعودي&fold_alef=1&whole_word=1&match_tashkeel=0"
    response = client.get(url + query)
    occurrences["GET /api/books/42/occurrences/?q=السعودي&fold_alef=1&whole_word=1&match_tashkeel=0"] = {
        "status": response.status_code,
        "response": response.json(),
    }
    for label, q in (("… an empty query", "?q="), ("… more than one word", "?q=السعودي المؤرخ")):
        response = client.get(url + q)
        occurrences[label] = {"status": response.status_code, "response": response.json()}
    fix = {}
    resolve = {"index": 1, "choice": "typed", "text": "المسعودي،", "t": "السعودي،"}
    response = client.post(
        reverse("api:line_resolve", args=[42021]), resolve, content_type="application/json"
    )
    label = (
        "POST /api/lines/42021/resolve/ … the answer's `elsewhere` (other occurrences of the corrected form)"
    )
    fix[label] = {
        "request": resolve,
        "status": response.status_code,
        "response (keys added to today's answer)": {"elsewhere": response.json()["elsewhere"]},
    }
    services.undo_last(Page.objects.get(pk=4202), user)
    fix_url = reverse("api:fix_everywhere", args=[42])
    body = {
        "from": "السعودي",
        "to": "المسعودي",
        "match_tashkeel": False,
        "fold_alef": True,
        "whole_word": True,
        "picks": [
            {"line_id": 42012, "index": 3, "t": "«السعودي»"},
            {"line_id": 42021, "index": 1, "t": "السعودي،"},
            {"line_id": 42031, "index": 0, "t": "كلمة تغيّرت منذ فتح القائمة"},
        ],
    }
    response = client.post(fix_url, body, content_type="application/json")
    fix["POST /api/books/42/fix-everywhere/ (an unedited book)"] = {
        "request": body,
        "status": 200,
        "response": response.json(),
    }
    batch = response.json()["batch"]
    undo = client.post(reverse("api:fix_everywhere_undo", args=[42, batch]))
    fix["POST /api/books/42/fix-everywhere/<batch>/undo/ (every line still at the fix)"] = {
        "request": {},
        "status": undo.status_code,
        "response": undo.json(),
    }
    again = client.post(fix_url, {**body, "picks": body["picks"][:2]}, content_type="application/json").json()
    services.edit_line(Line.objects.get(pk=42021), "قال المسعودي، وهو ثقة ثبت", user)
    undo = client.post(reverse("api:fix_everywhere_undo", args=[42, again["batch"]]))
    fix["… a partial undo (a line changed after the fix keeps its newer text)"] = {
        "request": {},
        "status": undo.status_code,
        "response": undo.json(),
    }
    Manuscript.objects.create(book=book, document={"type": "doc", "content": []}, version=2, origin="editor")
    edited = {
        "from": "السعودي",
        "to": "المسعودي",
        "match_tashkeel": False,
        "fold_alef": True,
        "whole_word": True,
        "picks": [{"line_id": 42012, "index": 3, "t": "«السعودي»"}],
    }
    response = client.post(fix_url, edited, content_type="application/json")
    fix["… on an edited book (the review toast adds the book-side replace)"] = {
        "request": edited,
        "status": response.status_code,
        "response": response.json(),
    }
    errors = {}
    for label, payload in (
        ("no picks", {"from": "السعودي", "to": "المسعودي", "picks": []}),
        ("the same word", {"from": "السعودي", "to": "السعودي", "picks": body["picks"][:1]}),
    ):
        response = client.post(fix_url, payload, content_type="application/json")
        errors[label] = {"status": response.status_code, "response": response.json()}
    response = client.post(reverse("api:fix_everywhere_undo", args=[42, BATCH]))
    errors["an unknown batch"] = {"status": response.status_code, "response": response.json()}
    stranger = Client()
    stranger.force_login(role_user("visitor", None))
    response = stranger.post(fix_url, body, content_type="application/json")
    errors["a reader (no review role)"] = {"status": response.status_code, "response": response.json()}
    fix["… errors"] = errors
    return {"occurrences.json": occurrences, "fix_everywhere.json": fix}


def test_review_payloads_equal_the_7c_contract(db, monkeypatch):
    from editor import services as editor_services

    monkeypatch.setattr(editor_services, "_schedule", lambda book, chapter_id, version: None)
    editor = role_user("editor", "editor")
    client = Client()
    client.force_login(editor)
    round_trip_book(editor)
    contract = {"review_nav.json": nav_contract(client)}
    Book.objects.filter(pk=40).delete()
    contract["next_step.json"] = next_step_contract(editor, client)
    contract.update(fix_contract(editor, client))
    check_contract(contract)


def test_the_origin_is_validated_and_carried_by_every_review_url(db):
    assert services.parse_origin("book", "12", "p5") == {
        "from": "book",
        "at": 12,
        "block": "p5",
        "query": "from=book&at=12&block=p5",
    }
    assert services.parse_origin("elsewhere", "12") is None
    assert services.parse_origin("book", "x", "<b>") == {
        "from": "book",
        "at": None,
        "block": None,
        "query": "from=book",
    }
    assert services.parse_origin("book", 0)["at"] is None
    assert services.with_origin("/books/1/review/next/?after=2", services.parse_origin("export")) == (
        "/books/1/review/next/?after=2&from=export"
    )


def test_review_next_keeps_the_origin(db):
    editor = role_user("editor", "editor")
    client = Client()
    client.force_login(editor)
    book = murooj_book()
    response = client.get(reverse("review:next", args=[book.pk]) + "?from=book&at=3")
    assert response.status_code == 302 and response["Location"] == "/books/42/review/2/?from=book&at=3"


def test_fix_everywhere_keeps_approval_skips_stale_tokens_and_undoes_in_one_batch(db):
    user = role_user("editor", "editor")
    book = murooj_book()
    Page.objects.filter(pk=4202).update(status=Page.Status.ASSEMBLED)
    picks = [
        {"line_id": 42012, "index": 3, "t": "«السعودي»"},
        {"line_id": 42021, "index": 1, "t": "السعودي،"},
        {"line_id": 42031, "index": 0, "t": "غيرها"},
        {"line_id": 99999, "index": 0, "t": "السعودي"},
    ]
    result = corrections.fix_everywhere(book, "السعودي", "المسعودي", picks, user)
    assert result["applied"] == 2 and result["pages"] == [1, 2] and result["find_url"] is None
    assert [(s["line_id"], s["reason"]) for s in result["skipped"]] == [
        (99999, "changed"),
        (42031, "changed"),
    ]
    token = Line.objects.get(pk=42012).tokens[3]
    assert token["t"] == "«المسعودي»" and token["orig"] == "«السعودي»" and token["res"] == "typed"
    assert Line.objects.get(pk=42021).text == "قال المسعودي، وهو ثقة"
    assert Page.objects.get(pk=4201).status == Page.Status.REVIEWED  # approved stays approved
    assert Page.objects.get(pk=4202).status == Page.Status.REVIEWED  # assembled → reviewed (D36)
    revisions = LineRevision.objects.filter(batch=result["batch"])
    assert revisions.count() == 2 and set(revisions.values_list("action", flat=True)) == {"fix"}
    undone = corrections.undo_fix(book, result["batch"], user)
    assert undone["reverted"] == 2 and undone["kept"] == []
    assert Line.objects.get(pk=42012).tokens[3]["t"] == "«السعودي»"
    assert corrections.undo_fix(book, result["batch"], user)["reverted"] == 0  # nothing left to undo
    with pytest.raises(corrections.CorrectionNotFound):
        corrections.undo_fix(book, "not-a-batch", user)


def test_a_page_undo_reverts_its_share_of_a_fix(db):
    user = role_user("editor", "editor")
    book = murooj_book()
    picks = [
        {"line_id": 42012, "index": 3, "t": "«السعودي»"},
        {"line_id": 42021, "index": 1, "t": "السعودي،"},
    ]
    corrections.fix_everywhere(book, "السعودي", "المسعودي", picks, user)
    services.undo_last(Page.objects.get(pk=4202), user)
    assert Line.objects.get(pk=42021).text == "قال السعودي، وهو ثقة"
    assert Line.objects.get(pk=42012).tokens[3]["t"] == "«المسعودي»"  # the other page keeps its share


def test_occurrences_ignore_diacritics_fold_alef_and_match_parts_of_words_when_asked(db):
    book = murooj_book()
    Line.objects.filter(pk=42021).update(
        text="قال السَّعودي، والسعودي وهو ثقة",
        tokens=[tok("قال"), tok("السَّعودي،"), tok("والسعودي"), tok("وهو"), tok("ثقة")],
    )
    whole = corrections.find_occurrences(book, "السعودي", corrections.options_of({}))
    words = [line["word"] for page in whole["results"] for line in page["lines"]]
    assert "السَّعودي،" in words and "والسعودي" not in words and whole["total"] == 4
    part = corrections.find_occurrences(book, "السعودي", corrections.options_of({"whole_word": "0"}))
    assert part["total"] == 5
    strict = corrections.find_occurrences(book, "السعودي", corrections.options_of({"match_tashkeel": "1"}))
    assert strict["total"] == 3
    heads = [line for page in whole["results"] for line in page["lines"] if line["head"]]
    assert [line["line_id"] for line in heads] == [42011] and heads[0]["pick"] is False
    assert (
        corrections.corrected(
            "والسعودي",
            corrections.folded_form("السعودي", corrections.options_of({"whole_word": 0})),
            "المسعودي",
            corrections.options_of({"whole_word": 0}),
        )
        == "والمسعودي"
    )
    with pytest.raises(services.ReviewError):
        corrections.find_occurrences(book, "", corrections.options_of({}))


# ---------------------------------------------------------------- 7 review: a resolution before a suggestion


@pytest.mark.django_db
def test_a_resolution_before_a_suggestion_keeps_the_suggestion_after_that_word(reviewer):
    # review draws the ▏ after word `index`; the word before it resolved to another reading, «إدراج» must
    # still put the words there, not after another «الشيخ» of the line (`gap_anchor` re-anchors by `after_t`)
    from ocr.models import TextGap

    book = Book.objects.create(title="ك", status=Book.Status.READY_FOR_REVIEW)
    page = make_page(book, 1, with_lines=False)
    body = Region.objects.create(page=page, kind="body", bbox=[0, 0, W, 150], order=0)
    words = [tok("قال"), tok("الشيخ", "low", alt="الشيح", tess="الشيح"), tok("ثم"), tok("الشيخ"), tok("كذا")]
    line = make_line(page, 0, body, words)
    suggestion = TextGap.objects.create(page=page, line=line, index=1, after_t="الشيخ", text="رحمه الله")
    services.refresh_page_text(page)
    services.resolve_token(line, 1, "secondary", user=reviewer)
    suggestion.refresh_from_db()
    assert (suggestion.index, suggestion.after_t) == (1, "الشيح")
    services.undo_last(page, reviewer)  # undo puts the word and the suggestion's anchor back
    suggestion.refresh_from_db()
    assert (suggestion.index, suggestion.after_t) == (1, "الشيخ")
    services.resolve_token(Line.objects.get(pk=line.pk), 1, "secondary", user=reviewer)
    line, _ = services.accept_gap(TextGap.objects.get(pk=suggestion.pk), user=reviewer)
    assert line.text == "قال الشيح رحمه الله ثم الشيخ كذا"
