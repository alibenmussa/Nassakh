"""Spike: a بيت as two grid items in WeasyPrint; do the laid-out lines come out as two boxes on one row?"""
import weasyprint
from weasyprint.formatting_structure import boxes
FONT = "/Users/alibenmussa/PycharmProjects/me/Nassakh/static/fonts/amiri/Amiri-Regular.ttf"
html = f"""<html dir="rtl"><head><style>
@font-face {{ font-family: A; src: url('file://{FONT}'); }}
@page {{ size: 170mm 240mm; margin: 20mm; }}
body {{ font-family: A; font-size: 13pt; line-height: 1.8; }}
p.nk-body {{ text-align: justify; }}
p.nk-verse {{ display: grid; grid-template-columns: 1fr 1fr; column-gap: 8%; margin: 0; }}
p.nk-verse > br {{ display: none; }}
p.nk-verse > span {{ text-align: justify; text-align-last: justify; }}
</style></head><body>
<p class="nk-body" data-block="p1">ثم ملك لبده بعدها وقال:</p>
<p class="nk-verse" data-block="p2"><span>لله دري! اذا اعدو على فرسي</span><br><span>الى الهياج؛ ونار الحرب تستعر</span></p>
<p class="nk-verse" data-block="p3"><span>وفي يدي صارم، افري الرؤوس به</span><br><span>في حده الموت، لا يبقي! ولا يذر</span></p>
</body></html>"""
doc = weasyprint.HTML(string=html).render()
def walk(box, depth=0):
    if isinstance(box, boxes.LineBox):
        texts = []
        def collect(b):
            if isinstance(b, boxes.TextBox): texts.append(b.text)
            for c in getattr(b, "children", ()): collect(c)
        collect(box)
        print(f"{'  '*depth}LineBox x={box.position_x:.1f} y={box.position_y:.1f} w={box.width:.1f} text={''.join(texts)!r}")
        return
    for c in getattr(box, "children", ()):
        walk(c, depth + 1)
for page in doc.pages:
    walk(page._page_box)
