# Research architecture and completion criteria

October 12, 2026. New strategies and models remain research/paper only. Existing
production weights, the saved seven-quarter threshold, current trading settings,
and the existing frozen intraday ML model are not changed by this package.

## Implemented workflow

PostgreSQL remains the authoritative source. `PitView` builds dated eligible
universes and exposes only public information known at each signal. Research
exports are Parquet feature snapshots, a **separate** future-label table, JSONL
factor provenance, and a manifest recording code/configuration hashes and source
metadata. Execution metadata, the exchange calendar, adjusted price history and
benchmark prices are frozen alongside the features for later model-portfolio tests.
Directory publication is atomic; existing bundles cannot be overwritten
through the research commands. File checksums detect subsequent changes. They do
not authenticate a writer or provide tamper-proof storage; protect and back up the
artifact root using the existing server controls.

The three v1 strategies are independent filters over the existing eligible
universe. Inside each filter the backtest retains the existing composite ranking.
They currently cover non-financial businesses; banks/NBFCs/insurers remain outside
these profiles until their sector-specific definitions and inputs are validated.
All rejection-level checks must be evaluated and clear/not applicable. Missing
checks are not approval. An experiment never upgrades a production rating.

| Profile | Frozen v1 requirements |
| --- | --- |
| Early Growth Inflection (`inflection`) | Latest revenue/PAT YoY at least 10%/15%; both two-quarter YoY rates positive; latest-quarter revenue and PAT YoY acceleration each at least 5 percentage points against the preceding quarter; non-decreasing YoY operating margin; annual cash/PAT at least 0.5; net debt/EBITDA at most 3. |
| Quality Compounder (`compounder`) | Three-year revenue and PAT CAGR each 10–100%; positive revenue YoY in at least 9 of 12 quarters; ROCE at least 15%; annual cash/PAT at least 0.8; nonnegative free-cash-flow margin; net debt/EBITDA at most 2; positive diluted EPS YoY. |
| GARP (`garp`) | Three-year revenue and PAT CAGR each 10–100%; positive trailing PEG at most 1.5; ROCE at least 12%; annual cash/PAT at least 0.8; net debt/EBITDA at most 2; positive diluted EPS YoY. |

Short revenue/PAT growth retains the 1000%/2000% plausibility ceilings. Diluted EPS
growth is capped at 2000%. Acceleration requires four exact quarters, positive
inputs and a >=2% PAT margin in both prior-year base quarters. Annual cash-flow
evidence is not assumed to prove multi-year durability. These starting thresholds
are explicit research hypotheses, not parameters selected to obtain a backtest win.
The exact rules and hash are included in strategy and comparison artifacts.

`igs research validate` runs the five earlier variants plus these three through
the corrected exact-session portfolio simulator with configured costs. Empty
selections remain scheduled cash periods; missing held prices remain unresolved.
The comparison never writes production factor-promotion files.

## Model separation and validation

| Horizon | Implementation and evidence boundary |
| --- | --- |
| Long term (12–36 months) | Export monthly snapshots with 252–756 exchange-session labels. Ridge/LightGBM experiments use the dated fundamental/quality/valuation/price-volume features. Adequate matured labels require years of history. |
| Swing (5–30 sessions) | Daily, weekly or monthly snapshots with a separately specified session horizon. Fits and artifact directories are separate from long-term models. Existing swing studies remain valid historical work; this toolkit does not erase their failed acceptance results. |
| Intraday | Existing five-minute feature pipeline and frozen paper LightGBM model remain separate. The new fundamental snapshot schema must not be supplied to that model. |

Targets are gross total-return-adjusted close-to-close stock returns: enter on
the next exchange session, exit exactly H sessions after entry, and freeze the
signal-date security ID. No stale prices, later listings or missed-entry
redistribution fill a missing label. Delisted/missing exits remain unknown; review
the excluded population before drawing conclusions. Gross labels and model rank
IC are **not** net portfolio P&L or proof of investable alpha.

Training is chronological. Training labels must mature before the validation
boundary minus a configurable calendar-day gap; validation labels must mature
before the holdout boundary minus that gap. Whole signal dates stay on one side
of each split. No future folds are used to fit a past model. Training-only median
imputation, standardization and missing indicators are serialized with the model;
an entirely unavailable selected feature stops training. This imputation is for
ML inputs only and never changes the strategy's missing-data qualification rules.

Ridge uses alpha 10. LightGBM uses 100 rounds, 15 leaves, learning rate 0.03,
minimum leaf size 30, seed 1729 and deterministic single-threaded training. No
hyperparameter search observes the holdout. Specify a feature subset before
evaluation when coverage is inadequate, and record that decision in the research
protocol. The model artifacts contain validation/holdout predictions, per-date
rank IC, MAE/RMSE, return-calibration buckets, split counts, library versions,
lockfile/data/code hashes and a paper-only stage. Re-running a holdout is still
exploratory; the toolkit does not claim that it is an untouched preregistered test.

Paper prediction requires compatible feature code, horizon and sampling frequency,
and signal times after the latest training label. Drift reports flag an absolute
mean shift above three training standard deviations, missingness increases above
20 percentage points or wholly unavailable features. These are operational review
thresholds, not calibrated financial-risk probabilities.

`research portfolio` takes a complete paper-prediction snapshot and its original
feature bundle. It requires every feature identity, including stocks without
matured labels, to avoid selecting future survivors. At monthly signal dates it
selects the highest forecasts among names with clear rejection checks, known
industry and positive liquidity. Defaults are 20 equal-weight names and at most
25% in an industry. Failure to fill the diversified target goes to cash rather
than increasing concentration. Limits apply at rebalances; prices may cause
weights to drift between them. The shared simulator then charges actual weight
changes, retains missing exits as unresolved, and reports configured costs plus
a doubled-spread/impact stress. It has no broker capability. This is a historical
research comparison, not a forward paper ledger or live risk gateway.

MLflow export is optional and explicit. It logs scalar metrics, parameters and an
artifact-manifest hash to an existing experiment. It requires HTTPS except on
loopback, does not follow redirects, and reads a token from `MLFLOW_TRACKING_TOKEN`.
It does not upload financial data, model files, or register/promote a served model.
Local hashed bundles remain the model record. Model serving and a shared MLflow
model registry require a separately provisioned service.

## Run on the data server

Run from the repository root, after updating and applying existing migrations.
Use an ordinary read-only database role for the new snapshot/operations commands
when possible. They explicitly open repeatable-read, read-only transactions.

```bash
uv sync --locked --all-groups
uv run igs gate run
uv run igs research audit --output reports/research/coverage-new.json
uv run igs research operations --output reports/research/order-state-new.json
uv run igs research strategies --as-of 2026-10-09 \
  --output reports/research/strategies-20261009
uv run igs research validate --start 2020-01-01 --end 2026-10-09 \
  --output reports/research/validation-new
uv run igs research features --start 2017-01-01 --end 2026-10-09 \
  --horizon 21 --frequency monthly --output reports/research/features-swing-v1
uv run igs research train --dataset reports/research/features-swing-v1 \
  --validation 2022-01-01 --holdout 2025-01-01 --algorithm ridge \
  --output reports/research/models/swing-ridge-v1
uv run igs research readiness \
  --comparison reports/research/validation-new/comparison.json \
  --dataset reports/research/features-swing-v1 \
  --model reports/research/models/swing-ridge-v1 \
  --output reports/research/readiness-new.json
```

Dates above are examples, not evidence that the server has that history. Training
stops with inadequate coverage (minimum 100 rows/12 training dates, 20 rows/two dates
in each evaluation split). These are implementation floors, not statistically
sufficient samples. Large universes and long daily histories can be expensive:
begin with a short monthly snapshot and inspect coverage. Feature export currently
loads the dataset and constructs the bundle in memory; partition very long jobs.
Readiness exits 2 when evidence is pending. That is an expected blocked result.

To produce paper predictions, export a new feature bundle whose first signal is
after the model's last training label and run:

```bash
uv run igs research predict --model reports/research/models/swing-ridge-v1 \
  --dataset reports/research/features-later \
  --output reports/research/predictions/swing-ridge-later
uv run igs research portfolio --dataset reports/research/features-later \
  --predictions reports/research/predictions/swing-ridge-later \
  --output reports/research/portfolios/swing-ridge-later
uv run igs research track --model reports/research/models/swing-ridge-v1 \
  --url https://YOUR-MLFLOW-HOST --experiment-id YOUR-EXPERIMENT-ID
```

No command in this workflow places orders, sends notifications or enables a new
live strategy. The optional `track` command explicitly writes metrics to MLflow.

## Remaining work and acceptance criteria

| Checkpoint | Required evidence / remaining implementation |
| --- | --- |
| Real data | Longer financial and ownership archives, usable source PDFs, missing banking metrics, explicit organic/acquisition disclosures, TRI and historical membership coverage. Audit now reports stock-price and benchmark history. Do not manufacture missing facts or dates. |
| Strategy validation | Rerun the eight comparisons with real prices; inspect full missing/delisted populations, costs/turnover and multiple market regimes. Freeze future protocols before collecting prospective outcomes. |
| ML validation | Train on adequate actual history; run the model-ranked monthly portfolio and cost stress alongside the existing rules; stress execution delay and liquidity, and collect forward paper outcomes. Weekly/intraday portfolio schedules remain on their existing dedicated engines. |
| Advanced research | Optuna nested tuning, Dagster orchestration, shared MLflow model registry/serving, Qwen3/FinBERT extraction evaluation and Chronos/TimesFM comparisons remain extensions. They are not silently installed, trained or claimed superior. Existing public-exchange PDF restrictions and AI budgets continue to apply. |
| Execution | Existing approved-order controls remain active. New models have no broker adapter. Stored order-state checks surface submitting/uncertain/unprotected orders and stale checks; actual position reconciliation, portfolio-wide exposure integration and fault-injection exercises still need the live environment. |
| Operations | Verify deployment, freshness, access controls, encrypted backups, restore and rollback drills, broker/exchange requirements and the operator's release decision. A git push or green CI is not a deployment or broker approval. |

LLMs may extract dated evidence and explain results; they must not decide risk
limits or place orders. Model promotion needs an independent costed OOS result,
adequate prospective evidence, no unexplained broker positions, tested recovery and
explicit operator approval. The readiness command inventories these requirements;
it deliberately cannot turn a checklist into live authorization.

## Sources for optional integrations

- [LightGBM Python API](https://lightgbm.readthedocs.io/en/stable/Python-API.html)
- [MLflow REST API](https://mlflow.org/docs/latest/api_reference/rest-api.html)
