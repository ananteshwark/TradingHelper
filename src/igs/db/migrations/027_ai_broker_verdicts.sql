-- The AI's verdict on each broker's call it was shown with a stock (prompt call-v4):
-- agree, partly agree, disagree or cannot judge, with the reason in a sentence. Stored
-- with the AI call that gave it and never changed; the latest verdict on a broker's call
-- is the one shown.
create table ai_broker_verdict (
    call_id        bigint not null references ai_call,
    broker_call_id bigint not null references broker_call on delete cascade,
    verdict        text   not null check (verdict in ('agree', 'partly agree', 'disagree',
                                                      'cannot judge')),
    reason         text   not null,
    primary key (call_id, broker_call_id)
);
create index ai_broker_verdict_broker_idx on ai_broker_verdict (broker_call_id);
