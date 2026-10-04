"""Search, quotation checking, passages and citations (D107, `research.services`), always within the caller's
books (`books.access`)."""

from __future__ import annotations

from urllib.parse import urlsplit

from django.http import Http404

import pytest

from editor.models import StyleSheet
from research import clips, index, services
from research.conftest import make_page

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def indexed(library):
    for book in (library["book"], library["other"], library["secret"]):
        index.reindex_book(book.pk)
    return library


# ====================================================================== printed page numbers


def test_printed_numbers_are_read_or_inferred_from_the_pages_around():
    rows = [(1, "11"), (2, "12"), (3, ""), (4, "41"), (5, "15"), (6, "16"), (7, "17")]
    got = services.infer_printed(rows)
    assert got[1] == ("11", "page")
    assert got[3] == ("13", "inferred")  # missing: the neighbours' offset
    assert got[4] == ("14", "inferred")  # misread: three or more neighbours agree on another
    assert services.infer_printed([(1, ""), (2, "")]) == {1: (None, "none"), 2: (None, "none")}


# ====================================================================== search


def test_phrase_search_finds_the_words_in_order_with_their_page_and_kind(reader, library):
    result = services.search(reader, "إنما الأعمال بالنيات")
    assert result.total == 1
    hit = result.hits[0]
    assert (hit.mode, hit.kind, hit.book.id, hit.page.number, hit.page.printed) == (
        "phrase",
        "body",
        library["book"].pk,
        1,
        "11",
    )
    assert hit.match == "إِنَّمَا الأَعْمَالُ بِالنِّيَّاتِ"  # as printed, diacritics kept
    assert hit.after.startswith("وإنما لكل امرئ")
    assert {w.state for w in hit.words} == {"unreviewed"}
    assert hit.citation.endswith("ص 11")
    assert urlsplit(hit.clip_url).path.startswith("/research/clip/")
    assert hit.review_url.endswith(f"/books/{library['book'].pk}/review/1/")


def test_all_words_and_fuzzy_come_after_phrases(reader):
    words = services.search(reader, "نافع مالك")  # both on the page, not in this order
    assert [h.mode for h in words.hits] == ["all_words"]
    fuzzy = services.search(reader, "عن عبد الله بن عمار أن رسول")  # «عمار» for «عمر»
    assert fuzzy.hits and fuzzy.hits[0].mode == "fuzzy" and fuzzy.hits[0].score >= 85
    assert "عمر" in fuzzy.hits[0].match


def test_the_kind_filter_keeps_the_body_and_the_notes_apart(reader):
    assert services.search(reader, "رواه البخاري", kind="notes").hits[0].kind == "notes"
    assert services.search(reader, "رواه البخاري", kind="body").total == 0
    assert services.search(reader, "رواه البخاري").hits[0].kind == "notes"


def test_a_phrase_over_a_page_break(reader, library):
    result = services.search(reader, "هذا الباب أن الصبر ضياء الصبر")
    hit = result.hits[0]
    assert hit.mode == "phrase"
    assert (hit.page.number, hit.end_page.number) == (5, 6)
    assert hit.page.printed == "15" and hit.page.printed_source == "inferred"  # page 5 prints no number


def test_search_never_returns_another_organisations_book(reader, stranger, library):
    mine = services.search(
        reader, "إنما الأعمال بالنيات", book_ids=[library["secret"].pk, library["book"].pk]
    )
    assert {h.book.id for h in mine.hits} == {library["book"].pk}
    assert services.search(reader, "سر مكتوم").total == 0
    theirs = services.search(stranger, "إنما الأعمال بالنيات")
    assert {h.book.id for h in theirs.hits} == {library["secret"].pk}


def test_paging_through_hits(reader, library):
    for n in range(7, 12):
        make_page(library["book"], n, ["تكرار العبارة نفسها في صفحات كثيرة"], printed=str(10 + n))
    first = services.search(reader, "العبارة نفسها", limit=2)
    second = services.search(reader, "العبارة نفسها", limit=2, offset=2)
    assert first.total == 5 and len(first.hits) == 2 and len(second.hits) == 2
    assert {h.page.number for h in first.hits}.isdisjoint({h.page.number for h in second.hits})


# ====================================================================== quotation checking


def test_exact(reader, library):
    result = services.verify_quote(reader, "إنما الأعمال بالنيات وإنما لكل امرئ ما نوى")
    assert result.status == "exact" and not result.diacritics_differ and not result.changes
    assert (result.book.id, result.page.printed, result.kind, result.attribution) == (
        library["book"].pk,
        "11",
        "body",
        "ok",
    )
    assert result.message == "النص مطابق لما في الكتاب."
    assert result.citation == "ابن أبي جمرة، مختصر صحيح البخاري، 1950، ص 11"
    assert result.clip_url and result.review_state == "unreviewed"


def test_exact_words_with_other_vowels(reader):
    result = services.verify_quote(reader, "إِنَّمَا الأَعْمَالِ بِالنِّيَّاتِ وإنما لكل")
    assert result.status == "exact" and result.diacritics_differ
    assert [(d.quote, d.source) for d in result.diacritics] == [("الأَعْمَالِ", "الأَعْمَالُ")]
    assert "يختلف الشكل" in result.message


def test_a_changed_confident_word_differs(reader):
    result = services.verify_quote(reader, "إنما الأعمال بالنيات وإنما لكل امرئ ما أراد")
    assert result.status == "differs"
    assert [(c.type, c.quote, c.source, c.source_state, c.needs_image) for c in result.changes] == [
        ("replaced", "أراد", "نوى", "unreviewed", False)
    ]
    assert [seg.op for seg in result.diff] == ["equal", "replaced"]


def test_a_changed_reviewed_word_differs(reader, library):
    make_page(library["book"], 30, ["وقال الحسن البصري الدنيا دار ممر لا دار مقر"], reviewed=True)
    result = services.verify_quote(reader, "وقال الحسن البصري الدنيا دار ممر لا دار قرار")
    assert result.status == "differs" and result.changes[0].source_state == "reviewed"
    assert result.review_state == "reviewed"


def test_a_difference_on_a_doubtful_reading_needs_the_image(reader, library):
    result = services.verify_quote(reader, "قال الشيخ رحمه الله في كتابه والعالم قبل القول والعمل")
    assert result.status == "needs_image_check"
    assert result.message.startswith("يحتاج مطابقة مع الصورة")
    change = result.changes[0]
    assert (change.quote, change.source, change.source_state, change.needs_image) == (
        "والعالم",
        "والعلم",
        "doubtful",
        True,
    )
    payload = clips.read_token(urlsplit(result.clip_url).path.split("/")[-2])
    assert payload["w"]  # the doubtful word is underlined in the clip


def test_missing_added_and_moved_words(reader):
    missing = services.verify_quote(reader, "إنما الأعمال بالنيات لكل امرئ ما نوى")
    assert missing.status == "differs" and [(c.type, c.source) for c in missing.changes] == [
        ("missing", "وإنما")
    ]
    added = services.verify_quote(reader, "إنما الأعمال بالنيات وإنما لكل امرئ مسلم ما نوى")
    assert [(c.type, c.quote) for c in added.changes] == [("added", "مسلم")]
    moved = services.verify_quote(reader, "إنما بالنيات الأعمال وإنما لكل امرئ ما نوى")
    assert [c.type for c in moved.changes] == ["moved"]


def test_a_quotation_over_a_page_break(reader):
    result = services.verify_quote(reader, "وكان آخر ما قاله في هذا الباب أن الصبر ضياء الصبر عند أول الصدمة")
    assert result.status == "exact"
    assert (result.page.number, result.end_page.number) == (5, 6)
    assert result.citation.endswith("ص 15–16") and len(result.clip_urls) == 2


def test_a_note_quoted_as_the_authors(reader):
    result = services.verify_quote(reader, "هذا الحديث رواه البخاري في أول صحيحه", attributed_to="author")
    assert result.status == "exact" and result.kind == "notes"
    assert result.attribution == "note_not_author"
    assert result.attribution_message == "هذا من حاشية المحقق لا من متن المؤلف."
    assert result.citation.endswith("ص 11 (الحاشية)")
    plain = services.verify_quote(reader, "هذا الحديث رواه البخاري في أول صحيحه")
    assert plain.attribution == "ok"


def test_found_only_in_another_book(reader, library):
    result = services.verify_quote(
        reader, "وجدنا هذه العبارة النادرة في كتاب آخر", book_id=library["book"].pk
    )
    assert result.status == "exact" and result.attribution == "other_book"
    assert result.book.id == library["other"].pk and "كتاب آخر" in result.attribution_message


def test_not_found_and_too_short(reader):
    absent = services.verify_quote(reader, "هذه جملة لا توجد في أي كتاب من كتب الحساب")
    assert absent.status == "not_found" and absent.message == "لم يوجد في كتب هذا الحساب."
    assert absent.book is None and absent.clip_url is None
    short = services.verify_quote(reader, "إنما الأعمال")
    assert short.status == "too_short"


def test_verify_sees_only_the_callers_books(reader, stranger, library):
    theirs = services.verify_quote(stranger, "إنما الأعمال بالنيات وإنما لكل امرئ ما نوى")
    assert theirs.book.id == library["secret"].pk
    assert services.verify_quote(reader, "سر مكتوم لا يراه غير أهله").status == "not_found"
    with pytest.raises(Http404):
        services.verify_quote(reader, "إنما الأعمال بالنيات", book_id=library["secret"].pk)


# ====================================================================== passages and citations


def test_get_passage_by_id_keeps_body_and_notes_apart(reader, library):
    hit = services.search(reader, "إنما الأعمال بالنيات").hits[0]
    passage = services.get_passage(reader, hit.passage_id, context=1)
    assert passage.text == hit.match and passage.kind == "body"
    assert [line.in_passage for line in passage.body] == [False, True]  # the line before, then the passage's
    assert [line.text for line in passage.notes][0].startswith("(١) هذا الحديث")
    assert passage.body[1].bbox and passage.page_size == [600, 900]
    assert passage.citation.endswith("ص 11")


def test_get_passage_of_a_whole_page_by_its_printed_number(reader, library):
    passage = services.get_passage(reader, book_id=library["book"].pk, printed_page="12")
    assert passage.page.number == 2 and passage.text.startswith("باب كيف كان بدء الوحي")
    with pytest.raises(Http404):
        services.get_passage(reader, book_id=library["secret"].pk, page=1)


def test_a_passage_of_another_organisation_is_not_found(reader, stranger):
    hit = services.search(reader, "إنما الأعمال بالنيات").hits[0]
    with pytest.raises(Http404):
        services.get_passage(stranger, hit.passage_id)
    with pytest.raises(Http404):
        services.cite(stranger, hit.passage_id)
    with pytest.raises(Http404):
        services.get_passage(reader, "not-a-passage")


def test_cite_uses_the_books_details(reader, library):
    StyleSheet.objects.create(
        book=library["book"],
        front_matter={"fields": {"editor": "محمد فؤاد", "publisher": "دار الكتب", "edition": "2"}},
    )
    library["book"].volume = "1"
    library["book"].save(update_fields=["volume"])
    hit = services.search(reader, "إنما الأعمال بالنيات").hits[0]
    citation = services.cite(reader, hit.passage_id)
    assert (
        citation.citation
        == "ابن أبي جمرة، مختصر صحيح البخاري، تحقيق: محمد فؤاد، دار الكتب، ط 2، 1950، ج 1، ص 11"
    )
    assert (citation.parts.page, citation.parts.scan_page, citation.parts.kind) == ("11", 1, "body")


def test_list_books(reader, library):
    result = services.list_books(reader)
    titles = {book.title for book in result.books}
    assert titles == {"مختصر صحيح البخاري", "كتاب آخر"}
    book = next(b for b in result.books if b.id == library["book"].pk)
    assert (book.pages, book.indexed_pages, book.printed_range, book.reviewed_share) == (6, 6, "11–16", 0.0)
