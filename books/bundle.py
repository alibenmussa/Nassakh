"""Move finished books from one Nassakh database to another: the demo bundle (docs/DEMO_DATA.md).

`export_bundle` writes a directory with everything the app shows for some books; `import_bundle` reads it
into another database as a copy that belongs to one account. The two management commands
(`export_demo_bundle`, `import_demo_bundle`) are thin; everything lives here.

**The bundle.**

- `data.json`: `{"header": {…}, "objects": [ … ]}` with one object per line, in Django's serializer shape
  (`model`, `pk`, `fields`) plus `_book` (the source book the row belongs to). Dates keep their microseconds
  (Django's own JSON encoder cuts them to milliseconds, and a page's review signature
  `assembly.services.page_signature` is built from `Line.updated_at`, so a truncated stamp would make every
  page look edited).
- `manifest.json`: every media file with its size (the import checks them before it writes anything).
- `media/books/<id>/…` and `media/orgs/<org>/fonts/…`: the files, at their paths in the source `MEDIA_ROOT`.

**What travels.** Everything that cascades from `Book` and that the app needs to show or use the book
(`TRAVEL`), the organisation faces its stylesheet names (`org-<pk>`), and the files those rows point at. What
does not travel is listed with its reason (`LEFT_OUT`, `NOT_EXPORTED_REFERENCES`, `LEFT_OUT_MEDIA`); a model
that cascades from `Book` and is in neither list stops the export, so a new table is never forgotten.

**Fresh keys.** The import gives every row a new primary key and remaps every foreign key (`IdMap`). Ids kept
inside JSON are remapped by the registry `JSON_REWRITERS`: the manuscript's `sourceLineIds`, its block and
note ids (`p<first line id>`, `h…`, `n…`), `bookId` / `runId`, the review history's snapshots, the
assembly runs' `included` page ids, the cover's image, the layout files' render ids. A JSONField that is
in neither `JSON_REWRITERS` nor `JSON_WITHOUT_IDS` fails the test suite, so a new field is looked at when it
appears.
Source page *numbers* (`sourcePages`, `sourcePage`, `seams`) are not ids: pages keep their numbers.
"""

from __future__ import annotations

import datetime
import decimal
import json
import os
import re
import shutil
import subprocess
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import serializers
from django.db import connection, models, transaction
from django.db.migrations.recorder import MigrationRecorder
from django.db.models.deletion import get_candidate_relations_to_delete

FORMAT_VERSION = 1
FORMAT_NAME = "nassakh-demo-bundle"
DATA_FILE = "data.json"
MANIFEST_FILE = "manifest.json"
MEDIA_DIR = "media"
DEFAULT_OUT = "~/nassakh-demo-bundle"
DEMO_ACCOUNT_NAME = "حساب التجربة"
CHUNK = 2000

ROOT = "books.book"
FONT = "accounts.organizationfont"
ORGANIZATION = "accounts.organization"

Log = Callable[[str], None]


class BundleError(Exception):
    """A bundle cannot be written or read; the message (English) says what to do."""


# ====================================================================== what travels

#: Models that cascade from `Book` and travel with it, with a word on what the app uses them for.
TRAVEL: dict[str, str] = {
    "books.page": "the pages (numbers, status, text, review state)",
    "processing.preprocess": "each page's prepared images (paths) and line boxes",
    "processing.layoutguides": "the book's layout guides",
    "processing.region": "the regions of each page",
    "ocr.ocrrun": "the OCR runs (raw and parsed output of each engine)",
    "ocr.line": "the reviewed lines with their words",
    "ocr.textgap": "the suggestions of skipped words",
    "review.linerevision": "the review history (undo)",
    "assembly.assemblyrun": "the assembly runs the manuscript and its staleness checks read",
    "editor.manuscript": "the manuscript",
    "editor.manuscriptsnapshot": "the manuscript's saved versions",
    "editor.stylesheet": "the book's physical form (trim, faces, front matter, cover)",
    "editor.bookimage": "the cover and body pictures",
    "publishing.previewrender": "the page renders the book page shows (only the live ones)",
    "publishing.livelayout": "the layout the book page draws",
}

#: Models that cascade from `Book` (or one of its pages) and stay behind, with the reason.
LEFT_OUT: dict[str, str] = {
    "research.pagetext": "rebuilt from the lines by `research.index` (the import runs it)",
    "accounts.quotahold": "a page queued for reading: runtime state, and the demo account is unlimited",
    "publishing.export": "a Word / PDF / EPUB file made on the source machine (the files are left out too)",
    "editor.changesplan": "a cached comparison of review changes; the app plans it again on demand",
}

#: Rows that point at a travelling row without cascading (they belong to someone else), with the reason.
NOT_EXPORTED_REFERENCES: dict[str, str] = {
    "accounts.quotaentry.book": "the ledger of the account that paid for the reading, not the book's",
    "accounts.quotaentry.page": "the ledger of the account that paid for the reading, not the book's",
    "ocr.remotecall.page": "the API log of the GPU endpoint",
    "accounts.styletemplate.source_book": "a format template of the source organisation; no book uses it",
}

#: Media a book's folder holds that the bundle leaves out, by the first path part (inside `books/<id>/`).
LEFT_OUT_MEDIA: dict[str, str] = {
    "source.pdf": "source PDF (only the re-ingest path reads it; --include-pdf keeps it)",
    "exports": "exports (Word / PDF / EPUB files of the source machine)",
    "clips": "research clip cache (made again on the first request)",
    "preview": "preview renders that are not the live layout's (old and cached)",
    "layout": "older live-layout revisions (only the current one is read)",
    "cover": "older cover renders (only the newest complete one is shown)",
    "pages": "page files no row points at",
    "images": "picture files no row points at",
}

# Statuses of rows still running when the book was exported: they come back as errors, never as work.
ACTIVE_RUNS = ("queued", "running")
INTERRUPTED = "Not finished when the book was exported."


# ====================================================================== the graph


def _chunks(values: Iterable, size: int = CHUNK) -> Iterator[list]:
    batch: list = []
    for value in values:
        batch.append(value)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _label(model: type[models.Model]) -> str:
    return model._meta.label_lower


def collect_graph(book_id: int) -> dict[str, set[int]]:
    """Every row that cascades from the book (`Book` → pages → …), as `{model label: {pk}}`.

    The same walk as Django's deletion `Collector` (`on_delete=CASCADE` relations, hidden ones included), but
    by primary keys and without loading rows, so the models can be classified before anything is read.
    """
    book = apps.get_model(ROOT)
    found: dict[type[models.Model], set[int]] = {book: {book_id}}
    frontier: dict[type[models.Model], set[int]] = {book: {book_id}}
    while frontier:
        following: dict[type[models.Model], set[int]] = {}
        for model, pks in frontier.items():
            for rel in get_candidate_relations_to_delete(model._meta):
                if rel.on_delete is not models.CASCADE:
                    continue
                child = rel.related_model
                got: set[int] = set()
                for chunk in _chunks(sorted(pks)):
                    got.update(
                        child._base_manager.filter(**{f"{rel.field.name}__in": chunk}).values_list(
                            "pk", flat=True
                        )
                    )
                new = got - found.get(child, set())
                if new:
                    found.setdefault(child, set()).update(new)
                    following.setdefault(child, set()).update(new)
        frontier = following
    return {_label(model): pks for model, pks in found.items()}


def classify(graph: dict[str, set[int]]) -> None:
    """Raise when the graph holds a model that neither travels nor is left out on purpose."""
    unknown = sorted(set(graph) - {ROOT} - set(TRAVEL) - set(LEFT_OUT))
    if unknown:
        raise BundleError(
            f"{', '.join(unknown)} cascade from Book but are not classified in books.bundle: "
            "add each to TRAVEL (it travels) or LEFT_OUT (with the reason) before exporting."
        )


#: Ids a model's JSON holds (the new ones are needed to write it) without a foreign key to say so: the
#: models whose rows must be written first. (`books.book`'s own `assembly_settings` is finished last.)
JSON_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "assembly.assemblyrun": ("books.page", "ocr.line"),
    "editor.manuscript": ("ocr.line", "assembly.assemblyrun"),
    "editor.manuscriptsnapshot": ("ocr.line", "assembly.assemblyrun"),
    "editor.stylesheet": ("editor.bookimage",),
    "publishing.livelayout": ("ocr.line",),
    "publishing.previewrender": ("ocr.line",),
    "review.linerevision": ("ocr.line", "ocr.textgap", "processing.region"),
}


def travelling_labels() -> list[str]:
    """The labels of every model a bundle holds (the book first), in import order: a row's foreign keys, and
    the ids its JSON holds (`JSON_DEPENDENCIES`), point at rows written before it."""
    labels = [ROOT, *TRAVEL]
    deps: dict[str, set[str]] = {label: set() for label in labels}
    for label in labels:
        for f in apps.get_model(label)._meta.concrete_fields:
            if f.is_relation:
                target = _label(f.remote_field.model)
                if target in deps and target != label:
                    deps[label].add(target)
        deps[label].update(JSON_DEPENDENCIES.get(label, ()))
    order: list[str] = []
    ready = sorted(label for label, need in deps.items() if not need)
    while ready:
        label = ready.pop(0)
        order.append(label)
        for other, need in deps.items():
            if label in need:
                need.discard(label)
                if not need and other not in order and other not in ready:
                    ready.append(other)
        ready.sort()
    if len(order) != len(labels):
        raise BundleError(
            "The foreign keys of the travelling models form a cycle; books.bundle needs a rule."
        )
    return order


def validate_relations() -> None:
    """Every foreign key of a travelling model points at a travelling model, a user or the organisation."""
    user = get_user_model()
    labels = {ROOT, FONT, *TRAVEL}
    for label in labels:
        for f in apps.get_model(label)._meta.concrete_fields:
            if not f.is_relation:
                continue
            target = f.remote_field.model
            if target is user or _label(target) in labels or _label(target) == ORGANIZATION:
                continue
            raise BundleError(
                f"{label}.{f.name} points at {_label(target)}, which the bundle does not carry."
            )


# ====================================================================== plans


@dataclass
class BookPlan:
    """What one book contributes to a bundle."""

    book_id: int
    title: str
    author: str
    source_page_count: int
    status: str
    rows: dict[str, list[int]]
    media: dict[str, int]  # path under MEDIA_ROOT → size
    left_out_rows: dict[str, int] = field(default_factory=dict)
    left_out_media: dict[str, list[int]] = field(default_factory=dict)  # reason → [files, bytes]
    kept_renders: int = 0
    dropped_renders: int = 0
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {label: len(pks) for label, pks in self.rows.items()}


@dataclass
class ExportPlan:
    books: list[BookPlan]
    fonts: list[int]
    font_media: dict[str, int]
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        total: Counter = Counter()
        for book in self.books:
            total.update(book.counts())
        if self.fonts:
            total[FONT] += len(self.fonts)
        return dict(sorted(total.items()))

    def media_totals(self) -> tuple[int, int]:
        sizes = [size for book in self.books for size in book.media.values()] + list(self.font_media.values())
        return len(sizes), sum(sizes)


def media_root() -> Path:
    return Path(settings.MEDIA_ROOT)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def layout_render_ids(data) -> set[int]:
    """The ids of the renders a layout file names (its own, and the one each page's image comes from)."""
    out: set[int] = set()
    if not isinstance(data, dict):
        return out
    if isinstance(data.get("render"), int) and not isinstance(data.get("render"), bool):
        out.add(data["render"])
    for page in data.get("pages") or []:
        src = page.get("src") if isinstance(page, dict) else None
        if isinstance(src, dict) and isinstance(src.get("render"), int):
            out.add(src["render"])
    return out


def _walk_files(folder: Path) -> Iterator[Path]:
    for base, _dirs, names in os.walk(folder):
        for name in names:
            yield Path(base, name)


def _newest_cover_folder(book_id: int) -> str | None:
    """The cover render the shelf shows (`books.shelf._rendered_cover`): the newest complete folder."""
    root = media_root() / "books" / str(book_id) / "cover"
    try:
        folders = [entry for entry in os.scandir(root) if entry.is_dir()]
    except OSError:
        return None
    folders.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)
    for entry in folders:
        if os.path.isfile(os.path.join(entry.path, "cover.pdf")) and os.path.isfile(
            os.path.join(entry.path, "cover.webp")
        ):
            return f"books/{book_id}/cover/{entry.name}"
    return None


def _prune_renders(book_id: int, render_pks: set[int], warnings: list[str]) -> tuple[set[int], str | None]:
    """The renders of the book the app still shows: the live layout's base and every render its file's pages
    take their image from, and the newest finished book render. Returns `(pks to keep, the live layout's
    file when it is not inside a kept render's folder)`."""
    from publishing.models import LiveLayout, PreviewRender

    keep: set[int] = set()
    live = LiveLayout.objects.filter(book_id=book_id).first()
    if live is not None and live.revision and live.path:
        if live.base_id:
            keep.add(live.base_id)
        data = _read_json(media_root() / live.path)
        if data is None:
            warnings.append(
                f"the live layout file {live.path} cannot be read: the book page lays it out again"
            )
        else:
            keep |= layout_render_ids(data)
    newest = (
        PreviewRender.objects.filter(
            book_id=book_id,
            scope=PreviewRender.Scope.BOOK,
            kind=PreviewRender.Kind.PAGES,
            status=PreviewRender.Status.DONE,
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if newest is not None:
        keep.add(newest.pk)
    keep &= render_pks
    return keep, (live.path if live is not None and live.revision and live.path else None)


def _book_rows_and_media(book, include_pdf: bool) -> BookPlan:
    book_id = book.pk
    graph = collect_graph(book_id)
    classify(graph)
    warnings: list[str] = []
    rows = {label: sorted(graph[label]) for label in [ROOT, *TRAVEL] if label in graph}
    left_out_rows = {label: len(graph[label]) for label in LEFT_OUT if label in graph}
    from publishing.models import PreviewRender

    keep, live_path = _prune_renders(book_id, set(rows.get("publishing.previewrender", [])), warnings)
    dropped = len(rows.get("publishing.previewrender", [])) - len(keep)
    if keep:
        rows["publishing.previewrender"] = sorted(keep)
    else:
        rows.pop("publishing.previewrender", None)
    renders = {
        row.pk: row
        for row in PreviewRender.objects.filter(pk__in=keep).only("id", "folder", "status", "images")
    }
    unfinished = [pk for pk, row in renders.items() if row.status != PreviewRender.Status.DONE]
    if unfinished:
        warnings.append(f"render(s) {unfinished} are not finished; they travel as they are")

    wanted: set[str] = set()
    root = media_root()
    for label in [ROOT, *TRAVEL]:
        if label not in rows:
            continue
        model = apps.get_model(label)
        for f in model._meta.concrete_fields:
            if not isinstance(f, models.FileField):
                continue
            if label == ROOT and f.name == "source_pdf" and not include_pdf:
                continue
            for chunk in _chunks(rows[label]):
                wanted.update(
                    name
                    for name in model._base_manager.filter(pk__in=chunk).values_list(f.name, flat=True)
                    if name
                )
    if "editor.bookimage" in rows:
        from editor.models import BookImage

        wanted.update(image.thumb_name for image in BookImage.objects.filter(pk__in=rows["editor.bookimage"]))
    for render in renders.values():
        if not render.folder:
            continue
        folder = root / render.folder
        if not folder.is_dir():
            warnings.append(f"render {render.pk}: the folder {render.folder} is missing")
            continue
        wanted.update(path.relative_to(root).as_posix() for path in _walk_files(folder))
    if live_path:
        wanted.add(live_path)
    cover = _newest_cover_folder(book_id)
    if cover:
        wanted.update(path.relative_to(root).as_posix() for path in _walk_files(root / cover))

    media: dict[str, int] = {}
    missing: list[str] = []
    for name in sorted(wanted):
        path = root / name
        if path.is_file():
            media[name] = path.stat().st_size
        else:
            missing.append(name)
    if missing:
        sample = ", ".join(missing[:3])
        warnings.append(f"{len(missing)} file(s) the rows name are missing on disk (e.g. {sample})")

    left_out_media: dict[str, list[int]] = {}
    own = root / "books" / str(book_id)
    if own.is_dir():
        for path in _walk_files(own):
            name = path.relative_to(root).as_posix()
            if name in media:
                continue
            first = path.relative_to(own).parts[0]
            reason = LEFT_OUT_MEDIA.get(first, "other files")
            entry = left_out_media.setdefault(reason, [0, 0])
            entry[0] += 1
            entry[1] += path.stat().st_size

    active = [
        label
        for label in ("assembly.assemblyrun",)
        if label in rows
        and apps.get_model(label)._base_manager.filter(pk__in=rows[label], status__in=ACTIVE_RUNS).exists()
    ]
    if active:
        warnings.append("an assembly run was still running: it comes back as an error")
    if book.status in ("processing", "ocr"):
        warnings.append(f"the book is «{book.status}»: it may still be changing on the source machine")
    return BookPlan(
        book_id=book_id,
        title=book.title,
        author=book.author,
        source_page_count=book.source_page_count,
        status=book.status,
        rows=rows,
        media=media,
        left_out_rows=left_out_rows,
        left_out_media=left_out_media,
        kept_renders=len(keep),
        dropped_renders=dropped,
        warnings=warnings,
    )


def _font_ids(plans: list[BookPlan]) -> list[int]:
    from editor.models import StyleSheet
    from publishing.fonts import org_font_id

    ids: set[int] = set()
    sheets = StyleSheet.objects.filter(
        pk__in=[pk for plan in plans for pk in plan.rows.get("editor.stylesheet", [])]
    )
    for sheet in sheets:
        for role in ("body_font", "latin_font", "heading_font"):
            pk = org_font_id(getattr(sheet, role, ""))
            if pk is not None:
                ids.add(pk)
    return sorted(ids)


def plan_export(book_ids: list[int], *, include_pdf: bool = False) -> ExportPlan:
    """Read the books and decide what the bundle holds; writes nothing."""
    validate_relations()
    book_model = apps.get_model(ROOT)
    ids = list(dict.fromkeys(book_ids))
    books = {book.pk: book for book in book_model._base_manager.filter(pk__in=ids)}
    missing = [pk for pk in ids if pk not in books]
    if missing:
        raise BundleError(f"No book with id {', '.join(map(str, missing))}.")
    plans = [_book_rows_and_media(books[pk], include_pdf) for pk in ids]

    font_model = apps.get_model(FONT)
    warnings: list[str] = []
    fonts: list[int] = []
    font_media: dict[str, int] = {}
    root = media_root()
    for pk in _font_ids(plans):
        font = font_model._base_manager.filter(pk=pk).first()
        if font is None:
            warnings.append(
                f"a stylesheet names the organisation face org-{pk}, which does not exist: Amiri is used"
            )
            continue
        fonts.append(pk)
        for style in ("regular", "bold", "italic", "bold_italic"):
            name = getattr(font, style).name
            if not name:
                continue
            path = root / name
            if path.is_file():
                font_media[name] = path.stat().st_size
            else:
                warnings.append(f"the file {name} of the face «{font.name}» is missing on disk")
    return ExportPlan(books=plans, fonts=fonts, font_media=font_media, warnings=warnings)


# ====================================================================== writing the bundle


class _Encoder(json.JSONEncoder):
    """Dates with their microseconds (Django's `DjangoJSONEncoder` keeps milliseconds only)."""

    def default(self, o):
        if isinstance(o, datetime.datetime | datetime.date | datetime.time):
            return o.isoformat()
        if isinstance(o, decimal.Decimal | uuid.UUID):
            return str(o)
        if isinstance(o, datetime.timedelta):
            return o.total_seconds()
        return super().default(o)


def _dumps(value) -> str:
    return json.dumps(value, cls=_Encoder, ensure_ascii=False, separators=(",", ":"))


def latest_migrations() -> dict[str, str]:
    """The newest applied migration of each app a bundle's models belong to."""
    labels = {ROOT, FONT, *TRAVEL}
    app_labels = sorted({label.split(".")[0] for label in labels})
    applied = MigrationRecorder(connection).applied_migrations()
    out: dict[str, str] = {}
    for app in app_labels:
        names = sorted(name for (label, name) in applied if label == app)
        if names:
            out[app] = names[-1]
    return out


def check_out_dir(out: Path) -> Path:
    """The bundle's directory; refuses one inside this repository or any git work tree (the images are
    copyrighted scans: a bundle must never reach a commit)."""
    path = Path(out).expanduser().resolve()
    base = Path(settings.BASE_DIR).resolve()
    if path == base or base in path.parents:
        raise BundleError(
            f"--out {path} is inside the repository {base}: the bundle holds copyrighted scans and must "
            "never be committed. Choose a directory outside it (the default is ~/nassakh-demo-bundle)."
        )
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if shutil.which("git"):
        found = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
        if found.returncode == 0 and found.stdout.strip() == "true":
            raise BundleError(
                f"--out {path} is inside a git work tree; choose a directory outside any repository."
            )
    return path


def _rows_of(label: str, pks: list[int], book_id: int | None) -> Iterator[str]:
    """One line per row: `{"_book": <source book or null>, "model": …, "pk": …, "fields": {…}}` (the reader
    takes the book and the model from the start of the line without parsing the row)."""
    model = apps.get_model(label)
    for chunk in _chunks(pks):
        queryset = model._base_manager.filter(pk__in=chunk).order_by("pk")
        for item in serializers.serialize("python", queryset):
            yield _dumps({"_book": book_id, **item})


def _summary_header(plan: ExportPlan) -> dict:
    files, size = plan.media_totals()
    return {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "migrations": latest_migrations(),
        "books": [
            {
                "id": book.book_id,
                "title": book.title,
                "author": book.author,
                "source_page_count": book.source_page_count,
                "status": book.status,
                "counts": book.counts(),
                "media_files": len(book.media),
                "media_bytes": sum(book.media.values()),
                "left_out_rows": book.left_out_rows,
                "dropped_renders": book.dropped_renders,
                "warnings": book.warnings,
            }
            for book in plan.books
        ],
        "fonts": len(plan.fonts),
        "counts": plan.counts(),
        "media_files": files,
        "media_bytes": size,
        "left_out_models": LEFT_OUT,
        "warnings": plan.warnings,
    }


def write_bundle(plan: ExportPlan, out: Path, log: Log = lambda line: None) -> Path:
    """Copy the files, write `manifest.json` and then `data.json` (its presence means the bundle is whole)."""
    out.mkdir(parents=True, exist_ok=True)
    root = media_root()
    manifest: dict[str, int] = {}
    for book in plan.books:
        for name, size in book.media.items():
            manifest[name] = size
    manifest.update(plan.font_media)
    for name in manifest:
        target = out / MEDIA_DIR / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, target)
        if target.stat().st_size != manifest[name]:
            raise BundleError(f"{name} changed while it was being copied; run the export again.")
    (out / MANIFEST_FILE).write_text(
        _dumps({"format": FORMAT_NAME, "version": FORMAT_VERSION, "files": manifest}), encoding="utf-8"
    )
    header = _summary_header(plan)
    temporary = out / f"{DATA_FILE}.partial"
    written: Counter = Counter()
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write('{"header": ' + _dumps(header) + ",\n")
        handle.write('"objects": [\n')
        first = True

        def emit(lines: Iterator[str], label: str) -> None:
            nonlocal first
            for line in lines:
                handle.write(("" if first else ",\n") + line)
                first = False
                written[label] += 1

        if plan.fonts:
            emit(_rows_of(FONT, plan.fonts, None), FONT)
        for book in plan.books:
            for label in travelling_labels():
                if label in book.rows:
                    emit(_rows_of(label, book.rows[label], book.book_id), label)
        handle.write("\n]}\n")
    if dict(written) != plan.counts():
        temporary.unlink(missing_ok=True)
        raise BundleError("The rows changed while they were being exported (counts differ); run it again.")
    temporary.replace(out / DATA_FILE)
    log(f"wrote {out / DATA_FILE} ({_mb(os.path.getsize(out / DATA_FILE))} MB)")
    return out


def _mb(size: int) -> str:
    return f"{size / 1_000_000:,.1f}"


def export_bundle(
    book_ids: list[int],
    out: Path | str = DEFAULT_OUT,
    *,
    dry_run: bool = False,
    force: bool = False,
    include_pdf: bool = False,
    log: Log = lambda line: None,
) -> ExportPlan:
    """Export the books to `out`. Reads the database and the media folder, never writes to them."""
    target = check_out_dir(Path(out))
    used = not dry_run and target.exists() and any(target.iterdir())
    if used and not force:
        raise BundleError(
            f"{target} is not empty. Choose another --out, or add --force to replace an earlier bundle."
        )
    plan = plan_export(book_ids, include_pdf=include_pdf)
    log_plan(plan, log)
    if not dry_run:
        if used:  # only what an earlier bundle made, and only once this export has a plan
            for item in (target / DATA_FILE, target / MANIFEST_FILE, target / MEDIA_DIR):
                if item.is_dir():
                    shutil.rmtree(item)
                elif item.exists():
                    item.unlink()
        write_bundle(plan, target, log)
    return plan


def log_plan(plan: ExportPlan, log: Log) -> None:
    """The summary of an export: rows per model, files, sizes, and what is left out and why."""
    for book in plan.books:
        log(f"book {book.book_id} «{book.title}» ({book.status}, {book.source_page_count} source pages)")
        for label, count in sorted(book.counts().items()):
            log(f"    {label:<28} {count:>7}")
        for label, count in sorted(book.left_out_rows.items()):
            log(f"    {label:<28} {count:>7}  left out: {LEFT_OUT[label]}")
        log(
            f"    files {len(book.media)} · {_mb(sum(book.media.values()))} MB "
            f"(renders kept {book.kept_renders}, old renders dropped {book.dropped_renders})"
        )
        by_kind: Counter = Counter()
        for name, size in book.media.items():
            by_kind[_media_kind(name)] += size
        for kind, size in sorted(by_kind.items(), key=lambda item: -item[1]):
            log(f"        {kind:<22} {_mb(size):>9} MB")
        for reason, (files, size) in sorted(book.left_out_media.items(), key=lambda item: -item[1][1]):
            log(f"    left out: {reason}: {files} file(s), {_mb(size)} MB")
        for warning in book.warnings:
            log(f"    warning: {warning}")
    if plan.fonts:
        log(f"organisation faces: {len(plan.fonts)} ({_mb(sum(plan.font_media.values()))} MB)")
    files, size = plan.media_totals()
    log("totals by model:")
    for label, count in plan.counts().items():
        log(f"    {label:<28} {count:>7}")
    log(f"media: {files} file(s), {_mb(size)} MB")
    for warning in plan.warnings:
        log(f"warning: {warning}")


def _media_kind(name: str) -> str:
    parts = name.split("/")
    if parts[0] == "orgs":
        return "organisation faces"
    if len(parts) >= 4 and parts[2] == "pages":
        return f"pages/{parts[4]}" if len(parts) > 4 else "pages"
    if len(parts) >= 3:
        return parts[2]
    return "other"


# ====================================================================== remapping ids


BLOCK_ID = re.compile(r"(fn-)?([hpn])(\d+)((?:-\d+)?)")
OBJECT_START = re.compile(r'\{"_book":(null|\d+),"model":"([a-z_]+\.[a-z_]+)"')

#: key → the model whose ids its value holds (an id, or a list of ids), in the documents and layout files
DOCUMENT_ID_KEYS: dict[str, str] = {
    "sourceLineIds": "ocr.line",
    "lineIds": "ocr.line",
    "bookId": "books.book",
    "runId": "assembly.assemblyrun",
}


class IdMap:
    """Old → new primary keys by model, and the user / account every row of the import is attributed to."""

    def __init__(self, user_id: int | None = None, organization_id: int | None = None) -> None:
        self.maps: dict[str, dict[int, int]] = defaultdict(dict)
        self.user_id = user_id
        self.organization_id = organization_id
        self.unmapped: Counter = Counter()

    def put(self, label: str, old: int, new: int) -> None:
        self.maps[label][int(old)] = int(new)

    def get(self, label: str, old) -> int | None:
        if old is None or isinstance(old, bool):
            return None
        return self.maps[label].get(int(old))

    def value(self, label: str, old):
        """The new id of an id held in JSON. One with no counterpart (the row was deleted or re-made in the
        source after the JSON was written: a manuscript names lines a later OCR pass replaced) is counted and
        becomes `-old`: still distinct, but no row has it. Kept as it was, it could one day be another
        account's row (`editor.uncertain.attach_readings` looks lines up by id alone)."""
        if isinstance(old, bool) or not isinstance(old, int):
            return old
        new = self.maps[label].get(old)
        if new is None:
            self.unmapped[label] += 1
            return -abs(old)
        return new

    def ids(self, label: str, value):
        """`value` is an id, a list of ids or null (the documents' `sourceLineIds`)."""
        if isinstance(value, list):
            return [self.value(label, item) for item in value]
        return self.value(label, value)

    def key(self, label: str, old) -> str | None:
        """The new id of a dictionary key that holds an id, as a string; None when there is no counterpart."""
        try:
            new = self.maps[label].get(int(old))
        except (TypeError, ValueError):
            return None
        return None if new is None else str(new)

    def block_id(self, text: str) -> str:
        """A block or note id (`p812`, `h812-2`, `fn-n812`) with the line id inside it replaced."""
        found = BLOCK_ID.fullmatch(text)
        if found is None:
            return text
        new = self.maps["ocr.line"].get(int(found.group(3)))
        if new is None:
            return text
        return f"{found.group(1) or ''}{found.group(2)}{new}{found.group(4)}"

    def user(self, old):
        return None if old is None else self.user_id


def remap_tree(value, ids: IdMap, keys: dict[str, str] | None = None):
    """A JSON value with every block id (as a string or a key) and every id under one of `keys` remapped."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            new_key = ids.block_id(key) if isinstance(key, str) else key
            label = keys.get(key) if keys else None
            out[new_key] = ids.ids(label, item) if label else remap_tree(item, ids, keys)
        return out
    if isinstance(value, list):
        return [remap_tree(item, ids, keys) for item in value]
    if isinstance(value, str):
        return ids.block_id(value)
    return value


def _document(value, ids: IdMap):
    return remap_tree(value, ids, DOCUMENT_ID_KEYS) if value is not None else None


def _blocks_only(value, ids: IdMap):
    return remap_tree(value, ids)


def _assembly_settings(value, ids: IdMap):
    if not isinstance(value, dict):
        return value
    out = dict(value)
    styles = out.get("line_styles")
    if isinstance(styles, dict):
        out["line_styles"] = {
            new: style for key, style in styles.items() if (new := ids.key("ocr.line", key)) is not None
        }
    if isinstance(out.get("dismissed_suggestions"), list):
        out["dismissed_suggestions"] = remap_tree(out["dismissed_suggestions"], ids)
    return out


def _snapshot(value, ids: IdMap):
    """A review snapshot (`review.services.line_snapshot` / `_page_state` / a suggestion's status)."""
    if not isinstance(value, dict):
        return value
    out = dict(value)
    if isinstance(out.get("id"), int):
        out["id"] = ids.value("ocr.line", out["id"])
    if out.get("region_id") is not None:
        out["region_id"] = ids.value("processing.region", out["region_id"])
    if isinstance(out.get("gap"), int):
        out["gap"] = ids.value("ocr.textgap", out["gap"])
    if isinstance(out.get("gaps"), dict):
        out["gaps"] = {
            new: state
            for key, state in out["gaps"].items()
            if (new := ids.key("ocr.textgap", key)) is not None
        }
    if isinstance(out.get("lines"), dict):
        out["lines"] = {
            new: flag for key, flag in out["lines"].items() if (new := ids.key("ocr.line", key)) is not None
        }
    if out.get("reviewed_by") is not None:
        out["reviewed_by"] = ids.user_id
    return out


def _run_settings(value, ids: IdMap):
    if not isinstance(value, dict):
        return value
    out = remap_tree(value, ids, DOCUMENT_ID_KEYS)
    out.pop("plan", None)  # the id of a comparison (`editor.ChangesPlan`) that stays behind
    return out


def _run_included(value, ids: IdMap):
    """`{page id: {number, reviewed, sig, at}}`: the keys are page ids."""
    if not isinstance(value, dict):
        return value
    return {new: info for key, info in value.items() if (new := ids.key("books.page", key)) is not None}


def _front_matter(value, ids: IdMap):
    """The stylesheet's front matter; the cover names its picture by `editor.BookImage` id."""
    if not isinstance(value, dict):
        return value
    out = dict(value)
    cover = out.get("cover")
    if isinstance(cover, dict) and isinstance(cover.get("image"), int):
        out["cover"] = {**cover, "image": ids.value("editor.bookimage", cover["image"])}
    return out


#: (model, field) → what turns its value into the new database's: the JSONFields that hold ids.
JSON_REWRITERS: dict[tuple[str, str], Callable] = {
    ("books.book", "assembly_settings"): _assembly_settings,
    ("review.linerevision", "before"): _snapshot,
    ("review.linerevision", "after"): _snapshot,
    ("assembly.assemblyrun", "settings"): _run_settings,
    ("assembly.assemblyrun", "warnings"): lambda value, ids: remap_tree(value, ids, DOCUMENT_ID_KEYS),
    ("assembly.assemblyrun", "included"): _run_included,
    ("editor.manuscript", "document"): _document,
    ("editor.manuscript", "base"): _document,
    ("editor.manuscriptsnapshot", "document"): _document,
    ("editor.manuscriptsnapshot", "base"): _document,
    ("editor.stylesheet", "front_matter"): _front_matter,
    ("publishing.previewrender", "chapters"): _blocks_only,
    ("publishing.previewrender", "checks"): _blocks_only,
    ("publishing.previewrender", "result"): _blocks_only,
    ("publishing.livelayout", "chapters"): _blocks_only,
}

#: The JSONFields that hold no id of any row (coordinates, parameters, counts, labels, faces' facts).
JSON_WITHOUT_IDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("books.page", "attention_flags"),
        ("books.page", "guides_override"),
        ("books.page", "reading"),
        ("processing.preprocess", "border_crop"),
        ("processing.preprocess", "crop_box"),
        ("processing.preprocess", "auto_params"),
        ("processing.preprocess", "manual_params"),
        ("processing.preprocess", "line_boxes"),
        ("processing.preprocess", "page_number_box"),
        ("processing.preprocess", "edge_strips_removed"),
        ("processing.region", "bbox"),
        ("ocr.ocrrun", "params"),
        ("ocr.line", "bbox"),
        ("ocr.line", "tokens"),
        ("assembly.assemblyrun", "stats"),
        ("editor.stylesheet", "heading_scale"),
        ("accounts.organizationfont", "files"),
    }
)


def rewrite_layout(data, ids: IdMap):
    """A layout file (`layout.json`, `live-<n>.json`): block ids remapped, and the render each page's image
    comes from (`render`, `pages[].src.render`) points at the new `PreviewRender` row (a page whose render did
    not travel loses its image reference, so the book page lays it out again)."""
    if not isinstance(data, dict):
        return data
    out = remap_tree(data, ids)
    if isinstance(out.get("render"), int):
        out["render"] = ids.get("publishing.previewrender", out["render"])
    for page in out.get("pages") or []:
        src = page.get("src") if isinstance(page, dict) else None
        if isinstance(src, dict):
            new = ids.get("publishing.previewrender", src.get("render"))
            page["src"] = None if new is None else {**src, "render": new}
    return out


# ====================================================================== reading a bundle


@dataclass
class Bundle:
    path: Path
    header: dict
    manifest: dict[str, int]
    lines: dict[tuple[int | None, str], list[str]]  # (source book, model) → serialized objects

    def book_ids(self) -> list[int]:
        return [book["id"] for book in self.header["books"]]


def read_bundle(path: Path | str) -> Bundle:
    """Read and check a bundle: the format, every media file named in the manifest (present, right size)."""
    root = Path(path).expanduser().resolve()
    data = root / DATA_FILE
    if not data.is_file() or not (root / MANIFEST_FILE).is_file():
        raise BundleError(f"{root} is not a complete bundle (data.json or manifest.json is missing).")
    manifest = _read_json(root / MANIFEST_FILE)
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_NAME:
        raise BundleError(f"{root / MANIFEST_FILE} is not a Nassakh bundle manifest.")
    lines: dict[tuple[int | None, str], list[str]] = defaultdict(list)
    with data.open(encoding="utf-8") as handle:
        first = handle.readline().rstrip("\n")
        second = handle.readline().rstrip("\n")
        prefix = '{"header": '
        if not first.startswith(prefix) or not first.endswith(",") or second != '"objects": [':
            raise BundleError(f"{data} is not a Nassakh bundle (unexpected layout).")
        header = json.loads(first[len(prefix) : -1])
        for line in handle:
            line = line.rstrip("\n")
            if line == "]}":
                break
            text = line[:-1] if line.endswith(",") else line
            found = OBJECT_START.match(text)
            if found is None:
                raise BundleError(f"{data} holds a line that is not a bundle object: {text[:60]!r}")
            book = None if found.group(1) == "null" else int(found.group(1))
            lines[(book, found.group(2))].append(text)
    if header.get("format") != FORMAT_NAME:
        raise BundleError(f"{data} is not a Nassakh bundle.")
    if header.get("version") != FORMAT_VERSION:
        raise BundleError(
            f"The bundle is format {header.get('version')}, this code reads format {FORMAT_VERSION}: "
            "export and import with the same version of Nassakh."
        )
    files = manifest.get("files") or {}
    bad: list[str] = []
    for name, size in files.items():
        item = root / MEDIA_DIR / name
        if not item.is_file() or item.stat().st_size != size:
            bad.append(name)
    if bad:
        raise BundleError(
            f"{len(bad)} media file(s) of the bundle are missing or incomplete (e.g. {bad[0]}): "
            "transfer the bundle again (rsync resumes where it stopped)."
        )
    return Bundle(path=root, header=header, manifest=files, lines=lines)


def check_migrations(header: dict) -> None:
    """The target database has every migration the bundle was written with."""
    applied = MigrationRecorder(connection).applied_migrations()
    behind = [
        f"{app}.{name}"
        for app, name in sorted(header.get("migrations", {}).items())
        if (app, name) not in applied
    ]
    if behind:
        raise BundleError(
            f"This database is behind the bundle's: {', '.join(behind)} not applied. "
            "Run `manage.py migrate` first."
        )


# ====================================================================== importing a bundle

#: CharFields that hold a media path inside `books/<id>/` (the FileFields are found from the models)
PATH_FIELDS: frozenset[tuple[str, str]] = frozenset(
    {("publishing.previewrender", "folder"), ("publishing.livelayout", "path")}
)


@dataclass
class BookResult:
    """What the import did with one book of the bundle."""

    source_id: int
    title: str
    action: str  # created | skipped | replaced
    new_id: int | None = None
    replaced_ids: list[int] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    media_files: int = 0
    media_bytes: int = 0
    indexed_pages: int = 0


@dataclass
class ImportReport:
    organization_id: int = 0
    organization_name: str = ""
    organization_created: bool = False
    user_email: str = ""
    user_id: int = 0
    user_created: bool = False
    password_changed: bool = False
    books: list[BookResult] = field(default_factory=list)
    faces_created: int = 0
    faces_reused: int = 0
    unmapped: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False

    def created_books(self) -> list[BookResult]:
        return [book for book in self.books if book.action != "skipped"]


def _account(name: str):
    """The demo account: an organisation (not an individual), unlimited."""
    from accounts.models import Organization

    organization = Organization.objects.filter(name=name).order_by("id").first()
    if organization is None:
        return (
            Organization.objects.create(name=name, kind=Organization.Kind.ORGANIZATION, unlimited=True),
            True,
        )
    changed = []
    if not organization.unlimited:
        organization.unlimited = True
        changed.append("unlimited")
    if organization.kind != Organization.Kind.ORGANIZATION:
        organization.kind = Organization.Kind.ORGANIZATION
        changed.append("kind")
    if changed:
        organization.save(update_fields=changed)
    return organization, False


def _user(email: str, password: str | None, organization):
    """The demo user: the email is the login (D106), active, the account's admin, in the `editor` group. An
    existing user keeps their password unless one is given."""
    from accounts.backends import normalize_email
    from accounts.models import Membership
    from accounts.services import ensure_groups
    from core.decorators import ROLE_EDITOR

    login = normalize_email(email)
    if "@" not in login or len(login) > 150:
        raise BundleError("--email must be an email address of at most 150 characters.")
    model = get_user_model()
    user = (
        model._default_manager.filter(username=login).first()
        or model._default_manager.filter(email__iexact=login).first()
    )
    created = False
    changed = False
    if user is None:
        if not password:
            raise BundleError(f"--password is required: {login} does not exist yet.")
        user = model._default_manager.create_user(
            username=login, email=login, password=password, first_name=organization.name[:150]
        )
        created = changed = True
    else:
        if password:
            user.set_password(password)
            changed = True
        user.is_active = True
        user.save()
    membership = Membership.objects.filter(user=user).select_related("organization").first()
    if membership is not None and membership.organization_id != organization.pk:
        raise BundleError(
            f"{login} already belongs to the organisation «{membership.organization.name}»; "
            "use another --email."
        )
    Membership.objects.update_or_create(
        user=user, defaults={"organization": organization, "role": Membership.Role.ADMIN}
    )
    editor = next(group for group in ensure_groups() if group.name == ROLE_EDITOR)
    user.groups.add(editor)
    return user, created, changed


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


class _Importer:
    """One import: the rows of the bundle written into the open transaction, then the files."""

    def __init__(self, bundle: Bundle, report: ImportReport, replace: bool, dry_run: bool, log: Log) -> None:
        self.bundle = bundle
        self.report = report
        self.replace = replace
        self.dry_run = dry_run
        self.log = log
        self.ids = IdMap()
        self.root = media_root()
        self.created: list[Path] = []  # files and folders this import made (removed when it fails)
        self.old_folders: list[Path] = []  # folders of replaced copies (removed once the import is committed)
        self.layout_sources: set[str] = set()  # source paths of the layout files that need their ids remapped
        self.font_copies: list[tuple[str, str]] = []  # (bundle path, path under MEDIA_ROOT)
        self.organization = None

    # ------------------------------------------------------------------ the run

    def run(self, organization, user) -> None:
        self.organization = organization
        self.ids.user_id = user.pk
        self.ids.organization_id = organization.pk
        todo = self._decide()
        self._check_space(todo)
        self._faces(todo)
        for result in todo:
            self._book(result)
        for result in todo:
            self._finish(result)
        if not self.dry_run:
            self._copy_media(todo)
        connection.check_constraints()
        self.report.unmapped = {label: count for label, count in sorted(self.ids.unmapped.items())}
        if self.ids.unmapped:
            self.report.warnings.append(
                "ids in JSON that name rows the source no longer has were made unresolvable (negative): "
                + ", ".join(f"{label} ×{count}" for label, count in sorted(self.ids.unmapped.items()))
            )

    # ------------------------------------------------------------------ which books

    def _decide(self) -> list[BookResult]:
        book_model = apps.get_model(ROOT)
        todo: list[BookResult] = []
        for entry in self.bundle.header["books"]:
            result = BookResult(source_id=entry["id"], title=entry["title"], action="created")
            if (entry["id"], ROOT) not in self.bundle.lines:
                raise BundleError(f"data.json holds no row for book {entry['id']} named in its header.")
            existing = list(
                book_model.objects.filter(
                    organization=self.organization,
                    title=entry["title"],
                    source_page_count=entry["source_page_count"],
                ).order_by("pk")
            )
            if existing and not self.replace:
                result.action = "skipped"
                result.replaced_ids = [book.pk for book in existing]
            elif existing:
                result.action = "replaced"
                result.replaced_ids = [book.pk for book in existing]
                for book in existing:
                    self.old_folders.append(self.root / "books" / str(book.pk))
                    book.delete()
            self.report.books.append(result)
            if result.action != "skipped":
                todo.append(result)
        return todo

    def _check_space(self, todo: list[BookResult]) -> None:
        if self.dry_run:
            return
        needed = 0
        for result in todo:
            prefix = f"books/{result.source_id}/"
            needed += sum(size for name, size in self.bundle.manifest.items() if name.startswith(prefix))
        needed += sum(size for name, size in self.bundle.manifest.items() if name.startswith("orgs/"))
        self.root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.root).free
        if free < needed * 1.05 + 50_000_000:
            raise BundleError(
                f"Not enough disk space under {self.root}: {_mb(needed)} MB needed, {_mb(free)} MB free."
            )

    # ------------------------------------------------------------------ the organisation's faces

    def _faces(self, todo: list[BookResult]) -> None:
        from publishing.fonts import org_font_id

        wanted: set[int] = set()
        for result in todo:
            for text in self.bundle.lines.get((result.source_id, "editor.stylesheet"), []):
                fields = json.loads(text)["fields"]
                for role in ("body_font", "latin_font", "heading_font"):
                    pk = org_font_id(fields.get(role, ""))
                    if pk is not None:
                        wanted.add(pk)
        if not wanted:
            return
        font_model = apps.get_model(FONT)
        for text in self.bundle.lines.get((None, FONT), []):
            item = json.loads(text)
            if item["pk"] not in wanted:
                continue
            fields = item["fields"]
            sha = ((fields.get("files") or {}).get("regular") or {}).get("sha256")
            same = None
            if sha:
                for font in font_model.objects.filter(organization=self.organization):
                    if ((font.files or {}).get("regular") or {}).get("sha256") == sha:
                        same = font
                        break
            if same is not None:
                self.ids.put(FONT, item["pk"], same.pk)
                self.report.faces_reused += 1
                continue
            for style in ("regular", "bold", "italic", "bold_italic"):
                name = fields.get(style) or ""
                if name:
                    target = f"orgs/{self.organization.pk}/fonts/{Path(name).name}"
                    self.font_copies.append((name, target))
                    fields[style] = target
            self._save(FONT, item["pk"], fields, source_id=None)
            self.report.faces_created += 1

    # ------------------------------------------------------------------ the rows

    def _book(self, result: BookResult) -> None:
        counts: Counter = Counter()
        for label in travelling_labels():
            for text in self.bundle.lines.get((result.source_id, label), []):
                item = json.loads(text)
                self._save(label, item["pk"], item["fields"], source_id=result.source_id)
                counts[label] += 1
            if label == ROOT:
                result.new_id = self.ids.get(ROOT, result.source_id)
        expected = next(b["counts"] for b in self.bundle.header["books"] if b["id"] == result.source_id)
        if dict(counts) != expected:
            raise BundleError(
                f"Book {result.source_id} «{result.title}»: the rows written differ from the bundle's header "
                f"({dict(counts)} against {expected})."
            )
        result.counts = dict(sorted(counts.items()))

    def _save(self, label: str, old_pk: int, fields: dict, *, source_id: int | None) -> None:
        self._rewrite(label, fields, source_id)
        item = {"model": label, "fields": fields}
        saved = next(iter(serializers.deserialize("python", [item])))
        saved.save()  # raw: the dates (`auto_now`) are the source's, as the review signatures need
        self.ids.put(label, old_pk, saved.object.pk)

    def _fk(self, label: str, f: models.Field, value):
        if value is None:
            return None
        target = f.remote_field.model
        if target is get_user_model():
            return self.ids.user_id
        target_label = _label(target)
        if target_label == ORGANIZATION:
            return self.ids.organization_id
        new = self.ids.get(target_label, value)
        if new is None:
            if f.null:
                self.ids.unmapped[f"{label}.{f.name}"] += 1
                return None
            raise BundleError(f"{label}.{f.name}: the bundle holds no {target_label} with id {value}.")
        return new

    def _path(self, label: str, name: str, value, source_id: int | None):
        """A media path of a source book re-based on the new book's folder."""
        if not value:
            return value
        if label == ROOT and name == "source_pdf" and value not in self.bundle.manifest:
            return ""  # the source PDF did not travel
        prefix = f"books/{source_id}/"
        if source_id is None or not value.startswith(prefix):
            raise BundleError(f"{label}.{name} holds {value!r}, outside {prefix}.")
        return f"books/{self.ids.get(ROOT, source_id)}/{value[len(prefix) :]}"

    def _rewrite(self, label: str, fields: dict, source_id: int | None) -> None:
        from publishing.fonts import is_org_key, org_font_id

        model = apps.get_model(label)
        for f in model._meta.concrete_fields:
            name = f.name
            if f.primary_key or name not in fields:
                continue
            value = fields[name]
            if f.is_relation:
                fields[name] = self._fk(label, f, value)
            elif isinstance(f, models.JSONField):
                if label == ROOT and name == "assembly_settings":
                    continue  # finished once the lines are in (`_finish`)
                rewrite = JSON_REWRITERS.get((label, name))
                if rewrite is not None:
                    fields[name] = rewrite(value, self.ids)
                elif (label, name) not in JSON_WITHOUT_IDS:
                    raise BundleError(
                        f"{label}.{name} is a JSONField books.bundle does not know: classify it."
                    )
            elif isinstance(f, models.FileField) and label != FONT:
                fields[name] = self._path(label, name, value, source_id)
            elif (label, name) in PATH_FIELDS:
                if (label, name) == ("publishing.previewrender", "folder") and value:
                    self.layout_sources.add(f"{value}/layout.json")
                if (label, name) == ("publishing.livelayout", "path") and value:
                    self.layout_sources.add(value)
                fields[name] = self._path(label, name, value, source_id)
        if label == "books.page":
            fields["run_token"] = ""
            fields["run_claimed_at"] = None
        elif label == "assembly.assemblyrun" and fields.get("status") in ACTIVE_RUNS:
            fields.update(status="error", error=INTERRUPTED, task_id="")
        elif label == "editor.stylesheet":
            for role in ("body_font", "latin_font", "heading_font"):
                key = fields.get(role)
                if not is_org_key(key):
                    continue
                new = self.ids.get(FONT, org_font_id(key))
                if new is None:
                    self.report.warnings.append(
                        f"the organisation face {key} did not travel: Amiri / Times is used"
                    )
                    fields[role] = model._meta.get_field(role).default
                else:
                    fields[role] = f"org-{new}"

    def _finish(self, result: BookResult) -> None:
        """What needs the lines' new ids and could not be written with the book row: its assembly settings."""
        text = self.bundle.lines[(result.source_id, ROOT)][0]
        value = json.loads(text)["fields"].get("assembly_settings")
        apps.get_model(ROOT).objects.filter(pk=result.new_id).update(
            assembly_settings=_assembly_settings(value, self.ids)
        )

    # ------------------------------------------------------------------ the files

    def _copy_media(self, todo: list[BookResult]) -> None:
        for result in todo:
            prefix = f"books/{result.source_id}/"
            names = sorted(name for name in self.bundle.manifest if name.startswith(prefix))
            if not names:
                continue
            folder = self.root / "books" / str(result.new_id)
            if folder.exists():
                raise BundleError(
                    f"{folder} exists already (left by an earlier failed import?): remove it first."
                )
            self.created.append(folder)
            for name in names:
                target = folder / name[len(prefix) :]
                target.parent.mkdir(parents=True, exist_ok=True)
                source = self.bundle.path / MEDIA_DIR / name
                if name in self.layout_sources:
                    data = rewrite_layout(json.loads(source.read_text(encoding="utf-8")), self.ids)
                    target.write_text(_dumps(data), encoding="utf-8")
                else:
                    shutil.copy2(source, target)
                result.media_files += 1
                result.media_bytes += self.bundle.manifest[name]
        for name, target in self.font_copies:
            destination = self.root / target
            if destination.exists():
                continue  # faces are named by their content
            destination.parent.mkdir(parents=True, exist_ok=True)
            self.created.append(destination)
            shutil.copy2(self.bundle.path / MEDIA_DIR / name, destination)


def import_bundle(
    path: Path | str,
    *,
    email: str,
    password: str | None = None,
    organization_name: str = DEMO_ACCOUNT_NAME,
    replace: bool = False,
    dry_run: bool = False,
    log: Log = lambda line: None,
) -> ImportReport:
    """Import a bundle as the books of the demo account.

    One transaction writes the account, its user and every row (fresh keys); the files are copied into
    `MEDIA_ROOT/books/<new id>/` before it commits, and removed again if anything fails, so the database and
    the media folder are never left half-imported. A copy of a book that is already in the account (same title
    and source page count) is skipped, or with `replace` deleted first (rows and files). With `dry_run` the
    whole import runs and is rolled back, and no file is written. The search index of the new books is built
    after the commit.
    """
    bundle = read_bundle(path)
    check_migrations(bundle.header)
    report = ImportReport(dry_run=dry_run)
    importer = _Importer(bundle, report, replace, dry_run, log)
    try:
        with transaction.atomic():
            organization, report.organization_created = _account(organization_name)
            user, report.user_created, report.password_changed = _user(email, password, organization)
            report.organization_id, report.organization_name = organization.pk, organization.name
            report.user_email, report.user_id = user.username, user.pk
            importer.run(organization, user)
            if dry_run:
                transaction.set_rollback(True)
    except BaseException:
        for item in reversed(importer.created):
            _remove(item)
        raise
    if dry_run:
        return report
    for folder in importer.old_folders:
        shutil.rmtree(folder, ignore_errors=True)
    from research import index

    for result in report.created_books():
        try:
            result.indexed_pages = index.reindex_book(result.new_id)
        except Exception as exc:  # noqa: BLE001 - the import stands; the index can be built by hand
            report.warnings.append(
                f"the search index of book {result.new_id} could not be built ({type(exc).__name__}: {exc}); "
                f"run `manage.py research_reindex {result.new_id}`"
            )
    return report


def log_import(report: ImportReport, log: Log) -> None:
    """What an import did (or, for a dry run, would do), in lines."""
    verb = "would be" if report.dry_run else "was"
    log(
        f"account {report.organization_id} «{report.organization_name}» "
        f"({'created' if report.organization_created else 'found'}, unlimited)"
    )
    password = "set" if report.password_changed else "unchanged"
    state = "created" if report.user_created else "found"
    log(f"user {report.user_id} <{report.user_email}> ({state}; password {password})")
    if report.faces_created or report.faces_reused:
        log(f"organisation faces: {report.faces_created} created, {report.faces_reused} already there")
    for book in report.books:
        if book.action == "skipped":
            log(f"book {book.source_id} «{book.title}»: skipped, in the account as {book.replaced_ids}")
            continue
        replaced = f", replaced {book.replaced_ids}" if book.replaced_ids else ""
        log(f"book {book.source_id} «{book.title}» {verb} imported as book {book.new_id}{replaced}")
        for label, count in book.counts.items():
            log(f"    {label:<28} {count:>7}")
        log(f"    files {book.media_files} · {_mb(book.media_bytes)} MB · index {book.indexed_pages} page(s)")
    for warning in report.warnings:
        log(f"warning: {warning}")
