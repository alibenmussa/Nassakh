"""Secondary-only runs (S1) with Tesseract support: does Tesseract hold the run's words, unaccounted for by
the primary, on the lines around the gap? Read-only probe over stored runs (no model call)."""
from __future__ import annotations

import os
import sys
from collections import Counter

sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
import django  # noqa: E402

django.setup()
from rapidfuzz import fuzz  # noqa: E402

from books.models import Page  # noqa: E402
from core.arabic import normalize  # noqa: E402
from ocr.alignment import align_tokens, build_lines, is_word  # noqa: E402
from ocr.services import _collect_region_texts, _page_geometry  # noqa: E402
from features import split_punct  # noqa: E402


def lk(t):
    return normalize(t, "lenient") or ("\x00" + t)


def runs_of(p, s):
    pp = split_punct(p)
    sp = split_punct(s)
    out, cur, anchor = [], [], 0
    for i, j in align_tokens([x for x, _ in pp], [x for x, _ in sp], key=lk):
        if i is None and j is not None:
            cur.append(sp[j][0])
        else:
            if cur and any(is_word(w) for w in cur):
                out.append((anchor, cur))
            cur = []
            if i is not None:
                anchor = pp[i][1] + 1  # the primary token after which the run goes
    if cur and any(is_word(w) for w in cur):
        out.append((anchor, cur))
    return out


def supported(run_words, pool):
    words = [w for w in run_words if is_word(w)]
    if not words:
        return 0.0
    pool = list(pool)
    hit = 0
    for w in words:
        lw = normalize(w, "lenient")
        best = max(((fuzz.ratio(lw, normalize(t, "lenient")), k) for k, t in enumerate(pool)), default=(0, None))
        if best[0] >= 75:
            hit += 1
            pool.pop(best[1])
    return hit / len(words)


def main(book_ids, verbose):
    tally = Counter()
    for page in Page.objects.filter(book_id__in=book_ids, is_excluded=False, status__in=("ocr_done", "reviewed", "assembled")).order_by("book_id", "number"):
        try:
            rts = _collect_region_texts(page)
        except Exception:  # noqa: BLE001
            continue
        bands, gray = _page_geometry(page)
        for rt in rts:
            if not rt.alt_text or not rt.tess_lines:
                continue
            p = rt.text.split()
            built = build_lines(rt.text, rt.alt_text, rt.tess_lines, bands, None)
            # primary token index -> built line index; each built line's Tesseract words (by Tesseract line)
            line_of_token = []
            for k, b in enumerate(built):
                line_of_token.extend([k] * len(b["tokens"]))
            # Tesseract words per built line: the Tesseract line whose box overlaps the built line's box most
            tlines = rt.tess_lines

            def tess_words_near(k):
                box = built[k]["bbox"] if 0 <= k < len(built) else None
                if not box:
                    return []
                out = []
                for tl in tlines:
                    tb = tl.get("bbox")
                    if tb and min(tb[3], box[3]) - max(tb[1], box[1]) > 0.3 * (box[3] - box[1]):
                        out.extend(str(w.get("text") or "") for w in tl.get("words") or [])
                return out

            for at, words in runs_of(p, rt.alt_text.split()):
                ks = set()
                for idx in (at - 1, at):
                    if 0 <= idx < len(line_of_token):
                        ks.add(line_of_token[idx])
                ks |= {k + 1 for k in ks} | {k - 1 for k in ks}
                pool = []
                prim = []
                for k in sorted(ks):
                    if 0 <= k < len(built):
                        pool.extend(tess_words_near(k))
                        prim.extend(t["t"] for t in built[k]["tokens"])
                # remove what the primary already accounts for
                left = list(pool)
                for t in prim:
                    lt = normalize(t, "lenient")
                    for n, w in enumerate(left):
                        if normalize(w, "lenient") == lt and lt:
                            left.pop(n)
                            break
                sup = supported(words, left)
                nw = sum(1 for w in words if is_word(w))
                tally[("runs",)] += 1
                tally[("supported>=0.5",)] += sup >= 0.5
                tally[("words",)] += nw
                tally[("words_supported",)] += nw if sup >= 0.5 else 0
                if verbose:
                    print(f"b{page.book_id} p{page.number} {page.status[:6]} {rt.target.kind:8s} sup={sup:.2f} +«{' '.join(words)}»  ctx «{' '.join(p[max(0, at - 3):at])} ⟨+⟩ {' '.join(p[at:at + 2])}»")
    print(dict(tally))


if __name__ == "__main__":
    main([int(x) for x in sys.argv[1].split(",")], len(sys.argv) > 2)
