"""The evaluation (`research.evaluation`, `research.eval_report`, `manage.py research_eval`) on a tiny
synthetic library: the cases are deterministic and labelled as they were made, each system's verdict maps
as designed, the data is left as it was, and the report renders."""

from __future__ import annotations

import json
import random

from django.core.management import call_command
from django.core.management.base import CommandError

import pytest

from accounts.models import Organization
from accounts.testing import owned
from books import access
from books.models import Book
from research import eval_report, index
from research import evaluation as ev
from research.conftest import make_page
from research.normalize import has_diacritics, normalize_text, vowel_key
from research.schemas import DiffSegment

pytestmark = pytest.mark.django_db

PLAIN = (
    "قال رسول الله صلى عليه وسلم حدثنا عبد بن مالك عن نافع اخبرنا يوسف العلم العمل النية الصبر الباب الكتاب "
    "الحديث الشيخ رحمه تعالى الإسلام الصلاة موسى الأعمال إمام آخر"
).split()
VOWELLED = {
    "قال": "قَالَ",
    "رسول": "رَسُولُ",
    "الله": "اللَّهِ",
    "عليه": "عَلَيْهِ",
    "وسلم": "وَسَلَّمَ",
    "العلم": "العِلْمُ",
    "الصبر": "الصَّبْرُ",
    "الكتاب": "الْكِتَابِ",
}
SKY = (
    "الفلك النجوم الشمس القمر السماء الارض البحر الجبل النهر الشجر الريح المطر السحاب البرق الرعد الليل"
).split()
TRUTH = {
    "exact_body": "exact",
    "exact_notes": "exact",
    "orthography": "exact",
    "replaced_word": "altered",
    "dropped_word": "altered",
    "swapped_words": "altered",
    "vowel_change": "vowels",
    "misattributed_note": "misattributed",
    "misattributed_body": "misattributed",
    "absent": "absent",
    "ocr_doubt": "doubt",
}


def _lines(pool: list[str], page: int, lines: int, vowelled: bool, doubtful: bool = False) -> list[str]:
    rng = random.Random(f"fixture:{page}:{lines}")
    out = []
    for _ in range(lines):
        words = [rng.choice(pool) for _ in range(8)]
        words = [VOWELLED.get(w, w) if vowelled else w for w in words]
        out.append(" ".join(words))
    if doubtful:
        first = out[0].split()
        first[3] += "~"
        out[0] = " ".join(first)
    return out


@pytest.fixture
def lab(org_a):
    """A book of six pages (body, notes on some, a doubtful word on two, vowels on the odd ones), and another
    book of sky words for the absent quotations."""
    book = owned(Book.objects.create(title="كتاب القياس", author="مؤلف"), org_a)
    for number in range(1, 7):
        make_page(
            book,
            number,
            _lines(PLAIN, number, 3, vowelled=number % 2 == 1, doubtful=number in (2, 4)),
            _lines(PLAIN, number + 100, 2, vowelled=False) if number % 2 == 0 else [],
            printed=str(10 + number),
        )
    sky = owned(Book.objects.create(title="كتاب آخر", author="مؤلف آخر"), org_a)
    for number in (1, 2):
        make_page(sky, number, _lines(SKY, number + 50, 3, vowelled=False))
    for item in (book, sky):
        index.reindex_book(item.pk)
    corpus = ev.Corpus(ev.load_docs([book.pk]), ev.load_docs([sky.pk], with_tokens=False))
    return {"book": book, "sky": sky, "corpus": corpus}


def _doc(corpus: ev.Corpus, case: ev.Case) -> ev.Doc:
    return next(
        doc
        for doc in corpus.docs
        if (doc.book_id, doc.page, doc.kind) == (case.source.book_id, case.source.page, case.source.kind)
    )


# ====================================================================== the cases


def test_cases_are_deterministic_per_seed_and_differ_between_seeds(lab):
    corpus = lab["corpus"]

    def make(seed):
        return [ev.make_cases(corpus, cls, seed, 5) for cls in ev.CLASSES]

    assert make(1) == make(1)
    assert make(1) != make(2)
    for cases in make(1):
        assert cases  # the fixture holds every class


def test_every_case_is_labelled_as_it_was_made(lab):
    corpus = lab["corpus"]
    for cls in ev.CLASSES:
        for case in ev.make_cases(corpus, cls, 3, 5):
            assert (case.cls, case.truth, case.band, case.key) == (cls, TRUTH[cls], ev.MAIN_BAND, cls)
            assert 3 <= case.n_words <= 14
            assert case.norm == normalize_text(case.quote)
            assert case.vowelled == has_diacritics(case.quote)


def test_exact_cases_are_verbatim_slices_of_the_raw_page(lab):
    corpus = lab["corpus"]
    for cls, kind, attributed in (("exact_body", "body", "author"), ("exact_notes", "notes", "editor")):
        for case in ev.make_cases(corpus, cls, 1, 6):
            assert case.source.kind == kind and case.attributed_to == attributed
            assert case.quote in _doc(corpus, case).raw
            assert 8 <= case.n_words <= 14


def test_orthography_cases_are_typed_differently_and_read_the_same(lab):
    corpus = lab["corpus"]
    cases = ev.make_cases(corpus, "orthography", 1, 8)
    assert cases
    for case in cases:
        doc = _doc(corpus, case)
        assert not has_diacritics(case.quote)
        assert case.norm in doc.norm
    assert any(case.quote not in _doc(corpus, case).raw for case in cases)


def test_altered_cases_differ_from_the_page_at_the_named_place(lab):
    corpus = lab["corpus"]
    for case in ev.make_cases(corpus, "replaced_word", 1, 6):
        words = case.norm.split()
        old, new = case.note.split("→")
        p = case.changed[0]
        assert words[p] == new and old != new and case.changed == (p, p + 1)
        words[p] = old
        assert " ".join(words) in _doc(corpus, case).norm
        assert new in corpus.vocab
    for case in ev.make_cases(corpus, "dropped_word", 1, 6):
        words = case.norm.split()
        p = case.changed[0]
        assert case.changed == (p, p) and 1 <= p <= len(words) - 1  # an inner word, never an edge
        words.insert(p, normalize_text(case.note[1:]))
        assert " ".join(words) in _doc(corpus, case).norm
    for case in ev.make_cases(corpus, "swapped_words", 1, 6):
        words = case.norm.split()
        p = case.changed[0]
        words[p], words[p + 1] = words[p + 1], words[p]
        assert " ".join(words) in _doc(corpus, case).norm
        assert case.norm not in _doc(corpus, case).norm


def test_a_vowel_case_changes_one_vowel_and_nothing_else(lab):
    corpus = lab["corpus"]
    cases = ev.make_cases(corpus, "vowel_change", 1, 6)
    assert cases
    for case in cases:
        doc = _doc(corpus, case)
        assert case.norm in doc.norm  # the letters are the page's
        assert case.quote not in doc.raw  # the vowels are not
        was, now = case.note.split("→")
        assert vowel_key(was) != vowel_key(now)


def test_misattributed_and_absent_cases(lab):
    corpus = lab["corpus"]
    for case in ev.make_cases(corpus, "misattributed_note", 1, 4):
        assert (case.source.kind, case.attributed_to, case.expect) == ("notes", "author", "note_not_author")
    for case in ev.make_cases(corpus, "misattributed_body", 1, 4):
        assert (case.source.kind, case.attributed_to, case.expect) == ("body", "editor", "body_not_editor")
    absent = ev.make_cases(corpus, "absent", 1, 4)
    assert absent
    for case in absent:
        assert case.source is None
        assert corpus.norm_index.find(case.norm) == []


def test_an_ocr_doubt_case_uses_the_real_other_reading_of_a_doubtful_word(lab):
    corpus = lab["corpus"]
    cases = ev.make_cases(corpus, "ocr_doubt", 1, 4)
    assert cases
    doubtful = {w["text"] for doc in corpus.docs for w in doc.words if w["state"] == index.DOUBTFUL}
    for case in cases:
        was, rest = case.note.split("→")
        reading, label = rest.split(" (")
        assert was in doubtful
        assert normalize_text(reading) == normalize_text(was) + "ا"  # the fixture's `alt`
        assert label == "alt)"
        assert normalize_text(reading) in case.norm.split()


def test_the_length_sweep_cases_carry_their_band(lab):
    corpus = lab["corpus"]
    cases = ev.make_cases(corpus, "replaced_word", 1, 5, (4, 5))
    assert cases and all(c.band == (4, 5) and c.key == "replaced_word@4-5" for c in cases)
    assert all(3 <= c.n_words <= 5 for c in cases)
    more = ev.make_cases(corpus, "replaced_3", 1, 4)  # the strength classes are made too
    assert more and all(c.truth == "altered" and c.n_words >= 8 for c in more)


def test_typing_like_people_keeps_the_normal_form():
    rng = random.Random(1)
    text = "إِنَّمَا الأَعْمَالُ بِالنِّيَّاتِ، وَالصَّلَاةُ مُوسَى رَحْمَةٌ."
    for _ in range(20):
        typed = ev.type_like_people(text, rng)
        assert normalize_text(typed) == normalize_text(text)
        assert not has_diacritics(typed)


# ====================================================================== the account


def test_the_account_is_a_rolled_back_member_of_an_organisation_holding_only_the_books(lab):
    book, sky = lab["book"], lab["sky"]
    before = (Organization.objects.count(), book.organization_id)
    with ev.eval_account([book.pk]) as user:
        assert not user.is_superuser
        assert [b.pk for b in access.books_for(user)] == [book.pk]  # `sky` is in the old organisation
        assert Book.objects.get(pk=book.pk).organization_id != book.organization_id
    assert (Organization.objects.count(), Book.objects.get(pk=book.pk).organization_id) == before
    assert not Organization.objects.filter(name="research_eval").exists()
    assert Book.objects.get(pk=sky.pk).organization_id == book.organization_id
    with pytest.raises(LookupError):
        with ev.eval_account([book.pk, 999999]):
            pass


# ====================================================================== the systems


def _first(corpus, cls, seed=1):
    return ev.make_cases(corpus, cls, seed, 3)[0]


def test_each_systems_verdict_on_each_kind_of_case(lab):
    corpus = lab["corpus"]
    with ev.eval_account([lab["book"].pk]) as user:
        run = {cls: ev.run_case(user, corpus, _first(corpus, cls)) for cls in ev.CLASSES}
    status = {cls: {s: v.status for s, v in verdicts.items()} for cls, verdicts in run.items()}
    both = {
        "nassakh": "exact",
        "nassakh_no_doubt": "exact",
        "raw_search": "exact",
        "normalized_search": "exact",
    }
    assert status["exact_body"] == both and status["exact_notes"] == both
    assert status["orthography"] == {**both, "raw_search": "not_found"}  # Ctrl+F fails on the spelling
    for cls in ("replaced_word", "dropped_word", "swapped_words"):
        assert status[cls] == {
            "nassakh": "differs",
            "nassakh_no_doubt": "differs",
            "raw_search": "not_found",
            "normalized_search": "not_found",
        }
    assert status["vowel_change"] == {**both, "raw_search": "not_found"}
    assert run["vowel_change"]["nassakh"].diacritics_differ  # only the vowels differ: named
    assert not run["vowel_change"]["normalized_search"].diacritics_differ  # a search cannot say so
    for cls, expected in (
        ("misattributed_note", "note_not_author"),
        ("misattributed_body", "body_not_editor"),
    ):
        assert status[cls] == both  # every system finds the text
        assert run[cls]["nassakh"].attribution == expected
    assert set(status["absent"].values()) == {"not_found"}
    # the doubt: the checker does not accuse, switched off it does, a search says «not found»
    assert status["ocr_doubt"] == {
        "nassakh": "needs_image_check",
        "nassakh_no_doubt": "differs",
        "raw_search": "not_found",
        "normalized_search": "not_found",
    }


def test_the_checker_names_where_the_quotation_differs(lab):
    corpus = lab["corpus"]
    with ev.eval_account([lab["book"].pk]) as user:
        for cls in ("replaced_word", "dropped_word", "swapped_words"):
            for case in ev.make_cases(corpus, cls, 2, 4):
                verdict = ev.run_case(user, corpus, case)["nassakh"]
                assert ev.names_where(verdict.spans, case.changed), (cls, case.quote, verdict.spans)
                assert verdict.pages == {(case.source.book_id, case.source.page)}
                assert verdict.printed is not None  # the printed page of the fixture


def test_diff_spans_and_names_where():
    diff = [
        DiffSegment(op="equal", source="a b"),
        DiffSegment(op="replaced", source="c", quote="x y"),
        DiffSegment(op="equal", source="d"),
        DiffSegment(op="missing", source="e"),
        DiffSegment(op="equal", source="f"),
        DiffSegment(op="added", quote="g"),
        DiffSegment(op="moved", quote="h"),
        DiffSegment(op="moved", source="h"),
    ]
    assert ev.diff_spans(diff) == ((2, 4), (5, 5), (6, 7), (7, 8), (8, 8))
    assert ev.names_where([(2, 4)], (3, 4)) and not ev.names_where([(2, 4)], (4, 5))
    assert ev.names_where([(5, 5)], (4, 5)) and ev.names_where([(5, 5)], (5, 5))  # a gap: the words beside it
    assert not ev.names_where([], (1, 2)) and not ev.names_where([(1, 2)], None)


def test_scoring_maps_a_verdict_to_the_classs_metrics():
    src = ev.Source(1, 2, "body")
    case = ev.Case("replaced_word", "q", "unknown", "altered", src, "q", 3, False, changed=(1, 2))
    right = ev.Verdict("differs", frozenset({(1, 2)}), spans=((1, 2),))
    got = ev.score(case, "nassakh", right, frozenset())
    assert got == {"location": True, "detected": True, "flagged": True, "located": True}
    unsure = ev.Verdict("needs_image_check", frozenset({(1, 2)}), spans=((1, 2),))
    assert ev.score(case, "nassakh", unsure, frozenset())["detected"] is False  # not «differs» ...
    assert ev.score(case, "nassakh", unsure, frozenset())["located"] is True  # ... but flagged, and where
    wrong_place = ev.Verdict("differs", frozenset({(1, 3)}), spans=((0, 1),))
    assert ev.score(case, "nassakh", wrong_place, frozenset()) == {
        "location": False,
        "detected": True,
        "flagged": True,
        "located": False,
    }
    search = ev.Verdict("not_found")
    assert ev.score(case, "raw_search", search, frozenset()) == {
        "location": False,
        "detected": True,
        "flagged": True,
    }
    doubt = ev.Case("ocr_doubt", "q", "unknown", "doubt", src, "q", 3, False, changed=(0, 1))
    assert (
        ev.score(doubt, "nassakh", ev.Verdict("needs_image_check", frozenset({(1, 2)})), frozenset())[
            "false_accusation"
        ]
        is False
    )
    assert ev.score(
        doubt, "nassakh_no_doubt", ev.without_doubt(ev.Verdict("needs_image_check")), frozenset()
    )["false_accusation"]
    assert ev.score(doubt, "raw_search", search, frozenset())["false_accusation"]  # «not found» accuses too
    twin = ev.Case("exact_body", "q", "author", "exact", src, "q", 3, False)
    assert ev.score(twin, "raw_search", ev.Verdict("exact", frozenset({(1, 9)})), frozenset({(1, 9)}))[
        "location"
    ]
    assert ev.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95) == 10 and ev.percentile([], 0.5) == 0.0


# ====================================================================== the run and the page


def test_the_whole_run_repeats_itself_and_leaves_the_data_alone(lab):
    book, sky = lab["book"], lab["sky"]
    config = ev.Config(books=(book.pk,), absent_books=(sky.pk,), seeds=2, n=3)
    before = (Organization.objects.count(), Book.objects.get(pk=book.pk).organization_id)
    report = ev.evaluate(config)
    assert (Organization.objects.count(), Book.objects.get(pk=book.pk).organization_id) == before
    json.dumps(report)  # JSON-ready
    repeat = report["repeatability"]
    assert repeat["same_cases"] and repeat["identical"] == repeat["cases"] > 0
    assert report["meta"]["seeds"] == [1, 2] and report["meta"]["titles"][str(book.pk)] == "كتاب القياس"
    rates = report["aggregate"]["rates"]
    assert rates["correct"]["nassakh"]["accepted"]["mean"] == 1.0
    assert rates["correct"]["raw_search"]["accepted"]["mean"] < 1.0  # the spelling case
    assert rates["altered"]["nassakh"]["located"]["mean"] == 1.0
    assert rates["ocr_doubt"]["nassakh_no_doubt"]["false_accusation"]["mean"] == 1.0
    assert rates["ocr_doubt"]["nassakh"]["false_accusation"]["mean"] == 0.0
    assert any("@" in key for key in rates) and "replaced_2" in rates  # the sweeps ran
    assert report["index"]["rows"] > 0 and report["index"]["build"]["pages"] == 6
    assert report["corpus"]["pages"] == 6


def test_the_report_renders_the_page(lab):
    book, sky = lab["book"], lab["sky"]
    report = ev.evaluate(
        ev.Config(books=(book.pk,), absent_books=(sky.pk,), seeds=2, n=3, index_timing=False)
    )
    text = eval_report.render(report, findings="ملاحظة يدوية.")
    for heading in (
        "# قياس التحقق",
        "## الخلاصة",
        "## الجداول الكاملة",
        "## الإخفاقات",
        "## إعادة التشغيل",
        "## حدود القياس",
    ):
        assert heading in text
    assert "manage.py research_eval --books" in text and "كتاب القياس" in text
    assert "ملاحظة يدوية." in text
    assert text.count("| نسّاخ |") >= 1 and "100%" in text
    assert (
        eval_report.pct(None) == "—"
        and eval_report.pct({"mean": 0.5, "min": 0.4, "max": 0.6}) == "50% (40–60)"
    )
    assert eval_report.pct({"mean": 1.0, "min": 1.0, "max": 1.0}) == "100%"


def test_the_command_writes_the_page_and_the_json(lab, tmp_path):
    book, sky = lab["book"], lab["sky"]
    out, data = tmp_path / "results.md", tmp_path / "results.json"
    call_command(
        "research_eval",
        "--books",
        str(book.pk),
        "--absent-books",
        str(sky.pk),
        "--seeds",
        "1",
        "--n",
        "3",
        "--out",
        str(out),
        "--json",
        str(data),
        "--no-repeat",
        "--no-index-timing",
    )
    assert "## حدود القياس" in out.read_text(encoding="utf-8")
    saved = json.loads(data.read_text(encoding="utf-8"))
    assert saved["meta"]["books"] == [book.pk] and saved["repeatability"] is None
    with pytest.raises(CommandError):
        call_command("research_eval", "--books", "999999", "--out", str(out))
    with pytest.raises(CommandError):
        call_command(
            "research_eval", "--books", str(book.pk), "--absent-books", str(book.pk), "--out", str(out)
        )
