"""The notes' markers set from Kraken's reading of their lines (D103, `ocr.markers`)."""

from __future__ import annotations

from ocr import markers, numbers


def word(text: str, x0: int, y: int, w: int = 80) -> dict:
    return {"t": text, "bbox": [x0, y, x0 + w, y + 40], "conf": "high"}


def line(k: int, texts: list[str], marker: bool) -> dict:
    """A built line `k` (y = 100·k) whose words run right to left from x 940, or from x 990 with a marker."""
    y = 100 * k
    x = 990 if marker else 940
    tokens = []
    for i, text in enumerate(texts):
        w = 40 if (marker and i == 0) else 80
        tokens.append(word(text, x - w, y, w))
        x -= w + 10
    return {
        "bbox": [x, y, 1000, y + 40],
        "tokens": tokens,
        "indices": list(range(10 * k, 10 * k + len(texts))),
    }


def kraken(k: int, first: str | None, words: list[str]) -> dict:
    """Kraken's line `k`: its marker («(٣)», «()») at x 950–990 when `first`, then its words."""
    y = 100 * k
    out = [{"text": first, "bbox": [950, y, 990, y + 40]}] if first is not None else []
    x = 940
    for text in words:
        out.append({"text": text, "bbox": [x - 80, y, x, y + 40]})
        x -= 90
    return {"bbox": [x, y, 1000, y + 40], "words": out}


def texts(built: list[dict]) -> list[str]:
    return [" ".join(t["t"] for t in b["tokens"]) for b in built]


def test_a_dropped_marker_is_put_back_and_the_renumbered_ones_follow_krakens_reading():
    # book 35 p. 2: printed (١)…(٥); the models read (1), nothing, (2), (3), (ه)
    built = [
        line(0, ["(1)", "انظر"], True),
        line(1, ["راجع", "تقديمه"], False),
        line(2, ["(2)", "في", "المعسول"], True),
        line(3, ["(3)", "انظر", "الإعلام"], True),
        line(4, ["(ه)", "نيل", "السول"], True),
    ]
    box_lines = [
        kraken(0, "(١)", ["انظر"]),
        kraken(1, "(٢)", ["راجع", "تقديمه"]),
        kraken(2, "(٣)", ["في", "المعسول"]),
        kraken(3, "()", ["انظر", "الإعلام"]),
        kraken(4, "(٥)", ["نيل", "السول"]),
    ]
    assert markers.fix_markers(built, box_lines) == 5
    assert texts(built) == [
        "(١) انظر",
        "(٢) راجع تقديمه",
        "(٣) في المعسول",
        "(٤) انظر الإعلام",
        "(٥) نيل السول",
    ]
    put = built[1]["tokens"][0]
    assert (
        put["src"] == "kraken"
        and put["marker"]
        and put["conf"] == "low"
        and put["bbox"] == [950, 100, 990, 140]
    )
    assert built[2]["tokens"][0]["qari"]["t"] == "(2)"  # what the models wrote stays on record


def test_a_marker_left_at_the_end_of_the_line_above_moves_down():
    # book 34 p. 5: «… الوصول 2/2. (6)» then «انظر المرجع السابق», Kraken «()» at that line's start
    built = [
        line(0, ["(5)", "انظر", "أثر", "(6)"], True),
        line(1, ["انظر", "المرجع"], False),
    ]
    box_lines = [kraken(0, "(٥)", ["انظر", "أثر"]), kraken(1, "()", ["انظر", "المرجع"])]
    markers.fix_markers(built, box_lines)
    assert texts(built) == ["(٥) انظر أثر", "(٦) انظر المرجع"]
    assert (
        built[1]["indices"][0] == 3
    )  # the models' token keeps its place in their text (suggestions follow it)


def test_a_marker_the_models_read_a_letter_into_is_a_marker():
    # book 29 p. 104: «(٢م)» for (٣)
    built = [line(0, ["(١)", "بعد"], True), line(1, ["(٢)", "كان"], True), line(2, ["(٢م)", "الهيرة"], True)]
    box_lines = [kraken(0, "(١)", ["بعد"]), kraken(1, "(٢)", ["كان"]), kraken(2, "(٣)", ["الهيرة"])]
    assert markers.fix_markers(built, box_lines) == 1
    assert texts(built)[2] == "(٣) الهيرة"


def test_a_bracketed_number_opening_a_carried_line_is_not_a_marker():
    # book 33 p. 6: «(2)، وشرح الخرشي» continues the note above; Kraken read no marker there
    built = [
        line(0, ["(1)", "هو", "أحمد"], True),
        line(1, ["(2)", "لقول", "خليل"], True),
        line(2, ["(2)،", "وشرح", "الخرشي"], True),
    ]
    box_lines = [
        kraken(0, "(1)", ["هو", "أحمد"]),
        kraken(1, "(2)", ["لقول", "خليل"]),
        kraken(2, None, ["(2)،"]),
    ]
    before = texts(built)
    markers.fix_markers(built, box_lines)
    assert texts(built) == before
    assert built[0]["tokens"][0]["marker"] and built[1]["tokens"][0]["marker"]  # checked, left as written
    assert not built[2]["tokens"][0].get("marker")


def test_markers_stay_as_written_when_krakens_digits_agree_with_no_run():
    built = [line(0, ["(1)", "أ"], True), line(1, ["(2)", "ب"], True), line(2, ["(3)", "ج"], True)]
    box_lines = [kraken(0, "(7)", ["أ"]), kraken(1, "(4)", ["ب"]), kraken(2, "(2)", ["ج"])]
    assert markers.fix_markers(built, box_lines) == 0
    assert texts(built) == ["(1) أ", "(2) ب", "(3) ج"]


def test_lettered_notes_are_left_as_they_are():
    built = [line(0, ["(أ)", "نص"], True), line(1, ["(ب)", "نص"], True), line(2, ["(ج)", "نص"], True)]
    box_lines = [kraken(0, "(١)", ["نص"]), kraken(1, "(٢)", ["نص"]), kraken(2, "(٣)", ["نص"])]
    assert markers.fix_markers(built, box_lines) == 0


def test_a_marker_kraken_reads_inside_the_first_word_is_none():
    # the models' first word already covers the ink Kraken read as a marker
    built = [{"bbox": [0, 0, 1000, 40], "tokens": [word("كلمة", 900, 0, 95)], "indices": [0]}]
    box_lines = [{"bbox": [0, 0, 1000, 40], "words": [{"text": "(٣)", "bbox": [950, 0, 990, 40]}]}]
    assert markers.fix_markers(built, box_lines) == 0


def test_the_numbers_pass_leaves_a_checked_marker_alone():
    # book 39 p. 6: Kraken's own reading of a marker «(٦)١» for (٤) must not replace the checked one
    token = {"t": "(4)", "digit": True, "marker": True}
    assert numbers.apply_reading(token, ["61"]) is False and token["t"] == "(4)"
    assert numbers._settled(token)
