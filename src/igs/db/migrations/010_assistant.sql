-- Optional research assistant (igs.assistant). Nothing in these tables feeds scoring.

-- Every model call, for the daily budget and for audit. cost_usd is an estimate from the
-- configured prices.
create table llm_call (
    call_id            bigserial   primary key,
    called_at          timestamptz not null default now(),
    feature            text        not null check (feature in ('ask', 'brief', 'announcements')),
    model              text        not null,
    input_tokens       integer     not null,
    output_tokens      integer     not null,
    cache_read_tokens  integer     not null default 0,
    cache_write_tokens integer     not null default 0,
    cost_usd           numeric(12, 6) not null,
    stop_reason        text,
    request_id         text
);
create index llm_call_day_idx on llm_call (called_at);

-- The assistant's reading of an announcement. Keyed on the announcement's natural key, not
-- ann_id, so notes survive `igs rebuild` (which reloads announcements with new ids).
create table announcement_note (
    exchange        text        not null,
    symbol          text        not null,
    filed_at        timestamptz not null,
    subject         text        not null,
    category        text        not null,
    materiality     text        not null check (materiality in ('low', 'medium', 'high')),
    summary         text        not null,
    concerns        text[]      not null default '{}',
    model           text        not null,
    prompt_version  text        not null,
    created_at      timestamptz not null default now(),
    primary key (exchange, symbol, filed_at, subject)
);
create index announcement_note_created_idx on announcement_note (created_at);

-- Plain-language briefs, one per run, stock and prompt version (briefs are not re-bought).
create table assistant_brief (
    run_id          bigint      not null,
    symbol          text        not null,
    prompt_version  text        not null,
    model           text        not null,
    text            text        not null,
    created_at      timestamptz not null default now(),
    primary key (run_id, symbol, prompt_version)
);
