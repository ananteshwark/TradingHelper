-- Safeguards: check severities, robustness gates, High conviction blockers and
-- run health are stored with every run, so a ranking can always show why a
-- stock was or was not High conviction.

alter table red_flag_result
    add column severity           text    not null default 'reject',
    add column unavailable_blocks boolean not null default true;

alter table score_result
    add column rank_pct             double precision,
    add column weight_stability     double precision,
    add column persist_hits         integer,
    add column persist_dates        integer,
    add column positive_pillars     integer,
    add column scored_pillars       integer,
    add column weakest_pillar       text,
    add column weakest_pillar_score double precision,
    add column top_factor           text,
    add column top_factor_share     double precision,
    add column hc_blockers          text[] not null default '{}';

-- {"issues": [...], "summary": {...}}; issues withheld High conviction for the run.
alter table score_run
    add column health jsonb not null default '{}';
