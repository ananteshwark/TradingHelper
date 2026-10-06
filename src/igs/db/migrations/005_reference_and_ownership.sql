-- Reference data, ownership and event tables.

-- Stable natural keys so ids survive incremental master rebuilds.
--   company_key  = issuer code from the ISIN (characters 4-7)
--   security_key = first ISIN of the security's ISIN chain
alter table company add column company_key text unique;
alter table security add column security_key text unique;

create unique index corporate_action_natural_key
    on corporate_action (exchange, symbol, ex_date, action_type, subject);

create table trading_holiday (
    exchange         text not null check (exchange in ('NSE', 'BSE')),
    holiday_date     date not null,
    description      text not null,
    source_fetch_id  text not null references raw_payload (fetch_id),
    primary key (exchange, holiday_date)
);

create table index_price (
    index_name       text          not null,
    trade_date       date          not null,
    open             numeric(18, 4),
    high             numeric(18, 4),
    low              numeric(18, 4),
    close            numeric(18, 4) not null,
    is_total_return  boolean       not null,
    source_fetch_id  text          not null references raw_payload (fetch_id),
    primary key (index_name, trade_date)
);

-- NSE four-level classification. valid_from is the date it was first observed.
create table industry_classification (
    company_id       bigint not null references company,
    macro_sector     text,
    sector           text,
    industry         text,
    basic_industry   text,
    valid_from       date   not null,
    source_fetch_id  text references raw_payload (fetch_id),
    primary key (company_id, valid_from)
);

-- ASM/GSM lists are published as current snapshots; history is the sequence
-- of snapshots we have landed. effective_from is the snapshot's trade date.
create table surveillance_snapshot (
    measure          text not null check (measure in ('ASM', 'GSM')),
    list_name        text not null,
    symbol           text not null,
    isin             text,
    stage            text,
    effective_from   date not null,
    source_fetch_id  text not null references raw_payload (fetch_id),
    primary key (measure, list_name, symbol, effective_from)
);

-- Shareholding pattern rows, one per category per filing. Append-only.
create table shareholding (
    shp_id            bigserial primary key,
    filing_id         bigint      not null references filing,
    company_id        bigint      not null references company,
    period_end        date        not null,
    category          text        not null,
    shares            numeric,
    pct_of_total      numeric,
    pledged_shares    numeric,
    pledged_pct       numeric,
    holders           numeric,
    filed_at          timestamptz not null,
    ingested_at       timestamptz not null
);
create index shareholding_company_idx on shareholding (company_id, period_end, filed_at);
create trigger shareholding_append_only
    before update or delete on shareholding
    for each row execute function forbid_row_mutation();

-- Corporate announcements (resignations, audit matters, results intimations).
create table announcement (
    ann_id           bigserial primary key,
    exchange         text        not null check (exchange in ('NSE', 'BSE')),
    symbol           text        not null,
    company_id       bigint references company,
    filed_at         timestamptz not null,
    category         text        not null,
    subject          text        not null,
    body             text        not null default '',
    attachment_url   text,
    exchange_ref     text,
    source_fetch_id  text        not null references raw_payload (fetch_id),
    ingested_at      timestamptz not null,
    unique (exchange, symbol, filed_at, subject)
);
create index announcement_company_idx on announcement (company_id, filed_at);
create trigger announcement_append_only
    before update or delete on announcement
    for each row execute function forbid_row_mutation();

-- Exchange reference masters as landed (latest snapshot wins per key).
create table nse_equity_list (
    symbol           text not null,
    isin             text not null,
    company_name     text not null,
    series           text not null,
    listed_on        date,
    face_value       numeric,
    paid_up_value    numeric,
    market_lot       integer,
    snapshot_date    date not null,
    source_fetch_id  text not null references raw_payload (fetch_id),
    primary key (isin, snapshot_date)
);

create table bse_scrip (
    scrip_code       text not null,
    isin             text,
    scrip_id         text,
    name             text not null,
    status           text,
    scrip_group      text,
    face_value       numeric,
    industry         text,
    snapshot_date    date not null,
    source_fetch_id  text not null references raw_payload (fetch_id),
    primary key (scrip_code, snapshot_date)
);

create table broker_instrument (
    broker           text not null,
    exchange         text not null,
    token            text not null,
    symbol           text not null,
    name             text,
    instrument_type  text,
    snapshot_date    date not null,
    source_fetch_id  text not null references raw_payload (fetch_id),
    primary key (broker, exchange, token, snapshot_date)
);

-- Tier 3 enrichment. Kept apart from point-in-time fundamentals: Screener
-- exports carry no filing timestamps, so they never feed factor math.
create table screener_enrichment (
    source_fetch_id  text   not null references raw_payload (fetch_id),
    nse_code         text,
    bse_code         text,
    company_id       bigint references company,
    field            text   not null,
    period_label     text,
    value_text       text,
    value_num        numeric
);
create index screener_enrichment_company_idx on screener_enrichment (company_id);

-- Tier 3 price fallback (yfinance). Always flagged unverified in the UI and
-- never used where a Tier 1 price exists.
create table price_eod_fallback (
    provider         text          not null,
    symbol           text          not null,
    trade_date       date          not null,
    close            numeric(18, 4) not null,
    adj_close        numeric(18, 4),
    volume           bigint,
    source_fetch_id  text          not null references raw_payload (fetch_id),
    primary key (provider, symbol, trade_date)
);
