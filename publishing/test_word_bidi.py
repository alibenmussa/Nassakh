"""The direction of Word runs against the preview's order (PHASE6_SPEC §5.6; the Phase 6 review, finding 2).

The preview applies the Unicode Bidi Algorithm (UAX #9) to a paragraph's plain text. Word reads the same
text, except that a `w:rtl` run is a right-to-left override for its weak characters other than EN, ET,
CS and AN (ES, NSM, BN) and for its neutrals (B, S, WS, ON): ECMA-376 Part 1 §17.3.2.30. The oracle below
is a plain per-character UAX #9 (rules W1–W7, N1–N2, I1–I2 and X9, no explicit embeddings), written
apart from `publishing.model`'s resolver: a paragraph is right when Word's levels, with the overrides of
its `w:rtl` runs, equal the preview's levels of the same text, character by character.
"""

from __future__ import annotations

import io
import random
import unicodedata
import zipfile

from django.conf import settings
from django.test import override_settings

import pytest
from lxml import etree

from publishing import fonts as F
from publishing.model import book_model, direction_runs
from publishing.tests import document, heading, note, para, text
from publishing.word import schema
from publishing.word.ooxml import W_NS
from publishing.word.options import WordOptions
from publishing.word.writer import DocMeta, PagePlan, build_docx

W = f"{{{W_NS}}}"
OVERRIDDEN = {"ES", "NSM", "BN", "B", "S", "WS", "ON"}  # what `w:rtl` turns into R (§17.3.2.30)
NEUTRAL = ("B", "S", "WS", "ON")
CALL = "‏"  # a footnote call or marker, as the writer models it: one strong right-to-left character


@pytest.fixture(autouse=True)
def _no_system_fonts(tmp_path):
    with override_settings(NASSAKH={**settings.NASSAKH, "FONT_DIRS": [str(tmp_path / "no-fonts")]}):
        yield


# ====================================================================== the oracle


def bidi_class(char: str) -> str:
    value = unicodedata.bidirectional(char) or "L"
    assert value not in ("LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI")
    return value


def levels(classes: list[str], paragraph: int = 1) -> list[int | None]:
    """UAX #9 levels of one paragraph (None for BN, which X9 removes)."""
    keep = [i for i, c in enumerate(classes) if c != "BN"]
    t = [classes[i] for i in keep]
    sos = "R" if paragraph % 2 else "L"
    n = len(t)
    for i in range(n):  # W1
        if t[i] == "NSM":
            t[i] = t[i - 1] if i else sos
    last = sos
    for i in range(n):  # W2
        if t[i] in ("L", "R", "AL"):
            last = t[i]
        elif t[i] == "EN" and last == "AL":
            t[i] = "AN"
    t = ["R" if c == "AL" else c for c in t]  # W3
    for i in range(1, n - 1):  # W4
        if t[i] == "ES" and t[i - 1] == "EN" == t[i + 1]:
            t[i] = "EN"
        elif t[i] == "CS" and t[i - 1] == t[i + 1] and t[i - 1] in ("EN", "AN"):
            t[i] = t[i - 1]
    i = 0
    while i < n:  # W5
        j = i
        while j < n and t[j] == "ET":
            j += 1
        if j > i and ((i and t[i - 1] == "EN") or (j < n and t[j] == "EN")):
            t[i:j] = ["EN"] * (j - i)
        i = max(j, i + 1)
    t = ["ON" if c in ("ES", "ET", "CS") else c for c in t]  # W6
    last = sos
    for i in range(n):  # W7
        if t[i] in ("L", "R"):
            last = t[i]
        elif t[i] == "EN" and last == "L":
            t[i] = "L"
    i = 0
    while i < n:  # N1, N2
        if t[i] not in NEUTRAL:
            i += 1
            continue
        j = i
        while j < n and t[j] in NEUTRAL:
            j += 1
        before = sos if i == 0 else ("R" if t[i - 1] in ("R", "EN", "AN") else "L")
        after = sos if j == n else ("R" if t[j] in ("R", "EN", "AN") else "L")
        t[i:j] = [before if before == after else sos] * (j - i)
        i = j
    out: list[int | None] = [None] * len(classes)
    for position, kind in zip(keep, t, strict=True):
        if paragraph % 2:
            out[position] = paragraph + (1 if kind in ("L", "EN", "AN") else 0)
        else:
            out[position] = paragraph + (1 if kind == "R" else 2 if kind in ("EN", "AN") else 0)
    return out


def word_matches_preview(runs: list[tuple[str, bool]], paragraph: int = 1) -> bool:
    """True when Word orders the `(text, rtl)` runs as the preview orders their joined text."""
    preview = levels([bidi_class(c) for text_, _rtl in runs for c in text_], paragraph)
    word = levels(
        ["R" if rtl and bidi_class(c) in OVERRIDDEN else bidi_class(c) for text_, rtl in runs for c in text_],
        paragraph,
    )
    return all(a == b for a, b in zip(preview, word, strict=True) if a is not None and b is not None)


def visual(runs: list[tuple[str, bool]], word: bool) -> str:
    """The line in visual order, left to right (the preview's, or Word's with `word`)."""
    value = "".join(text_ for text_, _rtl in runs)
    classes = [
        "R" if word and rtl and bidi_class(c) in OVERRIDDEN else bidi_class(c)
        for text_, rtl in runs
        for c in text_
    ]
    lv = levels(classes)
    order = [i for i in range(len(value)) if lv[i] is not None]
    for level in range(max((lv[i] for i in order), default=1), 0, -1):
        k = 0
        while k < len(order):
            if lv[order[k]] < level:
                k += 1
                continue
            j = k
            while j < len(order) and lv[order[j]] >= level:
                j += 1
            order[k:j] = reversed(order[k:j])
            k = j
    return "".join(value[i] for i in order)


def test_the_oracle_shows_the_reordering_the_review_found():
    """The old writer's runs (the space or the hyphen in a `w:rtl` run) come out reordered in Word."""
    old = [("Windows", False), (" 10", True)]
    assert visual(old, word=False) == "Windows 10" and visual(old, word=True) == "10 Windows"
    old = [("DE HAMMER, t. XII, p", False), (". 55-58.", True)]
    assert visual(old, word=True).startswith(".58-55")
    assert not word_matches_preview(old)


# ====================================================================== direction_runs


CASES = [
    # (text, the pieces the writer gives)
    ("Windows 10", [("Windows 10", False)]),
    ("نظام Windows 10 الجديد", [("نظام ", True), ("Windows 10", False), (" الجديد", True)]),
    ("مثل COVID-19 وغيرها", [("مثل ", True), ("COVID-19", False), (" وغيرها", True)]),
    ("DE HAMMER, t. XII, p. 55-58.", [("DE HAMMER, t. XII, p. 55-58", False), (".", True)]),
    # the Arabic comma is a common separator (CS), not a right-to-left letter
    (
        "انظر: Ibn Khaldun، Muqaddimah، ص 45.",
        [("انظر: ", True), ("Ibn Khaldun، Muqaddimah", False), ("، ص 45.", True)],
    ),
    ("قال Ibn Khaldun في 1377 كتابه", [("قال ", True), ("Ibn Khaldun", False), (" في 1377 كتابه", True)]),
    # numbers of an Arabic context: digits after Arabic letters are Arabic numbers (W2), their hyphens R
    ("من ليلة 24-23 يونيه", [("من ليلة 24-23 يونيه", True)]),
    ("ردمك: 978-9959-0-0001-1", [("ردمك: 978-9959-0-0001-1", True)]),
    # a number that opens the paragraph keeps its hyphen left to right (W4)
    ("12-15 من الآيات", [("12", True), ("-", False), ("15 من الآيات", True)]),
    # an overridden space would hide the Arabic letter from the digits (W2) and join the % to them (W5)
    ("بنسبة 50% تقريبا", [("بنسبة", True), (" ", False), ("50% تقريبا", True)]),
    ("Latin only", [("Latin only", False)]),
    ("", []),
]


@pytest.mark.parametrize(("value", "pieces"), CASES, ids=[f"c{i}" for i in range(len(CASES))])
def test_direction_runs_give_the_preview_order_in_word(value, pieces):
    runs = direction_runs(value)
    assert runs == pieces
    assert word_matches_preview(runs)
    assert visual(runs, word=True) == visual(runs, word=False)


MORE = [
    "», Paris, 1851, t, III, p. 403-405",
    "DOLPHYN) بالميناء، عادت سبع",
    "ISBN 978-3-16-148410-0",
    "في 2020/10/05 م",
    "بين 10-20 سنة",
    "«Paris» 1851",
    "القيمة 3.5% فقط",
    "$100 دولار",
    "ص 45-50 و 60",
    "x² + y² = z²",
    "مجلد 2، ص 3",
    "بِسْمِ اللَّهِ Allah‌ 12",
    "שלום 12-15 عربي",
    "Qari v0.3: مشكوكة (في النص)",
]


@pytest.mark.parametrize("value", MORE, ids=[f"m{i}" for i in range(len(MORE))])
def test_mixed_texts_keep_the_preview_order(value):
    runs = direction_runs(value)
    assert "".join(piece for piece, _rtl in runs) == value
    assert word_matches_preview(runs), runs


ALPHABET = (
    list("بتثجحخ")
    + list("abcXYZ")
    + list("0123456789")
    + list("-+%$#°")
    + list("٠١٢")
    + list(",./:، ")
    + ["َ", "ّ", "‌", "­", "א", "‏", "‎", "؜"]
    + list(" \t()«»!?…–\"'")
)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_texts_keep_the_preview_order_in_both_paragraph_directions(seed):
    rng = random.Random(seed)
    for _trial in range(1500):
        value = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 24)))
        for base, level in (("rtl", 1), ("ltr", 0)):
            runs = direction_runs(value, base)
            assert "".join(piece for piece, _rtl in runs) == value
            assert word_matches_preview(runs, level), (base, value, runs)


# ====================================================================== the whole paragraph decides


def build(source, sheet=None, options=None, *, readings=None):
    book = book_model(source, sheet, title="كتابي", author="المؤلف", editorial=readings is not None)
    return build_docx(
        book,
        F.resolve("amiri", "amiri", "amiri"),
        options or WordOptions(),
        plan=PagePlan("live", 3, {}, {}),
        readings=readings,
        meta=DocMeta.fixed(),
        embed_fonts=False,
    )


def story(data: bytes, name: str) -> etree._Element:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return etree.fromstring(archive.read(name))


def paragraph_runs(paragraph: etree._Element) -> list[tuple[str, bool]]:
    """`(text, rtl)` of a paragraph's runs as Word reads them; a call or a note's marker is one strong
    right-to-left character (its «(» and «)» are `w:rtl` runs around the number)."""
    out: list[tuple[str, bool]] = []
    in_call = False
    for node in paragraph.iter(f"{W}r"):
        style = node.find(f"{W}rPr/{W}rStyle")
        if style is not None and style.get(f"{W}val") in ("FootnoteReference", "NkNoteNumber"):
            if not in_call:
                out.append((CALL, True))
            in_call = True
            continue
        in_call = False
        value = "".join(t.text or "" for t in node.iter(f"{W}t"))
        if value:
            out.append((value, node.find(f"{W}rPr/{W}rtl") is not None))
    return out


def test_every_paragraph_of_a_mixed_book_keeps_the_preview_order():
    source = document(
        heading("h1", "الفصل الأول"),
        para(
            "p1",
            "كتاب ",
            text("Ibn", "bold"),
            " Khaldun في التاريخ، ونظام Windows 10 الجديد مثل COVID-19 وغيرها",
            note("n1", "DE HAMMER, t. XII, p. 55-58."),
            " من ليلة 24-23 يونيه.",
        ),
        para(
            "p2",
            "انظر: ",
            text("Ibn Khaldun", "italic"),
            "، Muqaddimah، ص 45",
            note("n2", "«Histoire», Paris, 1851, t, III, p. 403-405"),
            " بنسبة 50% تقريبا",
        ),
        para("p3", "12-15 من الآيات", {"type": "hardBreak"}, "Windows 10 ثم ", text("vol. 2", "bold")),
    )
    result = build(source, {"front_matter": {"title_page": False, "contents": False}})
    assert schema.check(result.data) == []
    checked = 0
    for name in ("word/document.xml", "word/footnotes.xml"):
        for paragraph in story(result.data, name).iter(f"{W}p"):
            if paragraph.find(f".//{W}br") is not None:  # a line break starts a new stretch
                continue
            runs = paragraph_runs(paragraph)
            if runs:
                checked += 1
                assert word_matches_preview(runs), (name, runs)
    assert checked >= 5
    body = story(result.data, "word/document.xml").findall(f".//{W}p")[1]
    runs = paragraph_runs(body)
    # the space between the bold «Ibn» and «Khaldun» is left to right: the name is not swapped
    assert ("Ibn", False) in runs and next(r for r in runs if r[0].startswith(" Khaldun"))[1] is False


def test_an_uncertain_word_inside_a_latin_phrase_does_not_change_the_directions():
    class Reading:
        def __init__(self, word):
            self.word, self.readings, self.source_page = word, [], 3

    source = document(
        heading("h1", "الفصل"),
        para("p1", "انظر Ibn ", text("Khaldun Muqaddimah", "uncertain"), " 12-15 في الكتاب"),
    )
    plain = build(source)
    commented = build(
        source,
        options=WordOptions(comments=True),
        readings={("p1", None): [Reading("Khaldun"), Reading("Muqaddimah")]},
    )
    assert commented.stats["comments"] == 2

    def joined(data):
        runs = paragraph_runs(story(data, "word/document.xml").findall(f".//{W}p")[-1])
        out: list[tuple[str, bool]] = []
        for value, rtl in runs:
            for char in value:
                out.append((char, rtl))
        return out

    # the uncertain words are cut at their spaces for the comments: every character keeps its direction
    assert joined(commented.data) == joined(plain.data)
    assert word_matches_preview(
        paragraph_runs(story(commented.data, "word/document.xml").findall(f".//{W}p")[-1])
    )
    spaces = [rtl for char, rtl in joined(plain.data) if char == " "]
    assert spaces == [True, False, False, False, True, True]  # «Ibn Khaldun Muqaddimah 12-15»: LTR


def test_the_direction_of_a_piece_depends_on_the_text_around_it():
    """«10» after the bold «Windows» is left to right, alone it is a number of the right-to-left line."""
    source = document(heading("h1", "الفصل"), para("p1", "نظام ", text("Windows", "bold"), " 10 الجديد"))
    runs = paragraph_runs(story(build(source).data, "word/document.xml").findall(f".//{W}p")[-1])
    assert (" 10", False) in runs and word_matches_preview(runs)


def test_many_shielded_numbers_in_one_paragraph_lose_only_their_own_overrides():
    """Each «بنسبة 50%» needs the space before its digits left without `w:rtl` (W2, W5); the other spaces
    keep it, so they stay in the Arabic face as in the preview."""
    value = "بنسبة 50% تقريبا " * 200
    runs = direction_runs(value)
    assert word_matches_preview(runs)
    spaces = [rtl for piece, rtl in runs for char in piece if char == " "]
    assert spaces.count(True) == 400 and spaces.count(False) == 200
