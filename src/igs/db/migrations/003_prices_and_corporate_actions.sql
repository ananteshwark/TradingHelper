-- End-of-day prices exactly as the exchange reported them (unadjusted), one row
-- per exchange/date/ISIN/series after session filtering. Adjusted prices are
-- never stored: they are derived from these rows plus corporate_action, using
-- only actions with ex_date <= as_of.

create table price_eod (
    exchange         text          not null check (exchange in ('NSE', 'BSE')),
    trade_date       date          not null,
    isin             text          not null,
    symbol           text          not null,
    series           text          not null,
    open             numeric(18, 4),
    high             numeric(18, 4),
    low              numeric(18, 4),
    close            numeric(18, 4) not null,
    last             numeric(18, 4),
    prev_close       numeric(18, 4),
    volume           bigint,
    turnover_inr     numeric(24, 2),
    trades           bigint,
    delivery_qty     bigint,
    delivery_pct     numeric(7, 4),
    session_id       text,
    source_fetch_id  text          not null references raw_payload (fetch_id),
    primary key (exchange, trade_date, isin, series)
);
create index price_eod_isin_idx on price_eod (isin, trade_date);

-- Corporate actions. Column meaning by action_type:
--   split / consolidation : fv_old -> fv_new
--   bonus                 : ratio_a new shares for every ratio_b held
--   rights                : ratio_a new shares for every ratio_b held at issue_price
--   dividend              : cash_per_share
--   demerger / other      : described in subject; adjustment needs manual review
create table corporate_action (
    ca_id            bigserial primary key,
    exchange         text        not null check (exchange in ('NSE', 'BSE')),
    symbol           text        not null,
    isin             text,
    security_id      bigint references security,
    action_type      text        not null check (action_type in (
                         'split', 'consolidation', 'bonus', 'rights',
                         'dividend', 'demerger', 'other')),
    ex_date          date        not null,
    record_date      date,
    announced_at     timestamptz,
    fv_old           numeric,
    fv_new           numeric,
    ratio_a          numeric,
    ratio_b          numeric,
    issue_price      numeric,
    cash_per_share   numeric,
    subject          text        not null,
    source_fetch_id  text        not null references raw_payload (fetch_id),
    ingested_at      timestamptz not null default now()
);
create index corporate_action_symbol_idx on corporate_action (symbol, ex_date);
