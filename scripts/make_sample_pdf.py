"""Make samples/alwaraqat-sample.pdf: a short "scanned" book to try Nassakh with (docs/RUN_LOCALLY.md).

The text is the opening of «الورقات» in أصول الفقه by إمام الحرمين الجويني (d. 478 AH), a classical text in
the public domain, typed for this sample. The footnote is a short editorial note written for the sample. The
pages are set in Amiri (static/fonts/amiri, SIL OFL) by WeasyPrint, rendered at 300 dpi by PyMuPDF and made to
look like a scan (gray paper, a little noise and blur, a slight skew), then saved as an image-only PDF: there
is no text layer, so the text that comes out of Nassakh can only come from OCR.

    .venv/bin/python scripts/make_sample_pdf.py               # writes samples/alwaraqat-sample.pdf
    .venv/bin/python scripts/make_sample_pdf.py -o /tmp/x.pdf

Deterministic: the same seed gives the same page images.
"""

from __future__ import annotations

import argparse
import io
import random
from pathlib import Path

import numpy as np
import pymupdf as fitz
from PIL import Image, ImageFilter
from weasyprint import HTML

ROOT = Path(__file__).resolve().parent.parent
FONTS = ROOT / "static" / "fonts" / "amiri"
DPI = 300

# Page 1 and page 2 of the sample: (printed page number, paragraphs, footnote or None).
PAGES = [
    (
        "٣",
        [
            ("center", "بسم الله الرحمن الرحيم"),
            (
                "",
                "قال الشيخ الإمام إمام الحرمين أبو المعالي عبد الملك الجويني رحمه الله تعالى<sup>(١)</sup>: "
                "هذه ورقات تشتمل على معرفة فصول من أصول الفقه، وذلك مؤلف من جزأين مفردين: "
                "أحدهما الأصول، والآخر الفقه.",
            ),
            ("", "فالأصل: ما بني عليه غيره، والفرع: ما يبنى على غيره."),
            ("", "والفقه: معرفة الأحكام الشرعية التي طريقها الاجتهاد."),
            (
                "",
                "والأحكام سبعة: الواجب، والمندوب، والمباح، والمحظور، والمكروه، والصحيح، والباطل.",
            ),
            ("", "فالواجب: ما يثاب على فعله، ويعاقب على تركه."),
            ("", "والمندوب: ما يثاب على فعله، ولا يعاقب على تركه."),
            ("", "والمباح: ما لا يثاب على فعله، ولا يعاقب على تركه."),
            ("", "والمحظور: ما يثاب على تركه، ويعاقب على فعله."),
            ("", "والمكروه: ما يثاب على تركه، ولا يعاقب على فعله."),
        ],
        "(١) هو إمام الحرمين أبو المعالي عبد الملك بن عبد الله بن يوسف الجويني، "
        "ولد سنة ٤١٩هـ، وتوفي بنيسابور سنة ٤٧٨هـ.",
    ),
    (
        "٤",
        [
            ("", "والصحيح: ما يتعلق به النفوذ ويعتد به."),
            ("", "والباطل: ما لا يتعلق به النفوذ ولا يعتد به."),
            ("", "والفقه أخص من العلم."),
            ("", "والعلم: معرفة المعلوم على ما هو به في الواقع."),
            ("", "والجهل: تصور الشيء على خلاف ما هو به في الواقع."),
            (
                "",
                "والعلم الضروري: ما لا يقع عن نظر واستدلال، كالعلم الواقع بإحدى الحواس الخمس التي هي: "
                "السمع، والبصر، والشم، والذوق، واللمس، أو بالتواتر.",
            ),
            ("", "وأما العلم المكتسب: فهو الموقوف على النظر والاستدلال."),
            ("", "والنظر: هو الفكر في حال المنظور فيه. والاستدلال: طلب الدليل."),
            ("", "والدليل: هو المرشد إلى المطلوب."),
            ("", "والظن: تجويز أمرين أحدهما أظهر من الآخر."),
            ("", "والشك: تجويز أمرين لا مزية لأحدهما على الآخر."),
            ("", "وأصول الفقه: طرقه على سبيل الإجمال، وكيفية الاستدلال بها."),
            (
                "",
                "وأبواب أصول الفقه: أقسام الكلام، والأمر، والنهي، والعام والخاص، والمجمل والمبين، "
                "والظاهر والمؤول، والأفعال، والناسخ والمنسوخ، والإجماع، والأخبار، والقياس، "
                "والحظر والإباحة، وترتيب الأدلة، وصفة المفتي والمستفتي، وأحكام المجتهدين.",
            ),
        ],
        None,
    ),
]

CSS = """
@font-face { font-family: Amiri; src: url("{regular}"); }
@font-face { font-family: Amiri; font-weight: bold; src: url("{bold}"); }
@page { size: 150mm 215mm; margin: 0; }
html { direction: rtl; }
body { margin: 0; font-family: Amiri; color: #111; }
.page { position: relative; width: 150mm; height: 215mm; page-break-after: always; }
.head { position: absolute; top: 14mm; left: 18mm; right: 18mm; height: 9mm; font-size: 12pt;
        border-bottom: 0.5pt solid #222; }
.head .title { position: absolute; left: 0; right: 0; text-align: center; }
.head .num { position: absolute; left: 0; }
.body { position: absolute; top: 28mm; left: 18mm; right: 18mm; font-size: 14pt; line-height: 1.6;
        text-align: justify; }
.body p { margin: 0 0 1mm 0; text-indent: 6mm; }
.body p.center { text-align: center; text-indent: 0; font-weight: bold; }
sup { font-size: 9pt; vertical-align: 5pt; line-height: 0; }
.foot { position: absolute; bottom: 18mm; left: 18mm; right: 18mm; font-size: 11pt; line-height: 1.6;
        text-align: justify; }
.foot .rule { width: 40mm; border-top: 0.6pt solid #222; margin-bottom: 1.5mm; }
"""


def build_html() -> str:
    css = CSS.replace("{regular}", (FONTS / "Amiri-Regular.ttf").as_uri()).replace(
        "{bold}", (FONTS / "Amiri-Bold.ttf").as_uri()
    )
    pages = []
    for number, paragraphs, footnote in PAGES:
        body = "".join(f'<p class="{cls}">{text}</p>' for cls, text in paragraphs)
        foot = f'<div class="foot"><div class="rule"></div>{footnote}</div>' if footnote else ""
        pages.append(
            '<div class="page"><div class="head"><span class="title">الورقات في أصول الفقه</span>'
            f'<span class="num">{number}</span></div><div class="body">{body}</div>{foot}</div>'
        )
    head = f'<head><meta charset="utf-8"><style>{css}</style></head>'
    return f'<html lang="ar" dir="rtl">{head}<body>{"".join(pages)}</body></html>'


def scanned(image: Image.Image, rng: random.Random) -> Image.Image:
    """Gray paper, a little noise and blur, a slight skew: what a flatbed scan of a page looks like."""
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    ink = 255.0 - gray
    paper = 236.0 + rng.uniform(-4, 4)
    out = paper - ink * (paper - 22.0) / 255.0
    noise = np.random.default_rng(rng.randrange(2**32)).normal(0.0, 6.0, out.shape)
    out = np.clip(out + noise, 0, 255).astype(np.uint8)
    page = Image.fromarray(out, "L").filter(ImageFilter.GaussianBlur(0.6))
    angle = rng.uniform(-0.8, 0.8)
    return page.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=int(paper))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-o", "--output", default=str(ROOT / "samples" / "alwaraqat-sample.pdf"))
    parser.add_argument("--seed", type=int, default=478)
    parser.add_argument("--quality", type=int, default=55, help="JPEG quality of the page images")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    typeset = fitz.open(stream=HTML(string=build_html(), base_url=str(ROOT)).write_pdf(), filetype="pdf")
    scan = fitz.open()
    for page in typeset:
        pix = page.get_pixmap(dpi=DPI, colorspace=fitz.csGRAY)
        image = scanned(Image.frombytes("L", (pix.width, pix.height), pix.samples), rng)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=args.quality, dpi=(DPI, DPI), optimize=True)
        out = scan.new_page(width=page.rect.width, height=page.rect.height)
        out.insert_image(out.rect, stream=buffer.getvalue())
    scan.set_metadata({"title": "الورقات (sample)", "creator": "scripts/make_sample_pdf.py"})
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    scan.save(output, garbage=4, deflate=True)
    print(f"{output}: {len(scan)} pages, {output.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
