# Continuous monitoring of placed intraday trades

Status: experimental software, observation by default. This is not evidence that a
trailing stop improves the current model's returns. No live broker account was used in
development. The worker is not installed or enabled automatically by this change.

## Scope

`igs trade-monitor` polls while the NSE weekday session is open, sleeping five seconds
after each cycle. This is continuous polling, not a tick-by-tick WebSocket feed; network
latency adds to the interval. It follows today's app-recorded intraday GTT trades only,
not holdings, options, manually placed positions or a new stock-selection strategy.
The existing 20-second approvals/reconciliation worker and its 15:00 ML square-off
remain responsible for the original order lifecycle.

The Trading page has an administrator-only **Open-trade monitor** tab. It shows the
latest mark, actual entry fill, broker stop, observed stop, estimated net mark-to-market,
worker observation age and recent events. Refresh to retrieve the latest observation;
opening the page never starts a trading worker. Missing or old observations are labelled.

The monitor deliberately does not run the morning model throughout the day. That model
was trained for the 09:45 decision; a continuous entry model needs separate validation.

## Policy v1

The policy is deterministic and symmetric for long and short trades:

1. Use the actual fully filled entry and the original approved stop to define initial
   risk per share, R. Never rewrite those original trade fields.
2. Follow the best executable-side price observed **after monitoring began**: bid for
   a long, ask for a short. Do not substitute the day's high/low, which may predate entry.
3. Keep the original stop until the best observed move is at least 1.5R.
4. Then propose a stop one original R behind the best observed price, at least covering
   estimated round-trip charges and a five-basis-point exit slippage budget if market
   distance permits. This is a budget, not a guaranteed break-even fill.
5. Never loosen a stop. Round to the instrument's broker-reported tick size; require an
   improvement of at least five ticks and two ticks of space from the current market.
6. If the observed stop is crossed, record `stop_crossed`. Do not add a second exit order:
   the existing broker stop may already be executing. In observation mode this may be a
   hypothetical stop crossing, not a broker fill.

The 1.5R/1R settings are starting hypotheses, not fitted or validated optimal settings.
They can reduce gains by closing winners early. They live in the immutable `Policy`
configuration; changing them is a strategy version change and needs a new evaluation.
An outage misses intermediate peaks; on restart the persisted observed peak is restored,
but missed highs/lows are not invented. No pyramiding, averaging down, target extension,
new entries, market exits, cancellation or resizing is implemented here.

## Broker execution safeguards

The default command and supplied systemd unit make no broker writes. The separate
explicit `--execute` flag permits only modifying the existing GTT's stop. It also requires
the application's live-trading switch and a valid look-ahead gate. The flag is not a
profitability certification. The owner must independently decide whether to run it.

Before a proposal, and again immediately before a modification:

- Match instrument, intraday product, signed quantity and day's gross buy/sell quantities
  against broker positions. Any partial close, mixed/manual trading or reversal blocks
  management. This is conservative because broker positions are netted by instrument.
- Verify GTT ID, instrument, quantity, product, direction and a fully completed entry.
  Require untriggered scheduled exits. Preserve entry and target rules exactly; do not
  replace an existing broker-native trailing stop.
- Require fresh quote-update and last-trade timestamps (at most 15 seconds old, never
  future), positive two-sided depth, an uncrossed market and spread no wider than 30 bps.
  The displayed mark is not a guaranteed execution price for the full quantity.
- Obtain the actual entry fill and require the full approved quantity.
- Refresh position, GTT and quote before submission. If market/stop changed, withhold.
  Stop modifications cease at 14:59 IST to leave the existing closing worker in control.

A PostgreSQL session lock allows only one monitor cycle at a time. An attempt is
committed as `submitting` before the network request. Read back the GTT to confirm the
new stop; a timeout or ambiguous acknowledgement becomes `uncertain`, and an explicit
broker rejection becomes `rejected`. Those states block automatic retry. A crash after
submission leaves `submitting`, which also blocks retry after restart. Human review is
required; there is intentionally no blind reset/retry button. After confirming the
broker's actual state, an operator can archive a reviewed event as `reviewed` while
retaining its detail; never delete the event to clear it. A `rejected` attempt blocks
further automatic modifications for that trade for the rest of the day.

Events are written every cycle so the monitoring tab is live. Once the session ends
(15:30 IST) the worker deletes that day's routine events (`hold`, `tighten`, `blocked`,
`stop_crossed`), keeping each trade's last event. Modification records (`submitting`,
`confirmed`, `rejected`, `uncertain`, `reviewed`) are never deleted, and
`trade_monitor_state` keeps the final snapshot. Missed days are cleaned on the next run.

Quote freshness uses the order-book timestamp (15 s). The last trade may be up to
120 s old so thinly traded stocks with a live book are still monitored. In observe mode
`stop_crossed` refers to the hypothetical trailed stop, not the broker's stop. A
malformed response or a disabled live switch skips the cycle and keeps the worker
running; a lost database connection exits so systemd restarts it.

Broker snapshots and updates are not atomic together. An exchange fill or manual action
can occur between requests; the broker's validation and linked exit rules remain
authoritative. This worker does not cancel/recreate GTTs to work around a rejection.
If the GTT endpoint stops returning a completed order, monitoring is withheld and the
existing order reconciliation process must resolve it. Broker-native stops remain in
place if the monitor dies; this is not a replacement for exchange/broker protection.

## Operator installation (observation only)

Run in the active checkout as `anant`, after deploying the reviewed commit using the
normal backup/migration procedure. These commands do not stop the dashboard:

```bash
cd /home/anant/TradingHelper
uv sync --locked --all-groups
uv run igs db migrate
uv run igs gate run
uv run igs trade-monitor --once
install -m 644 scripts/systemd/igs-trade-monitor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now igs-trade-monitor.service
systemctl --user status igs-trade-monitor.service --no-pager
```

Use the standard trading OAuth token already configured in Settings; it is never
printed. The worker exits if the token is missing. A rate-limit or connection failure
submits no action and logs feed unavailability. Systemd restarts a failed process after
30 seconds. Refreshing credentials is handled by restarting the worker.

Stopping the new observation service is reversible:

```bash
systemctl --user disable --now igs-trade-monitor.service
```

Do not stop the existing approvals/reconciliation worker. The new unit is deliberately
not added to automatic deployment startup lists. `loginctl` user persistence and the
existing app's user service setup still need to be functional on the server.

Live stop modification, if independently selected by the owner, is the explicit
`igs trade-monitor --execute` mode, not the default unit. Do not run competing monitors
with different modes. No live execution mode was started as part of this implementation.

## Evidence before promotion

Keep v1 frozen while collecting observations. Compare the original exit rule against
the proposed rule on the same entries, separately for longs and shorts and by decision
day. Report net P&L after charges, conservative spread/slippage, worst drawdown, profit
giveback, missed winners and unresolved trades. A trigger is not a simulated fill:
use the next actionable quote with available depth, and flag gaps/unknown outcomes.
Use an untouched forward period and uncertainty over days, accounting for overlapping
exposures and serial dependence. Do not select thresholds on the period used to claim
an improvement. If net improvement is not supported, retain the original exit policy.

The new event table records observations and modification attempts, not an already
completed profitability study. Live modification tests use a fake broker/HTTP transport.
Real Upstox API acceptance, live feed latency, special trading sessions and reconnect
behaviour still require an owner-run observation trial. Weekends are excluded; on
exchange holidays no position/quote should be actionable, and freshness checks still
apply. Special weekend trading sessions are not supported by this version.

## Official API contracts checked 24 October 2026

- [Positions](https://upstox.com/developer/api-documentation/get-positions/)
- [Full quotes and timestamp/depth fields](https://upstox.com/developer/api-documentation/get-full-market-quote/)
- [GTT identity, rules and statuses](https://upstox.com/developer/api-documentation/get-gtt-order-details/)
- [Modify an existing GTT](https://upstox.com/developer/api-documentation/modify-gtt-order/)

These establish API capabilities, not investment effectiveness. The documented modify
API retains an ENTRY rule and quantity; the monitor preserves both rather than creating
another exit. Unsupported states produce a blocked event.
