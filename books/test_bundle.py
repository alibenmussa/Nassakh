"""The demo bundle (docs/challenge/DEMO_DATA.md): a book exported from one organisation and imported into another
comes out whole: every row with a fresh key, every id kept inside JSON remapped, the files in the new book's
folder, the search index built, and nothing half-imported when something fails.

The source book is built with every kind of row the app keeps for a book (pages and their images, regions,
lines, OCR runs, suggestions, review history, assembly runs, manuscript, snapshots, stylesheet with an
organisation face and a cover picture, renders and the live layout, a cover render) plus the rows and files
that must stay behind (the source PDF, an export, a comparison of review changes, a quota hold, the search
index, a clip cache, an old render)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import models
from django.db.models.deletion import Collector
from django.test import Client
from django.utils import timezone

import numpy as np
import pytest

from accounts.models import Membership, Organization, OrganizationFont, QuotaHold
from accounts.testing import member
from assembly.models import AssemblyRun
from assembly.services import page_signatures
from books import access, bundle
from books.models import Book, Page
from core.storage import save_array
from editor.models import BookImage, ChangesPlan, Manuscript, ManuscriptSnapshot, StyleSheet
from ocr.models import Line, OcrRun, TextGap
from processing.models import LayoutGuides, Region
from publishing.models import Export, LiveLayout, PreviewRender
from research import index
from research.conftest import make_page
from research.models import PageText
from review.models import LineRevision

pytestmark = pytest.mark.django_db

User = get_user_model()
PASSWORD = "demo-secret-1"
EMAIL = "Demo@Example.org"
HASH = "a" * 24
OLD_HASH = "b" * 24
SHA = "c" * 64
FONT_SHA = "d" * 64


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    """Every test works in a folder of its own: new books take the ids a rolled-back test used before."""
    settings.MEDIA_ROOT = str(tmp_path / "media")
    return Path(settings.MEDIA_ROOT)


def put(name: str, data: bytes = b"x") -> Path:
    path = Path(bundle.media_root(), name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def picture(field, name: str, value: int = 200) -> None:
    save_array(field, np.full((24, 16), value, dtype=np.uint8), name)


# ====================================================================== the source book


def build_source() -> SimpleNamespace:
    """A finished book of the source organisation, with everything that travels and everything that stays."""
    org = Organization.objects.create(name="دار المصدر")
    user = member(User.objects.create_user("source@example.org", password="pass-1234"), org)
    font = OrganizationFont.objects.create(
        organization=org,
        name="خط المؤسسة",
        family="Face",
        regular=f"orgs/{org.pk}/fonts/{FONT_SHA}.ttf",
        files={"regular": {"sha256": FONT_SHA, "size": 4, "format": "ttf"}},
        uploaded_by=user,
        licence_confirmed_by=user,
    )
    put(font.regular.name, b"font")
    book = Book.objects.create(
        title="كتاب العرض",
        author="مؤلف",
        source_page_count=3,
        status=Book.Status.REVIEWING,
        created_by=user,
        organization=org,
    )
    Book.objects.filter(pk=book.pk).update(source_pdf=f"books/{book.pk}/source.pdf")
    put(f"books/{book.pk}/source.pdf", b"%PDF")

    p1 = make_page(
        book, 1, ["باب الأعمال", "حدثنا عبد الله بن يوسف", "قال أخبرنا مالك عن نافع"], ["(١) هذا حديث"], "11"
    )
    p2 = make_page(
        book, 2, ["قال الشيخ رحمه الله في كتابه", "والعلم~ قبل القول والعمل"], printed="12", reviewed=True
    )
    p3 = make_page(book, 3, ["وكان آخر ما قاله في هذا الباب"], printed="13")
    pages = [p1, p2, p3]
    for page in pages:
        pre = page.preprocess
        picture(page.original_image, "original.png")
        picture(page.scan_thumbnail, "scan_thumb.webp")
        picture(pre.bw_image, "bw.png")
        picture(pre.display_image, "display.webp")
        picture(pre.thumbnail, "thumb.webp")
        pre.save()
        page.run_token = "tok" if page.number == 3 else ""
        page.run_claimed_at = timezone.now() if page.number == 3 else None
        page.reviewed_by = user if page.number == 2 else None
        page.reviewed_at = timezone.now() if page.number == 2 else None
        page.save()
    page1_lines = list(Line.objects.filter(page=p1).order_by("order"))
    head, body_a, body_b, note = page1_lines[0], page1_lines[1], page1_lines[2], page1_lines[3]
    Line.objects.filter(pk=head.pk).update(updated_by=user, role="heading")
    region = Region.objects.filter(page=p1).first()
    LayoutGuides.objects.create(book=book, reference_page=p1, source="manual", header_cut=0.05)
    OcrRun.objects.create(page=p1, region=region, engine_name="qari_v03", raw_output="raw", parsed_text="نص")
    OcrRun.objects.create(
        page=p2, engine_name="tesseract", params={"scope": "page", "lines": [{"bbox": [1, 2, 3, 4]}]}
    )
    gap = TextGap.objects.create(
        page=p1,
        line=body_a,
        index=0,
        after_t="حدثنا",
        text="كلمة",
        status="inserted",
        decided_by=user,
        decided_at=timezone.now(),
    )
    snap = {
        "id": body_a.pk,
        "order": 1,
        "region_id": body_a.region_id,
        "text": "قبل",
        "tokens": [],
        "is_reviewed": False,
    }
    LineRevision.objects.create(
        page=p1,
        line=body_a,
        action="edit",
        before=snap,
        after={
            **snap,
            "text": "بعد",
            "gaps": {str(gap.pk): {"index": 0, "after_t": "x", "status": "open"}},
            "gap": gap.pk,
        },
        user=user,
    )
    LineRevision.objects.create(
        page=p2,
        action="approve",
        before={
            "status": "ocr_done",
            "reviewed_by": None,
            "reviewed_at": None,
            "lines": {str(body_b.pk): False},
        },
        after={
            "status": "reviewed",
            "reviewed_by": user.pk,
            "reviewed_at": timezone.now().isoformat(),
            "lines": {str(body_b.pk): True, str(note.pk): True},
        },
        user=user,
    )
    LineRevision.objects.create(
        page=p1,
        line=body_a,
        action="gap",
        before={"gap": gap.pk, "status": "open"},
        after={"gap": gap.pk, "status": "dismissed"},
        user=user,
    )

    included = {
        str(page.pk): {
            "number": page.number,
            "reviewed": page.number == 2,
            "sig": sig,
            "at": "2026-10-01T00:00:00+00:00",
        }
        for page, sig in zip(
            pages, (page_signatures([p.pk for p in pages])[p.pk] for p in pages), strict=True
        )
    }
    run = AssemblyRun.objects.create(
        book=book,
        status="done",
        stage="save",
        settings={"scope": "changes", "plan": 99, "chapter": f"h{head.pk}", "footnote_numbering": "page"},
        warnings=[
            {
                "blockId": f"p{body_a.pk}",
                "code": "note_orphan",
                "lineIds": [body_a.pk, body_b.pk],
                "message": "m",
                "page": 1,
                "severity": "warning",
            }
        ],
        stats={"words": 12, "applied": {"pages": [1, 2]}},
        included=included,
        created_by=user,
        finished_at=timezone.now(),
    )
    AssemblyRun.objects.create(book=book, status="running", stage="collect", task_id="celery-1")
    document = {
        "type": "doc",
        "attrs": {
            "bookId": book.pk,
            "runId": run.pk,
            "digitStyle": "western",
            "seams": [{"page": 2, "from_page": 1, "mode": "join", "decision": "auto", "reason": "geometry"}],
        },
        "content": [
            {"type": "title", "attrs": {"text": "كتاب العرض", "author": "مؤلف"}},
            {
                "type": "heading",
                "attrs": {
                    "level": 1,
                    "id": f"h{head.pk}",
                    "sourcePages": [1],
                    "sourceLineIds": [head.pk],
                    "reviewed": False,
                },
                "content": [{"type": "text", "text": "باب الأعمال"}],
            },
            {
                "type": "paragraph",
                "attrs": {
                    "id": f"p{body_a.pk}",
                    "sourcePages": [1, 2],
                    "sourceLineIds": [body_a.pk, body_b.pk],
                    "reviewed": False,
                },
                "content": [
                    {"type": "text", "text": "حدثنا عبد الله"},
                    {
                        "type": "footnote",
                        "attrs": {
                            "id": f"n{note.pk}",
                            "number": 1,
                            "marker": "١",
                            "sourcePage": 1,
                            "sourceLineIds": [note.pk],
                            "orphan": False,
                        },
                        "content": [{"type": "text", "text": "هذا حديث"}],
                    },
                    {"type": "pageBreak", "attrs": {"page": 2, "printed": "12"}},
                ],
            },
        ],
    }
    manuscript = Manuscript.objects.create(
        book=book, document=document, base=document, version=5, origin="editor", run=run, updated_by=user
    )
    ManuscriptSnapshot.objects.create(
        manuscript=manuscript, document=document, base=document, version=4, label="يدوية", created_by=user
    )
    image = BookImage.objects.create(
        book=book,
        file=f"books/{book.pk}/images/{SHA}.jpg",
        sha256=SHA,
        width=10,
        height=12,
        format="jpeg",
        purpose="cover",
        uploaded_by=user,
    )
    put(image.file.name, b"jpg")
    put(image.thumb_name, b"webp")
    StyleSheet.objects.create(
        book=book,
        body_font=font.key,
        heading_font=font.key,
        front_matter={
            "title_page": True,
            "contents": True,
            "copyright_page": False,
            "fields": {},
            "cover": {"mode": "image", "image": image.pk, "fit": "fill"},
        },
    )
    folder = f"books/{book.pk}/preview/{HASH}"
    render = PreviewRender.objects.create(
        book=book,
        scope="book",
        kind="pages",
        content_hash=HASH,
        status="done",
        page_count=2,
        folder=folder,
        chapters=[{"id": f"h{head.pk}", "title": "باب", "first": 1, "last": 2, "version": "abc"}],
        checks=[{"block": f"p{body_a.pk}", "code": "almost_empty_page", "message": "m", "page": 1}],
    )
    layout = {
        "render": render.pk,
        "kind": "pages",
        "scope": "book",
        "blocks": {f"h{head.pk}": [[f"h{head.pk}", "d1"], [f"p{body_a.pk}", "d2"]]},
        "chapters": [{"id": f"h{head.pk}", "title": "باب", "first": 1, "last": 2, "version": "abc"}],
        "numbers": {f"fn-n{note.pk}": "1"},
        "pages": [
            {
                "n": n,
                "chapter": f"h{head.pk}",
                "lines": [{"block": f"p{body_a.pk}", "runs": [{"note": f"n{note.pk}"}]}],
                "src": {"render": render.pk, "index": n},
            }
            for n in (1, 2)
        ],
    }
    put(f"{folder}/layout.json", json.dumps(layout).encode())
    for name in ("page-0001.webp", "page-0001-2x.webp", "page-0002.webp", "page-0002-2x.webp", "book.pdf"):
        put(f"{folder}/{name}", name.encode())
    LiveLayout.objects.create(
        book=book,
        revision=3,
        path=f"{folder}/layout.json",
        base=render,
        page_count=2,
        chapters=layout["chapters"],
        manuscript_version=5,
        setup_hash="setup",
    )
    old = PreviewRender.objects.create(
        book=book,
        scope="book",
        kind="pages",
        content_hash=OLD_HASH,
        status="done",
        page_count=1,
        folder=f"books/{book.pk}/preview/{OLD_HASH}",
    )
    PreviewRender.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=1))
    put(f"{old.folder}/page-0001.webp", b"old")
    put(f"books/{book.pk}/layout/live-00002.json", b"{}")
    for name in ("cover.pdf", "cover.webp", "cover-2x.webp"):
        put(f"books/{book.pk}/cover/{'e' * 24}/{name}", name.encode())
    put(f"books/{book.pk}/clips/abc.webp", b"clip")
    export = Export.objects.create(
        book=book, format="docx", status="done", file=f"books/{book.pk}/exports/1.docx", created_by=user
    )
    put(export.file.name, b"PK")
    ChangesPlan.objects.create(book=book, status="done", created_by=user)
    QuotaHold.objects.create(organization=org, page=p3, run_key="k")
    index.reindex_book(book.pk)
    return SimpleNamespace(
        org=org,
        user=user,
        font=font,
        book=book,
        pages=pages,
        lines=page1_lines,
        gap=gap,
        image=image,
        run=run,
        render=render,
        manuscript=manuscript,
        folder=folder,
    )


@pytest.fixture
def source() -> SimpleNamespace:
    return build_source()


@pytest.fixture
def bundle_dir(source, tmp_path) -> Path:
    out = tmp_path / "bundle"
    bundle.export_bundle([source.book.pk], out)
    return out


def do_import(bundle_dir: Path, **kwargs):
    kwargs.setdefault("email", EMAIL)
    kwargs.setdefault("password", PASSWORD)
    return bundle.import_bundle(bundle_dir, **kwargs)


def imported(report) -> Book:
    return Book.objects.get(pk=report.books[0].new_id)


# ====================================================================== the registries


def test_every_json_field_that_travels_is_classified():
    """A new JSONField is either given a rewriter (it holds ids) or declared to hold none."""
    from django.apps import apps

    seen = set()
    for label in (bundle.ROOT, bundle.FONT, *bundle.TRAVEL):
        for f in apps.get_model(label)._meta.concrete_fields:
            if isinstance(f, models.JSONField):
                key = (label, f.name)
                seen.add(key)
                assert (key in bundle.JSON_REWRITERS) != (key in bundle.JSON_WITHOUT_IDS), key
    assert set(bundle.JSON_REWRITERS) | bundle.JSON_WITHOUT_IDS == seen  # nothing listed that does not exist


def test_the_models_that_cascade_from_a_book_are_all_classified():
    """Walked by the model definitions alone: a new table under `Book` stops here until it is decided."""
    from django.apps import apps
    from django.db.models.deletion import get_candidate_relations_to_delete

    reached, todo = set(), [apps.get_model(bundle.ROOT)]
    while todo:
        model = todo.pop()
        for rel in get_candidate_relations_to_delete(model._meta):
            if rel.on_delete is models.CASCADE and rel.related_model not in reached:
                reached.add(rel.related_model)
                todo.append(rel.related_model)
    labels = {m._meta.label_lower for m in reached}
    assert labels <= set(bundle.TRAVEL) | set(bundle.LEFT_OUT)
    assert set(bundle.TRAVEL) <= labels and set(bundle.LEFT_OUT) <= labels  # and none is listed in vain


def test_a_row_that_points_at_a_book_without_cascading_is_accounted_for():
    from django.apps import apps
    from django.db.models.deletion import get_candidate_relations_to_delete

    travelling = {apps.get_model(label) for label in (bundle.ROOT, *bundle.TRAVEL, *bundle.LEFT_OUT)}
    seen = set()
    for model in travelling:
        for rel in get_candidate_relations_to_delete(model._meta):
            if rel.on_delete is models.CASCADE or rel.related_model in travelling:
                continue
            seen.add(f"{rel.related_model._meta.label_lower}.{rel.field.name}")
    assert seen == set(bundle.NOT_EXPORTED_REFERENCES)


def test_import_order_puts_every_row_after_the_rows_it_names():
    order = bundle.travelling_labels()
    from django.apps import apps

    for label in order:
        for f in apps.get_model(label)._meta.concrete_fields:
            if f.is_relation:
                target = f.remote_field.model._meta.label_lower
                if target in order and target != label:
                    assert order.index(target) < order.index(label), (label, f.name)
        for earlier in bundle.JSON_DEPENDENCIES.get(label, ()):
            assert order.index(earlier) < order.index(label), (label, earlier)
    bundle.validate_relations()


def test_the_graph_walk_finds_what_djangos_deletion_collector_would_delete(source):
    collector = Collector(using="default")
    collector.collect([source.book])
    expected: dict[str, set] = {}
    for model, instances in collector.data.items():
        expected.setdefault(model._meta.label_lower, set()).update(obj.pk for obj in instances)
    for queryset in collector.fast_deletes:
        expected.setdefault(queryset.model._meta.label_lower, set()).update(
            queryset.values_list("pk", flat=True)
        )
    expected = {label: pks for label, pks in expected.items() if pks}
    assert bundle.collect_graph(source.book.pk) == expected


# ====================================================================== the export


def test_the_export_writes_the_rows_and_only_the_files_the_app_needs(source, bundle_dir):
    header = json.loads((bundle_dir / "data.json").read_text(encoding="utf-8"))["header"]
    assert header["format"] == bundle.FORMAT_NAME and header["version"] == bundle.FORMAT_VERSION
    (entry,) = header["books"]
    assert (entry["id"], entry["title"], entry["source_page_count"]) == (source.book.pk, "كتاب العرض", 3)
    counts = entry["counts"]
    assert (
        counts["books.page"] == 3
        and counts["ocr.line"] == Line.objects.filter(page__book=source.book).count()
    )
    assert counts["publishing.previewrender"] == 1  # the live layout's render; the old one stays behind
    assert counts["assembly.assemblyrun"] == 2 and counts["editor.bookimage"] == 1
    for label in bundle.LEFT_OUT:
        assert label not in counts
    assert entry["left_out_rows"] == {
        "accounts.quotahold": 1,
        "editor.changesplan": 1,
        "publishing.export": 1,
        "research.pagetext": PageText.objects.filter(book=source.book).count(),
    }
    assert entry["dropped_renders"] == 1 and header["counts"]["accounts.organizationfont"] == 1
    files = set(json.loads((bundle_dir / "manifest.json").read_text())["files"])
    b = source.book.pk
    assert f"books/{b}/pages/0001/gray.png" in files and f"books/{b}/pages/0003/scan_thumb.webp" in files
    assert f"books/{b}/preview/{HASH}/layout.json" in files and f"books/{b}/preview/{HASH}/book.pdf" in files
    assert f"books/{b}/cover/{'e' * 24}/cover.webp" in files and f"books/{b}/images/{SHA}.jpg" in files
    assert (
        f"books/{b}/images/{SHA}-thumb.webp" in files
        and f"orgs/{source.org.pk}/fonts/{FONT_SHA}.ttf" in files
    )
    for left_behind in ("source.pdf", f"preview/{OLD_HASH}", "layout/live-00002.json", "clips", "exports"):
        assert not [name for name in files if f"books/{b}/{left_behind}" in name], left_behind
    for name in files:
        assert (bundle_dir / "media" / name).is_file()


def test_the_export_does_not_touch_the_database_or_the_media_folder(source, tmp_path):
    before = {label: model.objects.count() for label, model in _models().items()}
    media_before = sorted(
        p.relative_to(bundle.media_root()).as_posix() for p in bundle._walk_files(bundle.media_root())
    )
    bundle.export_bundle([source.book.pk], tmp_path / "out")
    assert {label: model.objects.count() for label, model in _models().items()} == before
    media_after = sorted(
        p.relative_to(bundle.media_root()).as_posix() for p in bundle._walk_files(bundle.media_root())
    )
    assert media_after == media_before


def _models() -> dict:
    from django.apps import apps

    return {m._meta.label_lower: m for m in apps.get_models() if m._meta.app_label != "sessions"}


def test_the_source_pdf_travels_only_when_asked(source, tmp_path):
    bundle.export_bundle([source.book.pk], tmp_path / "with", include_pdf=True)
    files = json.loads((tmp_path / "with" / "manifest.json").read_text())["files"]
    assert f"books/{source.book.pk}/source.pdf" in files


def test_dates_keep_their_microseconds(source, bundle_dir):
    """Django's JSON encoder cuts them to milliseconds, and a page's review signature is built from them."""
    stamps = {Line.objects.get(pk=line.pk).updated_at for line in source.lines}
    text = (bundle_dir / "data.json").read_text(encoding="utf-8")
    assert all(stamp.isoformat() in text for stamp in stamps)
    assert any(stamp.microsecond % 1000 for stamp in stamps)  # a stamp Django's encoder would have cut


def test_an_export_dry_run_writes_nothing(source, tmp_path):
    out = tmp_path / "dry"
    plan = bundle.export_bundle([source.book.pk], out, dry_run=True)
    assert plan.counts()["books.page"] == 3 and not out.exists()


def test_a_missing_book_is_named(source, tmp_path):
    with pytest.raises(bundle.BundleError, match=r"No book with id 999"):
        bundle.export_bundle([source.book.pk, 999], tmp_path / "x")


def test_an_export_refuses_a_directory_inside_the_repository(source, settings):
    inside = Path(settings.BASE_DIR) / "demo-bundle"
    with pytest.raises(bundle.BundleError, match="inside the repository"):
        bundle.export_bundle([source.book.pk], inside)
    assert not inside.exists()
    with pytest.raises(CommandError, match="never be committed"):
        call_command("export_demo_bundle", "--books", str(source.book.pk), "--out", str(inside / "deeper"))
    with pytest.raises(CommandError, match="inside the repository"):
        call_command(
            "export_demo_bundle", "--books", str(source.book.pk), "--out", str(Path(settings.BASE_DIR))
        )


def test_an_export_refuses_a_directory_inside_any_git_work_tree(source, tmp_path):
    import shutil
    import subprocess

    if not shutil.which("git"):
        pytest.skip("git is not installed")
    repo = tmp_path / "other-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pytest.raises(bundle.BundleError, match="git work tree"):
        bundle.export_bundle([source.book.pk], repo / "bundle")
    assert not (repo / "bundle").exists()


def test_a_failed_export_does_not_remove_the_earlier_bundle(source, tmp_path):
    out = tmp_path / "keep"
    bundle.export_bundle([source.book.pk], out)
    with pytest.raises(bundle.BundleError, match="No book with id 999"):
        bundle.export_bundle([999], out, force=True)
    assert (out / "data.json").is_file() and (out / "media").is_dir()


def test_an_export_into_a_used_directory_needs_force(source, tmp_path):
    out = tmp_path / "again"
    bundle.export_bundle([source.book.pk], out)
    with pytest.raises(bundle.BundleError, match="not empty"):
        bundle.export_bundle([source.book.pk], out)
    bundle.export_bundle([source.book.pk], out, force=True)
    assert (out / "data.json").is_file()


def test_the_export_command_prints_a_summary(source, tmp_path, capsys):
    call_command("export_demo_bundle", "--books", str(source.book.pk), "--out", str(tmp_path / "cmd"))
    out = capsys.readouterr().out
    assert "books.page" in out and "left out: source PDF" in out and "MB of media written" in out


# ====================================================================== the import


def test_a_round_trip_gives_the_same_rows_under_fresh_keys(source, bundle_dir):
    report = do_import(bundle_dir)
    book = imported(report)
    assert book.pk != source.book.pk and report.books[0].action == "created"
    assert report.unmapped == {} and report.warnings == []
    original = bundle.collect_graph(source.book.pk)
    copy = bundle.collect_graph(book.pk)
    for label in (bundle.ROOT, *bundle.TRAVEL):
        expected = (
            1 if label == "publishing.previewrender" else len(original[label])
        )  # only the live render came
        assert len(copy[label]) == expected, label
        assert copy[label].isdisjoint(original[label]), label  # fresh keys
    assert report.books[0].counts == {
        label: len(copy[label]) for label in sorted(copy) if label == bundle.ROOT or label in bundle.TRAVEL
    }
    for label in bundle.LEFT_OUT:
        if label != "research.pagetext":
            assert label not in copy  # nothing of what stayed behind came along
    assert copy.get("research.pagetext")  # the index is built by the import


def test_no_foreign_key_of_the_copy_points_at_a_missing_row_or_at_the_source(source, bundle_dir):
    report = do_import(bundle_dir)
    book = imported(report)
    copy = bundle.collect_graph(book.pk)
    original = bundle.collect_graph(source.book.pk)
    from django.apps import apps

    demo = Organization.objects.get(pk=report.organization_id)
    demo_user = User.objects.get(pk=report.user_id)
    checked = 0
    for label, pks in copy.items():
        if label == "research.pagetext":
            continue
        model = apps.get_model(label)
        for row in model.objects.filter(pk__in=pks):
            for f in model._meta.concrete_fields:
                if not f.is_relation:
                    continue
                value = getattr(row, f.attname)
                if value is None:
                    continue
                target = f.remote_field.model
                assert target._default_manager.filter(pk=value).exists(), (label, f.name, value)
                if target is User:
                    assert value == demo_user.pk, (label, f.name)
                elif target is Organization:
                    assert value == demo.pk, (label, f.name)
                else:
                    target_label = target._meta.label_lower
                    assert value in copy.get(target_label, set()), (label, f.name)
                    assert value not in original.get(target_label, set()), (label, f.name)
                checked += 1
    assert checked > 40
    assert book.organization_id == demo.pk and book.created_by_id == demo_user.pk


def test_every_id_kept_inside_json_points_at_the_new_rows(source, bundle_dir):
    report = do_import(bundle_dir)
    book = imported(report)
    new_lines = {line.text: line for line in Line.objects.filter(page__book=book)}
    old_lines = {line.pk: line for line in Line.objects.filter(page__book=source.book)}
    new_pages = {page.number: page for page in Page.objects.filter(book=book)}
    head, body_a, body_b, note = (new_lines[line.text] for line in source.lines[:4])
    run = AssemblyRun.objects.filter(book=book, status="done").get()

    document = Manuscript.objects.get(book=book).document
    assert document["attrs"]["bookId"] == book.pk and document["attrs"]["runId"] == run.pk
    heading, paragraph = document["content"][1], document["content"][2]
    assert heading["attrs"]["sourceLineIds"] == [head.pk] and heading["attrs"]["id"] == f"h{head.pk}"
    assert paragraph["attrs"]["sourceLineIds"] == [body_a.pk, body_b.pk]
    assert paragraph["attrs"]["id"] == f"p{body_a.pk}" and paragraph["attrs"]["sourcePages"] == [
        1,
        2,
    ]  # numbers stay
    footnote = paragraph["content"][1]["attrs"]
    assert (
        footnote["id"] == f"n{note.pk}"
        and footnote["sourceLineIds"] == [note.pk]
        and footnote["sourcePage"] == 1
    )
    assert paragraph["content"][2]["attrs"] == {"page": 2, "printed": "12"}
    assert document["attrs"]["seams"][0]["page"] == 2
    manuscript = Manuscript.objects.get(book=book)
    assert manuscript.base == document and manuscript.run_id == run.pk
    snapshot = ManuscriptSnapshot.objects.get(manuscript=manuscript)
    assert snapshot.document == document and snapshot.base == document
    for old in old_lines.values():  # every line the document names is a line of the copy, with the same text
        assert Line.objects.get(pk=new_lines[old.text].pk).text == old.text

    assert run.settings == {"scope": "changes", "chapter": f"h{head.pk}", "footnote_numbering": "page"}
    assert run.warnings[0]["blockId"] == f"p{body_a.pk}" and run.warnings[0]["lineIds"] == [
        body_a.pk,
        body_b.pk,
    ]
    assert set(run.included) == {str(page.pk) for page in new_pages.values()}
    assert run.included[str(new_pages[2].pk)]["reviewed"] is True and run.stats == {
        "words": 12,
        "applied": {"pages": [1, 2]},
    }

    revisions = {revision.action: revision for revision in LineRevision.objects.filter(page__book=book)}
    edit, approve = revisions["edit"], revisions["approve"]
    new_gap = TextGap.objects.get(page__book=book)
    assert edit.before["id"] == body_a.pk and edit.before["region_id"] == body_a.region_id
    assert edit.after["gap"] == new_gap.pk and list(edit.after["gaps"]) == [str(new_gap.pk)]
    assert approve.after["lines"] == {str(body_b.pk): True, str(note.pk): True}
    assert approve.after["reviewed_by"] == report.user_id and approve.before["reviewed_by"] is None
    assert revisions["gap"].before["gap"] == new_gap.pk and revisions["gap"].line_id == body_a.pk

    assert book.assembly_settings == {}
    sheet = StyleSheet.objects.get(book=book)
    image = BookImage.objects.get(book=book)
    assert sheet.front_matter["cover"]["image"] == image.pk
    font = OrganizationFont.objects.get(organization_id=report.organization_id)
    assert sheet.body_font == sheet.heading_font == f"org-{font.pk}" and sheet.latin_font == "times"


def test_an_id_that_names_a_row_the_source_no_longer_has_becomes_unresolvable(source, tmp_path):
    """A later OCR pass re-makes a page's lines, and a manuscript or an old assembly run still names the old
    ones. Kept as they were, such ids could name another account's lines in the target database."""
    stale = 987_654
    document = Manuscript.objects.get(book=source.book).document
    document["content"][2]["attrs"]["sourceLineIds"].append(stale)
    Manuscript.objects.filter(book=source.book).update(document=document)
    AssemblyRun.objects.filter(book=source.book, status="done").update(
        warnings=[{"blockId": "p987654", "lineIds": [stale, source.lines[0].pk], "code": "x", "page": 1}]
    )
    out = tmp_path / "stale"
    bundle.export_bundle([source.book.pk], out)
    report = do_import(out)
    book = imported(report)
    head = Line.objects.get(page__book=book, text=source.lines[0].text)
    ids = Manuscript.objects.get(book=book).document["content"][2]["attrs"]["sourceLineIds"]
    assert ids[-1] == -stale and all(i > 0 for i in ids[:-1])
    warning = AssemblyRun.objects.get(book=book, status="done").warnings[0]
    assert warning["lineIds"] == [-stale, head.pk] and warning["blockId"] == "p987654"
    assert "unresolvable" in report.warnings[0] and not Line.objects.filter(pk=-stale).exists()


def test_the_books_assembly_settings_are_remapped_too(source, tmp_path):
    lines = source.lines
    Book.objects.filter(pk=source.book.pk).update(
        assembly_settings={
            "line_styles": {str(lines[1].pk): "quote", "999999": "verse"},
            "dismissed_suggestions": [f"p{lines[0].pk}", "h424242"],
            "seams": {"2": "join"},
            "include_unreviewed": True,
        }
    )
    out = tmp_path / "settings"
    bundle.export_bundle([source.book.pk], out)
    book = imported(do_import(out))
    new = {line.text: line.pk for line in Line.objects.filter(page__book=book)}
    assert book.assembly_settings == {
        "line_styles": {
            str(new[lines[1].text]): "quote"
        },  # a style for a line that does not exist is dropped
        "dismissed_suggestions": [f"p{new[lines[0].text]}", "h424242"],
        "seams": {"2": "join"},
        "include_unreviewed": True,
    }


def test_the_layout_files_and_the_render_rows_agree_on_the_new_ids(source, bundle_dir, media):
    report = do_import(bundle_dir)
    book = imported(report)
    render = PreviewRender.objects.get(book=book)
    live = LiveLayout.objects.get(book=book)
    new_lines = {line.text: line.pk for line in Line.objects.filter(page__book=book)}
    head, body_a, note = (new_lines[source.lines[i].text] for i in (0, 1, 3))
    assert render.folder == f"books/{book.pk}/preview/{HASH}" and render.content_hash == HASH
    assert live.path == f"{render.folder}/layout.json" and live.base_id == render.pk
    assert live.chapters[0]["id"] == f"h{head}" and render.chapters[0]["id"] == f"h{head}"
    assert render.checks[0]["block"] == f"p{body_a}"
    data = json.loads((media / live.path).read_text(encoding="utf-8"))
    assert data["render"] == render.pk and [page["src"] for page in data["pages"]] == [
        {"render": render.pk, "index": 1},
        {"render": render.pk, "index": 2},
    ]
    assert list(data["blocks"]) == [f"h{head}"] and data["blocks"][f"h{head}"][1][0] == f"p{body_a}"
    assert (
        data["numbers"] == {f"fn-n{note}": "1"}
        and data["pages"][0]["lines"][0]["runs"][0]["note"] == f"n{note}"
    )
    assert data["pages"][0]["chapter"] == f"h{head}" and data["chapters"][0]["id"] == f"h{head}"


def test_the_files_are_copied_to_the_new_books_folder_and_the_rest_stays_behind(source, bundle_dir, media):
    report = do_import(bundle_dir)
    book = imported(report)
    folder = media / "books" / str(book.pk)
    for page in Page.objects.filter(book=book):
        assert page.original_image.name == f"books/{book.pk}/pages/{page.number:04d}/original.png"
        for name in ("original.png", "scan_thumb.webp", "gray.png", "bw.png", "display.webp", "thumb.webp"):
            assert (folder / "pages" / f"{page.number:04d}" / name).is_file(), (page.number, name)
        pre = page.preprocess
        assert pre.gray_image.name == f"books/{book.pk}/pages/{page.number:04d}/gray.png"
    image = BookImage.objects.get(book=book)
    assert (
        image.file.name == f"books/{book.pk}/images/{SHA}.jpg"
        and (media / image.file.name).read_bytes() == b"jpg"
    )
    assert (media / image.thumb_name).is_file()
    assert (folder / "preview" / HASH / "page-0001-2x.webp").is_file() and (
        folder / "preview" / HASH / "book.pdf"
    ).is_file()
    assert (folder / "cover" / ("e" * 24) / "cover.webp").is_file()
    for left in ("source.pdf", f"preview/{OLD_HASH}", "layout", "clips", "exports"):
        assert not (folder / left).exists(), left
    assert book.source_pdf.name == ""  # the PDF did not travel: no dangling name
    font = OrganizationFont.objects.get(organization_id=report.organization_id)
    assert font.regular.name == f"orgs/{report.organization_id}/fonts/{FONT_SHA}.ttf"
    assert (media / font.regular.name).read_bytes() == b"font"
    # the source is untouched
    assert (media / "books" / str(source.book.pk) / "source.pdf").is_file()
    assert (media / "books" / str(source.book.pk) / "pages" / "0001" / "gray.png").is_file()
    assert report.books[0].media_files == sum(1 for p in bundle._walk_files(folder))
    page = Page.objects.get(book=book, number=1)
    original = Page.objects.get(book=source.book, number=1)
    assert (media / page.original_image.name).read_bytes() == (
        media / original.original_image.name
    ).read_bytes()


def test_users_are_remapped_to_the_demo_user(source, bundle_dir):
    report = do_import(bundle_dir)
    book = imported(report)
    demo = User.objects.get(pk=report.user_id)
    assert Page.objects.get(book=book, number=2).reviewed_by_id == demo.pk
    assert Page.objects.get(book=book, number=1).reviewed_by_id is None
    assert Line.objects.filter(page__book=book, updated_by__isnull=False).count() == 1
    assert Line.objects.filter(page__book=book, updated_by__isnull=False).get().updated_by_id == demo.pk
    assert TextGap.objects.get(page__book=book).decided_by_id == demo.pk
    assert AssemblyRun.objects.filter(book=book, created_by=demo).count() == 1
    assert Manuscript.objects.get(book=book).updated_by_id == demo.pk
    assert OrganizationFont.objects.get(organization_id=report.organization_id).uploaded_by_id == demo.pk
    assert not User.objects.filter(pk=demo.pk, email=source.user.email).exists()


def test_review_signatures_survive_the_trip(source, bundle_dir):
    """`Line.updated_at` keeps its microseconds, so the assembly runs' page signatures still match: the
    manuscript is not called out of date."""
    book = imported(do_import(bundle_dir))
    old = page_signatures([page.pk for page in source.pages])
    new = page_signatures([page.pk for page in Page.objects.filter(book=book)])
    assert [old[page.pk] for page in source.pages] == [
        new[page.pk] for page in Page.objects.filter(book=book).order_by("number")
    ]
    run = AssemblyRun.objects.get(book=book, status="done")
    for page in Page.objects.filter(book=book):
        assert run.included[str(page.pk)]["sig"] == new[page.pk]
    new_lines = Line.objects.filter(page__book=book).order_by("page__number", "order")
    old_lines = Line.objects.filter(page__book=source.book).order_by("page__number", "order")
    assert [line.updated_at for line in new_lines] == [line.updated_at for line in old_lines]


def test_runtime_state_does_not_travel(source, bundle_dir):
    book = imported(do_import(bundle_dir))
    assert not Page.objects.filter(book=book).exclude(run_token="").exists()
    assert not Page.objects.filter(book=book, run_claimed_at__isnull=False).exists()
    runs = {run.status: run for run in AssemblyRun.objects.filter(book=book)}
    assert (
        set(runs) == {"done", "error"}
        and runs["error"].error == bundle.INTERRUPTED
        and runs["error"].task_id == ""
    )
    assert not QuotaHold.objects.filter(page__book=book).exists()
    assert (
        not ChangesPlan.objects.filter(book=book).exists() and not Export.objects.filter(book=book).exists()
    )


def test_the_account_and_its_user(source, bundle_dir):
    report = do_import(bundle_dir)
    organization = Organization.objects.get(pk=report.organization_id)
    assert (organization.name, organization.kind, organization.unlimited) == (
        bundle.DEMO_ACCOUNT_NAME,
        "organization",
        True,
    )
    user = User.objects.get(pk=report.user_id)
    assert (
        user.username == user.email == "demo@example.org" and user.is_active and user.check_password(PASSWORD)
    )
    assert not user.is_superuser and not user.is_staff
    membership = Membership.objects.get(user=user)
    assert (membership.organization_id, membership.role) == (organization.pk, "admin")
    assert user.groups.filter(name="editor").exists() and not user.groups.filter(name="admin").exists()
    assert report.organization_created and report.user_created and report.password_changed


def test_an_existing_user_keeps_the_password_unless_one_is_given(source, bundle_dir, tmp_path):
    do_import(bundle_dir)
    user = User.objects.get(username="demo@example.org")
    again = do_import(bundle_dir, password=None)
    assert not again.user_created and not again.password_changed
    assert User.objects.get(pk=user.pk).check_password(PASSWORD)
    changed = do_import(bundle_dir, password="another-1")
    assert changed.password_changed and User.objects.get(pk=user.pk).check_password("another-1")
    assert Organization.objects.filter(name=bundle.DEMO_ACCOUNT_NAME).count() == 1


def test_a_new_user_needs_a_password_and_nothing_is_left_behind(source, bundle_dir, media):
    before = Book.objects.count()
    with pytest.raises(bundle.BundleError, match="--password is required"):
        do_import(bundle_dir, password=None)
    assert Book.objects.count() == before and not User.objects.filter(username="demo@example.org").exists()
    assert not Organization.objects.filter(name=bundle.DEMO_ACCOUNT_NAME).exists()


def test_a_user_of_another_organisation_is_refused(source, bundle_dir):
    stranger = member(User.objects.create_user("demo@example.org", password="x"), source.org)
    with pytest.raises(bundle.BundleError, match="already belongs to the organisation"):
        do_import(bundle_dir)
    assert Membership.objects.get(user=stranger).organization_id == source.org.pk


def test_the_search_index_is_built_and_search_answers_for_the_demo_user_only(source, bundle_dir):
    from research import services

    report = do_import(bundle_dir)
    book = imported(report)
    assert report.books[0].indexed_pages == 3 and PageText.objects.filter(book=book).count() >= 3
    demo = User.objects.get(pk=report.user_id)
    outsider = member(
        User.objects.create_user("out@example.org", password="x"), Organization.objects.create(name="غريبة")
    )
    found = services.search(demo, "حدثنا عبد الله")
    assert {hit.book.id for hit in found.hits} == {
        book.pk
    }  # the demo account's copy only (the source is org A's)
    assert services.search(outsider, "حدثنا عبد الله").total == 0


def test_access_scoping_keeps_the_copy_out_of_other_accounts(source, bundle_dir, media):
    report = do_import(bundle_dir)
    book = imported(report)
    demo = User.objects.get(pk=report.user_id)
    outsider = member(User.objects.create_user("out@example.org", password="x"), source.org)
    assert access.may_access(demo, book) and not access.may_access(outsider, book)
    assert book.pk in access.books_for(demo).values_list("pk", flat=True)
    assert book.pk not in access.books_for(outsider).values_list("pk", flat=True)
    assert source.book.pk not in access.books_for(demo).values_list("pk", flat=True)
    path = f"/media/books/{book.pk}/pages/0001/gray.png"
    own, other = Client(), Client()
    own.force_login(demo)
    other.force_login(outsider)
    response = own.get(path)
    assert response.status_code == 200
    response.close()
    assert other.get(path).status_code == 404
    assert other.get(f"/books/{book.pk}/").status_code == 404
    assert Client().get(path).status_code in (302, 401, 403, 404)


def test_the_books_pages_answer_for_the_demo_user(source, bundle_dir):
    report = do_import(bundle_dir)
    book = imported(report)
    client = Client()
    client.force_login(User.objects.get(pk=report.user_id))
    assert client.get("/books/").status_code == 200 and "كتاب العرض" in client.get("/books/").content.decode()
    assert client.get(f"/books/{book.pk}/review/1/").status_code == 200
    assert client.get(f"/books/{book.pk}/").status_code == 200


# ---------------------------------------------------------------- skipping, replacing, dry runs, failures


def test_importing_again_skips_the_book_that_is_already_there(source, bundle_dir, media):
    first = do_import(bundle_dir)
    books_before = list(Book.objects.order_by("pk").values_list("pk", flat=True))
    lines_before = Line.objects.count()
    second = do_import(bundle_dir, password=None)
    assert [book.action for book in second.books] == ["skipped"]
    assert second.books[0].replaced_ids == [first.books[0].new_id]
    assert list(Book.objects.order_by("pk").values_list("pk", flat=True)) == books_before
    assert Line.objects.count() == lines_before
    assert OrganizationFont.objects.filter(organization_id=first.organization_id).count() == 1


def test_replace_deletes_the_old_copy_with_its_files_and_imports_a_new_one(source, bundle_dir, media):
    first = do_import(bundle_dir)
    old = first.books[0].new_id
    second = do_import(bundle_dir, password=None, replace=True)
    new = second.books[0].new_id
    assert second.books[0].action == "replaced" and second.books[0].replaced_ids == [old] and new != old
    assert not Book.objects.filter(pk=old).exists() and not Page.objects.filter(book_id=old).exists()
    assert not (media / "books" / str(old)).exists()
    assert (media / "books" / str(new) / "pages" / "0001" / "gray.png").is_file()
    demo = Book.objects.filter(organization_id=first.organization_id)
    assert list(demo.values_list("pk", flat=True)) == [new]
    assert (
        OrganizationFont.objects.filter(organization_id=first.organization_id).count() == 1
    )  # the face is reused
    assert (
        Line.objects.filter(page__book_id=new).count() == Line.objects.filter(page__book=source.book).count()
    )
    assert source.book.pk != new and Book.objects.filter(pk=source.book.pk).exists()


def test_an_import_dry_run_changes_nothing(source, bundle_dir, media):
    before = {label: model.objects.count() for label, model in _models().items()}
    files_before = sorted(p.relative_to(media).as_posix() for p in bundle._walk_files(media))
    report = do_import(bundle_dir, dry_run=True)
    assert (
        report.dry_run and report.books[0].action == "created" and report.books[0].counts["books.page"] == 3
    )
    assert {label: model.objects.count() for label, model in _models().items()} == before
    assert sorted(p.relative_to(media).as_posix() for p in bundle._walk_files(media)) == files_before


def test_a_failure_leaves_no_row_and_no_file(source, bundle_dir, media, monkeypatch):
    before = {label: model.objects.count() for label, model in _models().items()}
    files_before = sorted(p.relative_to(media).as_posix() for p in bundle._walk_files(media))
    real = bundle.shutil.copy2
    calls = []

    def failing(src, dst, *args, **kwargs):
        calls.append(dst)
        if len(calls) == 12:
            raise OSError("disk full")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(bundle.shutil, "copy2", failing)
    with pytest.raises(OSError, match="disk full"):
        do_import(bundle_dir)
    assert len(calls) == 12
    assert {label: model.objects.count() for label, model in _models().items()} == before
    assert sorted(p.relative_to(media).as_posix() for p in bundle._walk_files(media)) == files_before
    # the folder of the rolled-back book is gone, so a new run works
    monkeypatch.setattr(bundle.shutil, "copy2", real)
    assert do_import(bundle_dir).books[0].action == "created"


def test_a_failure_in_the_command_is_reported_as_rolled_back(source, bundle_dir, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(bundle.shutil, "copy2", broken)
    with pytest.raises(CommandError, match="rolled back"):
        call_command(
            "import_demo_bundle", "--bundle", str(bundle_dir), "--email", EMAIL, "--password", PASSWORD
        )
    assert not Organization.objects.filter(name=bundle.DEMO_ACCOUNT_NAME).exists()


def test_a_bundle_with_a_missing_file_is_refused_before_anything_is_written(source, bundle_dir):
    victim = next((bundle_dir / "media").rglob("gray.png"))
    victim.unlink()
    before = Book.objects.count()
    with pytest.raises(bundle.BundleError, match="missing or incomplete"):
        do_import(bundle_dir)
    assert (
        Book.objects.count() == before
        and not Organization.objects.filter(name=bundle.DEMO_ACCOUNT_NAME).exists()
    )


def test_a_truncated_file_is_refused(source, bundle_dir):
    victim = next((bundle_dir / "media").rglob("original.png"))
    victim.write_bytes(victim.read_bytes()[:-3])
    with pytest.raises(bundle.BundleError, match="missing or incomplete"):
        do_import(bundle_dir)


def test_a_directory_that_is_not_a_bundle_is_refused(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(bundle.BundleError, match="not a complete bundle"):
        do_import(tmp_path / "empty")
    with pytest.raises(CommandError, match="not a complete bundle"):
        call_command(
            "import_demo_bundle",
            "--bundle",
            str(tmp_path / "empty"),
            "--email",
            EMAIL,
            "--password",
            PASSWORD,
        )


def test_a_bundle_of_another_format_version_is_refused(source, bundle_dir):
    data = (bundle_dir / "data.json").read_text(encoding="utf-8")
    (bundle_dir / "data.json").write_text(data.replace('"version":1', '"version":2', 1), encoding="utf-8")
    with pytest.raises(bundle.BundleError, match="format 2"):
        do_import(bundle_dir)


def test_a_database_behind_the_bundle_is_told_to_migrate(source, bundle_dir):
    data = (bundle_dir / "data.json").read_text(encoding="utf-8")
    header = json.loads(data.split("\n", 1)[0][len('{"header": ') : -1])
    newest = header["migrations"]["books"]
    forged = data.replace(f'"books":"{newest}"', '"books":"9999_future"', 1)
    (bundle_dir / "data.json").write_text(forged, encoding="utf-8")
    with pytest.raises(bundle.BundleError, match=r"books\.9999_future not applied.*migrate"):
        do_import(bundle_dir)


def test_the_password_is_never_written_to_the_output_or_the_bundle(source, bundle_dir, capsys, tmp_path):
    call_command(
        "import_demo_bundle",
        "--bundle",
        str(bundle_dir),
        "--email",
        EMAIL,
        "--password",
        PASSWORD,
        "--dry-run",
    )
    text = capsys.readouterr().out + capsys.readouterr().err
    assert PASSWORD not in text and "password set" in text
    assert PASSWORD not in (bundle_dir / "data.json").read_text(encoding="utf-8")


def test_the_import_command_reports_what_it_made(source, bundle_dir, capsys):
    call_command("import_demo_bundle", "--bundle", str(bundle_dir), "--email", EMAIL, "--password", PASSWORD)
    out = capsys.readouterr().out
    assert "imported as book" in out and "books.page" in out and "1 book(s) imported, 0 skipped" in out


def test_two_books_travel_together_and_keep_their_own_files(source, tmp_path, media):
    other = Book.objects.create(title="كتاب ثان", source_page_count=1, organization=source.org)
    page = make_page(other, 1, ["سطر وحيد في الكتاب الثاني"], printed="1")
    picture(page.original_image, "original.png")
    page.save()
    out = tmp_path / "two"
    bundle.export_bundle([source.book.pk, other.pk], out)
    report = do_import(out)
    assert [book.action for book in report.books] == ["created", "created"]
    first, second = (Book.objects.get(pk=book.new_id) for book in report.books)
    assert Page.objects.filter(book=first).count() == 3 and Page.objects.filter(book=second).count() == 1
    assert (media / "books" / str(second.pk) / "pages" / "0001" / "original.png").is_file()
    assert Line.objects.get(page__book=second).text == "سطر وحيد في الكتاب الثاني"
    assert {book.organization_id for book in (first, second)} == {report.organization_id}
    assert source.book.created_at <= timezone.now() + timedelta(seconds=1)
    assert get_user_model().objects.filter(pk=report.user_id).exists()
