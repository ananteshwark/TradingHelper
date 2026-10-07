-- Brokers' buy / hold / sell calls (igs.brokers): a second opinion for the AI's own calls,
-- never used in the ranking. They come from news articles the AI reads (the feeds in
-- config/broker_calls.yaml; the response is kept in geopolitical_feed_fetch) or are
-- entered by the owner, e.g. from Moneycontrol.
create table broker_article (
    article_id   bigint generated always as identity primary key,
    url          text        not null unique,
    title        text        not null,
    body         text        not null,
    published_at timestamptz not null,
    feed_name    text        not null,
    fetch_id     bigint      references geopolitical_feed_fetch,
    received_at  timestamptz not null default clock_timestamp(),
    -- Passed the keyword filter (a rating, target or brokerage is mentioned), so the AI
    -- reads it; the rest are kept only for the record and deleted after 30 days.
    candidate    boolean     not null,
    read_at      timestamptz,
    read_attempts integer    not null default 0,
    read_error   text
);
create index broker_article_unread_idx on broker_article (published_at)
    where candidate and read_at is null;

create table broker_call (
    broker_call_id bigint generated always as identity primary key,
    company_id   bigint      references company,      -- null: the stock was not matched
    stock_name   text        not null,                -- as the source wrote it
    broker       text        not null,
    stance       text        not null check (stance in ('buy', 'hold', 'sell')),
    rating       text        not null,                -- as written: Accumulate, Overweight...
    kind         text        not null check (kind in ('research', 'trading')),
    target_price numeric     check (target_price > 0),
    called_on    date        not null,
    source       text        not null check (source in ('news', 'manual')),
    article_id   bigint      references broker_article,
    url          text,
    quote        text,                                -- the sentence it was read from
    model        text,
    prompt_version text,
    created_at   timestamptz not null default clock_timestamp(),
    -- One call per broker, stock, day, stance and target, however many articles repeat it.
    dedupe_key   text        not null unique
);
create index broker_call_company_idx on broker_call (company_id, called_on desc);

alter table llm_call drop constraint llm_call_feature_check;
alter table llm_call add constraint llm_call_feature_check
    check (feature in ('ask', 'brief', 'announcements', 'geopolitical', 'call', 'brokers'));

-- How the AI's call compares with the brokers' recent calls (prompt call-v3 on).
alter table ai_call add column vs_brokers text;
