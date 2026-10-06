-- Per-company discovery checkpoints; raw exchange listings remain replayable.
create table ownership_backfill (
    company_id bigint primary key references company(company_id),
    checked_at timestamptz not null default now(),
    next_attempt_at timestamptz not null,
    last_error text
);
