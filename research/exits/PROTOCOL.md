# Smarter exits for intraday trades: protocol

Written on 11 October 2026, before any variant below was run. Its SHA-256 is in
`PROTOCOL.sha256`. Choices are made on the development period and written in `CHOICE.md`
before the confirmation period is run, once.

## Question

Can an exit that adapts while a trade is open (a trailing stop, a break-even stop, a time
stop, a machine-learning exit, a daily loss limit) earn more or lose less than a fixed stop?

## Trades

The frozen ML intraday calls (`research/ml`, walk-forward LightGBM, k = 3 a side, 0.15%
gate), entered with a market order at the 09:50 open, as in `research/winners`. Periods:

- **Development:** 2023–2024.
- **Confirmation:** January 2025 – 8 October 2026. This period has been used twice before,
  for the model and for the winners' tests, so this is a weaker test than a fresh one; the
  forward paper record is the real one.

## Prices and costs

- Upstox 5-minute candles. The entry candle counts.
- A candle reaching a stop exits at the stop, or at its open when it opens beyond it.
- Exits decided at a candle's open (time stop, ML exit, loss limit) use only candles that
  have closed, and fill at that open.
- On one candle, open-based exits are checked first, then the stop.
- A trailing or break-even stop moves only after the candle that triggered the move has
  closed.
- Without another exit, the trade closes at the 15:15 open.
- Costs: Upstox intraday charges on ₹1 lakh, plus 0.02% slippage on the entry and on every
  exit.

## Variants

ATR is the stock's 14-day average true range, as a % of price, from the ML feature table.

- **X0:** no stop, out at 15:15 (the paper rule).
- **F:** a fixed stop 0.5 × ATR from the entry (the best rule in the winners' tests).
  F is the reference that the variants below must beat.
- **Trailing (T):** a stop m × ATR behind the best price since entry, instead of F.
  m = 0.5 or 1.0.
- **Break-even (B):** F, and once the trade has been up b × ATR, the stop moves to the entry
  price. b = 0.5 or 1.0.
- **Time stop (S):** F, and the trade closes at the 12:00 open (S12) or the 13:00 open
  (S13) if it is losing at the previous candle's close.
- **ML exit (M):** F, and at the 10:30, 11:00, ... 14:30 opens, a model predicts the
  stock's return from that open to the 15:15 open. The trade closes there if the
  prediction, signed for the trade's side, is below τ: τ = 0 (M0) or −0.10% (M10).
- **Daily loss limit (D):** F, and when the average of the day's trades, open and closed,
  marked at a candle's close before costs, is at L or worse, all open trades close at the
  next open. L = −0.5% or −1.0%.

## The ML exit model

- **Training rows:** every stock-day of the 197 stocks with candles at 09:50 and 15:15, as
  if bought at the 09:50 open, at each of the nine decision times.
- **Features**, from closed candles only:
  - the stock's move since entry;
  - the best and worst moves since entry;
  - its last 5-, 15- and 30-minute returns;
  - its distance from the session's VWAP;
  - its position in the day's range;
  - the minutes left to 15:15;
  - the ATR;
  - the Nifty 50's move since 09:50 and over the last 30 minutes.
- **Target:** the return from the decision open to the 15:15 open, clipped at ±5%.
- **Model:** LightGBM regression with 300 trees, learning rate 0.03, 31 leaves, at least
  1,000 rows a leaf, 80% feature and row sampling, and seed 7.
- **Walk-forward, by year:**
  - 2023 uses a model trained on 2022;
  - 2024 one trained on 2022–2023;
  - the confirmation one trained on 2022–2024.

## Choosing (development only)

In each family (T, B, S, M, D), the variant with the higher t-statistic of daily net P&L,
if that t-statistic is above F's; otherwise the family fails on development.

## Passing (confirmation)

The chosen variants run once, with X0 and F. Against F, a variant passes as either:

- **Adds profit:** a higher mean net return a trade, with a paired block bootstrap of daily
  P&L (21-day blocks, 2,000 draws, seed 7) showing it higher in at least 80% of draws.
- **Cuts risk:** a maximum drawdown of daily P&L at least 20% smaller, with the mean net
  return a trade no more than 0.02 percentage points lower.

Five families are tested: a single pass is evidence, not proof. Anything that passes goes
to the paper ML calls first, and to real orders only with the owner's approval.
