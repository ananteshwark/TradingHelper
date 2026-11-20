# Futures and options: protocol

Written on 11 October 2026, before any result below was computed. Its SHA-256 is in
`PROTOCOL.sha256`. Choices between variants are made on the development periods and
written in `CHOICE.md` before the confirmation periods are run, once.

## Question

Three uses of futures and options that the app's research points to. Do any of them make
money after today's costs?

1. **F1:** short stock futures after a bad results reaction. The results-day study found
   these stocks lag the market by about 0.9% over 21 sessions, before costs.
2. **F2:** long-short momentum with stock futures, market-neutral, as AQR-style funds run
   it.
3. **F3:** selling Nifty options with capped risk (an iron condor), monthly (F3a) and
   weekly (F3b).

## Data

- **NSE's daily F&O bhavcopies, January 2012 – 8 October 2026:** every futures and options
  contract traded each day, delisted stocks included. Old format to 5 July 2024, UDiFF
  after.
- **Prices and corporate actions:** the survivorship-free equity panel
  (`research/ml/build_daily.py`): adjusted and raw prices, and the 250 most-traded stocks
  each day.
- **Results dates:** from NSE's board-meeting files (`research/results`).
- **Corporate actions:** NSE adjusts stock futures for splits, bonuses and rights. A
  futures price on day t is multiplied by that stock's equity adjustment factor on day t
  (adjusted close / close), so a split inside a holding is not a fake move.

## Costs (today's rates, for every year)

**Futures:**
- STT 0.05% of the sale value;
- exchange charges 0.00173% a side;
- SEBI fees 0.0001% a side;
- stamp duty 0.002% of the purchase;
- brokerage ₹20 an order on a ₹15 lakh contract;
- GST 18% on brokerage, exchange and SEBI charges;
- slippage 0.03% a side for stock futures, 0.01% for Nifty futures.

**Options:**
- STT 0.15% of the premium sold, and 0.15% of the intrinsic value of long options that
  expire in the money;
- exchange charges 0.03503% of premium;
- SEBI fees 0.0001%;
- stamp duty 0.003% of premium bought;
- GST 18% on brokerage, exchange and SEBI charges;
- brokerage of ₹80 a condor, 1.3 index points with GST (four orders, at 75 units a lot);
- slippage of 0.5 index points a leg at entry.

Cash-settled expiry has no exit order.

## F1: shorting stock futures after a bad results reaction

- **Events:** as in R2 (`research/results/PROTOCOL.md`).
  - The reaction is the close-to-close return from the session before the results meeting
    (B−1) to the session after (B+1), less the average of the panel's 250 most-traded
    stocks on B+1. The stock must be among them.
  - Events with a reaction of −5% or worse count.
- **The trade:**
  - The stock must have a futures contract trading on the entry day, the session after
    B+1.
  - Sell the futures contract that expires on or after the exit day, at the entry day's
    open.
  - Buy it back at the open 21 sessions later.
- **Hedged (primary):** buy the same notional of Nifty futures (the contract expiring on or
  after the exit day) at the same opens. **Naked:** reported too.
- **Periods:** entries in 2013–2020 (development) and January 2021 – September 2026
  (confirmation). No variants.
- **Pass:** the hedged net return an event above zero on development, and above zero with
  t ≥ 2.0 (monthly averages) on confirmation.

## F2: long-short momentum with stock futures

- **Each month's last session:**
  - The universe is the stocks with a futures contract trading that day and a 12-1 month
    momentum in the equity panel (adjusted close 21 sessions ago over 252 sessions ago).
  - Rank them by momentum.
- **The positions:**
  - Buy futures on the top ten and sell futures on the bottom ten, equal notional, at the
    next session's open.
  - Use the contracts expiring on or after the next rebalance.
  - Close at the next rebalance's open. Each month uses new contracts, so every position
    pays a round trip.
- **Return a month:** long leg net plus short leg net, on one unit of each.
- **Periods:** rebalances entered from February 2013 to December 2020 (development), and
  January 2021 to September 2026 (confirmation). No variants.
- **Pass:** a mean monthly long-short net return above zero on development, and above zero
  with t ≥ 2.0 on confirmation.

## F3: Nifty iron condors

**Entry:** at the close of the first session after an expiry, for the next expiry: monthly
expiries (F3a) or weekly ones (F3b).

**Strikes:** relative to the close of the Nifty futures contract nearest that expiry. The
traded strike (contracts > 0) nearest each target:
- sell a call at +d and a put at −d;
- buy a call at +(d + w) and a put at −(d + w).

**Settlement:**
- Held to expiry and settled at the final settlement price.
- That price is the median across strikes of strike + call settle − put settle, using the
  expiring options' settle prices on the expiry day.

**Return a period:** P&L after costs, divided by the capital at risk, w × the futures
price.

**Variants (d, w):**
- F3a: (3%, 2%) or (5%, 3%).
- F3b: (1.5%, 1%) or (2.5%, 1.5%).

**Periods:**
- F3a: expiries in 2013–2020 (development), January 2021 – September 2026 (confirmation).
- F3b: from the first weekly expiry in February 2019 to December 2022 (development), and
  January 2023 – 8 October 2026 (confirmation).

**Choosing:** on development, the variant with the higher t-statistic of returns a period,
if its mean is above zero; otherwise the test fails on development.

**Pass:** on confirmation, a mean return a period above zero with t ≥ 2.0, and an
annualised mean (the mean a period × periods a year) above 6%, the return on cash that
margin forgoes.

## Reporting

Four tests: a single pass is evidence, not proof. Anything that passes goes on paper first,
as with the other models. Leverage and margin are reported, not optimised.
