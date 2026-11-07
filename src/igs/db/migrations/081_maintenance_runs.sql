-- Each cleanup of stale data (igs.maintenance): what it removed, and what it left for the
-- next run and why (a job writing its log at that moment, say).
create table maintenance_run (
    run_id      bigint generated always as identity primary key,
    started_at  timestamptz not null default clock_timestamp(),
    trigger     text not null check (trigger in ('scheduled', 'manual')),
    removed     jsonb not null,
    skipped     jsonb not null
);
