"""Uncertain words in the manuscript (PHASE5_SPEC §9.1, D47): the book page's «غير المؤكَّدة» tab.

An uncertain word is a run of characters carrying the `uncertain` mark (assembly marks the words that
were still unresolved in review, PHASE4_SPEC §2.7). `uncertain_words(book)` lists every one left in the
manuscript, in book order:

    {chapter, block, note, start, end, word, page, source_page, context: {before, after},
     readings: [{engine, label, text, current}]}

- `block` is the paragraph (or heading) id, `note` the footnote id when the word is in a note (the note's
  lines in the page layout carry the note id as their `block`); `start` / `end` are offsets into that
  container's plain text (`editor.document.inline_text`) in UTF-16 units, the page layout's unit.
- `page` is the printed page of the word in the book's live layout (null before one), `source_page` the
  scan page it was read from.
- `readings` come from the OCR tokens of the container's source lines (`sourceLineIds`): the unresolved
  word's primary reading (`orig`, else `t`), the second model's (`alt`) and Tesseract's (`tess`),
  distinct ones only, labelled like the review screen (a number Kraken read: «Kraken», and Qari's own
  letter as `alt` where it wrote one, D50–D51). The k-th uncertain word of a container is matched
  with the next low-confidence token of its lines that reads the same (diacritics aside); a word the
  editor changed since has no readings (it can still be typed or accepted).

`resolve(book, action, data, user)` edits the manuscript — `accept` (the word stays, the mark goes),
`choose` (a reading replaces it) or `type` (the typed text replaces it) — after the chapter's version
check and a check that the word is still there (409 otherwise), and asks for the chapter's re-layout.
The OCR lines are never changed: after the first edit the manuscript is the source of truth (D41).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from django.db import transaction

from books.models import Book
from core.arabic import strip_tashkeel

from . import document as doc
from .models import Manuscript
from .services import (
    NO_CHAPTER,
    ChapterConflict,
    EditorError,
    EditorNotFound,
    _check_chapter_id,
    _schedule,
    _write,
    manuscript_of,
)

MARK = "uncertain"
CONTEXT = 40
MAX_TYPED = 200
ACTIONS: tuple[str, ...] = ("accept", "choose", "type")
GONE = "لم تعد هذه الكلمة غير مؤكَّدة في النص؛ أعد تحميل القائمة."


@dataclass
class Word:
    """One uncertain word of a container (code point offsets in its `inline_text`)."""

    chapter: str
    block: str
    note: str | None
    start: int
    end: int
    word: str
    text: str  # the container's plain text
    lines: list[int] = field(default_factory=list)  # the container's source line ids
    source_page: int | None = None
    readings: list[dict] = field(default_factory=list)
    number: bool = False  # a number Kraken read (D50; set by `attach_readings`): the readiness counts them


def _spans(content: list) -> list[tuple[int, int, str]]:
    """`(start, end, word)` of the marked runs of a container (split at white space and inline nodes)."""
    out: list[tuple[int, int, str]] = []
    position = 0
    start = None
    chars: list[str] = []

    def close() -> None:
        nonlocal start, chars
        if start is not None and chars:
            out.append((start, start + len(chars), "".join(chars)))
        start, chars = None, []

    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "text":
            close()
            position += 1
            continue
        marked = any(isinstance(m, dict) and m.get("type") == MARK for m in item.get("marks") or [])
        for char in str(item.get("text") or ""):
            if marked and not char.isspace():
                if start is None:
                    start = position
                chars.append(char)
            else:
                close()
            position += 1
    close()
    return out


def _line_ids(node: dict | None) -> list[int]:
    return [
        value
        for value in doc.attrs_of(node).get("sourceLineIds") or []
        if isinstance(value, int) and not isinstance(value, bool)
    ]


def words_of(document: dict, chapter_id: str | None = None) -> list[Word]:
    """Every uncertain word of the document (or of one chapter), in order."""
    out: list[Word] = []
    for chapter in doc.chapters_of(document):
        if chapter_id is not None and chapter.id != chapter_id:
            continue
        for block, note, content in doc.iter_containers(chapter.nodes(document)):
            spans = _spans(content)
            if not spans:
                continue
            owner = note if note is not None else block
            text = doc.inline_text(content)
            pages = doc.source_pages(block)
            page = doc.attrs_of(note).get("sourcePage") if note is not None else (pages[0] if pages else None)
            for start, end, word in spans:
                out.append(
                    Word(
                        chapter=chapter.id,
                        block=doc.node_id(block),
                        note=doc.node_id(note) if note is not None else None,
                        start=start,
                        end=end,
                        word=word,
                        text=text,
                        lines=_line_ids(owner),
                        source_page=page if isinstance(page, int) and not isinstance(page, bool) else None,
                    )
                )
    return out


def _fold(text: str) -> str:
    return strip_tashkeel(str(text or "")).strip()


def _labels() -> dict[str, str]:
    from ocr.services import ENGINE_LABELS, engine_names

    primary, secondary, _fast = engine_names()
    return {
        "primary": ENGINE_LABELS.get(primary, primary),
        "secondary": ENGINE_LABELS.get(secondary, secondary),
        "tess": "Tesseract",
    }


def _readings(token: dict, word: str, labels: dict[str, str], normalize=None) -> list[dict]:
    """The distinct readings of a token, each as the manuscript would hold it (`normalize`: the book's
    assembly rules — digits in the book's style, tatweel — so a chosen reading never brings back what
    assembly took out, D6)."""
    normalize = normalize or (lambda text: text)
    rows: list[tuple[str, str]] = []
    for engine, value in (
        ("primary", token.get("orig") or token.get("t")),
        ("secondary", token.get("alt")),
        ("tess", token.get("tess")),
    ):
        text = normalize(str(value or "")) if value else ""
        if text and text not in (seen for _engine, seen in rows):
            rows.append((engine, text))
    # a number Kraken read (D50): its reading is the only one, but for Qari's letter where Qari wrote a
    # letter for it (D51), kept as the second reading under Qari's label
    kraken = token.get("src") == "kraken"
    kraken_labels = {**labels, "primary": "Kraken", "secondary": labels["primary"]}
    return [
        {
            "engine": engine,
            "label": (kraken_labels if kraken else labels)[engine],
            "text": text,
            "current": text == word,
        }
        for engine, text in rows
    ]


def normalizer(book: Book):
    """The book's assembly normalisation of one word (`assembly.pipeline.normalize_text`: digits in the
    book's `digit_style`, tatweel as the assembly settings say)."""
    from assembly.pipeline import DIGIT_STYLES, normalize_settings, normalize_text

    settings = normalize_settings(book.assembly_settings)
    style = book.digit_style if book.digit_style in DIGIT_STYLES else "western"
    return lambda text: normalize_text(text, settings.strip_tatweel, style)


def attach_readings(words: list[Word], normalize=None) -> None:
    """Resolve each word's readings from its container's source lines (one query for all of them);
    `normalize` (`normalizer(book)`) puts each reading in the manuscript's form."""
    from ocr.models import Line

    ids = {line_id for word in words for line_id in word.lines}
    if not ids:
        return
    lines = {
        line.pk: line
        for line in Line.objects.filter(pk__in=ids)
        .select_related("page")
        .only("id", "order", "tokens", "page__number")
    }
    labels = _labels()
    cursors: dict[tuple, int] = {}
    tokens_of: dict[tuple, list[tuple[dict, int]]] = {}
    for word in words:
        key = (word.chapter, word.block, word.note)
        if key not in tokens_of:
            found = sorted(
                (lines[i] for i in word.lines if i in lines), key=lambda line: (line.page.number, line.order)
            )
            tokens_of[key] = [
                (token, line.page.number)
                for line in found
                for token in line.tokens or []
                if isinstance(token, dict) and token.get("conf") == "low"
            ]
        tokens = tokens_of[key]
        cursor = cursors.get(key, 0)
        folded = _fold(word.word)
        for index in range(cursor, len(tokens)):
            token, page = tokens[index]
            texts = {str(token.get(k) or "") for k in ("t", "orig", "alt", "tess")} - {""}
            if normalize is not None:
                texts |= {normalize(t) for t in texts}  # (a digit read as ٣ is 3 in the manuscript)
            if word.word in texts or folded in {_fold(t) for t in texts}:
                word.readings = _readings(token, word.word, labels, normalize)
                word.source_page = page
                word.number = token.get("src") == "kraken"
                cursors[key] = index + 1
                break


def _page_index(book: Book) -> dict[str, list[tuple[int, int, int]]]:
    """Container id → `[(start, end, page)]` of its lines in the live layout."""
    from publishing.relayout import live_pages

    out: dict[str, list[tuple[int, int, int]]] = {}
    for page in live_pages(book.pk):
        for line in page.get("lines") or []:
            if line.get("block"):
                out.setdefault(line["block"], []).append(
                    (line.get("start", 0), line.get("end", 0), page["n"])
                )
    return out


def _page_of(index: dict, word: Word, start: int) -> int | None:
    lines = index.get(word.note or word.block) or []
    for low, high, page in lines:
        if low <= start < high:
            return page
    return None


def _context(text: str, start: int, end: int) -> dict:
    def clean(value: str) -> str:
        return " ".join(value.replace(doc.OBJECT, "").replace(doc.BREAK, " ").split())

    before = clean(text[max(0, start - CONTEXT - 20) : start])
    after = clean(text[end : end + CONTEXT + 20])
    if len(before) > CONTEXT:
        cut = before[-CONTEXT:]
        before = cut[cut.find(" ") + 1 :] if " " in cut else cut
    if len(after) > CONTEXT:
        cut = after[:CONTEXT]
        after = cut[: cut.rfind(" ")] if " " in cut else cut
    return {"before": before, "after": after}


def item(word: Word, pages: dict) -> dict:
    """The API form of a word (UTF-16 offsets)."""
    start = doc.utf16_index(word.text, word.start)
    end = doc.utf16_index(word.text, word.end)
    return {
        "chapter": word.chapter,
        "block": word.block,
        "note": word.note,
        "start": start,
        "end": end,
        "word": word.word,
        "page": _page_of(pages, word, start),
        "source_page": word.source_page,
        "context": _context(word.text, word.start, word.end),
        "readings": word.readings,
    }


def uncertain_words(book: Book) -> dict:
    """`api:uncertain`: `{count, manuscript_version, items}` (see the module docstring). A fixed number of
    queries whatever the book's size."""
    manuscript = manuscript_of(book)
    words = words_of(manuscript.document or {})
    attach_readings(words, normalizer(book))
    pages = _page_index(book)
    return {
        "count": len(words),
        "manuscript_version": manuscript.version,
        "items": [item(word, pages) for word in words],
    }


def count(document) -> int:
    """How many uncertain words the document has left (the tab's badge)."""
    return len(words_of(document or {}))


def counts(book: Book, document) -> dict[str, int]:
    """The uncertain words left, by kind (the export readiness, PHASE6_SPEC §7): `{words, numbers}` —
    every uncertain word, and those that are numbers Kraken read (`Word.number`). One query (the
    readings' OCR lines), none without uncertain words."""
    words = words_of(document or {})
    if words:
        attach_readings(words, normalizer(book))
    return {"words": len(words), "numbers": sum(1 for word in words if word.number)}


# ====================================================================== resolving


def _container(nodes: list, block_id: str, note_id: str | None) -> dict | None:
    """The node whose content holds the word: the block, or its footnote `note_id`."""
    for block in doc.flat_blocks(nodes):
        if doc.node_id(block) != block_id:
            continue
        if not note_id:
            return block
        for item_ in block.get("content") or []:
            if isinstance(item_, dict) and item_.get("type") == "footnote" and doc.node_id(item_) == note_id:
                return item_
    return None


def _offset(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise EditorError("موضع الكلمة غير صالح.")
    try:
        number = int(value)
    except ValueError:
        raise EditorError("موضع الكلمة غير صالح.") from None
    if number < 0:
        raise EditorError("موضع الكلمة غير صالح.")
    return number


def resolve(book: Book, action: str, data: dict, user=None) -> dict:
    """Accept, choose a reading for, or type over one uncertain word (see the module docstring).

    `data`: `{chapter, block, note?, start, end, word, version, engine? (choose), text? (type)}` (UTF-16
    offsets, `version` the chapter's). Returns `{chapter, version, manuscript_version, word, start, end,
    remaining, relayout}` — the chapter's new version, the word now in the text and its range, how many
    uncertain words the book has left and the re-layout to poll. Raises `ChapterConflict` (409) when the
    chapter changed or the word is no longer there, `EditorError` for bad input."""
    if action not in ACTIONS:
        raise EditorError("إجراء غير معروف.")
    data = data if isinstance(data, dict) else {}
    chapter_id = _check_chapter_id(str(data.get("chapter") or ""))
    block_id = str(data.get("block") or "")
    note_id = str(data.get("note") or "") or None
    start_units, end_units = _offset(data.get("start")), _offset(data.get("end"))
    if end_units <= start_units:
        raise EditorError("موضع الكلمة غير صالح.")
    typed = ""
    if action == "type":
        typed = " ".join(str(data.get("text") or "").split())
        if not typed:
            raise EditorError("اكتب الكلمة الصحيحة.")
        if len(typed) > MAX_TYPED:
            raise EditorError("النص المكتوب أطول من المسموح.")
    with transaction.atomic():
        manuscript = manuscript_of(book, lock=True)
        document = manuscript.document or {}
        chapter = doc.find_chapter(document, chapter_id)
        if chapter is None:
            raise EditorNotFound(NO_CHAPTER)
        current = chapter.nodes(document)
        version = doc.chapter_version(current)
        if str(data.get("version") or "") != version:
            raise ChapterConflict(chapter_id, version, current)
        nodes = copy.deepcopy(current)
        owner = _container(nodes, block_id, note_id)
        if owner is None:
            raise ChapterConflict(chapter_id, version, current)
        content = owner.get("content") if isinstance(owner.get("content"), list) else []
        text = doc.inline_text(content)
        start, end = doc.code_index(text, start_units), doc.code_index(text, end_units)
        word = text[start:end]
        if word != str(data.get("word") or word) or not doc.has_mark_range(content, start, end, MARK):
            raise ChapterConflict(chapter_id, version, current)
        if action == "accept":
            new_text = word
            owner["content"] = doc.unmark_range(content, start, end, MARK)
        else:
            if action == "choose":
                # the readings of every word of the container: the k-th word takes the k-th matching token
                siblings = [
                    w for w in words_of(document, chapter_id) if w.block == block_id and w.note == note_id
                ]
                attach_readings(siblings, normalizer(book))
                found = next((w for w in siblings if w.start == start and w.end == end), None)
                if found is None:
                    raise ChapterConflict(chapter_id, version, current)
                reading = next((r for r in found.readings if r["engine"] == data.get("engine")), None)
                if reading is None:
                    raise EditorError("لا توجد هذه القراءة لهذه الكلمة.")
                new_text = reading["text"]
            else:
                new_text = typed
            owner["content"] = doc.replace_range(content, start, end, new_text, drop=MARK)
        new_document = doc.splice(document, chapter, nodes)
        _write(manuscript, new_document, user)
    relayout = _schedule(book, chapter_id, manuscript.version)
    new_chapter = doc.find_chapter(new_document, chapter_id)
    new_version = doc.chapter_version(new_chapter.nodes(new_document)) if new_chapter is not None else None
    owner_text = doc.inline_text(owner.get("content") or [])
    return {
        "chapter": chapter_id,
        "version": new_version,
        "manuscript_version": manuscript.version,
        "word": new_text,
        "start": doc.utf16_index(owner_text, start),
        "end": doc.utf16_index(owner_text, start + len(new_text)),
        "remaining": count(new_document),
        "relayout": relayout,
    }


def total(manuscript: Manuscript | None) -> int:
    """The badge count for a manuscript row (0 without one)."""
    return count(manuscript.document) if manuscript is not None else 0
