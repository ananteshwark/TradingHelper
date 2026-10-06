-- Insider-trading disclosures under the SEBI (Prohibition of Insider Trading) Regulations,
-- as broadcast by the exchange. filed_at is the broadcast time: the moment the trade became
-- public, and the only time the point-in-time view may use. Raw values are kept verbatim;
-- side, open_market and insider_role are the parser's normalised reading of them.
create table insider_trade (
    insider_trade_id   bigserial primary key,
    exchange           text        not null check (exchange in ('NSE', 'BSE')),
    symbol             text        not null,
    company_name       text,
    person_name        text        not null,
    person_category    text,
    insider_role       text        not null check (insider_role in ('promoter', 'director_kmp',
                                                                    'other')),
    regulation         text,
    security_type      text,
    transaction_type   text,
    acquisition_mode   text,
    side               text        not null check (side in ('buy', 'sell', 'other')),
    open_market        boolean     not null,
    quantity           numeric     not null,
    value_inr          numeric,
    holding_before_pct numeric,
    holding_after_pct  numeric,
    trade_from         date        not null,
    trade_to           date,
    intimated_on       date,
    filed_at           timestamptz not null,
    xbrl_url           text,
    source_fetch_id    text        not null references raw_payload (fetch_id),
    ingested_at        timestamptz not null,
    unique (exchange, symbol, person_name, filed_at, side, quantity, trade_from)
);
create index insider_trade_symbol_idx on insider_trade (symbol, filed_at);
create trigger insider_trade_append_only
    before update or delete on insider_trade
    for each row execute function forbid_row_mutation();
