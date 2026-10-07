-- Market sentiment in the scores (igs.sentiment; scoring.yaml, sentiment).
-- The whole-market mood of each run: its readings, the mood and the pillar weights it
-- tilted to.
alter table score_run add column market_sentiment jsonb;

-- Each stock's sentiment overlay: brokers' calls and news tone, capped, with what it
-- rests on. composite = base_composite + geopolitical_adjustment + sentiment_adjustment.
alter table score_result add column sentiment_adjustment double precision not null default 0;
alter table score_result add column sentiment_evidence jsonb not null default '{}';

-- The tone of stock news for each company it is about, read by the AI
-- (igs.assistant.news_tone) from the articles in broker_article. Kept while the article
-- is: a past run must be re-scorable with what it saw.
create table stock_news_tone (
    tone_id        bigint generated always as identity primary key,
    article_id     bigint      not null references broker_article,
    company_id     bigint      references company,       -- null: the company not matched
    company_text   text        not null,                 -- as the AI named it
    tone           numeric     not null check (tone between -1 and 1),
    confidence     numeric     not null check (confidence between 0 and 1),
    reason         text        not null,
    quote          text        not null,                 -- the article's own words
    model          text        not null,
    prompt_version text        not null,
    assessed_at    timestamptz not null default clock_timestamp(),
    unique (article_id, company_text)
);
create index stock_news_tone_company_idx on stock_news_tone (company_id, assessed_at);

alter table broker_article add column tone_read_at timestamptz;
alter table broker_article add column tone_attempts integer not null default 0;
alter table broker_article add column tone_error text;

alter table llm_call drop constraint llm_call_feature_check;
alter table llm_call add constraint llm_call_feature_check
    check (feature in ('ask', 'brief', 'announcements', 'geopolitical', 'call', 'brokers',
                       'news_tone'));
