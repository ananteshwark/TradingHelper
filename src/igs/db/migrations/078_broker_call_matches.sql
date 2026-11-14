-- The AI matching brokers' calls whose stock wasn't recognised to a listed company
-- (igs.assistant.company_match).

alter table llm_call drop constraint llm_call_feature_check;
alter table llm_call add constraint llm_call_feature_check
    check (feature in ('ask', 'brief', 'announcements', 'geopolitical', 'call', 'brokers',
                       'news_tone', 'verdicts', 'forward', 'momentum', 'second_opinion',
                       'company_match'));

-- One row each time the AI reads an unmatched call. Never changed. A call it links is
-- recorded again under the company (a new broker_call row, linked_call_id), so the
-- unmatched row it read is gone; call_key, the unmatched call's dedupe_key, finds its
-- answers again, also after the owner undoes a link.
create table broker_call_match (
    match_id        bigint generated always as identity primary key,
    call_key        text not null,
    broker_call_id  bigint not null,         -- the unmatched call as it was read
    stock_name      text not null,           -- as the source wrote it
    created_at      timestamptz not null default clock_timestamp(),
    symbol          text not null,           -- as the AI gave it; '' for none
    ai_name         text not null,
    company_id      bigint references company,   -- the company it named, when one it was shown
    confidence      double precision not null check (confidence between 0 and 1),
    reason          text not null,
    candidates      jsonb not null,          -- the companies it was shown, last round
    rounds          integer not null check (rounds in (1, 2)),
    outcome         text not null check (outcome in ('linked', 'suggested', 'no match')),
    linked_call_id  bigint,                  -- the call recorded under the company
    model           text not null,
    prompt_version  text not null
);
create index broker_call_match_key_idx on broker_call_match (call_key, created_at desc);
create index broker_call_match_linked_idx on broker_call_match (linked_call_id)
    where linked_call_id is not null;
create trigger broker_call_match_append_only
    before update or delete on broker_call_match
    for each row execute function forbid_row_mutation();
