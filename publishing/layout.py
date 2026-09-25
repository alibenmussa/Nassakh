"""The page layout of a render (PHASE5_SPEC §9.1, D47): every laid-out line of every page, as data.

The book page draws **live pages** from this: each line absolutely positioned in the same faces as the
render, so the page on screen shows the same text and the same footprint as the PDF. `extract_layout`
walks WeasyPrint's box tree after the last layout pass (`document.pages[i]._page_box`: `LineBox` /
`TextBox` positions and the elements' `data-block` tags written by `publishing.html`) and returns one dict
per page, in points from the page's top-left corner:

    {n, side: "right"|"left", blank, width_pt, height_pt, margins: {top, right, bottom, left},
     lines: [line…], header: {…}|null, number: {…}|null, footnote_rule: {x, y, w}|null}

    line = {block, kind, style, x, y, w, h, baseline, dir, justify, start, end, first, runs: [run…]}
    run  = {text, font, size_pt, weight, italic, sup, note, start, end}  (+ `leader: true, w` for a dot
           leader: its one dot, to repeat across `w`)

- `block` is the block's id (a paragraph, a heading…), the note's id for the lines of a footnote at the
  page foot, a front matter item's id (`front-title`, `toc-3`…); `kind` is `body`, `heading`, `quote`,
  `verse`, `center`, `separator`, `title`, `note`, `contents`, `copyright`… (`style` the model style).
  Body lines come first, in reading order, then the footnote lines.
- `x` / `w` are the extent of the line's text (the first-line indent is left out: it is the space at the
  start side of a paragraph's first line); `y` / `h` the line box (the line height); `baseline` absolute.
  `justify` when WeasyPrint stretched the line's spaces (the browser justifies the text in `w`).
- `start` / `end` are offsets into the block's plain text (`editor.document.inline_text`: a footnote call
  or a scan page mark one U+FFFC, a hard break `\\n`) in UTF-16 code units — ProseMirror offsets in the
  textblock. Each run has its own range; a footnote call is a run `{text: "(1)", sup: true, note: <id>}`
  covering its one placeholder; generated text (the footnote marker at the foot, the contents' leader and
  page numbers) has an empty range at the cursor. Collapsed white space is the only difference between a
  run's `text` and its range of the plain text. `first` marks the first line of a block in the render.
- `header` / `number`: `{text, x, y, w, h, baseline, font, size_pt, weight, italic, align}` (the running
  header's and the page number's text, `align` the side the text keeps when it changes length).
- `footnote_rule`: the hairline above the notes.

`page_checks` reads a layout for the problems a typesetter looks for (a footnote running past its page,
a missing font, a chapter ending on an almost empty page, a heading at the foot of a page), and
`shift_pages` renumbers laid-out pages after a page-count change (sides swap on an odd shift: mirrored
margins move the text, an outer page number goes to the other corner).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from math import inf

from editor import document as doc

PT_PER_PX = 0.75  # WeasyPrint lays out in CSS pixels (96 per inch); the layout is in points
PAGE_MARK = "\x00"  # a scan page mark in `editor.document.object_kinds`
ALMOST_EMPTY_LINES = 3  # a chapter's last page with this many lines or fewer is «almost empty»
_DIGITS = re.compile(r"\d+")


def _pt(value) -> float:
    return round(float(value or 0) * PT_PER_PX, 2)


# ====================================================================== offsets


class Aligner:
    """Walks a block's plain text (`object_kinds`) while its laid-out text boxes come by in document order,
    and tells each box's character range. WeasyPrint's text differs from the source only by white space
    (collapsed, dropped at line ends and around line breaks); a footnote call is a pseudo-element that
    stands for one U+FFFC; scan page marks (U+0000 here) are not printed."""

    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.misses = 0
        self.missed: list[tuple[int, str]] = []
        self._astral = any(ord(char) > 0xFFFF for char in text)

    def units(self, index: int) -> int:
        """A code point index as UTF-16 units (the contract's unit)."""
        return doc.utf16_index(self.text, index) if self._astral else index

    def _skip(self, j: int, space: bool = True) -> int:
        text, n = self.text, len(self.text)
        while j < n and (text[j] == PAGE_MARK or (space and text[j].isspace())):
            j += 1
        return j

    def take(self, piece: str) -> tuple[int, int]:
        """The range of a text box's text; the cursor moves past it."""
        text, n = self.text, len(self.text)
        pos = self.pos
        start = None
        for char in piece:
            if char.isspace():
                j = self._skip(pos, space=False)
                if j < n and text[j].isspace():
                    if start is None:
                        start = j
                    pos = self._skip(j)
                continue
            j = self._skip(pos)
            if j < n and text[j] == char:
                if start is None:
                    start = j
                pos = j + 1
            else:
                self.misses += 1  # a hyphen added at a break, or a text that is not the source's
                self.missed.append((pos, char))
        self.pos = pos
        return (start if start is not None else pos), pos

    def take_object(self, kind: str = doc.OBJECT) -> tuple[int, int]:
        """The range of the next object (a footnote call, or with `kind` U+0000 a printed page mark)."""
        j = self._skip(self.pos) if kind == doc.OBJECT else self._skip_space(self.pos)
        if j < len(self.text) and self.text[j] == kind:
            self.pos = j + 1
            return j, j + 1
        if kind != doc.OBJECT:
            return self.pos, self.pos  # a page mark printed at a block's start has no node of its own
        found = self.text.find(kind, self.pos)
        if found >= 0:
            self.pos = found + 1
            return found, found + 1
        self.misses += 1
        return self.pos, self.pos

    def _skip_space(self, j: int) -> int:
        while j < len(self.text) and self.text[j].isspace():
            j += 1
        return j


# ====================================================================== the box tree


def _family(style) -> str:
    families = style["font_family"] or ()
    return str(families[0]) if families else ""


def _run(box, text: str, start: int, end: int, *, sup: bool = False, note: str | None = None) -> dict:
    style = box.style
    return {
        "text": text,
        "font": _family(style),
        "size_pt": _pt(style["font_size"]),
        "weight": int(style["font_weight"]) if str(style["font_weight"]).isdigit() else 400,
        "italic": style["font_style"] != "normal",
        "sup": sup,
        "note": note,
        "start": start,
        "end": end,
    }


class _Context:
    """The tagged element a box belongs to (`publishing.html` layout tags)."""

    __slots__ = ("key", "block", "kind", "style", "target")

    def __init__(self, key: str, block: str, kind: str, style: str, target: str | None):
        self.key, self.block, self.kind, self.style, self.target = key, block, kind, style, target


def _context_of(box) -> _Context | None:
    element = box.element
    if element is None or "::" in (box.element_tag or "") or not hasattr(element, "get"):
        return None
    block = element.get("data-block")
    if not block:
        return None
    return _Context(
        element.get("id") or block,
        block,
        element.get("data-kind") or "body",
        element.get("data-style") or "",
        element.get("data-target"),
    )


class _Extractor:
    def __init__(self, texts: dict[str, str]):
        self.texts = texts
        self.aligners: dict[str, Aligner] = {}
        self.seen: set[str] = set()

    def aligner(self, ctx: _Context | None) -> Aligner:
        if ctx is None:
            return Aligner("")
        found = self.aligners.get(ctx.key)
        if found is None:
            found = self.aligners[ctx.key] = Aligner(self.texts.get(ctx.key, ""))
        return found

    # ------------------------------------------------------------------ pages

    def page(self, page_box, n: int) -> dict:
        from weasyprint.formatting_structure import boxes

        lines: list[dict] = []
        notes: list[dict] = []
        header = number = rule = None
        for child in page_box.children:
            if isinstance(child, boxes.MarginBox):
                item = self.margin(child)
                if item is None:
                    continue
                if child.at_keyword == "@top-center":
                    header = item
                else:
                    number = item
            elif isinstance(child, boxes.FootnoteAreaBox):
                if child.children:
                    rule = {
                        "x": _pt(child.border_box_x()),
                        "y": _pt(child.position_y + child.margin_top),
                        "w": _pt(child.border_width()),
                    }
                    self.walk(child, None, notes)
            elif isinstance(child, boxes.Box):
                self.walk(child, None, lines)
        page_type = page_box.page_type
        return {
            "n": n,
            "side": page_type.side,
            "blank": bool(page_type.blank),
            "width_pt": _pt(page_box.margin_width()),
            "height_pt": _pt(page_box.margin_height()),
            "margins": {
                "top": _pt(page_box.margin_top),
                "right": _pt(page_box.margin_right),
                "bottom": _pt(page_box.margin_bottom),
                "left": _pt(page_box.margin_left),
            },
            "lines": lines + notes,
            "header": header,
            "number": number,
            "footnote_rule": rule,
        }

    def walk(self, box, ctx: _Context | None, out: list[dict]) -> None:
        from weasyprint.formatting_structure import boxes

        own = _context_of(box)
        if own is not None:
            ctx = own
        if isinstance(box, boxes.LineBox):
            self.line(box, ctx, out)
            return
        for child in getattr(box, "children", ()):
            if isinstance(child, boxes.Box):
                self.walk(child, ctx, out)

    # ------------------------------------------------------------------ lines

    def line(self, line_box, ctx: _Context | None, out: list[dict]) -> None:
        aligner = self.aligner(ctx)
        state = {"runs": [], "x0": inf, "x1": -inf, "justify": False}
        self.inline(line_box, ctx, aligner, state, out, note=None)
        runs = _trim_edges(state["runs"])
        if not runs:
            return
        ranged = [run for run in runs if run["end"] > run["start"]]
        start = ranged[0]["start"] if ranged else runs[0]["start"]
        end = ranged[-1]["end"] if ranged else runs[-1]["end"]
        first = False
        if ctx is not None and ctx.key not in self.seen:
            self.seen.add(ctx.key)
            first = True
        line = {
            "block": ctx.block if ctx is not None else None,
            "kind": ctx.kind if ctx is not None else "other",
            "style": ctx.style if ctx is not None else "",
            "x": _pt(state["x0"]),
            "y": _pt(line_box.position_y),
            "w": _pt(state["x1"] - state["x0"]),
            "h": _pt(line_box.height),
            "baseline": _pt(line_box.position_y + (line_box.baseline or 0)),
            "dir": line_box.style["direction"],
            "justify": state["justify"],
            "start": start,
            "end": end,
            "first": first,
            "runs": runs,
        }
        if ctx is not None and ctx.target:
            line["target"] = ctx.target
        out.append(line)

    def inline(self, box, ctx, aligner: Aligner, state: dict, out: list[dict], note: str | None) -> None:
        from weasyprint.formatting_structure import boxes

        for child in getattr(box, "children", ()):
            if not isinstance(child, boxes.Box):
                inner = getattr(child, "_box", None)  # an absolutely positioned box (a printed page mark)
                if inner is not None:
                    self.mark(inner, ctx, aligner, out)
                continue
            tag = child.element_tag or ""
            if isinstance(child, boxes.TextBox):
                if note is not None:
                    start, end = state["call"]
                    sup = True
                elif "::" in tag or (ctx is not None and ctx.kind == "separator"):
                    start = end = aligner.units(aligner.pos)  # generated text (a separator's «* * *»)
                    sup = False
                else:
                    a, b = aligner.take(child.text)
                    start, end = aligner.units(a), aligner.units(b)
                    sup = False
                marker = tag.endswith("::footnote-marker") and ctx is not None
                state["runs"].append(
                    _run(child, child.text, start, end, sup=sup, note=note or (ctx.block if marker else None))
                )
                state["x0"] = min(state["x0"], child.position_x)
                state["x1"] = max(state["x1"], child.position_x + child.width)
                if child.justification_spacing:
                    state["justify"] = True
            elif isinstance(child, boxes.InlineBox):
                if child.is_leader:  # the contents' dot leader: one run as wide as the leader box
                    dots = next((item.text for item in _text_boxes(child)), "")
                    if dots:
                        at = aligner.units(aligner.pos)
                        run = _run(child, dots, at, at)
                        run["leader"] = True
                        run["w"] = _pt(child.width)
                        state["runs"].append(run)
                        state["x0"] = min(state["x0"], child.position_x)
                        state["x1"] = max(state["x1"], child.position_x + child.width)
                elif tag.endswith("::footnote-call"):
                    a, b = aligner.take_object()
                    state["call"] = (aligner.units(a), aligner.units(b))
                    called = child.element.get("data-note") if child.element is not None else None
                    self.inline(child, ctx, aligner, state, out, note=called or "")
                else:
                    self.inline(child, ctx, aligner, state, out, note)
            # atomic inline boxes (the first-line indent spacer, images) print no text

    def mark(self, box, ctx, aligner: Aligner, out: list[dict]) -> None:
        """A printed scan page mark («ص 12» in the margin): a `source` line at its place."""
        element = box.element
        if element is None or not hasattr(element, "get") or element.get("data-src") is None:
            return
        a, _b = aligner.take_object(PAGE_MARK)
        texts = [item for item in _text_boxes(box)]
        if not texts:
            return
        x0 = min(item.position_x for item in texts)
        x1 = max(item.position_x + item.width for item in texts)
        at = aligner.units(a)
        out.append(
            {
                "block": ctx.block if ctx is not None else None,
                "kind": "source",
                "style": "source",
                "x": _pt(x0),
                "y": _pt(texts[0].position_y),
                "w": _pt(x1 - x0),
                "h": _pt(texts[0].height),
                "baseline": _pt(texts[0].position_y + (texts[0].baseline or 0)),
                "dir": box.style["direction"],
                "justify": False,
                "start": at,
                "end": at,
                "first": False,
                "runs": [_run(item, item.text, at, at) for item in texts],
                "page": int(element.get("data-src")) if str(element.get("data-src")).isdigit() else None,
            }
        )

    def margin(self, box) -> dict | None:
        texts = list(_text_boxes(box))
        if not texts:
            return None
        x0 = min(item.position_x for item in texts)
        x1 = max(item.position_x + item.width for item in texts)
        first = texts[0]
        style = first.style
        align = str(box.style["text_align_all"])
        if align in ("start", "end"):
            rtl = box.style["direction"] == "rtl"
            align = "right" if (align == "start") == rtl else "left"
        return {
            "text": "".join(item.text for item in texts),
            "x": _pt(x0),
            "y": _pt(first.position_y),
            "w": _pt(x1 - x0),
            "h": _pt(first.height),
            "baseline": _pt(first.position_y + (first.baseline or 0)),
            "font": _family(style),
            "size_pt": _pt(style["font_size"]),
            "weight": int(style["font_weight"]) if str(style["font_weight"]).isdigit() else 400,
            "italic": style["font_style"] != "normal",
            "align": align if align in ("left", "right", "center") else "center",
        }


def _trim_edges(runs: list[dict]) -> list[dict]:
    """The runs of a line without the white space at its two ends (a space kept before an inline element
    that the line broke after prints nothing and must not take room in the browser's line either)."""
    for index, step in ((0, 1), (len(runs) - 1, -1)):
        while 0 <= index < len(runs):
            run = runs[index]
            if run.get("note") or run.get("leader"):
                break
            text = run["text"].lstrip() if step == 1 else run["text"].rstrip()
            cut = len(run["text"]) - len(text)
            if cut and run["end"] > run["start"]:
                if step == 1:
                    run["start"] = min(run["end"], run["start"] + cut)
                else:
                    run["end"] = max(run["start"], run["end"] - cut)
            run["text"] = text
            if text:
                break
            runs.pop(index)
            if step == -1:
                index -= 1
    return runs


def _text_boxes(box) -> Iterable:
    from weasyprint.formatting_structure import boxes

    for item in box.descendants():
        if isinstance(item, boxes.TextBox):
            yield item


def extract_layout(document, texts: dict[str, str], *, first_page: int = 1) -> tuple[list[dict], int]:
    """The layout of a laid-out WeasyPrint `document` (see the module docstring): the pages (numbered
    from `first_page`) and the number of characters the aligner could not place (0 for a render of the
    manuscript's own text). `texts` is `publishing.html.Markup.texts`."""
    extractor = _Extractor(texts)
    pages = [extractor.page(page._page_box, first_page + index) for index, page in enumerate(document.pages)]
    misses = sum(aligner.misses for aligner in extractor.aligners.values())
    return pages, misses


# ====================================================================== reading a layout


def assign_chapters(pages: list[dict], chapters: list[dict]) -> None:
    """Set each page's `chapter` from the chapter ranges (`[{id, first, last}]`, printed numbers)."""
    for page in pages:
        page["chapter"] = next(
            (
                item.get("id")
                for item in chapters
                if isinstance(item.get("first"), int)
                and isinstance(item.get("last"), int)
                and item["first"] <= page["n"] <= item["last"]
            ),
            None,
        )


def note_numbers(pages: Iterable[dict]) -> dict[str, str]:
    """Note id → the number its call shows (`"(2)"` → `"2"`), from the body lines' call runs."""
    out: dict[str, str] = {}
    for page in pages:
        for line in page.get("lines") or []:
            if line.get("kind") == "note":
                continue
            for run in line.get("runs") or []:
                if run.get("note") and run.get("sup"):
                    found = _DIGITS.search(run.get("text") or "")
                    if found:
                        out.setdefault(run["note"], found.group())
    return out


def set_note_numbers(pages: list[dict], numbers: dict[str, str]) -> None:
    """Set the footnote numbers shown (note id → number) in the calls and the markers of a layout (in
    place): what a second layout pass would print when only the numbers change, not their widths."""
    for page in pages:
        for line in page.get("lines") or []:
            for run in line.get("runs") or []:
                number = numbers.get(run.get("note") or "")
                if number is not None:
                    run["text"] = _DIGITS.sub(number, run["text"], count=1)


def body_lines(page: dict) -> list[dict]:
    """The page's lines in the text column (not the notes, not the printed page marks)."""
    return [line for line in page.get("lines") or [] if line.get("kind") not in ("note", "source")]


def page_top(page: dict) -> tuple[str | None, int] | None:
    """`(block, start)` of the page's first line in the text column (None on a page without one)."""
    lines = body_lines(page)
    return (lines[0].get("block"), lines[0].get("start", 0)) if lines else None


def _lines_phrase(count: int) -> str:
    if count == 1:
        return "سطر واحد"
    if count == 2:
        return "سطران"
    return f"{count} أسطر"


def page_checks(
    pages: list[dict], chapters: list[dict], missing_fonts: list[dict] | None = None
) -> list[dict]:
    """The problems of a layout: `[{code, page, block, message}]` (Arabic messages, Western digits):

    - `missing_font`: a face of the stylesheet is not installed (Amiri stood in; `page` and `block` null)
    - `footnote_overflow`: a note continues past the page of its call, or runs into the bottom margin
      (`block` is the calling block, `note` the note)
    - `almost_empty_page`: a chapter (or the book) ends on a page with `ALMOST_EMPTY_LINES` lines or fewer
    - `heading_at_foot`: a heading is the last line of a page, its text on the next page
    """
    out: list[dict] = []
    for item in missing_fonts or []:
        out.append(
            {
                "code": "missing_font",
                "page": None,
                "block": None,
                "message": f"الخط «{item.get('name')}» غير مثبّت على هذا الجهاز؛"
                f" استُعمل «{item.get('fallback')}» بدلًا منه.",
            }
        )
    calls: dict[str, tuple[int, str | None]] = {}
    note_pages: dict[str, set[int]] = {}
    by_number = {page["n"]: page for page in pages}
    for page in pages:
        for line in page.get("lines") or []:
            if line.get("kind") == "note":
                note_pages.setdefault(line.get("block") or "", set()).add(page["n"])
                continue
            for run in line.get("runs") or []:
                if run.get("note") and run.get("sup"):
                    calls.setdefault(run["note"], (page["n"], line.get("block")))
        rule = page.get("footnote_rule")
        notes = [line for line in page.get("lines") or [] if line.get("kind") == "note"]
        if rule and notes:
            limit = page["height_pt"] - page["margins"]["bottom"] + 1
            late = [line for line in notes if line["y"] + line["h"] > limit]
            if late:
                note = late[0].get("block")
                where = calls.get(note or "", (page["n"], None))
                out.append(
                    {
                        "code": "footnote_overflow",
                        "page": page["n"],
                        "block": where[1],
                        "note": note,
                        "message": f"حاشية أطول من أن تسعها الصفحة {page['n']}؛ تجاوزت الهامش السفلي.",
                    }
                )
    for note, seen in note_pages.items():
        page_of_call, block = calls.get(note, (None, None))
        later = sorted(n for n in seen if page_of_call is not None and n != page_of_call)
        if later:
            out.append(
                {
                    "code": "footnote_overflow",
                    "page": page_of_call,
                    "block": block,
                    "note": note,
                    "message": f"حاشية الصفحة {page_of_call} لم تتسع لها الصفحة"
                    f" فاستمرت في الصفحة {later[0]}.",
                }
            )
    ends = []
    for index, item in enumerate(chapters):
        last = item.get("last")
        following = chapters[index + 1] if index + 1 < len(chapters) else None
        if not isinstance(last, int):
            continue
        if following is not None and following.get("first") == last:
            continue  # run-on sections share their last page with the next one
        ends.append((item, last))
    for item, last in ends:
        page = by_number.get(last)
        while page is not None and page.get("blank") and page["n"] > (item.get("first") or 0):
            page = by_number.get(page["n"] - 1)  # the blank page before a recto opening
        if page is None or page["n"] == item.get("first"):
            continue
        count = len(body_lines(page))
        if 0 < count <= ALMOST_EMPTY_LINES:
            lines = body_lines(page)
            out.append(
                {
                    "code": "almost_empty_page",
                    "page": page["n"],
                    "block": lines[0].get("block"),
                    "message": f"ينتهي «{item.get('title') or ''}» في الصفحة {page['n']} وليس فيها إلا"
                    f" {_lines_phrase(count)}.",
                }
            )
    for index, page in enumerate(pages[:-1]):
        lines = body_lines(page)
        if not lines or lines[-1].get("kind") != "heading":
            continue
        tail = lines[-1]
        top = page_top(pages[index + 1])
        if top is not None and top[0] == tail.get("block"):
            continue  # the heading itself goes on over the page
        out.append(
            {
                "code": "heading_at_foot",
                "page": page["n"],
                "block": tail.get("block"),
                "message": f"العنوان في أسفل الصفحة {page['n']} ونصه في الصفحة التالية.",
            }
        )
    return out


# ====================================================================== moving pages


def side_shift(setup) -> float:
    """How far (points) the text moves when a right-hand page becomes a left-hand one (mirrored margins:
    outer on the left of a left-hand page); the opposite move is the negative."""
    mm = 72 / 25.4
    return round((float(setup.outer_mm) - float(setup.inner_mm)) * mm, 2)


def _moved(item: dict | None, dx: float) -> dict | None:
    if item is None:
        return None
    return dict(item, x=round(item["x"] + dx, 2))


def shift_page(page: dict, delta: int, setup) -> dict:
    """A copy of `page` numbered `n + delta`; on an odd `delta` it swaps sides: the lines, the header and
    the footnote rule move by `side_shift`, a page number in the outer corner goes to the other corner."""
    if not delta:
        return page
    out = dict(page, n=page["n"] + delta)
    number = page.get("number")
    if number is not None:
        number = dict(
            number, text=str(page["n"] + delta) if number.get("text", "").isdigit() else number["text"]
        )
    if delta % 2:
        right = page.get("side") == "right"
        dx = side_shift(setup) if right else -side_shift(setup)
        out["side"] = "left" if right else "right"
        out["lines"] = [dict(line, x=round(line["x"] + dx, 2)) for line in page.get("lines") or []]
        out["header"] = _moved(page.get("header"), dx)
        out["footnote_rule"] = _moved(page.get("footnote_rule"), dx)
        margins = page.get("margins") or {}
        out["margins"] = dict(margins, left=margins.get("right"), right=margins.get("left"))
        if number is not None:
            if getattr(setup, "page_number", "") in ("bottom_outer", "top_outer"):
                number = dict(
                    number,
                    x=round(page["width_pt"] - number["x"] - number["w"], 2),
                    align={"left": "right", "right": "left"}.get(number.get("align"), number.get("align")),
                )
            else:
                number = _moved(number, dx)
    out["number"] = number
    return out


def shift_pages(pages: Iterable[dict], delta: int, setup) -> list[dict]:
    """`shift_page` on every page."""
    return [shift_page(page, delta, setup) for page in pages]


def refresh_contents(pages: list[dict]) -> None:
    """Set the page numbers of the contents lines (`target`: a heading's block) to the pages their headings
    are on now (after a splice moved them; the full render sets the same numbers)."""
    first: dict[str, int] = {}
    for page in pages:
        for line in body_lines(page):
            if line.get("block") and line.get("first"):
                first.setdefault(line["block"], page["n"])
    for page in pages:
        lines = page.get("lines") or []
        if not any(line.get("target") for line in lines):
            continue
        fresh = []
        for line in lines:
            target = line.get("target")
            runs = line.get("runs") or []
            if (
                target in first
                and runs
                and runs[-1]["text"].isdigit()
                and runs[-1]["text"] != str(first[target])
            ):
                line = dict(line, runs=[*runs[:-1], dict(runs[-1], text=str(first[target]))])
            fresh.append(line)
        page["lines"] = fresh  # new lists and dicts: the page's lines may be shared with a cached layout


def blank_page(n: int, like: dict) -> dict:
    """An empty page numbered `n` of the same size as `like` (a blank verso before a recto opening)."""
    side = "left" if n % 2 == 1 else "right"
    margins = dict(like.get("margins") or {})
    if like.get("side") != side:
        margins = dict(margins, left=margins.get("right"), right=margins.get("left"))
    return {
        "n": n,
        "side": side,
        "blank": True,
        "width_pt": like.get("width_pt"),
        "height_pt": like.get("height_pt"),
        "margins": margins,
        "lines": [],
        "header": None,
        "number": None,
        "footnote_rule": None,
        "chapter": like.get("chapter"),
    }


# ====================================================================== checking offsets


def _collapse(text: str) -> str:
    return " ".join(text.replace(doc.BREAK, " ").split())


def offset_problems(pages: list[dict], texts: dict[str, str]) -> list[str]:
    """What is wrong with a layout's offsets against the blocks' plain texts (`block id → object_kinds`
    text): a line whose runs do not read as its range (white space collapsed, calls as U+FFFC, printed page
    marks and generated text left out), ranges out of order, or text of a block on no line. Empty for a
    sound layout (the tests and the real-book check use it)."""
    problems: list[str] = []
    ranges: dict[str, list[tuple[int, int]]] = {}
    for page in pages:
        for line in page.get("lines") or []:
            block = line.get("block")
            if block not in texts or line.get("kind") in ("separator", "source"):
                continue
            text = texts[block]
            start = doc.code_index(text, line["start"])
            end = doc.code_index(text, line["end"])
            shown = "".join(
                doc.OBJECT if (run.get("note") and run.get("sup")) else run["text"]
                for run in line["runs"]
                if run["end"] > run["start"] or (run.get("note") and run.get("sup"))
            )
            source = text[start:end].replace(PAGE_MARK, "")
            if _collapse(shown) != _collapse(source):
                problems.append(f"page {page['n']} block {block}: {shown[:40]!r} != {source[:40]!r}")
            ranges.setdefault(block, []).append((start, end))
    for block, spans in ranges.items():
        text = texts[block]
        cursor = 0
        covered = [False] * len(text)
        for start, end in spans:
            if start < cursor:
                problems.append(f"block {block}: range {start}-{end} before {cursor}")
            cursor = max(cursor, end)
            for i in range(start, end):
                covered[i] = True
        missing = [
            i for i, char in enumerate(text) if not covered[i] and not char.isspace() and char != PAGE_MARK
        ]
        if missing:
            problems.append(f"block {block}: {len(missing)} characters on no line (first at {missing[0]})")
    return problems
