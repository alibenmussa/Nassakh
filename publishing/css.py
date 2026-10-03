"""The stylesheet → print CSS for WeasyPrint (PHASE5_SPEC §3).

`stylesheet_css(stylesheet, fonts)` writes every rule the book's pages need from the stylesheet
(`editor.StyleSheet`, a dict of its fields or a `publishing.model.PageSetup`):

- `@page` size (the trim; never bleed or crop marks, which belong to the print export, D60/D61) and
  **mirrored margins**: the inner margin is the binding side. An Arabic book (RTL) opens with a left-hand
  recto, so odd pages are `:left` pages with the inner margin on their right, even pages `:right` pages
  with it on their left.
- running headers from `string-set` (`running`, set by each chapter heading and by `div.nk-run-mark`,
  shown `first-except`: never on a chapter's opening page), page numbers (bottom centre, bottom outer or
  top outer), nothing on the front matter or on blank pages.
- the footnote area (`@footnote`, a hairline above) and the footnotes themselves (`float: footnote`;
  call and marker read `data-n`, which the per-page pass sets, D46; a note never lands on a page
  after its call's).
- recto openings (`break-before: recto`) or a new page for each chapter; sections of a book without
  headings run on.
- widows and orphans from the stylesheet (default 2), headings kept with the next line (`keep_headings`,
  default on), a new page before a block (`.nk-break`) and a block kept with the next (`.nk-keep`), D47.
- the title page with the book details, the copyright page (D47).
- D99: a subtitle (`.nk-section-title`) starts on the start side (right), the chapter title stays centred;
  the text options of a block (`text_option_rules`: alignment, a left-to-right paragraph, indent steps, no
  first-line indent, space before / after in lines of the body's pitch, sizes) win over its style.
- the faces' `@font-face` rules from the font registry (`publishing.fonts`).

For a chapter (or a window, D47) rendered alone, `first_page` makes the page counter start at that
chapter's first page in the current layout and puts that page on the right side (recto for an odd
number); a window's first page shows the running header unless the window opens the book's text.

**The PDF exports (D61, `output`: `publishing.engine.PdfOutput`)** add rules that never move a line, so
their pages are the preview's: a clean outline (chapter titles at level 1 and section titles at level 2,
labelled from `data-label` so a note call never leaks in; «المحتويات» at level 1; nothing for the title
page), the note links' plain look (`.nk-note-link`, the screen PDF), and for print the bleed asked for,
with a 6 mm slug for the crop marks when they are asked (`@page { bleed: <bleed + 6 mm slug> }`: the page
grows, the marks sit outside the bleed; the marks themselves are drawn in pure K by the PDF's finisher,
`publishing.pdf.finisher_for`, since WeasyPrint's `marks: crop` is an RGB drawing) and pure black (every
literal `#000`, and the scan marks' gray, as `device-cmyk`). The preview (`output=None`) is unchanged.
"""

from __future__ import annotations

import re

from .fonts import ResolvedFonts, families, font_face_css, resolve
from .model import PageSetup, page_setup

# print colours as pure K (D61): the literal colours of the print CSS
PURE_BLACK = "device-cmyk(0 0 0 1)"
_CMYK: dict[str, str] = {"#000": PURE_BLACK, "#555": "device-cmyk(0 0 0 0.667)"}
_RE_COLOUR = re.compile(r"#(?:000|555)(?![0-9A-Fa-f])")
# the export outline (D61): chapters level 1, sections level 2, the contents level 1, no title page entry
OUTLINE_CSS = (
    ".nk-title-page h1, .nk-book-title { bookmark-level: none; }",
    ".nk-contents-title { bookmark-level: 1; }",
    ".nk-chapter-title { bookmark-level: 1; bookmark-label: attr(data-label); }",
    ".nk-section-title { bookmark-level: 2; bookmark-label: attr(data-label); }",
    'h1[data-label=""], h2[data-label=""] { bookmark-level: none; }',
    # the screen PDF's call → note links look like the call (not a blue, underlined link)
    ".nk-note-link { color: inherit; text-decoration: none; }",
)


def _mm(value: float) -> str:
    return f"{round(float(value), 2):g}mm"


# a footnote call's size, as a share of the body text (the page editor mirrors it: book/edit.js `boxLook`)
FOOTNOTE_CALL_SCALE = 0.62


def _pt(value: float) -> str:
    return f"{round(float(value), 2):g}pt"


def _num(value: float) -> str:
    return f"{round(float(value), 3):g}"


def _setup(stylesheet) -> PageSetup:
    return stylesheet if isinstance(stylesheet, PageSetup) else page_setup(stylesheet)


def _number_boxes(setup: PageSetup) -> tuple[str, str, str]:
    """(`@page` box, `:left` box, `:right` box) rules of the page number."""
    number = (
        f"content: counter(page); font-family: {families()}; font-size: {_pt(setup.footnote_size_pt)};"
        " color: #000;"
    )
    if setup.page_number == "bottom_center":
        return f"@bottom-center {{ {number} vertical-align: top; padding-top: 4mm; }}", "", ""
    if setup.page_number == "bottom_outer":
        return (
            "",
            f"@bottom-left {{ {number} vertical-align: top; padding-top: 4mm; text-align: left; }}",
            f"@bottom-right {{ {number} vertical-align: top; padding-top: 4mm; text-align: right; }}",
        )
    if setup.page_number == "top_outer":
        return (
            "",
            f"@top-left {{ {number} vertical-align: bottom; padding-bottom: 3mm; text-align: left; }}",
            f"@top-right {{ {number} vertical-align: bottom; padding-bottom: 3mm; text-align: right; }}",
        )
    return "", "", ""


_ALL_BOXES = (
    "@top-left { content: none; } @top-center { content: none; } @top-right { content: none; }"
    " @bottom-left { content: none; } @bottom-center { content: none; } @bottom-right { content: none; }"
)


def stylesheet_css(
    stylesheet,
    fonts: ResolvedFonts | None = None,
    *,
    scope: str = "book",
    first_page: int = 1,
    continues: bool = False,
    output=None,
) -> str:
    """The print CSS of a book for WeasyPrint (see the module docstring); `fonts` are resolved from the
    stylesheet's faces when not given. `continues`: a window that goes on from the pages before it (its
    first page shows the running header like any page inside a chapter). `output` (a
    `publishing.engine.PdfOutput`) adds the export's rules; None is the preview."""
    s = _setup(stylesheet)
    if fonts is None:
        fonts = resolve(s.body_font, s.latin_font, s.heading_font)
    page_box, left_box, right_box = _number_boxes(s)
    header = ""
    if s.running_header in ("book", "chapter"):
        header = (
            f"@top-center {{ content: string(running, first-except); font-family: {families()};"
            f" font-size: {_pt(max(s.footnote_size_pt - 0.5, 6))}; color: #000; vertical-align: bottom;"
            " padding-bottom: 3mm; }"
        )
    opening = "recto" if s.chapter_opening == "recto" else "page"
    body_font = families()
    heading_font = families("heading")
    parts = [
        font_face_css(fonts),
        f"@page {{ size: {_mm(s.width_mm)} {_mm(s.height_mm)};"
        f" margin: {_mm(s.top_mm)} {_mm(s.outer_mm)} {_mm(s.bottom_mm)} {_mm(s.inner_mm)};"
        f" {header} {page_box}"
        " @footnote { border-top: 0.4pt solid #000; padding-top: 1.6mm; margin-top: 4mm; } }",
        f"@page :left {{ margin-left: {_mm(s.outer_mm)}; margin-right: {_mm(s.inner_mm)}; {left_box} }}",
        f"@page :right {{ margin-left: {_mm(s.inner_mm)}; margin-right: {_mm(s.outer_mm)}; {right_box} }}",
        f"@page front {{ {_ALL_BOXES} }}",
        f"@page :blank {{ {_ALL_BOXES} }}",
    ]
    if scope in ("chapter", "window") and first_page > 1:
        side = "recto" if first_page % 2 == 1 else "verso"
        parts.append(f"@page :first {{ counter-reset: page {int(first_page)}; }}")
        parts.append(f"html {{ break-before: {side}; }}")
    if scope == "window" and continues and header:
        parts.append("@page :first { @top-center { content: string(running, first); } }")
    keep = "avoid" if s.keep_headings else "auto"
    parts += [
        f"html {{ font-family: {body_font}; font-size: {_pt(s.body_size_pt)};"
        f" line-height: {_num(s.line_height)};"
        " color: #000; }",
        f"body {{ margin: 0; text-align: justify; hyphens: manual; orphans: {int(s.orphans)};"
        f" widows: {int(s.widows)}; }}",
        "p, h1, h2 { margin: 0; }",
        "b { font-weight: bold; } i { font-style: italic; }",
        ".nk-run-mark { string-set: running attr(data-running); height: 0; margin: 0; }",
        # front matter
        ".nk-front { page: front; }",
        ".nk-title-page { break-after: page; padding-top: 28%; text-align: center; }",
        f".nk-book-title {{ font-family: {heading_font}; font-size: {_num(s.h1_scale * 1.4)}em;"
        " font-weight: bold;"
        " line-height: 1.4; text-align: center; margin: 0 0 10mm; }",
        ".nk-book-author { font-size: 1.3em; text-align: center; text-indent: 0; }",
        ".nk-book-subtitle { font-size: 1.15em; text-align: center; text-indent: 0; margin: -6mm 0 10mm; }",
        ".nk-book-credit { font-size: 1.05em; text-align: center; text-indent: 0; margin-top: 3mm; }",
        ".nk-title-foot { margin-top: 30mm; }",
        ".nk-book-imprint { text-align: center; text-indent: 0; }",
        ".nk-copyright-page { break-before: page; break-after: page; padding-top: 70%; }",
        f".nk-copyright {{ font-size: {_pt(s.footnote_size_pt)}; line-height: 1.6; text-align: center;"
        " text-align-last: center; text-indent: 0; margin: 0 0 1mm; }",
        f".nk-contents {{ break-before: {opening}; break-after: page; }}",
        f".nk-contents-title {{ font-family: {heading_font}; font-size: {_num(s.h1_scale)}em;"
        " font-weight: bold;"
        " text-align: center; margin: 6mm 0 10mm; line-height: 1.4; }",
        ".nk-toc { list-style: none; margin: 0; padding: 0; }",
        ".nk-toc li { margin: 0 0 1.8mm; text-indent: 0; text-align: start; }",
        ".nk-toc-2 { padding-inline-start: 1.5em; font-size: 0.95em; }",
        ".nk-toc a { color: inherit; text-decoration: none; }",
        ".nk-toc a::after { content: leader('.') target-counter(attr(href), page); }",
        # chapters
        f".nk-chapter.is-chapter, .nk-chapter.is-front {{ break-before: {opening}; }}",
        ".nk-scope-chapter .nk-chapter:first-of-type, .nk-scope-window .nk-chapter:first-of-type"
        " { break-before: auto; }",
        f".nk-chapter-title {{ string-set: running attr(data-running); font-family: {heading_font};"
        f" font-size: {_num(s.h1_scale)}em; font-weight: bold; line-height: 1.35; text-align: center;"
        f" margin: 16mm 0 9mm; break-after: {keep}; }}",
        # D99 (the owner's review, 2026-10-03): a subtitle reads from the start side (right), the chapter
        # title stays centred; a block's own alignment wins (`text_option_rules`)
        f".nk-section-title {{ font-family: {heading_font}; font-size: {_num(s.h2_scale)}em;"
        " font-weight: bold;"
        f" line-height: 1.4; text-align: start; text-align-last: start; margin: 5mm 0 3mm;"
        f" break-after: {keep}; }}",
        # First-line indent as a start-side spacer: WeasyPrint puts `text-indent` on the left of an RTL line.
        f'.nk-body::before {{ content: ""; display: inline-block; width: {_num(s.indent_em)}em; }}',
        ".nk-body { text-indent: 0; }",
        # (rem: a quote's sides stay twice the body size whatever its own size, D99; = Word's quote style)
        ".nk-quote { margin: 2mm 2rem; text-indent: 0; }",
        ".nk-verse { text-align: center; text-align-last: center; text-indent: 0; margin: 2mm 0; }",
        ".nk-center { text-align: center; text-align-last: center; text-indent: 0; margin: 2mm 0; }",
        ".nk-separator { text-align: center; text-align-last: center; text-indent: 0; margin: 4mm 0; }",
        ".nk-book-title:not(:first-child) { margin-top: 8mm; }",
        # D99: a paragraph's or a heading's own text options (after the style rules: they win over them)
        *text_option_rules(s),
        # D47 block attrs (after the style rules: they win over a heading's `keep_headings: off`)
        ".nk-break { break-before: page; }",
        ".nk-keep { break-after: avoid; }",
        # footnotes (D46: numbers come from data-n)
        # `footnote-policy: line`: a note that does not fit takes the line of its call to the next page
        # (as Word does), so a call and its note are always on the same page and numbered there (D46).
        ".nk-fn { float: footnote; footnote-display: block; footnote-policy: line;"
        f" font-family: {body_font}; font-size: {_pt(s.footnote_size_pt)}; line-height: 1.5;"
        " font-weight: normal; font-style: normal;"
        " text-align: justify; text-indent: 0; }",
        # the call is sized from the body text (`em` would be the note's own smaller size: the call inherits
        # from the note)
        '.nk-fn::footnote-call { content: "(" attr(data-n) ")";'
        f" font-size: {_pt(s.body_size_pt * FOOTNOTE_CALL_SCALE)}; vertical-align: super;"
        " line-height: 0; font-weight: normal; font-style: normal; }",
        '.nk-fn::footnote-marker { content: "(" attr(data-n) ")\\00a0"; }',
    ]
    if scope == "front":
        # the front matter laid out alone (a heading's re-layout, D47): the headings are not in the document,
        # their pages are written into the entries (`data-page`)
        parts.append(".nk-toc a::after { content: leader('.') attr(data-page); }")
    if s.print_source_pages:
        side = max(min(s.inner_mm, s.outer_mm) - 3, 4)
        parts += [
            ".nk-body, .nk-quote, .nk-verse, .nk-center, .nk-section-title { position: relative; }",
            f".nk-src {{ position: absolute; right: -{_mm(side + 1)}; width: {_mm(side)}; font-size: 6.5pt;"
            " line-height: 1.2; color: #555; text-align: center; text-indent: 0; font-weight: normal;"
            " font-style: normal; }",
        ]
    if output is not None:
        parts += export_rules(output)
    css = "\n".join(part for part in parts[1:] if part)
    if output is not None and output.is_print:
        css = pure_black(css)
    return "\n".join(part for part in (parts[0], css) if part)


# D99: the styles' own vertical margins (mm, = the rules above and `publishing.word.styles`) that a block's
# space before / after adds to, and their text sizes (multiples of the body size)
STYLE_MARGINS_MM: dict[str, tuple[float, float]] = {
    "body": (0.0, 0.0),
    "quote": (2.0, 2.0),
    "verse": (2.0, 2.0),
    "center": (2.0, 2.0),
    "chapter-title": (16.0, 9.0),
    "section-title": (5.0, 3.0),
}
QUOTE_SIDE_EM = 2.0  # `.nk-quote`'s side margins


def line_mm(setup: PageSetup) -> float:
    """The body's line pitch in millimetres (the unit of a block's space before / after, D99)."""
    return float(setup.line_height) * float(setup.body_size_pt) * 25.4 / 72


def style_size(setup: PageSetup, style: str) -> float:
    """A style's text size as a multiple of the body size."""
    return {"chapter-title": setup.h1_scale, "section-title": setup.h2_scale}.get(style, 1.0)


def text_option_rules(setup: PageSetup) -> list[str]:
    """The rules of the text options (D99, `publishing.html.text_classes`): alignment, the left-to-right
    paragraph's start side, start-side indent steps, no first-line indent (also for a centred or end-aligned
    body paragraph, and an empty one), space added before / after the style's own, the sizes."""
    from editor import document as doc

    from .html import space_key

    step = doc.INDENT_STEP_REM
    rules = [
        ".nk-a-start { text-align: start; text-align-last: start; }",
        ".nk-a-center { text-align: center; text-align-last: center; }",
        ".nk-a-end { text-align: end; text-align-last: end; }",
        ".nk-a-justify { text-align: justify; text-align-last: start; }",
        ".nk-body.nk-a-center::before, .nk-body.nk-a-end::before, .nk-nofirst::before,"
        " .nk-empty::before { content: none; }",
    ]
    for n in doc.INDENT_LEVELS:
        rules += [
            f".nk-ind-{n} {{ margin-right: {_num(n * step)}rem; }}",
            f".nk-ltr.nk-ind-{n} {{ margin-right: 0; margin-left: {_num(n * step)}rem; }}",
            f".nk-quote.nk-ind-{n} {{ margin-right: {_num(QUOTE_SIDE_EM + n * step)}rem; }}",
            f".nk-quote.nk-ltr.nk-ind-{n} {{ margin-right: {_num(QUOTE_SIDE_EM)}rem;"
            f" margin-left: {_num(QUOTE_SIDE_EM + n * step)}rem; }}",
        ]
    pitch = line_mm(setup)
    for style, (top, bottom) in STYLE_MARGINS_MM.items():
        for lines in doc.SPACES:
            key = space_key(lines)
            rules.append(f".nk-{style}.nk-sb-{key} {{ margin-top: {_mm(top + lines * pitch)}; }}")
            rules.append(f".nk-{style}.nk-sa-{key} {{ margin-bottom: {_mm(bottom + lines * pitch)}; }}")
        for name, scale in doc.SIZE_SCALES.items():
            size = _num(style_size(setup, style) * scale)
            rules.append(f".nk-{style}.nk-sz-{name} {{ font-size: {size}em; }}")
    return rules


def export_rules(output) -> list[str]:
    """The rules a PDF export adds (`output`: a `publishing.engine.PdfOutput`): the clean outline, and
    for print the page's bleed (with the crop marks' slug when they are asked; the finisher draws the
    marks in pure K, never `marks: crop`)."""
    rules = list(OUTLINE_CSS)
    bleed = output.page_bleed_mm
    if output.is_print and bleed > 0:
        rules.append(f"@page {{ bleed: {_mm(bleed)}; }}")
    return rules


def pure_black(css: str) -> str:
    """`css` with its literal colours as pure K (`#000` → `device-cmyk(0 0 0 1)`), for the print PDF."""
    return _RE_COLOUR.sub(lambda match: _CMYK[match.group(0)], css)
