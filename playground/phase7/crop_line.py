import os, sys, django
sys.path.insert(0, "/Users/alibenmussa/PycharmProjects/me/Nassakh")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")
django.setup()
from books.models import Page
from PIL import Image
out = "/private/tmp/claude-501/-Users-alibenmussa-PycharmProjects-me-Nassakh/5e011468-4f0f-4097-b8c6-1a1084b7f4be/scratchpad/phase7/"
# args: book page word name
b, n, word, name = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
page = Page.objects.get(book_id=b, number=n)
img = Image.open(page.preprocess.gray_image.path)
crops = []
for line in page.lines.order_by("order"):
    if word in line.text and line.bbox:
        x0, y0, x1, y1 = line.bbox
        print(line.order, line.text)
        crops.append(img.crop((0, max(0, y0 - 40), img.width, min(img.height, y1 + 40))))
if crops:
    w = max(c.width for c in crops); h = sum(c.height for c in crops)
    canvas = Image.new("L", (w, h), 255)
    y = 0
    for c in crops:
        canvas.paste(c, (0, y)); y += c.height
    scale = min(1.0, 1400 / w)
    canvas.resize((int(w * scale), int(h * scale))).save(out + name + ".png")
