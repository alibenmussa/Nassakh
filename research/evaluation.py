"""The measurement suite of the quotation checker (D107; CHALLENGE_SPEC §6): quotations with a known truth,
the real `services.verify_quote` and three alternatives on the same cases, the rates over seeds.

**Source.** The quotations are cut from the indexed text of the books under test (`PageText`, with the raw
tokens of `ocr.Line` for the verbatim form), 8–14 words, one per page, round-robin over the books. They are
labelled by how they were made (`CLASSES`); nothing is labelled by hand.

**Account.** The checker is called as a non-superuser member of an organisation that holds exactly the books
under test (the shape of the demo account), so `books.access` scopes every call as it does for a judge. The
organisation, the user and the move of the books live in one transaction that is rolled back on exit
(`eval_account`): the data is not changed. `refresh_stale` runs before it, committed (the index may be
refreshed, nothing else).

**Systems** (`SYSTEMS`), each answering `exact | differs | needs_image_check | not_found` and, when it can,
where the passage is:

- `nassakh`: `services.verify_quote`;
- `nassakh_no_doubt`: the same answers with the doubt switched off, a difference on a doubtful reading
  counting as `differs` (post-processing of `nassakh`: `needs_image_check` becomes `differs`; the tool is not
  touched);
- `raw_search`: the quotation as typed, an exact substring of the raw page text (the tokens as printed, with
  their punctuation and vowels): what Ctrl+F in a text layer does; the named alternative;
- `normalized_search`: the quotation's normal form as an exact substring of the page's normal form.

A search answers `exact` (found) or `not_found`, and never says where in the quotation a difference sits.

**Truth and metrics** are per class (`score`). Rates are per seed; the report takes their mean and range.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import random
import re
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date

from django.contrib.auth import get_user_model
from django.db import connection, transaction

from accounts.models import Membership, Organization
from books.models import Book
from ocr.models import Line

from . import index, services
from .models import PageText
from .normalize import has_diacritics, normalize_text, token_words, vowel_key
from .schemas import DiffSegment, VerifyResult

CLASSES: tuple[str, ...] = (
    "exact_body",
    "exact_notes",
    "orthography",
    "replaced_word",
    "dropped_word",
    "swapped_words",
    "vowel_change",
    "misattributed_note",
    "misattributed_body",
    "absent",
    "ocr_doubt",
)
SYSTEMS: tuple[str, ...] = ("nassakh", "nassakh_no_doubt", "raw_search", "normalized_search")
DOUBT_FAMILY = ("nassakh", "nassakh_no_doubt")
GROUPS: dict[str, tuple[str, ...]] = {
    "correct": ("exact_body", "exact_notes", "orthography"),
    "altered": ("replaced_word", "dropped_word", "swapped_words"),
    "misattributed": ("misattributed_note", "misattributed_body"),
}
# the strength sweep: 2, 3 and 4 words replaced in a quotation of the main band
STRENGTH: tuple[str, ...] = ("replaced_2", "replaced_3", "replaced_4")
# the metric each class is judged by first (failures are the cases that miss it, for the nassakh system)
PRIMARY: dict[str, str] = {
    "exact_body": "accepted",
    "exact_notes": "accepted",
    "orthography": "accepted",
    "replaced_word": "detected",
    "dropped_word": "detected",
    "swapped_words": "detected",
    "vowel_change": "named",
    "misattributed_note": "caught",
    "misattributed_body": "caught",
    "absent": "correct",
    "ocr_doubt": "needs_image",
    **dict.fromkeys(STRENGTH, "detected"),
}
MAIN_BAND = (8, 14)  # words in a quotation
SHORT_BANDS: tuple[tuple[int, int], ...] = ((4, 5), (6, 7))  # the length sweep (SENSITIVITY classes only)
SENSITIVITY: tuple[str, ...] = ("exact_body", "orthography", "replaced_word", "dropped_word", "absent")
MAX_MATCHES = 200
MAX_TRIES = 25  # windows tried per page, and pages per wanted case (× n)
SEP = "\x00"
TASHKEEL = re.compile("[\u064b-\u065f\u0670\u0640]")  # vowel marks and tatweel
HAMZA_ALEF = str.maketrans("أإآ", "ااا")
SHORT_VOWELS = "\u064e\u064f\u0650"  # fatha, damma, kasra
PUNCT = re.compile(r"^[^\w]+|[^\w]+$")
FINAL_LETTER = re.compile(r"([ةىي])([^\w]*)$")
ARABIC_WORD = re.compile("^[\u0621-\u064a]+$")  # letters only (ء to ي)
BODY, NOTES = index.BODY, index.NOTES


# ====================================================================== configuration and shapes


@dataclass(frozen=True)
class Config:
    books: tuple[int, ...] = (29, 31, 41)
    absent_books: tuple[int, ...] = (33, 34, 35, 36, 37, 38)
    seeds: int = 3
    n: int = 60
    repeat: bool = True  # run the first seed twice and compare
    index_timing: bool = True  # rebuild the books' index inside a transaction that is rolled back


@dataclass(frozen=True)
class Source:
    book_id: int
    page: int
    kind: str


@dataclass(frozen=True)
class Case:
    """A quotation, how it is checked, and what is true of it."""

    cls: str
    quote: str
    attributed_to: str
    truth: str  # exact | altered | vowels | misattributed | absent | doubt
    source: Source | None
    norm: str
    n_words: int
    vowelled: bool
    has_doubt: bool = False  # a doubtful reading sits in the passage the quotation was cut from
    # the altered words [start, end) of the quotation; start == end: a gap between two words
    changed: tuple[int, int] | None = None
    expect: str = ""  # the attribution a misattributed case should draw
    note: str = ""
    band: tuple[int, int] = MAIN_BAND

    @property
    def key(self) -> str:
        """The class, with its length band when it is not the main one (`replaced_word@4-5`)."""
        return self.cls if self.band == MAIN_BAND else f"{self.cls}@{self.band[0]}-{self.band[1]}"


@dataclass(frozen=True)
class Verdict:
    """What one system answered for one case."""

    status: str
    pages: frozenset[tuple[int, int]] = frozenset()  # (book, page) the system points at
    kind: str | None = None
    attribution: str = "ok"
    diacritics_differ: bool = False
    spans: tuple[tuple[int, int], ...] = ()  # changed words of the quotation [start, end)
    printed: str | None = None
    printed_source: str = "none"
    ratio: float = 0.0
    ms: float = 0.0
    passage_id: str | None = None
    changes: tuple[tuple[str, str, str], ...] = ()

    def fingerprint(self) -> str:
        """Everything the answer says except its time (what two runs must agree on)."""
        body = [
            self.status,
            self.ratio,
            self.attribution,
            self.diacritics_differ,
            self.passage_id,
            self.changes,
        ]
        return hashlib.sha1(json.dumps(body, ensure_ascii=False).encode()).hexdigest()[:12]


# ====================================================================== the text under test


@dataclass(frozen=True)
class Unit:
    """A token as printed (`raw`, punctuation and vowels kept) and the index words it gave."""

    raw: str
    call: bool
    words: tuple[dict, ...]
    reads: tuple[str, ...] = ()  # the other readings OCR kept: `alt`, `orig`, `tess`


@dataclass
class Doc:
    """One page and kind of the index: its words, its tokens and the two texts a search reads."""

    book_id: int
    page: int
    kind: str
    words: list[dict]
    units: list[Unit]
    norm: str
    raw: str = ""
    _windows: dict[tuple[int, int], list[tuple[int, int]]] = field(default_factory=dict, repr=False)

    @property
    def source(self) -> Source:
        return Source(self.book_id, self.page, self.kind)

    def windows(self, band: tuple[int, int] = MAIN_BAND) -> list[tuple[int, int]]:
        """Every `[start, end)` of units holding `band` words, starting and ending on a word, with no
        footnote call inside."""
        if band not in self._windows:
            low, high = band
            found: set[tuple[int, int]] = set()
            for start, first in enumerate(self.units):
                if first.call or not first.words:
                    continue
                count = 0
                for end in range(start, len(self.units)):
                    unit = self.units[end]
                    if unit.call:
                        break
                    count += len(unit.words)
                    if count > high:
                        break
                    if count >= low and unit.words:
                        found.add((start, end + 1))
            self._windows[band] = sorted(found)
        return self._windows[band]


@dataclass(frozen=True)
class Window:
    doc: Doc
    start: int
    end: int

    @property
    def units(self) -> list[Unit]:
        return self.doc.units[self.start : self.end]

    @property
    def words(self) -> list[dict]:
        return [word for unit in self.units for word in unit.words]

    @property
    def texts(self) -> list[str]:
        return [word["text"] for word in self.words]

    @property
    def verbatim(self) -> str:
        """The tokens as printed, joined by single spaces (what a reader copies)."""
        return " ".join(unit.raw for unit in self.units if unit.raw)

    @property
    def clean(self) -> bool:
        """A run of real words: most of them two letters or more, few bare numbers."""
        norms = [word["norm"] for word in self.words]
        long = sum(1 for norm in norms if len(norm) >= 2 and not norm.isdigit())
        return long >= 0.8 * len(norms) and sum(1 for norm in norms if norm.isdigit()) <= 2


def _units(words: Sequence[dict], tokens: dict[int, list] | None) -> list[Unit]:
    """The units of a page's words: with `tokens` (line id → `Line.tokens`) every token of the lines the
    words sit on, in order, as printed; without, one unit per token that gave words."""
    by_token: dict[tuple[int, int], list[dict]] = defaultdict(list)
    lines: list[int] = []
    for word in words:
        by_token[(word["line"], word["i"])].append(word)
        if not lines or lines[-1] != word["line"]:
            lines.append(word["line"])
    units: list[Unit] = []
    for line_id in lines:
        line_tokens = (tokens or {}).get(line_id)
        if line_tokens is None:
            for key in sorted(k for k in by_token if k[0] == line_id):
                group = by_token[key]
                units.append(Unit(raw=" ".join(w["text"] for w in group), call=False, words=tuple(group)))
            continue
        for i, token in enumerate(line_tokens):
            if not isinstance(token, dict):
                continue
            raw = str(token.get("t") or "").strip()
            group = by_token.get((line_id, i), [])
            if not raw and not group:
                continue
            reads = tuple(str(token.get(key) or "") for key in ("alt", "orig", "tess"))
            units.append(Unit(raw=raw, call=bool(token.get("call")), words=tuple(group), reads=reads))
    return units


def load_docs(book_ids: Sequence[int], with_tokens: bool = True) -> list[Doc]:
    """The index rows of the books as `Doc`s, in book, page and kind order."""
    rows = (
        PageText.objects.filter(book_id__in=list(book_ids), page__is_excluded=False)
        .exclude(norm="")
        .select_related("page")
        .order_by("book_id", "page__number", "kind")
    )
    tokens = (
        dict(Line.objects.filter(page__book_id__in=list(book_ids)).values_list("id", "tokens"))
        if with_tokens
        else None
    )
    docs = []
    for row in rows:
        units = _units(row.words, tokens)
        docs.append(
            Doc(
                book_id=row.book_id,
                page=row.page.number,
                kind=row.kind,
                words=row.words,
                units=units,
                norm=row.norm,
                raw=" ".join(unit.raw for unit in units if unit.raw),
            )
        )
    return docs


class TextIndex:
    """Exact substring search over many texts at once (one string, a separator between the texts)."""

    def __init__(self, docs: Sequence[Doc], attr: str) -> None:
        self.docs = list(docs)
        parts = [getattr(doc, attr) for doc in self.docs]
        self.text = SEP.join(parts)
        self.starts: list[int] = []
        position = 0
        for part in parts:
            self.starts.append(position)
            position += len(part) + 1

    def find(self, query: str) -> list[Doc]:
        """The docs holding `query` (in order, each once)."""
        if not query or SEP in query:
            return []
        hit: list[int] = []
        at = self.text.find(query)
        seen = 0
        while at != -1 and seen < MAX_MATCHES:
            seen += 1
            number = bisect.bisect_right(self.starts, at) - 1
            if not hit or hit[-1] != number:
                hit.append(number)
            at = self.text.find(query, at + 1)
        return [self.docs[number] for number in hit]


@dataclass
class Corpus:
    """The books under test and the books that give absent quotations."""

    docs: list[Doc]
    absent_docs: list[Doc]
    vocab: Counter = field(default_factory=Counter)  # norm → count, confident readings only
    raws: dict[str, Counter] = field(default_factory=dict)  # norm → raw forms
    by_len: dict[int, list[str]] = field(default_factory=dict)
    raw_index: TextIndex = field(init=False)
    norm_index: TextIndex = field(init=False)

    def __post_init__(self) -> None:
        self.raw_index = TextIndex(self.docs, "raw")
        self.norm_index = TextIndex(self.docs, "norm")
        raws: dict[str, Counter] = defaultdict(Counter)
        for doc in self.docs:
            for word in doc.words:
                if word["state"] != index.DOUBTFUL and ARABIC_WORD.match(word["norm"]):
                    self.vocab[word["norm"]] += 1
                    raws[word["norm"]][word["text"]] += 1
        self.raws = dict(raws)
        buckets: dict[int, list[str]] = defaultdict(list)
        for norm, count in sorted(self.vocab.items()):
            if count >= 3 and len(norm) >= 2:
                buckets[len(norm)].append(norm)
        self.by_len = dict(buckets)

    def similar(self, norm: str) -> list[str]:
        """Words of the books' own vocabulary of the length of `norm` ±1, other than it."""
        return [
            word
            for length in (len(norm) - 1, len(norm), len(norm) + 1)
            for word in self.by_len.get(length, [])
            if word != norm
        ]

    def raw_form(self, norm: str, vowelled: bool) -> str:
        """The most common printed form of `norm`, vowelled when `vowelled` and one exists."""
        forms = sorted(self.raws[norm].items(), key=lambda item: (-item[1], item[0]))
        for text, _count in forms:
            if has_diacritics(text) == vowelled:
                return text
        return forms[0][0]

    def stats(self) -> dict:
        words = [word for doc in self.docs for word in doc.words]
        states = Counter(word["state"] for word in words)
        return {
            "pages": len({(doc.book_id, doc.page) for doc in self.docs}),
            "rows": len(self.docs),
            "words": len(words),
            "doubtful_words": states[index.DOUBTFUL],
            "doubtful_share": round(states[index.DOUBTFUL] / max(1, len(words)), 4),
            "vowelled_share": round(
                sum(1 for w in words if has_diacritics(w["text"])) / max(1, len(words)), 4
            ),
            "vocabulary": len(self.vocab),
            "absent_rows": len(self.absent_docs),
            "by_book": {
                str(book_id): sum(len(doc.words) for doc in self.docs if doc.book_id == book_id)
                for book_id in sorted({doc.book_id for doc in self.docs})
            },
        }


# ====================================================================== making the cases


def _alpha(norm: str) -> bool:
    return bool(ARABIC_WORD.match(norm))


def type_like_people(text: str, rng: random.Random) -> str:
    """The quotation as people type it: vowel marks and tatweel gone, أ إ آ as ا, a final ة as ه, a final
    ى and ي swapped, trailing punctuation dropped, a double space here and there, a full stop at the end."""
    words = []
    for word in text.split():
        word = TASHKEEL.sub("", word).translate(HAMZA_ALEF)
        match = FINAL_LETTER.search(word)
        if match and len(word) > 2:
            letter = match.group(1)
            if letter == "ة" and rng.random() < 0.6:
                word = word[: match.start(1)] + "ه" + match.group(2)
            elif letter == "ى" and rng.random() < 0.5:
                word = word[: match.start(1)] + "ي" + match.group(2)
            elif letter == "ي" and rng.random() < 0.4:
                word = word[: match.start(1)] + "ى" + match.group(2)
        if rng.random() < 0.5:
            word = PUNCT.sub("", word) or word
        words.append(word)
    typed = ""
    for n, word in enumerate(words):
        typed += word + (("  " if rng.random() < 0.15 else " ") if n < len(words) - 1 else "")
    return typed + ("." if rng.random() < 0.3 else "")


def has_orthographic_variation(win: Window) -> bool:
    return any(TASHKEEL.search(text) or re.search("[أإآ]|[ةى]$", text) for text in win.texts if len(text) > 2)


def _case(win_or_none: Window | None, cls: str, quote: str, truth: str, **extra) -> Case:
    source = win_or_none.doc.source if win_or_none is not None else None
    doubt = win_or_none is not None and any(w["state"] == index.DOUBTFUL for w in win_or_none.words)
    norm = normalize_text(quote)
    return Case(
        cls=cls,
        quote=quote,
        attributed_to=extra.pop("attributed_to", "unknown"),
        truth=truth,
        source=source,
        norm=norm,
        n_words=len(norm.split()),
        vowelled=has_diacritics(quote),
        has_doubt=doubt,
        **extra,
    )


def _plain(norm_of_window: str, quote: str) -> bool:
    return bool(quote) and normalize_text(quote) == norm_of_window


def _norm_of(win: Window) -> str:
    return " ".join(word["norm"] for word in win.words)


def build_exact(cls: str, attributed_to: str, expect: str = "") -> Callable:
    truth = "misattributed" if expect else "exact"

    def build(win: Window, rng: random.Random, corpus: Corpus) -> Case | None:
        if not _plain(_norm_of(win), win.verbatim):
            return None
        return _case(win, cls, win.verbatim, truth, attributed_to=attributed_to, expect=expect)

    return build


def build_orthography(win: Window, rng: random.Random, corpus: Corpus) -> Case | None:
    typed = type_like_people(win.verbatim, rng)
    if typed == win.verbatim or not _plain(_norm_of(win), typed):
        return None
    return _case(win, "orthography", typed, "exact")


def _free(words: Sequence[dict], p: int) -> bool:
    """A word an alteration may fall on: a letters-only word that is not a doubtful reading."""
    return words[p]["state"] != index.DOUBTFUL and _alpha(words[p]["norm"]) and len(words[p]["norm"]) >= 2


def build_replaced(win: Window, rng: random.Random, corpus: Corpus) -> Case | None:
    words = win.words
    spots = [p for p in range(len(words)) if _free(words, p)]
    if not spots:
        return None
    p = rng.choice(spots)
    pool = corpus.similar(words[p]["norm"])
    if not pool:
        return None
    new = rng.choice(pool)
    texts = win.texts
    texts[p] = corpus.raw_form(new, has_diacritics(texts[p]))
    quote = " ".join(texts)
    if normalize_text(quote) == _norm_of(win):
        return None
    return _case(win, "replaced_word", quote, "altered", changed=(p, p + 1), note=f"{words[p]['norm']}→{new}")


def build_replaced_k(k: int) -> Callable:
    """`k` words replaced from the books' vocabulary: the quotation drifts from the passage (the strength
    sweep: where does «differs» become «not found»)."""

    def build(win: Window, rng: random.Random, corpus: Corpus) -> Case | None:
        words = win.words
        spots = [p for p in range(len(words)) if _free(words, p)]
        if len(spots) < k:
            return None
        chosen = sorted(rng.sample(spots, k))
        texts = win.texts
        for p in chosen:
            pool = corpus.similar(words[p]["norm"])
            if not pool:
                return None
            texts[p] = corpus.raw_form(rng.choice(pool), has_diacritics(texts[p]))
        quote = " ".join(texts)
        if normalize_text(quote) == _norm_of(win):
            return None
        return _case(win, f"replaced_{k}", quote, "altered", changed=(chosen[0], chosen[-1] + 1))

    return build


def build_dropped(win: Window, rng: random.Random, corpus: Corpus) -> Case | None:
    words = win.words
    # an inner word only: a word dropped at an edge leaves a shorter verbatim quotation, not an alteration
    spots = [
        p
        for p in range(1, len(words) - 1)
        if _free(words, p) and words[p]["norm"] not in (words[p - 1]["norm"], words[p + 1]["norm"])
    ]
    if not spots:
        return None
    p = rng.choice(spots)
    texts = win.texts
    dropped = texts.pop(p)
    return _case(win, "dropped_word", " ".join(texts), "altered", changed=(p, p), note=f"-{dropped}")


def build_swapped(win: Window, rng: random.Random, corpus: Corpus) -> Case | None:
    words = win.words
    spots = [
        p
        for p in range(len(words) - 1)
        if _free(words, p) and _free(words, p + 1) and words[p]["norm"] != words[p + 1]["norm"]
    ]
    if not spots:
        return None
    p = rng.choice(spots)
    texts = win.texts
    texts[p], texts[p + 1] = texts[p + 1], texts[p]
    return _case(win, "swapped_words", " ".join(texts), "altered", changed=(p, p + 2), note=f"{p}↔{p + 1}")


def _vowel_spots(texts: Sequence[str]) -> list[int]:
    return [p for p, text in enumerate(texts) if any(mark in text for mark in SHORT_VOWELS)]


def accept_vowelled(win: Window) -> bool:
    return bool(_vowel_spots(win.texts))


def build_vowel_change(win: Window, rng: random.Random, corpus: Corpus) -> Case | None:
    texts = win.texts
    spots = _vowel_spots(texts)
    if not spots:
        return None
    p = rng.choice(spots)
    word = texts[p]
    marks = [n for n, ch in enumerate(word) if ch in SHORT_VOWELS]
    n = rng.choice(marks)
    new = rng.choice([mark for mark in SHORT_VOWELS if mark != word[n]])
    changed = word[:n] + new + word[n + 1 :]
    if vowel_key(changed) == vowel_key(word):
        return None
    texts[p] = changed
    quote = " ".join(texts)
    if normalize_text(quote) != _norm_of(win):
        return None
    return _case(win, "vowel_change", quote, "vowels", changed=(p, p + 1), note=f"{word}→{changed}")


def doubt_reading(unit: Unit) -> tuple[str, str] | None:
    """The other reading OCR kept for a doubtful one-word token: `(printed form, which)`, a different word of
    letters only (`alt` is the second model's, `orig` the first's after a vote, `tess` Tesseract's)."""
    if len(unit.words) != 1 or unit.words[0]["state"] != index.DOUBTFUL:
        return None
    word = unit.words[0]
    if not _alpha(word["norm"]):
        return None
    for label, text in zip(("alt", "orig", "tess"), unit.reads, strict=False):
        parts = token_words(text) if text else []
        if (
            len(parts) == 1
            and parts[0].norm != word["norm"]
            and _alpha(parts[0].norm)
            and len(parts[0].norm) >= 2
        ):
            return parts[0].raw, label
    return None


def accept_doubt(win: Window) -> bool:
    return any(doubt_reading(unit) for unit in win.units)


def build_ocr_doubt(win: Window, rng: random.Random, corpus: Corpus) -> Case | None:
    spots = []
    position = 0
    for unit in win.units:
        if unit.words and doubt_reading(unit):
            spots.append((position, unit))
        position += len(unit.words)
    if not spots:
        return None
    p, unit = rng.choice(spots)
    reading, label = doubt_reading(unit)  # type: ignore[misc]
    texts = win.texts
    was = texts[p]
    texts[p] = reading
    return _case(
        win, "ocr_doubt", " ".join(texts), "doubt", changed=(p, p + 1), note=f"{was}→{reading} ({label})"
    )


@dataclass(frozen=True)
class Spec:
    """Where a class draws its pages from, which windows it accepts and how it builds the case."""

    pool: str  # body | notes | both | absent
    build: Callable[[Window, random.Random, Corpus], Case | None]
    accept: Callable[[Window], bool] = lambda win: True


def build_absent(win: Window, rng: random.Random, corpus: Corpus) -> Case | None:
    quote = " ".join(win.texts)
    norm = normalize_text(quote)
    if corpus.norm_index.find(norm):
        return None  # the sentence is in the books under test after all: not an absent one
    return _case(None, "absent", quote, "absent")


SPECS: dict[str, Spec] = {
    "exact_body": Spec(BODY, build_exact("exact_body", "author")),
    "exact_notes": Spec(NOTES, build_exact("exact_notes", "editor")),
    "orthography": Spec("both", build_orthography, has_orthographic_variation),
    "replaced_word": Spec("both", build_replaced),
    "dropped_word": Spec("both", build_dropped),
    "swapped_words": Spec("both", build_swapped),
    "vowel_change": Spec("both", build_vowel_change, accept_vowelled),
    "misattributed_note": Spec(NOTES, build_exact("misattributed_note", "author", "note_not_author")),
    "misattributed_body": Spec(BODY, build_exact("misattributed_body", "editor", "body_not_editor")),
    "absent": Spec("absent", build_absent),
    "ocr_doubt": Spec("both", build_ocr_doubt, accept_doubt),
    **{f"replaced_{k}": Spec("both", build_replaced_k(k)) for k in (2, 3, 4)},
}


def doc_stream(docs: Sequence[Doc], rng: random.Random) -> Iterator[Doc]:
    """The docs round-robin over their books, each book's docs shuffled and used once per round (so a class's
    cases come from different pages until a book runs out)."""
    by_book: dict[int, list[Doc]] = defaultdict(list)
    for doc in docs:
        by_book[doc.book_id].append(doc)
    queues = {book_id: [] for book_id in sorted(by_book)}
    while queues:
        for book_id in list(queues):
            if not queues[book_id]:
                queues[book_id] = rng.sample(by_book[book_id], len(by_book[book_id]))
            yield queues[book_id].pop()


def make_cases(corpus: Corpus, cls: str, seed: int, n: int, band: tuple[int, int] = MAIN_BAND) -> list[Case]:
    """`n` cases of the class, quotations of `band` words (fewer when the books hold too little text of its
    kind)."""
    spec = SPECS[cls]
    rng = random.Random(f"{seed}:{cls}" if band == MAIN_BAND else f"{seed}:{cls}:{band[0]}-{band[1]}")
    if spec.pool == "absent":
        pool = corpus.absent_docs
    elif spec.pool == "both":
        pool = corpus.docs
    else:
        pool = [doc for doc in corpus.docs if doc.kind == spec.pool]
    pool = [doc for doc in pool if doc.windows(band)]
    if not pool:
        return []
    cases: list[Case] = []
    seen: set[str] = set()
    for tried, doc in enumerate(doc_stream(pool, rng)):
        if len(cases) >= n or tried >= MAX_TRIES * n:
            break
        windows = doc.windows(band)
        for _ in range(MAX_TRIES):
            start, end = rng.choice(windows)
            win = Window(doc, start, end)
            if not win.clean or not spec.accept(win):
                continue
            case = spec.build(win, rng, corpus)
            if case is not None and case.norm not in seen and case.n_words >= 3:
                seen.add(case.norm)
                cases.append(replace(case, band=band))
                break
    return cases


# ====================================================================== the systems


def diff_spans(diff: Sequence[DiffSegment]) -> tuple[tuple[int, int], ...]:
    """The quotation's changed words `[start, end)` from the word-by-word diff (a book's word the quotation
    leaves out is the empty span at its place)."""
    spans: list[tuple[int, int]] = []
    position = 0
    for segment in diff:
        quoted, printed = len(segment.quote.split()), len(segment.source.split())
        if segment.op == "equal":
            position += printed
        elif segment.op == "missing" or (segment.op == "moved" and not segment.quote):
            spans.append((position, position))
        else:  # replaced, added, moved (the quotation's side)
            spans.append((position, position + quoted))
            position += quoted
    return tuple(spans)


def verdict_of(result: VerifyResult, ms: float) -> Verdict:
    pages = frozenset({(result.book.id, result.page.number)}) if result.book and result.page else frozenset()
    return Verdict(
        status=result.status,
        pages=pages,
        kind=result.kind,
        attribution=result.attribution,
        diacritics_differ=result.diacritics_differ,
        spans=diff_spans(result.diff),
        printed=result.page.printed if result.page else None,
        printed_source=result.page.printed_source if result.page else "none",
        ratio=result.ratio,
        ms=ms,
        passage_id=result.passage_id,
        changes=tuple((change.type, change.quote, change.source) for change in result.changes),
    )


def without_doubt(verdict: Verdict) -> Verdict:
    """`nassakh_no_doubt`: a difference on a doubtful reading is a difference."""
    return replace(verdict, status="differs") if verdict.status == "needs_image_check" else verdict


def search_verdict(docs: list[Doc], ms: float) -> Verdict:
    return Verdict(
        status="exact" if docs else "not_found",
        pages=frozenset((doc.book_id, doc.page) for doc in docs),
        kind=docs[0].kind if docs else None,
        ms=ms,
    )


def run_case(user, corpus: Corpus, case: Case) -> dict[str, Verdict]:
    """The four systems' answers to one case (the searches never see the attribution)."""
    started = time.perf_counter()
    raw = search_verdict(corpus.raw_index.find(case.quote), (time.perf_counter() - started) * 1000)
    started = time.perf_counter()
    norm = search_verdict(corpus.norm_index.find(case.norm), (time.perf_counter() - started) * 1000)
    started = time.perf_counter()
    result = services.verify_quote(user, case.quote, attributed_to=case.attributed_to)
    nassakh = verdict_of(result, (time.perf_counter() - started) * 1000)
    return {
        "nassakh": nassakh,
        "nassakh_no_doubt": without_doubt(nassakh),
        "raw_search": raw,
        "normalized_search": norm,
    }


# ====================================================================== scoring


def _cover(span: tuple[int, int]) -> set[int]:
    start, end = span
    return set(range(start - 1, end + 1)) if start == end else set(range(start, end))


def names_where(spans: Sequence[tuple[int, int]], changed: tuple[int, int] | None) -> bool:
    """A reported change overlaps the altered words (a gap counts as the two words beside it)."""
    if changed is None:
        return False
    truth = _cover(changed)
    return any(_cover(span) & truth for span in spans)


def score(case: Case, system: str, verdict: Verdict, twins: frozenset[tuple[int, int]]) -> dict[str, bool]:
    """The metrics of one answer to one case (`PRIMARY` names the class's first). `twins`: the pages that hold
    the quotation's normal form verbatim (a repeated passage counts as found on any of them)."""
    status = verdict.status
    ours = system in DOUBT_FAMILY
    out: dict[str, bool] = {}
    if case.source is not None:
        want = {(case.source.book_id, case.source.page)} | set(twins)
        out["location"] = bool(verdict.pages & want)
    if case.cls in GROUPS["correct"]:
        out["accepted"] = status == "exact"
        out["false_alarm"] = status != "exact"
        if ours and case.cls != "orthography":
            out["attribution_ok"] = verdict.attribution == "ok"
    elif case.cls in GROUPS["altered"]:
        out["detected"] = status == "differs" if ours else status == "not_found"
        out["flagged"] = status in ("differs", "needs_image_check") if ours else status == "not_found"
        if ours:  # flagged (differs, or needs the image) with the change at the altered words
            out["located"] = out["flagged"] and names_where(verdict.spans, case.changed)
    elif case.cls in STRENGTH:
        out["detected"] = status == "differs" if ours else status == "not_found"
        out["flagged"] = status in ("differs", "needs_image_check") if ours else status == "not_found"
        out["lost"] = status == "not_found"
    elif case.cls == "vowel_change":
        out["named"] = ours and status == "exact" and verdict.diacritics_differ
        out["says_not_found"] = status == "not_found"
    elif case.cls in GROUPS["misattributed"]:
        out["caught"] = ours and verdict.attribution == case.expect
        out["found"] = status != "not_found"
    elif case.cls == "absent":
        out["correct"] = status == "not_found"
        out["false_match"] = status != "not_found"
    elif case.cls == "ocr_doubt":
        out["needs_image"] = status == "needs_image_check"
        out["false_accusation"] = status == "differs" if ours else status == "not_found"
    return out


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (`q` in 0–1)."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)] if ordered else 0.0


@dataclass
class SeedRun:
    seed: int
    cases: list[Case]
    verdicts: list[dict[str, Verdict]]

    def fingerprints(self) -> list[str]:
        return [v["nassakh"].fingerprint() for v in self.verdicts]


def tally(run: SeedRun) -> dict:
    """Counts of one seed: `rates[class][system][metric] = [hits, n]`, the answers' spread, the times."""
    rates: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: [0, 0])))
    status: dict = defaultdict(lambda: defaultdict(Counter))
    ms: dict[str, list[float]] = defaultdict(list)
    printed: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for case, verdicts in zip(run.cases, run.verdicts, strict=True):
        twins = verdicts["normalized_search"].pages
        main = case.band == MAIN_BAND
        keys = [case.key] + (
            [name for name, members in GROUPS.items() if case.cls in members] if main else []
        )
        for system, verdict in verdicts.items():
            if main:
                ms[system].append(verdict.ms)
            for key in keys:
                status[key][system][verdict.status] += 1
            for metric, hit in score(case, system, verdict, twins).items():
                for key in keys:
                    cell = rates[key][system][metric]
                    cell[0] += int(hit)
                    cell[1] += 1
        if main and verdicts["nassakh"].pages:
            printed["all"][0] += int(verdicts["nassakh"].printed is not None)
            printed["all"][1] += 1
    return {
        "rates": {k: {s: dict(m) for s, m in v.items()} for k, v in rates.items()},
        "status": {k: {s: dict(c) for s, c in v.items()} for k, v in status.items()},
        "ms": {s: {"median": statistics.median(v), "p95": percentile(v, 0.95)} for s, v in ms.items()},
        "printed_known": printed["all"],
        "cases": {
            key: {
                "n": sum(1 for c in run.cases if c.key == key),
                "vowelled": sum(1 for c in run.cases if c.key == key and c.vowelled),
                "doubt": sum(1 for c in run.cases if c.key == key and c.has_doubt),
                "words": sum(c.n_words for c in run.cases if c.key == key),
            }
            for key in sorted({c.key for c in run.cases})
        },
    }


def spread(values: Sequence[float]) -> dict[str, float]:
    return {"mean": statistics.fmean(values), "min": min(values), "max": max(values)} if values else {}


def aggregate(tallies: dict[int, dict]) -> dict:
    """Mean and range over seeds of every rate, answer share and time, with the pooled n."""
    rates: dict = {}
    keys = {k for t in tallies.values() for k in t["rates"]}
    for key in sorted(keys):
        for system in SYSTEMS:
            metrics = {m for t in tallies.values() for m in t["rates"].get(key, {}).get(system, {})}
            for metric in sorted(metrics):
                cells = [
                    t["rates"][key][system][metric]
                    for t in tallies.values()
                    if metric in t["rates"].get(key, {}).get(system, {})
                ]
                per_seed = [hits / n for hits, n in cells if n]
                rates.setdefault(key, {}).setdefault(system, {})[metric] = {
                    **spread(per_seed),
                    "n": sum(n for _hits, n in cells),
                    "hits": sum(hits for hits, _n in cells),
                    "seeds": len(per_seed),
                }
    status: dict = {}
    for key in sorted({k for t in tallies.values() for k in t["status"]}):
        for system in SYSTEMS:
            counts: Counter = Counter()
            for t in tallies.values():
                counts.update(t["status"].get(key, {}).get(system, {}))
            total = sum(counts.values())
            status.setdefault(key, {})[system] = (
                {name: counts[name] / total for name in sorted(counts)} if total else {}
            )
    latency = {
        system: {
            "median_ms": spread([t["ms"][system]["median"] for t in tallies.values()]),
            "p95_ms": spread([t["ms"][system]["p95"] for t in tallies.values()]),
        }
        for system in SYSTEMS
    }
    cases = {}
    for key in sorted({k for t in tallies.values() for k in t["cases"]}):
        counts = [t["cases"][key] for t in tallies.values() if key in t["cases"]]
        total = max(1, sum(c["n"] for c in counts))
        cases[key] = {
            "n_per_seed": statistics.fmean(c["n"] for c in counts),
            "vowelled_share": sum(c["vowelled"] for c in counts) / total,
            "doubt_share": sum(c["doubt"] for c in counts) / total,
            "mean_words": sum(c["words"] for c in counts) / total,
        }
    known = [t["printed_known"] for t in tallies.values()]
    return {
        "rates": rates,
        "status": status,
        "latency": latency,
        "cases": cases,
        "printed_known_share": sum(k[0] for k in known) / max(1, sum(k[1] for k in known)),
    }


def failures(runs: Sequence[SeedRun], limit: int = 8) -> dict[str, list[dict]]:
    """Per class, the first cases the nassakh system missed by the class's primary metric."""
    out: dict[str, list[dict]] = defaultdict(list)
    for run in runs:
        for case, verdicts in zip(run.cases, run.verdicts, strict=True):
            verdict = verdicts["nassakh"]
            twins = verdicts["normalized_search"].pages
            if (
                score(case, "nassakh", verdict, twins).get(PRIMARY[case.cls], True)
                or len(out[case.key]) >= limit
            ):
                continue
            out[case.key].append(
                {
                    "seed": run.seed,
                    "quote": case.quote,
                    "note": case.note,
                    "source": [case.source.book_id, case.source.page, case.source.kind]
                    if case.source
                    else None,
                    "changed": case.changed,
                    "status": verdict.status,
                    "ratio": verdict.ratio,
                    "attribution": verdict.attribution,
                    "diacritics_differ": verdict.diacritics_differ,
                    "got": sorted(verdict.pages),
                    "changes": [list(change) for change in verdict.changes],
                }
            )
    return dict(out)


# ====================================================================== the account and the index


@contextmanager
def eval_account(book_ids: Sequence[int]):
    """A non-superuser member of an organisation that holds exactly `book_ids`, as the demo account does. The
    organisation, the user and the move of the books are one transaction, rolled back on exit."""
    found = set(Book.objects.filter(pk__in=list(book_ids)).values_list("pk", flat=True))
    if found != set(book_ids):
        missing = ", ".join(map(str, sorted(set(book_ids) - found)))
        raise LookupError(f"No book with id {missing}.")
    with transaction.atomic():
        try:
            organisation = Organization.objects.create(name="research_eval")
            user = get_user_model().objects.create_user(
                "research-eval@example.invalid", email="research-eval@example.invalid", password=None
            )
            Membership.objects.create(user=user, organization=organisation, role=Membership.Role.MEMBER)
            Book.objects.filter(pk__in=list(book_ids)).update(organization=organisation)
            yield user
        finally:
            transaction.set_rollback(True)


def index_report(book_ids: Sequence[int], timing: bool) -> dict:
    """Size of the books' index, and how long a rebuild takes (inside a transaction that is rolled back)."""
    rows = list(PageText.objects.filter(book_id__in=list(book_ids)).only("words", "norm"))
    out: dict = {
        "rows": len(rows),
        "words": sum(len(row.words) for row in rows),
        "norm_chars": sum(len(row.norm) for row in rows),
        "words_json_bytes": sum(len(json.dumps(row.words, ensure_ascii=False).encode()) for row in rows),
        "vendor": connection.vendor,
    }
    if timing:
        built: dict[str, dict] = {}
        with transaction.atomic():
            for book_id in book_ids:
                started = time.perf_counter()
                pages = index.reindex_book(book_id)
                built[str(book_id)] = {"pages": pages, "seconds": round(time.perf_counter() - started, 2)}
            transaction.set_rollback(True)
        total = sum(item["seconds"] for item in built.values())
        out["build"] = {
            "books": built,
            "seconds": round(total, 2),
            "pages": sum(item["pages"] for item in built.values()),
        }
    return out


# ====================================================================== the run


def run_seed(user, corpus: Corpus, seed: int, n: int, sweep: bool = True) -> SeedRun:
    """The main cases of a seed and the sweeps' (`STRENGTH`; `SENSITIVITY` classes at `SHORT_BANDS`)."""
    cases = [case for cls in CLASSES for case in make_cases(corpus, cls, seed, n)]
    if sweep:
        cases += [case for cls in STRENGTH for case in make_cases(corpus, cls, seed, n)]
        cases += [
            case
            for band in SHORT_BANDS
            for cls in SENSITIVITY
            for case in make_cases(corpus, cls, seed, n, band)
        ]
    return SeedRun(seed=seed, cases=cases, verdicts=[run_case(user, corpus, case) for case in cases])


def repeatability(first: SeedRun, again: SeedRun) -> dict:
    """The same seed run twice: the cases, then the checker's answers (status, ratio, passage, changes)."""
    same_cases = [(c.cls, c.quote, c.attributed_to) for c in first.cases] == [
        (c.cls, c.quote, c.attributed_to) for c in again.cases
    ]
    a, b = first.fingerprints(), again.fingerprints()
    differing = [n for n, (x, y) in enumerate(zip(a, b, strict=False)) if x != y]
    return {
        "seed": first.seed,
        "same_cases": same_cases,
        "cases": len(a),
        "identical": len(a) - len(differing) if len(a) == len(b) else 0,
        "differing": [
            {"quote": first.cases[n].quote, "class": first.cases[n].cls, "first": a[n], "again": b[n]}
            for n in differing[:5]
        ],
    }


def evaluate(config: Config, log: Callable[[str], None] = lambda message: None) -> dict:
    """The whole evaluation: the report as a JSON-ready dict (`research.eval_report` renders it)."""
    wanted = list(config.books) + list(config.absent_books)
    index.refresh_stale(wanted)  # committed: a stale index may be refreshed, nothing else is written
    log("index")
    index_info = index_report(config.books, config.index_timing)
    corpus = Corpus(load_docs(config.books), load_docs(config.absent_books, with_tokens=False))
    log(f"corpus: {len(corpus.docs)} rows, {len(corpus.absent_docs)} absent rows")
    runs: list[SeedRun] = []
    repeat: dict | None = None
    with eval_account(config.books) as user:
        for seed in range(1, config.seeds + 1):
            started = time.perf_counter()
            run = run_seed(user, corpus, seed, config.n)
            runs.append(run)
            log(f"seed {seed}: {len(run.cases)} cases in {time.perf_counter() - started:.0f}s")
        if config.repeat and runs:
            again = run_seed(user, corpus, runs[0].seed, config.n)
            repeat = repeatability(runs[0], again)
            log(f"repeat of seed {runs[0].seed}: {repeat['identical']}/{repeat['cases']} identical")
    tallies = {run.seed: tally(run) for run in runs}
    return {
        "meta": {
            "generated": date.today().isoformat(),
            "books": list(config.books),
            "absent_books": list(config.absent_books),
            "seeds": [run.seed for run in runs],
            "n": config.n,
            "database": connection.vendor,
            "titles": {
                str(pk): title for pk, title in Book.objects.filter(pk__in=wanted).values_list("pk", "title")
            },
            "classes": list(CLASSES),
            "systems": list(SYSTEMS),
        },
        "corpus": corpus.stats(),
        "index": index_info,
        "aggregate": aggregate(tallies),
        "per_seed": {str(seed): t for seed, t in tallies.items()},
        "repeatability": repeat,
        "failures": failures(runs),
    }
