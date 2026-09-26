"""Checks of a generated .docx (PHASE6_SPEC §5.1 `check`, §11.1): the ECMA-376 schema over every
WordprocessingML part, and the integrity checks the schema cannot make.

`validate_package(data)` validates every `word/*.xml` part against the vendored XSDs (`xsd/`, loaded once
per process; Word 2010+ extension namespaces declared `mc:Ignorable` are dropped first, as
`playground/word/validate_spike.py` did). `integrity_errors(data)` checks that every style, footnote,
comment, relationship, bookmark and field the parts refer to exists exactly as it should. Both return a
list of messages (empty for a sound file); an export whose file has any is failed (`INVALID_FILE`).
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from lxml import etree

from .ooxml import R_NS, W_NS, local

XSD_DIR = Path(__file__).resolve().parent / "xsd"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_WORD_XML = re.compile(r"^word/[^/]+\.xml$")
_INSTR_TARGET = re.compile(r"^\s*(PAGEREF|HYPERLINK)\s+(\S+)")
_RID_ATTR = f"{{{R_NS}}}id"
_REL_ATTRS = (_RID_ATTR, f"{{{R_NS}}}embed", f"{{{R_NS}}}link")  # r:embed / r:link: a picture's part (D80)
_STORIES = ("word/document.xml", "word/footnotes.xml", "word/comments.xml")


@lru_cache(maxsize=1)
def wml_schema() -> etree.XMLSchema:
    """The WordprocessingML schema, parsed once per process."""
    return etree.XMLSchema(etree.parse(str(XSD_DIR / "wml.xsd")))


def _strip_ignorable(root: etree._Element) -> None:
    """Drop the elements and attributes of the namespaces `mc:Ignorable` names (the XSD predates them)."""
    ignorable = (root.get(f"{{{MC_NS}}}Ignorable") or "").split()
    known = {prefix: uri for prefix, uri in root.nsmap.items() if prefix}
    drop = {known[prefix] for prefix in ignorable if prefix in known}
    if not drop and not any(attr.startswith(f"{{{MC_NS}}}") for attr in root.attrib):
        return
    drop.add(MC_NS)
    for node in list(root.iter()):
        if not isinstance(node.tag, str):
            continue
        if etree.QName(node).namespace in drop and node.getparent() is not None:
            node.getparent().remove(node)
            continue
        for attr in list(node.attrib):
            if attr.startswith("{") and attr[1:].split("}")[0] in drop:
                del node.attrib[attr]


def word_parts(archive: zipfile.ZipFile) -> list[str]:
    """The WordprocessingML parts of the package (`word/*.xml`), in zip order."""
    return [name for name in archive.namelist() if _WORD_XML.match(name)]


def validate_package(data: bytes) -> list[str]:
    """Schema errors of every `word/*.xml` part: `["word/document.xml:12: message", …]`."""
    errors: list[str] = []
    schema = wml_schema()
    with zipfile.ZipFile(BytesIO(data)) as archive:
        for name in word_parts(archive):
            try:
                root = etree.fromstring(archive.read(name))
            except etree.XMLSyntaxError as exc:
                errors.append(f"{name}: {exc}")
                continue
            _strip_ignorable(root)
            if not schema.validate(root):
                for error in list(schema.error_log)[:20]:
                    errors.append(f"{name}:{error.line}: {error.message}")
    return errors


# ====================================================================== integrity


class _Reader:
    """The parsed parts of a package, read once."""

    def __init__(self, data: bytes):
        self.archive = zipfile.ZipFile(BytesIO(data))
        self.names = set(self.archive.namelist())
        self.xml: dict[str, etree._Element] = {}

    def part(self, name: str) -> etree._Element | None:
        if name not in self.names:
            return None
        if name not in self.xml:
            self.xml[name] = etree.fromstring(self.archive.read(name))
        return self.xml[name]

    def rels(self, name: str) -> dict[str, tuple[str, str, bool]]:
        """`rId → (type, target, external)` of a part's relationships."""
        folder, _slash, base = name.rpartition("/")
        rels_name = f"{folder}/_rels/{base}.rels" if folder else f"_rels/{base}.rels"
        node = self.part(rels_name)
        out: dict[str, tuple[str, str, bool]] = {}
        if node is None:
            return out
        for rel in node.iter(f"{{{_RELS_NS}}}Relationship"):
            out[rel.get("Id") or ""] = (
                rel.get("Type") or "",
                rel.get("Target") or "",
                rel.get("TargetMode") == "External",
            )
        return out


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _val(node: etree._Element, name: str = "val") -> str:
    return node.get(_w(name)) or ""


def _check_content_types(reader: _Reader, errors: list[str]) -> None:
    types = reader.part("[Content_Types].xml")
    if types is None:
        errors.append("[Content_Types].xml is missing")
        return
    defaults = {
        n.get("Extension", "").lower(): n.get("ContentType") for n in types.iter(f"{{{_CT_NS}}}Default")
    }
    overrides = {
        n.get("PartName", "").lstrip("/"): n.get("ContentType") for n in types.iter(f"{{{_CT_NS}}}Override")
    }
    for name in reader.names:
        if name == "[Content_Types].xml" or name.endswith("/"):
            continue
        extension = name.rpartition(".")[2].lower()
        if name not in overrides and extension not in defaults:
            errors.append(f"{name}: no content type")
    for name in overrides:
        if name not in reader.names:
            errors.append(f"[Content_Types].xml: override for a missing part {name}")


def _check_rels(reader: _Reader, errors: list[str]) -> None:
    """Every `r:id` (and a picture's `r:embed` / `r:link`) resolves and every relationship target
    exists."""
    sources = ["_rels/.rels"] + [n for n in reader.names if n.endswith(".rels") and n != "_rels/.rels"]
    for rels_name in sources:
        node = reader.part(rels_name)
        if node is None:
            continue
        if rels_name == "_rels/.rels":
            base = ""
        else:
            folder = rels_name.rpartition("/_rels/")[0]
            base = folder
        for rel in node.iter(f"{{{_RELS_NS}}}Relationship"):
            if rel.get("TargetMode") == "External":
                continue
            target = rel.get("Target") or ""
            resolved = posixpath.normpath(posixpath.join(base, target)) if base else target.lstrip("/")
            if resolved not in reader.names:
                errors.append(f"{rels_name}: target {target!r} ({resolved}) is missing")
    for name in list(reader.names):
        if not name.endswith(".xml") or name.endswith(".rels") or not name.startswith("word/"):
            continue
        root = reader.part(name)
        if root is None:
            continue
        rels = reader.rels(name)
        for node in root.iter():
            if not isinstance(node.tag, str):
                continue
            for attr in _REL_ATTRS:
                rel_id = node.get(attr)
                if rel_id is not None and rel_id not in rels:
                    kind = attr.rpartition("}")[2]
                    errors.append(f"{name}: r:{kind} {rel_id!r} on <{local(node)}> has no relationship")


def _style_ids(reader: _Reader) -> set[str]:
    styles = reader.part("word/styles.xml")
    if styles is None:
        return set()
    return {s.get(_w("styleId")) or "" for s in styles.iter(_w("style"))}


def _check_styles(reader: _Reader, errors: list[str]) -> None:
    ids = _style_ids(reader)
    if not ids:
        errors.append("word/styles.xml: no styles")
        return
    for name in sorted(reader.names):
        if not _WORD_XML.match(name) or name == "word/styles.xml":
            continue
        root = reader.part(name)
        if root is None:
            continue
        for tag in ("pStyle", "rStyle"):
            for node in root.iter(_w(tag)):
                if _val(node) not in ids:
                    errors.append(f"{name}: {tag} {_val(node)!r} is not a style id")
    styles = reader.part("word/styles.xml")
    for node in styles.iter(_w("basedOn"), _w("next"), _w("link")):
        if _val(node) not in ids:
            errors.append(f"word/styles.xml: {local(node)} {_val(node)!r} is not a style id")


def _check_footnotes(reader: _Reader, errors: list[str]) -> None:
    notes = reader.part("word/footnotes.xml")
    document = reader.part("word/document.xml")
    settings = reader.part("word/settings.xml")
    if notes is None or document is None:
        errors.append("word/footnotes.xml or word/document.xml is missing")
        return
    defined: dict[str, str] = {}
    for note in notes.iter(_w("footnote")):
        note_id = _val(note, "id")
        if note_id in defined:
            errors.append(f"word/footnotes.xml: footnote id {note_id} twice")
        defined[note_id] = _val(note, "type") or "normal"
    for separator, kind in (("-1", "separator"), ("0", "continuationSeparator")):
        if defined.get(separator) != kind:
            errors.append(f"word/footnotes.xml: footnote {separator} must be the {kind}")
    if settings is not None:
        listed = {_val(n, "id") for n in settings.iter(_w("footnotePr")) for n in n.iter(_w("footnote"))}
        if listed != {"-1", "0"}:
            errors.append(
                f"word/settings.xml: footnotePr lists {sorted(listed)}, not the separators -1 and 0"
            )
    referenced: dict[str, int] = {}
    for ref in document.iter(_w("footnoteReference")):
        referenced[_val(ref, "id")] = referenced.get(_val(ref, "id"), 0) + 1
    for note_id, kind in defined.items():
        if kind != "normal":
            continue
        count = referenced.get(note_id, 0)
        if count != 1:
            errors.append(f"word/footnotes.xml: footnote {note_id} is referenced {count} times")
    for note_id in referenced:
        if note_id not in defined:
            errors.append(f"word/document.xml: footnoteReference {note_id} has no footnote")
    for ref in notes.iter(_w("footnoteRef")):
        if ref.getparent().getparent().getparent().tag != _w("footnote"):
            errors.append("word/footnotes.xml: a footnoteRef outside a footnote paragraph")


def _check_comments(reader: _Reader, errors: list[str]) -> None:
    comments = reader.part("word/comments.xml")
    defined = {_val(c, "id") for c in comments.iter(_w("comment"))} if comments is not None else set()
    starts: dict[str, int] = {}
    ends: dict[str, int] = {}
    refs: dict[str, int] = {}
    for name in _STORIES:
        root = reader.part(name)
        if root is None:
            continue
        for node in root.iter(_w("commentRangeStart"), _w("commentRangeEnd"), _w("commentReference")):
            table = {
                "commentRangeStart": starts,
                "commentRangeEnd": ends,
                "commentReference": refs,
            }[local(node)]
            table[_val(node, "id")] = table.get(_val(node, "id"), 0) + 1
    used = set(starts) | set(ends) | set(refs)
    if used and comments is None:
        errors.append("word/comments.xml is missing but the text has comment marks")
    for comment_id in sorted(used | defined, key=lambda x: (len(x), x)):
        if comment_id not in defined:
            errors.append(f"comment {comment_id} is marked but not defined")
            continue
        triple = (starts.get(comment_id, 0), ends.get(comment_id, 0), refs.get(comment_id, 0))
        if triple != (1, 1, 1):
            errors.append(f"comment {comment_id}: start/end/reference counts {triple}, not (1, 1, 1)")
    if comments is not None and not defined:
        errors.append("word/comments.xml is empty")


def _check_bookmarks_and_fields(reader: _Reader, errors: list[str]) -> None:
    names_by_id: dict[str, str] = {}
    seen_names: set[str] = set()
    ends: set[str] = set()
    stories = list(_STORIES) + sorted(
        n for n in reader.names if re.match(r"^word/(header|footer)\d+\.xml$", n)
    )
    for name in stories:
        root = reader.part(name)
        if root is None:
            continue
        for node in root.iter(_w("bookmarkStart")):
            mark_id, mark_name = _val(node, "id"), _val(node, "name")
            if mark_name in seen_names:
                errors.append(f"{name}: bookmark name {mark_name!r} twice")
            if mark_id in names_by_id:
                errors.append(f"{name}: bookmark id {mark_id} twice")
            if len(mark_name) > 40:
                errors.append(f"{name}: bookmark name {mark_name!r} is longer than 40 characters")
            seen_names.add(mark_name)
            names_by_id[mark_id] = mark_name
        for node in root.iter(_w("bookmarkEnd")):
            ends.add(_val(node, "id"))
    for mark_id in names_by_id:
        if mark_id not in ends:
            errors.append(f"bookmark {names_by_id[mark_id]!r} ({mark_id}) has no end")
    for mark_id in ends:
        if mark_id not in names_by_id:
            errors.append(f"bookmarkEnd {mark_id} has no start")
    for name in stories:
        root = reader.part(name)
        if root is None:
            continue
        for link in root.iter(_w("hyperlink")):
            anchor = link.get(_w("anchor"))
            if anchor is not None and anchor not in seen_names:
                errors.append(f"{name}: hyperlink anchor {anchor!r} is not a bookmark")
        # fields may span paragraphs (the TOC does): begin / separate / end are balanced over the story
        state: list[str] = []
        for node in root.iter(_w("fldChar"), _w("instrText")):
            if local(node) == "instrText":
                match = _INSTR_TARGET.match(node.text or "")
                if match and match.group(1) == "PAGEREF" and match.group(2) not in seen_names:
                    errors.append(f"{name}: PAGEREF target {match.group(2)!r} is not a bookmark")
                continue
            kind = node.get(_w("fldCharType"))
            if kind == "begin":
                state.append("begin")
            elif kind == "separate":
                if not state or state[-1] != "begin":
                    errors.append(f"{name}: a field separate without its begin")
                else:
                    state[-1] = "separate"
            elif kind == "end":
                if not state:
                    errors.append(f"{name}: a field end without its begin")
                else:
                    state.pop()
        if state:
            errors.append(f"{name}: {len(state)} field(s) without an end")


def _check_sections_and_settings(reader: _Reader, errors: list[str]) -> None:
    document = reader.part("word/document.xml")
    settings = reader.part("word/settings.xml")
    if document is not None:
        body = document.find(_w("body"))
        if body is None or not len(body):
            errors.append("word/document.xml: an empty body")
        else:
            if body[-1].tag != _w("sectPr"):
                errors.append("word/document.xml: the body does not end with a sectPr")
            sections = list(document.iter(_w("sectPr")))
            starts = [s for s in sections if any(n.get(_w("start")) for n in s.iter(_w("pgNumType")))]
            if len(starts) > 1:
                errors.append("word/document.xml: pgNumType@start on more than one section")
            elif starts and any(
                s.find(_w("headerReference")) is not None or s.find(_w("footerReference")) is not None
                for s in sections[: sections.index(starts[0])]
            ):
                errors.append("word/document.xml: pgNumType@start after a body section")
            for section in sections:
                if section.find(_w("bidi")) is None:
                    errors.append("word/document.xml: a sectPr without bidi")
                for ref in section.iter(_w("headerReference"), _w("footerReference")):
                    rel_id = ref.get(_RID_ATTR)
                    if not rel_id:
                        errors.append(f"word/document.xml: a {local(ref)} without r:id")
    if settings is None:
        errors.append("word/settings.xml is missing")
        return
    if settings.find(_w("updateFields")) is not None:
        errors.append("word/settings.xml: updateFields is set (Word would ask on open)")
    zoom = settings.find(_w("zoom"))
    if zoom is None or not zoom.get(_w("percent")):
        errors.append("word/settings.xml: zoom@percent is missing")


def integrity_errors(data: bytes) -> list[str]:
    """What the schema cannot check (§11.1 item 2), as messages; empty for a sound file."""
    errors: list[str] = []
    reader = _Reader(data)
    try:
        _check_content_types(reader, errors)
        _check_rels(reader, errors)
        _check_styles(reader, errors)
        _check_footnotes(reader, errors)
        _check_comments(reader, errors)
        _check_bookmarks_and_fields(reader, errors)
        _check_sections_and_settings(reader, errors)
    finally:
        reader.archive.close()
    return errors


def check(data: bytes, *, schema: bool = True) -> list[str]:
    """Schema (when asked) and integrity errors together."""
    errors = validate_package(data) if schema else []
    return errors + integrity_errors(data)
