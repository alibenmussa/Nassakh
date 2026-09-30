"""Flag policy v2 (D71), words only the second model read (D72) and the looped prefix (D73): `ocr.flags`,
the vote (`ocr.chooser`) and their use by `ocr.alignment.build_lines` / `ocr.services.build_region`.

Pure: no database. The real texts come from the dev database (`fixtures/trust_runs.json`: book 23 page 1
with its Tesseract lines and bands, and the regions where each filter of §4.2 first fired).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ocr import chooser, flags
from ocr.alignment import build_lines

RUNS = json.loads((Path(__file__).parent / "fixtures" / "trust_runs.json").read_text())
W = 1000


def tess_lines(text: str, y0: int = 0, line_h: int = 20, conf: float = 90.0) -> list[dict]:
    """Tesseract-style lines for `text` (one per text line), words laid out right to left."""
    lines = []
    for k, raw in enumerate(text.split("\n")):
        words = raw.split()
        step = W // max(len(words), 1)
        y = y0 + k * line_h
        boxes = [
            {"text": tok, "bbox": [W - (i + 1) * step, y, W - i * step, y + line_h], "conf": conf}
            for i, tok in enumerate(words)
        ]
        lines.append({"bbox": [0, y, W, y + line_h], "words": boxes})
    return lines


def tokens_of(primary: str, secondary: str | None, tesseract: str, **kwargs) -> dict[str, dict]:
    lines = build_lines(primary, secondary, tess_lines(tesseract, conf=kwargs.pop("conf", 90.0)), **kwargs)
    return {token["t"]: token for line in lines for token in line["tokens"]}


# ---------------------------------------------------------------- readings: the punctuation split


def test_second_readings_split_punctuation_off_and_glue_it_back_in_the_primary_shape():
    assert flags.second_readings(["القرآن."], ["القرآن", "."]) == ["القرآن."]
    assert flags.second_readings(["قال", "الكتاب،"], ["قال", "الكتب."]) == ["قال", "الكتب،"]
    assert flags.second_readings(["قال", "الأمير"], ["قال"]) == ["قال", None]
    assert flags.second_readings(["قال"], []) == [None]


def test_punctuation_is_never_flagged():
    tokens = tokens_of("في القرآن. قال", "في القرآن . قال", "في القرآن. قال")
    assert tokens["القرآن."]["conf"] == "high" and "why" not in tokens["القرآن."]
    tokens = tokens_of("قال ، ثم", "قال . ثم", "قال ، ثم")  # a comma against a full stop: 0 errors in 48
    assert tokens["،"]["conf"] == "high" and tokens["،"]["alt"] is None
    assert flags.classify("،", ".", None, None, True, True) == []


# ---------------------------------------------------------------- numbers


def test_a_western_number_is_sure_only_when_all_three_readers_agree():
    assert flags.classify("1964", "1964", None, 90, True, True) == []  # Tesseract read the same (boxed)
    assert flags.classify("1964", "1964", "1964م", 90, True, True) == []
    assert flags.classify("1964", "1965", None, 90, True, True) == ["number"]
    assert flags.classify("1964", "1964", "1954", 90, True, True) == ["number"]
    assert flags.classify("1964", "1964", None, None, False, True) == ["number"]  # no Tesseract word
    assert flags.classify("1964", None, None, 90, True, True) == ["number"]
    assert tokens_of("سنة 1964 هنا", "سنة 1964 هنا", "سنة 1964 هنا")["1964"]["conf"] == "high"


def test_an_arabic_indic_number_stays_low():
    assert flags.classify("١٩٦٤", "١٩٦٤", None, 90, True, True) == ["number"]
    assert flags.classify("ص١٧٩", "ص١٧٩", None, 90, True, True) == ["number"]
    token = tokens_of("سنة ١٩٦٤ هنا", "سنة ١٩٦٤ هنا", "سنة ١٩٦٤ هنا")["١٩٦٤"]
    assert token["conf"] == "low" and token["why"] == ["number"] and token["digit"] is True


# ---------------------------------------------------------------- other scripts and stray symbols


@pytest.mark.parametrize("word", ["مилادية", "بزنзор", "وال德拉ية", "الأ石油化工", "※", "^", "©", "الإيyan"])
def test_foreign_letters_stray_symbols_and_mixed_scripts_are_flagged_script(word):
    assert flags.classify(word, word, None, 90, True, True) == ["script"]


@pytest.mark.parametrize("word", ["°", "%", "25%", "LAMPEDOUSE", "D’ANFREVILLE", "(1)", "«قال»"])
def test_ordinary_marks_and_latin_words_are_not_script(word):
    assert "script" not in flags.classify(word, word, None, 90, True, True)


def test_the_foreign_characters_are_listed_for_the_reason_line():
    assert flags.foreign_chars("مилادية") == ["и", "л"]
    assert flags.reason_chars({"t": "※"}) == ["※"]


# ---------------------------------------------------------------- the word rules


def test_a_word_qari_v02_lacks_is_sure_with_tesseract_and_alone_without():
    assert flags.classify("الأمير", None, None, 90, True, True) == []  # Tesseract read the same word
    assert flags.classify("الأمير", None, "الوزير", 90, True, True) == ["alone"]
    assert flags.classify("الأمير", None, None, None, False, True) == ["alone"]  # no Tesseract word


def test_the_models_differing_leniently_is_disagree_and_folds_are_not():
    assert flags.classify("الكتاب", "الكتب", None, 90, True, True) == ["disagree"]
    assert flags.classify("مدرسة", "مدرسه", None, 90, True, True) == []  # ة/ه hidden by the lenient form
    assert flags.classify("قالَ", "قال", None, 90, True, True) == []


def test_a_lone_letter_that_may_be_a_digit_keeps_the_rule_before_7b():
    assert flags.classify("آ", "آ", None, 90, True, True, today=True) == ["disagree"]
    assert flags.classify("(ه)", None, None, 90, True, True, today=True) == ["alone"]
    assert flags.classify("آ", "آ", None, 90, True, True, today=False) == []


def test_single_reader_flags_need_conf_85_and_another_dotless_skeleton():
    assert flags.classify("يجي", None, "يحيى", 90, True, False) == ["single"]
    assert flags.classify("يجي", None, "يحيى", 84, True, False) == []  # not confident enough
    assert flags.classify("يحيى", None, "يحبى", 95, True, False) == []  # the same skeleton: dots only
    assert flags.classify("يجي", None, "Yahya", 95, True, False) == []  # Tesseract's word is not Arabic
    assert flags.classify("يجي", None, None, None, False, False) == []
    assert flags.classify("يجي", None, "يحيى", 95, True, None) == []  # no model reading (text layer)
    assert flags.loose_single_flag("يحيى", "يحبى", 70) and not flags.loose_single_flag("يحيى", "يحبى", 69)


def test_the_dotless_skeleton():
    assert flags.skeleton("يحيى") == flags.skeleton("يحبى") == "ٮحٮٮ"
    assert flags.skeleton("بيت") == flags.skeleton("نبت") == "ٮٮٮ"
    assert flags.skeleton("قَرْأ") == flags.skeleton("فرا") == "ڡرا"
    assert flags.skeleton("الشمس،") == "السمس"
    assert flags.skeleton("مدرسة") == flags.skeleton("مدرسه")
    assert flags.skeleton("شيء") == "سٮ" and flags.skeleton("ضظغذزؤ") == "صطعدرو"
    assert flags.skeleton("يجي") != flags.skeleton("يحيى")


def test_build_lines_single_region_flags_and_why_is_stored_only_when_low():
    tokens = tokens_of("ولد يجي بطرابلس", None, "ولد يحيى بطرابلس", single=True)
    assert tokens["يجي"]["why"] == ["single"] and tokens["يجي"]["tess"] == "يحيى"
    assert tokens["يجي"]["tc"] == 90.0 and tokens["يجي"]["conf"] == "low"
    assert "why" not in tokens["ولد"] and tokens["ولد"]["conf"] == "high"
    tokens = tokens_of("ولد يجي بطرابلس", None, "ولد يحيى بطرابلس")  # no model reading to compare
    assert tokens["يجي"]["conf"] == "high"


def test_build_lines_partial_second_reading_makes_the_rest_one_reader():
    primary = "قال الكتاب في يجي هنا"
    tokens = tokens_of(primary, "قال الكتب", "قال الكتاب في يحيى هنا", partial=True)
    assert tokens["الكتاب"]["why"] == ["disagree"]  # within the looped prefix: two readers
    assert tokens["يجي"]["why"] == ["single"]  # after it: one reader
    assert tokens["في"]["conf"] == "high"


# ---------------------------------------------------------------- the vote (D26 activated, D71)


def voted(token: dict) -> tuple[dict, bool]:
    token = dict(token)
    return token, chooser.vote(token)


def test_the_vote_puts_the_reading_tesseract_backs_in_the_text_and_leaves_the_word_open():
    token = {"t": "يجي", "alt": "يحيى", "conf": "low", "digit": False, "bbox": [0, 0, 1, 1], "tess": "يحيى"}
    token["why"] = ["disagree"]
    keys = list(token)
    token, changed = voted(token)
    assert changed and token["t"] == "يحيى" and token["orig"] == "يجي" and token["pick"] == "vote"
    assert token.get("res") is None and token["conf"] == "low" and token["why"] == ["disagree"]
    assert list(token)[: len(keys)] == keys  # the stored keys keep their places


def test_the_vote_waits_for_tesseract_to_back_the_second_model():
    base = {"t": "زاهدا", "alt": "راهدا", "conf": "low", "digit": False, "bbox": None}
    assert not voted({**base, "tess": "زاهد"})[1]
    assert not voted({**base, "tess": None})[1]
    assert not voted({**base, "tess": "راهدا", "res": "primary"})[1]  # decided by the reviewer


@pytest.mark.parametrize(
    "token",
    [
        {"t": "١٩٦٤", "alt": "١٩٦٥", "tess": "١٩٦٥", "digit": True},
        {"t": "١٩٦٤", "alt": "١٩٦٥", "tess": "١٩٦٥", "digit": False, "src": "kraken"},
        {"t": "ص١٧٩", "alt": "ص١٧٨", "tess": "ص١٧٨", "digit": False},
        {"t": "ه", "alt": "٥", "tess": "٥", "digit": False},
        {"t": "(ا)", "alt": "(١)", "tess": "(١)", "digit": False},
        {"t": "«ع»", "alt": "«٤»", "tess": "«٤»", "digit": False},
    ],
)
def test_the_vote_never_touches_numbers_kraken_or_lone_letters(token):
    token = {**token, "conf": "low", "bbox": None}
    assert not voted(token)[1]


def test_apply_chooser_runs_the_vote_by_default(settings):
    settings.NASSAKH = {**settings.NASSAKH, "WORD_CHOOSER": "vote"}
    tokens = [
        {"t": "يجي", "alt": "يحيى", "conf": "low", "tess": "يحيى", "why": ["disagree"]},
        {"t": "قال", "alt": None, "conf": "high", "tess": None},
    ]
    assert chooser.apply_chooser(tokens, {}) == 1
    assert tokens[0]["t"] == "يحيى" and tokens[0]["pick"] == "vote" and tokens[0].get("res") is None
    settings.NASSAKH = {**settings.NASSAKH, "WORD_CHOOSER": "none"}
    assert chooser.apply_chooser([{"t": "يجي", "alt": "يحيى", "conf": "low", "tess": "يحيى"}], {}) == 0


# ---------------------------------------------------------------- words only the second model read (D72)


@pytest.mark.parametrize(
    ("key", "at", "text", "drop"),
    [
        ("split_hir", 179, "هير", "split"),  # «هير» of «هيرودوت» (book 1 page 2)
        ("split_laha", 44, "لها", "split"),  # «لها» of «واستقلالها.» (book 21 page 5)
        ("head", 0, "مقدمة", "head"),  # a running head at the start of a body region (book 19 page 9)
        ("duplicate", 62, "و ( ابراهيم بن الاغلب", "duplicate"),  # words read nearby (book 23 page 7)
    ],
)
def test_the_real_false_runs_are_dropped_by_their_filter(key, at, text, drop):
    region = RUNS[key]
    runs = flags.secondary_only_runs(region["primary"].split(), region["secondary"].split())
    assert [(run.at, run.text, run.drop) for run in runs] == [(at, text, drop)]


def test_the_filters_on_small_texts():
    def drop(primary: str, secondary: str, footnote: bool = False) -> list[tuple[int, str, str]]:
        runs = flags.secondary_only_runs(primary.split(), secondary.split(), footnote)
        return [(run.at, run.text, run.drop) for run in runs]

    assert drop("قال في سنة", "قال في سنة ٢٣٤هـ") == []  # a number: no word of two letters
    assert drop("قال في سنة", "قال في ص١١ه سنة") == [(2, "ص١١ه", "numbers")]  # every word holds digits
    assert drop("قال في سنة", "قال في ( ص١١ه و١٢ه ) سنة")[0][2] == "numbers"
    assert drop("قال في سنة", "قال في ــ سنة") == []  # a dash of tatweels is no word
    assert drop("قال الشيخ في كتابه", "عنوان قال الشيخ في كتابه") == [(0, "عنوان", "head")]
    assert drop("قال الشيخ في كتابه", "عنوان قال الشيخ في كتابه", footnote=True) == [(0, "عنوان", "")]
    words = "بطرابلس ونشأ بها واخذ عن جماعة"
    assert (
        drop(f"قال الشيخ رحمه الله {words} ثم", f"قال الشيخ {words} رحمه الله {words} ثم")[0][2]
        == "duplicate"
    )
    assert drop("ولد رحمه الله تعالى من كبار", f"ولد رحمه الله تعالى {words} من كبار") == [(4, words, "")]


def test_merge_runs_inserts_the_words_at_their_anchors():
    runs = [flags.Run(at=1, pieces=[("ب", 0)], words=["ب", "ج"]), flags.Run(at=3, pieces=[], words=["د"])]
    merged, inserted = flags.merge_runs(["أ", "ه", "و"], runs)
    assert merged == ["أ", "ب", "ج", "ه", "و", "د"] and inserted == {1: 0, 2: 0, 5: 1}


def _region_23_1(tesseract_lines=None):
    from ocr.services import RegionText, Target

    region = RUNS["b23p1"]
    lines = region["tesseract_lines"] if tesseract_lines is None else tesseract_lines
    return RegionText(
        Target(None, [0, 0, 1, 1]), region["primary"], region["secondary"], lines, False, "ok", "q"
    )


def test_the_dropped_line_of_23_1_comes_back_as_one_group_on_lines_11_and_12():
    from ocr.services import build_region

    build = build_region(_region_23_1(), RUNS["b23p1"]["bands"], None)
    assert build.readers == "two" and build.gaps == []
    line_11, line_12 = build.built[10]["tokens"], build.built[11]["tokens"]
    assert (
        " ".join(t["t"] for t in line_11) == "تعالى بطرابلس ونشأ بها واخذ عن جياعة من الفضاء وكان رحمه الله"
    )
    assert " ".join(t["t"] for t in line_12).startswith("تعالى من كبار الصوفية")
    group = [t for t in line_11 + line_12 if t.get("ins") == 1]
    assert len(group) == 12 and all(t["why"] == ["missing"] and t["conf"] == "low" for t in group)
    assert build.groups[1].support >= flags.SUPPORT_MIN


def test_without_tesseracts_support_the_run_is_a_gap_never_text():
    from ocr.services import build_region

    region = RUNS["b23p1"]
    lines = [
        line for line in region["tesseract_lines"] if "بطرابلس" not in {w["text"] for w in line["words"]}
    ]
    build = build_region(_region_23_1(lines), region["bands"], None)
    gap = next(run for _at, run in build.gaps if "بطرابلس" in run.text)
    assert gap.support < flags.SUPPORT_MIN
    assert "بطرابلس" not in " ".join(t["t"] for line in build.built for t in line["tokens"])


def test_tesseracts_support_is_looked_for_on_its_own_lines_whichever_reader_gives_the_boxes():
    import copy

    from ocr.services import build_region

    def lower(box):
        return [box[0], box[1] + 10000, box[2], box[3] + 10000]

    far = copy.deepcopy(RUNS["b23p1"]["tesseract_lines"])  # Kraken's lines (D92), here far below Tesseract's
    for line in far:
        line["bbox"] = lower(line["bbox"])
        for word in line["words"]:
            word["bbox"] = lower(word["bbox"])
    rt = _region_23_1()
    rt.box_lines = far
    build = build_region(rt, RUNS["b23p1"]["bands"], None)
    assert build.groups[1].support >= flags.SUPPORT_MIN and build.gaps == []  # the text as without them


# ---------------------------------------------------------------- the looped prefix (D73)


def test_the_looped_prefix_is_the_clean_start_without_the_repeated_unit():
    unit = "واخذ عن جماعة من الفضلاء "
    raw = "ولد يحيى فقيهاً فاضلاً زاهداً " + unit * 6
    prefix = flags.looped_prefix(raw)
    assert prefix.startswith("ولد يحيى فقيهاً فاضلاً زاهداً") and prefix.count("الفضلاء") <= 1
    assert flags.looped_prefix("قال " * 40) == ""  # nothing clean before the loop
    assert flags.looped_prefix("ولد يحيى فقيهاً فاضلاً زاهد", hit_cap=True) == "ولد يحيى فقيهاً فاضلاً"


def test_last_read_is_where_the_second_reading_stops():
    assert flags.last_read(["a", None, "b", None]) == 2
    assert flags.last_read([None, None]) == -1


# ---------------------------------------------------------------- years in words (§4.8)


@pytest.mark.parametrize(
    ("words", "value"),
    [
        ("اثنتين واربعين ومايتين", 242),
        ("ثلاث واربعين ومايتين وولي", 243),  # the words stop at the first other word
        ("خمس وخمسين ومائتين", 255),
        ("الف وثلثماية واربع وخمسين", 1354),
        ("ثلاثمئة وعشرة", 310),
        ("احدى عشرة", 11),
        ("ثلاثة آلاف", 3000),
        ("الفين", 2000),
    ],
)
def test_number_words_parse_arabic_cardinals_with_their_old_spellings(words, value):
    assert flags.number_words(words.split())[0] == value


def test_number_words_need_a_number_word_first():
    assert flags.number_words(["توفي", "ثلاث"]) is None and flags.number_words([]) is None


def num(t, conf="low", why=("number",), **extra) -> dict:
    token = {"t": t, "conf": conf, **extra}
    if why:
        token["why"] = list(why)
    return token


def words(*texts) -> list[dict]:
    return [{"t": text, "conf": "high"} for text in texts]


def test_a_year_that_agrees_with_its_words_is_settled():
    line = [*words("سنة", "("), num("٢٤٢"), *words(")", "اثنتين", "واربعين", "ومايتين", "وولي")]
    assert flags.year_check([line]) == 1
    assert line[2]["res"] == "words" and "sug" not in line[2] and line[2]["why"] == ["number"]


def test_a_year_that_differs_gets_the_words_reading_as_a_suggestion_in_its_script():
    line = [*words("سنة"), num("(٢٤٢هـ)"), *words("ثلاث", "واربعين")]
    following = words("ومايتين", "توفي")  # the words continue on the next line
    assert flags.year_check([line, following]) == 1
    token = line[1]
    assert token.get("res") is None and token["why"] == ["number", "year"] and token["conf"] == "low"
    assert token["sug"] == {
        "t": "(٢٤٣هـ)",
        "src": "words",
        "label": "من الحروف",
        "words": "ثلاث واربعين ومايتين",
    }
    western = [num("243"), *words("م", "ثلاث", "واربعين", "ومايتين")]  # the era between them
    flags.year_check([western])
    assert western[0]["res"] == "words"
    # once Kraken reads the digits right, the suggestion and its reason go
    token["t"] = "(٢٤٣هـ)"
    flags.year_check([line, following])
    assert token["res"] == "words" and "sug" not in token and token["why"] == ["number"]


def test_the_year_check_leaves_resolved_tokens_short_words_and_small_numbers_alone():
    resolved = [num("٢٤٢", res="typed"), *words("ثلاث", "واربعين", "ومايتين")]
    assert flags.year_check([resolved]) == 0 and "sug" not in resolved[0]
    short = [num("٢٥٠"), *words("خمسين"), *words("توفي")]  # «ومايتين» was read apart
    assert flags.year_check([short]) == 0
    small = [num("٢"), *words("اثنين")]  # below `YEAR_MIN_VALUE`
    assert flags.year_check([small]) == 0
    far = [num("٢٤٣"), *words("في", "سنة", "ثلاث", "واربعين")]  # other words between: no year in words
    assert flags.year_check([far]) == 0


# ---------------------------------------------------------------- 7 review: a mark alone reads no word


def test_a_mark_is_no_second_reading_of_a_word_the_second_model_skipped():
    # v0.2 skipped «الكتاب» but kept its comma: the comma is no reading of the word (D71: sure with Tesseract)
    assert flags.second_readings(["قال", "الكتاب،", "ثم"], ["قال،", "ثم"]) == ["قال", None, "ثم"]
    assert flags.second_readings(["قال", "،", "ثم"], ["قال", "،", "ثم"]) == ["قال", "،", "ثم"]  # a mark's own
    primary, skipped = "قال الكتاب، ثم ذهب إلى السوق", "قال، ثم ذهب إلى السوق"
    word = tokens_of(primary, skipped, primary)["الكتاب،"]
    assert (word["alt"], word["conf"], word.get("why")) == (None, "high", None)
    # without Tesseract's support it is one model's word, not a disagreement over a comma
    word = tokens_of(primary, skipped, "قال الوزير، ثم ذهب إلى السوق")["الكتاب،"]
    assert (word["alt"], word["why"]) == (None, ["alone"])


def test_the_vote_never_puts_a_mark_in_place_of_a_word():
    token = {"t": "الكتاب،", "alt": "،", "tess": ".", "conf": "low", "digit": False, "why": ["disagree"]}
    assert flags.vote_reading(token) is None and not voted(token)[1]


# ---------------------------------------------------------------- a unit the model wrote twice


def test_a_unit_the_model_wrote_twice_is_read_once():
    """Book 29 (full-book test, 2026-09-28): a footnote line written twice before the model went on."""
    unit = "بقي يخلف والياً على هذه المقاطعات الى أن مات المعز وتولى بعــده"
    twice = f"(١) {unit} (٢) {unit} (٣) في هذا التاريخ خلاف بين المؤرخين ."
    tess = "(١) بقي يخلف واليا على هذه المقاطمات الى أن مات المعز وتولى يده\nالعزيز بالل فطلب منه\n(؟) في هذا"
    text, cut = flags.strip_repeat(twice, tess)
    assert (
        cut == 12 and text == f"(١) {unit} (٣) في هذا التاريخ خلاف بين المؤرخين ."
    )  # the marker goes with it
    head = "(1) كنديته أبو زكرا ، وهو مؤسس الدولة الخفصية ، عين أم كيراً على"
    text, cut = flags.strip_repeat(f"{head} {head} افريقية من قبل الموحدين", "")
    assert (text, cut) == (f"{head} افريقية من قبل الموحدين", 14)
    # a line break elsewhere stays; a word between the copies is no repeat; short repeats are left alone
    text, cut = flags.strip_repeat(f"سطر أول\n{head} {head}\nسطر أخير", "")
    assert text == f"سطر أول\n{head}\nسطر أخير" and cut == 14
    kept = f"{head} افريقية {head}"
    assert flags.strip_repeat(kept, "") == (kept, 0)
    short = "قال ثم قال ثم قال ثم قال ثم قال ثم"
    assert flags.strip_repeat(short, "") == (short, 0)
    # printed twice (Tesseract shows the unit twice): kept
    assert flags.strip_repeat(f"{head} {head}", f"{head} {head}") == (f"{head} {head}", 0)
    assert flags.strip_repeat("", "") == ("", 0)
