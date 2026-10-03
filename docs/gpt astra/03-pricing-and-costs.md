# Pricing, Runpod costs, and prepaid-credit exposure

Conversation synthesis · 2026-10-02 · Proposed commercial policy, not a published offer

See [product/editor recommendations](01-product-and-editor.md) and [AI recommendations](02-ai-and-quality.md).

## Latest recommendation takes precedence

**Start with prepaid OCR page packs valid for six months, with volume discounts. Do not require a monthly subscription at launch.**

An earlier recommendation used twelve-month validity. After the user raised the risk of future GPU price increases, the recommendation changed to **six months**. The twelve-month proposal is historical, not the current preferred launch policy.

| Pack | Suggested standard price | Revenue per page | Proposed validity |
|---|---:|---:|---|
| Starter | 1,500 pages for $50 | 3.33 cents | Six months |
| Publisher | 5,000 pages for $125 | 2.50 cents | Six months |

The user proposed **5,000 pages for $100**. The discussion considered it a plausible limited early-customer offer, but the latest risk-adjusted recommendation favors $125 initially. Do not automatically apply an additional launch discount without reviewing margins.

The original conversation also used $49 for 1,500 pages. The later pricing discussion rounded this to $50; the table above follows that later proposal.

These prices are hypotheses for customer testing. Neither willingness to pay nor full profitability has been established. No pricing or billing implementation was changed.

## What the sales/publisher discussion established

The simulated publisher's workload could be uneven: process several books, then spend weeks editing. A monthly reset can therefore feel wasteful even when the advertised price per page is low.

“Can afford $100” is different from “will pay $100 every month.” The customer must see editorial time savings and useful deliverables.

Example discussed: 1,000 pages one month and 7,000 the next. The persona preferred packs unless subscription rollover and total cost made the subscription worthwhile. This is simulated feedback, not actual purchasing behavior.

At full use:

- $50/1,500 = approximately $0.0333 per page.
- $125/5,000 = $0.025 per page: a 25% unit discount against the starter pack.
- $100/5,000 = $0.02 per page: a 40% unit discount against the starter pack.
- If a $100 monthly subscriber uses only 1,000 pages, the effective rate is $0.10 per page.

## Measured local Runpod logs

The user supplied **$0.58 per GPU-hour**, described a 16 GB GPU tier, and said active workers were set to zero. The configured account rate and invoice were not independently inspected.

Read-only Django ORM queries were executed against the local database with PostgreSQL `default_transaction_read_only=on`. No OCR was launched and no database record was changed.

Retained logs dated **2026-10-01** contained:

| Item | Observed value |
|---|---:|
| All RemoteCall rows | 38 |
| Ping rows | 2 |
| OCR request rows | 36 |
| Distinct pages in those OCR requests | 16 |
| Main RTX A4500 group | 35 requests across 15 pages |
| Main group's summed execution time | 475.180 seconds |
| Separate RTX A4000 request | 24.722 seconds on one additional page |
| Total execution across all OCR requests | 499.902 seconds |

The main group's requests covered body, footnote, and page-number regions. Each requested both Qari v0.3 and v0.2. **Do not count each region request as a whole page, or charge the same shared request time twice for the two models.**

All retained OCR requests had API status `ok`; the pages were in final OCR state. That does not prove correct transcription. Older Runpod OcrRun error records also existed, so these retained request logs are not a complete history of unsuccessful processing costs.

The calculations below use the **35-request/15-page A4500 group** and the user's stated $0.58/hour rate:

```text
Average execution seconds/page = 475.180 / 15 = 31.6787
Execution cost/page = 31.6787 / 3600 × $0.58 = $0.0051038
```

| Projection for pages similar to the sample | Execution cost only |
|---|---:|
| 15 sampled pages | $0.0766 |
| 1 page | $0.0051, approximately half a cent |
| 1,500 pages | $7.66 |
| 5,000 pages | $25.52 |

The slowest sampled page accumulated approximately **81.924 seconds**, compared with the 31.68-second mean. A single small sample is not a dependable forecast for dense vowelled books or retry-heavy workloads.

## What the cost estimate excludes

`execution_ms` records Runpod job execution. `total_ms` records how long the caller waited, including queues/retries. Summing caller wait times would double-count overlapping waits and is not a valid invoice calculation.

Runpod documents billing from worker startup to shutdown, including initialization, execution, and idle timeout. Storage is also charged. Active workers set to zero means no deliberately maintained always-on baseline; it does not make startup, idle timeout, or storage free.

Source: [Runpod Serverless pricing](https://docs.runpod.io/serverless/pricing), [endpoint settings](https://docs.runpod.io/serverless/endpoints/endpoint-configurations).

Additional business costs include CPU preprocessing, database, app hosting, file storage, exports, payment fees, customer support, and unsuccessful/repeated processing. Their amounts were not measured here.

Illustrative scenarios for 5,000 pages at the same hourly rate:

| GPU-time scenario | Estimated GPU cost |
|---|---:|
| Observed execution average | $25.52 |
| 50% more billed GPU time | $38.28 |
| Twice the GPU time | $51.04 |

The latter two are planning scenarios, not measured overhead. At a $100 sale price, $74.48 remains after the sample-based execution estimate; **that is not profit**.

## GPU price increases and outstanding credits

Prepaid credits lock the customer's purchase terms while the future cost of fulfillment can change. This is a business exposure; no formal accounting or legal classification was determined here.

Holding processing speed constant:

| Hourly-price scenario | Execution cost for 5,000 pages |
|---|---:|
| Current stated rate | $25.52 |
| Double the rate | $51.04 |
| Four times the rate | $102.08 |

A fourfold price increase would exceed the entire $100 pack revenue before other costs. Price and processing-time increases can also compound.

Recommended protections:

- Six-month credit validity at launch instead of a year or indefinite validity.
- Pricing headroom: favor the $125 bulk pack initially.
- Review prices for **future purchases**, honoring existing sold-credit terms.
- Reserve part of prepaid receipts for unused-credit fulfillment. No fixed reserve percentage was established; size it from conservative cost scenarios and the outstanding balance.
- Track outstanding unused credits, their expiry dates, and estimated fulfillment costs.
- Limit large custom prepaid commitments until costs are predictable.

Expiry reduces the exposure window but does not remove it. Margin and the size of outstanding commitments matter more than expiry alone. Do not rely on customers failing to use their credits to make the offer profitable.

## Proposed credit rules

1. **Unit:** one retained logical book page after splitting. A scanned spread can therefore become two billable pages. Show the page count and charge before starting OCR.
2. **Exclusions:** pages excluded before OCR do not consume credits. Clarify treatment of pages removed after processing.
3. **Retries:** system failures and automatic retries must not create repeated customer charges. Account for that internal cost in pricing.
4. **Fresh processing:** explicitly requested new OCR can be chargeable, with the cost shown first. Define this separately from ordinary correction/editing.
5. **Editing/export:** ordinary editing, formatting, and re-exporting use no additional OCR page credits; apply documented resource limits rather than implying unlimited infrastructure.
6. **Expiry:** each purchased pack has its own six-month expiry. Consume earliest-expiring credits first and provide advance reminders.
7. **Top-ups:** add another pack with its own terms/expiry; do not silently reset or reprice earlier balances.
8. **Volume discount:** the lower rate belongs to the larger purchase. Avoid complicated lifetime-spend entitlements initially.
9. **Storage:** credit expiry must not itself trigger deletion of processed books. Define storage allowance, retention, renewal, and export access separately; unlimited lifetime hosting was not recommended.
10. **Accounting implementation:** use an auditable ledger with atomic reservation/debit/release operations and idempotent job handling so retries/concurrency cannot double-charge.

Exact refund policy, storage prices, retention periods, payment providers, taxes, and jurisdiction-specific terms remain undecided.

## Subscription options later

Separate two kinds of value:

- **OCR consumption:** prepaid pages, or explicitly opted-in automatic replenishment.
- **Ongoing workspace service:** team tools, assignments, templates/fonts, storage, support, and other recurring benefits.

A monthly workspace subscription can later coexist with separately purchased OCR credits. Do not charge a subscription merely to force repeat payment for an identical occasional OCR task.

Annual page pools may suit larger customers eventually, but require enough margin or predictable infrastructure cost. The earlier suggestion of annual pools was narrowed after the GPU-price-risk discussion; it is not a launch recommendation.

## Evidence needed before fixing permanent prices

- Several complete books representative of actual customers, including difficult material.
- Actual Runpod invoices reconciled with successful delivered pages, including startup/idle/retries.
- CPU/storage/export and support cost per customer/book.
- Reviewer time saved and usable Word/PDF handoff.
- Pack utilization, repeat purchase, and preference for packs versus recurring plans.
- Cost distribution by book type, rather than only one global average.

The sales agent's contribution favored prepaid packs and cautioned against a deep bulk discount before measuring difficult-page costs. The measured calculations, specific price proposals, and final six-month risk adjustment were assistant synthesis from the database and subsequent user discussion.

## Repository anchors

- [RemoteCall schema](../../ocr/models.py)
- [Timing and request logging](../../ocr/runpod.py)
- [Existing admin cost summary](../../ocr/admin.py)
- [Runpod operating notes](../RUNBOOK.md)
- [Runpod validation specification](../RUNPOD_SPEC.md)

Future reviewers should rerun read-only aggregates if logs or billing have changed. Do not use the old configured `RUNPOD_PRICE_PER_S` example as the user's current price without checking it.
