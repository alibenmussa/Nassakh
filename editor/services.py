"""Editor services (PHASE5_SPEC §3): chapters, saves, snapshots, find & replace, digits, review drift,
per-chapter re-assembly and the book's stylesheet.

The manuscript stays one JSON document (`editor.Manuscript.document`); the editor works on one chapter at
a time (D40, `editor.document`). Every save is version-checked per chapter (a short hash of its nodes):
a stale tab gets a conflict with the current chapter, a save of another chapter never conflicts. The
first save sets `origin = editor`: from then on the manuscript is the source of truth (D41) and review
corrections no longer flow in — pages changed in review since show as drift, and each chapter can be
re-assembled from the reviewed pages (the old chapter kept as a snapshot, reason `edit`).

Views and API functions stay thin; the renders after a save are scheduled through `publishing.engine`
(the chapter's fast re-layout at once, the book debounced, D44/D47), never run here: an edit's answer
carries `relayout` (`{id, status, url}` to poll, or null) so the book page can swap its live pages.
"""

from __future__ import annotations

import copy
import logging
import re
import time

from django.contrib.auth.models import AnonymousUser
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from books.models import Book, Page

from . import document as doc
from .models import BOOK_FIELDS, CUSTOM_TRIM, TRIM_PRESETS, Manuscript, ManuscriptSnapshot, StyleSheet

log = logging.getLogger(__name__)

EDIT_SNAPSHOTS_KEPT = 20  # automatic `edit` snapshots kept per manuscript (manual ones are never pruned)
MAX_QUERY = 500
MAX_MATCHES = 5000
MAX_LABEL = 200
SNAPSHOTS_LISTED = 100

NO_MANUSCRIPT = "لم يُجمَّع هذا الكتاب بعد؛ حوّله إلى كتاب أولًا."
NO_CHAPTER = "الفصل غير موجود؛ ربما تغيّر تقسيم الفصول. أعد تحميل قائمة الفصول."
CONFLICT = "تغيّر هذا الفصل في نافذة أخرى."
REASSEMBLY_ERROR = "تعذّرت إعادة تجميع الفصل؛ بقي الفصل كما هو. أعد المحاولة، وإن تكرّر الخطأ فراجع سجل الخادم."
CHAPTER_GONE = "لم يُعثر على نص هذا الفصل في الصفحات؛ بقي الفصل كما هو."
SUPERSEDED = "أُعيد تجميع الكتاب كله في أثناء ذلك؛ بقي الفصل كما جاء في التجميع الجديد."
CHAPTER_EDITED = "حُرِّر نص هذا الفصل في «الكتاب»؛ إعادة بنائه من المراجعة تستبدله كله. أكّد الاستبدال أولًا."
_RE_CHAPTER_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class EditorError(ValueError):
    """A refused editor request; the message is Arabic (API 400)."""


class EditorNotFound(EditorError):
    """No manuscript, no such chapter or snapshot (API 404)."""


class ChapterConflict(EditorError):
    """The chapter changed since the editor loaded it (API 409 with the current chapter)."""

    def __init__(self, chapter_id: str, version: str, nodes: list):
        super().__init__(CONFLICT)
        self.chapter_id = chapter_id
        self.version = version
        self.content = {"type": "doc", "content": nodes}


class EditorEdited(EditorError):
    """A chapter rebuild from review asked for over an edited text without `replace_edited` (D70, API 409)."""

    def __init__(self):
        super().__init__(CHAPTER_EDITED)


class StyleSheetError(EditorError):
    """Invalid stylesheet values: `errors` maps each field to its Arabic message (API 400)."""

    def __init__(self, errors: dict[str, str]):
        super().__init__(next(iter(errors.values())) if errors else "قيمة غير صالحة.")
        self.errors = errors


def _user_or_none(user):
    if user is None or isinstance(user, AnonymousUser) or not getattr(user, "is_authenticated", False):
        return None
    return user


def _parse_bool(value, default: bool) -> bool:
    """A posted boolean (`true`, `1`, `"on"` …); anything else is `default`."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("0", "false", "no", "off"):
        return False
    return default


def _check_chapter_id(chapter_id) -> str:
    if not isinstance(chapter_id, str) or not _RE_CHAPTER_ID.fullmatch(chapter_id):
        raise EditorNotFound(NO_CHAPTER)
    return chapter_id


# ====================================================================== reading

chapters_of = doc.chapters_of


def manuscript_of(book: Book, *, lock: bool = False, with_run: bool = False) -> Manuscript:
    """The book's manuscript (`EditorNotFound` before the first assembly); `lock` inside a transaction."""
    rows = Manuscript.objects.filter(book_id=book.pk)
    if with_run:
        rows = rows.select_related("run")
    if lock:
        rows = rows.select_for_update()
    manuscript = rows.first()
    if manuscript is None:
        raise EditorNotFound(NO_MANUSCRIPT)
    return manuscript


def _page_ranges(book: Book) -> dict[str, dict]:
    """Chapter id → `{first, last}` printed pages in the newest finished book render."""
    from publishing import engine

    return engine.chapter_pages(book)


def _source_range(nodes: list) -> dict | None:
    pages = doc.chapter_pages(nodes)
    return {"first": pages[0], "last": pages[-1]} if pages else None


def chapter_summaries(book: Book) -> list[dict]:
    """The chapters panel (`api:chapters`): `[{id, number, kind, title, version, blocks, words, pages:
    {first, last} | null, source_pages: {first, last} | null, drift}]` — `pages` from the newest book
    render, `drift` when a source page of the chapter changed in review since the manuscript was built.
    A fixed number of queries whatever the chapter count."""
    manuscript = manuscript_of(book, with_run=True)
    document = manuscript.document or {}
    chapters = doc.chapters_of(document)
    ranges = _page_ranges(book)
    drift = review_drift(book, manuscript, chapters)["chapters"]
    out = []
    for chapter in chapters:
        nodes = chapter.nodes(document)
        out.append(
            {
                "id": chapter.id,
                "number": chapter.number,
                "kind": chapter.kind,
                "title": chapter.title,
                "version": doc.chapter_version(nodes),
                "blocks": len(nodes),
                "words": doc.word_count(nodes),
                "pages": ranges.get(chapter.id),
                "source_pages": _source_range(nodes),
                "drift": chapter.id in drift,
            }
        )
    return out


def _open_warnings(warnings: list, nodes: list) -> list[dict]:
    """The assembly warnings still open in a chapter: those about its blocks (an `uncertain_words` warning
    only while the block keeps an uncertain mark, a `note_orphan` only while an orphan note is there) and
    the page warnings without a block on its source pages."""
    blocks = {doc.node_id(node): node for node in doc.flat_blocks(nodes) if doc.node_id(node)}
    for node in nodes:
        if isinstance(node, dict) and doc.node_id(node):
            blocks.setdefault(doc.node_id(node), node)
    pages = set(doc.chapter_pages(nodes))
    out: list[dict] = []
    for warning in warnings or []:
        if not isinstance(warning, dict):
            continue
        block_id = warning.get("blockId")
        if block_id:
            block = blocks.get(block_id)
            if block is None:
                continue
            code = warning.get("code")
            if code == "uncertain_words" and not doc.has_uncertain(block):
                continue
            if code == "note_orphan" and not any(
                note is not None and doc.attrs_of(note).get("orphan")
                for _b, note, _c in doc.iter_containers([block])
            ):
                continue
            out.append(warning)
        elif warning.get("page") in pages:
            out.append(warning)
    return out


def chapter_document(book: Book, chapter_id: str) -> dict:
    """One chapter for the editor (`api:chapter`): `{id, version, content, warnings, number, kind, title,
    prev, next, count, source_pages, pages, drift, manuscript_version, origin}` — `content` is a `doc`
    node holding the chapter's blocks."""
    _check_chapter_id(chapter_id)
    manuscript = manuscript_of(book, with_run=True)
    document = manuscript.document or {}
    chapters = doc.chapters_of(document)
    index = next((i for i, chapter in enumerate(chapters) if chapter.id == chapter_id), None)
    if index is None:
        raise EditorNotFound(NO_CHAPTER)
    chapter = chapters[index]
    nodes = chapter.nodes(document)
    run = manuscript.run
    drift = review_drift(book, manuscript, chapters)["chapters"]
    return {
        "id": chapter.id,
        "version": doc.chapter_version(nodes),
        "content": {"type": "doc", "content": nodes},
        "warnings": _open_warnings(run.warnings if run is not None else [], nodes),
        "number": chapter.number,
        "kind": chapter.kind,
        "title": chapter.title,
        "prev": chapters[index - 1].id if index > 0 else None,
        "next": chapters[index + 1].id if index + 1 < len(chapters) else None,
        "count": len(chapters),
        "source_pages": _source_range(nodes),
        "pages": _page_ranges(book).get(chapter.id),
        "drift": chapter.id in drift,
        "manuscript_version": manuscript.version,
        "origin": manuscript.origin,
    }


# ====================================================================== saving


def _schedule(book: Book, chapter_id: str | None, version: int) -> dict | None:
    """Schedule the renders after an edit; the chapter's re-layout to poll (None when none was asked)."""
    from publishing import engine

    row = engine.schedule_after_edit(book, chapter_id, version)
    return engine.relayout_payload(row) if row is not None else None


def _other_ids(document: dict, chapter: doc.ChapterSlice) -> set[str]:
    content = doc.content_of(document)
    return doc.all_ids(content[: chapter.start]) | doc.all_ids(content[chapter.end :])


def save_chapter(book: Book, chapter_id: str, content, version, user) -> dict:
    """Save one chapter (D40): splice it into the document by id, after the per-chapter version check.

    Returns `{version, id, chapters: [{id, version, title}], reload, manuscript_version, changed,
    relayout}`: the chapter's new version (`relayout`: the re-layout to poll, D47), or — when the saved
    blocks no longer form exactly this chapter (a new level-1 heading split it, a deleted heading merged
    it with the one before) — `reload: true`, `version: null` and the chapters they now form: the editor
    reloads the chapter list and the chapter before saving again. Raises `ChapterConflict` when
    `version` is not the chapter's current version, `EditorError` for invalid content.
    """
    _check_chapter_id(chapter_id)
    try:
        nodes = doc.clean_nodes(content)
    except doc.DocumentError as exc:
        raise EditorError(str(exc)) from exc
    if not nodes:
        raise EditorError("لا يُحفظ فصل فارغ؛ اترك فقرة واحدة على الأقل.")
    if not isinstance(version, str) or not version:
        raise EditorError("إصدار الفصل مطلوب للحفظ.")
    with transaction.atomic():
        manuscript = manuscript_of(book, lock=True)
        document = manuscript.document or {}
        chapter = doc.find_chapter(document, chapter_id)
        if chapter is None:
            raise EditorNotFound(NO_CHAPTER)
        current = chapter.nodes(document)
        current_version = doc.chapter_version(current)
        if version != current_version:
            raise ChapterConflict(chapter_id, current_version, current)
        doc.repair_ids(nodes, _other_ids(document, chapter))
        changed = nodes != current
        new_document = doc.splice(document, chapter, nodes) if changed else document
        end = chapter.start + len(nodes)
        formed = [c for c in doc.chapters_of(new_document) if c.start < end and c.end > chapter.start]
        if changed:
            manuscript.document = new_document
            manuscript.version += 1
            manuscript.origin = Manuscript.Origin.EDITOR
            manuscript.updated_by = _user_or_none(user)
            manuscript.save(update_fields=["document", "version", "origin", "updated_by", "updated_at"])
    relayout = _schedule(book, formed[0].id if formed else None, manuscript.version) if changed else None
    summary = [
        {"id": c.id, "version": doc.chapter_version(c.nodes(new_document)), "title": c.title} for c in formed
    ]
    reload = [c["id"] for c in summary] != [chapter_id]
    return {
        # No version to save with after a split or a merge: the editor holds more (or less) than one
        # chapter now and must reload it first (a save with it would duplicate or drop text).
        "version": None if reload else summary[0]["version"],
        "id": summary[0]["id"] if summary else chapter_id,
        "chapters": summary,
        "reload": reload,
        "manuscript_version": manuscript.version,
        "changed": changed,
        "relayout": relayout,
    }


def _write(manuscript: Manuscript, document: dict, user) -> None:
    manuscript.document = document
    manuscript.version += 1
    manuscript.origin = Manuscript.Origin.EDITOR
    manuscript.updated_by = _user_or_none(user)
    manuscript.save(update_fields=["document", "version", "origin", "updated_by", "updated_at"])


# ====================================================================== snapshots


def _snapshot_dict(snapshot: ManuscriptSnapshot, current_version: int | None = None) -> dict:
    user = snapshot.created_by
    return {
        "id": snapshot.pk,
        "version": snapshot.version,
        "label": snapshot.label,
        "reason": snapshot.reason,
        "reason_label": snapshot.get_reason_display(),
        "created_by": (user.get_full_name() or user.get_username()) if user is not None else "",
        "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
        "current": current_version is not None and snapshot.version == current_version,
    }


def prune_edit_snapshots(manuscript: Manuscript, keep: int = EDIT_SNAPSHOTS_KEPT) -> int:
    """Delete automatic `edit` snapshots beyond the newest `keep`; returns how many went."""
    old = list(
        manuscript.snapshots.filter(reason=ManuscriptSnapshot.Reason.EDIT)
        .order_by("-created_at", "-id")
        .values_list("id", flat=True)[keep:]
    )
    if old:
        ManuscriptSnapshot.objects.filter(pk__in=old).delete()
    return len(old)


def _take(manuscript: Manuscript, label: str, reason: str, user) -> ManuscriptSnapshot:
    snapshot = ManuscriptSnapshot.objects.create(
        manuscript=manuscript,
        document=copy.deepcopy(manuscript.document or {}),
        version=manuscript.version,
        label=label[:MAX_LABEL],
        reason=reason,
        created_by=_user_or_none(user),
    )
    if reason == ManuscriptSnapshot.Reason.EDIT:
        prune_edit_snapshots(manuscript)
    return snapshot


def snapshot(book: Book, label: str = "", reason: str = "manual", user=None) -> dict:
    """Save a copy of the manuscript as it is now («حفظ نسخة باسم…»); returns the snapshot's row dict."""
    if reason not in ManuscriptSnapshot.Reason.values:
        raise EditorError("سبب النسخة غير معروف.")
    label = " ".join(str(label or "").split())
    if len(label) > MAX_LABEL:
        raise EditorError("اسم النسخة أطول من المسموح.")
    with transaction.atomic():
        manuscript = manuscript_of(book, lock=True)
        taken = _take(manuscript, label or f"نسخة محفوظة · الإصدار {manuscript.version}", reason, user)
    return _snapshot_dict(taken, manuscript.version)


def snapshots(book: Book) -> list[dict]:
    """The manuscript's snapshots, newest first (without their documents)."""
    manuscript = manuscript_of(book)
    rows = manuscript.snapshots.select_related("created_by").defer("document")[:SNAPSHOTS_LISTED]
    return [_snapshot_dict(row, manuscript.version) for row in rows]


def restore(book: Book, snapshot_id, user=None) -> dict:
    """Put a snapshot's document back (the current one is kept as an `edit` snapshot first).

    Returns `{version (the manuscript's), snapshot (the one taken before), restored}`. The manuscript's
    run follows the restored document's `runId` when that run still exists, so drift is measured against
    what the restored text was built from.
    """
    from assembly.models import AssemblyRun

    try:
        snapshot_pk = int(str(snapshot_id))
    except (TypeError, ValueError):
        raise EditorNotFound("النسخة غير موجودة.") from None
    with transaction.atomic():
        manuscript = manuscript_of(book, lock=True)
        chosen = manuscript.snapshots.filter(pk=snapshot_pk).first()
        if chosen is None:
            raise EditorNotFound("النسخة غير موجودة.")
        before = _take(
            manuscript,
            f"قبل استعادة «{chosen.label or chosen.version}» · الإصدار {manuscript.version}",
            "edit",
            user,
        )
        document = copy.deepcopy(chosen.document or {})
        run_id = (document.get("attrs") or {}).get("runId") if isinstance(document, dict) else None
        if (
            isinstance(run_id, int)
            and AssemblyRun.objects.filter(pk=run_id, book_id=book.pk, status="done").exists()
        ):
            manuscript.run_id = run_id
            manuscript.save(update_fields=["run"])
        _write(manuscript, document, user)
    _schedule(book, None, manuscript.version)
    return {"version": manuscript.version, "snapshot": before.pk, "restored": chosen.pk}


# ====================================================================== find & replace, digits


def _find_options(data: dict) -> doc.FindOptions:
    return doc.FindOptions(
        match_tashkeel=_parse_bool(data.get("match_tashkeel"), False),
        fold_alef=_parse_bool(data.get("fold_alef"), True),
        whole_word=_parse_bool(data.get("whole_word"), False),
    )


def _scope(document: dict, chapter_id) -> list[doc.ChapterSlice]:
    chapters = doc.chapters_of(document)
    if chapter_id in (None, ""):
        return chapters
    _check_chapter_id(chapter_id)
    chosen = [chapter for chapter in chapters if chapter.id == chapter_id]
    if not chosen:
        raise EditorNotFound(NO_CHAPTER)
    return chosen


def _splice_many(document: dict, edits: dict[str, list]) -> dict:
    """`document` with each chapter in `edits` (id → new nodes) replaced (last chapter first)."""
    for chapter in sorted(doc.chapters_of(document), key=lambda c: c.start, reverse=True):
        if chapter.id in edits:
            document = doc.splice(document, chapter, edits[chapter.id])
    return document


def find_replace(
    book: Book,
    chapter_id,
    query,
    replacement="",
    options: dict | None = None,
    *,
    replace=False,
    version=None,
    user=None,
) -> dict:
    """Find (and with `replace`, replace every match of) `query` in one chapter or, with no chapter, the
    whole book (PHASE5_SPEC §3): tashkeel-insensitive unless `match_tashkeel`, alef forms folded when
    `fold_alef` (default), `whole_word`. Matches: `[{chapter, block, note, index, length}]` (`index` in
    the block's or note's text, its text nodes joined; at most `MAX_MATCHES`), plus `total`.

    Replacing takes an `edit` snapshot first and returns its id (the undo toast restores it), the
    manuscript version and, for one chapter, its new `version`; `version`, when given with a chapter, is
    checked first (`ChapterConflict`).
    """
    data = options if isinstance(options, dict) else {}
    opts = _find_options(data)
    query = str(query or "")
    replacement = str(replacement or "")
    if len(query) > MAX_QUERY or len(replacement) > MAX_QUERY:
        raise EditorError("نص البحث أطول من المسموح.")
    try:
        doc.fold_query(query, opts)
    except doc.DocumentError as exc:
        raise EditorError(str(exc)) from exc
    replace = _parse_bool(replace, False)
    with transaction.atomic():
        manuscript = manuscript_of(book, lock=replace)
        document = manuscript.document or {}
        chapters = _scope(document, chapter_id)
        if replace and chapter_id and version not in (None, ""):
            current = chapters[0].nodes(document)
            if str(version) != doc.chapter_version(current):
                raise ChapterConflict(chapters[0].id, doc.chapter_version(current), current)
        matches: list[dict] = []
        total = 0
        edits: dict[str, list] = {}
        replaced = 0
        for chapter in chapters:
            nodes = chapter.nodes(document)
            found = doc.find_in_nodes(nodes, query, opts)
            total += len(found)
            for match in found[: max(0, MAX_MATCHES - len(matches))]:
                matches.append(
                    {
                        "chapter": chapter.id,
                        "block": match.block,
                        "note": match.note,
                        "index": match.index,
                        "length": match.length,
                    }
                )
            if replace and found:
                copied = copy.deepcopy(nodes)
                replaced += doc.replace_in_nodes(copied, query, replacement, opts)
                edits[chapter.id] = copied
        out = {"matches": matches, "total": total, "replaced": replaced}
        if replace and replaced:
            label = f"قبل استبدال «{query[:40]}» بـ«{replacement[:40]}»"
            taken = _take(manuscript, label, ManuscriptSnapshot.Reason.EDIT, user)
            new_document = _splice_many(document, edits)
            _write(manuscript, new_document, user)
            out.update(snapshot=taken.pk, manuscript_version=manuscript.version)
            if chapter_id:
                chapter = doc.find_chapter(new_document, chapters[0].id)
                out["version"] = doc.chapter_version(chapter.nodes(new_document)) if chapter else None
    if replace and replaced:
        out["relayout"] = _schedule(book, chapter_id or None, manuscript.version)
    return out


def convert_digits(book: Book, chapter_id, style: str, user=None) -> dict:
    """Convert the digits of one chapter (or the whole book) to `style` (`western` or `arabic_indic`, D6).

    Returns `{changed}` (digits changed) and, when something changed, the `edit` snapshot taken first,
    the manuscript version and, for one chapter, its new `version`.
    """
    if style not in doc.DIGIT_STYLES:
        raise EditorError("نمط الأرقام غير معروف؛ المتاح: غربية أو عربية هندية.")
    with transaction.atomic():
        manuscript = manuscript_of(book, lock=True)
        document = manuscript.document or {}
        chapters = _scope(document, chapter_id)
        edits: dict[str, list] = {}
        changed = 0
        for chapter in chapters:
            copied = copy.deepcopy(chapter.nodes(document))
            count = doc.convert_digits_in_nodes(copied, style)
            if count:
                changed += count
                edits[chapter.id] = copied
        out: dict = {"changed": changed}
        if changed:
            label = "قبل تحويل الأرقام إلى " + ("الغربية" if style == "western" else "العربية الهندية")
            taken = _take(manuscript, label, ManuscriptSnapshot.Reason.EDIT, user)
            new_document = _splice_many(document, edits)
            _write(manuscript, new_document, user)
            out.update(snapshot=taken.pk, manuscript_version=manuscript.version)
            if chapter_id:
                chapter = doc.find_chapter(new_document, chapters[0].id)
                out["version"] = doc.chapter_version(chapter.nodes(new_document)) if chapter else None
    if changed:
        out["relayout"] = _schedule(book, chapter_id or None, manuscript.version)
    return out


# ====================================================================== review drift (D41)


def _chapters_of_page(chapters: list[tuple[str, list[int]]], page: int) -> list[str]:
    """The chapters a source page belongs to: every one whose blocks or notes come from it (a page where
    one chapter ends and the next begins is in both), else the one whose page range covers it, else the
    nearest one before it (the first chapter for a page before them all)."""
    having = [chapter_id for chapter_id, pages in chapters if page in pages]
    if having:
        return having
    for chapter_id, pages in chapters:
        if pages and pages[0] <= page <= pages[-1]:
            return [chapter_id]
    before = [(pages[0], chapter_id) for chapter_id, pages in chapters if pages and pages[0] <= page]
    if before:
        return [max(before)[1]]
    return [chapters[0][0]] if chapters else []


def review_drift(book: Book, manuscript: Manuscript | None = None, chapters=None) -> dict:
    """Pages whose text changed in review since the manuscript was built (D41), and the chapters they fall
    in: `{edited, pages: [n…], chapters: {chapter id: [n…]}}`. The manuscript's staleness against the pages
    its run read, without the approval-only pages (`assembly.services.drift_pages`, D70: approving a page
    whose lines did not change is not a change of its text); a chapter re-assembly moves that baseline for
    its pages. A page where one chapter ends and the next begins is listed under both (the change may be in
    either). Three queries (the manuscript with its run, the page rows, the signatures)."""
    from assembly.pipeline import normalize_settings
    from assembly.services import drift_pages, page_rows

    if manuscript is None:
        manuscript = Manuscript.objects.filter(book_id=book.pk).select_related("run").first()
    if manuscript is None:
        return {"edited": False, "pages": [], "chapters": {}}
    edited = manuscript.origin == Manuscript.Origin.EDITOR
    run = manuscript.run
    if run is None:
        return {"edited": edited, "pages": [], "chapters": {}}
    pages = drift_pages(run.included, page_rows(book), normalize_settings(book.assembly_settings))
    document = manuscript.document or {}
    slices = chapters if chapters is not None else doc.chapters_of(document)
    spans = [(chapter.id, doc.chapter_pages(chapter.nodes(document))) for chapter in slices]
    by_chapter: dict[str, list[int]] = {}
    for page in pages:
        for chapter_id in _chapters_of_page(spans, page):
            by_chapter.setdefault(chapter_id, []).append(page)
    return {"edited": edited, "pages": pages, "chapters": by_chapter}


def drift_of(book_id: int) -> dict | None:
    """The live review drift of the book page (D70, `api:review_drift`): `{edited, pages: [n…], chapters:
    [ids]}`, or None when the book has no manuscript. The book comes with the manuscript and its run, so
    three queries in all (`review_drift`)."""
    manuscript = Manuscript.objects.filter(book_id=book_id).select_related("run", "book").first()
    if manuscript is None:
        return None
    drift = review_drift(manuscript.book, manuscript)
    return {"edited": drift["edited"], "pages": drift["pages"], "chapters": sorted(drift["chapters"])}


# ====================================================================== chapter re-assembly (D41)


def reassemble_chapter(book: Book, chapter_id: str, user, replace_edited: bool = False):
    """Start re-assembling one chapter from the reviewed pages (D41); returns the queued `AssemblyRun`.

    The run's settings carry `{"scope": "chapter", "chapter": <id>}`; the task (`editor.tasks`) runs
    the whole pipeline in memory and replaces only this chapter, keeping the old one as an `edit`
    snapshot. Over a text edited on the book page the rebuild replaces the owner's headings, notes and
    words of the chapter, so it needs `replace_edited` (D70, as D49 does for the whole book): without it
    `EditorEdited` (409). An unedited manuscript loses nothing and needs no flag. Refused (400) while an
    assembly of the book is queued or running.
    """
    from assembly.models import AssemblyRun
    from assembly.pipeline import normalize_settings

    _check_chapter_id(chapter_id)
    manuscript = manuscript_of(book)
    if doc.find_chapter(manuscript.document or {}, chapter_id) is None:
        raise EditorNotFound(NO_CHAPTER)
    if manuscript.origin == Manuscript.Origin.EDITOR and not replace_edited:
        raise EditorEdited()
    with transaction.atomic():
        Book.objects.select_for_update().filter(pk=book.pk).first()
        active = AssemblyRun.objects.filter(book_id=book.pk, status__in=("queued", "running"))
        active = [
            run for run in active if run.created_at and timezone.now() - run.created_at < _abandoned_after()
        ]
        if active:
            raise EditorError("يجري تجميع هذا الكتاب الآن؛ انتظر حتى يكتمل ثم أعد المحاولة.")
        settings = normalize_settings(book.assembly_settings).as_dict()
        settings.update(scope="chapter", chapter=chapter_id)
        run = AssemblyRun.objects.create(
            book_id=book.pk,
            status=AssemblyRun.Status.QUEUED,
            settings=settings,
            created_by=_user_or_none(user),
        )
    from .tasks import reassemble_chapter as task

    try:
        result = task.delay(run.pk)
    except Exception as exc:  # noqa: BLE001 - reported on the run
        log.exception("chapter re-assembly %s of book %s could not be enqueued", run.pk, book.pk)
        from assembly.services import ENQUEUE_ERROR

        AssemblyRun.objects.filter(pk=run.pk, status="queued").update(
            status="error",
            error=f"{ENQUEUE_ERROR}\n{type(exc).__name__}: {exc}"[:4000],
            finished_at=timezone.now(),
        )
    else:
        task_id = getattr(result, "id", "") or ""
        if task_id:
            AssemblyRun.objects.filter(pk=run.pk, task_id="").update(task_id=task_id[:64])
    run.refresh_from_db()
    return run


def _abandoned_after():
    from assembly.services import ABANDONED_AFTER

    return ABANDONED_AFTER


def pick_chapter_nodes(new_document: dict, document: dict, chapter: doc.ChapterSlice) -> list:
    """The blocks of a fresh assembly that stand for `chapter` of the edited `document` (D41).

    The fresh blocks are cut where the *edited* document's chapters are — not where the fresh
    document's headings are, which may differ (a heading added or removed in the editor, a line made
    a heading in review) — by source line: from the first fresh block sharing a line with the chapter
    (the first chapter also takes the blocks before it) up to the first block after its last one that
    shares a line with a later chapter. Blocks read from lines of no chapter (text new in review) go
    with the chapter before them; a block whose lines are all in other chapters is left out (it is
    there already), so nothing is duplicated and every chapter keeps its text. A level-1 heading the
    owner typed (no source line in the fresh text) is kept, so the chapter stays a chapter. Without any
    shared line (every line id changed), the blocks whose first source page is in the chapter's range.
    """
    content = doc.content_of(new_document)
    fresh = content[doc.preamble_end(content) :]
    old = doc.content_of(document)
    first_block = doc.preamble_end(old)
    own_nodes = old[chapter.start : chapter.end]
    own = doc.source_lines(own_nodes)
    others = doc.source_lines(old[first_block : chapter.start]) | doc.source_lines(old[chapter.end :])
    later = doc.source_lines(old[chapter.end :])
    lines = [doc.source_lines([node]) for node in fresh]
    hits = [i for i, found in enumerate(lines) if found & own]
    if hits:
        begin = 0 if chapter.start <= first_block else hits[0]
        end = next(
            (i for i in range(hits[-1] + 1, len(fresh)) if lines[i] & later and not lines[i] & own),
            len(fresh),
        )
        picked = [
            fresh[i] for i in range(begin, end) if not lines[i] or lines[i] & own or not lines[i] & others
        ]
    else:
        pages = doc.chapter_pages(own_nodes)
        if not pages:
            return []
        picked = [
            node
            for node, found in zip(fresh, lines, strict=True)
            if doc.source_pages(node)
            and pages[0] <= min(doc.source_pages(node)) <= pages[-1]
            and not (found & others)
        ]
    head = own_nodes[0] if own_nodes else None
    fresh_lines = set().union(*lines) if lines else set()
    if (
        picked
        and chapter.kind == "chapter"
        and doc.is_chapter_heading(head)
        and not doc.is_chapter_heading(picked[0])
        and not (doc.source_lines([head]) & fresh_lines)
    ):
        picked = [head, *picked]
    return picked


def run_chapter_reassembly(run_id: int):
    """Run a queued chapter re-assembly (the task's job): pipeline in memory, then in one transaction an
    `edit` snapshot, the chapter spliced in, the baseline of its pages moved to what was read (so its drift
    clears), its warnings and seams replaced, its reviewed pages marked `assembled` (D36). Any failure →
    the run in error with an Arabic headline, the manuscript untouched. Returns the run."""
    from assembly import pipeline
    from assembly import services as assembly_services
    from assembly.models import AssemblyRun

    run = AssemblyRun.objects.filter(pk=run_id).first()
    if run is None or run.status != AssemblyRun.Status.QUEUED:
        return run
    if not AssemblyRun.objects.filter(pk=run.pk, status="queued").update(status="running", stage="collect"):
        run.refresh_from_db()
        return run
    started = time.monotonic()
    chapter_id = str((run.settings or {}).get("chapter") or "")
    try:
        book = Book.objects.get(pk=run.book_id)
        options = pipeline.normalize_settings(book.assembly_settings)
        loaded = assembly_services.load_book(book)

        def stage(key: str) -> None:
            AssemblyRun.objects.filter(pk=run.pk).update(stage=key)

        result = pipeline.assemble(loaded.pages, options, assembly_services.book_meta(book, run), stage)
        stage("save")
        _save_chapter_run(run, book, chapter_id, loaded, result, started)
    except Exception as exc:  # noqa: BLE001 - reported on the run, the manuscript stays
        if isinstance(exc, EditorError):  # a refusal (chapter gone, superseded), not a crash
            log.warning("chapter re-assembly %s of book %s refused: %s", run.pk, run.book_id, exc)
        else:
            log.exception("chapter re-assembly %s of book %s failed", run.pk, run.book_id)
        message = (
            str(exc) if isinstance(exc, EditorError) else f"{REASSEMBLY_ERROR}\n{type(exc).__name__}: {exc}"
        )
        AssemblyRun.objects.filter(pk=run.pk).update(
            status="error",
            error=message[:4000],
            finished_at=timezone.now(),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    run.refresh_from_db()
    return run


def _save_chapter_run(run, book: Book, chapter_id: str, loaded, result, started: float) -> None:
    from assembly.services import EMPTY_SIGNATURE, page_signatures

    finished = timezone.now()
    with transaction.atomic():
        Book.objects.select_for_update().filter(pk=book.pk).first()
        manuscript = manuscript_of(book, lock=True)
        if manuscript.run_id is not None and manuscript.run_id > run.pk:
            # a full assembly queued after this run saved first: it read every page again
            raise EditorError(SUPERSEDED)
        document = manuscript.document or {}
        chapter = doc.find_chapter(document, chapter_id)
        if chapter is None:
            raise EditorError(NO_CHAPTER)
        old_nodes = chapter.nodes(document)
        nodes = copy.deepcopy(pick_chapter_nodes(result.document, document, chapter))
        if not nodes:
            raise EditorError(CHAPTER_GONE)
        doc.repair_ids(nodes, _other_ids(document, chapter))
        pages = set(doc.chapter_pages(old_nodes)) | set(doc.chapter_pages(nodes))
        _take(
            manuscript,
            f"قبل إعادة تجميع الفصل «{chapter.title}» · الإصدار {manuscript.version}",
            "edit",
            run.created_by,
        )
        new_document = doc.splice(document, chapter, nodes)
        attrs = dict(new_document.get("attrs") or {})
        new_seams = [s for s in result.seams if isinstance(s, dict) and s.get("page") in pages]
        seams = [s for s in attrs.get("seams") or [] if isinstance(s, dict) and s.get("page") not in pages]
        attrs["seams"] = sorted([*seams, *new_seams], key=lambda s: int(s.get("page") or 0))
        attrs["runId"] = run.pk
        new_document["attrs"] = attrs
        base = manuscript.run
        included = dict(base.included or {}) if base is not None else {}
        read = {str(page_id) for page_id in result.pages}
        for key, info in list(included.items()):
            # a page of this chapter the fresh run no longer includes (excluded, back in the pipeline)
            if isinstance(info, dict) and info.get("number") in pages and key not in read:
                del included[key]
        for page in loaded.pages:
            if page.id in result.pages and page.number in pages:
                included[str(page.id)] = {
                    "number": page.number,
                    "reviewed": page.reviewed,
                    "sig": loaded.signatures.get(page.id, EMPTY_SIGNATURE),
                }
        old_warnings = [
            w for w in (base.warnings if base is not None else []) or [] if w.get("page") not in pages
        ]
        new_warnings = [w for w in result.warnings if w.get("page") in pages]
        stats = dict(base.stats or {}) if base is not None else dict(result.stats)
        stats.update(doc.document_stats(new_document))
        manuscript.document = new_document
        manuscript.version += 1
        manuscript.run = run
        manuscript.updated_by = run.created_by
        manuscript.save(update_fields=["document", "version", "run", "updated_by", "updated_at"])
        reviewed = [
            page.id for page in loaded.pages if page.number in pages and page.status == Page.Status.REVIEWED
        ]
        now = page_signatures(reviewed)
        unchanged = [pk for pk in reviewed if now.get(pk) == loaded.signatures.get(pk)]
        if unchanged:
            Page.objects.filter(pk__in=unchanged, status=Page.Status.REVIEWED).update(
                status=Page.Status.ASSEMBLED
            )
        Book.objects.get(pk=book.pk).refresh_status()
        run.status = "done"
        run.stage = "save"
        run.warnings = sorted(
            [*old_warnings, *new_warnings], key=lambda w: -1 if w.get("page") is None else w["page"]
        )
        run.stats = stats
        run.included = included
        run.error = ""
        run.finished_at = finished
        run.duration_ms = int((time.monotonic() - started) * 1000)
        run.save(
            update_fields=[
                "status",
                "stage",
                "warnings",
                "stats",
                "included",
                "error",
                "finished_at",
                "duration_ms",
            ]
        )
    _schedule(book, chapter_id, manuscript.version)


# ====================================================================== stylesheet

NUMBER_FIELDS: dict[str, tuple[float, float, str]] = {
    "width_mm": (80, 400, "العرض بين 80 و400 مم."),
    "height_mm": (100, 500, "الارتفاع بين 100 و500 مم."),
    "top_mm": (0, 80, "الهامش بين 0 و80 مم."),
    "bottom_mm": (0, 80, "الهامش بين 0 و80 مم."),
    "inner_mm": (0, 80, "الهامش بين 0 و80 مم."),
    "outer_mm": (0, 80, "الهامش بين 0 و80 مم."),
    "bleed_mm": (0, 10, "زيادة القص بين 0 و10 مم."),
    "body_size_pt": (7, 24, "حجم خط المتن بين 7 و24 نقطة."),
    "line_height": (1.0, 3.0, "تباعد الأسطر بين 1 و3."),
    "indent_em": (0, 6, "الإزاحة بين 0 و6."),
    "footnote_size_pt": (6, 16, "حجم خط الحواشي بين 6 و16 نقطة."),
}
HEADING_SCALE_LIMITS: dict[str, tuple[float, float, str]] = {
    "h1": (1.0, 3.0, "حجم عنوان الفصل بين 1 و3 أضعاف المتن."),
    "h2": (1.0, 2.5, "حجم العنوان الفرعي بين 1 و2.5 ضعف المتن."),
}
CHOICE_FIELDS: dict[str, type] = {
    "running_header": StyleSheet.RunningHeader,
    "page_number": StyleSheet.PageNumber,
    "chapter_opening": StyleSheet.ChapterOpening,
    "footnote_numbering": StyleSheet.FootnoteNumbering,
}
LINE_COUNT_FIELDS: dict[str, tuple[int, int, str]] = {
    "widows": (1, 5, "أقل عدد من الأسطر أعلى الصفحة بين 1 و5."),
    "orphans": (1, 5, "أقل عدد من الأسطر أسفل الصفحة بين 1 و5."),
}
BOOK_FIELD_LIMITS: dict[str, int] = {name: 300 for name in BOOK_FIELDS} | {"rights": 1000, "isbn": 40}
STYLESHEET_FIELDS: tuple[str, ...] = (
    "trim",
    *NUMBER_FIELDS,
    "body_font",
    "latin_font",
    "heading_font",
    "heading_scale",
    *CHOICE_FIELDS,
    *LINE_COUNT_FIELDS,
    "keep_headings",
    "front_matter",
    "print_source_pages",
)
FRONT_FLAGS: dict[str, bool] = {"title_page": True, "contents": True, "copyright_page": False}


def stylesheet_for(book: Book) -> StyleSheet:
    """The book's stylesheet, or an unsaved one with the defaults."""
    sheet = StyleSheet.objects.filter(book_id=book.pk).first()
    return sheet if sheet is not None else StyleSheet(book=book)


def stylesheet_values(sheet: StyleSheet) -> dict:
    """The stylesheet's fields as JSON values."""
    out = {name: getattr(sheet, name) for name in STYLESHEET_FIELDS}
    out["heading_scale"] = {"h1": 1.6, "h2": 1.25, **(sheet.heading_scale or {})}
    out["front_matter"] = front_matter_values(sheet.front_matter)
    out["updated_at"] = sheet.updated_at.isoformat() if sheet.updated_at else None
    return out


def front_matter_values(front) -> dict:
    """`front_matter` with every flag and every book detail present (`fields`: '' when not given)."""
    front = front if isinstance(front, dict) else {}
    fields = front.get("fields") if isinstance(front.get("fields"), dict) else {}
    out = {**FRONT_FLAGS, **{key: bool(front[key]) for key in FRONT_FLAGS if key in front}}
    out["fields"] = {name: str(fields.get(name) or "") for name in BOOK_FIELDS}
    return out


def stylesheet_payload(book: Book) -> dict:
    """`api:stylesheet` GET: `{stylesheet, saved, trims, fonts, choices, limits, missing_fonts,
    field_defaults}` (`field_defaults`: the book details used when a field is empty — the Book's title
    and author)."""
    from publishing.fonts import LATIN_FONTS, font_status, resolve

    sheet = stylesheet_for(book)
    values = stylesheet_values(sheet)
    return {
        "stylesheet": values,
        "saved": sheet.pk is not None,
        "trims": [
            *(
                {"key": key, "label": label, "width_mm": w, "height_mm": h}
                for key, (label, w, h) in TRIM_PRESETS.items()
            ),
            {"key": CUSTOM_TRIM, "label": StyleSheet.Trim.CUSTOM.label, "width_mm": None, "height_mm": None},
        ],
        "fonts": font_status(),
        "latin_fonts": list(LATIN_FONTS),
        "choices": {
            name: [{"value": value, "label": str(label)} for value, label in choices.choices]
            for name, choices in CHOICE_FIELDS.items()
        },
        "limits": {
            **{name: [lo, hi] for name, (lo, hi, _msg) in NUMBER_FIELDS.items()},
            **{f"heading_scale.{name}": [lo, hi] for name, (lo, hi, _msg) in HEADING_SCALE_LIMITS.items()},
            **{name: [lo, hi] for name, (lo, hi, _msg) in LINE_COUNT_FIELDS.items()},
            **{f"front_matter.fields.{name}": [0, limit] for name, limit in BOOK_FIELD_LIMITS.items()},
        },
        "missing_fonts": resolve(sheet.body_font, sheet.latin_font, sheet.heading_font).missing,
        "book_fields": list(BOOK_FIELDS),
        "field_defaults": {"title": book.title, "author": book.author},
    }


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789.")))
        except ValueError:
            return None
    return None


def update_stylesheet(book: Book, data, user=None) -> tuple[StyleSheet, bool]:
    """Validate and save the posted stylesheet fields (any subset; unknown keys are ignored).

    A preset `trim` sets the width and height; `custom` keeps the posted (or stored) ones. Faces must be
    registry keys (the Latin face one with Latin glyphs); a face not installed is accepted (it renders in
    Amiri and the panel says «غير مثبّت»). Raises `StyleSheetError` with every bad field. Returns
    `(stylesheet, changed)`.
    """
    from publishing.fonts import FONTS, LATIN_FONTS

    data = data if isinstance(data, dict) else {}
    errors: dict[str, str] = {}
    with transaction.atomic():
        Book.objects.select_for_update().filter(pk=book.pk).first()
        sheet = stylesheet_for(book)
        before = stylesheet_values(sheet)
        trim = data.get("trim", sheet.trim)
        if not isinstance(trim, str) or trim not in StyleSheet.Trim.values:
            errors["trim"] = "القطع غير معروف."
        else:
            sheet.trim = trim
        for name, (lo, hi, message) in NUMBER_FIELDS.items():
            if name not in data:
                continue
            value = _number(data[name])
            if value is None or not lo <= value <= hi:
                errors[name] = message
            else:
                setattr(sheet, name, round(value, 2))
        if sheet.trim in TRIM_PRESETS:
            sheet.width_mm, sheet.height_mm = TRIM_PRESETS[sheet.trim][1:]
            errors.pop("width_mm", None)
            errors.pop("height_mm", None)
        for name in ("body_font", "heading_font"):
            if name in data:
                if not isinstance(data[name], str) or data[name] not in FONTS:
                    errors[name] = "الخط غير معروف."
                else:
                    setattr(sheet, name, data[name])
        if "latin_font" in data:
            if not isinstance(data["latin_font"], str) or data["latin_font"] not in LATIN_FONTS:
                errors["latin_font"] = "اختر للنص اللاتيني خطًّا فيه حروف لاتينية."
            else:
                sheet.latin_font = data["latin_font"]
        if "heading_scale" in data:
            scale = data["heading_scale"]
            merged = {"h1": 1.6, "h2": 1.25, **(sheet.heading_scale or {})}
            if not isinstance(scale, dict):
                errors["heading_scale"] = "أحجام العناوين غير صالحة."
            else:
                for key, (lo, hi, message) in HEADING_SCALE_LIMITS.items():
                    if key in scale:
                        value = _number(scale[key])
                        if value is None or not lo <= value <= hi:
                            errors["heading_scale"] = message
                        else:
                            merged[key] = round(value, 2)
                sheet.heading_scale = merged
        for name, choices in CHOICE_FIELDS.items():
            if name in data:
                if not isinstance(data[name], str) or data[name] not in choices.values:
                    errors[name] = "قيمة غير معروفة."
                else:
                    setattr(sheet, name, data[name])
        for name, (lo, hi, message) in LINE_COUNT_FIELDS.items():
            if name in data:
                value = _number(data[name])
                if value is None or value != int(value) or not lo <= value <= hi:
                    errors[name] = message
                else:
                    setattr(sheet, name, int(value))
        if "keep_headings" in data:
            sheet.keep_headings = _parse_bool(data["keep_headings"], sheet.keep_headings)
        if "front_matter" in data:
            front = data["front_matter"]
            merged = front_matter_values(sheet.front_matter)
            if not isinstance(front, dict):
                errors["front_matter"] = "الصفحات التمهيدية غير صالحة."
            else:
                for key in FRONT_FLAGS:
                    if key in front:
                        merged[key] = _parse_bool(front[key], merged[key])
                fields = front.get("fields", {})
                if not isinstance(fields, dict):
                    errors["front_matter"] = "بيانات الكتاب غير صالحة."
                else:
                    for name, value in fields.items():
                        if name not in BOOK_FIELDS:
                            continue
                        if value is None:
                            value = ""
                        if not isinstance(value, str | int) or isinstance(value, bool):
                            errors[f"front_matter.fields.{name}"] = "قيمة غير صالحة."
                            continue
                        text = str(value).strip() if name == "rights" else " ".join(str(value).split())
                        if len(text) > BOOK_FIELD_LIMITS[name]:
                            errors[f"front_matter.fields.{name}"] = (
                                f"النص أطول من المسموح ({BOOK_FIELD_LIMITS[name]} حرفًا)."
                            )
                            continue
                        merged["fields"][name] = text
                merged["fields"] = {name: text for name, text in merged["fields"].items() if text}
                sheet.front_matter = merged
        if "print_source_pages" in data:
            sheet.print_source_pages = _parse_bool(data["print_source_pages"], sheet.print_source_pages)
        if sheet.inner_mm + sheet.outer_mm > sheet.width_mm - 40:
            errors.setdefault("inner_mm", "الهوامش الجانبية أعرض من أن يبقى للنص مكان.")
        if sheet.top_mm + sheet.bottom_mm > sheet.height_mm - 60:
            errors.setdefault("top_mm", "الهامشان العلوي والسفلي أكبر من أن يبقى للنص مكان.")
        if errors:
            raise StyleSheetError(errors)
        after = stylesheet_values(sheet)
        after["updated_at"] = before["updated_at"]
        changed = after != before or sheet.pk is None
        if changed:
            sheet.save()
    return sheet, changed


# ====================================================================== dashboard and pages


def editor_state(book: Book, manuscript_state: dict) -> dict:
    """The dashboard's `editor` state: `{edited, version, drift_pages}` (D41). Without a manuscript no
    query is made; otherwise one (the drift is the manuscript's stale pages without the approval-only ones,
    `drift_pages`, already computed: D70)."""
    if not manuscript_state.get("exists"):
        return {"edited": False, "version": 0, "drift_pages": []}
    row = Manuscript.objects.filter(book_id=book.pk).values("origin", "version").first()
    if row is None:
        return {"edited": False, "version": 0, "drift_pages": []}
    edited = row["origin"] == Manuscript.Origin.EDITOR
    return {
        "edited": edited,
        "version": row["version"],
        "drift_pages": list(manuscript_state.get("drift_pages") or []) if edited else [],
    }


CHAPTER_SLOT = "__cid__"


def editor_urls(book: Book) -> dict:
    """URLs of the book page and its APIs (`__cid__` stands for a chapter id, `__sid__` for a snapshot,
    `__rid__` for a re-layout). `layout` is the book page (D47); the old editor address only redirects
    there, so nothing links to it."""
    chapter = reverse("api:chapter", args=[book.pk, "CID"]).replace("CID", CHAPTER_SLOT)
    reassemble = reverse("api:chapter_reassemble", args=[book.pk, "CID"]).replace("CID", CHAPTER_SLOT)
    relayout = reverse("api:relayout", args=[book.pk, "CID"]).replace("CID", CHAPTER_SLOT)
    restore_url = reverse("api:snapshot_restore", args=[book.pk, 0])
    head, _sep, tail = restore_url.rpartition("/0/")
    status_url = reverse("api:relayout_status", args=[book.pk, 0])
    status_head, _sep, status_tail = status_url.rpartition("/0/")
    return {
        "layout": reverse("editor:layout", args=[book.pk]),
        "chapters": reverse("api:chapters", args=[book.pk]),
        "chapter": chapter,
        "reassemble": reassemble,
        "drift": reverse("api:review_drift", args=[book.pk]),
        "findReplace": reverse("api:find_replace", args=[book.pk]),
        "convertDigits": reverse("api:convert_digits", args=[book.pk]),
        "snapshots": reverse("api:snapshots", args=[book.pk]),
        "restore": f"{head}/__sid__/{tail}",
        "stylesheet": reverse("api:stylesheet", args=[book.pk]),
        "preview": reverse("api:preview", args=[book.pk]),
        "manuscriptState": reverse("api:manuscript_state", args=[book.pk]),
        "manuscript": reverse("assembly:manuscript", args=[book.pk]),
        "dashboard": reverse("books:detail", args=[book.pk]),
        "sheets": reverse("api:book_sheets", args=[book.pk]),
        # D47: live pages, the fast re-layout and the uncertain words
        "pageLayout": reverse("api:preview_layout", args=[book.pk]),
        "relayout": relayout,
        "relayoutStatus": f"{status_head}/__rid__/{status_tail}",
        "uncertain": reverse("api:uncertain", args=[book.pk]),
        "uncertainAccept": reverse("api:uncertain_accept", args=[book.pk]),
        "uncertainChoose": reverse("api:uncertain_choose", args=[book.pk]),
        "uncertainType": reverse("api:uncertain_type", args=[book.pk]),
    }


AUTOSAVE_MS = 1500
RELAYOUT_MS = 500  # the pause after typing on a live page before the chapter is saved and re-laid-out
MODES: tuple[str, ...] = ("preview", "edit")
# the side panel's tabs (`static/src/js/book/panel.js` TABS): `?tab=` opens the book page on one of them
PANEL_TABS: tuple[str, ...] = ("chapters", "pages", "find", "format", "block", "source", "uncertain")


def _faces(sheet: StyleSheet) -> dict:
    """The stylesheet's faces for the browser: registry key, display name and installed family name
    (the editor sheet uses them through `local()`; a missing face falls back to Amiri like the pages)."""
    from publishing.fonts import resolve

    fonts = resolve(sheet.body_font, sheet.latin_font, sheet.heading_font)
    return {
        role: {
            "key": face.key,
            "requested": face.requested,
            "name": face.name,
            "family": face.family,
            "fallback": face.fallback,
        }
        for role, face in (("body", fonts.body), ("latin", fonts.latin), ("heading", fonts.heading))
    }


def page_config(
    book: Book,
    user,
    chapter_id: str | None = None,
    page: str = "editor",
    mode: str | None = None,
    tab: str | None = None,
) -> dict:
    """What the book page (`bookLayout`, D47) and the old editor page start from: the book, the chapter to
    open (the requested one when it exists, else the first), the chapters' order, whether the user may
    edit, the stylesheet with its faces and every URL (`editor_urls`).

    For the book page (`page="layout"`) also: `mode` (`preview` | `edit`), the chapter summaries (versions,
    words, pages, drift: `chapter_summaries`), the review drift, the uncertain words' count, the browser
    `@font-face` rules of the live pages (`fontCss`), the pause before a re-layout (`relayoutMs`) and
    `tab`, the panel tab asked for with `?tab=` (one of `PANEL_TABS`, else None: the panel opens the tab
    remembered for the mode; the export page's readiness links use it, PHASE6_SPEC §6.5)."""
    from books.services import page_url_templates
    from core.decorators import ROLE_EDITOR, has_role

    manuscript = (
        Manuscript.objects.filter(book_id=book.pk)
        .only("id", "book_id", "document", "version", "origin")
        .first()
    )
    document = manuscript.document if manuscript is not None else {}
    chapters = doc.chapters_of(document)
    ids = [chapter.id for chapter in chapters]
    current = chapter_id if chapter_id in ids else (ids[0] if ids else None)
    sheet = stylesheet_for(book)
    extra: dict = {}
    if page == "layout":
        from publishing.fonts import browser_font_css, resolve

        from .uncertain import count as uncertain_count

        drift = (
            review_drift(book) if manuscript is not None else {"edited": False, "pages": [], "chapters": {}}
        )
        extra = {
            "mode": mode if mode in MODES else "preview",
            "chapterSummaries": chapter_summaries(book) if manuscript is not None else [],
            "drift": {
                "edited": drift["edited"],
                "pages": drift["pages"],
                "chapters": sorted(drift["chapters"]),
            },
            "uncertainCount": uncertain_count(document) if manuscript is not None else 0,
            "fontCss": browser_font_css(resolve(sheet.body_font, sheet.latin_font, sheet.heading_font)),
            "relayoutMs": RELAYOUT_MS,
            "tab": tab if tab in PANEL_TABS else None,
        }
    return {
        **extra,
        "page": page,
        "bookId": book.pk,
        "title": book.title,
        "author": book.author,
        "exists": manuscript is not None,
        "manuscriptVersion": manuscript.version if manuscript is not None else 0,
        "origin": manuscript.origin if manuscript is not None else None,
        "chapter": current,
        "requestedChapter": chapter_id or None,
        "chapters": [{"id": c.id, "number": c.number, "kind": c.kind, "title": c.title} for c in chapters],
        "canEdit": has_role(user, ROLE_EDITOR),
        "stylesheet": stylesheet_values(sheet),
        "faces": _faces(sheet),
        "autosaveMs": AUTOSAVE_MS,
        "urls": {**editor_urls(book), "review": page_url_templates(book)["review"]},
    }
