-- Paper record of intraday calls (igs.intraday.outcomes): what each buy/sell call would
-- have done with its limit entry, stop and target, replayed on that session's five-minute
-- candles the next day. A measurement after the fact; no order and no later call uses it.
create table intraday_call_outcome (
    company_id     bigint      not null references company,
    candle_end     timestamptz not null,
    trading_day    date        not null,
    symbol         text        not null,
    action         text        not null check (action in ('buy', 'sell')),
    rvol           numeric,
    close_location numeric,             -- where the volume candle closed in its range,
                                        -- toward the trade (1 = at its high for a buy)
    reference      numeric     not null,
    stop           numeric     not null,
    target         numeric     not null,
    outcome        text        not null check (outcome in
        ('target', 'stop', 'time_exit', 'not_filled')),
    filled_at      timestamptz,
    exit_at        timestamptz,
    exit_price     numeric,
    r_multiple     numeric,             -- result in units of the stop distance, before charges
    evaluated_at   timestamptz not null default now(),
    primary key (company_id, candle_end)
);
create index intraday_call_outcome_day_idx on intraday_call_outcome (trading_day);
