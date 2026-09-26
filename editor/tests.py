"""Tests of the editor app: the manuscript and its snapshots (Phase 4); chapters, saves, find & replace,
digits, drift, re-assembly, the stylesheet and the pages (Phase 5); D47: block page-break attrs, the plain
text offsets and range edits, widows / orphans / book details, the uncertain words (listing with readings,
accept / choose / type, re-layout), the old editor address's redirect and the book page's config."""

from __future__ import annotations

from django.contrib import admin
from django.db import IntegrityError, transaction

import pytest

from assembly.models import AssemblyRun
from assembly.services import prune_snapshots
from books.models import Book
from editor.models import Manuscript, ManuscriptSnapshot


@pytest.fixture
def book(db):
    return Book.objects.create(title="كتاب المخطوطة")


def test_manuscript_defaults_and_one_per_book(book):
    manuscript = Manuscript.objects.create(book=book)
    assert manuscript.document == {} and manuscript.version == 0 and manuscript.origin == "assembly"
    assert manuscript.run is None and manuscript.updated_by is None
    assert book.manuscript == manuscript and str(manuscript) == f"manuscript of book {book.pk} v0"
    with pytest.raises(IntegrityError), transaction.atomic():
        Manuscript.objects.create(book=book)


def test_a_deleted_run_leaves_the_manuscript(book):
    run = AssemblyRun.objects.create(book=book, status="done")
    manuscript = Manuscript.objects.create(book=book, run=run, version=1)
    run.delete()
    manuscript.refresh_from_db()
    assert manuscript.run is None and manuscript.version == 1


def test_snapshots_are_newest_first_and_go_with_the_manuscript(book):
    manuscript = Manuscript.objects.create(book=book, version=3)
    old = ManuscriptSnapshot.objects.create(manuscript=manuscript, version=1, reason="reassembly", label="أ")
    new = ManuscriptSnapshot.objects.create(manuscript=manuscript, version=2, reason="manual", label="ب")
    assert list(manuscript.snapshots.all()) == [new, old]
    assert str(new) == f"snapshot v2 of manuscript {manuscript.pk}"
    book.delete()
    assert not ManuscriptSnapshot.objects.exists() and not Manuscript.objects.exists()


def test_prune_keeps_the_newest_reassembly_snapshots_and_every_manual_one(book):
    manuscript = Manuscript.objects.create(book=book)
    for version in range(5):
        ManuscriptSnapshot.objects.create(manuscript=manuscript, version=version, reason="reassembly")
    manual = ManuscriptSnapshot.objects.create(manuscript=manuscript, version=9, reason="manual")
    assert prune_snapshots(manuscript, keep=2) == 3
    kept = manuscript.snapshots.filter(reason="reassembly").values_list("version", flat=True)
    assert sorted(kept) == [3, 4] and ManuscriptSnapshot.objects.filter(pk=manual.pk).exists()
    assert prune_snapshots(manuscript, keep=2) == 0


def test_models_are_in_the_admin():
    assert admin.site.is_registered(Manuscript) and admin.site.is_registered(ManuscriptSnapshot)
    assert admin.site.is_registered(AssemblyRun)


# ====================================================================== Phase 5: chapters and the editor

import json  # noqa: E402

from django.contrib.auth.models import Group, User  # noqa: E402
from django.test import Client, override_settings  # noqa: E402
from django.urls import reverse  # noqa: E402

from assembly import services as assembly_services  # noqa: E402
from books.models import Page  # noqa: E402
from books.services import book_progress  # noqa: E402
from editor import document as doc  # noqa: E402
from editor import services  # noqa: E402
from editor.models import BOOK_FIELDS, StyleSheet  # noqa: E402
from ocr.models import Line  # noqa: E402
from processing.models import Preprocess, Region  # noqa: E402
from review import services as review_services  # noqa: E402


def text(value, *marks):
    node = {"type": "text", "text": value}
    if marks:
        node["marks"] = [{"type": mark} for mark in marks]
    return node


def para(block_id, *content, pages=(1,), **attrs):
    return {
        "type": "paragraph",
        "attrs": {"id": block_id, "sourcePages": list(pages), "sourceLineIds": [], "reviewed": True, **attrs},
        "content": [text(c) if isinstance(c, str) else c for c in content],
    }


def heading(block_id, value, level=1, pages=(1,)):
    return {
        "type": "heading",
        "attrs": {
            "level": level,
            "id": block_id,
            "sourcePages": list(pages),
            "sourceLineIds": [],
            "reviewed": True,
        },
        "content": [text(value)],
    }


def note(note_id, value, number=1, page=1):
    return {
        "type": "footnote",
        "attrs": {
            "id": note_id,
            "number": number,
            "marker": str(number),
            "sourcePage": page,
            "sourceLineIds": [],
            "orphan": False,
        },
        "content": [text(value)],
    }


def document(*blocks, title="كتاب"):
    return {
        "type": "doc",
        "attrs": {"bookId": 1},
        "content": [{"type": "title", "attrs": {"text": title, "author": ""}}, *blocks],
    }


def sample_document():
    return document(
        para("p1", "تمهيد قبل الفصول", pages=(1,)),
        heading("h10", "الفصل الأول", pages=(2,)),
        para("p11", "مَدِينَةُ برقة القديمة ", note("n1", "حاشية عن برقة", page=2), " وأهلها", pages=(2,)),
        para("p12", "وقال أحمد إن إبراهيم زار المدينة سنة ١٩٦٦", pages=(3,)),
        heading("h20", "الفصل الثاني", pages=(4,)),
        heading("h21", "عنوان فرعي", level=2, pages=(4,)),
        para("p22", "برقة مدينة", pages=(4, 5)),
    )


def role_user(name: str, role: str | None) -> User:
    user = User.objects.create_user(name, password="pass-1234")
    if role:
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


def logged(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def editor_user(db):
    return role_user("editor", "editor")


@pytest.fixture
def reader_user(db):
    return role_user("reader", "proofreader")


@pytest.fixture
def written(book):
    return Manuscript.objects.create(book=book, document=sample_document(), version=1)


def put_json(client, url, data):
    return client.put(url, json.dumps(data), content_type="application/json")


def post_json(client, url, data=None):
    return client.post(url, json.dumps(data or {}), content_type="application/json")


# ---------------------------------------------------------------- chapter split (D40)


def test_chapters_split_by_level_one_headings_with_a_front_chapter():
    chapters = doc.chapters_of(sample_document())
    assert [(c.id, c.kind, c.title, c.number) for c in chapters] == [
        ("p1", "front", "قبل الفصل الأول", 1),
        ("h10", "chapter", "الفصل الأول", 2),
        ("h20", "chapter", "الفصل الثاني", 3),
    ]
    assert [(c.start, c.end) for c in chapters] == [(1, 2), (2, 5), (5, 8)]  # the title node is in none
    assert chapters[2].heading == "الفصل الثاني"


def test_a_book_without_headings_is_cut_into_sections_of_about_thirty_pages():
    blocks = [para(f"p{n}", f"فقرة {n}", pages=(n,)) for n in range(1, 71)]
    blocks.insert(5, para("pnew", "فقرة كتبها المحرر", pages=()))  # no source page: stays in its section
    chapters = doc.chapters_of(document(*blocks))
    assert [(c.id, c.kind, c.title) for c in chapters] == [
        ("p1", "section", "القسم 1"),
        ("p31", "section", "القسم 2"),
        ("p61", "section", "القسم 3"),
    ]
    assert doc.SECTION_PAGES == 30


def test_chapter_ids_are_unique_and_the_manuscript_helper_uses_the_split(book):
    duplicated = document(heading("h1", "أ"), para("p2", "نص"), heading("h1", "ب"), para("p3", "نص"))
    assert [c.id for c in doc.chapters_of(duplicated)] == ["h1", "h1-2"]
    manuscript = Manuscript.objects.create(book=book, document=sample_document())
    assert [c.id for c in manuscript.chapters()] == ["p1", "h10", "h20"]
    assert doc.chapters_of({}) == [] and doc.chapters_of(document()) == []


def test_chapter_versions_follow_the_content_only():
    first = doc.chapters_of(sample_document())[1].nodes(sample_document())
    same = doc.chapters_of(sample_document())[1].nodes(sample_document())
    assert doc.chapter_version(first) == doc.chapter_version(same) and len(doc.chapter_version(first)) == 12
    changed = json.loads(json.dumps(first))
    changed[1]["content"][0]["text"] += "!"
    assert doc.chapter_version(changed) != doc.chapter_version(first)


def test_splice_replaces_one_chapter_and_keeps_the_others():
    source = sample_document()
    chapter = doc.find_chapter(source, "h10")
    spliced = doc.splice(source, chapter, [heading("h10", "الفصل الأول معدلًا"), para("p11", "نص جديد")])
    assert [c.id for c in doc.chapters_of(spliced)] == ["p1", "h10", "h20"]
    assert spliced["content"][0] == source["content"][0] and spliced["content"][-1] == source["content"][-1]
    assert doc.plain_text(spliced["content"][2]) == "الفصل الأول معدلًا"
    assert source["content"][2]["content"][0]["text"] == "الفصل الأول"  # the original is untouched


def test_repair_ids_is_deterministic():
    nodes = [
        heading("h10", "أ"),
        para("p11", "ب"),
        para("p11", "ج"),
        para(None, "د"),
        para("", "هـ", note("", "و")),
    ]
    once = doc.repair_ids(json.loads(json.dumps(nodes)), {"e1", "p99"})
    twice = doc.repair_ids(json.loads(json.dumps(nodes)), {"e1", "p99"})
    ids = [n["attrs"]["id"] for n in once]
    assert ids == ["h10", "p11", "p11-2", "e2", "e3"] and once == twice
    assert once[4]["content"][1]["attrs"]["id"] == "ne1"


def test_clean_nodes_refuses_unknown_nodes_marks_and_empty_text():
    assert doc.clean_nodes({"type": "doc", "content": [para("p1", "نص")]})[0]["attrs"]["id"] == "p1"
    for bad in (
        [{"type": "table"}],
        [para("p1", text("نص", "link"))],
        [para("p1", {"type": "text", "text": ""})],
        [{"type": "paragraph", "attrs": {"style": "poster"}}],
        [{"type": "heading", "attrs": {"level": 9}}],
        [{"type": "blockquote", "content": [heading("h1", "x")]}],
        "نص",
    ):
        with pytest.raises(doc.DocumentError):
            doc.clean_nodes(bad)
    ok = [
        {"type": "blockquote", "content": [para("q1", "اقتباس")]},
        {"type": "separator", "attrs": {"id": "s1"}},
        {"type": "horizontalRule"},
        para("v1", text("شطر", "bold"), {"type": "hardBreak"}, text("عجز", "italic"), style="verse"),
        para(
            "c1",
            note("n5", "حاشية", 1),
            {"type": "pageBreak", "attrs": {"page": 3, "printed": "13"}},
            style="center",
        ),
    ]
    assert len(doc.clean_nodes(ok)) == 5


# ---------------------------------------------------------------- saving (autosave, conflicts)


def test_save_chapter_splices_by_id_and_sets_the_editor_origin(written, editor_user):
    loaded = services.chapter_document(written.book, "h10")
    content = loaded["content"]
    content["content"][1]["content"][0]["text"] = "مدينة برقة الحديثة "
    result = services.save_chapter(written.book, "h10", content, loaded["version"], editor_user)
    written.refresh_from_db()
    assert written.version == 2 and written.origin == "editor" and written.updated_by == editor_user
    assert result["id"] == "h10" and result["reload"] is False and result["changed"] is True
    assert result["version"] == services.chapter_document(written.book, "h10")["version"] != loaded["version"]
    assert [c["id"] for c in services.chapter_summaries(written.book)] == ["p1", "h10", "h20"]
    again = services.save_chapter(written.book, "h10", content, result["version"], editor_user)
    assert again["changed"] is False and Manuscript.objects.get(pk=written.pk).version == 2  # a no-op save


def test_a_stale_version_conflicts_but_another_chapter_does_not(written, editor_user):
    first = services.chapter_document(written.book, "h10")
    second = services.chapter_document(written.book, "h20")
    first["content"]["content"][2]["content"][0]["text"] = "نص معدل"
    services.save_chapter(written.book, "h10", first["content"], first["version"], editor_user)
    second["content"]["content"][2]["content"][0]["text"] = "برقة مدينة قديمة"
    saved = services.save_chapter(
        written.book, "h20", second["content"], second["version"], editor_user
    )  # no conflict
    assert saved["changed"]
    with pytest.raises(services.ChapterConflict) as caught:
        services.save_chapter(written.book, "h10", first["content"], first["version"], editor_user)
    assert caught.value.version == services.chapter_document(written.book, "h10")["version"]
    assert caught.value.content["content"][2]["content"][0]["text"] == "نص معدل"


def test_a_new_level_one_heading_splits_the_chapter_and_asks_for_a_reload(written, editor_user):
    loaded = services.chapter_document(written.book, "h10")
    nodes = loaded["content"]["content"] + [heading(None, "فصل جديد"), para(None, "نصه")]
    result = services.save_chapter(written.book, "h10", nodes, loaded["version"], editor_user)
    assert result["reload"] is True and [c["id"] for c in result["chapters"]] == ["h10", "e1"]
    assert result["version"] is None  # the editor must reload before saving again
    assert [c.id for c in Manuscript.objects.get(pk=written.pk).chapters()] == ["p1", "h10", "e1", "h20"]


def test_save_refuses_bad_content_and_unknown_chapters(written, editor_user):
    loaded = services.chapter_document(written.book, "h10")
    with pytest.raises(services.EditorError):
        services.save_chapter(written.book, "h10", [], loaded["version"], editor_user)
    with pytest.raises(services.EditorError):
        services.save_chapter(written.book, "h10", [{"type": "image"}], loaded["version"], editor_user)
    with pytest.raises(services.EditorError):
        services.save_chapter(written.book, "h10", loaded["content"], "", editor_user)
    with pytest.raises(services.EditorNotFound):
        services.save_chapter(written.book, "h99", loaded["content"], loaded["version"], editor_user)
    with pytest.raises(services.EditorNotFound):
        services.chapter_document(written.book, "../x")


def test_chapter_api_get_put_conflict_and_permissions(written, editor_user, reader_user):
    url = reverse("api:chapter", args=[written.book.pk, "h10"])
    client = logged(editor_user)
    data = client.get(url).json()
    assert data["id"] == "h10" and data["prev"] == "p1" and data["next"] == "h20" and data["count"] == 3
    assert (
        data["content"]["type"] == "doc"
        and data["kind"] == "chapter"
        and data["source_pages"] == {"first": 2, "last": 3}
    )
    data["content"]["content"][2]["content"][0]["text"] = "تعديل"
    saved = put_json(client, url, {"content": data["content"], "version": data["version"]})
    assert saved.status_code == 200 and saved.json()["version"] != data["version"]
    stale = put_json(client, url, {"content": data["content"], "version": data["version"]})
    assert stale.status_code == 409 and stale.json()["version"] == saved.json()["version"]
    assert (
        stale.json()["detail"] == "تغيّر هذا الفصل في نافذة أخرى." and stale.json()["content"]["type"] == "doc"
    )
    assert logged(reader_user).get(url).status_code == 200
    assert put_json(logged(reader_user), url, {"content": data["content"], "version": "x"}).status_code == 403
    assert Client().get(url).status_code == 403
    assert client.get(reverse("api:chapter", args=[written.book.pk, "nope"])).status_code == 404
    listing = client.get(reverse("api:chapters", args=[written.book.pk])).json()
    assert set(listing[1]) == {
        "id",
        "number",
        "kind",
        "title",
        "version",
        "blocks",
        "words",
        "pages",
        "source_pages",
        "drift",
    }
    assert listing[1]["words"] == 10 and listing[1]["blocks"] == 3 and listing[1]["pages"] is None


def test_chapter_api_without_manuscript_is_404(book, editor_user):
    assert logged(editor_user).get(reverse("api:chapters", args=[book.pk])).status_code == 404


def test_chapter_summaries_cost_a_fixed_number_of_queries(book, django_assert_num_queries):
    blocks = []
    for n in range(40):
        blocks += [heading(f"h{n}", f"الفصل {n}", pages=(n + 1,)), para(f"p{n}", "نص الفصل", pages=(n + 1,))]
    Manuscript.objects.create(book=book, document=document(*blocks))
    # the manuscript (no run: no drift queries), the live layout (D47), without one the newest book render
    with django_assert_num_queries(3):
        rows = services.chapter_summaries(book)
    assert len(rows) == 40


# ---------------------------------------------------------------- snapshots


def test_snapshots_create_list_and_restore(written, editor_user, reader_user):
    client = logged(editor_user)
    url = reverse("api:snapshots", args=[written.book.pk])
    created = post_json(client, url, {"label": "قبل التحرير"})
    assert (
        created.status_code == 201
        and created.json()["label"] == "قبل التحرير"
        and created.json()["reason"] == "manual"
    )
    loaded = services.chapter_document(written.book, "h10")
    loaded["content"]["content"][0]["content"][0]["text"] = "عنوان آخر"
    services.save_chapter(written.book, "h10", loaded["content"], loaded["version"], editor_user)
    listing = client.get(url).json()
    assert [row["label"] for row in listing] == ["قبل التحرير"] and "document" not in listing[0]
    restore = post_json(client, reverse("api:snapshot_restore", args=[written.book.pk, created.json()["id"]]))
    assert restore.status_code == 200
    written.refresh_from_db()
    assert written.document == sample_document() and written.version == 3 and written.origin == "editor"
    before = ManuscriptSnapshot.objects.get(pk=restore.json()["snapshot"])
    assert before.reason == "edit" and before.document["content"][2]["content"][0]["text"] == "عنوان آخر"
    assert (
        post_json(client, reverse("api:snapshot_restore", args=[written.book.pk, 999999])).status_code == 404
    )
    assert post_json(logged(reader_user), url, {"label": "x"}).status_code == 403


def test_edit_snapshots_are_pruned_manual_ones_kept(written, editor_user):
    services.snapshot(written.book, "يدوية", "manual", editor_user)
    for _ in range(services.EDIT_SNAPSHOTS_KEPT + 3):
        services.snapshot(written.book, "آلية", "edit", editor_user)
    assert written.snapshots.filter(reason="edit").count() == services.EDIT_SNAPSHOTS_KEPT
    assert written.snapshots.filter(reason="manual").count() == 1


# ---------------------------------------------------------------- find & replace, digits


def test_find_ignores_tashkeel_and_folds_alef_by_default(written):
    found = services.find_replace(written.book, None, "مدينة", "", {})
    assert [(m["chapter"], m["block"]) for m in found["matches"]] == [
        ("h10", "p11"),
        ("h10", "p12"),
        ("h20", "p22"),
    ]
    vowelled = found["matches"][0]
    assert (vowelled["index"], vowelled["length"]) == (0, 9)  # «مَدِينَةُ» with its marks
    assert services.find_replace(written.book, None, "مدينة", "", {"match_tashkeel": True})["total"] == 2
    assert services.find_replace(written.book, None, "احمد", "", {})["total"] == 1
    assert services.find_replace(written.book, None, "احمد", "", {"fold_alef": False})["total"] == 0
    assert services.find_replace(written.book, "h20", "برقة", "", {})["total"] == 1
    whole = services.find_replace(written.book, None, "برق", "", {"whole_word": True})
    assert whole["total"] == 0 and services.find_replace(written.book, None, "برق", "", {})["total"] == 3
    in_note = services.find_replace(written.book, None, "حاشية", "", {})["matches"]
    assert in_note == [{"chapter": "h10", "block": "p11", "note": "n1", "index": 0, "length": 5}]
    with pytest.raises(services.EditorError):
        services.find_replace(written.book, None, "  َ ", "", {})


def test_replace_all_takes_a_snapshot_and_drops_the_uncertain_mark(book, editor_user):
    source = document(
        heading("h1", "الفصل"), para("p2", "كلمة ", text("مدينه", "uncertain"), " وبعدها مدينه")
    )
    manuscript = Manuscript.objects.create(book=book, document=source, version=4)
    client = logged(editor_user)
    response = post_json(
        client,
        reverse("api:find_replace", args=[book.pk]),
        {"chapter": "h1", "query": "مدينه", "replacement": "مدينة", "replace": True},
    )
    data = response.json()
    assert response.status_code == 200 and data["replaced"] == 2 and data["manuscript_version"] == 5
    manuscript.refresh_from_db()
    assert manuscript.document["content"][2]["content"] == [
        {"type": "text", "text": "كلمة مدينة وبعدها مدينة"}
    ]
    assert ManuscriptSnapshot.objects.get(pk=data["snapshot"]).document == source
    assert data["version"] == services.chapter_document(book, "h1")["version"]
    conflict = post_json(
        client,
        reverse("api:find_replace", args=[book.pk]),
        {"chapter": "h1", "query": "كلمة", "replacement": "لفظ", "replace": True, "version": "stale"},
    )
    assert conflict.status_code == 409


def test_replace_keeps_bold_and_works_across_text_nodes():
    nodes = [para("p1", text("مدي", "bold"), text("نة", "bold", "uncertain"), " ثم مدينة")]
    count = doc.replace_in_nodes(nodes, "مدينة", "بلدة", doc.FindOptions())
    assert count == 2
    assert nodes[0]["content"] == [
        {"type": "text", "text": "بلدة", "marks": [{"type": "bold"}]},
        {"type": "text", "text": " ثم بلدة"},
    ]


def test_convert_digits_in_text_and_note_markers(written, editor_user):
    client = logged(editor_user)
    url = reverse("api:convert_digits", args=[written.book.pk])
    western = post_json(client, url, {"chapter": "h10", "style": "western"}).json()
    assert western["changed"] == 4
    assert "1966" in Manuscript.objects.get(pk=written.pk).document["content"][4]["content"][0]["text"]
    indic = post_json(client, url, {"style": "arabic_indic"}).json()
    assert indic["changed"] == 5  # the year and the note marker «1»
    stored = Manuscript.objects.get(pk=written.pk).document
    assert "١٩٦٦" in stored["content"][4]["content"][0]["text"]
    assert stored["content"][3]["content"][1]["attrs"]["marker"] == "١"
    assert post_json(client, url, {"style": "roman"}).status_code == 400
    assert post_json(client, url, {"chapter": "h10", "style": "arabic_indic"}).json() == {"changed": 0}


# ---------------------------------------------------------------- review drift and chapter re-assembly (D41)

W, H = 1000, 1600


class Pages:
    """A small real book (pages, regions, lines) like the assembly tests'."""

    def __init__(self):
        self.book = Book.objects.create(title="كتاب التحرير", author="المؤلف", status=Book.Status.REVIEWING)
        self.regions = {}

    def page(self, number, status=Page.Status.REVIEWED):
        page = Page.objects.create(
            book=self.book,
            number=number,
            source_index=number - 1,
            status=status,
            text_state=Page.TextState.FINAL,
            width=W,
            height=H,
            printed_number=str(number),
        )
        Preprocess.objects.create(page=page, output_width=W, output_height=H)
        for order, kind in enumerate(("body", "footnote")):
            self.regions[(page.pk, kind)] = Region.objects.create(
                page=page, kind=kind, bbox=[0, 0, W, H], order=order
            )
        return page

    def line(self, page, words, edges=(100, 900), role="body", kind="body"):
        order = page.lines.count()
        tokens = [
            {"t": w, "alt": None, "tess": None, "conf": "high", "digit": False, "bbox": None, "res": None}
            for w in words.split()
        ]
        return Line.objects.create(
            page=page,
            order=order,
            region=self.regions[(page.pk, kind)],
            bbox=[edges[0], 100 + order * 40, edges[1], 130 + order * 40],
            text=words,
            ocr_text=words,
            tokens=tokens,
            n_low=0,
            role=role,
        )


def assembled_book(user):
    pages = Pages()
    one, two, three = pages.page(1), pages.page(2), pages.page(3)
    pages.line(one, "الفصل الأول", (300, 700), role="heading")
    pages.line(one, "نص الفصل الأول.", (100, 850))
    pages.line(two, "تكملة الفصل الأول.", (100, 850))
    pages.line(three, "الفصل الثاني", (300, 700), role="heading")
    pages.line(three, "نص الفصل الثاني.", (100, 850))
    assembly_services.start_assembly(pages.book, user)
    return pages, (one, two, three)


def test_review_drift_after_editing_and_chapter_reassembly(editor_user):
    pages, (one, two, three) = assembled_book(editor_user)
    book = pages.book
    manuscript = Manuscript.objects.get(book=book)
    first, second = [c.id for c in manuscript.chapters()]
    assert services.review_drift(book) == {"edited": False, "pages": [], "chapters": {}}
    chapter = services.chapter_document(book, second)
    chapter["content"]["content"][1]["content"][0]["text"] = "نص عدّله المحرر."
    services.save_chapter(book, second, chapter["content"], chapter["version"], editor_user)
    line = two.lines.get(order=0)
    review_services.edit_line(line, "تكملة مصححة للفصل الأول.", editor_user)
    drift = services.review_drift(book)
    assert drift == {"edited": True, "pages": [2], "chapters": {first: [2]}}
    rows = {row["id"]: row for row in services.chapter_summaries(book)}
    assert rows[first]["drift"] is True and rows[second]["drift"] is False
    progress = book_progress(book)
    assert progress["editor"] == {"edited": True, "version": 2, "drift_pages": [2]}

    # D70: over an edited text the rebuild must be confirmed; the refusal names what is at stake
    url = reverse("api:chapter_reassemble", args=[book.pk, first])
    refused = post_json(logged(editor_user), url)
    assert refused.status_code == 409 and refused.json() == {
        "detail": "حُرِّر نص هذا الفصل في «الكتاب»؛ إعادة بنائه من المراجعة تستبدله كله. أكّد الاستبدال أولًا.",
        "edited": True,
    }
    assert Manuscript.objects.get(book=book).version == 2  # nothing replaced, no run queued
    response = post_json(logged(editor_user), url, {"replace_edited": True})
    assert response.status_code == 202 and response.json()["status"] == "done"
    manuscript.refresh_from_db()
    assert manuscript.version == 3 and manuscript.origin == "editor"
    texts = [doc.plain_text(node) for node in manuscript.document["content"][1:]]
    assert "نص الفصل الأول. تكملة مصححة للفصل الأول." in texts  # the reviewed text came in
    assert "نص عدّله المحرر." in texts  # the other chapter keeps its edit
    kept = manuscript.snapshots.get(reason="edit")
    assert "قبل إعادة تجميع الفصل" in kept.label
    assert services.review_drift(book)["pages"] == []
    assert Page.objects.get(pk=two.pk).status == Page.Status.ASSEMBLED
    assert manuscript.run.settings["scope"] == "chapter" and manuscript.run.settings["chapter"] == first


def test_an_unedited_manuscript_rebuilds_a_chapter_without_the_flag(editor_user):
    """D70: re-assembly of an unedited manuscript loses nothing, so it needs no confirmation."""
    pages, (one, _two, _three) = assembled_book(editor_user)
    book = pages.book
    first = Manuscript.objects.get(book=book).chapters()[0].id
    review_services.edit_line(one.lines.get(order=1), "نص الفصل الأول مصحح.", editor_user)
    response = post_json(logged(editor_user), reverse("api:chapter_reassemble", args=[book.pk, first]))
    assert response.status_code == 202 and response.json()["status"] == "done"
    with pytest.raises(services.EditorNotFound):
        services.reassemble_chapter(book, "zz9", editor_user)


def test_approval_only_pages_are_not_review_drift(editor_user):
    """Book 26's shape (D70): after an edit on the book page, approving pages whose lines did not change is
    not announced as «تغيّر نص … بعد التحرير»; a text change still is."""
    pages = Pages()
    one, two, three = pages.page(1), pages.page(2, Page.Status.OCR_DONE), pages.page(3, Page.Status.OCR_DONE)
    pages.line(one, "الفصل الأول", (300, 700), role="heading")
    pages.line(one, "نص الفصل الأول.", (100, 850))
    pages.line(two, "تكملة الفصل الأول.", (100, 850))
    pages.line(three, "خاتمة الفصل الأول.", (100, 850))
    assembly_services.start_assembly(pages.book, editor_user)
    book = pages.book
    first = Manuscript.objects.get(book=book).chapters()[0].id
    chapter = services.chapter_document(book, first)
    chapter["content"]["content"][0]["content"][0]["text"] = "الفصل الأول وقد عدّله المحرر"
    services.save_chapter(book, first, chapter["content"], chapter["version"], editor_user)
    for page in (two, three):
        review_services.approve_page(page, editor_user, force=True)
    assert assembly_services.manuscript_state(book)["stale_pages"] == [2, 3]  # still stale (D36) …
    assert services.review_drift(book) == {"edited": True, "pages": [], "chapters": {}}  # … but not drift
    assert services.chapter_summaries(book)[0]["drift"] is False
    assert book_progress(book)["editor"]["drift_pages"] == []
    review_services.edit_line(three.lines.get(order=0), "خاتمة مصححة للفصل الأول.", editor_user)
    assert services.review_drift(book) == {"edited": True, "pages": [3], "chapters": {first: [3]}}


def test_live_drift_api_answers_in_three_queries(editor_user, reader_user, django_assert_num_queries):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from editor import api

    pages, (one, two, _three) = assembled_book(editor_user)
    book = pages.book
    first, second = [c.id for c in Manuscript.objects.get(book=book).chapters()]
    chapter = services.chapter_document(book, second)
    chapter["content"]["content"][1]["content"][0]["text"] = "نص عدّله المحرر."
    services.save_chapter(book, second, chapter["content"], chapter["version"], editor_user)
    review_services.edit_line(two.lines.get(order=0), "تكملة مصححة للفصل الأول.", editor_user)
    url = reverse("api:review_drift", args=[book.pk])
    assert url == f"/api/books/{book.pk}/drift/"
    assert services.editor_urls(book)["drift"] == url
    request = APIRequestFactory().get(url)
    force_authenticate(request, user=reader_user)
    with django_assert_num_queries(3):
        response = api.review_drift(request, book_id=book.pk)
    assert response.status_code == 200
    assert response.data == {"edited": True, "pages": [2], "chapters": [first]}
    # a proofreader may read it; an anonymous visitor may not; no manuscript → no drift; no book → 404
    assert logged(reader_user).get(url).json() == {"edited": True, "pages": [2], "chapters": [first]}
    assert Client().get(url).status_code == 403
    bare = Book.objects.create(title="كتاب بلا مخطوطة")
    assert logged(reader_user).get(reverse("api:review_drift", args=[bare.pk])).json() == {
        "edited": False,
        "pages": [],
        "chapters": [],
    }
    assert logged(reader_user).get(reverse("api:review_drift", args=[10**6])).status_code == 404


def test_reassembly_refused_while_an_assembly_runs_and_for_unknown_chapters(editor_user, reader_user):
    pages, _ = assembled_book(editor_user)
    book = pages.book
    client = logged(editor_user)
    assert post_json(client, reverse("api:chapter_reassemble", args=[book.pk, "zz9"])).status_code == 404
    first = Manuscript.objects.get(book=book).chapters()[0].id
    assert (
        post_json(logged(reader_user), reverse("api:chapter_reassemble", args=[book.pk, first])).status_code
        == 403
    )
    from assembly.models import AssemblyRun

    AssemblyRun.objects.create(book=book, status="running")
    response = post_json(client, reverse("api:chapter_reassemble", args=[book.pk, first]))
    assert response.status_code == 400 and "يجري تجميع" in response.json()["detail"]


# ---------------------------------------------------------------- stylesheet


def test_stylesheet_defaults_without_a_row(book, reader_user):
    data = logged(reader_user).get(reverse("api:stylesheet", args=[book.pk])).json()
    sheet = data["stylesheet"]
    assert data["saved"] is False and not StyleSheet.objects.exists()
    assert (sheet["trim"], sheet["width_mm"], sheet["height_mm"]) == ("17x24", 170.0, 240.0)
    assert (sheet["top_mm"], sheet["bottom_mm"], sheet["inner_mm"], sheet["outer_mm"]) == (
        20.0,
        22.0,
        22.0,
        18.0,
    )
    assert (sheet["body_font"], sheet["latin_font"], sheet["heading_font"]) == ("amiri", "times", "amiri")
    assert sheet["heading_scale"] == {"h1": 1.6, "h2": 1.25} and sheet["front_matter"] == {
        "title_page": True,
        "contents": True,
        "copyright_page": False,
        "fields": {name: "" for name in BOOK_FIELDS},
    }
    assert (sheet["widows"], sheet["orphans"], sheet["keep_headings"]) == (2, 2, True)  # D47
    assert data["field_defaults"] == {"title": book.title, "author": book.author}
    # owner, 2026-09-25: no running header unless chosen
    assert (sheet["running_header"], sheet["page_number"], sheet["chapter_opening"]) == (
        "none",
        "bottom_center",
        "any",
    )
    assert sheet["footnote_numbering"] == "page" and sheet["print_source_pages"] is False
    assert [t["key"] for t in data["trims"]] == ["17x24", "a4", "a5", "b5", "14x21", "12x17", "custom"]
    assert {f["key"] for f in data["fonts"]} == {
        "amiri",
        "simplified_arabic",
        "traditional_arabic",
        "times",
        "lotus",
    }
    assert "lotus" not in data["latin_fonts"] and data["limits"]["body_size_pt"] == [7, 24]


def test_stylesheet_put_validates_saves_and_queues_the_book_render(
    book, editor_user, reader_user, monkeypatch
):
    from publishing import engine

    calls = []
    monkeypatch.setattr(
        engine,
        "request_preview",
        lambda b, scope="book", chapter_id=None, **kw: calls.append((scope, chapter_id)),
    )
    url = reverse("api:stylesheet", args=[book.pk])
    client = logged(editor_user)
    response = put_json(
        client,
        url,
        {"trim": "a5", "width_mm": 999, "body_size_pt": "12.5", "heading_scale": {"h1": 2}, "chapter": "h1"},
    )
    assert response.status_code == 200, response.json()
    sheet = StyleSheet.objects.get(book=book)
    assert (sheet.trim, sheet.width_mm, sheet.height_mm, sheet.body_size_pt) == ("a5", 148.0, 210.0, 12.5)
    assert sheet.heading_scale == {"h1": 2.0, "h2": 1.25} and calls == [("book", None), ("chapter", "h1")]
    assert response.json()["preview"] is None  # no manuscript yet
    custom = put_json(client, url, {"trim": "custom", "width_mm": 160, "height_mm": 230})
    assert custom.status_code == 200 and StyleSheet.objects.get(book=book).width_mm == 160.0
    bad = put_json(
        client,
        url,
        {"body_size_pt": 40, "latin_font": "lotus", "page_number": "middle", "inner_mm": 70, "outer_mm": 70},
    )
    assert bad.status_code == 400
    assert set(bad.json()["errors"]) == {"body_size_pt", "latin_font", "page_number", "inner_mm"}
    assert StyleSheet.objects.get(book=book).body_size_pt == 12.5  # nothing saved
    assert put_json(logged(reader_user), url, {"trim": "a4"}).status_code == 403


# ---------------------------------------------------------------- dashboard, pages, scheduling


def test_dashboard_states_before_and_after_the_manuscript(book, django_assert_max_num_queries):
    progress = book_progress(book)
    assert progress["editor"] == {"edited": False, "version": 0, "drift_pages": []}
    assert progress["layout"] == {
        "trim": "17x24",
        "trim_label": "17×24 سم",
        "page_count": None,
        "rendering": False,
        "rendered_at": None,
    }


def test_editor_and_layout_pages_render_their_config(written, editor_user):
    client = logged(editor_user)
    book_id = written.book.pk
    page = client.get(reverse("editor:layout", args=[book_id]) + "?chapter=h20&mode=edit")
    assert page.status_code == 200
    config = page.context["config"]
    assert config["chapter"] == "h20" and config["canEdit"] is True and config["exists"] is True
    assert config["page"] == "layout" and config["mode"] == "edit"
    assert config["urls"]["chapter"] == f"/api/books/{book_id}/chapters/__cid__/"
    assert config["urls"]["restore"] == f"/api/books/{book_id}/snapshots/__sid__/restore/"
    assert config["faces"]["body"]["key"] == "amiri"
    layout = client.get(reverse("editor:layout", args=[book_id]) + "?chapter=zz")
    assert layout.status_code == 200 and layout.context["config"]["chapter"] == "p1"
    assert layout.context["config"]["mode"] == "preview"
    other = Book.objects.create(title="بلا مخطوطة")
    empty = client.get(reverse("editor:layout", args=[other.pk]))
    assert empty.status_code == 200 and empty.context["config"]["exists"] is False
    assert Client().get(reverse("editor:layout", args=[book_id])).status_code == 302  # the login


def test_a_save_asks_for_the_relayout_and_schedules_the_book_render(written, editor_user, monkeypatch):
    """D47: a save asks for the chapter's fast re-layout at once (its answer says what to poll) and the
    book's render once edits settle (debounced, D44)."""
    from publishing import preview, relayout, tasks

    scheduled = []
    monkeypatch.setattr(
        tasks.render_book_preview,
        "apply_async",
        lambda args, countdown: scheduled.append(("book", args, countdown)),
    )
    monkeypatch.setattr(
        relayout,
        "_enqueue",
        lambda row: scheduled.append(("relayout", row.chapter_id, row.version, row.kind)),
    )
    loaded = services.chapter_document(written.book, "h10")
    loaded["content"]["content"][2]["content"][0]["text"] = "تعديل"
    with override_settings(
        NASSAKH={**__import__("django.conf").conf.settings.NASSAKH, "PREVIEW_AUTORENDER": True}
    ):
        saved = services.save_chapter(written.book, "h10", loaded["content"], loaded["version"], editor_user)
    assert scheduled == [
        ("relayout", "h10", 2, "layout"),
        ("book", (written.book.pk, 2), preview.BOOK_SETTLE_S),
    ]
    assert saved["relayout"]["status"] == "queued" and saved["relayout"]["url"].endswith(
        f"/relayout/{saved['relayout']['id']}/"
    )
    unchanged = services.save_chapter(written.book, "h10", loaded["content"], saved["version"], editor_user)
    assert unchanged["changed"] is False and unchanged["relayout"] is None and len(scheduled) == 2


def test_a_full_reassembly_keeps_the_edited_text_for_good(editor_user):
    pages, _ = assembled_book(editor_user)
    book = pages.book
    first = Manuscript.objects.get(book=book).chapters()[0].id
    chapter = services.chapter_document(book, first)
    chapter["content"]["content"][1]["content"][0]["text"] = "نص محرَّر."
    services.save_chapter(book, first, chapter["content"], chapter["version"], editor_user)
    # D49: a whole-book run over the edited text is refused until the replacement is confirmed
    with pytest.raises(assembly_services.AssemblyEdited):
        assembly_services.start_assembly(book, editor_user, {"strip_tatweel": False})
    assert not ManuscriptSnapshot.objects.filter(manuscript__book=book, reason="manual").exists()
    assembly_services.start_assembly(book, editor_user, {"strip_tatweel": False}, replace_edited=True)
    kept = ManuscriptSnapshot.objects.get(manuscript__book=book, reason="manual")
    assert kept.label.startswith("النص المحرَّر قبل إعادة التجميع") and "نص محرَّر." in json.dumps(
        kept.document, ensure_ascii=False
    )


def _edited(user):
    """An assembled book whose first chapter was then saved from the book page (D41)."""
    pages, lines = assembled_book(user)
    first = Manuscript.objects.get(book=pages.book).chapters()[0].id
    chapter = services.chapter_document(pages.book, first)
    chapter["content"]["content"][1]["content"][0]["text"] = "نص محرَّر."
    services.save_chapter(pages.book, first, chapter["content"], chapter["version"], user)
    return pages, lines


def test_manuscript_view_actions_over_an_edited_text_wait_for_the_replacement(editor_user):
    """D49: once the text is edited on the book page, every whole-book run of the manuscript view is refused
    (409, `edited`) until the request confirms it; meanwhile no line role, seam or run changes."""
    from assembly.models import AssemblyRun

    pages, (_one, two, _three) = _edited(editor_user)
    book = pages.book
    assert assembly_services.manuscript_state(book)["edited"] is True
    client = logged(editor_user)
    line = two.lines.get(order=0)
    runs = AssemblyRun.objects.filter(book=book).count()
    settings_before = dict(book.assembly_settings or {})
    for name, body in (
        ("api:book_assemble", {}),
        ("api:manuscript_seam", {"page": 2, "mode": "split"}),
        ("api:manuscript_roles", {"line_ids": [line.pk], "role": "heading"}),
        ("api:manuscript_suggestion", {"block_id": f"p{line.pk}", "action": "dismiss"}),
    ):
        response = post_json(client, reverse(name, args=[book.pk]), body)
        assert response.status_code == 409 and response.json()["edited"] is True, name
    line.refresh_from_db()
    book.refresh_from_db()
    assert line.role == "body" and AssemblyRun.objects.filter(book=book).count() == runs
    assert dict(book.assembly_settings or {}) == settings_before
    confirmed = post_json(
        client,
        reverse("api:manuscript_seam", args=[book.pk]),
        {"page": 2, "mode": "split", "replace_edited": True},
    )
    assert confirmed.status_code == 202
    assert assembly_services.manuscript_state(book)["edited"] is False  # the run replaced the edited text
    assert Manuscript.objects.get(book=book).origin == "assembly"


def test_a_whole_book_run_asks_for_the_book_pages_at_once(editor_user, monkeypatch):
    """D49: the book page gets the new text laid out as soon as a run saves (not only when it opens)."""
    from publishing import engine

    asked = []
    monkeypatch.setattr(engine, "render_after_assembly", lambda book: asked.append(book.pk))
    pages, _ = assembled_book(editor_user)
    assert asked == [pages.book.pk]


def test_render_after_assembly_follows_the_autorender_setting(book, monkeypatch):
    from django.conf import settings as django_settings

    from publishing import preview

    asked = []
    monkeypatch.setattr(preview, "request_preview", lambda b, scope: asked.append((b.pk, scope)) or "row")
    assert preview.render_after_assembly(book) is None and asked == []  # tests: autorender off
    with override_settings(NASSAKH={**django_settings.NASSAKH, "PREVIEW_AUTORENDER": True}):
        assert preview.render_after_assembly(book) == "row" and asked == [(book.pk, "book")]


# ---------------------------------------------------------------- review fixes (Phase 5 backend review)


def three_chapter_pages(user):
    """Page 1: heading + paragraphs A and B; page 2: paragraph C; page 3: heading + paragraph D."""
    pages = Pages()
    one, two, three = pages.page(1), pages.page(2), pages.page(3)
    pages.line(one, "الفصل الأول", (300, 700), role="heading")
    pages.line(one, "فقرة أولى تبدأ هنا", (100, 850))
    pages.line(one, "وتنتهي الفقرة الأولى.", (100, 900))
    pages.line(one, "فقرة ثانية تبدأ هنا", (100, 850))
    pages.line(one, "وتنتهي الفقرة الثانية.", (100, 900))
    pages.line(two, "فقرة ثالثة تبدأ هنا", (100, 850))
    pages.line(two, "وتنتهي الفقرة الثالثة.", (100, 900))
    pages.line(three, "الفصل الثاني", (300, 700), role="heading")
    pages.line(three, "فقرة رابعة في الفصل الثاني.", (100, 850))
    assembly_services.start_assembly(pages.book, user)
    return pages, (one, two, three)


def texts_of(book) -> list[str]:
    content = Manuscript.objects.get(book=book).document["content"]
    return [doc.plain_text(node) for node in content[1:]]


def test_three_chapter_fixture_assembles_as_expected(editor_user):
    pages, _ = three_chapter_pages(editor_user)
    manuscript = Manuscript.objects.get(book=pages.book)
    assert [(c.kind, c.title, c.end - c.start) for c in manuscript.chapters()] == [
        ("chapter", "الفصل الأول", 4),
        ("chapter", "الفصل الثاني", 2),
    ]


def edit_first_line(page, text, user):
    review_services.edit_line(page.lines.order_by("order")[1], text, user)


def test_chapter_reassembly_keeps_a_review_heading_split_whole(editor_user):
    """A line made a heading in review splits the chapter in the fresh text: the re-assembly still takes
    the whole edited chapter's stretch (nothing after the new heading is dropped)."""
    pages, (one, two, _three) = three_chapter_pages(editor_user)
    book = pages.book
    second = Manuscript.objects.get(book=book).chapters()[1].id
    chapter = services.chapter_document(book, second)
    chapter["content"]["content"][1]["content"][0]["text"] = "فقرة رابعة عدّلها المحرر."
    services.save_chapter(book, second, chapter["content"], chapter["version"], editor_user)
    review_services.set_line_role(one.lines.get(order=3), "heading", editor_user)  # «فقرة ثانية …»
    first = Manuscript.objects.get(book=book).chapters()[0].id
    run = services.reassemble_chapter(book, first, editor_user, replace_edited=True)
    assert run.status == "done", run.error
    texts = texts_of(book)
    assert texts == [
        "الفصل الأول",
        "فقرة أولى تبدأ هنا وتنتهي الفقرة الأولى.",
        "فقرة ثانية تبدأ هنا",  # now a heading (review), its paragraph and page 2 still follow
        "وتنتهي الفقرة الثانية.",
        "فقرة ثالثة تبدأ هنا وتنتهي الفقرة الثالثة.",
        "الفصل الثاني",
        "فقرة رابعة عدّلها المحرر.",
    ]
    assert [c.title for c in Manuscript.objects.get(book=book).chapters()] == [
        "الفصل الأول",
        "فقرة ثانية تبدأ هنا",
        "الفصل الثاني",
    ]


def test_chapter_reassembly_respects_a_chapter_split_in_the_editor(editor_user):
    """The owner split chapter 1 with a new heading: re-assembling the first half never brings the second
    half in again (no duplicate), and the new chapter keeps its heading and text."""
    pages, (one, _two, _three) = three_chapter_pages(editor_user)
    book = pages.book
    first = Manuscript.objects.get(book=book).chapters()[0].id
    chapter = services.chapter_document(book, first)
    nodes = chapter["content"]["content"]
    nodes.insert(
        2, {"type": "heading", "attrs": {"level": 1}, "content": [{"type": "text", "text": "فصل جديد"}]}
    )
    saved = services.save_chapter(book, first, chapter["content"], chapter["version"], editor_user)
    assert saved["reload"] is True and len(saved["chapters"]) == 2
    edit_first_line(one, "فقرة أولى مصححة", editor_user)
    run = services.reassemble_chapter(book, first, editor_user, replace_edited=True)
    assert run.status == "done", run.error
    texts = texts_of(book)
    assert texts == [
        "الفصل الأول",
        "فقرة أولى مصححة وتنتهي الفقرة الأولى.",
        "فصل جديد",
        "فقرة ثانية تبدأ هنا وتنتهي الفقرة الثانية.",
        "فقرة ثالثة تبدأ هنا وتنتهي الفقرة الثالثة.",
        "الفصل الثاني",
        "فقرة رابعة في الفصل الثاني.",
    ]
    content = Manuscript.objects.get(book=book).document["content"]
    ids = [n["attrs"]["id"] for n in content[1:]]
    assert len(ids) == len(set(ids)) and not any(i.endswith("-2") for i in ids)
    # the new chapter (typed heading, no source line) re-assembles with its heading kept
    new_chapter = Manuscript.objects.get(book=book).chapters()[1]
    assert new_chapter.title == "فصل جديد"
    run = services.reassemble_chapter(book, new_chapter.id, editor_user, replace_edited=True)
    assert run.status == "done", run.error
    assert texts_of(book) == texts


def test_chapter_reassembly_after_a_merge_in_the_editor_loses_nothing(editor_user):
    """The owner merged chapter 2 into chapter 1 (heading demoted): re-assembling chapter 1 covers both."""
    pages, (one, _two, _three) = three_chapter_pages(editor_user)
    book = pages.book
    second = Manuscript.objects.get(book=book).chapters()[1].id
    chapter = services.chapter_document(book, second)
    chapter["content"]["content"][0]["type"] = "paragraph"
    chapter["content"]["content"][0]["attrs"].pop("level")
    saved = services.save_chapter(book, second, chapter["content"], chapter["version"], editor_user)
    assert saved["reload"] is True
    edit_first_line(one, "فقرة أولى مصححة", editor_user)
    first = Manuscript.objects.get(book=book).chapters()[0].id
    assert len(Manuscript.objects.get(book=book).chapters()) == 1
    run = services.reassemble_chapter(book, first, editor_user, replace_edited=True)
    assert run.status == "done", run.error
    assert texts_of(book) == [
        "الفصل الأول",
        "فقرة أولى مصححة وتنتهي الفقرة الأولى.",
        "فقرة ثانية تبدأ هنا وتنتهي الفقرة الثانية.",
        "فقرة ثالثة تبدأ هنا وتنتهي الفقرة الثالثة.",
        "الفصل الثاني",  # the review's heading comes back (the chapter is re-read from review)
        "فقرة رابعة في الفصل الثاني.",
    ]


def test_drift_on_a_page_two_chapters_share_flags_both(editor_user):
    pages = Pages()
    one, two = pages.page(1), pages.page(2)
    pages.line(one, "الفصل الأول", (300, 700), role="heading")
    pages.line(one, "نص الفصل الأول.", (100, 850))
    pages.line(two, "تكملة الفصل الأول.", (100, 850))
    pages.line(two, "الفصل الثاني", (300, 700), role="heading")
    pages.line(two, "نص الفصل الثاني.", (100, 850))
    assembly_services.start_assembly(pages.book, editor_user)
    book = pages.book
    first, second = [c.id for c in Manuscript.objects.get(book=book).chapters()]
    chapter = services.chapter_document(book, first)
    chapter["content"]["content"][1]["content"][0]["text"] = "نص محرَّر."
    services.save_chapter(book, first, chapter["content"], chapter["version"], editor_user)
    review_services.edit_line(two.lines.get(order=2), "نص مصحح للفصل الثاني.", editor_user)
    drift = services.review_drift(book)
    assert drift["pages"] == [2] and drift["chapters"] == {first: [2], second: [2]}
    rows = {row["id"]: row["drift"] for row in services.chapter_summaries(book)}
    assert rows == {first: True, second: True}


def test_a_full_assembly_is_not_mistaken_for_a_running_chapter_reassembly(editor_user):
    from assembly.models import AssemblyRun

    pages, _ = three_chapter_pages(editor_user)
    book = pages.book
    first = Manuscript.objects.get(book=book).chapters()[0].id
    chapter_run = AssemblyRun.objects.create(
        book=book, status="queued", settings={"scope": "chapter", "chapter": first}
    )
    full = assembly_services.start_assembly(book, editor_user)
    assert full.pk != chapter_run.pk and full.status == "done"
    # the chapter run (older than the full run that saved since) changes nothing
    version = Manuscript.objects.get(book=book).version
    services.run_chapter_reassembly(chapter_run.pk)
    chapter_run.refresh_from_db()
    assert chapter_run.status == "error" and chapter_run.error == services.SUPERSEDED
    assert Manuscript.objects.get(book=book).version == version


def test_strict_tashkeel_matching_needs_the_same_marks_in_any_order():
    fatha, shadda = "َ", "ّ"
    text_node = f"حت{shadda}ى ثم حتى ثم مد{fatha}{shadda}ة"
    nodes = [para("p1", text_node)]
    strict = doc.FindOptions(match_tashkeel=True)
    assert [m.index for m in doc.find_in_nodes(nodes, "حت", strict)] == [text_node.index("حتى")]
    assert len(doc.find_in_nodes(nodes, f"حت{shadda}ى", strict)) == 1
    # shadda + fatha typed in the other order still match
    found = doc.find_in_nodes(nodes, f"مد{shadda}{fatha}ة", strict)
    assert [(m.index, m.length) for m in found] == [(text_node.index("مد"), 5)]
    assert len(doc.find_in_nodes(nodes, "حت", doc.FindOptions())) == 2  # insensitive: both
    assert doc.replace_in_nodes(nodes, f"مد{shadda}{fatha}ة", "مدينة", strict) == 1
    assert nodes[0]["content"][0]["text"] == f"حت{shadda}ى ثم حتى ثم مدينة"


def test_replacing_a_word_leaves_the_diacritics_around_it_alone():
    source = "كَتَبَ الوَلَدُ الدَّرْسَ"
    nodes = [para("p1", source)]
    assert doc.replace_in_nodes(nodes, "الولد", "الطفل", doc.FindOptions()) == 1
    assert nodes[0]["content"][0]["text"] == "كَتَبَ الطفل الدَّرْسَ"  # the word's marks go with it only
    nodes = [para("p1", source)]
    assert doc.replace_in_nodes(nodes, "الدرس", "الكتاب", doc.FindOptions(whole_word=True)) == 1
    assert nodes[0]["content"][0]["text"] == "كَتَبَ الوَلَدُ الكتاب"


def test_stylesheet_put_refuses_non_text_choices(book, editor_user):
    url = reverse("api:stylesheet", args=[book.pk])
    response = put_json(
        logged(editor_user),
        url,
        {"body_font": ["amiri"], "latin_font": {"x": 1}, "trim": ["a4"], "page_number": ["none"]},
    )
    assert response.status_code == 400
    assert set(response.json()["errors"]) == {"body_font", "latin_font", "trim", "page_number"}


def test_dashboard_and_editor_reads_cost_a_fixed_number_of_queries(
    editor_user, django_assert_max_num_queries
):
    from publishing import engine

    pages, _ = three_chapter_pages(editor_user)
    book = pages.book
    engine.request_preview(book, "book")  # eager: the book render is done
    first = Manuscript.objects.get(book=book).chapters()[0].id
    with django_assert_max_num_queries(12):
        progress = book_progress(book)
    assert progress["layout"]["page_count"] and progress["editor"]["edited"] is False
    with django_assert_max_num_queries(6):
        services.chapter_document(book, first)
    with django_assert_max_num_queries(8):
        engine.preview_payload(book, "book", enqueue=False)


def test_every_change_needs_an_editor_and_every_read_a_login(written, editor_user, reader_user):
    book = written.book
    snap = services.snapshot(book, "نسخة", user=editor_user)
    reader = logged(reader_user)
    changes = [
        (reverse("api:find_replace", args=[book.pk]), {"query": "برقة"}),
        (reverse("api:convert_digits", args=[book.pk]), {"style": "western"}),
        (reverse("api:snapshots", args=[book.pk]), {"label": "x"}),
        (reverse("api:snapshot_restore", args=[book.pk, snap["id"]]), {}),
        (reverse("api:chapter_reassemble", args=[book.pk, "h10"]), {}),
        (reverse("api:preview", args=[book.pk]), {"scope": "book"}),
    ]
    for url, body in changes:
        assert post_json(reader, url, body).status_code == 403, url
        assert post_json(Client(), url, body).status_code == 403, url
    assert put_json(reader, reverse("api:stylesheet", args=[book.pk]), {"trim": "a4"}).status_code == 403
    reads = ["api:chapters", "api:snapshots", "api:stylesheet", "api:preview"]
    for name in reads:
        assert Client().get(reverse(name, args=[book.pk])).status_code == 403, name
    # another book's snapshot is not this book's
    other = Book.objects.create(title="كتاب آخر")
    Manuscript.objects.create(book=other, document=sample_document())
    url = reverse("api:snapshot_restore", args=[other.pk, snap["id"]])
    assert post_json(logged(editor_user), url).status_code == 404
    assert Manuscript.objects.get(book=book).version == 1


def test_requests_only_queue_renders_never_run_them(written, editor_user, monkeypatch):
    """Views enqueue (hard rule): the stylesheet PUT, the preview GET / POST and a chapter save send
    tasks to the queue; none lays pages out in the request."""
    from publishing import engine, tasks

    def no_render(*args, **kwargs):
        raise AssertionError("a request rendered pages")

    monkeypatch.setattr(engine.WeasyPrintEngine, "render", no_render)
    sent = []
    for task in (tasks.render_book_preview, tasks.render_chapter_preview):
        monkeypatch.setattr(task, "delay", lambda *a, _t=task.name: sent.append((_t, a)))
        monkeypatch.setattr(task, "apply_async", lambda a, countdown, _t=task.name: sent.append((_t, a)))
    client = logged(editor_user)
    book = written.book
    styled = put_json(client, reverse("api:stylesheet", args=[book.pk]), {"trim": "a5", "chapter": "h10"})
    assert styled.status_code == 200
    assert client.get(reverse("api:preview", args=[book.pk])).status_code == 200
    assert post_json(client, reverse("api:preview", args=[book.pk]), {"scope": "book"}).status_code == 202
    loaded = client.get(reverse("api:chapter", args=[book.pk, "h10"])).json()
    loaded["content"]["content"][1]["content"][0]["text"] = "تعديل"
    with override_settings(
        NASSAKH={**__import__("django.conf").conf.settings.NASSAKH, "PREVIEW_AUTORENDER": True}
    ):
        url = reverse("api:chapter", args=[book.pk, "h10"])
        saved = put_json(client, url, {"content": loaded["content"], "version": loaded["version"]})
        assert saved.status_code == 200
    names = [name for name, _args in sent]
    assert "publishing.tasks.render_book_preview" in names
    assert "publishing.tasks.render_chapter_preview" in names
    assert client.get(reverse("editor:layout", args=[book.pk]) + "?chapter=h10").status_code == 200


# ====================================================================== D47: the book page


def test_block_page_break_attrs_are_validated():
    good = [para("p1", "نص", breakBefore=True, keepWithNext=False), heading("h2", "عنوان")]
    good[1]["attrs"]["keepWithNext"] = True
    assert doc.clean_nodes(good)[0]["attrs"]["breakBefore"] is True
    for bad in ("yes", 1, {"a": 1}):
        with pytest.raises(doc.DocumentError):
            doc.clean_nodes([para("p1", "نص", breakBefore=bad)])


def test_plain_text_offsets_and_range_edits():
    content = [
        text("أوّل "),
        text("كلمة", "uncertain"),
        {"type": "footnote", "attrs": {"id": "n1"}, "content": [text("حاشية")]},
        {"type": "hardBreak"},
        text("𝕏 بعد", "bold"),
        {"type": "pageBreak", "attrs": {"page": 3}},
    ]
    plain = doc.inline_text(content)
    assert plain == "أوّل كلمة￼\n𝕏 بعد￼" and doc.object_kinds(content) == "أوّل كلمة￼\n𝕏 بعد\x00"
    assert doc.utf16_index(plain, len(plain)) == len(plain) + 1 and doc.code_index(
        plain, len(plain) + 1
    ) == len(plain)
    assert doc.code_index(plain, 13) == 12  # inside the surrogate pair: its character's start
    assert doc.has_mark_range(content, 5, 9, "uncertain") and not doc.has_mark_range(
        content, 4, 9, "uncertain"
    )
    unmarked = doc.unmark_range(content, 5, 9)
    assert doc.inline_text(unmarked) == plain and not doc.has_mark_range(unmarked, 5, 9, "uncertain")
    assert unmarked[0] == {"type": "text", "text": "أوّل كلمة"}  # merged with its neighbour
    replaced = doc.replace_range(content, 5, 9, "لفظة")
    assert doc.inline_text(replaced) == "أوّل لفظة￼\n𝕏 بعد￼" and replaced[1]["type"] == "footnote"
    assert not doc.has_mark_range(replaced, 5, 9, "uncertain")
    bold = doc.replace_range(content, 13, 16, "قبل")  # keeps the marks of the text it replaces
    assert bold[-2] == {"type": "text", "text": "𝕏 قبل", "marks": [{"type": "bold"}]}


def test_stylesheet_line_rules_and_book_details(book, editor_user):
    client = logged(editor_user)
    url = reverse("api:stylesheet", args=[book.pk])
    fields = {
        "subtitle": "  دراسة   تاريخية ",
        "isbn": "978-9959-26-123-4",
        "rights": "جميع الحقوق\\nمحفوظة",
        "x": "y",
    }
    response = put_json(
        client,
        url,
        {
            "widows": 3,
            "orphans": "1",
            "keep_headings": False,
            "front_matter": {"copyright_page": True, "fields": fields},
        },
    )
    assert response.status_code == 200, response.json()
    sheet = StyleSheet.objects.get(book=book)
    assert (sheet.widows, sheet.orphans, sheet.keep_headings) == (3, 1, False)
    assert sheet.front_matter == {
        "title_page": True,
        "contents": True,
        "copyright_page": True,
        "fields": {
            "subtitle": "دراسة تاريخية",
            "isbn": "978-9959-26-123-4",
            "rights": "جميع الحقوق\\nمحفوظة",
        },
    }
    values = response.json()["stylesheet"]["front_matter"]["fields"]
    assert values["subtitle"] == "دراسة تاريخية" and values["title"] == "" and set(values) == set(BOOK_FIELDS)
    refused = put_json(
        client, url, {"widows": 9, "orphans": 1.5, "front_matter": {"fields": {"isbn": "9" * 41}}}
    )
    assert refused.status_code == 400
    errors = refused.json()["errors"]
    assert set(errors) == {"widows", "orphans", "front_matter.fields.isbn"}
    cleared = put_json(client, url, {"front_matter": {"fields": {"subtitle": ""}}})
    assert (
        "subtitle" not in StyleSheet.objects.get(book=book).front_matter["fields"]
        and cleared.status_code == 200
    )


def test_the_old_editor_address_redirects_to_the_book_page(written, editor_user):
    client = logged(editor_user)
    book_id = written.book.pk
    response = client.get(reverse("editor:edit", args=[book_id]) + "?chapter=h20")
    assert response.status_code == 302
    assert response["Location"] == f"/books/{book_id}/layout/?chapter=h20&mode=edit"
    plain = client.get(reverse("editor:edit", args=[book_id]))
    assert plain.status_code == 302 and plain["Location"] == f"/books/{book_id}/layout/?mode=edit"
    assert client.get(reverse("editor:edit", args=[999999])).status_code == 404


def test_the_book_page_config_has_everything_the_merged_page_needs(written, editor_user):
    from publishing import preview

    book = written.book
    preview.render_preview(book, "book")
    page = logged(editor_user).get(reverse("editor:layout", args=[book.pk]) + "?chapter=h20&mode=edit")
    config = page.context["config"]
    assert config["mode"] == "edit" and config["relayoutMs"] == 500 and config["uncertainCount"] == 0
    assert [c["id"] for c in config["chapterSummaries"]] == ["p1", "h10", "h20"]
    assert set(config["chapterSummaries"][1]) >= {"version", "words", "pages", "drift", "source_pages"}
    assert config["drift"] == {"edited": False, "pages": [], "chapters": []}
    assert '@font-face { font-family: "nk-body"' in config["fontCss"]
    urls = config["urls"]
    assert urls["relayout"] == f"/api/books/{book.pk}/chapters/__cid__/relayout/"
    assert urls["relayoutStatus"] == f"/api/books/{book.pk}/relayout/__rid__/"
    assert urls["pageLayout"] == f"/api/books/{book.pk}/preview/layout/"
    assert urls["uncertain"] == f"/api/books/{book.pk}/uncertain/"
    assert urls["uncertainChoose"] == f"/api/books/{book.pk}/uncertain/choose/"
    for key in (
        "chapter",
        "chapters",
        "findReplace",
        "convertDigits",
        "snapshots",
        "restore",
        "sheets",
        "review",
    ):
        assert urls[key], key
    initial = config["initial"]
    assert initial["layout"]["revision"] == 1 and initial["layout"]["pages"]
    first_h20 = next(c["first"] for c in initial["preview"]["layout"]["chapters"] if c["id"] == "h20")
    assert initial["layout"]["pages"][0]["n"] == first_h20


def uncertain_book(user):
    """A reviewed page whose words are still uncertain (low confidence, unresolved), assembled."""
    pages = Pages()
    one = pages.page(1)
    pages.line(one, "الفصل الأول", (300, 700), role="heading")
    line = pages.line(one, "كان أهل برقة يزرعون القمح", (100, 850))
    line.tokens[1] = dict(line.tokens[1], conf="low", alt="اهل", tess="أهل")
    line.tokens[3] = dict(line.tokens[3], conf="low", alt="يررعون", tess=None)
    line.save()
    other = pages.line(one, "وكان أهل المدينة كثيرين", (100, 850))
    other.tokens[1] = dict(other.tokens[1], conf="low", alt="أهيل", tess="اهل")
    other.save()
    assembly_services.start_assembly(pages.book, user)
    return pages.book


def test_uncertain_words_are_listed_with_their_readings_page_and_context(editor_user, reader_user):
    from publishing import preview

    book = uncertain_book(editor_user)
    preview.render_preview(book, "book")
    data = logged(reader_user).get(reverse("api:uncertain", args=[book.pk])).json()
    assert data["count"] == 3 and [item["word"] for item in data["items"]] == ["أهل", "يزرعون", "أهل"]
    first, second, third = data["items"]
    assert first["readings"] == [
        {"engine": "primary", "label": "Qari v0.3", "text": "أهل", "current": True},
        {"engine": "secondary", "label": "Qari v0.2", "text": "اهل", "current": False},
    ]
    assert third["readings"][1]["text"] == "أهيل" and third["readings"][2] == {
        "engine": "tess",
        "label": "Tesseract",
        "text": "اهل",
        "current": False,
    }  # the second «أهل» takes the token of its own line
    assert second["readings"][1]["text"] == "يررعون" and len(second["readings"]) == 2
    manuscript = Manuscript.objects.get(book=book)
    block = next(
        b for b in doc.flat_blocks(doc.content_of(manuscript.document)) if doc.node_id(b) == first["block"]
    )
    assert doc.inline_text(block["content"])[first["start"] : first["end"]] == "أهل"
    # (40 characters at most on each side, cut at a word)
    assert first["context"] == {"before": "كان", "after": "برقة يزرعون القمح وكان أهل المدينة"}
    # the book's page 3 (after the title and contents pages), read from scan page 1
    assert first["page"] == 3 and first["source_page"] == 1 and first["note"] is None and first["chapter"]


def test_uncertain_words_are_accepted_chosen_or_typed_and_relaid_out(editor_user, reader_user, monkeypatch):
    from publishing import relayout

    book = uncertain_book(editor_user)
    asked = []
    monkeypatch.setattr(relayout, "_enqueue", lambda row: asked.append(row.chapter_id))
    client = logged(editor_user)
    listing = client.get(reverse("api:uncertain", args=[book.pk])).json()["items"]
    chapter = services.chapter_document(book, listing[0]["chapter"])

    def body(item, version, **extra):
        keys = ("chapter", "block", "note", "start", "end", "word")
        return {**{key: item[key] for key in keys}, "version": version, **extra}

    assert (
        post_json(
            logged(reader_user),
            reverse("api:uncertain_accept", args=[book.pk]),
            body(listing[0], chapter["version"]),
        ).status_code
        == 403
    )
    stale = post_json(client, reverse("api:uncertain_accept", args=[book.pk]), body(listing[0], "old"))
    assert stale.status_code == 409 and stale.json()["version"] == chapter["version"]
    with override_settings(
        NASSAKH={**__import__("django.conf").conf.settings.NASSAKH, "PREVIEW_AUTORENDER": True}
    ):
        accepted = post_json(
            client, reverse("api:uncertain_accept", args=[book.pk]), body(listing[0], chapter["version"])
        )
        assert accepted.status_code == 200, accepted.json()
        answer = accepted.json()
        assert (
            answer["word"] == "أهل" and answer["remaining"] == 2 and answer["relayout"]["status"] == "queued"
        )
        assert asked == [listing[0]["chapter"]]
        again = post_json(
            client, reverse("api:uncertain_accept", args=[book.pk]), body(listing[0], answer["version"])
        )
        assert again.status_code == 409  # not uncertain any more
        chosen = post_json(
            client,
            reverse("api:uncertain_choose", args=[book.pk]),
            body(listing[1], answer["version"], engine="secondary"),
        )
        assert (
            chosen.status_code == 200
            and chosen.json()["word"] == "يررعون"
            and chosen.json()["remaining"] == 1
        )
        missing = post_json(
            client,
            reverse("api:uncertain_choose", args=[book.pk]),
            body(listing[2], chosen.json()["version"], engine="nope"),
        )
        assert missing.status_code == 400
        empty = post_json(
            client,
            reverse("api:uncertain_type", args=[book.pk]),
            body(listing[2], chosen.json()["version"], text="  "),
        )
        assert empty.status_code == 400
        typed = post_json(
            client,
            reverse("api:uncertain_type", args=[book.pk]),
            body(listing[2], chosen.json()["version"], text=" أهالي "),
        )
    assert typed.status_code == 200 and typed.json()["word"] == "أهالي" and typed.json()["remaining"] == 0
    assert typed.json()["end"] - typed.json()["start"] == len("أهالي")
    text_now = json.dumps(Manuscript.objects.get(book=book).document, ensure_ascii=False)
    assert "يررعون" in text_now and "أهالي" in text_now and '"uncertain"' not in text_now
    assert Manuscript.objects.get(book=book).origin == "editor" and len(asked) == 3
    assert client.get(reverse("api:uncertain", args=[book.pk])).json()["count"] == 0


def test_uncertain_readings_come_in_the_manuscripts_digits_and_keep_the_diacritics(editor_user):
    """Assembly writes Western digits (D6) and drops OCR's direction marks; a reading must match and be
    offered in that form, or choosing it brings back «٢١» into a Western-digit book (and a word read as
    «٢٠» found no readings at all)."""
    pages = Pages()
    one = pages.page(1)
    pages.line(one, "الفصل الأول", (300, 700), role="heading")
    line = pages.line(one, "وَفِي سنة ٢٠ للهجرة", (100, 850))
    line.tokens[2] = dict(line.tokens[2], conf="low", alt="٢١", tess="20‏")
    line.save()
    assembly_services.start_assembly(pages.book, editor_user)
    client = logged(editor_user)
    item = client.get(reverse("api:uncertain", args=[pages.book.pk])).json()["items"][0]
    assert item["word"] == "20"
    assert [(r["engine"], r["text"], r["current"]) for r in item["readings"]] == [
        ("primary", "20", True),
        ("secondary", "21", False),
    ]  # Tesseract's «20» less its direction mark is the primary reading: not offered twice
    chapter = services.chapter_document(pages.book, item["chapter"])
    keys = ("chapter", "block", "note", "start", "end", "word")
    chosen = post_json(
        client,
        reverse("api:uncertain_choose", args=[pages.book.pk]),
        {**{key: item[key] for key in keys}, "version": chapter["version"], "engine": "secondary"},
    )
    assert chosen.status_code == 200 and chosen.json()["word"] == "21"
    text = json.dumps(Manuscript.objects.get(book=pages.book).document, ensure_ascii=False)
    assert "وَفِي سنة 21 للهجرة" in text and "٢" not in text
