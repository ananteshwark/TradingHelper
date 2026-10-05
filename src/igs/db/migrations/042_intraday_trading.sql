alter table intraday_telegram add column telegram_message_id bigint;

create table intraday_trading_settings (
    singleton boolean primary key default true check (singleton),
    enabled boolean not null default false,
    max_trade_rupees numeric(12,2) not null default 10000 check (max_trade_rupees between 1 and 10000),
    max_daily_rupees numeric(12,2) not null default 30000 check (max_daily_rupees between 1 and 30000),
    max_daily_trades integer not null default 3 check (max_daily_trades between 1 and 3),
    max_price_deviation_pct numeric(5,2) not null default 0.50
        check (max_price_deviation_pct between 0 and 2)
);
insert into intraday_trading_settings(singleton) values(true);

create table intraday_trade (
    trade_id bigint generated always as identity primary key,
    company_id bigint not null references company,
    trading_day date not null,
    action text not null check(action in ('buy','sell')),
    scan_id bigint not null references intraday_scan,
    symbol text not null,
    instrument_key text not null,
    approved_by text not null check(approved_by in ('telegram','admin')),
    approved_at timestamptz not null,
    expires_at timestamptz not null,
    quantity integer not null check(quantity>0),
    entry_price numeric(12,2) not null check(entry_price>0),
    stop_price numeric(12,2) not null check(stop_price>0),
    target_price numeric(12,2) not null check(target_price>0),
    notional numeric(12,2) not null check(notional>0),
    status text not null check(status in
        ('submitting','submitted','uncertain','rejected','expired','entry_open',
         'entry_filled','exit_unprotected','closed','cancel_uncertain')),
    gtt_order_id text,
    broker_state jsonb,
    last_checked_at timestamptz,
    last_error text,
    unique(company_id,trading_day)
);
create index intraday_trade_status_idx on intraday_trade(status,trading_day);

create table intraday_telegram_cursor (
    singleton boolean primary key default true check(singleton),
    next_update_id bigint not null default 0
);
insert into intraday_telegram_cursor(singleton) values(true);
