import os, sys, django
sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
django.setup()
import cv2, numpy as np
from books.models import Page
from core.images import load_gray
from processing import pipeline as pp
for b, n in [(int(sys.argv[1]), int(x)) for x in sys.argv[2].split(",")]:
    page = Page.objects.get(book_id=b, number=n)
    pre = page.preprocess
    with pre.gray_image.open("rb") as fh:
        gray = load_gray(fh)
    h, w = gray.shape
    lines = pre.line_boxes
    med_h = pre.median_line_height
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kx = max(15, int(pp.RULE_CLOSE_FRAC * w)) | 1
    closed = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (kx, 1)))
    ncc, _, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
    max_thickness = max(3.0, 0.3 * med_h) if med_h else 3.0
    min_width = max(20, int(pp.RULE_MIN_WIDTH_FRAC * pp._text_block_width(lines, w)))
    print(f"b{b} p{n}: h={h} w={w} med_h={med_h} max_thick={max_thickness:.1f} min_width={min_width} kx={kx}")
    for i in range(1, ncc):
        x, y, cw, ch, area = (int(v) for v in stats[i])
        if cw < min_width or y < 0.4 * h: continue
        thick = area / cw
        if ch <= max(12, 0.06*h) and thick < 15:
            print(f"   comp y={y} h={ch} x={x} w={cw} thick={thick:.1f}  -> {'OK' if thick <= max_thickness else 'too thick'}")
