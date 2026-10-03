"""The faces of a Word file (PHASE6_SPEC §5.6, §5.12, D57): which family each role uses, which
characters of a right-to-left run the role's Arabic face covers (the rest go to the Latin face, as the
preview's `unicode-range` sends them), the font table, and the embedding of Amiri.

Only open-licence faces are embedded (`EMBEDDABLE`, that is Amiri) and only whole, obfuscated as
ECMA-376 Part 1 §17.8.1 asks: the first 32 bytes of the file XORed with the 16-byte key, whose bytes
are the GUID's hex digits taken in reverse order. The key of a face is `uuid5("nassakh:<family>:<style>")`
so the same file gives the same bytes. An organisation's face (D98) is embedded the same way when its
licence allows an editable document and its outlines are TrueType (`publishing.fonts.org_embeds`); the
organisation confirmed its licence when it uploaded it.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from lxml import etree

from publishing import fonts as F

from .ooxml import REL_FONT, guid, root, w

EMBEDDABLE: frozenset[str] = frozenset({"amiri"})
ROLES: tuple[str, ...] = ("body", "latin", "heading", "number")
_RESTRICTED_LICENSE = 0x0002  # OS/2 fsType bit 1: restricted licence embedding
_KEY_NAMESPACE = uuid.NAMESPACE_URL
_RUNS = re.compile(r"1+|0+")


def font_key(family: str, style: str) -> str:
    """The obfuscation GUID of a face's file (`{…}`, upper case), stable for a family and style."""
    return guid(uuid.uuid5(_KEY_NAMESPACE, f"nassakh:{family}:{style}"))


def obfuscate(data: bytes, key: str) -> bytes:
    """`data` with its first 32 bytes XORed with the key (§17.8.1); applied twice it gives the file back."""
    digits = key.strip("{}").replace("-", "")
    if len(digits) != 32:
        raise ValueError(f"not a GUID: {key!r}")
    key_bytes = bytes(int(digits[(15 - i) * 2 : (15 - i) * 2 + 2], 16) for i in range(16))
    head = bytes(byte ^ key_bytes[i % 16] for i, byte in enumerate(data[:32]))
    return head + data[32:]


@lru_cache(maxsize=32)
def _os2(path: str, size: int, mtime: int) -> tuple[str, int]:
    """`(panose hex, fsType)` of a font file (zeros when unreadable)."""
    from fontTools.ttLib import TTFont

    try:
        with TTFont(path, lazy=True, fontNumber=0) as font:
            os2 = font["OS/2"]
            panose = os2.panose
            values = [
                getattr(panose, name, 0)
                for name in (
                    "bFamilyType",
                    "bSerifStyle",
                    "bWeight",
                    "bProportion",
                    "bContrast",
                    "bStrokeVariation",
                    "bArmStyle",
                    "bLetterForm",
                    "bMidline",
                    "bXHeight",
                )
            ]
            return "".join(f"{int(v) & 0xFF:02X}" for v in values), int(os2.fsType)
    except Exception:  # noqa: BLE001 - an unreadable table: no panose, embeddable
        return "00" * 10, 0


def os2_info(path: Path) -> tuple[str, int]:
    try:
        stat = path.stat()
    except OSError:
        return "00" * 10, 0
    return _os2(str(path), stat.st_size, int(stat.st_mtime))


def embeddable(face: F.Face) -> bool:
    """True for an open-licence face whose file allows embedding, or an organisation's face whose licence
    allows an editable document (D98)."""
    if F.is_org_key(face.key):
        return F.org_embeds(face, "docx")
    if face.key not in EMBEDDABLE:
        return False
    _panose, fs_type = os2_info(face.files.regular)
    return not (fs_type & _RESTRICTED_LICENSE)


@dataclass(frozen=True)
class EmbeddedFont:
    """One embedded file: its zip name, the family and style it serves, its key and obfuscated bytes."""

    name: str  # word/fonts/font1.odttf
    family: str
    style: str  # regular | bold
    key: str
    data: bytes
    rel_id: str = ""


class _CoverageTable(dict):
    """`str.translate` table of a role: code point → `1` (its Arabic face sets it) or `0`, filled on use."""

    def __init__(self, plan: FacePlan, role: str):
        super().__init__()
        self.plan = plan
        self.role = role

    def __missing__(self, code: int) -> str:
        value = "1" if self.plan.covers(chr(code), self.role) else "0"
        self[code] = value
        return value


class FacePlan:
    """The family of each role, which characters an Arabic face covers, and the font table."""

    def __init__(self, fonts: F.ResolvedFonts):
        self.fonts = fonts
        self.faces: dict[str, F.Face] = {
            "body": fonts.body,
            "latin": fonts.latin,
            "heading": fonts.heading,
            "number": F.number_face(fonts),
        }
        self.family: dict[str, str] = {role: face.family for role, face in self.faces.items()}
        latin_file = fonts.latin.files.regular
        self._coverage: dict[str, tuple[list[tuple[int, int]], frozenset[int]] | None] = {}
        for role in ("body", "heading"):
            face = self.faces[role]
            if face.files.regular == latin_file:
                self._coverage[role] = None  # the same file as the Latin face: it sets everything (D60)
                continue
            ranges = F.parse_ranges(F._ARABIC_RANGES if face.latin else F._ARABIC_RANGES_NO_DIGITS)
            self._coverage[role] = (ranges, F.coverage(face.files.regular))
        self._tables = {role: _CoverageTable(self, role) for role in self._coverage}

    def covers(self, char: str, role: str = "body") -> bool:
        """True when the role's Arabic face sets `char` in the preview (else the Latin face does)."""
        coverage = self._coverage.get(role if role in self._coverage else "body")
        if coverage is None:
            return True
        ranges, have = coverage
        code = ord(char)
        if not any(lo <= code <= hi for lo, hi in ranges):
            return False
        return not have or code in have

    def split(self, text: str, role: str = "body") -> list[tuple[str, bool]]:
        """A right-to-left piece cut into `(text, covered)` runs: what the role's face covers and what
        goes to the Latin face (`rStyle NkLatin`)."""
        if not text:
            return []
        key = role if role in self._coverage else "body"
        if self._coverage.get(key) is None:
            return [(text, True)]
        marks = text.translate(self._tables[key])
        if "0" not in marks:
            return [(text, True)]
        return [(text[m.start() : m.end()], m.group()[0] == "1") for m in _RUNS.finditer(marks)]

    def families(self) -> list[str]:
        """The distinct families, in role order."""
        out: list[str] = []
        for role in ROLES:
            family = self.family[role]
            if family not in out:
                out.append(family)
        return out

    def embedded_faces(self) -> list[F.Face]:
        """The distinct faces to embed (one per family)."""
        out: list[F.Face] = []
        for role in ROLES:
            face = self.faces[role]
            if embeddable(face) and all(face.family != other.family for other in out):
                out.append(face)
        return out

    def not_embedded(self) -> list[F.Face]:
        """The distinct faces named but not embedded."""
        out: list[F.Face] = []
        for role in ROLES:
            face = self.faces[role]
            if not embeddable(face) and all(face.family != other.family for other in out):
                out.append(face)
        return out

    def embedded_files(self, embed: bool = True) -> list[EmbeddedFont]:
        """The obfuscated files to write (`word/fonts/fontN.odttf`), regular then bold per family."""
        if not embed:
            return []
        out: list[EmbeddedFont] = []
        for face in self.embedded_faces():
            for style, path in (("regular", face.files.regular), ("bold", face.files.bold)):
                if path is None:
                    continue
                key = font_key(face.family, style)
                out.append(
                    EmbeddedFont(
                        name=f"word/fonts/font{len(out) + 1}.odttf",
                        family=face.family,
                        style=style,
                        key=key,
                        data=obfuscate(path.read_bytes(), key),
                        rel_id=f"rId{len(out) + 1}",
                    )
                )
        return out

    def font_table(self, embedded: list[EmbeddedFont]) -> etree._Element:
        """`word/fontTable.xml`: one `w:font` per family (panose, charset, family, pitch, and the
        embed references of an embedded face)."""
        by_family: dict[str, list[EmbeddedFont]] = {}
        for item in embedded:
            by_family.setdefault(item.family, []).append(item)
        fonts = root("fonts")
        for family in self.families():
            face = next(face for face in self.faces.values() if face.family == family)
            panose, _fs_type = os2_info(face.files.regular)
            node = w(
                "font",
                w("panose1", val=panose),
                w("charset", val="00"),
                w("family", val="auto"),
                w("pitch", val="variable"),
                name=family,
            )
            for item in by_family.get(family, []):
                tag = "embedRegular" if item.style == "regular" else "embedBold"
                node.append(w(tag, r_id=item.rel_id, fontKey=item.key))
            fonts.append(node)
        return fonts


def font_rels(embedded: list[EmbeddedFont]):
    """The relationships of `fontTable.xml` to its embedded files."""
    from .ooxml import Rel

    return [Rel(item.rel_id, REL_FONT, item.name.removeprefix("word/")) for item in embedded]
