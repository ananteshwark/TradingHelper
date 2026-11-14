-- Spike picks: the spike-recovery model on paper (igs.intraday.spike_ml, docs/SPIKES.md,
-- research/spike_ml/PAPER.md). Nothing here places an order. Sessions are counted in
-- intraday_spike_day (075).

-- One row a stock a day, once its range today is 5% or more. Bars are candles' start times;
-- minutes are from midnight IST.
create table intraday_spike_ml (
    trade_date       date not null,
    instrument_key   text not null,
    isin             text not null,
    symbol           text not null,
    med20            numeric(20, 2) not null,     -- median traded value, previous 20 sessions
    prevclose        numeric(18, 4) not null,
    prev_ret         double precision,            -- the previous session's return
    day_high         numeric(18, 4),
    day_low          numeric(18, 4),
    last_price       numeric(18, 4),
    prev_high        numeric(18, 4),              -- the previous scan's quote
    prev_low         numeric(18, 4),
    prev_price       numeric(18, 4),
    checked_at       timestamptz,
    done             boolean not null default false,
    first5_minute    smallint,                    -- its 5% decision point, for the crowd
    first5_side      smallint check (first5_side in (-1, 1)),
    evaluated_until  smallint,                    -- the last decision point judged
    status           text not null default 'watching' check (status in
                         ('watching', 'picked', 'missed', 'closed', 'none', 'void')),
    -- The pick: side 1 is a rise (a short sale), -1 a fall (a buy).
    side             smallint check (side in (-1, 1)),
    x                numeric(5, 3),
    decision_bar     timestamptz,
    move_pct         numeric(9, 3),
    move_from        numeric(18, 4),
    move_to          numeric(18, 4),
    expected_pct     numeric(9, 3),               -- the best prediction, net % a trade
    target_pct       smallint check (target_pct between 1 and 5),
    preds            jsonb,                       -- each target's prediction
    features         jsonb,
    band_status      text check (band_status in ('no band', 'fixed', 'unknown')),
    band_pct         smallint,
    band_limit       numeric(18, 4),
    band_room_pct    numeric(9, 3),
    takes            boolean,                     -- in its record (a short not near the circuit)
    -- The trade.
    entry_bar        timestamptz,
    entry            numeric(18, 4),
    target_price     numeric(18, 4),
    exit_bar         timestamptz,
    exit             numeric(18, 4),
    exit_reason      text check (exit_reason in ('target', 'time')),
    locked           boolean,
    quantity         integer,
    gross_pct        numeric(9, 4),
    charges_inr      numeric(12, 2),
    net_inr          numeric(12, 2),
    net_pct          numeric(9, 4),
    alert_status     text check (alert_status in ('sent', 'late')),
    alert_sent_at    timestamptz,
    result_sent_at   timestamptz,
    primary key (trade_date, instrument_key)
);
