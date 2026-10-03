"""An organisation's fonts (D98): the upload checks, the stored files, removal and restoration.

An upload is one or more files (`.ttf`, `.otf`, `.woff2`, `.woff`), each at most `NASSAKH["ORG_FONT_MAX_MB"]`
and read with fontTools: a collection (`.ttc`), a variable face, a bitmap-only face, a face without the
Arabic letters nor the Latin ones, and a face whose licence (OS/2 `fsType`) forbids embedding or
subsetting are refused with the reason (the PDF embeds a subset of every face). A WOFF/WOFF2 file is
stored decompressed (TrueType or CFF OpenType: Word, EPUB and WeasyPrint read those). Each file's family
(name ID 16, else 1) and style (OS/2 weight 600 and up is bold; italic from `fsSelection` / `macStyle`)
are read from the file: files of one family make one face (`OrganizationFont`) with up to four files, and a
family the organisation already has takes the new files into its face (a style uploaded again replaces
the old file, which stays on disk). A new face needs its regular file. The admin confirms the organisation's
licence allows embedding; the licence notice is the one typed, else the files' own (name IDs 13, 14, 0).

Files are stored by content at `orgs/<org>/fonts/<sha256>.<ttf|otf>` and never rewritten. Removal is soft
(`remove_font`): the books that use the face fall back to Amiri with a notice («حُذف الخط من خطوط
المؤسسة», `publishing.fonts`) until another face is chosen or it is restored (`restore_font`); a removed face
no book uses can be deleted for good (`delete_font`, its files with it).
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from publishing import fonts as F

from .models import FONT_STYLE_LABELS, FONT_STYLES, OrganizationFont

EXTENSIONS: tuple[str, ...] = (".ttf", ".otf", ".woff2", ".woff")
MAX_FILES = 8
NAME_MAX = 120
LICENCE_MAX = 2000

NO_FILE = "اختر ملف خط واحدًا على الأقل."
TOO_MANY = f"ارفع {MAX_FILES} ملفات على الأكثر في المرة الواحدة."
NO_CONFIRM = "أقرّ أولًا بأن ترخيص الخط يسمح للمؤسسة بتضمينه في ملفات PDF وEPUB وWord."
WRONG_TYPE = "«{file}»: نوع الملف غير مقبول؛ المقبول TTF وOTF وWOFF2."
TOO_LARGE = "«{file}»: الملف أكبر من {mb} ميغابايت."
NOT_A_FONT = "«{file}»: تعذّرت قراءة الملف خطًّا؛ تأكّد أنه ملف خط سليم."
COLLECTION = "«{file}»: هذا ملف مجموعة خطوط (TTC)؛ ارفع كل خط في ملف TTF أو OTF مستقل."
VARIABLE = "«{file}»: الخطوط المتغيّرة غير مدعومة؛ ارفع ملفات الأوزان الثابتة (عادي وعريض)."
NO_OUTLINES = "«{file}»: الخط بلا رسوم للحروف (bitmap فقط)؛ لا يصلح للطباعة."
NO_LETTERS = "«{file}»: الخط لا يحوي حروفًا عربية ولا لاتينية."
RESTRICTED = "«{file}»: ترخيص الخط يمنع تضمينه في الملفات (fsType)؛ لا يمكن استعماله في الكتب."
NO_SUBSET = "«{file}»: ترخيص الخط يمنع تضمين جزء منه، وملفات PDF تضمّن الحروف المستعملة وحدها."
SAME_STYLE = "«{file}» و«{other}» كلاهما {style} لعائلة «{family}»؛ ارفع ملفًا واحدًا لكل وزن."
NO_REGULAR = "عائلة «{family}» بلا ملف عادي (Regular)؛ ارفعه معها، فالملف العادي أساس الخط."
NAME_TOO_LONG = f"الاسم أطول من {NAME_MAX} حرفًا."
STILL_USED = "الخط مستعمل في {count}؛ لا يُحذف نهائيًا حتى يُختار لها خط آخر."
NOT_REMOVED = "احذف الخط من القائمة أولًا، ثم احذفه نهائيًا."


class FontRefused(ValueError):
    """An upload (or an action) refused: `messages` are the Arabic reasons, one per file."""

    def __init__(self, messages: list[str] | str):
        self.messages = [messages] if isinstance(messages, str) else list(messages)
        super().__init__(" ".join(self.messages))


@dataclass
class InspectedFont:
    """A font file as it will be stored: its bytes (TrueType or CFF OpenType), extension, family, style and
    the facts kept in `OrganizationFont.files`."""

    data: bytes
    extension: str
    family: str  # name ID 1 (what Word and the reading systems match)
    group: str  # name ID 16, else 1: the files of one face
    full_name: str
    style: str
    weight: int
    italic: bool
    fs_type: int
    outlines: str
    latin: bool
    arabic: bool
    glyphs: int
    licence: str
    source_name: str
    sha256: str = field(init=False)

    def __post_init__(self):
        self.sha256 = hashlib.sha256(self.data).hexdigest()

    def facts(self) -> dict:
        return {
            "sha256": self.sha256,
            "size": len(self.data),
            "format": self.extension,
            "source_name": self.source_name,
            "full_name": self.full_name,
            "family": self.family,
            "group": self.group,
            "weight": self.weight,
            "italic": self.italic,
            "fs_type": self.fs_type,
            "outlines": self.outlines,
            "glyphs": self.glyphs,
        }


def max_bytes() -> int:
    return int(settings.NASSAKH.get("ORG_FONT_MAX_MB") or 20) * 1024 * 1024


def _name(font, *ids: int) -> str:
    table = font["name"] if "name" in font else None
    if table is None:
        return ""
    for name_id in ids:
        value = table.getDebugName(name_id)
        if value and value.strip():
            return " ".join(value.split())
    return ""


def _style_of(weight: int, italic: bool) -> str:
    bold = weight >= 600
    if bold and italic:
        return "bold_italic"
    if bold:
        return "bold"
    return "italic" if italic else "regular"


def inspect_font(data: bytes, source_name: str) -> InspectedFont:
    """Check one uploaded file and read what the face needs (see the module docstring). Raises
    `FontRefused` with the reason."""
    from fontTools.ttLib import TTFont, TTLibError

    label = source_name or "الملف"
    suffix = "." + source_name.rsplit(".", 1)[-1].lower() if "." in source_name else ""
    if suffix not in EXTENSIONS:
        raise FontRefused(WRONG_TYPE.format(file=label))
    if len(data) > max_bytes():
        raise FontRefused(TOO_LARGE.format(file=label, mb=max_bytes() // (1024 * 1024)))
    head = data[:4]
    if head == b"ttcf":
        raise FontRefused(COLLECTION.format(file=label))
    if head not in (b"\x00\x01\x00\x00", b"true", b"OTTO", b"wOFF", b"wOF2"):
        raise FontRefused(NOT_A_FONT.format(file=label))
    try:
        font = TTFont(io.BytesIO(data), fontNumber=0, lazy=False)
        for tag in ("head", "hhea", "maxp", "hmtx", "cmap", "name"):
            font[tag]  # noqa: B018 - decompiling the table is the check
        cmap = font.getBestCmap() or {}
        compressed = font.flavor in ("woff", "woff2")
        if compressed:
            font.flavor = None
            out = io.BytesIO()
            font.save(out)
            data = out.getvalue()
            font = TTFont(io.BytesIO(data), fontNumber=0, lazy=False)
    except (TTLibError, KeyError, AssertionError, ValueError, TypeError, IndexError, OSError, EOFError):
        raise FontRefused(NOT_A_FONT.format(file=label)) from None
    except Exception:  # noqa: BLE001 - fontTools raises many kinds on a broken file
        raise FontRefused(NOT_A_FONT.format(file=label)) from None
    if "fvar" in font or "CFF2" in font:
        raise FontRefused(VARIABLE.format(file=label))
    if "glyf" in font:
        outlines, extension = "glyf", "ttf"
    elif "CFF " in font:
        outlines, extension = "cff", "otf"
    else:
        raise FontRefused(NO_OUTLINES.format(file=label))
    if not cmap:
        raise FontRefused(NOT_A_FONT.format(file=label))
    os2 = font["OS/2"] if "OS/2" in font else None
    fs_type = int(getattr(os2, "fsType", 0) or 0)
    if fs_type & F.FS_BITMAP_ONLY:
        raise FontRefused(NO_OUTLINES.format(file=label))
    if fs_type & F.FS_RESTRICTED and not fs_type & (F.FS_PREVIEW_PRINT | F.FS_EDITABLE):
        raise FontRefused(RESTRICTED.format(file=label))
    if fs_type & F.FS_NO_SUBSETTING:
        raise FontRefused(NO_SUBSET.format(file=label))
    arabic = all(code in cmap for code in F.ARABIC_LETTERS)
    latin = all(code in cmap for code in F.LATIN_CHARS)
    if not arabic and not any(code in cmap for code in F.LATIN_CHARS[:52]):
        raise FontRefused(NO_LETTERS.format(file=label))
    weight = int(getattr(os2, "usWeightClass", 400) or 400)
    selection = int(getattr(os2, "fsSelection", 0) or 0)
    mac_style = int(getattr(font["head"], "macStyle", 0) or 0)
    if selection & 0x20 or mac_style & 0x01:
        weight = max(weight, 700)
    italic = bool(selection & 0x01 or mac_style & 0x02)
    family = _name(font, 1) or _name(font, 16) or source_name.rsplit(".", 1)[0]
    group = _name(font, 16, 1) or family
    licence = "\n".join(
        dict.fromkeys(value for value in (_name(font, 13), _name(font, 14), _name(font, 0)) if value)
    )
    return InspectedFont(
        data=data,
        extension=extension,
        family=family[:200],
        group=group[:200],
        full_name=_name(font, 4) or family,
        style=_style_of(weight, italic),
        weight=weight,
        italic=italic,
        fs_type=fs_type,
        outlines=outlines,
        latin=latin,
        arabic=arabic,
        glyphs=int(font["maxp"].numGlyphs),
        licence=licence[:LICENCE_MAX],
        source_name=source_name[:255],
    )


def _store(row: OrganizationFont, style: str, item: InspectedFont) -> None:
    """Put the file in the style's field: `orgs/<org>/fonts/<sha256>.<ext>`, written once (the same bytes
    uploaded again reuse the stored file)."""
    filename = f"{item.sha256}.{item.extension}"
    name = f"orgs/{row.organization_id}/fonts/{filename}"
    field_file = row.file_of(style)
    if default_storage.exists(name):
        field_file.name = name
    else:
        field_file.save(filename, ContentFile(item.data), save=False)


def _family_key(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def add_fonts(
    organization, uploads, *, user=None, name: str = "", licence: str = "", confirmed: bool = False
) -> list[OrganizationFont]:
    """Check and store uploaded font files for `organization` (see the module docstring): one face per
    family, a family it already has gets the files. `uploads` are Django `UploadedFile`s (or objects with
    `name`, `size` and `read()`). Raises `FontRefused` (nothing is stored) with every reason."""
    uploads = [item for item in (uploads or []) if item is not None]
    if not uploads:
        raise FontRefused(NO_FILE)
    if len(uploads) > MAX_FILES:
        raise FontRefused(TOO_MANY)
    if not confirmed:
        raise FontRefused(NO_CONFIRM)
    name = " ".join(str(name or "").split())
    if len(name) > NAME_MAX:
        raise FontRefused(NAME_TOO_LONG)
    errors: list[str] = []
    inspected: list[InspectedFont] = []
    for upload in uploads:
        source = str(getattr(upload, "name", "") or "")
        size = getattr(upload, "size", None)
        if size is not None and size > max_bytes():
            errors.append(TOO_LARGE.format(file=source or "الملف", mb=max_bytes() // (1024 * 1024)))
            continue
        try:
            inspected.append(inspect_font(upload.read(), source))
        except FontRefused as exc:
            errors.extend(exc.messages)
    families: dict[str, dict[str, InspectedFont]] = {}
    for item in inspected:
        styles = families.setdefault(_family_key(item.group), {})
        if item.style in styles:
            errors.append(
                SAME_STYLE.format(
                    file=item.source_name,
                    other=styles[item.style].source_name,
                    style=FONT_STYLE_LABELS[item.style],
                    family=item.group,
                )
            )
            continue
        styles[item.style] = item
    existing = {
        _family_key(group): row
        for row in OrganizationFont.objects.filter(organization=organization, removed_at__isnull=True)
        for group in {facts.get("group") or row.family for facts in (row.files or {}).values()} | {row.family}
    }
    for key, styles in families.items():
        if key not in existing and "regular" not in styles:
            errors.append(NO_REGULAR.format(family=next(iter(styles.values())).group))
    if errors:
        raise FontRefused(list(dict.fromkeys(errors)))
    licence = str(licence or "").strip()[:LICENCE_MAX]
    out: list[OrganizationFont] = []
    with transaction.atomic():
        for key, styles in families.items():
            row = existing.get(key)
            if row is None:
                regular = styles["regular"]
                row = OrganizationFont(
                    organization=organization,
                    name=(name if len(families) == 1 and name else regular.group)[:NAME_MAX],
                    family=regular.family,
                    uploaded_by=user if getattr(user, "is_authenticated", False) else None,
                )
            elif name and len(families) == 1:
                row.name = name
            files = dict(row.files or {})
            for style in FONT_STYLES:
                item = styles.get(style)
                if item is None:
                    continue
                _store(row, style, item)
                files[style] = item.facts()
                if style == "regular":
                    row.family = item.family
                    row.latin = item.latin
                    row.arabic = item.arabic
            row.files = files
            given = licence or "\n".join(
                dict.fromkeys(item.licence for item in styles.values() if item.licence)
            )
            if given:
                row.licence = given[:LICENCE_MAX]
            row.licence_confirmed_by = user if getattr(user, "is_authenticated", False) else None
            row.save()
            out.append(row)
    return out


BOOKS = ("كتاب واحد", "كتابين", "كتب", "كتابًا")


def books_phrase(count: int) -> str:
    """«كتاب واحد», «كتابين», «5 كتب», «12 كتابًا» (an object of a sentence, Western digits)."""
    from assembly.render import ar_count

    return ar_count(count, BOOKS)


def books_using(font: OrganizationFont):
    """The books whose stylesheet uses the face (any of its three roles), by title."""
    from books.models import Book

    key = font.key
    query = Q(stylesheet__body_font=key) | Q(stylesheet__latin_font=key) | Q(stylesheet__heading_font=key)
    return Book.objects.filter(query).order_by("title", "id").distinct()


def templates_using(font: OrganizationFont):
    """The organisation's templates that name the face."""
    from .models import StyleTemplate

    key = font.key
    return [
        template
        for template in StyleTemplate.objects.filter(organization_id=font.organization_id)
        if key in {(template.values or {}).get(name) for name in ("body_font", "latin_font", "heading_font")}
    ]


def remove_font(font: OrganizationFont, user=None) -> OrganizationFont:
    """Take the face off the organisation's menus (soft): the books that use it fall back to Amiri with a
    notice until another face is chosen or it is restored."""
    if font.removed_at is None:
        font.removed_at = timezone.now()
        font.removed_by = user if getattr(user, "is_authenticated", False) else None
        font.save(update_fields=["removed_at", "removed_by", "updated_at"])
    return font


def restore_font(font: OrganizationFont) -> OrganizationFont:
    """Bring a removed face back: the books that kept its key use it again."""
    if font.removed_at is not None:
        font.removed_at = None
        font.removed_by = None
        font.save(update_fields=["removed_at", "removed_by", "updated_at"])
    return font


def rename_font(font: OrganizationFont, name: str, licence: str | None = None) -> OrganizationFont:
    """Change the face's display name (and its licence notice when given)."""
    name = " ".join(str(name or "").split())
    if not name:
        raise FontRefused("اكتب اسمًا للخط.")
    if len(name) > NAME_MAX:
        raise FontRefused(NAME_TOO_LONG)
    font.name = name
    fields = ["name", "updated_at"]
    if licence is not None:
        font.licence = str(licence).strip()[:LICENCE_MAX]
        fields.append("licence")
    font.save(update_fields=fields)
    return font


def delete_font(font: OrganizationFont) -> None:
    """Delete a removed face no book uses, with its files (a file another face also stores is kept)."""
    if font.removed_at is None:
        raise FontRefused(NOT_REMOVED)
    used = books_using(font).count()
    if used:
        raise FontRefused(STILL_USED.format(count=books_phrase(used)))
    names = [font.file_of(style).name for style in FONT_STYLES if font.file_of(style)]
    others = OrganizationFont.objects.exclude(pk=font.pk)
    with transaction.atomic():
        font.delete()
    for name in names:
        shared = others.filter(Q(regular=name) | Q(bold=name) | Q(italic=name) | Q(bold_italic=name)).exists()
        if not shared:
            try:
                default_storage.delete(name)
            except OSError:
                pass
