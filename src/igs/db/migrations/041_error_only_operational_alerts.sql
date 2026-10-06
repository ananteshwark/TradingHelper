-- Keep warnings in dq_issue for review, but notify only errors on Telegram.
create or replace function notify_data_issues() returns trigger language plpgsql as $$
begin
    insert into operational_notification(event_key, kind, payload)
    select 'dq:' || pg_current_xact_id()::text || ':' || severity || ':' || category,
        'issue', jsonb_build_object('category', category, 'severity', severity, 'count', count(*))
    from inserted_rows where severity = 'error' group by severity, category
    on conflict (event_key) do update set payload = jsonb_set(
        operational_notification.payload, '{count}',
        to_jsonb((operational_notification.payload->>'count')::bigint +
                 (excluded.payload->>'count')::bigint));
    return null;
end $$;

-- Old unsent warning events must not leak out after the new filter is installed.
-- The underlying data-quality records remain available in the application.
delete from operational_notification
where kind = 'issue' and payload->>'severity' = 'warn' and sent_at is null;
