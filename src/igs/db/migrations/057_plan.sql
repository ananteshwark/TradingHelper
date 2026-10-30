-- The Plan: a paper portfolio of momentum stocks, gold and cash kept in rupees with whole
-- shares (igs.plan, docs/PLAN.md).

-- The plan after each session: cash, holdings at the close, and units, so that money added
-- or withdrawn doesn't count as a return (the value of a unit starts at 100).
create table plan_day (
    trade_date   date           primary key,
    cash_inr     numeric(18, 2) not null,
    holdings     jsonb          not null,      -- [{symbol, company_id, kind, shares, close, value_inr}]
    value_inr    numeric(18, 2) not null,
    units        numeric(20, 6) not null check (units > 0),
    unit_value   numeric(18, 6) not null,
    nifty_close  numeric(18, 4),
    flows_inr    numeric(18, 2) not null default 0,   -- money added (+) or withdrawn (-) that day
    charges_inr  numeric(18, 2) not null default 0,   -- costs of that day's orders
    dividends_inr numeric(18, 2) not null default 0,
    created_at   timestamptz    not null default now()
);

-- Orders worked out at a close for the next session's open, and how they were filled.
create table plan_rebalance (
    signal_date  date        primary key,          -- the close the orders come from
    session_date date        not null,             -- the open they are for
    reason       text        not null check (reason in ('start', 'monthly', 'quarterly', 'drift')),
    all_parts    boolean     not null,             -- stocks, gold and cash brought to their shares
    targets      jsonb       not null,             -- [{symbol, company_id, kind, rank, weight_pct, value_inr}]
    orders       jsonb       not null,             -- [{symbol, company_id, kind, side, shares, price, why, fill_price, filled, charges_inr}]
    note         text        not null default '',
    filled_on    date,
    created_at   timestamptz not null default now()
);

-- Money added to (+) or withdrawn from (-) the plan, applied at the next session's open.
create table plan_flow (
    flow_id     bigserial      primary key,
    amount_inr  numeric(18, 2) not null check (amount_inr <> 0),
    note        text           not null default '',
    created_at  timestamptz    not null default now(),
    applied_on  date
);
