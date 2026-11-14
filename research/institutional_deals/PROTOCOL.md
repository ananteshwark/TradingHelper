# Protocol: how prices moved after prominent institutions' block deals, 2013–2026

Written on 17 October 2026, before any return below was computed. Its sha256 is in
`PROTOCOL.sha256`. This is a description for the institutional-deal alerts' "possible
impact" line, not a strategy test: nothing passes or fails, and nothing is traded on it.

## Data

- **Deals:** NSE's block deals, January 2013 to 8 October 2026, as kept by
  `research/move_starts` (one JSON file a month): date, symbol, client, side, quantity
  and price.
  - Bulk deals have no usable history: NSE's listing returns at most 70 a request.
  - The impact of a bulk deal is therefore described by block deals of the same size.
- **Institutions:** `config/institutions.yaml`, as committed with this protocol. A client
  matches by its name fragments. Unmatched clients are left out.
- **Prices:** adjusted daily closes for every NSE EQ stock (`research/ml`), and traded
  value from the bhavcopies.

## Events

- **One event** is one institution's net trade in one stock on one day: its client names
  added up, buys minus sells.
- **Side:** buy or sell, from the sign. The price is the volume-weighted price of that
  side.
- **Size:** the event's value over the stock's average daily traded value in the 20
  sessions before it, in three groups: under 1×, 1–5×, 5× and more.
- **Type:** FII or DII, from the register.

A block deal has a buyer and a seller. When both are in the register (an FII selling to a
DII, say), the same stock-day appears once as a sell and once as a buy. That is kept,
because each alert is about one institution, and it is reported.

## Returns

Each window starts from the deal day's close, because a deal is known only after it:
- **Next session:** to the next session's close.
- **21 sessions:** to the 21st session's close.
- **The deal day itself** (previous close to the deal-day close) is reported as the move
  already seen when the alert arrives.

Each return is measured against the equal-weighted return, over the same days, of the
500 most-traded stocks at the last month-end before the deal.

## Statistics

For each type, side and size group, and for each type and side overall:
- the number of events;
- the mean and median excess return in each window;
- the share of events above zero;
- a t-statistic clustered by calendar month (the mean of the monthly averages over
  their standard deviation, times the square root of the number of months).

The same figures are given for 2013–2019 and 2020–2026, to show whether they held.

## Output

`impact.json` gives these figures by type, side and size group. The app's alert quotes the
matching group's number of events, mean next-session and 21-session excess return, and
t-statistic, as history, not a forecast. A group with fewer than 30 events is quoted as
too few to say.
