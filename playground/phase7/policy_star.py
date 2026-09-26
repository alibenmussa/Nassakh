import json, re, sys
sys.argv = ["x", "features.jsonl"]
exec(open("evaluate.py").read().split('if __name__ == "__main__":')[0])
from core.arabic import to_western_digits
FOREIGN = re.compile(r"[Ͱ-ϿЀ-ӿ֐-׿぀-ヿ一-鿿가-힯]|[※★∩∧∨≡→©¢€¥^]")
def digits(t): return re.sub(r"\D", "", to_western_digits(t or ""))
def star(r, single_tess=False):
    t, sec, cat = r["t"], r["sec"], r["cat"]
    if FOREIGN.search(t): return True
    if r["src"] == "kraken": return True
    if cat == "punct": return False
    if cat in ("number", "mixed_digit"):
        if not re.search(r"[٠-٩۰-۹]", t):
            s = digits(sec) if sec else None
            te = digits(r["tess"]) if r["tess"] else (digits(t) if r["boxed"] else None)
            if s == digits(t) and te == digits(t): return False
        return True
    if cat == "letterdigit": return r["conf"] == "low"
    if not r.get("has_sec"):
        return single_tess and tess_disagrees(r, 70)
    if sec is None:
        return not (r["boxed"] and not r["tess"])
    return lenient(t) != lenient(sec)
if __name__ == "__main__":
    for label, subset in (("ALL", None), ("UX 23+25", lambda r: r["book"] in (23, 25)), ("owner books", lambda r: r["book"] not in (23, 25, 26))):
        print("====", label)
        score("P0 current", p0, subset)
        score("P* proposed", star, subset)
        score("P* + Tesseract on single-reader regions", lambda r: star(r, True), subset)
