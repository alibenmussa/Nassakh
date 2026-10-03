"""The font registry (PHASE5_SPEC §2, D45): the book faces, where their files are, and what to do when
one is missing.

Amiri is vendored (`static/fonts/amiri/`, OFL). Simplified Arabic, Traditional Arabic, Times New Roman and
Lotus are Monotype / Microsoft / Linotype faces that are never committed: their files are looked up in the
Mac's font folders (`NASSAKH["FONT_DIRS"]`, by default `~/Library/Fonts`, `/Library/Fonts`,
`/System/Library/Fonts/Supplemental`, `/System/Library/Fonts`; Lotus also in `~/Downloads`). All five allow
PDF embedding (OS/2 `fsType` 0 or 8). A face whose files are missing is reported
(«الخط غير مثبّت على هذا الجهاز») and replaced by Amiri when rendering.

Lotus has no usable Latin letters or digits: Latin text uses the stylesheet's Latin face (Times New Roman
by default). In the CSS every Arabic face is declared with a `unicode-range` that leaves Latin letters to
the Latin face — and digits too when the face has none — so a paragraph is set in one family list
(`"nk-body", "nk-latin"`) and each character takes the right face, as Word's `rFonts` ascii / cs pair does.
A role set in the Latin face's own file (Simplified Arabic for both, D60) is declared without a range: the
face sets its own Latin letters (WeasyPrint does not take the same file again for `nk-latin`, and would
fall back to a system face such as "Serif Narrow").

**An organisation's faces (D98)** are `accounts.models.OrganizationFont` rows, keyed `org-<pk>` in a
stylesheet next to the registry's keys. `spec_of` / `locate` read them from the database (their files are
the stored uploads under MEDIA_ROOT, so every renderer takes them like any other face); a face that was
removed (or deleted) resolves like a face that is not installed — Amiri stands in — and its missing entry
says so (`REMOVED_MESSAGE`, `removed: True`). The browser takes their files from `accounts:font_file` (the
organisation's members only, never `local()`); the font menus show them in their own face through a
`nk-org-<pk>` family (`org_sample_css`). Embedding (`org_embeds`): the PDF always (an upload whose licence
forbids embedding or subsetting is refused), EPUB whole, Word whole when the licence allows an editable
document and the outlines are TrueType (Word embeds no CFF face).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from django.conf import settings

DEFAULT_FONT_DIRS: tuple[str, ...] = (
    "~/Library/Fonts",
    "/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/System/Library/Fonts",
)
VENDORED_DIR = Path(settings.BASE_DIR) / "static" / "fonts"
FALLBACK = "amiri"
DEFAULT_LATIN = "times"
MISSING_MESSAGE = "الخط غير مثبّت على هذا الجهاز"
# D98: an organisation's faces
ORG_PREFIX = "org-"
REMOVED_MESSAGE = "حُذف الخط من خطوط المؤسسة"
DELETED_NAME = "خط محذوف"
UNKNOWN_FACE = "الخط غير معروف."
NO_LATIN = "اختر للنص اللاتيني خطًّا فيه حروف لاتينية."
NO_ARABIC = "اختر خطًّا فيه حروف عربية."
REMOVED_FACE = "حُذف هذا الخط من خطوط المؤسسة؛ اختر خطًّا آخر."
ARABIC_LETTERS = tuple(ord(char) for char in "ابتحدرسعلمنهوي")  # a face that has them sets Arabic
LATIN_CHARS = tuple(range(0x41, 0x5B)) + tuple(range(0x61, 0x7B)) + tuple(range(0x30, 0x3A))  # A-Z a-z 0-9
# OS/2 fsType: the embedding licence of a font file
FS_RESTRICTED = 0x0002  # no embedding at all
FS_PREVIEW_PRINT = 0x0004  # embedded read-only (a PDF, an EPUB; not an editable Word file)
FS_EDITABLE = 0x0008
FS_NO_SUBSETTING = 0x0100
FS_BITMAP_ONLY = 0x0200

# Everything but Latin letters: controls, space, punctuation, digits, symbols, Arabic, Arabic
# presentation forms, general punctuation (dashes, quotes, ZWJ / ZWNJ, direction marks).
_ARABIC_RANGES = (
    "U+0000-0040, U+005B-0060, U+007B-00BF, U+00D7, U+00F7, U+0600-06FF, U+0750-077F, U+08A0-08FF, "
    "U+2000-206F, U+FB50-FDFF, U+FE70-FEFF"
)
# The same without the digits 0-9 (a face without Latin glyphs takes its digits from the Latin face).
_ARABIC_RANGES_NO_DIGITS = _ARABIC_RANGES.replace("U+0000-0040", "U+0000-002F, U+003A-0040")


@dataclass(frozen=True)
class FontSpec:
    """One face of the registry: display name, the installed family name and candidate file names."""

    key: str
    name: str
    label: str
    family: str  # the family name installed on the Mac (browser samples use it with local())
    regular: tuple[str, ...]
    bold: tuple[str, ...]
    licence: str
    latin: bool  # has usable Latin letters and digits
    vendored: bool = False
    extra_dirs: tuple[str, ...] = ()
    italic: tuple[str, ...] = ()  # the italic files (a face without them is set upright in italic runs)
    bold_italic: tuple[str, ...] = ()
    org: bool = False  # an organisation's face (D98): its files are its stored uploads
    arabic: bool = True  # has the Arabic letters (every registry face does)


FONTS: dict[str, FontSpec] = {
    "amiri": FontSpec(
        "amiri",
        "Amiri",
        "أميري",
        "Amiri",
        ("amiri/Amiri-Regular.ttf",),
        ("amiri/Amiri-Bold.ttf",),
        "SIL Open Font License 1.1 (مضمَّن مع نسّاخ)",
        latin=True,
        vendored=True,
    ),
    "simplified_arabic": FontSpec(
        "simplified_arabic",
        "Simplified Arabic",
        "Simplified Arabic",
        "Simplified Arabic",
        ("simpo.ttf", "Simplified Arabic.ttf", "SimplifiedArabic.ttf"),
        ("simpbdo.ttf", "Simplified Arabic Bold.ttf", "SimplifiedArabic-Bold.ttf"),
        "Microsoft / Monotype (يُسمح بتضمينه في PDF)",
        latin=True,
    ),
    "traditional_arabic": FontSpec(
        "traditional_arabic",
        "Traditional Arabic",
        "Traditional Arabic",
        "Traditional Arabic",
        ("trado.ttf", "Traditional Arabic.ttf", "TraditionalArabic.ttf"),
        ("tradbdo.ttf", "Traditional Arabic Bold.ttf", "TraditionalArabic-Bold.ttf"),
        "Microsoft / Monotype (يُسمح بتضمينه في PDF)",
        latin=True,
    ),
    "times": FontSpec(
        "times",
        "Times New Roman",
        "Times New Roman",
        "Times New Roman",
        ("Times New Roman.ttf", "times.ttf"),
        ("Times New Roman Bold.ttf", "timesbd.ttf"),
        "Monotype (يُسمح بتضمينه في PDF)",
        latin=True,
        italic=("Times New Roman Italic.ttf", "timesi.ttf"),
        bold_italic=("Times New Roman Bold Italic.ttf", "timesbi.ttf"),
    ),
    "lotus": FontSpec(
        "lotus",
        "Lotus",
        "Lotus",
        "Lotus Linotype Exnd",
        ("Lotus.ttf", "Lotus Linotype.ttf", "LotusLinotype.ttf", "Lotus Linotype Exnd.ttf"),
        ("Lotus-Bold.ttf", "Lotus Bold.ttf", "Lotus Linotype Bold.ttf", "LotusLinotype-Bold.ttf"),
        "Linotype (يُسمح بتضمينه في PDF)",
        latin=False,
        extra_dirs=("~/Downloads",),
    ),
}
LATIN_FONTS: tuple[str, ...] = tuple(key for key, spec in FONTS.items() if spec.latin)


def font_dirs() -> list[Path]:
    """The system folders searched for the non-vendored faces (setting `NASSAKH["FONT_DIRS"]`)."""
    dirs = settings.NASSAKH.get("FONT_DIRS") or DEFAULT_FONT_DIRS
    return [Path(os.path.expanduser(str(d))) for d in dirs]


@dataclass(frozen=True)
class FontFiles:
    """The files of an installed face (`bold`, `italic`, `bold_italic` None when not there)."""

    regular: Path
    bold: Path | None = None
    italic: Path | None = None
    bold_italic: Path | None = None

    def styles(self) -> list[tuple[str, str, Path]]:
        """`(weight, style, path)` of every file there is."""
        out = [("400", "normal", self.regular)]
        for weight, style, path in (
            ("700", "normal", self.bold),
            ("400", "italic", self.italic),
            ("700", "italic", self.bold_italic),
        ):
            if path is not None:
                out.append((weight, style, path))
        return out


def _find(names: tuple[str, ...], dirs: list[Path]) -> Path | None:
    for folder in dirs:
        for name in names:
            path = folder / name
            if path.is_file():
                return path
    return None


# ====================================================================== an organisation's faces (D98)


def org_font_id(key) -> int | None:
    """The `OrganizationFont` pk of an `org-<pk>` key, None for any other key."""
    if not isinstance(key, str) or not key.startswith(ORG_PREFIX):
        return None
    digits = key[len(ORG_PREFIX) :]
    return int(digits) if digits.isdigit() and len(digits) < 12 else None


def is_org_key(key) -> bool:
    """True for an organisation face's key (`org-<pk>`)."""
    return org_font_id(key) is not None


def sample_family(pk: int) -> str:
    """The CSS family the browser shows an organisation's face in (the font menus' samples)."""
    return f"nk-org-{int(pk)}"


def _org_row(key):
    pk = org_font_id(key)
    if pk is None:
        return None
    from accounts.models import OrganizationFont

    return OrganizationFont.objects.filter(pk=pk).first()


def _org_spec(row) -> FontSpec:
    return FontSpec(
        row.key,
        row.name,
        row.name,
        row.family,
        (),
        (),
        row.licence or "",
        latin=row.latin,
        org=True,
        arabic=row.arabic,
    )


def _org_files(row) -> FontFiles | None:
    """The stored files of an organisation's face; None when it was removed or its regular file is gone."""
    if row is None or row.removed_at is not None or not row.regular:
        return None
    from django.core.files.storage import default_storage

    def path_of(field_file) -> Path | None:
        if not field_file:
            return None
        try:
            path = Path(default_storage.path(field_file.name))
        except (NotImplementedError, ValueError):
            return None
        return path if path.is_file() else None

    regular = path_of(row.regular)
    if regular is None:
        return None
    return FontFiles(
        regular=regular,
        bold=path_of(row.bold),
        italic=path_of(row.italic),
        bold_italic=path_of(row.bold_italic),
    )


def spec_of(key) -> FontSpec | None:
    """The registry's spec of `key`, or an organisation face's (removed ones too), None when unknown."""
    if key in FONTS:
        return FONTS[key]
    row = _org_row(key)
    return _org_spec(row) if row is not None else None


def display_name(key) -> str:
    """The name a face is shown by (an organisation's: its name; a deleted one: «خط محذوف»)."""
    spec = spec_of(key)
    if spec is not None:
        return spec.name
    return DELETED_NAME if is_org_key(key) else str(key or "")


def face_error(key, organization, role: str = "body") -> str:
    """Why `key` cannot be chosen for a role (`body`, `heading`, `latin`) of a book of `organization`, ''
    when it can: a registry face (the Latin role needs Latin letters; a face not installed is accepted and
    renders in Amiri), or an active face of the book's own organisation with the role's letters."""
    unknown = NO_LATIN if role == "latin" else UNKNOWN_FACE
    if not isinstance(key, str):
        return unknown
    if key in FONTS:
        return NO_LATIN if role == "latin" and not FONTS[key].latin else ""
    row = _org_row(key)
    if row is None or organization is None or row.organization_id != getattr(organization, "pk", None):
        return unknown
    if row.removed_at is not None:
        return REMOVED_FACE
    if role == "latin":
        return "" if row.latin else NO_LATIN
    return "" if row.arabic else NO_ARABIC


def org_fonts(organization, include_removed: bool = False):
    """The faces of an organisation (a queryset; the active ones unless `include_removed`)."""
    from accounts.models import OrganizationFont

    if organization is None:
        return OrganizationFont.objects.none()
    rows = OrganizationFont.objects.filter(organization=organization)
    return rows if include_removed else rows.filter(removed_at__isnull=True)


def latin_keys(organization=None) -> list[str]:
    """The keys the Latin menu offers: the registry's faces with Latin letters, then the organisation's."""
    keys = list(LATIN_FONTS)
    keys += [row.key for row in org_fonts(organization) if row.latin]
    return keys


@lru_cache(maxsize=64)
def _embedding(path: str, size: int, mtime: int) -> tuple[int, str]:
    """`(fsType, outlines)` of a font file: `glyf` (TrueType) or `cff`; `(0, "")` when unreadable."""
    from fontTools.ttLib import TTFont

    try:
        with TTFont(path, lazy=True, fontNumber=0) as font:
            fs_type = int(font["OS/2"].fsType) if "OS/2" in font else 0
            outlines = "glyf" if "glyf" in font else "cff" if ("CFF " in font or "CFF2" in font) else ""
            return fs_type, outlines
    except Exception:  # noqa: BLE001 - an unreadable file embeds nowhere
        return FS_RESTRICTED, ""


def file_embedding(path: Path) -> tuple[int, str]:
    """`_embedding` of a file (cached by path, size and mtime)."""
    try:
        stat = path.stat()
    except OSError:
        return FS_RESTRICTED, ""
    return _embedding(str(path), stat.st_size, int(stat.st_mtime))


def org_embeds(face: Face, fmt: str) -> bool:
    """Whether an organisation's face goes into a file of format `fmt` (`pdf`, `epub`, `docx`): the PDF and
    EPUB when its licence allows any embedding, Word when it allows an editable document and its outlines
    are TrueType. False for any other face (the registry's own rules apply to those)."""
    if not is_org_key(face.key):
        return False
    fs_type, outlines = file_embedding(face.files.regular)
    if fs_type & (FS_RESTRICTED | FS_BITMAP_ONLY) and not fs_type & (FS_PREVIEW_PRINT | FS_EDITABLE):
        return False
    if fmt in ("pdf", "epub"):
        return True
    if fmt == "docx":
        return outlines == "glyf" and not fs_type & FS_PREVIEW_PRINT
    return False


def word_refusal(face: Face) -> str:
    """Why an organisation's face is not embedded in a Word file ('' when it is, or is no such face)."""
    if not is_org_key(face.key) or org_embeds(face, "docx"):
        return ""
    fs_type, outlines = file_embedding(face.files.regular)
    if outlines != "glyf":
        return "ملفه بصيغة OpenType CFF، وWord يضمّن خطوط TrueType وحدها"
    if fs_type & FS_PREVIEW_PRINT:
        return "ترخيصه يسمح بالعرض والطباعة فقط"
    return "ترخيصه لا يسمح"


def org_font_url(face_key: str, path: Path) -> str:
    """The address the browser loads an organisation face's file from (`accounts:font_file`)."""
    from django.urls import reverse

    return reverse("accounts:font_file", args=[org_font_id(face_key), path.name])


def org_sample_css(organization) -> str:
    """`@font-face` rules showing each active face of an organisation in its own face (`nk-org-<pk>`):
    the font menus' samples and the organisation page."""
    rules = []
    for row in org_fonts(organization):
        files = _org_files(row)
        if files is None:
            continue
        for weight, style, path in files.styles():
            url = org_font_url(row.key, path)
            rules.append(
                f'@font-face {{ font-family: "{sample_family(row.pk)}"; src: url("{url}");'
                f" font-weight: {weight}; font-style: {style}; font-display: swap; }}"
            )
    return "\n".join(rules)


def locate(key: str) -> FontFiles | None:
    """The files of face `key` on this Mac (an organisation's: its stored files), None when it is unknown,
    removed, or its regular file is missing."""
    spec = FONTS.get(key)
    if spec is None:
        return _org_files(_org_row(key)) if is_org_key(key) else None
    if spec.vendored:
        dirs = [VENDORED_DIR]
    else:
        dirs = [*font_dirs(), *(Path(os.path.expanduser(d)) for d in spec.extra_dirs)]
    regular = _find(spec.regular, dirs)
    if regular is None:
        return None
    return FontFiles(
        regular=regular,
        bold=_find(spec.bold, dirs),
        italic=_find(spec.italic, dirs) if spec.italic else None,
        bold_italic=_find(spec.bold_italic, dirs) if spec.bold_italic else None,
    )


def font_status(organization=None, keep=()) -> list[dict]:
    """Every face of the registry for the stylesheet panel: `{key, name, label, family, installed, latin,
    licence, vendored, bold, org}` (the font menus show «غير مثبّت» for a face not installed), then the
    active faces of `organization` (D98: `org`, `arabic`, `removed`; `family` is the `nk-org-<pk>` the
    menus show it in), and any face of `keep` (the book's own) that was removed since, flagged `removed`."""
    out = []
    for key, spec in FONTS.items():
        files = locate(key)
        out.append(
            {
                "key": key,
                "name": spec.name,
                "label": spec.label,
                "family": spec.family,
                "installed": files is not None,
                "bold": files is not None and files.bold is not None,
                "latin": spec.latin,
                "arabic": True,
                "licence": spec.licence,
                "vendored": spec.vendored,
                "org": False,
                "removed": False,
                "message": "" if files is not None else MISSING_MESSAGE,
            }
        )
    rows = list(org_fonts(organization))
    listed = {row.key for row in rows}
    for key in dict.fromkeys(keep):
        if is_org_key(key) and key not in listed:
            row = _org_row(key)
            rows.append(row if row is not None else key)
            listed.add(key)
    for row in rows:
        if isinstance(row, str):  # deleted for good: only its key is left
            out.append(
                {
                    "key": row,
                    "name": DELETED_NAME,
                    "label": DELETED_NAME,
                    "family": "",
                    "installed": False,
                    "bold": False,
                    "latin": False,
                    "arabic": False,
                    "licence": "",
                    "vendored": False,
                    "org": True,
                    "removed": True,
                    "message": REMOVED_MESSAGE,
                }
            )
            continue
        files = _org_files(row)
        removed = row.removed_at is not None
        out.append(
            {
                "key": row.key,
                "name": row.name,
                "label": row.name,
                "family": sample_family(row.pk),
                "installed": files is not None,
                "bold": files is not None and files.bold is not None,
                "latin": row.latin,
                "arabic": row.arabic,
                "licence": row.licence,
                "vendored": False,
                "org": True,
                "removed": removed,
                "message": REMOVED_MESSAGE if removed else ("" if files is not None else MISSING_MESSAGE),
            }
        )
    return out


@dataclass(frozen=True)
class Face:
    """A face ready to render: the key used (Amiri when the requested one is missing) and its files."""

    requested: str
    key: str
    name: str
    family: str
    files: FontFiles
    latin: bool
    licence: str = ""  # an organisation's face: its licence notice (D98: the EPUB carries it)

    @property
    def fallback(self) -> bool:
        """True when the requested face was missing and another one stands in."""
        return self.requested != self.key


@dataclass(frozen=True)
class ResolvedFonts:
    """The three faces of a stylesheet and the missing ones (`[{field, key, name, message, fallback}]`)."""

    body: Face
    latin: Face
    heading: Face
    missing: list[dict] = field(default_factory=list)

    def fingerprint(self) -> list:
        """What identifies the files (path, size, mtime): part of the preview hash."""
        out = []
        for face in (self.body, self.latin, self.heading):
            for path in (face.files.regular, face.files.bold, face.files.italic, face.files.bold_italic):
                if path is None:
                    out.append(None)
                    continue
                try:
                    stat = path.stat()
                    out.append([str(path), stat.st_size, int(stat.st_mtime)])
                except OSError:
                    out.append([str(path), 0, 0])
        return out


def _face(field_name: str, key: str, missing: list[dict], latin_only: bool = False) -> Face:
    org = is_org_key(key)
    row = _org_row(key) if org else None  # one query for an organisation's face (D98)
    spec = FONTS.get(key) if not org else (_org_spec(row) if row is not None else None)
    valid = spec is not None and (not latin_only or spec.latin)
    files = (_org_files(row) if org else locate(key)) if valid else None
    used = key
    if files is None:
        for used in (DEFAULT_LATIN, FALLBACK) if latin_only else (FALLBACK,):
            files = locate(used)
            if files is not None:
                break
        if files is None:  # the vendored face is part of the repository
            raise FileNotFoundError("Amiri is missing from static/fonts/amiri/")
        removed = org and (row is None or row.removed_at is not None)
        missing.append(
            {
                "field": field_name,
                "key": key,
                "name": spec.name if spec else (DELETED_NAME if org else key),
                "message": REMOVED_MESSAGE if removed else MISSING_MESSAGE,
                "fallback": FONTS[used].name,
                "removed": removed,
            }
        )
        spec = FONTS[used]
    licence = spec.licence if spec.org else ""
    return Face(
        requested=key,
        key=used,
        name=spec.name,
        family=spec.family,
        files=files,
        latin=spec.latin,
        licence=licence,
    )


def resolve(body: str, latin: str, heading: str) -> ResolvedFonts:
    """The faces to render with: each requested face, or Amiri when its files are missing (the Latin
    face falls back to Times New Roman, then Amiri)."""
    missing: list[dict] = []
    return ResolvedFonts(
        body=_face("body_font", body, missing),
        latin=_face("latin_font", latin, missing, latin_only=True),
        heading=_face("heading_font", heading, missing),
        missing=missing,
    )


def _url(path: Path) -> str:
    return path.resolve().as_uri()


def parse_ranges(ranges: str) -> list[tuple[int, int]]:
    """`"U+0000-0040, U+00D7"` → `[(0x0, 0x40), (0xD7, 0xD7)]`."""
    out: list[tuple[int, int]] = []
    for item in ranges.split(","):
        item = item.strip().upper().removeprefix("U+")
        if not item:
            continue
        lo, _sep, hi = item.partition("-")
        out.append((int(lo, 16), int(hi or lo, 16)))
    return out


def format_ranges(codes) -> str:
    """Code points → a CSS `unicode-range` value (`U+0600-06FF, U+FB50-FDFF`)."""
    out: list[str] = []
    start = previous = None
    for code in sorted(codes):
        if previous is not None and code == previous + 1:
            previous = code
            continue
        if start is not None:
            out.append(f"U+{start:04X}" if start == previous else f"U+{start:04X}-{previous:04X}")
        start = previous = code
    if start is not None:
        out.append(f"U+{start:04X}" if start == previous else f"U+{start:04X}-{previous:04X}")
    return ", ".join(out)


@lru_cache(maxsize=64)
def _cmap(path: str, size: int, mtime: int) -> frozenset[int]:
    from fontTools.ttLib import TTFont

    try:
        with TTFont(path, lazy=True, fontNumber=0) as font:
            return frozenset(font.getBestCmap() or {})
    except Exception:  # noqa: BLE001 - an unreadable table: keep the declared ranges
        return frozenset()


@lru_cache(maxsize=64)
def _tabular(path: str, size: int, mtime: int) -> bool:
    from fontTools.ttLib import TTFont

    try:
        with TTFont(path, lazy=True, fontNumber=0) as font:
            cmap = font.getBestCmap() or {}
            widths = {font["hmtx"][cmap[ord(digit)]][0] for digit in "0123456789" if ord(digit) in cmap}
            return len(widths) == 1
    except Exception:  # noqa: BLE001 - unknown: not tabular
        return False


def tabular_digits(path: Path) -> bool:
    """True when the ten digits of a font file are all as wide (a footnote number can change without
    moving a line)."""
    try:
        stat = path.stat()
    except OSError:
        return False
    return _tabular(str(path), stat.st_size, int(stat.st_mtime))


def number_face(fonts: ResolvedFonts) -> Face:
    """The face the footnote numbers print in: the body face, or the Latin face when the body face has no
    digits (Lotus)."""
    return fonts.body if fonts.body.latin else fonts.latin


def coverage(path: Path) -> frozenset[int]:
    """The code points a font file maps to glyphs (its `cmap`; cached by path, size and mtime)."""
    try:
        stat = path.stat()
    except OSError:
        return frozenset()
    return _cmap(str(path), stat.st_size, int(stat.st_mtime))


def face_ranges(path: Path, ranges: str) -> str:
    """`ranges` narrowed to the characters the file has: a character the face lacks (Lotus has no
    en dash, curly quotes, ellipsis or alef wasla) falls through to the next family of the list —
    the Latin face — instead of printing as a missing-glyph box."""
    have = coverage(path)
    if not have:
        return ranges
    wanted = [code for lo, hi in parse_ranges(ranges) for code in range(lo, hi + 1) if code in have]
    return format_ranges(wanted) or ranges


def _rules(family: str, face: Face, ranges: str | None) -> list[str]:
    rules = []
    for weight, style, path in face.files.styles():
        extra = f" unicode-range: {face_ranges(path, ranges)};" if ranges else ""
        rules.append(
            f'@font-face {{ font-family: "{family}"; src: url("{_url(path)}"); font-weight: {weight};'
            f" font-style: {style};{extra} }}"
        )
    return rules


def role_ranges(face: Face, latin: Face) -> str | None:
    """The `unicode-range` of an Arabic role (`nk-body`, `nk-heading`): Latin letters left out (and the
    digits for a face without them), or None — the whole face — when the role's regular file is the
    Latin face's own file (D60: the face sets its own Latin letters)."""
    if face.files.regular == latin.files.regular:
        return None
    return _ARABIC_RANGES if face.latin else _ARABIC_RANGES_NO_DIGITS


def font_face_css(fonts: ResolvedFonts) -> str:
    """`@font-face` rules for `nk-body`, `nk-heading` (Arabic faces: Latin letters left out, and every
    character the face's file lacks; `role_ranges`) and `nk-latin`; file URLs are absolute `file://`
    paths."""
    rules: list[str] = []
    for family, face in (("nk-body", fonts.body), ("nk-heading", fonts.heading)):
        rules += _rules(family, face, role_ranges(face, fonts.latin))
    rules += _rules("nk-latin", fonts.latin, None)
    return "\n".join(rules)


def families(role: str = "body") -> str:
    """The CSS family list of a role: `"nk-body", "nk-latin", serif` (or `nk-heading`)."""
    first = "nk-heading" if role == "heading" else "nk-body"
    return f'"{first}", "nk-latin", serif'


# ====================================================================== the same faces in the browser (D47)


@lru_cache(maxsize=64)
def _names(path: str, size: int, mtime: int) -> tuple[str, ...]:
    """The full name and the PostScript name of a font file (what CSS `local()` matches)."""
    from fontTools.ttLib import TTFont

    try:
        with TTFont(path, lazy=True, fontNumber=0) as font:
            table = font["name"]
            out = []
            for name_id in (4, 6):
                record = table.getDebugName(name_id)
                if record and record not in out:
                    out.append(record)
            return tuple(out)
    except Exception:  # noqa: BLE001 - an unreadable table: the family name stands in
        return ()


def local_names(path: Path) -> tuple[str, ...]:
    """`_names` of a file (cached by path, size and mtime)."""
    try:
        stat = path.stat()
    except OSError:
        return ()
    return _names(str(path), stat.st_size, int(stat.st_mtime))


def browser_faces(fonts: ResolvedFonts) -> list[dict]:
    """The render's faces for the live pages (D47): `[{family, weight, style, src: [...], unicode_range}]`
    — the same families (`nk-body`, `nk-heading`, `nk-latin`), files and unicode ranges (`role_ranges`) as
    the PDF. The vendored Amiri is served from /static/; an installed face is used through `local()` (its
    files stay in the Mac's font folders, never served); an organisation's face (D98) only from its stored
    file (`accounts:font_file`)."""
    from django.templatetags.static import static

    out: list[dict] = []
    for family, face, ranges in (
        ("nk-body", fonts.body, role_ranges(fonts.body, fonts.latin)),
        ("nk-heading", fonts.heading, role_ranges(fonts.heading, fonts.latin)),
        ("nk-latin", fonts.latin, None),
    ):
        for weight, style, path in face.files.styles():
            if is_org_key(face.key):
                out.append(
                    {
                        "family": family,
                        "weight": int(weight),
                        "style": style,
                        "src": [f'url("{org_font_url(face.key, path)}")'],
                        "unicode_range": face_ranges(path, ranges) if ranges else None,
                    }
                )
                continue
            src = [f'local("{name}")' for name in local_names(path)]
            if FONTS[face.key].vendored:  # the very file the PDF embeds first (an installed copy may differ)
                try:
                    relative = path.resolve().relative_to(VENDORED_DIR.parent.resolve())
                    src.insert(0, f'url("{static(str(relative))}")')
                except ValueError:
                    pass
            out.append(
                {
                    "family": family,
                    "weight": int(weight),
                    "style": style,
                    "src": src or [f'local("{face.family}")'],
                    "unicode_range": face_ranges(path, ranges) if ranges else None,
                }
            )
    return out


def browser_font_css(fonts: ResolvedFonts) -> str:
    """`browser_faces` as `@font-face` rules, ready for a `<style>`."""
    rules = []
    for face in browser_faces(fonts):
        extra = f" unicode-range: {face['unicode_range']};" if face["unicode_range"] else ""
        rules.append(
            f'@font-face {{ font-family: "{face["family"]}"; src: {", ".join(face["src"])};'
            f" font-weight: {face['weight']}; font-style: {face['style']}; font-display: block;{extra} }}"
        )
    return "\n".join(rules)
