"""The book model → print HTML (PHASE5_SPEC §3): the markup WeasyPrint lays out into pages.

`render_html(book, stylesheet=None, scope="book")` writes one static document: the title page, the
copyright page and the contents (book scope only, when the stylesheet asks), then one `section.nk-chapter`
per chapter with a block element per model block. Every style maps to one class (`nk-<style>`); how a
class looks is the stylesheet's CSS (`publishing.css`), never inline formatting. All text is escaped.
`render_markup` returns the same HTML with the plain text of every tagged element (the layout export
aligns the laid-out lines with it, D47).

Scopes: `book`; `chapter` (the chapters of the model, no front matter: a chapter preview or re-layout);
`window` (like `chapter`, the first chapter starting at `start_block`: the forward re-layout of a book
without chapter breaks starts at a block that opens a page; `stop_block` ends a window of a long chapter);
`front` (the front matter alone, the contents' page numbers written in from `contents_pages`: the
re-layout of a heading's edit lays out the contents page without the whole book).

Markup contract (used by `publishing.css`, `publishing.pdf` and `publishing.layout`):

- `section.nk-chapter.is-<kind>#ch-<chapter id>` (its first page is found from this anchor)
- blocks: `h1.nk-chapter-title` / `h2.nk-section-title` / `p.nk-<style>`, `id="b-<block id>"`; headings
  and the zero-height `div.nk-run-mark` carry `data-running` (the running header's text, D46 `string-set`);
  `.nk-break` starts a new page before the block, `.nk-keep` keeps it with the next one (D47)
- layout tags (D47): every element whose lines the layout reports has `data-block` (the block's, the note's
  or the front matter item's id), `data-kind` (`body`, `heading`, `note`, `title`, `contents`, …) and
  `data-style` (the model style); a footnote element also has `data-note` (its call is a pseudo-element of
  it); a contents entry `data-target` (the heading's block id)
- a footnote is `span.nk-fn#fn-<note id>[data-n]` at its call (CSS `float: footnote` moves the body to
  the page foot and leaves the call); `with_numbers` rewrites `data-n` for the per-page pass (D46)
- `b`, `i` for the marks, `br` for line breaks, `span.nk-src[data-src]` for scan page marks (only when
  printed)
- front matter: `section.nk-title-page` (title, subtitle, author, editor, translator, publisher, city and
  year), `section.nk-copyright-page` (the book details), `nav.nk-contents` > `ol.nk-toc` >
  `li.nk-toc-<level>` > `a[href="#b-<heading id>"]`
- with `labels` (the PDF exports, D61), every chapter and section title also carries `data-label`: its
  text without note calls or scan page marks, which the export CSS uses as the outline entry
  (`bookmark-label: attr(data-label)`); with `note_links` (the screen PDF) every footnote element sits in
  `a.nk-note-link[href="#fn-…"]`, so its call links to its note (the link's only box in the line is the
  call; the lines do not move). The preview's markup has neither.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from html import escape

from .model import Block, Book, Footnote, LineBreak, NoteRef, Run, SourceMark, page_setup

SCOPES: tuple[str, ...] = ("book", "chapter", "window", "front")
CONTENTS_TITLE = "المحتويات"
_TAGS: dict[str, str] = {"chapter-title": "h1", "section-title": "h2"}
# the layout's line kind of each model style (D47)
KINDS: dict[str, str] = {
    "chapter-title": "heading",
    "section-title": "heading",
    "body": "body",
    "quote": "quote",
    "verse": "verse",
    "center": "center",
    "separator": "separator",
    "book-title": "title",
    "footnote-text": "note",
}
CREDIT_LABELS: dict[str, str] = {"editor": "تحقيق", "translator": "ترجمة"}
EDITION_LABEL = "الطبعة"
ISBN_LABEL = "ردمك"
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]")
_RE_NUMBER = re.compile(r'(<span class="nk-fn" id="fn-)([^"]+)(" data-n=")([^"]*)(")')


def _text(value: str) -> str:
    return escape(value, quote=False)


def _attr(value) -> str:
    return escape(str(value), quote=True)


class _Ids:
    """Unique, safe HTML ids (a repeated block or note id gets `-2`, `-3`…)."""

    def __init__(self) -> None:
        self.seen: set[str] = set()

    def make(self, prefix: str, value: str, fallback: str) -> str:
        base = _SAFE_ID.sub("", value or "") or fallback
        candidate = f"{prefix}{base}"
        k = 2
        while candidate in self.seen:
            candidate = f"{prefix}{base}-{k}"
            k += 1
        self.seen.add(candidate)
        return candidate


def _run_html(run: Run) -> str:
    html = _text(run.text)
    if "italic" in run.marks:
        html = f"<i>{html}</i>"
    if "bold" in run.marks:
        html = f"<b>{html}</b>"
    return html


def _note_html(note: Footnote, element_id: str, number: str) -> str:
    body = "".join(_run_html(run) if isinstance(run, Run) else "<br>" for run in note.runs)
    key = note.id or element_id
    return (
        f'<span class="nk-fn" id="{_attr(element_id)}" data-n="{_attr(number)}" data-block="{_attr(key)}"'
        f' data-note="{_attr(key)}" data-kind="note" data-style="footnote-text">{body}</span>'
    )


def _tags(key: str, kind: str, style: str) -> str:
    """The layout tags of an element (D47)."""
    return f' data-block="{_attr(key)}" data-kind="{_attr(kind)}" data-style="{_attr(style)}"'


@dataclass
class Markup:
    """The print HTML and the plain text of every tagged element (element id → text, the layout aligner's
    input: `editor.document.object_kinds` for blocks and notes, the printed text for front matter)."""

    html: str
    texts: dict[str, str] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)  # note id → its element id (`fn-…`)


def heading_label(block: Block) -> str:
    """A heading's outline label: its text without note calls or scan page marks, spaces collapsed."""
    return " ".join(block.text().split())


class _Writer:
    def __init__(
        self, book: Book, numbers: dict[str, str] | None, labels: bool = False, note_links: bool = False
    ):
        self.book = book
        self.setup = book.setup
        self.numbers = numbers or {}
        self.labels = labels
        self.note_links = note_links
        self.texts: dict[str, str] = {}
        # HTML ids for every block and note, allocated once in document order (the contents page links
        # to headings before they are written).
        ids = _Ids()
        self.block_ids: dict[int, str] = {}
        self.note_ids: dict[tuple[int, str], str] = {}
        self.heading_ids: dict[str, str] = {}
        for chapter in book.chapters:
            for block in chapter.blocks:
                element_id = ids.make("b-", block.id, f"auto{len(self.block_ids) + 1}")
                self.block_ids[id(block)] = element_id
                self.heading_ids.setdefault(block.id, element_id)
                for note in block.footnotes:
                    self.note_ids[(id(block), note.id)] = ids.make(
                        "fn-", note.id, f"note{len(self.note_ids) + 1}"
                    )

    def first_number(self, note: Footnote) -> str:
        if note.id in self.numbers:
            return self.numbers[note.id]
        mode = self.setup.footnote_numbering
        if mode == "book":
            return str(note.book_number)
        if mode == "chapter":
            return str(note.number)
        return "1"  # per page: pass 1 only learns the pages (D46), `with_numbers` sets the real numbers

    def inline(self, block: Block) -> str:
        notes = {note.id: note for note in block.footnotes}
        parts: list[str] = []
        for run in block.runs:
            if isinstance(run, Run):
                parts.append(_run_html(run))
            elif isinstance(run, LineBreak):
                parts.append("<br>")
            elif isinstance(run, NoteRef):
                note = notes.get(run.note)
                element_id = self.note_ids.get((id(block), run.note))
                if note is None or element_id is None:
                    continue
                html = _note_html(note, element_id, self.first_number(note))
                if self.note_links:
                    html = f'<a class="nk-note-link" href="#{_attr(element_id)}">{html}</a>'
                parts.append(html)
            elif isinstance(run, SourceMark) and self.setup.print_source_pages:
                parts.append(
                    f'<span class="nk-src" data-src="{run.page}" aria-hidden="true">ص {run.page}</span>'
                )
        return "".join(parts)

    def block(self, block: Block, chapter_running: str, opens_page: bool = False) -> str:
        tag = _TAGS.get(block.style, "p")
        element_id = self.block_ids[id(block)]
        extra = ""
        if block.style == "chapter-title":
            extra = f' data-running="{_attr(chapter_running)}"'
        if self.labels and tag in ("h1", "h2"):
            extra += f' data-label="{_attr(heading_label(block))}"'
        classes = f"nk-{block.style}"
        if block.break_before and not opens_page:  # (a block that opens a page anyway: no empty page first)
            classes += " nk-break"
        if block.keep_with_next:
            classes += " nk-keep"
        self.texts[element_id] = block.plain
        for note in block.footnotes:
            note_element = self.note_ids.get((id(block), note.id))
            if note_element is not None:
                self.texts[note_element] = note.plain
        tags = _tags(block.id or element_id, KINDS.get(block.style, "body"), block.style)
        return f'<{tag} class="{classes}" id="{_attr(element_id)}"{tags}{extra}>{self.inline(block)}</{tag}>'

    def front(self, tag: str, style: str, key: str, text: str, kind: str = "front") -> str:
        """One tagged front matter element (its printed text is its plain text)."""
        self.texts[key] = text
        return f'<{tag} class="nk-{style}" id="{_attr(key)}"{_tags(key, kind, style)}>{_text(text)}</{tag}>'

    def title_page(self) -> list[str]:
        front = self.book.front
        parts = ['<section class="nk-front nk-title-page" id="front-title-page">']
        parts.append(self.front("h1", "book-title", "front-title", front.title, "title"))
        if front.subtitle:
            parts.append(self.front("p", "book-subtitle", "front-subtitle", front.subtitle, "subtitle"))
        if front.author:
            parts.append(self.front("p", "book-author", "front-author", front.author, "author"))
        for name in ("editor", "translator"):
            value = getattr(front, name)
            if value:
                text = value if value.startswith(CREDIT_LABELS[name]) else f"{CREDIT_LABELS[name]}: {value}"
                parts.append(self.front("p", "book-credit", f"front-{name}", text))
        place = "، ".join(value for value in (front.publisher, front.city, front.year) if value)
        if place:
            parts.append('<div class="nk-title-foot">')
            parts.append(self.front("p", "book-imprint", "front-imprint", place))
            parts.append("</div>")
        parts.append("</section>")
        return parts

    def copyright_page(self) -> list[str]:
        front = self.book.front
        lines: list[tuple[str, str]] = [("front-c-title", front.title)]
        if front.subtitle:
            lines.append(("front-c-subtitle", front.subtitle))
        if front.author:
            lines.append(("front-c-author", front.author))
        for name in ("editor", "translator"):
            value = getattr(front, name)
            if value:
                label = CREDIT_LABELS[name]
                lines.append((f"front-c-{name}", value if value.startswith(label) else f"{label}: {value}"))
        if front.edition:
            edition = front.edition
            lines.append(
                (
                    "front-c-edition",
                    edition if edition.startswith(EDITION_LABEL) else f"{EDITION_LABEL} {edition}",
                )
            )
        place = "، ".join(value for value in (front.publisher, front.city, front.year) if value)
        if place:
            lines.append(("front-c-imprint", place))
        parts = ['<section class="nk-front nk-copyright-page" id="front-copyright-page">']
        parts.append('<div class="nk-copyright">')
        for key, text in lines:
            parts.append(self.front("p", "copyright", key, text, "copyright"))
        if front.isbn:  # digits and hyphens keep their order in the right-to-left line (bidi EN / ES)
            parts.append(
                self.front("p", "copyright", "front-c-isbn", f"{ISBN_LABEL}: {front.isbn}", "copyright")
            )
        if front.rights:
            parts.append(self.front("p", "copyright", "front-c-rights", front.rights, "copyright"))
        parts.append("</div></section>")
        return parts

    def running_text(self, heading: str) -> str:
        if self.setup.running_header == "book":
            return self.book.front.title
        if self.setup.running_header == "chapter":
            return heading or self.book.front.title
        return ""


def chapter_anchor(chapter) -> str:
    """The HTML id of a chapter's section (`ch-<id>`): the preview finds each chapter's first page by it."""
    return f"ch-{_SAFE_ID.sub('', chapter.id) or f'c{chapter.number}'}"


def render_markup(
    book: Book,
    stylesheet=None,
    scope: str = "book",
    *,
    numbers: dict[str, str] | None = None,
    start_block: str | None = None,
    labels: bool = False,
    note_links: bool = False,
    stop_block: str | None = None,
    contents_pages: dict[str, int] | None = None,
) -> Markup:
    """The print HTML of `book` and the plain text of its tagged elements (`scope` `book`: front matter
    and every chapter; `chapter`: the chapters in the model only, no front matter; `window`: the same,
    the first chapter from the block `start_block` on). `stop_block` (`chapter` and `window`) ends the
    markup before that block (a window of a long chapter laid out a few pages past an edit, D47; the pages
    the cut reaches are not kept). `front`: the front matter alone, each contents entry with its heading's
    page from `contents_pages` (heading block id → page; `data-page`, printed by the front scope's CSS in
    place of `target-counter`). `stylesheet`, when given, replaces the model's page
    setup; `numbers` (note id or element id → shown number) fixes the footnote numbers (D46 pass 2);
    `labels` adds the headings' outline labels (`data-label`, the PDF exports), `note_links` the links from
    the footnote calls to their notes (the screen PDF)."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}")
    if stylesheet is not None:
        book = replace(book, setup=page_setup(stylesheet))  # the caller's model is left as it is
    writer = _Writer(book, numbers, labels, note_links)
    front = book.front
    parts: list[str] = [
        "<!DOCTYPE html>",
        f'<html lang="{_attr(book.language)}" dir="{_attr(book.direction)}">',
        '<head><meta charset="utf-8">',
        f"<title>{_text(front.title)}</title>",
        f'<meta name="author" content="{_attr(front.author)}">' if front.author else "",
        "</head>",
        f'<body class="nk-book nk-scope-{scope}">',
    ]
    with_front = scope in ("book", "front")
    if with_front and front.title_page and front.title:
        parts.extend(writer.title_page())
    if with_front and front.copyright_page and front.title:
        parts.extend(writer.copyright_page())
    entries = book.contents() if with_front and front.contents else []
    pages = contents_pages if scope == "front" else None
    if entries:
        parts.append('<nav class="nk-front nk-contents" id="front-contents">')
        parts.append(
            writer.front("h2", "contents-title", "front-contents-title", CONTENTS_TITLE, "contents-title")
        )
        parts.append('<ol class="nk-toc">')
        for number, entry in enumerate(entries, start=1):
            target = writer.heading_ids.get(entry.target)
            if target is None:
                continue
            level = min(entry.level, 2)
            key = f"toc-{number}"
            writer.texts[key] = entry.text
            page = f' data-page="{int(pages[entry.target])}"' if pages and entry.target in pages else ""
            parts.append(
                f'<li class="nk-toc-{level}" id="{key}"{_tags(key, "contents", f"contents-{level}")}'
                f' data-target="{_attr(entry.target)}"><a href="#{_attr(target)}"{page}>'
                f'<span class="nk-toc-text">{_text(entry.text)}</span></a></li>'
            )
        parts.append("</ol></nav>")
    if scope == "front":
        parts.append("</body></html>")
        return Markup("\n".join(part for part in parts if part), writer.texts, {})
    first_section = True
    started = scope != "window" or not start_block
    body_started = False
    stopped = False
    for chapter in book.chapters:
        if stopped:
            break
        blocks = chapter.blocks
        if not started:
            index = next((i for i, block in enumerate(blocks) if block.id == start_block), None)
            if index is None:
                continue  # the window starts in a later chapter
            blocks = blocks[index:]
            started = True
        if stop_block and scope != "book":
            end = next((i for i, block in enumerate(blocks) if block.id == stop_block), None)
            if end is not None:
                blocks, stopped = blocks[:end], True
        if not blocks:
            continue  # nothing to print (a chapter left with empty paragraphs): no blank page for it
        parts.append(
            f'<section class="nk-chapter is-{_attr(chapter.kind)}" id="{_attr(chapter_anchor(chapter))}"'
            f' data-chapter="{_attr(chapter.id)}">'
        )
        running = writer.running_text(chapter.heading)
        titled = blocks[0].style == "chapter-title"
        if (chapter.kind == "section" and first_section) or (chapter.kind != "section" and not titled):
            # the running header's text until the next chapter heading (inside the section, so the mark
            # sits on the chapter's first page and not on a page of its own)
            parts.append(f'<div class="nk-run-mark" data-running="{_attr(running)}"></div>')
        first_section = first_section and chapter.kind != "section"
        for index, block in enumerate(blocks):
            opens = not body_started or (index == 0 and chapter.kind != "section")
            parts.append(writer.block(block, running, opens))
            body_started = True
        parts.append("</section>")
    parts.append("</body></html>")
    notes: dict[str, str] = {}
    for (_block, note_id), element_id in writer.note_ids.items():
        notes.setdefault(note_id, element_id)
    return Markup("\n".join(part for part in parts if part), writer.texts, notes)


def render_html(
    book: Book,
    stylesheet=None,
    scope: str = "book",
    *,
    numbers: dict[str, str] | None = None,
    start_block: str | None = None,
) -> str:
    """The print HTML of `book` (see `render_markup`)."""
    return render_markup(book, stylesheet, scope, numbers=numbers, start_block=start_block).html


def note_order(html: str) -> list[str]:
    """Footnote element ids (`fn-…`) in document order."""
    return [f"fn-{match.group(2)}" for match in _RE_NUMBER.finditer(html)]


def with_numbers(html: str, numbers: dict[str, str]) -> str:
    """`html` with the `data-n` of each footnote element set from `numbers` (element id → number)."""

    def repl(match: re.Match) -> str:
        element_id = f"fn-{match.group(2)}"
        value = numbers.get(element_id, match.group(4))
        return f"{match.group(1)}{match.group(2)}{match.group(3)}{_attr(value)}{match.group(5)}"

    return _RE_NUMBER.sub(repl, html)
