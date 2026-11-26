-- An NSE index's members on the days its constituent list was fetched; a snapshot is kept
-- only when the members change.
create table index_member (
    index_name       text   not null,
    as_of            date   not null,
    company_id       bigint not null references company,
    source_fetch_id  text references raw_payload (fetch_id),
    primary key (index_name, as_of, company_id)
);

-- Paper portfolios of the 12-1 month momentum rule (igs.momentum), tracked forward: track
-- 'rule' holds the ten best-ranked stocks, 'ai' the ten best-ranked the AI's review keeps.
-- Nothing here places an order.
create table momentum_rebalance (
    track        text not null check (track in ('rule', 'ai')),
    signal_date  date not null,          -- the month's last close the ranking used
    entry_date   date not null,          -- holdings change at this day's open
    universe     integer not null,       -- liquid Nifty 200 members ranked
    holdings     jsonb not null,         -- [{company_id, symbol, rank, score_pct}]
    reviews      jsonb,                  -- the AI's reviews ('ai'), or why there were none
    note         text not null default '',
    created_at   timestamptz not null default now(),
    primary key (track, signal_date)
);

-- Each holding period, open to open between rebalances; the current one is marked to the
-- latest close (complete = false) until the next rebalance closes it.
create table momentum_period (
    track                text    not null check (track in ('rule', 'ai')),
    start_date           date    not null,
    end_date             date    not null,
    complete             boolean not null,
    portfolio_gross_pct  numeric not null,
    portfolio_net_pct    numeric not null,
    basket_pct           numeric,
    nifty_pct            numeric,
    bought               integer not null,
    detail               jsonb   not null,   -- [{symbol, return_pct}]
    updated_at           timestamptz not null default now(),
    primary key (track, start_date)
);

-- The AI review of the picks is logged as the assistant feature 'momentum'.
alter table llm_call drop constraint llm_call_feature_check;
alter table llm_call add constraint llm_call_feature_check
    check (feature in ('ask', 'brief', 'announcements', 'geopolitical', 'call', 'brokers',
                       'news_tone', 'verdicts', 'forward', 'momentum'));
