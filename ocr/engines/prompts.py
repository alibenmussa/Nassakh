"""Prompts, copied verbatim from the model cards (D10). Stored on every OcrRun."""

# Qari v0.2 and v0.3 share one plain-text prompt.
PROMPT_QARI = (
    "Below is the image of one page of a document, as well as some raw textual "
    "content that was previously extracted for it. Just return the plain text "
    "representation of this document as if you were reading it naturally. "
    "Do not hallucinate."
)
