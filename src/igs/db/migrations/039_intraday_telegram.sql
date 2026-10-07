-- One delivered alert per stock, direction and Indian trading day. Unsent alerts
-- can refresh from a newer scan; delivered alerts are never re-armed that day.
create table intraday_telegram (
    company_id bigint not null references company,
    trading_day date not null,
    action text not null check(action in ('buy','sell')),
    scan_id bigint not null references intraday_scan,
    symbol text not null,
    result jsonb not null,
    expires_at timestamptz not null,
    status text not null default 'pending' check(status in ('pending','expired','sent')),
    sent_at timestamptz,
    attempts integer not null default 0,
    next_attempt_at timestamptz not null default now(),
    last_error text,
    primary key(company_id,trading_day,action)
);
