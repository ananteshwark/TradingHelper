-- Queue confirmed broker calls atomically with the verdict, irrespective of its producer.
alter table alert_outbox drop constraint alert_outbox_status_check;
alter table alert_outbox add constraint alert_outbox_status_check
    check (status in ('pending', 'failed', 'sent', 'cancelled'));

create function queue_broker_agreement() returns trigger language plpgsql as $$
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
    if new.review_id is not null then
        select run_id, symbol into rid, sym from ai_broker_review where review_id=new.review_id;
    else
        select run_id, symbol into rid, sym from ai_call where call_id=new.call_id;
    end if;
    msg := upper(b.stance) || ' ' || sym || ' — AI agrees with ' || b.broker
        || E'\nSource: Broker | AI verdict: agree'
        || E'\nBroker date: ' || b.called_on || ' | Kind: ' || b.kind
        || case when b.target_price is null then '' else E'\nBroker target: Rs ' || b.target_price end
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
create trigger broker_agreement_after_verdict after insert on ai_broker_verdict
    for each row execute function queue_broker_agreement();

create function cancel_deleted_broker_agreement() returns trigger language plpgsql as $$
begin
    update alert_outbox o set status='cancelled',last_error='Broker call deleted'
        from alert_log a where a.alert_id=o.alert_id
          and a.dedupe_key='broker_agreement:' || old.broker_call_id
          and o.status in ('pending','failed');
    return old;
end $$;
create trigger broker_agreement_before_delete before delete on broker_call
    for each row execute function cancel_deleted_broker_agreement();
