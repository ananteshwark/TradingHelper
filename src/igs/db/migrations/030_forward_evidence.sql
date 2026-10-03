-- Auditable, unscored extraction from exchange announcements and their attachments.
create table forward_document (
    ann_id bigint primary key references announcement,
    company_id bigint not null references company,
    published_at timestamptz not null,
    received_at timestamptz not null default clock_timestamp(),
    source_url text,
    payload bytea,
    text_content text,
    content_sha256 text,
    assessed_at timestamptz,
    model text,
    claims jsonb not null default '[]',
    attempts integer not null default 0,
    retry_after timestamptz,
    last_error text
);
create index forward_document_company_idx on forward_document(company_id,published_at);
