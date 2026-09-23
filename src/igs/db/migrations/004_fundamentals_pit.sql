-- Point-in-time fundamentals.
--
-- Every fact row carries period_end, filed_at (exchange dissemination time)
-- and ingested_at. Rows are append-only: a restatement arrives as a new row
-- from a later filing and never overwrites the original. Factor code reads
-- through facts_as_of(), which filters on filed_at, never on period_end.

create table filing (
    filing_id         bigserial primary key,
    company_id        bigint      references company,
    exchange          text        not null check (exchange in ('NSE', 'BSE')),
    filing_system     text        not null,
    filing_type       text        not null,
    exchange_ref      text,
    period_start      date,
    period_end        date,
    statement_basis   text        check (statement_basis in ('standalone', 'consolidated')),
    filed_at          timestamptz not null,
    ingested_at       timestamptz not null,
    taxonomy_version  text,
    source_url        text,
    content_sha256    char(64)    not null,
    source_fetch_id   text        not null references raw_payload (fetch_id),
    unique (exchange, filing_system, content_sha256)
);
create index filing_company_idx on filing (company_id, filed_at);

create table fundamental_fact (
    fact_id          bigserial primary key,
    filing_id        bigint      not null references filing,
    company_id       bigint      not null references company,
    statement_basis  text        not null check (statement_basis in ('standalone', 'consolidated')),
    period_start     date,
    period_end       date        not null,
    period_type      text        not null check (period_type in ('Q', 'H1', '9M', 'FY', 'INSTANT')),
    concept          text        not null,
    source_element   text        not null,
    value            numeric     not null,
    unit             text        not null,
    decimals         integer,
    filed_at         timestamptz not null,
    ingested_at      timestamptz not null,
    check (period_start is null or period_start <= period_end)
);
create index fundamental_fact_lookup_idx
    on fundamental_fact (company_id, concept, period_end, filed_at);

create trigger filing_append_only
    before update or delete on filing
    for each row execute function forbid_row_mutation();

create trigger fundamental_fact_append_only
    before update or delete on fundamental_fact
    for each row execute function forbid_row_mutation();

-- Version history. A new version starts only when a later filing reports a
-- different value for the same key; re-reporting the same number (e.g. as a
-- prior-period comparative) does not create one.
create view fundamental_fact_versioned as
with ordered as (
    select f.*,
           lag(f.value) over w as prev_value
    from fundamental_fact f
    window w as (partition by company_id, statement_basis, period_end, period_type, concept
                 order by filed_at, fact_id)
)
select ordered.*,
       sum(case when prev_value is null or prev_value <> value then 1 else 0 end)
           over (partition by company_id, statement_basis, period_end, period_type, concept
                 order by filed_at, fact_id) as version,
       (prev_value is not null and prev_value <> value) as is_restatement
from ordered;

-- The latest value of each fact that was public at p_as_of.
create function facts_as_of(p_as_of timestamptz)
returns setof fundamental_fact
language sql stable as $$
    select distinct on (company_id, statement_basis, period_end, period_type, concept) *
    from fundamental_fact
    where filed_at <= p_as_of
    order by company_id, statement_basis, period_end, period_type, concept,
             filed_at desc, fact_id desc
$$;
