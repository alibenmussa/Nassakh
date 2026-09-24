"""Token alignment and line building (D12, D16, D17).

Qari returns the page as running text without reliable line breaks; Tesseract returns words with
boxes grouped into visual lines. `align_tokens` pairs two token sequences after lenient
normalisation; `build_lines` anchors the chosen engine's tokens to Tesseract's words to give each
token a line and a box, and attaches the other engine's reading as `alt` where it differs.
"""

from __future__ import annotations

import difflib
from collections import Counter

from rapidfuzz import fuzz

from core.arabic import is_digit_token, normalize

Pair = tuple[int | None, int | None]

# Minimum rapidfuzz ratio (0-100) for two differently normalised tokens to count as the same word.
FUZZY_MIN_RATIO = 60


def norm_token(token: str) -> str:
    """Lenient normalisation of one token (no diacritics, folded letters, Western digits, no punctuation)."""
    return normalize(token, "lenient")


def word_f1(a: str | list[str], b: str | list[str]) -> float:
    """Order-insensitive word overlap (multiset F1) of two texts or token lists, lenient normalisation."""
    ta = a.split() if isinstance(a, str) else list(a)
    tb = b.split() if isinstance(b, str) else list(b)
    ca = Counter(norm_token(t) for t in ta if norm_token(t))
    cb = Counter(norm_token(t) for t in tb if norm_token(t))
    common = sum((ca & cb).values())
    total = sum(ca.values()) + sum(cb.values())
    return (2 * common / total) if total else 0.0


def align_tokens(a: list[str], b: list[str], min_ratio: int = FUZZY_MIN_RATIO) -> list[Pair]:
    """Pair the tokens of `a` with those of `b`, in order; `(i, j)` pairs where either side may be None.

    Exact matches (after lenient normalisation) come from `difflib.SequenceMatcher`; inside replaced
    blocks tokens are paired by a monotone best-similarity alignment (rapidfuzz ratio ≥ `min_ratio`).
    A one-for-one replacement is always paired (it is the typical misread word).
    """
    na = [norm_token(t) for t in a]
    nb = [norm_token(t) for t in b]
    pairs: list[Pair] = []
    matcher = difflib.SequenceMatcher(None, na, nb, autojunk=False)
    for tag, i0, i1, j0, j1 in matcher.get_opcodes():
        if tag == "equal":
            pairs.extend(zip(range(i0, i1), range(j0, j1), strict=True))
        elif tag == "delete":
            pairs.extend((i, None) for i in range(i0, i1))
        elif tag == "insert":
            pairs.extend((None, j) for j in range(j0, j1))
        else:
            pairs.extend(_pair_block(na, nb, i0, i1, j0, j1, min_ratio))
    return pairs


def _pair_block(
    na: list[str], nb: list[str], i0: int, i1: int, j0: int, j1: int, min_ratio: int
) -> list[Pair]:
    """Monotone pairing of a replaced block by summed similarity (small DP; blocks are short)."""
    n, m = i1 - i0, j1 - j0
    if n == 1 and m == 1:
        return [(i0, j0)]
    sim = [[int(round(fuzz.ratio(na[i0 + x], nb[j0 + y]))) for y in range(m)] for x in range(n)]
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for x in range(1, n + 1):
        for y in range(1, m + 1):
            best = max(dp[x - 1][y], dp[x][y - 1])
            s = sim[x - 1][y - 1]
            if s >= min_ratio:
                best = max(best, dp[x - 1][y - 1] + s)
            dp[x][y] = best
    out: list[Pair] = []
    x, y = n, m
    while x > 0 or y > 0:
        if (
            x > 0
            and y > 0
            and sim[x - 1][y - 1] >= min_ratio
            and dp[x][y] == dp[x - 1][y - 1] + sim[x - 1][y - 1]
        ):
            out.append((i0 + x - 1, j0 + y - 1))
            x, y = x - 1, y - 1
        elif x > 0 and (y == 0 or dp[x][y] == dp[x - 1][y]):
            out.append((i0 + x - 1, None))
            x -= 1
        else:
            out.append((None, j0 + y - 1))
            y -= 1
    out.reverse()
    return out


def build_lines(primary_text: str, secondary_text: str | None, tesseract_lines: list[dict]) -> list[dict]:
    """Split the chosen text into visual lines with per-token confidence.

    Each primary token is anchored to the Tesseract word it aligns with and inherits that word's
    line and box; unmatched tokens inherit the previous token's line (leading ones take the first
    anchored line). Without Tesseract lines the primary text's own line breaks are used. `alt` is the
    secondary engine's token when its normalised form differs; `conf` is `low` when an alt differs,
    when the secondary has no counterpart for the token, or when the token is a number (D17).

    Returns `[{order, bbox, text, tokens, n_low, n_anchored, confidence}]` with tokens
    `{"t", "alt", "conf", "digit", "bbox", "tess"}` (`tess` = Tesseract's word when it differs).
    """
    p_tokens = primary_text.split()
    if not p_tokens:
        return []
    n = len(p_tokens)

    alts: list[str | None] = [None] * n
    disagree: list[bool] = [False] * n
    if secondary_text and secondary_text.split():
        s_tokens = secondary_text.split()
        for i, j in align_tokens(p_tokens, s_tokens):
            if i is None:
                continue
            if j is None:
                disagree[i] = True
            elif norm_token(p_tokens[i]) != norm_token(s_tokens[j]):
                alts[i] = s_tokens[j]
                disagree[i] = True

    line_of: list[int | None] = [None] * n
    bbox_of: list[list | None] = [None] * n
    tess_of: list[str | None] = [None] * n  # Tesseract's reading when it differs from the primary
    words = [(k, w) for k, line in enumerate(tesseract_lines or []) for w in line.get("words", [])]
    if words:
        for i, j in align_tokens(p_tokens, [w["text"] for _, w in words]):
            if i is not None and j is not None:
                line_of[i] = words[j][0]
                bbox_of[i] = words[j][1].get("bbox")
                word = str(words[j][1].get("text") or "")
                if word and norm_token(word) != norm_token(p_tokens[i]):
                    tess_of[i] = word
    anchored_any = any(k is not None for k in line_of)
    if anchored_any:
        current = next(k for k in line_of if k is not None)
        for i in range(n):
            if line_of[i] is None:
                line_of[i] = current
            else:
                current = line_of[i]
        line_bboxes = {k: line.get("bbox") for k, line in enumerate(tesseract_lines)}
    else:
        # No geometry, or nothing matched it: the text's own line breaks are the best we have.
        idx = 0
        for k, raw_line in enumerate(primary_text.split("\n")):
            for _ in raw_line.split():
                line_of[idx] = k
                idx += 1
        line_bboxes = {}

    grouped: dict[int, list[int]] = {}
    for i in range(n):
        grouped.setdefault(line_of[i], []).append(i)

    lines: list[dict] = []
    for order, key in enumerate(sorted(grouped)):
        indices = grouped[key]
        tokens = []
        for i in indices:
            tok = p_tokens[i]
            digit = is_digit_token(tok)
            low = digit or disagree[i]
            tokens.append(
                {
                    "t": tok,
                    "alt": alts[i],
                    "conf": "low" if low else "high",
                    "digit": digit,
                    "bbox": bbox_of[i],
                    "tess": tess_of[i],
                }
            )
        n_low = sum(1 for t in tokens if t["conf"] == "low")
        anchored = sum(1 for i in indices if bbox_of[i] is not None)
        bbox = line_bboxes.get(key)
        if bbox is None and anchored:
            boxes = [bbox_of[i] for i in indices if bbox_of[i] is not None]
            bbox = [
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            ]
        lines.append(
            {
                "order": order,
                "bbox": bbox,
                "text": " ".join(t["t"] for t in tokens),
                "tokens": tokens,
                "n_low": n_low,
                "n_anchored": anchored,
                "confidence": round(1.0 - n_low / len(tokens), 3) if tokens else 1.0,
            }
        )
    return lines
