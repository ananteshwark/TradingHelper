-- The spike watcher and the spike-reversal paper track (igs.intraday.spikes, docs/SPIKES.md,
-- research/spike_reversal/PAPER.md). Nothing here places an order.

-- Each session the watcher ran: the paper record's daily P&L counts every one of them.
create table intraday_spike_day (
    trade_date  date primary key,
    first_scan  timestamptz not null,
    last_scan   timestamptz not null,
    stocks      integer not null            -- liquid stocks quoted at the last scan
);

-- One row a stock a day, once its range makes a spike possible, rebuilt from its closed
-- 5-minute candles at each scan. Bars are the candles' start times.
create table intraday_spike (
    trade_date      date not null,
    instrument_key  text not null,
    isin            text not null,
    symbol          text not null,
    paper           boolean not null,       -- in the paper track's stocks (Rs 2 crore a day)
    day_high        numeric(18, 4),
    day_low         numeric(18, 4),
    checked_at      timestamptz,
    done            boolean not null default false,
    -- The alert: the first move of the alert threshold within 30 minutes, at any time.
    alert_side      smallint check (alert_side in (-1, 1)),   -- -1 a rise, 1 a fall
    alert_bar       timestamptz,
    alert_move_pct  numeric(9, 3),
    alert_from      numeric(18, 4),
    alert_to        numeric(18, 4),
    alert_status    text check (alert_status in ('sent', 'late')),
    alert_sent_at   timestamptz,
    -- The paper trade: -1 a short sale after a rise, 1 a buy after a fall.
    status          text not null default 'watching' check (status in
                        ('watching', 'waiting', 'open', 'closed', 'no flat', 'none',
                         'not tracked')),
    side            smallint check (side in (-1, 1)),
    trigger_bar     timestamptz,
    move_pct        numeric(9, 3),
    flat_bar        timestamptz,
    entry_bar       timestamptz,
    entry           numeric(18, 4),
    target          numeric(18, 4),
    band_status     text check (band_status in ('no band', 'fixed', 'unknown')),
    band_pct        smallint,
    band_limit      numeric(18, 4),
    band_room_pct   numeric(9, 3),
    band_takes      boolean,                -- the band rule (B) keeps the trade
    exit_bar        timestamptz,
    exit            numeric(18, 4),
    exit_reason     text check (exit_reason in ('target', 'time')),
    locked          boolean,                -- the exit candle had no price range
    quantity        integer,
    gross_pct       numeric(9, 4),
    charges_inr     numeric(12, 2),
    net_inr         numeric(12, 2),
    net_pct         numeric(9, 4),
    entry_sent_at   timestamptz,
    result_sent_at  timestamptz,
    primary key (trade_date, instrument_key)
);
