create table alert_outbox (
    alert_id bigint not null references alert_log(alert_id),
    channel text not null check (channel in ('email', 'telegram')),
    status text not null default 'pending' check (status in ('pending', 'failed', 'sent')),
    attempts integer not null default 0,
    last_error text,
    next_attempt_at timestamptz not null default now(),
    sent_at timestamptz,
    primary key (alert_id, channel)
);
create index alert_outbox_pending_idx on alert_outbox(channel, next_attempt_at)
    where status <> 'sent';
