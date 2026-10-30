-- Your own trades on Upstox, the ones you place by hand (igs.mytrades, docs/MY_TRADES.md).

-- Every fill Upstox reports on the account: today's trades (with their time, product and
-- order) on each intraday check, older ones from its trade history (by date only). Never
-- changed. The app's own orders are told apart when the record is built, not here.
create table upstox_fill (
    fill_id        bigint generated always as identity primary key,
    exchange       text not null,
    segment        text not null,            -- EQ, FO, COM, CD
    trade_date     date not null,
    trade_id       text not null,            -- the exchange's
    traded_at      timestamptz,              -- null from the trade history
    instrument_key text,                     -- NSE_EQ|INE..., NSE_FO|...; null for older F&O
    symbol         text not null,
    isin           text,
    expiry         date,
    strike         numeric,
    option_type    text check (option_type in ('CE', 'PE')),
    product        text,                     -- I, D, CO, MTF; null from the trade history
    side           text not null check (side in ('buy', 'sell')),
    quantity       integer not null check (quantity > 0),
    price          numeric not null check (price >= 0),
    order_id       text,
    order_type     text,
    source         text not null check (source in ('day', 'history')),
    received_at    timestamptz not null default clock_timestamp(),
    unique (exchange, trade_date, trade_id)
);
create index upstox_fill_day_idx on upstox_fill (trade_date);
create trigger upstox_fill_append_only before update or delete on upstox_fill
    for each row execute function forbid_row_mutation();

-- Each import: what Upstox was asked for and what came of it.
create table my_trades_import (
    import_id   bigint generated always as identity primary key,
    started_at  timestamptz not null default clock_timestamp(),
    kind        text not null check (kind in ('day', 'history')),
    start_date  date not null,
    end_date    date not null,
    fills_seen  integer not null,
    fills_added integer not null,
    error       text
);

-- The market around each of your bets: computed once, from the 5-minute candles of its
-- day and what the app had said about the stock (key: the bet's opening fill).
create table my_bet_context (
    bet_key     text primary key,
    computed_at timestamptz not null default clock_timestamp(),
    context     jsonb not null
);

-- Telegram notes already sent: a bet opened, closed, and each day's summary.
create table my_trade_note (
    note_key text primary key,
    sent_at  timestamptz not null default clock_timestamp()
);
