-- Large trading as it happens (igs.intraday.surges, docs/TRADE_SURGES.md): each liquid
-- stock's volume at the previous intraday scan, and every window in which it traded a
-- large share of a normal day's value, with what followed.
create table trade_surge_snapshot (
    trade_date     date    not null,
    instrument_key text    not null,
    taken_at       timestamptz not null,
    volume         bigint  not null,         -- traded today, as Upstox's quote says
    last_price     numeric not null,
    primary key (trade_date, instrument_key)
);

create table trade_surge (
    surge_id       bigint generated always as identity primary key,
    trade_date     date    not null,
    instrument_key text    not null,
    isin           text    not null,
    symbol         text    not null,
    company_id     bigint references company,
    window_from    timestamptz not null,     -- the previous scan
    window_to      timestamptz not null,     -- this scan
    volume         bigint  not null,         -- traded in the window
    value_inr      numeric not null,
    day_value_inr  numeric not null,         -- the median day of the previous 20 sessions
    share_of_day   numeric not null,         -- value_inr / day_value_inr
    price_from     numeric not null,
    price_to       numeric not null,
    move_pct       numeric not null,
    average_price  numeric,                  -- the day's average traded price so far
    bid_qty        numeric,                  -- the order book at the scan
    offer_qty      numeric,
    alert_status   text check (alert_status in ('sent', 'late', 'repeat')),
    alerted_at     timestamptz,
    alert_text     text,
    close_price    numeric,                  -- the day's close, once loaded
    to_close_pct   numeric,                  -- price_to to the close
    next_vs_index_pct numeric,               -- the next session against the Nifty 500
    unique (trade_date, instrument_key, window_to)
);
create index trade_surge_day on trade_surge (trade_date, alert_status);
