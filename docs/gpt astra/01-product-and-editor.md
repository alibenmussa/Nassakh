# Product, publisher, and editor recommendations

Conversation synthesis · 2026-10-02 · For subsequent agent review

## Read this first

These files summarize a read-only review of Nassakh and subsequent product discussions. They are recommendations, not an approved implementation specification. Creating these documents was authorized; implementing the proposals requires a separate task.

- [AI pipeline, quality, visual retrieval, and LoRA](02-ai-and-quality.md)
- [Pricing, measured Runpod costs, and prepaid-credit risk](03-pricing-and-costs.md)

Evidence levels used throughout:

- **Inspected:** observed in repository source/templates or queried from the local database.
- **Recorded experiment:** an existing report, not a benchmark rerun in this conversation.
- **Simulated feedback:** an agent acting as an Egyptian publisher, not a real customer interview.
- **Proposal:** a suggested product or business decision, not validated demand or a shipped feature.

Three agents reviewed the project as a publisher, product manager/sales specialist, and AI engineer. No live UI acceptance test, new OCR run, or test-suite run was performed in this conversation. No application code or database records were changed by this work.

## Overall recommendation

**Pursue an assisted paid pilot for a clearly defined class of printed Arabic books. The core publishing workflow exists, but a shared self-service paid SaaS launch needs additional work.**

The implemented journey is:

PDF upload → page extraction/splitting → preparation and guides → OCR → review against scans → manuscript assembly → editing/layout → export.

Commercially useful features include Arabic/RTL controls, scan-linked review, alternative readings, corrections across a book, approvals, undo/history, manuscript snapshots, and DOCX/print PDF/screen PDF/EPUB output. Export jobs include progress, cancellation, history, stale-output detection, and validation.

Suggested positioning:

> Turn scanned Arabic books into editable, reviewable manuscripts and publishing files.

Do not promise unattended, error-free, publication-ready conversion. A valid export file is not proof of correct transcription or acceptance by a publisher's editor/printer.

## What the simulated Egyptian publisher wanted

**Purchase verdict:** willing to try a paid book pilot after a representative sample of his own material proves useful. No conclusion about actual Egyptian willingness to pay was established.

His buying criterion was **total editorial time saved per finished book**, rather than OCR speed alone. Silent omissions, wrong numbers, misplaced footnotes, or unusable DOCX output can remove the benefit.

Suggested pilot:

1. Use 10–20 representative customer pages, including ordinary prose, small notes, numbers, poor scans, and vowelled text where relevant.
2. Compare correction effort and output usability with the customer's existing method.
3. If useful, process a complete paid pilot book.
4. Measure whether the publisher returns for another book.

Simulated purchasing priorities, in order:

1. Faithful text, easy correction of footnotes/numbers, and a usable Word handoff.
2. Reusable publisher templates and named styles.
3. A publisher-owned font library with predictable export results.
4. Comments, tracked changes, and review assignments if the team moves into the editor.
5. Pricing that accommodates intermittent work without surprise charges.

The persona would cancel if correction took as much effort as the old workflow, serious errors escaped notice, Word output needed rebuilding, or technical failures consumed credits.

## Editor: existing features versus proposed additions

The editor already has semantic style choices and per-book formatting: trim size, margins, body/heading/Latin fonts, type sizes, line spacing, footnote settings, headers, page numbering, and snapshots. **It is inaccurate to say that the editor has no styles.**

The missing capability identified in this review is a customer-owned library of reusable named styles and book templates. Current `StyleSheet` is one-to-one with a book.

### Reusable publisher templates

Examples: “Heritage series” and “General books.” A template could hold trim, margins, typography, heading styles, footnotes, and page furniture.

Proposed behavior:

- Save the current book's design as a named publisher template.
- Apply it to a new book and allow book-specific overrides.
- Keep existing books stable when the shared template changes; updating them should be explicit.
- Scope templates to the publishing-house workspace, with suitable team permissions.

### Custom named styles

Examples from the simulated interview:

- **House body:** font, size, leading, first-line indent.
- **Long quotation:** smaller type, side indents, spacing before/after.
- **Chapter heading:** distinct type, spacing, keep-with-next behavior.
- **House footnote:** separate type and spacing controls.

Editing a style should update its uses consistently. Preserve semantic meaning: a chapter heading must still participate in navigation, contents, and Word export. Export custom styles as meaningful Word styles where supported.

Do not assume custom styles are the next highest-value feature. The publisher would prioritize easier footnote correction if that is the main source of work, and use Word for formatting temporarily.

### Team editing and Word handoff

The persona would not abandon Word immediately. Prioritize reliable DOCX handoff before attempting broad word-processor parity.

Snapshots and revision history already provide useful protection. They are not equivalent to a full collaborative workflow with anchored comments, suggested edits, accept/reject changes, and assignments. Build those features if customers actually move team editing into Nassakh.

## Fonts

**Inspected:** Amiri is bundled. Other predefined fonts are discovered in local/server font directories; a customer font-upload workflow was not found.

**User proposal:** provide Amiri as the bundled default and let customers upload other fonts.

Recommended product design:

- Put uploaded fonts in the **publishing-house workspace**, not only one employee's preferences.
- Let the workspace define defaults for new books; preserve per-book choices.
- Support font families/weights, including regular and bold where available.
- Preview Arabic, diacritics, digits, Latin text, and footnotes.
- Clearly disclose missing glyphs, unavailable weights, or export limitations.
- Avoid silent font substitutions that unexpectedly alter pagination.

**Licensing qualification:** uploading a font does not itself grant permission for server use, browser delivery, or document embedding. Those uses can have different license terms. Do not treat a font file's embedding flags or a user checkbox as a universal license determination. Microsoft's FAQ illustrates these distinctions for its Windows fonts; other fonts require their own terms.

Source: [Microsoft font redistribution FAQ](https://learn.microsoft.com/en-us/typography/fonts/font-faq).

## Before a shared public paid launch

1. **Customer isolation.** The inspected book list uses a global overview, book views retrieve by ID without customer scope, and protected media checks login rather than publisher ownership. `created_by` is authorship metadata, not an access boundary. Global roles do not provide tenant isolation.
2. **Credits and payments.** No credit ledger, balance, checkout, or quota enforcement was found. Reservation, debit, and release/refund behavior must remain correct through concurrent jobs and retries.
3. **Customer onboarding.** Accounts expose login/logout; admin manages users. Add signup or invitations, password recovery, usage/billing visibility, and support access as appropriate to the launch model.
4. **Outcome and cost validation.** Measure complete customer books, including reviewer effort, severe errors, usable exports, actual billing, and support effort.
5. **Service operations.** Establish deployment, tested backup recovery, monitoring, stuck-job handling, resource limits, and clear storage/deletion policies. The repository contains useful error handling; a production SaaS operating environment was not demonstrated.

An operator-run service or a separate installation per publisher can support an early pilot without first completing shared SaaS architecture. Do not place unrelated customers in the current shared workspace.

## Later feature blocks

Prioritize based on pilot evidence:

1. **Publishing-house workspace:** shared catalogue, assignments, approvals, house styles, auditability.
2. **Difficult documents:** tables, illustrations, multicolumn material, varied print conditions, with evaluation by document class.
3. **Editorial quality assistant:** names/numbers consistency, references, footnotes, approved correction suggestions.
4. **Catalogue search:** discovery and source-linked questions across reviewed books.
5. **Integrations:** batch APIs, archive systems, and private deployment where demand warrants them.

Batch importing, completion notifications, elaborate tiers, coupons, and referral systems can wait. Templates and fonts may move earlier if pilot users identify them as blockers.

## Repository anchors

- [Book access paths](../../books/views.py)
- [Book overview and workflow services](../../books/services.py)
- [Media access](../../core/views.py)
- [Account routes](../../accounts/urls.py)
- [Review UI](../../templates/review/review.html)
- [Formatting UI](../../templates/editor/_book_format.html)
- [Editor models and StyleSheet](../../editor/models.py)
- [Font registry](../../publishing/fonts.py)
- [Export workflow](../../publishing/exports.py)

For future reviewers: verify the current implementation before treating a listed gap as still open. Do not turn simulated interview answers into claims about real customer demand.
