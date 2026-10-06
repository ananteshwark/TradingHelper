-- Live-screen snapshots are kept separately from fundamental ratings.
create table intraday_history (
    instrument_key text not null,
    session_date date not null,
    fetched_at timestamptz not null default clock_timestamp(),
    candles jsonb not null,
    primary key(instrument_key, session_date)
);
create table intraday_scan (
    scan_id bigint generated always as identity primary key,
    started_at timestamptz not null default clock_timestamp(),
    finished_at timestamptz,
    status text not null check(status in ('running','complete','failed','unconfigured','closed')),
    message text not null default '',
    scanned integer not null default 0
);
create table intraday_signal (
    scan_id bigint not null references intraday_scan,
    company_id bigint not null references company,
    symbol text not null,
    instrument_key text not null,
    observed_at timestamptz not null,
    result jsonb not null,
    primary key(scan_id,company_id)
);
-- Named investor purchases/sales from public disclosures; publication and receipt
-- are distinct from trade time. An administrator can import verified bulk/block deals.
create table intraday_investor_event (
    event_id bigint generated always as identity primary key,
    company_id bigint not null references company,
    investor text not null,
    category text not null check(category in ('prominent','FII','DII','large')),
    side text not null check(side in ('buy','sell')),
    trade_date date not null,
    published_at timestamptz not null,
    received_at timestamptz not null default clock_timestamp(),
    source_url text not null,
    evidence text not null,
    unique(company_id,investor,side,trade_date,source_url)
);
create index intraday_investor_event_company on intraday_investor_event(company_id,published_at);
create table intraday_investor_watch (
    investor text primary key,
    category text not null check(category in ('prominent','FII','DII'))
);
create table intraday_deal_fetch (
    content_hash text primary key,
    fetched_at timestamptz not null default clock_timestamp(),
    source_url text not null,
    body text not null
);
create trigger operational_ingestion after insert on intraday_investor_event
    referencing new table as inserted_rows for each statement
    execute function notify_ingested_rows();
create trigger operational_ingestion after insert on intraday_history
    referencing new table as inserted_rows for each statement
    execute function notify_ingested_rows();
