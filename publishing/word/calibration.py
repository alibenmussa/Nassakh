"""The calibration file (PHASE6_SPEC §11.6, D62): one labelled page per item, each with its expected
result in Arabic, for the owner's one look at what the spike could not check. `calibration_docx()`
builds it with the writer's own pieces (styles, sections, footnotes, comments, the font table with
Amiri embedded); `c12_docx()` is the harness's automatic C12 file (60 pairs of 10 mm after + 10 mm
before: the page count tells whether Word adds the two or takes the larger). `CHECKLIST` is what the
harness prints beside the file.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from lxml import etree

from publishing import fonts as F
from publishing.model import PageSetup

from . import ooxml
from .faces import FacePlan
from .ooxml import field_runs, fld_char, instr_text, root, run, text, twips, twips_mm, w
from .options import COMPAT_MODE, WordOptions
from .runs import CommentsPart, FootnotesPart, RunWriter, rpr
from .sections import (
    NUMBERING_RESTART,
    Bookmarks,
    HdrFtrPart,
    Para,
    page_field,
    resolve_flow,
    sect_pr,
)
from .styles import style_table, styles_xml, text_width_mm
from .writer import FIRST_PART_REL, Properties, assemble, base_rels, name_parts

FILLER = (
    "هذا نصّ للمعايرة يملأ السطر بكلمات عربية عادية حتى يلتفّ إلى السطر التالي ويُظهر ضبط الأسطر كما"
    " يضبطه Word، ويستمرّ الكلام على هذا النحو حتى يكتمل ما يلزم من الأسطر للحكم على الشكل."
)
TASHKEEL = (
    "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ، مُحَمَّدٌ رَسُولُ اللَّهِ، شَدَّةٌ مَعَ فَتْحَةٍ وَضَمَّةٌ مُشَدَّدَةٌ وَكَسْرَةٌ"
    " مُشَدَّدَةٌ، تَتَرَاكَبُ الحَرَكَاتُ فَوْقَ الحُرُوفِ وَتَحْتَهَا في هَذَا السَّطْرِ المُعَايَرِ."
)

# (item, title, what the owner should see)
CHECKLIST: tuple[tuple[str, str, str], ...] = (
    ("C1", "الهامش الداخلي", "الشكل أ: الهامش العريض في الصفحة 1 على اليمين وفي الصفحة 2 على اليسار."),
    ("C2", "أرقام الصفحات الخارجية ورأس الصفحة", "الرقم عند الحافة الخارجية، ورأس الصفحة في الوسط."),
    ("C3", "ترقيم الحواشي لكل صفحة", "(1) و(2) في كل صفحة من الصفحات الثلاث."),
    ("C4", "الفاصل فوق الحواشي", "خط بعرض النص كلّه، فوقه 4 مم وتحته 1.6 مم."),
    ("C5", "الكشيدة في كل خط", "فقرة بلا كشيدة، وخفيفة، ومتوسطة، وقوية في كل خط."),
    ("C6", "التعليقات", "تعليق على كلمة في المتن وعلى كلمة في حاشية."),
    ("C7", "خط أميري المضمَّن", "عطّل أميري في Font Book وأعد فتح الملف؛ يبقى النص بخط أميري."),
    ("C9", "المحتويات", "نقاط التوجيه، والأرقام في جهة النهاية، و⌘-نقرة تتبع الرابط."),
    ("C10", "معنى اليمين واليسار", "كل فقرة تقول أين يجب أن تظهر."),
    ("C11", "الحركات المتراكبة في أميري", "لا تُقصّ رؤوس الحركات عند 1.35 و1.7."),
    ("C13", "سطر ينتهي بفاصل سطر", "السطر الذي ينتهي بفاصل السطر لا يُمدّ."),
    ("C14", "افتتاح الفصول على اليمين", "ماذا تعرض الصفحة البيضاء قبل الفصل."),
    ("C15", "المسافة قبل الفقرة في أعلى الصفحة", "بعد فاصل صفحة مفروض وبعد فاصل طبيعي."),
)


def _setup() -> PageSetup:
    return PageSetup(
        body_font="amiri",
        latin_font="times",
        heading_font="amiri",
        page_number="bottom_outer",
        running_header="book",
        chapter_opening="recto",
    )


class _Calibration:
    """Builds the sections of the calibration file."""

    def __init__(self, now: datetime):
        self.now = now
        self.setup = _setup()
        self.fonts = F.resolve(self.setup.body_font, self.setup.latin_font, self.setup.heading_font)
        self.faces = FacePlan(self.fonts)
        self.table = style_table(self.setup)
        self.notes = FootnotesPart()
        self.comments = CommentsPart(None, ooxml.iso_datetime(now))  # text_runs set below
        self.writer = RunWriter(self.faces, notes=self.notes, comments=self.comments)
        self.comments.text_runs = self.writer.text_runs
        self.bookmarks = Bookmarks()
        self.parts: list[HdrFtrPart] = []
        self.next_rel = FIRST_PART_REL
        self.sections: list[tuple[list[Para], etree._Element]] = []
        self.width = text_width_mm(self.setup)

    # ------------------------------------------------------------------ pieces

    def p(self, value: str, style: str = "Normal", **kwargs) -> Para:
        return Para(
            style,
            self.writer.text_runs(value, role="heading" if style.startswith("Heading") else "body"),
            **kwargs,
        )

    def filler(self, count: int = 2, **kwargs) -> Para:
        return self.p(" ".join([FILLER] * count), **kwargs)

    def heading(self, code: str, title: str, expectation: str) -> list[Para]:
        name = self.bookmarks.name("_Toc_", code)
        head = Para(
            "Heading1", self.bookmarks.wrap(name, self.writer.text_runs(f"{code} — {title}", role="heading"))
        )
        return [head, self.p(expectation, "NkCenter")]

    def part(self, kind: str, type_: str, paragraph: etree._Element) -> HdrFtrPart:
        part = HdrFtrPart(
            kind, type_, root("hdr" if kind == "hdr" else "ftr", paragraph), rel_id=f"rId{self.next_rel}"
        )
        self.next_rel += 1
        self.parts.append(part)
        return part

    def number_footer(self, side: str) -> HdrFtrPart:
        paragraph = w(
            "p",
            w("pPr", w("pStyle", val="Footer"), w("jc", val=side) if side != "center" else None),
            *page_field(lambda: rpr(rstyle="PageNumber")),
        )
        return self.part("ftr", "default" if side != "right" else "even", paragraph)

    def header(self, type_: str, value: str) -> HdrFtrPart:
        content = self.writer.text_runs(value) if value else []
        return self.part("hdr", type_, w("p", w("pPr", w("pStyle", val="Header")), *content))

    def empty_parts(self) -> list[HdrFtrPart]:
        """Empty headers and footers, so a section shows none (else it inherits the previous ones)."""
        out = []
        for kind in ("hdr", "ftr"):
            for type_ in ("default", "even", "first"):
                paragraph = w("p", w("pPr", w("pStyle", val="Header" if kind == "hdr" else "Footer")))
                out.append(self.part(kind, type_, paragraph))
        return out

    def section(
        self,
        paras: list[Para],
        *,
        break_type: str | None = "nextPage",
        parts: list[HdrFtrPart] = (),
        title_pg: bool = False,
        setup: PageSetup | None = None,
        pgmar_left: str | None = None,
    ) -> None:
        props = sect_pr(
            setup or self.setup,
            self.table,
            break_type=break_type if self.sections else None,
            references=[(part.kind, part.type, part.rel_id) for part in parts],
            title_pg=title_pg,
            pgmar_left=pgmar_left,
        )
        self.sections.append((paras, props))

    # ------------------------------------------------------------------ items

    def intro(self) -> None:
        paras = [
            self.p("ملف المعايرة", "Title"),
            self.p("كل بند في صفحة، وتحته ما يُنتظر أن تراه. سجّل ما رأيته في D62.", "NkAuthor"),
            self.p("C9 — المحتويات", "TOCHeading"),
        ]
        entries = [(code, title) for code, title, _expect in CHECKLIST if code != "C9"]
        for index, (code, title) in enumerate(entries):
            name = self.bookmarks.name("_Toc_", code)
            link = w(
                "hyperlink",
                *self.writer.text_runs(f"{code} — {title}"),
                run(w("tab")),
                *field_runs(f"PAGEREF {name} \\h", [run(text(str(index + 2)))]),
                anchor=name,
                history=1,
            )
            content: list[etree._Element] = []
            if index == 0:
                content += [fld_char("begin"), instr_text('TOC \\o "1-2" \\h \\z \\u'), fld_char("separate")]
            content.append(link)
            if index == len(entries) - 1:
                content.append(fld_char("end"))
            paras.append(Para("TOC1", content))
        paras.append(self.p("الأرقام هنا تقريبية؛ «تحديث الحقل» يصحّحها، و⌘-نقرة تتبع الرابط.", "NkCenter"))
        self.section(paras, break_type=None, parts=self.empty_parts())

    def c1(self) -> None:
        wide = replace(self.setup, inner_mm=40, outer_mm=10)
        for variant, side, expect in (
            ("أ", "inner", "left = inner: الهامش العريض في الصفحة 1 على اليمين وفي الصفحة 2 على اليسار."),
            ("ب", "outer", "left = outer: الهامش العريض في الصفحة 1 على اليسار وفي الصفحة 2 على اليمين."),
        ):
            paras = self.heading(
                f"C1{variant}" if variant == "ب" else "C1", f"الهامش الداخلي — الشكل {variant}", expect
            )
            for page in range(1, 5):
                paras.append(
                    self.p(f"الشكل {variant} — الصفحة {page}: الهامش العريض 40 مم.", page_break=page > 1)
                )
                paras.append(self.filler(3))
            self.section(paras, break_type="oddPage", setup=wide, pgmar_left=side)

    def c2(self) -> None:
        parts = [
            self.number_footer("left"),
            self.number_footer("right"),
            self.header("default", "رأس الصفحة"),
            self.header("even", "رأس الصفحة"),
        ]
        paras = self.heading("C2", "أرقام الصفحات الخارجية ورأس الصفحة", CHECKLIST[1][2])
        for page in range(1, 4):
            paras.append(self.p(f"الصفحة {page} من ثلاث: الرقم عند الحافة الخارجية.", page_break=page > 1))
            paras.append(self.filler(2))
        self.section(paras, break_type="oddPage", parts=parts)

    def c3_c4(self) -> None:
        from publishing.model import Block, Footnote, NoteRef, Run

        paras = self.heading("C3", "ترقيم الحواشي لكل صفحة", CHECKLIST[2][2])
        c4 = self.bookmarks.wrap(
            self.bookmarks.name("_Toc_", "C4"), self.writer.text_runs("C4 — " + CHECKLIST[3][2])
        )
        paras.append(Para("NkCenter", c4))
        for page in range(1, 4):
            block = Block(
                "body",
                [
                    Run(f"الصفحة {page}: حاشية أولى"),
                    NoteRef(f"a{page}"),
                    Run(" وحاشية ثانية"),
                    NoteRef(f"b{page}"),
                    Run(" ثم يتمّ الكلام. " + FILLER),
                ],
                [
                    Footnote(f"a{page}", [Run(f"الحاشية الأولى في الصفحة {page}")], 1, 1),
                    Footnote(f"b{page}", [Run(f"الحاشية الثانية في الصفحة {page}")], 2, 2),
                ],
                id=f"c3p{page}",
            )
            paras.append(
                Para(
                    "Normal",
                    self.writer.inline(block.runs, block_id=block.id, footnotes=block.footnotes),
                    page_break=page > 1,
                )
            )
        self.section(paras, parts=self.empty_parts())

    def c5(self) -> None:
        paras = self.heading("C5", "الكشيدة في كل خط", CHECKLIST[4][2])
        for key in ("amiri", "simplified_arabic", "traditional_arabic", "lotus"):
            family = F.FONTS[key].family
            paras.append(self.p(f"الخط: {F.FONTS[key].name}", "Heading2"))
            for label, jc in (
                ("بلا", "both"),
                ("خفيفة", "lowKashida"),
                ("متوسطة", "mediumKashida"),
                ("قوية", "highKashida"),
            ):
                content = [
                    run(
                        text(f"{label}: " + FILLER),
                        rpr=w("rPr", w("rFonts", ascii=family, hAnsi=family, cs=family), w("rtl")),
                    )
                ]
                paras.append(Para("Normal", content, jc=jc))
        self.section(paras)

    def c6(self) -> None:
        from publishing.model import Block, Footnote, NoteRef, Run

        paras = self.heading("C6", "التعليقات", CHECKLIST[5][2])
        block = Block(
            "body",
            [
                Run("في المتن كلمة "),
                Run("مشكوكة", ("uncertain",)),
                Run(" عليها تعليق، وفي الحاشية"),
                NoteRef("n6"),
                Run(" كلمة أخرى."),
            ],
            [
                Footnote(
                    "n6", [Run("حاشية فيها كلمة "), Run("مشكوكة", ("uncertain",)), Run(" عليها تعليق.")], 1, 1
                )
            ],
            id="c6",
        )

        class _Word:
            def __init__(self, word, page):
                self.word, self.source_page = word, page
                self.readings = [
                    {"label": "Qari", "text": word, "current": True},
                    {"label": "Tesseract", "text": "مشكولة", "current": False},
                ]

        self.writer.readings = {("c6", None): [_Word("مشكوكة", 12)], ("c6", "n6"): [_Word("مشكوكة", 12)]}
        paras.append(
            Para("Normal", self.writer.inline(block.runs, block_id=block.id, footnotes=block.footnotes))
        )
        self.writer.readings = {}
        self.section(paras)

    def c7_c11(self) -> None:
        paras = self.heading("C7", "خط أميري المضمَّن", CHECKLIST[6][2])
        amiri = F.FONTS["amiri"].family
        fonts = w("rFonts", ascii=amiri, hAnsi=amiri, cs=amiri)
        paras.append(Para("Normal", [run(text(FILLER), rpr=w("rPr", fonts, w("rtl")))]))
        paras += self.heading("C11", "الحركات المتراكبة في أميري", CHECKLIST[9][2])
        for pitch in (1.35, 1.7):
            paras.append(self.p(f"الضبط {pitch:g}", "Heading2"))
            fonts = w("rFonts", ascii=amiri, hAnsi=amiri, cs=amiri)
            paras.append(
                Para(
                    "Normal",
                    [run(text(" ".join([TASHKEEL] * 2)), rpr=w("rPr", fonts, w("rtl")))],
                    extra=[w("spacing", line=twips(pitch * self.setup.body_size_pt), lineRule="exact")],
                )
            )
        self.section(paras)

    def c10(self) -> None:
        paras = self.heading("C10", "معنى اليمين واليسار في فقرة ثنائية الاتجاه", CHECKLIST[8][2])
        paras.append(
            Para(
                "Normal",
                self.writer.text_runs("jc = left: إن التصقت هذه الفقرة باليمين فإن left تعني البداية."),
                jc="left",
                extra=[w("ind", firstLine=0)],
            )
        )
        paras.append(
            Para(
                "Normal",
                self.writer.text_runs("jc = right: إن التصقت هذه الفقرة باليسار فإن right تعني النهاية."),
                jc="right",
                extra=[w("ind", firstLine=0)],
            )
        )
        paras.append(
            Para(
                "Normal",
                self.writer.text_runs(
                    "ind left = 2 سم: إن بدأت هذه الفقرة على بُعد 2 سم من اليمين فإن left تعني البداية."
                ),
                extra=[w("ind", left=twips_mm(20), firstLine=0)],
            )
        )
        tabs = w("tabs", w("tab", val="right", pos=twips_mm(50)))
        paras.append(
            Para(
                "Normal",
                [
                    *self.writer.text_runs("تبويب right عند 5 سم:"),
                    run(w("tab")),
                    *self.writer.text_runs("إن انتهى هذا النص عند 5 سم من اليمين فإن right تعني النهاية."),
                ],
                extra=[tabs, w("ind", firstLine=0)],
            )
        )
        self.section(paras)

    def c13(self) -> None:
        paras = self.heading("C13", "سطر ينتهي بفاصل سطر", CHECKLIST[10][2])
        paras.append(
            Para(
                "Normal",
                [
                    *self.writer.text_runs("سطر قصير ينتهي بفاصل سطر"),
                    self.writer.line_break(),
                    *self.writer.text_runs(FILLER),
                ],
            )
        )
        self.section(paras)

    def c14(self) -> None:
        parts = [self.number_footer("center")]
        for chapter in (1, 2):
            paras = self.heading(
                "C14" if chapter == 1 else "C14ب",
                f"افتتاح الفصول على اليمين — الفصل {chapter}",
                CHECKLIST[11][2],
            )
            paras.append(self.filler(2))
            self.section(paras, break_type="oddPage", parts=parts if chapter == 1 else [])

    def c15(self) -> None:
        paras = self.heading("C15", "المسافة قبل الفقرة في أعلى الصفحة", CHECKLIST[12][2])
        paras.append(self.p("بعد فاصل مفروض: عنوان الفصل أعلاه يبدأ على بُعد 16 مم من أعلى النص.", "NkCenter"))
        paras.append(self.p("عنوان الكتاب", "Title", extra_before=0.28 * self.width, page_break=True))
        paras.append(self.p("بعد فاصل مفروض: العنوان يبدأ على بُعد 28 % من عرض النص.", "NkCenter"))
        for index in range(24):
            paras.append(self.filler(2))
            if index % 6 == 5:
                paras.append(
                    self.p(
                        f"عنوان فرعي {index // 6 + 1}: إن وقع في أعلى صفحة طبيعيًا، هل تبقى 5 مم فوقه؟",
                        "Heading2",
                    )
                )
        self.section(paras, parts=self.empty_parts())

    # ------------------------------------------------------------------ the package

    def build(self) -> bytes:
        self.intro()
        self.c1()
        self.c2()
        self.c3_c4()
        self.c5()
        self.c6()
        self.c7_c11()
        self.c10()
        self.c13()
        self.c14()
        self.c15()
        body = w("body")
        for index, (paras, props) in enumerate(self.sections):
            resolve_flow(paras, self.table)
            last = index == len(self.sections) - 1
            for position, para in enumerate(paras):
                if position == len(paras) - 1 and not last:
                    para.sect_pr = props
                body.append(para.element())
            if last:
                body.append(props)
        document = root("document", body)
        with_comments = self.comments.count > 0
        document_rels = base_rels(with_comments)
        name_parts(self.parts, document_rels)
        embedded = self.faces.embedded_files(True)
        settings = ooxml.settings_xml(
            embed_fonts=bool(embedded),
            even_and_odd=True,
            footnote_numbering=NUMBERING_RESTART["page"],
            number_format="decimal",
            compat_mode=COMPAT_MODE,
        )
        package = assemble(
            document=document,
            document_rels=document_rels,
            styles=styles_xml(self.setup, self.faces, WordOptions().jc),
            settings=settings,
            faces=self.faces,
            embedded=embedded,
            footnotes=self.notes.root,
            comments=self.comments.root if with_comments else None,
            parts=self.parts,
            properties=Properties(
                title="ملف المعايرة", creator="نسّاخ", custom=(("NassakhRenderer", "calibration"),)
            ),
            now=self.now,
        )
        return package.to_bytes()


def calibration_docx(now: datetime | None = None) -> bytes:
    """The calibration file (see the module docstring)."""
    return _Calibration(now or datetime.now(UTC)).build()


def c12_docx(pairs: int = 60, now: datetime | None = None) -> bytes:
    """C12: `pairs` paragraphs with 10 mm after followed by 10 mm before. If Word adds the two, each pair
    takes 20 mm plus a line; if it takes the larger, 10 mm plus a line. The page count tells."""
    setup = _setup()
    fonts = F.resolve(setup.body_font, setup.latin_font, setup.heading_font)
    faces = FacePlan(fonts)
    table = style_table(setup)
    writer = RunWriter(faces)
    paras = []
    for index in range(pairs):
        paras.append(
            Para(
                "Normal",
                writer.text_runs(f"الفقرة {index + 1}: بعدها 10 مم."),
                direct_after=10,
                direct_before=0,
            )
        )
        paras.append(
            Para(
                "Normal",
                writer.text_runs(f"الفقرة {index + 1}: قبلها 10 مم."),
                direct_before=10,
                direct_after=0,
            )
        )
    props = sect_pr(setup, table, break_type=None)
    body = w("body", *[para.element() for para in paras], props)
    document_rels = base_rels(False)
    settings = ooxml.settings_xml(
        embed_fonts=False,
        even_and_odd=False,
        footnote_numbering="eachPage",
        number_format="decimal",
        compat_mode=COMPAT_MODE,
    )
    package = assemble(
        document=root("document", body),
        document_rels=document_rels,
        styles=styles_xml(setup, faces, WordOptions().jc),
        settings=settings,
        faces=faces,
        embedded=[],
        footnotes=FootnotesPart().root,
        comments=None,
        parts=[],
        properties=Properties(title="C12", creator="نسّاخ"),
        now=now or datetime.now(UTC),
    )
    return package.to_bytes()


def c12_expected(pairs: int = 60) -> dict[str, float]:
    """The height the pairs take under each rule, in mm (the harness compares with the page count)."""
    setup = _setup()
    line = table_line_mm(setup)
    return {
        "adds": pairs * (2 * line + 20),
        "max": pairs * (2 * line + 10),
        "text_height_mm": setup.height_mm - setup.top_mm - setup.bottom_mm,
    }


def table_line_mm(setup: PageSetup) -> float:
    return style_table(setup)["Normal"].line_pt * 25.4 / 72
