-- Operational download progress only. Screener exports remain tier-3 enrichment.
create table screener_download (
    company_id bigint primary key references company,
    status text not null check (status in ('downloaded', 'partial', 'failed')),
    attempts integer not null default 0,
    last_attempt_at timestamptz not null default now(),
    next_attempt_at timestamptz not null default now(),
    export_quarters integer not null default 0,
    source_fetch_id text references raw_payload(fetch_id),
    last_error text
);
create table screener_download_control (
    singleton boolean primary key default true check (singleton),
    paused_reason text,
    retry_after timestamptz
);
insert into screener_download_control(singleton) values (true);
