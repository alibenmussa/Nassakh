"""Tests of the assembly app.

Part 1 exercises the pure pipeline (`assembly.pipeline`) on synthetic `PageIn` / `LineIn`: page
selection, typography (D37), paragraph geometry, seams, footnotes, headings and suggestions,
uncertain words, ids, stats, warnings and the document shape. Part 2 covers the services, the task,
the API, the placeholder views, the management command and the review / dashboard integration (D36).
"""

from __future__ import annotations

import json
import re
from io import StringIO

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

import pytest

from assembly import pipeline, services
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
    assert settings.footnote_numbering == "chapter"
    assert settings.include_unreviewed is False and settings.strip_tatweel is False
    assert settings.seams == {"12": "join", "14": "split"}
    assert settings.dismissed_suggestions == frozenset({"p12", "h9"})
    assert settings.as_dict() == {
        "footnote_numbering": "chapter",
        "include_unreviewed": False,
        "strip_tatweel": False,
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
        ln("بند مزاح يبدأ هنا", (0.1, 0.84), y=0.14, id=3),
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
                ln("متن الصفحة الأولى (٣) بلا حاشية مقابلة ثم الفيل٤ أيضًا ثم 7 رقم عادي", id=40),
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
    assert text_of(blocks_of(result)[0]) == "الأول[1] والثاني (2) وتكرار[2] آخر"
    assert codes(result).count("marker_unmatched") == 1  # (2)


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
    }


def test_document_shape():
    result = run(_book())
    doc = result.document
    assert doc["type"] == "doc"
    assert doc["attrs"] == {
        "bookId": 7,
        "runId": 31,
        "assembledAt": None,
        "footnoteNumbering": "chapter",
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
        pg(1, [ln("متن (٣) ثم", uncertain=[0]), ln("(١) حاشية", kind="footnote")], status="ocr_done"),
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
    assert run.included == {
        str(one.pk): {"number": 1, "reviewed": True, "sig": run.included[str(one.pk)]["sig"]},
        str(two.pk): {"number": 2, "reviewed": False, "sig": run.included[str(two.pk)]["sig"]},
    }
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
        "warnings_count": 0,
        "stats": {},
        "options": {"footnote_numbering": "chapter", "include_unreviewed": True, "strip_tatweel": True},
        "unreviewed_pages": 1,
    }
    assert services.manuscript_state(f.book)["unreviewed_pages"] == 1


def test_manuscript_state_of_an_assembled_book_has_a_fixed_cost(editor, django_assert_num_queries):
    f, _ = two_page_book()
    services.start_assembly(f.book, editor)
    rows = services.page_rows(f.book)
    with django_assert_num_queries(3):
        services.manuscript_state(f.book, rows)


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
    note = one.lines.get(region__kind="footnote")
    foreign = other.book.pages.get(number=1).lines.filter(region__kind="body").first()
    with pytest.raises(services.AssemblyNotFound):
        services.set_block_roles(f.book, proofreader, [body.pk, foreign.pk], "heading")
    with pytest.raises(services.AssemblyError) as exc:
        services.set_block_roles(f.book, proofreader, [body.pk, note.pk], "heading")
    assert str(exc.value) == "سطر الحاشية لا يكون عنوانًا."
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
    assert set(data) == {"document", "warnings", "stats", "seams", "version"}
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
    note = one.lines.get(region__kind="footnote")
    roles = post(client, "manuscript_roles", f.book.pk, {"line_ids": [note.pk], "role": "heading"})
    assert roles.status_code == 400 and roles.json()["detail"] == "سطر الحاشية لا يكون عنوانًا."
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
