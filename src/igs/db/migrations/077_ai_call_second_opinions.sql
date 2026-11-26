-- Second opinions on AI calls from another model (igs.assistant.second_opinion).

-- What a call was asked with besides its inputs: why it was made, the previous call and the
-- last buy, as the model saw them. Calls made before this have none; a second opinion on
-- them rebuilds it from the calls before.
alter table ai_call add column context jsonb;

alter table llm_call drop constraint llm_call_feature_check;
alter table llm_call add constraint llm_call_feature_check
    check (feature in ('ask', 'brief', 'announcements', 'geopolitical', 'call', 'brokers',
                       'news_tone', 'verdicts', 'forward', 'momentum', 'second_opinion'));

-- One row a second opinion: the other model's own call on the same context and data, then
-- its review of the call. Never changed.
create table ai_call_second_opinion (
    opinion_id         bigint generated always as identity primary key,
    call_id            bigint not null references ai_call,
    created_at         timestamptz not null default clock_timestamp(),
    provider           text not null,
    model              text not null,          -- as the provider reported it
    context_source     text not null check (context_source in ('stored', 'rebuilt')),
    instructions       text not null,          -- the call instructions' version used
    cut_sections       jsonb not null,         -- sections the call itself saw cut short
    -- Its own call.
    own_action         text not null check (own_action in ('buy', 'hold', 'sell')),
    own_confidence     double precision not null check (own_confidence between 0 and 1),
    own_horizon_months integer not null,
    own_stage          text not null,
    own_target_price   double precision,
    own_target_pct_a_year double precision,     -- from the call's entry
    own_call           jsonb not null,          -- everything it answered
    -- Its review of the call.
    verdict            text not null check (verdict in ('agree', 'partly agree', 'disagree')),
    review_summary     text not null,
    supported          jsonb not null,
    errors             jsonb not null,
    missed             jsonb not null,
    rule_issues        jsonb not null,
    review_confidence  double precision not null check (review_confidence between 0 and 1),
    cost_usd           double precision not null default 0
);
create index ai_call_second_opinion_call_idx on ai_call_second_opinion (call_id, created_at desc);
create trigger ai_call_second_opinion_append_only
    before update or delete on ai_call_second_opinion
    for each row execute function forbid_row_mutation();
