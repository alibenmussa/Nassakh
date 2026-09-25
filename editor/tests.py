"""Tests of the editor app's models (Phase 4: the manuscript written by assembly, and its snapshots)."""

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
from editor.models import StyleSheet  # noqa: E402
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
    with django_assert_num_queries(2):  # the manuscript (no run: no drift queries), the newest book render
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

    response = post_json(logged(editor_user), reverse("api:chapter_reassemble", args=[book.pk, first]))
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
    }
    assert (sheet["running_header"], sheet["page_number"], sheet["chapter_opening"]) == (
        "chapter",
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
    page = client.get(reverse("editor:edit", args=[written.book.pk]) + "?chapter=h20")
    assert page.status_code == 200
    config = page.context["config"]
    assert config["chapter"] == "h20" and config["canEdit"] is True and config["exists"] is True
    assert config["urls"]["chapter"] == f"/api/books/{written.book.pk}/chapters/__cid__/"
    assert config["urls"]["restore"] == f"/api/books/{written.book.pk}/snapshots/__sid__/restore/"
    assert config["faces"]["body"]["key"] == "amiri" and 'id="editor-config"' in page.content.decode()
    assert (
        client.get(reverse("editor:edit", args=[written.book.pk]) + "?chapter=zz").context["config"][
            "chapter"
        ]
        == "p1"
    )
    layout = client.get(reverse("editor:layout", args=[written.book.pk]))
    assert layout.status_code == 200 and layout.context["config"]["page"] == "layout"
    other = Book.objects.create(title="بلا مخطوطة")
    empty = client.get(reverse("editor:edit", args=[other.pk]))
    assert empty.status_code == 200 and "لا توجد مخطوطة بعد" in empty.content.decode()
    assert Client().get(reverse("editor:edit", args=[written.book.pk])).status_code == 302


def test_a_save_schedules_the_debounced_renders(written, editor_user, monkeypatch):
    from publishing import preview, tasks

    scheduled = []
    monkeypatch.setattr(
        tasks.render_chapter_preview,
        "apply_async",
        lambda args, countdown: scheduled.append(("chapter", args, countdown)),
    )
    monkeypatch.setattr(
        tasks.render_book_preview,
        "apply_async",
        lambda args, countdown: scheduled.append(("book", args, countdown)),
    )
    loaded = services.chapter_document(written.book, "h10")
    loaded["content"]["content"][2]["content"][0]["text"] = "تعديل"
    with override_settings(
        NASSAKH={**__import__("django.conf").conf.settings.NASSAKH, "PREVIEW_AUTORENDER": True}
    ):
        services.save_chapter(written.book, "h10", loaded["content"], loaded["version"], editor_user)
    assert scheduled == [
        ("chapter", (written.book.pk, "h10", 2), preview.CHAPTER_SETTLE_S),
        ("book", (written.book.pk, 2), preview.BOOK_SETTLE_S),
    ]


def test_a_full_reassembly_keeps_the_edited_text_for_good(editor_user):
    pages, _ = assembled_book(editor_user)
    book = pages.book
    first = Manuscript.objects.get(book=book).chapters()[0].id
    chapter = services.chapter_document(book, first)
    chapter["content"]["content"][1]["content"][0]["text"] = "نص محرَّر."
    services.save_chapter(book, first, chapter["content"], chapter["version"], editor_user)
    assembly_services.start_assembly(book, editor_user, {"strip_tatweel": False})
    kept = ManuscriptSnapshot.objects.get(manuscript__book=book, reason="manual")
    assert kept.label.startswith("النص المحرَّر قبل إعادة التجميع") and "نص محرَّر." in json.dumps(
        kept.document, ensure_ascii=False
    )


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
    run = services.reassemble_chapter(book, first, editor_user)
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
    run = services.reassemble_chapter(book, first, editor_user)
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
    run = services.reassemble_chapter(book, new_chapter.id, editor_user)
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
    run = services.reassemble_chapter(book, first, editor_user)
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
