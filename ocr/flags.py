"""Flag policy v2 (D71), words only the second model read (D72), the looped prefix (D73): pure functions.

Every region has up to three readers: Qari v0.3 (the primary, whose tokens make the text), Qari v0.2
(the secondary) and Tesseract. Measured on the stored runs and review outcomes of 14 books
(docs/PHASE7_SPEC.md §2.4, `manage.py rebuild_lines --report`):

- `second_readings` gives Qari v0.2's reading of each primary token. Punctuation runs are split off
  both sides first (`split_pieces`), words are compared leniently and marks only against marks, and
  the secondary pieces are glued back in the primary token's shape, so «القرآن.» equals «القرآن .»
  (the whitespace alignment it replaces flagged the «.»: 95 punctuation flags, 89 of them right); a
  word whose only counterpart is a mark has no reading (v0.2 skipped it).
- `classify` gives the reasons a token is doubtful (`why`), first match first; an empty list is sure:
  `script` (letters of another script, a stray symbol, Arabic and Latin in one token), punctuation
  (never), a Western number all three readers read alike (sure, 70 of 70 right; any other number is
  `number`, D50–D51), a lone letter that may be a digit (D51, as before 7b), a one-reader region
  (`single` when Tesseract's word there is Arabic, confident and of another dotless skeleton), a word
  Qari v0.2 lacks (sure when Tesseract read the same word, 27 right of 28; else `alone`), and the two
  models differing leniently (`disagree`).
- `vote_reading` is the D26 vote: where the models differ and Tesseract backs Qari v0.2, v0.2's
  reading goes into the text (right in 88 of 116; `ocr.chooser`), the word stays open.
- `secondary_only_runs` finds the words Qari v0.2 read where v0.3 has nothing (homoeoteleuton: the
  model jumps from one repeated phrase to the next), drops the false ones (`drop_reason`) and
  `run_support` measures how much of a run Tesseract read around it: a supported run enters the text
  as one group of `missing` words, any other becomes a suggestion (`ocr.TextGap`, never text).
- `looped_prefix` is the clean start of a looped output, a second reading for the words it covers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from core.arabic import is_digit_token, normalize, strip_markup, strip_tashkeel, to_western_digits

from .alignment import align_tokens, is_word, norm_token

# ---------------------------------------------------------------- reasons (`why`)

SCRIPT = "script"
NUMBER = "number"
DISAGREE = "disagree"
ALONE = "alone"
SINGLE = "single"
MISSING = "missing"
YEAR = "year"
REASONS: tuple[str, ...] = (SCRIPT, NUMBER, DISAGREE, ALONE, SINGLE, MISSING, YEAR)

# Punctuation runs split off words (the combining marks of Arabic and the tatweel stay with the word).
_SPLIT = re.compile(r"([^\wً-ٰٟـ]+)")
# Letters of Greek, Cyrillic, Hebrew, kana, CJK and Hangul, and the stray symbols seen in stored tokens
# («※» a Quranic verse mark, «^» for «٧», «© ¢ € ¥ ≡»). «°», «%» and the like are ordinary marks.
_FOREIGN_LETTER = re.compile(r"[Ͱ-ϿЀ-ӿ֐-׿぀-ヿ一-鿿가-힯]")
STRANGE_SYMBOLS: frozenset[str] = frozenset("※★∩∧∨≡→©¢€¥^")
_ARABIC = re.compile(r"[ء-يٮ-ۓ]")
_LATIN = re.compile(r"[A-Za-z]")
_ARABIC_INDIC = re.compile(r"[٠-٩۰-۹]")
_NOT_DIGIT = re.compile(r"\D")
# A lone letter (or two) that looks like a digit, bare or with brackets / punctuation: «آ», «(ه)», «اا»
# (the same shape as `ocr.alignment`'s, D51).
_LETTER_DIGIT = re.compile(r"^[(\[«]?[اأإآهع]{1,2}ـ?[)\]»]?[.،:؛]?$")

# ---------------------------------------------------------------- one reader (`single`)

SINGLE_MIN_CONF = 85  # Tesseract's confidence for a `single` flag (6 flags, 3 real on the reviewed tokens)
SINGLE_LOOSE_CONF = 70  # the looser variant `rebuild_lines --report` also prints (any Arabic word, differs)

# The dotless skeleton (rasm): letters that differ only in their dots share a form.
_RASM = str.maketrans(
    {
        "ب": "ٮ",
        "ت": "ٮ",
        "ث": "ٮ",
        "ن": "ٮ",
        "ي": "ٮ",
        "ى": "ٮ",
        "ئ": "ٮ",
        "ج": "ح",
        "خ": "ح",
        "ذ": "د",
        "ز": "ر",
        "ش": "س",
        "ض": "ص",
        "ظ": "ط",
        "غ": "ع",
        "ف": "ڡ",
        "ق": "ڡ",
        "ة": "ه",
        "ؤ": "و",
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ء": None,
        "ـ": None,
    }
)
_NOT_ARABIC_LETTER = re.compile(r"[^ء-يٱ]")

# ---------------------------------------------------------------- words only the second model read

RUN_DUPLICATE_WINDOW = 12  # a run whose words appear within this many primary words is a duplicate ...
RUN_DUPLICATE_SHARE = 0.6  # ... when at least this share of them do
RUN_HEAD_MAX_WORDS = 4  # a run this short at the start of a body region is a running head
RUN_FUZZY_MIN = 75  # a run's word finds a Tesseract word at this rapidfuzz ratio (lenient forms)
RUN_LINE_OVERLAP = 0.3  # a Tesseract line lies on a built line when it covers this share of its height
SUPPORT_MIN = 0.7  # a run Tesseract supports this much enters the text; any other is a suggestion

# ---------------------------------------------------------------- the looped prefix

LOOP_MIN_UNIT = 8  # as `core.arabic.truncate_repetition`
LOOP_MAX_UNIT = 400
PREFIX_MIN_WORDS = 3  # a shorter clean start is no second reading


def lenient(text: str | None) -> str:
    """The lenient form of a token or text (no diacritics, folded letters, Western digits, no marks)."""
    return normalize(str(text or ""), "lenient")


def skeleton(word: str | None) -> str:
    """The dotless skeleton of a word: tashkeel and anything but Arabic letters removed, then ب ت ث ن ي ى ئ
    → ٮ, ج خ → ح, ذ → د, ز → ر, ش → س, ض → ص, ظ → ط, غ → ع, ف ق → ڡ, ة → ه, ؤ → و, أ إ آ ٱ → ا; ء and ـ
    dropped. Tesseract's one-letter misreads are mostly dots: a different skeleton is a real difference."""
    return _NOT_ARABIC_LETTER.sub("", strip_tashkeel(str(word or ""))).translate(_RASM)


def arabic_word(text: str | None) -> bool:
    """A token with an Arabic letter and no Latin one."""
    value = str(text or "")
    return bool(_ARABIC.search(value)) and not _LATIN.search(value)


def foreign_chars(text: str | None) -> list[str]:
    """The characters that make a token `script`: letters of another script (Greek, Cyrillic, Hebrew, kana,
    CJK, Hangul), stray symbols (`STRANGE_SYMBOLS`) and, in a token holding Arabic letters, Latin ones.
    Distinct, in order; empty for an ordinary token."""
    value = str(text or "")
    mixed = bool(_ARABIC.search(value)) and bool(_LATIN.search(value))
    out: list[str] = []
    for ch in value:
        if _FOREIGN_LETTER.match(ch) or ch in STRANGE_SYMBOLS or (mixed and _LATIN.match(ch)):
            if ch not in out:
                out.append(ch)
    return out


def is_punctuation(text: str | None) -> bool:
    """A token without a letter or a digit."""
    return not any(ch.isalnum() for ch in str(text or ""))


def has_digit(text: str | None) -> bool:
    """A token holding a digit of any script (a number, «1964م», «ص١٧٩»)."""
    return is_digit_token(str(text or "")) or any(ch.isdigit() for ch in str(text or ""))


def is_lone_letter(text: str | None) -> bool:
    """A lone letter that may be a digit Qari wrote as a letter («آ» for «١», «ه» for «٥», D51)."""
    return bool(_LETTER_DIGIT.match(strip_tashkeel(str(text or ""))))


def _digits(text: str | None) -> str:
    return _NOT_DIGIT.sub("", to_western_digits(str(text or "")))


# ---------------------------------------------------------------- readings


def split_pieces(tokens: list[str]) -> list[tuple[str, int]]:
    """The tokens with punctuation runs split off their words: `[(piece, index of its token)]`."""
    out: list[tuple[str, int]] = []
    for i, token in enumerate(tokens):
        out.extend((part, i) for part in _SPLIT.split(str(token or "")) if part)
    return out


def _piece_key(piece: str) -> str:
    """Lenient for a word; a mark equals only the same mark (`"\\x00" + mark`)."""
    return norm_token(piece) or ("\x00" + piece)


@dataclass
class PieceAlignment:
    """The two readings' pieces (`split_pieces`) and their pairs (`align_tokens`, `_piece_key`)."""

    primary: list[tuple[str, int]]
    secondary: list[tuple[str, int]]
    pairs: list[tuple[int | None, int | None]]


def align_pieces(primary: list[str], secondary: list[str]) -> PieceAlignment:
    """Align the pieces of two readings of one region (§4.1)."""
    pp, sp = split_pieces(primary), split_pieces(secondary)
    pairs = align_tokens([p for p, _ in pp], [s for s, _ in sp], key=_piece_key) if pp and sp else []
    return PieceAlignment(pp, sp, pairs)


def second_readings(primary: list[str], secondary: list[str]) -> list[str | None]:
    """Qari v0.2's reading of each primary token (None where it has no counterpart).

    The pieces of both readings are aligned (`align_pieces`); a primary token's reading is its pieces'
    counterparts glued back in its own shape: a word piece without one adds nothing, a mark without one
    stays the primary's (so the reading can replace the token without losing its punctuation). None
    when none of its word pieces has a counterpart (a mark alone is no reading of a word), or for a
    mark, none of its pieces. So «القرآن.» against «القرآن .» reads «القرآن.», «الكتاب،» against
    «الكتب.» reads «الكتب،», «الكتاب،» against «،» (v0.2 skipped the word, kept its comma) has no
    reading, and «،» against «.» has none either (marks only equal marks; punctuation is never flagged).
    """
    if not secondary:
        return [None] * len(primary)
    aligned = align_pieces(primary, secondary)
    per_token: dict[int, list[tuple[int, int | None]]] = {}
    for a, b in aligned.pairs:
        if a is not None:
            per_token.setdefault(aligned.primary[a][1], []).append((a, b))
    out: list[str | None] = []
    for i in range(len(primary)):
        found = per_token.get(i) or []
        words = [(a, b) for a, b in found if norm_token(aligned.primary[a][0])]
        if not any(b is not None for _, b in words or found):
            out.append(None)
            continue
        parts = []
        for a, b in found:
            piece = aligned.primary[a][0]
            if b is not None:
                parts.append(aligned.secondary[b][0])
            elif not norm_token(piece):  # a mark the other model did not write: the token keeps it
                parts.append(piece)
        out.append("".join(parts))
    return out


def last_read(readings: list[str | None]) -> int:
    """Index of the last primary token with a second reading (−1 when none): with a looped prefix as the
    second reading, the tokens up to it are two-reader, the rest one-reader (D73)."""
    return max((i for i, value in enumerate(readings) if value is not None), default=-1)


# ---------------------------------------------------------------- the policy


def classify(
    p: str,
    s: str | None,
    tess: str | None,
    tconf: float | None,
    boxed: bool,
    two_readers: bool | None,
    today: bool = False,
) -> list[str]:
    """Why token `p` is doubtful (D71): a list of reasons, empty when it is sure. The first match wins.

    `s` is Qari v0.2's reading (`second_readings`), `tess` Tesseract's word where it differs from `p`
    (None when it read the same word or none), `tconf` that word's confidence, `boxed` whether a
    Tesseract word was read there, `two_readers` whether the region had a second model reading for
    this token (False: one model read it, `single` applies; None: no model reading to compare, the
    text layer or Tesseract's own text). `today` is the rule before 7b for a lone letter that may be a
    digit (D51 left as it was): low when the whitespace alignment found no match or another reading.

    | case | result |
    | foreign letters, a stray symbol, Arabic and Latin in one token (`foreign_chars`) | script |
    | punctuation only | sure |
    | a Western number that v0.2 and Tesseract (its word, else `p` itself when boxed) read alike | sure |
    | any other number | number |
    | a lone letter that may be a digit | `today`: alone / disagree, else sure |
    | a one-reader region | single when Tesseract's Arabic word at conf ≥ 85 has another skeleton |
    | v0.2 has no counterpart | sure when Tesseract read the same word, else alone |
    | v0.3 ≠ v0.2 (lenient) | disagree |
    """
    text = str(p or "")
    if foreign_chars(text):
        return [SCRIPT]
    if is_punctuation(text):
        return []
    if has_digit(text):
        if not _ARABIC_INDIC.search(text):
            value = _digits(text)
            second = _digits(s) if s else None
            third = _digits(tess) if tess else (value if boxed else None)
            if value and second == value and third == value:
                return []
        return [NUMBER]
    if is_lone_letter(text):
        if not today:
            return []
        return [ALONE] if s is None else [DISAGREE]
    if two_readers is None:
        return []
    if not two_readers:
        return [SINGLE] if single_flag(text, tess, tconf) else []
    if s is None:
        return [] if boxed and not tess else [ALONE]
    return [DISAGREE] if lenient(text) != lenient(s) else []


def single_flag(p: str, tess: str | None, tconf: float | None, min_conf: float = SINGLE_MIN_CONF) -> bool:
    """A one-reader word that Tesseract reads otherwise, confidently: its word is Arabic, at conf ≥
    `min_conf`, and of another dotless skeleton (`skeleton`) than the Arabic word `p`."""
    if not tess or not arabic_word(tess) or not arabic_word(p) or tconf is None or tconf < min_conf:
        return False
    return lenient(tess) != lenient(p) and skeleton(tess) != skeleton(p)


def loose_single_flag(p: str, tess: str | None, tconf: float | None) -> bool:
    """The looser variant measured in §2.4 (65 % caught): any Arabic Tesseract word at conf ≥ 70 that
    differs leniently. Reported by `rebuild_lines --report`, not used to flag."""
    if not tess or not arabic_word(tess) or not arabic_word(p) or tconf is None or tconf < SINGLE_LOOSE_CONF:
        return False
    return lenient(tess) != lenient(p)


# the numbers pass's lone letter (`ocr.numbers.LETTER_TOKEN`), which also takes quotes
_D51_LETTER = re.compile(r"^([(\[«“\"]?)([اهع]ـ?)([)\]»”\"]?[.،:؛]?)$")


def vote_reading(token: dict) -> str | None:
    """The D26 vote: Qari v0.2's reading (`alt`) when Tesseract backs it against the text's (`t`), else None.

    `lenient(tess) == lenient(alt) != lenient(t)`. Never for a number (`digit`, or any digit in it), a
    number Kraken read (`src == "kraken"`) or a lone letter that may be a digit (D51): the numbers pass
    decides those; never for a reading that is punctuation only (no word to put in the text). A token
    already resolved or voted is left alone.
    """
    t, alt, tess = str(token.get("t") or ""), token.get("alt"), token.get("tess")
    if token.get("res") or token.get("pick") or not alt or not tess or not lenient(alt):
        return None
    if (
        token.get("digit")
        or token.get("src") == "kraken"
        or has_digit(t)
        or NUMBER in (token.get("why") or [])
    ):
        return None
    if is_lone_letter(t) or _D51_LETTER.match(t):
        return None
    if lenient(tess) == lenient(alt) != lenient(t):
        return str(alt)
    return None


# ---------------------------------------------------------------- words only the second model read


@dataclass
class Run:
    """A run of Qari v0.2 pieces with no Qari v0.3 counterpart, after `at` primary tokens (0: before the
    first). `pieces` as `split_pieces` gave them (with their secondary token index); `words` the pieces
    glued back per secondary token, the tokens a supported run adds to the text."""

    at: int
    pieces: list[tuple[str, int]]
    drop: str = ""
    support: float = 0.0
    words: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(self.words)

    @property
    def word_pieces(self) -> list[str]:
        """The pieces with two letters or more (`run_word`)."""
        return [piece for piece, _ in self.pieces if run_word(piece)]


def run_word(piece: str) -> bool:
    """A piece of a run that is a word: two letters or more (`alignment.is_word`), the tatweel not
    counted («ــ» is a dash)."""
    return is_word(str(piece or "").replace("ـ", ""))


def _glued(pieces: list[tuple[str, int]]) -> list[str]:
    """Consecutive pieces of one secondary token glued back into a word."""
    out: list[str] = []
    last = None
    for piece, source in pieces:
        if out and source == last:
            out[-1] += piece
        else:
            out.append(piece)
        last = source
    return out


def secondary_only_runs(primary: list[str], secondary: list[str], footnote: bool = False) -> list[Run]:
    """The runs of Qari v0.2 pieces that Qari v0.3 has no counterpart for, each with its `drop` reason.

    A run holds at least one word (`is_word`); it is anchored after the primary token its last aligned
    piece belongs to. `drop_reason` marks the false ones; `footnote` is True for a footnote region
    (a short run at its start is no running head).
    """
    if not primary or not secondary:
        return []
    aligned = align_pieces(primary, secondary)
    runs: list[Run] = []
    current: list[tuple[str, int]] = []
    anchor = 0

    def flush() -> None:
        if current and any(run_word(piece) for piece, _ in current):
            run = Run(at=anchor, pieces=list(current), words=_glued(current))
            run.drop = drop_reason(primary, run, footnote)
            runs.append(run)

    for a, b in aligned.pairs:
        if a is None and b is not None:
            current.append(aligned.secondary[b])
            continue
        flush()
        current = []
        if a is not None:
            anchor = aligned.primary[a][1] + 1
    flush()
    return runs


def drop_reason(primary: list[str], run: Run, footnote: bool = False) -> str:
    """Why a run is no missing text ('' when it may be):

    - `numbers`: every word of it holds a digit («٢٣٤هـ», «ص١١ه»: the numbers pass reads numbers);
    - `duplicate`: 60 % of its words appear within ±12 primary words (the other model misplaced them);
    - `split`: one word that begins or ends an adjacent primary token («هير» of «هيرودوت»);
    - `head`: at most 4 words before the first token of a body region (a running head).
    """
    words = run.word_pieces
    spelled = [word for word in run.words if any(ch.isalnum() for ch in word)]
    if not words or all(any(ch.isdigit() for ch in word) for word in spelled):
        return "numbers"
    at = run.at
    window = [lenient(t) for t in primary[max(0, at - RUN_DUPLICATE_WINDOW) : at + RUN_DUPLICATE_WINDOW]]
    if sum(1 for w in words if lenient(w) in window) >= RUN_DUPLICATE_SHARE * len(words):
        return "duplicate"
    if len(words) == 1:
        lw = lenient(words[0])
        for t in primary[max(0, at - 1) : at + 1]:
            lt = lenient(t)
            if lw and lt and lw != lt and (lt.startswith(lw) or lt.endswith(lw)):
                return "split"
    if at == 0 and len(words) <= RUN_HEAD_MAX_WORDS and not footnote:
        return "head"
    return ""


def _supported(words: list[str], pool: list[str]) -> float:
    """Share of `words` that find a word of `pool` (rapidfuzz ratio ≥ `RUN_FUZZY_MIN`, lenient), each
    pool word used once."""
    if not words:
        return 0.0
    left = list(pool)
    hit = 0
    for word in words:
        lw = lenient(word)
        best = max(((fuzz.ratio(lw, lenient(t)), k) for k, t in enumerate(left)), default=(0, None))
        if best[0] >= RUN_FUZZY_MIN:
            hit += 1
            left.pop(best[1])
    return hit / len(words)


def run_support(run: Run, built: list[dict], tess_lines: list[dict]) -> float:
    """Tesseract's support for a run: the share of its words (`word_pieces`) found among the Tesseract words
    of the anchor's built line ±1 (`built`: `alignment.build_lines` without the run), after removing the
    words the primary already accounts for there.

    The anchor's lines are those of the primary tokens around it (at − 1 and at); a Tesseract line lies on
    a built line when it covers `RUN_LINE_OVERLAP` of its height.
    """
    line_of: list[int] = []
    for k, line in enumerate(built):
        line_of.extend([k] * len(line.get("tokens") or []))
    near: set[int] = set()
    for index in (run.at - 1, run.at):
        if 0 <= index < len(line_of):
            near.add(line_of[index])
    near |= {k + 1 for k in near} | {k - 1 for k in near}
    pool: list[str] = []
    primary: list[str] = []
    for k in sorted(near):
        if not 0 <= k < len(built):
            continue
        box = built[k].get("bbox")
        if box:
            for line in tess_lines or []:
                tb = line.get("bbox")
                if tb and min(tb[3], box[3]) - max(tb[1], box[1]) > RUN_LINE_OVERLAP * (box[3] - box[1]):
                    pool.extend(str(w.get("text") or "") for w in line.get("words") or [])
        primary.extend(str(t.get("t") or "") for t in built[k].get("tokens") or [])
    for token in primary:
        key = lenient(token)
        if not key:
            continue
        for n, word in enumerate(pool):
            if lenient(word) == key:
                pool.pop(n)
                break
    return _supported(run.word_pieces, pool)


def merge_runs(primary: list[str], runs: list[Run]) -> tuple[list[str], dict[int, int]]:
    """The primary tokens with the `runs` words inserted at their anchors, and `{token index: run number}`
    (the run's position in `runs`) for the inserted tokens."""
    by_anchor: dict[int, list[int]] = {}
    for number, run in enumerate(runs):
        by_anchor.setdefault(run.at, []).append(number)
    out: list[str] = []
    inserted: dict[int, int] = {}
    for i in range(len(primary) + 1):
        for number in by_anchor.get(i, []):
            for word in runs[number].words:
                inserted[len(out)] = number
                out.append(word)
        if i < len(primary):
            out.append(primary[i])
    return out, inserted


# ---------------------------------------------------------------- the looped prefix


def _drop_repeats(text: str, key: str, min_repeats: int) -> tuple[str, bool]:
    """`text` without every copy of a unit repeated at its end (found on `key`, of the same length)."""
    cut = False
    while True:
        found = False
        for unit in range(LOOP_MIN_UNIT, min(LOOP_MAX_UNIT, len(key) // min_repeats) + 1):
            tail = key[-unit:]
            if all(key[-(k + 1) * unit : -k * unit or None] == tail for k in range(1, min_repeats)):
                copies = 1
                while len(key) >= (copies + 1) * unit and key[-(copies + 1) * unit : -copies * unit] == tail:
                    copies += 1
                text, key = text[: -copies * unit], key[: -copies * unit]
                cut = found = True
                break
        if not found:
            return text, cut


def looped_prefix(raw: str, hit_cap: bool = False) -> str:
    """The clean start of a looped model output (D73): its text before the repeated unit, markup stripped.

    Every copy of the unit repeated at the end goes (`core.arabic.truncate_repetition` keeps one), and
    with `hit_cap` the incrementing loops too (digits masked); a word the cut went through goes as well.
    An output cut by the token cap without a repeat loses its last word. '' when fewer than
    `PREFIX_MIN_WORDS` words are left.
    """
    text = str(raw or "")
    text, cut = _drop_repeats(text, text, 3)
    if hit_cap:
        text, masked = _drop_repeats(text, re.sub(r"[0-9٠-٩۰-۹]", "0", text), 4)
        cut = cut or masked
    broken = bool(text) and not text[-1].isspace() and (cut or hit_cap)
    plain = strip_markup(text)
    words = plain.split()
    if broken and words:
        plain = plain[: plain.rstrip().rfind(words[-1])].rstrip()
    return plain if len(plain.split()) >= PREFIX_MIN_WORDS else ""


# ---------------------------------------------------------------- helpers for the popover and reports


def reason_chars(token: dict) -> list[str]:
    """The foreign characters of a `script` token (for the reason line «حروف ليست عربية («и»، «л»)»)."""
    return foreign_chars(token.get("t"))


# ---------------------------------------------------------------- years in words (§4.8)

# Arabic cardinals in their lenient forms (ة → ه, ى / ئ → ي), old spellings included (ماية، مايتين،
# ثلثماية، الف): the year printed in words after its digits («سنة ( ٢٤٢ ) اثنتين واربعين ومايتين»).
_UNITS = {
    "واحد": 1, "واحده": 1, "احد": 1, "احدي": 1, "اثنين": 2, "اثنتين": 2, "اثنان": 2, "اثنتان": 2,
    "اثني": 2, "اثنتي": 2, "ثلاث": 3, "ثلاثه": 3, "ثلث": 3, "ثلثه": 3, "اربع": 4, "اربعه": 4, "خمس": 5,
    "خمسه": 5, "ست": 6, "سته": 6, "سبع": 7, "سبعه": 7, "ثمان": 8, "ثماني": 8, "ثمانيه": 8, "تسع": 9,
    "تسعه": 9,
}  # fmt: skip
_TENS = {
    "عشر": 10, "عشره": 10, "عشرين": 20, "عشرون": 20, "ثلاثين": 30, "ثلثين": 30, "ثلاثون": 30,
    "ثلثون": 30, "اربعين": 40, "اربعون": 40, "خمسين": 50, "خمسون": 50, "ستين": 60, "ستون": 60,
    "سبعين": 70, "سبعون": 70, "ثمانين": 80, "ثمانون": 80, "تسعين": 90, "تسعون": 90,
}  # fmt: skip
_HUNDRED_FORMS = ("مايه", "ماءه", "ميه", "مئه")
_HUNDREDS = {
    **dict.fromkeys(_HUNDRED_FORMS, 100),
    **dict.fromkeys(("مايتين", "ماءتين", "مايتان", "ميتين", "مئتين"), 200),
}
_THOUSANDS = {"الف": 1000, "الفين": 2000, "الفان": 2000}
_THOUSANDS_OF = ("الاف", "آلاف")  # «ثلاثة آلاف»: the units before it multiply
# what may stand between the digits and the words: brackets, marks, the era («م», «هـ», «ه»)
_ERA = frozenset({"م", "ه"})  # «هـ» without its tatweel
YEAR_WORDS_MAX = 8  # number words looked at after the digits
YEAR_GAP_MAX = 3  # tokens (brackets, marks, the era) skipped between the digits and the words
YEAR_MIN_VALUE = 3
YEAR_LABEL = "من الحروف"
YEAR_SOURCE = "words"
_DIGIT_RUN = re.compile(r"[0-9٠-٩۰-۹]+")
_ARABIC_INDIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
_NUMBER_TOKEN = re.compile(r"^[(\[«“\"]*[0-9٠-٩۰-۹]+(?:ـ?(?:م|هـ|ه))?[)\]»”\".،:؛]*$")


def number_word(word: str) -> tuple[str, int] | None:
    """`(kind, value)` of one Arabic number word (u units, t tens, h hundreds, k thousands, K «آلاف»),
    «و» before it allowed; None for any other word."""
    value = lenient(word).replace("ـ", "")
    for core in (value, value[1:] if value.startswith("و") else None):
        if not core:
            continue
        if core in _UNITS:
            return "u", _UNITS[core]
        if core in _TENS:
            return "t", _TENS[core]
        if core in _HUNDREDS:
            return "h", _HUNDREDS[core]
        if core in _THOUSANDS:
            return "k", _THOUSANDS[core]
        if core in _THOUSANDS_OF:
            return "K", 1000
        for unit, amount in _UNITS.items():  # ثلثماية، اربعمائة، ثلاثمئة
            if any(core in (unit + form, unit.removesuffix("ه") + form) for form in _HUNDRED_FORMS):
                return "h", amount * 100
    return None


def number_words(words: list[str]) -> tuple[int, int] | None:
    """The value of the number words at the start of `words` and how many they take, joined by «و»
    («اثنتين واربعين ومايتين» = 242, «ثلاث عشرة» = 13, «ثلاثة آلاف» = 3000); None when none."""
    total = count = 0
    for word in words[:YEAR_WORDS_MAX]:
        found = number_word(word)
        if found is None:
            break
        kind, value = found
        total = (total or 1) * 1000 if kind == "K" else total + value
        count += 1
    return (total, count) if count else None


def _between(token: str) -> bool:
    """A token that may stand between a year's digits and its words (a bracket, a mark, the era)."""
    text = str(token or "").strip("()[]«»“”\"'.,،:؛-–—")
    return not text or text.replace("ـ", "") in _ERA


def _in_script(number: int, like: str) -> str:
    """`number` in the digits `like` uses (Arabic-Indic or Western)."""
    text = str(number)
    return text.translate(_ARABIC_INDIC_DIGITS) if _ARABIC_INDIC.search(like) else text


def year_check(lines: list[list[dict]]) -> int:
    """Compare every number followed by its value in words (§4.8) with those words, in place.

    `lines` are the tokens of consecutive lines of one region (the words may continue on the next
    line). A number token (digits, brackets, the era) followed, after at most `YEAR_GAP_MAX` brackets,
    marks or «م / هـ», by number words worth `YEAR_MIN_VALUE` or more (words whose value is only the
    end of the digits' stopped short and are no evidence): when the values agree the open
    token is settled (`res = "words"`, its `year` reason and suggestion gone); when they differ it gets
    `sug = {t, src: "words", label: "من الحروف", words}` (the year in its digits' script) and the
    reason `year`, and stays open. Resolved tokens are left alone. Returns the number of tokens changed.
    """
    flat = [(k, i) for k, tokens in enumerate(lines) for i in range(len(tokens))]
    texts = [str(lines[k][i].get("t") or "") for k, i in flat]
    changed = 0
    for n, (k, i) in enumerate(flat):
        token = lines[k][i]
        text = texts[n]
        if token.get("res") or not _NUMBER_TOKEN.match(strip_tashkeel(text)):
            continue
        j = n + 1
        while j < len(texts) and j - n <= YEAR_GAP_MAX and _between(texts[j]):
            j += 1
        parsed = number_words(texts[j : j + YEAR_WORDS_MAX])
        if not parsed or parsed[0] < YEAR_MIN_VALUE:
            continue
        value, count = parsed
        digits = int(_digits(_DIGIT_RUN.search(text).group()))
        if value < digits and str(digits).endswith(str(value)):
            continue  # the words stop short («خمسين» of «خمسين ومايتين» read apart): no evidence
        before = dict(token)
        why = [reason for reason in token.get("why") or [] if reason != YEAR]
        if digits == value:
            token.pop("sug", None)
            if token.get("conf") == "low":
                token["res"] = YEAR_SOURCE
        else:
            token["sug"] = {
                "t": _DIGIT_RUN.sub(_in_script(value, text), text, count=1),
                "src": YEAR_SOURCE,
                "label": YEAR_LABEL,
                "words": " ".join(texts[j : j + count]),
            }
            why.append(YEAR)
            token["conf"] = "low"
        if why:
            token["why"] = why
        else:
            token.pop("why", None)
        changed += token != before
    return changed
