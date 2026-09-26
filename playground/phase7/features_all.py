"""Phase 7 probe (read-only): per-token features on approved pages, to evaluate flag policies offline.

For each approved page: the original tokens (as the reviewer first saw them, `measure.original_lines`),
the outcome (changed by the reviewer or not), and the readings of the three engines re-derived from the
stored runs: the secondary's exact counterpart after splitting punctuation off the words (so «القرآن.»
and «القرآن .» agree), Tesseract's word and its confidence (by box), Kraken's number.
Writes one JSON row per token to features.jsonl.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
import django  # noqa: E402

django.setup()

from books.models import Page  # noqa: E402
from core.arabic import normalize  # noqa: E402
from ocr.alignment import align_tokens, norm_token  # noqa: E402
from ocr.models import OcrRun  # noqa: E402
from ocr.services import _latest_runs, _targets, engine_names, select_text  # noqa: E402
from measure import category, change_kind, original_lines  # noqa: E402

PUNCT = ".،؛:؟!,;?…)]»”(«[“-–—ـ\"'*/"
_SPLIT = re.compile(r"([^\wً-ٰٟـ]+)")


def split_punct(tokens: list[str]) -> list[tuple[str, int]]:
    """Tokens with punctuation runs split off words: [(piece, index of the source token)]."""
    out = []
    for i, tok in enumerate(tokens):
        parts = [p for p in _SPLIT.split(tok) if p]
        for p in parts:
            out.append((p, i))
    return out


def exact_counterparts(primary: list[str], secondary: list[str]) -> list[str | None]:
    """For each primary token, the secondary's reading of the same word (punctuation split off both
    sides, words compared leniently; the secondary pieces re-glued in the primary token's shape)."""
    if not secondary:
        return [None] * len(primary)
    pp = split_punct(primary)
    sp = split_punct(secondary)
    pairs = align_tokens([p for p, _ in pp], [s for s, _ in sp], key=lambda t: norm_token(t) or ("\x00" + t))
    per_primary: dict[int, list[tuple[int, int | None]]] = {}
    for a, b in pairs:
        if a is None:
            continue
        per_primary.setdefault(pp[a][1], []).append((a, b))
    out: list[str | None] = []
    for i in range(len(primary)):
        items = per_primary.get(i) or []
        if not items or all(b is None for _, b in items):
            out.append(None)
            continue
        pieces = [sp[b][0] if b is not None else "" for _, b in items]
        out.append("".join(pieces))
    return out


def region_readings(page: Page):
    """Per OCR region: (primary tokens, secondary exact counterparts, tesseract words with boxes)."""
    primary_name, secondary_name, fast = engine_names()
    h, w = page.preprocess.output_height, page.preprocess.output_width
    out = []
    for target in _targets(page, (h, w), ocr_only=True):
        runs = _latest_runs(page, target)
        tess = runs.get(fast)
        text, alt, fallback, reason, source = select_text(runs.get(primary_name), runs.get(secondary_name), tess)
        p = text.split()
        s = (alt or "").split()
        words = []
        if tess is not None and tess.status == OcrRun.Status.OK:
            for ln in tess.params.get("lines") or []:
                for wd in ln.get("words") or []:
                    if wd.get("bbox"):
                        words.append(wd)
        out.append((target, p, exact_counterparts(p, s), words, source, bool(s), reason, tess.parsed_text if tess is not None else ""))
    return out


def iou(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def tess_conf(tok: dict, words: list[dict]) -> float | None:
    box, tt = tok.get("bbox"), tok.get("tess")
    if not box:
        return None
    best, conf = 0.0, None
    for wd in words:
        o = iou(box, wd["bbox"])
        if o > best and (tt is None or wd.get("text") == tt or o > 0.5):
            best, conf = o, wd.get("conf")
    return float(conf) if conf is not None and best > 0.3 else None


def run(book_ids, out_path):
    rows = 0
    with open(out_path, "w") as fh:
        for page in Page.objects.filter(book_id__in=book_ids, status__in=("reviewed", "assembled", "ocr_done")).order_by("book_id", "number"):
            try:
                regions = region_readings(page)
            except Exception as exc:  # noqa: BLE001
                print("skip page", page.pk, exc)
                continue
            # the primary tokens of all regions in order, with their secondary counterparts
            flat_p, flat_s, flat_h, flat_kind = [], [], [], []
            words = []
            for target, p, s, wds, source, has_sec, reason, tess_text in regions:
                flat_p.extend(p)
                flat_s.extend(s)
                flat_h.extend([has_sec] * len(p))
                flat_kind.extend([target.kind] * len(p))
                words.extend(wds)
            pointer = [0]
            for lid, orig, final, manual, line in original_lines(page):
                if orig is None:
                    continue
                ot = [str(t.get("t") or "") for t in orig]
                ft = [str(t.get("t") or "") for t in (final or [])]
                ocr_t = (line.ocr_text.split() if line is not None else ot)
                # map original tokens to primary tokens through the line's ocr_text (Kraken may have
                # changed `t`; its `qari.t` keeps Qari's reading)
                keys = [str((t.get("qari") or {}).get("t") or t.get("t") or "") for t in orig]
                if line is not None:
                    lo, hi = pointer[0], min(len(flat_p), pointer[0] + len(keys) + 80)
                else:
                    lo, hi = 0, len(flat_p)
                window = flat_p[lo:hi]
                sm_line = difflib.SequenceMatcher(None, window, keys, autojunk=False)
                sec_of = {}
                has_of = {}
                kind_of = {}
                last = None
                for tag, i0, i1, j0, j1 in sm_line.get_opcodes():
                    if tag == "equal":
                        for k in range(j1 - j0):
                            sec_of[j0 + k] = flat_s[lo + i0 + k]
                            has_of[j0 + k] = flat_h[lo + i0 + k]
                            kind_of[j0 + k] = flat_kind[lo + i0 + k]
                            last = lo + i0 + k
                if line is not None and last is not None:
                    pointer[0] = last + 1
                sm = difflib.SequenceMatcher(None, ot, ft, autojunk=False)
                changed = [False] * len(ot)
                newtxt = [""] * len(ot)
                fres = [None] * len(ot)
                for tag, i0, i1, j0, j1 in sm.get_opcodes():
                    if tag == "equal":
                        for k in range(i1 - i0):
                            fres[i0 + k] = (final[j0 + k] or {}).get("res")
                    if tag in ("replace", "delete"):
                        for k in range(i0, i1):
                            changed[k] = True
                            off = k - i0
                            if tag == "replace":
                                newtxt[k] = ft[j0 + off] if (i1 - i0) == (j1 - j0) else " ".join(ft[j0:j1])
                for k, tok in enumerate(orig):
                    row = {
                        "book": page.book_id,
                        "page": page.number,
                        "line": lid,
                        "i": k,
                        "t": ot[k],
                        "cat": category(ot[k]),
                        "conf": tok.get("conf"),
                        "digit": bool(tok.get("digit")),
                        "alt": tok.get("alt"),
                        "sec": sec_of.get(k),
                        "has_sec": has_of.get(k),
                        "region": kind_of.get(k) or (line.region.kind if line is not None and line.region_id else None),
                        "tess": tok.get("tess"),
                        "tconf": tess_conf(tok, words),
                        "src": tok.get("src"),
                        "res": fres[k] if not changed[k] else "changed",
                        "bq": tok.get("bq"),
                        "boxed": bool(tok.get("bbox")),
                        "changed": changed[k],
                        "new": newtxt[k],
                        "kind": change_kind(ot[k], newtxt[k]) if changed[k] and newtxt[k] else ("deleted" if changed[k] else "same"),
                    }
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    rows += 1
    print("rows", rows, "->", out_path)


if __name__ == "__main__":
    ids = [int(x) for x in sys.argv[1].split(",")]
    run(ids, sys.argv[2])
