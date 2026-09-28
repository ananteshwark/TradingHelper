create table geopolitical_news (
    news_id bigint generated always as identity primary key,
    url text not null unique,
    title text not null,
    body text not null,
    published_at timestamptz not null,
    received_at timestamptz not null default clock_timestamp(),
    content_hash text not null unique,
    companies jsonb not null
);
create table geopolitical_assessment (
    assessment_id bigint generated always as identity primary key,
    news_id bigint not null references geopolitical_news(news_id),
    company_id bigint not null references company(company_id),
    impact double precision not null check (impact between -1 and 1),
    confidence double precision not null check (confidence between 0 and 1),
    rationale text not null,
    evidence text not null,
    channel text not null,
    model text not null,
    prompt_version text not null,
    assessed_at timestamptz not null default clock_timestamp(),
    unique (news_id, company_id)
);
create index geopolitical_assessed_at_idx on geopolitical_assessment(assessed_at);
alter table score_result add column base_composite double precision;
alter table score_result add column geopolitical_adjustment double precision not null default 0;
alter table score_result add column geopolitical_evidence jsonb not null default '[]';
