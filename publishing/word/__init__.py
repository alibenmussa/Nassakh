"""The Word (.docx) export (PHASE6_SPEC §5, D53): Nassakh's own OOXML writer of the D43 book model.

`build_docx` (`writer.py`) is the pure renderer; `DocxExporter` (`exporter.py`) plugs it into the export
pipeline as the format `docx` (`publishing.exporters` imports `publishing.word.DocxExporter`)."""

from .options import WORD_VERSION

__all__ = ["WORD_VERSION", "DocxExporter"]


def __getattr__(name: str):
    if name == "DocxExporter":  # imported lazily: the exporter needs Django models, the writer does not
        from .exporter import DocxExporter

        return DocxExporter
    raise AttributeError(name)
