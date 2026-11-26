-- Who counts as an FII or a DII beyond config/institutions.yaml: SEBI's registers of foreign
-- portfolio investors and of mutual funds, refreshed every week (igs.institution_register,
-- docs/INSTITUTIONAL_DEALS.md).
create table institution_register (
    source        text not null check (source in ('sebi_fpi', 'sebi_mf')),
    reg_no        text not null,
    name          text not null,
    norm          text not null,          -- the name as matched (institution_register.norm)
    type          text not null check (type in ('FII', 'DII')),
    grp           text not null,
    country       text,
    validity      text,
    first_seen_at timestamptz not null,
    last_seen_at  timestamptz not null,
    active        boolean not null,       -- listed at the latest complete refresh
    primary key (source, reg_no)
);
create index institution_register_norm on institution_register (norm) where active;

-- Each refresh of one register, and what it found.
create table institution_register_run (
    run_id      bigint generated always as identity primary key,
    source      text not null,
    started_at  timestamptz not null,
    finished_at timestamptz,
    status      text not null check (status in ('running', 'ok', 'failed')),
    total       integer,
    added       integer,
    removed     integer,
    note        text not null default ''
);

-- A deal whose client was found in SEBI's registers, not in config/institutions.yaml.
alter table institution_deal add column from_register boolean not null default false;

-- Takeover-code filings (SEBI SAST Regulation 29: a holding of 5% or more, or a change of
-- 2% or more) by an FII or a DII, from NSE's announcements; each sent once on Telegram.
-- Keyed by the announcement's own key, not its ann_id, which `igs rebuild` renumbers.
create table institution_filing (
    filing_id      bigint generated always as identity primary key,
    exchange       text not null,
    company_id     bigint references company,
    symbol         text not null,
    company_name   text,
    institution    text not null,
    investor_type  text not null check (investor_type in ('FII', 'DII')),
    investor_group text not null,
    side           text not null check (side in ('buy', 'sell', 'unknown')),
    subject        text not null,
    link           text,
    filed_at       timestamptz not null,
    first_seen_at  timestamptz not null,
    alerted_at     timestamptz,
    alert_text     text,
    unique (exchange, symbol, filed_at, subject)
);
create index institution_filing_unsent on institution_filing (first_seen_at)
    where alerted_at is null;
