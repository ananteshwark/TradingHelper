# Winning traders: same-day bets in NSE's bulk deals, and following them

The **Winning traders** page lists the clients in NSE's bulk deal files whose same-day
bets keep winning. A client that newly qualifies is named once on Telegram (the deals
chat). What they carry overnight is followed on paper ([Following them](#following-them-on-paper)).

## Where the bets come from

NSE's bulk deal file names every client whose trades in a stock add up to more than 0.5%
of the company's shares in a day. For each client and stock it gives:
- the quantity bought, and its average price;
- the quantity sold, and its average price.

A client that both **bought and sold the same stock that day** made a same-day bet the
file shows whole:

| | |
|---|---|
| The bet | the quantity both bought and sold |
| Its result | that quantity × (sell price − buy price), before charges |
| Won | the sell price was above the buy price |
| What it carried | bought less sold, marked at the day's close |

The deals collection (`igs-intraday-deals.timer`, every 15 minutes on weekdays) reads
each new file. The first run also reads every file collected before, since collection
began in October 2026. A later copy of a day's file replaces that day's figures.

## Who counts as a consistent winner

Settings > Alerts, `winning_traders` (`config/alerts.yaml`):

| Setting | Default | What |
|---|---|---|
| `min_bets` | 20 | same-day bets at least |
| `min_win_rate` | 0.8 | won at least |
| `max_p` | 0.01 | chance of winning as often by luck, at most |

**The chance by luck** is how often a coin would come up heads as often in that many
tosses. With 20 bets, winning 16 has a 1-in-170 chance by luck; winning all 20, one in a
million.

**Each client's type, as far as the app knows:**
- an FII or DII, from the hand-written list or SEBI's registers;
- a high-frequency or arbitrage trader, from `exclude` in `config/institutions.yaml`;
- other: a broker, a proprietary trader or an individual.

## What it can and can't tell you

**It can't be copied the same day.**
- The file appears after the close, once the bet is over.
- It shows only clients whose trades in a stock exceeded 0.5% of its shares that day.
  Most of the steady winners are market makers and arbitrage traders. They earn a small
  spread many times over, with fast systems and low charges.

**What can be followed** is what a trader carried overnight. The page shows, for each
client, how the stock did the next session against the Nifty 500, signed for the side
they kept. The paper trades below follow it.

**There is no earlier history to test it on.** NSE's bulk-deal history can't be
downloaded whole: its listing returns at most 70 rows a request (research/move_starts).
So the record starts with the app's own collection.

## Following them, on paper

`igs.follow_winners` trades what the consistent winners carried, on paper, the next
session. The deals collection runs it after reading the files.

**A trade opens** when a consistent winner's bulk deal shows a carried position:
- **the side:** bought more than sold → a buy; sold more than bought → a short sale;
- **one a stock and day:** several winners on one side add up; winners on opposite sides
  cancel the stock;
- **at least ₹25 lakh carried** (`min_carried_cr`), and at most 10 trades a day
  (`max_per_day`), the largest first.

**Only trades that could have been placed count:**
- the trader was already named a consistent winner when the file was read;
- the session it is for had not opened (09:15 IST). The deals collection runs until 20:00,
  so a file published in the evening is in time for the next open.

**The trade:**

| | |
|---|---|
| Size | about ₹1 lakh (`notional_inr`), in whole shares |
| In | the session's open, 0.05% worse (`slippage_pct`) |
| Out | the session's close, 0.05% worse |
| Charges | Upstox's intraday charges (`igs.intraday.costs`), on both orders |
| Prices | the bhavcopy's open and close, series EQ |

**No trade is counted** where it couldn't have been done:
- **no fill:** the stock was locked at its price band (no range all day, or a buy that
  opened and closed at the high, a short sale at the low);
- **not intraday:** the stock traded only trade-for-trade (series BE), which allows no
  intraday trades.

A short sale also needs the broker to allow the stock intraday (Upstox's MIS list); the
paper record doesn't check that.

**On Telegram** (the paper chat):
- before the open: the session's trades, with who carried what;
- after the close, once the prices are in: each trade's result after charges, and the
  record so far.

A note not sent (Telegram unavailable) is retried at the next run, but not after the open
it was for. A result is retried for 3 days.

**Real orders aren't offered.** The paper record has to clear a bar first:
- at least 60 trades;
- positive after charges;
- a t-statistic of 2 or more on the daily results.

The page and every results message say where the record stands against it.

**Settings** (Settings > Alerts, `follow_winners`):

| Setting | Default |
|---|---|
| `enabled` | true |
| `notional_inr` | 100000 |
| `slippage_pct` | 0.05 |
| `min_carried_cr` | 0.25 |
| `max_per_day` | 10 |

## Commands

```bash
uv run igs intraday-deals    # collects the files, then reads the bets (as the timer does)
```

Migration 085 adds `deal_trader_bet`, `deal_trader_scan` and `deal_trader_flag`; migration
086 adds `follow_trade`.
