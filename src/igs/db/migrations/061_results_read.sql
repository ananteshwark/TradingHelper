-- How each newly filed quarterly report reads from its own figures (igs.results_read), and
-- whether Telegram got it: as shipped, only reports that read positive are sent
-- (config/alerts.yaml, rules.quarterly_results). Every report is read and kept, sent or not,
-- so the Results days page can show whether positive reads were followed by rises.
create table results_read (
    notification_id bigint  primary key references operational_notification,
    company_id      bigint  references company,
    period_end      date    not null,
    basis           text    check (basis in ('standalone', 'consolidated')),
    filed_at        timestamptz,
    verdict         text    not null check (verdict in ('positive', 'mixed', 'negative',
                                                        'unknown')),
    revenue         numeric,     -- the quarter's, in rupees (interest earned for banks)
    revenue_prev    numeric,     -- the same quarter a year earlier
    profit          numeric,     -- attributable to the owners where reported, else net profit
    profit_prev     numeric,
    revenue_yoy_pct numeric,
    profit_yoy_pct  numeric,
    surprise        numeric,     -- the profit change on a year earlier over its usual size
    reasons         text    not null,
    sent            boolean not null,
    read_at         timestamptz not null default now()
);
create index results_read_company_idx on results_read (company_id, period_end);

-- A report that was read and not sent is done, but was not delivered: `skipped` says why,
-- and sent_at stays empty.
alter table operational_notification add column skipped text;
drop index operational_pending;
create index operational_pending on operational_notification (next_attempt_at)
    where sent_at is null and skipped is null;
