-- Score runs (append-only history of every ranking produced) and the app's own
-- state: watchlist, saved screens, alert log.

create table score_run (
    run_id                  bigserial primary key,
    as_of                   timestamptz not null,
    created_at              timestamptz not null default now(),
    gate_fingerprint        text        not null,
    ic_status_generated_at  text,
    dropped_factors         jsonb       not null default '[]',
    config                  jsonb       not null,
    dq_summary              jsonb       not null default '{}'
);

create table score_result (
    run_id          bigint  not null references score_run,
    company_id      bigint  not null references company,
    symbol          text,
    mcap_cr         numeric,
    bucket          text,
    industry        text,
    sector          text,
    composite       double precision,
    coverage        double precision,
    rank            integer,
    scored          integer,
    tier            text    not null,
    tier_reason     text,
    explanation     text    not null,
    primary key (run_id, company_id)
);
create index score_result_tier_idx on score_result (run_id, tier, rank);

create table score_pillar (
    run_id      bigint not null references score_run,
    company_id  bigint not null,
    pillar      text   not null,
    score       double precision,
    coverage    double precision,
    primary key (run_id, company_id, pillar)
);

create table score_factor (
    run_id             bigint not null references score_run,
    company_id         bigint not null,
    factor             text   not null,
    pillar             text   not null,
    status             text   not null,
    value              double precision,
    winsorized         double precision,
    z                  double precision,
    peer_percentile    double precision,
    peer_level         text,
    peer_group         text,
    peer_count         integer,
    contribution       double precision,
    detail             text,
    source_fact_ids    bigint[] not null default '{}',
    source_filing_ids  bigint[] not null default '{}',
    primary key (run_id, company_id, factor)
);

create table red_flag_result (
    run_id       bigint not null references score_run,
    company_id   bigint not null,
    flag         text   not null,
    status       text   not null,
    message      text   not null,
    evidence     text,
    source_ids   bigint[] not null default '{}',
    source_urls  text[]   not null default '{}',
    primary key (run_id, company_id, flag)
);

create trigger score_run_append_only before update or delete on score_run
    for each row execute function forbid_row_mutation();

create table watchlist (
    company_id  bigint      primary key references company,
    added_at    timestamptz not null default now(),
    note        text        not null default ''
);

create table saved_screen (
    name        text        primary key,
    filters     jsonb       not null,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create table alert_log (
    alert_id     bigserial primary key,
    created_at   timestamptz not null default now(),
    kind         text        not null,
    company_id   bigint,
    run_id       bigint,
    message      text        not null,
    delivered    jsonb       not null default '{}',
    dedupe_key   text        unique
);
