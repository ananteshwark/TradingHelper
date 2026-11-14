-- NSE's sector for each company in the Nifty Total Market list (the Nifty 500 and the
-- Nifty Microcap 250; the list's "Industry" column is the sector level of NSE's
-- classification). Peers for companies without the four-level classification, which comes
-- from a quote API some servers are refused. A row per observed change; valid_from is the
-- date the list was fetched.
create table index_sector (
    company_id       bigint not null references company,
    sector           text   not null,
    valid_from       date   not null,
    source_fetch_id  text references raw_payload (fetch_id),
    primary key (company_id, valid_from)
);
