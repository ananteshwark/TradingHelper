# Winner/loser differentiators for future trade-call research

Updated 7 November 2026. Numerical evidence covers completed server paper calls through **6 November 2026**. This is a descriptive, side-specific reference for future calls, not a newly validated trading rule.

The clearest observed profiles were:

- **Morning shorts:** winners generally had a less extreme opening sell-off, a smaller opening range relative to normal volatility, and higher average traded value.
- **Morning buys:** winners had often fallen further over the previous five sessions and outperformed the Nifty less aggressively in the opening half-hour. Their previous session's final hour was slightly stronger.
- **Spike shorts:** winners tended to follow a flat/weak previous session, rather than a previous-day rally. They occurred later and amid more same-direction spikes, although those timing/breadth associations weaken within individual days.
- **Evening shorts:** winners followed weaker stock performance and lower relative volume into the previous close. Only four winning shorts exist and there are no shorts in the later comparison period.
- **Market context:** less extended Nifty conditions accompanied winning morning buys; a stronger Nifty final hour in the prior session accompanied winning evening calls. Market values are shared by calls on a date and cannot identify which stock wins on that same date.

These are differences in the recorded sample. The first simple filters built from earlier data **reduced later profit**. Use the profiles to label and investigate future calls; do not convert these medians into entry cutoffs, add arbitrary weights, or override the existing models.

## What was compared

A winner has positive **net** paper P&L; a loser/non-winner has net P&L at or below zero. Entry-time stock features, prior-session activity and contemporaneously available market inputs were compared separately. No exit price, target hit, future high/low, or end-of-day information unavailable at the decision was used as a predictor.

| Track | Buys: winners / losers | Shorts: winners / losers | Trading dates represented |
|---|---:|---:|---:|
| Morning ML | 15 / 17 | 18 / 15 | 18, plus one monitored no-pick session |
| Evening ML | 9 / 7 | 4 / 14 | 9 |
| Spike ML, timely and eligible | 9 / 8 | 30 / 20 | 7 |
| Rule-based intraday | 286 / 457 | 169 / 248 | 22 |

Morning, evening and spike outcomes include their configured paper charges/slippage. Rule outcomes use current configured sizing and estimated charges, without additional slippage. Rule calls can overlap in one stock/day and do not represent an executable portfolio with a capital limit.

The original chronological partitions were retained: morning earlier through 28 October/later from 29 October; evening earlier through 3 November/later from 4 November; spike earlier through 3 November/later from 4 November; rules earlier through 26 October/later from 27 October. This follow-up has already seen those periods, so **both blocks are now exploratory data**, not a fresh holdout.

“Same direction in both blocks” below means the winner-minus-loser median and standardized mean differences agree in direction in both blocks, with at least three winners and three losers in each. Three is only a reporting minimum, not statistical validation. Several variables were inspected; no discovery is presented as significant.

## 1. Stock characteristics and activity before the call

All numbers below are **winner median / loser median**, using the full sample for the stated side. Percentage returns are actual percentages, not raw fractional feature values. A smaller opening loss means a less negative return, not a recommendation to reverse the trade.

| Track / side | Parameter | Winners / losers | Repeatability and interpretation |
|---|---|---:|---|
| Morning short | First-30-minute return | **−2.26% / −3.77%** | Same direction in both blocks. Shorts entered after a less extreme initial decline did better in this sample. Within-day winners were less negative on only 4 of 7 mixed-outcome dates. |
| Morning short | First-30-minute range / 14-day ATR | **1.05× / 1.95×** | Same direction in both blocks; lower for winners on 4 of 7 comparable dates. An unusually exhausted opening move is a useful context flag, not a proven rejection rule. |
| Morning short | 20-session average traded value | **₹334.05 crore / ₹189.99 crore** | Same direction in both blocks; higher for winners on 4 of 7 comparable dates. This is traded value, not market capitalisation. |
| Morning short | ATR / previous close | **3.12% / 2.84%** | Same direction in both blocks; higher for winners on 5 of 7 comparable dates. Winners had more normal daily movement despite less extreme opening movement. |
| Morning buy | Previous five-session return | **−7.66% / −3.74%** | Same direction across blocks, but lower for winners on only 3 of 5 comparable dates and the average within-day gap points the other way. Mostly a contextual observation, not a reliable stock selector. |
| Morning buy | Opening return minus Nifty opening return | **+1.18 / +1.70 percentage points** | Same direction across blocks; winners had lower excess opening strength on 4 of 5 comparable dates. More opening outperformance was not automatically better. |
| Spike short | Decision-candle start, minutes after 09:15 | **195 / 62.5 minutes** | Roughly 12:30 / 10:17:30; the second value is an interpolated median, not an actual candle time. Same direction across blocks, but later winners on only 3 of 6 comparable dates. |
| Rule buy | Stop distance / entry price | **0.663% / 0.727%** | Same direction across blocks; lower for winners on 8 of 12 comparable dates. This does not justify narrowing stops: changing the stop would change the labels and trade path. |

Do not generalise the morning-short opening-range observation to buys: the earlier **pooled** opening-range filter failed later. Keep model and trade side explicit.

## 2. Previous-day activity

For morning calls, “previous session” is the completed session before the morning decision. For evening calls, it is the completed **signal session before the trade**, whose close the evening model uses. “Final hour” follows the stored feature definition, from the 14:30 candle's open to the session's last close. Values below describe the stock unless explicitly labelled Nifty.

| Track / side | Previous-session parameter | Winners / losers | Evidence and future-call context |
|---|---|---:|---|
| Morning buy | Final-hour stock return | **+0.713% / +0.639%** | Higher in both blocks, but the full-sample separation is only 0.074 percentage points and higher on 3 of 5 comparable dates. Weak supporting context. |
| Morning short | Open-to-close stock return | **+0.456% / +0.290%** | Higher in both blocks; 4 of 7 comparable dates agree, but the average within-day difference reverses. Record it, do not infer a strong stock-level effect. |
| Morning short | Final-hour stock return | **+0.222% / −0.130%** | Higher in both blocks, but higher on only 3 of 7 comparable dates and the average within-day difference reverses. Day composition matters. |
| Spike short | Previous close-to-close return | **−0.065% / +1.714%** | Lower in both blocks: earlier −0.188% / +1.146%, later +0.070% / +1.714%. Lower for winners on 4 of 6 comparable dates. A prior-day rally is a research caution when fading a new upward spike. |
| Evening short | Previous close-to-close return | **−0.221% / +2.278%** | Winners followed weaker stocks. Same-day mean differences point lower on all 3 comparable dates, but just 4 winners and **no later shorts**: unconfirmed. |
| Evening short | Previous open-to-close return | **−0.312% / +1.823%** | Lower for winners on all 3 comparable dates; again no later-period confirmation. Distinguish a genuinely weak prior session from a strong stock being shorted. |
| Evening short | Final-30-minute volume / prior-window average | **1.26× / 1.91×** | Lower for winners on all 3 comparable dates. Prior closing volume accompanied losing shorts here; no later shorts exist to confirm this. |
| Evening short | Prior close relative to session VWAP | **+0.032% / +0.579%** | Winners closed nearer VWAP; lower on 2 of 3 comparable dates. Small, preliminary sample. |

The morning stock's previous close-to-close return and previous closing location did **not** provide a consistent mean-and-median discriminator across both blocks. Evening-buy prior-day return, closing location and relative closing volume also changed direction or failed the consistency check. Keep these measurements in future records, but do not describe their present relationship as established.

For spike buys, only five observations belong to the earlier block. A directional confirmation cannot be made under the minimum class counts. The stored spike feature also maps a missing previous return to zero in its construction; a future export should retain a separate missing-input flag rather than treating every zero as a confirmed flat day.

## 3. Market-level context

These are the Nifty 50 features supplied to the models, not the Nifty 500 outcome benchmark. Morning values use only completed earlier sessions and the first six candles. Spike market values end at the decision candle. No later market close was used.

| Track / side | Market parameter | Winners / losers | Day-aware assessment |
|---|---|---:|---|
| Morning buy | Nifty previous five-session return | **−0.238% / +2.778%** | Lower in both blocks. Rank correlation with mean net P&L per buy on each represented date: **−0.37 over 14 dates**. Descriptive, not significant evidence. |
| Morning buy | Nifty opening gap | **−0.027% / +0.020%** | Lower in both blocks. Daily rank correlation **−0.53 over 14 dates**. Do not turn these near-zero medians into a hard threshold. |
| Morning short | Nifty previous-session return | **+0.203% / +1.048%** | Winning shorts followed less prior market strength in both blocks. A market context observation; it cannot distinguish stocks sharing the same morning. |
| Evening, both sides | Prior-session Nifty final-hour return | **+0.334% / +0.021%** | Daily rank correlation **+0.77 over 9 dates**; +0.89 in six earlier dates and +0.50 in three later dates. Later dates contain only buys, so this is not a confirmed short-side effect. |
| Evening buy | Prior-session Nifty final-hour return | **+0.334% / +0.082%** | Daily rank correlation **+0.60 over 6 represented dates**, +0.50 in each three-date block. One of the clearer contextual associations, still a very small sample. |
| Spike short | Same-direction spikes seen so far | **41 / 30 stocks** | Higher in both blocks, but higher for winners on just 3 of 6 comparable dates. Counts increase with time and depend on scan coverage. |
| Spike short | All spikes seen so far | **90 / 59.5 stocks** | Higher in both blocks, but daily rank correlation with mean P&L is **−0.25**. The trade-weighted association does not translate cleanly to a better trading day. |

For example, the pooled morning Nifty five-day-return association looked useful when every trade was counted separately, but its daily correlation was only **−0.03 over 18 trade dates**. This is why market effects must be evaluated by date and side, not by treating each stock as an independent market observation.

## 4. How to use this document for future calls

Use the profiles as **research annotations alongside the unchanged call**, with no position-size increase, automatic rejection, or new confidence score. The following is a field checklist, not a new trading strategy:

| Future call | Record stock context | Record prior-session context | Record market context |
|---|---|---|---|
| Morning buy | Five-session return, opening excess return, opening range/ATR, liquidity | Stock close-to-close return, final-hour return, closing location | Nifty five-session return, prior-session return, opening gap and first-30-minute return |
| Morning short | Opening return and range/ATR, ATR%, traded value | Prior intraday return, final-hour return and closing location | Nifty prior-session return, gap and opening return |
| Evening buy/short | ATR%, 20-day high/low distances, liquidity | Prior full-session and final-hour returns, closing VWAP distance, final-30-minute relative volume | Prior Nifty full-session, final-hour and final-30-minute returns |
| Spike buy/short | Spike size, time, speed, current candle shape, circuit room | Prior-session return **and whether it was genuinely observed** | Nifty move to decision, 30-minute move, same-direction/all-spike counts and scan coverage |
| Rule buy/short | Relative volume, stop width, reward/risk, candle closing location | Not present in this compact export; obtain a historical snapshot before interpreting it | Not present in this compact export; do not fill it using later prices |

Each record should also preserve model/version, trade side, decision timestamp, exact feature definitions/units, missing-data flags, selected/not-selected status, and eventual executable net outcome. A useful annotation is: “Opening range is larger than the historical winning-short median; evidence is exploratory and mixed within days.” An inappropriate annotation is: “Reject because opening range exceeds 1.05 ATR.” **The medians are descriptions, not trained thresholds.**

For the next study, freeze any chosen profile before collecting future outcomes. Compare an unchanged baseline with a shadow candidate on the same dates, after costs, using daily paired P&L and separate buy/short records. Do not optimise weights on the data in this document and then call this period a test. The prior morning/spike median filters lost ₹4,533/₹7,557 of later profit relative to the unchanged calls.

## Coverage and reproducibility

This analysis covers the stored numeric call inputs. It does **not** establish effects for valuation, quarterly growth, sector-relative strength, delivery percentage, prior-day full-session relative volume, VIX, order-book liquidity or futures open interest: matching point-in-time feature rows for those variables were not in the frozen export. “Not tested” is different from “not useful.” AI and results-reaction target horizons had not matured; momentum periods had insufficient independent history and retrospective timing.

- [Reproducible analysis](../research/paper_differentiators/analyze.py) emits all features for pooled, buy and short groups, including winner/loser medians, earlier/later comparisons, within-day gaps and daily rank correlations.
- [Machine-readable research hypotheses](../research/paper_differentiators/hypotheses.json) records selected differentiators and their limitations. It is not imported by scoring or order code.
- [Tests](../tests/test_paper_differentiators.py) check side separation, missing observations, repeated-day handling and unsupported temporal confirmation.
- Numerical source: the local frozen server snapshot at `reports/paper_filters_2026-11-06/snapshot.json`; SHA-256 is recorded with the hypotheses. Raw data and full JSON output remain in the ignored reports directory.

Reproduce from the repository root:

```bash
.venv/bin/python research/paper_differentiators/analyze.py \
  reports/paper_filters_2026-11-06/snapshot.json \
  reports/paper_filters_2026-11-06/differentiators.json
.venv/bin/pytest -q tests/test_paper_differentiators.py
```

There are no model, execution, production configuration or deployment changes. When publishing these research/documentation changes, add a section for this study to the shared branch's pull-request description.
