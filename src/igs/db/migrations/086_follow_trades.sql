-- Paper trades following the winning traders of NSE's bulk deal files: what a consistent
-- winner carried at the close, bought or sold short the next session from its open to its
-- close, after charges (igs.follow_winners, docs/DEAL_TRADERS.md).
create table follow_trade (
    trade_id       bigserial primary key,
    signal_date    date     not null,          -- the deal file's day
    symbol         text     not null,
    company_id     bigint references company,
    side           smallint not null check (side in (1, -1)),   -- 1 buy, -1 short sale
    clients        text     not null,          -- the winners who carried it
    carried_inr    numeric  not null,          -- what they carried, at their prices
    session        date     not null,          -- the session it is for
    status         text     not null check (status in
                       ('open', 'closed', 'no fill', 'not intraday')),
    created_at     timestamptz not null,
    signal_sent_at timestamptz,
    trade_date     date,                       -- the session it was settled on
    open_price     numeric,
    close_price    numeric,
    entry          numeric,                    -- the open, with slippage
    exit           numeric,                    -- the close, with slippage
    quantity       integer,
    gross_inr      numeric,
    charges_inr    numeric,
    net_inr        numeric,
    net_pct        numeric,
    settled_at     timestamptz,
    result_sent_at timestamptz,
    unique (signal_date, symbol)
);
create index follow_trade_open on follow_trade (status) where status = 'open';
