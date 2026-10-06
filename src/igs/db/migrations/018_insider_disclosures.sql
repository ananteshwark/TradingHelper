-- NSE's insider-trading listing since its new disclosure system (the corporates-pit-gg API):
-- one row per disclosure with its broadcast time and the link to its XBRL. The trades are in
-- the XBRL, fetched separately; each fetch record carries these fields, so a rebuild never
-- needs this table.
create table insider_disclosure_ref (
    exchange          text        not null check (exchange in ('NSE', 'BSE')),
    disclosure_id     text        not null,
    symbol            text        not null,
    company_name      text,
    regulation        text,
    submission_type   text        not null check (submission_type in ('Original', 'Revision')),
    revision_remark   text,
    filed_at          timestamptz not null,
    document_url      text        not null,
    source_fetch_id   text        not null references raw_payload (fetch_id),
    primary key (exchange, document_url)
);
create index insider_disclosure_ref_symbol_idx on insider_disclosure_ref (symbol, filed_at);

-- Which disclosure a trade came from. A revision replaces the earlier rows for the same
-- person and trade date from the time it is broadcast; rows from the older API have
-- neither value.
alter table insider_trade add column disclosure_id text;
alter table insider_trade add column submission_type text
    check (submission_type in ('Original', 'Revision'));
