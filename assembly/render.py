"""Manuscript document → HTML (PHASE4_SPEC §4.4), plus the view models of the manuscript view.

`render_document` turns the ProseMirror JSON of `editor.Manuscript.document` into the static markup the
manuscript view shows (and Phase 6 will export): every text is escaped, nothing inside carries Alpine
bindings, one `<section>` per chapter so the browser skips the chapters off screen (`content-visibility`).
The other functions build what the side panel and the toolbar show from the same payload: the outline
(table of contents), the warnings grouped by code, the stats rows and the counts line.

Contract (§4.4): `article.ms-doc` > `header.ms-title`, `section.ms-chapter[data-chapter]` > `h2.ms-h1` /
`h3.ms-h2` / `p.ms-p` with `data-block`, `data-pages`, `data-lines`, `data-reviewed` (and `data-suggested`
when a heading is suggested); inline `button.ms-ref[data-note]` (the number), `span.ms-seam[data-page]
[data-mode][data-decision]`, `mark.ms-uncertain`; `div.ms-split[data-page][data-mode][data-decision]`
between blocks at split and missing seams; `ol.ms-notes` > `li.ms-note[data-note][data-orphan]` after
each chapter. Western digits inside `<bdi>`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape

NOTE_MODES: tuple[str, ...] = ("chapter", "book", "none")
UNREVIEWED_TITLE = "من صفحة لم تُراجَع بعد"

# Arabic count forms: (one, two, few 3–10, many 11+ / 100 / 101 …), see `ar_count`.
PAGES = ("صفحة واحدة", "صفحتان", "صفحات", "صفحة")
CHAPTERS = ("فصل واحد", "فصلان", "فصول", "فصلًا")
PARAGRAPHS = ("فقرة واحدة", "فقرتان", "فقرات", "فقرة")
FOOTNOTES = ("حاشية واحدة", "حاشيتان", "حواشٍ", "حاشية")
NOTES = ("ملاحظة واحدة", "ملاحظتان", "ملاحظات", "ملاحظة")

# Warning codes (§2.9) → Arabic group labels, in the order the side panel lists them: warnings, then info.
WARNING_LABELS: dict[str, str] = {
    "marker_unmatched": "علامات بلا حاشية",
    "note_orphan": "حواشٍ بلا علامة في المتن",
    "note_marker_missing": "حواشٍ بلا علامة رُبطت بموضعها",
    "stray_note": "فقرات تبدأ بعلامة حاشية",
    "page_pending": "صفحات قيد المعالجة لم تُضمَّن",
    "page_error": "صفحات متعطّلة لم تُضمَّن",
    "page_unreviewed": "صفحات لم تُراجَع بعد",
    "uncertain_words": "كلمات غير مؤكَّدة",
    "empty_page": "صفحات بلا نص",
    "running_head": "ترويسات حُذفت",
    "no_headings": "لا عناوين رئيسية",
}
STAT_LABELS: tuple[tuple[str, str], ...] = (
    ("pages_included", "الصفحات المضمَّنة"),
    ("pages_unreviewed", "منها لم تُراجَع"),
    ("pages_skipped", "صفحات متجاوَزة"),
    ("chapters", "الفصول"),
    ("headings", "العناوين"),
    ("paragraphs", "الفقرات"),
    ("footnotes", "الحواشي"),
    ("joins", "فقرات موصولة عبر الصفحات"),
    ("words", "الكلمات"),
)
_RE_MARKER = re.compile(r"«(.+?)»")


def ar_count(n: int, forms: tuple[str, str, str, str]) -> str:
    """`n` with its Arabic noun: «صفحة واحدة», «صفحتان», «5 صفحات», «214 صفحة», «103 صفحات».

    `forms` = (one, two, few, many); the units of a number above 100 decide between few (3–10) and
    many, as Arabic counts do. Western digits (D6).
    """
    n = int(n)
    if n == 1:
        return forms[0]
    if n == 2:
        return forms[1]
    units = n % 100
    if 3 <= units <= 10:
        return f"{n} {forms[2]}"
    return f"{n} {forms[3]}"


def pages_label(pages: list[int]) -> str:
    """«12» for one page, «12–13» for a run of pages (first and last; en dash)."""
    numbers = sorted({int(p) for p in pages if isinstance(p, int) or str(p).isdigit()})
    if not numbers:
        return ""
    if len(numbers) == 1:
        return str(numbers[0])
    return f"{numbers[0]}–{numbers[-1]}"


def _join_pages(pages: list[int]) -> str:
    """«14» · «14 و15» · «14، 15 و16» for the pages a missing seam skipped."""
    numbers = [str(p) for p in pages]
    if len(numbers) <= 1:
        return "".join(numbers)
    return "، ".join(numbers[:-1]) + " و" + numbers[-1]


def _attr(value) -> str:
    return escape(str(value), quote=True)


def _ints(values) -> list[int]:
    out: list[int] = []
    for value in values or []:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


# ====================================================================== rendering


@dataclass
class _Ctx:
    """State carried while rendering one document."""

    seams: dict[int, dict]
    notes_mode: str
    unmatched: dict[str, list[str]]
    chapter_notes: list[dict] = field(default_factory=list)  # footnote nodes met in the open chapter
    book_notes: list[dict] = field(default_factory=list)
    block_id: str = ""
    # page number → reviewed now (D70): the amber mark follows the live status; None → the block attribute
    reviewed: dict[int, bool] | None = None


def _is_reviewed(pages: list[int], attrs: dict, live: dict[int, bool] | None) -> bool:
    """Whether a block reads as reviewed (D35's amber mark when not): from the live status of its source pages
    when it has any and the status is known (a page left out of the book since keeps the block's attribute),
    else from the attribute the assembly stored."""
    stored = bool(attrs.get("reviewed", True))
    if live is None or not pages:
        return stored
    return all(live.get(page, stored) for page in pages)


def _text_html(node: dict, ctx: _Ctx) -> str:
    text = escape(node.get("text") or "", quote=True)
    if any(isinstance(m, dict) and m.get("type") == "uncertain" for m in node.get("marks") or []):
        return f'<mark class="ms-uncertain">{text}</mark>'
    for marker in ctx.unmatched.get(ctx.block_id, []):
        for wrapped in (f"({marker})", f"[{marker}]"):
            probe = escape(wrapped, quote=True)
            if probe in text:
                return text.replace(probe, f'<span class="ms-unmatched">{probe}</span>', 1)
    return text


def _seam_html(attrs: dict, ctx: _Ctx) -> str:
    """The inline join marker of a `pageBreak` node: a hairline with the new page's number. A button
    (click / Enter) opening the seam menu of the view: split here, join, back to the automatic decision."""
    page = int(attrs.get("page") or 0)
    seam = ctx.seams.get(page, {})
    from_page = seam.get("from_page") or page - 1
    decision = seam.get("decision") or "auto"
    reason = seam.get("reason") or ""
    printed = attrs.get("printed") or ""
    label = f"وُصلت الفقرة بين الصفحتين {from_page} و{page}"
    return (
        f'<span class="ms-seam" role="button" tabindex="0" data-page="{page}" data-from="{_attr(from_page)}"'
        f' data-mode="join" data-decision="{_attr(decision)}" data-reason="{_attr(reason)}"'
        f' data-printed="{_attr(printed)}" aria-haspopup="menu" aria-label="{_attr(label)}">'
        f"<bdi>{page}</bdi></span>"
    )


def _ref_html(node: dict, ctx: _Ctx) -> str:
    attrs = node.get("attrs") or {}
    note_id = str(attrs.get("id") or "")
    number = attrs.get("number")
    number = int(number) if isinstance(number, int) else _attr(number or "")
    page = attrs.get("sourcePage") or ""
    orphan = bool(attrs.get("orphan"))
    label = f"الحاشية {number}" + (" (بلا علامة في المتن)" if orphan else "")
    classes = "ms-ref is-orphan" if orphan else "ms-ref"
    return (
        f'<button type="button" class="{classes}" id="ref-{_attr(note_id)}" data-note="{_attr(note_id)}"'
        f' data-number="{number}" data-page="{_attr(page)}" data-orphan="{"true" if orphan else "false"}"'
        f' aria-label="{_attr(label)}" aria-controls="note-{_attr(note_id)}"><bdi>{number}</bdi></button>'
    )


def _inline_html(content: list, ctx: _Ctx) -> str:
    parts: list[str] = []
    for node in content or []:
        if not isinstance(node, dict):
            continue
        kind = node.get("type")
        if kind == "text":
            parts.append(_text_html(node, ctx))
        elif kind == "pageBreak":
            parts.append(_seam_html(node.get("attrs") or {}, ctx))
        elif kind == "footnote":
            parts.append(_ref_html(node, ctx))
            ctx.chapter_notes.append(node)
            ctx.book_notes.append(node)
        elif kind == "hardBreak":
            parts.append("<br>")
        elif node.get("content"):
            parts.append(_inline_html(node.get("content") or [], ctx))
    return "".join(parts)


def _note_text_html(node: dict) -> str:
    """The body of a note without any inline node inside it (notes hold text only)."""
    out: list[str] = []
    for part in node.get("content") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text = escape(part.get("text") or "", quote=True)
            marks = part.get("marks") or []
            marked = any(isinstance(m, dict) and m.get("type") == "uncertain" for m in marks)
            out.append(f'<mark class="ms-uncertain">{text}</mark>' if marked else text)
        elif part.get("content"):
            out.append(_note_text_html(part))
    return "".join(out)


def _notes_html(notes: list[dict]) -> str:
    if not notes:
        return ""
    items: list[str] = []
    for node in notes:
        attrs = node.get("attrs") or {}
        note_id = _attr(attrs.get("id") or "")
        number = attrs.get("number")
        number = int(number) if isinstance(number, int) else _attr(number or "")
        page = attrs.get("sourcePage")
        orphan = bool(attrs.get("orphan"))
        flag = ""
        if orphan:
            marker = attrs.get("marker")
            printed = f" · العلامة المطبوعة «{_attr(marker)}»" if marker else ""
            where = f" · ص <bdi>{int(page)}</bdi>" if isinstance(page, int) else ""
            flag = f'<span class="ms-note-flag">بلا علامة في المتن{printed}{where}</span>'
        items.append(
            f'<li class="ms-note" id="note-{note_id}" data-note="{note_id}" data-number="{number}"'
            f' data-page="{_attr(page or "")}" data-orphan="{"true" if orphan else "false"}">'
            f'<a class="ms-note-num" href="#ref-{note_id}" data-ref="{note_id}"'
            f' aria-label="العودة إلى موضع الحاشية {number} في المتن"><bdi>{number}</bdi></a>'
            f'<span class="ms-note-body">{_note_text_html(node)}</span>{flag}</li>'
        )
    return f'<ol class="ms-notes" aria-label="الحواشي">{"".join(items)}</ol>'


def _split_html(seam: dict) -> str:
    """The block-level marker of a split or missing seam (a faint hairline with the page)."""
    page = int(seam.get("page") or 0)
    from_page = seam.get("from_page") or ""
    mode = seam.get("mode") or "split"
    decision = seam.get("decision") or "auto"
    reason = seam.get("reason") or ""
    if mode == "missing":
        skipped = _ints(seam.get("skipped")) or [page - 1]
        text = (
            f"صفحة <bdi>{skipped[0]}</bdi> غير مُضمَّنة"
            if len(skipped) == 1
            else f"الصفحات {_join_pages(skipped)} غير مُضمَّنة"
        )
        return (
            f'<div class="ms-split is-missing" role="note" data-page="{page}" data-from="{_attr(from_page)}"'
            f' data-mode="missing" data-decision="{_attr(decision)}" data-reason="{_attr(reason)}"'
            f' data-skipped="{_attr(",".join(str(p) for p in skipped))}">'
            f'<span class="ms-split-page">{text}</span></div>'
        )
    label = f"فاصل الصفحة {page}: فُصلت الفقرة عن الصفحة {from_page}" if from_page else f"فاصل الصفحة {page}"
    return (
        f'<div class="ms-split" data-page="{page}" data-from="{_attr(from_page)}" data-mode="split"'
        f' data-decision="{_attr(decision)}" data-reason="{_attr(reason)}">'
        f'<span class="ms-seam ms-seam-split ms-split-page" role="button" tabindex="0" data-page="{page}"'
        f' data-from="{_attr(from_page)}" data-mode="split" data-decision="{_attr(decision)}"'
        f' data-reason="{_attr(reason)}" aria-haspopup="menu" aria-label="{_attr(label)}">'
        f"ص <bdi>{page}</bdi></span></div>"
    )


def _suggest_html(block_id: str) -> str:
    bid = _attr(block_id)
    return (
        f'<span class="ms-suggest" role="group" aria-label="اقتراح: هذه الفقرة عنوان">'
        f'<span class="ms-suggest-label">عنوان؟</span>'
        f'<button type="button" class="ms-suggest-btn" data-suggest="accept" data-block="{bid}"'
        f' title="اعتماد الاقتراح: عنوان رئيسي" aria-label="اعتماد الاقتراح: عنوان رئيسي">✓</button>'
        f'<button type="button" class="ms-suggest-btn" data-suggest="dismiss" data-block="{bid}"'
        f' title="تجاهل الاقتراح" aria-label="تجاهل الاقتراح">×</button></span>'
    )


def _block_html(node: dict, ctx: _Ctx) -> str:
    attrs = node.get("attrs") or {}
    block_id = str(attrs.get("id") or "")
    ctx.block_id = block_id
    pages = _ints(attrs.get("sourcePages"))
    lines = _ints(attrs.get("sourceLineIds"))
    reviewed = _is_reviewed(pages, attrs, ctx.reviewed)
    if node.get("type") == "heading":
        level = 2 if attrs.get("level") == 2 else 1
        tag, cls = ("h3", "ms-h2") if level == 2 else ("h2", "ms-h1")
    else:
        tag, cls = "p", "ms-p"
    suggested = attrs.get("suggestedRole") if node.get("type") == "paragraph" else None
    extra = f' data-suggested="{_attr(suggested)}"' if suggested else ""
    if node.get("type") == "paragraph":
        if attrs.get("style") == "verse":
            extra += ' data-style="verse"'  # D74: the paragraph menu's «شعر» is checked
        if attrs.get("noteFor"):
            extra += f' data-note-for="{_attr(attrs["noteFor"])}"'  # D74: «حاشية للعلامة (n)»
    if ctx.unmatched.get(block_id):
        extra += f' data-unmatched="{_attr(",".join(ctx.unmatched[block_id]))}"'
    title = f' title="{UNREVIEWED_TITLE}"' if not reviewed else ""
    body = _inline_html(node.get("content") or [], ctx)
    if suggested:
        body += _suggest_html(block_id)
    return (
        f'<{tag} class="{cls} ms-block" id="b-{_attr(block_id)}" data-block="{_attr(block_id)}"'
        f' data-pages="{",".join(str(p) for p in pages)}" data-src="{pages_label(pages)}"'
        f' data-lines="{",".join(str(i) for i in lines)}" data-reviewed="{"true" if reviewed else "false"}"'
        f'{extra} tabindex="0"{title}>{body}</{tag}>'
    )


def _title_html(node: dict) -> str:
    attrs = node.get("attrs") or {}
    title = escape(str(attrs.get("text") or "").strip(), quote=True)
    author = escape(str(attrs.get("author") or "").strip(), quote=True)
    if not title and not author:
        return ""
    parts = ['<header class="ms-title">']
    if title:
        parts.append(f'<h1 class="ms-title-text">{title}</h1>')
    if author:
        parts.append(f'<p class="ms-title-author">{author}</p>')
    parts.append("</header>")
    return "".join(parts)


def _unmatched_markers(warnings) -> dict[str, list[str]]:
    """Block id → the printed markers `marker_unmatched` warnings name («18»), for the warning tint."""
    out: dict[str, list[str]] = {}
    for warning in warnings or []:
        if not isinstance(warning, dict) or warning.get("code") != "marker_unmatched":
            continue
        block_id = warning.get("blockId")
        match = _RE_MARKER.search(str(warning.get("message") or ""))
        if block_id and match:
            out.setdefault(str(block_id), []).append(match.group(1))
    return out


def render_document(
    doc: dict | None, *, notes: str = "chapter", warnings=None, reviewed: dict[int, bool] | None = None
) -> str:
    """The manuscript as HTML (§4.4). `notes`: `chapter` (a list after each chapter), `book` (one list
    at the end) or `none`. `warnings` (the run's, optional) tint the unmatched markers they name.
    `reviewed` (page number → reviewed now, `services.live_reviewed`) draws `data-reviewed` from the live
    status of each block's source pages (D70); without it, from the blocks' stored attribute."""
    if notes not in NOTE_MODES:
        raise ValueError(f"unknown notes mode {notes!r}")
    doc = doc if isinstance(doc, dict) else {}
    attrs = doc.get("attrs") or {}
    seams = {int(s["page"]): s for s in attrs.get("seams") or [] if isinstance(s, dict) and s.get("page")}
    pending = sorted(
        (s for s in seams.values() if s.get("mode") in ("split", "missing")), key=lambda s: int(s["page"])
    )
    ctx = _Ctx(seams=seams, notes_mode=notes, unmatched=_unmatched_markers(warnings), reviewed=reviewed)
    out: list[str] = ['<article class="ms-doc" dir="rtl">']
    chapter = -1
    open_section = False

    def close_section() -> None:
        nonlocal open_section
        if not open_section:
            return
        if ctx.notes_mode == "chapter":
            out.append(_notes_html(ctx.chapter_notes))
        ctx.chapter_notes = []
        out.append("</section>")
        open_section = False

    def open_new_section(heading_id: str | None) -> None:
        nonlocal chapter, open_section
        close_section()
        chapter += 1
        heading = f' data-heading="{_attr(heading_id)}"' if heading_id else ""
        out.append(f'<section class="ms-chapter" data-chapter="{chapter}"{heading}>')
        open_section = True

    for node in doc.get("content") or []:
        if not isinstance(node, dict):
            continue
        kind = node.get("type")
        if kind == "title":
            out.append(_title_html(node))
            continue
        if kind not in ("heading", "paragraph"):
            continue
        node_attrs = node.get("attrs") or {}
        if kind == "heading" and node_attrs.get("level", 1) != 2:
            open_new_section(str(node_attrs.get("id") or ""))
        elif not open_section:
            open_new_section(None)
        first_page = min(_ints(node_attrs.get("sourcePages")) or [0])
        while pending and int(pending[0]["page"]) <= first_page:
            out.append(_split_html(pending.pop(0)))
        out.append(_block_html(node, ctx))
    if pending:
        if not open_section:
            open_new_section(None)
        out.extend(_split_html(seam) for seam in pending)
    close_section()
    if ctx.notes_mode == "book":
        out.append(_notes_html(ctx.book_notes))
    out.append("</article>")
    return "".join(out)


# ====================================================================== view models


def plain_text(node: dict) -> str:
    """The text of a block or note node without its inline nodes (headings for the outline)."""
    out: list[str] = []
    for part in node.get("content") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            out.append(part.get("text") or "")
        elif part.get("type") == "footnote":
            continue
        elif part.get("content"):
            out.append(plain_text(part))
    return " ".join("".join(out).split())


def outline(doc: dict | None) -> list[dict]:
    """The table of contents: `{id, level, text, page, children}` per level-1 heading, with the
    level-2 headings nested under the chapter they belong to (those before any chapter float first)."""
    entries: list[dict] = []
    for node in (doc or {}).get("content") or []:
        if not isinstance(node, dict) or node.get("type") != "heading":
            continue
        attrs = node.get("attrs") or {}
        pages = _ints(attrs.get("sourcePages"))
        entry = {
            "id": str(attrs.get("id") or ""),
            "level": 2 if attrs.get("level") == 2 else 1,
            "text": plain_text(node) or "(عنوان بلا نص)",
            "page": pages[0] if pages else None,
            "children": [],
        }
        if entry["level"] == 2 and entries and entries[-1]["level"] == 1:
            entries[-1]["children"].append(entry)
        else:
            entries.append(entry)
    return entries


def flat_warnings(warnings) -> list[dict]:
    """The run's warnings in their stored order (book-level first, then by page), each with its
    `index` (the `]` / `[` order of the view) and the page's review URL left to the template. A footnote
    warning's `marker` and `actions` pass through when it has them (D74: `note_marker_missing`,
    `stray_note` with «جعلها حاشية» `{key, label, role, lineIds}` and «انتقال» `{key: "go", label,
    blockId}`)."""
    out: list[dict] = []
    for index, warning in enumerate(warnings or []):
        if not isinstance(warning, dict):
            continue
        page = warning.get("page")
        item = {
            "index": index,
            "code": str(warning.get("code") or ""),
            "severity": str(warning.get("severity") or "warning"),
            "page": page if isinstance(page, int) else None,
            "blockId": warning.get("blockId") or "",
            "lineIds": _ints(warning.get("lineIds")),
            "message": str(warning.get("message") or ""),
        }
        if warning.get("marker") is not None:
            item["marker"] = str(warning["marker"])
        actions = [
            _warning_action(action) for action in warning.get("actions") or [] if isinstance(action, dict)
        ]
        if actions:
            item["actions"] = actions
        out.append(item)
    return out


def _warning_action(action: dict) -> dict:
    """One action of a warning as the view reads it: `key` and `label`, with `role` and `lineIds` («جعلها
    حاشية», the roles endpoint's body) or `blockId` («انتقال») when it has them."""
    out = {"key": str(action.get("key") or ""), "label": str(action.get("label") or "")}
    if action.get("role"):
        out["role"] = str(action["role"])
    if "lineIds" in action:
        out["lineIds"] = _ints(action.get("lineIds"))
    if action.get("blockId"):
        out["blockId"] = str(action["blockId"])
    return out


def group_warnings(warnings) -> list[dict]:
    """Warnings grouped by code in the side panel's order (`WARNING_LABELS`), each group with its
    count; unknown codes go last under their own code."""
    groups: dict[str, dict] = {}
    for item in flat_warnings(warnings):
        group = groups.setdefault(
            item["code"],
            {
                "code": item["code"],
                "label": WARNING_LABELS.get(item["code"], item["code"]),
                "severity": item["severity"],
                "count": 0,
                "items": [],
            },
        )
        group["count"] += 1
        group["items"].append(item)
    order = {code: i for i, code in enumerate(WARNING_LABELS)}
    return sorted(groups.values(), key=lambda g: order.get(g["code"], len(order)))


def stats_rows(stats: dict | None) -> list[dict]:
    """`{key, label, value}` rows of the stats block; zero-only rows (skipped, unreviewed) are left out."""
    stats = stats or {}
    rows: list[dict] = []
    for key, label in STAT_LABELS:
        value = stats.get(key)
        if not isinstance(value, int):
            continue
        if key in ("pages_unreviewed", "pages_skipped") and value == 0:
            continue
        rows.append({"key": key, "label": label, "value": value})
    return rows


def counts_line(stats: dict | None) -> str:
    """The toolbar's counts: «214 صفحة · 38 فصلًا · 612 حاشية» (paragraphs stand in for chapters
    when the book has none yet)."""
    stats = stats or {}
    pages = int(stats.get("pages_included") or 0)
    chapters = int(stats.get("chapters") or 0)
    paragraphs = int(stats.get("paragraphs") or 0)
    notes = int(stats.get("footnotes") or 0)
    middle = ar_count(chapters, CHAPTERS) if chapters else ar_count(paragraphs, PARAGRAPHS)
    return " · ".join((ar_count(pages, PAGES), middle, ar_count(notes, FOOTNOTES) if notes else "بلا حواشٍ"))


def fragment_context(payload: dict | None) -> dict:
    """Context of `assembly/_document.html` from `assembly.services.manuscript_payload` (None → no document).

    `meta` is the JSON the Alpine component reads after a swap: version, assembled time, stats, the
    counts line, the flat warnings, the outline and the seams.
    """
    if not payload:
        return {"has_document": False, "document_html": "", "outline": [], "warning_groups": [], "meta": {}}
    doc = payload.get("document") or {}
    warnings = payload.get("warnings") or []
    stats = payload.get("stats") or {}
    toc = outline(doc)
    counts = counts_line(stats)
    return {
        "has_document": True,
        "version": payload.get("version") or 0,
        "document_html": render_document(doc, warnings=warnings, reviewed=payload.get("reviewed")),
        "outline": toc,
        "warning_groups": group_warnings(warnings),
        "warnings_total": len(warnings),
        "stats_rows": stats_rows(stats),
        "counts_text": counts,
        "meta": {
            "version": payload.get("version") or 0,
            "assembledAt": (doc.get("attrs") or {}).get("assembledAt"),
            "stats": stats,
            "countsText": counts,
            "warnings": flat_warnings(warnings),
            "toc": toc,
            "seams": payload.get("seams") or [],
        },
    }
