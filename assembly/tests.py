"""Tests of the assembly app.

Part 1 exercises the pure pipeline (`assembly.pipeline`) on synthetic `PageIn` / `LineIn`: page
selection, typography (D37), paragraph geometry, seams, footnotes, headings and suggestions,
uncertain words, ids, stats, warnings and the document shape. Part 2 covers the services, the task,
the API, the placeholder views, the management command and the review / dashboard integration (D36).
"""

from __future__ import annotations

import json
import os
import re
from io import StringIO

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

import pytest

from assembly import pipeline, render, services
from assembly.models import AssemblyRun
from assembly.pipeline import (
    Block,
    LineIn,
    Meta,
    PageIn,
    Rich,
    Settings,
    assemble,
    breaks_between,
    decide_seam,
    join_pages,
    line_shape,
    normalize_settings,
    normalize_text,
    page_notes,
    quote_roles,
    select_pages,
    split_note_marker,
    split_paragraphs,
    text_measure,
)
from books.models import Book, Page
from editor.models import Manuscript, ManuscriptSnapshot
from ocr.models import Line
from processing.models import Preprocess, Region
from review import services as review_services
from review.models import LineRevision

ARABIC_INDIC = re.compile("[٠-٩]")

# ====================================================================== part 1: the pipeline

# A page's text block from x = 0.1 to x = 0.9 (M = 0.8): indent > 0.016, short > 0.048.
FULL = (0.1, 0.9)
INDENT = (0.1, 0.85)
SHORT = (0.5, 0.9)
CENTRED = (0.3, 0.7)


def box(y: float, edges: tuple[float, float] = FULL) -> tuple[float, float, float, float]:
    return (edges[0], y, edges[1], y + 0.02)


_ids = iter(range(1000, 10**6))


def ln(text, edges=FULL, *, y=None, kind="body", role="body", uncertain=(), boxed=True, id=None) -> LineIn:
    """A line; boxes stack down the page in creation order unless `y` is given."""
    line_id = id if id is not None else next(_ids)
    y = y if y is not None else (line_id % 40) * 0.02
    return LineIn(
        id=line_id,
        order=0,
        kind=kind,
        role=role,
        text=text,
        box=box(y, edges) if boxed else None,
        uncertain=list(uncertain),
    )


def pg(number, lines, status="reviewed", printed=None, id=None) -> PageIn:
    for order, line in enumerate(lines):
        line.order = order
    return PageIn(
        id=id if id is not None else number,
        number=number,
        printed=str(number) if printed is None else printed,
        status=status,
        reviewed=status in pipeline.REVIEWED_STATUSES,
        lines=list(lines),
    )


def run(pages, **settings) -> pipeline.Result:
    return assemble(pages, settings, {"id": 7, "title": "كتاب", "author": "مؤلف", "run_id": 31})


def blocks_of(result) -> list[dict]:
    return result.document["content"][1:]


def text_of(node) -> str:
    """Plain text of a block node: text runs, `[n]` for footnotes, `|` for page breaks."""
    out = []
    for item in node.get("content", []):
        if item["type"] == "text":
            out.append(item["text"])
        elif item["type"] == "footnote":
            out.append(f"[{item['attrs']['number']}]")
        elif item["type"] == "pageBreak":
            out.append("|")
    return "".join(out)


def notes_of(result) -> list[dict]:
    return [
        item for node in blocks_of(result) for item in node.get("content", []) if item["type"] == "footnote"
    ]


def codes(result) -> list[str]:
    return [w["code"] for w in result.warnings]


# ---------------------------------------------------------------- settings


def test_normalize_settings_defaults_and_bad_values():
    assert normalize_settings(None) == Settings()
    settings = normalize_settings(
        {
            "footnote_numbering": "volume",
            "include_unreviewed": "no",
            "strip_tatweel": 0,
            "seams": {"12": "join", "x": "join", "13": "glue", "014": "split"},
            "dismissed_suggestions": ["p12", "h9", "n3", 5, "p"],
        }
    )
    assert settings.footnote_numbering == "page"  # the default: as printed, per page
    assert settings.include_unreviewed is False and settings.strip_tatweel is False
    assert settings.seams == {"12": "join", "14": "split"}
    assert settings.dismissed_suggestions == frozenset({"p12", "h9"})
    assert settings.as_dict() == {
        "footnote_numbering": "page",
        "include_unreviewed": False,
        "strip_tatweel": False,
        "strip_running_heads": True,
        "seams": {"12": "join", "14": "split"},
        "dismissed_suggestions": ["h9", "p12"],
    }


# ---------------------------------------------------------------- 2.1 page selection


def test_select_pages_includes_reviewed_and_unreviewed_and_skips_the_rest_with_warnings():
    pages = [
        pg(1, [ln("أ")], "reviewed"),
        pg(2, [ln("ب")], "assembled"),
        pg(3, [ln("ج")], "ocr_done"),
        pg(4, [ln("د")], "layout_done"),
        pg(5, [ln("ه")], "error"),
        pg(6, [ln("و")], "excluded"),
        pg(7, [ln("ز")], "reviewed"),
        pg(8, [ln("ح")], "uploaded"),
    ]
    selection = select_pages(pages, Settings())
    assert [p.number for p in selection.included] == [1, 2, 3, 7]
    assert [p.number for p in selection.skipped] == [4, 5, 8]
    assert selection.gaps == {7: [4, 5]}  # the excluded page 6 is silent and breaks nothing
    assert [(w.code, w.page) for w in selection.warnings] == [
        ("page_unreviewed", 3),
        ("page_pending", 4),
        ("page_error", 5),
        ("page_pending", 8),
    ]
    assert all(w.severity == "warning" for w in selection.warnings)
    assert selection.warnings[0].message == "الصفحة 3 لم تُراجَع بعد؛ نصها كما قرأه التعرّف الآلي."


def test_select_pages_leaves_unreviewed_pages_out_when_asked():
    pages = [pg(1, [ln("أ")]), pg(2, [ln("ب")], "ocr_done"), pg(3, [ln("ج")])]
    selection = select_pages(pages, Settings(include_unreviewed=False))
    assert [p.number for p in selection.included] == [1, 3] and selection.gaps == {3: [2]}
    assert selection.warnings[0].message == "الصفحة 2 لم تُراجَع بعد فلم تُضمَّن."


def test_a_skipped_page_breaks_the_join_chain_and_shows_a_missing_seam():
    pages = [
        pg(12, [ln("نص يستمر بلا نقطة")]),
        pg(13, [ln("قيد المعالجة")], "preprocessed"),
        pg(14, [ln("تتمة الفقرة.")]),
    ]
    result = run(pages)
    assert result.seams == [
        {
            "page": 14,
            "from_page": 12,
            "mode": "missing",
            "decision": "auto",
            "reason": "skipped_page",
            "skipped": [13],
        }
    ]
    assert len(blocks_of(result)) == 2 and result.stats["joins"] == 0
    assert result.stats["pages_included"] == 2 and result.stats["pages_skipped"] == 1


# ---------------------------------------------------------------- 2.2 typography (D37)


def test_punctuation_repair_on_the_real_line():
    assert (
        normalize_text("بـــين سلوق وتاكنست .وتاكنست هي قرية تاكنس")
        == "بين سلوق وتاكنست. وتاكنست هي قرية تاكنس"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("قال :إن الكتاب ،وهو ؛ثم ؟لماذا !نعم", "قال: إن الكتاب، وهو؛ ثم؟ لماذا! نعم"),
        ("الكلمة . والأخرى ، ثم ؛ هنا : هناك ؟ نعم !", "الكلمة. والأخرى، ثم؛ هنا: هناك؟ نعم!"),
        ("( توكرة ) و[ كذا ]", "(توكرة) و[كذا]"),
        ("قال : « كان لباتوس » .", "قال: «كان لباتوس»."),
        ("شتاء،كما النصوص.وقد", "شتاء، كما النصوص. وقد"),
        ("سوسة ) .ومن هنا", "سوسة). ومن هنا"),
        ("سنة 544 ق.م واستمر", "سنة 544 ق.م واستمر"),
        ("التد.لي", "التد.لي"),
        ("كلمة    أخرى\tثالثة", "كلمة أخرى ثالثة"),
    ],
)
def test_marks_spaces_and_brackets(raw, expected):
    assert normalize_text(raw) == expected


def test_opening_brackets_and_quotes_at_a_word_start_stay():
    assert (
        normalize_text('قال (الأول) «الثاني» "الثالث" [الرابع]') == 'قال (الأول) «الثاني» "الثالث" [الرابع]'
    )


def test_quotes_printed_with_the_closing_glyph_on_both_sides():
    assert normalize_text("تحت قاع » حمادة مرزق » .") == "تحت قاع »حمادة مرزق»."
    assert quote_roles("» في أواخر العصر") == {0: "open"}
    # a stray closing glyph followed by punctuation (the quote opened in an earlier paragraph) closes
    assert normalize_text("في القديم ” .") == "في القديم”."
    assert normalize_text("يلي : ” وبها آثار") == "يلي: ”وبها آثار"


def test_tatweel_is_removed_inside_words_and_kept_as_a_dash_or_a_prefix():
    assert normalize_text("بـــين وآثرـ بعد") == "بين وآثر بعد"
    assert normalize_text("1 ـ كتاب وصلحائها ـ رحلة") == "1 ـ كتاب وصلحائها ـ رحلة"
    assert normalize_text("المسماة بـ » الهروج «") == "المسماة بـ »الهروج «"
    assert normalize_text("الـ 13 مصدرًا وكتابـ 2") == "الـ 13 مصدرًا وكتاب 2"
    assert normalize_text("بـــين", strip_tatweel_marks=False) == "بـــين"


def test_fully_vowelled_text_is_unchanged():
    text = "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ، الْحَمْدُ لِلَّهِ رَبِّ الْعَالَمِينَ."
    assert normalize_text(text) == text
    assert normalize_text("قَالَ .وَكَتَبَ") == "قَالَ. وَكَتَبَ"


def test_digits_follow_the_book_digit_style():
    assert normalize_text("سنة ١٩٦٦ و۱۹۶۷") == "سنة 1966 و1967"
    assert normalize_text("سنة 1966", digit_style="arabic_indic") == "سنة ١٩٦٦"
    result = assemble(
        [pg(1, [ln("في سنة ١٩٦٦ (١)"), ln("(١) حاشية ٢", kind="footnote")])],
        {},
        {"id": 1, "digit_style": "western"},
    )
    assert text_of(blocks_of(result)[0]) == "في سنة 1966[1]"
    note = notes_of(result)[0]
    assert note["content"] == [{"type": "text", "text": "حاشية 2"}] and note["attrs"]["marker"] == "١"


def test_a_mark_at_a_line_start_goes_to_the_end_of_the_previous_line():
    page = pg(1, [ln("قرية تاكنس الحالية", y=0.1), ln(".ويؤكد المقدسي هذا", y=0.12)])
    assert text_of(blocks_of(run([page]))[0]) == "قرية تاكنس الحالية. ويؤكد المقدسي هذا"


def test_a_mark_at_a_block_start_goes_to_the_end_of_the_previous_block():
    page = pg(1, [ln("قرية تاكنس الحالية", SHORT, y=0.1), ln(".ويؤكد لنا المقدسي", INDENT, y=0.12)])
    first, second = blocks_of(run([page]))
    assert text_of(first) == "قرية تاكنس الحالية." and text_of(second) == "ويؤكد لنا المقدسي"


def test_a_leading_mark_is_never_given_to_a_heading_or_to_nothing():
    page = pg(1, [ln("عنوان", CENTRED, role="heading", y=0.1), ln(".نص يبدأ بنقطة", INDENT, y=0.12)])
    heading, paragraph = blocks_of(run([page]))
    assert text_of(heading) == "عنوان" and text_of(paragraph) == "نص يبدأ بنقطة"
    assert text_of(blocks_of(run([pg(1, [ln("،أول الكتاب")])]))[0]) == "أول الكتاب"


def test_notes_of_a_book_without_body_text_get_a_paragraph_of_their_own():
    result = run([pg(1, [ln("(1) حاشية وحيدة", kind="footnote", id=77)])])
    (node,) = blocks_of(result)
    assert node["attrs"]["id"] == "p77" and node["attrs"]["sourceLineIds"] == [77]
    assert node["content"][0]["type"] == "footnote" and node["content"][0]["attrs"]["orphan"] is True


def test_punctuation_moves_keep_uncertain_marks_on_their_words():
    rich = Rich.from_line(ln("وتاكنست .وتاكنست هي", uncertain=[1]))
    rich = pipeline.normalize_rich(rich)
    assert rich.text == "وتاكنست. وتاكنست هي"
    marked = "".join(c for c, m in zip(rich.text, rich.meta, strict=True) if m.uncertain)
    assert marked == ".وتاكنست"  # the moved mark keeps the word's flag; the inline node marks words only
    nodes = pipeline.inline_content(rich)
    assert nodes == [
        {"type": "text", "text": "وتاكنست"},
        {"type": "text", "text": ".", "marks": [{"type": "uncertain"}]},
        {"type": "text", "text": " "},
        {"type": "text", "text": "وتاكنست", "marks": [{"type": "uncertain"}]},
        {"type": "text", "text": " هي"},
    ]


# ---------------------------------------------------------------- 2.3 paragraphs


def test_text_measure_uses_the_lines_at_least_60_percent_as_wide_as_the_widest():
    lines = [ln("أ", FULL), ln("ب", (0.12, 0.9)), ln("ج", SHORT), ln("د", (0.1, 0.88))]
    measure = text_measure(lines)
    assert measure.left == pytest.approx(0.1) and measure.right == pytest.approx(0.9)
    assert measure.width == pytest.approx(0.8)
    assert text_measure([ln("أ", boxed=False)]) is None


def test_line_shape_indented_short_and_centred():
    measure = text_measure([ln("x", FULL), ln("y", FULL)])
    assert line_shape(box(0, FULL), measure) == pipeline.Shape(False, False, False)
    assert line_shape(box(0, INDENT), measure) == pipeline.Shape(True, False, False)
    assert line_shape(box(0, SHORT), measure) == pipeline.Shape(False, True, False)
    assert line_shape(box(0, CENTRED), measure) == pipeline.Shape(True, True, True)
    assert line_shape(box(0, (0.4, 0.8)), measure).centred is False  # gaps 0.1 and 0.3: not balanced
    assert line_shape(None, measure) is None and line_shape(box(0), None) is None


def test_split_paragraphs_by_indent_short_line_and_centred_line():
    page = pg(
        1,
        [
            ln("الأول يبدأ بمسافة", INDENT, y=0.10, id=1),
            ln("ويستمر السطر كاملًا", FULL, y=0.12, id=2),
            ln("وينتهي قصيرًا", SHORT, y=0.14, id=3),
            ln("فقرة ثانية كاملة", FULL, y=0.16, id=4),
            ln("وسطر آخر كامل", FULL, y=0.18, id=5),
            ln("سطر في الوسط", CENTRED, y=0.20, id=6),
            ln("فقرة ثالثة بمسافة", INDENT, y=0.22, id=7),
            ln("وتتمتها", FULL, y=0.24, id=8),
        ],
    )
    blocks = split_paragraphs(page)
    assert [b.line_ids for b in blocks] == [[1, 2, 3], [4, 5], [6], [7, 8]]
    assert [b.id for b in blocks] == ["p1", "p4", "p6", "p7"]
    assert blocks[0].rich.text == "الأول يبدأ بمسافة ويستمر السطر كاملًا وينتهي قصيرًا"


def test_lines_with_the_same_indent_form_one_indented_block():
    lines = [
        ln("سطر كامل", FULL, y=0.10, id=1),
        ln("ينتهي قصيرًا", SHORT, y=0.12, id=2),
        ln("بند مزاح يبدأ هنا،", (0.1, 0.84), y=0.14, id=3),
        ln("ويستمر بالمسافة نفسها", (0.1, 0.845), y=0.16, id=4),
        ln("وينتهي هنا", (0.5, 0.84), y=0.18, id=5),
        ln("فقرة كاملة", FULL, y=0.20, id=6),
        ln("وتتمتها", FULL, y=0.22, id=7),
    ]
    assert [b.line_ids for b in split_paragraphs(pg(1, lines))] == [[1, 2], [3, 4, 5], [6, 7]]
    # the spec rule alone (no measure) would start a paragraph at every indented line
    measure = text_measure(lines)
    shapes = [line_shape(line.box, measure) for line in lines]
    assert breaks_between(lines[2], lines[3], shapes[2], shapes[3]) is True
    assert breaks_between(lines[2], lines[3], shapes[2], shapes[3], measure) is False


def test_lines_without_boxes_break_on_terminal_punctuation_and_role_changes():
    a, b = ln("انتهت الجملة.", boxed=False), ln("جملة جديدة", boxed=False)
    assert breaks_between(a, b, None, None) is True
    c = ln("تستمر الجملة", boxed=False)
    assert breaks_between(c, b, None, None) is False
    heading = ln("عنوان", boxed=False, role="heading")
    assert breaks_between(c, heading, None, None) is True
    page = pg(
        1,
        [
            ln("سطر بلا صندوق", boxed=False, id=11),
            ln("يتبعه سطر آخر:", boxed=False, id=12),
            ln("ثم فقرة جديدة", boxed=False, id=13),
        ],
    )
    assert [blk.line_ids for blk in split_paragraphs(page)] == [[11, 12], [13]]


def test_line_roles_make_headings_and_consecutive_heading_lines_one_heading():
    page = pg(
        3,
        [
            ln("الفصل الأول", CENTRED, role="heading", y=0.10, id=812),
            ln("في جغرافية برقة", CENTRED, role="heading", y=0.12, id=813),
            ln("مدخل", CENTRED, role="subheading", y=0.14, id=814),
            ln("نص الفقرة الأولى", INDENT, y=0.16, id=815),
        ],
    )
    blocks = split_paragraphs(page)
    assert [(b.kind, b.level, b.id, b.line_ids) for b in blocks] == [
        ("heading", 1, "h812", [812, 813]),
        ("heading", 2, "h814", [814]),
        ("paragraph", 0, "p815", [815]),
    ]
    result = run([page])
    nodes = blocks_of(result)
    assert nodes[0]["type"] == "heading" and nodes[0]["attrs"]["level"] == 1
    assert text_of(nodes[0]) == "الفصل الأول في جغرافية برقة"
    assert nodes[1]["attrs"]["level"] == 2 and "suggestedRole" not in nodes[1]["attrs"]
    assert "no_headings" not in codes(result)


def test_footnote_lines_are_not_body_text():
    page = pg(1, [ln("متن"), ln("حاشية بلا علامة", kind="footnote")])
    assert [b.rich.text for b in split_paragraphs(page)] == ["متن"]


# ---------------------------------------------------------------- 2.4 seams


def _two_pages(last_edges=FULL, first_edges=FULL, last_text="نهاية الصفحة", first_text="بداية التالية", **kw):
    return [
        pg(12, [ln("فقرة", FULL, y=0.1, id=815), ln(last_text, last_edges, y=0.9, id=816)], **kw),
        pg(
            13,
            [ln(first_text, first_edges, y=0.1, id=901), ln("وتتمتها", FULL, y=0.12, id=902)],
            printed="13",
        ),
    ]


def test_a_paragraph_cut_by_a_page_break_is_joined_with_a_page_break_node():
    result = run(_two_pages())
    assert result.seams == [
        {"page": 13, "from_page": 12, "mode": "join", "decision": "auto", "reason": "geometry"}
    ]
    (node,) = blocks_of(result)
    assert node["attrs"]["id"] == "p815" and node["attrs"]["sourcePages"] == [12, 13]
    assert node["attrs"]["sourceLineIds"] == [815, 816, 901, 902]
    assert node["content"] == [
        {"type": "text", "text": "فقرة نهاية الصفحة"},
        {"type": "pageBreak", "attrs": {"page": 13, "printed": "13"}},
        {"type": "text", "text": " بداية التالية وتتمتها"},
    ]
    assert result.stats["joins"] == 1 and result.stats["paragraphs"] == 1


@pytest.mark.parametrize(
    ("last", "first", "mode"),
    [(SHORT, FULL, "split"), (FULL, INDENT, "split"), (FULL, FULL, "join")],
)
def test_seams_by_geometry(last, first, mode):
    result = run(_two_pages(last, first))
    assert result.seams[0]["mode"] == mode and result.seams[0]["reason"] == "geometry"


def test_seams_without_boxes_follow_the_punctuation():
    def pages(text):
        return [pg(1, [ln(text, boxed=False)]), pg(2, [ln("تتمة", boxed=False)])]

    joined = run(pages("جملة لم تنته"))
    assert joined.seams[0]["mode"] == "join" and joined.seams[0]["reason"] == "punctuation"
    split = run(pages("جملة انتهت."))
    assert split.seams[0]["mode"] == "split" and split.seams[0]["reason"] == "punctuation"


def test_a_heading_on_either_side_splits_the_seam():
    pages = [pg(1, [ln("نص بلا نهاية")]), pg(2, [ln("عنوان", CENTRED, role="heading"), ln("نص")])]
    assert run(pages).seams[0] == {
        "page": 2,
        "from_page": 1,
        "mode": "split",
        "decision": "auto",
        "reason": "heading",
    }


def test_seam_overrides_win():
    forced_split = run(_two_pages(), seams={"13": "split"})
    assert forced_split.seams[0] == {
        "page": 13,
        "from_page": 12,
        "mode": "split",
        "decision": "override",
        "reason": "geometry",
    }
    assert len(blocks_of(forced_split)) == 2
    forced_join = run(_two_pages(SHORT, INDENT), seams={"13": "join"})
    assert forced_join.seams[0]["mode"] == "join" and forced_join.seams[0]["decision"] == "override"
    assert len(blocks_of(forced_join)) == 1


def test_decide_seam_without_blocks_and_join_pages_across_three_pages():
    assert decide_seam(None, None, "join", 2, 1)["mode"] == "split"
    pages = [
        pg(1, [ln("أول", FULL, y=0.9, id=1)]),
        pg(2, [ln("وسط", FULL, y=0.5, id=2)]),
        pg(3, [ln("آخر", FULL, y=0.1, id=3)]),
    ]
    blocks, seams = join_pages([(p, split_paragraphs(p)) for p in pages], {}, {})
    assert [s["mode"] for s in seams] == ["join", "join"]
    assert len(blocks) == 1 and blocks[0].pages == [1, 2, 3] and blocks[0].line_ids == [1, 2, 3]
    assert blocks[0].rich.text == f"أول{pipeline.PB} وسط{pipeline.PB} آخر"


def test_an_empty_page_splits_both_seams_and_is_reported():
    pages = [pg(1, [ln("نص بلا نهاية")]), pg(2, []), pg(3, [ln("تتمة")])]
    result = run(pages)
    assert [s["mode"] for s in result.seams] == ["split", "split"]
    assert ("empty_page", 2, "info") in [(w["code"], w["page"], w["severity"]) for w in result.warnings]


# ---------------------------------------------------------------- 2.5 footnotes


@pytest.mark.parametrize(
    ("body", "note", "marker"),
    [
        ("قال المؤرخ (1) كذا", "(1) حاشية", "1"),
        ("قال المؤرخ (١) كذا", "(١) حاشية", "١"),
        ("قال المؤرخ [٢] كذا", "٢ - حاشية", "٢"),
        ("قال المؤرخ² كذا", "2. حاشية", "²"),
        ("قال الفيل٢ مرحلة", "٢، حاشية", "٢"),
        ("قال الفيل ٢ مرحلة", "٢ حاشية", "٢"),
        ("قال المؤرخ (*) كذا", "* حاشية", "*"),
        ("مرحلة » ا . ثم", "1 حاشية", "ا"),
        ("وهي برقة »١ ، ثم", "(١) حاشية", "١"),
        ("المعاصر ...الخ .1", "(1) - حاشية", "1"),
    ],
)
def test_footnote_markers_in_every_style_link_to_their_note(body, note, marker):
    result = run([pg(1, [ln(body), ln(note, kind="footnote")])])
    (footnote,) = notes_of(result)
    assert footnote["attrs"]["marker"] == marker and footnote["attrs"]["orphan"] is False
    assert footnote["content"] == [{"type": "text", "text": "حاشية"}]
    assert footnote["attrs"]["number"] == 1 and footnote["attrs"]["sourcePage"] == 1
    assert "note_orphan" not in codes(result) and "marker_unmatched" not in codes(result)
    text = text_of(blocks_of(result)[0])
    assert "[1]" in text and not re.search(r"\(\s*[1١*]\s*\)|[²٢]", text.replace("[1]", ""))


def test_split_note_marker_patterns():
    assert split_note_marker("(3) انظر") == ("3", 4)
    assert split_note_marker("[١٢] انظر")[0] == "١٢"
    assert split_note_marker("12- انظر")[0] == "12"
    assert split_note_marker("٣. انظر")[0] == "٣"
    assert split_note_marker("** انظر")[0] == "**"
    assert split_note_marker("(1)")[0] == "1"
    assert split_note_marker("1966 م كذا") == (None, 0)
    assert split_note_marker("انظر ص 12") == (None, 0)


def test_multi_line_notes_and_notes_continuing_on_the_next_page():
    pages = [
        pg(
            1,
            [
                ln("المتن (١) والمتن (٢) انتهى.", id=10),
                ln("(١) الحاشية الأولى", kind="footnote", id=11),
                ln("(٢) الحاشية الثانية تبدأ", kind="footnote", id=12),
                ln("وتستمر هنا", kind="footnote", id=13),
            ],
        ),
        pg(
            2,
            [
                ln("متن الصفحة الثانية (١)", INDENT, id=20),
                ln("وتنتهي في الصفحة التالية", kind="footnote", id=21),
                ln("(١) حاشية الصفحة الثانية", kind="footnote", id=22),
            ],
        ),
    ]
    result = run(pages)
    notes = notes_of(result)
    assert [n["attrs"]["id"] for n in notes] == ["n11", "n12", "n22"]
    assert notes[1]["attrs"]["sourceLineIds"] == [12, 13, 21] and notes[1]["attrs"]["sourcePage"] == 1
    assert notes[1]["content"][0]["text"] == "الحاشية الثانية تبدأ وتستمر هنا وتنتهي في الصفحة التالية"
    assert notes[2]["attrs"]["sourcePage"] == 2 and notes[2]["attrs"]["sourceLineIds"] == [22]


def test_a_continuation_without_a_note_before_it_starts_an_orphan_note():
    page = pg(1, [ln("متن بلا علامة"), ln("سطر حاشية بلا رقم", kind="footnote", id=31)])
    result = run([page])
    (note,) = notes_of(result)
    assert (
        note["attrs"]["orphan"] is True and note["attrs"]["marker"] is None and note["attrs"]["id"] == "n31"
    )
    (warning,) = [w for w in result.warnings if w["code"] == "note_orphan"]
    assert warning["lineIds"] == [31] and warning["severity"] == "warning" and warning["page"] == 1


def test_orphan_notes_go_to_the_end_of_their_page_text_and_unmatched_markers_warn():
    pages = [
        pg(
            1,
            [
                # two loose calls for one note: which one is its call cannot be told (D85 pairs one to one)
                ln("متن الصفحة الأولى (٣) بلا حاشية (٥) مقابلة ثم الفيل٤ أيضًا ثم 7 رقم عادي", id=40),
                ln("(١) حاشية بلا علامة", kind="footnote", id=41),
            ],
        ),
        pg(2, [ln("متن الصفحة الثانية.", id=50)]),
    ]
    result = run(pages)
    (node,) = blocks_of(result)  # joined: the first page does not end with a mark
    assert node["content"][-3]["type"] == "footnote" and node["content"][-3]["attrs"]["orphan"] is True
    assert node["content"][-2]["type"] == "pageBreak"
    unmatched = [w for w in result.warnings if w["code"] == "marker_unmatched"]
    assert [w["message"] for w in unmatched] == [
        "علامة الحاشية «3» في الصفحة 1 بلا حاشية مقابلة.",
        "علامة الحاشية «5» في الصفحة 1 بلا حاشية مقابلة.",
        "علامة الحاشية «4» في الصفحة 1 بلا حاشية مقابلة.",
    ]
    assert all(w["blockId"] == "p40" and w["lineIds"] == [40] for w in unmatched)
    orphan = next(w for w in result.warnings if w["code"] == "note_orphan")
    assert orphan["message"] == "الحاشية «1» في الصفحة 1 بلا علامة في المتن؛ أُلحقت بآخر فقرة من الصفحة."
    assert "(3)" in text_of(node) and "الفيل4" in text_of(node) and " 7 " in text_of(node)  # text stays


def test_a_page_without_notes_never_warns_about_markers():
    result = run([pg(1, [ln("متن فيه (١) وعلامة")])])
    assert "marker_unmatched" not in codes(result) and notes_of(result) == []


def test_bracketed_markers_win_over_standalone_numbers():
    page = pg(1, [ln("سار 1 ميلًا ثم قال (1) كذا"), ln("(1) الحاشية", kind="footnote")])
    assert text_of(blocks_of(run([page]))[0]) == "سار 1 ميلًا ثم قال[1] كذا"


def test_each_note_takes_the_first_unused_matching_candidate():
    page = pg(
        1,
        [
            ln("الأول (1) والثاني (2) وتكرار (1) آخر"),
            ln("(1) أولى", kind="footnote"),
            ln("(1) ثانية", kind="footnote"),
        ],
    )
    result = run([page])
    # D87: the second note's «(1)» is out of the page's sequence: it is note 2, and takes «(2)»
    assert text_of(blocks_of(result)[0]) == "الأول[1] والثاني[2] وتكرار (1) آخر"
    assert codes(result).count("marker_unmatched") == 1  # the second (1)


def test_several_orphans_of_a_page_keep_the_order_of_their_notes():
    # Book 13 page 8: two notes whose markers the OCR lost were appended in reverse order.
    page = pg(
        1,
        [
            ln("متن الصفحة بلا علامات", id=60),
            ln("(2) أبو الفداء", kind="footnote", id=61),
            ln("(3) الإدريسي", kind="footnote", id=62),
        ],
    )
    result = run([page])
    assert [n["attrs"]["id"] for n in notes_of(result)] == ["n61", "n62"]
    assert [n["attrs"]["marker"] for n in notes_of(result)] == ["2", "3"]
    assert [w["lineIds"] for w in result.warnings if w["code"] == "note_orphan"] == [[61], [62]]


def test_a_number_after_a_closing_quote_is_a_marker_and_warns_when_unmatched():
    # Book 13 page 6: «دينار » ١ ، … برقة » ١ ،» with one note: the first links, the second warns.
    page = pg(
        1,
        [
            ln("«ألف دينار » ١ ، وهناك إشارة وهي برقة » ١ ، فأرض", id=70),
            ln("ا ابن عبد الحكم", kind="footnote", id=71),
        ],
    )
    result = run([page])
    assert text_of(blocks_of(result)[0]) == "«ألف دينار»[1]، وهناك إشارة وهي برقة» 1، فأرض"
    (warning,) = [w for w in result.warnings if w["code"] == "marker_unmatched"]
    assert warning["message"] == "علامة الحاشية «1» في الصفحة 1 بلا حاشية مقابلة."


# ---------------------------------------------------------------- D82: calls the models misread


@pytest.mark.parametrize(
    "body, read",
    [
        ("ودان جيشا بقيادة بسر بن أبي أرطاة(ا) ، ففتحها سنة ٢٢ هـ", "(ا)"),  # book 29 p. 11, glued
        ("صفوان بن أبي مالك (أ) والي", "(أ)"),  # p. 34
        ("وبين زوجه ( خَوْد ) (”) وامتنعت", "(”)"),  # p. 160: a quote stroke
        ("وكان قائدهم ( مراد الارنؤوطي ) ( “ ) .", "(“)"),  # p. 195
        ("من قبل مسلمة بن مخلد ( ) .", "()"),  # p. 22: nothing inside the brackets
    ],
)
def test_a_bracketed_lookalike_glyph_is_the_call_of_the_one_note_it_can_only_be(body, read):
    """The models write the small raised «(١)» as an alef, a quote stroke or empty brackets (D82,
    book 29: 12 letters, 8 empty pairs, 5 quote strokes among 63 orphans)."""
    result = run([pg(1, [ln(body, id=10), ln("(١) حاشية", kind="footnote", id=11)])])
    (note,) = notes_of(result)
    assert note["attrs"]["orphan"] is False and note["attrs"]["marker"] == "١"
    text = text_of(blocks_of(result)[0])
    assert "[1]" in text and not re.search(r"[(\[]\s*[اأ”“]?\s*[)\]]", text)
    (warning,) = [w for w in result.warnings if w["code"] == "note_call_repaired"]
    assert (
        warning["message"]
        == f"علامة الحاشية «1» في الصفحة 1 قُرئت «{read}» في المتن؛ رُبطت بالحاشية؛ تحقّق منها."
    )
    assert warning["marker"] == "1" and warning["lineIds"] == [11] and warning["blockId"] == "p10"
    assert "note_orphan" not in codes(result) and "marker_unmatched" not in codes(result)


def test_the_glued_lookalike_call_sits_right_after_its_word():
    result = run(
        [pg(1, [ln("بقيادة بسر بن أبي أرطاة(ا) ، ففتحها", id=10), ln("(١) حاشية", kind="footnote")])]
    )
    assert text_of(blocks_of(result)[0]) == "بقيادة بسر بن أبي أرطاة[1]، ففتحها"


def test_a_lookalike_never_steals_a_call_and_never_guesses_between_notes():
    # note (١) has its own call: the «(ا)» stays text, silently (it may be a real letter)
    called = run([pg(1, [ln("الأول (١) ثم (ا) بعده", id=10), ln("(١) حاشية", kind="footnote", id=11)])])
    assert text_of(blocks_of(called)[0]) == "الأول[1] ثم (ا) بعده"
    assert "note_call_repaired" not in codes(called) and "marker_unmatched" not in codes(called)
    # two notes without a call and one lookalike: which one it is cannot be told
    two = run(
        [
            pg(
                1,
                [
                    ln("كلام (”) وكلام آخر", id=20),
                    ln("(١) الأولى", kind="footnote", id=21),
                    ln("(٢) الثانية", kind="footnote", id=22),
                ],
            )
        ]
    )
    assert [n["attrs"]["orphan"] for n in notes_of(two)] == [True, True]
    assert "note_call_repaired" not in codes(two)
    # two lookalikes for two notes pair in reading order
    paired = run(
        [
            pg(
                1,
                [
                    ln("الأول ( ) ثم الثاني (ا) هنا", id=30),
                    ln("(١) الأولى", kind="footnote", id=31),
                    ln("(٢) الثانية", kind="footnote", id=32),
                ],
            )
        ]
    )
    assert text_of(blocks_of(paired)[0]) == "الأول[1] ثم الثاني[2] هنا"
    assert codes(paired).count("note_call_repaired") == 2


def test_a_lookalike_must_lie_where_its_note_call_would_be():
    """Notes (١) (٢): the (١) call is read; the lookalike before it cannot be (٢)'s call."""
    page = pg(
        1,
        [
            ln("مقدّمة (ا) ثم الأول (١) ونهاية", id=40),
            ln("(١) الأولى", kind="footnote", id=41),
            ln("(٢) الثانية", kind="footnote", id=42),
        ],
    )
    result = run([page])
    assert [n["attrs"]["orphan"] for n in notes_of(result)] == [False, True]
    assert "note_call_repaired" not in codes(result)
    after = pg(
        1,
        [
            ln("الأول (١) ثم كلام (ا) ونهاية", id=50),
            ln("(١) الأولى", kind="footnote", id=51),
            ln("(٢) الثانية", kind="footnote", id=52),
        ],
    )
    result = run([after])
    assert text_of(blocks_of(result)[0]) == "الأول[1] ثم كلام[2] ونهاية"


def test_a_lookalike_at_a_block_start_is_a_marker_left_in_the_body_not_a_call():
    page = _stray_page(
        ln("متن الصفحة بلا علامة.", id=60),
        ln("(أ) البند الأول من قائمة", id=61),
        ln("(١) حاشية", kind="footnote", id=62),
    )
    result = run([page])
    assert len(blocks_of(result)) == 2  # the lettered item is its own paragraph
    (note,) = notes_of(result)
    assert note["attrs"]["orphan"] is True and "note_call_repaired" not in codes(result)


def test_a_note_without_a_marker_takes_a_lookalike_only_after_the_strong_calls():
    """Book 29 p. 84: the footnote's own «(١)» was dropped too; the body has «(ا)» alone."""
    alone = run([pg(1, [ln("وُرُو بن سعيد (ا)", id=70), ln("حاشية بلا علامة", kind="footnote", id=71)])])
    (note,) = notes_of(alone)
    assert note["attrs"]["orphan"] is False and note["attrs"]["marker"] is None
    (warning,) = [w for w in alone.warnings if w["code"] == "note_call_repaired"]
    assert (
        warning["message"]
        == "علامة حاشية بلا علامة في الصفحة 1 قُرئت «(ا)» في المتن؛ رُبطت بالحاشية؛ تحقّق منها."
    )
    both = run([pg(1, [ln("كلام (ا) ثم (١) هنا", id=72), ln("حاشية بلا علامة", kind="footnote", id=73)])])
    assert text_of(blocks_of(both)[0]) == "كلام (ا) ثم[1] هنا"  # D74's positional link wins
    assert "note_marker_missing" in codes(both) and "note_call_repaired" not in codes(both)


@pytest.mark.parametrize(
    "body, note, read",
    [
        ("ولسّى عبيدة بن عبد الرحمن (١١) على", "(١) حاشية", "11"),  # book 29 p. 33
        ("وثلاثة أشهر (٢١). وتولى الحكم", "(٢) حاشية", "21"),  # p. 73
        ("قال في الكتاب [٣١] كذا", "[3] حاشية", "31"),
    ],
)
def test_a_call_read_with_a_one_hung_on_it_is_the_uncalled_one_digit_note_of_its_page(body, note, read):
    result = run([pg(1, [ln(body, id=80), ln(note, kind="footnote", id=81)])])
    (footnote,) = notes_of(result)
    assert footnote["attrs"]["orphan"] is False and footnote["attrs"]["marker"] == note[1]
    text = text_of(blocks_of(result)[0])
    assert "[1]" in text and read not in text and "١" not in text
    (warning,) = [w for w in result.warnings if w["code"] == "note_call_repaired"]
    assert f"قُرئت «{read}» في المتن" in warning["message"] and warning["lineIds"] == [81]
    assert "marker_unmatched" not in codes(result) and "note_orphan" not in codes(result)


def test_the_two_digit_repair_stays_narrow():
    own = run(
        [
            pg(
                1,
                [
                    ln("الأول (١١) ثم", id=90),
                    ln("(١) الأولى", kind="footnote", id=91),
                    ln("(١١) الحادية عشرة", kind="footnote", id=92),
                ],
            )
        ]
    )
    # D87: «(١١)» after «(١)» is out of the page's sequence: a reference in note 1, whose call «(١١)» is
    assert [(n["attrs"]["marker"], n["attrs"]["orphan"]) for n in notes_of(own)] == [("١", False)]
    assert notes_of(own)[0]["attrs"]["sourceLineIds"] == [91, 92]
    # «(٤١)» (above the positional range: it may be a page reference; the call pass reads the ink), a
    # three-digit «(١١٧)», a bare «١١», a glued «كتاب١١»: none is taken for the call
    for body in ("كلام (٤١) هنا", "كلام (١١٧) هنا", "كلام ١١ هنا", "كتاب١١ هنا"):
        result = run([pg(1, [ln(body, id=93), ln("(٢) حاشية", kind="footnote", id=94)])])
        (footnote,) = notes_of(result)
        assert footnote["attrs"]["orphan"] is True, body
        assert "note_call_repaired" not in codes(result), body
    # the page's one loose small bracketed call and its one note without a call pair (D85, `leftover_calls`;
    # book 29 p. 59: «زناتة (١)» read «(٦)»)
    for body in ("كلام (١) هنا", "كلام (٦) هنا"):
        result = run([pg(1, [ln(body, id=93), ln("(٢) حاشية", kind="footnote", id=94)])])
        (footnote,) = notes_of(result)
        assert footnote["attrs"]["orphan"] is False, body
        assert codes(result).count("note_call_repaired") == 1, body
    # note (١) has its call: a leftover «(١١)» is not taken for it (it warns as before)
    called = run([pg(1, [ln("الأول (١) ثم (١١) بعده", id=95), ln("(١) حاشية", kind="footnote", id=96)])])
    assert text_of(blocks_of(called)[0]) == "الأول[1] ثم (11) بعده"
    assert codes(called).count("marker_unmatched") == 1 and "note_call_repaired" not in codes(called)
    # never across pages
    pages = [
        pg(1, [ln("كلام (١١) هنا", id=97)]),
        pg(2, [ln("متن الثانية.", id=98), ln("(١) حاشية", kind="footnote", id=99)]),
    ]
    result = run(pages)
    (footnote,) = notes_of(result)
    assert footnote["attrs"]["orphan"] is True and "note_call_repaired" not in codes(result)


def test_a_year_at_the_start_of_a_footnote_line_is_not_a_marker():
    """Book 29 p. 82: the note's second line starts «١ م ه ولم ترض» (a year); it read as a second
    note «١» and hung as an orphan. A bare number followed by an era sign continues the note."""
    assert split_note_marker("١ م ه ولم ترض زناتة") == (None, 0)
    assert split_note_marker("٦٤ ه وعمره ٩٠ سنة") == (None, 0)
    assert split_note_marker("1 هـ ، ثم") == (None, 0)
    assert split_note_marker("(١) م ه كلام")[0] == "١"  # bracketed: a marker whatever follows
    assert split_note_marker("١ ملك كلام")[0] == "١"  # a word, not an era sign
    page = pg(
        1,
        [
            ln("فلفل وباديس بن المنصور (١) دامت نحو سنتين", id=100),
            ln("(١) باديس بن المنصور لما انتقل الى مصر سنة", kind="footnote", id=101),
            ln("١ م ه ولم ترض زناتة بهذا التعيين .", kind="footnote", id=102),
        ],
    )
    result = run([page])
    (note,) = notes_of(result)
    assert note["attrs"]["orphan"] is False and note["attrs"]["sourceLineIds"] == [101, 102]
    assert "note_orphan" not in codes(result)


@pytest.mark.parametrize(
    "body",
    [
        "بالخلاصات التالية: 2 ـ كتاب الإشارات",  # a list number (book 15)
        "القوس الروماني. 2 – المقابر",
        "جاء المسلمون سنة 2 ه بقيادة عمرو",  # a year
        "منذ سنة 2 للهجرة هي برقة",
    ],
)
def test_list_numbers_and_years_are_never_markers(body):
    result = run([pg(1, [ln(body, id=80), ln("(2) حاشية", kind="footnote", id=81)])])
    (note,) = notes_of(result)
    assert note["attrs"]["orphan"] is True and " 2 " in text_of(blocks_of(result)[0])


def test_a_standalone_number_before_a_mark_wins_over_one_before_a_word():
    page = pg(1, [ln("سار 2 ميلًا ثم قال كذا 2 . وانتهى", id=90), ln("(2) الحاشية", kind="footnote")])
    assert text_of(blocks_of(run([page]))[0]) == "سار 2 ميلًا ثم قال كذا[1]. وانتهى"


def test_an_indented_line_after_a_full_line_ending_on_a_bare_word_continues_the_paragraph():
    # Book 13 page 3: the box of «– السعيد – ابن …» misses its leading dash and looks indented.
    lines = [
        ln("ومن أقدم النصوص", INDENT, y=0.10, id=1),
        ln("إذ يقول: «كان لباتوس", FULL, y=0.12, id=2),
        ln("– السعيد – ابن اسمه", INDENT, y=0.14, id=3),
        ln("ما إن ارتقى العرش", FULL, y=0.16, id=4),
        ln("وانتهى النص.", FULL, y=0.18, id=5),
        ln("فقرة جديدة بمسافة", INDENT, y=0.20, id=6),
        ln("تنتهي بكلمة لاتينية Touchera", FULL, y=0.22, id=7),
        ln("وإذن فالمدينة", INDENT, y=0.24, id=8),
        ln("تقع في الداخل", FULL, y=0.26, id=9),
    ]
    assert [b.line_ids for b in split_paragraphs(pg(1, lines))] == [[1, 2, 3, 4, 5], [6, 7], [8, 9]]
    assert pipeline.ends_mid_sentence("كان لباتوس") and pipeline.ends_mid_sentence("الإسلامُ")
    assert not pipeline.ends_mid_sentence("انتهى.") and not pipeline.ends_mid_sentence("سنة 21")


def test_direction_marks_are_removed_from_the_derived_text():
    assert normalize_text("مليتية WV\u200f ميلاً \u200e،") == "مليتية WV ميلاً،"


def test_footnotes_are_numbered_per_page_by_default():
    assert Settings().footnote_numbering == "page" == pipeline.DEFAULT_NUMBERING
    pages = [
        pg(
            1,
            [
                ln("متن (١) و(٢).", id=1),
                ln("(١) أ", kind="footnote", id=2),
                ln("(٢) ب", kind="footnote", id=3),
            ],
        ),
        pg(2, [ln("متن (١) انتهى.", INDENT, id=4), ln("(١) ج", kind="footnote", id=5)]),
    ]
    result = assemble(pages, {}, {"id": 7})
    assert [n["attrs"]["number"] for n in notes_of(result)] == [1, 2, 1]
    assert result.document["attrs"]["footnoteNumbering"] == "page"


def _chapters(numbering):
    pages = [
        pg(1, [ln("الفصل الأول", CENTRED, role="heading", id=1), ln("متن (١) و(٢).", INDENT, id=2)]),
        pg(2, [ln("متن (١) انتهى.", INDENT, id=3), ln("(١) ح", kind="footnote", id=4)]),
        pg(3, [ln("الفصل الثاني", CENTRED, role="heading", id=5), ln("متن (١).", INDENT, id=6)]),
    ]
    pages[0].lines += [ln("(١) أ", kind="footnote", id=7), ln("(٢) ب", kind="footnote", id=8)]
    pages[2].lines += [ln("(١) ج", kind="footnote", id=9)]
    result = run(pages, footnote_numbering=numbering)
    assert result.document["attrs"]["footnoteNumbering"] == numbering
    return [n["attrs"]["number"] for n in notes_of(result)]


@pytest.mark.parametrize(
    ("numbering", "numbers"), [("chapter", [1, 2, 3, 1]), ("book", [1, 2, 3, 4]), ("page", [1, 2, 1, 1])]
)
def test_footnote_numbering_modes(numbering, numbers):
    assert _chapters(numbering) == numbers


def test_page_notes_carry():
    first = pg(1, [ln("(1) حاشية", kind="footnote", id=1)])
    notes, carry = page_notes(first, None)
    assert [n.id for n in notes] == ["n1"] and carry is notes[0]
    second = pg(2, [ln("تتمة", kind="footnote", id=2), ln("وتتمة ثانية", kind="footnote", id=3)])
    notes2, carry2 = page_notes(second, carry)
    assert notes2 == [] and carry2 is carry and carry.line_ids == [1, 2, 3]
    assert page_notes(pg(3, [ln("متن")]), carry2) == ([], None)


# ---------------------------------------------------------------- 2.6 headings and suggestions


def _suggestion_pages(text="تمهيد الكتاب", edges=CENTRED, follow=True):
    lines = [ln(text, edges, y=0.1, id=100)]
    if follow:
        lines.append(ln("نص الفقرة التالية", INDENT, y=0.12, id=101))
    return [pg(1, lines)]


def test_a_short_centred_paragraph_followed_by_a_paragraph_is_a_heading_suggestion():
    result = run(_suggestion_pages())
    node = blocks_of(result)[0]
    assert node["type"] == "paragraph" and node["attrs"]["suggestedRole"] == "heading"
    assert blocks_of(result)[1]["attrs"]["suggestedRole"] is None
    assert "no_headings" in codes(result)  # suggestions are never applied


@pytest.mark.parametrize(
    "pages",
    [
        _suggestion_pages("تمهيد الكتاب."),
        _suggestion_pages("واحد اثنان ثلاثة أربعة خمسة ستة سبعة ثمانية تسعة"),
        _suggestion_pages(edges=SHORT),
        _suggestion_pages(follow=False),
    ],
)
def test_no_suggestion_for_punctuation_long_text_uncentred_or_last_blocks(pages):
    assert blocks_of(run(pages))[0]["attrs"]["suggestedRole"] is None


def test_dismissed_suggestions_stay_dismissed():
    assert (
        blocks_of(run(_suggestion_pages(), dismissed_suggestions=["p100"]))[0]["attrs"]["suggestedRole"]
        is None
    )


def test_a_title_ending_with_a_closing_quote_is_still_suggested():
    result = run(_suggestion_pages("مختارات من «مروج الذهب»"))
    assert blocks_of(result)[0]["attrs"]["suggestedRole"] == "heading"
    assert pipeline.ends_sentence("قال: «انتهى.»") and not pipeline.ends_sentence("كتاب «الذهب»")


# ---------------------------------------------------------------- running heads (D49)

HEAD = (0.45, 0.55)  # a running head: narrow, centred, at the very top of the page


def _head_book(heads=("المسعودي", "وصف بغداد"), pages=6, opening="وصف بغداد", variant=(4, "السعودي")):
    """Pages 1–`pages`: each starts with a running head (even pages the book's, odd ones the chapter's),
    then two full lines; the last line of a page runs on to the next page. Page 1 opens the chapter with
    its title lower on the page (`opening`); page `variant[0]` carries an OCR slip of the book's head."""
    out = []
    for n in range(1, pages + 1):
        head = heads[0] if n % 2 == 0 else heads[1]
        if n == variant[0]:
            head = variant[1]
        lines = []
        if n == 1 and opening:
            lines.append(ln(opening, CENTRED, y=0.1, id=n * 100))
        else:
            lines.append(ln(head, HEAD, y=0.01, id=n * 100))
        lines.append(ln(f"نص الصفحة {n} يبدأ هنا ويمضي", FULL, y=0.2, id=n * 100 + 1))
        lines.append(ln(f"ويتصل بما في الصفحة {n + 1} من", FULL, y=0.22, id=n * 100 + 2))
        out.append(pg(n, lines))
    return out


def _texts(result) -> list[str]:
    return [text_of(node) for node in blocks_of(result)]


def test_running_heads_are_dropped_and_the_pages_join_again():
    result = run(_head_book())
    texts = _texts(result)
    assert not [t for t in texts if t.strip() in ("المسعودي", "السعودي")]
    assert [t for t in texts if t.strip() == "وصف بغداد"] == ["وصف بغداد"]  # page 1: the chapter's own title
    heads = [w for w in result.warnings if w["code"] == "running_head"]
    assert [w["message"] for w in heads] == [
        "حُذفت الترويسة «المسعودي» من أعلى 3 صفحات.",  # «السعودي» on page 4 is the same head
        "حُذفت الترويسة «وصف بغداد» من أعلى صفحتين.",
    ] or [w["message"] for w in heads] == [
        "حُذفت الترويسة «وصف بغداد» من أعلى صفحتين.",
        "حُذفت الترويسة «المسعودي» من أعلى 3 صفحات.",
    ]
    assert result.stats["running_heads"] == 5
    assert all(w["severity"] == "info" for w in heads)
    # with the heads gone, a paragraph cut by a page break reads as one (a pageBreak inside)
    assert result.stats["joins"] >= 4 and any("|" in t for t in texts)


def test_running_heads_stay_when_the_option_is_off():
    result = run(_head_book(), strip_running_heads=False)
    assert "running_head" not in codes(result) and result.stats["running_heads"] == 0
    assert [t for t in _texts(result) if t.strip() in ("المسعودي", "السعودي")]


def test_a_reviewers_heading_a_boxless_line_and_a_two_page_repeat_are_kept():
    pages = _head_book(heads=("المسعودي", "رحلة"), pages=4, opening=None, variant=(0, ""))
    pages[1].lines[0].role = "heading"  # page 2: the reviewer made the top line a heading
    pages[3].lines[0].box = None  # page 4: no box, nothing to measure
    result = run(pages)
    texts = _texts(result)
    assert "المسعودي" in texts  # only page 2 (a heading) and page 4 (no box) carried it: kept
    assert texts.count("رحلة") == 2  # pages 1 and 3: two pages are not a running head
    assert "running_head" not in codes(result)


def test_a_reviewed_title_counts_for_its_running_head():
    title = ln("المسعودي في سطور", CENTRED, y=0.1, role="heading", id=90)
    first = pg(1, [title, ln("نص الفصل الأول يبدأ هنا", FULL, y=0.2, id=91)])
    pages = [first] + _head_book(heads=("المسعودي في سطور", "المسعودي في سطور"), pages=3, opening=None)[1:]
    texts = _texts(run(pages))
    assert texts.count("المسعودي في سطور") == 1  # the title on page 1; its head on pages 2–3 went


def test_no_headings_warning_is_info():
    (warning,) = [w for w in run([pg(1, [ln("نص")])]).warnings if w["code"] == "no_headings"]
    assert warning["severity"] == "info" and warning["page"] is None and warning["blockId"] is None


# ---------------------------------------------------------------- 2.7 uncertain words


def test_uncertain_words_are_marked_and_counted_per_page():
    pages = [
        pg(
            1,
            [
                ln("كلمة مشكوكة وأخرى", uncertain=[1], id=60),
                ln("(1) حاشية مشكوكة", kind="footnote", uncertain=[2]),
            ],
        ),
        pg(2, [ln("كل شيء واضح.", id=61)]),
    ]
    result = run(pages)
    content = blocks_of(result)[0]["content"]
    assert {"type": "text", "text": "مشكوكة", "marks": [{"type": "uncertain"}]} in content
    (warning,) = [w for w in result.warnings if w["code"] == "uncertain_words"]
    assert warning == {
        "code": "uncertain_words",
        "severity": "info",
        "page": 1,
        "blockId": "p60",
        "lineIds": [60, pages[0].lines[1].id],
        "message": "بقيت كلمات غير محسومة في الصفحة 1 (2).",
    }


# ---------------------------------------------------------------- ids, sources, stats, document


def _book():
    return [
        pg(1, [ln("الفصل الأول", CENTRED, role="heading", id=1), ln("نص الفقرة (١) يستمر", INDENT, id=2)]),
        pg(2, [ln("وينتهي هنا.", FULL, id=3), ln("فقرة ثانية", INDENT, id=4)], status="ocr_done"),
    ]


def test_ids_are_stable_across_runs_and_every_block_has_sources():
    pages = _book()
    pages[0].lines.append(ln("(١) حاشية", kind="footnote", id=5))
    first = run(pages)
    second = run(pages)
    ids = [n["attrs"]["id"] for n in blocks_of(first)]
    assert ids == [n["attrs"]["id"] for n in blocks_of(second)] == ["h1", "p2", "p4"]
    assert [n["attrs"]["id"] for n in notes_of(first)] == ["n5"]
    for node in blocks_of(first):
        assert node["attrs"]["sourcePages"] and node["attrs"]["sourceLineIds"]
    reviewed = {n["attrs"]["id"]: n["attrs"]["reviewed"] for n in blocks_of(first)}
    assert reviewed == {"h1": True, "p2": False, "p4": False}  # p2 runs onto the unreviewed page 2


def test_stats_count_the_run():
    pages = _book()
    pages[0].lines.append(ln("(١) حاشية", kind="footnote", id=5))
    assert run(pages).stats == {
        "pages_included": 2,
        "pages_skipped": 0,
        "pages_unreviewed": 1,
        "headings": 1,
        "chapters": 1,
        "paragraphs": 2,
        "footnotes": 1,
        "joins": 1,
        "words": 10,
        "running_heads": 0,
    }


def test_document_shape():
    result = run(_book())
    doc = result.document
    assert doc["type"] == "doc"
    assert doc["attrs"] == {
        "bookId": 7,
        "runId": 31,
        "assembledAt": None,
        "footnoteNumbering": "page",
        "digitStyle": "western",
        "seams": result.seams,
    }
    assert doc["content"][0] == {"type": "title", "attrs": {"text": "كتاب", "author": "مؤلف"}}
    heading, paragraph, _ = doc["content"][1:]
    assert heading["attrs"] == {
        "level": 1,
        "id": "h1",
        "sourcePages": [1],
        "sourceLineIds": [1],
        "reviewed": True,
    }
    assert set(paragraph["attrs"]) == {"id", "sourcePages", "sourceLineIds", "reviewed", "suggestedRole"}
    json.dumps(doc)  # plain JSON


def test_warnings_shape_messages_are_arabic_with_western_digits():
    pages = [
        pg(1, [ln("متن (٣) ثم (٥)", uncertain=[0]), ln("(١) حاشية", kind="footnote")], status="ocr_done"),
        pg(2, [ln("قيد المعالجة")], status="layout_done"),
        pg(3, [ln("خطأ")], status="error"),
        pg(4, []),
    ]
    result = run(pages)
    assert set(codes(result)) == {
        "no_headings",
        "page_unreviewed",
        "page_pending",
        "page_error",
        "empty_page",
        "marker_unmatched",
        "note_orphan",
        "uncertain_words",
    }
    for warning in result.warnings:
        assert set(warning) == {"code", "severity", "page", "blockId", "lineIds", "message"}
        assert warning["severity"] in ("warning", "info")
        assert re.search("[ء-ي]", warning["message"]) and not ARABIC_INDIC.search(warning["message"])
    assert [w["page"] or 0 for w in result.warnings] == sorted(w["page"] or 0 for w in result.warnings)


def test_on_stage_reports_each_step_in_order():
    seen: list[str] = []
    assemble([pg(1, [ln("نص")])], {}, {"id": 1}, on_stage=seen.append)
    assert seen == ["paragraphs", "seams", "footnotes", "headings", "typography"]


def test_a_book_without_pages_gives_a_title_only_document():
    result = run([])
    assert result.document["content"] == [{"type": "title", "attrs": {"text": "كتاب", "author": "مؤلف"}}]
    assert result.stats["pages_included"] == 0 and codes(result) == ["no_headings"]


def test_rich_keeps_one_meta_per_character():
    with pytest.raises(ValueError):
        Rich("ab", [Meta()])
    joined = Rich.join([Rich.of("أ", Meta(line=1)), Rich.of("ب", Meta(line=2))])
    assert joined.text == "أ ب" and [m.line for m in joined.meta] == [1, 1, 2]
    assert joined.lines() == [1, 2]
    block = Block("p1", "paragraph", 0, [], [1], [], Rich("x"))
    assert block.line_ids == []


# ---------------------------------------------------------------- D74: verse lines, the guard, stray notes

ENDING = (0.5, 0.85)  # indented and short: a one-line paragraph


def test_a_verse_line_is_never_joined_with_another_line():
    """Book 23 page 5: staggered hemistichs glued into the wrong paragraphs; each verse line now stands
    alone (a `paragraph` with `style: "verse"`), whatever its geometry. Pairing into bayts is 7d."""
    lines = [
        ln("قال الشاعر في ذلك", FULL, id=1),
        ln("لله دري اذا اعدو على فرسي", (0.45, 0.95), role="verse", id=2),
        ln("الى الهياج ونار الحرب تستعر", (0.07, 0.58), role="verse", id=3),
        ln("وفي يدي صارم افري الرؤوس به", (0.45, 0.95), role="verse", id=4),
        ln("في حده الموت لا يبقي ولا يذر", (0.07, 0.58), role="verse", id=5),
        ln("ثم انصرف", FULL, id=6),
        ln("الى بلده", SHORT, id=7),
    ]
    result = run([pg(1, lines)])
    nodes = blocks_of(result)
    assert [n["attrs"]["sourceLineIds"] for n in nodes] == [[1], [2], [3], [4], [5], [6, 7]]
    assert [n["attrs"].get("style") for n in nodes] == [None, "verse", "verse", "verse", "verse", None]
    assert [n["type"] for n in nodes] == ["paragraph"] * 6
    assert text_of(nodes[2]) == "الى الهياج ونار الحرب تستعر"
    assert "style" not in nodes[0]["attrs"]  # other paragraphs keep today's attrs


def test_verse_lines_break_without_boxes_and_are_never_heading_suggestions():
    a, b = ln("بيت اول", role="verse", boxed=False), ln("بيت ثان", role="verse", boxed=False)
    assert breaks_between(a, b, None, None) is True
    assert breaks_between(ln("نص", boxed=False), a, None, None) is True
    lines = [
        ln("لله دري اذا اعدو", CENTRED, y=0.1, role="verse", id=21),
        ln("نص يتبعه", INDENT, y=0.12, id=22),
    ]
    (verse, _following) = blocks_of(run([pg(1, lines)]))
    assert verse["attrs"]["suggestedRole"] is None and verse["attrs"]["style"] == "verse"


def test_a_verse_line_never_joins_across_a_page_even_with_an_override():
    pages = [
        pg(
            1,
            [
                ln("متن الصفحة", INDENT, id=31),
                ln("وتمامه", FULL, id=32),
                ln("بيت في آخر الصفحة", FULL, role="verse", id=33),
            ],
        ),
        pg(2, [ln("بيت في أول الصفحة", FULL, role="verse", id=41), ln("ثم نثر", FULL, id=42)]),
    ]
    for overrides in ({}, {"2": "join"}):
        result = run(pages, seams=overrides)
        (seam,) = result.seams
        assert seam["mode"] == "split" and seam["reason"] == "verse" and seam["decision"] == "auto"
        assert [n["attrs"]["sourceLineIds"] for n in blocks_of(result)] == [[31, 32], [33], [41], [42]]


def _guard_pages(first_note: str, second_body: str) -> list[PageIn]:
    """Page 1 has a note (1) that may run on; page 2 starts its notes with a line without a marker."""
    return [
        pg(
            1,
            [
                ln("متن الصفحة الأولى (1) انتهى", INDENT, id=51),
                ln("هنا.", SHORT, id=52),
                ln(first_note, kind="footnote", id=53),
            ],
        ),
        pg(
            2,
            [
                ln(second_body, INDENT, id=61),
                ln("وتمت.", SHORT, id=62),
                ln("سطر حاشية بلا علامة", kind="footnote", id=63),
            ],
        ),
    ]


def test_the_guard_lets_a_note_run_on_when_it_does_not_end_and_the_page_has_no_open_call():
    result = run(_guard_pages("(1) حاشية تبدأ ولا تنتهي", "متن الصفحة الثانية بلا علامة"))
    (note,) = notes_of(result)
    assert note["attrs"]["sourceLineIds"] == [53, 63] and note["content"][0]["text"].endswith("بلا علامة")
    assert not {"note_orphan", "note_marker_missing"} & set(codes(result))


def test_the_guard_stops_after_a_note_that_ends_a_sentence():
    result = run(_guard_pages("(1) حاشية تامة.", "متن الصفحة الثانية بلا علامة"))
    first, second = notes_of(result)
    assert first["attrs"]["sourceLineIds"] == [53] and second["attrs"]["sourceLineIds"] == [63]
    assert second["attrs"]["orphan"] is True and "note_marker_missing" not in codes(result)


def test_the_guard_gives_a_marker_less_note_to_the_open_call_of_its_page():
    """Book 25 page 6: its note (no marker) had joined page 5's note, which ended in «… 259 */»."""
    result = run(_guard_pages("(1) حاشية تبدأ ولا تنتهي", "وكان تحت إمرة ديستري (1) فتوقف"))
    first, second = notes_of(result)
    assert first["attrs"]["sourceLineIds"] == [53]
    assert second["attrs"] == {
        "id": "n63",
        "number": 1,
        "marker": "1",
        "sourcePage": 2,
        "sourceLineIds": [63],
        "orphan": False,
    }
    assert text_of(blocks_of(result)[1]) == "وكان تحت إمرة ديستري[1] فتوقف وتمت."
    (warning,) = [w for w in result.warnings if w["code"] == "note_marker_missing"]
    assert warning == {
        "code": "note_marker_missing",
        "severity": "warning",
        "page": 2,
        "blockId": "p61",
        "lineIds": [63],
        "message": "حاشية بلا علامة رُبطت بالعلامة (1)؛ تحقّق منها.",
        "marker": "1",
    }
    assert "note_orphan" not in codes(result) and "marker_unmatched" not in codes(result)


def test_the_first_open_call_takes_the_note_without_marker():
    """Only a page's first note lines can lack a marker (a later line continues the note before it):
    that note takes the first call its page's marker notes leave open, in reading order."""
    page = pg(
        1,
        [
            ln("الأول (1) والثاني (2) والثالث (3) والرابع (4) والخامس", id=71),
            ln("حاشية بلا علامة", kind="footnote", id=72),
            ln("(2) حاشية معلَّمة", kind="footnote", id=73),
        ],
    )
    result = run([page])
    body = text_of(blocks_of(result)[0])
    assert body == "الأول[1] والثاني[2] والثالث (3) والرابع (4) والخامس"
    notes = {n["attrs"]["id"]: n["attrs"]["marker"] for n in notes_of(result)}
    assert notes == {"n72": "1", "n73": "2"}
    assert [w["message"] for w in result.warnings if w["code"] == "marker_unmatched"] == [
        "علامة الحاشية «3» في الصفحة 1 بلا حاشية مقابلة.",
        "علامة الحاشية «4» في الصفحة 1 بلا حاشية مقابلة.",
    ]


@pytest.mark.parametrize(
    "body",
    [
        "سار التجار الى السوق1 ثم عادوا",  # glued digits: OCR noise as often as a call («ص٩»)
        "قال «هذا كلامه» 1 ثم سكت",  # quoted
        "التثمين (450)، والقيمة بعد التشطيب",  # a price in brackets (book 16)
        "كما في الصفحة (22) من الكتاب",  # above POSITIONAL_MAX
    ],
)
def test_a_note_without_a_marker_takes_only_a_small_bracketed_or_superscript_call(body):
    """On the dev books the positional link took «(814)», «(450)» and glued digits: only a bracketed or
    superscript call of 1–15 (or `*`) is taken by place. The guard then lets the line run on."""
    pages = _guard_pages("(1) حاشية تبدأ ولا تنتهي", body)
    result = run(pages)
    (note,) = notes_of(result)
    assert note["attrs"]["sourceLineIds"] == [53, 63]
    assert "note_marker_missing" not in codes(result)
    stopped = run(_guard_pages("(1) حاشية تامة.", body))
    assert [n["attrs"]["orphan"] for n in notes_of(stopped)] == [False, True]
    assert "note_marker_missing" not in codes(stopped)


def test_positional_calls():
    page = pg(
        1,
        [
            ln("الأول¹ والثاني (*) والثالث (3) والرابع (16) والخامس (0)", id=91),
        ],
    )
    blocks = split_paragraphs(page)
    by_key = {cand.key: cand for cand in pipeline.page_candidates(blocks, {91: 1})[1]}
    assert set(by_key) == {"1", "*", "3", "16", "0"}
    assert pipeline.positional_call(by_key["1"], blocks, ())  # superscript
    assert pipeline.positional_call(by_key["*"], blocks, ["2"])
    assert pipeline.positional_call(by_key["3"], blocks, ["4"])
    assert not pipeline.positional_call(by_key["3"], blocks, ["2", "3"])  # heads the page's notes
    assert not pipeline.positional_call(by_key["16"], blocks, ())
    assert not pipeline.positional_call(by_key["0"], blocks, ())


def test_a_note_without_a_marker_takes_a_call_below_the_pages_marker_notes():
    page = pg(
        1,
        [
            ln("الأول (1) والثاني (2) والرابع (4) والخامس", id=71),
            ln("حاشية بلا علامة", kind="footnote", id=72),
            ln("(2) حاشية معلَّمة", kind="footnote", id=73),
            ln("(3) حاشية ثالثة", kind="footnote", id=74),
        ],
    )
    result = run([page])
    assert {n["attrs"]["id"]: n["attrs"]["marker"] for n in notes_of(result)} == {
        "n72": "1",
        "n73": "2",
        "n74": "3",
    }
    lone = pg(
        1,
        [
            ln("الأول (4) والخامس", id=71),
            ln("حاشية بلا علامة", kind="footnote", id=72),
            ln("(2) حاشية معلَّمة", kind="footnote", id=73),
        ],
    )
    result = run([lone])
    assert [n["attrs"]["orphan"] for n in notes_of(result)] == [True, True]
    assert "note_marker_missing" not in codes(result)


def test_weak_candidates_and_block_initial_markers_are_never_open_calls():
    """A standalone number is text more often than a call, and a paragraph's own leading «(1)» is the
    marker of a note left in the body: neither takes a marker-less note."""
    page = pg(
        1,
        [
            ln("سار 3 أميال ثم وقف", INDENT, id=81),
            ln("وانتهى.", SHORT, id=82),
            ln("(1) فقرة تبدأ بعلامة", INDENT, id=83),
            ln("حاشية بلا علامة", kind="footnote", id=84),
        ],
    )
    result = run([page])
    (note,) = notes_of(result)
    assert note["attrs"]["orphan"] is True and "note_marker_missing" not in codes(result)


def _stray_page(*lines: LineIn, number: int = 3) -> PageIn:
    """A page whose first paragraph (three lines: the page's measure) holds `lines[0]`'s text, then
    `lines[1:]`, each a one-line paragraph (indented and short)."""
    first, *rest = lines
    opening = [
        ln(first.text, INDENT, id=first.id, role=first.role),
        ln("ويمتد السطر الثاني من الفقرة الى آخره", FULL, id=first.id + 500),
        ln("وتم.", SHORT, id=first.id + 501),
    ]
    for line in rest:
        line.box = box(line.box[1], ENDING) if line.box is not None else None
    return pg(number, [*opening, *rest])


def test_a_note_left_at_the_end_of_its_page_warns_with_its_actions():
    page = _stray_page(
        ln("وفي تلك السنة (1) وصل الأسطول الى", id=91),
        ln("(1) الأسطول الفرنسي بقيادة دوكين.", id=93),
    )
    result = run([page])
    body, stray = blocks_of(result)
    assert body["attrs"]["sourceLineIds"] == [91, 591, 592]
    assert stray["attrs"]["noteFor"] == "1" and "noteFor" not in body["attrs"]
    (warning,) = [w for w in result.warnings if w["code"] == "stray_note"]
    assert warning == {
        "code": "stray_note",
        "severity": "warning",
        "page": 3,
        "blockId": "p93",
        "lineIds": [93],
        "message": "فقرة في الصفحة 3 تبدأ بعلامة حاشية «(1)» ولم تُربط.",
        "marker": "1",
        "actions": [
            {"key": "footnote", "label": "جعلها حاشية", "role": "footnote", "lineIds": [93]},
            {"key": "go", "label": "انتقال", "blockId": "p93"},
        ],
    }
    assert "(1) الأسطول" in text_of(stray)  # the text stays as printed


def test_a_marker_initial_paragraph_inside_its_page_gets_the_label_only():
    page = _stray_page(
        ln("وفي تلك السنة (٢) وصل الأسطول.", INDENT, id=101),
        ln("(٢) بلاد فارس", INDENT, id=102),
        ln("ثم عاد الى بلده.", INDENT, id=103),
    )
    result = run([page])
    assert [n["attrs"].get("noteFor") for n in blocks_of(result)] == [None, "2", None]
    assert "stray_note" not in codes(result)


@pytest.mark.parametrize(
    "lines",
    [
        # no open call with that number on the page (book 19's numbered sub-heading «(١) بلاد فارس»)
        [ln("متن بلا علامة.", INDENT, id=111), ln("(1) بلاد فارس", INDENT, id=112)],
        [ln("متن (2) بعلامة أخرى.", INDENT, id=111), ln("(1) بلاد فارس", INDENT, id=112)],
        # the call is linked already: its note was found
        [
            ln("متن (1) بعلامة.", INDENT, id=111),
            ln("(1) بلاد فارس", INDENT, id=112),
            ln("(1) الحاشية", kind="footnote", id=113),
        ],
        # a marker with nothing after it, a verse line, a list number without brackets (book 26)
        [ln("متن (1) بعلامة.", INDENT, id=111), ln("(1)", INDENT, id=112)],
        [ln("متن (1) بعلامة.", INDENT, id=111), ln("(1) شطر بيت", INDENT, role="verse", id=112)],
        [ln("متن (1) بعلامة.", INDENT, id=111), ln("1 – كتاب الأوسط", INDENT, id=112)],
    ],
)
def test_no_stray_note_without_an_open_call_or_a_marker_with_text(lines):
    result = run([_stray_page(*lines)])
    assert "stray_note" not in codes(result)
    assert not any(n["attrs"].get("noteFor") for n in blocks_of(result))


def test_a_page_made_only_of_marker_initial_paragraphs_gets_the_label_but_no_warning():
    page = pg(
        4,
        [
            ln("(1) أولها وفيه (2) علامة.", boxed=False, id=131),
            ln("(2) ثانيها ولا شيء بعده.", boxed=False, id=132),
        ],
    )
    result = run([page])
    assert [n["attrs"].get("noteFor") for n in blocks_of(result)] == [None, "2"]
    assert "stray_note" not in codes(result)


def test_the_trailing_run_of_marker_initial_paragraphs_ends_the_page():
    page = _stray_page(
        ln("الأول (1) والثاني (2) وتم.", INDENT, id=121),
        ln("(1) حاشية أولى تُركت في المتن", INDENT, id=122),
        ln("(2) حاشية ثانية تُركت في المتن", INDENT, id=123),
    )
    result = run([page])
    strays = [w for w in result.warnings if w["code"] == "stray_note"]
    assert [(w["blockId"], w["marker"]) for w in strays] == [("p122", "1"), ("p123", "2")]


def test_the_new_warning_codes_sort_after_the_orphans():
    assert pipeline.CODE_ORDER.index("note_marker_missing") == pipeline.CODE_ORDER.index("note_orphan") + 1
    assert (
        pipeline.CODE_ORDER.index("note_call_repaired")
        == pipeline.CODE_ORDER.index("note_marker_missing") + 1
    )
    assert pipeline.CODE_ORDER.index("stray_note") < pipeline.CODE_ORDER.index("uncertain_words")


# ====================================================================== part 2: services, API, views

W, H = 1000, 1600  # preprocess output size: line boxes are gray-image pixels of this size
FULL_PX = (100, 900)
INDENT_PX = (100, 850)
SHORT_PX = (500, 900)


def tok(t, conf="high", res=None) -> dict:
    return {"t": t, "alt": None, "tess": None, "conf": conf, "digit": False, "bbox": None, "res": res}


def role_user(name: str, role: str | None) -> User:
    user = User.objects.create_user(name, password="pass-1234")
    if role:
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


class Factory:
    """A small real book: pages with preprocess, regions and lines (boxes in gray pixels)."""

    def __init__(self, title="كتاب التجميع"):
        self.book = Book.objects.create(title=title, author="المؤلف", status=Book.Status.REVIEWING)
        self.regions: dict[tuple[int, str], Region] = {}

    def page(self, number, status=Page.Status.REVIEWED, preprocess=True, excluded=False) -> Page:
        page = Page.objects.create(
            book=self.book,
            number=number,
            source_index=number - 1,
            status=Page.Status.EXCLUDED if excluded else status,
            is_excluded=excluded,
            text_state=Page.TextState.FINAL,
            width=W * 2,
            height=H * 2,
            printed_number=str(number + 10),
        )
        if preprocess:
            Preprocess.objects.create(page=page, output_width=W, output_height=H)
        for order, kind in enumerate(("running_header", "body", "footnote", "page_number")):
            self.regions[(page.pk, kind)] = Region.objects.create(
                page=page, kind=kind, bbox=[0, 0, W, H], order=order
            )
        return page

    def line(self, page, text, edges=FULL_PX, kind="body", role="body", low=(), y=None) -> Line:
        order = page.lines.count()
        y = y if y is not None else 100 + order * 40
        tokens = [tok(word, "low" if i in low else "high") for i, word in enumerate(text.split())]
        return Line.objects.create(
            page=page,
            order=order,
            region=self.regions[(page.pk, kind)],
            bbox=[edges[0], y, edges[1], y + 30] if edges else None,
            text=text,
            ocr_text=text,
            tokens=tokens,
            n_low=len(low),
            role=role,
        )


def two_page_book() -> tuple[Factory, list[Page]]:
    """Page 1 (reviewed): header, heading, a paragraph cut by the page break, a note; page 2 (ocr_done)."""
    f = Factory()
    one = f.page(1)
    f.line(one, "ترويسة الكتاب", kind="running_header")
    f.line(one, "الفصل الأول", (300, 700), role="heading")
    f.line(one, "نص الفقرة (١) يبدأ هنا", INDENT_PX)
    f.line(one, "ويستمر حتى آخر", FULL_PX)
    f.line(one, "(١) حاشية الصفحة الأولى", kind="footnote")
    f.line(one, "١٢", kind="page_number")
    two = f.page(2, Page.Status.OCR_DONE)
    f.line(two, "الصفحة ويتم الكلام.", FULL_PX, low=[1])
    f.line(two, "فقرة ثانية", INDENT_PX)
    return f, [one, two]


@pytest.fixture
def editor(db):
    return role_user("editor", "editor")


@pytest.fixture
def proofreader(db):
    return role_user("reader", "proofreader")


def post(client, name, book_id, data=None):
    return client.post(
        reverse(f"api:{name}", args=[book_id]), json.dumps(data or {}), content_type="application/json"
    )


def logged(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


# ---------------------------------------------------------------- loader


def test_load_book_reads_ratios_kinds_roles_and_uncertain_words(db):
    f, (one, two) = two_page_book()
    extra = f.line(two, "كلمة", None)  # a line without a box
    Line.objects.filter(pk=extra.pk).update(
        tokens=[tok("عبارة من كلمتين", "low"), tok("أخيرة", "low", "typed")]
    )
    bare = f.page(3, preprocess=False)
    f.line(bare, "بلا معالجة", (200, 1800))
    f.page(4, excluded=True)
    loaded = services.load_book(f.book)
    assert [p.number for p in loaded.pages] == [1, 2, 3]
    first = loaded.pages[0]
    assert first.printed == "11" and first.reviewed is True and first.status == "reviewed"
    assert [(line.kind, line.role, line.text) for line in first.lines] == [
        ("body", "heading", "الفصل الأول"),
        ("body", "body", "نص الفقرة (١) يبدأ هنا"),
        ("body", "body", "ويستمر حتى آخر"),
        ("footnote", "body", "(١) حاشية الصفحة الأولى"),
    ]  # running header and page number are left out
    assert first.lines[1].box == (0.1, pytest.approx(180 / H), 0.85, pytest.approx(210 / H))
    second = loaded.pages[1]
    assert second.reviewed is False and second.lines[0].uncertain == [1]
    assert second.lines[2].box is None
    assert second.lines[2].text == "عبارة من كلمتين أخيرة" and second.lines[2].uncertain == [0, 1, 2]
    bare_box = (0.1, 100 / (H * 2), 0.9, 130 / (H * 2))  # page size when there is no preprocess output
    assert loaded.pages[2].lines[0].box == pytest.approx(bare_box, abs=1e-5)
    assert set(loaded.signatures) == {one.pk, two.pk, bare.pk}
    assert loaded.signatures[one.pk].startswith("6:")  # every line counts, skipped kinds included


def test_load_book_uses_a_fixed_number_of_queries(db, django_assert_num_queries):
    small, _ = two_page_book()
    big = Factory("كتاب أكبر")
    for number in range(1, 7):
        page = big.page(number)
        for i in range(5):
            big.line(page, f"سطر {i} من الصفحة {number}")
        big.line(page, "(1) حاشية", kind="footnote")
    with django_assert_num_queries(2):
        services.load_book(small.book)
    with django_assert_num_queries(2):
        loaded = services.load_book(big.book)
    assert len(loaded.pages) == 6 and all(len(p.lines) == 6 for p in loaded.pages)


def test_load_book_gives_each_line_its_effective_kind_and_the_pipelines_role(db):
    """D74: `ocr.services.line_kind` — a `footnote` role makes a body line a note, `main` pulls a
    footnote-region line into the body, a heading or verse role on a footnote-region line is body too."""
    f = Factory()
    page = f.page(1)
    f.line(page, "متن")
    f.line(page, "سطر جعله المراجع حاشية", role="footnote")
    f.line(page, "بيت من الشعر", role="verse")
    f.line(page, "حاشية الصفحة", kind="footnote")
    f.line(page, "سطر أعيد الى المتن", kind="footnote", role="main")
    f.line(page, "عنوان في منطقة الحواشي", kind="footnote", role="heading")
    f.line(page, "بيت في منطقة الحواشي", kind="footnote", role="verse")
    (loaded,) = services.load_book(f.book).pages
    assert [(line.kind, line.role) for line in loaded.lines] == [
        ("body", "body"),
        ("footnote", "body"),
        ("body", "verse"),
        ("footnote", "body"),
        ("body", "body"),
        ("body", "heading"),
        ("body", "verse"),
    ]
    assert {services.pipeline_role(role) for role in Line.Role.values} == pipeline.LINE_ROLES


def test_footnote_role_lines_become_notes_and_main_lines_body_text(editor):
    f = Factory()
    page = f.page(1)
    f.line(page, "قال المؤرخ (1) كلامًا", INDENT_PX)
    f.line(page, "وتم.", SHORT_PX)
    f.line(page, "(1) سطر جعله المراجع حاشية", INDENT_PX, role="footnote")
    f.line(page, "وهذا سطر أعاده المراجع الى المتن.", (500, 850), kind="footnote", role="main")
    services.start_assembly(f.book, editor)
    doc = Manuscript.objects.get(book=f.book).document
    body = doc["content"][1:]
    assert [text_of(node) for node in body] == [
        "قال المؤرخ[1] كلامًا وتم.",
        "وهذا سطر أعاده المراجع الى المتن.",
    ]
    (note,) = [item for node in body for item in node["content"] if item["type"] == "footnote"]
    assert note["content"] == [{"type": "text", "text": "سطر جعله المراجع حاشية"}]
    assert note["attrs"]["orphan"] is False


# ---------------------------------------------------------------- D74: the manuscript fixtures for the UI

TRUST_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "trust")
ENDING_PX = (500, 850)  # indented and short: a one-line paragraph


def trust_book() -> Factory:
    """The book of `assembly/fixtures/trust/manuscript.json` (see `index.json` there): a heading and four
    verse lines (page 1), a note that runs on without ending (2), a marker-less note on a page with an
    open call (3: the guard and the positional link), a note left at the end of the body (4), and a
    reviewer's «حاشية» on a body line and «محتوى» on a footnote-region line (5)."""
    f = Factory("كتاب الحواشي والشعر")
    one = f.page(1)
    f.line(one, "الفصل الأول", (300, 700), role="heading")
    f.line(one, "قال الشاعر في وصف الحرب ما يرويه الناس", INDENT_PX)
    f.line(one, "في مجالسهم الى اليوم وهو من", FULL_PX)
    f.line(one, "البسيط:", SHORT_PX)
    for text, edges in (
        ("لله دري اذا اعدو على فرسي", (450, 950)),
        ("الى الهياج ونار الحرب تستعر", (70, 580)),
        ("وفي يدي صارم افري الرؤوس به", (450, 950)),
        ("في حده الموت لا يبقي ولا يذر", (70, 580)),
    ):
        f.line(one, text, edges, role="verse")
    f.line(one, "ثم انصرف الى بلده.", ENDING_PX)
    two = f.page(2)
    f.line(two, "وذكر ذلك ابن غلبون (1) في تاريخه وأطال", INDENT_PX)
    f.line(two, "في وصف الحملة وما جرى فيها من", FULL_PX)
    f.line(two, "الوقائع.", SHORT_PX)
    f.line(two, "(1) انظر كتاب التذكار صفحة 186 وكتاب المنهل العذب صفحة 259", kind="footnote")
    three = f.page(3)
    f.line(three, "وكان الأسطول كله تحت إمرة المارشال", INDENT_PX)
    f.line(three, "ديستري (1) فتوقف أولًا على سواحل", FULL_PX)
    f.line(three, "طرابلس.", SHORT_PX)
    f.line(three, "انظر تاريخ البحرية الفرنسية لليون غيران.", kind="footnote")
    four = f.page(4)
    f.line(four, "وفي تلك السنة (1) وصل الأسطول الى", INDENT_PX)
    f.line(four, "طرابلس وضرب المدينة بالمدافع حتى", FULL_PX)
    f.line(four, "طلب أهلها الصلح.", SHORT_PX)
    f.line(four, "(1) الأسطول الفرنسي بقيادة دوكين.", ENDING_PX)
    five = f.page(5)
    f.line(five, "وقال في موضع آخر (1) كلامًا طويلًا", INDENT_PX)
    f.line(five, "في هذا المعنى لا نطيل", FULL_PX)
    f.line(five, "بذكره.", SHORT_PX)
    f.line(five, "(1) هذا سطر جعله المراجع حاشية.", INDENT_PX, role="footnote")
    f.line(five, "وهذا سطر أعاده المراجع الى المتن.", ENDING_PX, kind="footnote", role="main")
    return f


def fixture_ids(book: Book) -> dict[int, int]:
    """Line pk → its fixture id: 1000 × page number + order + 1 (page 3's first line is 3001)."""
    rows = Line.objects.filter(page__book=book).values_list("pk", "page__number", "order")
    return {pk: 1000 * number + order + 1 for pk, number, order in rows}


def with_fixture_ids(value, ids: dict[int, int]):
    """A payload with its line ids (and the block and note ids made of them) as fixture ids."""
    if isinstance(value, list):
        return [with_fixture_ids(item, ids) for item in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        if key in ("sourceLineIds", "lineIds") and isinstance(item, list):
            out[key] = [ids.get(i, i) for i in item]
        elif key in ("id", "blockId") and isinstance(item, str) and re.fullmatch(r"[phn][0-9]+", item):
            out[key] = f"{item[0]}{ids.get(int(item[1:]), item[1:])}"
        else:
            out[key] = with_fixture_ids(item, ids)
    return out


def trust_payload(editor) -> dict:
    """`services.manuscript_payload` of `trust_book` after a run, with fixture ids and the run's own
    values (book, run, time) blanked, as JSON."""
    f = trust_book()
    services.start_assembly(f.book, editor)
    payload = json.loads(json.dumps(services.manuscript_payload(f.book)))
    payload = with_fixture_ids(payload, fixture_ids(f.book))
    payload["document"]["attrs"].update(bookId=0, runId=0, assembledAt=None)
    return payload


def load_trust(name: str):
    with open(os.path.join(TRUST_FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def check_trust(name: str, value) -> None:
    """`value` equals the fixture `name`; `NASSAKH_WRITE_TRUST_FIXTURES=1` rewrites the file first."""
    if os.environ.get("NASSAKH_WRITE_TRUST_FIXTURES") == "1":
        with open(os.path.join(TRUST_FIXTURES, name), "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
    assert value == load_trust(name)


D74_CODES = ("note_marker_missing", "stray_note")
RENDERED_ABOUT = (
    "`assembly.render.fragment_context` of `manuscript.json` (the manuscript view's markup and meta, "
    'D74): `blocks` the rendered verse paragraph (`data-style="verse"`: the paragraph menu\'s «شعر» is '
    "checked) and "
    "the paragraph whose leading marker has an open call on its page (`data-note-for`: the menu reads "
    "«حاشية للعلامة (n)»); `warnings` the D74 warnings as `meta.warnings` gives them (`flat_warnings`: "
    "`marker`, and `actions` for `stray_note`); `groups` their side-panel groups (`group_warnings`)."
)


def rendered_block(html: str, block_id: str) -> str:
    """The rendered element of block `block_id` in the document's HTML."""
    match = re.search(rf'<(p|h2|h3) [^>]*id="b-{block_id}".*?</\1>', html, re.S)
    assert match, block_id
    return match.group(0)


def test_the_manuscript_payload_equals_its_fixture(editor):
    """The UI's contract (agent E): the real payload of `trust_book` is `manuscript.json`, verse lines,
    the positional link and the stray note included; its rendering is `rendered.json`.
    `NASSAKH_WRITE_TRUST_FIXTURES=1` rewrites the files."""
    payload = trust_payload(editor)
    check_trust("manuscript.json", payload)
    fragment = render.fragment_context(payload)
    html = fragment["document_html"]
    check_trust(
        "rendered.json",
        {
            "about": RENDERED_ABOUT,
            "blocks": {"verse": rendered_block(html, "p1005"), "noteFor": rendered_block(html, "p4004")},
            "warnings": [w for w in fragment["meta"]["warnings"] if w["code"] in D74_CODES],
            "groups": [g for g in fragment["warning_groups"] if g["code"] in D74_CODES],
        },
    )


def test_the_rendered_fixture_carries_the_menus_attributes():
    rendered = load_trust("rendered.json")
    assert 'data-style="verse"' in rendered["blocks"]["verse"]
    assert "data-note-for" not in rendered["blocks"]["verse"]
    assert 'data-note-for="1"' in rendered["blocks"]["noteFor"]
    assert 'data-block="p4004"' in rendered["blocks"]["noteFor"]
    stray = next(w for w in rendered["warnings"] if w["code"] == "stray_note")
    assert stray["marker"] == "1" and stray["blockId"] == "p4004" and stray["page"] == 4
    assert stray["actions"] == [
        {"key": "footnote", "label": "جعلها حاشية", "role": "footnote", "lineIds": [4004]},
        {"key": "go", "label": "انتقال", "blockId": "p4004"},
    ]
    missing = next(w for w in rendered["warnings"] if w["code"] == "note_marker_missing")
    assert missing["marker"] == "1" and "actions" not in missing
    assert [(g["code"], g["label"], g["count"]) for g in rendered["groups"]] == [
        ("note_marker_missing", "حواشٍ بلا علامة رُبطت بموضعها", 1),
        ("stray_note", "فقرات تبدأ بعلامة حاشية", 1),
    ]


def test_flat_warnings_keeps_only_well_formed_actions():
    flat = render.flat_warnings(
        [
            {"code": "note_orphan", "page": 2, "message": "م"},
            {
                "code": "stray_note",
                "page": 3,
                "marker": 2,
                "actions": [
                    {"key": "footnote", "label": "جعلها حاشية", "role": "footnote", "lineIds": ["7", "x"]},
                    "go",
                ],
            },
        ]
    )
    assert "marker" not in flat[0] and "actions" not in flat[0]
    assert flat[1]["marker"] == "2"
    assert flat[1]["actions"] == [
        {"key": "footnote", "label": "جعلها حاشية", "role": "footnote", "lineIds": [7]}
    ]


def test_the_fixture_shows_each_d74_case():
    payload = load_trust("manuscript.json")
    blocks = {node["attrs"]["id"]: node for node in payload["document"]["content"][1:]}
    assert [i for i, node in blocks.items() if node["attrs"].get("style") == "verse"] == [
        "p1005",
        "p1006",
        "p1007",
        "p1008",
    ]
    assert blocks["p4004"]["attrs"]["noteFor"] == "1"
    by_code = {w["code"]: w for w in payload["warnings"]}
    assert by_code["note_marker_missing"]["page"] == 3 and by_code["note_marker_missing"]["lineIds"] == [3004]
    stray = by_code["stray_note"]
    assert stray["blockId"] == "p4004" and [a["key"] for a in stray["actions"]] == ["footnote", "go"]
    assert stray["actions"][0] == {
        "key": "footnote",
        "label": "جعلها حاشية",
        "role": "footnote",
        "lineIds": [4004],
    }
    notes = [item for node in blocks.values() for item in node["content"] if item["type"] == "footnote"]
    assert [(n["attrs"]["id"], n["attrs"]["sourcePage"]) for n in notes] == [
        ("n2004", 2),
        ("n3004", 3),
        ("n5004", 5),
    ]
    assert "no_headings" not in by_code and "note_orphan" not in by_code and "marker_unmatched" not in by_code


def test_the_block_roles_fixture_is_the_menu_the_service_takes():
    roles = load_trust("block_roles.json")
    assert [(item["value"], item["label"]) for item in roles["roles"]] == list(services.BLOCK_ROLES)
    assert roles["footnoteFor"] == services.FOOTNOTE_FOR_LABEL
    assert roles["footnoteFor"].format(n="1") == "حاشية للعلامة (1)"
    stray = next(w for w in load_trust("manuscript.json")["warnings"] if w["code"] == "stray_note")
    assert roles["request"]["body"] == {"line_ids": stray["actions"][0]["lineIds"], "role": "footnote"}


def test_the_stray_note_action_makes_the_paragraph_the_note_of_its_call(editor):
    """«جعلها حاشية» posts the roles endpoint with the warning's lines: the next run links the note."""
    f = trust_book()
    services.start_assembly(f.book, editor)
    run = AssemblyRun.objects.filter(book=f.book).latest("id")
    stray = next(w for w in run.warnings if w["code"] == "stray_note")
    action = stray["actions"][0]
    response = post(
        logged(editor), "manuscript_roles", f.book.pk, {"line_ids": action["lineIds"], "role": action["role"]}
    )
    assert response.status_code == 202
    run = AssemblyRun.objects.filter(book=f.book).latest("id")
    assert "stray_note" not in {w["code"] for w in run.warnings}
    doc = Manuscript.objects.get(book=f.book).document
    page_four = [n for n in doc["content"][1:] if n["attrs"].get("sourcePages") == [4]]
    assert [text_of(n) for n in page_four] == [
        "وفي تلك السنة[1] وصل الأسطول الى طرابلس وضرب المدينة بالمدافع حتى طلب أهلها الصلح."
    ]
    assert Line.objects.get(pk=action["lineIds"][0]).role == "footnote"


def test_set_block_roles_takes_the_menus_choices(proofreader):
    f = trust_book()
    one = f.book.pages.get(number=1)
    ending = one.lines.get(text="ثم انصرف الى بلده.")
    services.set_block_roles(f.book, proofreader, [ending.pk], "verse")
    ending.refresh_from_db()
    assert ending.role == "verse" and LineRevision.objects.filter(action="role").count() == 1
    services.set_block_roles(f.book, proofreader, [ending.pk], "body")
    ending.refresh_from_db()
    assert ending.role == "body"
    # a footnote-region line: «محتوى» pulls it into the body (`main`), «حاشية» gives it back (`body`), a
    # heading is allowed (the Phase 3 refusal is gone); a block over two pages is one call per page
    two = f.book.pages.get(number=2)
    note = two.lines.get(region__kind="footnote")
    body = two.lines.filter(region__kind="body").order_by("order").first()
    for choice, stored in (("body", "main"), ("footnote", "body"), ("heading", "heading")):
        services.set_block_roles(f.book, proofreader, [note.pk], choice)
        note.refresh_from_db()
        assert note.role == stored
    services.set_block_roles(f.book, proofreader, [ending.pk, body.pk], "footnote")
    assert [Line.objects.get(pk=pk).role for pk in (ending.pk, body.pk)] == ["footnote", "footnote"]
    batches = LineRevision.objects.filter(line_id__in=[ending.pk, body.pk]).order_by("-pk")[:2]
    assert len({revision.page_id for revision in batches}) == 2
    for bad in ("main", "chapter", ""):
        with pytest.raises(services.AssemblyError) as exc:
            services.set_block_roles(f.book, proofreader, [ending.pk], bad)
        assert str(exc.value) == "نوع السطر غير معروف."


# ---------------------------------------------------------------- the run end to end


def test_start_assembly_runs_eagerly_end_to_end(editor):
    f, (one, two) = two_page_book()
    run = services.start_assembly(f.book, editor, {"footnote_numbering": "book"})
    assert run.status == AssemblyRun.Status.DONE and run.stage == "save" and run.error == ""
    assert run.created_by == editor and run.finished_at is not None and run.task_id
    assert run.settings["footnote_numbering"] == "book"
    f.book.refresh_from_db()
    assert f.book.assembly_settings == {"footnote_numbering": "book"}
    manuscript = Manuscript.objects.get(book=f.book)
    assert manuscript.version == 1 and manuscript.origin == "assembly" and manuscript.run == run
    doc = manuscript.document
    assert doc["attrs"]["runId"] == run.pk and doc["attrs"]["assembledAt"] == run.finished_at.isoformat()
    heading, paragraph, second = doc["content"][1:]
    assert heading["type"] == "heading" and text_of(heading) == "الفصل الأول"
    assert text_of(paragraph) == "نص الفقرة[1] يبدأ هنا ويستمر حتى آخر| الصفحة ويتم الكلام."
    assert paragraph["attrs"]["sourcePages"] == [1, 2] and paragraph["attrs"]["reviewed"] is False
    assert text_of(second) == "فقرة ثانية"
    all_text = json.dumps(doc, ensure_ascii=False)
    assert "ترويسة" not in all_text and "١٢" not in all_text  # running header and page number left out
    assert run.stats["pages_included"] == 2 and run.stats["joins"] == 1 and run.stats["footnotes"] == 1
    assert {w["code"] for w in run.warnings} >= {"page_unreviewed", "uncertain_words"}
    at = run.included[str(one.pk)]["at"]  # D78: when the pages were read (before the run finished)
    assert run.included == {
        str(one.pk): {"number": 1, "reviewed": True, "sig": run.included[str(one.pk)]["sig"], "at": at},
        str(two.pk): {"number": 2, "reviewed": False, "sig": run.included[str(two.pk)]["sig"], "at": at},
    }
    assert __import__("datetime").datetime.fromisoformat(at) <= run.finished_at
    one.refresh_from_db()
    two.refresh_from_db()
    assert one.status == Page.Status.ASSEMBLED and two.status == Page.Status.OCR_DONE  # D36
    f.book.refresh_from_db()
    assert f.book.status == Book.Status.REVIEWING


def test_a_fully_reviewed_book_becomes_assembled(editor):
    f = Factory()
    for number in (1, 2):
        f.line(f.page(number), f"نص الصفحة {number}.")
    services.start_assembly(f.book, editor)
    f.book.refresh_from_db()
    assert f.book.status == Book.Status.ASSEMBLED
    assert set(f.book.pages.values_list("status", flat=True)) == {Page.Status.ASSEMBLED}


def test_start_assembly_is_idempotent_while_a_run_is_queued_or_running(editor):
    f, _ = two_page_book()
    queued = AssemblyRun.objects.create(book=f.book, status=AssemblyRun.Status.QUEUED)
    assert services.start_assembly(f.book, editor).pk == queued.pk
    assert AssemblyRun.objects.count() == 1 and not Manuscript.objects.exists()
    AssemblyRun.objects.filter(pk=queued.pk).update(status=AssemblyRun.Status.RUNNING)
    assert services.start_assembly(f.book, editor).pk == queued.pk  # nothing changed
    follow_up = services.start_assembly(f.book, editor, {"strip_tatweel": False})  # options changed
    assert follow_up.pk != queued.pk and follow_up.status == AssemblyRun.Status.DONE


def test_abandoned_runs_are_closed_and_a_new_one_starts(editor):
    f, _ = two_page_book()
    lost = AssemblyRun.objects.create(book=f.book, status=AssemblyRun.Status.RUNNING)
    AssemblyRun.objects.filter(pk=lost.pk).update(created_at=timezone.now() - services.ABANDONED_AFTER * 2)
    run = services.start_assembly(f.book, editor)
    lost.refresh_from_db()
    assert lost.status == AssemblyRun.Status.ERROR and lost.error == services.ABANDONED_ERROR
    assert run.pk != lost.pk and run.status == AssemblyRun.Status.DONE


@pytest.mark.parametrize(
    "options", [{"footnote_numbering": "volume"}, {"include_unreviewed": "maybe"}, {"strip_tatweel": [1]}]
)
def test_bad_options_are_refused_in_arabic(editor, options):
    f, _ = two_page_book()
    with pytest.raises(services.AssemblyError) as exc:
        services.start_assembly(f.book, editor, options)
    assert re.search("[ء-ي]", str(exc.value)) and not AssemblyRun.objects.exists()


def test_include_unreviewed_false_leaves_ocr_done_pages_out(editor):
    f, (_, two) = two_page_book()
    run = services.start_assembly(f.book, editor, {"include_unreviewed": "false"})
    assert run.stats["pages_included"] == 1 and run.stats["pages_skipped"] == 1
    assert str(two.pk) not in run.included


def test_reassembly_snapshots_the_previous_document_and_prunes_old_snapshots(editor):
    f, _ = two_page_book()
    services.start_assembly(f.book, editor)
    manuscript = Manuscript.objects.get(book=f.book)
    first_doc = manuscript.document
    for _ in range(11):
        ManuscriptSnapshot.objects.create(manuscript=manuscript, document={}, version=0, reason="reassembly")
    manual = ManuscriptSnapshot.objects.create(manuscript=manuscript, document={}, version=0, reason="manual")
    services.start_assembly(f.book, editor)
    manuscript.refresh_from_db()
    assert manuscript.version == 2
    newest = manuscript.snapshots.filter(reason="reassembly").order_by("-created_at", "-id").first()
    assert newest.document == first_doc and newest.version == 1 and newest.created_by == editor
    assert newest.label == "قبل إعادة التجميع · الإصدار 1"
    assert manuscript.snapshots.filter(reason="reassembly").count() == services.SNAPSHOTS_KEPT
    assert ManuscriptSnapshot.objects.filter(pk=manual.pk).exists()


def test_a_failed_run_keeps_the_old_manuscript(editor, monkeypatch):
    f, _ = two_page_book()
    services.start_assembly(f.book, editor)
    before = Manuscript.objects.get(book=f.book)

    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(services.pipeline, "assemble", boom)
    run = services.start_assembly(f.book, editor, {"strip_tatweel": False})
    assert run.status == AssemblyRun.Status.ERROR and run.error.startswith(services.RUN_ERROR)
    assert "RuntimeError: kaboom" in run.error and run.finished_at is not None
    after = Manuscript.objects.get(book=f.book)
    assert after.version == before.version and after.document == before.document
    state = services.manuscript_state(f.book)
    assert state["run"] == {"id": run.pk, "status": "error", "stage": "collect", "error": services.RUN_ERROR}
    assert state["exists"] is True and state["active"] is False


def test_an_older_run_never_overwrites_a_newer_manuscript(editor):
    f, _ = two_page_book()
    older = AssemblyRun.objects.create(book=f.book, status=AssemblyRun.Status.QUEUED)
    newer = AssemblyRun.objects.create(book=f.book, status=AssemblyRun.Status.QUEUED)
    services.run_assembly(newer.pk)
    services.run_assembly(older.pk)
    manuscript = Manuscript.objects.get(book=f.book)
    assert manuscript.run_id == newer.pk and manuscript.version == 1
    older.refresh_from_db()
    assert older.status == AssemblyRun.Status.DONE
    assert services.run_assembly(older.pk).status == AssemblyRun.Status.DONE  # not queued: left alone
    assert services.run_assembly(10**6) is None


def test_the_task_runs_a_queued_run(db):
    from assembly.tasks import assemble_book

    f, _ = two_page_book()
    run = AssemblyRun.objects.create(book=f.book, status=AssemblyRun.Status.QUEUED)
    assert assemble_book.delay(run.pk).get() == run.pk
    run.refresh_from_db()
    assert run.status == AssemblyRun.Status.DONE and Manuscript.objects.filter(book=f.book).exists()


def test_a_run_the_broker_refuses_fails_at_once_and_the_next_start_is_a_new_run(editor, monkeypatch):
    from assembly import tasks

    f, _ = two_page_book()

    def refuse(*args, **kwargs):
        raise ConnectionError("Error 61 connecting to localhost:6379")

    monkeypatch.setattr(tasks.assemble_book, "delay", refuse)
    run = services.start_assembly(f.book, editor)
    assert run.status == AssemblyRun.Status.ERROR and run.error.startswith(services.ENQUEUE_ERROR)
    state = services.manuscript_state(f.book)
    assert state["active"] is False and state["run"]["error"] == services.ENQUEUE_ERROR
    monkeypatch.undo()
    again = services.start_assembly(f.book, editor)
    assert again.pk != run.pk and again.status == AssemblyRun.Status.DONE


def test_a_run_claimed_by_another_delivery_is_left_alone(db, monkeypatch):
    # Two deliveries of the task (acks_late) both read the run as queued: only one may run it.
    f, _ = two_page_book()
    run = AssemblyRun.objects.create(book=f.book, status=AssemblyRun.Status.QUEUED)
    stale = AssemblyRun.objects.get(pk=run.pk)
    AssemblyRun.objects.filter(pk=run.pk).update(status=AssemblyRun.Status.RUNNING, stage="footnotes")

    class Stale:
        def filter(self, **kwargs):
            return self

        def first(self):
            return stale

    monkeypatch.setattr(AssemblyRun.objects, "select_related", lambda *args: Stale())
    monkeypatch.setattr(services, "load_book", lambda book: pytest.fail("the run was run twice"))
    result = services.run_assembly(run.pk)
    assert result.status == AssemblyRun.Status.RUNNING and result.stage == "footnotes"
    assert not Manuscript.objects.filter(book=f.book).exists()


# ---------------------------------------------------------------- D36 and staleness


def test_editing_an_assembled_page_puts_it_back_to_reviewed_and_the_manuscript_goes_stale(editor):
    f, (one, _) = two_page_book()
    services.start_assembly(f.book, editor)
    state = services.manuscript_state(f.book)
    assert (
        state["exists"] and state["version"] == 1 and state["stale"] is False and state["stale_pages"] == []
    )
    assert state["stats"]["pages_included"] == 2 and state["warnings_count"] > 0
    assert state["assembled_at"] and state["run"]["status"] == "done" and state["unreviewed_pages"] == 1
    line = one.lines.get(text="ويستمر حتى آخر")
    review_services.edit_line(line, "ويستمر حتى آخر السطر", editor)
    one.refresh_from_db()
    f.book.refresh_from_db()
    assert one.status == Page.Status.REVIEWED and f.book.status == Book.Status.REVIEWING
    state = services.manuscript_state(f.book)
    assert state["stale"] is True and state["stale_pages"] == [1]
    services.start_assembly(f.book, editor)
    state = services.manuscript_state(f.book)
    assert state["stale"] is False and state["version"] == 2
    one.refresh_from_db()
    assert one.status == Page.Status.ASSEMBLED


@pytest.mark.parametrize("action", ["role", "delete", "insert", "resolve", "undo"])
def test_every_review_action_on_an_assembled_page_flips_it_and_marks_it_stale(editor, action):
    f, (one, _) = two_page_book()
    services.start_assembly(f.book, editor)
    line = one.lines.get(text="ويستمر حتى آخر")
    if action == "role":
        review_services.set_line_role(line, "subheading", editor)
    elif action == "delete":
        review_services.delete_line(line, editor)
    elif action == "insert":
        new = review_services.insert_line(one, line.pk, "سطر جديد", editor)
        assert new.is_reviewed is True
    elif action == "resolve":
        Line.objects.filter(pk=line.pk).update(tokens=[tok("ويستمر", "low"), tok("حتى"), tok("آخر")])
        review_services.resolve_token(Line.objects.get(pk=line.pk), 0, "primary", user=editor)
    else:
        review_services.set_line_role(line, "subheading", editor)
        services.start_assembly(f.book, editor)
        review_services.undo_last(one, editor)
    one.refresh_from_db()
    assert one.status == Page.Status.REVIEWED
    assert services.manuscript_state(f.book)["stale_pages"] == [1]


def test_approve_and_reopen_on_an_assembled_page(editor):
    f, (one, _) = two_page_book()
    services.start_assembly(f.book, editor)
    one.refresh_from_db()
    assert review_services.approve_page(one, editor)["status"] == Page.Status.ASSEMBLED  # nothing changes
    review_services.reopen_page(one, editor)
    one.refresh_from_db()
    assert one.status == Page.Status.OCR_DONE
    assert services.manuscript_state(f.book)["stale_pages"] == [1]  # reviewed flag changed
    review_services.undo_last(one, editor)  # the undo restores the snapshot, as before assembly
    one.refresh_from_db()
    assert one.status == Page.Status.ASSEMBLED
    assert services.manuscript_state(f.book)["stale"] is False


def test_new_eligible_pages_and_excluded_pages_make_the_manuscript_stale(editor):
    f, (one, two) = two_page_book()
    three = f.page(3, Page.Status.LAYOUT_DONE)
    services.start_assembly(f.book, editor)
    assert services.manuscript_state(f.book)["stale"] is False
    Page.objects.filter(pk=three.pk).update(status=Page.Status.OCR_DONE)
    assert services.manuscript_state(f.book)["stale_pages"] == [3]
    Page.objects.filter(pk=three.pk).update(status=Page.Status.LAYOUT_DONE)
    Page.objects.filter(pk=two.pk).update(is_excluded=True, status=Page.Status.EXCLUDED)
    assert services.manuscript_state(f.book)["stale_pages"] == [2]


def test_manuscript_state_before_any_run_costs_one_query(db, django_assert_num_queries):
    f, _ = two_page_book()
    rows = services.page_rows(f.book)
    with django_assert_num_queries(1):
        state = services.manuscript_state(f.book, rows)
    assert state == {
        "exists": False,
        "version": 0,
        "assembled_at": None,
        "run": None,
        "active": False,
        "stale": False,
        "stale_pages": [],
        "drift_pages": [],
        "warnings_count": 0,
        "stats": {},
        "options": {
            "footnote_numbering": "page",
            "include_unreviewed": True,
            "strip_tatweel": True,
            "strip_running_heads": True,
        },
        "unreviewed_pages": 1,
        "edited": False,
    }
    assert services.manuscript_state(f.book)["unreviewed_pages"] == 1


def test_manuscript_state_of_an_assembled_book_has_a_fixed_cost(editor, django_assert_num_queries):
    f, _ = two_page_book()
    services.start_assembly(f.book, editor)
    rows = services.page_rows(f.book)
    with django_assert_num_queries(3):
        services.manuscript_state(f.book, rows)


# ---------------------------------------------------------------- D70: approval is not drift; live marks


def test_drift_pages_leave_out_approval_only_pages_that_stale_pages_keep(editor):
    """Book 26's shape: a page approved after assembly, its lines untouched, is stale (a re-assembly brings
    the approval in, D36) but not drift; a text change, a new page and an excluded page are both."""
    f, (one, two) = two_page_book()
    three = f.page(3, Page.Status.LAYOUT_DONE)
    services.start_assembly(f.book, editor)
    run = Manuscript.objects.select_related("run").get(book=f.book).run
    options = pipeline.normalize_settings(f.book.assembly_settings)

    def changes():
        rows = services.page_rows(f.book)
        both = services.page_changes(run.included, rows, options)
        assert both == (
            services.stale_pages(run.included, rows, options),
            services.drift_pages(run.included, rows, options),
        )
        return both

    assert changes() == ([], [])
    review_services.approve_page(two, editor, force=True)
    assert changes() == ([2], [])
    state = services.manuscript_state(f.book)
    assert state["stale_pages"] == [2] and state["drift_pages"] == []
    review_services.edit_line(two.lines.get(text="فقرة ثانية"), "فقرة ثانية مصححة", editor)
    assert changes() == ([2], [2])
    Page.objects.filter(pk=three.pk).update(status=Page.Status.OCR_DONE)  # newly eligible
    Page.objects.filter(pk=one.pk).update(is_excluded=True, status=Page.Status.EXCLUDED)  # left out
    assert changes() == ([1, 2, 3], [1, 2, 3])


def test_reopening_an_assembled_page_is_stale_but_not_drift(editor):
    f, (one, _) = two_page_book()
    services.start_assembly(f.book, editor)
    one.refresh_from_db()
    review_services.reopen_page(one, editor)
    state = services.manuscript_state(f.book)
    assert state["stale_pages"] == [1] and state["drift_pages"] == []


def test_the_amber_mark_follows_the_live_page_status():
    """D35's mark (`data-reviewed`) comes from the live status of the block's source pages; without the
    status (or for a block with no source page, or a page since left out) from the stored attribute."""

    def para(block_id, pages, reviewed):
        attrs = {"id": block_id, "sourcePages": pages, "reviewed": reviewed}
        return {"type": "paragraph", "attrs": attrs, "content": [{"type": "text", "text": block_id}]}

    doc = {
        "type": "doc",
        "content": [
            para("p1", [2], False),
            para("p2", [2, 3], False),
            para("p3", [], False),
            para("p4", [9], True),  # page 9 is not in the book (left out since): the attribute
            para("p5", [1], True),
        ],
    }

    def marks(html):
        return dict(re.findall(r'data-block="(p\d)"[^>]*data-reviewed="(true|false)"', html))

    assert marks(render.render_document(doc)) == {
        "p1": "false",
        "p2": "false",
        "p3": "false",
        "p4": "true",
        "p5": "true",
    }
    live = {1: False, 2: True, 3: False}
    html = render.render_document(doc, reviewed=live)
    assert marks(html) == {"p1": "true", "p2": "false", "p3": "false", "p4": "true", "p5": "false"}
    assert html.count(render.UNREVIEWED_TITLE) == 3


def test_a_page_approved_after_assembly_loses_its_amber_mark_at_once(editor):
    f, (_, two) = two_page_book()
    services.start_assembly(f.book, editor)
    client = logged(editor)
    url = reverse("assembly:document", args=[f.book.pk])

    def page_two_marks():
        html = client.get(url).content.decode()
        return set(re.findall(r'data-pages="2"[^>]*data-reviewed="(true|false)"', html))

    assert page_two_marks() == {"false"}
    review_services.approve_page(two, editor, force=True)
    assert page_two_marks() == {"true"}  # no re-assembly needed
    two.refresh_from_db()
    review_services.reopen_page(two, editor)
    assert page_two_marks() == {"false"}


# ---------------------------------------------------------------- overrides


def test_seam_override_is_stored_and_reapplied(editor):
    f, _ = two_page_book()
    run = services.set_seam_override(f.book, editor, "2", "split")
    assert run.status == "done"
    f.book.refresh_from_db()
    assert f.book.assembly_settings["seams"] == {"2": "split"}
    seams = Manuscript.objects.get(book=f.book).document["attrs"]["seams"]
    assert seams == [
        {"page": 2, "from_page": 1, "mode": "split", "decision": "override", "reason": "geometry"}
    ]
    services.set_seam_override(f.book, editor, 2, "auto")
    f.book.refresh_from_db()
    assert f.book.assembly_settings["seams"] == {}
    assert Manuscript.objects.get(book=f.book).document["attrs"]["seams"][0]["mode"] == "join"


@pytest.mark.parametrize(
    ("page", "mode", "error"),
    [
        ("x", "join", services.AssemblyError),
        (0, "join", services.AssemblyError),
        (True, "join", services.AssemblyError),
        (2, "glue", services.AssemblyError),
        (99, "join", services.AssemblyNotFound),
    ],
)
def test_seam_override_validation(editor, page, mode, error):
    f, _ = two_page_book()
    with pytest.raises(error):
        services.set_seam_override(f.book, editor, page, mode)


def test_dismiss_suggestion_is_stored_once(editor):
    f, _ = two_page_book()
    services.dismiss_suggestion(f.book, editor, "p12")
    services.dismiss_suggestion(f.book, editor, "p12")
    f.book.refresh_from_db()
    assert f.book.assembly_settings["dismissed_suggestions"] == ["p12"]
    with pytest.raises(services.AssemblyError):
        services.dismiss_suggestion(f.book, editor, "h12")
    with pytest.raises(services.AssemblyError):
        services.dismiss_suggestion(f.book, editor, "p12", "apply")


def test_block_roles_go_through_review_one_revision_per_changed_line(proofreader):
    f, (one, _) = two_page_book()
    lines = list(one.lines.filter(role="body", region__kind="body").order_by("order"))
    run = services.set_block_roles(f.book, proofreader, [lines[0].pk, str(lines[1].pk)], "heading")
    assert run.status == "done"
    assert list(one.lines.filter(pk__in=[lines[0].pk, lines[1].pk]).values_list("role", flat=True)) == [
        "heading",
        "heading",
    ]
    assert LineRevision.objects.filter(page=one, action="role").count() == 2
    services.set_block_roles(f.book, proofreader, [lines[0].pk], "heading")  # unchanged: no revision
    assert LineRevision.objects.filter(page=one, action="role").count() == 2


def test_block_roles_are_all_or_nothing(proofreader):
    f, (one, _) = two_page_book()
    other, _ = two_page_book()
    body = one.lines.get(text="ويستمر حتى آخر")
    waiting = f.page(3, Page.Status.LAYOUT_DONE)  # its text is not there yet: review refuses it
    unread = f.line(waiting, "سطر لم يُقرأ بعد")
    foreign = other.book.pages.get(number=1).lines.filter(region__kind="body").first()
    with pytest.raises(services.AssemblyNotFound):
        services.set_block_roles(f.book, proofreader, [body.pk, foreign.pk], "heading")
    with pytest.raises(services.AssemblyError) as exc:
        services.set_block_roles(f.book, proofreader, [body.pk, unread.pk], "heading")
    assert str(exc.value) == "الصفحة ليست جاهزة للمراجعة بعد؛ انتظر حتى ينتهي التعرّف على نصها."
    body.refresh_from_db()
    assert body.role == "body" and not LineRevision.objects.exists()
    for bad in ([], "12", ["x"], [True]):
        with pytest.raises(services.AssemblyError):
            services.set_block_roles(f.book, proofreader, bad, "heading")
    with pytest.raises(services.AssemblyError):
        services.set_block_roles(f.book, proofreader, [body.pk], "chapter")


# ---------------------------------------------------------------- API


def test_api_permissions_per_role(db):
    f, (one, _) = two_page_book()
    line = one.lines.get(text="ويستمر حتى آخر")
    anonymous = Client()
    assert post(anonymous, "book_assemble", f.book.pk).status_code == 403
    assert anonymous.get(reverse("api:manuscript_state", args=[f.book.pk])).status_code == 403
    guest = logged(role_user("guest", None))
    reader = logged(role_user("reader", "proofreader"))
    for client in (guest, reader):
        response = post(client, "book_assemble", f.book.pk)
        assert response.status_code == 403 and response.json()["detail"] == "هذا الإجراء يتطلب صلاحية محرّر."
        assert post(client, "manuscript_seam", f.book.pk, {"page": 2, "mode": "split"}).status_code == 403
        assert post(client, "manuscript_suggestion", f.book.pk, {"block_id": "p1"}).status_code == 403
    assert (
        post(guest, "manuscript_roles", f.book.pk, {"line_ids": [line.pk], "role": "heading"}).status_code
        == 403
    )
    assert (
        post(reader, "manuscript_roles", f.book.pk, {"line_ids": [line.pk], "role": "heading"}).status_code
        == 202
    )
    assert guest.get(reverse("api:manuscript_state", args=[f.book.pk])).status_code == 200
    admin = logged(role_user("boss", "admin"))
    assert post(admin, "book_assemble", f.book.pk).status_code == 202


def test_api_assemble_state_and_manuscript(editor):
    f, _ = two_page_book()
    client = logged(editor)
    missing = client.get(reverse("api:manuscript", args=[f.book.pk]))
    assert missing.status_code == 404 and missing.json()["detail"] == "لم يُجمَّع هذا الكتاب بعد."
    response = post(client, "book_assemble", f.book.pk, {"footnote_numbering": "page", "strip_tatweel": True})
    assert response.status_code == 202
    body = response.json()
    run = AssemblyRun.objects.get()
    assert body == {
        "run_id": run.pk,
        "status": "done",
        "stage": "save",
        "manuscript_url": reverse("assembly:manuscript", args=[f.book.pk]),
        "state_url": reverse("api:manuscript_state", args=[f.book.pk]),
    }
    state = client.get(body["state_url"]).json()
    assert (
        state["exists"] is True
        and state["run"]["id"] == run.pk
        and state["options"]["footnote_numbering"] == "page"
    )
    data = client.get(reverse("api:manuscript", args=[f.book.pk])).json()
    assert set(data) == {"document", "warnings", "stats", "seams", "version", "reviewed"}
    assert data["reviewed"] == {"1": True, "2": False}  # page number → reviewed now (D70's amber mark)
    assert data["version"] == 1 and data["seams"] == data["document"]["attrs"]["seams"]
    assert data["warnings"] == run.warnings and data["stats"] == run.stats


def test_api_errors_are_arabic_with_the_right_status(editor):
    f, (one, _) = two_page_book()
    other, _ = two_page_book()
    client = logged(editor)
    bad = post(client, "book_assemble", f.book.pk, {"footnote_numbering": "volume"})
    assert bad.status_code == 400 and bad.json()["detail"] == "طريقة ترقيم الحواشي غير معروفة."
    assert post(client, "book_assemble", 10**6).status_code == 404
    assert post(client, "book_assemble", 10**6).json()["detail"] == "الكتاب غير موجود."
    assert client.get(reverse("api:manuscript_state", args=[10**6])).status_code == 404
    assert post(client, "manuscript_seam", f.book.pk, {"page": 2, "mode": "glue"}).status_code == 400
    assert post(client, "manuscript_seam", f.book.pk, {"page": 42, "mode": "join"}).status_code == 404
    foreign = other.book.pages.get(number=1).lines.filter(region__kind="body").first()
    roles = post(client, "manuscript_roles", f.book.pk, {"line_ids": [foreign.pk], "role": "heading"})
    assert roles.status_code == 404 and roles.json()["detail"] == "السطر غير موجود في هذا الكتاب."
    unread = f.line(f.page(3, Page.Status.LAYOUT_DONE), "سطر لم يُقرأ بعد")
    roles = post(client, "manuscript_roles", f.book.pk, {"line_ids": [unread.pk], "role": "heading"})
    assert roles.status_code == 400
    assert roles.json()["detail"] == "الصفحة ليست جاهزة للمراجعة بعد؛ انتظر حتى ينتهي التعرّف على نصها."
    roles = post(client, "manuscript_roles", f.book.pk, {"line_ids": [unread.pk], "role": "main"})
    assert roles.status_code == 400 and roles.json()["detail"] == "نوع السطر غير معروف."
    assert (
        post(client, "manuscript_suggestion", f.book.pk, {"block_id": "x", "action": "dismiss"}).status_code
        == 400
    )
    assert not AssemblyRun.objects.exists()


def test_api_seams_roles_and_suggestions_start_runs(editor):
    f, (one, _) = two_page_book()
    client = logged(editor)
    seam = post(client, "manuscript_seam", f.book.pk, {"page": 2, "mode": "split"})
    assert seam.status_code == 202 and seam.json()["status"] == "done"
    line = one.lines.get(text="ويستمر حتى آخر")
    roles = post(client, "manuscript_roles", f.book.pk, {"line_ids": [line.pk], "role": "subheading"})
    assert roles.status_code == 202
    suggestion = post(client, "manuscript_suggestion", f.book.pk, {"block_id": "p5", "action": "dismiss"})
    assert suggestion.status_code == 202
    assert AssemblyRun.objects.filter(status="done").count() == 3
    f.book.refresh_from_db()
    assert f.book.assembly_settings == {"seams": {"2": "split"}, "dismissed_suggestions": ["p5"]}
    doc = Manuscript.objects.get(book=f.book).document
    assert any(n["type"] == "heading" and n["attrs"]["level"] == 2 for n in doc["content"])


# ---------------------------------------------------------------- views, dashboard, command


def test_manuscript_views_need_a_login_and_render(editor):
    f, _ = two_page_book()
    url = reverse("assembly:manuscript", args=[f.book.pk])
    assert Client().get(url).status_code == 302
    client = logged(editor)
    page = client.get(url)
    assert page.status_code == 200 and 'id="manuscript-config"' in page.content.decode()
    services.start_assembly(f.book, editor)
    fragment = client.get(reverse("assembly:document", args=[f.book.pk]))
    assert fragment.status_code == 200 and 'data-block="h' in fragment.content.decode()
    assert client.get(reverse("assembly:manuscript", args=[10**6])).status_code == 404


def test_dashboard_config_and_progress_carry_the_manuscript(editor):
    from books import services as book_services

    f, _ = two_page_book()
    dashboard = book_services.book_dashboard(f.book)
    assert dashboard["config"]["manuscript"]["exists"] is False
    assert dashboard["config"]["manuscriptUrls"] == services.manuscript_urls(f.book)
    assert dashboard["manuscript_urls"]["assemble"] == reverse("api:book_assemble", args=[f.book.pk])
    services.start_assembly(f.book, editor)
    data = logged(editor).get(reverse("api:book_progress", args=[f.book.pk]) + "?compact=1").json()
    assert data["manuscript"]["exists"] is True and data["manuscript"]["stale"] is False
    assert data["manuscript"]["version"] == 1


def test_assemble_command_dry_run_writes_nothing(db):
    f, _ = two_page_book()
    out = StringIO()
    call_command("assemble", f.book.pk, "--dry-run", "--show", "2", stdout=out)
    text = out.getvalue()
    assert "dry run" in text and "stats: pages_included=2" in text and "seam 1→2: join" in text
    assert "page_unreviewed p2" in text and "heading h" in text
    assert not AssemblyRun.objects.exists() and not Manuscript.objects.exists()
    assert set(f.book.pages.values_list("status", flat=True)) == {"reviewed", "ocr_done"}


def test_assemble_command_runs_and_saves(db):
    f, _ = two_page_book()
    out = StringIO()
    call_command("assemble", f.book.pk, stdout=out)
    assert "manuscript v1" in out.getvalue()
    assert Manuscript.objects.get(book=f.book).version == 1
    assert f.book.pages.get(number=1).status == Page.Status.ASSEMBLED


# ---------------------------------------------------------------- D85: the linker's text-side repairs


def test_a_note_marker_read_as_a_lookalike_takes_the_next_number_of_its_page():
    """Book 31 p. 28: the note marker «(١)» read «(أ)» made a marker-less orphan note (D85)."""
    first = run(
        [
            pg(
                1,
                [
                    ln("كلام (١) ثم كلام (٢) هنا", id=10),
                    ln("(أ) الأولى", kind="footnote", id=11),
                    ln("(٢) الثانية", kind="footnote", id=12),
                ],
            )
        ]
    )
    assert [(n["attrs"]["orphan"], n["attrs"]["id"]) for n in notes_of(first)] == [
        (False, "n11"),
        (False, "n12"),
    ]
    assert text_of(blocks_of(first)[0]) == "كلام[1] ثم كلام[2] هنا"
    second = run(
        [
            pg(
                1,
                [
                    ln("كلام (١) ثم كلام (٢) هنا", id=20),
                    ln("(١) الأولى", kind="footnote", id=21),
                    ln("(”) الثانية", kind="footnote", id=22),
                ],
            )
        ]
    )
    assert [n["attrs"]["orphan"] for n in notes_of(second)] == [False, False]
    # among lettered items «(ب)», «(ج)» the «(أ)» is a letter of the note: it continues the note
    lettered = run(
        [
            pg(
                1,
                [
                    ln("كلام (١) هنا", id=30),
                    ln("(١) حاشية فيها أقسام :", kind="footnote", id=31),
                    ln("(أ) القسم الأول", kind="footnote", id=32),
                    ln("(ب) القسم الثاني", kind="footnote", id=33),
                ],
            )
        ]
    )
    (note,) = notes_of(lettered)
    assert note["attrs"]["sourceLineIds"] == [31, 32, 33] and note["attrs"]["orphan"] is False


def test_a_bare_number_in_the_notes_of_a_bracketed_page_is_a_marker_only_in_sequence():
    """Book 31 p. 40: «١٢١ ـ عن عائشة» inside a commentary note made a note «121» (D85)."""
    inside = run(
        [
            pg(
                1,
                [
                    ln("كلام (١) هنا", id=10),
                    ln("(١) قال الحافظ : وفي الحديث", kind="footnote", id=11),
                    ln("١٢١ ـ عن عائشة رضي الله عنها", kind="footnote", id=12),
                ],
            )
        ]
    )
    (note,) = notes_of(inside)
    assert note["attrs"]["sourceLineIds"] == [11, 12] and note["attrs"]["orphan"] is False
    assert "note_orphan" not in codes(inside)
    # the next number of the sequence is a marker, bracketed or not
    sequence = run(
        [
            pg(
                1,
                [
                    ln("كلام (١) ثم (٢) هنا", id=20),
                    ln("(١) الأولى", kind="footnote", id=21),
                    ln("٢ ـ الثانية", kind="footnote", id=22),
                ],
            )
        ]
    )
    assert [n["attrs"]["orphan"] for n in notes_of(sequence)] == [False, False]
    # a page printing bare markers only is as before
    bare = run([pg(1, [ln("كلام ٣ هنا", id=30), ln("٣ ـ حاشية", kind="footnote", id=31)])])
    (note,) = notes_of(bare)
    assert note["attrs"]["marker"] == "٣"
    # D87: a number of two or three digits out of the sequence is a hadith's number, on any page
    hadith = run(
        [
            pg(1, [ln("كلام (١) هنا", id=40), ln("(١) شرح طويل", kind="footnote", id=41)]),
            pg(
                2,
                [
                    ln("كلام هنا", id=50),
                    ln("يتصل الشرح", kind="footnote", id=51),
                    ln("١٩ ـ وعن أبي هريرة", kind="footnote", id=52),
                ],
            ),
        ]
    )
    (note,) = notes_of(hadith)
    assert note["attrs"]["sourceLineIds"] == [41, 51, 52]
    # book 31 p. 107: a bracketed reference «(١٨)» after the page's «(١)» is text too
    reference = run(
        [
            pg(
                1,
                [
                    ln("كلام (١) هنا", id=60),
                    ln("(١) كتاب الزكاة", kind="footnote", id=61),
                    ln("(١٨) (٢٣٠/٢) .", kind="footnote", id=62),
                ],
            )
        ]
    )
    (note,) = notes_of(reference)
    assert note["attrs"]["sourceLineIds"] == [61, 62] and note["attrs"]["orphan"] is False


def test_a_carried_note_line_read_with_a_number_above_the_pages_first_marker_continues():
    """Book 32 p. 2: the printed «=» of a carried note was read «3»; below it the page's notes start
    again at «(١)», so «3» is out of sequence and the line continues the note before (D85)."""
    result = run(
        [
            pg(
                1,
                [
                    ln("كلام (١) ثم (٢) هنا", id=10),
                    ln("(١) الأولى", kind="footnote", id=11),
                    ln("(٢) الثانية تمتد", kind="footnote", id=12),
                ],
            ),
            pg(
                2,
                [
                    ln("متن (١) هنا", id=20),
                    ln("3 :٥٤ وقد انقلب اسمه", kind="footnote", id=21),
                    ln("(١) في نسبة الشهيد", kind="footnote", id=22),
                ],
            ),
        ]
    )
    notes = notes_of(result)
    assert [n["attrs"]["sourceLineIds"] for n in notes] == [[11], [12, 21], [22]]
    assert not any(n["attrs"]["orphan"] for n in notes)


def test_a_note_number_missing_from_the_pages_sequence_is_restored_on_its_one_possible_line():
    """Book 35 p. 2: the model dropped «(٢)» and moved the period of the line above to its start; notes
    (١) and (٣) with one line between them: that line is note 2 (D87)."""
    result = run(
        [
            pg(
                1,
                [
                    ln("متن (١) ثم (٢) ثم (٣) هنا", id=10),
                    ln("(١) انظر: نيل السول: ٦٠٤", kind="footnote", id=11),
                    ln(". راجع تقديمه لطبعة الكتاب الفاسية.", kind="footnote", id=12),
                    ln("(٣) في المعسول: ٨/٢٨٥ .", kind="footnote", id=13),
                ],
            )
        ]
    )
    notes = notes_of(result)
    assert [n["attrs"]["sourceLineIds"] for n in notes] == [[11], [12], [13]]
    assert not any(n["attrs"]["orphan"] for n in notes)
    assert notes[0]["content"][0]["text"].endswith("604.")  # the period moved back to the note it ends
    assert notes[1]["content"][0]["text"].startswith("راجع")
    # two lines that may start the missing note: which one is not known, the notes stay as read
    unsure = run(
        [
            pg(
                1,
                [
                    ln("متن (١) ثم (٣) هنا", id=20),
                    ln("(١) الأولى.", kind="footnote", id=21),
                    ln("ثانية.", kind="footnote", id=22),
                    ln("ثالثة", kind="footnote", id=23),
                    ln("(٣) الأخيرة", kind="footnote", id=24),
                ],
            )
        ]
    )
    assert [n["attrs"]["sourceLineIds"] for n in notes_of(unsure)] == [[21, 22, 23], [24]]
    # book 35 p. 3: a stray «أ» read before «(١)» does not hide the marker
    stray = run(
        [
            pg(
                1,
                [
                    ln("متن (١) ثم (٢) ثم (٣) هنا", id=30),
                    ln("أ (١) ويعني بها تنقيح الفصول", kind="footnote", id=31),
                    ln("الشاطبي أي الموافقات", kind="footnote", id=32),
                    ln(". يقصد تقييد عبدالله.", kind="footnote", id=33),
                    ln("(٣) نيل السول: ٥ .", kind="footnote", id=34),
                ],
            )
        ]
    )
    assert [n["attrs"]["sourceLineIds"] for n in notes_of(stray)] == [[31, 32], [33], [34]]
    assert not any(n["attrs"]["orphan"] for n in notes_of(stray))


def test_a_note_number_misread_against_the_pages_sequence_takes_its_place_in_it():
    """Book 34 p. 2: the notes read (١), (٤), (٣), (٤): the second is 2 (D87)."""
    result = run(
        [
            pg(
                1,
                [
                    ln("متن (١) ثم (٢) ثم (٣) ثم (٤) هنا", id=10),
                    ln("(١) الأولى", kind="footnote", id=11),
                    ln("(٤) الثانية", kind="footnote", id=12),
                    ln("(٣) الثالثة", kind="footnote", id=13),
                    ln("(٤) الرابعة", kind="footnote", id=14),
                ],
            )
        ]
    )
    notes = notes_of(result)
    assert [n["attrs"]["sourceLineIds"] for n in notes] == [[11], [12], [13], [14]]
    assert not any(n["attrs"]["orphan"] for n in notes)
    assert "marker_unmatched" not in codes(result)
    # book 29 p. 29: «(٢١)» after «(١)» is note 2 with the bracket read as a one
    hung = run(
        [
            pg(
                1,
                [
                    ln("متن (١) ثم (٢) هنا", id=30),
                    ln("(١) أ", kind="footnote", id=31),
                    ln("(٢١) ب", kind="footnote", id=32),
                ],
            )
        ]
    )
    assert [n["attrs"]["orphan"] for n in notes_of(hung)] == [False, False]
    # a page whose notes run on from the page before is left as read
    onward = run(
        [
            pg(
                1,
                [
                    ln("متن (١٢) ثم (١٣) هنا", id=20),
                    ln("(١٢) أ", kind="footnote", id=21),
                    ln("(١٣) ب", kind="footnote", id=22),
                ],
            )
        ]
    )
    assert [n["attrs"]["orphan"] for n in notes_of(onward)] == [False, False]


def test_lines_above_a_pages_first_new_note_continue_the_note_before_whatever_it_ends_with():
    """Book 31: a commentary note runs on over pages and often breaks at a sentence's end; the lines above
    the page's «(١)» are no note of this page, they continue the note before (D87)."""
    result = run(
        [
            pg(1, [ln("متن (١) هنا", id=10), ln("(١) شرح طويل انتهت جملته.", kind="footnote", id=11)]),
            pg(
                2,
                [
                    ln("متن (١) هنا", id=20),
                    ln("وفيه: إشارة إلى كذا.", kind="footnote", id=21),
                    ln("(١) حاشية الصفحة", kind="footnote", id=22),
                ],
            ),
        ]
    )
    notes = notes_of(result)
    assert [n["attrs"]["sourceLineIds"] for n in notes] == [[11, 21], [22]]
    assert not any(n["attrs"]["orphan"] for n in notes)
    # above a «(٢)» the lines may be note 1 with its marker lost: the guard decides as before (D74)
    lost = run(
        [
            pg(1, [ln("متن (١) هنا", id=30), ln("(١) شرح انتهى.", kind="footnote", id=31)]),
            pg(
                2,
                [
                    ln("متن (١) ثم (٢) هنا", id=40),
                    ln("حاشية فقدت علامتها", kind="footnote", id=41),
                    ln("(٢) الثانية", kind="footnote", id=42),
                ],
            ),
        ]
    )
    assert [n["attrs"]["sourceLineIds"] for n in notes_of(lost)] == [[31], [41], [42]]


def test_a_call_the_models_wrote_with_quote_strokes_is_linked():
    """Books 31, 34, 35: «صَدَقَةٌ ” “ .», «المخالفة"٢"،», «الأصول»"ا".» are the models' readings of a raised
    call; a quoted title stays text (D87)."""
    result = run(
        [
            pg(
                1,
                [
                    ln('فهي له صدقة ” “ . ومفهوم المخالفة"٢"، ثم علم الأصول»"ا". وقال "الموطأ" كذا', id=10),
                    ln("(١) الأولى", kind="footnote", id=11),
                    ln("(٢) الثانية", kind="footnote", id=12),
                    ln("(٣) الثالثة", kind="footnote", id=13),
                ],
            )
        ]
    )
    notes = notes_of(result)
    assert [n["attrs"]["orphan"] for n in notes] == [False, False, False]
    text = text_of(blocks_of(result)[0])
    assert text.startswith("فهي له صدقة[1]") and "المخالفة[2]،" in text and "الأصول»[3]" in text
    assert '"الموطأ"' in text


def test_a_second_call_with_the_number_of_a_linked_note_is_the_next_notes_call():
    """Book 32 p. 4: the calls read «(1)» and «(1)» for (١) and (٢); note 1 takes the first, the second is
    note 2's in the page's order (D87)."""
    result = run(
        [
            pg(
                1,
                [
                    ln("عبد الملك (1) ، ثم مات سنة 339 ـ (1) . وقال في التذكرة (٣) ، هنا", id=10),
                    ln("(١) الأولى", kind="footnote", id=11),
                    ln("(٢) الثانية", kind="footnote", id=12),
                    ln("(٣) الثالثة", kind="footnote", id=13),
                ],
            )
        ]
    )
    assert [n["attrs"]["orphan"] for n in notes_of(result)] == [False, False, False]
    assert "marker_unmatched" not in codes(result)
    # the same page as read: note 3 in note 2's line after its last sentence
    merged = run(
        [
            pg(
                1,
                [
                    ln("عبد الملك (1) ، ثم مات سنة 339 ـ (1) . وقال في التذكرة (٣) ، هنا", id=20),
                    ln("(١) الأولى", kind="footnote", id=21),
                    ln("(٢) الثانية وزدت كلمات. (3) م م :94 .", kind="footnote", id=22),
                ],
            )
        ]
    )
    notes = notes_of(merged)
    assert [n["attrs"]["orphan"] for n in notes] == [False, False, False]
    assert notes[1]["content"][0]["text"].endswith("كلمات.") and notes[2]["content"][0]["text"].startswith(
        "م م"
    )


def test_a_note_line_printed_with_an_equals_sign_continues_the_note_before():
    """Books 32 and 34 print «=» at the start of a note carried over from the page before: it continues
    that note even when the note ends a sentence and the page has calls of its own (D85)."""
    result = run(
        [
            pg(1, [ln("كلام (١) هنا", id=10), ln("(١) الأولى انتهت.", kind="footnote", id=11)]),
            pg(
                2,
                [
                    ln("متن (١) هنا", id=20),
                    ln("= تتمة الأولى", kind="footnote", id=21),
                    ln("(١) الثانية", kind="footnote", id=22),
                ],
            ),
        ]
    )
    notes = notes_of(result)
    assert [n["attrs"]["sourceLineIds"] for n in notes] == [[11, 21], [22]]
    assert not any(n["attrs"]["orphan"] for n in notes)
    assert "=" not in str(notes[0]["content"]) and "تتمة الأولى" in str(notes[0]["content"])


def test_a_call_read_with_one_bracket_is_a_lookalike():
    """Book 31 p. 23: the raised «(١)» after «»» read «(”» (D85)."""
    result = run([pg(1, [ln("فله الجنة » (” . ومنها", id=10), ln("(١) حاشية", kind="footnote", id=11)])])
    (note,) = notes_of(result)
    assert note["attrs"]["orphan"] is False
    assert text_of(blocks_of(result)[0]) == "فله الجنة»[1]. ومنها"
    assert codes(result).count("note_call_repaired") == 1


def test_a_leftover_call_pairs_only_in_the_order_of_its_page():
    # note (٢) takes its «(٢)»; the loose «(٣)» lies before it, where note (١)'s call must be: they pair
    before = run(
        [
            pg(
                1,
                [
                    ln("الأول (٣) ثم الثاني (٢) هنا", id=10),
                    ln("(١) الأولى", kind="footnote", id=11),
                    ln("(٢) الثانية", kind="footnote", id=12),
                ],
            )
        ]
    )
    assert text_of(blocks_of(before)[0]) == "الأول[1] ثم الثاني[2] هنا"
    # after note (٢)'s call it cannot be note (١)'s: the note stays an orphan, the call warns
    after = run(
        [
            pg(
                1,
                [
                    ln("الأول (٢) ثم الثاني (٣) هنا", id=20),
                    ln("(١) الأولى", kind="footnote", id=21),
                    ln("(٢) الثانية", kind="footnote", id=22),
                ],
            )
        ]
    )
    assert {n["attrs"]["id"]: n["attrs"]["orphan"] for n in notes_of(after)} == {"n21": True, "n22": False}
    assert "marker_unmatched" in codes(after) and "note_call_repaired" not in codes(after)
