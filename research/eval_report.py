"""The results page of the evaluation (`research.evaluation`): the report dict as Arabic Markdown.

Numbers come from the report; the sentences around them are fixed, except `FINDINGS`, a reading of the
failures of the run it names (written by hand after looking at them). Western digits throughout; «،» between
clauses.
"""

from __future__ import annotations

from collections.abc import Sequence

from .evaluation import MAIN_BAND, SENSITIVITY, SHORT_BANDS, STRENGTH, SYSTEMS

CLASS_LABELS: dict[str, str] = {
    "exact_body": "متن حرفيّ",
    "exact_notes": "حاشية حرفيّة",
    "orthography": "إملاء الناس",
    "replaced_word": "كلمة مبدَّلة",
    "dropped_word": "كلمة محذوفة",
    "swapped_words": "كلمتان متبادلتان",
    "vowel_change": "حركة مغيَّرة",
    "misattributed_note": "حاشية منسوبة للمؤلف",
    "misattributed_body": "متن منسوب للمحقق",
    "absent": "ليست في الكتب",
    "ocr_doubt": "قراءة OCR مشكوك فيها",
    "replaced_2": "كلمتان مبدَّلتان",
    "replaced_3": "3 كلمات مبدَّلة",
    "replaced_4": "4 كلمات مبدَّلة",
    "correct": "الصحيحة كلها",
    "altered": "المعدَّلة كلها",
    "misattributed": "المنسوبة خطأً كلها",
}
SYSTEM_LABELS: dict[str, str] = {
    "nassakh": "نسّاخ",
    "nassakh_no_doubt": "نسّاخ بلا ميزة الشك",
    "raw_search": "بحث حرفي في النص الخام (Ctrl+F)",
    "normalized_search": "بحث حرفي في النص المطبَّع",
}
SHORT_SYSTEMS: dict[str, str] = {
    "nassakh": "نسّاخ",
    "nassakh_no_doubt": "نسّاخ بلا الشك",
    "normalized_search": "بحث مطبَّع",
    "raw_search": "بحث خام (Ctrl+F)",
}
METRIC_LABELS: dict[str, str] = {
    "accepted": "مقبولة",
    "false_alarm": "إنذار كاذب",
    "location": "الكتاب والصفحة صحيحان",
    "attribution_ok": "النسبة سليمة",
    "detected": "كُشفت",
    "flagged": "نُبّه إليها",
    "located": "موضع الكلمة صحيح",
    "named": "الفرق في الحركات مسمّى",
    "says_not_found": "قال «غير موجود»",
    "caught": "النسبة كُشفت",
    "found": "وُجدت",
    "correct": "«لم يوجد» صحيحة",
    "false_match": "نُسبت إلى مقطع",
    "needs_image": "«يحتاج مطابقة مع الصورة»",
    "false_accusation": "اتُّهمت زورًا",
    "lost": "قيل «لم يوجد»",
}
STATUS_LABELS: dict[str, str] = {
    "exact": "مطابق",
    "differs": "يختلف",
    "needs_image_check": "يحتاج مطابقة مع الصورة",
    "not_found": "لم يوجد",
}

FINDINGS: str = """\
**قراءة الإخفاقات** (تشغيل 5 أكتوبر 2026، البذور 1 إلى 3؛ قرأنا الأمثلة بأيدينا):

- «ليست في الكتب»، من 8 إلى 14 كلمة: إخفاق واحد من 180. عبارة تاريخية فيها صيغة تأريخ شائعة \
(«لاثنتي عشرة ليلة خلت من المحرم») طابقت مقطعًا فيه الصيغة نفسها بنسبة 0.625، فأجاب نسّاخ «يختلف» وعدّد \
الفروق بدل «لم يوجد». لا يتهم أحدًا بالاختلاق، لكنه نسب العبارة إلى مقطع ليست منه.
- «ليست في الكتب»، من 4 إلى 5 كلمات: نحو 6% تُنسب إلى مقطع قريب، ونحو 1% في عبارات 6 إلى 7 كلمات. \
هي سلاسل إسناد وأسماء («عن أسامة بن زيد عن أبيه»، «أبو عبد الله عبد الرحمن بن»): ثلاث كلمات من خمس \
تتجاوز عتبة 0.6. البحث الحرفي أدق من نسّاخ هنا.
- أربع كلمات مبدَّلة من 8 إلى 14: يقول نسّاخ «لم يوجد» في نحو ثلثها لأن تطابق الكلمات ينزل إلى 0.50 \
أو 0.53 تحت العتبة. كلمتان وثلاث تُكشفان كلها. العتبة اختيار: يفضّل «لم يوجد» على نسبة عبارة بعيدة \
إلى مقطع.
- كلمتان متبادلتان في أول العبارة (إخفاقان من 180): الكلمة المقدَّمة تظهر «مضافة» لا «مبدَّلة»، وهي \
بجوار كلمة مشكوك فيها، فجاء الجواب «يحتاج مطابقة مع الصورة» لا «يختلف». لم يفلت التعديل (نُبّه إليه \
في كل الحالات) لكن الحكم أخفّ.
- «قراءة OCR مشكوك فيها»: إخفاق واحد من 180، اتُّهمت فيه العبارة. القراءة البديلة للكلمة المشكوك فيها \
(«يُحْيِيهِ») هي نفسها كلمة تليها بكلمتين في السطر، فاختارت المحاذاة الكلمة التالية وصار الفرق على كلمة \
ثابتة. مصادفة في الصنع، لكن مثلها يقع في النصوص المكررة.
- حذف كلمة من عبارة قصيرة (4 إلى 5): إخفاقان من 180، والصفحة غير صحيحة في نحو 7% (انظر الجدول). \
في أحدهما صارت العبارة بعد الحذف («الله عنه عن») عبارة حرفية في صفحة أخرى فأجاب «مطابق» لها؛ هذا غموض \
في العبارة القصيرة لا خطأ في الأداة.
"""


# ====================================================================== cells


def rate(report: dict, key: str, system: str, metric: str) -> dict | None:
    cell = report["aggregate"]["rates"].get(key, {}).get(system, {}).get(metric)
    return cell if cell and "mean" in cell else None


def _p(x: float) -> str:
    p = x * 100
    if p < 1e-9 or p > 100 - 1e-9:
        return f"{p:.0f}"
    return f"{p:.1f}" if p < 10 or p > 90 else f"{p:.0f}"


def pct(cell: dict | None, spread: bool = True) -> str:
    """«93% (90–95)»: the mean over seeds and the lowest and highest seed; «—» when the system has none."""
    if cell is None:
        return "—"
    text = f"{_p(cell['mean'])}%"
    if spread and abs(cell["max"] - cell["min"]) > 1e-9:
        text += f" ({_p(cell['min'])}–{_p(cell['max'])})"
    return text


def share(report: dict, key: str, system: str, status: str) -> float:
    return report["aggregate"]["status"].get(key, {}).get(system, {}).get(status, 0.0)


def table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def n_of(report: dict, key: str, metrics: Sequence[str]) -> int:
    """Cases of a class: the n of its first metric that some system has."""
    for metric in metrics:
        for system in SYSTEMS:
            cell = rate(report, key, system, metric)
            if cell:
                return int(cell["n"])
    return 0


# ====================================================================== sections


def summary(report: dict) -> str:
    meta, corpus = report["meta"], report["corpus"]
    seeds, n = len(meta["seeds"]), meta["n"]
    cases = report["aggregate"]["cases"]
    main = round(sum(c["n_per_seed"] for key, c in cases.items() if "@" not in key and key not in STRENGTH))
    extra = round(sum(c["n_per_seed"] for key, c in cases.items() if "@" in key or key in STRENGTH))
    return (
        f"قسنا أداة التحقق من الاقتباس في نسّاخ على عبارات قُصّت من ثلاثة كتب مفهرسة "
        f"({corpus['pages']} صفحة، {corpus['words']:,} كلمة): {len(meta['classes'])} صنفًا من العبارات، "
        f"في كل صنف {n} عبارة من 8 إلى 14 كلمة، في كل من {seeds} بذور، أي {main * seeds:,} عبارة، "
        f"و{extra * seeds:,} عبارة أخرى لفحص أثر الطول ودرجة التغيير. لكل عبارة حقيقة معروفة من طريقة "
        f"صنعها: حرفية، أو بإملاء الناس، أو بكلمة مبدَّلة أو محذوفة أو مقدَّمة، أو بحركة مغيَّرة، أو "
        f"منسوبة لغير قائلها، أو من كتب ليست في الحساب، أو مبنية على قراءة بديلة تركها نموذج OCR. "
        f"استدعينا خدمة التحقق الحقيقية بصفة عضو عادي في حساب لا يضم إلا هذه الكتب، وقارنّا جوابها "
        f"على العبارات نفسها بجواب «بحث حرفي في النص الخام»، وهو ما يفعله Ctrl+F في نص PDF وهو البديل "
        f"المسمّى، وبجواب بحث حرفي في النص المطبَّع، وبجواب نسّاخ نفسه بعد إيقاف ميزة «يحتاج مطابقة "
        f"مع الصورة»."
    )


def headline(report: dict) -> str:
    """Five rows a reader takes in at a glance. `unable`: the systems that cannot answer the row's question
    (a search says nothing of where a word differs or of who wrote the passage), shown as what they lack."""
    searches = ("normalized_search", "raw_search")

    def row(label: str, key: str, metric: str, unable: Sequence[str] = (), lacks: str = "") -> list[str]:
        return [label] + [
            lacks if system in unable else pct(rate(report, key, system, metric), spread=False)
            for system in SHORT_SYSTEMS
        ]

    rows = [
        row("عبارة صحيحة مكتوبة بإملاء الناس: قُبلت", "orthography", "accepted"),
        row("كلمة مبدَّلة أو محذوفة أو مقدَّمة: كُشفت وحُدّد موضعها", "altered", "located", searches, "لا يحدد"),
        row(
            "حاشية نُسبت إلى المؤلف أو متن إلى المحقق: كُشفت النسبة",
            "misattributed",
            "caught",
            searches,
            "لا يعرف",
        ),
        row("عبارة صحيحة وقراءة OCR مشكوك فيها: اتُّهمت زورًا (الأقل أفضل)", "ocr_doubt", "false_accusation"),
        row("عبارة ليست في الكتب: أُجيب «لم يوجد»", "absent", "correct"),
    ]
    return table(["", *SHORT_SYSTEMS.values()], rows)


def headline_notes(report: dict) -> str:
    def v(key: str, system: str, metric: str) -> str:
        return pct(rate(report, key, system, metric), spread=False)

    lost = rate(report, "replaced_4", "nassakh", "lost")
    notes = [
        "- الأرقام متوسط البذور، ومداها بين البذور في الجداول الكاملة.",
        "- البحثان جوابهما «وُجد» أو «لم يوجد». يقولان «لم يوجد» لكل عبارة معدَّلة، وهو جوابهما نفسه "
        "لعبارة من كتاب آخر، ولا يدلان على الكلمة؛ فلم نعدّ ذلك كشفًا مع تحديد الموضع.",
        "- البحث الحرفي في النص الخام يرفض العبارات المكتوبة بإملاء آخر "
        f"({v('orthography', 'raw_search', 'false_alarm')} إنذارًا كاذبًا)، وهذا ما تفعله Ctrl+F. "
        "البحث في النص المطبَّع يقبلها كنسّاخ لأنه يستعمل التطبيع نفسه، ويكشف التعديل بقوله «لم يوجد».",
        "- ما يضيفه نسّاخ على البحث المطبَّع: موضع الكلمة المعدَّلة؛ والحركة المغيَّرة، "
        f"وقد سمّاها في {v('vowel_change', 'nassakh', 'named')} من الحالات (البحث الخام يقول «لم يوجد» "
        "والمطبَّع يقول «وُجد» ولا يعرف أن الحركة تغيّرت)؛ والنسبة إلى المؤلف أو المحقق؛ وألا يتهم الاقتباس "
        "عند كلمة شكّ فيها OCR.",
        "- يخسر نسّاخ أمام البحث الحرفي في «ليست في الكتب»، وهو لا يخطئ فيها: "
        f"{v('absent', 'nassakh', 'correct')} صحيحًا في عبارات الـ8–14 كلمة، "
        f"و{v('absent@4-5', 'nassakh', 'correct')} في عبارات الـ4–5 كلمات.",
    ]
    if lost:
        notes.append(
            f"- وحين تتغيّر أربع كلمات من عبارة من 8–14 كلمة لا يعدّها نسّاخ من المقطع نفسه في "
            f"{pct(lost, False)} من الحالات، ويقول «لم يوجد» (عتبة تطابق الكلمات 0.6)."
        )
    return "\n".join(notes)


def corpus_section(report: dict) -> str:
    corpus, meta = report["corpus"], report["meta"]
    titles = meta.get("titles", {})
    rows = [
        [str(book), titles.get(str(book), ""), f"{corpus['by_book'].get(str(book), 0):,}"]
        for book in meta["books"]
    ]
    absent = "، ".join(str(book) for book in meta["absent_books"])
    text = (
        f"{table(['الكتاب', 'العنوان', 'الكلمات المفهرسة'], rows)}\n\n"
        f"- الكلمات المشكوك فيها (`doubtful`): {corpus['doubtful_words']:,} "
        f"({_p(corpus['doubtful_share'])}%)؛ المشكولة: {_p(corpus['vowelled_share'])}% من الكلمات؛ "
        f"مفردات الكتب: {corpus['vocabulary']:,} كلمة.\n"
        f"- العبارات غير الموجودة من نص الكتب {absent} ({corpus['absent_rows']} صفحة ونوعًا)؛ "
        f"حُذفت منها كل عبارة وردت حرفيًا في الكتب المقيسة.\n"
        f"- حصة العبارات المشكولة في كل صنف:"
    )
    cases = report["aggregate"]["cases"]
    if "exact_body" in cases:
        text = text.replace(
            "- حصة العبارات المشكولة",
            f"- {_p(cases['exact_body']['doubt_share'])}% من العبارات الحرفية من المتن فيها كلمة مشكوك فيها "
            "على الأقل: هذا ما تلقاه عبارة من نحو 11 كلمة من هذه الكتب.\n- حصة العبارات المشكولة",
        )
    shares = [
        f"{CLASS_LABELS[cls]} {_p(cases[cls]['vowelled_share'])}%" for cls in meta["classes"] if cls in cases
    ]
    return text + " " + "، ".join(shares) + "."


def full_tables(report: dict) -> str:
    def block(title: str, classes: Sequence[str], metrics: Sequence[str], systems=SYSTEMS) -> str:
        rows = []
        for cls in classes:
            for system in systems:
                cells = [rate(report, cls, system, metric) for metric in metrics]
                if all(cell is None for cell in cells):
                    continue
                rows.append(
                    [CLASS_LABELS[cls], SYSTEM_LABELS[system], str(n_of(report, cls, metrics))]
                    + [pct(cell) for cell in cells]
                )
        header = ["العبارات", "النظام", "ن"] + [METRIC_LABELS[m] for m in metrics]
        return f"### {title}\n\n{table(header, rows)}"

    parts = [
        block(
            "عبارات صحيحة",
            ["exact_body", "exact_notes", "orthography", "correct"],
            ["accepted", "false_alarm", "location", "attribution_ok"],
        ),
        block(
            "عبارات معدَّلة",
            ["replaced_word", "dropped_word", "swapped_words", "altered"],
            ["detected", "located", "flagged", "location"],
        ),
        block("الحركات", ["vowel_change"], ["named", "says_not_found", "location"]),
        block(
            "النسبة إلى المؤلف أو المحقق",
            ["misattributed_note", "misattributed_body", "misattributed"],
            ["caught", "found", "location"],
        ),
        block("عبارات ليست في الكتب", ["absent"], ["correct", "false_match"]),
        block(
            "عبارات صحيحة اختلفت عن النص عند كلمة مشكوك فيها",
            ["ocr_doubt"],
            ["needs_image", "false_accusation", "location"],
        ),
    ]
    return "\n\n".join(parts)


def how_to_read(report: dict) -> str:
    return (
        "- «ن»: عدد العبارات في الخلية على كل البذور. الخلية متوسط البذور، وبين القوسين أدنى بذرة وأعلاها "
        "(تُحذف الأقواس إذا تطابقت).\n"
        "- في البحثين: «كُشفت» و«نُبّه إليها» تعنيان «لم يوجد»، فالبحث لا يقول سوى وُجد أو لم يوجد؛ "
        "و«اتُّهمت زورًا» تعني «لم يوجد» لعبارة صحيحة. و«النسبة كُشفت» و«الفرق في الحركات مسمّى» "
        "و«موضع الكلمة صحيح» لا معنى لها في بحث، فهي صفر.\n"
        "- «الكتاب والصفحة صحيحان»: دلّ النظام على الصفحة التي قُصّت منها العبارة، أو على صفحة أخرى فيها "
        "العبارة نفسها حرفيًا (المقاطع المكررة)."
    )


def sweeps(report: dict) -> str:
    rows = []
    for cls in SENSITIVITY:
        for band in (*SHORT_BANDS, MAIN_BAND):
            key = cls if band == MAIN_BAND else f"{cls}@{band[0]}-{band[1]}"
            primary = {"exact_body": "accepted", "orthography": "accepted", "absent": "correct"}.get(
                cls, "detected"
            )
            ours = rate(report, key, "nassakh", primary)
            if ours is None:
                continue
            where = rate(report, key, "nassakh", "located") if primary == "detected" else None
            found = rate(report, key, "nassakh", "location")
            rows.append(
                [
                    CLASS_LABELS[cls],
                    f"{band[0]}–{band[1]}",
                    str(int(ours["n"])),
                    pct(ours),
                    pct(where) if where else "—",
                    pct(found),
                    pct(rate(report, key, "raw_search", primary)),
                ]
            )
    lengths = table(
        ["العبارات", "الكلمات", "ن", "نسّاخ", "موضع الكلمة صحيح", "الكتاب والصفحة صحيحان", "بحث خام"], rows
    )
    strength_rows = []
    for key in ("replaced_word", *STRENGTH):
        if key not in report["aggregate"]["status"]:
            continue
        strength_rows.append(
            [
                CLASS_LABELS[key],
                str(n_of(report, key, ["detected"])),
                pct({"mean": share(report, key, "nassakh", "differs"), "min": 0, "max": 0}, False),
                pct({"mean": share(report, key, "nassakh", "not_found"), "min": 0, "max": 0}, False),
                pct({"mean": share(report, key, "nassakh", "needs_image_check"), "min": 0, "max": 0}, False),
                pct({"mean": share(report, key, "raw_search", "not_found"), "min": 0, "max": 0}, False),
            ]
        )
    strength = table(
        [
            "التغيير (في عبارة من 8 إلى 14 كلمة)",
            "ن",
            "نسّاخ: يختلف",
            "نسّاخ: لم يوجد",
            "نسّاخ: يحتاج صورة",
            "بحث خام: لم يوجد",
        ],
        strength_rows,
    )
    return (
        "### طول العبارة\n\n"
        "الأصناف الخمسة نفسها بعبارات أقصر. الأقصر أضعف: ثلاث كلمات من أربع أو خمس تكفي لتبلغ عتبة "
        "التطابق (0.6) مع مقطع آخر. في عمود «بحث خام»: «كُشفت» و««لم يوجد» صحيحة» بمعناهما في بحث.\n\n"
        + lengths
        + "\n\n### درجة التغيير\n\n"
        "كلمات تُبدَّل من الكتب نفسها في عبارة واحدة: متى يتوقف نسّاخ عن عدّها اقتباسًا معدَّلًا من هذا المقطع "
        "ويقول «لم يوجد» (عتبة تطابق الكلمات 0.6).\n\n" + strength
    )


def speed_section(report: dict) -> str:
    latency = report["aggregate"]["latency"]
    rows = []
    for system in SYSTEMS:
        median, p95 = latency[system]["median_ms"], latency[system]["p95_ms"]
        if system == "nassakh_no_doubt":
            continue
        digits = 0 if median["mean"] >= 10 else 2
        rows.append(
            [
                SYSTEM_LABELS[system],
                f"{median['mean']:.{digits}f}",
                f"{p95['mean']:.{digits}f}",
            ]
        )
    out = (
        "### السرعة\n\n"
        + table(["النظام", "الوسيط (ms)", "المئين 95 (ms)"], rows)
        + "\n\nنسّاخ يقرأ من PostgreSQL ويتحقق من المقطع ويبني الإحالة ورابط الصورة؛ البحثان يعملان على نص في "
        "الذاكرة، فلا مقارنة بين الزمنين، وهما هنا لبيان التكلفة لا للتفضيل."
    )
    info = report["index"]
    size = (info["words_json_bytes"] + info["norm_chars"] * 2) / 1_048_576
    lines = [
        f"- الفهرس: {info['rows']:,} سطرًا (صفحة × نوع)، {info['words']:,} كلمة، نحو {size:.1f} م.ب "
        f"(الكلمات بصيغة JSON والنص المطبَّع)."
    ]
    build = info.get("build")
    if build:
        lines.append(
            f"- بناؤه من الأسطر: {build['seconds']:.1f} ثانية لـ {build['pages']} صفحة "
            f"({build['pages'] / max(build['seconds'], 0.01):.0f} صفحة في الثانية)، على {info['vendor']}."
        )
    return out + "\n\n### الفهرس\n\n" + "\n".join(lines)


def repeat_section(report: dict) -> str:
    repeat = report.get("repeatability")
    if not repeat:
        return "لم يُجرَ فحص التكرار في هذا التشغيل."
    same = "العبارات نفسها" if repeat["same_cases"] else "العبارات اختلفت"
    text = (
        f"البذرة {repeat['seed']} شُغّلت مرتين: {same}، وإجابات نسّاخ (الحالة والنسبة والمقطع والتغييرات "
        f"والنسبة إلى قائل) متطابقة في {repeat['identical']} من {repeat['cases']} عبارة. "
        "البحثان دالّتان خالصتان فلا تتغيران."
    )
    if repeat["differing"]:
        text += " المختلف:\n" + "\n".join(
            f"  - {item['class']}: {item['quote'][:60]}" for item in repeat["differing"]
        )
    seeds = report["meta"]["seeds"]
    return (
        text + f"\n\nالبذور {'، '.join(map(str, seeds))} تعطي مجموعات عبارات مختلفة؛ "
        "والمدى في الجداول هو بين البذور."
    )


def failure_section(report: dict, findings: str = "") -> str:
    failures = report["failures"]
    rows = []
    rates = report["aggregate"]["rates"]
    from .evaluation import PRIMARY

    for key in sorted(failures, key=lambda k: ("@" in k, k)):
        base = key.split("@")[0]
        cell = rates.get(key, {}).get("nassakh", {}).get(PRIMARY[base])
        total = int(cell["n"]) if cell else 0
        missed = total - int(cell["hits"]) if cell else len(failures[key])
        label = CLASS_LABELS[base] + (f" ({key.split('@')[1].replace('-', '–')} كلمات)" if "@" in key else "")
        for item in failures[key][:3]:
            note = f"{item['note']}؛ " if item["note"] else ""
            rows.append(
                [
                    label,
                    f"{missed} من {total}",
                    item["quote"][:70],
                    f"{note}{STATUS_LABELS.get(item['status'], item['status'])}، النسبة {item['ratio']}",
                ]
            )
    quiet = [
        CLASS_LABELS[cls]
        for cls in report["meta"]["classes"]
        if cls not in failures and rate(report, cls, "nassakh", PRIMARY[cls]) is not None
    ]
    out = "أمثلة مما أخطأ فيه نسّاخ (حتى ثلاثة لكل صنف، من كل البذور):\n\n"
    out += (
        table(["الصنف", "الإخفاقات", "العبارة", "جواب نسّاخ"], rows)
        if rows
        else "لا إخفاق في الأصناف الأساسية."
    )
    if quiet:
        out += "\n\nلا إخفاق في: " + "، ".join(quiet) + "."
    if findings:
        out += "\n\n" + findings
    return out


LIMITS = """\
- العبارات مقصوصة من نص OCR نفسه الذي يحمله الفهرس. لذلك نسب القبول والموضع في العبارات الحرفية صحيحة بحكم \
الصنع إلى حد كبير، وهي تقيس أن مسار البحث والمطابقة يرجع إلى ما فُهرس، لا أن النص المفهرس صحيح.
- أخطاء OCR التي لم يعلّم عليها النظام بالشك تبدو للأداة اختلافًا: عبارة صحيحة تُقابَل بكلمة مقروءة خطأ بثقة \
تُجاب «يختلف». لم نقس تواترها لأننا لا نملك صفحات مدقَّقة باليد.
- ثلاثة كتب فقط (تاريخ ليبيا وولاة طرابلس ومختصر صحيح البخاري)، بخط مطبوع واحد تقريبًا؛ لم نقس مخطوطات \
ولا شعرًا ولا كتبًا أخرى، وقد تختلف النتائج فيها.
- التعديلات اصطناعية: كلمة من مفردات الكتب بطول قريب، أو حذف، أو تقديم. الاقتباس الخاطئ الحقيقي أعقد \
(نقل بالمعنى، أو ذاكرة تخلط نصين) ولا تمثّله هذه الأصناف.
- لا مجموعة موسومة بيد بشر. الحقيقة المعروفة من طريقة الصنع. وفي «قراءة OCR المشكوك فيها» نفترض أن \
القراءة التي تركها النموذج الآخر قد تكون الصحيحة؛ لا نعرف أيّ القراءتين صحيحة. فالصنف يقيس ألا يُتَّهم \
الاقتباس عند موضع شكّ OCR نفسه، لا دقة OCR، والفرق بين نسّاخ ونسّاخ بلا ميزة الشك فيه مبني على الصنع: \
كل عبارة فيه فيها كلمة مشكوك فيها. أثره في اقتباس حقيقي يتوقف على عدد المرات التي تُخطئ فيها القراءة \
المشكوك فيها فعلًا، ولا نعرفه.
- البديل المسمّى بحث نصي بسيط. لم نقارن بحثًا تقريبيًا ولا بحثًا بالتضمين ولا نموذجًا لغويًا يُسأل عن \
الاقتباس؛ هذه مقارنة قادمة.
- العبارات داخل صفحة واحدة. المقطع العابر لحدّ صفحتين تغطيه اختبارات الوحدة (`research/test_services.py`) \
ولا يغطيه هذا القياس. والتعديل لا يقع على كلمة مشكوك فيها (هذا صنف «قراءة OCR»)، ولا على أول العبارة \
وآخرها في الحذف (حذف الطرف يترك عبارة حرفية أقصر).
- الزمن مقاس على جهاز التطوير وPostgreSQL محلية وثلاثة كتب؛ يكبر مع كتب الحساب لأن المرشَّحات تُجلب من \
الفهرس كله.
"""


def render(report: dict, findings: str | None = None) -> str:
    """The results page: summary, headline table, the full tables, failures, how to rerun, the limits."""
    meta = report["meta"]
    books = " ".join(str(b) for b in meta["books"])
    absent = " ".join(str(b) for b in meta["absent_books"])
    command = (
        f".venv/bin/python manage.py research_eval --books {books} --absent-books {absent} "
        f"--seeds {len(meta['seeds'])} --n {meta['n']} --out docs/CHALLENGE_RESULTS.md "
        "--json /tmp/research_eval.json"
    )
    sections = [
        "# قياس التحقق من الاقتباس في نسّاخ",
        f"شُغّل في {meta['generated']}.",
        summary(report),
        "## الخلاصة",
        headline(report),
        headline_notes(report),
        "## ما قيس",
        corpus_section(report),
        "## الجداول الكاملة",
        how_to_read(report),
        full_tables(report),
        sweeps(report),
        speed_section(report),
        "### التكرار",
        repeat_section(report),
        "## الإخفاقات",
        failure_section(report, FINDINGS if findings is None else findings),
        "## إعادة التشغيل",
        f"```\n{command}\n```\n\n"
        "العبارات تُولَّد من البذرة (`random.Random`)، فالتشغيل نفسه يعطي العبارات نفسها. الأمر لا يغيّر "
        "البيانات: ينشئ مؤسسة ومستخدمًا ويُلحق بها الكتب المقيسة داخل معاملة تُلغى عند الانتهاء (وقد يجدّد "
        "الفهرس إن كان قديمًا). `--json` يحفظ كل الأرقام وأمثلة الإخفاق.",
        "## حدود القياس",
        LIMITS,
    ]
    return "\n\n".join(sections) + "\n"
