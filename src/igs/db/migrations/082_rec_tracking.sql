-- Every recommendation the app makes, the market at the moment it was made, and its price
-- and volume path afterwards (igs.tracking, docs/TRACKING.md). Records: never removed by
-- the cleanup. Nothing here places an order.

create table rec_track (
    rec_id          bigint generated always as identity primary key,
    source          text not null check (source in ('ml_0945', 'evening', 'spike', 'scanner',
                                                    'ai_call', 'momentum')),
    ref             text not null,                 -- the source's own key
    instrument_key  text not null,
    company_id      bigint references company,
    symbol          text not null,
    side            smallint not null check (side in (1, -1)),   -- 1 buy, -1 short sell
    horizon         text not null check (horizon in ('intraday', 'swing')),
    recommended_at  timestamptz not null,          -- when the source made it
    source_price    numeric,                       -- the price the source itself recorded
    stop            numeric,
    track_from      timestamptz not null,          -- the path starts here
    track_until     timestamptz not null,
    -- The market when the tracker registered it (within a minute in market hours).
    captured_at     timestamptz,
    rec_price       numeric,                       -- last traded price
    rec_volume      bigint,                        -- the day's volume so far
    rec_vwap        numeric,                       -- the day's average traded price so far
    rec_high        numeric,
    rec_low         numeric,
    rec_prev_close  numeric,
    rec_rvol        double precision,              -- the day's volume against its usual by then
    rec_nifty       numeric,
    capture         text not null check (capture in ('live', 'close', 'failed')),
    capture_note    text not null default '',
    volume_curve    jsonb,                         -- mean cumulative volume a 5-minute slot,
                                                   -- previous 20 sessions (intraday only)
    -- The path so far, from rec_price, in % signed for the side (+ is in its favour).
    last_price      numeric,
    last_at         timestamptz,
    mae_pct         double precision,
    mae_at          timestamptz,
    rvol_at_mae     double precision,
    mfe_pct         double precision,
    mfe_at          timestamptz,
    ticks           integer not null default 0,
    status          text not null default 'tracking' check (status in ('tracking', 'done')),
    shadow          jsonb,                         -- the adverse-move models' predictions
    unique (source, ref)
);
create index rec_track_open on rec_track (status, track_from);
create index rec_track_recommended on rec_track (recommended_at);

create table rec_tick (
    rec_id    bigint not null references rec_track on delete cascade,
    at        timestamptz not null,
    price     numeric not null,
    volume    bigint,
    vwap      numeric,
    day_high  numeric,
    day_low   numeric,
    rvol      double precision,
    nifty     numeric,
    primary key (rec_id, at)
);
