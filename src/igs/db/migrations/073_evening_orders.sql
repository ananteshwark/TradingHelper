-- Real orders for the evening intraday calls (igs.intraday.trading.approve_evening and
-- place_evening). Approved in the evening or before 09:15 by a reply to the call's own
-- Telegram message or on the Evening picks tab; placed after the next session opens.

alter table intraday_trading_settings
    -- Off until the owner turns it on; live trading (enabled) must be on as well.
    add column evening_orders_enabled boolean not null default false;

-- An evening trade, like an ML trade, has no scan and no target: its stop or the app's
-- 15:00 square-off closes it.
alter table intraday_trade drop constraint intraday_trade_kind_check;
alter table intraday_trade add constraint intraday_trade_kind_check
    check (kind in ('rule', 'ml', 'evening'));
alter table intraday_trade drop constraint intraday_trade_rule_scan;
alter table intraday_trade add constraint intraday_trade_rule_scan
    check (kind <> 'rule' or scan_id is not null);
alter table intraday_trade drop constraint intraday_trade_rule_target;
alter table intraday_trade add constraint intraday_trade_rule_target
    check (kind <> 'rule' or target_price is not null);

alter table evening_pick
    -- The signal day's close: the last price the model saw. At the open, an order is
    -- refused when the live price is further from it than the trading settings allow.
    add column decision_price numeric,
    -- The call's own Telegram message, to be approved by a reply to it.
    add column telegram_message_id bigint unique,
    add column telegram_attempts integer not null default 0,
    -- The owner's approval, recorded before the open; the order is placed after it.
    add column approved_at timestamptz,
    add column approved_by text check (approved_by in ('admin', 'telegram')),
    add column approval_update_id bigint unique,
    -- The placement attempt: its trade, or why none was placed.
    add column order_attempted_at timestamptz,
    add column trade_id bigint references intraday_trade,
    add column order_note text;
