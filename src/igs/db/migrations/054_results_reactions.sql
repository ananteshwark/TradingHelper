-- Results reactions, tracked forward on paper (igs.results_drift). Nothing here orders.

-- Each company's results for a period (its first NSE financial-results filing) and the
-- price reaction: the close from the session before the filing day to the session after,
-- less the average of the liquid stocks over the same days.
create table results_reaction (
    company_id     bigint  not null references company,
    period_end     date    not null,
    symbol         text    not null,
    filed_at       timestamptz not null,
    before_date    date    not null,        -- the last session before the filing day
    after_date     date    not null,        -- the first session after it
    reaction_pct   numeric not null,
    universe_pct   numeric not null,
    abnormal_pct   numeric not null,        -- reaction less the liquid stocks' average
    flag_until     date,                    -- a -5% or worse reaction: flagged to this session
    decided_at     timestamptz not null default now(),
    primary key (company_id, period_end)
);
create index results_reaction_flag_idx on results_reaction (company_id, after_date)
    where abnormal_pct <= -5;

-- The paper buys after a +5% or better reaction, held 21 sessions from the next open.
create table results_trade (
    company_id     bigint  not null references company,
    period_end     date    not null,
    symbol         text    not null,
    abnormal_pct   numeric not null,
    entry_date     date    not null,        -- the open of the second session after filing
    exit_due       date    not null,        -- the open 21 sessions later
    status         text    not null check (status in ('open', 'closed', 'missed', 'no data')),
    entry          numeric,
    exit_date      date,
    exit           numeric,
    return_pct     numeric,
    universe_pct   numeric,
    cost_pct       numeric,
    net_excess_pct numeric,                 -- return less the liquid average, less costs
    universe       jsonb   not null,        -- the liquid companies on the session after
    updated_at     timestamptz not null default now(),
    primary key (company_id, period_end),
    foreign key (company_id, period_end) references results_reaction
);

-- The momentum paper portfolios gain a third track: the rule, skipping stocks flagged by a
-- bad results reaction.
alter table momentum_rebalance drop constraint momentum_rebalance_track_check;
alter table momentum_rebalance add constraint momentum_rebalance_track_check
    check (track in ('rule', 'ai', 'results'));
alter table momentum_period drop constraint momentum_period_track_check;
alter table momentum_period add constraint momentum_period_track_check
    check (track in ('rule', 'ai', 'results'));
