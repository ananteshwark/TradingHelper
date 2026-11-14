# ML model validation and improvement plan — 14 November 2026

This audit uses the repository's frozen study results, the production paper records
mirrored at 20:16 IST on 14 November, and a new local GPU ablation. It measures the
owner's goals: market-relative investing and trading profit **after charges**. A
historical fit, a positive correlation, or a high trade win rate alone is insufficient.

## What is deployed

The server checkout is `codex/server-app` at `aa6a77a`, with 2 vCPUs, 3.8 GiB RAM and
no NVIDIA GPU. The local computer is on `codex/local-app` with an RTX 3050 (6 GiB).
The three server inference artifacts have the same SHA-256 as the local checkout:

| Artifact | SHA-256 prefix | Use |
| --- | --- | --- |
| `intraday_ml_v1.json` | `bd6b05ae4fe3` | 09:45 stock calls |
| `intraday_evening_v1.json` | `575a841bf125` | Previous-evening calls |
| `spike_ml_v1.json` | `3317ff52a145` | Spike recovery paper calls |

The morning and spike artifacts were fitted through 8 October 2026; the evening
artifact through 23 October. They have stayed fixed so their forward records
remain attributable to one version.

The production server evaluates fixed tree artifacts on its CPU. Model training and
large historical studies belong on the local computer; the server should only receive
a **separately versioned model** after a frozen, later paper comparison proves a net
improvement. No candidate in this audit meets that condition.
The last five morning decisions completed at 09:45:54–09:45:55 IST and scored
191–196 of 200 index members, inside the 09:55 deadline. Current server inference
throughput is therefore adequate for this workflow; adding GPU infrastructure
there would address no observed timing problem.

The local database mirror reports `ready`, 97 tables and a successful refresh at
20:16 IST. The 20:30 local analytics service uploaded the 14 November report, and
the server stored it with `source: local`. Its 22:00 fallback can produce the same
report if the computer is off. The first analytics attempt at boot failed while the
mirror refresh was running; the scheduled 20:30 retry succeeded. This is an
operational timing issue, not a model training failure.

## Model inventory and evidence

| Model family | Historical result | Latest independent or forward check | Decision |
| --- | --- | --- | --- |
| Intraday LightGBM, 09:45 (`research/ml`) | 2025–Oct 2026: ₹479/day after simulated costs, daily t 2.4. Seven-seed mean t 1.94; profit concentrated in shorts and a few months. | 78 closed paper calls over 23 decision days, 13 Oct–13 Nov: **−₹1,589** (−₹69/day, descriptive daily t −0.09). Buys −₹3,536; shorts +₹1,947. | Keep the frozen paper record. Historical pass is not confirmed. Do not raise size or promote a replacement. |
| Evening LightGBM (`research/evening`) | 2025–Oct 2026: ₹488/day, daily t 2.02; buys lost and shorts carried the result. | 46 closed paper calls over 14 signal days: **−₹13,317**. Buys +₹13,641; shorts −₹26,959. Three 13 Nov calls were still open when mirrored, so a full-period t-statistic is withheld. | Continue tracking, resolve open calls, and investigate entry/exit availability. Do not infer a side switch from this short record. |
| Spike recovery LightGBM (`research/spike_ml`) | Trading rule failed its prespecified validation bar; 2025–26 test was +0.06%/trade, t 0.24. | 89 selected, closed paper calls across 11 dates: **+₹26,212**; shorts +₹25,039, buys +₹1,173. Ten selected calls were marked missed and are excluded from executable P&L. Across 12 monitored dates including cash days, descriptive daily t is about 0.7. | Paper only. The positive amount is too concentrated and brief to overturn the failed historical rule. |
| Swing LightGBM/ridge (`research/ml`) | Chosen LightGBM +0.55%/month over its universe after delivery costs, t 1.6 versus required 2.0; momentum baseline stronger. | No separate forward model. | Do not deploy. |
| Learned stock rating (`research/ml_rating`) | Trading LightGBM +0.34%/month, t 1.1; investing ridge −0.26%/month against universe. Neither beat the momentum evidence. | Rankings use momentum with a separate paper record. | Do not replace momentum with the learned rating. |
| Learned ranking weights (`research/ai_tuning`) | Four chronological tuning procedures all trailed fixed momentum. The learned linear weights made +0.29%/month over the universe versus fixed momentum's +1.14%/month on the same sample. | No separate forward learned-weight track. | Keep the fixed ranking and judge it prospectively. |
| Breakout-call meta ridge (`research/meta_intraday`) | 108 selected holdout calls, +0.031 R after charges and daily t 0.24; 2026 lost. | No deployment. | Do not use it as a confidence filter. |
| Candle sequence neural model (`research/seq`) | −₹117/day net in 2025–26, versus +₹318/day for its LightGBM baseline. | No forward deployment. | Reject this architecture for current calls. |
| Factor risk/portfolio optimizer (`research/aladdin_engine`) | Its estimated risk model spread momentum across about 55 stocks, but the selected engine made 24.4%/year versus 37.0% for concentrated momentum in 2021–Sep 2026. | Followed as a separate paper portfolio, not an intraday predictor. | Retain as a risk comparison; it has not improved the owner's return goal. |
| Local GPU candle-volume XGBoost (`research/candle_gpu`) | See the ablation below. Both fits confirmed `cuda:0` on the local RTX 3050. | 2026 exploratory results negative; no prospective record. | Keep as local research, never as server inference or orders. |
| Local lab five-session ridge (`src/igs/lab`) | Current six-date holdout mean date IC **−0.097**. Prediction deciles invert: lowest forecast bucket realised +5.49% gross; highest +3.55%. No costed portfolio. | Readiness reports missing net out-of-sample comparison, benchmark/universe validation and forward paper evidence. | Do not promote. Improve target/feature chronology and evaluate a costed momentum baseline first. |
| AI stock calls and local language models | Older cloud models have some one-month excess-return records, but calls overlap by date and stock. Current DeepSeek model has 1,386 calls, 558 directional calls scored **so far** at +2.0% mean direction-aware Nifty 500 edge; no one-month results had matured on 14 Nov. Local Qwen records are only 38 and 3 calls for two versions, without mature horizons. `research/local_models/PROTOCOL.md` defines task-specific quality gates, but no completed held-out evaluation artifact was found. | The latest scheduled recovery produced 57 calls and reported six issues, including malformed or incomplete answers that were rejected. | Do not equate agreement with a cloud answer or short-horizon price movement with an after-cost trading edge. Keep local task routing conditional on schema, evidence, outcome and latency tests. |
| Local filing extraction (`src/igs/gpu_lab`) | A pilot processed four unique announcement PDFs; extraction accuracy and later investing outcomes were not labelled. The queue preserves source, prompt and model identity. | No scheduled worker or forward investment test. | Label a frozen document cohort and compare task accuracy and runtime before using extracted claims as a predictive feature. |

The paper figures use stored estimated charges and fills. They are not broker
reconciliation, and repeated stock calls on one date are not independent trials.
Morning includes zero-pick decision days. Evening's three open calls are not counted
as zero-return cash. Spike `side` denotes the **spike direction**, so trade side is
its negative; the amounts above use trade side and exclude missed entries.
The mirrored `intraday_trade` table also has seven legacy rule trades marked
`exit_unprotected` and two marked `entry_filled`, all dated 6–29 October. No ML
or evening trade has one of those states; eight ML and one evening trades are
stored closed. The stale rule states are **not evidence of current broker
positions**. Reconcile them against Upstox before treating live execution P&L
or order safety as fully validated.

## Opening candles and volume: same-date ablation

The [frozen protocol](../research/candle_gpu/PROTOCOL.md) compares 24 existing
stock/market features with 53 features including 29 opening candle-volume features.
Both GPU XGBoost fits used the same 2022–24 training rows, fixed parameters and
0.15% predicted-move gate. The outcome is 09:50 open to 15:00 open, with Upstox
charges and 0.02% slippage per side on ₹1 lakh positions. The report includes
date-level net P&L, buy/short splits, prediction deciles and paired daily changes.

| Year | Baseline net/day | Added candle-volume net/day | Added minus baseline, paired daily t |
| --- | ---: | ---: | ---: |
| 2025 | +₹306 | +₹464 | +₹158, t 0.68 |
| 2026 | −₹380 | −₹542 | −₹162, t −0.65 |

The expanded model ranked returns slightly **worse** in both years: mean daily
rank correlation 0.0356 versus 0.0377 in 2025, and −0.0041 versus −0.0029 in
2026. Its 2026 buys lost ₹54,880 and shorts lost ₹60,609. At 0.05% slippage per
side it lost ₹749/day in 2026; at 0.10% it lost in both years. The top predicted
return decile in 2026 forecast about +0.45% but realised about +0.15% before
costs, showing overprediction. These results are **exploratory** because 2025–26
were inspected by earlier studies and today's Nifty 200 membership creates
survivorship bias. There is no evidence that more candle-volume complexity makes
calls more profitable. Reproducible details are in
[the study README](../research/candle_gpu/README.md).

**Feature parity exposed a training-data gap.** On the full candle download, the
production feature module rebuilt 205,784 stock-days; 506 rows (0.25%) differed
from the older research builder. Twenty-three stocks have an impossible 09:15
OHLC bar on 25 June 2024. Production rejects those bars, but the research
builder used them and changed each affected stock's following 22-session
history. A pre-recorded post-result sensitivity check excluded the invalid
session and next 22 rows for each stock (529 feature rows, 524 model-eligible),
then refit both models on the **same 2025–26 evaluation rows**. Its 2026 baseline
lost ₹140/day and the expanded model lost ₹522/day; expanded minus baseline was
−₹382/day (paired t −1.50). The original 2026 difference was −₹162/day.
This sensitivity to under 0.5% of training rows reinforces the need for exact
production/research feature parity and several seeds before interpreting a
backtest. It is not a new holdout or an exact rebuilt production training set.

## Performance and data improvements to test next

1. **Fix evaluation comparability before fitting new models.** Freeze a new
   prospective shadow rule and an unchanged baseline on the *same decision dates*.
   Record full candidate coverage, unavailable bars, late signals, missed entries,
   stock universe membership, decision timestamps and exact model/feature versions.
   Validate OHLC and prior-session windows with the same code in research and
   production; the 25 June 2024 case shows why source validation matters.
   Judge paired net daily P&L with zero-call days, both sides, realistic fill delay,
   spread/slippage stress, drawdown and market-relative investing return. Do not
   recycle the already examined 2025–26 data as a fresh test.
2. **Close the simulation-to-order gap.** Compare paper entry/exit assumptions with
   actual broker fills, stops, approval delay, charges and rejected orders, by
   model and side. The 09:45 model's live-style historical variant had lower
   evidence (t 1.7 in 2025–26) than its original paper rule. Check broker state
   before changing live sizing or automating any new strategy.
3. **Version and verify costs.** `config/costs.yaml` still uses NSE equity
   transaction charges of 0.00297%, while [Upstox's current published schedule](https://upstox.com/brokerage-charges/)
   lists 0.00307% from 1 March 2026. At ₹1 lakh each way the difference is about
   ₹0.20 per round trip, small but material to honest reporting. Its delivery
   brokerage/DP assumptions also need comparison with the owner's actual plan.
   Preserve past records and version any revised schedule rather than silently
   rewriting outcomes.
4. **Use local GPU for bounded experiments, server CPU for stable inference.**
   [XGBoost documents](https://xgboost.readthedocs.io/en/stable/gpu/) `device=cuda`
   and `tree_method=hist`, which this run verified as `cuda:0`. Keep the local
   6 GiB GPU for feature ablations, seed ensembles and walk-forward fits that fit
   memory; benchmark time and memory before increasing model size. Export a small,
   versioned artifact only after prospective net evidence, then check local/server
   prediction parity and server decision latency. Do not run training on the
   2-vCPU production server during scans.
5. **Test simpler, stable candidates first.** The historical intraday pass was
   seed-sensitive and the 2026 candle-volume ablation lost. Candidate studies can
   compare seed averaging, separate buy/short calibration, regime stability and
   calibrated abstention against the unchanged model. The existing winner/loser
   medians are hypotheses only: their first filtered variants cut later profit.
   Every extra variant needs its own future test and accounting for trials.
6. **Separate language-model quality from return prediction.** For each AI task,
   report valid structured answers, quote/evidence checks, missing-stock coverage,
   latency, token/compute cost and independent outcome comparison. Group stock-call
   outcomes by decision date and avoid choosing a model on agreement with another
   model alone. Do not claim FinGPT/Qwen is better until its fixed held-out task
   gates and a later forward record are available.
7. **Keep the mirror fast enough for a same-day verdict.** The local snapshot archive
   is about 1.3 GiB and the boot refresh completed at 20:16 after starting at
   20:11 IST. The 20:30 analytics retry succeeded. Monitor duration, bytes and
   freshness as history grows; only consider an incremental transfer after it can
   reproduce a full snapshot, including updates/deletions, with table counts and
   checksums. Retain the server's independent fallback.

## Validation performed and limits

- Confirmed matching SHA-256 values of all three deployed tree artifacts on the
  server and local checkout.
- Recomputed paper track counts, sums and date-aware descriptive statistics from
  the read-only local mirror; checked the server's uploaded local analytics report.
- Ran the 53-feature CUDA study with a saved data hash, training code hash,
  predictions and paired costed comparison. Its `cuda:0` report is under the
  ignored local `data/intraday-gpu/full-experiment-audit3/` directory.
- Ran the full research/production feature parity check and the frozen
  malformed-candle sensitivity fit. Its results are under the ignored local
  `data/intraday-gpu/quality-experiment/` directory.
- Checked the local lab readiness file and held-out metrics. Its blockers remain.
- Reviewed Upstox's published charges and XGBoost's official GPU documentation.

This audit does not establish a newly profitable model, prove fillability or
reconcile live broker orders. No model weights, production thresholds or order
permissions were changed.
