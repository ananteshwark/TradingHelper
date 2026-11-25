# Large trading as it happens

NSE names the traders of large deals only afterwards:
- block deals after each block window;
- bulk deals after the close ([INSTITUTIONAL_DEALS.md](INSTITUTIONAL_DEALS.md)).

What can be seen while it happens is how much a stock trades. So the app watches for a stock
trading a large part of a normal day's value within a few minutes: institution-sized buying
or selling, before any file names who.

## How it works

`igs.intraday.surges` runs with the intraday job (`igs-intraday.timer`), every 5 minutes from
09:20 to 15:30 IST.
1. **The stocks:** every NSE stock with a median traded value of ₹2 crore a day or more over
   its previous 20 sessions, the spike watcher's list.
2. **Their quotes:** Upstox's full market quote, 500 stocks a request. For each stock it gives
   the volume traded today so far, the last price, and the quantity bid and offered in the
   order book.
3. **The window:** the volume since the previous scan is what traded in those minutes, valued
   at the average of the two prices.
4. **A surge** is a window that:
   - traded at least 10% of a normal day's value (the median of the previous 20 sessions);
   - and at least ₹10 crore.
5. **Left out:**
   - the first scan of the day, which has nothing to compare with;
   - a gap of more than 12 minutes between scans;
   - the closing session (from 15:40), when trades are at the closing price.

## On Telegram

The deals chat gets a message within 5 minutes of the window, for the first surge of each
stock in a day:

```
LARGE TRADING NOW (information, not a call): 1 stock traded a large part of a normal day
in a few minutes.
BIG: ₹12.1 crore traded 10:00–10:05, 24% of a normal day's value; price up 2.0% (₹100.00
→ ₹102.00); bids 3.0× offers in the order book.
Who traded is not known yet: ...
```

How sending works:
- A later surge in the same stock that day is recorded, not sent.
- A message not sent while Telegram is unavailable is tried again at the next scan.
- After 20 minutes it is marked late and left unsent.

## What it can't tell you

- **Who traded.** A surge can be one institution, several, or many smaller traders at once,
  such as on news.
- **Which way.** The price's move over the window and the order book (more quantity bid than
  offered, or the reverse) are hints, not proof.
- **Whether it pays to follow.** The app's study of 2013–2026 block deals found the move on
  the deal day itself, before the deal was public, and no reliable move after it
  (INSTITUTIONAL_DEALS.md). A surge is seen sooner than a deal file, within minutes, so it
  may differ. The record below will show.

## The record

The **Institutional deals** page, "Large trading as it happens", lists every surge. For each
one it shows what followed:
- the stock's move from the end of the window to the day's close;
- the next session against the Nifty 500;
- the institutions that day's bulk and block deal files named in the stock, once NSE
  publishes them.

The averages are signed for the way the price went in the window: positive means it kept
going that way.

Nothing here trades. A rule to act on surges would have to be tested on this record first,
after charges.

## Settings

Settings > Alerts, `trade_surges` (`config/alerts.yaml`):

| Setting | Default | What |
|---|---|---|
| `enabled` | true | the scan, the messages and the record |
| `min_share_of_day` | 0.10 | the share of a normal day's value, at least |
| `min_value_cr` | 10 | the value in crore, at least |

**These defaults are starting points, not tuned.** The 5-minute history isn't loaded where
the app was built. The page shows how many surges a day they give; raise them if there are
too many.

It needs the Upstox access token (Settings > Upstox). It adds three quote requests a scan.

Migration 087 adds `trade_surge` and `trade_surge_snapshot`.
