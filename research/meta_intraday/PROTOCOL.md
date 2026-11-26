# Which intraday calls work: protocol

Written and fingerprinted before any outcome of this study was computed.

## Question

The owner asked what separates the app's intraday calls that reached their target from
those that hit their stop, and for an ML engine that gives only the calls that pay.

1. **The difference:** among the rule calls, how do the ones that reached the target first
   differ, at the moment of the call, from the ones that hit the stop first?
2. **The filter:** can a model trained only on earlier calls pick, among each new call, the
   ones that make money after charges? (Meta-labelling: the rules propose, the model
   decides which to take.)

## Data

Upstox 5-minute candles for the Nifty 200 members of `research/ml/universe_nifty200.json`
(today's members, so survivorship favours buying) and the Nifty 50, from the end of January
2022 to the last complete session in the files. The exchange tick is taken as Rs 0.05 for
every stock (the MIS tick list is today's, not historical). Price bands and news are not
replayed.

## The calls

The app's rules as they are now (`intraday-v4`, `igs.intraday.engine.evaluate`), replayed as
`igs.intraday.backtest.replay` does: on every closed candle, 20 seconds after it closes,
with the previous 30 days' same-time candles, the previous session's last candle and the
Nifty 50 up to that moment; the first call of each stock and day. Each is resolved as the
paper record does (`igs.intraday.outcomes.resolve`): a limit at the call's price filling
before it expires, then the first of stop and target, a candle touching both counting as
the stop, else a time exit at 15:15. Its result is in R after the Upstox intraday charges
on Rs 1 lakh (`igs.intraday.costs`), and in rupees.

## Inputs at the moment of the call

Signed in the trade's direction where a direction applies (a positive value favours the
trade):

| Input | |
|---|---|
| side | buy 1, sell 0 |
| time | minutes from 09:15 to the call candle |
| volume jump | log of the candle's volume against its same-time median |
| momentum | the 15-minute move, % |
| from VWAP | the close's distance from the session VWAP, in five-minute ranges (ATR) |
| day move | % from the previous close |
| gap | the open's % from the previous close |
| stop width | the stop distance as % of the price |
| market | the Nifty 50's move since its open, % |
| against the market | the day move less the Nifty's |
| close in candle | where the candle closed in its range, toward the trade (0 to 1) |
| candle size | the candle's range in ATRs |
| past the opening range | the close beyond the first 15 minutes' high (buy) or low (sell), in ATRs |
| day range | the session's high less low, % of the previous close |
| room to the extreme | the distance from the close to the session's high (buy) or low (sell), in ATRs |
| turnover | log of the rupees traded in the session so far |
| price | log of the close |
| yesterday | the previous session's return, % |
| five days | the return over the five previous sessions, % |
| market yesterday | the Nifty 50's previous-session return, % |
| weekday | Monday 0 to Friday 4 |

## Part 1: the difference (development calls only, entries before 2025)

Among filled calls: for target first and stop first, each input's median, and the
standardized difference of their means (difference / pooled standard deviation). For each
input, the win rate and average R after charges of all filled calls by quintile. Described,
never chosen on.

## Part 2: the filter

**Walk-forward:** for each quarter from January 2023, the models are fitted on every filled
call before the quarter and predict the R after charges of that quarter's calls. Unfilled
calls trade nothing and are not fitted.

**Models, fixed settings:**
- ridge regression on standardized inputs (clipped at 5 standard deviations), alpha 1;
- gradient-boosted trees (LightGBM): 300 trees, learning rate 0.03, 15 leaves, at least
  200 calls a leaf, 70% row and column sampling, L2 penalty 5, seed 7.

**Candidates:** each model, taking a call when its predicted R after charges is at least
0, 0.10 or 0.25: six candidates.

**Measure:** the rupees after charges of each taken, filled call on Rs 1 lakh. Each trading
day with a taken call counts once, at the sum of its calls; t = mean / standard deviation
x sqrt(days). Also reported: calls, win rate, average R after charges, rupees a day.

**Choice:** on the development years 2023-2024, the candidate with the highest t, if its
mean is positive and it took at least 100 calls. Otherwise no holdout is run.

**Holdout:** January 2025 to the end of the data, run once for the chosen candidate. These
years' candles have been used in other tests here (`research/ml`); this filter hasn't.

**Passing:** a positive mean with t at least 2, positive in both 2025 and 2026, and at least
150 calls.

If it passes, the app scores each live intraday call with the chosen model, fitted on every
call to the end of the data, and shows and alerts only the calls it takes, tracked forward
on paper. Automatic orders stay as the owner sets them. If it doesn't, the app keeps its
calls as they are and the report says what Part 1 found.
