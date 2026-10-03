-- Legacy verdicts remain unknown; new assessments supply their own confidence.
alter table ai_broker_verdict add column confidence double precision
    check (confidence >= 0 and confidence <= 1);

create or replace function queue_broker_agreement() returns trigger language plpgsql as $$
declare
    b broker_call%rowtype;
    rid bigint;
    sym text;
    aid bigint;
    latest bigint;
    msg text;
begin
    select * into b from broker_call where broker_call_id = new.broker_call_id for update;
    select verdict_id into latest from ai_broker_verdict
        where broker_call_id = b.broker_call_id order by given_at desc, verdict_id desc limit 1;
    if latest <> new.verdict_id then return new; end if;
    if new.verdict <> 'agree' then
        update alert_outbox o set status = 'cancelled', last_error = 'AI agreement withdrawn'
        from alert_log a where a.alert_id = o.alert_id
          and a.dedupe_key = 'broker_agreement:' || b.broker_call_id
          and o.status in ('pending', 'failed');
        return new;
    end if;
    if b.company_id is null or b.stance not in ('buy', 'sell') then return new; end if;
    -- Filling confidence for an unchanged legacy agreement is not a new call.
    if exists(select 1 from (
        select verdict,confidence from ai_broker_verdict
        where broker_call_id=b.broker_call_id
        order by given_at desc,verdict_id desc offset 1 limit 1
    ) previous where previous.verdict='agree' and previous.confidence is null) then
        return new;
    end if;
    if new.review_id is not null then
        select run_id, symbol into rid, sym from ai_broker_review where review_id=new.review_id;
    else
        select run_id, symbol into rid, sym from ai_call where call_id=new.call_id;
    end if;
    msg := upper(b.stance) || ' ' || sym || ' — AI agrees with ' || b.broker
        || E'\nSource: Broker | AI verdict: agree'
        || E'\nBroker date: ' || b.called_on || ' | Kind: ' || b.kind
        || case when b.target_price is null then '' else E'\nBroker target: Rs ' || b.target_price end
        || E'\nAssessment confidence: ' || case when new.confidence is null then 'Not assessed'
           else round(new.confidence*100)::text || '% (' ||
                case when new.confidence >= 0.75 then 'High'
                     when new.confidence >= 0.5 then 'Medium' else 'Low' end || ')' end
        || E'\n\nWhy: ' || left(new.reason, 1800)
        || E'\n\nAI judgement, not a guaranteed outcome. Details in the calls table.';
    insert into alert_log(kind,company_id,run_id,message,dedupe_key)
        values('broker_agreement',b.company_id,rid,msg,'broker_agreement:' || b.broker_call_id)
        on conflict(dedupe_key) do nothing returning alert_id into aid;
    if aid is not null then
        insert into alert_outbox(alert_id,channel) values(aid,'telegram_calls');
    end if;
    return new;
end $$;
