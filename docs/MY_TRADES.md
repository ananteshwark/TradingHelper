# My trades: your own Upstox trades, measured

The owner asked the app to monitor the bets they place by hand on Upstox and learn from
them. The **My trades** tab of the Trading page, a Telegram note on each bet, and
`igs my-trades` do that (`igs.mytrades`). Nothing here places, changes or cancels an
order: it only reads the account.

## What it reads

- **Today's trades,** on each intraday check (the `igs intraday` job, every 5 minutes from
  09:00 to 15:55 on weekdays). Each fill comes with its time, product (intraday or
  delivery) and order.
- **The trade history,** on request: the tab's "Read the trade history" button, or
  `igs my-trades import --history [--start YYYY-MM-DD] [--end YYYY-MM-DD]`.
  - Upstox gives the last three financial years, by date only: no times, products or
    orders.
  - It is read a month at a time.
  - A day whose trades were read on the day keeps those, which have times; the history
    adds only the other days.
  - Upstox's own forum reports gaps in this history, so compare it with your contract
    notes.
- **The token:** the trading token if one is saved (Settings, Upstox tab), else the
  Analytics token. Since June 2026 the Analytics token reads the account's orders and
  trades too, from a registered static IP.
- **Off switches:** `my_trades` in `config/alerts.yaml`: `enabled` for the whole thing,
  `notes` for the Telegram messages.

## What counts as yours

- **The app's own orders are left out:** every fill in a stock on a day the app traded
  that stock.
  - Manual trades in a stock on the same day as one of the app's go with them.
  - The tab says how many fills were left out.
- **Mutual funds are left out.**
- **Commodities and currencies** are kept, without charges, and are left out of the
  record after charges.

## Bets

**A bet** runs from a flat position to flat again in one instrument:
- **Intraday** (product I): within the day.
- **Delivery:** across days. A sale beyond the shares bought in the record sold shares
  bought before it; it is counted and left out.
- **F&O:** per contract, across days.
- A fill that reverses a position closes one bet and opens the next.

**Long or short** is the side that opened it.

**A day from the history** whose buys and sales in a stock match is an intraday round
trip without a side or times. Other history days in a stock go to its delivery bets.

**Charges** are estimated from `config/costs.yaml`, per order:
- **Intraday:** the `intraday` rates.
- **Delivery:** the statutory rates and brokerage, plus the depository charge on each day
  shares were sold.
- **F&O:** the new `fno` rates (₹20 an order; futures STT 0.05% and options STT 0.15% of
  the premium sold).
- **Not counted:** an option exercised at expiry pays STT on its intrinsic value, which
  isn't counted.

Your contract notes are the exact figure.

## The market around each bet

Kept once for each timed intraday bet in a stock, from its day's 5-minute candles and the
Nifty's:

- **Before it:**
  - the stock's move on the day (from the previous close);
  - its move over the 30 minutes before;
  - the distance from the day's VWAP;
  - where it stood in the day's range;
  - the Nifty since the open;
  - the minutes since 09:15.
- **What the app had said:** the 09:45 ML call or evening pick that day, a spike alert,
  and the momentum rank in the last score run before the day. Delivery bets keep the
  momentum rank and the rest of what the app had said.
- **While it was open:** the best and worst prices, in your direction. They come from the
  candles after the entry's own, up to the exit's.
- **After it:** what holding to 15:15 would have made, and the price 30 minutes after you
  closed.

## The record

**The summary** covers closed bets after charges:
- won %, net ₹, the charges and their share of your gross profit on winners;
- average win and loss, and profit factor;
- the t-statistic of the net result per bet. Bets on the same day aren't independent, so
  it overstates how sure the result is.

**By group**, each with its bets, won %, net ₹, ₹ and % a bet, and t:
- kind and side;
- entry time;
- the day's move before it (with a 2%+ move, against it, under 2%);
- the 30 minutes before (0.5%);
- time held;
- which bet of the day, and whether the bet before it won or lost;
- size;
- with or against the app's call;
- the momentum rank, for delivery.

**What stood out** is how the record learns:
- A group stands out when it has at least 15 bets in the earlier two thirds of the record
  and a t-statistic of 2 or more either way.
- The later third, which didn't choose it, is then checked: "held", "didn't hold", or too
  few later bets.
- With a dozen groupings, one can stand out by chance. One that held in the later bets is
  worth more, and the record says which.

**Your exits** covers timed intraday bets, before charges:
- what your exits made;
- the best and worst prices while open;
- holding to 15:15 instead;
- the 30 minutes after you closed;
- for winners, what you took against the best price; for losers, the worst price they
  reached.

## Telegram (the trading chat)

- **When a bet opens:**
  - the stock, side, quantity, price, time and size;
  - the market before it, and what the app had said that day;
  - your record on similar bets: the same kind and side, and the same relation to the
    day's move. It's given from 5 bets.
- **When it closes:** the result before and after charges, how long it was held, and the
  day so far.
- **After 15:35:** the day's bets, the best and the worst, what is still open, and the
  whole record.

Each note goes once.

## Limits

- **Charges are estimates.**
- **Best and worst prices** come from 5-minute candles, not ticks.
- **Gaps in the trade history** make some bets look open or unmatched.
- **An F&O position split across sources:** a position held overnight, with one end read
  on the day and the other from the history, is two bets, because the history doesn't
  give the contract's instrument key.
- **Too few bets:** below about 100 bets, most differences between groups are noise.

## Data

- `upstox_fill`: every fill read, never changed.
- `my_trades_import`: each import.
- `my_bet_context`: each bet's market context, computed once.
- `my_trade_note`: the notes sent.

Migration 079 adds them. The tab is shown to the administrator only.
