"""The PDF text layer (PHASE7_SPEC §4.7, owner question 1: reverse): what a viewer copies from the print and
the screen PDF.

WeasyPrint writes one `/ToUnicode` entry per glyph, in logical order: a lam-alef ligature glyph maps to
«لا» (U+0644 U+0627). The glyphs of a right-to-left run sit in the file in visual order, so a viewer that
reorders the run character by character after the lookup (Chrome's PDFium, Firefox's pdf.js, poppler,
MuPDF) turns «لا» into «ال»: on book 23, 66 % of the words copied right. Apple's engine (Preview, Safari)
reorders glyph by glyph and copies right as the file is (99 %).

`fix_text_layer` reverses, in every font's ToUnicode CMap, the code points of each entry that maps one
glyph to two or more characters when every character is Arabic script (`is_arabic`: U+0600–06FF,
0750–077F, 08A0–08FF, FB50–FDFF, FE70–FEFF, their combining marks included; the Arabic-Indic digits
left out: a digit pair runs left to right, like the Western digits); Latin ligatures («fi», «fl») and
single-character entries keep theirs. Measured on book 23: PDFium 66 % → 99 %, MuPDF 65 % → 99 %,
PDFKit 99 % → 66 % (the owner's choice: most readers use the first three; on the Mac, copy in Chrome).

The same rule serves both PDFs, applied by `PdfExporter.export` after the write and before the check,
so the audit reads the file as it ships. `bfchar` entries (what WeasyPrint writes) and `bfrange` entries
(`<lo> <hi> <dst>` and `<lo> <hi> [<dst> …]`) are handled; a range with one multi-character Arabic
destination over several codes becomes the array of its reversed destinations (the PDF rule increments
the destination's last byte, which a plain reversal would move to the first character). A file with no
such entry comes back as it was, byte for byte; otherwise PyMuPDF replaces the changed streams
(`update_stream`, compressed) and writes the file again (`SAVE_OPTIONS`: the same bytes for the same
input, the file's /ID kept), every other object — the outline, the links, the boxes, the metadata —
unchanged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

ARABIC_RANGES: tuple[tuple[int, int], ...] = (
    (0x0600, 0x06FF),
    (0x0750, 0x077F),
    (0x08A0, 0x08FF),
    (0xFB50, 0xFDFF),
    (0xFE70, 0xFEFF),
)
ARABIC_DIGITS: tuple[tuple[int, int], ...] = ((0x0660, 0x0669), (0x06F0, 0x06F9))

HEX = r"[0-9A-Fa-f\s]*"
BFCHAR_BLOCK = re.compile(r"(beginbfchar)(.*?)(endbfchar)", re.S)
BFRANGE_BLOCK = re.compile(r"(beginbfrange)(.*?)(endbfrange)", re.S)
BFCHAR_ENTRY = re.compile(rf"<({HEX})>(\s*)<({HEX})>")
BFRANGE_ENTRY = re.compile(rf"<({HEX})>(\s*)<({HEX})>(\s*)(<{HEX}>|\[[^\]]*\])")
ARRAY_ITEM = re.compile(rf"<({HEX})>")

# How a changed file is written: its /ID kept, the same bytes for the same input, object streams and
# deflate as WeasyPrint writes them (a plain save grows book 23's screen PDF from 56 to 69 KB), and the
# objects the rewrite leaves unreferenced (the old cross-reference and object streams) dropped.
SAVE_OPTIONS = {"garbage": 1, "deflate": 1, "use_objstms": 1, "no_new_id": 1}


def is_arabic(char: str) -> bool:
    """True for a character of the Arabic script blocks (`ARABIC_RANGES`, the combining marks inside them
    included), the Arabic-Indic digits left out (`ARABIC_DIGITS`: they run left to right)."""
    code = ord(char)
    if any(low <= code <= high for low, high in ARABIC_DIGITS):
        return False
    return any(low <= code <= high for low, high in ARABIC_RANGES)


def decode(hex_text: str) -> str | None:
    """A CMap destination (`<06440627>`'s hex, UTF-16BE) as text; None when it is not UTF-16BE."""
    digits = "".join(hex_text.split())
    if not digits or len(digits) % 4:
        return None
    try:
        return bytes.fromhex(digits).decode("utf-16-be")
    except ValueError:  # odd digits, a lone surrogate
        return None


def encode(text: str) -> str:
    """Text as a CMap destination's hex (UTF-16BE, lower case, as WeasyPrint writes it)."""
    return text.encode("utf-16-be").hex()


def reversible(text: str | None) -> bool:
    """True for a destination the rule reverses: two or more characters, every one Arabic (`is_arabic`)."""
    return text is not None and len(text) > 1 and all(is_arabic(char) for char in text)


def reverse_destination(hex_text: str) -> str | None:
    """The reversed hex of a destination the rule reverses (`reversible`), else None (left as it is)."""
    text = decode(hex_text)
    return encode(text[::-1]) if reversible(text) else None


def _range_destinations(low: str, high: str, first: str) -> list[str] | None:
    """The destinations of `<low> <high> <first>` one by one (the last byte of `first` incremented per
    code, PDF 32000 §9.10.3), or None when the codes do not read as hex or run backwards."""
    try:
        start, end = int("".join(low.split()), 16), int("".join(high.split()), 16)
        raw = bytearray(bytes.fromhex("".join(first.split())))
    except ValueError:
        return None
    if not raw or end < start or end - start > 0xFF:
        return None
    out = []
    for step in range(end - start + 1):
        item = bytearray(raw)
        item[-1] = (raw[-1] + step) & 0xFF
        out.append(item.hex())
    return out


@dataclass
class CmapFix:
    """A CMap after the rule: its text and how many entries changed (a range counts once per code)."""

    text: str
    changed: int = 0


def reverse_cmap(cmap: str) -> CmapFix:
    """Apply the rule (module docstring) to a ToUnicode CMap's text: every `bfchar` and `bfrange` entry
    whose destination is two or more Arabic characters gets them in reverse order; everything else —
    the header, the code space, single characters, Latin ligatures, spacing — is kept as it is."""
    changed = 0

    def bfchar(match: re.Match) -> str:
        nonlocal changed
        source, gap, destination = match.groups()
        flipped = reverse_destination(destination)
        if flipped is None:
            return match.group(0)
        changed += 1
        return f"<{source}>{gap}<{flipped}>"

    def array_item(match: re.Match) -> str:
        nonlocal changed
        flipped = reverse_destination(match.group(1))
        if flipped is None:
            return match.group(0)
        changed += 1
        return f"<{flipped}>"

    def bfrange(match: re.Match) -> str:
        nonlocal changed
        low, gap, high, gap2, destination = match.groups()
        if destination.startswith("["):
            return f"<{low}>{gap}<{high}>{gap2}{ARRAY_ITEM.sub(array_item, destination)}"
        first = destination[1:-1]
        if not reversible(decode(first)):
            return match.group(0)
        if "".join(low.split()).lower() == "".join(high.split()).lower():
            changed += 1
            return f"<{low}>{gap}<{high}>{gap2}<{reverse_destination(first)}>"
        items = _range_destinations(low, high, first)
        if items is None:
            return match.group(0)
        flipped = [reverse_destination(item) or item for item in items]
        changed += sum(1 for old, new in zip(items, flipped, strict=True) if old != new)
        return f"<{low}>{gap}<{high}>{gap2}[{' '.join(f'<{item}>' for item in flipped)}]"

    def block(pattern: re.Pattern, entry: re.Pattern, handler):
        def replace(match: re.Match) -> str:
            begin, body, end = match.groups()
            return f"{begin}{entry.sub(handler, body)}{end}"

        return lambda text: pattern.sub(replace, text)

    text = block(BFCHAR_BLOCK, BFCHAR_ENTRY, bfchar)(cmap)
    text = block(BFRANGE_BLOCK, BFRANGE_ENTRY, bfrange)(text)
    return CmapFix(text, changed)


def tounicode_streams(document) -> list[int]:
    """The xrefs of every `/ToUnicode` stream a font of the file points to, once each, in object order."""
    streams: list[int] = []
    for xref in range(1, document.xref_length()):
        try:
            kind, value = document.xref_get_key(xref, "ToUnicode")
        except Exception:  # noqa: BLE001 - a broken object holds no font
            continue
        if kind != "xref":
            continue
        try:
            target = int(value.split()[0])
        except (ValueError, IndexError):
            continue
        if target not in streams:
            streams.append(target)
    return sorted(streams)


@dataclass
class TextLayerFix:
    """What `fix_text_layer` did: the file, the CMaps read and changed, and the entries reversed."""

    data: bytes
    cmaps: int = 0
    changed_cmaps: int = 0
    entries: int = 0


def fix_text_layer(pdf: bytes) -> TextLayerFix:
    """The PDF with the rule applied to every font's ToUnicode CMap (module docstring). A file that opens
    with no entry to reverse, or does not open, is returned as it is (the check step reports a file that
    does not open)."""
    import pymupdf

    try:
        document = pymupdf.open(stream=pdf, filetype="pdf")
    except Exception:  # noqa: BLE001 - the audit raises InvalidExport for it
        return TextLayerFix(pdf)
    with document:
        streams = tounicode_streams(document)
        fix = TextLayerFix(pdf, cmaps=len(streams))
        for xref in streams:
            try:
                cmap = document.xref_stream(xref)
            except Exception:  # noqa: BLE001 - an unreadable stream is left as it is
                continue
            if not cmap:
                continue
            result = reverse_cmap(cmap.decode("latin-1"))
            if not result.changed:
                continue
            document.update_stream(xref, result.text.encode("latin-1"))
            fix.changed_cmaps += 1
            fix.entries += result.changed
        if fix.entries:
            fix.data = document.tobytes(**SAVE_OPTIONS)
    log.debug("text layer: %s entries reversed in %s of %s CMaps", fix.entries, fix.changed_cmaps, fix.cmaps)
    return fix


__all__ = [
    "ARABIC_RANGES",
    "CmapFix",
    "TextLayerFix",
    "fix_text_layer",
    "is_arabic",
    "reverse_cmap",
    "reversible",
    "tounicode_streams",
]
