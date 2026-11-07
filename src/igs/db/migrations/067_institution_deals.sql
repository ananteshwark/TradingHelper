-- Prominent institutions' trades in NSE's bulk and block deal files (igs.institutions,
-- config/institutions.yaml), each sent once on Telegram. One row is one institution's
-- net trade in one stock on one day in one file: its client names added up, buys minus
-- sells, at the volume-weighted price of the net side.
create table institution_deal (
    deal_id         bigint generated always as identity primary key,
    kind            text not null check (kind in ('bulk', 'block')),
    trade_date      date not null,
    symbol          text not null,
    security_name   text,
    company_id      bigint references company,
    institution     text not null,
    investor_type   text not null check (investor_type in ('FII', 'DII')),
    investor_group  text not null,
    client_names    text not null,
    side            text not null check (side in ('buy', 'sell')),
    quantity        numeric not null check (quantity > 0),
    price           numeric not null check (price > 0),
    value_inr       numeric not null check (value_inr > 0),
    shares_outstanding numeric,                 -- the latest shareholding filing's total
    pct_of_shares   numeric,
    adv_inr         numeric,                    -- average daily traded value, 20 sessions
    source_url      text not null,
    first_seen_at   timestamptz not null default clock_timestamp(),
    alert_text      text,
    alerted_at      timestamptz,
    unique (kind, trade_date, symbol, institution)
);
create index institution_deal_company on institution_deal (company_id, trade_date);
create index institution_deal_unsent on institution_deal (first_seen_at) where alerted_at is null;
create trigger operational_ingestion after insert on institution_deal
    referencing new table as inserted_rows for each statement
    execute function notify_ingested_rows();
