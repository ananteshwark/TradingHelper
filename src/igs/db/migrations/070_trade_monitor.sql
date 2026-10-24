-- Independent of the order record: never rewrite the original risk or call.
create table trade_monitor_state (
    trade_id bigint primary key references intraday_trade,
    peak numeric not null,
    observed_stop numeric not null,
    updated_at timestamptz not null,
    snapshot jsonb not null
);
create table trade_monitor_event (
    event_id bigint generated always as identity primary key,
    trade_id bigint not null references intraday_trade,
    created_at timestamptz not null,
    mode text not null check(mode in ('observe','execute')),
    status text not null,
    detail jsonb not null
);
create index trade_monitor_event_trade on trade_monitor_event(trade_id,event_id desc);
-- An interrupted/ambiguous broker modification blocks subsequent modifications.
create unique index trade_monitor_pending on trade_monitor_event(trade_id)
    where status in ('submitting','uncertain');
