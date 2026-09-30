"""The assembly pipeline (PHASE4_SPEC §2): a book's page lines → one ProseMirror manuscript document.

Pure functions over plain dataclasses; no ORM, no I/O. `assembly.services.load_book` builds the
inputs (`PageIn` / `LineIn`, boxes as ratios of the page) and `assemble` runs the steps in order:

1. `select_pages`        which pages are in, which are skipped (and break the join chain)   §2.1
   `drop_running_heads`  the book's running heads the layout left in the body (D49)
2. `split_paragraphs`    body lines of one page → paragraphs and headings (geometry first)   §2.3
3. `join_pages`          seams: the last paragraph of a page joined with the next page's first §2.4
4. `page_notes`, `link_footnotes`, `attach_orphans`, `number_footnotes`                     §2.5
   `stray_notes`         marker-initial paragraphs a page's open call may own (D74)
5. `suggest_headings`    heading suggestions, `no_headings`                                  §2.6
6. `normalize_rich`, `move_leading_marks`   typography in the derived text (D37)            §2.2
7. `build_document`, `compute_stats`                                                          §2.8–§2.10

Text flows through the steps as `Rich`: a string plus one `Meta` per character (source line,
uncertain flag, inline node), so punctuation can move and spaces can go while every character keeps
its source line and uncertain words keep their mark. Inline nodes sit in the string as one private
use character each (`PB` a page break at a join, `FN` a footnote reference) whose `Meta.node` holds
the node. Diacritics are never touched; stored lines are never changed (the pipeline only reads).
"""

from __future__ import annotations

import dataclasses
import re
import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from core.arabic import normalize as normalize_arabic
from core.arabic import to_western_digits

# ====================================================================== constants

BODY = "body"
FOOTNOTE = "footnote"
ROLE_BODY = "body"
ROLE_VERSE = "verse"  # D74: a verse line is never joined with another line
HEADING_LEVELS: dict[str, int] = {"heading": 1, "subheading": 2}
# The roles a `LineIn` carries (the loader folds `main` and `footnote` into `body`: the line's kind says
# whether it is a note, `ocr.services.line_kind`).
LINE_ROLES: frozenset[str] = frozenset({ROLE_BODY, ROLE_VERSE, *HEADING_LEVELS})
VERSE_STYLE = "verse"  # the paragraph style of a verse line (`editor.document.PARAGRAPH_STYLES`)

REVIEWED_STATUSES: frozenset[str] = frozenset({"reviewed", "assembled"})
UNREVIEWED_STATUS = "ocr_done"
PENDING_STATUSES: frozenset[str] = frozenset({"uploaded", "preprocessed", "layout_done"})
ERROR_STATUS = "error"
EXCLUDED_STATUS = "excluded"

NUMBERING_MODES: tuple[str, ...] = ("chapter", "book", "page")
# Footnote numbering when the book has not chosen one: as printed, restarting on each page (owner
# decision 2026-09-25, amends D35's per-chapter default).
DEFAULT_NUMBERING = "page"
SEAM_MODES: tuple[str, ...] = ("join", "split")
DIGIT_STYLES: tuple[str, ...] = ("western", "arabic_indic")

# Geometry (§2.3), as shares of the text block's measure M.
WIDE_SHARE = 0.6  # lines at least this share of the widest line define the block's edges
INDENT_SHARE = 0.02  # right gap above this: the line is indented (Arabic first-line indent)
SHORT_SHARE = 0.06  # left gap above this: the paragraph ended before the left edge
CENTRED_SHARE = 0.06  # both gaps above this ...
CENTRED_BALANCE = 0.35  # ... and differing by less than this share of the larger one: centred

MAX_PREFIX_LETTERS = 2  # «بـ», «الـ»: a tatweel that joins a detached prefix is kept
MAX_SUGGESTION_LINES = 2
MAX_SUGGESTION_WORDS = 8

# Placeholders for inline nodes inside `Rich.text` (Unicode private use area, never in OCR text).
PB = "\ue000"  # pageBreak at a join
FN = "\ue001"  # footnote reference
PLACEHOLDERS = PB + FN

# Terminal punctuation at the end of a line or paragraph (§2.3, §2.4); `?` and `”` read like `؟` and `»`.
TERMINAL: frozenset[str] = frozenset(".؟!:»)]?”")
# Marks that attach to the previous word (D37 rule 1): no space before them, moved back when glued forward.
MARKS = ".،؛:؟!,;?"
SPACE_BEFORE_DROPPED = ".،؛:؟!,;?"  # rule 2: no space before these
OPEN_BRACKETS = "(["
CLOSE_BRACKETS = ")]"
OPEN_QUOTES = {"«": "guillemet", "“": "curly"}
CLOSE_QUOTES = {"»": "guillemet", "”": "curly"}
# After a closing-quote glyph, these (after optional spaces) mean it closes rather than opens a quote.
CLOSING_CONTEXT: frozenset[str] = frozenset(MARKS + CLOSE_BRACKETS + "»”" + PLACEHOLDERS)

TATWEEL = "\u0640"
_AR_LETTERS = (  # Arabic letters (no tatweel, no marks), with the presentation forms
    "\u0621-\u063a\u0641-\u064a\u066e\u066f\u0671-\u06d3\u06d5\u06ee\u06ef\u06fa-\u06fc\u06ff"
    "\ufb50-\ufdff\ufe70-\ufefc"
)
# Harakat, tanween, shadda, sukun, Quranic marks.
_AR_MARKS = "\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06dc\u06df-\u06e8\u06ea-\u06ed"
_DIGITS = "0-9\u0660-\u0669\u06f0-\u06f9"
_SUPERSCRIPTS = "¹²³⁴⁵⁶⁷⁸⁹⁰"
_SUPERSCRIPT_DIGITS = str.maketrans(_SUPERSCRIPTS, "1234567890")
_TO_ARABIC_INDIC = str.maketrans(
    "0123456789\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9",
    "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669" * 2,
)
ALEF = "\u0627"  # OCR reads the Arabic-Indic one «١» as a lone alef «ا» (real pages of book 13)

CODE_ORDER: tuple[str, ...] = (
    "no_headings",
    "page_pending",
    "page_error",
    "page_unreviewed",
    "empty_page",
    "running_head",
    "marker_unmatched",
    "note_orphan",
    "note_marker_missing",
    "note_call_repaired",
    "stray_note",
    "uncertain_words",
)
STAGES: tuple[str, ...] = ("collect", "paragraphs", "seams", "footnotes", "headings", "typography", "save")


# ====================================================================== inputs and settings


@dataclass
class LineIn:
    """One OCR'd line of a page, as the loader reads it.

    `kind` is `body` or `footnote` (running headers and page numbers never reach the pipeline; the
    loader gives the line's effective kind, `ocr.services.line_kind`: a `footnote` role makes a body
    line a note, a `main` role pulls a footnote-region line into the body); `role` the reviewer's line
    role as the pipeline reads it (`LINE_ROLES`: `body`, `heading`, `subheading`, `verse`; D32, D74);
    `box` `[x0, y0, x1, y1]` as ratios of the page (None when the line has no box, e.g. a line inserted
    by a reviewer); `uncertain` the indexes, in `text.split()`, of the words still unresolved
    (`conf == "low"` and no `res`).
    """

    id: int
    order: int
    kind: str
    role: str
    text: str
    box: tuple[float, float, float, float] | None = None
    uncertain: list[int] = field(default_factory=list)


@dataclass
class PageIn:
    """One page of the book with its lines in reading order; `printed` is the printed page number."""

    id: int
    number: int
    printed: str
    status: str
    reviewed: bool
    lines: list[LineIn] = field(default_factory=list)


@dataclass(frozen=True)
class Settings:
    """Assembly options (`Book.assembly_settings`, D38), validated by `normalize_settings`."""

    footnote_numbering: str = DEFAULT_NUMBERING
    include_unreviewed: bool = True
    strip_tatweel: bool = True
    strip_running_heads: bool = True
    seams: dict[str, str] = field(default_factory=dict)
    dismissed_suggestions: frozenset[str] = frozenset()

    def as_dict(self) -> dict:
        """JSON form, as stored in `AssemblyRun.settings`."""
        return {
            "footnote_numbering": self.footnote_numbering,
            "include_unreviewed": self.include_unreviewed,
            "strip_tatweel": self.strip_tatweel,
            "strip_running_heads": self.strip_running_heads,
            "seams": dict(sorted(self.seams.items(), key=lambda item: int(item[0]))),
            "dismissed_suggestions": sorted(self.dismissed_suggestions),
        }


@dataclass(frozen=True)
class BookMeta:
    """What the document needs to know about the book and the run."""

    id: int
    title: str = ""
    author: str = ""
    digit_style: str = "western"
    run_id: int | None = None
    assembled_at: str | None = None


def _as_bool(value, default: bool) -> bool:
    """A stored or posted boolean (`True`, `"true"`, `1`, …); anything else is `default`."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
    return default


def normalize_settings(raw: dict | Settings | None) -> Settings:
    """`Settings` from `Book.assembly_settings`; unknown or invalid values fall back to the defaults.

    Seam overrides keep only `{"<page number>": "join" | "split"}`; dismissed suggestions only
    paragraph ids (`p<line id>`).
    """
    if isinstance(raw, Settings):
        return raw
    data = raw if isinstance(raw, dict) else {}
    numbering = data.get("footnote_numbering")
    seams: dict[str, str] = {}
    raw_seams = data.get("seams")
    if isinstance(raw_seams, dict):
        for key, mode in raw_seams.items():
            if str(key).isascii() and str(key).isdigit() and mode in SEAM_MODES:
                seams[str(int(str(key)))] = mode
    dismissed = data.get("dismissed_suggestions")
    ids = frozenset(
        str(value)
        for value in (dismissed if isinstance(dismissed, list | tuple) else [])
        if _is_block_id(value)
    )
    return Settings(
        footnote_numbering=numbering if numbering in NUMBERING_MODES else DEFAULT_NUMBERING,
        include_unreviewed=_as_bool(data.get("include_unreviewed"), True),
        strip_tatweel=_as_bool(data.get("strip_tatweel"), True),
        strip_running_heads=_as_bool(data.get("strip_running_heads"), True),
        seams=seams,
        dismissed_suggestions=ids,
    )


def _is_block_id(value) -> bool:
    """True for a paragraph / heading id (`p812`, `h812`)."""
    return isinstance(value, str) and bool(re.fullmatch(r"[ph][0-9]+", value))


# ====================================================================== rich text


@dataclass(frozen=True, slots=True)
class Meta:
    """What a character of `Rich` carries: its source line, the uncertain flag and an inline node."""

    line: int | None = None
    uncertain: bool = False
    node: object | None = None


PLAIN = Meta()
_NO_PLACEHOLDERS = {ord(c): None for c in PLACEHOLDERS}


class Rich:
    """Text with one `Meta` per character; inline nodes are `PB` / `FN` characters with `Meta.node`."""

    __slots__ = ("text", "meta")

    def __init__(self, text: str = "", meta: list[Meta] | None = None):
        self.text = text
        self.meta = meta if meta is not None else [PLAIN] * len(text)
        if len(self.meta) != len(self.text):
            raise ValueError("Rich: one Meta per character")

    def __len__(self) -> int:
        return len(self.text)

    def __add__(self, other: Rich) -> Rich:
        return Rich(self.text + other.text, self.meta + other.meta)

    def __repr__(self) -> str:
        return f"Rich({self.text!r})"

    @classmethod
    def of(cls, text: str, meta: Meta = PLAIN) -> Rich:
        """`text` with the same `meta` on every character."""
        return cls(text, [meta] * len(text))

    @classmethod
    def node(cls, char: str, node: object, line: int | None) -> Rich:
        """A one-character placeholder for an inline node."""
        return cls(char, [Meta(line=line, node=node)])

    @classmethod
    def from_line(cls, line: LineIn) -> Rich:
        """The words of a line joined by single spaces; unresolved words carry the uncertain flag."""
        words = (line.text or "").translate(_NO_PLACEHOLDERS).split()
        uncertain = set(line.uncertain or [])
        plain = Meta(line=line.id)
        marked = Meta(line=line.id, uncertain=True)
        chars: list[str] = []
        meta: list[Meta] = []
        for i, word in enumerate(words):
            if i:
                chars.append(" ")
                meta.append(plain)
            chars.append(word)
            meta.extend([marked if i in uncertain else plain] * len(word))
        return cls("".join(chars), meta)

    @classmethod
    def concat(cls, parts: Iterable[Rich]) -> Rich:
        """`parts` one after the other (linear time, for long joined paragraphs)."""
        parts = list(parts)
        meta: list[Meta] = []
        for part in parts:
            meta.extend(part.meta)
        return cls("".join(part.text for part in parts), meta)

    @classmethod
    def join(cls, parts: Sequence[Rich], sep: str = " ") -> Rich:
        """`parts` joined by `sep` (the separator takes the source line of the part before it)."""
        pieces: list[Rich] = []
        line: int | None = None
        for i, part in enumerate(parts):
            if i and sep:
                pieces.append(Rich.of(sep, Meta(line=line)))
            pieces.append(part)
            line = next((m.line for m in reversed(part.meta) if m.line is not None), line)
        return cls.concat(pieces)

    def cut(self, start: int, end: int | None = None) -> Rich:
        """The characters `start:end`."""
        return Rich(self.text[start:end], self.meta[start:end])

    def plain(self) -> str:
        """The text without inline-node placeholders."""
        return self.text.translate(_NO_PLACEHOLDERS)

    def nodes(self, char: str | None = None) -> list:
        """Inline nodes in order (only those of placeholder `char` when given)."""
        return [
            m.node
            for c, m in zip(self.text, self.meta, strict=True)
            if c in PLACEHOLDERS and (char is None or c == char)
        ]

    def lines(self) -> list[int]:
        """Source line ids of the characters, in order of first appearance."""
        seen: dict[int, None] = {}
        for m in self.meta:
            if m.line is not None:
                seen.setdefault(m.line, None)
        return list(seen)


Piece = tuple[int, int] | str


def _rewrite(rich: Rich, pattern: re.Pattern, build: Callable[[re.Match], list[Piece]]) -> Rich:
    """Replace every match of `pattern` by the pieces `build(match)` returns.

    A piece is a `(start, end)` span of the source (copied with its metas) or a literal string
    (inserted with a plain meta on the source line of the match start).
    """
    text, meta = rich.text, rich.meta
    out_text: list[str] = []
    out_meta: list[Meta] = []
    pos = 0
    for match in pattern.finditer(text):
        out_text.append(text[pos : match.start()])
        out_meta.extend(meta[pos : match.start()])
        for piece in build(match):
            if isinstance(piece, str):
                src = meta[match.start()] if match.start() < len(meta) else PLAIN
                out_text.append(piece)
                out_meta.extend([Meta(line=src.line)] * len(piece))
            else:
                start, end = piece
                out_text.append(text[start:end])
                out_meta.extend(meta[start:end])
        pos = match.end()
    if pos == 0:
        return rich
    out_text.append(text[pos:])
    out_meta.extend(meta[pos:])
    return Rich("".join(out_text), out_meta)


def _drop(rich: Rich, ranges: Iterable[tuple[int, int]]) -> Rich:
    """`rich` without the characters of the `(start, end)` ranges (they may overlap)."""
    spans = sorted((start, end) for start, end in ranges if end > start)
    if not spans:
        return rich
    texts: list[str] = []
    meta: list[Meta] = []
    pos = 0
    for start, end in spans:
        if start > pos:
            texts.append(rich.text[pos:start])
            meta.extend(rich.meta[pos:start])
        pos = max(pos, end)
    texts.append(rich.text[pos:])
    meta.extend(rich.meta[pos:])
    return Rich("".join(texts), meta)


# ====================================================================== typography (§2.2, D37)

_RE_WS_RUN = re.compile(r"\s{2,}|[^\S ]")
_RE_TATWEEL = re.compile(TATWEEL + "+")
# Rule 1: a mark glued to the start of an Arabic word moves to the end of the previous word.
_RE_GLUED_FORWARD = re.compile(rf"(\S)(\s+)([{re.escape(MARKS)}]+)(?=[{_AR_LETTERS}])")
# A mark glued on both sides («شتاء،كما», «النصوص.وقد», «التجاني).وقد») gets its space after it.
_RE_GLUED_BOTH = re.compile(rf"(?<=[{_AR_LETTERS}{_AR_MARKS}])([،؛؟!,;?:])(?=[{_AR_LETTERS}])")
_AR_WORDISH = f"[{_AR_LETTERS}{_AR_MARKS}]"
_RE_GLUED_PERIOD = re.compile(rf"(?<={_AR_WORDISH}{{3}})(\.)(?=[{_AR_LETTERS}]{_AR_WORDISH}{{2}})")
_RE_GLUED_AFTER_CLOSER = re.compile(rf"(?<=[)\]»”])([{re.escape(MARKS)}])(?=[{_AR_LETTERS}])")
_RE_SPACE_BEFORE = re.compile(rf"\s+(?=[{re.escape(SPACE_BEFORE_DROPPED + CLOSE_BRACKETS)}])")
_RE_SPACE_AFTER_OPEN = re.compile(rf"(?<=[{re.escape(OPEN_BRACKETS)}])\s+")
# Marks and closers right after a page break go before it («إلى⟨pb⟩.» → «إلى.⟨pb⟩»).
_RE_MARKS_AFTER_PB = re.compile(rf"{PB}([{re.escape(MARKS + CLOSE_BRACKETS)}»”]+)")
_RE_QUOTE = re.compile("[«»“”]")
_RE_LEADING_MARKS = re.compile(rf"^([{re.escape(MARKS)}]+)\s*(?=[{_AR_LETTERS}])")


def _is_letterish(char: str) -> bool:
    """A letter or an Arabic combining mark (the inside of a word)."""
    return bool(char) and (char.isalpha() or bool(re.match(f"[{_AR_MARKS}]", char)))


def collapse_spaces(rich: Rich) -> Rich:
    """Runs of whitespace become one plain space; leading and trailing whitespace go (rule 4)."""
    rich = _rewrite(rich, _RE_WS_RUN, lambda m: [" "])
    start, end = 0, len(rich.text)
    while start < end and rich.text[start].isspace():
        start += 1
    while end > start and rich.text[end - 1].isspace():
        end -= 1
    return rich.cut(start, end) if (start, end) != (0, len(rich.text)) else rich


def _letters_before(text: str, index: int) -> int:
    """Letters of the word that ends at `index` (combining marks not counted)."""
    count = 0
    i = index - 1
    while i >= 0 and _is_letterish(text[i]):
        count += 1 if text[i].isalpha() else 0
        i -= 1
    return count


def strip_tatweel(rich: Rich) -> Rich:
    """Remove tatweel (U+0640) inside or at the edge of a word (rule 3, `بـــين` → `بين`).

    A tatweel standing alone between spaces, digits or punctuation is a dash in the print
    («1 ـ كتاب», «وصلحائها ـ رحلة») and stays; so does the tatweel after a prefix of one or two
    letters written apart from its word («بـ »الهروج الأسود»», «الـ 13 مصدرًا»).
    """
    text = rich.text
    drop = []
    for match in _RE_TATWEEL.finditer(text):
        before = text[match.start() - 1] if match.start() else ""
        after = text[match.end()] if match.end() < len(text) else ""
        if not (_is_letterish(before) or _is_letterish(after)):
            continue
        if not _is_letterish(after) and _letters_before(text, match.start()) <= MAX_PREFIX_LETTERS:
            continue  # a prefix written apart from its word («بـ »الهروج»», «الـ 13») keeps its tatweel
        drop.append(match.span())
    return _drop(rich, drop)


def repair_glued_marks(rich: Rich) -> Rich:
    """Rule 1: `"وتاكنست .وتاكنست"` → `"وتاكنست. وتاكنست"`; marks glued on both sides get a space.

    Marks: `. ، ؛ : ؟ ! , ; ?`. Opening brackets and quotes at a word start are not marks and stay.
    A period between two Arabic letters is only split when both sides have three letters or more, so
    abbreviations (`ق.م`) and stray dots inside a word (`التد.لي`) stay.
    """
    rich = _rewrite(rich, _RE_GLUED_FORWARD, lambda m: [m.span(1), m.span(3), " "])
    rich = _rewrite(rich, _RE_GLUED_BOTH, lambda m: [m.span(1), " "])
    rich = _rewrite(rich, _RE_GLUED_PERIOD, lambda m: [m.span(1), " "])
    return _rewrite(rich, _RE_GLUED_AFTER_CLOSER, lambda m: [m.span(1), " "])


# A note reference after a closing-quote glyph («برقة » ١ ،»): the glyph closes.
_RE_MARKER_AFTER_QUOTE = re.compile(rf"[{_DIGITS}]{{1,3}}(?=\s*(?:$|[{re.escape(MARKS)}{PLACEHOLDERS}]))")


def quote_roles(text: str) -> dict[int, str]:
    """`{index: "open" | "close"}` for the quote characters of `text`.

    `«` and `“` open; `»` and `”` close an open quote. Books and OCR also print `»` / `”` for the
    opening quote («قاع » حمادة مرزق »»); a closing glyph with no quote open is read from its
    context: followed by punctuation, a note reference (a placeholder, or a number before a mark
    or the end) or the end → closing; glued to the word before it → closing; otherwise it opens a
    quote.
    """
    depth = {"guillemet": 0, "curly": 0}
    roles: dict[int, str] = {}
    size = len(text)
    for match in _RE_QUOTE.finditer(text):
        i, char = match.start(), match.group()
        if char in OPEN_QUOTES:
            roles[i] = "open"
            depth[OPEN_QUOTES[char]] += 1
        elif char in CLOSE_QUOTES:
            family = CLOSE_QUOTES[char]
            if depth[family] > 0:
                roles[i] = "close"
                depth[family] -= 1
                continue
            j = i + 1
            while j < size and text[j].isspace():
                j += 1
            space_before = i == 0 or text[i - 1].isspace()
            space_after = i + 1 >= size or text[i + 1].isspace()
            if (
                j >= size
                or text[j] in CLOSING_CONTEXT
                or (not space_before and space_after)
                or _RE_MARKER_AFTER_QUOTE.match(text, j)
            ):
                roles[i] = "close"
            else:
                roles[i] = "open"
                depth[family] += 1
    return roles


def tighten_spaces(rich: Rich) -> Rich:
    """Rule 2: no space before `. ، ؛ : ؟ !` (and `, ; ?`) or a closing `» ) ]`, none after `« ( [`."""
    text = rich.text
    drop = [
        match.span()
        for pattern in (_RE_SPACE_BEFORE, _RE_SPACE_AFTER_OPEN)
        for match in pattern.finditer(text)
    ]
    for index, role in quote_roles(text).items():
        if role == "close":
            j = index
            while j > 0 and text[j - 1].isspace():
                j -= 1
            drop.append((j, index))
        else:
            j = index + 1
            while j < len(text) and text[j].isspace():
                j += 1
            drop.append((index + 1, j))
    return _drop(rich, drop)


def convert_digits(rich: Rich, style: str = "western") -> Rich:
    """Rule 5: digits per the book's `digit_style` (Western 0-9 by default, D6); same length, metas kept."""
    text = to_western_digits(rich.text)
    if style == "arabic_indic":
        text = text.translate(_TO_ARABIC_INDIC)
    return rich if text == rich.text else Rich(text, rich.meta)


def digits_in(text: str, style: str = "western") -> str:
    """`text` with its digits in `style` (plain-string form of `convert_digits`)."""
    return convert_digits(Rich(text), style).text


_RE_BIDI_CONTROLS = re.compile("[\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def strip_bidi_controls(rich: Rich) -> Rich:
    """Remove the invisible direction marks OCR leaves in words («WV\u200f»); the document sets its own."""
    return _drop(rich, (match.span() for match in _RE_BIDI_CONTROLS.finditer(rich.text)))


def normalize_rich(rich: Rich, strip_tatweel_marks: bool = True, digit_style: str = "western") -> Rich:
    """All of D37 on one text: glued marks, spaces around marks and quotes, tatweel, spaces, digits.

    Invisible direction marks go first; tatweel next so a stretched word is one word for the
    punctuation rules; digits go last.
    """
    rich = collapse_spaces(strip_bidi_controls(rich))
    if strip_tatweel_marks:
        rich = strip_tatweel(rich)
    rich = repair_glued_marks(rich)
    rich = tighten_spaces(rich)
    rich = collapse_spaces(rich)
    rich = _rewrite(rich, _RE_MARKS_AFTER_PB, lambda m: [m.span(1), (m.start(), m.start() + 1)])
    return convert_digits(rich, digit_style)


def normalize_text(text: str, strip_tatweel_marks: bool = True, digit_style: str = "western") -> str:
    """`normalize_rich` on a plain string (tests, previews)."""
    return normalize_rich(Rich(text), strip_tatweel_marks, digit_style).text


def ends_terminal(text: str) -> bool:
    """True when `text` ends with terminal punctuation (`. ؟ ! : » ) ]`, also `?` and `”`)."""
    stripped = text.translate(_NO_PLACEHOLDERS).rstrip()
    return bool(stripped) and stripped[-1] in TERMINAL


_RE_CLOSERS_AT_END = re.compile(r"[\s»”)\]]+$")


def ends_sentence(text: str) -> bool:
    """True when `text` ends a sentence (`. ؟ ! :` or `?`, before any closing quote or bracket): a
    title such as «مختارات من «مروج الذهب»» ends with a quote but not a sentence."""
    stripped = _RE_CLOSERS_AT_END.sub("", text.translate(_NO_PLACEHOLDERS))
    return bool(stripped) and stripped[-1] in ".؟!:?"


_RE_ENDS_ARABIC_WORD = re.compile(f"[{_AR_LETTERS}][{_AR_MARKS}]*$")


def ends_mid_sentence(text: str) -> bool:
    """True when `text` ends on a bare Arabic word (no punctuation, number or Latin word after it)."""
    return bool(_RE_ENDS_ARABIC_WORD.search(text.translate(_NO_PLACEHOLDERS).rstrip()))


def word_count(text: str) -> int:
    """Words of `text` (whitespace separated, inline placeholders left out)."""
    return len(text.translate(_NO_PLACEHOLDERS).split())


# ====================================================================== warnings


@dataclass
class AssemblyWarning:
    """One assembly warning (§2.9); `page` is the page number (None for book-level warnings).

    `marker` (the note number a footnote warning is about, Western digits or `*`) and `actions` (what
    the manuscript offers on the warning, `stray_note`: «جعلها حاشية» · «انتقال») are written only when
    set, so the older warnings keep their shape.
    """

    code: str
    severity: str
    page: int | None
    message: str
    block_id: str | None = None
    line_ids: list[int] = field(default_factory=list)
    marker: str | None = None
    actions: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        out = {
            "code": self.code,
            "severity": self.severity,
            "page": self.page,
            "blockId": self.block_id,
            "lineIds": list(self.line_ids),
            "message": self.message,
        }
        if self.marker is not None:
            out["marker"] = self.marker
        if self.actions:
            out["actions"] = [dict(item) for item in self.actions]
        return out


def sort_warnings(warnings: Iterable[AssemblyWarning]) -> list[AssemblyWarning]:
    """Book-level warnings first, then by page and by code (stable)."""
    order = {code: i for i, code in enumerate(CODE_ORDER)}
    return sorted(
        warnings,
        key=lambda w: (-1 if w.page is None else w.page, order.get(w.code, len(order))),
    )


# ====================================================================== 2.1 page selection


@dataclass
class Selection:
    """Result of `select_pages`.

    `gaps` maps an included page number to the skipped page numbers just before it (the join chain
    breaks there: its seam is `missing`).
    """

    included: list[PageIn]
    skipped: list[PageIn]
    gaps: dict[int, list[int]]
    warnings: list[AssemblyWarning]


def select_pages(pages: Iterable[PageIn], settings: Settings) -> Selection:
    """Which pages go into the manuscript (D35).

    Included: `reviewed`, `assembled`, and `ocr_done` when `include_unreviewed` (flagged
    `page_unreviewed`). Skipped with a warning (and breaking the join chain): pages still in the
    pipeline (`page_pending`), in error (`page_error`), and `ocr_done` pages when unreviewed pages
    are left out. Excluded pages never appear.
    """
    included: list[PageIn] = []
    skipped: list[PageIn] = []
    gaps: dict[int, list[int]] = {}
    warnings: list[AssemblyWarning] = []
    pending_gap: list[int] = []
    for page in sorted(pages, key=lambda p: p.number):
        status = page.status
        if status == EXCLUDED_STATUS:
            continue
        n = page.number
        if status in REVIEWED_STATUSES or (status == UNREVIEWED_STATUS and settings.include_unreviewed):
            if status == UNREVIEWED_STATUS:
                warnings.append(
                    AssemblyWarning(
                        "page_unreviewed",
                        "warning",
                        n,
                        f"الصفحة {n} لم تُراجَع بعد؛ نصها كما قرأه التعرّف الآلي.",
                    )
                )
            if pending_gap and included:
                gaps[n] = pending_gap
            pending_gap = []
            included.append(page)
            continue
        if status == UNREVIEWED_STATUS:
            warnings.append(
                AssemblyWarning("page_unreviewed", "warning", n, f"الصفحة {n} لم تُراجَع بعد فلم تُضمَّن.")
            )
        elif status == ERROR_STATUS:
            warnings.append(AssemblyWarning("page_error", "warning", n, f"تعذّرت معالجة الصفحة {n} فلم تُضمَّن."))
        else:
            warnings.append(
                AssemblyWarning("page_pending", "warning", n, f"الصفحة {n} ما زالت قيد المعالجة فلم تُضمَّن.")
            )
        skipped.append(page)
        pending_gap = [*pending_gap, n]
    return Selection(included, skipped, gaps, warnings)


# ====================================================================== running heads (D49)

RUNNING_HEAD_PAGES = 3  # the same first line on at least this many pages is a running head
RUNNING_HEAD_WORDS = 6
RUNNING_HEAD_WIDTH = 0.6  # at most this share of the text measure
RUNNING_HEAD_TALLER = 1.25  # a member this much taller than its group's median is a title (kept) ...
RUNNING_HEAD_LOWER = 0.02  # ... and so is one this much lower on its page (a chapter opening)
_RE_NOT_LETTER = re.compile(r"[\s0-9]+")


def head_key(text: str) -> str:
    """A first line as running heads are compared: letters folded, no diacritics, digits, punctuation
    or spaces (the page number printed beside a head is not part of it)."""
    return _RE_NOT_LETTER.sub("", normalize_arabic(text, "lenient"))


def _within(a: str, b: str, limit: int) -> bool:
    """Whether `a` and `b` are at most `limit` edits apart (Levenshtein, stopping early)."""
    if abs(len(a) - len(b)) > limit:
        return False
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        if min(current) > limit:
            return False
        previous = current
    return previous[-1] <= limit


def heads_match(a: str, b: str) -> bool:
    """Two head keys read as the same head: equal, or one OCR slip apart (two for a long head);
    «المسعودي» and «السعودي» are the same head."""
    if a == b:
        return True
    if min(len(a), len(b)) < 4:
        return False
    return _within(a, b, 1 if max(len(a), len(b)) < 12 else 2)


def _head_candidate(page: PageIn) -> LineIn | None:
    """The page's first body line when it could be a running head: a body line (a reviewer's heading
    never is) with a box, short and narrow, followed by more text."""
    body = [line for line in page.lines if line.kind == BODY]
    if len(body) < 2:
        return None
    first = body[0]
    if first.role != ROLE_BODY or first.box is None or word_count(first.text) > RUNNING_HEAD_WORDS:
        return None
    if not head_key(first.text):
        return None
    measure = text_measure(body)
    if measure is not None and first.box[2] - first.box[0] > RUNNING_HEAD_WIDTH * measure.width:
        return None
    return first


def _pages_phrase(n: int) -> str:
    if n == 1:
        return "صفحة واحدة"
    if n == 2:
        return "صفحتين"
    return f"{n} صفحات" if 3 <= n % 100 <= 10 else f"{n} صفحة"


def drop_running_heads(pages: Sequence[PageIn]) -> tuple[list[PageIn], list[AssemblyWarning], int]:
    """The pages without the running heads that the layout left in their body (D49), one info warning
    per head, and the number of lines dropped.

    A head is a page's first body line (short, narrow, with a box) whose text is the first line of at
    least `RUNNING_HEAD_PAGES` pages, OCR slips allowed; books alternate two (the book's title and the
    chapter's), each is found on its own. A member clearly taller or lower than its group is kept: that
    is the chapter's own title on its first page. That title, or a reviewed heading with the same text,
    counts as one of the pages (a chapter's head repeats its title), but at least two pages must carry
    the head. Stored lines are never changed.
    """
    found = [(page, line, head_key(line.text)) for page in pages if (line := _head_candidate(page))]
    lines = [line for page in pages for line in page.lines if line.kind == BODY]
    titles = [head_key(line.text) for line in lines if line.role in HEADING_LEVELS]
    parent = list(range(len(found)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(found)):
        for j in range(i + 1, len(found)):
            if root(i) != root(j) and heads_match(found[i][2], found[j][2]):
                parent[root(j)] = root(i)
    groups: dict[int, list[int]] = {}
    for i in range(len(found)):
        groups.setdefault(root(i), []).append(i)
    dropped: set[int] = set()
    warnings: list[AssemblyWarning] = []
    for members in groups.values():
        titled = any(heads_match(found[members[0]][2], title) for title in titles if title)
        if len({found[i][0].number for i in members}) + titled < RUNNING_HEAD_PAGES:
            continue
        height = statistics.median(found[i][1].box[3] - found[i][1].box[1] for i in members)
        top = statistics.median(found[i][1].box[1] for i in members)
        heads = [
            i
            for i in members
            if found[i][1].box[3] - found[i][1].box[1] <= RUNNING_HEAD_TALLER * height
            and found[i][1].box[1] <= top + RUNNING_HEAD_LOWER
        ]
        numbers = sorted({found[i][0].number for i in heads})
        titled = titled or len(heads) < len(members)  # the chapter's own title repeats as its head
        if len(numbers) < 2 or len(numbers) + titled < RUNNING_HEAD_PAGES:
            continue
        dropped.update(found[i][1].id for i in heads)
        texts = [found[i][1].text.strip() for i in heads]
        text = max(set(texts), key=lambda t: (texts.count(t), -texts.index(t)))
        warnings.append(
            AssemblyWarning(
                "running_head",
                "info",
                numbers[0],
                f"حُذفت الترويسة «{text}» من أعلى {_pages_phrase(len(numbers))}.",
                line_ids=sorted(found[i][1].id for i in heads),
            )
        )
    if not dropped:
        return list(pages), [], 0

    def without(page: PageIn) -> PageIn:
        return dataclasses.replace(page, lines=[line for line in page.lines if line.id not in dropped])

    return [without(page) for page in pages], warnings, len(dropped)


# ====================================================================== 2.3 paragraphs inside a page


@dataclass(frozen=True)
class Measure:
    """The text block of a page: its left and right edges and its width (ratios of the page)."""

    left: float
    right: float
    width: float


@dataclass(frozen=True)
class Shape:
    """How a line sits in the text block."""

    indented: bool
    short: bool
    centred: bool


def text_measure(lines: Iterable[LineIn]) -> Measure | None:
    """Edges of the text block: medians of `x0` / `x1` over the lines ≥ 60 % as wide as the widest.

    None when no line has a usable box.
    """
    boxes = [line.box for line in lines if line.box is not None and line.box[2] > line.box[0]]
    if not boxes:
        return None
    widest = max(box[2] - box[0] for box in boxes)
    wide = [box for box in boxes if box[2] - box[0] >= WIDE_SHARE * widest]
    right = statistics.median(box[2] for box in wide)
    left = statistics.median(box[0] for box in wide)
    width = right - left
    return Measure(left, right, width) if width > 0 else None


def line_shape(box: Sequence[float] | None, measure: Measure | None) -> Shape | None:
    """Indented / short / centred for one line box (None without a box or a measure).

    RTL: a line starts at the right, so the indent is the right gap `R − x1` and a short last line
    leaves the left gap `x0 − L`.
    """
    if box is None or measure is None:
        return None
    gap_start = measure.right - box[2]
    gap_end = box[0] - measure.left
    m = measure.width
    indented = gap_start > INDENT_SHARE * m
    short = gap_end > SHORT_SHARE * m
    larger = max(gap_start, gap_end)
    centred = (
        gap_start > CENTRED_SHARE * m
        and gap_end > CENTRED_SHARE * m
        and abs(gap_start - gap_end) < CENTRED_BALANCE * larger
    )
    return Shape(indented, short, centred)


def breaks_between(
    a: LineIn, b: LineIn, shape_a: Shape | None, shape_b: Shape | None, measure: Measure | None = None
) -> bool:
    """True when a paragraph break falls between consecutive body lines `a` and `b` (§2.3).

    A verse line on either side → break (D74: each verse line is a paragraph of its own; pairing
    hemistichs into bayts is 7d). Roles differ → break; lines of one heading role never break (they
    form one heading). With both boxes: `a` short, `b` indented, or either centred. Without a box on
    either line: `a` ends with terminal punctuation.

    With the page's `measure`, `b` only counts as indented when it also starts left of `a`
    (`x1_a − x1_b > 0.02 · M`): consecutive lines with the same indent are one indented block (an
    inset list item, a quotation set narrower, a page whose right edge drifts), not a paragraph each.
    """
    if a.role == ROLE_VERSE or b.role == ROLE_VERSE or a.role != b.role:
        return True
    if a.role in HEADING_LEVELS:
        return False
    if shape_a is None or shape_b is None:
        return ends_terminal(a.text)
    indented = shape_b.indented
    if measure is not None and a.box is not None and b.box is not None:
        indented = indented and a.box[2] - b.box[2] > INDENT_SHARE * measure.width
    if indented and ends_mid_sentence(a.text):
        # A full line that stops on a bare word runs on: the "indent" is a start the boxes missed
        # (a leading dash «– السعيد –», a first word Tesseract could not read), not a new paragraph.
        indented = False
    return shape_a.short or indented or shape_a.centred or shape_b.centred


@dataclass
class Block:
    """A paragraph or a heading being assembled."""

    id: str
    kind: str  # "paragraph" | "heading"
    level: int  # 1 | 2 for headings, 0 for paragraphs
    lines: list[LineIn]
    pages: list[int]
    shapes: list[Shape | None]
    rich: Rich
    reviewed: bool = True
    suggested: str | None = None
    parts: list[Rich] | None = None  # pending joins, concatenated once by `join_pages`
    style: str | None = None  # `verse` for a verse line's paragraph (D74)
    note_for: str | None = None  # the open call a marker-initial paragraph may become the note of (D74)

    @property
    def line_ids(self) -> list[int]:
        return [line.id for line in self.lines]


def block_id(kind: str, first_line_id: int) -> str:
    """Stable block id: `h` / `p` + the first source line id (§2.8)."""
    return f"{'h' if kind == 'heading' else 'p'}{first_line_id}"


def note_id(first_line_id: int) -> str:
    """Stable note id: `n` + the first note line id."""
    return f"n{first_line_id}"


def _new_block(lines: list[LineIn], shapes: list[Shape | None], page: PageIn) -> Block:
    level = HEADING_LEVELS.get(lines[0].role, 0)
    kind = "heading" if level else "paragraph"
    return Block(
        id=block_id(kind, lines[0].id),
        kind=kind,
        level=level,
        lines=list(lines),
        pages=[page.number],
        shapes=list(shapes),
        rich=Rich.join([Rich.from_line(line) for line in lines]),
        reviewed=page.reviewed,
        style=VERSE_STYLE if lines[0].role == ROLE_VERSE else None,
    )


def split_paragraphs(page: PageIn) -> list[Block]:
    """The body lines of one page as paragraphs and headings, in reading order (§2.3).

    Consecutive `heading` lines form one level-1 heading, consecutive `subheading` lines one level-2
    heading, and every `verse` line a paragraph of its own with the style `verse` (D74). Lines without
    text are left out.
    """
    lines = [line for line in page.lines if line.kind != FOOTNOTE and (line.text or "").strip()]
    if not lines:
        return []
    measure = text_measure(lines)
    shapes = [line_shape(line.box, measure) for line in lines]
    blocks: list[Block] = []
    start = 0
    for i in range(1, len(lines) + 1):
        if i == len(lines) or breaks_between(lines[i - 1], lines[i], shapes[i - 1], shapes[i], measure):
            blocks.append(_new_block(lines[start:i], shapes[start:i], page))
            start = i
    return blocks


# ====================================================================== 2.4 seams


@dataclass(frozen=True)
class PageBreak:
    """Inline page break at a join (`{"type": "pageBreak", "attrs": {"page", "printed"}}`)."""

    page: int
    printed: str


def decide_seam(
    prev: Block | None, nxt: Block | None, override: str | None, page: int, from_page: int
) -> dict:
    """The seam record between page `from_page` and page `page` (§2.4).

    Join when both blocks are paragraphs, the last line of the first is not short and the first line
    of the second is not indented; without boxes, join when the first does not end with terminal
    punctuation. An override (`join` / `split`) wins when both sides are paragraphs. `reason` is the
    automatic rule that applied: `geometry`, `punctuation`, `heading` (a side is not a paragraph: a
    heading, or a page without body text) or `verse` (a side is a verse line, never joined, D74: no
    override joins it).
    """
    record = {"page": page, "from_page": from_page, "mode": "split", "decision": "auto", "reason": "heading"}
    if prev is None or nxt is None or prev.kind != "paragraph" or nxt.kind != "paragraph":
        return record
    if prev.style == VERSE_STYLE or nxt.style == VERSE_STYLE:
        record["reason"] = "verse"
        return record
    last, first = prev.shapes[-1], nxt.shapes[0]
    if last is None or first is None:
        record["reason"] = "punctuation"
        join = not ends_terminal(prev.lines[-1].text)
    else:
        record["reason"] = "geometry"
        join = not last.short and not first.indented
    record["mode"] = "join" if join else "split"
    if override in SEAM_MODES:
        record["decision"] = "override"
        record["mode"] = override
    return record


def merge_blocks(first: Block, second: Block, page: PageIn) -> Block:
    """`second` (the first block of `page`) appended to `first` with a page break at the join point."""
    first_line = second.lines[0].id if second.lines else None
    if first.parts is None:
        first.parts = [first.rich]
    first.parts += [
        Rich.node(PB, PageBreak(page.number, page.printed or ""), first_line),
        Rich.of(" ", Meta(line=first_line)),
        *(second.parts if second.parts is not None else [second.rich]),
    ]
    first.rich = Rich()  # materialised by `finish_joins`
    first.lines.extend(second.lines)
    first.shapes.extend(second.shapes)
    for number in second.pages:
        if number not in first.pages:
            first.pages.append(number)
    first.reviewed = first.reviewed and second.reviewed
    return first


def join_pages(
    page_blocks: Sequence[tuple[PageIn, list[Block]]], overrides: dict[str, str], gaps: dict[int, list[int]]
) -> tuple[list[Block], list[dict]]:
    """Join paragraphs across page boundaries; returns the blocks of the book and one seam per boundary.

    A boundary right after skipped pages is `missing` (reason `skipped_page`, never joined, with the
    skipped page numbers in `skipped`).
    """
    out: list[Block] = []
    seams: list[dict] = []
    prev_page: PageIn | None = None
    prev_last: Block | None = None
    for page, blocks in page_blocks:
        blocks = list(blocks)
        had_blocks = bool(blocks)
        if prev_page is not None:
            if page.number in gaps:
                seams.append(
                    {
                        "page": page.number,
                        "from_page": prev_page.number,
                        "mode": "missing",
                        "decision": "auto",
                        "reason": "skipped_page",
                        "skipped": list(gaps[page.number]),
                    }
                )
            else:
                nxt = blocks[0] if blocks else None
                record = decide_seam(
                    prev_last, nxt, overrides.get(str(page.number)), page.number, prev_page.number
                )
                seams.append(record)
                if record["mode"] == "join" and prev_last is not None and nxt is not None:
                    merge_blocks(prev_last, nxt, page)
                    blocks = blocks[1:]
        out.extend(blocks)
        if had_blocks:
            prev_last = out[-1]
        else:
            prev_last = None
        prev_page = page
    finish_joins(out)
    return out, seams


def finish_joins(blocks: Iterable[Block]) -> None:
    """Concatenate the pending parts of joined blocks (once, in linear time)."""
    for block in blocks:
        if block.parts is not None:
            block.rich = Rich.concat(block.parts)
            block.parts = None


# ====================================================================== 2.5 footnotes

_DIGIT_RUN = f"[{_DIGITS}]"
NOTE_MARKER = re.compile(
    rf"^\s*[\(\[]?\s*({_DIGIT_RUN}{{1,3}}|\*{{1,3}}|{ALEF}(?=[\s\)\]]))\s*[\)\]]?\s*[-–—ـ.:،]?(?:\s+|$)"
)
_RE_BRACKETED = re.compile(rf"[\(\[]\s*({_DIGIT_RUN}{{1,3}}|\*{{1,3}})\s*[\)\]]")
_RE_SUPERSCRIPT = re.compile(f"[{_SUPERSCRIPTS}]+")
_RE_GLUED = re.compile(
    rf"(?:(?<=[^\W\d_])|(?<=[{_AR_MARKS}»”])|(?<=[^\W\d_]\.)|(?<=\s\.))({_DIGIT_RUN}{{1,3}})(?![{_DIGITS}\w])"
)
_AFTER_MARKER = rf"(?=\s|$|[{re.escape(MARKS + CLOSE_BRACKETS)}»”{PLACEHOLDERS}])"
_RE_STANDALONE = re.compile(rf"(?<=\s)({_DIGIT_RUN}{{1,2}}){_AFTER_MARKER}")
_RE_ALEF = re.compile(rf"(?:(?<=\s)|(?<=[»”])){ALEF}(?=\s|$|[.،؛:{PLACEHOLDERS}])")
# A bracketed glyph the models write for the small raised «(١)» of a call (D82): an alef, a quote
# stroke, or nothing at all («(ا)», «(أ)», «(”)», «( )»; book 29). Glued to its word too («أرطاة(ا)»).
LOOKALIKE_GLYPHS = "اأإآ”“\"'’‘"
_RE_LOOKALIKE = re.compile(rf"[\(\[]\s*([{LOOKALIKE_GLYPHS}]?)\s*[\)\]]")
# The same reading with one bracket lost, a word of its own («» (” .», «(ا»; book 31 p. 23, D85).
_RE_HALF_LOOKALIKE = re.compile(
    rf"(?:(?<=\s)|^)(?:[\(\[]\s*([{LOOKALIKE_GLYPHS}])|([”“\"'’‘])\s*[\)\]])(?=\s|$|[{re.escape(MARKS)}])"
)
# What the models write for a raised call with quote strokes (D87): a pair of strokes as a word of its own
# («صَدَقَةٌ ” “ .», «""», book 31 p. 37), or strokes around one or two digits or an alef, glued to the word or
# alone («المخالفة"٢"،», «به"٢٢».», «الأصول»"ا".», books 34 and 35). A leading «»» stays the text's closer.
_RE_QUOTE_PAIR = re.compile(rf"(?:(?<=\s)|^)([”“\"'’‘])\s?[”“\"'’‘](?=\s|$|[{re.escape(MARKS)}])")
_RE_QUOTED_READING = re.compile(
    rf"(?:(?<=\s)|(?<=[^\W\d_])|(?<=[{_AR_MARKS}»]))[”“\"'’‘]({_DIGIT_RUN}{{1,2}}|[اأإآ])[”“\"'’‘»]"
    rf"(?=\s|$|[{re.escape(MARKS)}])"
)
LOOKALIKE = "lookalike"
# A note marker the models read as a bracketed lookalike («(أ) ١ ـ (الوحي)» for (١), book 31 p. 28): its
# number is the next of its page's sequence (`page_notes`, D85), unless the page's note lines start with
# lettered items («(ب)» …), where «(أ)» is a letter.
_NOTE_LOOKALIKE = re.compile(r"^\s*[\(\[]\s*([أإآ”“\"'’‘])\s*[\)\]]\s*[-–—ـ.:،]?(?:\s+|$)")
_RE_LETTERED = re.compile(r"^\s*[\(\[]\s*[بتثجحخدذرزسشصضطظعغفقكلمنهوي]\s*[\)\]]")
NOTE_LOOKALIKES = "أإآ”“\"'’‘"
REPAIR_MAX = (
    9  # a call read with a «١» hung on a one-digit note number (D82): «(١١)» for (١) … «(٩١)» for (٩)
)
# A standalone number right after a closing quote or bracket («دينار » ١ ،») is printed as a marker.
STRONG_STYLES: frozenset[str] = frozenset({"bracket", "superscript", "glued", "quoted"})
# A note without a marker takes a call by its place only when the call is bracketed or superscript and
# its number is small (`positional_call`, D74).
POSITIONAL_STYLES: frozenset[str] = frozenset({"bracket", "superscript"})
POSITIONAL_MAX = 15  # as `publishing.readiness.NOTE_MARKER_MAX`
_QUOTED_AFTER = frozenset("»”)]")
# Standalone numbers that are text, not markers: a list number («3 ـ كتاب»), a year («سنة 21 ه»).
_RE_LIST_DASH = re.compile(r"\s*[ـ–—-](?:\s|$)")
YEAR_WORDS: frozenset[str] = frozenset({"سنة", "سنه", "عام", "سنتي", "عامي", "سنوات"})
ERA_WORDS: frozenset[str] = frozenset({"ه", "هـ", "م", "ق", "ق.", "ق.م", "للهجرة", "هجرية", "ميلادية"})
_RE_STRIP_MARKS = re.compile(f"[{_AR_MARKS}]")


def marker_key(marker: str) -> str:
    """Comparable form of a marker: its number in Western digits (`٢` → `2`), `*` runs as they are.

    A lone alef is the OCR's reading of `١` and counts as 1.
    """
    if marker == ALEF:
        return "1"
    if marker.startswith("*"):
        return marker
    digits = to_western_digits(marker.translate(_SUPERSCRIPT_DIGITS))
    return str(int(digits)) if digits.isdigit() else digits


@dataclass
class Note:
    """A footnote of a page; `page` is the page it starts on, `lines` may run onto the next page.

    `marker` is the marker that starts the note, `ref` the reference in the body it was linked to;
    the document's `marker` attr is the printed reference (the note's marker for an orphan).
    """

    id: str
    marker: str | None
    key: str | None
    page: int
    lines: list[LineIn]
    rich: Rich
    reviewed: bool = True
    number: int | None = None
    orphan: bool = False
    ref: str | None = None  # the reference as printed in the body text, once linked («²», «ا», «٢»)

    @property
    def line_ids(self) -> list[int]:
        return [line.id for line in self.lines]


# One stray character the model read before a bracketed number at a note line's start («أ (١) ويعني»).
STRAY_BEFORE_MARKER = "أاإآء.،:ـ-•*"  # not a letter that is a word of its own («و (٣)» is text)
_RE_STRAY_BEFORE_MARKER = re.compile(
    rf"^\s*[{re.escape(STRAY_BEFORE_MARKER)}]\s+(?=[\(\[]\s*{_DIGIT_RUN}{{1,3}}\s*[\)\]])"
)


def split_note_marker(text: str) -> tuple[str | None, int]:
    """`(marker, length)` of the marker that starts a footnote line (`(None, 0)` when there is none).

    Marker: `(n)`, `[n]`, `n`, `n-`, `n.` … with 1–3 Western or Arabic-Indic digits or `*`, `**`,
    `***` (spec regex), plus a lone alef read for `١`. A bare number followed by an era sign («١ م ه
    ولم ترض»: a year at the start of a note's second line, book 29 p. 82) is text, not a marker (D82).
    A bracketed lookalike («(أ)», «(”)») is returned as its glyph; `page_notes` numbers it by its page's
    sequence (D85).
    """
    stray = _RE_STRAY_BEFORE_MARKER.match(text or "")
    if stray:  # «أ (١) ويعني»: a stray mark the model read before the bracketed marker (book 35 p. 3, D87)
        marker, length = split_note_marker(text[stray.end() :])
        return (marker, stray.end() + length) if marker is not None else (None, 0)
    match = NOTE_MARKER.match(text or "")
    if not match:
        look = _NOTE_LOOKALIKE.match(text or "")
        return (look.group(1), look.end()) if look else (None, 0)
    if not any(char in "([" for char in match.group(0)):
        following = text[match.end() :].split()
        if following and following[0].rstrip("،,؛;") in ERA_WORDS:
            return None, 0
    return match.group(1), match.end()


def footnote_lines(page: PageIn) -> list[LineIn]:
    """The page's note lines with text, in reading order."""
    return [line for line in page.lines if line.kind == FOOTNOTE and (line.text or "").strip()]


def note_keys(page: PageIn) -> list[str]:
    """The keys of the markers that start the page's note lines, in order (`marker_key`)."""
    out: list[str] = []
    for line in footnote_lines(page):
        marker, _length = split_note_marker(Rich.from_line(line).text)
        if marker is not None:
            out.append(marker_key(marker))
    return out


_RE_CONTINUED = re.compile(r"^\s*=\s*")  # a note line printed with «=» continues the note of the page before


def continues(carry: Note | None, open_calls: int, text: str = "", first_below: str | None = None) -> bool:
    """The continuation guard (D74): a marker-less line at the top of a page's notes continues the
    previous page's note only when that note does not end with terminal punctuation and the page's
    body has no open call that the line could be the note of (`open_calls`: `open_calls_before`, the
    calls `link_footnotes` would give it, `positional_call`). A line the printer starts with «=» (the mark
    of a note carried over; books 32, 34) continues it whatever it ends with (D85). So does a line above the
    page's first new note (`first_below`, the key of the first marked note below it): that note is 1, or one
    more than `carry`'s, so what stands above it is no note of this page (book 31, a commentary running on
    over pages and breaking at a sentence's end, D87)."""
    if carry is not None and _RE_CONTINUED.match(text or ""):
        return True
    if carry is not None and first_below is not None and _starts_page(first_below, carry):
        return True
    return carry is not None and not open_calls and not ends_terminal(carry.rich.plain())


def _starts_page(key: str, carry: Note) -> bool:
    """True when a note numbered `key` is the first new note of a page after `carry`: 1, or one more than
    `carry`'s number in a book numbering its notes on (not 2 after 1: that page may have lost its «(١)»)."""
    if key == "1":
        return True
    return bool(
        carry.key and carry.key.isdigit() and key.isdigit() and key != "2" and int(key) == int(carry.key) + 1
    )


def _first_key_below(texts: list[str], index: int) -> str | None:
    """The key of the first bracketed numbered marker on the page's note lines after line `index`."""
    for text in texts[index + 1 :]:
        key = _bracketed_key(text)
        if key is not None:
            return key if key.isdigit() else None
    return None


def page_notes(page: PageIn, carry: Note | None, open_calls: int = 0) -> tuple[list[Note], Note | None]:
    """The notes of one page, and the note the next page may continue (§2.5).

    A footnote line that starts with a marker starts a note; a line without one continues the
    current note. The page's first footnote line without a marker continues `carry` (the previous
    page's last note) when the continuation guard lets it (`continues`: `carry` does not end a
    sentence and the page has no `open_calls`), else it starts a note without marker — the page's
    open call may take it (`link_footnotes`, `note_marker_missing`).
    """
    notes: list[Note] = []
    current: Note | None = None
    lines = footnote_lines(page)
    texts = [Rich.from_line(line).text for line in lines]
    bracketed = [_bracketed_key(text) for text in texts]
    lettered = any(_RE_LETTERED.match(text) for text in texts)
    for index, line in enumerate(lines):
        rich = Rich.from_line(line)
        marker, length = split_note_marker(rich.text)
        if marker is not None and marker in NOTE_LOOKALIKES and lettered:
            marker = None  # «(أ)» among «(ب)», «(ج)»: a lettered item of a note, not a marker
        if marker is not None and any(bracketed) and bracketed[index] is None:
            key = marker_key(marker)
            below = next((int(k) for k in bracketed[index + 1 :] if k and k.isdigit()), None)
            if key.isdigit() and (
                key not in _next_keys(notes, carry) or (below is not None and int(key) >= below)
            ):
                marker = None  # «١٢١ ـ» in a note of a page that prints «(١)», «=» read «3» above «(١)» (D85)
        elif marker is not None and bracketed[index] is None:
            key = marker_key(marker)
            if key.isdigit() and len(key) >= 2 and key not in _next_keys(notes, carry):
                marker = None  # a hadith's number «١٩ ـ» at a line's start, out of the notes' sequence (D87)
        if marker is not None:
            current = Note(
                id=note_id(line.id),
                marker=marker,
                key=marker_key(marker),
                page=page.number,
                lines=[line],
                rich=rich.cut(length),
                reviewed=page.reviewed,
            )
            notes.append(current)
        elif current is None and continues(carry, open_calls, rich.text, _first_key_below(texts, index)):
            sign = _RE_CONTINUED.match(rich.text)
            if sign:
                rich = rich.cut(sign.end())  # the printed «=» is the book's mark of a carried note, not text
            current = carry
            current.lines.append(line)
            current.rich = Rich.join([current.rich, rich]) if len(current.rich) else rich
            current.reviewed = current.reviewed and page.reviewed
        elif current is None:
            current = Note(note_id(line.id), None, None, page.number, [line], rich, reviewed=page.reviewed)
            notes.append(current)
        else:
            current.lines.append(line)
            current.rich = Rich.join([current.rich, rich]) if len(current.rich) else rich
    _number_lookalike_notes(notes)
    _repair_sequence(notes)
    restored = _restore_missing_number(notes, page)
    if restored is not None and current is not None and restored[0] is current:
        current = restored[1]  # the page's last note is now the restored one
    if not lines:
        return notes, None
    return notes, current


_RE_BRACKETED_MARKER = re.compile(
    rf"^\s*(?:[{re.escape(STRAY_BEFORE_MARKER)}]\s+)?[\(\[]\s*"
    rf"(?:{_DIGIT_RUN}{{1,3}}|\*{{1,3}}|[{ALEF}{re.escape(NOTE_LOOKALIKES)}])\s*[\)\]]"
)


def _bracketed_marker(text: str) -> bool:
    """True when a note line starts with a bracketed marker («(١)», «[٢]», «(*)», «(أ)»)."""
    return bool(_RE_BRACKETED_MARKER.match(text or ""))


def _bracketed_key(text: str) -> str | None:
    """The key of the bracketed marker a note line starts with («(٢)» → "2", «(أ)» → its glyph), else None."""
    if not _bracketed_marker(text):
        return None
    marker, _length = split_note_marker(text)
    return marker_key(marker) if marker is not None else None


def _next_keys(notes: Sequence[Note], carry: Note | None) -> set[str]:
    """The numbers the page's next note may carry: one more than its last numbered note; on a page
    without one, 1 or one more than the previous page's last note (a book numbering its notes on)."""
    numbers = [int(note.key) for note in notes if note.key and note.key.isdigit()]
    if numbers:
        return {str(numbers[-1] + 1)}
    out = {"1"}
    if carry is not None and carry.key and carry.key.isdigit():
        out.add(str(int(carry.key) + 1))
    return out


def _is_lookalike_marker(marker: str | None) -> bool:
    return marker is not None and len(marker) == 1 and marker in NOTE_LOOKALIKES


def _number_lookalike_notes(notes: list[Note]) -> None:
    """Number the notes whose marker was read as a lookalike (D85): one more than the numbered note
    before it on the page, else one less than the one after it, else 1; the number becomes the note's
    marker. When that number is taken on the page the note keeps no number (key None)."""
    taken = {note.key for note in notes if note.key and note.key.isdigit()}
    for i, note in enumerate(notes):
        if not _is_lookalike_marker(note.marker):
            continue
        before = next((int(n.key) for n in reversed(notes[:i]) if n.key and n.key.isdigit()), None)
        after = next((int(n.key) for n in notes[i + 1 :] if n.key and n.key.isdigit()), None)
        if before is not None:
            number = before + 1
        elif after is not None and after > 1:
            number = after - 1
        else:
            number = 1
        key = str(number)
        if key in taken:
            note.key = None
            continue
        taken.add(key)
        note.key = note.marker = key


def _repair_sequence(notes: list[Note]) -> None:
    """Note numbers misread against the page's sequence (D87): a number between two that differ by two is
    the one between them («(١)», «(٤)», «(٣)»: the model read «(٢)» as «٤», book 34 p. 2); the first,
    before a «(٢)», is 1; the last, not above the one before it, is one more. The number becomes the note's
    marker, as a lookalike's does (`_number_lookalike_notes`)."""
    numbered = [note for note in notes if note.key and note.key.isdigit()]
    keys = [int(note.key) for note in numbered]
    for i, note in enumerate(numbered):
        before = keys[i - 1] if i else None
        after = keys[i + 1] if i + 1 < len(keys) else None
        if before is not None and after is not None:
            fixed = before + 1 if after == before + 2 else None
        elif before is None and after is not None:
            fixed = 1 if after == 2 else None
        elif before is not None:
            fixed = before + 1 if keys[i] <= before else None
        else:
            fixed = None
        if fixed is None and before is not None and str(keys[i]) == f"{before + 1}1":
            fixed = before + 1  # «(٢١)» after «(١)»: the «(» stroke read as a one, as on the calls (D82)
        if fixed is not None and fixed != keys[i]:
            keys[i] = fixed
            note.key = note.marker = str(fixed)


_RE_STRAY_START = re.compile(r"^\s*[.،:؛]\s*")  # the last mark of the line before, moved to this one


def _restore_missing_number(notes: list[Note], page: PageIn) -> tuple[Note, Note] | None:
    """A number missing from the page's notes (D87): «(١)» then «(٣)», and between them the lines of note 1
    of which exactly one may start a note — the only one, or the one after a line that ends a sentence, or
    one that starts with the mark of the line before («. راجع تقديمه», book 35 p. 2: the model dropped
    «(٢)» and moved the period of the line above). That line starts note 2 (its number is its marker; the
    moved mark goes back to the note before). Returns `(note before, restored note)` of the last split, or
    None. One number per gap: a longer gap is left as it is."""
    last = None
    i = 0
    while i + 1 < len(notes):
        note, following = notes[i], notes[i + 1]
        i += 1
        if not (note.key and note.key.isdigit() and following.key and following.key.isdigit()):
            continue
        if int(following.key) != int(note.key) + 2 or note.page != page.number or len(note.lines) < 2:
            continue
        if any(line not in page.lines for line in note.lines):
            continue  # a note carried from the page before: its lines are not all this page's
        texts = [Rich.from_line(line).text for line in note.lines]
        starts = [
            k
            for k in range(1, len(note.lines))
            if len(note.lines) == 2 or ends_terminal(texts[k - 1]) or _RE_STRAY_START.match(texts[k])
        ]
        if len(starts) != 1:
            continue
        k = starts[0]
        _marker, length = split_note_marker(texts[0])
        first = Rich.from_line(note.lines[0])
        head = Rich.join([first.cut(length), *(Rich.from_line(line) for line in note.lines[1:k])])
        tail = Rich.join([Rich.from_line(line) for line in note.lines[k:]])
        stray = _RE_STRAY_START.match(tail.text)
        if stray:
            mark = tail.cut(0, stray.end()).text.strip()
            if mark and not ends_terminal(head.plain()):
                head = head + Rich.of(mark)
            tail = tail.cut(stray.end())
        key = str(int(note.key) + 1)
        restored = Note(
            id=note_id(note.lines[k].id),
            marker=key,
            key=key,
            page=note.page,
            lines=note.lines[k:],
            rich=tail,
            reviewed=note.reviewed,
        )
        note.lines, note.rich = note.lines[:k], head
        notes.insert(i, restored)
        last = (note, restored)
        i += 1
    return last


@dataclass(frozen=True)
class Candidate:
    """A possible footnote reference in a block's text."""

    block: int
    start: int
    end: int
    cut: int  # where the replaced span starts (the whitespace before the marker goes with it)
    key: str
    marker: str
    style: str  # bracket | superscript | glued | quoted | standalone | alef
    page: int
    line: int | None
    rank: int = 0  # linking priority: 0 strong styles, 1 weak before a mark or the end, 2 weak before a word


def find_candidates(block_index: int, rich: Rich, line_page: dict[int, int]) -> list[Candidate]:
    """Footnote reference candidates of one block, in reading order (§2.5).

    `(n)` `[n]` `(*)`; superscript digits; a digit run glued to the end of a word (`الفيل٢`, also
    after a closing quote `»١` or a period `الخ .1`); a standalone token of 1–2 digits
    (`الفيل ٢ مرحلة`; `quoted` right after a closing quote or bracket, `دينار » ١ ،`); a lone
    alef (`» ا .`, the OCR's `١`); a bracketed lookalike glyph (`(ا)`, `(”)`, `( )`: the models'
    readings of a small raised call, `LOOKALIKE`, key '' since its number is unknown; D82), also with one
    bracket lost when it is a word of its own (`(”`, `(ا`, `”)`; D85).
    Overlapping matches keep the first style in that order. A standalone number that starts a list
    item (`3 ـ كتاب`) or is a year (`سنة 21 ه`) is text.
    """
    text = rich.text
    found: list[tuple[int, int, str, str]] = []
    taken = [False] * len(text)

    def add(start: int, end: int, marker: str, style: str) -> None:
        if any(taken[start:end]):
            return
        for i in range(start, end):
            taken[i] = True
        found.append((start, end, marker, style))

    for match in _RE_BRACKETED.finditer(text):
        add(match.start(), match.end(), match.group(1), "bracket")
    for match in _RE_LOOKALIKE.finditer(text):
        add(match.start(), match.end(), match.group(1), LOOKALIKE)
    for match in _RE_HALF_LOOKALIKE.finditer(text):
        add(match.start(), match.end(), match.group(1) or match.group(2), LOOKALIKE)
    for match in _RE_QUOTED_READING.finditer(text):
        glyph = match.group(1)
        add(match.start(), match.end(), glyph, LOOKALIKE if glyph in "اأإآ" else "quoted")
    for match in _RE_QUOTE_PAIR.finditer(text):
        add(match.start(), match.end(), match.group(1), LOOKALIKE)
    for match in _RE_SUPERSCRIPT.finditer(text):
        add(match.start(), match.end(), match.group(), "superscript")
    for match in _RE_GLUED.finditer(text):
        add(match.start(1), match.end(1), match.group(1), "glued")
    for match in _RE_STANDALONE.finditer(text):
        if _is_text_number(text, match.start(1), match.end(1)):
            continue
        before = text[: match.start(1)].rstrip()
        style = "quoted" if before and before[-1] in _QUOTED_AFTER else "standalone"
        add(match.start(1), match.end(1), match.group(1), style)
    for match in _RE_ALEF.finditer(text):
        add(match.start(), match.end(), match.group(), "alef")
    out = []
    for start, end, marker, style in sorted(found):
        cut = start
        while cut > 0 and text[cut - 1].isspace():
            cut -= 1
        line = rich.meta[start].line
        page = line_page.get(line) if line is not None else None
        if page is None:
            continue
        rank = 0 if style in STRONG_STYLES else (1 if _before_mark(text, end) else 2)
        key = "" if style == LOOKALIKE else marker_key(marker)
        out.append(Candidate(block_index, start, end, cut, key, marker, style, page, line, rank))
    return out


def _is_text_number(text: str, start: int, end: int) -> bool:
    """A standalone number that is text: a list number before a dash, or a year (`سنة 21 ه`)."""
    if _RE_LIST_DASH.match(text, end):
        return True
    previous = _RE_STRIP_MARKS.sub("", text[:start].split()[-1]) if text[:start].split() else ""
    following = text[end:].split()
    return previous in YEAR_WORDS or bool(following and following[0].rstrip("،,؛;") in ERA_WORDS)


def _before_mark(text: str, end: int) -> bool:
    """True when only spaces separate `end` from a mark, a closer, an inline node or the end."""
    rest = text[end:].lstrip()
    return not rest or rest[0] in MARKS or rest[0] in CLOSE_BRACKETS or rest[0] in "»”" + PLACEHOLDERS


def page_candidates(blocks: Sequence[Block], line_page: dict[int, int]) -> dict[int, list[Candidate]]:
    """Every block's reference candidates (`find_candidates`), by the page they sit on, in reading order."""
    by_page: dict[int, list[Candidate]] = {}
    for index, block in enumerate(blocks):
        for cand in find_candidates(index, block.rich, line_page):
            by_page.setdefault(cand.page, []).append(cand)
    return by_page


def is_leading(cand: Candidate, blocks: Sequence[Block]) -> bool:
    """True for a candidate that starts its block: the marker of a note left in the body («(1) انظر …»),
    never the call of another note."""
    return not blocks[cand.block].rich.text[: cand.start].strip()


def is_call(cand: Candidate, blocks: Sequence[Block]) -> bool:
    """A candidate that reads as a call on its own: a strong style (bracket, superscript, glued, quoted)
    not at a block's start (`stray_notes` pairs such a call with a paragraph's leading marker)."""
    return cand.style in STRONG_STYLES and not is_leading(cand, blocks)


def positional_call(cand: Candidate, blocks: Sequence[Block], keys: Iterable[str] = ()) -> bool:
    """A call a note without a marker may take by its place alone (D74), with nothing on the note to
    confirm the number: a bracketed or superscript call (`is_call`, `POSITIONAL_STYLES`) of `*` or of
    1–`POSITIONAL_MAX`, below the smallest number of the page's marker notes (`keys`, `note_keys`: a
    marker-less note heads its page's notes). Glued and quoted digits, a bracketed price «(450)» or page
    «(241)» stay text: on the dev books they were OCR noise («ص٩») or not calls (books 1, 4, 16, 19)."""
    if cand.style not in POSITIONAL_STYLES or not is_call(cand, blocks):
        return False
    if cand.key.startswith("*"):
        return True
    if not cand.key.isdigit() or not 1 <= int(cand.key) <= POSITIONAL_MAX:
        return False
    numbers = [int(key) for key in keys if key.isdigit()]
    return not numbers or int(cand.key) < min(numbers)


def open_calls_before(
    cands: Sequence[Candidate], keys: Iterable[str], blocks: Sequence[Block]
) -> list[Candidate]:
    """The calls of a page that a note without a marker could take (`positional_call`) and that none of
    its marker notes takes, before linking: each key of `keys` (the page's note markers, `note_keys`)
    takes the first strong call with that key. What the continuation guard counts (`continues`)."""
    keys = list(keys)
    pool = [cand for cand in cands if is_call(cand, blocks)]
    for key in keys:
        hit = next((cand for cand in pool if cand.key == key), None)
        if hit is not None:
            pool.remove(hit)
    return [cand for cand in pool if positional_call(cand, blocks, keys)]


def two_digit_call(
    note: Note, notes: Sequence[Note], cands: Sequence[Candidate], used: set[int], blocks: Sequence[Block]
) -> int | None:
    """The index of the bracketed call «(n١)» that is `note`'s call read with a «١» hung on it (D82):
    `note` has a one-digit number n (1–`REPAIR_MAX`) and no call, no note of its page is numbered n1,
    and the page's body holds an unused bracketed «(n1)» that does not start its block. None otherwise.
    Book 29: «(١١)», «(٢١)» for the printed (١), (٢) on nine pages; a «(٤١)» for (٢) is left alone."""
    if note.key is None or not note.key.isdigit() or not 1 <= int(note.key) <= REPAIR_MAX:
        return None
    wanted = note.key + "1"
    if any(other.key == wanted for other in notes):
        return None
    return next(
        (
            i
            for i, cand in enumerate(cands)
            if i not in used
            and cand.style == "bracket"
            and cand.key == wanted
            and not is_leading(cand, blocks)
        ),
        None,
    )


def lookalike_calls(
    notes: Sequence[Note],
    unlinked: Sequence[Note],
    cands: Sequence[Candidate],
    used: set[int],
    linked_at: dict[int, int],
    blocks: Sequence[Block],
) -> list[tuple[Note, int]]:
    """The page's lookalike calls (`LOOKALIKE`: «(ا)», «(”)», «( )») paired with the notes they can
    only be (D82), as `(note, candidate index)`; empty unless every pair is certain.

    The notes still without a call whose number is small (1–`POSITIONAL_MAX`, or a marker-less note,
    which heads its page's notes) and the page's unused lookalikes, in reading order, pair one to one
    only when they are as many, and each lookalike lies after the calls of the notes before its note
    and before the calls of the notes after it (`linked_at`: note → the index of its call). A lookalike
    at a block's start is a marker left in the body, never a call.
    """
    wanting = _wanting(unlinked)
    looks = [
        i
        for i, cand in enumerate(cands)
        if i not in used and cand.style == LOOKALIKE and not is_leading(cand, blocks)
    ]
    return _pair_in_order(notes, wanting, looks, linked_at)


def _wanting(unlinked: Sequence[Note]) -> list[Note]:
    """The notes still without a call that a repaired call may take: a small number (1–
    `POSITIONAL_MAX`) or no marker (a marker-less note heads its page's notes)."""
    return [
        note
        for note in unlinked
        if note.key is None or (note.key.isdigit() and 1 <= int(note.key) <= POSITIONAL_MAX)
    ]


def _pair_in_order(
    notes: Sequence[Note], wanting: Sequence[Note], picks: Sequence[int], linked_at: dict[int, int]
) -> list[tuple[Note, int]]:
    """`wanting` and `picks` (candidate indexes in reading order) paired one to one, or nothing: they must
    be as many, and each pick must lie after the calls of the notes before its note and before the
    calls of the notes after it (`linked_at`: note → the index of its call)."""
    if not wanting or len(wanting) != len(picks):
        return []
    order = {id(note): k for k, note in enumerate(notes)}
    pairs = list(zip(wanting, picks, strict=True))
    for note, i in pairs:
        for other in notes:
            at = linked_at.get(id(other))
            if at is None:
                continue
            if order[id(other)] < order[id(note)] and at > i:
                return []
            if order[id(other)] > order[id(note)] and at < i:
                return []
    return pairs


def leftover_calls(
    notes: Sequence[Note],
    unlinked: Sequence[Note],
    cands: Sequence[Candidate],
    used: set[int],
    linked_at: dict[int, int],
    blocks: Sequence[Block],
) -> list[tuple[Note, int]]:
    """The page's bracketed or superscript calls whose small number (1–`POSITIONAL_MAX`: a page
    reference «(22)» is text, D74) no note of the page carries — a misread digit, «(٦)» for (١), «(3)» for
    (٢) (book 29 pp. 59, 256) — paired with the notes still without a call when they can only be theirs
    (D85): as many, in the page's order (`_pair_in_order`). A call at a block's start is a marker left in
    the body, never a call."""
    keys = {note.key for note in notes if note.key}
    loose = [
        i
        for i, cand in enumerate(cands)
        if i not in used
        and cand.style in POSITIONAL_STYLES
        and cand.key.isdigit()
        and 1 <= int(cand.key) <= POSITIONAL_MAX
        and cand.key not in keys
        and not is_leading(cand, blocks)
    ]
    return _pair_in_order(notes, _wanting(unlinked), loose, linked_at)


def _repaired_warning(page_number: int, note: Note, cand: Candidate, block_id: str | None) -> AssemblyWarning:
    read = f"({cand.marker})" if cand.style == LOOKALIKE else digits_in(cand.marker)
    which = f"علامة الحاشية «{digits_in(note.marker)}»" if note.marker else "علامة حاشية بلا علامة"
    return AssemblyWarning(
        "note_call_repaired",
        "warning",
        page_number,
        f"{which} في الصفحة {page_number} قُرئت «{read}» في المتن؛ رُبطت بالحاشية؛ تحقّق منها.",
        block_id,
        note.line_ids,
        marker=note.key,
    )


@dataclass
class _PageLinks:
    """The linking state of one page: its candidates, the ones `used`, each linked note's call
    (`linked_at`: id(note) → index in `cands`), and the book-wide `chosen` links by block."""

    cands: Sequence[Candidate]
    chosen: dict[int, list[tuple[Candidate, Note]]]
    used: set[int] = field(default_factory=set)
    linked_at: dict[int, int] = field(default_factory=dict)

    def take(self, note: Note, pick: int, ref: str | None) -> Candidate:
        """Link `note` to candidate `pick`; `ref` is the reference the document prints for it."""
        self.used.add(pick)
        cand = self.cands[pick]
        self.linked_at[id(note)] = pick
        note.ref = ref
        self.chosen.setdefault(cand.block, []).append((cand, note))
        return cand


def link_footnotes(
    blocks: list[Block],
    notes_by_page: dict[int, list[Note]],
    line_page: dict[int, int],
    by_page: dict[int, list[Candidate]] | None = None,
) -> tuple[list[Note], list[AssemblyWarning]]:
    """Replace the markers of linked notes by footnote nodes; returns the orphan notes and warnings.

    A candidate is linked only when a note of the same page carries that number (or `*`); each note
    takes the first unused matching candidate of the page, bracketed / superscript / glued / quoted
    ones first, then standalone digits and alefs before a mark or the end, then those before a
    word. A one-digit note left without a call takes its page's bracketed «(n١)» when no note is
    numbered so (`two_digit_call`, D82). Then the k-th note without a marker takes the k-th call still
    open on its page that it may take by place (`positional_call`, in reading order) with the warning
    `note_marker_missing` (D74; only a page's first note lines lack a marker, so k is 1 in practice).
    Then the notes still without a call pair with the page's lookalike glyphs when they can only be
    theirs (`lookalike_calls`, D82). A repaired link warns `note_call_repaired` (the note's own marker
    is printed); a note left without any call is an orphan (in the order of the page's notes). A strong
    candidate left over on a page that has notes → `marker_unmatched` (the text stays as printed).
    `by_page` is `page_candidates` when the caller has it.
    """
    if by_page is None:
        by_page = page_candidates(blocks, line_page)
    chosen: dict[int, list[tuple[Candidate, Note]]] = {}
    orphans: list[Note] = []
    warnings: list[AssemblyWarning] = []
    for page_number in sorted(notes_by_page):
        notes = notes_by_page[page_number]
        cands = by_page.get(page_number, [])
        links = _PageLinks(cands, chosen)
        used, linked_at, take = links.used, links.linked_at, links.take
        unlinked: list[Note] = []
        for note in notes:
            pick = None
            if note.key is not None:
                for rank in (0, 1, 2):
                    pick = next(
                        (
                            i
                            for i, c in enumerate(cands)
                            if i not in used and c.key == note.key and c.rank == rank
                        ),
                        None,
                    )
                    if pick is not None:
                        break
            if pick is None:
                unlinked.append(note)
                continue
            take(note, pick, cands[pick].marker)
        for note in list(unlinked):
            pick = two_digit_call(note, notes, cands, used, blocks)
            if pick is not None:
                cand = take(note, pick, note.marker)
                unlinked.remove(note)
                warnings.append(_repaired_warning(page_number, note, cand, blocks[cand.block].id))
        keys = [note.key for note in notes if note.key is not None]
        free = [i for i, cand in enumerate(cands) if i not in used and positional_call(cand, blocks, keys)]
        still: list[Note] = []
        for note in unlinked:
            if note.key is not None or not free:
                still.append(note)
                continue
            pick = free.pop(0)
            cand = take(note, pick, cands[pick].marker)
            shown = digits_in(cand.marker.translate(_SUPERSCRIPT_DIGITS))
            warnings.append(
                AssemblyWarning(
                    "note_marker_missing",
                    "warning",
                    page_number,
                    f"حاشية بلا علامة رُبطت بالعلامة ({shown})؛ تحقّق منها.",
                    blocks[cand.block].id,
                    note.line_ids,
                    marker=cand.key,
                )
            )
        for note, pick in lookalike_calls(notes, still, cands, used, linked_at, blocks):
            cand = take(note, pick, note.marker)
            still.remove(note)
            warnings.append(_repaired_warning(page_number, note, cand, blocks[cand.block].id))
        for note, pick in leftover_calls(notes, still, cands, used, linked_at, blocks):
            cand = take(note, pick, note.marker)
            still.remove(note)
            warnings.append(_repaired_warning(page_number, note, cand, blocks[cand.block].id))
        orphans.extend(still)
        if notes:
            for i, cand in enumerate(cands):
                if i not in used and cand.style in STRONG_STYLES:
                    shown = digits_in(cand.marker.translate(_SUPERSCRIPT_DIGITS))
                    warnings.append(
                        AssemblyWarning(
                            "marker_unmatched",
                            "warning",
                            page_number,
                            f"علامة الحاشية «{shown}» في الصفحة {page_number} بلا حاشية مقابلة.",
                            blocks[cand.block].id,
                            [cand.line] if cand.line is not None else [],
                        )
                    )
    for index, links in chosen.items():
        block = blocks[index]
        rich = block.rich
        for cand, note in sorted(links, key=lambda item: item[0].start, reverse=True):
            rich = rich.cut(0, cand.cut) + Rich.node(FN, note, cand.line) + rich.cut(cand.end)
        block.rich = rich
    return orphans, warnings


def attach_orphans(
    blocks: list[Block], orphans: Sequence[Note], line_page: dict[int, int]
) -> list[AssemblyWarning]:
    """Append each orphan note to its page's last body block (right after that page's text); warn.

    A page without body text puts its orphans on the nearest block before it (else after it); a book
    without any block gets one paragraph holding them.
    """
    warnings: list[AssemblyWarning] = []
    for note in orphans:
        note.orphan = True
        host = None
        for index in range(len(blocks) - 1, -1, -1):
            if note.page in blocks[index].pages:
                host = index
                break
        if host is None:
            before = [i for i, b in enumerate(blocks) if b.pages and max(b.pages) < note.page]
            after = [i for i, b in enumerate(blocks) if b.pages and min(b.pages) > note.page]
            host = before[-1] if before else (after[0] if after else None)
        if host is None:
            blocks.append(
                Block(
                    id=block_id("paragraph", note.lines[0].id),
                    kind="paragraph",
                    level=0,
                    lines=list(note.lines),  # its sources are the note's lines
                    pages=[note.page],
                    shapes=[],
                    rich=Rich(),
                    reviewed=note.reviewed,
                )
            )
            host = len(blocks) - 1
        block = blocks[host]
        rich = block.rich
        position = len(rich.text)
        if note.page in block.pages:
            for i in range(len(rich.meta) - 1, -1, -1):
                line = rich.meta[i].line
                if line is not None and line_page.get(line) == note.page and rich.text[i] not in PLACEHOLDERS:
                    position = i + 1
                    break
            # after the orphans of this page already placed there, so they keep the notes' order
            while (
                position < len(rich.text)
                and rich.text[position] == FN
                and getattr(rich.meta[position].node, "orphan", False)
            ):
                position += 1
        line = note.lines[0].id if note.lines else None
        block.rich = rich.cut(0, position) + Rich.node(FN, note, line) + rich.cut(position)
        shown = f"«{digits_in(note.marker)}» " if note.marker else ""
        warnings.append(
            AssemblyWarning(
                "note_orphan",
                "warning",
                note.page,
                f"الحاشية {shown}في الصفحة {note.page} بلا علامة في المتن؛ أُلحقت بآخر فقرة من الصفحة.",
                block.id,
                note.line_ids,
            )
        )
    return warnings


STRAY_NOTE_ACTIONS: tuple[tuple[str, str], ...] = (("footnote", "جعلها حاشية"), ("go", "انتقال"))
# A paragraph's leading note marker: bracketed or starred only (playground/phase7/probe_stray2.py; a bare
# «١ –» starts a list item, book 26).
_RE_LEADING_MARKER = re.compile(rf"^\s*[\(\[]\s*([{_DIGITS}]{{1,3}}|\*{{1,3}})\s*[\)\]]\s*[-–.:،]?\s*")


def leading_marker(text: str) -> str | None:
    """The key (`marker_key`) of the bracketed note marker a paragraph's text starts with when words
    follow it («(1) انظر …» → `1`); None otherwise."""
    match = _RE_LEADING_MARKER.match(text or "")
    if match is None or not any(char.isalpha() for char in text[match.end() :]):
        return None
    return marker_key(match.group(1))


def stray_notes(blocks: Sequence[Block], line_page: dict[int, int]) -> list[AssemblyWarning]:
    """Paragraphs that may be a note left in the body (D74), after linking; sets `Block.note_for`.

    A body paragraph (not a verse line) that starts with a bracketed note marker (`leading_marker`), on
    a page whose body still has an open call with that number (`is_call`, what `link_footnotes` left),
    gets `note_for` = that number: the manuscript's paragraph menu then reads «حاشية للعلامة (n)». When
    the paragraph also lies at the end of its page's text (one source page, in the run of such
    paragraphs that ends the page, not the whole page: probe_stray2's page-end rule) it gets the warning
    `stray_note` with the actions «جعلها حاشية» (the footnote role on its lines) and «انتقال». The
    readiness row `stray_notes` keeps its own measured rule (critic 1.6).
    """
    calls: dict[int, set[str]] = {}
    for page, cands in page_candidates(blocks, line_page).items():
        calls[page] = {cand.key for cand in cands if is_call(cand, blocks)}
    markers: dict[int, str] = {}  # block index → the key of its leading marker (body paragraphs)
    for index, block in enumerate(blocks):
        if block.kind == "paragraph" and not block.style and block.pages:
            key = leading_marker(block.rich.plain())
            if key is not None:
                markers[index] = key
                if key in calls.get(block.pages[0], set()):
                    block.note_for = key
    by_page: dict[int, list[int]] = {}  # page → indexes of its blocks, in order
    for index, block in enumerate(blocks):
        for number in block.pages:
            by_page.setdefault(number, []).append(index)
    warnings: list[AssemblyWarning] = []
    for page, indexes in sorted(by_page.items()):
        tail: list[int] = []  # the run of one-page marker-initial paragraphs that ends the page
        for index in reversed(indexes):
            if index not in markers or blocks[index].pages != [page]:
                break
            tail.append(index)
        if len(tail) == len(indexes):  # the whole page (a numbered list), not a run that ends it
            continue
        for index in reversed(tail):
            block = blocks[index]
            key = markers[index]
            if block.note_for is None:
                continue
            warnings.append(
                AssemblyWarning(
                    "stray_note",
                    "warning",
                    page,
                    f"فقرة في الصفحة {page} تبدأ بعلامة حاشية «({key})» ولم تُربط.",
                    block.id,
                    block.line_ids,
                    marker=key,
                    actions=[
                        {
                            "key": "footnote",
                            "label": STRAY_NOTE_ACTIONS[0][1],
                            "role": FOOTNOTE,
                            "lineIds": block.line_ids,
                        },
                        {"key": "go", "label": STRAY_NOTE_ACTIONS[1][1], "blockId": block.id},
                    ],
                )
            )
    return warnings


def number_footnotes(blocks: Sequence[Block], mode: str) -> None:
    """Number the footnote nodes in document order: per chapter (restart at each level-1 heading),
    through the book, or per source page (§2.5)."""
    counter = 0
    per_page: dict[int, int] = {}
    for block in blocks:
        if mode == "chapter" and block.kind == "heading" and block.level == 1:
            counter = 0
        for note in block.rich.nodes(FN):
            if mode == "page":
                per_page[note.page] = per_page.get(note.page, 0) + 1
                note.number = per_page[note.page]
            else:
                counter += 1
                note.number = counter


# ====================================================================== 2.6 headings and suggestions


def suggest_headings(blocks: Sequence[Block], dismissed: frozenset[str] | set[str]) -> None:
    """Mark heading suggestions (never applied): a paragraph of 1–2 centred lines, not ending a
    sentence, at most 8 words, followed by a paragraph, and not dismissed. A verse line (centred, short)
    is never one."""
    for index, block in enumerate(blocks):
        block.suggested = None
        if block.kind != "paragraph" or block.style or not 1 <= len(block.lines) <= MAX_SUGGESTION_LINES:
            continue
        if block.id in dismissed or not all(shape is not None and shape.centred for shape in block.shapes):
            continue
        text = block.rich.plain()
        words = word_count(text)
        if not words or words > MAX_SUGGESTION_WORDS or ends_sentence(text):
            continue
        following = blocks[index + 1] if index + 1 < len(blocks) else None
        if following is None or following.kind != "paragraph":
            continue
        block.suggested = "heading"


def heading_warnings(blocks: Sequence[Block]) -> list[AssemblyWarning]:
    """`no_headings` (info) when the book has no level-1 heading."""
    if any(block.kind == "heading" and block.level == 1 for block in blocks):
        return []
    return [
        AssemblyWarning(
            "no_headings",
            "info",
            None,
            "لا يوجد عنوان رئيسي في الكتاب؛ حدّد عناوين الفصول ليُقسَّم الكتاب إلى فصول.",
        )
    ]


# ====================================================================== 2.2 typography over blocks


def move_leading_marks(blocks: list[Block]) -> None:
    """A block that starts with a mark glued to its first word gives the mark to the block before it.

    D37 rule 1 at a block start: «الحالية» ⏎ «.ويؤكد» → «الحالية.» ⏎ «ويؤكد». Without a paragraph
    before it (first block, or a heading), or when that block already ends with the same mark, the
    mark is dropped.
    """
    for index, block in enumerate(blocks):
        match = _RE_LEADING_MARKS.match(block.rich.text)
        if not match:
            continue
        marks = block.rich.cut(0, match.end(1))
        block.rich = block.rich.cut(match.end())
        if index == 0 or blocks[index - 1].kind != "paragraph":
            continue
        prev = blocks[index - 1]
        tail = prev.rich.plain().rstrip()
        if tail.endswith(marks.text):
            continue
        prev.rich = prev.rich + marks


def typeset_blocks(
    blocks: list[Block], notes: Iterable[Note], settings: Settings, digit_style: str
) -> list[Block]:
    """D37 on every block and note; blocks left without text or nodes are dropped."""
    for note in notes:
        note.rich = normalize_rich(note.rich, settings.strip_tatweel, digit_style)
    for block in blocks:
        block.rich = normalize_rich(block.rich, settings.strip_tatweel, digit_style)
    move_leading_marks(blocks)
    for block in blocks:
        block.rich = collapse_spaces(block.rich)
    return [block for block in blocks if block.rich.text.strip()]


# ====================================================================== 2.7 uncertain words


def uncertain_warnings(pages: Iterable[PageIn], blocks: Sequence[Block]) -> list[AssemblyWarning]:
    """One `uncertain_words` warning (info) per included page with unresolved words."""
    first_block: dict[int, str] = {}
    for block in blocks:
        for line_id in block.line_ids:
            first_block.setdefault(line_id, block.id)
    out = []
    for page in pages:
        lines = [line for line in page.lines if line.uncertain and (line.text or "").strip()]
        count = sum(len(line.uncertain) for line in lines)
        if not count:
            continue
        host = next((first_block[line.id] for line in lines if line.id in first_block), None)
        out.append(
            AssemblyWarning(
                "uncertain_words",
                "info",
                page.number,
                f"بقيت كلمات غير محسومة في الصفحة {page.number} ({count}).",
                host,
                [line.id for line in lines],
            )
        )
    return out


# ====================================================================== 2.8 document


def inline_content(rich: Rich) -> list[dict]:
    """ProseMirror inline nodes of a text: text runs (uncertain words marked) and inline nodes."""
    out: list[dict] = []
    buffer: list[str] = []
    marked = False

    def flush() -> None:
        if buffer:
            node: dict = {"type": "text", "text": "".join(buffer)}
            if marked:
                node["marks"] = [{"type": "uncertain"}]
            out.append(node)
            buffer.clear()

    for char, meta in zip(rich.text, rich.meta, strict=True):
        if char in PLACEHOLDERS:
            flush()
            out.append(_inline_node(meta.node))
            continue
        flag = meta.uncertain and not char.isspace()
        if flag != marked:
            flush()
            marked = flag
        buffer.append(char)
    flush()
    return out


def _inline_node(node) -> dict:
    if isinstance(node, PageBreak):
        return {"type": "pageBreak", "attrs": {"page": node.page, "printed": node.printed}}
    if isinstance(node, Note):
        return {
            "type": "footnote",
            "attrs": {
                "id": node.id,
                "number": node.number,
                "marker": node.ref if node.ref is not None else node.marker,
                "sourcePage": node.page,
                "sourceLineIds": node.line_ids,
                "orphan": node.orphan,
            },
            "content": inline_content(node.rich),
        }
    raise TypeError(f"unknown inline node {node!r}")


def block_node(block: Block) -> dict:
    """The ProseMirror node of a paragraph or heading, with its source mapping. A verse line's paragraph
    carries `style: "verse"` (the editor's paragraph style), and a marker-initial paragraph whose page
    has an open call with its number `noteFor` (that number: the manuscript's «حاشية للعلامة (n)»)."""
    attrs: dict = {
        "id": block.id,
        "sourcePages": list(block.pages),
        "sourceLineIds": block.line_ids,
        "reviewed": block.reviewed,
    }
    if block.kind == "heading":
        return {
            "type": "heading",
            "attrs": {"level": block.level, **attrs},
            "content": inline_content(block.rich),
        }
    attrs["suggestedRole"] = block.suggested
    if block.style:
        attrs["style"] = block.style
    if block.note_for is not None:
        attrs["noteFor"] = block.note_for
    return {"type": "paragraph", "attrs": attrs, "content": inline_content(block.rich)}


def build_document(blocks: Sequence[Block], meta: BookMeta, settings: Settings, seams: list[dict]) -> dict:
    """The manuscript document (§2.8): a title node, then the blocks in order."""
    return {
        "type": "doc",
        "attrs": {
            "bookId": meta.id,
            "runId": meta.run_id,
            "assembledAt": meta.assembled_at,
            "footnoteNumbering": settings.footnote_numbering,
            "digitStyle": meta.digit_style,
            "seams": seams,
        },
        "content": [
            {"type": "title", "attrs": {"text": meta.title, "author": meta.author}},
            *(block_node(block) for block in blocks),
        ],
    }


# ====================================================================== 2.10 stats


def compute_stats(selection: Selection, blocks: Sequence[Block], seams: Sequence[dict]) -> dict:
    """Counts of the run (§2.10), plus `chapters` (level-1 headings)."""
    notes = [note for block in blocks for note in block.rich.nodes(FN)]
    words = sum(word_count(block.rich.text) for block in blocks) + sum(word_count(n.rich.text) for n in notes)
    return {
        "pages_included": len(selection.included),
        "pages_skipped": len(selection.skipped),
        "pages_unreviewed": sum(1 for page in selection.included if not page.reviewed),
        "headings": sum(1 for block in blocks if block.kind == "heading"),
        "chapters": sum(1 for block in blocks if block.kind == "heading" and block.level == 1),
        "paragraphs": sum(1 for block in blocks if block.kind == "paragraph"),
        "footnotes": len(notes),
        "joins": sum(1 for seam in seams if seam["mode"] == "join"),
        "words": words,
    }


# ====================================================================== the whole run


@dataclass
class Result:
    """What `assemble` returns; `warnings` are dicts in the §2.9 shape, `pages` the included page ids."""

    document: dict
    warnings: list[dict]
    stats: dict
    seams: list[dict]
    pages: list[int] = field(default_factory=list)  # ids of the included pages


def _book_meta(meta: BookMeta | dict) -> BookMeta:
    if isinstance(meta, BookMeta):
        return meta
    style = meta.get("digit_style") or "western"
    return BookMeta(
        id=int(meta.get("id") or 0),
        title=str(meta.get("title") or ""),
        author=str(meta.get("author") or ""),
        digit_style=style if style in DIGIT_STYLES else "western",
        run_id=meta.get("run_id"),
        assembled_at=meta.get("assembled_at"),
    )


def assemble(
    pages: Iterable[PageIn],
    settings: Settings | dict | None,
    book_meta: BookMeta | dict,
    on_stage: Callable[[str], None] | None = None,
) -> Result:
    """Run the whole pipeline on a book's pages (§2); `on_stage(key)` is told when each step starts.

    Steps: `paragraphs`, `seams`, `footnotes`, `headings`, `typography` (the caller reports
    `collect` and `save` around it).
    """
    stage = on_stage or (lambda key: None)
    options = normalize_settings(settings)
    meta = _book_meta(book_meta)
    pages = list(pages)
    selection = select_pages(pages, options)
    line_page = {line.id: page.number for page in selection.included for line in page.lines}

    stage("paragraphs")
    head_warnings: list[AssemblyWarning] = []
    heads = 0
    if options.strip_running_heads:
        selection.included, head_warnings, heads = drop_running_heads(selection.included)
    page_blocks = [(page, split_paragraphs(page)) for page in selection.included]
    empty = [
        AssemblyWarning("empty_page", "info", page.number, f"الصفحة {page.number} بلا نص في المتن.")
        for page, blocks in page_blocks
        if not blocks
    ]

    stage("seams")
    blocks, seams = join_pages(page_blocks, options.seams, selection.gaps)

    stage("footnotes")
    notes_by_page: dict[int, list[Note]] = {}
    carry: Note | None = None
    by_page = page_candidates(blocks, line_page)
    for page in selection.included:
        if page.number in selection.gaps:
            carry = None
        open_calls = open_calls_before(by_page.get(page.number, []), note_keys(page), blocks)
        notes, carry = page_notes(page, carry, len(open_calls))
        if notes:
            notes_by_page[page.number] = notes
    orphans, footnote_warnings = link_footnotes(blocks, notes_by_page, line_page, by_page)
    footnote_warnings += attach_orphans(blocks, orphans, line_page)
    footnote_warnings += stray_notes(blocks, line_page)

    stage("headings")
    suggest_headings(blocks, options.dismissed_suggestions)
    number_footnotes(blocks, options.footnote_numbering)

    stage("typography")
    all_notes = [note for notes in notes_by_page.values() for note in notes]
    blocks = typeset_blocks(blocks, all_notes, options, meta.digit_style)

    first_block_of_page: dict[int, str] = {}
    for block in blocks:
        for number in block.pages:
            first_block_of_page.setdefault(number, block.id)
    page_warnings = []
    for warning in selection.warnings:
        if warning.code == "page_unreviewed" and warning.page in first_block_of_page:
            warning.block_id = first_block_of_page[warning.page]
        page_warnings.append(warning)
    warnings = sort_warnings(
        [
            *heading_warnings(blocks),
            *page_warnings,
            *empty,
            *head_warnings,
            *footnote_warnings,
            *uncertain_warnings(selection.included, blocks),
        ]
    )
    document = build_document(blocks, meta, options, seams)
    stats = compute_stats(selection, blocks, seams)
    stats["running_heads"] = heads
    return Result(
        document, [w.as_dict() for w in warnings], stats, seams, [page.id for page in selection.included]
    )
