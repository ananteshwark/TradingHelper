-- The AI's buy / hold / sell call on one stock, made from the data the app held at the time
-- and kept with that data. A call is never changed afterwards; its outcome is measured from
-- later prices, so the record shows how the calls actually did.
create table ai_call (
    call_id         bigint generated always as identity primary key,
    company_id      bigint      not null references company,
    symbol          text        not null,
    run_id          bigint      not null references score_run,
    action          text        not null check (action in ('buy', 'hold', 'sell')),
    confidence      double precision not null check (confidence between 0 and 1),
    horizon_months  integer     not null check (horizon_months between 1 and 36),
    summary         text        not null,
    reasons         jsonb       not null,
    risks           jsonb       not null,
    buy_when        jsonb       not null,
    sell_when       jsonb       not null,
    data_gaps       jsonb       not null,
    price_date      date,
    price_close     double precision,
    inputs          jsonb       not null,
    model           text        not null,
    prompt_version  text        not null,
    trigger         text        not null check (trigger in ('manual', 'scheduled')),
    cost_usd        double precision not null default 0,
    created_at      timestamptz not null default clock_timestamp()
);
create index ai_call_company_idx on ai_call (company_id, created_at desc);
create trigger ai_call_append_only
    before update or delete on ai_call
    for each row execute function forbid_row_mutation();

alter table llm_call drop constraint llm_call_feature_check;
alter table llm_call add constraint llm_call_feature_check
    check (feature in ('ask', 'brief', 'announcements', 'geopolitical', 'call'));
