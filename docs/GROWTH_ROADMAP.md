# Growth-stock roadmap

Approved October 1, 2026. Preserve the user's saved history threshold.

1. Short-history growth: matched quarterly and two-quarter revenue/PAT YoY,
   operating-margin change, positive-base guards, point-in-time provenance.
2. Separate experimental Early growth / Established growth profiles, persisted
   per run, with coverage and risks; no price or sentiment substitute for growth.
3. Historical financial/ownership coverage audit and source-backed backfill.
4. Sustainable growth: diluted EPS, cash backing, reinvestment and organic growth.
5. Sector-specific measures, beginning with banking/NBFCs.
6. Forward evidence: guidance, order books, capacity, segments and delivery tracking.
7. Sentiment coverage/deduplication and corporate-action-aware volume signals.
8. Out-of-sample validation with costs before production weighting changes.

## First increment

New growth profiles are experimental research filters; the existing composite and
its weights are unchanged. Early growth requires positive latest-quarter AND
combined latest-two-quarter revenue and PAT YoY growth. All matching periods must
exist, profits must be positive and the prior profit margin at least 2%. Revenue
growth above 1000% and PAT growth above 2000% do not qualify. Established growth
additionally requires positive three-year revenue/PAT CAGR (at most 300%) and
positive revenue YoY in at least nine of the last twelve quarters. These are
explicit starting rules, not validated forecasts. Margin improvement is evidence,
not a universal requirement (financial businesses have different margins).

Growth strength is the weakest of the four core growth rates, not expected stock
return. Coverage is the available share of those four readings. A tripped hard
risk check blocks qualification; missing/cautionary checks remain visible in the
existing rating and risk panels. A growth profile does not upgrade the rating.
Previous stored runs remain unassessed; new runs persist their exact evidence.

The remaining implementation and its data dependencies are recorded below.

## Remaining implementation (October 1, 2026)

The research toolkit for phases 3–8 is implemented. Data coverage and prospective
validation remain prerequisites for promoting the new measures into ratings.

| Phase | Implementation | Remaining evidence requirement |
| --- | --- | --- |
| 3 | `igs research audit` reports coverage and failures; `igs research backfill --limit 25` replays cached failures and fetches verified listed documents. Failed URLs have retry cooldowns. | Available financial listings begin June 2024; loaded ownership filings cover June/September 2026. Longer archives are still needed. Undisclosed promoter holdings stay unknown. |
| 4 | Diluted EPS growth/dilution, annual cash/PAT, free-cash-flow margin, capex intensity and incremental EBIT/capital measures, with dated sources and missing-data guards. | Organic/acquisition contributions require explicit company disclosures; never inferred from headline growth. |
| 5 | Bank/NBFC advances growth, GNPA, NNPA and capital-adequacy research measures. | Capital adequacy needs a mapped disclosure; other industries need additional comparable sector data before numerical scoring. |
| 6 | Public exchange PDF extraction, source payload/hash/quotes, explicit period/unit/scope, retry/budget controls and later matching reported values. UI displays unscored evidence. | First live downloads timed out. Scanned PDFs require OCR/manual review. Delivery matching requires identical metric, unit, scope and period. |
| 7 | Exact syndicated news deduplication, relative volume, liquid price/volume breakout and directional volume balance. Corporate actions suppress affected volume windows. | Different wording about the same event is not automatically treated as a duplicate. Sparse source coverage remains visible. |
| 8 | `igs research validate --start 2025-01-01 --end 2026-09-30` compares eligible baseline, growth profiles, volume confirmation and disabled sentiment with configured trading costs. Saves rules/configuration fingerprint. | Historical comparisons are exploratory. Prospective results and adequate samples are required; new factors remain zero-weight. |

Public PDF AI extraction requires `IGS_FORWARD_AI_ENABLED=true` and an enabled,
budgeted assistant. The user approved public exchange PDFs only. HTTPS NSE/BSE
hosts are allowlisted; redirects, non-PDF responses and oversized documents are
rejected. Only the first 20 pages / 40,000 characters are examined. No attachment
body fallback is sent to the model. Daily processing attempts five documents and
writes a research coverage audit after scoring. Use `igs research extract --limit 5`
for a bounded manual run. Extraction confidence measures source interpretation,
not the probability of stock returns. Exact quotes do not independently verify
management claims.

The existing seven-quarter setting and production composite weights are preserved.
The new profile evidence and bank/volume/cash measures are research aids; they do
not turn missing history into passing ratings. No production IC approval is written
by the research comparison command.

## Verification and current coverage

The implementation passed 580 regression tests, 25 focused extraction/retry tests,
and the point-in-time gate (78 passed, one skipped). Live run 24 covers 836 stocks:
674 have diluted-EPS growth, 606 cash/profit, 766 free-cash-flow margin, and 835
relative-volume readings. Only a subset has enough comparable peers for ranking;
the coverage audit reports these counts separately. There are 171 experimental
Early growth candidates and no Established growth candidates.

Bank measures and incremental EBIT return currently have no computable readings
in the screened universe. The latter requires both balance-sheet dates, positive
capital growth, and earnings periods aligned with the balance sheet. Missing
inputs remain unavailable. The first two PDF downloads timed out before model
submission; the scheduled extractor retains retry status.

The 2025-01-01 through 2026-09-30 comparison completed all five variants with
seven rebalance dates each and zero realized portfolio periods. All variants
are **INCONCLUSIVE**. The loaded benchmark is a price index rather than a total
return index, another limitation to resolve before evaluating excess returns.
Artifacts are saved locally under `reports/research/validation/`; the current
coverage audit is `reports/research/coverage.json`.
