-- Machine-learning intraday paper calls (igs.intraday.ml). Nothing here places an order.

-- Each stock's full sessions, summarised from Upstox five-minute candles: what the model's
-- features need from the previous 22 sessions. Kept for about 75 days.
create table ml_intraday_session (
    instrument_key  text    not null,
    session_date    date    not null,
    open            numeric not null,
    high            numeric not null,
    low             numeric not null,
    close           numeric not null,
    value           numeric not null,     -- sum of close x volume over the session's candles
    last_hour       numeric,              -- close over the 14:30 candle's open, less one
    v30             numeric not null,     -- volume of the first six candles (09:15 to 09:40)
    primary key (instrument_key, session_date)
);

-- One row a trading day once the model has run (or why it made no calls).
create table ml_intraday_day (
    session_date  date primary key,
    decided_at    timestamptz not null,
    model         text not null,
    universe      integer not null,       -- Nifty 200 members considered
    scored        integer not null,       -- of them, liquid with complete inputs
    note          text not null default ''
);

-- The day's paper calls: up to three buys and three short sells.
create table ml_intraday_pick (
    session_date  date    not null references ml_intraday_day,
    instrument_key text   not null,
    company_id    bigint references company,
    symbol        text    not null,
    side          smallint not null check (side in (1, -1)),
    prediction    numeric not null,        -- predicted return to 15:15
    rank          integer not null,
    features      jsonb   not null,
    entry_after   timestamptz not null,    -- entry at the first candle opening after this
    entry         numeric,
    exit          numeric,
    quantity      integer,
    gross_inr     numeric,
    charges_inr   numeric,
    net_inr       numeric,
    net_pct       numeric,
    status        text not null check (status in ('open', 'closed', 'skipped', 'no data')),
    primary key (session_date, instrument_key)
);
