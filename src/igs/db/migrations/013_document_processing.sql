-- Download success and processing success are independent. Raw payloads remain immutable.
create table document_processing (
    fetch_id text primary key references raw_payload(fetch_id),
    status text not null check (status in ('loaded', 'failed')),
    parser_version text not null,
    attempts integer not null default 1,
    rows_loaded integer not null default 0,
    last_error text,
    processed_at timestamptz not null default now()
);
create index raw_payload_document_url_idx on raw_payload(url)
    where source_id = 'nse_xbrl_document' and http_status = 200;
