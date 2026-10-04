"""The shapes `research.services` answers with (D107, D108): pydantic models, so the JSON API
(`research.api`) and the MCP tools' structured output (`research.mcp_server`, whose output schemas they are)
are one contract. Field descriptions are written for the AI clients that read the tools' schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal["body", "notes"]
KindFilter = Literal["body", "notes", "all"]
State = Literal["reviewed", "doubtful", "unreviewed"]
ReviewState = Literal["reviewed", "partly_reviewed", "unreviewed"]
Status = Literal["exact", "differs", "needs_image_check", "not_found", "too_short"]
Attribution = Literal["ok", "note_not_author", "body_not_editor", "other_book"]
AttributedTo = Literal["author", "editor", "unknown"]


class BookRef(BaseModel):
    id: int
    title: str
    author: str = ""


class PageRef(BaseModel):
    """Where a page is: its place in the scan and the number printed on it."""

    number: int = Field(description="The page's place in the scanned book (1 = first page kept).")
    printed: str | None = Field(
        default=None, description="The page number printed on the page; null when unknown. Cite this one."
    )
    printed_source: Literal["page", "inferred", "none"] = Field(
        default="none",
        description="`page`: read on the page; `inferred`: from the numbers of the pages around it; `none`.",
    )


class WordState(BaseModel):
    text: str
    state: State = Field(
        description="`reviewed`: checked by a reviewer; `doubtful`: the OCR reading is uncertain and nobody "
        "settled it; `unreviewed`: a confident reading nobody checked yet."
    )


class Hit(BaseModel):
    passage_id: str = Field(description="Stable id of the matched words; pass it to get_passage and cite.")
    book: BookRef
    page: PageRef
    end_page: PageRef | None = Field(default=None, description="Set when the match runs onto the next page.")
    kind: Kind = Field(description="`body`: the author's text; `notes`: the editor's footnotes.")
    mode: Literal["phrase", "all_words", "fuzzy"] = Field(
        description="`phrase`: the words in order; `all_words`: every word on the page; "
        "`fuzzy`: a close match."
    )
    score: float = Field(description="0–100; 100 for phrase and all-words matches.")
    before: str = Field(description="Words before the match (context).")
    match: str = Field(description="The matched words as printed (diacritics kept).")
    after: str = Field(description="Words after the match (context).")
    words: list[WordState] = Field(description="The matched words with their review state.")
    doubtful: int = Field(description="How many matched words have a doubtful reading.")
    review_state: ReviewState
    clip_url: str = Field(
        description="Image of the printed lines, the match highlighted (signed link, no login)."
    )
    review_url: str = Field(description="The page in Nassakh's review screen (members only).")
    citation: str = Field(description="Ready citation in Arabic, with the printed page.")


class SearchResult(BaseModel):
    query: str
    normalized: str = Field(description="The query's Arabic normal form (what was searched).")
    kind: KindFilter
    total: int = Field(description="Number of matches; page through them with `offset`.")
    offset: int
    limit: int
    hits: list[Hit]
    message: str = Field(description="One Arabic sentence summing up the result.")


class PassageLine(BaseModel):
    id: int
    order: int
    kind: Kind
    text: str
    bbox: list[int] | None = Field(
        default=None, description="[x0, y0, x1, y1] in page pixels (see page_size)."
    )
    reviewed: bool
    in_passage: bool
    doubtful_words: list[str] = Field(default_factory=list)


class Passage(BaseModel):
    passage_id: str
    book: BookRef
    page: PageRef
    end_page: PageRef | None = None
    kind: Kind
    text: str = Field(description="The passage's words as printed.")
    body: list[PassageLine] = Field(description="Lines of the author's text (the passage and its context).")
    notes: list[PassageLine] = Field(description="Lines of the editor's notes, kept apart from the body.")
    page_size: list[int] = Field(description="[width, height] of the page image the boxes refer to.")
    doubtful_words: list[str]
    review_state: ReviewState
    clip_url: str
    clip_urls: list[str] = Field(description="One clip per page when the passage runs over a page break.")
    review_url: str
    citation: str


class CitationParts(BaseModel):
    author: str = ""
    title: str = ""
    editor: str = Field(default="", description="المحقق")
    publisher: str = ""
    city: str = ""
    edition: str = ""
    year: str = ""
    volume: str = ""
    page: str | None = Field(default=None, description="Printed page (or range); null when unknown.")
    scan_page: int = Field(
        description="The page's place in the scan, for when the printed number is unknown."
    )
    kind: Kind


class Citation(BaseModel):
    passage_id: str
    citation: str = Field(description="«المؤلف، العنوان، تحقيق: المحقق، الناشر، الطبعة، السنة، ص N»")
    parts: CitationParts


class Change(BaseModel):
    type: Literal["replaced", "missing", "added", "moved"] = Field(
        description="`replaced`: the quotation has other words; `missing`: the book's words are left out; "
        "`added`: the quotation has words the book does not; `moved`: the same words in another order."
    )
    quote: str = Field(description="The quotation's words ('' for missing).")
    source: str = Field(description="The book's words ('' for added).")
    source_state: State | None = Field(default=None, description="Review state of the book's words here.")
    needs_image: bool = Field(description="True when the book's reading here is doubtful: check the image.")


class DiffSegment(BaseModel):
    op: Literal["equal", "replaced", "missing", "added", "moved"]
    source: str = ""
    quote: str = ""
    state: State | None = None
    needs_image: bool = False


class DiacriticDifference(BaseModel):
    quote: str
    source: str


class VerifyResult(BaseModel):
    status: Status = Field(
        description="`exact`; `differs` (a difference on a reviewed or confident reading); "
        "`needs_image_check` "
        "(the only differences fall on doubtful readings: the page may be misread, not the quotation); "
        "`not_found` (not in this account's books — never call it fabricated); `too_short` (under 3 words)."
    )
    message: str = Field(description="One Arabic sentence for the reader.")
    quote: str
    normalized: str
    ratio: float = Field(description="Word agreement of the best passage, 0–1 (below 0.6: not found).")
    diacritics_differ: bool = Field(description="Every word matches but some vowels differ.")
    diacritics: list[DiacriticDifference] = Field(default_factory=list)
    changes: list[Change] = Field(default_factory=list)
    diff: list[DiffSegment] = Field(default_factory=list, description="The book's passage word by word.")
    attribution: Attribution = "ok"
    attribution_message: str = ""
    passage_id: str | None = None
    book: BookRef | None = None
    page: PageRef | None = None
    end_page: PageRef | None = None
    kind: Kind | None = None
    source_text: str = Field(default="", description="The book's words for the quotation, as printed.")
    review_state: ReviewState | None = None
    clip_url: str | None = None
    clip_urls: list[str] = Field(default_factory=list)
    review_url: str | None = None
    citation: str | None = None


class BookInfo(BaseModel):
    id: int
    title: str
    author: str
    editor: str = ""
    publisher: str = ""
    edition: str = ""
    year: str = ""
    volume: str = ""
    pages: int = Field(description="Pages of the book (excluded pages not counted).")
    indexed_pages: int = Field(description="Pages with searchable text.")
    printed_range: str | None = Field(default=None, description="First–last printed page number, when known.")
    reviewed_share: float = Field(description="Share of the text's lines a reviewer checked, 0–1.")


class BooksResult(BaseModel):
    books: list[BookInfo]
    message: str
