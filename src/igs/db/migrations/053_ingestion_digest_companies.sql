-- The daily data ingestion summary counts unique companies: each ingestion event also keeps
-- the distinct company identifiers of its rows, whichever the table has, in this order:
-- company_id ('c:'), security_id ('s:'), ISIN ('i:', also from an NSE_EQ instrument key),
-- NSE symbol ('n:'). The dispatcher resolves them to companies.
create function operational_company_key(r jsonb) returns text language sql immutable as $$
    select case
        when r->>'company_id' is not null then 'c:' || (r->>'company_id')
        when r->>'security_id' is not null then 's:' || (r->>'security_id')
        when r->>'isin' is not null then 'i:' || (r->>'isin')
        when r->>'instrument_key' like 'NSE_EQ|%' then 'i:' || split_part(r->>'instrument_key', '|', 2)
        when r->>'symbol' is not null then 'n:' || (r->>'symbol')
    end
$$;

create or replace function notify_ingested_rows() returns trigger language plpgsql as $$
declare n bigint; keys jsonb;
begin
    if tg_op = 'UPDATE' then
        select count(*), coalesce(jsonb_agg(distinct k) filter (where k is not null), '[]')
          into n, keys
          from (select operational_company_key(to_jsonb(c)) k
                  from (select * from inserted_rows except all select * from prior_rows) c) x;
    else
        select count(*), coalesce(jsonb_agg(distinct k) filter (where k is not null), '[]')
          into n, keys
          from (select operational_company_key(to_jsonb(r)) k from inserted_rows r) x;
    end if;
    if n > 0 then
        insert into operational_notification(event_key, kind, payload)
        values ('ingestion:' || tg_op || ':' || tg_table_name || ':' || pg_current_xact_id()::text,
                'ingestion', jsonb_build_object('table', tg_table_name, 'rows', n,
                                                'operation', lower(tg_op), 'keys', keys))
        on conflict (event_key) do update set payload = jsonb_set(jsonb_set(
            operational_notification.payload, '{rows}',
            to_jsonb((operational_notification.payload->>'rows')::bigint + n)), '{keys}',
            (select coalesce(jsonb_agg(distinct v), '[]') from jsonb_array_elements(
                coalesce(operational_notification.payload->'keys', '[]') || (excluded.payload->'keys')) v));
    end if;
    return null;
end $$;
