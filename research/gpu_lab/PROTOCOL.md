# Local GPU filing extraction: pilot v1

Frozen before the first pilot inference on 25 October 2026. This protocol measures
extraction operations, not investment returns. No parameter, ranking, order, or capital
allocation changes follow from this pilot.

## Sample and inputs

Take up to five most recently received, cached exchange filings with nonempty text from
`forward_document`, ordered by received_at descending then ann_id descending. If that
table has none, take up to five recent NSE announcements with approved PDF attachments
matching the existing forward extractor's topics, ordered by filed_at then ann_id
descending. Record this fallback and all failed downloads. Use the existing bounded
PDF extraction (first 20 pages, maximum 40,000 characters); the text hash identifies the
actual input. This pilot does not establish coverage of complete filings or OCR pages.
Keep source URL, publication and receipt times, source text, and model identity.

Use the model already loaded in Strata. Run the existing forward-evidence system prompt
and schema, temperature zero, maximum 4096 output tokens. Cache by exact document,
model, protocol, system prompt and schema; retain failures, not just successful answers.
The queue tries a job at most three times, with backoff, and one research job at a time.
It yields to existing app usage by retaining gateway busy errors for a later invocation.

## Measurements

Record completion time, model-reported token usage, original and validated claims,
quote/numeric validation rejections, retries and failures. A syntactically accepted
claim is not a verified fact. Independently reviewed labels are required before reporting
precision/recall. Match metric, reported/guidance kind, numeric value, original unit,
scope and period exactly. Record empty correct answers; do not treat unlabeled documents
as empty truth. Failed documents contribute missing facts to recall. Source hash changes
invalidate a label. Exact scope wording can undercount semantically equivalent facts;
review disagreements, but keep original metrics and version any relaxed matching rule.

The first five documents are development/operational checks only. Any model comparison
requires a separate frozen sample of at least 100 documents, stratified across event
types and including no-fact documents, with labels prepared without seeing predictions.
Report all models and failures. Proposed progression criterion: precision >=95%, recall
>=80%, no unit/date/company-attribution errors, and 95th-percentile latency compatible
with the intended workflow. Sample uncertainty must be reported; this is an engineering
threshold, not evidence of financial advantage.

## Investment validation after extraction is reliable

Before the first scored prospective signal, commit a separate protocol specifying the
event-to-signal mapping, universe, original receipt and assessment times, next executable
price, holding/rebalance schedule, benchmark, risk and cash matching, all costs, slippage
stress, sample requirements and stopping rule. Compare the unchanged momentum baseline
with the same portfolio plus the event feature. Log every attempted variant. Measure
paired daily portfolio returns, turnover, drawdown and after-cost difference; account
for time dependence and multiple trials. Do not reuse the historical 2021–2026 research
period as an independent holdout. Current LLM knowledge makes historical extraction
unsuitable for claiming a clean historical prediction test.

No financial promotion is authorized by extraction scores alone. Forward performance
takes calendar time; an operational GPU pilot cannot establish profitability today.
