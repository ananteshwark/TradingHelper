-- Transactional notifications: rollback of ingested data also rolls back its event.
-- Installing this migration does not notify historical rows.
create table operational_notification (
    notification_id bigserial primary key,
    event_key text not null unique,
    kind text not null check (kind in ('ingestion', 'quarterly_report', 'issue')),
    payload jsonb not null,
    created_at timestamptz not null default now(),
    sent_at timestamptz,
    attempts integer not null default 0,
    next_attempt_at timestamptz not null default now(),
    last_error text
);
create index operational_pending on operational_notification (next_attempt_at)
    where sent_at is null;

create function notify_ingested_rows() returns trigger language plpgsql as $$
declare n bigint;
begin
    if tg_op = 'UPDATE' then
        select count(*) into n from
            (select * from inserted_rows except all select * from prior_rows) changed;
    else
        select count(*) into n from inserted_rows;
    end if;
    if n > 0 then
        insert into operational_notification(event_key, kind, payload)
        values ('ingestion:' || tg_op || ':' || tg_table_name || ':' || pg_current_xact_id()::text,
                'ingestion', jsonb_build_object('table', tg_table_name, 'rows', n, 'operation', lower(tg_op)))
        on conflict (event_key) do update set payload = jsonb_set(
            operational_notification.payload, '{rows}',
            to_jsonb((operational_notification.payload->>'rows')::bigint + n));
    end if;
    return null;
end $$;

do $$
declare t text;
begin
    foreach t in array array['price_eod', 'price_eod_fallback', 'corporate_action',
        'index_price', 'trading_holiday', 'nse_equity_list', 'bse_scrip',
        'broker_instrument', 'surveillance_snapshot', 'shareholding', 'announcement',
        'filing_ref', 'filing', 'fundamental_fact', 'insider_trade',
        'insider_disclosure_ref', 'geopolitical_news', 'broker_article', 'broker_call',
        'forward_document', 'industry_classification', 'screener_enrichment']
    loop
        execute format('create trigger operational_ingestion after insert on %I '
            'referencing new table as inserted_rows for each statement '
            'execute function notify_ingested_rows()', t);
    end loop;
end $$;

-- Corrections and delivery-volume additions are data changes too. No-op updates are ignored.
do $$
declare t text;
begin
    foreach t in array array['price_eod', 'price_eod_fallback', 'index_price',
        'trading_holiday', 'screener_enrichment', 'industry_classification',
        'nse_equity_list', 'bse_scrip', 'broker_instrument']
    loop
        execute format('create trigger operational_update after update on %I '
            'referencing new table as inserted_rows old table as prior_rows '
            'for each statement execute function notify_ingested_rows()', t);
    end loop;
end $$;

create function notify_quarterly_report() returns trigger language plpgsql as $$
begin
    insert into operational_notification(event_key, kind, payload)
    select distinct 'quarter:' || f.exchange || ':' || f.filing_system || ':' ||
        f.content_sha256 || ':' || f.period_end::text, 'quarterly_report',
        jsonb_build_object('company', c.name, 'company_id', f.company_id,
            'period_end', f.period_end, 'basis', f.statement_basis,
            'filed_at', f.filed_at, 'exchange', f.exchange)
    from inserted_rows r join filing f using (filing_id) join company c on c.company_id=f.company_id
    where r.period_type='Q' and r.period_end=f.period_end
        and f.filing_type='financial_results'
    on conflict (event_key) do nothing;
    return null;
end $$;
create trigger operational_quarter after insert on fundamental_fact
    referencing new table as inserted_rows for each statement
    execute function notify_quarterly_report();

create function notify_data_issues() returns trigger language plpgsql as $$
begin
    insert into operational_notification(event_key, kind, payload)
    select 'dq:' || pg_current_xact_id()::text || ':' || severity || ':' || category,
        'issue', jsonb_build_object('category', category, 'severity', severity, 'count', count(*))
    from inserted_rows where severity in ('warn','error') group by severity, category
    on conflict (event_key) do update set payload = jsonb_set(
        operational_notification.payload, '{count}',
        to_jsonb((operational_notification.payload->>'count')::bigint +
                 (excluded.payload->>'count')::bigint));
    return null;
end $$;
create trigger operational_issue after insert on dq_issue
    referencing new table as inserted_rows for each statement
    execute function notify_data_issues();
