-- Same-day buying and selling by the clients named in NSE's bulk deal files: each one's
-- intraday bets and how they came out (igs.deal_traders, docs/DEAL_TRADERS.md).
create table deal_trader_bet (
    trade_date        date    not null,
    symbol            text    not null,
    client            text    not null,
    company_id        bigint references company,
    buy_qty           numeric not null,
    buy_price         numeric,               -- volume-weighted
    sell_qty          numeric not null,
    sell_price        numeric,
    matched_qty       numeric not null,      -- bought and sold the same day
    pnl_inr           numeric,               -- on the matched quantity, before charges
    ret_pct           numeric,               -- sell price over buy price, less one, %
    net_qty           numeric not null,      -- bought less sold: carried to the close
    close             numeric,               -- the day's close, once loaded
    net_pnl_inr       numeric,               -- the carried quantity marked at the close
    next_vs_index_pct numeric,               -- next session against the Nifty 500, signed
                                             -- for the carried side
    source_hash       text    not null,      -- the intraday_deal_fetch file read last
    updated_at        timestamptz not null,
    primary key (trade_date, symbol, client)
);
create index deal_trader_bet_client on deal_trader_bet (client, trade_date);

-- Which collected bulk deal files were read.
create table deal_trader_scan (
    content_hash text primary key,
    scanned_at   timestamptz not null,
    bets         integer not null
);

-- Clients already named on Telegram as consistent winners.
create table deal_trader_flag (
    client     text primary key,
    flagged_at timestamptz not null,
    bets       integer,
    wins       integer,
    alert_text text
);
