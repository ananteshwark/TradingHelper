-- Realised result of a closed intraday trade, from the average fills of its entry and
-- exit orders on Upstox, less estimated charges (igs.intraday.trading.record_pnl).
alter table intraday_trade
    add column entry_fill numeric,         -- average entry price
    add column exit_fill numeric,          -- average exit price (target or stop order)
    add column filled_quantity integer,
    add column gross_pnl numeric,          -- before charges
    add column charges numeric,            -- estimated from config/costs.yaml at the fills
    add column net_pnl numeric,            -- gross_pnl - charges
    add column pnl_note text,              -- why a closed trade has no figure
    add column pnl_checked_at timestamptz;
