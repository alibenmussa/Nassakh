"""The Arabic normal form of search and quotation checking (D107, `research.normalize`)."""

from __future__ import annotations

import pytest

from research.normalize import (
    diacritics_differ,
    has_diacritics,
    is_marker,
    normalize_text,
    normalize_word,
    token_words,
    words_of,
)


@pytest.mark.parametrize(
    ("raw", "norm"),
    [
        ("قَالَ", "قال"),  # tashkeel
        ("الشَّيْخُ", "الشيخ"),
        ("كتابـــه", "كتابه"),  # tatweel
        ("أحمد", "احمد"),
        ("إسلام", "اسلام"),
        ("آمن", "امن"),
        ("ٱلرحمن", "الرحمن"),  # alef wasla
        ("مصطفى", "مصطفي"),  # alef maqsura
        ("الصلاة", "الصلاه"),  # ta marbuta
        ("مؤمن", "مومن"),
        ("قائل", "قايل"),
        ("١٩٦٦", "1966"),  # Arabic-Indic digits
        ("۱۹۶۶", "1966"),  # Persian digits
        ("«الصلاة»،", "الصلاه"),  # punctuation and brackets
        ("ﻻ", "لا"),  # a presentation form
        ("کتاب", "كتاب"),  # Persian kaf
        ("وٰلك", "ولك"),  # superscript alef
    ],
)
def test_a_word_takes_its_normal_form(raw, norm):
    assert normalize_word(raw) == norm or [w.norm for w in token_words(raw)] == [norm]


def test_a_text_keeps_its_words_and_their_raw_forms():
    words = words_of("قَالَ الشَّيْخُ: «الصلاةُ» عِمادُ الدين.")
    assert [w.norm for w in words] == ["قال", "الشيخ", "الصلاه", "عماد", "الدين"]
    assert [w.raw for w in words][:2] == ["قَالَ", "الشَّيْخُ"]  # diacritics kept, punctuation gone
    assert normalize_text("  قال   الشيخ  ") == "قال الشيخ"


def test_punctuation_inside_a_token_splits_it_and_a_lone_mark_is_no_word():
    assert [w.norm for w in words_of("قال:حدثنا — مالك")] == ["قال", "حدثنا", "مالك"]


def test_footnote_marks_are_apparatus_but_numbers_stay():
    assert is_marker("(١)") and is_marker("[2]") and is_marker("٣)") and is_marker("(١٢).")
    assert not is_marker("(١٣٤٢٨)") and not is_marker("1966")
    assert [w.norm for w in words_of("قال الشيخ (١) رحمه الله")] == ["قال", "الشيخ", "رحمه", "الله"]
    assert [w.norm for w in words_of("الشيخ(١) رحمه")] == ["الشيخ", "رحمه"]  # a glued mark is cut off
    assert [w.norm for w in words_of("سنة ١٩٦٦م (١٣٤٢٨)")] == ["سنه", "1966م", "13428"]


def test_a_ligature_of_several_words_stays_one_word():
    assert [w.norm for w in words_of("النبي ﷺ قال")] == ["النبي", "ﷺ", "قال"]


def test_diacritics_differ_only_when_both_are_vowelled_and_differ():
    assert diacritics_differ("عَلِمَ", "عُلِمَ")
    assert not diacritics_differ("عَلِمَ", "عَلِمَ")
    assert not diacritics_differ("علم", "عُلِمَ")  # the quotation has no vowels
    assert not diacritics_differ("عَلِمَ", "علم")  # the book has none
    assert not diacritics_differ("أَمَرَ", "أَمَرَ")
    assert has_diacritics("قَالَ") and not has_diacritics("قال")
