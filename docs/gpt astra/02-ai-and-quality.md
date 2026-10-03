# AI pipeline, quality, visual retrieval, and LoRA

Conversation synthesis · 2026-10-02 · Recommendations for review, not implementation approval

See [product/editor recommendations](01-product-and-editor.md) and [pricing/cost analysis](03-pricing-and-costs.md).

## Engineering assessment

The AI engineer approved a **supervised publisher pilot**, assigning **7/10 engineering maturity**. This is a subjective review score, not 70% OCR accuracy.

| Area | Score |
|---|---:|
| Architecture and traceability | 8/10 |
| OCR quality assurance | 6/10 |
| Operational validation | 6/10 |
| Domain-specific workflow | 8/10 |

Strengths observed:

- Hybrid processing: OpenCV/NumPy preparation, Qari v0.3/v0.2, Tesseract reference/fallback, Kraken digits and word geometry, and human review.
- Difficult-region retries in smaller pieces are already implemented.
- Model caching and separate CPU/local-model/remote-request worker arrangements.
- Stored raw output, model revision, prompt, generation parameters, duration, and completion state.
- Before/after review history and substantial regression coverage.
- Existing ground-truth evaluation, including a 17-page dataset and CER/WER tooling. It would be wrong to say there is no evaluation.

These findings came from code and existing experiments. No new OCR benchmark or test suite was run for this review.

## Highest-priority quality work

1. **Measure errors that escape flags.** Model agreement and “zero uncertain words” are not verification. Two related Qari models can make the same mistake.
2. **Diacritic-aware review.** Some disagreement checks use lenient normalization that removes vowel marks and folds letter forms. This does not establish that every comparison ignores diacritics, but it creates a concrete blind spot in the inspected flag policy.
3. **Omissions and invented text.** Detect missing lines/regions and unsupported additions, not only disagreements between models.
4. **Numbers and footnote attachment.** Measure correctness of the number and its attachment to the intended passage, not merely whether a marker was found.
5. **Fresh held-out evaluation.** Recheck the latest pipeline on unseen books, not only examples used to design fixes.
6. **Cloud validation.** Compare backend output quality, throughput, retries, cold starts, and actual billed cost under realistic concurrent use.

The code's large OCR service module also merits incremental separation of orchestration, selection, alignment, and persistence as maintenance work becomes necessary.

## Historical results: avoid stale conclusions

The [September 30 full-book report](../FULLBOOK_TEST_2026-09-30.md) recorded a complete 120-page path through four exports. A two-page sample contained 10 letter-error words and 10 additional vowel-error words among 257 words; five letter-error words and no vowel-error words were flagged. This was a small historical sample, not a current overall accuracy estimate.

The [footnote report](../FOOTNOTES_REPORT_2026-09-30.md) subsequently recorded:

- 70/74 correctly linked calls across four six-page test books.
- 24/25 correct calls in the sampled history book.
- 17/27 correct calls in the harder vowelled book sample, including one wrong attachment.

Later changes matter:

- **D90:** smaller-piece rereading restored model text on eight difficult pages.
- **D91:** improved line placement using image evidence.
- Newer Kraken word-box work also appeared in the inspected code.

Consult [decisions](../DECISIONS.md) and current source before reusing old findings. Earlier failures cannot simply be labelled unchanged; restored model text also does not prove exact transcription. A fresh benchmark after these fixes is needed.

## Is RAG needed now?

**No, not for launching the core OCR workflow.** Retrieval-augmented generation supplies retrieved material to a generator; it does not directly solve recognition of printed pixels.

Potential later applications:

- Ask questions across a publisher's reviewed catalogue with book/page citations.
- Find related passages and candidate references.
- Retrieve approved spellings, terminology, or earlier corrections as review suggestions.

For current review work, exact/fuzzy search and book-specific correction dictionaries may deliver useful value without a generative subsystem.

**Preserve the edition being transcribed.** A reference book can legitimately use different wording. Never silently replace the scan with a retrieved edition. Show the scan, suggested change, and source for approval. Retrieval must respect publisher access boundaries.

Reference: [Original RAG paper](https://arxiv.org/abs/2005.11401).

## User proposal: retrieve reviewed words by image embeddings

**This is a promising experiment before LoRA.** It is visual retrieval/word spotting and does not require a generative model.

Proposed flow:

1. Save a correctly aligned word crop, verified transcription, and image embedding, with book/page and review provenance.
2. Embed an uncertain word crop.
3. Retrieve visually similar reviewed crops, initially from the same book/typeface.
4. Show the top three examples, their images, and their reviewed readings.
5. Let the reviewer accept a suggestion or choose “no reliable match.”

Model considerations:

- **DINOv2** is a useful baseline to test, not an established Arabic word-recognition solution.
- Its general visual similarity may fail to distinguish tiny but decisive dots and diacritics. Preserve those details in the crops and test explicitly.
- Compare against a text-specific word-spotting approach. PHOCNet is an example of the research direction, not a verified drop-in model for this project's Arabic data.
- Incorrect word boxes, clipped marks, low-resolution scans, and repeated visual shapes can undermine retrieval.
- Similarity scores should not be presented as calibrated probabilities of transcription correctness.
- Missing detail in the source scan cannot be recovered reliably just by retrieving a lookalike.

Evaluate whether the correct reading appears in the top three suggestions, how often suggestions mislead, and whether reviewers finish faster. Separate test queries from indexed copies/near-duplicates so the experiment is meaningful. Broaden beyond one book only after testing generalization and maintaining customer boundaries.

References: [DINOv2](https://arxiv.org/abs/2304.07193), [PHOCNet](https://arxiv.org/abs/1604.00187).

## LoRA later: trigger on evidence, not customer count

LoRA is a parameter-efficient adaptation method, not a guarantee of better OCR.

Prepare useful data now, but start a training experiment when there is:

- A recurring measurable failure type, such as a typeface, dense diacritics, digits, or small notes.
- Permission to use the books and corrections for that training purpose.
- Vetted **image crop → faithful transcription** pairs.
- A held-out evaluation set split by whole books/editions, not random lines from the same books.
- A baseline and a clear acceptance criterion for accuracy, review effort, cost, and regressions.

Final edited prose is not automatically OCR ground truth: an editor may modernize spelling, alter wording, or reorganize the passage. Existing review history is a useful starting point, not a ready-made clean training corpus.

Evaluate raw character errors, diacritic errors, numbers, omissions, invented text, reviewer time, and cost. Five publishers contributing diverse, carefully verified examples may provide more useful data than hundreds contributing noisy edits.

Start with a narrow experiment targeting one demonstrated problem. Avoid committing early to maintaining a general proprietary model or separate adapters for every customer.

Reference: [Hugging Face PEFT: LoRA](https://huggingface.co/docs/peft/main/en/conceptual_guides/lora).

## Repository anchors

- [Preprocessing](../../processing/pipeline.py)
- [OCR orchestration and smaller-piece retries](../../ocr/services.py)
- [Disagreement flags](../../ocr/flags.py)
- [Arabic normalization](../../core/arabic.py)
- [Alignment](../../ocr/alignment.py)
- [Word boxes](../../ocr/boxes.py)
- [OCR and remote-call records](../../ocr/models.py)
- [Review history](../../review/models.py)
- [Runpod validation plan](../RUNPOD_SPEC.md)
