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


def locate(key: str) -> FontFiles | None:
    """The files of face `key` on this Mac, None when it is unknown or its regular file is missing."""
    spec = FONTS.get(key)
    if spec is None:
        return None
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


def font_status() -> list[dict]:
    """Every face of the registry for the stylesheet panel: `{key, name, label, family, installed, latin,
    licence, vendored, bold}` (the font menus show «غير مثبّت» for a face not installed)."""
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
                "licence": spec.licence,
                "vendored": spec.vendored,
                "message": "" if files is not None else MISSING_MESSAGE,
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
    valid = key in FONTS and (not latin_only or FONTS[key].latin)
    files = locate(key) if valid else None
    used = key
    if files is None:
        for used in (DEFAULT_LATIN, FALLBACK) if latin_only else (FALLBACK,):
            files = locate(used)
            if files is not None:
                break
        if files is None:  # the vendored face is part of the repository
            raise FileNotFoundError("Amiri is missing from static/fonts/amiri/")
        spec = FONTS.get(key)
        missing.append(
            {
                "field": field_name,
                "key": key,
                "name": spec.name if spec else key,
                "message": MISSING_MESSAGE,
                "fallback": FONTS[used].name,
            }
        )
    spec = FONTS[used]
    return Face(requested=key, key=used, name=spec.name, family=spec.family, files=files, latin=spec.latin)


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
    files stay in the Mac's font folders, never served)."""
    from django.templatetags.static import static

    out: list[dict] = []
    for family, face, ranges in (
        ("nk-body", fonts.body, role_ranges(fonts.body, fonts.latin)),
        ("nk-heading", fonts.heading, role_ranges(fonts.heading, fonts.latin)),
        ("nk-latin", fonts.latin, None),
    ):
        for weight, style, path in face.files.styles():
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
