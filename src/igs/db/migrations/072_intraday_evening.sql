-- Evening intraday paper calls (igs.intraday.evening, research/evening). Nothing here places
-- an order.

-- One row per evening the model has run (or why it made no calls). signal_date is the
-- session whose close the decision used; the calls trade the next session.
create table evening_day (
    signal_date   date primary key,
    decided_at    timestamptz not null,
    model         text not null,
    universe      integer not null,       -- Nifty 200 members considered
    scored        integer not null,       -- of them, liquid with complete inputs
    note          text not null default ''
);

-- The evening's paper calls: up to three buys and three short sells for the next session,
-- entered at its 09:15 open and closed at its 15:15 open.
create table evening_pick (
    signal_date    date    not null references evening_day,
    instrument_key text    not null,
    company_id     bigint references company,
    symbol         text    not null,
    side           smallint not null check (side in (1, -1)),
    prediction     numeric not null,       -- predicted return, 09:15 open to 15:15 open
    rank           integer not null,
    features       jsonb   not null,
    trade_date     date,                   -- the session it traded, once known
    entry          numeric,
    exit           numeric,
    quantity       integer,
    gross_inr      numeric,
    charges_inr    numeric,
    net_inr        numeric,
    net_pct        numeric,
    status         text not null check (status in ('open', 'closed', 'skipped', 'no data')),
    primary key (signal_date, instrument_key)
);
