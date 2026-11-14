-- Real orders for the ML intraday calls (igs.intraday.trading.approve_ml), approved on the
-- ML intraday page or by a reply to the call's own Telegram message, and a switch for the
-- rule-based intraday calls.
alter table intraday_trading_settings
    -- Off until the owner turns it on; live trading (enabled) must be on as well.
    add column ml_orders_enabled boolean not null default false,
    -- The rule-based calls lost after charges in every test (docs/INTRADAY_FILTER.md). Off,
    -- at the owner's request: no alerts, approvals or automatic orders. Their scans and
    -- paper record go on.
    add column rule_calls_enabled boolean not null default false;

-- An ML trade has no scan and no target: it is closed by its stop or by the app at 15:00.
alter table intraday_trade
    add column kind text not null default 'rule' check (kind in ('rule', 'ml')),
    alter column scan_id drop not null,
    alter column target_price drop not null,
    add column exit_order_id text,      -- the app's own square-off order
    add constraint intraday_trade_rule_scan check (kind = 'ml' or scan_id is not null),
    add constraint intraday_trade_rule_target check (kind = 'ml' or target_price is not null);
alter table intraday_trade drop constraint intraday_trade_status_check;
alter table intraday_trade add constraint intraday_trade_status_check check (status in
    ('submitting', 'submitted', 'uncertain', 'rejected', 'expired', 'entry_open',
     'entry_filled', 'exit_unprotected', 'closed', 'cancel_uncertain',
     'squaring_off', 'square_off_failed'));

alter table ml_intraday_pick
    -- The 09:40 candle's close: the last price the model saw. Approval is refused once the
    -- live price is further from it than the trading settings allow.
    add column decision_price numeric,
    -- The call's own Telegram message, to be approved by a reply to it.
    add column telegram_message_id bigint unique,
    add column telegram_attempts integer not null default 0;
