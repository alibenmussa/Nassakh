"""The Word export's options, its Arabic messages and the conventions calibration settles (PHASE6_SPEC
§3.1, §5.4, §7).

`WordOptions.parse(data)` normalises the `{kashida, comments}` of an export request (a `ValueError` with an
Arabic message for a bad value). `KASHIDA` is the choice table the export page renders (the labels live
here, never in a template). `NOTES` are the known differences of a Word file (`DocxExporter.notes`) and
`WARNINGS` what a build reports about the file it made.

The calibration constants (D62, §5.4) are the writer's reading of the OOXML specification until the
owner's calibration pass settles each one; every constant names the item (C0–C15, §11.6) that decides it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

WORD_VERSION = "nk-word-1"

# ====================================================================== options

KASHIDA_KEYS: tuple[str, ...] = ("none", "low", "medium", "high")
KASHIDA_DEFAULT = "low"
# the choices of the export page: key → (short label, full label, hint)
KASHIDA: dict[str, dict[str, str]] = {
    "none": {
        "label": "بلا",
        "title": "بلا كشيدة",
        "hint": "تُضبط الأسطر بالمسافات وحدها، كما في المعاينة.",
    },
    "low": {
        "label": "خفيفة",
        "title": "كشيدة خفيفة",
        "hint": "الأسطر كما في المعاينة تقريبًا.",
    },
    "medium": {
        "label": "متوسطة",
        "title": "كشيدة متوسطة",
        "hint": "يغيّر Word فواصل الأسطر فيطول الكتاب عن المعاينة.",
    },
    "high": {
        "label": "قوية",
        "title": "كشيدة قوية",
        "hint": "يغيّر Word فواصل الأسطر فيطول الكتاب كثيرًا عن المعاينة.",
    },
}
# the `w:jc` of the body, quote and footnote styles for each choice (§5.10)
KASHIDA_JC: dict[str, str] = {
    "none": "both",
    "low": "lowKashida",
    "medium": "mediumKashida",
    "high": "highKashida",
}
KASHIDA_LABEL = "الكشيدة"
COMMENTS_LABEL = "تعليقات على الكلمات غير المؤكَّدة"
COMMENTS_HINT = (
    "تعليق Word لكل كلمة بقراءاتها وصفحتها الأصلية، للمدقّق. نسخة للمراجعة لا للمطبعة: يطبع Word "
    "التعليقات في الهامش ما لم تُخفِها."
)
COMMENTS_NONE_HINT = "لا كلمات غير مؤكَّدة في الكتاب."
BAD_OPTIONS = "خيارات الإخراج غير صالحة."
BAD_KASHIDA = "قيمة الكشيدة غير معروفة."
BAD_COMMENTS = "خيار التعليقات يكون نعم أو لا."
COMMENTS_SUFFIX = " - مع التعليقات"


@dataclass(frozen=True)
class WordOptions:
    """The normalised options of one Word export."""

    kashida: str = KASHIDA_DEFAULT
    comments: bool = False

    @classmethod
    def parse(cls, data: dict | None) -> WordOptions:
        """Options from a request's `{kashida, comments}` (missing keys take the defaults, unknown keys are
        ignored). Raises `ValueError` with an Arabic message for a bad value."""
        data = data if isinstance(data, dict) else {}
        kashida = data.get("kashida", KASHIDA_DEFAULT)
        if kashida is None:
            kashida = KASHIDA_DEFAULT
        if not isinstance(kashida, str) or kashida not in KASHIDA_KEYS:
            raise ValueError(BAD_KASHIDA)
        comments = data.get("comments", False)
        if comments is None:
            comments = False
        if isinstance(comments, str):
            lowered = comments.strip().lower()
            if lowered in ("true", "1", "yes", "on"):
                comments = True
            elif lowered in ("false", "0", "no", "off", ""):
                comments = False
        if not isinstance(comments, bool):
            raise ValueError(BAD_COMMENTS)
        return cls(kashida=kashida, comments=comments)

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def jc(self) -> str:
        """The `w:jc` value of the justified styles."""
        return KASHIDA_JC[self.kashida]

    def text(self) -> str:
        """«كشيدة خفيفة · تعليقات» (the history's `options_text`)."""
        parts = [KASHIDA[self.kashida]["title"]]
        if self.comments:
            parts.append("تعليقات")
        return " · ".join(parts)


def kashida_choices() -> list[dict]:
    """The choices for the page's segmented control: `[{value, label, title, hint}]`."""
    return [{"value": key, **KASHIDA[key]} for key in KASHIDA_KEYS]


# ====================================================================== notes and warnings (§7)

# the known differences of a Word file for a book (`DocxExporter.notes`); `{}` fields are filled in
NOTES: dict[str, tuple[str, str]] = {
    "font_embedded": ("info", "خط أميري مضمَّن في الملف."),
    "font_not_embedded": (
        "info",
        "خط «{name}» غير مضمَّن في الملف (ترخيصه لا يسمح)؛ يلزم أن يكون مثبّتًا على الجهاز الذي يُفتح عليه.",
    ),
    "font_missing": (
        "warn",
        "الخط «{name}» غير مثبّت على هذا الجهاز؛ يُستعمل {fallback} بدلًا منه، كما في المعاينة.",
    ),
    # D98: an organisation's faces
    "font_embedded_org": ("info", "خط «{name}» من خطوط المؤسسة مضمَّن في الملف."),
    "font_not_embedded_org": (
        "info",
        "خط «{name}» من خطوط المؤسسة غير مضمَّن في الملف ({reason})؛ يلزم أن يكون مثبّتًا على الجهاز الذي"
        " يُفتح عليه.",
    ),
    "font_removed": (
        "warn",
        "حُذف الخط «{name}» من خطوط المؤسسة؛ يُستعمل {fallback} بدلًا منه، كما في المعاينة.",
    ),
    "toc_update": (
        "warn",
        "يرتّب Word صفحاته بنفسه، وقد تختلف أرقام المحتويات بصفحة؛ حدّثها في Word قبل الطباعة: "
        "زر الفأرة الأيمن على المحتويات ← تحديث الحقل.",
    ),
    "layout_first": (
        "info",
        "الصفحات أقدم من النص؛ تُرتَّب أولًا لأرقام المحتويات، فيطول الإخراج قليلًا.",
    ),
    "blank_versos": ("info", "الصفحة البيضاء قبل الفصل تحمل {what} في Word."),
    "source_pages": ("info", "أرقام الصفحات الأصلية في الهامش لا تُطبع في ملف Word."),
    "widows_approx": (
        "info",
        "يضبط Word الأرامل واليتامى بسطرين دائمًا؛ اختيار {n} لا ينتقل إلى الملف.",
    ),
}
# what a build reports about the file it made
WARNINGS: dict[str, tuple[str, str]] = {
    "comments_skipped": ("warn", "تعذّر وضع تعليق على {phrase} من الكلمات غير المؤكَّدة (تغيّر نصهما)."),
    "toc_numbers_missing": ("warn", "كُتبت المحتويات بلا أرقام صفحات؛ حدّثها في Word."),
    "comments": ("info", "{phrase} للمدقّق."),
}
FONTS_ACTION = {"label": "الخطوط", "url": "/books/{id}/layout/?tab=format"}


def fallback_name(used: str | None) -> str:
    """The face that stands in for a missing one, as the notes name it: «أميري», or «Times New Roman»
    (a missing Latin face falls back to Times first, `publishing.fonts.resolve`)."""
    used = used or "Amiri"
    return "أميري" if used == "Amiri" else f"«{used}»"


def blank_verso_shows(setup) -> str:
    """What Word prints on the blank page before a recto opening (it belongs to the previous section,
    whose header and footer it shows; the preview's is empty): «رقمها», «الترويسة», both, or '' when
    it prints nothing (§5.7)."""
    if setup.chapter_opening != "recto":
        return ""
    number = setup.page_number in ("bottom_center", "bottom_outer", "top_outer")
    header = setup.running_header in ("book", "chapter")
    if number and header:
        return "رقمها والترويسة"
    return "رقمها" if number else "الترويسة" if header else ""


def note(code: str, **values) -> dict:
    """A `{code, level, message}` row of `NOTES` or `WARNINGS` with its fields filled in."""
    level, message = NOTES.get(code) or WARNINGS[code]
    return {"code": code, "level": level, "message": message.format(**values) if values else message}


def words_phrase(count: int) -> str:
    """«كلمة واحدة», «كلمتين», «5 كلمات» (an object of a sentence, Western digits)."""
    if count == 1:
        return "كلمة واحدة"
    if count == 2:
        return "كلمتين"
    if 3 <= count <= 10:
        return f"{count} كلمات"
    return f"{count} كلمة"


def comments_phrase(count: int) -> str:
    """«تعليق واحد», «تعليقان», «12 تعليقًا»."""
    if count == 1:
        return "تعليق واحد"
    if count == 2:
        return "تعليقان"
    if 3 <= count <= 10:
        return f"{count} تعليقات"
    return f"{count} تعليقًا"


# ====================================================================== calibration conventions (§5.4, D62)

# C0: Word 2013+ layout (mode 15 shows no «وضع التوافق»); the harness measures 15 against 14.
COMPAT_MODE = 15
# C1 (and the Word-made file r1): with `mirrorMargins`, `pgMar/@left` is the inside margin, so odd pages
# bind on the right — an RTL book opens with a left-hand recto, as `css.py`'s `@page :left`.
MIRROR_PGMAR_LEFT = "inner"
# C10, r1: in a bidi paragraph `left` / `right` in `jc`, `ind` and tab stops mean start / end.
BIDI_LEFT_IS_START = True
# C12, measured 2026-09-26 (`word_check --c12`, Word 16.113, mode 15): 60 pairs of 10 mm after + 10 mm
# before took 8 pages, the "larger of the two" count, not the 11 of a sum. Word collapses paragraph
# spacing to the larger value, as CSS does; a direct value is written only where CSS differs (negative
# margins, an override such as the imprint's 30 mm).
SPACING_ADDS = False
# Harness line counts: the call is 0.62 of the body and WeasyPrint's `super` raises it by half its own
# size, so `w:position` = 0.31 × body (8 half-points at 13 pt).
CALL_RAISE = 0.31
# C6: comments anchored inside footnote text (else on the note's call in the body).
COMMENTS_IN_NOTES = True
# C8: the Arabic-Indic number format, used only if the owner asks for Arabic-Indic page numbers (Q3: no).
ARABIC_INDIC_FORMAT = "hindiNumbers"

# The Word style ids of the model's styles (§4.3): `publishing.model.STYLES[...].word` must agree.
CALIBRATION_ITEMS: tuple[str, ...] = (
    "C1",
    "C2",
    "C3",
    "C4",
    "C5",
    "C6",
    "C7",
    "C9",
    "C10",
    "C11",
    "C13",
    "C14",
    "C15",
)
