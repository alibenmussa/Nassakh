"""Tests of the numbers pass (D50, D51): Kraken reads the Arabic-Indic numbers of a finalised page,
and the numbers Qari wrote as letters.

The pure parts (`ocr.numbers`: the book's digits, the areas, matching Kraken's digit runs, the token)
and the runner's helpers run without Kraken; the pass runs with a fake engine that answers each area
from a table, and the real engine only on its error path (the project's Python has no Kraken).
"""

from __future__ import annotations

import sys

from django.test import override_settings

import numpy as np
import pytest

from books.models import Book, Page
from core.storage import save_array
from ocr import numbers as nb
from ocr.engines import kraken_runner
from ocr.engines.kraken import KrakenEngine, KrakenError
from ocr.models import Line, OcrRun
from processing.models import Preprocess

# ---------------------------------------------------------------- the book's printed digits


def tok(t, alt=None, bbox=None, **extra):
    return {
        "t": t,
        "alt": alt,
        "tess": None,
        "conf": "low",
        "digit": True,
        "bbox": bbox,
        "res": None,
        **extra,
    }


def test_a_book_printing_arabic_indic_digits_is_told_from_qaris_readings():
    # the secondary model writes them Arabic-Indic (90–100 % in such books) ...
    arabic = [tok("1328", alt="٢٢٢٩"), tok("91328هـ", alt="٩٢٢ه"), tok("28", alt="٢٣")]
    assert nb.style_of(nb.digit_counts(arabic)) == nb.ARABIC_INDIC
    # ... or the primary does often enough
    primary = [tok("١٢٣٤"), tok("2020"), tok("٥٦٧")]
    assert nb.style_of(nb.digit_counts(primary)) == nb.ARABIC_INDIC
    western = [tok("1938", alt="1938"), tok("1964م"), tok("(726هـ)", alt="(726ه)")]
    assert nb.style_of(nb.digit_counts(western)) == nb.WESTERN
    assert nb.style_of(nb.digit_counts([tok("12")])) == ""  # too few digits to tell
    words = [{"t": "كتاب", "alt": "١٢٣", "digit": False}]  # only number tokens count
    assert nb.digit_counts(words) == (0, 0, 0, 0)


# ---------------------------------------------------------------- where the numbers are


def test_areas_are_the_word_box_or_the_gap_between_boxed_neighbours():
    # right to left: token 0 is the rightmost word
    tokens = [
        {"t": "ولد", "bbox": [900, 10, 980, 40]},
        {"t": "سنة", "bbox": [800, 12, 880, 40]},
        tok("٢٧٩هـ", bbox=[700, 14, 780, 38]),
        {"t": "في", "bbox": [640, 12, 690, 40]},
        tok("١٩٦٨م"),  # no box: between «في» (x0 640) on its right and «بغداد» (x1 590) on its left
        tok("٣٠٩هـ"),  # same gap
        {"t": "بغداد", "bbox": [500, 10, 590, 40]},
        tok("٤"),  # no box, nothing boxed on its left: up to the line's edge
    ]
    areas = nb.number_areas(tokens, [100, 5, 1000, 45])
    assert [(a.bbox, a.tokens) for a in areas] == [
        ([700, 14, 780, 38], [2]),
        ([590, 5, 640, 45], [4, 5]),
        ([100, 5, 500, 45], [7]),
    ]


# ---------------------------------------------------------------- Kraken's digits for them


def chars(text, x=0):
    return [[c, float(x + i), float(x + i + 1), 0.9] for i, c in enumerate(text)]


def test_kraken_numbers_go_to_the_areas_numbers_in_order_when_the_counts_agree():
    area = [tok("(٢٢٢٢–٢٢٢٢هـ)."), tok("٢٢")]
    assert nb.assign(area, chars("(٣٢٠–٣٢٢هـ). ٣٣٢")) == [["٣٢٠", "٣٢٢"], ["٣٣٢"]]
    # stray spaces inside a number are closed up when that makes the counts agree
    assert nb.assign([tok("٢٠٠٠م.")], chars("٢٠ ٠ ٠م.")) == [["٢٠٠٠"]]
    # counts that cannot agree: Qari's reading stays
    assert nb.assign([tok("١٢")], chars("١٢ و ٣٤")) is None
    assert nb.box_text(chars("ص۲۳- ۲٤).")) == "ص۲۳- ۲٤)." and nb.box_text(chars("قال")) == ""


def test_krakens_persian_six_is_the_pages_four_and_the_rest_go_by_value():
    assert "٧٥۶ ۲۶٠ 19".translate(nb.KRAKEN_DIGITS) == "٧٥٤ ٢٤٠ ١٩"


def test_numbers_of_one_area_come_in_the_pages_order_when_that_agrees_better_with_qari():
    # «، ٥٢ ، ٥٣ .» read without its spaces: «،٥٣،٥٢.» (BiDi kept the pair left to right); on the page
    # ٥٢ is on the right (x 368–385), ٥٣ on the left (x 295–308); Qari's «٢» and «٣» side with the page
    read = [
        ["،", 417, 420, 1],
        ["٥", 295, 300, 1],
        ["٣", 303, 308, 1],
        ["،", 339, 342, 1],
        ["٥", 368, 373, 1],
        ["٢", 380, 385, 1],
        [".", 266, 268, 1],
    ]
    assert nb.assign([tok("2"), tok("3")], read) == [["٥٢"], ["٥٣"]]
    # a pair set left to right on the page too («٨/٣٨»): Qari agrees with Kraken's order, which stays
    pair = [["٨", 100, 105, 1], ["/", 106, 108, 1], ["٣", 110, 115, 1], ["٨", 116, 121, 1]]
    assert nb.assign([tok("٨/٣٨")], pair) == [["٨", "٣٨"]]
    # no telling (Qari's digits match neither): Kraken's order
    assert nb.assign([tok("٧"), tok("٩")], read) == [["٥٣"], ["٥٢"]]


# ---------------------------------------------------------------- the token


def test_the_token_takes_krakens_digits_and_keeps_qaris_brackets_and_letters():
    token = tok("(9هـ/1241م)،", alt="(943-1543هـ/199م)،", tess="(@YV4")
    assert nb.apply_reading(token, ["345", "956"])
    assert token["t"] == "(٣٤٥هـ/٩٥٦م)،"  # Kraken's digits, written Arabic-Indic; the rest is Qari's
    assert token["src"] == "kraken" and token["alt"] is None and token["tess"] is None
    assert token["qari"] == {"t": "(9هـ/1241م)،", "alt": "(943-1543هـ/199م)،", "tess": "(@YV4"}
    assert token["conf"] == "low" and token["digit"] is True  # the reviewer confirms it (D17)
    assert not nb.apply_reading(token, ["1"])  # read once
    # Qari held a fragment of the box: the token takes Kraken's reading of the whole box
    fragment = tok("١،", bbox=[1, 2, 3, 4])
    assert nb.apply_reading(fragment, whole="ص۲۳- ۲٤).") and fragment["t"] == "ص٢٣- ٢٤)."
    resolved = tok("١٢", res="typed")
    assert not nb.apply_reading(resolved, ["٣٤"]) and resolved["t"] == "١٢"  # the reviewer's word stays
    mismatch = tok("١٢ و ٣٤")
    assert not nb.apply_reading(mismatch, ["٥"]) and "src" not in mismatch


# ---------------------------------------------------------------- numbers Qari wrote as letters (D51)


def word(t, bbox=None, **extra):
    return {
        "t": t,
        "alt": None,
        "tess": None,
        "conf": "high",
        "digit": False,
        "bbox": bbox,
        "res": None,
        **extra,
    }


def test_lone_letters_that_look_like_digits_and_digitless_dates_are_found():
    for text in ("ا", "ا،", "(ه)", "(هـ)", "ع", "«ا»", "ه."):
        assert nb.is_letter_digit(word(text)), text
    for text in ("(د)", "اب", "هو", "١", "(٥)", "ج"):
        assert not nb.is_letter_digit(word(text)), text
    assert not nb.is_letter_digit(word("ا", res="typed"))  # the reviewer's word stays
    # «هـ» after a year is its era sign, not a «٥» (book 29: «سنة ٢٩ هـ» became «٢٩٩ ٨»)
    for year in ("29", "٢٩", "٤٥", "(٧٧٥"):
        line = [word("سنة"), word(year), word("هـ"), word("من")]
        assert nb.is_era_sign(line, 2), year
    assert nb.is_era_sign([word("٥٢"), word("ه.")], 1)
    assert not nb.is_era_sign([word("سنة"), word("ه")], 1)  # no year before it: a letter for «٥»
    assert not nb.is_era_sign([word("(ه)"), word("الخزر")], 0)
    assert not nb.is_era_sign([word("٢٩"), word("ع")], 1)  # «ع» for «٤» stays a candidate
    line = [word(t) for t in ("المنصور", "(ع", "ه", "–", "ه", "م)", "بأيدي", "(هـ", "–", "م)", "و", "(٧٧٥م)")]
    assert nb.digitless_dates(line) == [(1, 5), (7, 9)]  # «(٧٧٥م)» has its digits
    assert nb.digitless_dates([word("(الكتاب"), word("الأبيض)")]) == []  # a dash is needed, and «م»/«هـ»
    # words, not marks, between the brackets: real text
    for text in ("(كتابه – شرحه)", "(الإسلام – السلام)", "(الله / رسوله)"):
        assert nb.digitless_dates([word(t) for t in text.split()]) == [], text
    # an unclosed «(» before a date: the date alone, never the words before it
    unclosed = [word(t) for t in ("قال", "(ابن", "سينا", "(هـ", "–", "م)")]
    assert nb.digitless_dates(unclosed) == [(3, 5)]


def test_readings_are_compared_on_skeletons():
    assert nb.skeleton("إلى") == nb.skeleton("إلي") == nb.skeleton("الی") == "الي"  # ى, ي, Persian ی
    assert nb.skeleton("کتاب") == "كتاب" and nb.skeleton("(ج۱، ص٢٣٨).") == "ج١ص٢٣٨"
    assert nb.anchored_number("ذهب إلي١ المدينة", [word("إلى"), word("ا"), word("المدينة")], 1) == "١"


def test_a_letter_takes_the_number_kraken_read_between_qaris_neighbours_in_the_line():
    tokens = [word("وأكرههم»"), word("(ج"), word("ا،"), word("ص"), tok("١٩١)،")]
    line = "في مملكته وأكرههم» (ج۱، ص ۱٩١)، فهرب"
    assert nb.anchored_number(line, tokens, 2) == "١"
    # a footnote mark at the line's start, and one inside the text
    assert nb.anchored_number("١١ أي من كان", [word("ا"), word("أي"), word("من")], 0) == "١١"
    assert nb.anchored_number("الجوارح۱١ ولا فاحش", [word("الجوارح"), word("ا"), word("ولا")], 1) == "١١"
    # Latin neighbours anchor too; the letters around must be found once
    assert nb.anchored_number("برقة ٥aae »١ .", [word("Barcae"), word("»"), word("ا"), word(".")], 2) == "١"
    assert nb.anchored_number("قال ١ قال ٢ قال", [word("قال"), word("ا"), word("قال")], 1) == ""
    assert (
        nb.anchored_number("سنة ٢١ هـ بقيادة", [word("سنة"), tok("21"), word("ه"), word("بقيادة")], 2) == ""
    )


def test_a_letter_in_its_own_area_takes_the_one_number_there_when_the_rest_agrees_with_qari():
    assert nb.box_number(chars("(٥)"), "()") == "٥"
    assert nb.box_number(chars("ص۲۳۸)."), "،") == ""  # the box of the next word: not the letter's
    assert nb.box_number(chars("١ و"), "") == ""
    assert nb.shows_letter(chars("(هـ)"), "(ه)") and not nb.shows_letter(chars("(٥)"), "(ه)")
    assert not nb.shows_letter([], "(ه)")
    # a letter without a box, alone between boxed words: its gap read alone vetoes the line's reading
    line = nb.LineLetters(
        line=type(
            "L",
            (),
            {"pk": 1, "tokens": [word("علي", [60, 0, 90, 10]), word("(ع)"), word("قال", [0, 0, 30, 10])]},
        )(),
        letters=[1],
        dates=[],
        areas={1: ([30, 0, 60, 10], "()", False)},
    )
    by_id = {"1:line": {"text": "علي (۶) قال"}, "1:letter1": {"chars": chars("(ع)")}}
    assert nb.read_letters(line, by_id) == (0, 0) and line.line.tokens[1]["t"] == "(ع)"
    # its own area: the box, else a gap holding nothing but punctuation besides it
    tokens = [word("جياد", [60, 0, 90, 10]), word("»"), word("ا"), word("."), word("فهاته", [0, 0, 30, 10])]
    assert nb.letter_area(tokens, 2, [0, 0, 100, 10]) == ([30, 0, 60, 10], "».", False)
    boxed = [word("(ه)", [80, 0, 95, 10])]
    assert nb.letter_area(boxed, 0, [0, 0, 100, 10]) == ([80, 0, 95, 10], "()", True)
    crowded = [word("قال", [60, 0, 90, 10]), word("ا"), word("كذا"), word("فهاته", [0, 0, 30, 10])]
    assert nb.letter_area(crowded, 1, [0, 0, 100, 10]) is None


def test_the_letter_becomes_krakens_number_and_qaris_letter_stays_the_second_reading():
    token = word("(ه)", tess=")0(")
    assert nb.apply_letter(token, "٥")
    assert token["t"] == "(٥)" and token["alt"] == "(ه)" and token["tess"] is None
    assert token["src"] == "kraken" and token["conf"] == "low" and token["digit"] is True
    assert token["qari"] == {"t": "(ه)", "alt": None, "tess": ")0("}
    assert not nb.apply_letter(token, "٦")  # read once
    footnote = word("ا،")
    assert nb.apply_letter(footnote, "۱") and footnote["t"] == "١،"
    bold = word("ه")
    assert nb.apply_letter(bold, "0") and bold["t"] == "٥"  # Kraken's zero where Qari saw ه: a circle, ٥
    assert not nb.apply_letter(word("ا"), "")


def test_a_digitless_date_becomes_krakens_date_between_the_same_neighbours():
    tokens = [word("المنصور"), word("(ع"), word("ه"), word("–"), word("ه"), word("م)"), word("بأيدي")]
    line = "في عهد الخليفة أبي جعفر المنصور (٧٥۶- ٧٧٥م) بأيدي مئة"
    reading = nb.date_reading(line, tokens, 1, 5)
    assert reading == "(٧٥٤–٧٧٥م)"  # Kraken's digits (its ۶ the page's ٤), Qari's dash
    token = nb.replace_date(tokens, 1, 5, reading)
    assert [t["t"] for t in tokens] == ["المنصور", "(٧٥٤–٧٧٥م)", "بأيدي"]
    assert token["qari"]["t"] == "(ع ه – ه م)" and token["src"] == "kraken"
    assert token["alt"] == "(ع ه – ه م)"  # Qari's reading stays offered, second
    # what Qari read before the «(» and after the «)» stays
    tokens = [word("المتوكل"), word("(هـ"), word("–"), word("م)،"), word("كان")]
    reading = nb.date_reading("فإن المتوكِّل (٨٤٧-٨٦١م)، كان", tokens, 1, 3)
    nb.replace_date(tokens, 1, 3, reading)
    assert [t["t"] for t in tokens] == ["المتوكل", "(٨٤٧–٨٦١م)،", "كان"]
    # read in its own area: the one date there, the hijri mark with its tatweel
    tokens = [word("سنة", [80, 0, 95, 10]), word("(هـ"), word("/"), word("م)"), word("توفي", [0, 0, 20, 10])]
    assert nb.date_area(tokens, 1, 3, [0, 0, 100, 10]) == [20, 0, 80, 10]
    assert nb.date_reading("", tokens, 1, 3, "ة (٢٥٥ه/٨٦٩م) ت") == "(٢٥٥هـ/٨٦٩م)"
    assert nb.date_reading("", tokens, 1, 3, "(١-٢م) (٣-٤م)") == ""  # two dates there: no telling
    # a gap shared with a number or a bracket could hold another date: no own area (the line decides)
    crowded = [word("انظر"), word("(هـ"), word("–"), word("م)"), word("و"), tok("(٧٧٥–٧٨٠م)"), word("كذا")]
    assert nb.date_area(crowded, 1, 3, [0, 0, 100, 10]) is None
    # a word sharing it does not matter: Kraken reads no bracketed date out of a word
    worded = [
        word("فإن", [80, 0, 95, 10]),
        word("المتوكل"),
        word("(هـ"),
        word("–"),
        word("م)"),
        word("كان", [0, 0, 20, 10]),
    ]
    assert nb.date_area(worded, 2, 4, [0, 0, 100, 10]) == [20, 0, 80, 10]
    # not a date between those neighbours, or two of them: nothing
    assert (
        nb.date_reading(
            "المنصور (الكتاب) بأيدي",
            [word("المنصور"), word("(هـ"), word("–"), word("م)"), word("بأيدي")],
            1,
            3,
        )
        == ""
    )
    both = "المنصور (١٢-١٣م) و المنصور (١٤-١٥م) و"
    assert nb.date_reading(both, [word("المنصور"), word("(هـ"), word("–"), word("م)"), word("و")], 1, 3) == ""


# ---------------------------------------------------------------- the runner's helpers


def test_runner_pads_lines_and_puts_character_positions_in_page_pixels():
    assert kraken_runner.padded([100, 50, 300, 90], 1000, 1000) == (88, 38, 312, 102)
    assert kraken_runner.padded([0, 0, 20, 10], 15, 8) == (0, 0, 15, 8)  # clamped to the page
    assert kraken_runner.padded([100, 50, 300, 90], 1000, 1000, 0.15) == (
        94,
        38,
        306,
        102,
    )  # less on the sides
    cuts = [[(10, 0), (14, 30), (12, 5)], [(20, 0), (26, 30)]]
    assert kraken_runner.char_rows("١٢", cuts, [0.9, 0.8], dx=100) == [
        ["١", 110.0, 114.0, 0.9],
        ["٢", 120.0, 126.0, 0.8],
    ]
    assert kraken_runner.char_rows("١", None, None, dx=0) == [["١", None, None, 0.0]]


# ---------------------------------------------------------------- the pass


class FakeKraken:
    """Answers each area from `table` ({bbox tuple: text}), characters one pixel wide."""

    name = "kraken"
    model_id = "kraken:fake.mlmodel"
    model_revision = "test"
    backend = "cpu"

    def __init__(self, table):
        self.table = table
        self.calls = []

    def read(self, pages):
        self.calls.append(pages)
        lines = []
        for page in pages:
            for line in page["lines"]:
                text = self.table.get(tuple(line["bbox"]), "")
                lines.append({"id": line["id"], "text": text, "chars": chars(text, line["bbox"][0])})
        return {"ok": True, "lines": lines, "elapsed": 0.05}


@pytest.fixture
def numbers_page(db):
    book = Book.objects.create(title="كتاب", status=Book.Status.OCR)
    page = Page.objects.create(
        book=book, number=1, source_index=0, status=Page.Status.OCR_DONE, text_state=Page.TextState.FINAL
    )
    pre = Preprocess.objects.create(page=page, output_width=100, output_height=100)
    save_array(pre.gray_image, np.full((100, 100), 230, dtype=np.uint8), "gray.png")
    pre.save()
    body = [
        {"t": "توفي", "bbox": [80, 10, 95, 20], "conf": "high"},
        tok("(٢٢٢٢هـ)", alt="(٢٢٢ه)", bbox=[60, 10, 75, 20]),
        {"t": "وولد", "bbox": [40, 10, 55, 20], "conf": "high"},
        tok("٢٢٢٢", alt="٢٢٢٢"),  # no box: the gap between «وولد» (x0 40) and the line's left edge
    ]
    line = Line.objects.create(page=page, order=0, bbox=[0, 8, 100, 22], text="", tokens=body, n_low=2)
    reviewed = Line.objects.create(
        page=page,
        order=1,
        bbox=[0, 30, 100, 44],
        text="١٢",
        tokens=[tok("١٢", alt="١٣", bbox=[50, 30, 60, 44])],
        is_reviewed=True,
        n_low=1,
    )
    return page, line, reviewed


def test_the_pass_reads_the_numbers_of_unreviewed_lines_and_records_the_run(numbers_page):
    page, line, reviewed = numbers_page
    engine = FakeKraken({(60, 10, 75, 20): "(٣٣٤هـ)", (0, 8, 40, 22): "٣٢٢"})
    done = nb.read_page_numbers(page, engine=engine, style=nb.ARABIC_INDIC)
    assert (done.areas, done.applied) == (2, 2)
    line.refresh_from_db()
    assert [t["t"] for t in line.tokens] == ["توفي", "(٣٣٤هـ)", "وولد", "٣٢٢"]
    assert line.text == "توفي (٣٣٤هـ) وولد ٣٢٢" and line.n_low == 2  # still to confirm
    assert all(t.get("src") == "kraken" for t in line.tokens if t.get("digit"))
    reviewed.refresh_from_db()
    assert reviewed.tokens[0]["t"] == "١٢" and "src" not in reviewed.tokens[0]  # reviewed lines stay
    page.refresh_from_db()
    assert "(334هـ)" in page.final_text  # the page text follows (Western digits, D6)
    run = OcrRun.objects.get(page=page, engine_name="kraken")
    assert run.params == {"areas": 2, "applied": 2, "style": nb.ARABIC_INDIC}
    # a second pass finds nothing left to read
    again = nb.read_page_numbers(page, engine=engine, style=nb.ARABIC_INDIC)
    assert (again.areas, again.applied) == (0, 0) and len(engine.calls) == 1


def test_the_pass_reads_letters_and_dates_on_their_line_and_saves_them(numbers_page):
    page, line, _reviewed = numbers_page
    line.tokens = [
        word("المنصور", [80, 10, 95, 20]),
        word("(ع"),
        word("ه"),
        word("–"),
        word("ه"),
        word("م)"),
        word("بأيدي", [60, 10, 75, 20]),
        word("(ه)", [45, 10, 55, 20]),
        word("(هـ)", [30, 10, 40, 20]),
    ]
    line.save(update_fields=["tokens"])
    engine = FakeKraken(
        {
            (0, 8, 100, 22): "المنصور (٧٥٤-٧٧٥م) بأيدي (٥) (هـ)",  # the whole line
            (45, 10, 55, 20): "(٥)",  # each letter's own box
            (30, 10, 40, 20): "(هـ)",
        }
    )
    done = nb.read_page_numbers(page, engine=engine, style=nb.ARABIC_INDIC)
    assert (done.letters, done.dates, done.applied) == (1, 1, 2)
    line.refresh_from_db()
    assert line.text == "المنصور (٧٥٤–٧٧٥م) بأيدي (٥) (هـ)"
    assert line.tokens[3]["alt"] == "(ه)" and "src" not in line.tokens[4]  # «(هـ)»: Kraken read the letter
    assert line.n_low == 2  # the date and «(٥)»: the reviewer confirms them (D17)
    run = OcrRun.objects.get(page=page, engine_name="kraken")
    assert run.params["letters"] == 1 and run.params["dates"] == 1
    ids = [area["id"] for area in engine.calls[0][0]["lines"]]
    assert f"{line.pk}:line" in ids and f"{line.pk}:letter7" in ids
    # a second pass changes nothing (the real «(هـ)» is read again, and stays)
    again = nb.read_page_numbers(page, engine=engine, style=nb.ARABIC_INDIC)
    assert again.applied == 0
    line.refresh_from_db()
    assert line.text == "المنصور (٧٥٤–٧٧٥م) بأيدي (٥) (هـ)"


def test_numbers_letters_and_a_date_on_one_line_all_land(numbers_page):
    page, line, _reviewed = numbers_page
    line.tokens = [
        word("توفي", [85, 10, 95, 20]),
        tok("(٢٢٢٢هـ)", bbox=[70, 10, 82, 20]),  # a number (D50), its own box
        word("ا"),  # a footnote mark, no box
        word("وولد", [55, 10, 65, 20]),
        word("(هـ"),  # a digit-less date, no boxes
        word("–"),
        word("م)"),
        word("ثم", [30, 10, 40, 20]),
    ]
    line.save(update_fields=["tokens"])
    engine = FakeKraken(
        {
            (70, 10, 82, 20): "(٣٣٤هـ)",
            (0, 8, 100, 22): "توفي (٣٣٤هـ)١ وولد (٨٤٧-٨٦١م) ثم",
            (65, 10, 70, 20): "١",  # the letter's gap
            (40, 10, 55, 20): "(٨٤٧-٨٦١م)",  # the date's gap
        }
    )
    done = nb.read_page_numbers(page, engine=engine, style=nb.ARABIC_INDIC)
    assert (done.applied, done.letters, done.dates) == (3, 1, 1)
    line.refresh_from_db()
    assert line.text == "توفي (٣٣٤هـ) ١ وولد (٨٤٧–٨٦١م) ثم"


def test_a_page_approved_while_kraken_reads_is_left_alone(numbers_page):
    page, line, _reviewed = numbers_page

    class Meanwhile(FakeKraken):
        def read(self, pages):
            from django.utils import timezone

            Page.objects.filter(pk=page.pk).update(reviewed_at=timezone.now())
            return super().read(pages)

    done = nb.read_page_numbers(page, engine=Meanwhile({(60, 10, 75, 20): "(٣٣٤هـ)"}), style=nb.ARABIC_INDIC)
    assert done.applied == 0
    line.refresh_from_db()
    assert line.tokens[1]["t"] == "(٢٢٢٢هـ)"


def test_a_line_the_reviewer_changes_while_kraken_reads_is_left_as_they_made_it(numbers_page):
    page, line, _reviewed = numbers_page

    class Meanwhile(FakeKraken):
        def read(self, pages):
            from django.utils import timezone

            # the reviewer's edit (review saves `updated_at` with every change of a line)
            Line.objects.filter(pk=line.pk).update(
                text="كتبه المراجع", tokens=[word("كتبه")], updated_at=timezone.now()
            )
            return super().read(pages)

    engine = Meanwhile({(60, 10, 75, 20): "(٣٣٤هـ)", (0, 8, 40, 22): "٣٢٢"})
    done = nb.read_page_numbers(page, engine=engine, style=nb.ARABIC_INDIC)
    assert done.applied == 0
    line.refresh_from_db()
    assert line.text == "كتبه المراجع"
    assert OcrRun.objects.get(page=page, engine_name="kraken").params["left_to_reviewer"] == 1


def test_the_pass_leaves_western_books_and_approved_pages_alone(numbers_page):
    page, line, _reviewed = numbers_page
    engine = FakeKraken({})
    assert nb.read_page_numbers(page, engine=engine, style=nb.WESTERN).skipped == "not arabic-indic"
    from django.utils import timezone

    page.reviewed_at = timezone.now()
    page.save(update_fields=["reviewed_at"])
    assert nb.read_page_numbers(page, engine=engine, style=nb.ARABIC_INDIC).skipped == "approved"
    assert engine.calls == [] and not OcrRun.objects.filter(engine_name="kraken").exists()


def test_the_book_style_counts_qaris_readings_of_numbers_kraken_already_read(numbers_page):
    page, line, _reviewed = numbers_page
    # the style is Qari's: a number Kraken read counts with the readings kept under `qari`
    line.tokens = [tok("٣٣٤", src="kraken", qari={"t": "1328", "alt": "1328", "tess": None})] * 3
    line.save(update_fields=["tokens"])
    Line.objects.filter(pk=_reviewed.pk).delete()
    assert nb.book_style(page.book) == nb.WESTERN


# ---------------------------------------------------------------- scheduling and the engine


def test_scheduling_follows_the_setting_and_needs_kraken(
    numbers_page, django_capture_on_commit_callbacks, monkeypatch
):
    from django.conf import settings

    from ocr import tasks

    page = numbers_page[0]
    queued = []
    monkeypatch.setattr(tasks.read_numbers, "delay", lambda page_id: queued.append(page_id))
    with django_capture_on_commit_callbacks(execute=True):
        nb.schedule(page)  # tests: the pass is off
    assert queued == []
    with override_settings(NASSAKH={**settings.NASSAKH, "NUMBERS_PASS": True}):
        monkeypatch.setattr(KrakenEngine, "is_prepared", lambda self: True)
        with django_capture_on_commit_callbacks(execute=True):
            nb.schedule(page)
    assert queued == [page.pk]


def test_the_engine_reports_a_missing_setup_and_a_runner_failure(tmp_path):
    missing = KrakenEngine(python=tmp_path / "none", model=tmp_path / "none.mlmodel")
    assert not missing.is_prepared()
    with pytest.raises(KrakenError, match="make kraken"):
        missing.read([])
    model = tmp_path / "model.mlmodel"
    model.write_bytes(b"x")
    # the project's own Python has no Kraken: the runner answers ok: false and the engine raises
    engine = KrakenEngine(python=sys.executable, model=model)
    with pytest.raises(KrakenError, match="kraken"):
        engine.read([{"image": str(model), "lines": []}])


def test_review_payload_and_book_page_readings_carry_krakens_label():
    from editor.uncertain import _readings
    from review.services import normalize_token

    assert "src" not in normalize_token({"t": "١٢"})  # stored tokens stay as they were
    token = tok("(٣٣٤هـ)", src="kraken")
    assert normalize_token(token)["src"] == "kraken"
    labels = {"primary": "Qari v0.3", "secondary": "Qari v0.2", "tess": "Tesseract"}
    assert _readings(token, "(٣٣٤هـ)", labels) == [
        {"engine": "primary", "label": "Kraken", "text": "(٣٣٤هـ)", "current": True}
    ]
    # a letter Qari wrote for a digit (D51): Qari's letter is the second reading, under Qari's label
    letter = tok("(٥)", alt="(ه)", src="kraken")
    assert [(r["label"], r["text"]) for r in _readings(letter, "(٥)", labels)] == [
        ("Kraken", "(٥)"),
        ("Qari v0.3", "(ه)"),
    ]
