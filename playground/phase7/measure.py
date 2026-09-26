"""Phase 7 probe (read-only): which OCR flags were useful on reviewed pages.

For every approved page (reviewed / assembled) the lines as the reviewer first saw them are rebuilt from
the review history (`LineRevision.before` of the first live revision of each line; untouched lines are as
stored), then aligned word by word with the lines as approved. A token is `changed` when the reviewer
changed or removed it; words the reviewer added are `added` (missed by OCR).
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict

sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
import django  # noqa: E402

django.setup()

from books.models import Page  # noqa: E402
from core.arabic import is_digit_token, normalize, strip_tashkeel  # noqa: E402
from review.models import LineRevision  # noqa: E402

LINE_ACTIONS = {"resolve", "edit", "merge", "drop_word", "role", "insert", "delete"}
_LATIN = re.compile(r"[A-Za-z]")
_ARABIC = re.compile(r"[ء-يٮ-ۓݐ-ݿﭐ-﷿ﹰ-ﻼ]")
_LETTER_DIGIT = re.compile(r"^[(\[«]?[اأإآهع]{1,2}ـ?[)\]»]?[.،:؛]?$")


def category(t: str) -> str:
    t = t or ""
    if not any(ch.isalnum() for ch in t):
        return "punct"
    if is_digit_token(t):
        return "number"
    if any(ch.isdigit() for ch in t):
        return "mixed_digit"
    letters = [ch for ch in t if ch.isalpha()]
    foreign = [ch for ch in letters if not _ARABIC.match(ch) and not _LATIN.match(ch)]
    if foreign:
        return "foreign"
    if letters and all(_LATIN.match(ch) for ch in letters):
        return "latin"
    if _LETTER_DIGIT.match(strip_tashkeel(t)):
        return "letterdigit"
    return "word"


def change_kind(a: str, b: str) -> str:
    """How an original token `a` differs from the approved `b`."""
    if a == b:
        return "same"
    na, nb = unicodedata.normalize("NFKC", a), unicodedata.normalize("NFKC", b)
    if strip_tashkeel(na) == strip_tashkeel(nb):
        return "diacritics"
    la, lb = normalize(a, "lenient"), normalize(b, "lenient")
    if la == lb:
        pa = re.sub(r"[\wً-ٟ]", "", na)
        pb = re.sub(r"[\wً-ٟ]", "", nb)
        return "punct" if pa != pb else "letterfold"
    return "letters"


def reason(tok: dict) -> str:
    if tok.get("src") == "kraken":
        return "kraken"
    if tok.get("digit") or is_digit_token(tok.get("t") or ""):
        return "digit"
    if tok.get("alt"):
        return "alt"
    return "nosec"


def original_lines(page: Page):
    """[(line_id, original tokens | None (inserted), final tokens | None (deleted), is_manual)]."""
    revs = list(
        LineRevision.objects.filter(page=page, undone=False, action__in=LINE_ACTIONS).order_by("created_at", "id")
    )
    first_before: dict[int, dict | None] = {}
    for r in revs:
        snap_id = None
        for snap in (r.before, r.after):
            if isinstance(snap, dict) and snap.get("id"):
                snap_id = snap["id"]
                break
        lid = r.line_id or snap_id
        if lid is None or lid in first_before:
            continue
        first_before[lid] = r.before if r.action != "insert" else None
    out = []
    seen = set()
    for line in page.lines.order_by("order", "id"):
        seen.add(line.pk)
        if line.pk in first_before:
            before = first_before[line.pk]
            orig = None if before is None else before.get("tokens") or []
        else:
            orig = None if line.is_manual else list(line.tokens or [])
        out.append((line.pk, orig, list(line.tokens or []), line.is_manual, line))
    for r in revs:
        if r.action == "delete" and isinstance(r.before, dict):
            lid = r.before.get("id")
            if lid in seen:
                continue
            seen.add(lid)
            before = first_before.get(lid) or r.before
            out.append((lid, before.get("tokens") or [], None, False, None))
    return out


def measure(book_ids, statuses=("reviewed", "assembled"), verbose=False):
    stats = defaultdict(Counter)
    examples = defaultdict(list)
    pages_rows = []
    for page in Page.objects.filter(book_id__in=book_ids, status__in=statuses).order_by("book_id", "number"):
        pr = Counter()
        for lid, orig, final, manual, line in original_lines(page):
            if orig is None:  # inserted line: every word missed by OCR
                n = len([t for t in (final or []) if (t.get("t") or "").strip()])
                stats[page.book_id]["added_inserted_line_words"] += n
                stats[page.book_id]["inserted_lines"] += 1
                pr["added"] += n
                examples["inserted_line"].append((page.book_id, page.number, " ".join(t["t"] for t in final)))
                continue
            ot = [str(t.get("t") or "") for t in orig]
            ft = [str(t.get("t") or "") for t in (final or [])]
            if final is None:
                stats[page.book_id]["deleted_lines"] += 1
                examples["deleted_line"].append((page.book_id, page.number, " ".join(ot)))
            sm = difflib.SequenceMatcher(None, ot, ft, autojunk=False)
            for tag, i0, i1, j0, j1 in sm.get_opcodes():
                if tag == "insert":
                    stats[page.book_id]["added_words"] += j1 - j0
                    pr["added"] += j1 - j0
                    examples["added"].append((page.book_id, page.number, " ".join(ft[j0:j1]), " ".join(ot[max(0,i0-2):i0+2])))
                    continue
                for k in range(i0, i1):
                    tok = orig[k]
                    low = tok.get("conf") == "low"
                    cat = category(ot[k])
                    changed = tag != "equal"
                    new = ""
                    if tag == "replace":
                        # pair by position inside the block
                        off = k - i0
                        new = ft[j0 + off] if j0 + off < j1 and (i1 - i0) == (j1 - j0) else " ".join(ft[j0:j1])
                    kind = change_kind(ot[k], new) if tag == "replace" else ("deleted" if changed else "same")
                    key = ("low" if low else "high", "chg" if changed else "ok")
                    stats[page.book_id][key] += 1
                    stats[page.book_id][(key, cat)] += 1
                    if low:
                        stats[page.book_id][(key, "reason", reason(tok))] += 1
                    if changed:
                        stats[page.book_id][(key, "kind", kind)] += 1
                    pr[key] += 1
                    if changed and not low:
                        examples["missed"].append((page.book_id, page.number, ot[k], new, kind, cat, tok.get("alt"), tok.get("tess")))
                    if low and not changed:
                        examples["false_flag"].append((page.book_id, page.number, ot[k], cat, reason(tok), tok.get("alt"), tok.get("res")))
                    if low and changed:
                        examples["true_flag"].append((page.book_id, page.number, ot[k], new, cat, reason(tok), tok.get("alt"), tok.get("res")))
                if tag == "replace" and (j1 - j0) > (i1 - i0):
                    stats[page.book_id]["added_words"] += (j1 - j0) - (i1 - i0)
        pages_rows.append((page.book_id, page.number, pr[("low", "chg")], pr[("low", "ok")], pr[("high", "chg")], pr["added"]))
    return stats, examples, pages_rows


if __name__ == "__main__":
    ids = [int(x) for x in sys.argv[1].split(",")]
    stats, examples, pages_rows = measure(ids)
    total = Counter()
    for b in sorted(stats):
        s = stats[b]
        total.update(s)
        tp, fp, fn, tn = s[("low", "chg")], s[("low", "ok")], s[("high", "chg")], s[("high", "ok")]
        print(f"book {b}: tokens {tp+fp+fn+tn} flagged {tp+fp} (useful {tp}, precision {tp/max(1,tp+fp):.0%}) "
              f"errors {tp+fn} (flag recall {tp/max(1,tp+fn):.0%}) missed {fn} added_words {s['added_words']} "
              f"inserted_lines {s['inserted_lines']} ({s['added_inserted_line_words']} words) deleted_lines {s['deleted_lines']}")
    s = total
    tp, fp, fn, tn = s[("low", "chg")], s[("low", "ok")], s[("high", "chg")], s[("high", "ok")]
    print(f"ALL: tokens {tp+fp+fn+tn} flagged {tp+fp} useful {tp} precision {tp/max(1,tp+fp):.1%} errors {tp+fn} recall {tp/max(1,tp+fn):.1%} missed {fn} added {s['added_words']} ins_lines {s['inserted_lines']} ({s['added_inserted_line_words']}) del_lines {s['deleted_lines']}")
    print("\nflag outcome by category:")
    cats = sorted({k[1] for k in s if isinstance(k, tuple) and len(k) == 2 and isinstance(k[0], tuple)})
    for cat in cats:
        a, b_, c, d = s[(("low", "chg"), cat)], s[(("low", "ok"), cat)], s[(("high", "chg"), cat)], s[(("high", "ok"), cat)]
        print(f"  {cat:12s} low-changed {a:4d} low-kept {b_:4d} high-changed {c:4d} high-kept {d:5d}  err-rate {(a+c)/max(1,a+b_+c+d):.1%}")
    print("\nflag reason:")
    for r in ("digit", "kraken", "alt", "nosec"):
        a, b_ = s[(("low", "chg"), "reason", r)], s[(("low", "ok"), "reason", r)]
        print(f"  {r:8s} useful {a:4d} not {b_:4d} precision {a/max(1,a+b_):.0%}")
    print("\nkind of change (low / high):")
    for kind in ("diacritics", "letterfold", "punct", "letters", "deleted"):
        print(f"  {kind:10s} low {s[(('low','chg'),'kind',kind)]:4d} high {s[(('high','chg'),'kind',kind)]:4d}")
    out = "/private/tmp/claude-501/-Users-alibenmussa-PycharmProjects-me-Nassakh/5e011468-4f0f-4097-b8c6-1a1084b7f4be/scratchpad/phase7/examples_%s.json" % sys.argv[1].replace(",", "_")
    with open(out, "w") as fh:
        json.dump({k: v for k, v in examples.items()}, fh, ensure_ascii=False, indent=0)
    with open(out.replace("examples_", "pages_"), "w") as fh:
        json.dump(pages_rows, fh)
    print("examples ->", out)
