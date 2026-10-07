-- A verified export is knowable only after both receipt and basis verification.
-- Keep every version, without inventing an exchange filing/publication timestamp.
create table screener_export_context (
    context_id bigserial primary key,
    source_fetch_id text not null references raw_payload(fetch_id),
    company_id bigint not null references company,
    statement_basis text not null check (statement_basis in ('consolidated','standalone')),
    verified_at timestamptz not null default now(),
    unique(source_fetch_id, company_id)
);
