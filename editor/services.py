"""Editor services (PHASE5_SPEC §3): chapters, saves, snapshots, find & replace, digits, review drift,
per-chapter re-assembly, the book's stylesheet and its images (D80: the cover's settings and pictures).

The manuscript stays one JSON document (`editor.Manuscript.document`); the editor works on one chapter at
a time (D40, `editor.document`). Every save is version-checked per chapter (a short hash of its nodes):
a stale tab gets a conflict with the current chapter, a save of another chapter never conflicts. The
first save sets `origin = editor`: from then on the manuscript is the source of truth (D41) and review
corrections no longer flow in — pages changed in review since show as drift, and each chapter can be
re-assembled from the reviewed pages (the old chapter kept as a snapshot, reason `edit`).

Views and API functions stay thin; the renders after a save are scheduled through `publishing.engine`
(the chapter's fast re-layout at once, the book debounced, D44/D47), never run here: an edit's answer
carries `relayout` (`{id, status, url}` to poll, or null) so the book page can swap its live pages.

**The cover (D80, COVER_SPEC §2).** `front_matter["cover"]` is validated key by key by the stylesheet's PUT
(`_cover_update`, errors `front_matter.cover.<key>`), filled with its defaults in every payload
(`publishing.model.cover_settings`) and stored only once a cover key was posted, so a book that never
chose a cover keeps the stored stylesheet it had. `cover_choices` is the panel's block (modes, fits,
presets, limits, the print-size note). `upload_image` checks, normalises and stores an uploaded image
(`editor.BookImage`, named by its content; see `normalise_image`).
"""

from __future__ import annotations

import copy
import hashlib
import io
import logging
import os
import re
import time
from dataclasses import dataclass

from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from books.models import Book, Page
from publishing.model import (
    COVER_BOTTOM_GAP_MM,
    COVER_BOTTOM_LIMITS,
    COVER_DEFAULTS,
    COVER_FITS,
    COVER_MODES,
    COVER_PRESETS,
    COVER_SIZE_LIMITS,
    COVER_TEXT_MAX,
    CUSTOM_PRESET,
    cover_settings,
    cover_text,
    hex_colour,
    preset_of,
)

from . import document as doc
from .models import (
    BOOK_FIELDS,
    CUSTOM_TRIM,
    TRIM_PRESETS,
    BookImage,
    Manuscript,
    ManuscriptSnapshot,
    StyleSheet,
)

log = logging.getLogger(__name__)

EDIT_SNAPSHOTS_KEPT = 20  # automatic `edit` snapshots kept per manuscript (manual ones are never pruned)
MAX_QUERY = 500
MAX_MATCHES = 5000
MAX_LABEL = 200
SNAPSHOTS_LISTED = 100

NO_MANUSCRIPT = "لم تُجمَع مخطوطة هذا الكتاب بعد؛ اجمعها أولًا."
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


def manuscript_of(
    book: Book, *, lock: bool = False, with_run: bool = False, with_base: bool = False
) -> Manuscript:
    """The book's manuscript (`EditorNotFound` before the first assembly); `lock` inside a transaction.

    Its `base` (D78, as large as the document) is deferred unless `with_base`: the autosave locks the row
    every 1.5 s and never reads it (`_mark_edited` only writes it)."""
    rows = Manuscript.objects.filter(book_id=book.pk)
    if with_run:
        rows = rows.select_related("run")
    if not with_base:
        rows = rows.defer("base")
    if lock:  # the manuscript's row only: PostgreSQL refuses FOR UPDATE on the nullable side of a join
        rows = rows.select_for_update(of=("self",))
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
            fields = _mark_edited(manuscript)
            manuscript.document = new_document
            manuscript.version += 1
            manuscript.origin = Manuscript.Origin.EDITOR
            manuscript.updated_by = _user_or_none(user)
            manuscript.save(
                update_fields=[*fields, "document", "version", "origin", "updated_by", "updated_at"]
            )
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


_KEEP = object()  # `_write`: the base follows `_mark_edited`


def _mark_edited(manuscript: Manuscript) -> list[str]:
    """Before the first edit of an assembled text, keep that text as the manuscript's base (D78): the
    assembled document every later edit descends from. Every editor write calls it first (`save_chapter`,
    `_write`: find & replace, digits, the uncertain words); a text already edited keeps its base (an edit
    never rewrites it). Returns the fields to save with the write (`["base"]` or none)."""
    if manuscript.origin != Manuscript.Origin.ASSEMBLY:
        return []
    manuscript.base = copy.deepcopy(manuscript.document or {})
    return ["base"]


def _write(manuscript: Manuscript, document: dict, user, base=_KEEP) -> None:
    """Write an edited document (version + 1, origin editor); the base as `_mark_edited` keeps it, or `base`
    when given (a restore puts the snapshot's back)."""
    if base is _KEEP:
        fields = _mark_edited(manuscript)
    else:
        manuscript.base = base
        fields = ["base"]
    manuscript.document = document
    manuscript.version += 1
    manuscript.origin = Manuscript.Origin.EDITOR
    manuscript.updated_by = _user_or_none(user)
    manuscript.save(update_fields=[*fields, "document", "version", "origin", "updated_by", "updated_at"])


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


def _take(
    manuscript: Manuscript, label: str, reason: str, user, run_id: int | None = None
) -> ManuscriptSnapshot:
    """Save a copy of the document and its base (D78). `run_id` stamps the copy's `attrs.runId`, so a
    restore puts that run back as the baseline (an apply of review changes moves the baseline without
    writing the document)."""
    document = copy.deepcopy(manuscript.document or {})
    if run_id is not None and isinstance(document, dict):
        document["attrs"] = {**(document.get("attrs") or {}), "runId": run_id}
    snapshot = ManuscriptSnapshot.objects.create(
        manuscript=manuscript,
        document=document,
        base=copy.deepcopy(manuscript.base),
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
    rows = manuscript.snapshots.select_related("created_by").defer("document", "base")[:SNAPSHOTS_LISTED]
    return [_snapshot_dict(row, manuscript.version) for row in rows]


def restore(book: Book, snapshot_id, user=None) -> dict:
    """Put a snapshot's document back (the current one is kept as an `edit` snapshot first).

    Returns `{version (the manuscript's), snapshot (the one taken before), restored}`. The manuscript's
    run follows the restored document's `runId` when that run still exists, so drift is measured against
    what the restored text was built from. The base (D78) is the snapshot's; a snapshot taken before 7c has
    none (its pages merge as without a base), except one taken before a re-assembly, whose text was not
    edited and is its own base. The current document never becomes the base (review changes made since
    would be lost).
    """
    from assembly.models import AssemblyRun

    try:
        snapshot_pk = int(str(snapshot_id))
    except (TypeError, ValueError):
        raise EditorNotFound("النسخة غير موجودة.") from None
    with transaction.atomic():
        manuscript = manuscript_of(book, lock=True, with_base=True)
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
        base = copy.deepcopy(chosen.base)
        if base is None and chosen.reason == ManuscriptSnapshot.Reason.REASSEMBLY:
            base = copy.deepcopy(chosen.document or {})
        _write(manuscript, document, user, base=base)
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


NO_DRIFT: dict = {"edited": False, "pages": [], "reasons": {}, "approvals": [], "chapters": {}}


def review_drift(book: Book, manuscript: Manuscript | None = None, chapters=None) -> dict:
    """Pages whose text changed since the manuscript was built (D41), classified (D78), and the chapters they
    fall in: `{edited, pages: [n…], reasons: {"n": review | processing | added | removed}, approvals: [n…],
    chapters: {chapter id: [n…]}}`.

    `pages` is the content drift against the pages the manuscript's run read (`assembly.services.
    stale_reasons`, D70: approving a page whose lines did not change is not a change of its text; those
    pages are `approvals`, never announced, absorbed by the next apply). `review`: a line-changing review
    revision after the page was read (undone ones too); `processing`: the lines changed with none (a
    re-run, the numbers pass); `added` / `removed`: the page came into the book or left it. A chapter
    re-assembly and an apply move the baseline of their pages. A page where one chapter ends and the next
    begins is listed under both. Three queries (the manuscript with its run, the page rows, the signatures),
    a fourth for the revisions when a page's lines changed."""
    from assembly.pipeline import normalize_settings
    from assembly.services import page_rows, stale_reasons

    if manuscript is None:
        manuscript = Manuscript.objects.filter(book_id=book.pk).select_related("run").defer("base").first()
    if manuscript is None:
        return copy.deepcopy(NO_DRIFT)
    edited = manuscript.origin == Manuscript.Origin.EDITOR
    run = manuscript.run
    if run is None:
        return {**copy.deepcopy(NO_DRIFT), "edited": edited}
    classified = stale_reasons(
        run.included, page_rows(book), normalize_settings(book.assembly_settings), finished_at=run.finished_at
    )
    pages = classified["pages"]
    document = manuscript.document or {}
    slices = chapters if chapters is not None else doc.chapters_of(document)
    spans = [(chapter.id, doc.chapter_pages(chapter.nodes(document))) for chapter in slices]
    by_chapter: dict[str, list[int]] = {}
    for page in pages:
        for chapter_id in _chapters_of_page(spans, page):
            by_chapter.setdefault(chapter_id, []).append(page)
    return {
        "edited": edited,
        "pages": pages,
        "reasons": classified["reasons"],
        "approvals": classified["approvals"],
        "chapters": by_chapter,
    }


def drift_payload(drift: dict) -> dict:
    """`review_drift` as the book page reads it (`api:review_drift`, `config.drift`): `chapters` the ids with
    drift (as in 7a), `chapter_pages` the pages of each (the 7c contract, editor/fixtures/contract/)."""
    return {
        "edited": drift["edited"],
        "pages": list(drift["pages"]),
        "reasons": dict(drift.get("reasons") or {}),
        "approvals": list(drift.get("approvals") or []),
        "chapters": sorted(drift["chapters"]),
        "chapter_pages": {key: list(value) for key, value in sorted(drift["chapters"].items())},
    }


def drift_of(book_id: int) -> dict | None:
    """The live review drift of the book page (D70, `api:review_drift`): `drift_payload`, or None when the
    book has no manuscript. The book comes with the manuscript and its run, so three queries in all (a
    fourth when a page's lines changed: `review_drift`)."""
    manuscript = (
        Manuscript.objects.filter(book_id=book_id).select_related("run", "book").defer("base").first()
    )
    if manuscript is None:
        return None
    return drift_payload(review_drift(manuscript.book, manuscript))


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
        read = timezone.now()
        loaded = assembly_services.load_book(book)

        def stage(key: str) -> None:
            AssemblyRun.objects.filter(pk=run.pk).update(stage=key)

        result = pipeline.assemble(loaded.pages, options, assembly_services.book_meta(book, run), stage)
        stage("save")
        _save_chapter_run(run, book, chapter_id, loaded, result, started, read)
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


def _save_chapter_run(run, book: Book, chapter_id: str, loaded, result, started: float, read=None) -> None:
    from assembly.services import EMPTY_SIGNATURE, page_signatures

    from . import merge

    finished = timezone.now()
    stamp = (read or finished).isoformat()
    with transaction.atomic():
        Book.objects.select_for_update().filter(pk=book.pk).first()
        manuscript = manuscript_of(book, lock=True, with_base=True)
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
                    "at": stamp,
                }
        old_warnings = [
            w for w in (base.warnings if base is not None else []) or [] if w.get("page") not in pages
        ]
        new_warnings = [w for w in result.warnings if w.get("page") in pages]
        stats = dict(base.stats or {}) if base is not None else dict(result.stats)
        stats.update(doc.document_stats(new_document))
        fields = ["document", "version", "run", "updated_by", "updated_at"]
        if manuscript.origin == Manuscript.Origin.EDITOR:
            # D78: the rebuilt chapter now is the fresh text, so its pages' base is too (a book edited before
            # 7c: the fresh text, with the other drift pages recorded as not known)
            drift = review_drift(book, manuscript)["pages"]
            manuscript.base = merge.splice_base(
                manuscript.base, result.document, pages, unknown=set(drift) - pages
            )
            fields.append("base")
        manuscript.document = new_document
        manuscript.version += 1
        manuscript.run = run
        manuscript.updated_by = run.created_by
        manuscript.save(update_fields=fields)
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


# ====================================================================== review changes, page by page (D78)

PLAN_ERROR = "تعذّرت المقارنة؛ بقي الكتاب كما هو."
PLAN_ABANDONED = "توقّفت المقارنة قبل أن تكتمل؛ أعد المحاولة."
NOT_EDITED = "لم يُحرَّر نص الكتاب بعد؛ أعد التجميع، فلا يضيع شيء."
PLAN_MISSING = "المقارنة غير موجودة."
PLAN_NOT_READY = "لم تكتمل المقارنة بعد."
BAD_CHOICE = "اختيار غير معروف لأحد التغييرات."
BAD_PAGES = "أرقام الصفحات غير صالحة."
NO_PAGES_TAKEN = "لم تُحدَّد صفحة من صفحات المقارنة."
BAD_BATCH = "لا تصحيح بهذا المعرّف في هذا الكتاب."
STALE_VERSION = "تغيّر نص الكتاب منذ المقارنة؛ أعد المقارنة."
REASON_LABELS: dict[str, str] = {
    "review": "المراجعة",
    "processing": "إعادة المعالجة",
    "added": "صفحة جديدة",
    "removed": "أُخرجت من الكتاب",
}
MAX_PLAN_PAGES = 5000
_RE_BATCH = re.compile(r"[0-9a-fA-F-]{32,36}")


class PlanStale(EditorError):
    """The text or a planned page changed since the plan (API 409 `{detail, stale: true, pages}`)."""

    def __init__(self, message: str, pages=()):
        super().__init__(message)
        self.pages = sorted(pages)


class NotEdited(EditorError):
    """Review changes asked for on a text never edited: a plain re-assembly loses nothing (API 409
    `{detail, reassemble: true}`)."""

    def __init__(self):
        super().__init__(NOT_EDITED)


def _stale_pages_message(pages: list[int]) -> str:
    if len(pages) == 1:
        return f"تغيّرت الصفحة {pages[0]} في المراجعة منذ المقارنة؛ أعد المقارنة."
    return f"تغيّرت الصفحات {'، '.join(str(n) for n in pages)} في المراجعة منذ المقارنة؛ أعد المقارنة."


def _page_numbers(values) -> list[int] | None:
    """Posted page numbers (ints or digit strings), sorted and unique; None when not given."""
    if values is None:
        return None
    if not isinstance(values, list | tuple) or len(values) > MAX_PLAN_PAGES:
        raise EditorError(BAD_PAGES)
    out: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not str(value).strip().isdigit():
            raise EditorError(BAD_PAGES)
        out.add(int(str(value).strip()))
    return sorted(out)


def _batch_pages(book: Book, batch) -> list[int]:
    """The page numbers of a «تصحيح في كل الكتاب» batch (D79) of this book."""
    from review.models import LineRevision

    if not isinstance(batch, str) or not _RE_BATCH.fullmatch(batch):
        raise EditorNotFound(BAD_BATCH)
    numbers = sorted(
        set(
            LineRevision.objects.filter(page__book_id=book.pk, batch=batch).values_list(
                "page__number", flat=True
            )
        )
    )
    if not numbers:
        raise EditorNotFound(BAD_BATCH)
    return numbers


def plan_review_changes(book: Book, user, pages=None, fix=None):
    """Start comparing review changes with the edited book (D78); returns the `ChangesPlan` (queued, or done
    when the task ran at once).

    `pages` limits the plan to those pages (default: every drift page); `fix`, a «تصحيح في كل الكتاب» batch
    (D79), plans the batch's pages and settles at once those left with no item (their baseline moves: the
    book text now agrees with review). Refused (400) before a manuscript exists and with `NotEdited` (409)
    when the text was never edited. Idempotent: while a plan of the book is queued or running it is returned.
    The task (`editor.tasks.plan_review_changes`, default queue) runs the pipeline in memory; the manuscript
    is not touched."""
    from .models import ChangesPlan

    manuscript = Manuscript.objects.filter(book_id=book.pk).only("id", "version", "origin").first()
    if manuscript is None:
        raise EditorError(NO_MANUSCRIPT)
    if manuscript.origin != Manuscript.Origin.EDITOR:
        raise NotEdited()
    settle = fix not in (None, "")
    wanted = _batch_pages(book, str(fix)) if settle else _page_numbers(pages)
    with transaction.atomic():
        Book.objects.select_for_update().filter(pk=book.pk).first()
        active = list(
            ChangesPlan.objects.filter(book_id=book.pk, status__in=ChangesPlan.ACTIVE).defer("results")
        )
        lost = [
            plan.pk
            for plan in active
            if plan.created_at and timezone.now() - plan.created_at > _abandoned_after()
        ]
        if lost:
            ChangesPlan.objects.filter(pk__in=lost).update(
                status=ChangesPlan.Status.ERROR, error=PLAN_ABANDONED, finished_at=timezone.now()
            )
        live = [plan for plan in active if plan.pk not in lost]
        if live:
            return live[0]
        plan = ChangesPlan.objects.create(
            book_id=book.pk,
            manuscript_version=manuscript.version,
            status=ChangesPlan.Status.QUEUED,
            pages=wanted,
            results={"settle": settle},
            created_by=_user_or_none(user),
        )
        kept = list(
            ChangesPlan.objects.filter(book_id=book.pk)
            .order_by("-created_at", "-id")
            .values_list("id", flat=True)[ChangesPlan.PLANS_KEPT :]
        )
        if kept:
            ChangesPlan.objects.filter(pk__in=kept).exclude(status__in=ChangesPlan.ACTIVE).delete()
    from .tasks import plan_review_changes as task

    try:
        result = task.delay(plan.pk)
    except Exception as exc:  # noqa: BLE001 - reported on the plan
        log.exception("review changes plan %s of book %s could not be enqueued", plan.pk, book.pk)
        from assembly.services import ENQUEUE_ERROR

        ChangesPlan.objects.filter(pk=plan.pk, status="queued").update(
            status="error",
            error=f"{ENQUEUE_ERROR}\n{type(exc).__name__}: {exc}"[:4000],
            finished_at=timezone.now(),
        )
    else:
        task_id = getattr(result, "id", "") or ""
        if task_id:
            ChangesPlan.objects.filter(pk=plan.pk, task_id="").update(task_id=task_id[:64])
    return ChangesPlan.objects.defer("results").get(pk=plan.pk)


def compute_plan(book: Book, manuscript: Manuscript, wanted: list[int] | None = None) -> tuple[dict, dict]:
    """The comparison itself, in memory (the task's and `manage.py review_changes`'s): the pipeline on the
    book as it is now, the classified drift, then `editor.merge.plan` of the manuscript, its base (stored,
    else none) and the fresh text over the drift pages (those in `wanted`, when given). Returns `(plan,
    results)`:
    what the book page reads (the page rows with their reasons, the items, the counts, the approval-only
    pages, the planned pages' signatures `sigs`) and what only an apply reads (the result nodes, the fresh
    document, the planned lines, warnings and seams). Writes nothing."""
    from assembly import pipeline
    from assembly import services as assembly_services

    from . import merge

    options = pipeline.normalize_settings(book.assembly_settings)
    read = timezone.now()
    loaded = assembly_services.load_book(book)
    result = pipeline.assemble(loaded.pages, options, assembly_services.book_meta(book))
    run = manuscript.run
    rows = [(page.id, page.number, page.status) for page in loaded.pages]
    classified = (
        assembly_services.stale_reasons(run.included, rows, options, finished_at=run.finished_at)
        if run is not None
        else {"pages": [], "reasons": {}, "approvals": []}
    )
    chosen = set(wanted) if wanted is not None else None
    pages = [n for n in classified["pages"] if chosen is None or n in chosen]
    reasons = {n: classified["reasons"][str(n)] for n in pages}
    lines = {line.id: (page.number, line.order) for page in loaded.pages for line in page.lines}
    document = manuscript.document or {}
    items = merge.plan(
        document,
        manuscript.base,
        result.document,
        pages,
        added=[n for n, reason in reasons.items() if reason == "added"],
        lines=lines,
    )
    chapters = doc.chapters_of(document)
    spans = [(chapter.id, doc.chapter_pages(chapter.nodes(document))) for chapter in chapters]
    titles = {chapter.id: chapter.title for chapter in chapters}
    page_rows = []
    for number in pages:
        owners = _chapters_of_page(spans, number)
        page_rows.append(
            {
                "number": number,
                "reason": reasons[number],
                "reason_label": REASON_LABELS[reasons[number]],
                "chapter": owners[0] if owners else None,
                "chapter_title": titles.get(owners[0], "") if owners else "",
                "items": [item["id"] for item in items if item["page"] == number],
            }
        )
    moved = set(pages) | set(classified["approvals"])
    included = set(result.pages)
    sigs = {
        str(page.id): {
            "number": page.number,
            "reviewed": page.reviewed,
            "sig": loaded.signatures.get(page.id, assembly_services.EMPTY_SIGNATURE),
            "at": read.isoformat(),
        }
        for page in loaded.pages
        if page.number in moved and page.id in included
    }
    counts = {"items": len(items), "pages": len(pages)}
    counts.update({kind: sum(1 for item in items if item["kind"] == kind) for kind in merge.DEFAULTS})
    data = {
        "base": "stored" if manuscript.base is not None else "none",
        "pages": page_rows,
        "approvals": list(classified["approvals"]),
        "items": [{k: v for k, v in item.items() if k != "work"} for item in items],
        "counts": counts,
        "applied": None,
        "sigs": sigs,
    }
    results = {
        "items": {item["id"]: item["work"] for item in items},
        "fresh": result.document,
        "lines": {str(k): list(v) for k, v in lines.items() if v[0] in moved},
        "warnings": [w for w in result.warnings if isinstance(w, dict) and w.get("page") in moved],
        "seams": [w for w in result.seams if isinstance(w, dict) and w.get("page") in moved],
    }
    return data, results


def run_changes_plan(plan_id: int):
    """Run a queued plan (the task's job): `compute_plan` for the plan's pages, stored on the plan; a plan of
    a batch (`settle`) then applies «نصّي» to the pages left with no item (and the approval-only pages), so
    their baseline moves. Any failure → error with an Arabic headline, the manuscript untouched. A plan
    that is not queued any more is left alone."""
    from .models import ChangesPlan

    plan = ChangesPlan.objects.filter(pk=plan_id).first()
    if plan is None or plan.status != ChangesPlan.Status.QUEUED:
        return plan
    if not ChangesPlan.objects.filter(pk=plan.pk, status="queued").update(status="running"):
        plan.refresh_from_db()
        return plan
    try:
        book = Book.objects.get(pk=plan.book_id)
        manuscript = manuscript_of(book, with_run=True, with_base=True)
        data, results = compute_plan(book, manuscript, plan.pages)
        results = {**(plan.results or {}), **results}
        ChangesPlan.objects.filter(pk=plan.pk).update(
            status=ChangesPlan.Status.DONE,
            manuscript_version=manuscript.version,
            plan=data,
            results=results,
            error="",
            finished_at=timezone.now(),
        )
        empty = [row["number"] for row in data["pages"] if not row["items"]]
        if results.get("settle") and (empty or data["approvals"]):
            try:
                apply_review_changes(book, plan.pk, {}, empty, plan.created_by, keep_all=True, snapshot=False)
            except EditorError as exc:  # the text moved meanwhile: the pages stay in «تغييرات المراجعة»
                log.info("review changes plan %s of book %s not settled: %s", plan.pk, book.pk, exc)
    except Exception as exc:  # noqa: BLE001 - reported on the plan, the manuscript stays
        log.exception("review changes plan %s of book %s failed", plan.pk, plan.book_id)
        ChangesPlan.objects.filter(pk=plan.pk).update(
            status=ChangesPlan.Status.ERROR,
            error=f"{PLAN_ERROR}\n{type(exc).__name__}: {exc}"[:4000],
            finished_at=timezone.now(),
        )
    plan.refresh_from_db()
    return plan


def plan_payload(plan) -> dict:
    """A plan as the book page reads it (editor/fixtures/contract/changes_plan.json): `{id, status,
    status_label, error (the headline), created_at, finished_at, manuscript_version, requested, base, pages,
    approvals, items, counts, applied}`."""
    data = plan.plan if isinstance(plan.plan, dict) else {}
    error = (plan.error or "").splitlines()
    return {
        "id": plan.pk,
        "status": plan.status,
        "status_label": plan.get_status_display(),
        "error": error[0] if error and plan.status == "error" else "",
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "finished_at": plan.finished_at.isoformat() if plan.finished_at else None,
        "manuscript_version": plan.manuscript_version,
        "requested": plan.pages,
        "base": data.get("base"),
        "pages": list(data.get("pages") or []),
        "approvals": list(data.get("approvals") or []),
        "items": list(data.get("items") or []),
        "counts": dict(data.get("counts") or {}),
        "applied": data.get("applied"),
    }


def _moved_since(sigs: dict) -> list[int]:
    """The planned pages whose signature or reviewed flag moved since the plan (two queries)."""
    from assembly.pipeline import REVIEWED_STATUSES
    from assembly.services import page_signatures

    ids = [int(pk) for pk in sigs if str(pk).isdigit()]
    if not ids:
        return []
    now = page_signatures(ids)
    statuses = dict(Page.objects.filter(pk__in=ids).values_list("pk", "status"))
    moved = []
    for pk in ids:
        info = sigs[str(pk)]
        if now.get(pk) != info.get("sig") or (statuses.get(pk) in REVIEWED_STATUSES) != bool(
            info.get("reviewed")
        ):
            moved.append(int(info.get("number")))
    return sorted(moved)


def review_changes(book: Book) -> dict:
    """`api:review_changes`: `{drift, plan, stale}` — the live drift (`drift_payload`), the newest plan
    (`plan_payload`, or None) and whether it is out of date (the manuscript's version, or a planned page's
    lines or approval, moved since: plan again). The plan's server-side results are never loaded."""
    from .models import ChangesPlan

    drift = drift_payload(review_drift(book))
    plan = ChangesPlan.objects.filter(book_id=book.pk).defer("results").order_by("-created_at", "-id").first()
    if plan is None:
        return {"drift": drift, "plan": None, "stale": False}
    stale = False
    if plan.status == ChangesPlan.Status.DONE:
        version = Manuscript.objects.filter(book_id=book.pk).values_list("version", flat=True).first()
        stale = version != plan.manuscript_version or bool(_moved_since((plan.plan or {}).get("sigs") or {}))
    return {"drift": drift, "plan": plan_payload(plan), "stale": stale}


def _pages_label(pages: list[int]) -> str:
    return "، ".join(str(n) for n in pages[:8]) + ("…" if len(pages) > 8 else "")


def apply_review_changes(
    book: Book,
    plan_id,
    choices,
    pages,
    user,
    keep_all: bool = False,
    snapshot: bool = True,
) -> dict:
    """Take a plan's changes into the edited book (D78), in one transaction on the locked book and
    manuscript.

    `pages` are the page rows taken now (None: all of them); an item applies when its `page` is one of
    them, with its choice from `choices` (item id → theirs | mine | merged; its `default` when left out;
    every choice `mine` with `keep_all`). The approval-only pages come along. Steps: the plan must be fresh
    (`PlanStale`, 409: the manuscript's version, or a page taken whose lines or approval moved); an `edit`
    snapshot «قبل أخذ تغييرات المراجعة · ص …» with the base and the baseline (unless `snapshot` is false);
    `merge.apply` and the document written (version + 1) only when an item changes it; the base moved over
    the pages taken (`merge.splice_base`); one `done` `AssemblyRun` (`settings.scope = "changes"`) whose
    `included` is the manuscript run's with the pages taken at their planned signatures, the warnings and
    seams of those pages the fresh ones, `stats.applied`; D36 for the reviewed pages still at the planned
    signature; the focus chapter's re-layout (D47). Returns `{version, snapshot, chapters: [{id, version,
    title}], reload, relayout, applied: {at, taken, merged, kept, pages, written}}`."""
    from assembly.models import AssemblyRun
    from assembly.pipeline import REVIEWED_STATUSES, normalize_settings

    from . import merge
    from .models import ChangesPlan

    try:
        plan_pk = int(str(plan_id))
    except (TypeError, ValueError):
        raise EditorNotFound(PLAN_MISSING) from None
    asked = _page_numbers(pages)
    if choices is not None and not isinstance(choices, dict):
        raise EditorError(BAD_CHOICE)
    choices = {str(k): v for k, v in (choices or {}).items()}
    now = timezone.now()
    with transaction.atomic():
        Book.objects.select_for_update().filter(pk=book.pk).first()
        plan = ChangesPlan.objects.filter(pk=plan_pk, book_id=book.pk).first()
        if plan is None:
            raise EditorNotFound(PLAN_MISSING)
        if plan.status != ChangesPlan.Status.DONE:
            raise EditorError(PLAN_NOT_READY)
        manuscript = manuscript_of(book, lock=True, with_run=True, with_base=True)
        if manuscript.version != plan.manuscript_version:
            raise PlanStale(STALE_VERSION)
        data, results = plan.plan or {}, plan.results or {}
        rows = [row["number"] for row in data.get("pages") or []]
        taken = set(rows) if asked is None else set(rows) & set(asked)
        if asked is not None and not taken:
            raise EditorError(NO_PAGES_TAKEN)  # an explicit page list that names none of the plan's pages
        approvals = set(data.get("approvals") or [])
        moved = taken | approvals
        sigs = {pk: info for pk, info in (data.get("sigs") or {}).items() if info.get("number") in moved}
        changed = _moved_since(sigs)
        if changed:
            raise PlanStale(_stale_pages_message(changed), changed)
        work = results.get("items") or {}
        items, final = [], {}
        for item in data.get("items") or []:
            if item.get("page") not in taken:
                continue
            choice = merge.MINE if keep_all else choices.get(item["id"], item["default"])
            if choice not in item["choices"]:
                raise EditorError(BAD_CHOICE)
            final[item["id"]] = choice
            items.append({**item, "work": work.get(item["id"]) or {}})
        reviewed = set(
            Page.objects.filter(book_id=book.pk, is_excluded=False, status__in=REVIEWED_STATUSES).values_list(
                "number", flat=True
            )
        )
        document = manuscript.document or {}
        new_document, stats = merge.apply(document, items, final, approvals=approvals, reviewed=reviewed)
        taken_pages = sorted(moved)
        taken_snapshot = None
        if snapshot:
            label = f"قبل أخذ تغييرات المراجعة · ص {_pages_label(sorted(taken) or taken_pages)}"
            if not stats["written"]:
                label = f"قبل الاحتفاظ بنصّي · ص {_pages_label(sorted(taken) or taken_pages)}"
            taken_snapshot = _take(
                manuscript, label, ManuscriptSnapshot.Reason.EDIT, user, run_id=manuscript.run_id
            )
        fresh = results.get("fresh") or {}
        lines = {int(k): tuple(v) for k, v in (results.get("lines") or {}).items()}
        if manuscript.base is None:
            unknown = set(review_drift(book, manuscript)["pages"]) - moved
            new_base = merge.splice_base(None, fresh, moved, unknown=unknown, lines=lines)
        else:
            new_base = merge.splice_base(manuscript.base, fresh, moved, lines=lines)
        base_run = manuscript.run
        included = dict(base_run.included or {}) if base_run is not None else {}
        for key, info in list(included.items()):
            if isinstance(info, dict) and info.get("number") in moved and key not in sigs:
                del included[key]  # a page taken out of the book (`removed`)
        for key, info in sigs.items():
            included[str(key)] = dict(info)
        old_warnings = [
            w for w in (base_run.warnings if base_run is not None else []) or [] if w.get("page") not in moved
        ]
        new_warnings = [w for w in results.get("warnings") or [] if w.get("page") in moved]
        applied = {
            "at": now.isoformat(),
            "taken": stats["taken"],
            "merged": stats["merged"],
            "kept": stats["kept"],
            "pages": taken_pages,
            "written": stats["written"],
        }
        run_stats = dict(base_run.stats or {}) if base_run is not None else {}
        run = AssemblyRun.objects.create(
            book_id=book.pk,
            status=AssemblyRun.Status.DONE,
            stage="save",
            settings={
                **normalize_settings(book.assembly_settings).as_dict(),
                "scope": "changes",
                "plan": plan.pk,
            },
            included=included,
            warnings=sorted(
                [*old_warnings, *new_warnings], key=lambda w: -1 if w.get("page") is None else w["page"]
            ),
            created_by=_user_or_none(user),
            finished_at=now,
        )
        fields = ["base", "run", "updated_by", "updated_at"]
        if stats["written"]:
            attrs = dict(new_document.get("attrs") or {})
            seams = [
                s for s in attrs.get("seams") or [] if isinstance(s, dict) and s.get("page") not in moved
            ]
            attrs["seams"] = sorted(
                [*seams, *(results.get("seams") or [])], key=lambda s: int(s.get("page") or 0)
            )
            attrs["runId"] = run.pk
            new_document["attrs"] = attrs
            manuscript.document = new_document
            manuscript.version += 1
            fields += ["document", "version"]
        manuscript.base = new_base
        manuscript.run = run
        manuscript.updated_by = _user_or_none(user)
        manuscript.save(update_fields=fields)
        run_stats.update(doc.document_stats(manuscript.document or {}))
        run_stats["applied"] = applied
        AssemblyRun.objects.filter(pk=run.pk).update(stats=run_stats)
        from assembly.services import page_signatures

        ready = [int(pk) for pk, info in sigs.items() if info.get("reviewed")]
        current = page_signatures(ready)
        settled = [pk for pk in ready if current.get(pk) == sigs[str(pk)]["sig"]]
        if settled:
            Page.objects.filter(pk__in=settled, status=Page.Status.REVIEWED).update(
                status=Page.Status.ASSEMBLED
            )
        Book.objects.get(pk=book.pk).refresh_status()
        data = dict(data)
        data["applied"] = applied
        ChangesPlan.objects.filter(pk=plan.pk).update(plan=data)
    final_document = manuscript.document or {}
    chapters = doc.chapters_of(final_document)
    relayout = None
    if stats["written"]:
        focus = None
        changed_ids = set(stats["changed"])
        for chapter in chapters:
            if doc.all_ids(chapter.nodes(final_document)) & changed_ids:
                focus = chapter.id
                break
        relayout = _schedule(book, focus, manuscript.version)
    return {
        "version": manuscript.version,
        "snapshot": taken_snapshot.pk if taken_snapshot is not None else None,
        "chapters": [
            {"id": c.id, "version": doc.chapter_version(c.nodes(final_document)), "title": c.title}
            for c in chapters
        ],
        "reload": stats["written"],
        "relayout": relayout,
        "applied": applied,
    }


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
    """`front_matter` with every flag and every book detail present (`fields`: '' when not given) and the
    cover's settings with their defaults (`cover`, mode `none` when the book has none, D80)."""
    front = front if isinstance(front, dict) else {}
    fields = front.get("fields") if isinstance(front.get("fields"), dict) else {}
    out = {**FRONT_FLAGS, **{key: bool(front[key]) for key in FRONT_FLAGS if key in front}}
    out["fields"] = {name: str(fields.get(name) or "") for name in BOOK_FIELDS}
    out["cover"] = cover_settings(front.get("cover"))
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
            **{f"front_matter.cover.{name}": list(pair) for name, pair in COVER_LIMITS.items()},
        },
        "missing_fonts": resolve(sheet.body_font, sheet.latin_font, sheet.heading_font).missing,
        "book_fields": list(BOOK_FIELDS),
        "field_defaults": {"title": book.title, "author": book.author},
        "cover": cover_choices(book, sheet, values["front_matter"]["cover"]),
    }


# ====================================================================== the cover's settings (D80)

COVER_LIMITS: dict[str, tuple[int, int]] = {
    "center": (0, COVER_TEXT_MAX),
    "bottom": (0, COVER_TEXT_MAX),
    "center_pt": (int(COVER_SIZE_LIMITS[0]), int(COVER_SIZE_LIMITS[1])),
    "bottom_pt": (int(COVER_SIZE_LIMITS[0]), int(COVER_SIZE_LIMITS[1])),
    "bottom_mm": (int(COVER_BOTTOM_LIMITS[0]), int(COVER_BOTTOM_LIMITS[1])),
}
COVER_DPI = 300  # the print resolution the size note asks for
COVER_INVALID = "إعدادات الغلاف غير صالحة."
COVER_MODE = "نوع الغلاف غير معروف."
COVER_FIT = "طريقة وضع الصورة غير معروفة."
COVER_IMAGE = "الصورة غير موجودة في هذا الكتاب."
COVER_TOO_LONG = f"النص أطول من المسموح ({COVER_TEXT_MAX} حرفًا)."
COVER_VALUE = "قيمة غير صالحة."
COVER_SIZE = f"الحجم بين {COVER_LIMITS['center_pt'][0]} و{COVER_LIMITS['center_pt'][1]} نقطة."
COVER_COLOUR = "اللون غير صالح؛ يُكتب مثل #1d2433."
COVER_PRESET = "مجموعة الألوان غير معروفة."
COVER_BOTTOM = f"المسافة بين {COVER_LIMITS['bottom_mm'][0]} و{COVER_LIMITS['bottom_mm'][1]} مم."
COVER_SIZE_NOTE = "للطباعة: {dpi} نقطة في البوصة، أي {width} × {height} بكسل على هذا القطع"
COVER_INFO_NOTE = (
    "يُؤخذ العنوان والعنوان الفرعي والمؤلف من «بيانات الكتاب»، والناشر والمدينة والسنة في الأسفل."
)


def print_pixels(width_mm: float, height_mm: float, dpi: int = COVER_DPI) -> tuple[int, int]:
    """The pixels a picture needs to cover a trim at `dpi`: `round(mm / 25.4 × dpi)` per side."""
    return round(width_mm / 25.4 * dpi), round(height_mm / 25.4 * dpi)


def cover_choices(book: Book, sheet: StyleSheet, settings: dict | None = None) -> dict:
    """The stylesheet payload's `cover` block (the «الغلاف» section of «التنسيق»): the modes, the fits and
    the presets with their labels, the defaults and limits, the bottom block's automatic distance (the
    page's bottom margin + 8 mm), the upload's limits, the print-size note for this trim and the pixels
    of every trim preset, the `info` note, and the chosen image (`image_payload`, or None)."""
    settings = settings if settings is not None else cover_settings((sheet.front_matter or {}).get("cover"))
    width, height = print_pixels(sheet.width_mm, sheet.height_mm)
    image = None
    if settings.get("image"):
        row = BookImage.objects.filter(pk=settings["image"], book_id=book.pk).first()
        image = image_payload(row) if row is not None else None
    return {
        "modes": [{"value": key, "label": label} for key, label in COVER_MODES.items()],
        "fits": [{"value": key, "label": label} for key, label in COVER_FITS.items()],
        "presets": [
            {"key": key, "label": label, "background": background, "color": color}
            for key, (label, background, color) in COVER_PRESETS.items()
        ],
        "defaults": dict(COVER_DEFAULTS),
        "limits": {name: list(pair) for name, pair in COVER_LIMITS.items()},
        "bottom_mm_auto": round(float(sheet.bottom_mm) + COVER_BOTTOM_GAP_MM, 2),
        "upload": {
            "field": "file",
            "max_bytes": IMAGE_MAX_BYTES,
            "max_mb": IMAGE_MAX_BYTES // (1024 * 1024),
            "max_side": IMAGE_MAX_SIDE,
            "types": list(IMAGE_TYPES.values()),
            "accept": ",".join(IMAGE_TYPES.values()),
        },
        "print_size": {
            "dpi": COVER_DPI,
            "width_px": width,
            "height_px": height,
            "note": COVER_SIZE_NOTE.format(dpi=COVER_DPI, width=width, height=height),
        },
        "print_sizes": {key: list(print_pixels(w, h)) for key, (_label, w, h) in TRIM_PRESETS.items()},
        "info_note": COVER_INFO_NOTE,
        "image": image,
    }


def _cover_image_id(book: Book, value) -> tuple[int | None, bool]:
    """A posted `image`: `(id, ok)` — None for none; not ok for anything that is not an image of this
    book."""
    if value is None or value == "":
        return None, True
    if isinstance(value, bool):
        return None, False
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or value <= 0:
        return None, False
    return value, BookImage.objects.filter(pk=value, book_id=book.pk).exists()


def _cover_update(book: Book, data: dict, current: dict) -> tuple[dict, dict[str, str]]:
    """The cover's settings with the posted keys applied (`current`: the stored ones, `cover_settings`),
    and the errors per key (`front_matter.cover.<key>`). A `preset` sets both colours, a posted colour
    wins over it, and `preset` then follows the two colours (`preset_of`)."""
    out = dict(current)
    errors: dict[str, str] = {}

    def refuse(key: str, message: str) -> None:
        errors[f"front_matter.cover.{key}"] = message

    if "mode" in data:
        if isinstance(data["mode"], str) and data["mode"] in COVER_MODES:
            out["mode"] = data["mode"]
        else:
            refuse("mode", COVER_MODE)
    if "image" in data:
        image, ok = _cover_image_id(book, data["image"])
        if ok:
            out["image"] = image
        else:
            refuse("image", COVER_IMAGE)
    if "fit" in data:
        if isinstance(data["fit"], str) and data["fit"] in COVER_FITS:
            out["fit"] = data["fit"]
        else:
            refuse("fit", COVER_FIT)
    for key in ("center", "bottom"):
        if key not in data:
            continue
        value = "" if data[key] is None else data[key]
        if not isinstance(value, str):
            refuse(key, COVER_VALUE)
            continue
        text_value = cover_text(value)
        if len(text_value) > COVER_TEXT_MAX:
            refuse(key, COVER_TOO_LONG)
        else:
            out[key] = text_value
    for key in ("center_pt", "bottom_pt"):
        if key in data:
            number = _number(data[key])
            if number is None or not COVER_SIZE_LIMITS[0] <= number <= COVER_SIZE_LIMITS[1]:
                refuse(key, COVER_SIZE)
            else:
                out[key] = round(number, 2)
    colours: dict[str, str] = {}
    for key in ("background", "color"):
        if key in data:
            colour = hex_colour(data[key])
            if colour is None:
                refuse(key, COVER_COLOUR)
            else:
                colours[key] = colour
    if "preset" in data:
        preset = data["preset"]
        if isinstance(preset, str) and preset in COVER_PRESETS:
            _label, out["background"], out["color"] = COVER_PRESETS[preset]
        elif preset != CUSTOM_PRESET:
            refuse("preset", COVER_PRESET)
    out.update(colours)
    if "bottom_mm" in data:
        if data["bottom_mm"] is None or data["bottom_mm"] == "":
            out["bottom_mm"] = None
        else:
            number = _number(data["bottom_mm"])
            if number is None or not COVER_BOTTOM_LIMITS[0] <= number <= COVER_BOTTOM_LIMITS[1]:
                refuse("bottom_mm", COVER_BOTTOM)
            else:
                out["bottom_mm"] = round(number, 2)
    out["preset"] = preset_of(out["background"], out["color"])
    return out, errors


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
    Amiri and the panel says «غير مثبّت»). `front_matter.cover` takes any subset of the cover's keys
    (`_cover_update`, D80). Raises `StyleSheetError` with every bad field. Returns `(stylesheet, changed)`.
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
            had_cover = isinstance((sheet.front_matter or {}).get("cover"), dict)
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
                if "cover" in front:  # D80
                    if not isinstance(front["cover"], dict):
                        errors["front_matter.cover"] = COVER_INVALID
                    else:
                        cover, cover_errors = _cover_update(book, front["cover"], merged["cover"])
                        errors.update(cover_errors)
                        merged["cover"] = cover
                elif not had_cover:
                    del merged["cover"]  # a book that never chose a cover keeps its stored front matter
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


# ====================================================================== the book's images (D80)

IMAGE_MAX_BYTES = 30 * 1024 * 1024
IMAGE_MAX_SIDE = 12_000
IMAGE_TYPES: dict[str, str] = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
IMAGE_JPEG_QUALITY = 92
IMAGE_THUMB_SIDE = 480
IMAGE_THUMB_QUALITY = 80
IMAGE_TOO_LARGE = f"الصورة أكبر من {IMAGE_MAX_BYTES // (1024 * 1024)} ميغابايت؛ اختر صورة أصغر."
IMAGE_UNREADABLE = "تعذّرت قراءة الملف صورةً؛ اختر صورة JPEG أو PNG أو WebP."
IMAGE_WRONG_TYPE = "نوع الصورة غير مقبول؛ المقبول JPEG وPNG وWebP."
IMAGE_TOO_WIDE = f"الصورة أكبر من {IMAGE_MAX_SIDE} بكسل في أحد جانبيها؛ صغّرها ثم ارفعها."
IMAGE_MISSING = "لم تُرسَل صورة."


class ImageRefused(EditorError):
    """An upload that is not a usable image: `code` (`too_large`, `not_an_image`, `wrong_type`,
    `too_many_pixels`, `no_file`, `bad_purpose`) and the HTTP `status` (413, 422 or 400)."""

    def __init__(self, message: str, code: str, status: int):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class NormalisedImage:
    """An image as it is stored (`normalise_image`): the file's bytes, its format (`jpeg` | `png`), its
    pixel size and its WebP thumbnail."""

    data: bytes
    format: str
    width: int
    height: int
    thumb: bytes


def _srgb(image, icc: bytes | None):
    """`image` in sRGB: through its ICC profile when it has one (CMYK, Adobe RGB, Display P3…), else by
    Pillow's own conversion. Grey stays grey; anything with alpha comes back RGBA."""
    from PIL import ImageCms

    mode = image.mode
    if mode == "I" or mode.startswith("I;16"):  # 16-bit grey: to 8 bits
        return image.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    if mode in ("1", "F"):
        return image.convert("L")
    if mode == "L":
        return image
    if mode in ("P", "PA"):
        transparent = mode == "PA" or "transparency" in image.info
        image = image.convert("RGBA" if transparent else "RGB")
        mode = image.mode
    elif mode == "LA":
        image = image.convert("RGBA")
        mode = "RGBA"
    target = "RGBA" if mode in ("RGBA", "RGBa") else "RGB"
    if icc and mode in ("RGB", "RGBA", "CMYK"):
        try:
            source = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            if mode == "RGBA":  # the colour through the profile, the alpha as it is
                alpha = image.getchannel("A")
                converted = ImageCms.profileToProfile(
                    image.convert("RGB"), source, ImageCms.createProfile("sRGB"), outputMode="RGB"
                )
                converted.putalpha(alpha)
                return converted
            return ImageCms.profileToProfile(image, source, ImageCms.createProfile("sRGB"), outputMode="RGB")
        except (ImageCms.PyCMSError, OSError, ValueError, TypeError):
            log.warning("an image's ICC profile could not be applied; converted without it")
    return image.convert(target) if image.mode != target else image


def normalise_image(data: bytes) -> NormalisedImage:
    """Check an uploaded image and give it as it is stored (COVER_SPEC §1.7): JPEG, PNG or WebP (the
    format read from the file, not its name), at most `IMAGE_MAX_SIDE` px a side, readable (Pillow
    `verify`, then a full load); turned upright (EXIF orientation), in sRGB (`_srgb`), then JPEG q92
    (4:4:4, no metadata) when it is opaque — an alpha channel that is all opaque counts as none — or PNG
    when it has transparency, with a WebP thumbnail at most `IMAGE_THUMB_SIDE` px long. Raises
    `ImageRefused`."""
    import warnings

    from PIL import Image, ImageOps, UnidentifiedImageError

    unreadable = ImageRefused(IMAGE_UNREADABLE, "not_an_image", 422)
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format not in IMAGE_TYPES:
                raise ImageRefused(IMAGE_WRONG_TYPE, "wrong_type", 422)
            if max(probe.size) > IMAGE_MAX_SIDE:
                raise ImageRefused(IMAGE_TOO_WIDE, "too_many_pixels", 422)
            probe.verify()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)  # the sides are checked above
            image = Image.open(io.BytesIO(data))
            image.load()
    except ImageRefused:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise unreadable from None
    icc = image.info.get("icc_profile")
    try:
        image = ImageOps.exif_transpose(image) or image
        image = _srgb(image, icc)
    except (OSError, ValueError) as exc:
        raise unreadable from exc
    if image.mode == "RGBA" and image.getchannel("A").getextrema() == (255, 255):
        image = image.convert("RGB")
    out = io.BytesIO()
    if image.mode == "RGBA":
        image.save(out, "PNG", compress_level=6)
        kind = BookImage.Format.PNG
    else:
        image.save(out, "JPEG", quality=IMAGE_JPEG_QUALITY, subsampling=0, optimize=True)
        kind = BookImage.Format.JPEG
    thumb = image.copy()
    thumb.thumbnail((IMAGE_THUMB_SIDE, IMAGE_THUMB_SIDE), Image.Resampling.LANCZOS)
    small = io.BytesIO()
    thumb.save(small, "WEBP", quality=IMAGE_THUMB_QUALITY, method=4)
    return NormalisedImage(out.getvalue(), str(kind), image.width, image.height, small.getvalue())


def image_payload(row: BookImage) -> dict:
    """An image for the book page: `{id, url, thumb_url, width, height, format, source_name}`."""
    from django.core.files.storage import default_storage

    thumb = row.thumb_name
    return {
        "id": row.pk,
        "url": default_storage.url(row.file.name),
        "thumb_url": default_storage.url(thumb if default_storage.exists(thumb) else row.file.name),
        "width": row.width,
        "height": row.height,
        "format": row.format,
        "source_name": row.source_name,
    }


def upload_payload(row: BookImage) -> dict:
    """`api:book_images`'s answer: `image_payload` with the purpose and the sha256."""
    return {**image_payload(row), "purpose": row.purpose, "sha256": row.sha256}


def _read_upload(upload) -> bytes:
    """The uploaded file's bytes, at most `IMAGE_MAX_BYTES` (else `ImageRefused` 413)."""
    size = getattr(upload, "size", None)
    if isinstance(size, int) and size > IMAGE_MAX_BYTES:
        raise ImageRefused(IMAGE_TOO_LARGE, "too_large", 413)
    try:
        upload.seek(0)
    except (AttributeError, OSError):
        pass
    data = upload.read(IMAGE_MAX_BYTES + 1)
    if len(data) > IMAGE_MAX_BYTES:
        raise ImageRefused(IMAGE_TOO_LARGE, "too_large", 413)
    return data


def _write_once(name: str, data: bytes) -> None:
    """Write `data` at the storage name `name` unless a file is already there (files named by their
    content never change)."""
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    if default_storage.exists(name):
        return
    saved = default_storage.save(name, ContentFile(data))
    if saved != name:  # another upload wrote the same name meanwhile: the same bytes, keep one
        default_storage.delete(saved)


def upload_image(book: Book, upload, purpose: str | None = None, user=None) -> tuple[BookImage, bool]:
    """Store an uploaded image of `book` (`POST api:book_images`, D80): read at most 30 MB, check and
    normalise it (`normalise_image`), write `books/<id>/images/<sha256>.<ext>` and its thumbnail, and
    record the `BookImage` (`purpose` cover | body, default cover). The same stored bytes uploaded again
    give the existing row. Returns `(row, created)`; raises `ImageRefused`."""
    if upload is None:
        raise ImageRefused(IMAGE_MISSING, "no_file", 400)
    purpose = purpose or BookImage.Purpose.COVER
    if purpose not in BookImage.Purpose.values:
        raise ImageRefused(COVER_VALUE, "bad_purpose", 400)
    image = normalise_image(_read_upload(upload))
    sha = hashlib.sha256(image.data).hexdigest()
    extension = "jpg" if image.format == BookImage.Format.JPEG else "png"
    name = f"books/{book.pk}/images/{sha}.{extension}"
    _write_once(name, image.data)
    _write_once(f"books/{book.pk}/images/{sha}-thumb.webp", image.thumb)
    existing = BookImage.objects.filter(book_id=book.pk, sha256=sha).first()
    if existing is not None:
        return existing, False
    source = os.path.basename(str(getattr(upload, "name", "") or ""))[:255]
    try:
        with transaction.atomic():
            row = BookImage.objects.create(
                book=book,
                file=name,
                sha256=sha,
                width=image.width,
                height=image.height,
                format=image.format,
                source_name=source,
                purpose=purpose,
                uploaded_by=_user_or_none(user),
            )
    except IntegrityError:  # the same image uploaded at the same moment
        return BookImage.objects.get(book_id=book.pk, sha256=sha), False
    return row, True


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
PLAN_SLOT = "__pid__"


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
        # 7c: «تغييرات المراجعة» (D78), «تحويل إلى حاشية» (D74) and the stage bar (D76)
        "reviewChanges": reverse("api:review_changes", args=[book.pk]),
        "reviewChangesApply": reverse("api:review_changes_apply", args=[book.pk, 0]).replace(
            "/0/", f"/{PLAN_SLOT}/"
        ),
        "toFootnote": reverse("api:to_footnote", args=[book.pk]),
        "stages": reverse("api:book_stages", args=[book.pk]),
        # D80: the cover sheet of the stage and the filmstrip, and the image upload
        "cover": reverse("api:cover", args=[book.pk]),
        "bookImages": reverse("api:book_images", args=[book.pk]),
    }


AUTOSAVE_MS = 1500
RELAYOUT_MS = 500  # the pause after typing on a live page before the chapter is saved and re-laid-out
MODES: tuple[str, ...] = ("preview", "edit")
# the side panel's tabs (`static/src/js/book/panel.js` TABS): `?tab=` opens the book page on one of them
PANEL_TABS: tuple[str, ...] = (
    "chapters",
    "pages",
    "find",
    "format",
    "block",
    "source",
    "uncertain",
    "changes",
)
_RE_BLOCK = re.compile(r"[hpn][0-9]+(?:-[0-9]+)?|e[0-9]+")


def _find_prefill(find: dict | None) -> dict | None:
    """The book page's find & replace, filled in from review (`?q=&r=&fix=`, D79); None without a query."""
    find = find if isinstance(find, dict) else {}
    query = " ".join(str(find.get("q") or "").split())[:MAX_QUERY]
    if not query:
        return None
    batch = str(find.get("fix") or "")
    return {
        "query": query,
        "replacement": " ".join(str(find.get("r") or "").split())[:MAX_QUERY],
        "fix": batch if _RE_BATCH.fullmatch(batch) else None,
    }


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
    block: str | None = None,
    find: dict | None = None,
) -> dict:
    """What the book page (`bookLayout`, D47) and the old editor page start from: the book, the chapter to
    open (the requested one when it exists, else the first), the chapters' order, whether the user may
    edit, the stylesheet with its faces and every URL (`editor_urls`).

    For the book page (`page="layout"`) also: `mode` (`preview` | `edit`), the chapter summaries (versions,
    words, pages, drift: `chapter_summaries`), the review drift, the uncertain words' count, the browser
    `@font-face` rules of the live pages (`fontCss`), the pause before a re-layout (`relayoutMs`) and
    `tab`, the panel tab asked for with `?tab=` (one of `PANEL_TABS`, else None: the panel opens the tab
    remembered for the mode; the export page's readiness links use it, PHASE6_SPEC §6.5). 7c: `block`, the
    block asked for with `?block=` when it is in the document (the page opens on its chapter, lands on its
    page and lights it), and `findPrefill` `{query, replacement, fix}` for `?tab=find&q=&r=&fix=` (review's
    «تصحيح في كل الكتاب» on an edited book, D79), else None."""
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
    landing = None
    if isinstance(block, str) and _RE_BLOCK.fullmatch(block):
        for chapter in chapters:
            if block in doc.all_ids(chapter.nodes(document)):
                landing = block
                if chapter_id not in ids:
                    chapter_id = chapter.id
                break
    current = chapter_id if chapter_id in ids else (ids[0] if ids else None)
    sheet = stylesheet_for(book)
    extra: dict = {}
    if page == "layout":
        from publishing.fonts import browser_font_css, resolve

        from .uncertain import count as uncertain_count

        drift = review_drift(book) if manuscript is not None else copy.deepcopy(NO_DRIFT)
        extra = {
            "mode": mode if mode in MODES else "preview",
            "chapterSummaries": chapter_summaries(book) if manuscript is not None else [],
            "drift": drift_payload(drift),
            "block": landing,
            "findPrefill": _find_prefill(find) if tab == "find" else None,
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
