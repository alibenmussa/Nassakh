"""The book model → print HTML (PHASE5_SPEC §3): the markup WeasyPrint lays out into pages.

`render_html(book, stylesheet=None, scope="book")` writes one static document: the title page and the
contents (book scope only, when the stylesheet asks), then one `section.nk-chapter` per chapter with a
block element per model block. Every style maps to one class (`nk-<style>`); how a class looks is the
stylesheet's CSS (`publishing.css`), never inline formatting. All text is escaped.

Markup contract (used by `publishing.css` and `publishing.pdf`):

- `section.nk-chapter.is-<kind>#ch-<chapter id>` (its first page is found from this anchor)
- blocks: `h1.nk-chapter-title` / `h2.nk-section-title` / `p.nk-<style>`, `id="b-<block id>"`; headings
  and the zero-height `div.nk-run-mark` carry `data-running` (the running header's text, D46 `string-set`)
- a footnote is `span.nk-fn#fn-<note id>[data-n]` at its call (CSS `float: footnote` moves the body to
  the page foot and leaves the call); `with_numbers` rewrites `data-n` for the per-page pass (D46)
- `b`, `i` for the marks, `br` for line breaks, `span.nk-src` for scan page marks (only when printed)
- contents: `nav.nk-contents` > `ol.nk-toc` > `li.nk-toc-<level>` > `a[href="#b-<heading id>"]`
"""

from __future__ import annotations

import re
from dataclasses import replace
from html import escape

from .model import Block, Book, Footnote, LineBreak, NoteRef, Run, SourceMark, page_setup

SCOPES: tuple[str, ...] = ("book", "chapter")
CONTENTS_TITLE = "المحتويات"
_TAGS: dict[str, str] = {"chapter-title": "h1", "section-title": "h2"}
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
    return f'<span class="nk-fn" id="{_attr(element_id)}" data-n="{_attr(number)}">{body}</span>'


class _Writer:
    def __init__(self, book: Book, numbers: dict[str, str] | None):
        self.book = book
        self.setup = book.setup
        self.numbers = numbers or {}
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
                parts.append(_note_html(note, element_id, self.first_number(note)))
            elif isinstance(run, SourceMark) and self.setup.print_source_pages:
                parts.append(f'<span class="nk-src" aria-hidden="true">ص {run.page}</span>')
        return "".join(parts)

    def block(self, block: Block, chapter_running: str) -> str:
        tag = _TAGS.get(block.style, "p")
        element_id = self.block_ids[id(block)]
        extra = ""
        if block.style == "chapter-title":
            extra = f' data-running="{_attr(chapter_running)}"'
        return f'<{tag} class="nk-{block.style}" id="{_attr(element_id)}"{extra}>{self.inline(block)}</{tag}>'

    def running_text(self, heading: str) -> str:
        if self.setup.running_header == "book":
            return self.book.front.title
        if self.setup.running_header == "chapter":
            return heading or self.book.front.title
        return ""


def chapter_anchor(chapter) -> str:
    """The HTML id of a chapter's section (`ch-<id>`): the preview finds each chapter's first page by it."""
    return f"ch-{_SAFE_ID.sub('', chapter.id) or f'c{chapter.number}'}"


def render_html(
    book: Book, stylesheet=None, scope: str = "book", *, numbers: dict[str, str] | None = None
) -> str:
    """The print HTML of `book` (`scope` `book`: front matter and every chapter; `chapter`: the chapters
    in the model only, no front matter). `stylesheet`, when given, replaces the model's page setup;
    `numbers` (note id → shown number) fixes the footnote numbers (D46 pass 2)."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}")
    if stylesheet is not None:
        book = replace(book, setup=page_setup(stylesheet))  # the caller's model is left as it is
    writer = _Writer(book, numbers)
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
    if scope == "book" and front.title_page and front.title:
        parts.append('<section class="nk-front nk-title-page" id="front-title">')
        parts.append(f'<h1 class="nk-book-title">{_text(front.title)}</h1>')
        if front.author:
            parts.append(f'<p class="nk-book-author">{_text(front.author)}</p>')
        parts.append("</section>")
    entries = book.contents() if scope == "book" and front.contents else []
    if entries:
        parts.append('<nav class="nk-front nk-contents" id="front-contents">')
        parts.append(f'<h2 class="nk-contents-title">{CONTENTS_TITLE}</h2><ol class="nk-toc">')
        for entry in entries:
            target = writer.heading_ids.get(entry.target)
            if target is None:
                continue
            parts.append(
                f'<li class="nk-toc-{min(entry.level, 2)}"><a href="#{_attr(target)}">'
                f'<span class="nk-toc-text">{_text(entry.text)}</span></a></li>'
            )
        parts.append("</ol></nav>")
    first_section = True
    for chapter in book.chapters:
        if not chapter.blocks:
            continue  # nothing to print (a chapter left with empty paragraphs): no blank page for it
        parts.append(
            f'<section class="nk-chapter is-{_attr(chapter.kind)}" id="{_attr(chapter_anchor(chapter))}"'
            f' data-chapter="{_attr(chapter.id)}">'
        )
        running = writer.running_text(chapter.heading)
        titled = chapter.blocks[0].style == "chapter-title"
        if (chapter.kind == "section" and first_section) or (chapter.kind != "section" and not titled):
            # the running header's text until the next chapter heading (inside the section, so the mark
            # sits on the chapter's first page and not on a page of its own)
            parts.append(f'<div class="nk-run-mark" data-running="{_attr(running)}"></div>')
        first_section = first_section and chapter.kind != "section"
        for block in chapter.blocks:
            parts.append(writer.block(block, running))
        parts.append("</section>")
    parts.append("</body></html>")
    return "\n".join(part for part in parts if part)


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
