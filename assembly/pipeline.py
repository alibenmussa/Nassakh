"""The assembly pipeline (PHASE4_SPEC §2): a book's page lines → one ProseMirror manuscript document.

Pure functions over plain dataclasses; no ORM, no I/O. `assembly.services.load_book` builds the
inputs (`PageIn` / `LineIn`, boxes as ratios of the page) and `assemble` runs the steps in order:

1. `select_pages`        which pages are in, which are skipped (and break the join chain)   §2.1
2. `split_paragraphs`    body lines of one page → paragraphs and headings (geometry first)   §2.3
3. `join_pages`          seams: the last paragraph of a page joined with the next page's first §2.4
4. `page_notes`, `link_footnotes`, `attach_orphans`, `number_footnotes`                     §2.5
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

import re
import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from core.arabic import to_western_digits

# ====================================================================== constants

BODY = "body"
FOOTNOTE = "footnote"
ROLE_BODY = "body"
HEADING_LEVELS: dict[str, int] = {"heading": 1, "subheading": 2}

REVIEWED_STATUSES: frozenset[str] = frozenset({"reviewed", "assembled"})
UNREVIEWED_STATUS = "ocr_done"
PENDING_STATUSES: frozenset[str] = frozenset({"uploaded", "preprocessed", "layout_done"})
ERROR_STATUS = "error"
EXCLUDED_STATUS = "excluded"

NUMBERING_MODES: tuple[str, ...] = ("chapter", "book", "page")
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
    "marker_unmatched",
    "note_orphan",
    "uncertain_words",
)
STAGES: tuple[str, ...] = ("collect", "paragraphs", "seams", "footnotes", "headings", "typography", "save")


# ====================================================================== inputs and settings


@dataclass
class LineIn:
    """One OCR'd line of a page, as the loader reads it.

    `kind` is `body` or `footnote` (running headers and page numbers never reach the pipeline);
    `role` the reviewer's line role (D32); `box` `[x0, y0, x1, y1]` as ratios of the page (None when
    the line has no box, e.g. a line inserted by a reviewer); `uncertain` the indexes, in
    `text.split()`, of the words still unresolved (`conf == "low"` and no `res`).
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

    footnote_numbering: str = "chapter"
    include_unreviewed: bool = True
    strip_tatweel: bool = True
    seams: dict[str, str] = field(default_factory=dict)
    dismissed_suggestions: frozenset[str] = frozenset()

    def as_dict(self) -> dict:
        """JSON form, as stored in `AssemblyRun.settings`."""
        return {
            "footnote_numbering": self.footnote_numbering,
            "include_unreviewed": self.include_unreviewed,
            "strip_tatweel": self.strip_tatweel,
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
        footnote_numbering=numbering if numbering in NUMBERING_MODES else "chapter",
        include_unreviewed=_as_bool(data.get("include_unreviewed"), True),
        strip_tatweel=_as_bool(data.get("strip_tatweel"), True),
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


def quote_roles(text: str) -> dict[int, str]:
    """`{index: "open" | "close"}` for the quote characters of `text`.

    `«` and `“` open; `»` and `”` close an open quote. Books and OCR also print `»` / `”` for the
    opening quote («قاع » حمادة مرزق »»); a closing glyph with no quote open is read from its
    context: followed by punctuation, a note reference or the end → closing; glued to the word
    before it → closing; otherwise it opens a quote.
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
            if j >= size or text[j] in CLOSING_CONTEXT or (not space_before and space_after):
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


def normalize_rich(rich: Rich, strip_tatweel_marks: bool = True, digit_style: str = "western") -> Rich:
    """All of D37 on one text: glued marks, spaces around marks and quotes, tatweel, spaces, digits.

    Tatweel goes first so a stretched word is one word for the punctuation rules; digits go last.
    """
    rich = collapse_spaces(rich)
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


def word_count(text: str) -> int:
    """Words of `text` (whitespace separated, inline placeholders left out)."""
    return len(text.translate(_NO_PLACEHOLDERS).split())


# ====================================================================== warnings


@dataclass
class AssemblyWarning:
    """One assembly warning (§2.9); `page` is the page number (None for book-level warnings)."""

    code: str
    severity: str
    page: int | None
    message: str
    block_id: str | None = None
    line_ids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "page": self.page,
            "blockId": self.block_id,
            "lineIds": list(self.line_ids),
            "message": self.message,
        }


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

    Roles differ → break; lines of one heading role never break (they form one heading). With both
    boxes: `a` short, `b` indented, or either centred. Without a box on either line: `a` ends with
    terminal punctuation.

    With the page's `measure`, `b` only counts as indented when it also starts left of `a`
    (`x1_a − x1_b > 0.02 · M`): consecutive lines with the same indent are one indented block (an
    inset list item, a quotation set narrower, a page whose right edge drifts), not a paragraph each.
    """
    if a.role != b.role:
        return True
    if a.role in HEADING_LEVELS:
        return False
    if shape_a is None or shape_b is None:
        return ends_terminal(a.text)
    indented = shape_b.indented
    if measure is not None and a.box is not None and b.box is not None:
        indented = indented and a.box[2] - b.box[2] > INDENT_SHARE * measure.width
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
    )


def split_paragraphs(page: PageIn) -> list[Block]:
    """The body lines of one page as paragraphs and headings, in reading order (§2.3).

    Consecutive `heading` lines form one level-1 heading, consecutive `subheading` lines one level-2
    heading. Lines without text are left out.
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
    automatic rule that applied: `geometry`, `punctuation`, or `heading` (a side is not a paragraph:
    a heading, or a page without body text).
    """
    record = {"page": page, "from_page": from_page, "mode": "split", "decision": "auto", "reason": "heading"}
    if prev is None or nxt is None or prev.kind != "paragraph" or nxt.kind != "paragraph":
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
STRONG_STYLES: frozenset[str] = frozenset({"bracket", "superscript", "glued"})


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


def split_note_marker(text: str) -> tuple[str | None, int]:
    """`(marker, length)` of the marker that starts a footnote line (`(None, 0)` when there is none).

    Marker: `(n)`, `[n]`, `n`, `n-`, `n.` … with 1–3 Western or Arabic-Indic digits or `*`, `**`,
    `***` (spec regex), plus a lone alef read for `١`.
    """
    match = NOTE_MARKER.match(text or "")
    if not match:
        return None, 0
    return match.group(1), match.end()


def page_notes(page: PageIn, carry: Note | None) -> tuple[list[Note], Note | None]:
    """The notes of one page, and the note the next page may continue (§2.5).

    A footnote line that starts with a marker starts a note; a line without one continues the
    current note. The page's first footnote line without a marker continues `carry` (the previous
    page's last note) when there is one, else it starts a note without marker.
    """
    notes: list[Note] = []
    current: Note | None = None
    lines = [line for line in page.lines if line.kind == FOOTNOTE and (line.text or "").strip()]
    for line in lines:
        rich = Rich.from_line(line)
        marker, length = split_note_marker(rich.text)
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
        elif current is None and carry is not None:
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
    if not lines:
        return notes, None
    return notes, current


@dataclass(frozen=True)
class Candidate:
    """A possible footnote reference in a block's text."""

    block: int
    start: int
    end: int
    cut: int  # where the replaced span starts (the whitespace before the marker goes with it)
    key: str
    marker: str
    style: str  # bracket | superscript | glued | standalone | alef
    page: int
    line: int | None


def find_candidates(block_index: int, rich: Rich, line_page: dict[int, int]) -> list[Candidate]:
    """Footnote reference candidates of one block, in reading order (§2.5).

    `(n)` `[n]` `(*)`; superscript digits; a digit run glued to the end of a word (`الفيل٢`, also
    after a closing quote `»١` or a period `الخ .1`); a standalone token of 1–2 digits
    (`الفيل ٢ مرحلة`); a lone alef (`» ا .`, the OCR's `١`).
    Overlapping matches keep the first style in that order.
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
    for match in _RE_SUPERSCRIPT.finditer(text):
        add(match.start(), match.end(), match.group(), "superscript")
    for match in _RE_GLUED.finditer(text):
        add(match.start(1), match.end(1), match.group(1), "glued")
    for match in _RE_STANDALONE.finditer(text):
        add(match.start(1), match.end(1), match.group(1), "standalone")
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
        out.append(Candidate(block_index, start, end, cut, marker_key(marker), marker, style, page, line))
    return out


def link_footnotes(
    blocks: list[Block], notes_by_page: dict[int, list[Note]], line_page: dict[int, int]
) -> tuple[list[Note], list[AssemblyWarning]]:
    """Replace the markers of linked notes by footnote nodes; returns the orphan notes and warnings.

    A candidate is linked only when a note of the same page carries that number (or `*`); each note
    takes the first unused matching candidate of the page, bracketed / superscript / glued ones
    before standalone digits and alefs. A bracketed, superscript or glued candidate left over on a
    page that has notes → `marker_unmatched` (the text stays as printed).
    """
    by_page: dict[int, list[Candidate]] = {}
    for index, block in enumerate(blocks):
        for cand in find_candidates(index, block.rich, line_page):
            by_page.setdefault(cand.page, []).append(cand)
    chosen: dict[int, list[tuple[Candidate, Note]]] = {}
    orphans: list[Note] = []
    warnings: list[AssemblyWarning] = []
    for page_number in sorted(notes_by_page):
        notes = notes_by_page[page_number]
        cands = by_page.get(page_number, [])
        used: set[int] = set()
        for note in notes:
            pick = None
            if note.key is not None:
                for strong in (True, False):
                    pick = next(
                        (
                            i
                            for i, c in enumerate(cands)
                            if i not in used and c.key == note.key and (c.style in STRONG_STYLES) is strong
                        ),
                        None,
                    )
                    if pick is not None:
                        break
            if pick is None:
                orphans.append(note)
                continue
            used.add(pick)
            cand = cands[pick]
            note.ref = cand.marker
            chosen.setdefault(cand.block, []).append((cand, note))
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
    """Mark heading suggestions (never applied): a paragraph of 1–2 centred lines, not ending with
    terminal punctuation, at most 8 words, followed by a paragraph, and not dismissed."""
    for index, block in enumerate(blocks):
        block.suggested = None
        if block.kind != "paragraph" or not 1 <= len(block.lines) <= MAX_SUGGESTION_LINES:
            continue
        if block.id in dismissed or not all(shape is not None and shape.centred for shape in block.shapes):
            continue
        text = block.rich.plain()
        words = word_count(text)
        if not words or words > MAX_SUGGESTION_WORDS or ends_terminal(text):
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
    """The ProseMirror node of a paragraph or heading, with its source mapping."""
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
    for page in selection.included:
        if page.number in selection.gaps:
            carry = None
        notes, carry = page_notes(page, carry)
        if notes:
            notes_by_page[page.number] = notes
    orphans, footnote_warnings = link_footnotes(blocks, notes_by_page, line_page)
    footnote_warnings += attach_orphans(blocks, orphans, line_page)

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
            *footnote_warnings,
            *uncertain_warnings(selection.included, blocks),
        ]
    )
    document = build_document(blocks, meta, options, seams)
    stats = compute_stats(selection, blocks, seams)
    return Result(
        document, [w.as_dict() for w in warnings], stats, seams, [page.id for page in selection.included]
    )
