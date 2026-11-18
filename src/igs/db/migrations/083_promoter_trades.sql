-- Promoters' and promoter groups' own buying and selling, across every listed company, as
-- soon as the app sees it (igs.promoter_trades, docs/PROMOTER_TRADES.md). Three sources:
-- - 'disclosure': their insider-trading disclosures (SEBI PIT), once loaded;
-- - 'bulk', 'block': NSE's bulk and block deal files, when the client is one of the
--   company's known promoters (the deal day, before any disclosure);
-- - 'sast': takeover-code (SEBI SAST Regulation 29) disclosures naming a known promoter.
create table promoter_trade (
    trade_id           bigint generated always as identity primary key,
    source             text not null check (source in ('disclosure', 'bulk', 'block', 'sast')),
    ref                text not null,         -- the source row (see igs.promoter_trades)
    company_id         bigint references company,
    symbol             text not null,
    company_name       text,
    person             text not null,
    category           text,                  -- as disclosed: Promoter, Promoter Group, ...
    side               text not null check (side in ('buy', 'sell', 'unknown')),
    quantity           numeric,
    price              numeric,
    value_inr          numeric,
    holding_before_pct numeric,
    holding_after_pct  numeric,
    mode               text,                  -- as disclosed, or 'bulk deal' / 'block deal'
    trade_date         date,
    filed_at           timestamptz,           -- when the exchange made it public
    detail             text not null default '',
    link               text,
    first_seen_at      timestamptz not null,
    alerted_at         timestamptz,
    alert_text         text,
    unique (source, ref)
);
create index promoter_trade_company on promoter_trade (company_id, trade_date);
create index promoter_trade_unsent on promoter_trade (first_seen_at) where alerted_at is null;

-- Which collected bulk and block deal files (intraday_deal_fetch) were checked for
-- promoters.
create table promoter_deal_scan (
    content_hash  text primary key,
    scanned_at    timestamptz not null,
    found         integer not null
);
