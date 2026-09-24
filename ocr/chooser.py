"""Word-chooser hook for uncertain words (D26).

A future small classifier may pick the best reading of an uncertain word among its candidates
(primary `t`, secondary `alt`, Tesseract `tess`) while a page is finalised. There is no UI: a
chosen word gets `res = "chooser"` and keeps `conf = "low"`, so it still counts as uncertain for
the reviewer's eyes but no longer as unresolved. `choose_word` is a placeholder that never chooses.

Enabled by `settings.NASSAKH["WORD_CHOOSER"]` (default `"none"` = off).
"""

from __future__ import annotations

from django.conf import settings

CHOOSER_OFF = "none"
RES_CHOOSER = "chooser"


def enabled() -> bool:
    """True when a word chooser is configured (`NASSAKH["WORD_CHOOSER"]` other than `"none"`)."""
    name = str(settings.NASSAKH.get("WORD_CHOOSER", CHOOSER_OFF) or CHOOSER_OFF).strip().lower()
    return name != CHOOSER_OFF


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


def apply_chooser(tokens: list[dict], context: dict) -> int:
    """Run `choose_word` on every unresolved low-confidence token with two or more candidates.

    Mutates `tokens` in place: a returned string that is one of the candidates becomes `t`, the
    token gets `res = "chooser"` and keeps `conf = "low"`; the original primary reading is kept in
    `orig` when it changes. Anything else returned is ignored. Returns the number of chosen tokens.
    Does nothing (returns 0) when the chooser is disabled.
    """
    if not enabled():
        return 0
    words = [str(tok.get("t") or "") for tok in tokens]
    chosen = 0
    for index, token in enumerate(tokens):
        if token.get("conf") != "low" or token.get("res"):
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
