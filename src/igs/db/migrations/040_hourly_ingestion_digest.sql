-- Persist the cadence across restarts, starting from the last delivered summary.
create table ingestion_digest_schedule (
    singleton boolean primary key default true check(singleton),
    last_sent_at timestamptz not null,
    next_attempt_at timestamptz not null default now()
);
insert into ingestion_digest_schedule(singleton,last_sent_at)
    select true,coalesce(max(sent_at),now()) from operational_notification where kind='ingestion';
