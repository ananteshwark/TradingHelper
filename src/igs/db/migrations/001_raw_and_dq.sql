-- Index of the raw landing zone plus the data-quality issue log.
-- raw_payload is rebuilt from the fetch records on disk (igs raw reindex).

create table raw_payload (
    fetch_id          text primary key,
    source_id         text        not null,
    url               text,
    fetched_at        timestamptz not null,
    http_status       integer,
    content_sha256    char(64)    not null,
    size_bytes        bigint      not null,
    content_type      text,
    blob_path         text        not null,
    origin            text        not null check (origin in ('http', 'manual')),
    request_params    jsonb       not null default '{}',
    response_headers  jsonb       not null default '{}',
    note              text        not null default '',
    indexed_at        timestamptz not null default now()
);
create index raw_payload_source_idx on raw_payload (source_id, fetched_at);

create table dq_issue (
    dq_id        bigserial primary key,
    detected_at  timestamptz not null,
    severity     text        not null check (severity in ('info', 'warn', 'error')),
    category     text        not null,
    message      text        not null,
    source_id    text,
    fetch_id     text,
    security_id  bigint,
    as_of_date   date,
    details      jsonb       not null default '{}'
);
create index dq_issue_category_idx on dq_issue (category, detected_at);

-- Shared guard for append-only tables. Row-level UPDATE and DELETE are refused;
-- a full rebuild uses TRUNCATE and reloads from raw.
create function forbid_row_mutation() returns trigger
language plpgsql as $$
begin
    raise exception '% is append-only: % refused (rebuild with TRUNCATE and reload from raw)',
        tg_table_name, tg_op;
end
$$;

create trigger raw_payload_append_only
    before update or delete on raw_payload
    for each row execute function forbid_row_mutation();
