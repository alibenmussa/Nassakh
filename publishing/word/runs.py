"""Runs, footnotes and comments (PHASE6_SPEC §5.6, §5.8, §5.11).

`RunWriter` turns a block's inlines into `w:r` elements: the text is cut by direction, decided once
over the paragraph's whole text (`model.direction_flags`: a footnote call counts as right to left, a line
break starts a new stretch), so a mark, a note call or an uncertain word's boundary never changes it;
right-to-left pieces get `w:rtl` so Word uses the complex-script font and size, and `w:rtl` is left off
wherever Word would read it as an override that reorders the text (ECMA-376 §17.3.2.30). The
right-to-left pieces are then cut by face (`FacePlan.split`: what the role's Arabic face lacks goes to
`rStyle NkLatin`, as the preview's `unicode-range` sends it to the Latin face). Bold and italic are
written both ways (`b`+`bCs`, `i`+`iCs`). A `LineBreak` is `w:br`, a `NoteRef` three `FootnoteReference`
runs «(», the reference, «)», and a `SourceMark` nothing. Tabs and newlines in text become `w:tab` and
`w:br`; what XML forbids is dropped.

With `editorial=True` the model keeps the `uncertain` mark; the writer rebuilds the words exactly as
`editor.uncertain._spans` counts them (consecutive uncertain runs joined, cut at white space and at any
non-`Run` inline), pairs the k-th word of a container with the k-th `uncertain.Word` of that container,
and anchors a comment by «نسّاخ» on each word whose text still matches.
"""

from __future__ import annotations

import re
from collections import Counter, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from lxml import etree

from publishing.model import (
    Footnote,
    Inline,
    LineBreak,
    NoteRef,
    Run,
    SourceMark,
    bidi_classes,
    direction_flags,
    direction_pieces,
)

from .faces import FacePlan
from .ooxml import root, run, text, twips_mm, w, xml_safe
from .options import COMMENTS_IN_NOTES

UNCERTAIN = "uncertain"
COMMENT_AUTHOR = "نسّاخ"
COMMENT_INITIALS = "ن"
COMMENT_WORD = "كلمة غير مؤكَّدة: {word}"
COMMENT_IN_NOTE = "في الحاشية: "
COMMENT_READINGS = "القراءات:"
COMMENT_CURRENT = " (في النص)"
COMMENT_PAGE = "الصفحة الأصلية: {page}"
COMMENT_NO_READINGS = "لا قراءات أخرى لهذه الكلمة؛ راجعها على الأصل."
NBSP = " "
_WS = re.compile(r"(\s+)")


@dataclass
class WriteStats:
    """What the run writer counted."""

    paragraphs: int = 0
    footnotes: int = 0
    comments: int = 0
    comments_skipped: int = 0
    words_seen: int = 0


def rpr(*, rtl: bool = False, marks: tuple[str, ...] = (), rstyle: str | None = None) -> etree._Element:
    """Run properties: the style, bold and italic both ways, the direction."""
    bold = "bold" in marks
    italic = "italic" in marks
    return w(
        "rPr",
        w("rStyle", val=rstyle) if rstyle else None,
        w("b") if bold else None,
        w("bCs") if bold else None,
        w("i") if italic else None,
        w("iCs") if italic else None,
        w("rtl") if rtl else None,
    )


def text_content(value: str) -> list[etree._Element]:
    """`w:t`, `w:tab` and `w:br` elements for a piece of text (tabs and newlines become elements)."""
    out: list[etree._Element] = []
    for piece in re.split(r"([\t\n])", xml_safe(value.replace("\r\n", "\n").replace("\r", "\n"))):
        if piece == "\t":
            out.append(w("tab"))
        elif piece == "\n":
            out.append(w("br"))
        elif piece:
            out.append(text(piece))
    return out


class RunWriter:
    """Inline elements for the body, the notes, the front matter and the comments."""

    def __init__(
        self,
        faces: FacePlan,
        *,
        notes: FootnotesPart | None = None,
        comments: CommentsPart | None = None,
        readings: Mapping[tuple[str, str | None], list] | None = None,
        duplicates: frozenset[tuple[str, str | None]] = frozenset(),
        stats: WriteStats | None = None,
    ):
        self.faces = faces
        self.notes = notes
        self.comments = comments
        self.readings = readings or {}
        self.duplicates = duplicates
        self.stats = stats or WriteStats()
        self._cursor: dict[tuple[str, str | None], int] = {}
        self._pending: list[tuple[str, int]] = []  # comments of a note's words anchored on its call

    # ------------------------------------------------------------------ text

    def text_runs(
        self,
        value: str,
        marks: tuple[str, ...] = (),
        role: str = "body",
        *,
        base: str = "rtl",
        flags: str | None = None,
    ) -> list[etree._Element]:
        """The runs of one piece of text: direction pieces, then the face split of the right-to-left ones.
        `flags` are the piece's direction flags when the paragraph decided them (`inline`); a text on its
        own (a title, a header, a comment line) is decided over itself."""
        out: list[etree._Element] = []
        if not value:
            return out
        marks = tuple(m for m in marks if m in ("bold", "italic"))
        if flags is None:
            flags = direction_flags(bidi_classes(value), base)
        for piece, is_rtl in direction_pieces(value, flags):
            if not is_rtl:
                out.append(run(*text_content(piece), rpr=rpr(marks=marks)))
                continue
            for segment, covered in self.faces.split(piece, role):
                out.append(
                    run(
                        *text_content(segment),
                        rpr=rpr(rtl=True, marks=marks, rstyle=None if covered else "NkLatin"),
                    )
                )
        return out

    def line_break(self) -> etree._Element:
        return run(w("br"))

    # ------------------------------------------------------------------ calls

    def call_runs(self, number: int) -> list[etree._Element]:
        """«(n)» as three `FootnoteReference` runs around `w:footnoteReference` (§5.6)."""
        return [
            run(text("("), rpr=rpr(rtl=True, rstyle="FootnoteReference")),
            run(w("footnoteReference", id=number), rpr=rpr(rtl=True, rstyle="FootnoteReference")),
            run(text(")"), rpr=rpr(rtl=True, rstyle="FootnoteReference")),
        ]

    def marker_runs(self) -> list[etree._Element]:
        """The note's marker «(n) » on the baseline (`NkNoteNumber` around `w:footnoteRef`)."""
        return [
            run(text("("), rpr=rpr(rtl=True, rstyle="NkNoteNumber")),
            run(w("footnoteRef"), rpr=rpr(rtl=True, rstyle="NkNoteNumber")),
            run(text(")" + NBSP), rpr=rpr(rtl=True, rstyle="NkNoteNumber")),
        ]

    # ------------------------------------------------------------------ inlines

    def inline(
        self,
        runs: list[Inline],
        role: str = "body",
        *,
        block_id: str = "",
        note_id: str | None = None,
        footnotes: list[Footnote] | None = None,
        base: str = "rtl",
    ) -> list[etree._Element]:
        """The elements of a block's (or a note's) inlines, with the comment anchors of its uncertain
        words. `footnotes` are the block's notes, taken by id through a queue (a repeated id never swaps
        two notes). `base` is the paragraph's direction (D99: `ltr` for a left-to-right paragraph)."""
        queues: dict[str, deque[Footnote]] = {}
        for note in footnotes or []:
            queues.setdefault(note.id, deque()).append(note)
        pieces, words = _pieces(runs)
        directions = self._directions(pieces, footnotes, base)
        matched = self._match_words([text for _indexes, text in words], block_id, note_id)
        starts = {
            indexes[0]: index for index, (indexes, _t) in enumerate(words) if matched.get(index) is not None
        }
        ends = {
            indexes[-1]: index for index, (indexes, _t) in enumerate(words) if matched.get(index) is not None
        }
        out: list[etree._Element] = []
        for index, (kind, item) in enumerate(pieces):
            if index in starts:
                out.append(w("commentRangeStart", id=matched[starts[index]]))
            if kind == "text":
                out.extend(self.text_runs(item.text, item.marks, role, flags=directions.get(index)))
            elif isinstance(item, LineBreak):
                out.append(self.line_break())
            elif isinstance(item, NoteRef):
                out.extend(self._note_ref(item, queues, block_id))
            elif isinstance(item, SourceMark):
                pass
            if index in ends:
                comment_id = matched[ends[index]]
                out.append(w("commentRangeEnd", id=comment_id))
                out.append(self.reference_run(comment_id))
        return out

    def _directions(
        self, pieces: list[tuple[str, object]], footnotes: list[Footnote] | None, base: str = "rtl"
    ) -> dict[int, str]:
        """The direction flags of each text piece (by index), decided over the paragraph's whole text: a
        call that will be written counts as one right-to-left character (its «(» and «)» are `w:rtl`
        runs), a line break ends a stretch, a scan page mark prints nothing (§5.6)."""
        available = Counter(note.id for note in footnotes or [])
        codes: list[str] = []
        spans: dict[int, tuple[int, int]] = {}
        position = 0
        for index, (kind, item) in enumerate(pieces):
            if kind == "text":
                classes = bidi_classes(item.text)
                spans[index] = (position, position + len(classes))
                codes.append(classes)
                position += len(classes)
            elif isinstance(item, NoteRef):
                if self.notes is not None and available[item.note] > 0:
                    available[item.note] -= 1
                    codes.append("R")
                    position += 1
            elif isinstance(item, LineBreak):
                codes.append("|")
                position += 1
        flags = direction_flags("".join(codes), base)
        return {index: flags[start:end] for index, (start, end) in spans.items()}

    def reference_run(self, comment_id: int) -> etree._Element:
        return run(w("commentReference", id=comment_id), rpr=rpr(rstyle="CommentReference"))

    def _note_ref(
        self, ref: NoteRef, queues: dict[str, deque[Footnote]], block_id: str
    ) -> list[etree._Element]:
        queue = queues.get(ref.note)
        if not queue or self.notes is None:
            return []
        note = queue.popleft()
        content = self.marker_runs() + self.inline(
            note.runs, "body", block_id=block_id, note_id=note.id, footnotes=None
        )
        pending, self._pending = self._pending, []
        paragraph = w("p", w("pPr", w("pStyle", val="FootnoteText")), *content)
        number = self.notes.add(paragraph)
        self.stats.footnotes += 1
        calls = self.call_runs(number)
        for _word, comment_id in pending:  # the fallback anchor: the call in the body (§5.11)
            calls = [w("commentRangeStart", id=comment_id), *calls, w("commentRangeEnd", id=comment_id)]
            calls.append(self.reference_run(comment_id))
        return calls

    def _match_words(self, words: list[str], block_id: str, note_id: str | None) -> dict[int, int | None]:
        """Word index → comment id (None: skipped) for the uncertain words of a container."""
        out: dict[int, int | None] = {}
        if not words or self.comments is None:
            return out
        key = (block_id, note_id)
        self.stats.words_seen += len(words)
        if key in self.duplicates or key not in self.readings:
            self.stats.comments_skipped += len(words)
            return {index: None for index in range(len(words))}
        candidates = self.readings[key]
        cursor = self._cursor.get(key, 0)
        for index, word_text in enumerate(words):
            position = cursor + index
            found = candidates[position] if position < len(candidates) else None
            if found is None or found.word != word_text:
                self.stats.comments_skipped += 1
                out[index] = None
                continue
            in_note = note_id is not None
            comment_id = self.comments.add(word_text, found, in_note=in_note and not COMMENTS_IN_NOTES)
            self.stats.comments += 1
            if in_note and not COMMENTS_IN_NOTES:
                self._pending.append((word_text, comment_id))
                out[index] = None
            else:
                out[index] = comment_id
        self._cursor[key] = cursor + len(words)
        return out


def _pieces(runs: list[Inline]) -> tuple[list[tuple[str, object]], list[tuple[list[int], str]]]:
    """The inlines cut so that every uncertain word is whole pieces: `(pieces, words)` where each word is
    `(the indexes of its pieces, its text)`. Uncertain runs are split at white space; other inlines close
    a word, as `editor.uncertain._spans` does."""
    pieces: list[tuple[str, object]] = []
    words: list[tuple[list[int], str]] = []
    current: list[int] | None = None

    def close() -> None:
        nonlocal current
        if current:
            words.append((current, "".join(pieces[i][1].text for i in current)))
        current = None

    for item in runs:
        if isinstance(item, Run) and UNCERTAIN in item.marks:
            for chunk in _WS.split(item.text):
                if not chunk:
                    continue
                pieces.append(("text", Run(chunk, item.marks)))
                if chunk.isspace():
                    close()
                else:
                    if current is None:
                        current = []
                    current.append(len(pieces) - 1)
            continue
        close()
        pieces.append(("text" if isinstance(item, Run) else "inline", item))
    close()
    return pieces, words


# ====================================================================== the footnotes part


class FootnotesPart:
    """`word/footnotes.xml`: the separators `-1` and `0` drawn as the preview's rule, then one
    `w:footnote` per note in call order (ids 1, 2, …)."""

    def __init__(self) -> None:
        self.root = root("footnotes")
        for note_id, kind in ((-1, "separator"), (0, "continuationSeparator")):
            self.root.append(w("footnote", self.separator_paragraph(), type=kind, id=note_id))
        self.count = 0

    @staticmethod
    def separator_paragraph() -> etree._Element:
        """An empty paragraph whose top border is the rule: 0.4 pt (sz 3 eighths), 4 mm above, a 1.6 mm
        exact line below, the full text width; a 1 pt run keeps the line at that height."""
        small = w("rPr", w("sz", val=2), w("szCs", val=2))
        return w(
            "p",
            w(
                "pPr",
                w("pBdr", w("top", val="single", sz=3, space=0, color="auto")),
                w("spacing", before=twips_mm(4), after=0, line=twips_mm(1.6), lineRule="exact"),
                w("rPr", w("sz", val=2), w("szCs", val=2)),
            ),
            run(text(" "), rpr=small),
        )

    def add(self, *paragraphs: etree._Element) -> int:
        """Add a note (its paragraphs) and return its id."""
        self.count += 1
        self.root.append(w("footnote", *paragraphs, id=self.count))
        return self.count


# ====================================================================== the comments part


@dataclass
class CommentsPart:
    """`word/comments.xml`: one comment by «نسّاخ» per uncertain word (§5.11)."""

    text_runs: Callable[..., list[etree._Element]]
    date: str  # ISO UTC
    root: etree._Element = field(default_factory=lambda: root("comments"))
    count: int = 0

    def paragraph(self, value: str, *, first: bool = False) -> etree._Element:
        content: list[etree._Element] = []
        if first:
            content.append(run(w("annotationRef"), rpr=rpr(rstyle="CommentReference")))
        content.extend(self.text_runs(value, (), "body"))
        return w("p", w("pPr", w("pStyle", val="CommentText")), *content)

    def add(self, word: str, reading, *, in_note: bool = False) -> int:
        """Add the comment of `word` with its `uncertain.Word` readings; returns the comment id."""
        comment_id = self.count
        self.count += 1
        prefix = COMMENT_IN_NOTE if in_note else ""
        paragraphs = [self.paragraph(prefix + COMMENT_WORD.format(word=word), first=True)]
        readings = list(getattr(reading, "readings", None) or [])
        if readings:
            paragraphs.append(self.paragraph(COMMENT_READINGS))
            for item in readings:
                line = f"{item.get('label', '')}: {item.get('text', '')}"
                if item.get("current"):
                    line += COMMENT_CURRENT
                paragraphs.append(self.paragraph(line))
        page = getattr(reading, "source_page", None)
        if isinstance(page, int) and not isinstance(page, bool):
            paragraphs.append(self.paragraph(COMMENT_PAGE.format(page=page)))
        if not readings:
            paragraphs.append(self.paragraph(COMMENT_NO_READINGS))
        self.root.append(
            w(
                "comment",
                *paragraphs,
                id=comment_id,
                author=COMMENT_AUTHOR,
                date=self.date,
                initials=COMMENT_INITIALS,
            )
        )
        return comment_id
