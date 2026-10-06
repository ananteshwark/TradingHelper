-- The AI's verdict on every broker's call (igs.assistant.verdicts). Calls on stocks the
-- buy / hold / sell calls don't cover get their verdicts from a review of the stock's data
-- instead, stored here with exactly what it was given and never changed.
create table ai_broker_review (
    review_id      bigint generated always as identity primary key,
    company_id     bigint      not null references company,
    symbol         text        not null,
    run_id         bigint      not null references score_run,
    in_run         boolean     not null,     -- false: the run's universe left the stock out
    created_at     timestamptz not null default clock_timestamp(),
    data_gaps      jsonb       not null,
    inputs         jsonb       not null,
    model          text        not null,
    prompt_version text        not null,
    trigger        text        not null check (trigger in ('manual', 'scheduled')),
    cost_usd       numeric     not null,
    reason         text        not null
);
create index ai_broker_review_company_idx on ai_broker_review (company_id, created_at desc);

-- A verdict now comes from an AI call or from a review, and carries the time it was given:
-- the latest one on a broker's call is the one shown.
alter table ai_broker_verdict drop constraint ai_broker_verdict_pkey;
alter table ai_broker_verdict
    add column verdict_id bigint generated always as identity primary key,
    add column review_id  bigint references ai_broker_review,
    add column given_at   timestamptz;
alter table ai_broker_verdict alter column call_id drop not null;
update ai_broker_verdict v set given_at = a.created_at from ai_call a where a.call_id = v.call_id;
alter table ai_broker_verdict alter column given_at set not null,
                              alter column given_at set default clock_timestamp();
alter table ai_broker_verdict add constraint ai_broker_verdict_one_source
    check (num_nonnulls(call_id, review_id) = 1);
create unique index ai_broker_verdict_call_uq on ai_broker_verdict (call_id, broker_call_id)
    where call_id is not null;
create unique index ai_broker_verdict_review_uq on ai_broker_verdict (review_id, broker_call_id)
    where review_id is not null;
drop index ai_broker_verdict_broker_idx;
create index ai_broker_verdict_broker_idx on ai_broker_verdict (broker_call_id, given_at desc);

alter table llm_call drop constraint llm_call_feature_check;
alter table llm_call add constraint llm_call_feature_check
    check (feature in ('ask', 'brief', 'announcements', 'geopolitical', 'call', 'brokers',
                       'news_tone', 'verdicts'));

-- Screener.in Excel exports are read section by section (PROFIT & LOSS, Quarters, BALANCE
-- SHEET, ...): "Sales" is a line of both the annual and the quarterly results.
alter table screener_enrichment add column section text;
create index screener_enrichment_fetch_idx on screener_enrichment (source_fetch_id);
