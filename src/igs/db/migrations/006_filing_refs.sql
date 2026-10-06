-- Filing references parsed from exchange listings (results, integrated filing,
-- shareholding). Derived from the landed listing payloads; the XBRL documents
-- they point to are fetched separately and carry these fields in their fetch
-- record so that a rebuild never needs this table.

create table filing_ref (
    exchange          text        not null check (exchange in ('NSE', 'BSE')),
    filing_system     text        not null,
    filing_type       text        not null check (filing_type in
                                      ('financial_results', 'shareholding')),
    symbol            text        not null,
    company_name      text,
    period_end        date        not null,
    basis_hint        text,
    filed_at          timestamptz not null,
    filed_at_precise  boolean     not null,
    document_url      text        not null,
    exchange_ref      text,
    source_fetch_id   text        not null references raw_payload (fetch_id),
    primary key (exchange, filing_system, document_url)
);
create index filing_ref_symbol_idx on filing_ref (symbol, period_end);

alter table filing add column results_format text;
alter table filing add column audit_opinion text;
