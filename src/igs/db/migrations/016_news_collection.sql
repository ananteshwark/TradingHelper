create table geopolitical_feed_fetch (
    fetch_id bigint generated always as identity primary key,
    feed_name text not null,
    feed_url text not null,
    fetched_at timestamptz not null default clock_timestamp(),
    http_status integer,
    payload bytea,
    imported integer not null default 0,
    skipped integer not null default 0,
    error text
);
create index geopolitical_feed_fetch_url_time on geopolitical_feed_fetch(feed_url, fetched_at desc);
alter table geopolitical_news add column feed_fetch_id bigint references geopolitical_feed_fetch;
alter table geopolitical_news add column intake text not null default 'manual'
    check (intake in ('manual', 'rss'));
alter table geopolitical_news add column assessment_attempts integer not null default 0;
alter table geopolitical_news add column assessment_error text;
alter table geopolitical_news add column next_assessment_at timestamptz not null default now();
