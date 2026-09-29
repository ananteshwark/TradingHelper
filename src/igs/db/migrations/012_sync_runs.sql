-- Every check of the exchanges for new files (`igs sync`, the app's own checks and the
-- ingest part of `igs daily`): when it ran, what started it, what each step fetched.
-- The UI shows the latest; the minimum interval between checks is measured from here.
create table sync_run (
    sync_id      bigserial   primary key,
    trigger      text        not null check (trigger in ('manual', 'startup', 'interval',
                                                         'timer', 'daily')),
    started_at   timestamptz not null,
    finished_at  timestamptz,
    status       text        not null default 'running'
                             check (status in ('running', 'ok', 'partial', 'failed')),
    new_rows     integer     not null default 0,
    steps        jsonb       not null default '[]',
    note         text        not null default ''
);
create index sync_run_started_idx on sync_run (started_at desc);
