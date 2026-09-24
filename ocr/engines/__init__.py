"""OCR engines behind one interface (`OcrEngine`), served by `registry.get_engine(name)`.

Engines: `qari_v03`, `qari_v02` (Qwen2-VL fine-tunes on PyTorch/MPS or Apple MLX), `tesseract`
(classic, gives word and line boxes), `pdf_text` (repaired text layer of born-digital PDFs) and
`fake` (deterministic, for tests). Import engine modules lazily through the registry so that the
heavy ML dependencies are only imported in the GPU worker.
"""
