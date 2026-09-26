"""Assemble books 23 and 25 in memory with footnote kinds from the proposed detection (read-only)."""
import os, sys, dataclasses
sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
import django; django.setup()
from books.models import Book, Page
from assembly import pipeline, services
from core.images import load_gray
from processing import pipeline as pp
from rules import rule_candidates
from fnzone import zone
from ocr.services import _latest_runs, _targets, engine_names
_, _, fast = engine_names()
def split_y(page):
    pre = page.preprocess
    with pre.gray_image.open("rb") as fh: gray = load_gray(fh)
    ink = pp.binarize_clean(gray, min_area=max(4, int(gray.size * 1e-5)))
    bands, med = pp.detect_lines(ink)
    cand = rule_candidates(gray, bands, med, 0.5)
    if cand and (cand[2] <= 0.3 * med or cand[3] >= 0.6): return cand[0], "rule"
    lines = []
    for t in _targets(page, (pre.output_height, pre.output_width), ocr_only=True):
        if t.kind == "footnote": return None, "has region"
        tess = _latest_runs(page, t).get(fast)
        if tess is not None and tess.status == "ok": lines.extend(tess.params.get("lines") or [])
    z = zone(lines, pre.output_height, None)
    return (z[2] - 5, "zone") if z else (None, "none")
def run(book_id, with_fix):
    book = Book.objects.get(pk=book_id)
    loaded = services.load_book(book)
    pages = []
    for pin in loaded.pages:
        page = Page.objects.get(pk=pin.id)
        if with_fix:
            y, how = split_y(page)
            if y is not None:
                ratio = y / page.preprocess.output_height
                lines = [dataclasses.replace(l, kind=pipeline.FOOTNOTE) if l.box and l.box[1] >= ratio else l for l in pin.lines]
                pin = dataclasses.replace(pin, lines=lines)
        pages.append(pin)
    res = pipeline.assemble(pages, book.assembly_settings, services.book_meta(book))
    notes = [w for w in res.warnings if w["code"] in ("note_orphan", "marker_unmatched")]
    print(f"book {book_id} {'with detection' if with_fix else 'as today'}: footnotes {res.stats['footnotes']}, orphans {sum(1 for w in notes if w['code']=='note_orphan')}, unmatched markers {sum(1 for w in notes if w['code']=='marker_unmatched')}")
    for w in notes: print("     ", w["code"], w["page"], w["message"][:90])
for b in (25, 23):
    run(b, False); run(b, True)
