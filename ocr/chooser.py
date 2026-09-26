"""Word-chooser hook for uncertain words (D26), and the vote that activates it (D71).

While a page is finalised, a chooser may put the best reading of an uncertain word among its candidates
(primary `t`, secondary `alt`, Tesseract `tess`) into the text. There is no UI of its own.

- `"vote"` (the default): where the two models disagree and Tesseract backs Qari v0.2
  (`ocr.flags.vote_reading`), v0.2's reading goes into the text: `t = alt`, `orig` keeps the old `t`,
  `pick = "vote"`, and `res` stays null, so the word stays open for the reviewer (Enter confirms the
  reading now in the text). Measured on 177 disagreements of 14 books: the reading Tesseract backs is
  right in 76 %, Qari v0.3's alone in 40 %. Never a number (`digit`), a number Kraken read or a lone
  letter that may be a digit (D51): `ocr/test_numbers.py` gives the same tokens with the vote on.
- any other name: `choose_word`, a placeholder for a future classifier that never chooses; a word it
  chose would get `res = "chooser"` and keep `conf = "low"` (uncertain for the eye, no longer open).
- `"none"`: off.

Set by `settings.NASSAKH["WORD_CHOOSER"]` (env `WORD_CHOOSER`, default `"vote"`).
"""

from __future__ import annotations

from django.conf import settings

from . import flags

CHOOSER_OFF = "none"
CHOOSER_VOTE = "vote"
RES_CHOOSER = "chooser"
PICK_VOTE = "vote"


def name() -> str:
    """The configured chooser (`NASSAKH["WORD_CHOOSER"]`), lower case; `"none"` when unset."""
    return str(settings.NASSAKH.get("WORD_CHOOSER", CHOOSER_VOTE) or CHOOSER_OFF).strip().lower()


def enabled() -> bool:
    """True when a word chooser is configured (`NASSAKH["WORD_CHOOSER"]` other than `"none"`)."""
    return name() != CHOOSER_OFF


def candidates(token: dict) -> list[str]:
    """Distinct non-empty readings of a token in the order primary, secondary, Tesseract."""
    out: list[str] = []
    for key in ("t", "alt", "tess"):
        value = token.get(key)
        if isinstance(value, str) and value and value not in out:
            out.append(value)
    return out


def choose_word(token: dict, context: dict) -> str | None:
    """Pick the best reading of an uncertain `token`, or None to leave it to the reviewer.

    `context` holds `page_id`, `region_kind`, `line_index`, `index` (token position), `line`
    (the line's tokens as strings) and `candidates`. Placeholder: always None.
    """
    return None


def vote(token: dict) -> bool:
    """Apply the vote to one token (in place): True when Qari v0.2's reading went into the text.

    `t` becomes `alt`, `orig` keeps the reading it replaced (set once: a resolution later keeps it),
    `pick = "vote"`; `res` stays null and the other keys keep their places.
    """
    reading = flags.vote_reading(token)
    if reading is None:
        return False
    token.setdefault("orig", token.get("t"))
    token["t"] = reading
    token["pick"] = PICK_VOTE
    return True


def apply_chooser(tokens: list[dict], context: dict) -> int:
    """Run the configured chooser on every unresolved low-confidence token; returns how many it changed.

    Mutates `tokens` in place. The vote (`vote`) puts Tesseract's side into the text and leaves the word
    open. Any other chooser runs `choose_word` on tokens with two or more candidates: a returned string
    that is one of them becomes `t` (the primary reading kept in `orig` when it changes), the token gets
    `res = "chooser"` and keeps `conf = "low"`; anything else returned is ignored. Returns 0 when the
    chooser is off.
    """
    chosen_name = name()
    if chosen_name == CHOOSER_OFF:
        return 0
    words = [str(tok.get("t") or "") for tok in tokens]
    chosen = 0
    for index, token in enumerate(tokens):
        if token.get("conf") != "low" or token.get("res"):
            continue
        if chosen_name == CHOOSER_VOTE:
            chosen += vote(token)
            continue
        options = candidates(token)
        if len(options) < 2:
            continue
        pick = choose_word(token, {**context, "index": index, "line": words, "candidates": options})
        if not isinstance(pick, str) or pick not in options:
            continue
        if pick != token.get("t"):
            token.setdefault("orig", token.get("t"))
            token["t"] = pick
        token["res"] = RES_CHOOSER
        chosen += 1
    return chosen
